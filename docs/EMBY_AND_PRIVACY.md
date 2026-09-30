# Emby原生事件与请求隐私

## 配置Emby

Emby的Webhook功能属于其Premiere功能，具体版本支持和配置入口以 [Emby官方说明](https://emby.media/support/articles/Webhooks.html) 为准。官方开发者论坛展示了 `Event`、`Item`、`Session` 等结构，并讨论了JSON与multipart内容类型：[开发者资料](https://emby.media/community/topic/96170-webhook-information/)。这里据此实现了相应格式解析，但**未对实际Emby运行实例联调**。

地址：

- MoviePilot：`https://domain/api/v1/plugin/P115Tool/emby/webhook`
- 独立服务：`https://domain/p115tool/emby/webhook`

必须发送独立Webhook密钥 `X-API-Key: <webhook_key>` 或 `Authorization: Bearer <webhook_key>`，管理密钥不能替代Webhook密钥。若实际Emby版本无法设置所需请求头，使用有访问控制的本地桥接／反向代理注入请求头；不要把密钥写在URL查询参数中。不要公开无鉴权的注入代理。

支持 `playback.start`、`playback.stop`，也接受进度、暂停和恢复事件用于延长缓存保护。未知事件忽略，测试事件返回安全确认。

```json
{
  "Event": "playback.start",
  "Item": {"Path": "/emby/Movies/Movie.mkv.strm"},
  "Session": {"Id": "session-id"},
  "Server": {"Id": "server-id"}
}
```

也支持 `Item.MediaSources[].Path` 中的本插件播放URL。网关URL必须是当前配置的同源、同接口前缀，不能从第三方URL里提取相似token。

支持的载体：

- `application/json`：直接发送JSON对象。
- `multipart/form-data`：唯一名为 `data` 的无文件part，内容是JSON。
- `application/x-www-form-urlencoded`：唯一 `data` 字段，内容是JSON。

不会处理文件上传、额外multipart部分或嵌套multipart。所有请求体（包括分块传输）限制为1MiB。

## 不同容器路径

本地STRM目录与Emby挂载不同，配置：

```json
{
  "emby_path_mappings": [
    {"emby": "/emby", "local": "/config/strm"}
  ]
}
```

`local` 必须位于本插件 `strm_dir` 内；支持POSIX和Windows Emby路径。映射按完整目录边界匹配，拒绝`..`。这里只匹配数据库保存的STRM路径，**不读取Webhook提供的任意文件**。

标题、TMDB ID、Emby Item ID不会被猜测成插件媒体ID。多个来源指向不同媒体时拒绝，未管理的媒体返回 `accepted:false`，不触碰缓存。

开始事件可记录服务器＋会话的哈希键，后续停止事件缺少Item时能用该关联定位。跨服务器同名会话不能互相操作；过旧的会话不能继续用于无Item事件。会话关联入SQLite（schema4），缓存维护会清理过旧记录。

开始、进度和停止都只更新访问时间与保护租约；停止**不立即删除缓存**。极长播放必须配置足够长的 `playback_lease`，且需要实际服务器按预期发送事件；插件不声称单一开始事件能无限延长保护。

原有归一化格式继续可用：

```json
{"event":"PlaybackStop","token":"<existing token>"}
```

## 请求隐私保护

所有导出的插件路由使用Request-only适配器，在插件自身内部校验，而不依赖独立FastAPI应用的全局错误处理器。这个机制同样覆盖MoviePilot宿主直接注册的路由。

- 鉴权先于读取和解析请求体。
- 无效JSON、body模型、路径和query返回固定422，不返回原始输入、错误上下文或JSON片段。
- 重复JSON键（例如两个相反的删除开关）和重复已知query参数拒绝。
- 1MiB上限同时检查Content-Length和实际分块读取。
- 额外／未知字段保持原有模型的拒绝策略。
- 业务错误依旧用固定400/409/502/503；未知错误不传出异常详情。
- 同一管理请求绑定同一个Service实例，配置重载不会将已鉴权的请求悄悄转向另一个数据库；关闭实例仍由可用性安全门拒绝写操作。
- 原生User信息、Session原值、Path和token不会写入任务日志；只记录归一化事件和媒体ID。
- OpenAPI仍保留请求体与query约束说明，不在多个方法之间重复操作ID。

这不处理服务器、反向代理或第三方SDK自身的访问日志。部署时仍须禁记敏感URL和请求正文，保护SQLite备份。路径中的播放token和302的签名URL本身属于访问凭证。

## 验证边界

测试覆盖直接宿主注册路由的错误脱敏、错误鉴权优先、无效JSON／参数、重复键、分块超限、OpenAPI约束、原生JSON/multipart/form、Windows挂载映射、同源限制、会话停止、跨服务器隔离及缓存租约。实际MoviePilot／Emby实例及115网盘仍未联调。
