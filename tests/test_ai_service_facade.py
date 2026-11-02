import inspect
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

from pixiv_novel_sync.ai import service as service_facade
from pixiv_novel_sync.ai.invocation import InvocationValidationError
from pixiv_novel_sync.ai.model_router import ModelRouteConflictError
from pixiv_novel_sync.ai.models import AIStreamChunk
from pixiv_novel_sync.ai.providers import AIProviderError
from pixiv_novel_sync.ai.service import AIServiceError, AIWritingService
from pixiv_novel_sync.storage.ai.core import AIJobConflictError
from pixiv_novel_sync.storage_db import Database


def test_ai_service_facade_exports_public_api() -> None:
    assert AIWritingService.__name__ == "AIWritingService"
    assert issubclass(AIServiceError, RuntimeError)


def test_ai_service_facade_exports_conflict_error() -> None:
    assert issubclass(service_facade.AIConflictError, AIServiceError)
    assert "AIConflictError" in service_facade.__all__


PAYLOAD = {"messages": [{"role": "user", "content": "INPUT_SENTINEL"}], "max_tokens": 50}
COMPLETE_USAGE = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
                  "complete": True, "missing_reason": None}


@pytest.fixture
def invocation_env(tmp_path, monkeypatch):
    class FakeSecrets:
        def is_legacy_ciphertext(self, value):
            assert value == "synthetic-cipher"
            return False

        def decrypt(self, value):
            assert value == "synthetic-cipher"
            return "synthetic-key"

    def forbidden_network(*_args, **_kwargs):
        raise AssertionError("real network forbidden")

    monkeypatch.setattr(requests.Session, "request", forbidden_network)
    service = AIWritingService(tmp_path / "facade.db", secret_manager=FakeSecrets())
    db = service._db()
    provider_id = db.create_ai_provider({
        "name": "fake", "provider_type": "openai_compatible", "enabled": True,
        "base_url": "https://provider.invalid/v1", "api_key_encrypted": "synthetic-cipher",
        "context_window": 100000,
    })
    agent_id = db.create_ai_agent({
        "name": "fake", "task_type": "general", "provider_id": provider_id,
        "model": "fake-model", "system_prompt": "DO_NOT_INJECT", "context_window": 100000,
    })

    class FakeProvider:
        events = [AIStreamChunk(type="delta", text="OUTPUT_SENTINEL"),
                  AIStreamChunk(type="done", data={"finish_reason": "stop"})]
        calls = 0
        closed = 0
        final_usage = COMPLETE_USAGE
        before_guard = None
        messages = None

        def _post(self):
            observer = Database(db.path)
            try:
                row = observer.conn.execute("SELECT * FROM ai_jobs ORDER BY id DESC LIMIT 1").fetchone()
                saved = observer.get_ai_job(row["job_id"])
                self.job_id = saved["job_id"]
                self.owner_token = row["owner_token"]
                assert saved["network_request_count"] == 1
                assert saved["attempts"][0]["usage"]["requests"][0]["missing_reason"] == "pending"
                if saved["input"]["retention_policy"] == "metadata_only":
                    assert saved["input"]["input_fingerprint"] == saved["candidate_snapshot"]["plan"]["request_fingerprint"]
                    assert saved["input"]["allowed_provider_ids"] == [provider_id]
                    assert saved["input"]["snapshot_version"] == 2
            finally:
                observer.close()

        def stream_generate(self, messages, model, temperature, top_p, max_tokens, *,
                            request_guard=None, is_cancelled=None, on_usage=None):
            self.messages = messages
            if self.before_guard is not None:
                self.before_guard()
            ordinal = request_guard()
            self.calls += 1
            self._post()
            try:
                for event in self.events:
                    if callable(event):
                        event = event()
                    if isinstance(event, BaseException):
                        raise event
                    if event is not None:
                        yield event
            finally:
                self.closed += 1
                if self.final_usage is not None:
                    on_usage(ordinal, self.final_usage)

    provider = FakeProvider()
    monkeypatch.setattr(service.model_router, "_get_provider", lambda _config: provider)
    try:
        yield SimpleNamespace(service=service, db=db, provider=provider, agent_id=agent_id, provider_id=provider_id)
    finally:
        service.close()
        db.close()


def assert_no_raw_text(env, caplog):
    rows = [dict(row) for row in env.db.conn.execute("SELECT * FROM ai_jobs")]
    rows += [dict(row) for row in env.db.conn.execute("SELECT * FROM ai_job_model_attempts")]
    assert "INPUT_SENTINEL" not in json.dumps(rows)
    assert "OUTPUT_SENTINEL" not in json.dumps(rows)
    assert "INPUT_SENTINEL" not in caplog.text and "OUTPUT_SENTINEL" not in caplog.text
    for row in env.db.conn.execute("SELECT output_text, output_json FROM ai_jobs"):
        assert row["output_text"] is None and row["output_json"] is None


