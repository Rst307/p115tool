import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from p115tool.config import Config
from p115tool.service import Service
from p115tool.telegram_web import PublicChannelError
from test_telegram import Drive
from test_telegram_web import preview


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.drive = Drive()
        self.service = Service(Config(data_dir=str(root/'data'), strm_dir=str(root/'strm')), client=self.drive)
        self.monitor = self.service.telegram

    def tearDown(self):
        self.service.close()
        self.assertEqual(self.drive.calls, [])
        self.temp.cleanup()

    def check(self):
        self.assertEqual(self.monitor.check_network()['state'], 'NETWORK_CHECK_SCHEDULED')
        self.monitor._network_future.result(3)
        return self.monitor.snapshot()['network']

    def test_disabled_initially_unknown_without_network_or_thread(self):
        self.assertEqual(self.monitor.snapshot()['network']['state'], 'UNKNOWN')
        self.assertIsNone(self.monitor._thread)
        self.assertFalse(self.monitor.settings_path.exists())

    def test_web_uses_saved_proxy_fixed_telegram_page_and_closes(self):
        self.monitor.settings['proxy'] = 'socks5://user:secret@localhost:1080'
        reader = AsyncMock()
        with patch('p115tool.telegram_web.PublicChannelReader', return_value=reader) as factory:
            result = self.check()
        factory.assert_called_once_with(self.monitor.settings['proxy'])
        reader.page.assert_awaited_once_with('@telegram')
        reader.close.assert_awaited_once()
        self.assertEqual(result['state'], 'AVAILABLE')
        self.assertTrue(result['via_proxy'])
        self.assertGreater(result['checked_at'], 0)
        self.assertGreaterEqual(result['latency_ms'], 0)
        self.assertNotIn('secret', json.dumps(self.monitor.snapshot()))
        self.assertEqual(self.monitor._state, 'DISABLED')
        self.assertFalse(self.monitor.settings_path.exists())

    def test_web_validates_identity_and_rejects_captive_portal_and_redirect(self):
        client_factory = httpx.AsyncClient
        for response, expected in [
            (httpx.Response(200, text=preview([], username='telegram'), headers={'content-type':'text/html'}), 'AVAILABLE'),
            (httpx.Response(200, text='<html>Login to Wi-Fi</html>', headers={'content-type':'text/html'}), 'UNAVAILABLE'),
            (httpx.Response(302, headers={'location':'http://127.0.0.1/secret'}), 'UNAVAILABLE'),
        ]:
            seen = []
            def handler(request):
                seen.append(request)
                return response
            def factory(**kwargs):
                return client_factory(**kwargs, transport=httpx.MockTransport(handler))
            self.monitor._network_requested_at = None
            with patch('httpx.AsyncClient', side_effect=factory):
                result = self.check()
            self.assertEqual(result['state'], expected)
            self.assertEqual([str(request.url) for request in seen], ['https://t.me/s/telegram'])

    def test_errors_are_safe_and_leave_subscription_state_untouched(self):
        self.monitor._state = 'LOGIN_REQUIRED'; self.monitor._error = 'CODE_EXPIRED'
        with patch.object(self.monitor, '_probe_network', AsyncMock(side_effect=OSError('socks5://user:secret@proxy:1080'))):
            result = self.check()
        self.assertEqual(result['state'], 'UNAVAILABLE')
        self.assertEqual(result['error'], 'NETWORK_FAILED')
        self.assertNotIn('secret', json.dumps(self.monitor.snapshot()))
        self.assertEqual((self.monitor._state, self.monitor._error), ('LOGIN_REQUIRED', 'CODE_EXPIRED'))

    def test_account_checks_actual_api_before_login_without_writing_session(self):
        self.monitor.settings.update(mode='account', api_id=123, api_hash='a'*32)
        client = AsyncMock()
        with patch.object(self.monitor, '_connect', AsyncMock(return_value=client)):
            result = self.check()
        self.assertEqual(result['state'], 'AVAILABLE')
        self.assertEqual(result['mode'], 'account')
        self.assertEqual(type(client.call_args.args[0]).__name__, 'GetConfigRequest')
        client.sign_in.assert_not_called(); client.send_code_request.assert_not_called()
        self.assertFalse(self.monitor._authorized)
        self.assertFalse(self.monitor.session_path.exists())

    def test_account_without_credentials_does_not_attempt_connection(self):
        self.monitor.settings['mode'] = 'account'
        with patch.object(self.monitor, '_probe_network', AsyncMock()) as probe:
            result = self.check()
        self.assertEqual(result['state'], 'SETTINGS_REQUIRED')
        self.assertEqual(result['error'], 'API_REQUIRED')
        probe.assert_not_called()

    def test_timeout_is_bounded_and_cancels_probe(self):
        cancelled = threading.Event()
        async def wait():
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        with patch.object(self.monitor, '_probe_network', wait), patch('p115tool.telegram.NETWORK_TIMEOUT', .01):
            result = self.check()
        self.assertEqual(result['error'], 'NETWORK_TIMEOUT')
        self.assertTrue(cancelled.is_set())

    def test_rate_limits_are_preserved_for_manual_and_auto_checks(self):
        with patch.object(self.monitor, '_probe_network', AsyncMock(side_effect=PublicChannelError('RATE_LIMITED', 3600))) as probe:
            self.assertEqual(self.check()['state'], 'RATE_LIMITED')
            self.monitor._network_requested_at = None
            self.assertEqual(self.monitor.check_network()['state'], 'RATE_LIMITED')
            self.assertEqual(probe.await_count, 1)
        self.monitor._network = self.monitor._network_unknown()
        self.monitor._rate_until = self.monitor._network_next
        self.assertEqual(self.monitor.check_network()['state'], 'RATE_LIMITED')

    def test_repeated_clicks_use_cooldown(self):
        with patch.object(self.monitor, '_probe_network', AsyncMock()) as probe:
            self.check()
            self.assertEqual(self.monitor.check_network()['state'], 'NETWORK_CHECK_COOLDOWN')
            self.assertEqual(probe.await_count, 1)

    def test_inflight_check_is_shared_and_shutdown_cancels_it(self):
        entered, cancelled = threading.Event(), threading.Event()
        async def wait():
            entered.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        with patch.object(self.monitor, '_probe_network', wait):
            self.monitor.check_network()
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.monitor.snapshot()['network']['state'], 'CHECKING')
            self.monitor.check_network()
            self.service.close()
            self.assertTrue(cancelled.is_set())
            self.assertFalse(self.monitor._thread.is_alive())

    def test_changing_connection_settings_invalidates_previous_result(self):
        with patch.object(self.monitor, '_probe_network', AsyncMock()): self.check()
        self.monitor.configure({'mode':'web', 'proxy':'http://localhost:8080'})
        result = self.monitor.snapshot()['network']
        self.assertEqual(result['state'], 'UNKNOWN')
        self.assertIsNone(result['checked_at'])
        self.assertTrue(result['via_proxy'])
        self.assertIsNone(self.monitor._network_requested_at)

    def test_enabled_subscriptions_check_automatically_and_do_not_probe_each_poll(self):
        completed = threading.Event()
        async def probe(): completed.set()
        with patch.object(self.monitor, '_probe_network', probe), patch.object(self.monitor, '_poll', AsyncMock()):
            self.monitor.configure({'mode':'web', 'enabled':True, 'channels':['@example']})
            self.assertTrue(completed.wait(3))
            self.monitor._network_future.result(3)
            self.assertEqual(self.monitor.snapshot()['network']['state'], 'AVAILABLE')
            self.assertGreater(self.monitor._network_next, self.monitor.snapshot()['network']['checked_at'] + 299)


if __name__ == '__main__': unittest.main()
