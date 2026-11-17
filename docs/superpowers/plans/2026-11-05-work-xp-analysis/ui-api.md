# Phase 2 Work Analysis: API And UI Tasks

> Planning only. This appendix defines Tasks 7/8 of the implementation plan. It is not an
> implementation, test result, deployment authorization, or independent final
> plan. Read it with the root plan's shared contracts and validation gates.

**Baseline:** `c8956442ba42ca0114bdbd628c790ca1b5a45688`, branch
`codex/work-xp-analysis-design` in the existing isolated worktree.

**Boundary:** Preserve the existing local preference statistics and the
unsupported main AI-job continue endpoint. Do not add SSE, generation probes,
automatic library processing, a second execution queue, or a CSS hand edit.
The new view inherits the Library OS, pinned Vue runtime, modal, picker,
authentication, CSRF, and shared job infrastructure.

### Task 7: Strict WorkAnalysisService And Authenticated API

**Files:**
- Create: `src/pixiv_novel_sync/work_analysis/service.py`.
- Create: `src/pixiv_novel_sync/work_analysis_web.py`.
- Modify: `src/pixiv_novel_sync/web/request_limits.py:19`.
- Modify: `src/pixiv_novel_sync/webapp.py:999`, immediately after the existing
  `register_ai_routes` call and after Task 6 provisions its coordinator extension.
- Create: `tests/work_analysis_api_helpers.py`.
- Create: `tests/test_work_analysis_service.py`.
- Create: `tests/test_work_analysis_api.py`.

**Interfaces:**
- Consume Task 1/2 `WorkAnalysisError`, `canonical_bytes`, `fingerprint`,
  `validate_selection`, `validate_budget`, `capture_manifest`,
  `public_preview_identity`, `preview_fingerprint`, `plan_original_units`, and the configuration
  identity adapter. The planner receives the saved Agent prompt explicitly;
  `normalize_text_invocation` does not prepend it.
- The captured configuration preflight accepts a full payload and supplies
  private `work_can_segment` plus `work_targets` (the complete, safe, frozen
  target inventory). Neither private property is copied into API responses.
- Consume Task 3 storage methods from the coordination contract, including
  `lookup_work_analysis_idempotency(idempotency_key, request_identity)`.
  Its identity has exactly `selection`, `agent_id`, `output_tokens`, `budget`,
  `confirmations`, and `preview_fingerprint`. Lookup runs before recapturing
  sources/configuration; atomic create repeats the lookup before its recheck.
- Consume Task 6 `app.extensions['work_analysis_coordinator']` and
  `request_control(run_id, action, expected_version)` / `wake(reason)`.
- Produce `WorkAnalysisService(db_factory, ai_factory, coordinator)` with the
  exact facade methods in the coordination contract and
  `register_work_analysis_routes(app, service) -> None`.
- The existing current-AI-service proxy is reused, not recreated on every
  preview or unit. A fresh Database instance is closed after every facade
  operation. Pure planning occurs after its short read transaction closes.

- [ ] **Step 1: Add isolated, synthetic fixtures and the failing facade tests.**

Create `tests/work_analysis_api_helpers.py`:

```python
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace

from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.models import NovelRecord, NovelTextRecord, UserRecord
from pixiv_novel_sync.storage_db import Database


TEXT = "BEGIN fixture \U0001f600 unique evidence. PRIVATE_BODY_END"
PROMPT = "SAVED_AGENT_PROMPT_ONLY_IN_MEMORY"
CIPHER = "v2$fixture-ciphertext-never-decrypted"
BASE = "/api/dashboard/work-analyses"


class RecordingCoordinator:
    def __init__(self, db_factory):
        self.db_factory = db_factory
        self.wakes = []
        self.controls = []

    def wake(self, reason):
        self.wakes.append(reason)

    def request_control(self, run_id, action, expected_version):
        self.controls.append((run_id, action, expected_version))
        db = self.db_factory()
        try:
            if action == "resume":
                return db.resume_work_analysis(run_id, expected_version)
            return db.request_work_analysis_stop(run_id, action, expected_version)
        finally:
            db.close()


def seed_database(path: Path):
    db = Database(path)
    db.init_schema()
    db.upsert_user(UserRecord(1, "Fixture author", None, "{}"))
    db.upsert_novel(NovelRecord(
        novel_id=101, user_id=1, series_id=None, title="Fixture book",
        caption=None, visible=True, restrict="public", x_restrict=0,
        text_length=len(TEXT), total_bookmarks=0, total_views=0,
        cover_url=None, tags_json="[]", create_date="2026-01-01T00:00:00Z",
        raw_json="{}", meta_hash="fixture-metadata",
    ))
    db.upsert_novel_text(NovelTextRecord(
        101, TEXT, None, hashlib.sha256(TEXT.encode("utf-8")).hexdigest(),
    ))
    provider_id = db.create_ai_provider({
        "name": "Fixture provider", "provider_type": "openai_compatible",
        "base_url": "https://private-provider.fixture.invalid/v1",
        "api_key_encrypted": CIPHER, "default_model": "org/model ",
        "context_window": 1000000, "enabled": True,
    })
    agent_id = db.create_ai_agent({
        "name": "Fixture Agent", "task_type": "general",
        "binding_type": "fixed", "provider_id": provider_id,
        "model": "org/model ", "system_prompt": PROMPT,
        "temperature": 0, "top_p": 0, "max_tokens": 1000,
        "context_window": 1000000, "enabled": True,
    })
    return db, agent_id, provider_id


def request_payload(agent_id):
    return {
        "selection": {"source_kind": "novel", "source_id": 101,
                      "publication_state": "unknown"},
        "agent_id": agent_id, "output_tokens": 1000,
        "budget": {"request_limit": 16, "token_limit": 500000,
                   "active_seconds_limit": 600},
        "confirmations": {"order_confirmed": False,
                          "current_archive_only": False,
                          "external_send_confirmed": False,
                          "budget_confirmed": False},
    }


def confirmed_payload(service, payload, key="1" * 32):
    result = deepcopy(payload)
    result["preview_fingerprint"] = service.preview(payload)["preview_fingerprint"]
    result["confirmations"] = {name: True for name in payload["confirmations"]}
    result["idempotency_key"] = key
    return result


def facade_fixture(path):
    from pixiv_novel_sync.work_analysis.service import WorkAnalysisService

    db, agent_id, provider_id = seed_database(path)
    ai = AIWritingService(path)
    factory = lambda: Database(path)
    coordinator = RecordingCoordinator(factory)
    service = WorkAnalysisService(factory, lambda: ai, coordinator)
    return SimpleNamespace(db=db, ai=ai, service=service, coordinator=coordinator,
                           agent_id=agent_id, provider_id=provider_id,
                           payload=request_payload(agent_id), path=path)
```

Create `tests/test_work_analysis_service.py`:

```python
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
```

- [ ] **Step 2: Run the focused RED command.**

Run `python -m pytest tests/test_work_analysis_service.py -q`.
Expected before implementation: collection fails because
`pixiv_novel_sync.work_analysis.service` does not exist, not because fixtures
touch a live database, missing credentials, or an external endpoint.

- [ ] **Step 3: Implement the facade, captured preflight, and safe projections.**

Create `src/pixiv_novel_sync/work_analysis/service.py`:

```python
from __future__ import annotations

from collections.abc import Mapping
import logging
import re

from ..ai.invocation import InvocationValidationError
from .configuration import snapshot_work_configuration
from .contracts import WorkAnalysisError, canonical_bytes, validate_budget
from .manifest import capture_manifest, preview_fingerprint, validate_selection
from .planning import plan_original_units


logger = logging.getLogger(__name__)
CONFIRMATIONS = frozenset({"order_confirmed", "current_archive_only",
                           "external_send_confirmed", "budget_confirmed"})
PREVIEW_FIELDS = frozenset({"selection", "agent_id", "output_tokens", "budget", "confirmations"})
TARGET_FIELDS = frozenset({"provider_id", "provider_name", "provider_model_id", "model_key",
                           "display_name", "pool_id", "pool_name", "pool_position", "fallback_depth"})


def exact_object(value, keys):
    if not isinstance(value, Mapping) or set(value) != set(keys):
        raise WorkAnalysisError("invalid_fields")
    return dict(value)


def strict_integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise WorkAnalysisError("invalid_integer")
    return value


def checked_id(value, length=32):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{" + str(length) + r"}", value) is None:
        raise WorkAnalysisError("invalid_id")
    return value


def normalized_request(payload, *, create=False):
    extra = {"preview_fingerprint", "idempotency_key"} if create else set()
    data = exact_object(payload, PREVIEW_FIELDS | extra)
    result = {
        "selection": validate_selection(data["selection"]),
        "agent_id": strict_integer(data["agent_id"], 1, 2**63 - 1),
        "output_tokens": strict_integer(data["output_tokens"], 1, 1000000),
        "budget": validate_budget(data["budget"]),
        "confirmations": exact_object(data["confirmations"], CONFIRMATIONS),
    }
    if any(type(value) is not bool for value in result["confirmations"].values()):
        raise WorkAnalysisError("invalid_confirmation")
    if create:
        result["preview_fingerprint"] = checked_id(data["preview_fingerprint"], 64)
        result["idempotency_key"] = checked_id(data["idempotency_key"])
    return result


class WorkAnalysisService:
    def __init__(self, db_factory, ai_factory, coordinator):
        self.db_factory = db_factory
        self.ai_factory = ai_factory
        self.coordinator = coordinator

    def _read(self, method, *args, **kwargs):
        db = self.db_factory()
        try:
            return getattr(db, method)(*args, **kwargs)
        finally:
            db.close()

    def _prepare(self, request):
        db = self.db_factory()
        try:
            with db.read_transaction() as conn:
                manifest, texts = capture_manifest(conn, request["selection"])
                configuration, agent_prompt, captured_preflight = snapshot_work_configuration(
                    conn, request["agent_id"], request["output_tokens"],
                )
                agent_name = db.get_ai_agent(request["agent_id"])["name"]
        except InvocationValidationError:
            raise WorkAnalysisError("configuration_unavailable") from None
        finally:
            db.close()

        # Only bounded in-memory text is retained, never a DB transaction or Provider.
        decisions = {}

        def preflight(payload):
            messages = payload.get("messages") if isinstance(payload, Mapping) else None
            if not isinstance(messages, list) or not 1 <= len(messages) <= 256:
                raise WorkAnalysisError("input_too_large", 413)
            bounded = {**payload, "allowed_provider_ids": configuration["allowed_provider_ids"]}
            if len(canonical_bytes(bounded)) > 16 * 1024 * 1024:
                raise WorkAnalysisError("input_too_large", 413)
            try:
                result = captured_preflight(bounded)
            except InvocationValidationError:
                raise WorkAnalysisError("configuration_unavailable") from None
            rows = result.get("work_targets")
            if (not isinstance(rows, list) or not 1 <= len(rows) <= 64
                    or any(not isinstance(row, Mapping) or set(row) != TARGET_FIELDS for row in rows)):
                raise WorkAnalysisError("configuration_unavailable", 409)
            for row in rows:
                identity = (row["provider_id"], row["model_key"])
                decisions[identity] = {key: row[key] for key in TARGET_FIELDS}
            return result

        try:
            plan = plan_original_units(
                manifest, texts, preflight, agent_prompt=agent_prompt,
                output_tokens=request["output_tokens"],
            )
        finally:
            texts.clear()

        requirements = {
            "order_confirmed": manifest["source_kind"] == "series"
                               or "ordered_novel_ids" in request["selection"],
            "current_archive_only": manifest["requires_partial_confirmation"],
            "external_send_confirmed": True,
            "budget_confirmed": True,
        }
        enough = (request["budget"]["request_limit"] >= plan["minimum_requests"]
                  and request["budget"]["token_limit"] >=
                  plan["minimum_requests"] * request["output_tokens"])
        warnings = list(plan["warnings"])
        if not enough:
            warnings.append("budget_below_known_minimum")
        if configuration["legacy_provider_ids"]:
            warnings.append("legacy_repreview_required")
        preview = {
            "selection": request["selection"], "manifest": manifest, "plan": plan,
            "agent_id": request["agent_id"], "output_tokens": request["output_tokens"],
            "agent_name": agent_name, "targets": list(decisions.values()),
            "budget": request["budget"], "preview_fingerprint": preview_fingerprint(
                request["selection"], manifest, plan, configuration, request["budget"],
            ),
            "can_create": enough, "confirmation_requirements": requirements,
            "warnings": list(dict.fromkeys(warnings)),
        }
        return preview, configuration

    def preview(self, payload):
        preview, _configuration = self._prepare(normalized_request(payload))
        return preview

    def create(self, payload):
        request = normalized_request(payload, create=True)
        key = request["idempotency_key"]
        identity = {name: value for name, value in request.items() if name != "idempotency_key"}
        existing = self._read("lookup_work_analysis_idempotency", key, identity)
        if existing is not None:
            return existing
        preview, configuration = self._prepare(request)
        if preview["preview_fingerprint"] != request["preview_fingerprint"]:
            raise WorkAnalysisError("stale_preview", 409)
        if not preview["can_create"]:
            raise WorkAnalysisError("budget_below_known_minimum", 409)
        if any(required and not request["confirmations"][name]
               for name, required in preview["confirmation_requirements"].items()):
            raise WorkAnalysisError("confirmation_required")
        result = self._read(
            "create_work_analysis", request["selection"], preview["manifest"],
            preview["plan"], configuration, request["budget"], request["confirmations"],
            key, request["preview_fingerprint"],
        )
        try:
            self.coordinator.wake("created")
        except Exception as exc:
            # The parent intent has committed; a missed wake is not a failed create.
            logger.warning("work analysis admission wake deferred (%s)", type(exc).__name__)
        return result

    def list_runs(self, offset, limit):
        return self._read("list_work_analyses", strict_integer(offset, 0, 1000000),
                          strict_integer(limit, 1, 100))

    def detail(self, run_id):
        result = self._read("get_work_analysis", checked_id(run_id))
        if result is None:
            raise WorkAnalysisError("not_found", 404)
        return result

    def control(self, run_id, action, expected_version):
        checked_id(run_id)
        strict_integer(expected_version, 1, 2**63 - 1)
        if action not in {"pause", "cancel", "resume"}:
            raise WorkAnalysisError("invalid_action")
        return self.coordinator.request_control(run_id, action, expected_version)

    def amend_budget(self, run_id, budget, expected_version):
        return self._read("update_work_analysis_budget", checked_id(run_id),
                          validate_budget(budget), strict_integer(expected_version, 1, 2**63 - 1))

    def units(self, run_id, offset, limit):
        return self._read("list_work_analysis_units", checked_id(run_id),
                          strict_integer(offset, 0, 1000000), strict_integer(limit, 1, 100))

    def report(self, run_id):
        return self._read("get_work_analysis_report", checked_id(run_id))

    def evidence(self, run_id, evidence_id):
        return self._read("get_work_analysis_evidence", checked_id(run_id), checked_id(evidence_id, 64))

    def delete(self, run_id, expected_version):
        checked_id(run_id)
        strict_integer(expected_version, 1, 2**63 - 1)
        return self.coordinator.request_control(run_id, "delete", expected_version)
```

`targets` contains the private configuration closure's complete `work_targets`
inventory, not just candidates eligible for current original-unit payloads.
A smaller reduction can make a previously capacity-excluded candidate eligible;
that Provider/model is disclosed before consent too. Missing inventory fails
closed. Both private `work_targets` and `work_can_segment` stay inside the
configuration/planning boundary, without changing global v2 public preflight.
`targets` contains exact opaque model keys and safe labels only. No prompt,
endpoint, proxy, ciphertext, configuration hash, raw body, output, or owner is
returned by preview. The fingerprint deliberately excludes the four approval
checkboxes, permitting the user to approve the displayed immutable preview;
source/order/publication/Agent/output/budget/configuration changes still change
the fingerprint. Known-minimum requests/token reserves are lower bounds, not
prices or a promise that every possible retry fits.

- [ ] **Step 4: Run the facade GREEN command and review its boundary.**

Run `python -m pytest tests/test_work_analysis_service.py tests/test_ai_preflight_api.py tests/test_ai_invocation_planner.py -q`.
Expected: all selected tests pass with synthetic data and zero Provider/DNS
calls. Review the SQL authorizer's observed connections, the saved-prompt
assertion, and replay-before-recapture test, not just the final test count.

- [ ] **Step 5: Add failing authenticated-route and strict-body tests.**

Create `tests/test_work_analysis_api.py`:

```python
from copy import deepcopy
import json

import pytest

from pixiv_novel_sync.webapp import create_app
from work_analysis_api_helpers import BASE, confirmed_payload, facade_fixture


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "fixture-login-token")
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "fixture-flask-secret-not-a-credential")
    env = tmp_path / ".env"
    env.write_text("PIXIV_REFRESH_TOKEN=fixture\nDASHBOARD_TOKEN=fixture-login-token\n",
                   encoding="utf-8")
    fixture = facade_fixture(tmp_path / "fixture.db")
    app = create_app(env_path=str(env), start_scheduler=False)
    app.config.update(TESTING=True, RAW_HTTP=True)
    app.extensions["pixiv_novel_sync.work_analysis_service"] = fixture.service
    client = app.test_client()
    fixture.app, fixture.client = app, client
    try:
        yield fixture
    finally:
        app.extensions["pixiv_novel_sync.ai_service"].close()
        fixture.ai.close()
        fixture.db.close()


def login(api):
    assert api.client.post("/api/auth/login", data={"token": "fixture-login-token"}).status_code == 302
    token = api.client.get("/api/csrf-token").get_json()["csrf_token"]
    return {"X-CSRF-Token": token}


@pytest.mark.parametrize("path", [BASE, BASE + "/", BASE + "/" + "1" * 32,
                                  BASE + "/" + "1" * 32 + "/report",
                                  BASE + "/" + "1" * 32 + "/evidence/" + "2" * 64])
def test_work_api_uses_existing_authentication_and_no_store_on_failure(api, path):
    response = api.client.get(path)
    assert response.status_code == 401
    assert "no-store" in response.headers["Cache-Control"]
    assert "Location" not in response.headers


def test_work_preview_requires_csrf_even_without_external_send(api):
    login(api)
    response = api.client.post(BASE + "/preview", json=api.payload)
    assert response.status_code == 403
    assert "no-store" in response.headers["Cache-Control"]
    assert api.coordinator.wakes == []


@pytest.mark.parametrize("suffix", ["", "/"])
def test_base_routes_never_redirect_and_write_returns_the_committed_parent(api, suffix):
    headers = login(api)
    preview = api.client.post(BASE + "/preview", json=api.payload, headers=headers)
    assert preview.status_code == 200
    assert "no-store" in preview.headers["Cache-Control"]
    payload = confirmed_payload(api.service, api.payload)
    created = api.client.post(BASE + suffix, json=payload, headers=headers)
    assert created.status_code == 200
    assert "Location" not in created.headers
    assert created.get_json()["data"]["status"] == "queued"
    listed = api.client.get(BASE + suffix)
    assert listed.status_code == 200
    assert listed.get_json()["data"]["total"] == 1
    assert "Location" not in listed.headers


@pytest.mark.parametrize("raw,status", [
    ("[]", 400), ("null", 400), ('{"agent_id":1,"agent_id":2}', 400),
    ('{"budget":{"token_limit":NaN}}', 400), ("{" + " " * (256 * 1024) + "}", 413),
], ids=["array", "null", "duplicate-key", "nonfinite", "too-large"])
def test_work_body_parser_is_bounded_and_rejects_duplicate_or_nonfinite_json(api, raw, status):
    headers = login(api)
    response = api.client.post(BASE + "/preview", data=raw,
                               content_type="application/json", headers=headers)
    assert response.status_code == status
    assert "no-store" in response.headers["Cache-Control"]
    assert response.get_json()["ok"] is False


@pytest.mark.parametrize("query", ["offset=-1", "limit=0", "limit=101", "offset=1.0",
                                   "limit=true", "offset=0&offset=1", "include_private=true"])
def test_work_list_rejects_invalid_or_ambiguous_query_fields(api, query):
    login(api)
    response = api.client.get(BASE + "?" + query)
    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_controls_require_explicit_version_and_budget_consent(api):
    headers = login(api)
    run = api.service.create(confirmed_payload(api.service, api.payload))
    url = BASE + "/" + run["run_id"]
    for body in ({}, {"expected_version": True}, {"expected_version": run["version"], "force": True}):
        assert api.client.post(url + "/pause", json=body, headers=headers).status_code == 400
    assert api.client.post(url + "/budget", json={
        "expected_version": run["version"], "budget": run["budget"],
        "budget_confirmed": False,
    }, headers=headers).status_code == 400
    paused = api.client.post(url + "/pause", json={"expected_version": run["version"]}, headers=headers)
    assert paused.status_code == 200
    assert paused.get_json()["data"]["status"] == "paused"
    conflict = api.client.post(url + "/resume", json={"expected_version": run["version"]}, headers=headers)
    assert conflict.status_code == 409


def test_incomplete_report_and_unrelated_evidence_are_not_exposed(api):
    login(api)
    run = api.service.create(confirmed_payload(api.service, api.payload))
    url = BASE + "/" + run["run_id"]
    assert api.client.get(url + "/report").status_code == 409
    assert api.client.get(url + "/evidence/" + "f" * 64).status_code in {404, 409}
    detail = api.client.get(url).get_json()["data"]
    assert "report" not in detail and "configuration" not in detail and "owner_token" not in detail
    units = api.client.get(url + "/units").get_json()["data"]
    assert all("artifact" not in item for item in units["items"])


def test_work_errors_never_echo_exception_messages_or_offer_sse(api, monkeypatch):
    login(api)

    def explode(offset, limit):
        raise RuntimeError("PRIVATE_BODY PRIVATE_KEY PRIVATE_REPORT")

    monkeypatch.setattr(api.service, "list_runs", explode)
    response = api.client.get(BASE)
    assert response.status_code == 500
    assert response.get_json() == {"ok": False, "error": "work_analysis_unavailable",
                                   "code": "work_analysis_unavailable"}
    assert "PRIVATE" not in response.get_data(as_text=True)
    assert api.client.get(BASE + "/events").status_code == 400
    assert api.client.get(BASE + "/" + "1" * 32 + "/events").status_code == 404


def test_delete_is_idempotent_and_never_resumes_a_tombstone(api):
    headers = login(api)
    request = confirmed_payload(api.service, api.payload)
    run = api.service.create(request)
    url = BASE + "/" + run["run_id"]
    body = {"expected_version": run["version"]}
    first = api.client.delete(url, json=body, headers=headers)
    assert first.status_code == 200
    again = api.client.delete(url, json=body, headers=headers)
    assert again.status_code == 200
    assert again.get_json()["data"]["deleted"] is True
    assert api.client.post(BASE, json=deepcopy(request), headers=headers).status_code == 410
    assert api.client.get(url + "/report").status_code == 410
    assert api.client.get(url + "/evidence/" + "f" * 64).status_code == 410
```

