"""审计整改回归测试。

覆盖：连接跨线程生命周期、init_schema 不再误杀 running 日志、归档删除
trash 安全顺序、delete_user 救援目录清理、pending 恢复原子性、
共享任务提交 TOCTOU、调度器 submit 退避、登录限流上限、缓存扫描截断、
settings datetime 注解。
"""
from __future__ import annotations

import threading
import time
import typing
from pathlib import Path

import pytest

from pixiv_novel_sync import settings as settings_module
from pixiv_novel_sync.models import NovelRecord, NovelTextRecord, UserRecord
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.storage_files import FileStorage
from pixiv_novel_sync.web.managers import AutoSyncScheduler
from pixiv_novel_sync.webapp import _LoginFailureTracker, create_app
import pixiv_novel_sync.webapp as webapp_module


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str = ""):
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "audit-test-secret")
    if token:
        monkeypatch.setenv("DASHBOARD_TOKEN", token)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    app.config["TESTING"] = True
    return app


def _seed_novel_with_files(db: Database, settings) -> tuple[Path, Path]:
    """插入 novel 100（作者 1）并在磁盘写归档文件，返回 (novel_dir, asset_path)。"""
    db.upsert_user(UserRecord(user_id=1, name="作者A", account="a", raw_json="{}"))
    db.upsert_novel(NovelRecord(
        novel_id=100, user_id=1, series_id=None, title="测试小说", caption="简介",
        visible=True, restrict="public", x_restrict=0, text_length=6000,
        total_bookmarks=10, total_views=100,
        cover_url="https://i.pximg.net/c/cover.jpg", tags_json="[]",
        create_date=None, raw_json="{}", meta_hash="meta",
    ))
    db.upsert_novel_text(NovelTextRecord(
        novel_id=100, text_raw="正文", text_markdown=None, text_hash="text"
    ))
    storage = FileStorage(settings)
    novel_dir = storage.novel_dir("public", 1, "作者A", 100, "测试小说")
    asset_path = storage.asset_path(novel_dir, "cover", "cover.jpg")
    storage.write_text(novel_dir / "text.txt", "正文")
    storage.write_bytes(asset_path, b"image")
    db.record_asset(100, "cover", "https://i.pximg.net/c/cover.jpg", str(asset_path), "hash")
    return novel_dir, asset_path


# ---------------------------------------------------------------------------
# 1. DatabaseConnection 跨线程生命周期
# ---------------------------------------------------------------------------

def test_conn_rebuilt_after_close_in_other_thread(tmp_path: Path) -> None:
    db = Database(tmp_path / "conn.db")
    db.init_schema()
    results: list = []

    ready = threading.Event()
    resume = threading.Event()

    def worker() -> None:
        db.conn.execute("SELECT 1")
        ready.set()
        resume.wait(5)
        # close() 之后旧连接失效，conn 属性应重建而非返回已关闭连接
        try:
            row = db.conn.execute("SELECT 1").fetchone()
            results.append(row[0])
        except Exception as exc:  # pragma: no cover - 失败路径
            results.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    ready.wait(5)
    db.close()
    resume.set()
    thread.join(5)
    assert results == [1]
    db.close()


def test_dead_thread_connections_are_pruned(tmp_path: Path) -> None:
    db = Database(tmp_path / "prune.db")
    db.init_schema()

    def worker() -> None:
        db.conn.execute("SELECT 1")

    for _ in range(5):
        t = threading.Thread(target=worker)
        t.start()
        t.join(5)
    # 主线程新建连接时会清理死线程遗留连接；最多允许残留最后一个死线程条目
    db.conn.execute("SELECT 1")
    with db._lock:
        alive = [th for th in db._all_conns.values() if th.is_alive()]
        assert len(db._all_conns) <= len(alive) + 1
        assert len(db._all_conns) <= 2
    db.close()


def test_transaction_does_not_block_other_thread_reads(tmp_path: Path) -> None:
    db = Database(tmp_path / "txn.db")
    db.init_schema()
    in_txn = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with db.transaction():
            in_txn.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert in_txn.wait(5)
    started = time.monotonic()
    # 旧实现 transaction() 全程持 RLock，其他线程首次创建连接会被卡住
    row = db.conn.execute("SELECT COUNT(*) FROM users").fetchone()
    elapsed = time.monotonic() - started
    release.set()
    thread.join(5)
    assert row[0] == 0
    assert elapsed < 2.0
    db.close()


