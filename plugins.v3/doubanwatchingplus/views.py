"""原生页面与首页卡片的声明式布局；读取插件状态，不执行同步或保存配置。"""

import re
from datetime import datetime

from . import matching
from .migration import LegacyImporter



def form(plugin):
    """默认打开档案管理；账号、同步规则和首页显示参数放在第二个页签。"""
    def column(model, label, component="VTextField", md=12, sm=12, **extra):
        props = {"model": model, "label": label, **extra}
        return {"component": "VCol", "props": {"cols": 12, "sm": sm, "md": md},
                "content": [{"component": component, "props": props}]}

    rows = [
        {"component": "VRow", "content": [
            column("enable", "启用插件", "VSwitch", md=4),
            column("private", "豆瓣记录仅自己可见", "VSwitch", md=4),
            column("first", "不标记第一集", "VSwitch", md=4)]},
        {"component": "VRow", "content": [
            column("user", "媒体库用户名", md=6, placeholder="必填，多个用户名以逗号分隔。"),
            column("exclude", "媒体路径排除关键词", md=6, placeholder="多个关键词以逗号分隔。")]},
        {"component": "VRow", "content": [
            column("cookie", "豆瓣 Cookie", type="text", autocomplete="off",
                   placeholder="留空则使用 MoviePilot 已配置的 CookieCloud。")]},
        {"component": "VRow", "content": [
            column("pc_month", "大屏幕显示月份数", md=3, sm=6, type="number", min=1, max=36),
            column("pc_num", "大屏幕每月最多显示数", md=3, sm=6, type="number", min=1, max=500),
            column("mobile_month", "小屏幕显示月份数", md=3, sm=6, type="number", min=1, max=36),
            column("mobile_num", "小屏幕每月最多显示数", md=3, sm=6, type="number", min=1, max=500)]},
        {"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "density": "compact"}, "text":
                 "请开启媒体服务器 Webhook，并填写媒体库用户名。"
                 "Emby 需开启“停止播放”和“标记已播放”通知；“开始播放”通知仅记录日志。"}]},
            {"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "density": "compact"}, "text":
                 "跟随 Emby 的续播和已播放状态：电影已播放后标记看过；剧集可续播或普通集已播放时标记在看，"
                 "季末集已播放时标记整季看过。其他媒体服务器沿用原版同步规则。"}]}]}
    ]
    rows.extend(_migration_section(plugin, column))
    choices = _archive_choices(plugin)
    archive = _archive_section(plugin, column) + _retry_section(plugin, column)
    # 同一个表单模型连接页签和内容；切换不保存配置、不发起同步。
    return [{"component": "VForm", "props": {"id": "doubanwatchingplus-config"}, "content": [
        {"component": "VTabs", "props": {"model": "_ui_tab", "grow": True, "class": "mb-4"}, "content": [
            {"component": "VTab", "props": {"value": "archive"}, "text": "档案管理"},
            {"component": "VTab", "props": {"value": "settings"}, "text": "插件设置"}]},
        {"component": "VWindow", "props": {"model": "_ui_tab"}, "content": [
            {"component": "VWindowItem", "props": {"value": "archive"}, "content": archive},
            {"component": "VWindowItem", "props": {"value": "settings"}, "content": rows}]}]},
        matching.dialog(plugin.__class__.__name__)], {"_match": matching.initial_state(),
        "_ui_tab": "archive", "enable": False, "private": True, "first": True, "user": "", "exclude": "", "cookie": "",
        "pc_month": 3, "pc_num": 50, "mobile_month": 2, "mobile_num": 15,
        "archive_search": "", "_archive_options": choices,
        "retry_now": False, "retry_remove": [], "archive_item": [], "archive_action": None, "import_legacy": False}


def _migration_section(plugin, column) -> list[dict]:
    result = plugin._load_dict(LegacyImporter.RESULT)
    occupied = any(plugin.get_data(key) not in (None, {}) for key in ("data", "wait", "manual_matches"))
    interrupted = bool(plugin.get_data(LegacyImporter.JOURNAL))
    blocked = occupied or interrupted or result.get("state") == "完成"
    message = result.get("message") or (
        "当前已有档案、待重试记录或人工匹配，不能覆盖导入。" if occupied else
        "先停用 honue 原版插件，再勾选并保存，即可导入配置、档案和待重试记录。导入后请检查配置并启用本插件。"
        "旧档案缺少状态时显示“未确认”；首页请重新添加本插件卡片。")
    if interrupted:
        message = "导入未完成，插件保持停用；重新加载会尝试恢复导入前数据，详细原因见日志。"
    return [{"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
        {"component": "hr", "props": {"class": "v-divider mb-4", "role": "separator"}},
        {"component": "div", "props": {"class": "text-subtitle-1"}, "text": "原版插件数据导入（honue）"}]}]},
        {"component": "VRow", "content": [column("import_legacy", "保存后从原版导入（仅一次）", "VSwitch", disabled=blocked)]},
        {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
            {"component": "VAlert", "props": {"type": "warning" if result.get("state") in {"失败", "未执行"} else "info",
                "variant": "tonal", "density": "compact"}, "text": str(message)}]}]}]


