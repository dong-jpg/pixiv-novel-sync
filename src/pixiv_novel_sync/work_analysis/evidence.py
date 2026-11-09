from __future__ import annotations

from collections.abc import Mapping

from .artifacts import (
    CERTAINTIES, DIMENSIONS, MAX_SOURCE_BYTES, claim_identity, evidence_identity,
    parse_model_json, validate_artifact,
)
from .contracts import WorkAnalysisError, hex_id, strict_int, strict_object, strict_text


def _unit_shape(unit):
    if not isinstance(unit, Mapping):
        raise WorkAnalysisError("artifact_invalid")
    unit_id = hex_id(unit.get("unit_id"))
    kind = unit.get("kind")
    is_final = unit.get("is_final")
    if kind not in {"whole", "extract", "reduce"} or type(is_final) is not bool:
        raise WorkAnalysisError("artifact_invalid")
    values = unit.get("child_unit_ids")
    if not isinstance(values, list) or len(values) > 16:
        raise WorkAnalysisError("artifact_invalid")
    values = [hex_id(item) for item in values]
    if len(set(values)) != len(values):
        raise WorkAnalysisError("artifact_invalid")
    refs = unit.get("claim_refs")
    if not isinstance(refs, list):
        raise WorkAnalysisError("artifact_invalid")
    if kind == "reduce":
        if not 1 <= len(values) <= 16:
            raise WorkAnalysisError("artifact_invalid")
        if unit.get("primary_ranges") not in (None, []) or unit.get("context_ranges") not in (None, []):
            raise WorkAnalysisError("artifact_invalid")
    else:
        if (kind == "whole" and not is_final) or (kind == "extract" and is_final):
            raise WorkAnalysisError("artifact_invalid")
        if values or refs:
            raise WorkAnalysisError("artifact_invalid")
    return unit_id, kind, is_final, values


def _child_unit_ids(unit):
    return _unit_shape(unit)[3]


def selected_child_claims(unit, children):
    if not isinstance(children, Mapping):
        raise WorkAnalysisError("artifact_invalid")
    _unit_id, kind, _is_final, child_ids = _unit_shape(unit)
    if kind != "reduce":
        raise WorkAnalysisError("artifact_invalid")
    if set(children) != set(child_ids):
        raise WorkAnalysisError("artifact_invalid")
    available = {}
    for child_id in child_ids:
        raw = children[child_id]
        if (not isinstance(raw, Mapping) or raw.get("is_final") is not False):
            raise WorkAnalysisError("artifact_invalid")
        child = validate_artifact(raw, final=False)
        if child["unit_id"] != child_id:
            raise WorkAnalysisError("artifact_invalid")
        for claim in child["claims"]:
            available[(child_id, claim["claim_id"])] = claim
    refs = unit.get("claim_refs")
    if not isinstance(refs, list):
        raise WorkAnalysisError("artifact_invalid")
    result = {}
    for ref in refs:
        ref = strict_object(ref, {"unit_id", "claim_id"})
        key = (hex_id(ref["unit_id"]), hex_id(ref["claim_id"]))
        if key not in available or key in result:
            raise WorkAnalysisError("artifact_invalid")
        result[key] = available[key]
    return result


def _allowed_child_evidence(unit, selected, children):
    result = {}
    for (child_id, _claim_id), claim in selected.items():
        source = validate_artifact(children[child_id], final=children[child_id]["is_final"])
        for evidence_id in claim["evidence_ids"]:
            result[evidence_id] = source["evidence"][evidence_id]
    for child_id in _child_unit_ids(unit):
        source = validate_artifact(children[child_id], final=children[child_id]["is_final"])
        for counter in source["counterexamples"]:
            for evidence_id in counter["evidence_ids"]:
                row = source["evidence"][evidence_id]
                if evidence_id in result and result[evidence_id] != row:
                    raise WorkAnalysisError("artifact_invalid")
                result[evidence_id] = row
    return result


def allowed_child_evidence(unit, children):
    selected = selected_child_claims(unit, children)
    return _allowed_child_evidence(unit, selected, children)


def _resolve_quote(value, blocks):
    value = strict_object(value, {"block_id", "quote"})
    block_id = hex_id(value["block_id"])
    quote = strict_text(value["quote"], 240)
    block = blocks.get(block_id)
    if not isinstance(block, Mapping):
        raise WorkAnalysisError("artifact_invalid")
    block = strict_object(block, {"novel_id", "text_hash", "start", "end", "text"})
    strict_int(block["novel_id"], 1, 2**63 - 1)
    hex_id(block["text_hash"])
    block_start = strict_int(block["start"], 0, MAX_SOURCE_BYTES - 1)
    block_end = strict_int(block["end"], block_start + 1, MAX_SOURCE_BYTES)
    text = block["text"]
    if not isinstance(text, str) or len(text) != block_end - block_start:
        raise WorkAnalysisError("artifact_invalid")
    offset = text.find(quote)
    if offset < 0 or text.find(quote, offset + 1) >= 0:
        raise WorkAnalysisError("artifact_invalid")
    start = block_start + offset
    if start < block_start or start + len(quote) > block_end:
        raise WorkAnalysisError("artifact_invalid")
    row = {
        "novel_id": block["novel_id"], "text_hash": block["text_hash"],
        "block_id": block_id, "start": start, "end": start + len(quote),
        "quote": quote,
    }
    row["evidence_id"] = evidence_identity(row)
    return row


