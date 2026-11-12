"""Offline screenshot QA for the remote pilot, using the real Flask app.

Run from the checkout with an already-downloaded Vue 3.4.38 global production
runtime: python docs/superpowers/execution/verify_remote_ui.py --vue-runtime PATH
The runtime is checked against base.html's pinned SHA-384. The browser's requests
are fulfilled by Flask's test client; no server listens and no cloud is contacted.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vue-runtime", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    vue = args.vue_runtime.read_bytes()
    if base64.b64encode(hashlib.sha384(vue).digest()).decode() != "Z9a2xA0EIaURxthVmeQmPrgSC05eG8Lch8X4Y2+lXpTg9wcxPj2m3QNurDl5hLiS":
        raise RuntimeError("Vue runtime integrity mismatch")
    checkout = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(checkout / "src"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    # Retain only OS/runtime locating variables before importing application code.
    keep = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "HOME",
        "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PLAYWRIGHT_BROWSERS_PATH",
    }}
    os.environ.clear()
    os.environ.update(keep)

    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def guarded_connect(self, address):
        # Windows asyncio implements its internal wakeup pipe with socketpair.
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(self, address)
        raise AssertionError("UI fixture must not open external network sockets")

    def guarded_connect_ex(self, address):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect_ex(self, address)
        raise AssertionError("UI fixture must not open external network sockets")

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    from playwright.sync_api import sync_playwright
    from pixiv_novel_sync.remote.contracts import digest
    from pixiv_novel_sync.storage_db import Database
    from pixiv_novel_sync.webapp import create_app

    origin = "http://127.0.0.1:5018"
    pages = []
    errors = []
    blocked = []
    now = time.time()
    applications = []
    with tempfile.TemporaryDirectory(prefix="pixiv-remote-ui-") as temporary:
        root = Path(temporary)
        os.environ.update({
            "DASHBOARD_TOKEN": "ui-fixture-password", "PIXIV_FLASK_SECRET": "ui-fixture-secret",
            "PIXIV_REFRESH_TOKEN": "test-only-refresh-token", "PIXIV_DB_PATH": str(root / "library.db"),
            "PIXIV_PUBLIC_DIR": str(root / "public"), "PIXIV_PRIVATE_DIR": str(root / "private"),
            "PIXIV_REMOTE_ENABLED": "1", "PIXIV_REMOTE_STATE_DIR": str(root / "remote"),
            "PIXIV_REMOTE_MACHINE_ID": "m_fixture",
            "PIXIV_REMOTE_WORKER_KEY_SHA256": digest(b"B" * 32),
        })
        env_path = root / "empty.env"
        env_path.write_text("", encoding="utf-8")
        db = Database(root / "library.db")
        db.init_schema()
        db.conn.execute("INSERT INTO users(user_id,name,raw_json) VALUES(1,'合成作者','{}')")
        db.conn.execute("""INSERT INTO novels(novel_id,user_id,title,visible,restrict_value,x_restrict,text_length,
            total_bookmarks,total_views,tags_json,raw_json,meta_hash,status)
            VALUES(42,1,'合成书目：远程执行界面验收',1,'public',0,100,0,0,'[]','{}','fixture','normal')""")
        db.conn.execute("INSERT INTO novel_texts(novel_id,text_raw,has_content,text_hash) VALUES(42,?,1,'fixture')",
                        ("这是合成正文。[newpage]第二页。",))
        db.conn.commit()
        db.close()

        def new_app():
            app = create_app(config_path=str(root / "missing.yaml"), env_path=str(env_path), start_scheduler=False)
            app.config.update(TESTING=True, RAW_HTTP=True, REMOTE_CLOCK=lambda: now)
            applications.append(app)
            client = app.test_client()
            response = client.post("/api/auth/login", data={"token": "ui-fixture-password"}, base_url=origin)
            assert response.status_code == 302
            token = client.get("/api/csrf-token", base_url=origin).get_json()["csrf_token"]
            return app, client, token

        app, client, csrf = new_app()
        remote = app.extensions["remote_execution"]
        store, blobs, results = remote["store"], remote["blobs"], remote["results"]
        run_ids = []
        for index, kind in enumerate(("diagnostic.v1", "epub.single.v1", "epub.single.v1")):
            body = {"kind": kind}
            if kind == "epub.single.v1":
                body["novel_id"] = 42
            response = client.post("/api/dashboard/remote/runs", json=body, base_url=origin,
                                   headers={"X-CSRF-Token": csrf, "Idempotency-Key": f"ui-fixture-{index}"})
            assert response.status_code == 202
            run_ids.append(response.get_json()["run_id"])
        # Complete a real diagnostic publication, then fail a real EPUB attempt.
        session = store.enroll(str(uuid.uuid4()), digest(b"ui-fixture-session"), 1, "epub-v1", now)
        with store.transaction() as conn:
            conn.execute("""UPDATE machine_control SET mode='ready',runtime_ready=1,provider_state='running',
                gate_ready_until=?,last_provider_at=?,runtime_budget_until=-1""", (now + 45, now))
            for index, run_id in enumerate(run_ids):
                conn.execute("UPDATE runs SET created_at=? WHERE run_id=?", (now - 180 + index * 30, run_id))
        grant = store.claim(session["session_id"], "ui-diagnostic-claim", now)
        assert grant.kind == "diagnostic.v1"
        with blobs.open_blob(grant.input_sha256) as source:
            output = source.read()
        results.stage(grant, io.BytesIO(output), len(output), digest(output), now)
        results.publish(grant, digest(output), len(output), now)
        grant = store.claim(session["session_id"], "ui-epub-claim", now)
        store.fail(grant, "render_failed", now)
        with store.transaction() as conn:
            conn.execute("""UPDATE machine_control SET mode='cooldown',runtime_ready=0,provider_state='suspended',
                gate_ready_until=NULL,last_provider_at=?,cooldown_until=?,hold_until=NULL""", (now - 30, now + 900))
            conn.execute("UPDATE runs SET error_code=? WHERE run_id=?", ("fixture_" + "long_status_" * 5, run_ids[2]))

        active_client = client
        try:
            with sync_playwright() as browser_api:
                browser = browser_api.chromium.launch(headless=True)
                context = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai", viewport={"width": 1280, "height": 900})

                def dispatch(route):
                    request = route.request
                    parsed = urlsplit(request.url)
                    if request.url == "https://unpkg.com/vue@3.4.38/dist/vue.global.prod.js":
                        route.fulfill(status=200, body=vue, content_type="application/javascript",
                                      headers={"Access-Control-Allow-Origin": "*"})
                        return
                    if f"{parsed.scheme}://{parsed.netloc}" != origin:
                        blocked.append(request.url)
                        route.abort()
                        return
                    path = parsed.path + ("?" + parsed.query if parsed.query else "")
                    headers = {key: value for key, value in request.headers.items() if key.lower() not in {"host", "cookie", "content-length"}}
                    response = active_client.open(path, method=request.method, base_url=origin,
                                                  headers=headers, data=request.post_data_buffer)
                    response_headers = {key: value for key, value in response.headers.items() if key.lower() not in {"content-length", "transfer-encoding"}}
                    route.fulfill(status=response.status_code, headers=response_headers, body=response.get_data())

                context.route("**/*", dispatch)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))

                def capture(width, disabled=False):
                    page.set_viewport_size({"width": width, "height": 900 if width == 1280 else 844})
                    page.goto(origin + "/dashboard/remote", wait_until="networkidle")
                    page.wait_for_selector("#app:not([v-cloak])")
                    if not disabled:
                        page.get_by_role("heading", name="任务记录", exact=True).wait_for()
                        page.wait_for_selector(".remote-run")
                    metrics = page.evaluate("""() => ({
                        viewport: innerWidth, document: document.documentElement.scrollWidth,
                        controls: [...document.querySelectorAll('.remote-page button,.remote-page input,.remote-page a.library-btn')]
                          .filter(el => el.getClientRects().length)
                          .map(el => ({label: el.textContent.trim() || el.id, width: el.getBoundingClientRect().width, height: el.getBoundingClientRect().height})),
                        taskCount: document.querySelectorAll('.remote-run').length,
                    })""")
                    assert metrics["document"] <= metrics["viewport"], metrics
                    assert all(control["width"] >= 44 and control["height"] >= 44 for control in metrics["controls"]), metrics
                    name = f"remote-ui-{'disabled-' if disabled else ''}{width}.png"
                    page.screenshot(path=str(args.output_dir / name), full_page=True)
                    metrics.update(screenshot=name, disabled=disabled)
                    pages.append(metrics)
                    if width == 390:
                        page.get_by_role("button", name="更多", exact=True).click()
                        link = page.get_by_role("navigation", name="更多导航").get_by_role("link", name="远程执行", exact=True)
                        assert link.is_visible()
                        page.get_by_role("dialog").get_by_role("button", name="关闭", exact=True).last.click()

                capture(390)
                capture(1280)
                # Browser interaction still uses real authenticated APIs/CSRF.
                page.get_by_role("button", name="提交诊断", exact=True).click()
                page.get_by_role("status").filter(has_text="任务已提交").wait_for()
                assert len(store.list_runs()) == 4
                os.environ["PIXIV_REMOTE_ENABLED"] = "0"
                _, active_client, _ = new_app()
                capture(390, disabled=True)
                capture(1280, disabled=True)
                assert not errors, errors
                assert not blocked, blocked
                browser.close()
        finally:
            for application in applications:
                application.extensions["close_work_analysis_owner"]()
        report = {"offline": True, "real_flask_auth": True, "provider_calls": 0,
                  "browser_errors": errors, "unexpected_requests": blocked,
                  "submission_verified": True, "pages": pages}
        (args.output_dir / "remote-ui-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"screenshots": len(pages), "overflow": False, "min_control_size": "44x44", "browser_errors": errors}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
