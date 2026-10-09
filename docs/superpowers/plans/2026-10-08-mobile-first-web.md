# Mobile-first Web Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按用户已确认方案，完成 main 的安卓手机优先界面和可选30天可撤销登录，保持桌面兼容。

**Architecture:** 保持 Flask/Jinja/Vue3 和现有API；新增轻量认证会话存储，页面共用移动组件，阅读/返回独立辅助模块。按互斥文件写集分两波实现，控制器串行暂存/提交与集成复核。

**Tech Stack:** Python >=3.10、Flask、SQLite、Vue3、Jinja `{[ ]}`、pytest、Node VM、CUA浏览器。

## Global Constraints

- 工作目录为 `C:/Users/dong/.codex/worktrees/main-remediation-verification/pixiv-novel-sync`，分支 `codex/mobile-first-main`，基线 `481f8a93b24abbcd9d9049da4da8ba023fe3c53b`。
- 用户已批准移动优先、桌面兼容、可选记住30天并要求开始实施；详细约束见 `docs/superpowers/specs/2026-10-08-mobile-first-web-design.md`。
- 不访问服务器、真实 .env、私钥、生产数据库或真实Provider；不推送、不部署、不恢复main中已删除的写作/成人模块。
- 保留既有未提交服务器文档；子代理只改授权文件，不运行git add/commit/reset/checkout/clean，由控制器负责提交。
- 变更请求使用 `window.csrfFetch`，错误使用既有 `window.errorText`；不能绕过认证、CSRF、限流或Referrer-Policy。
- 新功能/bug修复先写可失败的回归，记录RED/GREEN命令与结果；不要弱化已有断言来制造通过。
- 主要手机触控目标至少44px；登录输入16px、登录主控件48px；覆盖360/390/430px及桌面。
- 浏览器交互仅用CUA；Node VM是纯单元测试，不代表真实浏览器。测试用假数据，不调用真实写入/生成/轮换接口。

## 调度与共享接口

第一波 Task 1 与 Task 2 文件互斥；Task 2 通过复核后，Task 3/4/5 可按互斥文件集推进。控制器负责预览数据、浏览器集成、全量测试和文档；不得并发改同一文件或共用Git暂存操作。

- Task 2 提供 `base.html` 的 `{% block body_class %}{% endblock %}`；Task 4 设置 `pns-reader-page`，手机下隐藏全局 `.mobile-bottom-bar`。
- Task 2 在 `registerGlobalComponents(app)` 之后、`app.mount` 之前调用可选 `window.registerPageComponents(app)`；Task 3 在页面专属组件partial中定义此钩子。
- Task 2 的 `app-modal` 新增 `closeOnBackdrop`，默认true；Task 5 Token弹窗传false。
- Task 4 的返回/滚动辅助仅通过 `navigation_helpers.html` 在所负责页面引入，不改base。

## Task 1: 安全可撤销登录与手机登录页

**Write set:** `src/pixiv_novel_sync/webapp.py`、`storage_db.py`、`storage/schema.py`；新增 `storage/web_auth_sessions.py`、可选 `web/auth_sessions.py`、`templates/login.html`；新增 `tests/test_mobile_auth.py`，按需迁移 `tests/test_webapp_security.py` 的认证夹具。其他文件需要先向控制器说明。

**Interfaces:** 认证会话表 `web_auth_sessions`，至少包含 token_hash(TEXT主键)、credential_version(TEXT)、created_at(REAL)、expires_at(REAL)、persistent(INTEGER)。随机标识只放签名HttpOnly Cookie，数据库只存SHA-256；credential_version 是以Flask secret为key、带用途前缀的HMAC，不存可离线猜口令的普通摘要。存储操作使用现有事务/连接机制，Schema新增不删改业务表。

最小记录结构：

```sql
CREATE TABLE IF NOT EXISTS web_auth_sessions (
  token_hash TEXT PRIMARY KEY,
  credential_version TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  persistent INTEGER NOT NULL CHECK (persistent IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_web_auth_sessions_expiry ON web_auth_sessions(expires_at);
```

