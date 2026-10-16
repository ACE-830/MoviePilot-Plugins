"""豆瓣搜索与观影状态写入；基于 honue/DoubanHelper.py，GPL-3.0。"""

import json
import re
import unicodedata
from collections import Counter
from html.parser import HTMLParser
from http.cookies import CookieError, SimpleCookie
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from app.chain.media import MediaChain
from app.schemas.types import MediaSource, MediaType

# 宿主尚未提供 CookieCloud 的 SDK 出口，使用兼容清单指定的现行入口。
from app.adapters.external.cookiecloud import CookieCloudHelper
from app.sdk.config import settings
from .log import log_recognition
from app.sdk.media import MetaInfo
from app.sdk.network import RequestUtils


def media_description(media, fallback_type=None) -> str:
    """只提取日志需要的公开身份字段，不展开媒体对象。"""
    mtype = getattr(media, "type", None) or fallback_type
    source = getattr(media, "media_source", None)
    source = {MediaSource.TMDB: "TMDB", MediaSource.Douban: "豆瓣"}.get(source, getattr(source, "value", source))
    return (f"名称={getattr(media, 'title', None) or '未知'}，类型={getattr(mtype, 'value', mtype) or '未知'}，"
            f"年份={getattr(media, 'year', None) or '未知'}，"
            f"身份={source or '未知'}/{getattr(media, 'media_id', None) or '未知'}")


class _DoubanPage(HTMLParser):
    """只提取条目链接、页面标题和 JSON-LD，不执行网页脚本。"""

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.search_links = []
        self.search_results = False
        self._search_depth = 0
        self.title = []
        self.documents = []
        self._anchor = None
        self._script = None
        self._in_title = False
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "div":
            if self._search_depth:
                self._search_depth += 1
            elif "search-result" in attrs.get("class", "").split():
                self.search_results = True
                self._search_depth = 1
        if tag == "a":
            self._anchor = (attrs.get("href", ""), [])
        elif tag == "title":
            self._in_title = True
        elif tag == "script" and attrs.get("type") == "application/ld+json":
            self._script = []

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor[1].append(data)
        if self._in_title:
            self.title.append(data)
        if self._script is not None:
            self._script.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            link = (self._anchor[0], "".join(self._anchor[1]).strip())
            self.links.append(link)
            if self._search_depth:
                self.search_links.append(link)
            self._anchor = None
        elif tag == "title":
            self._in_title = False
        elif tag == "script" and self._script is not None:
            try:
                document = json.loads("".join(self._script))
                if isinstance(document, dict):
                    self.documents.append(document)
            except ValueError:
                pass
            self._script = None
        if tag == "div" and self._search_depth:
            self._search_depth -= 1


class _InterestPage(HTMLParser):
    """识别豆瓣官方页面的登录状态、删除表单和未收藏按钮。"""

    def __init__(self, html: str, subject_id: str):
        super().__init__(convert_charrefs=True)
        self.subject_id = str(subject_id)
        self.logged_in = False
        self.remove_form = False
        self.unmarked_buttons = False
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            link = urlsplit(attrs.get("href", ""))
            if link.hostname == "www.douban.com" and link.path == "/accounts/logout":
                self.logged_in = True
            if ("collect_btn" in attrs.get("class", "").split() and
                    attrs.get("name") == f"pbtn-{self.subject_id}-collect"):
                self.unmarked_buttons = True
        elif tag == "form" and attrs.get("method", "").upper() == "POST":
            if attrs.get("action") in ("remove", f"/subject/{self.subject_id}/remove",
                                       f"https://movie.douban.com/subject/{self.subject_id}/remove"):
                self.remove_form = True


