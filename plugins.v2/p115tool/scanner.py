"""Conservative source absence reconciliation after complete tree enumeration."""
import time
from .models import MissingFile, RemoteError, ToolError


def reconcile_sources(service, snapshots, counts):
    db = service.db
    observed_all = set().union(*(files for _, _, files in snapshots))
    candidates = {}
    confirmed = []
    normals = db.all('SELECT media_id,file_id,parent_id FROM normal_objects')
    # Membership is source identity, not virtual-path prefix (which can change).
    # Persist only after every configured tree completed successfully.
    with db.connect() as connection:
        for cid, parents, observed in snapshots:
            members = {r['media_id'] for r in normals
                       if r['parent_id'] in parents or r['file_id'] in observed}
            for mid in members:
                connection.execute('INSERT OR IGNORE INTO scan_members VALUES(?,?)', (cid, mid))
            rows = connection.execute('''SELECT n.media_id,n.file_id FROM scan_members s
                JOIN normal_objects n ON n.media_id=s.media_id WHERE s.root_cid=?''', (cid,)).fetchall()
            for row in rows:
                if row['file_id'] not in observed_all:
                    candidates.setdefault(row['media_id'], row['file_id'])
    if not candidates:
        return
    # Expired authentication must never be interpreted as file deletion.
    try:
        service.client.refresh()
    except ToolError:
        counts['errors'] += 1
        db.log('scan_reconcile', 'FAILED', detail='Authentication unavailable; sources retained')
        return
    for mid, fid in candidates.items():
        media = db.media(mid)
        if media.source_deleted:
            continue
        if db.one("SELECT media_id FROM organize_plans WHERE media_id=? AND state<>'DONE'", (mid,)):
            continue
        if media.status not in ('READY', 'DISCOVERED', 'BROKEN'):
            continue  # Never overwrite an archive/uncertain-write checkpoint.
        try:
            file = service.client.stat(fid)
            if file.file_id != fid or file.is_dir or file.sha1.upper() != media.sha1.upper() or file.size != media.size:
                raise RemoteError('Source identity changed')
        except MissingFile:
            db.execute('''INSERT INTO missing_sources VALUES(?,?,?)
                ON CONFLICT(media_id) DO UPDATE SET file_id=excluded.file_id,confirmed_at=excluded.confirmed_at''',
                (mid, fid, time.time()))
            counts['missing'] += 1
            confirmed.append(mid)
            # Keep viable virtual backends and their stable STRM/token unchanged.
            alternate = db.one('SELECT media_id FROM share_objects WHERE media_id=?', (mid,)) or db.one(
                'SELECT media_id FROM cache_objects WHERE media_id=?', (mid,))
            if not alternate and media.storage_type == 'NORMAL':
                db.transition(mid, 'BROKEN', 'Source missing (scan confirmed)')
                service.invalidate(mid)
            db.log('scan_reconcile', 'MISSING', mid, 'Source absence confirmed; remote files untouched')
        except ToolError:
            counts['errors'] += 1
            db.log('scan_reconcile', 'FAILED', mid, 'Source query inconclusive; STRM retained')
        else:
            db.execute('DELETE FROM missing_sources WHERE media_id=?', (mid,))
            if media.status == 'BROKEN' and media.error == 'Source missing (scan confirmed)':
                if service.config.auto_generate:
                    service.strm.repair(mid)
                db.transition(mid, 'READY' if service.config.auto_generate else 'DISCOVERED')
            # Existing files moved outside monitored roots are not deleted.
            db.execute('DELETE FROM scan_members WHERE media_id=?', (mid,))
            counts['outside'] += 1
            db.log('scan_reconcile', 'OUTSIDE', mid, 'Source exists outside current scan snapshot')
    if service.config.clean_missing_strm:
        counts['removed_strm'] += len(service.strm.clean_broken(confirmed))


def clean_missing(service, media_id=None):
    """Explicit local cleanup rechecks old absence evidence before unlinking."""
    rows = service.db.all('SELECT media_id,file_id FROM missing_sources' +
        (' WHERE media_id=?' if media_id is not None else ''), (media_id,) if media_id is not None else ())
    if not rows:
        return {'removed': [], 'inconclusive': []}
    service.client.refresh()
    confirmed, inconclusive = [], []
    for row in rows:
        try:
            service.client.stat(row['file_id'])
        except MissingFile:
            confirmed.append(row['media_id'])
        except ToolError:
            inconclusive.append(row['media_id'])
        else:
            service.db.execute('DELETE FROM missing_sources WHERE media_id=?', (row['media_id'],))
    removed = service.strm.clean_broken(confirmed)
    for mid in removed:
        service.db.log('strm_cleanup', 'DONE', mid, 'Owned STRM removed after fresh source absence check')
    return {'removed': removed, 'inconclusive': inconclusive}
