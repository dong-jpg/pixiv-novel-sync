from __future__ import annotations

from dataclasses import replace
import io
import json
import stat
from types import SimpleNamespace
import uuid
import warnings
import zipfile

import pytest

from pixiv_novel_sync.remote.blobs import BlobStore
from pixiv_novel_sync.remote.contracts import RemoteError, canonical_bytes, digest, request_digest
from pixiv_novel_sync.remote.epub_task import freeze_diagnostic, freeze_epub, render_epub
from pixiv_novel_sync.remote.results import ResultService, validate_epub
from pixiv_novel_sync.remote.store import RemoteStore


@pytest.fixture
def result_fixture(tmp_path):
    store = RemoteStore(tmp_path / "state", "fixture-machine")
    store.migrate()
    blobs = BlobStore(tmp_path / "state" / "blobs")
    clock = SimpleNamespace(now=1000.0)
    session = store.enroll(str(uuid.uuid4()), digest(b"session-secret"), 1, "epub-v1", clock.now)

    def enqueue(kind="diagnostic.v1"):
        if kind == "diagnostic.v1":
            sha = freeze_diagnostic(blobs)
        else:
            sha = freeze_epub({"novel_id": 42, "title": "Fixture", "text_raw": "One[newpage]Two"}, None, blobs)
        with blobs.open_blob(sha) as source:
            raw = source.read()
        manifest = json.loads(raw)
        parts = {"manifest": (sha, len(raw))}
        if kind != "diagnostic.v1":
            parts["text"] = (manifest["text_sha256"], manifest["text_size"])
        key = str(uuid.uuid4())
        reservation = store.reserve_blob("input", key, sum(size for _, size in parts.values()), clock.now)
        store.register_input(sha, parts, reservation, clock.now)
        run = store.enqueue(key, request_digest(kind, 42 if kind != "diagnostic.v1" else None), kind, sha, clock.now)
        with store.transaction() as conn:
            conn.execute("""UPDATE machine_control SET mode='ready', provider_state='running',runtime_ready=1,
                            gate_ready_until=?,last_provider_at=?,runtime_budget_until=-1""",
                         (clock.now + 45, clock.now))
        grant = store.claim(session["session_id"], str(uuid.uuid4()), clock.now)
        assert grant is not None
        output = raw if kind == "diagnostic.v1" else render_epub(manifest, b"One[newpage]Two", None, tmp_path / "render")
        return run, grant, output

    service = ResultService(store, blobs, clock=lambda: clock.now)
    yield SimpleNamespace(store=store, blobs=blobs, clock=clock, session=session, enqueue=enqueue, service=service)
    store.close()


def test_stage_and_publish_are_idempotent_and_receipt_survives_lost_response(result_fixture):
    f = result_fixture
    run, grant, payload = f.enqueue()
    sha = digest(payload)
    first = f.service.stage(grant, io.BytesIO(payload), len(payload), sha, f.clock.now)
    assert first == f.service.stage(grant, io.BytesIO(payload), len(payload), sha, f.clock.now)
    assert f.store.get_run(run["run_id"])["state"] == "active"
    receipt = f.service.publish(grant, sha, len(payload), f.clock.now)
    assert receipt["state"] == "succeeded"
    f.clock.now += 200  # Old task lease is now past, but the original ACK is durable.
    assert f.service.publish(grant, sha, len(payload), f.clock.now) == receipt
    assert f.service.receipt(run["run_id"], f.session["session_id"], f.clock.now) == receipt
    with f.store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts WHERE state='succeeded'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM blob_reservations").fetchone()[0] == 0


def test_epub_result_validates_and_publishes(result_fixture):
    f = result_fixture
    run, grant, payload = f.enqueue("epub.single.v1")
    f.service.stage(grant, io.BytesIO(payload), len(payload), digest(payload), f.clock.now)
    receipt = f.service.publish(grant, digest(payload), len(payload), f.clock.now)
    assert receipt["run_id"] == run["run_id"]
    with f.blobs.open_blob(receipt["sha256"]) as source:
        validate_epub(source)


