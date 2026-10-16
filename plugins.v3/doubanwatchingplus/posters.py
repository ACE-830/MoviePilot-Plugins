"""手动匹配海报的地址校验、原生占位图和缓存生命周期；不处理媒体匹配或观看状态。"""

import json
import re
from urllib.parse import urlsplit


_DEFAULT_POSTER = r"/^https?:\/\/(?:[^/]+\.)?doubanio\.com\/[^?#]*\/(?:movie|tv)_normal\.(?:png|jpe?g|webp)(?:[?#]|$)/i"


def normalize_url(value):
    """空字符串表示豆瓣明确无图；None 表示不能缓存的输入。"""
    if not isinstance(value, str) or len(value) > 4096:
        return None
    if not value:
        return ""
    try:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            return None
    except ValueError:
        return None
    if (url.hostname == "doubanio.com" or url.hostname.endswith(".doubanio.com")) and re.search(
            r"/(?:movie|tv)_normal\.(?:png|jpe?g|webp)$", url.path, re.I):
        return ""
    return value


def image(candidate):
    """候选图片使用原生加载/失败事件；错误插槽复用 MP 占位图。"""
    source = f"({candidate}?.poster_path || '')"
    fallback = "_match.posterFallback || undefined"
    poster = f"/^https?:\\/\\//.test({source}) && !{_DEFAULT_POSTER}.test({source}) ? {candidate}.poster_path : ({fallback})"
    props = {"width": 60, "height": 90, "cover": True, "class": "rounded"}
    return {"component": "VImg", "props": {**props, "src": "{{ " + poster + " }}",
        "key": "{{ " + candidate + "?.subject_id + ':' + " + source + " }}",
        "onLoad": "(url) => _match.posterLoaded?.(" + candidate + ", url)",
        "onError": "(url) => _match.posterFailed?.(" + candidate + ", url)",
        "referrerpolicy": "no-referrer", "alt": "{{ " + candidate + "?.title || '暂无海报' }}"},
        "slots": {"error": [{"component": "VImg", "props": {
            **props, "src": "{{ " + fallback + " }}", "alt": "暂无海报"}}]}}


# MP 未导出默认图片全局变量；只从前端预缓存清单解析资源地址，不执行脚本。
FALLBACK_CLIENT = r'''
if (!state.posterFallback) {
    try {
        const manifest = new URL('service-worker.js', document.baseURI);
        const response = await fetch(manifest.href, {cache: 'no-cache', signal: AbortSignal.timeout(5000)});
        if (response.ok) {
            const match = (await response.text()).match(/["']((?:\.\/|\/)?assets\/no-image(?:-[\w-]+)?\.jpe?g)["']/i);
            if (match) state.posterFallback = new URL(match[1], manifest).href;
        }
    } catch (_) { /* 占位图获取失败不阻止搜索和人工确认。 */ }
}
'''


# VImg 的 load/error 事件驱动缓存：成功加载才保存，临时失败不记作永久无图。
CACHE_CLIENT = r'''
const searches = new Map();
const writes = new Map();
const live = seq => _match === state && state.open && state.seq === seq;
const posterURL = value => {
    if (!/^https?:\/\//.test(value || '')) return '';
    return __DEFAULT_POSTER__.test(value) ? '' : value;
};
const savePoster = async (candidate, url) => {
    const sid = candidate.subject_id;
    const key = sid + ':' + url;
    if (state.posterCache[sid] === url || writes.has(key)) return;
    const request = (async () => {
        try {
            unwrap(await api.post(posterEndpoint, {subject_id: sid, poster_path: url}, {feedback: 'silent'}));
            state.posterCache[sid] = url;
        } catch (_) { /* 缓存写入失败不阻止核对或同步。 */ }
    })();
    writes.set(key, request);
    try { await request; } finally { writes.delete(key); }
};
const refreshPoster = async (candidate, seq) => {
    candidate._posterRefreshed = true;
    const keyword = searchTitle(candidate.title);
    if (!keyword || !live(seq)) return;
    // 同一弹窗、同一轮搜索的同名各季共用请求，包括失败结果。
    const key = seq + ':' + keyword;
    if (!searches.has(key)) searches.set(key, searchCandidates(keyword).catch(() => []));
    const found = (await searches.get(key)).find(row => row.subject_id === candidate.subject_id);
    if (!live(seq) || !found) return;
    candidate.poster_path = posterURL(found.poster_path);
    if (!candidate.poster_path) await savePoster(candidate, '');
};
state.preparePosters = async (candidates, fresh = false) => {
    const seq = state.seq;
    for (const candidate of candidates) {
        if (!live(seq)) return;
        candidate._posterRefreshed = fresh;
        if (fresh) {
            candidate.poster_path = posterURL(candidate.poster_path);
            if (!candidate.poster_path) await savePoster(candidate, '');
        } else if (Object.hasOwn(state.posterCache, candidate.subject_id)) {
            candidate.poster_path = state.posterCache[candidate.subject_id];
        } else {
            await refreshPoster(candidate, seq);
        }
    }
};
state.posterLoaded = async (candidate, url) => {
    if (!live(state.seq) || state.busy || !state.candidates.includes(candidate) || !url || url !== candidate.poster_path) return;
    await savePoster(candidate, url);
};
state.posterFailed = async (candidate, url) => {
    const seq = state.seq;
    if (!live(seq) || state.busy || candidate._posterRefreshing || !state.candidates.includes(candidate) || !url || url !== candidate.poster_path) return;
    // 缓存图失败只刷新一次；新搜索图再失败直接使用原生占位图。
    candidate._posterRefreshing = true;
    if (!candidate._posterRefreshed) await refreshPoster(candidate, seq);
    candidate._posterRefreshing = false;
    if (live(seq) && state.candidates.includes(candidate) && candidate.poster_path === url) candidate.poster_path = '';
};
'''.replace("__DEFAULT_POSTER__", _DEFAULT_POSTER)


def client(plugin_id):
    """在匹配弹窗中初始化缓存控制器，复用宿主 API 与已定义的豆瓣搜索函数。"""
    endpoint = json.dumps("plugin/" + plugin_id + "/match/poster")
    return "\nconst posterEndpoint = " + endpoint + ";\n" + CACHE_CLIENT
