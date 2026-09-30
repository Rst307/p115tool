"""Write-ahead automatic organizer; source deletion is never part of this module."""
import hashlib
import json
from .models import MissingFile, SafetyError
from .organizer import preview
from .strm import virtual_path
from pathlib import PurePosixPath


def directory(service,parent,name):
    db,client=service.db,service.client
    key='organize_mkdir:'+hashlib.sha256((parent+'/'+name).encode()).hexdigest()
    saved=db.one('SELECT value FROM settings WHERE name=?',(key,))
    intent=json.loads(saved['value']) if saved else None
    rows=[f for f in client.list_files(parent) if f.name==name]
    if not rows:
        if intent:
            raise SafetyError('Uncertain directory creation: wait for an observable outcome; no mkdir replay')
        intent={'parent':parent,'name':name}
        db.execute('INSERT INTO settings VALUES(?,?)',(key,json.dumps(intent)))
        cid=str(client.make_directory(parent,name))
        if not cid.isdecimal() or cid in ('0',parent):
            raise SafetyError('Invalid created organize directory identity')
        intent['folder_id']=cid
        db.execute('UPDATE settings SET value=? WHERE name=?',(json.dumps(intent),key))
        rows=[f for f in client.list_files(parent) if f.name==name]
    if len(rows)!=1 or not rows[0].is_dir:
        raise SafetyError('Destination directory absent, ambiguous, or occupied by a file')
    candidate=rows[0]
    if intent and intent.get('folder_id') and intent['folder_id']!=candidate.file_id:
        raise SafetyError('Directory differs from recorded mkdir response')
    actual=client.stat(candidate.file_id)
    if not actual.is_dir or actual.file_id!=candidate.file_id or actual.parent_id!=parent or actual.name!=name:
        raise SafetyError('Organize directory identity changed')
    if intent:
        db.execute('DELETE FROM settings WHERE name=?',(key,))
    return actual.file_id


def execute(service,mid,resume=False,recognizer=None):
    service.available()
    if not service.config.auto_organize_enabled:
        raise SafetyError('Automatic organize is disabled')
    with service._maintenance,service.lock(mid):
        db=service.db
        key=f'auto_organize:{mid}'
        saved=db.one('SELECT value FROM settings WHERE name=?',(key,))
        plan=json.loads(saved['value']) if saved else None
        if not plan:
            rendered=preview(service,mid,recognizer)
            media=db.media(mid)
            plan={'target':rendered,'root':service.config.organize_root_cid,'sha1':media.sha1,'size':media.size,'state':'DIRECTORIES'}
            db.execute('INSERT INTO settings VALUES(?,?)',(key,json.dumps(plan)))
            db.log('auto_organize','PLANNED',mid,'Recognition target frozen before remote writes')
        if plan['root']!=service.config.organize_root_cid:
            raise SafetyError('Destination root differs from pending automatic organize plan')
        target=plan['target']
        destination=virtual_path(target['virtual_path'])
        if (PurePosixPath(destination).name!=target['name'] or
                str(PurePosixPath(destination).parent).lstrip('/')!=target['relative_parent']):
            raise SafetyError('Frozen automatic organize path is inconsistent')
        media=db.media(mid)
        if media.sha1!=plan['sha1'] or media.size!=plan['size']:
            raise SafetyError('Media content differs from automatic organize intent')
        if plan['state']=='DONE':
            if media.virtual_path!=target['virtual_path'] or media.file_name!=target['name']:
                raise SafetyError('Completed automatic organize identity changed')
            return media
        if plan['state']=='EXECUTING':
            verify_parent(service,plan['root'],plan['parent_id'],target['relative_parent'])
            remote=db.one('SELECT * FROM organize_plans WHERE media_id=?',(mid,))
            if remote:
                if (remote['new_parent'],remote['new_name'],remote['virtual_path'])!=(plan['parent_id'],target['name'],target['virtual_path']):
                    raise SafetyError('Another organize plan replaced the automatic intent')
                service.reconcile_organize(mid,resume=resume)
                if db.one('SELECT state FROM organize_plans WHERE media_id=?',(mid,))['state']!='DONE':
                    raise SafetyError('Pending move/rename requires explicit automatic organize resume')
            elif resume:
                service.organize(mid,plan['parent_id'],target['name'],target['virtual_path'])
            else:
                raise SafetyError('Automatic organize execution requires explicit resume')
        else:
            if media.source_deleted or media.storage_type!='NORMAL' or service.groups.membership(mid):
                raise SafetyError('Frozen source is no longer an ungrouped retained normal file')
            pending=db.one("SELECT media_id FROM organize_plans WHERE media_id=? AND state<>'DONE'",(mid,))
            if pending:
                raise SafetyError('Another pending organize plan must be reconciled first')
            source=service.client.stat(target['file_id'])
            if (source.sha1.upper(),source.size,source.name,source.parent_id)!=(plan['sha1'].upper(),plan['size'],target['source_name'],target['source_parent']):
                raise SafetyError('Source identity moved or changed before directory creation')
            root=service.client.stat(plan['root'])
            if not root.is_dir or root.file_id!=plan['root']:
                raise SafetyError('Organize destination root is not a directory')
            parent=plan['root']
            for part in target['relative_parent'].split('/') if target['relative_parent'] else []:
                parent=directory(service,parent,part)
            db.execute("DELETE FROM organize_plans WHERE media_id=? AND state='DONE'",(mid,))
            plan.update(state='EXECUTING',parent_id=parent)
            db.execute('UPDATE settings SET value=? WHERE name=?',(json.dumps(plan),key))
            verify_parent(service,plan['root'],parent,target['relative_parent'])
            service.organize(mid,parent,target['name'],target['virtual_path'])
        data=target['identity']
        service.groups.metadata(mid,data['media_type'],data['season'],data['tmdb_id'])
        db.execute('UPDATE media SET title=? WHERE id=?',(data['title'],mid))
        plan['state']='DONE'
        db.execute('UPDATE settings SET value=? WHERE name=?',(json.dumps(plan),key))
        db.log('auto_organize','DONE',mid,'Destination verified; token preserved; no source deletion')
        return db.media(mid)


def mapped_path(service,file,path):
    row=service.db.one('''SELECT s.value FROM settings s JOIN normal_objects n
        ON s.name='auto_organize:' || n.media_id WHERE n.file_id=?''',(file.file_id,))
    if row:
        plan=json.loads(row['value'])
        target=plan['target']
        if (plan['state']=='DONE' and file.parent_id==plan['parent_id'] and file.name==target['name']
            and file.size==plan['size'] and file.sha1.upper()==plan['sha1'].upper()):
            return target['virtual_path']
    return path


def verify_parent(service,root,parent,relative):
    # Recheck the complete destination ancestry at the remote move/resume gate.
    current=parent
    for part in reversed(relative.split('/') if relative else []):
        try:
            folder=service.client.stat(current)
        except MissingFile:
            raise SafetyError('Destination directory ancestry is absent') from None
        if not folder.is_dir or folder.file_id!=current or folder.name!=part:
            raise SafetyError('Destination directory ancestry changed')
        current=folder.parent_id
    if current!=root:
        raise SafetyError('Destination directory escaped configured root')
    try:
        folder=service.client.stat(root)
    except MissingFile:
        raise SafetyError('Destination root is absent') from None
    if not folder.is_dir or folder.file_id!=root:
        raise SafetyError('Destination root identity changed')
