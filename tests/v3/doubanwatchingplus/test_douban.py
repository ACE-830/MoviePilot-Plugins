"""豆瓣 HTTP 边界测试，所有响应均为本地替身。"""

from unittest.mock import Mock

import pytest

from helpers import (douban_response as response, douban_result as result,
                     douban_web_search as web_search, douban_web_detail as web_detail)


@pytest.mark.parametrize("payload,success", [({"r": 0}, True), ({"r": False}, False), ({"r": True}, False),
                                           ({"r": "0"}, False), ({"r": 1}, False), ({}, False), ([], False)])
def test_success_requires_integer_zero(runtime, monkeypatch, payload, success):
    helper = runtime.helper_module.DoubanHelper("ck=secret; dbcl2=account")
    res = response(payload=payload)
    monkeypatch.setattr(helper, "_request", Mock(return_value=res))
    assert helper.set_watching_status("999", "collect") is success
    res.close.assert_called_once()


def test_timeout_and_malformed_json(runtime, monkeypatch):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    monkeypatch.setattr(helper, "_request", Mock(return_value=None))
    assert helper.get_subject_id("测试剧") == (None, None)
    assert not helper.set_watching_status("999")
    res = response()
    res.json.side_effect = ValueError("bad json")
    helper._request.return_value = res
    assert not helper.set_watching_status("999")


def test_403_refresh_retries_once_with_new_cookie(runtime, monkeypatch):
    helper = runtime.helper_module.DoubanHelper("ck=old; dbcl2=secret")
    request = Mock(side_effect=[response(403), response(headers={"Set-Cookie": "ck=new; Path=/"}),
                                response(payload={"r": 0})])
    monkeypatch.setattr(helper, "_request", request)
    assert helper.set_watching_status("999", private=False)
    assert request.call_count == 3
    assert request.call_args.kwargs["data"]["ck"] == "new"
    assert "private" not in request.call_args.kwargs["data"]
    assert helper.cookies["dbcl2"] == "secret"


def test_403_cannot_loop_forever(runtime, monkeypatch):
    helper = runtime.helper_module.DoubanHelper("ck=old")
    request = Mock(side_effect=[response(403), response(headers={"Set-Cookie": "ck=new; Path=/"}), response(403)])
    monkeypatch.setattr(helper, "_request", request)
    assert not helper.set_watching_status("999")
    assert request.call_count == 3


def test_search_uses_frodo_chain_and_filters_year(runtime):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    runtime.search.return_value = [result(runtime, year="2022"), result(runtime, subject="888"),
                                   result(runtime, title="其他剧")]
    assert helper.get_subject_id("测试剧", "2024", runtime.MediaType.TV) == ("测试剧", "888")
    args = runtime.search.call_args.kwargs
    assert args["media_source"] == runtime.MediaSource.Douban
    assert args["meta"].name == "测试剧"
    assert args["meta"].type == runtime.MediaType.TV
    assert args["meta"].year == "2024"


def test_ambiguous_search_not_first_result(runtime):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    runtime.search.return_value = [result(runtime), result(runtime, subject="888")]
    assert helper.get_subject_id("测试剧", "2024") == (None, None)


def test_first_season_and_chinese_season_match(runtime):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    runtime.search.return_value = [result(runtime, title="测试剧 第一季")]
    assert helper.get_subject_id("测试剧", "2024") == ("测试剧 第一季", "999")
    runtime.search.return_value = [result(runtime, title="测试剧 第二季")]
    assert helper.get_subject_id("测试剧 第2季", "2024") == ("测试剧 第二季", "999")
    meta = runtime.search.call_args.kwargs["meta"]
    assert meta.name == "测试剧"
    assert meta.begin_season is None  # 防止宿主给所有结果追加第二季，误用第一季 ID。
    assert meta.media_id is None and meta.media_source is None


@pytest.mark.parametrize("overrides", [{"media_source": "themoviedb"}, {"media_id": "invalid"},
                                       {"type": "图书"}, {"year": None}, {"title": "测试剧 第二季"}])
def test_search_rejects_wrong_identity_type_year_or_season(runtime, overrides):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    runtime.search.return_value = [result(runtime, **overrides)]
    assert helper.get_subject_id("测试剧", "2024", runtime.MediaType.TV) == (None, None)