- [ ] **Step 6: Run the authenticated-route RED command.**

Run `python -m pytest tests/test_work_analysis_api.py -q`.
Expected before route registration: failures are 404/absent no-store headers,
not a successful synthetic run. Keep `RAW_HTTP=True`; the global fixture must
not silently supply the CSRF header to the negative tests.

- [ ] **Step 7: Add opt-in strict JSON parsing, routes, and dynamic integration.**

In `src/pixiv_novel_sync/web/request_limits.py`, add these complete helpers
before `read_bounded_json_object`, change its signature, and replace only its
`json.loads(raw)` call. Existing callers retain their behavior:

```python
def _unique_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("nonfinite_json_number")


def read_bounded_json_object(request: Request, *, limit_bytes: int, strict: bool = False) -> dict[str, Any]:
    if not request.is_json:
        raise JSONBodyError("invalid_json", "请求体必须是 JSON 对象")
    length = request.content_length
    if length is not None and length > limit_bytes:
        raise JSONBodyError("payload_too_large", "请求体超过允许大小", 413)
    try:
        raw = request.stream.read(limit_bytes + 1)
    except RequestEntityTooLarge:
        raise JSONBodyError("payload_too_large", "请求体超过允许大小", 413) from None
    except BadRequest:
        raise JSONBodyError("invalid_json", "请求体不完整或无法读取") from None
    if len(raw) > limit_bytes:
        raise JSONBodyError("payload_too_large", "请求体超过允许大小", 413)
    try:
        payload = (json.loads(raw, object_pairs_hook=_unique_json_pairs,
                              parse_constant=_reject_json_constant)
                   if strict else json.loads(raw))
    except (ValueError, UnicodeError, RecursionError):
        raise JSONBodyError("invalid_json", "请求体必须是有效的 JSON 对象") from None
    if not isinstance(payload, dict):
        raise JSONBodyError("invalid_json", "请求体必须是 JSON 对象")
    return payload
```

Create `src/pixiv_novel_sync/work_analysis_web.py`:

```python
from __future__ import annotations

from functools import wraps
import logging
import re

from flask import jsonify, request

from .web.request_limits import JSONBodyError, read_bounded_json_object
from .work_analysis.contracts import WorkAnalysisError
from .work_analysis.service import exact_object, strict_integer


BASE = "/api/dashboard/work-analyses"
BODY_LIMIT = 256 * 1024
logger = logging.getLogger(__name__)


def register_work_analysis_routes(app, service):
    app.extensions["pixiv_novel_sync.work_analysis_service"] = service

    def current():
        return app.extensions["pixiv_novel_sync.work_analysis_service"]

    @app.after_request
    def work_analysis_no_store(response):
        if request.path == BASE or request.path.startswith(BASE + "/"):
            response.headers["Cache-Control"] = "private, no-store, no-cache, max-age=0, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    def endpoint(function):
        @wraps(function)
        def guarded(*args, **kwargs):
            try:
                return jsonify({"ok": True, "data": function(*args, **kwargs)})
            except WorkAnalysisError as exc:
                return jsonify({"ok": False, "error": exc.code, "code": exc.code}), exc.status
            except JSONBodyError as exc:
                return jsonify({"ok": False, "error": exc.code, "code": exc.code}), exc.status_code
            except Exception as exc:
                logger.warning("work analysis API unavailable (%s)", type(exc).__name__)
                return jsonify({"ok": False, "error": "work_analysis_unavailable",
                                "code": "work_analysis_unavailable"}), 500
        return guarded

    def query(allowed=()):
        if set(request.args) - set(allowed) or any(len(request.args.getlist(key)) != 1 for key in request.args):
            raise WorkAnalysisError("invalid_query")

    def page():
        query({"offset", "limit"})
        values = []
        for name, default, lower, upper in (("offset", "0", 0, 1000000), ("limit", "20", 1, 100)):
            raw = request.args.get(name, default)
            if not re.fullmatch(r"0|[1-9][0-9]{0,6}", raw):
                raise WorkAnalysisError("invalid_query")
            values.append(strict_integer(int(raw), lower, upper))
        return values

    def body():
        query()
        return read_bounded_json_object(request, limit_bytes=BODY_LIMIT, strict=True)

    def version():
        return strict_integer(exact_object(body(), {"expected_version"})["expected_version"], 1, 2**63 - 1)

    @app.post(BASE + "/preview")
    @endpoint
    def work_analysis_preview():
        return current().preview(body())

    @app.route(BASE, methods=["GET", "POST"])
    @app.route(BASE + "/", methods=["GET", "POST"])
    @endpoint
    def work_analysis_collection():
        if request.method == "POST":
            return current().create(body())
        offset, limit = page()
        return current().list_runs(offset, limit)

    @app.route(BASE + "/<run_id>", methods=["GET", "DELETE"])
    @endpoint
    def work_analysis_detail(run_id):
        if request.method == "DELETE":
            return current().delete(run_id, version())
        query()
        return current().detail(run_id)

    @app.post(BASE + "/<run_id>/pause")
    @app.post(BASE + "/<run_id>/cancel")
    @app.post(BASE + "/<run_id>/resume")
    @endpoint
    def work_analysis_control(run_id):
        action = request.path.rsplit("/", 1)[1]
        return current().control(run_id, action, version())

    @app.post(BASE + "/<run_id>/budget")
    @endpoint
    def work_analysis_budget(run_id):
        data = exact_object(body(), {"expected_version", "budget", "budget_confirmed"})
        if data["budget_confirmed"] is not True:
            raise WorkAnalysisError("confirmation_required")
        return current().amend_budget(run_id, data["budget"], data["expected_version"])

    @app.get(BASE + "/<run_id>/units")
    @endpoint
    def work_analysis_units(run_id):
        offset, limit = page()
        return current().units(run_id, offset, limit)

    @app.get(BASE + "/<run_id>/report")
    @endpoint
    def work_analysis_report(run_id):
        query()
        return current().report(run_id)

    @app.get(BASE + "/<run_id>/evidence/<evidence_id>")
    @endpoint
    def work_analysis_evidence(run_id, evidence_id):
        query()
        return current().evidence(run_id, evidence_id)
```

At the existing `webapp.py` route-registration anchor, insert this block after
`register_ai_routes(app, current_settings_for_routes)`; do not move the existing
authentication/CSRF hooks or exempt the new prefix:

```python
    from .work_analysis.service import WorkAnalysisService
    from .work_analysis_web import register_work_analysis_routes

    register_work_analysis_routes(app, WorkAnalysisService(
        work_coordinator.db_factory,
        lambda: app.extensions["pixiv_novel_sync.ai_service"],
        app.extensions["work_analysis_coordinator"],
    ))
```

Schema preparation belongs to the existing startup/settings lifecycle from
Tasks 3/6, not a preview call. Reuse the coordinator's owner-bound database
factory: if the configured path changes, return a safe unavailable error even
when the new database is ready. Do not migrate owners or silently create/migrate
a database during a supposedly read-only preview; restart binds the new owner.

- [ ] **Step 8: Run GREEN and the focused API review gate.**

Run `python -m pytest tests/test_work_analysis_service.py tests/test_work_analysis_api.py tests/test_ai_preflight_api.py tests/test_preference_csrf.py tests/test_webapp_security.py tests/test_ai_invocation_runtime.py -q`.
Expected: selected tests pass; no new endpoint exists below `/api/rescue/v1`,
the old local statistics still use their original routes, and main continue is
still unsupported. Verify all prefix responses, including auth/CSRF/404/405,
carry no-store; no request handler dispatches paid work as a consequence of GET.

Commit only the Task 7 paths:

```text
git add src/pixiv_novel_sync/work_analysis/service.py src/pixiv_novel_sync/work_analysis_web.py src/pixiv_novel_sync/web/request_limits.py src/pixiv_novel_sync/webapp.py tests/work_analysis_api_helpers.py tests/test_work_analysis_service.py tests/test_work_analysis_api.py
git commit -m "feat: add private work analysis service and API"
```

The Task 7 reviewer rejects leaking private configuration or invocation data,
consent bypasses, parser coercions, stale fingerprint acceptance, replay lookup
after source preflight, failed wake being reported as failed creation, or
pause/delete bypassing the shared finalization/cancel adapter.

### Task 8: Existing-Page Work Analysis UI, Reader Evidence, And Offline Browser

**Files:**
- Create: `src/pixiv_novel_sync/static/js/work_analysis.js`.
- Create: `src/pixiv_novel_sync/templates/dashboard_work_analysis.html`.
- Create: `src/pixiv_novel_sync/templates/dashboard_work_analysis_scripts.html`.
- Modify: `src/pixiv_novel_sync/preference_web.py:32`.
- Modify: `src/pixiv_novel_sync/templates/dashboard_preferences.html:5` and `:172`.
- Modify: `src/pixiv_novel_sync/templates/dashboard_novel_detail.html:383`,
  `:507`, `:521`, and its setup return at `:884`.
- Modify: `src/pixiv_novel_sync/templates/dashboard_series_detail.html:50`.
- Modify: `src/pixiv_novel_sync/storage/novels.py:209` (detail projection only).
- Modify: `src/pixiv_novel_sync/static/js/ai_settings.js:8`.
- Modify: `config/nginx/pixiv-novel-sync.conf:115`.
- Modify: `tests/test_deployment_config.py:65`.
- Create: `tests/test_work_analysis_ui.py`.
- Create: `tests/test_work_analysis_runtime.cjs`.
- Create: `tests/test_work_analysis_runtime.py`.
- Create: `tests/work_analysis_browser_helpers.py`.
- Create: `tests/test_work_analysis_browser.py`.
- Generated only, if the offline Tailwind check requires it:
  `src/pixiv_novel_sync/static/css/tailwind.css` and the content-hash link in
  `src/pixiv_novel_sync/templates/base.html`.

**Interfaces:**
- Consume Task 7's authenticated prefix, strict envelope, hex IDs, no-store
  responses, preview contract, and Task 3's public report/coverage/usage shape.
- A stored artifact always has mandatory `is_final`; nonfinal units allow 32
  claims/2000 summary code points and final roots allow 128 claims/4000 summary
  code points. Every stored unit, including the final root, remains at most
  128 KiB. Only the server-built report envelope may reach 1 MiB. The UI reads
  `artifact.claims`, `artifact.evidence`, `coverage.source_completeness`, and
  `synthesis_complete` separately; it does not promote a unit to finality or
  infer a report from partial output. Task 2/3 enforce these typed bounds.
- Reuse `window.csrfFetch`, `window.initVueApp`, `window.AIPicker`,
  `window.AISettings`, and the existing `app-modal`; no replacement model picker.
- Produce `window.WorkAnalysis.createController(Vue, request, search)` for the
  actual page setup and directly testable method-entry/lifecycle guards.
- Produce `window.WorkAnalysis.useReaderEvidence(Vue, novelRef, novelId)` for a
  narrowly scoped, text-only reader highlight panel. Only `analysis_id` and
  `evidence_id` go in the reader URL; quote/body/report text never does.
- Each read owns an AbortController and generation. Old finally/catch handlers
  cannot clear a newer request's loading/error state. Every write snapshots its
  request, expected version, target, and idempotency key before transport.
- A confirmed write is committed UI state before any list/detail readback.
  Failed readbacks offer GET retry, never repeat POST/DELETE. Unknown create
  outcomes retain the same body/key for explicit retry; unknown controls use
  readback/CAS and never blindly issue a second command.
- Every preview request and accepted replacement clears all four approvals,
  even with an unchanged form or fingerprint. Consent applies only to the
  displayed response, never a later source/configuration/target snapshot.
  A control GET observes state, not ownership of an unacknowledged command:
  only unchanged-version readback permits an explicit identical-CAS retry.
  Newer versions retire that obsolete request without claiming success;
  `sync_yield` never confirms user pause and still permits explicit pause.

- [ ] **Step 1: Write the static integration and reader projection tests.**

Create `tests/test_work_analysis_ui.py`:

```python
from pathlib import Path
import re

from flask import Flask, render_template

from work_analysis_api_helpers import facade_fixture


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src/pixiv_novel_sync/templates"


def render_preferences(work_view):
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.jinja_env.variable_start_string = "{["
    app.jinja_env.variable_end_string = "]}"
    with app.test_request_context("/dashboard/preferences"):
        return render_template("dashboard_preferences.html", work_analysis_view=work_view,
                               task_labels={}, csp_nonce=lambda: "fixture-nonce")


def test_preferences_default_and_work_view_mount_different_existing_page_setups():
    old = render_preferences(False)
    work = render_preferences(True)
    assert 'id="preference-management"' in old
    assert "window.useRecommendations()" in old
    assert "/static/js/work_analysis.js" not in old
    assert 'id="work-analysis"' in work
    assert "window.useRecommendations()" not in work
    assert "recommendations/search-plan" not in work
    assert work.count("window.initVueApp(") == 1
    assert '<script nonce="fixture-nonce" src="/static/js/work_analysis.js"></script>' in work
    assert work.index("/static/js/ai_picker.js") < work.index("/static/js/work_analysis.js")
    assert work.index("/static/js/work_analysis.js") < work.index("WorkAnalysis.createController")


def test_work_partial_never_renders_html_or_adds_an_external_generation_action():
    html = (TEMPLATES / "dashboard_work_analysis.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static/js/work_analysis.js").read_text(encoding="utf-8")
    assert "v-html" not in html
    assert "innerHTML" not in script
    assert "localStorage" not in script and "sessionStorage" not in script
    assert "streamSSE" not in script and "EventSource" not in script
    assert "/continue" not in script and "/probe" not in script
    assert 'data-evidence-id' in html
    assert 'open="' not in html
    assert 'budget_confirmed' in html and 'current_archive_only' in html
    for name in ("dashboard_novel_detail.html", "dashboard_series_detail.html"):
        source = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "/dashboard/preferences?view=work-analysis" in source
    assert "useReaderEvidence" in (TEMPLATES / "dashboard_novel_detail.html").read_text(encoding="utf-8")


def test_reader_detail_includes_current_body_hash_without_mutating_it(tmp_path):
    fixture = facade_fixture(tmp_path / "reader.db")
    try:
        raw = fixture.db.conn.execute("SELECT text_hash FROM novel_texts WHERE novel_id=101").fetchone()[0]
        assert fixture.db.get_novel_detail(101)["text_hash"] == raw
        assert fixture.db.conn.execute("SELECT count(*) FROM work_analysis_runs").fetchone()[0] == 0
    finally:
        fixture.ai.close()
        fixture.db.close()


def test_work_js_and_partial_are_in_existing_wheel_package_data():
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"templates/*.html"' in project and '"static/js/*.js"' in project
    assert (TEMPLATES / "dashboard_work_analysis.html").is_file()
    assert (TEMPLATES / "dashboard_work_analysis_scripts.html").is_file()
    assert (TEMPLATES.parent / "static/js/work_analysis.js").is_file()
```

In `tests/test_deployment_config.py`, rename
`test_ai_javascript_cache_policy_matches_only_five_shipped_assets` to
`test_ai_javascript_cache_policy_matches_only_six_shipped_assets`, and replace
only its `names` set with:

```python
    names = {
        "ai_settings.js", "ai_picker.js", "ai_provider_onboarding.js",
        "ai_preflight.js", "ai_job_diagnostics.js", "work_analysis.js",
    }
```

The existing negative-path loop, exact-name matching, private/no-store header,
HSTS inheritance, and proxy-trust assertions stay in that test file unchanged.

Create `tests/test_work_analysis_runtime.py`:

```python
from pathlib import Path
import shutil
import subprocess

import pytest


def test_work_analysis_runtime():
    root = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the work-analysis VM regression")
    result = subprocess.run(
        [node, "--test", str(root / "tests/test_work_analysis_runtime.cjs")],
        cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

Create `tests/test_work_analysis_runtime.cjs`. The VM calls the shipped
controller, not a copied implementation. Its explicitly driven watch callbacks
are method/lifecycle tests, never Vue scheduling or browser-layout evidence.

```javascript
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const settings = require('../src/pixiv_novel_sync/static/js/ai_settings.js');
const BASE = '/api/dashboard/work-analyses';
const RUN = 'a'.repeat(32), HASH = 'b'.repeat(64), UNIT = 'c'.repeat(64);
const clone = value => JSON.parse(JSON.stringify(value));
const tick = async () => { for (let i = 0; i < 24; i++) await Promise.resolve(); };
const later = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
const budget = {request_limit: 16, token_limit: 100000, active_seconds_limit: 600};
const agent = {kind: 'agent', agent_id: 7, label: 'Fixture Agent', availability: 'ready'};
function run(overrides = {}) {
  return {run_id: RUN, version: 1, status: 'queued', reason: null, title: 'Synthetic book',
    deleted: false, has_report: false, can_resume: false, budget: clone(budget),
    request_count: 2, planned_tokens: 5000, active_seconds_debited: 4,
    unit_counts: {total: 1, succeeded: 0}, coverage: null, usage_summary: null, ...overrides};
}
function preview(body) {
  return {selection: clone(body.selection), agent_id: body.agent_id,
    budget: clone(body.budget), output_tokens: body.output_tokens,
    manifest: {schema_version: 1, source_kind: 'novel', source_id: 101,
      title: 'Synthetic book', publication_state: 'unknown', publication_source: 'unknown',
      order_source: 'publication_date_suggestion', expected_count: null,
      expected_count_observed_at: null, remote_inventory_verified: false,
      requires_partial_confirmation: true, manifest_fingerprint: HASH,
      chapters: [{novel_id: 101, position: 0, title: 'Synthetic chapter', text_hash: HASH,
        chars: 10, utf8_bytes: 10, fetched_at: null, has_text: true}]},
    plan: {schema_version: 1, strategy: 'whole', manifest_fingerprint: HASH,
      chunker_version: 1, prompt_version: 1, report_schema_version: 1,
      minimum_requests: 1, warnings: [], original_units: [{unit_id: UNIT, kind: 'whole',
        is_final: true, level: 0, primary_ranges: [{novel_id: 101, text_hash: HASH, start: 0, end: 10}],
        context_ranges: [], child_unit_ids: [], claim_refs: [], input_fingerprint: HASH}]},
    preview_fingerprint: HASH, targets: [{provider_id: 3, provider_name: 'Synthetic', model_key: 'org/model '}],
    warnings: [], can_create: true, confirmation_requirements: {order_confirmed: false,
      current_archive_only: true, external_send_confirmed: true, budget_confirmed: true}};
}
function fixture() {
  const calls = [], watchers = [], timers = new Map(), listeners = new Map();
  let timerId = 0, nonce = 0, handle;
  const Vue = {
    reactive: value => value, computed: get => ({get value() { return get(); }}),
    nextTick: async () => {}, onUnmounted: () => {},
    watch(read, callback, options = {}) {
      const entry = {read, callback, previous: JSON.stringify(read())};
      watchers.push(entry);
      if (options.immediate) callback(read());
      return () => { const index = watchers.indexOf(entry); if (index >= 0) watchers.splice(index, 1); };
    },
  };
  const root = {
    location: {search: '?source_kind=novel&source_id=101'},
    document: {hidden: false, getElementById: () => null},
    crypto: {getRandomValues: bytes => bytes.fill(++nonce)},
    setTimeout(fn) { const id = ++timerId; timers.set(id, fn); return id; },
    clearTimeout: id => timers.delete(id),
    addEventListener(name, fn) { listeners.set(name, fn); },
    removeEventListener(name) { listeners.delete(name); },
  };
  const context = vm.createContext({window: root, URLSearchParams, AbortController, Uint8Array});
  const filename = path.resolve(__dirname, '../src/pixiv_novel_sync/static/js/work_analysis.js');
  vm.runInContext(fs.readFileSync(filename, 'utf8'), context, {filename});
  const answer = async (url, options = {}) => {
    if (url === '/api/dashboard/ai/agents') return [{id: 7, name: 'Fixture Agent', enabled: true}];
    if (url === BASE + '/preview') return preview(JSON.parse(options.body));
    if (url === BASE && options.method === 'POST') return run();
    if (url === BASE + '/' + RUN) return run();
    if (url === BASE + '?offset=0&limit=20') return {items: [run()], total: 1, offset: 0, limit: 20};
    throw new Error('Unexpected synthetic route: ' + url);
  };
  handle = answer;
  const controller = root.WorkAnalysis.createController(Vue, async (url, options = {}) => {
    calls.push({url, method: options.method || 'GET', body: options.body, signal: options.signal});
    return clone(await handle(url, options));
  });
  controller.wa.agents = [clone(agent)];
  controller.chooseAgent(agent);
  controller.wa.form.budget = clone(budget);
  const trigger = () => {
    for (const entry of watchers.slice()) {
      const value = JSON.stringify(entry.read());
      if (value !== entry.previous) { entry.previous = value; entry.callback(entry.read()); }
    }
  };
  trigger();
  return {controller, api: root.WorkAnalysis, root, calls, timers, trigger, answer,
    handle: next => { handle = next; }, writes: () => calls.filter(row => row.method !== 'GET')};
}
async function ready(f) {
  assert.equal(await f.controller.previewWork(), true);
  for (const name of Object.keys(f.controller.wa.confirmations)) f.controller.wa.confirmations[name] = true;
  assert.equal(f.controller.canStart.value, true);
}

test('agent-kind identity never collapses to an undefined model identity', () => {
  assert.deepEqual(JSON.parse(settings.optionKey(agent)), ['agent', 7]);
  assert.notEqual(settings.optionKey(agent), settings.optionKey({...agent, agent_id: 8}));
  assert.notEqual(settings.optionKey(agent), settings.optionKey({kind: 'model', provider_id: 7, model_key: 'org/model '}));
  assert.equal(JSON.parse(settings.optionKey({kind: 'model', provider_id: 7, model_key: 'org/model '}))[2], 'org/model ');
});
test('consent does not invalidate a preview, but direct stale-method calls cannot create', async () => {
  const f = fixture();
  await f.controller.previewWork();
  assert.equal(await f.controller.createWork(), false);
  await ready(f);
  f.controller.wa.form.budget.token_limit += 1;
  assert.equal(await f.controller.createWork(), false);
  assert.equal(f.calls.filter(row => row.url === BASE && row.method === 'POST').length, 0);
  f.trigger();
  assert.equal(f.controller.wa.preview, null);
  assert.equal(f.controller.wa.confirmations.external_send_confirmed, false);
});
test('same-form preview replacement always requires fresh consent at method entry', async () => {
  for (const changedOnServer of [false, true]) {
    const f = fixture(), held = later();
    await ready(f);
    const replacement = preview(JSON.parse(f.calls.at(-1).body));
    if (changedOnServer) {
      replacement.preview_fingerprint = 'd'.repeat(64);
      replacement.manifest.chapters[0].text_hash = 'e'.repeat(64);
      replacement.targets = [{provider_id: 4, provider_name: 'Changed synthetic service', model_key: 'new/model '}];
    }
    f.handle((url, options) => url === BASE + '/preview' ? held.promise : f.answer(url, options));
    const reading = f.controller.previewWork();
    assert.ok(Object.values(f.controller.wa.confirmations).every(value => value === false));
    assert.equal(await f.controller.createWork(), false);
    // A response cannot inherit even an approval written while its request was pending.
    for (const name of Object.keys(f.controller.wa.confirmations)) f.controller.wa.confirmations[name] = true;
    held.resolve(replacement);
    assert.equal(await reading, true);
    assert.equal(f.controller.wa.preview.preview_fingerprint, replacement.preview_fingerprint);
    assert.ok(Object.values(f.controller.wa.confirmations).every(value => value === false));
    assert.equal(f.controller.canStart.value, false);
    assert.equal(await f.controller.createWork(), false);
    assert.equal(f.calls.filter(row => row.url === BASE && row.method === 'POST').length, 0);
    for (const name of Object.keys(f.controller.wa.confirmations)) f.controller.wa.confirmations[name] = true;
    assert.equal(f.controller.canStart.value, true);
  }
});
test('missing or disabled current Agent stays visible but cannot silently become a new selection', async () => {
  const f = fixture();
  await ready(f);
  f.handle(async url => url === '/api/dashboard/ai/agents' ? [{id: 8, name: 'Other', enabled: true}] : f.answer(url));
  await f.controller.reloadAgents();
  assert.equal(f.controller.currentAgent.value.agent_id, 7);
  assert.equal(f.controller.currentAgent.value.availability, 'unavailable');
  assert.equal(f.controller.chooseAgent({...agent, availability: 'disabled'}), false);
  assert.equal(await f.controller.createWork(), false);
});
test('invalid budget or missing amendment/deletion consent blocks direct method entry', async () => {
  for (const value of ['', '0', '-1', '1.5', 'Infinity', 'NaN', true, 513]) {
    const f = fixture();
    f.controller.wa.form.budget.request_limit = value;
    assert.equal(await f.controller.previewWork(), false);
    assert.equal(f.writes().length, 0);
  }
  const f = fixture();
  f.controller.wa.selectedId = RUN; f.controller.wa.run = run({status: 'paused', can_resume: true});
  f.controller.wa.amend = clone(budget);
  assert.equal(await f.controller.control('budget'), false);
  assert.equal(await f.controller.control('delete'), false);
  f.controller.wa.amendConfirmed = true; f.controller.wa.amend.active_seconds_limit = '0';
  assert.equal(await f.controller.control('budget'), false);
  assert.equal(f.writes().length, 0);
});
test('late preview rejection cannot clear a newer loading owner or replace its error', async () => {
  const f = fixture(), first = later(), second = later();
  let count = 0;
  f.handle((url, options) => url === BASE + '/preview' ? (++count === 1 ? first.promise : second.promise) : f.answer(url, options));
  const old = f.controller.previewWork(), current = f.controller.previewWork();
  assert.equal(f.calls[0].signal.aborted, true);
  first.reject(new Error('OLD_UNTRUSTED_FAILURE'));
  await old;
  assert.equal(f.controller.wa.busy.preview, true);
  assert.equal(f.controller.wa.readErrors.preview, '');
  second.resolve(preview(JSON.parse(f.calls[1].body)));
  await current;
  assert.equal(f.controller.wa.busy.preview, false);
  assert.equal(f.controller.wa.preview.preview_fingerprint, HASH);
});
test('double create and unknown outcome preserve one immutable body and idempotency key', async () => {
  const f = fixture(), held = later();
  await ready(f);
  let sends = 0;
  f.handle((url, options) => {
    if (url === BASE && options.method === 'POST') return ++sends === 1 ? held.promise : run();
    return f.answer(url, options);
  });
  const first = f.controller.createWork();
  assert.equal(await f.controller.createWork(), false);
  assert.equal(f.controller.chooseAgent({...agent, agent_id: 8}), false);
  held.reject(new TypeError('NETWORK_OUTCOME_UNKNOWN'));
  await first;
  assert.equal(f.controller.wa.pending.phase, 'unknown');
  const event = {prevented: false, preventDefault() { this.prevented = true; }};
  f.controller.beforeUnload(event);
  assert.equal(event.prevented, true); assert.equal(event.returnValue, '');
  assert.equal(await f.controller.retryUnknown(), true);
  const writes = f.calls.filter(row => row.url === BASE && row.method === 'POST');
  assert.equal(writes.length, 2);
  assert.equal(writes[0].body, writes[1].body);
  assert.match(JSON.parse(writes[0].body).idempotency_key, /^[0-9a-f]{32}$/);
  assert.equal(f.controller.wa.pending, null);
  assert.equal(f.controller.wa.run.run_id, RUN);
});
test('known write success survives failed GET readback without permitting POST retry', async () => {
  const f = fixture();
  await ready(f);
  f.handle((url, options) => {
    if (url === BASE && options.method === 'POST') return run();
    throw Object.assign(new Error('FAILED_GET'), {status: 503});
  });
  assert.equal(await f.controller.createWork(), true);
  assert.equal(f.controller.wa.pending, null);
  assert.equal(f.controller.wa.run.status, 'queued');
  assert.equal(f.controller.wa.writeError, '');
  assert.match(f.controller.wa.notice, /刷新失败/);
  assert.equal(await f.controller.retryUnknown(), false);
  assert.equal(f.calls.filter(row => row.url === BASE && row.method === 'POST').length, 1);
});
test('unknown control requires readback before an explicit same-version CAS retry', async () => {
  const f = fixture();
  f.controller.wa.selectedId = RUN; f.controller.wa.run = run({status: 'running', version: 4});
  let sends = 0;
  f.handle((url, options) => {
    if (url === BASE + '/' + RUN + '/pause') {
      sends += 1;
      if (sends === 1) throw new TypeError('UNKNOWN_CONTROL');
      return run({status: 'pausing', version: 5, reason: 'user_pause'});
    }
    if (url === BASE + '/' + RUN) return run({status: sends > 1 ? 'pausing' : 'running', version: sends > 1 ? 5 : 4});
    return f.answer(url, options);
  });
  assert.equal(await f.controller.control('pause'), false);
  assert.equal(await f.controller.retryUnknown(), false);
  await f.controller.reconcileWrite();
  assert.equal(f.controller.wa.pending.canRetry, true);
  assert.equal(await f.controller.retryUnknown(), true);
  const writes = f.calls.filter(row => row.url.endsWith('/pause'));
  assert.equal(writes.length, 2); assert.equal(writes[0].body, writes[1].body);
  assert.equal(f.controller.wa.run.status, 'pausing');
});
test('unknown pause cannot turn a scheduler yield into confirmed user intent', async () => {
  for (const status of ['pausing', 'paused']) {
    const f = fixture();
    let current = run({status: 'running', version: 4}), sends = 0;
    f.controller.wa.selectedId = RUN; f.controller.wa.run = current;
    f.handle((url, options) => {
      if (url === BASE + '/' + RUN + '/pause') {
        if (++sends === 1) throw new TypeError('PAUSE_NOT_ACKNOWLEDGED');
        assert.equal(JSON.parse(options.body).expected_version, 5);
        current = run({status, version: 6, reason: 'user_pause', can_resume: status === 'paused'});
        return current;
      }
      if (url === BASE + '/' + RUN) return current;
      return f.answer(url, options);
    });
    assert.equal(await f.controller.control('pause'), false);
    current = run({status, version: 5, reason: 'sync_yield', can_resume: status === 'paused'});
    await f.controller.reconcileWrite();
    assert.equal(f.controller.wa.pending, null);
    assert.doesNotMatch(f.controller.wa.notice, /写入已确认/);
    assert.match(f.controller.wa.writeError, /未确认原写入/);
    assert.equal(f.controller.controlAllowed('pause'), true);
    assert.equal(sends, 1);
    assert.equal(await f.controller.control('pause'), true);
    assert.equal(f.controller.wa.run.reason, 'user_pause');
    const writes = f.calls.filter(row => row.url.endsWith('/pause'));
    assert.deepEqual(writes.map(row => JSON.parse(row.body).expected_version), [4, 5]);
    assert.equal(f.calls.filter(row => row.url.endsWith('/resume')).length, 0);
  }
});
test('a newer matching state is observed but never attributed to an unacknowledged write', async () => {
  for (const [action, status, reason] of [
    ['pause', 'paused', 'user_pause'], ['cancel', 'cancelled', 'user_cancel'],
    ['resume', 'running', null],
  ]) {
    const f = fixture();
    f.controller.wa.selectedId = RUN;
    f.controller.wa.run = run({status: action === 'resume' ? 'paused' : 'running', version: 4, can_resume: action === 'resume'});
    f.handle((url, options) => {
      if (url === BASE + '/' + RUN + '/' + action) throw new TypeError('UNKNOWN_CONTROL');
      if (url === BASE + '/' + RUN) return run({status, reason, version: 7});
      return f.answer(url, options);
    });
    await f.controller.control(action);
    await f.controller.reconcileWrite();
    assert.equal(f.controller.wa.pending, null);
    assert.equal(f.controller.wa.run.version, 7);
    assert.match(f.controller.wa.notice, /原写入结果仍未知/);
    assert.doesNotMatch(f.controller.wa.notice, /写入已确认/);
    assert.equal(await f.controller.retryUnknown(), false);
    assert.equal(f.calls.filter(row => row.method !== 'GET').length, 1);
  }
});
test('older reconciliation cannot replace a newer poll or authorize another CAS write', async () => {
  const f = fixture(), held = later();
  f.controller.wa.selectedId = RUN; f.controller.wa.run = run({status: 'running', version: 4});
  f.handle((url, options) => {
    if (url.endsWith('/pause')) throw new TypeError('UNKNOWN_CONTROL');
    if (url === BASE + '/' + RUN) return held.promise;
    return f.answer(url, options);
  });
  await f.controller.control('pause');
  const reading = f.controller.reconcileWrite();
  f.controller.wa.run = run({status: 'paused', reason: 'user_pause', version: 6});
  held.resolve(run({status: 'paused', reason: 'sync_yield', version: 5}));
  await reading;
  assert.equal(f.controller.wa.run.version, 6);
  assert.equal(f.controller.wa.run.reason, 'user_pause');
  assert.equal(f.controller.wa.pending.phase, 'unknown');
  assert.equal(f.controller.wa.pending.canRetry, false);
  assert.equal(await f.controller.retryUnknown(), false);
  assert.match(f.controller.wa.notice, /较旧版本/);
});
test('reconciliation cannot apply to another selection or a replacement write owner', async () => {
  for (const changed of ['selection', 'operation']) {
    const f = fixture(), held = later();
    f.controller.wa.selectedId = RUN; f.controller.wa.run = run({status: 'running', version: 4});
    f.handle((url, options) => {
      if (url.endsWith('/pause')) throw new TypeError('UNKNOWN_CONTROL');
      if (url === BASE + '/' + RUN) return held.promise;
      return f.answer(url, options);
    });
    await f.controller.control('pause');
    const original = f.controller.wa.pending;
    const reading = f.controller.reconcileWrite();
    if (changed === 'selection') {
      f.controller.wa.selectedId = 'f'.repeat(32);
      f.controller.wa.run = run({run_id: f.controller.wa.selectedId, version: 1});
    } else f.controller.wa.pending = {...original, phase: 'unknown', canRetry: false};
    const currentRun = f.controller.wa.run, currentPending = f.controller.wa.pending;
    held.resolve(run({status: 'paused', reason: 'user_pause', version: 5}));
    await reading;
    assert.equal(f.controller.wa.run, currentRun);
    assert.equal(f.controller.wa.pending, currentPending);
    assert.equal(f.controller.wa.pending.canRetry, false);
    assert.doesNotMatch(f.controller.wa.notice, /写入已确认/);
  }
});
test('a new reconciliation revokes a prior retry authorization while its read is pending', async () => {
  const f = fixture(), held = later();
  f.controller.wa.selectedId = RUN; f.controller.wa.run = run({status: 'running', version: 4});
  let reads = 0;
  f.handle((url, options) => {
    if (url.endsWith('/pause')) throw new TypeError('UNKNOWN_CONTROL');
    if (url === BASE + '/' + RUN) return ++reads === 1 ? run({status: 'running', version: 4}) : held.promise;
    return f.answer(url, options);
  });
  await f.controller.control('pause');
  await f.controller.reconcileWrite();
  assert.equal(f.controller.wa.pending.canRetry, true);
  const reading = f.controller.reconcileWrite();
  assert.equal(f.controller.wa.pending.canRetry, false);
  assert.equal(await f.controller.retryUnknown(), false);
  held.resolve(run({status: 'paused', reason: 'sync_yield', version: 5}));
  await reading;
  assert.equal(f.calls.filter(row => row.url.endsWith('/pause')).length, 1);
});
test('a newer poll revokes old-version retry and an older poll cannot roll back reconciliation', async () => {
  const f = fixture(), held = later();
  f.controller.wa.selectedId = RUN; f.controller.wa.run = run({status: 'running', version: 4});
  let reads = 0;
  f.handle((url, options) => {
    if (url.endsWith('/pause')) throw new TypeError('UNKNOWN_CONTROL');
    if (url === BASE + '/' + RUN) {
      reads += 1;
      return reads === 1 ? run({status: 'running', version: 4}) : reads === 3 ? held.promise
        : run({status: 'paused', reason: 'sync_yield', version: 5});
    }
    return f.answer(url, options);
  });
  await f.controller.control('pause');
  await f.controller.reconcileWrite();
  assert.equal(f.controller.wa.pending.canRetry, true);
  await f.controller.refreshRun();
  assert.equal(f.controller.wa.pending.canRetry, false);
  const polling = f.controller.refreshRun();
  assert.equal(await f.controller.retryUnknown(), false);
  assert.equal(f.calls.filter(row => row.url.endsWith('/pause')).length, 1);
  await f.controller.reconcileWrite();
  held.resolve(run({status: 'running', version: 4}));
  await polling;
  assert.equal(f.controller.wa.run.version, 5);
  assert.equal(f.controller.wa.run.reason, 'sync_yield');
  assert.equal(f.controller.wa.pending, null);
  assert.equal(f.controller.controlAllowed('pause'), true);
  assert.equal(f.calls.filter(row => row.url.endsWith('/pause')).length, 1);
});
test('negative, overlong, and UTF-16-confused evidence never becomes a precise location', () => {
  const f = fixture();
  const evidence = {novel_id: 101, text_hash: HASH, start: 1001, end: 1003, quote: '\u{1f600}Z'};
  const value = {evidence, jump: {...evidence, context_start: 1000, context_text: 'A\u{1f600}ZB'}};
  assert.deepEqual(clone(f.api.partsFromEvidence(value)), {before: 'A', quote: '\u{1f600}Z', after: 'B'});
  assert.throws(() => f.api.partsFromEvidence({...value, jump: {...value.jump, end: 1004}}));
  assert.throws(() => f.api.partsFromEvidence({...value, jump: {...value.jump, context_start: -1}}));
  const long = {evidence: {...evidence, start: 129, end: 131},
    jump: {...value.jump, start: 129, end: 131, context_start: 0, context_text: 'A'.repeat(129) + '\u{1f600}Z'}};
  assert.throws(() => f.api.partsFromEvidence(long));
});
test('hidden/disposed controllers do not poll or accept late reads', async () => {
  const f = fixture(), held = later();
  f.controller.wa.selectedId = RUN;
  await f.controller.refreshRun();
  assert.equal(f.timers.size, 1);
  f.root.document.hidden = true; f.controller.visibility();
  assert.equal(f.timers.size, 0);
  f.handle((url, options) => url === BASE + '/preview' ? held.promise : f.answer(url, options));
  const reading = f.controller.previewWork();
  f.controller.suspend();
  held.resolve(preview(JSON.parse(f.calls.at(-1).body)));
  await reading;
  assert.equal(f.controller.wa.preview, null);
  f.controller.dispose();
  const count = f.calls.length;
  f.controller.restore(); await tick();
  assert.equal(await f.controller.createWork(), false);
  assert.equal(f.calls.length, count);
  assert.equal(f.timers.size, 0);
});
```

Create `tests/work_analysis_browser_helpers.py`. Reuse only the established
offline browser driver's context, held-response utilities, and assertions; all
work-analysis routes are a separate exact whitelist. `shipped_csp()` reads the
actual application policy without importing or starting `webapp`.

```python
from copy import deepcopy
import ast
import hashlib
import json
import mimetypes
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from flask import render_template

