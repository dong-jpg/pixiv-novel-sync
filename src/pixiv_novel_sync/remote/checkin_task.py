"""Optional fixed daily-checkin integration with an at-most-once submission ledger."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .checkin_status import (
    CheckinLocalConfig,
    CheckinStatusRequest,
    CheckinStatusResult,
    DailyCheckinRunnerGateway,
    _ReadGuard,
    _ReadOnlyAdapter,
    _check_binding,
    create_local_gateway,
    execute_checkin_status,
)
from .contracts import RemoteError, canonical_bytes, require_fields

KINDS = frozenset({"checkin.status.v1", "checkin.submit.v1"})


def migrate_checkin(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS checkin_effects(
        site_id TEXT NOT NULL, identity TEXT NOT NULL, business_day TEXT NOT NULL,
        request_sha256 TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('submitted','confirmed')),
        attempt_id TEXT NOT NULL, submitted_at REAL NOT NULL, confirmed_at REAL,
        PRIMARY KEY(site_id,identity,business_day))"""
    )


def validate_manifest(value, kind):
    require_fields(value, {"protocol", "kind", "request"})
    if (
        type(value["protocol"]) is not int
        or value["protocol"] != 1
        or value["kind"] != kind
        or kind not in KINDS
    ):
        raise RemoteError("invalid_manifest", 400)
    CheckinStatusRequest.from_dict(value["request"])
    return value


def validate_result(payload, manifest):
    from ..pixiv_fetch import _decode_json

    return CheckinStatusResult.from_dict(
        _decode_json(payload, 4096), request=CheckinStatusRequest.from_dict(manifest["request"])
    ).to_dict()


def permit_submission(store, grant, manifest, now):
    request = CheckinStatusRequest.from_dict(manifest["request"])
    from .checkin_status import _day_relation

    if _day_relation(request, lambda: datetime.fromtimestamp(now, timezone.utc)):
        raise RemoteError("checkin_business_day_changed", 409)
    with store.transaction() as conn:
        store._check_grant(conn, grant, now)
        old = conn.execute(
            "SELECT * FROM checkin_effects WHERE site_id=? AND identity=? AND business_day=?",
            (request.site_id, request.expected_identity, request.business_day),
        ).fetchone()
        if old is not None:
            if old["attempt_id"] != grant.attempt_id or old["request_sha256"] != request.sha256:
                raise RemoteError("checkin_already_submitted", 409)
            return {"allowed": True, "retry_after": 0}
        conn.execute(
            "INSERT INTO checkin_effects VALUES(?,?,?,?,'submitted',?,?,NULL)",
            (
                request.site_id,
                request.expected_identity,
                request.business_day,
                request.sha256,
                grant.attempt_id,
                now,
            ),
        )
        return {"allowed": True, "retry_after": 0}


def load_worker_config():
    raw = os.environ.get("PIXIV_REMOTE_CHECKIN_CONFIG")
    if not raw:
        raise RemoteError("checkin_not_configured", 503)
    path = Path(raw)
    if (
        not path.is_absolute()
        or path.resolve() != path.absolute()
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > 16384
    ):
        raise RemoteError("checkin_config_unavailable", 503)
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise RemoteError("checkin_config_permissions", 503)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        require_fields(value, {"data_dir", "identity_key_hex", "session_bindings"})
        CheckinLocalConfig(
            Path(value["data_dir"]), bytes.fromhex(value["identity_key_hex"]), value["session_bindings"]
        )
    except (ValueError, TypeError, KeyError):
        raise RemoteError("checkin_config_unavailable", 503) from None
    return value


class SubmitGateway(DailyCheckinRunnerGateway):
    def submit_once(self, request, permit):
        guard = _ReadGuard(self, request)
        runner_factory, adapter_factory = self._factories()
        sending = [False]
        sent = [False]
        mutation_sent = [False]

        class Transport:
            def __init__(self, inner):
                self.inner = inner

            def request(self, method, path, **kwargs):
                guard.check()
                if not isinstance(method, str):
                    guard.forbid()
                if method.upper() not in {"GET", "HEAD"}:
                    if method.upper() != "POST" or not sending[0] or mutation_sent[0]:
                        guard.forbid()
                    mutation_sent[0] = True
                if (
                    not isinstance(path, str)
                    or not path.startswith("/")
                    or path.startswith("//")
                    or "\\" in path
                ):
                    guard.forbid()
                return self.inner.request(method, path, **kwargs)

            def fresh_login(self, *args, **kwargs):
                guard.forbid()

            def detect_challenge(self):
                guard.check()
                return self.inner.detect_challenge()

        class Factory:
            @contextmanager
            def open(_self, site):
                guard.check()
                if site.id != request.site_id:
                    guard.forbid()
                with self.transport_factory.open(site) as transport:
                    yield Transport(transport)

        class Adapter(_ReadOnlyAdapter):
            def submit(self, day, **kwargs):
                guard.check()
                if (
                    sent[0]
                    or day != request.business_day
                    or guard.identity_match is not True
                    or guard.status_state not in {"due", "ready"}
                    or guard.status_day != day
                ):
                    guard.forbid()
                permit(1)
                guard.check()
                sent[0] = True
                sending[0] = True
                try:
                    return self._adapter.submit(day, **kwargs)
                finally:
                    sending[0] = False

        guard.check()
        runner = runner_factory(
            self.store,
            Factory(),
            solver=None,
            clock=self.clock,
            adapter_factory=lambda site, transport: Adapter(adapter_factory(site, transport), guard),
        )
        try:
            runner.run([request.site_id], execute=True)
        except Exception:
            pass
        if guard.violation:
            raise RemoteError("checkin_mutation_blocked", 409)
        if guard.binding_changed or guard.day_relation:
            raise RemoteError("checkin_binding_mismatch", 409)


def execute(manifest, config, permit, *, gateway=None, clock=None):
    request = CheckinStatusRequest.from_dict(manifest["request"])
    clock = clock or (lambda: datetime.now(timezone.utc))
    if gateway is None:
        local = CheckinLocalConfig(
            Path(config["data_dir"]), bytes.fromhex(config["identity_key_hex"]), config["session_bindings"]
        )
        base = create_local_gateway(local, clock=clock)
        gateway = SubmitGateway(
            store=base.store,
            transport_factory=base.transport_factory,
            identity_key=base.identity_key,
            session_bindings=base.session_bindings,
            clock=clock,
        )
    before = execute_checkin_status(request, gateway, clock=clock)
    if manifest["kind"] == "checkin.submit.v1" and before.status == "due":
        if _check_binding(request, gateway.describe_site(request.site_id)):
            raise RemoteError("checkin_binding_mismatch", 409)
        gateway.submit_once(request, permit)
        after = execute_checkin_status(request, gateway, clock=clock)
        if after.status == "due":
            after = CheckinStatusResult(
                1,
                request.site_id,
                request.business_day,
                request.sha256,
                "uncertain",
                True,
                "live_status",
                "ambiguous_status",
            )
        return canonical_bytes(after.to_dict())
    return canonical_bytes(before.to_dict())