def test_nested_transaction_depth_still_works(tmp_path: Path) -> None:
    db = Database(tmp_path / "nested.db")
    db.init_schema()
    with db.transaction():
        with db.transaction():
            db.conn.execute(
                "INSERT INTO users (user_id, name, raw_json) VALUES (1, 'n', '{}')"
            )
        assert db._transaction_depth == 1
    assert db._transaction_depth == 0
    assert db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    db.close()


# ---------------------------------------------------------------------------
# 2. init_schema 不再误杀 running 任务日志
# ---------------------------------------------------------------------------

def test_init_schema_keeps_running_logs(tmp_path: Path) -> None:
    path = tmp_path / "logs.db"
    db = Database(path)
    db.init_schema()
    log_id = db.create_task_log(task_type="bookmark", task_name="同步收藏")
    db.close()

    other = Database(path)
    other.init_schema()  # 模拟并发 Web 请求打开数据库
    status = other.conn.execute(
        "SELECT status FROM task_logs WHERE id = ?", (log_id,)
    ).fetchone()[0]
    assert status == "running"

    assert other.fail_stale_task_logs() == 1
    status = other.conn.execute(
        "SELECT status FROM task_logs WHERE id = ?", (log_id,)
    ).fetchone()[0]
    assert status == "failed"
    other.close()


def test_create_app_fails_stale_running_logs_once(tmp_path: Path, monkeypatch) -> None:
    db_path = Path(str(tmp_path / "state" / "test.db"))
    db = Database(db_path)
    db.init_schema()
    log_id = db.create_task_log(task_type="bookmark", task_name="同步收藏")
    db.close()

    _make_app(tmp_path, monkeypatch)

    db = Database(db_path)
    db.init_schema()
    status = db.conn.execute(
        "SELECT status FROM task_logs WHERE id = ?", (log_id,)
    ).fetchone()[0]
    db.close()
    assert status == "failed"


# ---------------------------------------------------------------------------
# 4. 归档删除的 trash 安全顺序
# ---------------------------------------------------------------------------

def test_delete_novel_removes_files_after_db_success(tmp_path, monkeypatch) -> None:
    app = _make_app(tmp_path, monkeypatch)
    settings = settings_module.load_settings(None, str(tmp_path / ".env"))
    db = Database(settings.storage.db_path)
    db.init_schema()
    novel_dir, _ = _seed_novel_with_files(db, settings)
    db.close()

    resp = app.test_client().delete(
        "/api/dashboard/novels/100", environ_base={"REMOTE_ADDR": "127.0.0.1"}
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["archive_cleanup"]["dirs_removed"] == 1
    assert not novel_dir.exists()
    # trash 目录已清空
    trash_root = settings.storage.public_dir.parent / ".trash"
    assert not any(trash_root.iterdir()) if trash_root.exists() else True

    db = Database(settings.storage.db_path)
    db.init_schema()
    assert db.conn.execute("SELECT COUNT(*) FROM novels").fetchone()[0] == 0
    db.close()


def test_delete_novel_restores_files_when_db_delete_fails(tmp_path, monkeypatch) -> None:
    app = _make_app(tmp_path, monkeypatch)
    settings = settings_module.load_settings(None, str(tmp_path / ".env"))
    db = Database(settings.storage.db_path)
    db.init_schema()
    novel_dir, asset_path = _seed_novel_with_files(db, settings)
    db.close()

    def boom(self, novel_id):
        raise RuntimeError("db delete failed")

    monkeypatch.setattr(Database, "delete_novel", boom)
    resp = app.test_client().delete(
        "/api/dashboard/novels/100", environ_base={"REMOTE_ADDR": "127.0.0.1"}
    )
    assert resp.status_code == 500
    # 文件应被移回原位
    assert novel_dir.exists()
    assert (novel_dir / "text.txt").read_text(encoding="utf-8") == "正文"
    assert asset_path.exists()


# ---------------------------------------------------------------------------
# 5. delete_user 清理救援目录
# ---------------------------------------------------------------------------

def _seed_rescue_novel(db: Database, novel_id: int, *, series_id: int | None = None) -> None:
    db.conn.execute(
        "INSERT OR IGNORE INTO users (user_id, name, raw_json) VALUES (2, '作者', '{}')"
    )
    db.conn.execute(
        """
        INSERT INTO novels (
            novel_id, user_id, series_id, title, visible, restrict_value, x_restrict,
            text_length, total_bookmarks, total_views, tags_json, raw_json,
            meta_hash, status
        ) VALUES (?, 2, ?, '小说', 1, 'public', 0, 6, 0, 0, '[]', '{}', ?, 'deleted')
        """,
        (novel_id, series_id, f"h-{novel_id}"),
    )
    db.upsert_novel_text(NovelTextRecord(
        novel_id=novel_id, text_raw="救援正文", text_markdown=None, text_hash=f"t-{novel_id}"
    ))
    db.conn.commit()


def test_delete_user_cleans_rescue_catalog_rows(tmp_path: Path) -> None:
    db = Database(tmp_path / "rescue.db")
    db.init_schema()
    db.conn.execute(
        """
        INSERT INTO series (series_id, title, user_id, total_novels, status)
        VALUES (40, '系列', 2, 1, 'deleted')
        """
    )
    _seed_rescue_novel(db, 31, series_id=40)
    db.rebuild_rescue_catalog()
    assert db.get_rescue_catalog_item("novel", 31) is not None or \
        db.get_rescue_catalog_item("series", 40) is not None

    db.delete_user(2)

    assert db.get_rescue_catalog_item("novel", 31) is None
    assert db.conn.execute(
        "SELECT COUNT(*) FROM rescue_catalog_sources WHERE item_type = 'novel' AND item_id = 31"
    ).fetchone()[0] == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM rescue_catalog_memberships WHERE novel_id = 31"
    ).fetchone()[0] == 0
    # 父系列被刷新：无成员正文后不应再作为可救援系列存在
    assert db.get_rescue_catalog_item("series", 40) is None
    db.close()


