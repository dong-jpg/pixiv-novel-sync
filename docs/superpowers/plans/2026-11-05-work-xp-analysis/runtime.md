# Tasks 4-6: Parent Invocation, Execution And Shared Admission

> Planning artifact only, based on the approved Phase 2 specification. Isolated snippet validation does not implement or authorize this feature. Tasks 4-6 consume Tasks 1-3 of this implementation plan. Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` when implementation is separately authorized.

**Goal:** Bind paid calls to durable parent accounting, execute the frozen analysis graph, and share the existing Web execution slot without weakening cancellation or retention.

**Architecture:** Extend the existing text invocation and its existing durable request reservation transaction. Business workers run only through the existing Web `JobRunner`; the coordinator schedules intentions, not a second worker pool. Parent records remain the authoritative business result after crashes.

**Tech Stack:** Existing Python, SQLite, pytest, Flask, `JobManager`, `JobRunner`, and AI router/provider adapters; no added runtime dependency.

## Interface Contract

- The existing router type is `RouteRequest`, not `RouteExecutionRequest` (`ai/model_router.py:143`). Add optional server-only `work_binding` there and in `RouteJobContext`; all existing callers default to `None`.
- Task 1 must define the pure configuration helper before Task 3 imports it. Its agreed public internal shape is `schema_version`, `agent_id`, `output_tokens`, `configuration_fingerprint`, `allowed_provider_ids`, `legacy_provider_ids`. It hashes the saved Agent prompt independently; the legacy `_agent_config_hash` does not include it.
- `recover_work_analyses(*, now, process_start=False)` distinguishes sole-owner startup recovery from an ordinary expiry check. Startup passes `True` and pauses even queued, unexpired pre-crash intentions; neither path activates a paid task.
- `claim_work_analysis(..., shared_job_id=None, shared_log_id=None)` atomically records the actual shared activation and exact optional task-log row. The worker supplies `context["job_id"]` and `context["job"].progress.get("log_id")`; these are trusted runner metadata, never client input. Sole-owner startup orders `fail_stale_task_logs()`, conservative parent recovery, then `reconcile_work_analysis_task_logs()` before admission opens.
- `get_work_analysis_control(run_id)` supplies only the private owner/epoch, version, stop state, identity fingerprints, and budget ceilings needed for a bounded same-owner internal `sync_yield` CAS retry. Public stop/resume requests remain single-attempt strict CAS operations.
- `request_work_analysis_stop` accepts `pause`, `cancel`, `sync_yield`, and `delete`. Normal stop keeps the epoch until its one time settlement. Deletion and source invalidation revoke immediately and conservatively retain the charged time slice.
- Coordinator constructor adds keyword-only `db_factory` and `scheduler`; the existing four positional arguments do not supply either dependency. The `run_shared` callback launches an already admitted job using the Web application's existing thread-launch function.
- `RouteResult.work_error_code: str | None = None` is a fixed safe code, never a Provider error body. It distinguishes parent admission refusal from normal route failure without permitting another fallback.

### Task 4: Bind AI Children, Request Reservations, And Usage To A Parent

**Files:**
- Modify: `src/pixiv_novel_sync/storage/ai/core.py` (`create_ai_job:775`, `claim_ai_job_network_request:1373`, `record_ai_job_request_usage:1434`).
- Modify: `src/pixiv_novel_sync/ai/services/core.py` (`RouteJobContext:53`, `_start_route_job:189`, `_stream_route:431`, `invoke_text:552`, `_invoke_text:575`).
- Modify: `src/pixiv_novel_sync/ai/model_router.py` (`RouteRequest:143`, `RouteResult:164`, `_start_heartbeat:1163`, `_claim_network_request:1306`, `execute_stream` request guard and terminal handlers).
- Modify: `src/pixiv_novel_sync/ai/providers.py` (`_before_network_request:457`).
- Modify tests: `tests/test_ai_job_routing_storage.py`, `tests/test_ai_invocation_runtime.py`, `tests/test_ai_service_facade.py`.

**Interfaces:**
- Consumes `WorkLease`, `WorkCallBinding`, `WorkAnalysisError`, and `work_configuration_identity(conn, agent_id, output_tokens)` from Task 1; `_attach_work_analysis_child`, `_reserve_work_analysis_request`, `_record_work_analysis_usage`, and `heartbeat_work_analysis` from Task 3.
- Produces the backward-compatible `invoke_text(..., work_binding: WorkCallBinding | None = None)`, `RouteRequest.work_binding`, `RouteJobContext.work_binding`, and `RouteResult.work_error_code`.
- `planned_tokens` is the selected v2 candidate's `input_tokens + message_overhead + safety_margin + output_reserve`. The existing planner stores the raw estimate separately from overhead and margin (`invocation_planner.py:255-265`); do not omit those two terms or combine different candidates' estimates.
- All parent attachment and reservation callbacks run inside the existing child transaction, not in an outer transaction. Ordinal zero represents attachment only; it is not a network request or reported usage.

- [ ] **Step 4.1: Append failing atomic-bridge tests to `tests/test_ai_job_routing_storage.py`.**

These tests use that file's existing `db`, `create_owned_job`, and `valid_attempt_data` helpers. The probe table is a synthetic transaction witness, not another product table.

```python
def test_work_child_attachment_rolls_back_with_child_insert(db, monkeypatch):
    from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError, WorkCallBinding, WorkLease

    binding = WorkCallBinding(WorkLease("a" * 32, 1, "work-owner"), "b" * 64, 1, "c" * 64)
    db.conn.execute("CREATE TABLE bridge_probe (value TEXT PRIMARY KEY)")
    db.conn.commit()

    def deny_attach(conn, received, child_job_id):
        assert conn.in_transaction and received == binding and child_job_id == "bound"
        conn.execute("INSERT INTO bridge_probe VALUES ('attachment')")
        raise WorkAnalysisError("owner_lost", 409)

    monkeypatch.setattr(db, "_attach_work_analysis_child", deny_attach)
    with pytest.raises(WorkAnalysisError, match="owner_lost"):
        db.create_ai_job(
            "bound", "general", None, {"business_type": "work_analysis"},
            owner_token="child-owner", retention_policy="metadata_only", work_binding=binding,
        )
    assert db.conn.execute("SELECT COUNT(*) FROM ai_jobs WHERE job_id='bound'").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM bridge_probe").fetchone()[0] == 0


def test_work_reservation_failure_rolls_back_parent_child_and_unknown_usage(db, monkeypatch):
    from pixiv_novel_sync.storage.ai.core import AIWorkAdmissionDenied
    from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError, WorkCallBinding, WorkLease

    create_owned_job(db)
    attempt = db.allocate_ai_model_attempt("job", "owner", valid_attempt_data())
    binding = WorkCallBinding(WorkLease("a" * 32, 1, "work-owner"), "b" * 64, 1, "c" * 64)
    db.conn.execute("CREATE TABLE bridge_probe (value TEXT PRIMARY KEY)")
    db.conn.commit()

    def deny_reservation(conn, received, child_job_id, ordinal, planned_tokens, *, now):
        assert conn.in_transaction and received == binding
        assert (child_job_id, ordinal, planned_tokens) == ("job", 1, 371)
        conn.execute("INSERT INTO bridge_probe VALUES ('reservation')")
        raise WorkAnalysisError("budget_exhausted", 409)

    monkeypatch.setattr(db, "_reserve_work_analysis_request", deny_reservation)
    with pytest.raises(AIWorkAdmissionDenied) as caught:
        db.claim_ai_job_network_request(
            "job", "owner", attempt, work_binding=binding, planned_tokens=371,
        )
    assert caught.value.code == "budget_exhausted"
    assert db.get_ai_job("job")["network_request_count"] == 0
    envelope = db.list_ai_job_model_attempts("job")[0]["usage"]
    assert envelope is None or envelope["requests"] == []
    assert db.conn.execute("SELECT COUNT(*) FROM bridge_probe").fetchone()[0] == 0


def test_work_usage_projection_and_child_usage_share_one_transaction(db, monkeypatch):
    create_owned_job(db)
    attempt = db.allocate_ai_model_attempt("job", "owner", valid_attempt_data())
    ordinal = db.claim_ai_job_network_request("job", "owner", attempt)
    before = db.list_ai_job_model_attempts("job")[0]["usage"]

    def reject_projection(conn, child_job_id, request_ordinal, usage):
        assert conn.in_transaction and (child_job_id, request_ordinal) == ("job", ordinal)
        assert usage["total_tokens"] == 12
        raise sqlite3.OperationalError("synthetic projection failure")

    monkeypatch.setattr(db, "_record_work_analysis_usage", reject_projection)
    with pytest.raises(sqlite3.OperationalError, match="synthetic projection"):
        db.record_ai_job_request_usage(
            "job", "owner", attempt, ordinal,
            {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
             "complete": True, "missing_reason": None},
        )
    assert db.list_ai_job_model_attempts("job")[0]["usage"] == before
```

- [ ] **Step 4.2: Run the red atomic-bridge tests.**

Run: `python -m pytest tests/test_ai_job_routing_storage.py -q -k "work_child_attachment or work_reservation_failure or work_usage_projection"`

Expected before implementation: failure on the missing `work_binding` keyword or missing `AIWorkAdmissionDenied`; the third test must fail because the parent projection is not yet called. Do not count import/fixture errors unrelated to those missing interfaces as the intended red result.

- [ ] **Step 4.3: Implement the three in-transaction storage hooks.**

In `storage/ai/core.py`, add these imports next to the existing AI usage import. Task 3's methods import configuration helpers locally so this contracts-only import creates no storage/router cycle.

```python
from ...work_analysis.contracts import WorkAnalysisError, WorkCallBinding
```

Add this complete exception after `AIRouteBudgetExhausted`. A `RuntimeError`, rather than `AIProviderError` or `AIJobConflictError`, deliberately bypasses candidate fallback and the generic lost-child terminal-race shortcut.

```python
class AIWorkAdmissionDenied(RuntimeError):
    def __init__(self, code: str) -> None:
        allowed = {
            "budget_exhausted", "active_time_exhausted", "owner_lost", "lease_expired",
            "source_changed", "source_deleted", "configuration_changed",
            "legacy_repreview_required", "work_stopping", "work_deleted",
            "state_conflict", "version_conflict", "invalidated",
        }
        self.code = code if code in allowed else "work_stopping"
        super().__init__(self.code)
```

In `create_ai_job`, append this exact keyword-only parameter after `validation_status`:

```python
        work_binding: WorkCallBinding | None = None,
```

Immediately after its retention-policy selection and before `json.dumps`, add:

```python
        if work_binding is not None:
            if not isinstance(work_binding, WorkCallBinding) or retention_policy != "metadata_only":
                raise ValueError("work binding requires metadata-only retention")
            if parent_job_id is not None or idempotency_key is not None:
                raise ValueError("work binding cannot use legacy continuation")
```

Immediately after the existing `INSERT INTO ai_jobs` call, still inside `with self.transaction() as conn`, add:

```python
            if work_binding is not None:
                self._attach_work_analysis_child(conn, work_binding, job_id)
```

Replace the complete `claim_ai_job_network_request` function with:

```python
    def claim_ai_job_network_request(
        self,
        job_id: str,
        owner_token: str,
        attempt_index: int | None = None,
        *,
        work_binding: WorkCallBinding | None = None,
        planned_tokens: int | None = None,
    ) -> int:
        if attempt_index is not None:
            if type(attempt_index) is not int or attempt_index < 0:
                raise ValueError("invalid attempt index")
            if self.conn.in_transaction:
                raise AIJobConflictError("request reservation requires its own durable transaction")
        if work_binding is not None:
            if not isinstance(work_binding, WorkCallBinding) or attempt_index is None:
                raise ValueError("work binding requires a typed attempted invocation")
            if type(planned_tokens) is not int or planned_tokens <= 0:
                raise ValueError("invalid work planned tokens")
        elif planned_tokens is not None:
            raise ValueError("work planned tokens require a binding")
        try:
            with self.transaction() as conn:
                job = conn.execute(
                    "SELECT status, owner_token, network_request_count, cancel_requested, route_deadline_at "
                    "FROM ai_jobs WHERE job_id = ?", (job_id,),
                ).fetchone()
                if job is None or job["status"] != "running" or job["owner_token"] != owner_token:
                    raise AIJobConflictError("AI job ended or ownership changed")
                attached = conn.execute(
                    "SELECT 1 FROM work_analysis_requests WHERE child_job_id=? AND request_ordinal=0",
                    (job_id,),
                ).fetchone()
                if attached is not None and work_binding is None:
                    raise AIWorkAdmissionDenied("owner_lost")
                current = int(job["network_request_count"] or 0)
                if current >= 32:
                    raise AIRouteBudgetExhausted("network request limit reached")
                next_count = current + 1
                now = _utc_now()
                if attempt_index is not None:
                    if job["cancel_requested"]:
                        raise AIJobConflictError("AI job cancellation was requested")
                    if job["route_deadline_at"] and job["route_deadline_at"] <= _sql_timestamp(now):
                        raise AIRouteBudgetExhausted("AI job route deadline has expired")
                    attempt = conn.execute(
                        "SELECT usage_json FROM ai_job_model_attempts "
                        "WHERE job_id=? AND attempt_index=? AND status='running' AND owner_token=?",
                        (job_id, attempt_index, owner_token),
                    ).fetchone()
                    if attempt is None:
                        raise AIJobConflictError("AI attempt is missing, ended or owned by another worker")
                    usage = _decode_usage(attempt["usage_json"])
                    if any(record["request_ordinal"] >= next_count for record in usage["requests"]):
                        raise ValueError("usage reservation disagrees with job counter")
                    usage["requests"].append({"request_ordinal": next_count, **ProviderUsage().to_dict()})
                    serialized_usage = _encode_usage(usage)
                if work_binding is not None:
                    self._reserve_work_analysis_request(
                        conn, work_binding, job_id, next_count, planned_tokens, now=now,
                    )
                conn.execute(
                    "UPDATE ai_jobs SET network_request_count=? "
                    "WHERE job_id=? AND status='running' AND owner_token=?",
                    (next_count, job_id, owner_token),
                )
                if attempt_index is not None:
                    conn.execute(
                        "UPDATE ai_job_model_attempts SET usage_json=? "
                        "WHERE job_id=? AND attempt_index=? AND owner_token=?",
                        (serialized_usage, job_id, attempt_index, owner_token),
                    )
            return next_count
        except WorkAnalysisError as error:
            raise AIWorkAdmissionDenied(error.code) from None
```

In `record_ai_job_request_usage`, insert this line immediately before the `return True` inside the matching-ordinal loop, outside the `if serialized != row["usage_json"]` branch. Repeated identical reports must still repair/check the idempotent projection in the same transaction.

```python
                self._record_work_analysis_usage(conn, job_id, request_ordinal, merged)
```

The Task 3 helper returns without writing when no business request row exists; old AI callers do not acquire parent behavior. It merges normalized counters on an existing `(child_job_id, request_ordinal)` row and never refunds request or estimated-token debits.

- [ ] **Step 4.4: Run the storage bridge tests and existing retention/accounting tests.**

Run: `python -m pytest tests/test_ai_job_routing_storage.py tests/test_ai_usage.py -q`

Expected: all tests pass, including the three new rollback tests; no change to `metadata_only`, `keyword_compat`, legacy continuation, or ordinary request limits.

- [ ] **Step 4.5: Append failing no-fallback and candidate-estimate tests to `tests/test_ai_invocation_runtime.py`.**

The file's existing synthetic router fixtures and `collect_result` return `(chunks, result)`. These tests isolate the router handoff from Task 3's separate parent-state tests.

```python
def test_parent_admission_denial_is_terminal_without_candidate_fallback(db, route_router, registry, monkeypatch):
    from pixiv_novel_sync.storage.ai.core import AIWorkAdmissionDenied
    from pixiv_novel_sync.work_analysis.contracts import WorkCallBinding, WorkLease

    agent, providers, models, pool = pool_setup(db)
    request = make_route_request(db, route_router, agent, retention_policy="metadata_only")
    binding = WorkCallBinding(WorkLease("a" * 32, 1, "parent-owner"), "b" * 64, 1, "c" * 64)
    request = replace(request, work_binding=binding)
    reservations = []

    def reject(self, job_id, owner_token, attempt_index=None, *, work_binding=None, planned_tokens=None):
        reservations.append((work_binding, planned_tokens))
        raise AIWorkAdmissionDenied("budget_exhausted")

    monkeypatch.setattr(Database, "claim_ai_job_network_request", reject)
    monkeypatch.setattr(Database, "heartbeat_work_analysis", lambda self, lease, *, now: True)
    chunks, result = collect_result(route_router.execute_stream(request))
    assert len(reservations) == 1 and reservations[0][0] == binding
    budget = request.candidate_snapshot.plan.eligible[0]
    assert reservations[0][1] == (
        budget.input_tokens + budget.message_overhead + budget.safety_margin + budget.output_reserve
    )
    assert registry.network_calls == []
    assert len(registry.calls) == 1
    assert result.work_error_code == "budget_exhausted"
    assert result.finish_state == "failed_before_output"
    assert db.get_ai_job(request.job_id)["status"] == "failed"
    assert all(item["status"] != "running" for item in db.list_ai_job_model_attempts(request.job_id))
    assert not any(chunk.type == "progress" and chunk.data.get("action") == "switch" for chunk in chunks)


def test_unbound_invocation_preserves_default_binding_and_no_parent_callbacks(db, route_router, fixed_agent, monkeypatch):
    from dataclasses import fields

    binding_field = next(item for item in fields(RouteRequest) if item.name == "work_binding")
    assert binding_field.default is None
    request = make_route_request(db, route_router, fixed_agent)
    monkeypatch.setattr(Database, "_reserve_work_analysis_request", forbidden)
    monkeypatch.setattr(Database, "heartbeat_work_analysis", forbidden)
    chunks, result = collect_result(route_router.execute_stream(request))
    assert result.finish_state == "succeeded"
    assert result.work_error_code is None


