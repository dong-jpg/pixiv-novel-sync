# Pixiv Novel Sync 统一项目需求规格

更新：2026-11-17。本文定义有效需求和范围；当前完成度与验证见 [项目现状](STATUS.md)，执行任务见 [实施计划](IMPLEMENTATION_PLAN.md)。main 包含书库、作品分析、个人偏好学习和远程执行基础；ai-writing 继承 main 并增加写作与成人润色。

## 1. 文档定位

本文只保留系统必须提供的能力、必须遵守的边界和明确非目标。操作、实现设计、API 字段和开发任务由各自文档维护，避免把旧计划勾选或历史报告当作当前事实。

### 1.1 需求状态标记

| 标记 | 含义 |
|------|------|
| `MUST` | 已确认的目标要求，后续实现必须满足 |
| `CURRENT` | 代码、测试或当前 API 契约已经提供的行为 |
| `DONE` | 有代码和相应测试证明已落地 |
| `PARTIAL` | 核心能力存在，但仍有明确缺口 |
| `PLANNED` | 已确认设计或计划，尚未证明完整落地 |
| `HISTORICAL` | 归档阶段要求，只作为背景或继承约束 |
| `SUPERSEDED` | 被较新的决策替代，不得作为当前实现目标 |
| `OUT` | 明确不属于当前范围 |

### 1.2 来源优先级

当文档互相矛盾时，按下面两条链分别判断：

1. **当前事实链**：代码与测试 > `README.md` > `docs/frontend-api-contract.md` > `docs/frontend-pages.md`、`docs/library-os-style-guide.md` > `CLAUDE.md`。
2. **目标需求链**：用户最新明确要求 > 本统一需求 > 当前设计 > 实施计划。

文档发生冲突时核对实现并修正文档，未验证事实标记为待核实。旧报告、旧 API 快照和已执行计划已移出当前树，历史可通过 Git 查询。

### 1.3 范围与明确非目标

个人画像、全库自动分析及推荐闭环已经纳入当前范围。下列非目标继续保留，不据旧计划恢复无关功能。

| 项 | 决定 |
|---|---|
| `recommendation_search_plans` 表 | `OUT`。搜索计划只活在当次请求里。 |
| `JobType.RECOMMENDATION_SYNC`、`explanation_source` | `OUT`。 |
| 偏好 SSE、完整多画像工作流 | `OUT`。默认本地画像仍是一条。 |
| 个人画像 AI 结构化总结 | `CURRENT`。2026-11-12 扩展为明确评价与有效作品报告驱动的独立版本化画像，见 §12.6。 |
| `refresh_rescue_entities` 这个新名字 | `OUT`。继续用 `refresh_rescue_item`。 |
| 4593 条性能夹具、`verify_rescue_performance.py` | `OUT`。 |
| task_logs owner lease、seen-cursor、trash manifest 启动重放 | `OUT`。取消和垃圾箱回滚按现有水位与事务做。 |
| Cloudflare 现网证书、48 小时生产观察 | `OUT`。不在代码任务里。 |

作品级内容报告、父级累计预算与业务检查点见 §12.5；个人画像 AI 总结、全库/新增作品分析及推荐反馈闭环见 §12.6。完整多画像、偏好 SSE、推荐计划持久化和 main 写作能力仍为 OUT；作品和画像的业务所有权不恢复旧 task_logs owner lease 工作流。

## 2. 产品目标与用户边界

### 2.1 产品目标

`MUST` 系统应在本地安全保存用户的 Pixiv 小说资产，提供可搜索、可导出、可阅读、可恢复的小说库，并在同一 Web 界面中支持同步任务与偏好推荐。AI 创作工作台（项目 / 章节 / 向导 / 蒸馏 / 成人润色）已移到 `ai-writing` 分支单独维护；main 保留 AI 基础设施（Provider / 模型目录 / 模型池 / 统一路由 / 关键词清洗），另提供手动单作品内容报告，不恢复创作工作台。

`MUST` AI 能力应复用现有 Provider、Agent、SQLite、SSE 和任务基础设施；新增模块不得另建一套互不兼容的用户、文件或任务系统。

`MUST` 系统应优先保证数据可恢复、行为可审计、长任务可取消和敏感信息不外泄，再扩展自动化或模型编排能力。

### 2.2 用户与信任边界

- `MUST` 默认面向单用户本地部署；多用户画像、多账号管理属于后续候选，不得假设已经存在。
- `MUST` Pixiv refresh token、Dashboard token、AI Provider key、小说正文和偏好画像属于敏感数据。
- `MUST` 未配置 `DASHBOARD_TOKEN` 时仅允许本机访问；任何公网、反向代理或 Cloudflare 暴露场景都必须配置独立 token。
- `MUST` 不自动点赞、收藏、关注、评论，不绕过 Pixiv 的可见性、年龄或访问限制。

## 3. 当前系统基线

### 3.1 代码与目录

- Python 包位于 `src/pixiv_novel_sync/`；`cli.py` 是命令入口，`webapp.py` 创建 Flask 应用。
- `jobs/` 负责共享任务、`storage/` 负责 SQLite mixin、`ai/` 负责 AI Provider 与模型目录/池/路由（创作服务在 `ai-writing` 分支）、`templates/` 使用随包提供的 Vue 3 与 Jinja；首屏脚本与样式不依赖外部 CDN。
- `userscripts/pixiv-rescue.user.js` 与救援 API 是一对兼容接口；修改救援响应或认证时必须同步验证脚本。
- `config/`、`deploy/`、`scripts/` 负责配置和部署；`assets/` 负责品牌素材；`tests/` 是 pytest 测试集。

### 3.2 运行时与开发约束

