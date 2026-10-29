"""Tailwind 静态 CSS 的回归测试。

base.html 原先加载 Tailwind Play CDN：浏览器里运行时 JIT，页面用到什么类就现场生成什么样式。
现在改为引用提交进仓库的 static/css/tailwind.css（scripts/build_tailwind_css.py 生成）。
模板里新增了 CSS 里没有的工具类，页面不会报错，只会悄悄失去样式，下面的用例负责拦住这种遗漏。
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "pixiv_novel_sync"
TEMPLATES = PACKAGE / "templates"
CSS = PACKAGE / "static" / "css" / "tailwind.css"
REBUILD = "python scripts/build_tailwind_css.py"


def _load_build_script():
    spec = importlib.util.spec_from_file_location("build_tailwind_css", ROOT / "scripts" / "build_tailwind_css.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _load_build_script()

# Tailwind v3 工具类的首段（去掉变体、! 与负号后，第一个 - 之前的部分）。
# 不在表里的（library-btn、reader-tool、fs-small……）当作项目自定义类，不检查。
TAILWIND_ROOTS = frozenset("""
    container columns break box block inline flex grid table contents flow list hidden
    static fixed absolute relative sticky inset top right bottom left start end
    visible invisible collapse isolate isolation z order col row auto gap
    grow shrink basis justify content items self place float clear object overflow overscroll
    p px py pt pr pb pl ps pe m mx my mt mr mb ml ms me space w min max h size aspect
    font text antialiased subpixel italic not normal tracking leading line underline overline
    no decoration uppercase lowercase capitalize truncate indent align whitespace hyphens
    tabular ordinal slashed lining oldstyle proportional diagonal stacked placeholder
    bg from via to rounded border divide outline ring shadow opacity mix blur brightness
    contrast drop grayscale hue invert saturate sepia backdrop filter transition duration
    ease delay animate transform origin scale rotate translate skew accent appearance caret
    cursor pointer resize scroll snap touch select will fill stroke sr forced
