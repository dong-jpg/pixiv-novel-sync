import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.ai.models import AIStreamChunk
from pixiv_novel_sync.jobs.manager import JobManager
from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobStatus, JobType
from pixiv_novel_sync.jobs.runner import JobRunner
from pixiv_novel_sync.preference_learning.profiles import PreferenceLearningService, validate_summary
from pixiv_novel_sync.preference_learning.contracts import PreferenceCallBinding
from pixiv_novel_sync.recommendations import RecommendationService
from pixiv_novel_sync.storage.preference_learning import capture_sources, migrate_preference_learning
from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError
from pixiv_novel_sync.work_analysis.executor import WorkAnalysisExecutor
from pixiv_novel_sync.work_analysis.planning import materialize_unit
from work_analysis_runtime_helpers import make_runtime_environment


@pytest.fixture
def env(tmp_path, monkeypatch):
    body = "Unique synthetic evidence. PRIVATE_WHOLE_NOVEL_SENTINEL_7721 " + "ordinary background " * 100
    value = make_runtime_environment(tmp_path, monkeypatch, texts=[body])
    unit = value.plan["original_units"][0]
    _, blocks = materialize_unit(value.manifest, unit, {1: body}, {},
                                 agent_prompt="Use only supplied synthetic evidence.", output_tokens=128)
    output = {"unit_id": unit["unit_id"], "summary": "Synthetic work report", "no_findings_reason": None,
              "claims": [{"dimension": "theme", "certainty": "direct", "label": "Synthetic theme",
                          "statement": "Synthetic supported feature.",
                          "quotes": [{"block_id": next(iter(blocks)), "quote": "Unique synthetic evidence."}]}],
              "counterexamples": [], "uncertainties": ["This is synthetic."]}
    original = value.provider.stream_generate

    def work_stream(*args, **kwargs):
        for chunk in original(*args, **kwargs):
            if chunk.type == "delta":
                chunk.text = json.dumps(output)
            yield chunk

    monkeypatch.setattr(value.provider, "stream_generate", work_stream)
    manager = JobManager()
    job = manager.submit(JobSpec(JobSource.WEB, ["work_analysis"], JobType.WORK_ANALYSIS, {"run_id": value.run_id}))
    executor = WorkAnalysisExecutor(value.db_factory, lambda: value.service)
    JobRunner(manager, lambda task, context: executor.run(value.run_id, context)).run(job.job_id)
    assert job.status == JobStatus.SUCCEEDED
    value.learning = PreferenceLearningService(value.db_factory, lambda: value.service)
    value.summary_calls = []
    value.before_summary = None
    value.summary_mutation = None
    value.before_summary_guard = None
    value.extra_summary_request = False

    def summary_stream(messages, model, temperature, top_p, max_tokens, *, request_guard=None,
                       is_cancelled=None, on_usage=None, deadline=None):
        if value.before_summary_guard:
            value.before_summary_guard()
        ordinal = request_guard()
        data = json.loads(messages[-1]["content"])
        value.summary_calls.append(data)
        if value.before_summary:
            value.before_summary()
        if value.extra_summary_request:
            request_guard()
        source = data["sources"][0]
        result = {"version": 1, "summary": "PRIVATE_PROFILE_SENTINEL_8182 explicit feedback summary",
                  "preferences": [{"label": "Synthetic theme", "dimension": "theme", "polarity": "positive",
                                   "certainty": "tentative", "source_ids": [source["source_key"]]}],
                  "counterexamples": [], "uncertainties": ["An overall reaction is not a causal explanation."]}
        if value.summary_mutation:
            value.summary_mutation(result)
        yield AIStreamChunk(type="delta", text=json.dumps(result))
        if on_usage:
            on_usage(ordinal, {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
                               "complete": True, "missing_reason": None})
        yield AIStreamChunk(type="done", data={"finish_reason": "stop"})

    monkeypatch.setattr(value.provider, "stream_generate", summary_stream)
    try:
        yield value
    finally:
        value.close()


