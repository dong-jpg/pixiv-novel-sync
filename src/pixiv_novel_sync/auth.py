from __future__ import annotations

from dataclasses import dataclass
import logging
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
        # S5: proxies 和 verify_ssl 必须传给 AppPixivAPI 构造函数，
        # 设置 api.proxies 属性不起作用（pixivpy3 3.7.5 不读）
        if self._settings.proxy:
            kwargs["proxies"] = {
                "http": self._settings.proxy,
                "https": self._settings.proxy,
            }
        # verify_ssl 也要传入，否则配了代理时 SSL 验证行为不可控
        if self._settings.verify_ssl is not None:
            kwargs["verify"] = self._settings.verify_ssl

        api = AppPixivAPI(**kwargs)
        api.set_accept_language("zh-cn")
        return api

    def login(self) -> tuple[AppPixivAPI, AuthResult]:
        if not self._settings.refresh_token and not self._settings.access_token:
            raise PixivAuthError("Missing PIXIV_REFRESH_TOKEN or PIXIV_ACCESS_TOKEN")

        api = self.create_api()

        try:
            if self._settings.refresh_token:
                raw = api.auth(refresh_token=self._settings.refresh_token)
            else:
                api.set_auth(self._settings.access_token, None)
                raw = {}
        except InterruptedError:
            raise
        except Exception as exc:
            raise PixivAuthError(f"Pixiv auth failed: {exc}") from exc

        access_token = getattr(api, "access_token", None) or self._settings.access_token
        refresh_token = getattr(api, "refresh_token", None) or self._settings.refresh_token
        user_payload = (raw.get("user") or {}) if isinstance(raw, dict) else {}
        authenticated_id = _coerce_user_id(user_payload.get("id"))
        user_id = self._settings.user_id or authenticated_id
        if authenticated_id and user_id != authenticated_id:
            raise PixivAuthError("配置的 Pixiv 用户与登录账号不一致")
        renewed = False

        def reauthenticate() -> bool:
            nonlocal renewed
            if renewed or not refresh_token or not user_id:
                return False
            renewed = True
            try:
                candidate = self.create_api()
                refreshed = candidate.auth(refresh_token=refresh_token)
                refreshed_user = (refreshed.get("user") or {}) if isinstance(refreshed, dict) else {}
                refreshed_id = _coerce_user_id(refreshed_user.get("id"))
                if refreshed_id != user_id or not candidate.access_token:
                    return False
                api.set_auth(candidate.access_token, candidate.refresh_token)
            except InterruptedError:
                raise
            except Exception:
                logging.getLogger(__name__).warning("Pixiv 认证续期失败，本轮停止重试")
                return False
            logging.getLogger(__name__).info("Pixiv 认证已续期，重试当前请求")
            return True

        if refresh_token:
            api._pixiv_reauthenticate = reauthenticate

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
