from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from .ai.service import AIServiceError, AIWritingService
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

    def sse(event: str, data: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def stream_response(chunks: Iterator) -> Response:
        def generate():
            for chunk in chunks:
                if chunk.type == "delta":
                    yield sse("delta", {"text": chunk.text})
                elif chunk.type == "metadata":
                    yield sse("metadata", chunk.data or {})
                elif chunk.type == "done":
                    yield sse("done", chunk.data or {})
                elif chunk.type == "error":
                    yield sse("error", chunk.data or {"message": "AI 任务失败"})

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
            page = int(request.args.get("page", 1))
            page_size = int(request.args.get("page_size", 20))
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
