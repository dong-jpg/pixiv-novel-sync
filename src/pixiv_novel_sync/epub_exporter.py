"""EPUB导出功能模块

正文按 ``pixiv_markup`` 解析：``[newpage]`` 和 ``[chapter:]`` 各起一个 XHTML 文件并进目录，
注音转 ``<ruby><rt>``，jumpuri 只放行 http(s)，``[jump:N]`` 链到对应页的文件。
插图标记降级成文字说明：备份没有存「插图 ID → 图片文件」的对应关系，不猜图。
"""
from __future__ import annotations

import html
import io
import re
from pathlib import Path
from typing import Any

from ebooklib import epub

from . import pixiv_markup

# 恶意正文可以塞上万个 [newpage]；超过这个数的分页并进最后一个文件（页内锚点仍可跳转）
MAX_SECTIONS = 500
# XML 1.0 不允许的字符：C0 控制符（保留 \t \n \r）、孤立代理项、U+FFFE / U+FFFF。
# lxml 遇到它们要么报错要么生成阅读器打不开的文件，先剔掉
_XML_INVALID_RE = re.compile("[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\ud800-\\udfff\\ufffe\\uffff]")
_IMAGE_NOTE = "此处原有插图（私人备份未收录插图）"
_NAV_TITLES = {"ja": "目次", "zh": "目录", "ko": "목차"}
_STYLE = """\
h2.chapter { margin: 1.5em 0 1em; font-size: 1.3em; font-weight: bold; }
hr.page-break { margin: 2em 0; border: 0; border-top: 1px solid #ccc; }
p.image-missing { margin: 1em 0; color: #777; font-size: 0.9em; text-indent: 0; }
span.emphasis { text-emphasis: filled dot; -epub-text-emphasis-style: filled dot; -webkit-text-emphasis: filled dot; }
span.emphasis-sesame { text-emphasis: filled sesame; -epub-text-emphasis-style: filled sesame; -webkit-text-emphasis: filled sesame; }
"""


def create_epub_from_novel(
    novel_data: dict[str, Any],
    text_content: str,
    cover_path: Path | None = None,
    language: str | None = None,
) -> bytes:
    """从小说数据创建EPUB字节流；``language`` 不传时按正文判断（ja / zh / ko / en）。"""
    novel_id = novel_data["novel_id"]
    title = _xml_safe(str(novel_data.get("title") or f"Novel {novel_id}"))
    author = _xml_safe(str(novel_data.get("author_name") or "Unknown"))
    text = _xml_safe(text_content or "")
    lang = language or detect_language(text)

    book = epub.EpubBook()
    book.set_identifier(f"pixiv-novel-{novel_id}")
    book.set_title(title)
    book.set_language(lang)
    book.add_author(author)

    cover_page = _add_cover(book, cover_path)
    style = epub.EpubItem(uid="style", file_name="style/main.css", media_type="text/css", content=_STYLE)
    book.add_item(style)

    pages = pixiv_markup.parse(text)
    sections, page_targets = _build_sections(pages)
    files = [f"chap_{index + 1:03d}.xhtml" for index in range(len(sections))]

    def page_href(page: int) -> str | None:
        target = page_targets.get(page)
        if target is None:
            return None
        section_index, anchor = target
        return files[section_index] + (f"#{anchor}" if anchor else "")

    chapters = []
    for index, section in enumerate(sections):
        chapter = epub.EpubHtml(
            title=_section_title(section, index, title, len(sections)),
            file_name=files[index],
            lang=lang,
        )
        chapter.set_content(_render_section(section, title if index == 0 else None, page_href))
        chapter.add_item(style)
        book.add_item(chapter)
        chapters.append(chapter)

    book.toc = tuple(chapters[index] for index in _toc_indexes(sections))
    book.add_item(epub.EpubNcx())
    nav = epub.EpubNav(title=_NAV_TITLES.get(lang.split("-")[0], "Contents"))
    nav.add_item(style)
    book.add_item(nav)

    # 封面页放最前且不参与线性阅读；只有一个目录项时目录页没有意义，不进书脊
    spine: list[Any] = []
    if cover_page is not None:
        spine.append((cover_page, "no"))
    if len(book.toc) > 1:
        spine.append(nav)
    book.spine = spine + chapters

    output = io.BytesIO()
    epub.write_epub(output, book)
    return output.getvalue()