def test_changed_result_cannot_replace_staged_or_published_object(result_fixture):
    f = result_fixture
    _, grant, payload = f.enqueue()
    sha = digest(payload)
    f.service.stage(grant, io.BytesIO(payload), len(payload), sha, f.clock.now)
    with pytest.raises(RemoteError, match="result_conflict"):
        f.service.stage(grant, io.BytesIO(b"other"), 5, digest(b"other"), f.clock.now)
    f.service.publish(grant, sha, len(payload), f.clock.now)
    with pytest.raises(RemoteError, match="result_conflict"):
        f.service.publish(grant, digest(b"other"), 5, f.clock.now)


def test_diagnostic_output_must_match_server_challenge(result_fixture):
    f = result_fixture
    run, grant, payload = f.enqueue()
    value = json.loads(payload)
    value["challenge"] = "0" * 64
    wrong = canonical_bytes(value)
    with pytest.raises(RemoteError, match="invalid_diagnostic"):
        f.service.stage(grant, io.BytesIO(wrong), len(wrong), digest(wrong), f.clock.now)
    assert f.store.get_run(run["run_id"])["state"] == "active"
    with f.store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM blob_reservations").fetchone()[0] == 0


@pytest.mark.parametrize("reason", ["cancel", "expired"])
def test_upload_rechecks_grant_after_stream_finishes(result_fixture, reason):
    f = result_fixture
    run, grant, payload = f.enqueue()

    class ChangedDuringUpload(io.BytesIO):
        changed = False

        def read(self, size=-1):
            if not self.changed:
                self.changed = True
                if reason == "cancel":
                    f.store.cancel(run["run_id"], f.clock.now)
                else:
                    f.clock.now += 181
            return super().read(size)

    with pytest.raises(RemoteError):
        f.service.stage(grant, ChangedDuringUpload(payload), len(payload), digest(payload), 1000)
    with f.store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM blob_reservations").fetchone()[0] == 0
    assert f.store.get_run(run["run_id"])["state"] != "succeeded"


def test_crash_after_file_sync_before_database_keeps_only_an_orphan(result_fixture, monkeypatch):
    f = result_fixture
    run, grant, payload = f.enqueue()

    def unavailable(*args):
        raise OSError("fixture database unavailable")

    monkeypatch.setattr(f.store, "register_staged", unavailable)
    with pytest.raises(RemoteError, match="storage_unavailable"):
        f.service.stage(grant, io.BytesIO(payload), len(payload), digest(payload), f.clock.now)
    with f.blobs.open_blob(digest(payload)) as source:
        assert source.read() == payload
    with f.store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM blob_reservations").fetchone()[0] == 0
    assert f.store.get_run(run["run_id"])["state"] == "active"


def test_publish_refuses_corruption_after_staging(result_fixture):
    f = result_fixture
    run, grant, payload = f.enqueue()
    sha = digest(payload)
    f.service.stage(grant, io.BytesIO(payload), len(payload), sha, f.clock.now)
    (f.blobs.root / sha).write_bytes(b"corrupt")
    with pytest.raises(RemoteError, match="blob_corrupt"):
        f.service.publish(grant, sha, len(payload), f.clock.now)
    assert f.store.get_run(run["run_id"])["state"] == "active"


def test_cancel_after_staging_revokes_publish(result_fixture):
    f = result_fixture
    run, grant, payload = f.enqueue()
    sha = digest(payload)
    f.service.stage(grant, io.BytesIO(payload), len(payload), sha, f.clock.now)
    f.store.cancel(run["run_id"], f.clock.now)
    with pytest.raises(RemoteError):
        f.service.publish(grant, sha, len(payload), f.clock.now)
    assert f.store.get_run(run["run_id"])["state"] == "cancelled"
    with f.store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts WHERE state='published'").fetchone()[0] == 0


