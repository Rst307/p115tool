import asyncio
from functools import partial
import re

from fastapi import Request, HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from .models import MissingFile, ToolError
try:
    from app.log import logger
except ModuleNotFoundError:
    import logging
    logger=logging.getLogger(__name__)

# Exact local messages mapped to fixed codes; never emit arbitrary exception text.
SAFETY_REASONS={
    'Playback mapping unavailable':'PLAYBACK_MAPPING_UNAVAILABLE',
    'Service unavailable':'PLAYBACK_SERVICE_UNAVAILABLE',
    'Service stopped':'PLAYBACK_SERVICE_STOPPED',
    'MoviePilot 115 authorization unavailable':'MOVIEPILOT_AUTH_UNAVAILABLE',
    'This operation requires 115 Cookie mode':'COOKIE_MODE_REQUIRED',
    'Temporary copy must be transferred in 115 Cookie mode':'COOKIE_TRANSFER_REQUIRED',
    '115 file identity mismatch':'FILE_IDENTITY_MISMATCH',
    'Temporary identity changed':'TEMP_FILE_IDENTITY_CHANGED',
    'Temporary directory identity changed':'TEMP_FOLDER_IDENTITY_CHANGED',
    'Temporary contents changed':'TEMP_CONTENTS_CHANGED',
    'Temporary contents not uniquely verified':'TEMP_CONTENTS_NOT_UNIQUE',
    'Temporary write outcome unresolved':'TEMP_WRITE_UNRESOLVED',
    'Temporary recovery cooling down':'TEMP_RECOVERY_COOLDOWN',
    'Temporary transfer outcome unknown':'TEMP_TRANSFER_UNKNOWN',
    'Temporary transfer awaiting visibility':'TEMP_TRANSFER_PENDING',
    'Temporary playback code unavailable':'TEMP_PICKCODE_MISSING',
    'Share contains no media':'SHARE_MEDIA_NOT_AVAILABLE',
    'Share identity is ambiguous':'SHARE_IDENTITY_NOT_UNIQUE',
    'Share playback validation failed':'SHARE_PLAYBACK_NOT_AVAILABLE',
    'Share mapping changed':'SHARE_MAPPING_CHANGED',
    'Recycle file not uniquely verified':'RECYCLE_IDENTITY_NOT_UNIQUE',
    'Recycle purge outcome unconfirmed':'RECYCLE_PURGE_UNCONFIRMED',
    'Unsupported recycle listing':'RECYCLE_LIST_UNSUPPORTED',
    'Unsupported recycle entry':'RECYCLE_ENTRY_UNSUPPORTED',
    'Invalid recycle entry identity':'RECYCLE_ENTRY_INVALID',
    'Incomplete recycle listing':'RECYCLE_LIST_INCOMPLETE',
    'Temporary directory not empty':'TEMP_FOLDER_NOT_EMPTY',
    'Temporary directory not configured':'TEMP_ROOT_NOT_CONFIGURED',
    'Invalid temporary root':'TEMP_ROOT_INVALID',
    'Share mapping unavailable':'SHARE_MAPPING_UNAVAILABLE',
    'Owned share file changed':'OWNED_SHARE_CHANGED',
    'Playback link unavailable for existing copy':'EXISTING_COPY_LINK_UNAVAILABLE',
    '115 directory identity mismatch':'DIRECTORY_IDENTITY_MISMATCH',
    '115 returned another directory; scan aborted':'DIRECTORY_FALLBACK',
    'Invalid directory ancestors':'DIRECTORY_ANCESTORS_INVALID',
    'Invalid directory root position':'DIRECTORY_ROOT_INVALID',
    'Invalid directory ancestor name':'DIRECTORY_NAME_INVALID',
    'Directory ancestors unavailable':'DIRECTORY_ANCESTORS_UNAVAILABLE',
    'Invalid listed file name':'LISTED_FILENAME_INVALID',
    'Unsafe STRM path':'PATH_INVALID',
    '115 URL requires cookies and cannot be safely used for 302':'LINK_REQUIRES_COOKIE',
    'Unsafe download URL':'LINK_INVALID',
    'IP-address download URLs are not allowed':'LINK_IP_NOT_ALLOWED',
    'Download URL is outside configured 115 CDN domains':'LINK_DOMAIN_NOT_ALLOWED',
}