from test_ai_settings_browser import (
    CSRF, ORIGIN, ROOT, STATIC, TEMPLATES, VUE_URL, OfflineUI,
)
from test_deployment_config import _ai_javascript_location


BASE = "/api/dashboard/work-analyses"
RUN = "a" * 32
UNIT = "b" * 64
EVIDENCE = "c" * 64
CLAIM = "d" * 64
BLOCK = "e" * 64
LABEL = '<img src="/fixture-injected" onerror="window.fixtureInjected=true">'
QUOTE = "\u552f\u4e00\u5f15\u6587\U0001f680 " + LABEL
BODY = ("Synthetic prefix \U0001f600 " * 16) + QUOTE + (" synthetic suffix " * 16)
TEXT_HASH = hashlib.sha256(BODY.encode("utf-8")).hexdigest()
START = BODY.index(QUOTE)
END = START + len(QUOTE)
LIMITS = {"request_limit": 16, "token_limit": 500000, "active_seconds_limit": 600}
APPROVALS = ("order_confirmed", "current_archive_only", "external_send_confirmed", "budget_confirmed")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def usage():
    return {"known_input_tokens": 0, "known_output_tokens": 0, "known_total_tokens": 0,
            "reported_requests": 1, "unreported_requests": 1,
            "network_request_count": 2, "complete": False}


def request_draft(kind="series"):
    return {"selection": {"source_kind": kind, "source_id": 501 if kind == "series" else 101,
                          "publication_state": "unknown"},
            "agent_id": 7, "output_tokens": 4000, "budget": deepcopy(LIMITS),
            "confirmations": dict.fromkeys(APPROVALS, False)}


def preview_document(payload, revision=0):
    selection = payload["selection"]
    chapters = [{"novel_id": 101, "position": 0, "title": "Synthetic chapter",
                 "text_hash": TEXT_HASH, "chars": len(BODY), "utf8_bytes": len(BODY.encode()),
                 "fetched_at": "2026-01-01T00:00:00Z", "has_text": True}]
    if selection["source_kind"] == "series":
        chapters.append({"novel_id": 102, "position": 1, "title": "Empty synthetic chapter",
                         "text_hash": hashlib.sha256(b"").hexdigest(), "chars": 0,
                         "utf8_bytes": 0, "fetched_at": None, "has_text": False})
    by_id = {row["novel_id"]: row for row in chapters}
    ordered = selection.get("ordered_novel_ids", list(by_id))
    assert sorted(ordered) == sorted(by_id)
    chapters = [{**by_id[novel_id], "position": index} for index, novel_id in enumerate(ordered)]
    manifest = {"schema_version": 1, "source_kind": selection["source_kind"],
                "source_id": selection["source_id"], "title": "Synthetic work" if revision == 0 else "Revised synthetic work",
                "publication_state": selection["publication_state"],
                "publication_source": "unknown" if selection["publication_state"] == "unknown" else "user_declared",
                "order_source": "user_confirmed" if "ordered_novel_ids" in selection else "publication_date_suggestion",
                "expected_count": 3 if selection["source_kind"] == "series" else None,
                "expected_count_observed_at": None, "remote_inventory_verified": False,
                "requires_partial_confirmation": True, "chapters": chapters}
    manifest["manifest_fingerprint"] = digest(manifest)
    unit = {"unit_id": UNIT, "kind": "whole", "is_final": True, "level": 0,
            "primary_ranges": [{"novel_id": 101, "text_hash": TEXT_HASH, "start": 0, "end": len(BODY)}],
            "context_ranges": [], "child_unit_ids": [], "claim_refs": [], "input_fingerprint": digest(manifest)}
    plan = {"schema_version": 1, "strategy": "whole", "manifest_fingerprint": manifest["manifest_fingerprint"],
            "chunker_version": 1, "prompt_version": 1, "report_schema_version": 1,
            "original_units": [unit], "minimum_requests": 1, "warnings": ["source_completeness_unverified"]}
    identity = {name: deepcopy(payload[name]) for name in ("selection", "agent_id", "output_tokens", "budget")}
    identity["fixture_configuration_revision"] = revision
    return {"selection": deepcopy(selection), "manifest": manifest, "plan": plan,
            "agent_id": payload["agent_id"], "agent_name": "Fixture Agent A", "output_tokens": payload["output_tokens"],
            "budget": deepcopy(payload["budget"]), "preview_fingerprint": digest(identity), "can_create": True,
            "confirmation_requirements": {"order_confirmed": selection["source_kind"] == "series",
                "current_archive_only": True, "external_send_confirmed": True, "budget_confirmed": True},
            "targets": [{"provider_id": 3 + revision, "provider_name": "Synthetic service" if revision == 0 else "Changed synthetic service",
                         "provider_model_id": 11 + revision,
                         "model_key": "org/model " if revision == 0 else "new/model ", "display_name": "Literal model", "pool_id": None,
                         "pool_name": None, "pool_position": None, "fallback_depth": 0}],
            "warnings": ["source_completeness_unverified"]}


def coverage(manifest, complete=False):
    return {"validated_chars": len(BODY) if complete else 0, "available_chars": len(BODY),
            "ratio": 1.0 if complete else 0.0, "complete": complete,
            "missing_novel_ids": [102] if manifest["source_kind"] == "series" else [],
            "source_completeness": {"status": "partial" if manifest["source_kind"] == "series" else "unverified",
                "expected_count": manifest["expected_count"],
                "missing_count": 1 if manifest["source_kind"] == "series" else None,
                "local_chapters": len(manifest["chapters"]), "available_chapters": 1,
                "publication_state": manifest["publication_state"], "publication_source": manifest["publication_source"],
                "order_source": manifest["order_source"]}}


def evidence_document():
    evidence = {"evidence_id": EVIDENCE, "novel_id": 101, "text_hash": TEXT_HASH,
                "block_id": BLOCK, "start": START, "end": END, "quote": QUOTE}
    context_start = max(0, START - 128)
    return {"evidence": evidence, "jump": {"novel_id": 101, "text_hash": TEXT_HASH,
            "start": START, "end": END, "context_start": context_start,
            "context_text": BODY[context_start:min(len(BODY), END + 128)]}}


def report_document(run):
    artifact = {"schema_version": 1, "unit_id": UNIT, "is_final": True,
                "summary": "Synthetic report: " + LABEL, "no_findings_reason": None,
                "input_unit_ids": [], "claims": [{"claim_id": CLAIM, "dimension": "narrative",
                    "certainty": "interpretation", "label": "Synthetic label", "statement": LABEL,
                    "evidence_ids": [EVIDENCE]}],
                "evidence": {EVIDENCE: evidence_document()["evidence"]},
                "counterexamples": [{"statement": "A bounded counterexample", "evidence_ids": [EVIDENCE]}],
                "uncertainties": ["Source completeness remains unverified."]}
    return {"schema_version": 1, "run_id": RUN, "manifest": deepcopy(run["manifest"]),
            "coverage": coverage(run["manifest"], True), "artifact": artifact,
            "units": [{"unit_id": UNIT, "kind": "whole", "actual_target": {"provider_id": 3, "model_key": "org/model "},
                       "usage_summary": usage()}], "usage_summary": usage(),
            "created_at": "2026-01-01T00:00:00Z", "published_at": "2026-01-01T00:01:00Z",
            "report_stale": False, "archive_outdated": False, "synthesis_complete": True}


def shipped_csp():
    source = (ROOT / "src/pixiv_novel_sync/webapp.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    matches = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Attribute) and node.func.attr == "setdefault"
               and node.args and isinstance(node.args[0], ast.Constant)
               and node.args[0].value == "Content-Security-Policy"]
    assert len(matches) == 1
    join = matches[0].args[1]
    assert isinstance(join, ast.Call) and isinstance(join.func, ast.Attribute) and join.func.attr == "join"
    assert isinstance(join.args[0], ast.Tuple)
    directives = []
    for item in join.args[0].elts:
        if isinstance(item, ast.Constant):
            directives.append(item.value)
        else:
            assert isinstance(item, ast.JoinedStr)
            parts = []
            for part in item.values:
                if isinstance(part, ast.Constant):
                    parts.append(part.value)
                else:
                    assert isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Call)
                    assert isinstance(part.value.func, ast.Name) and part.value.func.id == "_csp_nonce"
                    parts.append("test-nonce")
            directives.append("".join(parts))
    return "; ".join(directives)


class OfflineWorkUI(OfflineUI):
    def __init__(self, browser, local_vue, width):
        super().__init__(browser, local_vue, width)
        self.work_run = None
        self.work_report = None
        self.idempotency = {}
        self.fail_reads = set()
        self.lose_next = set()
        self.lose_before_apply = set()
        self.preview_revision = 0
        self.evidence_status = 200
        self.reader_hash = TEXT_HASH
        self.work_agents = [{"id": 7, "name": "Fixture Agent A", "enabled": True},
                            {"id": 8, "name": "Fixture Agent B", "enabled": True},
                            {"id": 9, "name": LABEL, "enabled": False}]
        self.csp = shipped_csp()
        self.cache_expression, cache_body = _ai_javascript_location()
        self.js_cache = re.search(r'add_header Cache-Control "([^"]+)" always;', cache_body).group(1)

    def seed_run(self, status="succeeded", payload=None):
        preview = preview_document(payload or request_draft(), self.preview_revision)
        self.work_run = {"run_id": RUN, "version": 1, "status": status, "reason": None,
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "completed_at": None, "deleted_at": None, "deleted": False,
            "source_kind": preview["manifest"]["source_kind"], "source_id": preview["manifest"]["source_id"],
            "title": preview["manifest"]["title"], "manifest": deepcopy(preview["manifest"]),
            "manifest_fingerprint": preview["manifest"]["manifest_fingerprint"],
            "budget": deepcopy(preview["budget"]), "request_count": 2, "planned_tokens": 8000,
            "active_seconds_debited": 5, "unit_counts": {"total": 1, "succeeded": int(status == "succeeded")},
            "coverage": coverage(preview["manifest"], status == "succeeded"),
            "usage_summary": usage(), "has_report": status == "succeeded",
            "can_resume": status in {"paused", "cancelled", "failed"},
            "report_stale": False, "archive_outdated": False}
        self.work_report = report_document(self.work_run) if status == "succeeded" else None
        return deepcopy(self.work_run)

    def transition(self, status, reason=None):
        self.work_run.update(status=status, reason=reason, version=self.work_run["version"] + 1,
                             can_resume=status in {"paused", "cancelled", "failed"})

    def render(self, path):
        parsed = urlsplit(path)
        names = {"/dashboard/preferences": "dashboard_preferences.html",
                 "/dashboard/novels/101": "dashboard_novel_detail.html",
                 "/dashboard/series/501": "dashboard_series_detail.html"}
        assert parsed.path in names
        with self.app.test_request_context(path):
            return render_template(names[parsed.path], task_labels={}, csp_nonce=lambda: "test-nonce",
                novel_id=101, series_id=501,
                work_analysis_view=parse_qs(parsed.query).get("view") == ["work-analysis"])

    def _json(self, route, data, status=200, envelope=True):
        route.fulfill(status=status, content_type="application/json",
            headers={"Cache-Control": "private, no-store, no-cache, max-age=0, must-revalidate"},
            body=json.dumps({"ok": True, "data": data} if envelope else data, ensure_ascii=False))

    def _error(self, route, code, status):
        return self._json(route, {"ok": False, "error": code, "code": code}, status, envelope=False)

    def _route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        record = {"method": request.method, "url": request.url, "path": parsed.path,
                  "query": parse_qs(parsed.query), "body": json.loads(request.post_data) if request.post_data else None,
                  "csrf": request.headers.get("x-csrf-token")}
        self.requests.append(record)
        if request.method == "GET" and request.url == VUE_URL:
            return route.fulfill(body=self.vue, content_type="application/javascript",
                                 headers={"Access-Control-Allow-Origin": "*"})
        if parsed.scheme + "://" + parsed.netloc != ORIGIN:
            self.unknown.append(record)
            return route.abort("blockedbyclient")
        if request.method == "GET" and parsed.path in {
                "/dashboard/preferences", "/dashboard/novels/101", "/dashboard/series/501"}:
            return route.fulfill(content_type="text/html", body=self.render(parsed.path + "?" + parsed.query),
                                 headers={"Content-Security-Policy": self.csp, "Cache-Control": "no-store"})
        static_names = {"/static/js/ai_settings.js", "/static/js/ai_picker.js", "/static/js/work_analysis.js",
                        "/static/css/tailwind.css"}
        if request.method == "GET" and parsed.path in static_names:
            assert not parsed.query or parsed.path == "/static/css/tailwind.css"
            target = (STATIC / parsed.path.removeprefix("/static/")).resolve()
            assert target.is_relative_to(STATIC.resolve()) and target.is_file()
            headers = {"Cache-Control": self.js_cache if self.cache_expression.fullmatch(parsed.path) else "no-cache"}
            return route.fulfill(body=target.read_bytes(), content_type=mimetypes.guess_type(target.name)[0], headers=headers)
        if request.method == "GET" and parsed.path == "/favicon.ico":
            return route.fulfill(status=204, body="")
        if parsed.path.startswith("/api/"):
            key = (request.method, parsed.path)
            if request.method != "GET" and key not in self.allowed_writes:
                self.unknown.append(record)
                return route.abort("blockedbyclient")
            if key in self.holds:
                self.pending[key].append((route, record))
                return None
            return self._api(route, record)
        self.unknown.append(record)
        return route.abort("blockedbyclient")

    def _write_result(self, route, record):
        key = (record["method"], record["path"])
        if key in self.lose_next:
            self.lose_next.remove(key)
            return route.abort("failed")
        return self._json(route, deepcopy(self.work_run))

    def _api(self, route, record):
        method, path, body, query = record["method"], record["path"], record["body"], record["query"]
        if method != "GET":
            assert record["csrf"] == CSRF
            assert query == {}
            if (method, path) in self.lose_before_apply:
                self.lose_before_apply.remove((method, path))
                return route.abort("failed")
        paged = path in {BASE, BASE + "/" + RUN + "/units", "/api/dashboard/recommendations/items"}
        if query and not paged:
            self.unknown.append(record)
            return route.abort("blockedbyclient")
        if method == "GET" and path in self.fail_reads:
            return self._error(route, "fixture_read_unavailable", 503)
        raw = {"/api/csrf-token": {"csrf_token": CSRF}, "/api/dashboard/shell-data": {"pending_count": 0},
               "/api/dashboard/status": {"current_user": None}, "/api/dashboard/auto-sync/status": {"running": False},
               "/api/dashboard/novels/101/progress": {"progress": 0}}
        if method == "GET" and path in raw:
            return self._json(route, raw[path], envelope=False)
        if method == "GET" and path == "/api/dashboard/preferences/profiles":
            return self._json(route, [])
        if method == "GET" and path == "/api/dashboard/recommendations/items":
            assert set(query) <= {"page", "page_size", "status"}
            return self._json(route, {"items": [], "page": 1, "page_size": 10, "total": 0, "total_pages": 1})
        if method == "GET" and path == "/api/dashboard/ai/agents":
            return self._json(route, deepcopy(self.work_agents))
        if method == "GET" and path == "/api/dashboard/novels/101":
            return self._json(route, {"novel_id": 101, "title": "Synthetic reader", "author_name": "Fixture author",
                "user_id": None, "series_id": None, "text_raw": BODY, "text_markdown": None,
                "text_hash": self.reader_hash, "text_length": len(BODY), "total_bookmarks": 0,
                "total_views": 0, "tags": [], "create_date": None, "rescue": None}, envelope=False)
        if method == "GET" and path == "/api/dashboard/series/501":
            return self._json(route, {"series_id": 501, "title": "Synthetic series", "user_id": None,
                "author_name": "Fixture author", "caption": "Synthetic", "total_novels": 2,
                "total_text_length": len(BODY), "rescue": None, "novels": [
                    {"novel_id": 101, "title": "Synthetic chapter", "text_length": len(BODY), "create_date": None},
                    {"novel_id": 102, "title": "Empty synthetic chapter", "text_length": 0, "create_date": None}]}, envelope=False)
        if method == "POST" and path == BASE + "/preview":
            assert set(body) == {"selection", "agent_id", "output_tokens", "budget", "confirmations"}
            return self._json(route, preview_document(body, self.preview_revision))
        if method == "POST" and path == BASE:
            assert set(body) == {"selection", "agent_id", "output_tokens", "budget", "confirmations",
                                 "preview_fingerprint", "idempotency_key"}
            key = body["idempotency_key"]
            if key in self.idempotency:
                if self.idempotency[key] != body:
                    return self._error(route, "idempotency_conflict", 409)
                return self._write_result(route, record)
            current = preview_document(body, self.preview_revision)
            if body["preview_fingerprint"] != current["preview_fingerprint"]:
                return self._error(route, "stale_preview", 409)
            assert all(body["confirmations"][name] is True for name, required in current["confirmation_requirements"].items() if required)
            self.idempotency[key] = deepcopy(body)
            self.seed_run("queued", body)
            self.work_run.update(request_count=0, planned_tokens=0, active_seconds_debited=0,
                usage_summary={"known_input_tokens": None, "known_output_tokens": None, "known_total_tokens": None,
                    "reported_requests": 0, "unreported_requests": 0, "network_request_count": 0, "complete": False})
            return self._write_result(route, record)
        if method == "GET" and path == BASE:
            assert set(query) <= {"offset", "limit"}
            items = [] if self.work_run is None or self.work_run["deleted"] else [deepcopy(self.work_run)]
            return self._json(route, {"items": items, "total": len(items), "offset": int(query.get("offset", [0])[0]),
                                      "limit": int(query.get("limit", [20])[0])})
        run_path = BASE + "/" + RUN
        if path == run_path and method == "GET":
            return self._json(route, deepcopy(self.work_run)) if self.work_run else self._error(route, "not_found", 404)
        if path in {run_path + suffix for suffix in ("/units", "/report", "/evidence/" + EVIDENCE)} and method == "GET":
            if not self.work_run or self.work_run["deleted"]:
                return self._error(route, "work_deleted", 410)
            if path.endswith("/units"):
                assert set(query) <= {"offset", "limit"}
                item = {"unit_id": UNIT, "kind": "whole", "is_final": True, "level": 0,
                        "status": "succeeded" if self.work_run["has_report"] else "running", "try_no": 1,
                        "last_error": None, "actual_target": {"provider_id": 3, "model_key": "org/model "},
                        "usage_summary": usage()}
                return self._json(route, {"items": [item], "total": 1, "offset": 0, "limit": 20})
            if not self.work_report:
                return self._error(route, "report_not_ready", 409)
            if path.endswith("/report"):
                return self._json(route, deepcopy(self.work_report))
            if self.evidence_status != 200:
                return self._error(route, "source_changed", self.evidence_status)
            return self._json(route, evidence_document())
        actions = {run_path + "/" + action: action for action in ("pause", "cancel", "resume", "budget")}
        if (method == "POST" and path in actions) or (method == "DELETE" and path == run_path):
            if body["expected_version"] != self.work_run["version"]:
                return self._error(route, "version_conflict", 409)
            action = actions.get(path, "delete")
            if action == "budget":
                assert body["budget_confirmed"] is True
                self.work_run["budget"] = deepcopy(body["budget"])
                self.work_run["version"] += 1
            elif action == "delete":
                self.transition("cancelled", "deleted")
                self.work_run.update(deleted=True, deleted_at="2026-01-01T00:02:00Z", has_report=False,
                    can_resume=False, title=None, manifest=None, manifest_fingerprint=None, source_id=None, source_kind=None)
                self.work_report = None
            else:
                status, reason = {"pause": ("pausing", "user_pause"), "cancel": ("cancelling", "user_cancel"),
                                  "resume": ("queued", None)}[action]
                if action == "pause" and self.work_run["status"] == "paused":
                    status = "paused"
                self.transition(status, reason)
            return self._write_result(route, record)
        self.unknown.append(record)
        return route.abort("blockedbyclient")

    def prepare_preview(self, kind="series"):
        self.goto("/dashboard/preferences?view=work-analysis&source_kind=" + kind + "&source_id=" + ("501" if kind == "series" else "101"))
        page = self.page
        page.locator("#wa-agent").click()
        dialog = page.locator("#wa-agent-dialog")
        self.assert_modal(dialog)
        dialog.get_by_role("option").filter(has_text="Fixture Agent A").click()
        for suffix, value in (("request", 16), ("token", 500000), ("time", 600)):
            page.locator("#wa-budget-" + suffix).fill(str(value))
        self.allow("POST", BASE + "/preview")
        page.locator("#wa-preview").click()
        self.expect(page.locator("#wa-create")).to_be_visible()
        return page

    def confirm_preview(self):
        for suffix in ("order", "archive", "send", "budget"):
            field = self.page.locator("#wa-confirm-" + suffix)
            if field.count():
                field.check()

    def capture(self, name):
        target = ROOT / ".superpowers/sdd/phase2-browser" / ("touch-" + str(self.width))
        target.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(target / (name + ".png")), full_page=True)
        (target / (name + "-requests.json")).write_text(json.dumps(self.requests, ensure_ascii=False, indent=2), encoding="utf-8")

    def assert_storage(self):
        for item in self.storage_writes:
            assert item["method"] == "setItem", item
            value = json.loads(item["args"][1])
            if item["kind"] == "local":
                assert item["args"][0] == "pixivNovelSync:reading:101", item
                assert set(value) == {"novelId", "seriesId", "title", "scrollY", "progress", "updatedAt"}
                assert value["novelId"] == 101 and value["title"] == "Synthetic reader"
            else:
                assert item["kind"] == "session" and item["args"][0] == "pixivNovelSync:listScroll:/dashboard/series/501"
                assert set(value) == {"scrollY"} and type(value["scrollY"]) in {int, float} and value["scrollY"] >= 0
        for origin in self.context.storage_state()["origins"]:
            assert origin["origin"] == ORIGIN
            assert all(item["name"] == "pixivNovelSync:reading:101" for item in origin["localStorage"])
        assert self.page.evaluate("Object.keys(sessionStorage).every(key => key === 'pixivNovelSync:listScroll:/dashboard/series/501')")
        recorded = json.dumps(self.storage_writes, ensure_ascii=False)
        assert BODY not in recorded and QUOTE not in recorded and LABEL not in recorded
