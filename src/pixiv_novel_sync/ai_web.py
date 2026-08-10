from __future__ import annotations

import json
import hashlib
import logging
import re
import secrets
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

from .ai.service import (
    AINotFoundError,
    AIConflictError,
    AIServiceError,
    AIWritingService,
)
from .ai.detection import detect_ai_tells
from .ai.adult_auth import (
    AdultOwner,
    require_adult_owner,
    sign_adult_access,
    verify_adult_access,
)
from .ai.adult_types import (
    AdultConflictError,
    AdultInputError,
    parse_adult_request,
    raw_sha256,
)
from .ai.adult_validation import compute_provider_scope_hash
from .ai.services.adult import _review_agent_config
from .ai.models import AIStreamChunk
from .settings import Settings
from .storage.ai.core import ADULT_AI_TASK_TYPES
from .storage_files import FileStorage

logger = logging.getLogger(__name__)

_AI_COVER_MAX_BYTES = 10 * 1024 * 1024
_AI_COVER_TYPES = {
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".webp": ("image/webp", b"RIFF"),
}


def _safe_ai_cover_target(public_dir: Path, project_id: int, suffix: str) -> Path:
    root = public_dir.resolve()
    target = (root / "ai_projects" / str(project_id) / f"cover{suffix}").resolve()
    if not target.is_relative_to(root):
        raise AIServiceError("封面路径无效")
    return target


def _safe_stored_ai_cover_path(public_dir: Path, project_id: int, cover_path: str) -> Path:
    root = public_dir.resolve()
    relative = Path(cover_path)
    if relative.is_absolute():
        raise AIServiceError("封面路径无效")
    target = (root / relative).resolve()
    if target == root or not target.is_relative_to(root):
        raise AIServiceError("封面路径无效")
    allowed_targets = {
        _safe_ai_cover_target(root, project_id, suffix)
        for suffix in _AI_COVER_TYPES
    }
    if target not in allowed_targets:
        raise AIServiceError("封面路径无效")
    return target


def _validated_ai_cover(file: Any) -> tuple[str, bytes]:
    filename = str(file.filename or "")
    suffix = Path(filename).suffix.lower()
    file_type = _AI_COVER_TYPES.get(suffix)
    if file_type is None:
        raise AIServiceError("封面仅支持 JPEG、PNG 或 WebP 格式")
    expected_mime, signature = file_type
    actual_mime = str(file.content_type or "").split(";", 1)[0].strip().lower()
    if actual_mime != expected_mime:
        raise AIServiceError("封面扩展名与 MIME 类型不一致")
    payload = file.read(_AI_COVER_MAX_BYTES + 1)
    if len(payload) > _AI_COVER_MAX_BYTES:
        raise AIServiceError("封面不能超过 10 MiB")
    if not payload.startswith(signature):
        raise AIServiceError("封面文件头无效")
    if suffix == ".webp" and (len(payload) < 12 or payload[8:12] != b"WEBP"):
        raise AIServiceError("封面文件头无效")
    return suffix, payload


def _remove_ai_cover_file(public_dir: Path, project_id: int, cover_path: str | None) -> None:
    if not cover_path:
        return
    target = _safe_stored_ai_cover_path(public_dir, project_id, cover_path)
    target.unlink(missing_ok=True)
    try:
        target.parent.rmdir()
    except OSError:
        pass


