from __future__ import annotations

from types import SimpleNamespace

import pytest

from pixiv_novel_sync.ai.service import AIWritingService, AIServiceError
from pixiv_novel_sync.ai.crypto import AISecretManager
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


class _FakeSecretManager(AISecretManager):
    def decrypt(self, ciphertext):
        del ciphertext
        return "decrypted-key"


class _GuardedSecretManager(AISecretManager):
    """Fail if probe borrows a key when the network tuple is not intact."""

    def decrypt(self, ciphertext):
        raise AssertionError("saved key was decrypted after an unsafe tuple change")


def test_probe_forwards_proxy_to_the_provider(tmp_path, monkeypatch) -> None:
    seen = {}

    def capture(config):
        seen["proxy"] = config.proxy
        return _FakeProvider()

    service = AIWritingService(db_path=tmp_path / "probe-proxy.db")
    monkeypatch.setattr("pixiv_novel_sync.ai.services.admin.create_provider", capture)
    service.probe_provider_models({
        "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-test",
        "proxy": "http://127.0.0.1:7890",
    })
    assert seen["proxy"] == "http://127.0.0.1:7890"


def test_saved_model_key_rejects_control_characters(tmp_path) -> None:
    service = AIWritingService(db_path=tmp_path / "model-key.db")
    with pytest.raises(AIServiceError, match="控制字符"):
        service._normalize_provider_payload(
            {
                "name": "网关",
                "provider_type": "openai_compatible",
                "default_model": "bad\nmodel",
            }
        )
    kept = service._normalize_agent_payload({"model": "grok-4"}, partial=True)
    assert kept["model"] == "grok-4"


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


def test_probe_borrowed_key_requires_the_saved_network_tuple(tmp_path) -> None:
    db = Database(tmp_path / "probe-tuple.db")
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "p",
        "provider_type": "anthropic",
        "base_url": "https://api.example.com",
        "proxy": "http://127.0.0.1:7890",
        "api_key_encrypted": "cipher",
        "default_model": None,
        "enabled": 1,
    })
    db.close()
    service = AIWritingService(db_path=tmp_path / "probe-tuple.db")

    for changed in (
        {"provider_type": "openai_compatible"},
        {"base_url": None},
        {"proxy": "http://127.0.0.1:9999"},
    ):
        payload = {
            "provider_id": provider_id,
            "provider_type": "anthropic",
            "base_url": "https://api.example.com",
            "proxy": "http://127.0.0.1:7890",
        }
        payload.update(changed)
        with pytest.raises(AIServiceError) as excinfo:
            service.probe_provider_models(payload)
        assert "地址、协议或代理" in str(excinfo.value)


def test_probe_null_urls_reject_provider_type_change_before_key_use(
    tmp_path, monkeypatch,
) -> None:
    db = Database(tmp_path / "probe-null-url.db")
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "p",
        "provider_type": "anthropic",
        "base_url": None,
        "api_key_encrypted": "cipher",
        "default_model": None,
        "enabled": 1,
    })
    db.close()
    service = AIWritingService(
        db_path=tmp_path / "probe-null-url.db",
        secret_manager=_GuardedSecretManager(),
    )

    def fail_provider_creation(config):
        raise AssertionError("Provider was constructed after an unsafe tuple change")

    monkeypatch.setattr(
        "pixiv_novel_sync.ai.services.admin.create_provider", fail_provider_creation
    )
    with pytest.raises(AIServiceError) as excinfo:
        service.probe_provider_models({
            "provider_id": provider_id,
            "provider_type": "openai_compatible",
            "base_url": None,
        })

    assert "地址、协议或代理" in str(excinfo.value)


def test_probe_borrowed_key_accepts_the_unchanged_network_tuple(
    tmp_path, monkeypatch,
) -> None:
    db = Database(tmp_path / "probe-same.db")
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "p",
        "provider_type": "anthropic",
        "base_url": "https://api.example.com",
        "proxy": "http://127.0.0.1:7890",
        "api_key_encrypted": "cipher",
        "default_model": None,
        "enabled": 1,
    })
    db.close()
    service = AIWritingService(
        db_path=tmp_path / "probe-same.db",
        secret_manager=_FakeSecretManager(),
    )
    created = {}

    def capture(config):
        created["config"] = config
        return _FakeProvider()

    monkeypatch.setattr(
        "pixiv_novel_sync.ai.services.admin.create_provider", capture
    )
    result = service.probe_provider_models({
        "provider_id": provider_id,
        "provider_type": "anthropic",
        "base_url": "https://api.example.com",
        "proxy": "http://127.0.0.1:7890",
    })

    assert result["complete"] is True
    assert created["config"].api_key == "decrypted-key"
    assert created["config"].proxy == "http://127.0.0.1:7890"


def test_probe_new_key_can_use_a_new_network_tuple(tmp_path, monkeypatch) -> None:
    db = Database(tmp_path / "probe-new-key.db")
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "p",
        "provider_type": "openai_compatible",
        "base_url": "https://old.example.com/v1",
        "api_key_encrypted": "cipher",
        "default_model": None,
        "enabled": 1,
    })
    db.close()
    service = AIWritingService(db_path=tmp_path / "probe-new-key.db")
    created = {}

    def capture(config):
        created["config"] = config
        return _FakeProvider()

    monkeypatch.setattr(
        "pixiv_novel_sync.ai.services.admin.create_provider", capture
    )
    service.probe_provider_models({
        "provider_id": provider_id,
        "provider_type": "anthropic",
        "base_url": "https://new.example.com",
        "proxy": "http://127.0.0.1:9999",
        "api_key": "sk-new",
    })

    assert created["config"].api_key == "sk-new"
    assert created["config"].provider_type == "anthropic"


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
