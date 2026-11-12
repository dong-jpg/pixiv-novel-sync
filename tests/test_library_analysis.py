from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.models import NovelTextRecord
from pixiv_novel_sync.preference_learning.library import LibraryAnalysisService
from pixiv_novel_sync.storage.library_analysis import migrate_library_analysis
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError
from work_analysis_api_helpers import RecordingCoordinator, facade_fixture


@pytest.fixture
def library(tmp_path, monkeypatch):
    import requests

    def forbidden(*args, **kwargs):
        raise AssertionError("library discovery must never call a remote service")

    monkeypatch.setattr(requests.Session, "request", forbidden)
    env = facade_fixture(tmp_path / "library.db")
    factory = lambda: Database(env.path)
    service = LibraryAnalysisService(factory, lambda: env.ai, env.coordinator)
    service.startup_recover()
    configuration = {
        "scope": "all", "agent_id": env.agent_id, "output_tokens": 1000,
        "work_budget": {"request_limit": 16, "token_limit": 500000, "active_seconds_limit": 600},
        "batch_budget": {"request_limit": 64, "token_limit": 2000000, "active_seconds_limit": 2400},
        "work_limit": 4, "interval_minutes": 60,
        "confirmations": {"external_send_confirmed": False, "budget_confirmed": False,
                          "current_archive_only": False, "order_confirmed": False},
    }
    yield SimpleNamespace(service=service, env=env, db=env.db, factory=factory, configuration=configuration)
    env.ai.close()
    env.db.close()


def confirmed(library, key="1" * 32, **changes):
    payload = deepcopy(library.configuration)
    payload.update(changes)
    preview = library.service.preview(payload)
    payload["confirmations"] = {name: True for name in payload["confirmations"]}
    payload.update(preview_fingerprint=preview["preview_fingerprint"], idempotency_key=key)
    return payload


def enable(library, **changes):
    value = confirmed(library, **changes)
    value["preview_fingerprint"] = library.service.preview({**library.configuration, **changes, "purpose": "policy"})["preview_fingerprint"]
    value.pop("idempotency_key")
    value.update(enabled=True, expected_version=library.service.state()["policy"]["version"])
    return library.service.policy(value)


def add_novel(db, novel_id, *, series_id=None, body="Unique synthetic library text"):
    with db.transaction() as conn:
        if series_id is not None:
            conn.execute("INSERT OR IGNORE INTO series(series_id,title,user_id,total_novels) VALUES(?,'Synthetic series',1,2)", (series_id,))
        conn.execute(
            "INSERT INTO novels(novel_id,user_id,series_id,title,visible,restrict_value,x_restrict,text_length,"
            "total_bookmarks,total_views,tags_json,raw_json,meta_hash,create_date) "
            "VALUES(?,1,?,'Synthetic source',1,'public',0,?,0,0,'[]','{}','fixture','2026-01-02')",
            (novel_id, series_id, len(body)),
        )
        db.upsert_novel_text(NovelTextRecord(novel_id, body, None, hashlib.sha256(body.encode()).hexdigest()))


def created_runs(db):
    return [dict(row) for row in db.conn.execute("SELECT r.* FROM work_analysis_runs r JOIN library_analysis_targets t ON t.run_id=r.run_id WHERE t.status='created' ORDER BY t.rowid")]


def cancel_run(db, run):
    current = db.get_work_analysis(run["run_id"])
    return db.request_work_analysis_stop(run["run_id"], "cancel", current["version"])


def test_default_off_and_preview_are_read_only(library):
    assert library.service.state()["policy"]["enabled"] is False
    assert library.service.tick() is False
    counts = {table: library.db.conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
              for table in ("work_analysis_runs", "library_analysis_batches", "library_analysis_targets", "ai_jobs")}
    preview = library.service.preview(library.configuration)
    assert preview["can_create"] and preview["candidate_count"] == 1
    assert preview["targets"] and preview["samples"][0]["source_id"] == 101
    assert "PRIVATE_BODY_END" not in str(preview)
    for table, count in counts.items():
        assert library.db.conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == count


def test_preview_and_state_serialize_through_flask_jsonify(library):
    from flask import Flask, jsonify

    app = Flask(__name__)
    app.add_url_rule("/preview", endpoint="preview", view_func=lambda: jsonify(library.service.preview(library.configuration)))
    app.add_url_rule("/state", endpoint="state", view_func=lambda: jsonify(library.service.state()))
    assert app.test_client().get("/preview").status_code == 200
    assert app.test_client().get("/state").status_code == 200


