"""一次性原版导入：无外部网络，真实执行入口、存储核对和中断恢复。"""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from helpers import form_nodes as nodes

SOURCE = 'DouBanWatching'


def source(runtime):
    runtime.plugin.foreign_data[SOURCE] = {
        'data': {'旧剧': {'subject_id': '111', 'subject_name': '旧剧', 'type': '电视剧',
                        'timestamp': '2025-03-20 12:00:00', 'poster_path': 'https://invalid/one.jpg'},
                 '旧电影': {'subject_id': '222', 'subject_name': '旧电影', 'type': '电影',
                          'timestamp': '2025-05-20 12:00:00', 'poster_path': 'https://invalid/two.jpg'}},
        'wait': {'失败剧': {'subject_id': '333', 'subject_name': '失败剧', 'status': 'do', 'type': 'TV'}}}
    runtime.plugin.foreign_config[SOURCE] = {
        'enable': True, 'private': False, 'first': False, 'user': 'original-user', 'exclude': 'trailer',
        'cookie': 'private-cookie', 'pc_month': 6, 'pc_num': 30, 'mobile_month': 3, 'mobile_num': 12,
        'retry_now': True, 'archive_action': 'remove', 'archive_item': ['douban:111']}
    return deepcopy(runtime.plugin.foreign_data), deepcopy(runtime.plugin.foreign_config)


def run_import(runtime, **extra):
    runtime.plugin.init_plugin({'_ui_tab': 'settings', 'import_legacy': True, 'enable': True,
                                'user': 'new-user', **extra})


def test_import_preserves_all_original_information_and_does_not_sync(runtime, caplog):
    caplog.set_level('INFO', logger='doubanwatching-test')
    old_data, old_config = source(runtime)
    run_import(runtime)
    data = runtime.plugin.data
    assert data['data'] == old_data[SOURCE]['data']
    assert data['wait'] == old_data[SOURCE]['wait']
    assert data['timestamp_schema'] == 1
    assert data['legacy_import']['state'] == '完成'
    assert (data['legacy_import']['records'], data['legacy_import']['waiting'], data['legacy_import']['unknown']) == (2, 1, 2)
    assert 'legacy_import_journal' not in data
    for key in ['user', 'cookie', 'private', 'first', 'exclude', 'pc_month', 'pc_num', 'mobile_month', 'mobile_num']:
        assert runtime.plugin.config[key] == old_config[SOURCE][key]
    assert runtime.plugin.config['enable'] is False and not runtime.plugin.get_state()
    assert not runtime.plugin.config['import_legacy'] and not runtime.plugin.config['retry_now']
    assert runtime.plugin.config['archive_action'] is None and runtime.plugin.config['archive_item'] == []
    assert runtime.plugin.foreign_data == old_data and runtime.plugin.foreign_config == old_config
    assert all(row['status'] == 'unknown' for row in runtime.plugin.api_archive().data['records'])
    assert '2025年05月' in str(runtime.plugin.get_line_item())
    assert 'private-cookie' not in caplog.text and 'private-cookie' not in str(data['legacy_import'])
    assert '[原版导入][开始]' in caplog.text and '[原版导入][成功]' in caplog.text
    runtime.helper_factory.assert_not_called()
    runtime.schedule.assert_not_called()


def test_repeat_and_reload_never_read_or_overwrite_source_again(runtime):
    source(runtime); run_import(runtime)
    before = deepcopy(runtime.plugin.data)
    original_get = runtime.plugin.get_data
    def no_cross_read(key, plugin_id=None):
        assert plugin_id is None
        return original_get(key)
    runtime.plugin.get_data = no_cross_read
    runtime.plugin.get_config = Mock(return_value=deepcopy(runtime.plugin.config))
    runtime.plugin.init_plugin(runtime.plugin.config)
    run_import(runtime)
    assert runtime.plugin.data == before
    runtime.plugin.get_config.assert_not_called()
    runtime.helper_factory.assert_not_called()


@pytest.mark.parametrize('key,value', [('data', {'existing': {'status': 'collect'}}), ('wait', {'failed': {'status': 'do'}}), ('data', []), ('wait', 'bad')])
def test_existing_target_is_never_overwritten(runtime, key, value):
    source(runtime)
    runtime.plugin.data[key] = deepcopy(value)
    run_import(runtime)
    assert runtime.plugin.data[key] == value
    assert runtime.plugin.data['legacy_import']['state'] == '未执行'
    assert '已有档案或待重试' in runtime.plugin.data['legacy_import']['message']
    assert runtime.plugin.config['user'] == 'new-user'
    runtime.manager.get_plugin_state.assert_not_called()