def test_movie_and_tv_with_same_title_are_distinct(runtime):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    runtime.search.return_value = [result(runtime, type=runtime.MediaType.MOVIE)]
    assert helper.get_subject_id("测试剧", "2024", runtime.MediaType.TV) == (None, None)


def test_preferred_id_is_validated_before_search(runtime):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    runtime.recognize.return_value = result(runtime, subject="777")
    assert helper.get_subject_id("测试剧", "2024", runtime.MediaType.TV, "777") == ("测试剧", "777")
    args = runtime.recognize.call_args.kwargs
    assert args["media_source"] == runtime.MediaSource.Douban and args["media_id"] == "777"
    assert args["meta"] is None  # 不允许给媒体库 ID 的实际标题追加目标季号。
    runtime.search.assert_not_called()


@pytest.mark.parametrize("name", ["我们与恶的距离2", "我们与恶的距离 第二季", "我们与恶的距离 第2季"])
def test_base_query_matches_second_season_alias_without_archive(runtime, name):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    runtime.recognize.return_value = result(runtime, title="我们与恶的距离", subject="30181230", year="2019")
    runtime.search.return_value = [runtime.recognize.return_value,
        result(runtime, title=name, subject="33437319", year="2025")]
    assert helper.get_subject_id("我们与恶的距离 第2季", "2025", runtime.MediaType.TV,
                                 "30181230", season=2) == (name, "33437319")
    meta = runtime.search.call_args.kwargs["meta"]
    assert meta.name == "我们与恶的距离" and meta.begin_season is None
    assert helper.last_error == ""


@pytest.mark.parametrize("name,year,kind", [
    ("测试剧", "2025", "TV"), ("测试剧3", "2025", "TV"),
    ("测试剧2", "2024", "TV"), ("测试剧2", "2025", "MOVIE"),
])
def test_broad_query_does_not_accept_wrong_season_year_or_type(runtime, name, year, kind):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    helper.cookies = {}
    runtime.search.return_value = [result(runtime, title=name, year=year, type=getattr(runtime.MediaType, kind))]
    assert helper.get_subject_id("测试剧 第2季", "2025", runtime.MediaType.TV, season=2) == (None, None)
    assert "MP 豆瓣搜索无合格候选" in helper.last_error


def test_alias_needs_year_and_never_picks_first_of_two_ids(runtime):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    helper.cookies = {}
    runtime.search.return_value = [result(runtime, title="测试剧2")]
    assert helper.get_subject_id("测试剧 第2季", mtype=runtime.MediaType.TV, season=2) == (None, None)
    assert "目标年份缺失" in helper.last_error
    runtime.search.return_value.append(result(runtime, title="测试剧 第二季", subject="888"))
    assert helper.get_subject_id("测试剧 第2季", "2024", runtime.MediaType.TV, season=2) == (None, None)
    assert "多个候选（2 个）" in helper.last_error


def test_numeric_series_name_and_movie_sequel_keep_their_full_query(runtime):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    runtime.search.return_value = [result(runtime, title="1899")]
    assert helper.get_subject_id("1899", "2024", runtime.MediaType.TV, season=1) == ("1899", "999")
    assert runtime.search.call_args.kwargs["meta"].name == "1899"
    runtime.search.return_value = [result(runtime, title="测试电影2", type=runtime.MediaType.MOVIE)]
    assert helper.get_subject_id("测试电影2", "2024", runtime.MediaType.MOVIE) == ("测试电影2", "999")
    assert runtime.search.call_args.kwargs["meta"].name == "测试电影2"


def test_series_id_for_first_season_cannot_mark_second_season(runtime):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    runtime.recognize.return_value = result(runtime, title="测试剧 第一季", subject="777")
    runtime.search.return_value = [result(runtime, title="测试剧 第二季", subject="888", year="2025")]
    assert helper.get_subject_id("测试剧 第2季", "2025", runtime.MediaType.TV, "777") == ("测试剧 第二季", "888")


def test_preferred_lookup_error_falls_back_and_search_error_is_retryable(runtime):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    runtime.recognize.side_effect = RuntimeError("private details")
    runtime.search.return_value = [result(runtime)]
    assert helper.get_subject_id("测试剧", "2024", runtime.MediaType.TV, "777") == ("测试剧", "999")
    runtime.search.side_effect = RuntimeError("private details")
    assert helper.get_subject_id("测试剧") == (None, None)


