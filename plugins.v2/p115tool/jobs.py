from __future__ import annotations
import hashlib
import json
import threading
import time
from .models import SafetyError
from .strm import virtual_path


class DurableJobs:
    """Single-consumer, SQLite-backed queue. Never replay an unknown remote write.

    RUNNING is durable before dispatch. A process restart marks it NEEDS_ATTENTION;
    only jobs which never began remain automatically runnable. The separate task
    log retains detailed operation checkpoints; payloads never contain credentials.
    """
    SHAPES = {
        'transfer': ({'file_id', 'virtual_path', 'title', 'tmdb_id', 'allow_delete', 'media_type', 'season', 'category'}, {'file_id', 'virtual_path'}),
        'archive_policy': ({'media_id', 'allow_delete'}, {'media_id'}),
        'archive_group': ({'media_ids', 'delete'}, {'media_ids'}),
        'scan': ({'allow_delete', 'scheduled'}, set()),
        'share_batch': ({'automatic', 'delete'}, set()),
        'virtualize': ({'delete'}, set()),
        'generate_batch': (set(), set()),
        'health': ({'deep'}, set()),
        'cleanup': (set(), set()),
        'generate': ({'media_id'}, {'media_id'}),
        'archive': ({'media_id', 'delete'}, {'media_id'}),
        'restore': ({'media_id'}, {'media_id'}),
        'auto_organize': ({'media_id'}, {'media_id'}),
    }

    def __init__(self, service):
        self.service = service
        self.db = service.db
        self._dispatch = threading.Lock()
        self._control = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self.db.execute("UPDATE jobs SET state='NEEDS_ATTENTION',error='Process interrupted; reconcile remote result before retry',updated_at=? WHERE state='RUNNING'", (time.time(),))
        self.db.execute("UPDATE jobs SET state='CANCELLED',error='Legacy organize/event archive replaced by organized-directory scan',updated_at=? WHERE state='PENDING' AND kind IN ('auto_organize','archive_policy')", (time.time(),))

    def validate(self, kind, payload):
        if kind not in self.SHAPES or not isinstance(payload, dict):
            raise ValueError('Unknown job kind/payload')
        allowed, required = self.SHAPES[kind]
        if set(payload) - allowed or required - set(payload):
            raise ValueError('Invalid job fields')
        data = dict(payload)
        if data.get('category') is not None:
            from .organizer import portable_title
            data['category'] = portable_title(data['category'])
        for name in ('allow_delete', 'delete', 'deep', 'scheduled', 'automatic'):
            if name in data and not isinstance(data[name], bool):
                raise ValueError('Boolean job flags must be actual booleans')
        for name in ('media_id', 'tmdb_id'):
            if data.get(name) is not None and (type(data[name]) is not int or data[name] < 1):
                raise ValueError('Invalid media identifier')
        for name in ('file_id', 'parent_id'):
            if name in data and (not isinstance(data[name], str) or not data[name].isdigit()):
                raise ValueError('Invalid remote identifier')
        if 'virtual_path' in data:
            data['virtual_path'] = virtual_path(data['virtual_path'])
        for name in ('title', 'name'):
            if name in data and data[name] is not None and (not isinstance(data[name], str) or len(data[name]) > 1000):
                raise ValueError('Invalid job text')
        if data.get('media_type') is not None and data['media_type'] not in ('MOVIE','TV','UNKNOWN'):
            raise ValueError('Invalid media type')
        if data.get('season') is not None and (type(data['season']) is not int or not 0 <= data['season'] <= 999):
            raise ValueError('Invalid season')
        if 'media_ids' in data:
            ids=data['media_ids']
            if not isinstance(ids,list) or not ids or len(ids)>1000 or any(type(mid) is not int or mid<1 for mid in ids) or len(set(ids))!=len(ids):
                raise ValueError('Invalid group membership')
        if kind in ('transfer', 'scan', 'archive_policy'):
            data.setdefault('allow_delete', self.service.config.auto_delete)
        if data.get('delete') or data.get('allow_delete'):
            permitted = self.service.config.delete_source
            if kind in ('transfer', 'scan', 'archive_policy'):
                permitted = permitted and self.service.config.auto_delete
            if not permitted:
                raise SafetyError('Deletion is not authorized by current configuration')
        return data

    def enqueue(self, kind, payload=None, dedup_key=None, delay=0):
        self.service.available()
        data = self.validate(kind, payload or {})
        if dedup_key is not None and (not isinstance(dedup_key, str) or len(dedup_key) > 128):
            raise ValueError('Invalid deduplication key')
        now = time.time()
        if type(delay) not in (int,float) or not 0 <= delay <= 86400:
            raise ValueError('Invalid job delay')
        with self.db.connect() as db:
            if dedup_key:
                old = db.execute('SELECT * FROM jobs WHERE dedup_key=?', (dedup_key,)).fetchone()
                if old:
                    if old['kind'] != kind or json.loads(old['payload']) != data:
                        raise SafetyError('Deduplication key was reused for another operation')
                    return self.public(dict(old))
            jid = db.execute('INSERT INTO jobs(kind,payload,dedup_key,created_at,updated_at,not_before) VALUES(?,?,?,?,?,?)',
                             (kind, json.dumps(data, sort_keys=True), dedup_key, now, now, now+delay)).lastrowid
        self._wake.set()
        return self.get(jid)

    def enqueue_active(self, kind, payload=None):
        """Coalesce scheduler ticks only while the same operation is active."""
        self.service.available()
        data = self.validate(kind, payload or {})
        encoded = json.dumps(data, sort_keys=True)
        now = time.time()
        with self.db.connect() as db:
            old = db.execute("SELECT * FROM jobs WHERE kind=? AND payload=? AND state IN ('PENDING','RUNNING') ORDER BY id LIMIT 1", (kind, encoded)).fetchone()
            if old:
                return self.public(dict(old))
            jid = db.execute('INSERT INTO jobs(kind,payload,created_at,updated_at) VALUES(?,?,?,?)', (kind, encoded, now, now)).lastrowid
        self._wake.set()
        return self.get(jid)

    def enqueue_transfer(self, payload):
        # Secrets and arbitrary event metadata are deliberately not serialized.
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return self.enqueue('transfer', payload, 'transfer:' + digest)

    @staticmethod
    def public(row):
        return {k: row[k] for k in ('id', 'kind', 'state', 'attempts', 'created_at', 'updated_at', 'error')}

    def get(self, jid):
        row = self.db.one('SELECT * FROM jobs WHERE id=?', (jid,))
        if not row:
            raise KeyError('Job not found')
        return self.public(row)

    def list(self, limit=100, offset=0):
        return [self.public(row) for row in self.db.all('SELECT * FROM jobs ORDER BY id DESC LIMIT ? OFFSET ?', (limit, offset))]

    def cancel(self, jid):
        if not self.db.execute("UPDATE jobs SET state='CANCELLED',updated_at=? WHERE id=? AND state='PENDING'", (time.time(), jid)):
            self.get(jid)
            raise SafetyError('Only pending jobs can be cancelled')
        return self.get(jid)

    def retry(self, jid):
        row = self.db.one('SELECT * FROM jobs WHERE id=?', (jid,))
        if not row:
            raise KeyError('Job not found')
        if row['state'] not in ('FAILED', 'NEEDS_ATTENTION', 'CANCELLED'):
            raise SafetyError('Job is not retryable')
        data = json.loads(row['payload'])
        # Reconciliation is mandatory for every operation that could issue a
        # remote write; a generic retry button must not defeat safety checkpoints.
        if row['kind'] not in ('health', 'generate'):
            raise SafetyError('Remote-write jobs require operation-specific reconciliation')
        self.validate(row['kind'], data)
        changed = self.db.execute("UPDATE jobs SET state='PENDING',error=NULL,updated_at=? WHERE id=? AND state=?",
                                  (time.time(), jid, row['state']))
        if not changed:
            raise SafetyError('Job state changed concurrently')
        self._wake.set()
        return self.get(jid)

    def run_next(self):
        self.service.available()
        if not self._dispatch.acquire(blocking=False):
            return None
        try:
            with self.db.connect() as db:
                row = db.execute("SELECT * FROM jobs WHERE state='PENDING' AND not_before<=? ORDER BY id LIMIT 1", (time.time(),)).fetchone()
                if not row:
                    return None
                row = dict(row)
                db.execute("UPDATE jobs SET state='RUNNING',attempts=attempts+1,updated_at=? WHERE id=? AND state='PENDING'", (time.time(), row['id']))
            try:
                self.db.log('job', 'RUNNING', detail=f"job_id={row['id']}; kind={row['kind']}")
                from .activity import activity
                activity('任务开始', f"任务ID={row['id']} 类型={row['kind']}")
                payload = self.validate(row['kind'], json.loads(row['payload']))
                with self.service._maintenance:
                    self.service.available()
                    self.dispatch(row['kind'], payload)
                state, error = 'DONE', None
            except Exception as exc:
                state = 'FAILED' if row['kind'] in ('health', 'generate') else 'NEEDS_ATTENTION'
                error = type(exc).__name__ + ': operation failed; inspect durable checkpoints'
            self.db.execute('UPDATE jobs SET state=?,error=?,updated_at=? WHERE id=?', (state, error, time.time(), row['id']))
            self.db.log('job', state, detail=f"job_id={row['id']}; kind={row['kind']}")
            from .activity import activity
            activity('任务结束', f"{state} 任务ID={row['id']} 类型={row['kind']}")
            return self.get(row['id'])
        finally:
            self._dispatch.release()

    def dispatch(self, kind, data):
        service = self.service
        if kind == 'transfer':
            file = service.client.stat(data['file_id'])
            media=service.ingest(file, data['virtual_path'], data.get('title'), data.get('tmdb_id'), allow_auto_delete=data['allow_delete'],
                defer_archive=True,defer_generate=True,media_type=data.get('media_type'),season=data.get('season'),category=data.get('category'))
            # Output creation belongs to the scheduled/manual batch stages.
            return media
        if kind == 'archive_policy':
            return service.groups.policy(data['media_id'],allow_delete=data['allow_delete'])
        if kind == 'archive_group':
            return service.groups.archive(data['media_ids'],delete=data.get('delete',False))
        if kind == 'scan':
            result = service.scan(allow_auto_delete=False)
            if result['errors']:
                raise SafetyError('Directory import incomplete; output stages were not submitted')
            if data.get('scheduled'):
                if service.config.share_enabled and (service.config.auto_archive or any(r['storage'] == 'SHARE' for r in service.config.policies)):
                    self.enqueue_active('share_batch', {'automatic': True, 'delete': service.config.auto_delete})
                if service.config.auto_generate:
                    self.enqueue_active('generate_batch')
            return result
        if kind == 'virtualize':
            result = service.scan(allow_auto_delete=False)
            if result['errors']:
                raise SafetyError('Directory import incomplete; virtualization was not started')
            from .postprocess import run_batch
            return run_batch(service, 'share_batch', delete=data.get('delete', False))
        if kind in ('share_batch', 'generate_batch'):
            from .postprocess import run_batch
            return run_batch(service, kind, automatic=data.get('automatic', False), delete=data.get('delete', False))
        if kind == 'health':
            return service.health(deep=data.get('deep', False))
        if kind == 'cleanup':
            return service.cleanup()
        if kind == 'generate':
            return service.strm.generate(data['media_id'])
        if kind == 'archive':
            return service.archive(data['media_id'], delete=data.get('delete', False), generate_strm=data.get('delete', False))
        if kind == 'restore':
            return service.restore(data['media_id'])
        if kind == 'auto_organize':
            return service.automatic_organize(data['media_id'])
        raise ValueError('Unknown job kind')

    def start(self):
        with self._control:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name='p115tool-jobs', daemon=True)
            self._thread.start()

    def _worker(self):
        while not self._stop.is_set():
            try:
                job = self.run_next()
            except Exception:
                job = None
            if job is None:
                self._wake.wait(0.5)
                self._wake.clear()

    def stop(self):
        with self._control:
            self._stop.set()
            self._wake.set()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join()  # Finish in-flight SDK calls before releasing process ownership.
