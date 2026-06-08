from __future__ import annotations

import logging
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
        hostname = (parsed.hostname or "").lower()
        if hostname != PIXIV_CALLBACK_HOST:
            return False
        return bool(parse_qs(parsed.query).get("code"))

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

    def login_web(self, username: str, password: str) -> dict[str, Any]:
        """普通网页登录 pixiv.net，获取 Web Cookie（PHPSESSID 等）。

        使用 Firefox headed 模式提高自动登录成功率。
        在无显示器的服务器上自动启动 Xvfb 虚拟显示器。

        Returns:
            {"success": True, "cookie_string": "k1=v1; k2=v2; ..."}
            或 {"success": False, "error": "..."}
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return {"success": False, "error": "playwright 未安装"}

        # Web 登录需要更长超时（页面加载慢）
        original_timeout = self.timeout
        self.timeout = max(self.timeout, 60000)  # 至少 60 秒

        xvfb_proc = None
        try:
            # 在无 DISPLAY 的 Linux 服务器上自动启动 Xvfb 虚拟显示器
            xvfb_proc = self._ensure_display()

            with sync_playwright() as p:
                return self._do_login_web(p, username, password)
        finally:
            self.timeout = original_timeout
            if xvfb_proc:
                xvfb_proc.terminate()
                xvfb_proc.wait()

    def _ensure_display(self) -> Any:
        """确保有可用的 DISPLAY 环境变量（Linux 服务器需要 Xvfb）。

        Returns:
            Xvfb subprocess 对象（需要调用方 terminate），或 None（已有 DISPLAY）。
        """
        import os
        import sys

        # Windows / macOS 或已有 DISPLAY 时无需处理
        if sys.platform != "linux" or os.environ.get("DISPLAY"):
            return None

        import subprocess
        import random

        # 选择一个随机显示号避免冲突
        display_num = random.randint(99, 999)
        display = f":{display_num}"

        try:
            proc = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # 等待 Xvfb 启动
            time.sleep(1)
            if proc.poll() is not None:
                # Xvfb 启动失败，尝试另一个显示号
                display_num = random.randint(1000, 1999)
                display = f":{display_num}"
                proc = subprocess.Popen(
                    ["Xvfb", display, "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                time.sleep(1)

            os.environ["DISPLAY"] = display
            logger.info("已启动 Xvfb 虚拟显示器: %s", display)
            return proc
        except FileNotFoundError:
            logger.warning("Xvfb 未安装，尝试 headless 模式（可能无法通过 reCAPTCHA）")
            return None

    def _do_login_web(self, p: Any, username: str, password: str) -> dict[str, Any]:
        """使用 Firefox headed 模式登录 Pixiv（需要 Xvfb 虚拟显示器）。

        Firefox headed 模式更接近常规浏览器登录流程，
        在 Linux 服务器上通过 xvfb-run 提供虚拟显示器。
        """
        browser = None
        try:
            import os
            # 如果有 DISPLAY 环境变量，使用 headed 模式（更容易通过 reCAPTCHA）
            has_display = bool(os.environ.get("DISPLAY"))
            use_headless = not has_display

            launch_args: dict[str, Any] = {"headless": use_headless}
            if self.proxy:
                launch_args["proxy"] = {"server": self.proxy}

            if use_headless:
                logger.warning("无虚拟显示器，使用 headless 模式（可能无法通过 reCAPTCHA）")

            browser = p.firefox.launch(**launch_args)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
                viewport={"width": 1280, "height": 800},
                locale="ja-JP",
                timezone_id="Asia/Tokyo",
            )
            page = context.new_page()

            # 导航到 Pixiv 登录页
            logger.info("Web Cookie 刷新: 打开 Pixiv 登录页 (Firefox headed)...")
            page.goto("https://accounts.pixiv.net/login", wait_until="networkidle", timeout=self.timeout)
            time.sleep(3)

            # 查找用户名输入框（Pixiv 使用多种 autocomplete 属性）
            username_selector = None
            for sel in [
                'input[autocomplete="username webauthn"]',
                'input[autocomplete="username"]',
                'input[type="text"]:visible',
            ]:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        username_selector = sel
                        break
                except Exception:
                    continue

            if not username_selector:
                self._save_screenshot(page, "web_login_form_not_found")
                return {"success": False, "error": "无法找到登录表单用户名输入框"}

            # 模拟人类输入
            logger.info("Web Cookie 刷新: 填写登录信息...")
            page.click(username_selector)
            time.sleep(0.5)
            page.type(username_selector, username, delay=80)
            time.sleep(0.8)

            # 查找密码输入框
            password_selector = None
            for sel in [
                'input[autocomplete="current-password webauthn"]',
                'input[autocomplete="current-password"]',
                'input[type="password"]:visible',
            ]:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        password_selector = sel
                        break
                except Exception:
                    continue

            if not password_selector:
                self._save_screenshot(page, "web_login_password_not_found")
                return {"success": False, "error": "无法找到密码输入框"}

            page.click(password_selector)
            time.sleep(0.3)
            page.type(password_selector, password, delay=50)
            time.sleep(2)

            # 等待 reCAPTCHA 评分完成
            time.sleep(3)

            # 点击登录按钮（精确匹配 "ログイン" 文本的 submit 按钮）
            login_btn = page.locator('button[type="submit"]:has-text("ログイン")')
            btn_count = login_btn.count()
            if btn_count > 0:
                login_btn.last.click()
                logger.info("Web Cookie 刷新: 已点击登录按钮")
            else:
                # 回退：尝试其他提交方式
                for sel in ['button:has-text("Login")', 'button:has-text("登录")', 'input[type="submit"]']:
                    try:
                        btn = page.query_selector(sel)
                        if btn and btn.is_visible():
                            btn.click()
                            break
                    except Exception:
                        continue
                else:
                    page.keyboard.press("Enter")

            # 等待登录完成
            logger.info("Web Cookie 刷新: 等待登录完成...")
            try:
                page.wait_for_url("**/pixiv.net/**", timeout=30000)
            except Exception:
                pass
            # 额外等待确保 cookie 完全设置
            time.sleep(5)

            # 检查是否登录成功
            cookies = context.cookies("https://www.pixiv.net")
            cookie_dict = {c["name"]: c["value"] for c in cookies}

            if "PHPSESSID" not in cookie_dict:
                error_msg = self._extract_error_message(page)
                self._save_screenshot(page, "web_login_failed")
                return {"success": False, "error": error_msg or "登录失败，未获取到 PHPSESSID"}

            # 构建 cookie 字符串
            cookie_string = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            logger.info("Web Cookie 刷新成功，获取到 %d 个 cookie", len(cookies))
            return {"success": True, "cookie_string": cookie_string}

        except Exception as exc:
            logger.exception("Web Cookie 刷新异常")
            return {"success": False, "error": f"登录异常: {exc}"}
        finally:
            if browser:
                browser.close()

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
