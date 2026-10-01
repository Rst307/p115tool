from dataclasses import dataclass, field, fields
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

@dataclass
class Config:
    enabled: bool = False
    cookie: str = ''
    data_dir: str = './data/p115tool'
    strm_dir: str = './strm'
    public_url: str = 'http://localhost:3000'
    playback_prefix: str = '/api/v1/plugin/P115Tool'
    source_cids: list = field(default_factory=list)
    organize_cids: list = field(default_factory=list)
    scheduled: bool = False
    organize_scheduled: bool = False
    organize_time: str = '02:00'
    auto_after_transfer: bool = False
    scan_time: str = '03:00'
    request_timeout: int = 30
    allowed_cdn_suffixes: list = field(default_factory=lambda: ['115.com','115cdn.com','115cdn.net','115cdn.cn'])
    media_extensions: list = field(default_factory=lambda: ['.mkv','.mp4','.avi','.mov','.ts','.m2ts','.iso','.wmv','.flv','.m4v','.mpg','.mpeg'])

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict): raise ValueError('Invalid config')
        data = dict(data)
        if 'source_cids_json' in data:
            data['source_cids'] = json.loads(data.pop('source_cids_json'))
        data = {k:v for k,v in data.items() if k in {f.name for f in fields(cls)}}
        for k in ('enabled','scheduled','organize_scheduled','auto_after_transfer'):
            if k in data and type(data[k]) is not bool: raise ValueError('Invalid boolean')
        if 'request_timeout' in data:
            v=data['request_timeout']
            if isinstance(v,str) and re.fullmatch(r'\d{1,3}',v): v=int(v)
            if type(v) is not int or not 5<=v<=120: raise ValueError('Invalid timeout')
            data['request_timeout']=v
        cfg=cls(**data)
        for k in ('cookie','data_dir','strm_dir','public_url','playback_prefix','scan_time','organize_time'):
            if not isinstance(getattr(cfg,k),str): raise ValueError('Invalid text')
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
