"""Small display labels only; never retain Telegram message bodies or URLs."""
import re

URL = re.compile(r'(?:https?://|www\.)\S+', re.I)
SENSITIVE = re.compile(r'提取|访问码|访问密码|密码|口令|password|pwd|cookie|token|api.?hash|secret|磁力|magnet:', re.I)


def display_name(value, secrets=()):
    value = URL.sub('', str(value))
    for secret in secrets:
        if secret: value = value.replace(secret, '')
    if SENSITIVE.search(value): return ''
    value = ''.join(c for c in value if c.isprintable())
    return re.sub(r'\s+', ' ', value).strip(' :：|/-')[:240]


def message_title(message, secrets=()):
    for line in (getattr(message, 'message', '') or '')[:65536].splitlines():
        # A link/code line is not a title, even after removing its URL.
        if URL.search(line) or SENSITIVE.search(line): continue
        title = display_name(line, secrets)
        if len(title) >= 2: return title
    return ''
