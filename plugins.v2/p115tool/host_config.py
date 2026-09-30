"""Normalize MoviePilot form values without relaxing core/API validation."""
from dataclasses import fields
import json
import re

from .config import Config
from .transport import unique_object

JSON_FIELDS = ('source_cids', 'policies', 'allowed_cdn_suffixes',
               'media_extensions', 'emby_path_mappings', 'organize_templates')


def form_data(config):
    data = dict(config or {})
    for name in JSON_FIELDS:
        raw = data.pop(name + '_json', None)
        if raw is not None:
            if not isinstance(raw, str):
                raise ValueError('JSON form field must be text')
            data[name] = json.loads(raw, object_pairs_hook=unique_object)
    for field in fields(Config):
        value = data.get(field.name)
        # VTextField type=number can emit text. Booleans remain strict: the
        # string "false" must never turn into authorization through truthiness.
        if type(field.default) is int and isinstance(value, str):
            if len(value) > 20 or not re.fullmatch(r'-?[0-9]+', value):
                raise ValueError('Integer form field required')
            data[field.name] = int(value)
    return data
