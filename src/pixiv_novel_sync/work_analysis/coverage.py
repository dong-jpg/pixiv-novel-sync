from __future__ import annotations

from collections.abc import Mapping

from .contracts import WorkAnalysisError, strict_int
from .manifest import validate_manifest


def coverage(manifest, accepted_units):
    manifest = validate_manifest(manifest)
    if not isinstance(accepted_units, (list, tuple)):
        raise WorkAnalysisError("invalid_plan")
    chapters = {row["novel_id"]: row for row in manifest["chapters"]}
    ranges = {key: [] for key, row in chapters.items() if row["has_text"]}
    for unit in accepted_units:
        if not isinstance(unit, Mapping):
            raise WorkAnalysisError("invalid_plan")
        if unit.get("status") != "succeeded" or unit.get("kind") not in {"whole", "extract"}:
            continue
        primary_ranges = unit.get("primary_ranges")
        if not isinstance(primary_ranges, list):
            raise WorkAnalysisError("invalid_plan")
        for row in primary_ranges:
            if not isinstance(row, Mapping):
                raise WorkAnalysisError("invalid_plan")
            novel_id = strict_int(row.get("novel_id"), 1, 2**63 - 1)
            chapter = chapters.get(novel_id)
            if (chapter is None or not chapter["has_text"]
                    or row.get("text_hash") != chapter["text_hash"]):
                raise WorkAnalysisError("invalid_plan")
            start = strict_int(row.get("start"), 0, chapter["chars"] - 1)
            end = strict_int(row.get("end"), start + 1, chapter["chars"])
            ranges[row["novel_id"]].append((start, end))
    validated = 0
    for values in ranges.values():
        until = 0
        for start, end in sorted(values):
            validated += max(0, end - max(start, until))
            until = max(until, end)
    available = sum(row["chars"] for row in chapters.values() if row["has_text"])
    missing = [key for key, row in chapters.items() if not row["has_text"]]
    expected = manifest["expected_count"]
    shortage = None if expected is None else max(0, expected - len(chapters))
    return {
        "validated_chars": validated, "available_chars": available,
        "ratio": validated / available if available else 0,
        "complete": bool(available and validated == available),
        "missing_novel_ids": missing,
        "source_completeness": {
            "status": "partial" if missing or shortage else "unverified",
            "expected_count": expected, "missing_count": shortage,
            "local_chapters": len(chapters), "available_chapters": len(ranges),
            "publication_state": manifest["publication_state"],
            "publication_source": manifest["publication_source"],
            "order_source": manifest["order_source"],
        },
    }
