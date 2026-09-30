# MoviePilot 宿主契约与安装验收

更新时间：2026-09-30（Asia/Shanghai）。此文记录阶段1开发证据，不能替代真实插件安装验收。

## 测试与部署边界

目标宿主为MoviePilot v2.15.6。真实安装和运行验收由部署者执行；离线测试不证明真实宿主兼容性或115操作已通过。公开文档不记录测试环境的地址、主机名或登录信息。

前端独立构建版本、容器 Python 版本和运行时依赖尚未取得；页面显示的主程序版本不能证明前端提交版本。GitHub 连接的身份已确认为 Rst307；仓库名称、正式分支和实际上传尚未完成。

## 兼容矩阵

| 项目 | 审计目标／证据 | 真实验收 |
|---|---|---|
| 主程序 | 实例显示 v2.15.6；[对应后端基类](https://github.com/jxxghp/MoviePilot/blob/v2.15.6/app/plugins/__init__.py)与[API注册源码](https://github.com/jxxghp/MoviePilot/blob/v2.15.6/app/api/endpoints/plugin.py) | 版本已观察；本插件待安装 |
| 前端 | [v2 FormRender](https://github.com/jxxghp/MoviePilot-Frontend/blob/v2/src/components/render/FormRender.vue)、[PageRender](https://github.com/jxxghp/MoviePilot-Frontend/blob/v2/src/components/render/PageRender.vue)和配置／详情弹窗源码 | 实际前端构建待确认 |
| Python | 插件要求3.12+；开发环境3.12.14 | 容器运行时待确认 |
| SDK | 固定 p115client 0.0.9.7.2；离线参数契约已测试 | 与宿主其他插件的依赖冲突待检查 |
| 安装结构 | `package.v2.json`＋`plugins.v2/p115tool/`；类名 P115Tool，目录小写 | 用户安装待验收 |
| UI | 现有Vuetify原生表单与静态操作；复杂动态管理将采用Vue联邦 | 自由输入和复杂恢复表单未交付 |
| 其他主版本 | 索引限制 `>=2.15.6,<3`，`v3:false`，限定当前审计目标 | 不宣称V3兼容 |

索引版本范围用于限制安装候选，不表示范围内所有版本都已验证。参考[官方仓库指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Repository_Guide.md)。

## 输入、刷新和鉴权契约

FormRender 的 `model` 绑定将组件更新值原样写入表单；`VTextField type=number`不能据此保证提交整数。插件仅在宿主配置入口转换有界十进制整数字符串，随后调用严格 Config 校验。字符串布尔值继续拒绝，JSON重复键拒绝；核心和独立API校验不放宽。

PageRender 的事件闭包只发送声明的固定params；成功触发action刷新，不能读取自由输入字段。原生自由搜索／接入／续做将使用[宿主支持的Vue联邦合同](https://github.com/jxxghp/MoviePilot-Frontend/blob/v2/docs/module-federation-guide.md)。Config接收initialConfig并emit save，宿主负责保存；Page使用注入api和bear登录态，请求采用相对路径。Vue模式下页面必须自行刷新数据，宿主action不保证重取插件数据。

管理路由保留激活超级管理员依赖；nonce只用于拒绝过期页面。没有管理员认证模块时不暴露原生动作。未登录／普通／停用用户的离线拒绝测试不证明实际宿主访问控制已验收。

## 配置和重载保护

- 先校验候选配置，再停止运行服务。无效配置保留原服务、页面nonce、队列及已接受配置，并显示固定脱敏提示。
- 同一数据库不能双开。有效替换先等待消费者及在途API完成；创建新实例失败时尝试恢复原配置和服务。恢复也失败则停用并保留固定错误，不声称仍可服务。
- 缺省管理／Webhook密钥在重载时保留。媒体token、任务及授权快照存SQLite，不因配置重载重新生成。
- 停止服务清理直链缓存并关闭统一客户端门，释放数据目录所有权。固定SDK无每客户端session关闭API，不关闭其他插件可能共享的全局传输池。
- 重载后等待中的旧播放请求在取得维护锁后复查服务状态，不能继续使用已关闭客户端。

`test_host_lifecycle.py`覆盖上述配置、持久性、在途请求和资源停止行为。`test_native_package.py`将实际目录包解压，以最小宿主基类／事件桩在独立子进程中导入`app.plugins.p115tool`，检查禁用初始化、表单和API路径；这仍是离线宿主模拟。

## 原生交付包

构建：` .\.venv\Scripts\python.exe tools/build_moviepilot.py `。

输出位于`dist/moviepilot/`：

- `p115tool_v0.1.1.zip`：根目录直接是插件源文件，部署到宿主`app/plugins/p115tool/`。
- `repository/`与`p115tool_repository_v0.1.1.zip`：仓库根为索引，源码在`plugins.v2/p115tool/`。此结构用于后续GitHub main分支安装仓库。
- `manifest.json`：源文件及压缩包SHA256。

构建仅包含插件代码、依赖声明及辅助静态资源；不包含Cookie配置、数据库、STRM、虚拟环境或测试日志。输出中有未知／旧文件时拒绝生成，使用新输出目录，不自动删除。ZIP可复现。本轮仍为静态原生页面开发包；Vue联邦实现后将更新产物与发布方式，不能把本包当作完整发布版。

用户验收应先安装禁用状态，核对Python和依赖，再保存无删除配置并打开配置／详情页；验证启停、重启持久性及访问控制。无需启动8765或独立FastAPI。真实扫描、分享、删除与Emby验收仍按REMAINING_PLAN的风险门执行。

## 当前剩余

0.1.2原生404兼容修复包位于`dist/moviepilot-0.1.2/`。同步／异步管理员鉴权均支持，内部导入失败改为固定错误；全量264项、前端3项与构建通过。实际实例404原因仍待部署后核实，证据及边界见[NATIVE_404.md](NATIVE_404.md)。

阶段1真实安装、宿主配置保存／重读、启停／重启、页面与管理员访问控制均待验收。阶段2Vue联邦动态管理界面待实现，后续阶段保持原计划；不能宣称任一阶段全部完成。
