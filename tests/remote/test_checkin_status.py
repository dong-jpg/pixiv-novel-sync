from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
import sys

import pytest

from pixiv_novel_sync.remote.checkin_status import (
    CheckinLocalConfig,
    CheckinSiteBinding,
    CheckinStatusRequest,
    CheckinStatusResult,
    DailyCheckinRunnerGateway,
    GatewayObservation,
    create_local_gateway,
    execute_checkin_status,
    pseudonymous_identity,
)
from pixiv_novel_sync.remote.contracts import RemoteError, canonical_bytes


NOW = datetime(2026, 11, 12, 8, tzinfo=timezone.utc)
KEY = b"synthetic-identity-hmac-key-000000"
UID = "synthetic-account-42"
SESSION = "b" * 64


def request_body(**changes):
    body = {
        "protocol": 1,
        "site_id": "fixture-site",
        "config_version": 3,
        "business_day": "2026-11-12",
        "timezone": "Asia/Shanghai",
        "expected_identity": pseudonymous_identity(KEY, "fixture-site", UID),
        "session_binding": SESSION,
    }
    body.update(changes)
    return body


def binding(**changes):
    value = dict(site_id="fixture-site", config_version=3, timezone="Asia/Shanghai",
                 identity=pseudonymous_identity(KEY, "fixture-site", UID),
                 session_binding=SESSION, enabled=True, timezone_verified=True)
    value.update(changes)
    return CheckinSiteBinding(**value)


class Gateway:
    def __init__(self, observation=None):
        self.current = binding()
        self.observation = observation or GatewayObservation("ready", "2026-11-12", True, "live_status")
        self.binding_calls = []
        self.reads = []

    def describe_site(self, site_id):
        self.binding_calls.append(site_id)
        return self.current

    def read_status(self, request):
        self.reads.append(request)
        return self.observation


def test_wire_round_trip_is_closed_and_contains_only_pseudonymous_bindings():
    request = CheckinStatusRequest.from_dict(request_body())
    assert request.to_dict() == request_body()
    result = execute_checkin_status(request, Gateway(), clock=lambda: NOW)
    encoded = canonical_bytes(result.to_dict())
    assert result.status == "due" and result.identity_match is True
    assert UID not in encoded.decode()
    assert SESSION not in encoded.decode()
    assert request.expected_identity not in repr(request)
    assert CheckinStatusResult.from_dict(result.to_dict(), request=request) == result
    other = CheckinStatusRequest.from_dict(request_body(session_binding="c" * 64))
    with pytest.raises(RemoteError, match="checkin_result_mismatch"):
        CheckinStatusResult.from_dict(result.to_dict(), request=other)


@pytest.mark.parametrize("field,value", [
    ("url", "https://example.invalid/"), ("command", "run --execute"),
    ("path", "/etc/passwd"), ("execute", True), ("execute", False),
    ("payload", {}), ("token", "never-accepted"),
])
def test_requests_never_accept_execution_or_location_fields(field, value):
    with pytest.raises(RemoteError, match="invalid_fields"):
        CheckinStatusRequest.from_dict(request_body(**{field: value}))


@pytest.mark.parametrize("field,value", [
    ("protocol", True), ("protocol", 2), ("config_version", True),
    ("config_version", 0), ("site_id", "../site"), ("site_id", "https://site"),
    ("site_id", "site --execute"), ("business_day", "2026-11-31"),
    ("business_day", "2026-1-2"), ("timezone", "../UTC"),
    ("timezone", "Not/AZone"), ("expected_identity", UID),
    ("session_binding", "Bearer token"),
])
def test_invalid_wire_types_and_values_are_rejected(field, value):
    with pytest.raises(RemoteError):
        CheckinStatusRequest.from_dict(request_body(**{field: value}))


def test_identity_is_keyed_and_scoped_to_site():
    tag = pseudonymous_identity(KEY, "fixture-site", UID)
    assert tag != hashlib.sha256(UID.encode()).hexdigest()
    assert tag != pseudonymous_identity(b"z" * 32, "fixture-site", UID)
    assert tag != pseudonymous_identity(KEY, "other-site", UID)
    with pytest.raises(RemoteError, match="invalid_checkin_identity_key"):
        pseudonymous_identity(b"short", "fixture-site", UID)