def test_post_reservation_best_effort_cancel_keeps_authorized_debit():
    from pixiv_novel_sync.ai.providers import _before_network_request

    state = {"cancelled": False, "reservations": 0}

    def reserve():
        state["reservations"] += 1
        state["cancelled"] = True
        return 1

    with pytest.raises(AIProviderError) as caught:
        _before_network_request(reserve, lambda: state["cancelled"])
    assert caught.value.category == "cancelled"
    assert state["reservations"] == 1
```

- [ ] **Step 4.6: Run the red router tests.**

Run: `python -m pytest tests/test_ai_invocation_runtime.py -q -k "parent_admission or default_binding or post_reservation"`

Expected before implementation: missing dataclass field/terminal error code, or the post-reservation cancellation test does not raise. The tests intentionally do not assert that cancellation can undo an already committed send authorization.

- [ ] **Step 4.7: Implement the server-only router handoff and terminal denial.**

In `ai/model_router.py`, extend its `dataclasses` import and storage import, then add the contracts import:

```python
from dataclasses import asdict, dataclass, replace
from ..storage.ai.core import AIJobConflictError, AIRouteBudgetExhausted, AIWorkAdmissionDenied
from ..work_analysis.contracts import WorkCallBinding
```

Append to `RouteRequest` after `retention_policy`:

```python
    work_binding: WorkCallBinding | None = None
```

Append to `RouteResult` after `job_status`:

```python
    work_error_code: str | None = None
```

Replace `_claim_network_request` with the following complete function. The candidate is taken from the already validated frozen v2 chain, not from a new planning pass.

```python
    def _claim_network_request(
        self, request: RouteRequest, attempt_index: int, candidate: ModelCandidate,
    ) -> int:
        if self._request_cancelled(request):
            raise AIProviderError(
                "AI request cancelled", category="cancelled", scope="model", finish_reason="cancelled",
            )
        db = self._db_factory()
        try:
            self._active_route_state(db, request)
            try:
                if request.work_binding is None:
                    return db.claim_ai_job_network_request(request.job_id, request.owner_token, attempt_index)
                if not isinstance(request.candidate_snapshot, PlannedCandidateSnapshot):
                    raise AIWorkAdmissionDenied("configuration_changed")
                self._validate_invocation_request(request)
                budget = request.candidate_snapshot.plan.eligible[candidate.candidate_index]
                if (budget.reason is not None or type(budget.input_tokens) is not int
                        or budget.profile.provider_id != candidate.provider_id
                        or budget.profile.model_key != candidate.model_key):
                    raise AIWorkAdmissionDenied("configuration_changed")
                return db.claim_ai_job_network_request(
                    request.job_id, request.owner_token, attempt_index,
                    work_binding=request.work_binding,
                    planned_tokens=(budget.input_tokens + budget.message_overhead
                                    + budget.safety_margin + budget.output_reserve),
                )
            except AIJobConflictError:
                state = db.get_ai_job_route_state(request.job_id, request.owner_token)
                if state is not None and state.get("status") == "running" and state.get("cancel_requested"):
                    raise AIProviderError(
                        "AI request cancelled", category="cancelled", scope="model", finish_reason="cancelled",
                    ) from None
                raise
        finally:
            db.close()
```

Replace the nested `request_guard` definition in `execute_stream` with:

```python
                    def request_guard(
                        _attempt_index: int = attempt_index, _candidate: ModelCandidate = candidate,
                    ) -> int:
                        return self._claim_network_request(request, _attempt_index, _candidate)
```

Insert this outer `except` immediately before the existing outer `except AIRouteBudgetExhausted` (the one following exhaustion of the candidate loop, originally line 1941). It is not inside the candidate-loop `AIProviderError` handler.

```python
        except AIWorkAdmissionDenied as error:
            if current_iterator is not None:
                close = getattr(current_iterator, "close", None)
                if callable(close):
                    close()
                current_iterator = None
            partial = request.stage == "main" and current_output_started
            if current_attempt is not None:
                db.finish_ai_model_attempt(
                    request.job_id, current_attempt, request.owner_token,
                    "partial" if partial else "failed", error_scope="model",
                    error_message="Work analysis admission denied", error_category="route_budget_exhausted",
                    finish_reason="error", output_started=current_output_started,
                    latency_ms=max(0, int((time.monotonic() - started_at) * 1000))
                    if started_at is not None else 0,
                )
                current_attempt = None
            db.finish_ai_job_cas(
                request.job_id, request.owner_token, "partial" if partial else "failed",
                error_message="Work analysis admission denied", error_category="route_budget_exhausted",
            )
            return replace(
                self._terminal_race_result(db, request, memory_output=""),
                work_error_code=error.code,
            )
```

In the existing `_start_heartbeat` worker, insert this block immediately before `if db.ai_job_should_stop(request.job_id)`. It reuses the existing heartbeat thread; it does not create another execution or heartbeat pool.

```python
                    if request.work_binding is not None and not db.heartbeat_work_analysis(
                        request.work_binding.lease, now=_utc_now(),
                    ):
                        cancel_event.set()
                        return
```

Replace `providers._before_network_request` with:

```python
def _before_network_request(
    request_guard: Callable[[], int | None] | None,
    is_cancelled: Callable[[], bool] | None,
    deadline: float | None = None,
) -> int | None:
    _check_cancelled(is_cancelled, deadline)
    ordinal = request_guard() if request_guard is not None else None
    _check_cancelled(is_cancelled, deadline)
    return ordinal
```

The second cancellation check is best effort after authorization. A cancel can still race between that check and the physical socket send. The durable ordinal is never refunded merely because this check raises or the process crashes before a response.

- [ ] **Step 4.8: Run router and provider regressions.**

Run: `python -m pytest tests/test_ai_invocation_runtime.py tests/test_ai_generation_deadlines.py tests/test_ai_provider_completion.py tests/test_ai_providers_fallback.py tests/test_ai_provider_probe.py -q`

Expected: pass; parent admission denial produces one terminal failure, not another fallback. Existing per-child 16-candidate, 32-request, 30-minute limits and publication boundaries remain enforced.

- [ ] **Step 4.9: Append the failing typed-service and legacy-upgrade tests to `tests/test_ai_service_facade.py`.**

```python
def test_work_binding_crosses_real_text_facade_without_body_retention(invocation_env, monkeypatch):
    from pixiv_novel_sync.work_analysis.configuration import work_configuration_identity
    from pixiv_novel_sync.work_analysis.contracts import WorkCallBinding, WorkLease

    env = invocation_env
    identity = work_configuration_identity(env.db.conn, env.agent_id, PAYLOAD["max_tokens"])
    binding = WorkCallBinding(
        WorkLease("a" * 32, 1, "parent-owner"), "b" * 64, 1, identity["configuration_fingerprint"],
    )
    attached, reservations = [], []

    def attach(self, conn, received, child_job_id):
        assert conn.in_transaction
        attached.append((received, child_job_id))

    def reserve(self, conn, received, child_job_id, ordinal, planned_tokens, *, now):
        assert conn.in_transaction
        reservations.append((received, child_job_id, ordinal, planned_tokens))

    monkeypatch.setattr(Database, "_attach_work_analysis_child", attach)
    monkeypatch.setattr(Database, "_reserve_work_analysis_request", reserve)
    monkeypatch.setattr(Database, "heartbeat_work_analysis", lambda self, lease, *, now: True)
    result = env.service.invoke_text(env.agent_id, PAYLOAD, work_binding=binding)
    assert result.finish_state == "succeeded"
    assert attached == [(binding, result.job_id)]
    assert len(reservations) == 1 and reservations[0][:3] == (binding, result.job_id, 1)
    assert reservations[0][3] > PAYLOAD["max_tokens"]
    assert env.provider.calls == 1
    row = dict(env.db.conn.execute("SELECT * FROM ai_jobs WHERE job_id=?", (result.job_id,)).fetchone())
    assert row["output_text"] is None and row["output_json"] is None
    assert "INPUT_SENTINEL" not in json.dumps(row)
    assert "OUTPUT_SENTINEL" not in json.dumps(row)
    assert inspect.signature(env.service.invoke_text).parameters["work_binding"].default is None


def test_work_legacy_upgrade_requires_explicit_repreview_before_send(invocation_env, monkeypatch):
    from pixiv_novel_sync.work_analysis.configuration import work_configuration_identity
    from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError, WorkCallBinding, WorkLease

    env = invocation_env
    identity = work_configuration_identity(env.db.conn, env.agent_id, PAYLOAD["max_tokens"])
    binding = WorkCallBinding(
        WorkLease("a" * 32, 1, "parent-owner"), "b" * 64, 1, identity["configuration_fingerprint"],
    )
    monkeypatch.setattr(Database, "_attach_work_analysis_child", lambda self, conn, received, child: None)
    monkeypatch.setattr(env.service.secret_manager, "is_legacy_ciphertext", lambda value: value == "synthetic-cipher")
    monkeypatch.setattr(env.service.secret_manager, "decrypt", lambda value: "synthetic-key")
    monkeypatch.setattr(env.service.secret_manager, "encrypt", lambda value: "v2$synthetic-upgrade", raising=False)
    with pytest.raises(WorkAnalysisError) as caught:
        env.service.invoke_text(env.agent_id, PAYLOAD, work_binding=binding)
    assert caught.value.code == "configuration_changed"
    assert env.provider.calls == 0
    provider = env.db.get_ai_provider(env.provider_id, include_secret=True)
    assert provider["api_key_encrypted"] == "v2$synthetic-upgrade"
    assert env.db.conn.execute("SELECT SUM(network_request_count) FROM ai_jobs").fetchone()[0] == 0
    assert all(row["status"] == "failed" for row in env.db.list_ai_jobs()["items"])
```

- [ ] **Step 4.10: Run the red facade tests.**

Run: `python -m pytest tests/test_ai_service_facade.py -q -k "work_binding or work_legacy_upgrade"`

Expected before forwarding implementation: `invoke_text` rejects the new keyword, or legacy upgrade is not followed by the required bound-identity check.

- [ ] **Step 4.11: Forward the binding through every service layer and reject changed consent identity.**

Add the following imports to `ai/services/core.py`:

```python
from ...work_analysis.configuration import work_configuration_identity
from ...work_analysis.contracts import WorkAnalysisError, WorkCallBinding
```

Append to `RouteJobContext` after `retention_policy`:

```python
    work_binding: WorkCallBinding | None = None
```

Append this keyword-only parameter to `_start_route_job`, `invoke_text`, and `_invoke_text` (after each function's current last keyword parameter):

```python
        work_binding: WorkCallBinding | None = None,
```

Inside `_start_route_job`, immediately after reading `_ROUTE_RESUME_CONTEXT`, insert:

```python
        if work_binding is not None:
            if (not isinstance(work_binding, WorkCallBinding) or prepared_resume is not None
                    or invocation is None or retention_policy != "metadata_only"):
                raise WorkAnalysisError("configuration_changed", 409)
            current_identity = work_configuration_identity(db.conn, agent.id, invocation.max_tokens)
            if current_identity["configuration_fingerprint"] != work_binding.configuration_fingerprint:
                raise WorkAnalysisError("configuration_changed", 409)
```

In that function's `db.create_ai_job` call, add this keyword expansion after `validation_status`:

```python
                **({"work_binding": work_binding} if work_binding is not None else {}),
```

Immediately after `candidate_snapshot = self.model_router.resolve_invocation(agent, invocation)` inside its invocation branch, insert:

```python
                    if work_binding is not None:
                        current_identity = work_configuration_identity(db.conn, agent.id, invocation.max_tokens)
                        if current_identity["configuration_fingerprint"] != work_binding.configuration_fingerprint:
                            raise WorkAnalysisError("configuration_changed", 409)
```

This second check occurs after the router's authorized self-owned legacy encryption preparation and v2 freeze, but before the frozen snapshot can authorize a send. A changed ciphertext fingerprint, even when the service itself upgraded it, invalidates this parent. The application does not pretend the old confirmation authorized a different frozen identity. A new preview and explicit new parent are required; preview itself never attempts the upgrade.

In `_start_route_job`'s `except Exception as error`, immediately after `db.finish_ai_job_cas(...)` and before the `metadata_only` generic exception conversion, insert:

```python
            if work_binding is not None and isinstance(error, WorkAnalysisError):
                raise
```

In its `return RouteJobContext(...)`, add:

```python
            work_binding=work_binding,
```

In `_stream_route`'s `RouteRequest(...)`, add:

```python
            work_binding=context.work_binding,
```

In `invoke_text`'s `_invoke_text(...)`, add:

```python
            work_binding=work_binding,
```

In `_invoke_text`'s `_start_route_job(...)`, add:

```python
                work_binding=work_binding,
```

Change `_invoke_text`'s outer `except Exception:` to `except Exception as error:`; immediately before its final `if retention_policy == "metadata_only": raise AIServiceError(...)`, insert:

```python
            if work_binding is not None and isinstance(error, WorkAnalysisError):
                raise
```

No client JSON accepts `work_binding`; `normalize_text_invocation` continues to reject unknown keys. No code changes `_build_metadata_audit` to include raw text, business artifacts, parent owner tokens, or credentials.

- [ ] **Step 4.12: Run all focused facade, routing, and retention tests.**

Run: `python -m pytest tests/test_ai_service_facade.py tests/test_ai_invocation_runtime.py tests/test_ai_job_routing_storage.py tests/test_ai_usage.py tests/test_ai_keyword_clean_routing.py tests/test_ai_invocation_snapshots.py -q`

Expected: pass. Review both binding-absent and binding-present paths. The legacy test must observe a real local ciphertext upgrade followed by zero reserved/sent requests, not a mocked success response.

- [ ] **Step 4.13: Review the send boundary and commit only the Task 4 paths.**

Review gate: (1) child association is durable before any network ordinal; (2) the parent and child reservation roll back together; (3) every retry, empty-stream retry, and fallback still uses `request_guard`; (4) a parent refusal cannot enter another candidate; (5) attachment rows never enter usage totals; (6) ordinary callers and `metadata_only` tests remain unchanged.

```powershell
git add src/pixiv_novel_sync/storage/ai/core.py src/pixiv_novel_sync/ai/services/core.py src/pixiv_novel_sync/ai/model_router.py src/pixiv_novel_sync/ai/providers.py tests/test_ai_job_routing_storage.py tests/test_ai_invocation_runtime.py tests/test_ai_service_facade.py
git commit -m "feat: bind work analysis calls to atomic parent accounting"
```

## Task 1 Prerequisite Location

The complete prerequisite now lives in [configuration.md](configuration.md) and
must run as Task 1C before storage Task 3. Tasks 4-6 consume that completed code.

### Task 5: Execute Frozen Units, Reuse Checkpoints, And Publish The Validated Root

**Files:**
- Create: `src/pixiv_novel_sync/work_analysis/executor.py`.
- Modify: `src/pixiv_novel_sync/jobs/models.py` (`JobType:30`, one new enum member).
- Create tests: `tests/work_analysis_runtime_helpers.py`, `tests/test_work_analysis_executor.py`.

**Interfaces:**
- Consumes `snapshot_work_configuration`, `read_source_texts`, `materialize_unit`, `plan_reduction_level`, `validate_unit_output`, `validate_artifact`, and Task 3's claimed-owner/checkpoint/graph/publish/settlement methods.
- Produces `WorkAnalysisExecutor(db_factory, ai_factory).run(run_id, context) -> dict`; `context` is the existing `JobRunner` context with `manager`, `job`, `job_id`, and `claim_finalization`. The exact log ID comes from that job's existing `progress["log_id"]`, when present; the runner context has no separate `log_id` key.
- The private storage view contains decoded `manifest`, `plan`, `configuration`, `units`, `slice_deadline_at`, and safe state fields. Each entry in `units` is `{unit, status, try_no, child_job_id, artifact, actual_target, usage_summary}`. Only succeeded artifacts may become reducer input.
- `publish_work_analysis(lease, report)` takes the validated final **Artifact**, not an arbitrary full report envelope. Task 3 compares it with the stored final-root checkpoint, constructs the server report/usage/coverage envelope, and commits the report and success state atomically.
- Every unit, including `is_final=True`, remains at most 128 KiB. Only the server-built final report envelope gets the 1 MiB bound.

- [ ] **Step 5.1: Add the synthetic real-database/fake-Provider fixture and failing executor tests.**

Create `tests/work_analysis_runtime_helpers.py` with this complete helper. It creates a database under pytest's temporary directory, not the configured production path. All candidate planning, child creation, reservation, usage, and storage validation remain real; only the paid transport is replaced.

```python
from __future__ import annotations

import hashlib
import json
import uuid
from types import SimpleNamespace

from pixiv_novel_sync.ai.models import AIStreamChunk
from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.work_analysis.configuration import snapshot_work_configuration
from pixiv_novel_sync.work_analysis.manifest import capture_manifest, preview_fingerprint
from pixiv_novel_sync.work_analysis.planning import plan_original_units


class SyntheticSecrets:
    def is_legacy_ciphertext(self, ciphertext):
        return False

    def decrypt(self, ciphertext):
        if ciphertext != "v2$synthetic-only":
            raise AssertionError("unexpected fixture ciphertext")
        return "synthetic-only-key"


class SyntheticWorkProvider:
    def __init__(self, db_factory):
        self.db_factory = db_factory
        self.sent_units = []
        self.bad_calls = set()
        self.before_response = None

    def stream_generate(
        self, messages, model, temperature, top_p, max_tokens, *,
        request_guard=None, is_cancelled=None, on_usage=None, deadline=None,
    ):
        ordinal = request_guard()
        db = self.db_factory()
        try:
            row = db.conn.execute(
                "SELECT r.unit_id FROM work_analysis_requests r JOIN ai_jobs j ON j.job_id=r.child_job_id "
                "WHERE r.entry_kind='attachment' AND j.status='running' ORDER BY j.id DESC LIMIT 1",
            ).fetchone()
            if row is None:
                raise AssertionError("child must already have a durable parent attachment")
            unit_id = row["unit_id"]
        finally:
            db.close()
        self.sent_units.append(unit_id)
        if self.before_response is not None:
            self.before_response(unit_id)
        raw = json.dumps({
            "unit_id": unit_id, "summary": "No supported findings in this synthetic test.",
            "no_findings_reason": "The fixture intentionally makes no content claims.",
            "claims": [], "counterexamples": [], "uncertainties": ["Interpretation is not guaranteed."],
        })
        if len(self.sent_units) in self.bad_calls:
            raw = "{synthetic-invalid-json"
        yield AIStreamChunk(type="delta", text=raw)
        if on_usage is not None:
            on_usage(ordinal, {
                "input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
                "complete": True, "missing_reason": None,
            })
        yield AIStreamChunk(type="done", data={"finish_reason": "stop"})


