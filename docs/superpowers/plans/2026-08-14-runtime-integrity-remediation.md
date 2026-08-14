# Runtime Integrity Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 task log 误回收、无界分页、Provider 重试、归档删除/恢复、部署契约和静态错误。

**Architecture:** `init_schema()` 恢复为纯迁移入口；task log 通过 owner lease 恢复。网络分页统一走共享守卫。文件删除使用同卷 trash manifest 与 SQLite 事务协调。部署脚本和 unit 由自动契约测试绑定。

**Tech Stack:** Python 3.10+、SQLite、Flask、pytest、PowerShell/Linux systemd 配置。

## Global Constraints

- 所有生产行为先有失败回归测试并确认 RED。
- 迁移幂等，旧数据库可读，结束时 `PRAGMA foreign_key_check` 为空。
- 不删除用户配置、数据库、归档或未跟踪文件。
- 文件路径只能位于配置的 storage roots。

---

### Task 1: task log owner lease 与安全恢复

**Files:**
- Modify: `src/pixiv_novel_sync/storage/schema.py`
- Modify: `src/pixiv_novel_sync/storage/tasks.py`
- Modify: `src/pixiv_novel_sync/webapp.py`
- Test: `tests/test_task_log_leases.py`

**Interfaces:**
- Produces: `create_task_log(..., owner_token: str | None = None, lease_seconds: int = 30) -> int`
- Produces: `heartbeat_task_log(log_id: int, owner_token: str, lease_seconds: int = 30) -> bool`
- Produces: `finish_task_log(log_id: int, owner_token: str, status: str, ...) -> bool`
- Produces: `fail_stale_task_logs(now: datetime | None = None, grace_seconds: int = 30) -> int`

- [ ] **Step 1: 写 `init_schema()` 不修改 live log 的失败测试**

```python
def test_init_schema_preserves_running_task_log(tmp_path):
    db = Database(tmp_path / "state.db")
    db.init_schema()
    log_id = db.create_task_log("sync", "同步", owner_token="owner-a")
    other = Database(tmp_path / "state.db")
    other.init_schema()
    assert db.get_task_log_by_id(log_id)["status"] == "running"
```

- [ ] **Step 2: 写租约过期/活跃/错误 owner 的失败测试**

```python
def test_stale_recovery_requires_expired_lease_and_heartbeat(db):
    active = db.create_task_log("sync", "active", owner_token="a")
    stale = db.create_task_log("sync", "stale", owner_token="b")
    db.conn.execute("UPDATE task_logs SET lease_until='2000-01-01', heartbeat_at='2000-01-01' WHERE id=?", (stale,))
    db.conn.commit()
    assert db.fail_stale_task_logs() == 1
    assert db.get_task_log_by_id(active)["status"] == "running"
    assert db.get_task_log_by_id(stale)["status"] == "failed"
    assert db.finish_task_log(active, "wrong", "succeeded") is False
```

- [ ] **Step 3: 运行 RED**

Run: `python -m pytest tests/test_task_log_leases.py -q`
Expected: FAIL，因为新列和租约方法不存在，且第二次 `init_schema()` 把日志改为 failed。

- [ ] **Step 4: 实现幂等列迁移和 owner-CAS CRUD**

在 `_migrate_task_logs_table()` 增加 `owner_token`、`heartbeat_at`、`lease_until` 和 `(status, lease_until)` 索引；从 `init_schema()` 删除 `_fix_stale_running_logs()` 调用。所有收口 SQL 使用 `WHERE id=? AND owner_token=? AND status='running'`。

- [ ] **Step 5: Web task-log bridge 建立 10 秒 heartbeat 并在 finally 停止**

`_submit_shared_job()` 为日志生成 owner token；`_run_shared_web_job()` 的 heartbeat 线程只持有独立短连接，正常、失败和取消均停止线程并 owner-CAS 收口。`create_app()` 初始化时显式调用 `fail_stale_task_logs()` 一次。未传 owner token 的旧调用仍可创建日志，但不能被租约恢复入口误判为失效任务。

- [ ] **Step 6: 运行 GREEN 和关联测试**

Run: `python -m pytest tests/test_task_log_leases.py tests/test_jobs_runner.py tests/test_webapp_jobs.py -q`
Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add src/pixiv_novel_sync/storage/schema.py src/pixiv_novel_sync/storage/tasks.py src/pixiv_novel_sync/webapp.py tests/test_task_log_leases.py
git commit -m "fix: recover only expired task log leases"
```

### Task 2: 统一 Pixiv 分页守卫

**Files:**
- Create: `src/pixiv_novel_sync/sync/pagination.py`
- Modify: `src/pixiv_novel_sync/sync_engine.py`
- Modify: `src/pixiv_novel_sync/jobs/services.py`
- Test: `tests/test_sync_pagination.py`

**Interfaces:**
- Produces: `PaginationGuard(limit: int | None, safety_limit: int, stop_requested: Callable[[], bool] | None)`
- Produces: `PaginationGuard.accept(next_query: Mapping[str, Any] | None) -> bool`

- [ ] **Step 1: 写重复 cursor、安全默认上限和取消 RED**

```python
def test_guard_rejects_repeated_cursor():
    guard = PaginationGuard(None, safety_limit=3)
    assert guard.accept({"offset": 30}) is True
    assert guard.accept({"offset": 30}) is False
    assert guard.reason == "repeated_cursor"

