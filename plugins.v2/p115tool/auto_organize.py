"""MoviePilot owns recognition, naming, directories and transfer execution."""
import json
import time
from pathlib import Path
from .models import SafetyError
from .organizer import value
from .strm import virtual_path

STORAGES = ('u115', '115网盘', '115云盘')


def save(service, key, record):
    service.db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                       (key, json.dumps(record)))


def status(service, mid):
    service.db.media(mid)
    row = service.db.one('SELECT value FROM settings WHERE name=?', (f'moviepilot_organize:{mid}',))
    record = json.loads(row['value']) if row else {'media_id': mid, 'state': 'NOT_SUBMITTED'}
    return {k: record[k] for k in ('media_id', 'state', 'source_path', 'target_path', 'updated_at') if k in record}


def archive_guard(service, mid):
    if status(service, mid)['state'] != 'NOT_SUBMITTED':
        raise SafetyError('Archive the MoviePilot returned target; retain the organize source')


def host_item(service, mid):
    media = service.db.media(mid)
    if (media.source_deleted or media.storage_type != 'NORMAL' or
            media.status not in ('DISCOVERED', 'READY') or service.groups.membership(mid)):
        raise SafetyError('MoviePilot organize requires a retained ungrouped normal source')
    if service.db.one("SELECT media_id FROM organize_plans WHERE media_id=? AND state<>'DONE'", (mid,)):
        raise SafetyError('An older plugin organize checkpoint requires read-only reconciliation')
    source = service.client.stat(service.normal(mid)['file_id'])
    if not service.expected(media).matches(source):
        raise SafetyError('115 source identity changed before MoviePilot organization')
    from app.chain.storage import StorageChain
    item = StorageChain().get_file_item(storage='u115', path=Path(source.path or media.virtual_path))
    if (not item or value(item, 'storage') not in STORAGES or
            str(value(item, 'fileid', '')) != str(source.file_id)):
        raise SafetyError('MoviePilot cannot identify this 115 source path; check the scan prefix')
    return media, source, item


def execute(service, mid, resume=False, recognizer=None):
    service.available()
    if not service.config.auto_organize_enabled:
        raise SafetyError('MoviePilot organize delegation is disabled')
    if resume:
        raise SafetyError('Manage organize retries in MoviePilot')
    with service._maintenance, service.lock(mid):
        media = service.db.media(mid)
        if status(service, mid)['state'] != 'NOT_SUBMITTED':
            return media
        fid = service.normal(mid)['file_id']
        if service.db.one('SELECT name FROM settings WHERE name=?', (f'moviepilot_target:{fid}',)):
            return media
        try:
            media, source, item = host_item(service, mid)
            from app.chain.transfer import TransferChain
        except SafetyError:
            raise
        except Exception:
            raise SafetyError('MoviePilot organization is unavailable') from None
        key = f'moviepilot_organize:{mid}'
        record = {'media_id': mid, 'source_file_id': str(source.file_id),
                  'source_path': virtual_path(str(value(item, 'path'))),
                  'state': 'SUBMITTED', 'updated_at': time.time()}
        save(service, key, record)
        try:
            ok, _ = TransferChain().manual_transfer(
                fileitem=item, target_storage='u115', transfer_type='copy',
                background=True, force=False, sync_extra_files=True)
        except Exception:
            if status(service, mid)['state'] == 'SUBMITTED':
                record.update(state='UNKNOWN', updated_at=time.time())
                save(service, key, record)
            raise SafetyError('MoviePilot submit outcome is unknown; check host history') from None
        if ok is not True:
            if status(service, mid)['state'] == 'SUBMITTED':
                record.update(state='FAILED', updated_at=time.time())
                save(service, key, record)
            raise SafetyError('MoviePilot rejected the organize request; check host history')
        service.db.log('moviepilot_organize', 'SUBMITTED', mid, 'MoviePilot transfer queued; source retained')
        return media


def preview(service, mid):
    service.available()
    try:
        _, _, item = host_item(service, mid)
        from app.chain.transfer import TransferChain
        ok, result = TransferChain().manual_transfer(
            fileitem=item, target_storage='u115', transfer_type='copy',
            background=False, preview=True, force=False, sync_extra_files=True)
        if ok is not True or not isinstance(result, dict):
            raise SafetyError('MoviePilot preview failed')
        items = []
        for entry in result.get('items', []):
            items.append({key: virtual_path(str(entry[key])) if entry.get(key) else None
                          for key in ('source', 'target', 'target_dir')} |
                         {'success': entry.get('success') is True})
        return {'provider': 'MoviePilot', 'items': items}
    except SafetyError:
        raise
    except Exception:
        raise SafetyError('MoviePilot preview is unavailable') from None


def observe_result(service, source_item, target_item, success):
    """Host event callback: no network, no host writes, whitelisted metadata only."""
    if value(source_item, 'storage') not in STORAGES:
        return None
    source_id = str(value(source_item, 'fileid', '') or '')
    if not source_id.isdigit():
        return None
    target = None
    if success is True:
        if value(target_item, 'storage') not in STORAGES:
            return None
        fid = str(value(target_item, 'fileid', '') or '')
        path = virtual_path(str(value(target_item, 'path', '') or ''))
        if not fid.isdigit() or Path(path).suffix.lower() not in service.config.media_extensions:
            return None
        target = {'target_path': path, 'target_file_id': fid}
        save(service, f'moviepilot_target:{fid}', target)
    with service._maintenance:
        for row in service.db.all("SELECT name,value FROM settings WHERE name LIKE 'moviepilot_organize:%'"):
            record = json.loads(row['value'])
            if (record.get('source_file_id') != source_id or record.get('state') == 'DONE' or
                    record.get('source_path') != str(value(source_item, 'path', ''))):
                continue
            record.update(state='DONE' if success is True else 'FAILED', updated_at=time.time())
            if target:
                record.update(target)
            save(service, row['name'], record)
            service.db.log('moviepilot_organize', record['state'], record['media_id'], 'MoviePilot result observed; source retained')
            return record
    return None
