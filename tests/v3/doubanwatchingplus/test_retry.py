"""失败次数、成功门槛、全量遍历、取消与可见操作的离线行为检查。"""
import json
import threading
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest


def pending(i=1, **kwargs):
    return {"title": f"待处理 {i}", "subject_id": str(1000 + i), "type": "电影",
            "status": "collect", **kwargs}


@pytest.mark.parametrize("season", [0, "0"])
def test_specials_pending_is_visible_but_not_retried_or_rematched(runtime, caplog, season):
    caplog.set_level("INFO", logger="doubanwatching-test")
    item = pending(type="电视剧", season=season, attempts=2, last_error="旧匹配失败")
    runtime.plugin.data["wait"] = {"special": item}
    before = deepcopy(runtime.plugin.data["wait"])
    preview = runtime.plugin.api_match_preview({"key": "special"})
    assert not preview.success and preview.message == "第0季为特别篇，暂不同步"
    assert not runtime.plugin._can_match(item)
    assert runtime.plugin.api_archive().data["waiting"][0]["reason"] == "第0季为特别篇，暂不同步；不会重试，可移除此失败记录"
    runtime.plugin.api_archive_retry()
    runtime.run_batch()
    runtime.helper_factory.assert_not_called()
    assert runtime.plugin.data["wait"] == before
    summary = runtime.plugin.data["retry_summary"]
    assert (summary["attempted"], summary["skipped"], summary["failed"]) == (0, 1, 0)
    assert "第0季为特别篇，暂不同步；失败记录保留" in caplog.text


def test_season_zero_guard_does_not_block_movies_or_explicit_archive_operations(runtime):
    for item in (pending(season=0), pending(type="电视剧", season=0, manual_status=True),
                 pending(type="电视剧", season=0, status="remove", manual_status=True)):
        assert runtime.plugin._valid_pending(item)


@pytest.mark.parametrize("item_type", ["MOV", "TV"])
def test_success_for_movie_or_tv_schedules_all(runtime, emit, item_type):
    runtime.plugin.data["wait"] = {str(i): pending(i, attempts=i) for i in range(7)}
    runtime.state_result.played = item_type == "MOV"
    emit(item_type=item_type)
    assert runtime.schedule.call_count == 1
    assert runtime.helper.set_watching_status.call_count == 1  # 后台任务尚未运行
    runtime.helper.set_watching_status.side_effect = [False, True, False, True, True, False, True]
    runtime.run_batch()
    assert runtime.helper.set_watching_status.call_count == 8
    assert set(runtime.plugin.data["wait"]) == {"0", "2", "5"}
    assert [runtime.plugin.data["wait"][str(i)]["attempts"] for i in [0, 2, 5]] == [1, 3, 6]
    summary = runtime.plugin.data["retry_summary"]
    assert (summary["attempted"], summary["success"], summary["failed"], summary["remaining"]) == (7, 4, 3, 3)
    assert summary["state"] == "完成"
    assert runtime.schedule.call_count == 1  # 批次里的成功不再递归触发
    assert runtime.helper_factory.call_count == 2  # 当前事件一次，整轮共用一次


def test_current_failure_does_not_trigger_others(runtime, emit):
    runtime.plugin.data["wait"] = {"old": pending()}
    runtime.helper.set_watching_status.return_value = False
    emit()
    runtime.schedule.assert_not_called()
    assert runtime.plugin.data["wait"]["old"] == pending()
    assert runtime.helper.set_watching_status.call_count == 1


def test_unknown_legacy_does_not_block_write_or_success_trigger(runtime, emit):
    runtime.plugin.data.update(data={"legacy": {"subject_id": "999"}}, wait={"old": pending()})
    emit()
    runtime.helper.set_watching_status.assert_called_once_with("999", "do", True)
    runtime.schedule.assert_called_once()
    assert runtime.plugin.data["data"]["legacy"] == {"subject_id": "999"}
    assert runtime.plugin.data["wait"] == {"old": pending()}


