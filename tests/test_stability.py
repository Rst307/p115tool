import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from p115tool import P115Tool
from p115tool.config import Config
from p115tool.models import DownloadLink, RemoteFile, ToolError
from p115tool.native_ui import dispatch
from p115tool.service import Service


class StabilityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        config = Config.from_dict({
            'enabled': True,
            'data_dir': str(self.root / 'data'),
            'strm_dir': str(self.root / 'strm'),
            'source_cids': [{'cid': '10', 'prefix': '/电影/外语电影'}],
            'public_url': 'http://mp:3000',
        })
        client = Mock()
        client.directory_path.return_value = '/电影/外语电影'
        client.list_files.return_value = []
        self.service = Service(config, client)
        self.addCleanup(self.service.close)

    @unittest.skipUnless(os.name == 'nt', 'Windows rename semantics')
    def test_windows_strm_creation_works_without_hardlinks_and_preserves_concurrent_output(self):
        service = self.service
        service.client.list_files.return_value = [RemoteFile('1', 'a.mkv', 1, 'A', '10', 'pc')]
        with patch('p115tool.strm.os.link', side_effect=PermissionError):
            service.generate_all()
        self.assertEqual(service.snapshot()['generated'], 1)
        self.assertTrue((self.root / 'strm/电影/外语电影/a.mkv.strm').is_file())

        rename = os.rename

        def concurrent_create(source, target):
            target.write_text('external content', encoding='utf-8')
            rename(source, target)

        with patch('p115tool.strm.os.rename', side_effect=concurrent_create):
            with self.assertRaises(FileExistsError):
                service.strm.generate('race.mkv', 'x' * 32)
        self.assertEqual((self.root / 'strm/race.mkv.strm').read_text('utf-8'), 'external content')

    def test_strm_thread_start_failure_is_terminal_and_can_be_retried(self):
        service = self.service
        with patch('p115tool.service.threading.Thread') as thread:
            thread.return_value.start.side_effect = RuntimeError('mock thread startup failure')
            with self.assertRaises(ToolError):
                service.start()
        self.assertEqual(service.snapshot()['state'], 'FAILED')
        self.assertEqual(service.snapshot()['error'], 'START_FAILED')
        self.assertIsNone(service._worker)
        service.start()
        worker = service._worker
        if worker:
            worker.join(3)
        self.assertEqual(service.snapshot()['state'], 'DONE')

    def test_slow_playback_leaves_status_responsive_and_stop_drains_playback(self):
        service = self.service
        token = service.register(RemoteFile('1', 'a.mkv', 1, 'A', '10', 'pc'), '电影/a.mkv')
        plugin = P115Tool()
        plugin._service = service
        app = FastAPI()
        for route in plugin.get_api():
            app.add_api_route(route['path'], route['endpoint'], methods=route['methods'])
        entered = threading.Event()
        release = threading.Event()

        def slow_link(pickcode, ua):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Playback fixture timed out')
            service.client.close.assert_not_called()
            return DownloadLink('https://cdn.115.com/video', {})

        service.client.normal_link = slow_link
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=3) as pool:
            playback = pool.submit(client.get, '/play/' + token, follow_redirects=False)
            try:
                self.assertTrue(entered.wait(3))
                status = pool.submit(dispatch, plugin, 'data', {'kind': 'status'})
                self.assertEqual(status.result(timeout=1)['state'], 'IDLE')
                stopped = pool.submit(plugin.stop_service)
                self.assertTrue(service._stop.wait(1))
                self.assertFalse(stopped.done())
                self.assertEqual(client.get('/play/' + token).status_code, 503)
            finally:
                release.set()
            self.assertEqual(playback.result(timeout=3).status_code, 302)
            stopped.result(timeout=3)
            service.client.close.assert_called_once()
