"""Offline epoch recovery and resumable GC restricted to the control directory."""
from __future__ import annotations

import os
import re
import stat

from .contracts import METADATA_SECONDS, RETENTION_SECONDS, RemoteError, require_time

ORPHAN_GRACE = 3600


def recover(store, now: float):
    """The CLI requires the API-offline attestation and exact directory match."""
    require_time(now)
    return {"epoch": store.rotate_epoch(now)}


def _unlink(blobs, name):
    if re.fullmatch(r"[0-9a-f]{64}|\.upload-[A-Za-z0-9_-]+", name) is None:
        raise RemoteError("unsafe_gc_path", 503)
    root = blobs.root
    if root.resolve() != root.absolute() or root.is_symlink():
        raise RemoteError("unsafe_gc_path", 503)
    path = root / name
    if path.parent != root:
        raise RemoteError("unsafe_gc_path", 503)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise RemoteError("unsafe_gc_path", 503)
    path.unlink()
    blobs._sync_directory()
    return True


def collect_garbage(store, blobs, now: float):
    require_time(now)
    if blobs.root != store.state_dir / "blobs":
        raise RemoteError("gc_directory_mismatch", 400)
    cutoff = now - RETENTION_SECONDS
    deleted = 0
    with store.transaction() as conn:
        store._sweep(conn, now)
        store._account_files(conn, now)
        # Keep the succeeded metadata to distinguish a cleaned download (410)
        # from an unknown run. Old failed-attempt results follow the run's life.
        conn.execute("""UPDATE artifacts SET state='expired' WHERE attempt_id IN (
            SELECT a.attempt_id FROM attempts a JOIN runs r USING(run_id)
            WHERE r.state NOT IN ('queued','active') AND r.finished_at<=?)""", (cutoff,))
        reservations = conn.execute("SELECT count(*) FROM blob_reservations WHERE expires_at>?", (now,)).fetchone()[0]
        if not reservations:
            conn.execute("""DELETE FROM input_parts WHERE input_sha256 NOT IN (
                SELECT input_sha256 FROM runs WHERE state IN ('queued','active') OR finished_at>?)
                AND input_sha256 IN (SELECT sha256 FROM blob_objects WHERE created_at<=?)""",
                         (cutoff, now - ORPHAN_GRACE))
            conn.execute("""UPDATE blob_objects SET state='deleting' WHERE state='ready' AND created_at<=?
                AND sha256 NOT IN (SELECT blob_sha256 FROM input_parts)
                AND sha256 NOT IN (SELECT blob_ref FROM artifacts WHERE state IN ('staged','published'))""",
                         (now - ORPHAN_GRACE,))
        marked = [row[0] for row in conn.execute("SELECT sha256 FROM blob_objects WHERE state='deleting'")]
        old_runs = [row[0] for row in conn.execute("SELECT run_id FROM runs WHERE finished_at<=? AND state NOT IN ('queued','active')",
                                                 (now - METADATA_SECONDS,))]
        for run_id in old_runs:
            conn.execute("DELETE FROM artifacts WHERE attempt_id IN (SELECT attempt_id FROM attempts WHERE run_id=?)", (run_id,))
            conn.execute("DELETE FROM attempts WHERE run_id=?", (run_id,))
            conn.execute("DELETE FROM runs WHERE run_id=?", (run_id,))
        # Never remove this epoch's boot tombstones: old requests must not gain
        # enrollment rights merely because GC freed historical metadata.
        conn.execute("""DELETE FROM sessions WHERE state!='active' AND epoch!=(SELECT epoch FROM meta WHERE id=1)
            AND created_at<=? AND session_id NOT IN (SELECT session_id FROM attempts)""", (now - METADATA_SECONDS,))
    for sha in marked:
        try:
            # Recheck and unlink under the same short transaction. A second GC
            # may have acted on our snapshot, followed by a new ready object at
            # the same digest. Never unlink that replacement from a stale list.
            with store.transaction() as conn:
                row = conn.execute("SELECT state FROM blob_objects WHERE sha256=?", (sha,)).fetchone()
                if row is None or row[0] != "deleting":
                    continue
                removed = _unlink(blobs, sha)
                conn.execute("DELETE FROM blob_objects WHERE sha256=? AND state='deleting'", (sha,))
        except PermissionError:
            continue  # Windows open handle; retain deleting state for the next GC.
        deleted += int(removed)
    temporary = 0
    if not reservations:
        # Only known temporary names can be removed. Files are not recursively
        # enumerated, and no caller-supplied path reaches unlink.
        with os.scandir(blobs.root) as entries:
            candidates = []
            for entry in entries:
                if re.fullmatch(r"\.upload-[A-Za-z0-9_-]+", entry.name):
                    try:
                        if entry.stat(follow_symlinks=False).st_mtime <= now - ORPHAN_GRACE:
                            candidates.append(entry.name)
                    except FileNotFoundError:
                        pass
        for name in candidates:
            try:
                temporary += int(_unlink(blobs, name))
            except PermissionError:
                pass
    return {"blobs_removed": deleted, "temporary_removed": temporary, "runs_removed": len(old_runs)}