def test_http_sdk_has_cookie_timeout_and_no_secret_logs(runtime, monkeypatch, caplog):
    with caplog.at_level("DEBUG"):
        helper = runtime.helper_module.DoubanHelper("ck=topsecret; dbcl2=loginsecret")
        request = Mock()
        request.post_res.return_value = response(payload={"r": 0})
        factory = Mock(return_value=request)
        monkeypatch.setattr(runtime.helper_module, "RequestUtils", factory)
        assert helper.set_watching_status("999")
    assert factory.call_args.kwargs["cookies"]["ck"] == "topsecret"
    assert factory.call_args.kwargs["timeout"] == 15
    assert factory.call_args.kwargs["verify"] is True
    assert request.post_res.call_args.kwargs["allow_redirects"] is False
    assert "topsecret" not in caplog.text
    assert "loginsecret" not in caplog.text


def test_empty_cookiecloud_is_safe(runtime, monkeypatch):
    cloud = Mock()
    cloud.download.return_value = (None, "offline")
    monkeypatch.setattr(runtime.helper_module, "CookieCloudHelper", Mock(return_value=cloud))
    helper = runtime.helper_module.DoubanHelper()
    assert not helper.set_watching_status("999")


def test_cookie_refresh_failure_preserves_old_ck(runtime, monkeypatch):
    helper = runtime.helper_module.DoubanHelper("ck=old")
    monkeypatch.setattr(helper, "_request", Mock(return_value=response(headers={"Set-Cookie": "ck=deleted; Path=/"})))
    assert not helper.refresh_ck()
    assert helper.cookies["ck"] == "old"


def test_web_uses_base_query_and_numeric_season_alias(runtime, monkeypatch):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    request = Mock(side_effect=[web_search(("30181230", "我们与恶的距离"), ("33437319", "我们与恶的距离2")),
                               web_detail("33437319", "我们与恶的距离2", "2025")])
    monkeypatch.setattr(helper, "_request", request)
    assert helper.get_subject_id("我们与恶的距离 第2季", "2025", runtime.MediaType.TV, season=2) == (
        "我们与恶的距离2", "33437319")
    assert request.call_args_list[0].kwargs["params"] == {"cat": "1002", "q": "我们与恶的距离"}
    assert request.call_args_list[1].args[1] == "https://movie.douban.com/subject/33437319/"


def test_failure_keeps_both_mp_filter_reason_and_web_problem(runtime, monkeypatch):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    runtime.search.return_value = [result(runtime, title="测试剧2", year="2023")]
    monkeypatch.setattr(helper, "_request", Mock(return_value=response(text="<title>未知页面</title>")))
    assert helper.get_subject_id("测试剧 第2季", "2025", runtime.MediaType.TV, season=2) == (None, None)
    assert "年份不符 1 条" in helper.last_error
    assert "网页搜索结构无法识别（页面标题：未知页面）" in helper.last_error
    assert "登录" not in helper.last_error and "验证" not in helper.last_error


# 模拟 API 隐藏但登录态网页可见的条目；不保存真实 Cookie 或完整网页。


def test_restricted_api_subject_uses_authenticated_web_and_verifies_details(runtime, monkeypatch, caplog):
    helper = runtime.helper_module.DoubanHelper("ck=secret; dbcl2=account")
    pages = [web_search(("35225519", "嗜血法医：杀魔新生"), ("7916280", "嗜血法医 第七季")), web_detail()]
    request = Mock(side_effect=pages)
    monkeypatch.setattr(helper, "_request", request)
    with caplog.at_level("DEBUG"):
        assert helper.get_subject_id("嗜血法医 第7季", "2012", runtime.MediaType.TV) == ("嗜血法医 第七季", "7916280")
    assert request.call_count == 2
    assert request.call_args_list[0].args == ("get", "https://www.douban.com/search")
    assert request.call_args_list[0].kwargs["params"] == {"cat": "1002", "q": "嗜血法医"}
    assert request.call_args_list[1].args == ("get", "https://movie.douban.com/subject/7916280/")
    assert "登录态网页匹配成功" in caplog.text
    assert "secret" not in caplog.text and "account" not in caplog.text
    for page in pages:
        page.close.assert_called_once()


