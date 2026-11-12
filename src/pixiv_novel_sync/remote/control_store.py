"""Persisted controller decisions, independent of all provider network I/O."""
from __future__ import annotations

import json
import uuid

from .contracts import PROTOCOL, RENDERER, RemoteError, require_int, require_key, require_time
from .instabox import MachineView, MUTATIONS, OperationView, ProviderError


ACTION_FIELDS = frozenset({
    "action_id", "action", "idempotency_key", "operation_id", "activation_id",
    "phase", "created_at", "last_sent_at", "retry_at",
})
ACTION_PHASES = frozenset({"created", "sent", "accepted", "settled", "unknown"})
STICKY_ERRORS = frozenset({
    "provider_authentication", "provider_rejected", "action_expired",
    "operation_unknown", "invalid_control_action", "suspend_uncertain",
    "activation_unconfirmed",
})


def _action(raw):
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != ACTION_FIELDS
                or value["action"] not in MUTATIONS or value["phase"] not in ACTION_PHASES):
            raise ValueError
        require_key(value["action_id"])
        require_key(value["activation_id"])
        require_key(value["idempotency_key"])
        require_time(value["created_at"])
        require_time(value["retry_at"])
        if value["last_sent_at"] is not None:
            require_time(value["last_sent_at"])
        if value["operation_id"] is not None:
            require_key(value["operation_id"])
        return value
    except (ValueError, TypeError, KeyError, RemoteError):
        raise RemoteError("invalid_control_action", 503) from None


