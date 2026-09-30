from __future__ import annotations
import ipaddress
import re
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from .models import RemoteFile, DownloadLink, RemoteError, MissingFile, SafetyError


def check_response(response):
    if not isinstance(response, dict) or response.get("state") not in (True, 1):
        # Never include upstream messages/payloads: they can contain cookies or URLs.
        if isinstance(response, dict) and response.get("errno") in (20018, 50003, 90008):
            raise MissingFile("Remote file no longer exists")
        raise RemoteError("115 request failed or returned an unrecognized response")
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


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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

    def login_status(self):
        result=self.call('login_status')
        if type(result) is not bool:
            raise RemoteError('Unsupported login status response')
        return result

    def refresh(self):
        if not self.login_status():
            raise RemoteError("115 authentication expired; replace cookie")

    def account_capacity(self):
        response = check_response(self.call('fs_index_info', {'count_space_nums':0}))
        # Only recognize explicit byte counters. Never pass raw account/device
        # payloads through, parse human units, or convert unknown fields to zero.
        data = response.get('data', response)
        if not isinstance(data,dict):
            return None
        space = data.get('space_info')
        if not isinstance(space,dict):
            return None
        result = {}
        for output, field in [('total_bytes','all_total'),('used_bytes','all_use'),('free_bytes','all_remain')]:
            value = space.get(field)
            if isinstance(value,dict):
                value=value.get('size')
            if type(value) is int:
                number=value
            elif isinstance(value,str) and re.fullmatch(r'[0-9]+',value):
                if len(value)>19:
                    return None
                number=int(value)
            else:
                return None
            if not 0 <= number <= 2**63-1:
                return None
            result[output]=number
        if result['used_bytes']>result['total_bytes'] or result['free_bytes']>result['total_bytes']:
            return None
        return result

    def list_files(self, cid="0"):
        offset = 0
        seen = set()
        while True:
            resp = check_response(self.call("fs_files", {"cid": cid, "offset": offset, "limit": 1000, "cur": 1, "show_dir": 1}))
            # fs_files silently falls back to root for a non-existing directory.
            if str(cid) != "0":
                trail = resp.get("path", [])
                if not trail or str(trail[-1].get("cid")) != str(cid):
                    raise SafetyError("115 returned another directory; scan aborted")
            rows = resp.get("data")
            if not isinstance(rows, list):
                raise RemoteError("Malformed directory response")
            for row in rows:
                file = normalize_file(row, str(cid))
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

    def share_link(self, share, ua):
        payload = {"share_code": share["share_code"], "receive_code": share["receive_code"], "file_id": share["file_id"]}
        return self._link(self.call("share_download_url", payload, headers={"User-Agent": ua}))

    def create_share(self, fid):
        ids = [str(value) for value in fid] if isinstance(fid, (list, tuple)) else [str(fid)]
        if not ids or len(ids) > 1000 or any(not value.isdigit() for value in ids):
            raise ValueError("Share creation requires 1..1000 numeric file IDs")
        resp = check_response(self.call("share_send", {"file_ids": ",".join(ids)}))
        data = resp.get("data", {})
        code = data.get("share_code")
        password = data.get("receive_code")
        if not code or not password:
            raise RemoteError("Share response is missing its code/password")
        return str(code), str(password)

    def ensure_share_retention(self, code):
        # share_send does not document retention settings. Change it through
        # updateshare ONLY after the caller has durably saved the returned code.
        check_response(self.call('share_update', {'share_code':code,'share_duration':-1}))

    def share_files(self, code, password, cid="0"):
        offset = 0
        seen = set()
        while True:
            resp = check_response(self.call("share_snap", {"share_code": code, "receive_code": password,
                                                           "cid": cid, "offset": offset, "limit": 1000}))
            data = resp.get("data", {})
            rows = data.get("list")
            if not isinstance(rows, list):
                raise RemoteError("Malformed share snapshot")
            for row in rows:
                file = normalize_file(row, str(cid))
                if file.file_id in seen:
                    raise RemoteError("Share changed while paging")
                seen.add(file.file_id)
                yield file
            offset += len(rows)
            if offset >= int(data.get("count", offset)):
                break
            if not rows:
                raise RemoteError("Incomplete share pagination")

    def find_share_file(self, code, password, expected):
        stack, visited, matches = ["0"], set(), []
        while stack:
            cid = stack.pop()
            if cid in visited or len(visited) > 10000:
                raise RemoteError("Share directory cycle or traversal limit")
            visited.add(cid)
            for file in self.share_files(code, password, cid):
                if file.is_dir:
                    stack.append(file.file_id)
                elif expected.matches(file):
                    matches.append(file)
        if len(matches) != 1:
            raise SafetyError("Share must contain exactly one matching name/size/SHA1 file")
        return matches[0]

    def make_cache_folder(self, parent, name):
        return self.make_directory(parent,name)

    def make_directory(self, parent, name):
        resp = check_response(self.call("fs_mkdir", name, pid=parent))
        cid = resp.get("cid", resp.get("data", {}).get("cid"))
        if not cid:
            raise RemoteError("Missing cache folder ID")
        return str(cid)

    def restore(self, share, cid):
        check_response(self.call("share_receive", {"share_code": share["share_code"], "receive_code": share["receive_code"], "file_id": share["file_id"], "cid": cid}))

    def delete(self, fid):
        check_response(self.call("fs_delete", str(fid)))

    def move(self, fid, parent):
        # fs_move otherwise defaults to irreversible replacement on collisions.
        import json
        check_response(self.call("fs_move", {"fid": str(fid), "pid": parent,
            "conflict_policy": json.dumps({str(fid): {"action": "keep_both"}})}))

    def rename(self, fid, name):
        check_response(self.call("fs_rename", (str(fid), name)))

    def probe_range(self, link, ua, size):
        self.validate_url(link.url)
        # Disable environment proxies and redirects; request exactly one byte,
        # read at most two bytes even if the remote server ignores Range.
        req = Request(link.url, headers={"User-Agent": ua, "Range": "bytes=0-0", "Accept-Encoding": "identity"})
        opener = build_opener(NoRedirect(), ProxyHandler({}))
        try:
            with opener.open(req, timeout=self.config.request_timeout) as response:
                if response.status != 206 or response.headers.get("Content-Range") != f"bytes 0-0/{size}":
                    raise SafetyError("Share did not pass the bounded Range check")
                if len(response.read(2)) != 1:
                    raise SafetyError("Share Range length mismatch")
        except SafetyError:
            raise
        except Exception:
            raise RemoteError("Share Range request failed") from None
