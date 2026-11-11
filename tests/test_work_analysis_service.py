from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from pixiv_novel_sync.ai.crypto import AISecretManager
from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.models import NovelRecord, NovelTextRecord
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError
from work_analysis_api_helpers import (
    CIPHER, PROMPT, TEXT, confirmed_payload, facade_fixture,
)


@pytest.fixture
def work(tmp_path):
    fixture = facade_fixture(tmp_path / "work.db")
    try:
        yield fixture
    finally:
        fixture.ai.close()
        fixture.db.close()


def test_preview_is_read_only_secret_free_and_uses_the_saved_prompt(work, monkeypatch):
    from pixiv_novel_sync.work_analysis import configuration as module

    def forbidden(*args, **kwargs):
        raise AssertionError("preview attempted external, credential, or execution work")

    for cls, name in ((AISecretManager, "decrypt"), (AISecretManager, "encrypt"),
                      (AIWritingService, "_get_provider"),
                      (AIWritingService, "invoke_text")):
        monkeypatch.setattr(cls, name, forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    monkeypatch.setattr("requests.sessions.Session.send", forbidden)
    monkeypatch.setattr(Database, "upgrade_ai_provider_ciphertext", forbidden)
    seen, denied, connections = [], [], []
    original_plan = module.plan_invocation
    original_conn = Database.conn.fget

    def observed_plan(invocation, profiles, **kwargs):
        seen.append(invocation)
        assert any(PROMPT in message.content for message in invocation.messages)
        assert any(TEXT in message.content for message in invocation.messages)
        return original_plan(invocation, profiles, **kwargs)

    def guarded_conn(database):
        conn = original_conn(database)
        if conn not in connections:
            connections.append(conn)
            allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                       sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_TRANSACTION,
                       sqlite3.SQLITE_SAVEPOINT, sqlite3.SQLITE_RECURSIVE}

            def authorize(action, first, second, _database, _trigger):
                if action not in allowed:
                    denied.append((action, first, second))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(module, "plan_invocation", observed_plan)
    monkeypatch.setattr(Database, "conn", property(guarded_conn))
    preview = work.service.preview(work.payload)
    assert preview["plan"]["strategy"] == "whole"
    assert preview["can_create"] is True
    assert preview["confirmation_requirements"]["current_archive_only"] is True
    assert seen and connections and not denied
    rendered = json.dumps(preview, ensure_ascii=False)
    for sentinel in (TEXT, PROMPT, CIPHER, "private-provider.fixture.invalid",
                     "owner_token", "provider_config_hash", "configuration_fingerprint"):
        assert sentinel not in rendered
    assert preview["targets"][0]["model_key"] == "org/model "
    assert work.coordinator.wakes == []


@pytest.mark.parametrize("mutation", [
    {"agent_id": True}, {"agent_id": 0}, {"output_tokens": 0},
    {"output_tokens": 1000001}, {"output_tokens": 1.0},
    {"messages": [{"role": "user", "content": "injected"}]},
    {"work_binding": {}}, {"strategy": "sample"},
    {"confirmations": {"external_send_confirmed": True}},
    {"budget": {"request_limit": 16, "token_limit": 0,
                "active_seconds_limit": 600}},
])
def test_preview_rejects_invalid_or_unknown_fields(work, mutation):
    with pytest.raises(WorkAnalysisError):
        work.service.preview({**work.payload, **mutation})
    assert work.coordinator.wakes == []


def test_preview_requires_no_send_consent_but_create_requires_all_applicable_consent(work):
    preview = work.service.preview(work.payload)
    request = {**deepcopy(work.payload), "preview_fingerprint": preview["preview_fingerprint"],
               "idempotency_key": "2" * 32}
    with pytest.raises(WorkAnalysisError, match="confirmation_required"):
        work.service.create(request)
    assert work.db.conn.execute("SELECT count(*) FROM work_analysis_runs").fetchone()[0] == 0


def test_source_change_after_preview_never_creates_or_wakes(work):
    request = confirmed_payload(work.service, work.payload)
    changed = TEXT + " NEW VERSION"
    work.db.upsert_novel_text(NovelTextRecord(
        101, changed, None, hashlib.sha256(changed.encode()).hexdigest(),
    ))
    with pytest.raises(WorkAnalysisError, match="stale_preview"):
        work.service.create(request)
    assert work.coordinator.wakes == []