@pytest.mark.parametrize("mode", ["succeeded", "partial", "failed", "cancelled"])
def test_invoke_text_enforces_retention_in_sql_not_just_projection(invocation_env, mode, caplog):
    env = invocation_env
    if mode == "failed":
        env.provider.events = [AIProviderError("INPUT_SENTINEL", category="network")]
    elif mode == "partial":
        env.provider.events = [AIStreamChunk(type="delta", text="OUTPUT_SENTINEL"),
                               AIProviderError("OUTPUT_SENTINEL", category="network")]
    stopped = False
    deltas, progress = [], []

    def on_delta(text):
        nonlocal stopped
        deltas.append(text)
        stopped = mode == "cancelled"

    result = env.service.invoke_text(env.agent_id, PAYLOAD, on_delta=on_delta,
                                    on_progress=progress.append, stop_requested=lambda: stopped)
    assert result.job_status == mode
    assert result.finish_state == ("failed_before_output" if mode == "failed" else mode)
    assert result.output_text == ("" if mode == "failed" else "OUTPUT_SENTINEL")
    assert deltas == ([] if mode == "failed" else ["OUTPUT_SENTINEL"])
    assert len([item for item in progress if item.get("action") == "attempt"]) == 1
    assert env.provider.messages == PAYLOAD["messages"]
    assert env.provider.closed == 1 and not env.service.model_router._heartbeat_workers
    assert result.validation_status == "not_requested"
    assert result.usage_summary["known_total_tokens"] == 12
    assert result.usage_summary["reported_requests"] == 1
    assert result.attempts[0]["usage"]["requests"][0]["complete"] is True
    saved = env.db.get_ai_job(result.job_id)
    assert saved["input"]["input_chars"] == len("INPUT_SENTINEL")
    assert saved["input"]["message_count"] == 1
    assert saved["input"]["retention_policy"] == "metadata_only"
    assert_no_raw_text(env, caplog)


@pytest.mark.parametrize("field", ["retention_policy", "validator", "tools", "raw"])
def test_invoke_text_has_no_retention_or_validator_escape(invocation_env, field):
    env = invocation_env
    assert set(inspect.signature(env.service.invoke_text).parameters) == {
        "agent_id", "payload", "stop_requested", "deadline_at", "on_delta", "on_progress",
    }
    with pytest.raises(InvocationValidationError) as caught:
        env.service.invoke_text(env.agent_id, {**PAYLOAD, field: "INPUT_SENTINEL"})
    assert caught.value.code == "unsupported" and "INPUT_SENTINEL" not in str(caught.value)
    assert env.db.conn.execute("SELECT count(*) FROM ai_jobs").fetchone()[0] == 0
    assert env.provider.calls == 0


@pytest.mark.parametrize("kind", ["missing", "disabled"])
def test_invoke_text_never_falls_back_to_another_agent(invocation_env, kind):
    env = invocation_env
    requested_id = env.agent_id + 100 if kind == "missing" else env.agent_id
    if kind == "disabled":
        env.db.set_ai_agents_enabled([env.agent_id], False)
    with pytest.raises(AIServiceError):
        env.service.invoke_text(requested_id, PAYLOAD)
    assert env.db.conn.execute("SELECT count(*) FROM ai_jobs").fetchone()[0] == 0
    assert env.provider.calls == 0


def test_invoke_text_empty_plan_is_auditable_without_network(invocation_env, caplog):
    env = invocation_env
    result = env.service.invoke_text(env.agent_id, {**PAYLOAD, "allowed_provider_ids": []})
    assert result.job_status == "failed" and result.finish_state == "failed_before_output"
    assert result.attempts == () and env.provider.calls == 0
    saved = env.db.get_ai_job(result.job_id)
    assert saved["network_request_count"] == 0
    assert saved["input"]["allowed_provider_ids"] == []
    assert saved["error_message"] == "no_eligible_candidate"
    assert result.usage_summary["unreported_requests"] == 0
    assert_no_raw_text(env, caplog)


@pytest.mark.parametrize("later", [False, True])
def test_invoke_text_deadline_is_validated_and_cannot_extend_server_limit(invocation_env, later):
    env = invocation_env
    now = datetime.now(timezone.utc)
    caller_deadline = now + timedelta(minutes=60 if later else 2)
    result = env.service.invoke_text(env.agent_id, PAYLOAD, deadline_at=caller_deadline.isoformat())
    saved = env.db.get_ai_job(result.job_id)
    deadline = datetime.fromisoformat(saved["route_deadline_at"]).replace(tzinfo=timezone.utc)
    assert deadline <= now + timedelta(minutes=30, seconds=1)
    if not later:
        assert deadline == caller_deadline.replace(microsecond=0)
    with pytest.raises(ValueError):
        env.service.invoke_text(env.agent_id, PAYLOAD, deadline_at="INPUT_SENTINEL")
    assert env.db.conn.execute("SELECT count(*) FROM ai_jobs").fetchone()[0] == 1


