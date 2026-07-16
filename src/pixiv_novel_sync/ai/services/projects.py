from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Iterator
from typing import Any

from ...storage_db import Database
from ..chunking import get_tail_context
from ..detection import detect_ai_tells
from ..models import AIAgentConfig, AIStreamChunk
from ..prompts import (
    build_chapter_summary_messages,
    build_continue_messages,
    build_foreshadow_resolve_messages,
    build_longform_detail_messages,
    build_longform_plan_messages,
    build_polish_messages,
    compose_style_control_prompt,
    safe_prompt_preview,
)
from .core import AIServiceError

# M4: 状态解析的模型输出可能受源文本提示注入影响，诱导伪造海量伏笔。
# 单次状态更新对新增伏笔数量与单条长度设硬上限，作为数据完整性兜底。
_MAX_STATE_NEW_FORESHADOWS = 200
_MAX_STATE_FORESHADOW_DESC_LEN = 2000


class AIProjectsMixin:
    # 章节 Pipeline 步骤的规范顺序与显示标签。stream_chapter_pipeline /
    # stream_chapters_pipeline 会按此顺序对用户勾选的 steps 排序并展示标签。
    PIPELINE_STEP_ORDER: tuple[str, ...] = (
        "continue",
        "polish_dialogue",
        "polish_psychology",
        "deai",
        "summary",
        "state",
        "foreshadow",
        "audit",
        "detect",
        "index",
    )
    PIPELINE_STEP_LABEL: dict[str, str] = {
        "continue": "续写",
        "polish_dialogue": "对话润色",
        "polish_psychology": "心理描写润色",
        "deai": "去AI味",
        "summary": "章节摘要",
        "state": "项目状态更新",
        "foreshadow": "伏笔回收",
        "audit": "内容审计",
        "detect": "AI痕迹检测",
        "index": "检索索引",
    }

    @staticmethod
    def _with_project_cover_url(project: dict[str, Any]) -> dict[str, Any]:
        item = dict(project)
        item["cover_url"] = (
            f"/api/dashboard/ai/projects/{int(item['id'])}/cover"
            if item.get("cover_path")
            else None
        )
        return item

    def list_writing_projects(self, status: str | None = None) -> list[dict[str, Any]]:
        db = self._db()
        try:
            return [
                self._with_project_cover_url(project)
                for project in db.list_ai_writing_projects(status=status)
            ]
        finally:
            db.close()

    def get_writing_project(self, project_id: int) -> dict[str, Any]:
        db = self._db()
        try:
            project = db.get_ai_writing_project(project_id)
            if not project:
                raise AIServiceError("写作项目不存在")
            return self._with_project_cover_url(project)
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

    def update_writing_project_cover(self, project_id: int, cover_path: str | None) -> None:
        db = self._db()
        try:
            db.update_ai_writing_project(project_id, {"cover_path": cover_path})
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
                parts.append("【超期伏笔 - 急需回收】\n" + "\n".join(lines))
            if approaching:
                non_overdue = [f for f in approaching if f not in overdue]
                if non_overdue:
                    lines = [f"- {f['description']}（目标第{f['target_resolve_chapter']}章回收）" for f in non_overdue]
                    parts.append("【即将到期伏笔】\n" + "\n".join(lines))

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
                parts.append("【前文摘要】\n" + "\n".join(summary_lines))

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
        if start < 0:
            raise AIServiceError("模型未返回有效 JSON 对象")
        # 7.7: 括号配平,找到第一个完整JSON对象的结束位置
        depth = 0
        end = -1
        for i in range(start, len(cleaned)):
            if cleaned[i] == "{":
                depth += 1
            elif cleaned[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end < 0:
            raise AIServiceError("模型未返回有效 JSON 对象(括号不配对)")
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

    def stream_longform_plan(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
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
                system_prompt=None,
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
            provider = self._get_provider(provider_config)
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
                system_prompt=None,
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
            provider = self._get_provider(provider_config)
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
        # #14 项目级风格控制（滑块+标签+自定义）：从项目 settings 渲染成指令，
        # 拼进 style_prompt，从初期即控制生成风格。即使已有风格档案也叠加。
        if project_id and project is None:
            project = db.get_ai_writing_project(project_id)
        if project:
            style_control = (project.get("settings") or {}).get("style_control")
            control_prompt = compose_style_control_prompt(style_control)
            if control_prompt:
                style_prompt = f"{style_prompt}\n\n{control_prompt}" if style_prompt else control_prompt
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
            provider = self._get_provider(provider_config)
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
            provider = self._get_provider(provider_config)
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
        added = 0
        with db.transaction():
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
                    # 解析伏笔列表；按已有 description 去重，避免同章 pipeline 重跑后伏笔重复插入
                    existing_descs = {
                        str(fs.get("description") or "").strip()
                        for fs in db.list_ai_foreshadows(project_id)
                    }
                    # M4: 章节正文可能夹带提示注入诱导模型吐出海量伏笔行，
                    # 单次解析的新增数量与单条长度设硬上限兜底。
                    for line in section.splitlines():
                        if added >= _MAX_STATE_NEW_FORESHADOWS:
                            break
                        line = line.strip().lstrip("- •")
                        if not line:
                            continue
                        parts = line.split("|")
                        description = parts[0].strip()[:_MAX_STATE_FORESHADOW_DESC_LEN]
                        importance = "normal"
                        if len(parts) > 1:
                            imp = parts[1].strip().lower()
                            if imp in ("high", "normal", "low"):
                                importance = imp
                        if description and description not in existing_descs:
                            db.create_ai_foreshadow({
                                "project_id": project_id,
                                "description": description,
                                "planted_chapter": chapter.get("chapter_number"),
                                "importance": importance,
                            })
                            existing_descs.add(description)
                            added += 1

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
            provider = self._get_provider(provider_config)
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
        # Only allow resolving foreshadows that actually belong to this project. The
        # model output is driven by untrusted chapter text, so a hallucinated/echoed
        # id from another project must not be flipped to "resolved" here.
        project_foreshadow_ids = {int(f["id"]) for f in db.list_ai_foreshadows(project_id)}
        for r in parsed.get("resolved") or []:
            try:
                fs_id = int(r.get("id"))
            except (TypeError, ValueError):
                warnings.append("模型返回了无效的伏笔 id，已跳过")
                continue
            if fs_id not in project_foreshadow_ids:
                warnings.append(f"模型返回的伏笔 id={fs_id} 不属于该项目，已跳过")
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
            provider = self._get_provider(provider_config)
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
            msg = str(exc)
            if job_created:
                db.update_ai_job(job_id, "failed", output_text="".join(output_parts), error_message=msg)
            yield AIStreamChunk(type="error", data={"message": msg})
        finally:
            db.close()

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
                    # 续写 prompt 要求模型“从锚点开始续写、不要重复已有内容”，
                    # 因此 step_output 只是新生成片段。必须拼接章节已有正文后再写回，
                    # 否则会用续写片段整段覆盖原文，造成不可逆的数据丢失（与单步
                    # stream_chapter_continue 的 existing_content + generated 行为对齐）。
                    pre_db = self._db()
                    try:
                        _pre_ch = pre_db.get_ai_chapter(chapter_id)
                        existing_content = (_pre_ch.get("content") if _pre_ch else "") or ""
                    finally:
                        pre_db.close()
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
                    generated = "".join(parts)
                    if generated:
                        step_output = f"{existing_content}{generated}"
                        latest_text = step_output
                        # 写回章节 content（已有正文 + 续写片段）
                        sub_db = self._db()
                        try:
                            sub_db.update_ai_chapter(chapter_id, {"content": step_output, "status": "draft"})
                        finally:
                            sub_db.close()
                    step_meta = {"job_id": job_id, "chars": len(generated)}

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
                    skipped_this = False
                    for chunk in self.stream_auto_resolve_foreshadows(sub_payload):
                        if chunk.type == "metadata":
                            job_id = (chunk.data or {}).get("job_id") or ""
                            if (chunk.data or {}).get("skipped"):
                                skipped_steps += 1
                                _emit_step(step, "skipped", reason=(chunk.data or {}).get("reason", ""))
                                yield AIStreamChunk(type="custom", data={"event": "step_done", "step": step, "skipped": True})
                                skipped_this = True
                                break
                        elif chunk.type == "delta":
                            yield AIStreamChunk(type="custom", data={"event": "delta", "step": step, "text": chunk.text})
                        elif chunk.type == "done":
                            resolved = (chunk.data or {}).get("resolved", []) or []
                        elif chunk.type == "error":
                            raise AIServiceError((chunk.data or {}).get("message", "伏笔回收失败"))
                    # 跳过和正常完成都必须 continue：否则会落到循环末尾的通用 done 分支，
                    # 把刚标记为 skipped 的步骤又覆盖成 done，并重复发一个 step_done 事件。
                    if skipped_this:
                        continue
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

    def stream_chapters_pipeline(self, payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        project_id = int(payload.get("project_id") or 0)
        if not project_id:
            raise AIServiceError("缺少 project_id")
        raw_ids = payload.get("chapter_ids") or []
        chapter_ids = []
        for raw in raw_ids:
            try:
                cid = int(raw)
            except (TypeError, ValueError):
                continue
            if cid > 0 and cid not in chapter_ids:
                chapter_ids.append(cid)
        if not chapter_ids:
            raise AIServiceError("请选择要生成的章节")

        db = self._db()
        try:
            chapters = db.list_ai_chapters(project_id)
        finally:
            db.close()
        allowed = {int(ch["id"]): ch for ch in chapters}
        ordered = [allowed[cid] for cid in chapter_ids if cid in allowed]
        ordered.sort(key=lambda ch: int(ch.get("chapter_number") or 0))
        if not ordered:
            raise AIServiceError("没有可生成的章节")

        succeeded = 0
        failed = 0
        yield AIStreamChunk(type="custom", data={"event": "batch_start", "total": len(ordered)})
        for index, chapter in enumerate(ordered, 1):
            chapter_id = int(chapter["id"])
            chapter_number = int(chapter.get("chapter_number") or index)
            yield AIStreamChunk(type="custom", data={
                "event": "chapter_start",
                "chapter_id": chapter_id,
                "chapter_number": chapter_number,
                "title": chapter.get("title") or f"第{chapter_number}章",
                "index": index,
                "total": len(ordered),
            })
            chapter_failed = False
            last_status = "running"
            sub_payload = {**payload, "project_id": project_id, "chapter_id": chapter_id}
            for chunk in self.stream_chapter_pipeline(sub_payload):
                if chunk.type == "custom":
                    data = dict(chunk.data or {})
                    event_name = data.get("event") or "custom"
                    data.update({"chapter_id": chapter_id, "chapter_number": chapter_number, "event": event_name})
                    if event_name == "step_failed":
                        chapter_failed = True
                    yield AIStreamChunk(type="custom", data=data)
                elif chunk.type == "metadata":
                    data = dict(chunk.data or {})
                    data.update({"chapter_id": chapter_id, "chapter_number": chapter_number})
                    yield AIStreamChunk(type="metadata", data=data)
                elif chunk.type == "done":
                    last_status = str((chunk.data or {}).get("status") or "succeeded")
                elif chunk.type == "error":
                    chapter_failed = True
                    failed += 1
                    yield AIStreamChunk(type="custom", data={
                        "event": "chapter_failed",
                        "chapter_id": chapter_id,
                        "chapter_number": chapter_number,
                        "message": (chunk.data or {}).get("message") or "章节生成失败",
                    })
                    break
                else:
                    yield chunk
            else:
                if chapter_failed or last_status == "partial":
                    failed += 1
                else:
                    succeeded += 1
                yield AIStreamChunk(type="custom", data={
                    "event": "chapter_done",
                    "chapter_id": chapter_id,
                    "chapter_number": chapter_number,
                    "status": last_status,
                    "partial": chapter_failed or last_status == "partial",
                })
        yield AIStreamChunk(type="done", data={"succeeded": succeeded, "failed": failed, "total": len(ordered)})

    def get_writing_project_reader(self, project_id: int) -> dict[str, Any]:
        db = self._db()
        try:
            project = db.get_ai_writing_project(project_id)
            if not project:
                raise AIServiceError("写作项目不存在")
            chapters = db.list_ai_chapters(project_id)
        finally:
            db.close()
        total_words = sum(int(ch.get("word_count") or len(ch.get("content") or "")) for ch in chapters)
        reader_project = self._with_project_cover_url(
            {**project, "total_words": total_words, "chapter_count": len(chapters)}
        )
        return {"project": reader_project, "chapters": chapters}

    def export_writing_project_text(self, project_id: int) -> tuple[str, str]:
        data = self.get_writing_project_reader(project_id)
        project = data["project"]
        chapters = data["chapters"]
        title = str(project.get("name") or f"写作项目 {project_id}").strip()
        safe_name = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", title).strip("._ ") or f"writing-project-{project_id}"
        parts = [title]
        description = str(project.get("description") or "").strip()
        if description:
            parts.extend(["", description])
        for chapter in chapters:
            chapter_number = int(chapter.get("chapter_number") or 0)
            chapter_title = str(chapter.get("title") or f"第{chapter_number}章").strip()
            content = str(chapter.get("content") or "").strip()
            parts.extend(["", "", f"第{chapter_number}章 {chapter_title}", "", content])
        return safe_name + ".txt", "\n".join(parts).strip() + "\n"

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
