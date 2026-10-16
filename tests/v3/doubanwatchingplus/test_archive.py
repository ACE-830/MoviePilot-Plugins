"""档案统计与单项、批量编辑：状态、日期、重试衔接及中断处理。"""
from copy import deepcopy
import json
import threading
from unittest.mock import Mock

import pytest

from helpers import archive_item as item, request_archive_edit as request, run_archive_job as run_job


# 档案统计与单项编辑

def test_stats_deduplicate_subjects_and_do_not_guess_old_state(runtime):
    runtime.plugin.data["data"] = {
        "old": item("1"), "alias": item("1", "do"), "finished": item("2", "collect"),
        "unknown": item("3"), "legacy": item("4", "legacy"), "broken": None}
    before = deepcopy(runtime.plugin.data)
    groups = runtime.plugin._archive_groups()
    assert len(groups) == 5
    assert [sum(g["status"] == state for g in groups.values()) for state in ["do", "collect", "unknown"]] == [1, 1, 3]
    stats = runtime.module.views.archive_stats(runtime.plugin._archive_groups())
    labels = [(c["content"][0]["content"][0]["text"], c["content"][0]["content"][1]["text"]) for c in stats["content"]]
    assert labels == [("全部档案", "5"), ("在看", "1"), ("看过", "1"), ("未确认", "3")]
    assert runtime.plugin.data == before
    runtime.helper_factory.assert_not_called()


@pytest.mark.parametrize("status", ["do", "collect"])
def test_edit_updates_aliases_and_date_only_after_real_state_change(runtime, status, monkeypatch):
    monkeypatch.setattr(runtime.plugin, "_now", lambda: "2026-09-30 12:00:00")
    previous = "do" if status == "collect" else "collect"
    runtime.plugin.data["data"] = {"old": item(), "identity": item(status=previous), "other": item("888")}
    before = deepcopy(runtime.plugin.data["data"])
    request(runtime, status)
    assert runtime.plugin.data["data"] == before
    assert runtime.plugin.config["archive_action"] is None and runtime.plugin.config["archive_item"] == []
    run_job(runtime, "archive_edit")
    runtime.helper.set_watching_status.assert_called_once_with("999", status, True)
    runtime.helper.get_subject_id.assert_not_called()
    assert runtime.plugin.data["data"]["other"] == before["other"]
    for key in ["old", "identity"]:
        record = runtime.plugin.data["data"][key]
        assert record["status"] == status and record["timestamp"] == "2026-09-30 12:00:00"
        assert record["subject_name"] == before[key]["subject_name"]
        assert "status_updated_at" not in record
        assert not {"manual_status", "manual_request_id", "attempts", "last_error"} & record.keys()
    assert runtime.plugin.data["archive_edit"]["state"] == "完成"
    runtime.plugin.init_plugin(runtime.plugin.config)
    assert runtime.plugin.get_service() == []  # 一次性动作已经复位，不会重启重复提交。


@pytest.mark.parametrize("action", ["do", "collect", "remove"])
def test_specials_skip_preserves_explicit_existing_archive_operations(runtime, action):
    previous = "do" if action == "collect" else "collect"
    runtime.plugin.data["data"] = {"saved": {**item(status=previous), "season": 0}}
    request(runtime, action)
    run_job(runtime, "archive_edit")
    assert runtime.plugin.data["archive_edit"]["state"] == "完成"
    runtime.helper.get_subject_id.assert_not_called()
    if action == "remove":
        runtime.helper.clear_watching_status.assert_called_once_with("999")
        assert runtime.plugin.data["data"] == {}
    else:
        runtime.helper.set_watching_status.assert_called_once_with("999", action, True)
        assert runtime.plugin.data["data"]["saved"]["status"] == action


def test_failed_downgrade_keeps_archive_then_retry_applies_manual_target(runtime):
    runtime.plugin.data["data"] = {"saved": item(status="collect")}
    before = deepcopy(runtime.plugin.data["data"])
    runtime.helper.set_watching_status.return_value = False
    runtime.helper.last_error = "豆瓣状态写入失败，HTTP 403"
    request(runtime, "do")
    run_job(runtime, "archive_edit")
    assert runtime.plugin.data["data"] == before
    queued = runtime.plugin.data["wait"]["saved"]
    assert queued["manual_status"] is True and queued["status"] == "do" and queued["attempts"] == 1
    assert runtime.plugin.data["archive_edit"]["state"] == "失败"
    runtime.helper.set_watching_status.return_value = True
    runtime.plugin._queue_retry("手动重试")
    run_job(runtime, "retry_waiting")
    assert runtime.plugin.data["data"]["saved"]["status"] == "do"
    assert runtime.plugin.data["data"]["saved"]["timestamp"] == queued["timestamp"]
    assert runtime.plugin.data["wait"] == {}
    assert runtime.plugin.data["archive_edit"]["state"] == "完成"
    assert runtime.helper.set_watching_status.call_count == 2


