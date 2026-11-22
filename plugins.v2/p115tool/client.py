from __future__ import annotations
from contextlib import ExitStack
import ipaddress
import re
import threading
import time
from urllib.parse import parse_qsl, urlsplit
from .cookie_renewal import CookieRenewal, cookie_identity
from .models import RemoteFile, DownloadLink, RemoteError, MissingFile, SafetyError, ShareRejected, ToolError, PLUGIN_VERSION
try:
    from app.log import logger
except ModuleNotFoundError:
    import logging
    logger = logging.getLogger('p115tool.client')

PICKCODE_RE = re.compile(r'[A-Za-z0-9]+')

OPEN_READ_METHODS = {
    'fs_files': 'fs_files',
    'fs_file': 'fs_info',
    'download_url': 'download_url_info',
}
COOKIE_RETRY_READS = frozenset(OPEN_READ_METHODS) | {'fs_files_app', 'share_snap', 'share_download_url', 'recyclebin_list'}
TRANSIENT_RETRY_READS = frozenset(OPEN_READ_METHODS) | {'fs_files_app'}
TRANSIENT_SDK_ERRORS = frozenset({
    'ConnectError', 'ConnectionError', 'ConnectionResetError', 'ConnectionAbortedError',
    'BrokenPipeError', 'RemoteProtocolError', 'ProtocolError', 'SSLError',
    'ReadError', 'IncompleteRead',
})
COOKIE_RENEWAL_COOLDOWN = 1800
COOKIE_DOWNLOAD_SLOTS = 2
COOKIE_DOWNLOAD_WAIT = 2.0
QR_PRIMARY = ('qandroid', 'M1')
QR_BACKUP = ('alipaymini', 'R2')


def qr_data(response):
    if isinstance(response, dict) and 'state' not in response and response_code(response) == 0:
        return response['data']
    return check_response(response)['data']

def response_code(response):
    if not isinstance(response,dict): return None
    for key in ('errno','errNo','errcode','errCode','code','msg_code'):
        value=response.get(key)
        if type(value) is int: return value
        if isinstance(value,str) and re.fullmatch(r'[0-9]{1,9}',value): return int(value)
    return None


def check_response(response):
    if not isinstance(response, dict) or response.get("state") not in (True, 1):
        # Never include upstream messages/payloads: they can contain cookies or URLs.
        code=response_code(response)
        # The pinned SDK also maps 800001 (directory does not exist) to ENOENT.
        error=(MissingFile("Remote file no longer exists") if code in (20013, 20018, 50003, 90008, 800001)
               else RemoteError("115 request failed or returned an unrecognized response"))
        error.upstream_code=code
        raise error
    return response


def normalize_file(data, parent="0", path=""):
    is_dir = not bool(data.get("fid") or data.get("file_id"))
    if "is_directory" in data:
        is_dir = str(data["is_directory"]).lower() in ("1", "true")
    if "file_category" in data:
        is_dir = str(data["file_category"]) == "0"
    fid = data.get("fid") or data.get("file_id") or data.get("cid") or data.get('category_id')
    name = data.get("n") or data.get('fn') or data.get("file_name") or data.get("name") or data.get('category_name')
    if fid is None or name is None:
        raise RemoteError("Unsupported 115 file metadata shape")
    original_parent = data.get('parent_id', data.get('pid'))
    if original_parent is None:
        original_parent = parent if is_dir else data.get('category_id', data.get('cid', parent))
    return RemoteFile(str(fid), str(name), int(data.get("s", data.get('fs', data.get("file_size", data.get("size", 0))))),
                      str(data.get("sha") or data.get("sha1") or data.get('file_sha1') or '').upper(),
                      str(original_parent),
                      str(data.get("pc", data.get("pick_code", data.get("pickcode", "")))),
                      path, is_dir)


