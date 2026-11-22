"""Built-in local Emby 302 reverse proxy that resolves p115Tool STRM links locally."""
from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import json
import re
import threading
import time
from urllib.parse import urlsplit

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from .api import TOKEN_RE, log_failure
from .models import PLUGIN_VERSION

try:
    from app.log import logger
except ModuleNotFoundError:
    import logging
    logger = logging.getLogger('p115tool.emby_proxy')

HOP_BY_HOP = frozenset({
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailers', 'transfer-encoding', 'upgrade', 'host', 'content-length',
})
PLAYBACK_INFO_RE = re.compile(r'^(?:/emby)?/Items/([^/]+)/PlaybackInfo$', re.IGNORECASE)
STREAM_RE = re.compile(r'^(?:/emby)?/Videos/([^/]+)/(?:stream(?:\.[^/]+)?|original(?:\.[^/]+)?)$', re.IGNORECASE)
SOURCE_CACHE_TTL = 1800.0
SOURCE_CACHE_MAX = 2048
PREFETCH_TTL = 30.0
PREFETCH_MAX = 128


def extract_plugin_token(path_value, playback_prefix):
    """Return the p115Tool playback token if path_value is a plugin /play/{token} URL."""
    if not isinstance(path_value, str) or not path_value:
        return None
    parsed = urlsplit(path_value.strip())
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return None
    prefix = playback_prefix.rstrip('/') + '/play/'
    if not parsed.path.startswith(prefix):
        return None
    token = parsed.path[len(prefix):]
    return token if TOKEN_RE.fullmatch(token) else None


def _filter_headers(headers, extra_drop=()):
    drop = HOP_BY_HOP | frozenset(h.lower() for h in extra_drop)
    return [(k, v) for k, v in headers.items() if k.lower() not in drop]