def test_none_limit_uses_safety_limit():
    guard = PaginationGuard(None, safety_limit=2)
    assert [guard.accept({"offset": value}) for value in (1, 2, 3)] == [True, True, False]
```

- [ ] **Step 2: 运行 RED**

Run: `python -m pytest tests/test_sync_pagination.py -q`
Expected: FAIL，模块不存在。

- [ ] **Step 3: 实现稳定 JSON cursor 指纹和明确 stop reason**

指纹使用排序后的 JSON；`None`、空 mapping、达到上限、重复 cursor、取消分别记录 `exhausted|empty|limit|repeated_cursor|cancelled`。

- [ ] **Step 4: 替换关注用户、用户小说、用户备份、收藏和系列循环**

每次请求前后调用 `guard.check_cancelled()`；业务配置只可降低安全上限。

- [ ] **Step 5: 运行 GREEN**

Run: `python -m pytest tests/test_sync_pagination.py tests/test_sync_engine_incremental.py tests/test_jobs_services.py -q`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add src/pixiv_novel_sync/sync/pagination.py src/pixiv_novel_sync/sync_engine.py src/pixiv_novel_sync/jobs/services.py tests/test_sync_pagination.py
git commit -m "fix: bound all Pixiv pagination loops"
```

### Task 3: Provider fallback 尊重 retry 配置

**Files:**
- Modify: `src/pixiv_novel_sync/ai/providers.py`
- Modify: `tests/test_ai_providers_fallback.py`

- [ ] **Step 1: 写两类 Provider `max_retries=0` 请求计数 RED**

```python
@pytest.mark.parametrize("provider_type", ["openai_compatible", "anthropic"])
def test_empty_stream_fallback_honors_zero_retries(provider_type):
    provider, calls = empty_stream_then_failing_fallback(provider_type, max_retries=0)
    with pytest.raises(AIProviderError):
        list(provider.generate("prompt"))
    assert calls.non_stream == 1
```

- [ ] **Step 2: 运行 RED**

Run: `python -m pytest tests/test_ai_providers_fallback.py -q`
Expected: FAIL，fallback 发起 4 次请求。

- [ ] **Step 3: 删除硬编码 override 并统一 `max(0, config.max_retries)`**

stream fallback 调用 `_non_stream_generate()` 时不传 `max_retries_override=3`；Anthropic 普通非流式默认值与 OpenAI-compatible 一致。

- [ ] **Step 4: 运行 GREEN**

Run: `python -m pytest tests/test_ai_providers_fallback.py tests/test_ai_provider_completion.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/pixiv_novel_sync/ai/providers.py tests/test_ai_providers_fallback.py
git commit -m "fix: honor provider retry budgets"
```

### Task 4: 可恢复归档删除与原子 pending restore

**Files:**
- Modify: `src/pixiv_novel_sync/storage_files.py`
- Modify: `src/pixiv_novel_sync/web/utils.py`
- Modify: `src/pixiv_novel_sync/webapp.py`
- Modify: `src/pixiv_novel_sync/storage/pending_and_watermarks.py`
- Test: `tests/test_archive_integrity.py`
- Test: `tests/test_pending_deletions.py`

**Interfaces:**
- Produces: `FileStorage.stage_archive_deletion(paths, operation_id) -> ArchiveTrashOperation`
- Produces: `ArchiveTrashOperation.restore()`, `purge()`, `write_manifest()`
- Produces: `Database.restore_pending_deletion_atomic(deletion_id, source_key) -> dict | None`

- [ ] **Step 1: 写 DB 删除失败恢复文件和启动重放 RED**

```python
def test_archive_delete_restores_files_when_database_delete_fails(app, monkeypatch):
    original = seed_archive(app)
    monkeypatch.setattr(Database, "delete_novel", lambda *_: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    response = app.delete("/api/dashboard/novels/100")
    assert response.status_code == 500
    assert original.exists()
```

- [ ] **Step 2: 写 pending 状态与关系同事务 RED**

在关系恢复 SQL 人为抛错后，断言 pending 仍未处理且 source/subscription 未部分更新。

- [ ] **Step 3: 运行 RED**

