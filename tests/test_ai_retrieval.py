from __future__ import annotations

import socket
import sqlite3
import struct
import sys
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.ai import retrieval


@pytest.fixture(autouse=True)
def public_embedding_dns(monkeypatch):
    """请求替身使用保留测试域名；安全解析仍走真实校验逻辑。"""
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))])

from pixiv_novel_sync.ai.retrieval import APIEmbeddingRetriever, TFIDFRetriever, create_retriever


@pytest.mark.parametrize("kind,table", [
    ("tfidf", "retrieval_entries"),
    ("api", "retrieval_api_vectors"),
    ("local", "retrieval_vectors"),
])
def test_preparation_preserves_index_and_publication_needs_no_computation(tmp_path, monkeypatch, kind, table):
    if kind == "tfidf":
        retriever = TFIDFRetriever(tmp_path / "main.db")
    elif kind == "api":
        retriever = APIEmbeddingRetriever(tmp_path / "main.db", "https://api.example.test/v1", "test-key")
        monkeypatch.setattr(retriever.client, "embed", lambda texts: [[1.0, 0.0] for _ in texts])
    else:
        # 替身覆盖本地模型路径，无需安装模型、numpy 或下载文件。
        model = SimpleNamespace(encode=lambda texts, **kwargs: [[1.0, 0.0] for _ in texts])
        monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=lambda name: model))
        monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace(
            float32=float,
            array=lambda values, dtype: SimpleNamespace(tobytes=lambda: struct.pack("<2f", *values)),
        ))
        retriever = retrieval.EmbeddingRetriever(tmp_path / "main.db")
    try:
        retriever.index_chapter(1, 1, "已发布")
        prepared = retriever.prepare_chapter(1, 1, "新摘要", ["新事件"])
        assert retriever.conn.execute(f"SELECT content FROM {table} ORDER BY id").fetchall() == [("已发布",)]
        assert retriever.conn.in_transaction is False

        def fail(*args, **kwargs):
            raise AssertionError("发布阶段不能分词或计算向量")

        if kind == "tfidf":
            monkeypatch.setattr(retriever, "_tokenize", fail)
        elif kind == "api":
            monkeypatch.setattr(retriever.client, "embed", fail)
        else:
            monkeypatch.setattr(retriever.model, "encode", fail)
        retriever.publish_chapter(prepared)
        assert retriever.conn.execute(f"SELECT content FROM {table} ORDER BY id").fetchall() == [("新摘要",), ("新事件",)]
    finally:
        retriever.close()


def test_prepared_api_index_rejects_in_place_model_change(tmp_path, monkeypatch):
    retriever = APIEmbeddingRetriever(tmp_path / "main.db", "https://api.example.test/v1", "test-key")
    monkeypatch.setattr(retriever.client, "embed", lambda texts: [[1.0, 0.0] for _ in texts])
    try:
        retriever.index_chapter(1, 1, "已发布")
        prepared = retriever.prepare_chapter(1, 1, "待发布")
        retriever.client.model_name = "changed-model"
        with pytest.raises(RuntimeError, match="配置已变更"):
            retriever.publish_chapter(prepared)
        assert retriever.conn.execute("SELECT content FROM retrieval_api_vectors").fetchall() == [("已发布",)]
        assert retriever.conn.in_transaction is False
    finally:
        retriever.close()


def test_tfidf_delete_chapter_removes_only_target_chapter(tmp_path):
    retriever = TFIDFRetriever(tmp_path / "main.db")

    retriever.index_chapter(1, 1, "主角发现秘密线索", ["秘密被揭开"])
    retriever.index_chapter(1, 2, "配角追查秘密来源", ["线索指向旧案"])
    assert len(retriever.search(1, "秘密", top_k=10)) >= 2

    retriever.delete_chapter(1, 1)
    results = retriever.search(1, "秘密", top_k=10)

    assert all(item.chapter_number != 1 for item in results)
    assert any(item.chapter_number == 2 for item in results)


