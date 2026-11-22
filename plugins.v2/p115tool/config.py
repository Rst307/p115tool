from dataclasses import dataclass, field, fields
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from .cookie_renewal import cookie_identity

@dataclass
class Config:
    enabled: bool = False
    cookie: str = ''
    cookie_auto_refresh: bool = False
    cookie_renewal_seed: str = ''
    use_moviepilot_auth: bool = False
    data_dir: str = './data/p115tool'
    strm_dir: str = './strm'
    public_url: str = 'http://localhost:3000'
    playback_prefix: str = '/api/v1/plugin/P115Tool'
    source_cids: list = field(default_factory=list)
    organize_cids: list = field(default_factory=list)
    organize_target_cid: str = ''
    scheduled: bool = False
    organize_scheduled: bool = False
    organize_time: str = '02:00'
    auto_after_transfer: bool = False
    scan_time: str = '03:00'
    request_timeout: int = 30
    temp_cid: str = ''
    temp_days: int = 7
    temp_cleanup: bool = True
    recycle_purge: bool = False
    recycle_password: str = ''
    recycle_empty_scheduled: bool = False
    recycle_empty_time: str = '04:00'
    emby_proxy_enabled: bool = False
    emby_url: str = ''
    emby_proxy_port: int = 8095
    emby_proxy_direct_path: bool = False
    allowed_cdn_suffixes: list = field(default_factory=lambda: ['115.com','115cdn.com','115cdn.net','115cdn.cn'])
    media_extensions: list = field(default_factory=lambda: ['.mkv','.mp4','.avi','.mov','.ts','.m2ts','.iso','.wmv','.flv','.m4v','.mpg','.mpeg'])

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict): raise ValueError('Invalid config')
        data = dict(data)
        if 'source_cids_json' in data:
            data['source_cids'] = json.loads(data.pop('source_cids_json'))
        data = {k:v for k,v in data.items() if k in {f.name for f in fields(cls)}}
        for k in ('enabled','cookie_auto_refresh','use_moviepilot_auth','scheduled','organize_scheduled','auto_after_transfer','temp_cleanup','recycle_purge','recycle_empty_scheduled','emby_proxy_enabled','emby_proxy_direct_path'):
            if k in data and type(data[k]) is not bool: raise ValueError('Invalid boolean')
        if 'emby_proxy_port' in data:
            v=data['emby_proxy_port']
            if isinstance(v,str) and re.fullmatch(r'\d{1,5}',v): v=int(v)
            if type(v) is not int or not 1024<=v<=65535: raise ValueError('Invalid emby proxy port')
            data['emby_proxy_port']=v
        if 'request_timeout' in data:
            v=data['request_timeout']
            if isinstance(v,str) and re.fullmatch(r'\d{1,3}',v): v=int(v)
            if type(v) is not int or not 5<=v<=120: raise ValueError('Invalid timeout')
            data['request_timeout']=v
        cfg=cls(**data)
        if not isinstance(cfg.organize_target_cid,str) or (cfg.organize_target_cid and not re.fullmatch(r'[1-9][0-9]{0,19}',cfg.organize_target_cid)):
            raise ValueError('Invalid organize target')
        if not isinstance(cfg.recycle_password,str) or (cfg.recycle_password and not re.fullmatch(r'[0-9]{6}',cfg.recycle_password)):
            raise ValueError('Invalid recycle security key')
        if cfg.recycle_empty_scheduled and not cfg.recycle_password:
            raise ValueError('Recycle security key required')
        if not isinstance(cfg.recycle_empty_time,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',cfg.recycle_empty_time):
            raise ValueError('Invalid recycle empty time')
        if not isinstance(cfg.temp_cid,str) or (cfg.temp_cid and not re.fullmatch(r'[1-9][0-9]{0,19}',cfg.temp_cid)):
            raise ValueError('Invalid temporary directory')
        if isinstance(cfg.temp_days,str) and re.fullmatch(r'\d{1,3}',cfg.temp_days): cfg.temp_days=int(cfg.temp_days)
        if type(cfg.temp_days) is not int or not 1<=cfg.temp_days<=365: raise ValueError('Invalid retention days')
        for k in ('cookie','cookie_renewal_seed','data_dir','strm_dir','public_url','playback_prefix','scan_time','organize_time','emby_url'):
            if not isinstance(getattr(cfg,k),str): raise ValueError('Invalid text')
        cfg.emby_url=cfg.emby_url.strip().rstrip('/')
        if cfg.emby_proxy_enabled and not cfg.emby_url:
            raise ValueError('Emby URL required when proxy is enabled')
        if cfg.emby_url:
            eurl=urlsplit(cfg.emby_url)
            if eurl.scheme not in ('http','https') or not eurl.hostname or eurl.username or eurl.password or eurl.query or eurl.fragment or any(ord(c)<33 for c in cfg.emby_url):
                raise ValueError('Invalid Emby URL')
        if cfg.cookie_auto_refresh and not cfg.use_moviepilot_auth:
            primary = cookie_identity(cfg.cookie)
            seed = cookie_identity(cfg.cookie_renewal_seed)
            if not primary or not seed or primary[0] != seed[0] or primary[1] == seed[1]:
                raise ValueError('Cookie renewal requires two devices on the same 115 account')
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',cfg.scan_time): raise ValueError('Invalid time')
        if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',cfg.organize_time): raise ValueError('Invalid time')
        cfg.public_url=cfg.public_url.rstrip('/')
        url=urlsplit(cfg.public_url)
        if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or url.query or url.fragment or any(ord(c)<33 for c in cfg.public_url): raise ValueError('Invalid public URL')
        if not re.fullmatch(r'/[A-Za-z0-9_/-]+',cfg.playback_prefix): raise ValueError('Invalid prefix')
        if not cfg.strm_dir or not cfg.data_dir or Path(cfg.strm_dir).resolve()==Path(cfg.data_dir).resolve(): raise ValueError('Invalid directories')
        for source_field in ('source_cids','organize_cids'):
            if not isinstance(getattr(cfg,source_field),list) or len(getattr(cfg,source_field))>100: raise ValueError('Invalid sources')
            sources=[]
            for source in getattr(cfg,source_field):
                if isinstance(source,(str,int)) and not isinstance(source,bool): source={'cid':str(source),'prefix':'/'}
                if not isinstance(source,dict) or set(source)-{'cid','prefix'}: raise ValueError('Invalid source')
                cid=str(source.get('cid','')); prefix=source.get('prefix','/')
                if not re.fullmatch(r'\d{1,20}',cid) or not isinstance(prefix,str): raise ValueError('Invalid source')
                from .strm import safe_parts
                if not prefix.startswith('/'): raise ValueError('Invalid source path')
                if prefix!='/':
                    try: safe_parts(prefix.strip('/'))
                    except Exception: raise ValueError('Invalid source path') from None
                if cid not in {s['cid'] for s in sources}: sources.append({'cid':cid,'prefix':prefix})
            setattr(cfg,source_field,sources)
        for k in ('media_extensions','allowed_cdn_suffixes'):
            values=getattr(cfg,k)
            if not isinstance(values,list) or not values or any(not isinstance(v,str) for v in values): raise ValueError('Invalid list')
        if any(not re.fullmatch(r'\.[a-z0-9]{1,8}',v) for v in cfg.media_extensions): raise ValueError('Invalid extensions')
        if any(not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,}',v) for v in cfg.allowed_cdn_suffixes): raise ValueError('Invalid CDN domains')
        return cfg

    def playback_url(self,token):
        return f'{self.public_url}{self.playback_prefix}/play/{token}'
