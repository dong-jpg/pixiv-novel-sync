# Repository Guidelines

## Project Layout

Under `src/pixiv_novel_sync/`, `cli.py` defines commands, `webapp.py` creates the Flask app, `jobs/` runs tasks, `sync/` handles discovery and checkpoints, and `storage/` manages SQLite. The UI ships in `templates/` and `static/`. Root-level `assets/` holds logos; `userscripts/` holds the Pixiv rescue script.

## Local Development

Use a Python 3.10+ virtual environment. On first setup, copy `.env.example` to `.env` and `config/config.yaml.example` to `config/config.yaml`. Never commit credentials, databases, or private archives.

- `python -m pip install -e ".[test]"` installs the editable package and pytest.
- `pixiv-novel-sync web-token-ui` serves `http://127.0.0.1:5010/dashboard`.
- `python -m pip wheel --no-deps . -w dist` builds a wheel.
- `python scripts/build_tailwind_css.py` rebuilds committed CSS after template class changes; `--check` verifies it.

## Coding Style

Use four-space Python indentation, `snake_case` functions and modules, and `PascalCase` classes. Keep UI strings in Chinese. Jinja uses `{[ value ]}`; Vue uses `{{ value }}`. No formatter is configured; `python -m compileall -q src` checks syntax.

## Testing

Put `test_<behavior>` functions in `tests/test_<feature>.py`. `tests/conftest.py` isolates database and archive paths. Add a regression case for each fix; no coverage threshold is configured. Run `python -m pytest tests/test_jobs_runner.py` for runner changes, then `python -m pytest -q` for the suite.

## Commits and Pull Requests

Use `type: subject` in English or Chinese, e.g., `fix: preserve bookmark visibility`. Target `main` for shared changes and `ai-writing` for writing-only features. Describe the behavior change, link relevant issues, report test results, and attach screenshots for visible UI changes.

<!-- fastctx:begin -->
## Local file inspection

For reading, searching, and finding local files, prefer the FastCtx MCP
server's own tools — `inspect_local_file`, `grep`, and `glob` — over shell
equivalents such as `cat`/`Get-Content`, `rg`/`findstr`/`Select-String`,
and `dir`/`ls -R`.
Use FastCtx file tools directly for local-file operations, including when a
local reference is URI-shaped; pass the equivalent plain absolute filesystem path.
Read only what the task needs. When you need several files, pass them to
one `inspect_local_file` call as files=[{"path": ...}, ...] instead of one
call per file. The last line of every result says `Complete` or
`Partial` — continue only with the exact parameters a `Partial` note
provides.

### Batch replacement

Use FastCtx's `replace` for mechanical find-and-replace across files.
It preserves each file's encoding and line endings, supports dry-run previews,
and rejects concurrent changes before writing. Use apply_patch for generated
content, semantic rewrites, or small local edits.
<!-- fastctx:end -->
