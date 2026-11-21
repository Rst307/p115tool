from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from p115tool import P115Tool
from p115tool.client import P115ClientManager
from p115tool.config import Config
from p115tool.models import RemoteError
from p115tool.native_ui import dispatch
from p115tool.service import Service


def sample_cookie(user, device, stamp):
    return f'UID={user}_{device}_{stamp}; CID=fixture-cid-{stamp}; SEID=fixture-seid-{stamp}'


class CookieRenewalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.original = sample_cookie(123, 'A1', 1)
        self.backup = sample_cookie(123, 'F1', 2)
        self.rotated = sample_cookie(123, 'A1', 3)
        self.issued = self.rotated
        self.primary_active = False
        self.backup_active = True
        self.calls = []
        self.transport = self.enterContext(patch('urllib3_future_request.request', side_effect=self.request))
        self.config = Config.from_dict({'enabled': True, 'data_dir': str(self.root / 'data'),
                                       'strm_dir': str(self.root / 'strm'), 'cookie': self.original,
                                       'cookie_auto_refresh': True, 'cookie_renewal_seed': self.backup})
        Path(self.config.data_dir).mkdir()
        self.manager = self.create_manager()

    def create_manager(self, config=None):
        manager = P115ClientManager(config or self.config)
        self.addCleanup(manager.close)
        return manager

    def request(self, *, url, parse, **kwargs):
        path = urlsplit(url).path
        uid = next((cookie.value for cookie in kwargs.get('cookies', ()) if cookie.name == 'UID'), '')
        self.calls.append((path, uid))
        if urlsplit(url).hostname == 'my.115.com':
            status = {'123_A1_1': self.primary_active, '123_F1_2': self.backup_active}.get(uid, True)
            return parse(None, json.dumps({'state': status} if status is not None else {'unknown': True}).encode())
        if path.endswith('/token/'):
            result = {'state': True, 'data': {'uid': 'offline-test-channel'}}
        elif path.endswith(('/prompt.php', '/slogin.php')):
            self.assertEqual(uid, '123_F1_2')
            result = {'state': True, 'data': {}}
        elif path.endswith('/login/qrcode/'):
            self.assertIn('/web/', path)
            result = {'state': True, 'data': {'cookie': self.issued}}
        else:
            result = {'state': False, 'errno': 990001} if uid == '123_A1_1' else {'state': True, 'data': []}
        return parse(None, json.dumps(result).encode())

    def test_expired_read_rotates_once_and_reuses_verified_cookie_after_restart(self):
        self.assertTrue(self.manager.call('fs_files', {'cid': '0'})['state'])
        self.assertEqual([uid for path, uid in self.calls if path.endswith('/files')], ['123_A1_1', '123_A1_3'])
        self.assertEqual(self.manager.cookie_status()['state'], 'RENEWED')
        self.assertNotIn(self.rotated, str(self.manager.cookie_status()))
        self.assertEqual(sum(path.endswith('/login/qrcode/') for path, _ in self.calls), 1)
        self.config.cookie_auto_refresh = False
        restarted = self.create_manager()
        self.assertTrue(restarted.call('fs_files', {'cid': '0'})['state'])
        self.assertEqual(self.calls[-1][1], '123_A1_3')
        self.config.cookie = sample_cookie(123, 'A1', 4)
        replaced = self.create_manager()
        self.assertTrue(replaced.call('fs_files', {'cid': '0'})['state'])
        self.assertEqual(self.calls[-1][1], '123_A1_4')

    def test_mutating_calls_never_trigger_renewal_or_replay(self):
        for method, args in (
            ('fs_delete', ('20',)),
            ('share_receive', ({'share_code': 'fixture', 'file_id': '7', 'cid': '9'},)),
            ('recyclebin_clean', ({'tid': '17'},)),
        ):
            with self.subTest(method=method), self.assertRaises(RemoteError):
                self.manager.call(method, *args)
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(all(uid == '123_A1_1' for _, uid in self.calls))

    def test_a_healthy_primary_does_not_rotate_due_to_an_endpoint_rejection(self):
        self.primary_active = True
        with self.assertRaises(RemoteError):
            self.manager.call('fs_files', {'cid': '0'})
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.manager.cookie_status()['state'], 'VALID')

    def test_unrecognized_status_or_invalid_backup_does_not_start_login(self):
        self.primary_active = None
        self.manager.check_cookie()
        self.assertEqual(self.manager.cookie_status()['state'], 'STATUS_UNAVAILABLE')
        self.assertEqual(len(self.calls), 1)
        self.primary_active = False
        self.backup_active = False
        manager = self.create_manager()
        self.calls.clear()
        manager.check_cookie()
        self.assertEqual(manager.cookie_status()['state'], 'BACKUP_EXPIRED')
        self.assertEqual(len(self.calls), 2)

    def test_wrong_account_result_is_rejected_and_cooldown_survives_restart(self):
        self.issued = sample_cookie(789, 'A1', 3)
        self.manager.check_cookie()
        self.assertEqual(self.manager.cookie_status()['state'], 'RENEWAL_FAILED')
        record = json.loads(self.manager._cookie_renewal.path.read_bytes())
        self.assertEqual(record['cookie'], '')
        self.calls.clear()
        self.issued = self.rotated
        restarted = self.create_manager()
        restarted.check_cookie()
        self.assertEqual(restarted.cookie_status()['state'], 'COOLDOWN')
        self.assertEqual(len(self.calls), 1)

    def test_disk_failure_prevents_starting_login(self):
        with patch('p115tool.cookie_renewal.private_write', side_effect=OSError('fixture disk unavailable')):
            self.manager.check_cookie()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.manager.cookie_status()['state'], 'PERSISTENCE_FAILED')

    def test_concurrent_reads_share_the_verified_rotation(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(self.manager.call, 'fs_files', {'cid': '0'}) for _ in range(2)]
            self.assertTrue(all(future.result(5)['state'] for future in results))
        self.assertEqual(sum(path.endswith('/login/qrcode/') for path, _ in self.calls), 1)
        self.assertEqual([uid for path, uid in self.calls if path.endswith('/files')],
                         ['123_A1_1', '123_A1_3', '123_A1_3'])

    def test_config_requires_same_account_distinct_devices_and_masks_saved_backup(self):
        for seed in (self.original, sample_cookie(456, 'F1', 2), '', 'invalid'):
            with self.subTest(seed=seed), self.assertRaises(ValueError):
                Config.from_dict({'cookie_auto_refresh': True, 'cookie': self.original, 'cookie_renewal_seed': seed})
        plugin = P115Tool()
        plugin._config = self.config
        service = Service(self.config, client=self.manager)
        self.addCleanup(service.close)
        plugin._service = service
        _, form = plugin.get_form()
        self.assertTrue(form['cookie_renewal_seed_set'])
        self.assertNotIn('cookie_renewal_seed', form)
        self.assertEqual(dispatch(plugin, 'validate', {'config': form}), {'valid': True})
        self.assertEqual([job['id'] for job in plugin.get_service()], ['p115tool_cookie_check'])
        self.assertEqual(dispatch(plugin, 'data', {'kind': 'cookie_status'}), {'state': 'UNCHECKED', 'renewed_at': None})
        self.config.use_moviepilot_auth = True
        plugin.run_cookie_check()
        self.assertEqual(plugin.get_service(), [])
        self.transport.assert_not_called()
