from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pixiv_novel_sync.ai.providers import ProviderConfigError, validate_base_url
from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.webapp import create_app


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

    bound_finding = next(f for f in with_agents if f["code"] == "disabled_but_bound")
    assert bound_finding["level"] == "will_fail"
    assert not any(f["code"] == "disabled_but_bound" for f in without)


def test_disabled_provider_really_fails_routing_not_just_degrades(db: Database) -> None:
    """把 disabled_but_bound 定为 will_fail 的依据：路由层是硬失败，不是降级。

    spec §3.2 把这一格的档位留给「以 resolve_candidates 的真实行为为准」。
    真实行为是 _provider_row 直接抛 ModelRouteError，固定绑定连目录都走不到，
    所以这条固定为 will_fail。这里断言的是**行为**而不是源码字面量：哪天路由改成
    跳过禁用 Provider 继续找别的候选，这条会红，提醒把档位降回 warn；
    而只改文案不改行为不会误伤。
    """
    from pixiv_novel_sync.ai.model_router import ModelRouteError, ModelRouter

    provider_id = db.create_ai_provider({
        "name": "停用网关", "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1", "api_key_encrypted": "cipher", "enabled": 0,
    })

    with pytest.raises(ModelRouteError):
        ModelRouter._provider_row(db, provider_id)


def test_lint_survives_a_base_url_that_breaks_urlparse_itself() -> None:
    """urlparse 自己抛的裸 ValueError 也必须收成 finding，不能穿出来。

    _parse_provider_url 的 urlparse(raw) 在任何 try 之外（providers.py:123），
    IPv6 括号不配对（``http://[::1``）会抛裸 ValueError("Invalid IPv6 URL")。
    健康端点把整个 ai_health() 包在 except Exception 里，所以只要库里存着一行这种
    地址，整条健康横幅就会整体消失——恰好是配置坏掉、最需要横幅说话的时候。

    另外这类裸异常的消息是英文的，不能直接回显给中文界面。
    """
    findings = AIWritingService.provider_config_lint(
        {"base_url": "http://[::1", "has_api_key": True, "enabled": True},
        routable_models=3,
    )

    base_url_finding = next(f for f in findings if f["code"] == "base_url")
    assert base_url_finding["level"] == "will_fail"
    # 裸 ValueError 的英文原文不许直接进界面
    assert "Invalid IPv6 URL" not in base_url_finding["message"]
    assert "base_url" in base_url_finding["message"]


def test_lint_leaves_a_parked_provider_alone() -> None:
    """停用且没有 Agent 绑着的 Provider 是「停着的」，不是「坏的」。

    catalog.py:155 对已停用 Provider 把 routable 硬置 0，所以不豁免的话每个停用
    Provider 都会常驻一条 no_routable_model 警告加一条缺 Key 的 will_fail。
    横幅上的黄色要稀有才有意义（同 CLAUDE.md 对 partial 的态度）。
    """
    parked = {"base_url": "https://api.example.com/v1", "has_api_key": False, "enabled": False}

    assert AIWritingService.provider_config_lint(parked, bound_agent_count=0, routable_models=0) == []


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """按仓库既有装配方式起真 app（未设 DASHBOARD_TOKEN 时鉴权门只放行环回地址）。"""
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "ai-health-test")
    db_path = tmp_path / "app.db"
    monkeypatch.setenv("PIXIV_DB_PATH", str(db_path))
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "storage:\n"
        f"  public_dir: {(tmp_path / 'pub').as_posix()}\n"
        f"  private_dir: {(tmp_path / 'priv').as_posix()}\n"
        f"  db_path: {db_path.as_posix()}\n"
        "sync:\n  auto_sync_enabled: false\n",
        encoding="utf-8",
    )
    app = create_app(
        config_path=str(config_path), env_path=str(env_path), start_scheduler=False
    )
    return app, db_path


