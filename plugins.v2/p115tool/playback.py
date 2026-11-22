"""Efficient 302 playback orchestration, per-resource locking, and virtual copy resolution."""
from contextlib import contextmanager
import threading
import time
import uuid
from .models import MissingFile, PLUGIN_VERSION, RemoteError, SafetyError, ToolError
try:
    from app.log import logger
except ModuleNotFoundError:
    import logging
    logger = logging.getLogger('p115tool.playback')

PLAYBACK_GUARD = 24 * 3600
RECEIVE_RECOVERY_COOLDOWN = 30


def _err_code(exc):
    code = getattr(exc, 'upstream_code', None)
    return code if type(code) is int else None


def matches_copy(file, row, parent=None):
    return (not file.is_dir and file.name == row['name'] and file.size == row['size']
            and (not row['sha1'] or file.sha1 == row['sha1'])
            and (parent is None or file.parent_id == parent))


class StorageGate:
    """Reentrant exclusive/shared gate with per-media locking for concurrent virtual playback.

    - Exclusive mode (`with gate:`): used by cleanup, virtualize, reconcile, quality upgrades,
      recycle operations, and shutdown. Reentrant on the same thread.
    - Shared mode (`with gate.shared():` or `with gate.for_media(media_id):`): allows concurrent
      playback of different virtual resources while serializing requests for the same resource
      and coordinating safely with exclusive storage operations.
    """

    def __init__(self):
        self._cond = threading.Condition(threading.Lock())
        self._writer = None
        self._writer_depth = 0
        self._writer_name = None
        self._waiting_writers = 0
        self._readers = 0
        self._reader_depths = {}
        self._media_meta = threading.Lock()
        self._media_locks = {}
        self._media_owners = {}

    def __enter__(self):
        tid = threading.get_ident()
        with self._cond:
            if self._writer == tid:
                self._writer_depth += 1
                return self
            self._waiting_writers += 1
            try:
                while self._writer is not None or self._readers > 0:
                    self._cond.wait()
                self._writer = tid
                self._writer_depth = 1
                self._writer_name = threading.current_thread().name
            finally:
                self._waiting_writers -= 1
        return self

    def __exit__(self, exc_type, exc, tb):
        with self._cond:
            self._writer_depth -= 1
            if self._writer_depth == 0:
                self._writer = None
                self._writer_name = None
                self._cond.notify_all()
        return False

    @contextmanager
    def shared(self, media_id=None):
        tid = threading.get_ident()
        started = time.monotonic()
        logged_wait = False
        with self._cond:
            if self._writer == tid:
                reentered_writer = True
            else:
                reentered_writer = False
                already_reading = self._reader_depths.get(tid, 0) > 0
                while self._writer is not None or (not already_reading and self._waiting_writers > 0):
                    self._cond.wait(timeout=2.0)
                    if not logged_wait and time.monotonic() - started >= 2.0 and (
                            self._writer is not None or (not already_reading and self._waiting_writers > 0)):
                        logged_wait = True
                        logger.warning(
                            f'115播放等待全局门锁：媒体={media_id or "none"}，占用线程={self._writer_name or "waiting_writer"}，'
                            f'读计数={self._readers}，等待写={self._waiting_writers}'
                        )
                self._readers += 1
                self._reader_depths[tid] = self._reader_depths.get(tid, 0) + 1
        try:
            yield
        finally:
            if not reentered_writer:
                with self._cond:
                    self._readers -= 1
                    depth = self._reader_depths[tid] - 1
                    if depth:
                        self._reader_depths[tid] = depth
                    else:
                        del self._reader_depths[tid]
                    if self._readers == 0:
                        self._cond.notify_all()

    @contextmanager
    def for_media(self, media_id):
        with self.shared(media_id=media_id):
            with self._media_meta:
                entry = self._media_locks.get(media_id)
                if entry is None:
                    lock = threading.RLock()
                    self._media_locks[media_id] = [lock, 1]
                else:
                    lock = entry[0]
                    entry[1] += 1
            acquired = False
            try:
                if not lock.acquire(timeout=2.0):
                    owner = self._media_owners.get(media_id, 'unknown')
                    logger.warning(f'115播放等待资源锁：媒体={media_id}，当前占用线程={owner}')
                    lock.acquire()
                acquired = True
                prev_owner = self._media_owners.get(media_id)
                self._media_owners[media_id] = threading.current_thread().name
                try:
                    yield
                finally:
                    if prev_owner is None:
                        self._media_owners.pop(media_id, None)
                    else:
                        self._media_owners[media_id] = prev_owner
            finally:
                if acquired:
                    lock.release()
                with self._media_meta:
                    entry = self._media_locks[media_id]
                    entry[1] -= 1
                    if entry[1] == 0:
                        del self._media_locks[media_id]


