"""Emby 状态查询、通知分流、任务合并及失败计数；不连接真实媒体服务。"""

from copy import deepcopy
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest

from helpers import emby_item, emby_service


@pytest.mark.parametrize("played,resumable", [(False, False), (False, True), (True, False)])
@pytest.mark.parametrize("item_type,emby_type", [("TV", "Episode"), ("MOV", "Movie")])
def test_emby_reads_event_user_and_item_saved_state(runtime, played, resumable, item_type, emby_type):
    item = emby_item(played, Type=emby_type)
    payloads = [item] + ([] if played else [{"Items": [item] if resumable else []}])
    instance, responses = emby_service(runtime, payloads)
    info = runtime.Webhook(item_id="whole-series", server_name="nas", item_type=item_type,
                           json_object={"User": {"Id": "viewer-9"}, "Item": {"Id": "episode-2"},
                                        "PlaybackInfo": {"PlayedToCompletion": not played, "PositionTicks": 999999999999}})
    result = runtime.read_emby_state(info)
    assert (result.played, result.resumable) == (played, resumable)
    urls = [call.kwargs['url'] for call in instance.get_data.call_args_list]
    assert all("/Users/viewer-9/Items" in url for url in urls)
    assert "whole-series" not in str(urls) and "[USER]" not in str(urls)
    if not played:
        # NAS Emby 4.10.1 的 Resume 列表会忽略 Ids；不能据此判断某一集是否可续播。
        assert '/Items?' in urls[1]
        query = parse_qs(urlsplit(urls[1]).query)
        assert query['Ids'] == ['episode-2']
        assert query['Filters'] == ['IsResumable']
        assert query['Recursive'] == ['true']
    for response in responses:
        response.close.assert_called_once()


@pytest.mark.parametrize("payload,reason", [
    ({}, '用户'), ({"User": {"Id": "../other"}, "Item": {"Id": "episode-2"}}, '用户'),
    ({"User": {"Id": "viewer-9"}, "Item": {"SeriesId": "series-123"}}, '条目'),
])
def test_emby_never_substitutes_default_user_or_series(runtime, payload, reason):
    instance, _ = emby_service(runtime, [])
    with pytest.raises(runtime.module.emby.StateError, match=reason):
        runtime.read_emby_state(runtime.Webhook(item_id='series-123', json_object=payload))
    instance.get_data.assert_not_called()


@pytest.mark.parametrize('item', [emby_item(UserData={}), emby_item(UserData={'Played': 'false'}),
                                 emby_item(Id='other'), emby_item(Type='Series')])
def test_emby_invalid_state_is_unknown_not_unplayed(runtime, item):
    instance, responses = emby_service(runtime, [item])
    with pytest.raises(runtime.module.emby.StateError):
        runtime.read_emby_state(runtime.Webhook())
    responses[0].close.assert_called_once()
    assert instance.get_data.call_count == 1


@pytest.mark.parametrize('response', [None, {'Items': None}, {'Items': [{}]}, {'Items': [emby_item(Id='other')]},
                                     {'Items': [emby_item(UserData={})]}])
def test_emby_invalid_resume_response_cannot_allow_sync(runtime, response):
    _, responses = emby_service(runtime, [emby_item(), response])
    with pytest.raises(runtime.module.emby.StateError):
        runtime.read_emby_state(runtime.Webhook())
    for row in responses:
        row.close.assert_called_once()


def test_emby_latest_resume_snapshot_can_confirm_completion(runtime):
    emby_service(runtime, [emby_item(), {'Items': [emby_item(True)]}])
    assert runtime.read_emby_state(runtime.Webhook()).played


@pytest.mark.parametrize('failure', ['http', 'json', 'network', 'missing'])
def test_emby_read_failure_closes_response_and_does_not_leak_credentials(runtime, failure):
    instance, responses = emby_service(runtime, [emby_item()])
    if failure == 'http': responses[0].status_code = 403
    if failure == 'json': responses[0].json.side_effect = ValueError('SECRET')
    if failure == 'network': instance.get_data.side_effect = RuntimeError('SECRET')
    if failure == 'missing': instance.get_data.side_effect = [None]
    with pytest.raises(runtime.module.emby.StateError) as error:
        runtime.read_emby_state(runtime.Webhook())
    assert 'SECRET' not in str(error.value)
    if failure in {'http', 'json'}: responses[0].close.assert_called_once()


