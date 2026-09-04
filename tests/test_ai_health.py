from __future__ import annotations

from pathlib import Path

import pytest

from pixiv_novel_sync.ai.providers import ProviderConfigError, validate_base_url
from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.storage_db import Database


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "health.db")
    database.init_schema()
    yield database
    database.close()


def _seed_attempt(db: Database, job_id: str, provider_id: int, status: str, **kw) -> None:
    """直接插 attempts 行：allocate/finish 那套 CAS 是给真实路由用的，这里只需要行。"""
    # attempts.job_id 是指向 ai_jobs 的外键，而每条连接都开着 foreign_keys=ON，
    # 所以先补一行父 job；status 留在 running，免得污染任务级失败摘要。
    db.conn.execute(
        """INSERT OR IGNORE INTO ai_jobs (job_id, task_type, status, input_json)
           VALUES (?, 'keyword_clean', 'running', '{}')""",
        (job_id,),
    )
    db.conn.execute(
        """INSERT INTO ai_job_model_attempts
           (job_id, attempt_index, provider_id, model_key, stage, status,
            error_scope, error_category, error_message,
            agent_config_hash, provider_config_hash, candidate_list_hash,
            started_at, finished_at)
           VALUES (?, ?, ?, ?, 'main', ?, ?, ?, ?, 'h', 'h', 'h',
                   datetime('now'), datetime('now'))""",
        (
            job_id, kw.get("attempt_index", 0), provider_id, kw.get("model_key", "m1"),
            status, kw.get("error_scope"), kw.get("error_category"), kw.get("error_message"),
        ),
    )
    db.conn.commit()


def test_provider_attempt_health_aggregates_per_provider(db: Database) -> None:
    _seed_attempt(db, "j1", 3, "failed", error_scope="provider",
                  error_category="configuration", error_message="base_url 必须使用 https（本机回环地址除外）")
    _seed_attempt(db, "j2", 3, "failed", error_scope="provider", error_category="configuration")
    _seed_attempt(db, "j3", 4, "succeeded")

    health = db.get_provider_attempt_health(days=7)

    assert health[3]["attempts"] == 2
    assert health[3]["failures"] == 2
    assert health[3]["last_error_category"] == "configuration"
    assert health[4]["failures"] == 0


def test_ai_job_failure_summary_groups_by_task_type(db: Database) -> None:
    for i in range(3):
        db.conn.execute(
            """INSERT INTO ai_jobs (job_id, task_type, status, input_json, created_at, finished_at)
               VALUES (?, 'keyword_clean', 'failed', '{}', datetime('now'), datetime('now'))""",
            (f"k{i}",),
        )
    db.conn.execute(
        """INSERT INTO ai_jobs (job_id, task_type, status, input_json, created_at, finished_at)
           VALUES ('ok1', 'keyword_clean', 'succeeded', '{}', datetime('now'), datetime('now'))"""
    )
    db.conn.commit()

    summary = db.get_ai_job_failure_summary(days=7)

    assert summary == [
        {"task_type": "keyword_clean", "failures": 3, "last_at": summary[0]["last_at"]}
    ]
    assert summary[0]["last_at"]


def test_lint_flags_plain_http_with_the_runtime_message() -> None:
    """体检结论必须与运行时那句报错逐字相同——另写一份规则迟早漂移。"""
    provider = {"base_url": "http://nas.example.com:3000", "has_api_key": True, "enabled": True}

    findings = AIWritingService.provider_config_lint(provider, routable_models=3)

    base_url_finding = next(f for f in findings if f["code"] == "base_url")
    assert base_url_finding["level"] == "will_fail"
    with pytest.raises(ProviderConfigError) as excinfo:
        validate_base_url("http://nas.example.com:3000", resolve=False)
    assert base_url_finding["message"] == str(excinfo.value)


def test_lint_does_not_touch_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """体检必须用 resolve=False：一旦有人改成 resolve=True，这条就会炸。"""
    import socket

    def explode(*args, **kwargs):
        raise AssertionError("静态体检不允许发起 DNS 解析")

    monkeypatch.setattr(socket, "getaddrinfo", explode)
    provider = {"base_url": "https://api.example.com/v1", "has_api_key": True, "enabled": True}

    assert AIWritingService.provider_config_lint(provider, routable_models=5) == []


def test_lint_flags_missing_key_and_empty_catalog() -> None:
    findings = AIWritingService.provider_config_lint(
        {"base_url": "https://api.example.com/v1", "has_api_key": False, "enabled": True},
        routable_models=0,
    )
    codes = {f["code"]: f["level"] for f in findings}
    assert codes["api_key"] == "will_fail"
    assert codes["no_routable_model"] == "warn"


def test_lint_flags_disabled_provider_only_when_agents_bound() -> None:
    disabled = {"base_url": "https://api.example.com/v1", "has_api_key": True, "enabled": False}

    with_agents = AIWritingService.provider_config_lint(disabled, bound_agent_count=15, routable_models=3)
    without = AIWritingService.provider_config_lint(disabled, bound_agent_count=0, routable_models=3)

    assert any(f["code"] == "disabled_but_bound" for f in with_agents)
    assert not any(f["code"] == "disabled_but_bound" for f in without)
