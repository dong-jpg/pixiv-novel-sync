from __future__ import annotations

import json
import uuid

import pytest

from pixiv_novel_sync.ai.services.core import AIServiceCore, AIServiceError
from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "adult-characters.db")
    database.init_schema()
    project_id = database.create_ai_writing_project(
        {
            "name": "角色项目",
            "description": "项目事实",
            "outline": {"arc": "主线"},
            "settings": {"world": "虚构世界"},
        }
    )
    assert project_id == 1
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def service(db):
    from pixiv_novel_sync.ai.services.adult import AIAdultPolishMixin
    from pixiv_novel_sync.ai.services.admin import AIAdminMixin
    from pixiv_novel_sync.ai.services.projects import AIProjectsMixin

    class AdultCharacterService(
        AIAdultPolishMixin,
        AIProjectsMixin,
        AIAdminMixin,
        AIServiceCore,
    ):
        pass

    instance = AdultCharacterService(db.path)
    try:
        yield instance
    finally:
        instance.close()


def _character_payload(name: str = "安娜", **overrides):
    payload = {
        "canonical_name": name,
        "aliases": [name[:1]],
        "age_years": 25,
        "age_basis": "项目设定",
        "fictional": True,
    }
    payload.update(overrides)
    return payload


def _confirmation_payload(*character_ids: str):
    return {
        "adult_content_enabled": True,
        "adult_characters_confirmed": True,
        "fictional_characters_confirmed": True,
        "character_ids": list(character_ids),
    }


def test_character_id_is_server_generated_and_revision_is_cas(db, service):
    row = service.create_adult_character(1, _character_payload())

    parsed_id = uuid.UUID(row["character_id"])
    assert parsed_id.version == 4
    assert str(parsed_id) == row["character_id"]
    assert row["revision"] == 1

    with pytest.raises(AIServiceError, match="revision"):
        service.update_adult_character(
            row["character_id"],
            {"canonical_name": "错误更新"},
            expected_revision=2,
        )

    updated = service.update_adult_character(
        row["character_id"],
        {"canonical_name": "安娜二"},
        expected_revision=1,
    )
    assert updated["canonical_name"] == "安娜二"
    assert updated["revision"] == 2


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"canonical_name": ""}, "名称"),
        ({"canonical_name": "名" * 201}, "200"),
        ({"aliases": ["别名"] * 33}, "32"),
        ({"aliases": ["别" * 101]}, "100"),
        ({"age_years": -1}, "年龄"),
        ({"age_basis": "  "}, "年龄依据"),
        ({"fictional": 1}, "fictional"),
    ],
)
def test_character_fields_are_strictly_bounded(service, payload, message):
    with pytest.raises(AIServiceError, match=message):
        service.create_adult_character(1, _character_payload(**payload))


@pytest.mark.parametrize(
    "character, message",
    [
        ({"age_years": 17}, "18"),
        ({"age_years": None}, "年龄"),
        ({"fictional": False}, "虚构"),
    ],
)
def test_confirmation_rejects_minor_unknown_age_or_real_character(
    service,
    character,
    message,
):
    row = service.create_adult_character(1, _character_payload(**character))
    current = service.get_adult_confirmation(1)

    with pytest.raises(AIServiceError, match=message):
        service.update_adult_confirmation(
            1,
            _confirmation_payload(row["character_id"]),
            expected_revision=current["adult_confirmation_revision"],
        )


def test_confirmation_is_sorted_hashed_and_invalidated_by_character_change(
    db,
    service,
):
    second = service.create_adult_character(1, _character_payload("周岚"))
    first = service.create_adult_character(1, _character_payload("安娜"))
    current = service.get_adult_confirmation(1)

    confirmed = service.update_adult_confirmation(
        1,
        _confirmation_payload(second["character_id"], first["character_id"]),
        expected_revision=current["adult_confirmation_revision"],
    )

    stored_ids = [item["character_id"] for item in confirmed["adult_characters"]]
    assert stored_ids == sorted(stored_ids)
    assert all(item["character_revision"] == 1 for item in confirmed["adult_characters"])
    assert len(confirmed["adult_characters_hash"]) == 64
    assert confirmed["adult_characters_confirmed"] is True
    assert confirmed["fictional_characters_confirmed"] is True

    updated = service.update_adult_character(
        first["character_id"],
        {"aliases": ["安", "娜娜"]},
        expected_revision=1,
    )
    invalidated = service.get_adult_confirmation(1)
    assert updated["revision"] == 2
    assert invalidated["adult_characters_confirmed"] is False
    assert invalidated["fictional_characters_confirmed"] is False
    assert invalidated["adult_confirmation_revision"] == (
        confirmed["adult_confirmation_revision"] + 1
    )

    raw = db.conn.execute(
        "SELECT adult_characters_json FROM ai_writing_projects WHERE id = 1"
    ).fetchone()[0]
    assert isinstance(json.loads(raw), list)


