"""Persistent share restores into the original personal-drive directory."""
import json
import time
from .models import SafetyError, MissingFile


def restore(service, mid, reconcile_only=False):
    with service._maintenance, service.lock(mid):
        media, normal = service.db.media(mid), service.normal(mid)
        parent = normal['parent_id']
        if not parent or parent == '0':
            raise SafetyError('An explicit original directory is required')
        folder = service.client.stat(parent)
        if not folder.is_dir or folder.file_id != parent:
            raise SafetyError('Original directory is unavailable; no transfer attempted')
        key = f'original_restore:{mid}'
        saved = service.db.one('SELECT value FROM settings WHERE name=?', (key,))
        plan = json.loads(saved['value']) if saved else None
        identity = {'parent': parent, 'name': folder.name, 'ancestor': folder.parent_id,
                    'sha1': media.sha1, 'size': media.size, 'file_name': media.file_name}
        if plan and plan != identity:
            raise SafetyError('Original restore identity changed')
        try:
            source = service.client.stat(normal['file_id'])
        except MissingFile:
            source = None
        if source is not None:
            if not service.expected(media).matches(source) or source.parent_id != parent or not source.pickcode:
                raise SafetyError('Original source identity changed')
            file = source
        else:
            rows = list(service.client.list_files(parent))
            matches = [f for f in rows if service.expected(media).matches(f)]
            if any(f.name == media.file_name for f in rows) and not (plan and len(matches) == 1):
                raise SafetyError('Original directory has a conflicting file; no transfer attempted')
            if not matches:
                if reconcile_only or plan:
                    raise SafetyError('Unknown original restore outcome; only read-only reconciliation is allowed')
                # Verify the source before recording a single remote receive intent.
                # The download endpoint may be the reason for this fallback;
                # receive plus final personal-file identity checks establish it.
                service.verify_share(mid)
                service.db.execute('INSERT INTO settings VALUES(?,?)', (key, json.dumps(identity)))
                service.client.restore(service.share(mid), parent)
                # Only reads are retried, never the transfer request.
                for attempt in range(3):
                    matches = [f for f in service.client.list_files(parent) if service.expected(media).matches(f)]
                    if matches:
                        break
                    time.sleep(0.5 * (attempt + 1))
            if len(matches) != 1 or not matches[0].pickcode or matches[0].parent_id != parent:
                raise SafetyError('Original restore is absent or ambiguous')
            file = service.client.stat(matches[0].file_id)
            if not service.expected(media).matches(file) or file.parent_id != parent or not file.pickcode:
                raise SafetyError('Restored original identity changed')
        # Permanent personal-drive storage: never enrolled in the TTL cache.
        with service.db.connect() as connection:
            connection.execute('UPDATE normal_objects SET file_id=?,pickcode=?,parent_id=? WHERE media_id=?',
                               (file.file_id, file.pickcode, parent, mid))
            connection.execute("UPDATE media SET source_deleted=0,storage_type='NORMAL',status='READY',error=NULL,updated_at=? WHERE id=?",
                               (time.time(), mid))
            connection.execute('DELETE FROM settings WHERE name=?', (key,))
            connection.execute('DELETE FROM missing_sources WHERE media_id=?', (mid,))
        service.invalidate(mid)
        service.db.log('original_restore', 'DONE', mid)
        return service.normal(mid)
