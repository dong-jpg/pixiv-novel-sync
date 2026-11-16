"""Durable single-machine queue with short, fenced SQLite transactions."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid

from .contracts import (
    ATTEMPT_SECONDS, COVER_LIMIT, DIAGNOSTIC_LIMIT, DISK_LIMIT, Grant, KINDS, LEASE_SECONDS,
    MANIFEST_LIMIT, MAX_RUNS, PROTOCOL, RENDERER, RESULT_LIMIT, RUN_SECONDS,
    SESSION_SECONDS, TEXT_LIMIT, WORKER_ERRORS, RemoteError, require_digest,
    require_int, require_key, require_time, require_uuid, retry_delay,
)
from .schema import SCHEMA_VERSION, STATEMENTS


class RemoteStore:
    def __init__(self, state_dir: Path, machine_id: str):
        self.state_dir = Path(state_dir)
        self.machine_id = machine_id
        self.db_path = self.state_dir / "control.db"
        if not self.state_dir.is_absolute() or self.state_dir.parent == self.state_dir:
            raise RemoteError("absolute_directory_required", 400)

    def _safe_paths(self):
        if self.state_dir.resolve() != self.state_dir.absolute() or self.db_path.is_symlink():
            raise RemoteError("unsafe_control_path", 503)

    def _connect(self):
        self._safe_paths()
        conn = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def read(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.read() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def migrate(self):
        self._safe_paths()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.transaction() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise RemoteError("unsupported_control_schema", 503)
            if version == 0:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone():
                    raise RemoteError("not_a_control_database", 503)
                for statement in STATEMENTS:
                    conn.execute(statement)
                conn.execute("INSERT INTO meta VALUES(1,?,1)", (str(uuid.uuid4()),))
                conn.execute("INSERT INTO machine_control(id,machine_id) VALUES(1,?)", (self.machine_id,))
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            row = conn.execute("SELECT machine_id FROM machine_control WHERE id=1").fetchone()
            if row is None or row[0] != self.machine_id:
                raise RemoteError("machine_mismatch", 503)
        with self.read() as conn:
            conn.execute("PRAGMA journal_mode=WAL")

    def close(self):
        # Connections belong to individual operations, including Flask threads.
        pass

    @property
    def epoch(self) -> str:
        with self.read() as conn:
            return conn.execute("SELECT epoch FROM meta WHERE id=1").fetchone()[0]

    @staticmethod
    def _submission(conn, key, request_sha256):
        row = conn.execute("SELECT * FROM runs WHERE scope='dashboard' AND idempotency_key=?", (key,)).fetchone()
        if row and row["request_sha256"] != request_sha256:
            raise RemoteError("idempotency_conflict")
        return dict(row) if row else None

    def find_submission(self, key: str, request_sha256: str):
        require_key(key)
        require_digest(request_sha256)
        with self.read() as conn:
            return self._submission(conn, key, request_sha256)

    @staticmethod
    def _record_activity(conn):
        # 短任务可能在 controller 两次观察之间完成，活动必须同事务持久化。
        # 由下次确认无活动的 tick 开始完整宽限；幂等回执不调用此方法。
        conn.execute("""UPDATE machine_control SET idle_since=NULL,revision=revision+1
                        WHERE id=1 AND idle_since IS NOT NULL""")

    def enqueue(self, key: str, request_sha256: str, kind: str, input_sha256: str, now: float):
        require_key(key)
        require_digest(request_sha256)
        require_digest(input_sha256)
        require_time(now)
        if kind not in KINDS:
            raise RemoteError("unsupported_kind", 400)
        with self.transaction() as conn:
            existing = self._submission(conn, key, request_sha256)
            if existing:
                return existing
            self._sweep(conn, now)
            if conn.execute("SELECT count(*) FROM runs WHERE state IN ('queued','active')").fetchone()[0] >= MAX_RUNS:
                raise RemoteError("queue_full", 429)
            parts = conn.execute("""SELECT p.part,p.blob_sha256,b.state FROM input_parts p
                LEFT JOIN blob_objects b ON b.sha256=p.blob_sha256 WHERE p.input_sha256=?""", (input_sha256,)).fetchall()
            required = {"manifest", "text"} if kind == "epub.single.v1" else {"manifest"}
            by_part = {row["part"]: row for row in parts}
            if not required <= by_part.keys() or by_part["manifest"]["blob_sha256"] != input_sha256:
                raise RemoteError("input_unavailable", 503)
            if any(row["state"] != "ready" for row in parts):
                raise RemoteError("blob_deleting", 503)
            run_id = str(uuid.uuid4())
            conn.execute("""INSERT INTO runs(run_id,idempotency_key,request_sha256,kind,input_sha256,
                         state,created_at,not_before,deadline) VALUES(?,?,?,?,?,'queued',?,?,?)""",
                         (run_id, key, request_sha256, kind, input_sha256, now, now, now + RUN_SECONDS))
            self._record_activity(conn)
            return dict(conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())

    def _session(self, conn, session_id: str, now: float):
        row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        epoch = conn.execute("SELECT epoch FROM meta WHERE id=1").fetchone()[0]
        if (not row or row["state"] != "active" or row["expires_at"] <= now
                or row["machine_id"] != self.machine_id or row["epoch"] != epoch):
            raise RemoteError("invalid_session", 401)
        return row

    @staticmethod
    def _public_session(row):
        return {key: row[key] for key in ("session_id", "epoch", "expires_at")}

    def enroll(self, boot_id: str, token_hash: str, protocol: int, renderer: str, now: float):
        require_uuid(boot_id)
        require_digest(token_hash)
        if type(protocol) is not int or protocol != PROTOCOL or renderer != RENDERER:
            raise RemoteError("protocol_mismatch", 400)
        with self.transaction() as conn:
            epoch = conn.execute("SELECT epoch FROM meta WHERE id=1").fetchone()[0]
            old = conn.execute("SELECT * FROM sessions WHERE machine_id=? AND boot_id=?", (self.machine_id, boot_id)).fetchone()
            if old:
                if old["token_hash"] != token_hash:
                    raise RemoteError("boot_conflict")
                if old["state"] != "active" or old["expires_at"] <= now or old["epoch"] != epoch:
                    raise RemoteError("retired_boot")
                return self._public_session(old)
            if conn.execute("SELECT 1 FROM sessions WHERE token_hash=?", (token_hash,)).fetchone():
                raise RemoteError("session_secret_reused")
            if conn.execute("SELECT count(*) FROM sessions WHERE epoch=?", (epoch,)).fetchone()[0] >= 4096:
                raise RemoteError("session_capacity", 429)
            for attempt in conn.execute("SELECT a.*,r.kind,r.deadline FROM attempts a JOIN runs r USING(run_id) WHERE a.state='active'").fetchall():
                self._end_attempt(conn, attempt, "lost", "worker_restarted", now)
            conn.execute("UPDATE sessions SET state='retired' WHERE machine_id=? AND state='active'", (self.machine_id,))
            session_id = str(uuid.uuid4())
            conn.execute("""INSERT INTO sessions VALUES(?,?,?,?,?,?,?,'active',?,?,?)""",
                         (session_id, self.machine_id, boot_id, token_hash, epoch, protocol, renderer,
                          now, now + SESSION_SECONDS, now))
            return self._public_session(conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone())

    def authenticate(self, token: str, now: float):
        if not isinstance(token, str) or not 1 <= len(token) <= 256:
            raise RemoteError("invalid_session", 401)
        with self.read() as conn:
            row = conn.execute("SELECT session_id FROM sessions WHERE token_hash=?",
                               (hashlib.sha256(token.encode("utf-8")).hexdigest(),)).fetchone()
            if not row:
                raise RemoteError("invalid_session", 401)
            session = self._session(conn, row[0], now)
            return dict(session)

    def session_heartbeat(self, session_id: str, now: float):
        with self.transaction() as conn:
            self._session(conn, session_id, now)
            conn.execute("UPDATE sessions SET last_seen_at=?,expires_at=? WHERE session_id=?",
                         (now, now + SESSION_SECONDS, session_id))
            return {"session_id": session_id, "expires_at": now + SESSION_SECONDS}

    @staticmethod
    def _grant(row, now):
        return Grant(row["run_id"], row["attempt_id"], row["session_id"], row["epoch"],
                     row["fence"], row["input_sha256"], row["kind"], row["lease_until"],
                     row["hard_deadline"], now)

    @staticmethod
    def _attempt(conn, attempt_id):
        return conn.execute("""SELECT a.*,r.input_sha256,r.kind,r.state AS run_state,r.deadline
                               FROM attempts a JOIN runs r USING(run_id) WHERE a.attempt_id=?""", (attempt_id,)).fetchone()

    def _probe_receiver(self) -> int:
        """Prove current writability outside a SQLite transaction.

        This is a readiness snapshot, not a promise about future disk capacity;
        result staging still reserves quota and validates the completed file.
        """
        from .blobs import ensure_safe_directory

        root = ensure_safe_directory(self.state_dir / "blobs")
        probe = None
        try:
            fd, name = tempfile.mkstemp(prefix=".upload-", dir=root)
            probe = Path(name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(b"remote-ready\n")
                stream.flush()
                os.fsync(stream.fileno())
            return shutil.disk_usage(root).free
        finally:
            if probe is not None:
                probe.unlink(missing_ok=True)

    def claim(self, session_id: str, claim_key: str, now: float):
        require_key(claim_key)
        # Preserve original claim receipts even when storage became unavailable.
        # New claims perform bounded file I/O before taking the database lock.
        with self.read() as conn:
            self._session(conn, session_id, now)
            replay = conn.execute("SELECT 1 FROM attempts WHERE session_id=? AND claim_key=?", (session_id, claim_key)).fetchone()
        physical_free = None
        if not replay:
            try:
                physical_free = self._probe_receiver()
            except (OSError, RemoteError):
                physical_free = None
        with self.transaction() as conn:
            self._session(conn, session_id, now)
            self._sweep(conn, now)
            previous = conn.execute("SELECT attempt_id FROM attempts WHERE session_id=? AND claim_key=?", (session_id, claim_key)).fetchone()
            if previous:
                row = self._attempt(conn, previous[0])
                if row["state"] != "active":
                    raise RemoteError("claim_terminal", run_id=row["run_id"], attempt_id=row["attempt_id"])
                grant = self._grant(row, now)
                self._check_grant(conn, grant, now)
                return grant
            session = self._session(conn, session_id, now)
            control = conn.execute("SELECT * FROM machine_control WHERE id=1").fetchone()
            budget = control["runtime_budget_until"]
            if (control["mode"] != "ready" or control["provider_state"] != "running" or not control["runtime_ready"]
                    or control["gate_ready_until"] is None or control["gate_ready_until"] <= now
                    or control["last_provider_at"] is None or not 0 <= now - control["last_provider_at"] <= 45
                    or not 0 <= now - session["last_seen_at"] <= 60
                    or session["protocol"] != PROTOCOL or session["renderer"] != RENDERER
                    or control["provider_operation_id"] is not None
                    or budget is None or (budget != -1 and budget - now < ATTEMPT_SECONDS + 60)):
                return None
            if control["action_json"]:
                try:
                    if json.loads(control["action_json"])["phase"] != "settled":
                        return None
                except (ValueError, KeyError, TypeError):
                    return None
            if conn.execute("SELECT 1 FROM attempts WHERE machine_id=? AND state='active'", (self.machine_id,)).fetchone():
                return None
            run = conn.execute("SELECT * FROM runs WHERE state='queued' AND not_before<=? AND deadline>? ORDER BY created_at,run_id LIMIT 1", (now, now)).fetchone()
            if not run:
                return None
            output_budget = DIAGNOSTIC_LIMIT if run["kind"] == "diagnostic.v1" else RESULT_LIMIT
            physical_extra = 0
            if physical_free is not None:
                try:
                    # A process can die after fsync but before blob registration
                    # or reservation cleanup. Those bytes still consume quota
                    # before a new attempt spends its bounded retry allowance.
                    physical_extra = self._account_files(conn, now)
                except (OSError, RemoteError):
                    physical_free = None
            used = conn.execute("SELECT coalesce(sum(size_bytes),0) FROM blob_objects").fetchone()[0]
            reserved = conn.execute("SELECT coalesce(sum(size_bytes),0) FROM blob_reservations WHERE expires_at>?", (now,)).fetchone()[0]
            if (physical_free is None or physical_free < output_budget
                    or used + reserved + physical_extra + output_budget > DISK_LIMIT):
                conn.execute("UPDATE runs SET error_code='storage_unavailable' WHERE run_id=?", (run["run_id"],))
                return None
            meta = conn.execute("SELECT * FROM meta WHERE id=1").fetchone()
            attempt_no = conn.execute("SELECT count(*) FROM attempts WHERE run_id=?", (run["run_id"],)).fetchone()[0] + 1
            attempt_id = str(uuid.uuid4())
            hard_deadline = min(run["deadline"], now + ATTEMPT_SECONDS)
            conn.execute("""INSERT INTO attempts(attempt_id,run_id,attempt_no,machine_id,session_id,
                claim_key,epoch,fence,state,claimed_at,lease_until,hard_deadline)
                VALUES(?,?,?,?,?,?,?,?,'active',?,?,?)""", (attempt_id, run["run_id"], attempt_no,
                self.machine_id, session_id, claim_key, meta["epoch"], meta["next_fence"],
                now, min(now + LEASE_SECONDS, hard_deadline), hard_deadline))
            conn.execute("UPDATE meta SET next_fence=next_fence+1 WHERE id=1")
            conn.execute("UPDATE runs SET state='active',error_code=NULL WHERE run_id=?", (run["run_id"],))
            self._record_activity(conn)
            return self._grant(self._attempt(conn, attempt_id), now)

    def _check_grant(self, conn, grant: Grant, now: float):
        row = self._attempt(conn, grant.attempt_id)
        self._session(conn, grant.session_id, now)
        current_epoch = conn.execute("SELECT epoch FROM meta WHERE id=1").fetchone()[0]
        if (not row or row["session_id"] != grant.session_id or row["machine_id"] != self.machine_id
                or row["epoch"] != current_epoch or grant.epoch != row["epoch"]
                or type(grant.fence) is not int or grant.fence != row["fence"]
                or grant.run_id != row["run_id"] or grant.input_sha256 != row["input_sha256"]
                or grant.kind != row["kind"] or row["state"] != "active" or row["run_state"] != "active"):
            raise RemoteError("stale_grant")
        if now >= row["lease_until"] or now >= row["hard_deadline"] or now >= row["deadline"]:
            raise RemoteError("lease_expired")

    def check_grant(self, grant: Grant, now: float):
        with self.read() as conn:
            self._check_grant(conn, grant, now)

    def resolve_grant(self, session_id: str, attempt_id: str, epoch: str, fence: int, input_sha256: str, now: float):
        require_uuid(attempt_id)
        require_uuid(epoch)
        require_int(fence, 1)
        require_digest(input_sha256)
        with self.read() as conn:
            row = self._attempt(conn, attempt_id)
            if not row:
                raise RemoteError("attempt_not_found", 404)
            grant = Grant(row["run_id"], attempt_id, session_id, epoch, fence, input_sha256,
                          row["kind"], row["lease_until"], row["hard_deadline"], now)
            self._check_grant(conn, grant, now)
            return grant

    def renew(self, grant: Grant, seq: int, now: float):
        require_int(seq)
        with self.transaction() as conn:
            self._check_grant(conn, grant, now)
            row = self._attempt(conn, grant.attempt_id)
            if seq < row["heartbeat_seq"] or (seq == 0 and not row["heartbeat_receipt_json"]):
                raise RemoteError("heartbeat_out_of_order")
            if seq == row["heartbeat_seq"]:
                return json.loads(row["heartbeat_receipt_json"])
            receipt = {"seq": seq, "lease_until": min(now + LEASE_SECONDS, row["hard_deadline"]), "server_now": now}
            conn.execute("UPDATE attempts SET lease_until=?,heartbeat_seq=?,heartbeat_receipt_json=? WHERE attempt_id=?",
                         (receipt["lease_until"], seq, json.dumps(receipt), grant.attempt_id))
            return receipt

    @staticmethod
    def _end_attempt(conn, row, state, code, now):
        # Enrollment/recovery can observe an old active row before the regular
        # sweeper. Restarting must not turn an already exhausted hard deadline
        # into another retry merely because it won that race.
        if now >= row["hard_deadline"] or now >= row["deadline"]:
            state, code = "expired", "attempt_expired"
        changed = conn.execute("UPDATE attempts SET state=?,error_code=?,ended_at=? WHERE attempt_id=? AND state='active'",
                               (state, code, now, row["attempt_id"]))
        if not changed.rowcount:
            return
        RemoteStore._record_activity(conn)
        delay = retry_delay(row["kind"], row["attempt_no"], code)
        if state == "expired" or now >= row["deadline"]:
            conn.execute("UPDATE runs SET state='expired',error_code=?,finished_at=? WHERE run_id=? AND state='active'", (code, now, row["run_id"]))
        elif delay is not None and now + delay < row["deadline"]:
            conn.execute("UPDATE runs SET state='queued',error_code=?,not_before=? WHERE run_id=? AND state='active'", (code, now + delay, row["run_id"]))
        else:
            conn.execute("UPDATE runs SET state='failed',error_code=?,finished_at=? WHERE run_id=? AND state='active'", (code, now, row["run_id"]))

    def fail(self, grant: Grant, code: str, now: float):
        if code not in WORKER_ERRORS:
            raise RemoteError("invalid_error_code", 400)
        with self.transaction() as conn:
            self._check_grant(conn, grant, now)
            row = self._attempt(conn, grant.attempt_id)
            self._end_attempt(conn, row, "failed", code, now)
            return dict(conn.execute("SELECT * FROM runs WHERE run_id=?", (grant.run_id,)).fetchone())

    def cancel(self, run_id: str, now: float):
        require_uuid(run_id)
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise RemoteError("run_not_found", 404)
            if row["state"] in ("queued", "active"):
                conn.execute("UPDATE attempts SET state='cancelled',error_code='cancelled',ended_at=? WHERE run_id=? AND state='active'", (now, run_id))
                conn.execute("UPDATE runs SET state='cancelled',error_code='cancelled',finished_at=? WHERE run_id=?", (now, run_id))
                self._record_activity(conn)
            return dict(conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())

    def _sweep(self, conn, now):
        changes = 0
        for row in conn.execute("""SELECT a.*,r.kind,r.deadline,s.state AS session_state,s.expires_at
                FROM attempts a JOIN runs r USING(run_id) JOIN sessions s USING(session_id)
                WHERE a.state='active'""").fetchall():
            if now >= row["hard_deadline"] or now >= row["deadline"]:
                self._end_attempt(conn, row, "expired", "attempt_expired", now)
            elif now >= row["lease_until"] or now >= row["expires_at"] or row["session_state"] != "active":
                self._end_attempt(conn, row, "lost", "lease_lost", now)
            else:
                continue
            changes += 1
        expired = conn.execute("UPDATE runs SET state='expired',error_code='run_expired',finished_at=? WHERE state='queued' AND deadline<=?", (now, now)).rowcount
        if expired:
            self._record_activity(conn)
        changes += expired
        conn.execute("UPDATE sessions SET state='expired' WHERE state='active' AND expires_at<=?", (now,))
        conn.execute("DELETE FROM blob_reservations WHERE expires_at<=?", (now,))
        return changes

    def sweep(self, now: float):
        with self.transaction() as conn:
            return self._sweep(conn, now)

    def get_run(self, run_id: str):
        require_uuid(run_id)
        with self.read() as conn:
            row = conn.execute("""SELECT r.*,(SELECT count(*) FROM attempts a WHERE a.run_id=r.run_id) AS attempt_count
                                  FROM runs r WHERE run_id=?""", (run_id,)).fetchone()
            if not row:
                raise RemoteError("run_not_found", 404)
            return dict(row)

    def list_runs(self, limit: int = 50):
        require_int(limit, 1, 50)
        with self.read() as conn:
            return [dict(row) for row in conn.execute("""SELECT r.*,
                (SELECT count(*) FROM attempts a WHERE a.run_id=r.run_id) AS attempt_count
                FROM runs r ORDER BY created_at DESC,run_id LIMIT ?""", (limit,))]

    def rotate_epoch(self, now: float):
        with self.transaction() as conn:
            control = conn.execute("SELECT * FROM machine_control WHERE id=1").fetchone()
            if control["leader_until"] and control["leader_until"] > now:
                raise RemoteError("controller_active")
            for row in conn.execute("SELECT a.*,r.kind,r.deadline FROM attempts a JOIN runs r USING(run_id) WHERE a.state='active'").fetchall():
                self._end_attempt(conn, row, "lost", "worker_restarted", now)
            epoch = str(uuid.uuid4())
            conn.execute("UPDATE meta SET epoch=?,next_fence=1 WHERE id=1", (epoch,))
            conn.execute("UPDATE sessions SET state='retired' WHERE state='active'")
            conn.execute("""UPDATE machine_control SET revision=revision+1,mode='disabled',gate_ready_until=NULL,
                runtime_budget_until=NULL,runtime_ready=0,owned_run_started_at=NULL,activation_id=NULL,
                activation_previous_run_started_at=NULL,leader_id=NULL,leader_until=NULL,last_error='epoch_rotated' WHERE id=1""")
            return epoch

    def reserve_blob(self, purpose: str, owner_key: str, size_bytes: int, now: float):
        if purpose not in {"input", "result"}:
            raise RemoteError("invalid_reservation", 400)
        require_int(size_bytes, 0, RESULT_LIMIT if purpose == "result" else TEXT_LIMIT + COVER_LIMIT + MANIFEST_LIMIT)
        require_key(owner_key)
        with self.transaction() as conn:
            physical_extra = self._account_files(conn, now)
            conn.execute("DELETE FROM blob_reservations WHERE expires_at<=?", (now,))
            if conn.execute("SELECT 1 FROM blob_reservations WHERE purpose=? AND owner_key=?", (purpose, owner_key)).fetchone():
                raise RemoteError("upload_in_progress")
            used = conn.execute("SELECT coalesce(sum(size_bytes),0) FROM blob_objects").fetchone()[0]
            reserved = conn.execute("SELECT coalesce(sum(size_bytes),0) FROM blob_reservations").fetchone()[0]
            if used + reserved + physical_extra + size_bytes > DISK_LIMIT:
                raise RemoteError("storage_quota", 413)
            reservation_id = str(uuid.uuid4())
            conn.execute("INSERT INTO blob_reservations VALUES(?,?,?,?,?)", (reservation_id, purpose, owner_key, size_bytes, now + LEASE_SECONDS))
            self._record_activity(conn)
            return reservation_id

    def _account_files(self, conn, now):
        """Account crash orphans using only flat-directory file metadata.

        Never read blob contents while holding a database lock. Installed SHA
        objects already underwent durable BlobStore validation; unregistered
        ones remain unreferenced until an input/result explicitly registers them.
        Temporary bytes count conservatively in addition to outstanding reserves.
        """
        root = self.state_dir / "blobs"
        if not root.exists():
            return 0
        if root.is_symlink() or root.resolve() != root.absolute():
            raise RemoteError("unsafe_blob_path", 503)
        extra = 0
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue  # GC may have just removed a marked object.
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise RemoteError("unsafe_blob_path", 503)
                if re.fullmatch(r"[0-9a-f]{64}", entry.name):
                    conn.execute("INSERT OR IGNORE INTO blob_objects VALUES(?,?,?,'result','ready')",
                                 (entry.name, info.st_size, now))
                    known = conn.execute("SELECT size_bytes FROM blob_objects WHERE sha256=?", (entry.name,)).fetchone()[0]
                    extra += max(0, info.st_size - known)
                else:
                    extra += info.st_size
        return extra

    def release_reservation(self, reservation_id: str, now: float | None = None):
        with self.transaction() as conn:
            self._account_files(conn, time.time() if now is None else now)
            conn.execute("DELETE FROM blob_reservations WHERE reservation_id=?", (reservation_id,))

    @staticmethod
    def _reservation(conn, reservation_id, purpose, total, now):
        row = conn.execute("SELECT * FROM blob_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
        if not row or row["purpose"] != purpose or row["expires_at"] <= now or row["size_bytes"] < total:
            raise RemoteError("reservation_expired")
        return row

    def _register_blob(self, conn, sha, size, kind, now):
        # This metadata check shares the reference transaction with GC's final
        # unlink. A file installed before GC removed a deleting object cannot
        # become a dangling reference after the deletion marker disappears.
        root = self.state_dir / "blobs"
        if root.resolve() != root.absolute() or root.is_symlink():
            raise RemoteError("unsafe_blob_path", 503)
        try:
            info = (root / sha).lstat()
        except FileNotFoundError:
            raise RemoteError("blob_missing", 503) from None
        if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or info.st_size != size):
            raise RemoteError("blob_conflict", 503)
        row = conn.execute("SELECT * FROM blob_objects WHERE sha256=?", (sha,)).fetchone()
        if row:
            if row["state"] != "ready":
                raise RemoteError("blob_deleting", 503)
            if row["size_bytes"] != size:
                raise RemoteError("blob_conflict")
        else:
            conn.execute("INSERT INTO blob_objects VALUES(?,?,?,?,'ready')", (sha, size, now, kind))

    def register_input(self, input_sha256: str, parts: dict, reservation_id: str, now: float):
        require_digest(input_sha256)
        if not isinstance(parts, dict) or "manifest" not in parts or parts.keys() - {"manifest", "text", "cover"}:
            raise RemoteError("invalid_input_parts", 400)
        for part, (sha, size) in parts.items():
            require_digest(sha)
            require_int(size, 0, {"manifest": MANIFEST_LIMIT, "text": TEXT_LIMIT, "cover": COVER_LIMIT}[part])
        if parts["manifest"][0] != input_sha256:
            raise RemoteError("input_mismatch")
        with self.transaction() as conn:
            self._reservation(conn, reservation_id, "input", sum(size for _, size in parts.values()), now)
            existing = {row["part"]: (row["blob_sha256"], row["size_bytes"]) for row in conn.execute("SELECT * FROM input_parts WHERE input_sha256=?", (input_sha256,))}
            if existing and existing != parts:
                raise RemoteError("input_mismatch")
            for part, (sha, size) in parts.items():
                self._register_blob(conn, sha, size, "input", now)
                conn.execute("INSERT OR IGNORE INTO input_parts VALUES(?,?,?,?)", (input_sha256, part, sha, size))
            conn.execute("DELETE FROM blob_reservations WHERE reservation_id=?", (reservation_id,))

    def register_staged(self, grant: Grant, sha256: str, size_bytes: int, reservation_id: str, now: float):
        require_digest(sha256)
        require_int(size_bytes, 0, RESULT_LIMIT)
        with self.transaction() as conn:
            self._check_grant(conn, grant, now)
            self._reservation(conn, reservation_id, "result", size_bytes, now)
            old = conn.execute("SELECT * FROM artifacts WHERE attempt_id=? AND chunk_id=0", (grant.attempt_id,)).fetchone()
            if old and (old["sha256"] != sha256 or old["size_bytes"] != size_bytes):
                raise RemoteError("artifact_conflict")
            self._register_blob(conn, sha256, size_bytes, "result", now)
            conn.execute("INSERT OR IGNORE INTO artifacts VALUES(?,0,?,?,?,'staged',?,NULL)", (grant.attempt_id, sha256, size_bytes, sha256, now))
            conn.execute("DELETE FROM blob_reservations WHERE reservation_id=?", (reservation_id,))
            return {"run_id": grant.run_id, "attempt_id": grant.attempt_id, "state": "staged", "sha256": sha256, "size_bytes": size_bytes}