""".split())

# 长得像工具类、其实不是的词：词 → 原因。新加时写清楚，免得把真缺的类藏起来。
NOT_TAILWIND: dict[str, str] = {}

# Jinja {[ ]} / {% %}、Vue {{ }}、模板字符串 ${}；只按单行匹配，免得吞掉整段脚本
INTERPOLATION = re.compile(r"\{\[[^\n]*?\]\}|\{\{[^\n]*?\}\}|\{%[^\n]*?%\}|\$\{[^}\n]*\}")
STRING_LITERAL = re.compile(r"'([^'\\\n]*)'|\"([^\"\\\n]*)\"")
STATIC_CLASS = re.compile(r"(?<![\w:.-])class=(?:\"([^\"]*)\"|'([^']*)')")
BOUND_CLASS = re.compile(r"(?:(?<![\w-]):|v-bind:)class=\"([^\"]*)\"")
OBJECT_KEY = re.compile(r"[{,]\s*([A-Za-z_][\w-]*)\s*:")
SCRIPT = re.compile(r"<script[^>]*>(.*?)</script>", re.S)
CLASS_SINK = re.compile(r"(?:className\s*=|classList\.(?:add|remove|toggle|replace)\s*\()([^;\n]*)")
# 类名的形状：方括号外只有小写字母、数字与 - : / . % !；引号、逗号、下划线只能出现在 [...] 任意值里
CLASS_TOKEN = re.compile(r"[!-]?(?:\[[^\]\s]+\]|[a-z0-9])(?:\[[^\]\s]+\]|[a-z0-9:/.%!-])*")
# Tailwind 刻度后缀：-300 / -2.5 / -[13px] / -lg / -2xl / -full / -bold / -white/70 ……
UTILITY_SUFFIX = re.compile(
    r"-(?:\d+(?:\.\d+)?|\[[^\]]+\]|\d?xs|sm|md|lg|\d?xl|full|screen|auto|none|px"
    r"|thin|extralight|light|normal|medium|semibold|bold|extrabold|black|white|transparent)(?:/\d+)?$"
)


@lru_cache(maxsize=None)
def generated_classes() -> frozenset[str]:
    return frozenset(build.class_names(CSS.read_bytes()))


@lru_cache(maxsize=None)
def custom_classes() -> frozenset[str]:
    """模板自带 <style> 里定义的类（library-*、reader-* 等），它们不归 Tailwind 管。"""
    names: set[str] = set()
    for path in TEMPLATES.glob("*.html"):
        for block in re.findall(r"<style[^>]*>(.*?)</style>", path.read_text(encoding="utf-8"), re.S):
            names |= build.class_names(block.encode("utf-8"))
    return frozenset(names)


def _literals(expr: str) -> list[str]:
    return [a or b for a, b in STRING_LITERAL.findall(expr)]


def _class_list(value: str) -> list[str]:
    """class 属性值：插值里的字符串字面量也算（Jinja 三元式拼的类），插值本身丢掉。"""
    tokens: list[str] = []
    for expr in INTERPOLATION.findall(value):
        for literal in _literals(expr):
            tokens += literal.split()
    return tokens + INTERPOLATION.sub(" ", value).split()


def _bound_class_tokens(expr: str) -> list[str]:
    """:class 表达式：字符串字面量里的类，加上对象写法里没加引号的键（{ hidden: !open }）。"""
    tokens: list[str] = []
    for literal in _literals(expr):
        tokens += literal.split()
    return tokens + OBJECT_KEY.findall(STRING_LITERAL.sub("''", expr))


def _script_tokens(script: str) -> list[str]:
    """脚本里的类：className / classList 的参数；整串都像类名的多词字符串（徽章颜色映射表）；
    以及带刻度后缀的单词串（dotClass: 'bg-slate-400'、日志级别 warning: 'text-amber-300'）。
    单词串只认刻度后缀，'no-store'、'flex-start'、'overflow-y' 这类非类名字符串不会被误判。"""
    tokens: list[str] = []
    for match in CLASS_SINK.finditer(script):
        for literal in _literals(match.group(1)):
            tokens += literal.split()
    for literal in _literals(INTERPOLATION.sub(" ", script)):
        words = literal.split()
        if not words or not all(CLASS_TOKEN.fullmatch(word) for word in words):
            continue
        if len(words) > 1 or UTILITY_SUFFIX.search(words[0]):
            tokens += words
    return tokens


def template_class_tokens(text: str) -> set[str]:
    tokens: list[str] = []
    for double, single in STATIC_CLASS.findall(text):
        tokens += _class_list(double or single)
    for expr in BOUND_CLASS.findall(text):
        tokens += _bound_class_tokens(expr)
    for script in SCRIPT.findall(text):
        tokens += _script_tokens(script)
    return set(tokens)


def strip_variants(token: str) -> str:
    """去掉 sm: / hover: / group-hover: 等变体前缀；方括号里的冒号（[@media(...)]:）不算分隔符。"""
    depth, start = 0, 0
    for index, char in enumerate(token):
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        elif char == ":" and depth == 0:
            start = index + 1
    return token[start:]


def looks_like_tailwind(token: str) -> bool:
    if not CLASS_TOKEN.fullmatch(token) or token in custom_classes() or token in NOT_TAILWIND:
        return False
    utility = strip_variants(token).lstrip("!").lstrip("-")
    if utility.startswith("["):  # 任意属性写法 [mask-type:luminance]
        return True
    root = re.match(r"[a-z0-9]+", utility)
    return bool(root) and root.group(0) in TAILWIND_ROOTS


def css_version() -> str:
    """与 scripts/build_tailwind_css.py 的 css_version 同一算法。"""
    return hashlib.sha256(CSS.read_bytes().replace(b"\r\n", b"\n")).hexdigest()[:12]


def test_base_loads_committed_css_instead_of_play_cdn():
    html = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "cdn.tailwindcss.com" not in html
    assert "tailwind.config =" not in html, "theme 已迁到 scripts/tailwind.config.js"
    links = re.findall(r'<link rel="stylesheet" href="/static/css/tailwind\.css\?v=([0-9a-f]{12})">', html)
    assert links == [css_version()], f"base.html 的 ?v= 与 tailwind.css 内容不符，运行 {REBUILD}"


def test_tailwind_css_is_the_last_stylesheet_in_head():
    """Play CDN 把生成的 <style> 追加在 head 末尾，同权重时工具类压过 library-* 与页面 extra_head 规则。"""
    head = (TEMPLATES / "base.html").read_text(encoding="utf-8").split("</head>", 1)[0]
    tail = head.rsplit("{% block extra_head %}{% endblock %}", 1)[1]
    tail = re.sub(r"\{#.*?#\}", "", tail, flags=re.S)
    assert "/static/css/tailwind.css" in tail
    assert "<style" not in tail and "<script" not in tail


def test_every_template_tailwind_class_is_in_generated_css():
    missing: dict[str, list[str]] = {}
    for path in sorted(TEMPLATES.glob("*.html")):
        tokens = template_class_tokens(path.read_text(encoding="utf-8"))
        absent = sorted(token for token in tokens if looks_like_tailwind(token) and token not in generated_classes())
        if absent:
            missing[path.name] = absent
    assert not missing, (
        f"模板里的这些 Tailwind 类不在 tailwind.css 里，运行 {REBUILD} 重生成；"
        f"若确实不是 Tailwind 类，写进本文件的 NOT_TAILWIND 并注明原因：{missing}"
    )


def test_class_scanner_sees_every_kind_of_class_context():
    """扫描器自身失灵（正则写坏）时，上面的覆盖测试会空跑通过，这里拿合成模板兜底。"""
    sample = """<div class="mt-[1234px] {[ 'text-[77px]' if x else '' ]}" :class="{ 'bg-[#123456]': on, hidden: off }"></div>
