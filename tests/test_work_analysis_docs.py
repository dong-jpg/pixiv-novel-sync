from pathlib import Path

import pytest
from flask import Flask

from pixiv_novel_sync.work_analysis.service import CONFIRMATIONS, PREVIEW_FIELDS
from pixiv_novel_sync.work_analysis_web import BASE, register_work_analysis_routes


ROOT = Path(__file__).parents[1]


def read_doc(path):
    return (ROOT / path).read_text(encoding="utf-8")


def work_api_section():
    text = read_doc("docs/frontend-api-contract.md")
    heading = "## Work Analysis APIs"
    assert heading in text
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


@pytest.mark.parametrize("path", [
    "README.md", "docs/UNIFIED_PROJECT_REQUIREMENTS.md", "docs/MODEL_ROUTING_GUIDE.md",
])
def test_entry_docs_scope_analysis_to_explicit_archived_work(path):
    text = read_doc(path)
    for required in ("已归档", "单篇/系列", "作品", "个人画像", "推荐", "检查点"):
        assert required in text, (path, required)
    assert "整书 XP 分析为未实现的第二阶段" not in text
    assert "业务仍仅接入关键词清洗" not in text


def test_work_api_documents_all_registered_methods_and_paths():
    text = work_api_section()
    app = Flask(__name__)
    register_work_analysis_routes(app, object())
    rules = [rule for rule in app.url_map.iter_rules() if rule.rule.startswith(BASE)]
    assert rules
    for rule in rules:
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            assert f"{method} {rule.rule.rstrip('/')}" in text


def test_work_api_documents_strict_requests_budget_and_evidence_boundaries():
    text = work_api_section()
    required = set(PREVIEW_FIELDS) | set(CONFIRMATIONS) | {
        "preview_fingerprint", "idempotency_key", "expected_version", "request_limit",
        "token_limit", "active_seconds_limit", "256 KiB", "重复键", "非有限数",
        "128 KiB", "1 MiB", "Unicode code point", "UTF-16", "410", "no-store",
        "X-CSRF-Token", "idempotency_conflict", "source_changed", "report_stale",
        "archive_outdated", "metadata_only", "HTTP 200", "原始规范化请求",
        "已删除的原任务返回 410", "不重新创建或复活",
    }
    for token in required:
        assert token in text, token


def test_work_pages_document_current_binding_and_unknown_write_ownership():
    text = read_doc("docs/frontend-pages.md")
    for token in (
        "/dashboard/preferences?view=work-analysis", "dashboard_work_analysis.html",
        "当前绑定", "重新确认", "幂等", "原请求", "写入所有权", "expected_version",
        "analysis_id", "evidence_id", "410", "UTF-16", "localStorage", "短证据",
    ):
        assert token in text, token
    assert "main continue 不可用" in text


def test_routing_docs_distinguish_parent_reservation_from_generic_continue():
    text = read_doc("docs/MODEL_ROUTING_GUIDE.md")
    for token in (
        "work_binding=None", "input_tokens + message_overhead + safety_margin + output_reserve",
        "attachment", "ordinal=0", "metadata_only", "未知不补零", "物理发送", "退款",
        "/work-analyses/<run_id>/resume", "continue API 仍返回", "work_targets",
    ):
        assert token in text, token


def test_status_and_execution_docs_separate_implementation_from_final_acceptance():
    status = read_doc("docs/STATUS.md")
    for token in ("作品分析第二阶段", "Task 9", "原生 Linux", "canonical", "实际部署"):
        assert token in status, token
    for path in (
        "docs/superpowers/specs/2026-11-05-work-xp-analysis-design.md",
        "docs/superpowers/plans/2026-11-05-work-xp-analysis.md",
    ):
        text = read_doc(path)
        assert "Task 9" in text
        assert "项目现状" in text
        assert "整阶段验收" in text
