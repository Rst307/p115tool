"""Resumable share replacement without sacrificing the previous backend."""
import json
import re
from .models import SafetyError, ToolError


def repair(service, mid, attach=None):
    db, client = service.db, service.client
    media = db.media(mid)
    if not service.config.share_enabled:
        raise SafetyError('Share backend is disabled')
    if service.groups.membership(mid):
        raise SafetyError('Grouped share replacement requires a group-level recovery plan')
    if media.status not in ('READY','BROKEN','FAILED_VERIFY'):
        raise SafetyError('Existing archive checkpoint must be reconciled first')
    if db.one("SELECT media_id FROM organize_plans WHERE media_id=? AND state<>'DONE'",(mid,)):
        raise SafetyError('Pending organize plan must be reconciled first')
    key = f'share_repair:{mid}'
    saved = db.one('SELECT value FROM settings WHERE name=?',(key,))
    plan = json.loads(saved['value']) if saved else None
    if not plan:
        if attach is not None:
            raise SafetyError('Attaching a repair outcome requires an existing repair intent')
        try:
            service.verify_share(mid,deep=True)
        except ToolError:
            pass
        else:
            service.strm.repair(mid)
            db.transition(mid,'READY',storage='SHARE')
            return db.media(mid)
        client.refresh()  # Authentication failure is not evidence to create shares.
        source = retained_source(service, media)
        plan = {'name':media.file_name,'sha1':media.sha1,'size':media.size,'source_id':source.file_id,'state':'CREATING'}
        db.execute('INSERT INTO settings VALUES(?,?)',(key,json.dumps(plan)))
        db.log('share_repair','CREATING',mid,'Creation intent recorded; existing backend retained')
        # Keep the old share mapping. Persist the returned identifiers before
        # retention update, metadata, Range, or STRM work can fail.
        code,password = client.create_share(source.file_id)
        plan.update(code=code,password=password,state='VERIFYING')
        db.execute('UPDATE settings SET value=? WHERE name=?',(json.dumps(plan),key))
        db.metric('shares_created')
    if (plan['name'],plan['sha1'].upper(),plan['size']) != (media.file_name,media.sha1.upper(),media.size):
        raise SafetyError('Media identity differs from share repair intent')
    if attach is not None:
        code,password = attach
        if not re.fullmatch(r'[A-Za-z0-9]+',code) or not re.fullmatch(r'[A-Za-z0-9]{4}',password):
            raise ValueError('Invalid share code/password')
        if plan.get('code') and (code,password) != (plan['code'],plan['password']):
            raise SafetyError('Recorded repair response cannot be replaced silently')
        plan.update(code=code,password=password,state='VERIFYING')
        db.execute('UPDATE settings SET value=? WHERE name=?',(json.dumps(plan),key))
    if not plan.get('code'):
        raise SafetyError('Uncertain share creation; explicitly attach the existing repair outcome')
    client.ensure_share_retention(plan['code'])
    file = client.find_share_file(plan['code'],plan['password'],service.expected(media))
    new_share = {'share_code':plan['code'],'receive_code':plan['password'],'file_id':file.file_id,'parent_id':file.parent_id}
    link = client.share_link(new_share,'p115tool/1.0')
    client.validate_url(link.url)
    client.probe_range(link,'p115tool/1.0',media.size)
    service.strm.repair(mid)
    # A single commit switches the backend and clears its pending repair intent.
    # No source/cache deletion is performed, including automatic repair.
    return finish(service,mid,plan,file,key)


def finish(service,mid,plan,file,key):
    import time
    now=time.time()
    with service.db.connect() as connection:
        connection.execute('''INSERT INTO share_objects VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(media_id) DO UPDATE SET share_code=excluded.share_code,receive_code=excluded.receive_code,
            file_id=excluded.file_id,parent_id=excluded.parent_id,sha1=excluded.sha1,size=excluded.size,
            name=excluded.name,created_at=excluded.created_at,verified_at=excluded.verified_at,range_verified_at=excluded.range_verified_at''',
            (mid,plan['code'],plan['password'],file.file_id,file.parent_id,file.sha1.upper(),file.size,file.name,now,now,now))
        connection.execute("UPDATE media SET storage_type='SHARE',status='READY',error=NULL,updated_at=? WHERE id=?",(now,mid))
        connection.execute('DELETE FROM settings WHERE name=?',(key,))
    service.invalidate(mid)
    service.db.log('share_repair','DONE',mid,'Replacement share verified; retained sources unchanged')
    return service.db.media(mid)


def retained_source(service, media):
    db, client, mid = service.db, service.client, media.id
    source = None
    if not media.source_deleted:
        try:
            candidate = client.stat(service.normal(mid)['file_id'])
            if service.expected(media).matches(candidate):
                source = candidate
        except ToolError:
            pass
    if source is None:
        cache = db.one("SELECT * FROM cache_objects WHERE media_id=? AND state='READY'",(mid,))
        if not cache:
            raise SafetyError('No verified retained source or cache available for repair')
        from .recovery import cache_folder
        cache_folder(service,mid,reconcile_only=True)
        candidate = client.stat(cache['file_id'])
        if not service.expected(media).matches(candidate) or candidate.parent_id != cache['parent_id']:
            raise SafetyError('Cache identity changed; cannot rearchive')
        service.touch(mid)
        source = candidate
    return source
