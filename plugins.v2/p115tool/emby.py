from __future__ import annotations
import hashlib
import re
import time
from urllib.parse import urlsplit

EVENTS={
    'PlaybackStart':'PlaybackStart','playback.start':'PlaybackStart',
    'PlaybackStop':'PlaybackStop','playback.stop':'PlaybackStop',
    'playback.progress':'PlaybackProgress','PlaybackProgress':'PlaybackProgress',
    'playback.resume':'PlaybackProgress','playback.pause':'PlaybackProgress',
}


def media_path(value):
    if not isinstance(value,str) or not value or len(value)>4096 or any(ord(c)<32 for c in value):
        raise ValueError('Invalid media path')
    value=value.replace('\\','/')
    if not (value.startswith('/') or re.match(r'^[A-Za-z]:/',value)):
        raise ValueError('Media path must be absolute')
    if any(part in ('.','..') for part in value.split('/')):
        raise ValueError('Path traversal is not allowed')
    return value.rstrip('/')


def windows_path(value):
    return bool(re.match(r'^[A-Za-z]:/',value)) or value.startswith('//')


class EmbyBridge:
    def __init__(self,service):
        self.service,self.db=service,service.db

    def resolve_path(self,value):
        if not isinstance(value,str):
            raise ValueError('Invalid source path')
        parsed=urlsplit(value)
        if parsed.scheme.lower() in ('http','https'):
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                return None
            match=re.search(r'/(?:play|redirect)/([A-Za-z0-9_-]{40,64})$',parsed.path)
            if not match:
                return None
            token=match.group(1)
            expected=urlsplit(self.service.config.playback_url(token))
            if (parsed.scheme.lower()!=expected.scheme.lower() or parsed.netloc.lower()!=expected.netloc.lower()
                    or parsed.path not in (expected.path,expected.path.replace('/play/','/redirect/'))):
                return None
            row=self.db.one('SELECT id FROM media WHERE token=?',(token,))
            return row['id'] if row else None
        # Windows drive letters are not URL schemes. Other schemes are unmanaged.
        if parsed.scheme and not re.match(r'^[A-Za-z]:[\\/]',value):
            return None
        path=media_path(value)
        candidates=[path]
        for mapping in self.service.config.emby_path_mappings:
            origin=media_path(mapping['emby'])
            target=media_path(mapping['local'])
            compare=path.lower() if windows_path(origin) else path
            prefix=origin.lower() if windows_path(origin) else origin
            if compare.startswith(prefix+'/'):
                candidates.append(target+path[len(origin):])
        found=set()
        for candidate in candidates:
            if windows_path(candidate):
                rows=self.db.all("SELECT id FROM media WHERE lower(replace(strm_path,char(92),'/'))=lower(?)",(candidate,))
            else:
                rows=self.db.all("SELECT id FROM media WHERE replace(strm_path,char(92),'/')=?",(candidate,))
            found.update(row['id'] for row in rows)
        if len(found)>1:
            raise ValueError('Ambiguous path mapping')
        return next(iter(found)) if found else None

    def handle(self,payload):
        self.service.available()
        if not isinstance(payload,dict):
            raise ValueError('Webhook payload must be an object')
        native=payload.get('Event')
        normalized=payload.get('event')
        if native is not None and normalized is not None and EVENTS.get(native)!=EVENTS.get(normalized):
            raise ValueError('Conflicting events')
        raw_event=native if native is not None else normalized
        if not isinstance(raw_event,str) or len(raw_event)>128:
            raise ValueError('Invalid event name')
        if raw_event=='system.webhooktest':
            return {'accepted':True,'test':True}
        event=EVENTS.get(raw_event)
        if not event:
            return {'accepted':False,'reason':'ignored_event'}
        item=payload.get('Item',{}) or {}
        session=payload.get('Session',{}) or {}
        server=payload.get('Server',{}) or {}
        if not all(isinstance(value,dict) for value in (item,session,server)):
            raise ValueError('Invalid webhook metadata')
        identifiers=set()
        if 'token' in payload:
            token=payload['token']
            if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{40,64}',token):
                raise ValueError('Invalid playback identity')
            row=self.db.one('SELECT id FROM media WHERE token=?',(token,))
            if row:
                identifiers.add(row['id'])
        paths=[]
        if item.get('Path'):
            paths.append(item['Path'])
        sources=item.get('MediaSources',[])
        if not isinstance(sources,list) or len(sources)>32:
            raise ValueError('Invalid media sources')
        for source in sources:
            if not isinstance(source,dict):
                raise ValueError('Invalid media source')
            if source.get('Path'):
                paths.append(source['Path'])
        for path in paths:
            mid=self.resolve_path(path)
            if mid:
                identifiers.add(mid)
        session_id=session.get('Id',payload.get('SessionId'))
        server_id=server.get('Id','')
        key=None
        if session_id is not None:
            if (not isinstance(session_id,str) or not session_id or len(session_id)>256
                    or not isinstance(server_id,str) or len(server_id)>256):
                raise ValueError('Invalid session identity')
            key=hashlib.sha256((server_id+'\0'+session_id).encode()).hexdigest()
        if len(identifiers)>1:
            raise ValueError('Conflicting playback identities')
        previous=self.db.one('SELECT * FROM emby_sessions WHERE session_key=?',(key,)) if key else None
        if not identifiers and previous and event!='PlaybackStart' and previous['updated_at']+self.service.config.playback_lease>=time.time():
            identifiers.add(previous['media_id'])
        if not identifiers:
            return {'accepted':False,'reason':'unmanaged_media'}
        mid=next(iter(identifiers))
        if previous and event!='PlaybackStart' and previous['media_id']!=mid:
            raise ValueError('Session does not match selected media')
        now=time.time()
        self.service.touch(mid)
        if key:
            state='STOPPED' if event=='PlaybackStop' else 'ACTIVE'
            self.db.execute('INSERT INTO emby_sessions VALUES(?,?,?,?) ON CONFLICT(session_key) DO UPDATE SET media_id=excluded.media_id,state=excluded.state,updated_at=excluded.updated_at',(key,mid,state,now))
        self.db.log('emby',event,mid)
        self.db.metric('emby_events')
        # No raw event, user, server, session, path or token is logged/returned.
        return {'accepted':True}
