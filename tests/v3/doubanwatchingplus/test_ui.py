"""原生表单、全选交互、管理接口与手动操作结果展示。"""
import json
from copy import deepcopy

import pytest

from helpers import run_form_script
from helpers import (archive_item as item, request_archive_edit as request, run_archive_job as run_job,
                     form_nodes as nodes, seed_manual_match, preview_match_payload)


# 档案与设置页签

def seed_archive_tabs(runtime):
    runtime.plugin.data['data']={
        'a':{'subject_id':'1','subject_name':'电影甲','status':'do','timestamp':'2026-09-30 12:00:00'},
        'b':{'subject_id':'2','subject_name':'剧集乙','status':'collect'},
        'c':{'subject_id':'3','subject_name':'旧档案'},
    }


def test_tabs_open_on_archive_and_render_without_writing_or_contacting_douban(runtime):
    seed_archive_tabs(runtime);before=deepcopy(runtime.plugin.data)
    form,model=runtime.plugin.get_form()
    assert model['_ui_tab']=='archive'
    assert runtime.plugin.get_page() is None
    assert runtime.plugin.get_render_mode()==('vuetify',None)
    assert runtime.plugin.data==before
    runtime.helper_factory.assert_not_called()
    tabs=next(n for n in nodes(form) if n.get('component')=='VTabs')
    assert [n['text'] for n in tabs['content']]==['档案管理','插件设置']
    window=next(n for n in nodes(form) if n.get('component')=='VWindow')
    fields=lambda tree:{n.get('props',{}).get('model') for n in nodes(tree)}
    archive,settings=window['content']
    assert {'archive_item','archive_action','retry_now','retry_remove'} <= fields(archive)
    assert not {'cookie','enable'} & fields(archive)
    assert {'cookie','enable','user'} <= fields(settings)
    assert not {'archive_item','archive_action','retry_now','retry_remove'} & fields(settings)


def test_management_native_multiselect_submits_and_clears_legacy_search(runtime):
    seed_archive_tabs(runtime)
    form,model=runtime.plugin.get_form()
    field=next(n['props'] for n in nodes(form) if n.get('component')=='VAutocomplete')
    assert field['model']=='archive_item' and field['multiple'] is True
    assert field['items']=='_archive_options'
    assert {item['value'] for item in model['_archive_options']}=={'douban:1','douban:2','douban:3'}
    assert model['archive_search']==''
    assert field['model:search']=='archive_search'
    assert not any(n.get('component')=='VCheckbox' or n.get('props',{}).get('show') for n in nodes(form[0]))
    assert '全部档案' in json.dumps(form,ensure_ascii=False)
    runtime.plugin.init_plugin({**model,'enable':True,'archive_item':['douban:1','douban:3'],'archive_action':'collect','archive_search':'甲'})
    assert 'archive_search' not in runtime.plugin.config and '_archive_options' not in runtime.plugin.config
    assert runtime.plugin.config['archive_item']==[] and runtime.plugin.config['archive_action'] is None
    assert [r['group_key'] for r in runtime.plugin.data['archive_edit']['items']]==['douban:1','douban:3']
    runtime.helper_factory.assert_not_called()


# 原生全选交互

def test_select_all_scopes_selection_and_preserves_unmatched_items(runtime):
    props = runtime.module.views.select_all_slot()["prepend-item"][0]["props"]
    script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const props = JSON.parse(fs.readFileSync(0, 'utf8'));