@pytest.mark.parametrize("overrides", [{"year": "2021"}, {"kind": "Movie"},
    {"name": "嗜血法医 第一季"}, {"name": "嗜血法医：杀魔新生"},
    {"url": "/subject/35225519/"}, {"datePublished": None}])
def test_web_search_does_not_trust_search_title_without_details(runtime, monkeypatch, overrides):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    monkeypatch.setattr(helper, "_request", Mock(side_effect=[
        web_search(("7916280", "嗜血法医 第七季")), web_detail(**overrides)]))
    assert helper.get_subject_id("嗜血法医 第7季", "2012", runtime.MediaType.TV) == (None, None)


@pytest.mark.parametrize("bad_page", [response(302), response(403), response(text='<title>登录豆瓣</title>'),
    response(text='<title>验证码 (豆瓣)</title><script type="application/ld+json">broken</script>')])
def test_web_unavailable_detail_is_retryable(runtime, monkeypatch, bad_page):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    monkeypatch.setattr(helper, "_request", Mock(side_effect=[
        web_search(("7916280", "嗜血法医 第七季")), bad_page]))
    assert helper.get_subject_id("嗜血法医 第7季", "2012", runtime.MediaType.TV) == (None, None)
    bad_page.close.assert_called_once()


def test_web_duplicate_candidates_are_not_selected_arbitrarily(runtime, monkeypatch):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    monkeypatch.setattr(helper, "_request", Mock(side_effect=[
        web_search(("7916280", "嗜血法医 第七季"), ("9999999", "嗜血法医 第七季")),
        web_detail(), web_detail(subject="9999999")]))
    assert helper.get_subject_id("嗜血法医 第7季", "2012", runtime.MediaType.TV) == (None, None)


def test_web_missing_candidate_details_cannot_remove_ambiguity(runtime, monkeypatch):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    monkeypatch.setattr(helper, "_request", Mock(side_effect=[
        web_search(("7916280", "嗜血法医 第七季"), ("9999999", "嗜血法医 第七季")), web_detail(), None]))
    assert helper.get_subject_id("嗜血法医 第7季", "2012", runtime.MediaType.TV) == (None, None)


@pytest.mark.parametrize("url", ["https://book.douban.com/subject/7916280/", "https://evil.test/subject/7916280/",
    "https://movie.douban.com.evil.test/subject/7916280/", "https://movie.douban.com@evil.test/subject/7916280/",
    "https://www.douban.com/link2/?url=https%3A%2F%2Fevil.test%2Fsubject%2F7916280%2F"])
def test_web_links_cannot_send_cookies_to_other_hosts(runtime, monkeypatch, url):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    request = Mock(return_value=response(text=f'<a href="{url}">嗜血法医 第七季</a>'))
    monkeypatch.setattr(helper, "_request", request)
    assert helper.get_subject_id("嗜血法医 第7季", "2012", runtime.MediaType.TV) == (None, None)
    request.assert_called_once()


def test_frodo_ambiguity_never_uses_web_to_pick_first(runtime, monkeypatch):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    runtime.search.return_value = [result(runtime), result(runtime, subject="888")]
    request = Mock()
    monkeypatch.setattr(helper, "_request", request)
    assert helper.get_subject_id("测试剧", "2024", runtime.MediaType.TV) == (None, None)
    request.assert_not_called()


def test_frodo_error_still_allows_web_fallback(runtime, monkeypatch):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    runtime.search.side_effect = RuntimeError("private response")
    monkeypatch.setattr(helper, "_request", Mock(side_effect=[
        web_search(("7916280", "嗜血法医 第七季")), web_detail()]))
    assert helper.get_subject_id("嗜血法医 第7季", "2012", runtime.MediaType.TV) == ("嗜血法医 第七季", "7916280")


@pytest.mark.parametrize("candidates,expected", [([], "MP 豆瓣搜索未返回候选条目；未配置豆瓣 Cookie，未执行网页搜索"),
    (["1", "2", "2"], "MP 豆瓣搜索匹配到多个候选（2 个），无法唯一确定")])
def test_api_distinguishes_zero_and_multiple_matches(runtime, candidates, expected):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    helper.cookies = {}  # 本次仅检查 API 结果，不启动网页补查。
    runtime.search.return_value = [result(runtime, subject=sid) for sid in candidates]
    assert helper.get_subject_id("测试剧", "2024", runtime.MediaType.TV) == (None, None)
    assert helper.last_error == expected


