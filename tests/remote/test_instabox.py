"""Provider fixtures are public-schema examples, never account captures."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from pixiv_novel_sync.remote.instabox import (
    InstaBoxClient,
    ProviderError,
    decode_machine,
    decode_operation,
)


FIXTURES = Path(__file__).with_name("fixtures")


def machine_payload():
    return json.loads((FIXTURES / "machine.json").read_text(encoding="utf-8"))


def operation_payload():
    return json.loads((FIXTURES / "operation.json").read_text(encoding="utf-8"))


class Response:
    def __init__(self, status=200, payload=None, headers=None, raw=None):
        self.status_code = status
        self.headers = headers or {}
        self.raw_body = raw if raw is not None else json.dumps(payload).encode()
        self.closed = False

    def iter_content(self, chunk_size):
        for start in range(0, len(self.raw_body), chunk_size):
            yield self.raw_body[start:start + chunk_size]

    def close(self):
        self.closed = True


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_running_is_not_runtime_ready():
    payload = machine_payload()
    payload["runtime"]["ready"] = False
    view = decode_machine(payload, "m_fixture")
    assert view.state == "running"
    assert view.runtime_ready is False
    assert view.max_run_minutes == 480
    assert view.run_started_at == datetime(2026, 11, 10, 12, 50, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("state", ["paused", "failed", "unknown", None, 1, []])
def test_unknown_machine_states_fail_closed(state):
    payload = machine_payload()
    payload["state"] = state
    with pytest.raises(ProviderError, match="invalid_machine"):
        decode_machine(payload, "m_fixture")


@pytest.mark.parametrize("field,value", [
    ("id", "m_other"), ("operationId", "../op"),
    ("maxRunMinutes", True), ("maxRunMinutes", -2), ("maxRunMinutes", 1.5),
    ("runStartedAt", "2026-11-10T12:00:00"),
    ("runStartedAt", "2026-11-10"), ("cooldownUntil", False),
    ("runtime", {"ready": 1}), ("runtime", {}),
])
def test_invalid_machine_fields_are_rejected(field, value):
    payload = machine_payload()
    payload[field] = value
    with pytest.raises(ProviderError, match="invalid_machine"):
        decode_machine(payload, "m_fixture")


@pytest.mark.parametrize("field", [
    "state", "runtime", "operationId", "maxRunMinutes", "runStartedAt", "cooldownUntil",
])
def test_missing_machine_fields_are_rejected(field):
    payload = machine_payload()
    del payload[field]
    with pytest.raises(ProviderError, match="invalid_machine"):
        decode_machine(payload, "m_fixture")


def test_unknown_runtime_budget_is_not_unlimited():
    payload = machine_payload()
    payload["runStartedAt"] = None
    assert decode_machine(payload, "m_fixture").runtime_budget_until is None
    payload["maxRunMinutes"] = -1
    assert decode_machine(payload, "m_fixture").runtime_budget_until == -1
    payload["maxRunMinutes"] = 0
    assert decode_machine(payload, "m_fixture").runtime_budget_until is None


def test_operation_uses_wire_type_and_needs_no_embedded_machine():
    view = decode_operation(operation_payload(), "m_fixture")
    assert view.id == "op_fixture"
    assert view.action == "start"
    assert view.state == "queued"


@pytest.mark.parametrize("field,value", [
    ("machineId", "m_other"), ("state", "ready"),
    ("state", []), ("type", "shell"), ("type", {}),
    ("id", "../op"),
])
def test_invalid_operations_are_rejected(field, value):
    payload = operation_payload()
    payload[field] = value
    with pytest.raises(ProviderError, match="invalid_operation"):
        decode_operation(payload, "m_fixture")


def test_failed_operation_retains_only_known_error_code():
    payload = operation_payload()
    payload.update(state="failed", error={"code": "no_capacity", "message": "SECRET"})
    view = decode_operation(payload, "m_fixture")
    assert view.error_code == "no_capacity"
    assert "SECRET" not in repr(view)
    payload["error"]["code"] = "SECRET"
    assert decode_operation(payload, "m_fixture").error_code == "provider_error"


def test_start_transport_contract_and_no_hidden_retries():
    transport = Transport(requests.Timeout("SECRET"), Response(202, operation_payload()))
    client = InstaBoxClient("ibk_test_secret", "m_fixture", transport)
    with pytest.raises(ProviderError, match="provider_unreachable") as caught:
        client.mutate("start", "same-retry-key")
    assert "SECRET" not in str(caught.value)
    assert len(transport.calls) == 1
    assert client.mutate("start", "same-retry-key").id == "op_fixture"
    first, second = transport.calls
    assert first[0] == second[0]
    assert {k: v for k, v in first[1].items() if k != "timeout"} == {k: v for k, v in second[1].items() if k != "timeout"}
    args, kwargs = first
    assert args == ("POST", "https://api.instabox.sh/v1/machines/m_fixture/start")
    assert kwargs["json"] == {}
    assert kwargs["headers"]["X-API-Key"] == "ibk_test_secret"
    assert kwargs["headers"]["Idempotency-Key"] == "same-retry-key"
    assert kwargs["timeout"].total == 15
    assert kwargs["timeout"].connect_timeout == 5
    assert kwargs["timeout"].read_timeout == 10
    assert kwargs["allow_redirects"] is False
    assert kwargs["verify"] is True
    assert kwargs["stream"] is True


@pytest.mark.parametrize("error,reason", [
    (requests.Timeout("private-network-detail"), "timeout"),
    (requests.exceptions.SSLError("private-certificate-detail"), "tls"),
    (requests.ConnectionError("private-connection-detail"), "connection"),
    (TimeoutError("private-read-detail"), "timeout"),
])
def test_network_diagnostic_uses_only_fixed_reason_codes(error, reason):
    transport = Transport(error)
    with pytest.raises(ProviderError) as caught:
        InstaBoxClient("ibk_test_secret", "m_fixture", transport).machine()
    assert caught.value.code == "provider_unreachable"
    assert caught.value.network_reason == reason
    assert "private" not in str(caught.value)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("action", ["pause", "stop", "delete", "exec", "../start"])
def test_mutation_registry_is_closed(action):
    transport = Transport()
    with pytest.raises(ProviderError, match="invalid_action"):
        InstaBoxClient("ibk_test", "m_fixture", transport).mutate(action, "key")
    assert transport.calls == []


@pytest.mark.parametrize("key", ["", "with space", "x\r\ny", "中文", "a" * 201])
def test_idempotency_key_rejects_header_injection(key):
    with pytest.raises(ProviderError, match="invalid_idempotency_key"):
        InstaBoxClient("ibk_test", "m_fixture", Transport()).mutate("start", key)


def test_wake_and_operation_paths_do_not_use_alternate_endpoints():
    transport = Transport(Response(200, machine_payload()), Response(200, operation_payload()))
    client = InstaBoxClient("ibk_test", "m_fixture", transport)
    assert client.wake().id == "m_fixture"
    assert client.operation("op_fixture").state == "queued"
    assert transport.calls[0][0][1].endswith("/machines/m_fixture/wake")
    assert "Idempotency-Key" not in transport.calls[0][1]["headers"]
    assert transport.calls[1][0][1].endswith("/operations/op_fixture")
    assert "?" not in transport.calls[1][0][1]


def test_retry_after_uses_latest_header_and_body(monkeypatch):
    monkeypatch.setattr("pixiv_novel_sync.remote.instabox.time.time", lambda: 1000)
    payload = json.loads((FIXTURES / "cooldown.json").read_text(encoding="utf-8"))
    response = Response(409, payload, {"Retry-After": "1200"})
    with pytest.raises(ProviderError) as caught:
        InstaBoxClient("ibk_test", "m_fixture", Transport(response)).mutate("start", "key")
    assert caught.value.code == "machine_cooldown"
    assert caught.value.http_status == 409
    assert caught.value.retry_after == 1200
    assert response.closed


def test_http_date_retry_after(monkeypatch):
    monkeypatch.setattr("pixiv_novel_sync.remote.instabox.time.time", lambda: 1000)
    response = Response(429, {"error": {"code": "rate_limited"}},
                        {"Retry-After": "Thu, 01 Jan 1970 00:20:00 GMT"})
    with pytest.raises(ProviderError) as caught:
        InstaBoxClient("ibk_test", "m_fixture", Transport(response)).machine()
    assert caught.value.retry_after == 200


@pytest.mark.parametrize("response", [
    Response(429, raw=b"<html>rate limited SECRET</html>", headers={"Retry-After": "120"}),
    Response(429, {}, {"Content-Encoding": "gzip", "Retry-After": "120"}),
    Response(429, {}, {"Content-Length": str(1024 * 1024 + 1), "Retry-After": "120"}),
])
def test_retry_after_is_preserved_when_error_body_is_unusable(response):
    with pytest.raises(ProviderError) as caught:
        InstaBoxClient("ibk_test", "m_fixture", Transport(response)).machine()
    assert caught.value.http_status == 429
    assert caught.value.retry_after == 120
    assert "SECRET" not in str(caught.value)
    assert response.closed


def test_retry_after_is_preserved_when_rate_limit_body_times_out():
    from urllib3.exceptions import ReadTimeoutError

    class TimedOut:
        def read1(self, size, decode_content=False):
            raise ReadTimeoutError(None, "SECRET", "SECRET")

    response = Response(429, {}, {"Retry-After": "120"})
    response.raw = TimedOut()
    with pytest.raises(ProviderError) as caught:
        InstaBoxClient("ibk_test", "m_fixture", Transport(response)).machine()
    assert caught.value.http_status == 429
    assert caught.value.retry_after == 120
    assert "SECRET" not in str(caught.value)
    assert response.closed


@pytest.mark.parametrize("status", [401, 403, 429])
def test_error_status_survives_overall_response_deadline(monkeypatch, status):
    clock = [100.0]
    monkeypatch.setattr("pixiv_novel_sync.remote.instabox.time.monotonic", lambda: clock[0])

    class Drip:
        def read1(self, size, decode_content=False):
            clock[0] += 6
            return b" "

    response = Response(status, {}, {"Retry-After": "120"})
    response.raw = Drip()
    with pytest.raises(ProviderError, match="provider_unreachable") as caught:
        InstaBoxClient("ibk_test", "m_fixture", Transport(response)).machine()
    assert caught.value.http_status == status
    assert caught.value.retry_after == 120
    assert response.closed


def test_operation_conflict_is_not_cooldown():
    response = Response(409, {"error": {"code": "operation_conflict", "operationId": "op_other"}})
    with pytest.raises(ProviderError) as caught:
        InstaBoxClient("ibk_test", "m_fixture", Transport(response)).mutate("start", "key")
    assert caught.value.code == "operation_conflict"
    assert caught.value.operation_id == "op_other"
    assert caught.value.retry_after is None


@pytest.mark.parametrize("response", [
    Response(302, {"error": {"code": "redirect"}}),
    Response(200, raw=b"invalid JSON SECRET"),
    Response(200, raw=b"x" * (1024 * 1024 + 1)),
    Response(200, {}, {"Content-Length": str(1024 * 1024 + 1)}),
    Response(200, {"machine": {}}),
])
def test_invalid_or_unbounded_response_fails_closed(response):
    with pytest.raises(ProviderError) as caught:
        InstaBoxClient("ibk_test", "m_fixture", Transport(response)).machine()
    assert "SECRET" not in str(caught.value)
    assert response.closed


def test_mutation_and_poll_verify_operation_identity():
    payload = operation_payload()
    payload["type"] = "suspend"
    client = InstaBoxClient("ibk_test", "m_fixture", Transport(Response(202, payload)))
    with pytest.raises(ProviderError, match="invalid_operation"):
        client.mutate("start", "key")
    client = InstaBoxClient("ibk_test", "m_fixture", Transport(Response(200, payload)))
    with pytest.raises(ProviderError, match="invalid_operation"):
        client.operation("op_unrelated")


@pytest.mark.parametrize("raw", [b'{"id":"m_fixture","id":"m_other"}', b'{"value":NaN}'])
def test_ambiguous_json_is_rejected(raw):
    with pytest.raises(ProviderError, match="invalid_provider_response"):
        InstaBoxClient("ibk_test", "m_fixture", Transport(Response(200, raw=raw))).machine()


def test_slow_response_read1_cannot_run_past_overall_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("pixiv_novel_sync.remote.instabox.time.monotonic", lambda: clock[0])

    class Drip:
        calls = 0

        def read1(self, size, decode_content=False):
            self.calls += 1
            clock[0] += 6
            return b" "

    response = Response(200, {})
    response.raw = Drip()
    with pytest.raises(ProviderError, match="provider_unreachable"):
        InstaBoxClient("ibk_test", "m_fixture", Transport(response)).machine()
    assert response.raw.calls == 3
    assert response.closed


@pytest.mark.parametrize("attached_connection", [True, False])
@pytest.mark.parametrize("supports_read1", [True, False])
def test_provider_body_socket_timeout_tracks_remaining_deadline(monkeypatch, attached_connection, supports_read1):
    import http.client
    import io

    from urllib3.connection import HTTPConnection
    from urllib3.response import HTTPResponse

    clock = [100.0]
    monkeypatch.setattr("pixiv_novel_sync.remote.instabox.time.monotonic", lambda: clock[0])

    class SlowSocket:
        timeout = 10.0

        def __init__(self):
            self.timeouts = []

        def settimeout(self, value):
            self.timeout = value
            self.timeouts.append(value)

        def makefile(self, mode):
            return io.BufferedReader(SlowWire(self))

        def close(self):
            pass

    class SlowWire(io.RawIOBase):
        def __init__(self, sock):
            self._sock = sock
            self.headers_sent = False

        def readable(self):
            return True

        def readinto(self, buffer):
            if not self.headers_sent:
                value = b"HTTP/1.1 429 Too Many Requests\r\nContent-Length: 10000\r\nRetry-After: 120\r\n\r\n"
                self.headers_sent = True
            else:
                clock[0] += min(6.0, self._sock.timeout)
                if self._sock.timeout < 6:
                    raise TimeoutError("fixture body timed out")
                value = b" "
            buffer[:len(value)] = value
            return len(value)

    sock = SlowSocket()
    original = http.client.HTTPResponse(sock)
    original.begin()
    connection = HTTPConnection("fixture.invalid")
    connection.sock = sock if attached_connection else None
    raw = HTTPResponse(body=original, original_response=original, connection=connection,
                       headers=original.headers.items(), status=429, preload_content=False,
                       decode_content=False)
    if not supports_read1:
        raw.read1 = None
    response = requests.Response()
    response.status_code = 429
    response.headers.update(original.headers)
    response.raw = raw

    with pytest.raises(ProviderError, match="provider_unreachable") as caught:
        InstaBoxClient("ibk_test", "m_fixture", Transport(response)).machine()

    assert clock[0] == pytest.approx(115)
    assert sock.timeouts == pytest.approx([10, 9, 3])
    assert caught.value.http_status == 429
    assert caught.value.retry_after == 120
    assert raw.closed


def test_provider_compression_is_rejected_before_read():
    response = Response(200, machine_payload(), {"Content-Encoding": "gzip"})
    with pytest.raises(ProviderError, match="invalid_provider_response"):
        InstaBoxClient("ibk_test", "m_fixture", Transport(response)).machine()
    assert response.closed


def test_raw_stream_timeout_is_redacted():
    from urllib3.exceptions import ReadTimeoutError

    class TimedOut:
        def read1(self, size, decode_content=False):
            raise ReadTimeoutError(None, "SECRET", "SECRET")

    response = Response(200, {})
    response.raw = TimedOut()
    with pytest.raises(ProviderError, match="provider_unreachable") as caught:
        InstaBoxClient("ibk_test", "m_fixture", Transport(response)).machine()
    assert "SECRET" not in str(caught.value)
    assert response.closed
