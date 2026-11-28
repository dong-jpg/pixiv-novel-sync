from types import SimpleNamespace

import pytest
from pixivpy3 import PixivError

from pixiv_novel_sync import auth
from pixiv_novel_sync.jobs import services
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.sync.bookmark_pages import read_bookmark_page
from pixiv_novel_sync.sync_engine import BookmarkNovelSyncService
from test_user_backup_incremental import AUTHOR_ID, _env, _novel, _OfflineStorage


@pytest.fixture
def library(tmp_path, monkeypatch):
    settings, api = _env(tmp_path, monkeypatch)
    with Database(settings.storage.db_path) as db:
        service = BookmarkNovelSyncService(api, db, _OfflineStorage(settings), settings)
        yield service


@pytest.mark.parametrize("page", [None, {}, {"novels": None}, {"novels": "bad"},
                                  {"novels": []}, {"novels": [{"id": 3}]},
                                  {"novels": [{"id": True}]}, {"novels": [{"id": 1}, {"id": 1}]}])
def test_malformed_bookmark_page_cannot_create_or_clear_pending_deletions(library, page):
    service = library
    service.api.user_bookmarks_novel = lambda **params: SimpleNamespace(novels=[_novel(1), _novel(2), _novel(3)], next_url=None)
    service.sync(999, ["public"])
    service.db.add_pending_deletion("novel", 1, "unbookmarked", "title", "author", "")
    service.api.user_bookmarks_novel = lambda **params: page if params.get("offset") else SimpleNamespace(
        novels=[_novel(1), _novel(2)], next_url="https://app-api.pixiv.net/v1/user/bookmarks/novel?offset=30")
    with pytest.raises(ValueError):
        service.detect_unbookmarked_novels(999, ["public"])
    assert [row[0] for row in service.db.conn.execute("SELECT item_id FROM pending_deletions")] == [1]


@pytest.mark.parametrize("url", [
    "https://evil.test/v1/user/bookmarks/novel?offset=30",
    "https://app-api.pixiv.net/v1/user/bookmarks/novel?offset=30&user_id=8",
    "https://app-api.pixiv.net/v1/user/bookmarks/novel?offset=30&restrict=private",
    "https://app-api.pixiv.net/v1/user/bookmarks/novel?offset=30&offset=60",
    "https://app-api.pixiv.net/v1/user/bookmarks/novel?offset=0",
    "https://app-api.pixiv.net/v1/user/bookmarks/novel?max_bookmark_id=90",
])
def test_bookmark_cursor_must_preserve_scope_and_advance(url):
    current = {"user_id": 999, "restrict": "public", "max_bookmark_id": 90} if "max_bookmark" in url else {"user_id": 999, "restrict": "public"}
    with pytest.raises(ValueError):
        read_bookmark_page({"novels": [{"id": 1}], "next_url": url}, current)


def test_bookmark_id_cursor_and_legitimate_empty_last_page():
    current = {"user_id": 999, "restrict": "private", "max_bookmark_id": 90}
    novels, cursor = read_bookmark_page({"novels": [{"id": "1"}], "next_url":
        "https://app-api.pixiv.net/v1/user/bookmarks/novel?max_bookmark_id=80&user_id=999&restrict=private&filter=for_ios"}, current)
    assert int(novels[0].id) == 1 and cursor == {**current, "max_bookmark_id": 80}
    assert read_bookmark_page({"novels": [], "next_url": None}, cursor) == ([], None)


@pytest.mark.parametrize("message,reason", [("Rate Limit", "rate_limited"), ("invalid_grant", "auth_failed")])
def test_bookmark_account_error_stops_before_private_scope(library, message, reason):
    calls = []
    def listing(**params):
        calls.append(params["restrict"])
        return {"error": {"message": message}}
    library.api.user_bookmarks_novel = listing
    stats = library.sync(999, ["public", "private"])
    assert calls == ["public"] and stats["aborted_reason"] == reason and stats["failed"] == 0


def test_scope_conflict_preserves_files_without_starving_later_books(library):
    library.api.user_bookmarks_novel = lambda **params: SimpleNamespace(novels=[_novel(31)], next_url=None)
    library.sync(999, ["public"])
    relative = library.db.conn.execute("SELECT archive_dir FROM novels").fetchone()[0]
    target = library.storage.base_dir("private") / relative / "text.txt"
    target.parent.mkdir(parents=True)
    target.write_text("existing archive", encoding="utf-8")
    library.api.user_bookmarks_novel = lambda **params: SimpleNamespace(novels=[_novel(31), _novel(32)], next_url=None)
    stats = library.sync(999, ["private"])
    assert stats["aborted_reason"] == "archive_scope_error" and 32 in library.api.detail_calls
    assert target.read_text() == "existing archive"
    assert library.db.get_watermark("bookmark_scope_move", "31")


