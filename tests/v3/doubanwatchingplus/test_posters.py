"""海报缓存、失效刷新与原生占位图；不改变匹配绑定和观看状态。"""

from copy import deepcopy

import pytest

from helpers import run_form_script, seed_manual_match as seed, preview_match_payload as preview_payload


def test_poster_cache_is_separate_from_matching_and_archive_state(runtime):
    item, candidate = seed(runtime)
    item['match_candidates'] = [candidate]
    before = deepcopy(runtime.plugin.data)
    revision = preview_payload(runtime)['revision']
    url = 'https://qnmob3-sign.doubanio.com/view/photo/large/public/example.jpg?sa_ct=example'
    assert runtime.plugin.api_match_poster({'subject_id': candidate['subject_id'], 'poster_path': url}).success
    assert {k: v for k, v in runtime.plugin.data.items() if k != 'poster_cache'} == before
    preview = runtime.plugin.api_match_preview({'key': 'movie'}).data
    assert preview['revision'] == revision
    assert preview['candidates'] == [candidate]
    assert preview['poster_cache'] == {candidate['subject_id']: url}
    # 自动重试仍保存旧候选，不能覆盖独立的展示缓存。
    runtime.helper.get_subject_id.return_value = (None, None)
    runtime.helper.last_candidates = [candidate]
    runtime.helper.last_error = '年份不符'
    runtime.plugin._sync_item('movie', item, {}, runtime.plugin._load_dict('wait'))
    assert runtime.plugin.data['poster_cache'][candidate['subject_id']] == url
    runtime.helper.set_watching_status.assert_not_called()
    runtime.schedule.assert_not_called()


@pytest.mark.parametrize('poster', ['', 'https://img3.doubanio.com/f/frodo/pics/subject/tv_normal.png'])
def test_poster_cache_remembers_explicit_missing_image(runtime, poster):
    item, candidate = seed(runtime)
    item['match_candidates'] = [candidate]
    assert runtime.plugin.api_match_poster({'subject_id': candidate['subject_id'], 'poster_path': poster}).success
    assert runtime.plugin.api_match_preview({'key': 'movie'}).data['poster_cache'] == {candidate['subject_id']: ''}


@pytest.mark.parametrize('payload', [
    {'subject_id': '0', 'poster_path': ''}, {'subject_id': '1', 'poster_path': 'javascript:alert(1)'},
    {'subject_id': '1', 'poster_path': 'https://user:password@example.invalid/image.jpg'},
    {'subject_id': '1', 'poster_path': {}}, {'subject_id': '1', 'poster_path': 'https://' + 'a' * 4096},
])
def test_invalid_poster_cache_does_not_change_data(runtime, payload):
    seed(runtime)
    before = deepcopy(runtime.plugin.data)
    assert not runtime.plugin.api_match_poster(payload).success
    assert runtime.plugin.data == before


