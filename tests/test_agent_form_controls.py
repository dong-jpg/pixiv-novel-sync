from __future__ import annotations

from pathlib import Path


def test_agent_form_exposes_budget_controls_without_overwriting_zero() -> None:
    text = Path('src/pixiv_novel_sync/templates/dashboard_settings_agents.html').read_text(encoding='utf-8')
    assert 'v-model.number="agentForm.context_window"' in text
    assert 'v-model.number="agentForm.top_p"' in text
    assert 'temperature: agent.temperature ?? 0.8' in text
    assert 'top_p: agent.top_p ?? 0.9' in text


def test_agent_task_options_respect_branch_features() -> None:
    text = Path('src/pixiv_novel_sync/templates/dashboard_settings_agents.html').read_text(encoding='utf-8')
    writing = Path('src/pixiv_novel_sync/ai/services/projects.py').exists()
    for value in ('extract_summary', 'resolve_foreshadow', 'polish_dialogue', 'polish_psychology'):
        assert (f'<option value="{value}">' in text) == writing
