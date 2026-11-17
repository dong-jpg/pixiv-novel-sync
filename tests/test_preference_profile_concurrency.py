from contextlib import contextmanager

import pytest

from pixiv_novel_sync.storage_db import Database


def test_deleted_profile_cannot_clear_default_before_update(tmp_path, monkeypatch):
    path = tmp_path / "profile-race.db"
    with Database(path) as db, Database(path) as other:
        db.init_schema()
        default_id = db.create_preference_profile({"name": "Default", "is_default": True})
        target_id = db.create_preference_profile({"name": "Target"})
        transaction = db.transaction
        interleaved = False

        @contextmanager
        def delete_before_write_lock():
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                other.delete_preference_profile(target_id)
            with transaction() as conn:
                yield conn

        monkeypatch.setattr(db, "transaction", delete_before_write_lock)
        with pytest.raises(ValueError, match="偏好画像不存在"):
            db.update_preference_profile(target_id, {"is_default": True})

        assert interleaved
        assert db.get_default_preference_profile()["id"] == default_id
        assert db.get_preference_profile(target_id) is None
