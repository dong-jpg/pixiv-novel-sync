from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
WRITING_BRANCH = "https://github.com/dong-jpg/pixiv-novel-sync/tree/ai-writing"


class ReadmeHTML(HTMLParser):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def section(title: str) -> str:
    match = re.search(
        rf"^## [^\n]*{re.escape(title)}[^\n]*\n(.*?)(?=^## |\Z)",
        README,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"Missing README section: {title}"
    return match.group(1)


def test_readme_has_main_identity_and_writing_branch_link() -> None:
    assert "assets/main-logo.svg" in README
    assert "把喜欢的小说，留在自己的书库里。" in README
    assert WRITING_BRANCH in README
    assert "assets/logo" not in README
    assert "<table" not in README.lower()


def test_readme_opens_with_small_accessible_icon_and_text_identity() -> None:
    front = README.split("\n## ", 1)[0]
    html = ReadmeHTML(front)
    images = [attrs for tag, attrs in html.elements if tag == "img"]
    assert images, "The README needs a main-branch icon"
    assert images[0].get("src") == "assets/main-logo.svg"
    assert images[0].get("width") == "112"
    assert images[0].get("height") == "112"
    assert images[0].get("alt") == "Pixiv Novel Sync：书页与同步环图标"
    first_tag, first_attrs = html.elements[0]
    assert first_tag == "p"
    assert first_attrs.get("align") == "center"
    visible_text = "".join(html.text)
    assert re.search(r"^# Pixiv Novel Sync\s*$", visible_text, re.MULTILINE)
    assert "把喜欢的小说，留在自己的书库里。" in visible_text
    assert "私人书库" in visible_text


def test_readme_is_single_column_without_front_page_maintenance_ledger() -> None:
    assert "<table" not in README.lower()
    assert not re.search(r"^\s*\|?\s*:?-{3,}:?\s*\|", README, re.MULTILINE)
    front = README.split("## 快速开始", 1)[0]
    assert "开发状态" not in front
    assert "移动改造（" not in front
    assert not re.search(r"\b20\d{2}-\d{2}-\d{2}\b", front)


def test_readme_relative_links_and_image_resources_exist() -> None:
    html = ReadmeHTML(README)
    targets = re.findall(r"!?\[[^\]]*\]\(([^\s)]+)\)", README)
    targets.extend(
        value
        for _, attrs in html.elements
        for key, value in attrs.items()
        if key in {"src", "href"} and value
    )
    assert "assets/main-logo.svg" in targets
    for target in targets:
        url = urlsplit(target)
        if url.scheme or url.netloc or not url.path:
            continue
        path = (ROOT / unquote(url.path)).resolve()
        assert path.is_relative_to(ROOT), f"Not a repository-relative link: {target}"
        assert path.exists(), f"Broken README resource: {target}"


def test_readme_distinguishes_library_and_writing_branches() -> None:
    branches = section("分支")
    assert "main" in branches
    assert "私人书库" in branches
    assert WRITING_BRANCH in branches
    assert "写作" in branches
    assert re.search(r"main[^。\n]*(?:不包含|不提供)[^。\n]*写作", branches)


def test_readme_installation_explicitly_selects_main_and_supported_runtime() -> None:
    quick_start = section("快速开始")
    assert re.search(r"Python\s*(?:>=|≥)\s*3\.10", README)
    assert "Flask" in README and "SQLite" in README
    assert "无需前端构建" in README
    clone_lines = re.findall(r"^git clone [^\n]+", quick_start, re.MULTILINE)
    assert clone_lines, "Installation must explicitly clone main"
    assert all(re.search(r"(?:--branch|-b)\s+main\b", line) for line in clone_lines)
    assert "https://github.com/dong-jpg/pixiv-novel-sync.git" in quick_start
    assert "cd pixiv-novel-sync" in quick_start
    assert "python -m venv .venv" in quick_start
    assert "source .venv/bin/activate" in quick_start
    assert ".venv\\Scripts\\Activate.ps1" in quick_start
    assert "pip install -e ." in quick_start


def test_readme_documents_configuration_and_loopback_startup() -> None:
    quick_start = section("快速开始")
    for source, destination in (
        (".env.example", ".env"),
        ("config/config.yaml.example", "config/config.yaml"),
    ):
        assert re.search(
            rf"(?:cp|Copy-Item)\s+{re.escape(source)}\s+{re.escape(destination)}",
            quick_start,
        ), source
    assert "PIXIV_REFRESH_TOKEN" in quick_start
    assert "PIXIV_WEB_COOKIE" in quick_start
    assert "pixiv-novel-sync web-token-ui" in quick_start
    assert "http://127.0.0.1:5010/dashboard" in quick_start
    assert "http://127.0.0.1:5010/token-login" in quick_start
    assert "0.0.0.0" not in quick_start
    assert "PIXIV_NOVEL_SYNC_AI_SECRET_KEY" not in quick_start


def test_readme_uses_the_singular_bookmark_sync_task() -> None:
    assert re.search(
        r"pixiv-novel-sync sync\s+bookmark\s+following_novels\s+subscribed_series",
        README,
    )
    assert not re.search(r"pixiv-novel-sync sync\s+bookmarks\b", README)


def test_readme_describes_reader_workflows_without_promising_an_offline_web_app() -> None:
    for term in ("公开收藏", "私密收藏", "关注", "追更系列", "全文检索", "阅读进度", "EPUB"):
        assert term in README
    assert "CDN" in README
    assert re.search(r"(?:不|不能|并非)[^。\n]*完整离线网页", README)
    assert "已归档文件" in README


def test_readme_limits_rescue_to_existing_private_backups() -> None:
    assert "私人本地备份" in README
    assert re.search(r"(?:不能|无法)[^。\n]*恢复从未归档的作品", README)
    assert re.search(r"(?:不|不能)[^。\n]*绕过访问限制", README)
    assert "/dashboard/novels?category=rescue" in README
    assert "userscripts/pixiv-rescue.user.js" in README
    assert "独立救援 Token" in README
    assert re.search(r"支持[^。\n]*用户脚本[^。\n]*扩展[^。\n]*浏览器", README)
    assert re.search(r"安卓原生 Chrome[^。\n]*(?:不支持|不自带)", README)


def test_readme_requires_public_deployment_authentication_and_secure_cookies() -> None:
    security = section("部署与安全")
    assert "deploy.sh" in security
    assert "scripts/install_server.sh" in security
    assert "origin/main" in security and "备份" in security
    assert "公网" in security and "HTTPS" in security
    assert "DASHBOARD_TOKEN" in security
    assert "PIXIV_COOKIE_SECURE=1" in security
    assert "PIXIV_FLASK_SECRET" in security and "保持稳定" in security
    assert re.search(r"(?:留空|未配置)[^。\n]*仅[^。\n]*本机", security)


def test_readme_keeps_private_sessions_and_backups_safe() -> None:
    security = section("部署与安全")
    assert re.search(r"私人设备[^。\n]*30\s*天", security)
    assert re.search(r"默认不勾选", security)
    assert re.search(r"密码不[^。\n]*(?:浏览器|本地)缓存", security)
    assert "不要公开" in security
    for term in (".env", "令牌", "数据库", "私密备份", "日志", "config/config.yaml", "归档文件"):
        assert term in security
    assert "保留 14 天" in security
    assert "sync.task_log_retention_days" in security


def test_readme_keeps_ai_optional_and_discloses_provider_privacy() -> None:
    ai = section("可选进阶")
    assert README.index("## 部署与安全") < README.index("## 可选进阶")
    assert re.search(r"(?:无需|不需要)[^。\n]*AI[^。\n]*同步[^。\n]*阅读", ai)
    assert "本地统计" in README and "规则" in README
    assert "仅用于" in ai and "关键词清洗" in ai
    assert "clean_keywords" in ai and "原始统计词" in ai
    assert "失败" in ai
    assert re.search(r"(?:未接入|不提供)[^。\n]*总结[^。\n]*推荐解释", ai)
    assert "PIXIV_NOVEL_SYNC_AI_SECRET_KEY" in ai
    assert "跨 Provider" in ai and "Prompt" in ai
    assert "多个 Provider" in ai


def test_readme_links_documentation_development_license_and_feedback() -> None:
    development = section("文档与开发")
    assert "docs/INDEX.md" in development
    assert 'pip install -e ".[test]"' in development
    assert "python -m pytest -q" in development
    assert "[MIT License](LICENSE)" in README
    assert "https://github.com/dong-jpg/pixiv-novel-sync/issues" in README