def test_emby_rejects_ambiguous_server_and_server_mismatch(runtime):
    instance, _ = emby_service(runtime, [])
    runtime.services.get_services.return_value = {'a': Mock(), 'b': Mock()}
    with pytest.raises(runtime.module.emby.StateError, match='唯一'):
        runtime.read_emby_state(runtime.Webhook())
    instance.get_data.assert_not_called()
    emby_service(runtime, [emby_item(ServerId='wrong-server')])
    info = runtime.Webhook(json_object={'User': {'Id': 'u1'}, 'Item': {'Id': 'episode-2'}, 'Server': {'Id': 'server-1'}})
    with pytest.raises(runtime.module.emby.StateError, match='服务器身份'):
        runtime.read_emby_state(info)


@pytest.mark.parametrize("overrides,expected", [
    ({}, "测试剧 第1季｜开始播放，第2集"),
    ({"season_id": 0, "episode_id": 4}, "测试剧 第0季｜开始播放，第4集"),
    ({"episode_id": 0}, "测试剧 第1季｜开始播放，第0集"),
    ({"season_id": None, "episode_id": None}, "测试剧｜开始播放，集号未知"),
    ({"episode_id": "bad"}, "测试剧 第1季｜开始播放，集号无效"),
    ({"item_type": "MOV", "item_name": "测试电影"}, "测试电影｜开始播放"),
    ({"json_object": {"Item": {"SeriesName": "特别篇", "ParentIndexNumber": 0, "IndexNumber": 0}}},
     "特别篇 第0季｜开始播放，第0集"),
])
def test_emby_start_only_logs_without_queries_jobs_or_data_changes(runtime, caplog, overrides, expected):
    caplog.set_level("INFO", logger="doubanwatching-test")
    runtime.plugin.data.update(data={"saved": {"status": "collect"}}, wait={"failed": {"attempts": 7}})
    before = deepcopy(runtime.plugin.data)
    info = runtime.Webhook(event="playback.start", **overrides)
    original = deepcopy(vars(info))
    runtime.plugin.sync_log(runtime.Event(event_data=info))
    assert [row.getMessage() for row in caplog.records] == [
        f"[播放同步][通知] {expected}；仅记录，停止播放或标记已播放时再同步"]
    assert vars(info) == original and runtime.plugin.data == before
    runtime.state_reader.assert_not_called()
    runtime.services.get_services.assert_not_called()
    runtime.recognize.assert_not_called()
    runtime.helper_factory.assert_not_called()
    runtime.playback_schedule.assert_not_called()
    runtime.schedule.assert_not_called()
    assert not runtime.plugin._playback_jobs


def test_emby_start_stop_only_attempts_once_per_playback(runtime, emit):
    runtime.helper.get_subject_id.return_value = (None, None)
    runtime.helper.last_error = "豆瓣搜索无合格候选"
    for failures in (1, 2):
        before = deepcopy(runtime.plugin.data)
        emit(event="playback.start")
        assert runtime.plugin.data == before
        emit(event="playback.stop")
        waiting = list(runtime.plugin.data["wait"].values())
        assert len(waiting) == 1 and waiting[0]["attempts"] == failures
    assert runtime.state_reader.call_count == 2
    assert runtime.recognize.call_count == 2
    assert runtime.helper.get_subject_id.call_count == 2
    runtime.helper.set_watching_status.assert_not_called()
    runtime.schedule.assert_not_called()


def test_emby_start_does_not_replace_pending_stop_job(runtime):
    runtime.plugin.sync_log(runtime.Event(event_data=runtime.Webhook(event="playback.stop")))
    call = runtime.playback_schedule.call_args
    pending = deepcopy(runtime.plugin._playback_jobs)
    runtime.plugin.sync_log(runtime.Event(event_data=runtime.Webhook(event="playback.start")))
    runtime.playback_schedule.assert_called_once()
    assert runtime.plugin._playback_jobs == pending
    call.args[2](**call.kwargs["func_kwargs"])
    runtime.helper.set_watching_status.assert_called_once_with("999", "do", True)