def make_runtime_environment(tmp_path, monkeypatch, *, texts=None, context_window=100000, request_limit=64):
    import requests

    def forbidden_network(*args, **kwargs):
        raise AssertionError("real transport is forbidden in default work-analysis tests")

    monkeypatch.setattr(requests.Session, "request", forbidden_network)
    path = tmp_path / "work-runtime.db"
    db = Database(path)
    db.init_schema()
    bodies = list(texts or ["synthetic opening and ending"])
    with db.transaction() as conn:
        conn.execute("INSERT INTO users(user_id,name,raw_json) VALUES(1,'Synthetic author','{}')")
        conn.execute("INSERT INTO series(series_id,title,user_id,total_novels) VALUES(10,'Synthetic series',1,?)", (len(bodies),))
        for index, body in enumerate(bodies, 1):
            conn.execute(
                "INSERT INTO novels(novel_id,user_id,series_id,title,visible,restrict_value,x_restrict,"
                "text_length,total_bookmarks,total_views,tags_json,raw_json,meta_hash,create_date) "
                "VALUES(?,1,10,?,1,'public',0,?,0,0,'[]','{}',?,?)",
                (index, f"Synthetic chapter {index}", len(body), "0" * 64, f"2026-01-{index:02d}T00:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO novel_texts(novel_id,text_raw,text_hash,has_content) VALUES(?,?,?,1)",
                (index, body, hashlib.sha256(body.encode("utf-8")).hexdigest()),
            )
    provider_id = db.create_ai_provider({
        "name": "Synthetic provider", "provider_type": "openai_compatible", "enabled": True,
        "base_url": "https://provider.invalid/v1", "api_key_encrypted": "v2$synthetic-only",
        "default_model": "synthetic-model", "context_window": context_window,
    })
    agent_id = db.create_ai_agent({
        "name": "Synthetic agent", "task_type": "general", "provider_id": provider_id,
        "model": "synthetic-model", "system_prompt": "Use only supplied synthetic evidence.",
        "context_window": context_window, "max_tokens": 128,
    })
    selection = {
        "source_kind": "series", "source_id": 10,
        "ordered_novel_ids": list(range(1, len(bodies) + 1)), "publication_state": "completed",
    }
    budget = {"request_limit": request_limit, "token_limit": 1_000_000, "active_seconds_limit": 600}
    confirmations = {
        "order_confirmed": True, "current_archive_only": True,
        "external_send_confirmed": True, "budget_confirmed": True,
    }
    with db.read_transaction():
        manifest, captured = capture_manifest(db.conn, selection)
        configuration, prompt, preflight = snapshot_work_configuration(db.conn, agent_id, 128)
    plan = plan_original_units(manifest, captured, preflight, agent_prompt=prompt, output_tokens=128)
    preview = preview_fingerprint(selection, manifest, plan, configuration, budget)
    run = db.create_work_analysis(
        selection, manifest, plan, configuration, budget, confirmations, uuid.uuid4().hex, preview,
    )
    db_factory = lambda: Database(path)
    service = AIWritingService(path, secret_manager=SyntheticSecrets())
    provider = SyntheticWorkProvider(db_factory)
    monkeypatch.setattr(service.model_router, "_get_provider", lambda config: provider)

    def close():
        service.close()
        db.close()

    return SimpleNamespace(
        db=db, db_factory=db_factory, service=service, provider=provider,
        agent_id=agent_id, provider_id=provider_id, run_id=run["run_id"],
        manifest=manifest, plan=plan, close=close,
    )
```

Create `tests/test_work_analysis_executor.py`:

```python
import hashlib

import pytest

from pixiv_novel_sync.jobs.manager import JobManager
from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobStatus, JobType
from pixiv_novel_sync.jobs.runner import JobRunner
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError
from pixiv_novel_sync.work_analysis.executor import WorkAnalysisExecutor
from work_analysis_runtime_helpers import make_runtime_environment


def run_environment(env):
    manager = JobManager()
    job = manager.submit(JobSpec(
        JobSource.WEB, ["work_analysis"], JobType.WORK_ANALYSIS, {"run_id": env.run_id},
    ))
    executor = WorkAnalysisExecutor(env.db_factory, lambda: env.service)
    JobRunner(manager, lambda task, context: executor.run(env.run_id, context)).run(job.job_id)
    return manager, job


def test_whole_path_publishes_one_validated_root_and_one_paid_call(tmp_path, monkeypatch):
    env = make_runtime_environment(tmp_path, monkeypatch)
    try:
        manager, job = run_environment(env)
        report = env.db.get_work_analysis_report(env.run_id)
        assert job.status == JobStatus.SUCCEEDED
        assert len(env.provider.sent_units) == 1
        assert report["artifact"]["is_final"] is True
        assert report["coverage"]["complete"] is True
        assert report["coverage"]["ratio"] == 1
        assert report["synthesis_complete"] is True
        assert report["usage_summary"]["network_request_count"] == 1
        row = env.db.conn.execute("SELECT output_text,output_json FROM ai_jobs").fetchone()
        assert tuple(row) == (None, None)
    finally:
        env.close()


def test_segmented_resume_reuses_validated_units_and_persisted_graph(tmp_path, monkeypatch):
    env = make_runtime_environment(
        tmp_path, monkeypatch, texts=[("chapter A\n" * 350), ("chapter B\n" * 350)], context_window=2500,
    )
    try:
        assert env.plan["strategy"] == "segmented"
        assert len(env.plan["original_units"]) >= 2
        env.provider.bad_calls = {2}
        manager, failed_job = run_environment(env)
        failed = env.db.get_work_analysis(env.run_id, include_private=True)
        assert failed["status"] == "failed"
        first = env.provider.sent_units[0]
        assert next(row for row in failed["units"] if row["unit"]["unit_id"] == first)["status"] == "succeeded"
        assert failed["report"] is None
        env.db.resume_work_analysis(env.run_id, failed["version"])
        env.provider.bad_calls.clear()
        manager, completed_job = run_environment(env)
        assert completed_job.status == JobStatus.SUCCEEDED
        assert env.provider.sent_units.count(first) == 1
        completed = env.db.get_work_analysis(env.run_id, include_private=True)
        roots = [row for row in completed["units"] if row["unit"]["is_final"]]
        assert len(roots) == 1 and roots[0]["status"] == "succeeded"
        assert any(row["unit"]["kind"] == "reduce" for row in completed["units"])
        assert env.db.get_work_analysis_report(env.run_id)["synthesis_complete"] is True
    finally:
        env.close()


def test_shared_finalization_claim_precedes_sensitive_publish(tmp_path, monkeypatch):
    env = make_runtime_environment(tmp_path, monkeypatch)
    manager = JobManager()
    job = manager.submit(JobSpec(JobSource.WEB, ["work_analysis"], JobType.WORK_ANALYSIS, {"run_id": env.run_id}))
    original = Database.publish_work_analysis
    claims_seen = []

    def checked_publish(self, lease, report):
        claims_seen.append(manager.request_cancel(job.job_id))
        return original(self, lease, report)

    monkeypatch.setattr(Database, "publish_work_analysis", checked_publish)
    try:
        executor = WorkAnalysisExecutor(env.db_factory, lambda: env.service)
        JobRunner(manager, lambda task, context: executor.run(env.run_id, context)).run(job.job_id)
        assert claims_seen == [False]
        assert job.status == JobStatus.SUCCEEDED
    finally:
        env.close()


def test_source_change_still_wins_after_memory_finalization_claim(tmp_path, monkeypatch):
    env = make_runtime_environment(tmp_path, monkeypatch)
    original = Database.publish_work_analysis

    def change_source_then_publish(self, lease, report):
        writer = env.db_factory()
        try:
            body = "replacement synthetic body"
            with writer.transaction() as conn:
                conn.execute("UPDATE novel_texts SET text_raw=?,text_hash=? WHERE novel_id=1",
                             (body, hashlib.sha256(body.encode("utf-8")).hexdigest()))
        finally:
            writer.close()
        return original(self, lease, report)

    monkeypatch.setattr(Database, "publish_work_analysis", change_source_then_publish)
    try:
        manager, job = run_environment(env)
        state = env.db.get_work_analysis(env.run_id, include_private=True)
        assert state["status"] == "invalidated"
        assert state["report"] is None
        assert job.status != JobStatus.SUCCEEDED
        assert manager.acquire_run_slot() is True
        manager.release_run_slot()
    finally:
        env.close()


def test_exhausted_failed_run_refuses_resume_without_a_second_send(tmp_path, monkeypatch):
    env = make_runtime_environment(tmp_path, monkeypatch, request_limit=1)
    env.provider.bad_calls = {1}
    try:
        run_environment(env)
        state = env.db.get_work_analysis(env.run_id)
        assert state["status"] == "failed"
        with pytest.raises(WorkAnalysisError) as caught:
            env.db.resume_work_analysis(env.run_id, state["version"])
        assert caught.value.code == "budget_exhausted"
        after = env.db.get_work_analysis(env.run_id)
        assert after["status"] == "failed" and after["version"] == state["version"]
        assert len(env.provider.sent_units) == 1
        assert after["request_count"] == 1
    finally:
        env.close()


def test_segmented_executor_budget_refusal_pauses_without_a_second_send(tmp_path, monkeypatch):
    env = make_runtime_environment(
        tmp_path, monkeypatch, texts=[("chapter A\n" * 350), ("chapter B\n" * 350)],
        context_window=2500, request_limit=1,
    )
    try:
        assert env.plan["strategy"] == "segmented" and len(env.plan["original_units"]) >= 2
        manager, job = run_environment(env)
        state = env.db.get_work_analysis(env.run_id, include_private=True)
        assert state["status"] == "paused" and state["reason"] == "budget_exhausted"
        assert job.status == JobStatus.CANCELLED
        assert len(env.provider.sent_units) == state["request_count"] == 1
        assert sum(item["status"] == "succeeded" for item in state["units"]) == 1
        assert state["report"] is None
    finally:
        env.close()
```

- [ ] **Step 5.2: Run the red executor suite.**

Run: `python -m pytest tests/test_work_analysis_executor.py -q`

Expected before implementation: the executor module/new job type is missing. After adding those symbols, tests must exercise real graph/checkpoint/publish behavior rather than being changed to accept any non-error result.

- [ ] **Step 5.3: Add the work job enum and implement the bounded unit executor.**

Append this member to `JobType` in `jobs/models.py`:

```python
    WORK_ANALYSIS = "work_analysis"
```

Create `work_analysis/executor.py` with:

```python
from __future__ import annotations

import time
from contextlib import nullcontext
from datetime import datetime, timezone

from .artifacts import validate_artifact
from .configuration import snapshot_work_configuration
from .contracts import WorkAnalysisError
from .evidence import validate_unit_output
from .manifest import read_source_texts
from .planning import materialize_unit, plan_reduction_level


def _utc_now():
    return datetime.now(timezone.utc)


class _Pulse:
    def __init__(self, db_factory, lease, context, slice_deadline_at):
        self.db_factory = db_factory
        self.lease = lease
        self.context = context
        self.slice_deadline_at = slice_deadline_at
        self.next_check = 0.0
        self.stopped = False

    def check(self):
        if self.context["manager"].is_cancel_requested(self.context["job_id"]):
            self.stopped = True
        if _utc_now().strftime("%Y-%m-%d %H:%M:%S") >= self.slice_deadline_at:
            self.stopped = True
        if self.stopped or time.monotonic() < self.next_check:
            return self.stopped
        db = self.db_factory()
        try:
            state = db.get_work_analysis(self.lease.run_id)
            self.stopped = state is None or state["status"] != "running"
            if not self.stopped:
                self.stopped = not db.heartbeat_work_analysis(self.lease, now=_utc_now())
        finally:
            db.close()
        self.next_check = time.monotonic() + 1.0
        return self.stopped

    def require_running(self):
        if self.check():
            raise WorkAnalysisError("work_stopping", 409)


class WorkAnalysisExecutor:
    def __init__(self, db_factory, ai_factory):
        self.db_factory = db_factory
        self.ai_factory = ai_factory

    def _private(self, run_id):
        db = self.db_factory()
        try:
            state = db.get_work_analysis(run_id, include_private=True)
            if state is None:
                raise WorkAnalysisError("work_deleted", 410)
            return state
        finally:
            db.close()

    def _configuration(self, state):
        db = self.db_factory()
        try:
            with db.read_transaction():
                try:
                    current, prompt, preflight = snapshot_work_configuration(
                        db.conn, state["configuration"]["agent_id"], state["configuration"]["output_tokens"],
                    )
                except WorkAnalysisError:
                    raise WorkAnalysisError("configuration_changed", 409) from None
                if current["configuration_fingerprint"] != state["configuration"]["configuration_fingerprint"]:
                    raise WorkAnalysisError("configuration_changed", 409)
                return current, prompt, preflight
        finally:
            db.close()

    def _run_unit(self, lease, state, entry, pulse):
        unit = entry["unit"]
        configuration, prompt, preflight = self._configuration(state)
        by_id = {item["unit"]["unit_id"]: item for item in state["units"]}
        children = {}
        for child_id in unit["child_unit_ids"]:
            child = by_id.get(child_id)
            if child is None or child["status"] != "succeeded" or child["artifact"] is None:
                raise WorkAnalysisError("graph_invalid", 409)
            children[child_id] = validate_artifact(child["artifact"], final=child["unit"]["is_final"])
        db = self.db_factory()
        try:
            with db.read_transaction():
                texts = read_source_texts(db.conn, state["manifest"])
        finally:
            db.close()
        try:
            payload, block_map = materialize_unit(
                state["manifest"], unit, texts, children,
                agent_prompt=prompt, output_tokens=configuration["output_tokens"],
            )
            payload["allowed_provider_ids"] = list(configuration["allowed_provider_ids"])
            if not preflight(payload)["can_run"]:
                raise WorkAnalysisError("synthesis_capacity_exceeded" if unit["kind"] == "reduce" else "configuration_changed", 409)
            pulse.require_running()
            db = self.db_factory()
            try:
                binding = db.begin_work_analysis_unit(lease, unit["unit_id"])
            finally:
                db.close()
            result = self.ai_factory().invoke_text(
                configuration["agent_id"], payload, work_binding=binding,
                stop_requested=pulse.check, deadline_at=state["slice_deadline_at"],
            )
            if result.work_error_code is not None:
                raise WorkAnalysisError(result.work_error_code, 409)
            pulse.require_running()
            if result.finish_state != "succeeded" or result.job_status != "succeeded":
                raise WorkAnalysisError("transport_failed", 409)
            try:
                artifact = validate_unit_output(result.output_text, unit, block_map, children)
            except (TypeError, ValueError):
                raise WorkAnalysisError("validation_failed", 409) from None
            pulse.require_running()
            db = self.db_factory()
            try:
                db.save_work_analysis_unit(
                    lease, unit["unit_id"], artifact,
                    actual_target={"provider_id": result.actual_provider_id, "model_key": result.actual_model_key},
                )
            finally:
                db.close()
        finally:
            texts.clear()

    def _derive_next_level(self, lease, state, frontier):
        configuration, prompt, preflight = self._configuration(state)
        children = {item["unit"]["unit_id"]: item["artifact"] for item in frontier}
        level = max(item["unit"]["level"] for item in frontier) + 1
        try:
            units = plan_reduction_level(
                lease.run_id, level, [item["unit"] for item in frontier], children, preflight,
                agent_prompt=prompt, output_tokens=configuration["output_tokens"],
            )
        except (TypeError, ValueError):
            raise WorkAnalysisError("synthesis_capacity_exceeded", 409) from None
        if not units or len(state["units"]) + len(units) > 512 or level > 8:
            raise WorkAnalysisError("synthesis_capacity_exceeded", 409)
        db = self.db_factory()
        try:
            db.add_work_analysis_units(lease, units)
        finally:
            db.close()

    def _classify_stop(self, run_id, error, slice_deadline_at):
        if error.code in {"source_changed", "source_deleted", "configuration_changed", "legacy_repreview_required", "invalidated"}:
            return "invalidated", error.code
        if error.code in {"budget_exhausted", "active_time_exhausted"}:
            return "paused", "budget_exhausted"
        if error.code == "synthesis_capacity_exceeded":
            return "paused", "synthesis_capacity_exceeded"
        if error.code in {"work_stopping", "work_deleted", "owner_lost", "lease_expired"}:
            db = self.db_factory()
            try:
                state = db.get_work_analysis(run_id)
            finally:
                db.close()
            if state is None:
                return "cancelled", "work_deleted"
            if state["status"] == "invalidated":
                return "invalidated", state.get("reason") or "invalidated"
            if state["status"] in {"cancelling", "cancelled"}:
                return "cancelled", state.get("reason") or "user_cancel"
            if state["status"] in {"pausing", "paused"}:
                return "paused", state.get("reason") or "user_pause"
            if _utc_now().strftime("%Y-%m-%d %H:%M:%S") >= slice_deadline_at:
                return "paused", "budget_exhausted"
            return "cancelled", "user_cancel"
        return "failed", error.code if error.code in {"validation_failed", "transport_failed", "graph_invalid"} else "execution_failed"

    def run(self, run_id, context):
        job = context.get("job")
        if (job is None or job.spec.source.value != "web" or job.spec.job_type.value != "work_analysis"
                or context.get("job_id") != job.job_id
                or context.get("params") != {"run_id": run_id}
                or not callable(context.get("claim_finalization"))):
            raise RuntimeError("work_analysis_requires_shared_web_runner")
        control_scope = context.get("work_control_scope", nullcontext)
        with control_scope():
            if context["manager"].is_cancel_requested(context["job_id"]):
                raise InterruptedError("work_stopping")
            db = self.db_factory()
            try:
                lease = db.claim_work_analysis(
                    run_id, now=_utc_now(), slice_seconds=1800,
                    shared_job_id=context["job_id"], shared_log_id=job.progress.get("log_id"),
                )
            finally:
                db.close()
        started = time.monotonic()
        outcome, reason = "failed", "execution_failed"
        slice_deadline_at = "9999-12-31 23:59:59"
        try:
            state = self._private(run_id)
            slice_deadline_at = state["slice_deadline_at"]
            pulse = _Pulse(self.db_factory, lease, context, slice_deadline_at)
            for graph_step in range(1025):
                pulse.require_running()
                state = self._private(run_id)
                self._configuration(state)
                entries = state["units"]
                originals = [item for item in entries if item["unit"]["kind"] in {"whole", "extract"}]
                expected = {unit["unit_id"] for unit in state["plan"]["original_units"]}
                if {item["unit"]["unit_id"] for item in originals} != expected or not originals:
                    raise WorkAnalysisError("graph_invalid", 409)
                original_pending = [item for item in originals if item["status"] != "succeeded"]
                if original_pending:
                    self._run_unit(lease, state, original_pending[0], pulse)
                    continue
                highest = max(item["unit"]["level"] for item in entries)
                frontier = [item for item in entries if item["unit"]["level"] == highest]
                pending = [item for item in frontier if item["status"] != "succeeded"]
                if pending:
                    self._run_unit(lease, state, pending[0], pulse)
                    continue
                if len(frontier) == 1 and frontier[0]["unit"]["is_final"]:
                    artifact = validate_artifact(frontier[0]["artifact"], final=True)
                    pulse.require_running()
                    if not context["claim_finalization"]():
                        raise WorkAnalysisError("work_stopping", 409)
                    db = self.db_factory()
                    try:
                        db.publish_work_analysis(lease, artifact)
                    finally:
                        db.close()
                    outcome, reason = "succeeded", None
                    return {"work_analysis": {"run_id": run_id, "status": "succeeded", "validated_units": len(entries)}}
                self._derive_next_level(lease, state, frontier)
            raise WorkAnalysisError("synthesis_capacity_exceeded", 409)
        except WorkAnalysisError as error:
            # A cancellation latch can precede its durable CAS/retry. Wait
            # for that short control operation before interpreting the stop.
            with control_scope():
                outcome, reason = self._classify_stop(run_id, error, slice_deadline_at)
            context["manager"].update_progress(context["job_id"], work_status=outcome, work_reason=reason)
            if outcome in {"paused", "cancelled", "invalidated"}:
                raise InterruptedError(reason) from None
            raise RuntimeError(reason) from None
        except Exception:
            outcome, reason = "failed", "execution_failed"
            raise RuntimeError("work_execution_failed") from None
        finally:
            db = self.db_factory()
            try:
                try:
                    db.settle_work_analysis(
                        lease, outcome=outcome, reason=reason,
                        elapsed_seconds=max(0.0, time.monotonic() - started),
                    )
                except WorkAnalysisError as error:
                    if error.code != "owner_lost":
                        raise RuntimeError("work_settlement_failed") from None
                except Exception:
                    raise RuntimeError("work_settlement_failed") from None
            finally:
                db.close()
```

The 1,025-step bound permits up to 512 execute steps and bounded graph-creation steps while preventing a corrupted graph from becoming an unbounded paid loop. Every actual send still independently consumes parent/child reservation. The executor never retries invalid JSON on its own; the next attempt requires explicit business resume. Its call to `claim_finalization()` occurs before `publish_work_analysis`, and the existing `JobRunner` exception path aborts that claim if publication fails. Task 6 injects the private `work_control_scope` into real Web workers. Only the short claim and stop-classification sections take it, before opening their database connections; no transport call or whole worker execution occurs under this lock. Isolated direct-runner tests without a coordinator use `nullcontext`.

- [ ] **Step 5.4: Run the executor, evidence, and storage suites.**

Run: `python -m pytest tests/test_work_analysis_executor.py tests/test_work_analysis_storage.py -q`

Expected: pass. Also run the Task 2 evidence/coverage/planning test paths defined in Task 2; do not substitute a permissive mocked validator. The real-provider fixture's `requests.Session.request` guard must stay installed.

- [ ] **Step 5.5: Review checkpoint/finality behavior and commit only these paths.**

Review gate: full original coverage precedes reduction; every derived graph is durable before a send; only validated same-run children enter reducers; finality comes from the frozen unit, never model output; a saved original is not re-sent on resume; a normal stop is settled once; an invalidated/deleted old owner cannot refund or publish; finalization is claimed before the report transaction.

```powershell
git add src/pixiv_novel_sync/work_analysis/executor.py src/pixiv_novel_sync/jobs/models.py tests/work_analysis_runtime_helpers.py tests/test_work_analysis_executor.py
git commit -m "feat: execute validated work analysis graphs and checkpoints"
```

### Task 6: Share Admission, Preemption, Controls, And Startup Recovery

**Files:**
- Create: `src/pixiv_novel_sync/work_analysis/coordinator.py`.
- Modify: `src/pixiv_novel_sync/jobs/models.py` (`JobState`, append defaulted admission metadata).
- Modify: `src/pixiv_novel_sync/jobs/manager.py` (`JobManager` initialization, shared admission, existing semaphore access).
- Modify: `src/pixiv_novel_sync/jobs/runner.py` (`run`, failed slot acquisition only).
- Modify: `src/pixiv_novel_sync/web/managers.py` (`scheduler_task_priority`, `scheduler_task_is_preemptible`, `AutoSyncScheduler` tick/watcher hooks).
- Modify: `src/pixiv_novel_sync/webapp.py` (`_release_scheduler_owner:325`, `create_app:488`, shared submission/completion, scheduler owner binding, generic cancellation and manual sync routes).
- Create: `tests/test_work_analysis_coordinator.py`, `tests/test_work_analysis_web_admission.py`.
- Modify tests: `tests/test_jobs_manager.py`, `tests/test_jobs_runner.py`, `tests/test_scheduler_priority.py`, `tests/test_webapp_jobs.py`.

**Interfaces:**
- Consumes `WorkAnalysisExecutor(db_factory, ai_factory).run(run_id, context)` and Task 3's public state, stop/resume, owner settlement, recovery, and `queued_work_analysis_ids()` methods. That last method returns `queued` and `paused/sync_yield` candidates; `claim_work_analysis` accepts both atomically. Admission does not reset counters or issue a business resume for an ordinary yield.
- Produces `WorkAnalysisCoordinator(manager, submit_shared, run_shared, settings_provider, *, db_factory, scheduler)` with `wake(reason)`, `request_control(run_id, action, expected_version)`, `on_job_finished(job_id)`, `request_sync(spec, settings, task_type, task_name, *, progress=None)`, `startup_recover()`, and the internal `cancel_shared_job(job_id, action='cancel')`, `work_control_scope()`, `challenger_priority()`, `close()` adapters.
- `submit_shared` remains the existing Web submit function; its new optional `work_admission_priority` argument creates a waiting item in the SAME `JobManager._jobs`. `run_shared(job_id)` is the existing Web thread launcher for an already admitted job, not a new executor.
- `JobState.shared_waiting` and `shared_priority` are server-only scheduling metadata. The analysis `JobSpec.params` is exactly `{'run_id': hex32}`. No settings, novel bodies, prompts, reports, or Provider errors enter shared params/progress/logs.
- At most eight manual synchronization waiters may accumulate behind a work analysis. They are ordinary `JobState` entries in the existing manager, not a second queue or database table. Non-work busy submissions preserve the existing reject behavior.
- The existing semaphore is reserved before a bridge-owned launch. A failed nonblocking acquire leaves the durable parent and its waiting shared item retryable; a completion or actual slot release wakes that SAME item. There is no speculative worker which fails and silently consumes the only pending parent intent.
- `wake` returns whether a work/bridge-owned shared job should defer the scheduler's ordinary submit pass. It is otherwise an idempotent notification, not an HTTP execution loop. Its only triggers are explicit creation/resume, shared slot/completion callbacks, and the existing scheduler tick/watcher.

**Lock and lifecycle rule:** Coordinator admission/control lock -> short manager operation -> release manager lock -> database transaction. No database call occurs under `JobManager._lock` or `AutoSyncScheduler._lock`. The worker never takes the coordinator lock while holding a database transaction. Its injected `work_control_scope` serializes only initial claim and stop classification with control; it never spans Provider work, normal checkpointing, publication, or worker exit. Cancellation permission linearizes under the manager lock BEFORE the stop CAS; finalization permission linearizes under that same lock BEFORE publication. The control gate remains held through internal yield retries, so a worker that sees the cancellation latch cannot classify/settle it as a user cancellation before the durable yield resolves. The parent transaction remains authoritative if memory and durable state disagree after a crash. The existing normalized-path Web owner is pinned while its coordinator is attached, including when auto synchronization is stopped. Secondary apps reuse that owner and startup recovery runs once. This is a NEW lifecycle guarantee, not an existing capability. The explicit idle-only application teardown hook below releases that ownership; a normal auto-sync stop does not.

- [ ] **Step 6.1: Add failing shared-manager and failed-slot tests.**

Append to `tests/test_jobs_manager.py`, adding these imports at the top:

```python
import pytest

from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobStatus, JobType


def test_work_waiters_use_the_same_bounded_manager_collection():
    manager = JobManager()
    work = manager.submit(JobSpec(JobSource.WEB, ["work_analysis"], JobType.WORK_ANALYSIS, {"run_id": "a" * 32}))
    assert manager.mark_running(work.job_id)
    queued = [manager.submit_work_waiting(JobSpec(JobSource.WEB, ["bookmark"]), 1) for index in range(8)]
    assert len(manager._jobs) == 9
    assert manager.shared_admission_snapshot()[0] == [work]
    assert manager.shared_admission_snapshot()[1] == queued
    with pytest.raises(RuntimeError, match="shared_pending_full"):
        manager.submit_work_waiting(JobSpec(JobSource.WEB, ["bookmark"]), 1)
    assert manager.reserve_shared_start(queued[0].job_id) is False
    assert all(item.shared_waiting and item.started_at is None for item in queued)


def test_work_waiting_does_not_change_non_work_busy_behavior():
    manager = JobManager()
    sync = manager.submit(JobSpec(JobSource.WEB, ["bookmark"]))
    manager.mark_running(sync.job_id)
    with pytest.raises(RuntimeError, match="shared_busy"):
        manager.submit_work_waiting(JobSpec(JobSource.WEB, ["subscribed_series"]), 2)
    assert len(manager._jobs) == 1


def test_reserved_slot_is_consumed_once_by_its_named_runner():
    manager = JobManager()
    work = manager.submit_work_waiting(
        JobSpec(JobSource.WEB, ["work_analysis"], JobType.WORK_ANALYSIS, {"run_id": "a" * 32}), 4,
    )
    assert manager.reserve_shared_start(work.job_id)
    assert manager.acquire_run_slot() is False
    assert manager.acquire_run_slot("different-job") is False
    assert manager.acquire_run_slot(work.job_id)
    assert manager.acquire_run_slot(work.job_id) is False
    manager.release_run_slot()
    assert manager.acquire_run_slot()
    manager.release_run_slot()


def test_slot_release_callback_runs_without_the_manager_lock():
    import threading

    manager = JobManager()
    observed = []

    def callback():
        def reader():
            with manager._lock:
                observed.append("unlocked")
        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        reader_thread.join(timeout=2)
        assert not reader_thread.is_alive()

    manager.set_slot_released_callback(callback)
    assert manager.acquire_run_slot()
    manager.release_run_slot()
    assert observed == ["unlocked"]
```

Append to `tests/test_jobs_runner.py`:

```python
def test_work_slot_race_defers_instead_of_blackholing_the_parent():
    from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobStatus, JobType

    manager = JobManager()
    assert manager.acquire_run_slot()
    job = manager.submit(JobSpec(JobSource.WEB, ["work_analysis"], JobType.WORK_ANALYSIS, {"run_id": "a" * 32}))
    executed = []
    runner = JobRunner(manager, lambda task, context: executed.append(context["params"]) or {})
    runner.run(job.job_id)
    assert job.status == JobStatus.QUEUED and job.shared_waiting
    assert executed == []
    manager.release_run_slot()
    assert manager.reserve_shared_start(job.job_id)
    runner.run(job.job_id)
    assert job.status == JobStatus.SUCCEEDED
    assert executed == [{"run_id": "a" * 32}]
```

- [ ] **Step 6.2: Run the red shared-admission tests.**

Run: `python -m pytest tests/test_jobs_manager.py tests/test_jobs_runner.py -q -k "work_wait or reserved_slot or slot_release_callback or work_slot_race"`

Expected: fail for the missing admission methods/metadata, and the work slot-race test observes `failed` rather than retryable `queued`. The existing ordinary busy-runner tests must retain their failure behavior.

- [ ] **Step 6.3: Implement bounded waiting and named reservations in the existing manager.**

Append these defaulted fields to `JobState`, after `finished_at`. They are not added to `_job_to_dict_unified` or client-provided `JobSpec.params`:

```python
    shared_waiting: bool = False
    shared_priority: int | None = None
```

In `jobs/manager.py`, add `import logging` and `from collections.abc import Callable`, extend the model import to include `JobType`, and define the module logger:

```python
logger = logging.getLogger(__name__)
```

Append these fields in `JobManager.__init__`:

```python
        self._reserved_slot_job_id: str | None = None
        self._slot_released_callback: Callable[[], None] | None = None
```

Add these complete methods to `JobManager`:

```python
    def shared_admission_snapshot(self) -> tuple[list[JobState], list[JobState]]:
        with self._lock:
            live = [job for job in self._jobs.values() if job.status not in _TERMINAL_STATUSES]
            active = [job for job in live if not job.shared_waiting]
            waiting = sorted(
                (job for job in live if job.shared_waiting),
                key=lambda job: (job.shared_priority if job.shared_priority is not None else 4,
                                 job.created_at, job.job_id),
            )
            return active, waiting

    def check_work_waiting_admission(self, spec: JobSpec, priority: int) -> None:
        if type(priority) is not int or not 1 <= priority <= 4 or spec.source.value != "web":
            raise ValueError("invalid_shared_admission")
        is_work = spec.job_type == JobType.WORK_ANALYSIS
        if is_work != (priority == 4):
            raise ValueError("invalid_shared_admission")
        with self._lock:
            active, waiting = self.shared_admission_snapshot()
            if any(job.spec.job_type != JobType.WORK_ANALYSIS for job in active):
                raise RuntimeError("shared_busy")
            if is_work:
                if active or waiting:
                    raise RuntimeError("shared_busy")
            elif len(waiting) >= 8:
                raise RuntimeError("shared_pending_full")

    def submit_work_waiting(self, spec: JobSpec, priority: int) -> JobState:
        with self._lock:
            self.check_work_waiting_admission(spec, priority)
            job = self.submit(spec)
            job.shared_waiting = True
            job.shared_priority = priority
            job.message = "waiting for shared slot"
            return job

    def reserve_shared_start(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != JobStatus.QUEUED or not job.shared_waiting:
                return False
            active, waiting = self.shared_admission_snapshot()
            if active or not waiting or waiting[0].job_id != job_id:
                return False
            if self._reserved_slot_job_id is not None or not self._semaphore.acquire(blocking=False):
                return False
            self._reserved_slot_job_id = job_id
            job.shared_waiting = False
            job.message = "admitted to shared slot"
            return True

    def abandon_reserved_start(self, job_id: str) -> bool:
        with self._lock:
            if self._reserved_slot_job_id != job_id:
                return False
            self._reserved_slot_job_id = None
            self._semaphore.release()
            return True

    def defer_work_start(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != JobStatus.QUEUED or job.spec.job_type != JobType.WORK_ANALYSIS:
                return False
            job.shared_waiting = True
            job.shared_priority = 4
            job.message = "waiting for shared slot"
            return True

    def set_slot_released_callback(self, callback: Callable[[], None] | None) -> None:
        with self._lock:
            self._slot_released_callback = callback
```

Replace the existing semaphore accessor methods with:

```python
    def acquire_run_slot(self, job_id: str | None = None) -> bool:
        with self._lock:
            if job_id is not None and self._reserved_slot_job_id == job_id:
                self._reserved_slot_job_id = None
                return True
            return self._semaphore.acquire(blocking=False)

    def release_run_slot(self) -> None:
        self._semaphore.release()
        with self._lock:
            callback = self._slot_released_callback
        if callback is not None:
            try:
                callback()
            except Exception:
                logger.warning("Shared admission notification failed")
```

In `jobs/runner.py`, extend the existing model import to `JobState, JobType`. Replace only the acquire/failure block at the beginning of `run`:

```python
        acquired = self.manager.acquire_run_slot(job_id)
        if not acquired:
            if state.spec.job_type == JobType.WORK_ANALYSIS:
                self.manager.defer_work_start(job_id)
            else:
                self.manager.mark_failed(job_id, "已有任务正在运行")
            return state
```

Keep the existing `FinalizationClaim`, exception/abort paths, and `finally: release_run_slot()` behavior. Reservation does not release a running worker early; it only transfers ownership of the SAME acquired semaphore to the named runner.

- [ ] **Step 6.4: Run shared-manager/runner regressions.**

Run: `python -m pytest tests/test_jobs_manager.py tests/test_jobs_runner.py tests/test_jobs_concurrency.py tests/test_cli_jobs.py -q`

Expected: pass. Generic CLI/sync behavior is unchanged. No `work_analysis` branch is added to the generic CLI task dispatcher.

- [ ] **Step 6.5: Add a real-business-storage coordinator harness and failing control/admission tests.**

Create `tests/test_work_analysis_coordinator.py`:

```python
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.jobs.manager import JobManager
from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobStatus, JobType
from pixiv_novel_sync.jobs.runner import JobRunner
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.web.managers import AutoSyncScheduler
from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError
from pixiv_novel_sync.work_analysis.coordinator import WorkAnalysisCoordinator
from pixiv_novel_sync.work_analysis.executor import WorkAnalysisExecutor
from work_analysis_runtime_helpers import make_runtime_environment


def wait_for(predicate):
    end = time.monotonic() + 5
    while not predicate():
        if time.monotonic() >= end:
            raise AssertionError("synthetic worker did not reach its event")
        time.sleep(0.005)


def make_bridge(tmp_path, monkeypatch, *, auto_enabled=False):
    env = make_runtime_environment(tmp_path, monkeypatch)
    manager = JobManager()
    settings = SimpleNamespace(sync=SimpleNamespace(
        auto_sync_enabled=auto_enabled, auto_sync_bookmarks_enabled=auto_enabled,
        auto_sync_timezone="UTC",
    ))
    scheduler = AutoSyncScheduler(None, None, shared_job_manager=manager)
    scheduler._running = auto_enabled
    monkeypatch.setattr("pixiv_novel_sync.web.managers.SCHEDULER_PREEMPT_POLL_SECONDS", 0.01)
    entered = threading.Event()
    release_provider = threading.Event()
    sync_entered = threading.Event()
    release_sync = threading.Event()
    threads = []
    starts = []
    holder = {}

    def hold_response(unit_id):
        entered.set()
        assert release_provider.wait(timeout=5)

    env.provider.before_response = hold_response
    executor = WorkAnalysisExecutor(env.db_factory, lambda: env.service)

    def execute(task, context):
        starts.append(task)
        if task == "work_analysis":
            context = {**context, "work_control_scope": holder["coordinator"].work_control_scope}
            return executor.run(context["params"]["run_id"], context)
        assert task == "bookmark"
        sync_entered.set()
        assert release_sync.wait(timeout=5)
        return {"bookmarks": 1}

    runner = JobRunner(manager, execute)

    def submit(spec, current_settings, task_type, task_name, *, run_async=True,
               work_admission_priority=None, progress=None):
        assert run_async is False and current_settings is settings
        job = manager.submit_work_waiting(spec, work_admission_priority)
        if progress:
            manager.update_progress(job.job_id, **progress)
        return job

    def launch(job_id):
        def run_and_complete(current_job_id):
            try:
                runner.run(current_job_id)
            finally:
                holder["coordinator"].on_job_finished(current_job_id)

        def watched():
            job = manager.get_job(job_id)
            if job.spec.job_type == JobType.WORK_ANALYSIS:
                scheduler._run_and_watch_for_preemption(settings, job_id, "work_analysis", run_and_complete)
            else:
                run_and_complete(job_id)

        thread = threading.Thread(target=watched, daemon=True)
        threads.append(thread)
        thread.start()

    coordinator = WorkAnalysisCoordinator(manager, submit, launch, lambda: settings,
                                          db_factory=env.db_factory, scheduler=scheduler)
    holder["coordinator"] = coordinator
    scheduler.work_coordinator = coordinator
    # Production calls recovery BEFORE creation. This fixture created its run
    # during setup, so prove startup fencing then explicitly restore the intent.
    coordinator.startup_recover()
    interrupted = env.db.get_work_analysis(env.run_id)
    env.db.resume_work_analysis(env.run_id, interrupted["version"])

    def close():
        release_provider.set()
        release_sync.set()
        for thread in list(threads):
            thread.join(timeout=5)
            assert not thread.is_alive()
        coordinator.close()
        env.close()

    return SimpleNamespace(env=env, manager=manager, scheduler=scheduler, settings=settings,
                           coordinator=coordinator, entered=entered, release_provider=release_provider,
                           sync_entered=sync_entered, release_sync=release_sync, threads=threads,
                           starts=starts, close=close)


def test_manual_sync_waits_for_real_exit_then_work_resumes_with_auto_sync_off(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        before = bridge.env.db.get_work_analysis(bridge.env.run_id, include_private=True)
        sync = bridge.coordinator.request_sync(JobSpec(JobSource.WEB, ["bookmark"]), bridge.settings,
                                               "bookmark", "Synthetic bookmark sync")
        stopping = bridge.env.db.get_work_analysis(bridge.env.run_id, include_private=True)
        assert stopping["status"] == "pausing" and stopping["reason"] == "sync_yield"
        assert stopping["epoch"] == before["epoch"]
        assert sync.shared_waiting and not bridge.sync_entered.is_set()
        assert bridge.manager.acquire_run_slot() is False
        bridge.release_provider.set()
        assert bridge.sync_entered.wait(timeout=5)
        paused = bridge.env.db.get_work_analysis(bridge.env.run_id)
        assert paused["status"] == "paused" and paused["reason"] == "sync_yield"
        assert paused["active_seconds_debited"] < 30
        assert len(bridge.env.provider.sent_units) == 1
        bridge.release_sync.set()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "succeeded")
        assert bridge.starts == ["work_analysis", "bookmark", "work_analysis"]
        assert bridge.env.db.get_work_analysis(bridge.env.run_id)["request_count"] == 2
    finally:
        bridge.close()


def test_sync_yield_retries_progress_cas_before_worker_can_classify_or_settle(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    stop_resolution_waiting = threading.Event()
    settled = threading.Event()
    attempts = []
    original_stop = Database.request_work_analysis_stop
    original_settle = Database.settle_work_analysis
    original_scope = WorkAnalysisCoordinator.work_control_scope

    @contextmanager
    def observed_scope(self):
        job = self._job_for_run(bridge.env.run_id)
        if job is not None and self.manager.is_cancel_requested(job.job_id):
            stop_resolution_waiting.set()
        with original_scope(self):
            yield

    def progress_before_stop(self, run_id, action, expected_version):
        if action == "sync_yield":
            attempts.append(expected_version)
            if len(attempts) == 1:
                job = bridge.coordinator._job_for_run(run_id)
                assert bridge.manager.is_cancel_requested(job.job_id)
                with self.transaction() as conn:
                    conn.execute("UPDATE work_analysis_runs SET version=version+1 WHERE run_id=?", (run_id,))
                bridge.release_provider.set()
                assert stop_resolution_waiting.wait(timeout=5)
                assert not settled.is_set()
                assert bridge.manager.acquire_run_slot() is False
        return original_stop(self, run_id, action, expected_version)

    def checked_settle(self, lease, **kwargs):
        if not settled.is_set():
            assert self.get_work_analysis(lease.run_id)["reason"] == "sync_yield"
            settled.set()
        return original_settle(self, lease, **kwargs)

    monkeypatch.setattr(WorkAnalysisCoordinator, "work_control_scope", observed_scope)
    monkeypatch.setattr(Database, "request_work_analysis_stop", progress_before_stop)
    monkeypatch.setattr(Database, "settle_work_analysis", checked_settle)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        before = bridge.env.db.get_work_analysis(bridge.env.run_id, include_private=True)
        bridge.coordinator.request_sync(JobSpec(JobSource.WEB, ["bookmark"]), bridge.settings,
                                        "bookmark", "Synthetic conflict sync")
        assert bridge.sync_entered.wait(timeout=5)
        paused = bridge.env.db.get_work_analysis(bridge.env.run_id, include_private=True)
        assert attempts == [before["version"], before["version"] + 1]
        assert settled.is_set() and paused["status"] == "paused" and paused["reason"] == "sync_yield"
        assert paused["request_count"] == 1 and len(bridge.env.provider.sent_units) == 1
        bridge.release_sync.set()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "succeeded")
        assert bridge.starts == ["work_analysis", "bookmark", "work_analysis"]
        assert bridge.env.db.get_work_analysis(bridge.env.run_id)["request_count"] == 2
    finally:
        bridge.close()


@pytest.mark.parametrize("intervening", ["pause", "cancel", "delete", "new_owner"])
def test_internal_yield_retry_never_overwrites_stronger_control_or_new_owner(tmp_path, monkeypatch, intervening):
    bridge = make_bridge(tmp_path, monkeypatch)
    original_stop = Database.request_work_analysis_stop
    attempts = []

    def change_owner_or_intent(self, run_id, action, expected_version):
        if action == "sync_yield":
            attempts.append(expected_version)
            if intervening == "new_owner":
                with self.transaction() as conn:
                    conn.execute("UPDATE work_analysis_runs SET epoch=epoch+1,owner_token=?,version=version+1 "
                                 "WHERE run_id=?", ("e" * 64, run_id))
            else:
                original_stop(self, run_id, intervening, expected_version)
        return original_stop(self, run_id, action, expected_version)

    monkeypatch.setattr(Database, "request_work_analysis_stop", change_owner_or_intent)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        job = bridge.coordinator._job_for_run(bridge.env.run_id)
        assert bridge.coordinator.cancel_shared_job(job.job_id, action="sync_yield") is False
        control = bridge.env.db.get_work_analysis_control(bridge.env.run_id)
        assert len(attempts) == 1
        if intervening == "new_owner":
            assert control["owner_token"] == "e" * 64 and control["status"] == "running"
        elif intervening == "delete":
            assert control["deleted_at"] is not None
        else:
            assert control["reason"] == {"pause": "user_pause", "cancel": "user_cancel"}[intervening]
        bridge.release_provider.set()
        wait_for(lambda: job.status == JobStatus.CANCELLED)
        after = bridge.env.db.get_work_analysis_control(bridge.env.run_id)
        if intervening == "new_owner":
            assert (after["epoch"], after["owner_token"]) == (control["epoch"], control["owner_token"])
        assert bridge.starts == ["work_analysis"] and len(bridge.env.provider.sent_units) == 1
    finally:
        bridge.close()


def test_public_control_version_conflict_remains_single_attempt_strict_cas(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    original_stop = Database.request_work_analysis_stop
    attempts = []

    def progress_before_stop(self, run_id, action, expected_version):
        attempts.append(expected_version)
        with self.transaction() as conn:
            conn.execute("UPDATE work_analysis_runs SET version=version+1 WHERE run_id=?", (run_id,))
        return original_stop(self, run_id, action, expected_version)

    monkeypatch.setattr(Database, "request_work_analysis_stop", progress_before_stop)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        before = bridge.env.db.get_work_analysis(bridge.env.run_id)
        with pytest.raises(WorkAnalysisError) as caught:
            bridge.coordinator.request_control(bridge.env.run_id, "pause", before["version"])
        assert caught.value.code == "version_conflict" and attempts == [before["version"]]
        assert bridge.env.db.get_work_analysis(bridge.env.run_id)["reason"] is None
        bridge.release_provider.set()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "cancelled")
    finally:
        bridge.close()


def test_internal_yield_retry_is_bounded_and_never_acknowledges_failed_persistence(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    original_stop = Database.request_work_analysis_stop
    attempts = []

    def always_conflict(self, run_id, action, expected_version):
        attempts.append(expected_version)
        with self.transaction() as conn:
            conn.execute("UPDATE work_analysis_runs SET version=version+1 WHERE run_id=?", (run_id,))
        return original_stop(self, run_id, action, expected_version)

    monkeypatch.setattr(Database, "request_work_analysis_stop", always_conflict)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        job = bridge.coordinator._job_for_run(bridge.env.run_id)
        with pytest.raises(WorkAnalysisError) as caught:
            bridge.coordinator.cancel_shared_job(job.job_id, action="sync_yield")
        assert caught.value.code == "control_unavailable" and len(attempts) == 3
        assert bridge.manager.is_cancel_requested(job.job_id)
        bridge.release_provider.set()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "cancelled")
        assert bridge.starts == ["work_analysis"] and len(bridge.env.provider.sent_units) == 1
    finally:
        bridge.close()


@pytest.mark.parametrize("action,expected", [("pause", "paused"), ("cancel", "cancelled")])
def test_user_stop_is_never_automatically_requeued(tmp_path, monkeypatch, action, expected):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        state = bridge.env.db.get_work_analysis(bridge.env.run_id)
        bridge.coordinator.request_control(bridge.env.run_id, action, state["version"])
        bridge.release_provider.set()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == expected)
        bridge.coordinator.wake("completion")
        assert bridge.starts == ["work_analysis"]
        assert bridge.env.db.get_work_analysis(bridge.env.run_id)["request_count"] == 1
    finally:
        bridge.close()


def test_generic_cancel_persists_parent_intent_before_acknowledgement(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        job = bridge.manager.shared_admission_snapshot()[0][0]
        assert bridge.coordinator.cancel_shared_job(job.job_id)
        state = bridge.env.db.get_work_analysis(bridge.env.run_id)
        assert state["status"] == "cancelling" and state["reason"] == "user_cancel"
        bridge.release_provider.set()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "cancelled")
    finally:
        bridge.close()


def test_stop_persistence_failure_keeps_memory_cancel_and_cannot_publish(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        state = bridge.env.db.get_work_analysis(bridge.env.run_id)
        original = Database.request_work_analysis_stop

        def disk_failure(self, run_id, action, expected_version):
            raise OSError("synthetic disk failure")

        monkeypatch.setattr(Database, "request_work_analysis_stop", disk_failure)
        with pytest.raises(WorkAnalysisError) as caught:
            bridge.coordinator.request_control(bridge.env.run_id, "pause", state["version"])
        assert caught.value.code == "control_unavailable"
        job = bridge.manager.shared_admission_snapshot()[0][0]
        assert bridge.manager.is_cancel_requested(job.job_id)
        monkeypatch.setattr(Database, "request_work_analysis_stop", original)
        bridge.release_provider.set()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "cancelled")
        assert bridge.env.db.get_work_analysis(bridge.env.run_id)["has_report"] is False
    finally:
        bridge.close()


def test_finalization_first_refuses_both_business_and_generic_cancellation(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    checked = []
    original = Database.publish_work_analysis

    def try_cancel_at_publication(self, lease, artifact):
        state = bridge.env.db.get_work_analysis(lease.run_id)
        with pytest.raises(WorkAnalysisError) as caught:
            bridge.coordinator.request_control(lease.run_id, "cancel", state["version"])
        assert caught.value.code == "finalizing"
        job = bridge.manager.shared_admission_snapshot()[0][0]
        assert bridge.coordinator.cancel_shared_job(job.job_id) is False
        checked.append(True)
        return original(self, lease, artifact)

    monkeypatch.setattr(Database, "publish_work_analysis", try_cancel_at_publication)
    try:
        bridge.release_provider.set()
        bridge.coordinator.wake("created")
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "succeeded")
        assert checked == [True]
    finally:
        bridge.close()


def test_startup_recovery_runs_once_and_never_activates_old_intent(tmp_path, monkeypatch):
    env = make_runtime_environment(tmp_path, monkeypatch)
    manager = JobManager()
    scheduler = AutoSyncScheduler(None, None, shared_job_manager=manager)
    calls = []
    coordinator = WorkAnalysisCoordinator(manager, lambda *a, **k: calls.append("submit"),
                                          lambda job: calls.append("run"), lambda: SimpleNamespace(),
                                          db_factory=env.db_factory, scheduler=scheduler)
    try:
        assert coordinator.startup_recover() == 1
        state = env.db.get_work_analysis(env.run_id)
        assert state["status"] == "paused" and state["reason"] == "process_interrupted"
        assert coordinator.startup_recover() == 0
        assert calls == [] and manager.latest_job() is None
    finally:
        coordinator.close()
        env.close()


def test_fresh_manager_restart_repairs_only_latest_published_log_without_resending(tmp_path, monkeypatch):
    class SyntheticProcessExit(BaseException):
        pass

    env = make_runtime_environment(tmp_path, monkeypatch)
    old_manager = JobManager()
    executor = WorkAnalysisExecutor(env.db_factory, lambda: env.service)
    runner = JobRunner(old_manager, lambda task, context: executor.run(env.run_id, context))
    fresh_coordinator = None

    def submit_slice():
        job = old_manager.submit(JobSpec(JobSource.WEB, ["work_analysis"], JobType.WORK_ANALYSIS,
                                         {"run_id": env.run_id}))
        log_id = env.db.create_task_log("work_analysis", "Synthetic activation", job_id=job.job_id)
        old_manager.update_progress(job.job_id, log_id=log_id)
        return job, log_id

    try:
        old_job, old_log = submit_slice()

        def cancel_first_slice(unit_id):
            state = env.db.get_work_analysis(env.run_id)
            assert old_manager.request_cancel(old_job.job_id)
            env.db.request_work_analysis_stop(env.run_id, "cancel", state["version"])

        env.provider.before_response = cancel_first_slice
        runner.run(old_job.job_id)
        assert old_job.status == JobStatus.CANCELLED
        env.db.update_task_log(old_log, "cancelled")
        old_row = dict(env.db.conn.execute("SELECT * FROM task_logs WHERE id=?", (old_log,)).fetchone())
        stopped = env.db.get_work_analysis(env.run_id)
        env.db.resume_work_analysis(env.run_id, stopped["version"])
        env.provider.before_response = None
        latest_job, latest_log = submit_slice()

        def crash_before_settlement(self, lease, **kwargs):
            assert self.get_work_analysis(lease.run_id)["status"] == "succeeded"
            raise SyntheticProcessExit()

        with monkeypatch.context() as crash_patch:
            crash_patch.setattr(Database, "settle_work_analysis", crash_before_settlement)
            with pytest.raises(SyntheticProcessExit):
                runner.run(latest_job.job_id)
        before = env.db.get_work_analysis(env.run_id, include_private=True)
        assert before["shared_job_id"] == latest_job.job_id and before["shared_log_id"] == latest_log
        assert before["shared_epoch"] == before["epoch"] and before["owner_token"] is not None
        assert env.db.conn.execute("SELECT status FROM task_logs WHERE id=?", (latest_log,)).fetchone()[0] == "running"
        duplicate = env.db.create_task_log("work_analysis", "Unbound duplicate", job_id=latest_job.job_id)
        unrelated = env.db.create_task_log("bookmark", "Unrelated sync", job_id="f" * 32)
        sends_before = list(env.provider.sent_units)
        calls = []
        fresh_manager = JobManager()
        fresh_scheduler = AutoSyncScheduler(None, None, shared_job_manager=fresh_manager)
        fresh_coordinator = WorkAnalysisCoordinator(
            fresh_manager, lambda *a, **k: calls.append("submit"), lambda job: calls.append("run"),
            lambda: SimpleNamespace(), db_factory=env.db_factory, scheduler=fresh_scheduler,
        )
        assert fresh_coordinator.startup_recover() == 1
        after = env.db.get_work_analysis(env.run_id, include_private=True)
        latest_row = dict(env.db.conn.execute("SELECT * FROM task_logs WHERE id=?", (latest_log,)).fetchone())
        assert latest_row["status"] == "succeeded" and latest_row["error_message"] is None
        assert latest_row["finished_at"] == after["completed_at"]
        assert latest_row["stats_json"] is None and latest_row["logs_json"] is None
        assert dict(env.db.conn.execute("SELECT * FROM task_logs WHERE id=?", (old_log,)).fetchone()) == old_row
        for log_id in (duplicate, unrelated):
            assert env.db.conn.execute("SELECT status FROM task_logs WHERE id=?", (log_id,)).fetchone()[0] == "failed"
        assert after["status"] == "succeeded" and after["report"] == before["report"]
        assert after["owner_token"] is None and after["slice_settled"]
        assert after["active_seconds_debited"] == before["active_seconds_debited"]
        assert after["request_count"] == before["request_count"] == len(sends_before)
        assert fresh_coordinator.startup_recover() == 0
        assert fresh_coordinator.wake("completion") is False
        assert fresh_manager.latest_job() is None and calls == []
        assert env.provider.sent_units == sends_before and env.db.queued_work_analysis_ids() == []
    finally:
        if fresh_coordinator is not None:
            fresh_coordinator.close()
        env.close()


def test_failed_slot_reservation_retries_same_shared_item_on_release(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        assert bridge.manager.acquire_run_slot()
        bridge.coordinator.wake("created")
        active, waiting = bridge.manager.shared_admission_snapshot()
        assert active == [] and len(waiting) == 1
        assert bridge.threads == []
        original_id = waiting[0].job_id
        bridge.release_provider.set()
        bridge.manager.release_run_slot()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "succeeded")
        assert bridge.manager.get_job(original_id).status == JobStatus.SUCCEEDED
        assert len(bridge.manager._jobs) == 1
    finally:
        bridge.close()


def test_manual_work_watcher_yields_to_new_due_sync_even_after_many_yields(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch, auto_enabled=True)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        bridge.scheduler._task_preempt_streak["work_analysis"] = 100
        bridge.scheduler._task_preempted_at["work_analysis"] = time.time()
        bridge.scheduler._task_next_run["bookmarks"] = time.time() - 1
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "pausing")
        assert bridge.env.db.get_work_analysis(bridge.env.run_id)["reason"] == "sync_yield"
        bridge.release_provider.set()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "paused")
        assert bridge.starts == ["work_analysis"]
        assert bridge.scheduler._task_next_run["bookmarks"] < time.time()
        # No scheduler thread runs in this fixture. Clear the synthetic due
        # entry to model its completion; the ordinary completion wake resumes.
        bridge.scheduler._task_next_run.clear()
        bridge.coordinator.wake("completion")
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "succeeded")
    finally:
        bridge.close()