- `MUST` 支持 Python 3.10+、Flask、SQLite/WAL、SSE、现有 `requests`/`pixivpy3` 依赖；模型池第一阶段不新增第三方依赖。
- `CURRENT` 前端没有独立构建器或 SPA 路由；服务端路由和独立 Vue island 是既有边界。
- `CURRENT` 测试入口为 `pytest`，`pyproject.toml` 只配置 `testpaths=tests` 和 `pythonpath=src`；Black、Flake8、Pylint、mypy 目前不是强制工具。
- `MUST` 新代码遵循四空格缩进、`snake_case`/`PascalCase`/大写常量、`from __future__ import annotations` 和既有 `dataclass(slots=True)` 风格。
- `MUST` 用户界面、错误消息和文档除专业术语外使用中文。

## 4. 认证、配置与本地数据

### 4.1 配置来源

- `MUST` 支持 `.env` 与 `config/config.yaml`；环境变量覆盖 YAML。
- `MUST` 从 `.env.example` 和 `config/config.yaml.example` 建立本地配置，不把真实 `.env`、数据库、日志或 `data/` 提交到 Git。
- `MUST` Pixiv refresh token 是基本同步凭据；OAuth、token 登录和 Playwright 辅助登录均应复用现有认证管理器。
- `MUST` Flask secret 缺省时随机生成并原子写回 `.env`，生成后保持稳定；`.env` 写入权限为 `0600`。
- `MUST` AI Provider key 在设置前要求 `PIXIV_NOVEL_SYNC_AI_SECRET_KEY`，使用加密存储，secret 变化时明确报告旧 key 不可解密。

### 4.2 Web 安全

- `MUST` 已配置 Dashboard token 的部署使用持久会话和 CSRF；无 token 的纯本机模式也校验 Host 和 CSRF。不可信 `X-Forwarded-For` 不得用于伪造本机请求。
- `MUST` 生产反向代理必须显式配置可信边界；无 token 的本机模式不应被描述为公网安全模式。
- `MUST` Dashboard 的所有可变 API 校验适用认证、CSRF、参数类型、资源归属和路径边界；救援/worker 使用各自独立凭据协议，不能把其豁免扩展到 Dashboard。
- `MUST` Provider 出站请求复用 SSRF、DNS/IP 固定、Host/SNI、证书校验和禁止重定向策略，不让凭据跟随重定向。

## 5. Pixiv 同步与归档

### 5.1 同步来源

`CURRENT`/`MUST` 支持以下来源，并保留来源关系而不是只保留一个主来源：

- 公开收藏与私密收藏；
- 关注用户的小说；
- 追更系列；
- 用户备份全部小说；
- 用户、小说、系列状态检查和待删除检测所需的远端状态。

### 5.2 内容与增量行为

- `MUST` 保存标题、简介、标签、作者、系列、创建/更新时间、收藏数、浏览数、限制等级和原始元数据。
- `MUST` 保存原始正文、Markdown 正文、正文 hash、封面和插图等可用资产；正文为空时明确标记不完整。
- `MUST` 支持分页、增量同步、去重、断点式状态更新、限速、重试和错误统计；单项失败不得污染已经成功写入的其他项。
- `MUST` 同步任务可协作取消。长等待使用可轮询的短间隔；`InterruptedError` 不能被宽泛异常吞成普通失败。
- `MUST` 保存来源类型、用户 ID、系列 ID 和历史关系，不能因一次同步结果覆盖其他来源。
- `MUST` 系列按远端章节顺序保存，独立小说与系列章节在列表和救援目录中不得重复计数。
- `CURRENT` 本审计分支的用户全量备份可跳过已归档作品，每位作者间隔 7 天强制全刷，只有完全成功才推进全刷水位；取消、单本 / 分页失败或页数截断均不记成功。若作者重访间隔已达 7 天，每轮仍可能全刷，不能保证节省请求。
- `CURRENT` 备份失败比例保护在已处理至少 10 本时启用，比例严格大于 0.2 则中止（包括由成功项达到最小样本的情况）；累计失败达到 10 本的绝对阈值仍独立生效。纯本地跳过不附加逐项延迟，真实请求继续保持间隔；P20 的连续完整页提前停止 / 每日回扫尚未实现。

订阅系列的章节数相同不构成内容未变的证明。整部跳过须有完整、无重复的远端章节 ID 并逐本核对归档文件；分页范围不足或续页异常仍返回不完整。远端重排和已归档同 ID 改文的周期回扫仍在实施计划 S1 中。

### 5.3 状态、待删除与恢复

- `MUST` 支持用户、小说、系列的状态检查，并把 `normal`、`suspended`/`deleted`、`cleared`、`unknown` 等状态映射为中文 UI 标识。
- `MUST` 检测取消收藏、取消追更、删除或受限作品，进入待删除表后提供确认删除和恢复入口；待确认（pending）记录没有宽限期、不会到期自动确认，只有已确认/已恢复记录按 `pending_deletion_cleanup_confirmed_days` 清理。
- `MUST` 删除操作遵守外键和业务顺序，失败时保留可恢复数据，不留下孤儿关系；删除系列不得误删仍被其他关系引用的章节正文。

### 5.4 导出与阅读

- `CURRENT` 支持单本和批量 EPUB 导出，导出正文、元数据、封面和插图时必须进行 XHTML/HTML 安全处理，禁止存储型 XSS。
- `CURRENT` 支持小说全文搜索、阅读进度条件保存/重置和系列目录阅读。11-13 已部署的阅读同步使用版本 CAS、幂等 mutation、离线待确认记录及冲突选择；重置保留递增版本标记。协议见[阅读进度说明](READING_PROGRESS_SYNC.md)，上线证据见[项目现状](STATUS.md)。
- `MUST` 导出失败返回可读错误，不破坏原始归档；大批量导出应有进度或明确的任务状态。

## 6. 存储与数据完整性