@pytest.mark.parametrize("field,value", [("scope", []), ("scope", "everything"), ("work_limit", True),
                                         ("work_limit", 100001), ("interval_minutes", 0),
                                         ("agent_id", "1"), ("output_tokens", False),
                                         ("batch_budget", {"request_limit": 1})])
def test_invalid_library_fields_fail_closed_without_creating_work(library, field, value):
    with pytest.raises(WorkAnalysisError):
        library.service.preview({**library.configuration, field: value})
    assert not created_runs(library.db)


def test_missing_consent_stale_preview_and_idempotent_unknown_commit(library):
    value = confirmed(library)
    rejected = deepcopy(value)
    rejected["confirmations"]["external_send_confirmed"] = False
    with pytest.raises(WorkAnalysisError, match="confirmation_required"):
        library.service.create(rejected)
    batch = library.service.create(value)
    add_novel(library.db, 102)
    assert library.service.create(value)["batch_id"] == batch["batch_id"]
    value["idempotency_key"] = "2" * 32
    with pytest.raises(WorkAnalysisError, match="stale_preview"):
        library.service.create(value)
    changed = deepcopy(value)
    changed.update(idempotency_key="1" * 32, work_limit=3)
    with pytest.raises(WorkAnalysisError, match="idempotency_conflict"):
        library.service.create(changed)


def test_single_tick_uses_existing_parent_with_atomic_allocation(library):
    batch = library.service.create(confirmed(library))
    assert library.service.tick()
    runs = created_runs(library.db)
    assert len(runs) == 1 and runs[0]["status"] == "queued"
    assert runs[0]["request_count"] == 0
    detail = library.service.batch(batch["batch_id"])
    assert detail["batch"]["allocated"] == library.configuration["work_budget"]
    assert detail["batch"]["work_count"] == 1
    assert detail["items"][0]["run_id"] == runs[0]["run_id"]
    assert library.service.tick() is False
    assert len(created_runs(library.db)) == 1


def test_preview_without_a_manual_run_does_not_veto_later_library_analysis(library):
    library.env.service.preview(library.env.payload)
    library.service.preview(library.configuration)
    assert library.db.conn.execute("SELECT COUNT(*) FROM work_analysis_runs").fetchone()[0] == 0
    assert library.db.conn.execute("SELECT COUNT(*) FROM library_analysis_targets").fetchone()[0] == 0
    batch = library.service.create(confirmed(library))
    library.service.tick()
    item = library.service.batch(batch["batch_id"])["items"][0]
    assert item["status"] == "created" and item["run_id"]


def test_series_and_standalone_are_unique_work_targets(library):
    add_novel(library.db, 102, series_id=10)
    add_novel(library.db, 103, series_id=10, body="Synthetic final chapter")
    preview = library.service.preview(library.configuration)
    assert {(row["source_kind"], row["source_id"]) for row in preview["samples"]} == {("novel", 101), ("series", 10)}
    batch = library.service.create(confirmed(library))
    library.service.tick()
    cancel_run(library.db, created_runs(library.db)[0])
    library.service.tick()
    runs = created_runs(library.db)
    assert len(runs) == 2 and runs[1]["source_kind"] == "series"
    assert library.db.conn.execute("SELECT COUNT(*) FROM work_analysis_sources WHERE run_id=?", (runs[1]["run_id"],)).fetchone()[0] == 2
    assert library.service.batch(batch["batch_id"])["batch"]["work_count"] == 2


def test_existing_cancelled_attempt_is_never_automatically_retried(library):
    library.service.create(confirmed(library))
    library.service.tick()
    run = created_runs(library.db)[0]
    cancel_run(library.db, run)
    library.service.tick()
    library.service.tick()
    second = library.service.create(confirmed(library, key="2" * 32))
    library.service.tick()
    item = library.service.batch(second["batch_id"])["items"][0]
    assert item["status"] == "skipped" and item["reason"] == "already_attempted"
    assert item["run_id"] == run["run_id"]
    assert len(created_runs(library.db)) == 1


def test_same_hash_refresh_does_not_schedule_but_real_update_does(library):
    library.service.create(confirmed(library))
    library.service.tick()
    original = created_runs(library.db)[0]
    cancel_run(library.db, original)
    for _ in range(2):
        library.service.tick()
    revision = library.db.conn.execute("SELECT revision FROM library_analysis_clock").fetchone()[0]
    with library.db.transaction() as conn:
        conn.execute("UPDATE novel_texts SET fetched_at='2030-01-01' WHERE novel_id=101")
        conn.execute("UPDATE novels SET title='Synthetic metadata refresh' WHERE novel_id=101")
    assert library.db.conn.execute("SELECT revision FROM library_analysis_clock").fetchone()[0] == revision
    body = "Different synthetic text with changed final words"
    library.db.upsert_novel_text(NovelTextRecord(101, body, None, hashlib.sha256(body.encode()).hexdigest()))
    library.service.create(confirmed(library, key="2" * 32))
    library.service.tick()
    assert len(created_runs(library.db)) == 2
    assert created_runs(library.db)[0]["status"] == "invalidated"


