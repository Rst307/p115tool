from __future__ import annotations
import ipaddress
import re
import threading
import time
from urllib.parse import urlsplit
from .models import RemoteFile, DownloadLink, RemoteError, MissingFile, SafetyError


def check_response(response):
    if not isinstance(response, dict) or response.get("state") not in (True, 1):
        # Never include upstream messages/payloads: they can contain cookies or URLs.
        code=response.get('errno') if isinstance(response,dict) else None
        code=code if type(code) is int else None
        error=(MissingFile("Remote file no longer exists") if code in (20013, 20018, 50003, 90008)
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
    """One authenticated session. All remote operations share a serialized gate.

    No automatic login on expired cookies; update credentials explicitly instead of
    launching QR login/headless loops or retrying destructive calls.
    """
    def __init__(self, config, sdk=None):
        self.config = config
        self._lock = threading.RLock()
        self._sdk = sdk
        self._last_request = 0.0
        self._closed = False

    def close(self):
        # The pinned SDK has no per-client close/session API. Drain our shared
        # gate and release the SDK instance; never create a client
        # just to stop it or close SDK-global transports used by other plugins.
        with self._lock:
            self._closed = True
            self._sdk = None

    def _client(self):
        if self._sdk is None:
            if not self.config.cookie:
                raise RemoteError("115 cookie is not configured")
            try:
                from p115client import P115Client
            except ImportError as exc:
                raise RemoteError("Install p115client in the MoviePilot environment") from exc
            # p115client 0.0.9.7.2 logs in only when cookies=None. Always pass
            # explicit cookies and never call login()/relogin() automatically.
            self._sdk = P115Client(self.config.cookie, console_qrcode=False)
        return self._sdk

    def call(self, method, *args, **kwargs):
        with self._lock:
            if self._closed:
                raise RemoteError('115 client is stopped')
            wait = 0.2 - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                return getattr(self._client(), method)(*args, timeout=self.config.request_timeout, **kwargs)
            except MissingFile:
                raise
            except Exception as exc:
                # SDK exceptions may embed cookie-bearing request representations.
                raise RemoteError(f"115 {method} failed ({type(exc).__name__})") from None
            finally:
                self._last_request = time.monotonic()

    def directory_path(self, cid="0"):
        """Read the selected directory's ancestors, without trusting a manual prefix."""
        if str(cid) == '0':
            return '/'
        response = check_response(self.call('fs_files', {
            'cid': str(cid), 'offset': 0, 'limit': 1, 'cur': 1, 'show_dir': 1}))
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

    def list_files(self, cid="0"):
        offset = 0
        seen = set()
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
            for row in rows:
                name = row.get('n') or row.get('fn') or row.get('file_name') or row.get('name') or row.get('category_name')
                from .strm import safe_parts
                if not isinstance(name, str) or len(safe_parts(name)) != 1:
                    raise SafetyError('Invalid listed file name')
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
        return normalize_file(rows[0] if isinstance(rows, list) else rows)

    def _link(self, value):
        url = str(value)
        headers = dict(getattr(value, "headers", {}) or {})
        self.validate_url(url)
        # Header-bound direct URLs cannot be played using a pure redirect if a
        # Cookie is required. Do NOT forward the account cookie to Emby/CDN.
        query = dict(__import__("urllib.parse", fromlist=["parse_qsl"]).parse_qsl(urlsplit(url).query))
        if query.get("f") == "3" or any(k.lower() == "cookie" and v for k, v in headers.items()):
            raise SafetyError("115 URL requires cookies and cannot be safely used for 302")
        return DownloadLink(url, headers)

    def validate_url(self, url):
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password
                or parsed.port not in (None, 80, 443) or any(c in url for c in ("\r", "\n"))):
            raise SafetyError("Unsafe download URL")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise SafetyError("IP-address download URLs are not allowed")
        if not any(host == s or host.endswith("." + s) for s in self.config.allowed_cdn_suffixes if s):
            raise SafetyError("Download URL is outside configured 115 CDN domains")

    def normal_link(self, pickcode, ua):
        return self._link(self.call("download_url", pickcode, user_agent=ua))

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
        return self._link(self.call('share_download_url', {'share_code':code,
            'receive_code':password,'file_id':fid}, headers={'User-Agent':ua}))

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
