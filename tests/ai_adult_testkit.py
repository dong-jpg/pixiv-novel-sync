from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pixiv_novel_sync.ai.adult_types import (
    AdultCharacterFact,
    AdultValidationResult,
    canonical_sha256,
    raw_sha256,
)
from pixiv_novel_sync.ai.model_router import (
    CandidateSnapshot,
    ModelCandidate,
    RouteResult,
)
from pixiv_novel_sync.ai.models import AIStreamChunk


CHARACTER_A_ID = "11111111-1111-4111-8111-111111111111"
CHARACTER_B_ID = "22222222-2222-4222-8222-222222222222"


def valid_adult_payload(_seed: Any = None, **overrides: Any) -> dict[str, Any]:
    target = "安娜握住他的手，停顿片刻后仍保持原来的称呼和视角。"
    chapter = f"前文。{target}后文。"
    start = len("前文。")
    payload: dict[str, Any] = {
        "project_id": 1,
        "chapter_id": 9,
        "agent_id": 7,
        "target_start": start,
        "target_end": start + len(target),
        "chapter_content_hash": raw_sha256(chapter),
        "target_text_hash": raw_sha256(target),
        "chapter_revision": 0,
        "participant_character_ids": [CHARACTER_A_ID, CHARACTER_B_ID],
        "adult_characters_confirmed": True,
        "intensity": {"explicitness": 50, "lyricism": 50, "vulgarity": 20},
        "locked_terms": ["安娜"],
        "instruction": "只调整措辞",
        "idempotency_key": "adult-request-key-0001",
        "provider_scope_hash": "a" * 64,
    }
    payload.update(overrides)
    return payload


def character_fact(
    name: str = "安娜",
    age_years: int | None = 25,
    fictional: bool = True,
    *,
    character_id: str = CHARACTER_A_ID,
    revision: int = 1,
) -> AdultCharacterFact:
    return AdultCharacterFact(
        character_id=character_id,
        revision=revision,
        canonical_name=name,
        aliases=(name[:1],),
        age_years=age_years,
        age_basis="项目设定",
        fictional=fictional,
        active=True,
    )


def safe_validation() -> AdultValidationResult:
    payload = {
        "applicable": True,
        "warnings": [],
        "blocking_issues": [],
        "protected_terms_missing": [],
        "paragraph_delta": 0,
        "length_ratio": 1.0,
        "perspective_warning": False,
        "new_number_tokens": [],
        "diff_summary": {"inserted": 0, "deleted": 0, "replaced": 0},
    }
    return AdultValidationResult(
        applicable=True,
        warnings=(),
        blocking_issues=(),
        protected_terms_missing=(),
        paragraph_delta=0,
        length_ratio=1.0,
        perspective_warning=False,
        new_number_tokens=(),
        diff_summary=payload["diff_summary"],
        validation_hash=canonical_sha256(payload),
    )


def structural_validation(code: str = "length_ratio") -> AdultValidationResult:
    base = safe_validation()
    payload = {
        "applicable": False,
        "warnings": [],
        "blocking_issues": [code],
        "protected_terms_missing": [],
        "paragraph_delta": base.paragraph_delta,
        "length_ratio": base.length_ratio,
        "perspective_warning": base.perspective_warning,
        "new_number_tokens": [],
        "diff_summary": dict(base.diff_summary),
    }
    return AdultValidationResult(
        applicable=False,
        warnings=(),
        blocking_issues=(code,),
        protected_terms_missing=(),
        paragraph_delta=base.paragraph_delta,
        length_ratio=base.length_ratio,
        perspective_warning=base.perspective_warning,
        new_number_tokens=(),
        diff_summary=base.diff_summary,
        validation_hash=canonical_sha256(payload),
    )


