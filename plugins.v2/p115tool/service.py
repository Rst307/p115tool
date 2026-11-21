"""Personal-drive traversal and stable STRM mapping, with explicit storage tasks."""
from pathlib import Path
from contextlib import closing, contextmanager
import json
import secrets
import sqlite3
import threading
import hashlib
try:
    from app.log import logger
except ModuleNotFoundError:
    import logging
    logger = logging.getLogger('p115tool')
import time
from urllib.parse import urlsplit
from .client import P115ClientManager
from .models import SafetyError, MissingFile, ToolError
from .strm import StrmManager, classify, safe_parts

class Service:
    def __init__(self,config,client=None,recognizer=None,virtual_organizer=None):
        self.config=config
        self.client=client or P115ClientManager(config)
        self._lock=threading.RLock(); self._stop=threading.Event(); self._worker=None
        self._playback_idle = threading.Condition(self._lock)
        self._active_playbacks = 0
        self._closed=False; self.recognizer=recognizer
        self._db=None
        from .virtual_organize import organize_virtual
        self.virtual_organizer=virtual_organizer or organize_virtual
        self._auto_timer=None; self._auto_revision=0; self._rerun=False
        directory=Path(config.data_dir); directory.mkdir(parents=True,exist_ok=True)
        self.path=directory/'media.sqlite3'
        self._file_lock=open(directory/'strm-service.lock','a+b')
        try:
            if __import__('os').name=='nt':
                import msvcrt
                self._file_lock.seek(0); self._file_lock.write(b'0'); self._file_lock.flush(); self._file_lock.seek(0)
                msvcrt.locking(self._file_lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self._file_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.strm=StrmManager(config)
            with closing(sqlite3.connect(self.path)) as db,db:
                db.executescript('''CREATE TABLE IF NOT EXISTS strm_files (
                    file_id TEXT PRIMARY KEY,token TEXT UNIQUE NOT NULL,pickcode TEXT NOT NULL,
                    name TEXT NOT NULL,size INTEGER NOT NULL,sha1 TEXT NOT NULL,parent_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,strm_path TEXT);
                    CREATE TABLE IF NOT EXISTS strm_state (id INTEGER PRIMARY KEY CHECK(id=1),value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS strm_relocations (
                    file_id TEXT NOT NULL,old_path TEXT NOT NULL,new_path TEXT NOT NULL,state TEXT NOT NULL,
                    PRIMARY KEY(file_id,old_path));
                    CREATE TABLE IF NOT EXISTS strm_output_proofs (
                    path TEXT PRIMARY KEY,file_id TEXT NOT NULL,digest TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS strm_address_updates (
                    id INTEGER PRIMARY KEY,path TEXT NOT NULL,old_digest TEXT NOT NULL,
                    new_digest TEXT NOT NULL,state TEXT NOT NULL);''')
                # Upgrade only ordinary, retained sources. Old share/cache mappings
                # and all old jobs/checkpoints are preserved in storage but not used.
                tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if {'media','normal_objects'}<=tables:
                    db.execute('''INSERT OR IGNORE INTO strm_files
                        SELECT n.file_id,m.token,n.pickcode,m.file_name,n.size,n.sha1,n.parent_id,
                        ltrim(m.virtual_path,'/'),m.strm_path FROM media m JOIN normal_objects n ON n.media_id=m.id
                        WHERE m.storage_type='NORMAL' AND m.source_deleted=0''')
            old=self._state()
            if old.get('state')=='RUNNING': old['state']='INTERRUPTED'; self._save_state(old)
            from .storage import StorageManager
            self.storage=StorageManager(self)
            from .telegram import TelegramMonitor
            self.telegram=TelegramMonitor(self)
        except Exception:
            if self._db is not None: self._db.close()
            self.client.close(); self._file_lock.close(); raise

    def _conn(self):
        if self._closed and self._db is None:
            raise ToolError('Service stopped')
        if self._db is None:
            self._db = sqlite3.connect(self.path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
        return self._db

    @contextmanager
    def transaction(self):
        with self._lock:
            with self._conn() as db:
                yield db

    def query(self,sql,args=(),one=False):
        # All callers (workers, playback and status) share the service lock.
        # Keep each write durable before returning, but reuse the connection and
        # its prepared-statement cache rather than reopening it for every file.
        with self._lock:
            with self._conn() as db:
                cursor=db.execute(sql,args)
                if cursor.description:
                    rows=[dict(r) for r in cursor.fetchall()]
                    return (rows[0] if rows else None) if one else rows

    def _state(self):
        row=self.query('SELECT value FROM strm_state WHERE id=1',one=True)
        return json.loads(row['value']) if row else {'state':'IDLE','found':0,'generated':0,'failed':0,'error':None}

    def _save_state(self,state):
        self.query('INSERT INTO strm_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',(json.dumps(state),))

    def snapshot(self):
        with self._lock:
            return {**self._state(),'count':self.query('SELECT count(*) count FROM strm_files f WHERE NOT EXISTS (SELECT 1 FROM virtual_editions e WHERE e.media_id=f.file_id AND e.stable_id!=e.media_id)',one=True)['count']}

    def start(self,rerun=False):
        with self._lock:
            if self._closed: raise ToolError('Service stopped')
            if self.storage.busy:
                if rerun:
                    self._rerun=True
                    return self.snapshot()
                raise ToolError('Storage task running')
            if self._worker and self._worker.is_alive():
                if rerun: self._rerun=True
                return self.snapshot()
            if not self.config.source_cids and not self.storage.virtual_rows(): raise ValueError('Configure source folders')
            state = {'state':'RUNNING','mode':'generate','found':0,'generated':0,'failed':0,'error':None}
            self._save_state(state)
            self._worker=threading.Thread(target=self._generate_pending,name='p115tool-strm',daemon=True)
            try:
                self._worker.start()
            except RuntimeError:
                self._worker = None
                self._save_state({**state, 'state':'FAILED', 'error':'START_FAILED'})
                raise ToolError('STRM task did not start') from None
            return self.snapshot()

    def start_address_refresh(self):
        with self._lock:
            if self._closed: raise ToolError('Service stopped')
            if self.storage.busy:
                raise ToolError('Storage task running')
            if self._worker and self._worker.is_alive():
                return self.snapshot()
            state = {'state':'RUNNING','mode':'refresh_addresses','found':0,'generated':0,'failed':0,'skipped':0,'error':None}
            self._save_state(state)
            self._worker=threading.Thread(target=self._refresh_addresses_worker,name='p115tool-strm-address',daemon=True)
            try:
                self._worker.start()
            except RuntimeError:
                self._worker = None
                self._save_state({**state, 'state':'FAILED', 'error':'START_FAILED'})
                raise ToolError('STRM task did not start') from None
            return self.snapshot()

    def _refresh_addresses_worker(self):
        self.refresh_addresses()
        with self._lock:
            if self._closed or not self._rerun:
                self._worker=None
                self.storage.drain_pending()
                return
            self._rerun=False
        self._generate_pending()

    def refresh_addresses(self):
        state={'state':'RUNNING','mode':'refresh_addresses','found':0,'generated':0,'failed':0,'skipped':0,'error':None}
        self._save_state(state)
        last_progress=time.monotonic()
        def save_progress():
            nonlocal last_progress
            now=time.monotonic()
            if now-last_progress>=1:
                self._save_state(state)
                last_progress=now
        reasons={}
        try:
            rows=self.query(
                "SELECT f.file_id, f.token, f.relative_path, f.strm_path FROM strm_files f "
                "WHERE f.strm_path IS NOT NULL AND f.strm_path != '' "
                "AND NOT EXISTS (SELECT 1 FROM virtual_editions e WHERE e.media_id=f.file_id AND e.stable_id!=e.media_id) "
                "AND NOT EXISTS (SELECT 1 FROM removed_storage_records r WHERE r.media_id=f.file_id)"
            )
            token_owners={r['token']: r['file_id'] for r in rows}
            play_prefix=self.config.playback_prefix+'/play/'
            for row in rows:
                if self._stop.is_set(): raise InterruptedError()
                state['found']+=1
                try:
                    candidate=None
                    recorded=Path(row['strm_path'])
                    if recorded.is_symlink():
                        if recorded.parent.resolve().is_relative_to(self.strm.root):
                            raise SafetyError('Unsafe output target')
                    else:
                        resolved_recorded=recorded.resolve()
                        if resolved_recorded.is_relative_to(self.strm.root) and resolved_recorded.suffix.lower()=='.strm' and resolved_recorded.is_file():
                            candidate=resolved_recorded
                    if candidate is None and row['relative_path']:
                        try:
                            fallback=self.strm.root.joinpath(*safe_parts(row['relative_path']+'.strm'))
                        except SafetyError:
                            fallback=None
                        if fallback is not None:
                            if fallback.is_symlink():
                                raise SafetyError('Unsafe output target')
                            resolved_fallback=fallback.resolve()
                            if resolved_fallback.is_relative_to(self.strm.root) and resolved_fallback.suffix.lower()=='.strm' and resolved_fallback.is_file():
                                candidate=resolved_fallback
                    if candidate is None:
                        state['skipped']+=1
                        save_progress()
                        continue
                    resolved=candidate
                    proof=self.query('SELECT file_id FROM strm_output_proofs WHERE path=?',(str(resolved),),one=True)
                    if proof and proof['file_id']!=row['file_id']:
                        state['skipped']+=1
                        save_progress()
                        continue
                    if proof is None:
                        try:
                            raw_line=resolved.read_text('utf-8').replace('\r\n','\n').rstrip('\n')
                            parsed_path=urlsplit(raw_line).path
                            if parsed_path.startswith(play_prefix):
                                disk_token=parsed_path[len(play_prefix):]
                                owner=token_owners.get(disk_token)
                                if owner and owner!=row['file_id']:
                                    state['skipped']+=1
                                    save_progress()
                                    continue
                        except (OSError,UnicodeError,ValueError):
                            pass
                    rel=resolved.relative_to(self.strm.root).as_posix()[:-5]
                    self.write_output(row['file_id'], rel, row['token'])
                    state['generated']+=1
                except Exception as exc:
                    state['failed']+=1
                    state['error']='OUTPUT_CONFLICT' if isinstance(exc,SafetyError) else 'FILE_GENERATION_FAILED'
                    key=exc.args[0] if isinstance(exc,ToolError) and exc.args and isinstance(exc.args[0],str) else type(exc).__name__
                    reasons[key]=reasons.get(key,0)+1
                save_progress()
            state['state']='DONE' if not state['failed'] else 'PARTIAL'
        except InterruptedError:
            state['state']='INTERRUPTED'
        except Exception:
            state['state']='FAILED'
            state['error']='ADDRESS_REFRESH_FAILED'
        self._save_state(state)
        logger.info(
            f"115本地STRM地址更新结束：状态={state['state']}，检查={state['found']}，成功={state['generated']}，"
            f"失败={state['failed']}，跳过={state['skipped']}，失败分类={reasons or '无'}"
        )

    def request_auto_generate(self):
        with self._lock:
            if self._closed or not self.config.auto_after_transfer or not self.config.source_cids: return
            self._auto_revision+=1
            revision=self._auto_revision
            if self._auto_timer: self._auto_timer.cancel()
            self._auto_timer=threading.Timer(10,self._auto_generate,args=(revision,))
            self._auto_timer.daemon=True
            self._auto_timer.start()

    def _auto_generate(self,revision):
        with self._lock:
            if self._closed or revision!=self._auto_revision: return
            self._auto_timer=None
            try: self.start(rerun=True)
            except Exception:
                logger.warning('115整理后自动生成未启动，请检查STRM配置和数据目录。')

    def _generate_pending(self):
        while True:
            self.generate_all()
            with self._lock:
                if self._closed or not self._rerun:
                    self._worker=None
                    self.storage.drain_pending()
                    return
                self._rerun=False

    def generate_all(self):
        state={'state':'RUNNING','mode':'generate','found':0,'generated':0,'failed':0,'error':None}
        self._save_state(state)
        last_progress=time.monotonic()
        def save_progress():
            nonlocal last_progress
            now=time.monotonic()
            if now-last_progress>=1:
                self._save_state(state)
                last_progress=now
        seen_files=set(); seen_dirs=set(); outputs={}; inventory=[]
        state['relocated']=0
        state['paths']=[]
        try:
            for source in self.config.source_cids:
                if self.storage.skip_directory(source['cid']): continue
                # Include ancestors even when the selected CID is a category or
                # a show folder and its manually configured prefix is only '/'.
                resolve_path=getattr(self.client,'directory_path',None)
                prefix=resolve_path(source['cid']) if callable(resolve_path) else source['prefix']
                stack=[(source['cid'],'')]
                while stack:
                    cid,relative=stack.pop()
                    if self._stop.is_set(): raise InterruptedError()
                    if cid in seen_dirs: continue
                    if len(seen_dirs)>=100000: raise SafetyError('Traversal limit')
                    seen_dirs.add(cid)
                    for file in self.client.list_files(cid):
                        if self._stop.is_set(): raise InterruptedError()
                        if file.parent_id!=cid: raise SafetyError('Unexpected parent')
                        safe_parts(file.name)
                        if '/' in file.name: raise SafetyError('Unexpected filename')
                        path='/'.join(filter(None,[relative,file.name]))
                        if file.is_dir:
                            if not self.storage.skip_directory(file.file_id): stack.append((file.file_id,path))
                            continue
                        if self.storage.skip_file(file.file_id): continue
                        if Path(file.name).suffix.lower() not in self.config.media_extensions or file.file_id in seen_files: continue
                        seen_files.add(file.file_id); state['found']+=1
                        try:
                            actual_path=file.path
                            if not file.pickcode:
                                detail=self.client.stat(file.file_id)
                                if detail.is_dir or (detail.file_id,detail.name,detail.size,detail.parent_id)!=(file.file_id,file.name,file.size,cid) or (file.sha1 and detail.sha1!=file.sha1): raise SafetyError('File changed')
                                file=detail
                            if not file.pickcode: raise SafetyError('Missing pickcode')
                            destination=classify('/' if actual_path else prefix,actual_path.lstrip('/') if actual_path else path,**({'recognizer':self.recognizer} if self.recognizer else {}))
                            token=self.register(file,destination)
                            inventory.append((file.file_id,actual_path or prefix.rstrip('/')+'/'+path))
                            output=self.write_output(file.file_id,destination,token,state)
                            outputs[self.config.playback_url(token)+'\n']=(file.file_id,output,token)
                            if len(state['paths'])<10:
                                state['paths'].append({'source':actual_path or prefix.rstrip('/')+'/'+path,'output':output})
                            state['generated']+=1
                        except Exception as exc:
                            state['failed']+=1
                            state['error']='OUTPUT_CONFLICT' if isinstance(exc,SafetyError) else 'FILE_GENERATION_FAILED'
                        save_progress()
            for row in self.storage.virtual_rows():
                if self._stop.is_set(): raise InterruptedError()
                state['found']+=1
                try:
                    output=self.write_output(row['file_id'],row['relative_path'],row['token'],state)
                    outputs[self.config.playback_url(row['token'])+'\n']=(row['file_id'],output,row['token'])
                    state['generated']+=1
                except Exception as exc:
                    state['failed']+=1
                    state['error']='OUTPUT_CONFLICT' if isinstance(exc,SafetyError) else 'FILE_GENERATION_FAILED'
                save_progress()
            state['state']='DONE' if not state['failed'] else 'PARTIAL'
            if state['state']=='DONE':
                self.storage.publish_inventory(inventory)
                # Earlier versions updated strm_path but left their old output
                # behind. Only exact stable-URL duplicates of files successfully
                # generated this round, with the same filename, are candidates.
                current_outputs={Path(output) for _,output,_ in outputs.values()}
                for old in self.strm.root.rglob('*.strm'):
                    if old in current_outputs: continue
                    if old.is_symlink() or not old.resolve().is_relative_to(self.strm.root):
                        continue
                    try: match=outputs.get(old.read_text('utf-8'))
                    except (OSError,UnicodeError): continue
                    if not match: continue
                    fid,new,token=match
                    if old==Path(new) or old.name!=Path(new).name: continue
                    if self.relocate(fid,str(old),new,token)=='DONE': state['relocated']+=1
        except InterruptedError: state['state']='INTERRUPTED'
        except Exception: state['state']='FAILED'; state['error']='SCAN_FAILED'
        self._save_state(state)

    def relocate(self,fid,old,new,token):
        existing=self.query('SELECT state FROM strm_relocations WHERE file_id=? AND old_path=?',(fid,old),one=True)
        if existing:
            return None  # PENDING after interruption and UNKNOWN are never replayed.
        # Commit the intent before touching an older output; store no credentials.
        self.query('INSERT INTO strm_relocations VALUES(?,?,?,?)',(fid,old,new,'PENDING'))
        try:
            state=self.strm.remove_owned(old,new,token)
        except Exception:
            state='UNKNOWN'
        self.query('UPDATE strm_relocations SET state=? WHERE file_id=? AND old_path=?',(state,fid,old))
        return state

    def write_output(self, fid, destination, token, state=None):
        """Use the same ownership/checkpoint rules for actual and virtual STRMs."""
        previous=self.query('SELECT strm_path,relative_path FROM strm_files WHERE file_id=?',(fid,),one=True)
        target=self.strm.root.joinpath(*safe_parts(destination+'.strm'))
        proof=self.query('SELECT * FROM strm_output_proofs WHERE path=?',(str(target),),one=True)
        digest=proof['digest'] if proof and proof['file_id']==fid else None
        if proof is None and previous and previous['strm_path']:
            same_recorded=Path(previous['strm_path']).resolve()==target.resolve()
            same_relative=False
            if not same_recorded and previous['relative_path']:
                try:
                    same_relative=self.strm.root.joinpath(*safe_parts(previous['relative_path']+'.strm')).resolve()==target.resolve()
                except SafetyError:
                    same_relative=False
            if same_recorded or same_relative:
                digest=self.legacy_loopback_digest(target,token)
        checkpoint=[]
        def before_replace():
            if self.query("SELECT id FROM strm_address_updates WHERE path=? AND state IN ('PENDING','UNKNOWN')",(str(target),),one=True):
                raise SafetyError('Address update outcome unresolved')
            self.query('INSERT INTO strm_address_updates(path,old_digest,new_digest,state) VALUES(?,?,?,?)',
                (str(target),digest,hashlib.sha256((self.config.playback_url(token)+'\n').encode()).hexdigest(),'PENDING'))
            checkpoint.append(self.query('SELECT max(id) id FROM strm_address_updates',one=True)['id'])
        try: output=self.strm.generate(destination,token,expected_digest=digest,before_replace=before_replace)
        except Exception:
            if checkpoint: self.query("UPDATE strm_address_updates SET state='UNKNOWN' WHERE id=?",(checkpoint[0],))
            raise
        content=Path(output).read_bytes()
        if content.decode('utf-8').replace('\r\n','\n')!=self.config.playback_url(token)+'\n': raise SafetyError('Output changed')
        self.query('INSERT INTO strm_output_proofs VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET file_id=excluded.file_id,digest=excluded.digest WHERE strm_output_proofs.file_id!=excluded.file_id OR strm_output_proofs.digest!=excluded.digest',
            (output,fid,hashlib.sha256(content).hexdigest()))
        if checkpoint: self.query("UPDATE strm_address_updates SET state='DONE' WHERE id=?",(checkpoint[0],))
        if previous and previous['strm_path'] and previous['strm_path']!=output:
            if self.relocate(fid,previous['strm_path'],output,token)=='DONE' and state is not None:
                state['relocated']+=1
        self.query('UPDATE strm_files SET strm_path=? WHERE file_id=? AND (strm_path IS NULL OR strm_path!=?)',(output,fid,output))
        return output

    def legacy_loopback_digest(self,target,token):
        # Compatibility repair for old plugin output created before strm_output_proofs:
        # require no existing proof, recorded strm_path, exact playback prefix and exact random token.
        if target.is_symlink() or not target.resolve().is_relative_to(self.strm.root) or not target.is_file():
            return None
        content=target.read_bytes()
        try:
            value=content.decode('utf-8').replace('\r\n','\n')
            parsed=urlsplit(value.rstrip('\n'))
            if (value!=parsed.geturl()+'\n' or parsed.scheme not in ('http','https')
                    or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path!=self.config.playback_prefix+'/play/'+token or parsed.query or parsed.fragment):
                return None
        except (ValueError,UnicodeError):
            return None
        return hashlib.sha256(content).hexdigest()

    def register(self,file,path):
        with self._lock:
            row=self.query('SELECT * FROM strm_files WHERE file_id=?',(file.file_id,),one=True)
            if row and (row['size']!=file.size or (row['sha1'] and file.sha1 and row['sha1']!=file.sha1)):
                raise SafetyError('Source identity changed')
            token=row['token'] if row else secrets.token_urlsafe(32)
            if row and (row['pickcode'],row['name'],row['parent_id'],row['relative_path'])==(file.pickcode,file.name,file.parent_id,path) and (not file.sha1 or row['sha1']==file.sha1):
                return token
            self.query('''INSERT INTO strm_files VALUES(?,?,?,?,?,?,?,?,NULL)
                ON CONFLICT(file_id) DO UPDATE SET pickcode=excluded.pickcode,name=excluded.name,
                sha1=CASE WHEN excluded.sha1='' THEN strm_files.sha1 ELSE excluded.sha1 END,parent_id=excluded.parent_id,relative_path=excluded.relative_path''',
                (file.file_id,token,file.pickcode,file.name,file.size,file.sha1,file.parent_id,path))
            return token

    def play(self,token,ua):
        with self._playback_idle:
            if self._closed: raise ToolError('Service stopped')
            row=self.query(
                'SELECT f.file_id,f.pickcode,s.kind,s.stage,'
                'EXISTS(SELECT 1 FROM virtual_selections WHERE stable_id=f.file_id) AS selected '
                'FROM strm_files f LEFT JOIN resource_storage s ON s.media_id=f.file_id WHERE f.token=?',
                (token,),one=True)
            if not row: raise MissingFile('Playback mapping unavailable')
            self._active_playbacks += 1
        try:
            return self.storage.play(row['file_id'], ua, target=row)
        finally:
            with self._playback_idle:
                self._active_playbacks -= 1
                if not self._active_playbacks:
                    self._playback_idle.notify_all()

    def close(self):
        with self._playback_idle:
            self._closed=True; self._stop.set(); worker=self._worker
            self._auto_revision+=1; self._rerun=False
            if self._auto_timer: self._auto_timer.cancel(); self._auto_timer=None
            self._playback_idle.wait_for(lambda: not self._active_playbacks)
        from .iso_transfer import unregister
        unregister(self)
        if worker and worker is not threading.current_thread(): worker.join()
        self.telegram.close()
        self.storage.close()
        with self._lock:
            if self._db is not None: self._db.close(); self._db=None
            self.client.close(); self._file_lock.close()