def build_proxy_app(service, emby_url, direct_path=False, upstream_client=None):
    """Build the Starlette ASGI app for local Emby 302 proxying."""
    base_url = emby_url.rstrip('/')
    playback_prefix = service.config.playback_prefix
    source_tokens = {}      # (item_id, media_source_id) -> (token, expires_at)
    item_tokens = {}        # item_id -> (token, expires_at)
    stream_head_relay = {}  # (token, ua) -> (location, expires_at)

    def remember_stream_head(token, ua, location):
        now = time.monotonic()
        if len(stream_head_relay) >= PREFETCH_MAX and (token, ua) not in stream_head_relay:
            for k in [k for k, v in stream_head_relay.items() if now >= v[1]]:
                stream_head_relay.pop(k, None)
        stream_head_relay[(token, ua)] = (location, now + 15.0)

    def remember_token(item_id, media_source_id, token):
        now = time.monotonic()
        exp = now + SOURCE_CACHE_TTL
        if len(source_tokens) >= SOURCE_CACHE_MAX:
            for k in [k for k, v in source_tokens.items() if now >= v[1]]:
                source_tokens.pop(k, None)
            if len(source_tokens) >= SOURCE_CACHE_MAX:
                oldest = min(source_tokens.items(), key=lambda item: item[1][1])[0]
                source_tokens.pop(oldest, None)
        if media_source_id:
            source_tokens[(str(item_id), str(media_source_id))] = (token, exp)
        if len(item_tokens) >= SOURCE_CACHE_MAX:
            for k in [k for k, v in item_tokens.items() if now >= v[1]]:
                item_tokens.pop(k, None)
        item_tokens[str(item_id)] = (token, exp)

    def lookup_token(item_id, media_source_id):
        now = time.monotonic()
        if media_source_id:
            entry = source_tokens.get((str(item_id), str(media_source_id)))
            if entry and now < entry[1]:
                return entry[0]
        entry = item_tokens.get(str(item_id))
        if entry and now < entry[1]:
            return entry[0]
        return None

    async def resolve_upstream_item_token(client, item_id, media_source_id, request):
        """Fallback lookup against local Emby when a client hits /Videos/.../stream directly."""
        auth_headers = {}
        for name in ('x-emby-token', 'x-mediabrowser-token', 'x-emby-authorization', 'authorization'):
            val = request.headers.get(name)
            if val:
                auth_headers[name] = val
        params = {'Fields': 'Path,MediaSources'}
        for key in ('api_key', 'X-Emby-Token', 'x-emby-token'):
            val = request.query_params.get(key)
            if val:
                params['api_key'] = val
                break
        if 'api_key' not in params and not auth_headers:
            return None
        try:
            resp = await client.get(f'{base_url}/emby/Items/{item_id}', params=params, headers=auth_headers)
            if resp.status_code != 200:
                return None
            data = resp.json()
        except Exception:
            return None
        sources = data.get('MediaSources') if isinstance(data, dict) else None
        if isinstance(sources, list):
            for src in sources:
                if not isinstance(src, dict):
                    continue
                tok = extract_plugin_token(src.get('Path'), playback_prefix)
                if tok:
                    src_id = src.get('Id')
                    remember_token(item_id, src_id, tok)
                    if not media_source_id or str(src_id) == str(media_source_id):
                        return tok
        if isinstance(data, dict):
            tok = extract_plugin_token(data.get('Path'), playback_prefix)
            if tok:
                remember_token(item_id, media_source_id, tok)
                return tok
        return None

    async def handle_playback_info(request: Request, client: httpx.AsyncClient, item_id: str):
        target_url = base_url + request.url.path
        if request.url.query:
            target_url += '?' + request.url.query
        req_headers = _filter_headers(request.headers, extra_drop=('accept-encoding',))
        req_headers.append(('accept-encoding', 'identity'))
        body = await request.body()
        upstream = await client.request(
            request.method, target_url, headers=req_headers, content=body,
        )
        content_type = upstream.headers.get('content-type', '')
        if upstream.status_code != 200 or 'json' not in content_type.lower():
            resp_headers = _filter_headers(upstream.headers, extra_drop=('content-encoding',))
            return Response(content=upstream.content, status_code=upstream.status_code, headers=dict(resp_headers))

        try:
            payload = upstream.json()
        except Exception:
            resp_headers = _filter_headers(upstream.headers, extra_drop=('content-encoding',))
            return Response(content=upstream.content, status_code=upstream.status_code, headers=dict(resp_headers))

        sources = payload.get('MediaSources') if isinstance(payload, dict) else None
        if isinstance(sources, list):
            for src in sources:
                if not isinstance(src, dict):
                    continue
                token = extract_plugin_token(src.get('Path'), playback_prefix)
                if not token:
                    continue
                remember_token(item_id, src.get('Id'), token)

        resp_headers = dict(_filter_headers(upstream.headers, extra_drop=('content-encoding', 'content-length')))
        resp_headers['content-type'] = 'application/json; charset=utf-8'
        encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        return Response(content=encoded, status_code=upstream.status_code, headers=resp_headers)

    async def handle_stream(request: Request, client: httpx.AsyncClient, item_id: str):
        media_source_id = (
            request.query_params.get('MediaSourceId')
            or request.query_params.get('mediaSourceId')
            or ''
        )
        token = lookup_token(item_id, media_source_id)
        if not token:
            token = await resolve_upstream_item_token(client, item_id, media_source_id, request)
        if not token:
            return await forward_streaming(request, client)
        ua = request.headers.get('user-agent', '')
        if len(ua) > 1024 or any(ord(c) < 32 for c in ua):
            return Response(status_code=400, content='Invalid request')
        started = time.monotonic()
        location = None
        if request.method == 'GET':
            relayed = stream_head_relay.pop((token, ua), None)
            if relayed is not None and time.monotonic() < relayed[1]:
                location = relayed[0]
        if location:
            elapsed = (time.monotonic() - started) * 1000
            logger.info(f'[v{PLUGIN_VERSION}] 115 Emby本地代理：stream复用HEAD直链返回302，耗时={elapsed:.1f}ms')
        else:
            try:
                location = await run_in_threadpool(service.play, token, ua)
            except Exception as exc:
                log_failure(exc)
                logger.warning(f'[v{PLUGIN_VERSION}] 115 Emby本地代理：stream 302取链失败，回退至Emby上游')
                return await forward_streaming(request, client)
            if request.method == 'HEAD':
                remember_stream_head(token, ua, location)
            elapsed = (time.monotonic() - started) * 1000
            logger.info(f'[v{PLUGIN_VERSION}] 115 Emby本地代理：stream 302直链返回，耗时={elapsed:.1f}ms')
        return Response(
            status_code=302,
            headers={
                'Location': location,
                'Cache-Control': 'no-store',
                'Referrer-Policy': 'no-referrer',
                'Server-Timing': f'p115-emby-proxy;dur={elapsed:.1f}',
            },
        )

    async def forward_streaming(request: Request, client: httpx.AsyncClient):
        target_url = base_url + request.url.path
        if request.url.query:
            target_url += '?' + request.url.query
        req_headers = _filter_headers(request.headers)
        body = await request.body()
        req = client.build_request(request.method, target_url, headers=req_headers, content=body)
        upstream = await client.send(req, stream=True)
        resp_headers = dict(_filter_headers(upstream.headers))
        if upstream.is_stream_consumed:
            return Response(content=upstream.content, status_code=upstream.status_code, headers=resp_headers)

        async def iterator():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(iterator(), status_code=upstream.status_code, headers=resp_headers)

    async def dispatch_proxy(request: Request):
        client = request.app.state.client
        path = request.url.path
        pb_match = PLAYBACK_INFO_RE.match(path)
        if pb_match and request.method in ('GET', 'POST'):
            try:
                return await handle_playback_info(request, client, pb_match.group(1))
            except httpx.HTTPError as exc:
                logger.warning(f'115 Emby本地代理：PlaybackInfo转发Emby失败，异常={type(exc).__name__}')
                return Response(status_code=502, content='Emby upstream unavailable')
        stream_match = STREAM_RE.match(path)
        if stream_match and request.method in ('GET', 'HEAD'):
            try:
                return await handle_stream(request, client, stream_match.group(1))
            except httpx.HTTPError as exc:
                logger.warning(f'115 Emby本地代理：stream转发Emby失败，异常={type(exc).__name__}')
                return Response(status_code=502, content='Emby upstream unavailable')
        try:
            return await forward_streaming(request, client)
        except httpx.HTTPError as exc:
            logger.warning(f'115 Emby本地代理：连接上游Emby失败，异常={type(exc).__name__}')
            return Response(status_code=502, content='Emby upstream unavailable')

    @asynccontextmanager
    async def lifespan(starlette_app):
        if upstream_client is not None:
            starlette_app.state.client = upstream_client
            owns_client = False
        else:
            starlette_app.state.client = httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
            )
            owns_client = True
        try:
            yield
        finally:
            stream_head_relay.clear()
            if owns_client:
                await starlette_app.state.client.aclose()

    methods = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']
    app = Starlette(
        routes=[
            Route('/', dispatch_proxy, methods=methods),
            Route('/{path:path}', dispatch_proxy, methods=methods),
        ],
        lifespan=lifespan,
    )
    return app