def test_tfidf_delete_project_removes_project_entries(tmp_path):
    retriever = TFIDFRetriever(tmp_path / "main.db")

    retriever.index_chapter(1, 1, "项目一秘密", [])
    retriever.index_chapter(2, 1, "项目二秘密", [])
    retriever.delete_project(1)

    assert retriever.search(1, "秘密") == []
    assert retriever.search(2, "秘密")


class FakeEmbeddingResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self):
        return self._payload


def test_api_embedding_retriever_uses_openai_compatible_endpoint(tmp_path, monkeypatch):
    calls = []

    def fake_post(url, headers, json, timeout, allow_redirects):
        assert allow_redirects is False
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        vectors = []
        for index, text in enumerate(json["input"]):
            vector = [1.0, 0.0] if "secret" in text.lower() else [0.0, 1.0]
            vectors.append({"index": index, "embedding": vector})
        return FakeEmbeddingResponse({"data": vectors})

    monkeypatch.setattr(retrieval.http_requests, "post", fake_post)
    retriever = APIEmbeddingRetriever(
        tmp_path / "main.db",
        base_url="https://api.example.test/v1",
        api_key="test-key",
        model_name="Qwen3-Embedding-8B",
        timeout=12,
    )

    retriever.index_chapter(1, 1, "secret clue appears", ["old case"])
    retriever.index_chapter(1, 2, "warm cooking scene", [])
    results = retriever.search(1, "secret", top_k=1)

    assert results[0].chapter_number == 1
    assert calls[0]["url"] == "https://api.example.test/v1/embeddings"
    assert calls[0]["headers"]["Authorization"] == "Bearer test-key"
    assert calls[0]["json"]["model"] == "Qwen3-Embedding-8B"
    assert calls[0]["timeout"] == 12


def test_api_embedding_retriever_empty_index_search_skips_api_call(tmp_path, monkeypatch):
    calls = []

    def fake_post(url, headers, json, timeout, allow_redirects):
        assert allow_redirects is False
        calls.append(json["input"])
        return FakeEmbeddingResponse({"data": []})

    monkeypatch.setattr(retrieval.http_requests, "post", fake_post)
    retriever = APIEmbeddingRetriever(
        tmp_path / "main.db",
        base_url="https://api.example.test/v1",
        api_key="test-key",
        model_name="Qwen3-Embedding-8B",
    )

    assert retriever.search(1, "secret") == []
    assert calls == []



def test_api_embedding_retriever_stores_vectors_as_blob(tmp_path, monkeypatch):
    def fake_post(url, headers, json, timeout, allow_redirects):
        assert allow_redirects is False
        return FakeEmbeddingResponse({
            "data": [
                {"index": index, "embedding": [1.0, 0.0]}
                for index, _text in enumerate(json["input"])
            ]
        })

    monkeypatch.setattr(retrieval.http_requests, "post", fake_post)
    retriever = APIEmbeddingRetriever(
        tmp_path / "main.db",
        base_url="https://api.example.test/v1",
        api_key="test-key",
        model_name="Qwen3-Embedding-8B",
    )

    retriever.index_chapter(1, 1, "secret clue appears", [])
    row = retriever.conn.execute(
        "SELECT embedding_blob, embedding_json FROM retrieval_api_vectors WHERE project_id = ? AND chapter_number = ?",
        (1, 1),
    ).fetchone()

    assert row[0]
    assert row[1] is None


def test_api_embedding_retriever_skips_unchanged_chapter(tmp_path, monkeypatch):
    calls = []

    def fake_post(url, headers, json, timeout, allow_redirects):
        assert allow_redirects is False
        calls.append(json["input"])
        return FakeEmbeddingResponse({
            "data": [
                {"index": index, "embedding": [1.0, 0.0]}
                for index, _text in enumerate(json["input"])
            ]
        })

    monkeypatch.setattr(retrieval.http_requests, "post", fake_post)
    retriever = APIEmbeddingRetriever(
        tmp_path / "main.db",
        base_url="https://api.example.test/v1",
        api_key="test-key",
        model_name="Qwen3-Embedding-8B",
    )

    retriever.index_chapter(1, 1, "secret clue appears", ["old case"])
    retriever.index_chapter(1, 1, "secret clue appears", ["old case"])

    assert calls == [["secret clue appears", "old case"]]


