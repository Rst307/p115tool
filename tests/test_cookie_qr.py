from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path
import sys
import tempfile
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from p115tool import P115Tool
from p115tool.native_ui import dispatch, routes


PRIMARY = {'UID': '123_M1_5', 'CID': 'fixture-primary', 'SEID': 'fixture-session'}
BACKUP = {'UID': '123_R2_6', 'CID': 'fixture-backup', 'SEID': 'fixture-session'}
PNG = b'\x89PNG\r\n\x1a\nfixture-image'
QR_CHALLENGE = {'uid': 'offline-fixture', 'sign': 'private-fixture', 'time': 1234}


class CookieQrTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.plugin = P115Tool()
        self.plugin.get_data_path = lambda: self.home / 'data'
        self.plugin.init_plugin({'request_timeout': 42, 'strm_dir': str(self.home / 'strm')})
        self.addCleanup(self.plugin.stop_service)
        self.calls = []
        self.status = 2
        self.primary = PRIMARY
        self.backup = BACKUP
        self.entry = self.enterContext(patch('urllib3_future_request.request', side_effect=self.request))

    def request(self, *, url, parse, **kwargs):
        host, path = urlsplit(url).hostname, urlsplit(url).path
        uid = next((item.value for item in kwargs.get('cookies', ()) if item.name == 'UID'), '')
        self.calls.append((path, uid))
        if path.endswith('/qrcode'):
            self.assertFalse(parse)
            if kwargs.get('params', {}).get('uid') != QR_CHALLENGE['uid']:
                return b'{"state":false,"message":"invalid QR parameters"}'
            return PNG
        if host == 'my.115.com':
            result = {'state': uid in (self.primary['UID'], BACKUP['UID'])}
        elif path.endswith('/token/'):
            result = {'state': True, 'data': QR_CHALLENGE}
        elif path == '/get/status/':
            self.assertEqual({key: kwargs.get('params', {}).get(key) for key in QR_CHALLENGE}, QR_CHALLENGE)
            result = {'code': 0, 'data': {'status': self.status}}
        elif path.endswith(('/prompt.php', '/slogin.php')):
            self.assertEqual(uid, PRIMARY['UID'])
            result = {'state': True, 'data': {}}
        elif path.endswith('/qandroid/1.0/login/qrcode/'):
            result = {'state': True, 'data': {'cookie': self.primary}}
        elif path.endswith('/alipaymini/1.0/login/qrcode/'):
            result = {'state': bool(self.backup), 'data': {'cookie': self.backup}}
        else:
            self.fail('Unexpected 115 endpoint')
        return parse(None, json.dumps(result).encode())

    def scan(self, action='start', scan_id=None):
        body = {'action': action}
        if scan_id is not None:
            body['scan_id'] = scan_id
        return dispatch(self.plugin, 'cookie_qr', body)

    def test_scan_provisions_two_sessions_once_and_keeps_current_config(self):
        start = self.scan()
        self.assertEqual(start['state'], 'WAITING')
        self.assertTrue(start['image'].startswith('data:image/png;base64,'))
        self.assertEqual(set(start), {'state', 'scan_id', 'image'})
        ticket = start['scan_id']
        for status, expected in ((0, 'WAITING'), (1, 'SCANNED'), (2, 'SAVED')):
            self.status = status
            self.assertEqual(self.scan('poll', ticket), {'state': expected})
        initial_calls = list(self.calls)
        self.assertEqual(self.scan('poll', ticket), {'state': 'SAVED'})
        self.assertEqual(self.calls, initial_calls)
        bindings = [path for path, _ in self.calls if path.endswith('/login/qrcode/')]
        self.assertEqual(bindings, ['/app/1.0/qandroid/1.0/login/qrcode/', '/app/1.0/alipaymini/1.0/login/qrcode/'])
        config = self.plugin._saved_config
        self.assertIn(PRIMARY['UID'], config['cookie'])
        self.assertIn(BACKUP['UID'], config['cookie_renewal_seed'])
        self.assertTrue(config['cookie_auto_refresh'])
        self.assertFalse(config['enabled'])
        self.assertEqual(config['request_timeout'], 42)
        _, shown = self.plugin.get_form()
        self.assertTrue(shown['cookie_set'] and shown['cookie_renewal_seed_set'])
        self.assertNotIn('cookie', shown)
        self.assertNotIn('cookie_renewal_seed', shown)
        for field in ('cookie', 'cookie_renewal_seed'):
            shown.pop(field + '_set')
        self.assertEqual(dispatch(self.plugin, 'validate', {'config': shown}), {'valid': True})
        self.plugin.init_plugin(shown)
        self.assertEqual(self.plugin._config.cookie, config['cookie'])
        self.assertEqual(self.plugin._config.cookie_renewal_seed, config['cookie_renewal_seed'])

    def test_partial_success_preserves_moviepilot_mode(self):
        self.plugin.init_plugin({'use_moviepilot_auth': True})
        self.backup = {**BACKUP, 'UID': '345_R2_6'}
        ticket = self.scan()['scan_id']
        self.assertEqual(self.scan('poll', ticket), {'state': 'PRIMARY_ONLY'})
        self.assertTrue(self.plugin._config.use_moviepilot_auth)
        self.assertIn(PRIMARY['UID'], self.plugin._config.cookie)
        self.assertEqual(self.plugin._config.cookie_renewal_seed, '')
        self.assertFalse(self.plugin._config.cookie_auto_refresh)

    def test_different_account_has_no_backup_attempt_and_stores_nothing(self):
        self.plugin.init_plugin({'cookie': 'UID=432_A1_1; CID=existing'})
        ticket = self.scan()['scan_id']
        self.assertEqual(self.scan('poll', ticket), {'state': 'ACCOUNT_MISMATCH'})
        self.assertIn('432_A1_1', self.plugin._config.cookie)
        self.assertFalse(any('alipaymini' in path for path, _ in self.calls))

    def test_expired_invalid_and_concurrent_scan_requests_are_bounded(self):
        with patch('p115tool.cookie_qr.monotonic', return_value=100):
            ticket = self.scan()['scan_id']
            self.assertEqual(self.scan(), {'state': 'BUSY'})
            self.assertEqual(self.scan('poll', 'a' * 24), {'state': 'EXPIRED'})
        with patch('p115tool.cookie_qr.monotonic', return_value=221):
            self.assertEqual(self.scan('poll', ticket), {'state': 'EXPIRED'})
            self.assertNotEqual(self.scan()['scan_id'], ticket)
        self.assertEqual(len(self.calls), 4)

    def test_config_change_blocks_stale_scan_before_exchange(self):
        ticket = self.scan()['scan_id']
        self.plugin.init_plugin({**asdict(self.plugin._config), 'request_timeout': 55})
        self.assertEqual(self.scan('poll', ticket), {'state': 'CONFIG_CHANGED'})
        self.assertEqual(len(self.calls), 2)

    def test_cancel_during_status_read_blocks_exchange(self):
        ticket = self.scan()['scan_id']
        reading, proceed = Event(), Event()

        def check_status(_):
            reading.set()
            if not proceed.wait(3):
                raise TimeoutError
            return 2

        with patch('p115tool.client.P115ClientManager.cookie_qr_status', side_effect=check_status):
            with ThreadPoolExecutor(max_workers=1) as pool:
                poll = pool.submit(self.scan, 'poll', ticket)
                try:
                    self.assertTrue(reading.wait(3))
                    self.assertEqual(self.scan('poll', ticket), {'state': 'BUSY'})
                    self.assertEqual(self.scan('cancel', ticket), {'state': 'CANCELED'})
                finally:
                    proceed.set()
                self.assertEqual(poll.result(3), {'state': 'CANCELED'})
        self.assertEqual(len(self.calls), 2)

    def test_failed_save_does_not_bind_twice_or_change_running_config(self):
        ticket = self.scan()['scan_id']
        with patch.object(self.plugin, 'update_config', return_value=False), self.assertLogs('p115tool', level='ERROR') as log:
            self.assertEqual(self.scan('poll', ticket), {'state': 'SAVE_FAILED'})
        initial_calls = list(self.calls)
        self.assertEqual(self.scan('poll', ticket), {'state': 'SAVE_FAILED'})
        self.assertEqual(self.calls, initial_calls)
        self.assertEqual(self.plugin._config.cookie, '')
        self.assertNotIn(PRIMARY['UID'], str(log.output))

    def test_success_updates_moviepilot_scheduler_for_enabled_cookie_mode(self):
        initial = {**asdict(self.plugin._config), 'enabled': True}
        scheduler = Mock()
        with patch('p115tool.Service'), patch.dict(sys.modules, {'app.scheduler': SimpleNamespace(Scheduler=scheduler)}):
            self.plugin.init_plugin(initial)
            self.assertEqual(self.scan('poll', self.scan()['scan_id']), {'state': 'SAVED'})
            scheduler.return_value.update_plugin_job.assert_called_once_with('P115Tool')
            self.assertIn('p115tool_cookie_check', [job['id'] for job in self.plugin.get_service()])

    def test_route_requires_administrator_and_marks_qr_response_uncacheable(self):
        async def auth(authorization: str = Header('')):
            if not authorization:
                raise HTTPException(401)
            return SimpleNamespace(is_superuser=authorization == 'administrator', is_active=True)

        app = FastAPI()
        with patch('p115tool.native_ui.import_module', return_value=SimpleNamespace(get_current_active_superuser_async=auth)):
            for route in routes(self.plugin):
                app.add_api_route(route['path'], route['endpoint'], methods=route['methods'], dependencies=route['dependencies'])
        client = TestClient(app)
        path = '/native/cookie_qr'
        self.assertEqual(client.post(path, json={'action': 'start'}).status_code, 401)
        self.assertEqual(client.post(path, json={'action': 'start'}, headers={'authorization': 'user'}).status_code, 403)
        self.assertEqual(self.calls, [])
        response = client.post(path, json={'action': 'start'}, headers={'authorization': 'administrator'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertNotIn('private-fixture', response.text)
