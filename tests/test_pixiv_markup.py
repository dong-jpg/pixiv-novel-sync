"""Pixiv 正文标记解析器：共享夹具、恶意输入与线性时间。

``tests/fixtures/pixiv_markup_cases.json`` 同时由 ``test_pixiv_markup_runtime.cjs``
喂给阅读页的 JS 解析器；``test_js_parser_matches_python_on_fuzz_corpus`` 再拿同一批
随机输入对两边的输出逐项比对，保证阅读页与 EPUB 看到的是同一棵树。
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from pixiv_novel_sync import pixiv_markup

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
CASES = json.loads((FIXTURES / "pixiv_markup_cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_parser_matches_shared_fixture(case):
    assert pixiv_markup.parse(case["text"]) == case["pages"]


@pytest.mark.parametrize(
    "url,safe",
    [
        ("https://www.pixiv.net/novel/show.php?id=1", True),
        ("HTTP://EXAMPLE.COM/", True),
        ("javascript:alert(1)", False),
        ("JaVaScRiPt:alert(1)", False),
        ("data:text/html,<script>alert(1)</script>", False),
        ("//evil.example/", False),
        ("https://", False),
        ("https://e.com/\"onmouseover=\"x", False),
        ("https://e.com/a b", False),
        ("https://e.com/\nx", False),
        ("https://e.com/　x", False),
        ("httpſ://e.com/", False),  # ſ 不能被大小写折叠成 s
        ("", False),
        (None, False),
    ],
)
def test_only_http_urls_are_safe(url, safe):
    assert pixiv_markup.is_safe_url(url) is safe


def test_plain_text_drops_ruby_reading_and_jump():
    nodes = pixiv_markup.parse("[chapter:[[rb:始 > はじ]]まり[jump:2][b:強]]")[0][0]["content"]
    assert pixiv_markup.plain_text(nodes) == "始まり強"


@pytest.mark.parametrize("image_id,expected", [("12345", "12345"), ("12345-2", "12345"), ("abc", None), ("１２", None), ("", None)])
def test_pixiv_artwork_id(image_id, expected):
    assert pixiv_markup.pixiv_artwork_id(image_id) == expected


@pytest.mark.parametrize(
    "text",
    [
        "a" + "\n" * 100_000 + "b",
        "[b:" * 100_000,
        "[[rb:x" * 50_000,
        "[" * 100_000 + "]" * 100_000,
        "[pixivimage:" * 50_000 + "]" * 50_000,
        "[chapter:" * 30_000 + "]" * 30_000,
        " " * 200_000 + "x",
        "[[jumpuri:" + ">" * 200_000 + "]]",
        "x[newpage]" * 30_000,
    ],
    ids=["blank-lines", "unclosed-b", "unclosed-rb", "brackets", "nested-images", "nested-chapters",
         "spaces", "gt-run", "newpages"],
)
def test_adversarial_input_parses_in_linear_time(text):
    started = time.perf_counter()
    pages = pixiv_markup.parse(text)
    assert time.perf_counter() - started < 3.0
    assert pages


def test_ten_thousand_blank_lines_collapse():
    pages = pixiv_markup.parse("上" + "\n" * 10_000 + "下")
    assert [block["type"] for block in pages[0]] == ["paragraph", "blank", "blank", "blank", "paragraph"]


def _fuzz_corpus(count: int, seed: int) -> list[str]:
    pieces = json.loads((FIXTURES / "pixiv_markup_fuzz_pieces.json").read_text(encoding="utf-8"))
    rng = random.Random(seed)
    return ["".join(rng.choice(pieces) for _ in range(rng.randint(1, 18))) for _ in range(count)]


def test_fuzz_corpus_never_raises():
    for text in _fuzz_corpus(3000, 1029):
        pixiv_markup.parse(text)


def test_js_parser_matches_python_on_fuzz_corpus(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the cross-language parity check")
    corpus = [{"text": text, "pages": pixiv_markup.parse(text)} for text in _fuzz_corpus(3000, 20261029)]
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(corpus, ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [node, str(ROOT / "tests/test_pixiv_markup_runtime.cjs"), str(corpus_path)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_reader_markup_runtime():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for frontend runtime regression checks")
    result = subprocess.run(
        [node, str(ROOT / "tests/test_pixiv_markup_runtime.cjs")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
