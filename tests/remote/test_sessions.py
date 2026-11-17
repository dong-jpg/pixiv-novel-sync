import hashlib
import uuid

import pytest

from pixiv_novel_sync.remote.contracts import RemoteError
from pixiv_novel_sync.remote.store import RemoteStore


def test_replaced_boot_and_old_credential_cannot_reenroll(tmp_path):
    store = RemoteStore(tmp_path / "queue", "m_fixture")
    store.migrate()
    boot = str(uuid.uuid4())
    first_hash = hashlib.sha256(b"s" * 32).hexdigest()
    old = store.enroll(boot, first_hash, 1, "epub-v1", 1000)
    assert store.enroll(boot, first_hash, 1, "epub-v1", 1001)["session_id"] == old["session_id"]
    with pytest.raises(RemoteError, match="boot_conflict"):
        store.enroll(boot, "a" * 64, 1, "epub-v1", 1002)
    store.enroll(str(uuid.uuid4()), "b" * 64, 1, "epub-v1", 1003)
    with pytest.raises(RemoteError, match="retired_boot"):
        store.enroll(boot, first_hash, 1, "epub-v1", 1004)
    with pytest.raises(RemoteError):
        store.authenticate("s" * 32, 1004)


def test_heartbeat_never_revives_expired_session(tmp_path):
    store = RemoteStore(tmp_path / "queue", "m_fixture")
    store.migrate()
    session = store.enroll(str(uuid.uuid4()), "a" * 64, 1, "epub-v1", 1000)
    with pytest.raises(RemoteError):
        store.session_heartbeat(session["session_id"], 1900)
