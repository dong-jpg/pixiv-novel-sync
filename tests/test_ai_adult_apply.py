from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from ai_adult_testkit import (
    CHARACTER_A_ID,
    CHARACTER_B_ID,
    FakeModelRouter,
    run_concurrently,
    safe_validation,
    seed_adult_project,
    valid_adult_payload,
)
from pixiv_novel_sync.ai.adult_types import (
    AdultConflictError,
    raw_sha256,
    warning_ack_hash,
)
from pixiv_novel_sync.ai.adult_policies import SAFETY_POLICY
from pixiv_novel_sync.ai.adult_validation import (
    compute_provider_scope_hash,
    compute_validation_hash,
)
from pixiv_novel_sync.ai.model_router import (
    CandidateSnapshot,
    ModelCandidate,
    PromptBudget,
    RouteResult,
)
from pixiv_novel_sync.ai.service import AIServiceError, AIWritingService
from pixiv_novel_sync.storage_db import Database


def _snapshot(seed: str, *, capabilities: tuple[str, ...] = ()) -> CandidateSnapshot:
    candidate = ModelCandidate(
        provider_id=1,
        provider_name=f"provider-{seed}",
        model_key=f"model-{seed}",
        provider_model_id=None,
        pool_id=None,
        pool_name=None,
        pool_version=None,
        pool_position=None,
        provider_config_hash=seed * 64,
        capabilities=capabilities,
        context_window=32_000,
    )
    payload = {
        "agent_config_hash": seed * 64,
        "binding_version": 1,
        "candidates": [asdict(candidate)],
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return CandidateSnapshot(
        candidates=(candidate,),
        snapshot_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        agent_config_hash=seed * 64,
        binding_version=1,
    )


class ApplyRouter(FakeModelRouter):
    def __init__(self) -> None:
        super().__init__(
            snapshots={
                "main": _snapshot("a"),
                "safety": _snapshot("b", capabilities=("json",)),
                "fact_guard": _snapshot("c", capabilities=("json",)),
            }
        )
        self.resolve_calls: list[tuple[str, str]] = []

    def close(self) -> None:
        pass

    def resolve_candidates(
        self,
        agent: Any,
        stage: str = "main",
        snapshot: CandidateSnapshot | None = None,
    ) -> CandidateSnapshot:
        task_type = str(getattr(agent, "task_type", ""))
        kind = (
            "safety"
            if task_type == "adult_safety_review"
            else "fact_guard"
            if task_type == "adult_fact_guard"
            else "main"
        )
        self.resolve_calls.append((kind, stage))
        if snapshot is not None:
            return snapshot
        resolved = self.snapshots[kind]
        binding_version = int(getattr(agent, "binding_version", 1))
        if binding_version == resolved.binding_version:
            return resolved
        config_hash = hashlib.sha256(
            f"{kind}:{binding_version}".encode("utf-8")
        ).hexdigest()
        snapshot_hash = hashlib.sha256(
            f"{resolved.snapshot_hash}:{config_hash}".encode("utf-8")
        ).hexdigest()
        return replace(
            resolved,
            binding_version=binding_version,
            agent_config_hash=config_hash,
            snapshot_hash=snapshot_hash,
        )

    def build_prompt_budget(
        self,
        _agent: Any,
        _snapshot: CandidateSnapshot,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> PromptBudget:
        assert messages
        return PromptBudget(
            effective_context_window=32_000,
            input_budget=24_000,
            output_reserve=max_tokens,
            message_overhead=4 * len(messages) + 2,
            safety_margin=256,
            estimator="utf8_bytes",
        )

    def execute(self, request: Any) -> RouteResult:
        self.execute_count += 1
        self.requests.append(request)
        self.stages.append(request.stage)
        if request.stage == "validation":
            self.validation_requests.append(request)
        output = '{"safe":true,"issues":[]}'
        request.on_delta(output)
        return RouteResult(
            job_id=request.job_id,
            output_text=output,
            candidate_snapshot_hash=request.candidate_snapshot.snapshot_hash,
            attempts=(),
            finish_state="succeeded",
        )


@pytest.fixture
def db(tmp_path: Path):
    database = Database(tmp_path / "adult-apply.db")
    database.init_schema()
    seed_adult_project(database)
    database.delete_ai_job("adult-job")
    database.conn.execute(
        """
        UPDATE ai_adult_review_bindings
        SET binding_type = 'fixed', provider_id = 1, model = 'adult-model',
            model_pool_id = NULL, enabled = 1
        WHERE review_kind IN ('safety', 'fact_guard')
        """
    )
    database.conn.commit()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def fake_router() -> ApplyRouter:
    return ApplyRouter()


@pytest.fixture
def service(db: Database, fake_router: ApplyRouter):
    instance = AIWritingService(db.path)
    instance.model_router.close()
    instance.model_router = fake_router
    try:
        yield instance
    finally:
        instance.close()


def _raw_candidate(prepared: Any, candidate: str) -> str:
    token = next(iter(prepared.prompt.token_map))
    masked = candidate.replace("安娜", token)
    boundary = prepared.prompt.boundary
    return (
        f"{boundary}_CANDIDATE_BEGIN\n"
        f"{masked}\n"
        f"{boundary}_CANDIDATE_END"
    )


def _prepare_candidate(
    service: AIWritingService,
    fake_router: ApplyRouter,
    *,
    idempotency_key: str,
) -> tuple[Any, str]:
    scope_hash = compute_provider_scope_hash(fake_router.snapshots)
    payload = valid_adult_payload(
        participant_character_ids=[CHARACTER_A_ID, CHARACTER_B_ID],
        provider_scope_hash=scope_hash,
        idempotency_key=idempotency_key,
    )
    job = service.prepare_adult_job(payload, "owner-a", owner_token="lease-a")
    candidate = job.target.replace("停顿片刻", "略作停顿")
    events = list(service.finish_adult_candidate(job, _raw_candidate(job, candidate)))
    assert events[-1].type == "done"
    return job, candidate


@pytest.fixture
def prepared(service: AIWritingService, fake_router: ApplyRouter):
    return _prepare_candidate(
        service,
        fake_router,
        idempotency_key="adult-apply-request-0001",
    )


def test_apply_changes_only_target_and_invalidates_derivatives(
    db: Database,
    service: AIWritingService,
    prepared: tuple[Any, str],
):
    job, candidate = prepared
    original = db.get_ai_chapter(job.request.chapter_id)["content"]
    expected = (
        original[: job.request.target_start]
        + candidate
        + original[job.request.target_end :]
    )

    result = service.apply_adult_polish(
        job.job_id,
        "owner-a",
        "",
        job.access_token,
    )

    chapter = db.get_ai_chapter(job.request.chapter_id)
    assert chapter["content"] == expected
    assert chapter["chapter_revision"] == job.request.chapter_revision + 1
    assert chapter["word_count"] == len(expected)
    assert result == {
        "application_id": result["application_id"],
        "chapter_revision_after": chapter["chapter_revision"],
        "chapter_hash_after": raw_sha256(expected),
        "already_applied": False,
    }
    application = db.get_application_for_owner(job.job_id, "owner-a")
    assert application is not None
    assert application["applied_at"] is not None
    assert application["chapter_hash_after"] == raw_sha256(expected)
    saved_job = db.get_adult_job(job.job_id, "owner-a")
    assert saved_job is not None
    assert saved_job.get("output_text") is None
    invalidation = db.conn.execute(
        "SELECT * FROM ai_chapter_derivative_invalidations WHERE chapter_id = ?",
        (job.request.chapter_id,),
    ).fetchone()
    assert invalidation is not None
    assert invalidation["chapter_revision"] == chapter["chapter_revision"]
    assert invalidation["reason"] == "adult_polish_applied"
    assert invalidation["status"] == "pending"


def test_two_concurrent_apply_calls_replace_once(
    db: Database,
    service: AIWritingService,
    prepared: tuple[Any, str],
):
    job, _candidate = prepared

    results = run_concurrently(
        lambda: service.apply_adult_polish(
            job.job_id,
            "owner-a",
            "",
            job.access_token,
        ),
        count=2,
    )

    assert all(isinstance(result, dict) for result in results)
    assert sorted(result["already_applied"] for result in results) == [False, True]
    chapter = db.get_ai_chapter(job.request.chapter_id)
    assert chapter["chapter_revision"] == job.request.chapter_revision + 1
    application = db.get_application_for_owner(job.job_id, "owner-a")
    assert application is not None
    assert application["chapter_revision_after"] == chapter["chapter_revision"]


def test_two_concurrent_policy_upgrade_apply_calls_return_one_existing_success(
    db: Database,
    service: AIWritingService,
    fake_router: ApplyRouter,
    monkeypatch: pytest.MonkeyPatch,
    prepared: tuple[Any, str],
):
    job, _candidate = prepared
    db.conn.execute(
        "UPDATE ai_polish_applications SET safety_policy_hash = ? WHERE source_job_id = ?",
        ("0" * 64, job.job_id),
    )
    db.conn.commit()
    phase2_barrier = threading.Barrier(2, timeout=5)
    original_revalidation = service._run_stored_revalidation

    def synchronized_revalidation(*args: Any):
        result = original_revalidation(*args)
        phase2_barrier.wait()
        return result

    monkeypatch.setattr(
        service,
        "_run_stored_revalidation",
        synchronized_revalidation,
    )
    calls_before = fake_router.execute_count

    results = run_concurrently(
        lambda: service.apply_adult_polish(
            job.job_id,
            "owner-a",
            "",
            job.access_token,
        ),
        count=2,
    )

    assert all(isinstance(result, dict) for result in results), results
    assert sorted(result["already_applied"] for result in results) == [False, True]
    assert fake_router.execute_count == calls_before + 4
    chapter = db.get_ai_chapter(job.request.chapter_id)
    assert chapter["chapter_revision"] == job.request.chapter_revision + 1


def test_revalidate_applied_candidate_fails_closed(
    service: AIWritingService,
    prepared: tuple[Any, str],
):
    job, _candidate = prepared
    service.apply_adult_polish(
        job.job_id,
        "owner-a",
        "",
        job.access_token,
    )

    with pytest.raises(AdultConflictError, match="已应用"):
        service.revalidate_stored_candidate(job.job_id, "owner-a")


def test_apply_rejects_chapter_revision_aba_and_leaves_content(
    db: Database,
    service: AIWritingService,
    prepared: tuple[Any, str],
):
    job, _candidate = prepared
    original = db.get_ai_chapter(job.request.chapter_id)["content"]
    db.update_ai_chapter(job.request.chapter_id, {"content": "临时章节正文"})
    db.update_ai_chapter(job.request.chapter_id, {"content": original})

    with pytest.raises(AdultConflictError, match="409.*revision"):
        service.apply_adult_polish(
            job.job_id,
            "owner-a",
            "",
            job.access_token,
        )

    chapter = db.get_ai_chapter(job.request.chapter_id)
    assert chapter["content"] == original
    assert chapter["chapter_revision"] == job.request.chapter_revision + 2
    application = db.get_application_for_owner(job.job_id, "owner-a")
    assert application is not None
    assert application["applied_at"] is None


@pytest.mark.parametrize(
    ("owner_scope", "access_token"),
    [
        ("owner-b", "lease-placeholder"),
        ("owner-a", "wrong-access-token"),
    ],
)
def test_apply_rejects_wrong_owner_or_access_token(
    db: Database,
    service: AIWritingService,
    prepared: tuple[Any, str],
    owner_scope: str,
    access_token: str,
):
    job, _candidate = prepared
    original = db.get_ai_chapter(job.request.chapter_id)
    effective_access_token = (
        job.access_token if owner_scope != "owner-a" else access_token
    )

    with pytest.raises(AdultConflictError, match="409"):
        service.apply_adult_polish(
            job.job_id,
            owner_scope,
            "",
            effective_access_token,
        )

    chapter = db.get_ai_chapter(job.request.chapter_id)
    assert chapter["content"] == original["content"]
    assert chapter["chapter_revision"] == original["chapter_revision"]


def test_apply_requires_present_and_exact_warning_acknowledgment(
    db: Database,
    service: AIWritingService,
    prepared: tuple[Any, str],
):
    job, _candidate = prepared
    warning_code = "perspective_warning"
    pending = replace(
        safe_validation(),
        warnings=(warning_code,),
        validation_hash="",
    )
    validation = replace(
        pending,
        validation_hash=compute_validation_hash(pending),
    )
    db.conn.execute(
        """
        UPDATE ai_polish_applications
        SET validation_json = ?, validation_hash = ?
        WHERE source_job_id = ?
        """,
        (
            json.dumps(asdict(validation), ensure_ascii=False),
            validation.validation_hash,
            job.job_id,
        ),
    )
    db.conn.commit()

    with pytest.raises(AIServiceError, match="必须提供"):
        service.apply_adult_polish(
            job.job_id,
            "owner-a",
            None,
            job.access_token,
        )
    with pytest.raises(AdultConflictError, match="warning"):
        service.apply_adult_polish(
            job.job_id,
            "owner-a",
            "",
            job.access_token,
        )

    application = db.get_application_for_owner(job.job_id, "owner-a")
    assert application is not None
    acknowledgment = warning_ack_hash(
        validation.validation_hash,
        application["safety_policy_hash"],
        application["validator_policy_hash"],
        [warning_code],
    )
    result = service.apply_adult_polish(
        job.job_id,
        "owner-a",
        acknowledgment,
        job.access_token,
    )

    assert result["already_applied"] is False
    saved = db.get_application_for_owner(job.job_id, "owner-a")
    assert saved is not None
    assert saved["warning_ack_hash"] == acknowledgment


def test_scope_or_binding_change_returns_409_without_provider_call(
    db: Database,
    service: AIWritingService,
    fake_router: ApplyRouter,
    prepared: tuple[Any, str],
):
    job, _candidate = prepared
    calls_before = fake_router.execute_count
    db.conn.execute(
        """
        UPDATE ai_polish_applications
        SET safety_policy_hash = ?
        WHERE source_job_id = ?
        """,
        ("0" * 64, job.job_id),
    )
    db.conn.commit()
    db.cas_update_review_binding(
        "safety",
        expected_version=1,
        route={
            "binding_type": "fixed",
            "provider_id": 1,
            "model": "changed-model",
            "model_pool_id": None,
            "enabled": True,
        },
    )

    with pytest.raises(AdultConflictError, match="Provider 范围|binding"):
        service.apply_adult_polish(
            job.job_id,
            "owner-a",
            "",
            job.access_token,
        )

    assert fake_router.execute_count == calls_before
    application = db.get_application_for_owner(job.job_id, "owner-a")
    assert application is not None
    assert application["applied_at"] is None


def test_policy_upgrade_revalidates_outside_lock_then_applies(
    db: Database,
    service: AIWritingService,
    fake_router: ApplyRouter,
    prepared: tuple[Any, str],
):
    job, _candidate = prepared
    db.conn.execute(
        """
        UPDATE ai_polish_applications
        SET safety_policy_hash = ?
        WHERE source_job_id = ?
        """,
        ("0" * 64, job.job_id),
    )
    db.conn.commit()
    calls_before = fake_router.execute_count

    result = service.apply_adult_polish(
        job.job_id,
        "owner-a",
        "",
        job.access_token,
    )

    assert result["already_applied"] is False
    assert fake_router.execute_count == calls_before + 2
    assert fake_router.stages[-2:] == ["validation", "validation"]
    application = db.get_application_for_owner(job.job_id, "owner-a")
    assert application is not None
    assert application["safety_policy_hash"] == SAFETY_POLICY.expected_hash
    assert application["applied_at"] is not None


def test_policy_upgrade_rejects_phase3_chapter_change(
    db: Database,
    service: AIWritingService,
    fake_router: ApplyRouter,
    monkeypatch: pytest.MonkeyPatch,
    prepared: tuple[Any, str],
):
    job, _candidate = prepared
    db.conn.execute(
        """
        UPDATE ai_polish_applications
        SET safety_policy_hash = ?
        WHERE source_job_id = ?
        """,
        ("0" * 64, job.job_id),
    )
    db.conn.commit()
    original_execute = fake_router.execute
    changed_content: list[str] = []

    def execute_with_concurrent_change(request: Any) -> RouteResult:
        result = original_execute(request)
        if request.stage == "validation" and not changed_content:
            chapter = db.get_ai_chapter(job.request.chapter_id)
            changed = chapter["content"] + "并发追加。"
            db.update_ai_chapter(job.request.chapter_id, {"content": changed})
            changed_content.append(changed)
        return result

    monkeypatch.setattr(fake_router, "execute", execute_with_concurrent_change)

    with pytest.raises(AdultConflictError, match="重审期间|章节"):
        service.apply_adult_polish(
            job.job_id,
            "owner-a",
            "",
            job.access_token,
        )

    assert db.get_ai_chapter(job.request.chapter_id)["content"] == changed_content[0]
    application = db.get_application_for_owner(job.job_id, "owner-a")
    assert application is not None
    assert application["applied_at"] is None


def test_policy_upgrade_requires_request_guard_before_provider_call(
    db: Database,
    service: AIWritingService,
    fake_router: ApplyRouter,
    prepared: tuple[Any, str],
):
    job, _candidate = prepared
    application = db.get_application_for_owner(job.job_id, "owner-a")
    assert application is not None
    snapshots = dict(application["snapshots"])
    snapshots.pop("request_guard")
    db.conn.execute(
        """
        UPDATE ai_polish_applications
        SET safety_policy_hash = ?, snapshots_json = ?
        WHERE source_job_id = ?
        """,
        ("0" * 64, json.dumps(snapshots, ensure_ascii=False), job.job_id),
    )
    db.conn.commit()
    calls_before = fake_router.execute_count

    with pytest.raises(AdultConflictError, match="快照|重新生成"):
        service.apply_adult_polish(job.job_id, "owner-a", "", job.access_token)

    assert fake_router.execute_count == calls_before


def test_policy_upgrade_rejects_expired_output_before_provider_call(
    db: Database,
    service: AIWritingService,
    fake_router: ApplyRouter,
    prepared: tuple[Any, str],
):
    job, _candidate = prepared
    db.conn.execute(
        "UPDATE ai_polish_applications SET safety_policy_hash = ? WHERE source_job_id = ?",
        ("0" * 64, job.job_id),
    )
    db.conn.execute(
        "UPDATE ai_jobs SET output_text = NULL WHERE job_id = ?",
        (job.job_id,),
    )
    db.conn.commit()
    calls_before = fake_router.execute_count

    with pytest.raises(AdultConflictError, match="过期|重新生成"):
        service.apply_adult_polish(job.job_id, "owner-a", "", job.access_token)

    assert fake_router.execute_count == calls_before


def test_policy_upgrade_rejects_non_succeeded_job_status_before_provider_call(
    db: Database,
    service: AIWritingService,
    fake_router: ApplyRouter,
    prepared: tuple[Any, str],
):
    job, _candidate = prepared
    db.conn.execute(
        "UPDATE ai_polish_applications SET safety_policy_hash = ? WHERE source_job_id = ?",
        ("0" * 64, job.job_id),
    )
    db.conn.execute(
        "UPDATE ai_jobs SET status = 'failed' WHERE job_id = ?",
        (job.job_id,),
    )
    db.conn.commit()
    calls_before = fake_router.execute_count

    with pytest.raises(AdultConflictError, match="终态|状态"):
        service.apply_adult_polish(job.job_id, "owner-a", "", job.access_token)

    assert fake_router.execute_count == calls_before


def test_apply_rejects_validation_json_hash_mismatch(
    db: Database,
    service: AIWritingService,
    prepared: tuple[Any, str],
):
    job, _candidate = prepared
    application = db.get_application_for_owner(job.job_id, "owner-a")
    assert application is not None
    corrupted = dict(application["validation"])
    corrupted["warnings"] = ["perspective_warning"]
    db.conn.execute(
        "UPDATE ai_polish_applications SET validation_json = ? WHERE source_job_id = ?",
        (json.dumps(corrupted, ensure_ascii=False), job.job_id),
    )
    db.conn.commit()
    forged_ack = warning_ack_hash(
        application["validation_hash"],
        application["safety_policy_hash"],
        application["validator_policy_hash"],
        corrupted["warnings"],
    )
    original = db.get_ai_chapter(job.request.chapter_id)["content"]

    with pytest.raises(AdultConflictError, match="校验|validation"):
        service.apply_adult_polish(
            job.job_id,
            "owner-a",
            forged_ack,
            job.access_token,
        )

    assert db.get_ai_chapter(job.request.chapter_id)["content"] == original


def test_policy_upgrade_new_warning_invalidates_old_acknowledgment(
    db: Database,
    service: AIWritingService,
    monkeypatch: pytest.MonkeyPatch,
    prepared: tuple[Any, str],
):
    job, _candidate = prepared
    old_pending = replace(
        safe_validation(),
        warnings=("new_number_warning",),
        validation_hash="",
    )
    old_validation = replace(
        old_pending,
        validation_hash=compute_validation_hash(old_pending),
    )
    application = db.get_application_for_owner(job.job_id, "owner-a")
    assert application is not None
    old_ack = warning_ack_hash(
        old_validation.validation_hash,
        application["safety_policy_hash"],
        application["validator_policy_hash"],
        old_validation.warnings,
    )
    db.conn.execute(
        """
        UPDATE ai_polish_applications
        SET validation_json = ?, validation_hash = ?, warning_ack_hash = ?,
            safety_policy_hash = ?
        WHERE source_job_id = ?
        """,
        (
            json.dumps(asdict(old_validation), ensure_ascii=False),
            old_validation.validation_hash,
            old_ack,
            "0" * 64,
            job.job_id,
        ),
    )
    db.conn.commit()
    original_revalidation = service._run_stored_revalidation

    def revalidate_with_new_warning(*args: Any):
        validation, safety, fact = original_revalidation(*args)
        pending = replace(
            validation,
            warnings=("perspective_warning",),
            perspective_warning=True,
            validation_hash="",
        )
        return (
            replace(pending, validation_hash=compute_validation_hash(pending)),
            safety,
            fact,
        )

    monkeypatch.setattr(service, "_run_stored_revalidation", revalidate_with_new_warning)

    with pytest.raises(AdultConflictError, match="warning"):
        service.apply_adult_polish(job.job_id, "owner-a", old_ack, job.access_token)

    refreshed = db.get_application_for_owner(job.job_id, "owner-a")
    assert refreshed is not None
    assert refreshed["warning_ack_hash"] == ""
    with pytest.raises(AdultConflictError, match="warning"):
        service.apply_adult_polish(job.job_id, "owner-a", old_ack, job.access_token)
    new_ack = warning_ack_hash(
        refreshed["validation_hash"],
        refreshed["safety_policy_hash"],
        refreshed["validator_policy_hash"],
        refreshed["validation"]["warnings"],
    )
    result = service.apply_adult_polish(
        job.job_id,
        "owner-a",
        new_ack,
        job.access_token,
    )
    assert result["already_applied"] is False


def test_cleanup_ai_jobs_deletes_expired_unapplied_application_before_job(
    db: Database,
    prepared: tuple[Any, str],
):
    job, _candidate = prepared
    db.conn.execute(
        "UPDATE ai_jobs SET created_at = datetime('now', '-4 days') WHERE job_id = ?",
        (job.job_id,),
    )
    db.conn.execute(
        """
        UPDATE ai_polish_applications
        SET created_at = datetime('now', '-4 days')
        WHERE source_job_id = ?
        """,
        (job.job_id,),
    )
    db.conn.commit()

    assert db.cleanup_ai_jobs(keep_days=3) == 1
    assert db.get_adult_job(job.job_id, "owner-a") is None
    assert db.get_application_for_owner(job.job_id, "owner-a") is None


def test_cleanup_ai_jobs_preserves_job_referenced_by_unapplied_application(
    db: Database,
    prepared: tuple[Any, str],
):
    job, _candidate = prepared
    db.conn.execute(
        "UPDATE ai_jobs SET created_at = datetime('now', '-4 days') WHERE job_id = ?",
        (job.job_id,),
    )
    db.conn.commit()

    assert db.cleanup_ai_jobs(keep_days=3) == 0
    assert db.get_adult_job(job.job_id, "owner-a") is not None
    assert db.get_application_for_owner(job.job_id, "owner-a") is not None


def test_startup_repairs_only_orphaned_adult_candidate(
    db: Database,
    service: AIWritingService,
    fake_router: ApplyRouter,
    prepared: tuple[Any, str],
):
    orphan_job, _candidate = prepared
    protected_job, _protected_candidate = _prepare_candidate(
        service,
        fake_router,
        idempotency_key="adult-apply-request-0002",
    )
    db.create_ai_job("general-output", "general", None, {})
    db.conn.execute(
        """
        UPDATE ai_jobs
        SET status = 'succeeded', output_text = 'non-adult output'
        WHERE job_id = 'general-output'
        """
    )
    db.conn.execute(
        "DELETE FROM ai_polish_applications WHERE source_job_id = ?",
        (orphan_job.job_id,),
    )
    db.conn.commit()

    assert db.fail_stale_ai_jobs() == 1
    orphan = db.get_adult_job(orphan_job.job_id, "owner-a")
    assert orphan is not None
    assert orphan["status"] == "failed"
    assert orphan["output_text"] is None
    protected = db.get_adult_job(protected_job.job_id, "owner-a")
    assert protected is not None
    assert protected["status"] == "succeeded"
    assert protected["output_text"] is not None
    general = db.get_ai_job("general-output")
    assert general is not None
    assert general["status"] == "succeeded"
    assert general["output_text"] == "non-adult output"


def test_old_process_and_storage_cannot_apply_after_database_policy_upgrade(db, service, fake_router, prepared):
    from pixiv_novel_sync.ai.service import AIServiceError
    job, _ = prepared
    original = db.get_ai_chapter(9)["content"]
    application = db.get_application_for_owner(job.job_id, "owner-a")
    snapshot = service._build_apply_snapshot(db, application, db.get_adult_job(job.job_id, "owner-a"))
    previous = db.list_adult_policy_state()
    released = [{**row, "policy_version": row["policy_version"] + 1, "policy_hash": "d" * 64, "prompt_hash": "e" * 64, "schema_hash": "f" * 64} for row in previous]
    db.upgrade_adult_policy_state(previous, released)
    calls = fake_router.execute_count
    with pytest.raises((AIServiceError, AdultConflictError)):
        service.apply_adult_polish(job.job_id, "owner-a", "", job.access_token)
    with pytest.raises(AdultConflictError):
        db.apply_adult_polish(job.job_id, "owner-a", "", raw_sha256(job.access_token), snapshot)
    assert fake_router.execute_count == calls
    assert db.get_ai_chapter(9)["content"] == original
    assert db.get_application_for_owner(job.job_id, "owner-a")["applied_at"] is None


def test_accepted_revalidation_cancel_between_parse_and_success_cas_never_applies(db, service, prepared, monkeypatch):
    from pixiv_novel_sync.ai.service import AIServiceError
    job, _ = prepared
    original = db.get_ai_chapter(9)["content"]
    db.conn.execute("UPDATE ai_polish_applications SET safety_policy_hash=? WHERE source_job_id=?", ("0" * 64, job.job_id))
    parse = service._parse_review_output
    calls = []
    def parse_then_cancel(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            assert db.request_adult_review_cancel(job.job_id, "owner-a") is True
        return parse(*args, **kwargs)
    monkeypatch.setattr(service, "_parse_review_output", parse_then_cancel)
    with pytest.raises((AIServiceError, AdultConflictError)):
        service.apply_adult_polish(job.job_id, "owner-a", "", job.access_token)
    assert db.get_ai_chapter(9)["content"] == original
    assert db.get_application_for_owner(job.job_id, "owner-a")["applied_at"] is None
    assert db.conn.execute("SELECT COUNT(*) FROM ai_jobs WHERE parent_job_id=? AND status='succeeded' AND cancel_requested=1", (job.job_id,)).fetchone()[0] == 0


def test_upgrade_warning_is_replayable_then_acknowledged_through_events(db, service, prepared, monkeypatch, tmp_path):
    import time
    from flask import Flask
    import pixiv_novel_sync.ai_web as web
    from pixiv_novel_sync.ai.adult_auth import AdultOwner, sign_adult_access
    from test_ai_adult_web import _settings
    from test_ai_adult_integration import _parse_sse
    job, _ = prepared
    db.conn.execute("UPDATE ai_polish_applications SET safety_policy_hash=? WHERE source_job_id=?", ("0" * 64, job.job_id))
    db.conn.execute("UPDATE ai_jobs SET output_json=json_set(output_json,'$.code','policy_upgrade_required') WHERE job_id=?", (job.job_id,))
    revalidate = service._run_stored_revalidation
    def with_warning(*args):
        result, safety, fact = revalidate(*args)
        result = replace(result, warnings=("perspective_changed",), perspective_warning=True, validation_hash="")
        return replace(result, validation_hash=compute_validation_hash(result)), safety, fact
    monkeypatch.setattr(service, "_run_stored_revalidation", with_warning)
    with pytest.raises(AdultConflictError, match="warning"):
        service.apply_adult_polish(job.job_id, "owner-a", "", job.access_token)
    app = Flask(__name__)
    app.secret_key = "revalidation-events-key"
    settings = _settings(tmp_path, "dashboard-secret")
    settings = replace(settings, storage=replace(settings.storage, db_path=db.path))
    owner = AdultOwner(scope="owner-a", authenticated_at=time.time_ns())
    monkeypatch.setattr(web, "require_adult_owner", lambda settings: owner)
    web.register_ai_routes(app, settings)
    with app.app_context():
        token = sign_adult_access(owner, job.job_id)
    response = app.test_client().get(f"/api/dashboard/ai/polish/adult/{job.job_id}/events", headers={"X-Adult-Access-Token": token}, buffered=True)
    events = _parse_sse(response)
    assert "error" not in events, events
    ack = events["validation"][0]["warning_ack_hash"]
    assert ack
    service.apply_adult_polish(job.job_id, "owner-a", ack, job.access_token)
    assert db.get_application_for_owner(job.job_id, "owner-a")["applied_at"] is not None


def test_policy_upgrade_between_preflight_and_job_creation_never_publishes_candidate(db, service, fake_router, monkeypatch):
    original_content = db.get_ai_chapter(9)["content"]
    resolve = fake_router.resolve_candidates
    upgraded = []
    def upgrade_then_resolve(*args, **kwargs):
        if not upgraded:
            previous = db.list_adult_policy_state()
            released = [{**row, "policy_version": row["policy_version"] + 1,
                         "policy_hash": "d" * 64, "prompt_hash": "e" * 64,
                         "schema_hash": "f" * 64} for row in previous]
            db.upgrade_adult_policy_state(previous, released)
            upgraded.append(True)
        return resolve(*args, **kwargs)
    monkeypatch.setattr(fake_router, "resolve_candidates", upgrade_then_resolve)
    payload = valid_adult_payload(
        participant_character_ids=[CHARACTER_A_ID, CHARACTER_B_ID],
        provider_scope_hash=compute_provider_scope_hash(fake_router.snapshots),
        idempotency_key="upgrade-between-preflight-and-job",
    )
    job = service.prepare_adult_job(payload, "owner-a", owner_token="lease-a")
    candidate = job.target.replace("停顿片刻", "略作停顿")
    events = list(service.finish_adult_candidate(job, _raw_candidate(job, candidate)))
    assert upgraded
    assert not any(event.type in {"candidate", "done"} for event in events)
    assert events[-1].type == "error"
    assert db.get_application_for_owner(job.job_id, "owner-a") is None
    parent = db.get_adult_job(job.job_id, "owner-a")
    assert parent["status"] != "succeeded"
    assert not parent.get("output_text")
    assert db.get_ai_chapter(9)["content"] == original_content
