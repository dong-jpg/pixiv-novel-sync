"""可选的语义检索模块。

支持两种模式：
1. 有 sentence-transformers 时：使用本地 embedding 模型做语义检索
2. 无依赖时：使用 TF-IDF 关键词匹配（零依赖 fallback）

用法：
    retriever = create_retriever(db_path)
    retriever.index_chapter(project_id, chapter_number, summary, key_events)
    results = retriever.search(project_id, query, top_k=5)
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from collections import Counter
from pathlib import Path
from typing import Any


class RetrievalEntry:
    __slots__ = ("project_id", "chapter_number", "content", "entry_type", "score")

    def __init__(self, project_id: int, chapter_number: int, content: str, entry_type: str, score: float = 0.0):
        self.project_id = project_id
        self.chapter_number = chapter_number
        self.content = content
        self.entry_type = entry_type
        self.score = score


class BaseRetriever:
    """检索器基类。"""

    def index_chapter(self, project_id: int, chapter_number: int, summary: str, key_events: list[str] | None = None) -> None:
        raise NotImplementedError

    def search(self, project_id: int, query: str, top_k: int = 5) -> list[RetrievalEntry]:
        raise NotImplementedError

    def delete_project(self, project_id: int) -> None:
        raise NotImplementedError


class TFIDFRetriever(BaseRetriever):
    """基于 TF-IDF 的轻量检索器（零外部依赖）。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.parent / "ai_retrieval.db"
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS retrieval_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                chapter_number INTEGER NOT NULL,
                content TEXT NOT NULL,
                entry_type TEXT NOT NULL,
                tokens_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_retrieval_project ON retrieval_entries(project_id)")
        self.conn.commit()

    def index_chapter(self, project_id: int, chapter_number: int, summary: str, key_events: list[str] | None = None) -> None:
        with self._lock:
            # 删除旧条目
            self.conn.execute(
                "DELETE FROM retrieval_entries WHERE project_id = ? AND chapter_number = ?",
                (project_id, chapter_number),
            )
            # 索引摘要
            if summary and summary.strip():
                tokens = self._tokenize(summary)
                self.conn.execute(
                    "INSERT INTO retrieval_entries (project_id, chapter_number, content, entry_type, tokens_json) VALUES (?, ?, ?, ?, ?)",
                    (project_id, chapter_number, summary, "summary", json.dumps(tokens, ensure_ascii=False)),
                )
            # 索引关键事件
            if key_events:
                for event in key_events:
                    if event and event.strip():
                        tokens = self._tokenize(event)
                        self.conn.execute(
                            "INSERT INTO retrieval_entries (project_id, chapter_number, content, entry_type, tokens_json) VALUES (?, ?, ?, ?, ?)",
                            (project_id, chapter_number, event, "key_event", json.dumps(tokens, ensure_ascii=False)),
                        )
            self.conn.commit()

    def search(self, project_id: int, query: str, top_k: int = 5) -> list[RetrievalEntry]:
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        with self._lock:
            rows = self.conn.execute(
                "SELECT project_id, chapter_number, content, entry_type, tokens_json FROM retrieval_entries WHERE project_id = ?",
                (project_id,),
            ).fetchall()

        if not rows:
            return []

        # 计算 IDF（一次性，不在循环里重算）
        doc_count = len(rows)
        df: Counter[str] = Counter()
        parsed_docs: list[tuple[Any, list[str]]] = []
        for row in rows:
            tokens = json.loads(row[4])
            parsed_docs.append((row, tokens))
            for t in set(tokens):
                df[t] += 1

        idf_cache: dict[str, float] = {}

        def idf_of(term: str) -> float:
            v = idf_cache.get(term)
            if v is None:
                v = math.log((doc_count + 1) / (df.get(term, 0) + 1)) + 1
                idf_cache[term] = v
            return v

        # 计算查询向量
        query_tf = Counter(query_tokens)
        query_vec: dict[str, float] = {term: freq * idf_of(term) for term, freq in query_tf.items()}
        norm_q = math.sqrt(sum(v * v for v in query_vec.values()))

        # 计算每个文档的相似度
        results: list[RetrievalEntry] = []
        for row, doc_tokens in parsed_docs:
            doc_tf = Counter(doc_tokens)
            doc_vec: dict[str, float] = {term: freq * idf_of(term) for term, freq in doc_tf.items()}

            # 余弦相似度（仅迭代查询向量的 term，缺失项贡献为 0）
            dot = sum(qv * doc_vec.get(t, 0.0) for t, qv in query_vec.items())
            norm_d = math.sqrt(sum(v * v for v in doc_vec.values()))
            score = dot / (norm_q * norm_d) if norm_q > 0 and norm_d > 0 else 0.0

            if score > 0.05:
                results.append(RetrievalEntry(
                    project_id=row[0], chapter_number=row[1],
                    content=row[2], entry_type=row[3], score=score,
                ))

        results.sort(key=lambda x: x.score, reverse=True)
        return results[:top_k]

    def delete_project(self, project_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM retrieval_entries WHERE project_id = ?", (project_id,))
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass

    def _tokenize(self, text: str) -> list[str]:
        """简单中文分词：按字符 bigram + 标点分割的词。"""
        text = text.lower()
        # 提取中文字符序列的 bigram
        chinese = re.findall(r'[\u4e00-\u9fff]+', text)
        tokens: list[str] = []
        for seg in chinese:
            for i in range(len(seg) - 1):
                tokens.append(seg[i:i + 2])
            if len(seg) == 1:
                tokens.append(seg)
        # 提取英文单词
        english = re.findall(r'[a-z]+', text)
        tokens.extend(english)
        return tokens


class EmbeddingRetriever(BaseRetriever):
    """基于 sentence-transformers 的语义检索器。"""

    def __init__(self, db_path: Path, model_name: str = "paraphrase-multilingual-MiniLM-L12-v2") -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError("需要安装 sentence-transformers: pip install sentence-transformers")

        self.model = SentenceTransformer(model_name)
        self.db_path = db_path.parent / "ai_retrieval_vec.db"
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS retrieval_vectors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                chapter_number INTEGER NOT NULL,
                content TEXT NOT NULL,
                entry_type TEXT NOT NULL,
                embedding BLOB NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_vec_project ON retrieval_vectors(project_id)")
        self.conn.commit()

    def index_chapter(self, project_id: int, chapter_number: int, summary: str, key_events: list[str] | None = None) -> None:
        import numpy as np

        texts: list[tuple[str, str]] = []
        if summary and summary.strip():
            texts.append((summary, "summary"))
        if key_events:
            for event in key_events:
                if event and event.strip():
                    texts.append((event, "key_event"))

        contents = [t[0] for t in texts]
        embeddings = self.model.encode(contents, normalize_embeddings=True) if contents else []

        with self._lock:
            self.conn.execute(
                "DELETE FROM retrieval_vectors WHERE project_id = ? AND chapter_number = ?",
                (project_id, chapter_number),
            )
            if not texts:
                self.conn.commit()
                return

            for (content, entry_type), emb in zip(texts, embeddings):
                self.conn.execute(
                    "INSERT INTO retrieval_vectors (project_id, chapter_number, content, entry_type, embedding) VALUES (?, ?, ?, ?, ?)",
                    (project_id, chapter_number, content, entry_type, np.array(emb, dtype=np.float32).tobytes()),
                )
            self.conn.commit()

    def search(self, project_id: int, query: str, top_k: int = 5) -> list[RetrievalEntry]:
        import numpy as np

        query_emb = self.model.encode([query], normalize_embeddings=True)[0]

        with self._lock:
            rows = self.conn.execute(
                "SELECT project_id, chapter_number, content, entry_type, embedding FROM retrieval_vectors WHERE project_id = ?",
                (project_id,),
            ).fetchall()

        if not rows:
            return []

        results: list[RetrievalEntry] = []
        for row in rows:
            doc_emb = np.frombuffer(row[4], dtype=np.float32)
            score = float(np.dot(query_emb, doc_emb))
            if score > 0.3:
                results.append(RetrievalEntry(
                    project_id=row[0], chapter_number=row[1],
                    content=row[2], entry_type=row[3], score=score,
                ))

        results.sort(key=lambda x: x.score, reverse=True)
        return results[:top_k]

    def delete_project(self, project_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM retrieval_vectors WHERE project_id = ?", (project_id,))
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass


def create_retriever(db_path: Path, use_embeddings: bool = False, model_name: str | None = None) -> BaseRetriever:
    """创建检索器实例。

    Args:
        db_path: 数据库路径
        use_embeddings: 是否使用 embedding 模型（需要 sentence-transformers）
        model_name: embedding 模型名称

    Returns:
        TFIDFRetriever（默认）或 EmbeddingRetriever
    """
    if use_embeddings:
        try:
            return EmbeddingRetriever(db_path, model_name=model_name or "paraphrase-multilingual-MiniLM-L12-v2")
        except ImportError:
            pass  # fallback to TFIDF
    return TFIDFRetriever(db_path)
