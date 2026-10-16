"""原生表单中的人工匹配：复用 MP 的 API 客户端、媒体搜索及 Vuetify 控件。

参考 MoviePilot-Frontend/src/components/misc/MediaIdSelector.vue：
按关键词调用 media/search，固定豆瓣来源，展示候选后由用户确认。
不访问登录凭据、不放宽自动匹配、不依赖自定义前端构建。
"""
import hashlib
import json
import re

from . import posters


def revision(item):
    """确认时验证用户看到的仍是同一条待重试记录。"""
    return hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def selected_candidate(value):
    """只校验用户提交的字段格式；不再调用识别器重新判断人工选择。"""
    if not isinstance(value, dict):
        return None
    fields = ("subject_id", "title", "year", "type", "overview", "poster_path")
    result = {key: value.get(key, "") for key in fields}
    if (any(not isinstance(v, str) for v in result.values())
            or not re.fullmatch(r"[1-9]\d*", result["subject_id"])
            or not result["title"].strip() or len(result["title"]) > 500
            or result["type"] not in {"电影", "电视剧"}):
        return None
    result["title"] = result["title"].strip()
    result["year"] = result["year"][:16]
    result["overview"] = result["overview"][:1500]
    result["poster_path"] = result["poster_path"] if result["poster_path"].startswith(("https://", "http://")) else ""
    result["label"] = f"{result['title']}（{result['year'] or '年份未知'}） · {result['type']} · {result['subject_id']}"
    result["reason"] = ""
    return result


def candidate_list():
    """原生可选列表；海报是独立链接，点击不会同时改变选择。"""
    rows = []
    for index in range(20):
        item = f"_match.candidates[{index}]"
        selected = f"_match.selected?.subject_id === {item}?.subject_id"
        rows.append({"component": "VListItem", "props": {
            "show": "{{ !!" + item + " }}", "class": "py-2 rounded", "color": "primary",
            "title": "{{ " + item + "?.title || '' }}",
            "subtitle": "{{ (" + item + "?.year || '年份未知') + ' · ' + (" + item + "?.type || '') }}",
            "active": "{{ " + selected + " }}",
            "disabled": "{{ _match.busy || _match.confirming || _match.done }}",
            "onClick": "() => { _match.selected = " + item + "; }"},
            "slots": {"prepend": [{"component": "a", "props": {
                "href": "{{ " + item + " ? 'https://movie.douban.com/subject/' + " + item + ".subject_id + '/' : undefined }}",
                "target": "_blank", "rel": "noopener noreferrer", "class": "me-3",
                "aria-label": "{{ '打开豆瓣条目：' + (" + item + "?.title || '') }}",
                "onClick": "(event) => event.stopPropagation()"}, "content": [posters.image(item)]}],
                "append": [{"component": "VChip", "props": {"show": "{{ " + selected + " }}", "size": "small"}, "text": "已选"}]}})
    return {"component": "VList", "props": {"class": "mt-4", "style": "max-height:260px;overflow-y:auto;"}, "content": rows}


def initial_state():
    return dict(open=False, busy=False, confirming=False, done=False, seq=0, key='', revision='',
                keyword='', original={}, candidates=[], selected=None, message='', error=False, posterFallback='')