def test_confirmation_rejects_duplicate_unknown_and_inactive_ids(service):
    row = service.create_adult_character(1, _character_payload())
    current = service.get_adult_confirmation(1)
    with pytest.raises(AIServiceError, match="重复"):
        service.update_adult_confirmation(
            1,
            _confirmation_payload(row["character_id"], row["character_id"]),
            expected_revision=current["adult_confirmation_revision"],
        )

    with pytest.raises(AIServiceError, match="不存在"):
        service.update_adult_confirmation(
            1,
            _confirmation_payload("33333333-3333-4333-8333-333333333333"),
            expected_revision=current["adult_confirmation_revision"],
        )

    deactivated = service.deactivate_adult_character(
        row["character_id"],
        expected_revision=1,
    )
    assert deactivated["active"] is False
    assert service.list_adult_characters(1) == []
    current = service.get_adult_confirmation(1)
    with pytest.raises(AIServiceError, match="不可用"):
        service.update_adult_confirmation(
            1,
            _confirmation_payload(row["character_id"]),
            expected_revision=current["adult_confirmation_revision"],
        )


def test_chapter_revision_increments_once_for_each_mutation_path(db):
    chapter_id = db.create_ai_chapter(
        {"project_id": 1, "chapter_number": 1, "content": "甲"}
    )
    assert db.get_ai_chapter(chapter_id)["chapter_revision"] == 0

    db.update_ai_chapter(chapter_id, {"content": "乙", "summary": "摘要"})
    assert db.get_ai_chapter(chapter_id)["chapter_revision"] == 1

    db.patch_ai_chapter_metadata(chapter_id, {"style": "x"})
    assert db.get_ai_chapter(chapter_id)["chapter_revision"] == 2

    db.update_ai_chapters_outlines_and_metadata(
        [{"id": chapter_id, "outline": "提纲", "metadata": {"style": "y"}}]
    )
    assert db.get_ai_chapter(chapter_id)["chapter_revision"] == 3

    db.update_ai_chapter(chapter_id, {})
    assert db.get_ai_chapter(chapter_id)["chapter_revision"] == 3


def test_project_facts_snapshot_is_stable_sorted_and_excludes_chapter_text(
    db,
    service,
):
    from pixiv_novel_sync.ai.services.adult import build_project_facts_snapshot

    service.create_adult_character(1, _character_payload("周岚"))
    service.create_adult_character(1, _character_payload("安娜"))
    db.upsert_ai_project_state(1, "world_state", "雨夜")
    db.upsert_ai_project_state(1, "character_state", "同行")
    db.create_ai_foreshadow(
        {
            "project_id": 1,
            "description": "旧信件",
            "planted_chapter": 1,
            "target_resolve_chapter": 3,
        }
    )
    db.create_ai_chapter(
        {"project_id": 1, "chapter_number": 1, "content": "绝不能进入事实快照的正文"}
    )

    snapshot, digest = build_project_facts_snapshot(db, 1)
    repeated, repeated_digest = build_project_facts_snapshot(db, 1)

    assert snapshot == repeated
    assert digest == repeated_digest
    assert len(digest) == 64
    assert [row["canonical_name"] for row in snapshot["characters"]] == sorted(
        ["周岚", "安娜"]
    )
    assert [row["state_type"] for row in snapshot["states"]] == [
        "character_state",
        "world_state",
    ]
    assert "绝不能进入事实快照的正文" not in json.dumps(
        snapshot,
        ensure_ascii=False,
    )