def _get_health(app, query: str = "") -> dict[str, Any]:
    res = app.test_client().get(
        f"/api/dashboard/ai/health{query}", environ_base={"REMOTE_ADDR": "127.0.0.1"}
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["ok"] is True
    return body["data"]


def test_health_endpoint_surfaces_broken_provider_and_bound_agents(tmp_path, monkeypatch) -> None:
    """复刻 2026-09-03 生产事故：http:// 非回环 Provider + 一堆 Agent 绑在上面。"""
    app, db_path = _app(tmp_path, monkeypatch)
    db = Database(db_path)
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "deepseek-v4-pro", "provider_type": "openai_compatible",
        "base_url": "http://nas.example.com:3000", "api_key_encrypted": "cipher",
        "default_model": None, "enabled": 1,
    })
    for i in range(3):
        db.create_ai_agent({
            "name": f"写作助手{i}", "task_type": "continue", "binding_type": "fixed",
            "provider_id": provider_id, "model": "m1", "system_prompt": "x", "enabled": 1,
        })
    db.close()

    res = app.test_client().get(
        "/api/dashboard/ai/health", environ_base={"REMOTE_ADDR": "127.0.0.1"}
    )

    assert res.status_code == 200
    data = res.get_json()["data"]
    provider = next(p for p in data["providers"] if p["id"] == provider_id)
    assert provider["status"] == "will_fail"
    assert provider["bound_agent_count"] == 3
    assert data["totals"]["agents_unhealthy"] == 3
    # 健康端点不许泄露连接地址
    assert "base_url" not in provider
    assert "nas.example.com" not in res.get_data(as_text=True)


def test_health_response_carries_no_connection_details_at_all(tmp_path, monkeypatch) -> None:
    """逐字段把投影钉死：连接地址、代理、密钥、同步租约持有者一个都不许出现。

    list_ai_providers() 是 SELECT *，会带上 base_url / proxy / models_sync_owner，
    所以投影必须是白名单挑字段，不能拿整行往外递。
    """
    app, db_path = _app(tmp_path, monkeypatch)
    db = Database(db_path)
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "私有网关", "provider_type": "openai_compatible",
        "base_url": "https://secret-host.internal/v1", "api_key_encrypted": "cipher-blob",
        "proxy": "http://proxy.internal:8080", "enabled": 1,
    })
    db.close()

    res = app.test_client().get(
        "/api/dashboard/ai/health", environ_base={"REMOTE_ADDR": "127.0.0.1"}
    )
    body = res.get_data(as_text=True)
    provider = next(p for p in res.get_json()["data"]["providers"] if p["id"] == provider_id)

    assert set(provider) == {
        "id", "name", "enabled", "status", "findings", "bound_agent_count",
        "routable_models", "models_synced_at", "models_sync_error", "attempts",
    }
    for leak in ("secret-host.internal", "proxy.internal", "cipher-blob"):
        assert leak not in body
    for key in ("base_url", "proxy", "api_key_encrypted", "models_sync_owner",
                "models_sync_lease_until", "default_model"):
        assert key not in provider


def test_health_endpoint_inherits_status_for_pool_bound_agents(tmp_path, monkeypatch) -> None:
    """池绑定的 Agent：池里所有成员的 Provider 都必失败时才算它必失败。"""
    app, db_path = _app(tmp_path, monkeypatch)
    db = Database(db_path)
    db.init_schema()
    broken_id = db.create_ai_provider({
        "name": "坏网关", "provider_type": "openai_compatible",
        "base_url": "http://nas.example.com:3000", "api_key_encrypted": "cipher", "enabled": 1,
    })
    good_id = db.create_ai_provider({
        "name": "好网关", "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1", "api_key_encrypted": "cipher", "enabled": 1,
    })
    broken_model = db.create_ai_provider_model(
        {"provider_id": broken_id, "model_key": "bad-model", "enabled": True}
    )
    good_model = db.create_ai_provider_model(
        {"provider_id": good_id, "model_key": "good-model", "enabled": True}
    )

    def _pool(name: str, model_ids: list[int]) -> int:
        pool_id = db.create_ai_model_pool({"name": name, "pool_kind": "custom"})
        version = db.replace_ai_model_pool_members(
            pool_id,
            [{"provider_model_id": mid, "enabled": True} for mid in model_ids],
            expected_version=1,
        )
        db.update_ai_model_pool(pool_id, {"enabled": True}, expected_version=version)
        return pool_id

    all_broken = _pool("全坏池", [broken_model])
    mixed = _pool("混合池", [broken_model, good_model])
    empty_pool = db.create_ai_model_pool({"name": "空池", "pool_kind": "custom"})

    doomed = db.create_ai_agent({
        "name": "全坏池助手", "task_type": "continue", "binding_type": "pool",
        "model_pool_id": all_broken, "system_prompt": "x", "enabled": 1,
    })
    survivor = db.create_ai_agent({
        "name": "混合池助手", "task_type": "continue", "binding_type": "pool",
        "model_pool_id": mixed, "system_prompt": "x", "enabled": 1,
    })
    starved = db.create_ai_agent({
        "name": "空池助手", "task_type": "continue", "binding_type": "pool",
        "model_pool_id": empty_pool, "system_prompt": "x", "enabled": 1,
    })
    db.close()

    data = _get_health(app)
    by_id = {a["id"]: a for a in data["agents"]}

    assert by_id[doomed]["status"] == "will_fail"
    assert by_id[survivor]["status"] == "healthy"
    assert by_id[survivor]["reason"] == ""
    assert by_id[starved]["status"] == "will_fail"
    assert "成员" in by_id[starved]["reason"]
    assert data["totals"]["agents_unhealthy"] == 2


