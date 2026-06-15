from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ...storage_db import Database
from ..chunking import estimate_token_count, get_tail_context, split_text_by_chars
from ..crypto import AISecretManager
from ..detection import detect_ai_tells
from ..models import AIAgentConfig, AIProviderConfig, AIStreamChunk
from ..prompts import (
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
from ..providers import AIProvider, create_provider
from ..retrieval import BaseRetriever, create_retriever
from .core import AIServiceError


class AIGenerationMixin:
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
            provider = self._get_provider(provider_config)
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
            provider = self._get_provider(provider_config)
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
            provider = self._get_provider(provider_config)

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
            provider = self._get_provider(provider_config)

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
            provider = self._get_provider(provider_config)
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
            provider = self._get_provider(provider_config)
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
        provider = self._get_provider(provider_config)
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