```

- [ ] **Step 6.6: Run the red coordinator suite.**

Run: `python -m pytest tests/test_work_analysis_coordinator.py -q`

Expected: missing coordinator module before implementation. Once imported, the tests must exercise real parent CAS, real request debits, real cancellation/finalization, and the existing runner. Do not replace the parent database with an always-successful fake.

- [ ] **Step 6.7: Implement the coordinator without a worker pool or side queue.**

Create `work_analysis/coordinator.py`:

```python
from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from ..jobs.models import JobSource, JobSpec, JobStatus, JobType
from .contracts import WorkAnalysisError

logger = logging.getLogger(__name__)
_TERMINAL = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
_SYNC_PRIORITY = {
    "bookmark": 1, "subscribed_series": 2, "following_users": 3,
    "following_novels": 3, "user_status": 3, "novel_status": 3,
    "series_status": 3, "user_backup": 3, "pending_deletion_detection": 3,
}


def _checked_run_id(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise WorkAnalysisError("invalid_run_id", 400)
    return value


class WorkAnalysisCoordinator:
    def __init__(self, manager, submit_shared, run_shared, settings_provider, *, db_factory, scheduler):
        self.manager = manager
        self.submit_shared = submit_shared
        self.run_shared = run_shared
        self.settings_provider = settings_provider
        self.db_factory = db_factory
        self.scheduler = scheduler
        self._gate = threading.RLock()
        self._recovered = False
        self._closed = False
        self.manager.set_slot_released_callback(lambda: self.wake("slot_released"))

    @contextmanager
    def work_control_scope(self):
        with self._gate:
            yield

    def _read(self, run_id):
        db = self.db_factory()
        try:
            state = db.get_work_analysis(run_id)
            if state is None:
                raise WorkAnalysisError("not_found", 404)
            return state
        finally:
            db.close()

    def _job_for_run(self, run_id):
        active, waiting = self.manager.shared_admission_snapshot()
        matches = [job for job in active + waiting
                   if job.spec.job_type == JobType.WORK_ANALYSIS and job.spec.params == {"run_id": run_id}]
        if len(matches) > 1:
            raise WorkAnalysisError("admission_conflict", 409)
        return matches[0] if matches else None

    def _due_priority(self):
        settings = self.settings_provider()
        sync = getattr(settings, "sync", None)
        if not self.scheduler.is_running() or not bool(getattr(sync, "auto_sync_enabled", False)):
            return None
        return self.scheduler._highest_due_priority(settings, "work_analysis")

    def challenger_priority(self):
        active, waiting = self.manager.shared_admission_snapshot()
        priorities = [job.shared_priority for job in waiting
                      if job.spec.job_type != JobType.WORK_ANALYSIS and job.status == JobStatus.QUEUED]
        due = self._due_priority()
        if due is not None:
            priorities.append(due)
        return min(priorities) if priorities else None

    def startup_recover(self):
        with self._gate:
            if self._closed:
                raise WorkAnalysisError("admission_unavailable", 503)
            if self._recovered:
                return 0
            db = self.db_factory()
            try:
                stale_count = db.fail_stale_task_logs()
                count = db.recover_work_analyses(now=datetime.now(timezone.utc), process_start=True)
                repaired_count = db.reconcile_work_analysis_task_logs()
            finally:
                db.close()
            if stale_count or repaired_count:
                logger.info("Recovered %d stale shared logs; reconciled %d work logs", stale_count, repaired_count)
            self._recovered = True
            return count

    def _launch(self, job):
        if not self.manager.reserve_shared_start(job.job_id):
            return False
        try:
            self.run_shared(job.job_id)
        except Exception:
            self.manager.abandon_reserved_start(job.job_id)
            self.manager.mark_failed(job.job_id, "shared_launch_failed")
            logger.warning("Shared work launch failed")
            return False
        return True

    def _cancel_waiting(self, job):
        self.manager.mark_cancelled(job.job_id)
        log_id = job.progress.get("log_id")
        if type(log_id) is not int:
            return
        db = self.db_factory()
        try:
            db.update_task_log(log_id, "cancelled", error_message="cancelled before admission")
        except Exception:
            logger.warning("Waiting shared task log finalization failed")
        finally:
            db.close()

    def wake(self, reason):
        if reason not in {"created", "resumed", "completion", "slot_released", "scheduler_tick", "manual_sync"}:
            raise ValueError("invalid_work_wake")
        with self._gate:
            if self._closed or not self._recovered:
                return False
            db = self.db_factory()
            try:
                db.recover_work_analyses(now=datetime.now(timezone.utc), process_start=False)
            finally:
                db.close()
            active, waiting = self.manager.shared_admission_snapshot()
            for job in waiting:
                if job.status == JobStatus.CANCEL_REQUESTED:
                    self._cancel_waiting(job)
            active, waiting = self.manager.shared_admission_snapshot()
            if active:
                work = next((job for job in active if job.spec.job_type == JobType.WORK_ANALYSIS), None)
                if work is not None and self.challenger_priority() is not None:
                    self.cancel_shared_job(work.job_id, action="sync_yield")
                return work is not None or any(job.shared_priority is not None for job in active)
            due = self._due_priority()
            if waiting:
                job = waiting[0]
                if due is not None and due < job.shared_priority:
                    return False
                if job.spec.job_type == JobType.WORK_ANALYSIS:
                    state = self._read(job.spec.params["run_id"])
                    if state["deleted"] or not (state["status"] == "queued" or
                                                  (state["status"] == "paused" and state["reason"] == "sync_yield")):
                        self._cancel_waiting(job)
                        return self.wake("completion")
                self._launch(job)
                return True
            if due is not None:
                # Leave the existing scheduler to submit its due job. The
                # scheduler tick does not consume/retry that due entry early.
                return False
            db = self.db_factory()
            try:
                candidates = db.queued_work_analysis_ids()
            finally:
                db.close()
            if not candidates:
                return False
            run_id = candidates[0]
            spec = JobSpec(JobSource.WEB, ["work_analysis"], JobType.WORK_ANALYSIS, {"run_id": run_id})
            try:
                job = self.submit_shared(
                    spec, self.settings_provider(), "work_analysis", "Work analysis",
                    run_async=False, work_admission_priority=4,
                )
            except RuntimeError:
                return False
            self._launch(job)
            return True

    def request_sync(self, spec, settings, task_type, task_name, *, progress=None):
        kinds = [name.split(":", 1)[0] for name in spec.task_types]
        if spec.source != JobSource.WEB or not kinds or any(name not in _SYNC_PRIORITY for name in kinds):
            return self.submit_shared(spec, settings, task_type, task_name, progress=progress)
        with self._gate:
            active, waiting = self.manager.shared_admission_snapshot()
            work_present = any(job.spec.job_type == JobType.WORK_ANALYSIS for job in active + waiting)
            bridge_waiters = any(job.shared_priority is not None for job in waiting)
            if not work_present and not bridge_waiters:
                return self.submit_shared(spec, settings, task_type, task_name, progress=progress)
            job = self.submit_shared(
                spec, settings, task_type, task_name, progress=progress,
                run_async=False, work_admission_priority=min(_SYNC_PRIORITY[name] for name in kinds),
            )
            self.wake("manual_sync")
            return job

    def request_control(self, run_id, action, expected_version):
        run_id = _checked_run_id(run_id)
        if action not in {"pause", "cancel", "resume", "delete", "sync_yield"}:
            raise WorkAnalysisError("invalid_action", 400)
        if type(expected_version) is not int or expected_version < 1:
            raise WorkAnalysisError("invalid_version", 400)
        with self._gate:
            if self._closed or not self._recovered:
                raise WorkAnalysisError("admission_unavailable", 503)
            before = self._read(run_id)
            if before["deleted"] and action != "delete":
                raise WorkAnalysisError("work_deleted", 410)
            if before["version"] != expected_version and not (before["deleted"] and action == "delete"):
                raise WorkAnalysisError("version_conflict", 409)
            job = self._job_for_run(run_id)
            if action == "resume":
                if job is not None:
                    raise WorkAnalysisError("state_conflict", 409)
            elif job is not None:
                if not self.manager.request_cancel(job.job_id):
                    raise WorkAnalysisError("finalizing", 409)
            db = self.db_factory()
            try:
                if action == "resume":
                    result = db.resume_work_analysis(run_id, expected_version)
                else:
                    result = db.request_work_analysis_stop(run_id, action, expected_version)
            except WorkAnalysisError:
                raise
            except Exception:
                # Cancellation stays latched even if the durable stop failed.
                # The caller must read back; it must not receive success.
                raise WorkAnalysisError("control_unavailable", 503) from None
            finally:
                db.close()
            if job is not None and job.shared_waiting:
                self._cancel_waiting(job)
        try:
            self.wake("resumed" if action == "resume" else "completion")
        except Exception:
            logger.warning("Work remains queued after admission notification failure")
        return result

    def _request_sync_yield(self, job):
        # Caller holds _gate; work cannot claim or classify a stop until this
        # operation resolves. Storage CAS remains strict on every attempt.
        run_id = job.spec.params["run_id"]
        db = self.db_factory()
        try:
            original = db.get_work_analysis_control(run_id)
            if original is None or original["deleted_at"] is not None or original["invalidated_at"] is not None:
                return False
            if original["status"] in {"pausing", "paused", "cancelling", "cancelled"}:
                return original["reason"] == "sync_yield"
            if original["owner_token"] is None:
                if original["status"] != "queued":
                    return False
                self.request_control(run_id, "sync_yield", original["version"])
                return True
            if original["status"] != "running" or not self.manager.request_cancel(job.job_id):
                return False
            keys = ("run_id", "epoch", "owner_token", "manifest_fingerprint", "configuration_fingerprint",
                    "request_limit", "token_limit", "active_seconds_limit")
            expected_identity = tuple(original[key] for key in keys)
            current = original
            for attempt in range(3):
                try:
                    db.request_work_analysis_stop(run_id, "sync_yield", current["version"])
                    return True
                except WorkAnalysisError as error:
                    if error.code != "version_conflict":
                        raise
                current = db.get_work_analysis_control(run_id)
                if (current is None or current["deleted_at"] is not None or current["invalidated_at"] is not None
                        or tuple(current[key] for key in keys) != expected_identity):
                    return False
                if current["status"] == "pausing" and current["reason"] == "sync_yield":
                    return True
                if current["status"] != "running" or current["reason"] is not None:
                    return False
            raise WorkAnalysisError("control_unavailable", 503)
        except WorkAnalysisError:
            raise
        except Exception:
            raise WorkAnalysisError("control_unavailable", 503) from None
        finally:
            db.close()

    def cancel_shared_job(self, job_id, action="cancel"):
        if action not in {"cancel", "sync_yield"}:
            raise ValueError("invalid_shared_control")
        with self._gate:
            if self._closed or not self._recovered:
                raise WorkAnalysisError("admission_unavailable", 503)
            job = self.manager.get_job(job_id)
            if job is None:
                return False
            if job.spec.job_type != JobType.WORK_ANALYSIS:
                accepted = self.manager.request_cancel(job_id)
                if accepted and job.shared_waiting:
                    self._cancel_waiting(job)
                    self.wake("completion")
                return accepted
            if job.status in _TERMINAL:
                return False
            try:
                if action == "sync_yield":
                    return self._request_sync_yield(job)
                state = self._read(job.spec.params["run_id"])
                self.request_control(state["run_id"], action, state["version"])
            except WorkAnalysisError as error:
                if error.code in {"finalizing", "state_conflict", "version_conflict", "work_deleted"}:
                    return False
                raise
            return True

    def on_job_finished(self, job_id):
        job = self.manager.get_job(job_id)
        if job is not None and job.spec.job_type == JobType.WORK_ANALYSIS:
            with self._gate:
                with self.manager._lock:
                    latest = next((item for item in reversed(self.manager._jobs.values())
                                   if item.spec.job_type == JobType.WORK_ANALYSIS
                                   and item.spec.params == job.spec.params), None)
                state = self._read(job.spec.params["run_id"])
                if latest is job and state["status"] == "succeeded" and not state["deleted"]:
                    # Publication may have committed before a memory callback
                    # failed. Never regenerate that already-published report.
                    self.manager.mark_succeeded(job_id)
                elif job.status in _TERMINAL and state["status"] in {"paused", "cancelled", "invalidated"}:
                    self.manager.mark_cancelled(job_id)
        self.wake("completion")

    def close(self):
        with self._gate:
            active, waiting = self.manager.shared_admission_snapshot()
            if active or waiting:
                raise RuntimeError("shared_owner_still_active")
            self._closed = True
            self.manager.set_slot_released_callback(None)
```

Only `sync_yield` retains automatic resumption eligibility. The private yield path makes at most three strict stop-CAS attempts, refreshing only the narrow control projection after a version conflict. A changed owner, epoch, source/configuration identity, budget ceiling, deletion/invalidation, user pause, or cancellation never authorizes a retry. Exhaustion/storage failure raises `control_unavailable` and leaves memory cancellation latched; it never acknowledges success or grants replay. Public `request_control` remains single-attempt strict CAS even when the conflicting version came from harmless progress. A source deletion/invalidation still wins through Task 3's immediate epoch revocation; a normal stop retains only heartbeat, usage merge, and one settlement. The coordinator does not settle a still-running worker or release its slot to make cancellation look immediate.

- [ ] **Step 6.8: Extend the existing scheduler watcher and tick, without a work schedule entry.**

Append these tests to `tests/test_scheduler_priority.py`:

```python
def test_work_analysis_priority_is_four_but_it_is_never_an_automatic_schedule():
    assert scheduler_task_priority("work_analysis") == 4
    assert scheduler_task_is_preemptible("work_analysis") is True
    assert all(config["name"] != "work_analysis" for config in SCHEDULER_TASK_CONFIGS)
    scheduler = AutoSyncScheduler(None, None)
    scheduler._task_preempt_streak["work_analysis"] = 100
    scheduler._task_preempted_at["work_analysis"] = time.time()
    assert scheduler._may_preempt("work_analysis", time.time()) is True


def test_work_yield_uses_durable_control_adapter_not_generic_memory_cancel():
    calls = []
    scheduler = AutoSyncScheduler(None, None,
        cancel_task=lambda job_id: calls.append(("wrong-generic", job_id)) or True)
    scheduler.work_coordinator = SimpleNamespace(
        cancel_shared_job=lambda job_id, action: calls.append((action, job_id)) or True,
    )
    assert scheduler._request_yield("work-job", "work_analysis", 1)
    assert calls == [("sync_yield", "work-job")]
```

Red command: `python -m pytest tests/test_scheduler_priority.py -q -k "work_analysis_priority or work_yield"`.

Expected: the old default priority is 3 and its watcher uses the generic cancellation path. Then make these exact changes in `web/managers.py`:

Insert this first branch in `scheduler_task_priority` before its config lookup:

```python
    if task_name == "work_analysis":
        return 4
```

Insert this first branch in `scheduler_task_is_preemptible` before its config lookup:

```python
    if task_name == "work_analysis":
        return True
```

Append this defaulted field to the `AutoSyncScheduler` dataclass; do not add a row to `SCHEDULER_TASK_CONFIGS`:

```python
    work_coordinator: Any = field(default=None, repr=False)
```

At the beginning of `_may_preempt`, before the existing generic cooldown/streak checks, insert:

```python
        if task_name == "work_analysis":
            return True
```

In `_run_scheduler_loop`, replace only the existing disabled-auto-sync branch with:

```python
                if not settings.sync.auto_sync_enabled:
                    if self.work_coordinator is not None:
                        self.work_coordinator.wake("scheduler_tick")
                    stop_event.wait(60)
                    continue
```

Immediately before `due_tasks = self._collect_due_tasks(settings)`, after the existing schedule initialization loop has released its lock, insert:

```python
                if self.work_coordinator is not None and self.work_coordinator.wake("scheduler_tick"):
                    stop_event.wait(SCHEDULER_PREEMPT_POLL_SECONDS)
                    continue
```

This leaves a due sync entry due while a work analysis drains; it does not turn a failed attempt against the occupied slot into a new 60/300-second due time. When no work/bridge job occupies the slot, the original scheduler submits its due task using its existing bookkeeping and watcher.

Replace the complete `_run_and_watch_for_preemption` method with:

```python
    def _run_and_watch_for_preemption(self, settings, job_id, task_name, run_task):
        if not scheduler_task_is_preemptible(task_name):
            run_task(job_id)
            return
        errors = []

        def worker():
            try:
                run_task(job_id)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=worker, name=f"auto-sync-{task_name}", daemon=True)
        thread.start()
        running_priority = scheduler_task_priority(task_name)
        yielded = False
        is_work = task_name == "work_analysis"
        while thread.is_alive():
            thread.join(timeout=SCHEDULER_PREEMPT_POLL_SECONDS)
            if not thread.is_alive() or yielded or (self._stop_event.is_set() and not is_work):
                continue
            try:
                if is_work:
                    challenger = self.work_coordinator.challenger_priority() if self.work_coordinator is not None else None
                else:
                    challenger = self._highest_due_priority(settings, task_name)
            except Exception:
                logger.warning("Shared preemption check failed")
                continue
            if challenger is None or challenger >= running_priority:
                continue
            now = time.time()
            if self._may_preempt(task_name, now) and self._request_yield(job_id, task_name, challenger):
                if not is_work:
                    self._note_preemption(task_name, now)
                yielded = True
        thread.join()
        if errors:
            raise errors[0]