def test_event_dedup_cannot_suppress_pending_manual_change(runtime, emit):
    key = "themoviedb:123|TV|S1"
    runtime.plugin.data["data"] = {key: item(status="collect")}
    runtime.helper.set_watching_status.return_value = False
    request(runtime, "do")
    run_job(runtime, "archive_edit")
    runtime.helper.set_watching_status.return_value = True
    emit(episode_id="3")  # 自动事件想设看过，先完成用户未完成的在看修改。
    assert [c.args[1] for c in runtime.helper.set_watching_status.call_args_list] == ["do", "do"]
    assert runtime.plugin.data["data"][key]["status"] == "do"
    assert runtime.plugin.data["wait"] == {}


def test_new_identity_lookup_also_respects_pending_manual_target(runtime, emit):
    runtime.plugin.data["data"] = {"old-title": item(status="collect")}
    runtime.helper.set_watching_status.return_value = False
    request(runtime, "do")
    run_job(runtime, "archive_edit")
    runtime.helper.set_watching_status.return_value = True
    emit(episode_id="3")
    assert runtime.helper.set_watching_status.call_args.args[1] == "do"
    assert runtime.plugin.data["data"]["old-title"]["status"] == "do"
    assert runtime.plugin.data["wait"] == {}


def test_new_manual_target_replaces_conflicting_wait_entries_without_losing_failure_count(runtime):
    runtime.plugin.data.update(data={"old": item(), "identity": item(status="collect")},
        wait={"old": {**item(status="do"), "attempts": 2}, "identity": {**item(status="collect"), "attempts": 3},
              "other": {**item("888", "do"), "attempts": 1}})
    runtime.helper.set_watching_status.return_value = False
    request(runtime, "do")
    run_job(runtime, "archive_edit")
    mine = [v for v in runtime.plugin.data["wait"].values() if v["subject_id"] == "999"]
    assert len(mine) == 1 and mine[0]["status"] == "do" and mine[0]["attempts"] == 6
    assert runtime.plugin.data["wait"]["other"]["attempts"] == 1


def test_removal_clears_aliases_only_after_remote_confirmation(runtime):
    records = {"old": item(), "alias": item(status="collect"), "other": item("888")}
    runtime.plugin.data.update(data=deepcopy(records), wait={"old": {"status": "do"},
        "different-key": {**item(status="collect"), "attempts": 3}, "other": {**item("888", "do"), "attempts": 2}})
    request(runtime, "remove")
    assert runtime.plugin.data["data"] == records
    runtime.helper.clear_watching_status.return_value = True
    run_job(runtime, "archive_edit")
    assert runtime.plugin.data["data"] == {"other": records["other"]}
    assert set(runtime.plugin.data["wait"]) == {"other"}
    assert "archive_last_removed" not in runtime.plugin.data
    runtime.helper.clear_watching_status.assert_called_once_with("999")
    runtime.helper.set_watching_status.assert_not_called()
    assert "已取消" in runtime.plugin.data["archive_edit"]["message"]


@pytest.mark.parametrize("overrides", [{"enable": False}, {"archive_item": "douban:123"}, {"archive_action": "unexpected"}])
def test_invalid_or_disabled_edit_is_visible_and_does_not_write(runtime, overrides):
    runtime.plugin.data["data"] = {"saved": item()}
    original = deepcopy(runtime.plugin.data["data"])
    config = {"enable": True, "archive_item": "douban:999", "archive_action": "do", **overrides}
    runtime.plugin.init_plugin(config)
    assert runtime.plugin.data["data"] == original
    expected = "已跳过" if overrides.get("archive_item") == "douban:123" else "未启动"
    assert runtime.plugin.data["archive_edit"]["state"] == expected
    runtime.helper_factory.assert_not_called()
    runtime.schedule.assert_not_called()


