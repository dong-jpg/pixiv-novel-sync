from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
API_CONTRACT = (ROOT / "docs" / "frontend-api-contract.md").read_text(
    encoding="utf-8"
)
FRONTEND_PAGES = (ROOT / "docs" / "frontend-pages.md").read_text(
    encoding="utf-8"
)
DOC_INDEX = (ROOT / "docs" / "INDEX.md").read_text(encoding="utf-8")
UNIFIED_REQUIREMENTS = (
    ROOT / "docs" / "UNIFIED_PROJECT_REQUIREMENTS.md"
).read_text(encoding="utf-8")
AI_UNIFIED_REQUIREMENTS = (
    ROOT
    / "docs"
    / "superpowers"
    / "specs"
    / "2026-07-27-ai-model-catalog-pools-unified-requirements.md"
).read_text(encoding="utf-8")


def _enclosing_function(tree: ast.AST, target: ast.AST) -> str | None:
    matches: list[tuple[int, str]] = []
    target_line = getattr(target, "lineno", -1)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end_line = getattr(node, "end_lineno", node.lineno)
        if node.lineno <= target_line <= end_line:
            matches.append((node.lineno, node.name))
    return max(matches, default=(-1, None))[1]


def test_readme_describes_catalog_pools_and_no_old_single_provider_fallback() -> None:
    assert "尚不支持跨 Provider fallback" not in README
    for text in (
        "模型目录",
        "模型池",
        "/api/dashboard/ai/providers/<provider_id>/models/sync",
        "16 个候选",
        "32 次网络请求",
        "30 分钟",
    ):
        assert text in README


def test_frontend_docs_cover_model_routing_operations_and_privacy() -> None:
    for text in (
        "/api/dashboard/ai/providers/<provider_id>/models",
        "/api/dashboard/ai/model-sync-operations/<operation_id>/confirm-empty",
        "/api/dashboard/ai/model-pools/<pool_id>/members",
        "/api/dashboard/ai/jobs/<job_id>/continue",
        "candidate_snapshot_hash",
        "required_capabilities",
        "跨 Provider",
        "Prompt",
    ):
        assert text in API_CONTRACT

    for text in (
        "ai-model-pools",
        "模型目录",
        "模型池",
        "partial",
        "下一个模型",
    ):
        assert text in FRONTEND_PAGES


def test_unified_requirements_record_completed_model_routing_line() -> None:
    assert "AI 模型目录、模型池与统一路由第一阶段 Task 1-22 已完成" in (
        UNIFIED_REQUIREMENTS
    )
    assert "当前状态：第一阶段 Task 1-22 已完成" in AI_UNIFIED_REQUIREMENTS
    assert "Task 4（有序池、后备图、版本 CAS）是当前开发入口" not in (
        UNIFIED_REQUIREMENTS
    )
    assert "代码处于 Task 4 起点" not in AI_UNIFIED_REQUIREMENTS
    assert "UNIFIED_PROJECT_REQUIREMENTS.md" in DOC_INDEX
    assert "2026-07-27-ai-model-catalog-pools-unified-requirements.md" in DOC_INDEX


def test_only_router_provider_implementation_and_connection_test_call_provider() -> None:
    ai_root = ROOT / "src" / "pixiv_novel_sync" / "ai"
    allowed_modules = {"model_router.py", "providers.py"}
    offenders: list[str] = []

    for path in ai_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "stream_generate"
            ):
                continue
            if path.name in allowed_modules:
                continue
            function_name = _enclosing_function(tree, node)
            if path.name == "admin.py" and function_name == "test_provider":
                continue
            offenders.append(
                f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:{function_name}"
            )

    assert offenders == []


def test_dashboard_templates_do_not_reference_private_storage_fields() -> None:
    forbidden = {"api_key_encrypted", "candidate_snapshot_json", "owner_token"}
    offenders: list[str] = []
    for path in (ROOT / "src" / "pixiv_novel_sync" / "templates").glob("*.html"):
        text = path.read_text(encoding="utf-8")
        for field in sorted(forbidden):
            if field in text:
                offenders.append(f"{path.name}:{field}")

    assert offenders == []
