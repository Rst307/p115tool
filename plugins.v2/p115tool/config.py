from __future__ import annotations
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit
import os
import re
from copy import deepcopy
from .organizer import DEFAULT_TEMPLATES


@dataclass
class Config:
    enabled: bool = False
    cookie: str = ""
    data_dir: str = "./data/p115tool"
    strm_dir: str = "./strm"
    public_url: str = "http://localhost:8000"
    playback_prefix: str = "/p115tool"
    api_key: str = ""
    webhook_key: str = ""
    emby_path_mappings: list = field(default_factory=list)
    source_cids: list = field(default_factory=list)
    cache_cid: str = ""
    share_enabled: bool = False
    share_strategy: str = "auto"
    group_settle_seconds: int = 300
    delete_source: bool = False
    safe_mode: bool = True
    auto_archive: bool = False
    auto_repair_share: bool = False
    auto_delete: bool = False
    auto_generate: bool = True
    strm_by_type: bool = False
    strm_by_category: bool = False
    clean_missing_strm: bool = False
    auto_organize_enabled: bool = False
    organize_root_cid: str = ''
    cache_ttl: int = 21600
    cache_max_bytes: int = 300 * 1024**3
    playback_lease: int = 21600
    url_cache_ttl: int = 60
    max_concurrency: int = 2
    request_timeout: int = 30
    scan_interval: int = 3600
    health_batch: int = 20
    account_status_ttl: int = 300
    statistics_utc_offset: int = 480
    allowed_cdn_suffixes: list = field(default_factory=lambda: ["115.com", "115cdn.com", "115cdn.net", "115cdn.cn"])
    media_extensions: list = field(default_factory=lambda: [".mkv", ".mp4", ".avi", ".mov", ".ts", ".m2ts", ".iso", ".wmv"])
    policies: list = field(default_factory=list)
    organize_templates: dict = field(default_factory=lambda: DEFAULT_TEMPLATES.copy())

    @classmethod
    def from_dict(cls, data=None):
        data = deepcopy(dict(data or {}))
        allowed = {f.name for f in fields(cls)}
        cfg = cls(**{k: v for k, v in data.items() if k in allowed})
        if not isinstance(cfg.organize_root_cid,str) or (cfg.organize_root_cid and
                (not re.fullmatch(r'[0-9]+',cfg.organize_root_cid) or int(cfg.organize_root_cid)==0)):
            raise ValueError('organize_root_cid must be a non-root numeric CID')
        if cfg.auto_organize_enabled and not cfg.organize_root_cid:
            raise ValueError('Automatic organize requires an explicit destination root CID')
        from .organizer import validate_templates
        validate_templates(cfg.organize_templates)
        if type(cfg.account_status_ttl) is not int or not 1 <= cfg.account_status_ttl <= 3600:
            raise ValueError('account_status_ttl must be 1..3600 seconds')
        if cfg.auto_repair_share and not cfg.share_enabled:
            raise ValueError('Automatic share repair requires share_enabled')
        if type(cfg.statistics_utc_offset) is not int or not -720 <= cfg.statistics_utc_offset <= 840:
            raise ValueError('statistics_utc_offset must be minutes in -720..840')
        for name in ('cookie','data_dir','strm_dir','public_url','playback_prefix','api_key','webhook_key','cache_cid','share_strategy'):
            value=getattr(cfg,name)
            if not isinstance(value,str) or any(ord(c)<32 or 0xD800<=ord(c)<=0xDFFF for c in value):
                raise ValueError(f'{name} must be a valid string without control characters')
        if not cfg.data_dir.strip() or not cfg.strm_dir.strip():
            raise ValueError('data_dir and strm_dir cannot be empty')
        if cfg.cache_cid and not re.fullmatch(r'[0-9]+',cfg.cache_cid):
            raise ValueError('cache_cid must be numeric')
        if cfg.cache_cid and int(cfg.cache_cid)==0:
            raise ValueError('cache_cid cannot be the drive root')
        for name in ('api_key','webhook_key'):
            if getattr(cfg,name) and len(getattr(cfg,name))<32:
                raise ValueError('Configured keys must have at least 32 characters')
        for f in fields(cls):
            value = getattr(cfg, f.name)
            if isinstance(f.default, bool) and not isinstance(value, bool):
                raise ValueError(f"{f.name} must be a boolean")
        for name in ("cache_ttl", "cache_max_bytes", "playback_lease", "url_cache_ttl", "max_concurrency", "request_timeout", "scan_interval", "health_batch", "group_settle_seconds"):
            value = getattr(cfg, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("source_cids", "allowed_cdn_suffixes", "media_extensions", "policies", "emby_path_mappings"):
            if not isinstance(getattr(cfg, name), list):
                raise ValueError(f"{name} must be a list")
        from .strm import virtual_path
        from .models import SafetyError
        def directory_prefix(value):
            if not isinstance(value,str):
                raise ValueError('Invalid directory prefix')
            if value!='/':
                try:
                    virtual_path(value.rstrip('/'))
                except SafetyError:
                    raise ValueError('Invalid directory prefix') from None
        for root in cfg.source_cids:
            if isinstance(root,dict):
                if set(root)-{'cid','prefix'} or 'cid' not in root:
                    raise ValueError('Invalid source directory fields')
                cid=root['cid']
                prefix=root.get('prefix','/')
                directory_prefix(prefix)
            else:
                cid=root
            if isinstance(cid,bool) or not isinstance(cid,(int,str)) or not re.fullmatch(r'[0-9]+',str(cid)):
                raise ValueError('Source CID must be numeric')
        for suffix in cfg.allowed_cdn_suffixes:
            if not isinstance(suffix,str) or len(suffix)>253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+',suffix):
                raise ValueError('Invalid CDN domain suffix')
            if all(part.isdigit() for part in suffix.split('.')):
                raise ValueError('IP suffixes are not allowed')
        if not cfg.allowed_cdn_suffixes:
            raise ValueError('At least one CDN domain suffix is required')
        if any(not isinstance(ext,str) or not re.fullmatch(r'\.[a-z0-9]+',ext) for ext in cfg.media_extensions):
            raise ValueError('Invalid media extension')
        for rule in cfg.policies:
            if not isinstance(rule,dict) or set(rule)-{'storage','prefix','min_bytes','max_bytes'}:
                raise ValueError('Invalid storage policy fields')
            if rule.get('storage')=='SHARE_VIRTUAL':
                rule['storage']='SHARE'
            if rule.get('storage') not in ('NORMAL','SHARE'):
                raise ValueError('Invalid storage backend')
            prefix=rule.get('prefix','/')
            directory_prefix(prefix)
            minimum=rule.get('min_bytes',0)
            maximum=rule.get('max_bytes',2**63-1)
            if type(minimum) is not int or type(maximum) is not int or not 0<=minimum<=maximum<=2**63-1:
                raise ValueError('Invalid policy size range')
            if rule['storage']=='SHARE' and not cfg.share_enabled:
                raise ValueError('SHARE policy requires share_enabled')
        parsed = urlsplit(cfg.public_url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("public_url must be an absolute HTTP(S) URL without credentials/query/fragment")
        if not cfg.playback_prefix.startswith("/") or cfg.playback_prefix.startswith('//') or any(x in cfg.playback_prefix for x in ("..", "?", "#", "\\")):
            raise ValueError("invalid playback_prefix")
        if not cfg.safe_mode:
            raise ValueError("Safety checks cannot be disabled")
        if cfg.delete_source and not cfg.share_enabled:
            raise ValueError("Source deletion requires share_enabled")
        if cfg.auto_archive and not cfg.share_enabled:
            raise ValueError('auto_archive requires share_enabled')
        if cfg.auto_delete and not (cfg.share_enabled and cfg.delete_source):
            raise ValueError("auto_delete requires share_enabled and delete_source")
        if cfg.share_strategy not in ("file", "movie", "season", "auto"):
            raise ValueError("share_strategy must be file/movie/season/auto")
        if cfg.group_settle_seconds > 86400:
            raise ValueError('group_settle_seconds cannot exceed one day')
        if cfg.max_concurrency > 16:
            raise ValueError("max_concurrency must not exceed 16")
        if cfg.request_timeout>120:
            raise ValueError('request_timeout cannot exceed 120 seconds')
        if cfg.playback_lease < cfg.cache_ttl:
            raise ValueError("playback_lease must be at least cache_ttl")
        cfg.data_dir = str(Path(cfg.data_dir).expanduser().resolve())
        cfg.strm_dir = str(Path(cfg.strm_dir).expanduser().resolve())
        from .emby import media_path
        for mapping in cfg.emby_path_mappings:
            if not isinstance(mapping,dict) or set(mapping)!={'emby','local'}:
                raise ValueError('Path mappings require emby/local')
            media_path(mapping['emby'])
            media_path(mapping['local'])
            if not Path(mapping['local']).resolve().is_relative_to(Path(cfg.strm_dir)):
                raise ValueError('Local mapping must stay within strm_dir')
        cfg.public_url = cfg.public_url.rstrip("/")
        cfg.playback_prefix = cfg.playback_prefix.rstrip("/")
        return cfg

    def storage_policy(self, path, size):
        for rule in self.policies:
            if not isinstance(rule, dict) or rule.get("storage") not in ("NORMAL", "SHARE"):
                raise ValueError("Invalid storage policy")
            prefix = rule.get("prefix", "/").rstrip("/") + "/"
            if (path.startswith(prefix) and size >= int(rule.get("min_bytes", 0))
                    and size <= int(rule.get("max_bytes", 2**63 - 1))):
                return rule["storage"]
        return "SHARE" if self.auto_archive else "NORMAL"

    def playback_url(self, token):
        return f"{self.public_url}{self.playback_prefix}/play/{token}"