def _copy_error_attrs(target, source):
    for key in ('operation', 'sdk_error', 'upstream_code'):
        if hasattr(source, key):
            setattr(target, key, getattr(source, key))


class PlaybackManager:
    """Handles direct actual-file 302 playback and verified virtual temporary-copy playback."""

    def __init__(self, storage):
        self.storage = storage
        self.service = storage.service
        self.client = storage.client
        self.config = storage.config

    def _list_verified_copy_folder(self, copy):
        """List copy folder and verify its identity in one fs_files round-trip when supported."""
        fast_list = getattr(self.client, 'list_verified_folder', None)
        if callable(fast_list):
            try:
                return fast_list(copy['folder_cid'], copy['root_cid'], copy['folder_name'])
            except SafetyError:
                # 115 fs_files falls back to root when a folder was deleted.
                # Call stat() to distinguish MissingFile (recreate) from SafetyError (tampered).
                self.validate_folder(copy)
                raise
        self.validate_folder(copy)
        return list(self.client.list_files(copy['folder_cid']))

    def validate_folder(self, copy):
        folder = self.client.stat(copy['folder_cid'])
        if not folder.is_dir or folder.parent_id != copy['root_cid'] or folder.name != copy['folder_name']:
            raise SafetyError('Temporary directory identity changed')
        return folder

    def locate_copy(self, row, copy):
        self.validate_folder(copy)
        files = list(self.client.list_files(copy['folder_cid']))
        candidates = [f for f in files if matches_copy(f, row, copy['folder_cid'])]
        if len(files) != 1 or len(candidates) != 1 or not candidates[0].pickcode:
            raise SafetyError('Temporary contents not uniquely verified')
        return candidates[0]

    def create_copy_folder(self, media_id, root, abandoned=None):
        if not root:
            raise SafetyError('Temporary directory not configured')
        if str(root) == '0' or self.client.directory_path(root) == '/':
            raise SafetyError('Invalid temporary root')
        name = 'p115tool-' + uuid.uuid4().hex
        # Commit the new identity before creating; an unknown result is never retried.
        with self.service.transaction() as db:
            # Archive and replace atomically: a crash can never discard the old
            # unknown intent or leave it eligible for an automatic resubmission.
            if abandoned is not None:
                db.execute(
                    'INSERT INTO abandoned_resource_copies VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex, media_id, abandoned['root_cid'], abandoned['folder_name'],
                     abandoned['folder_cid'], abandoned['file_id'], abandoned['stage'],
                     abandoned['received_at'], abandoned['lease_until'], time.time()),
                )
            db.execute(
                "INSERT INTO resource_copies(media_id,root_cid,folder_name,stage) VALUES(?,?,?,'FOLDER_CREATING') "
                "ON CONFLICT(media_id) DO UPDATE SET root_cid=excluded.root_cid,folder_name=excluded.folder_name,"
                "folder_cid='',file_id='',received_at=0,lease_until=0,stage='FOLDER_CREATING'",
                (media_id, root, name),
            )
        started = time.monotonic()
        try:
            cid = self.client.create_temp_directory(root, name)
        except Exception:
            self.service.query("UPDATE resource_copies SET stage='FOLDER_UNKNOWN' WHERE media_id=?", (media_id,))
            raise ToolError('Temporary directory outcome unknown') from None
        self.service.query("UPDATE resource_copies SET folder_cid=?,stage='EMPTY' WHERE media_id=?", (cid, media_id))
        logger.info(f'115播放流程：媒体={media_id}，步骤=创建临时目录完成，耗时={(time.monotonic() - started) * 1000:.1f}ms')
        return {
            'media_id': media_id, 'root_cid': root, 'folder_name': name,
            'folder_cid': cid, 'file_id': '', 'stage': 'EMPTY',
            'received_at': 0.0, 'lease_until': 0.0,
        }

    def _resolve_candidate_detail(self, candidate, row, folder_cid, require_stat=False):
        """Return verified RemoteFile with pickcode; skip stat() when listing already suffices."""
        needs_stat = require_stat or not candidate.pickcode or (bool(row['sha1']) and not candidate.sha1)
        if not needs_stat:
            return candidate
        actual = self.client.stat(candidate.file_id)
        if actual.file_id != candidate.file_id or not matches_copy(actual, row, folder_cid):
            raise SafetyError('Temporary identity changed')
        if not actual.pickcode:
            raise SafetyError('Temporary playback code unavailable')
        return actual

    def _resolve_pending_copy(self, media_id, row, copy, recover_unknown, fresh_receive=False):
        """Verify a RECEIVED or RECEIVE_UNKNOWN copy; return (actual_or_None, new_copy_or_None)."""
        try:
            if fresh_receive:
                files = list(self.client.list_files(copy['folder_cid']))
            else:
                try:
                    self.validate_folder(copy)
                except MissingFile:
                    files = []
                else:
                    files = list(self.client.list_files(copy['folder_cid']))
            if not files:
                if copy['stage'] == 'RECEIVED' and time.time() - copy['received_at'] < RECEIVE_RECOVERY_COOLDOWN:
                    raise SafetyError('Temporary transfer awaiting visibility')
                if not recover_unknown:
                    raise SafetyError('Temporary write outcome unresolved')
                recent = self.service.query(
                    'SELECT max(abandoned_at) at FROM abandoned_resource_copies WHERE media_id=?',
                    (media_id,), one=True,
                )
                if recent['at'] is not None and time.time() - recent['at'] < RECEIVE_RECOVERY_COOLDOWN:
                    raise SafetyError('Temporary recovery cooling down')
                if self.storage.verify_share(row) != row['share_fid']:
                    raise SafetyError('Share mapping changed')
                # Recheck after share validation, which may take time.
                try:
                    self.validate_folder(copy)
                except MissingFile:
                    pass
                else:
                    if list(self.client.list_files(copy['folder_cid'])):
                        raise SafetyError('Temporary contents changed')
                if self.service._stop.is_set():
                    raise InterruptedError()
                logger.info(f'115播放流程：媒体={media_id}，步骤=待确认副本目录为空，归档后重建目录，原状态={copy["stage"]}')
                new_copy = self.create_copy_folder(media_id, copy['root_cid'], abandoned=copy)
                return None, new_copy
            if len(files) != 1:
                raise SafetyError('Temporary contents not uniquely verified')
            candidate = files[0]
            if not matches_copy(candidate, row, copy['folder_cid']):
                raise SafetyError('Temporary identity changed')
            # Pre-existing RECEIVED/RECEIVE_UNKNOWN checkpoints verify full detail via stat();
            # a freshly received copy in the same call only needs stat() if listing omitted pickcode/sha1.
            actual = self._resolve_candidate_detail(
                candidate, row, copy['folder_cid'], require_stat=not fresh_receive,
            )
            self.service.query(
                "UPDATE resource_copies SET file_id=?,stage='READY',received_at=? WHERE media_id=?",
                (actual.file_id, time.time(), media_id),
            )
            logger.info(f'115播放流程：媒体={media_id}，步骤=副本核实就绪，原状态={copy["stage"]}')
            return actual, None
        except Exception as exc:
            if self.service._stop.is_set():
                raise InterruptedError() from None
            if copy['stage'] == 'RECEIVED':
                if isinstance(exc, ToolError):
                    exc.copy_state = self.storage.copy(media_id)['stage']
                raise
            message = ('Temporary recovery cooling down'
                       if exc.args == ('Temporary recovery cooling down',)
                       else 'Temporary write outcome unresolved')
            error = SafetyError(message)
            error.copy_state = self.storage.copy(media_id)['stage']
            _copy_error_attrs(error, exc)
            raise error from None

    def _verify_missing_ready_copy(self, copy, row):
        """Before replacing an absent READY copy, verify it was not moved/mutated elsewhere."""
        if not copy['file_id']:
            return
        try:
            actual = self.client.stat(copy['file_id'])
        except (MissingFile, RemoteError):
            return
        if not matches_copy(actual, row, copy['folder_cid']):
            raise SafetyError('Temporary identity changed')

    def ensure_copy(self, media_id, recover_unknown=True):
        with self.storage._gate.for_media(media_id):
            if self.service._stop.is_set():
                raise InterruptedError()
            row = self.storage.row(media_id)
            if not row or not row['share_fid']:
                raise SafetyError('Share mapping unavailable')
            copy = self.storage.copy(media_id)
            logger.info(f'115播放流程：媒体={media_id}，步骤=检查临时副本，副本状态={copy["stage"] if copy else "NONE"}')
            fresh_folder = False
            fresh_receive = False

            while True:
                if self.service._stop.is_set():
                    raise InterruptedError()

                if copy and copy['stage'] in ('RECEIVE_UNKNOWN', 'RECEIVED'):
                    actual, new_copy = self._resolve_pending_copy(
                        media_id, row, copy, recover_unknown, fresh_receive=fresh_receive,
                    )
                    if actual is not None:
                        return actual
                    copy = new_copy
                    fresh_folder = True
                    fresh_receive = False
                    recover_unknown = False
                    continue

                if copy and copy['stage'] == 'READY':
                    started = time.monotonic()
                    try:
                        contents = self._list_verified_copy_folder(copy)
                    except MissingFile:
                        self._verify_missing_ready_copy(copy, row)
                        logger.info(f'115播放流程：媒体={media_id}，步骤=已有副本目录丢失，准备重建转存')
                        copy = self.create_copy_folder(media_id, copy['root_cid'])
                        fresh_folder = True
                        continue
                    if not contents:
                        self._verify_missing_ready_copy(copy, row)
                        logger.info(f'115播放流程：媒体={media_id}，步骤=已有副本文件缺失，准备重新转存')
                        self.service.query(
                            "UPDATE resource_copies SET stage='EMPTY',file_id='',received_at=0,lease_until=0 WHERE media_id=?",
                            (media_id,),
                        )
                        copy = {**copy, 'stage': 'EMPTY', 'file_id': '', 'received_at': 0.0, 'lease_until': 0.0}
                        fresh_folder = True  # Folder was just validated and confirmed empty.
                        continue
                    members = [f for f in contents if f.file_id == copy['file_id']]
                    if len(members) != 1 or not matches_copy(members[0], row, copy['folder_cid']):
                        raise SafetyError('Temporary contents changed')
                    needs_stat = not members[0].pickcode or (bool(row['sha1']) and not members[0].sha1)
                    actual = self._resolve_candidate_detail(members[0], row, copy['folder_cid'], require_stat=False)
                    logger.info(f'115播放流程：媒体={media_id}，步骤=复用已有副本，额外stat={needs_stat}，耗时={(time.monotonic() - started) * 1000:.1f}ms')
                    return actual

                if copy and copy['stage'] in ('FOLDER_UNKNOWN', 'DELETE_UNKNOWN', 'FOLDER_CREATING', 'RECEIVING', 'DELETING'):
                    error = SafetyError('Temporary write outcome unresolved')
                    error.copy_state = copy['stage']
                    raise error

                if not copy:
                    copy = self.create_copy_folder(media_id, self.config.temp_cid)
                    fresh_folder = True

                if not fresh_folder:
                    try:
                        self.validate_folder(copy)
                    except MissingFile:
                        copy = self.create_copy_folder(media_id, copy['root_cid'])
                        fresh_folder = True
                    else:
                        if list(self.client.list_files(copy['folder_cid'])):
                            raise SafetyError('Temporary directory not empty')

                # Verify sharing metadata again immediately before receiving for owned shares.
                if row['own']:
                    members = list(self.client.share_files(row['share_code'], row['password'], '0'))
                    if not any(f.file_id == row['share_fid'] and matches_copy(f, row) for f in members):
                        raise SafetyError('Owned share file changed')
                if self.service._stop.is_set():
                    raise InterruptedError()

                self.service.query("UPDATE resource_copies SET stage='RECEIVING' WHERE media_id=?", (media_id,))
                recv_started = time.monotonic()
                try:
                    self.client.receive_to_temp(row['share_code'], row['password'], row['share_fid'], copy['folder_cid'])
                except Exception as exc:
                    self.service.query("UPDATE resource_copies SET stage='RECEIVE_UNKNOWN' WHERE media_id=?", (media_id,))
                    error = ToolError('Temporary transfer outcome unknown')
                    error.copy_state = 'RECEIVE_UNKNOWN'
                    _copy_error_attrs(error, exc)
                    raise error from None
                logger.info(f'115播放流程：媒体={media_id}，步骤=分享转存完成，耗时={(time.monotonic() - recv_started) * 1000:.1f}ms')

                now = time.time()
                self.service.query("UPDATE resource_copies SET stage='RECEIVED',received_at=? WHERE media_id=?", (now, media_id))
                copy = {**copy, 'stage': 'RECEIVED', 'received_at': now}
                fresh_receive = fresh_folder
                recover_unknown = False

    def play_moviepilot_copy(self, media_id, ua):
        with self.storage._gate.for_media(media_id):
            selection = self.service.query(
                'SELECT selected_id,previous_id FROM virtual_selections WHERE stable_id=?',
                (media_id,), one=True,
            )
            versions = (selection['selected_id'], selection['previous_id']) if selection else (media_id,)
            unavailable = SafetyError('Temporary copy must be transferred in 115 Cookie mode')
            for version_id in dict.fromkeys(versions):
                if self.service._stop.is_set():
                    raise InterruptedError()
                copy = self.storage.copy(version_id)
                logger.info(f'115播放流程：媒体={version_id}，步骤=原生模式核实副本，副本状态={copy["stage"] if copy else "NONE"}')
                if not copy:
                    continue
                unavailable.copy_state = copy['stage']
                if copy['stage'] == 'EMPTY':
                    continue
                if copy['stage'] not in ('READY', 'RECEIVED', 'RECEIVE_UNKNOWN'):
                    error = SafetyError('Temporary write outcome unresolved')
                    error.copy_state = copy['stage']
                    raise error
                row = self.storage.row(version_id)
                if not row or not row['share_fid']:
                    raise SafetyError('Share mapping unavailable')
                try:
                    self.validate_folder(copy)
                    files = list(self.client.list_files(copy['folder_cid']))
                except MissingFile:
                    logger.info(f'115播放流程：媒体={version_id}，步骤=原生模式跳过副本，原因=目录缺失')
                    continue
                if not files:
                    logger.info(f'115播放流程：媒体={version_id}，步骤=原生模式跳过副本，原因=目录为空')
                    continue
                if len(files) != 1:
                    raise SafetyError('Temporary contents not uniquely verified')
                candidate = files[0]
                check_file_id = copy['stage'] == 'READY' or bool(copy['file_id'])
                if (check_file_id and candidate.file_id != copy['file_id']) or not matches_copy(candidate, row, copy['folder_cid']):
                    raise SafetyError('Temporary contents changed')
                try:
                    actual = self._resolve_candidate_detail(candidate, row, copy['folder_cid'], require_stat=False)
                except MissingFile:
                    logger.info(f'115播放流程：媒体={version_id}，步骤=原生模式跳过副本，原因=文件详情缺失')
                    continue
                link_started = time.monotonic()
                try:
                    link = self.client.normal_link(actual.pickcode, ua, file_id=actual.file_id)
                except MissingFile:
                    logger.info(f'115播放流程：媒体={version_id}，步骤=原生模式跳过副本，原因=取链文件缺失')
                    continue
                self.extend_lease(version_id)
                logger.info(f'115播放流程：媒体={version_id}，步骤=原生模式副本取链成功，耗时={(time.monotonic() - link_started) * 1000:.1f}ms')
                return link.url
            raise unavailable

    @staticmethod
    def _direct_from_row(source, respect_selection):
        if not source:
            raise MissingFile('Playback mapping unavailable')
        if source['kind'] is None:
            return source['pickcode']
        if (source['kind'] == 'ACTUAL' and source['stage'] not in ('SOURCE_DELETE_UNKNOWN', 'SOURCE_DELETING')
                and not (respect_selection and source['selected'])):
            return source['pickcode']
        return None

    def direct_pickcode(self, media_id, respect_selection):
        source = self.service.query(
            'SELECT f.pickcode,s.kind,s.stage,EXISTS(SELECT 1 FROM virtual_selections WHERE stable_id=f.file_id) AS selected '
            'FROM strm_files f LEFT JOIN resource_storage s ON s.media_id=f.file_id WHERE f.file_id=?',
            (media_id,), one=True,
        )
        return self._direct_from_row(source, respect_selection)

    def extend_lease(self, media_id):
        # The 302 endpoint cannot observe when the player finishes reading the CDN stream.
        self.service.query(
            'UPDATE resource_copies SET lease_until=max(lease_until,?) WHERE media_id=?',
            (time.time() + PLAYBACK_GUARD, media_id),
        )

    def _fetch_cookie_link(self, media_id, actual, ua):
        # The storage gate is already released; lease_until protects the copy during link lookup.
        started = time.monotonic()
        try:
            url = self.client.normal_link(actual.pickcode, ua).url
            logger.info(f'115播放流程：媒体={media_id}，步骤=副本取链成功，耗时={(time.monotonic() - started) * 1000:.1f}ms')
            return url
        except RemoteError as exc:
            logger.info(f'115播放流程：媒体={media_id}，步骤=副本取链失败触发复核恢复，类型={type(exc).__name__}，上游错误码={_err_code(exc)}')
            # A copy may disappear between validation and link lookup. Recheck
            # live membership before one bounded recovery attempt.
            with self.storage._gate.for_media(media_id):
                restored = self.storage.ensure_copy(media_id, recover_unknown=False)
                if restored.file_id == actual.file_id:
                    if isinstance(exc, MissingFile):
                        raise SafetyError('Playback link unavailable for existing copy') from None
                    raise
                self.extend_lease(media_id)
            retry_started = time.monotonic()
            url = self.client.normal_link(restored.pickcode, ua).url
            logger.info(f'115播放流程：媒体={media_id}，步骤=恢复后副本取链成功，耗时={(time.monotonic() - retry_started) * 1000:.1f}ms')
            return url

    def play(self, media_id, ua, target=None):
        if self.service._stop.is_set():
            raise ToolError('Service stopped')
        native = self.config.use_moviepilot_auth
        mode = 'native' if native else 'cookie'
        link_args = {'file_id': media_id} if native else {}
        pickcode = (self._direct_from_row(target, respect_selection=not native)
                    if target is not None else self.direct_pickcode(media_id, respect_selection=not native))
        kind = target['kind'] if target is not None else 'UNKNOWN'
        stage = target['stage'] if target is not None else 'UNKNOWN'
        logger.info(f'[v{PLUGIN_VERSION}] 115播放流程：媒体={media_id}，模式={mode}，类型={kind or "NONE"}，存储状态={stage or "NONE"}，直链快速路径={pickcode is not None}')
        if pickcode is not None:
            # Actual files can get a link while another resource is transferring.
            # Recheck before returning in case conversion crossed its delete checkpoint.
            started = time.monotonic()
            try:
                location = self.client.normal_link(pickcode, ua, **link_args).url
            except RemoteError as exc:
                logger.info(f'115播放流程：媒体={media_id}，步骤=实际源取链失败，类型={type(exc).__name__}，上游错误码={_err_code(exc)}')
                if self.direct_pickcode(media_id, respect_selection=not native) == pickcode:
                    raise
                logger.info(f'115播放流程：媒体={media_id}，步骤=实际源状态变更，转入虚拟副本流程')
            else:
                if self.direct_pickcode(media_id, respect_selection=not native) == pickcode:
                    logger.info(f'115播放流程：媒体={media_id}，步骤=实际源取链成功，耗时={(time.monotonic() - started) * 1000:.1f}ms')
                    return location
                logger.info(f'115播放流程：媒体={media_id}，步骤=实际源取链后状态变更，转入虚拟副本流程')

        with self.storage._gate.for_media(media_id):
            if self.service._stop.is_set():
                raise ToolError('Service stopped')
            if not native:
                resolved = self.storage.upgrades.playback_source(media_id)
                if resolved != media_id:
                    logger.info(f'115播放流程：媒体={media_id}，步骤=画质选源，目标={resolved}')
                    media_id = resolved
            pickcode = self.direct_pickcode(media_id, respect_selection=False)
            if pickcode is not None:
                direct_args = {'file_id': media_id} if native else {}
                started = time.monotonic()
                url = self.client.normal_link(pickcode, ua, **direct_args).url
                logger.info(f'115播放流程：媒体={media_id}，步骤=锁内实际源取链成功，耗时={(time.monotonic() - started) * 1000:.1f}ms')
                return url
            if native:
                return self.play_moviepilot_copy(media_id, ua)
            actual = self.storage.ensure_copy(media_id)
            self.extend_lease(media_id)

        return self._fetch_cookie_link(media_id, actual, ua)
