from fastapi import Request, HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from .models import MissingFile, SafetyError, ToolError
import re
try:
    from app.log import logger
except ModuleNotFoundError:
    import logging
    logger=logging.getLogger(__name__)

# Exact local messages mapped to fixed codes; never emit arbitrary exception text.
SAFETY_REASONS={
    '115 file identity mismatch':'FILE_IDENTITY_MISMATCH',
    'Temporary identity changed':'TEMP_FILE_IDENTITY_CHANGED',
    'Temporary directory identity changed':'TEMP_FOLDER_IDENTITY_CHANGED',
    'Temporary contents changed':'TEMP_CONTENTS_CHANGED',
    'Temporary contents not uniquely verified':'TEMP_CONTENTS_NOT_UNIQUE',
    'Temporary write outcome unresolved':'TEMP_WRITE_UNRESOLVED',
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

def log_failure(exc):
    code=getattr(exc,'upstream_code',None)
    code=code if type(code) is int else None
    operation=getattr(exc,'operation','unknown')
    allowed={'fs_file','fs_files','fs_mkdir','share_snap','share_receive','download_url','share_download_url','share_send','share_update','fs_delete'}
    operation=operation if isinstance(operation,str) and operation in allowed else 'unknown'
    sdk_error=getattr(exc,'sdk_error','none')
    sdk_error=sdk_error if isinstance(sdk_error,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,80}',sdk_error) else 'unknown'
    message=exc.args[0] if isinstance(exc,SafetyError) and exc.args else None
    reason=SAFETY_REASONS.get(message,'unknown') if type(message) is str else 'unknown'
    copy_state=getattr(exc,'copy_state',None)
    copy_state=copy_state if type(copy_state) is str and copy_state in {
        'FOLDER_UNKNOWN','RECEIVE_UNKNOWN','DELETE_UNKNOWN','FOLDER_CREATING','RECEIVING','DELETING'} else 'none'
    # Never log the request, token, URL, or exception text/upstream payload.
    logger.warning(f'115播放失败：接口={operation}，类型={type(exc).__name__}，SDK异常={sdk_error}，上游错误码={code}，原因={reason}，副本状态={copy_state}')

def routes(plugin):
    async def play(request: Request):
        token=request.path_params.get('token','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{20,128}',token): raise HTTPException(404,'Playback unavailable')
        ua=request.headers.get('user-agent','')
        if len(ua)>1024 or any(ord(c)<32 for c in ua): raise HTTPException(400,'Invalid request')
        def resolve():
            with plugin._lifecycle:
                if not plugin._service: raise ToolError('Service unavailable')
                return plugin._service.play(token,ua)
        try: location=await run_in_threadpool(resolve)
        except MissingFile as exc:
            log_failure(exc)
            raise HTTPException(404,'Playback unavailable') from None
        except Exception as exc:
            log_failure(exc)
            raise HTTPException(503,'Playback unavailable') from None
        return Response(status_code=302,headers={'Location':location,'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
    return [{'path':'/play/{token}','endpoint':play,'methods':['GET','HEAD'],'summary':'115 direct playback','allow_anonymous':True}]