```

At the beginning of `_request_yield`, before its existing message/log/generic-cancel path, insert:

```python
        if task_name == "work_analysis":
            if self.work_coordinator is None:
                return False
            try:
                return self.work_coordinator.cancel_shared_job(job_id, action="sync_yield")
            except Exception:
                logger.warning("Work yield control failed")
                return False
```

The manual work watcher remains alive while its worker is alive even if `AutoSyncScheduler.stop()` has set its scheduler stop event. Its coordinator still sees manual pending sync jobs; disabled/stopped automatic synchronization contributes no due candidates. Existing non-work watcher cooldowns and streak rules are unchanged.

Green commands:

`python -m pytest tests/test_work_analysis_coordinator.py tests/test_scheduler_priority.py tests/test_scheduler_persistence.py -q`

`python -m pytest tests/test_work_analysis_executor.py tests/test_work_analysis_storage.py -q`

Expected: pass; the manual-off test observes no early slot release, one conserved first request debit, a short normal-yield active-time charge, then the same parent resumes after sync completion. The owner/source/usage tests remain Task 3's real transactional assertions.

- [ ] **Step 6.9: Add failing real-Web admission/lifecycle tests.**

Create `tests/test_work_analysis_web_admission.py`. All databases/configs are synthetic temporary files. The tests use the real app's submit/log/runner bridge, no production credentials or paid network. An unset Dashboard token intentionally follows the existing unauthenticated-test fixture convention; the authenticated/CSRF negative tests remain Task 7/8's responsibility.

```python
from __future__ import annotations