def _content_disposition(filename: str, disposition: str = "attachment") -> str:
    """Build a safe ``Content-Disposition`` header value (L5).

    Strips CR/LF/other control chars (header-injection defense), emits an ASCII
    ``filename=`` fallback plus an RFC 5987 ``filename*`` for non-ASCII names
    (e.g. Chinese project titles) so the original text survives without letting
    raw bytes into the header.
    """
    from urllib.parse import quote

    raw = filename or "download"
    # 去掉控制符（含 CR/LF）与引号/反斜杠，防止头注入与引号闭合逃逸
    cleaned = "".join(ch for ch in raw if ch >= " " and ch not in '"\\').strip()
    cleaned = cleaned or "download"
    ascii_fallback = cleaned.encode("ascii", "ignore").decode("ascii").strip() or "download"
    encoded = quote(cleaned, safe="")
    return (
        f"{disposition}; filename=\"{ascii_fallback}\"; "
        f"filename*=UTF-8''{encoded}"
    )


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

    def adult_owner() -> AdultOwner:
        return require_adult_owner(current_settings())

    def generic_adult_scope(*, required: bool = False) -> str:
        try:
            return adult_owner().scope
        except PermissionError:
            if required:
                raise
            return ""

    def adult_fail(exc: Exception, status: int = 400):
        message = str(exc) if isinstance(exc, AIServiceError) else "请求无法处理"
        return jsonify({"ok": False, "error": message}), status

    def adult_no_store(response: Response) -> Response:
        response.headers.update(
            {
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "X-Robots-Tag": "noindex, nofollow, noarchive",
                "X-Content-Type-Options": "nosniff",
            }
        )
        return response

    def adult_route_fail(exc: Exception, context: str):
        if isinstance(exc, PermissionError):
            return adult_fail(exc, 403)
        if isinstance(exc, AINotFoundError):
            return adult_fail(exc, 404)
        if isinstance(exc, (AIConflictError, AdultConflictError)):
            return adult_fail(exc, 409)
        if isinstance(exc, AdultInputError):
            return adult_fail(exc, 422)
        if isinstance(exc, AIServiceError):
            return adult_fail(exc, 400)
        logger.warning(context)
        return adult_fail(RuntimeError(), 400)

    def adult_job_for_owner(job_id: str, owner: AdultOwner) -> dict[str, Any] | None:
        db = service._db()
        try:
            job = db.get_adult_job(job_id, owner.scope)
            if job is None:
                return None
            job_input = job.get("input")
            if not isinstance(job_input, dict):
                return None
            project_id = job_input.get("project_id")
            chapter_id = job_input.get("chapter_id")
            if (
                isinstance(project_id, bool)
                or not isinstance(project_id, int)
                or isinstance(chapter_id, bool)
                or not isinstance(chapter_id, int)
            ):
                return None
            project = db.get_ai_writing_project(project_id)
            chapter = db.get_ai_chapter(chapter_id)
            if (
                project is None
                or chapter is None
                or int(chapter.get("project_id") or 0) != project_id
            ):
                return None
            return job
        finally:
            db.close()

    def adult_public_job(job: dict[str, Any]) -> dict[str, Any]:
        result = {
            key: job.get(key)
            for key in (
                "job_id",
                "task_type",
                "status",
                "stage",
                "started_at",
                "finished_at",
                "created_at",
                "error_message",
            )
        }
        output = job.get("output")
        if isinstance(output, dict):
            result["output"] = output
        candidate = job.get("output_text")
        if job.get("status") == "succeeded" and isinstance(candidate, str):
            result["candidate"] = candidate
        return result

    def generic_public_job(job: dict[str, Any]) -> dict[str, Any]:
        if job.get("task_type") not in ADULT_AI_TASK_TYPES:
            return job
        result = adult_public_job(job)
        result.pop("candidate", None)
        return result

    def adult_character_for_project(
        project_id: int,
        character_id: str,
    ) -> dict[str, Any] | None:
        db = service._db()
        try:
            if db.get_ai_writing_project(project_id) is None:
                return None
            character = db.get_adult_character(character_id)
            if character is None or int(character.get("project_id") or 0) != project_id:
                return None
            return character
        finally:
            db.close()

    def adult_candidate_group(snapshot: Any) -> list[dict[str, Any]]:
        fields = (
            "provider_id",
            "provider_name",
            "model_key",
            "provider_model_id",
            "pool_id",
            "pool_name",
            "pool_version",
            "pool_position",
            "capabilities",
            "context_window",
            "fallback_depth",
            "candidate_index",
        )
        return [
            {
                key: (
                    list(value)
                    if key == "capabilities" and isinstance(value, tuple)
                    else value
                )
                for key in fields
                if (value := getattr(candidate, key, None)) is not None
            }
            for candidate in snapshot.candidates
        ]

    def adult_provider_snapshots(agent_id: int) -> dict[str, Any]:
        db = service._db()
        try:
            agent = service._load_agent_config(db, agent_id)
            if agent.task_type != "adult_polish":
                raise AIServiceError("所选 Agent 不是成人描写润色 Agent")
            safety_binding = db.get_adult_review_binding("safety")
            fact_binding = db.get_adult_review_binding("fact_guard")
            if safety_binding is None or fact_binding is None:
                raise AIServiceError("成人审查绑定缺失")
            safety_agent = _review_agent_config("safety", safety_binding)
            fact_agent = _review_agent_config("fact_guard", fact_binding)
        finally:
            db.close()
        return {
            "main": service.model_router.resolve_candidates(agent, stage="main"),
            "safety": service.model_router.resolve_candidates(
                safety_agent,
                stage="validation",
            ),
            "fact_guard": service.model_router.resolve_candidates(
                fact_agent,
                stage="validation",
            ),
        }

    def validate_adult_stream_preflight(payload: dict[str, Any]) -> None:
        parsed = parse_adult_request(payload)
        db = service._db()
        try:
            project = db.get_ai_writing_project(parsed.project_id)
            chapter = db.get_ai_chapter(parsed.chapter_id)
            if project is None or chapter is None:
                raise AIServiceError("写作项目或章节不存在")
            if int(chapter.get("project_id") or 0) != parsed.project_id:
                raise AIServiceError("章节不属于当前写作项目")
            content = chapter.get("content")
            if not isinstance(content, str):
                raise AIServiceError("章节正文无效")
            if int(chapter.get("chapter_revision") or 0) != parsed.chapter_revision:
                raise AIConflictError("409: 章节 revision 已变化")
            if raw_sha256(content) != parsed.chapter_content_hash:
                raise AIConflictError("409: 章节正文已变化")
            if parsed.target_end > len(content):
                raise AIServiceError("目标片段超出章节正文范围")
            target = content[parsed.target_start : parsed.target_end]
            if raw_sha256(target) != parsed.target_text_hash:
                raise AIConflictError("409: 目标片段已变化")
        finally:
            db.close()
        snapshots = adult_provider_snapshots(parsed.agent_id)
        if compute_provider_scope_hash(snapshots) != parsed.provider_scope_hash:
            raise AIConflictError("409: Provider 范围已变化，请重新确认")

    def adult_access_from_request(owner: AdultOwner, job_id: str) -> str:
        token = request.headers.get("X-Adult-Access-Token") or request.args.get(
            "access_token"
        )
        verify_adult_access(token, owner, job_id)
        assert isinstance(token, str)
        return token

    def adult_stream_response(chunks: Iterator, owner: AdultOwner) -> Response:
        allowed = {"metadata", "progress", "validation", "candidate", "done", "error"}
        event_fields = {
            "metadata": {"job_id", "parent_job_id", "replayed"},
            "progress": {
                "job_id",
                "phase",
                "action",
                "stage",
                "status",
                "candidate_index",
                "provider_id",
                "provider_name",
                "provider_model_id",
                "model_key",
                "pool_id",
                "pool_name",
                "pool_position",
                "fallback_depth",
                "error_category",
                "finish_reason",
                "reason",
            },
            "validation": {
                "job_id",
                "applicable",
                "warnings",
                "blocking_issues",
                "protected_terms_missing",
                "paragraph_delta",
                "length_ratio",
                "perspective_warning",
                "new_number_tokens",
                "diff_summary",
                "validation_hash",
            },
            "candidate": {"job_id", "applicable", "validation_hash", "replayed"},
            "done": {"job_id", "applicable", "validation_hash", "replayed"},
            "error": {"job_id", "code", "message", "replayed"},
        }
        error_messages = {
            "adult_polish_failed": "成人润色任务失败",
            "cancelled": "成人润色任务已取消",
            "generation_failed": "成人润色任务未成功完成",
            "idempotent_in_progress": "相同的成人润色请求正在执行",
            "output_too_large": "成人润色候选无效",
            "partial": "生成结果不完整，候选已丢弃",
            "preflight_failed": "成人润色前置校验失败",
            "review_unavailable": "成人审查暂不可用",
            "route_contract_error": "成人润色候选无效",
            "route_unavailable": "成人润色模型暂不可用",
            "safety_blocked": "成人润色候选未通过安全检查",
            "validation_failed": "成人润色候选校验失败",
        }

        def generate():
            access_token: str | None = None
            job_id: str | None = None

            def cancel_active_job() -> None:
                if job_id is None:
                    return
                db = service._db()
                try:
                    db.request_adult_job_cancel(job_id, owner.scope)
                finally:
                    db.close()

            try:
                for chunk in chunks:
                    event = str(getattr(chunk, "type", ""))
                    if event == "delta" or event not in allowed:
                        continue
                    raw_data = getattr(chunk, "data", None)
                    data = dict(raw_data) if isinstance(raw_data, dict) else {}
                    event_job_id = data.get("job_id")
                    if isinstance(event_job_id, str) and event_job_id:
                        if job_id is not None and event_job_id != job_id:
                            yield sse(
                                "error",
                                {"code": "route_contract_error", "message": "成人润色任务响应无效"},
                            )
                            return
                        job_id = event_job_id
                    data = {
                        key: data[key]
                        for key in event_fields[event]
                        if key in data
                    }
                    if event == "metadata":
                        if job_id is None:
                            yield sse(
                                "error",
                                {"code": "route_contract_error", "message": "成人润色任务响应无效"},
                            )
                            return
                        access_token = sign_adult_access(owner, job_id)
                        data["access_token"] = access_token
                    elif event == "candidate":
                        candidate = getattr(chunk, "text", None)
                        if job_id is None or access_token is None or not isinstance(candidate, str):
                            yield sse(
                                "error",
                                {"code": "route_contract_error", "message": "成人润色候选响应无效"},
                            )
                            return
                        data["candidate"] = candidate
                        data["access_token"] = access_token
                    elif event == "error":
                        code = str(data.get("code") or "adult_polish_failed")
                        if code not in error_messages:
                            code = "adult_polish_failed"
                        data = {
                            "code": code,
                            "message": error_messages[code],
                            **(
                                {"job_id": job_id}
                                if job_id is not None
                                else {}
                            ),
                            **(
                                {"replayed": True}
                                if data.get("replayed") is True
                                else {}
                            ),
                        }
                    yield sse(event, data)
            except GeneratorExit:
                cancel_active_job()
                close = getattr(chunks, "close", None)
                if callable(close):
                    close()
                raise
            except Exception:
                try:
                    cancel_active_job()
                except Exception:
                    logger.warning("成人润色断连取消失败")
                logger.warning("成人润色 SSE 输出失败")
                yield sse(
                    "error",
                    {"code": "stream_failed", "message": "成人润色响应中断"},
                )
            finally:
                close = getattr(chunks, "close", None)
                if callable(close):
                    close()

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "X-Robots-Tag": "noindex, nofollow, noarchive",
                "X-Content-Type-Options": "nosniff",
                "X-Accel-Buffering": "no",
            },
        )

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
                close = getattr(chunks, "close", None)
                if callable(close):
                    close()
                raise

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/dashboard/ai")
    def dashboard_ai_page():
        return render_template("dashboard_ai.html")

    @app.get("/dashboard/wizard")
    def dashboard_wizard_page():
        return render_template("dashboard_wizard.html")

    @app.get("/dashboard/novels/ai/<int:project_id>")
    def dashboard_ai_project_reader_page(project_id: int):
        return render_template("dashboard_ai_reader.html", project_id=project_id)

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
            service.update_provider(provider_id, json_payload())
            return ok()
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

    @app.post("/api/dashboard/ai/agents/adult-polish/seed")
    def seed_adult_polish_agent():
        try:
            adult_owner()
            return ok(service.ensure_adult_polish_agent(require_json_object()))
        except Exception as exc:
            return adult_route_fail(exc, "创建成人润色 Agent 失败")

    @app.get("/api/dashboard/ai/adult-review-bindings/<review_kind>")
    def get_adult_review_binding(review_kind: str):
        try:
            adult_owner()
            bindings = service.list_adult_review_bindings()
            binding = bindings.get(review_kind)
            if binding is None:
                raise AIServiceError("成人审查绑定类型无效")
            return ok(binding)
        except Exception as exc:
            return adult_route_fail(exc, "读取成人审查绑定失败")

    @app.put("/api/dashboard/ai/adult-review-bindings/<review_kind>")
    def update_adult_review_binding(review_kind: str):
        try:
            adult_owner()
            payload = require_json_object()
            if "expected_version" not in payload:
                raise AIServiceError("缺少 expected_version")
            expected_version = payload.pop("expected_version")
            if (
                isinstance(expected_version, bool)
                or not isinstance(expected_version, int)
                or expected_version <= 0
            ):
                raise AIServiceError("expected_version 必须是正整数")
            return ok(
                service.update_adult_review_binding(
                    review_kind,
                    payload,
                    expected_version=expected_version,
                )
            )
        except Exception as exc:
            return adult_route_fail(exc, "更新成人审查绑定失败")

    @app.post("/api/dashboard/ai/polish/adult/scope")
    def get_ai_adult_provider_scope():
        try:
            adult_owner()
            payload = require_json_object()
            if set(payload) != {"agent_id"}:
                raise AIServiceError("Provider 范围请求字段无效")
            agent_id = payload.get("agent_id")
            if isinstance(agent_id, bool) or not isinstance(agent_id, int) or agent_id <= 0:
                raise AIServiceError("agent_id 必须是正整数")
            snapshots = adult_provider_snapshots(agent_id)
            return ok(
                {
                    "groups": {
                        kind: adult_candidate_group(snapshot)
                        for kind, snapshot in snapshots.items()
                    },
                    "provider_scope_hash": compute_provider_scope_hash(snapshots),
                }
            )
        except Exception as exc:
            return adult_route_fail(exc, "读取成人 Provider 范围失败")

    @app.get("/api/dashboard/ai/projects/<int:project_id>/characters")
    def list_ai_adult_characters(project_id: int):
        try:
            adult_owner()
            return ok(service.list_adult_characters(project_id))
        except Exception as exc:
            return adult_route_fail(exc, "读取成人角色失败")

    @app.post("/api/dashboard/ai/projects/<int:project_id>/characters")
    def create_ai_adult_character(project_id: int):
        try:
            adult_owner()
            return ok(service.create_adult_character(project_id, require_json_object()))
        except Exception as exc:
            return adult_route_fail(exc, "创建成人角色失败")

    @app.put(
        "/api/dashboard/ai/projects/<int:project_id>/characters/<character_id>"
    )
    def update_ai_adult_character(project_id: int, character_id: str):
        try:
            adult_owner()
            if adult_character_for_project(project_id, character_id) is None:
                return adult_fail(AINotFoundError("成人角色不存在"), 404)
            payload = require_json_object()
            expected_revision = payload.pop("expected_revision", None)
            return ok(
                service.update_adult_character(
                    character_id,
                    payload,
                    expected_revision=expected_revision,
                )
            )
        except Exception as exc:
            return adult_route_fail(exc, "更新成人角色失败")

    @app.delete(
        "/api/dashboard/ai/projects/<int:project_id>/characters/<character_id>"
    )
    def delete_ai_adult_character(project_id: int, character_id: str):
        try:
            adult_owner()
            if adult_character_for_project(project_id, character_id) is None:
                return adult_fail(AINotFoundError("成人角色不存在"), 404)
            payload = require_json_object()
            unknown = sorted(set(payload) - {"expected_revision"})
            if unknown:
                raise AIServiceError("角色删除请求字段无效")
            return ok(
                service.deactivate_adult_character(
                    character_id,
                    expected_revision=payload.get("expected_revision"),
                )
            )
        except Exception as exc:
            return adult_route_fail(exc, "停用成人角色失败")

    @app.get(
        "/api/dashboard/ai/projects/<int:project_id>/adult-confirmation"
    )
    def get_ai_adult_confirmation(project_id: int):
        try:
            adult_owner()
            return ok(service.get_adult_confirmation(project_id))
        except Exception as exc:
            return adult_route_fail(exc, "读取项目成人确认失败")

    @app.put(
        "/api/dashboard/ai/projects/<int:project_id>/adult-confirmation"
    )
    def update_ai_adult_confirmation(project_id: int):
        try:
            adult_owner()
            payload = require_json_object()
            expected_revision = payload.pop("expected_revision", None)
            return ok(
                service.update_adult_confirmation(
                    project_id,
                    payload,
                    expected_revision=expected_revision,
                )
            )
        except Exception as exc:
            return adult_route_fail(exc, "更新项目成人确认失败")

    @app.post("/api/dashboard/ai/documents/upload")
    def upload_ai_document():
        try:
            file = request.files.get("file")
            if file is None or not file.filename:
                raise AIServiceError("请选择要上传的文件")
            filename = file.filename
            suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if suffix not in {"txt", "md"}:
                raise AIServiceError("仅支持上传 .txt / .md 文件")
            raw = file.read()
            if len(raw) > 5 * 1024 * 1024:
                raise AIServiceError("上传文本不能超过 5MB")
            content = raw.decode("utf-8-sig")
            document_id = service.create_document({"title": filename, "source_type": "upload", "content": content})
            return ok({"id": document_id})
        except UnicodeDecodeError:
            return fail(AIServiceError("文件必须是 UTF-8 编码"))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/documents/manual")
    def create_ai_document():
        try:
            document_id = service.create_document(json_payload())
            return ok({"id": document_id})
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/continue/stream")
    def stream_ai_continue():
        try:
            return stream_response(service.stream_continue(json_payload()))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/rewrite/stream")
    def stream_ai_rewrite():
        try:
            return stream_response(service.stream_rewrite(json_payload()))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/polish/adult/stream")
    def stream_ai_adult_polish():
        try:
            owner = adult_owner()
            payload = require_json_object()
            validate_adult_stream_preflight(payload)
            chunks = service.stream_adult_polish(
                payload,
                owner.scope,
                secrets.token_urlsafe(32),
            )
            return adult_stream_response(chunks, owner)
        except PermissionError as exc:
            return adult_fail(exc, 403)
        except AdultInputError as exc:
            return adult_fail(exc, 422)
        except AIConflictError as exc:
            return adult_fail(exc, 409)
        except AIServiceError as exc:
            return adult_fail(exc, 400)
        except Exception:
            logger.warning("成人润色请求初始化失败")
            return adult_fail(RuntimeError(), 400)

    @app.get("/api/dashboard/ai/polish/adult/<job_id>")
    def get_ai_adult_polish(job_id: str):
        try:
            owner = adult_owner()
            job = adult_job_for_owner(job_id, owner)
            if job is None:
                return adult_fail(AINotFoundError("任务不存在"), 404)
            adult_access_from_request(owner, job_id)
            return adult_no_store(ok(adult_public_job(job)))
        except PermissionError as exc:
            return adult_fail(exc, 403)
        except Exception:
            logger.warning("读取成人润色任务失败")
            return adult_fail(RuntimeError(), 404)

    @app.get("/api/dashboard/ai/polish/adult/<job_id>/events")
    def stream_ai_adult_polish_events(job_id: str):
        try:
            owner = adult_owner()
            job = adult_job_for_owner(job_id, owner)
            if job is None:
                return adult_fail(AINotFoundError("任务不存在"), 404)
            adult_access_from_request(owner, job_id)

            def replay():
                yield AIStreamChunk(
                    type="metadata",
                    data={"job_id": job_id, "replayed": True},
                )
                status = str(job.get("status") or "")
                candidate = job.get("output_text")
                if status == "succeeded" and isinstance(candidate, str):
                    yield AIStreamChunk(
                        type="candidate",
                        text=candidate,
                        data={"job_id": job_id, "replayed": True},
                    )
                    yield AIStreamChunk(
                        type="done",
                        data={"job_id": job_id, "replayed": True},
                    )
                elif status == "running":
                    yield AIStreamChunk(
                        type="progress",
                        data={"job_id": job_id, "status": "running"},
                    )
                else:
                    yield AIStreamChunk(
                        type="error",
                        data={
                            "job_id": job_id,
                            "code": str((job.get("output") or {}).get("code") or status or "failed"),
                            "message": str(job.get("error_message") or "成人润色任务未成功完成"),
                        },
                    )

            return adult_stream_response(replay(), owner)
        except PermissionError as exc:
            return adult_fail(exc, 403)
        except Exception:
            logger.warning("恢复成人润色事件失败")
            return adult_fail(RuntimeError(), 404)

    @app.post("/api/dashboard/ai/polish/adult/<job_id>/cancel")
    def cancel_ai_adult_polish(job_id: str):
        try:
            owner = adult_owner()
            job = adult_job_for_owner(job_id, owner)
            if job is None:
                return adult_fail(AINotFoundError("任务不存在"), 404)
            adult_access_from_request(owner, job_id)
            db = service._db()
            try:
                requested = db.request_adult_job_cancel(job_id, owner.scope)
            finally:
                db.close()
            return ok({"cancel_requested": requested})
        except PermissionError as exc:
            return adult_fail(exc, 403)
        except Exception:
            logger.warning("取消成人润色任务失败")
            return adult_fail(RuntimeError(), 404)

    @app.post("/api/dashboard/ai/polish/adult/<job_id>/regenerate")
    def regenerate_ai_adult_polish(job_id: str):
        try:
            owner = adult_owner()
            job = adult_job_for_owner(job_id, owner)
            if job is None:
                return adult_fail(AINotFoundError("任务不存在"), 404)
            adult_access_from_request(owner, job_id)
            payload = require_json_object()
            supplied_parent = payload.get("parent_job_id")
            if supplied_parent not in {None, job_id}:
                raise AIConflictError("409: 父成人润色任务不匹配")
            payload["parent_job_id"] = job_id
            validate_adult_stream_preflight(payload)
            chunks = service.stream_adult_polish(
                payload,
                owner.scope,
                secrets.token_urlsafe(32),
            )
            return adult_stream_response(chunks, owner)
        except PermissionError as exc:
            return adult_fail(exc, 403)
        except AdultInputError as exc:
            return adult_fail(exc, 422)
        except (AIConflictError, AdultConflictError) as exc:
            return adult_fail(exc, 409)
        except AIServiceError as exc:
            return adult_fail(exc, 400)
        except Exception:
            logger.warning("重新生成成人润色候选失败")
            return adult_fail(RuntimeError(), 400)

    @app.post("/api/dashboard/ai/polish/adult/<job_id>/apply")
    def apply_ai_adult_polish(job_id: str):
        try:
            owner = adult_owner()
            job = adult_job_for_owner(job_id, owner)
            if job is None:
                return adult_fail(AINotFoundError("任务不存在"), 404)
            access_token = adult_access_from_request(owner, job_id)
            payload = require_json_object()
            if set(payload) != {"warning_ack_hash"}:
                raise AIServiceError("apply 请求字段无效")
            warning_ack_hash = payload.get("warning_ack_hash")
            if not isinstance(warning_ack_hash, str) or len(warning_ack_hash) > 64:
                raise AIServiceError("warning_ack_hash 无效")
            access_hash = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
            db = service._db()
            try:
                binding = db.bind_adult_application_access(
                    job_id,
                    owner.scope,
                    access_hash,
                )
            finally:
                db.close()
            if binding is None:
                raise AIConflictError("409: 成人润色候选不可应用")
            if binding["applied"]:
                return ok(
                    {
                        "application_id": binding["application_id"],
                        "chapter_revision_after": binding[
                            "chapter_revision_after"
                        ],
                        "chapter_hash_after": binding["chapter_hash_after"],
                        "idempotent": True,
                    }
                )
            return ok(
                service.apply_adult_polish(
                    job_id,
                    owner.scope,
                    warning_ack_hash,
                    access_token,
                )
            )
        except PermissionError as exc:
            return adult_fail(exc, 403)
        except (AIConflictError, AdultConflictError) as exc:
            return adult_fail(exc, 409)
        except AIServiceError as exc:
            return adult_fail(exc, 400)
        except Exception:
            logger.warning("应用成人润色候选失败")
            return adult_fail(RuntimeError(), 400)

    @app.get("/api/dashboard/ai/drafts")
    def list_ai_drafts():
        try:
            page = parse_int(request.args.get("page"), 1, "page", min_value=1)
            page_size = parse_int(request.args.get("page_size"), 20, "page_size", min_value=1, max_value=200)
            return ok(service.list_drafts(page=page, page_size=page_size))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/drafts")
    def create_ai_draft():
        try:
            draft_id = service.create_draft(json_payload())
            return ok({"id": draft_id})
        except Exception as exc:
            return fail(exc)

    @app.put("/api/dashboard/ai/drafts/<int:draft_id>")
    def update_ai_draft(draft_id: int):
        try:
            service.update_draft(draft_id, json_payload())
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/dashboard/ai/drafts/<int:draft_id>")
    def delete_ai_draft(draft_id: int):
        try:
            service.delete_draft(draft_id)
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/drafts/<int:draft_id>/history")
    def get_ai_draft_history(draft_id: int):
        try:
            return ok(service.get_draft_history(draft_id))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/drafts/<int:draft_id>/fork")
    def fork_ai_draft(draft_id: int):
        try:
            new_id = service.fork_draft(draft_id, json_payload())
            return ok({"id": new_id})
        except Exception as exc:
            return fail(exc)

    # ── AI 任务日志（数据源迁移到统一任务日志页展示）───────────────────────

    @app.get("/api/dashboard/ai/jobs")
    def list_ai_jobs():
        try:
            task_type = request.args.get("task_type") or None
            status = request.args.get("status") or None
            page = parse_int(request.args.get("page"), 1, "page", min_value=1)
            page_size = parse_int(request.args.get("page_size"), 20, "page_size", min_value=1, max_value=200)
            owner_scope = generic_adult_scope(
                required=task_type in ADULT_AI_TASK_TYPES
            )
            db = service._db()
            try:
                result = db.list_ai_jobs(
                    task_type=task_type,
                    status=status,
                    page=page,
                    page_size=page_size,
                    owner_scope=owner_scope,
                )
            finally:
                db.close()
            result["items"] = [
                generic_public_job(item) for item in result.get("items", [])
            ]
            return ok(result)
        except PermissionError as exc:
            return adult_fail(exc, 403)
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/jobs/<job_id>")
    def get_ai_job(job_id: str):
        try:
            db = service._db()
            try:
                job = db.get_ai_job(job_id, owner_scope=generic_adult_scope())
            finally:
                db.close()
            if job is None:
                raise AINotFoundError("任务不存在")
            return ok(generic_public_job(job))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/jobs/<job_id>/continue")
    def continue_ai_job_with_next_model(job_id: str):
        try:
            db = service._db()
            try:
                job = db.get_ai_job(job_id, owner_scope=generic_adult_scope())
            finally:
                db.close()
            if job is None or job.get("task_type") in ADULT_AI_TASK_TYPES:
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
            db = service._db()
            try:
                deleted = db.cleanup_ai_jobs(
                    keep_days=keep_days,
                    keep_failed_days=keep_failed_days,
                    owner_scope=generic_adult_scope(),
                )
            finally:
                db.close()
            return ok({"deleted": deleted})
        except Exception as exc:
            return fail(exc)

    # ── 风格蒸馏 ────────────────────────────────────────────────

    @app.post("/api/dashboard/ai/distill/style/stream")
    def stream_distill_style():
        try:
            return stream_response(service.stream_distill_style(json_payload()))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/style-profiles")
    def list_style_profiles():
        try:
            page = parse_int(request.args.get("page"), 1, "page", min_value=1)
            page_size = parse_int(request.args.get("page_size"), 20, "page_size", min_value=1, max_value=200)
            return ok(service.list_style_profiles(page=page, page_size=page_size))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/style-profiles/<int:profile_id>")
    def get_style_profile(profile_id: int):
        try:
            return ok(service.get_style_profile(profile_id))
        except Exception as exc:
            return fail(exc)

    @app.put("/api/dashboard/ai/style-profiles/<int:profile_id>")
    def update_style_profile(profile_id: int):
        try:
            service.update_style_profile(profile_id, json_payload())
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/dashboard/ai/style-profiles/<int:profile_id>")
    def delete_style_profile(profile_id: int):
        try:
            service.delete_style_profile(profile_id)
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/style-profiles/save")
    def save_style_profile():
        try:
            profile_id = service.save_style_profile(json_payload())
            return ok({"id": profile_id})
        except Exception as exc:
            return fail(exc)

    # ── 小说蒸馏 ────────────────────────────────────────────────

    @app.post("/api/dashboard/ai/distill/novel/stream")
    def stream_distill_novel():
        try:
            return stream_response(service.stream_distill_novel(json_payload()))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/novel-profiles")
    def list_novel_profiles():
        try:
            page = parse_int(request.args.get("page"), 1, "page", min_value=1)
            page_size = parse_int(request.args.get("page_size"), 20, "page_size", min_value=1, max_value=200)
            return ok(service.list_novel_profiles(page=page, page_size=page_size))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/novel-profiles/<int:profile_id>")
    def get_novel_profile(profile_id: int):
        try:
            return ok(service.get_novel_profile(profile_id))
        except Exception as exc:
            return fail(exc)

    @app.put("/api/dashboard/ai/novel-profiles/<int:profile_id>")
    def update_novel_profile(profile_id: int):
        try:
            service.update_novel_profile(profile_id, json_payload())
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/dashboard/ai/novel-profiles/<int:profile_id>")
    def delete_novel_profile(profile_id: int):
        try:
            service.delete_novel_profile(profile_id)
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/novel-profiles/save")
    def save_novel_profile():
        try:
            profile_id = service.save_novel_profile(json_payload())
            return ok({"id": profile_id})
        except Exception as exc:
            return fail(exc)

    # ── 内容审计 ────────────────────────────────────────────────

    @app.post("/api/dashboard/ai/audit/stream")
    def stream_audit():
        try:
            return stream_response(service.stream_audit(json_payload()))
        except Exception as exc:
            return fail(exc)

    # ── 写前构思 ────────────────────────────────────────────────

    @app.post("/api/dashboard/ai/plan/stream")
    def stream_plan():
        try:
            return stream_response(service.stream_plan(json_payload()))
        except Exception as exc:
            return fail(exc)

    # ── AI 痕迹检测（本地规则，无需 LLM）─────────────────────────

    @app.post("/api/dashboard/ai/detect-ai-tells")
    def detect_ai_tells_route():
        try:
            payload = json_payload()
            text = str(payload.get("text") or "")
            if not text.strip():
                raise AIServiceError("文本不能为空")
            report = detect_ai_tells(text)
            return ok({
                "score": report.score,
                "issues": [
                    {"type": i.type, "severity": i.severity, "message": i.message, "detail": i.detail}
                    for i in report.issues
                ],
                "stats": report.stats,
            })
        except Exception as exc:
            return fail(exc)

    # ── Prompt 模板 ─────────────────────────────────────────────

    @app.get("/api/dashboard/ai/prompt-templates")
    def list_prompt_templates():
        try:
            category = request.args.get("category") or None
            return ok(service.list_prompt_templates(category=category))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/prompt-templates/<int:template_id>")
    def get_prompt_template(template_id: int):
        try:
            return ok(service.get_prompt_template(template_id))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/prompt-templates")
    def create_prompt_template():
        try:
            template_id = service.create_prompt_template(json_payload())
            return ok({"id": template_id})
        except Exception as exc:
            return fail(exc)

    @app.put("/api/dashboard/ai/prompt-templates/<int:template_id>")
    def update_prompt_template(template_id: int):
        try:
            service.update_prompt_template(template_id, json_payload())
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/dashboard/ai/prompt-templates/<int:template_id>")
    def delete_prompt_template(template_id: int):
        try:
            service.delete_prompt_template(template_id)
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/prompt-templates/seed")
    def seed_prompt_templates():
        try:
            service.seed_builtin_templates()
            return ok()
        except Exception as exc:
            return fail(exc)

    # ── 系列搜索 ────────────────────────────────────────────────

    @app.get("/api/dashboard/ai/series/search")
    def search_series_for_ai():
        """搜索系列，用于 AI 创作选择输入源。"""
        try:
            q = str(request.args.get("q", "") or "").strip()
            limit = parse_int(request.args.get("limit"), 10, "limit", min_value=1, max_value=20)
            from .storage_db import Database
            db = Database(current_settings().storage.db_path)
            db.init_schema()
            try:
                search_pattern = f"%{q}%" if q else "%"
                rows = db.conn.execute(
                    """
                    SELECT
                        se.series_id,
                        CASE WHEN se.title IS NOT NULL AND se.title != '' THEN se.title
                             ELSE (SELECT MIN(n.title) FROM novels n WHERE n.series_id = se.series_id)
                        END AS title,
                        u.name AS author_name,
                        se.total_novels,
                        COALESCE((SELECT SUM(n.text_length) FROM novels n WHERE n.series_id = se.series_id), 0) AS total_text_length
                    FROM series se
                    LEFT JOIN users AS u ON u.user_id = se.user_id
                    WHERE (se.title LIKE ? OR u.name LIKE ?)
                      AND EXISTS (SELECT 1 FROM novels n WHERE n.series_id = se.series_id)
                    ORDER BY se.last_seen_at DESC
                    LIMIT ?
                    """,
                    (search_pattern, search_pattern, limit),
                ).fetchall()
                return ok([dict(row) for row in rows])
            finally:
                db.close()
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

    # ── 写作项目 ───────────────────────────────────────────────

    @app.get("/api/dashboard/ai/projects")
    def list_writing_projects():
        try:
            status = request.args.get("status") or None
            return ok(service.list_writing_projects(status=status))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/projects/<int:project_id>")
    def get_writing_project(project_id: int):
        try:
            return ok(service.get_writing_project(project_id))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/projects")
    def create_writing_project():
        try:
            project_id = service.create_writing_project(json_payload())
            return ok({"id": project_id})
        except Exception as exc:
            return fail(exc)

    @app.put("/api/dashboard/ai/projects/<int:project_id>")
    def update_writing_project(project_id: int):
        try:
            payload = json_payload()
            payload.pop("cover_path", None)
            service.update_writing_project(project_id, payload)
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/projects/<int:project_id>/cover")
    def upload_writing_project_cover(project_id: int):
        try:
            file = request.files.get("cover")
            if file is None or not file.filename:
                raise AIServiceError("请选择要上传的封面")
            project = service.get_writing_project(project_id)
            suffix, payload = _validated_ai_cover(file)
            settings_now = current_settings()
            public_dir = settings_now.storage.public_dir.resolve()
            target = _safe_ai_cover_target(public_dir, project_id, suffix)
            previous_path = project.get("cover_path")
            previous_target = (
                _safe_stored_ai_cover_path(public_dir, project_id, str(previous_path))
                if previous_path else None
            )
            previous_payload = (
                previous_target.read_bytes()
                if previous_target == target and previous_target.exists()
                else None
            )
            FileStorage(settings_now).write_bytes(target, payload)
            relative = target.relative_to(public_dir).as_posix()
            try:
                service.update_writing_project_cover(project_id, relative)
            except Exception:
                if previous_payload is not None:
                    FileStorage(settings_now).write_bytes(target, previous_payload)
                else:
                    target.unlink(missing_ok=True)
                    try:
                        target.parent.rmdir()
                    except OSError:
                        pass
                raise
            if previous_target is not None and previous_target != target:
                try:
                    _remove_ai_cover_file(public_dir, project_id, str(previous_path))
                except (AIServiceError, OSError):
                    logger.warning("清理旧 AI 项目封面失败：%s", previous_path, exc_info=True)
            return ok({"cover_url": f"/api/dashboard/ai/projects/{project_id}/cover"})
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/projects/<int:project_id>/cover")
    def get_writing_project_cover(project_id: int):
        try:
            project = service.get_writing_project(project_id)
            cover_path = project.get("cover_path")
            if not cover_path:
                return fail(AIServiceError("封面不存在"), 404)
            target = _safe_stored_ai_cover_path(
                current_settings().storage.public_dir,
                project_id,
                str(cover_path),
            )
            if not target.is_file():
                return fail(AIServiceError("封面不存在"), 404)
            mimetype = _AI_COVER_TYPES.get(target.suffix.lower(), (None, b""))[0]
            return send_file(target, mimetype=mimetype, conditional=True)
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/dashboard/ai/projects/<int:project_id>/cover")
    def delete_writing_project_cover(project_id: int):
        try:
            project = service.get_writing_project(project_id)
            cover_path = project.get("cover_path")
            if cover_path:
                public_dir = current_settings().storage.public_dir.resolve()
                _safe_stored_ai_cover_path(public_dir, project_id, str(cover_path))
                service.update_writing_project_cover(project_id, None)
                _remove_ai_cover_file(public_dir, project_id, str(cover_path))
            return ok({"cover_url": None})
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/dashboard/ai/projects/<int:project_id>")
    def delete_writing_project(project_id: int):
        try:
            project = service.get_writing_project(project_id)
            cover_path = project.get("cover_path")
            service.delete_writing_project(project_id)
            if cover_path:
                try:
                    _remove_ai_cover_file(
                        current_settings().storage.public_dir,
                        project_id,
                        str(cover_path),
                    )
                except (AIServiceError, OSError):
                    logger.warning("删除 AI 项目时清理封面失败：%s", cover_path, exc_info=True)
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/projects/<int:project_id>/reader")
    def writing_project_reader_api(project_id: int):
        try:
            return ok(service.get_writing_project_reader(project_id))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/projects/<int:project_id>/download")
    def writing_project_download_api(project_id: int):
        try:
            filename, content = service.export_writing_project_text(project_id)
            # L5: 文件名源自用户可改的项目标题。仅删 " 不足以防头注入
            # （CR/LF、控制符）。_content_disposition 剥离控制符并生成合规头：
            # 中文标题走 RFC 5987 filename*，并带一个 ASCII 回退。
            disposition = _content_disposition(filename)
            return Response(
                content,
                mimetype="text/plain; charset=utf-8",
                headers={"Content-Disposition": disposition},
            )
        except Exception as exc:
            return fail(exc)

    # ── 章节 ───────────────────────────────────────────────────

    @app.get("/api/dashboard/ai/projects/<int:project_id>/chapters")
    def list_chapters(project_id: int):
        try:
            return ok(service.list_chapters(project_id))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/chapters/<int:chapter_id>")
    def get_chapter(chapter_id: int):
        try:
            return ok(service.get_chapter(chapter_id))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/chapters")
    def create_chapter():
        try:
            chapter_id = service.create_chapter(json_payload())
            return ok({"id": chapter_id})
        except Exception as exc:
            return fail(exc)

    @app.put("/api/dashboard/ai/chapters/<int:chapter_id>")
    def update_chapter(chapter_id: int):
        try:
            service.update_chapter(chapter_id, json_payload())
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/dashboard/ai/chapters/<int:chapter_id>")
    def delete_chapter(chapter_id: int):
        try:
            service.delete_chapter(chapter_id)
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/projects/<int:project_id>/longform-plan/stream")
    def stream_longform_plan(project_id: int):
        try:
            payload = json_payload()
            payload["project_id"] = project_id
            return stream_response(service.stream_longform_plan(payload))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/projects/<int:project_id>/longform-plan/details/stream")
    def stream_longform_plan_details(project_id: int):
        try:
            payload = json_payload()
            payload["project_id"] = project_id
            return stream_response(service.stream_longform_plan_details(payload))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/projects/<int:project_id>/longform-plan/import-output")
    def import_longform_plan_output_api(project_id: int):
        try:
            return ok(service.import_longform_plan_output(project_id, json_payload()))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/projects/<int:project_id>/longform-plan/details/import-output")
    def import_longform_plan_details_output_api(project_id: int):
        try:
            return ok(service.import_longform_plan_details_output(project_id, json_payload()))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/projects/<int:project_id>/context/preview")
    def preview_project_context_api(project_id: int):
        try:
            payload = json_payload()
            payload["project_id"] = project_id
            return ok(service.preview_project_context(payload))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/projects/<int:project_id>/chapters/batch")
    def create_chapters_batch(project_id: int):
        try:
            payload = json_payload()
            return ok(service.create_chapters_from_plan(
                project_id,
                payload.get("chapters") or [],
                mode=payload.get("mode") or "missing_only",
            ))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/chapters/continue/stream")
    def stream_chapter_continue():
        try:
            return stream_response(service.stream_chapter_continue(json_payload()))
        except Exception as exc:
            return fail(exc)

    # ── 项目状态记忆 ───────────────────────────────────────────

    @app.get("/api/dashboard/ai/projects/<int:project_id>/states")
    def get_project_states(project_id: int):
        try:
            return ok(service.get_project_states(project_id))
        except Exception as exc:
            return fail(exc)

    @app.put("/api/dashboard/ai/projects/<int:project_id>/states/<state_type>")
    def update_project_state(project_id: int, state_type: str):
        try:
            payload = json_payload()
            content = str(payload.get("content") or "")
            service.update_project_state(project_id, state_type, content)
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/projects/<int:project_id>/states/auto-update/stream")
    def stream_update_state(project_id: int):
        try:
            payload = json_payload()
            payload["project_id"] = project_id
            return stream_response(service.stream_update_project_state(payload))
        except Exception as exc:
            return fail(exc)

    # ── 伏笔管理 ───────────────────────────────────────────────

    @app.get("/api/dashboard/ai/projects/<int:project_id>/foreshadows")
    def list_foreshadows(project_id: int):
        try:
            status = request.args.get("status") or None
            return ok(service.list_foreshadows(project_id, status=status))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/foreshadows")
    def create_foreshadow():
        try:
            foreshadow_id = service.create_foreshadow(json_payload())
            return ok({"id": foreshadow_id})
        except Exception as exc:
            return fail(exc)

    @app.put("/api/dashboard/ai/foreshadows/<int:foreshadow_id>")
    def update_foreshadow(foreshadow_id: int):
        try:
            service.update_foreshadow(foreshadow_id, json_payload())
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/dashboard/ai/foreshadows/<int:foreshadow_id>")
    def delete_foreshadow(foreshadow_id: int):
        try:
            service.delete_foreshadow(foreshadow_id)
            return ok()
        except Exception as exc:
            return fail(exc)

    # ── 语义检索 ───────────────────────────────────────────────

    @app.post("/api/dashboard/ai/projects/<int:project_id>/chapters/<int:chapter_id>/index")
    def index_chapter_retrieval(project_id: int, chapter_id: int):
        try:
            service.index_chapter_for_retrieval(project_id, chapter_id)
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/projects/<int:project_id>/search")
    def search_project(project_id: int):
        try:
            query = str(request.args.get("q", "") or "").strip()
            if not query:
                raise AIServiceError("搜索关键词不能为空")
            top_k = parse_int(request.args.get("top_k"), 5, "top_k", min_value=1, max_value=20)
            return ok(service.search_project_context(project_id, query, top_k=top_k))
        except Exception as exc:
            return fail(exc)

    # ── 创作向导多轮对话 ─────────────────────────────────────────────

    @app.get("/api/dashboard/ai/chat/sessions")
    def list_chat_sessions_api():
        try:
            scope = request.args.get("scope") or None
            status = request.args.get("status") or None
            return ok(service.list_chat_sessions(scope=scope, status=status))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/chat/sessions")
    def create_chat_session_api():
        try:
            sid = service.create_chat_session(json_payload())
            return ok({"id": sid})
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/chat/sessions/<int:session_id>")
    def get_chat_session_api(session_id: int):
        try:
            return ok(service.get_chat_session(session_id, with_messages=True))
        except Exception as exc:
            return fail(exc)

    @app.put("/api/dashboard/ai/chat/sessions/<int:session_id>")
    def update_chat_session_api(session_id: int):
        try:
            service.update_chat_session(session_id, json_payload())
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/dashboard/ai/chat/sessions/<int:session_id>")
    def delete_chat_session_api(session_id: int):
        try:
            service.delete_chat_session(session_id)
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/chat/stream")
    def chat_stream_api():
        try:
            return stream_response(service.stream_chat(json_payload()))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/chat/sessions/<int:session_id>/preview")
    def preview_wizard_session_api(session_id: int):
        try:
            return ok(service.parse_wizard_session(session_id))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/chat/sessions/<int:session_id>/import-to-project")
    def import_wizard_to_project_api(session_id: int):
        try:
            payload = json_payload()
            project_id = service.import_wizard_session(
                session_id,
                mode=payload.get("mode") or "create",
                target_project_id=payload.get("target_project_id"),
                overwrite_fields=payload.get("overwrite_fields") or [],
            )
            return ok({"project_id": project_id})
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/chat/sessions/<int:session_id>/import-raw-to-project")
    def import_wizard_raw_to_project_api(session_id: int):
        try:
            project_id = service.import_wizard_output(session_id, json_payload())
            return ok({"project_id": project_id})
        except Exception as exc:
            return fail(exc)

    # ── 章节 Pipeline + 摘要/伏笔/润色/聚合面板 ──────────────────────

    @app.post("/api/dashboard/ai/chapters/pipeline/stream")
    def chapter_pipeline_stream_api():
        try:
            return stream_response(service.stream_chapter_pipeline(json_payload()))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/chapters/pipeline/batch/stream")
    def chapter_pipeline_batch_stream_api():
        try:
            return stream_response(service.stream_chapters_pipeline(json_payload()))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/chapters/extract-summary/stream")
    def chapter_extract_summary_stream_api():
        try:
            return stream_response(service.stream_extract_chapter_summary(json_payload()))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/chapters/polish/stream")
    def chapter_polish_stream_api():
        try:
            return stream_response(service.stream_polish(json_payload()))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/projects/<int:project_id>/foreshadows/auto-resolve/stream")
    def auto_resolve_foreshadows_stream_api(project_id: int):
        try:
            payload = {**json_payload(), "project_id": project_id}
            return stream_response(service.stream_auto_resolve_foreshadows(payload))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/projects/<int:project_id>/foreshadows/auto-resolve/import-output")
    def import_foreshadow_resolution_output_api(project_id: int):
        try:
            return ok(service.import_foreshadow_resolution_output(project_id, json_payload()))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/chapters/<int:chapter_id>/dashboard")
    def chapter_dashboard_api(chapter_id: int):
        try:
            return ok(service.get_chapter_dashboard(chapter_id))
        except Exception as exc:
            return fail(exc)