def test_count_has_no_cap_and_success_clears_failure_metadata(runtime, emit):
    runtime.helper.set_watching_status.return_value = False
    for _ in range(25):
        emit()
    key, item = next(iter(runtime.plugin.data["wait"].items()))
    assert item["attempts"] == 25
    assert item["last_attempt"] and item["last_error"]
    assert runtime.helper.set_watching_status.call_count == 25
    runtime.helper.set_watching_status.return_value = True
    emit()
    assert runtime.plugin.data["wait"] == {}
    assert not {"attempts", "last_attempt", "last_error", "next_retry"}.intersection(runtime.plugin.data["data"][key])
    runtime.schedule.assert_not_called()  # 成功的当前项已出队，不会额外再写一次


def test_corrupt_count_is_recovered_once_without_deleting_record(runtime, emit):
    runtime.helper.set_watching_status.return_value = False
    emit()
    key = next(iter(runtime.plugin.data["wait"]))
    runtime.plugin.data["wait"][key]["attempts"] = "invalid"
    emit()
    assert runtime.plugin.data["wait"][key]["attempts"] == 1


def test_helper_initialization_exception_counts_once_and_hides_secrets(runtime, emit, caplog):
    runtime.helper_factory.side_effect = RuntimeError("cookie=top-secret")
    emit()
    item = next(iter(runtime.plugin.data["wait"].values()))
    assert item["attempts"] == 1
    assert item["last_error"] == "同步异常（RuntimeError）"
    assert "top-secret" not in json.dumps(runtime.plugin.data) + caplog.text


def test_search_failure_counts_once_per_sync_attempt(runtime, emit):
    runtime.helper.get_subject_id.return_value = (None, None)
    runtime.helper.last_error = "豆瓣名称、类型或年份没有唯一匹配"
    emit()
    assert len(runtime.plugin.data["wait"]) == 1
    assert next(iter(runtime.plugin.data["wait"].values()))["attempts"] == 1
    runtime.helper.set_watching_status.assert_not_called()
    emit()
    item = next(iter(runtime.plugin.data["wait"].values()))
    assert item["attempts"] == 2 and item["last_error"] == runtime.helper.last_error
    runtime.helper.set_watching_status.assert_not_called()


def test_manual_retry_remove_resets_actions_and_preserves_archive(runtime):
    runtime.plugin.data.update(wait={"keep": pending(), "remove": pending(2)}, data={"saved": pending(3)})
    archive = deepcopy(runtime.plugin.data["data"])
    runtime.plugin.init_plugin({"enable": True, "user": "alice", "cookie": "private",
                                "retry_now": True, "retry_remove": ["remove"], "retry_interval": 0})
    assert runtime.plugin.config["retry_now"] is False
    assert runtime.plugin.config["retry_remove"] == []
    assert runtime.plugin.config["cookie"] == "private"
    assert runtime.plugin.data["data"] == archive
    assert set(runtime.plugin.data["wait"]) == {"keep"}
    runtime.run_batch()
    assert runtime.plugin.data["wait"] == {}
    calls = runtime.schedule.call_count
    runtime.plugin.init_plugin(runtime.plugin.config)
    assert runtime.schedule.call_count == calls


def test_manual_retry_disabled_is_visible_without_attempt(runtime):
    runtime.plugin.data["wait"] = {"old": pending(attempts=12)}
    runtime.plugin.init_plugin({"enable": False, "retry_now": True})
    assert runtime.plugin.data["retry_summary"]["state"] == "未启动"
    assert runtime.plugin.data["wait"]["old"]["attempts"] == 12
    runtime.schedule.assert_not_called()
    runtime.helper_factory.assert_not_called()


@pytest.mark.parametrize("failure", [False, RuntimeError("private")])
def test_scheduler_failure_is_visible_and_does_not_count(runtime, failure):
    runtime.plugin.data["wait"] = {"old": pending(attempts=12)}
    if isinstance(failure, Exception):
        runtime.schedule.side_effect = failure
    else:
        runtime.schedule.return_value = failure
    runtime.plugin._queue_retry("手动重试")
    assert runtime.plugin.data["retry_summary"]["state"] == "未启动"
    assert runtime.plugin._active_batch is None
    assert runtime.plugin.data["wait"]["old"]["attempts"] == 12


