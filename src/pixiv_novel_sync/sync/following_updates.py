from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from ..models import SourceRecord
from ..rate_limiter import cancellable_sleep
from .following_snapshot import read_following_snapshot
from .utils import response_is_pixiv_error_page, response_is_auth_failed, response_is_rate_limited


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _next_offset(next_url, offset, restrict):
    parsed = urlsplit(next_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme != "https" or parsed.netloc != "app-api.pixiv.net"
            or parsed.path != "/v1/novel/follow" or parsed.fragment
            or set(query) - {"offset", "restrict"}
            or query.get("restrict", [restrict]) != [restrict]
            or len(query.get("offset", [])) != 1):
        raise ValueError("invalid_feed_cursor")
    following = int(query["offset"][0])
    if following <= offset or following > 2**31 - 1:
        raise ValueError("invalid_feed_cursor")
    return following


def sync_updates(service, progress_callback=None):
    def check_cancel():
        if service.stop_requested and service.stop_requested():
            raise InterruptedError("Task stopped by user")

    def wait(seconds):
        cancellable_sleep(seconds, stop_requested=service.stop_requested)

    def incomplete(reason):
        stats.update(incomplete=True)
        failures.add(reason)
        stats["aborted_reason"] = ",".join(sorted(failures))

    owner = service.settings.pixiv.user_id
    stats = {"novels": 0, "skipped": 0, "failed": 0, "feed_pages": 0, "following_snapshot_used": True}
    failures = set()
    members = {}
    for restrict in ("public", "private"):
        check_cancel()
        users = read_following_snapshot(service.db, owner, restrict)
        if users is None:
            stats["following_snapshot_used"] = False
            refreshed = service.sync_following_list(progress_callback, restrict)
            if refreshed.get("incomplete"):
                incomplete("following_snapshot_unavailable")
                continue
            users = read_following_snapshot(service.db, owner, restrict)
        if users is None:
            incomplete("following_snapshot_unavailable")
            continue
        members[restrict] = {user.id for user in users}
    all_members = members.get("public", set()) | members.get("private", set())
    stats["users_total"] = len(all_members)
    settings = service.settings.sync
    for restrict in ("public", "private"):
        key = f"following_feed:{owner}:{restrict}"
        if restrict not in members:
            continue
        state = service.db.get_watermark(key) or {}
        previous_head = set(state.get("head_ids", []))
        scan_head = state.get("scan_head", [])
        offset = 0
        current_head = None
        resume_expected = None
        refreshed_members = False
        page_limit = min(20, max(3, int(settings.max_pages_per_run or 6)))
        for page in range(page_limit):
            check_cancel()
            if progress_callback:
                progress_callback("phase", {"phase": f"关注新作：{restrict} 第 {page + 1} 页"})
            try:
                response = service.api.novel_follow(restrict=restrict, offset=offset)
            except InterruptedError:
                raise
            except Exception as exc:
                if response_is_auth_failed(exc):
                    return {**stats, "incomplete": True, "aborted_reason": "auth_failed"}
                if response_is_rate_limited(exc):
                    return {**stats, "incomplete": True, "aborted_reason": "rate_limited"}
                incomplete("following_feed_unavailable")
                break
            stats["feed_pages"] += 1
            novels = _field(response, "novels")
            if response_is_auth_failed(response):
                return {**stats, "incomplete": True, "aborted_reason": "auth_failed"}
            if response_is_rate_limited(response):
                return {**stats, "incomplete": True, "aborted_reason": "rate_limited"}
            if response_is_pixiv_error_page(response) or not isinstance(novels, (list, tuple)):
                incomplete("following_feed_unavailable")
                break
            try:
                identifiers = [int(_field(novel, "id")) for novel in novels]
                author_ids = [int(_field(_field(novel, "user"), "id")) for novel in novels]
                if (any(value <= 0 or value > 2**63 - 1 for value in identifiers + author_ids)
                        or len(set(identifiers)) != len(identifiers)):
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                incomplete("invalid_feed_page")
                break
            if current_head is None:
                current_head = identifiers
            if resume_expected and not resume_expected.intersection(identifiers):
                stats.update(incomplete=True, rotation_pending=True, feed_backlog=True)
                break
            resume_expected = None
            if set(author_ids).difference(all_members) and not refreshed_members:
                refreshed = service.sync_following_list(progress_callback, restrict)
                refreshed_members = True
                stats["following_snapshot_used"] = False
                users = read_following_snapshot(service.db, owner, restrict)
                if refreshed.get("incomplete") or users is None:
                    incomplete("following_snapshot_unavailable")
                    break
                members[restrict] = {user.id for user in users}
                all_members = members.get("public", set()) | members.get("private", set())
                stats["users_total"] = len(all_members)
            check_cancel()
            with service.db.transaction() as connection:
                for novel_id, author_id in zip(identifiers, author_ids):
                    if author_id in all_members:
                        connection.execute(
                            "INSERT OR IGNORE INTO following_feed_pending(owner_id,novel_id,author_id,scope) VALUES(?,?,?,?)",
                            (owner, novel_id, author_id, restrict),
                        )
            next_url = _field(response, "next_url")
            try:
                next_offset = _next_offset(next_url, offset, restrict) if next_url is not None else None
                if next_offset is not None and not novels:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                incomplete("invalid_feed_cursor")
                break
            if next_offset is None or previous_head.intersection(identifiers):
                service.db.update_watermark(key, {"head_ids": current_head,
                    "completed_at": datetime.now(timezone.utc).isoformat()})
                break
            overlap = next((identity for identity in scan_head if identity in identifiers), None)
            checkpoint = {"head_ids": list(previous_head), "offset": next_offset,
                          "scan_head": current_head, "anchor_ids": identifiers, "anchor_offset": offset}
            service.db.update_watermark(key, checkpoint)
            if overlap is not None and state.get("offset") and state.get("anchor_ids"):
                shift = offset + identifiers.index(overlap) - scan_head.index(overlap)
                resume_offset = state.get("anchor_offset", 0) + shift
                if resume_offset > next_offset and page + 1 < page_limit:
                    next_offset = resume_offset
                    resume_expected = set(state["anchor_ids"])
                scan_head = []
            offset = next_offset
            wait(settings.delay_seconds_between_pages)
        else:
            stats.update(incomplete=True, rotation_pending=True, feed_backlog=True)
    budget = max(1, int(settings.max_items_per_run or 60))
    requests = 0
    pending = service.db.conn.execute(
        "SELECT novel_id,author_id,scope FROM following_feed_pending WHERE owner_id=? "
        "AND (retry_at IS NULL OR retry_at<=datetime('now')) ORDER BY novel_id DESC LIMIT 1000", (owner,)
    ).fetchall()
    for row in pending:
        check_cancel()
        novel_id, author_id, scope = row
        if author_id not in all_members and len(members) < 2:
            continue
        counters = {}
        if author_id in all_members and service._archive_complete_on_disk(novel_id, settings.download_assets):
            service.db.upsert_source(SourceRecord(novel_id, "following_user_scan", str(author_id)))
            stats["skipped"] += 1
        elif author_id in all_members:
            if requests >= budget:
                break
            requested_scope = "private" if author_id not in members.get("public", set()) else None
            from ..sync_engine import SyncAccountError
            try:
                counters = service._sync_novel(SimpleNamespace(id=novel_id), requested_scope, settings.download_assets,
                                               settings.write_markdown, settings.write_raw_text,
                                               "following_user_scan", str(author_id))
            except SyncAccountError as exc:
                incomplete(exc.code)
                break
            requests += int(not counters.get("failed_cooldown"))
            for name, value in counters.items():
                if type(value) is int:
                    stats[name] = stats.get(name, 0) + value
            if not counters.get("failed_cooldown"):
                wait(settings.delay_seconds_between_items)
        check_cancel()
        if counters.get("failed"):
            incomplete("novel_sync_errors")
            service.db.conn.execute(
                "UPDATE following_feed_pending SET retry_at=datetime('now','+6 hours') WHERE owner_id=? AND novel_id=?",
                (owner, novel_id),
            )
        elif counters.get("content_unavailable"):
            service.db.conn.execute(
                "UPDATE following_feed_pending SET retry_at=datetime('now','+7 days') WHERE owner_id=? AND novel_id=?",
                (owner, novel_id),
            )
        else:
            service.db.conn.execute("DELETE FROM following_feed_pending WHERE owner_id=? AND novel_id=?", (owner, novel_id))
    stats["feed_pending"] = service.db.conn.execute(
        "SELECT COUNT(*) FROM following_feed_pending WHERE owner_id=?", (owner,)
    ).fetchone()[0]
    if stats["feed_pending"]:
        stats.update(incomplete=True, rotation_pending=True)
    return stats