def test_invoke_text_pre_cancelled_stops_before_any_request(invocation_env):
    env = invocation_env
    result = env.service.invoke_text(env.agent_id, PAYLOAD, stop_requested=lambda: True)
    assert result.job_status == "cancelled" and result.output_text == ""
    assert env.provider.calls == 0 and result.usage_summary["unreported_requests"] == 0


def test_invoke_text_committed_cancel_cannot_race_the_request_reservation(invocation_env, monkeypatch):
    env = invocation_env
    # Ignore the heartbeat's cached event to exercise the durable guard itself.
    monkeypatch.setattr(env.service.model_router, "_request_cancelled", lambda _request: False)

    def cancel_before_guard():
        job_id = env.db.conn.execute("SELECT job_id FROM ai_jobs").fetchone()[0]
        assert env.db.request_ai_job_cancel(job_id)

    env.provider.before_guard = cancel_before_guard
    result = env.service.invoke_text(env.agent_id, PAYLOAD)
    assert result.job_status == "cancelled" and env.provider.calls == 0
    assert result.usage_summary["unreported_requests"] == 0


@pytest.mark.parametrize("callback", ["on_delta", "on_progress"])
def test_invoke_text_callback_failure_closes_router_and_keeps_safe_storage(invocation_env, callback, caplog):
    env = invocation_env

    def fail(_value):
        raise RuntimeError("INPUT_SENTINEL OUTPUT_SENTINEL")

    with pytest.raises(AIServiceError) as caught:
        env.service.invoke_text(env.agent_id, PAYLOAD, **{callback: fail})
    assert "SENTINEL" not in str(caught.value)
    assert not env.service.model_router._heartbeat_workers
    job = env.db.get_ai_job(env.db.conn.execute("SELECT job_id FROM ai_jobs").fetchone()[0])
    assert job["status"] != "running"
    assert all(item["status"] != "running" for item in job["attempts"])
    assert_no_raw_text(env, caplog)


@pytest.mark.parametrize("failure", ["typed_conflict", "close_usage_conflict", "close_usage_error"])
def test_invoke_text_error_cleanup_finishes_owned_attempts_before_parent(invocation_env, monkeypatch, failure, caplog):
    env = invocation_env
    kwargs = {}
    if failure == "typed_conflict":
        env.provider.events = [AIStreamChunk(type="delta", text="OUTPUT_SENTINEL"),
                               ModelRouteConflictError("INPUT_SENTINEL")]
    else:
        error = AIJobConflictError if failure == "close_usage_conflict" else RuntimeError

        def usage_failure(*_args, **_kwargs):
            raise error("INPUT_SENTINEL")

        monkeypatch.setattr(Database, "record_ai_job_request_usage", usage_failure)

        def consumer_failure(_value):
            raise RuntimeError("OUTPUT_SENTINEL")

        kwargs["on_delta"] = consumer_failure
    with pytest.raises(AIServiceError) as caught:
        env.service.invoke_text(env.agent_id, PAYLOAD, **kwargs)
    assert "SENTINEL" not in str(caught.value)
    saved = env.db.get_ai_job(env.provider.job_id)
    assert saved["status"] != "running"
    assert saved["attempts"][0]["status"] != "running"
    assert env.provider.closed == 1 and not env.service.model_router._heartbeat_workers
    assert_no_raw_text(env, caplog)


def test_invoke_text_start_failure_after_row_has_only_safe_audit(invocation_env, monkeypatch, caplog):
    env = invocation_env

    def failed_audit(*_args):
        raise RuntimeError("INPUT_SENTINEL")

    monkeypatch.setattr(Database, "replace_ai_job_audit", failed_audit)
    with pytest.raises(AIServiceError) as caught:
        env.service.invoke_text(env.agent_id, PAYLOAD)
    assert "INPUT_SENTINEL" not in str(caught.value)
    rows = env.db.list_ai_jobs()["items"]
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert env.provider.calls == 0
    assert_no_raw_text(env, caplog)


