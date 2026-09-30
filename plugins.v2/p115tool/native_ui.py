"""MoviePilot PageRender events: host-login auth, no embedded management key."""
import hmac
from importlib import import_module
from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from .models import SafetyError, ToolError
from .browser import VirtualBrowser


def routes(plugin):
    try:
        user_oper = import_module('app.db.user_oper')
    except ModuleNotFoundError as exc:
        if exc.name not in {'app', 'app.db', 'app.db.user_oper'}:
            raise RuntimeError('MoviePilot administrator authentication could not load') from None
        return []  # No host auth means no native route, including standalone mode.
    except ImportError:
        raise RuntimeError('MoviePilot administrator authentication could not load') from None

    host_administrator = getattr(user_oper, 'get_current_active_superuser_async', None)
    if not callable(host_administrator):
        host_administrator = getattr(user_oper, 'get_current_active_superuser', None)
    if not callable(host_administrator):
        raise RuntimeError('MoviePilot administrator authentication is unavailable')

    async def administrator(user=Depends(host_administrator)):
        if getattr(user, 'is_superuser', False) is not True or getattr(user, 'is_active', False) is not True:
            raise HTTPException(403, 'Active MoviePilot administrator required')

    async def dispatch(request, function):
        from .transport import bounded_body, decode_body
        try:
            payload = decode_body(await bounded_body(request), request.headers.get('content-type', ''))
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(422, 'Native action validation failed') from None
        try:
            return await run_in_threadpool(function, plugin, payload)
        except (TypeError, ValueError):
            raise HTTPException(422, 'Native action validation failed') from None
        except SafetyError:
            raise HTTPException(409, 'Native action safety gate rejected operation') from None
        except KeyError:
            raise HTTPException(404, 'Native action object unavailable') from None
        except ToolError:
            raise HTTPException(503, 'Plugin unavailable') from None
        except Exception:
            raise HTTPException(500, 'Native action failed') from None

    async def endpoint(request: Request):
        return await dispatch(request, action)

    async def data_endpoint(request: Request):
        return await dispatch(request, read_data)

    async def validate_endpoint(request: Request):
        return await dispatch(request, validate_config)

    return [{'path': '/native/' + path, 'endpoint': handler, 'methods': ['POST'],
             'summary': 'MoviePilot native plugin action', 'allow_anonymous': False, 'auth': 'bear',
             'dependencies': [Depends(administrator)]}
            for path, handler in [('action', endpoint), ('data', data_endpoint), ('validate', validate_endpoint)]]


def config_schema(plugin):
    form, _ = plugin.get_form()
    groups = []
    for panel in form[1]['content']:
        fields = []
        for row in panel['content'][1]['content'][0]['content']:
            item = row['content'][0]
            props = item['props']
            kind = {'VSwitch': 'boolean', 'VTextarea': 'json'}.get(item['component'], props.get('type', 'text'))
            fields.append({'name': props['model'], 'label': props['label'], 'type': kind})
        groups.append({'title': panel['content'][0]['text'], 'fields': fields})
    return groups


def validate_config(plugin, payload):
    from .host_config import form_data
    from .config import Config
    if not isinstance(payload, dict) or set(payload) != {'config'} or not isinstance(payload['config'], dict):
        raise ValueError('Invalid configuration envelope')
    # Validate an explicit form snapshot without storing it or touching any SDK.
    Config.from_dict(form_data(payload['config']))
    return {'valid': True}


