"""非收藏路径重访作品时保留原归档树（审查 S10）。

只有收藏同步知道作品属于公开还是私密收藏。全量备份、追更章节写死 ``"public"``，
关注作者扫描传的是作品自身的可见性（0/1/2）：私密收藏的作品经这些路径重访且正文
有变时，整套文件写进 public 树、``restrict_value`` 翻成 public，private 树里的封面
成孤儿，EPUB 导出丢封面。
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from pixiv_novel_sync.jobs import services
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.storage_files import FileStorage
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "pixiv_novel_sync"
NOVEL_ID = 4242


class _OfflineStorage(FileStorage):
    """真实的目录布局与原子写，只把资源下载换成本地写文件。"""

    def download_asset(self, url, target, timeout, verify_ssl, proxy, max_retries=3, stop_requested=None):
        return self.write_bytes(Path(target), b"img")


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        pixiv=SimpleNamespace(timeout=1, verify_ssl=True, proxy=None, web_cookie=None, user_id=999),
        sync=SimpleNamespace(
            delay_seconds_between_pages=0,
            delay_seconds_between_items=0,
            delay_seconds_between_skips=0,
            max_items_per_run=None,
            max_pages_per_run=None,
            user_backup_max_pages_per_run=None,
            download_assets=True,
            write_markdown=True,
            write_raw_text=True,
        ),
        storage=SimpleNamespace(
            public_dir=tmp_path / "public",
            private_dir=tmp_path / "private",
            db_path=tmp_path / "state" / "test.db",
        ),
    )


def _novel(novel_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=novel_id,
        user=SimpleNamespace(id=7, name="author", account="acc"),
        caption="",
        tags=[],
        image_urls=SimpleNamespace(large=f"https://i.pximg.net/c/{novel_id}.jpg", medium=None, square_medium=None),
        series=None,
        title="私密收藏的作品",
        visible=True,
        x_restrict=0,
        text_length=4,
        total_bookmarks=1,
        total_view=1,
        create_date="2026-01-01T00:00:00+00:00",
    )


class _Api:
    def __init__(self, body: str) -> None:
        self.body = body

    def novel_detail(self, novel_id):
        return SimpleNamespace(novel=_novel(int(novel_id)))

    def webview_novel(self, novel_id):
        return {"text": self.body}

    def user_novels(self, **query):
        return SimpleNamespace(novels=[SimpleNamespace(id=NOVEL_ID)], next_url=None)

    def parse_qs(self, next_url):
        return None


def _novel_row(settings) -> tuple[str, str]:
    db = Database(settings.storage.db_path)
    try:
        row = db.conn.execute(
            "SELECT restrict_value, archive_dir FROM novels WHERE novel_id = ?", (NOVEL_ID,)
        ).fetchone()
        return str(row[0]), str(row[1])
    finally:
        db.close()


def test_user_backup_keeps_private_bookmark_in_private_tree(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    settings.storage.db_path.parent.mkdir(parents=True)
    db = Database(settings.storage.db_path)
    db.init_schema()
    try:
        # 先由私密收藏同步归档
        seed = BookmarkNovelSyncService(_Api("v1"), db, _OfflineStorage(settings), settings)
        assert seed._sync_novel(_novel(NOVEL_ID), "private", True, True, True, source_type="bookmark_private")["novels"] == 1
    finally:
        db.close()
    restrict_value, archive_dir = _novel_row(settings)
    assert restrict_value == "private"

    # 作者改了正文，全量备份重访这本
    monkeypatch.setattr(services, "_login", lambda s: _Api("v2"))
    monkeypatch.setattr(services, "FileStorage", _OfflineStorage)
    stats = services.run_user_backup_task(settings, user_id=7)

    assert stats["novels"] == 1
    restrict_value, archive_dir_after = _novel_row(settings)
    assert restrict_value == "private"
    assert archive_dir_after == archive_dir
    private_text = settings.storage.private_dir / archive_dir / "text.txt"
    assert private_text.read_text(encoding="utf-8").strip() == "v2"
    # 公开树里不能长出这本的副本
    assert not (settings.storage.public_dir / archive_dir).exists()


def test_new_novel_without_bookmark_context_lands_in_public_tree(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.storage.db_path.parent.mkdir(parents=True)
    db = Database(settings.storage.db_path)
    db.init_schema()
    try:
        service = BookmarkNovelSyncService(_Api("body"), db, _OfflineStorage(settings), settings)
        counters = service._sync_novel(_novel(NOVEL_ID), None, True, True, True, source_type="user_backup")
    finally:
        db.close()

    assert counters["novels"] == 1
    restrict_value, archive_dir = _novel_row(settings)
    assert restrict_value == "public"
    assert (settings.storage.public_dir / archive_dir / "text.txt").exists()


def test_only_bookmark_sync_passes_an_explicit_restrict() -> None:
    """`_sync_novel` 的第二个参数只能是收藏同步的 `restrict` 变量或 None。"""
    offenders: list[str] = []
    call_sites = 0
    for relative in ("sync_engine.py", "jobs/services.py"):
        path = SRC_ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "_sync_novel" or len(node.args) < 2:
                continue
            call_sites += 1
            arg = node.args[1]
            is_restrict_var = isinstance(arg, ast.Name) and arg.id == "restrict"
            is_none = isinstance(arg, ast.Constant) and arg.value is None
            if not (is_restrict_var or is_none):
                offenders.append(f"{relative}:{node.lineno} {ast.unparse(arg)}")
    assert offenders == []
    assert call_sites >= 4  # 收藏、关注扫描、追更章节、全量备份