def test_satisfied_and_corrupt_entries_not_counted_as_failure(runtime):
    runtime.plugin.data.update(wait={"done": pending(attempts=20), "corrupt": None,
                                    "badstatus": pending(2, status="bad", attempts=12), "ok": pending(3)},
                              data={"other": pending(status="collect")})
    runtime.plugin._queue_retry("手动重试")
    runtime.run_batch()
    assert set(runtime.plugin.data["wait"]) == {"corrupt", "badstatus"}
    assert runtime.plugin.data["wait"]["badstatus"]["attempts"] == 12
    summary = runtime.plugin.data["retry_summary"]
    assert (summary["attempted"], summary["success"], summary["skipped"], summary["cancelled"]) == (1, 1, 3, 0)


def test_queued_tasks_are_not_duplicated_and_old_callback_cannot_run_new_batch(runtime):
    runtime.plugin.data["wait"] = {"old": pending()}
    runtime.plugin._queue_retry("手动重试")
    old = deepcopy(runtime.schedule.call_args.kwargs["func_kwargs"])
    runtime.plugin._queue_retry("同步成功后自动重试")
    assert runtime.schedule.call_count == 1
    runtime.plugin.stop_service()
    runtime.cancel.assert_called_once()
    runtime.plugin.init_plugin({"enable": True, "retry_interval": 0})
    runtime.plugin._queue_retry("手动重试")
    runtime.plugin._run_retry(**old)
    runtime.helper_factory.assert_not_called()
    runtime.run_batch()
    runtime.helper.set_watching_status.assert_called_once()


def test_request_spacing_can_be_interrupted_without_losing_remaining_items(runtime):
    del runtime.plugin._RETRY_INTERVAL_SECONDS  # 使用正式固定值，旧配置不能覆盖。
    runtime.plugin.init_plugin({"enable": True, "retry_interval": 30})
    runtime.plugin.data["wait"] = {str(i): pending(i) for i in range(3)}
    waiting = threading.Event()
    original_wait = runtime.plugin._stop_event.wait
    def wait(seconds):
        assert seconds == 3
        waiting.set()
        return original_wait(seconds)
    runtime.plugin._stop_event.wait = wait
    runtime.plugin._queue_retry("手动重试")
    assert runtime.schedule.call_args.kwargs["delay_seconds"] == 3
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(runtime.run_batch)
        assert waiting.wait(3)
        runtime.plugin.stop_service()
        future.result(timeout=3)
    runtime.helper.set_watching_status.assert_called_once()
    assert len(runtime.plugin.data["wait"]) == 2
    assert runtime.plugin.data["retry_summary"]["state"] == "已停止"


def test_worker_rereads_changed_item_before_retry(runtime):
    runtime.plugin.data["wait"] = {"old": pending(status="do")}
    runtime.plugin._queue_retry("手动重试")
    runtime.plugin.data["wait"]["old"]["status"] = "collect"
    runtime.run_batch()
    runtime.helper.set_watching_status.assert_called_once_with("1001", "collect", True)


def test_details_show_retry_status_without_extra_settings(runtime):
    runtime.plugin.data["wait"] = {"old": pending(attempts=24, last_attempt="2026-09-30 12:00:00", last_error="HTTP 403"), "invalid": None}
    data = runtime.plugin.api_archive().data
    assert len(data["waiting"]) == 2
    assert data["waiting"][0]["attempts"] == 24
    assert data["waiting"][0]["reason"] == "HTTP 403"
    assert "retry_summary" not in data
    assert "retry_interval" not in runtime.plugin.get_form()[1]
    runtime.helper_factory.assert_not_called()


def test_dedup_cleans_stale_failure_without_triggering_batch(runtime, emit):
    emit()
    key = next(iter(runtime.plugin.data["data"]))
    runtime.plugin.data["wait"] = {key: pending(status="do", attempts=20), "old": pending(2)}
    emit()
    assert set(runtime.plugin.data["wait"]) == {"old"}
    runtime.schedule.assert_not_called()