def request_for(env):
    return {"agent_id": env.agent_id, "output_tokens": 128,
            "budget": {"request_limit": 2, "token_limit": 100000, "active_seconds_limit": 60},
            "confirmations": {"external_send_confirmed": True, "budget_confirmed": True}}


def create_summary(env, *, key="summary-key"):
    env.learning.feedback("series", 10, {"reaction": "liked", "expected_version": 0})
    payload = request_for(env)
    preview = env.learning.preview(payload)
    assert preview["can_create"]
    return env.learning.create({**payload, "preview_fingerprint": preview["preview_fingerprint"], "idempotency_key": key})


def execute(env, run_id, *, claim=lambda: True):
    return env.learning.execute(run_id, {"stop_requested": lambda: False, "claim_finalization": claim})


def test_feedback_is_explicit_versioned_and_source_checked(env):
    assert env.learning.state()["eligible_works"] == 0
    env.db.upsert_reading_progress(1, 100, "finished")
    assert env.learning.feedback_state("novel", 1)["reaction"] == "clear"
    assert env.learning.state()["eligible_works"] == 0
    for kind, source_id in (("novel", 999), ("unknown", 1), ("novel", True)):
        with pytest.raises(WorkAnalysisError):
            env.learning.feedback(kind, source_id, {"reaction": "liked", "expected_version": 0})
    feedback = env.learning.feedback("series", 10, {"reaction": "liked", "expected_version": 0})
    assert feedback["version"] == 1
    with pytest.raises(WorkAnalysisError, match="version_conflict"):
        env.learning.feedback("series", 10, {"reaction": "disliked", "expected_version": 0})
    cleared = env.learning.feedback("series", 10, {"reaction": "clear", "expected_version": 1, "note": "remove me"})
    assert cleared["note"] == "" and cleared["version"] == 2
    assert env.learning.state()["eligible_works"] == 0


@pytest.mark.parametrize("reaction", [[], {}, True, "interested", "saved", "finished"])
def test_invalid_feedback_does_not_become_a_preference(env, reaction):
    with pytest.raises(WorkAnalysisError):
        env.learning.feedback("series", 10, {"reaction": reaction, "expected_version": 0})
    assert env.learning.state()["feedback_counts"] == {"liked": 0, "disliked": 0, "neutral": 0}


def test_preview_is_read_only_serializable_and_create_is_idempotent(env):
    env.learning.feedback("series", 10, {"reaction": "liked", "expected_version": 0})
    payload = request_for(env)
    before = env.db.conn.total_changes
    preview = env.learning.preview(payload)
    assert json.loads(json.dumps(preview, ensure_ascii=False))["can_create"]
    assert env.db.conn.total_changes == before and not env.summary_calls
    assert "PRIVATE_WHOLE_NOVEL_SENTINEL_7721" not in json.dumps(preview)
    created = {**payload, "preview_fingerprint": preview["preview_fingerprint"], "idempotency_key": "stable-key"}
    first = env.learning.create(created)
    assert env.learning.create(created)["run_id"] == first["run_id"]
    assert env.db.conn.execute("SELECT COUNT(*) FROM preference_summary_runs").fetchone()[0] == 1
    changed = {**created, "confirmations": {"external_send_confirmed": False, "budget_confirmed": True}}
    with pytest.raises(WorkAnalysisError, match="idempotency_conflict"):
        env.learning.create(changed)


def test_feedback_change_rejects_old_preview_before_any_paid_call(env):
    env.learning.feedback("series", 10, {"reaction": "liked", "expected_version": 0})
    payload = request_for(env)
    preview = env.learning.preview(payload)
    env.learning.feedback("series", 10, {"reaction": "disliked", "expected_version": 1})
    with pytest.raises(WorkAnalysisError, match="stale_preview"):
        env.learning.create({**payload, "preview_fingerprint": preview["preview_fingerprint"], "idempotency_key": "stale"})
    assert not env.summary_calls


