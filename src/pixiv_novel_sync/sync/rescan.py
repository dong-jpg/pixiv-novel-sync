"""Bounded bookmark sweeps coexist with frequent newest-page checks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def migrate_rescans(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS archive_refreshes(
        novel_id INTEGER PRIMARY KEY REFERENCES novels(novel_id) ON DELETE CASCADE,
        checked_at TEXT NOT NULL)"""
    )


class BookmarkSweep:
    def __init__(self, db, user_id, restrict, days, page_limit, *, now=None):
        self.db, self.initial = db, {"user_id": user_id, "restrict": restrict}
        self.key = f"bookmark_sweep_v1:{user_id}:{restrict}"
        self.now = now or datetime.now(timezone.utc)
        self.state = db.get_watermark(self.key) or {}
        self.active = False
        self.current = dict(self.initial)
        self.page_limit = page_limit
        self.head_only = False
        self.unchanged_pages = 0
        if days <= 0:
            return
        try:
            baseline = datetime.fromisoformat(self.state.get("completed_at") or self.state["baseline_at"])
            if baseline.tzinfo is None:
                raise ValueError
        except (KeyError, ValueError, TypeError):
            self.state = {"baseline_at": self.now.isoformat()}
            self.save()
            return
        if self.state.get("started_at") or self.now >= baseline + timedelta(days=days):
            self.active = True
            self.state.setdefault("started_at", self.now.isoformat())
            cursor = self.state.get("cursor")
            if (
                not isinstance(cursor, dict)
                or cursor.get("user_id") != user_id
                or cursor.get("restrict") != restrict
                or set(cursor) - {"user_id", "restrict", "offset", "max_bookmark_id"}
                or any(
                    type(cursor.get(key, 0)) is not int or cursor.get(key, 0) < 0
                    for key in ("offset", "max_bookmark_id")
                )
            ):
                self.state["cursor"] = dict(self.initial)
            if page_limit == 1 and self.has_cursor():
                self.head_only = not self.state.get("head_only", False)
                self.state["head_only"] = self.head_only
            self.save()

    def save(self):
        self.db.update_watermark(self.key, self.state)

    def has_cursor(self):
        return any(self.state.get("cursor", {}).get(key, 0) for key in ("offset", "max_bookmark_id"))

    def initial_query(self):
        if self.active and self.page_limit == 1 and not self.head_only:
            self.current = dict(self.state["cursor"])
        return self.current

    def needs_refresh(self, novel_id):
        if not self.active or self.head_only:
            return False
        row = self.db.conn.execute(
            "SELECT checked_at FROM archive_refreshes WHERE novel_id=?", (novel_id,)
        ).fetchone()
        return row is None or row[0] < self.state["started_at"]

    def can_stop_incremental(self, page_number, *, page_unchanged):
        self.unchanged_pages = self.unchanged_pages + 1 if page_unchanged else 0
        return (
            not self.active and "completed_at" in self.state and page_unchanged and self.unchanged_pages >= 2
        )

    def next_query(self, parsed, page_number, *, page_ok):
        if not self.active or self.head_only:
            self.current = parsed
            return parsed
        cursor = self.state["cursor"]
        # Always inspect the newest page first when the budget allows two pages.
        # Do not replace a saved continuation with the newest page's next URL.
        if page_number == 1 and self.page_limit > 1 and self.has_cursor():
            self.current = dict(cursor)
            return self.current
        if not page_ok:
            return None  # Retry this page next time; successful per-novel checks are remembered.
        if parsed is None:
            self.state = {"baseline_at": self.state["baseline_at"], "completed_at": self.now.isoformat()}
        else:
            field = "max_bookmark_id" if "max_bookmark_id" in parsed else "offset"
            offset = parsed.get(field)
            if isinstance(offset, str) and offset.isdecimal():
                offset = int(offset)
            if type(offset) is not int or offset < 0:
                return parsed  # Unknown cursor shape cannot become a durable remote target.
            self.state["cursor"] = {**self.initial, field: offset}
        self.save()
        self.current = parsed
        return parsed


def record_refresh(db, novel_id):
    db.conn.execute(
        "INSERT INTO archive_refreshes(novel_id,checked_at) VALUES(?,?) "
        "ON CONFLICT(novel_id) DO UPDATE SET checked_at=excluded.checked_at",
        (novel_id, datetime.now(timezone.utc).isoformat()),
    )
