from __future__ import annotations

from pixiv_novel_sync.ai import retrieval
from pixiv_novel_sync.ai.retrieval import APIEmbeddingRetriever, TFIDFRetriever, create_retriever


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

    def fake_post(url, headers, json, timeout):
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


def test_api_embedding_retriever_stores_vectors_as_blob(tmp_path, monkeypatch):
    def fake_post(url, headers, json, timeout):
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

    def fake_post(url, headers, json, timeout):
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
