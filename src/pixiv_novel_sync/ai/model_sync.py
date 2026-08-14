"""Provider 模型目录同步的领域对象与异步协调器。"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .providers import AIProvider, AIProviderError, _redact_secrets


class ModelSyncConflictError(RuntimeError):
    """模型同步 operation 与当前 Provider 状态冲突。"""

    def __init__(
        self,
        message: str,
        *,
        existing_operation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.existing_operation_id = existing_operation_id


_PROVIDER_SYNC_HASH_FIELDS = (
    "name",
    "provider_type",
    "base_url",
    "api_key_encrypted",
    "default_model",
    "timeout_seconds",
    "max_retries",
    "proxy",
    "context_window",
    "stream_enabled",
    "enabled",
)


def provider_model_sync_config_hash(provider: Mapping[str, Any]) -> str:
    """计算不含明文密钥的 Provider 同步配置摘要。"""

    payload = {field: provider.get(field) for field in _PROVIDER_SYNC_HASH_FIELDS}
    for field in ("stream_enabled", "enabled"):
        payload[field] = bool(payload[field])
    for field in ("timeout_seconds", "max_retries", "context_window"):
        value = payload[field]
        payload[field] = int(value) if value is not None else None
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class _ModelSyncDeadlineExceeded(RuntimeError):
    pass


class _ModelSyncLostOwnership(RuntimeError):
    pass


class _ModelSyncCancelled(RuntimeError):
    pass


ProviderResolver = Callable[[Any, int], AIProvider]
_MODEL_SYNC_DEADLINE_SECONDS = 10 * 60
_MODEL_SYNC_HEARTBEAT_SECONDS = 15


class ModelSyncCoordinator:
    """运行最多两个 Provider 模型发现 worker。"""

    def __init__(
        self,
        db_path: Path,
        *,
        provider_resolver: ProviderResolver,
    ) -> None:
        self.db_path = Path(db_path)
        self._provider_resolver = provider_resolver
        self._executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="ai-model-sync",
        )
        self._lock = threading.Lock()
        self._futures: dict[str, Future[None]] = {}
        self._closed = False
        db = self._db()
        try:
            db.init_schema()
        finally:
            db.close()

    def _db(self):
        from ..storage_db import Database

        return Database(self.db_path)

    def start(self, provider_id: int) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("模型同步协调器已关闭")
            db = self._db()
            try:
                provider_row = db.get_ai_provider(provider_id, include_secret=True)
                if provider_row is None:
                    raise AIProviderError("Provider 不存在")
                if not bool(provider_row.get("enabled")):
                    raise AIProviderError("Provider 已禁用")
                # 先完成本地解密/旧密文升级，再计算稳定配置 hash。
                self._provider_resolver(db, provider_id)
                provider_row = db.get_ai_provider(provider_id, include_secret=True)
                assert provider_row is not None
                config_hash = provider_model_sync_config_hash(provider_row)
                owner_token = secrets.token_urlsafe(32)
                operation = db.create_model_sync_operation(
                    provider_id,
                    provider_row["name"],
                    config_hash,
                    owner_token,
                )
            finally:
                db.close()

            future = self._executor.submit(
                self._run,
                operation["operation_id"],
                owner_token,
                operation["generation"],
            )
            self._futures[operation["operation_id"]] = future
        future.add_done_callback(
            lambda _future, operation_id=operation["operation_id"]: (
                self._forget_future(operation_id)
            )
        )
        return operation

    def _forget_future(self, operation_id: str) -> None:
        with self._lock:
            self._futures.pop(operation_id, None)

    def get(self, operation_id: str) -> dict[str, Any]:
        db = self._db()
        try:
            operation = db.get_model_sync_operation(operation_id)
        finally:
            db.close()
        if operation is None:
            raise ModelSyncConflictError("模型同步 operation 不存在")
        return operation

    def cancel(self, operation_id: str) -> bool:
        db = self._db()
        try:
            return db.request_model_sync_cancel(operation_id)
        finally:
            db.close()

    def confirm_empty(
        self,
        operation_id: str,
        generation: int,
        result_digest: str,
    ) -> dict[str, int]:
        db = self._db()
        try:
            return db.confirm_model_sync_empty(
                operation_id,
                generation,
                result_digest,
            )
        finally:
            db.close()

    def reconcile(self) -> int:
        db = self._db()
        try:
            return db.reconcile_model_sync_operations()
        finally:
            db.close()

    @staticmethod
    def _sync_error_message(exc: Exception, provider: AIProvider | None) -> str:
        message = str(exc) or exc.__class__.__name__
        api_key = getattr(getattr(provider, "config", None), "api_key", None)
        if api_key:
            message = message.replace(str(api_key), "[REDACTED]")
        return _redact_secrets(message)

    def _heartbeat_loop(
        self,
        operation_id: str,
        owner_token: str,
        generation: int,
        stop: threading.Event,
        lost_ownership: threading.Event,
    ) -> None:
        while not stop.wait(_MODEL_SYNC_HEARTBEAT_SECONDS):
            db = self._db()
            try:
                try:
                    alive = db.heartbeat_model_sync_operation(
                        operation_id,
                        owner_token,
                        generation,
                    )
                except Exception:
                    lost_ownership.set()
                    return
            finally:
                db.close()
            if not alive:
                lost_ownership.set()
                return

    def _run(
        self,
        operation_id: str,
        owner_token: str,
        generation: int,
    ) -> None:
        db = self._db()
        heartbeat_stop = threading.Event()
        lost_ownership = threading.Event()
        heartbeat_thread: threading.Thread | None = None
        provider: AIProvider | None = None
        deadline = time.monotonic() + _MODEL_SYNC_DEADLINE_SECONDS
        try:
            if not db.claim_model_sync_operation(
                operation_id,
                owner_token,
                generation,
            ):
                return
            operation = db.get_model_sync_operation(operation_id)
            if operation is None:
                return
            provider = self._provider_resolver(db, int(operation["provider_id"]))
            heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                args=(
                    operation_id,
                    owner_token,
                    generation,
                    heartbeat_stop,
                    lost_ownership,
                ),
                name=f"ai-model-sync-heartbeat-{operation_id[:8]}",
                daemon=True,
            )
            heartbeat_thread.start()

            def is_cancelled() -> bool:
                if time.monotonic() >= deadline:
                    raise _ModelSyncDeadlineExceeded("模型同步超过 10 分钟")
                current = db.get_model_sync_operation(operation_id)
                if current is None:
                    raise _ModelSyncLostOwnership("模型同步 operation 已丢失")
                if bool(current.get("cancel_requested")):
                    return True
                if lost_ownership.is_set():
                    raise _ModelSyncLostOwnership("模型同步租约已失效")
                return False

            def on_page(pages: int, discovered_count: int) -> None:
                if time.monotonic() >= deadline:
                    raise _ModelSyncDeadlineExceeded("模型同步超过 10 分钟")
                if db.update_model_sync_progress(
                    operation_id,
                    owner_token,
                    generation,
                    pages=pages,
                    discovered_count=discovered_count,
                ):
                    return
                current = db.get_model_sync_operation(operation_id)
                if current and bool(current.get("cancel_requested")):
                    raise _ModelSyncCancelled("模型同步已取消")
                raise _ModelSyncLostOwnership("模型同步租约已失效")

            result = provider.list_models(
                on_page=on_page,
                is_cancelled=is_cancelled,
                deadline=deadline,
            )
            if is_cancelled():
                raise _ModelSyncCancelled("模型同步已取消")
            if not bool(result.complete):
                raise AIProviderError(
                    result.partial_reason or "模型目录分页未完整结束"
                )
            if not db.finish_model_sync_success(
                operation_id,
                owner_token,
                generation,
                result.models,
                result.result_digest,
                empty_authoritative=result.empty_authoritative,
                partial_reason=result.partial_reason,
            ):
                raise _ModelSyncLostOwnership("模型同步终态 CAS 失败")
        except _ModelSyncLostOwnership:
            db.reconcile_model_sync_operations()
            return
        except Exception as exc:
            current = db.get_model_sync_operation(operation_id)
            cancelled = bool(
                isinstance(exc, _ModelSyncCancelled)
                or (current and current.get("cancel_requested"))
            )
            if (
                isinstance(exc, _ModelSyncDeadlineExceeded)
                or time.monotonic() >= deadline
            ):
                error_code = "deadline_exceeded"
            elif cancelled:
                error_code = "cancelled"
            elif isinstance(exc, AIProviderError):
                error_code = "provider_error"
            else:
                error_code = "internal_error"
            db.finish_model_sync_failure(
                operation_id,
                owner_token,
                generation,
                error_code=error_code,
                error_message=(
                    "模型同步已取消"
                    if cancelled
                    else self._sync_error_message(exc, provider)
                ),
                cancelled=cancelled,
            )
        finally:
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=2)
            db.close()

    def events(
        self,
        operation_id: str,
        poll_interval: float = 0.25,
    ) -> Iterator[dict[str, Any]]:
        interval = max(0.01, float(poll_interval))
        last_pages = -1
        started = False
        # 与后台同步任务的总时长上限对齐，防止 SSE 轮询无限阻塞连接。
        deadline = time.monotonic() + _MODEL_SYNC_DEADLINE_SECONDS
        while True:
            operation = self.get(operation_id)
            if not started:
                started = True
                yield {
                    "event": "started",
                    "data": {
                        "operation_id": operation_id,
                        "provider_id": operation["provider_id"],
                        "generation": operation["generation"],
                    },
                }
            if operation["pages"] > last_pages and operation["pages"] > 0:
                last_pages = operation["pages"]
                yield {
                    "event": "page",
                    "data": {
                        "operation_id": operation_id,
                        "pages": operation["pages"],
                        "discovered_count": operation["discovered_count"],
                    },
                }

            status = operation["status"]
            if status == "needs_empty_confirmation":
                yield {
                    "event": "empty_confirmation_required",
                    "data": {
                        "operation_id": operation_id,
                        "generation": operation["generation"],
                        "result_digest": operation["result_digest"],
                    },
                }
                return
            if status == "succeeded":
                yield {
                    "event": "completed",
                    "data": {
                        "operation_id": operation_id,
                        "generation": operation["generation"],
                        "result_digest": operation["result_digest"],
                        "pages": operation["pages"],
                        "discovered_count": operation["discovered_count"],
                    },
                }
                return
            if status == "failed":
                yield {
                    "event": "failed",
                    "data": {
                        "operation_id": operation_id,
                        "error_code": operation["error_code"],
                        "error_message": operation["error_message"],
                    },
                }
                return
            if status == "cancelled":
                yield {
                    "event": "cancelled",
                    "data": {"operation_id": operation_id},
                }
                return
            if time.monotonic() >= deadline:
                yield {
                    "event": "failed",
                    "data": {
                        "operation_id": operation_id,
                        "error_code": "timeout",
                        "error_message": "模型同步事件流超时",
                    },
                }
                return
            time.sleep(interval)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            operation_ids = list(self._futures)
        for operation_id in operation_ids:
            self.cancel(operation_id)
        self._executor.shutdown(wait=True, cancel_futures=False)


__all__ = [
    "ModelSyncConflictError",
    "ModelSyncCoordinator",
    "provider_model_sync_config_hash",
]
