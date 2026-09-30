# 分享分组与安全屏障

## 策略

`share_strategy`：

- `file`：每个媒体文件一个分享。
- `movie`：有 MOVIE 类型和 TMDB ID 的媒体，按电影身份归组。
- `season`：有 TV 类型、TMDB ID 和季号的媒体，按剧集身份＋季号归组。
- `auto`（默认）：电影按电影、剧集按季，缺少识别信息时退回单文件。不会仅因文件同目录便猜测属于同一作品。

MoviePilot整理事件会提取媒体类型、TMDB和季号并入库。原生事件的真实负载仍待宿主联调；未知字段不会用文件名猜测。手动设置：

```http
POST /media/metadata
{"media_id":1,"media_type":"TV","tmdb_id":123,"season":1}
```

普通导入 `/media/import` 也接受可选 `media_type` 和 `season`。

## 自动路径

- 完整扫描：先枚举并导入全部媒体，扫描成功后按策略冻结分组，不在分页中途开始删除。
- 整理事件：先入库，提交延迟 `archive_policy` 任务。默认等待 `group_settle_seconds=300` 秒，处理时收集当前已知、仍有个人盘源文件、匹配 SHARE 存储策略的同组成员。
- 等待窗口并不能证明“一季已经下载完”。要求一次分享包含整季时，应等全部文件完成后手动提交明确成员列表。
- 已释放的历史成员不会被自动转存进新分享；后续到达的文件形成新一代不可变快照，旧媒体token及分享记录继续保留。
- 自动删除同时检查当前配置和**每个成员的提交授权快照**。任何成员未授权，整组只归档、不删除。后续另一集授权不能追溯删除以前未授权的成员。

## 手动分组

```http
POST /archive/group
{"media_ids":[1,2,3]}
```

可以通过 `/jobs` 提交 `archive_group` 后台任务。

删除须明确全组确认，ID按升序、英文逗号连接：

```json
{"media_ids":[1,2,3],"delete_source":true,"confirmation":"DELETE_GROUP:1,2,3"}
```

队列形态：

```json
{"kind":"archive_group","payload":{"media_ids":[1,2,3],"delete":true},"confirmation":"DELETE_GROUP:1,2,3"}
```

个人盘源删除开关仍默认关闭，所有操作受SHA1、大小、名称、下载URL、Range和STRM门槛保护。没有真实网盘写操作被本项目测试执行。

## 不可变快照与删除屏障

SQLite schema3新增媒体识别信息、分享组、冻结成员、当前成员关系和逐媒体自动删除授权。既有数据库自动升级，token不变。

```text
PLANNED → CREATING → SHARED → VERIFYING → VERIFIED → [DELETING] → READY
```

1. 冻结每个成员的源file_id、名称、大小、SHA1。
2. 以逗号分隔file_ids一次创建分享；立即保存返回的分享码/接收码。
3. 使用已核对的 `share_update(share_duration=-1)` 接口请求长期分享，失败则停止，不删除源文件。不能假设share_send接受同名参数就一定永久。
4. 每个成员独立读取并匹配分享文件，再验证下载URL和1字节Range。
5. 为全部成员生成并读取校验稳定STRM。
6. **所有成员均通过**才跨过删除屏障。任一成员失败，源文件全部保留。
7. 批量删除内部仍逐个重读源与分享身份；不代理视频、不修改已有token。

同一快照重复提交复用原分享，不重复创建。完全重复的名称＋大小＋SHA1在同一分享内无法唯一匹配时，拒绝分组，应单独归档。不能用新请求随意拆分已经属于现有快照的成员，以免绕过组安全门。

组操作不具有远端事务：删除第一个成员后出现错误，后面的成员会保留，不尝试“继续删完”。分享本身可能随源删除失效，这必须用可丢弃文件实测，分享不是备份。

## 故障恢复

`GET /share/groups` 和MoviePilot页面提供分组状态，响应不会泄漏接收码/分享码。

分享创建已生效却超时：

```http
POST /share/group/attach
{"group_id":1,"share_code":"existingcode","receive_code":"abcd"}
```

接口先匹配全部冻结成员，再接入并继续验证；不会重新创建未知结果的分享。

部分删除已生效却超时：

```http
POST /share/group/reconcile
{"group_id":1}
```

逐成员查询删除结果和播放安全门，仅对账、不发删除。确认READY后若仍要删除剩余源，可再次显式提交带全组确认的归档操作。通用任务retry不能绕过组对账。

## 验证

分组测试覆盖单分享多成员、movie/season隔离、缺元数据回退、Range或STRM失败时零删除、创建超时attach、部分删除对账、延迟队列聚合、成员授权快照和进程重启。SDK离线契约测试核对了真实安装SDK的多file_ids发送和长期分享更新参数；没有证明115服务器会在源文件删除后继续提供分享内容。
