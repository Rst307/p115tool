from __future__ import annotations
from contextlib import contextmanager, closing
from pathlib import Path
import json
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from .models import MediaObject, RemoteFile, SafetyError

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
 id INTEGER PRIMARY KEY, token TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
 file_name TEXT NOT NULL, virtual_path TEXT NOT NULL UNIQUE, size INTEGER NOT NULL,
 sha1 TEXT NOT NULL, storage_type TEXT NOT NULL DEFAULT 'NORMAL',
 status TEXT NOT NULL DEFAULT 'DISCOVERED', strm_path TEXT, source_deleted INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT, tmdb_id INTEGER);
CREATE TABLE IF NOT EXISTS normal_objects (
 media_id INTEGER PRIMARY KEY REFERENCES media(id), file_id TEXT NOT NULL UNIQUE,
 pickcode TEXT NOT NULL, parent_id TEXT NOT NULL, path TEXT NOT NULL,
 sha1 TEXT NOT NULL, size INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS share_objects (
 media_id INTEGER PRIMARY KEY REFERENCES media(id), share_code TEXT NOT NULL,
 receive_code TEXT NOT NULL, file_id TEXT NOT NULL, parent_id TEXT NOT NULL,
 sha1 TEXT NOT NULL, size INTEGER NOT NULL, name TEXT NOT NULL,
 created_at REAL NOT NULL, verified_at REAL, range_verified_at REAL);
CREATE TABLE IF NOT EXISTS cache_objects (
 media_id INTEGER PRIMARY KEY REFERENCES media(id), file_id TEXT NOT NULL,
 pickcode TEXT NOT NULL, parent_id TEXT NOT NULL, sha1 TEXT NOT NULL, size INTEGER NOT NULL,
 created_at REAL NOT NULL, last_access_at REAL NOT NULL, expire_at REAL NOT NULL,
 lease_until REAL NOT NULL, state TEXT NOT NULL DEFAULT 'READY');
CREATE TABLE IF NOT EXISTS cache_folders (
 media_id INTEGER PRIMARY KEY REFERENCES media(id), folder_id TEXT NOT NULL UNIQUE,
 created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
 id INTEGER PRIMARY KEY, media_id INTEGER REFERENCES media(id), operation TEXT NOT NULL,
 state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS metrics (
 name TEXT PRIMARY KEY, value REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS metrics_daily (
 day TEXT NOT NULL,utc_offset INTEGER NOT NULL,name TEXT NOT NULL,value REAL NOT NULL,
 PRIMARY KEY(day,utc_offset,name));
CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
 dedup_key TEXT UNIQUE, state TEXT NOT NULL DEFAULT 'PENDING',
 attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
 updated_at REAL NOT NULL, error TEXT);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state,id);
CREATE TABLE IF NOT EXISTS organize_plans (
 media_id INTEGER PRIMARY KEY REFERENCES media(id), file_id TEXT NOT NULL,
 old_parent TEXT NOT NULL, old_name TEXT NOT NULL, new_parent TEXT NOT NULL,
 new_name TEXT NOT NULL, virtual_path TEXT NOT NULL, sha1 TEXT NOT NULL,
 size INTEGER NOT NULL, state TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS media_metadata (
 media_id INTEGER PRIMARY KEY REFERENCES media(id), media_type TEXT NOT NULL,
 season INTEGER, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS share_groups (
 id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
 state TEXT NOT NULL, share_code TEXT, receive_code TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS share_group_members (
 group_id INTEGER NOT NULL REFERENCES share_groups(id),
 media_id INTEGER NOT NULL REFERENCES media(id), file_id TEXT NOT NULL,
 name TEXT NOT NULL, size INTEGER NOT NULL, sha1 TEXT NOT NULL,
 PRIMARY KEY(group_id,media_id));
CREATE TABLE IF NOT EXISTS media_groups (
 media_id INTEGER PRIMARY KEY REFERENCES media(id),
 group_id INTEGER NOT NULL REFERENCES share_groups(id));
CREATE TABLE IF NOT EXISTS archive_permissions (
 media_id INTEGER PRIMARY KEY REFERENCES media(id), allow_delete INTEGER NOT NULL,
 requested_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS emby_sessions (
 session_key TEXT PRIMARY KEY,media_id INTEGER NOT NULL REFERENCES media(id),
 state TEXT NOT NULL,updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS scan_members (
 root_cid TEXT NOT NULL,media_id INTEGER NOT NULL REFERENCES media(id),
 PRIMARY KEY(root_cid,media_id));
CREATE TABLE IF NOT EXISTS missing_sources (
 media_id INTEGER PRIMARY KEY REFERENCES media(id),file_id TEXT NOT NULL,
 confirmed_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_media_status ON media(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
CREATE TABLE IF NOT EXISTS recycle_intents (
 id INTEGER PRIMARY KEY,media_id INTEGER NOT NULL REFERENCES media(id),
 purpose TEXT NOT NULL CHECK(purpose IN ('SOURCE','CACHE')),
 file_id TEXT NOT NULL,parent_id TEXT NOT NULL,name TEXT NOT NULL,sha1 TEXT NOT NULL,size INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('REQUESTED','ACKNOWLEDGED','ABSENT','RETAINED')),
 requested_at REAL NOT NULL,observed_at REAL);
CREATE INDEX IF NOT EXISTS idx_recycle_intents_file ON recycle_intents(file_id,state);
PRAGMA user_version=7;
"""


class Database:
    def __init__(self, path, statistics_utc_offset=480):
        self.statistics_utc_offset = statistics_utc_offset
        self.statistics_timezone = timezone(timedelta(minutes=statistics_utc_offset))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self._lock = threading.RLock()
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 7:
                raise SafetyError("Database schema is newer than this plugin")
            db.executescript(SCHEMA)
            if 'not_before' not in {row[1] for row in db.execute('PRAGMA table_info(jobs)')}:
                db.execute('ALTER TABLE jobs ADD COLUMN not_before REAL NOT NULL DEFAULT 0')

    @contextmanager
    def connect(self):
        with self._lock:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def one(self, sql, args=()):
        with self.connect() as db:
            row = db.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).rowcount

    def media(self, media_id=None, token=None):
        row = self.one("SELECT * FROM media WHERE " + ("token=?" if token else "id=?"), (token or media_id,))
        if not row:
            raise KeyError("Media not found")
        return MediaObject(**row)

    def ingest(self, file: RemoteFile, virtual_path, title=None, tmdb_id=None):
        if file.is_dir or file.size < 0 or not file.file_id or not file.pickcode:
            raise ValueError("Expected a real file with pickcode")
        now = time.time()
        with self.connect() as db:
            existing = db.execute("SELECT media_id FROM normal_objects WHERE file_id=?", (file.file_id,)).fetchone()
            if existing:
                mid = existing[0]
                old = db.execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()
                if old["source_deleted"]:
                    raise SafetyError("Deleted source identity cannot be silently replaced")
                if old["sha1"].upper() != file.sha1.upper() or old["size"] != file.size:
                    raise SafetyError("Remote file content changed; explicit re-import required")
                db.execute("UPDATE media SET file_name=?,virtual_path=?,title=?,updated_at=? WHERE id=?",
                           (file.name, virtual_path, title or old["title"], now, mid))
            else:
                cur = db.execute("INSERT INTO media(token,title,file_name,virtual_path,size,sha1,created_at,updated_at,tmdb_id) VALUES(?,?,?,?,?,?,?,?,?)",
                                 (secrets.token_urlsafe(32), title or file.name, file.name, virtual_path, file.size, file.sha1.upper(), now, now, tmdb_id))
                mid = cur.lastrowid
            db.execute("INSERT INTO normal_objects VALUES(?,?,?,?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET pickcode=excluded.pickcode,parent_id=excluded.parent_id,path=excluded.path",
                       (mid, file.file_id, file.pickcode, file.parent_id, file.path, file.sha1.upper(), file.size))
        return self.media(mid)

    def transition(self, mid, status, error=None, storage=None):
        with self.connect() as db:
            db.execute("UPDATE media SET status=?,error=?,updated_at=? WHERE id=?", (status, error, time.time(), mid))
            if storage:
                db.execute("UPDATE media SET storage_type=? WHERE id=?", (storage, mid))
            db.execute("INSERT INTO tasks(media_id,operation,state,detail,created_at) VALUES(?,?,?,?,?)",
                       (mid, "archive", status, error or "", time.time()))

    def log(self, operation, state, mid=None, detail=""):
        self.execute("INSERT INTO tasks(media_id,operation,state,detail,created_at) VALUES(?,?,?,?,?)",
                     (mid, operation, state, detail, time.time()))
        from .activity import activity
        activity(operation, state, mid)

    def metric(self, name, value=1, at=None):
        with self.connect() as db:
            self._metric(db, name, value, at)

    def _metric(self, db, name, value, at=None):
        day = datetime.fromtimestamp(time.time() if at is None else at, self.statistics_timezone).date().isoformat()
        db.execute("INSERT INTO metrics VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=value+excluded.value", (name, value))
        db.execute('''INSERT INTO metrics_daily VALUES(?,?,?,?)
            ON CONFLICT(day,utc_offset,name) DO UPDATE SET value=value+excluded.value''',
            (day, self.statistics_utc_offset, name, value))

    def confirm_source_deleted(self, mid):
        # Idempotent in the same transaction as the byte statistic. A recovery
        # observation cannot double count an already confirmed source deletion.
        with self.connect() as db:
            row = db.execute('SELECT size,source_deleted FROM media WHERE id=?', (mid,)).fetchone()
            if not row:
                raise KeyError('Media not found')
            if not row['source_deleted']:
                db.execute('UPDATE media SET source_deleted=1 WHERE id=?', (mid,))
                self._metric(db, 'source_bytes_released', row['size'])

    @staticmethod
    def playback_statistics(values):
        requests = values.get('play_requests', 0)
        cached = values.get('url_cache_hits', 0)
        resolutions = values.get('redirect_success', 0)
        successes = resolutions + cached
        return {'requests': requests, 'successes': successes, 'errors': values.get('redirect_errors', 0),
                'success_rate': successes / requests if requests else None,
                'error_rate': values.get('redirect_errors', 0) / requests if requests else None,
                'cache_hit_rate': cached / requests if requests else None,
                'mean_resolve_seconds': values.get('resolve_seconds', 0) / resolutions if resolutions else None}

    def save_share(self, mid, code, password, file):
        now = time.time()
        self.execute("INSERT INTO share_objects VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET share_code=excluded.share_code,receive_code=excluded.receive_code,file_id=excluded.file_id,parent_id=excluded.parent_id,sha1=excluded.sha1,size=excluded.size,name=excluded.name,verified_at=NULL,range_verified_at=NULL",
                     (mid, code, password, file.file_id, file.parent_id, file.sha1.upper(), file.size, file.name, now, None, None))

    def backup(self, destination):
        with self.connect() as db, closing(sqlite3.connect(str(destination))) as target:
            db.backup(target)

    def ingest_share(self, file, virtual_path, code, password, title=None):
        """Import share-only media, with no owned personal-drive source to delete."""
        if file.is_dir or file.size < 0 or not file.sha1:
            raise SafetyError("Share import requires file metadata")
        now = time.time()
        with self.connect() as db:
            old = db.execute("SELECT m.* FROM media m JOIN share_objects s ON s.media_id=m.id WHERE s.share_code=? AND s.file_id=?",
                             (code, file.file_id)).fetchone()
            if old:
                if old["sha1"].upper() != file.sha1.upper() or old["size"] != file.size:
                    raise SafetyError("Share file identity changed")
                mid = old["id"]
            else:
                mid = db.execute("INSERT INTO media(token,title,file_name,virtual_path,size,sha1,storage_type,status,source_deleted,created_at,updated_at) VALUES(?,?,?,?,?,?,'SHARE','DISCOVERED',1,?,?)",
                                 (secrets.token_urlsafe(32), title or file.name, file.name, virtual_path, file.size, file.sha1.upper(), now, now)).lastrowid
            db.execute("INSERT INTO share_objects VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET receive_code=excluded.receive_code",
                       (mid, code, password, file.file_id, file.parent_id, file.sha1.upper(), file.size, file.name, now, None, None))
        return self.media(mid)

    def dashboard(self):
        day = datetime.fromtimestamp(time.time(), self.statistics_timezone).date().isoformat()
        metrics = self.all('SELECT * FROM metrics ORDER BY name')
        today = {r['name']: r['value'] for r in self.all('SELECT name,value FROM metrics_daily WHERE day=? AND utc_offset=?',
            (day, self.statistics_utc_offset))}
        return {"storage": self.all("SELECT storage_type,COUNT(*) count,SUM(size) bytes FROM media GROUP BY storage_type"),
                "status": self.all("SELECT status,COUNT(*) count FROM media GROUP BY status"),
                "cache": self.one("SELECT COUNT(*) count,COALESCE(SUM(size),0) bytes FROM cache_objects"),
                "metrics": metrics,
                "today": {'date': day, 'utc_offset_minutes': self.statistics_utc_offset, 'metrics': today,
                          'playback': self.playback_statistics(today)},
                "playback": self.playback_statistics({r['name']: r['value'] for r in metrics}),
                "counts": self.one('''SELECT
                    (SELECT COUNT(*) FROM media WHERE strm_path IS NOT NULL) strm_records,
                    (SELECT COUNT(DISTINCT share_code) FROM share_objects) shares,
                    (SELECT COUNT(*) FROM media WHERE status LIKE 'FAILED_%' OR status='BROKEN') abnormal'''),
                "tasks": self.all("SELECT * FROM tasks ORDER BY id DESC LIMIT 100")}

