from __future__ import annotations
import inspect
import json
from typing import get_type_hints
from urllib.parse import parse_qs
from email.parser import BytesParser
from email.policy import default as email_policy
from fastapi import Request, HTTPException
from pydantic import create_model, TypeAdapter
from starlette.concurrency import run_in_threadpool

MAX_BODY = 1024 * 1024


def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON keys')
        result[key]=value
    return result


def decode_body(raw, content_type, webhook=False):
    kind=content_type.split(';',1)[0].strip().lower()
    if webhook and kind=='application/x-www-form-urlencoded':
        fields=parse_qs(raw.decode('utf-8'),strict_parsing=True,max_num_fields=10)
        if set(fields)!={'data'} or len(fields['data'])!=1:
            raise ValueError('Expected one data field')
        raw=fields['data'][0].encode()
    elif webhook and kind=='multipart/form-data':
        if '\r' in content_type or '\n' in content_type:
            raise ValueError('Invalid content type')
        message=BytesParser(policy=email_policy).parsebytes(b'Content-Type: '+content_type.encode('ascii')+b'\r\nMIME-Version: 1.0\r\n\r\n'+raw)
        if message.defects or not message.is_multipart():
            raise ValueError('Invalid multipart envelope')
        parts=list(message.iter_parts())
        if len(parts)!=1:
            raise ValueError('Expected one data part')
        part=parts[0]
        if (part.defects or part.is_multipart() or part.get_filename() is not None
                or part.get_param('name',header='Content-Disposition')!='data'):
            raise ValueError('Invalid data part')
        raw=part.get_payload(decode=True)
        if raw is None:
            raise ValueError('Empty data part')
    elif kind not in ('application/json',''):
        raise HTTPException(415,'Unsupported request content type')
    return json.loads(raw.decode('utf-8'),object_pairs_hook=unique_object)


async def bounded_body(request):
    header=request.headers.get('content-length')
    if header:
        try:
            length=int(header)
        except ValueError:
            raise HTTPException(400,'Invalid request') from None
        if length<0:
            raise HTTPException(400,'Invalid request')
        if length>MAX_BODY:
            raise HTTPException(413,'Request body too large')
    chunks=[]
    length=0
    async for chunk in request.stream():
        length+=len(chunk)
        if length>MAX_BODY:
            raise HTTPException(413,'Request body too large')
        chunks.append(chunk)
    return b''.join(chunks)


def protected_route(api, function, path):
    """Validate inside a Request-only callable, not host framework body binding.

    FastAPI's default 422 errors contain raw inputs. A global handler in our app
    would not cover MoviePilot's app, so each exported route owns its validation.
    The original synchronous handlers continue running in the worker threadpool.
    """
    hints=get_type_hints(function)
    signature=inspect.signature(function)
    fields={}
    body_type=None
    for name,parameter in signature.parameters.items():
        if name=='request':
            continue
        annotation=hints[name]
        if name=='body':
            body_type=annotation
        else:
            default=... if parameter.default is inspect.Parameter.empty else parameter.default
            fields[name]=(annotation,default)
    params=create_model(function.__name__+'Params',**fields)
    webhook=function.__name__=='webhook'
    public=function.__name__ in ('play','management_page','management_script','management_style')

    async def endpoint(request: Request):
        if not public:
            try:
                api.authorize(request,webhook=webhook)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(500,'Authentication configuration unavailable') from None
        kwargs={'request':request}
        try:
            values={}
            for name in fields:
                if name in request.path_params:
                    values[name]=request.path_params[name]
                elif name in request.query_params:
                    entries=request.query_params.getlist(name)
                    if len(entries)!=1:
                        raise ValueError('Duplicate query parameter')
                    values[name]=entries[0]
            parsed=params.model_validate(values)
            kwargs.update({name:getattr(parsed,name) for name in fields})
            if body_type is not None:
                raw=await bounded_body(request)
                data=decode_body(raw,request.headers.get('content-type',''),webhook)
                kwargs['body']=TypeAdapter(body_type).validate_python(data)
        except HTTPException:
            raise
        except Exception:
            # Never include errors(), input, exception messages, JSON fragments,
            # dynamic field names, passwords, cookies, or playback tokens.
            raise HTTPException(422,'Request validation failed') from None
        try:
            return await run_in_threadpool(function,**kwargs)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(500,'Operation failed') from None

    endpoint.__name__=function.__name__
    schema=params.model_json_schema()
    required=set(schema.get('required',[]))
    extra={'parameters':[]}
    for name,value in schema.get('properties',{}).items():
        location='path' if '{'+name+'}' in path else 'query'
        extra['parameters'].append({'name':name,'in':location,'required':location=='path' or name in required,'schema':value})
    if body_type is not None:
        body_schema=TypeAdapter(body_type).json_schema()
        extra['requestBody']={'required':True,'content':{'application/json':{'schema':body_schema}}}
        if webhook:
            form={'type':'object','required':['data'],'properties':{'data':{'type':'string'}}}
            for media in ('multipart/form-data','application/x-www-form-urlencoded'):
                extra['requestBody']['content'][media]={'schema':form}
    extra['responses']={'422':{'description':'Request validation failed (inputs are never echoed)'},'413':{'description':'Body exceeds 1 MiB'}}
    return endpoint,extra
