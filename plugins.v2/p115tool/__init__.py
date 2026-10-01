from dataclasses import asdict
from pathlib import Path
import logging
import threading
from .config import Config
from .service import Service
try:
    from app.plugins import _PluginBase
    from app.log import logger
except ModuleNotFoundError as exc:
    if exc.name!='app': raise
    logger=logging.getLogger('p115tool')
    class _PluginBase:
        def update_config(self,config): self._saved_config=config
        def get_data_path(self): return Path('./data/p115tool')

class P115Tool(_PluginBase):
    plugin_name='115 工具箱'
    plugin_desc='递归生成分类STRM，115个人网盘302直链播放'
    plugin_icon='https://raw.githubusercontent.com/jxxghp/MoviePilot-Frontend/refs/heads/v2/src/assets/images/misc/u115.png'
    plugin_version='0.2.7'
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
        if not self.get_state() or not self._config.scheduled: return []
        from apscheduler.triggers.cron import CronTrigger
        hour,minute=map(int,self._config.scan_time.split(':'))
        return [{'id':'p115tool_strm','name':'115分类STRM生成','trigger':CronTrigger(hour=hour,minute=minute,timezone='Asia/Shanghai'),'func':self.run_generate,'kwargs':{}}]
    def run_generate(self):
        with self._lifecycle:
            if self._service:
                try: return self._service.start()
                except Exception: logger.warning('115 STRM任务未启动，请检查源目录配置。')
    def stop_service(self):
        with self._lifecycle:
            if self._service: self._service.close(); self._service=None
    def get_form(self):
        data=asdict(self._config)
        return [{'component':'VAlert','props':{'type':'info'},'text':'配置115源文件夹、STRM输出目录和播放地址，然后在详情页生成STRM。'}],data
    def get_page(self):
        return [{'component':'VAlert','props':{'type':'info'},'text':self._initialization_error or '仅提供分类STRM生成和302直链播放，请使用原生Vue详情页。'}]