def test_poster_cache_reuses_loaded_images_and_bounds_failed_refreshes(runtime):
    m = runtime.module.matching
    script = r'''
const assert = require('node:assert/strict');
const ui = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const model = {_match: ui.state};
const invoke = (code, event) => new Function('model','event','with(model){return (' + code + ')(event)}')(model,event);
const image = ui.list.content[0].slots.prepend[0].content[0];
const candidate = {subject_id:'2079211',title:'乔治洛佩兹秀 第一季',year:'2002',type:'电视剧',
 reason:'名称不符',poster_path:'https://img3.doubanio.com/old.webp'};
const cache = {};
const posts = [];
let searches = 0;
let lookup;
let rejectWrites = false;
global.document = {baseURI:'https://mp.invalid/'};
global.fetch = async()=>({ok:true,text:async()=>'{"url":"assets/no-image-current.jpeg"}'});
global.window = {MoviePilotAPI:{
 post:async(url,body)=>{
  if(url.endsWith('/preview'))return {success:true,data:{original:{},keyword:'洛佩兹一家 第5季',
    candidates:[{...candidate}],revision:'unchanged',poster_cache:{...cache}}};
  assert.ok(url.endsWith('/poster')); // 不确认匹配，不修改观看状态。
  posts.push({...body});
  if(rejectWrites)throw new Error('cache offline');
  cache[body.subject_id] = body.poster_path;
  return {success:true};
 },
 get:async(url,opts)=>{searches++;assert.equal(url,'media/search');return {success:true,data:await lookup()};}
}};
const result = poster => [{media_source:'douban',media_id:'2079211',title:candidate.title,
 year:'2002',type:'电视剧',poster_path:poster}];
const current = () => model._match.candidates[0];
const load = url => invoke(image.props.onLoad,url);
const fail = url => invoke(image.props.onError,url);
(async()=>{
 const good = 'https://qnmob3-sign.doubanio.com/first.jpg?signature=one';
 lookup = async()=>result(good);
 await invoke(ui.open);
 assert.equal(searches,1);
 assert.equal(posts.length,0); // 搜索返回地址并不等于浏览器成功加载。
 await load(good);
 await load(good);
 assert.equal(posts.length,1);
 assert.equal(cache['2079211'],good);
 await invoke(ui.open);
 assert.equal(searches,1); // 重新打开，包括新的表单实例，只读持久缓存。
 model._match = {...ui.state};
 await invoke(ui.open);
 assert.equal(searches,1);
 assert.equal(current().poster_path,good);
 // 缓存地址失效，只重新搜索一次，并等新图片加载成功后再更新缓存。
 const newer = 'https://qnmob3-sign.doubanio.com/first.jpg?signature=two';
 lookup = async()=>result(newer);
 await fail(good);
 assert.equal(searches,2);
 assert.equal(current().poster_path,newer);
 assert.equal(cache['2079211'],good);
 await load(good); // 旧图片迟到的事件不能覆盖新 URL。
 assert.equal(posts.length,1);
 await load(newer);
 assert.equal(cache['2079211'],newer);
 await fail(newer);
 assert.equal(searches,2);
 assert.equal(current().poster_path,''); // 刷新图也失败，使用占位图且不再搜索。
 assert.equal(cache['2079211'],newer); // 临时失败不能持久化为无海报。
 await load('https://mp.invalid/assets/no-image-current.jpeg');
 assert.equal(cache['2079211'],newer);
 // 明确的豆瓣默认图会缓存为空；以后打开不重复搜索。
 delete cache['2079211'];
 lookup = async()=>result('https://img3.doubanio.com/f/frodo/pics/subject/tv_normal.png');
 await invoke(ui.open);
 assert.equal(cache['2079211'],'');
 const noImageSearches = searches;
 await invoke(ui.open);
 assert.equal(searches,noImageSearches);
 assert.equal(current().poster_path,'');
 // 用户主动搜索仍可更新无图缓存。
 lookup = async()=>result(newer);
 await invoke(ui.search);
 assert.equal(current().poster_path,newer);
 assert.equal(model._match.selected,null);
 await load(newer);
 assert.equal(cache['2079211'],newer);
 // 网络失败不清除缓存，不重复搜索，不阻碍选择和确认。
 await invoke(ui.open);
 lookup = async()=>{throw new Error('offline')};
 const beforeFailure = searches;
 await fail(newer);
 await fail(newer);
 assert.equal(searches,beforeFailure+1);
 assert.equal(current().poster_path,'');
 assert.equal(cache['2079211'],newer);
 assert.equal(model._match.error,false);
 assert.equal(model._match.busy,false);
 // 缓存写入失败也不影响图片显示。
 rejectWrites = true;
 lookup = async()=>result(good);
 await invoke(ui.search);
 await load(good);
 assert.equal(current().poster_path,good);
 assert.equal(model._match.error,false);
 rejectWrites = false;
 // 缓存刷新期间关闭弹窗，返回结果不得落库或更新之后的弹窗。
 await invoke(ui.open);
 let resolve;
 lookup = ()=>new Promise(r=>resolve=r);
 const beforeClose = posts.length;
 const pending = fail(newer);
 model._match.open = false;
 model._match.seq++;
 resolve(result(''));
 await pending;
 assert.equal(posts.length,beforeClose);
 assert.equal(cache['2079211'],newer);
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    data = {'state': m.initial_state(), 'open': m.open_handler('movie', 'DoubanWatchingPlus'),
            'list': m.candidate_list(), 'search': m.SEARCH}
    run_form_script(script, data)


def test_native_placeholder_uses_current_frontend_asset_and_tolerates_missing_manifest(runtime):
    script = r'''
const assert = require('node:assert/strict');
const code = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const resolve = new Function('state', 'return (async () => {' + code + '})();');
global.document = {baseURI:'https://mp.invalid/moviepilot/#/plugins'};
(async () => {
 let requests=0;
 global.fetch=async url=>{
  requests++; assert.equal(url,'https://mp.invalid/moviepilot/service-worker.js');
  return {ok:true,text:async()=>'{"url":"assets/no-image-updated.jpeg"}'};
 };
 const state={posterFallback:''}; await resolve(state);
 assert.equal(state.posterFallback,'https://mp.invalid/moviepilot/assets/no-image-updated.jpeg');
 await resolve(state); assert.equal(requests,1);
 for (const manifest of ['<html>Not a manifest</html>', '{"url":"https://unrelated.invalid/no-image.jpeg"}']) {
  global.fetch=async()=>({ok:true,text:async()=>manifest});
  const empty={posterFallback:''}; await resolve(empty); assert.equal(empty.posterFallback,'');
 }
 for (const fetcher of [async()=>({ok:false}),async()=>{throw new Error('offline');}]) {
  global.fetch=fetcher;
  const empty={posterFallback:''}; await resolve(empty); assert.equal(empty.posterFallback,'');
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    run_form_script(script, runtime.module.posters.FALLBACK_CLIENT)