- `MUST` 使用 SQLite 单库并启用 WAL、外键和现有 UTC ISO 时间格式；迁移必须幂等，结束时执行 `PRAGMA foreign_key_check`。
- `MUST` 多步骤写入使用事务；目录刷新、模型池成员替换、AI 状态收口和文件替换必须原子化。
- `MUST` 以 `Database` facade 和 `storage/` mixin 分域维护 novels、users、series、bookmarks、tasks、recommendations、rescue、reading progress 和 AI 表。
- `MUST` `data/library/{public,private}/authors/.../novels/...` 的文件路径只能落在允许的存储根目录内；上传、替换和删除必须防路径穿越并保持原子性。
- `MUST` FTS 查询转义用户输入；搜索异常不得把整页 API 变成 500。
- `MUST` Schema 迁移保留既有固定 Agent ID、旧数据和向后兼容读取；弃用字段如 `available_models_json` 只能作一次性迁移输入，不能继续作为运行时事实来源。

## 7. 统一任务、调度与日志

### 7.1 任务模型

- `CURRENT` CLI、Web、自动调度、偏好和推荐任务使用共享 `JobSpec`、`JobManager`、`JobRunner` 和 `execute_task`。
- `MUST` 新增任务类型同时登记执行分派、两个标签字典（`jobs/tasks.py:_TASK_LABELS` 与 `web/managers.py:TASK_LABELS`）和 `web/utils.py:_job_spec` 分支，定时任务另补 `SCHEDULER_TASK_CONFIGS` 与设置三件套（清单见 `docs/JOB_SYSTEM.md` §4）；任务报告 source、job type、task types、统计、进度、错误和终态。
- `MUST` 状态至少包括 queued、running、cancel_requested 与终态 succeeded、failed、cancelled（`task_logs` 另有 `partial`）；取消是合作式的，不能伪装成失败。
- `MUST` 同时限制同一 Web 入口的 active job，避免重复运行、重复刷新目录或并发写同一状态。

### 7.2 自动同步

- `CURRENT` 支持每项任务的 interval-hours 和 cron；cron 优先，错误 cron 返回无下一次时间而不泄漏异常。
- `CURRENT` 本审计分支重启后只补跑最近一个错过的 cron 槽，不逐个追补历史槽；没有历史运行记录时不补跑（`9d763bb`）。
- `MUST` 自动调度具备重载、停止、状态查询、限速顺延和重启后的安全初始化；开发 reloader 不得启动重复调度器。
- `MUST` 调度器按 `sync.task_log_retention_days` 清理任务日志，默认 14 天；同步任务和 AI 任务在同一日志页可按 category、task type、状态、时间筛选。直接调用清理函数而不传天数时，参数默认仍是 3 天。
- `MUST` AI `ai_jobs` 与 `task_logs` 继续分表，但日志页面提供统一投影、真实 job ID、详情、输入/输出摘要和脱敏错误。

### 7.3 取消和并发

- `MUST` 从入口到 Provider、分页等待、推荐搜索和状态检查传递 `stop_requested`。
- `CURRENT` A5 已补齐偏好任务到关键词清洗、路由与 Provider 的取消回调，清洗后检测取消则不发布画像（`a6b95ca`、`db23fe5`）。这是轮询点的协作取消，不保证立即打断同步 HTTP 阻塞读取；deadline 即使以网络错误结束也按路由预算耗尽处理（`8fe9c5f`），不误标为用户取消。
- `MUST` 共享统计更新使用锁内合并；Web 序列化读取快照时不能与后台写入发生竞态。
- `MUST` 服务、数据库连接、HTTP session、信号量和文件句柄在正常、失败、取消路径均释放。

## 8. Web 页面与 API 契约

### 8.1 页面边界

| 页面 | 必须提供的能力 |
|------|----------------|
| `/dashboard` | 系统状态、同步统计、快速操作、最新任务进度 |
| `/dashboard/novels` | 全部、收藏、追更、救援分类；搜索、排序、导出 |
| `/dashboard/novels/<id>` | 元数据、正文、阅读进度、系列关系、救援信息 |
| `/dashboard/series/<id>` | 系列信息、作者、封面、顺序章节和章节跳转 |
| `/dashboard/follows` | 关注用户、状态标识、用户详情入口和单独备份 |
| `/dashboard/users/<id>` | 用户信息、状态、小说列表、检查状态、备份全部 |
| `/dashboard/pending-deletions` | 待删除列表、检测、确认、恢复 |
| `/dashboard/logs` | 同步/AI 日志筛选、详情、刷新、脱敏错误和进度 |
| `/dashboard/settings` | Pixiv、同步、定时、限速、AI Provider、救援 Token |
| `/dashboard/preferences` | 画像分析、搜索计划、推荐任务、反馈和屏蔽 |
| `/dashboard/novels?category=rescue` | 预计算救援目录及筛选 |
| `/token-login` | OAuth/token 登录和错误提示 |

### 8.2 API 约定

- `MUST` 以 `docs/frontend-api-contract.md` 为当前端点和响应契约；旧 `API_COMPLETE.md` 中的 `/api/ai/*`、旧同步路径和旧响应字段标记为 `SUPERSEDED`。注意：`/api/auth/login` **不属于** `SUPERSEDED`——它仍是 Dashboard 登录的必经路径（`webapp.py:_check_auth` 的登录页重定向依赖它），已收录进当前契约。
- `MUST` API 使用统一的 JSON 成功/错误结构、分页、排序、状态码和 CSRF 约定；异步操作返回 job/operation 标识，不能假装同步完成。
- `CURRENT` API 分为 shell/status、sync、archive、rescue、users、logs、settings/cache、pending deletions、preferences/recommendations、AI configuration/jobs、OAuth/token 等族群。
- `MUST` 前端只依赖契约中公开字段；未认证、参数错误、资源不存在、冲突、过期目录和取消分别返回可区分结果。
- `MUST` AI SSE 至少使用 `delta`、`progress`、`metadata`、`done`、`error` 事件并以 `done` 或 `error` 终止；事件可重连、携带阶段/进度/终态，并在页面刷新后通过 job/operation 查询恢复，不发送 key、prompt、正文或完整响应头。

### 8.3 小说、系列和用户需求

