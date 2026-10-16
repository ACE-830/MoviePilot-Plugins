# MoviePilot-Plugins

MoviePilot 第三方插件仓库，适用于 **MoviePilot V3**。

## 插件列表

| 插件 | 版本 | 说明 |
| --- | --- | --- |
| [豆瓣书影音档案Plus](plugins.v3/doubanwatchingplus/) | 1.1 | 同步豆瓣观影状态，管理本地档案并展示观影时间线。 |

### 豆瓣书影音档案Plus

基于 [「豆瓣书影音档案」原版插件](https://github.com/honue/MoviePilot-Plugins/tree/main/plugins/doubanwatching)进行 V3 适配并扩展功能，使用独立插件 ID `DoubanWatchingPlus`。

本插件作者：[**ACE-830**](https://github.com/ACE-830)。原版插件作者：[**honue**](https://github.com/honue)。

- **播放同步**：Emby 停止播放或标记已播放时，根据续播和已播放状态同步豆瓣；开始播放仅记录日志。
- **档案管理**：支持状态统计、搜索、多选及批量编辑；移除档案时同步取消豆瓣标记。
- **手动匹配**：搜索豆瓣条目并预览海报，人工确认的匹配优先采用。
- **失败重试**：展示失败原因和累计次数，支持自动及手动重试。
- **首页卡片**：按月份展示近期观影档案，适配桌面和手机。
- **原版导入**：支持导入 honue 原版插件的配置、档案和失败记录。

## 更新日志

- **v1.1**：新增档案管理页，支持状态统计、搜索多选、批量编辑与移除、手动匹配及海报预览，统一展示手动操作结果。
- **v1.0**：适配 MoviePilot V3，优化媒体匹配、失败重试与日志，Emby 同步跟随用户续播和已播放状态。

## 鸣谢

- [jxxghp/MoviePilot](https://github.com/jxxghp/MoviePilot)：MoviePilot 主程序。
- [jxxghp/MoviePilot-Plugins](https://github.com/jxxghp/MoviePilot-Plugins)：官方插件开发规范。
- [honue/MoviePilot-Plugins](https://github.com/honue/MoviePilot-Plugins)：豆瓣书影音档案原插件（v1.9.6），作者 honue。

## 许可证

遵循 [GPL-3.0](LICENSE) 协议，保留上游作者署名与许可证。
