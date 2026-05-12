from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

PIXIV_CALLBACK_HOST = "app-api.pixiv.net"
CAPTCHA_SELECTORS = [
    "iframe[src*='captcha']",
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "div.g-recaptcha",
    "#captcha",
    "[class*='captcha']",
]


@dataclass(slots=True)
class LoginResult:
    success: bool
    callback_url: str | None = None
    error: str | None = None
    screenshot_path: str | None = None


class PlaywrightLoginHelper:
    """用 Playwright 无头浏览器自动完成 Pixiv OAuth 登录。"""

    def __init__(self, proxy: str | None = None, timeout: int = 30) -> None:
        self.proxy = proxy
        self.timeout = timeout * 1000  # Playwright 使用毫秒

    def login(self, login_url: str, username: str, password: str) -> LoginResult:
        """自动登录 Pixiv OAuth，返回包含 code 和 state 的回调 URL。"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return LoginResult(success=False, error="playwright 未安装，请运行: pip install playwright && playwright install chromium")

        with sync_playwright() as p:
            return self._do_login(p, login_url, username, password)

    def _do_login(self, p: Any, login_url: str, username: str, password: str) -> LoginResult:
        browser = None
        try:
            launch_args: dict[str, Any] = {
                "headless": True,
                "args": [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            }
            if self.proxy:
                launch_args["proxy"] = {"server": self.proxy}

            browser = p.chromium.launch(**launch_args)
            context = browser.new_context(
                user_agent="PixivAndroidApp/5.0.234 (Android 11; Pixel 5)",
                viewport={"width": 414, "height": 896},
                locale="zh-CN",
            )
            page = context.new_page()

            # 监听所有 navigation，捕获回调 URL
            callback_url_holder: list[str] = []

            def _on_frame_navigated(frame: Any) -> None:
                url = frame.url
                if PIXIV_CALLBACK_HOST in url and "code=" in url:
                    callback_url_holder.append(url)

            page.on("framenavigated", _on_frame_navigated)

            # 同时监听 response 以捕获重定向
            def _on_response(response: Any) -> None:
                url = response.url
                if PIXIV_CALLBACK_HOST in url and "code=" in url:
                    callback_url_holder.append(url)

            page.on("response", _on_response)

            # 导航到 Pixiv OAuth 登录页
            logger.info("正在打开 Pixiv 登录页: %s", login_url[:80])
            page.goto(login_url, wait_until="networkidle", timeout=self.timeout)

            # 检查是否已经跳转到回调页（可能有缓存的登录态）
            if self._is_pixiv_callback_url(page.url):
                return LoginResult(success=True, callback_url=page.url)

            # 等待登录表单加载
            logger.info("等待登录表单加载...")
            username_selector = 'input[autocomplete="username"], input[type="email"], input[name="email"]'
            password_selector = 'input[autocomplete="current-password"], input[type="password"]'

            try:
                page.wait_for_selector(username_selector, timeout=self.timeout)
            except Exception:
                # 尝试其他选择器
                try:
                    page.wait_for_selector('input[name="pixiv_id"]', timeout=5000)
                    username_selector = 'input[name="pixiv_id"]'
                except Exception:
                    screenshot_path = self._save_screenshot(page, "login_form_not_found")
                    return LoginResult(
                        success=False,
                        error="无法找到登录表单，Pixiv 页面结构可能已变化",
                        screenshot_path=screenshot_path,
                    )

            # 填写用户名
            logger.info("填写用户名...")
            page.fill(username_selector, username)

            # 填写密码
            logger.info("填写密码...")
            try:
                page.wait_for_selector(password_selector, timeout=5000)
            except Exception:
                try:
                    page.wait_for_selector('input[name="password"]', timeout=5000)
                    password_selector = 'input[name="password"]'
                except Exception:
                    screenshot_path = self._save_screenshot(page, "password_field_not_found")
                    return LoginResult(
                        success=False,
                        error="无法找到密码输入框",
                        screenshot_path=screenshot_path,
                    )

            page.fill(password_selector, password)

            # 检查 CAPTCHA
            if self._detect_captcha(page):
                screenshot_path = self._save_screenshot(page, "captcha_detected")
                return LoginResult(
                    success=False,
                    error="检测到验证码(CAPTCHA)，请稍后重试或手动登录",
                    screenshot_path=screenshot_path,
                )

            # 点击登录按钮
            logger.info("点击登录按钮...")
            submit_selectors = [
                'button[type="submit"]',
                'button:has-text("登录")',
                'button:has-text("Login")',
                'button:has-text("ログイン")',
                'input[type="submit"]',
            ]
            clicked = False
            for sel in submit_selectors:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                # 尝试按 Enter 键
                page.keyboard.press("Enter")

            # 等待导航完成（跳转到回调 URL）
            logger.info("等待 Pixiv 跳转到回调 URL...")
            callback_url = self._wait_for_callback(page, callback_url_holder, timeout=self.timeout)

            if callback_url:
                logger.info("成功获取回调 URL")
                return LoginResult(success=True, callback_url=callback_url)

            # 没有捕获到回调 URL，检查当前页面状态
            current_url = page.url
            if self._is_pixiv_callback_url(current_url):
                return LoginResult(success=True, callback_url=current_url)

            # 检查是否有错误信息
            error_msg = self._extract_error_message(page)
            if error_msg:
                screenshot_path = self._save_screenshot(page, "login_error")
                return LoginResult(success=False, error=f"登录失败: {error_msg}", screenshot_path=screenshot_path)

            # 检查是否需要二次验证
            screenshot_path = self._save_screenshot(page, "login_timeout")
            return LoginResult(
                success=False,
                error="登录超时，可能需要二次验证或账号密码错误",
                screenshot_path=screenshot_path,
            )

        except Exception as exc:
            logger.exception("Playwright 登录异常")
            return LoginResult(success=False, error=f"登录异常: {exc}")
        finally:
            if browser:
                browser.close()

    def _wait_for_callback(self, page: Any, callback_holder: list[str], timeout: int) -> str | None:
        """等待回调 URL 出现。"""
        deadline = time.time() + timeout / 1000
        check_interval = 0.5

        while time.time() < deadline:
            # 检查是否已经通过事件捕获到
            if callback_holder:
                return callback_holder[0]

            # 检查当前页面 URL
            try:
                current_url = page.url
                if self._is_pixiv_callback_url(current_url):
                    return current_url
            except Exception:
                pass

            time.sleep(check_interval)

        # 最后检查一次
        if callback_holder:
            return callback_holder[0]
        try:
            if self._is_pixiv_callback_url(page.url):
                return page.url
        except Exception:
            pass

        return None

    def _is_pixiv_callback_url(self, url: str) -> bool:
        """检查 URL 是否是 Pixiv 回调地址。"""
        if not url:
            return False
        parsed = urlparse(url)
        return PIXIV_CALLBACK_HOST in parsed.hostname and "code=" in url

    def _detect_captcha(self, page: Any) -> bool:
        """检测页面是否有 CAPTCHA。"""
        for selector in CAPTCHA_SELECTORS:
            try:
                if page.query_selector(selector):
                    return True
            except Exception:
                continue
        return False

    def _extract_error_message(self, page: Any) -> str | None:
        """从页面提取错误信息。"""
        error_selectors = [
            ".error-message",
            ".alert-error",
            "[class*='error']",
            "[class*='Error']",
            "p[class*='error']",
        ]
        for selector in error_selectors:
            try:
                el = page.query_selector(selector)
                if el and el.is_visible():
                    text = el.inner_text().strip()
                    if text and len(text) < 200:
                        return text
            except Exception:
                continue
        return None

    def _save_screenshot(self, page: Any, name: str) -> str | None:
        """保存页面截图。"""
        try:
            from pathlib import Path
            screenshot_dir = Path("data/screenshots")
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            path = screenshot_dir / f"{name}_{ts}.png"
            page.screenshot(path=str(path))
            return str(path)
        except Exception:
            return None
