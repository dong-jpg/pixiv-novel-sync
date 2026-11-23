from types import SimpleNamespace
import json
import time
import uuid

import pytest
from pixivpy3 import AppPixivAPI

from pixiv_novel_sync.remote.account_scope import acquire_account
from pixiv_novel_sync.remote.business import BusinessTasks
from pixiv_novel_sync.remote.contracts import digest
from pixiv_novel_sync.remote.runner import run_child
from pixiv_novel_sync.remote.worker_http import WorkerHTTP
from .test_end_to_end import LocalTransport


CASES = [
    ("bookmark-public", "user_bookmarks_novel", {"user_id": 7, "restrict": "public"},
     "/v1/user/bookmarks/novel", {"user_id": 7, "restrict": "public", "filter": "for_ios"}),
    ("bookmark-private-cursor", "user_bookmarks_novel", {"user_id": 7, "restrict": "private", "max_bookmark_id": 123},
     "/v1/user/bookmarks/novel", {"user_id": 7, "restrict": "private", "max_bookmark_id": 123, "filter": "for_ios"}),
    ("bookmark-legacy-offset", "user_bookmarks_novel", {"user_id": 7, "restrict": "public", "offset": 30},
     "/v1/user/bookmarks/novel", {"user_id": 7, "restrict": "public", "offset": 30, "filter": "for_ios"}),
    ("following-list", "user_following", {"user_id": 7, "offset": 30},
     "/v1/user/following", {"user_id": 7, "offset": 30, "restrict": "public"}),
    ("author-backup", "user_novels", {"user_id": 7, "offset": 30},
     "/v1/user/novels", {"user_id": 7, "offset": 30, "filter": "for_ios"}),
    ("user-status", "user_detail", {"user_id": 7},
     "/v1/user/detail", {"user_id": 7, "filter": "for_ios"}),
    ("novel-status", "novel_detail", {"novel_id": 42},
     "/v2/novel/detail", {"novel_id": 42}),
    ("series-status-and-subscription", "novel_series", {"series_id": 3, "last_order": 30},
     "/v2/novel/series", {"series_id": 3, "last_order": 30, "filter": "for_ios"}),
    ("recommendation-search", "search_novel", {"word": "synthetic", "offset": 30},
     "/v1/search/novel", {"word": "synthetic", "offset": 30, "search_target": "partial_match_for_tags",
                          "merge_plain_keyword_results": "true", "include_translated_tag_results": "true",
                          "sort": "date_desc", "filter": None}),
    ("novel-text", "webview_novel", {"novel_id": 42},
     "/webview/v2/novel", {"id": 42, "viewer_version": "20221031_ai"}),
    ("watchlist-and-pending-deletions", "watchlist", {"page": 2},
     "/ajax/watch_list/novel", {"p": 2, "lang": "zh"}),
]


@pytest.mark.parametrize("label,method,params,endpoint,wire_params", CASES, ids=[case[0] for case in CASES])
def test_fixed_pixiv_calls_cross_worker_http_with_real_sdk(
    remote_web_fixture, tmp_path, monkeypatch, label, method, params, endpoint, wire_params,
):
    fixture = remote_web_fixture
    monkeypatch.setenv("PIXIV_REMOTE_TASK_SECRET", "S" * 32)
    token = "SYNTHETIC_PRIVATE_PIXIV_TOKEN" * 2
    payload = {"novels": [], "user": {"id": 7}, "novel": {"id": 42}, "next_url": None}
    if method == "webview_novel":
        payload = {"id": 42, "text": "Synthetic smoke test text"}
    if method == "watchlist":
        payload = {"status_code": 200, "data": {"body": {"novels": [], "isLastPage": True}}}
    calls = []

    def fake_wire(api, verb, url, **kwargs):
        assert api.access_token == token
        assert api.requests.trust_env is False
        calls.append((verb, url, kwargs))
        raw = json.dumps(payload)
        if method == "webview_novel":
            raw = "novel: " + raw + ", isOwnWork: false"
        return SimpleNamespace(text=raw, headers={})

    monkeypatch.setattr(AppPixivAPI, "no_auth_requests_call", fake_wire)

    class WatchlistResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def iter_content(self, chunk_size):
            yield json.dumps(payload["data"]).encode()

    def fake_watchlist(session, url, **kwargs):
        assert session.trust_env is False
        assert kwargs["headers"]["Cookie"] == token
        assert kwargs["params"] == wire_params
        assert kwargs["allow_redirects"] is False
        calls.append(("GET", url, {"params": kwargs["params"]}))
        return WatchlistResponse()

    monkeypatch.setattr("requests.Session.get", fake_watchlist)
    client = WorkerHTTP("https://example.invalid", fixture.bootstrap, LocalTransport(SimpleNamespace(web=fixture)))
    try:
        client.enroll(str(uuid.uuid4()), "W" * 32)
        scope = "a" * 64
        owner = acquire_account(fixture.store, scope, 0, fixture.clock.now)
        tasks = BusinessTasks(fixture.store, fixture.blobs, clock=lambda: fixture.clock.now)
        run = (tasks.submit_watchlist(params["page"], token, scope, owner, label) if method == "watchlist"
               else tasks.submit_api(method, params, token, scope, owner, label))
        with fixture.store.transaction() as conn:
            conn.execute(
                "UPDATE machine_control SET mode='ready',provider_state='running',runtime_ready=1,"
                "last_provider_at=?,gate_ready_until=?,runtime_budget_until=-1",
                (fixture.clock.now, fixture.clock.now + 45),
            )
        grant = client.claim(label)
        assert grant is not None and grant.kind == "pixiv.api.v1"
        manifest = client.fetch_input(grant, "manifest")
        assert token.encode() not in manifest
        directory = tmp_path / "worker"
        directory.mkdir()
        manifest_path = directory / "manifest.json"
        manifest_path.write_bytes(manifest)

        def upstream(ordinal):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                response = client.upstream(grant, ordinal)
                if response["state"] == "ready":
                    return response["data"]
                time.sleep(.01)
            pytest.fail("fixed upstream did not return a receipt")

        result_path = directory / "result.json"
        run_child(grant.kind, manifest_path, None, None, result_path, permit=upstream)
        body = result_path.read_bytes()
        assert token.encode() not in body
        assert upstream(1) == payload
        client.stage(grant, result_path, digest(body), len(body))
        client.publish(grant, digest(body), len(body))
        assert fixture.store.get_run(run["run_id"])["state"] == "succeeded"
        assert tasks.result(run["run_id"]) == payload
        origin = "https://www.pixiv.net" if method == "watchlist" else "https://app-api.pixiv.net"
        expected = {"params": wire_params} if method == "watchlist" else {"params": wire_params, "req_auth": True}
        assert calls == [("GET", origin + endpoint, expected)]
    finally:
        client.close()
