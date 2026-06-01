from __future__ import annotations

from pixiv_novel_sync.ai.retrieval import TFIDFRetriever


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