def test_new_bookmark_source_refreshes_catalog_without_rebuilding(library, monkeypatch):
    library._sync_novel(_novel(31), None, True, True, True, "user_backup", "7")
    library.db.set_rescue_override("novel", 31, "include")
    library.db.rebuild_rescue_catalog()
    monkeypatch.setattr(library.db, "rebuild_rescue_catalog", lambda: pytest.fail("unexpected full rebuild"))
    library.api.user_bookmarks_novel = lambda **params: SimpleNamespace(novels=[_novel(31)], next_url=None)
    assert library.sync(999, ["public"])["skipped"] == 1
    assert {row[0] for row in library.db.conn.execute("SELECT source_kind FROM rescue_catalog_sources WHERE item_id=31")} == {"bookmark", "user_backup"}


def test_author_interior_replacement_requires_reconciliation(library):
    identities = [100, 90, 80, 70, 60, 50]
    def listing(*, user_id, offset=0):
        return SimpleNamespace(novels=[_novel(identity) for identity in identities[offset:offset + 1]],
            next_url=str(offset + 1) if offset + 1 < len(identities) else None)
    library.api.user_novels = listing
    library.api.parse_qs = lambda url: {"offset": url} if url else None
    def backup():
        return services.run_user_backup_task(library.settings, AUTHOR_ID, api=library.api, scheduled_owner=999, rebuild_catalog=False)
    backup()
    backup()
    identities[1] = 95
    assert not backup()["history_complete"]
    for attempt in range(8):
        result = backup()
        assert result["pages_read"] <= 2
        if result["history_complete"]:
            break
    assert result["history_complete"] and set(identities) <= set(library.api.detail_calls)


def test_author_head_growth_does_not_restart_history(library):
    identities = [100, 90, 80, 70, 60, 50]
    original = set(identities)
    def listing(*, user_id, offset=0):
        return SimpleNamespace(novels=[_novel(identity) for identity in identities[offset:offset + 1]],
            next_url=str(offset + 1) if offset + 1 < len(identities) else None)
    library.api.user_novels = listing
    library.api.parse_qs = lambda url: {"offset": url} if url else None
    for attempt in range(6):
        if attempt:
            identities.insert(0, 100 + attempt)
        result = services.run_user_backup_task(library.settings, AUTHOR_ID, api=library.api, scheduled_owner=999, rebuild_catalog=False)
        assert result["pages_read"] <= 2 and not result["history_complete"]
    assert original <= set(library.api.detail_calls)