def test_record_without_id_cannot_be_removed_or_synced(runtime):
    runtime.plugin.data["data"] = {"bad": {"title": "坏数据"}}
    request(runtime, "do", "record:bad")
    assert runtime.plugin.data["archive_edit"]["state"] == "已跳过"
    runtime.schedule.assert_not_called()
    request(runtime, "remove", "record:bad", enable=False)
    assert "bad" in runtime.plugin.data["data"]
    assert runtime.plugin.data["archive_edit"]["state"] == "未启动"


def test_edit_is_registered_after_init_not_as_immediate_once_job(runtime):
    from datetime import datetime
    runtime.plugin.data["data"] = {"saved": item(status="collect")}
    request(runtime, "do")
    runtime.schedule.assert_not_called()
    runtime.helper_factory.assert_not_called()
    service = runtime.plugin.get_service()[0]
    assert service["trigger"] == "date" and service["id"] == "archive_edit"
    assert service["kwargs"]["misfire_grace_time"] is None
    assert 0 < (service["kwargs"]["run_date"] - datetime.now().astimezone()).total_seconds() <= 3
    run_job(runtime, "archive_edit")
    assert runtime.plugin.get_service() == []
    assert runtime.plugin.data["archive_edit"]["state"] == "完成"


def test_stop_and_stale_edit_callback_cannot_apply_new_request(runtime):
    runtime.plugin.data["data"] = {"saved": item()}
    request(runtime, "do")
    old_call = runtime.plugin.get_service()[0]
    runtime.plugin.stop_service()
    assert runtime.plugin.data["archive_edit"]["state"] == "已取消"
    runtime.cancel.assert_called_with("DoubanWatchingPlus", "archive_edit")
    request(runtime, "collect")
    old_call["func"](**old_call["func_kwargs"])
    runtime.helper_factory.assert_not_called()
    run_job(runtime, "archive_edit")
    runtime.helper.set_watching_status.assert_called_once_with("999", "collect", True)


def test_missing_archive_after_queueing_skips_without_remote_write(runtime):
    runtime.plugin.data["data"] = {"saved": item()}
    request(runtime, "do")
    runtime.plugin.data["data"] = {}
    run_job(runtime, "archive_edit")
    runtime.helper_factory.assert_not_called()
    assert runtime.plugin.data["archive_edit"]["state"] == "已跳过"


def test_manual_success_triggers_others_and_failure_only_if_explicitly_requested(runtime):
    runtime.plugin.data.update(data={"saved": item()}, wait={"other": {**item("888", "collect"), "attempts": 1}})
    runtime.helper.set_watching_status.return_value = False
    request(runtime, "do", retry_now=True)
    run_job(runtime, "archive_edit")
    retry = runtime.schedule.call_args
    assert retry.args[1] == "retry_waiting" and retry.kwargs["func_kwargs"]["keys"] == ["other"]
    runtime.helper.set_watching_status.return_value = True
    run_job(runtime, "retry_waiting")
    assert set(runtime.plugin.data["wait"]) == {"saved"}
    runtime.plugin._queue_retry("手动重试")
    run_job(runtime, "retry_waiting")
    assert runtime.plugin.data["wait"] == {}


def test_archive_and_settings_tabs_are_separate(runtime):
    runtime.plugin.data["data"] = {"saved": item(status="do")}
    form, defaults = runtime.plugin.get_form()
    assert "cookie" in defaults and defaults["archive_item"] == []
    assert "全部档案" in json.dumps(form, ensure_ascii=False)
    assert runtime.plugin.get_page() is None and runtime.plugin.get_render_mode() == ("vuetify", None)
    assert runtime.plugin.api_archive().data["records"][0]["status"] == "do"


def test_failed_clear_keeps_archive_and_retries_without_marking_watched(runtime):
    runtime.plugin.data["data"] = {"saved": item(status="collect"), "alias": item()}
    before = deepcopy(runtime.plugin.data["data"])
    runtime.helper.clear_watching_status.return_value = False
    runtime.helper.last_error = "豆瓣取消后状态无法确认"
    request(runtime, "remove")
    run_job(runtime, "archive_edit")
    assert runtime.plugin.data["data"] == before
    pending = next(iter(runtime.plugin.data["wait"].values()))
    assert pending["status"] == "remove" and pending["attempts"] == 1
    assert runtime.plugin.api_archive().data["waiting"][0]["target"] == "取消豆瓣标记"
    assert runtime.plugin.data["archive_edit"]["state"] == "失败"
    runtime.helper.clear_watching_status.return_value = True
    runtime.plugin._queue_retry("手动重试")
    run_job(runtime, "retry_waiting")
    assert runtime.plugin.data["data"] == {} and runtime.plugin.data["wait"] == {}
    runtime.helper.set_watching_status.assert_not_called()
    assert runtime.helper.clear_watching_status.call_count == 2
    assert runtime.plugin.data["archive_edit"]["state"] == "完成"


