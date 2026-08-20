from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pixivpy3 import AppPixivAPI

from .settings import PixivSettings


@dataclass(slots=True)
class AuthResult:
    access_token: str | None
    refresh_token: str | None
    user_id: int | None
    raw: dict[str, Any]

    def self_profile(self) -> dict[str, Any] | None:
        """从 auth 响应里提取本人账号资料（含会员状态）。

        Pixiv 的 auth 响应里带 is_premium / x_restrict 等本人专属字段，而 users 表
        只存被关注的作者，所以这里单独抽出来供侧边栏展示。
        """
        user = self.raw.get("user") if isinstance(self.raw, dict) else None
        if not isinstance(user, dict):
            return None
        user_id = _coerce_user_id(user.get("id")) or self.user_id
        if user_id is None:
            return None
        avatars = user.get("profile_image_urls")
        avatar_url = None
        if isinstance(avatars, dict):
            for key in ("px_170x170", "px_50x50", "px_16x16"):
                value = avatars.get(key)
                if isinstance(value, str) and value:
                    avatar_url = value
                    break
        return {
            "user_id": user_id,
            "name": user.get("name"),
            "account": user.get("account"),
            "avatar_url": avatar_url,
            "is_premium": bool(user.get("is_premium")),
            "x_restrict": user.get("x_restrict"),
            "mail_authorized": bool(user.get("is_mail_authorized")),
        }


class PixivAuthError(RuntimeError):
    """Raised when Pixiv authentication fails."""


class PixivAuthManager:
    def __init__(self, settings: PixivSettings) -> None:
        self._settings = settings

    def create_api(self) -> AppPixivAPI:
        kwargs: dict[str, Any] = {
            "timeout": self._settings.timeout,
        }
        api = AppPixivAPI(**kwargs)
        api.set_accept_language("zh-cn")
        return api

    def login(self) -> tuple[AppPixivAPI, AuthResult]:
        if not self._settings.refresh_token and not self._settings.access_token:
            raise PixivAuthError("Missing PIXIV_REFRESH_TOKEN or PIXIV_ACCESS_TOKEN")

        api = self.create_api()
        if self._settings.proxy:
            api.proxies = {"http": self._settings.proxy, "https": self._settings.proxy}

        try:
            if self._settings.refresh_token:
                raw = api.auth(refresh_token=self._settings.refresh_token)
            else:
                api.set_auth(self._settings.access_token, None)
                raw = {}
        except Exception as exc:
            raise PixivAuthError(f"Pixiv auth failed: {exc}") from exc

        access_token = getattr(api, "access_token", None) or self._settings.access_token
        refresh_token = getattr(api, "refresh_token", None) or self._settings.refresh_token
        user_payload = raw.get("user", {}) if isinstance(raw, dict) else {}
        user_id = self._settings.user_id or _coerce_user_id(user_payload.get("id"))

        return api, AuthResult(
            access_token=access_token,
            refresh_token=refresh_token,
            user_id=user_id,
            raw=raw if isinstance(raw, dict) else {},
        )


def _coerce_user_id(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
