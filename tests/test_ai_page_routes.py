from __future__ import annotations

import re
from pathlib import Path

from pixiv_novel_sync.webapp import create_app


TEMPLATES = Path("src/pixiv_novel_sync/templates")
# 三个 AI 页面：公共层（csrfFetch / errorText / streamSSE）必须由 base.html 提供
AI_PAGES = ("dashboard_ai.html", "dashboard_wizard.html", "dashboard_ai_reader.html")

_LINE_COMMENT = re.compile(r"^[ \t]*//.*$", re.M)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
# 裸 fetch(：排除 csrfFetch( / .fetch( 这类带前缀的形态
_BARE_FETCH = re.compile(r"(?<![\w.])fetch\s*\(")
_API_URL = re.compile(r"""['"`]/api/""")
_SSE_URL = re.compile(r"/stream['\"`]")

_SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
# @click / @change / @keydown.enter / 组件 emit 的 @save @detect …，含所有修饰符
_EVENT_BINDING = re.compile(r'@([a-zA-Z][\w-]*)(?:\.[\w.]+)?\s*=\s*"([^"]*)"')
# 只认「裸标识符」「fn(...)」「name = ...」三种形态；成员调用与 $emit / $refs 不在范围内
_HANDLER_NAME = re.compile(r"^\s*([A-Za-z_][\w$]*)\s*(?:\(|=(?!=)|$)")
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")
_OPENING = "{[("
_CLOSING = "}])"