@pytest.mark.parametrize("change", ["agent", "prompt", "budget", "output"])
def test_changed_preview_identity_cannot_start(work, change):
    request = confirmed_payload(work.service, work.payload)
    if change == "agent":
        work.db.update_ai_agent(work.agent_id, {"temperature": 0.5})
    elif change == "prompt":
        work.db.update_ai_agent(work.agent_id, {"system_prompt": "NEW SAVED PROMPT"})
    elif change == "budget":
        request["budget"]["request_limit"] += 1
    else:
        request["output_tokens"] += 1
    with pytest.raises(WorkAnalysisError, match="stale_preview"):
        work.service.create(request)
    assert work.coordinator.wakes == []


def test_unknown_create_outcome_reuses_committed_id_before_source_preflight(work):
    request = confirmed_payload(work.service, work.payload)
    created = work.service.create(request)
    work.db.upsert_novel_text(NovelTextRecord(
        101, "", None, hashlib.sha256(b"").hexdigest(),
    ))
    replay = work.service.create(deepcopy(request))
    assert replay["run_id"] == created["run_id"]
    assert len(work.coordinator.wakes) == 1
    assert work.db.conn.execute("SELECT count(*) FROM work_analysis_runs").fetchone()[0] == 1
    request["budget"]["request_limit"] += 1
    with pytest.raises(WorkAnalysisError, match="idempotency_conflict"):
        work.service.create(request)


def test_read_facades_do_not_schedule_and_controls_use_the_coordinator(work):
    run = work.service.create(confirmed_payload(work.service, work.payload))
    work.coordinator.wakes.clear()
    assert work.service.detail(run["run_id"])["run_id"] == run["run_id"]
    assert work.service.list_runs(0, 20)["total"] == 1
    assert work.service.units(run["run_id"], 0, 20)["total"] >= 1
    assert work.coordinator.wakes == []
    paused = work.service.control(run["run_id"], "pause", run["version"])
    assert paused["status"] == "paused"
    assert work.coordinator.controls == [(run["run_id"], "pause", run["version"])]


def test_confirmed_create_survives_admission_wake_failure(work, monkeypatch):
    def unavailable(reason):
        raise RuntimeError("BODY_OR_CREDENTIAL_MUST_NOT_LEAK")

    monkeypatch.setattr(work.coordinator, "wake", unavailable)
    result = work.service.create(confirmed_payload(work.service, work.payload))
    assert result["status"] == "queued"
    assert result["run_id"]
    assert work.db.get_work_analysis(result["run_id"]) is not None


