# main README & Logo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 main 交付与写作分支区分的私人书库图标和中文 README。

**Architecture:** 新增独立静态 SVG，README 用真实文本与单栏布局引用它。控制器完成图标，README 委派给独立写集的实现者；不修改运行时、原有品牌和 ai-writing。完成后统一验证与复核，Git 写操作仅由控制器执行。

**Tech Stack:** Markdown、SVG/XML、Python 3.10+、pytest；预览仅用本机静态页面。

## Global Constraints

- 设计依据：[已批准规格](../specs/2026-10-09-main-readme-branding-design.md)。
- 工作目录：`C:/Users/dong/.codex/worktrees/main-remediation-verification/pixiv-novel-sync`，分支 main，起点 `88e3e3d`。
- 不编辑 `D:/gitcode/pixiv-novel-sync`（ai-writing），不替换旧 `assets/logo*`。
- 不编辑既有未提交文档，不更改业务代码、网页 favicon、GitHub 设置或服务器。
- 快速开始必须使用 `pixiv-novel-sync web-token-ui`、`127.0.0.1:5010`、单数 task key `bookmark`。
- Python >=3.10；Flask + SQLite；AI 配置可选；不得承诺完整离线网页或恢复从未归档的作品。
- 实现者不 stage / commit / push；最终限定文件本地提交，不推送、不部署。

## Task 1: main 专用 SVG（控制器）

**Files:**
- Create: `assets/main-logo.svg`
- Test: `tests/test_main_logo.py`

**Interfaces:**
- Consumes: 已批准的「打开的书＋同步环」视觉方向。
- Produces: `assets/main-logo.svg`，128 × 128 方形 viewBox、自包含静态图标，供 README 用 112px 图标展示。

- [x] **Step 1: 添加并执行失败测试。** 最小存在性与可访问性断言：

  ```python
  from pathlib import Path
  from xml.etree import ElementTree as ET

  ROOT = Path(__file__).resolve().parents[1]
  SVG = ROOT / "assets" / "main-logo.svg"

  def test_main_logo_has_accessible_square_canvas():
      assert SVG.is_file(), "main needs an independent logo"
      root = ET.fromstring(SVG.read_text(encoding="utf-8"))
      assert root.attrib["viewBox"] == "0 0 128 128"
      assert root.attrib["role"] == "img"
      ids = {node.attrib.get("id") for node in root.iter()}
      assert root.attrib["aria-labelledby"].split()
      assert all(key in ids for key in root.attrib["aria-labelledby"].split())
  ```

  同文件补充静态元素/属性白名单、无脚本/外链/字体依赖的检查；不锁死路径几何。
  Run: `python -m pytest tests/test_main_logo.py -q`。Expected: 缺少 main 专用图标的断言失败。
- [x] **Step 2: 实现图标。** 以墨蓝圆角底板、浅色打开的书、青蓝两段同步箭头组成图标；只用 SVG 基础形状和路径，加入中文描述。不嵌入标题文字；不动旧 SVG。
- [x] **Step 3: 转绿并目视检查。** Run: `python -m pytest tests/test_main_logo.py -q`。Expected: 全通过。预览 32 / 64 / 112px，检查书脊和箭头不粘连、明暗背景可辨认。

## Task 2: main README（独立实现者）

**Files:**
- Modify: `README.md`
- Test: `tests/test_main_readme_branding.py`

**Interfaces:**
- Consumes: `assets/main-logo.svg`；此文件由控制器创建，不由 README 实现者写入。
- Produces: 中文单栏 README；main 与 ai-writing 区分清晰；相对链接指向已有文件。
- Report: `.superpowers/main-readme-branding-2026-10-09/task-2-report.md`。

- [x] **Step 1: 添加并执行失败测试。** README 首屏须引用新的小图标和标语、链接写作分支且不再引用旧 logo / 三栏表格。用 HTMLParser 检查图片属性，不锁死空格/引号。最小内容契约：

  ```python
  from pathlib import Path

  ROOT = Path(__file__).resolve().parents[1]

  def test_readme_has_main_identity_and_writing_branch_link():
      text = (ROOT / "README.md").read_text(encoding="utf-8")
      assert "assets/main-logo.svg" in text
      assert "把喜欢的小说，留在自己的书库里。" in text
      assert "https://github.com/dong-jpg/pixiv-novel-sync/tree/ai-writing" in text
      assert "assets/logo.svg" not in text
      assert "<table" not in text.lower()
  ```

  同文件加入相对资源路径、明确选择 main 的安装命令与关键安全/能力边界检查；不要修改旧测试降低要求。
  Run: `python -m pytest tests/test_main_readme_branding.py -q`。Expected: 旧 README 不满足新 main 品牌契约。
