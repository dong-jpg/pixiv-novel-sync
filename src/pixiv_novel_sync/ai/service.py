from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..storage_db import Database
from .chunking import estimate_token_count, get_tail_context, split_text_by_chars
from .crypto import AISecretManager
from .models import AIAgentConfig, AIProviderConfig, AIStreamChunk
from .prompts import (
    build_audit_messages,
    build_continue_messages,
    build_novel_distill_messages,
    build_rewrite_messages,
    build_style_distill_messages,
    build_summarize_messages,
)
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
            smart = bool(payload.get("smart_context", True))
            context_chars = int(payload.get("context_chars") or agent.context_window)
            if smart:
                model = agent.model or provider_config.default_model
                if model:
                    context = self._smart_context(context, context_chars, provider_config, model)
                else:
                    context = get_tail_context(context, context_chars)
            else:
                context = get_tail_context(context, context_chars)
            messages = build_continue_messages(
                system_prompt=agent.system_prompt,
                context=context,
                instruction=payload.get("instruction"),
                output_chars=payload.get("output_chars"),
                style_prompt=payload.get("style_prompt"),
                novel_prompt=payload.get("novel_prompt"),
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
        if data.get("task_type") not in {None, "continue", "rewrite", "distill_style", "distill_novel", "audit", "general"}:
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

    # ── 任务历史 ────────────────────────────────────────────────

    def list_jobs(self, task_type: str | None = None, status: str | None = None,
                  page: int = 1, page_size: int = 20) -> dict[str, Any]:
        db = self._db()
        try:
            return db.list_ai_jobs(task_type=task_type, status=status, page=page, page_size=page_size)
        finally:
            db.close()

    def get_job(self, job_id: str) -> dict[str, Any]:
        db = self._db()
        try:
            job = db.get_ai_job(job_id)
            if not job:
                raise AIServiceError("任务不存在")
            return job
        finally:
            db.close()

    # ── 风格蒸馏 ────────────────────────────────────────────────

    def stream_distill_style(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        db = self._db()
        job_id = uuid.uuid4().hex
        output_parts: list[str] = []
        job_created = False
        agent_id = int(payload.get("agent_id") or 0)
        try:
            agent = self._load_agent_config(db, agent_id)
            provider_config = self._load_provider_config(db, agent.provider_id)
            text = self._resolve_input_text(db, payload)
            chunks = split_text_by_chars(text, int(payload.get("chunk_chars") or 4000))
            existing_profile = None
            if payload.get("existing_profile_id"):
                existing_profile = db.get_ai_style_profile(int(payload["existing_profile_id"]))
                if existing_profile:
                    existing_profile = existing_profile.get("profile")
            messages = build_style_distill_messages(
                system_prompt=agent.system_prompt,
                text_chunks=chunks,
                existing_profile=existing_profile,
            )
            db.create_ai_job(job_id, "distill_style", agent.id, {**payload, "chunks_count": len(chunks)})
            job_created = True
            yield AIStreamChunk(type="metadata", data={"job_id": job_id})
            model = agent.model or provider_config.default_model
            if not model:
                raise AIServiceError("Agent 或 Provider 未配置模型")
            provider = create_provider(provider_config)
            for chunk in provider.stream_generate(
                messages, model=model, temperature=agent.temperature,
                top_p=agent.top_p, max_tokens=agent.max_tokens,
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

    def save_style_profile(self, payload: dict[str, Any]) -> int:
        db = self._db()
        try:
            return db.create_ai_style_profile(payload)
        finally:
            db.close()

    def list_style_profiles(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        db = self._db()
        try:
            return db.list_ai_style_profiles(page=page, page_size=page_size)
        finally:
            db.close()

    def get_style_profile(self, profile_id: int) -> dict[str, Any]:
        db = self._db()
        try:
            profile = db.get_ai_style_profile(profile_id)
            if not profile:
                raise AIServiceError("风格档案不存在")
            return profile
        finally:
            db.close()

    def update_style_profile(self, profile_id: int, payload: dict[str, Any]) -> None:
        db = self._db()
        try:
            db.update_ai_style_profile(profile_id, payload)
        finally:
            db.close()

    def delete_style_profile(self, profile_id: int) -> None:
        db = self._db()
        try:
            db.delete_ai_style_profile(profile_id)
        finally:
            db.close()

    # ── 小说蒸馏 ────────────────────────────────────────────────

    def stream_distill_novel(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        db = self._db()
        job_id = uuid.uuid4().hex
        output_parts: list[str] = []
        job_created = False
        agent_id = int(payload.get("agent_id") or 0)
        try:
            agent = self._load_agent_config(db, agent_id)
            provider_config = self._load_provider_config(db, agent.provider_id)
            text = self._resolve_input_text(db, payload)
            chunks = split_text_by_chars(text, int(payload.get("chunk_chars") or 4000))
            existing_profile = None
            if payload.get("existing_profile_id"):
                existing_profile = db.get_ai_novel_profile(int(payload["existing_profile_id"]))
                if existing_profile:
                    existing_profile = existing_profile.get("profile")
            messages = build_novel_distill_messages(
                system_prompt=agent.system_prompt,
                text_chunks=chunks,
                existing_profile=existing_profile,
            )
            db.create_ai_job(job_id, "distill_novel", agent.id, {**payload, "chunks_count": len(chunks)})
            job_created = True
            yield AIStreamChunk(type="metadata", data={"job_id": job_id})
            model = agent.model or provider_config.default_model
            if not model:
                raise AIServiceError("Agent 或 Provider 未配置模型")
            provider = create_provider(provider_config)
            for chunk in provider.stream_generate(
                messages, model=model, temperature=agent.temperature,
                top_p=agent.top_p, max_tokens=agent.max_tokens,
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

    def save_novel_profile(self, payload: dict[str, Any]) -> int:
        db = self._db()
        try:
            return db.create_ai_novel_profile(payload)
        finally:
            db.close()

    def list_novel_profiles(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        db = self._db()
        try:
            return db.list_ai_novel_profiles(page=page, page_size=page_size)
        finally:
            db.close()

    def get_novel_profile(self, profile_id: int) -> dict[str, Any]:
        db = self._db()
        try:
            profile = db.get_ai_novel_profile(profile_id)
            if not profile:
                raise AIServiceError("小说档案不存在")
            return profile
        finally:
            db.close()

    def update_novel_profile(self, profile_id: int, payload: dict[str, Any]) -> None:
        db = self._db()
        try:
            db.update_ai_novel_profile(profile_id, payload)
        finally:
            db.close()

    def delete_novel_profile(self, profile_id: int) -> None:
        db = self._db()
        try:
            db.delete_ai_novel_profile(profile_id)
        finally:
            db.close()

    # ── 内容审计 ────────────────────────────────────────────────

    def stream_audit(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        db = self._db()
        job_id = uuid.uuid4().hex
        output_parts: list[str] = []
        job_created = False
        agent_id = int(payload.get("agent_id") or 0)
        try:
            agent = self._load_agent_config(db, agent_id)
            provider_config = self._load_provider_config(db, agent.provider_id)
            text = self._resolve_input_text(db, payload)
            messages = build_audit_messages(
                system_prompt=agent.system_prompt,
                text=text,
                audit_dimensions=payload.get("audit_dimensions"),
            )
            db.create_ai_job(job_id, "audit", agent.id, {**payload, "text_chars": len(text)})
            job_created = True
            yield AIStreamChunk(type="metadata", data={"job_id": job_id})
            model = agent.model or provider_config.default_model
            if not model:
                raise AIServiceError("Agent 或 Provider 未配置模型")
            provider = create_provider(provider_config)
            for chunk in provider.stream_generate(
                messages, model=model, temperature=agent.temperature,
                top_p=agent.top_p, max_tokens=agent.max_tokens,
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

    # ── Prompt 模板 ─────────────────────────────────────────────

    def list_prompt_templates(self, category: str | None = None) -> list[dict[str, Any]]:
        db = self._db()
        try:
            return db.list_ai_prompt_templates(category=category)
        finally:
            db.close()

    def get_prompt_template(self, template_id: int) -> dict[str, Any]:
        db = self._db()
        try:
            template = db.get_ai_prompt_template(template_id)
            if not template:
                raise AIServiceError("Prompt 模板不存在")
            return template
        finally:
            db.close()

    def create_prompt_template(self, payload: dict[str, Any]) -> int:
        name = str(payload.get("name") or "").strip()
        template = str(payload.get("template") or "").strip()
        if not name:
            raise AIServiceError("模板名称不能为空")
        if not template:
            raise AIServiceError("模板内容不能为空")
        db = self._db()
        try:
            return db.create_ai_prompt_template(payload)
        finally:
            db.close()

    def update_prompt_template(self, template_id: int, payload: dict[str, Any]) -> None:
        db = self._db()
        try:
            existing = db.get_ai_prompt_template(template_id)
            if not existing:
                raise AIServiceError("Prompt 模板不存在")
            if existing.get("is_builtin") and not payload.get("force"):
                raise AIServiceError("内置模板不可修改，如需自定义请复制后修改")
            db.update_ai_prompt_template(template_id, payload)
        finally:
            db.close()

    def delete_prompt_template(self, template_id: int) -> None:
        db = self._db()
        try:
            existing = db.get_ai_prompt_template(template_id)
            if not existing:
                raise AIServiceError("Prompt 模板不存在")
            if existing.get("is_builtin"):
                raise AIServiceError("内置模板不可删除")
            db.delete_ai_prompt_template(template_id)
        finally:
            db.close()

    def seed_builtin_templates(self) -> None:
        """初始化内置 Prompt 模板（幂等，已存在则跳过）。"""
        templates = [
            {"name": "续写-默认", "category": "continue", "template": "你是专业中文小说续写助手。\n你的任务是根据用户提供的上下文继续写正文。\n规则：\n1. 你要续写，不要总结，不要解释。\n2. 保持人物设定、叙述视角、语气和文风。\n3. 不要突然跳剧情，不要随意引入新角色或重大设定。\n4. 不要输出标题、列表、分析或写作说明。\n5. 只输出续写后的小说正文。", "description": "标准续写 prompt", "is_builtin": True},
            {"name": "续写-心理描写", "category": "continue", "template": "你是专业中文小说续写助手，擅长细腻的心理描写。\n你的任务是根据用户提供的上下文继续写正文。\n规则：\n1. 重点描写角色的内心活动、情感变化和心理冲突。\n2. 保持人物设定、叙述视角、语气和文风。\n3. 不要突然跳剧情。\n4. 只输出续写后的小说正文。", "description": "侧重心理描写的续写 prompt", "is_builtin": True},
            {"name": "改写-润色", "category": "rewrite", "template": "你是专业中文小说改写助手。\n你的任务是润色文本，提升文学质量。\n规则：\n1. 保留原剧情事实和关键信息。\n2. 优化用词和句式，提升文学性。\n3. 不新增重大事件，不删除关键情节。\n4. 只输出改写后的正文。", "description": "标准润色 prompt", "is_builtin": True},
            {"name": "改写-去AI味", "category": "rewrite", "template": "你是专业中文小说改写助手。\n你的任务是改写文本，去除 AI 生成的痕迹。\n规则：\n1. 保留原剧情事实和关键信息。\n2. 去除模板化、套路化的表达。\n3. 增加自然的口语化表达和个性化描写。\n4. 避免过度使用排比、比喻等修辞。\n5. 只输出改写后的正文。", "description": "去除 AI 痕迹的改写 prompt", "is_builtin": True},
            {"name": "审计-全面审查", "category": "audit", "template": "你是专业的小说内容审计专家。\n请从角色一致性、剧情连贯性、文风统一性、伏笔追踪、节奏把控、对话质量、描写质量七个维度进行审查。\n每个维度给出评分（1-10）和具体意见。", "description": "全面内容审计 prompt", "is_builtin": True},
            {"name": "蒸馏-风格提取", "category": "distill", "template": "你是专业的文学风格分析专家。\n请从叙事视角、语气特征、句式特点、用词风格、描写手法、对话风格、节奏特征、常用修辞手法等维度提取写作风格特征。", "description": "风格蒸馏 prompt", "is_builtin": True},
            {"name": "蒸馏-小说设定提取", "category": "distill", "template": "你是专业的小说结构分析专家。\n请提取角色列表及关系、世界观设定、关键剧情点、伏笔列表、时间线、主题与情感基调。", "description": "小说蒸馏 prompt", "is_builtin": True},
            {"name": "摘要提取", "category": "summarize", "template": "你是专业的小说文本摘要提取助手。\n请保留主要角色当前状态、正在进行的剧情线、最近发生的重要事件、已埋伏笔、情感氛围、时间地点信息。\n摘要控制在原文 10%-20% 篇幅。", "description": "长文本摘要提取 prompt", "is_builtin": True},
        ]
        db = self._db()
        try:
            existing = db.list_ai_prompt_templates()
            existing_names = {t["name"] for t in existing}
            for t in templates:
                if t["name"] not in existing_names:
                    db.create_ai_prompt_template(t)
        finally:
            db.close()

    # ── 草稿版本历史 ────────────────────────────────────────────

    def get_draft_history(self, draft_id: int) -> list[dict[str, Any]]:
        db = self._db()
        try:
            draft = db.get_ai_draft(draft_id)
            if not draft:
                raise AIServiceError("草稿不存在")
            return db.get_ai_draft_history(draft_id)
        finally:
            db.close()

    def fork_draft(self, draft_id: int, payload: dict[str, Any]) -> int:
        db = self._db()
        try:
            original = db.get_ai_draft(draft_id)
            if not original:
                raise AIServiceError("原草稿不存在")
            new_content = str(payload.get("content") or original.get("content", ""))
            new_title = str(payload.get("title") or f"{original.get('title', '未命名')} - 新版本")
            return db.create_ai_draft({
                "title": new_title,
                "content": new_content,
                "parent_draft_id": draft_id,
                "source_job_id": original.get("source_job_id"),
                "style_profile_id": original.get("style_profile_id"),
                "novel_profile_id": original.get("novel_profile_id"),
            })
        finally:
            db.close()

    # ── 长文本智能处理 ──────────────────────────────────────────

    def _smart_context(self, text: str, context_window: int,
                       provider_config: AIProviderConfig, model: str) -> str:
        """智能上下文处理：超长时自动提取摘要 + 末尾上下文。"""
        est_tokens = estimate_token_count(text)
        max_tokens = context_window * 0.6  # 留 40% 给输出和 prompt
        if est_tokens <= max_tokens:
            return text
        # 需要摘要：取前文做摘要 + 末尾上下文
        tail_chars = int(context_window * 0.4)
        tail = get_tail_context(text, tail_chars)
        head = text[:len(text) - len(tail)]
        if not head.strip():
            return tail
        # 同步调用摘要
        messages = build_summarize_messages(text=head[:8000])  # 限制摘要输入
        summary_parts: list[str] = []
        provider = create_provider(provider_config)
        for chunk in provider.stream_generate(messages, model=model, temperature=0.3, top_p=0.9, max_tokens=1000):
            if chunk.type == "delta":
                summary_parts.append(chunk.text)
        summary = "".join(summary_parts).strip()
        if summary:
            return f"【前文摘要】\n{summary}\n\n【最近原文】\n{tail}"
        return tail