Run: `python -m pytest tests/test_archive_integrity.py tests/test_pending_deletions.py -q`
Expected: FAIL，当前文件先删且 restore 分两次提交。

- [ ] **Step 4: 实现同卷 trash manifest 与恢复扫描**

manifest 仅存相对路径、operation ID 和阶段；所有路径经过 `_is_inside_storage`。移动完成后才执行数据库事务；异常恢复，提交后 purge。

- [ ] **Step 5: 把 pending restore 移入 storage transaction**

novel source、series subscription、pending 状态和救援刷新在一个 `BEGIN IMMEDIATE` 中完成。

- [ ] **Step 6: 运行 GREEN**

Run: `python -m pytest tests/test_archive_integrity.py tests/test_pending_deletions.py tests/test_rescue_storage.py -q`
Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add src/pixiv_novel_sync/storage_files.py src/pixiv_novel_sync/web/utils.py src/pixiv_novel_sync/webapp.py src/pixiv_novel_sync/storage/pending_and_watermarks.py tests/test_archive_integrity.py tests/test_pending_deletions.py
git commit -m "fix: make archive deletion recoverable"
```

### Task 5: 部署契约、datetime 和死代码

**Files:**
- Modify: `scripts/install_server.sh`
- Modify: `deploy/systemd/pixiv-novel-sync.service`
- Modify: `src/pixiv_novel_sync/settings.py`
- Modify: `src/pixiv_novel_sync/oauth_helper.py`
- Modify: `src/pixiv_novel_sync/preferences.py`
- Modify: `src/pixiv_novel_sync/preference_web.py`
- Modify: `src/pixiv_novel_sync/recommendations.py`
- Modify: `src/pixiv_novel_sync/storage_db.py`
- Modify: `src/pixiv_novel_sync/sync_engine.py`
- Modify: `src/pixiv_novel_sync/ai/service.py`
- Modify: `src/pixiv_novel_sync/ai/services/generation.py`
- Modify: `src/pixiv_novel_sync/storage/schema.py`
- Modify: `src/pixiv_novel_sync/storage/users.py`
- Modify: `src/pixiv_novel_sync/sync/utils.py`
- Modify: `src/pixiv_novel_sync/web/managers.py`
- Modify: `src/pixiv_novel_sync/webapp.py`
- Test: `tests/test_deployment_contract.py`
- Test: `tests/test_settings.py`

- [ ] **Step 1: 写 install/unit 路径一致性和类型注解 RED**

```python
def test_legacy_install_and_systemd_unit_share_identity_and_paths():
    install = Path("scripts/install_server.sh").read_text()
    unit = Path("deploy/systemd/pixiv-novel-sync.service").read_text()
    assert "pixivsync" in unit
    assert "/opt/pixiv-novel-sync/app/.venv/bin/pixiv-novel-sync" in unit
    assert "WorkingDirectory=/opt/pixiv-novel-sync/app" in unit

def test_simple_cron_type_hints_resolve():
    assert get_type_hints(_simple_cron_next_run)["base_dt"] is datetime
```

- [ ] **Step 2: 运行 RED**

Run: `python -m pytest tests/test_deployment_contract.py tests/test_settings.py -q`
Expected: FAIL。

- [ ] **Step 3: 统一脚本/unit 并修复模块级 datetime 导入**

删除已由局部函数导入覆盖的重复导入，保持 croniter 可选依赖行为。

- [ ] **Step 4: 清理 pyflakes 命中的普通未使用导入**

删除确认无调用方的普通导入；`AIWritingService`、`Database` 与 `webapp` 的兼容入口使用显式 `__all__` 或窄范围 `noqa`。`SyncJobManager/SyncJobState` 仍由 `tests/test_webapp_settings.py` 和外部兼容入口使用，必须保留，不删除实现。

- [ ] **Step 5: 运行 GREEN 和静态检查**

Run: `python -m pytest tests/test_deployment_contract.py tests/test_settings.py tests/test_webapp_jobs.py -q`
Run: `pyflakes src`
Expected: 测试通过；无 undefined name，兼容导出不再报告 unused。

- [ ] **Step 6: 提交**

```bash
git add scripts/install_server.sh deploy/systemd/pixiv-novel-sync.service src tests/test_deployment_contract.py tests/test_settings.py
git commit -m "fix: align deployment and remove static defects"
```

### Task 6: 域验证

- [ ] Run: `python -m pytest tests/test_task_log_leases.py tests/test_sync_pagination.py tests/test_ai_providers_fallback.py tests/test_archive_integrity.py tests/test_pending_deletions.py tests/test_deployment_contract.py -q`
- [ ] Run: `python -m compileall -q src tests`
- [ ] Run: `git diff --check`
- [ ] 更新计划 checkbox 并提交验证记录。