def read_data(plugin, payload):
    """Local, administrator-only data contract for the federated interface.

    Never returns config defaults, SDK objects, raw settings or job payloads.
    Reads do not modify the global PageRender navigation state.
    """
    if not isinstance(payload, dict):
        raise ValueError('Expected object')
    kind = payload.get('kind')
    shape = {'bootstrap': set(), 'dashboard': set(), 'folders': {'cid', 'offset', 'limit'},
             'media': {'prefix', 'q', 'storage', 'status', 'offset', 'limit'},
             'tree': {'prefix', 'q', 'storage', 'status', 'offset', 'limit'},
             'detail': {'media_id'}, 'groups': {'offset', 'limit'},
             'group': {'group_id'}, 'cache': {'offset', 'limit'},
             'jobs': {'offset', 'limit'}, 'job': {'job_id'}, 'ledger': {'offset', 'limit'}}
    if kind not in shape or set(payload) - {'kind'} - shape[kind]:
        raise ValueError('Invalid data fields')
    offset, limit = payload.get('offset', 0), payload.get('limit', 20)
    if type(offset) is not int or not 0 <= offset <= 1000000 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('Invalid pagination')
    with plugin._lifecycle:
        if kind == 'bootstrap':
            return {'enabled': plugin.get_state(), 'nonce': plugin._native_nonce,
                    'error': plugin._initialization_error, 'schema': config_schema(plugin)}
        if kind == 'folders':
            import re
            cid = payload.get('cid', '0')
            if not isinstance(cid, str) or not re.fullmatch(r'[0-9]{1,20}', cid):
                raise ValueError('Invalid folder identity')
            from .client import P115ClientManager
            from .strm import virtual_path
            def listing(client):
                items = []
                for file in client.list_files(cid):
                    if file.is_dir:
                        virtual_path('/' + file.name)
                        items.append({'cid': file.file_id, 'name': file.name})
                items.sort(key=lambda row: (row['name'].casefold(), row['cid']))
                return {'items': items[offset:offset+limit], 'total': len(items)}
            if plugin._service:
                with plugin._service._maintenance:
                    return listing(plugin._service.client)
            if not plugin._config.cookie:
                raise ToolError('Save account configuration before browsing')
            client = P115ClientManager(plugin._config)
            try:
                return listing(client)
            finally:
                client.close()
        service = plugin._service
        if not service:
            raise ToolError('Plugin unavailable')
        with service._maintenance:
            service.available()
            db = service.db
            if kind == 'dashboard':
                result = db.dashboard()
                result['account'] = service.account.snapshot()
                result['jobs'] = db.all('SELECT state,COUNT(*) count FROM jobs GROUP BY state')
                return result
            if kind in ('media', 'tree'):
                prefix, query = payload.get('prefix', '/'), payload.get('q', '')
                storage, status = payload.get('storage'), payload.get('status')
                from .models import Status
                if not isinstance(prefix, str) or not isinstance(query, str) or storage not in (None, 'NORMAL', 'SHARE', 'CACHE'):
                    raise ValueError('Invalid browse fields')
                if status is not None and status not in {value.value for value in Status}:
                    raise ValueError('Invalid status')
                return getattr(VirtualBrowser(db), kind)(prefix, query, storage, status, offset, limit)
            if kind == 'detail':
                mid = positive(payload.get('media_id'))
                media = db.media(mid).public()
                share = db.one('SELECT verified_at,range_verified_at,created_at FROM share_objects WHERE media_id=?', (mid,))
                cache = db.one('SELECT created_at,last_access_at,expire_at,lease_until,state FROM cache_objects WHERE media_id=?', (mid,))
                organize = db.one('SELECT state,updated_at FROM organize_plans WHERE media_id=?', (mid,))
                return {'media': media, 'share': share, 'cache': cache, 'organize': organize,
                        'moviepilot_organize': service.automatic_organize_status(mid),
                        'history': db.all('SELECT operation,state,detail,created_at FROM tasks WHERE media_id=? ORDER BY id DESC LIMIT 50', (mid,))}
            if kind in ('groups', 'cache', 'jobs', 'ledger'):
                table = {'groups': 'share_groups', 'cache': 'cache_objects', 'jobs': 'jobs', 'ledger': 'recycle_intents'}[kind]
                total = db.one('SELECT COUNT(*) total FROM ' + table)['total']
                if kind == 'groups':
                    items = [service.groups.public(row['id']) for row in db.all('SELECT id FROM share_groups ORDER BY id DESC LIMIT ? OFFSET ?', (limit, offset))]
                elif kind == 'cache':
                    items = db.all('SELECT c.media_id,m.title,m.virtual_path,c.size,c.last_access_at,c.expire_at,c.lease_until,c.state FROM cache_objects c JOIN media m ON m.id=c.media_id ORDER BY c.media_id DESC LIMIT ? OFFSET ?', (limit, offset))
                elif kind == 'jobs':
                    items = service.jobs.list(limit, offset)
                else:
                    from .recycle import history
                    items = history(service, limit, offset)
                return {'items': items, 'total': total, 'offset': offset, 'limit': limit}
            if kind == 'group':
                gid = positive(payload.get('group_id'))
                result = service.groups.public(gid)
                result['members'] = [db.media(row['media_id']).public() for row in service.groups.members(gid)]
                return result
            if kind == 'job':
                jid = positive(payload.get('job_id'))
                return service.jobs.get(jid)


def positive(value):
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ValueError('Invalid identity')
    return value


