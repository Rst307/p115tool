"""Read public previews or joined Telegram channels into virtual share imports.

No bot, channel joins, outbound messages, media downloads or 115 writes here.
Credentials stay in private local files; public state contains fixed error codes.
"""
import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit, unquote

from .storage import parse_share
from .models import ToolError
from .import_history import message_title, display_name

URL = re.compile(r'https?://[^\s<>"\'，。；）)\]】]+', re.I)
PASSWORD = re.compile(r'(?:提取码|提取密码|访问码|访问密码|密码|口令|pwd|password)\s*[:：=]?\s*([A-Za-z0-9]{4})(?![A-Za-z0-9])', re.I)
BACKLOG_LIMIT = 1000
NETWORK_INTERVAL = 300
NETWORK_COOLDOWN = 30
NETWORK_TIMEOUT = 15
RETRYABLE = "state IN ('FAILED','PARTIAL','INTERRUPTED') AND share_code!='' AND coalesce(error,'')!='EXTRACTION_AMBIGUOUS'"
DEFAULTS = {'enabled': False, 'mode': 'web', 'api_id': 0, 'api_hash': '', 'channels': [],
            'interval': 60, 'history_limit': 0, 'proxy': '', 'auto_upgrade': True}


def normalize_channel(value):
    if not isinstance(value, str): raise ValueError('Invalid Telegram channel')
    value = value.strip()
    if value.startswith('https://t.me/'):
        value = value[len('https://t.me/'):].rstrip('/')
        if value.startswith('s/'): value = value[2:]
    if value.startswith('@'): value = value[1:]
    if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', value): return '@' + value.lower()
    if re.fullmatch(r'-100[1-9][0-9]{0,15}', value): return value
    raise ValueError('Invalid Telegram channel')


def proxy_options(value):
    if not value: return None
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise ValueError('Invalid Telegram proxy')
    url = urlsplit(value)
    if (url.scheme not in ('socks5', 'http') or not url.hostname or not url.port
            or url.path not in ('', '/') or url.query or url.fragment):
        raise ValueError('Invalid Telegram proxy')
    return {'proxy_type': url.scheme, 'addr': url.hostname, 'port': url.port,
            'username': unquote(url.username) if url.username else None,
            'password': unquote(url.password) if url.password else None, 'rdns': True}


def extract_shares(message):
    """Parse visible URLs, UTF-16 text links and URL buttons without fetching URLs.

    A password belongs to the following text up to the next share. A single
    share may also use a code before its link. Ambiguous codes are never guessed.
    """
    text = (getattr(message, 'message', '') or '')[:65536]
    candidates = [(m.start(), m.end(), m.group()) for m in URL.finditer(text)]
    encoded = text.encode('utf-16-le')
    for entity in getattr(message, 'entities', None) or []:
        url = getattr(entity, 'url', None)
        if not isinstance(url, str): continue
        offset = getattr(entity, 'offset', 0); length = getattr(entity, 'length', 0)
        if type(offset) is not int or type(length) is not int or offset < 0 or length < 0: continue
        start = len(encoded[:offset * 2].decode('utf-16-le', errors='ignore'))
        end = len(encoded[:(offset + length) * 2].decode('utf-16-le', errors='ignore'))
        candidates.append((start, end, url))
    markup = getattr(message, 'reply_markup', None)
    for row in getattr(markup, 'rows', None) or []:
        for button in getattr(row, 'buttons', None) or []:
            url = getattr(button, 'url', None)
            if isinstance(url, str): candidates.append((len(text), len(text), url))
    valid = []
    for start, end, url in sorted(set(candidates)):
        try: code, password = parse_share(url, '')
        except (ValueError, TypeError): continue
        valid.append((start, end, code, password))
    results = []; ambiguous = 0
    for index, (start, end, code, password) in enumerate(valid):
        if not password:
            stop = valid[index + 1][0] if index + 1 < len(valid) else len(text)
            context = text[end:min(stop, end + 400)]
            if len(valid) == 1: context = text
            codes = set(PASSWORD.findall(context))
            if len(codes) > 1:
                ambiguous += 1; continue
            password = next(iter(codes), '')
        if (code, password) not in results: results.append((code, password))
    return results, ambiguous


def private_write(path, text):
    temporary = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            output.write(text); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()