def test_internal_403_retry_is_one_sync_failure_then_success_clears_reason(runtime, emit, monkeypatch):
    helper = runtime.helper_module.DoubanHelper("ck=secret; dbcl2=secret")
    helper.get_subject_id = Mock(return_value=("测试剧", "999"))
    def response(code):
        return Mock(status_code=code, headers={"Set-Cookie": "ck=new; Path=/"}, json=Mock(return_value={"r": 0}))
    helper._request = Mock(side_effect=[response(403), response(200), response(403), response(200)])
    runtime.helper_factory.return_value = helper
    emit()
    failed = next(iter(runtime.plugin.data["wait"].values()))
    assert failed["attempts"] == 1 and failed["last_error"] == "豆瓣状态写入失败，HTTP 403"
    assert helper._request.call_count == 3
    emit()
    assert runtime.plugin.data["wait"] == {}
    assert helper.last_error == ""


def test_waiting_interval_does_not_restrict_current_failed_event(runtime, emit):
    runtime.plugin.init_plugin({"enable": True, "user": "alice", "retry_interval": 30})
    runtime.helper.set_watching_status.return_value = False
    emit()
    emit()
    assert runtime.helper.set_watching_status.call_count == 2
    runtime.schedule.assert_not_called()


def test_failed_sync_retries_immediately_and_preserves_upgrade(runtime, emit, caplog):
    caplog.set_level("INFO", logger="doubanwatching-test")
    runtime.helper.set_watching_status.return_value = False
    emit()
    assert not runtime.plugin.data.get("data")
    pending = next(iter(runtime.plugin.data["wait"].values()))
    assert "next_retry" not in pending
    pending["next_retry"] = 9999999999  # 升级前保存的未来等待时间也不能阻止再次播放。
    emit()
    assert runtime.helper.set_watching_status.call_count == 2
    assert "next_retry" not in next(iter(runtime.plugin.data["wait"].values()))
    runtime.state_result.played = True
    emit(episode_id="3")
    assert runtime.helper.set_watching_status.call_count == 3
    runtime.state_result.played = False
    runtime.helper.set_watching_status.return_value = True
    emit()
    assert runtime.helper.set_watching_status.call_count == 4
    assert runtime.helper.set_watching_status.call_args.args[1] == "collect"
    assert runtime.plugin.data["wait"] == {}
    assert "已加入待重试" in caplog.text
    assert "重试等待期" not in caplog.text
    assert "其他条目同步成功或手动重试时再尝试" not in caplog.text
    assert caplog.text.count("[播放同步][失败] ") == 3
    assert "累计失败 3 次" in caplog.text


def test_retry_visits_all_legacy_queue(runtime, caplog):
    caplog.set_level("INFO", logger="doubanwatching-test")
    waiting = {str(i): {"subject_id": str(i + 1), "status": "collect", "type": "MOV",
                        "next_retry": 9999999999} for i in range(5)}
    records = {}
    runtime.plugin.data.update(wait=waiting, data=records)
    runtime.plugin._queue_retry("手动重试")
    runtime.run_batch()
    assert len(runtime.plugin.data["wait"]) == 0
    records = runtime.plugin.data["data"]
    assert len(records) == 5
    assert all(value["type"] == "电影" for value in records.values())
    assert sum(record.message.startswith("[失败重试][开始] ") and "批量任务" not in record.message for record in caplog.records) == 5


def test_retry_does_not_downgrade_collected_subject(runtime):
    waiting = {"旧标题": {"subject_id": "999", "status": "do"}}
    records = {"identity": {"subject_id": "999", "status": "collect"}}
    runtime.plugin.data.update(wait=waiting, data=records)
    runtime.plugin._queue_retry("手动重试")
    runtime.run_batch()
    assert runtime.plugin.data["wait"] == {}
    runtime.helper_factory.assert_not_called()


def test_dedup_does_not_trigger_old_queue(runtime, emit):
    emit()
    runtime.helper.set_watching_status.reset_mock()
    runtime.plugin.data["wait"] = {"old-movie": {"title": "另一部电影", "subject_id": "888",
        "status": "collect", "type": "MOV", "next_retry": 9999999999}}
    emit()
    runtime.helper.set_watching_status.assert_not_called()
    runtime.schedule.assert_not_called()
    assert "old-movie" in runtime.plugin.data["wait"]