ALLOWED_OPERATIONS = frozenset({
    'fs_file', 'fs_files', 'fs_mkdir', 'share_snap', 'share_receive', 'download_url',
    'share_download_url', 'share_send', 'share_update', 'fs_delete', 'recyclebin_list', 'recyclebin_clean',
})
COPY_STATES = frozenset({
    'FOLDER_UNKNOWN', 'RECEIVE_UNKNOWN', 'DELETE_UNKNOWN', 'FOLDER_CREATING',
    'RECEIVING', 'DELETING', 'RECEIVED', 'READY', 'EMPTY',
})
SDK_ERROR_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,80}')
TOKEN_RE = re.compile(r'[A-Za-z0-9_-]{20,128}')


def log_failure(exc,context='playback'):
    code=getattr(exc,'upstream_code',None)
    code=code if type(code) is int else None
    operation=getattr(exc,'operation','unknown')
    operation=operation if isinstance(operation,str) and operation in ALLOWED_OPERATIONS else 'unknown'
    sdk_error=getattr(exc,'sdk_error','none')
    sdk_error=sdk_error if isinstance(sdk_error,str) and SDK_ERROR_RE.fullmatch(sdk_error) else 'unknown'
    message=exc.args[0] if isinstance(exc,ToolError) and exc.args else None
    reason=SAFETY_REASONS.get(message,'unknown') if type(message) is str else 'unknown'
    copy_state=getattr(exc,'copy_state',None)
    copy_state=copy_state if type(copy_state) is str and copy_state in COPY_STATES else 'none'
    # Never log the request, token, URL, or exception text/upstream payload.
    label='115回收站处理失败' if context=='recycle' else '115播放失败'
    logger.warning(f'{label}：接口={operation}，类型={type(exc).__name__}，SDK异常={sdk_error}，上游错误码={code}，原因={reason}，副本状态={copy_state}')

PLAYBACK_SLOW_SECONDS = 10


async def resolve_playback(service, token, ua):
    loop = asyncio.get_running_loop()
    started = loop.time()
    slow = loop.call_later(PLAYBACK_SLOW_SECONDS, logger.warning,
                           f'115播放仍在等待：已超过 {PLAYBACK_SLOW_SECONDS} 秒，尚未返回直链。')
    location = None
    try:
        location = await run_in_threadpool(service.play, token, ua)
        status = 302
    except MissingFile as exc:
        log_failure(exc)
        status = 404
    except Exception as exc:
        log_failure(exc)
        status = 503
    finally:
        slow.cancel()
    return status, location, (loop.time() - started) * 1000


def routes(plugin):
    pending = {}

    def forget(key, finished):
        if pending.get(key) is finished:
            pending.pop(key)

    async def play(request: Request):
        token=request.path_params.get('token','')
        if not TOKEN_RE.fullmatch(token): raise HTTPException(404,'Playback unavailable')
        ua=request.headers.get('user-agent','')
        if len(ua)>1024 or any(ord(c)<32 for c in ua): raise HTTPException(400,'Invalid request')
        # Service.play coordinates with close() without holding the plugin lifecycle lock.
        service = plugin._service
        if not service:
            log_failure(ToolError('Service unavailable'))
            raise HTTPException(503, 'Playback unavailable')
        if getattr(service, '_closed', False):
            log_failure(ToolError('Service stopped'))
            raise HTTPException(503, 'Playback unavailable')
        key = (asyncio.get_running_loop(), id(service), token, ua)
        task = pending.get(key)
        if task is None or task.done():
            task = asyncio.create_task(resolve_playback(service, token, ua))
            pending[key] = task
            task.add_done_callback(partial(forget, key))
        # A disconnected player must not cancel an in-progress copy or kick off another one.
        status, location, elapsed = await asyncio.shield(task)
        headers = {'Cache-Control': 'no-store', 'Server-Timing': f'p115-resolve;dur={elapsed:.1f}'}
        if status != 302:
            raise HTTPException(status, 'Playback unavailable', headers=headers)
        headers.update({'Location': location, 'Referrer-Policy': 'no-referrer'})
        return Response(status_code=status, headers=headers)
    return [{'path':'/play/{token}','endpoint':play,'methods':['GET','HEAD'],'summary':'115 direct playback','allow_anonymous':True}]
