from __future__ import annotations

import hashlib
import json
import uuid
from types import SimpleNamespace

from pixiv_novel_sync.ai.models import AIStreamChunk
from pixiv_novel_sync.ai.service import AIWritingService
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.work_analysis.configuration import snapshot_work_configuration
from pixiv_novel_sync.work_analysis.manifest import capture_manifest, preview_fingerprint
from pixiv_novel_sync.work_analysis.planning import plan_original_units


class SyntheticSecrets:
    def is_legacy_ciphertext(self, ciphertext):
        return False

    def decrypt(self, ciphertext):
        if ciphertext != "v2$synthetic-only":
            raise AssertionError("unexpected fixture ciphertext")
        return "synthetic-only-key"


class SyntheticWorkProvider:
    def __init__(self, db_factory):
        self.db_factory = db_factory
        self.sent_units = []
        self.bad_calls = set()
        self.before_response = None
        self.summary = "No supported findings in this synthetic test."

    def stream_generate(
        self, messages, model, temperature, top_p, max_tokens, *,
        request_guard=None, is_cancelled=None, on_usage=None, deadline=None,
    ):
        ordinal = request_guard()
        db = self.db_factory()
        try:
            row = db.conn.execute(
                "SELECT r.unit_id FROM work_analysis_requests r JOIN ai_jobs j ON j.job_id=r.child_job_id "
                "WHERE r.entry_kind='attachment' AND j.status='running' ORDER BY j.id DESC LIMIT 1",
            ).fetchone()
            if row is None:
                raise AssertionError("child must already have a durable parent attachment")
            unit_id = row["unit_id"]
        finally:
            db.close()
        self.sent_units.append(unit_id)
        if self.before_response is not None:
            self.before_response(unit_id)
        raw = json.dumps({
            "unit_id": unit_id, "summary": self.summary,
            "no_findings_reason": "The fixture intentionally makes no content claims.",
            "claims": [], "counterexamples": [], "uncertainties": ["Interpretation is not guaranteed."],
        })
        if len(self.sent_units) in self.bad_calls:
            raw = "{synthetic-invalid-json"
        yield AIStreamChunk(type="delta", text=raw)
        if on_usage is not None:
            on_usage(ordinal, {
                "input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
                "complete": True, "missing_reason": None,
            })
        yield AIStreamChunk(type="done", data={"finish_reason": "stop"})


def make_runtime_environment(
    tmp_path, monkeypatch, *, texts=None, context_window=100000, request_limit=64,
    token_limit=1_000_000,
):
    import requests

    def forbidden_network(*args, **kwargs):
        raise AssertionError("real transport is forbidden in default work-analysis tests")

    monkeypatch.setattr(requests.Session, "request", forbidden_network)
    path = tmp_path / "work-runtime.db"
    db = Database(path)
    db.init_schema()
    bodies = list(texts or ["synthetic opening and ending"])
    with db.transaction() as conn:
        conn.execute("INSERT INTO users(user_id,name,raw_json) VALUES(1,'Synthetic author','{}')")
        conn.execute("INSERT INTO series(series_id,title,user_id,total_novels) VALUES(10,'Synthetic series',1,?)", (len(bodies),))
        for index, body in enumerate(bodies, 1):
            conn.execute(
                "INSERT INTO novels(novel_id,user_id,series_id,title,visible,restrict_value,x_restrict,"
                "text_length,total_bookmarks,total_views,tags_json,raw_json,meta_hash,create_date) "
                "VALUES(?,1,10,?,1,'public',0,?,0,0,'[]','{}',?,?)",
                (index, f"Synthetic chapter {index}", len(body), "0" * 64, f"2026-01-{index:02d}T00:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO novel_texts(novel_id,text_raw,text_hash,has_content) VALUES(?,?,?,1)",
                (index, body, hashlib.sha256(body.encode("utf-8")).hexdigest()),
            )
    provider_id = db.create_ai_provider({
        "name": "Synthetic provider", "provider_type": "openai_compatible", "enabled": True,
        "base_url": "https://provider.invalid/v1", "api_key_encrypted": "v2$synthetic-only",
        "default_model": "synthetic-model", "context_window": context_window,
    })
    agent_id = db.create_ai_agent({
        "name": "Synthetic agent", "task_type": "general", "provider_id": provider_id,
        "model": "synthetic-model", "system_prompt": "Use only supplied synthetic evidence.",
        "context_window": context_window, "max_tokens": 128,
    })
    selection = {
        "source_kind": "series", "source_id": 10,
        "ordered_novel_ids": list(range(1, len(bodies) + 1)), "publication_state": "completed",
    }
    budget = {"request_limit": request_limit, "token_limit": token_limit, "active_seconds_limit": 600}
    confirmations = {
        "order_confirmed": True, "current_archive_only": True,
        "external_send_confirmed": True, "budget_confirmed": True,
    }
    with db.read_transaction():
        manifest, captured = capture_manifest(db.conn, selection)
        configuration, prompt, preflight = snapshot_work_configuration(db.conn, agent_id, 128)
    plan = plan_original_units(manifest, captured, preflight, agent_prompt=prompt, output_tokens=128)
    preview = preview_fingerprint(selection, manifest, plan, configuration, budget)
    run = db.create_work_analysis(
        selection, manifest, plan, configuration, budget, confirmations, uuid.uuid4().hex, preview,
    )
    db_factory = lambda: Database(path)
    service = AIWritingService(path, secret_manager=SyntheticSecrets())
    provider = SyntheticWorkProvider(db_factory)
    monkeypatch.setattr(service.model_router, "_get_provider", lambda config: provider)

    def close():
        service.close()
        db.close()

    return SimpleNamespace(
        db=db, db_factory=db_factory, service=service, provider=provider,
        agent_id=agent_id, provider_id=provider_id, run_id=run["run_id"],
        manifest=manifest, plan=plan, close=close,
    )