def test_api_candidates_count_only_verified_distinct_ids(runtime):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    runtime.search.return_value = [result(runtime, subject="1"), result(runtime, subject="1"),
        result(runtime, subject="2", year="1999"), result(runtime, subject="3", type=runtime.MediaType.MOVIE)]
    assert helper.get_subject_id("测试剧", "2024", runtime.MediaType.TV) == ("测试剧", "1")
    assert helper.last_error == ""


def test_api_search_exception_is_not_no_match(runtime):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    helper.cookies = {}
    runtime.search.side_effect = RuntimeError("cookie=private")
    assert helper.get_subject_id("测试剧") == (None, None)
    assert helper.last_error == "MP 豆瓣搜索接口异常（RuntimeError）；未配置豆瓣 Cookie，未执行网页搜索"
    assert "private" not in helper.last_error


@pytest.mark.parametrize("items,details,expected", [
    ([], [], "豆瓣网页搜索未返回候选条目"),
    ([("1", "测试剧")], [web_detail("1", "测试剧", "1999")], "豆瓣网页搜索无合格候选（返回 1 条；年份不符 1 条）"),
    ([("1", "测试剧"), ("2", "测试剧")], [web_detail("1", "测试剧", "2024"), web_detail("2", "测试剧", "2024")],
     "豆瓣网页搜索匹配到多个候选（2 个），无法唯一确定"),
])
def test_web_distinguishes_zero_and_verified_multiple(runtime, monkeypatch, items, details, expected):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    monkeypatch.setattr(helper, "_request", Mock(side_effect=[web_search(*items), *details]))
    assert helper.get_subject_id("测试剧", "2024", runtime.MediaType.TV) == (None, None)
    assert helper.last_error == "MP 豆瓣搜索未返回候选条目；" + expected


def test_web_many_unverified_names_not_reported_as_confirmed_matches(runtime, monkeypatch):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    request = Mock(return_value=web_search(*[(str(i), "测试剧") for i in range(1, 7)]))
    monkeypatch.setattr(helper, "_request", request)
    assert helper.get_subject_id("测试剧", "2024", runtime.MediaType.TV) == (None, None)
    assert helper.last_error == "MP 豆瓣搜索未返回候选条目；豆瓣网页搜索名称候选过多（6 个），尚未逐一核验"
    request.assert_called_once()


@pytest.mark.parametrize("page,expected", [
    (None, "豆瓣候选详情（ID：2）未收到响应（网络错误或超时）"),
    (response(403), "豆瓣候选详情（ID：2）失败，HTTP 403"),
    (response(text="<title>登录豆瓣</title>"), "豆瓣候选详情（ID：2）返回登录页，请检查 Cookie 登录状态"),
    (web_detail("2", "测试剧", "2024", datePublished=None), "豆瓣候选详情信息不完整（ID：2），无法核验类型和年份"),
])
def test_one_matching_candidate_plus_unknown_detail_is_not_no_match_or_ambiguity(runtime, monkeypatch, page, expected):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    monkeypatch.setattr(helper, "_request", Mock(side_effect=[web_search(("1", "测试剧"), ("2", "测试剧")),
                                                            web_detail("1", "测试剧", "2024"), page]))
    assert helper.get_subject_id("测试剧", "2024", runtime.MediaType.TV) == (None, None)
    assert helper.last_error == "MP 豆瓣搜索未返回候选条目；" + expected + "；无法确认网页候选是否唯一"


@pytest.mark.parametrize("body,expected", [
    ("", "豆瓣网页搜索结构无法识别（页面标题：空）"),
    ("<title>登录豆瓣</title>", "豆瓣网页搜索返回登录页，请检查 Cookie 登录状态"),
    ("<title>安全验证</title>", "豆瓣网页搜索返回验证页，需要先完成豆瓣验证"),
])
def test_non_search_page_not_reported_as_zero_matches(runtime, monkeypatch, body, expected):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    monkeypatch.setattr(helper, "_request", Mock(return_value=response(text=body)))
    assert helper.get_subject_id("测试剧") == (None, None)
    assert helper.last_error == "MP 豆瓣搜索未返回候选条目；" + expected


