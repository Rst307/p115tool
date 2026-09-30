"""Cache directory write-ahead intent and read-only outcome reconciliation."""
import json
import time
from .models import SafetyError


def cache_folder(service, mid, reconcile_only=False, adopt_id=None):
    db, client = service.db, service.client
    key = f'cache_folder_create:{mid}'
    parent, name = str(service.config.cache_cid), f'p115tool-{mid}'
    existing = db.one('SELECT * FROM cache_folders WHERE media_id=?', (mid,))
    if existing:
        if adopt_id is not None and str(adopt_id) != existing['folder_id']:
            raise SafetyError('Directory already belongs to a different recorded identity')
        file = client.stat(existing['folder_id'])
        if not file.is_dir or file.parent_id != parent or file.name != name:
            raise SafetyError('Owned cache folder moved or changed identity')
        return existing
    saved = db.one('SELECT value FROM settings WHERE name=?', (key,))
    plan = json.loads(saved['value']) if saved else None
    if plan and (plan.get('parent') != parent or plan.get('name') != name):
        raise SafetyError('Cache directory configuration differs from pending intent')
    if adopt_id is not None and (not plan or not reconcile_only):
        raise SafetyError('Explicit directory adoption requires a recorded creation intent')
    if not plan:
        if reconcile_only:
            raise SafetyError('No recorded cache directory creation to reconcile')
        # Refuse preexisting same-name objects; name alone is not ownership.
        if any(file.name == name for file in client.list_files(parent)):
            raise SafetyError('Cache folder name already exists without ownership')
        plan = {'parent': parent, 'name': name}
        db.execute('INSERT INTO settings VALUES(?,?)', (key, json.dumps(plan)))
        # A timeout/process exit leaves the intent. No automatic mkdir replay.
        cid = str(client.make_cache_folder(parent, name))
        if not cid.isdecimal() or cid in ('0', parent):
            raise SafetyError('Invalid created cache folder identity')
        plan['folder_id'] = cid
        db.execute('UPDATE settings SET value=? WHERE name=?', (json.dumps(plan), key))
    elif not reconcile_only:
        raise SafetyError('Uncertain directory creation requires read-only reconciliation')
    matches = [file for file in client.list_files(parent) if file.name == name]
    if len(matches) != 1 or not matches[0].is_dir:
        raise SafetyError('Created cache folder outcome absent or ambiguous')
    folder = matches[0]
    if not plan.get('folder_id'):
        if adopt_id is None or str(adopt_id) != folder.file_id:
            raise SafetyError('Unknown mkdir outcome requires explicit candidate ownership confirmation')
    if plan.get('folder_id') and plan['folder_id'] != folder.file_id:
        raise SafetyError('Created directory differs from recorded response')
    actual = client.stat(folder.file_id)
    if not actual.is_dir or actual.file_id != folder.file_id or actual.parent_id != parent or actual.name != name:
        raise SafetyError('Cache directory identity could not be confirmed')
    with db.connect() as connection:
        connection.execute('INSERT INTO cache_folders VALUES(?,?,?)', (mid, folder.file_id, time.time()))
        connection.execute('DELETE FROM settings WHERE name=?', (key,))
    db.log('cache_folder_reconcile' if reconcile_only else 'cache_folder_create', 'DONE', mid)
    return db.one('SELECT * FROM cache_folders WHERE media_id=?', (mid,))


def folder_candidates(service, mid):
    service.db.media(mid)
    saved=service.db.one('SELECT value FROM settings WHERE name=?',(f'cache_folder_create:{mid}',))
    if not saved:
        return {'media_id':mid,'candidates':[]}
    plan=json.loads(saved['value'])
    if plan.get('parent')!=str(service.config.cache_cid):
        raise SafetyError('Cache directory configuration differs from pending intent')
    rows=[f for f in service.client.list_files(plan['parent']) if f.name==plan['name']]
    return {'media_id':mid,'candidates':[{'folder_id':f.file_id,'is_directory':f.is_dir} for f in rows]}
