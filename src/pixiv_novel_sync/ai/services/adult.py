"""Adult fictional-character confirmation and polish orchestration."""

from __future__ import annotations

import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from ...storage_db import Database
from ..adult_types import AdultConflictError, canonical_sha256
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


__all__ = ["AIAdultPolishMixin", "build_project_facts_snapshot"]
