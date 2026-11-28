"""Bounded author enumeration followed by an independent reconciliation pass."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re


CURSOR_VERSION = 3
_EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


class AuthorHistoryCursor:
    def __init__(self, db, owner, author):
        self.db = db
        self.key = f"{owner}:{author}"
        saved = db.get_watermark("user_backup_page", self.key)
        self.state = saved if isinstance(saved, dict) else {}
        self.resumed = (self.state.get("cursor_version") == CURSOR_VERSION
                        and type(self.state.get("offset")) is int and self.state["offset"] > 0
                        and self._valid_digest(self.state.get("pass_digest")))
        self.offset = self.state["offset"] if self.resumed else 0
        self.digest = self.state["pass_digest"] if self.resumed else _EMPTY_DIGEST
        self.previous_digest = self.state.get("previous_digest") if self.state.get("cursor_version") == CURSOR_VERSION else None
        self.unavailable = set(self.state.get("unavailable_ids", [])) if self.resumed else set()
        self.enumerated = False
        self.verified = False

    @staticmethod
    def _valid_digest(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None

    def verify(self, offset, ids, next_offset):
        if (offset != self.offset or any(type(identity) is not int or not 0 < identity < 2**63 for identity in ids)
                or len(set(ids)) != len(ids) or (not ids and next_offset is not None)):
            raise ValueError("invalid author page")

    def advance(self, offset, ids, next_offset):
        digest = self.digest
        for identity in ids:
            digest = hashlib.sha256(bytes.fromhex(digest) + identity.to_bytes(8, "big")).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        self.enumerated = next_offset is None
        self.verified = self.enumerated and (offset == 0 or digest == self.previous_digest)
        compatible = self.state.get("cursor_version") == CURSOR_VERSION
        last_verified = self.state.get("last_verified_at") if compatible else None
        last_complete = self.state.get("last_archive_complete") if compatible else None
        self.state = {
            "cursor_version": CURSOR_VERSION,
            "offset": next_offset or 0,
            "pass_digest": digest if next_offset is not None else _EMPTY_DIGEST,
            "previous_digest": digest if self.enumerated else self.previous_digest,
            "unavailable_ids": sorted(self.unavailable),
            "completed_at": now if self.verified else None,
            "last_enumerated_at": now if self.enumerated else (self.state.get("last_enumerated_at") or self.state.get("completed_at")),
            "last_verified_at": now if self.verified else last_verified,
            "last_archive_complete": not self.unavailable if self.verified else last_complete,
        }
        self.db.update_watermark("user_backup_page", self.state, self.key)
        self.offset = next_offset or 0
        self.digest = digest
