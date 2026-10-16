"""核对识别日志能解释回退及终止原因，并且不增加请求、不泄露登录态。"""
import logging
from copy import deepcopy
from unittest.mock import Mock

import pytest

from helpers import (douban_result as result, douban_web_search as web_search, douban_web_detail as web_detail,
                     emby_service, seed_manual_match as seed, preview_match_payload as preview_payload)


def messages(caplog, level=logging.INFO):
    return [record.getMessage() for record in caplog.records if record.levelno >= level]


def test_full_chain_rejected_library_id_then_mp_unique(runtime, emit, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    helper = runtime.helper_module.DoubanHelper('ck=private-cookie')
    monkeypatch.setattr(runtime.module, 'DoubanHelper', Mock(return_value=helper))
    write = Mock(return_value=True)
    monkeypatch.setattr(helper, 'set_watching_status', write)
    web = Mock(side_effect=AssertionError('唯一匹配不应搜索网页'))
    monkeypatch.setattr(helper, '_request', web)
    runtime.recognize.side_effect = [runtime.media, result(runtime, subject='777', year='2020')]
    runtime.search.return_value = [result(runtime, subject='999')]
    instance, _ = emby_service(runtime, [{'Type': 'Series', 'ProviderIds': {'Douban': '777'}}])
    emit(item_id='series-1')
    text = '\n'.join(messages(caplog))
    stages = ['[播放同步][开始]', '来源=Emby 整部剧（Series）', '[播放同步][判断]', '核验待选 ID=777', '排除候选：年份不符',
              '转入 MP 豆瓣搜索', '开始搜索：关键词=测试剧', '返回 1 条，合格候选 1 个',
              '采用 MP 豆瓣搜索唯一匹配', '无需网页搜索', '[写入]', '[播放同步][成功]']
    positions = [text.index(stage) for stage in stages]
    assert positions == sorted(positions)
    assert all(line.startswith('[播放同步][') for line in messages(caplog))
    assert '按指定身份识别：来源=TMDB，ID=123' in caplog.text
    assert '识别结果：' in caplog.text
    assert '按指定身份识别' not in text and '识别结果：' not in text
    assert '年份=2020' in text and '目标年份=2024' in text
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)
    assert text.count('[播放同步][成功]') == 1
    assert runtime.recognize.call_count == 2 and runtime.search.call_count == 1
    assert instance.get_data.call_count == 1
    web.assert_not_called()
    write.assert_called_once_with('999', 'do', True)
    assert 'private-cookie' not in caplog.text and 'api_key' not in caplog.text


@pytest.mark.parametrize('season,years,expected', [
    (1, {}, '第一季年份缺失，使用整剧年份'),
    (2, {}, '年份=未知（来源=缺少可用年份）'),
    (2, {2: '2025'}, '年份=2025（来源=第2季首播年份）'),
])
def test_title_fallback_and_year_source(runtime, emit, caplog, season, years, expected):
    caplog.set_level(logging.DEBUG)
    runtime.media.season_years = years
    runtime.recognize.side_effect = [None, runtime.media]
    emit(season_id=str(season))
    assert '清除身份参数后按标题识别' in caplog.text
    assert expected in caplog.text
    assert runtime.recognize.call_count == 2
    call = runtime.recognize.call_args.kwargs
    assert call['media_source'] is None and call['media_id'] is None
    assert call['meta'].media_source is None and call['meta'].media_id is None


def test_preferred_id_verified_without_search(runtime, caplog):
    caplog.set_level(logging.INFO)
    helper = runtime.helper_module.DoubanHelper('ck=private-cookie')
    runtime.recognize.return_value = result(runtime)
    assert helper.get_subject_id('测试剧', '2024', runtime.MediaType.TV, '999') == ('测试剧', '999')
    assert '采用已核验 ID' in caplog.text and '无需 MP 或网页搜索' in caplog.text
    runtime.search.assert_not_called()


