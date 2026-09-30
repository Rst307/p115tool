"""Replace an entire immutable group's share only after an all-member barrier."""
from contextlib import ExitStack
import json
import re
import time
from .models import SafetyError, ToolError
from .share_repair import retained_source


def repair_group(service, gid, attach=None):
    service.available()
    if not service.config.share_enabled:
        raise SafetyError('Share backend is disabled')
    with service._maintenance, ExitStack() as locks:
        db, client, groups = service.db, service.client, service.groups
        group = db.one('SELECT * FROM share_groups WHERE id=?',(gid,))
        if not group:
            raise KeyError('Group not found')
        if group['state'] != 'READY':
            raise SafetyError('Original group archive checkpoint must be reconciled first')
        rows = groups.members(gid)
        if not rows or len(rows)>1000:
            raise SafetyError('Invalid repair member snapshot')
        for row in rows:
            mid = row['media_id']
            locks.enter_context(service.lock(mid))
            media = db.media(mid)
            if service.share(mid)['share_code'] != group['share_code'] or db.one('SELECT value FROM settings WHERE name=?',(f'share_repair:{mid}',)):
                raise SafetyError('Member backend or pending single repair differs from group')
            if ((groups.membership(mid) or {}).get('id') != gid or
                (media.file_name,media.size,media.sha1) != (row['name'],row['size'],row['sha1']) or
                media.status not in ('READY','BROKEN') or
                db.one("SELECT media_id FROM organize_plans WHERE media_id=? AND state<>'DONE'",(mid,))):
                raise SafetyError('Member identity or pending checkpoint prevents group repair')
        key = f'group_repair:{gid}'
        saved = db.one('SELECT value FROM settings WHERE name=?',(key,))
        plan = json.loads(saved['value']) if saved else None
        if plan and (plan['members'] != rows or plan['old_code'] != group['share_code']):
            raise SafetyError('Group snapshot changed since repair intent')
        if not plan:
            if attach is not None:
                raise SafetyError('Attaching requires an existing group repair intent')
            try:
                for row in rows:
                    service.verify_share(row['media_id'],deep=True)
            except ToolError:
                pass
            else:
                for row in rows:
                    service.strm.repair(row['media_id'])
                    db.transition(row['media_id'],'READY',storage='SHARE')
                return groups.public(gid)
            client.refresh()
            sources = [retained_source(service,db.media(row['media_id'])) for row in rows]
            ids = [file.file_id for file in sources]
            if len(set(ids)) != len(ids):
                raise SafetyError('Group repair source identities must be distinct')
            plan = {'members':rows,'old_code':group['share_code'],'source_ids':ids,'state':'CREATING'}
            db.execute('INSERT INTO settings VALUES(?,?)',(key,json.dumps(plan)))
            db.log('group_repair','CREATING',detail=f'group_id={gid}; old backend retained')
            code,password = client.create_share(ids)
            plan.update(code=code,password=password,state='VERIFYING')
            db.execute('UPDATE settings SET value=? WHERE name=?',(json.dumps(plan),key))
            db.metric('shares_created')
        if attach is not None:
            code,password=attach
            if not re.fullmatch(r'[A-Za-z0-9]+',code) or not re.fullmatch(r'[A-Za-z0-9]{4}',password):
                raise ValueError('Invalid share code/password')
            if plan.get('code') and (code,password)!=(plan['code'],plan['password']):
                raise SafetyError('Recorded repair response cannot be silently replaced')
            plan.update(code=code,password=password,state='VERIFYING')
            db.execute('UPDATE settings SET value=? WHERE name=?',(json.dumps(plan),key))
        if not plan.get('code'):
            raise SafetyError('Uncertain group share creation requires explicit outcome attachment')
        client.ensure_share_retention(plan['code'])
        verified=[]
        for row in rows:
            media=db.media(row['media_id'])
            file=client.find_share_file(plan['code'],plan['password'],service.expected(media))
            share={'share_code':plan['code'],'receive_code':plan['password'],'file_id':file.file_id,'parent_id':file.parent_id}
            link=client.share_link(share,'p115tool/1.0')
            client.validate_url(link.url)
            client.probe_range(link,'p115tool/1.0',media.size)
            service.strm.repair(media.id)
            verified.append((media.id,file))
        now=time.time()
        with db.connect() as connection:
            # All members pass before this transaction changes any mapping.
            for mid,file in verified:
                connection.execute('''UPDATE share_objects SET share_code=?,receive_code=?,file_id=?,parent_id=?,
                    sha1=?,size=?,name=?,created_at=?,verified_at=?,range_verified_at=? WHERE media_id=?''',
                    (plan['code'],plan['password'],file.file_id,file.parent_id,file.sha1.upper(),file.size,file.name,now,now,now,mid))
                connection.execute("UPDATE media SET storage_type='SHARE',status='READY',error=NULL,updated_at=? WHERE id=?",(now,mid))
            connection.execute('UPDATE share_groups SET share_code=?,receive_code=?,error=NULL,updated_at=? WHERE id=?',
                (plan['code'],plan['password'],now,gid))
            connection.execute('DELETE FROM settings WHERE name=?',(key,))
        for row in rows:
            service.invalidate(row['media_id'])
        db.log('group_repair','DONE',detail=f'group_id={gid}; all members verified; sources unchanged')
        return groups.public(gid)
