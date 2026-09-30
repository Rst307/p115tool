from __future__ import annotations
from pathlib import Path, PurePosixPath
import os
import re
import tempfile
from .models import SafetyError


def virtual_path(value):
    if not isinstance(value, str) or not value.startswith("/") or "\\" in value:
        raise SafetyError("Virtual path must be an absolute POSIX path")
    components = value[1:].split("/")
    if not components or any(p in ("", ".", "..") or any(ord(c) < 32 for c in p) for p in components):
        raise SafetyError("Unsafe virtual path")
    if any(re.search(r'[<>:"|?*]', p) or p.endswith((".", " ")) for p in components):
        raise SafetyError("Virtual path contains non-portable characters")
    if any(re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", p, re.I) for p in components):
        raise SafetyError("Reserved path component")
    return "/" + "/".join(components)


class StrmManager:
    def __init__(self, config, db):
        self.config, self.db = config, db
        self.root = Path(config.strm_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def target(self, media):
        relative = PurePosixPath(virtual_path(media.virtual_path).lstrip("/"))
        from .classification import folders
        classified = folders(self.config, self.db, media)
        # Retain original extension to avoid movie.mkv and movie.mp4 colliding.
        target = self.root.joinpath(*classified, *relative.parts[:-1], relative.name + ".strm")
        if not target.resolve().is_relative_to(self.root):
            raise SafetyError("STRM path escaped output root")
        return target

    def generate(self, mid):
        media = self.db.media(mid)
        from .classification import folders
        folders(self.config, self.db, media, refresh=True)
        target = self.target(media)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.parent.resolve().is_relative_to(self.root):
            raise SafetyError("STRM parent escaped root")
        content = self.config.playback_url(media.token) + "\n"
        owner = self.db.one("SELECT id FROM media WHERE strm_path=? AND id<>?", (str(target), mid))
        if owner:
            raise SafetyError("STRM path already owned by another media object")
        if target.exists() and target.read_text(encoding="utf-8") != content:
            raise SafetyError("Refusing to overwrite an unowned/edited STRM")
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as out:
                temp = Path(out.name)
                out.write(content)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, target)
            self.db.execute("UPDATE media SET strm_path=? WHERE id=?", (str(target), mid))
            self.db.metric("strm_generated")
            self.db.log('strm_generate', 'DONE', mid, 'STRM written atomically')
        finally:
            if temp and temp.exists():
                temp.unlink()
        # Old paths are left intact until their content proves ownership.
        if media.strm_path and Path(media.strm_path) != target:
            old = Path(media.strm_path)
            if old.resolve().is_relative_to(self.root) and old.is_file() and old.read_text(encoding="utf-8") == content:
                old.unlink()
        return str(target)

    def verify(self, mid):
        media = self.db.media(mid)
        target = self.target(media)
        return (media.strm_path == str(target) and target.is_file()
                and target.read_text(encoding="utf-8") == self.config.playback_url(media.token) + "\n")

    def repair(self, mid):
        if self.verify(mid):
            return self.db.media(mid).strm_path
        return self.generate(mid)

    def clean_broken(self, media_ids=None):
        removed = []
        allowed = None if media_ids is None else set(media_ids)
        for row in self.db.all("""SELECT m.id FROM media m
            JOIN missing_sources x ON x.media_id=m.id
            JOIN normal_objects n ON n.media_id=m.id AND n.file_id=x.file_id
            WHERE m.status='BROKEN' AND m.error='Source missing (scan confirmed)'
            AND m.source_deleted=0 AND m.storage_type='NORMAL'
            AND NOT EXISTS(SELECT 1 FROM share_objects s WHERE s.media_id=m.id)
            AND NOT EXISTS(SELECT 1 FROM cache_objects c WHERE c.media_id=m.id)
            AND NOT EXISTS(SELECT 1 FROM organize_plans p WHERE p.media_id=m.id AND p.state<>'DONE')"""):
            media = self.db.media(row["id"])
            if allowed is not None and media.id not in allowed:
                continue
            if self.verify(media.id):
                Path(media.strm_path).unlink()
                self.db.execute("UPDATE media SET strm_path=NULL WHERE id=?", (media.id,))
                removed.append(media.id)
        return removed
