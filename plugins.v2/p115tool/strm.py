"""STRM paths, local classification and atomic output. Never organizes remote files."""
from pathlib import Path
import os
import re
import tempfile
from .models import SafetyError

TYPES={'电影':'电影','Movies':'电影','movies':'电影','Movie':'电影','电视剧':'电视剧','TV':'电视剧','TV Shows':'电视剧','剧集':'电视剧','动漫':'动漫'}

def safe_parts(path):
    parts=path.split('/')
    if not parts or any(not p or p in ('.','..') or p.endswith((' ','.')) or re.search(r'[<>:"\\|?*\x00-\x1f]',p) or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)',p,re.I) for p in parts):
        raise SafetyError('Unsafe STRM path')
    return parts

def recognize(path):
    from app.chain.media import MediaChain
    context=MediaChain().recognize_by_path(path=path,obtain_images=False)
    info=getattr(context,'media_info',None)
    kind=getattr(info,'type',None); kind=getattr(kind,'name',kind)
    kind={'MOVIE':'电影','TV':'电视剧','电影':'电影','电视剧':'电视剧'}.get(kind,'未识别')
    category=getattr(info,'category',None) or '未分类'
    safe_parts(str(category))
    if '/' in str(category): raise SafetyError('Invalid category')
    return kind,str(category)

def classify(prefix,relative,recognizer=recognize):
    parts=safe_parts(relative)
    context=[p for p in prefix.split('/') if p]+parts[:-1]
    # An already organized source hierarchy is authoritative, including a selected
    # root such as /电影/外语电影. Do not duplicate it after classification.
    for i,p in enumerate(context):
        if p in TYPES:
            return '/'.join([TYPES[p],*context[i+1:],parts[-1]])
    try: kind,category=recognizer('/'.join([prefix.rstrip('/'),relative]))
    except Exception: kind,category='未识别','未分类'
    return '/'.join([kind,category,*parts])

class StrmManager:
    def __init__(self,config):
        self.config=config
        self.root=Path(config.strm_dir).resolve()
        self.root.mkdir(parents=True,exist_ok=True)

    def generate(self,relative,token):
        parts=safe_parts(relative)
        target=self.root.joinpath(*parts[:-1],parts[-1]+'.strm')
        if target.is_symlink() or not target.resolve().is_relative_to(self.root): raise SafetyError('Unsafe output target')
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.parent.resolve().is_relative_to(self.root): raise SafetyError('Unsafe output parent')
        content=self.config.playback_url(token)+'\n'
        if target.exists():
            if target.read_text('utf-8')!=content: raise SafetyError('Existing STRM differs')
            return str(target)
        temp=None
        try:
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=target.parent,delete=False) as output:
                temp=Path(output.name); output.write(content); output.flush(); os.fsync(output.fileno())
            # Atomic create without overwriting a file created concurrently.
            os.link(temp,target)
        finally:
            if temp and temp.exists(): temp.unlink()
        return str(target)

    def remove_owned(self, old_path, new_path, token):
        old, new = Path(old_path), Path(new_path)
        if old.resolve() == new.resolve():
            return 'SAME_PATH'
        if (old == new or old.suffix.lower() != '.strm' or old.is_symlink()
                or not old.resolve().is_relative_to(self.root)
                or not new.resolve().is_relative_to(self.root)):
            return 'RETAINED'
        content = self.config.playback_url(token) + '\n'
        if not new.is_file() or new.is_symlink() or new.read_text('utf-8') != content:
            return 'RETAINED'
        if not old.exists():
            return 'ABSENT'
        if not old.is_file() or old.read_text('utf-8') != content:
            return 'RETAINED'
        old.unlink()
        # Only empty directories beneath the configured output root are removed.
        parent = old.parent
        while parent != self.root and parent.resolve().is_relative_to(self.root):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return 'DONE'