const evalProp = (name, model) => new Function('model', 'with(model) { return ' + props[name].slice(2, -2) + '; }')(model);
const click = model => new Function('model', 'with(model) { return (' + props.onClick + ')(); }')(model);
const options = [
  {title: 'Alpha · 在看', value: 'a', props: {disabled: false}},
  {title: 'Alpha 2 · 看过', value: 'b', props: {disabled: false}},
  {title: 'Beta · 未确认', value: 'c', props: {disabled: false}},
  {title: 'Alpha broken · 未确认', value: 'bad', props: {disabled: true}},
  {title: 'Quote " test · 在看', value: 'quote', props: {disabled: false}},
];
const model = {_archive_options: options, archive_search: 'ALPHA', archive_item: ['c', 'a']};
assert.equal(evalProp('disabled', model), false);
assert.equal(evalProp('prepend-icon', model), 'mdi-minus-box');
assert.equal(evalProp('title', model), '全选当前结果（2）');
click(model);
assert.deepEqual(model.archive_item, ['c', 'a', 'b']);
assert.equal(evalProp('prepend-icon', model), 'mdi-checkbox-marked');
assert.equal(evalProp('title', model), '取消全选当前结果（2）');
click(model);
assert.deepEqual(model.archive_item, ['c']); // unrelated selection survives
model.archive_search = '不存在';
assert.equal(evalProp('disabled', model), true);
assert.equal(evalProp('prepend-icon', model), 'mdi-checkbox-blank-outline');
click(model);
assert.deepEqual(model.archive_item, ['c']); // no matches is a no-op
model.archive_search = 'broken';
assert.equal(evalProp('disabled', model), true); // disabled candidates are excluded
model.archive_search = null;
model.archive_item = null; // legacy saved defaults
click(model);
assert.deepEqual(model.archive_item, ['a', 'b', 'c', 'quote']);
click(model);
assert.deepEqual(model.archive_item, []);
model.archive_search = 'Quote "';
click(model);
assert.deepEqual(model.archive_item, ['quote']);
model._archive_options = [];
assert.equal(evalProp('disabled', model), true);
click(model);
assert.deepEqual(model.archive_item, ['quote']);
console.log('Selection boundaries verified');
'''
    run_form_script(script, props)


# 档案管理接口

def seed_archive_api(runtime):
    runtime.plugin.data['data']={'a':{'subject_id':'1','subject_name':'电影甲','status':'do','timestamp':'2026-09-30 12:00:00'},
                                 'b':{'subject_id':'2','subject_name':'电影乙','timestamp':'2026-09-29 12:00:00'}}


def test_read_is_local_only_and_does_not_return_secrets(runtime):
    seed_archive_api(runtime)
    runtime.plugin._cookie='secret'
    before=deepcopy(runtime.plugin.data)
    r=runtime.plugin.api_archive()
    assert r.success and len(r.data['records'])==2
    assert r.data['records'][1]['status']=='unknown'
    assert 'secret' not in str(r.data) and 'cookie' not in str(r.data)
    assert runtime.plugin.data==before
    runtime.helper_factory.assert_not_called()
    assert all(x['auth']=='bear' for x in runtime.plugin.get_api())
    assert all(x['methods']==['POST'] for x in runtime.plugin.get_api() if x['path']!='/archive')


def test_api_submit_schedules_without_config_reload_and_executes_selection(runtime):
    seed_archive_api(runtime);before=deepcopy(runtime.plugin.data['data'])
    r=runtime.plugin.api_archive_edit({'selection':['douban:1'],'action':'collect'})
    assert r.success and runtime.plugin.data['data']==before
    assert runtime.schedule.call_args.kwargs['delay_seconds']==3
    runtime.helper_factory.assert_not_called()
    runtime.schedule.call_args.args[2](**runtime.schedule.call_args.kwargs['func_kwargs'])
    runtime.helper.set_watching_status.assert_called_once_with('1','collect',True)
    assert runtime.plugin.data['data']['b']==before['b']
    assert runtime.plugin.api_archive().data['latest']['state']=='完成'


@pytest.mark.parametrize('payload',[{}, {'selection':[],'action':'remove'}, {'selection':['douban:1'],'action':[]}, {'selection':[{}],'action':'do'}, {'selection':'douban:1','action':'collect'}])
def test_bad_payload_does_not_write_or_schedule(runtime,payload):
    seed_archive_api(runtime);before=deepcopy(runtime.plugin.data)
    assert not runtime.plugin.api_archive_edit(payload).success
    assert runtime.plugin.data==before
    runtime.schedule.assert_not_called()


def test_busy_rejects_second_batch_and_queue_changes(runtime):
    seed_archive_api(runtime)
    assert runtime.plugin.api_archive_edit({'selection':['douban:1'],'action':'do'}).success
    before=deepcopy(runtime.plugin.data)
    assert not runtime.plugin.api_archive_edit({'selection':['douban:2'],'action':'collect'}).success
    assert not runtime.plugin.api_archive_retry().success
    assert not runtime.plugin.api_archive_drop_wait({'selection':['other']}).success
    assert runtime.plugin.data==before
    assert runtime.schedule.call_count==1


@pytest.mark.parametrize('raise_error',[False,True])
def test_scheduler_failure_is_visible_and_does_not_touch_archives(runtime,raise_error):
    seed_archive_api(runtime);before=deepcopy(runtime.plugin.data['data'])
    if raise_error:runtime.schedule.side_effect=RuntimeError('scheduler failed')
    else:runtime.schedule.return_value=False
    assert not runtime.plugin.api_archive_edit({'selection':['douban:1'],'action':'remove'}).success
    assert runtime.plugin._active_edit is None
    assert runtime.plugin.data['data']==before
    assert runtime.plugin.data['archive_edit']['items'][0]['state']=='已取消'
    assert runtime.plugin.data.get('wait',{})=={}
    runtime.helper_factory.assert_not_called()


def test_drop_wait_only_removes_explicit_failures(runtime):
    seed_archive_api(runtime);before=deepcopy(runtime.plugin.data['data'])
    runtime.plugin.data['wait']={'a':{'status':'do'},'b':{'status':'collect'}}
    assert not runtime.plugin.api_archive_drop_wait({'selection':[]}).success
    assert runtime.plugin.api_archive_drop_wait({'selection':['a']}).success
    assert set(runtime.plugin.data['wait'])=={'b'}
    assert runtime.plugin.data['data']==before
    runtime.helper_factory.assert_not_called()


def test_disabled_submit_rejected(runtime):
    seed_archive_api(runtime);runtime.plugin._enable=False
    assert not runtime.plugin.api_archive_edit({'selection':['douban:1'],'action':'collect'}).success
    assert not runtime.plugin.api_archive_retry().success
    runtime.schedule.assert_not_called()


# 统一手动操作结果

def result_alerts(plugin):
    form,_=plugin.get_form()
    archive=next(n for n in nodes(form[0]) if n.get('component')=='VWindowItem' and n['props']['value']=='archive')
    return [n for n in nodes(archive) if n.get('component')=='VAlert' and n.get('props',{}).get('density')=='compact']


def test_static_native_form_does_not_embed_form_render_or_dynamic_snapshot(runtime):
    seed_manual_match(runtime)
    form,model=runtime.plugin.get_form()
    assert form[0]['props']['id']=='doubanwatchingplus-config'
    assert form[-1]['component']=='VDialog'
    assert not any(n.get('component')=='FormRender' for n in nodes(form))
    assert '_archive_model' not in model
    assert not any(route['path']=='/archive/view' for route in runtime.plugin.get_api())
    assert {'archive_item','archive_action','retry_now','retry_remove'}<=set(model)


@pytest.mark.parametrize('outcome',['success','failed','skipped'])
def test_manual_match_reappears_in_single_result_area(runtime,outcome):
    original,_=seed_manual_match(runtime)
    runtime.plugin.data['archive_edit']={'state':'完成','message':'原编辑任务'}
    if outcome=='failed':
        runtime.helper.set_watching_status.return_value=False
        runtime.helper.last_error='写入失败原因'
    if outcome=='skipped':
        runtime.plugin.data['data']={'movie':{**original,'subject_id':'27114240'}}
    response=runtime.plugin.api_match_confirm(preview_match_payload(runtime))
    assert response.data['outcome']==outcome
    alerts=result_alerts(runtime.plugin)
    assert len(alerts)==1 and response.message==alerts[0]['text']
    assert alerts[0]['text'].startswith('[手动匹配]')
    assert runtime.plugin.data['archive_edit']['message']=='原编辑任务'
    if outcome=='failed':
        assert '写入失败原因' in json.dumps(runtime.plugin.get_form()[0],ensure_ascii=False)
        assert runtime.plugin.data['wait']['movie']['attempts']==4
    before=deepcopy(runtime.plugin.data)
    runtime.plugin.init_plugin({'enable':True})
    assert runtime.plugin.data==before
    assert result_alerts(runtime.plugin)[0]['text']==response.message


def test_edit_and_following_retry_are_one_operation_and_one_alert(runtime):
    runtime.plugin.data['data']={'saved':item(status='do')}
    runtime.plugin.data['wait']={'outside':{'title':'另外一部剧','subject_id':'888','type':'电视剧','status':'collect'}}
    request(runtime,'collect',retry_now=True)
    identity=runtime.plugin.data['manual_result']['id']
    run_job(runtime,'archive_edit')
    # 后续重试尚未执行时，原编辑结果与重试进度同时可见。
    current=runtime.plugin._manual_result()['message']
    assert '[档案编辑][成功]' in current and '[失败重试]' in current
    runtime.helper.set_watching_status.return_value=False
    runtime.helper.last_error='HTTP 403'
    run_job(runtime,'retry_waiting')
    assert runtime.plugin.data['manual_result']['id']==identity
    assert set(runtime.plugin.data['manual_result']['parts'])=={'档案编辑','批量重试'}
    alerts=result_alerts(runtime.plugin)
    assert len(alerts)==1 and alerts[0]['props']['type']=='warning'
    assert '[档案编辑][成功]' in alerts[0]['text'] and 'HTTP 403' in alerts[0]['text']
    assert len(runtime.plugin._manual_result()['items'])==2


def test_later_automatic_retry_cannot_replace_manual_result(runtime):
    seed_manual_match(runtime)
    runtime.plugin.api_match_confirm(preview_match_payload(runtime))
    before=deepcopy(runtime.plugin.data['manual_result'])
    runtime.plugin._queue_retry('同步成功后自动重试')
    run_job(runtime,'retry_waiting')
    assert runtime.plugin.data['manual_result']==before


def test_new_manual_retry_replaces_last_result_but_old_edit_callback_cannot(runtime):
    runtime.plugin.data['data']={'saved':item(status='do')}
    runtime.helper.set_watching_status.return_value=False
    request(runtime,'collect');run_job(runtime,'archive_edit')
    old=deepcopy(runtime.plugin.data['archive_edit'])
    runtime.helper.set_watching_status.return_value=True
    runtime.plugin._queue_retry('手动重试')
    run_job(runtime,'retry_waiting')
    assert set(runtime.plugin.data['manual_result']['parts'])=={'批量重试'}
    assert runtime.plugin.data['archive_edit']['state']=='完成'
    before=deepcopy(runtime.plugin.data['manual_result'])
    runtime.plugin._save_archive_progress(old)
    assert runtime.plugin.data['manual_result']==before
    assert len(result_alerts(runtime.plugin))==1


def test_all_skipped_edit_and_empty_retry_share_result(runtime):
    runtime.plugin.data['data']={'movie':{**item(status='collect'),'type':'电影'}}
    request(runtime,'do',retry_now=True)
    message=runtime.plugin._manual_result()['message']
    assert '电影不支持在看' in message and '没有待重试记录' in message
    assert len(result_alerts(runtime.plugin))==1
    runtime.schedule.assert_not_called()


@pytest.mark.parametrize('enabled',[False,True])
def test_retry_cancellation_is_visible(runtime,enabled):
    runtime.plugin.data['wait']={'pending':{'title':'待重试剧','subject_id':'888','type':'电视剧','status':'collect'}}
    runtime.plugin._enable=enabled
    runtime.schedule.return_value=False
    runtime.plugin._queue_retry('手动重试')
    alerts=result_alerts(runtime.plugin)
    assert len(alerts)==1 and alerts[0]['props']['type']=='warning'
    assert '[失败重试]' in alerts[0]['text'] and ('未能启动' in alerts[0]['text'] or '未启用' in alerts[0]['text'])


@pytest.mark.parametrize('outcome',['success','failed','rejected','transport'])
def test_confirm_closes_both_windows_before_response_and_does_not_refresh(runtime,outcome):
    script=r'''const assert=require('node:assert/strict');