@pytest.mark.parametrize("winner", ["succeeded", "failed", "cancelled", "partial"])
def test_invoke_text_terminal_cas_loser_discards_local_output(invocation_env, monkeypatch, winner, caplog):
    env = invocation_env

    def competing_finish(db, context, *_args, **_kwargs):
        assert db.finish_ai_job_cas(context.job_id, context.owner_token, winner, output_text="WINNER")
        return False

    monkeypatch.setattr(env.service, "_finish_route_job", competing_finish)
    result = env.service.invoke_text(env.agent_id, PAYLOAD)
    assert result.job_status == winner and result.output_text == ""
    assert result.finish_state == ("failed_before_output" if winner == "failed" else winner)
    assert_no_raw_text(env, caplog)


@pytest.mark.parametrize("body", ['not JSON', '{"keywords": []}', '{"keywords": "wrong"}',
                                  '{"keywords": [1]}', '{"keywords": ["ok"], "dropped_sample": null}'])
def test_keyword_validation_failure_keeps_succeeded_transport_attempt(invocation_env, monkeypatch, body):
    env = invocation_env
    env.provider.events = [AIStreamChunk(type="delta", text=body),
                           AIStreamChunk(type="done", data={"finish_reason": "stop"})]
    original = env.service._invoke_text
    results = []

    def capture(*args, **kwargs):
        value = original(*args, **kwargs)
        results.append(value)
        return value

    monkeypatch.setattr(env.service, "_invoke_text", capture)
    assert env.service.clean_keywords(["synthetic"]) is None
    result, parsed = results[0]
    assert result.finish_state == "succeeded" and result.job_status == "failed"
    assert result.validation_status == "failed" and parsed is None
    saved = env.db.get_ai_job(result.job_id)
    assert saved["task_type"] == "keyword_clean" and saved["agent_id"] == env.agent_id
    assert saved["input"]["retention_policy"] == "keyword_compat"
    assert saved["status"] == "failed" and saved["error_message"] == "validation_failed"
    assert saved["attempts"][0]["status"] == "succeeded"


def test_keyword_compat_input_and_parsed_output_remain_bounded(invocation_env):
    env = invocation_env
    env.provider.events = [AIStreamChunk(type="delta", text=json.dumps({
        "keywords": [f"k{i}" for i in range(40)], "dropped_sample": [f"d{i}" for i in range(20)],
    })), AIStreamChunk(type="done", data={"finish_reason": "stop"})]
    result = env.service.clean_keywords([f"input{i}" for i in range(90)], [f"tag{i}" for i in range(50)])
    assert len(result["keywords"]) == 30 and len(result["dropped_sample"]) == 10
    saved = env.db.get_ai_job(env.provider.job_id)
    assert saved["task_type"] == "keyword_clean" and saved["validation_status"] == "passed"
    assert len(saved["input"]["raw_keywords"]) == 80 and len(saved["input"]["tags"]) == 40
    assert saved["output"] == result


def test_keyword_terminal_cas_loser_never_returns_its_parsed_result(invocation_env, monkeypatch):
    env = invocation_env
    env.provider.events = [AIStreamChunk(type="delta", text='{"keywords":["loser"]}'),
                           AIStreamChunk(type="done", data={"finish_reason": "stop"})]

    def competing_finish(db, context, *_args, **_kwargs):
        assert db.finish_ai_job_cas(context.job_id, context.owner_token, "succeeded",
                                    output_text='{"keywords":["winner"]}', validation_status="passed")
        return False

    monkeypatch.setattr(env.service, "_finish_route_job", competing_finish)
    assert env.service.clean_keywords(["synthetic"]) is None
    assert env.db.get_ai_job(env.provider.job_id)["output_text"] == '{"keywords":["winner"]}'


def test_keyword_deep_json_is_validation_failure_not_a_running_job(invocation_env):
    env = invocation_env
    body = '{"keywords":' + '[' * 1500 + '"nested"' + ']' * 1500 + '}'
    env.provider.events = [AIStreamChunk(type="delta", text=body),
                           AIStreamChunk(type="done", data={"finish_reason": "stop"})]
    assert env.service.clean_keywords(["input"]) is None
    saved = env.db.get_ai_job(env.provider.job_id)
    assert saved["status"] == "failed" and saved["validation_status"] == "failed"
    assert saved["error_message"] == "validation_failed"
    assert saved["attempts"][0]["status"] == "succeeded"


def test_keyword_typed_conflict_after_output_closes_parent_without_buffer_fallback(invocation_env):
    env = invocation_env
    env.provider.events = [AIStreamChunk(type="delta", text='{"keywords":["discarded"]}'),
                           ModelRouteConflictError("private failure")]
    assert env.service.clean_keywords(["input"]) is None
    saved = env.db.get_ai_job(env.provider.job_id)
    assert saved["status"] == "partial"
    assert saved["attempts"][0]["status"] == "partial"
    assert not saved.get("output_text") and saved.get("output") is None
    assert "discarded" not in json.dumps(saved)
