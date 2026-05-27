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
    build_plan_messages,
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
                plan_text=payload.get("plan_text"),
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
        keys = ["name", "provider_type", "base_url", "default_model", "available_models", "timeout_seconds", "max_retries", "proxy", "context_window", "enabled"]
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
        if data.get("task_type") not in {None, "continue", "rewrite", "distill_style", "distill_novel", "audit", "general", "plan"}:
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
            novels = db.conn.execute(
                """
                SELECT nt.text_raw, nt.text_markdown, n.title, n.create_date
                FROM novels n
                LEFT JOIN novel_texts nt ON nt.novel_id = n.novel_id
                WHERE n.series_id = ?
                ORDER BY n.create_date ASC
                """,
                (series_id,),
            ).fetchall()
            if not novels:
                raise AIServiceError("系列下没有找到小说")
            parts: list[str] = []
            for row in novels:
                r = dict(row)
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

            # 确定每批大小
            effective_window = agent.context_window if agent.context_window > 16000 else provider_config.context_window
            usable_chars = int(effective_window * 1.5 * 0.7)
            batch_size = min(15, max(6, usable_chars // chunk_char_size))

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

            effective_window = agent.context_window if agent.context_window > 16000 else provider_config.context_window
            usable_chars = int(effective_window * 1.5 * 0.8)
            batch_size = min(25, max(10, usable_chars // chunk_char_size))

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

    # ── 写前构思 ────────────────────────────────────────────────

    def stream_plan(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        """生成续写前的章节构思。"""
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
            messages = build_plan_messages(
                system_prompt=agent.system_prompt,
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
                       provider_config: AIProviderConfig, model: str) -> str:
        """智能上下文处理：超长时自动分段摘要 + 末尾上下文。

        分层策略：
        - 短文本（<= 60% 窗口）：原样返回
        - 长文本：分段摘要前文 + 保留尾部 30% 字符作为续接锚点
        - 分段摘要：每 8000 字一段，避免长文摘要时丢失中段信息
        """
        est_tokens = estimate_token_count(text)
        max_tokens = context_window * 0.6  # 留 40% 给输出和 prompt
        if est_tokens <= max_tokens:
            return text
        # 保留尾部 30% 字符作为续接锚点（含最近的完整场景）
        tail_chars = int(context_window * 0.3)
        tail = get_tail_context(text, tail_chars)
        head = text[:len(text) - len(tail)]
        if not head.strip():
            return tail
        # 分段摘要：每 8000 字一段
        segment_size = 8000
        segments = [head[i:i + segment_size] for i in range(0, len(head), segment_size)]
        provider = create_provider(provider_config)
        summary_parts: list[str] = []
        for idx, seg in enumerate(segments, 1):
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
            return f"【前文摘要】\n{summary}\n\n【最近原文】\n{tail}"
        return tail
