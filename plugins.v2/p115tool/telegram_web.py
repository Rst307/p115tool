"""Bounded, anonymous reads of Telegram's public channel preview pages."""
from dataclasses import dataclass
from html.parser import HTMLParser
from email.utils import parsedate_to_datetime
import re
import time
from types import SimpleNamespace

MAX_PAGE_BYTES = 2 * 1024 * 1024
VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}


class PublicChannelError(Exception):
    def __init__(self, code, retry_after=60):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


@dataclass
class PublicPage:
    messages: list
    more_before: bool = False


class _PreviewParser(HTMLParser):
    def __init__(self, username):
        super().__init__(convert_charrefs=True)
        self.username = username.lower()
        self.stack = []; self.current = None; self.messages = {}
        self.text_depth = 0; self.ignored = 0; self.history = False; self.identity = False
        self.history_closed = False; self.identity_closed = False
        self.more_before = False

    def append(self, value):
        if self.current is None or not self.text_depth or self.ignored: return
        self.current['length'] += len(value)
        if self.current['length'] > 65536: raise PublicChannelError('WEB_PAGE_INVALID')
        self.current['text'].append(value)
        self.current['utf16'] += len(value.encode('utf-16-le')) // 2

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes); classes = set(attrs.get('class', '').split())
        if tag == 'main' and attrs.get('data-url', '').lower() == '/' + self.username:
            self.identity = True
        if 'tgme_channel_history' in classes: self.history = True
        if 'tme_messages_more' in classes and re.fullmatch(r'[1-9][0-9]{0,18}', attrs.get('data-before', '')):
            self.more_before = True
        entry = {'tag': tag, 'message': False, 'text': False, 'ignore': False, 'anchor': None,
                 'history': 'tgme_channel_history' in classes,
                 'identity': tag == 'main' and attrs.get('data-url', '').lower() == '/' + self.username}
        if 'tgme_widget_message' in classes:
            match = re.fullmatch(r'([A-Za-z][A-Za-z0-9_]{3,31})/([1-9][0-9]{0,18})', attrs.get('data-post', ''))
            if (self.current is not None or not match or match[1].lower() != self.username
                    or int(match[2]) >= 2**63): raise PublicChannelError('WEB_PAGE_INVALID')
            self.current = {'id': int(match[2]), 'text': [], 'entities': [], 'buttons': [], 'length': 0, 'utf16': 0}
            entry['message'] = True
        if self.current is not None:
            if tag in ('script', 'style'): entry['ignore'] = True; self.ignored += 1
            if 'tgme_widget_message_text' in classes: entry['text'] = True; self.text_depth += 1
            href = attrs.get('href', '')
            if tag == 'a' and href and not self.ignored:
                if self.text_depth:
                    entry['anchor'] = (self.current['utf16'], href)
                elif 'tgme_widget_message_inline_button' in classes:
                    self.current['buttons'].append(SimpleNamespace(url=href))
            if tag == 'br': self.append('\n')
        if tag not in VOID_TAGS: self.stack.append(entry)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS: self.handle_endtag(tag)

    def handle_data(self, data): self.append(data)

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.stack)-1, -1, -1) if self.stack[i]['tag'] == tag), None)
        if index is None: return
        while len(self.stack) > index:
            entry = self.stack.pop()
            if entry['history'] and entry['tag'] == tag: self.history_closed = True
            if entry['identity'] and entry['tag'] == tag: self.identity_closed = True
            if self.current is not None and entry['anchor']:
                offset, href = entry['anchor']
                self.current['entities'].append(SimpleNamespace(offset=offset,
                    length=self.current['utf16']-offset, url=href))
            if entry['text']: self.text_depth -= 1
            if entry['ignore']: self.ignored -= 1
            if entry['message'] and self.current is not None:
                item = self.current
                message = SimpleNamespace(id=item['id'], message=''.join(item['text']), entities=item['entities'],
                    reply_markup=SimpleNamespace(rows=[SimpleNamespace(buttons=item['buttons'])]))
                if item['id'] in self.messages: raise PublicChannelError('WEB_PAGE_INVALID')
                self.messages[item['id']] = message; self.current = None


def parse_preview(html, username):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', username): raise ValueError('Invalid public channel')
    parser = _PreviewParser(username)
    try:
        parser.feed(html); parser.close()
    except PublicChannelError: raise
    except Exception: raise PublicChannelError('WEB_PAGE_INVALID') from None
    if (parser.current is not None or not parser.identity or not parser.history
            or not parser.history_closed or not parser.identity_closed):
        raise PublicChannelError('WEB_CHANNEL_UNAVAILABLE')
    return PublicPage([parser.messages[key] for key in sorted(parser.messages)], parser.more_before)


class PublicChannelReader:
    def __init__(self, proxy=''):
        self.proxy = proxy; self.client = None

    async def page(self, reference, *, before=None, after=None):
        if not re.fullmatch(r'@[a-z][a-z0-9_]{3,31}', reference): raise ValueError('Invalid public channel')
        if before is not None and after is not None: raise ValueError('Conflicting page boundaries')
        for value in (before, after):
            if value is not None and (type(value) is not int or not 0 <= value < 2**63): raise ValueError('Invalid page boundary')
        import httpx
        if self.client is None:
            self.client = httpx.AsyncClient(proxy=self.proxy or None, follow_redirects=False,
                timeout=httpx.Timeout(15, connect=10), headers={'User-Agent': 'p115tool-public-channel/0.2.42'},
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=1))
        params = {'before': before} if before is not None else {'after': after} if after is not None else {}
        try:
            # Construct only this fixed Telegram origin; never follow page links,
            # redirects, media URLs or a channel-supplied next-page address.
            async with self.client.stream('GET', 'https://t.me/s/' + reference[1:], params=params) as response:
                if response.status_code == 429:
                    raw = response.headers.get('retry-after', '')
                    if re.fullmatch(r'\d{1,12}', raw): delay = max(60, int(raw))
                    else:
                        try: delay = max(60, int(parsedate_to_datetime(raw).timestamp()-time.time())+1)
                        except (ValueError, TypeError, OverflowError): delay = 300
                    raise PublicChannelError('RATE_LIMITED', delay)
                if response.status_code != 200: raise PublicChannelError('WEB_CHANNEL_UNAVAILABLE')
                if 'text/html' not in response.headers.get('content-type', '').lower():
                    raise PublicChannelError('WEB_PAGE_INVALID')
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_PAGE_BYTES: raise PublicChannelError('WEB_PAGE_TOO_LARGE')
            return parse_preview(content.decode('utf-8', errors='strict'), reference[1:])
        except PublicChannelError: raise
        except UnicodeError: raise PublicChannelError('WEB_PAGE_INVALID') from None
        except Exception: raise PublicChannelError('WEB_REQUEST_FAILED') from None

    async def close(self):
        client, self.client = self.client, None
        if client: await client.aclose()