- [x] RED：新增真实Flask客户端测试，证明当前记住选项无效、退出后重放旧Cookie仍可认证或没有服务端撤销记录。执行 `python -m pytest -q tests/test_mobile_auth.py`，记录预期失败。
- [x] 实现未勾选浏览器会话/服务端7天、勾选固定30天。字段名 `remember_device`，表单勾选值 `1`；每次登录旋转随机标识并撤销本浏览器旧标识。普通请求不延长期限。
- [x] 登录旧格式Cookie需重新认证；退出撤销数据库记录，旧Cookie不能重放；其他设备会话保留。生效口令变化通过HMAC版本失效，服务重启后持久会话保留。
- [x] 验证存储异常返回503并保留Cookie，不把数据库故障当密码错误或静默放行。保留限流、UTF-8常量比较、CSRF、本机和救援认证边界。
- [x] 创建独立轻量login模板：viewport、48px控件、16px输入、可选记住、错误/429页内显示、允许浏览器密码管理器；绝不回显口令或写localStorage。成功后恢复安全站内next路径，拒绝外域、`//`、反斜杠、控制字符及自循环登录路径。
- [x] GREEN：覆盖30天与7天期限边界、重启、退出重放、独立设备、口令变更、伪造/缺失标识、储存故障、Cookie属性、无明文、中文口令、限流和CSRF；运行 `python -m pytest -q tests/test_mobile_auth.py tests/test_webapp_security.py`。
- [x] 报告源码/API改动与后续HTTPS部署需要 `PIXIV_COOKIE_SECURE=1` 的前提；不自行改服务器。

## Task 2: 共享移动框架、底栏与弹窗

**Write set:** `templates/base.html`、`templates/vue_components.html`；新增 `tests/test_mobile_shell.py`、`tests/test_mobile_shell_runtime.cjs`。不改各具体页面。

- [x] RED：对当前底栏缺失作者入口、小分页触控和遮罩关闭行为写回归。执行 `python -m pytest -q tests/test_mobile_shell.py`。
- [x] 保留Library OS设计，手机主要交互44px，表单16px/限宽、安全区和底部空间；禁止通过隐藏整页横滚掩盖不可达控件。宽表保留局部横滚。
- [x] 底栏最多5项：首页、书库、作者与系列、任务、更多。更多包含偏好/待确认/设置/退出，菜单打开/关闭及当前路径高亮可用，桌面侧栏保留。退出必须检查请求成功，失败不能假装退出。
- [x] 扩展body_class及registerPageComponents接口，保持原initVueApp兼容。读页body类在手机隐藏全局底栏。
- [x] app-modal新增closeOnBackdrop=true默认；焦点保持/返回、Escape关闭、背景滚动锁在关闭/卸载恢复；窄屏有限高度可滚动，底部操作不溢出。注意嵌套/并存弹窗不能提前解除背景锁。
- [x] GREEN：执行 `python -m pytest -q tests/test_mobile_shell.py tests/test_frontend_library_os.py` 和 `node tests/test_mobile_shell_runtime.cjs`。如既有测试定位旧实现，报告需要迁移的位置，由控制器协调，不随意删断言。

## Task 3: 手机优先推荐与分页过滤

**Write set:** `templates/dashboard.html`、`templates/dashboard_preferences.html`；新增 `templates/recommendation_components.html`（包含共享Vue卡片和局部样式）；新增 `tests/test_mobile_recommendations.py`、`tests/test_mobile_recommendations_runtime.cjs`。可更新与这两个页面直接相关的既有测试断言，但先通知控制器避免与Task2写冲突。

- [x] RED：覆盖隐藏已反馈后空白页、过期分页响应覆盖、重复反馈/网络失败，以及卡片结构。执行 `python -m pytest -q tests/test_mobile_recommendations.py`。
- [x] 新partial通过 `window.registerPageComponents(app)` 注册共享 `recommendation-card`；首页与偏好页都使用它。仅显示与派发事件，不在卡片里重复实现数据请求。
- [x] 手机单列，标题至少2行、理由可展开，标签不挤占主文；外部作品链接与反馈按钮分离，触控44px，反馈busy/失败反馈明确。保留Pixiv外链和noopener noreferrer。
- [x] 首页统计/任务控制压缩首屏但可访问；偏好页优先展示推荐结果，画像/搜索管理分区不堵在结果之前。
- [x] 用已有 `/api/dashboard/recommendations/items?page=...&page_size=10&status=new` 实现隐藏已反馈，未隐藏不传status。两页分页一致；过滤改变回第一页，响应代次保护；反馈后最后一页消失则回有效页。处理真实空态、加载态、错误重试，不修改推荐算法/旧无page数组接口。
- [x] GREEN：执行新增pytest/Node runtime，保留并更新既有首页/偏好行为测试，报告真实失败→成功证据与重复逻辑清理。

## Task 4: 阅读工具、站内返回和救援来源

**Write set:** `templates/dashboard_novel_detail.html`、`dashboard_novels.html`、`dashboard_series_detail.html`、`dashboard_user_detail.html`、`dashboard_follows.html`、`dashboard_users.html`（存在时）；新增 `templates/navigation_helpers.html`、`tests/test_mobile_reading.py`、`tests/test_mobile_reading_runtime.cjs`；按需更新 `tests/test_reader_actions_runtime.cjs` / `.py` 的夹具，保留原竞态断言。