class ControlStore:
    def __init__(self, store):
        self.store = store
        self._owner = None

    def _row(self, conn):
        row = conn.execute("SELECT * FROM machine_control WHERE id=1").fetchone()
        if row is None or row["machine_id"] != self.store.machine_id:
            raise RemoteError("machine_mismatch", 503)
        return dict(row)

    @staticmethod
    def _change(conn, row, **values):
        # Only internal callers supply column names; values always use parameters.
        fields = ",".join(f"{name}=?" for name in values)
        result = conn.execute(
            f"UPDATE machine_control SET {fields},revision=revision+1 WHERE id=1 AND revision=?",
            (*values.values(), row["revision"]),
        )
        if result.rowcount != 1:
            raise RemoteError("controller_changed")

    def _leader(self, row, now):
        return self._owner is not None and row["leader_id"] == self._owner and (row["leader_until"] or 0) > now

    def _require_leader(self, row, now):
        if not self._leader(row, now):
            raise RemoteError("controller_not_leader")

    @staticmethod
    def _activity(conn, now):
        queued = conn.execute(
            "SELECT count(*) FROM runs WHERE state='queued' AND not_before<=? AND deadline>?", (now, now),
        ).fetchone()[0]
        active = conn.execute(
            "SELECT count(*) FROM attempts WHERE state='active' AND lease_until>? AND hard_deadline>?", (now, now),
        ).fetchone()[0]
        uploads = conn.execute("SELECT count(*) FROM blob_reservations WHERE expires_at>?", (now,)).fetchone()[0]
        return {"queued_count": queued, "active_count": active, "upload_count": uploads,
                "has_demand": bool(queued or active), "has_activity": bool(queued or active or uploads)}

    def snapshot(self, now):
        require_time(now)
        with self.store.read() as conn:
            row = self._row(conn)
            row["action"] = _action(row["action_json"])
            row.update(self._activity(conn, now))
            row["worker_online"] = bool(conn.execute(
                """SELECT 1 FROM sessions s JOIN meta m ON m.id=1 WHERE s.machine_id=?
                   AND s.epoch=m.epoch AND s.state='active' AND s.expires_at>?
                   AND s.last_seen_at>=? AND s.last_seen_at<=? AND s.protocol=? AND s.renderer=?""",
                (self.store.machine_id, now, now - 60, now, PROTOCOL, RENDERER),
            ).fetchone())
            return row

    def set_hold(self, seconds, now):
        require_int(seconds, 0, 14400)
        require_time(now)
        with self.store.transaction() as conn:
            row = self._row(conn)
            self._change(conn, row, hold_until=now + seconds if seconds else None)
        return self.snapshot(now)

    def claim_leader(self, owner, now):
        require_key(owner)
        require_time(now)
        with self.store.transaction() as conn:
            row = self._row(conn)
            if row["leader_id"] not in (None, owner) and (row["leader_until"] or 0) > now:
                return False
            self._change(conn, row, leader_id=owner, leader_until=now + 60)
            self._owner = owner
            return True

    def initialize_after_recovery(self, now):
        """An explicitly restarted, enabled controller may resume reconciliation.

        The caller invokes this only once after first obtaining leadership.
        Ordinary ticks cannot undo a disabled state, and recovery never restores
        old dispatch gates or ownership of a potentially different running VM.
        """
        with self.store.transaction() as conn:
            row = self._row(conn)
            self._require_leader(row, now)
            if row["mode"] != "disabled" or row["last_error"] != "epoch_rotated":
                return
            self._change(conn, row, mode="idle", last_error=None, gate_ready_until=None,
                         runtime_ready=0, runtime_budget_until=None, last_provider_at=None,
                         activation_id=None, owned_run_started_at=None,
                         activation_previous_run_started_at=None)

    def observe(self, machine: MachineView, now):
        if machine.id != self.store.machine_id:
            raise RemoteError("machine_mismatch", 503)
        with self.store.transaction() as conn:
            row = self._row(conn)
            if not self._leader(row, now):
                return False
            values = dict(
                provider_state=machine.state, last_provider_at=now,
                provider_run_started_at=machine.run_started_at,
                provider_operation_id=machine.operation_id,
                runtime_ready=int(machine.runtime_ready), runtime_budget_until=machine.runtime_budget_until,
                cooldown_until=machine.cooldown_until, gate_ready_until=None,
            )
            if row["owned_run_started_at"] is not None and (
                machine.state != "running" or machine.run_started_at != row["owned_run_started_at"]
            ):
                values.update(owned_run_started_at=None, activation_id=None)
            self._change(conn, row, **values)
            return True

    def decide(self, now):
        """Recompute the dispatch gate and idle state from a fresh DB snapshot."""
        with self.store.transaction() as conn:
            row = self._row(conn)
            self._require_leader(row, now)
            action = _action(row["action_json"])
            if row["mode"] == "disabled" or row["last_error"] in STICKY_ERRORS or action:
                self._change(conn, row, gate_ready_until=None)
                return
            activity = self._activity(conn, now)
            hold = (row["hold_until"] or 0) > now
            fresh = row["last_provider_at"] is not None and 0 <= now - row["last_provider_at"] <= 45
            if (row["cooldown_until"] or 0) > now:
                self._change(conn, row, mode="cooldown", gate_ready_until=None,
                             next_poll_at=max(row["next_poll_at"] or 0, row["cooldown_until"]))
                return
            if not fresh:
                self._change(conn, row, gate_ready_until=None)
                return
            running = row["provider_state"] == "running"
            worker = bool(conn.execute(
                """SELECT 1 FROM sessions s JOIN meta m ON m.id=1 WHERE s.machine_id=?
                   AND s.epoch=m.epoch AND s.state='active' AND s.expires_at>?
                   AND s.last_seen_at>=? AND s.last_seen_at<=? AND s.protocol=? AND s.renderer=?""",
                (self.store.machine_id, now, now - 60, now, PROTOCOL, RENDERER),
            ).fetchone())
            budget = row["runtime_budget_until"]
            ready = bool(running and row["runtime_ready"] and not row["provider_operation_id"] and worker
                         and budget is not None and (budget == -1 or budget - now >= 660))
            idle_since = None if activity["has_activity"] or hold else row["idle_since"]
            if not activity["has_activity"] and not hold and idle_since is None:
                idle_since = now
            if running and not activity["has_activity"] and not hold and now - idle_since >= 300:
                mode = "draining" if row["owned_run_started_at"] is not None else "idle"
                ready = False
            elif ready:
                mode = "ready"
            elif running or row["provider_state"] in ("creating", "starting", "resuming"):
                mode = "starting"
            elif row["provider_state"] in ("suspending", "stopping"):
                mode = "suspending"
            elif row["provider_state"] in ("stopped", "suspended"):
                mode = "starting" if activity["has_demand"] else "idle"
            else:
                mode = "error"
            self._change(conn, row, mode=mode, idle_since=idle_since,
                         gate_ready_until=now + 45 if ready else None)

    def reserve_action(self, action, activation_id, now):
        if action not in MUTATIONS:
            raise RemoteError("invalid_action", 400)
        require_key(activation_id)
        with self.store.transaction() as conn:
            row = self._row(conn)
            self._require_leader(row, now)
            if _action(row["action_json"]) is not None:
                raise RemoteError("action_pending")
            if row["mode"] == "disabled" or row["last_error"] in STICKY_ERRORS:
                raise RemoteError("controller_disabled")
            if row["last_provider_at"] is None or not 0 <= now - row["last_provider_at"] <= 45:
                raise RemoteError("provider_stale")
            if row["provider_operation_id"] or (row["cooldown_until"] or 0) > now:
                raise RemoteError("provider_busy")
            activity = self._activity(conn, now)
            if action == "suspend":
                if (activity["has_activity"] or (row["hold_until"] or 0) > now or row["mode"] != "draining"
                        or row["provider_state"] != "running" or row["owned_run_started_at"] is None
                        or row["owned_run_started_at"] != row["provider_run_started_at"]
                        or row["activation_id"] != activation_id):
                    raise RemoteError("suspend_inhibited")
            elif (not activity["has_demand"] or row["provider_state"] != {"start": "stopped", "resume": "suspended"}[action]):
                raise RemoteError("start_inhibited")
            value = {"action_id": str(uuid.uuid4()), "action": action,
                     "idempotency_key": str(uuid.uuid4()), "operation_id": None,
                     "activation_id": activation_id, "phase": "created", "created_at": now,
                     "last_sent_at": None, "retry_at": now}
            values = dict(action_json=json.dumps(value), mode="suspending" if action == "suspend" else "starting",
                          gate_ready_until=None, last_error=None)
            if action != "suspend":
                values.update(activation_id=activation_id, owned_run_started_at=None,
                              activation_previous_run_started_at=row["provider_run_started_at"])
            self._change(conn, row, **values)
            return value

    def prepare_send(self, action_id, now):
        """Reserve a write and recheck inhibit conditions before network I/O."""
        with self.store.transaction() as conn:
            row = self._row(conn)
            self._require_leader(row, now)
            action = _action(row["action_json"])
            if not action or action["action_id"] != action_id or action["operation_id"]:
                return None
            if row["mode"] == "disabled" or row["last_error"] in STICKY_ERRORS:
                return None
            if now - action["created_at"] >= 7 * 86400:
                self._change(conn, row, mode="error", gate_ready_until=None, last_error="action_expired")
                return None
            if action["last_sent_at"] is None:
                activity = self._activity(conn, now)
                inhibited = (not activity["has_demand"] if action["action"] != "suspend" else
                             activity["has_activity"] or (row["hold_until"] or 0) > now
                             or row["owned_run_started_at"] is None
                             or row["owned_run_started_at"] != row["provider_run_started_at"])
                if inhibited:
                    values = dict(action_json=None, mode="idle", gate_ready_until=None, idle_since=None)
                    if action["action"] != "suspend":
                        values.update(activation_id=None, owned_run_started_at=None)
                    self._change(conn, row, **values)
                    return None
            elif action["action"] != "suspend" and not self._activity(conn, now)["has_demand"]:
                # A retry could be the first accepted activation. Once its
                # business demand expires, keep the unknown intent for review
                # instead of creating a fresh running machine without work.
                self._change(conn, row, mode="error", gate_ready_until=None,
                             last_error="activation_unconfirmed")
                return None
            retry_at = max(action["retry_at"], (row["last_write_at"] or 0) + 30)
            if retry_at > now:
                self._change(conn, row, next_poll_at=retry_at, gate_ready_until=None)
                return None
            action.update(phase="sent", last_sent_at=now, retry_at=now + 15)
            self._change(conn, row, action_json=json.dumps(action), last_write_at=now,
                         next_poll_at=now + 15, gate_ready_until=None)
            return action

    def record_action(self, action_id, response: OperationView, now, *, from_mutation=False):
        if response.machine_id != self.store.machine_id:
            raise RemoteError("machine_mismatch", 503)
        with self.store.transaction() as conn:
            row = self._row(conn)
            if not self._leader(row, now):
                return False
            action = _action(row["action_json"])
            if not action or action["action_id"] != action_id:
                return False
            if action["operation_id"] is not None and response.id != action["operation_id"]:
                return False
            values = dict(gate_ready_until=None, next_poll_at=now + 15,
                          mode="suspending" if action["action"] == "suspend" else "starting",
                          last_error="operation_conflict" if row["last_error"] == "operation_conflict" else None)
            if response.action != action["action"]:
                values.update(activation_id=None, owned_run_started_at=None, last_error="operation_conflict")
            if from_mutation and response.state == "succeeded" and action["action"] != "suspend":
                # An already-complete start can mean someone else started it
                # between our observation and request. Borrow it without pause rights.
                values.update(activation_id=None, owned_run_started_at=None)
            if response.state == "failed":
                values.update(action_json=None, mode="error", activation_id=None, owned_run_started_at=None,
                              last_error=response.error_code or "provider_error", next_poll_at=now + 30,
                              backoff_count=row["backoff_count"] + 1)
            else:
                action.update(operation_id=response.id,
                              phase="settled" if response.state == "succeeded" else "accepted", retry_at=now + 15)
                values["action_json"] = json.dumps(action)
            if row["mode"] == "disabled":
                values["mode"] = "disabled"
            self._change(conn, row, **values)
            return True

    def reconcile_unknown(self, action_id, machine: MachineView, now):
        with self.store.transaction() as conn:
            row = self._row(conn)
            if not self._leader(row, now):
                return
            action = _action(row["action_json"])
            if not action or action["action_id"] != action_id:
                return
            if row["mode"] == "disabled":
                return
            values = dict(next_poll_at=now + 15, gate_ready_until=None)
            if machine.operation_id:
                action.update(operation_id=machine.operation_id, phase="unknown", retry_at=now)
                # The provider's active operation may belong to a concurrent
                # human request. Without our response we cannot claim ownership.
                values.update(activation_id=None, owned_run_started_at=None)
            else:
                if action["action"] == "suspend" and (
                    row["activation_id"] != action["activation_id"] or row["owned_run_started_at"] is None
                    or machine.run_started_at != row["owned_run_started_at"] or machine.state != "running"
                ):
                    self._change(conn, row, mode="error", gate_ready_until=None,
                                 last_error="suspend_uncertain", activation_id=None, owned_run_started_at=None)
                    return
                # A successful-looking machine is not the missing acknowledgement.
                # Retrieve the original response with the original idempotency key.
                action.update(phase="created", retry_at=max(action["retry_at"], now))
            values["action_json"] = json.dumps(action)
            self._change(conn, row, **values)

    def complete_observation(self, action_id, machine: MachineView, now):
        with self.store.transaction() as conn:
            row = self._row(conn)
            if not self._leader(row, now):
                return False
            action = _action(row["action_json"])
            if not action or action["action_id"] != action_id or action["phase"] != "settled":
                return False
            stable = machine.state in ("running", "stopped", "suspended", "deleted", "error") and not machine.operation_id
            if not stable:
                return False
            expected = machine.state == ("suspended" if action["action"] == "suspend" else "running")
            values = dict(action_json=None, gate_ready_until=None, next_poll_at=now,
                          backoff_count=0, last_error=None)
            if not expected and row["last_error"] != "operation_conflict":
                values.update(mode="error", last_error="operation_state_mismatch", activation_id=None,
                              owned_run_started_at=None, next_poll_at=now + 30)
            elif action["action"] == "suspend" or not expected:
                values.update(mode="idle", activation_id=None, owned_run_started_at=None, idle_since=None)
            else:
                values["mode"] = "starting"
                if (row["activation_id"] == action["activation_id"]
                        and machine.run_started_at is not None
                        and machine.run_started_at != row["activation_previous_run_started_at"]
                        and machine.run_started_at >= action["created_at"]):
                    values["owned_run_started_at"] = machine.run_started_at
                else:
                    values.update(activation_id=None, owned_run_started_at=None)
            if row["mode"] == "disabled":
                values["mode"] = "disabled"
            self._change(conn, row, **values)
            return True

    def fail(self, error: ProviderError, now, jitter=0.0, action_id=None):
        with self.store.transaction() as conn:
            row = self._row(conn)
            if not self._leader(row, now):
                return
            action = _action(row["action_json"])
            if action_id is not None and (not action or action["action_id"] != action_id):
                return
            count = min(row["backoff_count"] + 1, 20)
            delay = min(300, 30 * 2 ** (count - 1)) + max(0, min(5, float(jitter)))
            retry_at = max(now + delay, now + (error.retry_after or 0), row["cooldown_until"] or 0)
            values = dict(mode="error", gate_ready_until=None, last_error=error.code,
                          next_poll_at=retry_at, backoff_count=count)
            known_unaccepted = bool(action and not action["operation_id"]
                                    and error.http_status in (409, 429))
            if error.http_status in (401, 403):
                values["last_error"] = "provider_authentication"
            elif error.code == "machine_cooldown":
                values.update(mode="cooldown", cooldown_until=retry_at)
            elif error.code == "operation_conflict" and error.operation_id and action:
                action.update(operation_id=error.operation_id, phase="unknown", retry_at=retry_at)
                values.update(activation_id=None, owned_run_started_at=None)
                known_unaccepted = False
            elif error.http_status == 404 and action and action["operation_id"]:
                values["last_error"] = "operation_unknown"
            elif error.code == "action_expired":
                values["last_error"] = "action_expired"
            elif error.http_status is not None and 400 <= error.http_status < 500 and error.http_status != 429:
                values["last_error"] = "provider_rejected"
            if action:
                if known_unaccepted:
                    action.update(phase="created", last_sent_at=None)
                elif action["phase"] != "settled":
                    action["phase"] = "unknown"
                action["retry_at"] = retry_at
                values["action_json"] = json.dumps(action)
            if row["mode"] == "disabled":
                values["mode"] = "disabled"
            self._change(conn, row, **values)

    def reserve_wake(self, now):
        with self.store.transaction() as conn:
            row = self._row(conn)
            self._require_leader(row, now)
            activity = self._activity(conn, now)
            if (row["mode"] == "disabled" or row["last_error"] in STICKY_ERRORS
                    or row["provider_state"] != "running" or row["provider_operation_id"]
                    or _action(row["action_json"]) or (row["cooldown_until"] or 0) > now
                    or not (activity["has_demand"] or (row["hold_until"] or 0) > now)
                    or row["last_provider_at"] is None or not 0 <= now - row["last_provider_at"] <= 45
                    or (row["last_write_at"] is not None and now - row["last_write_at"] < 30)
                    or (row["last_wake_at"] is not None and now - row["last_wake_at"] < 300)):
                return False
            self._change(conn, row, last_write_at=now, last_wake_at=now)
            return True