def detect_language(text: str) -> str:
    """按正文前 2 万字粗判语言：有一定比例假名是日文，谚文为主是韩文，有汉字是中文，否则英文。

    假名按占「假名 + 汉字」的比例算：中文网文偶尔夹几个「の」，不能一见假名就判日文。
    「・」「ー」两个片假名符号在中文里也常用来写外国人名，不计入。
    """
    kana = hangul = han = 0
    for ch in text[:20000]:
        code = ord(ch)
        if (0x3041 <= code <= 0x309F or 0x30A0 <= code <= 0x30FA or 0x30FD <= code <= 0x30FF
                or 0x31F0 <= code <= 0x31FF or 0xFF66 <= code <= 0xFF9D):
            kana += 1
        elif 0xAC00 <= code <= 0xD7A3 or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F:
            hangul += 1
        elif 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF or 0xF900 <= code <= 0xFAFF:
            han += 1
    if hangul and hangul >= han:
        return "ko"
    if kana and kana * 10 >= kana + han:
        return "ja"
    if han:
        return "zh"
    return "en"


def _xml_safe(value: str) -> str:
    return _XML_INVALID_RE.sub("", value)


def _sniff_image(data: bytes) -> tuple[str, str] | None:
    """按文件头认封面格式；只收 EPUB 核心媒体类型，认不出就不放封面。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def _add_cover(book: epub.EpubBook, cover_path: Path | None) -> epub.EpubCoverHtml | None:
    if cover_path is None:
        return None
    try:
        data = cover_path.read_bytes()
    except OSError:
        return None
    kind = _sniff_image(data)
    if kind is None:
        return None
    extension, media_type = kind
    # 不用 book.set_cover：它按文件名猜媒体类型，PNG/WebP 封面也会被写成固定的 cover.jpg
    image_name = f"images/cover.{extension}"
    image = epub.EpubCover(file_name=image_name)
    image.media_type = media_type
    image.content = data
    book.add_item(image)
    page = epub.EpubCoverHtml(image_name=image_name)
    book.add_item(page)
    book.add_metadata(None, "meta", "", {"name": "cover", "content": image.id})
    return page


def _build_sections(pages: list[list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], dict[int, tuple[int, str | None]]]:
    """按页和章切分。返回 (sections, 页码 → (section 下标, 锚点))。

    每页起一个 section；页内遇到 [chapter:] 时若当前 section 已有内容就再起一个，
    章名紧跟在页首时直接当这一页的章名。超过 MAX_SECTIONS 后不再新开文件，
    分页画成 <hr> 并带锚点，章名留在正文里。
    """
    sections: list[dict[str, Any]] = []
    page_targets: dict[int, tuple[int, str | None]] = {}
    for page_index, blocks in enumerate(pages):
        page = page_index + 1
        if len(sections) < MAX_SECTIONS:
            sections.append({"page": page, "chapter": None, "blocks": []})
            page_targets[page] = (len(sections) - 1, None)
        else:
            anchor = f"page-{page}"
            sections[-1]["blocks"].append({"type": "pagebreak", "anchor": anchor})
            page_targets[page] = (len(sections) - 1, anchor)
        for block in blocks:
            current = sections[-1]
            if block["type"] == "chapter":
                if current["chapter"] is None and not current["blocks"]:
                    current["chapter"] = block["content"]
                    continue
                if len(sections) < MAX_SECTIONS:
                    sections.append({"page": page, "chapter": block["content"], "blocks": []})
                    continue
            current["blocks"].append(block)
    return sections, page_targets


def _chapter_title(content: list[Any]) -> str:
    return pixiv_markup.plain_text(content).strip() or "无标题章节"


def _section_title(section: dict[str, Any], index: int, book_title: str, total: int) -> str:
    if section["chapter"] is not None:
        return _chapter_title(section["chapter"])
    if index == 0 or total == 1:
        return book_title
    return f"第 {section['page']} 页"


def _toc_indexes(sections: list[dict[str, Any]]) -> list[int]:
    """有章名时目录列「开头（无章名的话用书名）+ 每一章」，否则逐页列出。"""
    if any(section["chapter"] is not None for section in sections):
        return [index for index, section in enumerate(sections) if index == 0 or section["chapter"] is not None]
    return list(range(len(sections)))


def _render_section(section: dict[str, Any], book_title: str | None, page_href) -> str:
    parts: list[str] = []
    if book_title is not None:
        parts.append(f"<h1>{_escape(book_title)}</h1>")
    if section["chapter"] is not None:
        parts.append(f'<h2 class="chapter">{_render_inline(section["chapter"], page_href, False)}</h2>')
    for block in section["blocks"]:
        parts.append(_render_block(block, page_href))
    # lxml 解析空字符串会失败，ebooklib 随即写出 0 字节文件；空页至少留一个空段落
    return "\n".join(parts) or "<p></p>"


def _render_block(block: dict[str, Any], page_href) -> str:
    kind = block["type"]
    if kind == "paragraph":
        return f'<p>{_escape(block["indent"])}{_render_inline(block["content"], page_href, False)}</p>'
    if kind == "blank":
        return "<br/>"
    if kind == "chapter":
        return f'<h2 class="chapter">{_render_inline(block["content"], page_href, False)}</h2>'
    if kind == "image":
        return _render_image(block)
    if kind == "pagebreak":
        return f'<hr class="page-break" id="{block["anchor"]}"/>'
    return ""


def _render_image(block: dict[str, Any]) -> str:
    artwork = pixiv_markup.pixiv_artwork_id(block["id"]) if block["kind"] == "pixivimage" else None
    if artwork is None:
        return f'<p class="image-missing">［{_IMAGE_NOTE}］</p>'
    href = _escape(f"https://www.pixiv.net/artworks/{artwork}")
    return f'<p class="image-missing">［{_IMAGE_NOTE}：<a href="{href}">到 Pixiv 查看作品</a>］</p>'


def _render_inline(nodes: list[Any], page_href, in_link: bool) -> str:
    """链接里不能再套链接：嵌套的 jumpuri / jump 只留文字。"""
    parts: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            parts.append(_escape(node))
            continue
        kind = node["type"]
        if kind == "ruby":
            if node["rt"]:
                parts.append(f'<ruby>{_escape(node["base"])}<rp>(</rp><rt>{_escape(node["rt"])}</rt><rp>)</rp></ruby>')
            else:
                parts.append(_escape(node["base"]))
        elif kind == "link":
            label = _render_inline(node["content"], page_href, True)
            href = node["href"]
            if in_link or not pixiv_markup.is_safe_url(href):
                parts.append(label)
            else:
                parts.append(f'<a href="{_escape(href)}">{label}</a>')
        elif kind == "jump":
            label = _escape(f"跳到第 {node['page']} 页")
            target = None if in_link else page_href(node["page"])
            parts.append(f'<a href="{_escape(target)}">{label}</a>' if target else label)
        elif kind == "bold":
            parts.append(f"<b>{_render_inline(node['content'], page_href, in_link)}</b>")
        elif kind == "italic":
            parts.append(f"<i>{_render_inline(node['content'], page_href, in_link)}</i>")
        elif kind == "emphasis":
            css = "emphasis-sesame" if node["style"] == "sesame" else "emphasis"
            parts.append(f'<span class="{css}">{_render_inline(node["content"], page_href, in_link)}</span>')
    return "".join(parts)


def _escape(value: str) -> str:
    return html.escape(value, quote=True)
