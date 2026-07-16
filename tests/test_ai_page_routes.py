from __future__ import annotations

from pixiv_novel_sync.webapp import create_app


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
    client = create_app(config_path=str(config_path), env_path=str(env_path)).test_client()

    ai = client.get("/dashboard/ai", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    wizard = client.get("/dashboard/wizard", environ_base={"REMOTE_ADDR": "127.0.0.1"})

    ai_html = ai.get_data(as_text=True)
    wizard_html = wizard.get_data(as_text=True)
    assert 'data-page="ai-writing"' in ai_html
    assert 'data-page="writing-wizard"' not in ai_html
    assert 'data-page="writing-wizard"' in wizard_html
    assert 'data-page="ai-writing"' not in wizard_html