def test_real_router_summary_has_atomic_ledger_evidence_and_private_retention(env):
    run = create_summary(env)
    result = execute(env, run["run_id"])
    detail = env.learning.detail(run["run_id"])
    assert detail["status"] == "succeeded" and detail["usage"]["network_request_count"] == 1
    assert detail["usage"]["planned_tokens"] > 0 and detail["usage"]["reported_input_tokens"] == 100
    assert detail["result"]["preferences"][0]["evidence_refs"][0]["report_run_id"] == env.run_id
    assert "PRIVATE_PROFILE_SENTINEL_8182" not in json.dumps(result)
    generic = json.dumps([dict(row) for row in env.db.conn.execute("SELECT * FROM ai_jobs")])
    assert "PRIVATE_PROFILE_SENTINEL_8182" not in generic
    assert "PRIVATE_WHOLE_NOVEL_SENTINEL_7721" not in json.dumps(env.summary_calls)
    assert "Unique synthetic evidence." in json.dumps(env.summary_calls)
    active = env.learning.activate(run["run_id"], detail["version"])
    assert active["active"] and env.learning.state()["active_run_id"] == run["run_id"]


@pytest.mark.parametrize("mutation", [
    lambda value: value["preferences"][0].update(source_ids=["novel:999"]),
    lambda value: value["preferences"][0].update(label="Invented preference"),
    lambda value: value["preferences"][0].update(polarity="negative"),
    lambda value: value["preferences"][0].update(source_ids=[["series:10"]]),
    lambda value: value.update(identity="invented"),
])
def test_unfounded_model_preferences_fail_without_retry_or_profile(env, mutation):
    run = create_summary(env)
    env.summary_mutation = mutation
    with pytest.raises(RuntimeError, match="validation_failed"):
        execute(env, run["run_id"])
    detail = env.learning.detail(run["run_id"])
    assert detail["status"] == "failed" and detail["result"] is None
    assert detail["usage"]["network_request_count"] == len(env.summary_calls) == 1


def test_feedback_reversal_and_report_delete_stop_recommendation_influence(env):
    run = create_summary(env)
    execute(env, run["run_id"])
    detail = env.learning.detail(run["run_id"])
    env.learning.activate(run["run_id"], detail["version"])
    assert env.db.get_preference_learning_signals()["weights"]["Synthetic theme"] == 2
    env.learning.feedback("series", 10, {"reaction": "disliked", "expected_version": 1})
    assert env.db.get_preference_learning_signals()["weights"]["Synthetic theme"] == -1
    assert env.learning.detail(run["run_id"])["status"] == "invalidated"
    work = env.db.get_work_analysis(env.run_id)
    env.db.request_work_analysis_stop(env.run_id, "delete", work["version"])
    assert env.db.get_preference_learning_signals()["weights"] == {}
    assert env.learning.detail(run["run_id"])["result"] is None


@pytest.mark.parametrize("action", ["cancel", "delete", "feedback"])
def test_inflight_control_prevents_late_publication(env, action):
    run = create_summary(env)

    def change():
        detail = env.learning.detail(run["run_id"])
        if action == "feedback":
            env.learning.feedback("series", 10, {"reaction": "clear", "expected_version": 1})
        else:
            getattr(env.learning, action)(run["run_id"], detail["version"])

    env.before_summary = change
    with pytest.raises((RuntimeError, InterruptedError)):
        execute(env, run["run_id"])
    detail = env.learning.detail(run["run_id"])
    assert detail["result"] is None and not detail["active"]
    assert detail["status"] in {"cancelled", "deleted", "invalidated"}
    assert detail["usage"]["network_request_count"] == 1


def test_finalization_denial_and_restart_never_replay_paid_generation(env):
    run = create_summary(env)
    with pytest.raises(InterruptedError):
        execute(env, run["run_id"], claim=lambda: False)
    assert env.learning.detail(run["run_id"])["status"] == "cancelled"
    payload = request_for(env)
    preview = env.learning.preview(payload)
    queued = env.learning.create({**payload, "preview_fingerprint": preview["preview_fingerprint"], "idempotency_key": "restart"})
    assert env.learning.startup_recover()["recovered"] == 1
    assert env.learning.detail(queued["run_id"])["error_code"] == "process_interrupted"
    assert len(env.summary_calls) == 1