const ui=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const model={_match:{...ui.state,open:true,revision:'r',selected:{subject_id:'1'},key:'k'}};
let closeCount=0,posts=0,resolve,reject;
global.document={getElementById:id=>{assert.equal(id,'doubanwatchingplus-config');return {closest:selector=>{
 assert.equal(selector,'.v-card');return {querySelector:selector=>{
 assert.equal(selector,':scope > button[aria-label]');return {click:()=>{assert.equal(model._match.open,false);closeCount++;}};}};}};}};
global.window={MoviePilotAPI:{post:(url,body,options)=>{posts++;assert.ok(url.endsWith('/match/confirm'));
 assert.notEqual(options.feedback,'silent');return new Promise((a,b)=>{resolve=a;reject=b;});},
 get:()=>{throw new Error('不应再刷新或重载表单');},put:()=>{throw new Error('不应保存未提交配置');}}};
const click=()=>new Function('model','with(model){return ('+ui.handler+')();}')(model);
(async()=>{
 const pending=click();
 assert.equal(closeCount,1);assert.equal(model._match.open,false); // 不等待豆瓣响应。
 await click();assert.equal(posts,1);
 if(ui.outcome==='transport')reject(new Error('请求超时'));
 else resolve({success:ui.outcome!=='rejected',message:'结果',data:{outcome:ui.outcome}});
 await pending;assert.equal(closeCount,1);assert.equal(model._match.confirming,false);
})().catch(e=>{console.error(e);process.exitCode=1});'''
    m=runtime.module.matching
    run_form_script(script, dict(state=m.initial_state(), handler=m.confirm_handler('DoubanWatchingPlus'), outcome=outcome))


def test_match_shows_pending_result_before_write_completes(runtime):
    seed_manual_match(runtime)
    def write(*args):
        assert '等待同步结果' in runtime.plugin._manual_result()['message']
        return True
    runtime.helper.set_watching_status.side_effect=write
    result=runtime.plugin.api_match_confirm(preview_match_payload(runtime))
    assert result.data['outcome']=='success'
    assert '等待同步结果' not in runtime.plugin._manual_result()['message']


def test_dashboard_year_month_limits_invalid_records_no_network(runtime):
    runtime.plugin.init_plugin({"pc_month": 3, "pc_num": 1, "mobile_month": "bad", "mobile_num": 0})
    runtime.plugin.data["data"] = {
        "new": {"timestamp": "2026-01-01 01:00:00", "subject_id": "1", "subject_name": "无海报"},
        "old": {"timestamp": "2025-01-01 01:00:00", "subject_id": "2", "poster_path": "https://image.invalid/p.jpg"},
        "older": {"timestamp": "2024-01-01 01:00:00", "subject_id": "3"},
        "bad": {"timestamp": "wrong"}, "none": None,
    }
    timeline = runtime.plugin.get_line_item()
    assert len(timeline) == 3
    assert timeline[0]["content"][0]["text"].startswith("2026年01月")
    assert timeline[1]["content"][0]["text"].startswith("2025年01月")
    assert runtime.plugin.get_dashboard(user_agent=None)
    assert runtime.plugin.get_page() is None
    runtime.recognize.assert_not_called()


def test_dashboard_and_management_share_subject_and_date_selection(runtime):
    runtime.plugin.data["data"] = {
        "old": {"subject_id": "1", "timestamp": "2025-01-01 12:00:00"},
        "confirmed": {"subject_id": "1", "timestamp": "2026-08-01 12:00:00", "status": "collect"},
        "unknown-alias": {"subject_id": "1", "timestamp": "2026-09-30 12:00:00"}}
    timeline = runtime.plugin.get_line_item()
    assert len(timeline) == 1
    assert timeline[0]["content"][0]["text"].startswith("2026年08月")
    assert runtime.plugin.api_archive().data["records"][0]["date"] == "2026-08-01 12:00:00"
    runtime.helper_factory.assert_not_called()