# ---------------------------------------------------------------------------
# 6. pending deletion 恢复原子化
# ---------------------------------------------------------------------------

def test_restore_pending_deletion_atomic_novel_and_series(tmp_path: Path) -> None:
    db = Database(tmp_path / "pending.db")
    db.init_schema()
    _seed_rescue_novel(db, 51)
    db.conn.execute(
        """
        INSERT INTO series (series_id, title, user_id, is_subscribed, status)
        VALUES (60, '系列', 2, 0, 'unknown')
        """
    )
    db.conn.commit()
    db.add_pending_deletion("novel", 51, "unbookmarked", "小说", "作者", "", source_type="bookmark_public")
    db.add_pending_deletion("series", 60, "unsubscribed", "系列", "作者", "")
    rows = db.conn.execute("SELECT id, item_type FROM pending_deletions ORDER BY id").fetchall()
    novel_deletion = next(r["id"] for r in rows if r["item_type"] == "novel")
    series_deletion = next(r["id"] for r in rows if r["item_type"] == "series")

    record = db.restore_pending_deletion_atomic(novel_deletion, bookmark_source_key="777")
    assert record is not None and record["item_id"] == 51
    source = db.conn.execute(
        "SELECT source_type, source_key FROM sources WHERE novel_id = 51"
    ).fetchone()
    assert tuple(source) == ("bookmark_public", "777")

    record = db.restore_pending_deletion_atomic(series_deletion, bookmark_source_key="777")
    assert record is not None and record["item_id"] == 60
    assert db.conn.execute(
        "SELECT is_subscribed FROM series WHERE series_id = 60"
    ).fetchone()[0] == 1

    # 重复恢复返回 None
    assert db.restore_pending_deletion_atomic(novel_deletion) is None
    db.close()


def test_restore_pending_deletion_atomic_rolls_back_on_failure(tmp_path, monkeypatch) -> None:
    db = Database(tmp_path / "pending2.db")
    db.init_schema()
    _seed_rescue_novel(db, 52)
    db.add_pending_deletion("novel", 52, "unbookmarked", "小说", "作者", "", source_type="bookmark_public")
    deletion_id = db.conn.execute("SELECT id FROM pending_deletions").fetchone()[0]
    # 制造来源写入失败：删除 sources 表模拟中途异常
    db.conn.execute("ALTER TABLE sources RENAME TO sources_backup")
    db.conn.commit()
    with pytest.raises(Exception):
        db.restore_pending_deletion_atomic(deletion_id, bookmark_source_key="777")
    # 状态未被部分提交，仍是 pending
    assert db.conn.execute(
        "SELECT status FROM pending_deletions WHERE id = ?", (deletion_id,)
    ).fetchone()[0] == "pending"
    db.close()