@pytest.mark.parametrize("state,identity,evidence,expected", [
    ("ready", True, "live_status", "due"),
    ("due", True, "live_status", "due"),
    ("success", True, "live_status", "already_done"),
    ("already", True, "live_status", "already_done"),
    ("already", None, "local_receipt", "already_done"),
    ("pending", True, "live_status", "uncertain"),
    ("unknown", True, "live_status", "uncertain"),
    ("uncertain", None, "none", "uncertain"),
    ("needs_login", None, "none", "login_required"),
    ("identity_mismatch", False, "none", "login_required"),
    ("rate_limited", None, "none", "unavailable"),
    ("needs_verification", True, "live_status", "unavailable"),
    ("unsupported", True, "live_status", "unavailable"),
    ("failed", None, "none", "unavailable"),
    ("skipped", None, "none", "unavailable"),
])
def test_native_states_are_conservatively_mapped(state, identity, evidence, expected):
    gateway = Gateway(GatewayObservation(state, "2026-11-12", identity, evidence))
    result = execute_checkin_status(request_body(), gateway, clock=lambda: NOW)
    assert result.status == expected


@pytest.mark.parametrize("day,identity,evidence", [
    (None, True, "live_status"), ("2026-11-11", True, "live_status"),
    ("2026-11-12", None, "none"), ("2026-11-12", False, "live_status"),
])
def test_success_without_matching_date_or_identity_evidence_is_not_confirmed(day, identity, evidence):
    result = execute_checkin_status(
        request_body(), Gateway(GatewayObservation("success", day, identity, evidence)), clock=lambda: NOW
    )
    assert result.status != "already_done"


@pytest.mark.parametrize("day,status", [("2026-11-11", "expired"), ("2026-11-13", "unavailable")])
def test_noncurrent_business_day_never_opens_gateway(day, status):
    gateway = Gateway()
    result = execute_checkin_status(request_body(business_day=day), gateway, clock=lambda: NOW)
    assert result.status == status
    assert not gateway.binding_calls and not gateway.reads


def test_crossing_midnight_during_read_expires_the_original_day():
    clock = SimpleNamespace(now=NOW)
    gateway = Gateway()

    def read(request):
        clock.now += timedelta(days=1)
        return GatewayObservation("already", request.business_day, True, "live_status")

    gateway.read_status = read
    result = execute_checkin_status(request_body(), gateway, clock=lambda: clock.now)
    assert result.status == "expired" and result.business_day == "2026-11-12"
    assert result.identity_match is None and result.evidence == "none"


@pytest.mark.parametrize("change", [
    {"config_version": 4}, {"timezone": "UTC"},
    {"identity": "e" * 64}, {"session_binding": "f" * 64},
])
def test_stale_binding_prevents_any_status_request(change):
    gateway = Gateway()
    gateway.current = binding(**change)
    with pytest.raises(RemoteError, match="checkin_binding_mismatch"):
        execute_checkin_status(request_body(), gateway, clock=lambda: NOW)
    assert gateway.reads == []


def test_session_rotation_after_read_rejects_the_result():
    gateway = Gateway()

    def read(request):
        gateway.current = binding(session_binding="f" * 64)
        return GatewayObservation("already", request.business_day, True, "live_status")

    gateway.read_status = read
    with pytest.raises(RemoteError, match="checkin_binding_mismatch"):
        execute_checkin_status(request_body(), gateway, clock=lambda: NOW)


@pytest.mark.parametrize("method", ["describe_site", "read_status"])
def test_unexpected_gateway_failures_never_expose_paths_or_responses(method):
    gateway = Gateway()

    def fail(*args):
        raise OSError("private/profile/session-cookie-and-upstream-body")

    setattr(gateway, method, fail)
    with pytest.raises(RemoteError, match="checkin_gateway_unavailable") as caught:
        execute_checkin_status(request_body(), gateway, clock=lambda: NOW)
    assert "private" not in str(caught.value)


def test_invalid_clock_does_not_open_the_gateway():
    gateway = Gateway()
    with pytest.raises(RemoteError, match="invalid_checkin_clock"):
        execute_checkin_status(request_body(), gateway, clock=lambda: NOW.replace(tzinfo=None))
    assert not gateway.binding_calls and not gateway.reads


def test_gateway_observed_day_change_cannot_be_undone_by_clock_rollback():
    gateway = Gateway(GatewayObservation("expired", "2026-11-12", None))
    result = execute_checkin_status(request_body(), gateway, clock=lambda: NOW)
    assert result.status == "expired" and result.evidence == "none"


