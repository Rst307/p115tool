import asyncio
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient, Client, MockTransport, ReadTimeout, Response

from p115tool.api import routes
from p115tool.client import P115ClientManager
from p115tool.config import Config
from p115tool.models import DownloadLink, RemoteFile
from p115tool.service import Service


TOKEN = 'fixture-playback-identifier'
LINK = 'https://cdn.115.com/fixture'


class CookieVirtualPlaybackTests(unittest.TestCase):
    def setUp(self):
        from p115client import P115Client

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = Config.from_dict({'enabled': True, 'data_dir': str(root / 'data'),
                                   'strm_dir': str(root / 'strm'), 'temp_cid': '90'})
        sdk = P115Client('UID=1_A1_1; CID=fixture; SEID=fixture', console_qrcode=False)
        manager = P115ClientManager(config, sdk)
        self.assertTrue(manager._use_httpx_transport(sdk))
        manager._http = Client(transport=MockTransport(self.request_115))
        self.service = Service(config, manager)
        self.addCleanup(self.service.close)
        self.calls = []
        self.members = []
        self.folder_count = 0
        self.folder_name = 'p115tool-fixture'
        self.token = self.service.register(RemoteFile('v_fixture', 'video.mkv', 32, 'A' * 40, '0'), 'video.mkv')
        self.service.query("INSERT INTO resource_storage(media_id,kind,stage,share_code,password,share_fid,own) "
                           "VALUES('v_fixture','VIRTUAL','READY','fixture','abcd','7',0)")
        self.service.query("INSERT INTO resource_copies(media_id,root_cid,folder_name,folder_cid,stage) "
                           "VALUES('v_fixture','90',?,'101','EMPTY')", (self.folder_name,))
        download = patch.object(sdk, 'download_url_app', return_value={'state': True, 'data': {'url': LINK}})
        self.download = download.start()
        self.addCleanup(download.stop)
        app = FastAPI()
        route, = routes(SimpleNamespace(_service=self.service))
        app.add_api_route(route['path'], route['endpoint'], methods=route['methods'])
        self.http = self.enterContext(TestClient(app))

    def request_115(self, request):
        host_path = (request.url.host, request.url.path)
        self.calls.append(host_path)
        if host_path == ('webapi.115.com', '/files/get_info'):
            return Response(200, json={'state': True, 'data': [{'cid': '101', 'pid': '90', 'n': self.folder_name}]})
        if host_path == ('webapi.115.com', '/files'):
            raise ReadTimeout('Simulated directory listing timeout', request=request)
        if host_path == ('proapi.115.com', '/android/2.0/ufile/files'):
            ancestors = [{'cid': '0', 'name': 'root'}, {'cid': '90', 'name': 'temp'}]
            if request.url.params['cid'] == '90':
                return Response(200, json={'state': True, 'data': [], 'count': 0, 'path': ancestors})
            payload = {'state': True, 'data': self.members, 'path': [
                *ancestors, {'cid': '101', 'name': self.folder_name},
            ]}
            if self.folder_count is not None:
                payload['count'] = self.folder_count
            return Response(200, json=payload)
        if host_path == ('webapi.115.com', '/files/add') and request.method == 'POST':
            from urllib.parse import parse_qs
            self.folder_name = parse_qs(request.content.decode())['cname'][0]
            return Response(200, json={'state': True, 'cid': '101'})
        if host_path == ('webapi.115.com', '/share/receive') and request.method == 'POST':
            self.members = [{'fid': '201', 'pid': '101', 'fc': '1', 'fn': 'video.mkv',
                             'fs': '32', 'sha1': 'A' * 40, 'pc': 'fixturepickcode'}]
            self.folder_count = 1
            return Response(200, json={'state': True})
        self.fail(f'Unexpected fixture endpoint {host_path}')

    def test_empty_copy_plays_when_web_directory_listing_times_out(self):
        response = self.http.get('/play/' + self.token, headers={'User-Agent': 'Player fixture'}, follow_redirects=False)
        self.assertEqual(response.status_code, 302, self.calls)
        self.assertEqual(response.headers['location'], LINK)
        self.assertEqual(self.calls, [
            ('proapi.115.com', '/android/2.0/ufile/files'),
            ('webapi.115.com', '/share/receive'),
            ('proapi.115.com', '/android/2.0/ufile/files'),
        ])
        copy = self.service.storage.copy('v_fixture')
        self.assertEqual((copy['folder_cid'], copy['file_id'], copy['stage']), ('101', '201', 'READY'))
        self.download.assert_called_once()
        self.assertEqual(self.download.call_args.kwargs['user_agent'], 'Player fixture')

    def test_virtual_resource_without_copy_plays_when_web_directory_listing_times_out(self):
        self.service.query("DELETE FROM resource_copies WHERE media_id='v_fixture'")
        response = self.http.get('/play/' + self.token, follow_redirects=False)
        self.assertEqual(response.status_code, 302, self.calls)
        self.assertEqual(response.headers['location'], LINK)
        self.assertEqual(self.calls, [
            ('proapi.115.com', '/android/2.0/ufile/files'),
            ('webapi.115.com', '/files/add'),
            ('webapi.115.com', '/share/receive'),
            ('proapi.115.com', '/android/2.0/ufile/files'),
        ])

    def test_empty_copy_with_unverified_listing_does_not_receive(self):
        for count, members in ((None, []), (1, []), (1, [{'fid': '201', 'pid': '101', 'fc': '0', 'fn': 'other'}])):
            with self.subTest(count=count, members=members):
                self.folder_count, self.members = count, members
                self.calls.clear()
                response = self.http.get('/play/' + self.token, follow_redirects=False)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(self.calls, [('proapi.115.com', '/android/2.0/ufile/files')])
                self.assertEqual(self.service.storage.copy('v_fixture')['stage'], 'EMPTY')


class PlaybackEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop = asyncio.get_running_loop()
        self.resolving = asyncio.Event()
        self.head_entered = asyncio.Event()
        self.release = threading.Event()
        self.calls = []

        def resolve(token, ua):
            self.calls.append((token, ua))
            self.loop.call_soon_threadsafe(self.resolving.set)
            if not self.release.wait(4):
                raise TimeoutError('Fixture release not signaled')
            return LINK

        app = FastAPI()
        plugin = SimpleNamespace(_service=SimpleNamespace(play=resolve))
        route, = routes(plugin)
        app.add_api_route(route['path'], route['endpoint'], methods=route['methods'])

        async def observe(scope, receive, send):
            if scope['method'] == 'HEAD':
                self.head_entered.set()
            await app(scope, receive, send)

        self.client = AsyncClient(transport=ASGITransport(app=observe), base_url='http://moviepilot.test')
        self.addAsyncCleanup(self.client.aclose)

    async def requests(self, head_ua='Emby fixture', cancel_get=False):
        path = '/play/' + TOKEN
        get = asyncio.create_task(self.client.get(path, headers={'User-Agent': 'Emby fixture'}))
        try:
            await asyncio.wait_for(self.resolving.wait(), 2)
            if cancel_get:
                get.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await get
            head = asyncio.create_task(self.client.head(path, headers={'User-Agent': head_ua}))
            await asyncio.wait_for(self.head_entered.wait(), 2)
        finally:
            self.release.set()
        responses = [await head] if cancel_get else await asyncio.gather(get, head)
        for response in responses:
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers['location'], LINK)
            self.assertRegex(response.headers['server-timing'], r'^p115-resolve;dur=\d+\.\d$')

    async def test_overlapping_get_and_head_share_one_lookup_but_later_request_is_fresh(self):
        await self.requests()
        self.assertEqual(self.calls, [(TOKEN, 'Emby fixture')])
        await self.client.get('/play/' + TOKEN, headers={'User-Agent': 'Emby fixture'})
        self.assertEqual(len(self.calls), 2)

    async def test_a_disconnected_caller_does_not_restart_resolution(self):
        await self.requests(cancel_get=True)
        self.assertEqual(self.calls, [(TOKEN, 'Emby fixture')])

    async def test_different_playback_user_agents_have_separate_lookups(self):
        await self.requests(head_ua='Another player')
        self.assertCountEqual(self.calls, [(TOKEN, 'Emby fixture'), (TOKEN, 'Another player')])

    async def test_slow_lookup_is_logged_before_the_http_response_without_identifiers(self):
        warning = asyncio.Event()
        with patch('p115tool.api.PLAYBACK_SLOW_SECONDS', 0.01), patch('p115tool.api.logger.warning') as log:
            log.side_effect = lambda message: warning.set()
            get = asyncio.create_task(self.client.get('/play/' + TOKEN))
            try:
                await asyncio.wait_for(warning.wait(), 2)
                self.assertFalse(get.done())
                self.assertNotIn(TOKEN, str(log.call_args))
                self.assertNotIn(LINK, str(log.call_args))
            finally:
                self.release.set()
            self.assertEqual((await get).status_code, 302)
            log.assert_called_once()

    async def test_stale_singleflight_task_does_not_trap_new_requests(self):
        with patch('p115tool.api.SINGLEFLIGHT_MAX_AGE', 0.01):
            get = asyncio.create_task(self.client.get('/play/' + TOKEN, headers={'User-Agent': 'Emby fixture'}))
            try:
                await asyncio.wait_for(self.resolving.wait(), 2)
                await asyncio.sleep(0.02)
                head = asyncio.create_task(self.client.head('/play/' + TOKEN, headers={'User-Agent': 'Emby fixture'}))
                await asyncio.wait_for(self.head_entered.wait(), 2)
            finally:
                self.release.set()
            responses = await asyncio.gather(get, head)
        self.assertEqual([r.status_code for r in responses], [302, 302])
        self.assertEqual(len(self.calls), 2)

    async def test_head_to_get_one_shot_relay_skips_second_lookup(self):
        self.release.set()
        head = await self.client.head('/play/' + TOKEN, headers={'User-Agent': 'SenPlayer'})
        self.assertEqual(head.status_code, 302)
        self.assertEqual(len(self.calls), 1)
        # Immediate GET with same UA consumes the one-shot HEAD relay without a second lookup.
        first_get = await self.client.get('/play/' + TOKEN, headers={'User-Agent': 'SenPlayer'})
        self.assertEqual(first_get.status_code, 302)
        self.assertEqual(first_get.headers['location'], LINK)
        self.assertEqual(len(self.calls), 1)
        # Subsequent GET is fresh (one-shot relay already consumed).
        second_get = await self.client.get('/play/' + TOKEN, headers={'User-Agent': 'SenPlayer'})
        self.assertEqual(second_get.status_code, 302)
        self.assertEqual(len(self.calls), 2)


class ActualPlaybackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = Config.from_dict({'enabled': True, 'data_dir': str(root / 'data'), 'strm_dir': str(root / 'strm')})
        self.drive = SimpleNamespace(normal_link=lambda pc, ua: DownloadLink(LINK, {}), close=lambda: None)
        self.service = Service(config, self.drive)
        self.addCleanup(self.service.close)
        self.file = RemoteFile('10', 'sample.mkv', 32, 'A' * 40, '1', 'samplecode')
        self.token = self.service.register(self.file, 'sample.mkv')
        self.service.query("INSERT INTO resource_storage(media_id,kind,stage) VALUES('10','ACTUAL','READY')")

    def test_actual_playback_does_not_wait_for_the_virtual_storage_gate(self):
        result = []
        finished = threading.Event()

        def play():
            result.append(self.service.play(self.token, 'Emby fixture'))
            finished.set()

        with self.service.storage._gate:
            worker = threading.Thread(target=play)
            worker.start()
            completed_while_locked = finished.wait(1)
        worker.join(4)
        self.assertTrue(completed_while_locked, 'Actual playback waited for the virtual storage gate')
        self.assertEqual(result, [LINK])

    def test_source_delete_checkpoint_is_rechecked_before_returning_an_actual_link(self):
        copy_link = 'https://cdn.115.com/fixture-copy'

        def resolve(pickcode, ua):
            if pickcode == self.file.pickcode:
                self.service.query("UPDATE resource_storage SET stage='SOURCE_DELETING' WHERE media_id='10'")
                return DownloadLink(LINK, {})
            self.assertEqual(pickcode, 'copyfixture')
            return DownloadLink(copy_link, {})

        self.drive.normal_link = resolve
        copy = RemoteFile('11', self.file.name, 32, self.file.sha1, '90', 'copyfixture')
        with patch.object(self.service.storage, 'ensure_copy', return_value=copy) as ensure:
            self.assertEqual(self.service.play(self.token, 'Emby fixture'), copy_link)
        ensure.assert_called_once_with('10')