import threading
import time

import pytest

import pixiv_novel_sync.webapp as webapp_module
from pixiv_novel_sync.jobs.models import JobSource, JobSpec, JobStatus, JobType
from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError
from pixiv_novel_sync.work_analysis.executor import WorkAnalysisExecutor
from work_analysis_runtime_helpers import make_runtime_environment


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Web worker did not reach the expected state")
        time.sleep(0.005)


@pytest.fixture
def web_work(tmp_path, monkeypatch):
    env = make_runtime_environment(tmp_path, monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("sync:\n  auto_sync_enabled: false\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=synthetic-never-used\n", encoding="utf-8")
    monkeypatch.setenv("PIXIV_DB_PATH", str(env.db.path))
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "synthetic-test-secret")
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("FLASK_DEBUG", raising=False)
    monkeypatch.setattr("pixiv_novel_sync.web.managers.SCHEDULER_PREEMPT_POLL_SECONDS", 0.01)
    monkeypatch.setattr(webapp_module, "execute_task", lambda task, settings, context: {"synthetic_sync": 1})
    app = webapp_module.create_app(str(config_path), str(env_path), start_scheduler=False)
    coordinator = app.extensions["work_analysis_coordinator"]
    app.extensions["pixiv_novel_sync.work_ai_factory"] = lambda: env.service
    interrupted = env.db.get_work_analysis(env.run_id)
    env.db.resume_work_analysis(env.run_id, interrupted["version"])
    try:
        yield app, env
    finally:
        active, waiting = app.config["job_manager"].shared_admission_snapshot()
        for job in active + waiting:
            coordinator.cancel_shared_job(job.job_id)
        wait_for(lambda: not any(app.config["job_manager"].shared_admission_snapshot()))
        app.extensions["close_work_analysis_owner"]()
        env.close()


