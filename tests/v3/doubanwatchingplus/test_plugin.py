"""宿主入口、V3 媒体身份、媒体库豆瓣 ID、数据隔离与生命周期。"""

from unittest.mock import Mock

import pytest

from helpers import emby_service


def test_v3_identity_and_no_tmdbid(runtime, emit):
    emit()
    call = runtime.recognize.call_args.kwargs
    assert call["media_source"] == runtime.MediaSource.TMDB
    assert call["media_id"] == "123"
    assert "tmdbid" not in call
    runtime.helper.set_watching_status.assert_called_once_with("999", "do", True)


def test_non_tmdb_source_preserved(runtime, emit):
    emit(media_source=runtime.MediaSource.Douban, media_id="456")
    assert runtime.recognize.call_args.kwargs["media_source"] == runtime.MediaSource.Douban
    assert runtime.recognize.call_args.kwargs["media_id"] == "456"


def test_fallback_removes_identity_even_when_host_mutates_meta(runtime):
    meta = runtime.module.MetaInfo("测试剧")
    meta.type = runtime.MediaType.TV
    meta.begin_season = 2
    meta.year = "2025"
    meta.media_source = runtime.MediaSource.TMDB
    meta.media_id = "bad-id"
    def recognize(**kwargs):
        value = kwargs["meta"]
        if kwargs["media_id"]:
            value.media_source = kwargs["media_source"]
            value.media_id = kwargs["media_id"]
            value.begin_season = 99
            value.year = "1900"
            return None
        assert value.media_source is None and value.media_id is None
        assert value.begin_season == 2 and value.year == "2025"
        return runtime.media
    runtime.recognize.side_effect = recognize
    assert runtime.plugin._recognize_media(meta, runtime.MediaSource.TMDB, "7750138") is runtime.media
    assert runtime.recognize.call_count == 2
    assert meta.media_id == "bad-id" and meta.begin_season == 2 and meta.year == "2025"


def test_library_id_uses_event_server_and_series_not_episode(runtime, emit):
    instance, responses = emby_service(runtime, [{"Type": "Series", "ProviderIds": {"Tmdb": "123", "Douban": "777"}}])
    emit(item_id="episode-1", server_name="nas", json_object={"User": {"Id": "user-alice"}, "Item": {"Id": "episode-1", "SeriesId": "series-9"}})
    runtime.services.get_service.assert_called_once_with(name="nas", type_filter="emby")
    assert "/Items/series-9?" in instance.get_data.call_args.kwargs["url"]
    assert runtime.helper.get_subject_id.call_args.kwargs["preferred_id"] == "777"
    responses[0].close.assert_called_once()


def test_library_episode_response_follows_series_and_discards_episode_id(runtime):
    instance, responses = emby_service(runtime, [
        {"Type": "Episode", "SeriesId": "series-9", "ProviderIds": {"Douban": "episode-id"}},
        {"Type": "Series", "ProviderIds": {"douban": "777"}}])
    assert runtime.plugin._library_douban_id(runtime.Webhook(item_id="episode-1")) == "777"
    assert instance.get_data.call_count == 2
    for res in responses:
        res.close.assert_called_once()


def test_library_movie_reads_movie_itself(runtime):
    instance, _ = emby_service(runtime, [{"Type": "Movie", "ProviderIds": {"Douban": "777"}}])
    assert runtime.plugin._library_douban_id(runtime.Webhook(item_type="MOV", item_id="movie-1", json_object={})) == "777"
    assert "/Items/movie-1?" in instance.get_data.call_args.kwargs["url"]


def test_library_does_not_guess_server(runtime):
    instance, _ = emby_service(runtime, [])
    runtime.services.get_services.return_value = {"one": Mock(), "two": Mock()}
    assert runtime.plugin._library_douban_id(runtime.Webhook(item_id="123")) is None
    instance.get_data.assert_not_called()
    runtime.services.get_service.return_value = None
    assert runtime.plugin._library_douban_id(runtime.Webhook(item_id="123", server_name="missing")) is None


def test_library_network_failure_falls_back_to_search(runtime, emit):
    instance, _ = emby_service(runtime, [])
    instance.get_data.side_effect = RuntimeError("private details")
    emit(item_id="123", server_name="nas")
    assert runtime.helper.get_subject_id.call_args.kwargs["preferred_id"] is None
    runtime.helper.set_watching_status.assert_called_once()


def test_library_bad_json_is_closed_and_falls_back(runtime):
    instance, responses = emby_service(runtime, [None])
    responses[0].json.side_effect = ValueError("bad json")
    assert runtime.plugin._library_douban_id(runtime.Webhook(item_id="123")) is None
    responses[0].close.assert_called_once()


