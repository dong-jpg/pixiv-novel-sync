from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from pixiv_novel_sync.ai.service import AIServiceError, AIWritingService
from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database(Path(os.environ["PIXIV_DB_PATH"]))
    database.init_schema()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def service(db: Database) -> AIWritingService:
    return AIWritingService(db.path)


def _create_project(db: Database, name: str = "测试项目") -> int:
    return db.create_ai_writing_project({"name": name, "settings": {}})


def _create_session(db: Database) -> int:
    return db.create_ai_chat_session({
        "scope": "wizard",
        "title": "测试向导会话",
        "metadata": {"stage": "ready"},
    })


def _database_snapshot(db: Database) -> dict[str, list[tuple[object, ...]]]:
    tables = (
        "ai_writing_projects",
        "ai_chapters",
        "ai_foreshadows",
        "ai_chat_sessions",
    )
    return {
        table: [
            tuple(row)
            for row in db.conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        ]
        for table in tables
    }


def _seed_wizard_data(db: Database) -> tuple[int, int]:
    project_id = _create_project(db, "原项目")
    db.create_ai_chapter({
        "project_id": project_id,
        "chapter_number": 1,
        "title": "原章节",
    })
    db.create_ai_foreshadow({
        "project_id": project_id,
        "description": "原伏笔",
        "planted_chapter": 1,
    })
    return project_id, _create_session(db)


def test_parse_and_save_state_persists_all_sections(
    db: Database,
    service: AIWritingService,
) -> None:
    project_id = _create_project(db)
    output = """=== character_state ===
角色保持警惕
=== plot_progress ===
调查进入旧宅
=== new_foreshadows ===
- 门后的脚步声 | high
- 缺页的日记 | low
"""

    service._parse_and_save_state(
        db,
        project_id,
        {"chapter_number": 7},
        output,
    )

    assert db.get_all_project_states(project_id) == {
        "character_state": "角色保持警惕",
        "plot_progress": "调查进入旧宅",
    }
    foreshadows = db.list_ai_foreshadows(project_id)
    assert [item["description"] for item in foreshadows] == [
        "门后的脚步声",
        "缺页的日记",
    ]
    assert [item["importance"] for item in foreshadows] == ["high", "low"]
    assert {item["planted_chapter"] for item in foreshadows} == {7}


def test_parse_and_save_state_limits_foreshadows_across_repeated_sections(
    db: Database,
    service: AIWritingService,
) -> None:
    project_id = _create_project(db)
    first_section = "\n".join(f"- 第一段伏笔 {index}" for index in range(150))
    second_section = "\n".join(f"- 第二段伏笔 {index}" for index in range(150))
    output = (
        f"=== new_foreshadows ===\n{first_section}\n"
        f"=== new_foreshadows ===\n{second_section}"
    )

    service._parse_and_save_state(
        db,
        project_id,
        {"chapter_number": 8},
        output,
    )

    assert len(db.list_ai_foreshadows(project_id)) == 200


