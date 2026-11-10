from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from types import SimpleNamespace

from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.models import NovelRecord, NovelTextRecord, UserRecord
from pixiv_novel_sync.storage_db import Database


TEXT = "BEGIN fixture \U0001f600 unique evidence. PRIVATE_BODY_END"
PROMPT = "SAVED_AGENT_PROMPT_ONLY_IN_MEMORY"
CIPHER = "v2$fixture-ciphertext-never-decrypted"
BASE = "/api/dashboard/work-analyses"


class RecordingCoordinator:
    def __init__(self, db_factory):
        self.db_factory = db_factory
        self.wakes = []
        self.controls = []

    def wake(self, reason):
        self.wakes.append(reason)

    def request_control(self, run_id, action, expected_version):
        self.controls.append((run_id, action, expected_version))
        db = self.db_factory()
        try:
            if action == "resume":
                return db.resume_work_analysis(run_id, expected_version)
            return db.request_work_analysis_stop(run_id, action, expected_version)
        finally:
            db.close()


def seed_database(path: Path):
    db = Database(path)
    db.init_schema()
    db.upsert_user(UserRecord(1, "Fixture author", None, "{}"))
    db.upsert_novel(NovelRecord(
        novel_id=101, user_id=1, series_id=None, title="Fixture book",
        caption=None, visible=True, restrict="public", x_restrict=0,
        text_length=len(TEXT), total_bookmarks=0, total_views=0,
        cover_url=None, tags_json="[]", create_date="2026-01-01T00:00:00Z",
        raw_json="{}", meta_hash="fixture-metadata",
    ))
    db.upsert_novel_text(NovelTextRecord(
        101, TEXT, None, hashlib.sha256(TEXT.encode("utf-8")).hexdigest(),
    ))
    provider_id = db.create_ai_provider({
        "name": "Fixture provider", "provider_type": "openai_compatible",
        "base_url": "https://private-provider.fixture.invalid/v1",
        "api_key_encrypted": CIPHER, "default_model": "org/model ",
        "context_window": 1000000, "enabled": True,
    })
    agent_id = db.create_ai_agent({
        "name": "Fixture Agent", "task_type": "general",
        "binding_type": "fixed", "provider_id": provider_id,
        "model": "org/model ", "system_prompt": PROMPT,
        "temperature": 0, "top_p": 0, "max_tokens": 1000,
        "context_window": 1000000, "enabled": True,
    })
    return db, agent_id, provider_id


def request_payload(agent_id):
    return {
        "selection": {"source_kind": "novel", "source_id": 101,
                      "publication_state": "unknown"},
        "agent_id": agent_id, "output_tokens": 1000,
        "budget": {"request_limit": 16, "token_limit": 500000,
                   "active_seconds_limit": 600},
        "confirmations": {"order_confirmed": False,
                          "current_archive_only": False,
                          "external_send_confirmed": False,
                          "budget_confirmed": False},
    }


def confirmed_payload(service, payload, key="1" * 32):
    result = deepcopy(payload)
    result["preview_fingerprint"] = service.preview(payload)["preview_fingerprint"]
    result["confirmations"] = {name: True for name in payload["confirmations"]}
    result["idempotency_key"] = key
    return result


def facade_fixture(path):
    from pixiv_novel_sync.work_analysis.service import WorkAnalysisService

    db, agent_id, provider_id = seed_database(path)
    ai = AIWritingService(path)
    factory = lambda: Database(path)
    coordinator = RecordingCoordinator(factory)
    service = WorkAnalysisService(factory, lambda: ai, coordinator)
    return SimpleNamespace(db=db, ai=ai, service=service, coordinator=coordinator,
                           agent_id=agent_id, provider_id=provider_id,
                           payload=request_payload(agent_id), path=path)
