# 原生页面读取失败：HTTP 404

## 0.1.3：重复插件ID前缀修复

[MoviePilot v2.15.6 PluginManager.get_plugin_apis](https://github.com/jxxghp/MoviePilot/blob/v2.15.6/app/core/plugin.py)
先为插件返回的路径添加`/P115Tool`，API注册器再添加`/api/v1/plugin`。
0.1.1／0.1.2插件返回的路径已包含`/P115Tool`，最终形成
`/api/v1/plugin/P115Tool/P115Tool/native/data`，与前端请求不匹配。
这是明确的宿主契约错误，原离线测试遗漏了PluginManager的前缀阶段。

0.1.3让所有宿主接口返回插件相对路径（如`/native/data`、`/play/{token}`），
保留前端及STRM中的最终请求地址。回归测试现在覆盖完整两级前缀、真实HTTP请求、
同步／异步管理员依赖及停用时的bootstrap／配置校验，保持独立服务路由不变。
补齐宿主前缀阶段后，修复前data、validate、action均为`404 != 200`，修复后通过。

配置页顶部新增独立“启用插件”开关，按钮为“保存并启动”或“保存并停止”。
字段名称与类型随前端打包，不依赖bootstrap成功才能显示。
仅在插件校验接口404时，允许走宿主原有管理员配置保存流程；初始化仍会完整校验配置，
校验失败不替换运行服务。401／403／422／500及未知错误不会触发此回退。
停用不会删除媒体数据库或源文件。更新后重新加载插件并强制刷新浏览器，
再验证最终请求路径及启停状态。

## 0.1.2排查历史及验证限制

2026-09-30（Asia/Shanghai），用户在MoviePilot v2.15.6、插件0.1.1中观察到
`POST /api/v1/plugin/P115Tool/native/data`返回404。截图证明请求到达服务器，
不能单凭该响应确定路由缺失的具体原因。

## 已复现及修复

原实现只导入`get_current_active_superuser_async`，捕获任何ImportError后返回空路由。
在仅提供同步管理员依赖的宿主模拟中，通过真实`P115Tool.get_api()`注册FastAPI，
data、validate、action三个请求均返回404。修复后优先使用异步依赖，其次使用同步依赖；
继续严格检查超级管理员和激活状态。宿主鉴权内部导入损坏或未提供管理员依赖时，
抛出固定错误，不再静默忽略；没有宿主模块的独立模式继续不提供原生接口。

复现／回归命令：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_native_auth_compat.py -v
```

修复前同步宿主用例在三个接口上均出现`404 != 200`，修复后通过。
补充覆盖异步依赖优先、内部导入异常、缺少管理员依赖以及先鉴权后解析。

前端按固定HTTP状态显示登录、权限、参数、路由和服务错误。
不展示原始响应、请求头或异常内容，不自动重试请求。
`cd frontend; npm.cmd test`验证404提示、敏感信息隔离、无重试以及宿主响应格式兼容。

## 用户实例仍待确认

[v2.15.6宿主源码](https://github.com/jxxghp/MoviePilot/blob/v2.15.6/app/db/user_oper.py)
包含异步管理员依赖。因此，同步宿主回归证明一个真实代码缺陷，
并不证明它就是用户实例404的原因。前后端版本不一致、路由注册异常及代理路径仍需排除。
本轮Computer Use无法访问用户内网地址，没有使用截图中的登录凭据。

0.1.2修复包需部署到运行中的`app/plugins/p115tool/`，包括Python文件及`dist/`。
不要删除宿主插件数据或配置。重新加载插件（必要时重启MoviePilot），强制刷新浏览器，
确认版本0.1.2后再检查`native/data`。如果仍为404，查看宿主启动／插件加载日志中
P115Tool的接口注册错误；只提供脱敏错误，不复制请求头、Cookie或播放信息。

此修复不涉及真实115文件操作，也不代表完整42节目标完成。
