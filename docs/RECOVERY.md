# 持久任务和中断恢复

0.1.5整理改由MoviePilot执行，旧整理检查点的resume禁止写操作；本说明中的旧整理计划仅供历史对账，当前流程见[AUTO_ORGANIZE.md](AUTO_ORGANIZE.md)。

以下路径均相对于 `/p115tool`（独立服务）或 `/api/v1/plugin/P115Tool`（MoviePilot）。管理请求必须附带独立管理密钥。

## 后台队列

SQLite schema 2 增加 jobs 和 organize_plans，schema 3 增加分享组，schema 4 增加 Emby 会话关联，schema 5 增加扫描成员和源缺失证据，schema 6 增加每日指标，当前schema7增加删除请求身份账本。旧库启动时自动迁移，原媒体 ID/token 保持不变；不补造历史删除证据，旧版本不支持降级读取新 schema。

MoviePilot TransferComplete 只校验事件并提交到队列，不发115请求；后台单消费者读取个人盘 metadata 后处理。周期扫描、健康检查和缓存清理也先提交队列，同一参数的活跃任务会合并。MoviePilot和独立服务生命周期均会启动消费者；直接构造Service的调用方可显式启动 `service.jobs.start()`。

```http
POST /jobs
X-API-Key: <management key>
Idempotency-Key: my-request-123
Content-Type: application/json

{"kind":"archive","payload":{"media_id":1}}
```

结果返回任务 ID、PENDING状态，不返回分享密码、直链或payload。后续用 `GET /jobs?limit=100&offset=0` 查询。支持 transfer、scan、health、cleanup、generate、archive、restore、auto_organize；0.1.5起旧organize写任务不再执行。

队列归档删除与同步删除一样需要显式确认：

```json
{"kind":"archive","payload":{"media_id":1,"delete":true},"confirmation":"DELETE:1"}
```

`Idempotency-Key` 在同一数据库生命周期内固定关联同一参数；同key不同参数会拒绝。整理事件以安全字段的哈希去重。周期任务只合并PENDING/RUNNING，不会因上次DONE而永远不再执行。

### 状态

- PENDING：未开始，可以取消。
- RUNNING：已在数据库记录，再执行工作。
- DONE：正常完成。
- FAILED：本地生成／健康检查失败，可以显式重试。
- NEEDS_ATTENTION：可能发生远端写入，或进程中断，必须对账。
- CANCELLED：用户取消尚未开始的任务。

重启时所有RUNNING变为NEEDS_ATTENTION，**不自动重发**。未开始的PENDING可以继续执行。关闭时等待当前SDK调用与任务退出，再释放数据目录所有权。

```http
POST /jobs/1
{"action":"cancel"}
```

仅health/generate等不会发远端写操作的任务允许通用retry，即使archive已被取消，也不能绕过这条限制：

```json
{"action":"retry","confirmation":"RETRY:1"}
```

远端任务修复后可以按对应流程重新提交一个新任务；旧NEEDS_ATTENTION记录保留审计。队列尚未提供全部任务的“一键自动补偿”。

自动删除授权在事件/扫描任务提交时快照保存，并在执行时检查当前配置。提交时未授权删除的事件，不会因为后续配置开启自动删除而追溯删除。

## 远端整理

先保存旧父目录/旧名称、目标父目录/新名称、SHA1、大小和虚拟路径，再执行：

```text
PLANNED → MOVING → MOVED → RENAMING → MAPPED → DONE
```

每次写操作之前保存检查点；超时则保留当前计划。其他导入/归档不能绕过未完成的整理计划。检查目标目录同时包含“原文件名”和“新文件名”，且fs_move显式keep_both，避免115默认覆盖已有文件。

查询并对账（默认不发任何move/rename）：

```http
POST /organize/reconcile
{"media_id":1}
```

如果实际文件已经到达目标目录且完成重命名，自动完成本地映射和STRM更新；否则仅报告观测状态。继续未完成的步骤需要明确授权：

```json
{"media_id":1,"resume":true,"confirmation":"RESUME:1"}
```

续做前再读取远端身份和碰撞检查。实际SHA1/大小/位置/名称超出原计划时拒绝。移动已经生效但返回超时不会重复move；重命名已生效但返回超时不会重复rename。

## 缓存删除

缓存删除超时停在DELETING，不能再次删除。对账：

```http
POST /cache/reconcile
{"media_id":1}
```

- 文件已不存在：深度验证分享后移除缓存映射，不重发删除。
- 文件仍在且身份/父目录一致：恢复READY，后续清理仍受播放租约保护。
- 远端返回不明确或文件身份变化：保持不确定状态，拒绝继续。

源文件删除使用已有 `/archive/reconcile`。分享创建、转存接收以及目录创建的全部不确定状态可视化操作仍需进一步完善。

## 缓存目录创建与转存接收

创建专属目录前，先完整查询缓存根目录，拒绝同名既有对象，再持久化目录创建意图。mkdir超时后再次恢复不会重发mkdir。已记录成功响应ID但后续验证失败时，可用下述只读对账接口确认目录和转存结果：

```http
POST /restore/reconcile
{"media_id":1}
```

只读是指不发任何远端写请求：不创建目录、不接收分享、不删除缓存；可以提交已确认的本地映射并更新租约。没有观察到文件返回409，保留检查点；匹配到唯一名称/大小/SHA1/父目录正确的可播放文件则完成本地缓存记录。

若mkdir连响应ID都未保存，单凭同名目录不能证明它是本插件创建（可能有外部并发创建）。先查询候选：

```http
POST /restore/folder/candidates
{"media_id":1}
```

人工确认候选确实属于本次恢复后，显式接入（仍不发远端写请求）：

```http
POST /restore/folder/attach
{"media_id":1,"folder_id":"999","confirmation":"ADOPT_FOLDER:1:999"}
```

必须已有创建意图、当前配置缓存根与意图一致、同名候选唯一、目录ID/父目录/名称复查通过。多个同名候选、目录被移动、配置改变或无检查点时拒绝；不要为了继续任务认领不属于插件的目录。

目录认领成功但未有转存结果时，仍不能靠只读对账发送接收请求。若没有转存意图，显式正常 `/restore` 才能首次提交；已有不确定转存意图且文件不可见时保持等待人工核查，不自动重复。不存在可观测目录结果时也不盲目清除意图或创建第二目录；需真实平台证据确认结果。本轮没有实现无证据强制重试开关。

## 验证范围

离线回归包括：真正的子进程 `os._exit` 后RUNNING检查点保留/文件锁释放、schema1迁移、重复事件、授权快照、worker关闭、move/rename生效后超时与恢复、缓存删除对账。远端行为仍由可控客户端模拟，不能代替真实115账号联调。