def test_web_manual_off_creation_uses_the_real_shared_runner_and_id_only_params(web_work):
    app, env = web_work
    app.extensions["work_analysis_coordinator"].wake("created")
    wait_for(lambda: env.db.get_work_analysis(env.run_id)["status"] == "succeeded")
    manager = app.config["job_manager"]
    jobs = list(manager._jobs.values())
    assert len(jobs) == 1 and jobs[0].spec.params == {"run_id": env.run_id}
    assert jobs[0].spec.job_type == JobType.WORK_ANALYSIS
    assert env.db.get_work_analysis_report(env.run_id)["synthesis_complete"] is True
    assert app.config["auto_sync_scheduler"].is_running() is False


def test_generic_http_cancel_cannot_report_success_before_parent_stop(web_work):
    app, env = web_work
    entered, release = threading.Event(), threading.Event()

    def hold(unit_id):
        entered.set()
        assert release.wait(timeout=5)

    env.provider.before_response = hold
    try:
        app.extensions["work_analysis_coordinator"].wake("created")
        assert entered.wait(timeout=5)
        job = app.config["job_manager"].shared_admission_snapshot()[0][0]
        response = app.test_client().post("/api/dashboard/sync/cancel", json={"job_id": job.job_id})
        assert response.status_code == 200
        assert env.db.get_work_analysis(env.run_id)["status"] == "cancelling"
        assert app.config["job_manager"].acquire_run_slot() is False
    finally:
        release.set()
    wait_for(lambda: env.db.get_work_analysis(env.run_id)["status"] == "cancelled")


def test_manual_p1_enters_existing_manager_while_work_is_draining(web_work):
    app, env = web_work
    entered, release = threading.Event(), threading.Event()

    def hold(unit_id):
        entered.set()
        assert release.wait(timeout=5)

    env.provider.before_response = hold
    try:
        app.extensions["work_analysis_coordinator"].wake("created")
        assert entered.wait(timeout=5)
        response = app.test_client().post("/api/dashboard/sync/bookmark")
        assert response.status_code == 200
        manager = app.config["job_manager"]
        active, waiting = manager.shared_admission_snapshot()
        assert len(active) == len(waiting) == 1
        assert waiting[0].spec.task_types == ["bookmark"] and waiting[0].started_at is None
        assert env.db.get_work_analysis(env.run_id)["reason"] == "sync_yield"
    finally:
        release.set()
    wait_for(lambda: env.db.get_work_analysis(env.run_id)["status"] == "succeeded")
    assert [job.spec.task_types for job in manager._jobs.values()] == [["work_analysis"], ["bookmark"], ["work_analysis"]]


def test_pending_work_placeholder_does_not_block_a_due_p1_scheduler_submission(web_work):
    app, env = web_work
    manager = app.config["job_manager"]
    assert manager.acquire_run_slot()
    coordinator = app.extensions["work_analysis_coordinator"]
    coordinator.wake("created")
    work = manager.shared_admission_snapshot()[1][0]
    submit = app.config["submit_shared_web_job"]
    scheduler = app.config["auto_sync_scheduler"]
    settings = coordinator.settings_provider()
    sync = submit(webapp_module._scheduler_job_spec("bookmarks"), settings,
                  "bookmark", "Synthetic scheduled bookmark", is_auto_sync=True,
                  run_async=False, scheduler_priority=1)
    assert work.shared_waiting and sync.status == JobStatus.QUEUED
    # The unrelated test holder releases without an ordinary sync worker yet.
    # Its callback cannot launch work past the already-admitted sync item.
    manager.release_run_slot()
    assert work.shared_waiting and work.started_at is None
    app.config["run_shared_job"](sync.job_id)
    wait_for(lambda: env.db.get_work_analysis(env.run_id)["status"] == "succeeded")


def test_secondary_app_reuses_stopped_owner_and_does_not_recover_live_parent_again(web_work):
    app, env = web_work
    coordinator = app.extensions["work_analysis_coordinator"]
    second = webapp_module.create_app(app.config["work_config_path"], app.config["work_env_path"], start_scheduler=False)
    assert second.config["job_manager"] is app.config["job_manager"]
    assert second.extensions["work_analysis_coordinator"] is coordinator
    assert coordinator.startup_recover() == 0
    assert env.db.get_work_analysis(env.run_id)["status"] == "queued"
    assert not app.config["auto_sync_scheduler"].is_running()


def test_non_web_work_execution_remains_unsupported(web_work):
    app, env = web_work
    from pixiv_novel_sync.jobs.tasks import execute_task

    with pytest.raises(RuntimeError, match="Unsupported task type"):
        execute_task("work_analysis", app.extensions["work_analysis_coordinator"].settings_provider(), {})
    assert env.provider.sent_units == []
```

Fixture path note: `Database`'s actual path attribute is `path` at this baseline; this is a temporary fixture database. `work_config_path`/`work_env_path` below are test-readable local app configuration, not HTTP output or a shared-job parameter.

Red command: `python -m pytest tests/test_work_analysis_web_admission.py -q`.

Expected: missing `app.extensions['work_analysis_coordinator']`/bridge options. Do not run these tests against an initialized real user database to get past a fixture failure.

- [ ] **Step 6.10: Wire the sole Web owner and real submit/completion functions.**

In `webapp.py`, add these imports beside the current job/scheduler imports:

```python
from .web.managers import scheduler_task_priority
from .work_analysis.coordinator import WorkAnalysisCoordinator
from .work_analysis.contracts import WorkAnalysisError
from .work_analysis.executor import WorkAnalysisExecutor
```

Extend the existing jobs model import to:

```python
from .jobs.models import JobSource, JobSpec, JobState, JobStatus, JobType
```

In `_release_scheduler_owner`, inside its existing registry lock and before popping the matching owner, insert this guard:

```python
        if current is not None and current.scheduler is scheduler and scheduler.work_coordinator is not None:
            return
```

A coordinator attachment pins the EXISTING owner, not a second owner registry. Unattached standalone scheduler instances preserve their previous release behavior. The teardown closure below detaches the coordinator explicitly before calling this release function.

In `create_app`, replace the original `fail_stale_task_logs()` startup block before scheduler-owner selection with schema preparation only. Stale-log cleanup moves into the coordinator's serialized `startup_recover`, immediately before parent recovery and exact-log reconciliation. It must not run separately after the owner is attached: a secondary app could otherwise finish recovery/open admission before that late cleanup marks a new live task failed. Keep stale trash sweeping at its existing location.

```python
    try:
        startup_db = _open_database(settings_manager.load(env_path=env_path))
        startup_db.close()
    except Exception:
        logger.warning("Shared startup schema preparation unavailable")
```

Set these local holders immediately before `run_web_task`:

```python
    work_coordinator = None
    work_ai_factory = None
```

Replace `run_web_task` with this full function:

```python
    def run_web_task(task_type: str, context: dict[str, Any]) -> dict[str, Any] | None:
        current_settings = settings_manager.load(env_path=env_path)
        if task_type == "work_analysis":
            if work_coordinator is None or work_ai_factory is None:
                raise RuntimeError("work_admission_unavailable")
            context = {**context, "work_control_scope": work_coordinator.work_control_scope}
            return WorkAnalysisExecutor(work_coordinator.db_factory, work_ai_factory).run(
                context["params"]["run_id"], context,
            )
        return execute_task(task_type, current_settings, context)
```

Keep `_has_active_shared_jobs` and `_has_any_running_web_job` for ordinary callers. Add this helper before `_submit_shared_job`; it is called only under the manager's lock, and performs no database IO:

```python
    def _check_shared_admission(spec, work_admission_priority, scheduler_priority):
        if work_admission_priority is not None:
            try:
                shared_job_manager.check_work_waiting_admission(spec, work_admission_priority)
            except RuntimeError as error:
                if str(error) == "shared_pending_full":
                    raise RuntimeError("已有同步任务正在运行，等待队列已满") from None
                raise RuntimeError("已有同步任务正在运行，请稍后再试") from None
            return
        active, waiting = shared_job_manager.shared_admission_snapshot()
        if scheduler_priority is not None:
            if spec.source != JobSource.SCHEDULER or type(scheduler_priority) is not int or not 1 <= scheduler_priority <= 3:
                raise ValueError("invalid_scheduler_admission")
            if not active and waiting and scheduler_priority < waiting[0].shared_priority:
                return
        if active or waiting:
            raise RuntimeError("已有同步任务正在运行，请稍后再试")
```

Replace `_run_shared_web_job` with these three complete functions. `_persist_shared_job_result` preserves the previous log pruning; business artifacts never enter its arguments. The work completion callback is reached even for jobs without `log_id`, terminal errors, and publication-to-memory reconciliation:

```python
    def _persist_shared_job_result(job_id: str) -> None:
        job = shared_job_manager.get_job(job_id)
        if job is None or job.status not in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
            return
        log_id = job.progress.get("log_id")
        if not log_id:
            return
        current_settings = settings_manager.load(env_path=env_path)
        db = Database(current_settings.storage.db_path)
        try:
            prepare_schema(db)
            logs = _shared_job_logs_for_db(job)
            if job.status == JobStatus.SUCCEEDED:
                db.update_task_log(log_id, _task_log_status_for_stats(job.stats),
                                   stats=_prune_stats_for_log(job.stats), logs=logs)
            elif job.status == JobStatus.FAILED:
                db.update_task_log(log_id, JobStatus.FAILED.value, error_message=job.error, logs=logs)
            else:
                db.update_task_log(log_id, JobStatus.CANCELLED.value, logs=logs)
        except Exception:
            logger.error("Shared task log finalization failed")
        finally:
            db.close()

    def _run_shared_web_job(job_id: str) -> None:
        if shared_job_runner is None:
            raise RuntimeError("shared job runner is not initialized")
        try:
            shared_job_runner.run(job_id)
        finally:
            try:
                if work_coordinator is not None:
                    work_coordinator.on_job_finished(job_id)
            except Exception:
                logger.warning("Shared work completion reconciliation failed")
            _persist_shared_job_result(job_id)

    def _launch_shared_web_job(job_id: str) -> None:
        def run():
            try:
                job = shared_job_manager.get_job(job_id)
                if job is not None and job.spec.job_type == JobType.WORK_ANALYSIS:
                    auto_sync_scheduler._run_and_watch_for_preemption(
                        settings_manager.load(env_path=env_path), job_id, "work_analysis", _run_shared_web_job,
                    )
                else:
                    _run_shared_web_job(job_id)
            except Exception:
                if shared_job_manager.abandon_reserved_start(job_id):
                    shared_job_manager.mark_failed(job_id, "shared_launch_failed")
                    _persist_shared_job_result(job_id)
                logger.warning("Shared work watcher failed")

        thread = threading.Thread(target=run, daemon=True)
        try:
            thread.start()
        except Exception:
            shared_job_manager.abandon_reserved_start(job_id)
            shared_job_manager.mark_failed(job_id, "shared_launch_failed")
            _persist_shared_job_result(job_id)
            raise RuntimeError("shared_launch_failed") from None
```

Append these two keyword-only parameters to `_submit_shared_job`, after `run_async`:

```python
        work_admission_priority: int | None = None,
        scheduler_priority: int | None = None,
```

At the beginning of `_submit_shared_job`, before its nested log helper or any database access, insert:

```python
        if work_admission_priority is not None and run_async:
            raise ValueError("waiting work jobs require coordinator admission")
```

Replace BOTH existing `if _has_any_running_web_job(): raise RuntimeError(...)` checks inside the manager-lock scopes with:

```python
            _check_shared_admission(spec, work_admission_priority, scheduler_priority)
```

At the second check, replace `job = shared_job_manager.submit(spec)` with:

```python
                if work_admission_priority is None:
                    job = shared_job_manager.submit(spec)
                else:
                    job = shared_job_manager.submit_work_waiting(spec, work_admission_priority)
```

Preserve the existing create-log-before-submission, second check, failed-log handling, ID backfill, and progress-update code. Replace only its final `if run_async` body with:

```python
        if run_async:
            _launch_shared_web_job(job.job_id)
```

In `_submit_scheduler_task`, append this keyword to its existing `_submit_shared_job(...)` call:

```python
            scheduler_priority=scheduler_task_priority(task_name),