@pytest.mark.parametrize("change,status", [
    ({"identity": None}, "login_required"),
    ({"enabled": False}, "unavailable"),
    ({"timezone_verified": False}, "unavailable"),
])
def test_unprovisioned_binding_is_reported_without_opening_browser(change, status):
    gateway = Gateway()
    gateway.current = binding(**change)
    assert execute_checkin_status(request_body(), gateway, clock=lambda: NOW).status == status
    assert not gateway.reads


class MemoryStore:
    """只承载合成账户绑定，不打开真实文件或项目数据库。"""

    def __init__(self):
        self.site = SimpleNamespace(id="fixture-site", version=3, timezone="Asia/Shanghai",
                                    timezone_verified=True, enabled=True, expected_uid=UID)
        self.cached = None
        self.local_writes = 0

    def get_site(self, site_id):
        assert site_id == self.site.id
        return self.site

    def receipt(self, site_id, uid, day):
        assert (site_id, uid, day) == (self.site.id, UID, "2026-11-12")
        return self.cached


class SyntheticTransport:
    def __init__(self):
        self.methods = []
        self.logins = 0

    def request(self, method, path, **kwargs):
        self.methods.append(method)
        return SimpleNamespace(status=200)

    def fresh_login(self, method):
        self.logins += 1
        raise AssertionError("fresh_login must never reach the underlying transport")


class TransportFactory:
    def __init__(self):
        self.transport = SyntheticTransport()
        self.open_count = 0

    @contextmanager
    def open(self, site):
        self.open_count += 1
        yield self.transport


class SyntheticAdapter:
    submit_count = 0

    def __init__(self, site, transport):
        self.site, self.transport = site, transport

    def identity(self):
        self.transport.request("GET", "/api/user/self")
        return SimpleNamespace(uid=UID)

    def status(self, day):
        self.transport.request("GET", "/api/user/checkin")
        return SimpleNamespace(state="due", day=day)

    def submit(self, day, **kwargs):
        SyntheticAdapter.submit_count += 1
        raise AssertionError("submit must never reach the native adapter")


class SyntheticRunner:
    """复制公开调用约定；不导入或运行用户的 daily-checkin 项目。"""

    calls = []
    forbidden_action = None
    cached = False

    def __init__(self, store, transport_factory, solver=None, *, clock=None, adapter_factory=None):
        assert solver is None
        self.store, self.transport_factory = store, transport_factory
        self.adapter_factory = adapter_factory

    def run(self, site_ids=None, *, execute=False):
        self.calls.append((site_ids, execute))
        assert execute is False
        if self.cached:
            return [SimpleNamespace(site_id=site_ids[0], state="already", day="2026-11-12")]
        site = self.store.get_site(site_ids[0])
        with self.transport_factory.open(site) as transport:
            adapter = self.adapter_factory(site, transport)
            adapter.identity()
            observation = adapter.status("2026-11-12")
            self.store.local_writes += 1
            if self.forbidden_action:
                # 模拟原 Runner 的宽泛异常处理：网关不能因为异常被吞掉就报告成功。
                try:
                    if self.forbidden_action == "submit":
                        adapter.submit("2026-11-12")
                    elif self.forbidden_action == "post":
                        transport.request("POST", "/api/user/checkin")
                    else:
                        transport.fresh_login("github")
                except Exception:
                    pass
                return [SimpleNamespace(site_id=site.id, state="success", day="2026-11-12")]
            return [SimpleNamespace(site_id=site.id, state="ready", day=observation.day,
                                    message="never expose private response", reward=999)]


@pytest.fixture
def runner_gateway(monkeypatch):
    monkeypatch.setattr(SyntheticRunner, "calls", [])
    monkeypatch.setattr(SyntheticRunner, "forbidden_action", None)
    monkeypatch.setattr(SyntheticRunner, "cached", False)
    monkeypatch.setattr(SyntheticAdapter, "submit_count", 0)
    store, transports = MemoryStore(), TransportFactory()
    gateway = DailyCheckinRunnerGateway(
        store=store, transport_factory=transports, identity_key=KEY,
        session_bindings={"fixture-site": SESSION}, clock=lambda: NOW,
        runner_factory=SyntheticRunner, adapter_factory=SyntheticAdapter,
    )
    return gateway, store, transports


