from __future__ import annotations

import pytest

from pixiv_novel_sync.ai.preference_context import (
    build_preference_context,
    inject_preference_context,
    normalize_preference_strength,
)
from pixiv_novel_sync.ai.services.core import AIServiceCore, AIServiceError
from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def profile() -> dict[str, object]:
    return {
        "id": 7,
        "description": "备用描述",
        "profile": {
            "summary": "偏好温柔、克制且重视人物关系的叙事。",
            "positive_preferences": {
                "tags": ["百合", "治愈", "校园", "成长"],
                "keywords": ["相互扶持", "细腻对话"],
                "themes": ["成长"],
                "scenes_or_situations": ["雨夜重逢"],
                "narrative_patterns": ["双视角推进"],
            },
            "negative_preferences": {
                "excluded_tags": ["猎奇"],
                "excluded_keywords": ["强制反转"],
                "avoid_themes": ["无铺垫背叛"],
            },
            "sample_texts": ["sample text 不得进入 Prompt"],
        },
        "stats": {"sample_text": "sample text"},
    }


@pytest.mark.parametrize(
    ("strength", "expected"),
    [
        ("off", None),
        ("light", "偏好标签"),
        ("standard", "负向排除"),
        ("strong", "叙事偏好"),
    ],
)
def test_preference_context_strengths(
    profile: dict[str, object],
    strength: str,
    expected: str | None,
) -> None:
    result = build_preference_context(profile, strength)

    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert expected in result
        assert "sample text" not in result


def test_preference_context_is_bounded_by_strength_budget(
    profile: dict[str, object],
) -> None:
    profile_data = profile["profile"]
    assert isinstance(profile_data, dict)
    positive = profile_data["positive_preferences"]
    assert isinstance(positive, dict)
    positive["tags"] = [f"标签{i}" for i in range(30)]

    context = build_preference_context(profile, "strong")

    assert context is not None
    assert "标签15" in context
    assert "标签16" not in context


def test_empty_preference_profile_does_not_inject_an_empty_block() -> None:
    assert build_preference_context({"profile": {}}, "strong") is None


def test_normalize_preference_strength_is_conservative() -> None:
    assert normalize_preference_strength(" STRONG ") == "strong"
    assert normalize_preference_strength("unknown") == "off"
    assert normalize_preference_strength(None) == "off"


def test_inject_preference_context_does_not_mutate_messages() -> None:
    messages = [
        {"role": "system", "content": "固定事实约束"},
        {"role": "user", "content": "开始创作"},
    ]
    original = [dict(message) for message in messages]

    injected = inject_preference_context(messages, "【用户偏好画像】\n- 偏好标签：治愈")

    assert messages == original
    assert injected is not messages
    assert [message["role"] for message in injected] == ["system", "system", "user"]
    assert injected[0]["content"] == "固定事实约束"
    assert "用户偏好画像" in injected[1]["content"]


def test_resolver_uses_project_defaults_and_request_overrides(tmp_path) -> None:
    db = Database(tmp_path / "prefs.db")
    db.init_schema()
    project_profile_id = db.create_preference_profile(
        {
            "name": "项目画像",
            "source_scope": {},
            "stats": {},
            "profile": {
                "summary": "项目默认",
                "positive_preferences": {"tags": ["项目标签"]},
            },
        }
    )
    request_profile_id = db.create_preference_profile(
        {
            "name": "单次画像",
            "source_scope": {},
            "stats": {},
            "profile": {
                "summary": "单次覆盖",
                "positive_preferences": {"tags": ["请求标签"]},
                "negative_preferences": {"excluded_tags": ["排除标签"]},
            },
        }
    )
    project = {
        "preference_profile_id": project_profile_id,
        "preference_injection_strength": "light",
    }

    default_result = AIServiceCore._resolve_preference_context(db, {}, project)
    override_result = AIServiceCore._resolve_preference_context(
        db,
        {
            "preference_profile_id": request_profile_id,
            "preference_injection_strength": "standard",
        },
        project,
    )

    assert default_result[:2] == (project_profile_id, "light")
    assert default_result[2] is not None and "项目标签" in default_result[2]
    assert override_result[:2] == (request_profile_id, "standard")
    assert override_result[2] is not None and "请求标签" in override_result[2]
    db.close()


def test_resolver_rejects_missing_profile_and_invalid_strength(tmp_path) -> None:
    db = Database(tmp_path / "prefs.db")
    db.init_schema()

    with pytest.raises(AIServiceError, match="偏好画像不存在"):
        AIServiceCore._resolve_preference_context(
            db,
            {"preference_profile_id": 999, "preference_injection_strength": "light"},
        )
    with pytest.raises(AIServiceError, match="偏好注入强度"):
        AIServiceCore._resolve_preference_context(
            db,
            {"preference_profile_id": 999, "preference_injection_strength": "maximum"},
        )

    assert AIServiceCore._resolve_preference_context(db, {}) == (None, "off", None)
    db.close()