def test_new_edit_overrides_failed_clear(runtime):
    runtime.plugin.data["data"] = {"saved": item(status="do")}
    runtime.helper.clear_watching_status.return_value = False
    request(runtime, "remove")
    run_job(runtime, "archive_edit")
    request(runtime, "collect")
    run_job(runtime, "archive_edit")
    assert runtime.plugin.data["data"]["saved"]["status"] == "collect"
    assert runtime.plugin.data["wait"] == {}
    runtime.helper.set_watching_status.assert_called_once_with("999", "collect", True)


def test_saving_settings_does_not_submit_hidden_management_actions(runtime):
    runtime.plugin.data["data"] = {"saved": item(status="do")}
    runtime.plugin.data["wait"] = {"other": {**item("888", "collect"), "attempts": 1}}
    before = deepcopy(runtime.plugin.data)
    request(runtime, "remove", ["douban:999"], _ui_tab="settings", cookie="new-cookie",
            retry_now=True, retry_remove=["other"])
    assert runtime.plugin.data == before
    assert runtime.plugin.config["cookie"] == "new-cookie"
    assert "_ui_tab" not in runtime.plugin.config
    assert runtime.plugin.config["archive_item"] == [] and runtime.plugin.config["archive_action"] is None
    assert runtime.plugin.get_service() == []
    runtime.helper_factory.assert_not_called()
    runtime.schedule.assert_not_called()
    assert runtime.plugin.get_form()[1]["_ui_tab"] == "archive"


def test_management_tab_submits_and_resets_ui_state(runtime):
    runtime.plugin.data["data"] = {"saved": item(status="do")}
    request(runtime, "collect", ["douban:999"], _ui_tab="archive")
    assert "_ui_tab" not in runtime.plugin.config
    run_job(runtime, "archive_edit")
    runtime.helper.set_watching_status.assert_called_once_with("999", "collect", True)
    assert runtime.plugin.data["archive_edit"]["state"] == "完成"
    runtime.plugin.init_plugin(runtime.plugin.config)
    assert runtime.plugin.get_service() == []
    assert runtime.plugin.get_form()[1]["_ui_tab"] == "archive"


@pytest.mark.parametrize("pending_status", ["do", "collect", "remove"])
def test_same_state_skips_write_keeps_date_and_clears_pending(runtime, pending_status):
    original = item(status="collect")
    runtime.plugin.data.update(data={"saved": original}, wait={"alias": {
        **item(status=pending_status), "manual_status": True, "attempts": 3}})
    request(runtime, "collect")
    run_job(runtime, "archive_edit")
    runtime.helper_factory.assert_not_called()
    assert runtime.plugin.data["data"]["saved"] == original
    assert runtime.plugin.data["wait"] == {}
    assert runtime.plugin.data["archive_edit"]["skipped"] == 1
    runtime.schedule.assert_not_called()


def test_first_attempt_date_survives_resubmit_retry_and_success(runtime, monkeypatch):
    clock = ["2026-08-01 12:00:00"]
    monkeypatch.setattr(runtime.plugin, "_now", lambda: clock[0])
    original = item(status="do")
    runtime.plugin.data["data"] = {"saved": original}
    runtime.helper.set_watching_status.return_value = False
    request(runtime, "collect")
    run_job(runtime, "archive_edit")
    assert runtime.plugin.data["data"]["saved"] == original
    assert runtime.plugin.data["wait"]["saved"]["timestamp"] == clock[0]
    clock[0] = "2026-09-01 12:00:00"
    request(runtime, "collect")  # 再次提交相同目标不会刷新首次时间。
    run_job(runtime, "archive_edit")
    assert runtime.plugin.data["wait"]["saved"]["timestamp"] == "2026-08-01 12:00:00"
    assert runtime.plugin.data["wait"]["saved"]["attempts"] == 2
    assert runtime.plugin.data["wait"]["saved"]["last_attempt"] == clock[0]
    runtime.helper.set_watching_status.return_value = True
    runtime.plugin._queue_retry("手动重试")
    run_job(runtime, "retry_waiting")
    saved = runtime.plugin.data["data"]["saved"]
    assert saved["status"] == "collect" and saved["timestamp"] == "2026-08-01 12:00:00"
    assert runtime.plugin.data["wait"] == {}