- [x] RED：写可执行回归证明返回丢失上下文、外域from不应接受，及阅读中保存必须不移动位置。执行新增pytest/Node测试。
- [x] 独立partial提供 `safeDashboardReturn(value,fallback)` 与 `withDashboardReturn(target,from)`；仅允许合法站内dashboard路径，拒绝协议相对URL、反斜杠、控制字符。安全传递来源，不能依赖document.referrer，也不能放宽Referrer-Policy。
- [x] 显式保存/恢复列表滚动状态，按完整路径和查询隔离，仅保存UI状态（不存口令或正文）；恢复后清理监听，在加载等待中用户手动操作则不覆盖，正常浏览器返回与显式返回均保留筛选/页码。
- [x] 阅读页body_class为pns-reader-page；手机用独立工具栏提供当前位置保存、字号/章节导航，主导航不叠加。危险动作在次级菜单并保留已有确认、失败不丢数据/进度的逻辑。
- [x] 来源摘要可触屏展开查看完整来源，不依赖hover，不误触作品跳转。保证同页多个条目展开互不污染，桌面可用。
- [x] GREEN：执行 `python -m pytest -q tests/test_mobile_reading.py tests/test_reader_actions_runtime.py` 与两个Node runtime；保留原手动往返滚动/GET与保存竞态/重置失败等用例。

## Task 5: 手机管理表单与可靠反馈

**Write set:** `templates/dashboard_pending_deletions.html`、`dashboard_settings_models.html`、`dashboard_settings_agents.html`、`dashboard_settings_system.html`；新增 `tests/test_mobile_management.py`、`tests/test_mobile_management_runtime.cjs`。不改base、通用组件、后端路由或Token协议。

- [x] RED：500 JSON不能变空态、旧分页响应不能覆盖新选择、复制失败在弹窗内可见、长选项控件不溢出；用假数据/假Token编写runtime测试。
- [x] 待删除页检查HTTP/业务错误，保留筛选页码并显示原地重试；重试仅GET，不调用检测/删除；快速筛选的迟到响应丢弃。
- [x] 模型/模型池编辑在手机立即定位并聚焦正确表单；结果靠近操作区，错误不在4秒后消失；保留必要宽表的局部滚动，不全量重构模型路由功能。
- [x] Agent选择器与刷新在窄屏可堆叠且可收缩，长名称不使页面横向溢出。
- [x] Token弹窗传 `:close-on-backdrop="false"`，复制成功/失败在弹窗内，提供选中后手动复制提示；显式关闭仍清空明文，不能存localStorage，不能为了复制而触发新一轮Token签发。
- [x] GREEN：执行新增pytest/Node runtime及直接相关已有前端安全测试；不使用真实Token或发出真实变更。

## Task 6: 集成验收、复核与文档（控制器）

- [x] 保存各任务RED/GREEN报告和带上下文的差异包；独立复核规格与质量，关闭重要问题后再结束任务。
- [x] 使用临时配置、数据库和合成数据启动本地预览；关闭所有调度器并禁止真实Provider调用。浏览器仅经CUA操作。
- [x] 360/390/430px、横屏和桌面检查推荐、导航、阅读、设置、错误/复制/更多菜单；用DOM尺寸确认无非预期整页横滚，用实际事件确认行为，不把CSS字符串测试当视觉验收。
- [x] 执行 `python -m pytest -q -ra`、`python -m compileall -q src tests`、`python -m pyflakes src tests`、模板JS语法检查和 `git diff --check`。
- [x] 更新手机改造完成报告、活跃前端契约/页面指南与索引，准确保留真机软键盘/系统返回键和部署边界。不得把服务器旧审计文档当新改造提交。
- [x] 本地提交可按明确任务文件集串行完成；本轮不推送/部署。最终报告实际测试证据、分支、产物和未完成边界。

## 最终本地验收（2026-10-09）

- 全量pytest：1430 passed、6 Windows符号链接/POSIX权限限制skip，exit0；被中断的运行不计通过。
- 五个任务与整分支复核均通过；最终WB-1批量改绑弹窗错误、WB-2推荐刷新卡片卸载问题已修复，补丁代码提交e6f5cde。
- 补丁后Cua验证：延迟成功/失败推荐回读保留卡片及展开/新焦点；批量错误在活动弹窗持续显示，取消保留选择。全量编译/静态/脚本/差异检查通过。
- 当前为本地分支codex/mobile-first-main交付，未合并、推送或部署；真实安卓与Linux/生产环境边界见移动改造报告。