def test_new_scope_observes_old_id_backfill_and_not_seeded_archive(library):
    # Recreate just the discovery index at its first-install boundary. Existing
    # body rows must become baseline, while a newly archived smaller id is new.
    with library.db.transaction() as conn:
        conn.execute("DELETE FROM library_analysis_sources")
        conn.execute("UPDATE library_analysis_clock SET seeded=0,revision=0")
        migrate_library_analysis(conn)
    assert library.service.preview({**library.configuration, "scope": "new"})["candidate_count"] == 0
    add_novel(library.db, 7)
    preview = library.service.preview({**library.configuration, "scope": "new"})
    assert preview["candidate_count"] == 1 and preview["samples"][0]["source_id"] == 7
    with library.db.transaction() as conn:
        migrate_library_analysis(conn)
    assert library.service.preview({**library.configuration, "scope": "new"})["candidate_count"] == 1


def test_total_cap_stops_second_parent_and_child_cannot_bypass_it(library):
    add_novel(library.db, 102)
    batch = library.service.create(confirmed(library, batch_budget=library.configuration["work_budget"]))
    library.service.tick()
    run = created_runs(library.db)[0]
    current = library.db.get_work_analysis(run["run_id"])
    larger = {**current["budget"], "request_limit": 17}
    with pytest.raises(WorkAnalysisError, match="library_budget_exhausted"):
        library.db.update_work_analysis_budget(run["run_id"], larger, current["version"])
    cancel_run(library.db, run)
    library.service.tick()
    assert len(created_runs(library.db)) == 1
    assert library.service.batch(batch["batch_id"])["batch"]["reason"] == "allocation_exhausted"


def test_explicit_child_budget_increase_allocates_from_same_authority(library):
    enable(library)
    library.service.tick()
    run = created_runs(library.db)[0]
    before = library.db.get_work_analysis(run["run_id"])
    changed = library.db.update_work_analysis_budget(run["run_id"], {**before["budget"], "request_limit": 18}, before["version"])
    state = library.service.state()
    assert changed["budget"]["request_limit"] == 18
    assert state["policy"]["allocated"]["request_limit"] == 18
    assert state["batches"][0]["allocated"]["request_limit"] == 18


def test_parent_creation_and_allocation_roll_back_together(library, monkeypatch):
    batch = library.service.create(confirmed(library))
    original = Database.create_work_analysis

    def injected(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("synthetic failure before allocation commit")

    monkeypatch.setattr(Database, "create_work_analysis", injected)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        library.service.tick()
    assert not created_runs(library.db)
    assert library.db.conn.execute("SELECT COUNT(*) FROM work_analysis_runs").fetchone()[0] == 0
    detail = library.service.batch(batch["batch_id"])
    assert detail["total"] == 0 and detail["batch"]["work_count"] == 0
    assert not any(detail["batch"]["allocated"].values())


def test_concurrent_discovery_cannot_duplicate_parent_or_allocation(library):
    batch = library.service.create(confirmed(library))
    second = LibraryAnalysisService(library.factory, lambda: library.env.ai, library.env.coordinator)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda service: service.tick(), (library.service, second)))
    assert len(created_runs(library.db)) == 1
    assert library.service.batch(batch["batch_id"])["batch"]["allocated"] == library.configuration["work_budget"]


def test_policy_caps_survive_disable_reenable_and_new_batches(library):
    policy = enable(library, work_limit=1)
    library.service.tick()
    run = created_runs(library.db)[0]
    state = library.service.state()
    disabled = library.service.policy({"enabled": False, "expected_version": state["policy"]["version"]})
    assert disabled["allocated"] == library.configuration["work_budget"]
    assert library.db.get_work_analysis(run["run_id"])["status"] == "paused"
    add_novel(library.db, 102)
    enabled = enable(library, work_limit=1)
    assert enabled["work_count"] == 1
    library.service.tick()
    assert len(created_runs(library.db)) == 1
    assert library.service.state()["policy"]["reason"] == "allocation_exhausted"