def test_changed_pending_goal_starts_new_date(runtime, monkeypatch):
    monkeypatch.setattr(runtime.plugin, "_now", lambda: "2026-09-30 12:00:00")
    runtime.plugin.data.update(data={"saved": item()}, wait={"saved": {
        **item(status="do", stamp="2026-08-01 12:00:00"), "manual_status": True, "attempts": 2}})
    runtime.helper.set_watching_status.return_value = False
    request(runtime, "collect")
    run_job(runtime, "archive_edit")
    pending = runtime.plugin.data["wait"]["saved"]
    assert pending["status"] == "collect" and pending["timestamp"] == "2026-09-30 12:00:00"
    assert pending["attempts"] == 3


# 批量编辑与并发边界

def seed_bulk_archive(runtime):
    runtime.plugin.data["data"] = {"a": item("1", "do", "电影甲"), "b": item("2", "collect", "电影乙"),
                                   "c": item("3", None, "电影丙"), "alias": item("1", None, "电影甲")}


@pytest.mark.parametrize("with_tv", [False, True])
def test_every_selected_movie_has_start_and_skip_logs_without_writes(runtime, caplog, with_tv):
    caplog.set_level("INFO", logger="doubanwatching-test")
    runtime.plugin.data["data"] = {
        "movie": {**item("1", "collect", "电影甲"), "type": "电影"},
        "tv": item("2", "collect", "剧集乙"),
    }
    before = deepcopy(runtime.plugin.data["data"])
    selection = ["douban:1", "douban:2"] if with_tv else ["douban:1"]
    request(runtime, "do", selection)
    if with_tv:
        run_job(runtime, "archive_edit")
        runtime.helper.set_watching_status.assert_called_once_with("2", "do", True)
    else:
        assert runtime.plugin.get_service() == []
        runtime.helper_factory.assert_not_called()
    lines = [record.message for record in caplog.records]
    start = next(i for i, line in enumerate(lines) if line.startswith("[档案编辑][开始] 电影甲"))
    skipped = next(i for i, line in enumerate(lines) if line.startswith("[档案编辑][跳过] 电影甲"))
    assert start < skipped
    assert sum(line.startswith("[档案编辑][跳过] 电影甲") for line in lines) == 1
    assert "电影不支持在看" in lines[skipped]
    assert runtime.plugin.data["data"]["movie"] == before["movie"]
    assert runtime.plugin.data.get("wait", {}) == {}
    result = runtime.plugin.data["archive_edit"]
    assert (result["skipped"], result["cancelled"], result["failed"]) == (1, 0, 0)
    assert result["items"][0]["message"] == lines[skipped]
    assert sum(line.startswith("[档案编辑][汇总] 批量任务｜") for line in lines) == int(with_tv)


def test_single_edit_result_is_not_repeated_as_a_summary(runtime, caplog):
    caplog.set_level("INFO", logger="doubanwatching-test")
    runtime.plugin.data["data"] = {"saved": item("1", "do", "剧集甲")}
    request(runtime, "collect", ["douban:1"])
    run_job(runtime, "archive_edit")
    lines = [record.message for record in caplog.records]
    expected = "[档案编辑][成功] 剧集甲｜已同步豆瓣看过"
    assert lines.count(expected) == 1
    assert runtime.plugin.data["archive_edit"]["message"] == expected
    assert not any(line.startswith("[档案编辑][汇总] 批量任务｜") for line in lines)


