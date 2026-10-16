"""独立离线测试：只替换宿主边界，按生产包名加载完整插件。"""

import copy
import importlib.util
import logging
import socket
import sys
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """任何意外网络调用立即失败，不使用真实账号或宿主数据库。"""
    def denied(*args, **kwargs):
        raise AssertionError("离线测试禁止网络访问")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.fixture
def runtime(monkeypatch):
    """装配隔离存储及严格 V3 识别签名，测试结束恢复 sys.modules。"""
    class MediaType(Enum):
        TV = "电视剧"
        MOVIE = "电影"

    class MediaSource(Enum):
        TMDB = "themoviedb"
        Douban = "douban"

    class Base:
        def __init__(self):
            self.data = {}
            self.foreign_data = {}
            self.foreign_config = {}
            self.config = {}

        def update_config(self, config):
            self.config = copy.deepcopy(config)

        def get_data(self, key, plugin_id=None):
            values = self.foreign_data.get(plugin_id, {}) if plugin_id else self.data
            return copy.deepcopy(values.get(key))

        def get_config(self, plugin_id=None):
            return copy.deepcopy(self.foreign_config.get(plugin_id) if plugin_id else self.config)

        def save_data(self, key, value):
            self.data[key] = copy.deepcopy(value)

        def del_data(self, key):
            self.data.pop(key, None)

    class Webhook(SimpleNamespace):
        def __init__(self, **kwargs):
            values = dict(event="playback.stop", channel="emby", item_type="TV", item_name="测试剧 S01E02",
                          season_id="1", episode_id="2", item_path="/media/show", user_name="alice",
                          media_source=MediaSource.TMDB, media_id="123", save_reason=None, json_object={"User": {"Id": "user-alice"}, "Item": {"Id": "episode-2", "SeriesId": "series-123"}},
                          item_id=None, server_name=None)
            values.update(kwargs)
            super().__init__(**values)

        @classmethod
        def model_validate(cls, data):
            return cls(**data)

    def key(source, media_id):
        if not source or media_id in (None, "", "0", 0):
            return ""
        return f"{getattr(source, 'value', source)}:{media_id}"

    media = SimpleNamespace(media_source=MediaSource.TMDB, media_id="123", title="测试剧", year="2024",
                            seasons={1: [1, 2, 3], 2: [1, 2, 3]}, season_years={1: "2024", 2: "2025"},
                            poster_path="https://example.invalid/poster.jpg")
    recognize = Mock(return_value=media)
    search = Mock(return_value=[])
    services = Mock()
    services.get_services.return_value = {}
    services.get_service.return_value = None

    class Chain:
        def recognize_media(self, meta=None, mtype=None, media_source=None, media_id=None,
                            episode_group=None, cache=True, share_meta=None, music_type=None):
            return recognize(meta=meta, mtype=mtype, media_source=media_source, media_id=media_id, cache=cache)

        def search_medias(self, meta, media_source=None):
            return search(meta=meta, media_source=media_source)

    manager = Mock()
    manager.get_plugin_state.return_value = False
    schedule = Mock(return_value=True)
    cancel = Mock()
    playback_schedule = Mock(return_value=True)
    def add_job(*args, **kwargs):
        target = playback_schedule if str(args[1]).startswith("playback_") else schedule
        return target(*args, **kwargs)
    scheduler = SimpleNamespace(add_plugin_once_job=add_job, remove_plugin_once_job=cancel, remove_plugin_job=Mock())
    modules = {
        "app": {}, "app.chain": {}, "app.chain.media": {"MediaChain": Chain},
        "app.plugins": {}, "app.schemas": {"WebhookEventInfo": Webhook, "Response": SimpleNamespace},
        "app.schemas.types": {"EventType": SimpleNamespace(WebhookMessage="webhook"), "MediaType": MediaType,
                              "MediaSource": MediaSource},
        "app.sdk": {"scheduler": scheduler}, "app.sdk.plugin": {"_PluginBase": Base, "PluginManager": Mock(return_value=manager)},
        "app.sdk.events": {"Event": SimpleNamespace, "eventmanager": SimpleNamespace(register=lambda _: lambda f: f)},
        "app.sdk.logging": {"logger": logging.getLogger("doubanwatching-test")},
        "app.sdk.media": {"MetaInfo": lambda title: SimpleNamespace(title=title, name=title, type=None,
                          media_source=None, media_id=None, year=None, begin_season=None), "build_media_key": key},
        "app.sdk.services": {"MediaServerHelper": Mock(return_value=services)},
        "app.sdk.config": {"settings": SimpleNamespace(USER_AGENT="test-agent")},
        "app.sdk.network": {"RequestUtils": Mock(side_effect=AssertionError("HTTP must be mocked"))},
        "app.adapters": {}, "app.adapters.external": {},
        "app.adapters.external.cookiecloud": {"CookieCloudHelper": Mock()},
    }
    for name, symbols in modules.items():
        module = ModuleType(name)
        module.__path__ = []
        module.__dict__.update(symbols)
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).resolve().parents[3] / "plugins.v3" / "doubanwatchingplus" / "__init__.py"
    spec = importlib.util.spec_from_file_location("app.plugins.doubanwatchingplus", path)
    plugin_module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, plugin_module)
    # 按依赖顺序装配所有子模块，保证每个测试使用独立的宿主替身与模块状态。
    loaded = {}
    for leaf in ("log", "emby", "douban", "migration", "posters", "matching", "views"):
        name = spec.name + "." + leaf
        child_spec = importlib.util.spec_from_file_location(name, path.with_name(leaf + ".py"))
        child = importlib.util.module_from_spec(child_spec)
        monkeypatch.setitem(sys.modules, name, child)
        child_spec.loader.exec_module(child)
        loaded[leaf] = child
    helper_module, emby_module = loaded["douban"], loaded["emby"]
    spec.loader.exec_module(plugin_module)
    read_emby_state = emby_module.read_state
    state_result = emby_module.PlaybackState(False, True, {})
    state_reader = Mock(return_value=state_result)
    monkeypatch.setattr(emby_module, "read_state", state_reader)
    plugin = plugin_module.DoubanWatchingPlus()
    plugin.init_plugin({"enable": True, "user": "alice"})
    helper = Mock()
    helper.last_error = ""
    helper.get_subject_id.return_value = ("测试剧", "999")
    helper.set_watching_status.return_value = True
    monkeypatch.setattr(plugin, "_RETRY_INTERVAL_SECONDS", 0)
    helper_factory = Mock(return_value=helper)
    monkeypatch.setattr(plugin_module, "DoubanHelper", helper_factory)
    return SimpleNamespace(plugin=plugin, module=plugin_module, helper_module=helper_module,
                           helper=helper, helper_factory=helper_factory, media=media, recognize=recognize,
                           Webhook=Webhook, Event=SimpleNamespace, MediaSource=MediaSource, MediaType=MediaType,
                           search=search, services=services, schedule=schedule, cancel=cancel, manager=manager,
                           playback_schedule=playback_schedule, state_result=state_result, state_reader=state_reader,
                           read_emby_state=read_emby_state,
                           run_batch=lambda: plugin._run_retry(**schedule.call_args.kwargs["func_kwargs"]))


@pytest.fixture
def emit(runtime):
    """创建播放事件。"""
    def send(**kwargs):
        before = runtime.playback_schedule.call_count
        runtime.plugin.sync_log(runtime.Event(event_data=runtime.Webhook(**kwargs)))
        if runtime.playback_schedule.call_count > before and runtime.playback_schedule.return_value:
            call = runtime.playback_schedule.call_args
            call.args[2](**call.kwargs["func_kwargs"])
    return send