@pytest.mark.parametrize("overrides", [{"channel": "jellyfin"}, {"item_id": None}, {"item_id": "../bad"}])
def test_library_skips_unsupported_or_invalid_input(runtime, overrides):
    instance, _ = emby_service(runtime, [])
    values = {"item_id": "123", "json_object": {}, **overrides}
    assert runtime.plugin._library_douban_id(runtime.Webhook(**values)) is None
    instance.get_data.assert_not_called()


@pytest.mark.parametrize("overrides", [{"user_name": "bob"}, {"event": "item.markunplayed"},
                                       {"item_type": "MUSIC"}, {"item_name": None},
                                       {"episode_id": None}, {"episode_id": "invalid"}])
def test_ignored_events(runtime, emit, overrides):
    emit(**overrides)
    runtime.helper_factory.assert_not_called()


def test_disabled_and_stopped(runtime, emit):
    runtime.plugin.init_plugin({"enable": False, "user": "alice"})
    emit()
    runtime.plugin.init_plugin({"enable": True, "user": "alice"})
    runtime.plugin.stop_service()
    runtime.plugin.stop_service()
    emit()
    runtime.recognize.assert_not_called()


def test_filters_ignore_empty_tokens(runtime, emit, caplog):
    caplog.set_level("INFO", logger="doubanwatching-test")
    runtime.plugin.init_plugin({"enable": True, "user": " bob， alice, ", "exclude": " excluded,， "})
    emit(item_path="/excluded/movie")
    assert "[播放同步][跳过] 测试剧 第1季｜命中路径排除关键词" in caplog.text
    assert "/excluded/movie" not in caplog.text
    runtime.helper_factory.assert_not_called()
    emit(item_path=None)
    runtime.helper_factory.assert_called_once()


def test_legacy_data_not_deleted_on_init(runtime):
    data = {"processed": ["legacy"], "data": {"old": {"subject_id": "1"}}, "wait": {"old": {"status": "do"}}}
    runtime.plugin.data = data.copy()
    runtime.plugin.init_plugin({})
    assert runtime.plugin.data == {**data, "timestamp_schema": 1}


def test_legacy_unknown_archive_is_retained_but_does_not_control_sync(runtime, emit):
    runtime.plugin.data["data"] = {"测试剧": {"subject_id": "999", "timestamp": "2025-01-01 00:00:00"},
                                    "corrupt": None}
    emit()
    runtime.helper.set_watching_status.assert_called_once_with("999", "do", True)
    assert runtime.plugin.data["data"]["corrupt"] is None
    assert runtime.plugin.data["data"]["测试剧"] == {"subject_id": "999", "timestamp": "2025-01-01 00:00:00"}
    record = next(value for value in runtime.plugin.data["data"].values()
                  if isinstance(value, dict) and value.get("media_id"))
    assert record["status"] == "do"
    emit()
    assert runtime.helper.set_watching_status.call_count == 1
    runtime.state_result.played = True
    emit(episode_id="3")
    runtime.helper.set_watching_status.assert_called_with("999", "collect", True)
    assert runtime.helper.set_watching_status.call_count == 2


def test_known_collected_subject_not_downgraded_by_other_source(runtime, emit):
    runtime.plugin.data["data"] = {"another-source": {"subject_id": "999", "status": "collect",
                                                        "timestamp": "2025-01-01 00:00:00"}}
    emit()
    runtime.helper.set_watching_status.assert_not_called()


def test_converted_legacy_mapping_is_not_reused(runtime, emit):
    emit()
    key = next(iter(runtime.plugin.data["data"]))
    runtime.plugin.data["data"][key].update(status="legacy", subject_id="777")
    runtime.helper.get_subject_id.reset_mock()
    runtime.helper.set_watching_status.reset_mock()
    emit()
    runtime.helper.get_subject_id.assert_called_once()
    assert runtime.helper.get_subject_id.call_args.kwargs["season"] == 1
    runtime.helper.set_watching_status.assert_called_once_with("999", "do", True)
    assert runtime.plugin.data["data"][key]["subject_id"] == "999"


def test_unknown_recognition_and_exception_are_contained(runtime, emit):
    runtime.recognize.side_effect = [None, None]
    emit()
    runtime.helper_factory.assert_not_called()
    runtime.recognize.side_effect = RuntimeError("bad")
    emit()
    assert runtime.plugin.data == {"timestamp_schema": 1}
