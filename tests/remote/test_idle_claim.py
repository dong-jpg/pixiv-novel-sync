from .test_store import ready_store, enqueue


def test_empty_claims_skip_disk_probes_but_new_demand_checks_capacity(tmp_path, monkeypatch):
    store, session = ready_store(tmp_path / "queue")
    probes = []
    original = store._probe_receiver

    def probe():
        probes.append(True)
        return original()

    monkeypatch.setattr(store, "_probe_receiver", probe)
    for index in range(20):
        assert store.claim(session["session_id"], f"idle-{index}", 1001) is None
    assert probes == []
    enqueue(store)
    assert store.claim(session["session_id"], "new-demand", 1002) is not None
    assert probes == [True]


def test_demand_appearing_after_empty_precheck_waits_for_next_claim(tmp_path, monkeypatch):
    store, session = ready_store(tmp_path / "queue")
    created = []

    # Simulate the enqueue at the read/write transaction boundary, before sweep.
    from contextlib import contextmanager

    transaction = store.transaction

    @contextmanager
    def racing_transaction():
        if not created:
            created.append(True)
            monkeypatch.setattr(store, "transaction", transaction)
            enqueue(store)
        with transaction() as conn:
            yield conn

    monkeypatch.setattr(store, "transaction", racing_transaction)
    assert store.claim(session["session_id"], "raced", 1001) is None
    assert store.claim(session["session_id"], "next", 1002) is not None
