import hashlib
import json
from pathlib import Path
import re

from .private_files import private_write


def cookie_identity(cookie):
    if not isinstance(cookie, str) or len(cookie) > 16384 or not cookie.isascii() or any(ord(c) < 32 for c in cookie):
        return None
    values = re.findall(r'(?:^|;)\s*UID\s*=([^;]*)', cookie)
    if len(values) != 1:
        return None
    identity = re.fullmatch(r'([1-9][0-9]{0,19})_([A-Z][0-9])_[0-9]+', values[0].strip())
    return identity.groups() if identity else None


class CookieRenewal:
    """Local state for the configured Cookie, including the last remote attempt."""

    def __init__(self, config):
        self.path = Path(config.data_dir) / '115-cookie-renewal.json'
        self.source = hashlib.sha256(config.cookie.encode()).hexdigest()
        self.record = {'source': self.source, 'cookie': '', 'attempt_at': 0, 'renewed_at': 0}
        self.state = 'UNCHECKED'
        self.next_check = 0.0
        if config.cookie and not config.use_moviepilot_auth:
            self._load(config.cookie)

    def _load(self, configured):
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 32768:
                return
            record = json.loads(self.path.read_text('utf-8'))
        except (OSError, UnicodeError, ValueError):
            return
        if not isinstance(record, dict) or record.get('source') != self.source:
            return
        for key in ('attempt_at', 'renewed_at'):
            if type(record.get(key)) is int and record[key] >= 0:
                self.record[key] = record[key]
        identity = cookie_identity(configured)
        if identity and cookie_identity(record.get('cookie')) == identity:
            self.record['cookie'] = record['cookie']

    def save(self, **changes):
        updated = {**self.record, **changes}
        private_write(self.path, json.dumps(updated))
        self.record = updated
