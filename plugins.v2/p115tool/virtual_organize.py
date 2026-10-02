"""Plan virtual media paths using MoviePilot recognition and naming engines.

No storage operator or transfer queue is involved: the shared video stays in
the share and only a local STRM is written by Service.
"""
from copy import deepcopy
from pathlib import Path
from .models import SafetyError
from .strm import safe_parts


def organize_virtual(relative):
    safe_parts(relative)
    # Pass only the share-relative hierarchy, never a URL or extraction code.
    source = '/' + relative
    try:
        from app.chain.media import MediaChain
        from app.core.config import settings
        from app.modules.filemanager.transhandler import TransHandler
        from app.schemas import FileItem
        context = MediaChain().recognize_by_path(path=source, obtain_images=False)
        media = getattr(context, 'media_info', None)
        meta = deepcopy(getattr(context, 'meta_info', None))
        kind = getattr(getattr(media, 'type', None), 'name', None)
        if not media or meta is None or kind not in ('MOVIE', 'TV'):
            raise SafetyError('Virtual media recognition failed')
        category = str(getattr(media, 'category', None) or '未分类')
        if len(safe_parts(category)) != 1:
            raise SafetyError('Virtual category invalid')
        template = settings.RENAME_FORMAT(media.type)
        if not isinstance(template, str) or not template.strip():
            raise SafetyError('Virtual naming template unavailable')
        suffix = Path(relative).suffix
        item = FileItem(storage='p115tool_share', path=source, name=Path(relative).name,
                        basename=Path(relative).stem, type='file', extension=suffix.lstrip('.'))
        root = Path.cwd() / '.p115tool-virtual'
        base = root / ('电影' if kind == 'MOVIE' else '电视剧') / category
        season_iso = False
        if kind == 'TV':
            season = getattr(meta, 'begin_season', None)
            episode = getattr(meta, 'begin_episode', None)
            if type(season) is not int or not 0 <= season <= 999:
                raise SafetyError('Virtual season unavailable')
            season_iso = episode is None and suffix.lower() == '.iso'
            if not season_iso and (type(episode) is not int or not 0 <= episode <= 9999):
                raise SafetyError('Virtual episode unavailable')
            # Match the host's single-file naming normalization.
            meta.end_season = None
            if getattr(meta, 'total_season', None): meta.total_season = 1
            if getattr(meta, 'total_episode', 0) and meta.total_episode > 2:
                meta.total_episode = 1
                meta.end_episode = None
        handler = TransHandler()
        destination = handler.get_rename_path(
            path=base, template_string=template,
            rename_dict=handler.get_naming_dict(meta=meta, mediainfo=media, file_ext=suffix),
            source_path=source, source_item=item)
        destination = Path(destination)
        if season_iso:
            from app.helper.directory import DirectoryHelper
            series = DirectoryHelper.get_media_root_path(template, rename_path=destination)
            if not series or Path(series) == base:
                raise SafetyError('Virtual series template invalid')
            destination = Path(series) / f'Season {season:02d}' / item.name
        result = destination.relative_to(root).as_posix()
        safe_parts(result)
        if not destination.is_relative_to(base) or destination == base or destination.suffix.lower() != suffix.lower():
            raise SafetyError('Virtual naming path invalid')
        return result
    except Exception:
        # Host exceptions may contain raw responses; surface a fixed message.
        raise SafetyError('MoviePilot virtual organization failed') from None
