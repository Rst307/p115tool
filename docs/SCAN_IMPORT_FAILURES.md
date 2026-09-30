# 扫描导入失败排查（0.1.8）

目录进度显示“文件有数量、媒体=0、错误递增”，说明目录枚举已获取视频文件，但逐文件导入失败。0.1.7宿主只显示 scan · FAILED，无法据此确认具体根因。

0.1.8补充：

- 列表缺少播放提取码或SHA1时，通过统一P115ClientManager只读查询文件详情；文件ID、名称、大小、父目录及已知SHA1必须一致才允许补齐元数据。
- 支持SDK详情响应的category_id、file_sha1和category_name，以及web列表fn/fs别名，保留原目录身份。
- 宿主返回空字符串或空白分类视为未分类，不让空分类阻止正常扫描导入。
- 单文件失败显示 scan_import · 固定原因码，不打印异常原文、文件名、Cookie、分享密码、token或直链。

| 宿主原因码 | 含义 |
|---|---|
| FAILED_PATH | 虚拟路径不符合安全规则，例如英文冒号、问号、星号、尾部空格／点、保留名称 |
| FAILED_METADATA_LOOKUP | 列表元数据不全，读取详情失败 |
| FAILED_METADATA_IDENTITY | 详情与扫描快照身份或父目录不一致 |
| FAILED_PICKCODE_MISSING | 详情仍没有可用于播放的提取码 |
| FAILED_METADATA_CONTEXT | 保存的宿主识别元数据格式不正确 |
| FAILED_IMPORT_SAFETY | 入库安全校验、历史已删除身份／待处理整理检查点或非空分类校验失败 |

失败扫描不公布批处理快照，不自动提交分享、STRM或删除任务。未知写操作保护、路径安全门均保留。特殊网盘名称不会被自动改名。

离线回归复现并修复缺提取码与空分类两种导入失败，也验证详情字段、身份不符拒绝及宿主日志隐私。用户仅提供旧版通用日志，尚不能确认实机遇到的具体分支；升级后可根据scan_import原因码继续定位。未调用真实115账户或删除真实文件。

## 0.1.9：入库安全失败与历史空SHA1

FAILED_IMPORT_SAFETY说明路径和列表补齐阶段已通过，失败来自入库或宿主元数据校验。此前这一原因码仍覆盖多个分支，不能据此判断真实实例具体根因。

离线复现：历史media和normal_objects的SHA1均为空时，新列表提供完整SHA1，原入库代码将其视为内容变化。仅对未删除、NORMAL、DISCOVERED／READY、无分享／缓存映射、无待处理整理计划及未决删除账本的记录允许补齐。列表和额外只读stat必须与历史文件ID、名称、大小、父目录、提取码完全一致，stat必须提供完整40位SHA1且与列表一致。只更新原两条记录的缺失SHA1，保留ID、路径、token；已有SHA1不覆盖，分享源和未决检查点不绕过。

0.1.9新增固定原因码：

| 原因码 | 含义 |
|---|---|
| FAILED_FILE_METADATA | 入库文件基础信息或提取码不完整 |
| FAILED_SOURCE_CONTENT_CHANGED | 已记录的有效SHA1或大小与当前文件不一致 |
| FAILED_SOURCE_ALREADY_DELETED | 已删除源的身份再次出现，需对账 |
| FAILED_ORGANIZE_PENDING | 旧整理计划尚未对账 |
| FAILED_HOST_MEDIA_TYPE | 宿主媒体类型不符合MOVIE／TV／UNKNOWN |
| FAILED_HOST_SEASON | 宿主季号不是有效整数 |
| FAILED_HOST_TMDB_ID | 宿主TMDB ID不是有效正整数 |
| FAILED_HOST_CATEGORY | 非空宿主分类不符合可用路径名称规则 |
| FAILED_LEGACY_HASH_CHECK | 历史空SHA1复查发生其他已脱敏错误 |
| FAILED_LEGACY_HASH_PROTECTED | 空SHA1记录有共享后端、删除或待处理检查点保护 |
| FAILED_LEGACY_IDENTITY | 空SHA1历史身份与当前列表／详情不一致 |

原因分类只用确切本地校验消息映射到固定白名单，不输出异常原文。历史补齐完成显示scan_legacy_hash · COMPLETED及媒体ID，不显示文件名或敏感身份。仍需真实宿主升级后日志确认该实例触发哪一分支。