def test_missing_binding_cannot_bypass_parent_budget(env):
    run = create_summary(env)
    execute(env, run["run_id"])
    child = env.db.conn.execute("SELECT child_job_id FROM preference_summary_runs WHERE run_id=?", (run["run_id"],)).fetchone()[0]
    with pytest.raises(WorkAnalysisError, match="owner_lost"):
        with env.db.transaction() as conn:
            env.db._reserve_preference_learning_request(conn, None, child, 2, 1, now=None)
    with env.db.transaction() as conn:
        env.db._reserve_preference_learning_request(conn, None, "unrelated-child", 1, None, now=None)


def test_recommendation_queries_and_score_follow_explicit_evidence(env):
    env.learning.feedback("series", 10, {"reaction": "liked", "expected_version": 0})
    recommender = RecommendationService(env.db, SimpleNamespace(sync=SimpleNamespace(delay_seconds_between_pages=0)))
    profile = {"id": 1, "profile": {}}
    assert recommender.build_search_plan(profile)["queries"][0]["query"] == "Synthetic theme"
    novel = SimpleNamespace(title="Synthetic theme", caption="", total_bookmarks=0, text_length=0, user=None)
    liked_score, matched = recommender._score(novel, [], profile, 0)
    assert matched["reading_feedback"][0]["sources"][0]["report_run_id"] == env.run_id
    env.learning.feedback("series", 10, {"reaction": "disliked", "expected_version": 1})
    disliked_score, _ = recommender._score(novel, [], profile, 0)
    assert liked_score > 0 > disliked_score
    assert not recommender.build_search_plan(profile)["queries"]


def test_activate_supplies_first_default_without_archival_statistics_or_ai_cleaning(env, monkeypatch):
    assert env.db.get_default_preference_profile() is None
    run = create_summary(env)
    execute(env, run["run_id"])
    env.learning.activate(run["run_id"], env.learning.detail(run["run_id"])["version"])
    profile = env.db.get_default_preference_profile()
    assert profile["source_scope"] == {"explicit_feedback": True}
    recommender = RecommendationService(env.db, SimpleNamespace(sync=SimpleNamespace(delay_seconds_between_pages=0)))
    assert recommender.build_search_plan(profile)["queries"][0]["query"] == "Synthetic theme"
    recommender.api = SimpleNamespace(search_novel=lambda **kwargs: SimpleNamespace(novels=[]))
    monkeypatch.setattr(recommender, "_page_delay", lambda emit=None: None)
    events = []
    recommendation = recommender.run(progress_callback=lambda name, data: events.append((name, data)))
    assert recommendation["stats"]["searched"] == 1
    assert "Synthetic theme" not in json.dumps(events)
    assert len(env.summary_calls) == 1


def test_request_budget_blocks_the_second_physical_attempt(env):
    env.learning.feedback("series", 10, {"reaction": "liked", "expected_version": 0})
    payload = request_for(env)
    payload["budget"]["request_limit"] = 1
    preview = env.learning.preview(payload)
    run = env.learning.create({**payload, "preview_fingerprint": preview["preview_fingerprint"], "idempotency_key": "one-request"})
    env.extra_summary_request = True
    with pytest.raises(RuntimeError, match="budget_exhausted"):
        execute(env, run["run_id"])
    detail = env.learning.detail(run["run_id"])
    assert detail["usage"]["network_request_count"] == 1 and detail["result"] is None
    child_count = env.db.conn.execute("SELECT network_request_count FROM ai_jobs WHERE job_id=(SELECT child_job_id "
                                     "FROM preference_summary_runs WHERE run_id=?)", (run["run_id"],)).fetchone()[0]
    assert child_count == 1


def test_changed_configuration_is_rechecked_at_the_actual_send(env):
    run = create_summary(env)

    def change():
        with env.db.transaction() as conn:
            conn.execute("UPDATE ai_agents SET system_prompt='changed config' WHERE id=?", (env.agent_id,))

    env.before_summary_guard = change
    with pytest.raises(RuntimeError, match="configuration_changed"):
        execute(env, run["run_id"])
    assert not env.summary_calls
    assert env.learning.detail(run["run_id"])["usage"]["network_request_count"] == 0


