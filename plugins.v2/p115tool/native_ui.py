from importlib import import_module
import json
import re
from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from .config import Config
from .models import ToolError

def decode(raw):
    def pairs(values):
        result={}
        for k,v in values:
            if k in result: raise ValueError('Duplicate field')
            result[k]=v
        return result
    value=json.loads(raw,object_pairs_hook=pairs)
    if not isinstance(value,dict): raise ValueError('Invalid body')
    return value

def dispatch(plugin,route,payload):
    with plugin._lifecycle:
        if route=='validate':
            if set(payload)!={'config'}: raise ValueError('Invalid config envelope')
            Config.from_dict(payload['config']); return {'valid':True}
        if route=='data' and (payload=={'kind':'bootstrap'} or set(payload)=={'kind','ui_contract'} and payload.get('kind')=='bootstrap'):
            current=type(payload.get('ui_contract')) is int and payload['ui_contract']==3
            return {'enabled':plugin.get_state() if current else False,
                    'error':plugin._initialization_error if current else '插件页面已升级，请按 Ctrl+F5 强制刷新整个 MoviePilot 页面，再重新打开插件。旧页面接口已停用，请使用新版存储与STRM页面。',
                    'ui_contract':3,'refresh_required':not current}
        # A request already sent by the cached 0.1.x page may arrive after upgrade.
        # Return only an upgrade notice; do not expose or revive its retired data.
        if route=='data' and payload=={'kind':'dashboard'}:
            return {'refresh_required':True,'message':'请按 Ctrl+F5 刷新 MoviePilot 页面后重新打开插件。',
                    'jobs':[],'account':{},'metrics':{},'daily':{},'cumulative':{}}
        service=plugin._service
        if not service: raise ToolError('Service unavailable')
        if route=='action' and payload=={'action':'generate'}: return service.start()
        if route=='action' and payload=={'action':'scan_storage'}: return service.storage.start('scan')
        if route=='action' and set(payload)=={'action','link','password'} and payload['action']=='import_share':
            return service.storage.start('import',{'link':payload['link'],'password':payload['password']})
        if route=='action' and set(payload)=={'action','ids','delete_source'} and payload['action']=='virtualize':
            return service.storage.start('virtualize',{'ids':payload['ids'],'delete_source':payload['delete_source']})
        if route=='action' and set(payload)=={'action','delete_source'} and payload['action']=='virtualize_all':
            return service.storage.start('virtualize_all',{'delete_source':payload['delete_source']})
        if route=='action' and set(payload)=={'action','ids'} and payload['action']=='reconcile_storage':
            return service.storage.start('reconcile',{'ids':payload['ids']})
        if route=='action' and payload=={'action':'cleanup_copies'}: return service.storage.start('cleanup')
        if route=='action' and set(payload)=={'action','id','link','password'} and payload['action']=='attach_share':
            return service.storage.attach_share(payload['id'],payload['link'],payload['password'])
        if route=='data' and set(payload)=={'kind','page','storage','search'} and payload['kind']=='storage':
            return service.storage.listing(payload['page'],payload['storage'],payload['search'])
        if route=='data' and set(payload)=={'kind','id'} and payload['kind']=='share_details':
            if not isinstance(payload['id'],str) or len(payload['id'])>128: raise ValueError('Invalid media')
            row=service.storage.row(payload['id'])
            if not row or not row['share_code']: raise ValueError('Share unavailable')
            return {'url':'https://115.com/s/'+row['share_code'],'password':row['password']}
        if route=='action' and set(payload)=={'action','cids'} and payload['action']=='organize':
            from .host_transfer import organize
            return organize(service,payload['cids'])
        if route=='data' and payload=={'kind':'status'}: return service.snapshot()
        if route=='data' and payload=={'kind':'organize_sources'}:
            return {'items':[{'cid':source['cid'],'prefix':source['prefix']} for source in service.config.organize_cids],
                    'scheduled':plugin._config.organize_scheduled,'time':plugin._config.organize_time,
                    'paused':getattr(service,'_organize_unknown',False)}
        if route=='data' and payload=={'kind':'sources'}:
            return {'items':[{'cid':source['cid'],'prefix':source['prefix']} for source in service.config.source_cids]}
        if route=='data' and set(payload)=={'kind','cid'} and payload['kind']=='folders':
            cid=payload['cid']
            if not isinstance(cid,str) or not re.fullmatch(r'\d{1,20}',cid): raise ValueError('Invalid folder')
            return {'items':[{'cid':f.file_id,'name':f.name} for f in service.client.list_files(cid) if f.is_dir]}
        raise ValueError('Unsupported action')

def routes(plugin):
    try: module=import_module('app.db.user_oper')
    except ModuleNotFoundError as exc:
        if exc.name not in ('app','app.db','app.db.user_oper'): raise
        return []
    auth=getattr(module,'get_current_active_superuser_async',None) or getattr(module,'get_current_active_superuser',None)
    if not callable(auth): raise RuntimeError('Administrator authentication unavailable')
    async def administrator(user=Depends(auth)):
        if getattr(user,'is_superuser',False) is not True or getattr(user,'is_active',False) is not True: raise HTTPException(403,'Administrator required')
    def endpoint(route):
        async def handler(request:Request):
            try:
                raw=bytearray()
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw)>65536: raise HTTPException(413,'Request too large')
                payload=decode(raw)
                return await run_in_threadpool(dispatch,plugin,route,payload)
            except HTTPException: raise
            except (ValueError,TypeError): raise HTTPException(422,'Invalid plugin request') from None
            except ToolError: raise HTTPException(503,'Plugin unavailable') from None
            except Exception: raise HTTPException(500,'Plugin request failed') from None
        return handler
    return [{'path':'/native/'+r,'endpoint':endpoint(r),'methods':['POST'],'allow_anonymous':False,'auth':'bear','dependencies':[Depends(administrator)]} for r in ('data','action','validate')]
