from __future__ import annotations

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


# A7：借用已保存的 Key 时，除 base_url 外 provider_type 与 proxy 也决定 Key 发去
# 哪里、以什么头发出去。旧代码只比对 base_url，两边 base_url 都为空时改
# provider_type，会把 Anthropic Key 当 Bearer 发到 OpenAI 默认地址。
_BORROW_SECRET_ENV = "PIXIV_NOVEL_SYNC_AI_SECRET_KEY"


def _seed_saved_key_provider(tmp_path, monkeypatch, **overrides) -> int:
    from pixiv_novel_sync.ai.crypto import AISecretManager

    monkeypatch.setenv(_BORROW_SECRET_ENV, "probe-borrow-secret")
    db = Database(tmp_path / "probe-borrow.db")
    db.init_schema()
    data = {
        "name": "saved", "provider_type": "anthropic", "base_url": None,
        "api_key_encrypted": AISecretManager().encrypt("sk-saved"),
        "default_model": None, "enabled": 1,
    }
    data.update(overrides)
    provider_id = db.create_ai_provider(data)
    db.close()
    return provider_id


def _capture_created_providers(monkeypatch) -> list:
    seen: list = []

    def capture(config):
        seen.append(config)
        return _FakeProvider()

    monkeypatch.setattr("pixiv_novel_sync.ai.services.admin.create_provider", capture)
    return seen


def test_probe_refuses_to_borrow_stored_key_for_a_different_provider_type(tmp_path, monkeypatch) -> None:
    provider_id = _seed_saved_key_provider(tmp_path, monkeypatch)
    seen = _capture_created_providers(monkeypatch)
    service = AIWritingService(db_path=tmp_path / "probe-borrow.db")
    try:
        with pytest.raises(AIServiceError, match="重新填入 Key"):
            service.probe_provider_models({
                "provider_id": provider_id,
                "provider_type": "openai_compatible",
            })
    finally:
        service.close()

    assert seen == []


def test_probe_refuses_to_borrow_stored_key_through_a_different_proxy(tmp_path, monkeypatch) -> None:
    provider_id = _seed_saved_key_provider(
        tmp_path, monkeypatch,
        provider_type="openai_compatible",
        base_url="https://api.example.com/v1",
        proxy="http://corp-proxy.example:3128",
    )
    seen = _capture_created_providers(monkeypatch)
    service = AIWritingService(db_path=tmp_path / "probe-borrow.db")
    try:
        # 换成别的代理，或显式不走代理，都不借用已保存的 Key
        for proxy in ("http://attacker.example:8080", ""):
            with pytest.raises(AIServiceError, match="重新填入 Key"):
                service.probe_provider_models({
                    "provider_id": provider_id,
                    "provider_type": "openai_compatible",
                    "base_url": "https://api.example.com/v1",
                    "proxy": proxy,
                })
    finally:
        service.close()

    assert seen == []


def test_probe_borrowing_stored_key_inherits_saved_type_and_proxy(tmp_path, monkeypatch) -> None:
    """前端探测只发 provider_type / base_url：没传的字段按已保存配置补齐，Key 走它平时走的路径。"""
    provider_id = _seed_saved_key_provider(
        tmp_path, monkeypatch,
        provider_type="openai_compatible",
        base_url="https://api.example.com/v1",
        proxy="http://corp-proxy.example:3128",
    )
    seen = _capture_created_providers(monkeypatch)
    service = AIWritingService(db_path=tmp_path / "probe-borrow.db")
    try:
        service.probe_provider_models({
            "provider_id": provider_id,
            "provider_type": "openai_compatible",
            "base_url": "https://api.example.com/v1",
        })
        service.probe_provider_models({
            "provider_id": provider_id,
            "base_url": "https://api.example.com/v1",
            "proxy": "http://corp-proxy.example:3128",
        })
    finally:
        service.close()

    assert [(config.provider_type, config.proxy, config.api_key) for config in seen] == [
        ("openai_compatible", "http://corp-proxy.example:3128", "sk-saved"),
        ("openai_compatible", "http://corp-proxy.example:3128", "sk-saved"),
    ]