def validate_unit_output(raw, unit, block_map, child_artifacts):
    unit_id, kind, is_final, child_ids = _unit_shape(unit)
    data = strict_object(parse_model_json(raw), {
        "unit_id", "summary", "no_findings_reason",
        "claims", "counterexamples", "uncertainties",
    })
    if data["unit_id"] != unit_id:
        raise WorkAnalysisError("artifact_invalid")
    reducing = kind == "reduce"
    if reducing and block_map:
        raise WorkAnalysisError("artifact_invalid")
    if not reducing and child_artifacts:
        raise WorkAnalysisError("artifact_invalid")
    selected = selected_child_claims(unit, child_artifacts) if reducing else {}
    permitted = _allowed_child_evidence(unit, selected, child_artifacts) if reducing else {}
    claim_sources = {claim_id: value for _child_id, claim_id in selected for value in [selected[(_child_id, claim_id)]]}
    evidence, claims, counters = {}, [], []

    def references(row, *, claim):
        if reducing:
            ids = row["evidence_ids"]
            if not isinstance(ids, list) or not 1 <= len(ids) <= 4:
                raise WorkAnalysisError("artifact_invalid")
            ids = [hex_id(item) for item in ids]
            if len(set(ids)) != len(ids):
                raise WorkAnalysisError("artifact_invalid")
            if claim:
                sources = row["source_claim_ids"]
                if not isinstance(sources, list) or not 1 <= len(sources) <= 16:
                    raise WorkAnalysisError("artifact_invalid")
                sources = [hex_id(item) for item in sources]
                if len(set(sources)) != len(sources):
                    raise WorkAnalysisError("artifact_invalid")
                valid = set()
                for source in sources:
                    if source not in claim_sources:
                        raise WorkAnalysisError("artifact_invalid")
                    valid.update(claim_sources[source]["evidence_ids"])
                if not set(ids) <= valid:
                    raise WorkAnalysisError("artifact_invalid")
            for evidence_id in ids:
                if evidence_id not in permitted:
                    raise WorkAnalysisError("artifact_invalid")
                evidence[evidence_id] = permitted[evidence_id]
            return ids
        quotes = row["quotes"]
        if not isinstance(quotes, list) or not 1 <= len(quotes) <= 4:
            raise WorkAnalysisError("artifact_invalid")
        ids = []
        for quote in quotes:
            value = _resolve_quote(quote, block_map)
            evidence[value["evidence_id"]] = value
            if value["evidence_id"] in ids:
                raise WorkAnalysisError("artifact_invalid")
            ids.append(value["evidence_id"])
        return ids

    if not isinstance(data["claims"], list) or len(data["claims"]) > (128 if is_final else 32):
        raise WorkAnalysisError("artifact_invalid")
    for raw_claim in data["claims"]:
        fields = {"dimension", "certainty", "label", "statement"}
        fields |= {"evidence_ids", "source_claim_ids"} if reducing else {"quotes"}
        raw_claim = strict_object(raw_claim, fields)
        if (not isinstance(raw_claim["dimension"], str) or raw_claim["dimension"] not in DIMENSIONS
                or not isinstance(raw_claim["certainty"], str) or raw_claim["certainty"] not in CERTAINTIES):
            raise WorkAnalysisError("artifact_invalid")
        value = {key: raw_claim[key] for key in ("dimension", "certainty", "label", "statement")}
        strict_text(value["label"], 80)
        strict_text(value["statement"], 600)
        value["evidence_ids"] = references(raw_claim, claim=True)
        value["claim_id"] = claim_identity(unit_id, value)
        claims.append(value)
    if not isinstance(data["counterexamples"], list) or len(data["counterexamples"]) > 32:
        raise WorkAnalysisError("artifact_invalid")
    for raw_counter in data["counterexamples"]:
        raw_counter = strict_object(
            raw_counter, {"statement", "evidence_ids"} if reducing else {"statement", "quotes"},
        )
        counters.append({
            "statement": raw_counter["statement"],
            "evidence_ids": references(raw_counter, claim=False),
        })
    return validate_artifact({
        "schema_version": 1, "unit_id": unit_id, "is_final": is_final,
        "summary": data["summary"], "no_findings_reason": data["no_findings_reason"],
        "input_unit_ids": child_ids, "claims": claims,
        "evidence": evidence, "counterexamples": counters,
        "uncertainties": data["uncertainties"],
    }, final=is_final)