def test_public_runner_entry_is_fixed_to_one_site_and_execute_false(runner_gateway):
    gateway, store, transports = runner_gateway
    result = execute_checkin_status(request_body(), gateway, clock=lambda: NOW)
    assert result.status == "due" and result.identity_match is True
    assert SyntheticRunner.calls == [(["fixture-site"], False)]
    assert transports.transport.methods == ["GET", "GET"]
    assert SyntheticAdapter.submit_count == 0
    assert store.local_writes == 1  # 只读指不上游提交；原 Runner 可维护本地回执。
    encoded = json.dumps(result.to_dict())
    assert "private response" not in encoded and "reward" not in encoded and UID not in encoded


@pytest.mark.parametrize("action", ["submit", "post", "fresh_login"])
def test_gateway_blocks_mutation_even_if_runner_swallows_the_exception(runner_gateway, monkeypatch, action):
    gateway, _, transports = runner_gateway
    monkeypatch.setattr(SyntheticRunner, "forbidden_action", action)
    with pytest.raises(RemoteError, match="checkin_mutation_blocked"):
        execute_checkin_status(request_body(), gateway, clock=lambda: NOW)
    assert SyntheticAdapter.submit_count == 0
    assert transports.transport.methods == ["GET", "GET"]
    assert transports.transport.logins == 0


def test_cached_receipt_does_not_claim_fresh_browser_identity(runner_gateway, monkeypatch):
    gateway, store, transports = runner_gateway
    monkeypatch.setattr(SyntheticRunner, "cached", True)
    store.cached = dict(site_id="fixture-site", uid=UID, day="2026-11-12", state="success")
    result = execute_checkin_status(request_body(), gateway, clock=lambda: NOW)
    assert result.status == "already_done" and result.identity_match is None
    assert result.evidence == "local_receipt" and transports.open_count == 0


@pytest.mark.parametrize("state", ["pending", "uncertain", "unknown", "failed"])
def test_unconfirmed_local_record_cannot_substantiate_cached_success(runner_gateway, monkeypatch, state):
    gateway, store, _ = runner_gateway
    monkeypatch.setattr(SyntheticRunner, "cached", True)
    store.cached = dict(site_id="fixture-site", uid=UID, day="2026-11-12", state=state)
    assert execute_checkin_status(request_body(), gateway, clock=lambda: NOW).status == "uncertain"


def test_mismatched_live_account_never_reads_checkin_status(runner_gateway, monkeypatch):
    gateway, _, transports = runner_gateway
    monkeypatch.setattr(SyntheticAdapter, "identity", lambda self: SimpleNamespace(uid="different-account"))
    result = execute_checkin_status(request_body(), gateway, clock=lambda: NOW)
    assert result.status == "login_required" and result.identity_match is False
    assert transports.transport.methods == []


def test_optional_local_config_does_not_load_package_or_create_state_until_explicit_factory(tmp_path, monkeypatch):
    calls = []
    module = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "daily_checkin", module)
    monkeypatch.setitem(sys.modules, "daily_checkin.storage", SimpleNamespace(Store=lambda path: calls.append(("store", path)) or MemoryStore()))
    monkeypatch.setitem(sys.modules, "daily_checkin.browser", SimpleNamespace(BrowserFactory=lambda path: calls.append(("browser", path)) or TransportFactory()))
    config = CheckinLocalConfig(tmp_path / "checkin", KEY, {"fixture-site": SESSION})
    assert calls == [] and not config.data_dir.exists()
    assert KEY.decode() not in repr(config)
    create_local_gateway(config, clock=lambda: NOW)
    assert calls == [("store", config.data_dir), ("browser", config.data_dir)]
    assert not config.data_dir.exists()


def test_optional_project_dependency_failure_is_sanitized(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "daily_checkin", None)
    with pytest.raises(RemoteError, match="checkin_dependency_unavailable"):
        create_local_gateway(CheckinLocalConfig(tmp_path / "checkin", KEY, {"fixture-site": SESSION}))


def test_response_schema_rejects_private_extra_fields():
    request = CheckinStatusRequest.from_dict(request_body())
    result = execute_checkin_status(request, Gateway(), clock=lambda: NOW).to_dict()
    result["cookie"] = "never-return-this"
    with pytest.raises(RemoteError, match="invalid_fields"):
        CheckinStatusResult.from_dict(result, request=request)
