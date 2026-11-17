import threading

from pixiv_novel_sync.preference_learning.library import LibraryAnalysisService
from pixiv_novel_sync.storage_db import Database
from test_library_analysis import add_novel, created_runs
from test_work_analysis_coordinator import make_bridge, wait_for


def _enable_library(bridge):
    old = bridge.env.db.get_work_analysis(bridge.env.run_id)
    bridge.coordinator.request_control(bridge.env.run_id, "cancel", old["version"])
    add_novel(bridge.env.db, 100)
    service = LibraryAnalysisService(bridge.env.db_factory, lambda: bridge.env.service, bridge.coordinator)
    service.startup_recover()
    bridge.coordinator.set_discovery_hook(service.tick)
    configuration = {
        "scope": "all", "agent_id": bridge.env.agent_id, "output_tokens": 128,
        "work_budget": {"request_limit": 16, "token_limit": 500000, "active_seconds_limit": 600},
        "batch_budget": {"request_limit": 64, "token_limit": 2000000, "active_seconds_limit": 2400},
        "work_limit": 4, "interval_minutes": 60,
        "confirmations": {"external_send_confirmed": True, "budget_confirmed": True,
                          "current_archive_only": True, "order_confirmed": True},
    }
    preview = service.preview(configuration)
    service.policy({**configuration, "enabled": True, "expected_version": 1,
                    "preview_fingerprint": preview["preview_fingerprint"]})
    return service


def test_disabling_policy_stops_child_before_publish_without_releasing_busy_slot(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    try:
        service = _enable_library(bridge)
        assert bridge.entered.wait(timeout=5)
        run = created_runs(bridge.env.db)[0]
        before = bridge.env.db.get_work_analysis(run["run_id"], include_private=True)
        policy = service.state()["policy"]
        disabled = service.policy({"enabled": False, "expected_version": policy["version"]})
        stopping = bridge.env.db.get_work_analysis(run["run_id"], include_private=True)
        assert not disabled["enabled"] and stopping["status"] == "pausing"
        assert stopping["epoch"] == before["epoch"]
        assert bridge.manager.acquire_run_slot() is False
        bridge.release_provider.set()
        wait_for(bridge.manager.shared_admission_is_idle)
        result = bridge.env.db.get_work_analysis(run["run_id"], include_private=True)
        assert result["status"] == "paused" and result["report"] is None
        assert result["request_count"] == 1
        assert service.state()["policy"]["allocated"] == disabled["allocated"]
        service.tick()
        assert len(created_runs(bridge.env.db)) == 1
    finally:
        bridge.close()


def test_disabling_policy_respects_an_existing_finalization_claim(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    publishing, release = threading.Event(), threading.Event()
    original = Database.publish_work_analysis

    def hold_publication(self, *args, **kwargs):
        publishing.set()
        assert release.wait(timeout=5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Database, "publish_work_analysis", hold_publication)
    bridge.release_provider.set()
    try:
        service = _enable_library(bridge)
        assert publishing.wait(timeout=5)
        run = created_runs(bridge.env.db)[0]
        policy = service.state()["policy"]
        disabled = service.policy({"enabled": False, "expected_version": policy["version"]})
        assert disabled["enabled"] is False
        assert bridge.env.db.get_work_analysis(run["run_id"])["status"] == "running"
        assert bridge.manager.acquire_run_slot() is False
        release.set()
        wait_for(bridge.manager.shared_admission_is_idle)
        assert bridge.env.db.get_work_analysis(run["run_id"])["status"] == "succeeded"
        assert len(bridge.env.provider.sent_units) == 1
    finally:
        release.set()
        bridge.close()


def test_discovery_hook_never_runs_under_an_outer_control_scope(tmp_path, monkeypatch):
    bridge = make_bridge(tmp_path, monkeypatch)
    seen = []

    def observe(reason):
        assert not bridge.coordinator._gate._is_owned()
        seen.append(reason)

    bridge.coordinator.set_discovery_hook(observe)
    try:
        with bridge.coordinator.work_control_scope():
            bridge.coordinator.wake("created")
        assert not seen
        bridge.coordinator.wake("scheduler_tick")
        assert seen == ["scheduler_tick"]
    finally:
        bridge.close()