<script>
el.className = 'px-[33px] ' + extra; el.classList.add('ring-[7px]');
const badges = { a: 'rounded-[9px] shadow-[0_0_1px_red]' }, dot = { b: 'bg-slate-[333]' };
headers = { cache: 'no-store', Accept: 'text/event-stream' }; el.style.setProperty('overflow-y', 'hidden');
</script>"""
    flagged = {t for t in template_class_tokens(sample) if looks_like_tailwind(t) and t not in generated_classes()}
    assert flagged == {"mt-[1234px]", "text-[77px]", "bg-[#123456]", "px-[33px]", "ring-[7px]",
                       "rounded-[9px]", "shadow-[0_0_1px_red]", "bg-slate-[333]"}
    real = set().union(*(template_class_tokens(p.read_text(encoding="utf-8")) for p in TEMPLATES.glob("*.html")))
    assert {"sm:grid-cols-2", "!p-3", "z-[80]", "group-hover:text-brand-600", "text-amber-300"} <= real


DYNAMIC_ROOT = "|".join(sorted(TAILWIND_ROOTS, key=len, reverse=True))
DYNAMIC_CLASS = re.compile(
    rf"['\"](?:[a-z0-9-]+:)*(?:{DYNAMIC_ROOT})-[a-z0-9-]*['\"]\s*\+"  # 'bg-' + color
    rf"|(?<![\w-])(?:{DYNAMIC_ROOT})-[a-z0-9-]*\$\{{"  # `text-${size}`
    rf"|(?<![\w-])(?:{DYNAMIC_ROOT})-[a-z0-9-]*\{{\["  # text-{[ color ]}
)


def test_templates_do_not_assemble_tailwind_class_names_at_runtime():
    """CLI 只扫源码文本：运行时拼出来的类名看不见，得写完整字面量，或登记到 scripts/tailwind.config.js 的 safelist。"""
    found = {
        path.name: sorted(set(DYNAMIC_CLASS.findall(path.read_text(encoding="utf-8"))))
        for path in sorted(TEMPLATES.glob("*.html"))
    }
    found = {name: hits for name, hits in found.items() if hits}
    assert not found, found


def test_generated_css_keeps_the_library_os_theme():
    """theme 原样搬自 base.html 的 Play CDN 内联配置，重生成时漏了配置会让品牌色悄悄变成默认蓝。"""
    css = CSS.read_text(encoding="utf-8")
    assert re.search(r"\.text-brand-600\{[^}]*color:rgb\(59 99 219", css)
    assert re.search(r"\.bg-brand-500\{[^}]*background-color:rgb\(79 124 255", css)
    assert re.search(r"\.text-pixiv-gray\{[^}]*color:rgb\(107 114 128", css)
    preflight = re.search(r"(?::host,html|html,:host)\{[^}]*\}", css).group(0)
    assert "font-family:Inter,-apple-system" in preflight
    serif = re.search(r"\.font-serif\{[^}]*\}", css).group(0)
    assert "BIZ UDMincho" in serif and "Noto Serif SC" in serif


def test_stylesheet_is_packaged():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"static/css/*.css"' in pyproject
    assert CSS.stat().st_size > 10_000


def test_stylesheet_is_public_and_long_cached(tmp_path, monkeypatch):
    """登录页（token_login.html 也继承 base.html）在登录前就要拿到样式，所以 /static/ 必须免登录；
    带 ?v= 的 URL 内容随版本号变，可以 immutable 长缓存，免得每次翻页都为阻塞渲染的样式表多等一个往返。"""
    from pixiv_novel_sync.webapp import create_app

    monkeypatch.setenv("DASHBOARD_TOKEN", "secret-token")
    monkeypatch.delenv("PIXIV_FLASK_SECRET", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\nDASHBOARD_TOKEN=secret-token\n", encoding="utf-8")
    client = create_app(env_path=str(env_path), start_scheduler=False).test_client()
    remote = {"REMOTE_ADDR": "203.0.113.7"}

    assert client.get("/dashboard", environ_base=remote).status_code == 302
    versioned = client.get(f"/static/css/tailwind.css?v={css_version()}", environ_base=remote)
    assert versioned.status_code == 200
    assert versioned.mimetype == "text/css"
    assert versioned.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert versioned.get_data() == CSS.read_bytes()
    versioned.close()
    bare = client.get("/static/css/tailwind.css", environ_base=remote)
    assert "immutable" not in bare.headers.get("Cache-Control", "")
    bare.close()
