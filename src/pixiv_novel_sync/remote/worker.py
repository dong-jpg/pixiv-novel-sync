"""One-slot worker with a supervisor independent of file transfer/rendering."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
import uuid

from .contracts import (
    ATTEMPT_SECONDS, COVER_LIMIT, DIAGNOSTIC_LIMIT, HEARTBEAT_SECONDS,
    MANIFEST_LIMIT, RESULT_LIMIT, STOP_SECONDS, TEXT_LIMIT, WORK_LIMIT,
    WORKER_ERRORS, Grant, RemoteError, canonical_bytes, digest,
    require_digest, require_int, require_time, require_uuid,
)
from .runner import SpawnRunner


@dataclass
class LeaseGuard:
    safe_until: float = 0.0

    def accept(self, *, sent_monotonic: float, received_monotonic: float) -> None:
        if received_monotonic < sent_monotonic:
            raise ValueError("invalid_monotonic_order")
        self.safe_until = max(self.safe_until, sent_monotonic + STOP_SECONDS)

    def can_work(self, now_monotonic: float) -> bool:
        return now_monotonic < self.safe_until


@dataclass
class _Attempt:
    grant: Grant
    directory: Path
    guard: LeaseGuard
    hard_until: float
    next_renew: float
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    seq: int = 0
    pending_seq: int | None = None
    pending_sent: float | None = None
    child: object | None = None
    thread: threading.Thread | None = None
    sha256: str | None = None
    size: int | None = None
    confirmed: bool = False
    error: str | None = None


class Worker:
    """Call tick at least once a second; only this supervisor renews authority.

    Production work runs in a separate thread and a fixed spawned renderer.
    ``threaded=False`` supports deterministic tests with injected transports and
    an immediate renderer; it is not exposed as an operator CLI option.
    """

    def __init__(self, client, work_dir: Path, clock=time.time,
                 monotonic=time.monotonic, runner=None, *, threaded: bool = True):
        self.client = client
        self.work_dir = Path(work_dir)
        self.clock, self.monotonic = clock, monotonic
        self.runner = runner if runner is not None else SpawnRunner()
        self.threaded = threaded
        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._stop = threading.Event()
        self._attempt: _Attempt | None = None
        self._enrolled = False
        self._boot_id = str(uuid.uuid4())
        self._secret = secrets.token_urlsafe(32)
        self._claim_key: str | None = None
        self._claim_sent: float | None = None
        self._last_wall: float | None = None
        self._last_mono: float | None = None
        self._next_session_heartbeat = 0.0
        self._next_request = 0.0
        self._pending_receipts: deque[dict] = deque()
        self._receipt_next = 0.0
        self._cleanup_next = 0.0
        self.completed: deque[str] = deque(maxlen=50)
        self.last_error: str | None = None
        self.state = "registering"
        self._prepare_directory()
        self._watchdog_thread = None
        if threaded:
            self._watchdog_thread = threading.Thread(target=self._watchdog, name="pixiv-remote-watchdog", daemon=True)
            self._watchdog_thread.start()

    @property
    def active_grant(self) -> Grant | None:
        with self._lock:
            return self._attempt.grant if self._attempt is not None else None

    def _prepare_directory(self) -> None:
        root = self.work_dir
        if (not root.is_absolute() or root.parent == root or root.is_symlink()
                or root.resolve() != root.absolute()):
            raise RemoteError("invalid_work_directory", 400)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = root / ".pixiv-remote-worker"
        if not marker.exists():
            if next(root.iterdir(), None) is not None:
                raise RemoteError("work_directory_not_empty", 400)
            with marker.open("xb") as output:
                output.write(b"pixiv-remote-worker-v1\n")
        if marker.is_symlink() or marker.stat().st_size != 23 or marker.read_bytes() != b"pixiv-remote-worker-v1\n":
            raise RemoteError("invalid_work_directory", 400)
        # Previous boots may only reconcile an already produced result. They
        # never restore a session token, a lease, or in-progress computation.
        for entry in root.iterdir():
            if entry == marker:
                continue
            self._check_task_directory(entry)
            receipt = entry / "receipt.json"
            if self.clock() - entry.stat().st_mtime >= 3600:
                self._remove_task(entry)
                continue
            if receipt.is_file() and not receipt.is_symlink():
                try:
                    with receipt.open("rb") as source:
                        value = json.loads(source.read(4097))
                    if set(value) != {"run_id", "attempt_id", "sha256", "size_bytes"}:
                        raise ValueError
                    require_uuid(value["run_id"])
                    if require_uuid(value["attempt_id"]) != entry.name:
                        raise ValueError
                    require_digest(value["sha256"])
                    require_int(value["size_bytes"], 1, RESULT_LIMIT)
                    self._pending_receipts.append(value)
                except (RemoteError, ValueError, TypeError, KeyError):
                    raise RemoteError("invalid_work_directory", 400) from None
        self._check_capacity(0)

    def _cleanup_stale(self) -> None:
        """Worker caches are expendable after a complete run's one-hour window."""
        now = self.clock()
        removed = set()
        for directory in self.work_dir.iterdir():
            if directory.name == ".pixiv-remote-worker":
                continue
            self._check_task_directory(directory)
            if self._attempt is not None and directory == self._attempt.directory:
                continue
            if now - directory.stat().st_mtime >= 3600:
                self._remove_task(directory)
                removed.add(directory.name)
        if removed:
            self._pending_receipts = deque(item for item in self._pending_receipts
                                           if item["attempt_id"] not in removed)
        self._cleanup_next = self.monotonic() + 60

    def _check_task_directory(self, directory: Path) -> None:
        if (directory.parent != self.work_dir or directory.is_symlink()
                or directory.resolve().parent != self.work_dir
                or not directory.is_dir()):
            raise RemoteError("invalid_work_directory", 400)
        require_uuid(directory.name)

    def _check_capacity(self, extra: int) -> None:
        count = 0
        for path in self.work_dir.iterdir():
            if path.is_symlink():
                raise RemoteError("invalid_work_directory", 400)
            if path.is_file():
                count += path.stat().st_size
            else:
                self._check_task_directory(path)
                for leaf in path.iterdir():
                    if leaf.is_symlink() or not leaf.is_file() or leaf.resolve().parent != path:
                        raise RemoteError("invalid_work_directory", 400)
                    count += leaf.stat().st_size
            if count + extra > WORK_LIMIT:
                raise RemoteError("work_quota_exceeded", 413)

    def _remove_task(self, directory: Path) -> None:
        # No recursive deletion: only our UUID directory and its verified files.
        self._check_task_directory(directory)
        files = list(directory.iterdir())
        for leaf in files:
            if leaf.is_symlink() or not leaf.is_file() or leaf.resolve().parent != directory:
                raise RemoteError("invalid_work_directory", 400)
        for leaf in files:
            leaf.unlink()
        directory.rmdir()

    def _restart_session(self) -> None:
        self._enrolled = False
        self._boot_id = str(uuid.uuid4())
        self._secret = secrets.token_urlsafe(32)
        self._claim_key = self._claim_sent = None
        self._next_request = self.monotonic() + 5
        if hasattr(self.client, "reset_session"):
            self.client.reset_session()
        self.state = "registering"

    def _can_work(self, attempt: _Attempt) -> bool:
        now = self.monotonic()
        with self._lock:
            # A resumed transfer thread may run before the supervisor. Check
            # the same discontinuity at EVERY work boundary, not only tick().
            if self._last_wall is not None:
                wall_gap = self.clock() - self._last_wall
                mono_gap = now - self._last_mono
                if mono_gap < 0 or mono_gap >= STOP_SECONDS or abs(wall_gap - mono_gap) > 5:
                    attempt.cancel.set()
                    attempt.error = attempt.error or "lease_lost"
            if not attempt.guard.can_work(now) or now >= attempt.hard_until:
                attempt.cancel.set()
                attempt.error = attempt.error or "lease_lost"
            return (not self._stop.is_set() and not attempt.cancel.is_set()
                    and self._attempt is attempt and attempt.guard.can_work(now)
                    and now < attempt.hard_until)

    def _ensure_work(self, attempt: _Attempt) -> None:
        if not self._can_work(attempt):
            raise RemoteError("lease_lost", 409)

    def _watchdog(self) -> None:
        # A renewal HTTP call can cross a safety deadline. This thread never
        # performs network I/O and still terminates our renderer on time.
        while not self._stop.wait(0.25):
            attempt = self._attempt
            if attempt is not None and not attempt.done.is_set() and not self._can_work(attempt):
                self._cancel_attempt("lease_lost")

    def _cancel_attempt(self, code: str) -> None:
        with self._lock:
            attempt = self._attempt
            if attempt is None:
                return
            attempt.cancel.set()
            attempt.error = attempt.error or code
            child = attempt.child
            self.state = "stopping"
        if child is not None:
            child.stop()

    def _renew(self, attempt: _Attempt) -> None:
        now = self.monotonic()
        with self._lock:
            if attempt.pending_seq is None:
                attempt.pending_seq = attempt.seq + 1
                attempt.pending_sent = now
            seq, sent = attempt.pending_seq, attempt.pending_sent
        try:
            response = self.client.renew(attempt.grant, seq)
            received = self.monotonic()
            require_int(response.get("seq"), 1)
            server_now = require_time(response.get("server_now"))
            lease_until = require_time(response.get("lease_until"))
            if response["seq"] != seq or lease_until <= server_now:
                raise RemoteError("protocol_mismatch", 502)
            if not self._can_work(attempt):
                self._cancel_attempt("lease_lost")
                return
            with self._lock:
                if self._attempt is not attempt or attempt.pending_seq != seq or attempt.cancel.is_set():
                    return
                attempt.guard.accept(sent_monotonic=sent, received_monotonic=received)
                # A replay uses its ORIGINAL send time, including when the API
                # adds a newer server_now around the original receipt.
                attempt.guard.safe_until = min(attempt.guard.safe_until, sent + lease_until - server_now)
                attempt.seq, attempt.pending_seq, attempt.pending_sent = seq, None, None
                attempt.next_renew = received + HEARTBEAT_SECONDS
        except RemoteError as exc:
            if exc.status in {401, 403, 409, 410} or exc.code == "protocol_mismatch":
                self._cancel_attempt("lease_lost")
            else:
                attempt.next_renew = self.monotonic() + 5

    def _session_heartbeat(self) -> None:
        try:
            self.client.session_heartbeat()
            self._next_session_heartbeat = self.monotonic() + HEARTBEAT_SECONDS
        except RemoteError as exc:
            if exc.status in {401, 403, 409, 410}:
                if self._attempt is not None:
                    self._cancel_attempt("lease_lost")
                else:
                    self._restart_session()
            self._next_session_heartbeat = self.monotonic() + 5

    def _supervise(self, attempt: _Attempt) -> None:
        if not self._can_work(attempt):
            self._cancel_attempt("lease_lost")
            return
        if self.monotonic() >= attempt.next_renew:
            self._renew(attempt)
        if not self._can_work(attempt):
            self._cancel_attempt("lease_lost")
            return
        if self.monotonic() >= self._next_session_heartbeat:
            self._session_heartbeat()

    @staticmethod
    def _matches_receipt(value: dict, identity: dict) -> bool:
        return (value.get("state") == "succeeded"
                and all(value.get(key) == identity[key]
                        for key in ("run_id", "attempt_id", "sha256", "size_bytes")))

    def _pipeline(self, attempt: _Attempt) -> None:
        from .epub_task import decode_manifest

        grant, root = attempt.grant, attempt.directory
        try:
            self._ensure_work(attempt)
            encoded = self.client.fetch_input(grant, "manifest")
            self._ensure_work(attempt)
            if len(encoded) > MANIFEST_LIMIT or digest(encoded) != grant.input_sha256:
                raise RemoteError("input_mismatch", 400)
            manifest = decode_manifest(encoded, grant.kind)
            text_path = cover_path = None
            self._check_capacity(RESULT_LIMIT + TEXT_LIMIT + COVER_LIMIT + MANIFEST_LIMIT)
            manifest_path = root / "manifest.json"
            manifest_path.write_bytes(encoded)
            if grant.kind == "epub.single.v1":
                for part in ("text", "cover"):
                    if part == "cover" and manifest["cover_sha256"] is None:
                        continue
                    self._ensure_work(attempt)
                    content = self.client.fetch_input(grant, part)
                    self._ensure_work(attempt)
                    if len(content) != manifest[part + "_size"] or digest(content) != manifest[part + "_sha256"]:
                        raise RemoteError("input_mismatch", 400)
                    target = root / (part + ".bin")
                    target.write_bytes(content)
                    if part == "text":
                        text_path = target
                    else:
                        cover_path = target
            self._ensure_work(attempt)
            output = root / "result.bin"
            with self._lock:
                self._ensure_work(attempt)
                self.state = "running"
                attempt.child = self.runner.start(grant.kind, manifest_path, text_path, cover_path, output)
            while attempt.child.poll() is None:
                self._ensure_work(attempt)
                attempt.cancel.wait(0.1)
            self._ensure_work(attempt)
            if attempt.child.poll() != 0:
                error_path = output.with_suffix(".error")
                code = "render_failed"
                if error_path.is_file() and not error_path.is_symlink():
                    candidate = error_path.read_bytes()[:65].decode("ascii", errors="replace")
                    if candidate in WORKER_ERRORS:
                        code = candidate
                raise RemoteError(code, 400)
            if output.is_symlink() or not output.is_file() or output.resolve().parent != root:
                raise RemoteError("render_failed", 400)
            limit = DIAGNOSTIC_LIMIT if grant.kind == "diagnostic.v1" else RESULT_LIMIT
            size = output.stat().st_size
            if not 0 < size <= limit:
                raise RemoteError("result_too_large", 413)
            hashed = hashlib.sha256()
            with output.open("rb") as source:
                while chunk := source.read(64 * 1024):
                    self._ensure_work(attempt)
                    hashed.update(chunk)
            attempt.sha256, attempt.size = hashed.hexdigest(), size
            identity = {"run_id": grant.run_id, "attempt_id": grant.attempt_id,
                        "sha256": attempt.sha256, "size_bytes": size}
            with (root / "receipt.json").open("xb") as record:
                record.write(canonical_bytes(identity))
                record.flush()
                os.fsync(record.fileno())
            self._ensure_work(attempt)
            self.state = "staging"
            self.client.stage(grant, output, attempt.sha256, size)
            self._ensure_work(attempt)
            self.state = "publishing"
            for retry in range(3):
                self._ensure_work(attempt)
                try:
                    response = self.client.publish(grant, attempt.sha256, size)
                    if not self._matches_receipt(response, identity):
                        raise RemoteError("protocol_mismatch", 502)
                    attempt.confirmed = True
                    break
                except RemoteError as exc:
                    self.state = "reconciling"
                    response = self.client.receipt(grant.run_id)
                    if self._matches_receipt(response, identity):
                        attempt.confirmed = True
                        break
                    if exc.status not in {429, 500, 502, 503, 504}:
                        raise
                    if retry < 2:
                        attempt.cancel.wait(1)
            if not attempt.confirmed:
                raise RemoteError("worker_transient", 503)
        except RemoteError as exc:
            if exc.code in WORKER_ERRORS | {"lease_lost"}:
                attempt.error = exc.code
            elif exc.code == "storage_quota" or exc.status == 429 or 500 <= exc.status <= 599:
                attempt.error = "worker_transient"
            else:
                attempt.error = "input_mismatch"
        except (OSError, ValueError, TypeError):
            attempt.error = "worker_transient"
        except Exception:
            attempt.error = "render_failed"
        finally:
            if attempt.child is not None and attempt.child.poll() is None:
                attempt.child.stop()
            attempt.done.set()

    def _finish(self, attempt: _Attempt) -> None:
        if attempt.confirmed:
            self.completed.append(attempt.grant.run_id)
            self._remove_task(attempt.directory)
            self.last_error = None
        else:
            self.last_error = attempt.error or "worker_transient"
            acknowledged_failure = False
            if self._can_work(attempt) and self.last_error in WORKER_ERRORS:
                try:
                    response = self.client.fail(attempt.grant, self.last_error)
                    acknowledged_failure = (response.get("run_id") == attempt.grant.run_id
                                            and response.get("state") in {"queued", "failed", "cancelled", "expired"})
                except RemoteError:
                    pass
            if attempt.sha256 and attempt.size and not acknowledged_failure:
                self._pending_receipts.append({
                    "run_id": attempt.grant.run_id, "attempt_id": attempt.grant.attempt_id,
                    "sha256": attempt.sha256, "size_bytes": attempt.size,
                })
                self._receipt_next = 0.0
            else:
                # Inputs without an unconfirmed publish need no retention. A
                # server-acknowledged failure similarly cannot later succeed
                # under this attempt; retries receive their own frozen input.
                self._remove_task(attempt.directory)
        with self._lock:
            self._attempt = None
        if attempt.confirmed:
            self.state = "idle"
            self._next_request = self.monotonic() + 1
        else:
            self._restart_session()

    def _reconcile_pending(self) -> None:
        identity = self._pending_receipts[0]
        retire = False
        try:
            response = self.client.receipt(identity["run_id"])
            if response.get("run_id") == identity["run_id"]:
                retire = response.get("state") in {"succeeded", "failed", "cancelled", "expired"}
                if self._matches_receipt(response, identity):
                    self.completed.append(identity["run_id"])
        except RemoteError as exc:
            if exc.status in {404, 410}:
                retire = True
            elif exc.status == 401:
                self._restart_session()
        if retire:
            directory = self.work_dir / identity["attempt_id"]
            if directory.exists():
                self._remove_task(directory)
            self._pending_receipts.popleft()
        else:
            self._pending_receipts.rotate(-1)
        self._receipt_next = self.monotonic() + 30
        self._next_request = self.monotonic() + 5

    def tick(self) -> float:
        with self._tick_lock:
            return self._tick()

    def _tick(self) -> float:
        if self._stop.is_set():
            return self.clock() + 1
        wall, mono = self.clock(), self.monotonic()
        if self._last_wall is not None:
            wall_gap, mono_gap = wall - self._last_wall, mono - self._last_mono
            if self._attempt is not None and (mono_gap >= STOP_SECONDS or mono_gap < 0
                    or abs(wall_gap - mono_gap) > 5):
                self._cancel_attempt("lease_lost")
            elif self._attempt is None and (mono_gap >= STOP_SECONDS or abs(wall_gap - mono_gap) > 5):
                self._restart_session()
        with self._lock:
            self._last_wall, self._last_mono = wall, mono
        if mono >= self._cleanup_next:
            self._cleanup_stale()
        attempt = self._attempt
        if attempt is not None:
            if attempt.done.is_set():
                self._finish(attempt)
            else:
                self._supervise(attempt)
            return self.clock() + 0.5
        if mono < self._next_request:
            return self.clock() + min(1, self._next_request - mono)
        if not self._enrolled:
            try:
                self.client.enroll(self._boot_id, self._secret)
                self._enrolled = True
                self.state = "idle"
                self._next_session_heartbeat = self.monotonic() + HEARTBEAT_SECONDS
                self._next_request = self.monotonic()
            except RemoteError as exc:
                self.last_error = "protocol_mismatch" if exc.code == "protocol_mismatch" else "worker_transient"
                if exc.code in {"retired_boot", "boot_conflict"}:
                    self._boot_id, self._secret = str(uuid.uuid4()), secrets.token_urlsafe(32)
                self._next_request = self.monotonic() + 15
            return self.clock() + 0.5
        if mono >= self._next_session_heartbeat:
            self._session_heartbeat()
            if not self._enrolled:
                return self.clock() + 1
        if self._pending_receipts and mono >= self._receipt_next:
            self._reconcile_pending()
            return self.clock() + 1
        if self._claim_key is None:
            self._claim_key, self._claim_sent = str(uuid.uuid4()), self.monotonic()
        if self.monotonic() - self._claim_sent >= STOP_SECONDS:
            self._restart_session()
            return self.clock() + 1
        try:
            grant = self.client.claim(self._claim_key)
        except RemoteError as exc:
            if exc.code == "claim_terminal" and exc.run_id:
                try:
                    self.client.receipt(exc.run_id)
                except RemoteError:
                    pass
                self._claim_key = self._claim_sent = None
            elif exc.status in {401, 403, 409, 410}:
                self._restart_session()
            self._next_request = self.monotonic() + 5
            return self.clock() + 1
        if grant is None:
            self._claim_key = self._claim_sent = None
            self._next_request = self.monotonic() + 5
            return self.clock() + 1
        guard = LeaseGuard()
        guard.accept(sent_monotonic=self._claim_sent, received_monotonic=self.monotonic())
        guard.safe_until = min(guard.safe_until, self._claim_sent + grant.lease_until - grant.server_now)
        hard_until = self._claim_sent + min(ATTEMPT_SECONDS, grant.hard_deadline - grant.server_now)
        self._claim_key = self._claim_sent = None
        if not guard.can_work(self.monotonic()) or self.monotonic() >= hard_until:
            self._restart_session()
            return self.clock() + 1
        require_uuid(grant.attempt_id)
        directory = self.work_dir / grant.attempt_id
        self._check_capacity(RESULT_LIMIT + TEXT_LIMIT + COVER_LIMIT + MANIFEST_LIMIT)
        directory.mkdir(mode=0o700)
        attempt = _Attempt(grant, directory, guard, hard_until, self.monotonic() + HEARTBEAT_SECONDS)
        with self._lock:
            self._attempt = attempt
            self.state = "fetching"
        if self.threaded:
            attempt.thread = threading.Thread(target=self._pipeline, args=(attempt,),
                                               name="pixiv-remote-transfer", daemon=True)
            attempt.thread.start()
        else:
            self._pipeline(attempt)
        return self.clock() + 0.5

    def stop(self) -> None:
        self._stop.set()
        self._cancel_attempt("lease_lost")
        attempt = self._attempt
        if attempt is not None and attempt.thread is not None:
            attempt.thread.join(timeout=1)
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=1)
        if hasattr(self.client, "close"):
            self.client.close()
        self.state = "stopped"