def test_policy_cas_configuration_change_and_new_preview(library):
    value = confirmed(library)
    value.pop("idempotency_key")
    value.update(enabled=True, expected_version=1)
    policy = library.service.policy(value)
    with pytest.raises(WorkAnalysisError, match="version_conflict"):
        library.service.policy({"enabled": False, "expected_version": 1})
    with library.db.transaction() as conn:
        conn.execute("UPDATE ai_agents SET system_prompt='Changed synthetic prompt' WHERE id=?", (library.env.agent_id,))
    library.service.tick()
    assert library.service.state()["policy"]["reason"] == "configuration_changed"
    assert not created_runs(library.db)


def test_exhausted_stop_retries_report_failure_and_fence_the_real_request_ledger(library, monkeypatch):
    from pixiv_novel_sync.storage.ai.core import AIWorkAdmissionDenied
    from test_work_analysis_storage import insert_synthetic_child

    enable(library)
    library.service.tick()
    run = created_runs(library.db)[0]
    now = datetime.now(timezone.utc)
    lease = library.db.claim_work_analysis(run["run_id"], now=now, slice_seconds=600)
    unit_id = library.db.conn.execute("SELECT unit_id FROM work_analysis_units WHERE run_id=? LIMIT 1", (run["run_id"],)).fetchone()[0]
    binding = library.db.begin_work_analysis_unit(lease, unit_id)
    child_id = "synthetic-already-prepared-before-library-stop"
    with library.db.transaction() as conn:
        owner = insert_synthetic_child(conn, {"agent_id": library.env.agent_id, "provider_id": library.env.provider_id}, child_id, now)
        library.db._attach_work_analysis_child(conn, binding, child_id)
    original = library.env.coordinator.request_control
    attempts = []

    def lose_every_progress_cas(run_id, action, expected_version):
        attempts.append(expected_version)
        with library.db.transaction() as conn:
            conn.execute("UPDATE work_analysis_runs SET version=version+1 WHERE run_id=?", (run_id,))
        return original(run_id, action, expected_version)

    monkeypatch.setattr(library.env.coordinator, "request_control", lose_every_progress_cas)
    with pytest.raises(WorkAnalysisError, match="control_unavailable") as rejected:
        library.service.policy({"enabled": False, "expected_version": library.service.state()["policy"]["version"]})
    assert rejected.value.status == 503 and len(attempts) == 3
    assert library.service.state()["policy"]["enabled"] is False
    # The parent can still be draining with its original lease, but even a
    # previously attached child cannot obtain another actual send reservation.
    assert library.db.get_work_analysis(run["run_id"])["status"] == "running"
    with pytest.raises(AIWorkAdmissionDenied, match="work_stopping"):
        library.db.claim_ai_job_network_request(child_id, owner, 0, work_binding=binding, planned_tokens=100)
    assert library.db.get_work_analysis(run["run_id"])["request_count"] == 0
    assert library.db.conn.execute("SELECT network_request_count FROM ai_jobs WHERE job_id=?", (child_id,)).fetchone()[0] == 0
    assert library.db.conn.execute("SELECT COUNT(*) FROM work_analysis_requests WHERE child_job_id=? AND entry_kind='request'", (child_id,)).fetchone()[0] == 0


def test_automatic_new_scope_freezes_first_enable_boundary(library):
    assert library.service.preview({**library.configuration, "scope": "new", "purpose": "policy"})["candidate_count"] == 0
    assert library.service.preview({**library.configuration, "scope": "new", "purpose": "manual"})["candidate_count"] == 1
    policy = enable(library, scope="new")
    assert policy["new_baseline_revision"] == library.db.conn.execute("SELECT revision FROM library_analysis_clock").fetchone()[0]
    library.service.tick()
    assert not created_runs(library.db)
    add_novel(library.db, 7)
    policy_preview = library.service.preview({**library.configuration, "scope": "new", "purpose": "policy"})
    assert [row["source_id"] for row in policy_preview["samples"]] == [7]
    with library.db.transaction() as conn:
        conn.execute("UPDATE library_analysis_policy SET next_run_at='2000-01-01 00:00:00'")
    library.service.tick()
    assert [run["source_id"] for run in created_runs(library.db)] == [7]
    after = library.service.state()["policy"]
    library.service.policy({"enabled": False, "expected_version": after["version"]})
    enabled = enable(library, scope="new")
    assert enabled["new_baseline_revision"] == policy["new_baseline_revision"]


