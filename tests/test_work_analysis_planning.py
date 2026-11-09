import copy
import json

import pytest

from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError, fingerprint
from pixiv_novel_sync.work_analysis.evidence import validate_unit_output
from pixiv_novel_sync.work_analysis.planning import (
    materialize_unit, plan_original_units, plan_reduction_level, seal_unit,
    validate_derived_units,
)
from work_analysis_domain_helpers import capacity_preflight, source_fixture


def empty_result(unit):
    return json.dumps({
        "unit_id": unit["unit_id"], "summary": "Insufficient supported findings.",
        "no_findings_reason": "No synthetic content claim is made.",
        "claims": [], "counterexamples": [], "uncertainties": [],
    })


def test_whole_is_a_single_final_root_and_input_has_every_character():
    manifest, texts = source_fixture(["first\U0001f600last", "tiny"])
    plan = plan_original_units(
        manifest, texts, capacity_preflight(100000),
        agent_prompt="", output_tokens=256,
    )
    assert plan["strategy"] == "whole" and plan["minimum_requests"] == 1
    unit = plan["original_units"][0]
    assert unit["is_final"] is True
    payload, blocks = materialize_unit(
        manifest, unit, texts, {}, agent_prompt="", output_tokens=256,
    )
    for novel_id, body in texts.items():
        parts = sorted(
            (row["start"], row["text"])
            for row in blocks.values()
            if row["novel_id"] == novel_id
        )
        assert "".join(part for start, part in parts) == body
    assert "text_raw" not in str(plan) and "first" not in str(plan)
    assert len(payload["messages"]) <= 256


def test_segmentation_covers_unicode_tail_without_changing_failed_unit_granularity():
    manifest, texts = source_fixture([("line\U0001f600\n" * 1000) + "TAIL", "z"])
    plan = plan_original_units(
        manifest, texts, capacity_preflight(3600),
        agent_prompt="saved", output_tokens=128,
    )
    assert plan["strategy"] == "segmented"
    spans = sorted(
        (row["start"], row["end"])
        for unit in plan["original_units"]
        for row in unit["primary_ranges"]
        if row["novel_id"] == 1
    )
    cursor = 0
    for start, end in spans:
        assert start == cursor
        cursor = end
    assert cursor == len(texts[1])
    for unit in plan["original_units"]:
        payload, blocks = materialize_unit(
            manifest, unit, texts, {}, agent_prompt="saved", output_tokens=128,
        )
        assert capacity_preflight(3600)(payload)["can_run"]
        assert all(row["end"] - row["start"] <= 512 for row in unit["context_ranges"])
    assert plan == plan_original_units(
        manifest, texts, capacity_preflight(3600),
        agent_prompt="saved", output_tokens=128,
    )


def test_noncapacity_failure_does_not_split_and_reducer_hash_binds_children():
    manifest, texts = source_fixture(["A" * 3000, "B" * 3000])
    with pytest.raises(WorkAnalysisError, match="configuration_unavailable"):
        plan_original_units(
            manifest, texts,
            lambda payload: {"can_run": False, "work_can_segment": False},
            agent_prompt="", output_tokens=128,
        )
    plan = plan_original_units(
        manifest, texts, capacity_preflight(3600),
        agent_prompt="", output_tokens=128,
    )
    children = {}
    for unit in plan["original_units"]:
        payload, blocks = materialize_unit(
            manifest, unit, texts, {}, agent_prompt="", output_tokens=128,
        )
        children[unit["unit_id"]] = validate_unit_output(empty_result(unit), unit, blocks, {})
    reducers = plan_reduction_level(
        "1" * 32, 1, plan["original_units"], children, capacity_preflight(100000),
        agent_prompt="", output_tokens=128,
    )
    assert len(reducers) == 1 and reducers[0]["is_final"]
    validate_derived_units(reducers, plan["original_units"], children)
    changed = copy.deepcopy(children)
    first = next(iter(changed))
    changed[first]["summary"] = "Changed validated summary."
    with pytest.raises(WorkAnalysisError):
        validate_derived_units(reducers, plan["original_units"], changed)


