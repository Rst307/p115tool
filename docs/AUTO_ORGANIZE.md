# MoviePilot原生整理（0.1.5）

0.1.6已改为MoviePilot正常整理后统一扫描，当前使用说明见[POST_ORGANIZE_SCAN.md](POST_ORGANIZE_SCAN.md)。下述自动委托和事件立即处理流程保留为0.1.5历史记录，不代表当前页面或扫描行为。

本说明替代0.1.4及更早版本的插件目录创建、模板渲染、move/rename执行方案。

- 自动委托开关默认关闭；关闭时仍接收宿主整理完成事件。
- 启用后扫描或媒体操作通过StorageChain核对115文件，再调用TransferChain.manual_transfer。目录、命名、类型／类别、识别、附加文件及刮削由宿主决定。
- 请求固定target_storage=u115、transfer_type=copy、background=True、force=False。复制保留原文件，插件不会调用115整理写操作；需在MoviePilot配置115账户及媒体库规则。
- 提交前持久化SUBMITTED。宿主拒绝记FAILED，调用异常记UNKNOWN；上述状态及DONE均阻止插件重复提交。任务队列DONE只表示提交调用完成，媒体详情中的MoviePilot整理状态表示结果是否已返回。
- TransferComplete读取fileitem、transferinfo.target_item、mediainfo及meta；白名单校验115目标文件ID和路径，将目标交后台队列入库、STRM生成及分享策略。结果类别用于STRM分类，不重新推算整理目标。
- TransferFailed记录失败；不存储宿主错误详情、凭据或直链。源文件ID、存储和路径匹配后才关联委托记录。目标标记持久化，扫描不会再次整理已返回目标。
- 原文件与复制后的目标分别索引；原文件token保持。已委托原文件禁止分享归档，后续归档针对宿主返回目标，仍受原有验证与删除授权门禁控制。
- 失败、未知结果或缺失事件在MoviePilot整理历史和任务页面排查／重试。插件不猜测目标路径、不重放未知写请求；目录扫描可补偿媒体索引，但不能证明委托已完成。
- 旧organize_plans保留，/organize/reconcile仅查看远端状态，已经由外部完成的结果可更新本地映射。resume被拒绝，剩余整理在MoviePilot处理。旧/organize接口拒绝插件自行移动重命名。

## 宿主契约

核对MoviePilot v2.15.6源码：
[TransferChain及事件](https://github.com/jxxghp/MoviePilot/blob/v2.15.6/app/chain/transfer.py)、
[StorageChain路径查询](https://github.com/jxxghp/MoviePilot/blob/v2.15.6/app/chain/storage.py)。
离线桩不代替真实宿主、115账户和事件投递验收。整理预识别失败、宿主停止及未投递事件需要检查宿主历史。
