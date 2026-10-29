"""重生成 src/pixiv_novel_sync/static/css/tailwind.css（只有维护者改模板类名时才需要）。

    python scripts/build_tailwind_css.py          # 重生成 CSS，并更新 base.html 里的 ?v= 版本号
    python scripts/build_tailwind_css.py --check  # 只检查已提交的 CSS 是否与模板一致，不写文件

安装与部署不需要运行它，也不需要 Node：生成好的 CSS 已提交进仓库。脚本按平台下载钉死版本的
Tailwind v3 standalone CLI（单个可执行文件，自带运行时），校验 SHA-256 后缓存到
~/.cache/pixiv-novel-sync/；离线时可用 --cli 或环境变量 TAILWINDCSS_BIN 指定已有的同版本 CLI。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import platform
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

TAILWIND_VERSION = "3.4.17"
# 摘自官方 release 的 sha256sums.txt；升级版本时整表一起换
TAILWIND_SHA256 = {
    "tailwindcss-linux-arm64": "69b1378b8133192d7d2feb12a116fa12d035594f58db3eff215879e4ad8cf39b",
    "tailwindcss-linux-armv7": "704e7d91afba6e1f630889afd0d7db36b4634e628512cc141d504a5beae28860",
    "tailwindcss-linux-x64": "7d24f7fa191d2193b78cd5f5a42a6093e14409521908529f42d80b11fde1f1d4",
    "tailwindcss-macos-arm64": "a1d0c7985759accca0bf12e51ac1dcbf0f6cf2fffb62e6e0f62d091c477a10a3",
    "tailwindcss-macos-x64": "6cbdad74be776c087ffa5e9a057512c54898f9fe8828d3362212dfe32fc933a3",
    "tailwindcss-windows-arm64.exe": "76f516476784c00f1562160b5758e3d8f0e6c48957efb26b5b50fbdfd76aa382",
    "tailwindcss-windows-x64.exe": "67f1c5e3f5a03406a7bf5badf5ada09b79f3ae78ec43450c15f7e983068da346",
}
DOWNLOAD_URL = "https://github.com/tailwindlabs/tailwindcss/releases/download/v{version}/{asset}"

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "scripts" / "tailwind.config.js"
OUTPUT = ROOT / "src" / "pixiv_novel_sync" / "static" / "css" / "tailwind.css"
BASE_TEMPLATE = ROOT / "src" / "pixiv_novel_sync" / "templates" / "base.html"
# base.html 用内容哈希做缓存版本号：静态文件按 ?v= 长缓存，CSS 一变 URL 就变
VERSION_PATTERN = re.compile(r"(/static/css/tailwind\.css\?v=)[0-9a-f]{12}(?![0-9a-f])")
HEADER = "/* 生成文件，勿手改：python scripts/build_tailwind_css.py 重生成（见 CLAUDE.md Frontend 一节） */\n"


def css_version(css: bytes) -> str:
    """与 tests/test_tailwind_static_css.py 同一算法；先统一换行，免得 CRLF 检出算出另一个值。"""
    return hashlib.sha256(css.replace(b"\r\n", b"\n")).hexdigest()[:12]


def asset_name() -> str:
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64", "armv7l": "armv7"}.get(machine)
    system = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux" if sys.platform.startswith("linux") else None)
    name = f"tailwindcss-{system}-{arch}" + (".exe" if system == "windows" else "")
    if name not in TAILWIND_SHA256:
        raise SystemExit(f"没有适合 {sys.platform}/{machine} 的 Tailwind standalone CLI，请用 --cli 指定")
    return name


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_cli(explicit: str | None) -> Path:
    if explicit:
        cli = Path(explicit)
        result = subprocess.run([str(cli), "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if f"v{TAILWIND_VERSION}" not in result.stdout + result.stderr:
            raise SystemExit(f"{cli} 不是 Tailwind CSS v{TAILWIND_VERSION}，换版本会改变生成结果")
        return cli
    name = asset_name()
    cache_root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    cli = cache_root / "pixiv-novel-sync" / f"tailwindcss-{TAILWIND_VERSION}" / name
    if cli.exists() and sha256_file(cli) == TAILWIND_SHA256[name]:
        return cli
    cli.parent.mkdir(parents=True, exist_ok=True)
    url = DOWNLOAD_URL.format(version=TAILWIND_VERSION, asset=name)
    print(f"下载 {url}", file=sys.stderr)
    tmp = cli.with_name(cli.name + ".part")
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=120) as response, tmp.open("wb") as fh:
        for chunk in iter(lambda: response.read(1 << 20), b""):
            digest.update(chunk)
            fh.write(chunk)
    if digest.hexdigest() != TAILWIND_SHA256[name]:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"{name} 的 SHA-256 与官方 sha256sums.txt 不符，已删除下载文件")
    if os.name != "nt":
        tmp.chmod(0o755)
    os.replace(tmp, cli)
    return cli


def build(cli: Path) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "tailwind.css"
        result = subprocess.run(
            [str(cli), "--config", str(CONFIG), "--output", str(out), "--minify"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode != 0 or not out.exists():
            raise SystemExit(f"Tailwind CLI 失败（exit {result.returncode}）：\n{result.stdout}{result.stderr}")
        css = out.read_bytes().replace(b"\r\n", b"\n").rstrip(b"\n")
    return HEADER.encode("utf-8") + css + b"\n"


def class_names(css: bytes) -> set[str]:
    """生成的 CSS 里出现过的类名（反转义后），只看选择器，不看声明值里的 .5em 之类。"""
    text = re.sub(r"/\*.*?\*/", "", css.decode("utf-8"), flags=re.S)
    names = set()
    for selector in re.findall(r"([^{}]*)\{", text):
        if selector.strip().startswith("@"):
            continue
        for raw in re.findall(r"\.((?:\\[0-9a-fA-F]{1,6} ?|\\.|[\w-])+)", selector):
            names.add(re.sub(r"\\([0-9a-fA-F]{1,6}) ?|\\(.)",
                             lambda m: chr(int(m.group(1), 16)) if m.group(1) else m.group(2), raw))
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="只检查，不写文件；不一致时退出码 1")
    parser.add_argument("--cli", default=os.environ.get("TAILWINDCSS_BIN"), help=f"已有的 Tailwind v{TAILWIND_VERSION} standalone CLI")
    args = parser.parse_args()

    css = build(ensure_cli(args.cli))
    version = css_version(css)
    old = OUTPUT.read_bytes() if OUTPUT.exists() else b""
    # 按字节读写，保留 base.html 检出时的换行风格（Windows 上 autocrlf 是 CRLF）
    base = BASE_TEMPLATE.read_bytes().decode("utf-8")
    if not VERSION_PATTERN.search(base):
        raise SystemExit("base.html 里找不到 /static/css/tailwind.css?v=… 的 <link>")
    new_base = VERSION_PATTERN.sub(lambda m: m.group(1) + version, base)

    if args.check:
        stale = old.replace(b"\r\n", b"\n") != css or new_base != base
        print("tailwind.css 与模板不一致，请重生成" if stale else "tailwind.css 已是最新")
        return 1 if stale else 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(css)
    if new_base != base:
        BASE_TEMPLATE.write_bytes(new_base.encode("utf-8"))
    before, after = class_names(old), class_names(css)
    print(f"{OUTPUT.relative_to(ROOT)}: {len(css)} 字节（gzip {len(gzip.compress(css, 9))}），版本 {version}")
    for label, items in (("新增", sorted(after - before)), ("删除", sorted(before - after))):
        if items:
            print(f"{label} {len(items)} 个类：" + " ".join(items[:40]) + (" …" if len(items) > 40 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
