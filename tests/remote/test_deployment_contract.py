from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_remote_services_have_separate_secrets_and_cold_boot_entrypoints():
    controller = (ROOT / "deploy/systemd/pixiv-remote-controller.service").read_text(encoding="utf-8")
    worker = (ROOT / "deploy/systemd/pixiv-remote-worker.service").read_text(encoding="utf-8")
    assert "EnvironmentFile=/etc/pixiv-remote/controller.env" in controller
    assert "EnvironmentFile=/etc/pixiv-remote/worker.env" in worker
    for source, role in ((controller, "controller"), (worker, "worker")):
        assert f"/pixiv-remote {role}" in source
        assert "User=root" not in source
        assert "UMask=0077" in source
        assert "NoNewPrivileges=true" in source
    assert "Restart=always" in worker
    assert "KillMode=control-group" in worker


def test_configuration_examples_have_blank_secrets_and_disabled_vps_roles():
    for role in ("web", "controller", "worker"):
        source = (ROOT / f"config/remote-{role}.env.example").read_text(encoding="utf-8")
        entries = dict(line.split("=", 1) for line in source.splitlines() if line and not line.startswith("#"))
        if role != "worker":
            assert entries["PIXIV_REMOTE_ENABLED"] == "0"
        secret = {"web": "PIXIV_REMOTE_WORKER_KEY_SHA256", "controller": "INSTABOX_API_KEY",
                  "worker": "PIXIV_REMOTE_WORKER_KEY"}[role]
        assert entries[secret] == ""
        assert not ({"PIXIV_REFRESH_TOKEN", "PIXIV_FLASK_SECRET"} & entries.keys())


def test_gc_timer_is_an_offline_bounded_service_with_no_cloud_credentials():
    service = (ROOT / "deploy/systemd/pixiv-remote-gc.service").read_text(encoding="utf-8")
    timer = (ROOT / "deploy/systemd/pixiv-remote-gc.timer").read_text(encoding="utf-8")
    example = (ROOT / "config/remote-maintenance.env.example").read_text(encoding="utf-8")
    entries = dict(line.split("=", 1) for line in example.splitlines() if line and not line.startswith("#"))
    assert set(entries) == {"PIXIV_REMOTE_STATE_DIR", "PIXIV_REMOTE_MACHINE_ID"}
    assert entries["PIXIV_REMOTE_MACHINE_ID"] == ""
    for directive in (
        "Type=oneshot", "User=pixivsync", "PrivateNetwork=true",
        "EnvironmentFile=/etc/pixiv-remote/maintenance.env", "TimeoutStartSec=300",
        "ProtectSystem=strict", "ReadWritePaths=/var/lib/pixiv-remote", "UMask=0077",
        "/pixiv-remote gc --state-dir ${PIXIV_REMOTE_STATE_DIR} --confirm-state-dir ${PIXIV_REMOTE_STATE_DIR}",
    ):
        assert directive in service
    assert "[Install]" not in service
    assert "OnCalendar=hourly" in timer
    assert "Persistent=true" in timer
    assert "Unit=pixiv-remote-gc.service" in timer
    assert "WantedBy=timers.target" in timer
    for path in (ROOT / "deploy.sh", ROOT / "update.sh", ROOT / "scripts/install_server.sh"):
        assert "enable --now pixiv-remote-gc.timer" not in path.read_text(encoding="utf-8")


def test_nginx_example_separates_bootstrap_rate_limit_and_large_upload():
    source = (ROOT / "config/nginx/remote-worker-location.conf").read_text(encoding="utf-8")
    assert "http {}" in source
    assert "rate=6r/m" in source and "burst=3" in source
    assert "location = /api/remote-worker/v1/sessions" in source
    assert "client_max_body_size 33m;" in source
    assert "Retry-After 10 always;" in source
    assert "proxy_cache off;" in source


def test_nginx_bootstrap_and_worker_routes_preserve_identity_streaming():
    source = (ROOT / "config/nginx/remote-worker-location.conf").read_text(encoding="utf-8")
    for location, limit in (("= /api/remote-worker/v1/sessions", "16k"),
                            ("^~ /api/remote-worker/v1/", "33m")):
        block = source.split(f"location {location} {{", 1)[1].split("\n}", 1)[0]
        for directive in (
            "gzip off;",
            "proxy_http_version 1.1;",
            "proxy_set_header Connection '';",
            "proxy_set_header Accept-Encoding identity;",
            "proxy_request_buffering off;",
            "proxy_buffering off;",
            "proxy_cache off;",
            "proxy_set_header Host $host;",
            "proxy_set_header X-Forwarded-Proto $scheme;",
            f"client_max_body_size {limit};",
        ):
            assert directive in block, f"{location} is missing {directive}"


@pytest.mark.parametrize("location", ("= /api/remote-worker/v1/sessions", "^~ /api/remote-worker/v1/"))
def test_nginx_worker_routes_forward_only_the_verified_client_address(location):
    source = (ROOT / "config/nginx/remote-worker-location.conf").read_text(encoding="utf-8")
    block = source.split(f"location {location} {{", 1)[1].split("\n}", 1)[0]
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in block
    assert "$proxy_add_x_forwarded_for" not in block


def test_nginx_bootstrap_rate_limit_preserves_https_policy():
    source = (ROOT / "config/nginx/remote-worker-location.conf").read_text(encoding="utf-8")
    block = source.split("location @pixiv_remote_session_limited {", 1)[1].split("\n}", 1)[0]
    # A location with its own add_header directives no longer inherits server headers.
    assert 'add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;' in block
