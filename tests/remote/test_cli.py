from __future__ import annotations

import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from pixiv_novel_sync.remote.cli import run_cli


def test_doctor_disabled_is_offline_and_redacted(capsys, tmp_path):
    assert run_cli(["doctor"], env={}) == 0
    assert "disabled" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_disabled_controller_creates_no_state_directory(tmp_path, capsys):
    root = tmp_path / "not-created"
    assert run_cli(["controller", "--once"], env={"PIXIV_REMOTE_STATE_DIR": str(root)}) == 0
    assert not root.exists()
    assert "disabled" in capsys.readouterr().out


def test_parser_does_not_accept_arbitrary_command(capsys):
    assert run_cli(["worker", "--command", "anything"], env={}) == 2
    assert "anything" not in capsys.readouterr().out


def test_enabled_doctor_only_checks_explicit_configuration(tmp_path, capsys):
    root = tmp_path / "remote-not-created"
    env = {"PIXIV_REMOTE_ENABLED": "1", "PIXIV_REMOTE_STATE_DIR": str(root),
           "PIXIV_REMOTE_MACHINE_ID": "m_fixture", "PIXIV_REMOTE_WORKER_KEY_SHA256": "b" * 64}
    assert run_cli(["doctor"], env=env) == 0
    output = capsys.readouterr().out
    assert "creation_required" in output
    assert "not_checked" in output
    assert not root.exists()
    assert "b" * 64 not in output


def test_worker_cli_uses_dedicated_entry_without_loading_business_settings(tmp_path, monkeypatch):
    class FakeWorker:
        last_error = None
        def __init__(self, client, work_dir):
            assert work_dir == tmp_path / "scratch"
        def tick(self):
            return 0
        def stop(self):
            pass

    monkeypatch.setitem(sys.modules, "pixiv_novel_sync.remote.worker", SimpleNamespace(Worker=FakeWorker))
    env = {"PIXIV_REMOTE_SERVER_ORIGIN": "https://example.invalid", "PIXIV_REMOTE_WORKER_KEY": "b" * 32,
           "PIXIV_REMOTE_WORK_DIR": str(tmp_path / "scratch")}
    assert run_cli(["worker", "--once"], env=env) == 0
    assert not (tmp_path / "scratch").exists()


def test_parse_failure_does_not_repeat_pasted_secret(capsys):
    assert run_cli(["doctor", "--token", "accidental-secret"], env={}) == 2
    captured = capsys.readouterr()
    assert "accidental-secret" not in captured.out + captured.err


def test_controller_once_reports_a_persisted_provider_failure(tmp_path, monkeypatch):
    from pixiv_novel_sync.remote.instabox import ProviderError
    import pixiv_novel_sync.remote.instabox as instabox

    class DeniedProvider:
        def machine(self):
            raise ProviderError("permission_denied", 403)

    monkeypatch.setattr(instabox, "InstaBoxClient", lambda *args: DeniedProvider())
    env = {"PIXIV_REMOTE_ENABLED": "1", "PIXIV_REMOTE_STATE_DIR": str(tmp_path / "state"),
           "PIXIV_REMOTE_MACHINE_ID": "m_fixture", "INSTABOX_API_KEY": "fake-provider-key"}
    assert run_cli(["controller", "--once"], env=env) == 1


@pytest.mark.parametrize("role,expected_wait", [("controller", 15), ("worker", 1)])
def test_process_loop_honors_controller_poll_schedule_and_worker_supervision(role, expected_wait, tmp_path, monkeypatch):
    import pixiv_novel_sync.remote.cli as cli
    from pixiv_novel_sync.remote.config import RemoteConfig

    waits = []

    class StopEvent:
        stopped = False
        def is_set(self):
            return self.stopped
        def set(self):
            self.stopped = True
        def wait(self, seconds):
            waits.append(seconds)
            self.set()

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            pass
        def tick(self):
            return 115
        def stop(self):
            pass

    class FakeStore:
        def __init__(self, *args):
            pass
        def migrate(self):
            pass
        def close(self):
            pass

    monkeypatch.setattr(cli.threading, "Event", StopEvent)
    monkeypatch.setattr(cli.time, "time", lambda: 100)
    monkeypatch.setattr(cli, "_signals", lambda event: nullcontext(event))
    monkeypatch.setitem(sys.modules, "pixiv_novel_sync.remote.worker", SimpleNamespace(Worker=FakeProcess))
    monkeypatch.setitem(sys.modules, "pixiv_novel_sync.remote.worker_http", SimpleNamespace(WorkerHTTP=lambda *args: object()))
    monkeypatch.setitem(sys.modules, "pixiv_novel_sync.remote.lifecycle", SimpleNamespace(Controller=FakeProcess))
    monkeypatch.setitem(sys.modules, "pixiv_novel_sync.remote.control_store", SimpleNamespace(ControlStore=lambda *args: object()))
    monkeypatch.setitem(sys.modules, "pixiv_novel_sync.remote.instabox", SimpleNamespace(InstaBoxClient=lambda *args: object()))
    monkeypatch.setitem(sys.modules, "pixiv_novel_sync.remote.store", SimpleNamespace(RemoteStore=FakeStore))
    cfg = RemoteConfig(True, role, state_dir=tmp_path / "state", machine_id="m_fixture",
                       server_origin="https://example.invalid", work_dir=tmp_path / "work", api_key="fake", worker_key="b" * 32)
    assert cli._serve(SimpleNamespace(command=role, once=False), cfg) == 0
    assert waits == [expected_wait]
