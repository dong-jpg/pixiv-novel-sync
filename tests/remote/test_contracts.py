import pytest

from pixiv_novel_sync.remote.contracts import RemoteError, request_digest, retry_delay


def test_registry_rejects_commands_and_invalid_identifiers():
    for kind in ("shell", "sync bookmark", "checkin", "epub.single.v2"):
        with pytest.raises(RemoteError, match="unsupported_kind"):
            request_digest(kind, None)
    for novel_id in (True, "42", 0, -1, 1.5):
        with pytest.raises(RemoteError, match="invalid_novel_id"):
            request_digest("epub.single.v1", novel_id)
    with pytest.raises(RemoteError, match="unexpected_novel_id"):
        request_digest("diagnostic.v1", 1)
    assert request_digest("epub.single.v1", 42) != request_digest("epub.single.v1", 43)


def test_retry_is_bounded_and_only_for_transient_pure_work():
    assert retry_delay("diagnostic.v1", 1, "lease_lost") == 30
    assert retry_delay("epub.single.v1", 2, "worker_restarted") == 60
    assert retry_delay("diagnostic.v1", 3, "lease_lost") is None
    assert retry_delay("checkin", 1, "worker_transient") is None
    assert retry_delay("diagnostic.v1", 1, "input_mismatch") is None
