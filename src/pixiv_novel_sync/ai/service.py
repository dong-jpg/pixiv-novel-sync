from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..storage_db import Database
from .chunking import estimate_token_count, get_tail_context, split_text_by_chars
from .crypto import AISecretManager
from .detection import detect_ai_tells
from .models import AIAgentConfig, AIProviderConfig, AIStreamChunk
from .prompts import (
    DEFAULT_WIZARD_PROMPT,
    build_audit_messages,
    build_chapter_summary_messages,
    build_chat_messages,
    build_continue_messages,
    build_foreshadow_resolve_messages,
    build_longform_detail_messages,
    build_longform_plan_messages,
    build_novel_distill_messages,
    build_plan_messages,
    build_polish_messages,
    build_rewrite_messages,
    build_style_distill_messages,
    build_summarize_messages,
    safe_prompt_preview,
)
from .providers import create_provider
from .retrieval import BaseRetriever, create_retriever


class AIServiceError(RuntimeError):
    pass


class AIWritingService:
    _schema_initialized: bool = False

    def __init__(self, db_path: Path, secret_manager: AISecretManager | None = None) -> None:
        self.db_path = db_path
        self.secret_manager = secret_manager or AISecretManager()
        self._retriever: BaseRetriever | None = None

    def _get_retriever(self) -> BaseRetriever:
        if self._retriever is None:
            self._retriever = create_retriever(self.db_path)
        return self._retriever

    def _db(self) -> Database:
        db = Database(self.db_path)
        if not AIWritingService._schema_initialized:
            db.init_schema()
            AIWritingService._schema_initialized = True
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
            db.create_ai_job(job_id, "continue", agent.id, {
                **payload,
                "input_context_chars": len(context),
                "smart_context": smart,
                "requested_context_chars": context_chars,
            })
            job_created = True
            yield AIStreamChunk(type="metadata", data={"job_id": job_id})
            if smart:
                model = agent.model or provider_config.default_model
                if model:
                    for item in self._smart_context(context, context_chars, provider_config, model):
                        if isinstance(item, AIStreamChunk):
                            yield item
                        else:
                            context = item
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
                plan_text=payload.get("plan_text"),
            )
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
        keys = ["name", "provider_type", "base_url", "default_model", "available_models", "timeout_seconds", "max_retries", "proxy", "context_window", "stream_enabled", "enabled"]
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
        if data.get("task_type") not in {None, "continue", "rewrite", "distill_style", "distill_novel", "audit", "general", "plan", "wizard", "chat", "extract_summary", "resolve_foreshadow", "polish_dialogue", "polish_psychology"}:
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
            proxy=row.get("proxy"), context_window=int(row.get("context_window") or 128000),
            stream_enabled=bool(row.get("stream_enabled", 1)),
            enabled=bool(row.get("enabled")),
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
        elif source_type == "archive_series":
            series_id = int(payload.get("series_id") or 0)
            if not series_id:
                raise AIServiceError("请选择系列")
            novels = db.list_series_novel_texts(series_id)
            if not novels:
                raise AIServiceError("系列下没有找到小说")
            parts: list[str] = []
            for r in novels:
                title = r.get("title", "")
                content = r.get("text_raw") or r.get("text_markdown") or ""
                if content.strip():
                    parts.append(f"{'=' * 40}\n【{title}】\n{'=' * 40}\n\n{content}")
            if not parts:
                raise AIServiceError("系列下的小说均无文本内容（可能尚未抓取正文）")
            text = "\n\n".join(parts)
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

    def cleanup_jobs(self, keep_days: int = 30, keep_failed_days: int | None = None) -> int:
        """清理超期的 ai_jobs 历史记录，返回删除条数。"""
        db = self._db()
        try:
            return db.cleanup_ai_jobs(keep_days=keep_days, keep_failed_days=keep_failed_days)
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
            chunk_char_size = int(payload.get("chunk_chars") or 4000)
            all_chunks = split_text_by_chars(text, chunk_char_size)
            full_text_mode = bool(payload.get("full_text", False))

            # 确定每批大小：优先使用用户指定的 batch_size，否则自动计算
            user_batch_size = int(payload.get("batch_size") or 0)
            if user_batch_size > 0:
                batch_size = user_batch_size
            else:
                effective_window = agent.context_window if agent.context_window > 16000 else provider_config.context_window
                usable_chars = int(effective_window * 1.5 * 0.7)
                batch_size = min(5, max(3, usable_chars // chunk_char_size))

            if not full_text_mode and len(all_chunks) > batch_size:
                # 采样模式：均匀取样
                step = len(all_chunks) // batch_size
                sampled = [all_chunks[i * step] for i in range(batch_size)]
                if sampled[-1] != all_chunks[-1]:
                    sampled[-1] = all_chunks[-1]
                batches = [sampled]
            elif full_text_mode and len(all_chunks) > batch_size:
                # 全文模式：分批 map-reduce
                batches = [all_chunks[i:i + batch_size] for i in range(0, len(all_chunks), batch_size)]
            else:
                batches = [all_chunks]

            existing_profile = None
            if payload.get("existing_profile_id"):
                existing_profile = db.get_ai_style_profile(int(payload["existing_profile_id"]))
                if existing_profile:
                    existing_profile = existing_profile.get("profile")

            db.create_ai_job(job_id, "distill_style", agent.id, {
                **payload, "chunks_count": len(all_chunks), "batches": len(batches), "mode": "full" if full_text_mode else "sample",
            })
            job_created = True
            yield AIStreamChunk(type="metadata", data={"job_id": job_id, "batches": len(batches)})

            model = agent.model or provider_config.default_model
            if not model:
                raise AIServiceError("Agent 或 Provider 未配置模型")
            provider = create_provider(provider_config)

            for batch_idx, batch_chunks in enumerate(batches):
                is_last = batch_idx == len(batches) - 1
                # 批次间间隔 2 秒，避免触发网关限流
                if batch_idx > 0:
                    time.sleep(2)
                messages = build_style_distill_messages(
                    system_prompt=agent.system_prompt,
                    text_chunks=batch_chunks,
                    existing_profile=existing_profile,
                )
                # 进度通知
                if len(batches) > 1:
                    progress_text = f"\n\n--- 正在分析第 {batch_idx + 1}/{len(batches)} 批 ---\n\n"
                    yield AIStreamChunk(type="delta", text=progress_text)
                    output_parts.append(progress_text)

                batch_output: list[str] = []
                for chunk in provider.stream_generate(
                    messages, model=model, temperature=agent.temperature,
                    top_p=agent.top_p, max_tokens=agent.max_tokens,
                ):
                    if chunk.type == "delta":
                        batch_output.append(chunk.text)
                        if is_last:
                            yield chunk

                batch_text = "".join(batch_output)
                if not is_last:
                    # 中间批次：用输出作为下一批的 existing_profile
                    existing_profile = batch_text
                    output_parts.append(f"[批次 {batch_idx + 1} 完成，{len(batch_text)} 字]\n")
                    yield AIStreamChunk(type="delta", text=f"[批次 {batch_idx + 1} 完成，{len(batch_text)} 字]\n")
                else:
                    output_parts.append(batch_text)

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
            chunk_char_size = int(payload.get("chunk_chars") or 4000)
            all_chunks = split_text_by_chars(text, chunk_char_size)
            full_text_mode = bool(payload.get("full_text", False))

            user_batch_size = int(payload.get("batch_size") or 0)
            if user_batch_size > 0:
                batch_size = user_batch_size
            else:
                effective_window = agent.context_window if agent.context_window > 16000 else provider_config.context_window
                usable_chars = int(effective_window * 1.5 * 0.8)
                batch_size = min(8, max(5, usable_chars // chunk_char_size))

            if not full_text_mode and len(all_chunks) > batch_size:
                step = len(all_chunks) // batch_size
                sampled = [all_chunks[i * step] for i in range(batch_size)]
                if sampled[-1] != all_chunks[-1]:
                    sampled[-1] = all_chunks[-1]
                batches = [sampled]
            elif full_text_mode and len(all_chunks) > batch_size:
                batches = [all_chunks[i:i + batch_size] for i in range(0, len(all_chunks), batch_size)]
            else:
                batches = [all_chunks]

            existing_profile = None
            if payload.get("existing_profile_id"):
                existing_profile = db.get_ai_novel_profile(int(payload["existing_profile_id"]))
                if existing_profile:
                    existing_profile = existing_profile.get("profile")

            db.create_ai_job(job_id, "distill_novel", agent.id, {
                **payload, "chunks_count": len(all_chunks), "batches": len(batches), "mode": "full" if full_text_mode else "sample",
            })
            job_created = True
            yield AIStreamChunk(type="metadata", data={"job_id": job_id, "batches": len(batches)})

            model = agent.model or provider_config.default_model
            if not model:
                raise AIServiceError("Agent 或 Provider 未配置模型")
            provider = create_provider(provider_config)

            for batch_idx, batch_chunks in enumerate(batches):
                is_last = batch_idx == len(batches) - 1
                if batch_idx > 0:
                    time.sleep(2)
                messages = build_novel_distill_messages(
                    system_prompt=agent.system_prompt,
                    text_chunks=batch_chunks,
                    existing_profile=existing_profile,
                )
                if len(batches) > 1:
                    progress_text = f"\n\n--- 正在分析第 {batch_idx + 1}/{len(batches)} 批 ---\n\n"
                    yield AIStreamChunk(type="delta", text=progress_text)
                    output_parts.append(progress_text)

                batch_output: list[str] = []
                for chunk in provider.stream_generate(
                    messages, model=model, temperature=agent.temperature,
                    top_p=agent.top_p, max_tokens=agent.max_tokens,
                ):
                    if chunk.type == "delta":
                        batch_output.append(chunk.text)
                        if is_last:
                            yield chunk

                batch_text = "".join(batch_output)
                if not is_last:
                    existing_profile = batch_text
                    output_parts.append(f"[批次 {batch_idx + 1} 完成，{len(batch_text)} 字]\n")
                    yield AIStreamChunk(type="delta", text=f"[批次 {batch_idx + 1} 完成，{len(batch_text)} 字]\n")
                else:
                    output_parts.append(batch_text)
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

            # P4: 先跑规则检测，把结果注入 LLM 审计 prompt
            rule_report = detect_ai_tells(text)
            rule_context = None
            if rule_report.issues:
                lines = [f"- [{i.severity}] {i.message}" + (f" ({i.detail})" if i.detail else "") for i in rule_report.issues]
                rule_context = (
                    f"【规则检测预分析 - AI痕迹得分 {rule_report.score:.0f}/100】\n"
                    + "\n".join(lines)
                    + "\n\n请在审计中参考以上规则检测结果，对 AI 痕迹维度给出更精确的评估。"
                )

            messages = build_audit_messages(
                system_prompt=agent.system_prompt,
                text=text,
                audit_dimensions=payload.get("audit_dimensions"),
                rule_detection_context=rule_context,
            )
            db.create_ai_job(job_id, "audit", agent.id, {
                **payload, "text_chars": len(text),
                "rule_score": rule_report.score, "rule_issues_count": len(rule_report.issues),
            })
            job_created = True
            yield AIStreamChunk(type="metadata", data={
                "job_id": job_id,
                "rule_detection": {"score": rule_report.score, "issues_count": len(rule_report.issues)},
            })
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

    # ── 写前构思 ────────────────────────────────────────────────

    def stream_plan(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        """生成续写前的章节构思。"""
        from .prompts import DEFAULT_PLAN_PROMPT
        db = self._db()
        job_id = uuid.uuid4().hex
        output_parts: list[str] = []
        job_created = False
        agent_id = int(payload.get("agent_id") or 0)
        try:
            agent = self._load_agent_config(db, agent_id)
            provider_config = self._load_provider_config(db, agent.provider_id)
            context = self._resolve_input_text(db, payload)
            # 构思任务只看最近的内容即可，不需要全文摘要
            context_chars = int(payload.get("context_chars") or 8000)
            context = get_tail_context(context, context_chars)
            # 如果 Agent 不是 plan 类型，强制使用构思专用 prompt
            system_prompt = agent.system_prompt
            if agent.task_type != "plan":
                system_prompt = DEFAULT_PLAN_PROMPT
            messages = build_plan_messages(
                system_prompt=system_prompt,
                context=context,
                instruction=payload.get("instruction"),
                novel_prompt=payload.get("novel_prompt"),
            )
            db.create_ai_job(job_id, "plan", agent.id, {**payload, "resolved_context_chars": len(context)})
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
            {"name": "改写-去AI味", "category": "rewrite", "template": "你是专业中文小说改写助手，专门去除AI生成痕迹。\n\n禁用词汇：仿佛、宛如、不禁、竟然、微微、轻轻、缓缓、深吸一口气、嘴角上扬、眼眸、心中暗道、似乎、好像（每段最多1次）、不由自主、若有所思\n\n句式要求：\n- 禁止连续3句以上用相同句式开头\n- 长短句交替，禁止排比句\n- 对话不要全部用\"XX说\"\n\n描写要求：\n- 禁止抽象描写，用具体细节\n- 每段至少1个感官细节\n- 对话要有信息量\n\n整体要求：像真人写的，允许不完美表达，情感要克制。", "description": "去除 AI 痕迹的改写 prompt（详细版）", "is_builtin": True},
            {"name": "审计-全面审查", "category": "audit", "template": "你是专业的小说内容审计专家。\n请从角色一致性、剧情连贯性、文风统一性、伏笔追踪、节奏把控、对话质量、描写质量七个维度进行审查。\n每个维度给出评分（1-10）和具体意见。", "description": "全面内容审计 prompt", "is_builtin": True},
            {"name": "蒸馏-风格提取", "category": "distill", "template": "你是专业的文学风格分析专家。\n请从叙事视角、语气特征、句式特点、用词风格、描写手法、对话风格、节奏特征、常用修辞手法等维度提取写作风格特征。", "description": "风格蒸馏 prompt", "is_builtin": True},
            {"name": "蒸馏-小说设定提取", "category": "distill", "template": "你是专业的小说结构分析专家。\n请提取角色列表及关系、世界观设定、关键剧情点、伏笔列表、时间线、主题与情感基调。", "description": "小说蒸馏 prompt", "is_builtin": True},
            {"name": "摘要提取", "category": "summarize", "template": "你是专业的小说文本摘要提取助手。\n请保留主要角色当前状态、正在进行的剧情线、最近发生的重要事件、已埋伏笔、情感氛围、时间地点信息。\n摘要控制在原文 10%-20% 篇幅。", "description": "长文本摘要提取 prompt", "is_builtin": True},
            {"name": "写前构思", "category": "plan", "template": "你是专业的小说创作总编。\n请根据已有上文，为接下来的续写制定章节构思，包含：本次目标、读者期待、该兑现的伏笔、暂不掀开的悬念、必须发生的改变、章尾钩子、不要做的事。", "description": "续写前的章节规划 prompt", "is_builtin": True},
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

    def seed_builtin_agents(self, provider_id: int) -> dict[str, int]:
        """初始化内置 Agent（幂等，同名则跳过）。返回 {name: id}。"""
        from .prompts import DEAI_RULES
        agents = [
            {
                "name": "通用续写助手",
                "task_type": "continue",
                "system_prompt": "你是专业中文小说续写助手。\n你的任务是根据用户提供的上下文继续写正文。\n\n规则：\n1. 你要续写，不要总结，不要解释。\n2. 保持人物设定、叙述视角、语气和文风。\n3. 不要突然跳剧情，不要随意引入新角色或重大设定。\n4. 不要输出标题、列表、分析或写作说明。\n5. 只输出续写后的小说正文。\n6. 续写自然流畅，像原作者的风格继续写下去。\n7. 注意保持伏笔的延续，不要忘记前文埋下的线索。",
                "temperature": 0.85,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "续写-心理描写专精",
                "task_type": "continue",
                "system_prompt": '你是专业中文小说续写助手，擅长细腻的心理描写。\n\n写作要求：\n1. 重点描写角色的内心活动、情感变化和心理冲突。\n2. 通过行为细节暗示心理，而非直接说"他很伤心"。\n3. 保持人物设定、叙述视角、语气和文风。\n4. 不要突然跳剧情。\n5. 只输出续写后的小说正文。\n\n心理描写技巧：\n- 用身体反应暗示情绪（手指攥紧、呼吸变浅、眼神躲闪）\n- 用环境映射心理（光线变暗暗示心情沉重）\n- 用内心独白展现纠结（但不要每段都有）\n- 克制表达，不要动不动就"热泪盈眶"',
                "temperature": 0.8,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "续写-对话专精",
                "task_type": "continue",
                "system_prompt": '你是专业中文小说续写助手，擅长写自然生动的对话。\n\n写作要求：\n1. 对话要符合角色身份、性格和说话习惯。\n2. 对话要有信息量，推进剧情或展现人物关系。\n3. 禁止废话对话（嗯、哦、好吧、你说得对）。\n4. 对话中穿插动作描写，不要全是XX说。\n5. 每个角色的说话方式要有区别。\n6. 只输出续写后的小说正文。\n\n对话技巧：\n- 潜台词：角色不会把所有想法都说出来\n- 打断：真实对话中经常有打断和插话\n- 省略：有时话说一半比说完更有力量\n- 口癖：给角色设计1-2个口头禅（但不要滥用）',
                "temperature": 0.85,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "通用改写助手",
                "task_type": "rewrite",
                "system_prompt": "你是专业中文小说改写助手。\n你的任务是按用户要求改写文本。\n\n规则：\n1. 保留原剧情事实和关键信息。\n2. 不新增重大事件，不删除关键情节。\n3. 按用户指定的改写目标调整表达。\n4. 不要解释修改过程。\n5. 只输出改写后的正文。",
                "temperature": 0.7,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "去AI味改写专家",
                "task_type": "rewrite",
                "system_prompt": f"你是专业中文小说改写助手，专门去除AI生成痕迹。\n\n{DEAI_RULES}\n\n改写原则：\n1. 保留原剧情、人物关系、关键信息不变。\n2. 不新增重大事件，不删除关键情节。\n3. 重点改造句式、用词、描写方式，让文本读起来像真人写的。\n4. 不要解释修改过程。\n5. 只输出改写后的正文。\n\n记住：你的目标是让文本通过AI检测工具，同时保持文学质量。",
                "temperature": 0.9,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "润色助手",
                "task_type": "rewrite",
                "system_prompt": "你是专业中文小说润色助手。\n你的任务是提升文本的文学质量，但不改变剧情。\n\n润色方向：\n1. 优化用词，替换平淡的动词和形容词。\n2. 改善句式，增加长短句变化。\n3. 增强画面感，添加感官细节。\n4. 优化节奏，该快则快该慢则慢。\n5. 保留原剧情和人物关系不变。\n6. 只输出润色后的正文。\n\n注意：润色不是重写，要尊重原文风格。",
                "temperature": 0.75,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "内容审计专家",
                "task_type": "audit",
                "system_prompt": "你是专业的小说内容审计专家。\n\n请从以下维度进行审查，每个维度给出评分（1-10）和具体意见：\n\n1. 角色一致性：角色行为是否符合其性格设定，有无前后矛盾\n2. 剧情连贯性：情节发展是否自然流畅，有无逻辑漏洞\n3. 文风统一性：叙述风格是否前后一致，有无突兀的风格转变\n4. 伏笔追踪：已埋伏笔是否有回收，有无遗漏的线索\n5. 节奏把控：叙事节奏是否合理，有无拖沓或过于仓促之处\n6. 对话质量：对话是否自然、有信息量、符合角色身份\n7. 描写质量：场景描写、心理描写是否生动有效\n\n输出格式为 JSON，包含 overall_score（总分）、各维度的 score 和 comments，以及 issues 列表（发现的具体问题）和 suggestions 列表（改进建议）。",
                "temperature": 0.3,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "风格蒸馏师",
                "task_type": "distill_style",
                "system_prompt": "你是专业的文学风格分析专家。\n\n请从以下维度提取写作风格特征：\n1. 叙事视角（第一人称/第三人称/上帝视角等）\n2. 语气特征（冷峻/温暖/幽默/严肃等）\n3. 句式特点（长短句比例、句式结构偏好）\n4. 用词风格（口语化/书面化/文言色彩等）\n5. 描写手法（白描/工笔/意识流等）\n6. 对话风格（简洁/冗长、方言使用、语气词频率）\n7. 节奏特征（紧凑/舒缓、段落长度分布）\n8. 常用修辞手法\n9. 标志性表达（作者常用的句式或词汇）\n\n输出 JSON 格式的风格档案。",
                "temperature": 0.4,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "小说设定提取师",
                "task_type": "distill_novel",
                "system_prompt": "你是专业的小说结构分析专家。\n\n请提取以下内容：\n1. 角色列表：每个角色的姓名、身份、性格特征、与其他角色的关系\n2. 世界观设定：时代背景、地点、社会环境、特殊设定\n3. 关键剧情点：已发生的重要事件及其影响\n4. 伏笔列表：已埋下但未回收的伏笔和悬念\n5. 时间线：按时间顺序排列的主要事件\n6. 主题与情感基调\n7. 势力/阵营关系\n\n输出 JSON 格式的小说档案。",
                "temperature": 0.4,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "全能写作助手",
                "task_type": "general",
                "system_prompt": "你是专业中文小说写作助手，可以完成续写、改写、润色、审计等多种任务。\n\n根据用户的具体要求灵活调整：\n- 续写时：保持原文风格和剧情连贯\n- 改写时：按用户指定方向调整，保留核心信息\n- 审计时：从多个维度分析文本质量\n- 蒸馏时：提取结构化的风格或设定信息\n\n始终以专业、认真的态度完成任务。",
                "temperature": 0.8,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "章节构思师",
                "task_type": "plan",
                "system_prompt": "你是专业的小说创作总编（不是写手），擅长在动笔前规划章节走向。\n你的任务是根据已有上文，为接下来的续写制定一份简洁清晰的章节构思。\n\n【输出结构 - 严格按照以下格式输出 Markdown，不要输出其他内容】\n\n## 本次目标\n（一句话说明本段续写要达到什么效果，≤ 50 字）\n\n## 读者此刻在等什么\n（基于上文，分析读者最期待看到的剧情走向，最多 3 点）\n\n## 该兑现的伏笔/线索\n（列出 1-3 条上文已埋下、本次应当推进或回收的线索）\n\n## 暂不掀开的\n（列出 1-2 条可继续埋藏的悬念）\n\n## 本次必须发生的改变\n（明确 1-3 条具体变化：信息/关系/物理/情感/力量变化，要可验证）\n\n## 章尾钩子\n（设计一个让读者想继续看下去的悬念点）\n\n## 不要做的事\n（针对本段具体内容，列出 2-4 条禁忌）\n\n【原则】\n- 构思必须基于上文事实，不要脱离已有剧情发明新设定\n- 每节内容用一两句话表达，不要长篇大论\n- 不要写正文，只写规划",
                "temperature": 0.6,
                "max_tokens": 2000,
                "context_window": 16000,
            },
            {
                "name": "创作向导",
                "task_type": "wizard",
                "system_prompt": DEFAULT_WIZARD_PROMPT,
                "temperature": 0.85,
                "max_tokens": 6000,
                "context_window": 32000,
            },
            {
                "name": "章节摘要师",
                "task_type": "extract_summary",
                "system_prompt": _SUMMARY_AGENT_PROMPT,
                "temperature": 0.3,
                "max_tokens": 2000,
                "context_window": 16000,
            },
            {
                "name": "伏笔追踪师",
                "task_type": "resolve_foreshadow",
                "system_prompt": _FORESHADOW_AGENT_PROMPT,
                "temperature": 0.2,
                "max_tokens": 2000,
                "context_window": 16000,
            },
            {
                "name": "对话润色师",
                "task_type": "polish_dialogue",
                "system_prompt": _POLISH_DIALOGUE_AGENT_PROMPT,
                "temperature": 0.75,
                "max_tokens": 6000,
                "context_window": 16000,
            },
            {
                "name": "心理描写润色师",
                "task_type": "polish_psychology",
                "system_prompt": _POLISH_PSYCHOLOGY_AGENT_PROMPT,
                "temperature": 0.75,
                "max_tokens": 6000,
                "context_window": 16000,
            },
        ]
        db = self._db()
        created: dict[str, int] = {}
        try:
            existing = db.list_ai_agents()
            existing_names = {a["name"] for a in existing}
            for a in agents:
                if a["name"] not in existing_names:
                    agent_id = db.create_ai_agent({**a, "provider_id": provider_id, "enabled": True})
                    created[a["name"]] = agent_id
                else:
                    for ea in existing:
                        if ea["name"] == a["name"]:
                            created[a["name"]] = ea["id"]
                            break
        finally:
            db.close()
        return created

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
                       provider_config: AIProviderConfig, model: str) -> Iterator[AIStreamChunk | str]:
        """智能上下文处理：超长时自动分段摘要 + 末尾上下文。

        作为生成器使用：yield AIStreamChunk(type="progress") 表示进度，
        最后 yield 一个 str 表示最终结果。调用方需迭代并区分类型。

        分层策略：
        - 短文本（<= 60% 窗口）：原样返回
        - 长文本：分段摘要前文 + 保留尾部 30% 字符作为续接锚点
        - 分段摘要：每 8000 字一段，避免长文摘要时丢失中段信息
        """
        est_tokens = estimate_token_count(text)
        max_tokens = context_window * 0.6  # 留 40% 给输出和 prompt
        if est_tokens <= max_tokens:
            yield text
            return
        # 保留尾部 30% 字符作为续接锚点（含最近的完整场景）
        tail_chars = int(context_window * 0.3)
        tail = get_tail_context(text, tail_chars)
        head = text[:len(text) - len(tail)]
        if not head.strip():
            yield tail
            return
        # 分段摘要：每 8000 字一段
        segment_size = 8000
        segments = [head[i:i + segment_size] for i in range(0, len(head), segment_size)]
        provider = create_provider(provider_config)
        summary_parts: list[str] = []
        for idx, seg in enumerate(segments, 1):
            yield AIStreamChunk(
                type="progress",
                data={"message": f"正在摘要前文（{idx}/{len(segments)}）...", "step": idx, "total": len(segments)},
            )
            messages = build_summarize_messages(
                text=seg,
                focus=f"第 {idx}/{len(segments)} 段，请保留与后续剧情衔接相关的关键信息。",
            )
            seg_summary: list[str] = []
            for chunk in provider.stream_generate(
                messages, model=model, temperature=0.3, top_p=0.9, max_tokens=800,
            ):
                if chunk.type == "delta":
                    seg_summary.append(chunk.text)
            seg_text = "".join(seg_summary).strip()
            if seg_text:
                if len(segments) > 1:
                    summary_parts.append(f"[第 {idx} 段摘要]\n{seg_text}")
                else:
                    summary_parts.append(seg_text)
        summary = "\n\n".join(summary_parts)
        if summary:
            yield f"【前文摘要】\n{summary}\n\n【最近原文】\n{tail}"
        else:
            yield tail

    # ══════════════════════════════════════════════════════════════
    # 写作项目 / 章节 / 伏笔 / 状态记忆
    # ══════════════════════════════════════════════════════════════

    # ── 项目 CRUD ──────────────────────────────────────────────────

    def list_writing_projects(self, status: str | None = None) -> list[dict[str, Any]]:
        db = self._db()
        try:
            return db.list_ai_writing_projects(status=status)
        finally:
            db.close()

    def get_writing_project(self, project_id: int) -> dict[str, Any]:
        db = self._db()
        try:
            project = db.get_ai_writing_project(project_id)
            if not project:
                raise AIServiceError("写作项目不存在")
            return project
        finally:
            db.close()

    def create_writing_project(self, payload: dict[str, Any]) -> int:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise AIServiceError("项目名称不能为空")
        db = self._db()
        try:
            return db.create_ai_writing_project(payload)
        finally:
            db.close()

    def update_writing_project(self, project_id: int, payload: dict[str, Any]) -> None:
        db = self._db()
        try:
            db.update_ai_writing_project(project_id, payload)
        finally:
            db.close()

    def delete_writing_project(self, project_id: int) -> None:
        retriever = self._get_retriever()
        retriever.delete_project(project_id)
        db = self._db()
        try:
            db.delete_ai_writing_project(project_id)
        finally:
            db.close()

    # ── 章节 CRUD ──────────────────────────────────────────────────

    def list_chapters(self, project_id: int) -> list[dict[str, Any]]:
        db = self._db()
        try:
            return db.list_ai_chapters(project_id)
        finally:
            db.close()

    def get_chapter(self, chapter_id: int) -> dict[str, Any]:
        db = self._db()
        try:
            chapter = db.get_ai_chapter(chapter_id)
            if not chapter:
                raise AIServiceError("章节不存在")
            return chapter
        finally:
            db.close()

    def create_chapter(self, payload: dict[str, Any]) -> int:
        project_id = int(payload.get("project_id") or 0)
        if not project_id:
            raise AIServiceError("缺少 project_id")
        db = self._db()
        try:
            if not db.get_ai_writing_project(project_id):
                raise AIServiceError("写作项目不存在")
            if "chapter_number" not in payload or not payload["chapter_number"]:
                payload["chapter_number"] = db.get_next_chapter_number(project_id)
            return db.create_ai_chapter(payload)
        finally:
            db.close()

    def create_chapters_from_plan(self, project_id: int, chapters: list[dict[str, Any]], mode: str = "missing_only") -> dict[str, Any]:
        if mode != "missing_only":
            raise AIServiceError("当前仅支持 missing_only 模式")
        if not chapters:
            raise AIServiceError("没有可创建的章节")
        db = self._db()
        created: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        try:
            if not db.get_ai_writing_project(project_id):
                raise AIServiceError("写作项目不存在")
            existing_numbers = {int(ch["chapter_number"]) for ch in db.list_ai_chapter_refs(project_id)}
            for raw in chapters:
                try:
                    chapter_number = int(raw.get("chapter_number") or 0)
                except (TypeError, ValueError):
                    chapter_number = 0
                if chapter_number <= 0:
                    skipped.append({"chapter_number": raw.get("chapter_number"), "reason": "invalid_number"})
                    continue
                if chapter_number in existing_numbers:
                    skipped.append({"chapter_number": chapter_number, "reason": "exists"})
                    continue
                metadata = dict(raw.get("metadata") or {})
                metadata_fields = {
                    "target_words": raw.get("target_words"),
                    "summary_outline": raw.get("outline") or raw.get("summary_outline"),
                    "detailed_outline": raw.get("detailed_outline") or raw.get("expanded_outline"),
                    "volume_number": raw.get("volume_number"),
                    "story_function": raw.get("story_function"),
                    "key_events": raw.get("key_events"),
                    "foreshadow_refs": raw.get("foreshadow_refs"),
                    "scene_beats": raw.get("scene_beats"),
                    "writing_notes": raw.get("writing_notes"),
                }
                for key, value in metadata_fields.items():
                    if value:
                        metadata[key] = value
                outline_text = (
                    str(raw.get("detailed_outline") or "").strip()
                    or str(raw.get("expanded_outline") or "").strip()
                    or str(raw.get("outline") or raw.get("summary_outline") or "").strip()
                )
                chapter_payload = {
                    "project_id": project_id,
                    "chapter_number": chapter_number,
                    "title": str(raw.get("title") or "").strip() or f"第{chapter_number}章",
                    "outline": outline_text,
                }
                chapter_id = db.create_ai_chapter(chapter_payload)
                if metadata:
                    db.patch_ai_chapter_metadata(chapter_id, metadata)
                existing_numbers.add(chapter_number)
                created.append({"id": chapter_id, "chapter_number": chapter_number})
            return {"created": created, "skipped": skipped}
        finally:
            db.close()

    def update_chapter(self, chapter_id: int, payload: dict[str, Any]) -> None:
        db = self._db()
        try:
            before = db.get_ai_chapter(chapter_id)
            if not before:
                raise AIServiceError("章节不存在")
            old_project_id = int(before.get("project_id") or 0)
            old_number = int(before.get("chapter_number") or 0)
            db.update_ai_chapter(chapter_id, payload)
            if {"summary", "key_events"} & set(payload):
                after = db.get_ai_chapter(chapter_id)
                if after:
                    project_id = int(after.get("project_id") or old_project_id)
                    chapter_number = int(after.get("chapter_number") or old_number)
                    summary = after.get("summary") or ""
                    key_events = after.get("key_events") or []
                    retriever = self._get_retriever()
                    if summary.strip() or key_events:
                        retriever.index_chapter(project_id, chapter_number, summary, key_events)
                    else:
                        retriever.delete_chapter(project_id, chapter_number)
        finally:
            db.close()

    def delete_chapter(self, chapter_id: int) -> None:
        db = self._db()
        try:
            chapter = db.get_ai_chapter(chapter_id)
            if not chapter:
                raise AIServiceError("章节不存在")
            project_id = int(chapter.get("project_id") or 0)
            chapter_number = int(chapter.get("chapter_number") or 0)
            self._get_retriever().delete_chapter(project_id, chapter_number)
            db.delete_ai_chapter(chapter_id)
        finally:
            db.close()

    # ── 伏笔 CRUD ─────────────────────────────────────────────────

    def list_foreshadows(self, project_id: int, status: str | None = None) -> list[dict[str, Any]]:
        db = self._db()
        try:
            return db.list_ai_foreshadows(project_id, status=status)
        finally:
            db.close()

    def create_foreshadow(self, payload: dict[str, Any]) -> int:
        project_id = int(payload.get("project_id") or 0)
        if not project_id:
            raise AIServiceError("缺少 project_id")
        description = str(payload.get("description") or "").strip()
        if not description:
            raise AIServiceError("伏笔描述不能为空")
        db = self._db()
        try:
            return db.create_ai_foreshadow(payload)
        finally:
            db.close()

    def update_foreshadow(self, foreshadow_id: int, payload: dict[str, Any]) -> None:
        db = self._db()
        try:
            db.update_ai_foreshadow(foreshadow_id, payload)
        finally:
            db.close()

    def delete_foreshadow(self, foreshadow_id: int) -> None:
        db = self._db()
        try:
            db.delete_ai_foreshadow(foreshadow_id)
        finally:
            db.close()

    # ── 项目状态记忆 ───────────────────────────────────────────────

    def get_project_states(self, project_id: int) -> dict[str, str]:
        db = self._db()
        try:
            return db.get_all_project_states(project_id)
        finally:
            db.close()

    def update_project_state(self, project_id: int, state_type: str, content: str) -> None:
        db = self._db()
        try:
            db.upsert_ai_project_state(project_id, state_type, content)
        finally:
            db.close()

    # ── 项目上下文构建（续写时自动加载）─────────────────────────────

    def build_project_context(self, project_id: int, current_chapter_number: int | None = None) -> str:
        """构建项目级上下文，用于注入续写 prompt。

        包含：项目大纲 + 状态记忆 + 伏笔提醒 + 前几章摘要 + 上一章末尾。
        """
        db = self._db()
        try:
            return self._build_project_context_with_db(db, project_id, current_chapter_number)
        finally:
            db.close()

    def _build_project_context_with_db(
        self,
        db: Database,
        project_id: int,
        current_chapter_number: int | None = None,
    ) -> str:
        """复用调用方已持有的 db 连接构建项目上下文，避免重复连接。"""
        project = db.get_ai_writing_project(project_id)
        if not project:
            raise AIServiceError("写作项目不存在")

        parts: list[str] = []

        # 1. 项目大纲
        if project.get("outline"):
            outline = project["outline"]
            if isinstance(outline, dict):
                parts.append(f"【项目大纲】\n{json.dumps(outline, ensure_ascii=False, indent=2)}")
            else:
                parts.append(f"【项目大纲】\n{outline}")

        # 2. 状态记忆
        states = db.get_all_project_states(project_id)
        for state_type, content in states.items():
            label = {"character_state": "角色状态", "plot_progress": "剧情进展",
                     "world_state": "世界观状态", "pending_hooks": "伏笔追踪"}.get(state_type, state_type)
            parts.append(f"【{label}】\n{content}")

        # 3. 伏笔提醒
        if current_chapter_number:
            approaching = db.get_approaching_foreshadows(project_id, current_chapter_number)
            overdue = db.get_overdue_foreshadows(project_id, current_chapter_number)
            if overdue:
                lines = [f"- [超期] {f['description']}（第{f['planted_chapter']}章埋设，目标第{f['target_resolve_chapter']}章回收）" for f in overdue]
                parts.append(f"【超期伏笔 - 急需回收】\n" + "\n".join(lines))
            if approaching:
                non_overdue = [f for f in approaching if f not in overdue]
                if non_overdue:
                    lines = [f"- {f['description']}（目标第{f['target_resolve_chapter']}章回收）" for f in non_overdue]
                    parts.append(f"【即将到期伏笔】\n" + "\n".join(lines))

        # 4. 前几章摘要 + 上一章末尾
        chapters = db.list_ai_chapters(project_id)
        if chapters and current_chapter_number:
            prev_chapters = [c for c in chapters if c["chapter_number"] < current_chapter_number]
            # 取最近 5 章的摘要
            recent = prev_chapters[-5:]
            if recent:
                summary_lines = []
                for c in recent:
                    summary = c.get("summary") or "(无摘要)"
                    summary_lines.append(f"第{c['chapter_number']}章 {c.get('title') or ''}: {summary}")
                parts.append(f"【前文摘要】\n" + "\n".join(summary_lines))

            # 上一章末尾 500 字
            if prev_chapters:
                last_chapter = prev_chapters[-1]
                last_content = last_chapter.get("content") or ""
                if last_content:
                    tail = last_content[-500:] if len(last_content) > 500 else last_content
                    parts.append(f"【上一章末尾】\n{tail}")

        return "\n\n".join(parts)

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any]:
        cleaned = (text or "").strip()
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, flags=re.IGNORECASE)
        if fence_match:
            cleaned = fence_match.group(1).strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise AIServiceError("模型未返回有效 JSON 对象")
        json_text = cleaned[start:end + 1]
        try:
            data = json.loads(json_text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AIServiceError("模型返回的 JSON 对象无法解析") from exc
        if not isinstance(data, dict):
            raise AIServiceError("模型返回的 JSON 顶层必须是对象")
        return data

    @staticmethod
    def _safe_int(
        value: Any,
        default: int,
        name: str,
        min_value: int | None = None,
        max_value: int | None = None,
    ) -> int:
        if value is None or value == "":
            number = default
        else:
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise AIServiceError(f"{name} 必须是整数") from None
        if min_value is not None and number < min_value:
            raise AIServiceError(f"{name} 不能小于 {min_value}")
        if max_value is not None and number > max_value:
            raise AIServiceError(f"{name} 不能大于 {max_value}")
        return number

    @staticmethod
    def _optional_positive_int(value: Any) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @staticmethod
    def _normalize_longform_plan(
        data: dict[str, Any],
        *,
        target_words: int | None = None,
        chapter_words_reference: int | None = None,
    ) -> dict[str, Any]:
        chapters_in = data.get("chapters") or []
        chapters: list[dict[str, Any]] = []
        for i, raw in enumerate(chapters_in, 1):
            if not isinstance(raw, dict):
                continue
            try:
                chapter_number = int(raw.get("chapter_number") or i)
            except (TypeError, ValueError):
                chapter_number = i
            if chapter_number <= 0:
                continue
            target = raw.get("target_words") or chapter_words_reference or data.get("average_chapter_words") or 3000
            try:
                target = int(target)
            except (TypeError, ValueError):
                target = 3000
            outline = str(raw.get("outline") or raw.get("summary_outline") or "").strip()
            chapters.append({
                "chapter_number": chapter_number,
                "title": str(raw.get("title") or f"第{chapter_number}章").strip(),
                "outline": outline,
                "detailed_outline": str(raw.get("detailed_outline") or raw.get("expanded_outline") or "").strip(),
                "target_words": target,
                "volume_number": raw.get("volume_number"),
                "story_function": str(raw.get("story_function") or "").strip(),
                "key_events": raw.get("key_events") if isinstance(raw.get("key_events"), list) else [],
                "foreshadow_refs": raw.get("foreshadow_refs") if isinstance(raw.get("foreshadow_refs"), list) else [],
                "scene_beats": raw.get("scene_beats") if isinstance(raw.get("scene_beats"), list) else [],
                "writing_notes": str(raw.get("writing_notes") or "").strip(),
            })
        chapters.sort(key=lambda item: item["chapter_number"])
        plan_target = data.get("target_words") or target_words
        try:
            plan_target = int(plan_target) if plan_target else 0
        except (TypeError, ValueError):
            plan_target = target_words or 0
        expected = data.get("expected_chapter_count") or len(chapters)
        try:
            expected = int(expected)
        except (TypeError, ValueError):
            expected = len(chapters)
        average = data.get("average_chapter_words")
        try:
            average = int(average) if average else 0
        except (TypeError, ValueError):
            average = 0
        if not average and plan_target and expected:
            average = max(round(plan_target / expected), 1)
        foreshadows = [f for f in (data.get("foreshadows") or []) if isinstance(f, dict)]
        volumes = [v for v in (data.get("volumes") or data.get("volume_structure") or []) if isinstance(v, dict)]
        return {
            "project_outline": str(data.get("project_outline") or "").strip(),
            "target_words": max(plan_target or 0, 0),
            "expected_chapter_count": max(expected, 0),
            "average_chapter_words": max(average or 0, 0),
            "structure_notes": str(data.get("structure_notes") or data.get("planning_rationale") or "").strip(),
            "volumes": volumes,
            "chapters": chapters,
            "foreshadows": foreshadows,
        }

    def _resolve_output_text(self, db: Database, payload: dict[str, Any]) -> str:
        output = str(payload.get("output_text") or payload.get("raw_output") or "").strip()
        if output:
            return output
        job_id = str(payload.get("job_id") or "").strip()
        if not job_id:
            raise AIServiceError("缺少 output_text/raw_output 或 job_id")
        job = db.get_ai_job(job_id)
        if not job or not (job.get("output_text") or "").strip():
            raise AIServiceError("任务没有可导入的原始输出")
        return str(job["output_text"])

    def _apply_longform_plan(
        self,
        db: Database,
        project_id: int,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        project = db.get_ai_writing_project(project_id)
        if not project:
            raise AIServiceError("写作项目不存在")
        settings = dict(project.get("settings") or {})
        settings["longform_plan"] = plan
        settings["target_words"] = plan.get("target_words")
        settings["expected_chapter_count"] = plan.get("expected_chapter_count")
        settings["average_chapter_words"] = plan.get("average_chapter_words")
        new_outline = plan.get("project_outline") or project.get("outline")
        update_payload = {"outline": new_outline, "settings": settings}
        if new_outline != project.get("outline") or settings != (project.get("settings") or {}):
            db.update_ai_writing_project(project_id, update_payload)
        return plan

    def import_longform_plan_output(self, project_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        db = self._db()
        try:
            output = self._resolve_output_text(db, payload)
            target_words = self._optional_positive_int(payload.get("target_words"))
            chapter_words_reference = self._optional_positive_int(payload.get("chapter_words_reference")) or 3000
            plan = self._normalize_longform_plan(
                self._extract_json_object(output),
                target_words=target_words,
                chapter_words_reference=chapter_words_reference,
            )
            return self._apply_longform_plan(db, project_id, plan)
        finally:
            db.close()

    def _apply_longform_plan_details(
        self,
        db: Database,
        project_id: int,
        details: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        project = db.get_ai_writing_project(project_id)
        if not project:
            raise AIServiceError("写作项目不存在")
        settings = dict(project.get("settings") or {})
        plan = dict(settings.get("longform_plan") or {})
        plan_chapters = list(plan.get("chapters") or [])
        if not plan_chapters:
            raise AIServiceError("请先生成全书规划")
        existing_chapters = {
            int(ch["chapter_number"]): ch["id"]
            for ch in db.list_ai_chapter_refs(project_id)
        }
        chapter_updates: list[dict[str, Any]] = []
        updated = 0
        for ch in plan_chapters:
            number = int(ch.get("chapter_number") or 0)
            detail = details.get(number)
            if not detail:
                continue
            ch["detailed_outline"] = detail["detailed_outline"]
            ch["scene_beats"] = detail.get("scene_beats") or []
            ch["writing_notes"] = detail.get("writing_notes") or ""
            chapter_id = existing_chapters.get(number)
            if chapter_id:
                chapter_updates.append({
                    "id": chapter_id,
                    "outline": detail["detailed_outline"],
                    "metadata": {
                        "summary_outline": ch.get("outline"),
                        "detailed_outline": detail["detailed_outline"],
                        "scene_beats": detail.get("scene_beats") or [],
                        "writing_notes": detail.get("writing_notes") or "",
                    },
                })
            updated += 1
        if chapter_updates:
            db.update_ai_chapters_outlines_and_metadata(chapter_updates)
        plan["chapters"] = plan_chapters
        settings["longform_plan"] = plan
        if settings != (project.get("settings") or {}):
            db.update_ai_writing_project(project_id, {"settings": settings})
        return {"plan": plan, "updated": updated}

    def import_longform_plan_details_output(self, project_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        db = self._db()
        try:
            output = self._resolve_output_text(db, payload)
            details = self._normalize_longform_detail_plan(self._extract_json_object(output))
            if not details:
                raise AIServiceError("原始输出中没有可用详细梗概")
            return self._apply_longform_plan_details(db, project_id, details)
        finally:
            db.close()

        project_id = self._safe_int(payload.get("project_id"), 0, "project_id", min_value=0)
        agent_id = self._safe_int(payload.get("agent_id"), 0, "agent_id", min_value=0)
        if not project_id:
            raise AIServiceError("缺少 project_id")
        db = self._db()
        job_id = uuid.uuid4().hex
        output_parts: list[str] = []
        job_created = False
        try:
            agent = self._load_agent_config(db, agent_id)
            provider_config = self._load_provider_config(db, agent.provider_id)
            project = db.get_ai_writing_project(project_id)
            if not project:
                raise AIServiceError("写作项目不存在")
            chapters = db.list_ai_chapters(project_id)
            target_words = self._optional_positive_int(payload.get("target_words"))
            expected_chapters = self._optional_positive_int(payload.get("expected_chapters"))
            chapter_words_reference = self._optional_positive_int(payload.get("chapter_words_reference")) or 3000
            if not target_words and expected_chapters:
                target_words = expected_chapters * chapter_words_reference
            if not target_words:
                raise AIServiceError("缺少目标总字数")
            if target_words > 10_000_000:
                raise AIServiceError("目标总字数过大，请分阶段规划")
            if expected_chapters and expected_chapters > 2000:
                raise AIServiceError("章节数参考过大")
            messages = build_longform_plan_messages(
                system_prompt=agent.system_prompt if agent.task_type == "plan" else None,
                project=project,
                chapters=chapters,
                instruction=payload.get("instruction"),
                target_words=target_words,
                expected_chapters=expected_chapters,
                chapter_words_reference=chapter_words_reference,
            )
            db.create_ai_job(job_id, "longform_plan", agent.id, {
                "project_id": project_id,
                "target_words": target_words,
                "expected_chapters": expected_chapters,
                "chapter_words_reference": chapter_words_reference,
                "instruction": payload.get("instruction"),
            })
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
            plan = self._normalize_longform_plan(
                self._extract_json_object(output),
                target_words=target_words,
                chapter_words_reference=chapter_words_reference,
            )
            self._apply_longform_plan(db, project_id, plan)
            db.update_ai_job(job_id, "succeeded", output_text=output, output_json=plan)
            yield AIStreamChunk(type="custom", data={"event": "longform_plan", "plan": plan})
            yield AIStreamChunk(type="done", data={"job_id": job_id, "plan": plan})
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

    @staticmethod
    def _normalize_longform_detail_plan(data: dict[str, Any]) -> dict[int, dict[str, Any]]:
        result: dict[int, dict[str, Any]] = {}
        for raw in data.get("chapters") or []:
            if not isinstance(raw, dict):
                continue
            try:
                chapter_number = int(raw.get("chapter_number") or 0)
            except (TypeError, ValueError):
                continue
            detailed_outline = str(raw.get("detailed_outline") or raw.get("expanded_outline") or "").strip()
            if chapter_number <= 0 or not detailed_outline:
                continue
            result[chapter_number] = {
                "detailed_outline": detailed_outline,
                "scene_beats": raw.get("scene_beats") if isinstance(raw.get("scene_beats"), list) else [],
                "writing_notes": str(raw.get("writing_notes") or "").strip(),
            }
        return result

    def stream_longform_plan_details(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        project_id = self._safe_int(payload.get("project_id"), 0, "project_id", min_value=0)
        agent_id = self._safe_int(payload.get("agent_id"), 0, "agent_id", min_value=0)
        if not project_id:
            raise AIServiceError("缺少 project_id")
        db = self._db()
        job_id = uuid.uuid4().hex
        output_parts: list[str] = []
        job_created = False
        try:
            agent = self._load_agent_config(db, agent_id)
            provider_config = self._load_provider_config(db, agent.provider_id)
            project = db.get_ai_writing_project(project_id)
            if not project:
                raise AIServiceError("写作项目不存在")
            settings = dict(project.get("settings") or {})
            plan = dict(settings.get("longform_plan") or {})
            plan_chapters = list(plan.get("chapters") or [])
            if not plan_chapters:
                raise AIServiceError("请先生成全书规划")
            selected_numbers = payload.get("chapter_numbers") or []
            selected: set[int] = set()
            for raw_number in selected_numbers:
                number = self._safe_int(raw_number, 0, "chapter_number", min_value=0)
                if number > 0:
                    selected.add(number)
            mode = payload.get("mode") or "missing_only"
            target_chapters = []
            for ch in plan_chapters:
                number = int(ch.get("chapter_number") or 0)
                if selected and number not in selected:
                    continue
                if mode == "missing_only" and ch.get("detailed_outline"):
                    continue
                target_chapters.append(ch)
            if not target_chapters:
                raise AIServiceError("没有需要扩写的章节梗概")
            messages = build_longform_detail_messages(
                system_prompt=agent.system_prompt if agent.task_type == "plan" else None,
                project=project,
                longform_plan=plan,
                chapters=target_chapters,
                instruction=payload.get("instruction"),
            )
            db.create_ai_job(job_id, "longform_plan_details", agent.id, {
                "project_id": project_id,
                "mode": mode,
                "chapter_numbers": [ch.get("chapter_number") for ch in target_chapters],
                "instruction": payload.get("instruction"),
            })
            job_created = True
            yield AIStreamChunk(type="metadata", data={"job_id": job_id, "chapters": len(target_chapters)})
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
            details = self._normalize_longform_detail_plan(self._extract_json_object(output))
            if not details:
                raise AIServiceError("模型未返回可用详细梗概")
            applied = self._apply_longform_plan_details(db, project_id, details)
            plan = applied["plan"]
            db.update_ai_job(job_id, "succeeded", output_text=output, output_json={"plan": plan, "updated": applied["updated"]})
            yield AIStreamChunk(type="custom", data={"event": "longform_plan_details", "plan": plan})
            yield AIStreamChunk(type="done", data={"job_id": job_id, "plan": plan, "updated": len(details)})
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

    # ── 章节续写（项目模式）────────────────────────────────────────

    def _build_chapter_continue_inputs(
        self,
        db: Database,
        payload: dict[str, Any],
        agent: AIAgentConfig,
    ) -> dict[str, Any]:
        project_id = self._safe_int(payload.get("project_id"), 0, "project_id", min_value=0)
        chapter_id = self._safe_int(payload.get("chapter_id"), 0, "chapter_id", min_value=0)
        chapter = db.get_ai_chapter(chapter_id) if chapter_id else None
        chapter_number = chapter["chapter_number"] if chapter else self._safe_int(payload.get("chapter_number"), 1, "chapter_number", min_value=1)
        project_context = self._build_project_context_with_db(db, project_id, chapter_number) if project_id else ""
        existing_content = ""
        if chapter and chapter.get("content"):
            existing_content = chapter["content"]
        elif payload.get("text"):
            existing_content = str(payload["text"])
        if project_context and existing_content:
            full_context = f"{project_context}\n\n【当前章节已有内容】\n{existing_content}"
        elif project_context:
            full_context = project_context
        else:
            full_context = existing_content
        if not full_context.strip():
            raise AIServiceError("没有可用的上下文（项目为空且未提供文本）")
        context_chars = self._safe_int(payload.get("context_chars"), agent.context_window, "context_chars", min_value=1)
        original_context_chars = len(full_context)
        truncated = False
        if len(full_context) > context_chars:
            full_context = get_tail_context(full_context, context_chars)
            truncated = True
        plan_text = None
        if chapter and chapter.get("outline"):
            plan_text = chapter["outline"]
        elif payload.get("plan_text"):
            plan_text = payload["plan_text"]
        style_prompt = payload.get("style_prompt")
        novel_prompt = payload.get("novel_prompt")
        project = None
        if (not style_prompt or not novel_prompt) and project_id:
            project = db.get_ai_writing_project(project_id)
        if not style_prompt and project and project.get("style_profile_id"):
            profile = db.get_ai_style_profile(project["style_profile_id"])
            if profile:
                style_prompt = profile.get("profile_json") or profile.get("profile")
        if not novel_prompt and project and project.get("novel_profile_id"):
            profile = db.get_ai_novel_profile(project["novel_profile_id"])
            if profile:
                novel_prompt = profile.get("profile_json") or profile.get("profile")
        messages = build_continue_messages(
            system_prompt=agent.system_prompt,
            context=full_context,
            instruction=payload.get("instruction"),
            output_chars=payload.get("output_chars"),
            style_prompt=style_prompt,
            novel_prompt=novel_prompt,
            plan_text=plan_text,
        )
        return {
            "project_id": project_id,
            "chapter_id": chapter_id,
            "chapter": chapter,
            "chapter_number": chapter_number,
            "project_context": project_context,
            "existing_content": existing_content,
            "full_context": full_context,
            "original_context_chars": original_context_chars,
            "context_chars": context_chars,
            "truncated": truncated,
            "plan_text": plan_text,
            "style_prompt": style_prompt,
            "novel_prompt": novel_prompt,
            "messages": messages,
        }

    def preview_project_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = self._safe_int(payload.get("agent_id"), 0, "agent_id", min_value=0)
        db = self._db()
        try:
            agent = self._load_agent_config(db, agent_id)
            built = self._build_chapter_continue_inputs(db, payload, agent)
            max_chars = self._safe_int(payload.get("max_chars"), 4000, "max_chars", min_value=100, max_value=50000)
            return {
                "project_context": built["project_context"],
                "existing_content_preview": get_tail_context(built["existing_content"], max_chars) if built["existing_content"] else "",
                "full_context_preview": get_tail_context(built["full_context"], max_chars),
                "plan_text": built["plan_text"],
                "has_style_prompt": bool(built["style_prompt"]),
                "has_novel_prompt": bool(built["novel_prompt"]),
                "prompt_preview": safe_prompt_preview(built["messages"], max_chars=max_chars),
                "stats": {
                    "project_context_chars": len(built["project_context"]),
                    "existing_content_chars": len(built["existing_content"]),
                    "full_context_chars": len(built["full_context"]),
                    "original_context_chars": built["original_context_chars"],
                    "context_limit_chars": built["context_chars"],
                    "truncated": built["truncated"],
                    "messages": len(built["messages"]),
                    "chapter_number": built["chapter_number"],
                },
            }
        finally:
            db.close()

    def stream_chapter_continue(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        """基于项目上下文的章节续写。"""
        db = self._db()
        job_id = uuid.uuid4().hex
        output_parts: list[str] = []
        job_created = False
        agent_id = self._safe_int(payload.get("agent_id"), 0, "agent_id", min_value=0)
        project_id = self._safe_int(payload.get("project_id"), 0, "project_id", min_value=0)
        chapter_id = self._safe_int(payload.get("chapter_id"), 0, "chapter_id", min_value=0)

        try:
            agent = self._load_agent_config(db, agent_id)
            provider_config = self._load_provider_config(db, agent.provider_id)
            built = self._build_chapter_continue_inputs(db, payload, agent)
            chapter_id = built["chapter_id"]
            project_id = built["project_id"]
            chapter = built["chapter"]
            chapter_number = built["chapter_number"]
            existing_content = built["existing_content"]
            full_context = built["full_context"]
            messages = built["messages"]

            db.create_ai_job(job_id, "chapter_continue", agent.id, {
                "project_id": project_id, "chapter_id": chapter_id,
                "chapter_number": chapter_number, "context_chars": len(full_context),
            })
            job_created = True
            yield AIStreamChunk(type="metadata", data={"job_id": job_id, "project_id": project_id, "chapter_number": chapter_number})

            model = agent.model or provider_config.default_model
            if not model:
                raise AIServiceError("Agent 或 Provider 未配置模型")
            provider = create_provider(provider_config)
            auto_save = bool(chapter_id and payload.get("auto_save", True) is not False)
            auto_save_interval_chars = self._safe_int(
                payload.get("auto_save_interval_chars"), 800, "auto_save_interval_chars", min_value=100, max_value=20000,
            )
            auto_save_interval_sec = self._safe_int(
                payload.get("auto_save_interval_sec"), 3, "auto_save_interval_sec", min_value=1, max_value=120,
            )
            last_saved_chars = 0
            last_saved_at = time.time()

            def save_generated(status: str) -> None:
                nonlocal last_saved_chars, last_saved_at
                if not auto_save:
                    return
                generated = "".join(output_parts)
                if not generated and status not in {"succeeded", "cancelled", "failed"}:
                    return
                content = f"{existing_content}{generated}"
                db.update_ai_chapter(chapter_id, {"content": content, "status": "draft"})
                last_saved_chars = len(generated)
                last_saved_at = time.time()
                db.patch_ai_chapter_metadata(chapter_id, {
                    "continue_autosave": {
                        "status": status,
                        "job_id": job_id,
                        "saved_chars": len(generated),
                        "total_chars": len(content),
                        "saved_at": int(last_saved_at),
                    }
                })

            for chunk in provider.stream_generate(
                messages, model=model, temperature=agent.temperature,
                top_p=agent.top_p, max_tokens=agent.max_tokens,
            ):
                if chunk.type == "delta":
                    output_parts.append(chunk.text)
                    generated_chars = sum(len(part) for part in output_parts)
                    if (
                        auto_save
                        and generated_chars > last_saved_chars
                        and (
                            generated_chars - last_saved_chars >= auto_save_interval_chars
                            or time.time() - last_saved_at >= auto_save_interval_sec
                        )
                    ):
                        save_generated("running")
                    yield chunk

            output = "".join(output_parts)
            save_generated("succeeded")
            db.update_ai_job(job_id, "succeeded", output_text=output, output_json={"chars": len(output), "autosaved": auto_save})
            yield AIStreamChunk(type="done", data={"job_id": job_id, "chars": len(output), "autosaved": auto_save})
        except GeneratorExit:
            if job_created:
                if "save_generated" in locals():
                    save_generated("cancelled")
                db.update_ai_job(job_id, "cancelled", output_text="".join(output_parts), error_message="客户端断开连接")
            raise
        except Exception as exc:
            message = str(exc)
            if job_created:
                if "save_generated" in locals():
                    save_generated("failed")
                db.update_ai_job(job_id, "failed", output_text="".join(output_parts), error_message=message)
            yield AIStreamChunk(type="error", data={"message": message})
        finally:
            db.close()

    # ── 章节完成后自动更新状态 ─────────────────────────────────────

    def stream_update_project_state(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        """章节完成后，用 LLM 自动更新项目状态记忆。

        分析新章节内容，更新 character_state / plot_progress / pending_hooks。
        """
        db = self._db()
        job_id = uuid.uuid4().hex
        output_parts: list[str] = []
        job_created = False
        agent_id = self._safe_int(payload.get("agent_id"), 0, "agent_id", min_value=0)
        project_id = self._safe_int(payload.get("project_id"), 0, "project_id", min_value=0)
        chapter_id = self._safe_int(payload.get("chapter_id"), 0, "chapter_id", min_value=0)

        try:
            agent = self._load_agent_config(db, agent_id)
            provider_config = self._load_provider_config(db, agent.provider_id)

            chapter = db.get_ai_chapter(chapter_id)
            if not chapter or not chapter.get("content"):
                raise AIServiceError("章节内容为空，无法更新状态")

            # 获取现有状态
            states = db.get_all_project_states(project_id)
            existing_state_text = ""
            if states:
                parts = []
                for st, content in states.items():
                    parts.append(f"[{st}]\n{content}")
                existing_state_text = "\n\n".join(parts)

            system_prompt = """你是小说项目状态管理助手。根据新完成的章节内容，更新项目的状态记忆。

请输出以下三个部分（用 === 分隔）：

=== character_state ===
列出所有角色的当前状态（位置、情绪、关系变化、新获得的信息）。

=== plot_progress ===
简要记录到目前为止的剧情进展（按时间线，每章 1-2 句话）。

=== new_foreshadows ===
列出本章新埋下的伏笔（如果有的话），每条一行，格式：描述 | 重要性(high/normal/low)

规则：
- 简洁精炼，每个部分不超过 500 字
- 只记录事实，不做评价
- 如果有已有状态，在其基础上更新而非重写"""

            user_content_parts = []
            if existing_state_text:
                user_content_parts.append(f"【已有状态】\n{existing_state_text}")
            user_content_parts.append(f"【第{chapter['chapter_number']}章内容】\n{chapter['content']}")

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "\n\n".join(user_content_parts)},
            ]

            db.create_ai_job(job_id, "update_state", agent.id, {"project_id": project_id, "chapter_id": chapter_id})
            job_created = True
            yield AIStreamChunk(type="metadata", data={"job_id": job_id})

            model = agent.model or provider_config.default_model
            if not model:
                raise AIServiceError("Agent 或 Provider 未配置模型")
            provider = create_provider(provider_config)
            for chunk in provider.stream_generate(
                messages, model=model, temperature=0.3, top_p=0.9, max_tokens=2000,
            ):
                if chunk.type == "delta":
                    output_parts.append(chunk.text)
                    yield chunk

            output = "".join(output_parts)

            # 解析输出并保存状态
            self._parse_and_save_state(db, project_id, chapter, output)

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

    def _parse_and_save_state(self, db: Database, project_id: int, chapter: dict[str, Any], output: str) -> None:
        """解析 LLM 输出的状态更新并保存到数据库。"""
        sections = output.split("===")
        current_type = ""
        for section in sections:
            section = section.strip()
            if not section:
                continue
            # 检查是否是标题行
            if section in ("character_state", "plot_progress", "new_foreshadows"):
                current_type = section
                continue
            # 尝试从 "xxx ===" 格式提取
            for st in ("character_state", "plot_progress", "new_foreshadows"):
                if st in section:
                    current_type = st
                    section = section.replace(st, "").strip()
                    break

            if not section or not current_type:
                continue

            if current_type in ("character_state", "plot_progress"):
                db.upsert_ai_project_state(project_id, current_type, section)
            elif current_type == "new_foreshadows":
                # 解析伏笔列表
                for line in section.splitlines():
                    line = line.strip().lstrip("- •")
                    if not line:
                        continue
                    parts = line.split("|")
                    description = parts[0].strip()
                    importance = "normal"
                    if len(parts) > 1:
                        imp = parts[1].strip().lower()
                        if imp in ("high", "normal", "low"):
                            importance = imp
                    if description:
                        db.create_ai_foreshadow({
                            "project_id": project_id,
                            "description": description,
                            "planted_chapter": chapter.get("chapter_number"),
                            "importance": importance,
                        })

    # ── 语义检索 ───────────────────────────────────────────────────

    def index_chapter_for_retrieval(self, project_id: int, chapter_id: int) -> None:
        """将章节摘要和关键事件索引到检索库。"""
        db = self._db()
        try:
            chapter = db.get_ai_chapter(chapter_id)
            if not chapter:
                raise AIServiceError("章节不存在")
            actual_project_id = int(chapter.get("project_id") or 0)
            if actual_project_id != int(project_id):
                raise AIServiceError("章节不属于该项目")
            chapter_number = int(chapter["chapter_number"])
            summary = chapter.get("summary") or ""
            key_events = chapter.get("key_events") or []
            retriever = self._get_retriever()
            if summary.strip() or key_events:
                retriever.index_chapter(
                    project_id=project_id,
                    chapter_number=chapter_number,
                    summary=summary,
                    key_events=key_events,
                )
            else:
                retriever.delete_chapter(project_id, chapter_number)
        finally:
            db.close()

    def search_project_context(self, project_id: int, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """语义检索项目历史片段。"""
        retriever = self._get_retriever()
        results = retriever.search(project_id, query, top_k=top_k)
        return [
            {"chapter_number": r.chapter_number, "content": r.content, "entry_type": r.entry_type, "score": round(r.score, 3)}
            for r in results
        ]

    # ════════════════════════════════════════════════════════════════
    # 创作向导多轮对话
    # ════════════════════════════════════════════════════════════════

    def list_chat_sessions(self, scope: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        db = self._db()
        try:
            return db.list_ai_chat_sessions(scope=scope, status=status)
        finally:
            db.close()

    def get_chat_session(self, session_id: int, with_messages: bool = True) -> dict[str, Any]:
        db = self._db()
        try:
            session = db.get_ai_chat_session(session_id)
            if not session:
                raise AIServiceError("会话不存在")
            if with_messages:
                session["messages"] = db.list_ai_chat_messages(session_id)
            return session
        finally:
            db.close()

    def create_chat_session(self, payload: dict[str, Any]) -> int:
        db = self._db()
        try:
            agent_id = payload.get("agent_id")
            if agent_id:
                agent = db.get_ai_agent(int(agent_id))
                if not agent:
                    raise AIServiceError("Agent 不存在")
            return db.create_ai_chat_session({
                "agent_id": int(agent_id) if agent_id else None,
                "scope": payload.get("scope") or "wizard",
                "title": payload.get("title") or "新会话",
                "metadata": payload.get("metadata") or {},
            })
        finally:
            db.close()

    def update_chat_session(self, session_id: int, payload: dict[str, Any]) -> None:
        db = self._db()
        try:
            allowed = {k: payload[k] for k in ("title", "status", "agent_id", "metadata") if k in payload}
            if "agent_id" in allowed and allowed["agent_id"]:
                allowed["agent_id"] = int(allowed["agent_id"])
            db.update_ai_chat_session(session_id, allowed)
        finally:
            db.close()

    def delete_chat_session(self, session_id: int) -> None:
        db = self._db()
        try:
            db.delete_ai_chat_session(session_id)
        finally:
            db.close()

    def stream_chat(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        """多轮对话流式输出。
        payload: { session_id, user_message, agent_id?(覆盖), max_history? }
        """
        db = self._db()
        job_id = uuid.uuid4().hex
        output_parts: list[str] = []
        job_created = False
        session_id = int(payload.get("session_id") or 0)
        user_message = (payload.get("user_message") or "").strip()
        try:
            if not session_id:
                raise AIServiceError("缺少 session_id")
            if not user_message:
                raise AIServiceError("消息不能为空")
            session = db.get_ai_chat_session(session_id)
            if not session:
                raise AIServiceError("会话不存在")

            agent_id = int(payload.get("agent_id") or session.get("agent_id") or 0)
            if not agent_id:
                raise AIServiceError("会话未绑定 Agent")
            agent = self._load_agent_config(db, agent_id)
            provider_config = self._load_provider_config(db, agent.provider_id)

            # 写入用户消息
            db.append_ai_chat_message(session_id, "user", user_message)

            # 加载历史
            max_history = int(payload.get("max_history") or 40)
            all_msgs = db.list_ai_chat_messages(session_id)
            # 去掉刚写入的最后一条 user（构建时再单独追加）
            history_msgs = all_msgs[:-1] if all_msgs else []
            # 截断：只保留最近 max_history 条
            if len(history_msgs) > max_history:
                history_msgs = history_msgs[-max_history:]
            history = [{"role": m["role"], "content": m["content"]} for m in history_msgs]

            # 累计产物摘要（来自 session.metadata）
            extra = None
            sess_meta = session.get("metadata") or {}
            collected = sess_meta.get("collected_sections") or {}
            if collected:
                lines = [f"- {k}：已收集 {len(v)} 字" for k, v in collected.items() if isinstance(v, str) and v]
                if lines:
                    extra = "\n".join(lines)

            messages = build_chat_messages(
                system_prompt=agent.system_prompt,
                history=history,
                user_message=user_message,
                extra_system_context=extra,
            )

            db.create_ai_job(job_id, "chat", agent.id, {
                "session_id": session_id, "history_count": len(history_msgs),
            })
            job_created = True
            yield AIStreamChunk(type="metadata", data={
                "job_id": job_id, "session_id": session_id,
                "history_count": len(history_msgs),
            })

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
            # 写入 assistant 消息
            db.append_ai_chat_message(session_id, "assistant", output)

            # 检测 ready_for_import 标记 + 解析节段更新 metadata
            try:
                self._update_session_metadata_from_output(db, session_id, output)
            except Exception:
                # 解析失败不影响主流程
                pass

            ready = "<<<READY_FOR_IMPORT>>>" in output
            db.update_ai_job(job_id, "succeeded", output_text=output, output_json={"chars": len(output), "ready": ready})
            yield AIStreamChunk(type="done", data={"job_id": job_id, "chars": len(output), "ready_to_import": ready})
        except GeneratorExit:
            if job_created:
                db.update_ai_job(job_id, "cancelled", output_text="".join(output_parts), error_message="客户端断开连接")
            raise
        except Exception as exc:
            msg = str(exc)
            if job_created:
                db.update_ai_job(job_id, "failed", output_text="".join(output_parts), error_message=msg)
            yield AIStreamChunk(type="error", data={"message": msg})
        finally:
            db.close()

    def _update_session_metadata_from_output(self, db: Database, session_id: int, output: str) -> None:
        """从 assistant 输出中提取 ## 节段，浅合并到 session.metadata.collected_sections。
        节段标题作为 key，节段内容作为 value（覆盖式）。
        """
        sections: dict[str, str] = {}
        current_key: str | None = None
        current_lines: list[str] = []
        # 移除 <<<READY_FOR_IMPORT>>> 后面的 JSON 块（防止把 JSON 当节段）
        clean = output.split("<<<READY_FOR_IMPORT>>>")[0]
        for line in clean.splitlines():
            stripped = line.strip()
            if stripped.startswith("## "):
                if current_key is not None:
                    sections[current_key] = "\n".join(current_lines).strip()
                current_key = stripped[3:].strip()
                current_lines = []
            elif current_key is not None:
                current_lines.append(line)
        if current_key is not None:
            sections[current_key] = "\n".join(current_lines).strip()
        if sections:
            sess = db.get_ai_chat_session(session_id)
            meta = sess.get("metadata") if sess else {}
            collected = (meta.get("collected_sections") or {}) if isinstance(meta, dict) else {}
            collected.update(sections)
            db.patch_ai_chat_session_metadata(session_id, {"collected_sections": collected})

    def parse_wizard_session(self, session_id: int) -> dict[str, Any]:
        """从 wizard 会话提取结构化产物。
        优先从 assistant 消息里查找 <<<READY_FOR_IMPORT>>> 后的 JSON 块；
        没有则 fallback 用 collected_sections 拼装。
        """
        db = self._db()
        try:
            session = db.get_ai_chat_session(session_id)
            if not session:
                raise AIServiceError("会话不存在")
            messages = db.list_ai_chat_messages(session_id)
            # 倒序找最后一条含 READY 标记的 assistant 消息
            for msg in reversed(messages):
                if msg.get("role") != "assistant":
                    continue
                content = msg.get("content") or ""
                if "<<<READY_FOR_IMPORT>>>" not in content:
                    continue
                after = content.split("<<<READY_FOR_IMPORT>>>", 1)[1]
                try:
                    data = self._extract_json_object(after)
                    return self._normalize_wizard_payload(data, session)
                except AIServiceError:
                    break
            parse_warning = None
            if any(
                msg.get("role") == "assistant" and "<<<READY_FOR_IMPORT>>>" in (msg.get("content") or "")
                for msg in messages
            ):
                parse_warning = "READY JSON 无法解析，已退回为节段拼装"
            # fallback：用 collected_sections 拼装
            meta = session.get("metadata") or {}
            collected: dict[str, str] = meta.get("collected_sections") or {}
            project_name = "未命名作品"
            description = ""
            outline_parts = []
            settings: dict[str, Any] = {"raw_sections": collected}
            if "一句话梗概" in collected:
                description = collected["一句话梗概"]
                project_name = description[:20] + ("…" if len(description) > 20 else "")
            for k in ("分册结构", "剧情节点总览", "详细大纲（第N册）", "详细大纲"):
                if k in collected:
                    outline_parts.append(f"## {k}\n{collected[k]}")
            outline = "\n\n".join(outline_parts) if outline_parts else None
            return {
                "project": {"name": project_name, "description": description, "outline": outline, "settings": settings},
                "chapters": [],
                "foreshadows": [],
                "_source": "fallback_sections",
                "_parse_warning": parse_warning,
            }
        finally:
            db.close()

    @staticmethod
    def _normalize_wizard_payload(data: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
        proj = data.get("project") or {}
        if not proj.get("name"):
            proj["name"] = (session.get("title") or "未命名作品").strip()
        chapters = data.get("chapters") or []
        foreshadows = data.get("foreshadows") or []
        return {"project": proj, "chapters": chapters, "foreshadows": foreshadows, "_source": "ready_json"}

    def import_wizard_session(self, session_id: int, mode: str = "create",
                               target_project_id: int | None = None,
                               overwrite_fields: list[str] | None = None) -> int:
        """一键导入 wizard 会话产物到项目。返回 project_id。
        mode: 'create' 新建项目；'merge' 合并到已有项目
        """
        if mode not in ("create", "merge"):
            raise AIServiceError("不支持的导入模式")
        if mode == "merge" and not target_project_id:
            raise AIServiceError("merge 模式需要 target_project_id")
        parsed = self.parse_wizard_session(session_id)
        db = self._db()
        try:
            return self._import_wizard_payload(db, parsed, session_id, mode, target_project_id, overwrite_fields)
        finally:
            db.close()

    def import_wizard_output(self, session_id: int, payload: dict[str, Any]) -> int:
        mode = payload.get("mode") or "create"
        target_project_id = payload.get("target_project_id")
        overwrite_fields = payload.get("overwrite_fields") or []
        if mode not in ("create", "merge"):
            raise AIServiceError("不支持的导入模式")
        if mode == "merge" and not target_project_id:
            raise AIServiceError("merge 模式需要 target_project_id")
        db = self._db()
        try:
            session = db.get_ai_chat_session(session_id)
            if not session:
                raise AIServiceError("会话不存在")
            output = self._resolve_output_text(db, payload)
            marker = "<<<READY_FOR_IMPORT>>>"
            raw = output.split(marker, 1)[1] if marker in output else output
            parsed = self._normalize_wizard_payload(self._extract_json_object(raw), session)
            return self._import_wizard_payload(db, parsed, session_id, mode, target_project_id, overwrite_fields)
        finally:
            db.close()

    def _import_wizard_payload(
        self,
        db: Database,
        parsed: dict[str, Any],
        session_id: int,
        mode: str,
        target_project_id: int | None,
        overwrite_fields: list[str] | None,
    ) -> int:
        proj = parsed.get("project") or {}
        chapters = parsed.get("chapters") or []
        foreshadows = parsed.get("foreshadows") or []
        if mode == "create":
            project_id = db.create_ai_writing_project({
                "name": proj.get("name") or "未命名作品",
                "description": proj.get("description"),
                "outline": proj.get("outline"),
                "settings": proj.get("settings") or {},
            })
        else:
            project_id = int(target_project_id)
            existing = db.get_ai_writing_project(project_id)
            if not existing:
                raise AIServiceError("目标项目不存在")
            allow = set(overwrite_fields or [])
            update_payload: dict[str, Any] = {}
            for key in ("name", "description", "outline"):
                new_val = proj.get(key)
                if not new_val:
                    continue
                existing_val = existing.get(key)
                should_update = key in allow or not (existing_val or "").strip() if isinstance(existing_val, str) else key in allow
                if should_update:
                    update_payload[key] = new_val
            new_settings = proj.get("settings") or {}
            if new_settings:
                cur = existing.get("settings") or {}
                if isinstance(cur, dict):
                    cur.update(new_settings)
                    update_payload["settings"] = cur
                else:
                    update_payload["settings"] = new_settings
            if update_payload:
                db.update_ai_writing_project(project_id, update_payload)

        existing_numbers = {c["chapter_number"] for c in db.list_ai_chapters(project_id)}
        for ch in chapters:
            num = int(ch.get("chapter_number") or 0)
            if not num or num in existing_numbers:
                continue
            db.create_ai_chapter({
                "project_id": project_id,
                "chapter_number": num,
                "title": ch.get("title"),
                "outline": ch.get("outline"),
            })
            existing_numbers.add(num)

        existing_descs = {f["description"] for f in db.list_ai_foreshadows(project_id)}
        for fs in foreshadows:
            desc = (fs.get("description") or "").strip()
            if not desc or desc in existing_descs:
                continue
            db.create_ai_foreshadow({
                "project_id": project_id,
                "description": desc,
                "planted_chapter": fs.get("planted_chapter"),
                "target_resolve_chapter": fs.get("target_resolve_chapter"),
                "importance": fs.get("importance") or "normal",
                "notes": fs.get("notes"),
            })
            existing_descs.add(desc)

        db.update_ai_chat_session(session_id, {
            "imported_project_id": project_id,
            "status": "imported",
        })
        return project_id

    # ════════════════════════════════════════════════════════════════
    # 章节摘要 + 伏笔回收 + 对话/心理润色
    # ════════════════════════════════════════════════════════════════

    def stream_extract_chapter_summary(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        """提取章节摘要 + 关键事件，写回 chapter.summary / key_events"""
        db = self._db()
        job_id = uuid.uuid4().hex
        output_parts: list[str] = []
        job_created = False
        agent_id = int(payload.get("agent_id") or 0)
        chapter_id = int(payload.get("chapter_id") or 0)
        try:
            agent = self._load_agent_config(db, agent_id)
            provider_config = self._load_provider_config(db, agent.provider_id)
            chapter = db.get_ai_chapter(chapter_id)
            if not chapter or not (chapter.get("content") or "").strip():
                raise AIServiceError("章节内容为空")
            messages = build_chapter_summary_messages(
                system_prompt=agent.system_prompt,
                chapter_text=chapter["content"],
                chapter_number=chapter.get("chapter_number"),
                chapter_title=chapter.get("title"),
            )
            db.create_ai_job(job_id, "extract_summary", agent.id, {"chapter_id": chapter_id})
            job_created = True
            yield AIStreamChunk(type="metadata", data={"job_id": job_id, "chapter_id": chapter_id})
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
            summary, key_events = self._parse_summary_output(output)
            update_data: dict[str, Any] = {}
            if summary:
                update_data["summary"] = summary
            if key_events:
                update_data["key_events"] = key_events
            if update_data:
                db.update_ai_chapter(chapter_id, update_data)
            db.update_ai_job(job_id, "succeeded", output_text=output, output_json={"summary_chars": len(summary or ""), "events": len(key_events or [])})
            yield AIStreamChunk(type="done", data={"job_id": job_id, "summary": summary, "key_events": key_events})
        except GeneratorExit:
            if job_created:
                db.update_ai_job(job_id, "cancelled", output_text="".join(output_parts), error_message="客户端断开连接")
            raise
        except Exception as exc:
            msg = str(exc)
            if job_created:
                db.update_ai_job(job_id, "failed", output_text="".join(output_parts), error_message=msg)
            yield AIStreamChunk(type="error", data={"message": msg})
        finally:
            db.close()

    @staticmethod
    def _parse_summary_output(output: str) -> tuple[str, list[str]]:
        text = output or ""
        marker_pattern = re.compile(r"^\s*===\s*(summary|摘要|key[_\s-]*events?|关键事件)\s*===\s*$", re.IGNORECASE | re.MULTILINE)
        matches = list(marker_pattern.finditer(text))
        sections: dict[str, str] = {}
        for idx, match in enumerate(matches):
            label = match.group(1).lower().replace(" ", "_").replace("-", "_")
            start = match.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            content = text[start:end].strip()
            if label in {"summary", "摘要"}:
                sections["summary"] = content
            else:
                sections["key_events"] = content
        summary = sections.get("summary", "")
        key_events: list[str] = []
        k_text = sections.get("key_events", "")
        if k_text:
            for line in k_text.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith(("- ", "* ", "• ")):
                    line = line[2:].strip()
                elif re.match(r"^\d+[\.、]", line):
                    line = re.sub(r"^\d+[\.、]\s*", "", line)
                if line:
                    key_events.append(line)
        if not summary and not key_events:
            summary = text.strip()
        return summary, key_events

    def _apply_foreshadow_resolution_output(
        self,
        db: Database,
        project_id: int,
        chapter_id: int,
        output: str,
    ) -> dict[str, Any]:
        chapter = db.get_ai_chapter(chapter_id)
        if not chapter:
            raise AIServiceError("章节不存在")
        if int(chapter.get("project_id") or 0) != int(project_id):
            raise AIServiceError("章节不属于该项目")
        parsed = self._extract_json_object(output)
        resolved_records: list[dict[str, Any]] = []
        still_pending: list[int] = []
        warnings: list[str] = []
        for r in parsed.get("resolved") or []:
            try:
                fs_id = int(r.get("id"))
            except (TypeError, ValueError):
                warnings.append("模型返回了无效的伏笔 id，已跳过")
                continue
            db.update_ai_foreshadow(fs_id, {
                "status": "resolved",
                "resolved_chapter": chapter.get("chapter_number"),
                "notes": (r.get("evidence") or "")[:500],
            })
            resolved_records.append({"id": fs_id, "evidence": r.get("evidence", "")})
        still_pending = [int(x) for x in (parsed.get("still_pending") or []) if str(x).isdigit()]
        return {"resolved": resolved_records, "still_pending": still_pending, "warnings": warnings}

    def import_foreshadow_resolution_output(self, project_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        chapter_id = self._safe_int(payload.get("chapter_id"), 0, "chapter_id", min_value=0)
        if not chapter_id:
            raise AIServiceError("缺少 chapter_id")
        db = self._db()
        try:
            output = self._resolve_output_text(db, payload)
            return self._apply_foreshadow_resolution_output(db, project_id, chapter_id, output)
        finally:
            db.close()

    def stream_auto_resolve_foreshadows(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        """扫描章节正文，自动判定哪些 pending 伏笔被回收，更新数据库。"""
        db = self._db()
        job_id = uuid.uuid4().hex
        output_parts: list[str] = []
        job_created = False
        agent_id = self._safe_int(payload.get("agent_id"), 0, "agent_id", min_value=0)
        project_id = self._safe_int(payload.get("project_id"), 0, "project_id", min_value=0)
        chapter_id = self._safe_int(payload.get("chapter_id"), 0, "chapter_id", min_value=0)
        try:
            agent = self._load_agent_config(db, agent_id)
            provider_config = self._load_provider_config(db, agent.provider_id)
            chapter = db.get_ai_chapter(chapter_id)
            if not chapter or not (chapter.get("content") or "").strip():
                raise AIServiceError("章节内容为空")
            pending = db.list_ai_foreshadows(project_id, status="pending")
            if not pending:
                yield AIStreamChunk(type="metadata", data={"job_id": job_id, "skipped": True, "reason": "no_pending_foreshadows"})
                yield AIStreamChunk(type="done", data={"job_id": job_id, "resolved": [], "still_pending": []})
                return
            messages = build_foreshadow_resolve_messages(
                chapter_text=chapter["content"],
                pending_foreshadows=pending,
                chapter_number=chapter.get("chapter_number"),
            )
            db.create_ai_job(job_id, "resolve_foreshadow", agent.id, {
                "project_id": project_id, "chapter_id": chapter_id, "pending_count": len(pending),
            })
            job_created = True
            yield AIStreamChunk(type="metadata", data={"job_id": job_id, "pending_count": len(pending)})
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
            output = "".join(output_parts).strip()
            warnings: list[str] = []
            try:
                applied = self._apply_foreshadow_resolution_output(db, project_id, chapter_id, output)
                resolved_records = applied["resolved"]
                still_pending = applied["still_pending"]
                warnings = applied["warnings"]
            except AIServiceError:
                resolved_records = []
                still_pending = []
                warnings.append("模型返回的伏笔回收 JSON 无法解析，未更新伏笔状态")
            output_json = {"resolved": len(resolved_records)}
            if warnings:
                output_json["warnings"] = warnings
            db.update_ai_job(job_id, "succeeded", output_text=output, output_json=output_json)
            yield AIStreamChunk(type="done", data={"job_id": job_id, "resolved": resolved_records, "still_pending": still_pending, "warnings": warnings})
        except GeneratorExit:
            if job_created:
                db.update_ai_job(job_id, "cancelled", output_text="".join(output_parts), error_message="客户端断开连接")
            raise
        except Exception as exc:
            msg = str(exc)
            if job_created:
                db.update_ai_job(job_id, "failed", output_text="".join(output_parts), error_message=msg)
            yield AIStreamChunk(type="error", data={"message": msg})
        finally:
            db.close()

    def stream_polish(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        """polish_type='dialogue'|'psychology'。润色章节文本。"""
        polish_type = (payload.get("polish_type") or "dialogue").lower()
        if polish_type not in ("dialogue", "psychology"):
            raise AIServiceError("polish_type 必须是 dialogue 或 psychology")
        db = self._db()
        job_id = uuid.uuid4().hex
        output_parts: list[str] = []
        job_created = False
        agent_id = self._safe_int(payload.get("agent_id"), 0, "agent_id", min_value=0)
        try:
            agent = self._load_agent_config(db, agent_id)
            provider_config = self._load_provider_config(db, agent.provider_id)
            text = (payload.get("text") or "").strip()
            chapter_id = self._safe_int(payload.get("chapter_id"), 0, "chapter_id", min_value=0)
            if not text and chapter_id:
                ch = db.get_ai_chapter(chapter_id)
                if ch:
                    text = ch.get("content") or ""
            if not text:
                raise AIServiceError("没有可润色的文本")
            messages = build_polish_messages(
                polish_type=polish_type,
                text=text,
                extra_context=payload.get("extra_context"),
                instruction=payload.get("instruction"),
            )
            task_type = "polish_dialogue" if polish_type == "dialogue" else "polish_psychology"
            db.create_ai_job(job_id, task_type, agent.id, {"chapter_id": chapter_id, "chars": len(text)})
            job_created = True
            yield AIStreamChunk(type="metadata", data={"job_id": job_id, "polish_type": polish_type})
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
            msg = str(exc)
            if job_created:
                db.update_ai_job(job_id, "failed", output_text="".join(output_parts), error_message=msg)
            yield AIStreamChunk(type="error", data={"message": msg})
        finally:
            db.close()

    # ════════════════════════════════════════════════════════════════
    # 章节自动 Pipeline：编排续写→润色→去AI味→审计→摘要→更新状态→回收伏笔→索引→检测
    # ════════════════════════════════════════════════════════════════

    PIPELINE_STEP_ORDER = [
        "continue", "polish_dialogue", "polish_psychology", "deai",
        "summary", "state", "foreshadow", "audit", "detect", "index",
    ]
    PIPELINE_STEP_LABEL = {
        "continue": "续写", "polish_dialogue": "对话润色", "polish_psychology": "心理润色",
        "deai": "去AI味", "summary": "摘要+关键事件", "state": "更新项目状态",
        "foreshadow": "回收伏笔", "audit": "内容审计", "detect": "AI痕迹检测", "index": "索引入库",
    }

    def stream_chapter_pipeline(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        """章节自动 Pipeline。
        payload: {
          project_id, chapter_id,
          steps: ["continue","polish_dialogue",...,"index","detect"],   # 用户勾选启用的步骤（按 PIPELINE_STEP_ORDER 排序）
          agent_ids: { continue, polish_dialogue, polish_psychology, deai, summary, state, foreshadow, audit },
          # 续写参数：
          instruction, output_chars, plan_text, context_chars,
        }
        """
        project_id = int(payload.get("project_id") or 0)
        chapter_id = int(payload.get("chapter_id") or 0)
        if not project_id or not chapter_id:
            raise AIServiceError("缺少 project_id/chapter_id")
        steps_in = payload.get("steps") or []
        steps = [s for s in self.PIPELINE_STEP_ORDER if s in steps_in]
        if not steps:
            raise AIServiceError("未选择任何步骤")
        agent_ids = payload.get("agent_ids") or {}

        pipeline_id = uuid.uuid4().hex
        started = time.time()
        db = self._db()
        # 初始化 chapter.metadata.pipeline
        try:
            db.patch_ai_chapter_metadata(chapter_id, {
                "pipeline": {
                    "id": pipeline_id,
                    "status": "running",
                    "current_step": None,
                    "started_at": int(started),
                    "warnings": [],
                    "steps": [{"name": s, "status": "pending"} for s in steps],
                }
            })
        finally:
            db.close()

        yield AIStreamChunk(type="metadata", data={
            "pipeline_id": pipeline_id,
            "steps": [{"name": s, "label": self.PIPELINE_STEP_LABEL.get(s, s)} for s in steps],
        })

        latest_text: str | None = None  # 多步骤间共享章节正文（用于润色/去AI味/审计）

        def _emit_step(step_name: str, status: str, **extra) -> None:
            patch = {"name": step_name, "status": status, **extra}
            self._patch_pipeline_step(chapter_id, step_name, patch)

        failed_steps = 0
        skipped_steps = 0
        for idx, step in enumerate(steps):
            step_label = self.PIPELINE_STEP_LABEL.get(step, step)
            self._patch_pipeline(chapter_id, {"current_step": step})
            yield AIStreamChunk(type="custom", data={"event": "step_start", "step": step, "label": step_label, "index": idx})
            _emit_step(step, "running", started_at=int(time.time()))
            try:
                step_output = ""
                step_meta: dict[str, Any] = {}
                if step == "continue":
                    sub_payload = {
                        "agent_id": int(agent_ids.get("continue") or 0),
                        "project_id": project_id, "chapter_id": chapter_id,
                        "instruction": payload.get("instruction"),
                        "output_chars": payload.get("output_chars"),
                        "plan_text": payload.get("plan_text"),
                        "context_chars": payload.get("context_chars"),
                        "auto_save": False,
                    }
                    parts: list[str] = []
                    job_id = ""
                    for chunk in self.stream_chapter_continue(sub_payload):
                        if chunk.type == "metadata":
                            job_id = (chunk.data or {}).get("job_id") or ""
                        elif chunk.type == "delta":
                            parts.append(chunk.text)
                            yield AIStreamChunk(type="custom", data={"event": "delta", "step": step, "text": chunk.text})
                        elif chunk.type == "error":
                            raise AIServiceError((chunk.data or {}).get("message", "续写失败"))
                    step_output = "".join(parts)
                    if step_output:
                        latest_text = step_output
                        # 写回章节 content
                        sub_db = self._db()
                        try:
                            sub_db.update_ai_chapter(chapter_id, {"content": step_output, "status": "draft"})
                        finally:
                            sub_db.close()
                    step_meta = {"job_id": job_id, "chars": len(step_output)}

                elif step in ("polish_dialogue", "polish_psychology"):
                    sub_db = self._db()
                    try:
                        ch = sub_db.get_ai_chapter(chapter_id)
                        text = latest_text if latest_text is not None else (ch.get("content") if ch else "")
                    finally:
                        sub_db.close()
                    if not (text or "").strip():
                        skipped_steps += 1
                        _emit_step(step, "skipped", reason="no_text")
                        yield AIStreamChunk(type="custom", data={"event": "step_done", "step": step, "skipped": True})
                        continue
                    sub_payload = {
                        "agent_id": int(agent_ids.get(step) or 0),
                        "polish_type": "dialogue" if step == "polish_dialogue" else "psychology",
                        "text": text, "chapter_id": chapter_id,
                        "instruction": payload.get("instruction"),
                    }
                    parts = []
                    job_id = ""
                    for chunk in self.stream_polish(sub_payload):
                        if chunk.type == "metadata":
                            job_id = (chunk.data or {}).get("job_id") or ""
                        elif chunk.type == "delta":
                            parts.append(chunk.text)
                            yield AIStreamChunk(type="custom", data={"event": "delta", "step": step, "text": chunk.text})
                        elif chunk.type == "error":
                            raise AIServiceError((chunk.data or {}).get("message", "润色失败"))
                    step_output = "".join(parts)
                    if step_output.strip():
                        latest_text = step_output
                        sub_db = self._db()
                        try:
                            sub_db.update_ai_chapter(chapter_id, {"content": step_output})
                        finally:
                            sub_db.close()
                    step_meta = {"job_id": job_id, "chars": len(step_output)}

                elif step == "deai":
                    sub_db = self._db()
                    try:
                        ch = sub_db.get_ai_chapter(chapter_id)
                        text = latest_text if latest_text is not None else (ch.get("content") if ch else "")
                    finally:
                        sub_db.close()
                    if not (text or "").strip():
                        skipped_steps += 1
                        _emit_step(step, "skipped", reason="no_text")
                        yield AIStreamChunk(type="custom", data={"event": "step_done", "step": step, "skipped": True})
                        continue
                    sub_payload = {
                        "agent_id": int(agent_ids.get("deai") or 0),
                        "rewrite_type": "deai", "source_type": "manual", "text": text,
                        "instruction": payload.get("instruction"),
                    }
                    parts = []
                    job_id = ""
                    for chunk in self.stream_rewrite(sub_payload):
                        if chunk.type == "metadata":
                            job_id = (chunk.data or {}).get("job_id") or ""
                        elif chunk.type == "delta":
                            parts.append(chunk.text)
                            yield AIStreamChunk(type="custom", data={"event": "delta", "step": step, "text": chunk.text})
                        elif chunk.type == "error":
                            raise AIServiceError((chunk.data or {}).get("message", "去AI味失败"))
                    step_output = "".join(parts)
                    if step_output.strip():
                        latest_text = step_output
                        sub_db = self._db()
                        try:
                            sub_db.update_ai_chapter(chapter_id, {"content": step_output})
                        finally:
                            sub_db.close()
                    step_meta = {"job_id": job_id, "chars": len(step_output)}

                elif step == "summary":
                    sub_payload = {"agent_id": int(agent_ids.get("summary") or 0), "chapter_id": chapter_id}
                    job_id = ""
                    summary = ""
                    key_events: list[str] = []
                    for chunk in self.stream_extract_chapter_summary(sub_payload):
                        if chunk.type == "metadata":
                            job_id = (chunk.data or {}).get("job_id") or ""
                        elif chunk.type == "delta":
                            yield AIStreamChunk(type="custom", data={"event": "delta", "step": step, "text": chunk.text})
                        elif chunk.type == "done":
                            summary = (chunk.data or {}).get("summary", "")
                            key_events = (chunk.data or {}).get("key_events", []) or []
                        elif chunk.type == "error":
                            raise AIServiceError((chunk.data or {}).get("message", "摘要失败"))
                    step_meta = {"job_id": job_id, "summary_chars": len(summary), "events": len(key_events)}

                elif step == "state":
                    sub_payload = {"agent_id": int(agent_ids.get("state") or 0), "project_id": project_id, "chapter_id": chapter_id}
                    job_id = ""
                    for chunk in self.stream_update_project_state(sub_payload):
                        if chunk.type == "metadata":
                            job_id = (chunk.data or {}).get("job_id") or ""
                        elif chunk.type == "delta":
                            yield AIStreamChunk(type="custom", data={"event": "delta", "step": step, "text": chunk.text})
                        elif chunk.type == "error":
                            raise AIServiceError((chunk.data or {}).get("message", "状态更新失败"))
                    step_meta = {"job_id": job_id}

                elif step == "foreshadow":
                    sub_payload = {"agent_id": int(agent_ids.get("foreshadow") or 0), "project_id": project_id, "chapter_id": chapter_id}
                    job_id = ""
                    resolved: list[dict[str, Any]] = []
                    for chunk in self.stream_auto_resolve_foreshadows(sub_payload):
                        if chunk.type == "metadata":
                            job_id = (chunk.data or {}).get("job_id") or ""
                            if (chunk.data or {}).get("skipped"):
                                skipped_steps += 1
                                _emit_step(step, "skipped", reason=(chunk.data or {}).get("reason", ""))
                                yield AIStreamChunk(type="custom", data={"event": "step_done", "step": step, "skipped": True})
                                break
                        elif chunk.type == "delta":
                            yield AIStreamChunk(type="custom", data={"event": "delta", "step": step, "text": chunk.text})
                        elif chunk.type == "done":
                            resolved = (chunk.data or {}).get("resolved", []) or []
                        elif chunk.type == "error":
                            raise AIServiceError((chunk.data or {}).get("message", "伏笔回收失败"))
                    else:
                        step_meta = {"job_id": job_id, "resolved": len(resolved)}
                        _emit_step(step, "done", finished_at=int(time.time()), **step_meta)
                        yield AIStreamChunk(type="custom", data={"event": "step_done", "step": step, "meta": step_meta})
                        continue

                elif step == "audit":
                    sub_db = self._db()
                    try:
                        ch = sub_db.get_ai_chapter(chapter_id)
                        text = latest_text if latest_text is not None else (ch.get("content") if ch else "")
                    finally:
                        sub_db.close()
                    if not (text or "").strip():
                        skipped_steps += 1
                        _emit_step(step, "skipped", reason="no_text")
                        yield AIStreamChunk(type="custom", data={"event": "step_done", "step": step, "skipped": True})
                        continue
                    sub_payload = {"agent_id": int(agent_ids.get("audit") or 0), "source_type": "manual", "text": text}
                    parts = []
                    job_id = ""
                    for chunk in self.stream_audit(sub_payload):
                        if chunk.type == "metadata":
                            job_id = (chunk.data or {}).get("job_id") or ""
                        elif chunk.type == "delta":
                            parts.append(chunk.text)
                            yield AIStreamChunk(type="custom", data={"event": "delta", "step": step, "text": chunk.text})
                        elif chunk.type == "error":
                            raise AIServiceError((chunk.data or {}).get("message", "审计失败"))
                    audit_text = "".join(parts)
                    db2 = self._db()
                    try:
                        db2.patch_ai_chapter_metadata(chapter_id, {"audit_report": audit_text[:50000], "audit_job_id": job_id})
                    finally:
                        db2.close()
                    step_meta = {"job_id": job_id, "chars": len(audit_text)}

                elif step == "detect":
                    sub_db = self._db()
                    try:
                        ch = sub_db.get_ai_chapter(chapter_id)
                        text = latest_text if latest_text is not None else (ch.get("content") if ch else "")
                    finally:
                        sub_db.close()
                    if not (text or "").strip():
                        skipped_steps += 1
                        _emit_step(step, "skipped", reason="no_text")
                        yield AIStreamChunk(type="custom", data={"event": "step_done", "step": step, "skipped": True})
                        continue
                    report = detect_ai_tells(text)
                    # AITellReport 是 dataclass，必须用属性访问
                    score = float(report.score or 0)
                    issues_dump = [
                        {"type": i.type, "severity": i.severity, "message": i.message, "detail": i.detail}
                        for i in (report.issues or [])
                    ]
                    db2 = self._db()
                    try:
                        db2.patch_ai_chapter_metadata(chapter_id, {
                            "ai_score": score,
                            "ai_tells": issues_dump[:20],
                        })
                    finally:
                        db2.close()
                    step_meta = {"score": score, "issues": len(issues_dump)}
                    yield AIStreamChunk(type="custom", data={"event": "step_done", "step": step, "meta": step_meta})

                elif step == "index":
                    try:
                        self.index_chapter_for_retrieval(project_id, chapter_id)
                        step_meta = {"indexed": True}
                    except Exception as e:
                        step_meta = {"indexed": False, "error": str(e)}
                        self._append_pipeline_warning(chapter_id, {"step": step, "message": str(e)})

                _emit_step(step, "done", finished_at=int(time.time()), **step_meta)
                yield AIStreamChunk(type="custom", data={"event": "step_done", "step": step, "meta": step_meta})

            except AIServiceError as e:
                failed_steps += 1
                self._append_pipeline_warning(chapter_id, {"step": step, "message": str(e)})
                _emit_step(step, "failed", error=str(e), finished_at=int(time.time()))
                yield AIStreamChunk(type="custom", data={"event": "step_failed", "step": step, "error": str(e), "label": step_label})
                # 单步失败不中止整个 pipeline，继续后续步骤（用户可事后重试单步）
                continue
            except Exception as e:
                failed_steps += 1
                self._append_pipeline_warning(chapter_id, {"step": step, "message": str(e)})
                _emit_step(step, "failed", error=str(e), finished_at=int(time.time()))
                yield AIStreamChunk(type="custom", data={"event": "step_failed", "step": step, "error": str(e), "label": step_label})
                continue

        elapsed = int(time.time() - started)
        pipeline_status = "partial" if failed_steps else "succeeded"
        self._patch_pipeline(chapter_id, {
            "status": pipeline_status,
            "current_step": None,
            "finished_at": int(time.time()),
            "duration_sec": elapsed,
            "failed_steps": failed_steps,
            "skipped_steps": skipped_steps,
        })
        db_final = self._db()
        try:
            db_final.patch_ai_chapter_metadata(chapter_id, {
                "pipeline_finished_at": int(time.time()),
                "pipeline_duration_sec": elapsed,
            })
        finally:
            db_final.close()
        yield AIStreamChunk(type="done", data={"pipeline_id": pipeline_id, "chapter_id": chapter_id, "duration_sec": elapsed, "status": pipeline_status})

    def _patch_pipeline(self, chapter_id: int, patch: dict[str, Any]) -> None:
        db = self._db()
        try:
            ch = db.get_ai_chapter(chapter_id)
            if not ch:
                return
            meta = ch.get("metadata") or {}
            pipeline = meta.get("pipeline") or {"steps": []}
            pipeline.update(patch or {})
            db.patch_ai_chapter_metadata(chapter_id, {"pipeline": pipeline})
        finally:
            db.close()

    def _append_pipeline_warning(self, chapter_id: int, warning: dict[str, Any]) -> None:
        db = self._db()
        try:
            ch = db.get_ai_chapter(chapter_id)
            if not ch:
                return
            meta = ch.get("metadata") or {}
            pipeline = meta.get("pipeline") or {"steps": []}
            warnings = list(pipeline.get("warnings") or [])
            warnings.append({"created_at": int(time.time()), **(warning or {})})
            pipeline["warnings"] = warnings[-50:]
            db.patch_ai_chapter_metadata(chapter_id, {"pipeline": pipeline})
        finally:
            db.close()

    def _patch_pipeline_step(self, chapter_id: int, step_name: str, patch: dict[str, Any]) -> None:
        db = self._db()
        try:
            ch = db.get_ai_chapter(chapter_id)
            if not ch:
                return
            meta = ch.get("metadata") or {}
            pipeline = meta.get("pipeline") or {"steps": []}
            steps_list = pipeline.get("steps") or []
            updated = False
            for s in steps_list:
                if s.get("name") == step_name:
                    s.update(patch)
                    updated = True
                    break
            if not updated:
                steps_list.append({"name": step_name, **patch})
            pipeline["steps"] = steps_list
            db.patch_ai_chapter_metadata(chapter_id, {"pipeline": pipeline})
        finally:
            db.close()

    def get_chapter_dashboard(self, chapter_id: int) -> dict[str, Any]:
        """聚合产出面板数据。"""
        db = self._db()
        try:
            chapter = db.get_ai_chapter(chapter_id)
            if not chapter:
                raise AIServiceError("章节不存在")
            project_id = int(chapter["project_id"])
            cn = int(chapter["chapter_number"])
            states = db.get_all_project_states(project_id)
            planted_here = [f for f in db.list_ai_foreshadows(project_id) if f.get("planted_chapter") == cn]
            resolved_here = [f for f in db.list_ai_foreshadows(project_id) if f.get("resolved_chapter") == cn]
            return {
                "chapter": chapter,
                "project_states": states,
                "foreshadows_planted": planted_here,
                "foreshadows_resolved": resolved_here,
            }
        finally:
            db.close()


# ── 内置 Agent 系统提示常量（供 seed_builtin_agents 使用） ──────────

_SUMMARY_AGENT_PROMPT = """你是专业的小说章节信息提取助手。
任务：从用户给出的章节正文中，提取「章节摘要」和「关键事件清单」。

【输出格式 - 严格按以下格式】
=== summary ===
（200-400 字的章节摘要：本章发生了什么 / 角色状态变化 / 主要冲突 / 关系推进 / 留下的悬念）

=== key_events ===
- 事件 1（一句话，主语+动作+对象，可含场景）
- 事件 2
- 事件 3
（共 3-8 条，按时间顺序，每条不超过 60 字）

【原则】
- 只提取已发生的客观事件，不分析、不评价
- 关键事件 = 推动后续剧情/改变人物关系/揭示新信息/埋下/回收伏笔
"""

_FORESHADOW_AGENT_PROMPT = """你是专业的小说伏笔追踪助手。
任务：分析「最新章节正文」与「待回收伏笔列表」，判断哪些伏笔在本章已被回收。

【判定原则】
- 必须有正文中可引用的明确证据，才能认定回收
- 暗示性回收也算，但要在 evidence 中说明
- 宁缺毋滥
- 长期承诺类伏笔仅当本章实质性兑现时才回收

【输出格式 - 严格输出 JSON，不要任何其他文字】
{"resolved":[{"id":<id>,"evidence":"<证据引用>"}],"still_pending":[<未回收 id 列表>]}
"""

_POLISH_DIALOGUE_AGENT_PROMPT = """你是专业小说对话润色专家。
任务：只对章节文本中的对话部分做润色优化，不改剧情骨架、不改非对话叙述。

【润色目标】
1. 对话符合发言者身份/性格/语言档案
2. 对话有信息量，避免空话
3. 适度穿插动作/表情/环境，避免连续超过 5 句纯对话
4. 保留原对话的剧情功能
5. 长短句交替

【输出要求】
- 输出完整的章节文本（润色后的对话+未改动的叙述）
- 不要解释你改了什么
"""

_POLISH_PSYCHOLOGY_AGENT_PROMPT = """你是专业小说心理描写润色专家。
任务：只对章节文本中的心理描写部分做润色优化，不改剧情、不改对话内容。

【润色目标】
1. 抽象心理 → 具体感官（"她很紧张" → "她攥紧裙摆，听见自己心跳"）
2. 大段直白心理独白 → 用动作/微表情/呼吸/视线穿插表达
3. 删除"心想/暗道"类直白标识
4. 保留所有信息密度
5. 视角统一

【输出要求】
- 输出完整的章节文本
- 不要解释你改了什么
"""
