from fastapi import Request, HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from .models import MissingFile, ToolError
import re
try:
    from app.log import logger
except ModuleNotFoundError:
    import logging
    logger=logging.getLogger(__name__)

def log_failure(exc):
    code=getattr(exc,'upstream_code',None)
    code=code if type(code) is int else None
    operation=getattr(exc,'operation','unknown')
    allowed={'fs_file','fs_files','fs_mkdir','share_snap','share_receive','download_url','share_download_url','share_send','share_update','fs_delete'}
    operation=operation if isinstance(operation,str) and operation in allowed else 'unknown'
    sdk_error=getattr(exc,'sdk_error','none')
    sdk_error=sdk_error if isinstance(sdk_error,str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,80}',sdk_error) else 'unknown'
    # Never log the request, token, URL, or exception text/upstream payload.
    logger.warning(f'115播放失败：接口={operation}，类型={type(exc).__name__}，SDK异常={sdk_error}，上游错误码={code}')

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
