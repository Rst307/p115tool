"""Two storage types with private share mappings and bounded temporary copies.

All remote writes are checkpointed before submission. Unknown results are only
reconciled by reads, never resent. No legacy share/cache/delete tasks are resumed.
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
from .models import RemoteFile, MissingFile, SafetyError, ToolError
from .strm import classify, safe_parts

UA = 'p115tool-storage-validation'
PLAYBACK_GUARD = 24 * 3600


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
            for before,after in [('SHARE_CREATING','SHARE_CREATE_UNKNOWN'),('SOURCE_DELETING','SOURCE_DELETE_UNKNOWN')]:
                db.execute('UPDATE resource_storage SET stage=? WHERE stage=?',(after,before))
            for before,after in [('FOLDER_CREATING','FOLDER_UNKNOWN'),('RECEIVING','RECEIVE_UNKNOWN'),('DELETING','DELETE_UNKNOWN')]:
                db.execute('UPDATE resource_copies SET stage=? WHERE stage=?',(after,before))
        job=self.job()
        if job['state']=='RUNNING': job['state']='INTERRUPTED'; self._job(job)

    @property
    def busy(self): return bool(self._thread and self._thread.is_alive())

    def job(self):
        row=self.service.query('SELECT value FROM resource_job WHERE id=1',one=True)
        return json.loads(row['value']) if row else {'state':'IDLE','action':'','done':0,'failed':0,'error':None}

    def _job(self, value):
        self.service.query('INSERT INTO resource_job VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',
                           (json.dumps(value),))

    def row(self, media_id):
        return self.service.query('SELECT f.*,s.kind,s.stage,s.share_code,s.password,s.share_fid,s.own,s.source_deleted '
            'FROM strm_files f JOIN resource_storage s ON s.media_id=f.file_id WHERE f.file_id=?',(media_id,),one=True)

    def copy(self, media_id):
        return self.service.query('SELECT * FROM resource_copies WHERE media_id=?',(media_id,),one=True)

    def listing(self, page=1, kind='ALL', search=''):
        if type(page) is not int or not 1<=page<=100000 or kind not in ('ALL','ACTUAL','VIRTUAL') or not isinstance(search,str) or len(search)>100:
            raise ValueError('Invalid storage filter')
        where="WHERE (?='ALL' OR coalesce(s.kind,'ACTUAL')=?) AND instr(lower(f.relative_path),lower(?))>0"
        args=(kind,kind,search)
        total=self.service.query('SELECT count(*) n FROM strm_files f LEFT JOIN resource_storage s ON s.media_id=f.file_id '+where,args,one=True)['n']
        items=self.service.query("SELECT f.file_id id,f.name,f.relative_path path,coalesce(s.kind,'ACTUAL') kind,"
            "coalesce(s.stage,'READY') stage,c.stage copy_state,c.received_at,c.lease_until FROM strm_files f "
            'LEFT JOIN resource_storage s ON s.media_id=f.file_id LEFT JOIN resource_copies c ON c.media_id=f.file_id '
            +where+' ORDER BY f.relative_path,f.file_id LIMIT 50 OFFSET ?',(*args,(page-1)*50))
        for item in items:
            item['expires_at']=(item.pop('received_at') or 0)+self.config.temp_days*86400 if item['copy_state']=='READY' else None
        return {'items':items,'total':total,'page':page,'job':self.job(),'busy':self.busy,
                'temp_days':self.config.temp_days,'cleanup_enabled':self.config.temp_cleanup,
                'temp_configured':bool(self.config.temp_cid)}

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
            if not self.config.temp_cid: return {'state':'CONFIG_REQUIRED','field':'temp_cid'}
        elif action=='virtualize':
            if set(payload)!={'ids','delete_source'} or payload['delete_source'] is not True:
                raise ValueError('Explicit source deletion authorization required')
            args=(self._selection(payload['ids']),)
            if not self.config.temp_cid: return {'state':'CONFIG_REQUIRED','field':'temp_cid'}
        elif action=='reconcile':
            if set(payload)!={'ids'}: raise ValueError('Invalid reconciliation')
            args=(self._selection(payload['ids']),)
        elif action=='cleanup':
            if payload: raise ValueError('Invalid cleanup')
            args=()
        else: raise ValueError('Unknown storage action')
        with self.service._lock:
            if self.service._closed: raise ToolError('Service stopped')
            if self.busy or (self.service._worker and self.service._worker.is_alive()): return {'state':'BUSY'}
            self._job({'state':'RUNNING','action':action,'done':0,'failed':0,'error':None})
            self._thread=threading.Thread(target=self._run,args=(action,args),name='p115tool-storage',daemon=True)
            try: self._thread.start()
            except Exception:
                self._job({'state':'FAILED','action':action,'done':0,'failed':1,'error':'START_FAILED'})
                self._thread=None
                raise ToolError('Storage task did not start') from None
            return {'state':'RUNNING'}

    def _run(self, action, args):
        job={'state':'RUNNING','action':action,'done':0,'failed':0,'error':None}
        try:
            if action=='import': self.import_share(*args,job=job)
            elif action=='cleanup': self.cleanup(job)
            else:
                for media_id in args[0]:
                    if self.service._stop.is_set(): raise InterruptedError()
                    try:
                        if action=='virtualize': self.virtualize(media_id)
                        else: self.reconcile(media_id)
                        job['done']+=1
                    except Exception:
                        job['failed']+=1;job['error']='RESOURCE_BLOCKED'
                        row=self.row(media_id)
                        if action=='virtualize' and row and row['stage'] in ('SHARE_CREATE_UNKNOWN','SOURCE_DELETE_UNKNOWN','SHARE_UNAVAILABLE'):
                            self._job(job)
                            break
                    self._job(job)
            job['state']='DONE' if not job['failed'] else 'PARTIAL'
        except InterruptedError: job['state']='INTERRUPTED'
        except Exception: job['state']='FAILED';job['error']='STORAGE_FAILED';job['failed']+=1
        finally:
            self._job(job)
            with self.service._lock:
                if self.service._rerun and not self.service._closed:
                    self._thread=None
                    self.service._rerun=False
                    try: self.service.start()
                    except Exception: pass

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
            destination=classify('/分享导入/'+digest[:8],relative,
                **({'recognizer':self.service.recognizer} if self.service.recognizer else {}))
            record=RemoteFile(media_id,file.name,file.size,file.sha1,'0','',is_dir=False)
            self.service.register(record,destination)
            existing=self.row(media_id)
            if existing and (existing['share_fid']!=file.file_id or existing['share_code']!=code):
                raise SafetyError('Share mapping changed')
            self.service.query("INSERT INTO resource_storage(media_id,kind,stage,share_code,password,share_fid) "
                "VALUES(?,'VIRTUAL','READY',?,?,?) ON CONFLICT(media_id) DO UPDATE SET password=excluded.password",
                (media_id,code,password,file.file_id))
            try:
                row=self.row(media_id)
                self.service.write_output(media_id,row['relative_path'],row['token'])
                self.ensure_copy(media_id)
                job['done']+=1
            except Exception:
                job['failed']+=1;job['error']='RESOURCE_BLOCKED'
            self._job(job)

    def verify_share(self, row):
        candidates=[file for file,_ in self.share_tree(row['share_code'],row['password']) if matches(file,row)]
        if len(candidates)!=1: raise SafetyError('Share identity is ambiguous')
        link=self.client.share_link(row['share_code'],row['password'],candidates[0].file_id,UA)
        self.client.probe_range(link,UA,row['size'])
        return candidates[0].file_id

    def virtualize(self, media_id):
        with self._gate:
            if self.service._stop.is_set(): raise InterruptedError()
            row=self.row(media_id)
            if row and row['kind']=='VIRTUAL': return
            if row and row['stage'] in ('SHARE_CREATE_UNKNOWN','SOURCE_DELETE_UNKNOWN','SHARE_CREATING','SOURCE_DELETING'):
                raise SafetyError('Unresolved write checkpoint')
            source=self.service.query('SELECT * FROM strm_files WHERE file_id=?',(media_id,),one=True)
            if not source or not re.fullmatch(r'[1-9][0-9]{0,19}',media_id): raise SafetyError('Invalid actual source')
            actual=self.client.stat(media_id)
            if not matches(actual,source,source['parent_id']): raise SafetyError('Source identity changed')
            # Ensure a readable, owned STRM exists before considering source removal.
            self.service.write_output(media_id,source['relative_path'],source['token'])
            if not row:
                self.service.query("INSERT INTO resource_storage(media_id,kind,stage,own) VALUES(?,'ACTUAL','SHARE_CREATING',1)",(media_id,))
                try: code,password=self.client.create_share(media_id)
                except Exception:
                    self.service.query("UPDATE resource_storage SET stage='SHARE_CREATE_UNKNOWN' WHERE media_id=?",(media_id,))
                    raise ToolError('Share creation outcome unknown') from None
                self.service.query("UPDATE resource_storage SET share_code=?,password=?,stage='SHARE_CHECK' WHERE media_id=?",(code,password,media_id))
            row=self.row(media_id)
            self.client.retain_share(row['share_code'])
            fid=self.verify_share(row)
            self.service.query('UPDATE resource_storage SET share_fid=? WHERE media_id=?',(fid,media_id))
            # Exercise share-to-personal restoration before deletion; its verified
            # temporary copy also provides a fallback after the original is gone.
            self.ensure_copy(media_id)
            row=self.row(media_id)
            actual=self.client.stat(media_id)
            if not matches(actual,row,row['parent_id']): raise SafetyError('Source changed before deletion')
            if self.service._stop.is_set(): raise InterruptedError()
            self.service.query("UPDATE resource_storage SET stage='SOURCE_DELETING' WHERE media_id=?",(media_id,))
            try: self.client.delete_verified_file(media_id)
            except Exception:
                self.service.query("UPDATE resource_storage SET stage='SOURCE_DELETE_UNKNOWN' WHERE media_id=?",(media_id,))
                raise ToolError('Source deletion outcome unknown') from None
            self.service.query("UPDATE resource_storage SET kind='VIRTUAL',source_deleted=1,stage='READY' WHERE media_id=?",(media_id,))
            try: self.verify_share(self.row(media_id))
            except Exception:
                self.service.query("UPDATE resource_storage SET stage='SHARE_UNAVAILABLE' WHERE media_id=?",(media_id,))
                raise ToolError('Share needs attention; verified temporary copy retained') from None

    def validate_folder(self, copy):
        folder=self.client.stat(copy['folder_cid'])
        if not folder.is_dir or folder.parent_id!=copy['root_cid'] or folder.name!=copy['folder_name']:
            raise SafetyError('Temporary directory identity changed')
        return folder

    def locate_copy(self, row, copy):
        self.validate_folder(copy)
        files=list(self.client.list_files(copy['folder_cid']))
        candidates=[f for f in files if matches(f,row,copy['folder_cid'])]
        if len(files)!=1 or len(candidates)!=1 or not candidates[0].pickcode:
            raise SafetyError('Temporary contents not uniquely verified')
        return candidates[0]

    def create_copy_folder(self, media_id, root):
        if not root: raise SafetyError('Temporary directory not configured')
        if self.client.directory_path(root)=='/': raise SafetyError('Invalid temporary root')
        name='p115tool-'+uuid.uuid4().hex
        # Commit the new identity before creating; an unknown result is never retried.
        self.service.query("INSERT INTO resource_copies(media_id,root_cid,folder_name,stage) VALUES(?,?,?,'FOLDER_CREATING') "
            "ON CONFLICT(media_id) DO UPDATE SET root_cid=excluded.root_cid,folder_name=excluded.folder_name,"
            "folder_cid='',file_id='',received_at=0,lease_until=0,stage='FOLDER_CREATING'",(media_id,root,name))
        try: cid=self.client.create_temp_directory(root,name)
        except Exception:
            self.service.query("UPDATE resource_copies SET stage='FOLDER_UNKNOWN' WHERE media_id=?",(media_id,))
            raise ToolError('Temporary directory outcome unknown') from None
        self.service.query("UPDATE resource_copies SET folder_cid=?,stage='EMPTY' WHERE media_id=?",(cid,media_id))
        return self.copy(media_id)

    def ensure_copy(self, media_id):
        with self._gate:
            if self.service._stop.is_set(): raise InterruptedError()
            row=self.row(media_id)
            if not row or not row['share_fid']: raise SafetyError('Share mapping unavailable')
            copy=self.copy(media_id)
            if copy and copy['stage']=='READY':
                try: actual=self.client.stat(copy['file_id'])
                except MissingFile:
                    try: self.validate_folder(copy)
                    except MissingFile:
                        self.create_copy_folder(media_id,copy['root_cid'])
                        return self.ensure_copy(media_id)
                    if list(self.client.list_files(copy['folder_cid'])): raise SafetyError('Temporary contents changed')
                    self.service.query("UPDATE resource_copies SET stage='EMPTY',file_id='',received_at=0,lease_until=0 WHERE media_id=?",(media_id,))
                    return self.ensure_copy(media_id)
                if not matches(actual,row,copy['folder_cid']): raise SafetyError('Temporary identity changed')
                self.validate_folder(copy)
                return actual
            if copy and copy['stage'] in ('FOLDER_UNKNOWN','RECEIVE_UNKNOWN','DELETE_UNKNOWN','FOLDER_CREATING','RECEIVING','DELETING'):
                raise SafetyError('Temporary write outcome unresolved')
            if copy and copy['stage']=='RECEIVED':
                actual=self.locate_copy(row,copy)
                self.service.query("UPDATE resource_copies SET file_id=?,stage='READY',received_at=? WHERE media_id=?",(actual.file_id,time.time(),media_id))
                return actual
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
            except Exception:
                self.service.query("UPDATE resource_copies SET stage='RECEIVE_UNKNOWN' WHERE media_id=?",(media_id,))
                raise ToolError('Temporary transfer outcome unknown') from None
            self.service.query("UPDATE resource_copies SET stage='RECEIVED' WHERE media_id=?",(media_id,))
            return self.ensure_copy(media_id)

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
            return self.client.normal_link(actual.pickcode,ua).url

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
                    self.service.query("UPDATE resource_copies SET stage='DELETING' WHERE media_id=?",(copy['media_id'],))
                    try: self.client.delete_verified_file(actual.file_id)
                    except Exception:
                        self.service.query("UPDATE resource_copies SET stage='DELETE_UNKNOWN' WHERE media_id=?",(copy['media_id'],))
                        raise ToolError('Cleanup outcome unknown') from None
                    self.service.query("UPDATE resource_copies SET stage='EMPTY',file_id='',received_at=0,lease_until=0 WHERE media_id=?",(copy['media_id'],))
                    job['done']+=1
                except Exception:
                    job['failed']+=1;job['error']='CLEANUP_BLOCKED'
                if 'action' in job: self._job(job)
                current=self.copy(copy['media_id'])
                if current and current['stage'] in ('DELETING','DELETE_UNKNOWN'): break
        return job

    def reconcile(self, media_id):
        """Explicit read-only reconciliation. No write SDK methods are called."""
        with self._gate:
            row=self.row(media_id)
            if not row: return
            if row['stage']=='SHARE_CREATE_UNKNOWN': raise SafetyError('Find and attach the existing share manually')
            if row['stage']=='SOURCE_DELETE_UNKNOWN':
                try:
                    source=self.client.stat(media_id)
                    if not matches(source,row,row['parent_id']): raise SafetyError('Source changed')
                    raise SafetyError('Source still present; deletion is not automatically retried')
                except MissingFile:
                    self.service.query("UPDATE resource_storage SET kind='VIRTUAL',source_deleted=1,stage='READY' WHERE media_id=?",(media_id,))
            copy=self.copy(media_id)
            if copy and copy['stage']=='FOLDER_UNKNOWN':
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
            'SELECT media_id FROM resource_copies WHERE root_cid=? OR folder_cid=? LIMIT 1',(cid,cid),one=True))

    def close(self):
        if self._thread and self._thread is not threading.current_thread(): self._thread.join()
        with self._gate: pass