class ScanPlaybackTests(unittest.TestCase):
    desktop_ua = 'Desktop player fixture'
    phone_ua = 'Mobile player fixture'
    links = {desktop_ua: LINK + '/desktop', phone_ua: LINK + '/phone'}

    def setUp(self):
        from p115client import P115Client

        temporary = self.enterContext(tempfile.TemporaryDirectory())
        root = Path(temporary)
        config = Config.from_dict({'enabled': True, 'data_dir': str(root / 'data'),
                                   'strm_dir': str(root / 'strm'), 'source_cids': ['0']})
        sdk = P115Client('UID=1_A1_1; CID=fixture; SEID=fixture', console_qrcode=False)
        manager = P115ClientManager(config, sdk)
        self.scan_entered = threading.Event()
        self.download_entered = threading.Event()
        self.release_115 = threading.Event()
        self.hold_desktop = False
        default_transport = self.enterContext(patch('urllib3_future_request.request',
                                                     side_effect=AssertionError('Unexpected default SDK transport')))
        default_transport.__module__ = 'urllib3_future_request'
        self.default_transport = default_transport
        manager._http = Client(transport=MockTransport(self.request_115))
        self.service = Service(config, manager)
        self.addCleanup(self.service.close)
        file = RemoteFile('10', 'sample.mkv', 32, 'A' * 40, '1', 'a0000000000000000')
        self.playback_path = '/play/' + self.service.register(file, 'sample.mkv')
        app = FastAPI()
        route, = routes(SimpleNamespace(_service=self.service))
        app.add_api_route(route['path'], route['endpoint'], methods=route['methods'])
        self.http = self.enterContext(TestClient(app))
        self.enterContext(patch('p115client.client.rsa_decrypt', side_effect=self.decode_fixture_link))

    @staticmethod
    def decode_fixture_link(data):
        import json

        return json.dumps({'10': {'pick_code': 'a0000000000000000',
                                  'url': {'url': data.removeprefix('fixture:')}}}).encode()

    def request_115(self, request):
        if request.url.host == 'webapi.115.com' and request.url.path == '/files':
            self.scan_entered.set()
            if not self.release_115.wait(5):
                raise ReadTimeout('Fixture directory listing timed out', request=request)
            return Response(200, json={'state': True, 'data': [], 'count': 0})
        if request.url.host == 'proapi.115.com' and request.url.path == '/app/chrome/downurl':
            ua = request.headers['User-Agent']
            if ua == self.desktop_ua and self.hold_desktop:
                self.download_entered.set()
                if not self.release_115.wait(5):
                    raise ReadTimeout('Fixture desktop download timed out', request=request)
            return Response(200, json={'state': True, 'data': 'fixture:' + self.links[ua]})
        self.fail('Unexpected fixture endpoint')

    def play(self, ua):
        response = self.http.get(self.playback_path, headers={'User-Agent': ua}, follow_redirects=False)
        return response.status_code, response.headers.get('location')

    def assert_phone_plays(self, pool):
        phone = pool.submit(self.play, self.phone_ua)
        completed, _ = wait([phone], timeout=1)
        self.assertIn(phone, completed, 'Mobile playback must return a redirect before the other call is released')
        self.assertEqual(phone.result(), (302, self.links[self.phone_ua]))

    def test_phone_gets_own_redirect_while_actual_storage_scan_waits_on_115(self):
        with patch('p115client.util.get_stable_point', return_value='0000'), ThreadPoolExecutor() as pool:
            self.assertEqual(self.play(self.desktop_ua), (302, self.links[self.desktop_ua]))
            self.service.storage.request_refresh()
            try:
                self.assertTrue(self.scan_entered.wait(2), 'Actual storage scan did not reach 115')
                self.assert_phone_plays(pool)
            finally:
                self.release_115.set()

    def test_phone_gets_own_redirect_while_desktop_download_waits_on_115(self):
        self.hold_desktop = True
        with patch('p115client.util.get_stable_point', return_value='0000'), ThreadPoolExecutor() as pool:
            desktop = pool.submit(self.play, self.desktop_ua)
            try:
                self.assertTrue(self.download_entered.wait(2), 'Desktop playback did not reach 115')
                self.assert_phone_plays(pool)
            finally:
                self.release_115.set()
            self.assertEqual(desktop.result(2), (302, self.links[self.desktop_ua]))

    def test_known_pickcode_needs_no_sdk_directory_probe_on_first_play(self):
        with patch('p115client.util.get_stable_point', side_effect=KeyError):
            self.assertEqual(self.play(self.phone_ua), (302, self.links[self.phone_ua]))
        self.default_transport.assert_not_called()


class VirtualPlaybackEfficiencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = Config.from_dict({
            'enabled': True, 'data_dir': str(root / 'data'), 'strm_dir': str(root / 'strm'), 'temp_cid': '90',
        })
        self.files = {
            '90': RemoteFile('90', '临时副本', 0, '', '0', is_dir=True),
            '101': RemoteFile('101', 'p115tool-a', 0, '', '90', is_dir=True),
            '102': RemoteFile('102', 'p115tool-b', 0, '', '90', is_dir=True),
            '201': RemoteFile('201', 'a.mkv', 32, 'A' * 40, '101', 'pc-a'),
            '202': RemoteFile('202', 'b.mkv', 64, 'B' * 40, '102', 'pc-b'),
        }
        self.calls = []
        self.next_id = 300
        self.receive_started = threading.Event()
        self.release_receive = threading.Event()
        self.block_receive_fid = None

        class Drive:
            def __init__(drive):
                drive.outer = self

            def stat(drive, fid):
                drive.outer.calls.append(('stat', fid))
                return drive.outer.files[fid]

            def directory_path(drive, cid):
                drive.outer.calls.append(('directory_path', cid))
                return '/临时副本'

            def list_files(drive, cid):
                drive.outer.calls.append(('list_files', cid))
                yield from [f for f in drive.outer.files.values() if f.parent_id == cid]

            def create_temp_directory(drive, parent, name):
                drive.outer.calls.append(('mkdir', parent))
                drive.outer.next_id += 1
                cid = str(drive.outer.next_id)
                drive.outer.files[cid] = RemoteFile(cid, name, 0, '', parent, is_dir=True)
                return cid

            def receive_to_temp(drive, code, password, fid, cid):
                drive.outer.calls.append(('receive', fid))
                if fid == drive.outer.block_receive_fid:
                    drive.outer.receive_started.set()
                    if not drive.outer.release_receive.wait(4):
                        raise TimeoutError('Fixture receive release not signaled')
                drive.outer.next_id += 1
                new_id = str(drive.outer.next_id)
                drive.outer.files[new_id] = RemoteFile(new_id, 'cold.mkv', 128, 'C' * 40, cid, 'pc-' + new_id)

            def normal_link(drive, pc, ua):
                drive.outer.calls.append(('normal_link', pc))
                return DownloadLink(LINK + '/' + pc, {})

            def close(drive):
                pass

        self.drive = Drive()
        self.service = Service(config, self.drive)
        self.addCleanup(self.service.close)
        self.token_a = self._register_virtual('v_a', 'a.mkv', 32, 'A' * 40, '7', '101', 'p115tool-a', '201')
        self.token_b = self._register_virtual('v_b', 'b.mkv', 64, 'B' * 40, '8', '102', 'p115tool-b', '202')
        self.token_cold = self._register_virtual('v_cold', 'cold.mkv', 128, 'C' * 40, '9', None, None, None)

    def _register_virtual(self, mid, name, size, sha1, share_fid, folder_cid, folder_name, file_id):
        token = self.service.register(RemoteFile(mid, name, size, sha1, '0', ''), name)
        self.service.query(
            "INSERT INTO resource_storage(media_id,kind,stage,share_code,password,share_fid,own) VALUES(?,'VIRTUAL','READY','code','abcd',?,0)",
            (mid, share_fid),
        )
        if folder_cid is not None:
            self.service.query(
                "INSERT INTO resource_copies(media_id,root_cid,folder_name,folder_cid,file_id,stage) VALUES(?,'90',?,?,?,'READY')",
                (mid, folder_name, folder_cid, file_id),
            )
        return token

    def test_warm_ready_virtual_playback_skips_redundant_file_stat(self):
        self.calls.clear()
        self.assertEqual(self.service.play(self.token_a, 'Emby fixture'), LINK + '/pc-a')
        self.assertEqual(self.calls, [
            ('stat', '101'),
            ('list_files', '101'),
            ('normal_link', 'pc-a'),
        ])

    def test_warm_ready_virtual_playback_falls_back_to_stat_when_pickcode_missing(self):
        self.files['201'] = RemoteFile('201', 'a.mkv', 32, 'A' * 40, '101', '')
        real_stat = self.drive.stat

        def stat_with_pickcode(fid):
            item = real_stat(fid)
            if fid == '201':
                return RemoteFile('201', 'a.mkv', 32, 'A' * 40, '101', 'pc-from-stat')
            return item

        self.calls.clear()
        with patch.object(self.drive, 'stat', side_effect=stat_with_pickcode):
            self.assertEqual(self.service.play(self.token_a, 'Emby fixture'), LINK + '/pc-from-stat')
        self.assertEqual(self.calls, [
            ('stat', '101'),
            ('list_files', '101'),
            ('stat', '201'),
            ('normal_link', 'pc-from-stat'),
        ])

    def test_cold_virtual_playback_skips_redundant_folder_checks_on_new_folder(self):
        self.calls.clear()
        url = self.service.play(self.token_cold, 'Emby fixture')
        self.assertTrue(url.startswith(LINK + '/pc-'))
        self.assertEqual([op for op, _ in self.calls], [
            'directory_path', 'mkdir', 'receive', 'list_files', 'normal_link',
        ])

    def test_different_virtual_resources_play_concurrently_without_global_gate_blocking(self):
        self.block_receive_fid = '9'
        cold_result = []
        warm_result = []
        warm_done = threading.Event()

        def play_cold():
            cold_result.append(self.service.play(self.token_cold, 'Emby fixture'))

        def play_warm():
            warm_result.append(self.service.play(self.token_b, 'Emby fixture'))
            warm_done.set()

        t_cold = threading.Thread(target=play_cold)
        t_cold.start()
        try:
            self.assertTrue(self.receive_started.wait(2), 'Cold virtual transfer did not start')
            t_warm = threading.Thread(target=play_warm)
            t_warm.start()
            completed_while_transferring = warm_done.wait(1.5)
        finally:
            self.release_receive.set()
        t_cold.join(4)
        t_warm.join(4)
        self.assertTrue(completed_while_transferring, 'Warm virtual playback was blocked by another virtual transfer')
        self.assertEqual(warm_result, [LINK + '/pc-b'])
        self.assertEqual(len(cold_result), 1)

    def test_exclusive_storage_gate_still_blocks_virtual_playback_until_released(self):
        done = threading.Event()
        result = []

        def play_warm():
            result.append(self.service.play(self.token_a, 'Emby fixture'))
            done.set()

        with self.service.storage._gate:
            worker = threading.Thread(target=play_warm)
            worker.start()
            self.assertFalse(done.wait(0.15), 'Virtual playback bypassed exclusive storage gate')
        worker.join(4)
        self.assertTrue(done.is_set())
        self.assertEqual(result, [LINK + '/pc-a'])

    def test_playback_step_logs_emit_sanitized_trace_without_secrets(self):
        with self.assertLogs('p115tool.playback', level='INFO') as logs:
            self.assertEqual(self.service.play(self.token_a, 'Emby fixture'), LINK + '/pc-a')
        joined = '\n'.join(logs.output)
        self.assertIn('媒体=v_a', joined)
        self.assertIn('复用已有副本', joined)
        self.assertIn('副本取链成功', joined)
        for secret in (self.token_a, 'pc-a', LINK, 'abcd'):
            self.assertNotIn(secret, joined)

    def test_unexpected_bug_logs_file_and_line_without_exception_text(self):
        from p115tool.api import log_failure
        try:
            raise KeyError('SECRET_TOKEN_OR_URL')
        except KeyError as exc:
            with self.assertLogs('p115tool.api', level='WARNING') as logs:
                log_failure(exc)
        self.assertIn('类型=KeyError', logs.output[0])
        self.assertIn('位置=test_playback_concurrency.py:', logs.output[0])
        self.assertNotIn('SECRET_TOKEN_OR_URL', logs.output[0])

    def test_cleanup_of_one_virtual_resource_does_not_block_playback_of_another(self):
        cleanup_entered = threading.Event()
        release_cleanup = threading.Event()
        warm_done = threading.Event()
        warm_result = []

        self.service.query("UPDATE resource_copies SET received_at=1,lease_until=1 WHERE media_id='v_a'")
        self.service.query("UPDATE resource_copies SET received_at=9999999999,lease_until=9999999999 WHERE media_id='v_b'")

        def slow_verify(row):
            if row['file_id'] == 'v_a':
                cleanup_entered.set()
                if not release_cleanup.wait(4):
                    raise TimeoutError('Fixture cleanup release not signaled')
            return row['share_fid']

        def run_cleanup():
            self.service.storage.cleanup()

        def play_other():
            warm_result.append(self.service.play(self.token_b, 'Emby fixture'))
            warm_done.set()

        with patch.object(self.service.storage, 'verify_share', side_effect=slow_verify):
            t_cleanup = threading.Thread(target=run_cleanup)
            t_cleanup.start()
            try:
                self.assertTrue(cleanup_entered.wait(2), 'Cleanup did not start')
                t_play = threading.Thread(target=play_other)
                t_play.start()
                completed_during_cleanup = warm_done.wait(1.5)
            finally:
                release_cleanup.set()
            t_cleanup.join(4)
            t_play.join(4)
        self.assertTrue(completed_during_cleanup, 'Virtual playback was blocked by cleanup of another resource')
        self.assertEqual(warm_result, [LINK + '/pc-b'])

    def test_list_verified_folder_uses_single_fs_files_roundtrip_and_detects_fallback(self):
        from p115tool.client import P115ClientManager
        from p115tool.models import SafetyError
        sdk_calls = []

        class SDK:
            def __init__(self, fallback=False):
                self.fallback = fallback

            def fs_files(self, payload, timeout=30):
                sdk_calls.append(('fs_files', payload['cid']))
                if self.fallback:
                    return {'state': True, 'path': [{'cid': '0', 'name': '根目录'}], 'data': [], 'count': 0}
                return {
                    'state': True,
                    'path': [
                        {'cid': '0', 'name': '根目录'},
                        {'cid': '90', 'name': 'temp'},
                        {'cid': '101', 'name': 'p115tool-a'},
                    ],
                    'data': [{'fid': '201', 'n': 'a.mkv', 's': 32, 'sha': 'A' * 40, 'cid': '101', 'pc': 'pc-a'}],
                    'count': 1,
                }

        mgr = P115ClientManager(self.service.config, sdk=SDK(fallback=False))
        files = mgr.list_verified_folder('101', '90', 'p115tool-a')
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].pickcode, 'pc-a')
        self.assertEqual(sdk_calls, [('fs_files', '101')])

        mgr_fallback = P115ClientManager(self.service.config, sdk=SDK(fallback=True))
        with self.assertRaises(SafetyError):
            mgr_fallback.list_verified_folder('101', '90', 'p115tool-a')
