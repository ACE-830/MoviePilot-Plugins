"""测试共用的模拟响应、档案、人工匹配数据和表单遍历；不依赖其他测试模块。"""

import json
import os
import shutil
import subprocess
from copy import deepcopy
from html import escape
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest


def archive_item(sid="999", status=None, title="测试剧", stamp="2025-01-01 12:00:00"):
    value = {"subject_id": sid, "subject_name": title, "timestamp": stamp, "type": "电视剧",
             "poster_path": "https://example.invalid/poster.jpg"}
    if status is not None:
        value["status"] = status
    return value


def request_archive_edit(runtime, action, key="douban:999", **extra):
    runtime.plugin.init_plugin({"enable": True, "user": "alice", "archive_item": key,
                                "archive_action": action, **extra})


def run_archive_job(runtime, name):
    if name == "archive_edit":
        service = runtime.plugin.get_service()[0]
        service["func"](**service["func_kwargs"])
        return
    call = next(call for call in reversed(runtime.schedule.call_args_list) if call.args[1] == name)
    call.args[2](**call.kwargs["func_kwargs"])


def douban_response(code=200, payload=None, text="", headers=None):
    """构造明确可关闭的 HTTP 响应。"""
    return Mock(status_code=code, json=Mock(return_value=payload), text=text, headers=headers or {})


def douban_result(runtime, title="测试剧", subject="999", year="2024", **overrides):
    values = dict(title=title, media_id=subject, year=year,
                  media_source=runtime.MediaSource.Douban, type=runtime.MediaType.TV)
    values.update(overrides)
    return SimpleNamespace(**values)


def douban_web_search(*items):
    return douban_response(text="<title>豆瓣搜索</title>" + "".join(
        f'<div class="title"><a href="https://www.douban.com/link2/?{urlencode({"url": "https://movie.douban.com/subject/" + sid + "/"})}">{name}</a></div>'
        for sid, name in items))


def douban_web_detail(subject="7916280", name="嗜血法医 第七季", year="2012", kind="TVSeries", **extra):
    data = {"@type": kind, "url": f"/subject/{subject}/", "datePublished": f"{year}-09-30"}
    data.update(extra)
    return douban_response(text=f'<title>{escape(name)} (豆瓣)</title>'
                         f'<script type="application/ld+json">{json.dumps(data)}</script>')


def emby_item(played=False, **extra):
    """构造 Emby 单集和用户状态；默认仅有即时进度，不代表可续播。"""
    return {"Id": "episode-2", "Type": "Episode", "SeriesName": "测试剧", "SeriesId": "series-123",
            "ParentIndexNumber": 1, "IndexNumber": 2, "UserData": {"Played": played, "PlaybackPositionTicks": 1}, **extra}


def emby_service(runtime, payloads):
    responses = [Mock(status_code=200, json=Mock(return_value=payload)) for payload in payloads]
    instance = Mock()
    instance.get_data.side_effect = responses
    service = SimpleNamespace(instance=instance)
    runtime.services.get_service.return_value = service
    runtime.services.get_services.return_value = {"nas": service}
    return instance, responses


def seed_manual_match(runtime):
    item = {'title': '冒牌上尉', 'year': '2018', 'type': '电影', 'status': 'collect',
            'media_id': '475094', 'media_source': 'themoviedb', 'preferred_subject_id': '27114240',
            'timestamp': '2026-10-03 20:46:53', 'last_attempt': '2026-10-05 16:53:32',
            'attempts': 3, 'last_error': '年份不符'}
    candidate = {'title': '冒牌上尉', 'subject_id': '27114240', 'year': '2017', 'type': '电影',
                 'poster_path': 'https://example.invalid/poster.jpg', 'overview': '电影介绍', 'reason': '年份不符', 'label': '冒牌上尉（2017） · 电影 · 27114240'}
    runtime.plugin.data['wait'] = {'movie': item, 'other': {'title': '其他剧', 'type': '电视剧', 'status': 'do'}}
    runtime.helper.describe_subject.return_value = deepcopy(candidate)
    return item, candidate


def preview_match_payload(runtime):
    preview = runtime.plugin.api_match_preview({'key': 'movie'})
    assert preview.success
    return {'key': 'movie', 'revision': preview.data['revision'], 'candidate': deepcopy(preview.data['candidates'][0])}


def form_nodes(tree):
    if isinstance(tree, dict):
        yield tree
        for value in tree.values():
            yield from form_nodes(value)
    elif isinstance(tree, list):
        for value in tree:
            yield from form_nodes(value)


def run_form_script(script, payload, timeout=15):
    """共用的离线 FormRender 交互检查；Node 路径可由开发环境覆盖。"""
    node = os.environ.get("MOVIEPILOT_TEST_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for FormRender interaction checks")
    result = subprocess.run([node, "-e", script], input=json.dumps(payload), text=True,
                            capture_output=True, timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr
