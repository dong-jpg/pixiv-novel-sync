"""Adult fictional-character confirmation and polish orchestration."""

from __future__ import annotations

import secrets
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any, Literal

from ...storage_db import Database
from ..adult_prompt import AdultPrompt, build_adult_prompt
from ..adult_policies import (
    FACT_GUARD_POLICY,
    SAFETY_POLICY,
    verify_adult_policy_bundle,
)
from ..adult_types import (
    AdultCharacterFact,
    AdultConflictError,
    AdultInputError,
    AdultPolishRequest,
    PolicyMismatchError,
    canonical_sha256,
    parse_adult_request,
    raw_sha256,
)
from ..adult_validation import compute_provider_scope_hash
from ..model_router import CandidateSnapshot, PromptBudget, RouteRequest
from ..models import AIAgentConfig, AIStreamChunk
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
_MAX_ADULT_OUTPUT_CODEPOINTS = 36_000
_MAX_ADULT_OUTPUT_BYTES = 144_000
_ADULT_PROGRESS_FIELDS = frozenset(
    {
        "phase",
        "action",
        "stage",
        "status",
        "candidate_index",
        "provider_id",
        "provider_name",
        "provider_model_id",
        "model_key",
        "pool_id",
        "pool_name",
        "pool_position",
        "fallback_depth",
        "error_category",
        "finish_reason",
        "reason",
    }
)
ADULT_POLISH_SYSTEM_PROMPT = (
    "你负责润色服务器明确标记的单一目标片段。保持角色、剧情、事实、叙事视角和锁定词不变，"
    "遵循项目继承风格与本次强度参数，不扩写目标边界之外的内容。只输出可直接使用的替换片段正文，"
    "不得输出说明、标题、分析或代码块。"
)


@dataclass(frozen=True, slots=True)
class AdultRouteRequest:
    job_id: str
    stage: Literal["main", "validation"]
    messages: list[dict[str, str]]
    candidate_snapshot: CandidateSnapshot
    max_tokens: int
    owner_token: str
    on_delta: Callable[[str], None]
    on_progress: Callable[[dict[str, Any]], None]
    temperature: float
    top_p: float

    def to_route_request(self) -> RouteRequest:
        return RouteRequest(
            job_id=self.job_id,
            stage=self.stage,
            messages=[dict(message) for message in self.messages],
            candidate_snapshot=self.candidate_snapshot,
            max_tokens=self.max_tokens,
            owner_token=self.owner_token,
            on_delta=self.on_delta,
            on_progress=self.on_progress,
            temperature=self.temperature,
            top_p=self.top_p,
        )


@dataclass(frozen=True, slots=True)
class PreparedAdultJob:
    request: AdultPolishRequest
    job_id: str
    owner_scope: str
    owner_token: str
    reused: bool
    status: str
    agent: AIAgentConfig
    project: Mapping[str, Any]
    chapter_content: str
    target: str
    before: str
    after: str
    project_facts: Mapping[str, Any]
    project_facts_hash: str
    adult_characters_hash: str
    participant_hash: str
    characters: tuple[AdultCharacterFact, ...]
    participant_characters: tuple[AdultCharacterFact, ...]
    prompt: AdultPrompt
    main_snapshot: CandidateSnapshot
    safety_snapshot: CandidateSnapshot
    fact_guard_snapshot: CandidateSnapshot
    prompt_budget: PromptBudget
    job_input: Mapping[str, Any]


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


def _adult_owner_value(value: Any, name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AIServiceError(f"{name} 无效")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise AIServiceError(f"{name} 无效")
    return value


def _review_agent_config(
    review_kind: Literal["safety", "fact_guard"],
    binding: Mapping[str, Any],
) -> AIAgentConfig:
    if not bool(binding.get("enabled")):
        raise AIServiceError(f"成人 {review_kind} 审查绑定未启用")
    binding_type = binding.get("binding_type")
    if binding_type not in {"fixed", "pool"}:
        raise AIServiceError(f"成人 {review_kind} 审查绑定无效")
    provider_id = binding.get("provider_id")
    model_pool_id = binding.get("model_pool_id")
    if binding_type == "fixed":
        if isinstance(provider_id, bool) or not isinstance(provider_id, int):
            raise AIServiceError(f"成人 {review_kind} 审查 Provider 无效")
        model_pool_id = None
    else:
        if isinstance(model_pool_id, bool) or not isinstance(model_pool_id, int):
            raise AIServiceError(f"成人 {review_kind} 审查模型池无效")
        provider_id = None
    bundle = SAFETY_POLICY if review_kind == "safety" else FACT_GUARD_POLICY
    return AIAgentConfig(
        id=-1 if review_kind == "safety" else -2,
        name=f"adult_{review_kind}",
        task_type=f"adult_{review_kind}_review" if review_kind == "safety" else "adult_fact_guard",
        provider_id=provider_id,
        model=binding.get("model"),
        system_prompt=bundle.prompt_template,
        temperature=0.0,
        top_p=1.0,
        max_tokens=2_000,
        context_window=16_000,
        enabled=True,
        binding_type=binding_type,
        model_pool_id=model_pool_id,
        required_capabilities=("json",),
        binding_version=int(binding.get("version") or 1),
    )


def _character_fact(row: Mapping[str, Any]) -> AdultCharacterFact:
    return AdultCharacterFact(
        character_id=str(row["character_id"]),
        revision=int(row["revision"]),
        canonical_name=str(row["canonical_name"]),
        aliases=tuple(str(alias) for alias in (row.get("aliases") or [])),
        age_years=(
            int(row["age_years"])
            if row.get("age_years") is not None
            else None
        ),
        age_basis=str(row["age_basis"]),
        fictional=bool(row["fictional"]),
        active=bool(row["active"]),
    )


def _sanitize_progress(data: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in _ADULT_PROGRESS_FIELDS:
        value = data.get(key)
        if value is None or isinstance(value, (bool, int, float)):
            if key in data:
                result[key] = value
            continue
        if isinstance(value, str) and len(value) <= 200 and not any(
            unicodedata.category(char) == "Cc" for char in value
        ):
            result[key] = value
    return result


def _attempt_progress(attempt: Mapping[str, Any]) -> dict[str, Any]:
    return _sanitize_progress(
        {
            "phase": "route",
            "action": "attempt_result",
            "stage": attempt.get("stage"),
            "status": attempt.get("status"),
            "candidate_index": attempt.get("candidate_index"),
            "provider_id": attempt.get("provider_id"),
            "provider_name": attempt.get("provider_name_snapshot"),
            "provider_model_id": attempt.get("provider_model_id"),
            "model_key": attempt.get("model_key"),
            "pool_id": attempt.get("pool_id"),
            "pool_name": attempt.get("pool_name_snapshot"),
            "error_category": attempt.get("error_category"),
            "finish_reason": attempt.get("finish_reason"),
        }
    )


class AIAdultPolishMixin:
    @staticmethod
    def _adult_error(
        code: str,
        message: str,
        job_id: str | None = None,
        *,
        replayed: bool = False,
    ) -> AIStreamChunk:
        data: dict[str, Any] = {"code": code, "message": message}
        if job_id is not None:
            data["job_id"] = job_id
        if replayed:
            data["replayed"] = True
        return AIStreamChunk(type="error", data=data)

    @staticmethod
    def _adult_job_input(
        request: AdultPolishRequest,
        *,
        project_facts_hash: str,
        adult_confirmation_revision: int,
        adult_characters_hash: str,
        participant_hash: str,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "input_contract_version": 1,
            "project_id": request.project_id,
            "chapter_id": request.chapter_id,
            "agent_id": request.agent_id,
            "target_start": request.target_start,
            "target_end": request.target_end,
            "target_length": request.target_end - request.target_start,
            "chapter_revision": request.chapter_revision,
            "chapter_content_hash": request.chapter_content_hash,
            "target_text_hash": request.target_text_hash,
            "project_facts_hash": project_facts_hash,
            "adult_confirmation_revision": adult_confirmation_revision,
            "adult_characters_hash": adult_characters_hash,
            "participant_character_ids": list(request.participant_character_ids),
            "participant_hash": participant_hash,
            "provider_scope_hash": request.provider_scope_hash,
            "intensity": asdict(request.intensity),
            "locked_terms_count": len(request.locked_terms),
            "locked_terms_hash": canonical_sha256(list(request.locked_terms)),
            "instruction_length": len(request.instruction),
            "instruction_hash": raw_sha256(request.instruction),
            "idempotency_key_hash": raw_sha256(request.idempotency_key),
            "parent_job_id": request.parent_job_id,
            "preference_profile_id": request.preference_profile_id,
            "preference_injection_strength": request.preference_injection_strength,
        }
        data["request_hash"] = canonical_sha256(data)
        return data

    @staticmethod
    def _confirmed_character_facts(
        confirmation: Mapping[str, Any],
        rows: Sequence[Mapping[str, Any]],
        participant_ids: Sequence[str],
    ) -> tuple[
        tuple[AdultCharacterFact, ...],
        tuple[AdultCharacterFact, ...],
        str,
    ]:
        raw_entries = confirmation.get("adult_characters")
        if not isinstance(raw_entries, Sequence) or isinstance(
            raw_entries,
            (str, bytes),
        ):
            raise AIServiceError("成人角色确认记录无效")
        by_id = {str(row["character_id"]): row for row in rows}
        confirmed: dict[str, AdultCharacterFact] = {}
        for entry in raw_entries:
            if not isinstance(entry, Mapping):
                raise AIServiceError("成人角色确认记录无效")
            character_id = entry.get("character_id")
            revision = entry.get("character_revision")
            if (
                not isinstance(character_id, str)
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision <= 0
                or character_id in confirmed
            ):
                raise AIServiceError("成人角色确认记录无效")
            row = by_id.get(character_id)
            if row is None:
                raise AIServiceError("已确认成人角色不存在或不属于当前项目")
            fact = _character_fact(row)
            if fact.revision != revision:
                raise AIConflictError("409: 成人角色 revision 已变化")
            if not fact.active:
                raise AIServiceError("已确认成人角色已停用")
            if not fact.fictional:
                raise AIServiceError("成人润色只允许明确的虚构角色")
            if fact.age_years is None or fact.age_years < 18:
                raise AIServiceError("成人角色年龄必须明确且不小于 18 岁")
            confirmed[character_id] = fact
        if not confirmed:
            raise AIServiceError("项目尚未确认成人虚构角色")
        missing = [value for value in participant_ids if value not in confirmed]
        if missing:
            raise AIServiceError("参与者必须全部来自已确认成人虚构角色")
        characters = tuple(confirmed[key] for key in sorted(confirmed))
        participants = tuple(confirmed[key] for key in participant_ids)
        participant_hash = canonical_sha256(
            [
                {
                    "character_id": fact.character_id,
                    "revision": fact.revision,
                    "canonical_name": fact.canonical_name,
                    "aliases": list(fact.aliases),
                    "age_years": fact.age_years,
                    "fictional": fact.fictional,
                }
                for fact in participants
            ]
        )
        return characters, participants, participant_hash

    @staticmethod
    def _verify_named_participants(
        characters: Sequence[AdultCharacterFact],
        participant_ids: Sequence[str],
        context: str,
    ) -> None:
        allowed = set(participant_ids)
        for fact in characters:
            if fact.character_id in allowed:
                continue
            names = (fact.canonical_name, *fact.aliases)
            if any(name and name in context for name in names):
                raise AIServiceError(
                    "目标或上下文中的已确认角色必须列入本次参与者"
                )

    def prepare_adult_job(
        self,
        payload: Mapping[str, Any],
        owner_scope: str,
        *,
        owner_token: str | None = None,
    ) -> PreparedAdultJob:
        if getattr(self, "_closed", False):
            raise AIServiceError("AI 服务已关闭")
        request = parse_adult_request(payload)
        safe_owner_scope = _adult_owner_value(owner_scope, "owner_scope")
        execution_token = (
            _adult_owner_value(owner_token, "owner_token")
            if owner_token is not None
            else secrets.token_urlsafe(32)
        )
        if not request.adult_characters_confirmed:
            raise AIServiceError("本次请求尚未确认参与者均为成年虚构角色")

        db = self._db()
        try:
            with db.read_transaction():
                self._verify_adult_policy_state(db)
                project = db.get_ai_writing_project(request.project_id)
                if project is None:
                    raise AIServiceError("写作项目不存在")
                chapter = db.get_ai_chapter(request.chapter_id)
                if chapter is None:
                    raise AIServiceError("章节不存在")
                if int(chapter.get("project_id") or 0) != request.project_id:
                    raise AIServiceError("章节不属于当前写作项目")
                agent = self._load_agent_config(db, request.agent_id)
                if agent.task_type != "adult_polish":
                    raise AIServiceError("所选 Agent 不是成人描写润色 Agent")

                confirmation = db.get_adult_confirmation(request.project_id)
                if confirmation is None:
                    raise AIServiceError("项目成人确认不存在")
                if not bool(confirmation.get("adult_content_enabled")):
                    raise AIServiceError("项目尚未启用成人内容")
                if not bool(confirmation.get("adult_characters_confirmed")):
                    raise AIServiceError("项目尚未确认成年角色")
                if not bool(confirmation.get("fictional_characters_confirmed")):
                    raise AIServiceError("项目尚未确认角色均为虚构人物")

                chapter_content = chapter.get("content")
                if not isinstance(chapter_content, str):
                    raise AIServiceError("章节正文无效")
                if int(chapter.get("chapter_revision") or 0) != request.chapter_revision:
                    raise AIConflictError("409: 章节 revision 已变化")
                if raw_sha256(chapter_content) != request.chapter_content_hash:
                    raise AIConflictError("409: 章节正文已变化")
                if request.target_end > len(chapter_content):
                    raise AIServiceError("目标片段超出章节正文范围")
                target = chapter_content[request.target_start : request.target_end]
                if raw_sha256(target) != request.target_text_hash:
                    raise AIConflictError("409: 目标片段已变化")
                before = chapter_content[
                    max(0, request.target_start - 4_000) : request.target_start
                ]
                after = chapter_content[
                    request.target_end : request.target_end + 4_000
                ]

                rows = db.list_adult_characters(
                    request.project_id,
                    include_inactive=True,
                )
                characters, participants, participant_hash = (
                    self._confirmed_character_facts(
                        confirmation,
                        rows,
                        request.participant_character_ids,
                    )
                )
                self._verify_named_participants(
                    characters,
                    request.participant_character_ids,
                    before + target + after,
                )
                project_facts, project_facts_hash = build_project_facts_snapshot(
                    db,
                    request.project_id,
                )
                settings = project.get("settings")
                style_control = (
                    settings.get("style_control")
                    if isinstance(settings, Mapping)
                    and isinstance(settings.get("style_control"), Mapping)
                    else None
                )
                prompt = build_adult_prompt(
                    agent_prompt=agent.system_prompt,
                    project_facts=project_facts,
                    before=before,
                    target=target,
                    after=after,
                    style_control=style_control,
                    intensity=request.intensity,
                    instruction=request.instruction,
                    protected_terms=request.locked_terms,
                    characters=characters,
                )

                review_bindings = {
                    kind: db.get_adult_review_binding(kind)
                    for kind in _ADULT_REVIEW_KINDS
                }
                if any(binding is None for binding in review_bindings.values()):
                    raise AIServiceError("成人安全审查绑定缺失")
                safety_agent = _review_agent_config(
                    "safety",
                    review_bindings["safety"] or {},
                )
                fact_guard_agent = _review_agent_config(
                    "fact_guard",
                    review_bindings["fact_guard"] or {},
                )
                if request.parent_job_id is not None and db.get_adult_job(
                    request.parent_job_id,
                    safe_owner_scope,
                ) is None:
                    raise AIServiceError("父成人润色任务不存在")

            main_snapshot = self.model_router.resolve_candidates(
                agent,
                stage="main",
            )
            safety_snapshot = self.model_router.resolve_candidates(
                safety_agent,
                stage="validation",
            )
            fact_guard_snapshot = self.model_router.resolve_candidates(
                fact_guard_agent,
                stage="validation",
            )
            actual_scope_hash = compute_provider_scope_hash(
                {
                    "main": main_snapshot,
                    "safety": safety_snapshot,
                    "fact_guard": fact_guard_snapshot,
                }
            )
            if actual_scope_hash != request.provider_scope_hash:
                raise AIConflictError("409: Provider 范围已变化，请重新确认")
            prompt_budget = self.model_router.build_prompt_budget(
                agent,
                main_snapshot,
                prompt.user_messages,
                agent.max_tokens,
            )
            job_input = self._adult_job_input(
                request,
                project_facts_hash=project_facts_hash,
                adult_confirmation_revision=int(
                    confirmation.get("adult_confirmation_revision") or 0
                ),
                adult_characters_hash=str(
                    confirmation.get("adult_characters_hash") or ""
                ),
                participant_hash=participant_hash,
            )
            idempotency_key_hash = str(job_input["idempotency_key_hash"])
            existing = db.find_job_by_idempotency(
                safe_owner_scope,
                idempotency_key_hash,
            )
            if existing is not None:
                existing_input = existing.get("input")
                if not isinstance(existing_input, Mapping) or (
                    existing_input.get("request_hash") != job_input["request_hash"]
                ):
                    raise AIConflictError("409: 幂等键已用于不同的成人润色请求")
                execution = db.get_adult_job_execution(
                    str(existing["job_id"]),
                    safe_owner_scope,
                )
                if execution is None:
                    raise AIConflictError("409: 成人润色任务 owner 已变化")
                return PreparedAdultJob(
                    request=request,
                    job_id=str(existing["job_id"]),
                    owner_scope=safe_owner_scope,
                    owner_token=str(execution["owner_token"]),
                    reused=True,
                    status=str(existing.get("status") or ""),
                    agent=agent,
                    project=MappingProxyType(dict(project)),
                    chapter_content=chapter_content,
                    target=target,
                    before=before,
                    after=after,
                    project_facts=MappingProxyType(project_facts),
                    project_facts_hash=project_facts_hash,
                    adult_characters_hash=str(
                        confirmation.get("adult_characters_hash") or ""
                    ),
                    participant_hash=participant_hash,
                    characters=characters,
                    participant_characters=participants,
                    prompt=prompt,
                    main_snapshot=main_snapshot,
                    safety_snapshot=safety_snapshot,
                    fact_guard_snapshot=fact_guard_snapshot,
                    prompt_budget=prompt_budget,
                    job_input=MappingProxyType(job_input),
                )

            requested_job_id = uuid.uuid4().hex
            created = db.create_adult_job(
                job_id=requested_job_id,
                agent_id=agent.id,
                input_data=job_input,
                owner_scope=safe_owner_scope,
                owner_token=execution_token,
                idempotency_key_hash=idempotency_key_hash,
                parent_job_id=request.parent_job_id,
            )
            reused = str(created["job_id"]) != requested_job_id
            if reused:
                created_input = created.get("input")
                if not isinstance(created_input, Mapping) or (
                    created_input.get("request_hash") != job_input["request_hash"]
                ):
                    raise AIConflictError("409: 幂等键已用于不同的成人润色请求")
                execution = db.get_adult_job_execution(
                    str(created["job_id"]),
                    safe_owner_scope,
                )
                if execution is None:
                    raise AIConflictError("409: 成人润色任务 owner 已变化")
                execution_token = str(execution["owner_token"])
            else:
                try:
                    if not db.set_ai_job_candidate_snapshot(
                        requested_job_id,
                        execution_token,
                        self._snapshot_payload(main_snapshot),
                        main_snapshot.snapshot_hash,
                    ):
                        raise AIConflictError("AI job 候选快照保存冲突")
                    if not self._persist_prompt_budget(
                        db,
                        requested_job_id,
                        execution_token,
                        prompt_budget,
                    ):
                        raise AIConflictError("AI job PromptBudget 保存冲突")
                except Exception:
                    db.cas_finish_adult_job(
                        requested_job_id,
                        safe_owner_scope,
                        execution_token,
                        "failed",
                        error_code="route_setup_failed",
                        error_message="成人润色路由初始化失败",
                    )
                    raise

            return PreparedAdultJob(
                request=request,
                job_id=str(created["job_id"]),
                owner_scope=safe_owner_scope,
                owner_token=execution_token,
                reused=reused,
                status=str(created.get("status") or "running"),
                agent=agent,
                project=MappingProxyType(dict(project)),
                chapter_content=chapter_content,
                target=target,
                before=before,
                after=after,
                project_facts=MappingProxyType(project_facts),
                project_facts_hash=project_facts_hash,
                adult_characters_hash=str(
                    confirmation.get("adult_characters_hash") or ""
                ),
                participant_hash=participant_hash,
                characters=characters,
                participant_characters=participants,
                prompt=prompt,
                main_snapshot=main_snapshot,
                safety_snapshot=safety_snapshot,
                fact_guard_snapshot=fact_guard_snapshot,
                prompt_budget=prompt_budget,
                job_input=MappingProxyType(job_input),
            )
        except AdultInputError as exc:
            raise AIServiceError(str(exc)) from exc
        finally:
            db.close()

    def _replay_adult_job(self, prepared: PreparedAdultJob) -> Iterator[AIStreamChunk]:
        status = prepared.status
        if status == "succeeded":
            yield AIStreamChunk(
                type="done",
                data={"job_id": prepared.job_id, "replayed": True},
            )
            return
        code = {
            "running": "idempotent_in_progress",
            "partial": "partial",
            "cancelled": "cancelled",
        }.get(status, "generation_failed")
        message = {
            "running": "相同的成人润色请求正在执行",
            "partial": "生成结果不完整，候选已丢弃",
            "cancelled": "成人润色任务已取消",
        }.get(status, "成人润色任务未成功完成")
        yield self._adult_error(
            code,
            message,
            prepared.job_id,
            replayed=True,
        )

    def stream_adult_polish(
        self,
        payload: Mapping[str, Any],
        owner_scope: str,
        owner_token: str,
    ) -> Iterator[AIStreamChunk]:
        prepared: PreparedAdultJob | None = None
        try:
            prepared = self.prepare_adult_job(
                payload,
                owner_scope,
                owner_token=owner_token,
            )
        except (AIServiceError, AIConflictError) as exc:
            yield self._adult_error("preflight_failed", str(exc))
            return
        except Exception:
            yield self._adult_error("preflight_failed", "成人润色前置校验失败")
            return

        yield AIStreamChunk(
            type="metadata",
            data={
                "job_id": prepared.job_id,
                "parent_job_id": prepared.request.parent_job_id,
                "replayed": prepared.reused,
            },
        )
        if prepared.reused:
            yield from self._replay_adult_job(prepared)
            return

        output_parts: list[str] = []
        output_bytes = 0
        output_codepoints = 0
        output_too_large = False
        progress_events: list[dict[str, Any]] = []

        def on_delta(text: str) -> None:
            nonlocal output_bytes, output_codepoints, output_too_large
            if not text or output_too_large:
                return
            next_bytes = output_bytes + len(text.encode("utf-8"))
            next_codepoints = output_codepoints + len(text)
            if (
                next_codepoints > _MAX_ADULT_OUTPUT_CODEPOINTS
                or next_bytes > _MAX_ADULT_OUTPUT_BYTES
            ):
                output_parts.clear()
                output_bytes = 0
                output_codepoints = 0
                output_too_large = True
                return
            output_parts.append(text)
            output_bytes = next_bytes
            output_codepoints = next_codepoints

        def on_progress(data: dict[str, Any]) -> None:
            sanitized = _sanitize_progress(data)
            if sanitized:
                progress_events.append(sanitized)

        route_request = AdultRouteRequest(
            job_id=prepared.job_id,
            stage="main",
            messages=prepared.prompt.user_messages,
            candidate_snapshot=prepared.main_snapshot,
            max_tokens=prepared.agent.max_tokens,
            owner_token=prepared.owner_token,
            on_delta=on_delta,
            on_progress=on_progress,
            temperature=prepared.agent.temperature,
            top_p=prepared.agent.top_p,
        )
        try:
            result = self.model_router.execute(route_request.to_route_request())
        except Exception:
            output_parts.clear()
            db = self._db()
            try:
                db.cas_finish_adult_job(
                    prepared.job_id,
                    prepared.owner_scope,
                    prepared.owner_token,
                    "failed",
                    error_code="route_unavailable",
                    error_message="成人润色模型路由失败",
                )
            finally:
                db.close()
            yield self._adult_error(
                "route_unavailable",
                "成人润色模型暂不可用",
                prepared.job_id,
            )
            return

        for progress in progress_events:
            yield AIStreamChunk(type="progress", data=progress)
        for attempt in result.attempts:
            summary = _attempt_progress(attempt)
            if summary:
                yield AIStreamChunk(type="progress", data=summary)

        contract_valid = (
            result.job_id == prepared.job_id
            and result.candidate_snapshot_hash
            == prepared.main_snapshot.snapshot_hash
            and result.output_text == "".join(output_parts)
        )
        if not contract_valid or output_too_large:
            output_parts.clear()
            code = "output_too_large" if output_too_large else "route_contract_error"
            db = self._db()
            try:
                db.cas_finish_adult_job(
                    prepared.job_id,
                    prepared.owner_scope,
                    prepared.owner_token,
                    "failed",
                    error_code=code,
                    error_message="成人润色候选无效",
                    summary={"attempt_count": len(result.attempts)},
                )
            finally:
                db.close()
            yield self._adult_error(code, "成人润色候选无效", prepared.job_id)
            return

        if result.finish_state != "succeeded":
            output_parts.clear()
            if result.finish_state == "partial":
                status = "partial"
                code = "partial"
                message = "生成结果不完整，候选已丢弃"
            elif result.finish_state == "cancelled":
                status = "cancelled"
                code = "cancelled"
                message = "成人润色任务已取消"
            else:
                status = "failed"
                code = "route_unavailable"
                message = "所有成人润色候选模型均不可用"
            db = self._db()
            try:
                db.cas_finish_adult_job(
                    prepared.job_id,
                    prepared.owner_scope,
                    prepared.owner_token,
                    status,
                    error_code=code,
                    error_message=message,
                    summary={"attempt_count": len(result.attempts)},
                )
            finally:
                db.close()
            yield self._adult_error(code, message, prepared.job_id)
            return

        # Task 7 consumes this server-only buffer and runs both fixed reviews.
        yield AIStreamChunk(
            type="progress",
            data={
                "phase": "validation",
                "action": "pending",
                "job_id": prepared.job_id,
            },
        )

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
    "AdultRouteRequest",
    "AIAdultPolishMixin",
    "PreparedAdultJob",
    "build_project_facts_snapshot",
]