@pytest.mark.parametrize('event', list(('playback.start', 'playback.pause', 'playback.unpause', 'playback.stop', 'item.markplayed')))
@pytest.mark.parametrize('saved', ['unplayed', 'resumable', 'played'])
def test_emby_events_use_saved_state_not_event_name_or_instant_progress(runtime, emit, monkeypatch, event, saved):
    before = deepcopy(runtime.plugin.data)
    item = emby_item(saved == 'played', IndexNumber=3)
    instance, _ = emby_service(runtime, [item] + ([] if saved == 'played' else [{'Items': [item] if saved == 'resumable' else []}]))
    monkeypatch.setattr(runtime.module.emby, 'read_state', runtime.read_emby_state)
    emit(event=event, json_object={'User': {'Id': 'user-alice'}, 'Item': {'Id': 'episode-2'},
                                 'PlaybackInfo': {'PositionTicks': 1000000000, 'PlayedToCompletion': True}})
    if event in {'playback.start', 'playback.pause', 'playback.unpause'}:
        runtime.playback_schedule.assert_not_called()
        instance.get_data.assert_not_called()
        runtime.helper.set_watching_status.assert_not_called()
        assert runtime.plugin.data == before
        return
    if saved == 'unplayed':
        runtime.helper.set_watching_status.assert_not_called()
        assert not runtime.plugin.data.get('wait')
    else:
        runtime.helper.set_watching_status.assert_called_once_with('999', 'collect' if saved == 'played' else 'do', True)


def test_emby_stop_missing_season_is_filled_from_item(runtime, emit, monkeypatch):
    emby_service(runtime, [emby_item(), {'Items': [emby_item()]}])
    monkeypatch.setattr(runtime.module.emby, 'read_state', runtime.read_emby_state)
    emit(event='playback.stop', season_id=None, episode_id=None)
    runtime.helper.set_watching_status.assert_called_once_with('999', 'do', True)


def test_emby_item_episode_zero_is_preserved_when_webhook_says_first_episode(runtime, emit, monkeypatch, caplog):
    caplog.set_level("INFO", logger="doubanwatching-test")
    emby_service(runtime, [emby_item(True, IndexNumber=0)])
    monkeypatch.setattr(runtime.module.emby, 'read_state', runtime.read_emby_state)
    emit(event='playback.stop', episode_id='1',
         json_object={'User': {'Id': 'user-alice'}, 'Item': {'Id': 'episode-2'}})
    assert '停止播放，第0集；Emby 单集状态：已播放' in caplog.text
    assert '集号为0，跳过自动同步' in caplog.text
    runtime.recognize.assert_not_called()
    runtime.helper_factory.assert_not_called()


def test_emby_failed_state_check_preserves_archives_and_retry_counts(runtime, emit, caplog):
    caplog.set_level('INFO', logger='doubanwatching-test')
    runtime.plugin.data.update(data={'a': {'status': 'collect'}}, wait={'b': {'attempts': 7}})
    before = deepcopy(runtime.plugin.data)
    runtime.state_reader.side_effect = runtime.module.emby.StateError('Emby 状态查询返回 HTTP 503')
    emit(event='playback.stop')
    assert runtime.plugin.data == before
    runtime.recognize.assert_not_called()
    assert '未加入待重试' in caplog.text


def test_emby_duplicate_jobs_coalesce_and_stop_cancels_old_callbacks(runtime):
    event = runtime.Event(event_data=runtime.Webhook())
    runtime.plugin.sync_log(event)
    first = runtime.playback_schedule.call_args
    runtime.plugin.sync_log(event)
    second = runtime.playback_schedule.call_args
    assert first.args[1] == second.args[1] and second.kwargs['delay_seconds'] == 2
    first.args[2](**first.kwargs['func_kwargs'])
    runtime.state_reader.assert_not_called()
    second.args[2](**second.kwargs['func_kwargs'])
    runtime.helper.set_watching_status.assert_called_once()
    runtime.plugin.sync_log(event)
    third = runtime.playback_schedule.call_args
    runtime.plugin.stop_service()
    runtime.cancel.assert_called_with('DoubanWatchingPlus', third.args[1])
    runtime.plugin.init_plugin({'enable': True, 'user': 'alice'})
    third.args[2](**third.kwargs['func_kwargs'])
    runtime.helper.set_watching_status.assert_called_once()


def test_emby_users_get_distinct_check_jobs(runtime):
    for uid in ('u1', 'u2'):
        runtime.plugin.sync_log(runtime.Event(event_data=runtime.Webhook(json_object={'User': {'Id': uid}, 'Item': {'Id': 'episode-2'}})))
    assert len({call.args[1] for call in runtime.playback_schedule.call_args_list}) == 2


def test_emby_schedule_failure_does_not_sync_inline(runtime, emit, caplog):
    caplog.set_level('INFO', logger='doubanwatching-test')
    runtime.playback_schedule.return_value = False
    emit()
    assert not runtime.plugin._playback_jobs
    runtime.state_reader.assert_not_called()
    runtime.helper.set_watching_status.assert_not_called()
    assert '宿主未能安排 Emby 状态检查' in caplog.text
