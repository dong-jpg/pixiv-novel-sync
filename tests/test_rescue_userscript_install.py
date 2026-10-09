"""Keep the personal installation source stable without expanding permissions."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "userscripts" / "pixiv-rescue.user.js"
INSTALL_URL = (
    "https://raw.githubusercontent.com/dong-jpg/pixiv-novel-sync/"
    "main/userscripts/pixiv-rescue.user.js"
)


def metadata() -> dict[str, list[str]]:
    script = SCRIPT.read_text(encoding="utf-8")
    assert script.startswith("// ==UserScript==\n")
    header, separator, _ = script.partition("// ==/UserScript==")
    assert separator
    fields: dict[str, list[str]] = {}
    for key, value in re.findall(r"^//\s+@(\w+)\s+([^\n]+)", header, re.MULTILINE):
        fields.setdefault(key, []).append(value.strip())
    return fields


def test_personal_install_and_update_use_the_same_https_main_source() -> None:
    fields = metadata()
    assert fields.get("downloadURL") == [INSTALL_URL]
    assert fields.get("updateURL") == [INSTALL_URL]


def test_personal_install_keeps_identity_and_minimal_permissions() -> None:
    fields = metadata()
    assert fields.get("name") == ["Pixiv 小说私人备份救援阅读"]
    assert fields.get("namespace") == ["https://pixiv.dongboapp.com/"]
    assert fields.get("connect") == ["pixiv.dongboapp.com"]
    assert set(fields.get("grant", [])) == {
        "GM_xmlhttpRequest", "GM_getValue", "GM_setValue", "GM_registerMenuCommand",
    }
    assert set(fields.get("match", [])) == {
        "https://www.pixiv.net/novel/show.php*", "https://www.pixiv.net/novel/series/*",
    }
    assert "require" not in fields and "resource" not in fields
    version = fields.get("version", [""])
    assert len(version) == 1 and re.fullmatch(r"\d+\.\d+\.\d+", version[0])
    assert tuple(map(int, version[0].split("."))) >= (0, 1, 1)


def test_personal_install_identifies_its_author_license_and_source() -> None:
    fields = metadata()
    assert fields.get("author") == ["dong-jpg"]
    assert fields.get("license") == ["MIT"]
    assert fields.get("homepageURL") == ["https://github.com/dong-jpg/pixiv-novel-sync/tree/main"]
    assert fields.get("supportURL") == ["https://github.com/dong-jpg/pixiv-novel-sync/issues"]


def test_readme_and_guide_link_to_the_installable_file() -> None:
    for relative in ("README.md", "docs/RESCUE_USER_GUIDE.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert re.search(r"\[[^\]]+\]\(" + re.escape(INSTALL_URL) + r"\)", text), relative
        assert "?token=" not in text


def test_install_guide_covers_mobile_fallback_safe_upgrade_and_token_reuse() -> None:
    text = (ROOT / "docs" / "RESCUE_USER_GUIDE.md").read_text(encoding="utf-8")
    for term in (
        "从链接安装", "源码", "自动更新", "不要先卸载", "已有救援 Token",
        "@name", "@namespace", "DEFAULT_API_ORIGIN", "https://pixiv.dongboapp.com",
        "GM_xmlhttpRequest", "GM_getValue", "GM_setValue", "GM_registerMenuCommand",
        "DASHBOARD_TOKEN",
    ):
        assert term in text, term
    assert re.search(r"旧 Token[^。\n]*(?:失效|不可用)", text)
    assert not re.search(r"`API_ORIGIN`", text), "The old constant name is misleading"
