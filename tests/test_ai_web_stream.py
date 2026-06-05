from pathlib import Path
from types import SimpleNamespace

from flask import Flask

from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.ai_web import register_ai_routes
from pixiv_novel_sync.settings import PixivSettings, Settings, StorageSettings, SyncSettings


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        pixiv=PixivSettings(refresh_token="", access_token=None, proxy=None, timeout=30, verify_ssl=True, user_id=None),
        sync=SyncSettings(
            enabled=True,
            initial_manual_only=False,
            download_assets=False,
            write_markdown=True,
            write_raw_text=True,
            bookmark_restricts=["public"],
            max_items_per_run=None,
            max_pages_per_run=None,
            delay_seconds_between_items=0,
            delay_seconds_between_pages=0,
        ),
        storage=StorageSettings(public_dir=tmp_path / "public", private_dir=tmp_path / "private", db_path=tmp_path / "ai.db"),
    )


def test_stream_close_cancels_upstream_generator(monkeypatch, tmp_path: Path):
    closed = {"value": False}

    def fake_stream_continue(self, payload):
        try:
            yield SimpleNamespace(type="delta", text="hello", data=None)
            yield SimpleNamespace(type="delta", text="later", data=None)
        except GeneratorExit:
            closed["value"] = True
            raise

    monkeypatch.setattr(AIWritingService, "stream_continue", fake_stream_continue)
    app = Flask(__name__)
    register_ai_routes(app, make_settings(tmp_path))
    client = app.test_client()

    response = client.post("/api/dashboard/ai/continue/stream", json={}, buffered=False)
    first = next(response.response)
    response.close()

    assert b"event: delta" in first
    assert closed["value"] is True
