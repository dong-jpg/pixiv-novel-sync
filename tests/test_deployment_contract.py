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
