"""设置页保存路径的回归测试。

生产事故（2026-08-28）：勾选「增量分析本地偏好」/「生成推荐」并设置 cron 后点保存，
刷新页面全部丢失。根因不在后端——`save_sync_settings` 一直正确处理这两个任务——而是
前端 `saveSettings` 发 POST 时没带 `X-CSRF-Token`：鉴权网关对变更类方法强制校验，
配置了 DASHBOARD_TOKEN 的部署一律返回 403。本机开发无 token（走 loopback 放行分支）
完全察觉不到。

更糟的是错误提示：网关的错误体是 `{"error": "csrf token invalid"}`，而前端只读
`data.message`，于是把失败也显示成「设置已保存」，用户要等刷新后才发现没存下来。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from pixiv_novel_sync.webapp import create_app


TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "pixiv_novel_sync" / "templates"
MUTATING_METHOD = re.compile(r"method:\s*['\"](POST|PUT|PATCH|DELETE)['\"]")


def _fetch_calls(source: str) -> list[tuple[int, str]]:
    """取出每个 ``fetch(`` 调用的实参文本（按括号配平截取）与所在行号。

    不能用「往后看 N 行」的窗口：同一段代码里紧邻的另一个 POST 会把 GET 误判成变更
    类请求（dashboard_preferences.html 的 recommendations/items 就是这样）。
    """
    calls: list[tuple[int, str]] = []
    for match in re.finditer(r"\bfetch\s*\(", source):
        start = match.end()
        depth = 1
        index = start
        while index < len(source) and depth:
            char = source[index]
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            index += 1
        line_number = source.count("\n", 0, match.start()) + 1
        calls.append((line_number, source[start : index - 1]))
    return calls


def _secured_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret-token")
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "sync:\n  auto_sync_enabled: true\n  auto_sync_timezone: Asia/Seoul\n",
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    env_path.write_text(
        "PIXIV_REFRESH_TOKEN=test\nDASHBOARD_TOKEN=secret-token\n", encoding="utf-8"
    )
    app = create_app(
        config_path=str(config_path), env_path=str(env_path), start_scheduler=False
    )
    client = app.test_client()
    assert client.post("/api/auth/login", data={"token": "secret-token"}).status_code == 302
    return client, config_path


def test_settings_save_without_csrf_is_rejected(tmp_path, monkeypatch):
    """没带 CSRF 头必须被拒，且错误体用的是 error 字段（不是 message）。

    前端如果只读 data.message，就会把这个 403 显示成成功——正是这次事故的表现。
    """
    client, config_path = _secured_client(tmp_path, monkeypatch)

    response = client.post("/api/dashboard/settings", json={"auto_sync_bookmarks_enabled": False})

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["error"] == "csrf token invalid"
    assert "message" not in payload
    # 配置文件没有被改写
    assert "auto_sync_bookmarks_enabled" not in yaml.safe_load(
        config_path.read_text(encoding="utf-8")
    ).get("sync", {})


def test_preference_and_recommendation_schedules_persist(tmp_path, monkeypatch):
    """带上 CSRF 后，这两个任务的开关/间隔/cron 必须真正落盘并能读回。"""
    client, config_path = _secured_client(tmp_path, monkeypatch)
    csrf_token = client.get("/api/csrf-token").get_json()["csrf_token"]

    current = client.get("/api/dashboard/settings").get_json()
    payload = dict(current)
    payload.update(
        {
            "auto_sync_preference_analyze_enabled": True,
            "auto_sync_preference_analyze_interval_hours": 2,
            "auto_sync_preference_analyze_cron": "0 4 * * *",
            "auto_sync_recommendation_run_enabled": True,
            "auto_sync_recommendation_run_interval_hours": 12,
            "auto_sync_recommendation_run_cron": "0 5 * * *",
        }
    )

    response = client.post(
        "/api/dashboard/settings", json=payload, headers={"X-CSRF-Token": csrf_token}
    )
    assert response.status_code == 200, response.get_json()

    on_disk = yaml.safe_load(config_path.read_text(encoding="utf-8"))["sync"]
    assert on_disk["auto_sync_preference_analyze_enabled"] is True
    assert on_disk["auto_sync_preference_analyze_interval_hours"] == 2
    assert on_disk["auto_sync_preference_analyze_cron"] == "0 4 * * *"
    assert on_disk["auto_sync_recommendation_run_enabled"] is True
    assert on_disk["auto_sync_recommendation_run_interval_hours"] == 12
    assert on_disk["auto_sync_recommendation_run_cron"] == "0 5 * * *"

    # 模拟刷新页面：重新 GET 必须看到同样的值
    reread = client.get("/api/dashboard/settings").get_json()
    assert reread["auto_sync_preference_analyze_enabled"] is True
    assert reread["auto_sync_preference_analyze_cron"] == "0 4 * * *"
    assert reread["auto_sync_recommendation_run_enabled"] is True
    assert reread["auto_sync_recommendation_run_cron"] == "0 5 * * *"


def test_invalid_cron_is_reported_with_a_reason(tmp_path, monkeypatch):
    """非法 cron 必须返回可读原因，且整次保存不落盘（避免半保存）。"""
    client, config_path = _secured_client(tmp_path, monkeypatch)
    csrf_token = client.get("/api/csrf-token").get_json()["csrf_token"]

    response = client.post(
        "/api/dashboard/settings",
        json={"auto_sync_bookmarks_cron": "不是 cron"},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert "auto_sync_bookmarks_cron" in payload["detail"]
    assert "auto_sync_bookmarks_cron" not in yaml.safe_load(
        config_path.read_text(encoding="utf-8")
    ).get("sync", {})


def test_templates_send_csrf_on_every_mutating_fetch() -> None:
    """模板里所有变更类 fetch 都必须走 csrfFetch（或自己带 X-CSRF-Token）。

    漏掉只会在配置了 DASHBOARD_TOKEN 的部署上炸，本地开发永远测不出来，所以用静态
    断言兜住。base.html 提供 window.csrfFetch / window.errorText 给全站复用。
    """
    offenders: list[str] = []
    for path in sorted(TEMPLATES.glob("*.html")):
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for line_number, args in _fetch_calls(source):
            if not MUTATING_METHOD.search(args):
                continue
            if "X-CSRF-Token" in args:
                continue
            offenders.append(f"{path.name}:{line_number}: {lines[line_number - 1].strip()[:90]}")
    assert not offenders, (
        "以下变更类请求没有带 CSRF，配置 DASHBOARD_TOKEN 后会一律 403：\n  "
        + "\n  ".join(offenders)
    )


def test_base_template_exposes_shared_csrf_helpers() -> None:
    html = (TEMPLATES / "base.html").read_text(encoding="utf-8")

    assert "window.csrfFetch" in html
    assert "window.ensureCsrfToken" in html
    # errorText 负责从 error / detail / message 三种字段里取原因
    assert "window.errorText" in html
    assert "'/api/csrf-token'" in html
