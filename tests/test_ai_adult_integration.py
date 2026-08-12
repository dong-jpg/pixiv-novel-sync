from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ai_adult_testkit import CHARACTER_A_ID, seed_adult_project, valid_adult_payload
from pixiv_novel_sync.ai.model_router import (
    CandidateSnapshot,
    ModelCandidate,
    PromptBudget,
    RouteResult,
)
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.webapp import create_app


def _snapshot(seed: str, *, capabilities: tuple[str, ...] = ()) -> CandidateSnapshot:
    candidate = ModelCandidate(
        provider_id=1,
        provider_name=f"fake-{seed}",
        model_key=f"fake-model-{seed}",
        provider_model_id=None,
        pool_id=None,
        pool_name=None,
        pool_version=None,
        pool_position=None,
        provider_config_hash=seed * 64,
        capabilities=capabilities,
        context_window=32_000,
    )
    payload = {
        "agent_config_hash": seed * 64,
        "binding_version": 1,
        "candidates": [asdict(candidate)],
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return CandidateSnapshot(
        candidates=(candidate,),
        snapshot_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        agent_config_hash=seed * 64,
        binding_version=1,
    )


class IntegrationRouter:
    def __init__(self) -> None:
        self.snapshots = {
            "main": _snapshot("a"),
            "safety": _snapshot("b", capabilities=("json",)),
            "fact_guard": _snapshot("c", capabilities=("json",)),
        }
        self.stages: list[str] = []
        self.outputs: list[str] = []
        self.requests: list[Any] = []

    def close(self) -> None:
        pass

    def resolve_candidates(
        self,
        agent: Any,
        stage: str = "main",
        snapshot: CandidateSnapshot | None = None,
    ) -> CandidateSnapshot:
        task_type = str(getattr(agent, "task_type", ""))
        key = (
            "safety"
            if task_type == "adult_safety_review"
            else "fact_guard"
            if task_type == "adult_fact_guard"
            else "main"
        )
        return snapshot or self.snapshots[key]

    def build_prompt_budget(
        self,
        _agent: Any,
        _snapshot: CandidateSnapshot,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> PromptBudget:
        assert messages
        return PromptBudget(
            effective_context_window=32_000,
            input_budget=24_000,
            output_reserve=max_tokens,
            message_overhead=4 * len(messages) + 2,
            safety_margin=256,
            estimator="utf8_bytes",
        )

    def execute(self, request: Any) -> RouteResult:
        self.stages.append(request.stage)
        self.requests.append(request)
        if request.stage == "main":
            boundary_match = re.search(
                r"ADULT_BOUNDARY_[0-9a-f]{32}",
                request.messages[0]["content"],
            )
            assert boundary_match is not None
            boundary = boundary_match.group(0)
            target_lines = request.messages[3]["content"].splitlines()
            masked_target = "\n".join(target_lines[1:-1])
            candidate = masked_target.replace("手", "腕", 1)
            output = (
                f"{boundary}_CANDIDATE_BEGIN\n"
                f"{candidate}\n"
                f"{boundary}_CANDIDATE_END"
            )
        else:
            output = '{"safe":true,"issues":[]}'
        request.on_delta(output)
        self.outputs.append(output)
        return RouteResult(
            job_id=request.job_id,
            output_text=output,
            candidate_snapshot_hash=request.candidate_snapshot.snapshot_hash,
            attempts=(),
            finish_state="succeeded",
        )


def _parse_sse(response: Any) -> dict[str, list[dict[str, Any]]]:
    events: dict[str, list[dict[str, Any]]] = {}
    body = (
        response.decode("utf-8")
        if isinstance(response, (bytes, bytearray))
        else response.get_data(as_text=True)
    )
    for block in body.split("\n\n"):
        event_match = re.search(r"^event:\s*(\S+)$", block, re.MULTILINE)
        data_match = re.search(r"^data:\s*(.+)$", block, re.MULTILINE)
        if event_match and data_match:
            events.setdefault(event_match.group(1), []).append(
                json.loads(data_match.group(1))
            )
    return events


def test_adult_polish_end_to_end_changes_only_target_and_records_snapshots(
    tmp_path: Path,
    monkeypatch: Any,
):
    db_path = tmp_path / "adult-integration.db"
    monkeypatch.setenv("PIXIV_DB_PATH", str(db_path))
    monkeypatch.setenv("PIXIV_PUBLIC_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("PIXIV_PRIVATE_DIR", str(tmp_path / "private"))
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DASHBOARD_TOKEN=dashboard-secret\n"
        "PIXIV_FLASK_SECRET=flask-secret\n"
        "PIXIV_REFRESH_TOKEN=refresh-token\n",
        encoding="utf-8",
    )

    database = Database(db_path)
    database.init_schema()
    seed_adult_project(database)
    database.conn.execute(
        """
        UPDATE ai_adult_review_bindings
        SET binding_type = 'fixed', provider_id = 1, model = 'adult-model',
            model_pool_id = NULL, enabled = 1
        """
    )
    database.conn.commit()
    before = database.get_ai_chapter(9)["content"]
    database.close()

    app = create_app(env_path=str(env_path), start_scheduler=False)
    fake_router = IntegrationRouter()
    service_proxy = app.extensions["pixiv_novel_sync.ai_service"]
    service = service_proxy._current()
    service.model_router.close()
    service.model_router = fake_router

    client = app.test_client()
    login = client.post("/api/auth/login", data={"token": "dashboard-secret"})
    assert login.status_code == 302
    csrf = client.get("/api/csrf-token").get_json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}

    scope_response = client.post(
        "/api/dashboard/ai/polish/adult/scope",
        json={"agent_id": 7},
        headers=headers,
    )
    assert scope_response.status_code == 200
    scope_data = scope_response.get_json()["data"]
    payload = valid_adult_payload(
        provider_scope_hash=scope_data["provider_scope_hash"],
        participant_character_ids=[CHARACTER_A_ID],
    )

    stream = client.post(
        "/api/dashboard/ai/polish/adult/stream",
        json=payload,
        headers=headers,
        buffered=True,
    )
    assert stream.status_code == 200
    events = _parse_sse(stream)
    assert "candidate" in events, {
        "events": events,
        "outputs": fake_router.outputs,
        "messages": [item["content"] for item in fake_router.requests[0].messages],
    }
    metadata = events["metadata"][0]
    validation = events["validation"][0]
    job_id = metadata["job_id"]
    candidate = events["candidate"][0]["candidate"]
    assert validation["warning_ack_hash"] == ""

    apply = client.post(
        f"/api/dashboard/ai/polish/adult/{job_id}/apply",
        json={"warning_ack_hash": validation["warning_ack_hash"]},
        headers={
            **headers,
            "X-Adult-Access-Token": metadata["access_token"],
        },
    )
    assert apply.status_code == 200

    database = Database(db_path)
    try:
        after = database.get_ai_chapter(9)["content"]
        application = database.conn.execute(
            "SELECT snapshots_json FROM ai_polish_applications WHERE source_job_id = ?",
            (job_id,),
        ).fetchone()
    finally:
        database.close()

    start, end = payload["target_start"], payload["target_end"]
    assert after[:start] == before[:start]
    assert after[end:] == before[end:]
    assert after[start:end] == candidate
    assert after != before
    assert application is not None
    snapshots = json.loads(application["snapshots_json"])
    assert snapshots["main_route"]
    assert snapshots["safety_route"]
    assert snapshots["fact_guard_route"]
    assert fake_router.stages == ["main", "validation", "validation"]


def test_adult_polish_disconnect_replay_restores_validation_before_apply(
    tmp_path: Path,
    monkeypatch: Any,
):
    db_path = tmp_path / "adult-replay-integration.db"
    monkeypatch.setenv("PIXIV_DB_PATH", str(db_path))
    monkeypatch.setenv("PIXIV_PUBLIC_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("PIXIV_PRIVATE_DIR", str(tmp_path / "private"))
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DASHBOARD_TOKEN=dashboard-secret\n"
        "PIXIV_FLASK_SECRET=flask-secret\n"
        "PIXIV_REFRESH_TOKEN=refresh-token\n",
        encoding="utf-8",
    )

    database = Database(db_path)
    database.init_schema()
    seed_adult_project(database)
    database.conn.execute(
        """
        UPDATE ai_adult_review_bindings
        SET binding_type = 'fixed', provider_id = 1, model = 'adult-model',
            model_pool_id = NULL, enabled = 1
        """
    )
    database.conn.commit()
    database.close()

    app = create_app(env_path=str(env_path), start_scheduler=False)
    fake_router = IntegrationRouter()
    service_proxy = app.extensions["pixiv_novel_sync.ai_service"]
    service = service_proxy._current()
    service.model_router.close()
    service.model_router = fake_router

    client = app.test_client()
    assert client.post("/api/auth/login", data={"token": "dashboard-secret"}).status_code == 302
    csrf = client.get("/api/csrf-token").get_json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}
    scope_response = client.post(
        "/api/dashboard/ai/polish/adult/scope",
        json={"agent_id": 7},
        headers=headers,
    )
    assert scope_response.status_code == 200
    scope_data = scope_response.get_json()["data"]
    payload = valid_adult_payload(
        provider_scope_hash=scope_data["provider_scope_hash"],
        participant_character_ids=[CHARACTER_A_ID],
    )

    stream = client.post(
        "/api/dashboard/ai/polish/adult/stream",
        json=payload,
        headers=headers,
        buffered=False,
    )
    received = b""
    while b"event: candidate" not in received:
        received += next(stream.response)
    stream.close()
    initial_events = _parse_sse(received)
    metadata = initial_events["metadata"][0]
    job_id = metadata["job_id"]

    replay = client.get(
        f"/api/dashboard/ai/polish/adult/{job_id}/events",
        headers={"X-Adult-Access-Token": metadata["access_token"]},
        buffered=True,
    )
    assert replay.status_code == 200
    replay_events = _parse_sse(replay)
    assert "validation" in replay_events
    assert "candidate" in replay_events
    assert "done" in replay_events
    validation = replay_events["validation"][0]
    assert validation["warning_ack_hash"] == ""

    apply = client.post(
        f"/api/dashboard/ai/polish/adult/{job_id}/apply",
        json={"warning_ack_hash": validation["warning_ack_hash"]},
        headers={
            **headers,
            "X-Adult-Access-Token": metadata["access_token"],
        },
    )
    assert apply.status_code == 200, apply.get_data(as_text=True)