```

This narrow exception lets a due P1 scheduler job overtake a P4 waiting placeholder (or a lower-priority manual waiter) inside the same manager. It cannot overtake a running job, and it does not change normal busy handling for unrelated tasks.

Replace the complete scheduler-owner selection `if should_start_scheduler: ... else: ...` block with:

```python
    registry_settings = settings_manager.load(env_path=env_path)
    registry_key = _scheduler_registry_key(registry_settings.storage.db_path)
    auto_sync_scheduler, scheduler_created = _get_or_create_scheduler_owner(
        registry_key, config_path=config_path, env_path=env_path,
        job_manager=shared_job_manager, submit_task=_submit_scheduler_task,
        run_task=_run_shared_web_job, get_task=shared_job_manager.get_job,
        cancel_task=shared_job_manager.request_cancel,
    )
    shared_job_manager = auto_sync_scheduler.shared_job_manager
    start_auto_sync_scheduler = should_start_scheduler and (
        scheduler_created or not auto_sync_scheduler.is_running()
    )
```

`start_scheduler=False` still starts ZERO scheduler threads. It now participates in the same owner registry, so disabling periodic synchronization cannot create a second paid worker lane.

Immediately after `shared_job_runner = JobRunner(shared_job_manager, run_web_task)`, insert:

```python
    app.config["work_config_path"] = config_path
    app.config["work_env_path"] = env_path

    def work_database():
        current = settings_manager.load(env_path=env_path)
        if _scheduler_registry_key(current.storage.db_path) != registry_key or not current.storage.db_path.is_file():
            raise WorkAnalysisError("storage_unavailable", 503)
        return Database(current.storage.db_path)

    with _auto_sync_scheduler_registry_lock:
        work_coordinator = auto_sync_scheduler.work_coordinator
        new_work_owner = work_coordinator is None
        if new_work_owner:
            work_coordinator = WorkAnalysisCoordinator(
                shared_job_manager, _submit_shared_job, _launch_shared_web_job,
                lambda: settings_manager.load(env_path=env_path),
                db_factory=work_database, scheduler=auto_sync_scheduler,
            )
            auto_sync_scheduler.work_coordinator = work_coordinator
            auto_sync_scheduler.cancel_task = work_coordinator.cancel_shared_job
    app.extensions["work_analysis_coordinator"] = work_coordinator

    try:
        work_coordinator.startup_recover()
    except Exception:
        # The coordinator remains closed to admission until an explicit
        # startup retry succeeds; other dashboard features stay available.
        logger.warning("Work analysis startup recovery unavailable")

    def close_work_analysis_owner():
        # Application/process teardown only, never an HTTP auto-sync action.
        # Call after request serving stops and existing workers have drained.
        work_coordinator.close()
        auto_sync_scheduler.stop()
        thread = auto_sync_scheduler._thread
        if thread is not None and thread.is_alive():
            raise RuntimeError("shared_owner_still_active")
        with _auto_sync_scheduler_registry_lock:
            if auto_sync_scheduler.work_coordinator is work_coordinator:
                auto_sync_scheduler.work_coordinator = None
        _release_scheduler_owner(registry_key, auto_sync_scheduler)

    app.extensions["close_work_analysis_owner"] = close_work_analysis_owner
```

The registry lock above protects only attachment construction. Constructor code performs no database access; the recovery call occurs after releasing it. Generic stale-log cleanup, conservative parent recovery, and exact latest-log reconciliation are serialized together once by the coordinator, so a secondary app cannot pause a live parent or rewrite its live task log. Reconciliation trusts only the latest stored shared job/log/epoch association on a committed successful parent; it never recreates a pruned row, rewrites earlier cancelled slices, or copies reports/artifacts into generic logs. The existing process-scoped registry remains the only owner cache. The idle-only teardown hook gives test/server shutdown a concrete release path; it is not a user-facing control and must not be called while any app sharing the owner is still serving work. A runtime database-path change fails work admission closed until the app is restarted/rebound; it never directs an existing parent into another database.

Keep the existing `app.config['job_manager']`, `run_shared_job`, `submit_shared_web_job`, and `auto_sync_scheduler` assignments. Direct public routes must use the owner coordinator rather than a new runner. After `register_ai_routes(app, current_settings_for_routes)` and BEFORE Task 7's `register_work_analysis_routes` block, add:

```python
    app.extensions["pixiv_novel_sync.work_ai_factory"] = lambda: app.extensions["pixiv_novel_sync.ai_service"]
    work_ai_factory = lambda: app.extensions["pixiv_novel_sync.work_ai_factory"]()
```

The callback is lazy: app creation/recovery/preview do not instantiate or call a Provider. The factory may be replaced by a synthetic service in the real-Web tests. The primary owner's submission/runner closures remain authoritative; secondary app instances reuse the coordinator and manager instead of constructing an alternative execution path.

In `dashboard_sync_cancel`, replace its raw `shared_job_manager.request_cancel(job_id)` block with:

```python
        try:
            accepted = work_coordinator.cancel_shared_job(job_id)
        except WorkAnalysisError as error:
            return _api_error(error.code, error.status)
        if not accepted:
            return _api_error("任务不存在、已结束，或正在收尾", 409)
```

Leave the existing successful acknowledgement below it in place. It is now reached only after an active work parent's stop transaction succeeds. If finalization already won, the same endpoint returns 409.

Change these five manual call sites to the following exact calls; do not change scheduler submissions to `request_sync`:

```python
            job = work_coordinator.request_sync(spec, current_settings, "user_backup", f"用户 {user_id} 备份")
```

```python
            job = work_coordinator.request_sync(spec, current_settings, "manual", "全量手动同步")
```

```python
            job = work_coordinator.request_sync(spec, current_settings, internal_type, task_name)
```

```python
            job = work_coordinator.request_sync(
                spec, current_settings, "subscribed_series", "同步追更系列",
                progress={"series_limit": limit},
            )
```

```python
            job = work_coordinator.request_sync(spec, current_settings, "pending_deletion_detection", "检测取消收藏/追更")
```

Anchors are `sync_user_novels`'s submission near original line 1580, `dashboard_sync_start:1720`, `dashboard_sync_single:1740`, `dashboard_sync_subscribed_series:1778`, and `trigger_pending_detection` near original line 2131. `request_sync` delegates unrelated preference/recommendation tasks unchanged; it admits only genuine higher-priority sync tasks behind an analysis. Remove this route-local precheck from `sync_user_novels`; the coordinator plus `_submit_shared_job` now perform the same busy check for non-work jobs and the bounded waiting check for work jobs:

```python
        if _has_active_shared_jobs():
            return _api_error("已有同步任务正在运行，请稍后再试", 409)
```

Replace the success return in BOTH `dashboard_sync_single` and `dashboard_sync_subscribed_series` with this complete return; a queued synchronization must not be described as already executing:

```python
        message = "任务已排队，等待共享执行槽" if job.shared_waiting else "任务已启动"
        return jsonify({"ok": True, "message": message, "job": _shared_job_to_dict(job)})
```

Replace the success return in `trigger_pending_detection` with:

```python
        message = "检测任务已排队，等待共享执行槽" if job.shared_waiting else "检测任务已启动"
        return jsonify({"ok": True, "message": message, "job": _shared_job_to_dict(job)})
```

- [ ] **Step 6.11: Update the specific Web-owner lifecycle regression and add late-callback projection assertions.**

Replace `test_create_app_shares_scheduler_owner_by_normalized_db_path_and_releases_on_stop` in `tests/test_webapp_jobs.py` with this complete test. Only the attached Web owner now persists through an auto-sync stop; do not weaken the standalone scheduler registry tests immediately following it:

```python
def test_create_app_shares_pinned_work_owner_across_auto_sync_stop(tmp_path, monkeypatch):
    relative_db_path = "./scheduler.db"
    absolute_db_path = str((tmp_path / "scheduler.db").resolve())
    monkeypatch.chdir(tmp_path)
    start_calls = []
    original_start = AutoSyncScheduler.start

    def recording_start(self):
        start_calls.append(self)
        return original_start(self)

    def idle_scheduler(self, stop_event=None):
        (stop_event or self._stop_event).wait(timeout=5)

    monkeypatch.setattr(AutoSyncScheduler, "start", recording_start)
    monkeypatch.setattr(AutoSyncScheduler, "_run_scheduler", idle_scheduler)
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "test-secret")
    monkeypatch.setenv("PIXIV_REFRESH_TOKEN", "test-refresh-token")
    monkeypatch.delenv("FLASK_DEBUG", raising=False)
    monkeypatch.delenv("WERKZEUG_SERVER_FD", raising=False)
    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
    first_app = None
    try:
        monkeypatch.setenv("PIXIV_DB_PATH", relative_db_path)
        first_app = create_app()
        monkeypatch.setenv("PIXIV_DB_PATH", absolute_db_path)
        second_app = create_app()
        assert len(start_calls) == 1
        first_owner = start_calls[0]
        manager = first_app.config["job_manager"]
        coordinator = first_app.extensions["work_analysis_coordinator"]
        assert second_app.config["job_manager"] is manager
        assert second_app.extensions["work_analysis_coordinator"] is coordinator
        job = manager.submit(webapp_module._scheduler_job_spec("bookmarks"))
        assert manager.mark_running(job.job_id)

        class ForbiddenDatabase:
            def __init__(self, db_path):
                raise AssertionError("busy non-work submission reached the database")

        monkeypatch.setattr(webapp_module, "Database", ForbiddenDatabase)
        blocked = second_app.test_client().post("/api/dashboard/sync/user_status")
        assert blocked.status_code == 409
        assert "已有同步任务" in blocked.get_json()["error"]
        status = second_app.test_client().get(f"/api/dashboard/sync/status?job_id={job.job_id}")
        assert status.get_json()["job"]["job_id"] == job.job_id
        manager.mark_succeeded(job.job_id)
        first_owner.stop()
        third_app = create_app()
        assert len(start_calls) == 2 and start_calls[1] is first_owner
        assert third_app.config["job_manager"] is manager
        assert third_app.extensions["work_analysis_coordinator"] is coordinator
        assert coordinator.startup_recover() == 0
    finally:
        for scheduler in start_calls:
            scheduler.stop()
            worker = scheduler._thread
            if worker is not None:
                worker.join(timeout=3)
        if first_app is not None:
            first_app.extensions["close_work_analysis_owner"]()
    key = webapp_module._scheduler_registry_key(tmp_path / "scheduler.db")
    with webapp_module._auto_sync_scheduler_registry_lock:
        assert key not in webapp_module._auto_sync_scheduler_registry
```

Append these tests to `tests/test_work_analysis_coordinator.py`:

```python
def test_late_old_shared_completion_cannot_promote_its_cancelled_slice(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        bridge.coordinator.wake("created")
        assert bridge.entered.wait(timeout=5)
        old = bridge.manager.shared_admission_snapshot()[0][0]
        bridge.coordinator.request_sync(JobSpec(JobSource.WEB, ["bookmark"]), bridge.settings,
                                        "bookmark", "Synthetic sync")
        bridge.release_provider.set()
        assert bridge.sync_entered.wait(timeout=5)
        bridge.release_sync.set()
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "succeeded")
        bridge.coordinator.on_job_finished(old.job_id)
        assert old.status == JobStatus.CANCELLED
        assert bridge.env.db.get_work_analysis_report(bridge.env.run_id)["synthesis_complete"] is True
    finally:
        bridge.close()


def test_committed_report_repairs_latest_shared_projection_without_another_call(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        bridge.release_provider.set()
        bridge.coordinator.wake("created")
        wait_for(lambda: bridge.env.db.get_work_analysis(bridge.env.run_id)["status"] == "succeeded")
        for thread in list(bridge.threads):
            thread.join(timeout=5)
            assert not thread.is_alive()
        latest = bridge.manager.latest_job()
        before_calls = list(bridge.env.provider.sent_units)
        bridge.manager.mark_failed(latest.job_id, "synthetic_memory_projection_failure")
        bridge.coordinator.on_job_finished(latest.job_id)
        assert latest.status == JobStatus.SUCCEEDED
        assert bridge.env.provider.sent_units == before_calls
    finally:
        bridge.close()


def test_cancelled_waiting_parent_allows_next_explicit_intent_without_auto_scheduler(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        assert bridge.manager.acquire_run_slot()
        bridge.coordinator.wake("created")
        state = bridge.env.db.get_work_analysis(bridge.env.run_id)
        stopped = bridge.coordinator.request_control(bridge.env.run_id, "cancel", state["version"])
        assert stopped["status"] == "cancelled"
        assert bridge.manager.shared_admission_snapshot() == ([], [])
        bridge.manager.release_run_slot()
        assert bridge.env.provider.sent_units == []
    finally:
        bridge.close()


def test_expired_owner_sweep_pauses_without_paid_replay_when_auto_sync_is_off(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        lease = bridge.env.db.claim_work_analysis(bridge.env.run_id, now=datetime.now(timezone.utc), slice_seconds=30)
        before = bridge.env.db.get_work_analysis(bridge.env.run_id, include_private=True)
        with bridge.env.db.transaction() as conn:
            conn.execute("UPDATE work_analysis_runs SET lease_until='2000-01-01 00:00:00' WHERE run_id=?",
                         (bridge.env.run_id,))
        bridge.coordinator.wake("completion")
        state = bridge.env.db.get_work_analysis(bridge.env.run_id, include_private=True)
        assert state["status"] == "paused" and state["reason"] == "process_interrupted"
        assert state["active_seconds_debited"] == before["active_seconds_debited"]
        assert state["epoch"] > lease.epoch and state["owner_token"] is None
        assert bridge.env.provider.sent_units == [] and bridge.threads == []
    finally:
        bridge.close()
```

- [ ] **Step 6.12: Run the focused Web integration and shared-lifecycle regressions.**

Run: `python -m pytest tests/test_work_analysis_web_admission.py tests/test_work_analysis_coordinator.py tests/test_webapp_jobs.py tests/test_scheduler_priority.py tests/test_scheduler_persistence.py tests/test_jobs_manager.py tests/test_jobs_runner.py tests/test_jobs_concurrency.py tests/test_cli_jobs.py -q`

Expected: pass. Specifically verify the existing non-work busy path still avoids opening a database, log creation/backfill failures leave no ghost worker, scheduler-before-AI-route-registration ordering is unchanged, standalone scheduler owners still release correctly, and work owners do not split merely because periodic synchronization was disabled/stopped.

Run: `python -m pytest tests/test_ai_service_facade.py tests/test_ai_invocation_runtime.py tests/test_ai_job_routing_storage.py tests/test_ai_usage.py tests/test_work_analysis_configuration.py tests/test_work_analysis_executor.py tests/test_work_analysis_storage.py -q`

Expected: pass. This second command is the inter-task handoff gate, not a substitute for Task 9's full Windows/Linux/browser/packaging validation. Every automatic test stays on temporary databases and fake HTTP.

- [ ] **Step 6.13: Review the concurrency gate and commit only the bridge paths.**

Review gate:
- The owner is the existing normalized-path Web owner; there is no CLI work executor, automatic work schedule, second pending queue, or second semaphore.
- The manual priority-1 test observes a QUEUED item in the SAME manager while the original Provider call is still blocked; that item cannot acquire the slot early.
- The manual watcher sees newly due sync and manual waiters; a stopped automatic scheduler contributes no due candidate but cannot disable manual/completion admission.
- A normal yield stops new reservations/publication, retains the restricted epoch until exactly one monotonic settlement, and resumes only after real worker exit. Crashes/expiry/source deletion retain the full reserved time slice as Task 3 specifies.
- Manager cancellation permission precedes every active business/generic/watcher stop CAS. Rejected finalization or failed durable control is not acknowledged as successful cancellation. No path holds manager/scheduler locks across DB IO.
- Finalization is claimed before report publication; database source/deletion/owner checks still win against memory. The latest shared projection can be repaired from committed parent truth, but an old cancelled slice cannot become successful merely because a later slice completed.
- Startup recovery runs once per attached owner: generic stale-log cleanup, conservative parent recovery, then exact latest-log reconciliation. It never calls `wake`, starts a paid worker, revives old queued/sync-yield intent, or rewrites earlier cancelled logs. A secondary app shares that owner; explicit idle teardown releases it.
- Failed thread launch, slot reservation, task log write, or checkpoint validation does not hide a parent or refund a possibly sent request. A parent lacking budget moves out of `queued` before raising, rather than repeatedly consuming shared admissions.

```powershell
git add src/pixiv_novel_sync/work_analysis/coordinator.py src/pixiv_novel_sync/jobs/models.py src/pixiv_novel_sync/jobs/manager.py src/pixiv_novel_sync/jobs/runner.py src/pixiv_novel_sync/web/managers.py src/pixiv_novel_sync/webapp.py tests/test_work_analysis_coordinator.py tests/test_work_analysis_web_admission.py tests/test_jobs_manager.py tests/test_jobs_runner.py tests/test_scheduler_priority.py tests/test_webapp_jobs.py
git commit -m "feat: coordinate work analysis admission and cooperative sync preemption"
```

### Runtime Plan Self-Review And Integration Notes

- Task 1 absorbs the pure configuration prerequisite, including its full-payload preflight and private complete-candidate capacity decision. Task 4/5/6 remain ordered after Task 3's storage implementation; this plan does not create a storage/router import cycle.
- Task 3 includes the agreed private `report` key, request-only `usage_summary.network_request_count`, claim acceptance of `paused/sync_yield`, committed pause/invalidation before claim refusal, the approved fixed reason union, shared activation/log association, narrow control projection, and startup exact-log reconciliation. These interfaces are defined in [storage.md](storage.md).
- Task 7 registers the service only after the coordinator and AI facade bindings above exist. Its public response constructor must not expose `work_can_segment`, owner tokens, configuration hashes, ledger attachment rows, or internal admission metadata.
- Task 8's new module/static cache policy and Task 9's build/deployment boundary are outside this runtime plan. Plan-time syntax/synthetic checks do not constitute implementation or prove application tests pass; source changes, product migrations, paid/network calls, index changes, commits, and deployment remain unauthorized.
- The fenced tests and commands are instructions for the later implementation authorization. Their listed red/green outcomes are expected results, not claims that unimplemented code already passed.
