"""原版 1.9.6 的显式一次性导入；只通过插件 SDK 读写，不访问豆瓣。"""

from copy import deepcopy


class ImportRefused(ValueError):
    """可直接展示的迁移前置条件，不包含配置或异常原文。"""


class LegacyImporter:
    SOURCE = "DouBanWatching"
    RESULT = "legacy_import"
    JOURNAL = "legacy_import_journal"
    DATA_KEYS = ("data", "wait", "timestamp_schema", "archive_last_removed")
    CONFIG_KEYS = ("private", "first", "user", "exclude", "cookie",
                   "pc_month", "pc_num", "mobile_month", "mobile_num")

    def __init__(self, plugin):
        self.plugin = plugin

    @staticmethod
    def idle_config(config):
        """导入和恢复都不携带待执行动作；导入不会启动播放同步或重试。"""
        return {**config, "enable": False, "import_legacy": False,
                "retry_now": False, "retry_remove": [], "archive_action": None, "archive_item": []}

    def _write_config(self, config):
        if self.plugin.update_config(config) is False:
            raise RuntimeError("Configuration write rejected")
        if self.plugin.get_config() != config:
            raise RuntimeError("Configuration verification failed")

    def recover(self, config):
        """成功标记前中断则回滚；成功标记后仅清理日志，绝不再次复制旧数据。"""
        journal = self.plugin.get_data(self.JOURNAL)
        if not journal:
            return config
        completed = self.plugin.get_data(self.RESULT) or {}
        if completed.get("state") == "完成":
            self.plugin.del_data(self.JOURNAL)
            return config
        if not isinstance(journal, dict) or set(journal.get("data", {})) != set(self.DATA_KEYS):
            raise RuntimeError("Invalid import recovery journal")
        for key, value in journal["data"].items():
            if value is None:
                self.plugin.del_data(key)
            else:
                self.plugin.save_data(key, value)
        if any(self.plugin.get_data(key) != value for key, value in journal["data"].items()):
            raise RuntimeError("Import rollback verification failed")
        restored = self.idle_config(journal["config"])
        self._write_config(restored)
        self.report("失败", "上次导入未完成，已恢复导入前的数据和配置；可重新导入")
        self.plugin.del_data(self.JOURNAL)
        return restored

    def report(self, state, message, **counts):
        result = {"state": state, "message": message, "source": self.SOURCE,
                  "finished_at": self.plugin._now(), **counts}
        self.plugin.save_data(self.RESULT, result)
        if self.plugin.get_data(self.RESULT) != result:
            raise RuntimeError("Import result verification failed")
        return result

    def _source(self):
        if self.plugin._legacy_source_active():
            raise ImportRefused("请先停用原版插件，再执行导入")
        if self.plugin.get_data("processed", plugin_id=self.SOURCE):
            raise ImportRefused("检测到更早版 processed 数据，暂不支持直接导入；请保留原数据")
        values = {key: self.plugin.get_data(key, plugin_id=self.SOURCE) for key in ("data", "wait")}
        if all(value is None for value in values.values()):
            raise ImportRefused("未找到原版 data / wait 数据，请确认旧数据仍在此 MoviePilot 数据库中")
        if any(value is not None and not isinstance(value, dict) for value in values.values()):
            raise ImportRefused("原版 data / wait 不是支持的字典格式，未进行导入")
        settings = self.plugin.get_config(plugin_id=self.SOURCE)
        if settings is not None and not isinstance(settings, dict):
            raise ImportRefused("原版配置格式异常，未进行导入")
        return deepcopy({key: value or {} for key, value in values.items()}), deepcopy(settings or {})

    def run(self, config):
        """预检 → 记录恢复点 → 复制并验证 → 提交结果；失败使用同一恢复流程。"""
        result = self.plugin.get_data(self.RESULT) or {}
        if result.get("state") == "完成":
            return config, "跳过", "已经完成原版导入，不会重复执行"
        try:
            if any(self.plugin.get_data(key) not in (None, {}) for key in ("data", "wait", "manual_matches")):
                raise ImportRefused("新插件已有档案或待重试记录或人工匹配，未覆盖现有数据")
            source, settings = self._source()
        except ImportRefused as error:
            message = str(error)
            self.report("未执行", message)
            return config, "跳过", message
        except Exception as error:
            message = f"读取原版数据异常（{type(error).__name__}），未进行导入"
            self.report("失败", message)
            return config, "失败", message
        target_config = self.idle_config({**config, **{key: settings[key] for key in self.CONFIG_KEYS if key in settings}})
        journal = {"config": self.idle_config(config), "data": {
            key: self.plugin.get_data(key) for key in self.DATA_KEYS}}
        try:
            self.plugin.save_data(self.JOURNAL, journal)
            if self.plugin.get_data(self.JOURNAL) != journal:
                raise RuntimeError("Import journal verification failed")
            for key, value in source.items():
                self.plugin.save_data(key, value)
            if any(self.plugin.get_data(key) != value for key, value in source.items()):
                raise RuntimeError("Archive verification failed")
            self._write_config(target_config)
            # 新安装时已存在空库时间标记，导入旧记录后仍须执行统一的日期迁移。
            self.plugin.del_data("timestamp_schema")
            self.plugin._migrate_timestamps()
            count, pending = len(source["data"]), len(source["wait"])
            unknown = sum(not isinstance(item, dict) or item.get("status") not in {"do", "collect"}
                          for item in source["data"].values())
            fields = sum(key in settings for key in self.CONFIG_KEYS)
            message = f"已导入 {count} 条档案、{pending} 条待重试、{fields} 项配置；{unknown} 条档案未保存明确状态。请检查后启用插件"
            self.report("完成", message, records=count, waiting=pending, unknown=unknown, config_fields=fields)
        except Exception as error:
            # 只输出异常类型。恢复日志留在数据库，进程中断后也能由下次初始化继续回滚。
            restored = self.recover(config)
            message = f"导入异常（{type(error).__name__}），已恢复导入前的数据和配置"
            self.report("失败", message)
            return restored, "失败", message
        # 成功结果已持久化；这里中断也不会回滚已完成的导入。
        self.plugin.del_data(self.JOURNAL)
        return target_config, "成功", message
