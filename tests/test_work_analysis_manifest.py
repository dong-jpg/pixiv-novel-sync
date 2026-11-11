import copy
import hashlib

import pytest

from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError, fingerprint
from pixiv_novel_sync.work_analysis.manifest import (
    capture_manifest, preview_fingerprint, public_preview_identity,
    read_source_texts, validate_manifest, validate_selection,
)


@pytest.fixture
def source_db(tmp_path):
    db = Database(tmp_path / "manifest.db")
    db.init_schema()
    with db.transaction() as conn:
        conn.execute("INSERT INTO users(user_id,name,raw_json) VALUES(1,'Synthetic','{}')")
        conn.execute("INSERT INTO series(series_id,title,user_id,total_novels) VALUES(10,'Series',1,4)")
        for novel_id, body in ((1, "Opening\U0001f600\nEnding"), (2, "tiny"), (3, "   ")):
            conn.execute(
                "INSERT INTO novels(novel_id,user_id,series_id,title,visible,restrict_value,x_restrict,"
                "text_length,total_bookmarks,total_views,tags_json,raw_json,meta_hash,create_date) "
                "VALUES(?,1,10,?,1,'public',0,?,0,0,'[]','{}',?,'2026-01-01')",
                (novel_id, f"Chapter {novel_id}", len(body), "0" * 64),
            )
            conn.execute("INSERT INTO novel_texts(novel_id,text_raw,text_hash,has_content) VALUES(?,?,?,?)",
                         (novel_id, body, hashlib.sha256(body.encode()).hexdigest(), int(bool(body.strip()))))
    try:
        yield db
    finally:
        db.close()


def test_snapshot_includes_short_blank_and_missing_count_without_read_writes(source_db):
    before = source_db.conn.total_changes
    with source_db.read_transaction() as conn:
        manifest, texts = capture_manifest(conn, {"source_kind": "series", "source_id": 10})
    assert source_db.conn.total_changes == before
    assert [row["novel_id"] for row in manifest["chapters"]] == [1, 2, 3]
    assert manifest["expected_count"] == 4
    assert manifest["remote_inventory_verified"] is False
    assert manifest["requires_partial_confirmation"] is True
    assert manifest["chapters"][1]["chars"] == 4
    assert manifest["chapters"][2]["has_text"] is False
    assert manifest["chapters"][2]["text_hash"] is not None
    assert set(texts) == {1, 2, 3}
    assert "text_raw" not in str(manifest) and "Opening" not in str(manifest)
    assert validate_manifest(manifest) == manifest


def test_snapshot_retains_a_chapter_with_no_text_row_without_read_writes(source_db):
    with source_db.transaction() as conn:
        conn.execute("DELETE FROM novel_texts WHERE novel_id=2")
    before = source_db.conn.total_changes
    with source_db.read_transaction() as conn:
        manifest, texts = capture_manifest(conn, {"source_kind": "series", "source_id": 10})
        assert conn.execute("SELECT COUNT(*) FROM novels WHERE novel_id=2").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM novel_texts WHERE novel_id=2").fetchone()[0] == 0
    assert source_db.conn.total_changes == before
    assert [row["novel_id"] for row in manifest["chapters"]] == [1, 2, 3]
    assert [row["position"] for row in manifest["chapters"]] == [0, 1, 2]
    assert manifest["chapters"][1] == {
        "novel_id": 2, "position": 1, "title": "Chapter 2", "text_hash": None,
        "chars": 0, "utf8_bytes": 0, "fetched_at": None, "has_text": False,
    }
    assert manifest["chapters"][0]["has_text"] is True
    assert manifest["chapters"][0]["text_hash"] is not None
    assert manifest["chapters"][2]["text_hash"] is not None
    assert manifest["chapters"][2]["has_text"] is False
    assert set(texts) == {1, 3}
    assert manifest["requires_partial_confirmation"] is True
    assert validate_manifest(manifest) == manifest