def test_health_endpoint_follows_the_fallback_pool_chain(tmp_path, monkeypatch) -> None:
    """根池全坏但后备池是好的，Agent 不算必失败——路由会走过去。

    model_router.py:492-506 把绑定的池当成一条链在走（禁用的节点跳过、继续找后备），
    所以只看根池成员会把「有后备兜住」的 Agent 误报成红的。误报比不报更伤：
    横幅红了却无事可修，用户下次就不看它了。判据用的是运行时那个 expand_pool_ids
    （admin.py:_validate_agent_binding 也用它），而不是另写一遍链遍历。
    """
    app, db_path = _app(tmp_path, monkeypatch)
    db = Database(db_path)
    db.init_schema()
    broken_id = db.create_ai_provider({
        "name": "坏网关", "provider_type": "openai_compatible",
        "base_url": "http://nas.example.com:3000", "api_key_encrypted": "cipher", "enabled": 1,
    })
    good_id = db.create_ai_provider({
        "name": "好网关", "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1", "api_key_encrypted": "cipher", "enabled": 1,
    })
    broken_model = db.create_ai_provider_model(
        {"provider_id": broken_id, "model_key": "bad-model", "enabled": True}
    )
    good_model = db.create_ai_provider_model(
        {"provider_id": good_id, "model_key": "good-model", "enabled": True}
    )

    def _pool(name: str, model_id: int, fallback: int | None = None) -> int:
        pool_id = db.create_ai_model_pool({"name": name, "pool_kind": "custom"})
        version = db.replace_ai_model_pool_members(
            pool_id, [{"provider_model_id": model_id, "enabled": True}], expected_version=1,
        )
        patch: dict[str, Any] = {"enabled": True}
        if fallback is not None:
            patch["fallback_pool_id"] = fallback
        db.update_ai_model_pool(pool_id, patch, expected_version=version)
        return pool_id

    rescue = _pool("后备池", good_model)
    root = _pool("根池", broken_model, fallback=rescue)
    saved = db.create_ai_agent({
        "name": "有后备的助手", "task_type": "continue", "binding_type": "pool",
        "model_pool_id": root, "system_prompt": "x", "enabled": 1,
    })
    db.close()

    data = _get_health(app)
    agent = next(a for a in data["agents"] if a["id"] == saved)

    assert agent["status"] == "healthy"
    assert agent["reason"] == ""
    assert data["totals"]["agents_unhealthy"] == 0


def _enabled_pool(db: Database, name: str, model_ids: list[int]) -> int:
    """建一个启用的池并按顺序放入成员（成员行本身都是启用的）。"""
    pool_id = db.create_ai_model_pool({"name": name, "pool_kind": "custom"})
    version = db.replace_ai_model_pool_members(
        pool_id,
        [{"provider_model_id": mid, "enabled": True} for mid in model_ids],
        expected_version=1,
    )
    db.update_ai_model_pool(pool_id, {"enabled": True}, expected_version=version)
    return pool_id


