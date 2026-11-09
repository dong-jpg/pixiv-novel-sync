from __future__ import annotations

import hashlib
import json

from .contracts import (
    WorkAnalysisError, canonical_bytes, fingerprint, hex_id,
    strict_int, strict_object, strict_text, validate_budget,
)


CHAPTER_KEYS = {"novel_id", "position", "title", "text_hash", "chars", "utf8_bytes", "fetched_at", "has_text"}
MANIFEST_KEYS = {"schema_version", "source_kind", "source_id", "title", "publication_state",
                 "publication_source", "order_source", "expected_count", "expected_count_observed_at",
                 "remote_inventory_verified", "requires_partial_confirmation", "chapters", "manifest_fingerprint"}
MAX_BYTES = 64 * 1024 * 1024


def validate_selection(value):
    value = strict_object(value, {"source_kind", "source_id"},
                          optional={"ordered_novel_ids", "publication_state"})
    if not isinstance(value["source_kind"], str) or value["source_kind"] not in {"novel", "series"}:
        raise WorkAnalysisError("invalid_source")
    result = {"source_kind": value["source_kind"], "source_id": strict_int(value["source_id"], 1, 2**63 - 1),
              "publication_state": value.get("publication_state", "unknown")}
    if (not isinstance(result["publication_state"], str)
            or result["publication_state"] not in {"unknown", "ongoing", "completed"}):
        raise WorkAnalysisError("invalid_source")
    if "ordered_novel_ids" in value:
        ordered = value["ordered_novel_ids"]
        if not isinstance(ordered, list) or not 1 <= len(ordered) <= 2048:
            raise WorkAnalysisError("invalid_order")
        ordered = [strict_int(item, 1, 2**63 - 1) for item in ordered]
        if len(set(ordered)) != len(ordered):
            raise WorkAnalysisError("invalid_order")
        if result["source_kind"] == "novel" and ordered != [result["source_id"]]:
            raise WorkAnalysisError("invalid_order")
        result["ordered_novel_ids"] = ordered
    return result


def public_preview_identity(manifest):
    result = {key: value for key, value in manifest.items()
              if key not in {"manifest_fingerprint", "expected_count_observed_at", "chapters"}}
    result["chapters"] = [{key: value for key, value in row.items() if key != "fetched_at"}
                          for row in manifest["chapters"]]
    return result


def preview_fingerprint(selection, manifest, plan, configuration, budget):
    return fingerprint({"schema_version": 1, "selection": validate_selection(selection),
                        "manifest": public_preview_identity(manifest), "plan": plan,
                        "configuration": configuration, "budget": validate_budget(budget)})


def validate_manifest(value):
    value = strict_object(value, MANIFEST_KEYS)
    if value["schema_version"] != 1 or type(value["schema_version"]) is not int:
        raise WorkAnalysisError("invalid_manifest")
    validate_selection({key: value[key] for key in ("source_kind", "source_id", "publication_state")})
    strict_text(value["title"], 4096, nonempty=False)
    if (not isinstance(value["publication_source"], str)
            or value["publication_source"] not in {"unknown", "user_declared"}):
        raise WorkAnalysisError("invalid_manifest")
    if (not isinstance(value["order_source"], str)
            or value["order_source"] not in {"publication_date_suggestion", "user_confirmed"}):
        raise WorkAnalysisError("invalid_manifest")
    if value["remote_inventory_verified"] is not False or type(value["requires_partial_confirmation"]) is not bool:
        raise WorkAnalysisError("invalid_manifest")
    if value["expected_count"] is not None:
        strict_int(value["expected_count"], 0, 2**63 - 1)
    if value["expected_count_observed_at"] is not None:
        strict_text(value["expected_count_observed_at"], 64)
    chapters = value["chapters"]
    if not isinstance(chapters, list) or not 1 <= len(chapters) <= 2048:
        raise WorkAnalysisError("empty_source", 409)
    ids, total = set(), 0
    for position, chapter in enumerate(chapters):
        strict_object(chapter, CHAPTER_KEYS)
        novel_id = strict_int(chapter["novel_id"], 1, 2**63 - 1)
        if novel_id in ids or type(chapter["position"]) is not int or chapter["position"] != position:
            raise WorkAnalysisError("invalid_order")
        ids.add(novel_id)
        strict_text(chapter["title"], 4096, nonempty=False)
        strict_int(chapter["chars"], 0, MAX_BYTES)
        total += strict_int(chapter["utf8_bytes"], 0, MAX_BYTES)
        if type(chapter["has_text"]) is not bool:
            raise WorkAnalysisError("invalid_manifest")
        if chapter["text_hash"] is None:
            if chapter["chars"] or chapter["utf8_bytes"] or chapter["has_text"]:
                raise WorkAnalysisError("invalid_manifest")
        else:
            hex_id(chapter["text_hash"])
        if chapter["has_text"] and (not chapter["chars"] or chapter["text_hash"] is None):
            raise WorkAnalysisError("invalid_manifest")
        if chapter["fetched_at"] is not None:
            strict_text(chapter["fetched_at"], 64)
    if value["source_kind"] == "novel" and ids != {value["source_id"]}:
        raise WorkAnalysisError("invalid_manifest")
    if total > MAX_BYTES or len(canonical_bytes(value)) > 2 * 1024 * 1024:
        raise WorkAnalysisError("input_too_large", 413)
    if value["requires_partial_confirmation"] is not True:
        # The current archive cannot establish a remotely verified full inventory.
        raise WorkAnalysisError("invalid_manifest")
    if fingerprint(public_preview_identity(value)) != hex_id(value["manifest_fingerprint"]):
        raise WorkAnalysisError("invalid_manifest")
    return json.loads(canonical_bytes(value))


