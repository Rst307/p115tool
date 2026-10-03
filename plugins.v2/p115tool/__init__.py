from dataclasses import asdict
from pathlib import Path
import logging
import threading
from .config import Config
from .service import Service
try:
    from app.plugins import _PluginBase
    from app.log import logger
    from app.core.event import eventmanager
    from app.schemas.types import EventType
except ModuleNotFoundError as exc:
    if exc.name!='app': raise
    logger=logging.getLogger('p115tool')
    eventmanager=type('OfflineEvents',(),{'register':staticmethod(lambda event: lambda fn: fn)})()
    EventType=type('OfflineEventType',(),{'TransferComplete':'TransferComplete'})
    class _PluginBase:
        def update_config(self,config): self._saved_config=config
        def get_data_path(self): return Path('./data/p115tool')

class P115Tool(_PluginBase):
    plugin_name='115 工具箱'
    plugin_desc='实际与虚拟存储、分享临时转存、分类STRM及302播放'
    plugin_icon='https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/src/assets/images/misc/u115.png'
    plugin_version='0.2.33'
    plugin_author='Rst307'
    author_url='https://github.com/Rst307'
    plugin_config_prefix='p115tool_'
    plugin_order=99
    auth_level=1

    def __init__(self):
        super().__init__()
        self._config=Config(); self._service=None
        self._lifecycle=threading.RLock(); self._initialization_error=None

    def init_plugin(self,config=None):
        with self._lifecycle:
            previous=self._config
            try:
                data=dict(config or {})
                data['data_dir']=str(self.get_data_path())
                data['playback_prefix']='/api/v1/plugin/P115Tool'
                candidate=Config.from_dict(data)
            except Exception:
                self._initialization_error='配置无效，请检查网盘目录、输出路径和播放地址。'
                logger.error(self._initialization_error); return
            self.stop_service()
            service=None
            try:
                if candidate.enabled: service=Service(candidate)
                self.update_config(asdict(candidate))
                self._config=candidate; self._service=service; self._initialization_error=None
            except Exception:
                if service: service.close()
                self._config=previous; self._initialization_error='启动失败，请检查依赖、目录权限和其他实例。'
                logger.error(self._initialization_error)
                try:
                    self.update_config(asdict(previous))
                    if previous.enabled: self._service=Service(previous)
                except Exception: self._service=None

    def get_state(self): return bool(self._service and self._config.enabled)
    @staticmethod
    def get_command(): return []
    @staticmethod
    def get_render_mode(): return 'vue','dist/assets'
    def get_api(self):
        from .api import routes as playback
        from .native_ui import routes as native
        return playback(self)+native(self)
    def get_service(self):
        if not self.get_state(): return []
        from apscheduler.triggers.cron import CronTrigger
        jobs=[]
        if self._config.source_cids:
            jobs.append({'id':'p115tool_storage_refresh','name':'115存储30分钟刷新',
                         'trigger':CronTrigger(minute='*/30',timezone='Asia/Shanghai'),
                         'func':self.run_storage_refresh,'kwargs':{}})
        for enabled,time,job_id,name,func in (
            (self._config.scheduled,self._config.scan_time,'p115tool_strm','115分类STRM生成',self.run_generate),
            (self._config.organize_scheduled and bool(self._config.organize_cids),self._config.organize_time,'p115tool_organize','115 MoviePilot定时整理',self.run_organize)):
            if enabled:
                hour,minute=map(int,time.split(':'))
                jobs.append({'id':job_id,'name':name,'trigger':CronTrigger(hour=hour,minute=minute,timezone='Asia/Shanghai'),'func':func,'kwargs':{}})
        if self._config.temp_cleanup and self._config.temp_cid:
            jobs.append({'id':'p115tool_temp_cleanup','name':'115临时副本清理',
                         'trigger':CronTrigger(minute=15,timezone='Asia/Shanghai'),
                         'func':self.run_cleanup,'kwargs':{}})
        return jobs
    def run_storage_refresh(self):
        with self._lifecycle:
            if self._service:
                try: self._service.storage.request_refresh()
                except Exception: logger.warning('115存储刷新未启动，请检查插件状态。')
    def run_cleanup(self):
        with self._lifecycle:
            if self._service and self._config.temp_cleanup and self._config.temp_cid:
                try: return self._service.storage.start('cleanup')
                except Exception: logger.warning('115临时副本清理未启动，请检查插件状态。')
    def run_organize(self):
        with self._lifecycle:
            if not self._service or not self._config.organize_scheduled or not self._config.organize_cids: return
            from .host_transfer import organize
            try:
                result=organize(self._service,[s['cid'] for s in self._config.organize_cids])
                if result['state']=='UNKNOWN' or any(item['state']=='UNKNOWN' for item in result['items']):
                    # Persist the disabled switch, never the host payload or event.
                    self._config.organize_scheduled=False
                    self.update_config(asdict(self._config))
                    logger.warning('115定时整理提交结果未知，已关闭定时整理；请核实MoviePilot任务后再手动开启。')
                elif result['state']!='SUBMITTED':
                    logger.warning('115定时整理未提交或部分被拒绝，请检查MoviePilot存储、待整理目录和任务状态。')
                return result
            except Exception:
                # Do not retry an unexpected submission outcome on the next tick.
                self._config.organize_scheduled=False
                logger.warning('115定时整理异常，已关闭本次运行的定时整理；请核实MoviePilot任务及配置。')
                try: self.update_config(asdict(self._config))
                except Exception: logger.warning('115定时整理停用配置保存失败，请手动关闭定时整理。')
    def run_generate(self):
        with self._lifecycle:
            if self._service:
                try: return self._service.start()
                except Exception: logger.warning('115 STRM任务未启动，请检查源目录配置。')
    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self,event):
        # Never retain or log host payloads: they may contain private URLs.
        with self._lifecycle:
            if not self._service: return
            try:
                data=getattr(event,'event_data',None)
                if not isinstance(data,dict): return
                info=data.get('transferinfo')
                value=lambda obj,key: obj.get(key) if isinstance(obj,dict) else getattr(obj,key,None)
                if value(info,'success') is not True: return
                target=value(info,'target_item') or value(info,'target_diritem')
                if value(target,'storage')!='u115': return
                if self._config.auto_after_transfer: self._service.request_auto_generate()
                else: self._service.storage.request_refresh(after_transfer=True)
            except Exception:
                logger.warning('115整理后自动生成未启动，请检查STRM源目录配置。')
    def stop_service(self):
        with self._lifecycle:
            if self._service: self._service.close(); self._service=None
    def get_form(self):
        data=asdict(self._config)
        return [{'component':'VAlert','props':{'type':'info'},'text':'配置115源文件夹、STRM输出目录和播放地址，然后在详情页生成STRM。'}],data
    def get_page(self):
        return [{'component':'VAlert','props':{'type':'info'},'text':self._initialization_error or '实际与虚拟存储、分类STRM和302播放，请使用原生Vue详情页。'}]
