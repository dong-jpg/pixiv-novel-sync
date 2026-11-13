"""Dashboard pilot routes; no provider calls or lifecycle background threads."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

from flask import Blueprint, jsonify, render_template, request, send_file

from .blobs import BlobStore
from .config import RemoteConfig
from .contracts import (COVER_LIMIT, DISK_LIMIT, MANIFEST_LIMIT, RETENTION_SECONDS, TEXT_LIMIT,
                        RemoteError, require_fields, require_int, require_key, require_uuid, request_digest)
from .control_store import ControlStore
from .epub_task import freeze_diagnostic, freeze_epub
from .http_utils import bounded_json, install_error_handlers
from .results import ResultService
from .store import RemoteStore
from .worker_api import create_worker_blueprint


def _novel_input(settings, novel_id):
    from ..storage_db import Database
    from ..storage_files import FileStorage

    if not settings.storage.db_path.is_file():
        raise RemoteError("library_unavailable", 503)
    db = Database(settings.storage.db_path)
    try:
        row = db.conn.execute("""SELECT n.novel_id,n.title,n.user_id,u.name AS author_name,
                n.cover_url,n.restrict_value,n.archive_dir,
                length(CAST(t.text_raw AS BLOB)) AS text_bytes,
                CASE WHEN length(CAST(t.text_raw AS BLOB))<=? THEN t.text_raw END AS text_raw
                FROM novels n LEFT JOIN users u USING(user_id) LEFT JOIN novel_texts t USING(novel_id)
                WHERE n.novel_id=?""", (TEXT_LIMIT, novel_id)).fetchone()
    finally:
        db.close()
    if row is None:
        raise RemoteError("novel_not_found", 404)
    novel = dict(row)
    if (novel["text_bytes"] or 0) > TEXT_LIMIT:
        raise RemoteError("text_too_large", 413)
    cover = None
    cover_path = FileStorage(settings).get_novel_cover_path(novel)
    if cover_path is not None and (cover_path.exists() or cover_path.is_symlink()):
        cover_path = Path(cover_path).absolute()
        root = settings.storage.private_dir if novel["restrict_value"] == "private" else settings.storage.public_dir
        if cover_path.is_symlink() or cover_path.resolve() != cover_path or not cover_path.is_relative_to(root.resolve()):
            raise RemoteError("unsafe_cover_path", 403)
        with cover_path.open("rb") as source:
            cover = source.read(COVER_LIMIT + 1)
        if len(cover) > COVER_LIMIT:
            raise RemoteError("cover_too_large", 413)
    return novel, cover


def register_remote_routes(app, settings_callback):
    cfg = RemoteConfig.from_env(os.environ, "web")
    bp = Blueprint("remote_dashboard", __name__)
    install_error_handlers(bp)

    @bp.get("/dashboard/remote")
    def remote_page():
        return render_template("dashboard_remote.html", remote_enabled=cfg.enabled, active_page="remote")

    if not cfg.enabled:
        app.register_blueprint(bp)
        return
    if (cfg.state_dir / "control.db").resolve() == settings_callback().storage.db_path.resolve():
        raise RemoteError("state_overlaps_library", 400)
    store = RemoteStore(cfg.state_dir, cfg.machine_id)
    store.migrate()
    blobs = BlobStore(cfg.state_dir / "blobs")
    clock = lambda: app.config.get("REMOTE_CLOCK", time.time)()
    results = ResultService(store, blobs, clock)
    control = ControlStore(store)
    app.extensions["remote_execution"] = {"config": cfg, "cfg": cfg, "store": store, "blobs": blobs,
                                         "results": results, "control": control, "clock": clock}

    def response(payload, status=200):
        return jsonify(dict(payload, server_now=clock())), status

    def public_run(run):
        payload = {key: run.get(key) for key in ("run_id", "kind", "state", "created_at", "not_before",
                    "deadline", "finished_at", "error_code", "attempt_count")}
        payload["attempt_count"] = payload["attempt_count"] or 0
        expired = run["finished_at"] is not None and clock() >= run["finished_at"] + RETENTION_SECONDS
        with store.read() as conn:
            artifact = conn.execute("""SELECT b.state FROM artifacts b JOIN attempts a USING(attempt_id)
                WHERE a.run_id=? AND a.state='succeeded'""", (run["run_id"],)).fetchone()
        payload["result_expired"] = run["state"] == "succeeded" and (expired or bool(artifact and artifact[0] == "expired"))
        payload["can_download"] = run["state"] == "succeeded" and not payload["result_expired"] and bool(artifact and artifact[0] == "published")
        return payload

    def machine_view():
        with store.read() as conn:
            row = dict(conn.execute("SELECT * FROM machine_control WHERE id=1").fetchone())
            active = conn.execute("SELECT count(*) FROM runs WHERE state IN ('queued','active') AND deadline>?", (clock(),)).fetchone()[0]
            used = conn.execute("SELECT coalesce(sum(size_bytes),0) FROM blob_objects").fetchone()[0]
            reserved = conn.execute("SELECT coalesce(sum(size_bytes),0) FROM blob_reservations WHERE expires_at>?", (clock(),)).fetchone()[0]
        payload = {key: row[key] for key in ("mode", "provider_state", "last_provider_at", "last_error",
                    "cooldown_until", "hold_until", "gate_ready_until", "idle_since")}
        payload.update(active_runs=active, stored_bytes=used, reserved_bytes=reserved, capacity_bytes=DISK_LIMIT,
                       owned=row["owned_run_started_at"] is not None, action=None)
        if row["action_json"]:
            try:
                action = json.loads(row["action_json"])
                payload["action"] = {key: action[key] for key in ("action", "phase")}
            except (ValueError, KeyError, TypeError):
                payload["last_error"] = "invalid_action_state"
        return payload

    @bp.post("/api/dashboard/remote/runs")
    def submit():
        body = require_fields(bounded_json(), {"kind"}, {"novel_id"})
        key = require_key(request.headers.get("Idempotency-Key"))
        request_sha = request_digest(body["kind"], body.get("novel_id"))
        if body["kind"] == "diagnostic.v1" and "novel_id" in body:
            raise RemoteError("invalid_fields", 400)
        run = store.find_submission(key, request_sha)
        if run is not None:
            return response({"run_id": run["run_id"], "state": run["state"]}, 202)
        now = clock()
        if body["kind"] == "epub.single.v1":
            novel, cover = _novel_input(settings_callback(), body["novel_id"])
            reserved_size = (novel["text_bytes"] or 0) + len(cover or b"") + MANIFEST_LIMIT
        else:
            novel, cover, reserved_size = None, None, MANIFEST_LIMIT
        reservation = store.reserve_blob("input", key, reserved_size, now)
        try:
            input_sha = freeze_epub(novel, cover, blobs) if novel is not None else freeze_diagnostic(blobs)
            with blobs.open_blob(input_sha) as source:
                encoded = source.read(MANIFEST_LIMIT + 1)
            manifest = json.loads(encoded)
            parts = {"manifest": (input_sha, len(encoded))}
            if novel is not None:
                parts["text"] = (manifest["text_sha256"], manifest["text_size"])
                if manifest["cover_sha256"] is not None:
                    parts["cover"] = (manifest["cover_sha256"], manifest["cover_size"])
            store.register_input(input_sha, parts, reservation, clock())
            run = store.enqueue(key, request_sha, body["kind"], input_sha, clock())
            return response({"run_id": run["run_id"], "state": run["state"]}, 202)
        finally:
            store.release_reservation(reservation, clock())

    @bp.get("/api/dashboard/remote/runs")
    def runs():
        return response({"runs": [public_run(run) for run in store.list_runs()]})

    @bp.get("/api/dashboard/remote/runs/<run_id>")
    def run_detail(run_id):
        return response(public_run(store.get_run(require_uuid(run_id))))

    @bp.post("/api/dashboard/remote/runs/<run_id>/cancel")
    def cancel(run_id):
        if request.content_length:
            require_fields(bounded_json(), set())
        store.cancel(require_uuid(run_id), clock())
        return response(public_run(store.get_run(run_id)))

    @bp.get("/api/dashboard/remote/runs/<run_id>/download")
    def download(run_id):
        run = store.get_run(require_uuid(run_id))
        view = public_run(run)
        if view["result_expired"]:
            raise RemoteError("result_expired", 410)
        if not view["can_download"]:
            raise RemoteError("result_not_ready", 409)
        with store.read() as conn:
            artifact = conn.execute("""SELECT b.* FROM artifacts b JOIN attempts a USING(attempt_id)
                WHERE a.run_id=? AND a.state='succeeded' AND b.state='published'""", (run_id,)).fetchone()
        source = blobs.open_blob(artifact["blob_ref"])
        epub = run["kind"] == "epub.single.v1"
        result = send_file(source, mimetype="application/epub+zip" if epub else "application/json",
                           as_attachment=True, download_name=f"remote-{run_id}.{'epub' if epub else 'json'}", conditional=False)
        result.content_length = artifact["size_bytes"]
        result.call_on_close(source.close)
        return result

    @bp.get("/api/dashboard/remote/machine")
    def machine():
        return response(machine_view())

    @bp.post("/api/dashboard/remote/machine/hold")
    def hold():
        body = require_fields(bounded_json(), {"seconds"})
        control.set_hold(require_int(body["seconds"], 0, 14400), clock())
        return response(machine_view())

    app.register_blueprint(bp)
    app.register_blueprint(create_worker_blueprint(store, blobs, results, cfg, clock))