class DoubanHelper:
    """每批事件复用一次 CookieCloud 结果，不记录 Cookie 或响应正文。"""

    def __init__(self, user_cookie: str = ""):
        self.last_candidates = []
        """从手填配置或宿主 CookieCloud 读取登录态。"""
        self.last_error = ""
        raw_cookie = user_cookie
        if not raw_cookie:
            cookie_map, _ = CookieCloudHelper().download()
            cookie_map = cookie_map or {}
            raw_cookie = cookie_map.get("douban.com") or cookie_map.get(".douban.com") or ""
        self.cookies = {}
        try:
            self.cookies = {key: item.value for key, item in SimpleCookie(raw_cookie).items()}
        except CookieError:
            log_recognition("登录状态", "", "豆瓣 Cookie 格式无效，请重新填写或同步 CookieCloud", debug=True)
        self.cookies.pop("__utmz", None)
        self.headers = {"User-Agent": settings.USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"}
        if self.cookies and not self.cookies.get("ck"):
            self.refresh_ck()

    def _request(self, method: str, url: str, **kwargs):
        """通过宿主 HTTP 门面发送有超时、校验证书且不跨站重定向的请求。"""
        request = RequestUtils(headers=self.headers.copy(), cookies=self.cookies.copy(), timeout=15, verify=True)
        if method == "get":
            return request.get_res(url, allow_redirects=False, **kwargs)
        return request.post_res(url, allow_redirects=False, **kwargs)

    def refresh_ck(self) -> bool:
        """刷新 CSRF Cookie，失败时保留原值。"""
        response = self._request("get", "https://www.douban.com/")
        if response is None:
            return False
        try:
            if response.status_code != 200:
                return False
            cookie = SimpleCookie()
            cookie.load(response.headers.get("Set-Cookie", ""))
            token = cookie.get("ck")
            if token and token.value and token.value.lower() != "deleted":
                self.cookies["ck"] = token.value
                return True
        except CookieError:
            return False
        finally:
            response.close()
        return False

    @staticmethod
    def _normalized_title(title: str) -> str:
        """只统一大小写、标点和空白，不删除作品名称中的数字。"""
        text = unicodedata.normalize("NFKC", title).casefold()
        return re.sub(r"[^\w]", "", text)

    @staticmethod
    def _season_number(value) -> int | None:
        text = unicodedata.normalize("NFKC", str(value)).strip()
        if re.fullmatch(r"[0-9]{1,4}", text):
            return int(text)
        digits = {char: number for number, char in enumerate("零一二三四五六七八九")}
        digits.update({"〇": 0, "两": 2})
        if text in digits:
            return digits[text]
        if re.fullmatch(r"[一二三四五六七八九]?十[一二三四五六七八九]?", text):
            tens, units = text.split("十")
            return digits.get(tens, 1) * 10 + digits.get(units, 0)
        return None

    @classmethod
    def _split_season_title(cls, title: str) -> tuple[str, int | None]:
        """只拆明确的季后缀，保留《1899》等剧名自带数字。"""
        text = unicodedata.normalize("NFKC", title).strip()
        match = re.fullmatch(r"(.+?)\s*第([0-9零〇一二两三四五六七八九十]+)季", text)
        if not match:
            match = re.fullmatch(r"(.+?)\s+(?:season\s*|s)([0-9]+)", text, re.IGNORECASE)
        number = cls._season_number(match[2]) if match else None
        return (match[1].strip(), number) if number is not None else (text, None)

    @classmethod
    def _title_reason(cls, name: str, title: str, mtype, season=None) -> str:
        """在剧集上下文中核验季名别称；电影仍按完整片名匹配。"""
        if mtype == MediaType.MOVIE:
            return "" if cls._normalized_title(name) == cls._normalized_title(title) else "名称不符"
        base, title_season = cls._split_season_title(title)
        wanted = cls._season_number(season)
        if wanted is None:
            wanted = title_season if title_season is not None else 1
        if title_season is not None and title_season != wanted:
            return "目标名称与季数冲突"
        candidate_base, candidate_season = cls._split_season_title(name)
        normalized_base = cls._normalized_title(base)
        normalized_name = cls._normalized_title(candidate_base)
        if candidate_season is not None:
            if normalized_name != normalized_base:
                return "名称不符"
            return "" if candidate_season == wanted else "季数不符"
        if normalized_name == normalized_base:
            return "" if wanted == 1 else "候选名称缺少目标季数"
        # 只允许在完整剧名后追加同一个季号，不能随意删去名称中的数字。
        suffix = re.fullmatch(re.escape(normalized_base) + r"([0-9零〇一二两三四五六七八九十]+)",
                              normalized_name)
        if suffix and (number := cls._season_number(suffix[1])) is not None:
            return "" if number == wanted else "季数不符"
        return "名称不符"

    @classmethod
    def _match_reason(cls, media, title: str, year, mtype, season=None) -> str:
        """返回第一项不满足的身份条件，供接口和网页共用。"""
        if (not media or getattr(media, "media_source", None) != MediaSource.Douban
                or not re.fullmatch(r"[1-9]\d*", str(getattr(media, "media_id", "")))):
            return "条目身份无效"
        actual_type = getattr(media, "type", None)
        if actual_type not in {MediaType.TV, MediaType.MOVIE} or (mtype is not None and actual_type != mtype):
            return "类型不符"
        reason = cls._title_reason(getattr(media, "title", "") or "", title, actual_type, season)
        if reason:
            return reason
        if year and not getattr(media, "year", None):
            return "候选年份缺失"
        if year and str(media.year) != str(year):
            return "年份不符"
        # 数字结尾的宽容匹配须有年份佐证，年份未知时不靠季号猜测。
        if actual_type == MediaType.TV and not year:
            base, _ = cls._split_season_title(title)
            candidate_base, candidate_season = cls._split_season_title(media.title)
            if candidate_season is None and cls._normalized_title(candidate_base) != cls._normalized_title(base):
                return "目标年份缺失，无法核验数字季名"
        return ""

    @staticmethod
    def candidate_info(media, reason="") -> dict | None:
        """只保存人工核对所需的候选快照；候选不是已确认映射。"""
        sid = str(getattr(media, "media_id", "") or "")
        mtype = getattr(media, "type", None)
        if (getattr(media, "media_source", None) != MediaSource.Douban
                or not re.fullmatch(r"[1-9]\d*", sid) or mtype not in {MediaType.MOVIE, MediaType.TV}):
            return None
        title = str(getattr(media, "title", "") or "")
        if not title:
            return None
        year = str(getattr(media, "year", "") or "")
        return {"subject_id": sid, "title": title, "year": year, "type": mtype.value,
                "overview": str(getattr(media, "overview", "") or "")[:1500],
                "poster_path": str(getattr(media, "poster_path", "") or ""), "reason": reason,
                "label": f"{title}（{year or '年份未知'}） · {mtype.value} · {sid}"}

    def _remember_candidate(self, media, reason):
        candidate = self.candidate_info(media, reason)
        if candidate and len(self.last_candidates) < 20:
            if not any(row["subject_id"] == candidate["subject_id"] for row in self.last_candidates):
                self.last_candidates.append(candidate)

    def describe_subject(self, subject_id) -> dict | None:
        """按明确 ID 获取真实条目；人工选择不再经过自动名称/年份匹配。"""
        self.last_error = ""
        sid = str(subject_id or "")
        if not re.fullmatch(r"[1-9]\d*", sid):
            self.last_error = "请输入有效豆瓣 ID"
            return None
        media = MediaChain().recognize_media(media_source=MediaSource.Douban, media_id=sid, cache=True)
        candidate = self.candidate_info(media)
        if candidate and candidate["subject_id"] == sid:
            return candidate
        self.last_error = "无法核验所选豆瓣条目的身份，请稍后重试"
        return None

    @staticmethod
    def _match_error(count: int, stage: str, total=0, rejected=None) -> str:
        """数量仅统计已核验名称、类型和年份，且按豆瓣 ID 去重的候选。"""
        if count > 1:
            return f"{stage}匹配到多个候选（{count} 个），无法唯一确定"
        if not total:
            return f"{stage}未返回候选条目"
        details = "，".join(f"{reason} {number} 条" for reason, number in (rejected or {}).items())
        return f"{stage}无合格候选（返回 {total} 条" + (f"；{details}" if details else "") + "）"

    @staticmethod
    def _log_candidate(stage, media, title, year, mtype, season, reason, *, debug=False):
        expected = (f"目标类型={getattr(mtype, 'value', mtype) or '未指定'}，"
                    f"目标年份={year or '未知'}，目标季数={season if season is not None else '按标题判断'}")
        log_recognition(stage, title,
                        f"{'排除候选：' + reason if reason else '候选通过核验'}；"
                        f"{media_description(media)}；{expected}", debug=debug)

    @staticmethod
    def _log_search_result(stage, title, total, candidates, rejected):
        detail = f"返回 {total} 条，合格候选 {len(candidates)} 个（按豆瓣 ID 去重）"
        if rejected:
            detail += "；排除：" + "，".join(f"{reason} {count} 条" for reason, count in rejected.items())
        log_recognition(stage, title, detail)

    def get_subject_id(self, title: str, year: str | None = None, mtype=None,
                       preferred_id: str | None = None, season=None) -> tuple[str | None, str | None]:
        """使用剧名召回候选，再核验真实季数；不读取旧档案的映射。"""
        self.last_error = ""
        self.last_candidates = []
        reasons = []
        query, title_season = self._split_season_title(title) if mtype != MediaType.MOVIE else (title, None)
        requested_season = self._season_number(season)
        if requested_season is not None and title_season is not None and requested_season != title_season:
            self.last_error = "目标名称与季数冲突，未执行豆瓣搜索"
            log_recognition("匹配目标", title, f"{self.last_error}；标题季数={title_season}，目标季数={requested_season}")
            return None, None
        log_recognition("匹配目标", title,
                        f"类型={getattr(mtype, 'value', mtype) or '未指定'}，年份={year or '未知'}，"
                        f"季数={requested_season if requested_season is not None else title_season or '未指定'}")
        chain = MediaChain()
        if preferred_id and re.fullmatch(r"[1-9]\d*", str(preferred_id)):
            log_recognition("豆瓣 ID 核验", title, f"核验待选 ID={preferred_id}，按名称、类型、季数和年份判断")
            try:
                preferred = chain.recognize_media(media_source=MediaSource.Douban,
                                                   media_id=str(preferred_id), mtype=mtype, cache=True)
                reason = self._match_reason(preferred, title, year, mtype, season)
                self._remember_candidate(preferred, reason)
                if not reason and str(preferred.media_id) != str(preferred_id):
                    reason = "返回 ID 不一致"
                self._log_candidate("豆瓣 ID 核验", preferred, title, year, mtype, season, reason)
                if not reason:
                    log_recognition("确定条目", title,
                                    f"采用已核验 ID：{preferred.title}（豆瓣 ID={preferred.media_id}）；无需 MP 或网页搜索")
                    return preferred.title, str(preferred.media_id)
                reasons.append(f"媒体库豆瓣 ID 未通过核验（{reason}）")
                log_recognition("豆瓣 ID 核验", title, "待选 ID 未采用，转入 MP 豆瓣搜索")
            except Exception as error:
                reasons.append(f"媒体库豆瓣 ID 核验异常（{type(error).__name__}）")
                log_recognition("豆瓣 ID 核验", title, f"核验异常（{type(error).__name__}），转入 MP 豆瓣搜索")
        else:
            log_recognition("豆瓣 ID 核验", title, "没有有效待选 ID，直接进入 MP 豆瓣搜索")
        # MP 的豆瓣投影会按 meta.begin_season 给所有搜索结果追加季号。
        # 用不带季后缀的剧名搜索并清除季参数，保留候选真实标题用于核验。
        meta = MetaInfo(query)
        meta.name = query
        meta.type = mtype
        meta.year = str(year) if year else None
        meta.begin_season = None
        meta.end_season = None
        meta.media_source = None
        meta.media_id = None
        candidates = {}
        rejected = Counter()
        total = 0
        log_recognition("MP 豆瓣搜索", title,
                        f"开始搜索：关键词={query}，类型={getattr(mtype, 'value', mtype) or '未指定'}，"
                        f"年份={year or '未指定'}；不传季数，由插件核验候选的真实标题")
        try:
            for media in chain.search_medias(meta=meta, media_source=MediaSource.Douban) or []:
                total += 1
                reason = self._match_reason(media, title, year, mtype, season)
                self._remember_candidate(media, reason)
                self._log_candidate("MP 豆瓣搜索", media, title, year, mtype, season, reason,
                                    debug=not reason or rejected[reason] > 0)
                if reason:
                    rejected[reason] += 1
                else:
                    candidates[str(media.media_id)] = media.title
            self._log_search_result("MP 豆瓣搜索", title, total, candidates, rejected)
            api_error = self._match_error(len(candidates), "MP 豆瓣搜索", total, rejected)
        except Exception as error:
            candidates.clear()
            api_error = f"MP 豆瓣搜索接口异常（{type(error).__name__}）"
            log_recognition("MP 豆瓣搜索", title, f"{api_error}；本轮接口候选不采用")
        if len(candidates) == 1:
            subject_id, name = next(iter(candidates.items()))
            log_recognition("确定条目", title,
                            f"采用 MP 豆瓣搜索唯一匹配：{name}（豆瓣 ID={subject_id}）；无需网页搜索")
            return name, subject_id
        reasons.append(api_error)
        if not candidates and self.cookies:
            log_recognition("搜索回退", title, f"{api_error}；转入豆瓣网页搜索")
            matched = self._search_web(query, title, year, mtype, season)
            if matched != (None, None):
                return matched
            reasons.append(self.last_error or "豆瓣网页搜索无法确认条目")
        elif not candidates:
            reasons.append("未配置豆瓣 Cookie，未执行网页搜索")
            log_recognition("豆瓣网页搜索", title, "未执行：未配置豆瓣 Cookie")
        else:
            log_recognition("豆瓣网页搜索", title,
                            f"未执行：MP 已有 {len(candidates)} 个合格候选，不能通过网页回退消除歧义，需人工确认")
        self.last_error = "；".join(reasons)
        return None, None

    @staticmethod
    def _subject_id_from_url(url: str) -> str | None:
        """只接受豆瓣影视条目及官方搜索跳转，不跟随网页给出的任意地址。"""
        if url.startswith("/subject/"):
            url = "https://movie.douban.com" + url
        parsed = urlsplit(url)
        if (parsed.scheme == "https" and parsed.netloc == "www.douban.com"
                and parsed.path == "/link2/"):
            targets = parse_qs(parsed.query).get("url", [])
            if len(targets) != 1:
                return None
            parsed = urlsplit(targets[0])
        if parsed.scheme != "https" or parsed.netloc != "movie.douban.com":
            return None
        match = re.fullmatch(r"/subject/([1-9]\d*)/?", parsed.path)
        return match[1] if match else None

    @staticmethod
    def _page_problem(page) -> str:
        """只依据明确的页面标题识别登录/验证，不把普通空结果归为 Cookie 失效。"""
        title = "".join(page.title).strip()
        if "豆瓣" in title and "搜索" in title:
            return ""
        if "登录豆瓣" in title or "豆瓣登录" in title:
            return "返回登录页，请检查 Cookie 登录状态"
        if any(text in title for text in ("安全验证", "访问验证", "验证码", "异常请求")):
            return "返回验证页，需要先完成豆瓣验证"
        return ""

    def _read_web_page(self, url: str, *, context="豆瓣网页查询", **kwargs):
        response = self._request("get", url, **kwargs)
        if response is None:
            self.last_error = f"{context}未收到响应（网络错误或超时）"
            return None
        try:
            if response.status_code != 200:
                target = urlsplit(response.headers.get("Location", ""))
                if response.status_code in {301, 302, 303, 307, 308} and target.hostname == "sec.douban.com":
                    self.last_error = f"{context}跳转到豆瓣验证页"
                elif (response.status_code in {301, 302, 303, 307, 308}
                        and target.hostname in {"accounts.douban.com", "www.douban.com"}
                        and "login" in target.path):
                    self.last_error = f"{context}跳转到登录页，请检查 Cookie 登录状态"
                else:
                    self.last_error = f"{context}失败，HTTP {response.status_code}"
                log_recognition("请求结果", "", self.last_error, debug=True)
                return None
            page = _DoubanPage(response.text)
            problem = self._page_problem(page)
            if problem:
                self.last_error = context + problem
                return None
            return page
        finally:
            response.close()

    def _web_subject(self, subject_id: str):
        """从详情页核验真实 ID、媒体类型和首播年份，拒绝登录/验证码页面。"""
        context = f"豆瓣候选详情（ID：{subject_id}）"
        page = self._read_web_page(f"https://movie.douban.com/subject/{subject_id}/", context=context)
        if page is None:
            return None
        page_title = "".join(page.title).strip()
        if not page_title.endswith("(豆瓣)"):
            self.last_error = f"{context}页面无法识别，未确认条目身份"
            return None
        name = page_title[:-4].strip()
        media_types = {"TVSeries": MediaType.TV, "Movie": MediaType.MOVIE}
        for document in page.documents:
            if (self._subject_id_from_url(str(document.get("url") or "")) != subject_id
                    or document.get("@type") not in media_types):
                continue
            date = re.match(r"^(\d{4})(?:-\d{2}-\d{2})?$", str(document.get("datePublished") or ""))
            if name and date:
                return SimpleNamespace(title=name, year=date[1], type=media_types[document["@type"]],
                                       media_id=subject_id, media_source=MediaSource.Douban,
                                       poster_path=document.get("image") if isinstance(document.get("image"), str) else "")
        self.last_error = f"豆瓣候选详情信息不完整（ID：{subject_id}），无法核验类型和年份"
        return None

    def _search_web(self, query: str, title: str, year, mtype, season=None) -> tuple[str | None, str | None]:
        """登录态搜索后逐项核验，仍要求唯一匹配；不把搜索首项直接当成答案。"""
        try:
            stage = "豆瓣网页搜索"
            log_recognition(stage, title, f"开始搜索：关键词={query}，范围=影视；使用已配置的登录态，逐项核验详情")
            page = self._read_web_page("https://www.douban.com/search", context=stage,
                                       params={"cat": "1002", "q": query})
            if page is None:
                log_recognition(stage, title, self.last_error)
                return None, None
            page_title = " ".join("".join(page.title).split())
            # 搜索结果容器优先，标题只作兼容依据；推荐区链接不能证明搜索成功。
            if not page.search_results and not ("豆瓣" in page_title and "搜索" in page_title):
                self.last_error = f"豆瓣网页搜索结构无法识别（页面标题：{page_title[:80] or '空'}）"
                log_recognition(stage, title, self.last_error)
                return None, None
            subject_ids = set()
            linked_ids = set()
            rejected = Counter()
            title_rejections = set()
            rejected_titles = {}
            for url, name in page.search_links if page.search_results else page.links:
                subject_id = self._subject_id_from_url(url)
                if subject_id and name:
                    linked_ids.add(subject_id)
                    title_reason = self._title_reason(name, title, mtype, season)
                    log_recognition(stage, title,
                                    f"名称初筛{'排除：' + title_reason if title_reason else '通过'}；"
                                    f"候选={name}，豆瓣 ID={subject_id}",
                                    debug=not title_reason or title_reason in title_rejections)
                    if title_reason:
                        title_rejections.add(title_reason)
                        if subject_id not in subject_ids:
                            rejected_titles[subject_id] = title_reason
                    else:
                        subject_ids.add(subject_id)
                        rejected_titles.pop(subject_id, None)
            log_recognition(stage, title,
                            f"搜索页提取 {len(linked_ids)} 个条目，名称初筛通过 {len(subject_ids)} 个（均按豆瓣 ID 去重）")
            # 控制请求数量；候选过多或任一候选无法核验时不猜测。
            if not subject_ids:
                self.last_error = self._match_error(0, stage, len(linked_ids), Counter(rejected_titles.values()))
                log_recognition(stage, title, self.last_error)
                return None, None
            if len(subject_ids) > 5:
                self.last_error = f"豆瓣网页搜索名称候选过多（{len(subject_ids)} 个），尚未逐一核验"
                log_recognition(stage, title, self.last_error)
                return None, None
            candidates = {}
            for subject_id in sorted(subject_ids):
                media = self._web_subject(subject_id)
                if media is None:
                    self.last_error = f"{self.last_error}；无法确认网页候选是否唯一"
                    log_recognition(stage, title, f"停止核验（豆瓣 ID={subject_id}）：{self.last_error}")
                    return None, None
                reason = self._match_reason(media, title, year, mtype, season)
                self._remember_candidate(media, reason)
                self._log_candidate(stage, media, title, year, mtype, season, reason,
                                    debug=not reason or rejected[reason] > 0)
                if reason:
                    rejected[reason] += 1
                else:
                    candidates[subject_id] = media.title
            self._log_search_result(stage, title, len(subject_ids), candidates, rejected)
            if len(candidates) == 1:
                subject_id, name = next(iter(candidates.items()))
                self.last_error = ""
                log_recognition("确定条目", title,
                                f"登录态网页匹配成功，采用唯一匹配：{name}（豆瓣 ID={subject_id}）")
                return name, subject_id
            self.last_error = self._match_error(len(candidates), stage, len(subject_ids), rejected)
        except Exception as error:
            self.last_error = f"豆瓣网页搜索异常（{type(error).__name__}）"
        log_recognition("豆瓣网页搜索", title, self.last_error)
        return None, None

    def set_watching_status(self, subject_id: str, status: str = "do", private: bool = True) -> bool:
        """写入 do（在看）或 collect（看过）；403 最多刷新并重试一次。"""
        self.last_error = ""
        if not re.fullmatch(r"[1-9]\d*", str(subject_id)) or status not in {"do", "collect"}:
            self.last_error = "豆瓣条目 ID 或目标状态无效"
            return False
        if not self.cookies or not self.cookies.get("ck"):
            self.last_error = "豆瓣登录态不完整，请检查 Cookie 或 CookieCloud"
            log_recognition("请求结果", "", self.last_error, debug=True)
            return False
        url = f"https://movie.douban.com/j/subject/{subject_id}/interest"
        self.headers.update({"Referer": f"https://movie.douban.com/subject/{subject_id}/",
                             "Origin": "https://movie.douban.com"})
        for attempt in range(2):
            data = {"ck": self.cookies["ck"], "interest": status, "rating": "", "foldcollect": "U",
                    "tags": "", "comment": ""}
            if private:
                data["private"] = "on"
            response = self._request("post", url, data=data)
            if response is None:
                self.last_error = "豆瓣状态写入未收到响应（网络错误或超时）"
                return False
            try:
                code = response.status_code
                if code == 200:
                    try:
                        payload = response.json()
                    except ValueError:
                        self.last_error = "豆瓣写入响应不是有效 JSON，可能需要重新登录或验证"
                        return False
                    result = payload.get("r") if isinstance(payload, dict) else None
                    # bool 是 int 的子类，False == 0 不能作为成功判据。
                    if type(result) is int and result == 0:
                        return True
                    self.last_error = (f"豆瓣未确认写入成功（结果码 {result}）" if type(result) is int
                                       else "豆瓣写入响应缺少有效结果码")
                    return False
            finally:
                response.close()
            if code == 403 and attempt == 0 and self.refresh_ck():
                continue
            self.last_error = f"豆瓣状态写入失败，HTTP {code}"
            log_recognition("请求结果", "", self.last_error, debug=True)
            return False
        return False

    def _is_interest_cleared(self, subject_id: str) -> bool | None:
        """只认可登录页头和该条目的未收藏按钮；验证页/匿名页绝不能算成功。"""
        response = self._request("get", f"https://movie.douban.com/subject/{subject_id}/")
        if response is None:
            self.last_error = "豆瓣取消后回查未收到响应（网络错误或超时）"
            return None
        try:
            if response.status_code != 200:
                self.last_error = f"豆瓣取消后回查失败，HTTP {response.status_code}"
                return None
            page = _InterestPage(response.text, subject_id)
            if page.logged_in and page.unmarked_buttons and not page.remove_form:
                return True
            if page.logged_in and page.remove_form:
                self.last_error = "豆瓣仍显示收藏标记，未移除本地档案"
                return False
            self.last_error = "豆瓣取消后状态无法确认，可能需要重新登录或验证"
            return None
        finally:
            response.close()

    def clear_watching_status(self, subject_id: str) -> bool:
        """使用豆瓣官方删除收藏表单，回查确认后才能删除本地档案。"""
        self.last_error = ""
        if not re.fullmatch(r"[1-9]\d*", str(subject_id)):
            self.last_error = "豆瓣条目 ID 无效"
            return False
        if not self.cookies or not self.cookies.get("ck"):
            self.last_error = "豆瓣登录态不完整，请检查 Cookie 或 CookieCloud"
            return False
        url = f"https://movie.douban.com/subject/{subject_id}/"
        self.headers.update({"Referer": url, "Origin": "https://movie.douban.com"})
        # 网络超时后重试可能遇到已取消的收藏；先确认，避免重复提交删除表单。
        current = self._is_interest_cleared(subject_id)
        if current is None:
            return False
        if current:
            self.last_error = ""
            return True
        for attempt in range(2):
            response = self._request("post", url + "remove", data={"ck": self.cookies["ck"]})
            if response is None:
                self.last_error = "豆瓣取消标记未收到响应（网络错误或超时）"
                return False
            try:
                code = response.status_code
                # 不跟随重定向，不把 200/302 本身当作取消成功。
                if code in (200, 302, 303):
                    cleared = self._is_interest_cleared(subject_id)
                    if cleared:
                        self.last_error = ""
                    return cleared is True
            finally:
                response.close()
            if code == 403 and attempt == 0 and self.refresh_ck():
                continue
            self.last_error = f"豆瓣取消标记失败，HTTP {code}"
            return False
        return False
