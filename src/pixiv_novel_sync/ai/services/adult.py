"""Adult fictional-character confirmation and polish orchestration."""

from __future__ import annotations

import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from ...storage_db import Database
from ..adult_policies import (
    FACT_GUARD_POLICY,
    SAFETY_POLICY,
    verify_adult_policy_bundle,
)
from ..adult_types import AdultConflictError, canonical_sha256
from ..adult_types import PolicyMismatchError, raw_sha256
from .core import AIConflictError, AIServiceError


_CHARACTER_FIELDS = frozenset(
    {"canonical_name", "aliases", "age_years", "age_basis", "fictional"}
)
_CONFIRMATION_FIELDS = frozenset(
    {
        "adult_content_enabled",
        "adult_characters_confirmed",
        "fictional_characters_confirmed",
        "character_ids",
    }
)
_ADULT_REVIEW_KINDS = ("safety", "fact_guard")
_ADULT_REVIEW_FIELDS = frozenset(
    {"binding_type", "provider_id", "model", "model_pool_id", "enabled"}
)
_ADULT_AGENT_FIELDS = frozenset(
    {
        "name",
        "binding_type",
        "provider_id",
        "model",
        "model_pool_id",
        "required_capabilities",
        "temperature",
        "top_p",
        "max_tokens",
        "context_window",
        "enabled",
    }
)
ADULT_POLISH_SYSTEM_PROMPT = (
    "你负责润色服务器明确标记的单一目标片段。保持角色、剧情、事实、叙事视角和锁定词不变，"
    "遵循项目继承风格与本次强度参数，不扩写目标边界之外的内容。只输出可直接使用的替换片段正文，"
    "不得输出说明、标题、分析或代码块。"
)


def _bounded_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise AIServiceError(f"{name}必须是字符串")
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized:
        raise AIServiceError(f"{name}不能为空")
    if len(normalized) > maximum:
        raise AIServiceError(f"{name}最多 {maximum} 个码点")
    if any(unicodedata.category(char) == "Cc" for char in normalized):
        raise AIServiceError(f"{name}不得包含控制字符")
    return normalized


def _normalize_character(data: Mapping[str, Any]) -> dict[str, Any]:
    canonical_name = _bounded_text(data.get("canonical_name"), "角色名称", 200)

    raw_aliases = data.get("aliases", [])
    if not isinstance(raw_aliases, Sequence) or isinstance(raw_aliases, (str, bytes)):
        raise AIServiceError("aliases 必须是数组")
    if len(raw_aliases) > 32:
        raise AIServiceError("aliases 最多 32 项")
    aliases: list[str] = []
    for value in raw_aliases:
        alias = _bounded_text(value, "角色别名", 100)
        if alias not in aliases:
            aliases.append(alias)

    age_years = data.get("age_years")
    if age_years is not None and (
        isinstance(age_years, bool)
        or not isinstance(age_years, int)
        or age_years < 0
    ):
        raise AIServiceError("角色年龄必须是非负整数或 null")
    age_basis = _bounded_text(data.get("age_basis"), "年龄依据", 2_000)
    fictional = data.get("fictional")
    if not isinstance(fictional, bool):
        raise AIServiceError("fictional 必须是布尔值")
    return {
        "canonical_name": canonical_name,
        "aliases": aliases,
        "age_years": age_years,
        "age_basis": age_basis,
        "fictional": fictional,
    }


def _service_conflict(exc: AdultConflictError) -> AIConflictError:
    return AIConflictError(str(exc))


def build_project_facts_snapshot(
    db: Database,
    project_id: int,
) -> tuple[dict[str, Any], str]:
    project = db.get_ai_writing_project(int(project_id))
    if project is None:
        raise AIServiceError("写作项目不存在")
    confirmation = db.get_adult_confirmation(int(project_id)) or {}
    characters = [
        {
            "character_id": row["character_id"],
            "revision": int(row["revision"]),
            "canonical_name": row["canonical_name"],
            "aliases": list(row.get("aliases") or []),
            "age_years": row.get("age_years"),
            "age_basis": row["age_basis"],
            "fictional": bool(row["fictional"]),
            "active": bool(row["active"]),
        }
        for row in db.list_adult_characters(int(project_id))
    ]
    characters.sort(key=lambda row: (row["canonical_name"], row["character_id"]))

    states = [
        {"state_type": key, "content": value}
        for key, value in sorted(db.get_all_project_states(int(project_id)).items())
    ]
    foreshadow_keys = (
        "id",
        "description",
        "planted_chapter",
        "target_resolve_chapter",
        "resolved_chapter",
        "status",
        "importance",
        "notes",
    )
    foreshadows = [
        {key: row.get(key) for key in foreshadow_keys}
        for row in db.list_ai_foreshadows(int(project_id))
    ]
    foreshadows.sort(key=lambda row: int(row.get("id") or 0))

    snapshot = {
        "project": {
            "id": int(project["id"]),
            "name": project.get("name"),
            "description": project.get("description"),
            "outline": project.get("outline"),
            "settings": project.get("settings") or {},
        },
        "adult_confirmation": {
            "adult_content_enabled": bool(
                confirmation.get("adult_content_enabled")
            ),
            "adult_characters_confirmed": bool(
                confirmation.get("adult_characters_confirmed")
            ),
            "fictional_characters_confirmed": bool(
                confirmation.get("fictional_characters_confirmed")
            ),
            "adult_confirmation_revision": int(
                confirmation.get("adult_confirmation_revision") or 0
            ),
            "adult_characters": list(
                confirmation.get("adult_characters") or []
            ),
        },
        "characters": characters,
        "states": states,
        "foreshadows": foreshadows,
    }
    return snapshot, canonical_sha256(snapshot)


