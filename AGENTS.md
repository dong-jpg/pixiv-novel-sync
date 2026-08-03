# Repository Guidelines

## Project Structure & Module Organization

Application code lives in `src/pixiv_novel_sync/`. `cli.py` defines the command; `webapp.py` builds the Flask application. Domain code is grouped under `ai/`, `jobs/`, `storage/`, and `web/`; server-rendered Vue templates are in `templates/`. Tests live in `tests/` and follow the source feature they exercise. Operational files are separated into `config/`, `deploy/`, and `scripts/`; browser integration is in `userscripts/`, shared visuals in `assets/`, and active documentation in `docs/`.

## Build, Test, and Development Commands

- `python -m venv .venv`: create a local virtual environment (Python 3.10+).
- `pip install -e ".[test]"`: install the package in editable mode with pytest.
- `pixiv-novel-sync web-token-ui`: start the Flask UI at `http://127.0.0.1:5010`.
- `pixiv-novel-sync sync bookmark following_novels subscribed_series`: run the main sync sources manually.
- `python -m pytest -q`: run the complete test suite.
- `python -m pytest tests/test_preferences.py -q`: run one focused test module.

## Coding Style & Naming Conventions

Use four-space indentation and standard Python naming: `snake_case` for modules, functions, and variables; `PascalCase` for classes; and uppercase names for constants. New Python modules should use `from __future__ import annotations`; prefer `@dataclass(slots=True)` where the surrounding model code does. Keep imports deferred in CLI and task dispatch paths when that preserves startup speed. Match nearby Chinese comments and user-facing strings. No formatter, linter, or type checker is currently configured in `pyproject.toml`, so do not treat README examples for Black, Flake8, Pylint, or mypy as required checks.

## Testing Guidelines

Pytest discovers `tests/test_*.py`; name test functions `test_<behavior>`. Add focused regression coverage with every behavior change, especially for storage migrations, route security, job cancellation, and userscript/API compatibility. The autouse fixtures in `tests/conftest.py` redirect database and library paths to temporary directories; tests must never use real `data/` or secrets. There is no enforced coverage threshold.

## Commit & Pull Request Guidelines

Recent history follows Conventional Commit prefixes such as `feat:`, `fix:`, `docs:`, `refactor:`, and `chore:`. Keep the subject concise and imperative; either English or Chinese is consistent with existing history. Pull requests should explain the user-visible effect, list verification commands, link relevant issues, and call out schema or configuration changes. Include screenshots for changes under `templates/` and note any compatibility impact on `userscripts/pixiv-rescue.user.js`.

## Security & Configuration

Copy `.env.example` and `config/config.yaml.example` for local setup. Never commit `.env`, generated databases, logs, or `data/`. Keep `PIXIV_NOVEL_SYNC_AI_SECRET_KEY` stable once provider keys are stored, and set `DASHBOARD_TOKEN` before exposing the UI beyond localhost.
