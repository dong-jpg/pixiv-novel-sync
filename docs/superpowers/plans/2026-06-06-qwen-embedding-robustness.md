# Qwen Embedding Robustness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Qwen/OpenAI-compatible embedding retrieval resilient, cheaper, and documented.

**Architecture:** Keep the existing `APIEmbeddingRetriever` interface, add hash-based reuse, BLOB float32 storage, and graceful TF-IDF fallback from service creation. Do not introduce a vector database yet.

**Tech Stack:** Python 3.11, pytest, SQLite, requests, struct/array for float32 BLOB encoding.

---

## Files

- Modify: `src/pixiv_novel_sync/ai/retrieval.py` — API embedding storage, reuse, fallback helpers.
- Modify: `src/pixiv_novel_sync/ai/service.py` — fallback to TF-IDF when API retriever cannot initialize.
- Modify: `tests/test_ai_retrieval.py` — TDD coverage for BLOB storage, hash reuse, fallback behavior.
- Modify: `docs/QWEN_EMBEDDING_INTEGRATION.md` — document privacy, fallback, rebuild, env vars.

## Task 1: Store API vectors as BLOB and decode safely

- [ ] Write failing test in `tests/test_ai_retrieval.py` that indexes one chapter and asserts `embedding_blob` exists and `embedding_json` is absent/unused.
- [ ] Run: `pytest tests/test_ai_retrieval.py::test_api_embedding_retriever_stores_vectors_as_blob -v`; expect failure because schema lacks `embedding_blob`.
- [ ] Add float32 encode/decode helpers in `retrieval.py` and migrate table schema to include `embedding_blob BLOB`.
- [ ] Update search to decode blob first, preserving JSON fallback for existing rows.
- [ ] Run the test and existing retrieval tests; expect pass.

## Task 2: Skip duplicate embedding calls using content hash

- [ ] Write failing test that indexes the same chapter content twice and asserts the fake API was called once for indexing.
- [ ] Run the single test; expect failure because current code deletes and re-embeds every time.
- [ ] In `APIEmbeddingRetriever.index_chapter`, compare existing content hashes for the chapter/model and return early when unchanged.
- [ ] Run retrieval tests; expect pass.

## Task 3: Fallback to TF-IDF when API embedding is unavailable

- [ ] Write failing test that monkeypatches API retriever/client initialization to raise and asserts `create_retriever(...api...)` returns `TFIDFRetriever` when fallback is allowed.
- [ ] Run the single test; expect failure because exception propagates.
- [ ] Add fallback behavior in `create_retriever`; keep invalid API response errors during indexing/search visible once retriever was created.
- [ ] Run focused tests; expect pass.

## Task 4: Documentation update

- [ ] Update `docs/QWEN_EMBEDDING_INTEGRATION.md` with recommended env vars, privacy warning, fallback behavior, rebuild guidance, and suggested rollout order.
- [ ] Run focused tests again.