@pytest.mark.parametrize("final", [False, True])
def test_reducer_seal_ignores_manifest_and_binds_validated_children(final):
    manifest, texts = source_fixture(["A" * 3000, "B" * 3000])
    plan = plan_original_units(
        manifest, texts, capacity_preflight(3600), agent_prompt="", output_tokens=128,
    )
    children = {
        unit["unit_id"]: validate_unit_output(empty_result(unit), unit, {}, {})
        for unit in plan["original_units"]
    }
    root = plan_reduction_level(
        "1" * 32, 1, plan["original_units"], children,
        capacity_preflight(100000), agent_prompt="", output_tokens=128,
    )[0]
    fields = {
        key: value for key, value in root.items()
        if key not in {"unit_id", "input_fingerprint"}
    }
    fields["is_final"] = final
    sealed = seal_unit(fields, child_artifacts=children)
    for manifest_fingerprint in (manifest["manifest_fingerprint"], "f" * 64):
        assert seal_unit(
            fields, manifest_fingerprint=manifest_fingerprint, child_artifacts=children,
        ) == sealed

    changed = copy.deepcopy(children)
    first = next(iter(changed))
    changed[first]["summary"] = "Changed validated child summary."
    resealed = seal_unit(
        fields, manifest_fingerprint=manifest["manifest_fingerprint"],
        child_artifacts=changed,
    )
    assert resealed["input_fingerprint"] != sealed["input_fingerprint"]
    assert resealed["unit_id"] != sealed["unit_id"]
    changed[first]["is_final"] = True
    with pytest.raises(WorkAnalysisError, match="invalid_plan"):
        seal_unit(
            fields, manifest_fingerprint=manifest["manifest_fingerprint"],
            child_artifacts=changed,
        )


def test_single_extract_segmented_plan_counts_its_required_final_reducer():
    manifest, texts = source_fixture(["A" * 3000])
    whole = plan_original_units(
        manifest, texts, capacity_preflight(100000), agent_prompt="", output_tokens=128,
    )["original_units"][0]
    whole_payload, _ = materialize_unit(
        manifest, whole, texts, {}, agent_prompt="", output_tokens=128,
    )
    limit = sum(len(row["content"]) for row in whole_payload["messages"]) - 1
    preflight = capacity_preflight(limit)
    assert not preflight(whole_payload)["can_run"]
    plan = plan_original_units(
        manifest, texts, preflight, agent_prompt="", output_tokens=128,
    )
    assert plan["strategy"] == "segmented"
    assert len(plan["original_units"]) == 1
    extract = plan["original_units"][0]
    assert extract["kind"] == "extract" and extract["is_final"] is False
    payload, blocks = materialize_unit(
        manifest, extract, texts, {}, agent_prompt="", output_tokens=128,
    )
    assert preflight(payload)["can_run"]
    assert "".join(block["text"] for block in blocks.values()) == texts[1]
    children = {
        extract["unit_id"]: validate_unit_output(empty_result(extract), extract, blocks, {}),
    }
    reducers = plan_reduction_level(
        "1" * 32, 1, [extract], children, preflight, agent_prompt="", output_tokens=128,
    )
    assert len(reducers) == 1 and reducers[0]["is_final"]
    validate_derived_units(reducers, [extract], children)
    assert plan["minimum_requests"] == len(plan["original_units"]) + len(reducers) == 2


@pytest.mark.parametrize("fields", [
    {"kind": "whole", "is_final": False, "level": 0},
    {"kind": "extract", "is_final": True, "level": 0},
    {"kind": "reduce", "is_final": False, "level": 0},
])
def test_seal_unit_rejects_invalid_original_and_reducer_finality(fields):
    manifest, texts = source_fixture(["source"])
    chapter = manifest["chapters"][0]
    base = {
        "kind": fields["kind"], "is_final": fields["is_final"], "level": fields["level"],
        "primary_ranges": [{
            "novel_id": 1, "text_hash": chapter["text_hash"],
            "start": 0, "end": chapter["chars"],
        }],
        "context_ranges": [], "child_unit_ids": [], "claim_refs": [],
    }
    with pytest.raises(WorkAnalysisError):
        seal_unit(base, manifest_fingerprint=manifest["manifest_fingerprint"])


