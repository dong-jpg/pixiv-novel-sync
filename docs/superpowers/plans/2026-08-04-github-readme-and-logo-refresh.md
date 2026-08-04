# GitHub README And Logo Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Refresh the GitHub project introduction page and static Logo so the repository presents Pixiv Novel Sync as a polished local archive, writing studio, and discovery tool.

**Architecture:** Keep the implementation static and GitHub-native. SVG assets live under assets/, while README.md becomes the concise public entry point that links to deeper docs instead of duplicating every requirement.

**Tech Stack:** Markdown, static SVG, shields.io badges, existing Python project commands.

## Global Constraints

- Use the confirmed Writer's Desk visual direction: warm, restrained, editorial, static.
- Do not add animation, remote hero images, frontend build tools, or dynamic README dependencies.
- Keep README commands aligned with current tooling: pytest and compileall are valid; Black, Flake8, Pylint, and mypy are not configured mandatory checks.
- Keep sensitive configuration guidance explicit: .env, DASHBOARD_TOKEN, and PIXIV_NOVEL_SYNC_AI_SECRET_KEY.
- Verify with git diff --check and manual Markdown/SVG review.

---

### Task 1: Static Logo Assets

**Files:**
- Modify: assets/logo.svg
- Create: assets/logo-mark.svg
- Modify: assets/logo-design.md

**Interfaces:**
- Consumes: confirmed design spec docs/superpowers/specs/2026-08-04-github-readme-and-logo-refresh-design.md
- Produces: GitHub-renderable static SVG files referenced by README.md

- [ ] **Step 1: Replace assets/logo-mark.svg with a compact static mark**

Use a 128x128 viewBox. Draw a rounded warm background, a folded document, text lines, and a brick-red editing pen mark. Use these colors exactly:

~~~text
background #FFF7EF
paper #FFFFFF
ink #2A211B
primary #B75A3C
accent #4A7C74
shadow #E8D4C4
~~~

- [ ] **Step 2: Replace assets/logo.svg with the README hero Logo**

Use a 520x180 viewBox. Reuse the same mark geometry at the left, add text converted to SVG text nodes using generic system fonts, and include:

~~~text
Pixiv Novel Sync
从收藏，到灵感，再到下一章。
Archive · Create · Discover
~~~

Keep it static and self-contained. Do not reference external fonts, images, CSS files, scripts, or animation.

- [ ] **Step 3: Rewrite assets/logo-design.md**

Document the final Writer's Desk concept, color palette, asset inventory, usage rules, and non-goals. Remove the old blue-purple-pink prompt and generator-tool suggestions so the file no longer points future contributors back to the rejected direction.

- [ ] **Step 4: Verify SVG assets**

Run:

~~~bash
python - <<'PY'
from pathlib import Path
import xml.etree.ElementTree as ET
for name in ["assets/logo.svg", "assets/logo-mark.svg"]:
    ET.parse(Path(name))
    text = Path(name).read_text(encoding="utf-8")
    assert "<script" not in text.lower()
    assert "http://" not in text.lower()
    assert "https://" not in text.lower()
print("svg ok")
PY
~~~

Expected: svg ok

### Task 2: README Public Entry Refresh

**Files:**
- Modify: README.md

**Interfaces:**
- Consumes: assets/logo.svg, docs/INDEX.md, docs/UNIFIED_PROJECT_REQUIREMENTS.md, docs/frontend-api-contract.md
- Produces: concise GitHub introduction page with stable anchors

- [ ] **Step 1: Rewrite the Hero section**

Use centered HTML for the Logo, project name, positioning copy, badges, and three links:

~~~text
Features
Quick Start
Docs
~~~

The visible tagline must be:

~~~text
从收藏，到灵感，再到下一章。
~~~

- [ ] **Step 2: Replace the feature wall with three capability columns**

Create a ## Features section with a three-column HTML table:

~~~text
Library: 收藏/关注/追更同步、本地全文库、EPUB 导出、救援阅读
Writing Studio: 项目、长篇规划、章节 Pipeline、风格/小说蒸馏
Discovery: 偏好画像、搜索计划、推荐打分、反馈与屏蔽
~~~

- [ ] **Step 3: Move Quick Start before detailed workflows**

Keep commands for virtualenv, editable install, config copying, and local launch. Include Windows PowerShell activation because the project is often used on Windows.

- [ ] **Step 4: Add concise Core Workflows**

Add short subsections for:

~~~text
同步归档
AI 创作
智能推荐
Pixiv 原站救援阅读
~~~

Each subsection should point to the dashboard route or file the user needs.

- [ ] **Step 5: Condense configuration, development, docs, and license**

Keep DASHBOARD_TOKEN and PIXIV_NOVEL_SYNC_AI_SECRET_KEY guidance. Keep pytest and compileall commands. Link deeper docs through docs/INDEX.md, docs/UNIFIED_PROJECT_REQUIREMENTS.md, and docs/frontend-api-contract.md. Keep license and issue links short.

### Task 3: Verification And Commit

**Files:**
- Verify: README.md
- Verify: assets/logo.svg
- Verify: assets/logo-mark.svg
- Verify: assets/logo-design.md

**Interfaces:**
- Consumes: outputs of Task 1 and Task 2
- Produces: a clean implementation commit

- [ ] **Step 1: Run SVG parse/security check**

Run the SVG check from Task 1 Step 4.

- [ ] **Step 2: Run Markdown/link sanity checks**

Run:

~~~bash
python - <<'PY'
from pathlib import Path
readme = Path("README.md").read_text(encoding="utf-8")
required = [
    "assets/logo.svg",
    "# Features",
    "# Quick Start",
    "# Docs",
    "DASHBOARD_TOKEN",
    "PIXIV_NOVEL_SYNC_AI_SECRET_KEY",
    "python -m pytest -q",
    "python -m compileall -q src",
]
missing = [item for item in required if item not in readme]
assert not missing, missing
print("readme ok")
PY
~~~

Expected: readme ok

- [ ] **Step 3: Run whitespace check**

Run:

~~~bash
git diff --check
~~~

Expected: exit code 0.

- [ ] **Step 4: Commit implementation**

Run:

~~~bash
git add README.md assets/logo.svg assets/logo-mark.svg assets/logo-design.md docs/superpowers/plans/2026-08-04-github-readme-and-logo-refresh.md
git commit -m "docs: refresh GitHub README and logo"
~~~