def read(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


def read_code(name: str) -> str:
    """读模板并剥掉注释，只留可执行部分。

    下面三条正向断言原本是朴素子串匹配，而 dashboard_ai.html 顶部有一段
    「本页不再自建副本（window.csrfFetch / errorText / streamSSE / aiApi）」的
    说明注释——一个新页面只要把那段注释一起抄过去，就能在完全没调用公共层的
    情况下让断言变绿。所以断言前必须先剥注释，否则守的是注释不是代码。
    """
    code = _HTML_COMMENT.sub("", read(name))
    code = _BLOCK_COMMENT.sub("", code)
    return _LINE_COMMENT.sub("", code)


def _match_bracket(text: str, start: int) -> int:
    """返回 text[start] 处括号的配对下标，跳过字符串字面量内的括号。"""
    assert text[start] in _OPENING, text[start : start + 20]
    depth = 0
    quote = None
    i = start
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch in _OPENING:
            depth += 1
        elif ch in _CLOSING:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise AssertionError("括号未闭合")


def _split_top_level(body: str) -> list[str]:
    """按顶层逗号切分对象字面量的内容。"""
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    quote = None
    i = 0
    while i < len(body):
        ch = body[i]
        if quote:
            cur.append(ch)
            if ch == "\\" and i + 1 < len(body):
                cur.append(body[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
            cur.append(ch)
        elif ch in _OPENING:
            depth += 1
            cur.append(ch)
        elif ch in _CLOSING:
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def _object_keys(text: str, brace: int) -> set[str]:
    """取对象字面量的键名（含 { a, b } 简写形式）。"""
    keys = set()
    for part in _split_top_level(text[brace + 1 : _match_bracket(text, brace)]):
        part = part.strip()
        if not part or part.startswith("..."):
            continue
        key = part.split(":", 1)[0].strip()
        if _IDENTIFIER.fullmatch(key):
            keys.add(key)
    return keys


def _top_level_returns(body: str) -> list[int]:
    """定位 setup() 函数体顶层的 return，忽略嵌套函数里的 return。"""
    out: list[int] = []
    depth = 0
    quote = None
    i = 0
    while i < len(body):
        ch = body[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch in _OPENING:
            depth += 1
        elif ch in _CLOSING:
            depth -= 1
        elif depth == 0 and body.startswith("return", i):
            before = body[i - 1] if i else " "
            after = body[i + 6] if i + 6 < len(body) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                out.append(i + 6)
                i += 6
                continue
        i += 1
    return out


def setup_scope(html: str) -> set[str]:
    """setup() 实际交给模板的渲染作用域。

    base.html 的 window.initVueApp 只做 createApp(setupFunc) + mount，没有任何兜底，
    所以 setup() 的返回值就是模板能解析的全部名字。
    """
    brace = html.index("{", html.index("setup()"))
    body = html[brace + 1 : _match_bracket(html, brace)]
    returns = _top_level_returns(body)
    assert returns, "找不到 setup() 的顶层 return"

    names: set[str] = set()
    for pos in returns:
        rest = body[pos:].lstrip()
        if rest.startswith("{"):
            # return { a, b, c }
            names |= _object_keys(body, body.index("{", pos))
            continue
        # return exported —— 键分散在 const 声明和若干 Object.assign 里
        ident = _IDENTIFIER.match(rest)
        assert ident, f"无法解析 return 表达式：{rest[:40]!r}"
        alias = re.escape(ident.group(0))
        decl = re.search(rf"\b(?:const|let|var)\s+{alias}\s*=\s*\{{", body)
        assert decl, f"找不到 {ident.group(0)} 的对象字面量声明"
        names |= _object_keys(body, decl.end() - 1)
        for assign in re.finditer(rf"Object\.assign\(\s*{alias}\s*,\s*\{{", body):
            names |= _object_keys(body, assign.end() - 1)
    return names


def test_every_event_handler_is_exported_from_setup():
    """回归：事件绑到 setup() 没返回的名字上，按钮会静默失效或抛 TypeError。

    实际踩过两次：伏笔 tab 的「AI自动回收」（autoResolveForeshadows）和 Pipeline 弹窗的
    单步按钮（runSingleStep）都定义了却没进导出对象，点了没反应。组件 emit（@save /
    @detect）走同一套解析，漏导出同样是死绑定。
    """
    for name in AI_PAGES:
        html = read(name)
        scope = setup_scope(html)
        markup = _SCRIPT_BLOCK.sub("", html)

        handlers = set()
        for _event, expr in _EVENT_BINDING.findall(markup):
            matched = _HANDLER_NAME.match(expr)
            if matched:
                handlers.add(matched.group(1))

        assert handlers, f"{name} 没解析出任何事件处理器，正则该更新了"
        missing = sorted(handlers - scope)
        assert not missing, f"{name} 的事件绑定指向了 setup() 未返回的名字：{missing}"


def test_base_provides_shared_frontend_helpers():
    html = read("base.html")

    assert "window.csrfFetch = async function" in html
    assert "window.errorText = function" in html
    assert "window.streamSSE = async function" in html
    assert "window.aiApi = {" in html


def test_ai_pages_do_not_redefine_csrf_helpers():
    """回归：三个模板各自重写 csrfFetch，base.html 的全站版本形同虚设。"""
    for name in AI_PAGES:
        code = read_code(name)
        assert "async function csrfFetch" not in code, name
        assert "function ensureCsrfToken" not in code, name
        assert "'/api/csrf-token'" not in code, name


def test_ai_pages_route_every_request_through_shared_helpers():
    """页面不许绕过助手直接发请求，否则生产环境 CSRF 门会把它变成 403。

    正向部分是**条件式**的：拆页后不是每个页面都会直接调 window.csrfFetch
    （只用 window.aiApi / streamSSE 的页面同样合规，助手内部才调它）。所以
    判据是「这个页面若引用了 /api/ URL，就必须经由某个公共层助手」，而不是
    要求每页都出现某个固定字面量——后者会逼着新页面硬塞一次无用调用。
    """
    for name in AI_PAGES:
        code = read_code(name)
        assert not _BARE_FETCH.search(code), f"{name} 出现裸 fetch("
        if _API_URL.search(code):
            assert any(
                helper in code
                for helper in ("window.aiApi", "window.csrfFetch", "window.streamSSE")
            ), f"{name} 引用了 /api/ 却没走任何公共层助手"


def test_ai_pages_use_shared_error_text():
    """错误体有 error / detail 两种字段，只读 error 会把失败显示成「请求失败」。

    条件式：window.aiApi.request 内部已经走 errorText，所以只有**直接**调
    window.csrfFetch（绕过 api()）的页面才必须自己用 errorText。
    """
    for name in AI_PAGES:
        code = read_code(name)
        if "window.csrfFetch" in code:
            assert "window.errorText" in code, f"{name} 直接调 csrfFetch 却没用 errorText"


def test_ai_pages_do_not_hand_roll_sse_parsing():
    """条件式：没有流式端点的页面不该被迫出现 streamSSE 字面量。"""
    for name in AI_PAGES:
        code = read_code(name)
        assert "getReader()" not in code, name
        if _SSE_URL.search(code):
            assert "window.streamSSE" in code, f"{name} 有 /stream 端点却自行解析"


def test_shared_helper_assertions_ignore_comments():
    """守护上面三条断言的守护者：确认 read_code 真的剥掉了注释。

    没有这条，read_code 哪天退化成 read() 也不会有人发现，而那正是
    「抄注释即可假通过」这个漏洞的复发路径。
    """
    marker = "window.streamSSE"
    ai_raw = read("dashboard_ai.html")
    ai_code = read_code("dashboard_ai.html")

    # dashboard_ai.html 顶部确实有一段提到公共层名字的注释
    assert ai_raw.count(marker) > ai_code.count(marker), (
        "dashboard_ai.html 的公共层注释没有被剥掉，三条正向断言可被注释欺骗"
    )
    assert _LINE_COMMENT.sub("", "  // window.csrfFetch\nreal();") == "\nreal();"
    assert _HTML_COMMENT.sub("", "<!-- window.errorText -->x") == "x"


def test_ai_and_wizard_share_one_profile_loader():
    """agents / 风格档案 / 小说档案 / 偏好画像的加载只允许有一份实现。"""
    for name in ("dashboard_ai.html", "dashboard_wizard.html"):
        html = read(name)
        assert "window.aiApi" in html, name
        assert "'/api/dashboard/ai/style-profiles'" not in html, name
        assert "'/api/dashboard/ai/novel-profiles'" not in html, name
        assert "'/api/dashboard/preferences/profiles'" not in html, name


def test_ai_and_wizard_routes_render_distinct_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("PIXIV_FLASK_SECRET", "ai-page-route-test-secret")
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "storage:\n"
        f"  public_dir: {(tmp_path / 'public').as_posix()}\n"
        f"  private_dir: {(tmp_path / 'private').as_posix()}\n"
        f"  db_path: {(tmp_path / 'routes.db').as_posix()}\n"
        "sync:\n"
        "  auto_sync_enabled: false\n",
        encoding="utf-8",
    )
    client = create_app(config_path=str(config_path), env_path=str(env_path), start_scheduler=False).test_client()

    ai = client.get("/dashboard/ai", environ_base={"REMOTE_ADDR": "127.0.0.1"})
    wizard = client.get("/dashboard/wizard", environ_base={"REMOTE_ADDR": "127.0.0.1"})

    ai_html = ai.get_data(as_text=True)
    wizard_html = wizard.get_data(as_text=True)
    assert 'data-page="ai-writing"' in ai_html
    assert 'data-page="writing-wizard"' not in ai_html
    assert 'data-page="writing-wizard"' in wizard_html
    assert 'data-page="ai-writing"' not in wizard_html