def action(plugin, payload):
    if not isinstance(payload, dict):
        raise ValueError('Expected object')
    with plugin._lifecycle:
        nonce = payload.get('nonce')
        if not isinstance(nonce, str) or not hmac.compare_digest(nonce, plugin._native_nonce):
            raise SafetyError('Page expired: reload MoviePilot plugin page')
        service = plugin._service
        if not service:
            raise ToolError('Plugin is not initialized')
        service.available()
        name = payload.get('action')
        base = {'action', 'nonce'}
        if name == 'browse':
            if set(payload) - base - {'prefix', 'storage', 'offset'}:
                raise ValueError('Unexpected field')
            prefix, storage, offset = payload.get('prefix', '/'), payload.get('storage'), payload.get('offset', 0)
            if storage not in (None, 'NORMAL', 'SHARE', 'CACHE') or type(offset) is not int or not 0 <= offset <= 1000000:
                raise ValueError('Invalid browse criteria')
            VirtualBrowser(service.db).filters(prefix, '', storage, None)
            plugin._native_view = {'prefix': prefix, 'storage': storage, 'offset': offset}
            return {'success': True}
        if name in ('scan', 'health', 'cleanup', 'share_batch', 'generate_batch'):
            if set(payload) - base:
                raise ValueError('Unexpected field')
            data = {'allow_delete': False} if name == 'scan' else {}
            if name == 'share_batch':
                return service.jobs.enqueue_active('virtualize', {'delete': service.config.delete_source})
            return service.jobs.enqueue_active(name, data)
        if name in ('generate', 'archive', 'restore', 'auto_organize'):
            if set(payload) - base - {'media_id'}:
                raise ValueError('Unexpected field')
            mid = positive(payload.get('media_id'))
            service.db.media(mid)
            data = {'media_id': mid}
            if name == 'archive':
                data['delete'] = False
            return service.jobs.enqueue_active(name, data)
        if name in ('cancel', 'retry'):
            if set(payload) - base - {'job_id'}:
                raise ValueError('Unexpected field')
            jid = positive(payload.get('job_id'))
            return service.jobs.cancel(jid) if name == 'cancel' else service.jobs.retry(jid)
        operations = {'source_reconcile': service.reconcile_delete,
                      'cache_reconcile': service.reconcile_cache_delete,
                      'organize_reconcile': lambda mid: service.reconcile_organize(mid, resume=False),
                      'restore_reconcile': lambda mid: service.restore(mid, reconcile_only=True)}
        if name in operations:
            if set(payload) - base - {'media_id'}:
                raise ValueError('Unexpected field')
            mid = positive(payload.get('media_id'))
            operations[name](mid)
            service.db.log('native_reconcile', 'DONE', mid, 'Operation-specific read-only reconciliation completed')
            return {'success': True}
        raise ValueError('Unsupported native action')


def button(plugin, label, action_name, **params):
    return {'component': 'VBtn', 'props': {'variant': 'tonal', 'size': 'small', 'class': 'ma-1'},
            'text': label, 'events': {'click': {'api': 'plugin/P115Tool/native/action', 'method': 'POST',
                'params': {'action': action_name, 'nonce': plugin._native_nonce, **params}}}}