```

The browser fixture uses structurally valid whole/final artifacts and explicit
same-version evidence, but it is not a backend/domain validator. Task 2/3/7
tests establish those contracts; these synthetic responses only exercise the
shipped UI against them. It never calls a model, reads a user's novels, starts
the Web service, or imports an execution coordinator.

Create `tests/test_work_analysis_browser.py`:

```python
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from test_ai_settings_browser import WIDTHS, browser, local_vue
from work_analysis_browser_helpers import (
    BASE, BODY, END, EVIDENCE, LABEL, QUOTE, RUN, START, OfflineWorkUI,
)


@pytest.fixture(params=WIDTHS, ids=lambda width: "touch-" + str(width) + "px")
def ui(request, browser, local_vue):
    fixture = OfflineWorkUI(browser, local_vue, request.param)
    yield fixture
    try:
        assert fixture.unknown == [], "Non-whitelisted browser requests: " + repr(fixture.unknown)
        assert fixture.page_errors == [], "Actual Vue/Chromium errors: " + repr(fixture.page_errors)
        assert all("/ai/jobs" not in row["path"] and "/probe" not in row["path"] for row in fixture.requests)
        fixture.assert_storage()
    finally:
        fixture.context.close()


def test_no_view_preferences_and_plain_reader_keep_existing_read_only_behavior(ui):
    page = ui.page
    ui.goto("/dashboard/preferences")
    ui.expect(page.get_by_role("heading", name="偏好与推荐", exact=True)).to_be_visible()
    ui.expect(page.locator("#preference-management")).to_be_visible()
    assert ui.calls("GET", "/api/dashboard/preferences/profiles")
    assert ui.calls("GET", "/api/dashboard/recommendations/items")
    assert not any(row["path"].startswith(BASE) for row in ui.requests)
    assert not ui.calls("GET", "/static/js/work_analysis.js")
    assert ui.writes() == []
    ui.goto("/dashboard/novels/101")
    ui.expect(page.get_by_role("heading", name="Synthetic reader", exact=True)).to_be_visible()
    link = page.get_by_role("link", name="分析作品", exact=True)
    ui.expect(link).to_have_attribute("href", "/dashboard/preferences?view=work-analysis&source_kind=novel&source_id=101")
    assert not ui.calls("GET", "/static/js/work_analysis.js")
    assert not any(row["path"].startswith(BASE) for row in ui.requests)
    assert ui.writes() == []


def test_series_entry_real_picker_order_budget_and_preview_invalidation(ui):
    page = ui.page
    ui.goto("/dashboard/series/501")
    page.get_by_role("link", name="分析作品", exact=True).click()
    ui.expect(page.locator("#wa-source-kind")).to_have_value("series")
    ui.expect(page.locator("#wa-source-id")).to_have_value("501")
    for name in ("request", "token", "time"):
        ui.expect(page.locator("#wa-budget-" + name)).to_have_value("")
    page.locator("#wa-agent").focus()
    page.locator("#wa-agent").press("Enter")
    dialog = page.locator("#wa-agent-dialog")
    ui.assert_modal(dialog)
    ui.expect(dialog.get_by_role("option").filter(has_text=LABEL)).to_be_disabled()
    assert dialog.locator("img").count() == 0
    assert not page.locator("#wa-agent-filter").evaluate("el => el === document.activeElement")
    dialog.get_by_role("option").filter(has_text="Fixture Agent A").click()
    for name, value in (("request", 16), ("token", 500000), ("time", 600)):
        page.locator("#wa-budget-" + name).fill(str(value))
    ui.allow("POST", BASE + "/preview")
    page.locator("#wa-preview").click()
    ui.expect(page.locator("#wa-create")).to_be_disabled()
    ui.expect(page.get_by_text("缺失或空白正文", exact=False)).to_be_visible()
    assert page.locator('section[aria-label="预览与外发确认"] code').text_content() == "org/model "
    assert len(ui.writes()) == 1 and ui.writes()[0]["path"] == BASE + "/preview"
    page.get_by_role("button", name="下移第 1 章", exact=True).click()
    ui.expect(page.locator("#wa-create")).to_have_count(0)
    page.locator("#wa-preview").click()
    ui.expect(page.locator("#wa-create")).to_be_visible()
    assert ui.calls("POST", BASE + "/preview")[-1]["body"]["selection"]["ordered_novel_ids"] == [102, 101]
    ui.confirm_preview()
    ui.expect(page.locator("#wa-create")).to_be_enabled()
    page.locator("#wa-budget-token").fill("500001")
    ui.expect(page.locator("#wa-create")).to_have_count(0)
    page.locator("#wa-preview").click()
    ui.expect(page.locator("#wa-confirm-budget")).not_to_be_checked()
    ui.confirm_preview()
    ui.allow("POST", BASE)
    ui.hold("POST", BASE)
    page.locator("#wa-create").dblclick()
    ui.expect(page.get_by_text("正在提交；返回前不能确认远端调用已经结束。", exact=True)).to_be_visible()
    assert len(ui.calls("POST", BASE)) == 1
    ui.release("POST", BASE)
    ui.expect(page.locator("#wa-run-state")).to_contain_text("queued")
    request = ui.calls("POST", BASE)[0]["body"]
    assert request["budget"]["token_limit"] == 500001
    assert all(request["confirmations"].values())
    assert re.fullmatch(r"[0-9a-f]{32}", request["idempotency_key"])
    assert BODY not in json.dumps(request, ensure_ascii=False)
    ui.assert_viewport()
    ui.capture("created-from-series")


def test_removed_current_agent_does_not_auto_select_other_or_keep_consent(ui):
    page = ui.prepare_preview()
    ui.confirm_preview()
    ui.work_agents = [row for row in ui.work_agents if row["id"] != 7]
    page.get_by_role("button", name="刷新 Agent", exact=True).click()
    ui.expect(page.locator("#wa-create")).to_have_count(0)
    page.locator("#wa-agent").click()
    dialog = page.locator("#wa-agent-dialog")
    current = dialog.get_by_role("option").filter(has_text="Fixture Agent A")
    ui.expect(current).to_be_visible()
    ui.expect(current).to_be_disabled()
    dialog.get_by_role("option").filter(has_text="Fixture Agent B").click()
    page.locator("#wa-preview").click()
    ui.expect(page.locator("#wa-create")).to_be_disabled()
    assert ui.calls("POST", BASE + "/preview")[-1]["body"]["agent_id"] == 8
    assert ui.calls("POST", BASE) == []


def test_same_form_refresh_and_server_changed_preview_require_new_consent(ui):
    page = ui.prepare_preview()
    for revision in (0, 1):
        ui.confirm_preview()
        ui.expect(page.locator("#wa-create")).to_be_enabled()
        ui.preview_revision = revision
        page.locator("#wa-preview").click()
        ui.expect(page.locator("#wa-create")).to_be_disabled()
        for suffix in ("order", "archive", "send", "budget"):
            ui.expect(page.locator("#wa-confirm-" + suffix)).not_to_be_checked()
        if revision:
            preview = page.locator('section[aria-label="预览与外发确认"]')
            ui.expect(preview).to_contain_text("Changed synthetic service")
            ui.expect(preview).to_contain_text("Revised synthetic work")
            assert preview.locator("code").text_content() == "new/model "
    assert len(ui.calls("POST", BASE + "/preview")) == 3
    assert ui.calls("POST", BASE) == []


def test_unknown_create_reuses_the_same_body_and_known_success_survives_readback_failure(ui):
    page = ui.prepare_preview("novel")
    ui.confirm_preview()
    ui.allow("POST", BASE)
    ui.lose_next.add(("POST", BASE))
    ui.fail_reads.update({BASE, BASE + "/" + RUN})
    page.locator("#wa-create").click()
    retry = page.get_by_role("button", name="重试同一创建", exact=True)
    ui.expect(retry).to_be_visible()
    ui.expect(page.locator("#wa-source-id")).to_be_disabled()
    original = ui.calls("POST", BASE)[0]["body"]
    retry.click()
    ui.expect(page.locator("#wa-run-state")).to_contain_text("queued")
    ui.expect(page.get_by_text("写入已确认，但刷新失败；请只重试读取，不要重复提交。", exact=True)).to_be_visible()
    ui.expect(retry).to_have_count(0)
    assert len(ui.idempotency) == 1
    assert len(ui.calls("POST", BASE)) == 2
    assert ui.calls("POST", BASE)[1]["body"] == original
    ui.fail_reads.clear()
    page.get_by_role("button", name="刷新状态", exact=True).click()
    page.get_by_role("button", name="刷新记录", exact=True).click()
    ui.expect(page.locator('[data-run-id="' + RUN + '"]')).to_be_visible()
    assert len(ui.calls("POST", BASE)) == 2
    ui.capture("unknown-create-reconciled")


def test_pause_cancel_resume_budget_and_delete_are_separate_confirmed_operations(ui):
    ui.seed_run("running")
    page = ui.page
    ui.goto("/dashboard/preferences?view=work-analysis&analysis_id=" + RUN)
    path = BASE + "/" + RUN
    ui.allow("POST", path + "/pause")
    ui.lose_next.add(("POST", path + "/pause"))
    page.get_by_role("button", name="暂停分析", exact=True).click()
    ui.expect(page.get_by_role("button", name="先核对当前状态", exact=True)).to_be_visible()
    assert page.get_by_role("button", name="重试同一 CAS 请求", exact=True).count() == 0
    page.get_by_role("button", name="先核对当前状态", exact=True).click()
    ui.expect(page.locator("#wa-run-state")).to_contain_text("pausing")
    ui.expect(page.get_by_text("当前状态已核对；原写入结果仍未知，未重复提交。", exact=True)).to_be_visible()
    assert page.get_by_text("写入已确认。", exact=True).count() == 0
    ui.expect(page.get_by_text("正在等待当前调用真实退出；不保证远端已停止计算，不提前释放共享槽。", exact=True)).to_be_visible()
    assert len(ui.calls("POST", path + "/pause")) == 1
    ui.transition("paused", "user_pause")
    page.get_by_role("button", name="刷新状态", exact=True).click()
    ui.expect(page.get_by_role("button", name="恢复未完成分析", exact=True)).to_be_enabled()
    old = {name: ui.work_run[name] for name in ("request_count", "planned_tokens", "active_seconds_debited")}
    page.get_by_text("明确调整预算（不清零累计计数）", exact=True).click()
    page.locator("#wa-amend-request").fill("17")
    ui.expect(page.get_by_role("button", name="确认调整预算", exact=True)).to_be_disabled()
    page.locator("#wa-confirm-amend").check()
    ui.allow("POST", path + "/budget")
    page.get_by_role("button", name="确认调整预算", exact=True).click()
    ui.expect(page.locator("#wa-confirm-amend")).not_to_be_checked()
    assert ui.calls("POST", path + "/budget")[0]["body"]["budget_confirmed"] is True
    assert ui.work_run["budget"]["request_limit"] == 17
    assert all(ui.work_run[name] == value for name, value in old.items())
    ui.allow("POST", path + "/resume")
    page.get_by_role("button", name="恢复未完成分析", exact=True).click()
    ui.expect(page.locator("#wa-run-state")).to_contain_text("queued")
    assert all(ui.work_run[name] == value for name, value in old.items())
    ui.allow("POST", path + "/cancel")
    page.get_by_role("button", name="取消分析", exact=True).click()
    ui.expect(page.locator("#wa-run-state")).to_contain_text("cancelling")
    ui.transition("cancelled", "user_cancel")
    page.get_by_role("button", name="刷新状态", exact=True).click()
    ui.expect(page.locator("#wa-run-state")).to_contain_text("cancelled")
    ui.expect(page.get_by_role("button", name="恢复未完成分析", exact=True)).to_be_enabled()
    ui.capture("cancelled-checkpoint")
    page.get_by_text("删除本分析", exact=True).click()
    ui.expect(page.get_by_role("button", name="删除分析", exact=True)).to_be_disabled()
    page.locator("#wa-confirm-delete").check()
    ui.allow("DELETE", path)
    page.get_by_role("button", name="删除分析", exact=True).click()
    ui.expect(page.get_by_text("分析已删除，不能恢复；仅保留必要无正文审计。", exact=True)).to_be_visible()
    assert ui.work_run["deleted"] is True and ui.work_report is None
    assert page.locator("#wa-report").count() == 0
    assert page.get_by_role("button", name="恢复未完成分析", exact=True).count() == 0
    assert len(ui.calls("DELETE", path)) == 1


@pytest.mark.parametrize("yield_status", ["pausing", "paused"])
def test_lost_user_pause_does_not_confirm_a_yield_and_allows_a_new_pause(ui, yield_status):
    ui.seed_run("running")
    page = ui.page
    ui.goto("/dashboard/preferences?view=work-analysis&analysis_id=" + RUN)
    path = BASE + "/" + RUN
    ui.allow("POST", path + "/pause")
    ui.lose_before_apply.add(("POST", path + "/pause"))
    page.get_by_role("button", name="暂停分析", exact=True).click()
    check = page.get_by_role("button", name="先核对当前状态", exact=True)
    ui.expect(check).to_be_visible()
    ui.transition(yield_status, "sync_yield")
    expected_version = ui.work_run["version"]
    check.click()
    ui.expect(page.locator("#wa-run-state")).to_contain_text("sync_yield")
    ui.expect(page.get_by_text("状态已经变化；未确认原写入，也未重复提交，请按当前状态重新选择。", exact=True)).to_be_visible()
    assert page.get_by_text("写入已确认。", exact=True).count() == 0
    assert len(ui.calls("POST", path + "/pause")) == 1
    pause = page.get_by_role("button", name="暂停分析", exact=True)
    ui.expect(pause).to_be_enabled()
    pause.click()
    ui.expect(page.locator("#wa-run-state")).to_contain_text("user_pause")
    assert ui.work_run["status"] == yield_status
    writes = ui.calls("POST", path + "/pause")
    assert len(writes) == 2
    assert writes[0]["body"]["expected_version"] == 1
    assert writes[1]["body"]["expected_version"] == expected_version
    assert ui.calls("POST", path + "/resume") == []


def test_report_is_escaped_collapsed_and_reader_uses_only_server_codepoint_evidence(ui):
    ui.seed_run("succeeded")
    page = ui.page
    ui.goto("/dashboard/preferences?view=work-analysis&analysis_id=" + RUN)
    assert ui.calls("GET", BASE + "/" + RUN + "/report") == []
    page.get_by_role("button", name="读取已验证报告", exact=True).click()
    report = page.locator("#wa-report")
    ui.expect(report).to_be_visible()
    ui.expect(report.get_by_text(LABEL, exact=True)).to_be_visible()
    assert report.locator("img").count() == 0
    assert page.evaluate("window.fixtureInjected === undefined")
    fold = report.locator('[data-evidence-id="' + EVIDENCE + '"]').first
    assert not fold.evaluate("el => el.open")
    ui.expect(fold.locator("blockquote")).to_be_hidden()
    ui.expect(report).to_contain_text("解释性判断")
    ui.expect(report).to_contain_text("未报告请求：1")
    ui.expect(report).to_contain_text("原作完整性 partial")
    ui.capture("report-evidence-collapsed")
    fold.locator("summary").click()
    ui.expect(fold.locator("blockquote")).to_have_text(QUOTE)
    fold.get_by_role("button", name="核对当前正文片段", exact=True).click()
    ui.expect(fold.locator("mark")).to_have_text(QUOTE)
    anchor = fold.get_by_role("link", name="到阅读页核对同版本证据", exact=True)
    parsed = urlsplit(anchor.get_attribute("href"))
    assert parsed.path == "/dashboard/novels/101" and parsed.fragment == "work-analysis-evidence"
    assert parse_qs(parsed.query) == {"analysis_id": [RUN], "evidence_id": [EVIDENCE]}
    anchor.click()
    panel = page.locator("#work-analysis-evidence")
    ui.expect(panel.locator("mark")).to_have_text(QUOTE)
    ui.expect(panel).to_contain_text("[" + str(START) + ", " + str(END) + ")")
    assert panel.evaluate("el => el === document.activeElement")
    assert len(panel.locator("pre").inner_text()) <= 496
    assert panel.locator("img").count() == 0
    ui.assert_viewport()
    ui.capture("reader-same-hash-highlight")
    ui.reader_hash = "f" * 64
    page.reload(wait_until="networkidle")
    ui.expect(panel.get_by_role("alert")).to_contain_text("当前阅读正文与证据版本不同")
    assert panel.locator("mark").count() == 0
    assert ui.writes() == []


def test_stale_report_keeps_historical_quote_but_410_removes_current_jump(ui):
    ui.seed_run("succeeded")
    ui.work_run["report_stale"] = True
    ui.work_report["report_stale"] = True
    ui.evidence_status = 410
    page = ui.page
    ui.goto("/dashboard/preferences?view=work-analysis&analysis_id=" + RUN)
    page.get_by_role("button", name="读取已验证报告", exact=True).click()
    report = page.locator("#wa-report")
    ui.expect(report).to_contain_text("这是旧版本报告")
    fold = report.locator('[data-evidence-id="' + EVIDENCE + '"]').first
    fold.locator("summary").click()
    fold.get_by_role("button", name="核对当前正文片段", exact=True).click()
    ui.expect(fold.get_by_role("alert")).to_contain_text("当前正文版本不再支持此证据定位")
    ui.expect(fold.locator("blockquote")).to_have_text(QUOTE)
    assert fold.locator("mark").count() == 0
    fold.get_by_role("link", name="到阅读页核对同版本证据", exact=True).click()
    panel = page.locator("#work-analysis-evidence")
    ui.expect(panel.get_by_role("alert")).to_contain_text("当前正文版本不再支持此证据定位")
    assert panel.locator("mark").count() == 0
    assert ui.writes() == []