def test_preview_discloses_small_reducer_provider_excluded_from_every_original_unit(work, monkeypatch):
    from pixiv_novel_sync.work_analysis.configuration import snapshot_work_configuration
    from pixiv_novel_sync.work_analysis.evidence import validate_unit_output
    from pixiv_novel_sync.work_analysis.manifest import capture_manifest
    from pixiv_novel_sync.work_analysis.planning import materialize_unit, plan_reduction_level

    def forbidden(*args, **kwargs):
        raise AssertionError("pure target-disclosure test attempted a Provider request")

    monkeypatch.setattr(AIWritingService, "invoke_text", forbidden)
    monkeypatch.setattr(AISecretManager, "decrypt", forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    monkeypatch.setattr("requests.sessions.Session.send", forbidden)
    small_provider = work.db.create_ai_provider({
        "name": "Small reducer service", "provider_type": "openai_compatible",
        "base_url": "https://small-reducer.fixture.invalid/v1", "api_key_encrypted": CIPHER,
        "context_window": 8192, "enabled": True,
    })
    large_model = work.db.create_ai_provider_model({
        "provider_id": work.provider_id, "model_key": "large/source ",
        "manual_context_window": 32768, "enabled": True,
    })
    small_model = work.db.create_ai_provider_model({
        "provider_id": small_provider, "model_key": "small/reducer ",
        "manual_context_window": 8192, "enabled": True,
    })
    pool = work.db.create_ai_model_pool({"name": "Synthetic source and reducer pool", "pool_kind": "custom"})
    version = work.db.replace_ai_model_pool_members(pool, [
        {"provider_model_id": large_model, "enabled": True},
        {"provider_model_id": small_model, "enabled": True},
    ], expected_version=1)
    work.db.update_ai_model_pool(pool, {"enabled": True}, expected_version=version)
    work.db.update_ai_agent(work.agent_id, {"binding_type": "pool", "provider_id": None,
        "model": None, "model_pool_id": pool, "context_window": 1000000})
    work.db.upsert_subscribed_series(501, "Synthetic series", "", 1, None, total_novels=6)
    for novel_id in range(201, 207):
        body = "Synthetic chapter " + str(novel_id) + "\n" + "\u4e2d" * 20000
        work.db.upsert_novel(NovelRecord(novel_id=novel_id, user_id=1, series_id=501,
            title="Chapter " + str(novel_id), caption=None, visible=True, restrict="public", x_restrict=0,
            text_length=len(body), total_bookmarks=0, total_views=0, cover_url=None, tags_json="[]",
            create_date="2026-01-01T00:00:00Z", raw_json="{}", meta_hash="metadata-" + str(novel_id)))
        work.db.upsert_novel_text(NovelTextRecord(novel_id, body, None, hashlib.sha256(body.encode()).hexdigest()))
    payload = deepcopy(work.payload)
    payload.update(selection={"source_kind": "series", "source_id": 501, "publication_state": "unknown"},
                   output_tokens=128, budget={"request_limit": 24, "token_limit": 1000000,
                                              "active_seconds_limit": 600})
    displayed = work.service.preview(payload)
    assert displayed["plan"]["strategy"] == "segmented"
    with work.db.read_transaction() as conn:
        manifest, texts = capture_manifest(conn, payload["selection"])
        configuration, prompt, preflight = snapshot_work_configuration(conn, work.agent_id, 128)
    originals = displayed["plan"]["original_units"]
    artifacts = {}
    for unit in originals:
        invocation, blocks = materialize_unit(manifest, unit, texts, {}, agent_prompt=prompt, output_tokens=128)
        assert {row["provider_id"] for row in preflight(invocation)["eligible"]} == {work.provider_id}
        raw = json.dumps({"unit_id": unit["unit_id"], "summary": "Synthetic validated result",
            "no_findings_reason": "No content conclusion is asserted in this transport-free fixture.",
            "claims": [], "counterexamples": [], "uncertainties": ["Synthetic evidence boundary only."]})
        artifacts[unit["unit_id"]] = validate_unit_output(raw, unit, blocks, {})
    reduced = plan_reduction_level("a" * 32, 1, originals, artifacts, preflight,
                                   agent_prompt=prompt, output_tokens=128)
    assert len(reduced) == 1 and reduced[0]["is_final"] is True
    invocation, _blocks = materialize_unit(manifest, reduced[0], {}, artifacts,
                                          agent_prompt=prompt, output_tokens=128)
    assert {row["provider_id"] for row in preflight(invocation)["eligible"]} == {work.provider_id, small_provider}
    assert {(row["provider_id"], row["model_key"]) for row in displayed["targets"]} == {
        (work.provider_id, "large/source "), (small_provider, "small/reducer "),
    }
    rendered = json.dumps(displayed, ensure_ascii=False)
    for private in ("work_targets", "work_can_segment", "configuration_fingerprint", CIPHER,
                    "small-reducer.fixture.invalid", "\u4e2d" * 100):
        assert private not in rendered
    assert work.db.conn.execute("SELECT count(*) FROM ai_jobs").fetchone()[0] == 0
    assert work.db.conn.execute("SELECT count(*) FROM work_analysis_runs").fetchone()[0] == 0


def test_preview_fails_closed_when_configuration_target_inventory_is_absent(work, monkeypatch):
    from pixiv_novel_sync.work_analysis import service as module

    original = module.snapshot_work_configuration

    def missing_inventory(*args):
        configuration, prompt, captured = original(*args)

        def preflight(payload):
            result = captured(payload)
            result.pop("work_targets", None)
            return result

        return configuration, prompt, preflight

    monkeypatch.setattr(module, "snapshot_work_configuration", missing_inventory)
    with pytest.raises(WorkAnalysisError, match="configuration_unavailable"):
        work.service.preview(work.payload)
    assert work.coordinator.wakes == []