def test_create_retriever_prefers_api_embeddings_when_configured(tmp_path):
    retriever = create_retriever(
        tmp_path / "main.db",
        api_base_url="https://api.example.test/v1",
        api_key="test-key",
        model_name="Qwen3-Embedding-8B",
    )

    assert isinstance(retriever, APIEmbeddingRetriever)


def test_create_retriever_falls_back_to_tfidf_when_api_retriever_unavailable(tmp_path, monkeypatch):
    def fail_api_retriever(*args, **kwargs):
        raise RuntimeError("embedding init failed")

    monkeypatch.setattr(retrieval, "APIEmbeddingRetriever", fail_api_retriever)

    retriever = create_retriever(
        tmp_path / "main.db",
        api_base_url="https://api.example.test/v1",
        api_key="test-key",
        model_name="Qwen3-Embedding-8B",
    )

    assert isinstance(retriever, TFIDFRetriever)


@pytest.mark.parametrize("kind", ["tfidf", "api"])
def test_failed_reindex_preserves_previous_chapter_and_releases_transaction(tmp_path, monkeypatch, kind):
    if kind == "tfidf":
        retriever = TFIDFRetriever(tmp_path / "main.db")
        table = "retrieval_entries"
    else:
        retriever = APIEmbeddingRetriever(tmp_path / "main.db", "https://api.example.test/v1", "test-key")
        monkeypatch.setattr(retriever.client, "embed", lambda texts: [[1.0, 0.0] for _ in texts])
        table = "retrieval_api_vectors"
    try:
        retriever.index_chapter(1, 1, "original chapter")
        retriever.conn.execute(f"""CREATE TRIGGER reject_new BEFORE INSERT ON {table}
            WHEN NEW.content='replacement' BEGIN SELECT RAISE(ABORT, 'index rejected'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="index rejected"):
            retriever.index_chapter(1, 1, "replacement")
        assert retriever.conn.in_transaction is False
        # An unrelated later commit must not persist the failed attempt's DELETE.
        retriever.index_chapter(1, 2, "other chapter")
        assert retriever.conn.execute(f"SELECT content FROM {table} WHERE chapter_number=1").fetchall() == [
            ("original chapter",)
        ]
    finally:
        retriever.close()


@pytest.mark.parametrize("data", [
    [{"index": 0, "embedding": [1.0]}, {"index": 0, "embedding": [2.0]}],
    [{"index": 0, "embedding": [1.0]}, {"index": 2, "embedding": [2.0]}],
    [{"embedding": [1.0]}, {"index": 1, "embedding": [2.0]}],
    [{"index": False, "embedding": [1.0]}, {"index": 1, "embedding": [2.0]}],
    [{"index": 0, "embedding": []}, {"index": 1, "embedding": []}],
    [{"index": 0, "embedding": [float("nan")]}, {"index": 1, "embedding": [2.0]}],
    [{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [2.0, 3.0]}],
])
def test_embedding_response_must_preserve_input_alignment(monkeypatch, data):
    monkeypatch.setattr(retrieval.http_requests, "post", lambda *args, **kwargs: FakeEmbeddingResponse({"data": data}))
    client = retrieval.OpenAICompatibleEmbeddingClient("https://api.example.test/v1", "test-key", "embedding")
    with pytest.raises(RuntimeError, match="Embedding API"):
        client.embed(["first", "second"])


def test_embedding_response_reorders_valid_indices(monkeypatch):
    data = [{"index": 1, "embedding": [2.0]}, {"index": 0, "embedding": [1.0]}]
    monkeypatch.setattr(retrieval.http_requests, "post", lambda *args, **kwargs: FakeEmbeddingResponse({"data": data}))
    client = retrieval.OpenAICompatibleEmbeddingClient("https://api.example.test/v1", "test-key", "embedding")
    assert client.embed(["first", "second"]) == [[1.0], [2.0]]