```

- [ ] **Step 2: Run the UI integration RED command.**

Run `python -m pytest tests/test_work_analysis_ui.py tests/test_work_analysis_runtime.py tests/test_deployment_config.py -q`.
Expected before implementation: missing new partial/script, absent novel
`text_hash`, absent Agent-kind identity, and the new exact-name asset not matching the Nginx policy. Do not
remove old UI or cache assertions to turn these failures green.

Also run the opt-in real-browser RED command with the already present Vue file
and an already installed Playwright Chromium. These commands do not download
Vue, browsers, models, or packages:

```powershell
$env:AI_UI_VUE_JS = (Resolve-Path -LiteralPath '.superpowers/sdd/vendor/vue-3.4.38.global.prod.js').Path
python -m pytest tests/test_work_analysis_browser.py -q -rs
```

Expected with those prerequisites: failures for the absent work view, new
script, Agent-kind selection, and evidence panel. A missing dependency or a
skip is **blocked browser validation**, not a RED product regression and never
a browser pass. Preserve the real output; do not install anything or substitute
the VM to manufacture browser evidence.

- [ ] **Step 3: Add the real Vue controller and reader adapter.**

Create `src/pixiv_novel_sync/static/js/work_analysis.js` with the following
complete implementation. The existing UI uses Chinese copy; the new copy
deliberately follows that established language rather than changing the site's
visual or language conventions.

```javascript
(function (root) {
  'use strict';
  const BASE = '/api/dashboard/work-analyses';
  const approvals = ['order_confirmed', 'current_archive_only', 'external_send_confirmed', 'budget_confirmed'];
  const copy = value => JSON.parse(JSON.stringify(value));
  const hash32 = value => typeof value === 'string' && /^[0-9a-f]{32}$/.test(value);
  const hash64 = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
  const budgetKeys = ['request_limit', 'token_limit', 'active_seconds_limit'];
  const limits = {request_limit: [1, 512], token_limit: [1, 100000000], active_seconds_limit: [30, 14400]};
  function integer(value, low, high) {
    const raw = String(value);
    if (!/^[1-9][0-9]*$/.test(raw)) throw new Error('请输入有限正整数');
    const result = Number(raw);
    if (!Number.isSafeInteger(result) || result < low || result > high) throw new Error('数值超出允许范围');
    return result;
  }
  function budget(value) {
    return Object.fromEntries(budgetKeys.map(key => [key, integer(value[key], ...limits[key])]));
  }
  function newKey() {
    return Array.from(root.crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, '0')).join('');
  }
  function safeError(error) {
    if (error && (error.status === 401 || error.status === 403)) return '登录或 CSRF 校验失败，请重新登录后重试。';
    if (error && error.status === 410) return '分析已删除，或当前正文版本不再支持此证据定位。';
    if (error && error.status === 409) return '状态、正文、配置或预览已变化，请刷新并重新确认。';
    if (error && error.status === 413) return '输入或产物超过上限，未进行裁剪。';
    if (error && error.code) return '请求未完成：' + error.code;
    return '连接失败或结果未知；请先核对已有任务，不要另建重复分析。';
  }
  async function request(url, options) {
    const response = await root.csrfFetch(url, {credentials: 'same-origin', cache: 'no-store', ...options});
    let result;
    try { result = await response.json(); }
    catch (_error) { throw Object.assign(new Error('invalid_response'), {status: null}); }
    if (!response.ok || result.ok !== true || !Object.prototype.hasOwnProperty.call(result, 'data')) {
      const code = typeof result.code === 'string' && /^[a-z][a-z0-9_]{0,79}$/.test(result.code) ? result.code : null;
      throw Object.assign(new Error('request_failed'), {status: response.status, code});
    }
    return result.data;
  }
  function sourceFromSearch(search) {
    const params = new URLSearchParams(search);
    return {source_kind: params.get('source_kind') === 'series' ? 'series' : 'novel',
      source_id: params.get('source_id') || '', analysis_id: params.get('analysis_id') || ''};
  }
  function partsFromEvidence(value) {
    const evidence = value && value.evidence, jump = value && value.jump;
    if (!evidence || !jump || evidence.novel_id !== jump.novel_id || evidence.text_hash !== jump.text_hash ||
        evidence.start !== jump.start || evidence.end !== jump.end || !hash64(jump.text_hash) ||
        ![jump.start, jump.end, jump.context_start].every(Number.isSafeInteger) ||
        typeof jump.context_text !== 'string') throw new Error('invalid_evidence');
    const chars = Array.from(jump.context_text), start = jump.start - jump.context_start, end = jump.end - jump.context_start;
    if (chars.length > 496 || jump.context_start < 0 || start < 0 || start > 128 ||
        start >= end || end > chars.length || chars.length - end > 128 || end - start > 240 ||
        chars.slice(start, end).join('') !== evidence.quote) throw new Error('invalid_evidence');
    return {before: chars.slice(0, start).join(''), quote: chars.slice(start, end).join(''),
      after: chars.slice(end).join('')};
  }
  function usageText(value) {
    if (!value) return '用量未知 / 未报告';
    const known = value.known_total_tokens == null ? '未知' : String(value.known_total_tokens);
    return '已报告 token：' + known + '；未报告请求：' + String(value.unreported_requests ?? '未知') +
      (value.complete ? '；报告完整' : '；用量不完整，不按 0 计费');
  }
  function coverageText(value) {
    if (!value) return '尚无已验证输入覆盖';
    return '已验证输入 ' + value.validated_chars + ' / ' + value.available_chars + ' code point (' +
      Math.round(value.ratio * 10000) / 100 + '%)；不表示理解正确或原作已经完结';
  }
  function createController(Vue, send = request, search = root.location.search) {
    const source = sourceFromSearch(search);
    const wa = Vue.reactive({
      form: {source_kind: source.source_kind, source_id: source.source_id, publication_state: 'unknown',
        agent_id: '', output_tokens: '4000', budget: {request_limit: '', token_limit: '', active_seconds_limit: ''}},
      order: [], chapters: [], confirmations: Object.fromEntries(approvals.map(key => [key, false])),
      agents: [], selectedAgent: null, preview: null, previewFor: '', startKey: '',
      busy: {}, readErrors: {}, writeError: '', notice: '', pending: null,
      runs: [], total: 0, offset: 0, selectedId: '', run: null,
      units: [], unitTotal: 0, unitOffset: 0, report: null, evidence: {},
      amend: {request_limit: '', token_limit: '', active_seconds_limit: ''},
      amendConfirmed: false, deleteConfirmed: false,
    });
    let live = true, generation = 0, timer = null;
    const owners = new Map();
    const draftIdentity = () => JSON.stringify({form: wa.form, order: wa.order});
    function cancel(scope) {
      const owner = owners.get(scope);
      if (owner) owner.abort.abort();
      owners.delete(scope);
      wa.busy[scope] = false;
    }
    function invalidate() {
      if (!live) return;
      cancel('preview');
      wa.preview = null; wa.previewFor = ''; wa.startKey = '';
      for (const key of approvals) wa.confirmations[key] = false;
    }
    const stopSource = Vue.watch(() => [wa.form.source_kind, wa.form.source_id], () => {
      wa.order = []; wa.chapters = [];
    }, {flush: 'sync'});
    const stopDraft = Vue.watch(draftIdentity, invalidate, {flush: 'sync'});
    const stopBudget = Vue.watch(() => JSON.stringify(wa.amend), () => { wa.amendConfirmed = false; }, {flush: 'sync'});
    function currentAgent() {
      const selected = wa.agents.find(row => String(row.agent_id) === String(wa.form.agent_id));
      if (selected) return selected;
      return wa.selectedAgent ? {...wa.selectedAgent, availability: 'unavailable', reason: '当前 Agent 已移除，请明确重选'} : null;
    }
    function chooseAgent(row) {
      if (!live || wa.pending || !row || row.kind !== 'agent' || row.availability !== 'ready') return false;
      wa.selectedAgent = copy(row); wa.form.agent_id = String(row.agent_id);
      return true;
    }
    async function read(scope, url, apply) {
      if (!live) return false;
      cancel(scope);
      const owner = {generation: ++generation, abort: new AbortController()};
      owners.set(scope, owner); wa.busy[scope] = true; wa.readErrors[scope] = '';
      try {
        const value = await send(url, {signal: owner.abort.signal});
        if (!live || owners.get(scope) !== owner) return false;
        apply(value);
        return true;
      } catch (error) {
        if (live && owners.get(scope) === owner && error.name !== 'AbortError') wa.readErrors[scope] = safeError(error);
        return false;
      } finally {
        if (live && owners.get(scope) === owner) { owners.delete(scope); wa.busy[scope] = false; }
      }
    }
    function payload() {
      const agent = currentAgent();
      if (!agent || agent.availability !== 'ready') throw new Error('请选择当前可用的 Agent');
      const selection = {source_kind: wa.form.source_kind,
        source_id: integer(wa.form.source_id, 1, Number.MAX_SAFE_INTEGER),
        publication_state: wa.form.publication_state};
      if (!['novel', 'series'].includes(selection.source_kind)) throw new Error('无效作品类型');
      if (!['unknown', 'ongoing', 'completed'].includes(selection.publication_state)) throw new Error('无效出版状态');
      if (wa.order.length) selection.ordered_novel_ids = wa.order.slice();
      return {selection, agent_id: integer(wa.form.agent_id, 1, Number.MAX_SAFE_INTEGER),
        output_tokens: integer(wa.form.output_tokens, 1, 1000000), budget: budget(wa.form.budget),
        confirmations: copy(wa.confirmations)};
    }
    async function reloadAgents() {
      return read('agents', '/api/dashboard/ai/agents', rows => {
        wa.agents = rows.map(row => ({kind: 'agent', agent_id: row.id, label: row.name,
          provider_name: 'Agent #' + row.id, availability: row.enabled ? 'ready' : 'disabled',
          reason: row.enabled ? '实际外发目标以本次预览为准' : 'Agent 已禁用'}));
        invalidate();
      });
    }
    async function previewWork() {
      if (!live || wa.pending) return false;
      invalidate();
      let submitted;
      try { submitted = payload(); }
      catch (error) { wa.writeError = error.message; return false; }
      const identity = draftIdentity();
      wa.writeError = '';
      const owner = {generation: ++generation, abort: new AbortController()};
      owners.set('preview', owner); wa.busy.preview = true; wa.readErrors.preview = '';
      try {
        const result = await send(BASE + '/preview', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(submitted), signal: owner.abort.signal});
        if (!live || owners.get('preview') !== owner || draftIdentity() !== identity) return false;
        for (const key of approvals) wa.confirmations[key] = false;
        wa.preview = result; wa.previewFor = identity; wa.chapters = result.manifest.chapters.slice();
        wa.startKey = '';
        return true;
      } catch (error) {
        if (live && owners.get('preview') === owner && error.name !== 'AbortError') wa.readErrors.preview = safeError(error);
        return false;
      } finally {
        if (live && owners.get('preview') === owner) { owners.delete('preview'); wa.busy.preview = false; }
      }
    }
    function moveChapter(index, direction) {
      if (wa.pending || !['series'].includes(wa.form.source_kind)) return false;
      const next = index + direction;
      if (next < 0 || next >= wa.chapters.length) return false;
      const chapters = wa.chapters.slice();
      chapters.splice(next, 0, chapters.splice(index, 1)[0]);
      wa.chapters = chapters; wa.order = chapters.map(row => row.novel_id);
      return true;
    }
    function canStart() {
      if (!live || wa.pending || wa.busy.preview || !wa.preview || !wa.preview.can_create || wa.previewFor !== draftIdentity()) return false;
      const agent = currentAgent();
      return !!agent && agent.availability === 'ready' && approvals.every(key => !wa.preview.confirmation_requirements[key] || wa.confirmations[key] === true);
    }
    function stopPolling() { if (timer !== null) root.clearTimeout(timer); timer = null; }
    function poll() {
      stopPolling();
      if (!live || root.document.hidden || !wa.run || wa.run.deleted || ['succeeded', 'failed', 'invalidated', 'cancelled'].includes(wa.run.status)) return;
      timer = root.setTimeout(async () => { timer = null; await refreshRun(); poll(); }, 2500);
    }
    function cancelRunReads() {
      for (const scope of Array.from(owners.keys())) if (!['agents', 'list', 'preview'].includes(scope)) cancel(scope);
      stopPolling();
    }
    async function loadRuns(offset = wa.offset) {
      return read('list', BASE + '?offset=' + offset + '&limit=20', data => {
        wa.runs = data.items; wa.total = data.total; wa.offset = data.offset;
      });
    }
    async function refreshRun() {
      if (!hash32(wa.selectedId)) return false;
      const id = wa.selectedId;
      const result = await read('run', BASE + '/' + id, data => {
        if (wa.selectedId === id && data.run_id === id && (!wa.run || data.version >= wa.run.version)) {
          wa.run = data;
          if (wa.pending && wa.pending.kind !== 'create' && wa.pending.runId === id &&
              data.version !== wa.pending.expectedVersion) wa.pending.canRetry = false;
          if (data.deleted) { wa.report = null; wa.evidence = {}; wa.units = []; }
        }
      });
      poll();
      return result;
    }
    async function selectRun(id) {
      if (!live || wa.pending || !hash32(id)) return false;
      cancelRunReads(); wa.selectedId = id; wa.run = null; wa.report = null;
      wa.units = []; wa.unitTotal = 0; wa.unitOffset = 0; wa.evidence = {};
      wa.amendConfirmed = false; wa.deleteConfirmed = false;
      const loaded = await refreshRun();
      if (loaded && wa.selectedId === id && wa.run && !wa.run.deleted) wa.amend = copy(wa.run.budget);
      return loaded;
    }
    async function loadUnits(offset = wa.unitOffset) {
      const id = wa.selectedId;
      if (!hash32(id)) return false;
      return read('units', BASE + '/' + id + '/units?offset=' + offset + '&limit=20', data => {
        if (wa.selectedId === id) { wa.units = data.items; wa.unitTotal = data.total; wa.unitOffset = data.offset; }
      });
    }
    async function loadReport() {
      const id = wa.selectedId;
      if (!wa.run || !wa.run.has_report || !hash32(id)) return false;
      wa.report = null; wa.evidence = {};
      return read('report', BASE + '/' + id + '/report', data => {
        if (wa.selectedId === id) { wa.report = data; wa.evidence = {}; }
      });
    }
    async function inspectEvidence(id) {
      const run = wa.selectedId;
      if (!hash32(run) || !hash64(id) || !wa.report || !wa.report.artifact.evidence[id]) return false;
      delete wa.evidence[id];
      return read('evidence:' + id, BASE + '/' + run + '/evidence/' + id, data => {
        if (wa.selectedId === run) wa.evidence[id] = {parts: partsFromEvidence(data), jump: data.jump};
      });
    }
    function controlAllowed(action) {
      if (!live || wa.pending || !wa.run || wa.run.deleted) return false;
      if (action === 'resume') return wa.run.can_resume === true;
      if (action === 'pause') return ['queued', 'running'].includes(wa.run.status) ||
        (wa.run.reason === 'sync_yield' && ['pausing', 'paused'].includes(wa.run.status));
      if (action === 'cancel') return ['queued', 'running', 'pausing', 'paused'].includes(wa.run.status);
      if (action === 'delete') return wa.deleteConfirmed;
      return action === 'budget' && wa.amendConfirmed;
    }
    async function refreshAfterWrite() {
      const results = await Promise.all([refreshRun(), loadRuns()]);
      if (live && results.some(value => !value)) wa.notice = '写入已确认，但刷新失败；请只重试读取，不要重复提交。';
    }
    function acceptWrite(operation, data) {
      wa.pending = null; wa.writeError = ''; wa.notice = '写入已确认。';
      cancelRunReads(); wa.selectedId = data.run_id; wa.run = data;
      wa.report = null; wa.evidence = {}; wa.units = [];
      if (operation.kind === 'create') invalidate();
      if (data.budget && !data.deleted) wa.amend = copy(data.budget);
      wa.amendConfirmed = false; wa.deleteConfirmed = false;
      poll();
    }
    async function sendPending() {
      const operation = wa.pending;
      if (!live || !operation || operation.sending) return false;
      cancel('reconcile');
      operation.sending = true; operation.phase = 'sending'; wa.writeError = '';
      try {
        const result = await send(operation.url, {method: operation.method, headers: {'Content-Type': 'application/json'},
          body: operation.body});
        if (!live || wa.pending !== operation) return false;
        acceptWrite(operation, result);
        await refreshAfterWrite();
        return true;
      } catch (error) {
        if (!live || wa.pending !== operation) return false;
        if (error.status == null || error.status >= 500) {
          operation.phase = 'unknown'; wa.writeError = '写入结果未知；保留原请求，请先核对或重试同一创建。';
        } else {
          wa.pending = null; wa.writeError = safeError(error);
          if (operation.kind === 'create') invalidate();
          else await refreshRun();
        }
        return false;
      } finally {
        if (live && wa.pending === operation) operation.sending = false;
      }
    }
    async function createWork() {
      if (!canStart()) { if (live) wa.writeError = '预览已失效或确认不完整，请重新预览并确认。'; return false; }
      let submitted;
      try { submitted = payload(); } catch (error) { wa.writeError = error.message; return false; }
      if (wa.previewFor !== draftIdentity()) return false;
      wa.startKey = wa.startKey || newKey();
      submitted.preview_fingerprint = wa.preview.preview_fingerprint; submitted.idempotency_key = wa.startKey;
      wa.pending = {kind: 'create', method: 'POST', url: BASE, body: JSON.stringify(submitted),
        phase: 'ready', sending: false, canRetry: true};
      return sendPending();
    }
    async function control(action) {
      if (!controlAllowed(action)) return false;
      const submitted = {expected_version: wa.run.version};
      if (action === 'budget') {
        try { submitted.budget = budget(wa.amend); }
        catch (error) { wa.writeError = error.message; return false; }
        submitted.budget_confirmed = true;
      }
      wa.pending = {kind: action, method: action === 'delete' ? 'DELETE' : 'POST',
        url: BASE + '/' + wa.run.run_id + (action === 'delete' ? '' : '/' + action),
        runId: wa.run.run_id, expectedVersion: wa.run.version, requestedBudget: submitted.budget || null,
        body: JSON.stringify(submitted), phase: 'ready', sending: false, canRetry: false};
      return sendPending();
    }
    async function reconcileWrite() {
      const operation = wa.pending;
      if (!live || !operation || operation.phase !== 'unknown' || operation.sending ||
          operation.kind === 'create' || wa.selectedId !== operation.runId) return false;
      operation.canRetry = false;
      return read('reconcile', BASE + '/' + operation.runId, data => {
        if (wa.pending !== operation || operation.phase !== 'unknown' || operation.sending ||
            wa.selectedId !== operation.runId || data.run_id !== operation.runId) return;
        const newest = wa.run && wa.run.run_id === operation.runId ? wa.run.version : operation.expectedVersion;
        if (data.version < Math.max(newest, operation.expectedVersion)) {
          wa.notice = '读取返回较旧版本；原写入结果仍未知，请重新核对。';
          return;
        }
        wa.run = data;
        const states = {pause: ['paused', 'pausing'], cancel: ['cancelled', 'cancelling'], resume: ['queued', 'running', 'succeeded']};
        const satisfied = operation.kind === 'delete' ? data.deleted === true : operation.kind === 'budget'
          ? !!data.budget && budgetKeys.every(key => data.budget[key] === operation.requestedBudget[key])
          : (states[operation.kind] || []).includes(data.status) &&
            (operation.kind !== 'pause' || data.reason === 'user_pause') &&
            (operation.kind !== 'cancel' || data.reason === 'user_cancel');
        if (data.version === operation.expectedVersion) {
          operation.canRetry = true; wa.notice = '读取确认版本未变化，可明确重试同一 CAS 请求。';
          return;
        }
        // A GET observes current state, never receipt/ownership of the lost command.
        cancelRunReads(); wa.pending = null;
        wa.report = null; wa.evidence = {}; wa.units = [];
        if (data.budget && !data.deleted) wa.amend = copy(data.budget);
        wa.amendConfirmed = false; wa.deleteConfirmed = false;
        wa.notice = satisfied ? '当前状态已核对；原写入结果仍未知，未重复提交。' : '';
        wa.writeError = satisfied ? '' : '状态已经变化；未确认原写入，也未重复提交，请按当前状态重新选择。';
        poll();
      });
    }
    async function retryUnknown() {
      const operation = wa.pending;
      if (!operation || operation.phase !== 'unknown' || !operation.canRetry) return false;
      if (operation.kind !== 'create' && (wa.busy.reconcile || wa.selectedId !== operation.runId ||
          !wa.run || wa.run.run_id !== operation.runId || wa.run.version !== operation.expectedVersion)) return false;
      return sendPending();
    }
    function evidenceHref(id) {
      const evidence = wa.report && wa.report.artifact.evidence[id];
      return evidence ? '/dashboard/novels/' + encodeURIComponent(evidence.novel_id) + '?analysis_id=' +
        encodeURIComponent(wa.selectedId) + '&evidence_id=' + encodeURIComponent(id) + '#work-analysis-evidence' : '#';
    }
    function dispose() {
      if (!live) return;
      live = false; stopPolling(); stopSource(); stopDraft(); stopBudget();
      for (const owner of owners.values()) owner.abort.abort();
      owners.clear();
    }
    function visibility() { if (root.document.hidden) stopPolling(); else if (live) refreshRun(); }
    function beforeUnload(event) { if (wa.pending) { event.preventDefault(); event.returnValue = ''; } }
    function suspend() { stopPolling(); for (const scope of Array.from(owners.keys())) cancel(scope); }
    function restore() { if (live) { reloadAgents(); loadRuns(); refreshRun(); } }
    async function mount() { await Promise.all([reloadAgents(), loadRuns()]); if (hash32(source.analysis_id)) await selectRun(source.analysis_id); }
    return {wa, currentAgent: Vue.computed(currentAgent), canStart: Vue.computed(canStart), chooseAgent,
      previewWork, createWork, moveChapter, reloadAgents, loadRuns, selectRun, refreshRun, loadUnits,
      loadReport, inspectEvidence, controlAllowed, control, retryUnknown, reconcileWrite, evidenceHref,
      usageText, coverageText, invalidate, mount, dispose, visibility, beforeUnload, suspend, restore};
  }
  function useReaderEvidence(Vue, novel, novelId) {
    const params = new URLSearchParams(root.location.search), run = params.get('analysis_id'), id = params.get('evidence_id');
    const state = Vue.reactive({active: !!(run || id), loading: false, error: '', parts: null, jump: null});
    let live = true, generation = 0, abort = null;
    const reload = async () => {
      if (!state.active || !novel.value || !live) return;
      const mine = ++generation;
      if (abort) abort.abort();
      abort = new AbortController(); state.parts = null; state.jump = null; state.error = '';
      if (!hash32(run) || !hash64(id)) { state.error = '证据链接无效。'; return; }
      state.loading = true;
      try {
        const value = await request(BASE + '/' + run + '/evidence/' + id, {signal: abort.signal});
        if (!live || mine !== generation) return;
        if (value.jump.novel_id !== novelId || novel.value.text_hash !== value.jump.text_hash) {
          state.error = '当前阅读正文与证据版本不同，不能把旧偏移应用到新正文。'; return;
        }
        state.parts = partsFromEvidence(value); state.jump = value.jump;
        await Vue.nextTick();
        if (live && mine === generation) {
          const panel = root.document.getElementById('work-analysis-evidence');
          if (panel) { panel.focus(); panel.scrollIntoView({block: 'start'}); }
        }
      } catch (error) {
        if (live && mine === generation && error.name !== 'AbortError') state.error = safeError(error);
      } finally { if (live && mine === generation) state.loading = false; }
    };
    const stop = Vue.watch(() => [novel.value && novel.value.novel_id, novel.value && novel.value.text_hash], reload, {immediate: true});
    const hide = event => {
      if (event.persisted) { ++generation; if (abort) abort.abort(); state.parts = null; state.loading = false; }
      else dispose();
    };
    const show = event => { if (event.persisted && live) reload(); };
    const dispose = () => {
      live = false; ++generation; stop(); if (abort) abort.abort();
      root.removeEventListener('pagehide', hide); root.removeEventListener('pageshow', show);
    };
    Vue.onUnmounted(dispose);
    root.addEventListener('pagehide', hide); root.addEventListener('pageshow', show);
    return state;
  }
  root.WorkAnalysis = {createController, useReaderEvidence, partsFromEvidence, request};
})(window);
```

Read errors and write results deliberately live in different fields. Calling
`createWork`, `control`, or `retryUnknown` directly still goes through the same
guards as the buttons. Unknown writes do not enable a second create or silently
replace the old idempotency key. The only timer is read-only status polling;
closing the page does not cancel, resume, or admit a backend task.

- [ ] **Step 4: Add the partial and wire only the selected existing-page view.**

Create `src/pixiv_novel_sync/templates/dashboard_work_analysis.html`:

```html
<div id="work-analysis" class="library-page max-w-7xl mx-auto space-y-4">
  <header class="library-page-header flex-col sm:flex-row">
    <div class="min-w-0">
      <h1 class="text-2xl font-bold">作品分析</h1>
      <p class="text-sm library-muted">手动选择当前归档版本，生成可核对的作品报告；不是个人喜好画像。</p>
    </div>
    <a href="/dashboard/preferences" class="library-btn">返回本地偏好与推荐</a>
  </header>
  <p v-if="wa.notice" role="status" class="library-card text-sm">{{ wa.notice }}</p>
  <p v-if="wa.writeError" role="alert" class="library-card text-sm text-red-700">{{ wa.writeError }}</p>
  <section v-if="wa.pending" class="library-card space-y-3" aria-label="待核对写入">
    <p>{{ wa.pending.phase === 'unknown' ? '写入结果未知，已保留原请求。不要另建重复任务。' : '正在提交；返回前不能确认远端调用已经结束。' }}</p>
    <button v-if="wa.pending.phase === 'unknown' && wa.pending.kind === 'create'" @click="retryUnknown" class="library-btn" :disabled="wa.pending.sending">重试同一创建</button>
    <button v-if="wa.pending.phase === 'unknown' && wa.pending.kind !== 'create'" @click="reconcileWrite" class="library-btn" :disabled="wa.busy.reconcile">先核对当前状态</button>
    <button v-if="wa.pending.phase === 'unknown' && wa.pending.kind !== 'create' && wa.pending.canRetry" @click="retryUnknown" class="library-btn">重试同一 CAS 请求</button>
    <p v-if="wa.readErrors.reconcile" role="alert">{{ wa.readErrors.reconcile }}</p>
  </section>
  <section class="library-card space-y-4" aria-label="选择与预算">
    <h2 class="library-section-title">1. 选择作品、Agent 与有限预算</h2>
    <fieldset :disabled="!!wa.pending" class="space-y-4">
      <div class="grid gap-3 md:grid-cols-2">
        <label class="block">作品类型
          <select id="wa-source-kind" v-model="wa.form.source_kind" class="library-input w-full">
            <option value="novel">单篇小说</option><option value="series">系列当前归档</option>
          </select>
        </label>
        <label class="block" for="wa-source-id">本地作品 ID
          <input id="wa-source-id" v-model="wa.form.source_id" inputmode="numeric" class="library-input w-full">
        </label>
        <label class="block">出版状态（仅记录本次用户声明）
          <select id="wa-publication" v-model="wa.form.publication_state" class="library-input w-full">
            <option value="unknown">未知</option><option value="ongoing">连载</option><option value="completed">我声明已完结</option>
          </select>
        </label>
        <label class="block" for="wa-output">单次输出预留（1–1000000 token）
          <input id="wa-output" v-model="wa.form.output_tokens" inputmode="numeric" class="library-input w-full">
        </label>
      </div>
      <ai-model-picker id="wa-agent" label="分析 Agent" :options="wa.agents" :current="currentAgent" :disabled="!!wa.pending" @select="chooseAgent"></ai-model-picker>
      <button class="library-btn" @click="reloadAgents" :disabled="wa.busy.agents || !!wa.pending">刷新 Agent</button>
      <p v-if="wa.readErrors.agents" role="alert">{{ wa.readErrors.agents }}</p>
      <p class="text-sm library-muted">不自动测试或探测 Provider。模型身份、容量与可能使用的服务以本次零网络预览为准。</p>
      <div class="grid gap-3 md:grid-cols-3">
        <label class="block" for="wa-budget-request">整部请求上限（1–512）<input id="wa-budget-request" v-model="wa.form.budget.request_limit" inputmode="numeric" class="library-input w-full"></label>
        <label class="block" for="wa-budget-token">计划 token 上限（1–100000000）<input id="wa-budget-token" v-model="wa.form.budget.token_limit" inputmode="numeric" class="library-input w-full"></label>
        <label class="block" for="wa-budget-time">活跃秒数上限（30–14400）<input id="wa-budget-time" v-model="wa.form.budget.active_seconds_limit" inputmode="numeric" class="library-input w-full"></label>
      </div>
      <p class="text-sm library-muted">预算没有默认授权值。请求重试、fallback 与显式恢复仍消耗预算；计划 token 不是厂商账单或金额保证。</p>
      <button id="wa-preview" class="library-btn library-btn-primary" @click="previewWork" :disabled="wa.busy.preview || !!wa.pending">{{ wa.busy.preview ? '正在本地预览…' : '零网络预览' }}</button>
      <p v-if="wa.readErrors.preview" role="alert">{{ wa.readErrors.preview }}</p>
    </fieldset>
  </section>
  <section v-if="wa.chapters.length" class="library-card space-y-3" aria-label="归档清单与章序">
    <h2 class="library-section-title">2. 当前归档清单与本次章序</h2>
    <p class="text-sm library-muted">发表时间仅提供建议顺序；短章不会省略，缺失/空白正文不会假算成已覆盖。重排不能丢章。</p>
    <ol class="space-y-3">
      <li v-for="(chapter, index) in wa.chapters" :key="chapter.novel_id" class="border rounded-lg p-3">
        <p class="break-words">{{ index + 1 }}. {{ chapter.title }} · ID {{ chapter.novel_id }}</p>
        <p class="text-xs library-muted">{{ chapter.has_text ? chapter.chars + ' code point' : '缺失或空白正文' }} · 获取时间 {{ chapter.fetched_at || '未知' }}</p>
        <div v-if="wa.form.source_kind === 'series'" class="flex gap-2 mt-2">
          <button class="library-btn" :disabled="index === 0 || !!wa.pending" @click="moveChapter(index, -1)" :aria-label="'上移第 ' + (index + 1) + ' 章'">上移</button>
          <button class="library-btn" :disabled="index === wa.chapters.length - 1 || !!wa.pending" @click="moveChapter(index, 1)" :aria-label="'下移第 ' + (index + 1) + ' 章'">下移</button>
        </div>
      </li>
    </ol>
    <p v-if="!wa.preview" role="status">目标、顺序、Agent、出版状态或预算已变化，旧预览失效，请重新预览。</p>
  </section>
  <section v-if="wa.preview" class="library-card space-y-4" aria-label="预览与外发确认">
    <h2 class="library-section-title">3. {{ wa.preview.manifest.title }} · 当前归档版本预览</h2>
    <p>方案：{{ wa.preview.plan.strategy === 'whole' ? '整部输入' : '按章或块提取，再分层汇总' }}；确定的原文单元 {{ wa.preview.plan.original_units.length }}；最少请求 {{ wa.preview.plan.minimum_requests }}。</p>
    <p class="text-sm library-muted">未来汇总大小、重试与实际用量尚未知；最少请求不是精确总调用数。原文单元主区间覆盖全部本次可用正文，不能以开头抽样替代。</p>
    <p class="text-sm">远端期望章数：{{ wa.preview.manifest.expected_count ?? '未知' }}；本地 {{ wa.preview.manifest.chapters.length }} 章；出版状态 {{ wa.preview.manifest.publication_state }}。没有已核实的远端完整清单，即使数量相同也不证明原作完整。</p>
    <ul v-if="wa.preview.warnings.length" class="space-y-2"><li v-for="warning in wa.preview.warnings" :key="warning" role="status">{{ warning }}</li></ul>
    <p v-if="wa.preview.warnings.includes('legacy_repreview_required')" role="status">此配置含旧版 Key 密文。执行可先完成本地加密升级，但配置变化后会停止并要求重新预览，不会静默外发。</p>
    <h3 class="font-bold">正文可能发送至以下已配置服务</h3>
    <ul class="space-y-2"><li v-for="target in wa.preview.targets" :key="JSON.stringify([target.provider_id, target.model_key])" class="break-all">{{ target.provider_name }} · <code>{{ target.model_key }}</code></li></ul>
    <p class="text-sm library-muted">这里的“可用”仅指本地配置/容量检查，不代表远端认证、连通性或实际账单已通过测试。</p>
    <fieldset :disabled="!!wa.pending" class="space-y-3">
      <label v-if="wa.preview.confirmation_requirements.order_confirmed" class="block"><input id="wa-confirm-order" type="checkbox" v-model="wa.confirmations.order_confirmed"> 我已核对本次章序，不修改全站系列排序</label>
      <label v-if="wa.preview.confirmation_requirements.current_archive_only" class="block"><input id="wa-confirm-archive" type="checkbox" v-model="wa.confirmations.current_archive_only"> 仅分析当前可用归档版本；缺章、空白、连载和未知完整性继续保留在报告中</label>
      <label class="block"><input id="wa-confirm-send" type="checkbox" v-model="wa.confirmations.external_send_confirmed"> 我授权仅此任务的正文发送到上述服务，并在本地保存结构报告与有界短引文</label>
      <label class="block"><input id="wa-confirm-budget" type="checkbox" v-model="wa.confirmations.budget_confirmed"> 我确认整部上限：{{ wa.preview.budget.request_limit }} 次请求、{{ wa.preview.budget.token_limit }} 计划 token、{{ wa.preview.budget.active_seconds_limit }} 活跃秒</label>
    </fieldset>
    <p class="text-xs library-muted">短章的合法引文可能覆盖整章；不保存完整输入重放副本不等于引文永不等于一篇短文。应用层删除不能保证备份、WAL 或介质立即安全擦除。</p>
    <button id="wa-create" class="library-btn library-btn-primary" :disabled="!canStart" @click="createWork">明确开始本次分析</button>
  </section>
  <section class="library-card space-y-3" aria-label="分析记录">
    <div class="flex flex-wrap items-center gap-3"><h2 class="library-section-title">分析记录</h2><button class="library-btn" @click="loadRuns()" :disabled="wa.busy.list">刷新记录</button></div>
    <p v-if="wa.readErrors.list" role="alert">{{ wa.readErrors.list }}</p>
    <ul class="space-y-2"><li v-for="run in wa.runs" :key="run.run_id"><button class="library-btn w-full text-left" :disabled="!!wa.pending" @click="selectRun(run.run_id)" :data-run-id="run.run_id">{{ run.deleted ? '已删除分析' : run.title }} · {{ run.status }}</button></li></ul>
    <div class="flex gap-3"><button class="library-btn" :disabled="wa.offset === 0 || wa.busy.list" @click="loadRuns(Math.max(0, wa.offset - 20))">上一页记录</button><button class="library-btn" :disabled="wa.offset + 20 >= wa.total || wa.busy.list" @click="loadRuns(wa.offset + 20)">下一页记录</button></div>
  </section>
  <section v-if="wa.selectedId" class="library-card space-y-4" aria-label="当前分析">
    <h2 class="library-section-title">当前分析</h2>
    <button class="library-btn" @click="refreshRun" :disabled="wa.busy.run">刷新状态</button>
    <p v-if="wa.readErrors.run" role="alert">{{ wa.readErrors.run }}</p>
    <template v-if="wa.run">
      <p id="wa-run-state">{{ wa.run.status }} · {{ wa.run.reason || '无停止原因' }}</p>
      <p v-if="wa.run.deleted">分析已删除，不能恢复；仅保留必要无正文审计。</p>
      <template v-else>
        <p>{{ wa.run.title }} · 版本 {{ wa.run.version }} · 已验证单元 {{ wa.run.unit_counts.succeeded }} / {{ wa.run.unit_counts.total }}</p>
        <p>{{ coverageText(wa.run.coverage) }}</p>
        <p>累计发送预留 {{ wa.run.request_count }} / {{ wa.run.budget.request_limit }}；计划 token {{ wa.run.planned_tokens }} / {{ wa.run.budget.token_limit }}；已扣活跃秒 {{ wa.run.active_seconds_debited }} / {{ wa.run.budget.active_seconds_limit }}。</p>
        <p data-usage>{{ usageText(wa.run.usage_summary) }}</p>
        <p v-if="['pausing', 'cancelling'].includes(wa.run.status)" role="status">正在等待当前调用真实退出；不保证远端已停止计算，不提前释放共享槽。</p>
        <p v-if="wa.run.reason === 'sync_yield'">为同步任务让位；同步空闲后可以继续同一已确认任务。</p>
        <p v-if="wa.run.reason === 'process_interrupted'">进程中断后已暂停，不会自动重发可能已付费请求；恢复保留累计预算。</p>
        <div class="flex flex-wrap gap-3">
          <button class="library-btn" :disabled="!controlAllowed('pause')" @click="control('pause')">暂停分析</button>
          <button class="library-btn" :disabled="!controlAllowed('cancel')" @click="control('cancel')">取消分析</button>
          <button class="library-btn" :disabled="!controlAllowed('resume')" @click="control('resume')">恢复未完成分析</button>
        </div>
        <details class="space-y-3"><summary class="font-bold">明确调整预算（不清零累计计数）</summary>
          <div class="grid gap-3 md:grid-cols-3">
            <label for="wa-amend-request">请求上限<input id="wa-amend-request" v-model="wa.amend.request_limit" inputmode="numeric" class="library-input w-full" :disabled="!!wa.pending"></label>
            <label for="wa-amend-token">计划 token 上限<input id="wa-amend-token" v-model="wa.amend.token_limit" inputmode="numeric" class="library-input w-full" :disabled="!!wa.pending"></label>
            <label for="wa-amend-time">活跃秒上限<input id="wa-amend-time" v-model="wa.amend.active_seconds_limit" inputmode="numeric" class="library-input w-full" :disabled="!!wa.pending"></label>
          </div>
          <label class="block"><input id="wa-confirm-amend" type="checkbox" v-model="wa.amendConfirmed" :disabled="!!wa.pending"> 确认新的有限上限；正文或配置失效不能靠增加预算恢复</label>
          <button class="library-btn" :disabled="!controlAllowed('budget')" @click="control('budget')">确认调整预算</button>
        </details>
        <button class="library-btn" @click="loadUnits(0)" :disabled="wa.busy.units">读取单元诊断</button>
        <p v-if="wa.readErrors.units" role="alert">{{ wa.readErrors.units }}</p>
        <ul class="space-y-2"><li v-for="unit in wa.units" :key="unit.unit_id" class="break-words">{{ unit.kind }} · {{ unit.status }} · 尝试 {{ unit.try_no }} · {{ unit.last_error || '无错误' }} · {{ unit.actual_target?.model_key || '尚未调用模型' }} · {{ usageText(unit.usage_summary) }}</li></ul>
        <div v-if="wa.unitTotal > 20" class="flex gap-3"><button class="library-btn" :disabled="wa.unitOffset === 0 || wa.busy.units" @click="loadUnits(Math.max(0, wa.unitOffset - 20))">上一页单元</button><button class="library-btn" :disabled="wa.unitOffset + 20 >= wa.unitTotal || wa.busy.units" @click="loadUnits(wa.unitOffset + 20)">下一页单元</button></div>
        <button class="library-btn library-btn-primary" @click="loadReport" :disabled="!wa.run.has_report || wa.busy.report">读取已验证报告</button>
        <p v-if="wa.readErrors.report" role="alert">{{ wa.readErrors.report }}</p>
        <details class="space-y-3"><summary class="font-bold">删除本分析</summary>
          <p class="text-sm">先撤销运行权，再清理结论、引文和恢复引用。应用层删除不保证备份、WAL 或存储介质立即安全擦除。</p>
          <label class="block"><input id="wa-confirm-delete" type="checkbox" v-model="wa.deleteConfirmed" :disabled="!!wa.pending"> 确认删除本分析的敏感业务产物</label>
          <button class="library-btn" :disabled="!controlAllowed('delete')" @click="control('delete')">删除分析</button>
        </details>
      </template>
    </template>
  </section>
  <section v-if="wa.report && wa.run && !wa.run.deleted" id="wa-report" class="library-card space-y-4" aria-label="已验证作品报告">
    <h2 class="library-section-title">当前归档版本分析</h2>
    <p v-if="wa.report.report_stale || wa.run.report_stale" role="status">原文已经更新，这是旧版本报告。短引文保留为历史证据，当前正文定位将重新核验 hash。</p>
    <p v-if="wa.report.archive_outdated || wa.run.archive_outdated" role="status">归档成员已变化；本报告只覆盖创建时已确认清单，不包含后来新增章节。</p>
    <p class="text-xs break-all">清单 {{ wa.report.manifest.manifest_fingerprint }}</p>
    <p>{{ coverageText(wa.report.coverage) }}</p>
    <p>原作完整性 {{ wa.report.coverage.source_completeness.status }}；出版状态 {{ wa.report.coverage.source_completeness.publication_state }}；声明来源 {{ wa.report.coverage.source_completeness.publication_source }}；汇总 {{ wa.report.synthesis_complete ? '已完成' : '未完成' }}。</p>
    <p class="whitespace-pre-wrap break-words">{{ wa.report.artifact.summary }}</p>
    <p v-if="wa.report.artifact.no_findings_reason" class="whitespace-pre-wrap">未找到足够依据：{{ wa.report.artifact.no_findings_reason }}</p>
    <article v-for="claim in wa.report.artifact.claims" :key="claim.claim_id" class="border rounded-lg p-3 space-y-2">
      <h3 class="font-bold break-words">{{ claim.label }} · {{ claim.dimension }}</h3>
      <p>{{ {direct: '直接文本依据', interpretation: '解释性判断', insufficient: '证据不足'}[claim.certainty] }}（不是统计概率）</p>
      <p class="whitespace-pre-wrap break-words">{{ claim.statement }}</p>
      <p class="text-xs">支撑章节：{{ Array.from(new Set(claim.evidence_ids.map(id => wa.report.artifact.evidence[id].novel_id))).join('、') }}</p>
      <details v-for="id in claim.evidence_ids" :key="id" :data-evidence-id="id" class="border rounded-lg p-3 space-y-2">
        <summary>展开短证据 · 章节 {{ wa.report.artifact.evidence[id].novel_id }}</summary>
        <blockquote class="whitespace-pre-wrap break-words">{{ wa.report.artifact.evidence[id].quote }}</blockquote>
        <p class="text-xs">服务端 code point 区间 [{{ wa.report.artifact.evidence[id].start }}, {{ wa.report.artifact.evidence[id].end }})</p>
        <div class="flex flex-wrap gap-3"><button class="library-btn" :disabled="wa.busy['evidence:' + id]" @click="inspectEvidence(id)">核对当前正文片段</button><a :href="evidenceHref(id)" class="library-btn">到阅读页核对同版本证据</a></div>
        <p v-if="wa.readErrors['evidence:' + id]" role="alert">{{ wa.readErrors['evidence:' + id] }}</p>
        <pre v-if="wa.evidence[id]" class="whitespace-pre-wrap break-words"><span>{{ wa.evidence[id].parts.before }}</span><mark>{{ wa.evidence[id].parts.quote }}</mark><span>{{ wa.evidence[id].parts.after }}</span></pre>
      </details>
    </article>
    <section class="space-y-2"><h3 class="font-bold">反例与矛盾</h3><article v-for="(item, index) in wa.report.artifact.counterexamples" :key="index" class="border rounded-lg p-3"><p>{{ item.statement }}</p><details v-for="id in item.evidence_ids" :key="id" :data-evidence-id="id"><summary>展开反例证据 · 章节 {{ wa.report.artifact.evidence[id].novel_id }}</summary><blockquote class="whitespace-pre-wrap break-words">{{ wa.report.artifact.evidence[id].quote }}</blockquote><a :href="evidenceHref(id)" class="library-btn">到阅读页核对同版本证据</a></details></article></section>
    <section class="space-y-2"><h3 class="font-bold">不确定性</h3><ul><li v-for="(item, index) in wa.report.artifact.uncertainties" :key="index" class="break-words">{{ item }}</li></ul></section>
    <p>{{ usageText(wa.report.usage_summary) }}</p>
    <ul><li v-for="unit in wa.report.units" :key="unit.unit_id" class="break-all">{{ unit.kind }} · {{ unit.actual_target?.provider_id }} / {{ unit.actual_target?.model_key || '未报告模型' }} · {{ usageText(unit.usage_summary) }}</li></ul>
  </section>