def application_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "source_job_id": "adult-job",
        "owner_scope": "owner-a",
        "owner_token": "lease-a",
        "project_id": 1,
        "chapter_id": 9,
        "target_start": 3,
        "target_end": 30,
        "chapter_revision_before": 0,
        "chapter_hash_before": "a" * 64,
        "target_hash_before": "b" * 64,
        "project_facts_hash": "c" * 64,
        "adult_confirmation_revision": 1,
        "adult_characters_hash": "d" * 64,
        "participant_hash": "e" * 64,
        "provider_scope_hash": "f" * 64,
        "safety_policy_hash": "1" * 64,
        "validator_policy_hash": "2" * 64,
        "validation_hash": "3" * 64,
        "warning_ack_hash": "",
        "validation": safe_validation(),
        "applicable": True,
        "candidate": "候选片段",
        "access_token_hash": "4" * 64,
        "snapshots": {},
    }
    row.update(overrides)
    return row


def _snapshot(seed: str, *, binding_version: int = 1) -> CandidateSnapshot:
    candidate = ModelCandidate(
        provider_id=1,
        provider_name="fake",
        model_key="fake-model",
        provider_model_id=1,
        pool_id=None,
        pool_name=None,
        pool_version=None,
        pool_position=None,
        provider_config_hash=seed * 64,
        capabilities=("json",),
        context_window=16_000,
    )
    return CandidateSnapshot(
        candidates=(candidate,),
        snapshot_hash=seed * 64,
        agent_config_hash=seed * 64,
        binding_version=binding_version,
    )


class FakeModelRouter:
    def __init__(
        self,
        results: list[RouteResult] | None = None,
        snapshots: dict[str, CandidateSnapshot] | None = None,
    ) -> None:
        self.snapshots = snapshots or {
            "main": _snapshot("a"),
            "safety": _snapshot("b"),
            "fact_guard": _snapshot("c"),
            "validation": _snapshot("b"),
        }
        self.results = list(results or [])
        self.requests: list[Any] = []
        self.stages: list[str] = []
        self.validation_requests: list[Any] = []
        self.execute_count = 0
        self.result: RouteResult | None = None
        self.next_result: RouteResult | None = None

    def resolve_candidates(self, agent: Any, stage: str = "main", snapshot: Any = None):
        if snapshot is not None:
            return snapshot
        key = getattr(agent, "review_kind", None) or (
            agent.get("review_kind") if isinstance(agent, dict) else None
        )
        return self.snapshots.get(str(key or stage), self.snapshots[stage])

    def execute(self, request: Any) -> RouteResult:
        self.execute_count += 1
        self.requests.append(request)
        self.stages.append(request.stage)
        if request.stage == "validation":
            self.validation_requests.append(request)
        request.on_progress({"action": "attempt", "provider_name": "fake", "model_key": "fake-model"})
        result = self.next_result or self.result or (self.results.pop(0) if self.results else None)
        self.next_result = None
        if result is None:
            result = RouteResult(
                job_id=request.job_id,
                output_text="候选片段",
                candidate_snapshot_hash=request.candidate_snapshot.snapshot_hash,
                attempts=(),
                finish_state="succeeded",
            )
        if result.output_text:
            request.on_delta(result.output_text)
        return result

    def execute_stream(self, request: Any):
        yield AIStreamChunk(
            type="progress",
            data={
                "phase": "route",
                "action": "attempt",
                "stage": request.stage,
                "provider_name": "fake",
                "model_key": "fake-model",
            },
        )
        return self.execute(request)


def run_concurrently(callable_: Callable[[], Any], count: int = 2) -> list[Any]:
    barrier = threading.Barrier(count)
    results: list[Any] = [None] * count

    def worker(index: int) -> None:
        barrier.wait()
        try:
            results[index] = callable_()
        except BaseException as exc:
            results[index] = exc

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    return results