@pytest.mark.parametrize('problem', ['active', 'missing', 'processed', 'data_list', 'wait_list', 'config_list'])
def test_unsupported_source_refuses_whole_import_without_loss(runtime, problem):
    source(runtime)
    if problem == 'active': runtime.manager.get_plugin_state.return_value = True
    elif problem == 'missing': runtime.plugin.foreign_data = {}
    elif problem == 'processed': runtime.plugin.foreign_data[SOURCE]['processed'] = ['older-schema']
    elif problem == 'config_list': runtime.plugin.foreign_config[SOURCE] = []
    else: runtime.plugin.foreign_data[SOURCE][problem.split('_')[0]] = []
    old = deepcopy(runtime.plugin.foreign_data)
    run_import(runtime)
    assert runtime.plugin.data['legacy_import']['state'] == '未执行'
    assert 'data' not in runtime.plugin.data and 'wait' not in runtime.plugin.data
    assert runtime.plugin.foreign_data == old
    runtime.helper_factory.assert_not_called()


def test_empty_dict_source_is_supported_without_inventing_records(runtime):
    source(runtime)
    runtime.plugin.foreign_data[SOURCE] = {'data': {}, 'wait': {}}
    run_import(runtime)
    assert runtime.plugin.data['legacy_import']['records'] == 0
    assert runtime.plugin.data['legacy_import']['state'] == '完成'


def test_import_applies_date_normalization_even_after_empty_target_initialized(runtime):
    source(runtime)
    assert runtime.plugin.data['timestamp_schema'] == 1
    runtime.plugin.foreign_data[SOURCE]['data']['旧剧']['status_updated_at'] = '2026-09-30 12:00:00'
    runtime.plugin.foreign_data[SOURCE]['wait']['失败剧'].update(timestamp='2025-01-01 12:00:00', last_attempt='2026-09-29 12:00:00', attempts=5)
    run_import(runtime)
    assert runtime.plugin.data['data']['旧剧']['timestamp'] == '2025-03-20 12:00:00'
    assert 'status_updated_at' not in runtime.plugin.data['data']['旧剧']
    assert runtime.plugin.data['wait']['失败剧']['timestamp'] == '2026-09-29 12:00:00'
    assert runtime.plugin.data['wait']['失败剧']['attempts'] == 5


def test_imported_legacy_wait_retries_with_same_goal_and_counts_real_failure(runtime):
    source(runtime); run_import(runtime)
    runtime.plugin.init_plugin({**runtime.plugin.config, 'enable': True})
    runtime.helper.set_watching_status.return_value = False
    runtime.plugin._queue_retry('手动重试'); runtime.run_batch()
    pending = runtime.plugin.data['wait']['失败剧']
    assert pending['status'] == 'do' and pending['type'] == '电视剧' and pending['attempts'] == 1
    first = pending['timestamp']
    runtime.helper.set_watching_status.return_value = True
    runtime.plugin._queue_retry('手动重试'); runtime.run_batch()
    assert runtime.plugin.data['data']['失败剧']['timestamp'] == first
    assert runtime.plugin.data['wait'] == {}


def test_write_failure_rolls_back_without_touching_source(runtime, monkeypatch):
    old_data, old_config = source(runtime)
    save = runtime.plugin.save_data
    hit = []
    def fail_once(key, value):
        if key == 'wait' and not hit:
            hit.append(True)
            raise RuntimeError('private-cookie must not leak')
        save(key, value)
    monkeypatch.setattr(runtime.plugin, 'save_data', fail_once)
    run_import(runtime)
    assert 'data' not in runtime.plugin.data and 'wait' not in runtime.plugin.data
    assert runtime.plugin.data['timestamp_schema'] == 1
    assert 'legacy_import_journal' not in runtime.plugin.data
    assert runtime.plugin.data['legacy_import']['state'] == '失败'
    assert 'private-cookie' not in runtime.plugin.data['legacy_import']['message']
    assert runtime.plugin.config['user'] == 'new-user' and runtime.plugin.config['enable'] is False
    assert runtime.plugin.foreign_data == old_data and runtime.plugin.foreign_config == old_config
    run_import(runtime)
    assert runtime.plugin.data['legacy_import']['state'] == '完成'


def test_interrupted_import_recovers_on_next_init_without_automatically_reimporting(runtime, monkeypatch):
    source(runtime)
    save = runtime.plugin.save_data
    def interrupt(key, value):
        if key == 'wait': raise KeyboardInterrupt()
        save(key, value)
    monkeypatch.setattr(runtime.plugin, 'save_data', interrupt)
    with pytest.raises(KeyboardInterrupt): run_import(runtime)
    assert 'legacy_import_journal' in runtime.plugin.data
    monkeypatch.setattr(runtime.plugin, 'save_data', save)
    runtime.plugin.init_plugin(runtime.plugin.config)
    assert runtime.plugin.data['legacy_import']['state'] == '失败'
    assert 'legacy_import_journal' not in runtime.plugin.data
    assert 'data' not in runtime.plugin.data and 'wait' not in runtime.plugin.data
    assert not runtime.plugin.get_state()
    runtime.helper_factory.assert_not_called()