@pytest.mark.parametrize('candidates,explanation', [
    ([], '未配置豆瓣 Cookie'),
    (['998', '999'], 'MP 已有 2 个合格候选'),
])
def test_web_not_executed_reason(runtime, caplog, candidates, explanation):
    caplog.set_level(logging.INFO)
    helper = runtime.helper_module.DoubanHelper('ck=private-cookie')
    if not candidates:
        helper.cookies = {}
    runtime.search.return_value = [result(runtime, subject=sid) for sid in candidates]
    assert helper.get_subject_id('测试剧', '2024', runtime.MediaType.TV) == (None, None)
    assert explanation in caplog.text
    assert '[媒体匹配][匹配]' in caplog.text and '豆瓣网页搜索：' in caplog.text and '未执行：' in caplog.text
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


def test_rejections_one_example_per_reason_at_info_full_details_debug(runtime, caplog):
    caplog.set_level(logging.DEBUG)
    helper = runtime.helper_module.DoubanHelper('ck=private-cookie')
    helper.cookies = {}
    runtime.search.return_value = [result(runtime, subject='101', year='2001'),
                                   result(runtime, subject='102', year='2002'),
                                   result(runtime, subject='103', title='另一部剧')]
    assert helper.get_subject_id('测试剧', '2024', runtime.MediaType.TV) == (None, None)
    text = '\n'.join(messages(caplog))
    assert text.count('排除候选：年份不符') == 1
    assert text.count('排除候选：名称不符') == 1
    assert '年份不符 2 条' in text and '名称不符 1 条' in text
    assert '身份=豆瓣/101' in text and '身份=豆瓣/102' not in text
    assert '身份=豆瓣/102' in caplog.text
    assert len(helper.last_candidates) == 3


