"""EPUB 导出：按 [newpage] / [chapter:] 分章与目录、注音、链接白名单、插图降级、
语言、封面类型，以及恶意正文下仍是合法 XHTML；批量导出的 zip 文件名带 _{novel_id}。"""
from __future__ import annotations

import io
import json
import random
import re
import time
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from pixiv_novel_sync import epub_exporter
from pixiv_novel_sync.epub_exporter import create_epub_from_novel, detect_language
from pixiv_novel_sync.storage_db import Database
from pixiv_novel_sync.webapp import create_app

ROOT = Path(__file__).resolve().parents[1]
NS = {"x": "http://www.w3.org/1999/xhtml", "opf": "http://www.idpf.org/2007/opf",
      "dc": "http://purl.org/dc/elements/1.1/", "ncx": "http://www.daisy.org/z3986/2005/ncx/"}
STRICT = etree.XMLParser(recover=False, resolve_entities=False)
RAW_MARKERS = ("[newpage]", "[chapter:", "[[rb:", "[[jumpuri:", "[jump:", "[pixivimage:",
               "[uploadedimage:", "[b:", "[i:", "[[emphasismark:")

SAMPLE = (
    "　これは[[rb:漢字 > かんじ]]の本文です。\n\n"
    "[chapter:第一章 [[rb:始 > はじ]]まり]\n"
    "本文<script>alert(1)</script>と[[jumpuri:公式 > https://www.pixiv.net/?a=1&b=2]]、"
    "[[jumpuri:危険 > javascript:alert(1)]]。[jump:3][jump:99]\n"
    "[pixivimage:12345-2]\n[uploadedimage:999]\n"
    "[b:太字[i:斜体]]と[[emphasismark:強調 > ﹅]]\n"
    "[newpage]二ページ目\n[chapter:第二章]\n本文\n"
    "[newpage]三ページ目\n"
)


def _book(text: str = SAMPLE, **kwargs) -> zipfile.ZipFile:
    novel = {"novel_id": 42, "title": "テスト作品", "author_name": "作者"}
    novel.update(kwargs.pop("novel", {}))
    return zipfile.ZipFile(io.BytesIO(create_epub_from_novel(novel, text, **kwargs)))


def _xml(book: zipfile.ZipFile, name: str):
    return etree.fromstring(book.read(name), STRICT)


def _chapters(book: zipfile.ZipFile) -> list[str]:
    return sorted(name for name in book.namelist() if re.fullmatch(r"EPUB/chap_\d+\.xhtml", name))


def _body_text(book: zipfile.ZipFile, name: str) -> str:
    return "".join(_xml(book, name).find("x:body", NS).itertext())


def test_every_document_is_well_formed_xml():
    book = _book()
    assert book.read("mimetype") == b"application/epub+zip"
    for name in book.namelist():
        if name.endswith((".xhtml", ".opf", ".ncx")):
            assert book.read(name), name
            _xml(book, name)


def test_newpage_and_chapter_split_files_and_build_the_toc():
    book = _book()
    # 第 1 页：开头 + 第一章；第 2 页：续页 + 第二章；第 3 页
    assert len(_chapters(book)) == 5
    nav = _xml(book, "EPUB/nav.xhtml")
    entries = [(a.get("href"), a.text) for a in nav.iterfind(".//x:nav//x:a", NS)]
    assert entries == [("chap_001.xhtml", "テスト作品"), ("chap_002.xhtml", "第一章 始まり"), ("chap_004.xhtml", "第二章")]
    ncx = _xml(book, "EPUB/toc.ncx")
    assert [t.text for t in ncx.iterfind(".//ncx:navLabel/ncx:text", NS)] == ["テスト作品", "第一章 始まり", "第二章"]
    opf = _xml(book, "EPUB/content.opf")
    spine = [item.get("idref") for item in opf.iterfind("opf:spine/opf:itemref", NS)]
    assert spine == ["nav", "chapter_0", "chapter_1", "chapter_2", "chapter_3", "chapter_4"]


def test_no_raw_markers_left_in_any_chapter():
    book = _book()
    for name in _chapters(book):
        text = _body_text(book, name)
        for marker in RAW_MARKERS:
            assert marker not in text, (name, marker)


def test_ruby_links_jumps_and_escaping():
    book = _book()
    first = _xml(book, "EPUB/chap_001.xhtml")
    ruby = first.find(".//x:ruby", NS)
    assert ruby.text == "漢字" and ruby.find("x:rt", NS).text == "かんじ"
    chapter = _xml(book, "EPUB/chap_002.xhtml")
    assert chapter.find(".//x:h2/x:ruby/x:rt", NS).text == "はじ"
    hrefs = [a.get("href") for name in _chapters(book) for a in _xml(book, name).iterfind(".//x:a", NS)]
    assert "https://www.pixiv.net/?a=1&b=2" in hrefs
    assert not any(href.lower().startswith("javascript") for href in hrefs)
    assert all(re.fullmatch(r"https://www\.pixiv\.net/.*|chap_\d{3}\.xhtml(#page-\d+)?", href) for href in hrefs), hrefs
    # [jump:3] 链到第 3 页所在文件；[jump:99] 不存在，只留文字
    jumps = {a.text: a.get("href") for a in chapter.iterfind(".//x:a", NS)}
    assert jumps["跳到第 3 页"] == "chap_005.xhtml"
    body = _body_text(book, "EPUB/chap_002.xhtml")
    assert "危険" in body and "跳到第 99 页" in body and "跳到第 99 页" not in jumps
    # 正文里的 <script> 是文字，不是元素
    assert chapter.find(".//x:script", NS) is None
    assert "<script>alert(1)</script>" in body


