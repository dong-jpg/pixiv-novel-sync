from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ...storage_db import Database
from ..chunking import estimate_token_count, get_tail_context, split_text_by_chars
from ..crypto import AISecretManager
from ..detection import detect_ai_tells
from ..models import AIAgentConfig, AIProviderConfig, AIStreamChunk
from ..prompts import (
    DEFAULT_WIZARD_PROMPT,
    build_audit_messages,
    build_chapter_summary_messages,
    build_chat_messages,
    build_continue_messages,
    build_foreshadow_resolve_messages,
    build_longform_detail_messages,
    build_longform_plan_messages,
    build_novel_distill_messages,
    build_plan_messages,
    build_polish_messages,
    build_rewrite_messages,
    build_style_distill_messages,
    build_summarize_messages,
    safe_prompt_preview,
)
from ..providers import AIProvider, create_provider
from ..retrieval import BaseRetriever, create_retriever


class AIServiceError(RuntimeError):
    pass


class AIServiceCore:
    # Track which DB paths have had their schema initialized. A single class-wide
    # bool would skip init_schema() for a second service pointing at a different
    # path (tests, multiple DBs in one process), causing "no such table".
    _initialized_paths: set[str] = set()

    def __init__(self, db_path: Path, secret_manager: AISecretManager | None = None) -> None:
        self.db_path = db_path
        self.secret_manager = secret_manager or AISecretManager()
        self._retriever: BaseRetriever | None = None
        self._retriever_config_key: tuple[str | None, str | None, str, int] | None = None
        self._retriever_lock = threading.Lock()  # 7.7: 保护retriever缓存
        self._provider_cache: dict[tuple[Any, ...], AIProvider] = {}
        self._provider_cache_by_id: dict[int, tuple[Any, ...]] = {}
        self._provider_lock = threading.Lock()

    def _get_retriever(self) -> BaseRetriever:
        # 7.7: 加锁保护缓存逻辑,避免多线程竞态
        with self._retriever_lock:
            embedding_base_url = (
                os.getenv("PIXIV_NOVEL_SYNC_EMBEDDING_BASE_URL")
                or os.getenv("QWEN_EMBEDDING_BASE_URL")
            )
            embedding_api_key = (
                os.getenv("PIXIV_NOVEL_SYNC_EMBEDDING_API_KEY")
                or os.getenv("QWEN_EMBEDDING_API_KEY")
            )
            embedding_model = (
                os.getenv("PIXIV_NOVEL_SYNC_EMBEDDING_MODEL")
                or os.getenv("QWEN_EMBEDDING_MODEL")
                or "Qwen3-Embedding-8B"
            )
            timeout_raw = os.getenv("PIXIV_NOVEL_SYNC_EMBEDDING_TIMEOUT", "60")
            try:
                embedding_timeout = max(int(timeout_raw), 1)
            except ValueError:
                embedding_timeout = 60
            config_key = (embedding_base_url, embedding_api_key, embedding_model, embedding_timeout)
            if self._retriever is None or self._retriever_config_key != config_key:
                if self._retriever is not None and hasattr(self._retriever, "close"):
                    self._retriever.close()  # type: ignore[attr-defined]
                self._retriever = create_retriever(
                    self.db_path,
                    model_name=embedding_model,
                    api_base_url=embedding_base_url,
                    api_key=embedding_api_key,
                    api_timeout=embedding_timeout,
                )
                self._retriever_config_key = config_key
            return self._retriever

    def _db(self) -> Database:
        db = Database(self.db_path)
        key = str(self.db_path)
        if key not in AIServiceCore._initialized_paths:
            db.init_schema()
            AIServiceCore._initialized_paths.add(key)
        return db

    def _provider_cache_key(self, config: AIProviderConfig) -> tuple[Any, ...]:
        return (
            config.id,
            config.provider_type,
            config.base_url,
            config.api_key,
            config.timeout_seconds,
            config.max_retries,
            config.proxy,
            config.stream_enabled,
        )

    def _get_provider(self, config: AIProviderConfig) -> AIProvider:
        key = self._provider_cache_key(config)
        with self._provider_lock:
            cached = self._provider_cache.get(key)
            if cached is not None:
                return cached
            if config.id is not None:
                old_key = self._provider_cache_by_id.get(config.id)
                if old_key is not None and old_key != key:
                    old_provider = self._provider_cache.pop(old_key, None)
                    if old_provider is not None:
                        old_provider.close()
            from .. import service as service_facade

            provider = service_facade.create_provider(config)
            self._provider_cache[key] = provider
            if config.id is not None:
                self._provider_cache_by_id[config.id] = key
            return provider

    def _invalidate_provider(self, provider_id: int) -> None:
        with self._provider_lock:
            key = self._provider_cache_by_id.pop(provider_id, None)
            if key is None:
                return
            provider = self._provider_cache.pop(key, None)
            if provider is not None:
                provider.close()

    def close(self) -> None:
        with self._provider_lock:
            providers = list(self._provider_cache.values())
            self._provider_cache.clear()
            self._provider_cache_by_id.clear()
        for provider in providers:
            provider.close()
        with self._retriever_lock:
            retriever = self._retriever
            self._retriever = None
            self._retriever_config_key = None
        if retriever is not None and hasattr(retriever, "close"):
            retriever.close()  # type: ignore[attr-defined]
