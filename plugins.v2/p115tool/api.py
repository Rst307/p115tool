from __future__ import annotations
from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
from typing import Callable
from typing import Literal
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from .models import ToolError, RemoteError, SafetyError
from .service import Service


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class MediaRequest(StrictModel):
    media_id: int = Field(gt=0)


class ArchiveRequest(MediaRequest):
    delete_source: StrictBool = False
    confirmation: str = ''


class VerifyRequest(MediaRequest):
    deep: StrictBool = False


class AttachRequest(MediaRequest):
    share_code: str = Field(min_length=1, max_length=64)
    receive_code: str = Field(min_length=4, max_length=4)


class ShareImportRequest(StrictModel):
    share_code: str = Field(min_length=1, max_length=64)
    receive_code: str = Field(min_length=4, max_length=4)
    file_id: str = Field(pattern=r'^\d+$')
    virtual_path: str = Field(min_length=2, max_length=4096)
    title: str | None = None


class GenerateRequest(StrictModel):
    media_id: int | None = Field(default=None, gt=0)
    scan: StrictBool = False


class CleanupStrmRequest(StrictModel):
    media_id: int | None = Field(default=None, gt=0)


class FolderAttachRequest(MediaRequest):
    folder_id: str = Field(pattern=r'^[0-9]+$')
    confirmation: str


class OrganizeRequest(MediaRequest):
    parent_id: str = Field(pattern=r'^\d+$')
    name: str = Field(min_length=1, max_length=255)
    virtual_path: str = Field(min_length=2, max_length=4096)


class ReconcileOrganizeRequest(MediaRequest):
    resume: StrictBool = False
    confirmation: str = ''


class JobRequest(StrictModel):
    kind: Literal['transfer', 'scan', 'health', 'cleanup', 'generate', 'archive', 'archive_group', 'restore', 'organize','auto_organize']
    payload: dict = Field(default_factory=dict)
    confirmation: str = ''


class JobActionRequest(StrictModel):
    action: Literal['cancel', 'retry']
    confirmation: str = ''


class ImportRequest(StrictModel):
    file_id: str = Field(pattern=r'^\d+$')
    virtual_path: str | None = None
    title: str | None = None
    tmdb_id: int | None = Field(default=None, gt=0)
    media_type: Literal['MOVIE','TV','UNKNOWN'] | None = None
    season: int | None = Field(default=None, ge=0, le=999)


class MetadataRequest(MediaRequest):
    media_type: Literal['MOVIE','TV','UNKNOWN']
    season: int | None = Field(default=None, ge=0, le=999)
    tmdb_id: int | None = Field(default=None, gt=0)


class GroupArchiveRequest(StrictModel):
    media_ids: list[StrictInt] = Field(min_length=1, max_length=1000)
    delete_source: StrictBool = False
    confirmation: str = ''


class GroupAttachRequest(StrictModel):
    group_id: int = Field(gt=0)
    share_code: str = Field(min_length=1,max_length=64)
    receive_code: str = Field(min_length=4,max_length=4)


class GroupReconcileRequest(StrictModel):
    group_id: int = Field(gt=0)