def _archive_choices(plugin):
    """表单候选数据只用于当前页面，不写入插件配置。"""
    groups = plugin._archive_groups()
    labels = {"do": "在看", "collect": "看过", "unknown": "未确认"}
    return [{"title": f"{group['title']} · {labels[group['status']]}", "value": key,
                "props": {"disabled": not group["subject_id"]}}
               for key, group in sorted(groups.items(), key=lambda pair: (pair[1]["date"], pair[1]["title"]), reverse=True)]


def select_all_slot():
    """用原生顶部插槽补充全选，匹配规则对齐默认标题搜索。"""
    matches = "(_archive_options || []).filter(item => !item.props?.disabled && String(item.title).toLocaleLowerCase().includes(String(archive_search || '').toLocaleLowerCase()))"
    selected = "(Array.isArray(archive_item) ? archive_item : [])"
    all_selected = f"({matches}.length > 0 && {matches}.every(item => {selected}.includes(item.value)))"
    some_selected = f"{matches}.some(item => {selected}.includes(item.value))"
    toggle = "() => { const matches = " + matches + "; if (!matches.length) return; const current = " + selected + "; const keys = matches.map(item => item.value); archive_item = keys.every(key => current.includes(key)) ? current.filter(key => !keys.includes(key)) : [...new Set([...current, ...keys])]; }"
    # FormRender 总会传入默认插槽；VDivider 会为这个空插槽留缝，hr 复用原生分割线样式。
    return {"prepend-item": [
        {"component": "VListItem", "props": {
            "title": "{{ (" + all_selected + " ? '取消全选当前结果（' : '全选当前结果（') + " + matches + ".length + '）' }}",
            "prepend-icon": "{{ " + all_selected + " ? 'mdi-checkbox-marked' : (" + some_selected + " ? 'mdi-minus-box' : 'mdi-checkbox-blank-outline') }}",
            "disabled": "{{ " + matches + ".length === 0 }}",
            "onClick": toggle}},
        {"component": "hr", "props": {"class": "v-divider", "role": "separator", "aria-orientation": "horizontal"}}]}


def _archive_section(plugin, column) -> list[dict]:
    groups = plugin._archive_groups()
    latest = plugin._manual_result()
    message = latest["message"]
    content = [{"component": "div", "props": {"class": "text-subtitle-1 font-weight-medium mb-2"}, "text": "档案统计与编辑"},
               archive_stats(groups),
               {"component": "div", "props": {"class": "text-caption mt-2"},
                "text": "按豆瓣条目统计全部本地档案；旧记录未保存状态时显示为未确认。"}]
    rows = [{"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": content}]},
        {"component": "VRow", "content": [
            column("archive_item", "选择档案（可搜索、可多选）", "VAutocomplete", md=7, items="_archive_options",
                   multiple=True, chips=True, clearable=True,
                   hint="输入名称或状态搜索，可连续勾选多个条目。",
                   **{"model:search": "archive_search", "closable-chips": True, "persistent-hint": True, "no-data-text": "没有找到档案"}),
            column("archive_action", "保存后执行", "VSelect", md=5, clearable=True, items=[
                {"title": "改为在看并同步豆瓣（仅剧集）", "value": "do"},
                {"title": "改为看过并同步豆瓣", "value": "collect"},
                {"title": "取消豆瓣标记并移除档案", "value": "remove"}])]},
        {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12, "class": "text-caption"},
            "text": "选择操作后点击保存，条目间隔 3 秒。移除会先取消豆瓣标记；重新打开插件可查看结果。"}]}]
    rows[1]["content"][0]["content"][0]["slots"] = select_all_slot()
    if message:
        rows.append({"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
            {"component": "VAlert", "props": {"type": "warning" if latest.get("state") in {"失败", "部分失败", "部分完成", "未启动", "已取消"} else "info",
                                                "variant": "tonal", "density": "compact", "style": "white-space:pre-line;"}, "text": str(message)}]}]})
    results = latest.get("items")
    if isinstance(results, list) and len(results) > 1:
        details = [{"component": "div", "props": {"class": "py-2", "style": "overflow-wrap:anywhere;"},
            "content": [{"component": "div", "props": {"class": "font-weight-medium"},
                         "text": f"{row.get('title') or row.get('group_key')} · {plugin._result_label(row.get('state'))}"},
                        {"component": "div", "props": {"class": "text-caption"}, "text": str(row.get("message") or "")} ]}
                   for row in results if isinstance(row, dict)]
        rows.append({"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
            {"component": "VExpansionPanels", "props": {"variant": "accordion"}, "content": [
                {"component": "VExpansionPanel", "content": [
                    {"component": "VExpansionPanelTitle", "text": f"逐条结果（{len(results)} 条）"},
                    {"component": "VExpansionPanelText", "content": [{"component": "div", "props": {
                        "style": "max-height:360px;overflow-y:auto;"}, "content": details}]}]}]}]}]})
    return rows


