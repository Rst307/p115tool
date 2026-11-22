import asyncio
import json
import socket
import threading
import unittest

import httpx
from starlette.testclient import TestClient

from p115tool.config import Config
from p115tool.emby_proxy import EmbyProxyServer, build_proxy_app, extract_plugin_token

TOKEN = 'AbCdEfGhIjKlMnOpQrStUvWxYz0123456789'
DIRECT_LINK = 'https://cdn.115cdn.net/direct/video.mkv?t=123'


class FakeService:
    def __init__(self, fail=False):
        self.config = Config.from_dict({'emby_proxy_enabled': False})
        self.calls = []
        self.fail = fail

    def play(self, token, ua):
        self.calls.append((token, ua))
        if self.fail:
            raise RuntimeError('upstream failure')
        return DIRECT_LINK


class EmbyProxyTests(unittest.TestCase):
    def test_config_validation_for_emby_proxy(self):
        cfg = Config.from_dict({
            'emby_proxy_enabled': True,
            'emby_url': 'http://192.168.0.2:8096/',
            'emby_proxy_port': '8095',
            'emby_proxy_direct_path': True,
        })
        self.assertTrue(cfg.emby_proxy_enabled)
        self.assertEqual(cfg.emby_url, 'http://192.168.0.2:8096')
        self.assertEqual(cfg.emby_proxy_port, 8095)
        self.assertTrue(cfg.emby_proxy_direct_path)

        with self.assertRaises(ValueError):
            Config.from_dict({'emby_proxy_enabled': True, 'emby_url': ''})
        with self.assertRaises(ValueError):
            Config.from_dict({'emby_proxy_enabled': True, 'emby_url': 'http://192.168.0.2:8096', 'emby_proxy_port': 80})
        with self.assertRaises(ValueError):
            Config.from_dict({'emby_proxy_enabled': True, 'emby_url': 'ftp://192.168.0.2:8096'})

    def test_extract_plugin_token(self):
        self.assertEqual(
            extract_plugin_token(f'http://192.168.0.2:3000/api/v1/plugin/P115Tool/play/{TOKEN}', '/api/v1/plugin/P115Tool'),
            TOKEN,
        )
        self.assertIsNone(extract_plugin_token('/media/movies/local.mkv', '/api/v1/plugin/P115Tool'))
        self.assertIsNone(extract_plugin_token('http://192.168.0.2:3000/other/path', '/api/v1/plugin/P115Tool'))

    def test_playback_info_replaces_plugin_path_and_stream_redirects(self):
        service = FakeService()

        def handler(request: httpx.Request):
            if request.url.path == '/emby/Items/42/PlaybackInfo':
                return httpx.Response(200, json={
                    'MediaSources': [
                        {
                            'Id': 'ms-1',
                            'Path': f'http://192.168.0.2:3000/api/v1/plugin/P115Tool/play/{TOKEN}',
                        },
                        {
                            'Id': 'ms-local',
                            'Path': '/data/movies/local.mkv',
                        },
                    ],
                })
            if request.url.path == '/emby/Videos/99/stream':
                return httpx.Response(200, content=b'local-video-bytes', headers={'content-type': 'video/x-matroska'})
            if request.url.path == '/emby/System/Info/Public':
                return httpx.Response(200, json={'ServerName': 'HomeEmby'})
            return httpx.Response(404)

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app = build_proxy_app(service, 'http://192.168.0.2:8096', direct_path=True, upstream_client=upstream)

        with TestClient(app) as client:
            # 1. Normal Emby request passes through transparently.
            info = client.get('/emby/System/Info/Public')
            self.assertEqual(info.status_code, 200)
            self.assertEqual(info.json(), {'ServerName': 'HomeEmby'})

            # 2. PlaybackInfo records MediaSource tokens immediately without calling 115 or blocking.
            pb = client.post('/emby/Items/42/PlaybackInfo', headers={'User-Agent': 'SenPlayer/2.0'})
            self.assertEqual(pb.status_code, 200)
            sources = pb.json()['MediaSources']
            self.assertEqual(sources[0]['Path'], f'http://192.168.0.2:3000/api/v1/plugin/P115Tool/play/{TOKEN}')
            self.assertEqual(sources[1]['Path'], '/data/movies/local.mkv')
            self.assertEqual(service.calls, [])

            # 3. /Videos/42/stream redirects directly to 115 CDN using cached MediaSource token.
            with self.assertLogs('p115tool.emby_proxy', level='INFO') as logs:
                stream = client.get(
                    '/emby/Videos/42/stream?MediaSourceId=ms-1',
                    headers={'User-Agent': 'Infuse/8.0'},
                    follow_redirects=False,
                )
            self.assertEqual(stream.status_code, 302)
            self.assertEqual(stream.headers['location'], DIRECT_LINK)
            self.assertEqual(service.calls, [(TOKEN, 'Infuse/8.0')])
            self.assertNotIn(TOKEN, '\n'.join(logs.output))
            self.assertNotIn(DIRECT_LINK, '\n'.join(logs.output))

            # 4. Local non-STRM video stream passes through transparently.
            local_stream = client.get('/emby/Videos/99/stream', headers={'User-Agent': 'Infuse/8.0'})
            self.assertEqual(local_stream.status_code, 200)
            self.assertEqual(local_stream.content, b'local-video-bytes')

    def test_playback_info_does_not_contend_and_stream_head_relays_to_get(self):
        service = FakeService()
        lan_url = f'http://192.168.0.2:3000/api/v1/plugin/P115Tool/play/{TOKEN}'

        def handler(request: httpx.Request):
            if request.url.path == '/emby/Items/42/PlaybackInfo':
                return httpx.Response(200, json={'MediaSources': [{'Id': 'ms-1', 'Path': lan_url}]})
            return httpx.Response(404)

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app = build_proxy_app(service, 'http://192.168.0.2:8096', direct_path=False, upstream_client=upstream)
        with TestClient(app) as client:
            # PlaybackInfo uses API UA and passes through immediately without calling service.play.
            pb = client.post('/emby/Items/42/PlaybackInfo', headers={'User-Agent': 'SenPlayer-API/2.0'})
            self.assertEqual(pb.status_code, 200)
            self.assertEqual(pb.json()['MediaSources'][0]['Path'], lan_url)
            self.assertEqual(service.calls, [])

            # Player stream HEAD resolves once with player UA, then GET reuses it with 0 extra calls.
            head = client.head(
                '/emby/Videos/42/stream?MediaSourceId=ms-1',
                headers={'User-Agent': 'SenPlayer-MPV/2.0'},
                follow_redirects=False,
            )
            stream = client.get(
                '/emby/Videos/42/stream?MediaSourceId=ms-1',
                headers={'User-Agent': 'SenPlayer-MPV/2.0'},
                follow_redirects=False,
            )
        self.assertEqual(head.status_code, 302)
        self.assertEqual(stream.status_code, 302)
        self.assertEqual(stream.headers['location'], DIRECT_LINK)
        self.assertEqual(service.calls, [(TOKEN, 'SenPlayer-MPV/2.0')])

    def test_playback_info_failure_keeps_original_path(self):
        service = FakeService(fail=True)
        lan_url = f'http://192.168.0.2:3000/api/v1/plugin/P115Tool/play/{TOKEN}'

        def handler(request: httpx.Request):
            return httpx.Response(200, json={'MediaSources': [{'Id': 'ms-1', 'Path': lan_url}]})

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        app = build_proxy_app(service, 'http://192.168.0.2:8096', direct_path=True, upstream_client=upstream)
        with TestClient(app) as client:
            pb = client.post('/emby/Items/42/PlaybackInfo', headers={'User-Agent': 'SenPlayer/2.0'})
        self.assertEqual(pb.status_code, 200)
        self.assertEqual(pb.json()['MediaSources'][0]['Path'], lan_url)

    def test_overlapping_stream_head_and_get_share_one_link_lookup(self):
        service = FakeService()
        lan_url = f'http://moviepilot.test/api/v1/plugin/P115Tool/play/{TOKEN}'
        path = '/emby/Videos/42/stream'
        original_play = service.play

        async def exercise():
            loop = asyncio.get_running_loop()
            head_resolving = asyncio.Event()
            get_entered = asyncio.Event()
            release = threading.Event()

            def play(token, ua):
                loop.call_soon_threadsafe(head_resolving.set)
                if not release.wait(4):
                    raise TimeoutError('Fixture lookup was not released')
                return original_play(token, ua)

            service.play = play
            upstream = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={'MediaSources': [{'Id': 'ms-1', 'Path': lan_url}]})))
            app = build_proxy_app(service, 'http://emby.test', upstream_client=upstream)

            async def observed(scope, receive, send):
                if scope['method'] == 'GET' and scope['path'] == path:
                    get_entered.set()
                await app(scope, receive, send)

            async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=observed), base_url='http://proxy.test',
                headers={'User-Agent': 'Player fixture'},
            ) as client:
                await client.post('/emby/Items/42/PlaybackInfo')
                head = asyncio.create_task(client.head(path))
                try:
                    await asyncio.wait_for(head_resolving.wait(), 2)
                    get = asyncio.create_task(client.get(path))
                    await asyncio.wait_for(get_entered.wait(), 2)
                finally:
                    release.set()
                for response in await asyncio.gather(head, get):
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.headers['location'], DIRECT_LINK)
                self.assertEqual(service.calls, [(TOKEN, 'Player fixture')])
                self.assertEqual((await client.get(path)).status_code, 302)
                self.assertEqual(len(service.calls), 2)

        asyncio.run(exercise())

    def test_proxy_server_starts_and_closes_cleanly(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]

        service = FakeService()
        service.config = Config.from_dict({
            'emby_proxy_enabled': True,
            'emby_url': 'http://127.0.0.1:1',
            'emby_proxy_port': port,
        })
        server = EmbyProxyServer(service)
        server.start()
        try:
            self.assertIsNotNone(server._server)
            self.assertTrue(server._server.started)
        finally:
            server.close()
        self.assertIsNone(server._server)


if __name__ == '__main__':
    unittest.main()