class AIAdultPolishMixin:
    @staticmethod
    def _adult_policy_metadata(
        rows: Mapping[str, Mapping[str, Any]],
        kind: str,
    ) -> dict[str, Any]:
        bundle = SAFETY_POLICY if kind == "safety" else FACT_GUARD_POLICY
        expected = {
            "policy_id": bundle.policy_id,
            "policy_version": bundle.version,
            "policy_hash": bundle.expected_hash,
            "prompt_hash": raw_sha256(bundle.prompt_template),
            "schema_hash": canonical_sha256(bundle.output_schema),
        }
        stored = rows.get(kind)
        matches = stored is not None and all(
            stored.get(key) == value for key, value in expected.items()
        )
        return {**expected, "stored_matches": matches}

    @classmethod
    def _verify_adult_policy_state(cls, db: Database) -> None:
        try:
            verify_adult_policy_bundle()
        except PolicyMismatchError as exc:
            raise AIServiceError("固定成人审查策略代码校验失败") from exc
        rows = {
            str(row["policy_kind"]): row
            for row in db.list_adult_policy_state()
        }
        for kind in _ADULT_REVIEW_KINDS:
            if not cls._adult_policy_metadata(rows, kind)["stored_matches"]:
                raise AIServiceError("固定成人审查策略存储状态不匹配")

    @staticmethod
    def _normalize_adult_review_route(
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        unknown = sorted(set(payload) - _ADULT_REVIEW_FIELDS)
        if unknown:
            raise AIServiceError(
                f"成人审查绑定包含未知字段: {', '.join(unknown)}"
            )
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise AIServiceError("enabled 必须是布尔值")
        if not enabled:
            return {
                "binding_type": None,
                "provider_id": None,
                "model": None,
                "model_pool_id": None,
                "enabled": False,
            }
        binding_type = payload.get("binding_type")
        if binding_type not in {"fixed", "pool"}:
            raise AIServiceError("成人审查绑定类型必须是 fixed 或 pool")
        if binding_type == "fixed":
            provider_id = payload.get("provider_id")
            if (
                isinstance(provider_id, bool)
                or not isinstance(provider_id, int)
                or provider_id <= 0
            ):
                raise AIServiceError("固定成人审查绑定缺少 provider_id")
            if payload.get("model_pool_id") is not None:
                raise AIServiceError("固定模型和模型池不能同时提交")
            model = payload.get("model")
            if model is not None and not isinstance(model, str):
                raise AIServiceError("model 必须是字符串或 null")
            return {
                "binding_type": "fixed",
                "provider_id": provider_id,
                "model": model.strip() or None if isinstance(model, str) else None,
                "model_pool_id": None,
                "enabled": True,
            }
        model_pool_id = payload.get("model_pool_id")
        if (
            isinstance(model_pool_id, bool)
            or not isinstance(model_pool_id, int)
            or model_pool_id <= 0
        ):
            raise AIServiceError("模型池成人审查绑定缺少 model_pool_id")
        if payload.get("provider_id") is not None or payload.get("model") is not None:
            raise AIServiceError("固定模型和模型池不能同时提交")
        return {
            "binding_type": "pool",
            "provider_id": None,
            "model": None,
            "model_pool_id": model_pool_id,
            "enabled": True,
        }

    def list_adult_review_bindings(self) -> dict[str, dict[str, Any]]:
        db = self._db()
        try:
            policy_rows = {
                str(row["policy_kind"]): row
                for row in db.list_adult_policy_state()
            }
            result: dict[str, dict[str, Any]] = {}
            for kind in _ADULT_REVIEW_KINDS:
                binding = db.get_adult_review_binding(kind)
                if binding is None:
                    raise AIServiceError("成人审查绑定缺失")
                result[kind] = {
                    **binding,
                    "required_capabilities": ["json"],
                    **self._adult_policy_metadata(policy_rows, kind),
                }
            return result
        finally:
            db.close()

    def update_adult_review_binding(
        self,
        review_kind: str,
        payload: Mapping[str, Any],
        expected_version: int,
    ) -> dict[str, Any]:
        if review_kind not in _ADULT_REVIEW_KINDS:
            raise AIServiceError("成人审查绑定类型无效")
        if not isinstance(payload, Mapping):
            raise AIServiceError("成人审查绑定请求必须是对象")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version <= 0
        ):
            raise AIServiceError("expected_version 无效")

        db = self._db()
        try:
            current = db.get_adult_review_binding(review_kind)
            if current is None:
                raise AIServiceError("成人审查绑定缺失")
            if int(current["version"]) != expected_version:
                raise AIConflictError("409: 成人审查绑定 revision 已变化")

            try:
                route = self._normalize_adult_review_route(payload)
                if route["enabled"]:
                    self._verify_adult_policy_state(db)
                    self._validate_agent_binding(
                        db,
                        {
                            **route,
                            "required_capabilities": ["json"],
                        },
                    )
            except Exception as exc:
                try:
                    db.cas_update_review_binding(
                        review_kind,
                        expected_version=expected_version,
                        route={"enabled": False},
                    )
                except AdultConflictError as conflict:
                    raise _service_conflict(conflict) from conflict
                if isinstance(exc, (AIServiceError, ValueError)):
                    raise AIServiceError(
                        f"成人审查绑定配置无效: {exc}"
                    ) from exc
                raise AIServiceError("成人审查绑定配置无效") from exc

            try:
                saved = db.cas_update_review_binding(
                    review_kind,
                    expected_version=expected_version,
                    route=route,
                )
            except AdultConflictError as exc:
                raise _service_conflict(exc) from exc
            policy_rows = {
                str(row["policy_kind"]): row
                for row in db.list_adult_policy_state()
            }
            return {
                **saved,
                "required_capabilities": ["json"],
                **self._adult_policy_metadata(policy_rows, review_kind),
            }
        finally:
            db.close()

    def ensure_adult_polish_agent(
        self,
        binding: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(binding, Mapping):
            raise AIServiceError("成人润色 Agent 绑定必须是对象")
        unknown = sorted(set(binding) - _ADULT_AGENT_FIELDS)
        if unknown:
            raise AIServiceError(
                f"成人润色 Agent 包含未知字段: {', '.join(unknown)}"
            )
        if binding.get("binding_type") not in {"fixed", "pool"}:
            raise AIServiceError("成人润色 Agent 必须显式配置绑定类型")
        name = _bounded_text(
            binding.get("name", "成人描写润色"),
            "Agent 名称",
            200,
        )
        data = self._normalize_agent_payload(
            {
                **dict(binding),
                "name": name,
                "task_type": "adult_polish",
                "system_prompt": ADULT_POLISH_SYSTEM_PROMPT,
                "required_capabilities": list(
                    binding.get("required_capabilities") or []
                ),
                "temperature": binding.get("temperature", 0.7),
                "top_p": binding.get("top_p", 0.9),
                "max_tokens": binding.get("max_tokens", 12_000),
                "context_window": binding.get("context_window", 16_000),
                "enabled": binding.get("enabled", True),
            }
        )
        db = self._db()
        try:
            with db.transaction():
                self._validate_agent_binding(db, data)
                existing = next(
                    (
                        row
                        for row in db.list_ai_agents()
                        if row.get("task_type") == "adult_polish"
                        and row.get("name") == name
                    ),
                    None,
                )
                if existing is None:
                    agent_id = db.create_ai_agent(data)
                else:
                    agent_id = int(existing["id"])
                    db.update_ai_agent(agent_id, data)
                result = db.get_ai_agent(agent_id)
                if result is None:
                    raise RuntimeError("成人润色 Agent 初始化失败")
                return result
        finally:
            db.close()

    def list_adult_characters(self, project_id: int) -> list[dict[str, Any]]:
        db = self._db()
        try:
            if db.get_ai_writing_project(int(project_id)) is None:
                raise AIServiceError("写作项目不存在")
            return db.list_adult_characters(int(project_id))
        finally:
            db.close()

    def create_adult_character(
        self,
        project_id: int,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise AIServiceError("角色请求必须是对象")
        unknown = sorted(set(payload) - _CHARACTER_FIELDS)
        if unknown:
            raise AIServiceError(f"角色请求包含未知字段: {', '.join(unknown)}")
        normalized = _normalize_character(payload)
        db = self._db()
        try:
            return db.create_adult_character(
                {
                    **normalized,
                    "project_id": int(project_id),
                    "character_id": str(uuid.uuid4()),
                }
            )
        except ValueError as exc:
            raise AIServiceError(str(exc)) from exc
        finally:
            db.close()

    def update_adult_character(
        self,
        character_id: str,
        payload: Mapping[str, Any],
        expected_revision: int,
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise AIServiceError("角色请求必须是对象")
        unknown = sorted(set(payload) - _CHARACTER_FIELDS)
        if unknown:
            raise AIServiceError(f"角色请求包含未知字段: {', '.join(unknown)}")
        if not payload:
            raise AIServiceError("角色请求没有可更新字段")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision <= 0
        ):
            raise AIServiceError("expected revision 无效")

        db = self._db()
        try:
            current = db.get_adult_character(character_id)
            if current is None:
                raise AIServiceError("成人角色不存在")
            if int(current["revision"]) != expected_revision:
                raise AIConflictError("409: 角色 revision 已变化")
            merged = {
                "canonical_name": current["canonical_name"],
                "aliases": current.get("aliases") or [],
                "age_years": current.get("age_years"),
                "age_basis": current["age_basis"],
                "fictional": bool(current["fictional"]),
                **dict(payload),
            }
            normalized = _normalize_character(merged)
            changes = {
                key: normalized[key]
                for key in payload
                if normalized[key] != current.get(key)
            }
            if not changes:
                return current
            try:
                return db.cas_update_adult_character(
                    character_id,
                    expected_revision,
                    changes,
                )
            except AdultConflictError as exc:
                raise _service_conflict(exc) from exc
        finally:
            db.close()

    def deactivate_adult_character(
        self,
        character_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision <= 0
        ):
            raise AIServiceError("expected revision 无效")
        db = self._db()
        try:
            current = db.get_adult_character(character_id)
            if current is None:
                raise AIServiceError("成人角色不存在")
            if int(current["revision"]) != expected_revision:
                raise AIConflictError("409: 角色 revision 已变化")
            if not current["active"]:
                return current
            try:
                return db.cas_update_adult_character(
                    character_id,
                    expected_revision,
                    {"active": False},
                )
            except AdultConflictError as exc:
                raise _service_conflict(exc) from exc
        finally:
            db.close()

    def get_adult_confirmation(self, project_id: int) -> dict[str, Any]:
        db = self._db()
        try:
            confirmation = db.get_adult_confirmation(int(project_id))
            if confirmation is None:
                raise AIServiceError("写作项目不存在")
            return confirmation
        finally:
            db.close()

    def update_adult_confirmation(
        self,
        project_id: int,
        payload: Mapping[str, Any],
        expected_revision: int,
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise AIServiceError("成人确认请求必须是对象")
        unknown = sorted(set(payload) - _CONFIRMATION_FIELDS)
        if unknown:
            raise AIServiceError(f"成人确认包含未知字段: {', '.join(unknown)}")
        missing = sorted(_CONFIRMATION_FIELDS - set(payload))
        if missing:
            raise AIServiceError(f"成人确认缺少字段: {', '.join(missing)}")
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise AIServiceError("expected revision 无效")
        for key in _CONFIRMATION_FIELDS - {"character_ids"}:
            if not isinstance(payload.get(key), bool):
                raise AIServiceError(f"{key} 必须是布尔值")
        character_ids = payload.get("character_ids")
        if not isinstance(character_ids, Sequence) or isinstance(
            character_ids,
            (str, bytes),
        ):
            raise AIServiceError("character_ids 必须是数组")
        if len(character_ids) > 100:
            raise AIServiceError("成人角色确认最多 100 个角色")
        normalized_ids: list[str] = []
        for value in character_ids:
            if not isinstance(value, str):
                raise AIServiceError("成人角色 ID 无效")
            try:
                normalized = str(uuid.UUID(value))
            except (ValueError, AttributeError) as exc:
                raise AIServiceError("成人角色 ID 无效") from exc
            if normalized != value:
                raise AIServiceError("成人角色 ID 必须是规范小写 UUID")
            normalized_ids.append(value)
        if len(set(normalized_ids)) != len(normalized_ids):
            raise AIServiceError("成人角色 ID 不得重复")

        db = self._db()
        try:
            try:
                return db.set_adult_confirmation(
                    int(project_id),
                    expected_revision,
                    {
                        key: bool(payload[key])
                        for key in _CONFIRMATION_FIELDS - {"character_ids"}
                    },
                    normalized_ids,
                )
            except AdultConflictError as exc:
                raise _service_conflict(exc) from exc
            except ValueError as exc:
                raise AIServiceError(str(exc)) from exc
        finally:
            db.close()


__all__ = [
    "ADULT_POLISH_SYSTEM_PROMPT",
    "AIAdultPolishMixin",
    "build_project_facts_snapshot",
]
