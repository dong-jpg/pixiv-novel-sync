import copy
import hashlib
import json

import pytest

from pixiv_novel_sync.work_analysis.artifacts import parse_model_json, validate_artifact
from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError, fingerprint
from pixiv_novel_sync.work_analysis.evidence import validate_unit_output


def raw_result(unit_id, block_id, quote):
    return {
        "unit_id": unit_id, "summary": "An evidence-backed synthetic interpretation.",
        "no_findings_reason": None,
        "claims": [{
            "dimension": "theme", "certainty": "interpretation", "label": "contrast",
            "statement": "The quoted passage contains a contrast.",
            "quotes": [{"block_id": block_id, "quote": quote}],
        }],
        "counterexamples": [],
        "uncertainties": ["This is interpretation, not a measured probability."],
    }


def blocks_for(text, block_id="b" * 64, *, start=0, novel_id=1):
    return {
        block_id: {
            "novel_id": novel_id, "text_hash": hashlib.sha256(text.encode()).hexdigest(),
            "start": start, "end": start + len(text), "text": text,
        },
    }


def test_unicode_quote_offsets_are_server_resolved_and_private_output_is_bounded():
    text = "\U0001f600 before \u4e2d after"
    block_id = fingerprint({"block": 1})
    unit = {
        "unit_id": "a" * 64, "kind": "extract", "is_final": False,
        "child_unit_ids": [], "claim_refs": [],
    }
    blocks = blocks_for(text, block_id, start=100, novel_id=7)
    result = validate_unit_output(json.dumps(raw_result(unit["unit_id"], block_id, "\u4e2d")), unit, blocks, {})
    evidence = next(iter(result["evidence"].values()))
    assert evidence["start"] == 109 and evidence["end"] == 110
    assert evidence["quote"] == "\u4e2d"
    assert validate_artifact(result, final=False) == result
    assert "text" not in evidence and "raw_output" not in result
    broken = copy.deepcopy(result)
    broken["claims"][0]["evidence_ids"] = ["f" * 64]
    with pytest.raises(WorkAnalysisError):
        validate_artifact(broken, final=False)


@pytest.mark.parametrize("quote", ["same", "absent", "x" * 241])
def test_missing_ambiguous_and_excessive_quotes_are_not_repaired(quote):
    block_id = "b" * 64
    text = "same same"
    unit = {
        "unit_id": "a" * 64, "kind": "whole", "is_final": True,
        "child_unit_ids": [], "claim_refs": [],
    }
    blocks = {
        block_id: {
            "novel_id": 1, "text_hash": "c" * 64, "start": 0,
            "end": len(text), "text": text,
        },
    }
    with pytest.raises(WorkAnalysisError):
        validate_unit_output(json.dumps(raw_result(unit["unit_id"], block_id, quote)), unit, blocks, {})


def test_duplicate_json_unknown_fields_empty_claims_and_nonfinite_values_are_checked():
    unit = {
        "unit_id": "a" * 64, "kind": "extract", "is_final": False,
        "child_unit_ids": [], "claim_refs": [],
    }
    clean = {
        "unit_id": unit["unit_id"], "summary": "No supported finding.",
        "no_findings_reason": "Insufficient evidence.",
        "claims": [], "counterexamples": [], "uncertainties": [],
    }
    assert validate_unit_output(json.dumps(clean), unit, {}, {})["claims"] == []
    for raw in (
        '{"unit_id":"a","unit_id":"b"}',
        json.dumps({**clean, "raw_text": "forbidden"}),
        json.dumps({**clean, "no_findings_reason": None}),
        json.dumps({**clean, "summary": float("nan")}),
    ):
        with pytest.raises(WorkAnalysisError):
            validate_unit_output(raw, unit, {}, {})


@pytest.mark.parametrize("field,value", [
    ("dimension", []), ("certainty", {}), ("label", "\ud800"), ("statement", "\ud800"),
])
def test_model_fields_reject_invalid_json_values_with_domain_errors(field, value):
    unit = {
        "unit_id": "a" * 64, "kind": "extract", "is_final": False,
        "child_unit_ids": [], "claim_refs": [],
    }
    text, block_id = "synthetic", "b" * 64
    raw = raw_result(unit["unit_id"], block_id, text)
    raw["claims"][0][field] = value
    with pytest.raises(WorkAnalysisError):
        validate_unit_output(json.dumps(raw), unit, blocks_for(text, block_id), {})


