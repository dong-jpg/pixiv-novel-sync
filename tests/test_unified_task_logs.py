from __future__ import annotations

from pathlib import Path

import pytest

from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def db(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "test.db")
    instance.init_schema()
    return instance


def test_get_ai_task_logs_projects_ai_jobs_to_unified_shape(db: Database) -> None:
    """#12: ai_jobs 只读投影为 task_logs 结构，供统一日志页消费。"""
    db.create_ai_job("aijob-1", "chapter_continue", agent_id=1, input_data={"chapter_id": 5})
    db.update_ai_job("aijob-1", "succeeded", output_text="done")

    result = db.get_ai_task_logs(page=1, page_size=20, days=3)
    items = result["items"]
    assert len(items) == 1
    row = items[0]
    # 统一结构字段齐全
    assert row["job_id"] == "aijob-1"
    assert row["task_type"] == "chapter_continue"
    assert row["task_name"] == "自动生成章节"  # 映射为中文名
    assert row["status"] == "succeeded"
    assert row["category"] == "ai"
    assert row["is_auto_sync"] is False
    assert row["started_at"] is not None  # 回退到 created_at


def test_get_ai_task_logs_filters_by_task_type(db: Database) -> None:
    db.create_ai_job("aijob-a", "chapter_continue", agent_id=1, input_data={})
    db.create_ai_job("aijob-b", "distill_style", agent_id=1, input_data={})

    only_distill = db.get_ai_task_logs(task_type="distill_style", days=3)
    assert [r["job_id"] for r in only_distill["items"]] == ["aijob-b"]
    assert only_distill["items"][0]["task_name"] == "风格蒸馏"


def test_get_ai_task_logs_unknown_type_falls_back_to_raw(db: Database) -> None:
    db.create_ai_job("aijob-x", "some_new_pipeline_step", agent_id=None, input_data={})
    result = db.get_ai_task_logs(days=3)
    assert result["items"][0]["task_name"] == "some_new_pipeline_step"


def test_get_ai_task_logs_empty(db: Database) -> None:
    result = db.get_ai_task_logs(days=3)
    assert result["items"] == []
    assert result["total"] == 0
