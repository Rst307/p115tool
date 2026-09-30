"""Read-only 115 traversal, stable local playback mapping and one STRM worker."""
from pathlib import Path
from contextlib import closing
import json
import secrets
import sqlite3
import threading
from .client import P115ClientManager
from .models import SafetyError, MissingFile, ToolError
from .strm import StrmManager, classify, safe_parts

class Service:
    def __init__(self,config,client=None,recognizer=None):
        self.config=config
        self.client=client or P115ClientManager(config)
        self._lock=threading.RLock(); self._stop=threading.Event(); self._worker=None
        self._closed=False; self.recognizer=recognizer
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
                    PRIMARY KEY(file_id,old_path));''')
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
        except Exception:
            self.client.close(); self._file_lock.close(); raise

    def query(self,sql,args=(),one=False):
        with self._lock,closing(sqlite3.connect(self.path)) as db,db:
            db.row_factory=sqlite3.Row
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
            return {**self._state(),'count':self.query('SELECT count(*) count FROM strm_files',one=True)['count']}

    def start(self):
        with self._lock:
            if self._closed: raise ToolError('Service stopped')
            if self._worker and self._worker.is_alive(): return self.snapshot()
            if not self.config.source_cids: raise ValueError('Configure source folders')
            self._save_state({'state':'RUNNING','found':0,'generated':0,'failed':0,'error':None})
            self._worker=threading.Thread(target=self.generate_all,name='p115tool-strm',daemon=True)
            self._worker.start()
            return self.snapshot()

    def generate_all(self):
        state={'state':'RUNNING','found':0,'generated':0,'failed':0,'error':None}
        self._save_state(state)
        seen_files=set(); seen_dirs=set(); outputs={}
        state['relocated']=0
        state['paths']=[]
        try:
            for source in self.config.source_cids:
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
                        if file.is_dir: stack.append((file.file_id,path)); continue
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
                            previous=self.query('SELECT strm_path FROM strm_files WHERE file_id=?',(file.file_id,),one=True)
                            token=self.register(file,destination)
                            output=self.strm.generate(destination,token)
                            if previous and previous['strm_path'] and previous['strm_path']!=output:
                                if self.relocate(file.file_id,previous['strm_path'],output,token)=='DONE':
                                    state['relocated']+=1
                            self.query('UPDATE strm_files SET strm_path=? WHERE file_id=?',(output,file.file_id))
                            outputs[self.config.playback_url(token)+'\n']=(file.file_id,output,token)
                            if len(state['paths'])<10:
                                state['paths'].append({'source':actual_path or prefix.rstrip('/')+'/'+path,'output':output})
                            state['generated']+=1
                        except Exception as exc:
                            state['failed']+=1
                            state['error']='OUTPUT_CONFLICT' if isinstance(exc,SafetyError) else 'FILE_GENERATION_FAILED'
                        self._save_state(state)
            state['state']='DONE' if not state['failed'] else 'PARTIAL'
            if state['state']=='DONE':
                # Earlier versions updated strm_path but left their old output
                # behind. Only exact stable-URL duplicates of files successfully
                # generated this round, with the same filename, are candidates.
                for old in self.strm.root.rglob('*.strm'):
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

    def register(self,file,path):
        with self._lock:
            row=self.query('SELECT * FROM strm_files WHERE file_id=?',(file.file_id,),one=True)
            if row and (row['size']!=file.size or (row['sha1'] and file.sha1 and row['sha1']!=file.sha1)):
                raise SafetyError('Source identity changed')
            token=row['token'] if row else secrets.token_urlsafe(32)
            self.query('''INSERT INTO strm_files VALUES(?,?,?,?,?,?,?,?,NULL)
                ON CONFLICT(file_id) DO UPDATE SET pickcode=excluded.pickcode,name=excluded.name,
                sha1=CASE WHEN excluded.sha1='' THEN strm_files.sha1 ELSE excluded.sha1 END,parent_id=excluded.parent_id,relative_path=excluded.relative_path''',
                (file.file_id,token,file.pickcode,file.name,file.size,file.sha1,file.parent_id,path))
            return token

    def play(self,token,ua):
        with self._lock:
            if self._closed: raise ToolError('Service stopped')
            row=self.query('SELECT pickcode FROM strm_files WHERE token=?',(token,),one=True)
            if not row: raise MissingFile('Playback mapping unavailable')
            link=self.client.normal_link(row['pickcode'],ua)
            return link.url

    def close(self):
        with self._lock: self._closed=True; self._stop.set(); worker=self._worker
        if worker and worker is not threading.current_thread(): worker.join()
        with self._lock:
            self.client.close(); self._file_lock.close()