def test_renderer_rechecks_link_urls_even_if_the_tree_is_wrong():
    """解析器已把非 http(s) 的 href 置空；渲染端仍再校验一次，树被改坏也不放行。"""
    nodes = [{"type": "link", "href": "javascript:alert(1)", "content": ["x"]},
             {"type": "link", "href": 'https://e.com/"onclick="y', "content": ["y"]}]
    assert epub_exporter._render_inline(nodes, lambda page: None, False) == "xy"


def test_images_degrade_to_text_and_emphasis_survives():
    chapter = _xml(_book(), "EPUB/chap_002.xhtml")
    notes = chapter.findall(".//x:p[@class='image-missing']", NS)
    assert len(notes) == 2
    assert all("此处原有插图" in "".join(note.itertext()) for note in notes)
    assert notes[0].find("x:a", NS).get("href") == "https://www.pixiv.net/artworks/12345"
    assert notes[1].find("x:a", NS) is None
    assert chapter.find(".//x:img", NS) is None
    assert chapter.find(".//x:b/x:i", NS).text == "斜体"
    assert chapter.find(".//x:span[@class='emphasis-sesame']", NS).text == "強調"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("これは日本語の文章です。漢字も少しあります。", "ja"),
        ("这是一段中文正文，里面偶尔夹一个日文的の字，以及人名约翰・史密斯和长音ー。" * 3, "zh"),
        ("이것은 한국어 문장입니다. 漢字", "ko"),
        ("Just some English text.", "en"),
        ("", "en"),
    ],
)
def test_detect_language(text, expected):
    assert detect_language(text) == expected


def test_language_follows_the_body_and_can_be_overridden():
    zh = _book("这是中文正文。[newpage]第二页。", novel={"title": "中文作品"})
    assert _xml(zh, "EPUB/content.opf").find(".//dc:language", NS).text == "zh"
    assert _xml(zh, "EPUB/chap_001.xhtml").get("lang") == "zh"
    forced = _book("这是中文正文。", language="zh-TW")
    assert _xml(forced, "EPUB/content.opf").find(".//dc:language", NS).text == "zh-TW"


@pytest.mark.parametrize(
    "data,file_name,media_type",
    [
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "images/cover.png", "image/png"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 32, "images/cover.jpg", "image/jpeg"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 32, "images/cover.webp", "image/webp"),
    ],
)
def test_cover_uses_the_real_image_type(tmp_path, data, file_name, media_type):
    cover = tmp_path / "cover.jpg"  # 扩展名故意写错：以文件头为准
    cover.write_bytes(data)
    book = _book("本文", cover_path=cover)
    assert book.read("EPUB/" + file_name) == data
    opf = _xml(book, "EPUB/content.opf")
    item = opf.find(f"opf:manifest/opf:item[@href='{file_name}']", NS)
    assert item.get("media-type") == media_type and item.get("properties") == "cover-image"
    assert opf.find("opf:metadata/opf:meta[@name='cover']", NS).get("content") == item.get("id")
    first = opf.find("opf:spine/opf:itemref", NS)
    assert first.get("idref") == "cover" and first.get("linear") == "no"


def test_unknown_or_missing_cover_is_skipped(tmp_path):
    junk = tmp_path / "cover.jpg"
    junk.write_bytes(b"<html>not an image</html>")
    for path in (junk, tmp_path / "missing.jpg"):
        book = _book("本文", cover_path=path)
        assert not any(name.startswith("EPUB/images/") for name in book.namelist())
        assert _xml(book, "EPUB/content.opf").find("opf:metadata/opf:meta[@name='cover']", NS) is None


def test_single_page_book_has_no_nav_page_in_the_spine():
    book = _book("只有一页的正文")
    opf = _xml(book, "EPUB/content.opf")
    assert [i.get("idref") for i in opf.iterfind("opf:spine/opf:itemref", NS)] == ["chapter_0"]
    assert _chapters(book) == ["EPUB/chap_001.xhtml"]


