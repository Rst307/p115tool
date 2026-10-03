"""Two storage types with private share mappings and bounded temporary copies.

All remote writes are checkpointed before submission. Unconfirmed receives may
recover in an isolated directory after reads; old intents remain archived.
No legacy share/cache/delete tasks are resumed.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from urllib.parse import urlsplit, parse_qs
from .models import RemoteFile, MissingFile, RemoteError, SafetyError, ToolError, ShareRejected
from .strm import classify, safe_parts

UA = 'p115tool-storage-validation'
PLAYBACK_GUARD = 24 * 3600
RECEIVE_RECOVERY_COOLDOWN = 30


def parse_share(link, password):
    if not isinstance(link,str) or not isinstance(password,str) or len(link)>2048:
        raise ValueError('Invalid share input')
    parsed = urlsplit(link.strip())
    if (parsed.scheme not in ('http','https') or parsed.hostname not in ('115.com','www.115.com','115cdn.com')
            or parsed.username or parsed.password or parsed.port not in (None,80,443) or parsed.fragment
            or any(ord(c)<33 for c in link.strip())):
        raise ValueError('Invalid 115 share URL')
    match = re.fullmatch(r'/s/([A-Za-z0-9]{6,64})/?',parsed.path)
    query = parse_qs(parsed.query,keep_blank_values=True)
    if not match or set(query)-{'password'} or len(query.get('password',[]))>1:
        raise ValueError('Invalid 115 share URL')
    embedded = query.get('password',[''])[0]
    password = password.strip()
    if embedded and password and embedded!=password: raise ValueError('Conflicting extraction codes')
    password = password or embedded
    if password and not re.fullmatch(r'[A-Za-z0-9]{4}',password): raise ValueError('Invalid extraction code')
    return match[1],password


def matches(file, row, parent=None):
    return (not file.is_dir and file.name==row['name'] and file.size==row['size']
            and bool(row['sha1']) and file.sha1==row['sha1']
            and (parent is None or file.parent_id==parent))


class StorageManager:
    def __init__(self, service):
        self.service=service; self.client=service.client; self.config=service.config
        self._gate=threading.RLock(); self._thread=None
        self._live_job=None
        self._pending_action=None; self._refresh_pending=False
        self._refresh_timer=None; self._refresh_revision=0
        with closing(sqlite3.connect(service.path)) as db,db:
            db.executescript('''CREATE TABLE IF NOT EXISTS resource_storage (
                media_id TEXT PRIMARY KEY,kind TEXT NOT NULL,stage TEXT NOT NULL,
                share_code TEXT NOT NULL DEFAULT '',password TEXT NOT NULL DEFAULT '',
                share_fid TEXT NOT NULL DEFAULT '',own INTEGER NOT NULL DEFAULT 0,
                source_deleted INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS resource_copies (
                media_id TEXT PRIMARY KEY,root_cid TEXT NOT NULL,folder_name TEXT NOT NULL,
                folder_cid TEXT NOT NULL DEFAULT '',file_id TEXT NOT NULL DEFAULT '',
                stage TEXT NOT NULL,received_at REAL NOT NULL DEFAULT 0,
                lease_until REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS resource_job (id INTEGER PRIMARY KEY CHECK(id=1),value TEXT NOT NULL);''')
            db.execute('CREATE TABLE IF NOT EXISTS recycle_purge_intents (id TEXT PRIMARY KEY,file_id TEXT NOT NULL,rid TEXT NOT NULL DEFAULT \'\',stage TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS abandoned_resource_copies (
                id TEXT PRIMARY KEY,media_id TEXT NOT NULL,root_cid TEXT NOT NULL,
                folder_name TEXT NOT NULL,folder_cid TEXT NOT NULL,file_id TEXT NOT NULL,
                stage TEXT NOT NULL,received_at REAL NOT NULL,lease_until REAL NOT NULL,
                abandoned_at REAL NOT NULL)''')
            db.execute("UPDATE recycle_purge_intents SET stage='UNKNOWN' WHERE stage='PURGING'")
            db.executescript('''CREATE TABLE IF NOT EXISTS actual_inventory (
                media_id TEXT PRIMARY KEY,path TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS actual_inventory_state (
                id INTEGER PRIMARY KEY CHECK(id=1),scope TEXT NOT NULL,scanned_at REAL NOT NULL);''')
            db.execute('CREATE TABLE IF NOT EXISTS removed_storage_records (media_id TEXT PRIMARY KEY,removed_at REAL NOT NULL)')
            # Traversal checks these for every entry. Index both sides of the
            # directory OR so large copy/checkpoint histories stay inexpensive.
            for table, columns in (
                    ('resource_copies', ('file_id','root_cid','folder_cid')),
                    ('abandoned_resource_copies', ('root_cid','folder_cid'))):
                for column in columns:
                    db.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_{column} ON {table}({column})')
            for before,after in [('SHARE_CREATING','SHARE_CREATE_UNKNOWN'),('SOURCE_DELETING','SOURCE_DELETE_UNKNOWN')]:
                db.execute('UPDATE resource_storage SET stage=? WHERE stage=?',(after,before))
            for before,after in [('FOLDER_CREATING','FOLDER_UNKNOWN'),('RECEIVING','RECEIVE_UNKNOWN'),('DELETING','DELETE_UNKNOWN')]:
                db.execute('UPDATE resource_copies SET stage=? WHERE stage=?',(after,before))
        job=self.job()
        if job['state']=='RUNNING': job['state']='INTERRUPTED'; self._job(job)

    @property
    def busy(self): return bool(self._thread and self._thread.is_alive())

    def job(self):
        if self._live_job is not None:
            return {**self._live_job,'results':list(self._live_job.get('results',[]))}
        row=self.service.query('SELECT value FROM resource_job WHERE id=1',one=True)
        return json.loads(row['value']) if row else {'state':'IDLE','action':'','done':0,'failed':0,'error':None}

    def _job(self, value):
        self.service.query('INSERT INTO resource_job VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',
                           (json.dumps(value),))
        self._live_job=None

    def _progress(self, value):
        # Live phases do not need a disk fsync: remote-write checkpoints and
        # completed-file counts remain independently durable before continuing.
        self._live_job={**value,'results':list(value.get('results',[]))}

    def row(self, media_id):
        return self.service.query('SELECT f.*,s.kind,s.stage,s.share_code,s.password,s.share_fid,s.own,s.source_deleted '
            'FROM strm_files f JOIN resource_storage s ON s.media_id=f.file_id WHERE f.file_id=?',(media_id,),one=True)

    def copy(self, media_id):
        return self.service.query('SELECT * FROM resource_copies WHERE media_id=?',(media_id,),one=True)

    def listing(self, page=1, kind='ALL', search=''):
        if type(page) is not int or not 1<=page<=100000 or kind not in ('ALL','ACTUAL','VIRTUAL') or not isinstance(search,str) or len(search)>100:
            raise ValueError('Invalid storage filter')
        snapshot=self.service.query('SELECT * FROM actual_inventory_state WHERE id=1',one=True)
        current=bool(snapshot and snapshot['scope']==self.inventory_scope())
        path="CASE WHEN s.kind='VIRTUAL' THEN f.relative_path ELSE a.path END"
        where="WHERE NOT EXISTS (SELECT 1 FROM removed_storage_records r WHERE r.media_id=f.file_id) AND (s.kind='VIRTUAL' OR (? AND a.media_id IS NOT NULL)) AND (?='ALL' OR coalesce(s.kind,'ACTUAL')=?) AND instr(lower("+path+"),lower(?))>0"
        args=(current,kind,kind,search)
        joins=' FROM strm_files f LEFT JOIN resource_storage s ON s.media_id=f.file_id LEFT JOIN actual_inventory a ON a.media_id=f.file_id '
        total=self.service.query('SELECT count(*) n'+joins+where,args,one=True)['n']
        items=self.service.query("SELECT f.file_id id,f.name,"+path+" path,coalesce(s.kind,'ACTUAL') kind,"
            "coalesce(s.stage,'READY') stage,c.stage copy_state,c.received_at,c.lease_until FROM strm_files f "
            'LEFT JOIN resource_storage s ON s.media_id=f.file_id LEFT JOIN actual_inventory a ON a.media_id=f.file_id LEFT JOIN resource_copies c ON c.media_id=f.file_id '
            +where+' ORDER BY f.relative_path,f.file_id LIMIT 50 OFFSET ?',(*args,(page-1)*50))
        for item in items:
            item['expires_at']=(item.pop('received_at') or 0)+self.config.temp_days*86400 if item['copy_state']=='READY' else None
        return {'items':items,'total':total,'page':page,'job':self.job(),'busy':self.busy,
                'temp_days':self.config.temp_days,'cleanup_enabled':self.config.temp_cleanup,
                'recycle_purge_enabled':self.config.recycle_purge,
                'recycle_key_configured':bool(self.config.recycle_password),
                'recycle_purge_blocked':bool(self.service.query("SELECT id FROM recycle_purge_intents WHERE stage!='DONE' LIMIT 1",one=True)),
                'temp_configured':bool(self.config.temp_cid),'sources_configured':bool(self.config.source_cids),
                'scanned_at':snapshot['scanned_at'] if current else None,
                'queued':self._pending_action is not None}

    def delete_records(self, ids):
        # Remove list membership only. Playback mappings, copy ownership and all
        # uncertain remote-write checkpoints must remain available after restart.
        with self.service._lock:
            if self.service._closed: raise ToolError('Service stopped')
            if self.busy or self._pending_action is not None or (self.service._worker and self.service._worker.is_alive()):
                return {'state':'BUSY'}
            ids=self._selection(ids)
            with closing(sqlite3.connect(self.service.path)) as db,db:
                db.executemany('INSERT OR IGNORE INTO removed_storage_records VALUES(?,?)',
                               [(media_id,time.time()) for media_id in ids])
            return {'state':'RECORDS_DELETED','count':len(ids)}

    def request_refresh(self, after_transfer=False):
        with self.service._lock:
            if self.service._closed or not self.config.source_cids: return
            if after_transfer:
                self._refresh_revision+=1
                if self._refresh_timer: self._refresh_timer.cancel()
                self._refresh_timer=threading.Timer(10,self._refresh_after_transfer,args=(self._refresh_revision,))
                self._refresh_timer.daemon=True; self._refresh_timer.start()
                return
            if self.busy and self.job()['action']=='scan': return
            self._refresh_pending=True
            self.drain_pending()

    def _refresh_after_transfer(self, revision):
        with self.service._lock:
            if self.service._closed or revision!=self._refresh_revision: return
            self._refresh_timer=None
            self.request_refresh()

    def drain_pending(self):
        # Called under the service lock, only when the previous worker has yielded.
        if self.service._closed or self.busy or (self.service._worker and self.service._worker.is_alive()): return
        if self._pending_action:
            action,payload=self._pending_action; self._pending_action=None
            self.start(action,payload)
        elif self._refresh_pending:
            self._refresh_pending=False; self.start('scan')

    def inventory_scope(self):
        return json.dumps([sorted(s['cid'] for s in self.config.source_cids),self.config.temp_cid])

    def inventory_recent(self):
        snapshot=self.service.query('SELECT * FROM actual_inventory_state WHERE id=1',one=True)
        return bool(snapshot and snapshot['scope']==self.inventory_scope()
                    and 0<=time.time()-snapshot['scanned_at']<1800)

    def scan_actual(self, job):
        # Enumerate everything before replacing the last successful snapshot.
        files=[]; seen_dirs=set(); seen_files=set()
        for source in self.config.source_cids:
            if self.skip_directory(source['cid']): continue
            prefix=self.client.directory_path(source['cid'])
            stack=[(source['cid'],'')]
            while stack:
                cid,relative=stack.pop()
                if self.service._stop.is_set(): raise InterruptedError()
                if cid in seen_dirs: continue
                if len(seen_dirs)>=100000: raise SafetyError('Traversal limit')
                seen_dirs.add(cid)
                for file in self.client.list_files(cid):
                    if self.service._stop.is_set(): raise InterruptedError()
                    if file.parent_id!=cid: raise SafetyError('Unexpected parent')
                    safe_parts(file.name)
                    if '/' in file.name: raise SafetyError('Unexpected filename')
                    relative_path='/'.join(filter(None,[relative,file.name]))
                    if file.is_dir:
                        if not self.skip_directory(file.file_id): stack.append((file.file_id,relative_path))
                        continue
                    if self.skip_file(file.file_id) or file.file_id in seen_files or Path(file.name).suffix.lower() not in self.config.media_extensions: continue
                    seen_files.add(file.file_id)
                    files.append((file,prefix.rstrip('/')+'/'+relative_path))
                job['scanned']=len(seen_dirs);job['found']=len(files)
                if 'action' in job: self._progress(job)
        # Preserve existing playback identities and STRM paths, including checkpoints.
        job['phase']='REGISTERING';job['total']=len(files);job['registered']=0
        if 'action' in job: self._progress(job)
        for file,path in files:
            job['current']=file.file_id
            if 'action' in job: self._progress(job)
            if self.service._stop.is_set(): raise InterruptedError()
            previous=self.service.query('SELECT relative_path FROM strm_files WHERE file_id=?',(file.file_id,),one=True)
            destination=previous['relative_path'] if previous else classify('/',path.lstrip('/'),
                **({'recognizer':self.service.recognizer} if self.service.recognizer else {}))
            self.service.register(file,destination)
            job['registered']+=1
            if 'action' in job: self._progress(job)
        self.publish_inventory([(f.file_id,p) for f,p in files])
        job['done']=len(files)

    def publish_inventory(self, files):
        with self.service._lock:
            if self.service._stop.is_set(): raise InterruptedError()
            with closing(sqlite3.connect(self.service.path)) as db,db:
                db.execute('DELETE FROM actual_inventory')
                db.executemany('INSERT INTO actual_inventory VALUES(?,?)',files)
                db.execute('INSERT INTO actual_inventory_state VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET scope=excluded.scope,scanned_at=excluded.scanned_at',
                    (self.inventory_scope(),time.time()))

    def _selection(self, ids):
        if (not isinstance(ids,list) or not ids or len(ids)>100
                or any(not isinstance(i,str) or len(i)>128 for i in ids) or len(set(ids))!=len(ids)):
            raise ValueError('Invalid media selection')
        if any(not self.service.query('SELECT file_id FROM strm_files WHERE file_id=?',(i,),one=True) for i in ids):
            raise ValueError('Unknown media')
        return ids

    def start(self, action, payload=None):
        payload=payload or {}
        if action=='import':
            if set(payload)!={'link','password'}: raise ValueError('Invalid import')
            args=parse_share(payload['link'],payload['password'])
        elif action=='virtualize':
            if (set(payload) not in ({'ids','delete_source'},{'ids','delete_source','purge_source'})
                    or payload.get('delete_source') is not True or type(payload.get('purge_source',False)) is not bool):
                raise ValueError('Explicit source deletion authorization required')
            args=(self._selection(payload['ids']),self.config.recycle_purge or payload.get('purge_source',False))
        elif action=='virtualize_all':
            if (set(payload) not in ({'delete_source'},{'delete_source','purge_source'})
                    or payload.get('delete_source') is not True or type(payload.get('purge_source',False)) is not bool):
                raise ValueError('Explicit source deletion authorization required')
            if not self.config.source_cids: return {'state':'SOURCES_REQUIRED'}
            args=([],self.config.recycle_purge or payload.get('purge_source',False))
        elif action=='reconcile':
            if set(payload)!={'ids'}: raise ValueError('Invalid reconciliation')
            args=(self._selection(payload['ids']),)
        elif action=='scan':
            if payload: raise ValueError('Invalid scan')
            if not self.config.source_cids: return {'state':'SOURCES_REQUIRED'}
            args=()
        elif action=='cleanup':
            if payload: raise ValueError('Invalid cleanup')
            args=()
        else: raise ValueError('Unknown storage action')
        if action in ('virtualize','virtualize_all') and args[1] and not self.config.recycle_password:
            return {'state':'RECYCLE_KEY_REQUIRED'}
        with self.service._lock:
            if self.service._closed: raise ToolError('Service stopped')
            if self.busy:
                if self.job()['action']=='scan' and action!='scan' and self._pending_action is None:
                    # Keep one explicit submission in memory, never replay on restart.
                    self._pending_action=(action,{**payload,**({'ids':list(payload['ids'])} if 'ids' in payload else {})})
                    return {'state':'QUEUED'}
                return {'state':'BUSY'}
            if self.service._worker and self.service._worker.is_alive(): return {'state':'BUSY'}
            self._job({'state':'RUNNING','action':action,'done':0,'failed':0,'error':None,
                       'phase':'SCANNING' if action in ('scan','virtualize_all') else 'STARTING','current':None})
            self._thread=threading.Thread(target=self._run,args=(action,args),name='p115tool-storage',daemon=True)
            try: self._thread.start()
            except Exception:
                self._job({'state':'FAILED','action':action,'done':0,'failed':1,'error':'START_FAILED'})
                self._thread=None
                raise ToolError('Storage task did not start') from None
            return {'state':'RUNNING'}

    def _run(self, action, args):
        job={'state':'RUNNING','action':action,'done':0,'failed':0,'error':None,'results':[],
             'phase':'SCANNING' if action in ('scan','virtualize_all') else 'STARTING','current':None}
        try:
            if action=='import': self.import_share(*args,job=job)
            elif action=='scan': self.scan_actual(job)
            elif action=='cleanup': self.cleanup(job)
            else:
                if action=='virtualize_all':
                    # Reuse a recent complete snapshot (also the scan just awaited
                    # by a queued request). Each source is still stat-verified
                    # immediately before sharing or deleting.
                    if not self.inventory_recent(): self.scan_actual(job)
                    args=([r['media_id'] for r in self.service.query(
                        "SELECT a.media_id FROM actual_inventory a LEFT JOIN resource_storage s "
                        "ON s.media_id=a.media_id WHERE coalesce(s.kind,'ACTUAL')='ACTUAL' "
                        "AND NOT EXISTS (SELECT 1 FROM removed_storage_records r WHERE r.media_id=a.media_id) ORDER BY a.media_id")],args[1])
                    job['done']=0
                job['total']=len(args[0])
                self._job(job)
                for media_id in args[0]:
                    if self.service._stop.is_set(): raise InterruptedError()
                    job['current']=media_id;job['phase']='CHECKING';self._progress(job)
                    def progress(phase):
                        job['phase']=phase;self._progress(job)
                    try:
                        if action in ('virtualize','virtualize_all'):
                            self.virtualize(media_id,purge_source=args[1],progress=progress)
                            row=self.row(media_id)
                            if row and row['stage']!='READY': raise SafetyError('Resource needs attention')
                            if args[1] and self.service.query("SELECT id FROM recycle_purge_intents WHERE file_id=? AND stage!='DONE' LIMIT 1",(media_id,),one=True):
                                raise SafetyError('Recycle purge needs attention')
                        else: self.reconcile(media_id)
                        job['done']+=1
                        job['results']=(job['results']+[{'id':media_id,'state':'DONE'}])[-50:]
                    except InterruptedError: raise
                    except Exception as exc:
                        job['failed']+=1;job['error']='RESOURCE_BLOCKED'
                        row=self.row(media_id)
                        stage=row['stage'] if row else 'RESOURCE_BLOCKED'
                        if stage in ('SHARE_REJECTED','SHARE_CHECK','SHARE_UNAVAILABLE','SHARE_CREATE_UNKNOWN','SOURCE_DELETE_UNKNOWN'):
                            job['error']=stage
                        if job['phase']=='RECYCLE_SNAPSHOT': job['error']='RECYCLE_PRECHECK_FAILED'
                        if (self.config.recycle_purge or action in ('virtualize','virtualize_all') and args[1]) and self.service.query("SELECT id FROM recycle_purge_intents WHERE stage!='DONE' LIMIT 1",one=True):
                            job['error']='RECYCLE_PURGE_BLOCKED'
                        code=getattr(exc,'upstream_code',None)
                        job['results']=(job['results']+[{'id':media_id,'state':'FAILED','error':job['error'],
                            'upstream_code':code if type(code) is int else None}])[-50:]
                        self._job(job)
                        if job['error']=='RECYCLE_PURGE_BLOCKED': break
                        if action in ('virtualize','virtualize_all') and row and row['stage'] in ('SHARE_CREATE_UNKNOWN','SOURCE_DELETE_UNKNOWN','SHARE_UNAVAILABLE'):
                            self._job(job)
                            break
                    self._job(job)
            job['state']='DONE' if not job['failed'] else 'PARTIAL'
        except InterruptedError: job['state']='INTERRUPTED'
        except Exception: job['state']='FAILED';job['error']='STORAGE_FAILED';job['failed']+=1
        finally:
            job['current']=None;job['phase']='FINISHED'
            if 'total' in job: job['remaining']=max(0,job['total']-job['done']-job['failed'])
            self._job(job)
            with self.service._lock:
                self._thread=None
                if self.service._rerun and not self.service._closed:
                    self.service._rerun=False
                    try: self.service.start()
                    except Exception: pass
                self.drain_pending()

    def share_tree(self, code, password):
        stack=[('0','')]; seen=set(); files=[]
        while stack:
            cid,path=stack.pop()
            if self.service._stop.is_set(): raise InterruptedError()
            if cid in seen or len(seen)>=10000: raise SafetyError('Share traversal limit')
            seen.add(cid)
            for file in self.client.share_files(code,password,cid):
                relative='/'.join(filter(None,(path,file.name)))
                if file.is_dir: stack.append((file.file_id,relative))
                elif Path(file.name).suffix.lower() in self.config.media_extensions:
                    if not re.fullmatch(r'[A-F0-9]{40}',file.sha1) or file.size<=0:
                        raise SafetyError('Share media identity unavailable')
                    files.append((file,relative))
                    if len(files)>1000: raise SafetyError('Share media limit')
        if not files: raise SafetyError('Share contains no media')
        return files

    def import_share(self, code, password, job):
        # Complete enumeration before adding any media or sending copy requests.
        files=self.share_tree(code,password)
        digest=hashlib.sha256(code.encode()).hexdigest()[:24]
        for file,relative in files:
            if self.service._stop.is_set(): raise InterruptedError()
            media_id='v_'+digest+'_'+file.file_id
            try:
                existing=self.row(media_id)
                if existing and (existing['share_fid']!=file.file_id or existing['share_code']!=code):
                    raise SafetyError('Share mapping changed')
                destination=self.service.virtual_organizer(relative)
                safe_parts(destination)
                if self.service._stop.is_set(): raise InterruptedError()
                record=RemoteFile(media_id,file.name,file.size,file.sha1,'0','',is_dir=False)
                self.service.register(record,destination)
                self.service.query("INSERT INTO resource_storage(media_id,kind,stage,share_code,password,share_fid) "
                    "VALUES(?,'VIRTUAL','READY',?,?,?) ON CONFLICT(media_id) DO UPDATE SET password=excluded.password",
                    (media_id,code,password,file.file_id))
                row=self.row(media_id)
                self.service.write_output(media_id,row['relative_path'],row['token'])
                job['done']+=1
            except InterruptedError: raise
            except Exception:
                job['failed']+=1;job['error']='VIRTUAL_ORGANIZE_FAILED'
            self._job(job)

    def verify_share(self, row):
        candidates=[file for file,_ in self.share_tree(row['share_code'],row['password']) if matches(file,row)]
        if len(candidates)!=1: raise SafetyError('Share identity is ambiguous')
        link=self.client.share_link(row['share_code'],row['password'],candidates[0].file_id,UA)
        self.client.probe_range(link,UA,row['size'])
        return candidates[0].file_id

    def wait_share(self, row, progress):
        # Retry only read validation, never creation, deletion or identity errors.
        for attempt in range(3):
            if self.service._stop.is_set(): raise InterruptedError()
            try: return self.verify_share(row)
            except Exception as exc:
                pending=isinstance(exc,RemoteError) or (type(exc) is SafetyError and exc.args==('Share contains no media',))
                if not pending or attempt==2: raise
                progress('SHARE_WAITING')
                if self.service._stop.wait(attempt+1): raise InterruptedError()
                progress('SHARE_CHECK')

    def virtualize(self, media_id, purge_source=False, progress=None):
        progress=progress or (lambda phase:None)
        purge_source=self.config.recycle_purge or purge_source
        with self._gate:
            if self.service._stop.is_set(): raise InterruptedError()
            if purge_source and not re.fullmatch(r'[0-9]{6}',self.config.recycle_password):
                raise SafetyError('Recycle security key required')
            row=self.row(media_id)
            if row and row['kind']=='VIRTUAL': return
            if purge_source and self.service.query("SELECT id FROM recycle_purge_intents WHERE stage!='DONE' LIMIT 1",one=True):
                raise SafetyError('Recycle purge needs attention')
            if row and row['stage'] in ('SHARE_CREATE_UNKNOWN','SOURCE_DELETE_UNKNOWN','SHARE_CREATING','SOURCE_DELETING'):
                raise SafetyError('Unresolved write checkpoint')
            source=self.service.query('SELECT * FROM strm_files WHERE file_id=?',(media_id,),one=True)
            if not source or not re.fullmatch(r'[1-9][0-9]{0,19}',media_id): raise SafetyError('Invalid actual source')
            actual=self.client.stat(media_id)
            if not matches(actual,source,source['parent_id']): raise SafetyError('Source identity changed')
            # Ensure a readable, owned STRM exists before considering source removal.
            self.service.write_output(media_id,source['relative_path'],source['token'])
            if not row or row['stage']=='SHARE_REJECTED':
                self.service.query("INSERT INTO resource_storage(media_id,kind,stage,own) VALUES(?,'ACTUAL','SHARE_CREATING',1) "
                    "ON CONFLICT(media_id) DO UPDATE SET stage='SHARE_CREATING'",(media_id,))
                progress('SHARE_CREATING')
                try: code,password=self.client.create_share(media_id)
                except ShareRejected:
                    self.service.query("UPDATE resource_storage SET stage='SHARE_REJECTED' WHERE media_id=?",(media_id,))
                    raise
                except Exception:
                    self.service.query("UPDATE resource_storage SET stage='SHARE_CREATE_UNKNOWN' WHERE media_id=?",(media_id,))
                    raise ToolError('Share creation outcome unknown') from None
                self.service.query("UPDATE resource_storage SET share_code=?,password=?,stage='SHARE_CHECK' WHERE media_id=?",(code,password,media_id))
            row=self.row(media_id)
            progress('SHARE_RETAINING')
            self.client.retain_share(row['share_code'])
            progress('SHARE_CHECK')
            fid=self.wait_share(row,progress)
            self.service.query('UPDATE resource_storage SET share_fid=? WHERE media_id=?',(fid,media_id))
            # Conversion stores only the verified share mapping and STRM.
            # Playback creates its temporary copy lazily when needed.
            row=self.row(media_id)
            actual=self.client.stat(media_id)
            if not matches(actual,row,row['parent_id']): raise SafetyError('Source changed before deletion')
            if self.service._stop.is_set(): raise InterruptedError()
            progress('RECYCLE_SNAPSHOT' if purge_source else 'CHECKING')
            purge=self.prepare_purge(actual,enabled=purge_source)
            if purge is not None:
                try:
                    current=self.client.stat(media_id)
                    if not matches(current,row,row['parent_id']): raise SafetyError('Source changed before deletion')
                    if self.service._stop.is_set(): raise InterruptedError()
                except Exception:
                    # No deletion was submitted; this snapshot is safely cancelled.
                    self.service.query("UPDATE recycle_purge_intents SET stage='DONE' WHERE id=? AND stage='PREPARED'",(purge[0],))
                    raise
            self.service.query("UPDATE resource_storage SET stage='SOURCE_DELETING' WHERE media_id=?",(media_id,))
            progress('SOURCE_DELETING')
            try:
                self.client.delete_verified_file(media_id)
                self.confirm_absent(media_id)
            except Exception:
                self.service.query("UPDATE resource_storage SET stage='SOURCE_DELETE_UNKNOWN' WHERE media_id=?",(media_id,))
                raise ToolError('Source deletion outcome unknown') from None
            self.service.query("UPDATE resource_storage SET kind='VIRTUAL',source_deleted=1,stage='READY' WHERE media_id=?",(media_id,))
            progress('POST_DELETE_CHECK')
            try: self.wait_share(self.row(media_id),progress)
            except InterruptedError: raise
            except Exception:
                self.service.query("UPDATE resource_storage SET stage='SHARE_UNAVAILABLE' WHERE media_id=?",(media_id,))
                raise ToolError('Share needs attention; batch stopped') from None
            progress('RECYCLE_PURGING' if purge is not None else 'POST_DELETE_CHECK')
            self.finish_purge(actual,purge)
            if purge is not None:
                progress('POST_PURGE_CHECK')
                try: self.wait_share(self.row(media_id),progress)
                except InterruptedError: raise
                except Exception:
                    self.service.query("UPDATE resource_storage SET stage='SHARE_UNAVAILABLE' WHERE media_id=?",(media_id,))
                    raise ToolError('Share needs attention; batch stopped') from None

    def prepare_purge(self, file, enabled=None):
        if not (self.config.recycle_purge if enabled is None else enabled): return None
        if self.service.query("SELECT id FROM recycle_purge_intents WHERE stage!='DONE' LIMIT 1",one=True):
            raise SafetyError('Recycle purge needs attention')
        entries=self.client.recycle_entries()
        intent=uuid.uuid4().hex
        self.service.query("INSERT INTO recycle_purge_intents(id,file_id,stage) VALUES(?,?,'PREPARED')",(intent,file.file_id))
        return intent,{row['rid'] for row in entries}

    def finish_purge(self, file, prepared):
        if prepared is None: return
        intent,before=prepared
        try:
            for attempt in range(3):
                if self.service._stop.is_set(): raise InterruptedError()
                entries=self.client.recycle_entries()
                candidates=[]
                for entry in entries:
                    # Explicit original file ID plus content identity are mandatory.
                    # Unknown metadata layouts are blocked, never guessed by name.
                    fid=str(entry.get('file_id') or entry.get('fid') or '')
                    name=entry.get('file_name',entry.get('name',entry.get('n')))
                    size=entry.get('file_size',entry.get('size',entry.get('s')))
                    sha=entry.get('sha1',entry.get('sha',entry.get('file_sha1','')))
                    if (entry['rid'] not in before and fid==file.file_id and name==file.name
                            and type(size) in (str,int) and str(size)==str(file.size)
                            and isinstance(sha,str) and bool(file.sha1) and sha.upper()==file.sha1):
                        candidates.append(entry['rid'])
                if candidates or attempt==2: break
                if self.service._stop.wait(attempt+1): raise InterruptedError()
            if len(candidates)!=1: raise SafetyError('Recycle file not uniquely verified')
            rid=candidates[0]
            self.confirm_absent(file.file_id)
            if self.service._stop.is_set(): raise InterruptedError()
            self.service.query("UPDATE recycle_purge_intents SET rid=?,stage='PURGING' WHERE id=?",(rid,intent))
            self.client.purge_recycle_entry(rid)
            for attempt in range(3):
                if not any(row['rid']==rid for row in self.client.recycle_entries()): break
                if attempt==2: raise SafetyError('Recycle purge outcome unconfirmed')
                if self.service._stop.wait(attempt+1): raise InterruptedError()
            self.service.query("UPDATE recycle_purge_intents SET stage='DONE' WHERE id=?",(intent,))
        except Exception as exc:
            self.service.query("UPDATE recycle_purge_intents SET stage='UNKNOWN' WHERE id=?",(intent,))
            from .api import log_failure
            log_failure(exc,context='recycle')
            raise ToolError('Recycle purge needs attention; batch stopped') from None

    def validate_folder(self, copy):
        folder=self.client.stat(copy['folder_cid'])
        if not folder.is_dir or folder.parent_id!=copy['root_cid'] or folder.name!=copy['folder_name']:
            raise SafetyError('Temporary directory identity changed')
        return folder

    def confirm_absent(self, file_id):
        # One read after a write; an uncertain result stays checkpointed.
        try: self.client.stat(file_id)
        except MissingFile: return
        raise SafetyError('Deleted file still present')

    def locate_copy(self, row, copy):
        self.validate_folder(copy)
        files=list(self.client.list_files(copy['folder_cid']))
        candidates=[f for f in files if matches(f,row,copy['folder_cid'])]
        if len(files)!=1 or len(candidates)!=1 or not candidates[0].pickcode:
            raise SafetyError('Temporary contents not uniquely verified')
        return candidates[0]

    def create_copy_folder(self, media_id, root, abandoned=None):
        if not root: raise SafetyError('Temporary directory not configured')
        if self.client.directory_path(root)=='/': raise SafetyError('Invalid temporary root')
        name='p115tool-'+uuid.uuid4().hex
        # Commit the new identity before creating; an unknown result is never retried.
        with self.service._lock,closing(sqlite3.connect(self.service.path)) as db,db:
            # Archive and replace atomically: a crash can never discard the old
            # unknown intent or leave it eligible for an automatic resubmission.
            if abandoned is not None:
                db.execute('INSERT INTO abandoned_resource_copies VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex,media_id,abandoned['root_cid'],abandoned['folder_name'],
                     abandoned['folder_cid'],abandoned['file_id'],abandoned['stage'],
                     abandoned['received_at'],abandoned['lease_until'],time.time()))
            db.execute("INSERT INTO resource_copies(media_id,root_cid,folder_name,stage) VALUES(?,?,?,'FOLDER_CREATING') "
                "ON CONFLICT(media_id) DO UPDATE SET root_cid=excluded.root_cid,folder_name=excluded.folder_name,"
                "folder_cid='',file_id='',received_at=0,lease_until=0,stage='FOLDER_CREATING'",(media_id,root,name))
        try: cid=self.client.create_temp_directory(root,name)
        except Exception:
            self.service.query("UPDATE resource_copies SET stage='FOLDER_UNKNOWN' WHERE media_id=?",(media_id,))
            raise ToolError('Temporary directory outcome unknown') from None
        self.service.query("UPDATE resource_copies SET folder_cid=?,stage='EMPTY' WHERE media_id=?",(cid,media_id))
        return self.copy(media_id)

    def ensure_copy(self, media_id, recover_unknown=True):
        with self._gate:
            if self.service._stop.is_set(): raise InterruptedError()
            row=self.row(media_id)
            if not row or not row['share_fid']: raise SafetyError('Share mapping unavailable')
            copy=self.copy(media_id)
            if copy and copy['stage'] in ('RECEIVE_UNKNOWN','RECEIVED'):
                # Prefer a late successful receive. Only a verified empty or
                # missing old directory permits an isolated new attempt.
                try:
                    try: self.validate_folder(copy)
                    except MissingFile: files=[]
                    else: files=list(self.client.list_files(copy['folder_cid']))
                    if not files:
                        if copy['stage']=='RECEIVED' and time.time()-copy['received_at']<RECEIVE_RECOVERY_COOLDOWN:
                            raise SafetyError('Temporary transfer awaiting visibility')
                        if not recover_unknown: raise SafetyError('Temporary write outcome unresolved')
                        recent=self.service.query('SELECT max(abandoned_at) at FROM abandoned_resource_copies WHERE media_id=?',(media_id,),one=True)
                        if recent['at'] is not None and time.time()-recent['at']<RECEIVE_RECOVERY_COOLDOWN:
                            raise SafetyError('Temporary recovery cooling down')
                        if self.verify_share(row)!=row['share_fid']:
                            raise SafetyError('Share mapping changed')
                        # Recheck after share validation, which may take time.
                        try: self.validate_folder(copy)
                        except MissingFile: pass
                        else:
                            if list(self.client.list_files(copy['folder_cid'])):
                                raise SafetyError('Temporary contents changed')
                        if self.service._stop.is_set(): raise InterruptedError()
                        self.create_copy_folder(media_id,copy['root_cid'],abandoned=copy)
                    else:
                        if len(files)!=1:
                            raise SafetyError('Temporary contents not uniquely verified')
                        if not matches(files[0],row,copy['folder_cid']):
                            raise SafetyError('Temporary identity changed')
                        candidate=files[0]
                        actual=self.client.stat(candidate.file_id)
                        if actual.file_id!=candidate.file_id or not matches(actual,row,copy['folder_cid']):
                            raise SafetyError('Temporary identity changed')
                        if not actual.pickcode: raise SafetyError('Temporary playback code unavailable')
                except Exception as exc:
                    if self.service._stop.is_set(): raise InterruptedError() from None
                    if copy['stage']=='RECEIVED':
                        if isinstance(exc,ToolError): exc.copy_state=self.copy(media_id)['stage']
                        raise
                    error=SafetyError('Temporary recovery cooling down' if exc.args==('Temporary recovery cooling down',)
                                      else 'Temporary write outcome unresolved')
                    error.copy_state=self.copy(media_id)['stage']
                    for key in ('operation','sdk_error','upstream_code'):
                        if hasattr(exc,key):setattr(error,key,getattr(exc,key))
                    raise error from None
                if not files: return self.ensure_copy(media_id,recover_unknown=False)
                self.service.query("UPDATE resource_copies SET file_id=?,stage='READY',received_at=? WHERE media_id=?",(actual.file_id,time.time(),media_id))
                return actual
            if copy and copy['stage']=='READY':
                actual=None;read_error=None
                try: actual=self.client.stat(copy['file_id'])
                except MissingFile: pass
                except RemoteError as exc: read_error=exc
                if actual is not None and not matches(actual,row,copy['folder_cid']):
                    raise SafetyError('Temporary identity changed')
                # get_info can still return metadata for permanently deleted
                # files. Verify live membership in the recorded directory too.
                # A failed detail read alone never authorizes another receive.
                try: self.validate_folder(copy)
                except MissingFile:
                    self.create_copy_folder(media_id,copy['root_cid'])
                    return self.ensure_copy(media_id,recover_unknown=recover_unknown)
                contents=list(self.client.list_files(copy['folder_cid']))
                if not contents:
                    self.service.query("UPDATE resource_copies SET stage='EMPTY',file_id='',received_at=0,lease_until=0 WHERE media_id=?",(media_id,))
                    return self.ensure_copy(media_id,recover_unknown=recover_unknown)
                if read_error is not None: raise read_error
                members=[f for f in contents if f.file_id==copy['file_id']]
                if actual is None or len(members)!=1 or not matches(members[0],row,copy['folder_cid']):
                    raise SafetyError('Temporary contents changed')
                return actual
            if copy and copy['stage'] in ('FOLDER_UNKNOWN','RECEIVE_UNKNOWN','DELETE_UNKNOWN','FOLDER_CREATING','RECEIVING','DELETING'):
                error=SafetyError('Temporary write outcome unresolved')
                error.copy_state=copy['stage']
                raise error
            if not copy:
                copy=self.create_copy_folder(media_id,self.config.temp_cid)
            try: self.validate_folder(copy)
            except MissingFile:
                copy=self.create_copy_folder(media_id,copy['root_cid'])
                self.validate_folder(copy)
            if list(self.client.list_files(copy['folder_cid'])): raise SafetyError('Temporary directory not empty')
            # Verify sharing metadata again immediately before receiving.
            members=list(self.client.share_files(row['share_code'],row['password'],'0')) if row['own'] else None
            if members is not None and not any(f.file_id==row['share_fid'] and matches(f,row) for f in members):
                raise SafetyError('Owned share file changed')
            if self.service._stop.is_set(): raise InterruptedError()
            self.service.query("UPDATE resource_copies SET stage='RECEIVING' WHERE media_id=?",(media_id,))
            try: self.client.receive_to_temp(row['share_code'],row['password'],row['share_fid'],copy['folder_cid'])
            except Exception as exc:
                self.service.query("UPDATE resource_copies SET stage='RECEIVE_UNKNOWN' WHERE media_id=?",(media_id,))
                error=ToolError('Temporary transfer outcome unknown')
                error.copy_state='RECEIVE_UNKNOWN'
                # Preserve only diagnostic attributes; log_failure allowlists
                # their values and never emits the SDK payload or exception text.
                for key in ('operation','sdk_error','upstream_code'):
                    if hasattr(exc,key):setattr(error,key,getattr(exc,key))
                raise error from None
            self.service.query("UPDATE resource_copies SET stage='RECEIVED',received_at=? WHERE media_id=?",(time.time(),media_id))
            return self.ensure_copy(media_id,recover_unknown=False)

    def play(self, media_id, ua):
        if self.service._stop.is_set(): raise ToolError('Service stopped')
        row=self.row(media_id)
        if not row:
            source=self.service.query('SELECT * FROM strm_files WHERE file_id=?',(media_id,),one=True)
            return self.client.normal_link(source['pickcode'],ua).url
        with self._gate:
            if self.service._stop.is_set(): raise ToolError('Service stopped')
            row=self.row(media_id)
            if not row or (row['kind']=='ACTUAL' and row['stage'] not in ('SOURCE_DELETE_UNKNOWN','SOURCE_DELETING')):
                source=row or self.service.query('SELECT * FROM strm_files WHERE file_id=?',(media_id,),one=True)
                return self.client.normal_link(source['pickcode'],ua).url
            actual=self.ensure_copy(media_id)
            # Pure 302 cannot observe when Emby finishes reading the CDN stream.
            # A persistent 24h lease is renewed on every GET/HEAD request.
            self.service.query('UPDATE resource_copies SET lease_until=? WHERE media_id=?',(time.time()+PLAYBACK_GUARD,media_id))
            try: return self.client.normal_link(actual.pickcode,ua).url
            except RemoteError as exc:
                # The user may remove the copy after stat but before link lookup.
                # SDK download errors need not use the MissingFile subclass.
                # Recheck live membership; an existing copy is never re-received.
                restored=self.ensure_copy(media_id,recover_unknown=False)
                if restored.file_id==actual.file_id:
                    if isinstance(exc,MissingFile):
                        raise SafetyError('Playback link unavailable for existing copy') from None
                    raise
                self.service.query('UPDATE resource_copies SET lease_until=? WHERE media_id=?',(time.time()+PLAYBACK_GUARD,media_id))
                # One bounded recovery per request, no loop on persistent errors.
                return self.client.normal_link(restored.pickcode,ua).url

    def cleanup(self, job=None):
        job=job or {'done':0,'failed':0,'error':None}
        if self.service.query("SELECT media_id FROM resource_copies WHERE stage IN ('DELETING','DELETE_UNKNOWN') LIMIT 1",one=True):
            job['failed']+=1;job['error']='CLEANUP_UNKNOWN'
            return job
        now=time.time()
        copies=self.service.query("SELECT * FROM resource_copies WHERE stage='READY' AND received_at+?<=? AND lease_until<=?",(self.config.temp_days*86400,now,now))
        for candidate in copies:
            # Yield the gate between resources so playback can renew its lease.
            with self._gate:
                copy=self.copy(candidate['media_id'])
                if (not copy or copy['stage']!='READY' or copy['lease_until']>time.time()
                        or copy['received_at']+self.config.temp_days*86400>time.time()): continue
                if self.service._stop.is_set(): raise InterruptedError()
                try:
                    row=self.row(copy['media_id'])
                    # Never remove the last usable copy while sharing is broken.
                    self.verify_share(row)
                    actual=self.locate_copy(row,copy)
                    if actual.file_id!=copy['file_id']: raise SafetyError('Temporary file replaced')
                    final=self.client.stat(actual.file_id)
                    if not matches(final,row,copy['folder_cid']): raise SafetyError('Temporary file changed before deletion')
                    if self.service._stop.is_set(): raise InterruptedError()
                    purge=self.prepare_purge(final)
                    if purge is not None:
                        current=self.locate_copy(row,copy)
                        final=self.client.stat(actual.file_id)
                        if current.file_id!=actual.file_id or not matches(final,row,copy['folder_cid']):
                            raise SafetyError('Temporary file changed before deletion')
                        if self.service._stop.is_set(): raise InterruptedError()
                    self.service.query("UPDATE resource_copies SET stage='DELETING' WHERE media_id=?",(copy['media_id'],))
                    try:
                        self.client.delete_verified_file(actual.file_id)
                        self.confirm_absent(actual.file_id)
                    except Exception:
                        self.service.query("UPDATE resource_copies SET stage='DELETE_UNKNOWN' WHERE media_id=?",(copy['media_id'],))
                        raise ToolError('Cleanup outcome unknown') from None
                    self.service.query("UPDATE resource_copies SET stage='EMPTY',file_id='',received_at=0,lease_until=0 WHERE media_id=?",(copy['media_id'],))
                    self.finish_purge(final,purge)
                    job['done']+=1
                except Exception:
                    job['failed']+=1;job['error']='CLEANUP_BLOCKED'
                if 'action' in job: self._job(job)
                current=self.copy(copy['media_id'])
                if current and current['stage'] in ('DELETING','DELETE_UNKNOWN'): break
                if self.config.recycle_purge and self.service.query("SELECT id FROM recycle_purge_intents WHERE stage!='DONE' LIMIT 1",one=True):
                    job['error']='RECYCLE_PURGE_BLOCKED'
                    if 'action' in job: self._job(job)
                    break
        return job

    def reconcile(self, media_id):
        """Explicit read-only reconciliation. No write SDK methods are called."""
        with self._gate:
            row=self.row(media_id)
            if not row:
                source=self.service.query('SELECT * FROM strm_files WHERE file_id=?',(media_id,),one=True)
                if not source: return
                try: actual=self.client.stat(media_id)
                except MissingFile:
                    self.service.query('DELETE FROM actual_inventory WHERE media_id=?',(media_id,))
                    return
                if not matches(actual,source,source['parent_id']): raise SafetyError('Source changed')
                return
            if row['stage']=='SHARE_CREATE_UNKNOWN': raise SafetyError('Find and attach the existing share manually')
            if row['stage']=='SOURCE_DELETE_UNKNOWN':
                try:
                    source=self.client.stat(media_id)
                    if not matches(source,row,row['parent_id']): raise SafetyError('Source changed')
                    raise SafetyError('Source still present; deletion is not automatically retried')
                except MissingFile:
                    self.service.query("UPDATE resource_storage SET kind='VIRTUAL',source_deleted=1,stage='READY' WHERE media_id=?",(media_id,))
            copy=self.copy(media_id)
            if copy and copy['stage']=='READY':
                try: actual=self.client.stat(copy['file_id'])
                except MissingFile:
                    self.service.query("UPDATE resource_copies SET stage='EMPTY',file_id='',received_at=0,lease_until=0 WHERE media_id=?",(media_id,))
                else:
                    if not matches(actual,row,copy['folder_cid']): raise SafetyError('Copy changed')
            elif copy and copy['stage']=='FOLDER_UNKNOWN':
                folders=[f for f in self.client.list_files(copy['root_cid']) if f.is_dir and f.name==copy['folder_name'] and f.parent_id==copy['root_cid']]
                if len(folders)!=1: raise SafetyError('Temporary directory cannot be identified')
                self.service.query("UPDATE resource_copies SET folder_cid=?,stage='EMPTY' WHERE media_id=?",(folders[0].file_id,media_id))
            elif copy and copy['stage'] in ('RECEIVE_UNKNOWN','RECEIVED'):
                actual=self.locate_copy(row,copy)
                self.service.query("UPDATE resource_copies SET file_id=?,stage='READY',received_at=? WHERE media_id=?",(actual.file_id,time.time(),media_id))
            elif copy and copy['stage']=='DELETE_UNKNOWN':
                self.validate_folder(copy)
                try: self.client.stat(copy['file_id'])
                except MissingFile:
                    self.service.query("UPDATE resource_copies SET stage='EMPTY',file_id='',received_at=0,lease_until=0 WHERE media_id=?",(media_id,))
                else: raise SafetyError('Temporary file remains; deletion is not retried')
            if row['kind']=='VIRTUAL' or row['source_deleted']:
                self.verify_share(self.row(media_id))
                self.service.query("UPDATE resource_storage SET stage='READY' WHERE media_id=?",(media_id,))

    def attach_share(self, media_id, link, password):
        if not isinstance(media_id,str) or len(media_id)>128: raise ValueError('Invalid media')
        code,password=parse_share(link,password)
        with self._gate:
            row=self.row(media_id)
            if not row or row['stage']!='SHARE_CREATE_UNKNOWN': raise ValueError('No unknown share creation')
            candidate={**row,'share_code':code,'password':password}
            fid=self.verify_share(candidate)
            self.service.query("UPDATE resource_storage SET share_code=?,password=?,share_fid=?,stage='SHARE_CHECK' WHERE media_id=?",(code,password,fid,media_id))
        return {'state':'ATTACHED'}

    def virtual_rows(self):
        return self.service.query("SELECT f.* FROM strm_files f JOIN resource_storage s ON s.media_id=f.file_id WHERE s.kind='VIRTUAL'")

    def skip_file(self, fid):
        return bool(self.service.query("SELECT media_id FROM resource_storage WHERE media_id=? AND kind='VIRTUAL' UNION ALL SELECT media_id FROM resource_copies WHERE file_id=?",(fid,fid),one=True))

    def skip_directory(self, cid):
        return cid==self.config.temp_cid or bool(self.service.query(
            'SELECT media_id FROM resource_copies WHERE root_cid=? OR folder_cid=? '
            'UNION ALL SELECT media_id FROM abandoned_resource_copies WHERE root_cid=? OR folder_cid=? LIMIT 1',
            (cid,cid,cid,cid),one=True))

    def close(self):
        with self.service._lock:
            self._refresh_revision+=1
            if self._refresh_timer: self._refresh_timer.cancel(); self._refresh_timer=None
            self._pending_action=None; self._refresh_pending=False
        if self._thread and self._thread is not threading.current_thread(): self._thread.join()
        with self._gate: pass
