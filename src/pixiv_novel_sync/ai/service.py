from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..storage_db import Database
from .chunking import get_tail_context
from .crypto import AISecretManager
from .models import AIAgentConfig, AIProviderConfig, AIStreamChunk
from .prompts import build_continue_messages, build_rewrite_messages
from .providers import create_provider


class AIServiceError(RuntimeError):
    pass


class AIWritingService:
    def __init__(self, db_path: Path, secret_manager: AISecretManager | None = None) -> None:
        self.db_path = db_path
        self.secret_manager = secret_manager or AISecretManager()

    def _db(self) -> Database:
        db = Database(self.db_path)
        db.init_schema()
        return db

    def list_providers(self) -> list[dict[str, Any]]:
        db = self._db()
        try:
            return db.list_ai_providers()
        finally:
            db.close()

    def create_provider(self, payload: dict[str, Any]) -> int:
        data = self._normalize_provider_payload(payload, require_key=bool(payload.get("api_key")))
        db = self._db()
        try:
            return db.create_ai_provider(data)
        finally:
            db.close()

    def update_provider(self, provider_id: int, payload: dict[str, Any]) -> None:
        data = self._normalize_provider_payload(payload, require_key=False, partial=True)
        db = self._db()
        try:
            db.update_ai_provider(provider_id, data)
        finally:
            db.close()

    def delete_provider(self, provider_id: int) -> None:
        db = self._db()
        try:
            db.delete_ai_provider(provider_id)
        finally:
            db.close()

    def test_provider(self, provider_id: int) -> dict[str, Any]:
        db = self._db()
        try:
            provider_config = self._load_provider_config(db, provider_id)
        finally:
            db.close()
        model = provider_config.default_model
        if not model:
            raise AIServiceError("Provider 未配置默认模型")
        provider = create_provider(provider_config)
        started = time.time()
        text_parts: list[str] = []
        for chunk in provider.stream_generate(
            [{"role": "user", "content": "请只回复 OK。"}],
            model=model,
            temperature=0,
            top_p=1,
            max_tokens=32,
        ):
            if chunk.type == "delta":
                text_parts.append(chunk.text)
        return {"ok": True, "model": model, "latency_ms": int((time.time() - started) * 1000), "text": "".join(text_parts).strip()[:100]}

    def list_agents(self) -> list[dict[str, Any]]:
        db = self._db()
        try:
            return db.list_ai_agents()
        finally:
            db.close()

    def create_agent(self, payload: dict[str, Any]) -> int:
        data = self._normalize_agent_payload(payload)
        db = self._db()
        try:
            return db.create_ai_agent(data)
        finally:
            db.close()

    def update_agent(self, agent_id: int, payload: dict[str, Any]) -> None:
        data = self._normalize_agent_payload(payload, partial=True)
        db = self._db()
        try:
            db.update_ai_agent(agent_id, data)
        finally:
            db.close()

    def delete_agent(self, agent_id: int) -> None:
        db = self._db()
        try:
            db.delete_ai_agent(agent_id)
        finally:
            db.close()

    def create_document(self, payload: dict[str, Any]) -> int:
        content = str(payload.get("content") or "")
        if not content.strip():
            raise AIServiceError("文档内容不能为空")
        data = {
            "title": str(payload.get("title") or "未命名文档")[:200],
            "source_type": str(payload.get("source_type") or "manual"),
            "content": content,
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "metadata": payload.get("metadata") or {},
        }
        db = self._db()
        try:
            return db.create_ai_document(data)
        finally:
            db.close()

    def stream_continue(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        db = self._db()
        job_id = uuid.uuid4().hex
        output_parts: list[str] = []
        job_created = False
        agent_id = int(payload.get("agent_id") or 0)
        try:
            agent = self._load_agent_config(db, agent_id)
            provider_config = self._load_provider_config(db, agent.provider_id)
            context = self._resolve_input_text(db, payload)
            context = get_tail_context(context, int(payload.get("context_chars") or agent.context_window))
            messages = build_continue_messages(
                system_prompt=agent.system_prompt,
                context=context,
                instruction=payload.get("instruction"),
                output_chars=payload.get("output_chars"),
            )
            db.create_ai_job(job_id, "continue", agent.id, {**payload, "resolved_context_chars": len(context)})
            job_created = True
            yield AIStreamChunk(type="metadata", data={"job_id": job_id})
            model = agent.model or provider_config.default_model
            if not model:
                raise AIServiceError("Agent 或 Provider 未配置模型")
            provider = create_provider(provider_config)
            for chunk in provider.stream_generate(
                messages,
                model=model,
                temperature=agent.temperature,
                top_p=agent.top_p,
                max_tokens=agent.max_tokens,
            ):
                if chunk.type == "delta":
                    output_parts.append(chunk.text)
                    yield chunk
            output = "".join(output_parts)
            db.update_ai_job(job_id, "succeeded", output_text=output, output_json={"chars": len(output)})
            yield AIStreamChunk(type="done", data={"job_id": job_id, "chars": len(output)})
        except GeneratorExit:
            if job_created:
                db.update_ai_job(job_id, "cancelled", output_text="".join(output_parts), error_message="客户端断开连接")
            raise
        except Exception as exc:
            message = str(exc)
            if job_created:
                db.update_ai_job(job_id, "failed", output_text="".join(output_parts), error_message=message)
            yield AIStreamChunk(type="error", data={"message": message})
        finally:
            db.close()

    def stream_rewrite(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        db = self._db()
        job_id = uuid.uuid4().hex
        output_parts: list[str] = []
        job_created = False
        agent_id = int(payload.get("agent_id") or 0)
        try:
            agent = self._load_agent_config(db, agent_id)
            provider_config = self._load_provider_config(db, agent.provider_id)
            text = self._resolve_input_text(db, payload)
            messages = build_rewrite_messages(
                system_prompt=agent.system_prompt,
                text=text,
                rewrite_type=payload.get("rewrite_type"),
                instruction=payload.get("instruction"),
            )
            db.create_ai_job(job_id, "rewrite", agent.id, {**payload, "resolved_text_chars": len(text)})
            job_created = True
            yield AIStreamChunk(type="metadata", data={"job_id": job_id})
            model = agent.model or provider_config.default_model
            if not model:
                raise AIServiceError("Agent 或 Provider 未配置模型")
            provider = create_provider(provider_config)
            for chunk in provider.stream_generate(
                messages,
                model=model,
                temperature=agent.temperature,
                top_p=agent.top_p,
                max_tokens=agent.max_tokens,
            ):
                if chunk.type == "delta":
                    output_parts.append(chunk.text)
                    yield chunk
            output = "".join(output_parts)
            db.update_ai_job(job_id, "succeeded", output_text=output, output_json={"chars": len(output)})
            yield AIStreamChunk(type="done", data={"job_id": job_id, "chars": len(output)})
        except GeneratorExit:
            if job_created:
                db.update_ai_job(job_id, "cancelled", output_text="".join(output_parts), error_message="客户端断开连接")
            raise
        except Exception as exc:
            message = str(exc)
            if job_created:
                db.update_ai_job(job_id, "failed", output_text="".join(output_parts), error_message=message)
            yield AIStreamChunk(type="error", data={"message": message})
        finally:
            db.close()

    def list_drafts(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        db = self._db()
        try:
            return db.list_ai_drafts(page=page, page_size=page_size)
        finally:
            db.close()

    def create_draft(self, payload: dict[str, Any]) -> int:
        title = str(payload.get("title") or "未命名草稿")[:200]
        content = str(payload.get("content") or "")
        if not content.strip():
            raise AIServiceError("草稿内容不能为空")
        db = self._db()
        try:
            return db.create_ai_draft({**payload, "title": title, "content": content})
        finally:
            db.close()

    def update_draft(self, draft_id: int, payload: dict[str, Any]) -> None:
        db = self._db()
        try:
            db.update_ai_draft(draft_id, payload)
        finally:
            db.close()

    def delete_draft(self, draft_id: int) -> None:
        db = self._db()
        try:
            db.delete_ai_draft(draft_id)
        finally:
            db.close()

    def _normalize_provider_payload(self, payload: dict[str, Any], require_key: bool = False, partial: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {}
        keys = ["name", "provider_type", "base_url", "default_model", "available_models", "timeout_seconds", "max_retries", "proxy", "enabled"]
        for key in keys:
            if key in payload:
                data[key] = payload[key]
        if not partial:
            for key in ("name", "provider_type"):
                if not data.get(key):
                    raise AIServiceError(f"缺少 Provider 字段：{key}")
        if data.get("provider_type") not in {None, "openai_compatible", "anthropic", "xai"}:
            raise AIServiceError("不支持的 Provider 类型")
        api_key = str(payload.get("api_key") or "")
        if api_key:
            data["api_key_encrypted"] = self.secret_manager.encrypt(api_key)
        elif require_key:
            raise AIServiceError("缺少 API key")
        return data

    def _normalize_agent_payload(self, payload: dict[str, Any], partial: bool = False) -> dict[str, Any]:
        data = {key: payload[key] for key in ("name", "task_type", "provider_id", "model", "system_prompt", "temperature", "top_p", "max_tokens", "context_window", "enabled") if key in payload}
        if not partial:
            for key in ("name", "task_type", "provider_id", "system_prompt"):
                if not data.get(key):
                    raise AIServiceError(f"缺少 Agent 字段：{key}")
        if data.get("task_type") not in {None, "continue", "rewrite", "distill_style", "distill_novel", "general"}:
            raise AIServiceError("不支持的 Agent 类型")
        return data

    def _load_provider_config(self, db: Database, provider_id: int) -> AIProviderConfig:
        row = db.get_ai_provider(provider_id, include_secret=True)
        if not row:
            raise AIServiceError("Provider 不存在")
        if not bool(row.get("enabled")):
            raise AIServiceError("Provider 已禁用")
        api_key = self.secret_manager.decrypt(row.get("api_key_encrypted"))
        return AIProviderConfig(
            id=int(row["id"]), name=row["name"], provider_type=row["provider_type"],
            base_url=row.get("base_url"), api_key=api_key, default_model=row.get("default_model"),
            timeout_seconds=int(row.get("timeout_seconds") or 120), max_retries=int(row.get("max_retries") or 2),
            proxy=row.get("proxy"), enabled=bool(row.get("enabled")),
        )

    def _load_agent_config(self, db: Database, agent_id: int) -> AIAgentConfig:
        row = db.get_ai_agent(agent_id)
        if not row:
            raise AIServiceError("Agent 不存在")
        if not bool(row.get("enabled")):
            raise AIServiceError("Agent 已禁用")
        return AIAgentConfig(
            id=int(row["id"]), name=row["name"], task_type=row["task_type"], provider_id=int(row["provider_id"]),
            model=row.get("model"), system_prompt=row["system_prompt"], temperature=float(row.get("temperature") or 0.8),
            top_p=float(row.get("top_p") or 0.9), max_tokens=int(row.get("max_tokens") or 4000),
            context_window=int(row.get("context_window") or 16000), enabled=bool(row.get("enabled")),
        )

    def _resolve_input_text(self, db: Database, payload: dict[str, Any]) -> str:
        source_type = payload.get("source_type") or "manual"
        if source_type == "archive_novel":
            novel_id = int(payload.get("novel_id") or 0)
            novel = db.get_novel_detail(novel_id)
            if not novel:
                raise AIServiceError("归档小说不存在")
            text = novel.get("text_raw") or novel.get("text_markdown") or ""
        elif source_type == "document":
            document_id = int(payload.get("document_id") or 0)
            document = db.get_ai_document(document_id)
            if not document:
                raise AIServiceError("AI 文档不存在")
            text = document.get("content") or ""
        else:
            text = str(payload.get("text") or "")
        if not text.strip():
            raise AIServiceError("输入文本不能为空")
        return text
