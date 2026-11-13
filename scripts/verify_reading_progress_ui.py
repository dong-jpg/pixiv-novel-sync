"""离线 Chromium + 真实 Flask/CAS 接口核验阅读进度，无生产数据或外部请求。"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit
import uuid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vue-runtime", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    vue = args.vue_runtime.read_bytes()
    if base64.b64encode(hashlib.sha384(vue).digest()).decode() != "Z9a2xA0EIaURxthVmeQmPrgSC05eG8Lch8X4Y2+lXpTg9wcxPj2m3QNurDl5hLiS":
        raise RuntimeError("Vue runtime integrity mismatch")
    checkout = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(checkout / "src"))
    keep = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "HOME", "USERPROFILE",
        "LOCALAPPDATA", "APPDATA", "PLAYWRIGHT_BROWSERS_PATH",
    }}
    os.environ.clear()
    os.environ.update(keep)
    from playwright.sync_api import sync_playwright
    from pixiv_novel_sync.storage_db import Database
    from pixiv_novel_sync.webapp import create_app

    origin = "http://127.0.0.1:5018"
    errors, blocked, writes, captures, checks = [], [], [], [], []
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pixiv-reading-ui-") as temporary:
        root = Path(temporary)
        os.environ.update({"DASHBOARD_TOKEN": "fixture-reader-password", "PIXIV_FLASK_SECRET": "fixture-reader-secret",
            "PIXIV_REFRESH_TOKEN": "fixture-token", "PIXIV_DB_PATH": str(root / "library.db"),
            "PIXIV_PUBLIC_DIR": str(root / "public"), "PIXIV_PRIVATE_DIR": str(root / "private"),
            "PIXIV_REMOTE_ENABLED": "0"})
        env_path = root / "empty.env"
        env_path.write_text("", encoding="utf-8")
        db = Database(root / "library.db")
        db.init_schema()
        db.conn.execute("INSERT INTO users(user_id,name,raw_json) VALUES(1,'合成作者','{}')")
        db.conn.execute("""INSERT INTO novels(novel_id,user_id,title,visible,restrict_value,x_restrict,text_length,
            total_bookmarks,total_views,tags_json,raw_json,meta_hash,status)
            VALUES(42,1,'进度同步验收：另一台设备接着读',1,'public',0,100,0,0,'[]','{}','fixture','normal')""")
        db.conn.execute("INSERT INTO novel_texts(novel_id,text_raw,has_content,text_hash) VALUES(42,?,1,'fixture')",
            ("\n\n".join(f"第{index}段。这是用于验证阅读位置的合成文字。离线记录与服务器版本分别保留，其他设备的重置不会被旧页面覆盖。" for index in range(1, 101)),))
        db.conn.commit()
        db.close()
        app = create_app(config_path=str(root / "missing.yaml"), env_path=str(env_path), start_scheduler=False)
        app.config.update(TESTING=True, RAW_HTTP=True)

        def client():
            value = app.test_client()
            assert value.post("/api/auth/login", data={"token": "fixture-reader-password"}, base_url=origin).status_code == 302
            token = value.get("/api/csrf-token", base_url=origin).get_json()["csrf_token"]
            return value, token

        operator, token = client()
        endpoint = "/api/dashboard/novels/42/progress"

        def snapshot():
            response = operator.get(endpoint, base_url=origin)
            assert response.status_code == 200
            return response.get_json()

        def advance(progress, reset=False):
            current = snapshot()
            body = {"base_version": current["version"], "mutation_id": str(uuid.uuid4())}
            if not reset:
                body.update(progress=progress, status="completed" if progress == 100 else "reading" if progress else "unread")
            response = operator.open(endpoint, method="DELETE" if reset else "POST", json=body,
                base_url=origin, headers={"X-CSRF-Token": token})
            assert response.status_code == 200, response.get_json()
            return response.get_json()["record"]

        advance(60)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            contexts = []

            def device(width, legacy=None):
                flask_client, _ = client()
                context = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai",
                    viewport={"width": width, "height": 844 if width == 390 else 900})
                contexts.append(context)
                if legacy is not None:
                    context.add_init_script("localStorage.setItem('pixivNovelSync:reading:42', " + json.dumps(json.dumps(legacy)) + ");")
                device_state = {"offline": False}

                def dispatch(route):
                    request = route.request
                    parsed = urlsplit(request.url)
                    if request.url == "https://unpkg.com/vue@3.4.38/dist/vue.global.prod.js":
                        route.fulfill(status=200, body=vue, content_type="application/javascript", headers={"Access-Control-Allow-Origin": "*"})
                        return
                    if f"{parsed.scheme}://{parsed.netloc}" != origin:
                        blocked.append(request.url)
                        route.abort()
                        return
                    if device_state["offline"] and parsed.path == endpoint:
                        if request.method != "GET":
                            writes.append({"device": width, "method": request.method, "body": request.post_data_json, "status": "offline"})
                        route.abort()
                        return
                    path = parsed.path + ("?" + parsed.query if parsed.query else "")
                    headers = {key: value for key, value in request.headers.items() if key.lower() not in {"host", "cookie", "content-length"}}
                    response = flask_client.open(path, method=request.method, base_url=origin,
                        headers=headers, data=request.post_data_buffer)
                    if parsed.path == endpoint and request.method != "GET":
                        writes.append({"device": width, "method": request.method, "body": request.post_data_json, "status": response.status_code})
                    output_headers = {key: value for key, value in response.headers.items()
                        if key.lower() not in {"content-length", "transfer-encoding"}}
                    route.fulfill(status=response.status_code, headers=output_headers, body=response.get_data())

                context.route("**/*", dispatch)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(origin + "/dashboard/novels/42", wait_until="networkidle")
                page.wait_for_selector(".reader-body")
                page.wait_for_function("document.querySelector('.reader-sync-summary').textContent.trim() !== '核对进度'")
                return page, device_state

            def position(page):
                return page.evaluate("scrollY / Math.max(1,document.documentElement.scrollHeight-innerHeight)")

            def read_at(page, percent):
                page.evaluate("percent => { dispatchEvent(new WheelEvent('wheel', {deltaY: 1})); scrollTo(0, (document.documentElement.scrollHeight-innerHeight)*percent/100); dispatchEvent(new Event('scroll')); }", percent)

            def save(page):
                page.get_by_role("button", name="保存进度", exact=True).click()
                page.get_by_role("button", name="保存进度", exact=True).wait_for()

            def capture(page, name):
                metrics = page.evaluate("""() => ({viewport:innerWidth,document:document.documentElement.scrollWidth,
                    controls:[...document.querySelectorAll('.reader-tool,.reader-sync-options button')]
                      .filter(e => e.getClientRects().length).map(e => ({label:e.textContent.trim(),
                        width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height}))})""")
                assert metrics["document"] <= metrics["viewport"], metrics
                assert all(item["height"] >= 44 and item["width"] >= 44 for item in metrics["controls"]), metrics
                page.screenshot(path=str(args.output_dir / name), full_page=False, animations="disabled")
                captures.append(dict(metrics, screenshot=name))

            phone, phone_state = device(390)
            desktop, _ = device(1280)
            assert abs(position(phone) - .6) < .01
            assert abs(position(desktop) - .6) < .01
            assert not writes, "restoration must not generate a save"
            checks.append("two_devices_restore_current_server_without_writes")

            read_at(phone, 72)
            save(phone)
            phone.wait_for_function("document.querySelector('.reader-sync-summary').textContent.trim() === '已同步'")
            assert snapshot()["progress"] == 72
            read_at(desktop, 45)
            save(desktop)
            desktop.get_by_role("button", name="保留本机进度", exact=True).wait_for()
            assert abs(position(desktop) - .45) < .01
            capture(desktop, "reading-conflict-1280.png")
            desktop.get_by_role("button", name="使用服务器进度", exact=True).click()
            desktop.wait_for_function("document.querySelector('.reader-sync-summary').textContent.trim() === '已同步'")
            assert abs(position(desktop) - .72) < .01
            checks.append("stale_device_conflict_requires_explicit_choice")

            phone_state["offline"] = True
            read_at(phone, 81)
            save(phone)
            phone.get_by_role("button", name="重试同步", exact=True).wait_for()
            before_reload = writes[-1]["body"]
            capture(phone, "reading-offline-390.png")
            phone.reload(wait_until="networkidle")
            phone.wait_for_selector(".reader-body")
            advance(0, reset=True)
            phone_state["offline"] = False
            phone.evaluate("dispatchEvent(new Event('online'))")
            phone.get_by_role("button", name="保留本机进度", exact=True).wait_for()
            replay = next(write for write in reversed(writes) if write["status"] == 409 and write["device"] == 390)
            assert replay["body"] == before_reload
            assert snapshot()["is_reset"] is True
            capture(phone, "reading-reset-conflict-390.png")
            phone.get_by_role("button", name="保留本机进度", exact=True).click()
            phone.wait_for_function("document.querySelector('.reader-sync-summary').textContent.trim() === '已同步'")
            assert snapshot()["progress"] == 81
            assert snapshot()["is_reset"] is False
            assert abs(position(phone) - .81) < .01
            checks.append("offline_reload_retries_identical_mutation_and_respects_reset_tombstone")

            phone.get_by_role("button", name="更多", exact=True).click()
            phone.once("dialog", lambda dialog: dialog.accept())
            phone.get_by_role("button", name="重置阅读进度", exact=True).click()
            phone.wait_for_function("document.querySelector('#reader-action-feedback').textContent.includes('已重置')")
            phone.get_by_role("dialog").get_by_role("button", name="关闭", exact=True).click()
            assert snapshot()["is_reset"] is True
            assert abs(position(phone)) < .001
            capture(phone, "reading-reset-390.png")
            checks.append("explicit_reset_has_versioned_tombstone_and_no_scroll_revival")

            legacy, _ = device(390, {"novelId": 42, "scrollY": 1000, "progress": .56})
            legacy.get_by_role("button", name="保留本机进度", exact=True).wait_for()
            assert legacy.evaluate("JSON.parse(localStorage.getItem('pixivNovelSync:reading:42')).progress") == .56
            assert snapshot()["is_reset"] is True
            capture(legacy, "reading-legacy-conflict-390.png")
            checks.append("legacy_local_cache_preserved_as_explicit_candidate")

            assert not errors, errors
            assert not blocked, blocked
            for context in contexts:
                context.close()
            browser.close()
        app.extensions.get("close_work_analysis_owner", lambda: None)()
    report = {"offline_transport": True, "real_flask_auth_and_cas": True, "browser_errors": errors,
        "external_requests": blocked, "checks": checks, "captures": captures, "writes": writes}
    (args.output_dir / "reading-progress-ui-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"checks": len(checks), "screenshots": len(captures), "browser_errors": errors}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