class P115ClientManager:
    """Manage one SDK session, serialized storage calls, and bounded Cookie downloads."""
    def __init__(self, config, sdk=None):
        self.config = config
        self._lock = threading.RLock()
        self._lock_owner = None
        self._auth_lock = threading.RLock()
        self._cookie_revision = 0
        self._download_slots = threading.BoundedSemaphore(COOKIE_DOWNLOAD_SLOTS)
        self._last_download_start = 0.0
        self._sdk = sdk
        self._http = None
        self._moviepilot_storage = None
        self._last_request = 0.0
        self._closed = False
        self._cookie_renewal = CookieRenewal(config)

    def close(self):
        # Stop admissions before draining both kinds of calls. An active download
        # may still need the auth lock during cleanup, so release it before draining.
        with self._lock, ExitStack() as downloads:
            with self._auth_lock:
                self._closed = True
            for _ in range(COOKIE_DOWNLOAD_SLOTS):
                downloads.enter_context(self._download_slots)
            self._sdk = None
            if self._http is not None:
                try:
                    self._http.close()
                except Exception:
                    pass
                self._http = None
            self._moviepilot_storage = None

    def _http_client(self):
        with self._auth_lock:
            if self._http is None:
                import httpx
                # Use a shared HTTP/1.1 pool for direct connections to 115 APIs.
                self._http = httpx.Client(http2=False, trust_env=False, follow_redirects=False)
            return self._http

    @staticmethod
    def _use_httpx_transport(sdk):
        if not type(sdk).__module__.startswith('p115client'):
            return False
        try:
            import urllib3_future_request
            return getattr(urllib3_future_request.request, '__module__', '') == 'urllib3_future_request'
        except Exception:
            return True

    def _httpx_request(self, *, url, method='GET', params=None, data=None, json=None,
                       headers=None, cookies=None, timeout=30, follow_redirects=True,
                       raise_for_status=True, parse=None, async_=False, **extra):
        import httpx
        t = float(timeout) if timeout else float(self.config.request_timeout)
        req_timeout = httpx.Timeout(connect=min(8.0, t), read=t, write=10.0, pool=10.0)
        req_headers = dict(headers or {})
        if cookies and not any(k.lower() == 'cookie' for k in req_headers):
            if isinstance(cookies, dict):
                cookie_str = '; '.join(f'{k}={v}' for k, v in cookies.items())
            else:
                cookie_str = '; '.join(f'{c.name}={c.value}' for c in cookies)
            if cookie_str:
                req_headers['cookie'] = cookie_str
        req_kwargs = {
            'params': params,
            'headers': req_headers,
            'timeout': req_timeout,
            'follow_redirects': bool(follow_redirects),
        }
        if json is not None:
            req_kwargs['json'] = json
        elif isinstance(data, (bytes, bytearray, memoryview, str)):
            req_kwargs['content'] = data
        elif data is not None:
            req_kwargs['data'] = data
        response = self._http_client().request(method, url, **req_kwargs)
        if raise_for_status and response.status_code >= 400:
            response.raise_for_status()
        if callable(parse):
            return parse(response, response.content)
        if parse is False:
            return response.content
        if parse is True:
            return response.json()
        return response

    def _client(self):
        with self._auth_lock:
            if self._closed:
                raise RemoteError('115 client is stopped')
            if self._sdk is None:
                if not self.config.cookie:
                    raise RemoteError("115 cookie is not configured")
                self._sdk = self._new_cookie_client(self._cookie_renewal.record['cookie'] or self.config.cookie)
            return self._sdk

    @staticmethod
    def _new_cookie_client(cookie):
        try:
            from p115client import P115Client
        except ImportError:
            raise RemoteError('Install p115client in the MoviePilot environment') from None
        return P115Client(cookie, console_qrcode=False)

    def cookie_status(self):
        enabled = self.config.cookie_auto_refresh and not self.config.use_moviepilot_auth
        renewal = self._cookie_renewal
        return {'state': renewal.state if enabled else 'DISABLED',
                'renewed_at': renewal.record['renewed_at'] or None}

    def check_cookie(self):
        with self._auth_lock:
            self._check_cookie_locked()

    @staticmethod
    def _login_status(sdk):
        from p115client.client import json_maybe_decrypt_loads

        # The SDK's default parser returns False for malformed responses too.
        # Distinguish a definite rejection from a broken status request.
        def parse(_, body):
            payload = json_maybe_decrypt_loads(body)
            result = payload.get('state') if isinstance(payload, dict) else None
            if type(result) not in (bool, int) or result not in (0, 1):
                raise ValueError('Invalid 115 login status')
            return bool(result)

        return sdk.login_status(parse=parse, timeout=10, follow_redirects=False)

    @classmethod
    def _verify_issued_cookie(cls, response, device, user_id=None):
        issued = qr_data(response)['cookie']
        if not isinstance(issued, (dict, str)) or not issued:
            raise ValueError('Invalid 115 Cookie')
        sdk = cls._new_cookie_client(issued)
        cookie = str(sdk.cookies_str)
        identity = cookie_identity(cookie)
        if (not identity or identity[1] != device or (user_id is not None and identity[0] != user_id)
                or cls._login_status(sdk) is not True):
            raise ValueError('Unverified 115 Cookie')
        return sdk, cookie, identity[0]

    @staticmethod
    def start_cookie_qr():
        from p115client import P115Client

        data = qr_data(P115Client.login_qrcode_token(timeout=8, follow_redirects=False))
        uid, stamp, sign = data['uid'], data['time'], data['sign']
        if (not isinstance(uid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', uid)
                or not re.fullmatch(r'[0-9]{1,16}', str(stamp))
                or not isinstance(sign, str) or not 1 <= len(sign) <= 256):
            raise ValueError('Invalid 115 QR challenge')
        # p115client 0.0.9.7.2 does not forward the positional payload to the image request.
        image = P115Client.login_qrcode(uid, params={'uid': uid}, timeout=8, follow_redirects=False)
        if not isinstance(image, bytes) or not image.startswith(b'\x89PNG\r\n\x1a\n') or len(image) > 1024 * 1024:
            raise ValueError('Invalid 115 QR image')
        return {'uid': uid, 'time': stamp, 'sign': sign}, image

    @staticmethod
    def cookie_qr_status(challenge):
        from p115client import P115Client

        status = qr_data(P115Client.login_qrcode_scan_status(challenge, timeout=6, follow_redirects=False))['status']
        if type(status) is not int or status not in (-2, -1, 0, 1, 2):
            raise ValueError('Invalid 115 QR status')
        return status

    @classmethod
    def finish_cookie_qr(cls, challenge, expected_user=None):
        from p115client import P115Client

        app, device = QR_PRIMARY
        response = P115Client.login_qrcode_scan_result(challenge['uid'], app=app, timeout=10, follow_redirects=False)
        primary, cookie, user_id = cls._verify_issued_cookie(response, device)
        if expected_user is not None and user_id != expected_user:
            return '', ''
        app, device = QR_BACKUP
        try:
            response = primary.login_with_app(app, timeout=10, follow_redirects=False)
            _, backup, _ = cls._verify_issued_cookie(response, device, user_id)
        except Exception:
            backup = ''
        return cookie, backup

    def _check_cookie_locked(self, *, on_auth_reject=False):
        if self._closed or self.config.use_moviepilot_auth or not self.config.cookie_auto_refresh:
            return False
        renewal = self._cookie_renewal
        now = time.monotonic()
        # If the previous check saw VALID and a live request just received 99/990001,
        # re-verify immediately instead of failing for up to 60s on a stale VALID cache.
        if now < renewal.next_check and not (on_auth_reject and renewal.state == 'VALID'):
            return False
        renewal.next_check = now + 60
        try:
            active = self._login_status(self._client())
        except Exception:
            renewal.state = 'STATUS_UNAVAILABLE'
            logger.warning('115 Cookie 检查失败：状态=STATUS_UNAVAILABLE')
            return False
        if active is not False:
            renewal.state = 'VALID' if active is True else 'STATUS_UNAVAILABLE'
            if renewal.state != 'VALID':
                logger.warning('115 Cookie 检查结果异常：状态=STATUS_UNAVAILABLE')
            return False

        primary = cookie_identity(self.config.cookie)
        backup = cookie_identity(self.config.cookie_renewal_seed)
        from p115client.const import SSOENT_TO_APP
        if not primary or not backup or primary[0] != backup[0] or primary[1] == backup[1]:
            renewal.state = 'INVALID_PAIR'
            logger.warning('115 Cookie 自动续登跳过：状态=INVALID_PAIR，请重新配置同账号不同设备的主备 Cookie')
            return False
        device = SSOENT_TO_APP.get(primary[1])
        if not device:
            renewal.state = 'INVALID_PAIR'
            logger.warning('115 Cookie 自动续登跳过：状态=INVALID_PAIR，不支持的主设备类型')
            return False
        attempted_at = int(time.time())
        if attempted_at < renewal.record['attempt_at'] + COOKIE_RENEWAL_COOLDOWN:
            if renewal.state not in ('RENEWAL_FAILED', 'PERSISTENCE_FAILED'):
                renewal.state = 'COOLDOWN'
            logger.warning(f'115 Cookie 主会话已失效，续登冷却中：状态={renewal.state}')
            return False
        try:
            seed = self._new_cookie_client(self.config.cookie_renewal_seed)
            if self._login_status(seed) is not True:
                renewal.state = 'BACKUP_EXPIRED'
                logger.warning('115 Cookie 自动续登失败：状态=BACKUP_EXPIRED，备用会话也已失效，请重新扫码')
                return False
        except Exception:
            renewal.state = 'STATUS_UNAVAILABLE'
            logger.warning('115 Cookie 备用会话检查失败：状态=STATUS_UNAVAILABLE')
            return False
        try:
            renewal.save(attempt_at=attempted_at)
        except OSError:
            renewal.state = 'PERSISTENCE_FAILED'
            logger.warning('115 Cookie 自动续登中止：状态=PERSISTENCE_FAILED')
            return False
        try:
            result = seed.login_with_app(device, timeout=10, follow_redirects=False)
            updated, cookie, _ = self._verify_issued_cookie(result, primary[1], primary[0])
        except Exception:
            renewal.state = 'RENEWAL_FAILED'
            logger.warning('115 Cookie 自动续登失败：状态=RENEWAL_FAILED')
            return False
        try:
            renewal.save(cookie=cookie, renewed_at=int(time.time()))
        except OSError:
            renewal.state = 'PERSISTENCE_FAILED'
            logger.warning('115 Cookie 自动续登保存失败：状态=PERSISTENCE_FAILED')
            return False
        self._sdk = updated
        self._cookie_revision += 1
        renewal.state = 'RENEWED'
        logger.info('115 Cookie 主会话自动续登成功：状态=RENEWED')
        return True

    def _call_moviepilot(self, method, args, kwargs):
        if method not in OPEN_READ_METHODS:
            raise RemoteError('This operation requires 115 Cookie mode')
        if self._moviepilot_storage is None:
            from app.modules.filemanager.storages.u115 import U115Pan
            self._moviepilot_storage = U115Pan()
        # MoviePilot owns refresh and persistence. Explicit Authorization also
        # bypasses the SDK's refresh-and-retry loop for Open API requests.
        token = self._moviepilot_storage.access_token
        if not isinstance(token, str) or not re.fullmatch(r'[!-~]+', token):
            raise RemoteError('MoviePilot 115 authorization unavailable')
        if self._sdk is None:
            from p115client import P115OpenClient
            self._sdk = P115OpenClient(access_token=token, console_qrcode=False)
        if method == 'fs_file':
            args = ({'file_id': args[0]},)
        elif method == 'fs_files':
            args = ({**args[0], 'record_open_time': 0},)
        return getattr(self._sdk, OPEN_READ_METHODS[method])(
            *args, **kwargs, headers={'Authorization': 'Bearer ' + token},
            follow_redirects=False, timeout=self.config.request_timeout,
        )

    def call(self, method, *args, **kwargs):
        if method == 'download_url' and not self.config.use_moviepilot_auth:
            if self._closed:
                raise RemoteError('115 client is stopped')
            if not self._download_slots.acquire(timeout=COOKIE_DOWNLOAD_WAIT):
                error = ToolError('115 playback download slots busy')
                error.operation = method
                raise error
            try:
                return self._call_with_recovery(method, args, kwargs, download=True)
            finally:
                self._download_slots.release()

        acquired = self._lock.acquire(timeout=2.0)
        if not acquired:
            owner = self._lock_owner or 'unknown'
            logger.warning(f'[v{PLUGIN_VERSION}] 115 SDK等待全局调用锁：待执行接口={method}，当前占用={owner}')
            self._lock.acquire()
        prev_owner = self._lock_owner
        self._lock_owner = f'{threading.current_thread().name}:{method}'
        try:
            # Cookie renewal can invalidate the previous session. Keep it out of
            # in-flight writes, whose results may require checkpoint recovery.
            if method not in COOKIE_RETRY_READS:
                with self._auth_lock:
                    return self._call_with_recovery(method, args, kwargs)
            return self._call_with_recovery(method, args, kwargs)
        finally:
            self._lock_owner = prev_owner
            self._lock.release()

    def _call_with_recovery(self, method, args, kwargs, *, download=False):
        started = time.monotonic()
        cookie_revision = self._cookie_revision
        try:
            return self._call_once(method, args, kwargs, download=download)
        except RemoteError as exc:
            code = getattr(exc, 'upstream_code', None)
            sdk_error = getattr(exc, 'sdk_error', None)
            if method in COOKIE_RETRY_READS and code in (99, 990001):
                with self._auth_lock:
                    renewed = (self._cookie_revision != cookie_revision
                               or self._check_cookie_locked(on_auth_reject=True))
                if renewed:
                    return self._call_once(method, args, kwargs, download=download)
            if (method in TRANSIENT_RETRY_READS and not isinstance(exc, MissingFile)
                    and code is None and sdk_error in TRANSIENT_SDK_ERRORS and not self._closed):
                logger.info(f'115只读请求遇网络抖动，重试一次：接口={method}，SDK异常={sdk_error}')
                time.sleep(0.3)
                return self._call_once(method, args, kwargs, download=download)
            raise
        finally:
            elapsed = time.monotonic() - started
            if elapsed >= 5.0:
                logger.warning(f'[v{PLUGIN_VERSION}] 115 SDK慢调用：接口={method}，耗时={elapsed:.1f}s')

    @staticmethod
    def _cookie_download(sdk, pickcode, **kwargs):
        # We already have a pickcode. The SDK's high-level wrapper first tries
        # to discover its account-wide stable point via a hidden directory read.
        download = getattr(sdk, 'download_url_app', None)
        if not callable(download):
            return sdk.download_url(pickcode, **kwargs)
        if not isinstance(pickcode, str) or not PICKCODE_RE.fullmatch(pickcode):
            raise RemoteError('115 playback link unavailable')
        from p115client import check_response as sdk_check_response

        response = sdk_check_response(download(pickcode, **kwargs))
        data = response.get('data')
        url = data.get('url') if isinstance(data, dict) else None
        if url is None and isinstance(data, dict) and len(data) == 1:
            entry = next(iter(data.values()))
            if isinstance(entry, dict) and entry.get('pick_code') == pickcode and isinstance(entry.get('url'), dict):
                url = entry['url'].get('url')
        if not isinstance(url, str) or not url:
            raise RemoteError('115 playback link unavailable')
        return DownloadLink(url, response.get('headers') or {})

    def _call_once(self, method, args, kwargs, *, download):
        if self._closed:
            raise RemoteError('115 client is stopped')
        try:
            if download:
                with self._auth_lock:
                    wait = 0.05 - (time.monotonic() - self._last_download_start)
                    if wait > 0:
                        time.sleep(wait)
                    self._last_download_start = time.monotonic()
                    sdk = self._client()
            else:
                min_gap = 0.05 if method in TRANSIENT_RETRY_READS else 0.2
                wait = min_gap - (time.monotonic() - self._last_request)
                if wait > 0:
                    time.sleep(wait)
            if self.config.use_moviepilot_auth:
                result = self._call_moviepilot(method, args, kwargs)
                if method == 'fs_file' and response_code(result) == 20004:
                    error = MissingFile('Remote file no longer exists')
                    error.upstream_code = 20004
                    raise error
            else:
                call_kwargs = dict(kwargs)
                if not download:
                    sdk = self._client()
                if self._use_httpx_transport(sdk):
                    call_kwargs.setdefault('request', self._httpx_request)
                call_kwargs['timeout'] = self.config.request_timeout
                if download and call_kwargs.get('app') == 'chrome':
                    result = self._cookie_download(sdk, *args, **call_kwargs)
                else:
                    result = getattr(sdk, method)(*args, **call_kwargs)
            if (method=='share_send' and isinstance(result,dict) and result.get('state') in (False,0)
                    and isinstance(result.get('data',{}),dict) and not result.get('data',{}).get('share_code')):
                error=ShareRejected('115 rejected share creation')
                error.upstream_code=response_code(result)
                raise error
            if isinstance(result,dict) and result.get('state') in (False,0): check_response(result)
            return result
        except RemoteError as exc:
            exc.operation=method
            raise
        except Exception as exc:
            # SDK exceptions may embed cookie-bearing request representations.
            code=next((code for arg in exc.args if (code:=response_code(arg)) is not None),None)
            # Only a typed SDK absence on a read is proof that a copy disappeared.
            missing=(method in ('fs_file','fs_files','fs_files_app') and isinstance(exc,FileNotFoundError)
                     and type(exc).__module__=='p115client.exception')
            error=(MissingFile('Remote file no longer exists') if missing else RemoteError('115 SDK request failed'))
            error.operation=method;error.upstream_code=code;error.sdk_error=type(exc).__name__
            raise error from None
        finally:
            if not download:
                self._last_request = time.monotonic()

    def directory_path(self, cid="0"):
        """Read the selected directory's ancestors, without trusting a manual prefix."""
        return self._read_directory_path(cid, 'fs_files')

    def temporary_root_path(self, cid):
        return self._read_directory_path(cid, self._copy_list_method())

    def _read_directory_path(self, cid, method):
        if str(cid) == '0':
            return '/'
        response = self._directory_page(method, str(cid), offset=0, limit=1)
        return self._directory_path(response, str(cid))

    def _directory_path(self, response, cid):
        trail = response.get('path')
        if cid == '0' and not trail:
            return '/'
        if (not isinstance(trail, list) or not trail
                or not all(isinstance(item, dict) for item in trail)
                or str(trail[-1].get('cid')) != str(cid)):
            raise SafetyError('115 directory identity mismatch')
        from .strm import safe_parts
        parts, seen = [], set()
        for item in trail:
            fid = str(item.get('cid', ''))
            if not re.fullmatch(r'[0-9]{1,20}', fid) or fid in seen:
                raise SafetyError('Invalid directory ancestors')
            seen.add(fid)
            if fid == '0':
                if parts:
                    raise SafetyError('Invalid directory root position')
                continue
            name = item.get('name') or item.get('n')
            if not isinstance(name, str) or len(safe_parts(name)) != 1:
                raise SafetyError('Invalid directory ancestor name')
            parts.append(name)
        if not parts and cid != '0':
            raise SafetyError('Directory ancestors unavailable')
        return '/' + '/'.join(parts)

    def _copy_list_method(self):
        if not self.config.use_moviepilot_auth:
            with self._lock:
                if callable(getattr(self._client(), 'fs_files_app', None)):
                    return 'fs_files_app'
        return 'fs_files'

    def _directory_page(self, method, cid, *, offset, limit):
        payload = {'cid': cid, 'offset': offset, 'limit': limit, 'cur': 1, 'show_dir': 1}
        if method == 'fs_files_app':
            payload['record_open_time'] = 0
        return check_response(self.call(method, payload))

    def list_verified_folder(self, cid, expected_parent, expected_name):
        """Verify the folder identity against its ancestors and list its complete contents."""
        offset = 0
        seen = set()
        expected_count = None
        results = []
        list_method = self._copy_list_method()
        while True:
            resp = self._directory_page(list_method, cid, offset=offset, limit=1000)
            directory = self._directory_path(resp, str(cid))
            trail = resp.get("path", [])
            if (not isinstance(trail, list) or len(trail) < 2
                    or str(trail[-1].get("cid")) != str(cid)
                    or (trail[-1].get("name") or trail[-1].get("n")) != expected_name
                    or str(trail[-2].get("cid")) != str(expected_parent)):
                raise SafetyError("Temporary directory identity changed")
            rows = resp.get("data")
            if not isinstance(rows, list):
                raise RemoteError("Malformed directory response")
            count = resp.get('count')
            if (type(count) not in (str, int) or not str(count).isdigit()
                    or (expected_count is not None and expected_count != int(count))
                    or offset + len(rows) > int(count)):
                raise RemoteError('Incomplete directory pagination')
            expected_count = int(count)
            for row in rows:
                name = row.get('n') or row.get('fn') or row.get('file_name') or row.get('name') or row.get('category_name')
                from .strm import safe_parts
                if not isinstance(name, str) or len(safe_parts(name)) != 1:
                    raise SafetyError('Invalid listed file name')
                if self.config.use_moviepilot_auth or (list_method == 'fs_files_app' and 'fc' in row):
                    category = str(row.get('fc', ''))
                    if category not in ('0', '1'):
                        raise RemoteError('Unsupported 115 file metadata shape')
                    row = {**row, 'is_directory': category == '0'}
                file = normalize_file(row, str(cid), directory.rstrip('/') + '/' + name)
                if file.file_id in seen:
                    raise RemoteError("Directory changed while paging; retry scan")
                seen.add(file.file_id)
                results.append(file)
            offset += len(rows)
            if offset == expected_count:
                break
            if not rows:
                raise RemoteError("Incomplete directory pagination")
        return results

    def list_files(self, cid="0"):
        offset = 0
        seen = set()
        expected_count = None
        while True:
            resp = check_response(self.call("fs_files", {"cid": cid, "offset": offset, "limit": 1000, "cur": 1, "show_dir": 1}))
            directory = self._directory_path(resp, str(cid))
            # fs_files silently falls back to root for a non-existing directory.
            if str(cid) != "0":
                trail = resp.get("path", [])
                if not trail or str(trail[-1].get("cid")) != str(cid):
                    raise SafetyError("115 returned another directory; scan aborted")
            rows = resp.get("data")
            if not isinstance(rows, list):
                raise RemoteError("Malformed directory response")
            if self.config.use_moviepilot_auth:
                count = resp.get('count')
                if (type(count) not in (str, int) or not str(count).isdigit()
                        or (expected_count is not None and expected_count != int(count))
                        or offset + len(rows) > int(count)):
                    raise RemoteError('Incomplete directory pagination')
                expected_count = int(count)
            for row in rows:
                name = row.get('n') or row.get('fn') or row.get('file_name') or row.get('name') or row.get('category_name')
                from .strm import safe_parts
                if not isinstance(name, str) or len(safe_parts(name)) != 1:
                    raise SafetyError('Invalid listed file name')
                if self.config.use_moviepilot_auth:
                    category = str(row.get('fc', ''))
                    if category not in ('0', '1'):
                        raise RemoteError('Unsupported 115 file metadata shape')
                    row = {**row, 'is_directory': category == '0'}
                file = normalize_file(row, str(cid), directory.rstrip('/') + '/' + name)
                if file.file_id in seen:
                    raise RemoteError("Directory changed while paging; retry scan")
                seen.add(file.file_id)
                yield file
            offset += len(rows)
            count = int(resp.get("count", offset))
            if offset >= count:
                break
            if not rows:
                raise RemoteError("Incomplete directory pagination")

    def stat(self, fid):
        resp = check_response(self.call("fs_file", str(fid)))
        rows = resp.get("data")
        if not rows:
            raise MissingFile("Remote file not found")
        data=rows[0] if isinstance(rows,list) else rows
        if self.config.use_moviepilot_auth and data.get('parent_id') is None and 'category_id' in data:
            data = {**data, 'parent_id': data['category_id']}
        file=normalize_file(data)
        if file.file_id!=str(fid):
            raise SafetyError('115 file identity mismatch')
        # get_info also exposes permanent-deletion metadata (aid=120).
        # Deleted records are not live identities, even when old fields changed.
        # The pinned SDK documents aid=7 as recycle bin and aid=120 as deleted.
        if str(data.get('aid','')) in ('7','120'):
            error=MissingFile('Remote file no longer exists')
            error.operation='fs_file'
            raise error
        return file

    def _link(self, value):
        url = value.url if isinstance(value, DownloadLink) else str(value)
        headers = dict(getattr(value, "headers", {}) or {})
        parsed = self._validate_url_parsed(url)
        # Header-bound direct URLs cannot be played using a pure redirect if a
        # Cookie is required. Do NOT forward the account cookie to Emby/CDN.
        if (any(k == "f" and v == "3" for k, v in parse_qsl(parsed.query))
                or any(k.lower() == "cookie" and v for k, v in headers.items())):
            raise SafetyError("115 URL requires cookies and cannot be safely used for 302")
        return DownloadLink(url, headers)

    def _validate_url_parsed(self, url):
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password
                or parsed.port not in (None, 80, 443) or "\r" in url or "\n" in url):
            raise SafetyError("Unsafe download URL")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise SafetyError("IP-address download URLs are not allowed")
        if not any(host == s or host.endswith("." + s) for s in self.config.allowed_cdn_suffixes if s):
            raise SafetyError("Download URL is outside configured 115 CDN domains")
        return parsed

    def validate_url(self, url):
        self._validate_url_parsed(url)

    def normal_link(self, pickcode, ua, *, file_id=None):
        if self.config.use_moviepilot_auth:
            if not isinstance(pickcode, str) or not PICKCODE_RE.fullmatch(pickcode) or not file_id:
                raise SafetyError('115 file identity mismatch')
            response = check_response(self.call('download_url', pickcode, user_agent=ua))
            entries = response.get('data')
            entry = entries.get(str(file_id)) if isinstance(entries, dict) and len(entries) == 1 else None
            if not isinstance(entry, dict) or entry.get('pick_code', pickcode) != pickcode:
                raise SafetyError('115 file identity mismatch')
            link = entry.get('url')
            if not isinstance(link, dict) or not isinstance(link.get('url'), str):
                raise RemoteError('115 playback link unavailable')
            # The SDK echoes request headers. Validate the CDN URL without
            # propagating the Authorization header or other account credentials.
            return self._link(link['url'])
        # The SDK defaults to os_windows, which can reject a valid web cookie
        # with errno 99 even while file reads and chrome downloads succeed.
        return self._link(self.call("download_url", pickcode, user_agent=ua, app="chrome"))

    def share_files(self, code, password, cid='0'):
        offset, seen = 0, set()
        while True:
            response = check_response(self.call('share_snap', {'share_code':code,
                'receive_code':password, 'cid':cid, 'offset':offset, 'limit':1000}))
            data = response.get('data', {})
            rows = data.get('list')
            count = data.get('count')
            if not isinstance(rows, list) or type(count) not in (int,str) or not str(count).isdigit():
                raise RemoteError('Malformed share listing')
            if int(count) > 100000: raise SafetyError('Share listing limit')
            for row in rows:
                file = normalize_file(row, cid)
                from .strm import safe_parts
                if (not re.fullmatch(r'[1-9][0-9]{0,19}',file.file_id)
                        or file.file_id in seen or len(safe_parts(file.name)) != 1):
                    raise SafetyError('Unsafe share member')
                seen.add(file.file_id)
                yield file
            offset += len(rows)
            if offset == int(count): break
            if not rows or offset > int(count): raise RemoteError('Incomplete share listing')

    def create_share(self, fid):
        response = check_response(self.call('share_send', {'file_ids':fid}))
        data = response.get('data', {})
        code, password = data.get('share_code'), data.get('receive_code')
        if (not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9]{6,64}',code)
                or not isinstance(password,str) or not re.fullmatch(r'[A-Za-z0-9]{4}',password)):
            raise RemoteError('Incomplete share creation result')
        return code, password

    def retain_share(self, code):
        check_response(self.call('share_update', {'share_code':code,'share_duration':-1}))

    def share_link(self, code, password, fid, ua):
        # The pinned SDK's default android value falls back to os_windows.
        # Select the chrome share endpoint explicitly, as for normal downloads.
        return self._link(self.call('share_download_url', {'share_code':code,
            'receive_code':password,'file_id':fid}, app='chrome', headers={'User-Agent':ua}))

    def create_temp_directory(self, parent, name):
        response = check_response(self.call('fs_mkdir', name, pid=parent))
        cid = str(response.get('cid') or response.get('data',{}).get('cid') or '')
        if not re.fullmatch(r'[1-9][0-9]{0,19}',cid): raise RemoteError('Missing temporary directory identity')
        return cid

    def receive_to_temp(self, code, password, fid, cid):
        check_response(self.call('share_receive', {'share_code':code,
            'receive_code':password,'file_id':fid,'cid':cid}))

    def delete_verified_file(self, fid):
        # Only callers holding a persisted intent and verified file identity use this.
        check_response(self.call('fs_delete', fid))

    def recycle_entries(self):
        """Complete bounded enumeration; never infer an entry ID from a file ID."""
        entries=[]; seen=set(); offset=0; expected_count=None
        while True:
            response=check_response(self.call('recyclebin_list',{'offset':offset,'limit':1000}))
            rows=response.get('data'); count=response.get('count')
            if isinstance(rows,dict): count=rows.get('count');rows=rows.get('list')
            if not isinstance(rows,list) or type(count) not in (int,str) or not str(count).isdigit() or int(count)>100000:
                raise SafetyError('Unsupported recycle listing')
            if expected_count is not None and int(count)!=expected_count:
                raise SafetyError('Incomplete recycle listing')
            expected_count=int(count)
            for row in rows:
                if not isinstance(row,dict): raise SafetyError('Unsupported recycle entry')
                rid=str(row.get('rid') or row.get('id') or '')
                if not re.fullmatch(r'[1-9][0-9]{0,19}',rid) or rid in seen:
                    raise SafetyError('Invalid recycle entry identity')
                seen.add(rid);entries.append({**row,'rid':rid})
            offset+=len(rows)
            if offset==int(count): return entries
            if not rows or offset>int(count): raise SafetyError('Incomplete recycle listing')

    def purge_recycle_entry(self, rid):
        if not isinstance(rid,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',rid):
            raise SafetyError('Invalid recycle deletion target')
        check_response(self.call('recyclebin_clean',{'tid':rid},password=self.config.recycle_password))

    def purge_recycle_batch(self, rids):
        # Empty tid means erase the entire live bin in the SDK. Always send
        # explicit, bounded snapshot targets, including for scheduled emptying.
        if (not isinstance(rids,list) or not 1<=len(rids)<=100 or len(set(rids))!=len(rids)
                or any(not isinstance(rid,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',rid) for rid in rids)):
            raise SafetyError('Invalid recycle deletion targets')
        if not re.fullmatch(r'[0-9]{6}',self.config.recycle_password):
            raise SafetyError('Recycle security key required')
        check_response(self.call('recyclebin_clean',{'tid':','.join(rids)},password=self.config.recycle_password))

    def probe_range(self, link, ua, size):
        from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        self.validate_url(link.url)
        request = Request(link.url, headers={'User-Agent':ua,'Range':'bytes=0-0','Accept-Encoding':'identity'})
        try:
            with build_opener(NoRedirect(), ProxyHandler({})).open(request, timeout=self.config.request_timeout) as response:
                if (response.status != 206 or response.headers.get('Content-Range') != f'bytes 0-0/{size}'
                        or len(response.read(2)) != 1):
                    raise SafetyError('Share playback validation failed')
        except SafetyError: raise
        except Exception: raise RemoteError('Share playback validation failed') from None
