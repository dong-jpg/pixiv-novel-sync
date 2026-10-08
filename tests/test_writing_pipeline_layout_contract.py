"""Offline markup contracts only, NOT actual mobile/browser acceptance.

These check responsive classes on the real modal's structural nodes. They do
not load Tailwind/Vue, compute pixel geometry, or validate touch/scroll behavior.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest


@dataclass
class Element:
    tag: str
    attrs: dict[str, str | None]
    children: list[Element] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())


class ModalParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.root = Element("root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        element = Element(tag, dict(attrs))
        self.stack[-1].children.append(element)
        if tag not in {"input", "br", "hr", "img", "meta", "link"}:
            self.stack.append(element)

    def handle_endtag(self, tag):
        assert self.stack[-1].tag == tag, f"unbalanced modal tag: {tag}"
        self.stack.pop()


@pytest.fixture
def modal():
    root = Path(__file__).resolve().parents[1]
    templates = root / "src/pixiv_novel_sync/templates"
    page = (templates / "dashboard_ai_chapters.html").read_text(encoding="utf-8")
    assert "{% include 'dashboard_ai_pipeline_modal.html' %}" in page
    source = (templates / "dashboard_ai_pipeline_modal.html").read_text(encoding="utf-8")
    parser = ModalParser()
    parser.feed(re.sub(r"\{#[\s\S]*?#\}", "", source))
    parser.close()
    assert len(parser.stack) == 1
    assert len(parser.root.children) == 1
    overlay = parser.root.children[0]
    assert overlay.attrs.get("v-if") == "showPipelineModal"
    return overlay


def test_pipeline_mobile_single_column_static_contract(modal):
    shell, = modal.children
    header, body = shell.children
    controls, output = body.children
    assert {"fixed", "inset-0", "p-2", "sm:p-6"} <= modal.classes
    assert {"w-full", "max-w-6xl", "flex", "flex-col"} <= shell.classes
    assert {"grid", "grid-cols-1", "lg:grid-cols-12"} <= body.classes
    assert {c for c in body.classes if c.startswith("grid-cols-")} == {"grid-cols-1"}
    assert "lg:col-span-4" in controls.classes
    assert "lg:col-span-8" in output.classes
    for panel in (controls, output):
        assert not any(c.startswith("col-span-") for c in panel.classes), (
            "desktop spans must not create implicit columns on mobile"
        )
    assert header not in body.children  # header stays outside the scrolling grid


def test_pipeline_scroll_regions_static_contract(modal):
    shell, = modal.children
    header, body = shell.children
    controls, output = body.children
    assert "max-height: calc(100vh - 1rem)" in shell.attrs["style"]
    assert "overflow-hidden" in shell.classes
    assert "flex-shrink-0" in header.classes
    assert {"flex-1", "min-h-0", "overflow-y-auto", "lg:overflow-hidden"} <= body.classes
    assert not {"overflow-hidden", "overflow-y-hidden", "overflow-clip"} & body.classes
    assert "overflow-y-auto" in controls.classes
    assert {"flex", "flex-col", "overflow-hidden"} <= output.classes
    pre, = [child for child in output.children if child.tag == "pre"]
    assert pre.attrs.get("ref") == "pipelineOutputRef"
    assert {"flex-1", "overflow-y-auto", "whitespace-pre-wrap", "max-h-[calc(100vh-12rem)]"} <= pre.classes
    footer = output.children[-1]
    assert {"flex-shrink-0", "flex-wrap"} <= footer.classes