def archive_stats(groups) -> dict:
    """本地全部档案统计，与首页海报的月份/数量限制独立。"""
    counts = {status: sum(group["status"] == status for group in groups.values())
              for status in ("do", "collect", "unknown")}
    values = [("全部档案", len(groups)), ("在看", counts["do"]),
              ("看过", counts["collect"]), ("未确认", counts["unknown"])]
    return {"component": "VRow", "props": {"class": "ma-0"}, "content": [
        {"component": "VCol", "props": {"cols": 6, "sm": 3, "class": "pa-1"}, "content": [
            {"component": "div", "props": {"class": "pa-3 rounded-lg", "style": "background:rgba(128,128,128,.08);"},
             "content": [{"component": "div", "props": {"class": "text-caption"}, "text": label},
                         {"component": "div", "props": {"class": "text-h6 font-weight-medium"}, "text": str(count)}]}]}
        for label, count in values]}


def _retry_section(plugin, column) -> list[dict]:
    """原生设置页只读取状态；一次性操作在用户保存后生效。"""
    waiting = plugin._load_dict("wait")
    cards, choices = [], []
    for key, original in waiting.items():
        item = original if isinstance(original, dict) else {}
        title = str(item.get("title") or key)
        count = plugin._failure_count(item.get("attempts"))
        count_text = str(count) if "attempts" in item else "未知（旧记录未记录）"
        reason = plugin._pending_reason(item)
        target = plugin._action_label(item.get("status"))
        choices.append({"title": title, "value": key})
        cards.append({"component": "div", "props": {
            "class": "pa-3 mb-2", "style": "border:1px solid rgba(128,128,128,.25);border-radius:8px;overflow-wrap:anywhere;"},
            "content": [
                {"component": "div", "props": {"class": "d-flex flex-wrap align-center", "style": "gap:8px;"}, "content": [
                    {"component": "div", "props": {"class": "font-weight-medium"}, "text": title},
                    {"component": "VChip", "props": {"size": "small", "variant": "tonal"}, "text": "待取消豆瓣标记" if item.get("status") == "remove" else f"待同步{target}"}]},
                {"component": "div", "props": {"class": "d-flex flex-wrap text-body-2 mt-2", "style": "column-gap:16px;row-gap:4px;"},
                 "content": [{"component": "span", "text": f"累计失败：{count_text}"},
                             {"component": "span", "text": f"最后尝试：{item.get('last_attempt') or '未记录'}"}]},
                {"component": "div", "props": {"class": "text-body-2 mt-2"}, "text": f"原因：{reason}"}]})
        if item.get("matched_by") == "manual" and item.get("subject_id"):
            cards[-1]["content"].append({"component": "div", "props": {"class": "text-caption mt-2"},
                "text": f"已手动确认：{item.get('subject_name') or title}（豆瓣 ID：{item['subject_id']}）；后续按此条目重试"})
        if plugin._can_match(item):
            cards[-1]["content"].append({"component": "VBtn", "props": {
                "class": "mt-3", "variant": "tonal", "size": "small", "color": "primary",
                "onClick": matching.open_handler(key, plugin.__class__.__name__)}, "text": "手动确认匹配"})
    return [
        {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12, "class": "pb-0"},
            "content": [{"component": "hr", "props": {"class": "v-divider mb-4", "role": "separator", "aria-orientation": "horizontal"}}]}]},
        {"component": "VRow", "props": {"align": "center"}, "content": [
            {"component": "VCol", "props": {"cols": 12, "md": 4, "class": "pb-0"}, "content": [
                {"component": "div", "props": {"class": "text-subtitle-1 font-weight-medium"}, "text": f"待重试 {len(waiting)} 条"}]},
            column("retry_now", "保存后重试全部（仅一次）", "VSwitch", md=8, density="compact", disabled=not waiting)]},
        {"component": "VRow", "content": [column("retry_remove", "保存后移除所选失败记录", "VSelect", items=choices,
            multiple=True, chips=True, clearable=True, disabled=not waiting,
            hint="只移除待重试记录，不删除观影档案。", **{"persistent-hint": True})]},
        {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
            {"component": "div", "props": {"style": "max-height:360px;overflow-y:auto;"}, "content": cards}]}]}
    ]