def test_deadline_revokes_actual_sending_and_preserves_zero_request_budget(env):
    run = create_summary(env)

    def expire():
        with env.db.transaction() as conn:
            conn.execute("UPDATE preference_summary_runs SET deadline_at='2000-01-01 00:00:00' WHERE run_id=?", (run["run_id"],))

    env.before_summary_guard = expire
    with pytest.raises(RuntimeError, match="active_time_exhausted"):
        execute(env, run["run_id"])
    assert not env.summary_calls
    detail = env.learning.detail(run["run_id"])
    assert detail["usage"]["network_request_count"] == detail["usage"]["planned_tokens"] == 0


def test_reading_feedback_source_delete_is_atomic_and_purges_private_summary(env):
    run = create_summary(env)
    execute(env, run["run_id"])
    with pytest.raises(RuntimeError, match="rollback"):
        with env.db.transaction() as conn:
            conn.execute("DELETE FROM series WHERE series_id=10")
            raise RuntimeError("rollback")
    assert env.learning.detail(run["run_id"])["result"] is not None
    with env.db.transaction() as conn:
        conn.execute("DELETE FROM series WHERE series_id=10")
    detail = env.learning.detail(run["run_id"])
    assert detail["result"] is None and detail["sources"] == [] and detail["stale"]


def test_last_budget_slot_is_serialized_with_the_child_ledger(env):
    run = create_summary(env)
    outcomes = []

    def race():
        row = env.db.conn.execute("SELECT * FROM preference_summary_runs WHERE run_id=?", (run["run_id"],)).fetchone()
        child = env.db.conn.execute("SELECT * FROM ai_jobs WHERE job_id=?", (row["child_job_id"],)).fetchone()
        binding = PreferenceCallBinding(run["run_id"], row["owner_token"], row["configuration_fingerprint"])
        barrier = Barrier(2)

        def reserve():
            db = env.db_factory()
            try:
                barrier.wait(timeout=3)
                try:
                    return db.claim_ai_job_network_request(child["job_id"], child["owner_token"], 0,
                                                          preference_binding=binding, planned_tokens=100)
                except Exception as exc:
                    return getattr(exc, "code", type(exc).__name__)
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes.extend(list(pool.map(lambda _: reserve(), range(2))))

    env.before_summary = race
    execute(env, run["run_id"])
    assert outcomes.count(2) == 1 and len(outcomes) == 2
    usage = env.learning.detail(run["run_id"])["usage"]
    assert usage["network_request_count"] == 2
    assert usage["reported_input_tokens"] is None and usage["reported_known_input_tokens"] == 100
    assert usage["unknown_requests"] == 1
    rows = env.db.conn.execute("SELECT ordinal FROM preference_summary_requests WHERE run_id=? ORDER BY ordinal", (run["run_id"],)).fetchall()
    assert [row[0] for row in rows] == [0, 1, 2]


def test_busy_admission_waits_and_a_confirmed_queued_run_is_only_submitted_once(env):
    run = create_summary(env)
    calls = []
    env.learning.submit_job = lambda run_id: calls.append(run_id)
    env.learning.wake()
    assert env.learning.detail(run["run_id"])["shared_job_id"] is None
    env.learning.submit_job = lambda run_id: {"job_id": "shared-synthetic-id"}
    assert env.learning.wake() == "shared-synthetic-id"
    assert env.learning.wake() is None
    assert env.learning.detail(run["run_id"])["shared_job_id"] == "shared-synthetic-id"
    assert calls == [run["run_id"]] and not env.summary_calls


def test_series_and_chapter_feedback_do_not_double_count_the_same_work(env):
    env.learning.feedback("novel", 1, {"reaction": "liked", "expected_version": 0})
    env.learning.feedback("series", 10, {"reaction": "liked", "expected_version": 0})
    assert env.learning.state()["eligible_works"] == 1
    assert env.db.get_preference_learning_signals()["weights"]["Synthetic theme"] == 1