@pytest.mark.parametrize("field", ["evidence_ids", "source_claim_ids"])
def test_reducer_id_lists_reject_nested_values_with_domain_errors(field):
    child_unit = {
        "unit_id": "a" * 64, "kind": "extract", "is_final": False,
        "child_unit_ids": [], "claim_refs": [],
    }
    block_id, text = "b" * 64, "synthetic quotation"
    child = validate_unit_output(
        json.dumps(raw_result(child_unit["unit_id"], block_id, text)),
        child_unit, blocks_for(text, block_id), {},
    )
    claim = child["claims"][0]
    unit = {
        "unit_id": "c" * 64, "kind": "reduce", "is_final": True,
        "child_unit_ids": [child_unit["unit_id"]],
        "claim_refs": [{"unit_id": child_unit["unit_id"], "claim_id": claim["claim_id"]}],
    }
    raw = {
        "unit_id": unit["unit_id"], "summary": "Synthetic synthesis.",
        "no_findings_reason": None,
        "claims": [{
            "dimension": "theme", "certainty": "direct", "label": "synthetic",
            "statement": "An evidence-backed claim.",
            "evidence_ids": list(claim["evidence_ids"]),
            "source_claim_ids": [claim["claim_id"]],
        }],
        "counterexamples": [], "uncertainties": [],
    }
    raw["claims"][0][field] = [{}]
    with pytest.raises(WorkAnalysisError):
        validate_unit_output(json.dumps(raw), unit, {}, {child_unit["unit_id"]: child})


@pytest.mark.parametrize("field", ["input_unit_ids", "evidence_ids"])
def test_artifact_id_lists_reject_nested_values_with_domain_errors(field):
    unit = {
        "unit_id": "a" * 64, "kind": "extract", "is_final": False,
        "child_unit_ids": [], "claim_refs": [],
    }
    block_id, text = "b" * 64, "synthetic quotation"
    artifact = validate_unit_output(
        json.dumps(raw_result(unit["unit_id"], block_id, text)),
        unit, blocks_for(text, block_id), {},
    )
    if field == "input_unit_ids":
        artifact[field] = [[]]
    else:
        artifact["claims"][0][field] = [[]]
    with pytest.raises(WorkAnalysisError):
        validate_artifact(artifact, final=False)


def test_artifact_evidence_rejects_invalid_unicode_before_computing_identity():
    unit = {
        "unit_id": "a" * 64, "kind": "extract", "is_final": False,
        "child_unit_ids": [], "claim_refs": [],
    }
    block_id, text = "b" * 64, "synthetic quotation"
    artifact = validate_unit_output(
        json.dumps(raw_result(unit["unit_id"], block_id, text)),
        unit, blocks_for(text, block_id), {},
    )
    next(iter(artifact["evidence"].values()))["quote"] = "\ud800"
    with pytest.raises(WorkAnalysisError):
        validate_artifact(artifact, final=False)


def test_raw_output_byte_limit_keeps_its_specific_error_and_status():
    with pytest.raises(WorkAnalysisError) as caught:
        parse_model_json("x" * (1024 * 1024 + 1))
    assert caught.value.code == "artifact_too_large"
    assert caught.value.status == 413


def test_evidence_identity_binds_block_location_for_identical_quotes():
    unit = {
        "unit_id": "a" * 64, "kind": "extract", "is_final": False,
        "child_unit_ids": [], "claim_refs": [],
    }
    first_id, second_id, text = "b" * 64, "c" * 64, "same"
    blocks = {
        **blocks_for(text, first_id, start=0),
        **blocks_for(text, second_id, start=100, novel_id=2),
    }
    raw = raw_result(unit["unit_id"], first_id, text)
    raw["claims"][0]["quotes"].append({"block_id": second_id, "quote": text})
    result = validate_unit_output(json.dumps(raw), unit, blocks, {})
    assert len(result["evidence"]) == 2
    assert {row["block_id"] for row in result["evidence"].values()} == {first_id, second_id}


def test_reducer_cannot_borrow_evidence_from_unselected_child_claim():
    child_unit = {
        "unit_id": "a" * 64, "kind": "extract", "is_final": False,
        "child_unit_ids": [], "claim_refs": [],
    }
    block_id, text = "b" * 64, "alpha synthetic\nbeta synthetic"
    child_raw = raw_result(child_unit["unit_id"], block_id, "alpha synthetic")
    child_raw["claims"].append({
        "dimension": "tone", "certainty": "interpretation", "label": "second",
        "statement": "A second synthetic claim.",
        "quotes": [{"block_id": block_id, "quote": "beta synthetic"}],
    })
    child = validate_unit_output(
        json.dumps(child_raw), child_unit, blocks_for(text, block_id), {},
    )
    selected, unselected = child["claims"]
    unit = {
        "unit_id": "c" * 64, "kind": "reduce", "is_final": True,
        "child_unit_ids": [child_unit["unit_id"]],
        "claim_refs": [{"unit_id": child_unit["unit_id"], "claim_id": selected["claim_id"]}],
    }
    invalid = {
        "unit_id": unit["unit_id"], "summary": "Synthetic synthesis.",
        "no_findings_reason": None,
        "claims": [{
            "dimension": "theme", "certainty": "direct", "label": "borrowed",
            "statement": "Borrowed evidence must fail.",
            "source_claim_ids": [selected["claim_id"]],
            "evidence_ids": list(unselected["evidence_ids"]),
        }],
        "counterexamples": [], "uncertainties": [],
    }
    with pytest.raises(WorkAnalysisError):
        validate_unit_output(
            json.dumps(invalid), unit, {}, {child_unit["unit_id"]: child},
        )