def test_interruption_after_commit_only_cleans_journal(runtime, monkeypatch):
    source(runtime)
    delete = runtime.plugin.del_data
    def interrupt(key):
        if key == 'legacy_import_journal': raise KeyboardInterrupt()
        delete(key)
    monkeypatch.setattr(runtime.plugin, 'del_data', interrupt)
    with pytest.raises(KeyboardInterrupt): run_import(runtime)
    before = deepcopy(runtime.plugin.data['data'])
    monkeypatch.setattr(runtime.plugin, 'del_data', delete)
    runtime.plugin.init_plugin(runtime.plugin.config)
    assert runtime.plugin.data['data'] == before
    assert runtime.plugin.data['legacy_import']['state'] == '完成'
    assert 'legacy_import_journal' not in runtime.plugin.data


def test_unrecoverable_storage_error_stays_disabled_and_retains_recovery_journal(runtime, monkeypatch):
    source(runtime)
    save = runtime.plugin.save_data
    def broken(key, value):
        if key == 'wait': raise RuntimeError('storage offline')
        save(key, value)
    # 恢复目标存在空 wait 键时也写失败，确保保留恢复入口。
    runtime.plugin.data['wait'] = {}
    monkeypatch.setattr(runtime.plugin, 'save_data', broken)
    run_import(runtime)
    assert 'legacy_import_journal' in runtime.plugin.data
    assert not runtime.plugin.get_state() and runtime.plugin._stop_event.is_set()
    monkeypatch.setattr(runtime.plugin, 'save_data', save)
    runtime.plugin.init_plugin(runtime.plugin.config)
    assert 'legacy_import_journal' not in runtime.plugin.data
    assert runtime.plugin.data['wait'] == {} and 'data' not in runtime.plugin.data


def test_import_checkbox_only_acts_from_settings_tab(runtime):
    source(runtime)
    run_import(runtime, _ui_tab='archive')
    assert 'legacy_import' not in runtime.plugin.data
    assert runtime.plugin.config['import_legacy'] is False
    runtime.manager.get_plugin_state.assert_not_called()


@pytest.mark.parametrize('missing_key', ['legacy_import_journal', 'data', 'legacy_import'])
def test_silent_storage_failure_is_detected_and_never_claims_success(runtime, monkeypatch, missing_key):
    source(runtime)
    save = runtime.plugin.save_data
    failed = []
    def drop_once(key, value):
        if key == missing_key and not failed:
            failed.append(True)
            return
        save(key, value)
    monkeypatch.setattr(runtime.plugin, 'save_data', drop_once)
    run_import(runtime)
    assert runtime.plugin.data.get('legacy_import', {}).get('state') != '完成'
    assert 'data' not in runtime.plugin.data and 'wait' not in runtime.plugin.data
    runtime.helper_factory.assert_not_called()


def test_migration_control_is_native_and_disabled_when_target_is_occupied(runtime):
    for occupied in [False, True]:
        runtime.plugin.data['data'] = {'saved': {'subject_id': '1'}} if occupied else {}
        form, defaults = runtime.plugin.get_form()
        control = next(node for node in nodes(form) if node.get('props', {}).get('model') == 'import_legacy')
        assert control['component'] == 'VSwitch' and control['props']['disabled'] is occupied
        assert defaults['import_legacy'] is False


def test_date_migration_keeps_earliest_valid_time_and_is_idempotent(runtime):
    runtime.plugin.data = {
        "data": {
            "older": {"timestamp": "2025-01-01 12:00:00", "status_updated_at": "2026-09-30 12:00:00", "status": "collect"},
            "fallback": {"timestamp": "bad", "status_updated_at": "2026-08-01 12:00:00"},
            "earlier_update": {"timestamp": "2026-09-30 12:00:00", "status_updated_at": "2026-08-01 12:00:00"},
            "unknown": {"timestamp": "bad", "status_updated_at": None}},
        "wait": {"one": {"timestamp": "2025-01-01 12:00:00", "last_attempt": "2026-09-29 12:00:00", "status": "do", "attempts": 7},
                 "two": {"timestamp": "2025-01-01 12:00:00", "status": "collect"}},
        "archive_last_removed": {"records": {"deleted": {}}, "removed_at": "2026-09-30 12:00:00"}}
    runtime.plugin.init_plugin({"enable": True})
    data = runtime.plugin.data
    assert data["data"]["older"]["timestamp"] == "2025-01-01 12:00:00"
    assert data["data"]["fallback"]["timestamp"] == data["data"]["earlier_update"]["timestamp"] == "2026-08-01 12:00:00"
    assert data["data"]["unknown"] == {}
    assert all("status_updated_at" not in value for value in data["data"].values())
    assert data["wait"]["one"]["timestamp"] == "2026-09-29 12:00:00"
    assert data["wait"]["one"]["attempts"] == 7 and "timestamp" not in data["wait"]["two"]
    assert data["timestamp_schema"] == 1 and "archive_last_removed" not in data
    before = deepcopy(data)
    runtime.plugin.init_plugin({"enable": True})
    assert runtime.plugin.data == before
    runtime.helper_factory.assert_not_called()
