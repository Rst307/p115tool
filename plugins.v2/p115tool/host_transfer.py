"""Delegate selected directories to MoviePilot; no organizer, queue or event records."""
from pathlib import Path
from .strm import safe_parts


def value(item, key):
    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)


def organize_target(config):
    libraries = getattr(config, 'source_cids', [])
    selected = getattr(config, 'organize_target_cid', '')
    if selected:
        return next((source for source in libraries if source['cid'] == selected), None)
    return libraries[0] if len(libraries) == 1 else None


def organize(service, cids):
    if (not isinstance(cids, list) or not cids or len(cids) > 100
            or any(not isinstance(cid, str) for cid in cids)
            or len(set(cids)) != len(cids)):
        raise ValueError('Invalid folder selection')
    sources = {source['cid']: source for source in service.config.organize_cids}
    if any(cid not in sources for cid in cids):
        raise ValueError('Unconfigured folder')
    if getattr(service,'_organize_unknown',False):
        return {'items': [], 'state': 'UNKNOWN'}
    if service._worker and service._worker.is_alive():
        return {'items': [], 'state': 'BUSY'}
    target = organize_target(service.config)
    if not target:
        return {'items': [], 'state': 'TARGET_REQUIRED'}
    target_path = target['prefix'].rstrip('/') or '/'
    if target['cid'] == '0' or target_path == '/':
        return {'items': [], 'state': 'TARGET_MISMATCH'}
    try:
        from app.chain.storage import StorageChain
        from app.chain.transfer import TransferChain
        storage, transfer = StorageChain(), TransferChain()
    except Exception:
        return {'items': [], 'state': 'UNAVAILABLE'}
    try:
        safe_parts(target_path.lstrip('/'))
        target_item = storage.get_file_item(storage='u115', path=Path(target_path))
        if (not target_item or value(target_item, 'storage') not in ('u115', '115网盘', '115云盘')
                or value(target_item, 'type') != 'dir' or str(value(target_item, 'fileid')) != target['cid']
                or (str(value(target_item, 'path')).rstrip('/') or '/') != target_path):
            return {'items': [], 'state': 'TARGET_MISMATCH'}
    except Exception:
        return {'items': [], 'state': 'TARGET_UNAVAILABLE'}
    items = []
    # Resolve and validate every source before submitting any host task. In
    # particular, a non-root CID with prefix '/' must never organize the root.
    try:
        for cid in cids:
            path = sources[cid]['prefix'].rstrip('/') or '/'
            if path != '/':
                safe_parts(path.lstrip('/'))
            item = storage.get_file_item(storage='u115', path=Path(path))
            if (not item or value(item, 'storage') not in ('u115', '115网盘', '115云盘')
                    or value(item, 'type') != 'dir' or str(value(item, 'fileid')) != cid
                    or (str(value(item, 'path')).rstrip('/') or '/') != path):
                return {'items': [], 'state': 'SOURCE_MISMATCH'}
            items.append((cid, path, item))
    except Exception:
        return {'items': [], 'state': 'SOURCE_UNAVAILABLE'}
    if any(cid == target['cid'] or path == target_path or path == '/'
           or target_path.startswith(path + '/') or path.startswith(target_path + '/')
           for cid, path, _ in items):
        return {'items': [], 'state': 'TARGET_OVERLAP'}
    # Drop overlapping descendants: one directory submission already recursively
    # includes them. Equal paths with different identities were rejected above.
    roots = [(cid, path, item) for cid, path, item in items
             if not any(other != cid and path != parent
                        and (parent == '/' or path.startswith(parent + '/'))
                        for other, parent, _ in items)]
    try:
        from .iso_transfer import register
        register(service, [sources[cid] for cid, _, _ in roots], target)
    except Exception:
        return {'items': [], 'state': 'UNAVAILABLE'}
    results = []
    for cid, _, item in roots:
        try:
            accepted, _ = transfer.manual_transfer(
                fileitem=item, target_storage='u115', target_path=Path(target_path), transfer_type='move',
                background=True, force=False, sync_extra_files=True)
            state = 'SUBMITTED' if accepted is True else 'REJECTED'
        except Exception:
            state = 'UNKNOWN'
        results.append({'cid': cid, 'state': state})
        if state == 'UNKNOWN':
            service._organize_unknown=True
            break  # Never retry or submit remaining roots after an unknown result.
    return {'items': results, 'state': 'SUBMITTED' if all(r['state'] == 'SUBMITTED' for r in results) else 'ATTENTION'}
