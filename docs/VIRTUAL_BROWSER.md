# 虚拟目录与全库搜索

管理API提供两种视图，始终读取统一MediaObject数据库，不重新查询网盘、不读取视频、不暴露播放token/分享密码/直链。

## 全库媒体查询

```http
GET /media?prefix=/Movies/&q=movie&storage=SHARE&status=READY&limit=50&offset=0
```

- 路径相对独立服务 `/p115tool` 或MoviePilot插件API前缀，必须管理密钥。
- prefix按目录边界匹配：`/Movies`和`/Movies/`等价，不会匹配`/MoviesExtra/`；默认为根。
- q在标题、文件名、虚拟路径中做字面子串匹配，不把 `%`、`_` 或引号当SQL通配符/代码。SQLite lower对ASCII不区分大小写，不宣称完整Unicode大小写折叠。
- storage可选NORMAL/SHARE/CACHE；status只接受已定义的状态。
- limit最大200，offset至少0。返回total是过滤后的全部媒体数，不是当前页长度；搜索不限于插件页面最近200条。

## 即时目录层级

```http
GET /media/tree?prefix=/Shows/&limit=50&offset=0
```

返回当前目录的直接子目录和文件，不一次递归输出全库。目录先于文件，名称排序；total是过滤后当前层级的条目数。进入子目录时用该条目的path再次请求；响应parent用于返回上级，根目录parent为null。

每条含稳定key、name、path、is_directory、media_count、bytes及NORMAL/SHARE/CACHE计数。文件条目另有media_id，可通过 `/media/{id}` 读取信息。目录不存在也正常返回空列表，不发网盘请求。

q/storage/status也适用于树：目录聚合的是其匹配后代，不是未过滤的全部容量。原个人盘文件删除后的SHARE媒体仍参与虚拟目录和容量统计。

bytes是逻辑媒体字节，不是115实际配额占用或已经释放的物理容量。缓存与同一媒体的分享后端不能据此重复相加。异常计数取BROKEN或FAILED_*状态，不表示已经执行深度播放验证。

## 验证范围

`test_browser.py`覆盖字面匹配、目录边界、全库搜索、目录聚合与分页、虚拟分享源删除后可见性、无效条件、静态路由优先和敏感字段隔离。当前完成的是后端搜索/树API；MoviePilot页面仍为最近媒体表格，完整搜索与树导航控件尚需开发和真实宿主渲染验证。
