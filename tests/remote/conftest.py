from __future__ import annotations

import os
import socket
import hashlib
from types import SimpleNamespace
import time

import pytest


@pytest.fixture(autouse=True)
def remote_isolation(tmp_path, monkeypatch):
    """Remote tests never consume real credentials or reach a network."""
    for name in tuple(os.environ):
        if name.startswith("PIXIV_REMOTE_") or name == "INSTABOX_API_KEY":
            monkeypatch.delenv(name, raising=False)

    def deny_connect(*args, **kwargs):
        raise AssertionError("remote tests require injected transports")

    monkeypatch.setattr(socket.socket, "connect", deny_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_connect)
    monkeypatch.setenv("PIXIV_REMOTE_STATE_DIR", str(tmp_path / "remote"))
    monkeypatch.setenv("PIXIV_REMOTE_WORK_DIR", str(tmp_path / "scratch"))


@pytest.fixture
def remote_web_fixture(tmp_path, monkeypatch):
    from pixiv_novel_sync.storage_db import Database
    from pixiv_novel_sync.webapp import create_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "remote-test-dashboard")
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "remote-test-flask-secret")
    monkeypatch.setenv("PIXIV_REFRESH_TOKEN", "test-only-refresh-token")
    monkeypatch.setenv("PIXIV_REMOTE_ENABLED", "1")
    monkeypatch.setenv("PIXIV_REMOTE_MACHINE_ID", "m_fixture")
    bootstrap = "B" * 32
    monkeypatch.setenv("PIXIV_REMOTE_WORKER_KEY_SHA256", hashlib.sha256(bootstrap.encode()).hexdigest())
    env_path = tmp_path / "empty.env"
    env_path.write_text("", encoding="utf-8")
    db_path = tmp_path / "state" / "test.db"
    db = Database(db_path)
    db.init_schema()
    db.conn.execute("INSERT INTO users(user_id,name,raw_json) VALUES(1,'Fixture author','{}')")
    db.conn.execute("""INSERT INTO novels(novel_id,user_id,title,visible,restrict_value,x_restrict,text_length,
        total_bookmarks,total_views,tags_json,raw_json,meta_hash,status)
        VALUES(42,1,'Synthetic EPUB',1,'public',0,100,0,0,'[]','{}','test-hash','normal')""")
    db.conn.execute("INSERT INTO novel_texts(novel_id,text_raw,has_content,text_hash) VALUES(42,?,1,'fixture-text')",
                    ("First page[newpage]Second page[[rb:中文 > zhongwen]]",))
    db.conn.commit()
    db.close()
    clock = SimpleNamespace(now=time.time())

    def new_app():
        app = create_app(config_path=str(tmp_path / "missing.yaml"), env_path=str(env_path), start_scheduler=False)
        app.config.update(TESTING=True, RAW_HTTP=True, REMOTE_CLOCK=lambda: clock.now)
        return app

    app = new_app()
    client = app.test_client()
    remote = app.extensions.get("remote_execution", {})
    fixture = SimpleNamespace(app=app, client=client, clock=clock, bootstrap=bootstrap,
                              library_db=db_path, db_path=db_path, new_app=new_app,
                              store=remote.get("store"), blobs=remote.get("blobs"),
                              control=remote.get("control"), provider_calls=[], csrf=None)

    def login():
        assert fixture.client.post("/api/auth/login", data={"token": "remote-test-dashboard"}).status_code == 302
        fixture.csrf = fixture.client.get("/api/csrf-token").get_json()["csrf_token"]
        return {"X-CSRF-Token": fixture.csrf}

    fixture.login = login
    yield fixture
    app.extensions.get("close_work_analysis_owner", lambda: None)()
