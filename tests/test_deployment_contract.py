"""部署契约一致性测试。

deploy.sh 是 Web 部署唯一入口，自行内联生成 systemd unit；
deploy/systemd/pixiv-novel-sync.service 仅由 scripts/install_server.sh
（legacy timer 同步）安装使用。两者必须约定一致的运行用户与安装路径。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE_FILE = ROOT / "deploy" / "systemd" / "pixiv-novel-sync.service"
INSTALL_SCRIPT = ROOT / "scripts" / "install_server.sh"


def _service_fields() -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in SERVICE_FILE.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("["):
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip()
    return fields


def test_service_user_matches_install_script_user() -> None:
    script = INSTALL_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"useradd[^\n]*\s(\S+)\s*(?:2>|$)", script)
    assert match, "install_server.sh 应包含 useradd"
    script_user = match.group(1)
    fields = _service_fields()
    assert fields["User"] == script_user == "pixivsync"


def test_service_paths_match_install_script_app_dir() -> None:
    script = INSTALL_SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^APP_DIR=(\S+)", script, re.MULTILINE)
    assert match, "install_server.sh 应定义 APP_DIR"
    app_dir = match.group(1)
    fields = _service_fields()
    assert fields["WorkingDirectory"] == app_dir
    assert fields["EnvironmentFile"].startswith(app_dir + "/")
    assert fields["ExecStart"].startswith(app_dir + "/")
    assert app_dir + "/config/config.yaml" in fields["ExecStart"]


def test_install_script_puts_code_in_app_dir_and_chowns_service_user() -> None:
    script = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert 'cd "$APP_DIR"' in script
    assert "chown -R pixivsync:pixivsync" in script


def test_web_deploy_backup_path_nginx_render_and_unit_path() -> None:
    deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    update = (ROOT / "update.sh").read_text(encoding="utf-8")

    assert "umask 077" in update
    assert 'BACKUP_DIR="${INSTALL_DIR}/.backup/' in update
    assert 'rm -rf "$BACKUP_DIR"' in update
    assert "/tmp/pixiv-novel-sync-backup" not in update

    for script in (deploy, update):
        assert "/usr/local/bin:/usr/bin:/bin" in script
        assert "nginx -t &&" not in script
        assert re.search(r"^sudo nginx -t\s*$", script, re.MULTILINE)
        assert "envsubst" in script
        assert "PIXIV_SERVER_NAME:-pixiv.dongboapp.com" in script

    apt_line = next(line for line in deploy.splitlines() if "apt install" in line)
    assert "acl" in apt_line.split()
    assert "playwright install chromium" in deploy
