import asyncio
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from p115tool.api import routes
from p115tool.config import Config
from p115tool.models import DownloadLink, RemoteFile
from p115tool.service import Service


TOKEN = 'fixture-playback-identifier'
LINK = 'https://cdn.115.com/fixture'


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
