# 回收站策略验收范围（尚未实现）

原方案第30节的三个选项保持不变：仅删除至回收站（默认）、N天后清理、自动清空。本文是实现约束，不是功能完成说明。

## 固定版本SDK核对

本地p115client 0.0.9.7.2提供recyclebin_list（webapi.115.com/rb）、recyclebin_clean（rb/secret_del）和recyclebin_revert。已用inspect读取签名和文档，未执行远端请求。

clean未指定tid会清空整个回收站，不能传入空批次。安全密钥默认000000，不可假设它是用户的密钥。在线latest同时有OpenClient接口，不能替代固定版本契约。参考 [SDK文档](https://p115client.readthedocs.io/en/latest/reference/module/client.html) 和 [批量清理说明](https://p115client.readthedocs.io/en/latest/reference/tool/edit.html)。没有真实响应样本，条目ID和原文件ID对应关系尚未验证。

## 必需交付

- 删除前持久记录插件操作、原文件身份和时间，保存超时/对账结果；源缺失不代表拥有回收站条目。
- N天策略只处理确证条目和删除时间，排除未知来源；分页完整，测试时间边界。
- 自动清空是账户级不可逆操作，必须明示范围并独立授权，不能由普通删除或归档开关隐式启用。
- 永久清理先保存检查点；超时不盲目重复。重启默认只读对账。
- 密钥/Cookie不得写入日志、错误或卡片；测试必须覆盖空批次拒绝、三种策略、未授权、分页失败、未知结果和重启。
- 真实永久删除仅允许用户明确授权的可丢弃文件。

当前实现了schema7删除请求账本：源文件/恢复缓存删除前保存原ID、父目录、名称、SHA1、大小、用途和时间；意图提交后才发115请求。SDK成功记为ACKNOWLEDGED，异常保持REQUESTED，不再发第二次同ID未知请求。

源/缓存专属对账将结果记为ABSENT或RETAINED。RETAINED必须与原意图完整身份相符，包括父目录；已移动、替换或改名的文件不能清除未知意图。旧库不会补造历史删除记录，原ID和播放token保持不变。

MoviePilot原生详情页展示最近20条；管理API为`GET /recycle/intents`（有鉴权、分页）。这些记录**不是回收站条目所有权证明**：ABSENT只说明源查询已缺失，ACKNOWLEDGED只说明SDK收到成功响应。账本没有回收站条目ID，也没有永久清理请求。N天/自动清空策略仍未实现。