- [x] **Step 2: 重写 README。** 按规格顺序提供定位、分支选择、同步/归档/移动阅读/推荐/救援、快速开始、部署安全、可选 AI、文档开发、MIT 和问题反馈。首图写成：

  ```html
  <p align="center">
    <img src="assets/main-logo.svg" width="112" height="112" alt="Pixiv Novel Sync：书页与同步环图标">
  </p>
  ```

  标题/标语是文本；正文不使用横向表格；短段落优先。完整保留两份现有测试的文档契约：
  - `tests/test_ai_model_docs.py`：模型目录、模型池、`/api/dashboard/ai/providers/<provider_id>/models/sync`、16 个候选、32 次网络请求、30 分钟。
  - `tests/test_frontend_library_os.py`：保留 14 天、`task_log_retention_days`、`/dashboard/novels?category=rescue`、`userscripts/pixiv-rescue.user.js`。
  AI 内容移入可选进阶说明；不宣称未实现的 AI 推荐解释/总结。救援依赖本地备份，用户脚本需支持扩展的浏览器。公网 HTTPS、Dashboard Token、稳定签名密钥、Secure cookie、私人设备可选30天登录和敏感数据备份必须说明。
- [x] **Step 3: 转绿与自查。** Run: `python -m pytest tests/test_main_readme_branding.py tests/test_ai_model_docs.py tests/test_frontend_library_os.py -q`。Expected: 全通过（图标存在性依赖 Task 1）。报告 RED / GREEN 命令及输出、修改文件、发现的问题。不要写 Git index。

## Task 3: 集成验证与本地交付（控制器）

**Files:**
- Update: 本计划完成状态。
- Scratch only: `.superpowers/main-readme-branding-2026-10-09/` 中的报告、预览和指纹。

- [x] **Step 1: 检查链接、边界与所有相关测试。**

  ```powershell
  python -m pytest tests/test_main_logo.py tests/test_main_readme_branding.py tests/test_ai_model_docs.py tests/test_frontend_library_os.py -q
  git diff --check
  git diff --stat
  ```

  用 XML 解析验证图标；检查 README 本地路径、手写锚点和 GitHub 跳转；不启动真实服务或读取 `.env`。
- [x] **Step 2: 静态渲染。** 仅在 loopback 提供本任务 README 和 SVG；使用浏览器检查桌面、390px 手机与明暗背景，记录截图；预览不宣称是 GitHub 实际线上页面。
- [x] **Step 3: 独立规格与质量审查。** 给审查者本任务完整 diff 包和已验证结果；处理所有重要问题，再复核。审查与静态渲染可并行，不重复实现者的测试。
- [x] **Step 4: 限定文件本地提交。** 核对 ai-writing 和原有修改的 SHA-256 指纹，确认修改范围仅上述文件；只 stage 本次文件，检查 staged diff，提交后报告短 SHA。不 push、不部署。

## 验证记录

- 起点：`tests/test_ai_model_docs.py` + `tests/test_frontend_library_os.py`，40 passed。
- SVG RED：缺少新图标的 2 项预期失败；GREEN：2 passed。
- README RED：13 failed / 1 passed（原有单数 bookmark 命令已正确）；GREEN：54 passed（含既有文档契约）。
- 集成：上述四个测试文件在复核补强后合计 60 passed；18 项本地链接/锚点有效；18 个非本任务文件指纹不变，ai-writing 的分支、HEAD、工作区状态不变。
- 本地排版校样：桌面、390px 明暗背景；补查 360px / 430px，无整页横向溢出，长命令在代码块内滚动，导航锚点可达。不是线上 GitHub 或安卓真机验收。
- 独立复核发现的 P3 测试盲区已补强：以 PI 解析事件拒绝 SVG 根节点前、内部、之后的样式表处理指令，并保留普通 XML 声明支持；先见 3 个预期失败，再转绿。图标本身从未引用外部资源。
- 最终独立复核：规格与质量均通过，P3 已关闭，无剩余审查事项。本次仅本地提交，不推送、不部署。
- 临时浏览器标签和 loopback 预览已关闭，视口已恢复；详细报告和截图仅保留在忽略的 scratch 目录。
