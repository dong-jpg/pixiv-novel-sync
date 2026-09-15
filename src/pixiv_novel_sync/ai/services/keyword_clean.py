from __future__ import annotations

from collections.abc import Generator
from typing import Any

from ..model_router import RouteResult
from ..models import AIStreamChunk
from ..prompts import build_keyword_clean_messages
from .core import RouteJobContext


class AIKeywordCleanMixin:
    """关键词清洗：偏好分析用 AI 把机械分词噪声词提炼成可搜索关键词。

    main 分支上这是 AIWritingService 唯一保留的生成能力；
    完整写作模块在 ai-writing 分支。
    """

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
