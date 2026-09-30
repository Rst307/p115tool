from fastapi import Request, HTTPException
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response
from .models import MissingFile, ToolError
import re

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
        except MissingFile: raise HTTPException(404,'Playback unavailable') from None
        except Exception: raise HTTPException(503,'Playback unavailable') from None
        return Response(status_code=302,headers={'Location':location,'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
    return [{'path':'/play/{token}','endpoint':play,'methods':['GET','HEAD'],'summary':'115 direct playback','allow_anonymous':True}]
