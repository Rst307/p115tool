# 识别与自动整理路径预览

预览接口不会创建远端目录、move/rename、生成分享或删除源文件；返回 `execution_ready=false`，表示只完成识别/路径规划，不能证明远端目标可写。实际执行已由独立的自动整理入口接入，见 [AUTO_ORGANIZE.md](AUTO_ORGANIZE.md)。真实MoviePilot识别及115运行验收仍未完成。

## 接口

```http
POST /organize/preview
X-API-Key: <management key>

{"media_id":1}
```

只支持保留的NORMAL源，先复查名称/大小/SHA1；分组媒体和未完成整理计划拒绝。管理接口返回识别身份、源ID及目标name/virtual_path/relative_parent，不暴露播放token或直链。

识别适配依据 [MoviePilot MediaChain源码](https://github.com/jxxghp/MoviePilot/blob/v2/app/chain/media.py)，调用路径识别并关闭图片获取。读取返回上下文的media_info/meta_info，不在本插件内用文件名猜造TMDB身份。当前只对宿主协议做模拟测试，没有运行实际MoviePilot识别服务。

## 模板

配置 `organize_templates`（宿主表单使用organize_templates_json），必须同时有MOVIE和TV。默认：

```json
{
  "MOVIE":"电影/{title} ({year})/{title} ({year}){ext}",
  "TV":"电视剧/{title} ({year})/Season {season:02d}/{title} - S{season:02d}{episode_tag}{ext}"
}
```

模板只能使用title/year/tmdb_id/season/episode/episode_tag/ext；数字字段支持02d、03d格式。episode_tag由经过校验的集数生成，例如E01或E01-E03，不直接信任上游标签字符串。禁止属性访问、下标、conversion、任意格式表达式、绝对路径及穿越；扩展名必须保留在末尾。渲染后再次做便携路径/长度/扩展名和已有虚拟目标碰撞检查。

上游标题里的路径分隔符与Windows非法字符替换为下划线；控制字符、无效标题拒绝，保留中文。目录不是直接拼接上游未处理文本。

## 识别门槛

需要明确电影/电视剧类型、标题、年份和有效TMDB ID。电视需要明确单季及开始集，季0可作为特别篇。同季连续多集读取MoviePilot的begin_episode/end_episode并完整保留范围（如S01E01-E03），字段依据 [MoviePilot MetaBase源码](https://github.com/jxxghp/MoviePilot/blob/v2/app/core/meta/metabase.py)。结束集小于开始集、非整数、超出1–9999、跨季或没有明确开始集的文件拒绝，不把合集推断成单集。

旧的自定义单集模板仍可用于单集文件；整理多集文件必须在**文件名部分**使用episode_tag，不能只在父目录保留范围。默认模板已支持该标签，单集输出不变。现有冻结计划不会自动改目标。非TMDB身份、跨季合集及离散集数列表仍不支持自动重命名。

独立服务环境没有MoviePilot时返回脱敏的不可用错误；不偷偷启用弱识别或联网猜测替代宿主身份。

## 验证

`test_organizer_preview.py`覆盖方案中的电影路径、TV/特别篇、连续集数范围、标签重建/旧模板拒绝、标题净化、模板表达式/路径拒绝、识别失败及跨季拒绝、源身份/碰撞、宿主调用参数和API鉴权/脱敏。远端客户端与MoviePilot识别上下文均为可控模拟；真实识别准确性和宿主模板表单渲染尚待验证。
