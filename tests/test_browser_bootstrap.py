"""Exercise the shipped assets and Flask responses with all external requests blocked."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import re
from urllib.parse import urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "src/pixiv_novel_sync/static/vendor/vue-3.4.38.global.prod.js"
VUE_PATH = "/static/vendor/vue-3.4.38.global.prod.js"


def test_vue_is_pinned_packaged_and_served_without_authentication(tmp_path, monkeypatch):
    from pixiv_novel_sync.webapp import create_app

    data = ASSET.read_bytes()
    template = (ASSET.parents[2] / "templates/base.html").read_text(encoding="utf-8")
    version = hashlib.sha256(data).hexdigest()[:12]
    integrity = base64.b64encode(hashlib.sha384(data).digest()).decode("ascii")
    assert f'src="{VUE_PATH}?v={version}" integrity="sha384-{integrity}"' in template
    assert not re.search(r'<script[^>]+src="https?://', template)
    assert "The MIT License" in ASSET.with_name("vue-LICENSE.txt").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"static/vendor/*.js"' in pyproject and '"static/vendor/*.txt"' in pyproject
    monkeypatch.setenv("DASHBOARD_TOKEN", "fixture-bootstrap-password")
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    client = create_app(config_path=str(tmp_path / "missing.yaml"), env_path=str(env), start_scheduler=False).test_client()
    response = client.get(VUE_PATH + "?v=" + version, environ_base={"REMOTE_ADDR": "203.0.113.2"})
    try:
        assert response.status_code == 200
        assert response.mimetype in {"text/javascript", "application/javascript"}
        assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
        assert response.get_data() == data
    finally:
        response.close()


@pytest.fixture(scope="module", params=("chromium", "firefox"))
def bootstrap_browser(request):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        engine = getattr(runtime, request.param)
        if not Path(engine.executable_path).is_file():
            pytest.skip(f"Install the optional {request.param} browser with playwright install {request.param}")
        instance = engine.launch(headless=True)
        yield instance
        instance.close()


@pytest.mark.parametrize("width", (390, 1280))
def test_authenticated_pages_boot_without_cdn(bootstrap_browser, width, tmp_path, monkeypatch, record_property):
    from pixiv_novel_sync.storage_db import Database
    from pixiv_novel_sync.webapp import create_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "fixture-bootstrap-password")
    monkeypatch.setenv("PIXIV_REFRESH_TOKEN", "fixture-token")
    monkeypatch.setenv("PIXIV_REMOTE_ENABLED", "0")
    env = tmp_path / "empty.env"
    env.write_text("", encoding="utf-8")
    db = Database(tmp_path / "state/test.db")
    try:
        db.init_schema()
        db.conn.execute("INSERT INTO users(user_id,name,raw_json) VALUES(1,'Fixture author','{}')")
        db.conn.execute("""INSERT INTO novels(novel_id,user_id,title,visible,restrict_value,x_restrict,text_length,
            total_bookmarks,total_views,tags_json,raw_json,meta_hash,status)
            VALUES(42,1,'Offline browser fixture',1,'public',0,100,0,0,'[]','{}','fixture','normal')""")
        text = "\n\n".join(f"第 {index} 段。" + "用于跨浏览器验证正文显示与滚动。" * 10 for index in range(1000))
        db.conn.execute("INSERT INTO novel_texts(novel_id,text_raw,has_content,text_hash) VALUES(42,?,1,'fixture')", (text,))
        db.conn.commit()
    finally:
        db.close()
    app = create_app(config_path=str(tmp_path / "missing.yaml"), env_path=str(env), start_scheduler=False)
    app.config.update(TESTING=True, RAW_HTTP=True)
    client = app.test_client()
    origin = "http://127.0.0.1:5018"
    assert client.post("/api/auth/login", data={"token": "fixture-bootstrap-password"}, base_url=origin).status_code == 302
    errors, external, assets = [], [], []
    context = bootstrap_browser.new_context(viewport={"width": width, "height": 900}, locale="zh-CN")

    def dispatch(route):
        request = route.request
        url = urlsplit(request.url)
        if url.scheme + "://" + url.netloc != origin:
            external.append(request.url)
            return route.abort("blockedbyclient")
        headers = {key: value for key, value in request.headers.items() if key.lower() not in {"host", "cookie", "content-length"}}
        response = client.open(url.path + ("?" + url.query if url.query else ""), method=request.method,
                               base_url=origin, headers=headers, data=request.post_data_buffer)
        try:
            if url.path == VUE_PATH:
                assets.append(response.status_code)
                assert response.get_data() == ASSET.read_bytes()
            output = {key: value for key, value in response.headers.items() if key.lower() not in {"content-length", "transfer-encoding"}}
            return route.fulfill(status=response.status_code, headers=output, body=response.get_data())
        finally:
            response.close()

    context.route("**/*", dispatch)
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        for path in ("/dashboard", "/dashboard/novels", "/dashboard/novels/42"):
            page.goto(origin + path, wait_until="networkidle")
            page.wait_for_selector("#app:not([v-cloak])")
            assert page.evaluate("Vue.version") == "3.4.38"
            assert page.locator("#app").is_visible()
            assert not page.get_by_text("加载失败", exact=True).count()
            metrics = page.evaluate("""() => ({load: performance.getEntriesByType('navigation')[0].domContentLoadedEventEnd,
                width: document.documentElement.scrollWidth, viewport: innerWidth})""")
            assert metrics["width"] <= metrics["viewport"] + 1
            record_property(f"{bootstrap_browser.browser_type.name}_{width}_{path}_dom_ms", metrics["load"])
        assert "第 999 段" in page.locator("#app").inner_text()
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight / 2)")
        page.wait_for_function("window.scrollY > 0")
        assert external == []
        assert errors == []
        assert assets == [200, 200, 200]
    finally:
        context.close()
