"""读取事件用户的 Emby 状态；续播门槛由 Emby 决定，不使用即时播放进度推断。"""

import re
from dataclasses import dataclass
from urllib.parse import urlencode

from app.sdk.services import MediaServerHelper


EVENT_LABELS = {
    "playback.start": "开始播放", "playback.stop": "停止播放",
    "item.markplayed": "标记播放完成",
}


class StateError(ValueError):
    """可以安全输出到日志的状态读取失败原因。"""


@dataclass
class PlaybackState:
    played: bool
    resumable: bool
    item: dict

    @property
    def label(self):
        return "已播放" if self.played else "可续播" if self.resumable else "未播放且不可续播"


def event_ids(info):
    """TV 的标准化 item_id 是整剧 ID，必须使用原始报文里的具体单集 ID。"""
    payload = info.json_object if isinstance(info.json_object, dict) else {}
    values = []
    for field in ("User", "Item"):
        obj = payload.get(field)
        value = str(obj.get("Id") or "") if isinstance(obj, dict) else ""
        if not re.fullmatch(r"[A-Za-z0-9-]+", value):
            raise StateError(f"Emby 通知缺少有效的{'用户' if field == 'User' else '条目'} ID")
        values.append(value)
    return tuple(values)


def service_for(info):
    helper = MediaServerHelper()
    if info.server_name:
        return helper.get_service(name=info.server_name, type_filter="emby")
    services = helper.get_services(type_filter="emby")
    return next(iter(services.values())) if len(services) == 1 else None


def _read(instance, path):
    response = None
    try:
        response = instance.get_data(url=f"[HOST]emby/{path}&api_key=[APIKEY]")
        if response is None:
            raise StateError("Emby 状态查询未收到响应")
        if response.status_code != 200:
            raise StateError(f"Emby 状态查询返回 HTTP {response.status_code}")
        value = response.json()
        if not isinstance(value, dict):
            raise StateError("Emby 状态响应格式无效")
        return value
    except StateError:
        raise
    except Exception as error:
        # 请求异常可能包含鉴权 URL；只输出异常类型。
        raise StateError(f"Emby 状态查询异常（{type(error).__name__}）") from None
    finally:
        if response is not None:
            response.close()


def read_state(info):
    user_id, item_id = event_ids(info)
    service = service_for(info)
    if not service:
        raise StateError("无法唯一确定事件所属 Emby 服务")
    item = _read(service.instance, f"Users/{user_id}/Items/{item_id}?Fields=Path,ProviderIds&EnableUserData=true")
    expected_type = "Episode" if info.item_type == "TV" else "Movie"
    if str(item.get("Id")) != item_id or item.get("Type") != expected_type:
        raise StateError("Emby 返回的条目身份或类型不符；仅处理电影和具体单集")
    payload_server = (info.json_object.get("Server") or {}).get("Id")
    if payload_server and str(item.get("ServerId")) != str(payload_server):
        raise StateError("Emby 返回的服务器身份与通知不符")
    data = item.get("UserData")
    if not isinstance(data, dict) or not isinstance(data.get("Played"), bool):
        raise StateError("Emby 未返回有效的用户已播放状态")
    if data["Played"]:
        return PlaybackState(True, False, item)
    # Resume 是首页列表接口，部分 Emby 版本会忽略 Ids；用条目查询让服务端精确判断续播状态。
    query = urlencode({"Ids": item_id, "Filters": "IsResumable", "Recursive": "true",
                       "Limit": 1, "EnableImages": "false", "EnableUserData": "true"})
    resume = _read(service.instance, f"Users/{user_id}/Items?{query}")
    entries = resume.get("Items")
    if not isinstance(entries, list) or any(not isinstance(entry, dict) or str(entry.get("Id")) != item_id for entry in entries):
        raise StateError("Emby 续播查询返回的条目无效或身份不符")
    # 第二次查询可能已看到新提交的完成状态；不能用先前的未完成快照覆盖它。
    latest = entries[0].get("UserData") if entries else None
    if entries and (not isinstance(latest, dict) or not isinstance(latest.get("Played"), bool)):
        raise StateError("Emby 续播查询缺少有效的用户已播放状态")
    return PlaybackState(bool(latest and latest["Played"]), bool(entries), item)


def update_event(info, item):
    """优先采用给定条目字段；开始日志传入通知条目，同步传入服务端回读条目。"""
    info.item_path = item.get("Path") or info.item_path
    if info.item_type == "TV":
        info.item_name = item.get("SeriesName") or info.item_name
        info.season_id = item.get("ParentIndexNumber", info.season_id)
        info.episode_id = item.get("IndexNumber", info.episode_id)
    else:
        info.item_name = item.get("Name") or info.item_name