def test_web_fallback_logs_prefilter_and_actual_detail(runtime, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    helper = runtime.helper_module.DoubanHelper('ck=private-cookie; dbcl2=private-account')
    runtime.search.return_value = []
    request = Mock(side_effect=[web_search(('7916280', '嗜血法医 第七季'), ('999', '嗜血法医 第一季')),
                               web_detail()])
    monkeypatch.setattr(helper, '_request', request)
    assert helper.get_subject_id('嗜血法医 第7季', '2012', runtime.MediaType.TV, season=7) == ('嗜血法医 第七季', '7916280')
    for expected in ('MP 豆瓣搜索未返回候选条目；转入豆瓣网页搜索', '关键词=嗜血法医',
                     '名称初筛排除：季数不符', '搜索页提取 2 个条目，名称初筛通过 1 个',
                     '返回 1 条，合格候选 1 个', '登录态网页匹配成功', '豆瓣 ID=7916280'):
        assert expected in caplog.text
    assert request.call_count == 2
    assert 'private-cookie' not in caplog.text and 'private-account' not in caplog.text


@pytest.mark.parametrize('pages,reason', [
    ([None], '未收到响应'),
    ([web_search(*[(str(i), '嗜血法医 第七季') for i in range(1, 7)])], '名称候选过多'),
    ([web_search(('7916280', '嗜血法医 第七季')), None], '无法确认网页候选是否唯一'),
    ([web_search(('7916280', '嗜血法医 第七季')), web_detail(year='2013')], '年份不符'),
])
def test_web_stop_reason_visible_at_info(runtime, monkeypatch, caplog, pages, reason):
    caplog.set_level(logging.INFO)
    helper = runtime.helper_module.DoubanHelper('ck=private-cookie')
    request = Mock(side_effect=pages)
    monkeypatch.setattr(helper, '_request', request)
    assert helper.get_subject_id('嗜血法医 第7季', '2012', runtime.MediaType.TV, season=7) == (None, None)
    assert reason in caplog.text
    assert request.call_count == len(pages)
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


def test_search_exception_does_not_log_response_body(runtime, caplog):
    caplog.set_level(logging.DEBUG)
    helper = runtime.helper_module.DoubanHelper('ck=private-cookie')
    helper.cookies = {}
    runtime.recognize.side_effect = RuntimeError('Cookie=private-cookie; token=hidden-token')
    runtime.search.side_effect = ValueError('secret-response-body')
    assert helper.get_subject_id('测试剧', '2024', runtime.MediaType.TV, '999') == (None, None)
    assert 'RuntimeError' in caplog.text and 'ValueError' in caplog.text
    assert all(secret not in caplog.text for secret in ('private-cookie', 'hidden-token', 'secret-response-body'))


def test_manual_id_priority_visible_and_not_rematched(runtime, caplog):
    item, candidate = seed(runtime)
    assert runtime.plugin.api_match_confirm(preview_payload(runtime)).success
    runtime.plugin.data['data'] = {}
    runtime.helper.set_watching_status.reset_mock()
    caplog.clear()
    caplog.set_level(logging.INFO)
    assert runtime.plugin._sync_item('movie', {**item, 'subject_id': '998'}, {}, {}) == 'success'
    assert '人工确认（最高优先级）' in caplog.text
    assert '豆瓣 ID=27114240；直接复用，不重新搜索匹配' in caplog.text
    assert '豆瓣 ID=998' not in caplog.text
    runtime.helper.get_subject_id.assert_not_called()
    runtime.helper.set_watching_status.assert_called_once_with(candidate['subject_id'], 'collect', True)


@pytest.mark.parametrize('source', ['档案', '失败记录'])
def test_bound_id_origin_and_skip_or_write(runtime, emit, caplog, source):
    caplog.set_level(logging.DEBUG)
    item = {'title': '测试剧 第1季', 'subject_name': '测试剧', 'subject_id': '999', 'status': 'do',
            'type': '电视剧', 'timestamp': '2026-10-01 00:00:00'}
    key = 'themoviedb:123|TV|S1'
    runtime.plugin.data['data' if source == '档案' else 'wait'] = {key: deepcopy(item)}
    emit()
    text = '\n'.join(messages(caplog))
    assert f'{"本插件档案" if source == "档案" else source}已绑定条目' in caplog.text
    runtime.helper.get_subject_id.assert_not_called()
    if source == '档案':
        assert len(messages(caplog)) == 3
        assert '[播放同步][开始] 测试剧 第1季｜停止播放，第2集；Emby 单集状态：可续播' in text
        assert '[播放同步][判断] 测试剧 第1季｜当前第2集，季末第3集；目标：在看，本地：在看' in text
        assert '[播放同步][跳过] 测试剧 第1季｜本地已记录为在看，无需变更状态' in text
        assert '豆瓣 ID' not in text and '豆瓣 ID 来源：' in caplog.text
        assert '[写入]' not in text
        runtime.helper.set_watching_status.assert_not_called()
    else:
        assert '[写入]' in caplog.text and '[播放同步][成功]' in caplog.text
        runtime.helper.set_watching_status.assert_called_once()


def test_unknown_episode_count_is_not_logged_as_confirmed_season_finale(runtime, emit, caplog):
    caplog.set_level(logging.DEBUG)
    runtime.media.seasons = {}
    runtime.state_result.played = True
    emit(event='playback.stop', episode_id='1')
    assert '当前第1集，季末集未知（按单集处理）；目标：看过' in caplog.text
    assert '季末第1集' not in caplog.text
    runtime.helper.set_watching_status.assert_called_once_with('999', 'collect', True)


def test_operation_context_restores_after_exception_and_keeps_summary_separate(runtime, caplog):
    caplog.set_level(logging.INFO)
    with runtime.module.operation('失败重试', '原条目', 2, 3):
        with pytest.raises(RuntimeError):
            with runtime.module.operation('手动匹配', '另一条目'):
                runtime.module.log_recognition('豆瓣匹配', '候选名称', '人工候选')
                raise RuntimeError('test interruption')
        runtime.module.log_recognition('豆瓣匹配', '豆瓣不同标题', '继续重试')
        summary = runtime.plugin._batch_message('失败重试', 3, success=1, pending=2)
    runtime.module.log_recognition('豆瓣匹配', '独立条目', '新的查询')
    assert messages(caplog) == [
        '[手动匹配][匹配] 另一条目｜人工候选',
        '[失败重试][匹配] 原条目（2/3）｜继续重试',
        '[媒体匹配][匹配] 独立条目｜新的查询',
    ]
    assert summary.startswith('[失败重试][进度] 批量任务｜')
    assert '原条目' not in summary and '（2/3）' not in summary
