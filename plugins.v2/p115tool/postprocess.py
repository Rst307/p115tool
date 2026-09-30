"""Independent output stages over the last complete organized-directory scan."""
import json
from .models import SafetyError


OUTPUT_REASONS = {
    'FAILED_SCAN_REQUIRED': '没有成功扫描快照；请先立即扫描整理后目录，修复扫描错误后再生成STRM',
    'FAILED_SCAN_ROOTS_CHANGED': '扫描目录配置已变化；请重新扫描成功后再生成STRM',
    'FAILED_OUTPUT_PARTIAL': '部分输出失败；请按媒体ID查看本次输出失败原因',
}


class OutputStageError(SafetyError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(OUTPUT_REASONS[reason])


def strm_failure_reason(exc):
    # Only fixed local validation messages/types are exposed, never exception text.
    if isinstance(exc, PermissionError):
        return 'FAILED_STRM_PERMISSION'
    if isinstance(exc, OSError):
        return 'FAILED_STRM_IO'
    if isinstance(exc, UnicodeError):
        return 'FAILED_STRM_ENCODING'
    if isinstance(exc, SafetyError):
        if str(exc) in ('Refusing to overwrite an unowned/edited STRM',
                        'STRM path already owned by another media object'):
            return 'FAILED_STRM_CONFLICT'
        return 'FAILED_STRM_SAFETY'
    return 'FAILED_STRM_INTERNAL'


def run_batch(service, kind, automatic=False, delete=False):
    service.available()
    if kind == 'share_batch' and not service.config.share_enabled:
        raise SafetyError('Virtual-share backend is disabled')
    if delete and not service.config.delete_source:
        raise SafetyError('Source deletion is disabled')
    if delete and automatic and not service.config.auto_delete:
        raise SafetyError('Automatic source deletion is disabled')
    snapshot = service.db.one('SELECT value FROM settings WHERE name=?', ('organized_scan_ids',))
    if snapshot is None:
        raise OutputStageError('FAILED_SCAN_REQUIRED')
    roots = service.db.one('SELECT value FROM settings WHERE name=?', ('organized_scan_roots',))
    if not roots or json.loads(roots['value']) != service.config.source_cids:
        raise OutputStageError('FAILED_SCAN_ROOTS_CHANGED')
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
                service.archive(mid, delete=delete, generate_strm=delete)
            else:
                members = [member for member in ids if service.groups.key(member) == key
                           and selected(member)]
                service.groups.archive(members, delete=delete, label=key, generate_strm=delete)
            done += 1
        except Exception as exc:
            failed += 1
            reason = strm_failure_reason(exc) if kind == 'generate_batch' else 'FAILED'
            service.db.log(kind, reason, mid, 'Output stage failed; inspect durable checkpoints')
    service.db.log(kind, 'DONE' if not failed else 'FAILED', detail=f'completed={done}; failed={failed}')
    if failed:
        raise OutputStageError('FAILED_OUTPUT_PARTIAL')
    return {'completed': done, 'failed': failed}
