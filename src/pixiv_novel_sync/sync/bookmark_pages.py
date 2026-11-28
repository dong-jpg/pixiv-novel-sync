from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from .utils import positive_pixiv_id


def read_bookmark_page(response, current):
    def field(value, name):
        return value.get(name) if isinstance(value, dict) else getattr(value, name, None)

    novels = field(response, "novels")
    if not isinstance(novels, (list, tuple)):
        raise ValueError("收藏响应缺少作品列表")
    identifiers = [positive_pixiv_id(field(novel, "id")) for novel in novels]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("收藏页包含重复作品")
    novels = [SimpleNamespace(**novel) if isinstance(novel, dict) else novel for novel in novels]
    if (isinstance(response, dict) and "next_url" not in response
            or not isinstance(response, dict) and not hasattr(response, "next_url")):
        raise ValueError("收藏响应缺少分页状态")
    next_url = field(response, "next_url")
    if next_url is None:
        return novels, None
    if not novels or not isinstance(next_url, str):
        raise ValueError("空收藏页不能带下一页")
    parsed = urlsplit(next_url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme != "https" or parsed.netloc != "app-api.pixiv.net"
            or parsed.path != "/v1/user/bookmarks/novel" or parsed.fragment
            or set(params) - {"user_id", "restrict", "filter", "offset", "max_bookmark_id"}
            or any(len(values) != 1 for values in params.values())):
        raise ValueError("无效的收藏下一页地址")
    params = {key: values[0] for key, values in params.items()}
    if (positive_pixiv_id(params.get("user_id", current["user_id"])) != current["user_id"]
            or params.get("restrict", current["restrict"]) != current["restrict"]
            or params.get("filter", "for_ios") != "for_ios"):
        raise ValueError("收藏游标改变了账号或范围")
    cursors = set(params) & {"offset", "max_bookmark_id"}
    if len(cursors) != 1:
        raise ValueError("收藏游标缺失或含糊")
    cursor = cursors.pop()
    previous_kind = set(current) & {"offset", "max_bookmark_id"}
    if previous_kind and previous_kind != {cursor}:
        raise ValueError("收藏游标类型发生变化")
    value = positive_pixiv_id(params[cursor])
    previous = current.get(cursor)
    if ((cursor == "offset" and value <= (previous or 0))
            or (cursor == "max_bookmark_id" and previous is not None and value >= previous)):
        raise ValueError("收藏游标没有前进")
    return novels, {"user_id": current["user_id"], "restrict": current["restrict"], cursor: value}