def test_materialize_rejects_source_text_that_no_longer_matches_manifest():
    manifest, texts = source_fixture(["unchanged source"])
    plan = plan_original_units(
        manifest, texts, capacity_preflight(100000),
        agent_prompt="", output_tokens=128,
    )
    tampered = dict(texts)
    tampered[1] = texts[1][:-1] + "X"
    with pytest.raises(WorkAnalysisError, match="source_changed"):
        materialize_unit(
            manifest, plan["original_units"][0], tampered, {},
            agent_prompt="", output_tokens=128,
        )


def test_multi_level_reduction_uses_the_immediate_frontier_artifacts():
    manifest, texts = source_fixture([
        f"chapter {index} " + ("synthetic " * 12) for index in range(20)
    ])
    plan = plan_original_units(
        manifest, texts, capacity_preflight(1800),
        agent_prompt="", output_tokens=128,
    )
    assert len(plan["original_units"]) > 16
    children = {}
    for unit in plan["original_units"]:
        payload, blocks = materialize_unit(
            manifest, unit, texts, {}, agent_prompt="", output_tokens=128,
        )
        children[unit["unit_id"]] = validate_unit_output(
            empty_result(unit), unit, blocks, {},
        )
    level_one = plan_reduction_level(
        "1" * 32, 1, plan["original_units"], children,
        capacity_preflight(100000), agent_prompt="", output_tokens=128,
    )
    assert len(level_one) > 1
    level_one_artifacts = {
        unit["unit_id"]: validate_unit_output(
            empty_result(unit), unit, {},
            {key: children[key] for key in unit["child_unit_ids"]},
        )
        for unit in level_one
    }
    level_two = plan_reduction_level(
        "2" * 32, 2, level_one, level_one_artifacts,
        capacity_preflight(100000), agent_prompt="", output_tokens=128,
    )
    assert len(level_two) == 1 and level_two[0]["is_final"]
    validate_derived_units(level_two, level_one, level_one_artifacts)


@pytest.fixture
def claim_partition():
    quotes = [f"Unique clue {index}." for index in range(4)]
    manifest, texts = source_fixture([" ".join(quotes)])
    chapter = manifest["chapters"][0]
    extract = seal_unit({
        "kind": "extract", "is_final": False, "level": 0,
        "primary_ranges": [{
            "novel_id": 1, "text_hash": chapter["text_hash"],
            "start": 0, "end": chapter["chars"],
        }],
        "context_ranges": [], "child_unit_ids": [], "claim_refs": [],
    }, manifest_fingerprint=manifest["manifest_fingerprint"])
    _, blocks = materialize_unit(
        manifest, extract, texts, {}, agent_prompt="", output_tokens=128,
    )
    block_id = next(iter(blocks))
    raw = {
        "unit_id": extract["unit_id"], "summary": "Four supported synthetic findings.",
        "no_findings_reason": None,
        "claims": [{
            "dimension": "theme", "certainty": "direct", "label": f"Clue {index}",
            "statement": f"The source includes unique clue {index}.",
            "quotes": [{"block_id": block_id, "quote": quote}],
        } for index, quote in enumerate(quotes)],
        "counterexamples": [], "uncertainties": [],
    }
    children = {
        extract["unit_id"]: validate_unit_output(json.dumps(raw), extract, blocks, {}),
    }
    root = plan_reduction_level(
        "1" * 32, 1, [extract], children, capacity_preflight(100000),
        agent_prompt="", output_tokens=128,
    )[0]
    payload, _ = materialize_unit(
        manifest, root, {}, children, agent_prompt="", output_tokens=128,
    )
    # The final instructions are one character longer than intermediate ones.
    limit = sum(len(row["content"]) for row in payload["messages"]) - 2
    preflight = capacity_preflight(limit)
    reducers = plan_reduction_level(
        "1" * 32, 1, [extract], children, preflight, agent_prompt="", output_tokens=128,
    )
    assert len(reducers) > 1
    return manifest, extract, children, reducers, preflight