def test_preview_fingerprint_uses_semantics_not_key_order_or_observation_times(source_db):
    selection = {"source_kind": "series", "source_id": 10}
    with source_db.read_transaction() as conn:
        manifest, _ = capture_manifest(conn, selection)
    inputs = {"selection": selection, "manifest": manifest,
              "plan": {"strategy": "whole", "minimum_requests": 1},
              "configuration": {"agent_id": 7, "output_tokens": 128},
              "budget": {"request_limit": 2, "token_limit": 10000, "active_seconds_limit": 60}}
    original = copy.deepcopy(inputs)
    identity = preview_fingerprint(**inputs)
    reordered = {name: dict(reversed(list(value.items()))) for name, value in inputs.items()}
    reordered["manifest"]["chapters"] = [dict(reversed(list(row.items()))) for row in manifest["chapters"]]
    assert preview_fingerprint(**reordered) == identity

    for observation in ["fetched_at", "expected_count_observed_at"]:
        changed = copy.deepcopy(inputs)
        target = changed["manifest"]["chapters"][0] if observation == "fetched_at" else changed["manifest"]
        target[observation] = "2030-01-01"
        assert validate_manifest(changed["manifest"]) == changed["manifest"]
        assert preview_fingerprint(**changed) == identity
    explicit_default = copy.deepcopy(inputs)
    explicit_default["selection"]["publication_state"] = "unknown"
    assert preview_fingerprint(**explicit_default) == identity

    for part, field, value in [
        ("selection", "publication_state", "completed"),
        ("manifest", "text_hash", "f" * 64),
        ("plan", "minimum_requests", 2),
        ("configuration", "output_tokens", 256),
        ("budget", "token_limit", 10001),
    ]:
        changed = copy.deepcopy(inputs)
        if part == "manifest":
            changed[part]["chapters"][0][field] = value
            changed[part]["manifest_fingerprint"] = fingerprint(public_preview_identity(changed[part]))
        else:
            changed[part][field] = value
        assert validate_manifest(changed["manifest"]) == changed["manifest"]
        assert preview_fingerprint(**changed) != identity, part
    assert inputs == original


def test_exact_order_and_hash_checked_not_inferred_from_dates(source_db):
    selection = {"source_kind": "series", "source_id": 10, "ordered_novel_ids": [2, 1, 3]}
    with source_db.read_transaction() as conn:
        manifest, texts = capture_manifest(conn, selection)
    assert [row["position"] for row in manifest["chapters"]] == [0, 1, 2]
    assert [row["novel_id"] for row in manifest["chapters"]] == [2, 1, 3]
    with source_db.read_transaction() as conn:
        with pytest.raises(WorkAnalysisError):
            capture_manifest(conn, {**selection, "ordered_novel_ids": [1, 2]})
    source_db.conn.execute("UPDATE novel_texts SET text_hash=? WHERE novel_id=1", ("0" * 64,))
    with source_db.read_transaction() as conn:
        with pytest.raises(WorkAnalysisError, match="source_hash_mismatch"):
            capture_manifest(conn, selection)


def test_noop_observation_does_not_change_identity_but_body_change_does(source_db):
    with source_db.read_transaction() as conn:
        manifest, _ = capture_manifest(conn, {"source_kind": "novel", "source_id": 1})
    source_db.conn.execute("UPDATE novel_texts SET fetched_at='2030-01-01' WHERE novel_id=1")
    with source_db.read_transaction() as conn:
        refreshed, _ = capture_manifest(conn, {"source_kind": "novel", "source_id": 1})
        assert read_source_texts(conn, manifest)[1].endswith("Ending")
    assert manifest["manifest_fingerprint"] == refreshed["manifest_fingerprint"]
    assert public_preview_identity(manifest) == public_preview_identity(refreshed)
    body = "Changed"
    source_db.conn.execute("UPDATE novel_texts SET text_raw=?,text_hash=? WHERE novel_id=1",
                           (body, hashlib.sha256(body.encode()).hexdigest()))
    with source_db.read_transaction() as conn:
        with pytest.raises(WorkAnalysisError, match="source_changed"):
            read_source_texts(conn, manifest)
    corrupt = copy.deepcopy(manifest)
    corrupt["chapters"][0]["start"] = 0
    with pytest.raises(WorkAnalysisError):
        validate_manifest(corrupt)


@pytest.mark.parametrize("field,value", [
    ("source_kind", []), ("source_kind", {}),
    ("publication_state", []), ("publication_state", {}),
])
def test_selection_enums_reject_non_scalar_json_with_domain_errors(field, value):
    selection = {"source_kind": "novel", "source_id": 1, "publication_state": "unknown"}
    with pytest.raises(WorkAnalysisError):
        validate_selection({**selection, field: value})


@pytest.mark.parametrize("field", ["publication_source", "order_source"])
def test_manifest_enums_reject_non_scalar_values(source_db, field):
    with source_db.read_transaction() as conn:
        manifest, _ = capture_manifest(conn, {"source_kind": "novel", "source_id": 1})
    with pytest.raises(WorkAnalysisError):
        validate_manifest({**manifest, field: []})
