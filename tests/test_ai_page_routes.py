from __future__ import annotations

import re
from pathlib import Path

from pixiv_novel_sync.webapp import create_app


TEMPLATES = Path("src/pixiv_novel_sync/templates")
# 三个 AI 页面：公共层（csrfFetch / errorText / streamSSE）必须由 base.html 提供
AI_PAGES = ("dashboard_ai.html", "dashboard_wizard.html", "dashboard_ai_reader.html")

_LINE_COMMENT = re.compile(r"^[ \t]*//.*$", re.M)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
# 裸 fetch(：排除 csrfFetch( / .fetch( 这类带前缀的形态
_BARE_FETCH = re.compile(r"(?<![\w.])fetch\s*\(")
_API_URL = re.compile(r"""['"`]/api/""")
_SSE_URL = re.compile(r"/stream['\"`]")


def read(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def read_code(name: str) -> str:
    """读模板并剥掉注释，只留可执行部分。

    下面三条正向断言原本是朴素子串匹配，而 dashboard_ai.html 顶部有一段
    「本页不再自建副本（window.csrfFetch / errorText / streamSSE / aiApi）」的
    说明注释——一个新页面只要把那段注释一起抄过去，就能在完全没调用公共层的
    情况下让断言变绿。所以断言前必须先剥注释，否则守的是注释不是代码。
    """
    code = _HTML_COMMENT.sub("", read(name))
    code = _BLOCK_COMMENT.sub("", code)
    return _LINE_COMMENT.sub("", code)


def test_base_provides_shared_frontend_helpers():
    html = read("base.html")

    assert "window.csrfFetch = async function" in html
    assert "window.errorText = function" in html
    assert "window.streamSSE = async function" in html
    assert "window.aiApi = {" in html


def test_ai_pages_do_not_redefine_csrf_helpers():
    """回归：三个模板各自重写 csrfFetch，base.html 的全站版本形同虚设。"""
    for name in AI_PAGES:
        code = read_code(name)
        assert "async function csrfFetch" not in code, name
        assert "function ensureCsrfToken" not in code, name
        assert "'/api/csrf-token'" not in code, name


def test_ai_pages_route_every_request_through_shared_helpers():
    """页面不许绕过助手直接发请求，否则生产环境 CSRF 门会把它变成 403。

    正向部分是**条件式**的：拆页后不是每个页面都会直接调 window.csrfFetch
    （只用 window.aiApi / streamSSE 的页面同样合规，助手内部才调它）。所以
    判据是「这个页面若引用了 /api/ URL，就必须经由某个公共层助手」，而不是
    要求每页都出现某个固定字面量——后者会逼着新页面硬塞一次无用调用。
    """
    for name in AI_PAGES:
        code = read_code(name)
        assert not _BARE_FETCH.search(code), f"{name} 出现裸 fetch("
        if _API_URL.search(code):
            assert any(
                helper in code
                for helper in ("window.aiApi", "window.csrfFetch", "window.streamSSE")
            ), f"{name} 引用了 /api/ 却没走任何公共层助手"


def test_ai_pages_use_shared_error_text():
    """错误体有 error / detail 两种字段，只读 error 会把失败显示成「请求失败」。

    条件式：window.aiApi.request 内部已经走 errorText，所以只有**直接**调
    window.csrfFetch（绕过 api()）的页面才必须自己用 errorText。
    """
    for name in AI_PAGES:
        code = read_code(name)
        if "window.csrfFetch" in code:
            assert "window.errorText" in code, f"{name} 直接调 csrfFetch 却没用 errorText"


def test_ai_pages_do_not_hand_roll_sse_parsing():
    """条件式：没有流式端点的页面不该被迫出现 streamSSE 字面量。"""
    for name in AI_PAGES:
        code = read_code(name)
        assert "getReader()" not in code, name
        if _SSE_URL.search(code):
            assert "window.streamSSE" in code, f"{name} 有 /stream 端点却自行解析"


def test_shared_helper_assertions_ignore_comments():
    """守护上面三条断言的守护者：确认 read_code 真的剥掉了注释。

    没有这条，read_code 哪天退化成 read() 也不会有人发现，而那正是
    「抄注释即可假通过」这个漏洞的复发路径。
    """
    marker = "window.streamSSE"
    ai_raw = read("dashboard_ai.html")
    ai_code = read_code("dashboard_ai.html")

    # dashboard_ai.html 顶部确实有一段提到公共层名字的注释
    assert ai_raw.count(marker) > ai_code.count(marker), (
        "dashboard_ai.html 的公共层注释没有被剥掉，三条正向断言可被注释欺骗"
    )
    assert _LINE_COMMENT.sub("", "  // window.csrfFetch\nreal();") == "\nreal();"
    assert _HTML_COMMENT.sub("", "<!-- window.errorText -->x") == "x"


def test_ai_and_wizard_share_one_profile_loader():
    """agents / 风格档案 / 小说档案 / 偏好画像的加载只允许有一份实现。"""
    for name in ("dashboard_ai.html", "dashboard_wizard.html"):
        html = read(name)
        assert "window.aiApi" in html, name
        assert "'/api/dashboard/ai/style-profiles'" not in html, name
        assert "'/api/dashboard/ai/novel-profiles'" not in html, name
        assert "'/api/dashboard/preferences/profiles'" not in html, name


def test_ai_and_wizard_routes_render_distinct_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "ai-page-route-test-secret")
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "storage:\n"
        f"  public_dir: {(tmp_path / 'public').as_posix()}\n"
        f"  private_dir: {(tmp_path / 'private').as_posix()}\n"
        f"  db_path: {(tmp_path / 'routes.db').as_posix()}\n"
        "sync:\n"
        "  auto_sync_enabled: false\n",
        encoding="utf-8",
    )
    client = create_app(config_path=str(config_path), env_path=str(env_path), start_scheduler=False).test_client()

    ai = client.get("/dashboard/ai", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    wizard = client.get("/dashboard/wizard", environ_base={"REMOTE_ADDR": "127.0.0.1"})

    ai_html = ai.get_data(as_text=True)
    wizard_html = wizard.get_data(as_text=True)
    assert 'data-page="ai-writing"' in ai_html
    assert 'data-page="writing-wizard"' not in ai_html
    assert 'data-page="writing-wizard"' in wizard_html
    assert 'data-page="ai-writing"' not in wizard_html
