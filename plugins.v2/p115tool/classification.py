"""Local STRM classification using MoviePilot's recognized category."""
import json
from .organizer import value, portable_title


def recognize(path):
    from app.chain.media import MediaChain
    context = MediaChain().recognize_by_path(path=path, obtain_images=False)
    info = value(context, 'media_info')
    kind = value(info, 'type')
    kind = getattr(kind, 'name', kind)
    kind = {'MOVIE': '电影', 'TV': '电视剧', '电影': '电影', '电视剧': '电视剧'}.get(kind, '未识别')
    category = value(info, 'category')
    return {'type': kind, 'category': portable_title(category) if category else '未分类'}


def folders(config, db, media, refresh=False):
    if not (config.strm_by_type or config.strm_by_category):
        return []
    key = f'strm_classification:{media.id}'
    cached = db.one('SELECT value FROM settings WHERE name=?', (key,))
    data = json.loads(cached['value']) if cached else {'type': '未识别', 'category': '未分类'}
    if refresh and data.get('provider') != 'MoviePilot':
        try:
            data = recognize(media.virtual_path)
        except Exception:
            # Preserve a previous known classification during host outages.
            db.log('strm_classify', 'UNAVAILABLE', media.id, 'Recognition unavailable; cached or fallback classification retained')
        if data['type'] == '未识别':
            meta = db.one('SELECT media_type FROM media_metadata WHERE media_id=?', (media.id,)) or {}
            data['type'] = {'MOVIE': '电影', 'TV': '电视剧'}.get(meta.get('media_type'), '未识别')
            db.log('strm_classify', 'UNRECOGNIZED', media.id, 'No recognized category; fallback directories used')
        db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                   (key, json.dumps(data, ensure_ascii=False)))
    return ([portable_title(data['type'])] if config.strm_by_type else []) + ([portable_title(data['category'])] if config.strm_by_category else [])
