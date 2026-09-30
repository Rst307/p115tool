"""Small string helpers shared with MoviePilot classification integration."""
import re
from .models import SafetyError


def value(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def portable_title(raw):
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 200:
        raise SafetyError('Recognition title is missing or too long')
    title = re.sub(r'[\\/<>:"|?*]', '_', raw).strip().rstrip('. ')
    if not title or title in ('.', '..') or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in title):
        raise SafetyError('Recognition title cannot form a portable filename')
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\\.|$)', title, re.I):
        title = '_' + title
    return title