@pytest.mark.parametrize("action", ["do", "collect", "remove"])
def test_bulk_only_changes_distinct_selected_subjects_after_success(runtime, action):
    seed_bulk_archive(runtime)
    if action != "remove":
        # 本用例检查实际变更；同状态跳过另有独立覆盖。
        for value in runtime.plugin.data["data"].values():
            value["status"] = "collect" if action == "do" else "do"
    before = deepcopy(runtime.plugin.data["data"])
    runtime.helper.clear_watching_status.return_value = True
    request(runtime, action, ["douban:1", "douban:2", "douban:1"])
    assert runtime.plugin.data["data"] == before
    assert runtime.plugin.data["archive_edit"]["total"] == 2
    run_job(runtime, "archive_edit")
    archive = runtime.plugin.data["data"]
    assert archive["c"] == before["c"]
    if action == "remove":
        assert set(archive) == {"c"}
        assert runtime.helper.clear_watching_status.call_args_list[0].args == ("1",)
        assert runtime.helper.clear_watching_status.call_count == 2
        assert "archive_last_removed" not in runtime.plugin.data
    else:
        assert [c.args[:2] for c in runtime.helper.set_watching_status.call_args_list] == [("1", action), ("2", action)]
        assert all(archive[k]["status"] == action for k in ("a", "b", "alias"))
        assert all(archive[k]["timestamp"] != before[k]["timestamp"] for k in ("a", "b", "alias"))
    result = runtime.plugin.data["archive_edit"]
    assert (result["state"], result["success"], result["failed"], result["pending"]) == ("完成", 2, 0, 0)
    assert all(row["state"] == "完成" for row in result["items"])
    runtime.helper_factory.assert_called_once()


def test_partial_failure_continues_does_not_immediately_retry_same_batch_and_later_updates_results(runtime):
    seed_bulk_archive(runtime)
    runtime.plugin.data["data"]["a"]["status"] = "collect"
    before = deepcopy(runtime.plugin.data["data"])
    runtime.plugin.data["wait"] = {"outside": {**item("4", "do", "其他电影"), "attempts": 1}}
    runtime.helper.set_watching_status.side_effect = [True, False, True]
    runtime.helper.last_error = "HTTP 403"
    request(runtime, "do", ["douban:1", "douban:2", "douban:3"])
    run_job(runtime, "archive_edit")
    result = runtime.plugin.data["archive_edit"]
    assert (result["state"], result["success"], result["failed"]) == ("部分失败", 2, 1)
    assert [row["state"] for row in result["items"]] == ["完成", "失败", "完成"]
    assert runtime.plugin.data["data"]["b"] == before["b"]
    assert runtime.plugin.data["wait"]["b"]["attempts"] == 1
    assert runtime.schedule.call_count == 1
    assert runtime.schedule.call_args.kwargs["func_kwargs"]["keys"] == ["outside"]
    runtime.helper.set_watching_status.side_effect = None
    run_job(runtime, "retry_waiting")
    runtime.plugin._queue_retry("手动重试")
    run_job(runtime, "retry_waiting")
    result = runtime.plugin.data["archive_edit"]
    assert result["state"] == "完成" and result["success"] == 3 and result["failed"] == 0
    assert runtime.plugin.data["data"]["b"]["status"] == "do"
    assert runtime.plugin.data["wait"] == {}


def test_bulk_clear_failure_preserves_only_failed_archive_then_retry_removes_it(runtime):
    seed_bulk_archive(runtime)
    runtime.helper.clear_watching_status.side_effect = [True, False, True]
    runtime.helper.last_error = "取消后回查无法确认"
    request(runtime, "remove", ["douban:1", "douban:2", "douban:3"])
    run_job(runtime, "archive_edit")
    assert set(runtime.plugin.data["data"]) == {"b"}
    assert runtime.plugin.data["wait"]["b"]["status"] == "remove"
    runtime.schedule.assert_not_called()
    runtime.helper.clear_watching_status.side_effect = None
    runtime.helper.clear_watching_status.return_value = True
    runtime.plugin._queue_retry("手动重试")
    run_job(runtime, "retry_waiting")
    assert runtime.plugin.data["data"] == {} and runtime.plugin.data["wait"] == {}
    assert runtime.plugin.data["archive_edit"]["success"] == 3
    assert "archive_last_removed" not in runtime.plugin.data
    runtime.helper.set_watching_status.assert_not_called()


@pytest.mark.parametrize("selection", [[], None, "", [None, {}, 10]])
def test_empty_or_invalid_selection_never_means_all(runtime, selection):
    seed_bulk_archive(runtime)
    before = deepcopy(runtime.plugin.data["data"])
    request(runtime, "remove", selection)
    assert runtime.plugin.data["archive_edit"]["state"] == "未启动"
    assert runtime.plugin.get_service() == []
    assert runtime.plugin.data["data"] == before
    runtime.helper_factory.assert_not_called()


