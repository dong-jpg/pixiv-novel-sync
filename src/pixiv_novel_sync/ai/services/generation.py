from __future__ import annotations

import time
from collections.abc import Generator, Iterator
from dataclasses import replace
from typing import Any

from ..chunking import estimate_token_count, get_tail_context, split_text_by_chars
from ..detection import detect_ai_tells
from ..model_router import PromptBudget, RouteResult
from ..models import AIStreamChunk
from ..prompts import (
    build_audit_messages,
    build_continue_messages,
    build_keyword_clean_messages,
    build_novel_distill_messages,
    build_plan_messages,
    build_rewrite_messages,
    build_style_distill_messages,
    build_summarize_messages,
)
from .core import AIServiceError, RouteJobContext


class AIGenerationMixin:
    def _forward_route(
        self,
        context: RouteJobContext,
        messages: list[dict[str, str]],
        output_parts: list[str],
        *,
        stage: str = "main",
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        forward_delta: bool = True,
    ) -> Generator[AIStreamChunk, None, RouteResult]:
        stream = self._stream_route(
            context,
            messages,
            stage=stage,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
        )
        try:
            while True:
                try:
                    chunk = next(stream)
                except StopIteration as stopped:
                    return stopped.value
                if chunk.type == "delta":
                    output_parts.append(chunk.text)
                    if forward_delta:
                        yield chunk
                elif chunk.type == "progress":
                    yield chunk
        except GeneratorExit:
            stream.close()
            raise

    @staticmethod
    def _route_result_message(result: RouteResult) -> str:
        for attempt in reversed(result.attempts):
            message = attempt.get("error_message")
            if message:
                return str(message)
        if result.finish_state == "partial":
            return "生成结果不完整，已保留部分正文"
        if result.finish_state == "cancelled":
            return "生成任务已取消"
        return "所有候选模型均不可用"

    def _conclude_route(
        self,
        db: Any,
        context: RouteJobContext,
        result: RouteResult,
        *,
        output_json: dict[str, Any] | None = None,
        done_data: dict[str, Any] | None = None,
    ) -> AIStreamChunk:
        output = result.output_text
        if result.finish_state == "succeeded":
            self._finish_route_job(
                db,
                context,
                "succeeded",
                output,
                output_json=output_json,
            )
            data = {"job_id": context.job_id, "chars": len(output)}
            if done_data:
                data.update(done_data)
            return AIStreamChunk(type="done", data=data)

        message = self._route_result_message(result)
        if result.finish_state == "partial" and output:
            self._finish_route_job(
                db,
                context,
                "partial",
                output,
                error_message=message,
            )
        elif result.finish_state == "cancelled":
            self._cancel_route_job(db, context, message)
        else:
            self._finish_route_job(
                db,
                context,
                "failed",
                output,
                error_message=message,
            )
        return AIStreamChunk(type="error", data={"message": message})

    def stream_continue(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        db = self._db()
        output_parts: list[str] = []
        route_context: RouteJobContext | None = None
        agent_id = int(payload.get("agent_id") or 0)
        try:
            agent = self._load_agent_config(db, agent_id)
            context = self._resolve_input_text(db, payload)
            smart = bool(payload.get("smart_context", True))
            context_chars = int(payload.get("context_chars") or agent.context_window)
            budget_messages = build_continue_messages(
                system_prompt=agent.system_prompt,
                context="",
                instruction=payload.get("instruction"),
                output_chars=payload.get("output_chars"),
                style_prompt=payload.get("style_prompt"),
                novel_prompt=payload.get("novel_prompt"),
                plan_text=payload.get("plan_text"),
            )
            route_context = self._start_route_job(
                db,
                "continue",
                agent,
                {
                    **payload,
                    "input_context_chars": len(context),
                    "smart_context": smart,
                    "requested_context_chars": context_chars,
                },
                messages=budget_messages,
                max_tokens=agent.max_tokens,
                preference_payload=payload,
            )
            yield AIStreamChunk(
                type="metadata",
                data={"job_id": route_context.job_id},
            )
            effective_chars = max(
                1,
                min(context_chars, route_context.prompt_budget.input_budget),
            )
            if smart:
                effective_budget = replace(
                    route_context.prompt_budget,
                    input_budget=effective_chars,
                )
                for item in self._smart_context(
                    context,
                    effective_budget,
                    route_context,
                ):
                    if isinstance(item, AIStreamChunk):
                        yield item
                    else:
                        context = item
            else:
                context = get_tail_context(context, effective_chars)
            messages = build_continue_messages(
                system_prompt=agent.system_prompt,
                context=context,
                instruction=payload.get("instruction"),
                output_chars=payload.get("output_chars"),
                style_prompt=payload.get("style_prompt"),
                novel_prompt=payload.get("novel_prompt"),
                plan_text=payload.get("plan_text"),
            )
            result = yield from self._forward_route(
                route_context,
                messages,
                output_parts,
            )
            output = result.output_text
            yield self._conclude_route(
                db,
                route_context,
                result,
                output_json={"chars": len(output)},
            )
        except GeneratorExit:
            if route_context is not None:
                self._cancel_route_job(db, route_context, "客户端断开连接")
            raise
        except Exception as exc:
            message = str(exc)
            if route_context is not None:
                self._finish_route_job(
                    db,
                    route_context,
                    "failed",
                    "".join(output_parts),
                    error_message=message,
                )
            yield AIStreamChunk(type="error", data={"message": message})
        finally:
            db.close()

    def stream_rewrite(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        db = self._db()
        output_parts: list[str] = []
        route_context: RouteJobContext | None = None
        agent_id = int(payload.get("agent_id") or 0)
        try:
            agent = self._load_agent_config(db, agent_id)
            text = self._resolve_input_text(db, payload)
            budget_messages = build_rewrite_messages(
                system_prompt=agent.system_prompt,
                text="",
                rewrite_type=payload.get("rewrite_type"),
                instruction=payload.get("instruction"),
            )
            route_context = self._start_route_job(
                db,
                "rewrite",
                agent,
                {**payload, "resolved_text_chars": len(text)},
                messages=budget_messages,
                max_tokens=agent.max_tokens,
                preference_payload=payload,
            )
            yield AIStreamChunk(
                type="metadata",
                data={"job_id": route_context.job_id},
            )
            text = get_tail_context(text, route_context.prompt_budget.input_budget)
            messages = build_rewrite_messages(
                system_prompt=agent.system_prompt,
                text=text,
                rewrite_type=payload.get("rewrite_type"),
                instruction=payload.get("instruction"),
            )
            result = yield from self._forward_route(
                route_context,
                messages,
                output_parts,
            )
            output = result.output_text
            yield self._conclude_route(
                db,
                route_context,
                result,
                output_json={"chars": len(output)},
            )
        except GeneratorExit:
            if route_context is not None:
                self._cancel_route_job(db, route_context, "客户端断开连接")
            raise
        except Exception as exc:
            message = str(exc)
            if route_context is not None:
                self._finish_route_job(
                    db,
                    route_context,
                    "failed",
                    "".join(output_parts),
                    error_message=message,
                )
            yield AIStreamChunk(type="error", data={"message": message})
        finally:
            db.close()

    def stream_distill_style(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        db = self._db()
        output_parts: list[str] = []
        route_context: RouteJobContext | None = None
        agent_id = int(payload.get("agent_id") or 0)
        try:
            agent = self._load_agent_config(db, agent_id)
            text = self._resolve_input_text(db, payload)
            chunk_char_size = int(payload.get("chunk_chars") or 4000)
            all_chunks = split_text_by_chars(text, chunk_char_size)
            full_text_mode = bool(payload.get("full_text", False))

            # 确定每批大小：优先使用用户指定的 batch_size，否则自动计算
            user_batch_size = int(payload.get("batch_size") or 0)
            if user_batch_size > 0:
                batch_size = user_batch_size
            else:
                usable_chars = int(agent.context_window * 1.5 * 0.7)
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

            first_messages = build_style_distill_messages(
                system_prompt=agent.system_prompt,
                text_chunks=batches[0],
                existing_profile=existing_profile,
            )
            route_context = self._start_route_job(
                db,
                "distill_style",
                agent,
                {
                    **payload,
                    "chunks_count": len(all_chunks),
                    "batches": len(batches),
                    "mode": "full" if full_text_mode else "sample",
                },
                messages=first_messages,
                max_tokens=agent.max_tokens,
            )
            yield AIStreamChunk(
                type="metadata",
                data={"job_id": route_context.job_id, "batches": len(batches)},
            )

            last_result: RouteResult | None = None
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
                yield AIStreamChunk(
                    type="progress",
                    data={
                        "phase": "batch",
                        "batch": batch_idx + 1,
                        "total": len(batches),
                    },
                )

                batch_output: list[str] = []
                result = yield from self._forward_route(
                    route_context,
                    messages,
                    batch_output,
                    forward_delta=is_last,
                )
                last_result = result
                if result.finish_state != "succeeded":
                    terminal_result = replace(
                        result,
                        output_text="".join(output_parts) + result.output_text,
                    )
                    yield self._conclude_route(db, route_context, terminal_result)
                    return

                batch_text = result.output_text or "".join(batch_output)
                if not is_last:
                    # 中间批次：用输出作为下一批的 existing_profile
                    existing_profile = batch_text
                else:
                    output_parts.append(batch_text)

            output = "".join(output_parts)
            assert last_result is not None
            yield self._conclude_route(
                db,
                route_context,
                replace(last_result, output_text=output),
                output_json={"chars": len(output)},
            )
        except GeneratorExit:
            if route_context is not None:
                self._cancel_route_job(db, route_context, "客户端断开连接")
            raise
        except Exception as exc:
            message = str(exc)
            if route_context is not None:
                self._finish_route_job(
                    db,
                    route_context,
                    "failed",
                    "".join(output_parts),
                    error_message=message,
                )
            yield AIStreamChunk(type="error", data={"message": message})
        finally:
            db.close()

    def stream_distill_novel(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        db = self._db()
        output_parts: list[str] = []
        route_context: RouteJobContext | None = None
        agent_id = int(payload.get("agent_id") or 0)
        try:
            agent = self._load_agent_config(db, agent_id)
            text = self._resolve_input_text(db, payload)
            chunk_char_size = int(payload.get("chunk_chars") or 4000)
            all_chunks = split_text_by_chars(text, chunk_char_size)
            full_text_mode = bool(payload.get("full_text", False))

            user_batch_size = int(payload.get("batch_size") or 0)
            if user_batch_size > 0:
                batch_size = user_batch_size
            else:
                usable_chars = int(agent.context_window * 1.5 * 0.8)
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

            first_messages = build_novel_distill_messages(
                system_prompt=agent.system_prompt,
                text_chunks=batches[0],
                existing_profile=existing_profile,
            )
            route_context = self._start_route_job(
                db,
                "distill_novel",
                agent,
                {
                    **payload,
                    "chunks_count": len(all_chunks),
                    "batches": len(batches),
                    "mode": "full" if full_text_mode else "sample",
                },
                messages=first_messages,
                max_tokens=agent.max_tokens,
            )
            yield AIStreamChunk(
                type="metadata",
                data={"job_id": route_context.job_id, "batches": len(batches)},
            )

            last_result: RouteResult | None = None
            for batch_idx, batch_chunks in enumerate(batches):
                is_last = batch_idx == len(batches) - 1
                if batch_idx > 0:
                    time.sleep(2)
                messages = build_novel_distill_messages(
                    system_prompt=agent.system_prompt,
                    text_chunks=batch_chunks,
                    existing_profile=existing_profile,
                )
                yield AIStreamChunk(
                    type="progress",
                    data={
                        "phase": "batch",
                        "batch": batch_idx + 1,
                        "total": len(batches),
                    },
                )

                batch_output: list[str] = []
                result = yield from self._forward_route(
                    route_context,
                    messages,
                    batch_output,
                    forward_delta=is_last,
                )
                last_result = result
                if result.finish_state != "succeeded":
                    terminal_result = replace(
                        result,
                        output_text="".join(output_parts) + result.output_text,
                    )
                    yield self._conclude_route(db, route_context, terminal_result)
                    return

                batch_text = result.output_text or "".join(batch_output)
                if not is_last:
                    existing_profile = batch_text
                else:
                    output_parts.append(batch_text)
            output = "".join(output_parts)
            assert last_result is not None
            yield self._conclude_route(
                db,
                route_context,
                replace(last_result, output_text=output),
                output_json={"chars": len(output)},
            )
        except GeneratorExit:
            if route_context is not None:
                self._cancel_route_job(db, route_context, "客户端断开连接")
            raise
        except Exception as exc:
            message = str(exc)
            if route_context is not None:
                self._finish_route_job(
                    db,
                    route_context,
                    "failed",
                    "".join(output_parts),
                    error_message=message,
                )
            yield AIStreamChunk(type="error", data={"message": message})
        finally:
            db.close()

    def stream_audit(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        db = self._db()
        output_parts: list[str] = []
        route_context: RouteJobContext | None = None
        agent_id = int(payload.get("agent_id") or 0)
        try:
            agent = self._load_agent_config(db, agent_id)
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

            budget_messages = build_audit_messages(
                system_prompt=agent.system_prompt,
                text="",
                audit_dimensions=payload.get("audit_dimensions"),
                rule_detection_context=rule_context,
            )
            route_context = self._start_route_job(
                db,
                "audit",
                agent,
                {
                    **payload,
                    "text_chars": len(text),
                    "rule_score": rule_report.score,
                    "rule_issues_count": len(rule_report.issues),
                },
                messages=budget_messages,
                max_tokens=agent.max_tokens,
                preference_payload=payload,
            )
            yield AIStreamChunk(type="metadata", data={
                "job_id": route_context.job_id,
                "rule_detection": {"score": rule_report.score, "issues_count": len(rule_report.issues)},
            })
            text = get_tail_context(text, route_context.prompt_budget.input_budget)
            messages = build_audit_messages(
                system_prompt=agent.system_prompt,
                text=text,
                audit_dimensions=payload.get("audit_dimensions"),
                rule_detection_context=rule_context,
            )
            result = yield from self._forward_route(
                route_context,
                messages,
                output_parts,
            )
            output = result.output_text
            yield self._conclude_route(
                db,
                route_context,
                result,
                output_json={"chars": len(output)},
            )
        except GeneratorExit:
            if route_context is not None:
                self._cancel_route_job(db, route_context, "客户端断开连接")
            raise
        except Exception as exc:
            message = str(exc)
            if route_context is not None:
                self._finish_route_job(
                    db,
                    route_context,
                    "failed",
                    "".join(output_parts),
                    error_message=message,
                )
            yield AIStreamChunk(type="error", data={"message": message})
        finally:
            db.close()

    def stream_plan(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        """生成续写前的章节构思。"""
        from ..prompts import DEFAULT_PLAN_PROMPT
        db = self._db()
        output_parts: list[str] = []
        route_context: RouteJobContext | None = None
        agent_id = int(payload.get("agent_id") or 0)
        try:
            agent = self._load_agent_config(db, agent_id)
            context = self._resolve_input_text(db, payload)
            # 构思任务只看最近的内容即可，不需要全文摘要
            context_chars = int(payload.get("context_chars") or 8000)
            # 如果 Agent 不是 plan 类型，强制使用构思专用 prompt
            system_prompt = agent.system_prompt
            if agent.task_type != "plan":
                system_prompt = DEFAULT_PLAN_PROMPT
            budget_messages = build_plan_messages(
                system_prompt=system_prompt,
                context="",
                instruction=payload.get("instruction"),
                novel_prompt=payload.get("novel_prompt"),
            )
            route_context = self._start_route_job(
                db,
                "plan",
                agent,
                {**payload, "input_context_chars": len(context)},
                messages=budget_messages,
                max_tokens=agent.max_tokens,
                preference_payload=payload,
            )
            yield AIStreamChunk(
                type="metadata",
                data={"job_id": route_context.job_id},
            )
            context = get_tail_context(
                context,
                min(context_chars, route_context.prompt_budget.input_budget),
            )
            messages = build_plan_messages(
                system_prompt=system_prompt,
                context=context,
                instruction=payload.get("instruction"),
                novel_prompt=payload.get("novel_prompt"),
            )
            result = yield from self._forward_route(
                route_context,
                messages,
                output_parts,
            )
            output = result.output_text
            yield self._conclude_route(
                db,
                route_context,
                result,
                output_json={
                    "chars": len(output),
                    "resolved_context_chars": len(context),
                },
            )
        except GeneratorExit:
            if route_context is not None:
                self._cancel_route_job(db, route_context, "客户端断开连接")
            raise
        except Exception as exc:
            message = str(exc)
            if route_context is not None:
                self._finish_route_job(
                    db,
                    route_context,
                    "failed",
                    "".join(output_parts),
                    error_message=message,
                )
            yield AIStreamChunk(type="error", data={"message": message})
        finally:
            db.close()

    def _smart_context(
        self,
        text: str,
        prompt_budget: PromptBudget,
        route_context: RouteJobContext,
    ) -> Iterator[AIStreamChunk | str]:
        """智能上下文处理：超长时自动分段摘要 + 末尾上下文。

        作为生成器使用：yield AIStreamChunk(type="progress") 表示进度，
        最后 yield 一个 str 表示最终结果。调用方需迭代并区分类型。

        分层策略：
        - 短文本（<= 60% 窗口）：原样返回
        - 长文本：分段摘要前文 + 保留尾部 30% 字符作为续接锚点
        - 分段摘要：每 8000 字一段，避免长文摘要时丢失中段信息
        """
        est_tokens = estimate_token_count(text)
        max_tokens = prompt_budget.input_budget
        if est_tokens <= max_tokens:
            yield get_tail_context(text, max_tokens)
            return
        # 保留尾部 30% 字符作为续接锚点（含最近的完整场景）
        tail_chars = max(1, int(max_tokens * 0.3))
        tail = get_tail_context(text, tail_chars)
        head = text[:len(text) - len(tail)]
        if not head.strip():
            yield tail
            return
        # 分段摘要：每 8000 字一段
        segment_size = 8000
        segments = [head[i:i + segment_size] for i in range(0, len(head), segment_size)]
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
            result = yield from self._forward_route(
                route_context,
                messages,
                seg_summary,
                stage="internal",
                temperature=0.3,
                top_p=0.9,
                max_tokens=min(800, prompt_budget.output_reserve),
                forward_delta=False,
            )
            if result.finish_state != "succeeded":
                yield tail
                return
            seg_text = (result.output_text or "".join(seg_summary)).strip()
            if seg_text:
                if len(segments) > 1:
                    summary_parts.append(f"[第 {idx} 段摘要]\n{seg_text}")
                else:
                    summary_parts.append(seg_text)
        summary = "\n\n".join(summary_parts)
        if summary:
            labels = "【前文摘要】\n\n\n【最近原文】\n"
            summary_chars = max(0, max_tokens - len(tail) - len(labels))
            if summary_chars > 0:
                bounded_summary = get_tail_context(summary, summary_chars)
                yield f"【前文摘要】\n{bounded_summary}\n\n【最近原文】\n{tail}"
            else:
                yield tail
        else:
            yield tail

    def clean_keywords(
        self,
        raw_keywords: list[str],
        tags: list[str] | None = None,
        agent_id: int | None = None,
    ) -> dict[str, Any] | None:
        """#10：用 AI 把机械分词得到的噪声高频词清洗成可搜索关键词（同步调用）。

        优雅降级：未配置可用 Provider/Agent、调用失败或解析失败时返回 None，
        调用方应保留原始 top_keywords 不受影响。返回
        {"keywords": [...], "dropped_sample": [...]}。
        """
        import json
        import re

        raw_keywords = [str(k).strip() for k in (raw_keywords or []) if str(k).strip()]
        if not raw_keywords:
            return None

        db = self._db()
        output_parts: list[str] = []
        route_context: RouteJobContext | None = None
        try:
            # 选 Agent：优先 keyword_clean，其次 general，最后任意已启用 Agent。
            agent_row = None
            agents = db.list_ai_agents()
            enabled = [a for a in agents if a.get("enabled")]
            if agent_id:
                agent_row = next((a for a in enabled if int(a["id"]) == int(agent_id)), None)
            if agent_row is None:
                for pref in ("keyword_clean", "general"):
                    agent_row = next((a for a in enabled if a.get("task_type") == pref), None)
                    if agent_row:
                        break
            if agent_row is None and enabled:
                agent_row = enabled[0]
            if agent_row is None:
                return None  # 无可用 agent，降级

            agent = self._load_agent_config(db, int(agent_row["id"]))
            messages = build_keyword_clean_messages(raw_keywords=raw_keywords[:80], tags=(tags or [])[:40])
            route_context = self._start_route_job(
                db,
                "keyword_clean",
                agent,
                {
                    "raw_keywords": raw_keywords[:80],
                    "tags": (tags or [])[:40],
                },
                messages=messages,
                max_tokens=1500,
            )
            stream = self._forward_route(
                route_context,
                messages,
                output_parts,
                temperature=0.2,
                top_p=0.9,
                max_tokens=min(1500, route_context.prompt_budget.output_reserve),
                forward_delta=False,
            )
            while True:
                try:
                    next(stream)
                except StopIteration as stopped:
                    result = stopped.value
                    break

            output = (result.output_text or "".join(output_parts)).strip()
            if result.finish_state != "succeeded":
                status = (
                    "partial"
                    if result.finish_state == "partial" and output
                    else "cancelled"
                    if result.finish_state == "cancelled"
                    else "failed"
                )
                self._finish_route_job(
                    db,
                    route_context,
                    status,
                    output,
                    error_message=self._route_result_message(result),
                )
                return None
            if not output:
                self._finish_route_job(
                    db,
                    route_context,
                    "failed",
                    "",
                    error_message="关键词清洗返回空结果",
                )
                return None

            # 解析 JSON：容忍 ```json 包裹或前后杂字
            fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", output, re.DOTALL)
            candidate = fenced.group(1) if fenced else output
            if not fenced:
                brace = re.search(r"\{.*\}", candidate, re.DOTALL)
                if brace:
                    candidate = brace.group(0)
            try:
                data = json.loads(candidate)
            except (TypeError, ValueError):
                self._finish_route_job(
                    db,
                    route_context,
                    "failed",
                    output,
                    error_message="关键词清洗结果不是有效 JSON",
                )
                return None
            if not isinstance(data, dict):
                self._finish_route_job(
                    db,
                    route_context,
                    "failed",
                    output,
                    error_message="关键词清洗结果必须是 JSON 对象",
                )
                return None

            keywords = [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()]
            dropped = [str(k).strip() for k in (data.get("dropped_sample") or []) if str(k).strip()]
            if not keywords:
                self._finish_route_job(
                    db,
                    route_context,
                    "failed",
                    output,
                    error_message="关键词清洗结果未包含有效关键词",
                )
                return None
            cleaned = {"keywords": keywords[:30], "dropped_sample": dropped[:10]}
            self._finish_route_job(
                db,
                route_context,
                "succeeded",
                output,
                output_json=cleaned,
            )
            return cleaned
        except Exception as exc:
            if route_context is not None:
                self._finish_route_job(
                    db,
                    route_context,
                    "failed",
                    "".join(output_parts),
                    error_message=str(exc),
                )
            return None  # 任何异常都降级，不影响偏好分析主流程
        finally:
            db.close()
