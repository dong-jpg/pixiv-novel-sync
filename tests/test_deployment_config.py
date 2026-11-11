import os
import re
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


def test_preflight_body_limit_is_route_specific() -> None:
    import re

    text = (ROOT / "config/nginx/pixiv-novel-sync.conf").read_text(
        encoding="utf-8"
    )
    pattern = (
        r"location ~ \^/api/dashboard/ai/agents/\[0-9\]\+/preflight\$"
        r"\s*\{([^}]+)\}"
    )
    match = re.search(pattern, text)
    assert match is not None
    assert "client_max_body_size 16m;" in match.group(1)
    assert "proxy_request_buffering off;" in match.group(1)
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in match.group(1)
    assert "$proxy_add_x_forwarded_for" not in text
    without_preflight = text[: match.start()] + text[match.end() :]
    assert "client_max_body_size" not in without_preflight


def _ai_javascript_location() -> tuple[re.Pattern[str], str]:
    text = (ROOT / "config/nginx/pixiv-novel-sync.conf").read_text(encoding="utf-8")
    matches = re.findall(r"location ~ (\^/static/js/\S+)\s*\{([^}]+)\}", text)
    assert len(matches) == 1, "AI JavaScript needs one exact-name cache policy"
    expression, body = matches[0]
    return re.compile(expression), body


def test_ai_javascript_cache_policy_matches_only_six_shipped_assets() -> None:
    expression, _ = _ai_javascript_location()
    names = {
        "ai_settings.js", "ai_picker.js", "ai_provider_onboarding.js",
        "ai_preflight.js", "ai_job_diagnostics.js", "work_analysis.js",
    }
    for name in names:
        assert expression.fullmatch("/static/js/" + name)
    for path in (ROOT / "src/pixiv_novel_sync/static/js").glob("*.js"):
        assert bool(expression.fullmatch("/static/js/" + path.name)) == (path.name in names)
    for path in (
        "/static/css/tailwind.css", "/static/js/unrelated.js", "/static/js/ai_settingsXjs",
        "/static/js/ai_settings.js.map", "/static/js/ai_settings.js/extra",
        "/static/js/nested/ai_settings.js", "/other/static/js/ai_settings.js",
        "/static/js/AI_SETTINGS.js", "/api/dashboard/ai/agents/1/preflight",
    ):
        assert expression.search(path) is None


def test_ai_javascript_cache_policy_is_private_and_preserves_hsts_and_proxy_trust() -> None:
    _, body = _ai_javascript_location()
    assert 'proxy_hide_header Cache-Control;' in body
    assert 'add_header Cache-Control "private, no-store, no-cache, max-age=0, must-revalidate" always;' in body
    # A location's own add_header suppresses inherited headers on supported Nginx versions.
    assert 'add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;' in body
    for directive in (
        "proxy_pass http://127.0.0.1:5011;", "proxy_set_header Host $host;",
        "proxy_set_header X-Real-IP $remote_addr;", "proxy_set_header X-Forwarded-For $remote_addr;",
        "proxy_set_header X-Forwarded-Proto $scheme;", "proxy_cache off;",
    ):
        assert directive in body
    assert "client_max_body_size" not in body
    assert "$proxy_add_x_forwarded_for" not in body


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
