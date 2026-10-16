"""豆瓣书影音档案 Plus；基于 honue 1.9.6，使用独立插件身份。"""

import re
import threading
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from app.chain.media import MediaChain
from app.schemas import Response, WebhookEventInfo
from app.schemas.types import EventType, MediaSource, MediaType
from app.sdk import scheduler as scheduler_sdk
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.media import MetaInfo, build_media_key
from app.sdk.plugin import _PluginBase, PluginManager

from . import emby, matching, posters, views
from .douban import DoubanHelper, media_description
from .log import format_message, log_event, log_recognition, operation
from .migration import LegacyImporter


@dataclass
class _SyncPlan:
    """一次状态操作的目标、关联记录及去重决定，三个入口共用。"""
    key: str
    item: dict
    record_keys: set[str]
    pending_keys: set[str]
    attempts: int
    current: dict | None = None
    skip_reason: str = ""
    keep_pending: bool = False


class DoubanWatchingPlus(_PluginBase):
    """监听播放活动，同步豆瓣状态并展示本地档案时间线。"""

    plugin_name = "豆瓣书影音档案Plus"
    plugin_desc = "适配 MoviePilot V3，同步豆瓣观影状态，支持档案统计、批量编辑与手动匹配。"
    plugin_icon = "https://raw.githubusercontent.com/honue/MoviePilot-Plugins/main/icons/douban.png"
    plugin_version = "1.1"
    plugin_author = "ACE-830"
    author_url = "https://github.com/ACE-830"
    plugin_config_prefix = "doubanwatchingplus_"
    plugin_order = 15
    auth_level = 1
    _RETRY_INTERVAL_SECONDS = 3
    _TIMESTAMP_SCHEMA = 1
    _SPECIALS_SKIP_REASON = "第0季为特别篇，暂不同步"

    def __init__(self):
        """每个插件实例独立串行处理，避免重复事件和分身互相覆盖。"""
        super().__init__()
        self._lock = threading.RLock()
        self._enable = False
        self._stop_event = threading.Event()
        self._active_batch = None
        self._active_edit = None
        self._playback_jobs = {}

    def init_plugin(self, config: dict | None = None):
        """读取配置并迁移本插件日期字段，保留档案和失败队列。"""
        self.stop_service()
        config = dict(config or {})
        # 页签仅属于当前界面，不持久化；重新打开时总是先显示档案管理。
        transient_fields = {"_archive_model", "_match", "_archive_options", "archive_search", "_ui_tab"}
        had_transient = bool(transient_fields.intersection(config))
        tab = config.get("_ui_tab", "archive")
        for field in transient_fields:
            config.pop(field, None)
        if tab == "settings":
            # 在设置页保存不会执行另一页尚未提交的批量编辑/重试操作。
            config.update(retry_now=False, retry_remove=[], archive_action=None, archive_item=[])
        import_requested = bool(config.get("import_legacy")) and tab == "settings"
        if config.get("import_legacy"):
            config["import_legacy"] = False
            self.update_config(config)
        with self._lock:
            self._stop_event = threading.Event()
            importer = LegacyImporter(self)
            try:
                config = importer.recover(config)
                if import_requested:
                    self._log_start("原版导入", "豆瓣书影音档案")
                    config, result, message = importer.run(config)
                    self._log_result("原版导入", "豆瓣书影音档案", result, message)
            except Exception as error:
                # 恢复未完成时保持停用，保留恢复日志，绝不带着半套数据继续同步。
                self._stop_event.set()
                log_event("原版导入", "失败", "恢复数据", f"恢复未完成（{type(error).__name__}）；插件保持停用，请检查数据库后重新加载", level="error")
                return
            self._migrate_timestamps()
            self._enable = bool(config.get("enable", False))
            self._private = bool(config.get("private", True))
            self._first = bool(config.get("first", True))
            self._user = str(config.get("user") or "")
            self._exclude = str(config.get("exclude") or "")
            self._cookie = str(config.get("cookie") or "")
            self._pc_month = self._number(config.get("pc_month"), 3, 1, 36)
            self._pc_num = self._number(config.get("pc_num"), 50, 1, 500)
            self._mobile_month = self._number(config.get("mobile_month"), 2, 1, 36)
            self._mobile_num = self._number(config.get("mobile_num"), 15, 1, 500)
            archive_action = str(config.get("archive_action") or "")
            archive_item = config.get("archive_item")
            retry_now = bool(config.get("retry_now"))
            remove = config.get("retry_remove")
            remove = {key for key in remove if isinstance(key, str)} if isinstance(remove, list) else set()
            # 一次性操作先复位，避免安装、重启或保存设置时重复执行。
            if had_transient or retry_now or config.get("retry_remove") or archive_action:
                config.update(retry_now=False, retry_remove=[], archive_action=None, archive_item=[])
                self.update_config(config)
            if remove:
                waiting = self._load_dict("wait")
                removed = len(remove.intersection(waiting))
                for key in remove:
                    waiting.pop(key, None)
                self.save_data("wait", waiting)
                log_event("失败记录", "成功", "移除记录", f"已移除 {removed} 条，剩余 {len(waiting)} 条；观影档案不受影响")
            edit_queued = self._request_archive_action(archive_item, archive_action, retry_now) if archive_action else False
            if retry_now and not edit_queued:
                self._queue_retry("手动重试", operation_id=self._load_dict("archive_edit").get("id") if archive_action else None)

    @staticmethod
    def _legacy_source_active() -> bool:
        return PluginManager().get_plugin_state(LegacyImporter.SOURCE)

    @staticmethod
    def _number(value, default: int, minimum: int = 0, maximum: int = 10000) -> int:
        """容忍空值和非法配置，并对 UI 数量设置上限。"""
        try:
            return max(minimum, min(maximum, int(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _tokens(value: str) -> set[str]:
        """支持中英文逗号，忽略尾随逗号和空白项。"""
        return {item.strip() for item in re.split(r"[,，]", value) if item.strip()}

    @staticmethod
    def _is_played(info: WebhookEventInfo) -> bool:
        """解析已看事件，排除 Jellyfin 明确的取消已看操作。"""
        if info.event in {"item.markplayed", "media.scrobble"}:
            return True
        if info.channel == "jellyfin" and info.event == "UserDataSaved" and info.save_reason == "TogglePlayed":
            payload = info.json_object or {}
            user_data = payload.get("UserData") or {}
            played = payload.get("Played", user_data.get("Played") if isinstance(user_data, dict) else None)
            return played is not False and str(played).lower() not in {"false", "0"}
        return False

    @eventmanager.register(EventType.WebhookMessage)
    def sync_log(self, event: Event):
        """按用户过滤通知；Emby 开始只记录，停止和标记已播放进入同步。"""
        if not self._enable or not event or not event.event_data:
            return
        try:
            info = event.event_data
            if isinstance(info, dict):
                info = WebhookEventInfo.model_validate(info)
            if info.item_type not in {"TV", "MOV"} or info.user_name not in self._tokens(self._user):
                return
            if info.channel == "emby":
                if info.event not in emby.EVENT_LABELS:
                    return
                with self._lock:
                    if not self._enable:
                        return
                    if info.event == "playback.start":
                        self._log_emby_start(info)
                    else:
                        self._queue_emby_event(info)
                return
            played = self._is_played(info)
            if not played and info.event not in {"playback.start", "media.play", "PlaybackStart"}:
                return
            with self._lock:
                if not self._enable:
                    return
                self._process_event(info, played)
        except Exception as error:
            # 不输出异常正文，第三方异常可能包含请求头、Cookie 或响应内容。
            title = getattr(info, "item_name", None) or "当前条目"
            self._log_result("播放同步", title, "失败", f"事件处理异常（{type(error).__name__}）",
                             level="error")

    def _log_emby_start(self, info):
        """开始播放只记录通知字段，不查询状态、不调度任务，也不修改失败队列。"""
        info = deepcopy(info)
        payload = info.json_object if isinstance(info.json_object, dict) else {}
        item = payload.get("Item")
        if isinstance(item, dict):
            emby.update_event(info, item)
        title, season, episode = self._playback_fields(info)
        title, episode_text = self._playback_display(title or "当前条目", info.item_type == "TV", season, episode)
        log_event("播放同步", "通知", title,
                  f"开始播放{episode_text}；仅记录，停止播放或标记已播放时再同步")

    def _queue_emby_event(self, info):
        """稍后回读 Emby 已保存状态；合并同一服务器、用户、条目的连续通知。"""
        try:
            user_id, item_id = emby.event_ids(info)
        except emby.StateError as error:
            self._log_start("播放同步", info.item_name or "当前条目")
            self._log_result("播放同步", info.item_name or "当前条目", "跳过", str(error))
            return
        identity = "\0".join((info.server_name or "", user_id, item_id))
        job_id = "playback_" + sha256(identity.encode()).hexdigest()[:24]
        request_id = uuid4().hex
        self._playback_jobs[job_id] = request_id
        try:
            queued = scheduler_sdk.add_plugin_once_job(
                self.__class__.__name__, job_id, self._run_emby_event,
                "豆瓣书影音档案Plus：检查 Emby 观看状态", delay_seconds=2,
                func_kwargs={"info": deepcopy(info), "job_id": job_id, "request_id": request_id})
        except Exception:
            queued = False
        if not queued:
            self._playback_jobs.pop(job_id, None)
            self._log_result("播放同步", info.item_name or "当前条目", "失败", "宿主未能安排 Emby 状态检查；本次未同步")
        else:
            log_event("播放同步", "调度", info.item_name, f"收到 Emby {emby.EVENT_LABELS[info.event]}通知；2 秒后检查状态", level="debug")

    def _run_emby_event(self, info, job_id, request_id):
        with self._lock:
            if not self._enable or self._stop_event.is_set() or self._playback_jobs.get(job_id) != request_id:
                return
            self._playback_jobs.pop(job_id, None)
            try:
                self._process_event(info, False)
            except Exception as error:
                self._log_result("播放同步", info.item_name or "当前条目", "失败",
                                 f"事件处理异常（{type(error).__name__}）", level="error")

    def _recognize_media(self, meta, media_source=None, media_id=None):
        """使用 V3 统一身份；指定身份未命中时再按标题识别。"""
        chain = MediaChain()
        title = getattr(meta, "name", None) or "当前条目"
        source_name = {MediaSource.TMDB: "TMDB", MediaSource.Douban: "豆瓣"}.get(
            media_source, getattr(media_source, "value", media_source))
        def result(media):
            log_recognition("媒体识别", title,
                            f"识别结果：{media_description(media, meta.type)}" if media else "未识别到媒体信息",
                            debug=bool(media))
            return media
        if build_media_key(media_source, media_id):
            log_recognition("媒体识别", title,
                            f"按指定身份识别：来源={source_name}，ID={media_id}，类型={meta.type.value}，"
                            f"季数={getattr(meta, 'begin_season', None) if meta.type == MediaType.TV else '不适用'}",
                            debug=True)
            media = chain.recognize_media(meta=deepcopy(meta), mtype=meta.type, media_source=media_source,
                                          media_id=str(media_id), cache=True)
            if media:
                return result(media)
            log_recognition("媒体识别", title, "指定媒体身份未识别到媒体信息，清除身份参数后按标题识别")
        else:
            log_recognition("媒体识别", title, "事件未提供有效媒体身份，按标题识别")
        # 宿主即使识别失败也会回填 meta 的身份，省略关键字并不能清除它。
        fallback = deepcopy(meta)
        fallback.media_source = None
        fallback.media_id = None
        return result(chain.recognize_media(meta=fallback, mtype=fallback.type, cache=True))

    @staticmethod
    def _library_douban_id(info: WebhookEventInfo) -> str | None:
        """从事件所属 Emby 的原始 ProviderIds 读取豆瓣 ID，避免丢失次要身份。"""
        title = info.item_name or "当前条目"
        def unavailable(reason):
            log_recognition("媒体库豆瓣 ID", title, f"未取得：{reason}")
            return None
        if info.channel != "emby":
            return unavailable("当前媒体服务器不支持读取 ProviderIds")
        try:
            payload = info.json_object if isinstance(info.json_object, dict) else {}
            raw_item = payload.get("Item") or {}
            if not isinstance(raw_item, dict):
                raw_item = {}
            item_id = (raw_item.get("SeriesId") if info.item_type == "TV" else raw_item.get("Id")) or info.item_id
            if not item_id:
                return unavailable("事件缺少媒体库条目 ID")
            service = emby.service_for(info)
            if not service:
                return unavailable("无法唯一确定事件所属 Emby 服务")
            expected_type = "Series" if info.item_type == "TV" else "Movie"
            for _ in range(2):
                if not re.fullmatch(r"[A-Za-z0-9-]+", str(item_id)):
                    return unavailable("媒体库条目 ID 无效")
                response = service.instance.get_data(
                    url=f"[HOST]emby/Users/[USER]/Items/{item_id}?Fields=ProviderIds&api_key=[APIKEY]")
                if response is None:
                    return unavailable("媒体库请求未收到响应")
                try:
                    if response.status_code != 200:
                        return unavailable(f"媒体库返回 HTTP {response.status_code}")
                    data = response.json()
                finally:
                    response.close()
                if not isinstance(data, dict):
                    return unavailable("媒体库响应格式无效")
                if data.get("Type") == expected_type:
                    providers = data.get("ProviderIds") or {}
                    if not isinstance(providers, dict):
                        return unavailable("ProviderIds 格式无效")
                    subject_id = next((str(value) for key, value in providers.items()
                                       if key.lower() == "douban" and re.fullmatch(r"[1-9]\d*", str(value))), None)
                    if not subject_id:
                        return unavailable("ProviderIds 未提供有效豆瓣 ID")
                    log_recognition("媒体库豆瓣 ID", title,
                                    f"来源=Emby {'整部剧（Series）' if info.item_type == 'TV' else '电影（Movie）'}，"
                                    f"豆瓣 ID={subject_id}；仅作为待核验候选")
                    return subject_id
                if info.item_type != "TV" or data.get("Type") not in {"Episode", "Season"}:
                    return unavailable("媒体库条目类型不符合预期")
                log_recognition("媒体库豆瓣 ID", title, "当前为季或单集，继续读取所属整部剧的 ProviderIds")
                item_id = data.get("SeriesId")
            return unavailable("未能读取所属整部剧")
        except Exception as error:
            return unavailable(f"读取异常（{type(error).__name__}）")

    @staticmethod
    def _season_episode_number(value) -> int | None:
        """保留第0季/集；None 表示缺失，-1 表示无效，不套用 UI 数量截断规则。"""
        text = "" if value is None else str(value).strip()
        if not text:
            return None
        try:
            return int(text) if text.isdecimal() else -1
        except ValueError:
            return -1

    @classmethod
    def _playback_fields(cls, info) -> tuple[str, int | None, int | None]:
        """通知日志与同步共用片名和季集字段解析，不查询媒体服务。"""
        title = (info.item_name or "").strip()
        if info.item_type != "TV":
            return title, 0, 0
        title = re.split(r"\s+[Ss]\d+(?:[Ee]\d+)?\b", title, maxsplit=1)[0].strip()
        return title, cls._season_episode_number(info.season_id), cls._season_episode_number(info.episode_id)

    @staticmethod
    def _playback_display(title: str, is_tv: bool, season: int | None, episode: int | None) -> tuple[str, str]:
        """通知和同步共用季集显示规则，保留第0季/集，不把缺失值显示成0。"""
        if not is_tv:
            return title, ""
        if season is not None and season >= 0 and not re.search(r"第[\d一二三四五六七八九十]+季\s*$", title):
            title = f"{title} 第{season}季"
        episode_label = {None: "集号未知", -1: "集号无效"}.get(episode, f"第{episode}集")
        return title, f"，{episode_label}"

    def _playback_skip_reason(self, info, state, season: int | None, episode: int | None) -> str:
        """识别前统一过滤；第一集规则需要识别后的当前季集数，留在后续判断。"""
        if info.item_type == "TV":
            if season is None or episode is None:
                return "缺少季集信息"
            if season < 0 or episode < 0:
                return "季集信息无效"
            if season == 0:
                return self._SPECIALS_SKIP_REASON
            if episode == 0:
                return "集号为0，跳过自动同步"
        if state and not state.played and not state.resumable:
            return "Emby 判定为未播放且不可续播"
        if state and info.item_type == "MOV" and not state.played:
            return "电影尚未标记已播放，豆瓣不支持电影在看"
        if any(token in (info.item_path or "") for token in self._tokens(self._exclude)):
            return "命中路径排除关键词"
        return ""

    @classmethod
    def _season_episodes(cls, media, season: int) -> set[int]:
        """读取当前季正整数集号，不计第0集；空集合表示未知，按单集处理。"""
        seasons = getattr(media, "seasons", None) or {}
        episodes = seasons.get(season, seasons.get(str(season), []))
        if not isinstance(episodes, list):
            return set()
        numbers = {cls._season_episode_number(item) for item in episodes}
        return {number for number in numbers if number is not None and number > 0}

    def _process_event(self, info: WebhookEventInfo, played: bool):
        """Emby 按已保存状态同步；其他服务器保留原有行为，后续共用匹配和写入流程。"""
        info = deepcopy(info)
        event_label = emby.EVENT_LABELS.get(info.event, "标记播放完成" if played else "开始播放")
        state = None
        if info.channel == "emby":
            title = info.item_name or "当前条目"
            try:
                state = emby.read_state(info)
            except emby.StateError as error:
                self._log_start("播放同步", title, detail=event_label)
                self._log_result("播放同步", title, "失败", f"{error}；本次未同步，未加入待重试")
                return
            played = state.played
            emby.update_event(info, state.item)
        title, season, episode = self._playback_fields(info)
        if not title:
            return
        is_tv = info.item_type == "TV"
        display_title, episode_text = self._playback_display(title, is_tv, season, episode)
        with operation("播放同步", display_title):
            state_text = f"；Emby {'单集' if is_tv else '电影'}状态：{state.label}" if state else ""
            self._log_start("播放同步", display_title, detail=event_label + episode_text + state_text)
            skip_reason = self._playback_skip_reason(info, state, season, episode)
            if skip_reason:
                self._log_result("播放同步", title, "跳过", skip_reason)
                return
            meta = MetaInfo(title)
            meta.type = MediaType.TV if is_tv else MediaType.MOVIE
            if is_tv:
                meta.begin_season = season
            media = self._recognize_media(meta, info.media_source, info.media_id)
            if not media:
                self._log_result("播放同步", title, "失败", "未识别到媒体信息；未加入待重试")
                return
            source = getattr(media, "media_source", None)
            media_id = getattr(media, "media_id", None)
            identity = build_media_key(source, media_id)
            if not identity:
                source, media_id = info.media_source, info.media_id
                identity = build_media_key(source, media_id)
            if not identity:
                self._log_result("播放同步", title, "跳过", "识别结果缺少有效媒体身份")
                return
            key = f"{identity}|{info.item_type}|S{season}"
            numbers = self._season_episodes(media, season) if is_tv else set()
            if self._first and is_tv and episode == 1 and len(numbers) > 1:
                self._log_result("播放同步", self.format_title(title, season), "跳过",
                                 "已设置不标记第一集，当前季包含多集")
                return
            # 未知集数按单集处理；已知集数按最大集号判断季末，兼容非连续编号。
            last_episode = max(numbers, default=1)
            completed = played or info.channel != "emby"
            status = "collect" if completed and (not is_tv or episode == last_episode) else "do"
            episode_detail = ""
            if is_tv:
                ending = f"季末第{last_episode}集" if numbers else "季末集未知（按单集处理）"
                episode_detail = f"当前第{episode}集，{ending}"
            title = self.format_title(getattr(media, "title", None) or title, season)
            records = self._load_dict("data")
            waiting = self._load_dict("wait")
            # 旧档案及早期转换的 legacy 状态不参与自动识别、映射复用或去重。
            previous = records.get(key) if isinstance(records.get(key), dict) else {}
            if previous.get("status") not in {"do", "collect"}:
                previous = {}
            years = getattr(media, "season_years", None) or {}
            year = years.get(season, years.get(str(season))) if is_tv else getattr(media, "year", None)
            year_source = f"第{season}季首播年份" if is_tv else "电影年份"
            if is_tv and season == 1 and not year:
                year = getattr(media, "year", None)
                year_source = "第一季年份缺失，使用整剧年份"
            source_name = {MediaSource.TMDB: "TMDB", MediaSource.Douban: "豆瓣"}.get(source, getattr(source, "value", source))
            log_recognition("媒体识别", title,
                            f"媒体={source_name}/{media_id}，年份={year or '未知'}（来源={year_source if year else '缺少可用年份'}），"
                            f"类型={meta.type.value}，豆瓣目标={self._action_label(status)}" +
                            (f"，{episode_detail}" if episode_detail else ""), debug=True)
            item = {"title": title, "status": status, "type": meta.type.value,
                    "media_source": getattr(source, "value", source), "media_id": str(media_id), "season": season,
                    "year": str(year) if year else None, "poster_path": getattr(media, "poster_path", None) or ""}
            pending = waiting.get(key) if isinstance(waiting.get(key), dict) else {}
            if pending:
                log_recognition("同步目标", title, "再次收到播放事件，沿用失败记录的有效目标继续处理", debug=True)
            for field in ("subject_id", "subject_name"):
                if pending.get(field) or previous.get(field):
                    item[field] = pending.get(field) or previous.get(field)
            if not item.get("subject_id"):
                item["preferred_subject_id"] = self._library_douban_id(info)
                if not item["preferred_subject_id"] and source == MediaSource.Douban:
                    item["preferred_subject_id"] = str(media_id)
                    log_recognition("豆瓣 ID 来源", title, f"来源=媒体识别结果，豆瓣 ID={media_id}；仍需核验具体季和年份")
            if self._sync_item(key, item, records, waiting, task="播放同步", started=True,
                               decision=episode_detail) == "success":
                self._queue_retry("同步成功后自动重试", exclude={key})

    def _load_dict(self, key: str) -> dict:
        """保留旧版字典的所有条目，包括暂时无法展示的异常记录。"""
        value = self.get_data(key)
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _valid_timestamp(value) -> str:
        """统一验证可排序的本地日期字符串，缺失或损坏日期保持未知。"""
        if not isinstance(value, str):
            return ""
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return ""

    @classmethod
    def _earliest_timestamp(cls, *values) -> str:
        return min(filter(None, (cls._valid_timestamp(value) for value in values)), default="")

    def _migrate_timestamps(self):
        """一次性收敛日期字段；最后写版本标记，重复执行不会改变迁移结果。"""
        if self.get_data("timestamp_schema") == self._TIMESTAMP_SCHEMA:
            return
        records, waiting = self._load_dict("data"), self._load_dict("wait")
        updated = deepcopy(records)
        for item in updated.values():
            if isinstance(item, dict):
                stamp = self._earliest_timestamp(item.pop("timestamp", None), item.pop("status_updated_at", None))
                if stamp:
                    item["timestamp"] = stamp
        if updated != records:
            self.save_data("data", updated)
        converted = deepcopy(waiting)
        fallback_count = unknown_count = 0
        for item in converted.values():
            if not isinstance(item, dict):
                continue
            # 旧队列的 timestamp 可能是复制来的档案日期，不能当作首次尝试时间。
            item.pop("timestamp", None)
            item.pop("status_updated_at", None)
            stamp = self._valid_timestamp(item.get("last_attempt"))
            if stamp:
                item["timestamp"] = stamp
                fallback_count += 1
            else:
                unknown_count += 1
        if converted != waiting:
            self.save_data("wait", converted)
        self.del_data("archive_last_removed")
        self.save_data("timestamp_schema", self._TIMESTAMP_SCHEMA)
        log_event("数据迁移", "成功", "档案时间", f"历史日期取较早有效值；{fallback_count} 条旧待重试记录使用最后尝试时间作为兼容起点，"
                    f"{unknown_count} 条从下次实际尝试开始计时；已清理插件内移除备份")

    @staticmethod
    def _failure_count(value) -> int:
        """失败次数是累计值，不使用有上限的 UI 数字解析器。"""
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _log_start(self, task: str, title: str, action=None, index=1, total=1, *, detail="") -> str:
        parts = [detail, f"目标：{self._action_label(action)}" if action else ""]
        return log_event(task, "开始", title, "；".join(part for part in parts if part), index=index, total=total)

    @staticmethod
    def _log_result(task: str, title: str, result: str, detail: str, *, level=None) -> str:
        """界面与日志复用同一句结果；每个条目只输出一次最终结果。"""
        return log_event(task, result, title, detail, level=level)

    @staticmethod
    def _batch_message(task: str, total: int, success=0, failed=0, skipped=0, cancelled=0,
                       pending=0, remaining=None) -> str:
        detail = f"共 {total} 条，成功 {success} 条，失败 {failed} 条，跳过 {skipped} 条，取消 {cancelled} 条"
        if pending:
            detail += f"，待完成 {pending} 条"
        if remaining is not None:
            detail += f"；剩余待重试 {remaining} 条"
        return format_message(task, "进度" if pending else "汇总", "批量任务", detail, use_context=False)

    @classmethod
    def _record_rank(cls, item: dict) -> tuple:
        """展示与状态判断使用相同的记录优先级，不把未知状态当作已确认。"""
        return (item.get("status") in {"do", "collect"},
                cls._valid_timestamp(item.get("timestamp")), item.get("status") == "collect")

    def _apply_manual_match(self, key: str, item: dict) -> dict:
        """人工映射优先用于识别；显式档案操作及其重试固定使用选中的条目。"""
        if item.get("manual_status"):
            return dict(item)
        candidate = matching.selected_candidate(self._load_dict("manual_matches").get(key))
        if not candidate:
            return dict(item)
        # 档案海报沿用 MP 媒体识别结果；豆瓣候选海报仅供人工匹配界面核对。
        return {**item, "subject_id": candidate["subject_id"], "subject_name": candidate["title"],
                "type": candidate["type"], "matched_by": "manual"}

    def _sync_context(self, key: str, item: dict, records: dict, waiting: dict):
        related = {k for k in self._related_keys(records, {key}, item.get("subject_id"))
                   if isinstance(records[k], dict) and (item.get("manual_status") or k == key
                       or records[k].get("status") in {"do", "collect"})}
        pending = self._related_keys(waiting, related | {key}, item.get("subject_id"))
        confirmed = [records[k] for k in related if records[k].get("status") in {"do", "collect"}
                     and (item.get("matched_by") != "manual" or str(records[k].get("subject_id")) == str(item.get("subject_id")))]
        current = max(confirmed, key=self._record_rank, default=None)
        return related, pending, current

    def _prepare_sync(self, key: str, item: dict, records: dict, waiting: dict,
                      attempted_at: str) -> _SyncPlan:
        """确定有效目标及其首次尝试时间；只做决策，不写数据库或请求豆瓣。"""
        item = self._apply_manual_match(key, item)
        related, pending, current = self._sync_context(key, item, records, waiting)
        if not item.get("manual_status"):
            manual = [(k, waiting[k]) for k in pending if isinstance(waiting[k], dict)
                      and waiting[k].get("manual_status") and waiting[k].get("status") in {"do", "collect", "remove"}]
            if manual:
                key, requested = max(manual, key=lambda pair: self._valid_timestamp(pair[1].get("timestamp")))
                item = dict(requested)
                related, pending, current = self._sync_context(key, item, records, waiting)
            elif item.get("status") == "do" and any(isinstance(waiting[k], dict)
                    and waiting[k].get("status") == "collect" for k in pending):
                item["status"] = "collect"
                # 自动在看不能覆盖尚未完成的看过目标；沿用看过目标自己的时间。
                item.pop("timestamp", None)
        item["type"] = {"TV": "电视剧", "MOV": "电影"}.get(item.get("type"), item.get("type"))
        item["title"] = item.get("title") or key
        item.pop("next_retry", None)
        item.pop("status_updated_at", None)
        previous = [waiting[k] for k in pending if isinstance(waiting[k], dict)]
        same_target = [value.get("timestamp") for value in previous if value.get("status") == item.get("status")]
        item["timestamp"] = self._earliest_timestamp(*same_target, item.get("timestamp")) or attempted_at
        plan = _SyncPlan(key=key, item=item, record_keys=related, pending_keys=pending, current=current,
                         attempts=sum(self._failure_count(value.get("attempts")) for value in previous))
        plan.skip_reason = self._unsupported_status_reason(item)
        plan.keep_pending = bool(plan.skip_reason)
        if plan.skip_reason:
            return plan
        if item.get("manual_status") and not related:
            plan.skip_reason = "本地档案已不存在"
            return plan
        current_status = current.get("status") if current else None
        satisfied = current_status == item.get("status") or (
            not item.get("manual_status") and current_status == "collect" and item.get("status") == "do")
        if satisfied:
            plan.skip_reason = f"本地已记录为{self._action_label(current_status)}，无需变更状态"
        return plan

    @staticmethod
    def _archive_record(item: dict) -> dict:
        """正式档案不携带队列计数、编辑任务标识或废弃日期字段。"""
        transient = {"manual_status", "manual_request_id", "manual_group_key", "attempts", "last_attempt",
                     "last_error", "next_retry", "status_updated_at", "match_candidates"}
        return {key: value for key, value in item.items() if key not in transient}

    def _commit_sync(self, plan: _SyncPlan, outcome: str, records: dict, waiting: dict,
                     attempted_at: str, reason: str) -> str:
        """统一提交成功、失败和跳过；失败不更改档案，同目标重试不刷新起点。"""
        item = plan.item
        if plan.keep_pending:
            return plan.skip_reason + ("；失败记录保留" if plan.pending_keys else "")
        for key in plan.pending_keys:
            waiting.pop(key, None)
        if outcome == "failed":
            waiting[plan.key] = {**item, "attempts": plan.attempts + 1,
                                 "last_attempt": attempted_at, "last_error": reason}
            detail = f"{reason}；已加入待重试，累计失败 {plan.attempts + 1} 次"
        elif outcome == "success":
            if item["status"] == "remove":
                for key in plan.record_keys:
                    records.pop(key, None)
                detail = "已取消豆瓣标记并移除本地档案"
            else:
                for key in plan.record_keys:
                    records[key] = self._archive_record({**records[key], "status": item["status"],
                                                        "timestamp": item["timestamp"]})
                if not item.get("manual_status"):
                    records[plan.key] = self._archive_record(item)
                detail = f"已同步豆瓣{self._action_label(item['status'])}"
            self.save_data("data", records)
        else:
            # 补齐或纠正媒体绑定，沿用目标条目已确认的状态和日期，不重复写入豆瓣。
            previous = records.get(plan.key)
            if (plan.current and not item.get("manual_status")
                    and (item.get("media_id") or item.get("matched_by") == "manual")
                    and (not isinstance(previous, dict)
                         or str(previous.get("subject_id")) != str(item.get("subject_id")))):
                records[plan.key] = self._archive_record({**item, "status": plan.current["status"],
                                                        "timestamp": plan.current.get("timestamp", "")})
                self.save_data("data", records)
            detail = plan.skip_reason + ("；已清除对应待重试记录" if plan.pending_keys else "")
        self.save_data("wait", waiting)
        return detail

    def _sync_item(self, key: str, item: dict, records: dict, waiting: dict, helpers=None,
                   *, task=None, started=False, result_row=None, decision="") -> str:
        """所有入口共用：确定目标 → 判断变更 → 同步 → 提交 → 记录结果。"""
        attempted_at = self._now()
        task = task or ("档案编辑" if item.get("manual_status") else "播放同步")
        with operation(task, item.get("title") or key):
            plan = self._prepare_sync(key, item, records, waiting, attempted_at)
            if not started:
                self._log_start(task, plan.item["title"], plan.item.get("status"))
            target = plan.item
            current = self._action_label(plan.current.get("status")) if plan.current else "未记录"
            log_recognition("同步目标", target["title"],
                            (decision + "；" if decision else "") + f"目标：{self._action_label(target['status'])}，本地：{current}")
            origin = None
            if target.get("subject_id"):
                origin = "当前条目已绑定 ID"
                if target.get("matched_by") == "manual":
                    origin = "人工确认（最高优先级）"
                elif target.get("manual_status"):
                    origin = "档案编辑已绑定条目"
                elif any(isinstance(waiting.get(k), dict) and
                         str(waiting[k].get("subject_id")) == str(target["subject_id"]) for k in plan.pending_keys):
                    origin = "失败记录已绑定条目"
                elif any(isinstance(records.get(k), dict) and
                         str(records[k].get("subject_id")) == str(target["subject_id"]) for k in plan.record_keys):
                    origin = "本插件档案已绑定条目"
                log_recognition("豆瓣 ID 来源", target["title"],
                                f"来源={origin}，条目={target.get('subject_name') or target['title']}，"
                                f"豆瓣 ID={target['subject_id']}；直接复用，不重新搜索匹配", debug=bool(plan.skip_reason))
            succeeded, reason, error_level = False, "", None
            try:
                if not plan.skip_reason:
                    helpers = helpers if helpers is not None else []
                    if not helpers:
                        helpers.append(DoubanHelper(self._cookie))
                    helper = helpers[0]
                    target = plan.item
                    if not target.get("subject_id"):
                        log_recognition("豆瓣匹配", target["title"],
                                        f"尚无已确认条目，开始自动匹配；待核验豆瓣 ID={target.get('preferred_subject_id') or '无'}")
                        target["subject_name"], target["subject_id"] = helper.get_subject_id(
                            target["title"], year=target.get("year"),
                            mtype=MediaType.TV if target.get("type") == "电视剧" else MediaType.MOVIE,
                            preferred_id=target.get("preferred_subject_id"), season=target.get("season"))
                        candidates = getattr(helper, "last_candidates", None)
                        if isinstance(candidates, list) and candidates:
                            target["match_candidates"] = deepcopy(candidates)
                        # 新识别出的 ID 可能关联另一媒体身份或未完成的手动操作，复用同一决策流程。
                        if target.get("subject_id"):
                            plan = self._prepare_sync(plan.key, target, records, waiting, attempted_at)
                            log_recognition("同步目标", plan.item["title"],
                                            f"匹配后重新检查本地状态，最终目标={self._action_label(plan.item['status'])}", debug=True)
                    if not plan.skip_reason:
                        target = plan.item
                        if target.get("subject_id"):
                            log_recognition("豆瓣写入", target["title"],
                                            f"目标：{self._action_label(target['status'])}")
                        succeeded = bool(target.get("subject_id")) and (
                            helper.clear_watching_status(str(target["subject_id"])) if target["status"] == "remove" else
                            helper.set_watching_status(str(target["subject_id"]), target["status"], self._private))
                        reason = getattr(helper, "last_error", "")
                        if not isinstance(reason, str) or not reason:
                            reason = "豆瓣未返回写入成功" if target.get("subject_id") else "豆瓣搜索未能确认条目（未返回具体原因）"
            except Exception as error:
                reason, error_level = f"同步异常（{type(error).__name__}）", "error"
            outcome = "skipped" if plan.skip_reason else "success" if succeeded else "failed"
            detail = self._commit_sync(plan, outcome, records, waiting, attempted_at, reason)
            state, label = {"success": ("完成", "成功"), "failed": ("失败", "失败"), "skipped": ("跳过", "跳过")}[outcome]
            message = self._log_result(task, plan.item["title"], label, detail,
                                       level=error_level)
            self._finish_archive_edit(plan.item, state, message)
            if result_row is not None:
                result_row["message"] = message
            return outcome

    def _queue_retry(self, trigger: str, exclude: set | None = None, *, operation_id=None):
        """只由实际同步成功或用户操作触发；宿主管理一次性任务，不创建周期任务。"""
        if self._active_edit is not None:
            self._active_edit["retry_after_edit"] = True
            log_event("失败重试", "调度", "批量任务", "档案编辑完成后执行", level="debug")
            return
        if self._active_batch is not None:
            log_event("失败重试", "调度", "批量任务", "已有任务等待或执行，未重复提交", level="debug")
            return
        waiting = self._load_dict("wait")
        keys = [key for key in waiting if key not in (exclude or set())]
        if not keys and trigger != "手动重试":
            return
        batch = {"id": uuid4().hex, "state": "等待执行", "trigger": trigger, "started_at": self._now(),
                 "total": len(keys), "attempted": 0, "success": 0, "failed": 0,
                 "skipped": 0, "invalid": 0, "cancelled": 0, "remaining": len(waiting), "items": []}
        if operation_id or trigger == "手动重试":
            batch["operation_id"] = operation_id or batch["id"]
            self._save_manual_result("批量重试", batch, start=operation_id is None)
        if not keys:
            batch.update(state="完成", finished_at=self._now(), message=format_message("失败重试", "跳过", "批量任务", "没有待重试记录", use_context=False))
            self._save_retry_progress(batch)
            log_event("失败重试", "跳过", "批量任务", "没有待重试记录")
            return
        for key in keys:
            item = waiting[key] if isinstance(waiting[key], dict) else {}
            batch["items"].append({"key": key, "title": item.get("title") or key,
                                   "action": item.get("status"), "state": "等待执行", "message": "等待执行"})
        if not self._enable:
            batch["state"] = "未启动"
            self._finish_retry_batch(batch, "插件未启用；失败记录保留")
            return
        self._active_batch = batch
        self._save_retry_progress(batch)
        schedule_error = ""
        try:
            queued = scheduler_sdk.add_plugin_once_job(
                self.__class__.__name__, "retry_waiting", self._run_retry,
                "豆瓣书影音档案Plus：重试失败记录", delay_seconds=self._RETRY_INTERVAL_SECONDS,
                func_kwargs={"keys": keys, "batch_id": batch["id"]})
        except Exception as error:
            queued = False
            schedule_error = f"（{type(error).__name__}）"
            log_event("失败重试", "调度", "批量任务", f"任务调度异常{schedule_error}", level="error")
        if not queued:
            self._active_batch = None
            batch["state"] = "未启动"
            self._finish_retry_batch(batch, f"宿主未能启动任务{schedule_error}；失败记录保留")
        else:
            log_event("失败重试", "调度", "批量任务", f"已提交 {len(keys)} 条，来源：{trigger}", level="debug")

    def _finish_retry_batch(self, batch: dict, cancel_reason="任务已中止"):
        """结束时补齐未处理条目的取消结果；单条任务不重复输出汇总。"""
        for row in batch["items"]:
            if row["state"] in {"等待执行", "执行中"}:
                row["message"] = self._log_result("失败重试", row["title"], "取消", cancel_reason)
                row["state"] = "已取消"
        batch["cancelled"] = sum(row["state"] == "已取消" for row in batch["items"])
        batch.update(finished_at=self._now(), remaining=len(self._load_dict("wait")))
        batch["message"] = (batch["items"][0]["message"] if batch["total"] == 1 else
            self._batch_message("失败重试", batch["total"], batch["success"], batch["failed"],
                                batch["skipped"], batch["cancelled"], remaining=batch["remaining"]))
        self._save_retry_progress(batch)
        if batch["total"] > 1:
            logger.info(batch["message"])

    def _run_retry(self, keys: list[str], batch_id: str):
        """快照决定本轮范围；每项前重读存储，所有条目均有开始及结果。"""
        with self._lock:
            batch, stop = self._active_batch, self._stop_event
            if batch is None or batch["id"] != batch_id or stop.is_set() or not self._enable:
                return
            batch["state"] = "执行中"
            if len(keys) > 1:
                log_event("失败重试", "开始", "批量任务", f"共 {len(keys)} 条，来源：{batch['trigger']}；"
                            f"条目间隔 {self._RETRY_INTERVAL_SECONDS} 秒")
            self._save_retry_progress(batch)
        try:
            attempted = False
            helpers = []
            for index, key in enumerate(keys, 1):
                if attempted and stop.wait(self._RETRY_INTERVAL_SECONDS):
                    break
                with self._lock:
                    if stop.is_set() or not self._enable or self._active_batch is not batch:
                        break
                    waiting, records = self._load_dict("wait"), self._load_dict("data")
                    row = batch["items"][index - 1]
                    original = waiting.get(key)
                    if isinstance(original, dict):
                        row.update(title=original.get("title") or key, action=original.get("status"))
                    row["state"] = "执行中"
                    with operation("失败重试", row["title"], index, len(keys)):
                        self._log_start("失败重试", row["title"], row.get("action"), index, len(keys))
                        if key not in waiting:
                            row["message"] = self._log_result("失败重试", row["title"], "跳过",
                                "失败记录已移除")
                            outcome = "skipped"
                        elif not self._valid_pending(original):
                            reason = self._unsupported_status_reason(original) or "记录数据不完整"
                            row["message"] = self._log_result("失败重试", row["title"], "跳过",
                                f"{reason}；失败记录保留")
                            outcome = "skipped"
                        else:
                            outcome = self._sync_item(key, original, records, waiting, helpers,
                                                      task="失败重试", started=True, result_row=row)
                    attempted = bool(helpers)
                    batch["attempted"] += int(outcome != "skipped")
                    row["state"] = {"success": "完成", "failed": "失败", "skipped": "跳过"}[outcome]
                    batch[outcome] += 1
                    batch["remaining"] = len(waiting)
                    self._save_retry_progress(batch)
        except Exception as error:
            reason = f"处理异常（{type(error).__name__}）"
            batch["state"] = "中断"
            # 异常条目记失败；尚未开始的条目在收尾时记取消。
            active = next((item for item in batch["items"] if item["state"] == "执行中"), None)
            if active is not None:
                active["message"] = self._log_result("失败重试", active["title"], "失败",
                                                      reason, level="error")
                active["state"] = "失败"
                batch["failed"] += 1
            else:
                log_event("失败重试", "失败", "批量任务", reason, level="error")
        finally:
            with self._lock:
                if self._active_batch is batch:
                    if batch["state"] == "执行中":
                        batch["state"] = "已停止" if stop.is_set() else "完成"
                    self._finish_retry_batch(batch, "本轮重试已中止；失败记录保留")
                    self._active_batch = None

    @staticmethod
    def format_title(title: str, season: int) -> str:
        """为第二季起添加豆瓣季名，不重复添加已有的季后缀。"""
        if season > 1 and not re.search(r"第[\d一二三四五六七八九十]+季\s*$", title):
            return f"{title} 第{season}季"
        return title

    def get_line_item(self, mobile: bool = False) -> list[dict]:
        return views.timeline_items(self, mobile)

    def get_dashboard(self, **kwargs):
        """首页时间线使用档案中的 TMDB 海报。"""
        return views.dashboard(self, **kwargs)

    def get_page(self) -> list[dict] | None:
        """使用原生表单的档案管理/插件设置双页签，观影时间线仅在首页展示。"""
        # 必须保持占位实现；return [] 仍会被宿主识别为具有详情页。
        pass

    @staticmethod
    def get_render_mode():
        return "vuetify", None

    def get_state(self) -> bool:
        """返回启用状态。"""
        return self._enable

    def stop_service(self):
        """取消未开始的任务，中断批次间隔；正在进行的单条同步完成后再停用。"""
        self._stop_event.set()
        with self._lock:
            self._enable = False
            playback_jobs = list(self._playback_jobs)
            self._playback_jobs.clear()
            for job_id in playback_jobs:
                try:
                    scheduler_sdk.remove_plugin_once_job(self.__class__.__name__, job_id)
                except Exception:
                    log_event("播放同步", "调度", "Emby 状态检查", "任务取消失败；旧任务会根据请求编号跳过", level="warning")
            if self._active_batch is not None:
                batch = self._active_batch
                batch["state"] = "已停止"
                self._finish_retry_batch(batch, "插件已停用或重新加载；失败记录保留")
                self._active_batch = None
                try:
                    scheduler_sdk.remove_plugin_once_job(self.__class__.__name__, "retry_waiting")
                except Exception as error:
                    log_event("失败重试", "调度", "批量任务", f"任务取消异常（{type(error).__name__}）；旧任务执行前会检查停用状态", level="error")

            if self._active_edit is not None:
                request = self._active_edit
                self._cancel_archive_rows(request, "插件已停用或重新加载")
                self._active_edit = None
                self._log_archive_summary(request)
                try:
                    scheduler_sdk.remove_plugin_job(self.__class__.__name__)
                    scheduler_sdk.remove_plugin_once_job(self.__class__.__name__, "archive_edit")
                except Exception as error:
                    log_event("档案编辑", "调度", "批量任务", f"任务取消异常（{type(error).__name__}）；旧任务执行前会检查请求编号", level="error")

    def get_service(self) -> list[dict]:
        """宿主保存配置后注册日期任务；避免 init_plugin 内立即调度与服务重建竞争。"""
        with self._lock:
            request = self._active_edit
            if not self._enable or not request or request.get("state") != "等待执行":
                return []
            return [{"id": "archive_edit", "name": "豆瓣书影音档案Plus：编辑观看状态",
                     "trigger": "date", "func": self._run_archive_edit,
                     "kwargs": {"run_date": datetime.now().astimezone() + timedelta(seconds=3),
                                "misfire_grace_time": None},
                     "func_kwargs": {"request_id": request["id"],
                                     "retry_others": request.get("retry_others", False)}}]

    @staticmethod
    def get_command() -> list[dict]:
        """不注册远程命令。"""
        return []

    def get_api(self) -> list[dict]:
        """保留实例作用域的登录认证档案接口。"""
        return [{"path": path, "endpoint": endpoint, "methods": [method], "auth": "bear",
                 "summary": summary} for path, endpoint, method, summary in [
            ("/archive", self.api_archive, "GET", "读取档案管理状态"),
            ("/archive/edit", self.api_archive_edit, "POST", "提交档案批量编辑"),
            ("/archive/retry", self.api_archive_retry, "POST", "重试失败条目"),
            ("/archive/drop-wait", self.api_archive_drop_wait, "POST", "移除失败记录"),
            ("/match/preview", self.api_match_preview, "POST", "读取失败记录的待确认候选"),
            ("/match/poster", self.api_match_poster, "POST", "保存人工匹配海报缓存"),
            ("/match/confirm", self.api_match_confirm, "POST", "确认匹配并同步当前条目")]]

    def get_form(self):
        """默认打开档案管理，设置位于第二页签。"""
        return views.form(self)

    def _save_manual_result(self, kind: str, result: dict, *, start=False):
        """只保留最近一次手动操作；同次编辑及后续重试共享编号，旧任务不能覆盖新结果。"""
        operation_id = result.get("operation_id") or result.get("id")
        latest = {"id": operation_id, "parts": {}} if start else self._load_dict("manual_result")
        if operation_id and latest.get("id") == operation_id:
            latest["parts"][kind] = deepcopy(result)
            self.save_data("manual_result", latest)

    def _manual_result(self) -> dict:
        """用原有一个结果区呈现；仅在文字中标记操作类型。"""
        parts = self._load_dict("manual_result").get("parts")
        if not isinstance(parts, dict) or not parts:
            # 兼容升级前已有编辑结果；读取界面不修改存储。
            previous = self._load_dict("archive_edit")
            parts = {"档案编辑": previous} if previous else {}
        messages, items, warning = [], [], False
        active = {value.get("id") for value in (self._active_edit, self._active_batch) if value}
        for kind, result in parts.items():
            state = result.get("state")
            message = result.get("message") or state or ""
            if state in {"等待执行", "执行中"} and result.get("id") not in active:
                message = "上次操作未完成，请重新执行"
                warning = True
            warning = warning or bool(result.get("failed") or result.get("cancelled")) or state in {
                "失败", "部分失败", "部分完成", "未启动", "已取消", "已停止", "中断"}
            messages.append(message if message.startswith((kind, "[")) else f"{kind}：{message}")
            items.extend({**row, "title": f"{kind} · {row.get('title') or row.get('key') or row.get('group_key') or ''}"}
                         for row in result.get("items", []) if isinstance(row, dict))
        return {"message": "\n".join(messages), "items": items, "state": "失败" if warning else "完成"}

    def _save_retry_progress(self, batch: dict):
        self.save_data("retry_summary", batch)
        if batch.get("operation_id"):
            display = deepcopy(batch)
            pending = sum(row.get("state") in {"等待执行", "执行中"} for row in batch.get("items", []))
            display["message"] = batch.get("message") or self._batch_message("失败重试", batch["total"],
                batch["success"], batch["failed"], batch["skipped"], batch.get("cancelled", 0), pending,
                remaining=batch["remaining"])
            display["message"] = re.sub(r"^(?:批量)?失败重试", "批量重试", display["message"], count=1)
            self._save_manual_result("批量重试", display)

    @staticmethod
    def _action_label(action) -> str:
        return {"do": "在看", "collect": "看过", "remove": "取消豆瓣标记"}.get(str(action), "未知")

    @staticmethod
    def _result_label(state) -> str:
        # 保留旧数据的状态字段，仅统一用户看到的称呼。
        return {"完成": "成功", "已取消": "取消", "已跳过": "跳过"}.get(state, state or "未知")

    @classmethod
    def _unsupported_status_reason(cls, item) -> str:
        """播放同步及其失败记录暂停第0季；已有档案仍允许用户明确编辑或移除。"""
        if not isinstance(item, dict):
            return ""
        kind = str(item.get("type") or "").strip().upper()
        if (kind in {"电视剧", "TV"} and cls._season_episode_number(item.get("season")) == 0
                and not item.get("manual_status")):
            return cls._SPECIALS_SKIP_REASON
        if item.get("status") == "do" and kind in {"电影", "MOV", "MOVIE"}:
            return "电影不支持在看"
        return ""

    @classmethod
    def _valid_pending(cls, item) -> bool:
        return (isinstance(item, dict) and not cls._unsupported_status_reason(item)
                and (item.get("status") in ("do", "collect") or
                     (item.get("status") == "remove" and item.get("manual_status") is True)))

    @classmethod
    def _pending_reason(cls, item) -> str:
        unsupported = cls._unsupported_status_reason(item)
        if unsupported:
            return f"{unsupported}；不会重试，可移除此失败记录"
        if not cls._valid_pending(item):
            return "记录数据不完整，本轮不会尝试；可手动移除"
        return item.get("last_error") or "旧记录未记录原因，重试后更新"

    @staticmethod
    def _related_keys(items: dict, keys: set, subject_id=None) -> set:
        """定位同一豆瓣条目的别名键及其待重试记录。"""
        valid_id = re.fullmatch(r"[1-9]\d*", str(subject_id or ""))
        return {key for key, value in items.items() if key in keys or
                valid_id and isinstance(value, dict) and str(value.get("subject_id")) == str(subject_id)}

    def _archive_groups(self, records=None) -> dict:
        """按豆瓣 ID 归并档案；缺失状态保持未知，不把旧档案猜成看过。"""
        records = self._load_dict("data") if records is None else records
        groups = {}
        for key, original in records.items():
            item = original if isinstance(original, dict) else {}
            sid = str(item.get("subject_id") or "")
            valid = bool(re.fullmatch(r"[1-9]\d*", sid))
            group_key = f"douban:{sid}" if valid else f"record:{key}"
            group = groups.setdefault(group_key, {"keys": [], "subject_id": sid if valid else None,
                "status": "unknown", "record": {}, "key": key, "title": str(key), "rank": (), "date": ""})
            group["keys"].append(key)
            rank = self._record_rank(item)
            if rank > group["rank"]:
                group.update(record=dict(item), key=key, rank=rank,
                    title=str(item.get("subject_name") or item.get("title") or key),
                    status=item["status"] if rank[0] else "unknown", date=rank[1])
        return groups

    def api_archive(self) -> Response:
        """只读取本地状态，不查询或修改豆瓣；不返回配置和 Cookie。"""
        with self._lock:
            groups = self._archive_groups()
            records = [{"key": key, "title": group["title"], "subject_id": group["subject_id"],
                        "status": group["status"], "date": group["date"]}
                       for key, group in sorted(groups.items(), key=lambda pair: (pair[1]["date"], pair[1]["title"]), reverse=True)]
            waiting = []
            for key, original in self._load_dict("wait").items():
                item = original if isinstance(original, dict) else {}
                waiting.append({"key": key, "title": str(item.get("title") or key),
                    "target": self._action_label(item.get("status")),
                    "attempts": self._failure_count(item.get("attempts")) if "attempts" in item else None,
                    "last_attempt": item.get("last_attempt") or "未记录",
                    "reason": self._pending_reason(item)})
            latest = self._load_dict("archive_edit")
            if latest.get("state") in {"等待执行", "执行中"} and self._active_edit is None:
                latest["message"] = "上次编辑未完成，请重新勾选档案并执行"
            return Response(success=True, data={"records": records, "waiting": waiting, "latest": latest,
                "enabled": self._enable, "busy": self._active_edit is not None or self._active_batch is not None})

    def api_archive_edit(self, payload: dict) -> Response:
        """直接安排一次性任务，无需保存配置或重建服务。"""
        with self._lock:
            if self._active_edit is not None or self._active_batch is not None:
                return Response(success=False, message="已有任务正在执行，请完成后再提交")
            selection, action = payload.get("selection"), payload.get("action")
            if (not isinstance(action, str) or action not in {"do", "collect", "remove"} or not isinstance(selection, list)
                    or not selection or any(not isinstance(key, str) or not key for key in selection)):
                return Response(success=False, message="请勾选档案并选择操作")
            if not self._enable:
                return Response(success=False, message="请先在设置中启用插件")
            if not self._request_archive_action(selection, action):
                return Response(success=False, message=self._load_dict("archive_edit").get("message"))
            request = self._active_edit
            try:
                queued = scheduler_sdk.add_plugin_once_job(
                    self.__class__.__name__, "archive_edit", self._run_archive_edit,
                    "豆瓣书影音档案Plus：编辑观看状态", delay_seconds=3,
                    func_kwargs={"request_id": request["id"], "retry_others": False})
            except Exception as error:
                queued = False
                log_event("档案编辑", "调度", "批量任务", f"任务调度异常（{type(error).__name__}）", level="error")
            if not queued:
                self._cancel_archive_rows(request, "宿主未能启动编辑任务，请稍后重试")
                self._active_edit = None
                self._log_archive_summary(request)
                return Response(success=False, message="宿主未能启动编辑任务，请稍后重试")
            return Response(success=True, message="已提交，正在等待执行", data={"request_id": request["id"]})

    def api_archive_retry(self) -> Response:
        with self._lock:
            if not self._enable:
                return Response(success=False, message="请先在设置中启用插件")
            if self._active_edit is not None or self._active_batch is not None:
                return Response(success=False, message="已有任务正在执行，请完成后再重试")
            self._queue_retry("手动重试")
            if self._load_dict("wait") and self._active_batch is None:
                return Response(success=False, message="宿主未能启动重试任务，请稍后重试")
            return Response(success=True, message="已提交重试")

    def api_archive_drop_wait(self, payload: dict) -> Response:
        with self._lock:
            if self._active_edit is not None or self._active_batch is not None:
                return Response(success=False, message="已有任务正在执行，请完成后再移除")
            keys = payload.get("selection")
            if not isinstance(keys, list) or not keys or any(not isinstance(key, str) for key in keys):
                return Response(success=False, message="请勾选需要移除的失败记录")
            waiting = self._load_dict("wait")
            removed = len(set(keys).intersection(waiting))
            for key in keys:
                waiting.pop(key, None)
            self.save_data("wait", waiting)
            log_event("失败记录", "成功", "移除记录", f"已移除 {removed} 条，剩余 {len(waiting)} 条；观影档案不受影响")
            return Response(success=True, message=f"已移除 {removed} 条失败记录")

    @classmethod
    def _can_match(cls, item) -> bool:
        # 已绑定的档案编辑/取消失败应重试原条目，不能改为操作另一部作品。
        return cls._valid_pending(item) and not item.get("manual_status")

    def api_match_poster(self, payload: dict) -> Response:
        """仅缓存浏览器可用的海报或明确无图的结果，不修改候选、档案和匹配绑定。"""
        sid = payload.get("subject_id")
        poster = posters.normalize_url(payload.get("poster_path"))
        if not isinstance(sid, str) or not re.fullmatch(r"[1-9]\d{0,19}", sid) or poster is None:
            return Response(success=False, message="无效的海报缓存")
        with self._lock:
            cache = self._load_dict("poster_cache")
            if cache.get(sid) != poster:
                cache[sid] = poster
                self.save_data("poster_cache", cache)
        return Response(success=True)

    def api_match_preview(self, payload: dict) -> Response:
        """读取候选供人工核对；兼容升级前未保存候选的队列，不触发同步。"""
        key = payload.get("key")
        if not isinstance(key, str):
            return Response(success=False, message="请选择一条匹配失败记录")
        with self._lock:
            item = deepcopy(self._load_dict("wait").get(key))
        if not self._can_match(item):
            return Response(success=False, message=self._unsupported_status_reason(item) or
                            "此记录已处理或属于档案状态编辑，请重新打开插件查看")
        confirmed = matching.selected_candidate(self._load_dict("manual_matches").get(key))
        candidates = [confirmed] if confirmed else deepcopy(item.get("match_candidates") or [])
        if not isinstance(candidates, list):
            candidates = []
        candidates = [row for row in candidates if isinstance(row, dict)
                      and re.fullmatch(r"[1-9]\d*", str(row.get("subject_id") or ""))][:20]
        if not candidates:
            try:
                with operation("手动匹配", item.get("title") or key):
                    helper = DoubanHelper(self._cookie)
                    subject_id = item.get("subject_id") or item.get("preferred_subject_id")
                    if subject_id:
                        candidate = helper.describe_subject(subject_id)
                        if candidate:
                            candidate["reason"] = "上次自动匹配未通过，请核对名称、类型和年份"
                            candidates = [candidate]
                    else:
                        helper.get_subject_id(item.get("title") or key, year=item.get("year"),
                            mtype=MediaType.TV if item.get("type") in {"TV", "电视剧"} else MediaType.MOVIE,
                            season=item.get("season"))
                        candidates = deepcopy(helper.last_candidates)
            except Exception as error:
                log_event("手动匹配", "匹配", item.get("title") or key, f"候选读取失败（{type(error).__name__}），可重新搜索", level="debug")
        title = item.get("title") or key
        with self._lock:
            cache = self._load_dict("poster_cache")
        cached_posters = {str(row["subject_id"]): posters.normalize_url(cache[str(row["subject_id"])])
                         for row in candidates if str(row["subject_id"]) in cache}
        return Response(success=True, data={"revision": matching.revision(item),
            "keyword": title, "candidates": candidates,
            "poster_cache": {sid: url for sid, url in cached_posters.items() if url is not None},
            "original": {"title": title, "type": {"TV": "电视剧", "MOV": "电影"}.get(item.get("type"), item.get("type")),
                         "year": item.get("year"), "target": self._action_label(item.get("status"))}})

    def api_match_confirm(self, payload: dict) -> Response:
        """人工选择即最终匹配；直接同步，不重新查询/校验匹配或排队重试。"""
        key, revision = payload.get("key"), payload.get("revision")
        candidate = matching.selected_candidate(payload.get("candidate"))
        if not isinstance(key, str) or not isinstance(revision, str) or not candidate:
            return Response(success=False, message="请选择有效豆瓣条目后确认")
        with self._lock:
            result = {"id": uuid4().hex, "started_at": self._now(), "items": []}
            def finish(message, state, success=False, outcome=None):
                result.update(message=message, state=state, finished_at=self._now())
                self._save_manual_result("手动匹配", result, start=True)
                return Response(success=success, message=message, data={"outcome": outcome})
            waiting = self._load_dict("wait")
            item = waiting.get(key)
            if not self._enable:
                return finish("请先在设置中启用插件", "未启动")
            if self._active_edit is not None or self._active_batch is not None:
                return finish("已有任务正在执行，请完成后再确认", "未启动")
            if not self._can_match(item) or matching.revision(item) != revision:
                return finish(self._unsupported_status_reason(item) or
                              "失败记录已变化，请关闭并重新打开手动匹配", "未启动")
            reason = self._unsupported_status_reason({**item, "type": candidate["type"]})
            if reason:
                return finish(reason, "未启动")
            bindings = self._load_dict("manual_matches")
            bindings[key] = candidate
            self.save_data("manual_matches", bindings)
            waiting[key] = self._apply_manual_match(key, item)
            self.save_data("wait", waiting)
            self._log_start("手动匹配", item.get("title") or key, waiting[key].get("status"))
            log_event("手动匹配", "匹配", item.get("title") or key,
                f"已采用 {candidate['title']}（{candidate['year'] or '年份未知'}），豆瓣 ID：{candidate['subject_id']}；人工确认优先")
            result.update(state="等待结果", message=f"手动匹配已提交：{candidate['title']}，等待同步结果")
            self._save_manual_result("手动匹配", result, start=True)
            row = {}
            with operation("手动匹配", item.get("title") or key):
                outcome = self._sync_item(key, waiting[key], self._load_dict("data"), waiting,
                                          task="手动匹配", started=True, result_row=row)
            return finish(row["message"], {"success": "完成", "failed": "失败", "skipped": "已跳过"}[outcome],
                          success=True, outcome=outcome)

    def _request_archive_action(self, selection, action: str, retry_others: bool = False) -> bool:
        """保存选中条目快照；兼容旧版单选值，绝不把空选择解释为全部。"""
        keys = [selection] if isinstance(selection, str) else selection if isinstance(selection, list) else []
        keys = list(dict.fromkeys(k for k in keys if isinstance(k, str) and k))
        groups = self._archive_groups()
        request = {"id": uuid4().hex, "action": action, "started_at": self._now(),
                   "state": "未启动", "retry_others": retry_others, "items": []}
        self._save_manual_result("档案编辑", request, start=True)
        for key in keys:
            group = groups.get(key)
            valid = bool(group and group["subject_id"])
            reason = (self._unsupported_status_reason({**group["record"], "status": action, "manual_status": True}) if valid else
                      "档案不存在或缺少有效豆瓣 ID")
            request["items"].append({"group_key": key, "subject_id": group["subject_id"] if group else None,
                "title": group["title"] if group else key, "state": "等待执行", "skip_reason": reason,
                "message": "等待执行"})
        if action not in {"do", "collect", "remove"} or not keys:
            message = "未执行档案编辑，请选择至少一条有效档案和操作"
        elif not self._enable:
            message = "请先启用插件，再保存需要同步到豆瓣的操作"
        elif all(row["skip_reason"] for row in request["items"]):
            # 全部跳过时不创建调度任务，但仍按原选择顺序输出每项开始与结果。
            self._start_archive_batch(request)
            for index, row in enumerate(request["items"], 1):
                with operation("档案编辑", row["title"], index, len(keys)):
                    self._log_start("档案编辑", row["title"], action)
                    self._skip_archive_row(request, row, row["skip_reason"])
            self._log_archive_summary(request)
            return False
        else:
            request["state"] = "等待执行"
            self._active_edit = request
            self._save_archive_progress(request)
            log_event("档案编辑", "调度", "批量任务", f"已提交 {len(keys)} 条，目标：{self._action_label(action)}", level="debug")
            return True
        self._cancel_archive_rows(request, message)
        request.update(state="未启动", finished_at=self._now())
        if not request["items"]:
            request["message"] = message
        self.save_data("archive_edit", request)
        self._save_manual_result("档案编辑", request)
        if request["items"]:
            self._log_archive_summary(request)
        else:
            log_event("档案编辑", "取消", "批量任务", message)
        return False

    def _start_archive_batch(self, request: dict):
        request["state"] = "执行中"
        if len(request["items"]) > 1:
            log_event("档案编辑", "开始", "批量任务", f"共 {len(request['items'])} 条，"
                        f"目标：{self._action_label(request['action'])}；条目间隔 {self._RETRY_INTERVAL_SECONDS} 秒")
        self._save_archive_progress(request)

    def _skip_archive_row(self, request: dict, row: dict, reason: str):
        message = self._log_result("档案编辑", row["title"], "跳过", reason)
        row.update(state="跳过", message=message, finished_at=self._now())
        self._save_archive_progress(request)

    @staticmethod
    def _log_archive_summary(request: dict):
        if len(request.get("items", [])) > 1:
            logger.info(request["message"])

    def _save_archive_progress(self, request: dict):
        """最近一批结果随每条完成/重试更新，不会覆盖其他批次的结果。"""
        rows = request["items"]
        success = sum(row["state"] == "完成" for row in rows)
        failed = sum(row["state"] == "失败" for row in rows)
        skipped = sum(row["state"] == "跳过" for row in rows)
        cancelled = sum(row["state"] == "已取消" for row in rows)
        pending = len(rows) - success - failed - skipped - cancelled
        request.update(total=len(rows), success=success, failed=failed, skipped=skipped,
                       cancelled=cancelled, pending=pending)
        if not pending:
            request["state"] = ("部分失败" if failed and success else "失败" if failed else
                                "部分完成" if (cancelled or skipped) and success else
                                "已取消" if cancelled else "已跳过" if skipped else "完成")
            request["finished_at"] = self._now()
        if len(rows) == 1:
            request["message"] = rows[0]["message"]
        else:
            request["message"] = self._batch_message("档案编辑", len(rows), success, failed, skipped,
                                                      cancelled, pending)
        self.save_data("archive_edit", request)
        self._save_manual_result("档案编辑", request)

    def _cancel_archive_rows(self, request: dict, message: str):
        for row in request.get("items", []):
            if row["state"] in {"等待执行", "执行中"}:
                result = self._log_result("档案编辑", row["title"], "取消", message)
                row.update(state="已取消", message=result, finished_at=self._now())
        self._save_archive_progress(request)

    def _finish_archive_edit(self, item: dict, state: str, message: str):
        request_id = item.get("manual_request_id")
        latest = self._load_dict("archive_edit")
        if not request_id or latest.get("id") != request_id:
            return
        if self._active_edit and self._active_edit.get("id") == request_id:
            latest = self._active_edit
        if isinstance(latest.get("items"), list):
            for row in latest["items"]:
                if (row["group_key"] == item.get("manual_group_key") or
                        row.get("subject_id") and str(row["subject_id"]) == str(item.get("subject_id"))):
                    row.update(state=state, message=message, finished_at=self._now())
                    self._save_archive_progress(latest)
                    break
        else:
            # 更新前遗留的单条失败任务仍可完成并更新原提示。
            latest.update(state=state, message=message, finished_at=self._now())
            self.save_data("archive_edit", latest)

    def _run_archive_edit(self, request_id: str, retry_others: bool = False):
        with self._lock:
            request, stop = self._active_edit, self._stop_event
            if (not request or request["id"] != request_id or request["state"] != "等待执行"
                    or not self._enable or stop.is_set()):
                return
            self._start_archive_batch(request)
        attempted, succeeded, excluded, helpers = False, False, set(), []
        try:
            for index, row in enumerate(request["items"], 1):
                if row["state"] != "等待执行":
                    continue
                with operation("档案编辑", row["title"], index, len(request["items"])):
                    with self._lock:
                        if stop.is_set() or not self._enable or self._active_edit is not request:
                            break
                        if row.get("skip_reason"):
                            self._log_start("档案编辑", row["title"], request["action"], index, len(request["items"]))
                            self._skip_archive_row(request, row, row["skip_reason"])
                            continue
                    # 间隔不持锁，停用/重新保存设置可中止剩余条目。
                    if attempted and stop.wait(self._RETRY_INTERVAL_SECONDS):
                        break
                    with self._lock:
                        if stop.is_set() or not self._enable or self._active_edit is not request:
                            break
                        item = {"subject_id": row["subject_id"], "manual_request_id": request_id,
                                "manual_group_key": row["group_key"]}
                        self._log_start("档案编辑", row["title"], request["action"], index, len(request["items"]))
                        try:
                            records, waiting = self._load_dict("data"), self._load_dict("wait")
                            group = self._archive_groups(records).get(row["group_key"])
                            if not group or not group["subject_id"]:
                                self._skip_archive_row(request, row, "档案已不存在或缺少有效豆瓣 ID")
                                continue
                            reason = self._unsupported_status_reason({**group["record"], "status": request["action"], "manual_status": True})
                            if reason:
                                self._skip_archive_row(request, row, reason)
                                continue
                            key = group["key"]
                            row.update(state="执行中", message=f"正在处理 {group['title']}，目标：{self._action_label(request['action'])}")
                            self._save_archive_progress(request)
                            item.update(group["record"])
                            # 新操作不能继承档案日期；同目标失败的起点由公共流程从队列恢复。
                            item.pop("timestamp", None)
                            item.update(title=group["title"], subject_id=group["subject_id"], status=request["action"],
                                        manual_status=True, manual_request_id=request_id, manual_group_key=row["group_key"])
                            related = self._related_keys(waiting, set(group["keys"]), group["subject_id"])
                            excluded.update(related | set(group["keys"]))
                            outcome = self._sync_item(key, item, records, waiting, helpers,
                                                      task="档案编辑", started=True)
                            attempted = bool(helpers)
                            succeeded = succeeded or outcome == "success"
                        except Exception as error:
                            message = self._log_result("档案编辑", row["title"], "失败",
                                f"处理异常（{type(error).__name__}）", level="error")
                            self._finish_archive_edit(item, "失败", message)
        finally:
            with self._lock:
                if self._active_edit is request:
                    self._cancel_archive_rows(request, "本轮编辑已中止")
                    self._active_edit = None
                    self._log_archive_summary(request)
                    if not stop.is_set() and (succeeded or retry_others or request.get("retry_after_edit")):
                        # 本批失败不在同一轮立即重试；只遍历其他失败记录一次。
                        self._queue_retry("手动重试" if retry_others or request.get("retry_others") else "同步成功后自动重试",
                                          exclude=excluded, operation_id=request["id"])
