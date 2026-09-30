from __future__ import annotations
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path
import hashlib
import json
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit
from .client import P115ClientManager
from .config import Config
from .database import Database
from .models import RemoteFile, RemoteError, MissingFile, SafetyError, ToolError
from .strm import StrmManager, virtual_path


class WorkspaceLock:
    """Lifetime advisory lock: one writer/service for a database and output tree."""
    def __init__(self, directory):
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.file = open(Path(directory) / "service.lock", "a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise SafetyError("Another p115tool service owns this data directory") from None

    def close(self):
        if not self.file.closed:
            self.file.seek(0)
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()


class Service:
    def __init__(self, config, client=None):
        self.config = config if isinstance(config, Config) else Config.from_dict(config)
        self._ownership = WorkspaceLock(self.config.data_dir)
        try:
            self.db = Database(Path(self.config.data_dir) / "media.sqlite3", self.config.statistics_utc_offset)
            self.client = client or P115ClientManager(self.config)
            self.strm = StrmManager(self.config, self.db)
            self._media_locks = [threading.RLock() for _ in range(256)]
            self._maintenance = threading.RLock()
            self._url_lock = threading.RLock()
            self._links = OrderedDict()
            self._gate = threading.BoundedSemaphore(self.config.max_concurrency)
            self._closed = False
            self._health_cursor = 0
            from .account import AccountMonitor
            self.account = AccountMonitor(self)
            from .grouping import ShareGroups
            self.groups = ShareGroups(self)
            from .jobs import DurableJobs
            self.jobs = DurableJobs(self)
        except BaseException:
            self._ownership.close()
            raise

    def close(self):
        # Stop/join before acquiring maintenance: the worker may be using it.
        self.jobs.stop()
        with self._maintenance:
            self._closed = True
            try:
                if isinstance(self.client, P115ClientManager):
                    self.client.close()
                with self._url_lock:
                    self._links.clear()
            finally:
                self._ownership.close()

    def lock(self, mid):
        return self._media_locks[int(mid) % len(self._media_locks)]

    def available(self):
        if self._closed or not self.config.enabled:
            raise ToolError("Plugin is disabled")

    def expected(self, media):
        return RemoteFile("", media.file_name, media.size, media.sha1)

    def normal(self, mid):
        obj = self.db.one("SELECT * FROM normal_objects WHERE media_id=?", (mid,))
        if not obj:
            raise MissingFile("No normal source")
        return obj

    def share(self, mid):
        obj = self.db.one("SELECT * FROM share_objects WHERE media_id=?", (mid,))
        if not obj:
            raise MissingFile("No share source")
        return obj

    def ingest(self, file, path=None, title=None, tmdb_id=None, allow_auto_delete=True, defer_archive=False, media_type=None, season=None):
        self.available()
        path = virtual_path(path or file.path or "/" + file.name)
        if self.config.auto_organize_enabled:
            from .auto_organize import mapped_path
            path=mapped_path(self,file,path)
        with self._maintenance:
            pending = self.db.one("SELECT p.media_id FROM organize_plans p JOIN normal_objects n ON n.media_id=p.media_id WHERE n.file_id=? AND p.state<>'DONE'", (file.file_id,))
            if pending:
                raise SafetyError("Pending organize plan must be reconciled before re-import")
            media = self.db.ingest(file, path, title, tmdb_id)
            recovered = self.db.one('SELECT media_id FROM missing_sources WHERE media_id=?', (media.id,))
            if recovered:
                self.db.execute('DELETE FROM missing_sources WHERE media_id=?', (media.id,))
            if media.status == 'BROKEN' and media.error == 'Source missing (scan confirmed)':
                self.db.transition(media.id, 'DISCOVERED')
                media = self.db.media(media.id)
            self.db.execute('INSERT INTO archive_permissions VALUES(?,?,?) ON CONFLICT(media_id) DO UPDATE SET allow_delete=excluded.allow_delete,requested_at=excluded.requested_at',
                (media.id, int(self.config.auto_delete and allow_auto_delete),time.time()))
            if media_type is not None:
                self.groups.metadata(media.id, media_type, season, tmdb_id)
            if self.config.auto_generate:
                self.strm.generate(media.id)
                # Do not reset a partially completed archive's durable checkpoint.
                if media.status == "DISCOVERED":
                    self.db.transition(media.id, "READY")
            if not defer_archive and self.config.auto_organize_enabled:
                media=self.automatic_organize(media.id)
            if not defer_archive and self.config.storage_policy(media.virtual_path, file.size) == "SHARE":
                if not self.config.share_enabled:
                    raise SafetyError("SHARE policy requires share_enabled")
                self.groups.policy(media.id, allow_delete=self.config.auto_delete and allow_auto_delete)
            return self.db.media(media.id)

    def scan(self, allow_auto_delete=True):
        self.available()
        counts = {"files": 0, "media": 0, "errors": 0, "missing": 0, "removed_strm": 0, "outside": 0}
        imported = []
        snapshots = []
        with self._maintenance:
            for root in self.config.source_cids:
                cid = str(root.get("cid")) if isinstance(root, dict) else str(root)
                prefix = root.get("prefix", "/") if isinstance(root, dict) else "/"
                stack, seen = [(cid, prefix.rstrip("/"))], set()
                observed = set()
                while stack:
                    parent, path = stack.pop()
                    if parent in seen or len(seen) >= 100000:
                        raise SafetyError("Directory cycle or scan limit")
                    seen.add(parent)
                    for file in self.client.list_files(parent):
                        child = path + "/" + file.name
                        if file.is_dir:
                            if file.file_id != str(self.config.cache_cid):
                                stack.append((file.file_id, child))
                        else:
                            observed.add(file.file_id)
                            counts["files"] += 1
                            if Path(file.name).suffix.lower() not in self.config.media_extensions:
                                continue
                            try:
                                media = self.ingest(file, child, allow_auto_delete=allow_auto_delete, defer_archive=True)
                                imported.append(media.id)
                                counts["media"] += 1
                            except (ToolError, ValueError):
                                counts["errors"] += 1
                                self.db.log("scan", "FAILED", detail="File import failed; source untouched")
                snapshots.append((cid, seen, observed))
            # Absence in a listing is not deletion evidence. All roots must have
            # completed before independent authenticated per-file reconciliation.
            from .scanner import reconcile_sources
            reconcile_sources(self, snapshots, counts)
            if self.config.auto_organize_enabled:
                ready=[]
                for mid in dict.fromkeys(imported):
                    try:
                        self.automatic_organize(mid)
                        ready.append(mid)
                    except (ToolError,ValueError,OSError):
                        counts['errors']+=1
                        self.db.log('scan_organize','FAILED',mid,'Automatic organize incomplete; archive not submitted')
                imported=ready
            # Archive only after enumeration succeeds, so all known season/movie
            # files are ingested before the immutable member snapshot is made.
            processed = set()
            for mid in imported:
                key = self.groups.key(mid)
                if key in processed:
                    continue
                processed.add(key)
                try:
                    self.groups.policy(mid, allow_delete=allow_auto_delete)
                except (ToolError, ValueError):
                    counts['errors'] += 1
                    self.db.log('scan_archive','FAILED',mid,'Group or file archive failed')
            self.db.log("scan", "DONE", detail=json.dumps(counts))
        return counts

    def verify_share(self, mid, deep=False, ua="p115tool/1.0"):
        self.available()
        with self._maintenance, self.lock(mid):
            media, share = self.db.media(mid), self.share(mid)
            if not re.fullmatch(r"[A-Fa-f0-9]{40}", media.sha1):
                raise SafetyError("A full SHA1 is required for virtual-share verification")
            file = self.client.find_share_file(share["share_code"], share["receive_code"], self.expected(media))
            self.db.execute("UPDATE share_objects SET file_id=?,parent_id=?,verified_at=? WHERE media_id=?",
                            (file.file_id, file.parent_id, time.time(), mid))
            self.invalidate(mid)
            if deep:
                link = self.client.share_link(self.share(mid), ua)
                self.client.probe_range(link, ua, media.size)
                self.db.execute("UPDATE share_objects SET range_verified_at=? WHERE media_id=?", (time.time(), mid))
            return file

    def attach_share(self, mid, code, password):
        self.available()
        if not self.config.share_enabled:
            raise SafetyError("Share backend is disabled")
        if not re.fullmatch(r"[A-Za-z0-9]+", code) or not re.fullmatch(r"[A-Za-z0-9]{4}", password):
            raise ValueError("Invalid share code/password")
        with self._maintenance, self.lock(mid):
            media = self.db.media(mid)
            if media.source_deleted:
                raise SafetyError("Replacing a virtual source requires manual recovery")
            file = self.client.find_share_file(code, password, self.expected(media))
            self.db.save_share(mid, code, password, file)
            self.verify_share(mid, deep=True)
            self.strm.generate(mid)
            self.db.transition(mid, "READY", storage="SHARE")
            return self.db.media(mid)

    def import_share(self, code, password, file_id, path, title=None):
        self.available()
        if not self.config.share_enabled:
            raise SafetyError("Share backend is disabled")
        if not re.fullmatch(r"[A-Za-z0-9]+", code) or not re.fullmatch(r"[A-Za-z0-9]{4}", password):
            raise ValueError("Invalid share code/password")
        with self._maintenance:
            stack, seen, selected = ["0"], set(), []
            while stack:
                cid = stack.pop()
                if cid in seen or len(seen) > 10000:
                    raise SafetyError("Share traversal cycle/limit")
                seen.add(cid)
                for file in self.client.share_files(code, password, cid):
                    if file.is_dir:
                        stack.append(file.file_id)
                    elif file.file_id == file_id:
                        selected.append(file)
            if len(selected) != 1 or not re.fullmatch(r"[A-Fa-f0-9]{40}", selected[0].sha1):
                raise SafetyError("Share import requires one file with a full SHA1")
            media = self.db.ingest_share(selected[0], virtual_path(path), code, password, title)
            with self.lock(media.id):
                self.verify_share(media.id, deep=True)
                self.strm.generate(media.id)
                self.db.transition(media.id, "READY", storage="SHARE")
            return self.db.media(media.id)

    def archive(self, mid, delete=False):
        self.available()
        if not self.config.share_enabled:
            raise SafetyError("Virtual-share backend is disabled")
        # Global maintenance lock serializes archive/cache deletes with scanning.
        with self._maintenance, self.lock(mid):
            media = self.db.media(mid)
            group = self.groups.membership(mid)
            if group and group['state'] != 'READY':
                raise SafetyError('Pending share group requires group reconciliation')
            if self.db.one("SELECT media_id FROM organize_plans WHERE media_id=? AND state<>'DONE'", (mid,)):
                raise SafetyError("Pending organize plan must be reconciled before archive")
            if media.source_deleted:
                if media.status == "READY":
                    return media
                raise SafetyError("Virtual media requires recovery before re-archiving")
            if media.status == "SOURCE_DELETING" or media.status == "FAILED_DELETE":
                raise SafetyError("Previous deletion outcome needs explicit reconciliation")
            stage = "FAILED_SHARE"
            try:
                normal = self.normal(mid)
                source = self.client.stat(normal["file_id"])
                if not self.expected(media).matches(source):
                    raise SafetyError("Source identity changed before archive")
                if not re.fullmatch(r"[A-Fa-f0-9]{40}", media.sha1):
                    raise SafetyError("Missing full SHA1; source deletion is prohibited")
                share = self.db.one("SELECT * FROM share_objects WHERE media_id=?", (mid,))
                if not share:
                    if media.status in ("SHARE_CREATING", "FAILED_SHARE"):
                        raise SafetyError("Uncertain share creation: attach existing share or explicitly reset")
                    self.db.transition(mid, "ORGANIZED")
                    self.db.transition(mid, "SHARE_CREATING")
                    code, password = self.client.create_share(source.file_id)
                    # Persist identifiers immediately; matching/verifying is resumable.
                    self.db.save_share(mid, code, password, source)
                    self.db.metric('shares_created')
                    self.db.transition(mid, "SHARED")
                stage = "FAILED_VERIFY"
                self.client.ensure_share_retention(self.share(mid)['share_code'])
                self.db.transition(mid, "VERIFYING")
                self.verify_share(mid, deep=True)
                self.db.transition(mid, "VERIFIED")
                stage = "FAILED_STRM"
                self.strm.generate(mid)
                if not self.strm.verify(mid):
                    raise SafetyError("STRM verification failed")
                self.db.transition(mid, "STRM_CREATED", storage="SHARE")
                # Manual deletion requires request confirmation. Automatic deletion
                # additionally requires the independent auto_delete configuration.
                if delete:
                    stage = "FAILED_DELETE"
                    if not self.config.delete_source:
                        raise SafetyError("Source deletion is disabled in configuration")
                    self._delete_source(mid)
                self.db.transition(mid, "READY", storage="SHARE")
                return self.db.media(mid)
            except Exception as exc:
                self.db.transition(mid, stage, type(exc).__name__ + ": operation failed; inspect task state")
                raise

    def _delete_source(self, mid, group_id=None):
        media = self.db.media(mid)
        group = self.groups.membership(mid)
        if group:
            if group['state'] != 'READY' and not (group_id == group['id'] and group['state'] == 'DELETING'):
                raise SafetyError('Group deletion barrier is not satisfied')
            if group_id is None:
                # Single-media deletion must respect the group's all-member gate.
                # The group batch already crossed it and each item is rechecked
                # immediately below; avoid quadratic Range probes for a season.
                self.groups.verify_all(group['id'])
        normal = self.normal(mid)
        # Re-read everything immediately before the destructive boundary.
        source = self.client.stat(normal["file_id"])
        if not self.expected(media).matches(source):
            raise SafetyError("Source changed before deletion")
        self.verify_share(mid, deep=True)
        if not self.strm.verify(mid):
            raise SafetyError("STRM no longer matches its stable playback URL")
        self.db.transition(mid, "SOURCE_DELETING")
        # A failed/timeout result MUST NOT trigger an automatic deletion retry.
        from .recycle import delete
        delete(self, mid, 'SOURCE', source)
        self.db.confirm_source_deleted(mid)
        self.invalidate(mid)

    def reconcile_delete(self, mid):
        self.available()
        with self._maintenance, self.lock(mid):
            media = self.db.media(mid)
            if media.status not in ("SOURCE_DELETING", "FAILED_DELETE"):
                raise SafetyError("Media has no uncertain deletion")
            try:
                source = self.client.stat(self.normal(mid)["file_id"])
            except MissingFile:
                self.verify_share(mid, deep=True)
                if not self.strm.verify(mid):
                    raise SafetyError("Cannot confirm playable virtual media")
                self.db.confirm_source_deleted(mid)
                from .recycle import observe
                observe(self, mid, 'SOURCE', self.normal(mid)['file_id'], True)
                self.db.transition(mid, "READY", storage="SHARE")
                return {"source_deleted": True}
            if not self.expected(media).matches(source):
                raise SafetyError("Source identity is ambiguous")
            from .recycle import observe
            observe(self, mid, 'SOURCE', source.file_id, False, source)
            self.db.transition(mid, "STRM_CREATED", storage="SHARE")
            return {"source_deleted": False}

    def invalidate(self, mid):
        with self._url_lock:
            for key in list(self._links):
                if key[0] == mid:
                    del self._links[key]

    def _remember(self, key, link):
        ttl = self.config.url_cache_ttl
        expiry = parse_qs(urlsplit(link.url).query).get("t", [None])[0]
        if expiry:
            try:
                ttl = min(ttl, max(0, float(expiry) - time.time() - 30))
            except ValueError:
                ttl = 0
        with self._url_lock:
            self._links[key] = (time.monotonic() + ttl, link)
            self._links.move_to_end(key)
            while len(self._links) > 2048:
                self._links.popitem(last=False)

    def playback(self, token, ua):
        self.available()
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", token):
            raise KeyError("Media not found")
        if len(ua) > 1024 or any(ord(c) < 32 for c in ua):
            raise ValueError("Invalid User-Agent")
        media = self.db.media(token=token)
        key = (media.id, hashlib.sha256(ua.encode()).hexdigest())
        start = time.monotonic()
        requested_at = time.time()
        self.db.metric("play_requests", at=requested_at)
        # Cache operations acquire maintenance before media locks, everywhere.
        with self._gate, self._maintenance, self.lock(media.id):
            self.available()  # A request may have waited while reload drained.
            try:
                self.touch(media.id)
                with self._url_lock:
                    cached = self._links.get(key)
                if cached and cached[0] > time.monotonic():
                    self.db.metric("url_cache_hits", at=requested_at)
                    return cached[1]
                media = self.db.media(media.id)
                if media.storage_type == "NORMAL":
                    link = self.client.normal_link(self.normal(media.id)["pickcode"], ua)
                else:
                    try:
                        link = self.client.share_link(self.share(media.id), ua)
                        if media.storage_type == "CACHE":
                            self.db.execute("UPDATE media SET storage_type='SHARE' WHERE id=?", (media.id,))
                    except ToolError:
                        try:
                            self.client.refresh()
                            self.verify_share(media.id)
                            link = self.client.share_link(self.share(media.id), ua)
                        except ToolError:
                            if not media.source_deleted:
                                try:
                                    source = self.client.stat(self.normal(media.id)["file_id"])
                                    if not self.expected(media).matches(source):
                                        raise SafetyError("Original source identity changed")
                                    link = self.client.normal_link(source.pickcode, ua)
                                except ToolError:
                                    obj = self.restore(media.id)
                                    link = self.client.normal_link(obj["pickcode"], ua)
                            else:
                                obj = self.restore(media.id)
                                link = self.client.normal_link(obj["pickcode"], ua)
                self.client.validate_url(link.url)
                self._remember(key, link)
                self.db.metric("redirect_success", at=requested_at)
                self.db.metric("resolve_seconds", time.monotonic() - start, at=requested_at)
                return link
            except Exception:
                self.db.metric("redirect_errors", at=requested_at)
                self.db.log("playback", "FAILED", media.id, "No safe playable source")
                raise

    def touch(self, mid):
        now = time.time()
        self.db.execute("UPDATE cache_objects SET last_access_at=?,expire_at=?,lease_until=MAX(lease_until,?) WHERE media_id=?",
                        (now, now + self.config.cache_ttl, now + self.config.playback_lease, mid))

    def restore(self, mid, reconcile_only=False):
        self.available()
        if not self.config.cache_cid or str(self.config.cache_cid) == "0":
            raise SafetyError("An explicit non-root cache_cid is required")
        with self._maintenance, self.lock(mid):
            media = self.db.media(mid)
            existing = self.db.one("SELECT * FROM cache_objects WHERE media_id=?", (mid,))
            if existing:
                if existing["state"] != "READY":
                    raise SafetyError("Cache deletion outcome is uncertain")
                file = self.client.stat(existing["file_id"])
                if not self.expected(media).matches(file) or file.parent_id != existing["parent_id"]:
                    raise SafetyError("Cached file identity changed")
                self.touch(mid)
                return self.db.one("SELECT * FROM cache_objects WHERE media_id=?", (mid,))
            if not reconcile_only:
                self.cleanup(reserve=media.size)
            from .recovery import cache_folder
            folder = cache_folder(self, mid, reconcile_only=reconcile_only)
            # Recover an interrupted transfer by scanning our dedicated folder first.
            rows = list(self.client.list_files(folder["folder_id"]))
            matches = [f for f in rows if self.expected(media).matches(f)]
            intent = self.db.one("SELECT value FROM settings WHERE name=?", (f"restore:{mid}",))
            if not matches:
                if reconcile_only:
                    raise SafetyError('No restored file observed; no remote write attempted')
                if intent:
                    raise SafetyError("Uncertain share_receive; inspect cache folder before retry")
                self.db.execute("INSERT INTO settings VALUES(?,?)", (f"restore:{mid}", "PENDING"))
                self.client.restore(self.share(mid), folder["folder_id"])
                # Eventual consistency: only reads are retried, never share_receive.
                for attempt in range(3):
                    matches = [f for f in self.client.list_files(folder["folder_id"]) if self.expected(media).matches(f)]
                    if matches:
                        break
                    time.sleep(0.5 * (attempt + 1))
            if len(matches) != 1 or not matches[0].pickcode:
                raise SafetyError("Restore must produce exactly one matching playable file")
            file, now = matches[0], time.time()
            if file.parent_id != folder["folder_id"]:
                raise SafetyError("Restored file is outside owned cache folder")
            with self.db.connect() as db:
                db.execute("INSERT INTO cache_objects VALUES(?,?,?,?,?,?,?,?,?,?,'READY')",
                           (mid, file.file_id, file.pickcode, file.parent_id, file.sha1, file.size, now, now,
                            now + self.config.cache_ttl, now + self.config.playback_lease))
                db.execute("DELETE FROM settings WHERE name=?", (f"restore:{mid}",))
                db.execute("UPDATE media SET storage_type='CACHE' WHERE id=?", (mid,))
            self.db.log("restore", "DONE", mid)
            self.invalidate(mid)
            return self.db.one("SELECT * FROM cache_objects WHERE media_id=?", (mid,))

    def delete_cache(self, mid):
        self.available()
        with self._maintenance, self.lock(mid):
            obj = self.db.one("SELECT * FROM cache_objects WHERE media_id=?", (mid,))
            if not obj:
                return False
            if obj["state"] != "READY":
                raise SafetyError("Uncertain cache deletion requires manual reconciliation")
            if obj["lease_until"] > time.time():
                raise SafetyError("Cache has an active playback protection lease")
            media = self.db.media(mid)
            # Never delete a cache which is the only known healthy source.
            self.verify_share(mid, deep=True)
            file = self.client.stat(obj["file_id"])
            if not self.expected(media).matches(file) or file.parent_id != obj["parent_id"]:
                raise SafetyError("Refusing to delete an unowned/changed cached file")
            self.db.execute("UPDATE cache_objects SET state='DELETING' WHERE media_id=?", (mid,))
            from .recycle import delete
            delete(self, mid, 'CACHE', file)
            self.db.execute("DELETE FROM cache_objects WHERE media_id=?", (mid,))
            self.db.execute("UPDATE media SET storage_type='SHARE' WHERE id=? AND storage_type='CACHE'", (mid,))
            self.invalidate(mid)
            self.db.log("cache_delete", "DONE", mid)
            return True

    def cleanup(self, reserve=0):
        self.available()
        with self._maintenance:
            self.db.execute('DELETE FROM emby_sessions WHERE updated_at<?',(time.time()-2*self.config.playback_lease,))
            rows = self.db.all("SELECT * FROM cache_objects ORDER BY last_access_at")
            total = sum(r["size"] for r in rows)
            removed, skipped = [], []
            if reserve > self.config.cache_max_bytes:
                raise SafetyError("Media exceeds cache capacity")
            for obj in rows:
                if obj["expire_at"] > time.time() and total + reserve <= self.config.cache_max_bytes:
                    continue
                try:
                    if self.delete_cache(obj["media_id"]):
                        total -= obj["size"]
                        removed.append(obj["media_id"])
                except ToolError:
                    skipped.append(obj["media_id"])
            if total + reserve > self.config.cache_max_bytes:
                raise SafetyError("Cache capacity cannot be freed safely; active/broken sources retained")
            return {"removed": removed, "protected": skipped, "bytes": total}

    def health(self, deep=False):
        self.available()
        with self._maintenance:
            self.account.snapshot(refresh=True)
            rows = self.db.all("SELECT id FROM media WHERE id>? ORDER BY id LIMIT ?", (self._health_cursor, self.config.health_batch))
            if not rows:
                self._health_cursor = 0
                rows = self.db.all("SELECT id FROM media ORDER BY id LIMIT ?", (self.config.health_batch,))
            result = {"checked": 0, "healthy": 0, "broken": []}
            for row in rows:
                mid = row["id"]
                self._health_cursor = mid
                with self.lock(mid):
                    media = self.db.media(mid)
                    try:
                        if media.storage_type != "NORMAL":
                            try:
                                self.verify_share(mid, deep)
                            except ToolError:
                                if not self.config.auto_repair_share or media.status not in ('READY','BROKEN'):
                                    raise
                                group = self.groups.membership(mid)
                                if group:
                                    self.repair_group(group['id'])
                                else:
                                    self.repair_share(mid)
                        else:
                            source = self.client.stat(self.normal(mid)["file_id"])
                            if not self.expected(media).matches(source):
                                raise SafetyError("Normal source identity changed")
                        self.strm.repair(mid)
                        if media.status == "BROKEN":
                            self.db.transition(mid, "READY")
                        result["healthy"] += 1
                    except (ToolError, OSError):
                        # Never erase deletion/share-creation recovery checkpoints.
                        if media.status in ("READY", "BROKEN"):
                            self.db.transition(mid, "BROKEN", "Health check failed")
                        result["broken"].append(mid)
                    result["checked"] += 1
            self.db.log("health", "DONE", detail=json.dumps(result))
            return result

    def repair_share(self, mid, attach=None):
        self.available()
        with self._maintenance, self.lock(mid):
            from .share_repair import repair
            return repair(self, mid, attach=attach)

    def repair_group(self, gid, attach=None):
        from .group_repair import repair_group
        return repair_group(self, gid, attach=attach)

    def automatic_organize(self,mid,resume=False,recognizer=None):
        from .auto_organize import execute
        return execute(self,mid,resume=resume,recognizer=recognizer)

    def automatic_organize_status(self,mid):
        self.db.media(mid)
        row=self.db.one('SELECT value FROM settings WHERE name=?',(f'auto_organize:{mid}',))
        if not row:
            return {'media_id':mid,'state':'NOT_PLANNED'}
        plan=json.loads(row['value'])
        remote=self.db.one('SELECT state FROM organize_plans WHERE media_id=?',(mid,))
        return {'media_id':mid,'state':plan['state'],'root_cid':plan['root'],'virtual_path':plan['target']['virtual_path'],
            'parent_id':plan.get('parent_id'),'remote_state':remote['state'] if remote else None}

    def organize(self, mid, parent_id, name, path):
        self.available()
        path = virtual_path(path)
        virtual_path("/" + name)
        if "/" in name or not str(parent_id).isdigit():
            raise ValueError("Invalid organize destination")
        if Path(path).name != name:
            raise ValueError("Virtual filename must match organized filename")
        with self._maintenance, self.lock(mid):
            media = self.db.media(mid)
            if media.source_deleted or media.storage_type != "NORMAL":
                raise SafetyError("Only normal, non-deleted sources may be organized")
            plan = self.db.one("SELECT * FROM organize_plans WHERE media_id=?", (mid,))
            if plan and plan["state"] != "DONE":
                raise SafetyError("A pending organize plan requires reconciliation")
            obj = self.normal(mid)
            source = self.client.stat(obj["file_id"])
            if not self.expected(media).matches(source):
                raise SafetyError("Source identity changed")
            if Path(source.name).suffix.lower() != Path(name).suffix.lower():
                raise SafetyError("115 organize cannot change the media extension")
            if self.db.one("SELECT id FROM media WHERE virtual_path=? AND id<>?", (path, mid)):
                raise SafetyError("Virtual destination already exists")
            # fs_move uses the OLD filename; checking just the rename target can
            # otherwise overwrite an unrelated file before rename even begins.
            self._organize_collision(source, str(parent_id), name)
            self.db.execute("INSERT INTO organize_plans VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET file_id=excluded.file_id,old_parent=excluded.old_parent,old_name=excluded.old_name,new_parent=excluded.new_parent,new_name=excluded.new_name,virtual_path=excluded.virtual_path,sha1=excluded.sha1,size=excluded.size,state=excluded.state,updated_at=excluded.updated_at",
                            (mid, source.file_id, source.parent_id, source.name, str(parent_id), name, path,
                             source.sha1, source.size, "PLANNED", time.time()))
            self.db.log("organize", "PLANNED", mid)
            self._continue_organize(mid)
            return self.db.media(mid)

    def _organize_collision(self, source, parent_id, name):
        siblings = list(self.client.list_files(parent_id))
        if any(f.name in (source.name, name) and f.file_id != source.file_id for f in siblings):
            raise SafetyError("Remote destination already contains source or target name")

    def _organize_state(self, mid, state):
        self.db.execute("UPDATE organize_plans SET state=?,updated_at=? WHERE media_id=?", (state, time.time(), mid))
        self.db.log("organize", state, mid)

    def _organize_current(self, plan):
        file = self.client.stat(plan["file_id"])
        if (file.is_dir or file.sha1.upper() != plan["sha1"].upper() or file.size != plan["size"]
                or file.parent_id not in (plan["old_parent"], plan["new_parent"])
                or file.name not in (plan["old_name"], plan["new_name"])):
            raise SafetyError("Remote organize identity changed outside the recorded plan")
        return file

    def _continue_organize(self, mid):
        plan = self.db.one("SELECT * FROM organize_plans WHERE media_id=?", (mid,))
        source = self._organize_current(plan)
        self._organize_collision(source, plan["new_parent"], plan["new_name"])
        if source.parent_id != plan["new_parent"]:
            self._organize_state(mid, "MOVING")
            self.client.move(source.file_id, plan["new_parent"])
            source = self._organize_current(plan)
            if source.parent_id != plan["new_parent"]:
                raise SafetyError("Move result does not match organize plan")
        self._organize_state(mid, "MOVED")
        if source.name != plan["new_name"]:
            self._organize_state(mid, "RENAMING")
            self.client.rename(source.file_id, plan["new_name"])
            source = self._organize_current(plan)
            if source.name != plan["new_name"]:
                raise SafetyError("Rename result does not match organize plan")
        self._finish_organize(mid, plan, source)

    def _finish_organize(self, mid, plan, source):
        if source.parent_id != plan["new_parent"] or source.name != plan["new_name"]:
            raise SafetyError("Organize is not complete")
        now = time.time()
        with self.db.connect() as db:
            db.execute("UPDATE media SET file_name=?,virtual_path=?,updated_at=? WHERE id=?",
                       (plan["new_name"], plan["virtual_path"], now, mid))
            db.execute("UPDATE normal_objects SET parent_id=?,path=? WHERE media_id=?",
                       (plan["new_parent"], plan["virtual_path"], mid))
            if source.pickcode:
                db.execute("UPDATE normal_objects SET pickcode=? WHERE media_id=?", (source.pickcode, mid))
            db.execute("UPDATE organize_plans SET state='MAPPED',updated_at=? WHERE media_id=?", (now, mid))
        self.strm.generate(mid)
        self._organize_state(mid, "DONE")
        self.db.transition(mid, "READY")
        self.invalidate(mid)

    def reconcile_organize(self, mid, resume=False):
        self.available()
        with self._maintenance, self.lock(mid):
            plan = self.db.one("SELECT * FROM organize_plans WHERE media_id=?", (mid,))
            if not plan:
                raise SafetyError("No organize plan exists")
            source = self._organize_current(plan)
            if source.parent_id == plan["new_parent"] and source.name == plan["new_name"]:
                self._finish_organize(mid, plan, source)
            elif resume:
                # Only explicit user confirmation can authorize remaining writes.
                self._continue_organize(mid)
            else:
                observed = "MOVED" if source.parent_id == plan["new_parent"] else "PLANNED"
                self._organize_state(mid, observed)
            return self.db.one("SELECT media_id,state,updated_at FROM organize_plans WHERE media_id=?", (mid,))

    def reconcile_cache_delete(self, mid):
        self.available()
        with self._maintenance, self.lock(mid):
            obj = self.db.one("SELECT * FROM cache_objects WHERE media_id=?", (mid,))
            if not obj or obj["state"] != "DELETING":
                raise SafetyError("No uncertain cache deletion exists")
            try:
                file = self.client.stat(obj["file_id"])
            except MissingFile:
                self.verify_share(mid, deep=True)
                from .recycle import observe
                observe(self, mid, 'CACHE', obj['file_id'], True)
                with self.db.connect() as db:
                    db.execute("DELETE FROM cache_objects WHERE media_id=?", (mid,))
                    db.execute("UPDATE media SET storage_type='SHARE' WHERE id=? AND storage_type='CACHE'", (mid,))
                self.invalidate(mid)
                self.db.log("cache_reconcile", "DELETED", mid)
                return {"deleted": True}
            if not self.expected(self.db.media(mid)).matches(file) or file.parent_id != obj["parent_id"]:
                raise SafetyError("Cache identity changed; cannot reconcile")
            from .recycle import observe
            observe(self, mid, 'CACHE', file.file_id, False, file)
            self.db.execute("UPDATE cache_objects SET state='READY' WHERE media_id=?", (mid,))
            self.db.log("cache_reconcile", "RETAINED", mid)
            return {"deleted": False}