- `MUST` 小说页标签固定为“全部、收藏、追更”；追更按系列聚合，显示系列标题、章节数、最新更新时间和来源用户。
- `MUST` 系列详情显示标题、简介、作者、封面和顺序章节；章节可跳转小说详情。
- `MUST` 关注页显示正常、封号/失效、资源清空、未知等彩色状态；用户详情显示头像、账号、统计、小说列表、检查状态和“备份全部”。
- `MUST` 用户备份显示进度、结果统计、错误和重试；网络失败最多自动重试 3 次后给出可读错误，并保留已完成数据。

## 9. 救援目录与 Pixiv 救援阅读

### 9.1 资格规则

- `MUST` 独立小说必须远端明确失效或受限，且本地正文非空；人工 `include` 只能修正远端可用性，不能绕过正文完整性。
- `MUST` 系列只有 `expected_count > 0`、`local_count >= expected_count` 且 `complete_count == local_count` 才是 `success`；未知或非正总数最多为 `partial`，不能标记成功。
- `MUST` 系列父项进入目录后，其章节不再作为重复独立项；独立小说和系列单章按明确的内容类型规则展示。
- `MUST` 人工 `exclude` 优先于自动资格；救援目录不得复用 pending deletion，也不保存虚假的物化历史时间线。

### 9.2 预计算目录

- `DONE` 已有 `novel_texts.has_content`、`rescue_catalog`、`rescue_catalog_sources`、`rescue_catalog_meta` 和事务化重建/局部刷新基础；Task 6-8 的接线已核对完成（2026-08-14），触发点包括：同步/状态检查任务成功后刷新（`jobs/services.py` 的 `_run_status_task`、`run_user_backup_task`、`run_pending_deletion_detection_task`）、快速同步（`jobs/quick_sync.py` 的 `run_bookmark_sync`、`run_scheduled_user_backup`）、任务执行器（`jobs/tasks.py:_run_direct_sync_task`）、调度器首轮初始化（`web/managers.py:AutoSyncScheduler._initialize_rescue_catalog`）、实体删除与待删除恢复（`storage/novels.py:delete_novel`、`storage/users.py:delete_user`、`storage/pending_and_watermarks.py:restore_pending_deletion_atomic`、`webapp.py:_refresh_rescue_chapters`）与人工纠错（`rescue_web.py:_refresh_rescue_item_after_override`）。
- `MUST` 完整刷新和受影响对象增量刷新在事务内完成；失败时继续提供上一份目录和 meta。
- `MUST` 列表使用 SQL 常数查询、分页、排序和筛选，不读取 `novel_texts.text_raw`，不先全量加载到 Python。
- `MUST` 支持小说/系列、success/partial、类型、来源、搜索、stale 组合筛选；多来源筛选按包含关系，不用单一主来源覆盖其余来源。
- `MUST` 未初始化目录返回 503，过期目录明确显示 stale；第一页目标响应不超过 500ms，完整刷新目标不超过 10 秒。
- `MUST` 同步任务成功、首次初始化、人工纠错、实体删除和受影响父系列变化触发正确粒度的刷新，重复入口不能重复刷新。

### 9.3 只读 API 与 userscript

- `MUST` `/api/rescue/v1/` 使用独立 Bearer Token；Token 与 Dashboard token 分离，明文只在生成/轮换时返回一次并只保存摘要。
- `MUST` v1 API 不接受 Cookie 或 query-string token，不提供普通私有作品枚举；单项查询仍按当前数据库事实实时校验，不能因目录过期开放普通私人备份。
- `MUST` v1 API 只允许 GET/HEAD；限流使用三个独立的 60 秒滑动窗口桶，均按客户端 IP 而非 Token 前缀计数：未认证 / 无效 Token 120 次，认证正文 / 目录 / lookup 共用 120 次，认证封面 600 次。认证错误只消耗未认证桶；响应使用 `Cache-Control: no-store`、`X-Robots-Tag: noindex` 和 `X-Content-Type-Options: nosniff`，500 错误不得泄露内部路径。
- `MUST` userscript 在正常可读的小说 / 系列页不请求备份内容、不替换原内容；失效或状态不明的页面可查询备份，显式救援链接可直接打开阅读层。列表页允许一次 Token 探测且只重建失效卡片；手机新作页的「拯救系列」tab 专门展示救援系列。此已确认边界替代旧版「正常页面不得请求 API」的过宽限制，不授权修改正常原作内容。
- `MUST` userscript 的 Token 只保存在脚本特权存储、通过 `Authorization` 请求头发送，不写入 Pixiv 页面 DOM、Cookie 或查询参数；打开救援层的链接标记不携带 Token。
- `MUST` 救援正文使用 `textContent` 等纯文本节点渲染，不使用可注入的 `v-html` 或未净化 HTML。

## 10. 偏好分析与 Pixiv 推荐

### 10.1 偏好画像

- `MUST` 从本地 `novels`、`novel_texts`、`series`、`users`、`sources`、`novel_fts` 统计标签、关键词、作者、长度、来源、系列占比、限制等级和热度。
- `MUST` 支持全部归档、收藏、追更、指定作者/标签/时间范围、排除短文和排除失效项等分析范围；默认使用本地可见正文，过滤正文少于 1,000 字的样本。
- `MUST` 画像为版本化 JSON，目标结构包括摘要、正向标签/关键词/题材/关系/情境/语气/节奏/叙事模式、负向排除项、搜索策略、阅读偏差和置信度。当前审计已删除无生产者/消费者的部分正向维度；恢复这些维度时必须同时实现生产、消费和回归测试，不能只加空字段。
- `MUST` 本地统计不依赖 LLM，关键词清洗失败保留本地结果。个人画像 AI 总结单独版本化保存，以明确喜恶和有效作品报告为依据；普通本地统计重建不覆盖该层，作品归档不直接写成个人喜好。
- `CURRENT` 分析作为共享后台 job，通过任务状态轮询查看进度；保留画像版本、命名、删除和默认画像的已有 API。增量分析仅显式提供 name/description 时覆盖原值；偏好 SSE 与完整多画像工作流为 §1.3 的 OUT。

### 10.2 搜索计划与推荐

