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
