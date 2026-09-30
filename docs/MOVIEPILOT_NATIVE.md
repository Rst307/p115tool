# MoviePilot原生插件交付

交付主体是`app/plugins/p115tool`下的`P115Tool`插件，由MoviePilot加载、保存配置、运行周期任务和注册API。无需启动独立FastAPI或8765演示服务；此前的离线演示服务已关闭，独立网页不再作为插件详情页主入口。

## 原生页面交互

插件`get_page()`返回Vuetify组件与PageRender事件，操作在MoviePilot插件详情页中完成。依据 [宿主PageRender源码](https://github.com/jxxghp/MoviePilot-Frontend/blob/v2/src/components/render/PageRender.vue)，组件事件使用api/method/params，执行后触发页面刷新。本项目不在页面中嵌入管理API Key或播放token。

当前已有目录根/父级/子目录导航、存储筛选、20项分页、逐媒体生成STRM/保留源归档/自动整理/恢复缓存、专属只读远端对账，以及待执行任务取消与安全任务重试。按钮点击即提交，任务使用后台持久队列，不由浏览器代理视频。

删除请求账本在同一原生详情页展示最近20条，不需跳转浏览器或启动另一个服务。账本不是回收站列表，尚不能永久清理。

扫描按钮显式携带allow_delete=false；归档按钮显式携带delete=false。委托MoviePilot整理遵守开关，使用宿主规则；媒体详情显示宿主结果状态，旧整理对账只读。缓存清理仍受分享验证、所有权和租约保护。没有增加一键删除源或盲目重试远端写操作的按钮。

## 宿主认证边界

原生动作由插件get_api返回相对路径`/native/action`，设置allow_anonymous=false和auth=bear。PluginManager添加`/P115Tool`，API注册器添加`/api/v1/plugin`，最终路径为`/api/v1/plugin/P115Tool/native/action`。依据 [MoviePilot插件API注册源码](https://github.com/jxxghp/MoviePilot/blob/v2/app/api/endpoints/plugin.py)，宿主会注入Bearer校验。本路由额外保留Depends管理员依赖，检查超级管理员及激活状态；不能只靠页面按钮隐藏来保护操作。

无宿主认证模块时不注册原生动作，独立create_app也不注册。管理API仍需原独立密钥，两套鉴权不互相替代。动作使用有界Request-only解析，错误不回显原始字段。页面随机nonce在插件重新初始化后变更，旧页面不能提交新实例操作；它不是管理密钥。多个管理员共享同一插件实例的目录视图，当前视图仅保留在内存。

## 验证与剩余工作

目标实例已观察为MoviePilot v2.15.6。原生目录包、仓库索引、表单数字规范化及重载保护见[宿主兼容记录](HOST_COMPATIBILITY.md)。用户自行测试安装；真实插件验收仍待完成。已确认PageRender按钮只发送固定params，自由输入及复杂恢复采用宿主支持的Vue联邦路线，不能给现有按钮添加输入框后假定绑定成功。

test_native_ui覆盖带管理员依赖的原样FastAPI注册、未登录/普通/停用账户、先认证后解析、错误脱敏、旧页面、额外删除参数拒绝、保留源归档、扫描不继承自动删除授权、活跃任务合并及未知远端写不重试。使用真实SQLite和模拟宿主身份依赖，并非实际MoviePilot运行验收。

原生自由搜索输入、复杂分享接入/显式续做表单、源删除二次确认及完整分组界面仍需继续实现。原生get_form/get_page的实际宿主渲染、数字字段序列化及目标MoviePilot版本兼容未验证；不能用此前独立网页截图替代原生页面验收。
