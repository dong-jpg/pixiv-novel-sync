from __future__ import annotations

import json

from .contracts import (
    WorkAnalysisError, canonical_bytes, fingerprint, hex_id,
    strict_int, strict_object, strict_text,
)


DIMENSIONS = frozenset({"theme", "relationship", "situation", "tone", "pace", "narrative", "xp"})
CERTAINTIES = frozenset({"direct", "interpretation", "insufficient"})
ARTIFACT_KEYS = {
    "schema_version", "unit_id", "is_final", "summary", "no_findings_reason",
    "input_unit_ids", "claims", "evidence", "counterexamples", "uncertainties",
}
EVIDENCE_KEYS = {"evidence_id", "novel_id", "text_hash", "block_id", "start", "end", "quote"}
CLAIM_KEYS = {"claim_id", "dimension", "certainty", "label", "statement", "evidence_ids"}
MAX_SOURCE_BYTES = 64 * 1024 * 1024


def evidence_identity(value):
    return fingerprint({
        key: value[key]
        for key in ("novel_id", "text_hash", "block_id", "start", "end", "quote")
    })


def claim_identity(unit_id, value):
    return fingerprint({
        "unit_id": unit_id,
        **{key: value[key] for key in CLAIM_KEYS - {"claim_id"}},
    })


def _list(value, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise WorkAnalysisError("artifact_invalid")
    return value


def checked_evidence_ids(value, evidence):
    values = [hex_id(item) for item in _list(value, 4)]
    if not values or len(set(values)) != len(values):
        raise WorkAnalysisError("artifact_invalid")
    for item in values:
        if item not in evidence:
            raise WorkAnalysisError("artifact_invalid")
    return values


def _validated_evidence(value):
    evidence = {}
    if not isinstance(value, dict) or len(value) > 640:
        raise WorkAnalysisError("artifact_invalid")
    for evidence_id, row in value.items():
        row = strict_object(row, EVIDENCE_KEYS)
        if hex_id(evidence_id) != hex_id(row["evidence_id"]):
            raise WorkAnalysisError("artifact_invalid")
        strict_int(row["novel_id"], 1, 2**63 - 1)
        hex_id(row["text_hash"])
        hex_id(row["block_id"])
        strict_int(row["start"], 0, MAX_SOURCE_BYTES)
        strict_int(row["end"], row["start"] + 1, MAX_SOURCE_BYTES)
        strict_text(row["quote"], 240)
        if row["end"] - row["start"] != len(row["quote"]):
            raise WorkAnalysisError("artifact_invalid")
        if evidence_identity(row) != evidence_id:
            raise WorkAnalysisError("artifact_invalid")
        evidence[evidence_id] = row
    return evidence


def validate_artifact(value, *, final):
    data = strict_object(value, ARTIFACT_KEYS)
    if (type(final) is not bool or data["is_final"] is not final
            or type(data["schema_version"]) is not int or data["schema_version"] != 1):
        raise WorkAnalysisError("artifact_invalid")
    unit_id = hex_id(data["unit_id"])
    strict_text(data["summary"], 4000 if final else 2000)
    if data["no_findings_reason"] is not None:
        strict_text(data["no_findings_reason"], 600)
    children = [hex_id(item) for item in _list(data["input_unit_ids"], 16)]
    if len(set(children)) != len(children):
        raise WorkAnalysisError("artifact_invalid")
    evidence = _validated_evidence(data["evidence"])
    claims = _list(data["claims"], 128 if final else 32)
    if not claims and data["no_findings_reason"] is None:
        raise WorkAnalysisError("artifact_invalid")
    ids, used = set(), set()
    for row in claims:
        row = strict_object(row, CLAIM_KEYS)
        if (not isinstance(row["dimension"], str) or row["dimension"] not in DIMENSIONS
                or not isinstance(row["certainty"], str) or row["certainty"] not in CERTAINTIES):
            raise WorkAnalysisError("artifact_invalid")
        strict_text(row["label"], 80)
        strict_text(row["statement"], 600)
        evidence_ids = checked_evidence_ids(row["evidence_ids"], evidence)
        claim_id = hex_id(row["claim_id"])
        if claim_id in ids or claim_id != claim_identity(unit_id, row):
            raise WorkAnalysisError("artifact_invalid")
        ids.add(claim_id)
        used.update(evidence_ids)
    for row in _list(data["counterexamples"], 32):
        row = strict_object(row, {"statement", "evidence_ids"})
        strict_text(row["statement"], 600)
        used.update(checked_evidence_ids(row["evidence_ids"], evidence))
    for item in _list(data["uncertainties"], 32):
        strict_text(item, 600)
    if set(evidence) != used:
        raise WorkAnalysisError("artifact_invalid")
    try:
        encoded = canonical_bytes(data)
    except (ValueError, UnicodeError, TypeError):
        raise WorkAnalysisError("artifact_invalid") from None
    if len(encoded) > 128 * 1024:
        raise WorkAnalysisError("artifact_too_large", 413)
    return json.loads(encoded)


def parse_model_json(raw):
    if not isinstance(raw, str):
        raise WorkAnalysisError("artifact_invalid")
    try:
        if len(raw.encode("utf-8")) > 1024 * 1024:
            raise WorkAnalysisError("artifact_too_large", 413)

        def pairs(values):
            result = {}
            for key, value in values:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result

        def reject_constant(value):
            raise ValueError("nonfinite JSON")

        return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)
    except WorkAnalysisError:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise WorkAnalysisError("artifact_invalid") from None