def test_batch_pause_resume_reuses_parent_and_prior_allocation(library):
    batch = library.service.create(confirmed(library))
    library.service.tick()
    before = library.service.batch(batch["batch_id"])["batch"]
    paused = library.service.control(batch["batch_id"], "pause", before["version"])
    run = created_runs(library.db)[0]
    assert run["status"] == "paused"
    resumed = library.service.control(batch["batch_id"], "resume", paused["version"])
    assert resumed["allocated"] == before["allocated"]
    assert len(created_runs(library.db)) == 1
    assert created_runs(library.db)[0]["status"] == "queued"


def test_late_resume_acknowledgment_cannot_clear_a_new_batch_stop(library):
    batch = library.service.create(confirmed(library))
    library.service.tick()
    run = created_runs(library.db)[0]
    current = library.service.batch(batch["batch_id"])["batch"]
    paused = library.service.control(batch["batch_id"], "pause", current["version"])
    resuming = library.db.control_library_analysis_batch(batch["batch_id"], "resume", paused["version"])
    child = library.db.get_work_analysis(run["run_id"])
    library.env.coordinator.request_control(run["run_id"], "resume", child["version"])
    target_id = library.service.batch(batch["batch_id"])["items"][0]["target_id"]
    old_resume_snapshot = library.db.get_library_analysis_hold(target_id)
    assert old_resume_snapshot["held"] and old_resume_snapshot["batch_status"] == "scanning"
    library.service.control(batch["batch_id"], "pause", resuming["version"])
    with pytest.raises(WorkAnalysisError, match="control_unavailable"):
        library.service._synchronize_hold(old_resume_snapshot)
    stopped = library.db.get_library_analysis_hold(target_id)
    assert stopped["held"] and stopped["batch_status"] == "paused"


def test_new_owner_requires_explicit_recovery_and_secondary_owner_does_not_interrupt(library):
    batch = library.service.create(confirmed(library))
    library.service.tick()
    secondary = LibraryAnalysisService(library.factory, lambda: library.env.ai, library.env.coordinator)
    assert secondary.startup_recover() == 0
    assert library.service.batch(batch["batch_id"])["batch"]["status"] == "scanning"
    coordinator = RecordingCoordinator(library.factory)
    restarted = LibraryAnalysisService(library.factory, lambda: library.env.ai, coordinator)
    assert restarted.startup_recover() == 1
    restarted.tick()
    assert restarted.batch(batch["batch_id"])["batch"]["reason"] == "process_interrupted"
    assert len(created_runs(library.db)) == 1
    assert created_runs(library.db)[0]["status"] == "paused"


def test_completed_manual_report_is_reused_without_a_second_provider_call(tmp_path, monkeypatch):
    from pixiv_novel_sync.jobs.manager import JobManager
    from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobType
    from pixiv_novel_sync.jobs.runner import JobRunner
    from pixiv_novel_sync.work_analysis.executor import WorkAnalysisExecutor
    from work_analysis_runtime_helpers import make_runtime_environment

    env = make_runtime_environment(tmp_path, monkeypatch)
    try:
        manager = JobManager()
        job = manager.submit(JobSpec(JobSource.WEB, ["work_analysis"], JobType.WORK_ANALYSIS, {"run_id": env.run_id}))
        executor = WorkAnalysisExecutor(env.db_factory, lambda: env.service)
        JobRunner(manager, lambda task, context: executor.run(env.run_id, context)).run(job.job_id)
        assert env.db.get_work_analysis_report(env.run_id)["coverage"]["complete"]
        library_service = LibraryAnalysisService(env.db_factory, lambda: env.service, RecordingCoordinator(env.db_factory))
        configuration = {
            "scope": "all", "agent_id": env.agent_id, "output_tokens": 128,
            "work_budget": {"request_limit": 64, "token_limit": 1000000, "active_seconds_limit": 600},
            "batch_budget": {"request_limit": 64, "token_limit": 1000000, "active_seconds_limit": 600},
            "work_limit": 1, "interval_minutes": 60,
            "confirmations": {"external_send_confirmed": True, "budget_confirmed": True,
                              "current_archive_only": True, "order_confirmed": True},
        }
        preview = library_service.preview(configuration)
        batch = library_service.create({**configuration, "preview_fingerprint": preview["preview_fingerprint"], "idempotency_key": "1" * 32})
        library_service.tick()
        result = library_service.batch(batch["batch_id"])
        assert result["items"][0]["status"] == "reused"
        assert result["items"][0]["run_id"] == env.run_id
        assert result["batch"]["work_count"] == 0 and not any(result["batch"]["allocated"].values())
        assert len(env.provider.sent_units) == 1
    finally:
        env.close()