- `MUST` 当次请求的搜索计划包含宽泛、精准、组合和实验性查询、排除词、原因和 limit，并在服务端截断、去重后执行。`recommendation_search_plans` 表与跨次计划 CRUD 为 §1.3 的 OUT。
- `MUST` 推荐执行分页搜索、限速、详情补全、候选合并和历史去重；单篇正文少于 5,000 字、系列总字数少于 20,000 字的候选过滤，不设上限。
- `MUST` 默认排除已归档、历史 dismiss、屏蔽作者和屏蔽标签；系列去重必须按 series ID 跨 run 生效，不能只按单篇 novel ID。
- `MUST` 推荐结果保存标题、作者、标签、限制等级、字数、热度、score、命中标签/关键词/偏好、风险说明、来源查询和状态。
- `MUST` 规则分可解释：标签命中权重最高，标题/简介关键词次之，作者偏好和系列长度加分，热度只能轻微加分，负向冲突扣分或剔除；当前解释由确定性规则生成，不将其描述为 AI 结构化总结。
- `MUST` 支持感兴趣、不感兴趣、屏蔽作者、屏蔽标签、加入待阅读/待同步、立即同步单篇/系列；反馈和屏蔽在下一次推荐中生效。
- `CURRENT` 推荐核心、任务日志、`x_restrict`/risk 和跨 run 系列去重已有实现；query_errors 与 candidate_errors 分开统计，避免坏候选误判全部查询失败。个人 AI 画像与反馈按 §12.6 参与推荐；偏好/推荐 stream 端点仍为 §1.3 的 OUT，创作注入不属于 main。

### 10.3 推荐与 AI 创作连接（已移至 `ai-writing` 分支）

创作向导、长篇规划、章节续写、章节 Pipeline、润色、去 AI 味和内容审计中选择 `preference_profile_id`、以及偏好注入强度的需求，随 AI 写作模块在 `ai-writing` 分支维护，main 分支不实现。

- `MUST` 画像默认本地保存，可删除；前端不默认展开敏感正文证据。

## 11. AI 创作工作台（已移至 `ai-writing` 分支）

本节原记载 AI 工作台的全部需求：项目 / 章节 / 草稿 / Pipeline、长篇规划、风格蒸馏与小说蒸馏、语义检索（TF-IDF / Qwen embedding）、内容审计、自动摘要、伏笔管理、风格控制与封面、`/dashboard/ai` 与 `/dashboard/wizard` 页面边界等。2026-09-15 起 AI 写作模块（`ai/services/{generation,projects,chat_wizard,adult}.py`、`ai/retrieval.py`、`ai/detection.py`、`storage/ai/{writing,documents,adult}.py` 及对应模板/路由/测试）整体移至 `ai-writing` 分支维护，main 分支不再实现这些能力；需求原文以 `ai-writing` 分支的本文件为准。

main 分支保留的 AI 范围：

- `CURRENT` Provider、Agent、模型目录、模型池与统一路由（见第 12 节），Provider 适配至少覆盖 OpenAI-compatible、Anthropic、xAI。
- `DONE` 偏好关键词清洗（`AIWritingService.clean_keywords`，`jobs/tasks.py` 唯一调用点，失败优雅降级）。
- `CURRENT` 用户选择的已归档单篇/系列作品内容报告（§12.5），不属于个人画像或写作功能；整阶段验收状态见 [项目现状](STATUS.md)。
- `CURRENT` 明确评价与有效作品报告驱动的个人 AI 画像、全库分析和推荐反馈闭环（§12.6），使用独立业务预算与原共享执行槽。
- `MUST` 存量库中的写作表（`ai_drafts`、`ai_documents`、`ai_style_profiles`、`ai_novel_profiles`、`ai_prompt_templates` 及成人润色相关表）保留不删：迁移只加不减，切回 `ai-writing` 分支时数据仍可用。

## 12. AI 模型目录、模型池与统一路由

### 12.1 目标和兼容性

共享 AI 以 [AI 调用设计](AI_ROUTING_DESIGN.md) 为准，保留 Python3.10+、requests/Flask/SQLite 和无独立前端构建器。原 Provider/Agent 数值和默认值不自动升为1M。作品分析与画像使用独立业务预算及检查点，不能把通用路由继续等同业务恢复。实际状态见 [项目现状](STATUS.md)。

- `MUST` Provider 可安全发现并同步结构化模型目录，用户可建立有序 primary/secondary/grok/custom 模型池、后备池和 Agent 绑定。
- `MUST` 既有固定 Agent 自动迁移为 `binding_type=fixed`；ID、Provider、模型、Prompt、参数、启用状态和既有固定调用语义不变。
- `MUST` Agent 支持 fixed 与 pool 两种绑定；固定 Agent 不自动跨 Provider 切换，模型为空时使用 Provider default model，两者为空时在网络请求前返回中文配置错误。
- `MUST` 所有 AI 生成通过单一 `ModelRouter` 入口，业务层不得直接调用 `provider.stream_generate()`。显式豁免：Provider 实现内部、Router 内部，以及 `ai/services/admin.py` 的 `test_provider` 连通性测试（该测试的目的就是验证单一 Provider 的直连可用性，不应经过路由与故障转移）。

### 12.2 目录事实源和规范化

- `MUST` `ai_provider_models` 是运行时事实源；`available_models_json` 只用于一次性迁移。
- `MUST` 同步只更新 `discovered_*`、`discovered_available`、`last_seen_at`，不得覆盖 `manual_*` 和用户 enabled 状态；消失模型标记不可用，不直接删除被引用行。
- `MUST` model key 视为不透明上游标识，拒绝控制字符并保留字节语义；显示名、能力和白名单 metadata 使用 NFC 规范化。
- `MUST` canonical digest 只基于规范化字段；metadata 不得包含 key、Prompt、正文、请求体或 secret。

### 12.3 模型池与路由

