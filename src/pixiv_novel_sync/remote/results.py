"""Validate bounded worker output, then publish it in one control transaction."""
from __future__ import annotations

import ntpath
import re
import stat
from typing import BinaryIO, Callable
import zipfile
import zlib

from .blobs import BlobStore, CHUNK_SIZE, validate_sha256
from .contracts import DIAGNOSTIC_LIMIT, RESULT_LIMIT, Grant, RemoteError, require_time
from .epub_task import MANIFEST_LIMIT, decode_manifest

EXPANDED_LIMIT = 128 * 1024 * 1024
MAX_ZIP_MEMBERS = 4096
_REQUIRED_MEMBERS = frozenset({
    "mimetype", "META-INF/container.xml", "EPUB/content.opf", "EPUB/toc.ncx",
    "EPUB/nav.xhtml", "EPUB/style/main.css", "EPUB/chap_001.xhtml",
})
_COVER_MEMBERS = frozenset(f"EPUB/images/cover.{extension}" for extension in ("jpg", "png", "gif", "webp"))
_DIRECTORIES = frozenset({"META-INF/", "EPUB/", "EPUB/style/", "EPUB/images/"})


def _expected_member(name: str) -> bool:
    if name in _REQUIRED_MEMBERS | _COVER_MEMBERS | _DIRECTORIES | {"EPUB/cover.xhtml"}:
        return True
    chapter = re.fullmatch(r"EPUB/chap_([0-9]{3})\.xhtml", name)
    return bool(chapter and 1 <= int(chapter[1]) <= 500)


def _valid_member(info: zipfile.ZipInfo) -> bool:
    # ZipInfo normalizes backslashes on Windows; inspect the original wire name.
    name = info.orig_filename
    parts = name.rstrip("/").split("/")
    mode = info.external_attr >> 16
    directory = name.endswith("/")
    return bool(
        name and "\x00" not in info.orig_filename and "\\" not in name
        and not name.startswith("/") and not ntpath.splitdrive(name)[0]
        and _expected_member(name)
        and all(part not in {"", ".", ".."} for part in parts)
        and not (info.flag_bits & 1)
        and stat.S_IFMT(mode) in ({0, stat.S_IFDIR} if directory else {0, stat.S_IFREG})
        and (not directory or info.file_size == 0)
        and info.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
        and info.file_size >= 0
    )


def validate_epub(source: BinaryIO) -> None:
    """Inspect/stream every member without extraction or XML entity processing."""
    try:
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if (not members or len(members) > MAX_ZIP_MEMBERS
                    or len(names) != len(set(names))
                    or any(not _valid_member(member) for member in members)
                    or sum(member.file_size for member in members) > EXPANDED_LIMIT
                    or members[0].filename != "mimetype"
                    or members[0].compress_type != zipfile.ZIP_STORED
                    or members[0].file_size != len(b"application/epub+zip")
                    or not _REQUIRED_MEMBERS <= set(names)):
                raise RemoteError("invalid_epub", 400)
            covers = _COVER_MEMBERS & set(names)
            if len(covers) > 1 or ("EPUB/cover.xhtml" in names) != bool(covers):
                raise RemoteError("invalid_epub", 400)
            total = 0
            for member in members:
                count = 0
                mimetype = bytearray()
                with archive.open(member) as content:
                    while chunk := content.read(CHUNK_SIZE):
                        count += len(chunk)
                        total += len(chunk)
                        if total > EXPANDED_LIMIT or count > member.file_size:
                            raise RemoteError("invalid_epub", 400)
                        if member.filename == "mimetype":
                            mimetype.extend(chunk)
                if count != member.file_size:
                    raise RemoteError("invalid_epub", 400)
                if member.filename == "mimetype" and bytes(mimetype) != b"application/epub+zip":
                    raise RemoteError("invalid_epub", 400)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, EOFError, zlib.error, ValueError) as exc:
        if isinstance(exc, RemoteError):
            raise
        raise RemoteError("invalid_epub", 400) from None


