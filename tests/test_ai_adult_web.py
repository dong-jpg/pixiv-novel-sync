from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from types import SimpleNamespace

from flask import Flask

from ai_adult_testkit import application_row, seed_adult_project, valid_adult_payload
from pixiv_novel_sync.ai.service import AIConflictError, AIWritingService
from pixiv_novel_sync.ai.adult_auth import AdultOwner, sign_adult_access
from pixiv_novel_sync.ai_web import register_ai_routes
from pixiv_novel_sync.settings import Settings, StorageSettings
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.webapp import create_app
from pixiv_novel_sync.ai.adult_types import raw_sha256


def _settings(tmp_path: Path, dashboard_token: str | None) -> Settings:
    return Settings(
        pixiv=None,  # type: ignore[arg-type]
        sync=None,  # type: ignore[arg-type]
        storage=StorageSettings(
            public_dir=tmp_path / "public",
            private_dir=tmp_path / "private",
            db_path=tmp_path / "adult-web.db",
        ),
        dashboard_token=dashboard_token,
    )


def _app(tmp_path: Path, dashboard_token: str | None = "dashboard-secret") -> Flask:
    app = Flask(__name__)
    app.secret_key = "test-app-secret"
    app.config.update(TESTING=True)
    register_ai_routes(app, _settings(tmp_path, dashboard_token))
    return app


def _authenticate(client) -> None:
    with client.session_transaction() as current_session:
        current_session["authenticated"] = True
        current_session["authenticated_at"] = time.time_ns()


