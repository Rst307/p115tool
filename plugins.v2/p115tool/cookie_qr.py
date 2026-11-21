from base64 import b64encode
from dataclasses import dataclass, field
import re
from secrets import compare_digest, token_urlsafe
from threading import Event, Lock
from time import monotonic

from .client import P115ClientManager
from .config import Config
from .cookie_renewal import cookie_identity


QR_TTL_SECONDS = 120
QR_START_INTERVAL = 4


@dataclass
class CookieScan:
    scan_id: str
    expires_at: float
    config: Config
    challenge: dict | None
    result: dict
    canceled: Event = field(default_factory=Event)


class CookieQrLogin:
    def __init__(self, plugin):
        self._plugin = plugin
        self._lock = Lock()
        self._scan = None
        self._next_start = 0.0

    def dispatch(self, payload):
        action = payload.get('action')
        if action == 'start' and set(payload) == {'action'}:
            scan_id = None
        elif action in ('poll', 'cancel') and set(payload) == {'action', 'scan_id'}:
            scan_id = payload['scan_id']
            if not isinstance(scan_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{24}', scan_id):
                raise ValueError('Invalid scan identifier')
        else:
            raise ValueError('Invalid scan request')
        if action == 'cancel':
            scan = self._scan
            if not scan or not compare_digest(scan_id, scan.scan_id):
                return {'state': 'EXPIRED'}
            scan.canceled.set()
            return {'state': 'CANCELED'}
        if not self._lock.acquire(blocking=False):
            return {'state': 'BUSY'}
        try:
            if action == 'start':
                return self._start()
            return self._advance(scan_id)
        finally:
            self._lock.release()

    def _start(self):
        now = monotonic()
        active = (self._scan and self._scan.challenge and not self._scan.canceled.is_set()
                  and now < self._scan.expires_at)
        if active or now < self._next_start:
            return {'state': 'BUSY'}
        config = self._plugin._qr_config()
        if config is None:
            return {'state': 'CONFIG_CHANGED'}
        self._next_start = now + QR_START_INTERVAL
        try:
            challenge, png = P115ClientManager.start_cookie_qr()
        except Exception:
            return {'state': 'QR_UNAVAILABLE'}
        self._scan = CookieScan(token_urlsafe(18), now + QR_TTL_SECONDS, config, challenge, {'state': 'WAITING'})
        return {**self._scan.result, 'scan_id': self._scan.scan_id,
                'image': 'data:image/png;base64,' + b64encode(png).decode('ascii')}

    def _advance(self, scan_id):
        scan = self._scan
        if not scan or not compare_digest(scan_id, scan.scan_id):
            return {'state': 'EXPIRED'}
        if scan.challenge is None:
            return scan.result
        if scan.canceled.is_set():
            return self._finish(scan, 'CANCELED')
        if monotonic() >= scan.expires_at:
            return self._finish(scan, 'EXPIRED')
        if self._plugin._qr_config() is not scan.config:
            return self._finish(scan, 'CONFIG_CHANGED')
        try:
            status = P115ClientManager.cookie_qr_status(scan.challenge)
        except Exception:
            return {'state': 'POLL_UNAVAILABLE'}
        if status == -2 or scan.canceled.is_set():
            return self._finish(scan, 'CANCELED')
        if status == -1 or monotonic() >= scan.expires_at:
            return self._finish(scan, 'EXPIRED')
        if status in (0, 1):
            scan.result = {'state': 'SCANNED' if status == 1 else 'WAITING'}
            return scan.result

        challenge = scan.challenge
        self._finish(scan, 'LOGIN_FAILED')
        known = cookie_identity(scan.config.cookie)
        try:
            primary, backup = P115ClientManager.finish_cookie_qr(challenge, known[0] if known else None)
        except Exception:
            return scan.result
        if not primary:
            return self._finish(scan, 'ACCOUNT_MISMATCH')
        if scan.canceled.is_set():
            return self._finish(scan, 'CANCELED')
        scan.result = self._plugin._save_qr_cookies(scan.config, primary, backup)
        return scan.result

    @staticmethod
    def _finish(scan, state):
        scan.challenge = None
        scan.result = {'state': state}
        return scan.result