class API:
    def __init__(self, get_service: Callable[[], Service]):
        self.get_service = get_service

    def management_page(self, request: Request):
        from .management import asset
        return asset('html')

    def management_script(self, request: Request):
        from .management import asset
        return asset('js')

    def management_style(self, request: Request):
        from .management import asset
        return asset('css')

    def service(self, request=None):
        if request is not None:
            previous=getattr(request.state,'p115tool_service',None)
            if previous is not None:
                return previous
        service = self.get_service()
        if not service:
            raise HTTPException(503, 'Plugin is not initialized')
        if request is not None:
            request.state.p115tool_service=service
        return service

    def authorize(self, request, webhook=False):
        service = self.service(request)
        secret = service.config.webhook_key if webhook else service.config.api_key
        supplied = request.headers.get('x-api-key', '')
        auth = request.headers.get('authorization', '')
        if auth.startswith('Bearer '):
            supplied = auth[7:]
        if len(secret) < 32 or not hmac.compare_digest(supplied.encode(), secret.encode()):
            raise HTTPException(401, 'Missing or invalid API key')
        return service

    def run(self, service, function, *args, **kwargs):
        try:
            with service._maintenance:
                service.available()
                return function(*args, **kwargs)
        except KeyError:
            raise HTTPException(404, 'Media not found') from None
        except (ValueError, TypeError):
            raise HTTPException(400, 'Invalid operation parameters') from None
        except SafetyError:
            raise HTTPException(409, 'Safety gate rejected operation; inspect task status') from None
        except RemoteError:
            raise HTTPException(502, '115 operation failed; credentials or source may need attention') from None
        except ToolError:
            raise HTTPException(503, 'Plugin is unavailable') from None
        except Exception:
            raise HTTPException(500, 'Operation failed; no sensitive upstream details are exposed') from None

    def play(self, request: Request, token: str):
        service = self.service(request)
        link = self.run(service, service.playback, token, request.headers.get('user-agent', ''))
        # Do not proxy bytes, expose Cookie, or cache redirects in intermediary caches.
        return RedirectResponse(link.url, status_code=302, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

    def account(self, request: Request):
        service = self.authorize(request)
        snapshot=self.run(service, service.account.snapshot, refresh=True, force=True)
        if snapshot['logged_in'] is None:
            raise HTTPException(502,'Account check unavailable')
        return {'logged_in':snapshot['logged_in']}

    def account_status(self, request: Request, refresh: bool = False):
        service=self.authorize(request)
        return self.run(service,service.account.snapshot,refresh=refresh)

    def recycle_history(self, request: Request, limit: int = Query(50,ge=1,le=200), offset: int = Query(0,ge=0)):
        service=self.authorize(request)
        from .recycle import history
        return self.run(service,history,service,limit,offset)

    def dashboard(self, request: Request):
        service = self.authorize(request)
        def snapshot():
            data=service.db.dashboard()
            data['account']=service.account.snapshot()
            return data
        return self.run(service,snapshot)

    def media(self, request: Request, media_id: int):
        service = self.authorize(request)
        return self.run(service, lambda: service.db.media(media_id).public())

    def browse(self, request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), prefix: str = '/',
            q: str = Query('', max_length=256), storage: Literal['NORMAL','SHARE','CACHE'] | None = None,
            status: Literal['DISCOVERED','ORGANIZED','SHARE_CREATING','SHARED','VERIFYING','VERIFIED','STRM_CREATED','SOURCE_DELETING','READY','FAILED_SHARE','FAILED_VERIFY','FAILED_STRM','FAILED_DELETE','BROKEN'] | None = None):
        service = self.authorize(request)
        from .browser import VirtualBrowser
        return self.run(service, VirtualBrowser(service.db).media, prefix, q, storage, status, offset, limit)

    def browse_tree(self, request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200), prefix: str = '/',
            q: str = Query('', max_length=256), storage: Literal['NORMAL','SHARE','CACHE'] | None = None,
            status: Literal['DISCOVERED','ORGANIZED','SHARE_CREATING','SHARED','VERIFYING','VERIFIED','STRM_CREATED','SOURCE_DELETING','READY','FAILED_SHARE','FAILED_VERIFY','FAILED_STRM','FAILED_DELETE','BROKEN'] | None = None):
        service = self.authorize(request)
        from .browser import VirtualBrowser
        return self.run(service, VirtualBrowser(service.db).tree, prefix, q, storage, status, offset, limit)

    def import_media(self, request: Request, body: ImportRequest):
        service = self.authorize(request)
        def operation():
            file = service.client.stat(body.file_id)
            return service.ingest(file, body.virtual_path, body.title, body.tmdb_id, media_type=body.media_type,season=body.season).public()
        return self.run(service, operation)

    def generate(self, request: Request, body: GenerateRequest):
        service = self.authorize(request)
        if body.scan and body.media_id:
            raise HTTPException(400, 'Choose scan or media_id')
        if body.scan:
            return self.run(service, service.scan)
        if body.media_id:
            return {'strm_path': self.run(service, service.strm.generate, body.media_id)}
        def generate_all():
            result = {'generated': 0, 'failed': []}
            for row in service.db.all('SELECT id FROM media ORDER BY id'):
                try:
                    service.strm.generate(row['id'])
                    result['generated'] += 1
                except (ToolError, OSError):
                    result['failed'].append(row['id'])
            return result
        return self.run(service, generate_all)

    def clean_strm(self, request: Request, body: CleanupStrmRequest):
        service = self.authorize(request)
        from .scanner import clean_missing
        return self.run(service, clean_missing, service, body.media_id)

    def archive(self, request: Request, body: ArchiveRequest):
        service = self.authorize(request)
        if body.delete_source and body.confirmation != f'DELETE:{body.media_id}':
            raise HTTPException(400, 'Source deletion requires confirmation DELETE:<media_id>')
        return self.run(service, lambda: service.archive(body.media_id, delete=body.delete_source).public())

    def set_metadata(self, request: Request, body: MetadataRequest):
        service=self.authorize(request)
        def operation():
            service.groups.metadata(body.media_id,body.media_type,body.season,body.tmdb_id)
            return {'media_id':body.media_id,'group_key':service.groups.key(body.media_id)}
        return self.run(service,operation)

    def archive_group(self, request: Request, body: GroupArchiveRequest):
        service=self.authorize(request)
        expected='DELETE_GROUP:'+','.join(map(str,sorted(body.media_ids)))
        if body.delete_source and body.confirmation!=expected:
            raise HTTPException(400,'Grouped source deletion requires DELETE_GROUP:<sorted media ids>')
        return self.run(service,service.groups.archive,body.media_ids,body.delete_source)

    def share_groups(self, request: Request, limit: int = Query(50,ge=1,le=200), offset: int = Query(0,ge=0)):
        service=self.authorize(request)
        rows=service.db.all('SELECT id FROM share_groups ORDER BY id DESC LIMIT ? OFFSET ?',(limit,offset))
        return [service.groups.public(row['id']) for row in rows]

    def attach_group(self, request: Request, body: GroupAttachRequest):
        service=self.authorize(request)
        return self.run(service,service.groups.attach,body.group_id,body.share_code,body.receive_code)

    def reconcile_group(self, request: Request, body: GroupReconcileRequest):
        service=self.authorize(request)
        return self.run(service,service.groups.reconcile,body.group_id)

    def repair_group(self, request: Request, body: GroupReconcileRequest):
        service=self.authorize(request)
        return self.run(service,service.repair_group,body.group_id)

    def attach_group_repair(self, request: Request, body: GroupAttachRequest):
        service=self.authorize(request)
        return self.run(service,service.repair_group,body.group_id,attach=(body.share_code,body.receive_code))

    def attach(self, request: Request, body: AttachRequest):
        service = self.authorize(request)
        return self.run(service, lambda: service.attach_share(body.media_id, body.share_code, body.receive_code).public())

    def import_share(self, request: Request, body: ShareImportRequest):
        service = self.authorize(request)
        return self.run(service, lambda: service.import_share(body.share_code, body.receive_code, body.file_id, body.virtual_path, body.title).public())

    def verify(self, request: Request, body: VerifyRequest):
        service = self.authorize(request)
        self.run(service, service.verify_share, body.media_id, body.deep)
        return {'verified': True}

    def restore(self, request: Request, body: MediaRequest):
        service = self.authorize(request)
        obj = self.run(service, service.restore, body.media_id)
        return {'media_id': body.media_id, 'state': obj['state'], 'expire_at': obj['expire_at']}

    def delete_cache(self, request: Request, media_id: int):
        service = self.authorize(request)
        return {'deleted': self.run(service, service.delete_cache, media_id)}

    def reconcile_restore(self, request: Request, body: MediaRequest):
        service = self.authorize(request)
        obj = self.run(service, service.restore, body.media_id, reconcile_only=True)
        return {'media_id': body.media_id, 'state': obj['state'], 'expire_at': obj['expire_at']}

    def restore_folder_candidates(self, request: Request, body: MediaRequest):
        service=self.authorize(request)
        from .recovery import folder_candidates
        return self.run(service, folder_candidates, service, body.media_id)

    def attach_restore_folder(self, request: Request, body: FolderAttachRequest):
        service=self.authorize(request)
        if body.confirmation != f'ADOPT_FOLDER:{body.media_id}:{body.folder_id}':
            raise HTTPException(400,'Directory adoption requires explicit ownership confirmation')
        from .recovery import cache_folder
        folder=self.run(service, cache_folder, service, body.media_id, reconcile_only=True, adopt_id=body.folder_id)
        return {'media_id':body.media_id,'folder_id':folder['folder_id'],'state':'FOLDER_READY'}

    def cleanup(self, request: Request):
        service = self.authorize(request)
        return self.run(service, service.cleanup)

    def reconcile(self, request: Request, body: MediaRequest):
        service = self.authorize(request)
        return self.run(service, service.reconcile_delete, body.media_id)

    def repair(self, request: Request, body: MediaRequest):
        service = self.authorize(request)
        return self.run(service, lambda: service.repair_share(body.media_id).public())

    def health(self, request: Request, deep: bool = False):
        service = self.authorize(request)
        return self.run(service, service.health, deep)

    def attach_repair(self, request: Request, body: AttachRequest):
        service=self.authorize(request)
        return self.run(service, lambda: service.repair_share(body.media_id, attach=(body.share_code,body.receive_code)).public())

    def organize(self, request: Request, body: OrganizeRequest):
        service = self.authorize(request)
        return self.run(service, lambda: service.organize(body.media_id, body.parent_id, body.name, body.virtual_path).public())

    def preview_organize(self, request: Request, body: MediaRequest):
        service=self.authorize(request)
        from .organizer import preview
        return self.run(service,preview,service,body.media_id)

    def automatic_organize(self, request: Request, body: ReconcileOrganizeRequest):
        service=self.authorize(request)
        if body.resume and body.confirmation!=f'AUTO_RESUME:{body.media_id}':
            raise HTTPException(400,'Automatic organize resume requires explicit confirmation')
        return self.run(service,lambda:service.automatic_organize(body.media_id,resume=body.resume).public())

    def automatic_organize_status(self, request: Request, body: MediaRequest):
        service=self.authorize(request)
        return self.run(service,service.automatic_organize_status,body.media_id)

    def reconcile_organize(self, request: Request, body: ReconcileOrganizeRequest):
        service = self.authorize(request)
        if body.resume and body.confirmation != f'RESUME:{body.media_id}':
            raise HTTPException(400, 'Remaining remote writes require RESUME:<media_id>')
        return self.run(service, service.reconcile_organize, body.media_id, body.resume)

    def reconcile_cache(self, request: Request, body: MediaRequest):
        service = self.authorize(request)
        return self.run(service, service.reconcile_cache_delete, body.media_id)

    def jobs(self, request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        service = self.authorize(request)
        # Status must stay inspectable while a remote operation holds maintenance.
        return service.jobs.list(limit, offset)

    def enqueue_job(self, request: Request, body: JobRequest):
        service = self.authorize(request)
        if body.kind == 'archive' and body.payload.get('delete') is True:
            if body.confirmation != f"DELETE:{body.payload.get('media_id')}":
                raise HTTPException(400, 'Queued deletion requires DELETE:<media_id>')
        if body.kind == 'archive_group' and body.payload.get('delete') is True:
            ids=body.payload.get('media_ids',[])
            if not isinstance(ids,list) or any(type(mid) is not int for mid in ids):
                raise HTTPException(400,'Invalid group membership')
            if body.confirmation!='DELETE_GROUP:'+','.join(map(str,sorted(ids))):
                raise HTTPException(400,'Queued group deletion requires DELETE_GROUP:<sorted media ids>')
        key = request.headers.get('idempotency-key')
        if key:
            if len(key) > 128:
                raise HTTPException(400, 'Idempotency key too long')
            import hashlib
            key = 'api:' + hashlib.sha256(key.encode()).hexdigest()
        # Enqueue must not wait for a slow worker holding maintenance. No remote
        # calls occur here; the database transaction is enough to serialize it.
        try:
            return service.jobs.enqueue(body.kind, body.payload, key)
        except SafetyError:
            raise HTTPException(409, 'Queue safety gate rejected operation') from None
        except ValueError:
            raise HTTPException(400, 'Invalid queue parameters') from None
        except ToolError:
            raise HTTPException(503, 'Plugin unavailable') from None

    def job_action(self, request: Request, job_id: int, body: JobActionRequest):
        service = self.authorize(request)
        if body.action == 'retry' and body.confirmation != f'RETRY:{job_id}':
            raise HTTPException(400, 'Retry requires RETRY:<job_id>')
        try:
            return service.jobs.cancel(job_id) if body.action == 'cancel' else service.jobs.retry(job_id)
        except KeyError:
            raise HTTPException(404, 'Job not found') from None
        except SafetyError:
            raise HTTPException(409, 'Remote-write jobs require operation-specific reconciliation') from None

    def webhook(self, request: Request, body: dict):
        service = self.authorize(request, webhook=True)
        from .emby import EmbyBridge
        return self.run(service,EmbyBridge(service).handle,body)

    def routes(self, prefix='/p115tool'):
        definitions = [
            ('/manage', self.management_page, ['GET']),
            ('/manage.js', self.management_script, ['GET']),
            ('/manage.css', self.management_style, ['GET']),
            ('/play/{token}', self.play, ['GET', 'HEAD']),
            ('/redirect/{token}', self.play, ['GET', 'HEAD']),
            ('/account', self.account, ['GET']),
            ('/account/status', self.account_status, ['GET']),
            ('/dashboard', self.dashboard, ['GET']),
            ('/recycle/intents', self.recycle_history, ['GET']),
            ('/media/tree', self.browse_tree, ['GET']),
            ('/media/{media_id}', self.media, ['GET']),
            ('/media', self.browse, ['GET']),
            ('/media/import', self.import_media, ['POST']),
            ('/media/metadata',self.set_metadata,['POST']),
            ('/strm/generate', self.generate, ['POST']),
            ('/strm/cleanup', self.clean_strm, ['POST']),
            ('/archive', self.archive, ['POST']),
            ('/archive/group',self.archive_group,['POST']),
            ('/share/groups',self.share_groups,['GET']),
            ('/share/group/attach',self.attach_group,['POST']),
            ('/share/group/reconcile',self.reconcile_group,['POST']),
            ('/share/group/repair',self.repair_group,['POST']),
            ('/share/group/repair/attach',self.attach_group_repair,['POST']),
            ('/share/attach', self.attach, ['POST']),
            ('/share/import', self.import_share, ['POST']),
            ('/share/verify', self.verify, ['POST']),
            ('/share/repair', self.repair, ['POST']),
            ('/share/repair/attach', self.attach_repair, ['POST']),
            ('/restore', self.restore, ['POST']),
            ('/restore/reconcile', self.reconcile_restore, ['POST']),
            ('/restore/folder/candidates', self.restore_folder_candidates, ['POST']),
            ('/restore/folder/attach', self.attach_restore_folder, ['POST']),
            ('/cache/{media_id}', self.delete_cache, ['DELETE']),
            ('/cache/cleanup', self.cleanup, ['POST']),
            ('/archive/reconcile', self.reconcile, ['POST']),
            ('/health', self.health, ['POST']),
            ('/organize', self.organize, ['POST']),
            ('/organize/preview', self.preview_organize, ['POST']),
            ('/organize/auto', self.automatic_organize, ['POST']),
            ('/organize/auto/status', self.automatic_organize_status, ['POST']),
            ('/organize/reconcile', self.reconcile_organize, ['POST']),
            ('/cache/reconcile', self.reconcile_cache, ['POST']),
            ('/jobs', self.jobs, ['GET']),
            ('/jobs', self.enqueue_job, ['POST']),
            ('/jobs/{job_id}', self.job_action, ['POST']),
            ('/emby/webhook', self.webhook, ['POST']),
        ]
        from .transport import protected_route
        routes=[]
        for path,function,methods in definitions:
            endpoint,extra=protected_route(self,function,prefix+path)
            for method in methods:
                routes.append({'path':prefix+path,'endpoint':endpoint,'methods':[method],
                    'summary':function.__name__,'allow_anonymous':True,'openapi_extra':extra,
                    'include_in_schema':method!='HEAD'})
        return routes


def create_app(config=None, service=None):
    @asynccontextmanager
    async def lifespan(app):
        if app.state.owned:
            app.state.service.jobs.start()
        try:
            yield
        finally:
            if app.state.owned:
                app.state.service.close()
    app = FastAPI(title='115 工具箱', version='0.1.0', lifespan=lifespan)
    if service is None:
        if config is None:
            path = os.environ.get('P115TOOL_CONFIG', 'config.json')
            config = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        service = Service(config)
        app.state.owned = True
    else:
        app.state.owned = False
    app.state.service = service
    api = API(lambda: app.state.service)
    for route in api.routes(service.config.playback_prefix):
        route.pop('allow_anonymous')
        app.add_api_route(**route)
    return app