- `MUST` 池成员按 `position` 展开，先尝试当前池再进入后备池；后备链禁止直接/间接循环，深度最多 8。
- `MUST` 单池最多 64 成员，展开去重后最多 64 个有效候选；被 Agent 或其他池引用的池不能删除、禁用或清空；成员替换必须是完整原子替换。
- `MUST` 所有池写入携带 `expected_version`，使用 CAS；并发冲突返回可识别的 conflict，而不是覆盖他人修改。
- `MUST` 首个正文 `delta` 之前允许按候选顺序跨模型/Provider 故障转移；正文已输出后不得自动切换，后续失败统一收口为 `partial`。
- `MUST` 用户取消、断连或 `GeneratorExit` 不触发切换；Provider 自己的重试和流式降级仍可保留。
- `MUST` 每个 job 最多 16 个候选、32 次网络请求、总 deadline 30 分钟。
- `MUST` v2 对完整文本消息逐候选估算和准入，禁止跨候选 max(input)/min(window) 交叉比较；未知窗口/用量不补零。预检256消息/16MiB，不发网络请求或创建 job；模型/Provider/Agent 瓶颈和输出限制可解释。

### 12.4 审计、续接和同步

- `MUST` 每个 job 保存候选快照 hash、候选索引、池版本、Agent 版本、Provider 配置 hash 和实际 attempt；attempt 保存状态、阶段、模型、脱敏错误和时间。
- `MUST` `partial` 是正式终态；main 第一阶段不新增业务重放处理器，continue 保留409“任务类型暂不支持”，不创建 job/网络请求。保留v1快照/hash/预算与底层续接校验，不按新配置重建旧候选；UI 无可执行继续按钮。
- `MUST` 新统一调用使用 metadata_only 留存，正文仅经内存返回；唯一兼容例外为关键词清洗内部有限词/标签和结果。发送前同事务记录网络 ordinal 与 pending/未知用量；attempt usage_json 可空，最多16KiB/32请求，累计用量事件去重，业务校验与传输终态分开。
- `MUST` job owner、lease、heartbeat、generation 和终态使用 CAS；stale job 只回收过期租约，不能误杀其他进程的有效任务。
- `MUST` Provider 同步支持 queued/running/needs_empty_confirmation/succeeded/failed/cancelled，空目录必须二次确认；同步 operation 保存最小状态，调度器按 `sync.task_log_retention_days` 清理，默认 14 天；直接调用清理函数而不传天数时，参数默认仍是 3 天。
- `MUST` 单页同步响应最多 4 MiB、单次最多 20 MiB、最多 100 页和 5,000 个模型；同步总超时 10 分钟；复用现有 SSRF/DNS/Host-SNI/禁重定向/脱敏。
- `CURRENT` 共享基础提供 Schema、目录同步、池图/CAS、Router、用量与诊断；业务继续能力由具体处理器决定，不以底层 API 存在声明 main 可以续写。
- `OUT` 第一阶段不实现跨任务健康计数、冷却、权重轮询、成本排序和后台定时目录刷新。

### 12.5 手动作品分析（第二阶段局部扩展）

