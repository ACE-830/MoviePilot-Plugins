"""人工确认隔离搜索与写入，以人工映射为最终匹配并复用统一同步流程。"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from helpers import run_archive_job, run_form_script
from helpers import seed_manual_match as seed, preview_match_payload as preview_payload


def test_rejected_candidates_are_saved_without_relaxing_year_check(runtime):
    helper = runtime.helper_module.DoubanHelper('ck=offline;')
    runtime.recognize.return_value = SimpleNamespace(media_source=runtime.MediaSource.Douban,
        media_id='27114240', title='冒牌上尉', year='2017', type=runtime.MediaType.MOVIE)
    runtime.search.return_value = [runtime.recognize.return_value]
    helper.cookies = {}  # 禁止网页回退；仅验证真实匹配与快照逻辑。
    assert helper.get_subject_id('冒牌上尉', '2018', runtime.MediaType.MOVIE, '27114240') == (None, None)
    assert len(helper.last_candidates) == 1
    assert helper.last_candidates[0]['reason'] == '年份不符'
    item, _ = seed(runtime)
    runtime.helper.get_subject_id.return_value = (None, None)
    runtime.helper.last_candidates = helper.last_candidates
    runtime.helper.last_error = helper.last_error
    runtime.plugin._sync_item('movie', item, {}, runtime.plugin._load_dict('wait'))
    assert runtime.plugin.data['wait']['movie']['match_candidates'] == helper.last_candidates
    runtime.helper.set_watching_status.assert_not_called()


def test_preview_saved_candidate_is_local_and_old_queue_can_load_default(runtime):
    item, candidate = seed(runtime)
    item['match_candidates'] = [candidate]
    before = deepcopy(runtime.plugin.data)
    result = runtime.plugin.api_match_preview({'key': 'movie'})
    assert result.success and result.data['candidates'] == [candidate]
    runtime.helper_factory.assert_not_called()
    assert runtime.plugin.data == before
    del item['match_candidates']
    before = deepcopy(runtime.plugin.data)
    assert runtime.plugin.api_match_preview({'key': 'movie'}).data['candidates'][0]['subject_id'] == '27114240'
    assert runtime.plugin.data == before
    runtime.helper.describe_subject.assert_called_once_with('27114240')
    runtime.helper.set_watching_status.assert_not_called()
    runtime.schedule.assert_not_called()


@pytest.mark.parametrize('poster', ['https://image.tmdb.org/t/p/original/movie.jpg', ''])
def test_confirm_syncs_immediately_and_preserves_first_attempt(runtime, poster):
    item, _ = seed(runtime)
    item['poster_path'] = poster
    original_time = item['timestamp']
    other = deepcopy(runtime.plugin.data['wait']['other'])
    payload = preview_payload(runtime)
    runtime.helper.describe_subject.reset_mock()
    result = runtime.plugin.api_match_confirm(payload)
    assert result.success and result.data['outcome'] == 'success'
    runtime.schedule.assert_not_called()
    runtime.helper.describe_subject.assert_not_called()
    runtime.helper.get_subject_id.assert_not_called()
    runtime.helper.set_watching_status.assert_called_once_with('27114240', 'collect', True)
    assert runtime.plugin.data['wait'] == {'other': other}
    record = runtime.plugin.data['data']['movie']
    assert record['timestamp'] == original_time and record['subject_id'] == '27114240'
    assert record['poster_path'] == poster
    assert record['matched_by'] == 'manual'
    assert 'match_candidates' not in record and 'attempts' not in record
    assert runtime.plugin.data['manual_matches']['movie']['subject_id'] == '27114240'
    assert runtime.plugin.data['manual_matches']['movie']['poster_path'] == payload['candidate']['poster_path']
    runtime.recognize.assert_not_called()


def test_failed_write_keeps_confirmed_mapping_and_counts_only_attempt(runtime):
    item, _ = seed(runtime)
    item['poster_path'] = 'https://image.tmdb.org/t/p/original/movie.jpg'
    runtime.helper.set_watching_status.return_value = False
    runtime.helper.last_error = '豆瓣写入失败'
    result = runtime.plugin.api_match_confirm(preview_payload(runtime))
    assert result.success and result.data['outcome'] == 'failed'
    pending = runtime.plugin.data['wait']['movie']
    assert pending['attempts'] == 4 and pending['timestamp'] == item['timestamp']
    assert pending['subject_id'] == '27114240' and pending['matched_by'] == 'manual'
    assert pending['poster_path'] == item['poster_path']
    runtime.helper.get_subject_id.assert_not_called()
    assert not runtime.plugin.data.get('data')
    assert runtime.plugin.data['manual_matches']['movie']['subject_id'] == '27114240'
    runtime.helper.set_watching_status.return_value = True
    runtime.plugin._sync_item('movie', pending, {}, runtime.plugin._load_dict('wait'))
    assert 'movie' not in runtime.plugin.data['wait']
    runtime.helper.get_subject_id.assert_not_called()
    assert runtime.plugin.data['data']['movie']['poster_path'] == item['poster_path']


@pytest.mark.parametrize('change', ['removed', 'updated', 'busy', 'disabled', 'invalid'])
def test_stale_or_invalid_confirmation_does_not_write(runtime, change):
    seed(runtime)
    payload = preview_payload(runtime)
    if change == 'removed': runtime.plugin.data['wait'].pop('movie')
    elif change == 'updated': runtime.plugin.data['wait']['movie']['status'] = 'do'
    elif change == 'busy': runtime.plugin._active_batch = {'id': 'busy'}
    elif change == 'disabled': runtime.plugin._enable = False
    elif change == 'invalid': payload['candidate']['subject_id'] = 'https://invalid/'
    before = deepcopy(runtime.plugin.data)
    response = runtime.plugin.api_match_confirm(payload)
    assert not response.success
    assert {k:v for k,v in runtime.plugin.data.items() if k!='manual_result'} == before
    if change != 'invalid':
        assert response.message in runtime.plugin._manual_result()['message']
    runtime.schedule.assert_not_called()
    runtime.helper.set_watching_status.assert_not_called()


def test_confirmation_accepts_user_identity_without_any_new_lookup(runtime):
    seed(runtime)
    payload = preview_payload(runtime)
    payload['candidate'].update(subject_id='123', title='用户确认的名称', year='2001', type='电视剧')
    runtime.helper.describe_subject.side_effect = AssertionError('不得再次核验')
    runtime.helper.get_subject_id.side_effect = AssertionError('不得再次匹配')
    runtime.schedule.side_effect = AssertionError('不得排队重试')
    result = runtime.plugin.api_match_confirm(payload)
    assert result.success and result.data['outcome'] == 'success'
    runtime.helper.set_watching_status.assert_called_once_with('123', 'collect', True)


def test_manual_mapping_wins_over_later_events_and_survives_archive_removal(runtime):
    item, candidate = seed(runtime)
    assert runtime.plugin.api_match_confirm(preview_payload(runtime)).success
    runtime.helper.set_watching_status.reset_mock()
    # 移除本地档案也不丢失人工选择；新的播放事件带有其他豆瓣 ID。
    runtime.plugin.data['data'] = {}
    incoming = {**item, 'subject_id': '999', 'preferred_subject_id': '998', 'matched_by': 'auto',
                'poster_path': 'https://image.tmdb.org/t/p/original/new-poster.jpg'}
    assert runtime.plugin._sync_item('movie', incoming, {}, runtime.plugin._load_dict('wait')) == 'success'
    runtime.helper.set_watching_status.assert_called_once_with(candidate['subject_id'], 'collect', True)
    runtime.helper.get_subject_id.assert_not_called()
    assert runtime.plugin.data['data']['movie']['poster_path'] == incoming['poster_path']


def test_confirmed_correction_is_not_skipped_by_old_wrong_subject(runtime):
    item, _ = seed(runtime)
    runtime.plugin.data['data'] = {'movie': {**item, 'subject_id': '999', 'status': 'collect'}}
    assert runtime.plugin.api_match_confirm(preview_payload(runtime)).data['outcome'] == 'success'
    runtime.helper.set_watching_status.assert_called_once_with('27114240', 'collect', True)
    assert runtime.plugin.data['data']['movie']['subject_id'] == '27114240'


def test_correct_same_status_is_skipped_without_changing_archive_date(runtime):
    item, _ = seed(runtime)
    runtime.plugin.data['data'] = {'movie': {**item, 'subject_id': '27114240', 'timestamp': '2020-01-01 00:00:00'}}
    result = runtime.plugin.api_match_confirm(preview_payload(runtime))
    assert result.success and result.data['outcome'] == 'skipped'
    assert runtime.plugin.data['data']['movie']['timestamp'] == '2020-01-01 00:00:00'
    assert 'movie' not in runtime.plugin.data['wait']
    assert runtime.plugin.data['manual_matches']['movie']['subject_id'] == '27114240'
    runtime.helper.set_watching_status.assert_not_called()


@pytest.mark.parametrize('action', ['do', 'collect', 'remove'])
@pytest.mark.parametrize('retry', [False, True], ids=['immediate', 'after-failure'])
def test_archive_edit_keeps_selected_subject_after_failed_manual_correction(runtime, action, retry):
    item, candidate = seed(runtime)
    item['type'] = candidate['type'] = '电视剧'
    runtime.helper.describe_subject.return_value = deepcopy(candidate)
    runtime.plugin.data['wait'].pop('other')
    old = runtime.plugin._archive_record({**item, 'subject_id': '999', 'subject_name': '原档案',
        'status': 'do' if action == 'collect' else 'collect'})
    runtime.plugin.data['data'] = {'movie': old, 'old-alias': deepcopy(old)}
    runtime.helper.set_watching_status.return_value = False
    runtime.helper.last_error = '模拟写入失败'
    assert runtime.plugin.api_match_confirm(preview_payload(runtime)).data['outcome'] == 'failed'
    # 人工匹配已指向另一个条目，但失败的写入不能更改当前展示的档案。
    assert runtime.plugin.data['data']['movie'] == old
    runtime.plugin.data['data']['other-subject'] = runtime.plugin._archive_record(
        {**item, 'subject_id': candidate['subject_id'], 'subject_name': candidate['title']})
    before = deepcopy(runtime.plugin.data['data'])
    runtime.helper.set_watching_status.reset_mock()
    writer = (runtime.helper.clear_watching_status if action == 'remove'
              else runtime.helper.set_watching_status)
    writer.return_value = not retry
    assert runtime.plugin.api_archive_edit({'selection': ['douban:999'], 'action': action}).success
    run_archive_job(runtime, 'archive_edit')
    if retry:
        assert runtime.plugin.data['data'] == before
        pending = next(iter(runtime.plugin.data['wait'].values()))
        assert pending['manual_status'] and pending['subject_id'] == '999'
        writer.return_value = True
        assert runtime.plugin.api_archive_retry().success
        run_archive_job(runtime, 'retry_waiting')
    expected = ('999',) if action == 'remove' else ('999', action, True)
    assert [call.args for call in writer.call_args_list] == [expected] * (2 if retry else 1)
    records = runtime.plugin.data['data']
    assert records['other-subject'] == before['other-subject']
    if action == 'remove':
        assert set(records) == {'other-subject'}
        runtime.helper.set_watching_status.assert_not_called()
    else:
        for key in ('movie', 'old-alias'):
            assert records[key]['subject_id'] == '999' and records[key]['status'] == action
        runtime.helper.clear_watching_status.assert_not_called()
    assert runtime.plugin.data['wait'] == {}
    assert runtime.plugin.data['manual_matches']['movie']['subject_id'] == candidate['subject_id']
    runtime.helper.get_subject_id.assert_not_called()


@pytest.mark.parametrize('with_media_id', [True, False], ids=['media-identity', 'title-only'])
@pytest.mark.parametrize(('target', 'saved'), [('do', 'do'), ('collect', 'collect'), ('do', 'collect')])
def test_satisfied_manual_correction_updates_binding_without_changing_target_state(runtime, target, saved, with_media_id):
    item, candidate = seed(runtime)
    item.update(type='电视剧', status=target, poster_path='https://image.tmdb.org/t/p/original/show.jpg')
    candidate['type'] = '电视剧'
    runtime.helper.describe_subject.return_value = deepcopy(candidate)
    if not with_media_id:
        item.pop('media_id')
        item.pop('media_source')
    existing = runtime.plugin._archive_record({**item, 'subject_id': candidate['subject_id'],
        'subject_name': candidate['title'], 'status': saved, 'timestamp': '2020-01-01 00:00:00'})
    runtime.plugin.data['data'] = {
        'movie': {**existing, 'subject_id': '999', 'subject_name': '原错误条目', 'timestamp': '2026-01-01 00:00:00'},
        'alias': deepcopy(existing),
    }
    other = deepcopy(runtime.plugin.data['wait']['other'])
    response = runtime.plugin.api_match_confirm(preview_payload(runtime))
    assert response.success and response.data['outcome'] == 'skipped'
    corrected = runtime.plugin.data['data']['movie']
    assert corrected['subject_id'] == candidate['subject_id']
    assert corrected['subject_name'] == candidate['title'] and corrected['matched_by'] == 'manual'
    assert corrected['status'] == saved and corrected['timestamp'] == existing['timestamp']
    assert corrected['poster_path'] == item['poster_path']
    assert not {'attempts', 'last_error', 'last_attempt', 'match_candidates'} & corrected.keys()
    assert runtime.plugin.data['data']['alias'] == existing
    assert runtime.plugin.data['wait'] == {'other': other}
    assert set(runtime.plugin._archive_groups()) == {'douban:' + candidate['subject_id']}
    runtime.helper.set_watching_status.assert_not_called()
    runtime.helper.clear_watching_status.assert_not_called()
    runtime.helper.get_subject_id.assert_not_called()
    runtime.schedule.assert_not_called()


def test_movie_cannot_be_marked_do_even_when_manual(runtime):
    item, _ = seed(runtime)
    item.update(status='do', type='电视剧')
    before = deepcopy(runtime.plugin.data)
    result = runtime.plugin.api_match_confirm(preview_payload(runtime))
    assert not result.success and '电影不支持' in result.message
    assert {k:v for k,v in runtime.plugin.data.items() if k!='manual_result'} == before
    assert '电影不支持' in runtime.plugin._manual_result()['message']
    runtime.helper.set_watching_status.assert_not_called()


def test_manual_selection_only_retains_safe_candidate_fields(runtime):
    _, candidate = seed(runtime)
    candidate.update(poster_path='javascript:alert(1)', extra='discard', subject_id='0')
    assert runtime.module.matching.selected_candidate(candidate) is None
    candidate['subject_id'] = '123'
    value = runtime.module.matching.selected_candidate(candidate)
    assert value['poster_path'] == '' and value['reason'] == '' and 'extra' not in value


def test_bound_archive_operations_cannot_be_redirected(runtime):
    item, _ = seed(runtime)
    item.update(manual_status=True, status='remove', subject_id='27114240')
    before = deepcopy(runtime.plugin.data)
    assert not runtime.plugin.api_match_preview({'key': 'movie'}).success
    assert runtime.plugin.data == before
    runtime.helper_factory.assert_not_called()


def test_detail_rejects_wrong_source_id_and_missing_title(runtime):
    helper = runtime.helper_module.DoubanHelper('ck=offline;')
    for source, sid, title in [(runtime.MediaSource.TMDB, '27114240', '片名'),
                               (runtime.MediaSource.Douban, '999', '片名'),
                               (runtime.MediaSource.Douban, '27114240', '')]:
        runtime.recognize.return_value = SimpleNamespace(media_source=source, media_id=sid, title=title,
                                                         year='2017', type=runtime.MediaType.MOVIE)
        assert helper.describe_subject('27114240') is None


def test_dialog_state_is_not_persisted_or_executed_on_save(runtime):
    seed(runtime)
    form, defaults = runtime.plugin.get_form()
    assert form[-1]['component'] == 'VDialog'
    config = {**defaults, 'enable': True, '_match': {'subject_id': '27114240', 'confirming': True}}
    runtime.plugin.init_plugin(config)
    assert '_match' not in runtime.plugin.config
    assert not runtime.plugin.data['wait']['movie'].get('subject_id')
    runtime.helper_factory.assert_not_called()
    runtime.schedule.assert_not_called()


def test_native_handlers_use_host_search_and_require_explicit_confirmation(runtime):
    m = runtime.module.matching
    script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ui = JSON.parse(fs.readFileSync(0, 'utf8'));
const model = {_match: ui.state};
const click = code => new Function('model', 'with(model) { return (' + code + ')(); }')(model);
const calls = [];
let closed=0;
global.document={baseURI:'https://mp.invalid/',getElementById:id=>{assert.equal(id,'doubanwatchingplus-config');return {closest:selector=>{
 assert.equal(selector,'.v-card');return {querySelector:selector=>{assert.equal(selector,':scope > button[aria-label]');return {click:()=>closed++};}};}};}};
let manifests=0;
global.fetch=async url=>{
 assert.equal(url,'https://mp.invalid/service-worker.js'); manifests++;
 return {ok:true,text:async()=>'{"url":"assets/no-image-current.jpeg"}'};
};
let lookup;
const candidate = {subject_id:'27114240', title:'冒牌上尉', year:'2017', type:'电影', label:'冒牌上尉（2017）', reason:'年份不符', poster_path:'https://old.invalid/poster.webp'};
global.window = {MoviePilotAPI: {
  post: async (url, body) => {calls.push([url, body]);
    if (url.endsWith('/preview')) return {success:true, data:{original:{title:'冒牌上尉',year:'2018'}, keyword:'冒牌上尉', candidates:[{...candidate}], revision:'revision'}};
    return {success:true, message:'已同步', data:{outcome:'success'}};
  },
  get: async (url, options) => {calls.push([url, options]); return await lookup();}
}};
const fresh = () => Promise.resolve({success:true,data:[
 {media_source:'douban',media_id:'999',title:'同名其他作品',poster_path:'https://wrong.invalid/poster.jpg'},
 {media_source:'douban',media_id:'27114240',title:'搜索接口名称',year:'2017',type:'电影',poster_path:'https://signed.invalid/poster.webp?signature=current'}]});
const confirmations = () => calls.filter(([url])=>url.endsWith('/confirm'));
(async () => {
 lookup = fresh;
 await click(ui.open);
 assert.equal(model._match.selected.subject_id, '27114240');
 assert.equal(model._match.selected.poster_path, 'https://signed.invalid/poster.webp?signature=current');
 assert.equal(model._match.selected.title, candidate.title);
 assert.equal(model._match.selected.reason, '年份不符');
 assert.equal(confirmations().length, 0); // 刷新海报不确认、不更换候选。
 assert.equal(calls[1][0], 'media/search');
 assert.deepEqual(calls[1][1].params, {title:'冒牌上尉',type:'media',page:1,count:20,media_source:'douban'});
 lookup = async () => ({success:true,data:[{media_source:'douban',media_id:'123',title:'正确作品',year:'2015',type:'电影'},
     {media_source:'themoviedb',media_id:'456',title:'其他来源'}]});
 await click(ui.search);
 assert.equal(calls[1][0], 'media/search');
 assert.deepEqual(calls[1][1].params, {title:'冒牌上尉',type:'media',page:1,count:20,media_source:'douban'});
 assert.equal(model._match.candidates.length, 1);
 assert.equal(model._match.selected, null); // 搜索不会自动选择或提交
 const row = ui.list.content[0];
 const ev = expr => new Function('model', 'with(model){return ' + expr.slice(2,-2) + '}')(model);
 const anchor = row.slots.prepend[0];
 assert.equal(ev(anchor.props.href), 'https://movie.douban.com/subject/123/');
 const src = anchor.content[0].props.src;
 model._match.candidates[0].poster_path = 'https://example.invalid/poster.jpg';
 assert.equal(ev(src), 'https://example.invalid/poster.jpg');
 model._match.candidates[0].poster_path = 'javascript:alert(1)';
 const fallback = 'https://mp.invalid/assets/no-image-current.jpeg';
 assert.equal(ev(src), fallback);
 for (const value of ['', 'https://img3.doubanio.com/f/frodo/hash/pics/subject/tv_normal.png',
                      'https://img1.doubanio.com/f/frodo/hash/pics/subject/movie_normal.png?size=small']) {
  model._match.candidates[0].poster_path = value;
  assert.equal(ev(src), fallback);
  assert.equal(ev(anchor.props.href), 'https://movie.douban.com/subject/123/');
 }
 model._match.candidates[0].poster_path = 'https://example.invalid/movie_normal.png';
 assert.equal(ev(src), 'https://example.invalid/movie_normal.png'); // 只识别豆瓣自己的默认图。
 const errorImage = anchor.content[0].slots.error[0];
 assert.equal(errorImage.component, 'VImg'); // 原生失败插槽，不手写重试/错误状态。
 assert.equal(ev(errorImage.props.src), fallback);
 assert.equal(errorImage.props.width, 60);
 assert.equal(errorImage.props.height, 90);
 let stopped = false;
 new Function('model', 'event', 'with(model){(' + anchor.props.onClick + ')(event)}')(model, {stopPropagation:()=>stopped=true});
 assert.equal(stopped,true);
 assert.equal(model._match.selected,null);
 await click(row.props.onClick);
 await click(ui.confirm);
 assert.equal(confirmations()[0][1].candidate.subject_id, '123');
 assert.equal(model._match.done, true);
 assert.equal(model._match.open, false);
 assert.equal(closed,1);
 await click(ui.confirm);
 assert.equal(confirmations().length, 1); // 防止重复提交
 // 同名但不同 ID 不能替换默认海报；网络失败也不能阻止核对原条目。
 await click(ui.open);
 assert.equal(model._match.selected.poster_path, candidate.poster_path);
 lookup = async () => { throw new Error('offline'); };
 await click(ui.open);
 assert.equal(model._match.selected.subject_id, candidate.subject_id);
 assert.equal(model._match.error, false);
 assert.equal(model._match.busy, false);
 lookup = fresh;
 await click(ui.open);
 const old = model._match;
 let resolve;
 lookup = () => new Promise(r => resolve = r);
 const pending = click(ui.search);
 lookup = fresh;
 await click(ui.open); // 切换记录后的旧结果不能覆盖新对话框
 resolve({success:true,data:[]}); await pending;
 assert.notEqual(model._match, old);
 assert.equal(model._match.candidates.length, 1);
 assert.equal(manifests,1); // 重新打开同一表单复用已解析的原生资源地址。
})().catch(e => {console.error(e);process.exitCode=1;});
'''
    data = {'state': m.initial_state(), 'open': m.open_handler('movie', 'DoubanWatchingPlus'),
            'list': m.candidate_list(), 'search': m.SEARCH, 'confirm': m.confirm_handler('DoubanWatchingPlus')}
    run_form_script(script, data)


def test_manual_search_preserves_douban_seasons_and_refreshes_alias_posters(runtime):
    """真实回归：TMDB 叫洛佩兹一家，豆瓣叫乔治洛佩兹秀，不能按前者刷新候选海报。"""
    m = runtime.module.matching
    script = r'''
const assert = require('node:assert/strict');
const ui = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const model = {_match: ui.state};
const click = code => new Function('model', 'with(model) { return (' + code + ')(); }')(model);
const candidates = [
 {subject_id:'2079211', title:'乔治洛佩兹秀 第一季', type:'电视剧', year:'2002', reason:'名称不符', poster_path:'https://old.invalid/first.webp'},
 {subject_id:'38563292', title:'乔治洛佩兹秀 第二季', type:'电视剧', year:'2002', reason:'年份不符', poster_path:''}];
const preview = {original:{title:'洛佩兹一家 第5季'}, keyword:'洛佩兹一家 第5季', revision:'unchanged', candidates};
const original = JSON.stringify(preview);
const requests = [];
let writes = 0;
global.window = {MoviePilotAPI:{
 post:async url => {
  if (!url.endsWith('/preview')) { writes++; throw new Error('Preview must not write'); }
  return {success:true, data:JSON.parse(original)};
 },
 get:async (url, options) => {
  assert.equal(url, 'media/search');
  assert.equal(options.params.media_source, 'douban');
  const keyword = options.params.title;
  requests.push(keyword);
  // 模拟宿主把查询季数追加到所有候选标题的实际行为。
  const suffix = /第.*季|\bSeason\s*\d+|\bS\d+/i.test(keyword) ? ' 第五季' : '';
  return {success:true, data:candidates.map(c => ({media_source:'douban', media_id:c.subject_id,
   title:c.title + suffix, type:c.type, year:c.year, poster_path:'https://fresh.invalid/' + c.subject_id + '.webp'}))};
 }
}};
(async () => {
 await click(ui.open);
 assert.deepEqual(requests, ['乔治洛佩兹秀']); // 两季共用一次搜索；不使用洛佩兹一家。
 assert.equal(model._match.keyword, '洛佩兹一家 第5季');
 assert.equal(model._match.revision, 'unchanged');
 assert.equal(model._match.selected.subject_id, '2079211');
 assert.equal(model._match.selected.title, '乔治洛佩兹秀 第一季');
 assert.equal(model._match.selected.reason, '名称不符');
 assert.equal(model._match.selected.poster_path, 'https://fresh.invalid/2079211.webp');
 assert.equal(model._match.candidates[1].poster_path, 'https://fresh.invalid/38563292.webp');
 assert.equal(JSON.stringify(preview), original);
 for (const keyword of ['乔治洛佩兹秀 第5季', '乔治洛佩兹秀 第五季', '乔治洛佩兹秀 第 5 季',
                       '乔治洛佩兹秀 S05', '乔治洛佩兹秀 Season 5']) {
  model._match.keyword = keyword;
  await click(ui.search);
  assert.equal(requests.at(-1), '乔治洛佩兹秀');
  assert.equal(model._match.keyword, keyword); // 不改写用户输入。
  assert.equal(model._match.selected, null);
  assert.deepEqual(model._match.candidates.map(c => c.title), candidates.map(c => c.title));
  assert.equal(model._match.candidates[1].label, '乔治洛佩兹秀 第二季（2002） · 电视剧 · 38563292');
 }
 for (const keyword of ['1899', '1917', '第五季']) {
  model._match.keyword = keyword;
  await click(ui.search);
  assert.equal(requests.at(-1), keyword); // 保留片名数字，不发送空关键词。
 }
 assert.equal(writes, 0);
})().catch(e => {console.error(e);process.exitCode=1;});
'''
    data = {'state': m.initial_state(), 'open': m.open_handler('tmdb:3308|TV|S5', 'DoubanWatchingPlus'),
            'search': m.SEARCH}
    run_form_script(script, data)