def test_health_flags_a_provider_that_is_only_depended_on_through_a_pool(
    tmp_path, monkeypatch
) -> None:
    """「只经由模型池被依赖」的 Provider 被停用时，两侧都不许报绿。

    这是 2026-09-03 事故形状的池绑定版本，也是本横幅存在的理由：假绿比没有横幅更坏。
    bound_agent_count 只数固定绑定，所以这个 Provider 的「有人绑着」永远是 0 ——
    parked 豁免必须靠池引用来兜，否则缺 Key 与空目录双双被抑制，整行变绿。
    运行时的判据在 model_router.py:509-513（成员模型不 routable 就不产生候选）与
    :531-533（一个候选都没有就 raise ModelRouteError("模型池没有可用模型")），
    而 routable 的定义（catalog.py:76）里就含 provider_enabled。
    """
    app, db_path = _app(tmp_path, monkeypatch)
    db = Database(db_path)
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "池里唯一的网关", "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1", "api_key_encrypted": "cipher", "enabled": 1,
    })
    model_id = db.create_ai_provider_model(
        {"provider_id": provider_id, "model_key": "only-model", "enabled": True}
    )
    pool_id = _enabled_pool(db, "唯一的池", [model_id])
    agent_id = db.create_ai_agent({
        "name": "池绑定助手", "task_type": "continue", "binding_type": "pool",
        "model_pool_id": pool_id, "system_prompt": "x", "enabled": 1,
    })
    # UI 上一步可达：update_ai_provider 的 allowed 集合含 enabled，且没有任何引用守卫
    db.update_ai_provider(provider_id, {"enabled": 0})
    db.close()

    data = _get_health(app)
    provider = next(p for p in data["providers"] if p["id"] == provider_id)
    agent = next(a for a in data["agents"] if a["id"] == agent_id)

    # 有 Key、地址也合法，所以只剩 no_routable_model 这一条 warn
    assert provider["findings"] != []
    assert {f["code"] for f in provider["findings"]} == {"no_routable_model"}
    assert provider["status"] == "warn"
    assert provider["bound_agent_count"] == 0
    assert agent["status"] == "will_fail"
    assert data["totals"]["agents_unhealthy"] >= 1


def test_health_flags_a_pool_member_whose_model_row_is_switched_off(
    tmp_path, monkeypatch
) -> None:
    """成员模型行被停用（models 页的开关）时运行时同样选不出候选。

    reason 必须与「没有启用成员」区分开：成员开关本来就是开着的，
    照旧那句话会把用户送去翻一个已经开着的开关。
    """
    app, db_path = _app(tmp_path, monkeypatch)
    db = Database(db_path)
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "好网关", "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1", "api_key_encrypted": "cipher", "enabled": 1,
    })
    model_id = db.create_ai_provider_model(
        {"provider_id": provider_id, "model_key": "parked-model", "enabled": True}
    )
    pool_id = _enabled_pool(db, "成员被关掉的池", [model_id])
    agent_id = db.create_ai_agent({
        "name": "池绑定助手", "task_type": "continue", "binding_type": "pool",
        "model_pool_id": pool_id, "system_prompt": "x", "enabled": 1,
    })
    db.update_ai_provider_model(model_id, {"enabled": False})
    db.close()

    data = _get_health(app)
    agent = next(a for a in data["agents"] if a["id"] == agent_id)

    assert agent["status"] == "will_fail"
    assert "可路由" in agent["reason"]
    assert "启用成员" not in agent["reason"]


def test_health_does_not_cry_wolf_when_the_pool_keeps_one_routable_member(
    tmp_path, monkeypatch
) -> None:
    """一个成员的 Provider 停用、另一个还好着时，运行时优雅降级 —— 不许报红。

    这条防的是把 routable 判据加进去时改过头：假红会让用户下次不看这个横幅，
    与假绿一样毁掉它的价值。运行时会跳过不可路由的成员继续找下一个
    （model_router.py:508-513 是 continue，不是 raise）。
    """
    app, db_path = _app(tmp_path, monkeypatch)
    db = Database(db_path)
    db.init_schema()
    good_id = db.create_ai_provider({
        "name": "好网关", "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1", "api_key_encrypted": "cipher", "enabled": 1,
    })
    parked_id = db.create_ai_provider({
        "name": "待停用网关", "provider_type": "openai_compatible",
        "base_url": "https://backup.example.com/v1", "api_key_encrypted": "cipher", "enabled": 1,
    })
    good_model = db.create_ai_provider_model(
        {"provider_id": good_id, "model_key": "good-model", "enabled": True}
    )
    parked_model = db.create_ai_provider_model(
        {"provider_id": parked_id, "model_key": "parked-model", "enabled": True}
    )
    pool_id = _enabled_pool(db, "两个成员的池", [good_model, parked_model])
    agent_id = db.create_ai_agent({
        "name": "有余量的助手", "task_type": "continue", "binding_type": "pool",
        "model_pool_id": pool_id, "system_prompt": "x", "enabled": 1,
    })
    db.update_ai_provider(parked_id, {"enabled": 0})
    db.close()

    data = _get_health(app)
    agent = next(a for a in data["agents"] if a["id"] == agent_id)

    assert agent["status"] == "healthy"
    assert agent["reason"] == ""
    assert data["totals"]["agents_unhealthy"] == 0


