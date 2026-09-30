"""MoviePilot identity adapter and portable, constrained organize templates."""
from pathlib import PurePosixPath
from string import Formatter
import re
from .models import SafetyError, ToolError
from .strm import virtual_path

DEFAULT_TEMPLATES = {
    'MOVIE':'电影/{title} ({year})/{title} ({year}){ext}',
    'TV':'电视剧/{title} ({year})/Season {season:02d}/{title} - S{season:02d}{episode_tag}{ext}'
}
FIELDS={'title','year','tmdb_id','season','episode','episode_tag','ext'}


def value(obj, name, default=None):
    return obj.get(name,default) if isinstance(obj,dict) else getattr(obj,name,default)


def integer(raw, low, high):
    if isinstance(raw,bool) or not isinstance(raw,(int,str)) or not re.fullmatch(r'[0-9]+',str(raw)):
        raise SafetyError('Recognition requires unambiguous numeric identity')
    if not low<=int(raw)<=high:
        raise SafetyError('Recognition identity is out of bounds')
    return int(raw)


def portable_title(raw):
    if not isinstance(raw,str) or not raw.strip() or len(raw)>200:
        raise SafetyError('Recognition title is missing or too long')
    if any(ord(c)<32 or 0xD800<=ord(c)<=0xDFFF for c in raw):
        raise SafetyError('Recognition title contains unsafe characters')
    # Filename sanitation, not path concatenation of upstream text.
    title=re.sub(r'[\\/<>:"|?*]','_',raw).strip().rstrip('. ')
    if not title or title in ('.','..'):
        raise SafetyError('Recognition title cannot form a portable filename')
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)',title,re.I):
        title='_'+title
    return title


def validate_templates(templates):
    if not isinstance(templates,dict) or set(templates)!={'MOVIE','TV'}:
        raise ValueError('Organize templates require MOVIE and TV')
    for kind,template in templates.items():
        if not isinstance(template,str) or not 1<=len(template)<=1000 or template.startswith('/'):
            raise ValueError('Organize template must be a bounded relative path')
        fields=set()
        try:
            for literal,name,spec,conversion in Formatter().parse(template):
                if name is None:
                    continue
                if name not in FIELDS or conversion or spec not in ('','02d','03d'):
                    raise ValueError('Unsafe organize template expression')
                if spec and name not in ('season','episode','tmdb_id'):
                    raise ValueError('Numeric template format used for non-numeric field')
                fields.add(name)
            if not {'title','ext'}<=fields or (kind=='TV' and
                    ('season' not in fields or not fields.intersection({'episode','episode_tag'}))):
                raise ValueError('Organize template omits required identity fields')
            probe=template.format(title='Title',year='2000',tmdb_id=1,season=1,episode=1,episode_tag='E01',ext='.mkv')
            virtual_path('/'+probe)
            if not probe.endswith('.mkv'):
                raise ValueError('Organize template must retain extension at end')
        except (SafetyError,KeyError,IndexError):
            raise ValueError('Unsafe organize template path') from None


class MoviePilotRecognizer:
    def recognize(self,path):
        try:
            from app.chain.media import MediaChain
            context=MediaChain().recognize_by_path(path=path,obtain_images=False)
        except Exception:
            raise ToolError('MoviePilot recognition is unavailable') from None
        return identity(context)


def identity(context):
    media=value(context,'media_info')
    meta=value(context,'meta_info')
    if not media:
        raise SafetyError('MoviePilot did not recognize a media identity')
    kind=value(media,'type')
    kind=getattr(kind,'name',kind)
    kind={'MOVIE':'MOVIE','Movie':'MOVIE','电影':'MOVIE','TV':'TV','电视剧':'TV'}.get(kind)
    if not kind:
        raise SafetyError('Recognized media type is unsupported')
    data={'media_type':kind,'title':portable_title(value(media,'title')),
        'year':str(integer(value(media,'year'),1800,2200)),
        'tmdb_id':integer(value(media,'tmdb_id'),1,2**63-1),'season':None,'episode':None,
        'episode_end':None,'episode_tag':''}
    if kind=='TV':
        data['season']=integer(value(meta,'begin_season'),0,999)
        data['episode']=integer(value(meta,'begin_episode'),1,9999)
        end_season=value(meta,'end_season')
        if end_season is not None and integer(end_season,0,999)!=data['season']:
            raise SafetyError('Multi-season source cannot use a single-season destination')
        end_episode=value(meta,'end_episode')
        end=integer(end_episode,1,9999) if end_episode is not None else data['episode']
        if end<data['episode']:
            raise SafetyError('Episode range is reversed')
        data['episode_end']=end
        data['episode_tag']=f"E{data['episode']:02d}" + (f'-E{end:02d}' if end!=data['episode'] else '')
    return data


def preview(service,mid,recognizer=None):
    service.available()
    media=service.db.media(mid)
    if media.source_deleted or media.storage_type!='NORMAL':
        raise SafetyError('Automatic organize requires a retained normal source')
    if service.groups.membership(mid):
        raise SafetyError('Grouped snapshot cannot be renamed by automatic organize')
    if service.db.one("SELECT media_id FROM organize_plans WHERE media_id=? AND state<>'DONE'",(mid,)):
        raise SafetyError('Existing organize plan must be reconciled first')
    source=service.client.stat(service.normal(mid)['file_id'])
    if not service.expected(media).matches(source):
        raise SafetyError('Source identity changed before recognition')
    identified=(recognizer or MoviePilotRecognizer()).recognize(media.virtual_path)
    # Injected providers follow the same schema and cannot smuggle template keys.
    canonical=identity({'media_info':{'type':identified.get('media_type'),'title':identified.get('title'),
        'year':identified.get('year'),'tmdb_id':identified.get('tmdb_id')},
        'meta_info':{'begin_season':identified.get('season'),'begin_episode':identified.get('episode'),
            'end_season':identified.get('season_end'),'end_episode':identified.get('episode_end')}})
    ext=PurePosixPath(source.name).suffix
    if ext.lower() not in service.config.media_extensions:
        raise SafetyError('Unsupported media extension for automatic organize')
    validate_templates(service.config.organize_templates)
    template=service.config.organize_templates[canonical['media_type']]
    if (canonical['media_type']=='TV' and canonical['episode_end']!=canonical['episode'] and
            'episode_tag' not in {name for _,name,_,_ in Formatter().parse(template.rsplit('/',1)[-1])}):
        raise SafetyError('Multi-episode source requires episode_tag in the filename template')
    fields={'title':canonical['title'],'year':canonical['year'],'tmdb_id':canonical['tmdb_id'],
        'season':canonical['season'] or 0,'episode':canonical['episode'] or 0,
        'episode_tag':canonical['episode_tag'],'ext':ext}
    destination=virtual_path('/'+template.format(**fields))
    name=PurePosixPath(destination).name
    if len(name)>255 or len(destination)>4096 or PurePosixPath(name).suffix!=ext:
        raise SafetyError('Rendered organize path is too long or changes extension')
    if service.db.one('SELECT id FROM media WHERE virtual_path=? AND id<>?',(destination,mid)):
        raise SafetyError('Rendered virtual destination already belongs to another media')
    return {'media_id':mid,'file_id':source.file_id,'source_parent':source.parent_id,
        'source_name':source.name,'name':name,'virtual_path':destination,'relative_parent':str(PurePosixPath(destination).parent).lstrip('/'),
        'identity':canonical,'execution_ready':False}
