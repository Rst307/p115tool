"""Independent output stages over the last complete organized-directory scan."""
import json
from .models import SafetyError


def run_batch(service, kind, automatic=False):
    service.available()
    if kind == 'share_batch' and not service.config.share_enabled:
        raise SafetyError('Virtual-share backend is disabled')
    snapshot = service.db.one('SELECT value FROM settings WHERE name=?', ('organized_scan_ids',))
    if snapshot is None:
        raise SafetyError('Scan organized directories before starting output tasks')
    roots = service.db.one('SELECT value FROM settings WHERE name=?', ('organized_scan_roots',))
    if not roots or json.loads(roots['value']) != service.config.source_cids:
        raise SafetyError('Directory configuration changed; scan again before starting output tasks')
    ids = json.loads(snapshot['value'])
    # Restrict stale snapshots to the currently configured directory prefixes.
    prefixes = [r.get('prefix', '/') if isinstance(r, dict) else '/' for r in service.config.source_cids]
    ids = [mid for mid in ids if any(service.db.media(mid).virtual_path.startswith(p.rstrip('/') + '/') for p in prefixes)]
    def selected(mid):
        media = service.db.media(mid)
        if media.source_deleted:
            return False
        if automatic:
            return service.config.storage_policy(media.virtual_path, media.size) == 'SHARE'
        for rule in service.config.policies:
            if (media.virtual_path.startswith(rule.get('prefix', '/').rstrip('/') + '/')
                    and rule.get('min_bytes', 0) <= media.size <= rule.get('max_bytes', 2**63 - 1)):
                return rule['storage'] == 'SHARE'
        return True

    failed, done, processed = 0, 0, set()
    for mid in ids:
        media = service.db.media(mid)
        if kind == 'share_batch':
            if not selected(mid):
                continue
            key = service.groups.key(mid)
            if key in processed:
                continue
            processed.add(key)
        try:
            if kind == 'generate_batch':
                service.strm.generate(mid)
                if media.status == 'DISCOVERED':
                    service.db.transition(mid, 'READY')
            elif key.startswith('file:'):
                service.archive(mid, delete=False, generate_strm=False)
            else:
                members = [member for member in ids if service.groups.key(member) == key
                           and selected(member)]
                service.groups.archive(members, delete=False, label=key, generate_strm=False)
            done += 1
        except Exception:
            failed += 1
            service.db.log(kind, 'FAILED', mid, 'Output stage failed; inspect durable checkpoints')
    service.db.log(kind, 'DONE' if not failed else 'FAILED', detail=f'completed={done}; failed={failed}')
    if failed:
        raise SafetyError('Output stage incomplete; inspect individual checkpoints')
    return {'completed': done, 'failed': failed}
