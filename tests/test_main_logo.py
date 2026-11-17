"""The README logo must stay portable, accessible and safe to render on GitHub."""

from io import StringIO
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest


LOGO = Path(__file__).resolve().parents[1] / "assets" / "main-logo.svg"
NS = "{http://www.w3.org/2000/svg}"


def read_logo() -> ET.Element:
    assert LOGO.is_file(), "main needs an independent logo"
    text = LOGO.read_text(encoding="utf-8")
    assert "<!DOCTYPE" not in text.upper()
    assert "<!ENTITY" not in text.upper()
    # fromstring() silently discards processing instructions, including an
    # external xml-stylesheet in the prolog or after the root element.
    parser = ET.iterparse(StringIO(text), events=("pi",))
    assert not list(parser), "SVG processing instructions are not allowed"
    return parser.root


def test_main_logo_has_accessible_square_canvas() -> None:
    root = read_logo()
    assert root.tag == f"{NS}svg"
    assert root.attrib["viewBox"] == "0 0 128 128"
    assert root.attrib["role"] == "img"

    ids = [node.attrib["id"] for node in root.iter() if "id" in node.attrib]
    assert len(ids) == len(set(ids)), "accessible labels need unique IDs"
    labels = root.attrib["aria-labelledby"].split()
    title = root.find(f"{NS}title")
    description = root.find(f"{NS}desc")
    for node in (title, description):
        assert node is not None
        assert node.attrib["id"] in labels
        assert node.text and node.text.strip()
    assert set(labels) <= set(ids)
    assert "Pixiv Novel Sync" in title.text


def test_main_logo_is_static_self_contained_vector_without_font_dependencies() -> None:
    root = read_logo()
    # A drawing-only subset: no script, animation, text/font, image, links,
    # foreignObject, stylesheets or externally resolved resources.
    shapes = {"path", "rect", "circle", "ellipse", "line", "polygon", "polyline"}
    tags = {f"{NS}{tag}" for tag in shapes | {"svg", "title", "desc", "g"}}
    attrs = {
        "id", "viewBox", "width", "height", "role", "aria-labelledby",
        "fill", "fill-rule", "stroke", "stroke-width", "stroke-linecap",
        "stroke-linejoin", "opacity", "transform", "d", "x", "y", "rx", "ry",
        "cx", "cy", "r", "x1", "y1", "x2", "y2", "points",
    }
    for node in root.iter():
        assert node.tag in tags
        assert set(node.attrib) <= attrs
        for value in node.attrib.values():
            assert "url(" not in value.lower()
    assert any(node.tag in {f"{NS}{shape}" for shape in shapes} for node in root.iter())


def test_main_logo_has_a_light_background_and_contrasting_symbols() -> None:
    def luminance(color: str) -> float:
        channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            for value in channels
        ]
        return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    root = read_logo()
    background = root.find(f"{NS}rect")
    assert background is not None
    background_luminance = luminance(background.attrib["fill"])
    assert background_luminance >= 0.65, "Keep the requested light background"
    colors = {
        value for node in root.iter(f"{NS}path")
        for key, value in node.attrib.items()
        if key in {"fill", "stroke"} and value.startswith("#")
    }
    assert colors
    for color in colors:
        light, dark = sorted((background_luminance, luminance(color)), reverse=True)
        assert (light + 0.05) / (dark + 0.05) >= 3, f"Low-contrast symbol: {color}"


@pytest.mark.parametrize("placement", ("before", "inside", "after"))
def test_main_logo_rejects_stylesheet_processing_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, placement: str
) -> None:
    text = LOGO.read_text(encoding="utf-8")
    instruction = '<?xml-stylesheet type="text/css" href="https://example.invalid/logo.css"?>'
    if placement == "before":
        text = instruction + "\n" + text
    elif placement == "inside":
        text = text.replace("</svg>", instruction + "\n</svg>")
    else:
        text += "\n" + instruction
    candidate = tmp_path / "external-stylesheet.svg"
    candidate.write_text(text, encoding="utf-8")
    monkeypatch.setitem(globals(), "LOGO", candidate)

    with pytest.raises(AssertionError, match="processing instructions"):
        read_logo()


def test_main_logo_accepts_an_xml_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = '<?xml version="1.0" encoding="UTF-8"?>\n' + LOGO.read_text(encoding="utf-8")
    candidate = tmp_path / "declared.svg"
    candidate.write_text(text, encoding="utf-8")
    monkeypatch.setitem(globals(), "LOGO", candidate)

    assert read_logo().tag == f"{NS}svg"