def _stage_receipt(grant: Grant, sha256: str, size: int) -> dict:
    return {"run_id": grant.run_id, "attempt_id": grant.attempt_id, "state": "staged",
            "sha256": sha256, "size_bytes": size}


def _published_receipt(run, attempt, artifact) -> dict:
    return {"run_id": run["run_id"], "attempt_id": attempt["attempt_id"],
            "state": "succeeded", "sha256": artifact["sha256"],
            "size_bytes": artifact["size_bytes"], "published_at": artifact["published_at"]}


class ResultService:
    def __init__(self, store, blobs: BlobStore, clock: Callable[[], float] | None = None):
        self.store, self.blobs, self.clock = store, blobs, clock

    def _now(self, started_at: float) -> float:
        return max(require_time(started_at), require_time(self.clock())) if self.clock else require_time(started_at)

    @staticmethod
    def _validate_size(grant: Grant, size: int, sha256: str) -> None:
        validate_sha256(sha256)
        if type(size) is not int or size < 0:
            raise RemoteError("invalid_size", 400)
        if grant.kind not in {"diagnostic.v1", "epub.single.v1"}:
            raise RemoteError("unsupported_kind", 400)
        limit = DIAGNOSTIC_LIMIT if grant.kind == "diagnostic.v1" else RESULT_LIMIT
        if size > limit:
            raise RemoteError("result_too_large", 413)

    def _validate_output(self, grant: Grant, sha256: str) -> None:
        with self.blobs.open_blob(sha256) as source:
            if grant.kind == "epub.single.v1":
                validate_epub(source)
                return
            payload = source.read(DIAGNOSTIC_LIMIT + 1)
        with self.blobs.open_blob(grant.input_sha256) as source:
            manifest_bytes = source.read(MANIFEST_LIMIT + 1)
        try:
            output = decode_manifest(payload, "diagnostic.v1")
            expected = decode_manifest(manifest_bytes, "diagnostic.v1")
            if len(payload) > DIAGNOSTIC_LIMIT or output != expected:
                raise RemoteError("invalid_diagnostic", 400)
        except RemoteError:
            raise RemoteError("invalid_diagnostic", 400) from None

    def stage(self, grant: Grant, stream: BinaryIO, size: int, sha256: str, now: float) -> dict:
        self._validate_size(grant, size, sha256)
        now = self._now(now)
        self.store.check_grant(grant, now)
        with self.store.read() as conn:
            old = conn.execute("SELECT * FROM artifacts WHERE attempt_id=? AND chunk_id=0",
                               (grant.attempt_id,)).fetchone()
            if old is not None and (old["sha256"] != sha256 or old["size_bytes"] != size):
                raise RemoteError("result_conflict", 409)
        if old is not None:
            if old["state"] != "staged":
                raise RemoteError("result_not_staged", 409)
            self.blobs.verify(sha256, size)
            self.store.check_grant(grant, self._now(now))
            return _stage_receipt(grant, sha256, size)
        reservation = self.store.reserve_blob("result", grant.attempt_id, size, now)
        try:
            limit = DIAGNOSTIC_LIMIT if grant.kind == "diagnostic.v1" else RESULT_LIMIT
            self.blobs.receive(stream, size, sha256, limit)
            self._validate_output(grant, sha256)
            # Storage/network/validation happened outside the control transaction.
            # The store checks the current lease and cancellation once more here.
            self.store.register_staged(grant, sha256, size, reservation, self._now(now))
            return _stage_receipt(grant, sha256, size)
        except OSError:
            raise RemoteError("storage_unavailable", 503) from None
        finally:
            self.store.release_reservation(reservation, self._now(now))

    def _records(self, conn, grant: Grant, now: float):
        session = self.store._session(conn, grant.session_id, now)
        attempt = conn.execute("SELECT * FROM attempts WHERE attempt_id=?", (grant.attempt_id,)).fetchone()
        run = conn.execute("SELECT * FROM runs WHERE run_id=?", (grant.run_id,)).fetchone()
        if attempt is None or run is None:
            raise RemoteError("run_not_found", 404)
        if (attempt["run_id"] != run["run_id"] or attempt["machine_id"] != session["machine_id"]
                or attempt["epoch"] != grant.epoch or attempt["fence"] != grant.fence
                or run["input_sha256"] != grant.input_sha256 or run["kind"] != grant.kind):
            raise RemoteError("invalid_grant", 409)
        artifact = conn.execute("SELECT * FROM artifacts WHERE attempt_id=? AND chunk_id=0",
                                (grant.attempt_id,)).fetchone()
        return run, attempt, artifact

    def _publication(self, conn, grant: Grant, sha256: str, size: int, now: float):
        run, attempt, artifact = self._records(conn, grant, now)
        if run["state"] == "succeeded" and attempt["state"] == "succeeded":
            if artifact is None or artifact["sha256"] != sha256 or artifact["size_bytes"] != size:
                raise RemoteError("result_conflict", 409)
            if artifact["state"] == "expired":
                raise RemoteError("result_expired", 410)
            if artifact["state"] != "published":
                raise RemoteError("result_not_staged", 409)
            return _published_receipt(run, attempt, artifact)
        self.store._check_grant(conn, grant, now)
        if artifact is None or artifact["state"] != "staged":
            raise RemoteError("result_not_staged", 409)
        if artifact["sha256"] != sha256 or artifact["size_bytes"] != size or artifact["blob_ref"] != sha256:
            raise RemoteError("result_conflict", 409)
        return None

    def publish(self, grant: Grant, sha256: str, size: int, now: float) -> dict:
        self._validate_size(grant, size, sha256)
        now = self._now(now)
        with self.store.read() as conn:
            receipt = self._publication(conn, grant, sha256, size, now)
            if receipt is not None:
                return receipt
        self.blobs.verify(sha256, size)
        now = self._now(now)
        with self.store.transaction() as conn:
            receipt = self._publication(conn, grant, sha256, size, now)
            if receipt is not None:
                return receipt
            blob = conn.execute("SELECT * FROM blob_objects WHERE sha256=?", (sha256,)).fetchone()
            if blob is None or blob["state"] != "ready" or blob["size_bytes"] != size:
                raise RemoteError("blob_unavailable", 503)
            conn.execute("UPDATE artifacts SET state='published',published_at=? WHERE attempt_id=? AND chunk_id=0",
                         (now, grant.attempt_id))
            conn.execute("UPDATE attempts SET state='succeeded',ended_at=?,error_code=NULL WHERE attempt_id=?",
                         (now, grant.attempt_id))
            conn.execute("UPDATE runs SET state='succeeded',finished_at=?,error_code=NULL WHERE run_id=?",
                         (now, grant.run_id))
            run, attempt, artifact = self._records(conn, grant, now)
            return _published_receipt(run, attempt, artifact)

    def receipt(self, run_id: str, session_id: str, now: float) -> dict:
        now = self._now(now)
        with self.store.read() as conn:
            session = self.store._session(conn, session_id, now)
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise RemoteError("run_not_found", 404)
            if run["state"] != "succeeded":
                return {"run_id": run_id, "state": run["state"]}
            attempt = conn.execute("SELECT * FROM attempts WHERE run_id=? AND state='succeeded'", (run_id,)).fetchone()
            if attempt is None or attempt["machine_id"] != session["machine_id"]:
                raise RemoteError("run_not_found", 404)
            artifact = conn.execute("SELECT * FROM artifacts WHERE attempt_id=? AND chunk_id=0",
                                    (attempt["attempt_id"],)).fetchone()
            if artifact is None or artifact["state"] == "expired":
                raise RemoteError("result_expired", 410)
            if artifact["state"] != "published":
                raise RemoteError("result_unavailable", 503)
            return _published_receipt(run, attempt, artifact)