以 [作品分析设计](WORK_ANALYSIS_DESIGN.md) 为约束，[前端 API 契约](frontend-api-contract.md#work-analysis-apis) 为接口事实源；CURRENT 表示已有代码，不等于真实模型、手机或生产验收完成。

- `CURRENT` `/dashboard/preferences?view=work-analysis` 是原偏好页的独立视图，无 view 的画像/推荐页面不变；一次只处理用户明确选择的一篇已归档小说或一个已归档系列。
- `MUST` 手动作品入口不隐式扩大到全库或补章。全库/新增作品分析与个人画像按 §12.6 单独确认。归档、收藏、追更和“感兴趣”不等于已读且喜欢，不推断个人身份或性取向，不自动收藏或关注。
- `CURRENT` 本地预览冻结源清单、章序/hash 和配置，披露所有可能使用的安全 Provider/模型；不解密 Key、联网或创建 job。整部优先，仅输入容量超限时分章/块，不用开头抽样替代全文、不在发送失败后改变粒度。输入覆盖、原作完整性与模型解释可靠性分别显示。
- `MUST` 显式填写并确认父预算：请求数 1–512、计划 token 1–100000000、累计活跃秒 30–14400。每次真实发送按候选四项估算同事务预留，unknown 不补零，发送结果不确定保守扣账；不保证金额或厂商真实计费 token。每子 job 的 16 候选/32 请求/30 分钟上限不变。
- `CURRENT` 作品级检查点与版本 CAS 控制暂停/取消/恢复及增额；正常停止只结算一次时间片，崩溃/撤权保守扣未结算片。源或配置变化使任务失效，同 hash 元数据刷新不使报告旧版；新增系列章仅标 archive_outdated。重启不清预算或自动重发付费调用，旧 main continue 仍不支持。
- `MUST` 首版仅现有单一 Web 调度 owner、共享单槽和既有线程执行，同步优先；自动同步关闭仍允许手动/完成回调准入，不加 CLI 执行器或独立队列。
- `CURRENT` 通用调用保持 `metadata_only`；正文无完整输入重放副本，有界短证据可能与极短章重合。业务报告/证据使用 Dashboard 认证、CSRF、no-store 与文本节点，默认折叠；不进入普通日志、进度、URL、localStorage 或公开救援/FTS。删除撤权清理敏感业务产物，迟到 worker 不得复活；删除不承诺备份/WAL 的物理清除。
- `CURRENT` 报告支持逐条标记不可靠与恢复，使用报告绑定身份、严格版本和单调纠错 revision。操作保留模型原始结论，排除不可靠项进入画像；标记/恢复都使依赖画像失效，恢复不复活旧结果。§12.6 的作品级喜恶评价不代替该项。外部验收边界见 STATUS。

### 12.6 偏好学习、全库分析与推荐闭环（2026-11-12 扩展）

- `CURRENT` `/dashboard/preferences?view=learning` 提供独立的个人画像、全库批次及自动策略管理。小说/系列详情按需读取并版本化保存 `liked/disliked/neutral/clear` 评价，兴趣/收藏/仅已读不迁成喜恶。
- `CURRENT` 画像输入只包含明确评价和当前有效作品报告中的有界特征与短证据。模型必须引用确认输入，伪造标签、极性或引用会被拒绝；最多 2000 个反馈、16 MiB 序列化输入，超限拒绝而非静默抽样。
- `MUST` 画像沿用统一 Router 和 Web 共享单槽；`preference_binding=None` 为内部可选父绑定，真实请求预留与子调用序号同事务提交。三项显式预算、配置/源检查、取消与最终发布互斥，通用记录保持 `metadata_only`。
- `CURRENT` 画像独立保存、可启用或删除；新用户首次启用时补最小默认画像，不运行旧全库统计。反馈变化、来源删除或作品报告过期使相关画像失效；推荐只消费当前有效层，发布前再核对版本。
- `CURRENT` 全库以系列和独立单篇去重，复用当前有效报告、记录已尝试或已观察的任务身份。同 hash 的元数据刷新和删除已观察的报告不触发自动再次付费；实际新正文可形成新目标。
- `MUST` 每个作品分配完整预算上限，全批/策略预占与创建同事务。持续策略的总授权与作品数跨批次累计，停用、重启、修改配置不清零；子预算增额同时扣减剩余总授权。
- `CURRENT` 自动策略默认关闭，首次开启新增模式记录范围基线；后续暂停和提额保留基线。共享调度器即使关闭普通自动同步也可继续已启用的作品分析；停用会暂停关联批次，已获最终保存许可的结果仍可完成。
- `MUST` 重启停止遗留运行，原请求不会自动重发。批次 `completed` 只表示发现结束，逐作品报告另有运行状态。API、旧画像/推荐接口和页面均保持私有/no-store，普通日志不记录模型输入、画像或推荐查询内容。

## 13. 成人描写局部润色 Agent（已移至 `ai-writing` 分支）

本节原记载成人局部润色 Agent 的全部需求（fail-closed 认证、角色确认、Provider scope、固定 `adult_safety_review` / `adult_fact_guard` 两阶段 JSON review、乐观锁应用、脱敏存储与保留期）。2026-09-15 起成人润色模块（`ai/adult_*.py`、`ai/services/adult.py`、`storage/ai/adult.py`、`/dashboard/settings/adult` 页面与对应路由/测试）整体移至 `ai-writing` 分支维护，main 分支不实现该功能；需求原文以 `ai-writing` 分支的本文件为准。

- `MUST` main 分支保留存量成人润色数据表不删除（迁移只加不减）；`ai_jobs` 的 `owner_scope` / `idempotency_key_hash` 列继续存在。

## 14. 视觉、响应式与可访问性

- `MUST` 遵循 Library OS 设计：浅色工作台、稳定的 shell/page 容器、清晰层级、有限圆角、统一 token、可扫描表格和可预测的操作反馈。
- `MUST` 桌面使用约 260px 固定侧栏；1024px 以下切换底部导航，900-1024px 以下的复杂双栏改为单列，移动端页面 padding 保持 16-22px。
- `MUST` 桌面和移动端均不产生横向溢出；固定格式元素使用稳定尺寸，动态文字不得覆盖相邻内容。
- `MUST` 共享组件、按钮、表单、表格、badge、modal、terminal/log block 和空状态遵循 `library-os-style-guide.md`；页面不能嵌套卡片制造多重容器。
- `MUST` 交互控件提供键盘焦点、语义标签、足够对比度、可读错误、加载/空/失败/成功状态；不要把重要信息只放在颜色或 hover 中。
- `MUST` 前端使用安全文本渲染，避免 `v-html`；SSE、任务取消、重试和过期数据均有明确状态。
- `MUST` 品牌素材以当前 README 引用的 `assets/main-logo.svg` 为准，旧方案不重新成为实施要求。

## 15. 部署与运维

- `CURRENT` 支持本机启动、`deploy.sh` 的 venv + Nginx + systemd 部署；旧 `scripts/install_server.sh` 仅作为遗留 timer 部署参考。
- `MUST` 对外部署时 Dashboard 仅监听本机 Flask 端口，由 Nginx/Cloudflare 提供代理和 TLS。
- `MUST` 使用 Cloudflare 时启用 `Full (strict)` 并核验证书用途、域名、有效期和源站链；公网 HTTP 跳转 HTTPS，源站仅监听本机。示例域名/端口必须按实际环境配置，私钥只在服务器保存且权限受限；当前部署必须通过目标服务器核验。
- `MUST` 部署文档说明备份 `data/`、SQLite、`.env`、AI secret 和救援 token 轮换；日志和数据库权限不能向公众开放。
- `OUT` Docker、多账号、插件系统、移动端 App、国际化和更多 Provider 在 README/旧路线图中属于未来愿景，未被当前规格确认。

## 16. 非功能、性能与安全验收

### 16.1 数据和安全

- API key、refresh token、Dashboard token、救援 token、Prompt、正文、请求体、完整响应头不得出现在日志、SSE、快照、attempt 或错误响应中。
- 所有外部 URL、上传文件、模板变量、搜索表达式和导出正文必须经过相应的 SSRF、路径、XSS、注入和大小限制。
- 迁移、目录刷新、模型同步确认、池成员替换、AI 应用和删除恢复必须可回滚或保留上一份有效快照。
- 真实 Pixiv、AI、DNS、部署网络不参与默认 pytest；测试使用临时数据库、目录、HTTP 和 DNS fixture。

### 16.2 性能目标

- 搜索和救援列表按固定数据规模记录耗时、SQL 次数和读取量；列表不得读取全文正文。未提供测量环境时不承诺固定毫秒或每分钟同步量。
- 同步吞吐受 Pixiv 限速和用户请求预算约束；性能优化不能通过提高请求频率或跳过完整性校验达成。
- 单 job 的候选、请求和总 deadline 遵守第 12.3 节上限；模型同步遵守第 12.4 节上限。
- AI 可流式返回，首 token 受外部模型影响；超时、取消和断连必须释放资源。长篇和画像性能以重复读取量、心跳成本及事务持有时间衡量。

### 16.3 可靠性

- 长任务在重启、取消、异常、网络超时和 Provider 失败后都有明确终态或 stale 回收策略。
- 旧数据、旧固定 Agent、旧来源关系和旧 URL 的迁移或兼容行为必须有回归测试；删除或替代接口不得无提示地改变前端契约。

## 17. 测试与验收矩阵

### 17.1 统一验证

- `python -m pytest -q`：完整回归；默认 fixture 不接触真实数据。
- `python -m pytest tests/<feature>.py -q`：功能变更的定向回归。
- `python -m compileall src`：检查 Python 语法和导入基础完整性。
- `git diff --check`：检查文档和代码空白错误。
- 变更模板、API、Schema、userscript 或安全边界时，必须同时更新对应契约测试和失败/成功路径测试。

### 17.2 必测领域

- 同步分页、去重、限速、取消、失败保留成功数据、用户/小说/系列状态。
- SQLite 迁移、外键、事务回滚、FTS 转义、路径边界、文件原子替换和并发快照。
- 任务状态、单 active job、调度 cron、日志投影、`task_log_retention_days` 默认 14 天的清理和取消终态。
- 救援资格、覆盖优先级、目录重建/增量回滚、来源筛选、stale/503、Token 和 userscript 安全。
- 偏好空数据、短文本过滤、画像 JSON、推荐去重/评分/反馈/屏蔽、AI 不可用降级。
- Provider SSRF、模型规范化、池循环/CAS、路由切换、partial、续接快照和敏感字段脱敏。
- 前端深链接、响应式溢出、键盘可达、加载/空/错误态、SSE 重连和不使用 `v-html`。

### 17.3 覆盖率说明

当前 `pyproject.toml` 没有强制覆盖率阈值。测试按行为风险覆盖错误、并发、资源和兼容边界，不用单一百分比替代验收。

## 18. 实现状态与优先级

### 18.1 已有基础（`DONE/CURRENT`）

- 核心 Pixiv 同步、SQLite/文件归档、Web 仪表盘、EPUB、任务调度和共享 JobRunner。
- 任务取消硬化、Provider key 加密、SSRF/DNS/代理信任边界、测试隔离和多轮审计修复。
- 偏好本地统计、推荐核心、反馈/屏蔽基础、统一日志投影和关键词清洗。
- 救援单项实时 API、userscript、正文完整度字段、预计算目录和来源展示。
- AI 模型目录、池图/CAS、统一调用、用量与诊断、设置和日志；main 通用继续不可用，作品恢复和写作继续分别处理。

### 18.2 当前主线（main）

当前开发顺序以 [实施计划](IMPLEMENTATION_PLAN.md) 为准：先补结论纠错、分析重复读取与远程维护，再实施轻量心跳、同步数据语义、写作检索事务和完整远程任务接线。

§1.3 的非目标继续有效；写作/成人功能仅在 ai-writing。生产迁移、真实 Provider/Pixiv、证书与手机验收必须分别确认，不能由离线回归、计划或合并推出已上线。

### 18.3 后续体验维护

- main 维护小说库、偏好/推荐、救援与 AI 基础设施设置页面；本轮 Agent 表单补充 context_window/top_p 并保留合法零值。
- 任务日志筛选、详情与移动端布局继续保持 AI `ai_jobs` 和同步 `task_logs` 分表边界；浏览器/移动端实际验收与自动回归分开记录。
- AI 项目总览、写作/向导、章节 Pipeline 与成人交互只在 ai-writing，不是 main 待办。

## 19. 历史候选与明确非目标

以下需求曾出现在归档路线图、审计建议或早期愿景中，但没有被当前统一规格确认。它们必须作为候选记录，不能在实现中默认展开：

- 首次配置向导（Token → 同步配置 → 首次同步）。
- 统一 API 错误响应的全面重构、Blueprint 拆分、PostgreSQL 抽象和更大规模存储重构。
- Docker 部署、移动端 App、多账号、插件系统、国际化、监控告警。
- 高级 embedding/多模型投票、定时自动推书、复杂社交推荐和成本/权重调度。
- 自动点赞、收藏、关注、评论、绕过 Pixiv 限制、默认下载全部推荐全文。
- 成人整章/多片段批处理、外部身份猜测、大型 NLP 编辑器依赖。

旧归档中已经完成的并发、外键、事务、模块拆分、CLI/Web 共享任务和 Qwen embedding 工作，作为当前质量约束继承，但不重复创建实施任务。

## 20. 文档治理

所有现行入口由 [文档索引](INDEX.md) 维护。模型目录/统一调用、作品分析/偏好学习和远程协议分别维护稳定设计文档；原偏好推荐草案的有效要求已合入 §10、§12.6 与作品分析设计。

不再提交审计、验收、部署、完成报告或生成的验证附件。已执行计划和旧设计副本从当前树删除，必要历史通过 Git 查看。可复用验证程序放在 `scripts/`；新结果写入 STATUS 的当前验证区，失败或未验收条件明确说明。

当前 API 仍有 raw JSON 与 envelope 的兼容形态，不能在文档清理时假设所有端点已经统一。任务类型从实际 registry 核对，模板/静态资源、索引文件和命令从当前代码核对，不继承历史硬编码数量或路径。

## 21. 维护规则

- 新增或变更功能时，先更新本文件对应的需求、状态和来源，再修改 README、API 契约、页面清单或实施计划。
- 删除过期文档前将仍有效的约束迁入指南、设计或本文件，并修复入站引用。
- 需求状态变更必须有代码及对应测试，测试环境与生产启用分别记录；计划勾选不足以证明完成。
- API、数据库 Schema、认证、安全和 userscript 变更必须同时更新契约、回归测试和本文件的验收矩阵。
- 不新增平行报告或按日期复制指南；实施计划在原文件持续更新，历史由 Git 保存。
