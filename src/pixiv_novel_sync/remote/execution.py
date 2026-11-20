"""Business orchestration stays on the VPS while fixed work executes remotely."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import os
import threading
import time
import uuid

from .account_scope import ACCOUNT_LEASE_SECONDS, acquire_account, require_account, reserve_request
from .blobs import BlobStore
from .business import BusinessTasks
from .config import RemoteConfig
from .contracts import RemoteError
from .store import RemoteStore

_current = ContextVar("pixiv_execution", default=None)
PIXIV_TASKS = frozenset(
    {
        "bookmark",
        "following_list",
        "following_users",
        "following_novels",
        "subscribed_series",
        "user_backup",
        "user_status",
        "novel_status",
        "series_status",
        "pending_deletion_detection",
        "recommendation_run",
        "quick_sync",
    }
)


class ExecutionScope:
    def __init__(self, settings, *, stop_requested=lambda: False):
        cfg = RemoteConfig.from_env(os.environ, "web")
        if not cfg.enabled:
            raise RemoteError("remote_disabled", 503)
        self.store = RemoteStore(cfg.state_dir, cfg.machine_id)
        self.store.migrate()
        self.blobs = BlobStore(cfg.state_dir / "blobs")
        self.business = BusinessTasks(self.store, self.blobs)
        account_id = getattr(settings.pixiv, "user_id", None)
        if not account_id:
            from ..storage_db import Database

            db = Database(settings.storage.db_path)
            try:
                db.ensure_schema()
                profile = db.get_self_profile()
                account_id = profile.get("user_id") if profile else None
            finally:
                db.close()
        if not account_id:
            raise RemoteError("account_identity_required", 409)
        self.scope = hashlib.sha256(("pixiv:" + str(account_id)).encode()).hexdigest()
        self.owner = acquire_account(
            self.store, self.scope, max(1.0, settings.sync.delay_seconds_between_pages)
        )
        self.location = getattr(settings.sync, "pixiv_execution", "local")
        self.web_cookie = settings.pixiv.web_cookie
        self.stopped = stop_requested
        self._closed = threading.Event()
        self._lost = threading.Event()
        self._pending = set()
        self._ambiguous = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._renew, name="pixiv-account-lease", daemon=True)
        self._thread.start()

    def _renew(self):
        while not self._closed.wait(30):
            try:
                now = time.time()
                with self.store.transaction() as conn:
                    require_account(conn, self.scope, self.owner, now)
                    conn.execute(
                        "UPDATE account_leases SET expires_at=? WHERE scope=? AND owner=?",
                        (now + ACCOUNT_LEASE_SECONDS, self.scope, self.owner),
                    )
            except Exception:
                self._lost.set()
                return

    def check(self):
        if self.stopped():
            raise InterruptedError("Task stopped by user")
        if self._lost.is_set():
            raise RemoteError("account_lease_lost", 409)
        with self.store.read() as conn:
            require_account(conn, self.scope, self.owner, time.time())

    def check_commit(self):
        self.check()

    def local_permit(self):
        while True:
            self.check()
            with self.store.transaction() as conn:
                wait = reserve_request(conn, self.scope, self.owner, time.time())
                if not wait:
                    conn.execute(
                        "UPDATE account_leases SET expires_at=? WHERE scope=? AND owner=?",
                        (time.time() + ACCOUNT_LEASE_SECONDS, self.scope, self.owner),
                    )
            if not wait:
                return
            self._closed.wait(min(wait, 0.2))

    def call_api(self, api, method, args, params):
        from .pixiv_calls import normalize_call
        from pixivpy3.utils import JsonDict

        values = normalize_call(method, args, params)
        self.check()
        if self.location == "local":
            self.local_permit()
            method_call = getattr(api, method)
            return getattr(method_call, "_pixiv_retry_original", method_call)(**values)
        if method == "watchlist":
            cookie = self.web_cookie
            if not cookie:
                raise RemoteError("web_cookie_unavailable", 409)
            run = self.business.submit_watchlist(
                values["page"], cookie, self.scope, self.owner, uuid.uuid4().hex
            )
        else:
            run = self.business.submit_api(
                method, values, getattr(api, "access_token", None), self.scope, self.owner, uuid.uuid4().hex
            )
        result = self.wait_result(run)

        def convert(value):
            if isinstance(value, dict):
                return JsonDict({k: convert(v) for k, v in value.items()})
            if isinstance(value, list):
                return [convert(v) for v in value]
            return value

        return convert(result)

    def fetch_novel(self, api, novel_id, *, stop_requested=None):
        self.check()
        run = self.business.submit_novel(
            novel_id, getattr(api, "access_token", None), self.scope, self.owner, uuid.uuid4().hex
        )
        return self.wait_result(run, stop_requested=stop_requested)

    def wait_result(self, run, *, stop_requested=None):
        run_id = run["run_id"]
        with self._lock:
            self._pending.add(run_id)
        deadline = time.monotonic() + 650
        try:
            while time.monotonic() < deadline:
                self.check()
                if callable(stop_requested) and stop_requested():
                    raise InterruptedError("Task stopped by user")
                current = self.store.get_run(run_id)
                if current["state"] == "succeeded":
                    self.check()
                    return self.business.result(run_id)
                if current["state"] in ("failed", "cancelled", "expired"):
                    from ..pixiv_fetch import PixivFetchError

                    raise PixivFetchError(
                        "content_unavailable"
                        if current["error_code"] == "pixiv_unavailable"
                        else "remote_fetch_failed"
                    )
                self._closed.wait(0.25)
            raise RemoteError("remote_result_unknown", 503)
        except BaseException:
            if self.location == "remote":
                self._ambiguous = True
                self.store.cancel(run_id, time.time())
            raise
        finally:
            with self._lock:
                self._pending.discard(run_id)

    def close(self):
        self._closed.set()
        self._thread.join(timeout=2)
        with self.store.transaction() as conn:
            # Remote cancellation revokes publication immediately, but an in-flight
            # request may still be finishing. Preserve its spacing/lease grace.
            until = time.time() + (ACCOUNT_LEASE_SECONDS if self._ambiguous else 0)
            conn.execute(
                "UPDATE account_leases SET expires_at=? WHERE scope=? AND owner=?",
                (until, self.scope, self.owner),
            )


def current_execution():
    return _current.get()


class ScopedPixivAPI:
    def __init__(self, api, scope):
        self._api, self._execution = api, scope
        if scope.location == "local":
            from ..sync.utils import apply_pixiv_retry_patch

            apply_pixiv_retry_patch(self, stop_requested=scope.stopped)

    def __getattr__(self, name):
        from .pixiv_calls import CALLS

        if name in CALLS:
            return lambda *args, **kwargs: self._execution.call_api(self._api, name, args, kwargs)
        return getattr(self._api, name)


def bind_api(api):
    scope = current_execution()
    return ScopedPixivAPI(api, scope) if scope is not None and not isinstance(api, ScopedPixivAPI) else api


def watchlist_get(url, **kwargs):
    """The engine's two watchlist flows share this fixed remote boundary."""
    import requests
    from urllib.parse import urlsplit, parse_qs
    from types import SimpleNamespace

    scope = current_execution()
    if scope is None or scope.location == "local":
        if scope is not None:
            scope.local_permit()
        return requests.get(url, **kwargs)
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.pixiv.net"
        or parsed.path != "/ajax/watch_list/novel"
        or set(query) - {"p", "lang"}
    ):
        raise RemoteError("invalid_watchlist_target", 400)
    page = int(query.get("p", ["1"])[0])
    cookie = kwargs.get("headers", {}).get("Cookie")
    run = scope.business.submit_watchlist(page, cookie, scope.scope, scope.owner, uuid.uuid4().hex)
    result = scope.wait_result(run)
    return SimpleNamespace(status_code=result["status_code"], json=lambda: result["data"])


@contextmanager
def execution_scope(settings, task_type, stop_requested=lambda: False):
    kind = task_type.split(":", 1)[0]
    location = getattr(getattr(settings, "sync", None), "pixiv_execution", "local")
    if (
        _current.get() is not None
        or kind not in PIXIV_TASKS
        or (location == "local" and os.environ.get("PIXIV_REMOTE_ENABLED") != "1")
    ):
        yield _current.get()
        return
    scope = ExecutionScope(settings, stop_requested=stop_requested)
    token = _current.set(scope)
    try:
        yield scope
    finally:
        _current.reset(token)
        scope.close()