def test_receipt_requires_current_session_but_can_confirm_previous_boot_success(result_fixture):
    f = result_fixture
    run, grant, payload = f.enqueue()
    sha = digest(payload)
    f.service.stage(grant, io.BytesIO(payload), len(payload), sha, f.clock.now)
    original = f.service.publish(grant, sha, len(payload), f.clock.now)
    newer = f.store.enroll(str(uuid.uuid4()), digest(b"new-session-secret"), 1, "epub-v1", 1001)
    with pytest.raises(RemoteError):
        f.service.receipt(run["run_id"], grant.session_id, 1001)
    assert f.service.receipt(run["run_id"], newer["session_id"], 1001) == original
    assert f.service.publish(replace(grant, session_id=newer["session_id"]), sha, len(payload), 1001) == original


def _archive(extra=(), *, compression=zipfile.ZIP_STORED):
    target = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(target, "w", compression=compression) as archive:
            archive.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
            archive.writestr("META-INF/container.xml", b"<container/>")
            archive.writestr("EPUB/content.opf", b"<package/>")
            archive.writestr("EPUB/toc.ncx", b"<ncx/>")
            archive.writestr("EPUB/nav.xhtml", b"<html/>")
            archive.writestr("EPUB/style/main.css", b"p{}")
            archive.writestr("EPUB/chap_001.xhtml", b"<html>chapter one</html>")
            for name, data in extra:
                archive.writestr(name, data)
    raw = target.getvalue()
    # Python's Windows ZIP writer normalizes these separators. Restore the
    # malicious on-wire spelling in both the local and central directory names.
    for name, _ in extra:
        if isinstance(name, str) and "\\" in name:
            raw = raw.replace(name.replace("\\", "/").encode(), name.encode())
    return io.BytesIO(raw)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/absolute", "C:drive", "EPUB\\evil", "EPUB/../evil", "META-INF/container.xml", "payload.exe", "EPUB/chap_501.xhtml", "EPUB/chap_000.xhtml"])
def test_zip_members_reject_unsafe_or_duplicate_names(name):
    with pytest.raises(RemoteError, match="invalid_epub"):
        validate_epub(_archive([(name, b"bad")]))


def test_zip_symlink_and_unsupported_compression_are_rejected():
    link = zipfile.ZipInfo("EPUB/chap_002.xhtml")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(RemoteError, match="invalid_epub"):
        validate_epub(_archive([(link, b"/outside")]))
    with pytest.raises(RemoteError, match="invalid_epub"):
        validate_epub(_archive([("EPUB/chap_002.xhtml", b"data")], compression=zipfile.ZIP_BZIP2))


def test_zip_crc_mimetype_and_expanded_budget_are_verified(monkeypatch):
    raw = _archive([("EPUB/chap_002.xhtml", b"unique-content")]).getvalue()
    with pytest.raises(RemoteError, match="invalid_epub"):
        validate_epub(io.BytesIO(raw.replace(b"unique-content", b"broken-content")))
    with pytest.raises(RemoteError, match="invalid_epub"):
        validate_epub(io.BytesIO(raw.replace(b"application/epub+zip", b"application/epub+BAD")))
    monkeypatch.setattr("pixiv_novel_sync.remote.results.EXPANDED_LIMIT", 40)
    with pytest.raises(RemoteError, match="invalid_epub"):
        validate_epub(_archive([("EPUB/chap_002.xhtml", b"a" * 100)]))


def test_closed_renderer_package_requires_book_members_and_consistent_cover():
    validate_epub(_archive())
    with pytest.raises(RemoteError, match="invalid_epub"):
        validate_epub(_archive([("EPUB/images/cover.png", b"image-without-cover-page")]))
    with pytest.raises(RemoteError, match="invalid_epub"):
        validate_epub(_archive([("EPUB/cover.xhtml", b"page-without-image")]))
    bare = io.BytesIO()
    with zipfile.ZipFile(bare, "w") as archive:
        archive.writestr("mimetype", b"application/epub+zip")
        archive.writestr("META-INF/container.xml", b"<container/>")
    bare.seek(0)
    with pytest.raises(RemoteError, match="invalid_epub"):
        validate_epub(bare)
