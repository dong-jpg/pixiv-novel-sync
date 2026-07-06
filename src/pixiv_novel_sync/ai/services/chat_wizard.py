from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

from ...storage_db import Database
from ..models import AIStreamChunk
from ..prompts import (
    build_chat_messages,
)
from .core import AIServiceError


class AIChatWizardMixin:
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
            provider = self._get_provider(provider_config)
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
