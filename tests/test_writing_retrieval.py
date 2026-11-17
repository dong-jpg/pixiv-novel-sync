from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from pixiv_novel_sync.ai import retrieval
from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.storage_db import Database


@pytest.fixture
def writing_index(tmp_path, monkeypatch):
    monkeypatch.setenv("PIXIV_NOVEL_SYNC_EMBEDDING_BASE_URL", "https://embedding.example.test/v1")
    monkeypatch.setenv("PIXIV_NOVEL_SYNC_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("PIXIV_NOVEL_SYNC_EMBEDDING_MODEL", "test-model")
    monkeypatch.setenv("PIXIV_NOVEL_SYNC_EMBEDDING_TIMEOUT", "5")
    monkeypatch.setenv("PIXIV_NOVEL_SYNC_USE_EMBEDDINGS", "false")
    monkeypatch.setattr(retrieval, "validate_base_url", lambda url: url)
    monkeypatch.setattr(retrieval.OpenAICompatibleEmbeddingClient, "embed", lambda self, texts: [[1.0, 0.0] for _ in texts])
    path = tmp_path / "writing.db"
    db = Database(path)
    db.init_schema()
    pid = db.create_ai_writing_project({"name": "测试"})
    cid = db.create_ai_chapter({"project_id": pid, "chapter_number": 1, "content": "正文", "summary": "已发布"})
    service = AIWritingService(path)
    service.index_chapter_for_retrieval(pid, cid)
    try:
        yield db, service, pid, cid, service._get_retriever()
    finally:
        service.close()
        db.close()


def _contents(retriever):
    return retriever.conn.execute("SELECT content FROM retrieval_api_vectors ORDER BY id").fetchall()


def _block_embedding(monkeypatch, retriever):
    entered = Event()
    release = Event()

    def embed(texts):
        entered.set()
        assert release.wait(10), "测试调度超时"
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(retriever.client, "embed", embed)
    return entered, release


def test_slow_embedding_allows_business_write_and_cannot_replace_newer_index(writing_index, monkeypatch):
    db, first, pid, cid, retriever = writing_index
    second = AIWritingService(first.db_path)
    entered, release = _block_embedding(monkeypatch, retriever)

    def write_and_index():
        other = second._db()
        try:
            other.conn.execute("PRAGMA busy_timeout=500")
            other.create_ai_writing_project({"name": "独立业务写入"})
        finally:
            other.close()
        second.update_chapter(cid, {"summary": "最新索引"})

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            old = pool.submit(first.update_chapter, cid, {"summary": "慢速旧计算"})
            try:
                assert entered.wait(5)
                pool.submit(write_and_index).result(timeout=5)
                assert db.get_ai_chapter(cid)["summary"] == "最新索引"
                assert _contents(retriever) == [("最新索引",)]
            finally:
                release.set()
            old.result(timeout=5)
        assert _contents(retriever) == [("最新索引",)]
    finally:
        second.close()


@pytest.mark.parametrize("change", ["empty", "delete_chapter", "delete_project"])
def test_late_embedding_cannot_restore_cleared_or_deleted_chapter(writing_index, monkeypatch, change):
    db, service, pid, cid, retriever = writing_index
    entered, release = _block_embedding(monkeypatch, retriever)
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(service.update_chapter, cid, {"summary": "慢速旧计算"})
        try:
            assert entered.wait(5)
            if change == "empty":
                pool.submit(service.update_chapter, cid, {"summary": "", "key_events": []}).result(timeout=5)
            elif change == "delete_chapter":
                pool.submit(service.delete_chapter, cid).result(timeout=5)
            else:
                pool.submit(service.delete_writing_project, pid).result(timeout=5)
            assert _contents(retriever) == []
        finally:
            release.set()
        old.result(timeout=5)
    assert _contents(retriever) == []
    if change != "empty":
        assert db.get_ai_chapter(cid) is None


def test_embedding_failure_preserves_published_index(writing_index, monkeypatch):
    db, service, _, cid, retriever = writing_index

    def fail(texts):
        raise RuntimeError("向量计算失败")

    monkeypatch.setattr(retriever.client, "embed", fail)
    with pytest.raises(RuntimeError, match="向量计算失败"):
        service.update_chapter(cid, {"summary": "新摘要"})
    assert db.get_ai_chapter(cid)["summary"] == "新摘要"
    assert _contents(retriever) == [("已发布",)]
    assert retriever.conn.in_transaction is False


@pytest.mark.parametrize("field,value", [
    ("content", "新正文"),
    ("summary", "新摘要"),
    ("key_events_json", '["新事件"]'),
    ("chapter_revision", 99),
])
def test_publish_rechecks_revision_and_content_snapshot(writing_index, monkeypatch, field, value):
    db, service, pid, cid, retriever = writing_index
    db.update_ai_chapter(cid, {"summary": "慢速旧计算"})
    entered, release = _block_embedding(monkeypatch, retriever)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(service.index_chapter_for_retrieval, pid, cid)
        try:
            assert entered.wait(5)
            # 模拟不增加 revision 的旧写入路径，仍必须核对真实正文/摘要/事件。
            with db.transaction():
                db.conn.execute(f"UPDATE ai_chapters SET {field} = ? WHERE id = ?", (value, cid))
        finally:
            release.set()
        old.result(timeout=5)
    assert _contents(retriever) == [("已发布",)]


@pytest.mark.parametrize("setting,value", [
    ("PIXIV_NOVEL_SYNC_EMBEDDING_BASE_URL", "https://changed.example.test/v1"),
    ("PIXIV_NOVEL_SYNC_EMBEDDING_API_KEY", "changed-test-key"),
    ("PIXIV_NOVEL_SYNC_EMBEDDING_MODEL", "changed-model"),
    ("PIXIV_NOVEL_SYNC_EMBEDDING_TIMEOUT", "12"),
    ("PIXIV_NOVEL_SYNC_USE_EMBEDDINGS", "true"),
])
def test_changed_retriever_settings_discard_pending_vectors(writing_index, monkeypatch, setting, value):
    _, service, _, cid, retriever = writing_index
    entered, release = _block_embedding(monkeypatch, retriever)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(service.update_chapter, cid, {"summary": "慢速旧计算"})
        try:
            assert entered.wait(5)
            monkeypatch.setenv(setting, value)
        finally:
            release.set()
        old.result(timeout=5)
    assert _contents(retriever) == [("已发布",)]


def test_replaced_retriever_does_not_publish_to_closed_connection(writing_index, monkeypatch):
    _, service, _, cid, retriever = writing_index
    entered, release = _block_embedding(monkeypatch, retriever)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(service.update_chapter, cid, {"summary": "慢速旧计算"})
        try:
            assert entered.wait(5)
            monkeypatch.setenv("PIXIV_NOVEL_SYNC_EMBEDDING_MODEL", "changed-model")
            current = service._get_retriever()
            assert current is not retriever
            service.update_chapter(cid, {"summary": "新模型结果"})
        finally:
            release.set()
        old.result(timeout=5)
    assert _contents(current) == [("已发布",), ("新模型结果",)]