</div>
```

Replace only the preference-page route function body with:

```python
    @app.get("/dashboard/preferences")
    def dashboard_preferences_page():
        return render_template(
            "dashboard_preferences.html",
            work_analysis_view=request.args.get("view") == "work-analysis",
        )
```

Apply this precise wrapper edit in `dashboard_preferences.html`; the legacy
markup and setup between the anchors remain byte-for-byte unchanged:

```diff
 {% block content %}
+{% if work_analysis_view %}
+{% include "dashboard_work_analysis.html" %}
+{% else %}
 <div class="library-page pns-recommendations-page max-w-7xl mx-auto">
@@
     </details>
   </div>
 </div>
+{% endif %}
 {% endblock %}
@@
 {% block extra_scripts %}
+{% if work_analysis_view %}
+{% include "dashboard_work_analysis_scripts.html" %}
+{% else %}
 {% include "recommendation_components.html" %}
@@
 });
 </script>
+{% endif %}
 {% endblock %}
```

Create `src/pixiv_novel_sync/templates/dashboard_work_analysis_scripts.html`:

```html
{% include "dashboard_ai_controls.html" %}
<script nonce="{[ csp_nonce() ]}" src="/static/js/work_analysis.js"></script>
<script nonce="{[ csp_nonce() ]}">
window.initVueApp({setup() {
  const controller = window.WorkAnalysis.createController(Vue);
  const hide = event => event.persisted ? controller.suspend() : controller.dispose();
  const show = event => { if (event.persisted) controller.restore(); };
  Vue.onMounted(() => {
    controller.mount();
    document.addEventListener('visibilitychange', controller.visibility);
    window.addEventListener('pagehide', hide);
    window.addEventListener('pageshow', show);
    window.addEventListener('beforeunload', controller.beforeUnload);
  });
  Vue.onUnmounted(() => {
    controller.dispose();
    document.removeEventListener('visibilitychange', controller.visibility);
    window.removeEventListener('pagehide', hide);
    window.removeEventListener('pageshow', show);
    window.removeEventListener('beforeunload', controller.beforeUnload);
  });
  return controller;
}});
</script>
```

The extra script partial is deliberate: the existing recommendation VM reads
the legacy page source, while Jinja selects one real browser setup. Keep those
legacy tests unchanged; do not teach them to skip a newly broken setup or edit
their assertions. The rendered work view still has exactly one page mount.

In `ai_settings.js`, replace the complete `optionKey` function and the existing
search-fields expression, preserving exact opaque model keys:

```javascript
  function optionKey(o) {
    if (o.kind === 'agent') return JSON.stringify(['agent', o.agent_id]);
    return JSON.stringify(o.kind === 'pool' ? ['pool', o.pool_id]
      : ['model', o.provider_id, o.model_key]);
  }