def _require_transaction(conn):
    if not conn.in_transaction:
        raise WorkAnalysisError("snapshot_transaction_required", 409)


def _fetch_texts(conn, ids):
    texts, metadata = {}, {}
    for start in range(0, len(ids), 256):
        batch = ids[start:start + 256]
        marks = ",".join("?" for _ in batch)
        rows = conn.execute(
            "SELECT n.novel_id,t.text_raw,t.text_hash,t.fetched_at FROM novels n "
            "LEFT JOIN novel_texts t ON t.novel_id=n.novel_id WHERE n.novel_id IN (" + marks + ")", batch,
        ).fetchall()
        for row in rows:
            raw, stored = row["text_raw"], row["text_hash"]
            if raw is None:
                if stored is not None:
                    raise WorkAnalysisError("source_hash_mismatch", 409)
                metadata[row["novel_id"]] = (None, 0, 0, None, False)
                continue
            if not isinstance(raw, str):
                raise WorkAnalysisError("source_hash_mismatch", 409)
            try:
                encoded = raw.encode("utf-8")
            except UnicodeError:
                raise WorkAnalysisError("source_hash_mismatch", 409) from None
            actual = hashlib.sha256(encoded).hexdigest()
            if actual != stored:
                raise WorkAnalysisError("source_hash_mismatch", 409)
            texts[row["novel_id"]] = raw
            metadata[row["novel_id"]] = (actual, len(raw), len(encoded), row["fetched_at"], bool(raw.strip()))
    return texts, metadata


def capture_manifest(conn, selection):
    _require_transaction(conn)
    selection = validate_selection(selection)
    source_id, kind = selection["source_id"], selection["source_kind"]
    if kind == "series":
        source = conn.execute("SELECT title,total_novels,last_seen_at FROM series WHERE series_id=?", (source_id,)).fetchone()
        predicate = "n.series_id=?"
    else:
        source = conn.execute("SELECT title FROM novels WHERE novel_id=?", (source_id,)).fetchone()
        predicate = "n.novel_id=?"
    if source is None:
        raise WorkAnalysisError("not_found", 404)
    rows = conn.execute(
        "SELECT n.novel_id,n.title,coalesce(length(CAST(t.text_raw AS BLOB)),0) AS bytes "
        "FROM novels n LEFT JOIN novel_texts t ON t.novel_id=n.novel_id WHERE " + predicate +
        " ORDER BY n.create_date,n.novel_id LIMIT 2049", (source_id,),
    ).fetchall()
    if not rows:
        raise WorkAnalysisError("empty_source", 409)
    if len(rows) > 2048 or sum(row["bytes"] for row in rows) > MAX_BYTES:
        raise WorkAnalysisError("input_too_large", 413)
    by_id = {row["novel_id"]: row for row in rows}
    ordered = selection.get("ordered_novel_ids", list(by_id))
    if set(ordered) != set(by_id) or len(ordered) != len(by_id):
        raise WorkAnalysisError("invalid_order", 409)
    texts, metadata = _fetch_texts(conn, ordered)
    chapters = []
    for position, novel_id in enumerate(ordered):
        hashed, chars, size, fetched, present = metadata[novel_id]
        chapters.append({"novel_id": novel_id, "position": position, "title": by_id[novel_id]["title"],
                         "text_hash": hashed, "chars": chars, "utf8_bytes": size,
                         "fetched_at": fetched, "has_text": present})
    expected = source["total_novels"] if kind == "series" else 1
    if kind == "series" and (type(expected) is not int or expected <= 0):
        expected = None
    manifest = {
        "schema_version": 1, "source_kind": kind, "source_id": source_id, "title": source["title"],
        "publication_state": selection["publication_state"],
        "publication_source": "unknown" if selection["publication_state"] == "unknown" else "user_declared",
        "order_source": "user_confirmed" if kind == "novel" or "ordered_novel_ids" in selection else "publication_date_suggestion",
        "expected_count": expected,
        "expected_count_observed_at": source["last_seen_at"] if kind == "series" else None,
        "remote_inventory_verified": False, "requires_partial_confirmation": True,
        "chapters": chapters,
    }
    manifest["manifest_fingerprint"] = fingerprint(public_preview_identity(manifest))
    return validate_manifest(manifest), texts


def read_source_texts(conn, manifest):
    _require_transaction(conn)
    manifest = validate_manifest(manifest)
    ids = [row["novel_id"] for row in manifest["chapters"]]
    total = 0
    for start in range(0, len(ids), 256):
        batch = ids[start:start + 256]
        marks = ",".join("?" for _ in batch)
        total += conn.execute(
            "SELECT coalesce(sum(length(CAST(text_raw AS BLOB))),0) FROM novel_texts WHERE novel_id IN (" + marks + ")", batch,
        ).fetchone()[0]
    if total > MAX_BYTES:
        raise WorkAnalysisError("source_changed", 409)
    try:
        texts, metadata = _fetch_texts(conn, ids)
    except WorkAnalysisError:
        raise WorkAnalysisError("source_changed", 409) from None
    for row in manifest["chapters"]:
        current = metadata.get(row["novel_id"])
        if current is None or current[:3] != (row["text_hash"], row["chars"], row["utf8_bytes"]):
            raise WorkAnalysisError("source_changed", 409)
    return texts