def test_missing_and_malformed_targets_are_visible_without_false_failure_counts(runtime):
    seed_bulk_archive(runtime)
    runtime.plugin.data["data"]["bad"] = {"title": "缺少 ID"}
    request(runtime, "collect", ["douban:1", "record:bad", "douban:999", "douban:2"])
    runtime.plugin.data["data"].pop("b")
    run_job(runtime, "archive_edit")
    result = runtime.plugin.data["archive_edit"]
    assert (result["state"], result["success"], result["skipped"], result["cancelled"], result["failed"]) == ("部分完成", 1, 3, 0, 0)
    assert not runtime.plugin.data.get("wait")
    runtime.helper.set_watching_status.assert_called_once_with("1", "collect", True)


def test_fixed_spacing_reloads_data_and_defers_event_triggered_retry(runtime, monkeypatch):
    seed_bulk_archive(runtime)
    runtime.plugin.data["data"]["b"]["status"] = "do"
    request(runtime, "collect", ["douban:1", "douban:2", "douban:3"])
    monkeypatch.setattr(runtime.plugin, "_RETRY_INTERVAL_SECONDS", 3)
    def interval(seconds):
        assert seconds == 3
        runtime.plugin.data["data"]["external"] = item("88", "collect", "同时加入的档案")
        runtime.plugin.data["wait"]["outside"] = {**item("89", "do"), "attempts": 1}
        runtime.plugin._queue_retry("同步成功后自动重试")
        runtime.schedule.assert_not_called()
        return False
    wait = Mock(side_effect=interval)
    monkeypatch.setattr(runtime.plugin._stop_event, "wait", wait)
    run_job(runtime, "archive_edit")
    assert wait.call_count == 2
    assert runtime.plugin.data["data"]["external"]["subject_id"] == "88"
    assert runtime.schedule.call_count == 1
    assert runtime.schedule.call_args.kwargs["func_kwargs"]["keys"] == ["outside"]


def test_stop_during_interval_preserves_completed_items_and_cancels_remaining(runtime, monkeypatch):
    seed_bulk_archive(runtime)
    request(runtime, "collect", ["douban:1", "douban:2", "douban:3"])
    monkeypatch.setattr(runtime.plugin, "_RETRY_INTERVAL_SECONDS", 3)
    service = runtime.plugin.get_service()[0]
    stop = runtime.plugin._stop_event
    original_wait = stop.wait
    spacing = threading.Event()
    def wait(seconds):
        spacing.set()
        return original_wait(seconds)
    monkeypatch.setattr(stop, "wait", wait)
    worker = threading.Thread(target=service["func"], kwargs=service["func_kwargs"], daemon=True)
    worker.start()
    try:
        assert spacing.wait(2)
        runtime.plugin.stop_service()
        worker.join(timeout=2)
        assert not worker.is_alive()
    finally:
        stop.set()
        worker.join(timeout=2)
    result = runtime.plugin.data["archive_edit"]
    assert (result["success"], result["cancelled"], result["failed"], result["pending"]) == (1, 2, 0, 0)
    runtime.helper.set_watching_status.assert_called_once_with("1", "collect", True)
    assert runtime.plugin.data["data"]["c"].get("status") is None
    assert not runtime.plugin.data.get("wait")
    runtime.schedule.assert_not_called()


def test_stale_batch_and_retry_cannot_overwrite_new_batch_result(runtime):
    seed_bulk_archive(runtime)
    runtime.plugin.data["data"]["a"]["status"] = "collect"
    runtime.helper.set_watching_status.return_value = False
    request(runtime, "do", ["douban:1", "douban:2"])
    old_service = runtime.plugin.get_service()[0]
    run_job(runtime, "archive_edit")
    old_item = deepcopy(runtime.plugin.data["wait"]["a"])
    request(runtime, "collect", ["douban:3"])
    current = deepcopy(runtime.plugin.data["archive_edit"])
    old_service["func"](**old_service["func_kwargs"])
    runtime.plugin._finish_archive_edit(old_item, "完成", "旧请求已完成")
    assert runtime.plugin.data["archive_edit"] == current


def test_bulk_results_available_in_details(runtime):
    seed_bulk_archive(runtime)
    request(runtime, "collect", ["douban:1", "douban:2"])
    run_job(runtime, "archive_edit")
    data = runtime.plugin.api_archive().data
    assert len(data["records"]) == 3
    assert len(data["latest"]["items"]) == 2
    assert [row["state"] for row in data["latest"]["items"]] == ["完成", "跳过"]
    assert "全部档案" not in json.dumps(runtime.plugin.get_dashboard(), ensure_ascii=False)
