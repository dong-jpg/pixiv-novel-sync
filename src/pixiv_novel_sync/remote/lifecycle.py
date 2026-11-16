"""One provider call per tick; queue state, not a time-of-day loop, drives use."""
from __future__ import annotations

import random
import time
import uuid

from .contracts import RemoteError
from .control_store import STICKY_ERRORS
from .instabox import ProviderError


class Controller:
    def __init__(self, store, control, provider, clock=time.time, jitter=None):
        self.store = store
        self.control = control
        self.provider = provider
        self.clock = clock
        self.jitter = jitter if jitter is not None else lambda: random.uniform(0, 5)
        self.owner = str(uuid.uuid4())
        self._initialized = False

    def _send(self, action, now):
        prepared = self.control.prepare_send(action["action_id"], now)
        if prepared:
            try:
                response = self.provider.mutate(prepared["action"], prepared["idempotency_key"])
            except ProviderError as error:
                self.control.fail(error, self.clock(), self.jitter(), prepared["action_id"],
                                  from_mutation=True)
                return
            self.control.record_action(prepared["action_id"], response, self.clock(), from_mutation=True)

    def _pending(self, action, now):
        if now - action["created_at"] >= 7 * 86400 and action["phase"] != "settled":
            raise ProviderError("action_expired")
        if action["phase"] == "settled":
            machine = self.provider.machine()
            now = self.clock()
            self.control.observe(machine, now)
            if self.control.complete_observation(action["action_id"], machine, now):
                self.control.decide(now)
        elif action["operation_id"]:
            response = self.provider.operation(action["operation_id"])
            self.control.record_action(action["action_id"], response, self.clock())
        elif action["phase"] in ("sent", "unknown"):
            machine = self.provider.machine()
            now = self.clock()
            self.control.observe(machine, now)
            self.control.reconcile_unknown(action["action_id"], machine, now)
        else:
            snapshot = self.control.snapshot(now)
            observed_at = snapshot["last_provider_at"]
            if observed_at is None or not 0 <= now - observed_at <= 45:
                # Persisted, unsent intents and known-rejected retries may have
                # waited through a controller restart or a long Retry-After.
                # Reconfirm the current VM before using its cached ownership.
                machine = self.provider.machine()
                now = self.clock()
                self.control.observe(machine, now)
                if action["last_sent_at"] is not None:
                    self.control.reconcile_unknown(action["action_id"], machine, now)
            else:
                self._send(action, now)

    def tick(self):
        now = self.clock()
        if not self.control.claim_leader(self.owner, now):
            return now + 15
        if not self._initialized:
            self.control.initialize_after_recovery(now)
            self._initialized = True
        self.store.sweep(now)
        snapshot = self.control.snapshot(now)
        if snapshot["mode"] == "disabled" or snapshot["last_error"] in STICKY_ERRORS:
            return now + 15
        if (snapshot["next_poll_at"] or 0) > now:
            return now + 15
        action = snapshot["action"]
        try:
            if action:
                self._pending(action, now)
                return self.clock() + 15
            # Every other normal tick refreshes the provider. The intervening
            # tick can issue one bounded write using that recent observation.
            if snapshot["last_provider_at"] is None or not 0 <= now - snapshot["last_provider_at"] < 30:
                machine = self.provider.machine()
                now = self.clock()
                self.control.observe(machine, now)
                self.control.decide(now)
                return now + 15
            previous_mode = snapshot["mode"]
            self.control.decide(now)
            snapshot = self.control.snapshot(now)
            if snapshot["mode"] == "cooldown" or snapshot["provider_operation_id"]:
                return now + 15
            state = snapshot["provider_state"]
            if state in ("stopped", "suspended") and snapshot["has_demand"]:
                action = self.control.reserve_action(
                    "start" if state == "stopped" else "resume", str(uuid.uuid4()), now,
                )
                self._send(action, now)
            elif state == "running":
                if snapshot["mode"] == "draining" and previous_mode == "draining":
                    action = self.control.reserve_action("suspend", snapshot["activation_id"], now)
                    self._send(action, now)
                elif self.control.reserve_wake(now):
                    machine = self.provider.wake()
                    now = self.clock()
                    # Wake's response is a fresh observation, never independent
                    # evidence that a pending start completed.
                    self.control.observe(machine, now)
                    self.control.decide(now)
        except ProviderError as error:
            self.control.fail(error, self.clock(), self.jitter(), action["action_id"] if action else None)
        except RemoteError as error:
            # These are ordinary concurrent demand/hold/leader changes. The
            # next leader/tick recomputes state instead of forcing an action.
            if error.code not in {"controller_not_leader", "controller_changed", "action_pending",
                                  "suspend_inhibited", "start_inhibited", "provider_stale", "provider_busy",
                                  "controller_disabled"}:
                raise
        return self.clock() + 15
