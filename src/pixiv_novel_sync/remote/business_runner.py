"""Fixed worker-side fetcher; never logs credentials, metadata, or body text."""

from __future__ import annotations

from .contracts import RemoteError, require_fields


def fetch_remote_novel(manifest, credentials, permit, *, api_factory=None,exit_proxy=None):
    from pixivpy3 import AppPixivAPI
    from ..pixiv_fetch import fetch_novel, serialize_novel, PixivFetchError

    if manifest.get("method") == "watchlist":
        import requests
        from .pixiv_calls import normalize_call
        from .contracts import canonical_bytes

        require_fields(credentials, {"web_cookie"})
        page = normalize_call("watchlist", params=manifest["params"])["page"]
        permit(1)
        with requests.Session() as session:
            session.trust_env = False
            with session.get(
                "https://www.pixiv.net/ajax/watch_list/novel",
                params={"p": page, "lang": "zh"},
                headers={
                    "Cookie": credentials["web_cookie"],
                    "Referer": "https://www.pixiv.net/following/watchlist/novels",
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json",
                },
                timeout=30,
                allow_redirects=False,
                stream=True,
                proxies={'https':exit_proxy} if exit_proxy else None,
            ) as response:
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > 8 * 1024 * 1024:
                        raise RemoteError("result_too_large", 413)
                try:
                    import json

                    data = json.loads(body)
                except (ValueError, UnicodeError):
                    data = {}
                return canonical_bytes({"status_code": response.status_code, "data": data})
    require_fields(credentials, {"access_token"})
    token = credentials["access_token"]
    if not isinstance(token, str) or not 16 <= len(token) <= 4096 or any(c.isspace() for c in token):
        raise RemoteError("input_mismatch", 400)
    if not callable(permit):
        raise RemoteError("input_mismatch", 400)
    api = (api_factory or AppPixivAPI)(timeout=30)
    try:
        api.set_auth(token)
        if manifest["kind"] == "pixiv.api.v1":
            from .pixiv_calls import normalize_call, plain_result
            from .contracts import canonical_bytes

            params = normalize_call(manifest["method"], params=manifest["params"])
            permit(1)
            return canonical_bytes(plain_result(getattr(api, manifest["method"])(**params)))

        class ScopedAPI:
            def novel_detail(self, novel_id):
                if novel_id != manifest["novel_id"]:
                    raise RemoteError("input_mismatch", 400)
                permit(1)
                return api.novel_detail(novel_id)

            def webview_novel(self, novel_id):
                if novel_id != manifest["novel_id"]:
                    raise RemoteError("input_mismatch", 400)
                permit(2)
                return api.webview_novel(novel_id)

        return serialize_novel(fetch_novel(ScopedAPI(), manifest["novel_id"]))
    except PixivFetchError as exc:
        code = "pixiv_unavailable" if exc.code == "content_unavailable" else "pixiv_fetch_failed"
        raise RemoteError(code, 409) from None
    finally:
        session = getattr(api, "requests", None)
        if session is not None:
            session.close()
