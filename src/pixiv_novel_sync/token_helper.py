from __future__ import annotations

import json
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .settings import Settings


@dataclass(slots=True)
class TokenJob:
    job_id: str
    status: str = "idle"
    message: str = ""
    refresh_token: str | None = None
    user_id: int | None = None
    output: list[str] = field(default_factory=list)


class TokenUiManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jobs: dict[str, TokenJob] = {}
        self._lock = threading.Lock()

    def create_job(self) -> TokenJob:
        job = TokenJob(job_id=uuid.uuid4().hex, status="starting", message="准备启动 token 获取流程")
        with self._lock:
            self._jobs[job.job_id] = job
        thread = threading.Thread(target=self._run_job, args=(job.job_id,), daemon=True)
        thread.start()
        return job

    def get_job(self, job_id: str) -> TokenJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def save_token_to_env(self, refresh_token: str, user_id: int | None = None) -> None:
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

    def _run_job(self, job_id: str) -> None:
        job = self._jobs[job_id]
        job.status = "running"
        job.message = "请在服务器终端中根据提示完成 Pixiv 登录"

        try:
            process = subprocess.Popen(
                ["gppt", "get"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as exc:
            job.status = "failed"
            job.message = f"启动 gppt 失败: {exc}"
            return

        collected: list[str] = []
        refresh_token: str | None = None
        user_id: int | None = None

        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip("\n")
            collected.append(line)
            if len(collected) > 200:
                collected = collected[-200:]
            token_candidate = _extract_after_prefix(line, "refresh_token")
            if token_candidate:
                refresh_token = token_candidate
            user_candidate = _extract_numeric_after_prefix(line, "user_id")
            if user_candidate is not None:
                user_id = user_candidate
            job.output = collected[:]

        return_code = process.wait()
        job.output = collected[:]

        if return_code != 0:
            job.status = "failed"
            job.message = f"gppt 执行失败，退出码 {return_code}"
            return

        if not refresh_token:
            payload = _scan_token_from_output(collected)
            refresh_token = payload.get("refresh_token")
            if user_id is None and payload.get("user_id"):
                user_id = int(payload["user_id"])

        if not refresh_token:
            job.status = "failed"
            job.message = "未能从 gppt 输出中解析出 refresh_token，请查看终端输出"
            return

        job.refresh_token = refresh_token
        job.user_id = user_id
        job.status = "done"
        job.message = "token 获取成功，可复制或写入 .env"


def _extract_after_prefix(line: str, key: str) -> str | None:
    normalized = line.strip()
    lower = normalized.lower()
    if not lower.startswith(f"{key}:") and not lower.startswith(f"{key}="):
        return None
    _, value = normalized.split(normalized[len(key)], 1)
    cleaned = value.strip()
    return cleaned or None


def _extract_numeric_after_prefix(line: str, key: str) -> int | None:
    value = _extract_after_prefix(line, key)
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return int(digits) if digits else None


def _scan_token_from_output(lines: list[str]) -> dict[str, str]:
    joined = "\n".join(lines)
    try:
        payload = json.loads(joined)
        if isinstance(payload, dict):
            return {str(k): str(v) for k, v in payload.items() if v is not None}
    except Exception:
        pass
    result: dict[str, str] = {}
    for line in lines:
        if "refresh_token" in line.lower():
            value = _extract_after_prefix(line, "refresh_token")
            if value:
                result["refresh_token"] = value
        if "user_id" in line.lower():
            value = _extract_after_prefix(line, "user_id")
            if value:
                result["user_id"] = value
    return result