# MoviePilotAPI 返回宿主的 Response 外壳；无需读取 token 或自行拼接鉴权头。
_CLIENT = r'''
const api = window.MoviePilotAPI;
if (!api) throw new Error('当前前端未提供插件 API，请更新 MoviePilot 前端后重试');
const unwrap = value => {
    if (value && typeof value.success === 'boolean') {
        if (!value.success) throw new Error(value.message || '请求未成功');
        return value.data;
    }
    return value;
};
'''
# 只把片名交给宿主搜索，避免 MP 把搜索季数再次追加到各候选的真实季名后。
_SEARCH_CLIENT = _CLIENT + r'''
const searchTitle = keyword => {
    const title = String(keyword || '').normalize('NFKC').trim();
    return title.replace(/\s*第\s*[0-9零〇一二两三四五六七八九十]+\s*季\s*$/u, '')
        .replace(/\s+(?:season\s*|s)[0-9]+\s*$/iu, '').trim() || title;
};
const searchCandidates = async keyword => {
    const data = unwrap(await api.get('media/search', {params: {
        title: searchTitle(keyword), type: 'media', page: 1, count: 20, media_source: 'douban'
    }, feedback: 'silent'}));
    const seen = new Set();
    return (Array.isArray(data) ? data : []).filter(item => {
        const id = String(item.media_id || '');
        if (item.media_source !== 'douban' || !/^[1-9]\d*$/.test(id) || seen.has(id)) return false;
        seen.add(id); return true;
    }).map(item => ({subject_id: String(item.media_id), title: item.title || '', year: String(item.year || ''),
        type: item.type || '', overview: item.overview || '', poster_path: item.poster_path || '', reason: '',
        label: `${item.title || '未命名'}（${item.year || '年份未知'}） · ${item.type || '类型未知'} · ${item.media_id}`}));
};
'''
_ERROR = r'''
if (_match === s && s.seq === seq) {
    s.error = true;
    s.message = e?.payload?.message || e?.response?.data?.message || e?.message || '请求失败，请稍后重试';
}
'''


def open_handler(key, plugin_id):
    """key 通过 JSON 编码，外部标题不能变为可执行代码。"""
    return r'''async () => {
const s = ''' + json.dumps(initial_state(), ensure_ascii=False) + r''';
s.posterFallback = _match.posterFallback || '';
s.open = true; s.busy = true; s.key = ''' + json.dumps(key) + r''';
_match = s;
// 表单模型为 Vue reactive；保留代理身份用于过期响应判断。
const state = _match;
try {''' + posters.FALLBACK_CLIENT + _SEARCH_CLIENT + posters.client(plugin_id) + r'''
    if (_match !== state || !state.open) return;
    const data = unwrap(await api.post('plugin/''' + plugin_id + r'''/match/preview', {key: state.key}, {feedback: 'silent'}));
    if (_match !== state || !state.open) return;
    state.posterCache = data.poster_cache || {};
    await state.preparePosters(data.candidates);
    if (_match !== state || !state.open) return;
    Object.assign(state, data);
    state.selected = state.candidates[0] || null;
    state.message = state.candidates.length ? '请核对豆瓣条目；也可以修改关键词重新搜索。' : '暂无已保存候选，请按片名搜索豆瓣条目。';
} catch (e) {
    if (_match === state && state.open) {
        state.error = true;
        state.message = e?.payload?.message || e?.response?.data?.message || e?.message || '加载失败，请稍后重试';
    }
} finally { if (_match === state) state.busy = false; }
}'''


SEARCH = r'''async () => {
const s = _match;
if (s.busy || s.confirming || s.done || !s.keyword.trim()) return;
const seq = ++s.seq;
s.busy = true; s.error = false; s.message = ''; s.selected = null;
try {''' + _SEARCH_CLIENT + r'''
    const candidates = await searchCandidates(s.keyword);
    if (_match !== s || s.seq !== seq || !s.open) return;
    await s.preparePosters(candidates, true);
    if (_match !== s || s.seq !== seq || !s.open) return;
    s.candidates = candidates;
    s.message = s.candidates.length ? `找到 ${s.candidates.length} 个候选，请选择并核对。` : '未找到候选，请换一个片名或别名搜索。';
} catch (e) {''' + _ERROR + r'''
} finally { if (_match === s && s.seq === seq) s.busy = false; }
}'''


