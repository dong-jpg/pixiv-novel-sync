import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_nginx_config_enables_strict_https() -> None:
    config = (ROOT / "config/nginx/pixiv-novel-sync.conf").read_text(
        encoding="utf-8"
    )

    assert config.count("server_name ${PIXIV_SERVER_NAME};") == 2
    assert "listen 80;" in config
    assert "return 301 https://${PIXIV_SERVER_NAME}$request_uri;" in config
    assert "listen 443 ssl;" in config
    assert "ssl_certificate ${PIXIV_SSL_CERTIFICATE};" in config
    assert "ssl_certificate_key ${PIXIV_SSL_CERTIFICATE_KEY};" in config
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in config
    assert "proxy_pass http://127.0.0.1:5011;" in config
    # 只替换这三个占位符，nginx 自己的 $host 不能被 envsubst 吃掉
    assert "$host" in config


def test_update_script_reports_public_https_url() -> None:
    script = (ROOT / "update.sh").read_text(encoding="utf-8")

    assert 'PUBLIC_URL="https://pixiv.dongboapp.com"' in script
    assert 'echo "访问地址: ${PUBLIC_URL}"' in script


@pytest.mark.parametrize("initial_env", [
    "example", "empty", "restored", "restored-missing",
    "restored-missing-no-final-newline",
])
def test_deploy_environment_secure_defaults_execute_in_isolation(tmp_path, initial_env):
    if initial_env != "empty":
        shutil.copyfile(ROOT / ".env.example", tmp_path / ".env.example")
    restored = "PIXIV_REFRESH_TOKEN=keep-me\nPIXIV_COOKIE_SECURE=\nDASHBOARD_TRUST_PROXY=false\n"
    if initial_env.startswith("restored"):
        if initial_env.startswith("restored-missing"):
            restored = "PIXIV_REFRESH_TOKEN=keep-me\n"
        if initial_env == "restored-missing-no-final-newline":
            restored = restored.rstrip("\n")
        (tmp_path / ".env.bak").write_text(restored, encoding="utf-8")

    # Execute only the real environment setup block, never deployment operations.
    script = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    start = script.index('if [ -f ".env.bak" ]; then')
    end = script.index("chmod 0600 .env", start) + len("chmod 0600 .env")
    bash = shutil.which("bash")
    if os.name == "nt":
        git = shutil.which("git")
        git_bash = Path(git).resolve().parents[1] / "bin" / "bash.exe" if git else None
        if git_bash is not None and git_bash.is_file():
            bash = str(git_bash)
    if bash is None:
        pytest.skip("Bash is required to execute deployment configuration")
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-e", "-c", script[start:end]],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    assert result.returncode == 0, result.stderr
    actual = (tmp_path / ".env").read_text(encoding="utf-8")
    values = dict(
        line.split("=", 1)
        for line in actual.splitlines() if "=" in line and not line.startswith("#")
    )
    if initial_env.startswith("restored"):
        assert values["PIXIV_REFRESH_TOKEN"] == "keep-me"
    if initial_env == "restored":
        assert actual == restored
    else:
        assert values["PIXIV_COOKIE_SECURE"] == "1"
        assert values["DASHBOARD_TRUST_PROXY"] == "1"
