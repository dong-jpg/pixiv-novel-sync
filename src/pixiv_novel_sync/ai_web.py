from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from .ai.service import AIServiceError, AIWritingService
from .ai.detection import detect_ai_tells
from .settings import Settings


def register_ai_routes(app: Flask, settings: Settings) -> None:
    service = AIWritingService(settings.storage.db_path)

    def json_payload() -> dict[str, Any]:
        payload = request.get_json(silent=True)
        return payload if isinstance(payload, dict) else {}

    def ok(data: Any = None, **extra: Any):
        body = {"ok": True, **extra}
        if data is not None:
            body["data"] = data
        return jsonify(body)

    def fail(exc: Exception, status: int = 400):
        return jsonify({"ok": False, "error": str(exc)}), status

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

    def stream_response(chunks: Iterator) -> Response:
        def drain_remaining() -> None:
            try:
                for _chunk in chunks:
                    pass
            except Exception:
                pass

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
                threading.Thread(target=drain_remaining, daemon=True).start()
                raise

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/dashboard/ai")
    def dashboard_ai_page():
        return render_template("dashboard_ai.html")

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

    # ── 任务历史 ────────────────────────────────────────────────

    @app.get("/api/dashboard/ai/jobs")
    def list_ai_jobs():
        try:
            task_type = request.args.get("task_type") or None
            status = request.args.get("status") or None
            page = parse_int(request.args.get("page"), 1, "page", min_value=1)
            page_size = parse_int(request.args.get("page_size"), 20, "page_size", min_value=1, max_value=200)
            return ok(service.list_jobs(task_type=task_type, status=status, page=page, page_size=page_size))
        except Exception as exc:
            return fail(exc)

    @app.get("/api/dashboard/ai/jobs/<job_id>")
    def get_ai_job(job_id: str):
        try:
            return ok(service.get_job(job_id))
        except Exception as exc:
            return fail(exc)

    @app.post("/api/dashboard/ai/jobs/cleanup")
    def cleanup_ai_jobs():
        try:
            payload = json_payload()
            keep_days = parse_int(payload.get("keep_days"), 30, "keep_days", min_value=1)
            keep_failed_days = payload.get("keep_failed_days")
            if keep_failed_days is not None:
                keep_failed_days = parse_int(keep_failed_days, 0, "keep_failed_days", min_value=1)
            deleted = service.cleanup_jobs(keep_days=keep_days, keep_failed_days=keep_failed_days)
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
            from .settings import Settings
            current_settings = settings
            db = Database(current_settings.storage.db_path)
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
            service.update_writing_project(project_id, json_payload())
            return ok()
        except Exception as exc:
            return fail(exc)

    @app.delete("/api/dashboard/ai/projects/<int:project_id>")
    def delete_writing_project(project_id: int):
        try:
            service.delete_writing_project(project_id)
            return ok()
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