def _owner_scope(app: Flask, dashboard_token: str = "dashboard-secret") -> str:
    secret = app.secret_key
    assert isinstance(secret, str)
    return hmac.new(
        secret.encode("utf-8"),
        f"adult-owner:{dashboard_token}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _access_token(app: Flask, job_id: str, scope: str) -> str:
    with app.app_context():
        return sign_adult_access(
            AdultOwner(scope=scope, authenticated_at=time.time_ns()),
            job_id,
        )


def _seed_adult_job(
    tmp_path: Path,
    app: Flask,
    *,
    job_id: str,
    status: str,
    scope: str | None = None,
    candidate: str | None = None,
) -> tuple[int, int, str]:
    owner_scope = scope or _owner_scope(app)
    db = Database(_settings(tmp_path, "dashboard-secret").storage.db_path)
    db.init_schema()
    try:
        project_id = db.create_ai_writing_project({"name": "adult project", "settings": {}})
        chapter_id = db.create_ai_chapter(
            {
                "project_id": project_id,
                "chapter_number": 1,
                "content": "chapter content",
            }
        )
        db.conn.execute(
            """
            INSERT INTO ai_jobs (
                job_id, task_type, status, input_json, output_text,
                output_json, owner_scope, owner_token, stage
            ) VALUES (?, 'adult_polish', ?, ?, ?, ?, ?, ?, 'main')
            """,
            (
                job_id,
                status,
                '{"project_id":%d,"chapter_id":%d}' % (project_id, chapter_id),
                candidate,
                '{"validation_hash":"%s"}' % ("f" * 64),
                owner_scope,
                "execution-owner-token",
            ),
        )
        db.conn.commit()
        return project_id, chapter_id, owner_scope
    finally:
        db.close()


def _configured_adult_payload(tmp_path: Path, app: Flask, client) -> dict:
    db = Database(_settings(tmp_path, "dashboard-secret").storage.db_path)
    db.init_schema()
    try:
        if db.get_ai_writing_project(1) is None:
            seed_adult_project(db)
            provider_id = 1
            agent_id = 7
        else:
            provider_id = 1
            if db.get_ai_provider(provider_id) is None:
                provider_id = db.create_ai_provider(
                    {
                        "name": "adult-provider",
                        "provider_type": "openai",
                        "default_model": "adult-model",
                        "enabled": True,
                    }
                )
            agent_id = 7
            if db.get_ai_agent(agent_id) is None:
                agent_id = db.create_ai_agent(
                    {
                        "name": "成人描写润色",
                        "task_type": "adult_polish",
                        "binding_type": "fixed",
                        "provider_id": provider_id,
                        "model": "adult-model",
                        "system_prompt": "只输出替换片段",
                        "required_capabilities": [],
                    }
                )
        if not db.list_ai_provider_models(provider_id)["items"]:
            db.create_ai_provider_model(
                {
                    "provider_id": provider_id,
                    "model_key": "adult-model",
                    "manual_capabilities": ["json"],
                    "enabled": True,
                }
            )
        db.conn.execute(
            """
            UPDATE ai_adult_review_bindings
            SET binding_type = 'fixed', provider_id = ?, model = 'adult-model',
                model_pool_id = NULL, enabled = 1
            """,
            (provider_id,),
        )
        db.conn.commit()
        project = db.get_ai_writing_project(1)
        chapters = db.list_ai_chapters(1)
        assert project is not None and chapters
        chapter = chapters[0]
        content = str(chapter["content"] or "")
        if len(content) < 20:
            content = "temporary adult chapter content for route tests"
            db.update_ai_chapter(int(chapter["id"]), {"content": content})
            chapter = db.get_ai_chapter(int(chapter["id"]))
            assert chapter is not None
        target_end = min(len(content), 24)
        target = content[:target_end]
    finally:
        db.close()
    response = client.post(
        "/api/dashboard/ai/polish/adult/scope",
        json={"agent_id": agent_id},
    )
    assert response.status_code == 200, response.get_json()
    return valid_adult_payload(
        project_id=1,
        chapter_id=int(chapter["id"]),
        agent_id=agent_id,
        target_start=0,
        target_end=target_end,
        chapter_content_hash=raw_sha256(content),
        target_text_hash=raw_sha256(target),
        chapter_revision=int(chapter.get("chapter_revision") or 0),
        provider_scope_hash=response.get_json()["data"]["provider_scope_hash"],
    )


def test_adult_route_requires_configured_token_even_from_loopback(tmp_path):
    client = _app(tmp_path, dashboard_token=None).test_client()

    response = client.post(
        "/api/dashboard/ai/polish/adult/stream",
        json={},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 403


def test_other_authenticated_session_cannot_read_adult_job(tmp_path):
    app = _app(tmp_path)
    db = Database(_settings(tmp_path, "dashboard-secret").storage.db_path)
    db.init_schema()
    try:
        db.conn.execute(
            """
            INSERT INTO ai_jobs (
                job_id, task_type, status, input_json, output_text, owner_scope
            ) VALUES (?, 'adult_polish', 'succeeded', ?, ?, ?)
            """,
            (
                "adult-job-hidden",
                '{"project_id":1,"chapter_id":1}',
                "private candidate",
                "other-owner-scope",
            ),
        )
        db.conn.commit()
    finally:
        db.close()
    client = app.test_client()
    _authenticate(client)

    response = client.get("/api/dashboard/ai/polish/adult/adult-job-hidden")

    assert response.status_code in {403, 404}
    assert response.is_json
    assert "output_text" not in response.get_data(as_text=True)
    assert "private candidate" not in response.get_data(as_text=True)


def test_adult_sse_buffers_delta_and_sets_no_store_headers(
    tmp_path,
    monkeypatch,
):
    def fake_stream(self, payload, owner_scope, owner_token, **kwargs):
        assert owner_scope
        assert owner_token
        yield SimpleNamespace(
            type="metadata",
            text=None,
            data={
                "job_id": "adult-job-1",
                "owner_scope": "private-owner-scope",
                "provider_response": "private-provider-response",
            },
        )
        yield SimpleNamespace(type="delta", text="not-client-visible", data=None)
        yield SimpleNamespace(
            type="progress",
            text=None,
            data={"phase": "main", "output_text": "private-progress-output"},
        )
        yield SimpleNamespace(
            type="candidate",
            text="candidate text",
            data={
                "job_id": "adult-job-1",
                "applicable": True,
                "owner_token": "private-owner-token",
            },
        )
        yield SimpleNamespace(
            type="done",
            text=None,
            data={"job_id": "adult-job-1", "prompt": "private-prompt"},
        )

    monkeypatch.setattr(AIWritingService, "stream_adult_polish", fake_stream)
    client = _app(tmp_path).test_client()
    _authenticate(client)
    payload = _configured_adult_payload(tmp_path, client.application, client)

    response = client.post(
        "/api/dashboard/ai/polish/adult/stream",
        json=payload,
        buffered=True,
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"].startswith("no-store")
    assert response.headers["Pragma"] == "no-cache"
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert b"event: delta" not in response.data
    assert b"not-client-visible" not in response.data
    assert b"event: candidate" in response.data
    assert b"candidate text" in response.data
    for private_value in (
        b"private-owner-scope",
        b"private-provider-response",
        b"private-progress-output",
        b"private-owner-token",
        b"private-prompt",
    ):
        assert private_value not in response.data


def test_adult_detail_requires_access_token_and_sanitizes_candidate(tmp_path):
    app = _app(tmp_path)
    _project_id, _chapter_id, scope = _seed_adult_job(
        tmp_path,
        app,
        job_id="adult-job-detail",
        status="succeeded",
        candidate="candidate text",
    )
    client = app.test_client()
    _authenticate(client)

    blocked = client.get("/api/dashboard/ai/polish/adult/adult-job-detail")
    allowed = client.get(
        "/api/dashboard/ai/polish/adult/adult-job-detail",
        headers={
            "X-Adult-Access-Token": _access_token(
                app,
                "adult-job-detail",
                scope,
            )
        },
    )

    assert blocked.status_code == 403
    assert allowed.status_code == 200
    payload = allowed.get_json()["data"]
    assert payload["candidate"] == "candidate text"
    assert "output_text" not in payload
    assert "owner_scope" not in payload
    assert "owner_token" not in payload
    assert allowed.headers["Cache-Control"].startswith("no-store")
    assert allowed.headers["Pragma"] == "no-cache"
    assert allowed.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"
    assert allowed.headers["X-Content-Type-Options"] == "nosniff"


def test_adult_access_token_query_parameter_is_rejected(tmp_path):
    app = _app(tmp_path)
    _project_id, _chapter_id, scope = _seed_adult_job(
        tmp_path,
        app,
        job_id="adult-query-token",
        status="succeeded",
        candidate="candidate text",
    )
    client = app.test_client()
    _authenticate(client)
    token = _access_token(app, "adult-query-token", scope)

    response = client.get(
        "/api/dashboard/ai/polish/adult/adult-query-token",
        query_string={"access_token": token},
    )

    assert response.status_code == 403
    assert token not in response.get_data(as_text=True)


def test_invalid_access_token_does_not_reveal_job_existence(tmp_path):
    app = _app(tmp_path)
    _seed_adult_job(
        tmp_path,
        app,
        job_id="adult-existing-secret",
        status="succeeded",
        candidate="candidate text",
    )
    client = app.test_client()
    _authenticate(client)
    headers = {"X-Adult-Access-Token": "invalid-token"}

    existing = client.get(
        "/api/dashboard/ai/polish/adult/adult-existing-secret",
        headers=headers,
    )
    missing = client.get(
        "/api/dashboard/ai/polish/adult/adult-missing-secret",
        headers=headers,
    )

    assert existing.status_code == missing.status_code == 403
    assert existing.get_json() == missing.get_json()


def test_adult_job_responses_filter_nested_output_metadata(tmp_path):
    app = _app(tmp_path)
    _project_id, _chapter_id, scope = _seed_adult_job(
        tmp_path,
        app,
        job_id="adult-nested-output",
        status="succeeded",
        candidate="candidate text",
    )
    db = Database(_settings(tmp_path, "dashboard-secret").storage.db_path)
    try:
        db.conn.execute(
            "UPDATE ai_jobs SET output_json = ? WHERE job_id = ?",
            (
                '{"code":"succeeded","validation_hash":"%s",'
                '"provider_response":"nested-provider-secret",'
                '"context":{"prompt":"nested-prompt-secret"},'
                '"owner_token":"nested-owner-secret"}' % ("f" * 64),
                "adult-nested-output",
            ),
        )
        db.conn.commit()
    finally:
        db.close()
    client = app.test_client()
    _authenticate(client)

    generic = client.get("/api/dashboard/ai/jobs/adult-nested-output")
    dedicated = client.get(
        "/api/dashboard/ai/polish/adult/adult-nested-output",
        headers={
            "X-Adult-Access-Token": _access_token(
                app,
                "adult-nested-output",
                scope,
            )
        },
    )

    assert generic.status_code == dedicated.status_code == 200
    for response in (generic, dedicated):
        body = response.get_data(as_text=True)
        assert "nested-provider-secret" not in body
        assert "nested-prompt-secret" not in body
        assert "nested-owner-secret" not in body


def test_adult_events_require_bound_token_and_replay_only_committed_candidate(tmp_path):
    app = _app(tmp_path)
    _project_id, _chapter_id, scope = _seed_adult_job(
        tmp_path,
        app,
        job_id="adult-job-events",
        status="succeeded",
        candidate="committed candidate",
    )
    client = app.test_client()
    _authenticate(client)

    blocked = client.get(
        "/api/dashboard/ai/polish/adult/adult-job-events/events",
        headers={
            "X-Adult-Access-Token": _access_token(app, "another-job", scope),
        },
    )
    allowed = client.get(
        "/api/dashboard/ai/polish/adult/adult-job-events/events",
        headers={
            "X-Adult-Access-Token": _access_token(app, "adult-job-events", scope),
        },
        buffered=True,
    )

    assert blocked.status_code == 403
    assert allowed.status_code == 200
    assert allowed.headers["Cache-Control"].startswith("no-store")
    assert b"event: delta" not in allowed.data
    assert b"event: candidate" in allowed.data
    assert b"committed candidate" in allowed.data


def test_adult_cancel_is_owner_scoped_and_cannot_overwrite_terminal_job(tmp_path):
    app = _app(tmp_path)
    _project_id, _chapter_id, scope = _seed_adult_job(
        tmp_path,
        app,
        job_id="adult-job-running",
        status="running",
    )
    _seed_adult_job(
        tmp_path,
        app,
        job_id="adult-job-terminal",
        status="succeeded",
        candidate="committed candidate",
    )
    client = app.test_client()
    _authenticate(client)

    running = client.post(
        "/api/dashboard/ai/polish/adult/adult-job-running/cancel",
        headers={
            "X-Adult-Access-Token": _access_token(app, "adult-job-running", scope),
        },
        json={},
    )
    terminal = client.post(
        "/api/dashboard/ai/polish/adult/adult-job-terminal/cancel",
        headers={
            "X-Adult-Access-Token": _access_token(app, "adult-job-terminal", scope),
        },
        json={},
    )

    assert running.status_code == 200
    assert running.get_json()["data"]["cancel_requested"] is True
    assert terminal.status_code == 200
    assert terminal.get_json()["data"]["cancel_requested"] is False
    db = Database(_settings(tmp_path, "dashboard-secret").storage.db_path)
    try:
        assert db.get_adult_job("adult-job-running", scope)["status"] == "cancelled"
        terminal_job = db.get_adult_job("adult-job-terminal", scope)
        assert terminal_job["status"] == "succeeded"
        assert terminal_job["output_text"] == "committed candidate"
    finally:
        db.close()


def test_generic_adult_job_reads_are_owner_scoped(tmp_path):
    db = Database(tmp_path / "owner-filter.db")
    db.init_schema()
    try:
        for job_id, task_type, scope in (
            ("adult-a", "adult_polish", "owner-a"),
            ("adult-b", "adult_polish", "owner-b"),
            ("safety-a", "adult_safety_review", "owner-a"),
            ("safety-b", "adult_safety_review", "owner-b"),
            ("fact-a", "adult_fact_guard", "owner-a"),
            ("fact-b", "adult_fact_guard", "owner-b"),
            ("general", "general", None),
        ):
            db.conn.execute(
                """
                INSERT INTO ai_jobs (job_id, task_type, status, input_json, owner_scope)
                VALUES (?, ?, 'succeeded', '{}', ?)
                """,
                (job_id, task_type, scope),
            )
        db.conn.commit()

        listed = db.list_ai_jobs(owner_scope="owner-a")

        assert {item["job_id"] for item in listed["items"]} == {
            "adult-a",
            "safety-a",
            "fact-a",
            "general",
        }
        assert db.get_ai_job("adult-a", owner_scope="owner-a") is not None
        assert db.get_ai_job("adult-b", owner_scope="owner-a") is None
        assert db.get_ai_job("safety-b", owner_scope="owner-a") is None
        assert db.get_ai_job("fact-b", owner_scope="owner-a") is None
        assert db.get_ai_job("general", owner_scope="owner-a") is not None
    finally:
        db.close()


def test_unified_adult_logs_are_owner_scoped(tmp_path):
    db = Database(tmp_path / "owner-logs.db")
    db.init_schema()
    try:
        for job_id, task_type, scope in (
            ("adult-a", "adult_polish", "owner-a"),
            ("adult-b", "adult_polish", "owner-b"),
            ("safety-a", "adult_safety_review", "owner-a"),
            ("safety-b", "adult_safety_review", "owner-b"),
            ("fact-a", "adult_fact_guard", "owner-a"),
            ("fact-b", "adult_fact_guard", "owner-b"),
        ):
            db.conn.execute(
                """
                INSERT INTO ai_jobs (
                    job_id, task_type, status, input_json, owner_scope, started_at
                ) VALUES (?, ?, 'succeeded', '{}', ?, CURRENT_TIMESTAMP)
                """,
                (job_id, task_type, scope),
            )
        db.conn.commit()

        result = db.get_ai_task_logs(
            owner_scope="owner-a",
        )

        assert {item["job_id"] for item in result["items"]} == {
            "adult-a",
            "safety-a",
            "fact-a",
        }
    finally:
        db.close()


def test_cleanup_does_not_delete_other_owner_adult_jobs(tmp_path):
    db = Database(tmp_path / "owner-cleanup.db")
    db.init_schema()
    try:
        for job_id, task_type, scope in (
            ("adult-a", "adult_polish", "owner-a"),
            ("adult-b", "adult_polish", "owner-b"),
            ("safety-a", "adult_safety_review", "owner-a"),
            ("safety-b", "adult_safety_review", "owner-b"),
            ("fact-a", "adult_fact_guard", "owner-a"),
            ("fact-b", "adult_fact_guard", "owner-b"),
        ):
            db.conn.execute(
                """
                INSERT INTO ai_jobs (
                    job_id, task_type, status, input_json, owner_scope, created_at
                ) VALUES (?, ?, 'failed', '{}', ?, datetime('now', '-10 days'))
                """,
                (job_id, task_type, scope),
            )
        db.conn.commit()

        deleted = db.cleanup_ai_jobs(keep_days=3, owner_scope="owner-a")

        assert deleted == 3
        assert db.get_adult_job("adult-a", "owner-a") is None
        assert db.get_adult_job("adult-b", "owner-b") is not None
        assert db.get_ai_job("safety-a") is None
        assert db.get_ai_job("safety-b") is not None
        assert db.get_ai_job("fact-a") is None
        assert db.get_ai_job("fact-b") is not None
    finally:
        db.close()


def test_generic_job_routes_pass_authenticated_adult_owner_scope(tmp_path):
    app = _app(tmp_path)
    scope = _owner_scope(app)
    db = Database(_settings(tmp_path, "dashboard-secret").storage.db_path)
    db.init_schema()
    try:
        for job_id, owner_scope in (("adult-a", scope), ("adult-b", "other-owner")):
            db.conn.execute(
                """
                INSERT INTO ai_jobs (
                    job_id, task_type, status, input_json, output_text, owner_scope
                ) VALUES (?, 'adult_polish', 'succeeded', '{}', ?, ?)
                """,
                (job_id, f"{job_id}-candidate", owner_scope),
            )
        db.conn.commit()
    finally:
        db.close()
    client = app.test_client()
    _authenticate(client)

    listed = client.get("/api/dashboard/ai/jobs?task_type=adult_polish")
    visible = client.get("/api/dashboard/ai/jobs/adult-a")
    hidden = client.get("/api/dashboard/ai/jobs/adult-b")

    assert listed.status_code == 200
    listed_items = listed.get_json()["data"]["items"]
    assert [item["job_id"] for item in listed_items] == ["adult-a"]
    assert "output_text" not in listed_items[0]
    assert "adult-a-candidate" not in listed.get_data(as_text=True)
    adult_metadata_fields = {
        "job_id",
        "task_type",
        "status",
        "stage",
        "started_at",
        "finished_at",
        "created_at",
        "error_message",
        "output",
    }
    assert set(listed_items[0]) <= adult_metadata_fields
    assert visible.status_code == 200
    visible_data = visible.get_json()["data"]
    assert "output_text" not in visible_data
    assert set(visible_data) <= adult_metadata_fields
    assert "adult-a-candidate" not in visible.get_data(as_text=True)
    assert hidden.status_code == 404
    assert "adult-b-candidate" not in hidden.get_data(as_text=True)


def test_generic_cleanup_passes_authenticated_adult_owner_scope(tmp_path):
    app = _app(tmp_path)
    scope = _owner_scope(app)
    db = Database(_settings(tmp_path, "dashboard-secret").storage.db_path)
    db.init_schema()
    try:
        for job_id, owner_scope in (("adult-a", scope), ("adult-b", "other-owner")):
            db.conn.execute(
                """
                INSERT INTO ai_jobs (
                    job_id, task_type, status, input_json, owner_scope, created_at
                ) VALUES (?, 'adult_polish', 'failed', '{}', ?, datetime('now', '-10 days'))
                """,
                (job_id, owner_scope),
            )
        db.conn.commit()
    finally:
        db.close()
    client = app.test_client()
    _authenticate(client)

    response = client.post("/api/dashboard/ai/jobs/cleanup", json={"keep_days": 3})

    assert response.status_code == 200
    assert response.get_json()["data"]["deleted"] == 1
    db = Database(_settings(tmp_path, "dashboard-secret").storage.db_path)
    try:
        assert db.get_adult_job("adult-a", scope) is None
        assert db.get_adult_job("adult-b", "other-owner") is not None
    finally:
        db.close()


def test_webapp_logs_scope_adult_jobs_and_csrf_blocks_adult_mutation(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("DASHBOARD_TOKEN", "dashboard-secret")
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "test-app-secret")
    env_path = tmp_path / "adult-web.env"
    env_path.write_text(
        "PIXIV_REFRESH_TOKEN=test\nDASHBOARD_TOKEN=dashboard-secret\n"
        "PIXIV_FLASK_SECRET=test-app-secret\n",
        encoding="utf-8",
    )
    app = create_app(env_path=str(env_path))
    app.config.update(TESTING=True)
    client = app.test_client()
    assert client.post(
        "/api/auth/login",
        data={"token": "dashboard-secret"},
    ).status_code == 302
    scope = _owner_scope(app)
    db_path = Path(app.extensions["pixiv_novel_sync.ai_service"]._current().db_path)
    db = Database(db_path)
    db.init_schema()
    try:
        for job_id, owner_scope in (("adult-a", scope), ("adult-b", "other-owner")):
            db.conn.execute(
                """
                INSERT INTO ai_jobs (
                    job_id, task_type, status, input_json, owner_scope, started_at
                ) VALUES (?, 'adult_polish', 'succeeded', '{}', ?, CURRENT_TIMESTAMP)
                """,
                (job_id, owner_scope),
            )
        db.conn.commit()
    finally:
        db.close()

    logs = client.get(
        "/api/dashboard/logs?category=ai&task_type=adult_polish"
    )
    blocked_mutation = client.post(
        "/api/dashboard/ai/polish/adult/stream",
        json={},
    )

    assert logs.status_code == 200
    assert [item["job_id"] for item in logs.get_json()["items"]] == ["adult-a"]
    assert blocked_mutation.status_code == 403
    assert blocked_mutation.get_json()["error"] == "csrf token invalid"


def test_adult_apply_verifies_signed_access_and_binds_only_its_hash(
    tmp_path,
    monkeypatch,
):
    app = _app(tmp_path)
    scope = _owner_scope(app)
    db = Database(_settings(tmp_path, "dashboard-secret").storage.db_path)
    db.init_schema()
    try:
        seed_adult_project(db)
        db.conn.execute(
            "UPDATE ai_jobs SET owner_scope = ? WHERE job_id = 'adult-job'",
            (scope,),
        )
        db.conn.commit()
        db.save_candidate_application(
            application_row(
                owner_scope=scope,
                candidate="candidate text",
            )
        )
    finally:
        db.close()
    calls = []

    def fake_apply(self, job_id, owner_scope, warning_ack_hash, access_token):
        calls.append((job_id, owner_scope, warning_ack_hash, access_token))
        return {
            "application_id": 1,
            "chapter_revision_after": 1,
            "chapter_hash_after": "a" * 64,
            "idempotent": False,
        }

    monkeypatch.setattr(AIWritingService, "apply_adult_polish", fake_apply)
    client = app.test_client()
    _authenticate(client)
    token = _access_token(app, "adult-job", scope)

    blocked = client.post(
        "/api/dashboard/ai/polish/adult/adult-job/apply",
        headers={"X-Adult-Access-Token": _access_token(app, "wrong-job", scope)},
        json={"warning_ack_hash": ""},
    )
    allowed = client.post(
        "/api/dashboard/ai/polish/adult/adult-job/apply",
        headers={"X-Adult-Access-Token": token},
        json={"warning_ack_hash": ""},
    )

    assert blocked.status_code == 403
    assert allowed.status_code == 200
    assert calls == [("adult-job", scope, "", token)]
    db = Database(_settings(tmp_path, "dashboard-secret").storage.db_path)
    try:
        application = db.get_application_for_owner("adult-job", scope)
        assert application is not None
        assert application["access_token_hash"] == hashlib.sha256(
            token.encode("utf-8")
        ).hexdigest()
        assert token not in str(application)
    finally:
        db.close()


def test_adult_scope_returns_three_sanitized_candidate_groups(tmp_path):
    app = _app(tmp_path)
    db = Database(_settings(tmp_path, "dashboard-secret").storage.db_path)
    db.init_schema()
    try:
        seed_adult_project(db)
        db.create_ai_provider_model(
            {
                "provider_id": 1,
                "model_key": "adult-model",
                "manual_capabilities": ["json"],
                "enabled": True,
            }
        )
        db.conn.execute(
            """
            UPDATE ai_adult_review_bindings
            SET binding_type = 'fixed', provider_id = 1, model = 'adult-model',
                model_pool_id = NULL, enabled = 1
            """
        )
        db.conn.commit()
    finally:
        db.close()
    client = app.test_client()
    _authenticate(client)

    response = client.post(
        "/api/dashboard/ai/polish/adult/scope",
        json={"agent_id": 7},
    )

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()["data"]
    assert set(payload["groups"]) == {"main", "safety", "fact_guard"}
    assert all(payload["groups"][kind] for kind in payload["groups"])
    assert len(payload["provider_scope_hash"]) == 64
    serialized = response.get_data(as_text=True)
    assert "api_key" not in serialized
    assert "provider_config_hash" not in serialized


def test_adult_scope_sanitizes_unexpected_router_error(tmp_path, monkeypatch):
    app = _app(tmp_path)
    db = Database(_settings(tmp_path, "dashboard-secret").storage.db_path)
    db.init_schema()
    try:
        seed_adult_project(db)
    finally:
        db.close()
    client = app.test_client()
    _authenticate(client)

    def fail_load(self, db, agent_id):
        raise RuntimeError("raw provider configuration must not reach the client")

    monkeypatch.setattr(AIWritingService, "_load_agent_config", fail_load)
    response = client.post(
        "/api/dashboard/ai/polish/adult/scope",
        json={"agent_id": 7},
    )

    assert response.status_code == 400
    assert "raw provider configuration" not in response.get_data(as_text=True)


def test_adult_character_routes_cover_project_scoped_crud(tmp_path):
    app = _app(tmp_path)
    db = Database(_settings(tmp_path, "dashboard-secret").storage.db_path)
    db.init_schema()
    try:
        project_id = db.create_ai_writing_project({"name": "characters", "settings": {}})
    finally:
        db.close()
    client = app.test_client()
    _authenticate(client)

    created = client.post(
        f"/api/dashboard/ai/projects/{project_id}/characters",
        json={
            "canonical_name": "安娜",
            "aliases": ["安"],
            "age_years": 25,
            "age_basis": "项目设定",
            "fictional": True,
        },
    )
    assert created.status_code == 200
    character = created.get_json()["data"]
    character_id = character["character_id"]
    listed = client.get(f"/api/dashboard/ai/projects/{project_id}/characters")
    updated = client.put(
        f"/api/dashboard/ai/projects/{project_id}/characters/{character_id}",
        json={"canonical_name": "安娜二", "expected_revision": character["revision"]},
    )
    deleted = client.delete(
        f"/api/dashboard/ai/projects/{project_id}/characters/{character_id}",
        json={"expected_revision": updated.get_json()["data"]["revision"]},
    )

    assert [item["character_id"] for item in listed.get_json()["data"]] == [
        character_id
    ]
    assert updated.status_code == 200
    assert updated.get_json()["data"]["canonical_name"] == "安娜二"
    assert deleted.status_code == 200
    assert deleted.get_json()["data"]["active"] is False


def test_adult_confirmation_routes_cover_versioned_get_and_put(tmp_path):
    app = _app(tmp_path)
    db = Database(_settings(tmp_path, "dashboard-secret").storage.db_path)
    db.init_schema()
    try:
        project_id = db.create_ai_writing_project({"name": "confirmation", "settings": {}})
        character = db.create_adult_character(
            {
                "project_id": project_id,
                "character_id": "11111111-1111-4111-8111-111111111111",
                "canonical_name": "安娜",
                "aliases": [],
                "age_years": 25,
                "age_basis": "项目设定",
                "fictional": True,
                "active": True,
            }
        )
    finally:
        db.close()
    client = app.test_client()
    _authenticate(client)

    before = client.get(
        f"/api/dashboard/ai/projects/{project_id}/adult-confirmation"
    )
    revision = before.get_json()["data"]["adult_confirmation_revision"]
    updated = client.put(
        f"/api/dashboard/ai/projects/{project_id}/adult-confirmation",
        json={
            "expected_revision": revision,
            "adult_content_enabled": True,
            "adult_characters_confirmed": True,
            "fictional_characters_confirmed": True,
            "character_ids": [character["character_id"]],
        },
    )

    assert before.status_code == 200
    assert updated.status_code == 200
    assert updated.get_json()["data"]["adult_content_enabled"] is True
    assert updated.get_json()["data"]["adult_confirmation_revision"] == revision + 1


def test_adult_review_binding_routes_require_owner_and_preserve_versions(tmp_path):
    denied = _app(tmp_path / "denied", dashboard_token=None).test_client()
    assert denied.get(
        "/api/dashboard/ai/adult-review-bindings/safety"
    ).status_code == 403

    app = _app(tmp_path / "allowed")
    client = app.test_client()
    _authenticate(client)
    current = client.get("/api/dashboard/ai/adult-review-bindings/safety")
    updated = client.put(
        "/api/dashboard/ai/adult-review-bindings/safety",
        json={"enabled": False, "expected_version": current.get_json()["data"]["version"]},
    )

    assert current.status_code == 200
    assert updated.status_code == 200
    assert updated.get_json()["data"]["version"] == current.get_json()["data"]["version"] + 1


def test_adult_regenerate_requires_parent_access_and_injects_parent_job(
    tmp_path,
    monkeypatch,
):
    app = _app(tmp_path)
    _project_id, _chapter_id, scope = _seed_adult_job(
        tmp_path,
        app,
        job_id="adult-parent-job-0001",
        status="succeeded",
        candidate="old candidate",
    )
    calls = []

    def fake_stream(self, payload, owner_scope, owner_token, **kwargs):
        calls.append((payload, owner_scope, owner_token))
        yield SimpleNamespace(type="metadata", text=None, data={"job_id": "adult-child"})
        yield SimpleNamespace(
            type="candidate",
            text="new candidate",
            data={"job_id": "adult-child", "applicable": True},
        )
        yield SimpleNamespace(type="done", text=None, data={"job_id": "adult-child"})

    monkeypatch.setattr(AIWritingService, "stream_adult_polish", fake_stream)
    client = app.test_client()
    _authenticate(client)
    payload = _configured_adult_payload(tmp_path, app, client)
    payload["idempotency_key"] = "adult-regenerate-0001"

    response = client.post(
        "/api/dashboard/ai/polish/adult/adult-parent-job-0001/regenerate",
        headers={
            "X-Adult-Access-Token": _access_token(
                app,
                "adult-parent-job-0001",
                scope,
            ),
        },
        json=payload,
        buffered=True,
    )

    assert response.status_code == 200, response.get_json()
    assert calls[0][0]["parent_job_id"] == "adult-parent-job-0001"
    assert calls[0][1] == scope
    assert b"event: candidate" in response.data
    assert b"new candidate" in response.data


def test_adult_regenerate_rejects_malformed_payload_before_stream(
    tmp_path,
    monkeypatch,
):
    app = _app(tmp_path)
    _project_id, _chapter_id, scope = _seed_adult_job(
        tmp_path,
        app,
        job_id="adult-parent-malformed",
        status="succeeded",
        candidate="old candidate",
    )
    calls = []

    def fake_stream(self, payload, owner_scope, owner_token, **kwargs):
        calls.append((payload, owner_scope, owner_token))
        return iter(())

    monkeypatch.setattr(AIWritingService, "stream_adult_polish", fake_stream)
    client = app.test_client()
    _authenticate(client)
    response = client.post(
        "/api/dashboard/ai/polish/adult/adult-parent-malformed/regenerate",
        headers={
            "X-Adult-Access-Token": _access_token(
                app,
                "adult-parent-malformed",
                scope,
            ),
        },
        json={"idempotency_key": "adult-regenerate-malformed"},
        buffered=True,
    )

    assert response.status_code == 422
    assert calls == []


def test_adult_stream_disconnect_cancels_only_running_owner_job(
    tmp_path,
    monkeypatch,
):
    app = _app(tmp_path)
    _project_id, _chapter_id, scope = _seed_adult_job(
        tmp_path,
        app,
        job_id="adult-disconnect",
        status="running",
    )
    monkeypatch.setattr(
        "pixiv_novel_sync.ai_web.secrets.token_urlsafe",
        lambda _length: "execution-owner-token",
    )
    closed = {"value": False}

    def fake_stream(self, payload, owner_scope, owner_token, **kwargs):
        try:
            yield SimpleNamespace(
                type="metadata",
                text=None,
                data={"job_id": "adult-disconnect"},
            )
            yield SimpleNamespace(type="progress", text=None, data={"phase": "main"})
        finally:
            closed["value"] = True

    monkeypatch.setattr(AIWritingService, "stream_adult_polish", fake_stream)
    client = app.test_client()
    _authenticate(client)
    payload = _configured_adult_payload(tmp_path, app, client)
    response = client.post(
        "/api/dashboard/ai/polish/adult/stream",
        json=payload,
        buffered=False,
    )
    assert b"event: metadata" in next(response.response)

    response.close()

    db = Database(_settings(tmp_path, "dashboard-secret").storage.db_path)
    try:
        assert db.get_adult_job("adult-disconnect", scope)["status"] == "cancelled"
    finally:
        db.close()
    assert closed["value"] is True


def test_adult_stream_disconnect_uses_execution_owner_token_cas(
    tmp_path,
    monkeypatch,
):
    app = _app(tmp_path)
    captured: dict[str, str] = {}
    cancel_calls: list[tuple[str, str, str]] = []

    def fake_stream(self, payload, owner_scope, owner_token, **kwargs):
        captured["owner_token"] = owner_token
        yield SimpleNamespace(
            type="metadata",
            text=None,
            data={"job_id": "adult-disconnect-cas"},
        )
        yield SimpleNamespace(type="progress", text=None, data={"phase": "main"})

    def fake_cancel(self, job_id, owner_scope, owner_token):
        cancel_calls.append((job_id, owner_scope, owner_token))
        return True

    monkeypatch.setattr(AIWritingService, "stream_adult_polish", fake_stream)
    monkeypatch.setattr(Database, "request_adult_job_cancel", fake_cancel)
    client = app.test_client()
    _authenticate(client)
    payload = _configured_adult_payload(tmp_path, app, client)
    response = client.post(
        "/api/dashboard/ai/polish/adult/stream",
        json=payload,
        buffered=False,
    )
    assert b"event: metadata" in next(response.response)

    response.close()

    assert cancel_calls == [
        (
            "adult-disconnect-cas",
            _owner_scope(app),
            captured["owner_token"],
        )
    ]


def test_adult_stream_transport_failure_cancels_without_leaking_error(
    tmp_path,
    monkeypatch,
):
    app = _app(tmp_path)
    _project_id, _chapter_id, scope = _seed_adult_job(
        tmp_path,
        app,
        job_id="adult-socket-failure",
        status="running",
    )
    monkeypatch.setattr(
        "pixiv_novel_sync.ai_web.secrets.token_urlsafe",
        lambda _length: "execution-owner-token",
    )

    def fake_stream(self, payload, owner_scope, owner_token, **kwargs):
        yield SimpleNamespace(
            type="metadata",
            text=None,
            data={"job_id": "adult-socket-failure"},
        )
        raise OSError("raw socket detail must not leak")

    monkeypatch.setattr(AIWritingService, "stream_adult_polish", fake_stream)
    client = app.test_client()
    _authenticate(client)
    payload = _configured_adult_payload(tmp_path, app, client)

    response = client.post(
        "/api/dashboard/ai/polish/adult/stream",
        json=payload,
        buffered=True,
    )

    assert response.status_code == 200
    assert b"raw socket detail" not in response.data
    assert b"event: error" in response.data
    db = Database(_settings(tmp_path, "dashboard-secret").storage.db_path)
    try:
        assert db.get_adult_job("adult-socket-failure", scope)["status"] == "cancelled"
    finally:
        db.close()


def test_adult_stream_initialization_failure_does_not_log_raw_exception(
    tmp_path,
    monkeypatch,
    caplog,
):
    app = _app(tmp_path)
    client = app.test_client()
    _authenticate(client)
    payload = _configured_adult_payload(tmp_path, app, client)

    def fail_parse(_payload):
        raise RuntimeError("raw provider response must not be logged")

    monkeypatch.setattr("pixiv_novel_sync.ai_web.parse_adult_request", fail_parse)
    with caplog.at_level("WARNING", logger="pixiv_novel_sync.ai_web"):
        response = client.post(
            "/api/dashboard/ai/polish/adult/stream",
            json=payload,
        )

    assert response.status_code == 400
    assert "raw provider response" not in response.get_data(as_text=True)
    assert "raw provider response" not in caplog.text


def test_adult_stream_sanitizes_provider_error_event(tmp_path, monkeypatch):
    def fake_stream(self, payload, owner_scope, owner_token, **kwargs):
        yield SimpleNamespace(
            type="metadata",
            text=None,
            data={"job_id": "adult-provider-error"},
        )
        yield SimpleNamespace(
            type="error",
            text=None,
            data={
                "job_id": "adult-provider-error",
                "code": "route_unavailable",
                "message": "raw provider response must not reach the client",
            },
        )

    monkeypatch.setattr(AIWritingService, "stream_adult_polish", fake_stream)
    app = _app(tmp_path)
    client = app.test_client()
    _authenticate(client)
    payload = _configured_adult_payload(tmp_path, app, client)

    response = client.post(
        "/api/dashboard/ai/polish/adult/stream",
        json=payload,
        buffered=True,
    )

    assert response.status_code == 200
    assert b"event: error" in response.data
    assert b"raw provider response" not in response.data


def test_adult_stream_late_disconnect_preserves_committed_candidate(
    tmp_path,
    monkeypatch,
):
    app = _app(tmp_path)
    _project_id, _chapter_id, scope = _seed_adult_job(
        tmp_path,
        app,
        job_id="adult-committed",
        status="succeeded",
        candidate="committed candidate",
    )

    def fake_stream(self, payload, owner_scope, owner_token, **kwargs):
        yield SimpleNamespace(
            type="metadata",
            text=None,
            data={"job_id": "adult-committed"},
        )
        yield SimpleNamespace(
            type="candidate",
            text="committed candidate",
            data={"job_id": "adult-committed"},
        )
        yield SimpleNamespace(type="done", text=None, data={"job_id": "adult-committed"})

    monkeypatch.setattr(AIWritingService, "stream_adult_polish", fake_stream)
    client = app.test_client()
    _authenticate(client)
    payload = _configured_adult_payload(tmp_path, app, client)
    response = client.post(
        "/api/dashboard/ai/polish/adult/stream",
        json=payload,
        buffered=False,
    )
    next(response.response)
    assert b"event: candidate" in next(response.response)

    response.close()

    db = Database(_settings(tmp_path, "dashboard-secret").storage.db_path)
    try:
        job = db.get_adult_job("adult-committed", scope)
        assert job["status"] == "succeeded"
        assert job["output_text"] == "committed candidate"
    finally:
        db.close()


def test_adult_stream_maps_malformed_payload_to_422(tmp_path):
    app = _app(tmp_path)
    client = app.test_client()
    _authenticate(client)

    response = client.post(
        "/api/dashboard/ai/polish/adult/stream",
        json={"target_text": "must not be accepted"},
    )

    assert response.status_code == 422
    assert "must not be accepted" not in response.get_data(as_text=True)


def test_adult_stream_runs_complete_prepare_before_opening_sse(
    tmp_path,
    monkeypatch,
):
    app = _app(tmp_path)
    client = app.test_client()
    _authenticate(client)
    payload = _configured_adult_payload(tmp_path, app, client)
    def fail_prepare(self, payload, owner_scope, *, owner_token):
        raise AIConflictError("409: participant snapshot changed")

    monkeypatch.setattr(AIWritingService, "prepare_adult_job", fail_prepare)

    response = client.post(
        "/api/dashboard/ai/polish/adult/stream",
        json=payload,
        buffered=True,
    )

    assert response.status_code == 409


def test_adult_stream_maps_stale_chapter_and_provider_scope_to_409(tmp_path):
    app = _app(tmp_path)
    db = Database(_settings(tmp_path, "dashboard-secret").storage.db_path)
    db.init_schema()
    try:
        seed_adult_project(db)
        db.create_ai_provider_model(
            {
                "provider_id": 1,
                "model_key": "adult-model",
                "manual_capabilities": ["json"],
                "enabled": True,
            }
        )
        db.conn.execute(
            """
            UPDATE ai_adult_review_bindings
            SET binding_type = 'fixed', provider_id = 1, model = 'adult-model',
                model_pool_id = NULL, enabled = 1
            """
        )
        db.conn.commit()
    finally:
        db.close()
    client = app.test_client()
    _authenticate(client)

    stale_chapter = client.post(
        "/api/dashboard/ai/polish/adult/stream",
        json=valid_adult_payload(chapter_revision=9),
    )
    stale_scope = client.post(
        "/api/dashboard/ai/polish/adult/stream",
        json=valid_adult_payload(provider_scope_hash="0" * 64),
    )

    assert stale_chapter.status_code == 409
    assert stale_scope.status_code == 409