def test_health_does_not_count_a_disabled_agent_as_unhealthy(tmp_path, monkeypatch) -> None:
    """停用的 Agent 不可能因为投影所报的原因失败，所以不占 agents_unhealthy。

    运行时第一道门就是 model_router.py:650-652 的
    ``if not agent.enabled: raise ModelRouteError("Agent 已禁用")`` ——
    它永远走不到解析候选那一步。停一个 Provider 连带停掉它那批 Agent 是最自然的
    运维动作，此时 Provider 侧按 Ruling B 正确变绿，Agent 侧整批亮红会自相矛盾。
    agents[] 保持五键契约（T4 消费这个形状），所以纠偏必须在投影里做完。
    """
    app, db_path = _app(tmp_path, monkeypatch)
    db = Database(db_path)
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "坏网关", "provider_type": "openai_compatible",
        "base_url": "http://nas.example.com:3000", "api_key_encrypted": "cipher", "enabled": 1,
    })
    sleeping = db.create_ai_agent({
        "name": "已停用助手", "task_type": "continue", "binding_type": "fixed",
        "provider_id": provider_id, "model": "m1", "system_prompt": "x", "enabled": 0,
    })
    db.close()

    data = _get_health(app)
    agent = next(a for a in data["agents"] if a["id"] == sleeping)

    assert agent["status"] == "healthy"
    assert "停用" in agent["reason"]
    assert data["totals"]["agents_unhealthy"] == 0
    # 五键契约不许因为这条修正而变宽（尤其不许新增 enabled 让 T4 自己判断）
    assert set(agent) == {"id", "name", "task_type", "status", "reason"}


def test_health_endpoint_reports_parked_provider_as_healthy(tmp_path, monkeypatch) -> None:
    """停用且没人绑的 Provider 在投影里也是 healthy，不占横幅的黄色额度。"""
    app, db_path = _app(tmp_path, monkeypatch)
    db = Database(db_path)
    db.init_schema()
    parked_id = db.create_ai_provider({
        "name": "备用网关", "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1", "enabled": 0,
    })
    db.close()

    data = _get_health(app)
    parked = next(p for p in data["providers"] if p["id"] == parked_id)

    # 停用的 Provider 依然列出（Ruling C）：它是否有 Agent 绑着是这个视图最重要的事实
    assert parked["enabled"] is False
    assert parked["status"] == "healthy"
    assert parked["findings"] == []
    assert data["totals"]["providers_will_fail"] == 0


def test_health_endpoint_reports_failing_tasks_and_honours_days_window(tmp_path, monkeypatch) -> None:
    """任务级失败摘要是把「红 AI 任务」和「绿 preference_analyze」关联起来的唯一线索。"""
    app, db_path = _app(tmp_path, monkeypatch)
    db = Database(db_path)
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "网关", "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1", "api_key_encrypted": "cipher", "enabled": 1,
    })
    for i in range(2):
        db.conn.execute(
            """INSERT INTO ai_jobs (job_id, task_type, status, input_json, created_at, finished_at)
               VALUES (?, 'keyword_clean', 'failed', '{}', datetime('now'), datetime('now'))""",
            (f"recent{i}",),
        )
    db.conn.execute(
        """INSERT INTO ai_jobs (job_id, task_type, status, input_json, created_at, finished_at)
           VALUES ('old1', 'chapter_summary', 'failed', '{}',
                   datetime('now', '-20 days'), datetime('now', '-20 days'))"""
    )
    db.conn.commit()
    _seed_attempt(db, "recent0", provider_id, "failed",
                  error_scope="provider", error_category="configuration")
    db.close()

    week = _get_health(app)
    month = _get_health(app, "?days=30")

    assert week["window_days"] == 7
    assert week["ai_job_failures"] == [
        {"task_type": "keyword_clean", "failures": 2, "last_at": week["ai_job_failures"][0]["last_at"]}
    ]
    assert month["window_days"] == 30
    assert {row["task_type"] for row in month["ai_job_failures"]} == {"keyword_clean", "chapter_summary"}
    # 战绩按 provider_id 聚合，随窗口一起走（按 id 找，不按位置——list_ai_providers
    # 是 ORDER BY id DESC，加第二个 Provider 就会静默错位）
    assert next(p for p in week["providers"] if p["id"] == provider_id)["attempts"]["failures"] == 1