def test_parse_and_save_state_rolls_back_states_when_foreshadow_write_fails(
    db: Database,
    service: AIWritingService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = _create_project(db)
    db.upsert_ai_project_state(project_id, "character_state", "原角色状态")
    db.upsert_ai_project_state(project_id, "plot_progress", "原剧情状态")

    def fail_create_foreshadow(_payload: dict[str, object]) -> int:
        raise RuntimeError("模拟伏笔写入失败")

    monkeypatch.setattr(db, "create_ai_foreshadow", fail_create_foreshadow)
    output = """=== character_state ===
新角色状态
=== plot_progress ===
新剧情状态
=== new_foreshadows ===
- 将触发失败的伏笔 | high
"""

    with pytest.raises(RuntimeError, match="模拟伏笔写入失败"):
        service._parse_and_save_state(
            db,
            project_id,
            {"chapter_number": 9},
            output,
        )

    assert db.get_all_project_states(project_id) == {
        "character_state": "原角色状态",
        "plot_progress": "原剧情状态",
    }
    assert db.list_ai_foreshadows(project_id) == []


@pytest.mark.parametrize(
    "parsed",
    [
        {
            "project": {"name": "新项目", "settings": {}},
            "chapters": [
                {"chapter_number": 1, "title": "第一章"},
                "第二章不是字典",
            ],
            "foreshadows": [],
        },
        {
            "project": {"name": "新项目", "settings": {}},
            "chapters": [{"chapter_number": "1", "title": "章节号不是整数"}],
            "foreshadows": [],
        },
        {
            "project": {"name": "新项目", "settings": {}},
            "chapters": [{"chapter_number": 0, "title": "章节号过小"}],
            "foreshadows": [],
        },
        {
            "project": {"name": "新项目", "settings": {}},
            "chapters": [{"chapter_number": 2147483648, "title": "章节号过大"}],
            "foreshadows": [],
        },
        {
            "project": ["项目不是字典"],
            "chapters": [],
            "foreshadows": [],
        },
        {
            "project": {"name": "新项目", "settings": {}},
            "chapters": {"章节不是列表": True},
            "foreshadows": [],
        },
        {
            "project": {"name": "新项目", "settings": {}},
            "chapters": [],
            "foreshadows": {"伏笔不是列表": True},
        },
        {
            "project": {"name": "新项目", "settings": ["设置不是字典"]},
            "chapters": [],
            "foreshadows": [],
        },
        {
            "project": {"name": "新项目", "settings": {}},
            "chapters": [],
            "foreshadows": ["伏笔子项不是字典"],
        },
    ],
    ids=[
        "second-chapter-not-dict",
        "chapter-number-not-int",
        "chapter-number-below-range",
        "chapter-number-above-range",
        "project-not-dict",
        "chapters-not-list",
        "foreshadows-not-list",
        "settings-not-dict",
        "foreshadow-not-dict",
    ],
)
def test_import_wizard_payload_rejects_invalid_input_without_changes(
    db: Database,
    service: AIWritingService,
    parsed: dict[str, object],
) -> None:
    _project_id, session_id = _seed_wizard_data(db)
    before = _database_snapshot(db)

    with pytest.raises(AIServiceError):
        service._import_wizard_payload(
            db,
            parsed,
            session_id,
            "create",
            None,
            None,
        )

    assert _database_snapshot(db) == before


@pytest.mark.parametrize(
    "raw_payload",
    [
        {"project": ["项目不是字典"], "chapters": [], "foreshadows": []},
        {
            "project": {"name": "新项目", "settings": {}},
            "chapters": {},
            "foreshadows": [],
        },
        {
            "project": {"name": "新项目", "settings": {}},
            "chapters": [],
            "foreshadows": {},
        },
    ],
    ids=["project", "chapters", "foreshadows"],
)
def test_wizard_normalization_does_not_hide_invalid_container_types(
    db: Database,
    service: AIWritingService,
    raw_payload: dict[str, object],
) -> None:
    session_id = _create_session(db)
    before = _database_snapshot(db)

    with pytest.raises(AIServiceError):
        parsed = service._normalize_wizard_payload(
            raw_payload,
            {"title": "测试向导会话"},
        )
        service._import_wizard_payload(
            db,
            parsed,
            session_id,
            "create",
            None,
            None,
        )

    assert _database_snapshot(db) == before


@pytest.mark.parametrize("mode", ["create", "merge"])
def test_import_wizard_payload_rolls_back_mid_import_failure(
    db: Database,
    service: AIWritingService,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    target_project_id, session_id = _seed_wizard_data(db)
    before = _database_snapshot(db)
    original_create_foreshadow = db.create_ai_foreshadow
    calls = 0

    def fail_on_second_foreshadow(payload: dict[str, object]) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("模拟向导导入中途失败")
        return original_create_foreshadow(payload)

    monkeypatch.setattr(db, "create_ai_foreshadow", fail_on_second_foreshadow)
    parsed = {
        "project": {
            "name": "导入后的项目名",
            "description": "导入后的简介",
            "outline": "导入后的大纲",
            "settings": {"tone": "tense"},
        },
        "chapters": [
            {"chapter_number": 2, "title": "第二章"},
            {"chapter_number": 3, "title": "第三章"},
        ],
        "foreshadows": [
            {"description": "新伏笔一", "planted_chapter": 2},
            {"description": "新伏笔二", "planted_chapter": 3},
        ],
    }

    with pytest.raises(RuntimeError, match="模拟向导导入中途失败"):
        service._import_wizard_payload(
            db,
            parsed,
            session_id,
            mode,
            target_project_id if mode == "merge" else None,
            ["name", "description", "outline"],
        )

    assert _database_snapshot(db) == before


def test_import_wizard_payload_deduplicates_foreshadows_after_clipping(
    db: Database,
    service: AIWritingService,
) -> None:
    session_id = _create_session(db)
    shared_prefix = "x" * 2000
    parsed = {
        "project": {"name": "裁剪去重项目", "settings": {}},
        "chapters": [],
        "foreshadows": [
            {"description": shared_prefix + "甲"},
            {"description": shared_prefix + "乙"},
        ],
    }

    project_id = service._import_wizard_payload(
        db,
        parsed,
        session_id,
        "create",
        None,
        None,
    )

    foreshadows = db.list_ai_foreshadows(project_id)
    assert [item["description"] for item in foreshadows] == [shared_prefix]