def test_reducer_cannot_reference_unselected_claim_or_invent_evidence():
    from pixiv_novel_sync.work_analysis.planning import (
        materialize_unit, plan_original_units, plan_reduction_level,
    )
    from work_analysis_domain_helpers import capacity_preflight, source_fixture

    manifest, texts = source_fixture(["Alpha text\n" * 400, "Beta text\n" * 400])
    plan = plan_original_units(
        manifest, texts, capacity_preflight(3600),
        agent_prompt="", output_tokens=128,
    )
    children = {}
    for unit in plan["original_units"]:
        payload, blocks = materialize_unit(
            manifest, unit, texts, {}, agent_prompt="", output_tokens=128,
        )
        block_id, block = next(iter(blocks.items()))
        quote = block["text"]
        if len(quote) > 240:
            raw = {
                "unit_id": unit["unit_id"],
                "summary": "No uniquely supported fixture claim.",
                "no_findings_reason": "Repeated synthetic material.",
                "claims": [], "counterexamples": [], "uncertainties": [],
            }
        else:
            raw = raw_result(unit["unit_id"], block_id, quote)
        children[unit["unit_id"]] = validate_unit_output(
            json.dumps(raw), unit, blocks, {},
        )
    root = plan_reduction_level(
        "1" * 32, 1, plan["original_units"], children,
        capacity_preflight(100000), agent_prompt="", output_tokens=128,
    )[0]
    invalid = {
        "unit_id": root["unit_id"], "summary": "Unsupported invented evidence.",
        "no_findings_reason": None,
        "claims": [{
            "dimension": "theme", "certainty": "direct", "label": "invented",
            "statement": "Not supported.",
            "source_claim_ids": ["f" * 64], "evidence_ids": ["e" * 64],
        }],
        "counterexamples": [], "uncertainties": [],
    }
    with pytest.raises(WorkAnalysisError):
        validate_unit_output(json.dumps(invalid), root, {}, children)


@pytest.mark.parametrize("unit", [
    {
        "unit_id": "a" * 64, "kind": "reduce", "is_final": True,
        "child_unit_ids": [], "claim_refs": [],
    },
    {
        "unit_id": "a" * 64, "kind": "extract", "is_final": False,
        "child_unit_ids": ["b" * 64], "claim_refs": [],
    },
    {
        "unit_id": "a" * 64, "kind": "whole", "is_final": False,
        "child_unit_ids": [], "claim_refs": [],
    },
])
def test_unit_output_rejects_malformed_unit_shapes(unit):
    raw = {
        "unit_id": unit["unit_id"], "summary": "No supported finding.",
        "no_findings_reason": "Insufficient evidence.",
        "claims": [], "counterexamples": [], "uncertainties": [],
    }
    with pytest.raises(WorkAnalysisError):
        validate_unit_output(json.dumps(raw), unit, {}, {})


def test_reducer_rejects_final_child_artifact():
    child_unit = {
        "unit_id": "a" * 64, "kind": "whole", "is_final": True,
        "child_unit_ids": [], "claim_refs": [],
    }
    child_raw = {
        "unit_id": child_unit["unit_id"], "summary": "No supported finding.",
        "no_findings_reason": "Insufficient evidence.",
        "claims": [], "counterexamples": [], "uncertainties": [],
    }
    child = validate_unit_output(json.dumps(child_raw), child_unit, {}, {})
    parent = {
        "unit_id": "c" * 64, "kind": "reduce", "is_final": True,
        "child_unit_ids": [child_unit["unit_id"]], "claim_refs": [],
    }
    parent_raw = {**child_raw, "unit_id": parent["unit_id"]}
    with pytest.raises(WorkAnalysisError):
        validate_unit_output(
            json.dumps(parent_raw), parent, {}, {child_unit["unit_id"]: child},
        )


def test_quote_resolution_rejects_inconsistent_block_bounds():
    unit = {
        "unit_id": "a" * 64, "kind": "extract", "is_final": False,
        "child_unit_ids": [], "claim_refs": [],
    }
    text, block_id = "synthetic", "b" * 64
    blocks = {
        block_id: {
            "novel_id": 1, "text_hash": hashlib.sha256(text.encode()).hexdigest(),
            "start": 0, "end": len(text) + 1, "text": text,
        },
    }
    with pytest.raises(WorkAnalysisError):
        validate_unit_output(
            json.dumps(raw_result(unit["unit_id"], block_id, text)),
            unit, blocks, {},
        )