def test_health_days_window_is_validated_like_every_other_int_arg(tmp_path, monkeypatch) -> None:
    """days 走 ai_web 的 parse_int（和另外十几个 GET 整数参数同一把），越界即 400。

    静默 clamp 会让「我问了 3650 天」和「我问了 30 天」返回同一个数字而不作声，
    横幅上的「最近 N 天」就成了假话。宁可 400。
    """
    app, _db_path = _app(tmp_path, monkeypatch)
    client = app.test_client()

    def get(query: str):
        return client.get(
            f"/api/dashboard/ai/health{query}", environ_base={"REMOTE_ADDR": "127.0.0.1"}
        )

    assert get("?days=30").get_json()["data"]["window_days"] == 30
    # 空值与非法值回落到默认窗口，不报错（parse_int 的既有语义）
    assert get("?days=").get_json()["data"]["window_days"] == 7
    for bad in ("?days=0", "?days=31", "?days=-1"):
        res = get(bad)
        assert res.status_code == 400, bad
        assert "days" in res.get_json()["error"]


def test_health_endpoint_totals_are_consistent_on_an_empty_install(tmp_path, monkeypatch) -> None:
    """全新部署没有任何 Provider/Agent 时也要给出完整骨架，前端不必判 undefined。"""
    app, _db_path = _app(tmp_path, monkeypatch)

    data = _get_health(app)

    assert data == {
        "window_days": 7,
        "providers": [],
        "agents": [],
        "ai_job_failures": [],
        "totals": {
            "providers": 0, "providers_will_fail": 0, "routable_models": 0,
            "agents": 0, "agents_unhealthy": 0,
        },
    }


def test_health_endpoint_makes_no_network_call(tmp_path, monkeypatch) -> None:
    """整条健康路径零网络：这是「可随意刷新」的前提。"""
    import socket

    app, db_path = _app(tmp_path, monkeypatch)
    db = Database(db_path)
    db.init_schema()
    db.create_ai_provider({
        "name": "网关", "provider_type": "openai_compatible",
        "base_url": "http://nas.example.com:3000", "api_key_encrypted": "cipher", "enabled": 1,
    })
    db.close()

    def explode(*args, **kwargs):
        raise AssertionError("健康投影不允许发起 DNS 解析或建立连接")

    monkeypatch.setattr(socket, "getaddrinfo", explode)
    monkeypatch.setattr(socket.socket, "connect", explode)

    assert _get_health(app)["totals"]["providers_will_fail"] == 1


def test_test_provider_falls_back_to_first_routable_model(tmp_path, monkeypatch) -> None:
    """想验证一个新 Provider 通不通，不该被「先填默认模型」挡住。"""
    from pixiv_novel_sync.ai.service import AIServiceError

    app, db_path = _app(tmp_path, monkeypatch)
    db = Database(db_path)
    db.init_schema()
    provider_id = db.create_ai_provider({
        "name": "p", "provider_type": "openai_compatible",
        "base_url": "https://api.example.com/v1", "api_key_encrypted": None,
        "default_model": None, "enabled": 1,
    })
    db.close()

    res = app.test_client().post(
        f"/api/dashboard/ai/providers/{provider_id}/test",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    # 目录为空 ⇒ 不再是「未配置默认模型」，而是指向下一步动作
    assert res.status_code == 400
    assert "获取模型列表" in res.get_json()["error"]
