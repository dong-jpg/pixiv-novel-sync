"""EPUB导出功能模块"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from ebooklib import epub


def create_epub_from_novel(novel_data: dict[str, Any], text_content: str, cover_path: Path | None = None) -> bytes:
    """从小说数据创建EPUB字节流"""
    book = epub.EpubBook()

    # 基础元数据
    novel_id = novel_data["novel_id"]
    title = novel_data.get("title", f"Novel {novel_id}")
    author = novel_data.get("author_name", "Unknown")

    book.set_identifier(f"pixiv-novel-{novel_id}")
    book.set_title(title)
    book.set_language("ja")
    book.add_author(author)

    # 添加封面
    if cover_path and cover_path.exists():
        with open(cover_path, "rb") as f:
            cover_data = f.read()
        book.set_cover("cover.jpg", cover_data)

    # 创建章节
    chapter = epub.EpubHtml(title="Chapter 1", file_name="chap_01.xhtml", lang="ja")

    # 转换文本为HTML段落
    paragraphs = text_content.strip().split("\n")
    html_content = "<h1>" + title + "</h1>\n"
    html_content += "\n".join(f"<p>{p}</p>" if p.strip() else "<br/>" for p in paragraphs)

    chapter.set_content(html_content)
    book.add_item(chapter)

    # 目录
    book.toc = (chapter,)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # 脊柱
    book.spine = ["nav", chapter]

    # 生成EPUB
    output = io.BytesIO()
    epub.write_epub(output, book)
    return output.getvalue()
