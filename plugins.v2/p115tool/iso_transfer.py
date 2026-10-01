"""Scoped MoviePilot compatibility for season ISO images without episode numbers.

Keep host recognition, naming templates, storage operators and transfer events.
Do not enter the host's special-extra-file success-without-transfer branch.
"""
import inspect
from pathlib import Path
import threading
import weakref
from .strm import safe_parts

_lock = threading.RLock()
_services = weakref.WeakSet()
_handler = None
_original = None
_wrapper = None


def register(service, roots, target):
    global _handler, _original, _wrapper
    if not callable(getattr(service, 'query', None)):
        return  # Offline host mocks do not install process-wide compatibility.
    from app.modules.filemanager.transhandler import TransHandler
    with _lock:
        if _handler is None:
            original = TransHandler.transfer_media
            signature = inspect.signature(original)
            def wrapped(self, *args, **kwargs):
                bound = signature.bind(self, *args, **kwargs)
                bound.apply_defaults()
                data = dict(bound.arguments)
                data.pop('self', None)
                item, meta, media = (data.get(k) for k in ('fileitem', 'in_meta', 'mediainfo'))
                from app.schemas.types import MediaType
                if (item and meta and media and item.storage == 'u115' and item.type == 'file'
                        and str(item.extension).lower() == 'iso' and media.type == MediaType.TV
                        and meta.begin_episode is None and data.get('transfer_type') == 'move'
                        and data.get('target_storage') == 'u115' and not data.get('preview')):
                    with _lock:
                        services = list(_services)
                    for current in services:
                        scope = matching_scope(current, item.path, data.get('target_path'))
                        if scope:
                            return transfer_iso(current, self, data, scope)
                return original(self, *args, **kwargs)
            _handler, _original, _wrapper = TransHandler, original, wrapped
            TransHandler.transfer_media = wrapped
        elif _handler is not TransHandler or TransHandler.transfer_media is not _wrapper:
            raise RuntimeError('Host compatibility hook changed')
        scopes = list(getattr(service, '_iso_scopes', []))
        for source in roots:
            pair = (dict(source), dict(target))
            if pair not in scopes:
                scopes.append(pair)
        service._iso_scopes = scopes
        _services.add(service)


def unregister(service):
    global _handler, _original, _wrapper
    with _lock:
        _services.discard(service)
        service._iso_scopes = []
        if not _services and _handler:
            if _handler.transfer_media is _wrapper:
                _handler.transfer_media = _original
            _handler = _original = _wrapper = None


def matching_scope(service, source_path, destination):
    if getattr(service, '_closed', False) or destination is None:
        return None
    source_path, destination = Path(source_path), Path(destination)
    for source, target in getattr(service, '_iso_scopes', []):
        root, library = Path(source['prefix']), Path(target['prefix'])
        if source_path != root and source_path.is_relative_to(root) and destination.is_relative_to(library):
            return source, target
    return None


def same(item, expected):
    return (item and item.type == expected.type and item.storage == expected.storage
            and str(item.fileid) == str(expected.fileid) and item.path == expected.path
            and item.name == expected.name and item.size == expected.size)