# ---------------------------------------------------------------------------
# 7. _submit_shared_job TOCTOU
# ---------------------------------------------------------------------------

def test_submit_shared_job_is_atomic_under_concurrency(tmp_path, monkeypatch) -> None:
    app = _make_app(tmp_path, monkeypatch)
    submit = app.config["submit_shared_web_job"]
    settings = settings_module.load_settings(None, str(tmp_path / ".env"))

    results: list[str] = []
    barrier = threading.Barrier(8)

    def attempt(i: int) -> None:
        barrier.wait(5)
        try:
            spec = webapp_module._web_job_spec(["bookmark"])
            submit(spec, settings, "bookmark", f"任务{i}", run_async=False)
            results.append("ok")
        except RuntimeError:
            results.append("busy")

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert results.count("ok") == 1
    assert results.count("busy") == 7


# ---------------------------------------------------------------------------
# 8/9. 调度器：submit 失败返回 False（供短退避），成功返回 True
# ---------------------------------------------------------------------------

def test_run_single_task_returns_false_on_submit_failure() -> None:
    scheduler = AutoSyncScheduler(
        None, None,
        submit_task=lambda settings, task: (_ for _ in ()).throw(RuntimeError("busy")),
        run_task=lambda job_id: None,
    )
    assert scheduler._run_single_task(object(), "bookmarks") is False

    scheduler2 = AutoSyncScheduler(
        None, None,
        submit_task=lambda settings, task: None,
        run_task=lambda job_id: None,
    )
    assert scheduler2._run_single_task(object(), "bookmarks") is False


def test_run_single_task_returns_true_on_success() -> None:
    from types import SimpleNamespace

    state = SimpleNamespace(job_id="j1")
    scheduler = AutoSyncScheduler(
        None, None,
        submit_task=lambda settings, task: state,
        run_task=lambda job_id: None,
    )
    scheduler._running = True
    assert scheduler._run_single_task(object(), "bookmarks") is True


def test_scheduler_submit_retry_constant_is_short() -> None:
    from pixiv_novel_sync.web.managers import SCHEDULER_SUBMIT_RETRY_SECONDS

    assert 0 < SCHEDULER_SUBMIT_RETRY_SECONDS <= 600


# ---------------------------------------------------------------------------
# 10. 登录失败追踪器上限
# ---------------------------------------------------------------------------

def test_login_failure_tracker_bounded() -> None:
    tracker = _LoginFailureTracker(max_entries=100)
    now = time.time()
    for i in range(300):
        tracker.record_failure(f"10.0.{i // 256}.{i % 256}", now + i * 0.001)
    assert len(tracker) <= 100
    # 最新条目仍在
    assert tracker.is_blocked("nope") is False


def test_login_failure_tracker_blocks_after_max_failures() -> None:
    tracker = _LoginFailureTracker()
    now = time.time()
    for _ in range(5):
        tracker.record_failure("1.2.3.4", now)
    assert tracker.is_blocked("1.2.3.4", now) is True
    # 窗口过期后解除
    assert tracker.is_blocked("1.2.3.4", now + 301) is False
    tracker.record_failure("5.6.7.8", now)
    tracker.clear("5.6.7.8")
    assert tracker.is_blocked("5.6.7.8", now) is False


# ---------------------------------------------------------------------------
# 11. cache_status 遍历截断
# ---------------------------------------------------------------------------

def test_cache_status_truncates_large_directories(tmp_path, monkeypatch) -> None:
    cache_dir = tmp_path / "nginx_cache"
    cache_dir.mkdir()
    for i in range(5):
        (cache_dir / f"f{i}.bin").write_bytes(b"x" * 10)
    monkeypatch.setenv("PIXIV_NGINX_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(webapp_module, "_CACHE_SCAN_MAX_FILES", 3)

    app = _make_app(tmp_path, monkeypatch)
    resp = app.test_client().get(
        "/api/cache/status", environ_base={"REMOTE_ADDR": "127.0.0.1"}
    )
    payload = resp.get_json()
    assert payload["exists"] is True
    assert payload["truncated"] is True
    assert payload["file_count"] == 3


# ---------------------------------------------------------------------------
# 12. settings.py datetime 注解可解析
# ---------------------------------------------------------------------------

def test_settings_annotations_resolve() -> None:
    hints = typing.get_type_hints(settings_module._simple_cron_next_run)
    assert "base_dt" in hints