class EmbyProxyServer:
    """Runs the local Emby 302 proxy server in a background daemon thread."""

    def __init__(self, service):
        self.service = service
        self.config = service.config
        self._server = None
        self._thread = None
        self._ready = threading.Event()
        self._error = None

    def start(self):
        if not self.config.emby_proxy_enabled or not self.config.emby_url:
            return
        app = build_proxy_app(
            self.service,
            self.config.emby_url,
            direct_path=self.config.emby_proxy_direct_path,
        )
        uv_config = uvicorn.Config(
            app,
            host='0.0.0.0',
            port=self.config.emby_proxy_port,
            log_level='error',
            access_log=False,
            log_config=None,
        )
        self._server = uvicorn.Server(uv_config)
        # Never install signal handlers from a background thread.
        self._server.install_signal_handlers = lambda: None
        self._thread = threading.Thread(target=self._run, name='p115tool-emby-proxy', daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3.0)

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            async def serve():
                startup_task = asyncio.create_task(self._wait_started())
                try:
                    await self._server.serve()
                finally:
                    startup_task.cancel()
            loop.run_until_complete(serve())
        except Exception as exc:
            self._error = type(exc).__name__
            logger.warning(
                f'115 Emby本地代理启动失败：端口={self.config.emby_proxy_port}，异常={self._error}'
            )
        finally:
            self._ready.set()
            loop.close()

    async def _wait_started(self):
        while not getattr(self._server, 'started', False) and not getattr(self._server, 'should_exit', False):
            await asyncio.sleep(0.05)
        if getattr(self._server, 'started', False):
            logger.info(f'[v{PLUGIN_VERSION}] 115 Emby本地代理已启动：监听端口={self.config.emby_proxy_port}')
        self._ready.set()

    def close(self):
        server = self._server
        thread = self._thread
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        self._server = None
        self._thread = None
