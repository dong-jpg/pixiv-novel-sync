from __future__ import annotations

from pathlib import Path

from pixiv_novel_sync.ai.prompts import compose_style_control_prompt
from pixiv_novel_sync.ai.service import AIWritingService


def test_none_and_empty_return_none():
    assert compose_style_control_prompt(None) is None
    assert compose_style_control_prompt({}) is None
    assert compose_style_control_prompt({"sliders": {}, "tags": [], "custom": ""}) is None


def test_mid_range_sliders_do_not_inject():
    # 35-65 视为中庸，不注入任何指令
    assert compose_style_control_prompt({"sliders": {"explicitness": 50, "lyricism": 40, "pacing": 60}}) is None


def test_low_slider_uses_low_directive():
    out = compose_style_control_prompt({"sliders": {"explicitness": 10}})
    assert out is not None
    assert "点到为止" in out
    assert "情色露骨度" in out


def test_high_slider_uses_high_directive():
    out = compose_style_control_prompt({"sliders": {"explicitness": 90}})
    assert out is not None
    assert "直接露骨" in out


def test_tags_and_custom_appended():
    out = compose_style_control_prompt({"tags": ["NTR", "病娇"], "custom": "用第一人称写"})
    assert out is not None
    assert "NTR" in out and "病娇" in out
    assert "用第一人称写" in out


def test_invalid_slider_values_ignored():
    # 非数值滑块值应被跳过而非报错
    out = compose_style_control_prompt({"sliders": {"explicitness": "abc", "lyricism": None}, "tags": ["治愈"]})
    assert out is not None
    assert "治愈" in out
    # 无有效滑块指令，只有标签
    assert "情色露骨度" not in out


def test_multiple_sliders_combined():
    out = compose_style_control_prompt({"sliders": {"explicitness": 90, "darkness": 85, "vulgarity": 10}})
    assert out is not None
    assert "直接露骨" in out       # explicitness high
    assert "黑暗压抑" in out       # darkness high
    assert "保持书面语" in out     # vulgarity low


def test_project_style_control_prompt_reads_project_settings(tmp_path: Path):
    service = AIWritingService(tmp_path / "test.db")

    class FakeDB:
        @staticmethod
        def get_ai_writing_project(project_id: int):
            return {
                "id": project_id,
                "settings": {
                    "style_control": {
                        "sliders": {"explicitness": 90},
                        "tags": ["第一人称"],
                        "custom": "多用对话",
                    }
                },
            }

    prompt = service._project_style_control_prompt(FakeDB(), 7)

    assert prompt is not None
    assert "直接露骨" in prompt
    assert "第一人称" in prompt
    assert "多用对话" in prompt
