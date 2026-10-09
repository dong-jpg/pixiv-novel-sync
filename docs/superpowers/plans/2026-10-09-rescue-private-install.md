# Rescue Personal Installation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为维护者提供可直接安装、可由支持的管理器检查更新的救援脚本，以及安全的手机安装说明。

**Architecture:** 仅补充现有单文件用户脚本的元信息，原 JavaScript 运行时保持不变。GitHub main 的原始文件作为唯一安装和更新源；README 与救援指南共享该链接，不部署新服务或增加分发平台。

**Tech Stack:** 用户脚本元信息、Markdown、Python/pytest、Node 语法检查。

## Global Constraints

- 工作在现有 main worktree；不动 ai-writing、既有未提交服务器文档和旧品牌资源。
- 不公开/读取真实 Token，不轮换凭据、不操作服务器、不上架 Greasy Fork、不修改仓库权限。
- 保留脚本身份、默认服务地址、匹配规则、GM 权限和运行时正文；版本为 `0.1.1`。
- 不保证所有安卓 Chromium 浏览器均支持 GM API 或自动更新；未进行真实手机安装时不得声称已安装。
- 现有 Playwright UI 测试不经 shell 执行；本轮只执行静态契约、Node 语法与正文一致性检查。

## Task 1: 安装与更新元信息

**Files:** `userscripts/pixiv-rescue.user.js`、`tests/test_rescue_userscript_install.py`。

- [x] 添加以下失败契约并执行 `python -m pytest tests/test_rescue_userscript_install.py -q`：新版本、相同的 HTTPS 安装/更新 URL、稳定身份与最小权限、README/指南安装入口及升级隐私提示。预期旧脚本缺少更新元信息、旧文档缺少直装说明而失败。

  ```python
  INSTALL_URL = "https://raw.githubusercontent.com/dong-jpg/pixiv-novel-sync/main/userscripts/pixiv-rescue.user.js"
  assert metadata["updateURL"] == [INSTALL_URL]
  assert metadata["downloadURL"] == [INSTALL_URL]
  assert metadata["version"] == ["0.1.1"]
  ```

- [x] 将版本升级为 `0.1.1`，新增以下元信息，不改已有 name/namespace/grant/match/connect 和任何运行时函数：

  ```javascript
  // @author       dong-jpg
  // @license      MIT
  // @homepageURL  https://github.com/dong-jpg/pixiv-novel-sync/tree/main
  // @supportURL   https://github.com/dong-jpg/pixiv-novel-sync/issues
  // @downloadURL  https://raw.githubusercontent.com/dong-jpg/pixiv-novel-sync/main/userscripts/pixiv-rescue.user.js
  // @updateURL    https://raw.githubusercontent.com/dong-jpg/pixiv-novel-sync/main/userscripts/pixiv-rescue.user.js
  ```

## Task 2: 手机直装说明与安全边界

**Files:** `README.md`、`docs/RESCUE_USER_GUIDE.md`。

- [x] README 救援小节增加“直接安装用户脚本”链接，保留本地源码和完整指南链接。
- [x] 将救援指南第 2 节改为先直装，再 URL 导入/源码导入兜底；说明服务器已预设，不要为安装修改 namespace；更正过时的 `API_ORIGIN` 名称。
- [x] 说明已有 Token 直接复用、不要先卸载、轮换使所有旧 Token 失效，以及自动更新取决于管理器支持和设置。注明公开代码不包含私人书库与凭据。

## Task 3: 验证与交付

- [x] Run: `python -m pytest tests/test_rescue_userscript_install.py tests/test_rescue_userscript.py tests/test_main_readme_branding.py tests/test_frontend_library_os.py tests/test_ai_model_docs.py -k "not fixture" -q`。Expected: 相关契约全部通过，六个现有浏览器夹具用例明确不运行。
- [x] Run: `node --check userscripts/pixiv-rescue.user.js`；比对 `// ==/UserScript==` 之后的正文与基线 SHA-256，确认字节一致。
- [x] 检查 Markdown 链接、git diff 和原有文件指纹；仅将本任务文件提交到 main。
- [x] 为已选择的 GitHub 直装交付推送 main；独立核对远端 SHA 和 raw 文件版本/内容，不改其他分支或平台。
- [x] 返回手机可打开的安装链接和最短配置步骤，明确仍需维护者在设备上确认安装并填写 Token。

## 验证记录与边界

- 新安装契约先出现 5 项预期失败；实现后相关静态/文档测试 62 passed，6 项现有浏览器夹具用例明确未执行。
- `node --check` 通过；元信息之后的运行时正文与基线逐字节一致，未更换 Token 键、服务地址或权限。
- README 相对链接/锚点检查和原有文件、ai-writing 保留检查通过。
- 独立规格与质量复核通过，无待修正项；脚本安装确认、实际 GM API 兼容性、自动更新和存储保留仍需在维护者手机上确认，不能等同于静态检查。
- 分发只更新现有 GitHub main 安装源，不上架脚本平台，不生成/轮换 Token，不操作服务器。在线文件与提交内容的一致性校验记录保存在本任务的本地工作记录中。