def make_legacy_ai_database(path: Path):
    from pixiv_novel_sync.storage_db import Database

    db = Database(path)
    db.init_schema()
    provider_id = db.create_ai_provider(
        {
            "name": "legacy-provider",
            "provider_type": "openai",
            "default_model": "legacy-model",
            "enabled": True,
        }
    )
    agent_id = db.create_ai_agent(
        {
            "name": "legacy-agent",
            "task_type": "continue",
            "binding_type": "fixed",
            "provider_id": provider_id,
            "model": "legacy-model",
            "system_prompt": "legacy",
        }
    )
    db.conn.execute("UPDATE ai_agents SET id = 7 WHERE id = ?", (agent_id,))
    project_id = db.create_ai_writing_project({"name": "legacy-project", "settings": {}})
    chapter_id = db.create_ai_chapter(
        {"project_id": project_id, "chapter_number": 1, "content": "旧章节"}
    )
    db.conn.execute("UPDATE ai_chapters SET id = 9 WHERE id = ?", (chapter_id,))
    for table in (
        "ai_chapter_derivative_invalidations",
        "ai_polish_applications",
        "ai_project_characters",
        "ai_adult_review_bindings",
        "ai_adult_policy_state",
    ):
        db.conn.execute(f"DROP TABLE IF EXISTS {table}")
    db.conn.commit()
    return db


def seed_adult_project(db: Any) -> None:
    if db.get_ai_writing_project(1) is None:
        project_id = db.create_ai_writing_project({"name": "adult-project", "settings": {}})
        assert project_id == 1
    if db.get_ai_chapter(9) is None:
        chapter = "前文。安娜握住他的手，停顿片刻后仍保持原来的称呼和视角。后文。"
        chapter_id = db.create_ai_chapter(
            {
                "project_id": 1,
                "chapter_number": 1,
                "title": "第一章",
                "content": chapter,
            }
        )
        db.conn.execute("UPDATE ai_chapters SET id = 9 WHERE id = ?", (chapter_id,))

    if db.get_ai_provider(1) is None:
        provider_id = db.create_ai_provider(
            {
                "name": "adult-provider",
                "provider_type": "openai",
                "default_model": "adult-model",
                "enabled": True,
            }
        )
        assert provider_id == 1
    if db.get_ai_agent(7) is None:
        agent_id = db.create_ai_agent(
            {
                "name": "成人描写润色",
                "task_type": "adult_polish",
                "binding_type": "fixed",
                "provider_id": 1,
                "model": "adult-model",
                "system_prompt": "只输出替换片段",
                "required_capabilities": [],
            }
        )
        db.conn.execute("UPDATE ai_agents SET id = 7 WHERE id = ?", (agent_id,))

    characters = (
        (CHARACTER_A_ID, "安娜", "[\"安\"]", 25),
        (CHARACTER_B_ID, "林舟", "[\"林\"]", 27),
    )
    db.conn.executemany(
        """
        INSERT OR IGNORE INTO ai_project_characters (
            character_id, project_id, revision, canonical_name, aliases_json,
            age_years, age_basis, fictional, active
        ) VALUES (?, 1, 1, ?, ?, ?, '项目设定', 1, 1)
        """,
        characters,
    )
    confirmed = [
        {
            "character_id": character_id,
            "character_revision": 1,
            "confirmed_at": "2026-01-01T00:00:00+00:00",
        }
        for character_id, _name, _aliases, _age in characters
    ]
    db.conn.execute(
        """
        UPDATE ai_writing_projects
        SET adult_content_enabled = 1,
            adult_characters_confirmed = 1,
            fictional_characters_confirmed = 1,
            adult_characters_json = ?,
            adult_confirmation_revision = 1,
            adult_confirmation_updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
        """,
        (json.dumps(confirmed, ensure_ascii=False),),
    )
    db.conn.commit()
    if db.get_ai_job("adult-job") is None:
        db.create_ai_job(
            "adult-job",
            "adult_polish",
            7,
            {"project_id": 1, "chapter_id": 9},
            owner_token="lease-a",
        )
        db.conn.execute(
            """
            UPDATE ai_jobs
            SET owner_scope = 'owner-a', idempotency_key_hash = ?
            WHERE job_id = 'adult-job'
            """,
            (raw_sha256("adult-request-key-0001"),),
        )
    db.conn.commit()