def test_hostile_body_still_produces_valid_xhtml():
    text = ("控制符\x00\x01\x0b\x1f与孤立代理\ud800[chapter:\x07章\ud800]" + "[b:" * 500 + "[[rb:未闭合 > x]"
            + "\n" * 10_000 + "<script>alert(1)</script>[newpage][newpage]空页之后")
    book = _book(text, novel={"title": "标题\x00\x1b", "author_name": "作者\x08"})
    for name in book.namelist():
        if name.endswith((".xhtml", ".opf", ".ncx")):
            assert book.read(name), name
            _xml(book, name)
    # 行中的 [chapter:] 照样另起一个文件：开头 / 章 / 空页 / 末页
    chapters = _chapters(book)
    assert len(chapters) == 4
    text = "".join(_body_text(book, name) for name in chapters)
    assert "[[rb:未闭合 > x]" in text and "<script>alert(1)</script>" in text
    assert _xml(book, chapters[1]).find(".//x:h2", NS).text == "章"
    # 一万个空行只剩 3 个 <br/>
    assert sum(len(_xml(book, name).findall(".//x:br", NS)) for name in chapters) == 3
    assert _xml(book, "EPUB/content.opf").find(".//dc:title", NS).text == "标题"
    assert _xml(book, "EPUB/content.opf").find(".//dc:creator", NS).text == "作者"


def test_page_flood_is_capped_and_far_jumps_still_resolve():
    text = "[jump:1999]" + "".join(f"[newpage]第{n}页" for n in range(2, 2001))
    started = time.perf_counter()
    book = _book(text)
    assert time.perf_counter() - started < 30
    assert len(_chapters(book)) == epub_exporter.MAX_SECTIONS
    first = _xml(book, "EPUB/chap_001.xhtml")
    assert first.find(".//x:a", NS).get("href") == f"chap_{epub_exporter.MAX_SECTIONS:03d}.xhtml#page-1999"
    last = _xml(book, f"EPUB/chap_{epub_exporter.MAX_SECTIONS:03d}.xhtml")
    assert last.find(".//x:hr[@id='page-1999']", NS) is not None


def test_chapters_past_the_cap_stay_inline_in_the_last_file():
    text = "".join(f"[newpage]第{n}页" for n in range(2, epub_exporter.MAX_SECTIONS + 5)) + "\n[chapter:超出上限的章]\n尾"
    book = _book("开头" + text)
    chapters = _chapters(book)
    assert len(chapters) == epub_exporter.MAX_SECTIONS
    last = _xml(book, chapters[-1])
    assert [h.text for h in last.iterfind(".//x:h2", NS)] == ["超出上限的章"]
    assert len(list(_xml(book, "EPUB/nav.xhtml").iterfind(".//x:nav//x:a", NS))) == epub_exporter.MAX_SECTIONS


def test_fuzzed_bodies_always_produce_valid_xhtml():
    pieces = json.loads((ROOT / "tests/fixtures/pixiv_markup_fuzz_pieces.json").read_text(encoding="utf-8"))
    rng = random.Random(42)
    for _ in range(120):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 40)))
        book = _book(text)
        for name in _chapters(book) + ["EPUB/nav.xhtml", "EPUB/toc.ncx", "EPUB/content.opf"]:
            assert book.read(name), (text, name)
            _xml(book, name)


def _seed_same_title_novels(db_path: Path) -> None:
    db = Database(db_path)
    db.init_schema()
    db.conn.execute("INSERT INTO users (user_id, name, raw_json) VALUES (1, '作者', '{}')")
    for novel_id in (101, 102):
        db.conn.execute(
            """
            INSERT INTO novels (
                novel_id, user_id, title, visible, restrict_value, x_restrict, text_length,
                total_bookmarks, total_views, tags_json, raw_json, meta_hash, status
            ) VALUES (?, 1, '同名作品', 1, 'public', 0, 4, 0, 0, '[]', '{}', ?, 'normal')
            """,
            (novel_id, f"h{novel_id}"),
        )
        db.conn.execute(
            "INSERT INTO novel_texts (novel_id, text_raw, has_content, text_hash) VALUES (?, ?, 1, ?)",
            (novel_id, f"第 {novel_id} 篇[[rb:正文 > ほんぶん]]", f"t{novel_id}"),
        )
    db.conn.commit()
    db.close()


@pytest.fixture
def export_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "state" / "export.db"
    monkeypatch.setenv("PIXIV_DB_PATH", str(db_path))
    env_path = tmp_path / ".env"
    env_path.write_text("PIXIV_REFRESH_TOKEN=test\n", encoding="utf-8")
    app = create_app(env_path=str(env_path), start_scheduler=False)
    app.config.update(TESTING=True)
    _seed_same_title_novels(db_path)
    return app.test_client()


def test_batch_export_names_carry_the_novel_id(export_client):
    response = export_client.post("/api/dashboard/novels/export-epub", json={"novel_ids": [101, 102, 101, 999]})
    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.data))
    # 同名作品各占一个条目；重复 ID 只导一次；不存在的 ID 跳过
    assert sorted(archive.namelist()) == ["同名作品_101.epub", "同名作品_102.epub"]
    for name in archive.namelist():
        book = zipfile.ZipFile(io.BytesIO(archive.read(name)))
        assert book.read("mimetype") == b"application/epub+zip"
        assert b"<rt>" in book.read("EPUB/chap_001.xhtml")


def test_single_export_still_returns_one_epub(export_client):
    response = export_client.post("/api/dashboard/novels/export-epub", json={"novel_ids": [101]})
    assert response.status_code == 200
    assert response.mimetype == "application/epub+zip"
    book = zipfile.ZipFile(io.BytesIO(response.data))
    assert b"<ruby>" in book.read("EPUB/chap_001.xhtml")
