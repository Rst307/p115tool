# MoviePilot整理预览（0.1.5）

/organize/preview调用MoviePilot的manual_transfer(preview=True, background=False, transfer_type=copy)。插件只返回宿主预览的source、target、target_dir、success白名单，不渲染模板，不返回宿主错误消息或任意媒体元数据。预览不写入整理提交记录。

插件整理根目录和模板字段已移除；旧配置字段被忽略。规则统一在MoviePilot配置，需保证115宿主账户与插件账户对应，扫描prefix为真实115路径。路径查询的fileid与插件源身份不一致时拒绝提交。

旧版本的模板、连续多集命名测试已由宿主预览／委托契约测试替代；连续多集、跨季及分类行为以MoviePilot实现和真实部署结果为准。
