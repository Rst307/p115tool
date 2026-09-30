from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
from datetime import datetime, timezone
import json
import logging
import secrets
import threading
from .config import Config
from .service import Service

try:
    from app.plugins import _PluginBase
    from app.core.event import eventmanager
    from app.schemas.types import EventType
    from app.log import logger
except ModuleNotFoundError as exc:
    if exc.name != 'app':
        raise
    logger = logging.getLogger('p115tool')
    class _PluginBase:
        def update_config(self, config):
            self._saved_config = config
        def get_data_path(self):
            return Path('./data/p115tool')
    def transfer_listener(function):
        return function
else:
    def transfer_listener(function):
        return eventmanager.register(EventType.TransferComplete)(function)


def get_value(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


class P115Tool(_PluginBase):
    plugin_name = '115 工具箱'
    plugin_desc = '统一 STRM、302 播放、分享虚拟存储、归档和恢复缓存'
    plugin_icon = 'https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/src/assets/images/misc/u115.png'
    plugin_version = '0.1.3'
    plugin_author = 'Rst307'
    author_url = 'https://github.com/Rst307'
    plugin_config_prefix = 'p115tool_'
    plugin_order = 99
    auth_level = 1

    def __init__(self):
        super().__init__()
        self._service = None
        self._config = Config()
        self._lifecycle = threading.RLock()
        self._initialization_error = None
        self._native_nonce = secrets.token_urlsafe(24)
        self._native_view = {'prefix': '/', 'storage': None, 'offset': 0}

    def init_plugin(self, config=None):
        with self._lifecycle:
            previous = self._config
            try:
                from .host_config import form_data
                data = form_data(config)
                data.setdefault('data_dir', str(self.get_data_path()))
                data.setdefault('playback_prefix', '/api/v1/plugin/P115Tool')
                if not data.get('api_key'):
                    data['api_key'] = previous.api_key or secrets.token_urlsafe(32)
                if not data.get('webhook_key'):
                    data['webhook_key'] = previous.webhook_key or secrets.token_urlsafe(32)
                candidate = Config.from_dict(data)
            except Exception:
                self._initialization_error = '配置无效：本次更新未生效，请检查字段类型、规则和目录。'
                logger.error(self._initialization_error)
                if previous.api_key:
                    try:
                        self.update_config(asdict(previous))
                    except Exception:
                        logger.error('原配置持久化失败，请检查宿主配置存储。')
                return
            # Validate before touching the live service. A single data directory
            # cannot be opened twice, so replacement must drain the old worker.
            self.stop_service()
            candidate_service = None
            try:
                if candidate.enabled:
                    candidate_service = Service(candidate)
                self.update_config(asdict(candidate))
                if candidate_service:
                    candidate_service.jobs.start()
                self._config, self._service = candidate, candidate_service
                self._native_nonce = secrets.token_urlsafe(24)
                self._native_view = {'prefix': '/', 'storage': None, 'offset': 0}
                self._initialization_error = None
            except Exception:
                if candidate_service:
                    candidate_service.close()
                self._config = previous
                self._initialization_error = '初始化失败：检查目录权限、依赖及是否存在其他实例。'
                if previous.api_key:
                    restored = None
                    try:
                        self.update_config(asdict(previous))
                        if previous.enabled:
                            restored = Service(previous)
                            restored.jobs.start()
                            self._service = restored
                    except Exception:
                        if restored:
                            restored.close()
                        self._service = None
                logger.error(self._initialization_error)

    def get_state(self):
        return bool(self._service and self._config.enabled)

    @staticmethod
    def get_command():
        return []

    @staticmethod
    def get_render_mode():
        return 'vue', 'dist/assets'

    def get_api(self):
        from .api import API
        # MoviePilot's PluginManager adds /<plugin ID>; the endpoint registrar
        # subsequently adds /api/v1/plugin. Return plugin-relative paths here.
        from .native_ui import routes
        return API(lambda: self._service).routes('') + routes(self)

    def get_service(self):
        if not self.get_state():
            return []
        from apscheduler.triggers.interval import IntervalTrigger
        return [
            {'id': 'p115tool_scan', 'name': '115 工具箱目录同步', 'trigger': IntervalTrigger(seconds=self._config.scan_interval), 'func': self.run_scan, 'kwargs': {}},
            {'id': 'p115tool_health', 'name': '115 工具箱健康检查', 'trigger': IntervalTrigger(days=1), 'func': self.run_health, 'kwargs': {}},
            {'id': 'p115tool_cleanup', 'name': '115 工具箱缓存清理', 'trigger': IntervalTrigger(minutes=30), 'func': self.run_cleanup, 'kwargs': {}},
            {'id': 'p115tool_deep_health', 'name': '115 工具箱每周分批深度检查', 'trigger': IntervalTrigger(weeks=1), 'func': self.run_deep_health, 'kwargs': {}},
        ]

    def _job(self, name, function):
        with self._lifecycle:
            if self._service:
                try:
                    return function(self._service)
                except Exception:
                    self._service.db.log(name, 'FAILED', detail='Operation failed; credentials/source may require attention')
                    logger.warning('115 工具箱任务失败：%s', name)

    def run_scan(self):
        return self._job('scan', lambda service: service.jobs.enqueue_active('scan'))

    def run_health(self):
        return self._job('health', lambda service: service.jobs.enqueue_active('health'))

    def run_deep_health(self):
        return self._job('health_deep', lambda service: service.jobs.enqueue_active('health', {'deep': True}))

    def run_cleanup(self):
        return self._job('cleanup', lambda service: service.jobs.enqueue_active('cleanup'))

    @transfer_listener
    def on_transfer_complete(self, event):
        with self._lifecycle:
            if not self.get_state():
                return
            data = get_value(event, 'event_data', {}) or {}
            transfer = get_value(data, 'transferinfo')
            item = get_value(transfer, 'target_item')
            if not item or get_value(item, 'storage') not in ('115网盘', '115云盘', 'u115'):
                return
            path = str(get_value(item, 'path', ''))
            fid = str(get_value(item, 'fileid', get_value(item, 'file_id', '')))
            if not fid.isdigit() or Path(path).suffix.lower() not in self._config.media_extensions:
                self._service.db.log('transfer', 'SKIPPED', detail='Unsupported target metadata; scheduled scan will reconcile')
                return
            try:
                info = get_value(data, 'mediainfo')
                kind = get_value(info, 'type')
                kind = getattr(kind, 'name', kind)
                kind = {'电影':'MOVIE','电视剧':'TV','Movie':'MOVIE','TV':'TV','MOVIE':'MOVIE'}.get(str(kind))
                meta = get_value(data, 'meta')
                season = get_value(meta,'begin_season', get_value(meta,'season'))
                self._service.jobs.enqueue_transfer({'file_id': fid, 'virtual_path': path,
                    'title': get_value(info, 'title'), 'tmdb_id': get_value(info, 'tmdb_id'),
                    'allow_delete': self._config.auto_delete, 'media_type':kind,'season':season})
                self._service.db.log('transfer', 'QUEUED')
            except Exception:
                self._service.db.log('transfer', 'FAILED', detail='Transfer processing failed; source remains intact')
                logger.warning('115 工具箱整理事件处理失败，请检查任务日志')

    def stop_service(self):
        with self._lifecycle:
            if self._service:
                self._service.close()
                self._service = None

    def get_form(self):
        groups = [
            ('基础', [('enabled', '启用插件', 'switch'), ('public_url', 'Emby 可访问的服务地址（不含接口路径）', 'text'), ('data_dir', 'SQLite 数据目录', 'text'), ('statistics_utc_offset', '每日统计UTC偏移（分钟，默认480即UTC+08:00）', 'number')]),
            ('115账户', [('cookie', '115 Cookie（仅本地保存）', 'password'), ('account_status_ttl', '账号状态缓存秒数（1至3600）', 'number')]),
            ('STRM', [('strm_dir', 'STRM 输出目录', 'text'), ('auto_generate', '自动生成 STRM', 'switch'), ('clean_missing_strm', '完整扫描后清理已确认失效的普通STRM（默认关闭）', 'switch'), ('source_cids_json', '扫描目录 JSON，例如 [{"cid":"123","prefix":"/电影"}]', 'textarea')]),
            ('302播放', [('playback_prefix', '接口前缀（MoviePilot 请保留默认）', 'text'), ('url_cache_ttl', '直链缓存秒数', 'number'), ('max_concurrency', '最大并发', 'number')]),
            ('虚拟分享', [('share_enabled', '启用分享存储', 'switch'), ('auto_repair_share', '健康检查自动重新分享（默认关闭；保留源/缓存，不删除）', 'switch'), ('share_strategy', '分享策略：auto/file/movie/season', 'text'), ('group_settle_seconds', '整理事件分组等待时间（秒）', 'number'), ('auto_archive', '自动创建并验证分享', 'switch'), ('policies_json', '文件级存储规则 JSON', 'textarea')]),
            ('整理', [('auto_organize_enabled', '启用自动识别整理（默认关闭）', 'switch'), ('organize_root_cid', '115整理目标根目录CID（不可为根目录）', 'text'), ('scan_interval', '目录同步间隔（秒）', 'number'), ('organize_templates_json', '电影/电视剧路径模板 JSON（MOVIE/TV）', 'textarea')]),
            ('缓存', [('cache_cid', '115 临时缓存目录 CID（不可为根目录）', 'text'), ('cache_max_bytes', '缓存容量上限（字节）', 'number'), ('cache_ttl', '无访问过期时间（秒）', 'number'), ('playback_lease', '播放保护时间（秒，至少等于过期时间）', 'number')]),
            ('安全', [('delete_source', '允许验证后删除源文件至回收站', 'switch'), ('auto_delete', '自动归档后删除源文件（高风险，需同时启用允许删除）', 'switch'), ('api_key', '管理 API Key（32字符以上）', 'password'), ('webhook_key', 'Emby Webhook Key（32字符以上）', 'password')]),
            ('高级', [('request_timeout', '115 请求超时（秒）', 'number'), ('health_batch', '每次健康检查数量', 'number'), ('emby_path_mappings_json', 'Emby目录映射 JSON：emby/local（local须在STRM目录下）', 'textarea'), ('allowed_cdn_suffixes_json', '允许的 CDN 域名后缀 JSON', 'textarea'), ('media_extensions_json', '视频扩展名 JSON', 'textarea')]),
        ]
        panels = []
        for title, entries in groups:
            rows = []
            for name, label, kind in entries:
                component = 'VSwitch' if kind == 'switch' else ('VTextarea' if kind == 'textarea' else 'VTextField')
                props = {'model': name, 'label': label}
                if kind in ('password', 'number'):
                    props['type'] = kind
                rows.append({'component': 'VCol', 'props': {'cols': 12}, 'content': [{'component': component, 'props': props}]})
            panels.append({'component': 'VExpansionPanel', 'content': [
                {'component': 'VExpansionPanelTitle', 'text': title},
                {'component': 'VExpansionPanelText', 'content': [{'component': 'VRow', 'content': rows}]}]})
        defaults = asdict(self._config)
        if not self._service and self._config == Config():
            defaults['data_dir'] = str(self.get_data_path())
            defaults['playback_prefix'] = '/api/v1/plugin/P115Tool'
        for name in ('source_cids', 'policies', 'allowed_cdn_suffixes', 'media_extensions', 'emby_path_mappings', 'organize_templates'):
            defaults[name + '_json'] = json.dumps(defaults[name], ensure_ascii=False)
        form = [{'component': 'VAlert', 'props': {'type': 'warning', 'variant': 'tonal'},
                 'text': '分享不是可靠备份。删除源文件可能使分享失效；先用可丢弃文件实测。默认保留源文件且永不自动清空回收站。'},
                {'component': 'VExpansionPanels', 'content': panels}]
        return form, defaults

    def get_page(self):
        with self._lifecycle:
            result = self._get_page()
            if self._initialization_error and self._service:
                result.insert(0, {'component': 'VAlert', 'props': {'type': 'error'}, 'text': self._initialization_error})
            return result

    def _get_page(self):
        if self._initialization_error and not self._service:
            return [{'component': 'VAlert', 'props': {'type': 'error'}, 'text': self._initialization_error}]
        if not self._service:
            return [{'component': 'VAlert', 'props': {'type': 'info'}, 'text': '请先配置账户、目录及服务地址，再启用插件。'}]
        data = self._service.db.dashboard()
        cards = []
        account=self._service.account.snapshot()
        labels={'UNCHECKED':'尚未检查','AUTHENTICATED':'已登录','EXPIRED':'登录已失效','UNKNOWN':'检查不可用'}
        checked='未检查' if account['checked_at'] is None else datetime.fromtimestamp(account['checked_at'],timezone.utc).isoformat(timespec='seconds')
        account_text=f"{labels[account['state']]} · {'已过期，仅为历史观察' if account['stale'] else '缓存观察'} · {checked}"
        cards.append({'component':'VCol','props':{'cols':12,'md':6},'content':[{'component':'VCard','content':[
            {'component':'VCardTitle','text':'115账号登录状态'}, {'component':'VCardText','text':account_text}]}]})
        capacity=account['capacity']
        capacity_text='容量尚未获取或响应不可识别' if capacity is None else (
            f"账户报告：已用 {capacity['used_bytes']/1024**3:.2f} GiB / 总额 {capacity['total_bytes']/1024**3:.2f} GiB · 可用 {capacity['free_bytes']/1024**3:.2f} GiB")
        cards.append({'component':'VCol','props':{'cols':12,'md':6},'content':[{'component':'VCard','content':[
            {'component':'VCardTitle','text':'115账户容量（缓存观察）'}, {'component':'VCardText','text':capacity_text}]}]})
        daily = data['today']
        values = daily['metrics']
        summary = [
            ('今日STRM写入', str(int(values.get('strm_generated', 0)))),
            ('今日分享创建', str(int(values.get('shares_created', 0)))),
            ('今日源删除确认', f"{values.get('source_bytes_released', 0) / 1024**3:.2f} GiB（非容量到账）"),
            ('今日播放请求', str(int(daily['playback']['requests']))),
            ('STRM记录 / 分享 / 异常', f"{data['counts']['strm_records']} / {data['counts']['shares']} / {data['counts']['abnormal']}")]
        for label, value in summary:
            cards.append({'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [
                {'component': 'VCard', 'content': [{'component': 'VCardTitle', 'text': label},
                    {'component': 'VCardText', 'text': value}]}]})
        performance = []
        for label, stats in [('今日', daily['playback']), ('累计', data['playback'])]:
            performance.append({'period': label, 'requests': int(stats['requests']),
                'success_rate': '暂无请求' if stats['success_rate'] is None else f"{stats['success_rate']:.1%}",
                'cache_hit_rate': '暂无请求' if stats['cache_hit_rate'] is None else f"{stats['cache_hit_rate']:.1%}",
                'mean_resolve_seconds': '暂无非缓存成功' if stats['mean_resolve_seconds'] is None else f"{stats['mean_resolve_seconds']:.3f}"})
        for item in data['storage']:
            cards.append({'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [
                {'component': 'VCard', 'content': [
                    {'component': 'VCardTitle', 'text': item['storage_type']},
                    {'component': 'VCardText', 'text': f"{item['count']} 个媒体 · {item['bytes'] / 1024**3:.2f} GiB"}]}]})
        cards.append({'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [
            {'component': 'VCard', 'content': [{'component': 'VCardTitle', 'text': '临时缓存'},
                                            {'component': 'VCardText', 'text': f"{data['cache']['count']} 个 · {data['cache']['bytes'] / 1024**3:.2f} GiB"}]}]})
        items = self._service.db.all('SELECT id,title,virtual_path,size,storage_type,status FROM media ORDER BY id DESC LIMIT 200')
        headers = [{'title': label, 'key': key} for key, label in [('id','ID'), ('title','标题'), ('virtual_path','虚拟路径'), ('size','字节'), ('storage_type','存储'), ('status','状态')]]
        from .native_ui import page
        return page(self) + [
            {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal'},
                'text': f"今日统计：{daily['date']} · UTC偏移{daily['utc_offset_minutes']}分钟。源删除确认字节不代表回收站已清空或实际容量已释放。"},
            {'component': 'VRow', 'content': cards},
            {'component': 'VCard', 'content': [{'component': 'VCardTitle', 'text': '302成功率与解析耗时'},
                {'component': 'VDataTable', 'props': {'headers': [{'title': title, 'key': key} for key, title in
                    [('period','周期'), ('requests','有效媒体请求'), ('success_rate','成功率'), ('cache_hit_rate','直链缓存命中率'), ('mean_resolve_seconds','非缓存成功平均解析秒数')]], 'items': performance}}]},
            {'component': 'VCard', 'content': [{'component': 'VCardTitle', 'text': '分享分组快照'},
                {'component': 'VDataTable', 'props': {'headers': [{'title':k,'key':k} for k in ('id','label','state','media_ids','error')],
                    'items':[self._service.groups.public(row['id']) for row in self._service.db.all('SELECT id FROM share_groups ORDER BY id DESC LIMIT 100')], 'items-per-page':10}}]},
            {'component': 'VCard', 'content': [{'component': 'VCardTitle', 'text': '持久任务队列'},
                {'component': 'VDataTable', 'props': {'headers': [{'title': k, 'key': k} for k in ('id','kind','state','attempts','error')], 'items': self._service.jobs.list(), 'items-per-page': 10}}]},
            {'component': 'VCard', 'content': [{'component': 'VCardTitle', 'text': '虚拟媒体浏览器（最近200条）'},
                {'component': 'VDataTable', 'props': {'headers': headers, 'items': items, 'items-per-page': 20}}]},
            {'component': 'VCard', 'content': [{'component': 'VCardTitle', 'text': '任务日志'},
                {'component': 'VDataTable', 'props': {'headers': [{'title': k, 'key': k} for k in ('id','media_id','operation','state','detail')], 'items': data['tasks'], 'items-per-page': 10}}]},
            {'component': 'VCard', 'content': [{'component': 'VCardTitle', 'text': '播放统计'},
                {'component': 'VDataTable', 'props': {'headers': [{'title': '指标', 'key': 'name'}, {'title': '值', 'key': 'value'}], 'items': data['metrics']}}]},
        ]
