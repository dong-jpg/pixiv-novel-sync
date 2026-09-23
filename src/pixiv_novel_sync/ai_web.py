from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable, Iterator
from typing import Any

from flask import (
    Flask,
    Response,
    jsonify,
    request,
    stream_with_context,
)

from .ai.service import (
    AINotFoundError,
    AIConflictError,
    AIServiceError,
    AIWritingService,
)
from .settings import Settings

logger = logging.getLogger(__name__)


def register_ai_routes(app: Flask, settings: Settings | Callable[[], Settings]) -> None:
    def current_settings() -> Settings:
        return settings() if callable(settings) else settings

    class CurrentAIWritingService:
        def __init__(self) -> None:
            self._services: dict[str, AIWritingService] = {}
            self._lock = threading.Lock()

        def _current(self) -> AIWritingService:
            db_path = current_settings().storage.db_path
            key = str(db_path)
            with self._lock:
                service = self._services.get(key)
                if service is None:
                    service = AIWritingService(db_path)
                    self._services[key] = service
                return service

        def close(self) -> None:
            with self._lock:
                services = list(self._services.values())
                self._services.clear()
            for service in services:
                service.close()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._current(), name)

    service = CurrentAIWritingService()
    app.extensions["pixiv_novel_sync.ai_service"] = service

    # 启动对账：把上次运行残留、客户端断连后卡在 'running' 的 AI job 标记为 failed，
    # 否则前端会永久转圈，cleanup_ai_jobs 也不会回收这些幽灵任务。
    try:
        _startup_db = service._db()
        try:
            stale = _startup_db.fail_stale_ai_jobs()
            if stale:
                logger.info("启动对账：已修复 %d 个卡住的 AI job", stale)
        finally:
            _startup_db.close()
    except Exception:
        logger.warning("启动 AI job 对账失败", exc_info=True)

    try:
        reconciled_syncs = service.reconcile_model_sync_operations()
        if reconciled_syncs:
            logger.info(
                "启动对账：已修复 %d 个模型同步 operation",
                reconciled_syncs,
            )
    except Exception:
        logger.warning("启动模型同步 operation 对账失败", exc_info=True)

    def json_payload() -> dict[str, Any]:
        payload = request.get_json(silent=True)
        return payload if isinstance(payload, dict) else {}

    def require_json_object() -> dict[str, Any]:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise AIServiceError("请求体必须是 JSON 对象")
        return payload

    def ok(data: Any = None, **extra: Any):
        body = {"ok": True, **extra}
        if data is not None:
            body["data"] = data
        return jsonify(body)

    def fail(exc: Exception, status: int | None = None):
        if status is None:
            if isinstance(exc, AINotFoundError):
                status = 404
            elif isinstance(exc, AIConflictError):
                status = 409
            else:
                status = 400
        body: dict[str, Any] = {"ok": False, "error": str(exc)}
        if isinstance(exc, AIConflictError) and exc.data is not None:
            body["data"] = exc.data
        return jsonify(body), status

    def parse_int(value: Any, default: int, name: str = "参数",
                  min_value: int | None = None, max_value: int | None = None) -> int:
        """安全解析整数参数，给出友好错误信息。"""
        if value is None or value == "":
            return default
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise AIServiceError(f"{name} 必须是整数") from None
        if min_value is not None and number < min_value:
            raise AIServiceError(f"{name} 不能小于 {min_value}")
        if max_value is not None and number > max_value:
            raise AIServiceError(f"{name} 不能大于 {max_value}")
        return number

    def sse(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def query_bool(name: str, default: bool = False) -> bool:
        raw = request.args.get(name)
        if raw is None or raw == "":
            return default
        value = raw.strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
        raise AIServiceError(f"{name} 必须是布尔值")

    def model_sync_event_response(events: Iterator[dict[str, Any]]) -> Response:
        allowed_events = {
            "started",
            "page",
            "empty_confirmation_required",
            "completed",
            "failed",
            "cancelled",
        }

        def generate():
            for item in events:
                event = item.get("event")
                data = item.get("data")
                if event not in allowed_events or not isinstance(data, dict):
                    logger.warning("忽略无效模型同步 SSE 事件：%r", event)
                    continue
                yield sse(str(event), data)

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def stream_response(chunks: Iterator) -> Response:
        def generate():
            try:
                for chunk in chunks:
                    if chunk.type == "delta":
                        yield sse("delta", {"text": chunk.text})
                    elif chunk.type == "progress":
                        yield sse("progress", chunk.data or {})
                    elif chunk.type == "metadata":
                        yield sse("metadata", chunk.data or {})
                    elif chunk.type == "done":
                        yield sse("done", chunk.data or {})
                    elif chunk.type == "error":
                        yield sse("error", chunk.data or {"message": "AI 任务失败"})
                    elif chunk.type == "custom":
                        # pipeline 等多步骤场景的自定义事件，event 名取自 data.event
                        data = chunk.data or {}
                        event_name = data.get("event") or "custom"
                        payload = {k: v for k, v in data.items() if k != "event"}
                        yield sse(event_name, payload)
            except GeneratorExit:
                raise
            except Exception:
                logger.warning("AI SSE 输出失败")
                yield sse("error", {"message": "AI 响应中断"})
            finally:
                close = getattr(chunks, "close", None)
                if callable(close):
                    close()

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/dashboard/ai/health")
    def get_ai_health():
        """AI 配置的只读健康投影。零网络，可随意刷新。"""
        try:
            days = parse_int(request.args.get("days"), 7, "days", min_value=1, max_value=30)
            return ok(service.ai_health(days=days))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/providers/probe-models")
    def probe_ai_provider_models():
        """预览上游模型列表。不落库、不建 Provider（spec §4.2）。"""
        try:
            return ok(service.probe_provider_models(require_json_object()))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/providers")
    def list_ai_providers():
        try:
            return ok(service.list_providers())
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/providers")
    def create_ai_provider():
        try:
            provider_id = service.create_provider(json_payload())
            return ok({"id": provider_id})
        except Exception as exc:
            return fail(exc)

    @app.put("/api/dashboard/ai/providers/<int:provider_id>")
    def update_ai_provider(provider_id: int):
        try:
            warnings = service.update_provider(provider_id, json_payload())
            return ok({"warnings": warnings})
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/dashboard/ai/providers/<int:provider_id>")
    def delete_ai_provider(provider_id: int):
        try:
            service.delete_provider(provider_id)
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/providers/<int:provider_id>/test")
    def test_ai_provider(provider_id: int):
        try:
            return ok(service.test_provider(provider_id))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/providers/<int:provider_id>/models/sync")
    def start_ai_provider_model_sync(provider_id: int):
        try:
            operation = service.start_model_sync(provider_id)
            return ok(operation), 202
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/model-sync-operations/<operation_id>")
    def get_ai_model_sync_operation(operation_id: str):
        try:
            return ok(service.get_model_sync_operation(operation_id))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/model-sync-operations/<operation_id>/events")
    def stream_ai_model_sync_operation(operation_id: str):
        try:
            events = service.iter_model_sync_events(operation_id)
            return model_sync_event_response(events)
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/dashboard/ai/model-sync-operations/<operation_id>")
    def cancel_ai_model_sync_operation(operation_id: str):
        try:
            requested = service.cancel_model_sync(operation_id)
            return ok({"cancel_requested": requested})
        except Exception as exc:
            return fail(exc)

    @app.post(
        "/api/dashboard/ai/model-sync-operations/<operation_id>/confirm-empty"
    )
    def confirm_ai_model_sync_empty(operation_id: str):
        try:
            payload = require_json_object()
            unknown = sorted(set(payload) - {"generation", "result_digest"})
            if unknown:
                raise AIServiceError(f"不允许提交字段：{', '.join(unknown)}")
            generation = payload.get("generation")
            if (
                isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation <= 0
            ):
                raise AIServiceError("generation 必须是正整数")
            result_digest = payload.get("result_digest")
            if not isinstance(result_digest, str) or re.fullmatch(
                r"[0-9a-f]{64}", result_digest
            ) is None:
                raise AIServiceError("result_digest 必须是 64 位小写十六进制摘要")
            return ok(
                service.confirm_model_sync_empty(
                    operation_id,
                    generation,
                    result_digest,
                )
            )
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/providers/<int:provider_id>/models")
    def list_ai_provider_models(provider_id: int):
        try:
            return ok(
                service.list_provider_models(
                    provider_id,
                    search=request.args.get("search") or None,
                    routable_only=query_bool("routable_only"),
                    enabled_only=query_bool("enabled_only"),
                )
            )
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/providers/<int:provider_id>/models")
    def create_ai_provider_model(provider_id: int):
        try:
            model_id = service.create_manual_model(
                provider_id,
                require_json_object(),
            )
            return ok({"id": model_id})
        except Exception as exc:
            return fail(exc)

    @app.put("/api/dashboard/ai/provider-models/<int:model_id>")
    def update_ai_provider_model(model_id: int):
        try:
            service.update_provider_model(model_id, require_json_object())
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/dashboard/ai/provider-models/<int:model_id>")
    def delete_ai_provider_model(model_id: int):
        try:
            service.delete_provider_model(model_id)
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/model-pools")
    def list_ai_model_pools():
        try:
            return ok(service.list_model_pools())
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/model-pools")
    def create_ai_model_pool():
        try:
            pool_id = service.create_model_pool(require_json_object())
            return ok({"id": pool_id})
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/model-pools/<int:pool_id>")
    def get_ai_model_pool(pool_id: int):
        try:
            return ok(service.get_model_pool(pool_id))
        except Exception as exc:
            return fail(exc)

    @app.put("/api/dashboard/ai/model-pools/<int:pool_id>")
    def update_ai_model_pool(pool_id: int):
        try:
            version = service.update_model_pool(pool_id, require_json_object())
            return ok({"version": version})
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/dashboard/ai/model-pools/<int:pool_id>")
    def delete_ai_model_pool(pool_id: int):
        try:
            service.delete_model_pool(pool_id)
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.put("/api/dashboard/ai/model-pools/<int:pool_id>/members")
    def replace_ai_model_pool_members(pool_id: int):
        try:
            version = service.replace_model_pool_members(
                pool_id,
                require_json_object(),
            )
            return ok({"version": version})
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/model-pools/<int:pool_id>/attempts")
    def list_ai_model_pool_attempts(pool_id: int):
        try:
            limit = parse_int(
                request.args.get("limit"),
                50,
                "limit",
                min_value=1,
                max_value=200,
            )
            return ok(service.list_model_pool_attempts(pool_id, limit=limit))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/agents")
    def list_ai_agents():
        try:
            return ok(service.list_agents())
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/agents/<int:agent_id>/candidates")
    def preview_ai_agent_candidates(agent_id: int):
        """只读预览：这个 Agent 会按什么顺序调用哪些模型。

        纯解析，不发起任何生成请求；业务生成仍然只能走 ModelRouter 的执行路径。
        """
        try:
            return ok(service.preview_agent_candidates(agent_id))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/agents")
    def create_ai_agent():
        try:
            agent_id = service.create_agent(json_payload())
            return ok({"id": agent_id})
        except Exception as exc:
            return fail(exc)

    @app.put("/api/dashboard/ai/agents/<int:agent_id>")
    def update_ai_agent(agent_id: int):
        try:
            service.update_agent(agent_id, json_payload())
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/dashboard/ai/agents/<int:agent_id>")
    def delete_ai_agent(agent_id: int):
        try:
            service.delete_agent(agent_id)
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.put("/api/dashboard/ai/agents/bindings")
    def update_ai_agent_bindings_route():
        """批量改绑 / 批量启停。单事务。"""
        try:
            return ok(service.update_agent_bindings(require_json_object()))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/jobs")
    def list_ai_jobs():
        try:
            task_type = request.args.get("task_type") or None
            status = request.args.get("status") or None
            page = parse_int(request.args.get("page"), 1, "page", min_value=1)
            page_size = parse_int(request.args.get("page_size"), 20, "page_size", min_value=1, max_value=200)
            db = service._db()
            try:
                result = db.list_ai_jobs(
                    task_type=task_type,
                    status=status,
                    page=page,
                    page_size=page_size,
                )
            finally:
                db.close()
            return ok(result)
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/jobs/<job_id>")
    def get_ai_job(job_id: str):
        try:
            db = service._db()
            try:
                job = db.get_ai_job(job_id)
            finally:
                db.close()
            if job is None:
                raise AINotFoundError("任务不存在")
            return ok(job)
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/jobs/<job_id>/cancel")
    def cancel_ai_job(job_id: str):
        try:
            return ok(service.cancel_job(job_id))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/jobs/<job_id>/continue")
    def continue_ai_job_with_next_model(job_id: str):
        try:
            db = service._db()
            try:
                job = db.get_ai_job(job_id)
            finally:
                db.close()
            if job is None:
                raise AINotFoundError("任务不存在")
            payload = require_json_object()
            # 在建立 SSE 响应前同步校验，确保错误保持为 HTTP 4xx，
            # 不会在响应开始后退化成 HTTP 200 的 SSE error。
            return stream_response(
                service.stream_job_with_next_model(job_id, payload)
            )
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/jobs/cleanup")
    def cleanup_ai_jobs():
        try:
            payload = json_payload()
            keep_days = parse_int(payload.get("keep_days"), 3, "keep_days", min_value=1)
            keep_failed_days = payload.get("keep_failed_days")
            if keep_failed_days is not None:
                keep_failed_days = parse_int(keep_failed_days, 0, "keep_failed_days", min_value=1)
            deleted = service.cleanup_jobs(
                keep_days=keep_days,
                keep_failed_days=keep_failed_days,
            )
            return ok({"deleted": deleted})
        except Exception as exc:
            return fail(exc)

    # ── 内置 Agent 初始化 ──────────────────────────────────────

    @app.post("/api/dashboard/ai/agents/seed")
    def seed_builtin_agents():
        try:
            payload = json_payload()
            provider_id = parse_int(payload.get("provider_id"), 0, "provider_id", min_value=0)
            if not provider_id:
                raise AIServiceError("需要指定 provider_id")
            created = service.seed_builtin_agents(provider_id)
            return ok(created)
        except Exception as exc:
            return fail(exc)
