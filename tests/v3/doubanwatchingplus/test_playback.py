"""播放同步规则：季集边界、目标状态、去重及其他媒体服务器兼容。"""

import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest


@pytest.mark.parametrize("event", ["playback.stop", "item.markplayed"])
@pytest.mark.parametrize("played", [False, True])
def test_first_episode_of_multi_episode_season_always_skipped(runtime, emit, caplog, event, played):
    caplog.set_level("INFO", logger="doubanwatching-test")
    runtime.state_result.played = played
    before = deepcopy(runtime.plugin.data)
    emit(event=event, episode_id="1")
    assert "已设置不标记第一集" in caplog.text
    runtime.recognize.assert_called_once()
    runtime.helper_factory.assert_not_called()
    assert runtime.plugin.data == before


@pytest.mark.parametrize("episodes", [[1], [], None, [0, "invalid"], ["1", "1"]])
@pytest.mark.parametrize("played,resumable,status", [(False, False, None), (False, True, "do"), (True, False, "collect")])
def test_first_episode_single_or_unknown_season_follows_emby(runtime, emit, episodes, played, resumable, status):
    # 第一季有多集不影响当前第二季的单集/未知集数判断。
    runtime.media.seasons = {1: [1, 2, 3], "2": episodes}
    runtime.state_result.played = played
    runtime.state_result.resumable = resumable
    emit(event="playback.stop", season_id="2", episode_id="1")
    if status is None:
        runtime.helper.set_watching_status.assert_not_called()
    else:
        runtime.helper.set_watching_status.assert_called_once_with("999", status, True)


def test_first_episode_switch_disabled_allows_multi_episode_season(runtime, emit):
    runtime.plugin.init_plugin({"enable": True, "user": "alice", "first": False})
    runtime.state_result.played = True
    emit(event="item.markplayed", episode_id="1")
    runtime.helper.set_watching_status.assert_called_once_with("999", "do", True)


@pytest.mark.parametrize("event,label", [("playback.stop", "停止播放"),
                                        ("item.markplayed", "标记播放完成")])
@pytest.mark.parametrize("played,resumable", [(False, False), (False, True), (True, False)])
@pytest.mark.parametrize("first", [False, True])
@pytest.mark.parametrize("episode", [0, "0"])
def test_episode_zero_is_logged_and_skipped_without_changing_data(
        runtime, emit, caplog, event, label, played, resumable, first, episode):
    caplog.set_level("INFO", logger="doubanwatching-test")
    runtime.plugin.init_plugin({"enable": True, "user": "alice", "first": first})
    runtime.state_result.played = played
    runtime.state_result.resumable = resumable
    runtime.plugin.data.update(data={"existing": {"status": "collect", "timestamp": "2026-01-01 12:00:00"}},
                               wait={"failed": {"attempts": 2, "last_error": "之前失败"}})
    before = deepcopy(runtime.plugin.data)
    emit(event=event, episode_id=episode)
    assert f"[播放同步][开始] 测试剧 第1季｜{label}，第0集；Emby 单集状态：" in caplog.text
    assert "[播放同步][跳过] 测试剧 第1季｜集号为0，跳过自动同步" in caplog.text
    assert "缺少季集信息" not in caplog.text and "已设置不标记第一集" not in caplog.text
    runtime.recognize.assert_not_called()
    runtime.helper_factory.assert_not_called()
    runtime.schedule.assert_not_called()
    assert runtime.plugin.data == before


@pytest.mark.parametrize("field", ["season_id", "episode_id"])
@pytest.mark.parametrize("value,reason", [(None, "缺少季集信息"), ("", "缺少季集信息"),
                                         ("  ", "缺少季集信息"), (-1, "季集信息无效"),
                                         ("invalid", "季集信息无效"), (1.5, "季集信息无效"),
                                         (False, "季集信息无效")])
def test_missing_and_invalid_season_episode_have_distinct_logs(runtime, emit, caplog, field, value, reason):
    caplog.set_level("INFO", logger="doubanwatching-test")
    runtime.state_result.resumable = False
    before = deepcopy(runtime.plugin.data)
    emit(**{field: value})
    assert f"｜{reason}" in caplog.text
    if field == "episode_id":
        label = "集号未知" if reason == "缺少季集信息" else "集号无效"
        assert f"停止播放，{label}；Emby 单集状态：" in caplog.text
        assert "第0集" not in caplog.text
    else:
        assert "停止播放，第2集；Emby 单集状态：" in caplog.text
    runtime.recognize.assert_not_called()
    runtime.helper_factory.assert_not_called()
    assert runtime.plugin.data == before