def transfer_iso(service, handler, data, scope):
    from app.schemas import TransferInfo
    from app.helper.directory import DirectoryHelper
    from app.core.config import settings
    item, meta, media = data['fileitem'], data['in_meta'], data['mediainfo']
    def failure(code):
        return TransferInfo(success=False, fileitem=item, transfer_type='move', message=code)
    # Serialize the checkpoint and write with plugin shutdown and other writes.
    with service._lock:
        try:
            if getattr(service, '_closed', False):
                return failure('ISO_SERVICE_STOPPED')
            service.query('''CREATE TABLE IF NOT EXISTS iso_move_checkpoints (
                file_id TEXT PRIMARY KEY, source_path TEXT NOT NULL,
                target_path TEXT NOT NULL, size INTEGER NOT NULL, state TEXT NOT NULL)''')
            prior = service.query('SELECT * FROM iso_move_checkpoints WHERE file_id=?', (str(item.fileid),), one=True)
            if prior:
                return failure('ISO_MOVE_CHECKPOINT_EXISTS')
            source, target = scope
            source_oper, target_oper = data['source_oper'], data['target_oper']
            if (not callable(getattr(source_oper, 'get_item_strict', None))
                    or not callable(getattr(target_oper, 'get_item_strict', None))
                    or not callable(getattr(handler, '_TransHandler__transfer_file', None))):
                return failure('ISO_HOST_INTERFACE_UNAVAILABLE')
            for operator, directory in ((source_oper, source), (target_oper, target)):
                root_item = operator.get_item_strict(Path(directory['prefix']))
                if (not root_item or root_item.type != 'dir' or root_item.storage != 'u115'
                        or str(root_item.fileid) != directory['cid']
                        or Path(root_item.path) != Path(directory['prefix'])):
                    return failure('ISO_DIRECTORY_IDENTITY_CHANGED')
            if not same(source_oper.get_item_strict(Path(item.path)), item):
                return failure('ISO_SOURCE_IDENTITY_CHANGED')
            if not str(item.fileid).isdigit() or type(item.size) is not int or item.size <= 0:
                return failure('ISO_SOURCE_IDENTITY_INVALID')
            season = meta.begin_season
            if type(season) is not int or not 0 <= season <= 999:
                return failure('ISO_SEASON_REQUIRED')
            safe_parts(item.name)
            if Path(item.name).name != item.name or not item.name.lower().endswith('.iso'):
                return failure('ISO_SOURCE_NAME_INVALID')
            base = Path(data['target_path'])
            rendered = handler.get_rename_path(
                path=base, template_string=settings.RENAME_FORMAT(media.type),
                rename_dict=handler.get_naming_dict(meta=meta, mediainfo=media, file_ext='.iso'),
                source_path=item.path, source_item=item)
            series = DirectoryHelper.get_media_root_path(settings.RENAME_FORMAT(media.type), rename_path=rendered)
            if not series or series == base or not series.is_relative_to(base):
                return failure('ISO_SERIES_TEMPLATE_INVALID')
            destination = series / f'Season {season:02d}' / item.name
            safe_parts(destination.relative_to(Path(target['prefix'])).as_posix())
            if destination == Path(item.path) or target_oper.get_item_strict(destination):
                return failure('ISO_TARGET_EXISTS')
            # Both directory creation and moving can have unknown write outcomes.
            service.query('INSERT INTO iso_move_checkpoints VALUES(?,?,?,?,?)',
                          (str(item.fileid), item.path, destination.as_posix(), item.size, 'PREPARED'))
            folder = target_oper.get_folder(destination.parent)
            if not folder or folder.type != 'dir' or folder.storage != 'u115' or Path(folder.path) != destination.parent:
                return failure('ISO_FOLDER_UNVERIFIED')
            confirmed_folder = target_oper.get_item_strict(destination.parent)
            if (not confirmed_folder or confirmed_folder.type != 'dir' or confirmed_folder.storage != 'u115'
                    or Path(confirmed_folder.path) != destination.parent
                    or str(confirmed_folder.fileid) != str(folder.fileid)):
                return failure('ISO_FOLDER_UNVERIFIED')
            for operator, directory in ((source_oper, source), (target_oper, target)):
                root_item = operator.get_item_strict(Path(directory['prefix']))
                if (not root_item or root_item.type != 'dir' or root_item.storage != 'u115'
                        or str(root_item.fileid) != directory['cid']
                        or Path(root_item.path) != Path(directory['prefix'])):
                    return failure('ISO_DIRECTORY_IDENTITY_CHANGED')
            if (not same(source_oper.get_item_strict(Path(item.path)), item)
                    or target_oper.get_item_strict(destination)):
                return failure('ISO_PRE_MOVE_CHECK_FAILED')
            service.query('UPDATE iso_move_checkpoints SET state=? WHERE file_id=?', ('MOVING', str(item.fileid)))
            result = TransferInfo(success=False, fileitem=item, transfer_type='move')
            # Native host transfer retains interception events and refuses overwrite.
            new_item, _ = handler._TransHandler__transfer_file(
                fileitem=item, meta=meta, mediainfo=media, source_oper=source_oper,
                target_oper=target_oper, target_storage='u115', target_file=destination,
                transfer_type='move', result=result, over_flag=False)
            current = target_oper.get_item_strict(destination)
            if (not new_item or not current or str(current.fileid) != str(item.fileid)
                    or current.storage != 'u115' or current.type != 'file'
                    or current.size != item.size or Path(current.path) != destination
                    or current.name != item.name or source_oper.get_item_strict(Path(item.path))):
                return failure('ISO_MOVE_UNVERIFIED')
            service.query('UPDATE iso_move_checkpoints SET state=? WHERE file_id=?', ('DONE', str(item.fileid)))
            result.success = True
            result.target_item, result.target_diritem = current, folder
            result.need_scrape, result.need_notify = data.get('need_scrape', False), data.get('need_notify', True)
            return result
        except Exception:
            # Never return raw host exceptions, URLs, credentials or private payloads.
            return failure('ISO_HOST_OPERATION_UNVERIFIED')