@pytest.mark.parametrize("page_title", ["搜索: 测试剧", "测试剧", ""])
def test_search_result_structure_explains_rejected_candidates_without_title_keywords(runtime, monkeypatch, page_title):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    body = (f'<title>{page_title}</title><div class="search-result">'
            '<div class="result"><a href="https://movie.douban.com/subject/1/">测试剧 第一季</a></div>'
            '<div><a href="https://movie.douban.com/subject/1/">测试剧 第一季</a></div>'
            '<div><a href="https://movie.douban.com/subject/2/">其他作品</a></div></div>')
    request = Mock(return_value=response(text=body))
    monkeypatch.setattr(helper, "_request", request)
    assert helper.get_subject_id("测试剧 第2季", "2025", runtime.MediaType.TV, season=2) == (None, None)
    assert helper.last_error.endswith("豆瓣网页搜索无合格候选（返回 2 条；季数不符 1 条，名称不符 1 条）")
    assert "无法识别" not in helper.last_error
    request.assert_called_once()


def test_empty_search_container_ignores_recommendation_links(runtime, monkeypatch):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    body = ('<title>搜索: 测试剧</title><div class="search-result"><p>没有找到相关结果</p></div>'
            '<aside><a href="https://movie.douban.com/subject/1/">测试剧</a></aside>')
    request = Mock(return_value=response(text=body))
    monkeypatch.setattr(helper, "_request", request)
    assert helper.get_subject_id("测试剧") == (None, None)
    assert helper.last_error.endswith("豆瓣网页搜索未返回候选条目")
    request.assert_called_once()


def test_search_container_keeps_matching_result_and_ignores_matching_sidebar(runtime, monkeypatch):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    body = ('<title>搜索: 测试剧</title><div class="search-result"><div class="result"><div class="title">'
            '<a href="https://movie.douban.com/subject/1/"><span>测试剧</span></a></div></div></div>'
            '<aside><a href="https://movie.douban.com/subject/2/">测试剧</a></aside>')
    request = Mock(side_effect=[response(text=body), web_detail("1", "测试剧", "2024")])
    monkeypatch.setattr(helper, "_request", request)
    assert helper.get_subject_id("测试剧", "2024", runtime.MediaType.TV) == ("测试剧", "1")
    assert request.call_count == 2
    assert request.call_args_list[-1].args[1] == "https://movie.douban.com/subject/1/"


def test_unrecognized_page_with_subject_link_is_not_treated_as_search_results(runtime, monkeypatch):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    body = '<title>其他页面\n 内容</title><a href="https://movie.douban.com/subject/1/">测试剧</a>'
    request = Mock(return_value=response(text=body))
    monkeypatch.setattr(helper, "_request", request)
    assert helper.get_subject_id("测试剧") == (None, None)
    assert helper.last_error.endswith("豆瓣网页搜索结构无法识别（页面标题：其他页面 内容）")
    request.assert_called_once()


@pytest.mark.parametrize("page_title,reason", [("登录豆瓣", "返回登录页"), ("安全验证", "返回验证页")])
def test_login_and_verification_pages_take_precedence_over_search_markup(runtime, monkeypatch, page_title, reason):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    body = f'<title>{page_title}</title><div class="search-result"></div>'
    request = Mock(return_value=response(text=body))
    monkeypatch.setattr(helper, "_request", request)
    assert helper.get_subject_id("测试剧") == (None, None)
    assert reason in helper.last_error and "未返回候选" not in helper.last_error.split("；")[-1]
    request.assert_called_once()


@pytest.mark.parametrize("page,reason", [(None, "未收到响应"), (response(403), "HTTP 403"),
                                       (response(302, headers={"Location": "https://sec.douban.com/"}), "跳转到豆瓣验证页")])
def test_search_transport_failure_is_not_reported_as_empty_results(runtime, monkeypatch, page, reason):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    monkeypatch.setattr(helper, "_request", Mock(return_value=page))
    assert helper.get_subject_id("测试剧") == (None, None)
    assert reason in helper.last_error and "结构无法识别" not in helper.last_error


def test_new_attempt_clears_previous_ambiguity_reason(runtime):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    runtime.search.return_value = [result(runtime, subject="1"), result(runtime, subject="2")]
    assert helper.get_subject_id("测试剧") == (None, None)
    assert "多个候选" in helper.last_error
    runtime.search.return_value = [result(runtime, subject="1")]
    assert helper.get_subject_id("测试剧") == ("测试剧", "1")
    assert helper.last_error == ""