def timeline_items(plugin, mobile: bool = False) -> list[dict]:
    """仅渲染本地档案，按年月分组；页面刷新不触发网络识别。"""
    groups = {}
    dated = []
    for group in plugin._archive_groups().values():
        stamp = plugin._valid_timestamp(group["date"])
        if group["subject_id"] and stamp:
            dated.append((datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S"), group["record"]))
    month_limit = plugin._mobile_month if mobile else plugin._pc_month
    number_limit = plugin._mobile_num if mobile else plugin._pc_num
    for stamp, value in sorted(dated, key=lambda pair: pair[0], reverse=True):
        month = stamp.strftime("%Y年%m月")
        if month not in groups and len(groups) >= month_limit:
            break
        groups.setdefault(month, []).append(value)
    content = []
    width, height = (44, 66) if mobile else (66, 99)
    for month, values in groups.items():
        cards = []
        for value in values[:number_limit]:
            subject_id = str(value.get("subject_id") or "")
            if not subject_id.isdigit():
                continue
            title = value.get("subject_name") or value.get("title") or "豆瓣条目"
            poster = value.get("poster_path") or ""
            # 保留原版紧凑海报墙；长标题只作链接说明，不再撑大卡片或挤占下一行。
            poster_props = {"width": width, "height": height, "aspect-ratio": 2 / 3,
                            "cover": True, "alt": title}
            if poster.startswith(("https://", "http://")):
                poster_props["src"] = poster.replace("/original/", "/w200/")
            image = {"component": "VImg", "props": poster_props}
            if "src" not in poster_props:
                image["content"] = [{"component": "div", "props": {
                    "class": "d-flex align-center justify-center pa-1 text-caption",
                    "style": "height:100%;overflow:hidden;overflow-wrap:anywhere;text-align:center;"
                }, "text": title}]
            cards.append({"component": "a", "props": {
                "href": f"https://movie.douban.com/subject/{subject_id}/", "target": "_blank",
                "rel": "noopener noreferrer", "title": title, "aria-label": title,
                "style": f"display:block;flex:0 0 {width}px;width:{width}px;"
            }, "content": [{"component": "VCard", "props": {
                "width": width, "height": height, "class": "elevation-4",
                "style": "overflow:hidden;"
            }, "content": [image]}]})
        content.append({"component": "VTimelineItem", "props": {"size": "x-small"}, "content": [
            {"component": "h3", "props": {
                "class": "text-base font-weight-bold mb-3",
                "style": "line-height:1.5;"
            }, "text": f"{month} · {len(values)} 部"},
            # VRow 自带负边距；直接放海报会与月份标题重叠，改用无负边距的换行容器。
            {"component": "div", "props": {
                "class": "d-flex flex-wrap",
                "style": "gap:8px;align-items:flex-start;"
            }, "content": cards}]})
    return content


def _timeline(plugin, mobile: bool = False) -> dict:
    """对齐月份顶部的紧凑时间线，宽度跟随页面或仪表板容器。"""
    return {"component": "VTimeline", "props": {
        "direction": "vertical", "side": "end", "align": "start", "density": "compact",
        "hide-opposite": True, "dot-color": "#AF85FD",
        "style": "width:100%;padding:16px 8px;"
    }, "content": timeline_items(plugin, mobile)}


def dashboard(plugin, **kwargs):
    """沿用原插件桌面 66×99、移动端 44×66 的海报规格。"""
    mobile = bool(re.search(r"Mobile|Android|iPhone|iPad", kwargs.get("user_agent") or "", re.I))
    return {"cols": 12, "md": 12}, {"refresh": 600, "border": False}, [_timeline(plugin, mobile)]
