"""Conservative virtual-source selection; stable STRMs and copy identities stay intact."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time

from .models import SafetyError, ToolError


def quality(path):
    text = path.lower()
    if re.search(r'(?<![a-z0-9])(?:cam|hdcam|telesync|telecine|hdts|hdtc|dvdscr|sample|trailer)(?![a-z0-9])|预告|样片|枪版', text):
        return (0, 0, 0)
    heights = set()
    for match in re.finditer(r'(?<![a-z0-9])(2160|1080|720|576|480)([pi])(?![a-z0-9])', text):
        heights.add((int(match[1]), int(match[2] == 'p')))
    if re.search(r'(?<![a-z0-9])(?:4k|uhd)(?![a-z0-9])', text): heights.add((2160, 1))
    if len(heights) != 1: return (0, 0, 0)
    source = 0
    if re.search(r'(?<![a-z0-9])remux(?![a-z0-9])', text): source = 5
    elif re.search(r'blu[ ._-]?ray|b[dr]rip|蓝光', text): source = 4
    elif re.search(r'web[ ._-]?dl', text): source = 3
    elif re.search(r'web[ ._-]?rip', text): source = 2
    elif re.search(r'(?<![a-z0-9])hdtv(?![a-z0-9])', text): source = 1
    return (*next(iter(heights)), source)


def better(new, old):
    new, old = tuple(new), tuple(old)
    if not new[0] or not old[0]: return False
    if new[:2] != old[:2]: return new[:2] > old[:2]
    return bool(new[2] and old[2] and new[2] > old[2])


def edition(path):
    # Preserve cuts, languages, 3D and dynamic-range variants instead of silently
    # switching narrative/audio/device compatibility. Size/codec are never rank.
    text = path.lower()
    groups = (
        ('extended', r'extended|加长'), ('unrated', r'unrated|未分级'),
        ('directors', r"director[ ._'’-]*s?[ ._-]*cut|导演剪辑"),
        ('theatrical', r'theatrical|院线版|剧场版'),
        ('imax', r'(?<![a-z0-9])imax(?![a-z0-9])'),
        ('3d', r'(?<![a-z0-9])3d(?![a-z0-9])'), ('disc', r'\.iso$|bdmv'),
        ('dv', r'dolby[ ._-]*vision|(?<![a-z0-9])(?:dv|dovi)(?![a-z0-9])'),
        ('hdr10plus', r'hdr10\+|hdr10plus'),
        ('hdr', r'(?<![a-z0-9])hdr(?:10)?(?![a-z0-9+])'),
        ('国语', r'国语|国配|mandarin'), ('粤语', r'粤语|cantonese'),
        ('双语', r'双语|多语|dual[ ._-]*audio|multi[ ._-]*audio'),
    )
    return ','.join(key for key, pattern in groups if re.search(pattern, text))


def metadata(destination, source):
    identity = getattr(destination, 'identity', '')
    if not isinstance(identity, str) or not re.fullmatch(r'(?:movie:[0-9]+|tv:[0-9]+:[0-9]+:[0-9]+)', identity): identity = ''
    return identity + '|' + edition(source) if identity else '', quality(source)


class VirtualUpgrades:
    def __init__(self, storage):
        self.storage = storage
        self.service = storage.service
        with closing(sqlite3.connect(self.service.path)) as db, db:
            db.executescript('''CREATE TABLE IF NOT EXISTS virtual_editions (
                media_id TEXT PRIMARY KEY,stable_id TEXT NOT NULL,identity TEXT NOT NULL,
                quality TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS virtual_editions_identity ON virtual_editions(identity);
                CREATE TABLE IF NOT EXISTS virtual_selections (
                stable_id TEXT PRIMARY KEY,selected_id TEXT NOT NULL,previous_id TEXT NOT NULL,
                changed_at REAL NOT NULL);''')

    def remember(self, media_id, destination, source):
        identity, rank = metadata(destination, source)
        self.service.query('INSERT OR IGNORE INTO virtual_editions VALUES(?,?,?,?)',
                           (media_id, media_id, identity, json.dumps(rank)))

    def index_existing(self):
        rows = self.service.query("SELECT f.* FROM strm_files f JOIN resource_storage s ON s.media_id=f.file_id "
            "WHERE s.kind='VIRTUAL' AND s.stage='READY' AND substr(f.file_id,1,2)='v_' "
            "AND f.strm_path IS NOT NULL AND NOT EXISTS (SELECT 1 FROM virtual_editions e WHERE e.media_id=f.file_id) "
            "AND NOT EXISTS (SELECT 1 FROM removed_storage_records r WHERE r.media_id=f.file_id)")
        for row in rows:
            if self.service._stop.is_set(): raise InterruptedError()
            source = (Path(row['relative_path']).parent / row['name']).as_posix()
            try: destination = self.service.virtual_organizer(source)
            except Exception: destination = ''
            self.remember(row['file_id'], destination, source)

    def owned_output(self, row):
        path = Path(row['strm_path'] or '')
        root = self.service.strm.root
        if (not row['strm_path'] or path.is_symlink() or not path.resolve().is_relative_to(root)
                or not path.is_file()): raise SafetyError('Upgrade STRM ownership unavailable')
        proof = self.service.query('SELECT * FROM strm_output_proofs WHERE path=?', (str(path),), one=True)
        content = path.read_bytes()
        if (not proof or proof['file_id'] != row['file_id']
                or proof['digest'] != hashlib.sha256(content).hexdigest()
                or content.decode('utf-8').replace('\r\n', '\n') != self.service.config.playback_url(row['token']) + '\n'):
            raise SafetyError('Upgrade STRM ownership unavailable')

    def consider(self, media_id, file, destination, source, code, password, enabled, members):
        """Return a handled outcome, or None for the ordinary initial import."""
        with self.storage._gate:
            recorded = self.service.query('SELECT * FROM virtual_editions WHERE media_id=?', (media_id,), one=True)
            if recorded and recorded['stable_id'] != media_id:
                # Never regenerate a hidden edition into another STRM on reimport.
                self.service.query('UPDATE resource_storage SET password=? WHERE media_id=?', (password,media_id))
                return 'skipped'
            if not enabled: return None
            identity, rank = metadata(destination, source)
            if not identity: return None
            owners = self.service.query("SELECT e.* FROM virtual_editions e JOIN resource_storage s ON s.media_id=e.media_id "
                "WHERE e.identity=? AND e.media_id=e.stable_id AND s.kind='VIRTUAL' AND s.stage='READY' "
                "AND NOT EXISTS (SELECT 1 FROM removed_storage_records r WHERE r.media_id=e.media_id)", (identity,))
            # Existing duplicates are ambiguous; never choose one by title or path.
            if len(owners) != 1 or owners[0]['media_id'] == media_id: return None
            owner = owners[0]
            selection = self.service.query('SELECT * FROM virtual_selections WHERE stable_id=?', (owner['media_id'],), one=True)
            selected = selection['selected_id'] if selection else owner['media_id']
            current = self.service.query('SELECT * FROM virtual_editions WHERE media_id=?', (selected,), one=True)
            if not current or not better(rank, json.loads(current['quality'])): return 'skipped'
            stable = self.storage.row(owner['media_id'])
            self.owned_output(stable)
            if self.service.query("SELECT media_id FROM resource_copies WHERE media_id IN (?,?) "
                    "AND stage NOT IN ('READY','EMPTY')", (owner['media_id'], selected), one=True):
                raise SafetyError('Upgrade blocked by copy checkpoint')
            existing = self.storage.row(media_id)
            if existing:
                # An independently emitted STRM needs manual duplicate resolution.
                raise SafetyError('Upgrade candidate already registered')
            candidate = {'share_code':code, 'password':password, 'share_fid':file.file_id,
                         'name':file.name, 'size':file.size, 'sha1':file.sha1}
            # Read-only metadata and range validation; never receive or delete here.
            # The just-completed full enumeration proves identity without
            # rereading an entire season for every episode being upgraded.
            if members.get((file.name,file.size,file.sha1)) != [file.file_id]:
                raise SafetyError('Upgrade source identity changed')
            self.storage.probe_share_member(candidate, file.file_id)
            if self.service._stop.is_set(): raise InterruptedError()
            self.owned_output(stable)
            with self.service._lock, closing(sqlite3.connect(self.service.path)) as db, db:
                db.execute('INSERT INTO strm_files VALUES(?,?,?,?,?,?,?,?,NULL)',
                    (media_id, secrets.token_urlsafe(32), '', file.name, file.size, file.sha1, '0', str(destination)))
                db.execute("INSERT INTO resource_storage(media_id,kind,stage,share_code,password,share_fid) "
                           "VALUES(?,'VIRTUAL','READY',?,?,?)", (media_id, code, password, file.file_id))
                db.execute('INSERT INTO virtual_editions VALUES(?,?,?,?)',
                           (media_id, owner['media_id'], identity, json.dumps(rank)))
                db.execute('INSERT INTO virtual_selections VALUES(?,?,?,?) ON CONFLICT(stable_id) DO UPDATE SET '
                    'selected_id=excluded.selected_id,previous_id=excluded.previous_id,changed_at=excluded.changed_at',
                    (owner['media_id'], media_id, selected, time.time()))
            return 'upgraded'

    def playback_source(self, stable_id):
        """At most one read-only fallback; no retry after a transfer write."""
        selected = self.service.query('SELECT * FROM virtual_selections WHERE stable_id=?', (stable_id,), one=True)
        if not selected: return stable_id
        media_id = selected['selected_id']
        copy = self.storage.copy(media_id)
        if copy and copy['stage'] not in ('EMPTY',): return media_id
        try: self.storage.verify_share(self.storage.row(media_id))
        except ToolError as exc:
            if getattr(exc, 'upstream_code', None) in (99, 990001, 911): raise
            previous = selected['previous_id']
            previous_copy = self.storage.copy(previous)
            if not previous_copy or previous_copy['stage'] != 'READY':
                self.storage.verify_share(self.storage.row(previous))
            return previous
        return media_id