def test_precise_reason_reaches_queue_and_form_once_per_attempt(runtime, emit):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    helper.cookies = {}
    runtime.helper_factory.return_value = helper
    emit()
    item = next(iter(runtime.plugin.data["wait"].values()))
    assert item["attempts"] == 1
    assert item["last_error"] == "MP 豆瓣搜索未返回候选条目；未配置豆瓣 Cookie，未执行网页搜索"
    runtime.search.return_value = [result(runtime, subject="1"), result(runtime, subject="2")]
    emit()
    item = next(iter(runtime.plugin.data["wait"].values()))
    assert item["attempts"] == 2
    assert item["last_error"] == "MP 豆瓣搜索匹配到多个候选（2 个），无法唯一确定"
    form, _ = runtime.plugin.get_form()
    assert item["last_error"] in str(runtime.plugin.api_archive().data["waiting"])
    runtime.schedule.assert_not_called()


def interest_page(marked=False, logged_in=True, sid="999"):
    auth = '<a href="https://www.douban.com/accounts/logout">退出</a>' if logged_in else ''
    body = ('<form method="POST" action="remove"><input name="ck"></form>' if marked else
            f'<a class="collect_btn colbutt ll" name="pbtn-{sid}-collect">看过</a>')
    return response(text=auth + body)


def test_clear_posts_official_form_then_verifies_logged_in_page(runtime, monkeypatch):
    helper = runtime.helper_module.DoubanHelper("ck=secret; dbcl2=account")
    results = [interest_page(marked=True), response(302), interest_page()]
    request = Mock(side_effect=results)
    monkeypatch.setattr(helper, "_request", request)
    assert helper.clear_watching_status("999") is True
    assert request.call_args_list[1].args == ("post", "https://movie.douban.com/subject/999/remove")
    assert request.call_args_list[1].kwargs["data"] == {"ck": "secret"}
    assert helper.last_error == ""
    for res in results:
        res.close.assert_called_once()


@pytest.mark.parametrize("after", ["still_marked", "logged_out", "wrong_subject", "verification", "timeout", "http"])
def test_clear_never_treats_redirect_or_unverified_page_as_success(runtime, monkeypatch, after):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    result = {"still_marked": interest_page(marked=True), "logged_out": interest_page(logged_in=False),
              "wrong_subject": interest_page(sid="888"), "verification": response(text="请完成验证"),
              "timeout": None, "http": response(403)}[after]
    request = Mock(side_effect=[interest_page(marked=True), response(302), result])
    monkeypatch.setattr(helper, "_request", request)
    assert helper.clear_watching_status("999") is False
    assert helper.last_error


def test_clear_is_idempotent_and_unrecognized_precheck_does_not_post(runtime, monkeypatch):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    request = Mock(return_value=interest_page())
    monkeypatch.setattr(helper, "_request", request)
    assert helper.clear_watching_status("999")
    request.assert_called_once_with("get", "https://movie.douban.com/subject/999/")
    request.reset_mock();request.return_value = interest_page(logged_in=False)
    assert not helper.clear_watching_status("999")
    assert request.call_count == 1 and request.call_args.args[0] == "get"


def test_clear_403_refreshes_once_and_rechecks_result(runtime, monkeypatch):
    helper = runtime.helper_module.DoubanHelper("ck=old")
    request = Mock(side_effect=[interest_page(marked=True), response(403),
        response(headers={"Set-Cookie": "ck=new; Path=/"}), response(302), interest_page()])
    monkeypatch.setattr(helper, "_request", request)
    assert helper.clear_watching_status("999")
    posts = [c for c in request.call_args_list if c.args[0] == "post"]
    assert [c.kwargs["data"]["ck"] for c in posts] == ["old", "new"]


@pytest.mark.parametrize("cookie,sid", [("", "999"), ("ck=secret", "../remove"), ("ck=secret", "0")])
def test_clear_rejects_invalid_identity_without_http(runtime, monkeypatch, cookie, sid):
    helper = runtime.helper_module.DoubanHelper("ck=secret")
    if not cookie: helper.cookies = {}
    request = Mock()
    monkeypatch.setattr(helper, "_request", request)
    assert not helper.clear_watching_status(sid)
    request.assert_not_called()