@pytest.mark.parametrize("new_series_id", [None, 11])
def test_moving_a_chapter_invalidates_the_old_series_learning_basis(env, new_series_id):
    run = create_summary(env)
    execute(env, run["run_id"])
    env.learning.activate(run["run_id"], env.learning.detail(run["run_id"])["version"])
    with env.db.transaction() as conn:
        conn.execute("INSERT INTO series(series_id,title,user_id,total_novels) VALUES(11,'Other series',1,1)")
        conn.execute("UPDATE novels SET series_id=? WHERE novel_id=1", (new_series_id,))
    report = env.db.get_work_analysis_report(env.run_id)
    assert report["archive_outdated"] and not report["report_stale"]
    summary = env.learning.detail(run["run_id"])
    assert summary["stale"] and not summary["active"]
    assert summary["result"] is None and summary["sources"] == []
    assert env.learning.state()["missing_reports"] == 1
    assert env.db.get_preference_learning_signals()["weights"] == {}


def test_source_capture_batches_queries_without_reading_novel_bodies(env):
    env.learning.feedback("novel", 1, {"reaction": "liked", "expected_version": 0})
    env.learning.feedback("series", 10, {"reaction": "liked", "expected_version": 0})
    with env.db.transaction() as conn:
        for source_id in range(20, 40):
            conn.execute("INSERT INTO series(series_id,title,user_id,total_novels) VALUES(?,'Missing report',1,1)",
                         (source_id,))
            env.db.set_reading_feedback("series", source_id, "neutral", 0)
    queries = []
    with env.db.read_transaction() as conn:
        conn.set_trace_callback(queries.append)
        try:
            captured = capture_sources(conn)
        finally:
            conn.set_trace_callback(None)
    selects = [query for query in queries if query.lstrip().upper().startswith("SELECT")]
    assert len(selects) <= 2
    assert all("novel_texts" not in query.lower() for query in selects)
    assert captured["feedback_counts"] == {"liked": 2, "disliked": 0, "neutral": 20}
    assert captured["eligible_works"] == 1 and captured["missing_reports"] == 20
    assert [source["source_key"] for source in captured["sources"]] == ["series:10"]


def test_migration_is_repeatable_and_keeps_callers_transaction():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE novels(novel_id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE series(series_id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE work_analysis_runs(run_id TEXT PRIMARY KEY, deleted_at TEXT, report_stale INTEGER, archive_outdated INTEGER, report_json TEXT)")
        conn.commit()
        conn.execute("BEGIN")
        migrate_preference_learning(conn)
        migrate_preference_learning(conn)
        assert conn.in_transaction
        conn.rollback()
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='preference_summary_runs'").fetchone()
    finally:
        conn.close()


def test_schema_validator_rejects_duplicate_json_and_nonfinite(env):
    env.learning.feedback("series", 10, {"reaction": "liked", "expected_version": 0})
    with env.db.read_transaction() as conn:
        sources = capture_sources(conn)["sources"]
    for raw in ('{"version":1,"version":1}', '{"version":NaN}'):
        with pytest.raises(WorkAnalysisError):
            validate_summary(raw, sources)


@pytest.mark.parametrize("cause,status", [("configuration_changed", "invalidated"),
                                         ("active_time_exhausted", "failed")])
def test_inflight_business_stop_keeps_its_reason_instead_of_user_cancel(env, monkeypatch, cause, status):
    import pixiv_novel_sync.preference_learning.profiles as profile_module

    clock = [100.0]
    monkeypatch.setattr(profile_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    run = create_summary(env)

    def change_after_reservation():
        if cause == "configuration_changed":
            env.db.update_ai_agent(env.agent_id, {"system_prompt": "Changed synthetic instruction"})
        else:
            with env.db.transaction() as conn:
                conn.execute("UPDATE preference_summary_runs SET deadline_at='2000-01-01 00:00:00' WHERE run_id=?",
                             (run["run_id"],))
        clock[0] += 1

    env.before_summary = change_after_reservation
    with pytest.raises(RuntimeError, match=cause):
        execute(env, run["run_id"])
    detail = env.learning.detail(run["run_id"])
    assert detail["status"] == status and detail["error_code"] == cause
    assert detail["result"] is None and detail["usage"]["network_request_count"] == 1