@pytest.mark.parametrize("status", [502, 503, 504, 429])
def test_login_transient_status_retries_without_logging_upstream_html(monkeypatch, caplog, status):
    api = SimpleNamespace(access_token="access", refresh_token="refresh")
    attempts = []
    def authenticate(**kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise PixivError(f"auth failed! check refresh_token.\nHTTP {status}: <html>secret diagnostic</html>")
        return {"user": {"id": 999}}
    api.auth = authenticate
    manager = auth.PixivAuthManager(SimpleNamespace(refresh_token="refresh", access_token=None, user_id=999))
    monkeypatch.setattr(manager, "create_api", lambda: api)
    delays = []
    monkeypatch.setattr(auth, "cancellable_sleep", lambda delay, stop: delays.append(delay))
    assert manager.login()[1].user_id == 999
    assert delays == [2, 4] and "secret diagnostic" not in caplog.text and "check refresh_token" not in caplog.text


def test_login_permanent_failure_and_cancelled_backoff(monkeypatch):
    calls = []
    cancelled = []
    failure = PixivError("HTTP 400: private data", body='{"error":{"message":"invalid_grant"}}')
    def authenticate(**kwargs):
        calls.append(1)
        raise failure
    manager = auth.PixivAuthManager(SimpleNamespace(refresh_token="refresh", access_token=None, user_id=999))
    monkeypatch.setattr(manager, "create_api", lambda: SimpleNamespace(auth=authenticate))
    with pytest.raises(auth.PixivAuthError) as error:
        manager.login()
    assert error.value.code == "auth_failed" and len(calls) == 1 and "private data" not in str(error.value)
    failure = PixivError("HTTP 504: private data")
    monkeypatch.setattr(auth, "cancellable_sleep", lambda delay, stop: cancelled.append(True))
    with pytest.raises(InterruptedError):
        manager.login(stop_requested=lambda: bool(cancelled))
    assert len(calls) == 2


def test_login_exhausted_gateway_errors_remain_distinct_from_bad_token(monkeypatch):
    def authenticate(**kwargs):
        raise PixivError("auth failed! check refresh_token.\nHTTP 504: <html>private data</html>")
    manager = auth.PixivAuthManager(SimpleNamespace(refresh_token="refresh", access_token=None, user_id=999))
    monkeypatch.setattr(manager, "create_api", lambda: SimpleNamespace(auth=authenticate))
    monkeypatch.setattr(auth, "cancellable_sleep", lambda delay, stop: None)
    with pytest.raises(auth.PixivAuthError) as error:
        manager.login()
    assert error.value.code == "auth_unavailable" and "504" in str(error.value)
    assert "private data" not in str(error.value) and "refresh_token" not in str(error.value)


def test_nested_pixiv_oauth_payload_preserves_identity_and_profile(monkeypatch):
    api = SimpleNamespace(access_token="access", refresh_token="refresh",
        auth=lambda **kwargs: {"response": {"user": {"id": 999, "name": "owner"}}})
    manager = auth.PixivAuthManager(SimpleNamespace(refresh_token="refresh", access_token=None, user_id=None))
    monkeypatch.setattr(manager, "create_api", lambda: api)
    result = manager.login()[1]
    assert result.user_id == 999 and result.self_profile()["name"] == "owner"


def test_login_recognizes_sdk_wrapped_timeout_but_not_certificate_error(monkeypatch):
    import requests

    attempts = []
    problem = requests.Timeout("internal transport detail")
    def authenticate(**kwargs):
        attempts.append(1)
        try:
            raise problem
        except requests.RequestException:
            raise PixivError("SDK wrapped the transport error")

    manager = auth.PixivAuthManager(SimpleNamespace(refresh_token="refresh", access_token=None, user_id=999))
    monkeypatch.setattr(manager, "create_api", lambda: SimpleNamespace(auth=authenticate))
    monkeypatch.setattr(auth, "cancellable_sleep", lambda delay, stop: None)
    with pytest.raises(auth.PixivAuthError) as error:
        manager.login()
    assert error.value.code == "auth_unavailable" and len(attempts) == 3
    assert "internal transport detail" not in str(error.value)
    problem = requests.exceptions.SSLError("certificate verification failed")
    with pytest.raises(auth.PixivAuthError):
        manager.login()
    assert len(attempts) == 4


@pytest.mark.parametrize("code", ["invalid_grant", "invalid_token"])
def test_login_recognizes_standard_oauth_error_strings(monkeypatch, code):
    def authenticate(**kwargs):
        raise PixivError("HTTP 400: check refresh_token", body={"error": code})

    manager = auth.PixivAuthManager(SimpleNamespace(refresh_token="refresh", access_token=None, user_id=999))
    monkeypatch.setattr(manager, "create_api", lambda: SimpleNamespace(auth=authenticate))
    monkeypatch.setattr(auth, "cancellable_sleep", lambda *args: pytest.fail("unexpected retry"))
    with pytest.raises(auth.PixivAuthError) as error:
        manager.login()
    assert error.value.code == "auth_failed" and "refresh_token" not in str(error.value)


@pytest.mark.parametrize("page", [SimpleNamespace(novels=[]), SimpleNamespace(novels=[SimpleNamespace(id=True)], next_url=None)])
def test_incomplete_author_responses_never_publish_history_completion(library, page):
    library.api.user_novels = lambda **params: page
    result = services.run_user_backup_task(library.settings, AUTHOR_ID, api=library.api, scheduled_owner=999, rebuild_catalog=False)
    assert result["aborted_reason"] == "upstream_error_page" and not result["history_verified"]
    assert not library.db.get_watermark("user_backup_page", f"999:{AUTHOR_ID}")


def test_dashboard_counts_following_members_and_subscriptions_not_all_metadata(library):
    from pixiv_novel_sync.sync.following_snapshot import save_following_snapshot

    db = library.db
    save_following_snapshot(db, 999, [SimpleNamespace(id=7)])
    save_following_snapshot(db, 999, [SimpleNamespace(id=7), SimpleNamespace(id=8)], "private")
    db.conn.execute("INSERT INTO users(user_id,name,raw_json) VALUES(8,'private','{}'),(9,'unrelated','{}')")
    db.conn.execute("INSERT INTO series(series_id,title,user_id,is_subscribed) VALUES(1,'subscribed',7,1),(2,'metadata',9,0)")
    db.conn.execute("INSERT INTO following_feed_pending VALUES(999,31,7,'public',NULL),(99,32,7,'public',NULL)")
    stats = db.dashboard_stats(999)
    assert stats["users_count"] == 2 and stats["series_count"] == 1 and stats["feed_pending"] == 1
    assert db.dashboard_stats(None) == stats
    assert db.dashboard_stats(88)["users_count"] is None


def test_plain_paragraphs_do_not_build_markup_indexes(monkeypatch):
    from pixiv_novel_sync import pixiv_markup

    monkeypatch.setattr(pixiv_markup, "_pair_brackets", lambda text: pytest.fail("unexpected bracket index"))
    for text in ("第一段正文", "Author: example", "引用 [序言]", "未完成的 [b:标题"):
        assert pixiv_markup.parse(text) == [[{"type": "paragraph", "indent": "", "content": [text]}]]