def confirm_handler(plugin_id):
    return r'''async () => {
const s = _match;
if (s.busy || s.confirming || s.done || !s.selected || !s.revision) return;
s.confirming = true; s.done = true;
// 只定位本插件表单所在卡片的原生关闭按钮，不操作其他弹窗或保存配置。
const close = document.getElementById('doubanwatchingplus-config')?.closest('.v-card')?.querySelector(':scope > button[aria-label]');
try {''' + _CLIENT + r'''
    const request = api.post('plugin/''' + plugin_id + r'''/match/confirm',
        {key: s.key, revision: s.revision, candidate: s.selected}, {timeout: 60000});
    s.open = false;
    close?.click();
    unwrap(await request);
} catch (e) {
    // 网络/鉴权错误由宿主 API 提示；豆瓣同步结果已存入插件，重新打开即可查看。
    console.warn('手动匹配请求未完成', e?.message || '请重新打开插件查看结果');
} finally {
    s.confirming = false;
    if (s.open) { s.open = false; close?.click(); }
}
}'''


def dialog(plugin_id):
    def text(expression, css='text-body-2'):
        return {'component': 'div', 'props': {'class': css, 'textContent': '{{ ' + expression + ' }}'}}
    close = '() => { if (!_match.confirming) { _match.open = false; _match.seq++; } }'
    return {'component': 'VDialog', 'props': {'modelValue': '{{ _match.open }}', 'max-width': 720,
        'persistent': '{{ _match.confirming }}',
        'onUpdate:modelValue': '(value) => { if (!_match.confirming) { _match.open = value; if (!value) _match.seq++; } }'},
        'content': [{'component': 'VCard', 'content': [
            {'component': 'VCardTitle', 'text': '手动确认豆瓣匹配'},
            {'component': 'VCardText', 'content': [
                text("(_match.original.title || '') + ' · ' + (_match.original.type || '') + ' · ' + (_match.original.year || '年份未知')", 'text-subtitle-1 mb-2'),
                text("'待同步：' + (_match.original.target || '')", 'text-caption mb-4'),
                {'component': 'VRow', 'props': {'align': 'center'}, 'content': [
                    {'component': 'VCol', 'props': {'cols': 12, 'sm': 9}, 'content': [
                        {'component': 'VTextField', 'props': {'label': '搜索豆瓣片名', 'modelValue': '{{ _match.keyword }}',
                            'onUpdate:modelValue': '(value) => { _match.keyword = value || ""; }',
                            'disabled': '{{ _match.busy || _match.confirming || _match.done }}',
                            'onKeydown': '(event) => { if (event.key === "Enter") { event.preventDefault(); (' + SEARCH + ')(); } }'}}]},
                    {'component': 'VCol', 'props': {'cols': 12, 'sm': 3}, 'content': [
                        {'component': 'VBtn', 'props': {'block': True, 'color': 'primary', 'variant': 'tonal',
                            'loading': '{{ _match.busy }}', 'disabled': '{{ _match.confirming || _match.done || !_match.keyword.trim() }}',
                            'onClick': SEARCH}, 'text': '搜索'}]}]},
                candidate_list(),
                {'component': 'VCard', 'props': {'variant': 'outlined', 'class': 'pa-3 my-3', 'show': '{{ !!_match.selected }}'}, 'content': [
                    text("_match.selected?.label || ''", 'text-subtitle-1'),
                    text("_match.selected?.overview || ''", 'text-body-2 mt-2'),
                    text("_match.selected?.reason ? '程序未采用的原因：' + _match.selected.reason : ''", 'text-body-2 text-warning mt-2')]},
                text('_match.message', "{{ _match.error ? 'text-body-2 text-error my-3' : 'text-body-2 my-3' }}"),
                {'component': 'div', 'props': {'class': 'text-caption'},
                 'text': '点击海报可跳转豆瓣。人工确认优先于自动识别，确认后将直接同步当前状态到该条目，请核对确认条目正确。'}]},
            {'component': 'VCardActions', 'content': [
                {'component': 'VSpacer'},
                {'component': 'VBtn', 'props': {'onClick': close, 'disabled': '{{ _match.confirming }}'}, 'text': '关闭'},
                {'component': 'VBtn', 'props': {'color': 'primary', 'variant': 'flat',
                    'loading': '{{ _match.confirming }}',
                    'disabled': '{{ _match.busy || _match.confirming || _match.done || !_match.selected || !_match.revision }}',
                    'onClick': confirm_handler(plugin_id)}, 'text': '确认匹配并同步'}]}]}]}
