# 账号状态与容量观察

Dashboard和MoviePilot页面只读取内存观察缓存，不随每次渲染查询115。新初始化/重启为UNCHECKED，不把旧进程的认证结果冒充当前账号状态。缓存不保存Cookie、账号昵称、设备列表或原始上游响应。

## 检查入口

- `GET /account`：保留旧接口，立即检查登录，返回 `{"logged_in":true/false}`；无法判断返回脱敏502。
- `GET /account/status`：只读缓存，含state、logged_in、checked_at、age_seconds、stale、ttl_seconds及capacity。
- `GET /account/status?refresh=true`：观察已过期时更新；所有接口须管理密钥，密钥不能放URL。
- 健康检查也会更新过期观察。`account_status_ttl`默认300秒，允许1至3600秒。

状态为UNCHECKED、AUTHENTICATED、EXPIRED或UNKNOWN。UNKNOWN表示请求/响应不可判定，不是确定Cookie过期；容量查询失败不会把登录成功改成EXPIRED。已过TTL的历史观察明确标记stale，不宣称其仍实时有效。

## 容量与隐私

所有SDK请求仍通过唯一P115ClientManager。已安装SDK源码与 [p115client官方文档](https://p115client.readthedocs.io/en/latest/reference/module/client.html) 提供 `fs_index_info` 读取当前空间的接口；文档并未给出完整稳定响应schema。

因此解析采取有限兼容：仅接受space_info中的all_total/all_use/all_remain的明确整数或size整数，映射total_bytes/used_bytes/free_bytes。负数、布尔值、人类单位、溢出、未知字段或总额小于已用/可用值时，容量为null，状态UNAVAILABLE。不推算未知剩余空间，不把null填成0，不暴露响应里其他账号/设备字段。

这些字段兼容只完成离线契约验证，**没有真实账号响应验证**。未来获得不同响应时应先核实字段和单位，再扩展白名单，不根据名字或页面文本猜测字节数。页面展示的是上次账号报告的观察，不是逻辑媒体容量，也不证明回收站删除已使配额到账。

## 验证范围

`test_account.py`覆盖页面不访问网络、TTL/强制刷新/过期标记、未知与登录失效区分、容量失败、重启重置、快照隔离、管理鉴权、健康节流、实际安装SDK的请求参数、字段脱敏和非法容量拒绝。MoviePilot卡片只做组件结构验证；真实115响应与宿主渲染仍待联调。
