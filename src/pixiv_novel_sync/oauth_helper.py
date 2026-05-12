from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

PIXIV_AUTH_BASE = "https://app-api.pixiv.net/web/v1/login"
PIXIV_TOKEN_ENDPOINT = "https://oauth.secure.pixiv.net/auth/token"
PIXIV_CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
PIXIV_CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
PIXIV_REDIRECT_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
PIXIV_HASH_SECRET = "28c1fdd17047486203c2ad2b1db66c8c"
TASK_TTL_SECONDS = 900


@dataclass(slots=True)
class OAuthTask:
    task_id: str
    state: str
    code_verifier: str
    created_at: float
    callback_url: str
    login_url: str
    status: str = "pending"
    message: str = "等待浏览器完成 Pixiv 登录"
    refresh_token: str | None = None
    access_token: str | None = None
    user_id: int | None = None
    raw: dict[str, Any] | None = None
    pixiv_callback_url: str | None = None


class OAuthManager:
    def __init__(self) -> None:
        self._tasks: dict[str, OAuthTask] = {}

    def create_task(self, external_base_url: str) -> OAuthTask:
        task_id = secrets.token_urlsafe(16)
        state = secrets.token_urlsafe(24)
        code_verifier = self._generate_code_verifier()
        callback_url = f"{external_base_url.rstrip('/')}/oauth/callback"
        login_url = self._build_login_url(state=state, code_challenge=self._code_challenge(code_verifier), callback_url=callback_url)
        task = OAuthTask(
            task_id=task_id,
            state=state,
            code_verifier=code_verifier,
            created_at=time.time(),
            callback_url=callback_url,
            login_url=login_url,
        )
        self._tasks[task_id] = task
        self._cleanup()
        return task

    def get_task(self, task_id: str) -> OAuthTask | None:
        self._cleanup()
        return self._tasks.get(task_id)

    def find_task_by_state(self, state: str) -> OAuthTask | None:
        self._cleanup()
        for task in self._tasks.values():
            if task.state == state:
                return task
        return None

    def exchange_code(self, task: OAuthTask, code: str) -> OAuthTask:
        headers = self._build_oauth_headers()
        data = {
            "client_id": PIXIV_CLIENT_ID,
            "client_secret": PIXIV_CLIENT_SECRET,
            "code": code,
            "code_verifier": task.code_verifier,
            "grant_type": "authorization_code",
            "include_policy": "true",
            "redirect_uri": task.callback_url,
        }
        response = requests.post(PIXIV_TOKEN_ENDPOINT, data=data, headers=headers, timeout=30)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = response.text.strip()
            raise RuntimeError(f"Pixiv token 接口返回 {response.status_code}: {detail}") from exc
        payload = response.json()
        response_data = payload.get("response", payload)
        task.access_token = response_data.get("access_token")
        task.refresh_token = response_data.get("refresh_token")
        user = response_data.get("user", {}) if isinstance(response_data, dict) else {}
        try:
            task.user_id = int(user.get("id")) if user.get("id") is not None else None
        except (TypeError, ValueError):
            task.user_id = None
        task.raw = payload
        task.status = "done"
        task.message = "Pixiv token 获取成功"
        return task

    def exchange_callback_url(self, task: OAuthTask, callback_url: str) -> OAuthTask:
        parsed = urlparse(callback_url.strip())
        query = parse_qs(parsed.query)
        state = (query.get("state") or [""])[0]
        code = (query.get("code") or [""])[0]
        if not state or not code:
            raise ValueError("callback URL 中缺少 state 或 code")
        if state != task.state:
            raise ValueError("state 不匹配，请重新发起一次登录任务")
        return self.exchange_code(task, code)

    def save_to_env(self, refresh_token: str, user_id: int | None = None) -> None:
        env_path = Path(".env")
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        updates = {"PIXIV_REFRESH_TOKEN": refresh_token}
        if user_id is not None:
            updates["PIXIV_USER_ID"] = str(user_id)
        result: list[str] = []
        seen: set[str] = set()
        for line in lines:
            if "=" in line:
                key, _ = line.split("=", 1)
                if key in updates:
                    result.append(f"{key}={updates[key]}")
                    seen.add(key)
                    continue
            result.append(line)
        for key, value in updates.items():
            if key not in seen:
                result.append(f"{key}={value}")
        env_path.write_text("\n".join(result) + "\n", encoding="utf-8")

    def sync_state_from_callback_url(self, task: OAuthTask, callback_url: str) -> OAuthTask:
        parsed = urlparse(callback_url.strip())
        query = parse_qs(parsed.query)
        state = (query.get("state") or [""])[0]
        code = (query.get("code") or [""])[0]
        if not code:
            raise ValueError("callback URL 中缺少 code")
        if not state:
            raise ValueError("callback URL 中缺少 state")
        task.state = state
        task.pixiv_callback_url = callback_url.strip()
        task.message = "已根据 Pixiv callback URL 同步 state，可继续兑换 token"
        return task

    def _build_login_url(self, state: str, code_challenge: str, callback_url: str) -> str:
        # redirect_uri 必须是 Pixiv 注册的地址，登录后 Pixiv 会回调到这个地址
        # 用户需要从浏览器地址栏复制回调 URL（包含 code 和 state 参数）
        params = {
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "client": "pixiv-android",
            "redirect_uri": PIXIV_REDIRECT_URI,
            "response_type": "code",
            "state": state,
        }
        return f"{PIXIV_AUTH_BASE}?{urlencode(params)}"

    def _cleanup(self) -> None:
        now = time.time()
        expired = [task_id for task_id, task in self._tasks.items() if now - task.created_at > TASK_TTL_SECONDS]
        for task_id in expired:
            self._tasks.pop(task_id, None)

    def _generate_code_verifier(self) -> str:
        return secrets.token_urlsafe(48)

    def _code_challenge(self, code_verifier: str) -> str:
        digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")

    def _build_oauth_headers(self) -> dict[str, str]:
        now = str(int(time.time()))
        digest = hashlib.md5((now + PIXIV_HASH_SECRET).encode("utf-8")).hexdigest()
        return {
            "User-Agent": "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)",
            "App-OS": "android",
            "App-OS-Version": "11",
            "App-Version": "5.0.234",
            "X-Client-Time": now,
            "X-Client-Hash": digest,
            "Accept-Language": "zh-cn",
        }
