import pytest

from pixiv_novel_sync.work_analysis.contracts import WorkAnalysisError
from pixiv_novel_sync.work_analysis.coverage import coverage
from work_analysis_domain_helpers import source_fixture


def test_overlap_is_counted_once_failed_input_and_reduction_are_not_coverage():
    manifest, texts = source_fixture(["0123456789", " "])
    source = manifest["chapters"][0]

    def unit(start, end, *, status="succeeded", kind="extract"):
        return {
            "status": status, "kind": kind,
            "primary_ranges": [{
                "novel_id": 1, "text_hash": source["text_hash"],
                "start": start, "end": end,
            }],
        }

    partial = coverage(
        manifest,
        [
            unit(0, 6), unit(4, 8), unit(8, 10, status="failed"),
            unit(8, 10, kind="reduce"),
        ],
    )
    assert partial["validated_chars"] == 8 and partial["available_chars"] == 10
    assert partial["ratio"] == 0.8 and partial["complete"] is False
    full = coverage(manifest, [unit(0, 6), unit(4, 10)])
    assert full["validated_chars"] == 10 and full["ratio"] == 1 and full["complete"] is True
    assert full["source_completeness"]["status"] == "partial"
    assert full["missing_novel_ids"] == [2]


def test_unknown_published_completeness_is_not_model_understanding():
    manifest, texts = source_fixture(["body"])
    result = coverage(manifest, [])
    assert result["source_completeness"]["status"] == "unverified"
    assert "confidence" not in result and "synthesis_complete" not in result
    blank, _ = source_fixture([" "])
    assert coverage(blank, [])["ratio"] == 0
    assert coverage(blank, [])["complete"] is False


def test_coverage_rejects_bool_source_id():
    manifest, _ = source_fixture(["body"])
    source = manifest["chapters"][0]
    unit = {
        "status": "succeeded", "kind": "extract",
        "primary_ranges": [{
            "novel_id": True, "text_hash": source["text_hash"],
            "start": 0, "end": source["chars"],
        }],
    }
    with pytest.raises(WorkAnalysisError):
        coverage(manifest, [unit])
