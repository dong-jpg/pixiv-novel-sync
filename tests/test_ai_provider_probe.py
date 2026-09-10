from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.ai.service import AIWritingService, AIServiceError
from pixiv_novel_sync.storage_db import Database


class _FakeProvider:
    closed = False

    def list_models(self, **kwargs):
        return SimpleNamespace(
            models=[
                {"model_key": "glm-4.7", "display_name": "GLM 4.7",
                 "capabilities": ["chat"], "context_window": 200000},
                {"model_key": "bge-m3", "display_name": "BGE m3",
                 "capabilities": ["embedding"], "context_window": 8192},
                {"model_key": "mystery", "display_name": None,
                 "capabilities": [], "context_window": None},
            ],
            complete=True, partial_reason=None,
        )

    def close(self):
        type(self).closed = True


def test_probe_marks_chat_and_unlabelled_models_as_suggested(tmp_path, monkeypatch) -> None:
    """能力标签缺失时默认勾上：标签来自上游、不保证齐全，宁可多勾也别让用户以为没获取到。"""
    service = AIWritingService(db_path=tmp_path / "probe.db")
    monkeypatch.setattr(
        "pixiv_novel_sync.ai.services.admin.create_provider", lambda config: _FakeProvider()
    )

    result = service.probe_provider_models({
        "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-test",
    })

    suggested = {item["model_key"]: item["suggested"] for item in result["items"]}
    assert suggested == {"glm-4.7": True, "bge-m3": False, "mystery": True}
    assert _FakeProvider.closed is True


def test_probe_refuses_to_borrow_stored_key_for_a_different_address(tmp_path) -> None:
    """否则这个端点就是「把加密存好的 Key 发到我指定的任意地址」。"""
    db = Database(tmp_path / "probe.db")
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "p", "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1", "api_key_encrypted": "cipher",
        "default_model": None, "enabled": 1,
    })
    db.close()
    service = AIWritingService(db_path=tmp_path / "probe.db")

    with pytest.raises(AIServiceError) as excinfo:
        service.probe_provider_models({
            "provider_id": provider_id,
            "provider_type": "openai_compatible",
            "base_url": "https://attacker.example.com/v1",
        })

    assert "不能同时改地址" in str(excinfo.value)


def test_probe_writes_nothing_to_the_catalog(tmp_path, monkeypatch) -> None:
    db = Database(tmp_path / "probe.db")
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "p", "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1", "api_key_encrypted": "cipher",
        "default_model": None, "enabled": 1,
    })
    before = db.list_ai_provider_models(provider_id)["total"]
    db.close()
    service = AIWritingService(db_path=tmp_path / "probe.db")
    monkeypatch.setattr(
        "pixiv_novel_sync.ai.services.admin.create_provider", lambda config: _FakeProvider()
    )

    service.probe_provider_models({
        "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-test",
    })

    db = Database(tmp_path / "probe.db")
    assert db.list_ai_provider_models(provider_id)["total"] == before
    assert db.get_ai_provider(provider_id)["models_synced_at"] is None
    db.close()