def page(plugin):
    service = plugin._service
    view = plugin._native_view
    browser = VirtualBrowser(service.db)
    result = browser.tree(view['prefix'], storage=view['storage'], offset=view['offset'], limit=20)
    controls = [button(plugin, '立即扫描整理后目录', 'scan'), button(plugin, '一键扫描并创建虚拟分享', 'share_batch'), button(plugin, '开始生成STRM', 'generate_batch'), button(plugin, '健康检查', 'health'),
                button(plugin, '受保护缓存清理', 'cleanup')]
    filters = []
    for label, storage in [('全部存储', None), ('个人盘', 'NORMAL'), ('虚拟分享', 'SHARE'), ('转存／缓存', 'CACHE')]:
        filters.append(button(plugin, label, 'browse', prefix=result['prefix'], storage=storage, offset=0))
    navigation = [button(plugin, '媒体库根目录', 'browse', prefix='/', storage=view['storage'], offset=0)]
    if result['parent']:
        navigation.append(button(plugin, '返回父目录', 'browse', prefix=result['parent'], storage=view['storage'], offset=0))
    if view['offset']:
        navigation.append(button(plugin, '上一页', 'browse', prefix=result['prefix'], storage=view['storage'], offset=max(0, view['offset']-20)))
    if view['offset'] + 20 < result['total']:
        navigation.append(button(plugin, '下一页', 'browse', prefix=result['prefix'], storage=view['storage'], offset=view['offset']+20))
    entries = []
    for item in result['items']:
        if item['is_directory']:
            entries.append({'component': 'VListItem', 'props': {'title': item['name'],
                'subtitle': f"{item['media_count']} 个媒体 · {item['bytes']/1024**3:.2f} GiB"},
                'content': [button(plugin, '打开目录', 'browse', prefix=item['path'], storage=view['storage'], offset=0)]})
        else:
            mid = item['media_id']
            media = service.db.media(mid)
            actions = [button(plugin, '生成STRM', 'generate', media_id=mid),
                       button(plugin, '归档分享（保留源）', 'archive', media_id=mid)]
            if media.storage_type != 'NORMAL':
                actions.extend([button(plugin, '转存回原目录', 'restore', media_id=mid),
                                button(plugin, '转存只读对账', 'restore_reconcile', media_id=mid),
                                button(plugin, '缓存删除对账', 'cache_reconcile', media_id=mid)])
            if media.status in ('SOURCE_DELETING', 'FAILED_DELETE'):
                actions.append(button(plugin, '源删除只读对账', 'source_reconcile', media_id=mid))
            if service.db.one("SELECT media_id FROM organize_plans WHERE media_id=? AND state<>'DONE'", (mid,)):
                actions.append(button(plugin, '整理只读对账', 'organize_reconcile', media_id=mid))
            entries.append({'component': 'VCard', 'props': {'variant': 'outlined', 'class': 'mb-2'}, 'content': [
                {'component': 'VCardTitle', 'text': f'#{mid} {item["name"]}'},
                {'component': 'VCardSubtitle', 'text': f'{media.storage_type} · {media.status} · {media.size/1024**3:.2f} GiB'},
                {'component': 'VCardActions', 'props': {'class': 'flex-wrap'}, 'content': actions}]})
    jobs = []
    for job in service.jobs.list(limit=20):
        actions = []
        if job['state'] == 'PENDING':
            actions.append(button(plugin, '取消待执行', 'cancel', job_id=job['id']))
        elif job['kind'] in ('health', 'generate') and job['state'] in ('FAILED', 'NEEDS_ATTENTION', 'CANCELLED'):
            actions.append(button(plugin, '重试安全任务', 'retry', job_id=job['id']))
        jobs.append({'component': 'VListItem', 'props': {'title': f"#{job['id']} {job['kind']} · {job['state']}",
            'subtitle': '远端写结果不明时，请使用媒体专属对账，不能直接重试。'}, 'content': actions})
    from .recycle import history
    ledger = history(service, limit=20)
    return [
        {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal'},
            'text': '操作使用当前管理员登录态。一键虚拟分享会重新扫描整理目录；启用允许删除后验证并删除原影视文件，保留文件夹。定时删除还须开启自动删除。资源转存默认永久保存回原目录。'},
        {'component': 'VCardActions', 'props': {'class': 'flex-wrap'}, 'content': controls},
        {'component': 'VCard', 'content': [{'component': 'VCardTitle', 'text': '虚拟媒体目录'},
            {'component': 'VCardText', 'text': f"{result['prefix']} · {result['total']} 项 · 第 {view['offset']//20+1} 页"},
            {'component': 'VCardActions', 'props': {'class': 'flex-wrap'}, 'content': filters + navigation},
            {'component': 'VCardText', 'content': entries or [{'component': 'VAlert', 'text': '当前目录没有匹配媒体。返回根目录或扫描配置目录。'}]}]},
        {'component': 'VCard', 'content': [{'component': 'VCardTitle', 'text': '原生任务操作（最近20条）'},
            {'component': 'VList', 'content': jobs}]},
        {'component': 'VCard', 'content': [{'component': 'VCardTitle', 'text': '删除请求账本（最近20条）'},
            {'component': 'VCardText', 'text': '这里只记录插件删除意图与观察结果，不是回收站列表，也不证明条目归属。未实现永久清理。'},
            {'component': 'VDataTable', 'props': {'headers': [{'title': label, 'key': key} for key,label in
                [('id','请求'),('media_id','媒体'),('purpose','用途'),('file_id','原文件ID'),('state','结果'),('requested_at','请求时间')]],
                'items': [{key:row[key] for key in ('id','media_id','purpose','file_id','state','requested_at')} for row in ledger]}}]}]
