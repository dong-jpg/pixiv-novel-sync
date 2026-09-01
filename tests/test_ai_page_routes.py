from __future__ import annotations

from pathlib import Path

from pixiv_novel_sync.webapp import create_app


TEMPLATES = Path("src/pixiv_novel_sync/templates")
# 三个 AI 页面：公共层（csrfFetch / errorText / streamSSE）必须由 base.html 提供
AI_PAGES = ("dashboard_ai.html", "dashboard_wizard.html", "dashboard_ai_reader.html")


def read(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def test_base_provides_shared_frontend_helpers():
    html = read("base.html")

    assert "window.csrfFetch = async function" in html
    assert "window.errorText = function" in html
    assert "window.streamSSE = async function" in html
    assert "window.aiApi = {" in html


def test_ai_pages_do_not_redefine_csrf_helpers():
    """回归：三个模板各自重写 csrfFetch，base.html 的全站版本形同虚设。"""
    for name in AI_PAGES:
        html = read(name)
        assert "async function csrfFetch" not in html, name
        assert "function ensureCsrfToken" not in html, name
        assert "'/api/csrf-token'" not in html, name


def test_ai_pages_route_every_request_through_shared_helpers():
    """页面不许绕过助手直接发请求，否则生产环境 CSRF 门会把它变成 403。"""
    for name in AI_PAGES:
        html = read(name)
        assert "window.csrfFetch" in html, name
        assert "await fetch(" not in html, name
        assert "window.fetch(" not in html, name


def test_ai_pages_use_shared_error_text():
    """错误体有 error / detail 两种字段，只读 error 会把失败显示成「请求失败」。"""
    for name in AI_PAGES:
        html = read(name)
        assert "window.errorText" in html, name


def test_ai_pages_do_not_hand_roll_sse_parsing():
    for name in AI_PAGES:
        html = read(name)
        assert "getReader()" not in html, name
        assert "window.streamSSE" in html, name


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