def test_claim_bearing_child_partitions_fit_and_reach_a_final_root(claim_partition):
    manifest, extract, children, reducers, preflight = claim_partition
    validate_derived_units(reducers, [extract], children)
    expected_refs = {
        (extract["unit_id"], claim["claim_id"])
        for claim in children[extract["unit_id"]]["claims"]
    }
    actual_refs = [
        (ref["unit_id"], ref["claim_id"])
        for unit in reducers for ref in unit["claim_refs"]
    ]
    assert len(actual_refs) == len(set(actual_refs)) == len(expected_refs)
    assert set(actual_refs) == expected_refs
    intermediate_artifacts = {}
    for unit in reducers:
        assert unit["child_unit_ids"] == [extract["unit_id"]]
        assert unit["is_final"] is False
        payload, blocks = materialize_unit(
            manifest, unit, {}, children, agent_prompt="", output_tokens=128,
        )
        assert preflight(payload)["can_run"] and not blocks
        projected = json.loads(payload["messages"][-1]["content"])["input_artifacts"][0]
        assert {claim["claim_id"] for claim in projected["claims"]} == {
            ref["claim_id"] for ref in unit["claim_refs"]
        }
        raw = {
            "unit_id": unit["unit_id"], "summary": "Supported partition findings.",
            "no_findings_reason": None,
            "claims": [{
                **{key: value for key, value in claim.items() if key != "claim_id"},
                "source_claim_ids": [claim["claim_id"]],
            } for claim in projected["claims"]],
            "counterexamples": [], "uncertainties": [],
        }
        intermediate_artifacts[unit["unit_id"]] = validate_unit_output(
            json.dumps(raw), unit, blocks, children,
        )
    roots = plan_reduction_level(
        "1" * 32, 2, reducers, intermediate_artifacts, capacity_preflight(100000),
        agent_prompt="", output_tokens=128,
    )
    assert len(roots) == 1 and roots[0]["is_final"]
    assert len(roots[0]["claim_refs"]) == len(expected_refs)
    validate_derived_units(roots, reducers, intermediate_artifacts)
    assert {
        evidence_id for artifact in intermediate_artifacts.values()
        for evidence_id in artifact["evidence"]
    } == set(children[extract["unit_id"]]["evidence"])


@pytest.mark.parametrize("mutation", ["omitted", "duplicate"])
def test_claim_partition_rejects_missing_or_duplicate_cross_group_refs(claim_partition, mutation):
    _, extract, children, reducers, _ = claim_partition
    changed = copy.deepcopy(reducers)
    fields = {
        key: value for key, value in changed[0].items()
        if key not in {"unit_id", "input_fingerprint"}
    }
    if mutation == "omitted":
        fields["claim_refs"].pop()
    else:
        fields["claim_refs"].append(copy.deepcopy(changed[1]["claim_refs"][0]))
    # Reseal so rejection tests cross-group accounting, not stale unit identity.
    changed[0] = seal_unit(fields, child_artifacts=children)
    assert all(unit["child_unit_ids"] == [extract["unit_id"]] for unit in changed)
    with pytest.raises(WorkAnalysisError, match="invalid_plan"):
        validate_derived_units(changed, [extract], children)


def test_reduction_rejects_missing_child_artifact_as_domain_error():
    input_fingerprint = "c" * 64
    unit = {
        "kind": "reduce", "is_final": False, "level": 1,
        "primary_ranges": [], "context_ranges": [],
        "child_unit_ids": ["b" * 64], "claim_refs": [],
        "input_fingerprint": input_fingerprint,
        "unit_id": fingerprint({
            "kind": "work-unit-v1", "input_fingerprint": input_fingerprint,
        }),
    }
    with pytest.raises(WorkAnalysisError):
        plan_reduction_level(
            "1" * 32, 2, [unit], {}, capacity_preflight(100000),
            agent_prompt="", output_tokens=128,
        )


def test_reduction_rejects_tampered_original_unit_shape():
    manifest, texts = source_fixture(["source"])
    plan = plan_original_units(
        manifest, texts, capacity_preflight(100000),
        agent_prompt="", output_tokens=128,
    )
    unit = copy.deepcopy(plan["original_units"][0])
    unit["is_final"] = False
    artifact = {
        "schema_version": 1, "unit_id": unit["unit_id"], "is_final": False,
        "summary": "No supported finding.", "no_findings_reason": "Insufficient evidence.",
        "input_unit_ids": [], "claims": [], "evidence": {},
        "counterexamples": [], "uncertainties": [],
    }
    with pytest.raises(WorkAnalysisError):
        plan_reduction_level(
            "1" * 32, 1, [unit], {unit["unit_id"]: artifact},
            capacity_preflight(100000), agent_prompt="", output_tokens=128,
        )