```

```javascript
      [o.label, o.model_key, o.provider_name, o.provider_id, o.pool_id, o.agent_id, o.availability, o.reason]
```

Add this anchor immediately after the existing novel `<h1 class="novel-title">`
line; its server-rendered `novel_id` already exists in `webapp.py`:

```html
    <a href="/dashboard/preferences?view=work-analysis&amp;source_kind=novel&amp;source_id={[ novel_id ]}" class="library-btn">分析作品</a>
```

Add this anchor immediately after the series title `<h1>` line; its
server-rendered `series_id` already exists in `webapp.py`:

```html
        <a href="/dashboard/preferences?view=work-analysis&amp;source_kind=series&amp;source_id={[ series_id ]}" class="library-btn">分析作品</a>
```

- [ ] **Step 5: Add the same-hash reader panel without changing normal reading.**

Add `nt.text_hash,` between `nt.text_raw,` and `nt.text_markdown,` in
`NovelsMixin.get_novel_detail`'s existing SELECT. No new full-text getter or
duplicate body field is introduced.

```diff
                 nt.text_raw,
+                nt.text_hash,
                 nt.text_markdown,
```

In `dashboard_novel_detail.html`'s script block, insert the conditional bare
script before the existing inline setup; normal reading without these IDs loads
no new script and makes no evidence request:

```html
{% if request.args.get('analysis_id') or request.args.get('evidence_id') %}
<script nonce="{[ csp_nonce() ]}" src="/static/js/work_analysis.js"></script>
{% endif %}
```

Immediately after the existing `const novel = ref(null);`, insert:

```javascript
      const readerEvidence = window.WorkAnalysis
        ? window.WorkAnalysis.useReaderEvidence(Vue, novel, novelId) : null;
```

Add `readerEvidence,` to the existing setup return object before `novel,`.
Keep all other existing return fields; the precise insertion is:

```diff
       return {
+        readerEvidence,
         novel, loading, error, progressPercent, isScrolled, backHref, fontSizeLabel,
```

Between the error/loading branches and the reader content, place the following
inside the existing `<template v-else>` before the novel header:

```html
  <section v-if="readerEvidence && readerEvidence.active" id="work-analysis-evidence" tabindex="-1" class="library-card max-w-4xl mx-auto space-y-3" aria-label="同版本证据定位">
    <h2 class="library-section-title">核对同版本正文证据</h2>
    <p v-if="readerEvidence.loading" role="status">正在认证并重新核验正文 hash…</p>
    <p v-if="readerEvidence.error" role="alert">{{ readerEvidence.error }}</p>
    <template v-if="readerEvidence.parts">
      <p class="text-sm">当前正文 hash 匹配；服务端 code point 区间 [{{ readerEvidence.jump.start }}, {{ readerEvidence.jump.end }})。</p>
      <pre class="whitespace-pre-wrap break-words"><span>{{ readerEvidence.parts.before }}</span><mark>{{ readerEvidence.parts.quote }}</mark><span>{{ readerEvidence.parts.after }}</span></pre>
      <p class="text-xs library-muted">这是同版本正文的有界定位片段；没有把原文偏移套到格式化 HTML，也没有用 UTF-16 下标重算全书位置。</p>
    </template>
  </section>
```

The `#work-analysis-evidence` fragment prevents the existing saved-scroll
restoration from overriding this explicit navigation. No read-status or
preference-inference feature is added. The backend still refuses stale hashes;
the reader separately compares the newly loaded novel hash before displaying a
precise-location claim.

- [ ] **Step 6: Extend only the existing exact-name script cache policy.**

Change the existing Nginx location declaration to:

```nginx
    location ~ ^/static/js/(ai_settings|ai_picker|ai_provider_onboarding|ai_preflight|ai_job_diagnostics|work_analysis)\.js$ {
```

Keep its whole current body unchanged: `proxy_cache off`, hidden upstream
Cache-Control, `private, no-store, no-cache, max-age=0, must-revalidate`, HSTS,
and trusted forwarding headers. Do not broaden it to all `.js` or add a
query-only cache workaround. This is packaging the newly introduced bare script,
not reopening the previously completed static-cache remediation.

- [ ] **Step 7: Run UI integration and method/lifecycle GREEN.**

Run `python -m pytest tests/test_work_analysis_ui.py tests/test_work_analysis_runtime.py tests/test_deployment_config.py tests/test_ai_settings_runtime.py tests/test_ai_model_ui.py tests/test_reader_actions_runtime.py tests/test_mobile_reading.py tests/test_mobile_recommendations.py tests/test_preferences.py tests/test_preference_csrf.py -q`.
Expected: all selected integration/regression tests pass. A Node runtime test is
supplementary JavaScript coverage only; it is not evidence of a browser pass.

- [ ] **Step 8: Rebuild generated CSS with an existing offline CLI, then verify it.**

Use only an existing Tailwind 3.4.17 executable. On the Windows worktree:

```powershell
$tailwindCli = $env:TAILWINDCSS_BIN
if ([string]::IsNullOrWhiteSpace($tailwindCli)) {
    $tailwindCli = Join-Path $HOME '.cache/pixiv-novel-sync/tailwindcss-3.4.17/tailwindcss-windows-x64.exe'
}
if (-not (Test-Path -LiteralPath $tailwindCli -PathType Leaf)) {
    throw 'Existing offline Tailwind 3.4.17 CLI unavailable; do not download during this gate.'
}
python scripts/build_tailwind_css.py --cli "$tailwindCli" --check
```

If the check reports the generated CSS is stale, run this existing generator
and repeat the check. Its two generated outputs are the only permitted CSS
changes; never hand-edit `tailwind.css` or invent a fixed cache version:

```powershell
python scripts/build_tailwind_css.py --cli "$tailwindCli"
python scripts/build_tailwind_css.py --cli "$tailwindCli" --check
python -m pytest tests/test_tailwind_static_css.py tests/test_frontend_library_os.py -q
```

Expected after generation, or immediately when no generation was needed:
the check exits 0 and the static/style regressions pass. Keep missing CLI
prerequisites explicit; do not silently take the builder's network-download path.

- [ ] **Step 9: Run the actual offline Chromium matrix and inspect its artifacts.**

```powershell
$env:AI_UI_VUE_JS = (Resolve-Path -LiteralPath '.superpowers/sdd/vendor/vue-3.4.38.global.prod.js').Path
python -m pytest tests/test_work_analysis_browser.py tests/test_ai_settings_browser.py -q -rs
```

Expected: the selected suites run, with all work-analysis cases at 375, 768,
and 1440 pixels passing and no non-whitelisted request, page error, or new
sensitive storage entry. The local Vue bytes must match the SRI in the actual
base template. A skipped browser suite remains blocked and cannot satisfy W15.
No assertions, console traces, or screenshots are pre-labelled successful in
this plan; record real test outcomes at implementation time.

Inspect the generated screenshots under
`.superpowers/sdd/phase2-browser/touch-375/`, `touch-768/`, and `touch-1440/`:
creation from a series, reconciled unknown creation, cancelled checkpoint,
collapsed report evidence, and the reader's bounded same-hash highlight. Verify
the real picker stays inside the viewport, focused controls remain visible,
long literal text wraps, consent is readable, and the pending-state text never
claims that remote computation has stopped. Check request traces contain only
synthetic IDs/budget metadata, that preview is the only POST before consent,
and that no no-view preference page contacts the new API. The touch emulation
is not physical-device/soft-keyboard acceptance. The synthetic control server
is not proof of actual shared-worker cancellation or paid-provider behavior;
Tasks 4-6/9 own those separate regressions.

- [ ] **Step 10: Build an offline wheel from a short isolated staging directory.**

Run this PowerShell block. It copies only source/build inputs into a newly
created short temporary directory, keeps build output below the worktree's
ignored verification directory, and checks the ZIP members against the actual
source bytes. It neither installs nor contacts a package index; the temporary
build inputs remain available for investigation rather than being recursively
deleted by a computed shell path.

```powershell
@'
from pathlib import Path
from zipfile import ZipFile
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

root = Path.cwd().resolve()
output_root = root / ".superpowers/verification/work-analysis-ui"
output_root.mkdir(parents=True, exist_ok=True)
assert output_root.resolve().is_relative_to(root)
output = Path(tempfile.mkdtemp(prefix="wheel-", dir=output_root))
scratch = Path(tempfile.mkdtemp(prefix="wa-ui-"))
stage = scratch / "source"
stage.mkdir()
shutil.copytree(root / "src", stage / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
for name in ("pyproject.toml", "README.md", "LICENSE"):
    shutil.copy2(root / name, stage / name)
assets = [
    "pixiv_novel_sync/static/js/work_analysis.js",
    "pixiv_novel_sync/static/js/ai_settings.js",
    "pixiv_novel_sync/templates/dashboard_work_analysis.html",
    "pixiv_novel_sync/templates/dashboard_work_analysis_scripts.html",
    "pixiv_novel_sync/templates/dashboard_preferences.html",
    "pixiv_novel_sync/templates/dashboard_novel_detail.html",
    "pixiv_novel_sync/templates/dashboard_series_detail.html",
    "pixiv_novel_sync/templates/base.html",
    "pixiv_novel_sync/static/css/tailwind.css",
]
before = {name: hashlib.sha256((root / "src" / name).read_bytes()).hexdigest() for name in assets}
env = dict(os.environ)
env.update(TEMP=str(scratch), TMP=str(scratch), PIP_DISABLE_PIP_VERSION_CHECK="1")
command = [sys.executable, "-m", "pip", "wheel", "--no-index", "--no-deps", "--no-build-isolation",
           "--no-cache-dir", "--wheel-dir", str(output), str(stage)]
result = subprocess.run(command, cwd=stage, env=env, capture_output=True, text=True, encoding="utf-8", timeout=180)
(output / "build.log").write_text(result.stdout + result.stderr, encoding="utf-8")
assert result.returncode == 0, result.stdout + result.stderr
wheels = list(output.glob("pixiv_novel_sync-*.whl"))
assert len(wheels) == 1
with ZipFile(wheels[0]) as archive:
    for name in assets:
        assert name in archive.namelist(), name
        assert archive.read(name) == (root / "src" / name).read_bytes(), name
after = {name: hashlib.sha256((root / "src" / name).read_bytes()).hexdigest() for name in assets}
assert after == before, "The isolated wheel build changed checkout assets"
record = {"command": command, "exit_code": result.returncode, "wheel": str(wheels[0]),
          "wheel_sha256": hashlib.sha256(wheels[0].read_bytes()).hexdigest(),
          "verified_member_sha256": after, "scratch": str(scratch)}
(output / "verification.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
print(json.dumps(record, indent=2))
'@ | python -
```

Expected: one built wheel, byte-equal copies of all listed packaged assets,
unchanged checkout assets, and the real command/hash record saved in the
verification directory. Missing offline build dependencies are a blocked build
gate, not authority to install or assert success. Task 9 repeats the complete
Windows/canonical-LF Linux integration/build matrix; this is the scoped UI
packaging check.

- [ ] **Step 11: Review the UI boundary, then commit only the task's files.**

The task reviewer checks actual rendered templates and request traces, not
only source-string assertions: exact Agent-kind identity without altered model
keys; immutable consent fingerprint; method-entry and late-response ownership;
same-key unknown-create replay; CAS-only unknown-control retry; no blind POST
after confirmed success; separate input coverage/source completeness/synthesis;
`is_final`-appropriate report structure; unknown versus zero use; source-hash
reader guard with bounded code-point slicing; escaped, initially folded quotes;
tombstone clearing; CSP nonce and exact-name no-store rule; preserved no-view
preference, old reader/progress, old main-continue, and package behavior.

Before this gate, rerun the scoped suite from Step 7 and verify all new test
files remain present. Stop for defects or unexplained skips; do not weaken old
tests or substitute this plan for execution evidence. Once approved in the
implementation workflow, stage only these paths:

```text
git add src/pixiv_novel_sync/static/js/work_analysis.js src/pixiv_novel_sync/static/js/ai_settings.js src/pixiv_novel_sync/templates/dashboard_work_analysis.html src/pixiv_novel_sync/templates/dashboard_work_analysis_scripts.html src/pixiv_novel_sync/templates/dashboard_preferences.html src/pixiv_novel_sync/templates/dashboard_novel_detail.html src/pixiv_novel_sync/templates/dashboard_series_detail.html src/pixiv_novel_sync/preference_web.py src/pixiv_novel_sync/storage/novels.py config/nginx/pixiv-novel-sync.conf tests/test_deployment_config.py tests/test_work_analysis_ui.py tests/test_work_analysis_runtime.py tests/test_work_analysis_runtime.cjs tests/work_analysis_browser_helpers.py tests/test_work_analysis_browser.py
```

If Step 8 actually regenerated the CSS/hash, additionally stage its exact two
generated files, not unrelated base-template edits:

```text
git add src/pixiv_novel_sync/static/css/tailwind.css src/pixiv_novel_sync/templates/base.html
```

```text
git diff --cached --check
git diff --cached --stat
git commit -m "feat: add explicit work analysis UI and same-version evidence"
```

Review the staged diff for only this task's approved changes before committing;
if another change shares one of these paths, use a reviewed selective stage
rather than including or reverting it. Never stage the ignored draft fragments,
local Vue vendor file, screenshots, request traces, temporary build inputs, or
wheel outputs. This plan authorizes none of the actual implementation, tests,
commits, network access, push, SSH, or deployment during the planning turn.
