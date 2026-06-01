from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from flask import Flask

import pixiv_novel_sync.ai_web as ai_web


class FakeService:
    def __init__(self, _db_path: Path) -> None:
        self.calls: list[tuple[str, dict]] = []

    def list_drafts(self, *, page: int, page_size: int):
        self.calls.append(("list_drafts", {"page": page, "page_size": page_size}))
        return {"items": [], "page": page, "page_size": page_size}

    def list_jobs(self, *, task_type=None, status=None, page: int, page_size: int):
        self.calls.append(("list_jobs", {"page": page, "page_size": page_size}))
        return {"items": [], "page": page, "page_size": page_size}

    def list_style_profiles(self, *, page: int, page_size: int):
        self.calls.append(("list_style_profiles", {"page": page, "page_size": page_size}))
        return {"items": [], "page": page, "page_size": page_size}

    def list_novel_profiles(self, *, page: int, page_size: int):
        self.calls.append(("list_novel_profiles", {"page": page, "page_size": page_size}))
        return {"items": [], "page": page, "page_size": page_size}

    def seed_builtin_agents(self, provider_id: int):
        self.calls.append(("seed_builtin_agents", {"provider_id": provider_id}))
        return {"created": 0}

    def search_project_context(self, project_id: int, query: str, top_k: int):
        self.calls.append(("search_project_context", {"project_id": project_id, "query": query, "top_k": top_k}))
        return []


class FakeDatabase:
    def __init__(self, _db_path: Path) -> None:
        self.conn = SimpleNamespace(execute=self.execute)

    def init_schema(self) -> None:
        pass

    def execute(self, *_args, **_kwargs):
        return SimpleNamespace(fetchall=lambda: [])

    def close(self) -> None:
        pass


def make_client(monkeypatch, tmp_path):
    services: list[FakeService] = []

    def factory(db_path: Path):
        service = FakeService(db_path)
        services.append(service)
        return service

    monkeypatch.setattr(ai_web, "AIWritingService", factory)
    app = Flask(__name__, template_folder=str(tmp_path))
    settings = SimpleNamespace(storage=SimpleNamespace(db_path=tmp_path / "test.db"))
    ai_web.register_ai_routes(app, settings)
    return app.test_client(), services[0]


def test_list_drafts_invalid_pagination_returns_business_error(monkeypatch, tmp_path):
    client, _service = make_client(monkeypatch, tmp_path)

    response = client.get("/api/dashboard/ai/drafts?page=bad&page_size=bad")

    assert response.status_code == 400
    assert response.get_json()["error"] == "page 必须是整数"


def test_list_jobs_rejects_out_of_range_pagination(monkeypatch, tmp_path):
    client, _service = make_client(monkeypatch, tmp_path)

    response = client.get("/api/dashboard/ai/jobs?page=-5&page_size=9999")

    assert response.status_code == 400
    assert response.get_json()["error"] == "page 不能小于 1"


def test_list_style_profiles_uses_safe_pagination(monkeypatch, tmp_path):
    client, service = make_client(monkeypatch, tmp_path)

    response = client.get("/api/dashboard/ai/style-profiles?page=2&page_size=30")

    assert response.status_code == 200
    assert service.calls[-1] == ("list_style_profiles", {"page": 2, "page_size": 30})


def test_list_novel_profiles_rejects_invalid_pagination(monkeypatch, tmp_path):
    client, _service = make_client(monkeypatch, tmp_path)

    response = client.get("/api/dashboard/ai/novel-profiles?page=bad")

    assert response.status_code == 400
    assert response.get_json()["error"] == "page 必须是整数"


def test_seed_builtin_agents_invalid_provider_id_returns_business_error(monkeypatch, tmp_path):
    client, _service = make_client(monkeypatch, tmp_path)

    response = client.post("/api/dashboard/ai/agents/seed", json={"provider_id": "abc"})

    assert response.status_code == 400
    assert response.get_json()["error"] == "provider_id 必须是整数"


def test_search_project_uses_safe_top_k(monkeypatch, tmp_path):
    client, service = make_client(monkeypatch, tmp_path)

    response = client.get("/api/dashboard/ai/projects/1/search?q=hello&top_k=7")

    assert response.status_code == 200
    assert service.calls[-1] == ("search_project_context", {"project_id": 1, "query": "hello", "top_k": 7})


def test_search_series_invalid_limit_returns_business_error(monkeypatch, tmp_path):
    client, _service = make_client(monkeypatch, tmp_path)
    monkeypatch.setattr(ai_web, "Database", FakeDatabase, raising=False)

    response = client.get("/api/dashboard/ai/series/search?limit=bad")

    assert response.status_code == 400
    assert response.get_json()["error"] == "limit 必须是整数"