class TelegramMonitor:
    def __init__(self, service):
        self.service = service
        self.directory = Path(service.config.data_dir)
        self.settings_path = self.directory / 'telegram-settings.json'
        self.session_path = self.directory / 'telegram-account.session'
        self.settings = dict(DEFAULTS, channels=[])
        self._guard = threading.RLock(); self._thread = None; self._loop = None
        self._ready = threading.Event(); self._closing = False
        self._client = None; self._web = None; self._phone = ''; self._phone_hash = ''; self._login_until = 0
        self._state = 'DISABLED'; self._error = None; self._last_poll = None
        self._channels = []; self._next_poll = 0
        self._authorized = False; self._poll_task = None
        self._rate_until = 0
        self._network_future = None; self._network_next = 0; self._network_requested_at = None
        service.query('''CREATE TABLE IF NOT EXISTS telegram_cursors (
            channel TEXT PRIMARY KEY, message_id INTEGER NOT NULL)''')
        service.query('''CREATE TABLE IF NOT EXISTS telegram_imports (
            id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL,
            channel TEXT NOT NULL, message_id INTEGER NOT NULL,
            share_code TEXT NOT NULL, password TEXT NOT NULL, state TEXT NOT NULL,
            task_id TEXT, done INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
            error TEXT, created_at REAL NOT NULL)''')
        service.query('CREATE INDEX IF NOT EXISTS telegram_imports_state ON telegram_imports(state,id)')
        service.query('CREATE INDEX IF NOT EXISTS telegram_imports_task ON telegram_imports(task_id)')
        columns={c['name'] for c in service.query('PRAGMA table_info(telegram_imports)')}
        for name in ('upgraded','skipped'):
            if name not in columns:
                service.query(f'ALTER TABLE telegram_imports ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')
        for name, definition in (('title', "TEXT NOT NULL DEFAULT ''"), ('media_names', "TEXT NOT NULL DEFAULT '[]'"),
                                 ('total', 'INTEGER'), ('started_at', 'REAL'), ('finished_at', 'REAL'),
                                 ('updated_at', 'REAL'), ('attempts', 'INTEGER NOT NULL DEFAULT 0')):
            if name not in columns:
                service.query(f'ALTER TABLE telegram_imports ADD COLUMN {name} {definition}')
        service.query('CREATE INDEX IF NOT EXISTS telegram_resource_share ON resource_storage(share_code)')
        # A Telegram import never requests a remote write. Interrupted indexing
        # can safely be explicitly retried with the existing stable resource IDs.
        service.query("UPDATE telegram_imports SET state='INTERRUPTED',error='RESTARTED',finished_at=?,updated_at=? WHERE state IN ('QUEUED','RUNNING')", (time.time(),time.time()))
        if self.settings_path.exists():
            try: self.settings = self._validate(json.loads(self.settings_path.read_text()), preserve=False)
            except Exception: self._error = 'CONFIG_INVALID'
        self._network = self._network_unknown()
        if self.settings['enabled']:
            self._state = 'CONNECTING'; self._ensure_thread()

    def _validate(self, data, preserve=True):
        if not isinstance(data, dict) or set(data) - set(DEFAULTS) - {'clear_proxy'}:
            raise ValueError('Invalid Telegram settings')
        result = {**DEFAULTS, **data}
        # Existing saved settings and cached account-mode pages remain valid.
        if 'mode' not in data: result['mode'] = self.settings['mode'] if preserve else 'account'
        if result['mode'] not in ('web', 'account'): raise ValueError('Invalid Telegram mode')
        if type(result['enabled']) is not bool: raise ValueError('Invalid Telegram switch')
        if type(result['auto_upgrade']) is not bool: raise ValueError('Invalid upgrade switch')
        if 'auto_upgrade' not in data and preserve: result['auto_upgrade'] = self.settings['auto_upgrade']
        for key, low, high in (('api_id', 0, 2147483647), ('interval', 30, 3600), ('history_limit', 0, 500)):
            value = result[key]
            if isinstance(value, str) and re.fullmatch(r'\d{1,10}', value): value = int(value)
            if type(value) is not int or not low <= value <= high: raise ValueError('Invalid Telegram setting')
            result[key] = value
        if not isinstance(result['api_hash'], str) or (result['api_hash'] and not re.fullmatch(r'[a-fA-F0-9]{32}', result['api_hash'])):
            raise ValueError('Invalid Telegram API hash')
        if preserve and not result['api_hash']: result['api_hash'] = self.settings['api_hash']
        if not isinstance(result['channels'], list) or len(result['channels']) > 30:
            raise ValueError('Invalid Telegram channels')
        result['channels'] = list(dict.fromkeys(normalize_channel(c) for c in result['channels']))
        if result['mode'] == 'web' and any(not c.startswith('@') for c in result['channels']):
            raise ValueError('Public channel names required')
        if type(data.get('clear_proxy', False)) is not bool: raise ValueError('Invalid proxy switch')
        if not isinstance(result['proxy'], str): raise ValueError('Invalid proxy')
        if preserve and not result['proxy']: result['proxy'] = self.settings['proxy']
        if data.get('clear_proxy'): result['proxy'] = ''
        proxy_options(result['proxy'])
        result.pop('clear_proxy', None)
        if result['enabled'] and (not result['channels'] or result['mode'] == 'account' and (not result['api_id'] or not result['api_hash'])):
            raise ValueError('Telegram configuration required')
        return result

    def snapshot(self, page=1, state_filter='ALL', search=''):
        states = ('PENDING','QUEUED','RUNNING','DONE','PARTIAL','FAILED','INTERRUPTED')
        if (type(page) is not int or not 1 <= page <= 100000 or state_filter not in ('ALL', *states)
                or not isinstance(search,str) or len(search)>100):
            raise ValueError('Invalid Telegram history filter')
        with self._guard:
            settings = {k: self.settings[k] for k in ('enabled', 'mode', 'api_id', 'channels', 'interval', 'history_limit', 'auto_upgrade')}
            settings.update(api_hash_set=bool(self.settings['api_hash']), proxy_set=bool(self.settings['proxy']))
            state = {'settings': settings, 'state': self._state, 'error': self._error,
                     'authorized': self._authorized, 'last_poll': self._last_poll,
                     'network': dict(self._network),
                     'channels': [dict(c) for c in self._channels]}
        with self.service._lock:
            tasks = {task['id']: task for task in self.service.storage.import_queue()['tasks']}
            where = " WHERE (?='ALL' OR t.state=?) AND (?='' OR instr(lower(t.title||' '||t.channel||' '||t.message_id||' '||t.media_names),lower(?))>0 OR EXISTS (SELECT 1 FROM resource_storage s JOIN strm_files f ON f.file_id=s.media_id WHERE s.share_code=t.share_code AND instr(lower(f.relative_path),lower(?))>0))"
            args = (state_filter,state_filter,search,search,search)
            state['total'] = self.service.query('SELECT count(*) n FROM telegram_imports t'+where,args,one=True)['n']
            page = min(page,max(1,(state['total']+49)//50))
            state.update(page=page,page_size=50)
            state['counts'] = {key:0 for key in states}
            state['counts'].update({r['state']:r['n'] for r in self.service.query('SELECT state,count(*) n FROM telegram_imports GROUP BY state')})
            fields = 'id,channel,message_id,state,done,failed,error,task_id,upgraded,skipped,title,media_names,total,created_at,started_at,finished_at,updated_at,attempts,share_code,password'
            state['items'] = self.service.query('SELECT '+','.join('t.'+field for field in fields.split(','))+' FROM telegram_imports t'+where+' ORDER BY id DESC LIMIT 50 OFFSET ?',(*args,(page-1)*50))
            for item in state['items']:
                task = tasks.get(item.pop('task_id'))
                if task and item['state'] in ('QUEUED', 'RUNNING'):
                    item.update({k: task[k] for k in ('state', 'done', 'failed', 'error', 'upgraded', 'skipped','total','media_names','started_at','phase','scanned','found') if k in task})
                secrets = (item.pop('share_code'), item.pop('password'))
                names = item['media_names']
                if isinstance(names,str):
                    try: names=json.loads(names)
                    except (ValueError,TypeError): names=[]
                if not isinstance(names,list): names=[]
                if not names and secrets[0]:
                    names = [r['relative_path'] for r in self.service.query('SELECT f.relative_path FROM resource_storage s JOIN strm_files f ON f.file_id=s.media_id WHERE s.share_code=? ORDER BY f.relative_path LIMIT 10',(secrets[0],))]
                item['media_names'] = [label for name in names[:10] if isinstance(name,str) and (label:=display_name(name,secrets))]
                item['title'] = display_name(item['title'],secrets)
                channel = item['channel']
                username = channel[1:] if re.fullmatch(r'@[a-z][a-z0-9_]{3,31}',channel) else None
                private = channel[4:] if re.fullmatch(r'-100[1-9][0-9]{0,15}',channel) else None
                item['message_url'] = ('https://t.me/'+username+'/' if username else 'https://t.me/c/'+private+'/' if private else '') + str(item['message_id']) if username or private else None
                item['retryable'] = item['state'] in ('FAILED','PARTIAL','INTERRUPTED') and bool(secrets[0]) and item['error']!='EXTRACTION_AMBIGUOUS'
        state['pending'] = self.service.query("SELECT count(*) n FROM telegram_imports WHERE state IN ('PENDING','QUEUED','RUNNING')", one=True)['n']
        state['retryable'] = self.service.query('SELECT count(*) n FROM telegram_imports WHERE ' + RETRYABLE, one=True)['n']
        return state

    def _network_unknown(self):
        return {'state': 'UNKNOWN', 'mode': self.settings['mode'],
                'via_proxy': bool(self.settings['proxy']), 'checked_at': None,
                'latency_ms': None, 'error': None}

    def check_network(self):
        # Schedule immediately; never hold the native API/lifecycle lock while
        # waiting for a channel poll or a slow proxy. Use saved settings only.
        self._ensure_thread()
        with self._guard:
            if self._network['state'] == 'CHECKING': return {'state': 'NETWORK_CHECK_SCHEDULED'}
            now = time.monotonic()
            if self._network_requested_at is not None and now - self._network_requested_at < NETWORK_COOLDOWN:
                return {'state': 'NETWORK_CHECK_COOLDOWN'}
            if time.time() < self._rate_until or self._network['state'] == 'RATE_LIMITED' and time.time() < self._network_next:
                return {'state': 'RATE_LIMITED'}
            self._network_requested_at = now
            self._network = {**self._network_unknown(), 'state': 'CHECKING'}
            self._network_future = asyncio.run_coroutine_threadsafe(self._run_network_check(), self._loop)
        return {'state': 'NETWORK_CHECK_SCHEDULED'}

    async def _run_network_check(self):
        # Account checks share the live transport, serialized with login/polls.
        async with self._op:
            started = time.monotonic()
            state, error, delay = 'AVAILABLE', None, NETWORK_INTERVAL
            try:
                if self.settings['mode'] == 'account' and (not self.settings['api_id'] or not self.settings['api_hash']):
                    state, error = 'SETTINGS_REQUIRED', 'API_REQUIRED'
                else:
                    await asyncio.wait_for(self._probe_network(), NETWORK_TIMEOUT)
            except asyncio.CancelledError: raise
            except Exception as exc:
                from .telegram_web import PublicChannelError
                state = 'UNAVAILABLE'
                if isinstance(exc, (TimeoutError, asyncio.TimeoutError)): error = 'NETWORK_TIMEOUT'
                elif isinstance(exc, ModuleNotFoundError): error = 'DEPENDENCY_MISSING'
                elif isinstance(exc, PublicChannelError):
                    error = exc.code
                    if exc.code == 'RATE_LIMITED':
                        state, delay = 'RATE_LIMITED', max(NETWORK_INTERVAL, exc.retry_after)
                elif type(exc).__name__ == 'FloodWaitError':
                    state, error, delay = 'RATE_LIMITED', 'RATE_LIMITED', max(NETWORK_INTERVAL, getattr(exc, 'seconds', 60))
                    self._rate_until = max(self._rate_until, time.time() + delay)
                elif type(exc).__name__ in ('ApiIdInvalidError', 'ConnectionApiIdInvalidError'):
                    state, error = 'SETTINGS_REQUIRED', 'API_INVALID'
                else: error = 'NETWORK_FAILED'
            with self._guard:
                self._network = {**self._network_unknown(), 'state': state, 'error': error,
                    'checked_at': time.time(), 'latency_ms': max(0, round((time.monotonic() - started) * 1000))}
                self._network_next = time.time() + delay

    async def _probe_network(self):
        if self.settings['mode'] == 'account':
            from telethon.tl.functions.help import GetConfigRequest
            client = await self._connect()
            # Read-only RPC works before account login; no code requests,
            # messages, subscriptions or session-file writes.
            await client(GetConfigRequest())
        else:
            from .telegram_web import PublicChannelReader
            reader = PublicChannelReader(self.settings['proxy'])
            try: await reader.page('@telegram')
            finally: await reader.close()

    def _ensure_thread(self):
        with self._guard:
            if self._closing: raise ToolError('Telegram stopped')
            if not self._thread:
                self._thread = threading.Thread(target=self._thread_main, name='p115tool-telegram', daemon=True)
                self._thread.start()
        if not self._ready.wait(5): raise ToolError('Telegram unavailable')

    def _thread_main(self):
        loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
        self._loop = loop; self._op = asyncio.Lock()
        self._poll_task = loop.create_task(self._poll_loop())
        self._ready.set()
        try: loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending: task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def _submit(self, operation, *args):
        self._ensure_thread()
        async def invoke():
            async with self._op:
                try: return await asyncio.wait_for(operation(*args), 25)
                except asyncio.CancelledError: raise
                except Exception as exc:
                    self._failure(exc)
                    return {'state': self._state, 'error': self._error}
        future = asyncio.run_coroutine_threadsafe(invoke(), self._loop)
        try: return future.result(30)
        except concurrent.futures.TimeoutError:
            future.cancel(); raise ToolError('Telegram unavailable') from None

    def configure(self, data):
        # Validate before scheduling so the native API returns HTTP 422.
        candidate = self._validate(data)
        return self._submit(self._configure, candidate)

    async def _configure(self, candidate):
        changed = any(candidate[k] != self.settings[k] for k in ('mode', 'api_id', 'api_hash', 'proxy'))
        private_write(self.settings_path, json.dumps(candidate))
        if changed: await self._disconnect()
        with self._guard:
            self.settings = candidate; self._error = None; self._next_poll = 0
            self._state = 'CONNECTING' if candidate['enabled'] else 'DISABLED'
            self._channels = []
            if changed:
                if self._network_future: self._network_future.cancel()
                self._network = self._network_unknown()
                self._network_next = 0; self._network_requested_at = None
        if not candidate['enabled']: await self._disconnect()
        return {'state': 'SAVED'}

    async def _connect(self):
        if self.settings['mode'] != 'account': raise ValueError('Account mode required')
        if not self.settings['api_id'] or not self.settings['api_hash']:
            raise ValueError('Telegram configuration required')
        if self._client is None:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            session = self.session_path.read_text().strip() if self.session_path.exists() else ''
            transport_log = logging.Logger('p115tool.telegram.transport', level=logging.CRITICAL)
            transport_log.addHandler(logging.NullHandler()); transport_log.propagate = False
            self._client = TelegramClient(StringSession(session), self.settings['api_id'], self.settings['api_hash'],
                proxy=proxy_options(self.settings['proxy']), base_logger=transport_log,
                request_retries=0, connection_retries=1, flood_sleep_threshold=0,
                timeout=10, receive_updates=False)
        if not self._client.is_connected(): await self._client.connect()
        return self._client

    async def _disconnect(self):
        client, self._client = self._client, None
        web, self._web = self._web, None
        self._authorized = False; self._phone = ''; self._phone_hash = ''; self._login_until = 0
        if client:
            try: await asyncio.wait_for(client.disconnect(), 10)
            except Exception: pass
        if web:
            try: await asyncio.wait_for(web.close(), 10)
            except Exception: pass

    def send_code(self, phone):
        if self.settings['mode'] != 'account': raise ValueError('Account mode required')
        if not isinstance(phone, str) or not re.fullmatch(r'\+[1-9][0-9]{5,14}', phone):
            raise ValueError('Invalid Telegram phone')
        return self._submit(self._send_code, phone)

    async def _send_code(self, phone):
        if time.time() < self._rate_until: return {'state': 'RATE_LIMITED'}
        if time.time() < getattr(self, '_code_after', 0): return {'state': 'CODE_COOLDOWN'}
        self._code_after = time.time() + 60
        client = await self._connect()
        if await client.is_user_authorized():
            self._authorized = True
            return {'state': 'ALREADY_AUTHORIZED'}
        result = await client.send_code_request(phone)
        self._phone = phone; self._phone_hash = result.phone_code_hash; self._login_until = time.time() + 600
        self._state = 'CODE_SENT'; self._error = None
        return {'state': 'CODE_SENT'}

    def sign_in(self, code, password):
        if self.settings['mode'] != 'account': raise ValueError('Account mode required')
        if not isinstance(code, str) or (code and not re.fullmatch(r'[0-9]{3,8}', code)):
            raise ValueError('Invalid Telegram code')
        if not isinstance(password, str) or len(password) > 1024 or not (code or password):
            raise ValueError('Invalid Telegram password')
        return self._submit(self._sign_in, code, password)

    async def _sign_in(self, code, password):
        if time.time() < self._rate_until: return {'state': 'RATE_LIMITED'}
        if not self._phone_hash or time.time() > self._login_until:
            self._state = 'LOGIN_REQUIRED'; return {'state': 'LOGIN_REQUIRED'}
        client = await self._connect()
        if password: user = await client.sign_in(password=password)
        else: user = await client.sign_in(phone=self._phone, code=code, phone_code_hash=self._phone_hash)
        if getattr(user, 'bot', False):
            await self._disconnect(); self._state = 'USER_ACCOUNT_REQUIRED'
            return {'state': self._state}
        private_write(self.session_path, client.session.save())
        self._phone = ''; self._phone_hash = ''; self._login_until = 0
        self._authorized = True; self._error = None
        self._state = 'READY' if self.settings['enabled'] else 'DISABLED'
        self._next_poll = 0
        return {'state': 'AUTHORIZED'}

    def _failure(self, exc):
        # Never return exception text, URLs, phone numbers or Telegram payloads.
        name = type(exc).__name__
        codes = {'SessionPasswordNeededError': 'PASSWORD_REQUIRED', 'PhoneCodeInvalidError': 'CODE_INVALID',
            'PhoneCodeExpiredError': 'CODE_EXPIRED', 'PasswordHashInvalidError': 'PASSWORD_INVALID',
            'PhoneNumberInvalidError': 'PHONE_INVALID', 'ApiIdInvalidError': 'API_INVALID',
            'AuthKeyUnregisteredError': 'LOGIN_REQUIRED', 'SessionRevokedError': 'LOGIN_REQUIRED',
            'UserDeactivatedError': 'LOGIN_REQUIRED', 'ModuleNotFoundError': 'DEPENDENCY_MISSING',
            'FloodWaitError': 'RATE_LIMITED', 'PhoneNumberFloodError': 'RATE_LIMITED',
            'AuthRestartError': 'LOGIN_REQUIRED'}
        from .telegram_web import PublicChannelError
        with self._guard:
            self._error = exc.code if isinstance(exc, PublicChannelError) else codes.get(name, 'TELEGRAM_UNAVAILABLE')
            self._state = self._error
            if self._error == 'LOGIN_REQUIRED': self._authorized = False
            delay = exc.retry_after if isinstance(exc, PublicChannelError) else max(60, getattr(exc, 'seconds', 60)) if name == 'FloodWaitError' else 60
            if self._error == 'RATE_LIMITED': self._rate_until = time.time() + delay
            self._next_poll = time.time() + delay

    async def _poll_loop(self):
        while True:
            try:
                if self.settings['enabled'] and time.time() >= self._network_next:
                    self.check_network()
                if self.settings['enabled'] and time.time() >= max(self._next_poll, self._rate_until):
                    async with self._op:
                        # Bound a tick without losing committed message cursors.
                        await asyncio.wait_for(self._poll(), 120)
            except asyncio.CancelledError: raise
            except Exception as exc: self._failure(exc)
            await asyncio.sleep(1)

    def poll_now(self):
        self._ensure_thread()
        def wake():
            # Respect upstream FloodWait and avoid concurrent repeated clicks.
            if self._error != 'RATE_LIMITED': self._next_poll = 0
        self._loop.call_soon_threadsafe(wake)
        return {'state': 'POLL_SCHEDULED'}

    async def _poll(self):
        self._next_poll = time.time() + self.settings['interval']
        if self.settings['mode'] == 'web':
            return await self._poll_public()
        if self._phone_hash and time.time() < self._login_until: return
        self._sync_tasks()
        self._drain()
        client = await self._connect()
        if not await client.is_user_authorized():
            self._authorized = False; self._state = 'LOGIN_REQUIRED'; return
        user = await client.get_me()
        if getattr(user, 'bot', False):
            self._state = 'USER_ACCOUNT_REQUIRED'; return
        self._authorized = True
        from telethon.utils import get_peer_id
        entities = {}
        async for dialog in client.iter_dialogs(limit=2000):
            entity = dialog.entity
            if not getattr(entity, 'broadcast', False) or getattr(entity, 'left', False): continue
            entities[str(get_peer_id(entity))] = entity
            username = getattr(entity, 'username', None)
            if username: entities['@' + username.lower()] = entity
        statuses = []
        for reference in self.settings['channels']:
            if self._closing or self.service._stop.is_set(): break
            entity = entities.get(reference)
            if entity is None:
                statuses.append({'channel': reference, 'state': 'CHANNEL_NOT_JOINED'}); continue
            channel = str(get_peer_id(entity))
            try:
                await self._read_channel(client, entity, channel)
                statuses.append({'channel': reference, 'state': 'WATCHING'})
            except Exception as exc:
                if type(exc).__name__ == 'FloodWaitError': raise
                statuses.append({'channel': reference, 'state': 'CHANNEL_UNAVAILABLE'})
        self._sync_tasks(); self._drain()
        with self._guard:
            self._channels = statuses; self._last_poll = time.time(); self._error = None
            self._state = 'WATCHING' if statuses and all(c['state'] == 'WATCHING' for c in statuses) else 'CHANNEL_ATTENTION'

    async def _poll_public(self):
        from .telegram_web import PublicChannelReader, PublicChannelError
        self._authorized = False
        self._sync_tasks(); self._drain()
        if self._web is None: self._web = PublicChannelReader(self.settings['proxy'])
        statuses = []
        for reference in self.settings['channels']:
            if self._closing or self.service._stop.is_set(): break
            try:
                await self._read_public_channel(reference)
                statuses.append({'channel': reference, 'state': 'WATCHING'})
            except PublicChannelError as exc:
                if exc.code == 'RATE_LIMITED': raise
                statuses.append({'channel': reference, 'state': exc.code})
        self._sync_tasks(); self._drain()
        with self._guard:
            self._channels = statuses; self._last_poll = time.time(); self._error = None
            self._state = 'WATCHING' if statuses and all(c['state'] == 'WATCHING' for c in statuses) else 'CHANNEL_ATTENTION'

    async def _read_public_channel(self, reference):
        from .telegram_web import PublicChannelError
        key = 'web:' + reference
        checkpoint = self.service.query('SELECT message_id FROM telegram_cursors WHERE channel=?', (key,), one=True)
        if checkpoint is None:
            page = await self._web.page(reference)
            messages = page.messages
            if not messages:
                self._cursor(key, 0); return
            if not self.settings['history_limit']:
                self._cursor(key, messages[-1].id); return
            found = {m.id: m for m in messages}
            for _ in range(29):
                if len(found) >= self.settings['history_limit'] or not page.more_before: break
                boundary = min(found)
                page = await self._web.page(reference, before=boundary)
                older = [m for m in page.messages if m.id < boundary]
                if not older:
                    if page.more_before: raise PublicChannelError('WEB_PAGE_INVALID')
                    break
                found.update((m.id, m) for m in older)
            if len(found) < self.settings['history_limit'] and page.more_before:
                raise PublicChannelError('WEB_HISTORY_LIMIT')
            messages = [found[i] for i in sorted(found)][-self.settings['history_limit']:]
            self._cursor(key, messages[0].id-1)
        else:
            # The public preview supports ascending pages after a message ID.
            # One page per tick naturally bounds catch-up without skipping gaps.
            page = await self._web.page(reference, after=checkpoint['message_id'])
            messages = [m for m in page.messages if m.id > checkpoint['message_id']]
        for message in messages:
            if self._closing or self.service._stop.is_set(): return
            if not self.ingest(reference, message): return
            self._cursor(key, message.id)

    async def _read_channel(self, client, entity, channel):
        checkpoint = self.service.query('SELECT message_id FROM telegram_cursors WHERE channel=?', (channel,), one=True)
        if checkpoint is None:
            messages = await client.get_messages(entity, limit=max(1, self.settings['history_limit']))
            if not self.settings['history_limit']:
                self._cursor(channel, messages[0].id if messages else 0); return
            messages = sorted(messages, key=lambda m: m.id)
            # Persist the initial boundary before ingesting: backpressure must
            # never reselect a newer history window and skip unread messages.
            self._cursor(channel, messages[0].id - 1 if messages else 0)
        else:
            messages = [m async for m in client.iter_messages(entity, min_id=checkpoint['message_id'], reverse=True, limit=100)]
        for message in messages:
            if self._closing or self.service._stop.is_set(): return
            if not self.ingest(channel, message): return
            self._cursor(channel, message.id)

    def _cursor(self, channel, message_id):
        self.service.query('INSERT INTO telegram_cursors VALUES(?,?) ON CONFLICT(channel) DO UPDATE SET message_id=max(message_id,excluded.message_id)', (channel, message_id))

    def ingest(self, channel, message):
        shares, ambiguous = extract_shares(message)
        title = message_title(message, [part for share in shares for part in share] + PASSWORD.findall((getattr(message,'message','') or '')[:65536]))
        for code, password in shares:
            fingerprint = hashlib.sha256((code + ':' + password).encode()).hexdigest()
            if self.service.query('SELECT id FROM telegram_imports WHERE fingerprint=?', (fingerprint,), one=True): continue
            pending = self.service.query("SELECT count(*) n FROM telegram_imports WHERE state IN ('PENDING','QUEUED','RUNNING')", one=True)['n']
            if pending >= BACKLOG_LIMIT: return False
            self.service.query('INSERT OR IGNORE INTO telegram_imports(fingerprint,channel,message_id,share_code,password,state,created_at,title,updated_at) VALUES(?,?,?,?,?,\'PENDING\',?,?,?)',
                               (fingerprint, channel, message.id, code, password, time.time(),title,time.time()))
        if ambiguous:
            # One diagnostic per message, with no raw message or guessed password.
            fingerprint = hashlib.sha256(('ambiguous:' + channel + ':' + str(message.id)).encode()).hexdigest()
            self.service.query('INSERT OR IGNORE INTO telegram_imports(fingerprint,channel,message_id,share_code,password,state,error,created_at,title,updated_at,finished_at) VALUES(?,?,?,\'\',\'\',\'FAILED\',\'EXTRACTION_AMBIGUOUS\',?,?,?,?)',
                               (fingerprint, channel, message.id, time.time(),title,time.time(),time.time()))
        return True

    def _sync_tasks(self):
        # Keep the snapshot and updates under the same lock as worker progress;
        # stale QUEUED state must never overwrite a just-completed result.
        with self.service._lock:
            tasks = {task['id']: task for task in self.service.storage.import_queue()['tasks']}
            for row in self.service.query("SELECT id,task_id FROM telegram_imports WHERE state IN ('QUEUED','RUNNING')"):
                task = tasks.get(row['task_id'])
                if task:
                    self._record_task(task, 'id', row['id'])
                else:
                    self.service.query("UPDATE telegram_imports SET state='INTERRUPTED',error='TASK_UNAVAILABLE',updated_at=?,finished_at=? WHERE id=?", (time.time(),time.time(),row['id']))

    def _record_task(self, value, key, identity):
        now = time.time()
        terminal = value['state'] in ('DONE','PARTIAL','FAILED','INTERRUPTED')
        names = value.get('media_names')
        names = json.dumps(names[:10],ensure_ascii=False) if isinstance(names,list) else None
        self.service.query('UPDATE telegram_imports SET state=?,done=?,failed=?,error=?,upgraded=?,skipped=?,total=coalesce(?,total),media_names=coalesce(?,media_names),started_at=coalesce(started_at,?),updated_at=?,finished_at=? WHERE '+key+'=?',
            (value['state'],value.get('done',0),value.get('failed',0),value.get('error'),value.get('upgraded',0),value.get('skipped',0),value.get('total'),names,value.get('started_at'),now,now if terminal else None,identity))

    def import_progress(self, value):
        # Called under the service lock. Persist final results before the
        # storage queue trims its 50-item in-memory history.
        # Running progress stays in memory, as it does for ordinary imports.
        # Persist the start transition and terminal results, not each file.
        starting = value.get('phase')=='SCANNING' and 'scanned' not in value and value['state']=='RUNNING'
        if value.get('task_id') and (starting or value['state'] in ('DONE', 'PARTIAL', 'FAILED', 'INTERRUPTED')):
            self._record_task(value, 'task_id', value['task_id'])

    def _drain(self):
        for row in self.service.query("SELECT id,share_code,password FROM telegram_imports WHERE state='PENDING' ORDER BY id LIMIT 100"):
            if self._closing or self.service._stop.is_set(): return
            with self.service._lock:
                result = self.service.storage.start('import', {'link': 'https://115.com/s/' + row['share_code'], 'password': row['password'], 'auto_upgrade':self.settings['auto_upgrade']})
                state = result['state']
                if state == 'QUEUE_FULL': return
                if state in ('RUNNING', 'QUEUED', 'ALREADY_QUEUED'):
                    self.service.query('UPDATE telegram_imports SET state=?,task_id=?,error=NULL,updated_at=?,attempts=attempts+1 WHERE id=?',
                        ('RUNNING' if state == 'RUNNING' else 'QUEUED', result['task_id'], time.time(),row['id']))
                else:
                    self.service.query("UPDATE telegram_imports SET state='FAILED',error='START_FAILED',attempts=attempts+1,updated_at=?,finished_at=? WHERE id=?", (time.time(),time.time(),row['id']))

    def retry(self, entry_id):
        if type(entry_id) is not int or entry_id <= 0: raise ValueError('Invalid Telegram import')
        return self._submit(self._retry, entry_id)

    async def _retry(self, entry_id):
        return self._queue_retries(entry_id)

    def retry_all(self):
        return self._submit(self._retry_all)

    async def _retry_all(self):
        return self._queue_retries()

    def _queue_retries(self, entry_id=None):
        # One bounded update under the same lock as worker progress. Repeated
        # clicks only select terminal records still eligible for a new attempt.
        condition = RETRYABLE + (' AND id=?' if entry_id is not None else '')
        args = (entry_id,) if entry_id is not None else ()
        with self.service._lock:
            eligible = self.service.query('SELECT count(*) n FROM telegram_imports WHERE ' + condition, args, one=True)['n']
            pending = self.service.query("SELECT count(*) n FROM telegram_imports WHERE state IN ('PENDING','QUEUED','RUNNING')", one=True)['n']
            queued = min(eligible, max(0, BACKLOG_LIMIT - pending))
            if queued:
                self.service.query("UPDATE telegram_imports SET state='PENDING',task_id=NULL,error=NULL,done=0,failed=0,upgraded=0,skipped=0,total=NULL,started_at=NULL,finished_at=NULL,updated_at=? "
                    'WHERE id IN (SELECT id FROM telegram_imports WHERE ' + condition + ' ORDER BY id LIMIT ?)', (time.time(),*args, queued))
                self._next_poll = 0
        state = ('RETRY_QUEUED' if entry_id is not None else 'RETRY_BATCH_QUEUED') if queued else 'RETRY_QUEUE_FULL' if eligible else 'RETRY_UNAVAILABLE'
        return {'state': state, 'queued': queued, 'remaining': eligible - queued, 'subscription_enabled': self.settings['enabled']}

    def close(self):
        with self._guard:
            if self._closing: return
            self._closing = True
        if not self._thread: return
        async def shutdown():
            try:
                current = asyncio.current_task()
                tasks = [task for task in asyncio.all_tasks() if task is not current]
                for task in tasks: task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await self._disconnect()
            finally:
                # Schedule stop from the owning loop while it is awake, after
                # the concurrent future receives its completion notification.
                self._loop.call_later(0.01, self._loop.stop)
        future = asyncio.run_coroutine_threadsafe(shutdown(), self._loop)
        try: future.result(20)
        except Exception: pass
        self._thread.join()