@pytest.mark.parametrize("season", [0, "0"])
@pytest.mark.parametrize("episode", [0, 1, 4])
@pytest.mark.parametrize("event", ["playback.stop", "item.markplayed"])
@pytest.mark.parametrize("played,resumable", [(False, False), (False, True), (True, False)])
def test_season_zero_is_skipped_with_its_own_reason(runtime, emit, caplog, season, episode, event, played, resumable):
    caplog.set_level("INFO", logger="doubanwatching-test")
    runtime.plugin.init_plugin({"enable": True, "user": "alice", "first": False})
    runtime.state_result.played = played
    runtime.state_result.resumable = resumable
    before = deepcopy(runtime.plugin.data)
    emit(event=event, season_id=season, episode_id=episode)
    assert f"，第{episode}集；Emby 单集状态：" in caplog.text
    assert "[播放同步][跳过] 测试剧 第0季｜第0季为特别篇，暂不同步" in caplog.text
    assert "集号为0" not in caplog.text
    runtime.recognize.assert_not_called()
    runtime.helper_factory.assert_not_called()
    assert runtime.plugin.data == before


@pytest.mark.parametrize("episodes,episode,expected", [([0, 1], 1, "collect"),
                                                      ([0, 1, 2], 1, None),
                                                      ([0, 1, 2], 2, "collect")])
def test_episode_zero_does_not_count_toward_first_or_final_episode(runtime, emit, episodes, episode, expected):
    runtime.media.seasons = {1: episodes}
    runtime.state_result.played = True
    emit(event="item.markplayed", episode_id=episode)
    if expected is None:
        runtime.helper_factory.assert_not_called()
    else:
        runtime.helper.set_watching_status.assert_called_once_with("999", expected, True)


def test_missing_sxx_name_does_not_crash(runtime, emit):
    emit(item_name="测试剧")
    assert runtime.plugin.data["data"]


def test_progression_dedup_and_no_downgrade(runtime, emit, caplog):
    caplog.set_level("INFO", logger="doubanwatching-test")
    emit()
    emit()
    runtime.state_result.played = True
    emit(episode_id="3")
    runtime.state_result.played = False
    emit(episode_id="2")
    assert [call.args[1] for call in runtime.helper.set_watching_status.call_args_list] == ["do", "collect"]
    assert "[播放同步][跳过] 测试剧 第1季｜本地已记录为在看" in caplog.text
    assert "[播放同步][跳过] 测试剧 第1季｜本地已记录为看过" in caplog.text
    assert caplog.text.count("[播放同步][开始] 测试剧") == 4


def test_movie_requires_saved_played_state(runtime, emit, caplog):
    caplog.set_level("INFO", logger="doubanwatching-test")
    emit(item_type="MOV", item_name="测试电影")
    runtime.helper.set_watching_status.assert_not_called()
    assert "电影尚未标记已播放" in caplog.text
    runtime.state_result.played = True
    emit(item_type="MOV", item_name="测试电影")
    assert "[播放同步][开始] 测试电影｜停止播放" in caplog.text
    assert "[播放同步][成功]" in caplog.text
    assert "匹配到豆瓣条目" not in caplog.text
    runtime.helper.set_watching_status.assert_called_once_with("999", "collect", True)


@pytest.mark.parametrize("episodes,status", [([], "do"), ([1, 4], "do"), ([1, 2], "collect")])
def test_final_episode_uses_actual_number(runtime, emit, episodes, status):
    runtime.media.seasons = {1: episodes}
    runtime.state_result.played = True
    emit()
    assert runtime.helper.set_watching_status.call_args.args[1] == status


def test_jellyfin_unplayed_not_synced(runtime, emit):
    emit(event="UserDataSaved", channel="jellyfin", save_reason="TogglePlayed", json_object={"Played": False})
    runtime.helper_factory.assert_not_called()
    emit(event="UserDataSaved", channel="jellyfin", save_reason="TogglePlayed", json_object={"Played": True})
    runtime.helper_factory.assert_called_once()


def test_same_title_different_media_not_deduped(runtime, emit):
    emit()
    runtime.media.media_id = "456"
    emit(media_id="456")
    assert len(runtime.plugin.data["data"]) == 2


def test_concurrent_duplicate_events_write_once(runtime, emit):
    barrier = threading.Barrier(4)
    def send():
        barrier.wait(timeout=5)
        emit()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(send) for _ in range(4)]
        for future in futures:
            future.result(timeout=10)
    runtime.helper.set_watching_status.assert_called_once()


@pytest.mark.parametrize("channel,event", [("jellyfin", "PlaybackStart"), ("plex", "media.play")])
def test_other_servers_keep_original_playback_rules(runtime, emit, channel, event):
    emit(channel=channel, event=event)
    runtime.state_reader.assert_not_called()
    runtime.helper.set_watching_status.assert_called_once_with("999", "do", True)
