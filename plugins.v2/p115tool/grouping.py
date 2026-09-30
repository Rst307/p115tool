from __future__ import annotations
from contextlib import ExitStack
import hashlib
import json
import re
import time
from .models import SafetyError


class ShareGroups:
    """Immutable share snapshots: member list is fixed before any remote write."""
    def __init__(self, service):
        self.service, self.db = service, service.db

    def metadata(self, mid, media_type, season=None, tmdb_id=None):
        self.service.available()
        if media_type not in ('MOVIE', 'TV', 'UNKNOWN'):
            raise ValueError('Invalid media type')
        if season is not None and (type(season) is not int or not 0 <= season <= 999):
            raise ValueError('Invalid season')
        if tmdb_id is not None and (type(tmdb_id) is not int or tmdb_id < 1):
            raise ValueError('Invalid TMDB ID')
        with self.service._maintenance:
            self.db.media(mid)
            self.db.execute('INSERT INTO media_metadata VALUES(?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET media_type=excluded.media_type,season=excluded.season,updated_at=excluded.updated_at', (mid, media_type, season, time.time()))
            if tmdb_id:
                self.db.execute('UPDATE media SET tmdb_id=? WHERE id=?', (tmdb_id, mid))

    def key(self, mid):
        media = self.db.media(mid)
        info = self.db.one('SELECT * FROM media_metadata WHERE media_id=?', (mid,)) or {}
        strategy = self.service.config.share_strategy
        if strategy != 'file' and media.tmdb_id:
            if info.get('media_type') == 'MOVIE' and strategy in ('movie', 'auto'):
                return f'movie:{media.tmdb_id}'
            if info.get('media_type') == 'TV' and info.get('season') is not None and strategy in ('season', 'auto'):
                return f"season:{media.tmdb_id}:{info['season']}"
        # Missing identity must never group unrelated files merely by directory.
        return f'file:{mid}'

    def membership(self, mid):
        return self.db.one('SELECT g.* FROM share_groups g JOIN media_groups m ON m.group_id=g.id WHERE m.media_id=?', (mid,))

    def members(self, gid):
        return self.db.all('SELECT * FROM share_group_members WHERE group_id=? ORDER BY media_id', (gid,))

    def public(self, gid):
        group = self.db.one('SELECT * FROM share_groups WHERE id=?', (gid,))
        if not group:
            raise KeyError('Group not found')
        return {'id': gid, 'label': group['label'], 'state': group['state'], 'error': group['error'],
                'repair_pending': bool(self.db.one('SELECT name FROM settings WHERE name=?',(f'group_repair:{gid}',))),
                'media_ids': [row['media_id'] for row in self.members(gid)], 'updated_at': group['updated_at']}

    def state(self, gid, state, error=None):
        self.db.execute('UPDATE share_groups SET state=?,error=?,updated_at=? WHERE id=?', (state, error, time.time(), gid))
        self.db.log('share_group', state, detail=f'group_id={gid}')

    def _preflight(self, ids):
        if (not isinstance(ids, list) or not ids or len(ids) > 1000 or
                any(type(mid) is not int or mid < 1 for mid in ids) or len(set(ids)) != len(ids)):
            raise ValueError('Group requires 1..1000 distinct positive media IDs')
        rows = []
        for mid in sorted(ids):
            media = self.db.media(mid)
            from .auto_organize import archive_guard
            archive_guard(self.service, mid)
            if self.db.one("SELECT media_id FROM organize_plans WHERE media_id=? AND state<>'DONE'", (mid,)):
                raise SafetyError('Pending organize plan blocks group archive')
            if media.status in ('SOURCE_DELETING', 'FAILED_DELETE'):
                raise SafetyError('Source deletion requires reconciliation')
            if media.status in ('SHARE_CREATING','FAILED_SHARE') and not self.db.one('SELECT media_id FROM share_objects WHERE media_id=?',(mid,)):
                raise SafetyError('Individual share creation must be reconciled before regrouping')
            if not re.fullmatch(r'[A-Fa-f0-9]{40}', media.sha1):
                raise SafetyError('Group members require full SHA1')
            source = self.service.normal(mid)
            rows.append({'media_id':mid, 'file_id':source['file_id'], 'name':media.file_name, 'size':media.size, 'sha1':media.sha1})
        # Duplicate identity in a share would be ambiguous to the verifier.
        identities = [(r['name'], r['size'], r['sha1']) for r in rows]
        if len(set(identities)) != len(identities):
            raise SafetyError('Duplicate share identities must be archived separately')
        return rows

    def _snapshot(self, rows, label):
        fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
        old = self.db.one('SELECT * FROM share_groups WHERE fingerprint=?', (fingerprint,))
        if old:
            if any((self.membership(row['media_id']) or {}).get('id') != old['id'] for row in rows):
                raise SafetyError('Historical group snapshot is no longer the active membership')
            return old
        ids = {r['media_id'] for r in rows}
        for mid in ids:
            current = self.membership(mid)
            if current:
                if current['state'] != 'READY':
                    raise SafetyError('Member belongs to a pending group')
                if not {r['media_id'] for r in self.members(current['id'])} <= ids:
                    raise SafetyError('Cannot split an existing group without explicit recovery')
            if self.db.media(mid).source_deleted:
                raise SafetyError('Creating a new snapshot requires retained sources')
        now = time.time()
        with self.db.connect() as db:
            gid = db.execute('INSERT INTO share_groups(fingerprint,label,state,created_at,updated_at) VALUES(?,?,\'PLANNED\',?,?)', (fingerprint,label,now,now)).lastrowid
            for row in rows:
                db.execute('INSERT INTO share_group_members VALUES(?,?,?,?,?,?)', (gid,row['media_id'],row['file_id'],row['name'],row['size'],row['sha1']))
                db.execute('INSERT INTO media_groups VALUES(?,?) ON CONFLICT(media_id) DO UPDATE SET group_id=excluded.group_id', (row['media_id'],gid))
        return self.db.one('SELECT * FROM share_groups WHERE id=?', (gid,))

    def _assign_share(self, gid, code, password):
        now = time.time()
        with self.db.connect() as db:
            db.execute("UPDATE share_groups SET share_code=?,receive_code=?,state='SHARED',updated_at=?,error=NULL WHERE id=?", (code,password,now,gid))
            for row in self.members(gid):
                mid = row['media_id']
                parent = self.service.normal(mid)['parent_id']
                db.execute('INSERT INTO share_objects VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET share_code=excluded.share_code,receive_code=excluded.receive_code,file_id=excluded.file_id,parent_id=excluded.parent_id,sha1=excluded.sha1,size=excluded.size,name=excluded.name,verified_at=NULL,range_verified_at=NULL',
                    (mid,code,password,row['file_id'],parent,row['sha1'],row['size'],row['name'],now,None,None))
        for row in self.members(gid):
            self.service.invalidate(row['media_id'])

    def verify_all(self, gid):
        group = self.db.one('SELECT * FROM share_groups WHERE id=?', (gid,))
        for row in self.members(gid):
            mid = row['media_id']
            media = self.db.media(mid)
            if (media.file_name != row['name'] or media.size != row['size'] or media.sha1 != row['sha1']):
                raise SafetyError('Frozen group membership identity changed')
            if self.service.share(mid)['share_code'] != group['share_code']:
                raise SafetyError('Group member points to another share')
            self.service.verify_share(mid, deep=True)
            if not self.service.strm.verify(mid):
                raise SafetyError('Group member STRM is not verified')

    def archive(self, ids, delete=False, label='manual', generate_strm=True):
        self.service.available()
        if not self.service.config.share_enabled:
            raise SafetyError('Share backend disabled')
        if delete and not generate_strm:
            raise SafetyError('Deletion requires STRM safety stage')
        if delete and not self.service.config.delete_source:
            raise SafetyError('Source deletion disabled')
        with self.service._maintenance, ExitStack() as locks:
            rows = self._preflight(ids)
            for mid in sorted(ids):
                locks.enter_context(self.service.lock(mid))
            group = self._snapshot(rows, label)
            gid = group['id']
            if group['state'] in ('CREATING','FAILED_SHARE','DELETING','FAILED_DELETE'):
                raise SafetyError('Uncertain group write requires reconciliation')
            stage = 'FAILED_VERIFY'
            try:
                for row in rows:
                    media = self.db.media(row['media_id'])
                    if not media.source_deleted:
                        source = self.service.client.stat(row['file_id'])
                        if not self.service.expected(media).matches(source):
                            raise SafetyError('Group source identity changed')
                if not group['share_code']:
                    stage = 'FAILED_SHARE'
                    self.state(gid, 'CREATING')
                    code,password = self.service.client.create_share([r['file_id'] for r in rows])
                    self._assign_share(gid,code,password)
                    self.db.metric('shares_created')
                stage = 'FAILED_VERIFY'
                self.service.client.ensure_share_retention(self.service.share(rows[0]['media_id'])['share_code'])
                self.state(gid, 'VERIFYING')
                for row in rows:
                    mid = row['media_id']
                    self.db.transition(mid, 'VERIFYING')
                    self.service.verify_share(mid,deep=True)
                    self.db.transition(mid,'VERIFIED')
                stage = 'FAILED_STRM'
                for row in rows:
                    mid = row['media_id']
                    if generate_strm:
                        self.service.strm.generate(mid)
                        if not self.service.strm.verify(mid):
                            raise SafetyError('Group STRM failed')
                        self.db.transition(mid,'STRM_CREATED',storage='SHARE')
                # Barrier: no source can be deleted before every member passes.
                if generate_strm:
                    self.verify_all(gid)
                self.state(gid,'VERIFIED')
                if delete:
                    stage='FAILED_DELETE'
                    self.state(gid,'DELETING')
                    for row in rows:
                        mid=row['media_id']
                        if not self.db.media(mid).source_deleted:
                            try:
                                self.service._delete_source(mid, group_id=gid)
                            except Exception:
                                self.db.transition(mid,'FAILED_DELETE','Group deletion needs reconciliation')
                                raise
                        self.db.transition(mid,'READY',storage='SHARE')
                else:
                    for row in rows:
                        self.db.transition(row['media_id'],'READY',storage='SHARE')
                self.state(gid,'READY')
                return self.public(gid)
            except Exception as exc:
                self.state(gid,stage,type(exc).__name__+': group operation failed')
                raise

    def policy(self, mid, allow_delete=False):
        with self.service._maintenance:
            media = self.db.media(mid)
            if media.source_deleted:
                group=self.membership(mid)
                return self.public(group['id']) if group else media.public()
            key = self.key(mid)
            ids = [r['id'] for r in self.db.all('SELECT id FROM media WHERE source_deleted=0 ORDER BY id')
                   if self.key(r['id']) == key and self.service.config.storage_policy(self.db.media(r['id']).virtual_path,self.db.media(r['id']).size)=='SHARE']
            if mid not in ids:
                return media.public()
            if key.startswith('file:'):
                return self.service.archive(mid, delete=allow_delete and self.service.config.auto_delete).public()
            permitted = all(bool((self.db.one('SELECT allow_delete FROM archive_permissions WHERE media_id=?',(member,)) or {}).get('allow_delete')) for member in ids)
            return self.archive(ids,delete=allow_delete and self.service.config.auto_delete and permitted,label=key)

    def attach(self, gid, code, password):
        self.service.available()
        if not re.fullmatch(r'[A-Za-z0-9]+',code) or not re.fullmatch(r'[A-Za-z0-9]{4}',password):
            raise ValueError('Invalid share credentials')
        with self.service._maintenance:
            group=self.db.one('SELECT * FROM share_groups WHERE id=?',(gid,))
            if not group or group['state'] not in ('CREATING','FAILED_SHARE'):
                raise SafetyError('Only uncertain share creation can be attached')
            for row in self.members(gid):
                media=self.db.media(row['media_id'])
                if media.source_deleted:
                    raise SafetyError('Cannot replace a group with deleted sources')
                self.service.client.find_share_file(code,password,self.service.expected(media))
            self._assign_share(gid,code,password)
            return self.archive([r['media_id'] for r in self.members(gid)],label=group['label'])

    def reconcile(self, gid):
        self.service.available()
        with self.service._maintenance:
            group=self.db.one('SELECT * FROM share_groups WHERE id=?',(gid,))
            if not group or group['state'] not in ('DELETING','FAILED_DELETE'):
                raise SafetyError('No uncertain group deletion')
            for row in self.members(gid):
                mid=row['media_id']
                if self.db.media(mid).status in ('SOURCE_DELETING','FAILED_DELETE'):
                    self.service.reconcile_delete(mid)
            self.verify_all(gid)
            for row in self.members(gid):
                self.db.transition(row['media_id'],'READY',storage='SHARE')
            self.state(gid,'READY')
            return self.public(gid)
