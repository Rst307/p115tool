# 分享导入自动虚拟整理

2026-10-02，0.2.30。

用户要求粘贴115分享链接即可提取视频、自动由MoviePilot虚拟整理、生成STRM并加入虚拟存储；沿用此前明确要求，导入不立即转存，播放时才创建临时副本。

## 接口边界

现有host_transfer.organize负责个人网盘中的真实目录移动。分享文件不是宿主115存储中的个人文件，不能把分享ID伪装成个人文件ID提交移动任务。虚拟整理新增一个小型宿主适配模块virtual_organize，由分享导入工作线程调用，不创建额外服务、任务队列或整理数据库。

MediaChain.recognize_by_path接收分享相对路径，返回meta_info及media_info；识别结果中的季集、媒体类型和类别用于虚拟输出。TransHandler.get_naming_dict和get_rename_path使用宿主RENAME_FORMAT及命名事件构建路径，Service负责原有的STRM原子生成与迁移。宿主只做识别和命名，不承担115分享转存；所有115读写仍经P115ClientManager。

接口参考官方MoviePilot v2.15.6：

- https://github.com/jxxghp/MoviePilot/blob/v2.15.6/app/chain/media.py
- https://github.com/jxxghp/MoviePilot/blob/v2.15.6/app/modules/filemanager/transhandler.py

## 数据和失败处理

分享相对目录提供识别上下文；不向宿主传递分享链接、提取码、Cookie或播放token。识别和命名异常统一为固定错误，不将原始异常载荷返回界面。类型、类别、模板输出、季集信息和扩展名需通过路径校验。

虚拟路径可以使用标准片名，源name／size／SHA1／share_fid保持原始分享身份，避免因重命名破坏播放核实。重复导入保持media_id及token；模板变更导致路径变化时使用既有输出归属检查与迁移检查点。已有副本不重转存、不删除。输出冲突保留原STRM并计入失败。

逐文件识别失败计入PARTIAL，不生成假成功的未识别输出，也不终止其他视频导入。重新导入相同链接可重试未完成文件；宿主缺失、识别不可用及普通电视剧无法确定集数都明确失败。无集数季ISO保留宿主剧名目录与Season目录、原ISO名。此入口没有宿主真实移动任务或整理历史；正常个人网盘移动整理仍由原入口提交。

## 验证范围

新增模拟宿主接口测试覆盖电影规范目录、电视剧自定义模板及季集、ISO、路径越界和命名事件非法结果、识别失败与重导入、已有副本及播放地址保留、STRM路径迁移、同名冲突保护，以及真实Service播放入口按原分享身份首次转存。没有用真实115资源测试删除；宿主实际媒体识别结果和115分享可用性仍取决于部署环境。
