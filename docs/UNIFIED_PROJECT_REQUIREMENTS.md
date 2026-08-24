# Pixiv Novel Sync 统一项目需求规格

> 版本：v1.1（文档整合与状态校准版）
> 整合日期：2026-07-28
> 状态更新：2026-08-14
> 项目：Pixiv Novel Sync
> 文档性质：需求基线、现行契约索引、状态与来源追溯
> 覆盖范围：整合输入为 56 份正式 Markdown 与 23 份补充 Markdown（共 79 份，不含本文）

## 1. 文档定位

本文件把项目入口、功能需求、前端契约、审计报告、设计规格、实施计划、任务 brief/report 和历史归档中的有效要求合并为一个可检索的基线。它同时回答四个问题：系统必须提供什么、必须遵守哪些边界、哪些能力已经落地、后续应按什么顺序补齐。

本文不是把所有旧文字直接拼接。重复要求只保留一份；已替代或已经证明过时的方案保留在第 13 节；仅作为历史记录的报告不重新变成现行开发任务。

### 1.1 需求状态标记

| 标记 | 含义 |
|------|------|
| `MUST` | 已确认的目标要求，后续实现必须满足 |
| `CURRENT` | 代码、测试或当前 API 契约已经提供的行为 |
| `DONE` | 有提交、测试或完成报告证明已落地 |
| `PARTIAL` | 核心能力存在，但仍有明确缺口 |
| `PLANNED` | 已确认设计或计划，尚未证明完整落地 |
| `HISTORICAL` | 归档阶段要求，只作为背景或继承约束 |
| `SUPERSEDED` | 被较新的决策替代，不得作为当前实现目标 |
| `OUT` | 明确不属于当前范围 |

### 1.2 来源优先级

当文档互相矛盾时，按下面两条链分别判断：

1. **当前事实链**：代码与测试 > `README.md` > `docs/frontend-api-contract.md` > `docs/frontend-pages.md`、`docs/library-os-style-guide.md` > `CLAUDE.md`。
2. **目标需求链**：最新已确认的统一需求 > 最新设计规格 > 对应实施计划和任务报告 > 旧计划与归档报告。

`docs/INDEX.md` 认定 `API_COMPLETE.md`、`KNOWLEDGE_GRAPH.md` 和 `AI_WRITING_STUDIO_PLAN.md` 是历史快照；它们可以帮助理解演进，但不能覆盖当前接口或数据结构。无法判定的冲突必须保留来源并标记为“待核实”，不能静默选择。

## 2. 产品目标与用户边界

### 2.1 产品目标

`MUST` 系统应在本地安全保存用户的 Pixiv 小说资产，提供可搜索、可导出、可阅读、可恢复的小说库，并在同一 Web 界面中支持同步任务、偏好推荐和 AI 创作。

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
- `jobs/` 负责共享任务、`storage/` 负责 SQLite mixin、`ai/` 负责 AI Provider 与创作服务、`templates/` 使用 Vue 3 CDN 和 Jinja。
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

- `MUST` 已配置 Dashboard token 的部署使用会话和 CSRF；不可信 `X-Forwarded-For` 不得用于伪造本机请求。无 token 的纯本机模式保留现有 CSRF 例外，但该例外不能通过代理扩展到远程来源。
- `MUST` 生产反向代理必须显式配置可信边界；无 token 的本机模式不应被描述为公网安全模式。
- `MUST` 除上述纯本机兼容例外外，所有可变 API 统一校验认证、CSRF、参数类型、资源归属和路径边界。
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

### 5.3 状态、待删除与恢复

- `MUST` 支持用户、小说、系列的状态检查，并把 `normal`、`suspended`/`deleted`、`cleared`、`unknown` 等状态映射为中文 UI 标识。
- `MUST` 检测取消收藏、取消追更、删除或受限作品，进入待删除表后提供确认删除和恢复入口；宽限期由配置控制。
- `MUST` 删除操作遵守外键和业务顺序，失败时保留可恢复数据，不留下孤儿关系；删除系列不得误删仍被其他关系引用的章节正文。

### 5.4 导出与阅读

- `CURRENT` 支持单本和批量 EPUB 导出，导出正文、元数据、封面和插图时必须进行 XHTML/HTML 安全处理，禁止存储型 XSS。
- `CURRENT` 支持小说全文搜索、阅读进度保存/删除、系列目录阅读和 AI 创作小说阅读。
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
- `MUST` 新增任务类型同时登记执行分派和 `_TASK_LABELS`，任务报告 source、job type、task types、统计、进度、错误和终态。
- `MUST` 终态至少包括 queued、running、succeeded、failed、cancelled；取消是合作式的，不能伪装成失败。
- `MUST` 同时限制同一 Web 入口的 active job，避免重复运行、重复刷新目录或并发写同一状态。

### 7.2 自动同步

- `CURRENT` 支持每项任务的 interval-hours 和 cron；cron 优先，错误 cron 返回无下一次时间而不泄漏异常。
- `MUST` 自动调度具备重载、停止、状态查询、限速顺延和重启后的安全初始化；开发 reloader 不得启动重复调度器。
- `MUST` 任务日志保留默认 3 天；同步任务和 AI 任务在同一日志页可按 category、task type、状态、时间筛选。
- `MUST` AI `ai_jobs` 与 `task_logs` 继续分表，但日志页面提供统一投影、真实 job ID、详情、输入/输出摘要和脱敏错误。

### 7.3 取消和并发

- `MUST` 从入口到 Provider、分页等待、章节等待、推荐搜索和状态检查传递 `stop_requested`。
- `MUST` 共享统计更新使用锁内合并；Web 序列化读取快照时不能与后台写入发生竞态。
- `MUST` 服务、数据库连接、HTTP session、信号量和文件句柄在正常、失败、取消路径均释放。

## 8. Web 页面与 API 契约

### 8.1 页面边界

| 页面 | 必须提供的能力 |
|------|----------------|
| `/dashboard` | 系统状态、同步统计、快速操作、最新任务进度 |
| `/dashboard/novels` | 全部、收藏、追更、AI 创作、救援分类；搜索、排序、导出 |
| `/dashboard/novels/<id>` | 元数据、正文、阅读进度、系列关系、救援信息 |
| `/dashboard/series/<id>` | 系列信息、作者、封面、顺序章节和章节跳转 |
| `/dashboard/follows` | 关注用户、状态标识、用户详情入口和单独备份 |
| `/dashboard/users/<id>` | 用户信息、状态、小说列表、检查状态、备份全部 |
| `/dashboard/pending-deletions` | 待删除列表、检测、确认、恢复 |
| `/dashboard/logs` | 同步/AI 日志筛选、详情、刷新、脱敏错误和进度 |
| `/dashboard/settings` | Pixiv、同步、定时、限速、AI Provider、救援 Token |
| `/dashboard/preferences` | 画像分析、搜索计划、推荐任务、反馈和屏蔽 |
| `/dashboard/ai` | AI 项目、长篇规划、章节、Pipeline、封面、风格控制 |
| `/dashboard/wizard` | 创作向导、蒸馏档案和导入流程 |
| `/dashboard/novels?category=ai` | AI 创作小说库和阅读 |
| `/dashboard/novels?category=rescue` | 预计算救援目录及筛选 |
| `/token-login` | OAuth/token 登录和错误提示 |

### 8.2 API 约定

- `MUST` 以 `docs/frontend-api-contract.md` 为当前端点和响应契约；旧 `API_COMPLETE.md` 中的 `/api/ai/*`、旧同步路径和旧响应字段标记为 `SUPERSEDED`。注意：`/api/auth/login` **不属于** `SUPERSEDED`——它仍是 Dashboard 登录的必经路径（`webapp.py:484` 的登录页重定向依赖它），已收录进当前契约。
- `MUST` API 使用统一的 JSON 成功/错误结构、分页、排序、状态码和 CSRF 约定；异步操作返回 job/operation 标识，不能假装同步完成。
- `CURRENT` API 分为 shell/status、sync、archive、rescue、users、logs、settings/cache、pending deletions、preferences/recommendations、AI content/jobs/projects/chat、OAuth/token 等族群。
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

- `DONE` 已有 `novel_texts.has_content`、`rescue_catalog`、`rescue_catalog_sources`、`rescue_catalog_meta` 和事务化重建/局部刷新基础；Task 6-8 的接线已核对完成（2026-08-14），触发点包括：同步任务成功后刷新（`jobs/services.py:204,451`）、快速同步（`jobs/quick_sync.py:69,154`）、任务执行器（`jobs/tasks.py:209`）、web 管理器（`web/managers.py:329`）、应用启动首次初始化（`webapp.py:107`）、正文写入（`storage/novels.py:385`）与救援上传（`rescue_web.py:111-122`）。
- `MUST` 完整刷新和受影响对象增量刷新在事务内完成；失败时继续提供上一份目录和 meta。
- `MUST` 列表使用 SQL 常数查询、分页、排序和筛选，不读取 `novel_texts.text_raw`，不先全量加载到 Python。
- `MUST` 支持小说/系列、success/partial、类型、来源、搜索、stale 组合筛选；多来源筛选按包含关系，不用单一主来源覆盖其余来源。
- `MUST` 未初始化目录返回 503，过期目录明确显示 stale；第一页目标响应不超过 500ms，完整刷新目标不超过 10 秒。
- `MUST` 同步任务成功、首次初始化、人工纠错、实体删除和受影响父系列变化触发正确粒度的刷新，重复入口不能重复刷新。

### 9.3 只读 API 与 userscript

- `MUST` `/api/rescue/v1/` 使用独立 Bearer Token；Token 与 Dashboard token 分离，明文只在生成/轮换时返回一次并只保存摘要。
- `MUST` v1 API 不接受 Cookie 或 query-string token，不提供普通私有作品枚举；单项查询仍按当前数据库事实实时校验，不能因目录过期开放普通私人备份。
- `MUST` v1 API 只允许 GET/HEAD，并按来源地址与 Token 执行每分钟 120 次限流；响应使用 `Cache-Control: no-store`、`X-Robots-Tag: noindex` 和 `X-Content-Type-Options: nosniff`，500 错误不得泄露内部路径。
- `MUST` userscript 仅在 Pixiv 小说或系列明确失效时读取救援 API；正常页面不得请求 API、修改原内容或泄露 Token。
- `MUST` 救援正文使用 `textContent` 等纯文本节点渲染，不使用可注入的 `v-html` 或未净化 HTML。

## 10. 偏好分析与 Pixiv 推荐

### 10.1 偏好画像

- `MUST` 从本地 `novels`、`novel_texts`、`series`、`users`、`sources`、`novel_fts` 统计标签、关键词、作者、长度、来源、系列占比、限制等级和热度。
- `MUST` 支持全部归档、收藏、追更、指定作者/标签/时间范围、排除短文和排除失效项等分析范围；默认使用本地可见正文，过滤正文少于 1,000 字的样本。
- `MUST` 画像为版本化 JSON，目标结构包括摘要、正向标签/关键词/题材/关系/情境/语气/节奏/叙事模式、负向排除项、搜索策略、阅读偏差和置信度。当前审计已删除无生产者/消费者的部分正向维度；恢复这些维度时必须同时实现生产、消费和回归测试，不能只加空字段。
- `MUST` 本地统计不依赖 LLM；可选 AI 层基于统计和抽样文本总结偏好，AI 不可用时保留原始统计并优雅降级。
- `MUST` 分析作为后台 job 支持流式进度、失败重试、多个版本、手动命名、删除和一个默认画像。

### 10.2 搜索计划与推荐

- `MUST` 搜索计划包含宽泛、精准、组合和实验性查询、排除词、原因和 limit，并允许用户编辑、保存和去重。
- `MUST` 推荐执行分页搜索、限速、详情补全、候选合并和历史去重；单篇正文少于 5,000 字、系列总字数少于 20,000 字的候选过滤，不设上限。
- `MUST` 默认排除已归档、历史 dismiss、屏蔽作者和屏蔽标签；系列去重必须按 series ID 跨 run 生效，不能只按单篇 novel ID。
- `MUST` 推荐结果保存标题、作者、标签、限制等级、字数、热度、score、命中标签/关键词/偏好、风险说明、来源查询和状态。
- `MUST` 规则分可解释：标签命中权重最高，标题/简介关键词次之，作者偏好和系列长度加分，热度只能轻微加分，负向冲突扣分或剔除；AI 只生成解释，不独占排序控制。
- `MUST` 支持感兴趣、不感兴趣、屏蔽作者、屏蔽标签、加入待阅读/待同步、立即同步单篇/系列；反馈和屏蔽在下一次推荐中生效。
- `PARTIAL` 当前推荐核心与任务日志已有实现证据，但 AI 偏好总结、创作注入、若干 stream 接口和部分前端操作仍是缺口（`x_restrict`/risk 字段与跨 run 系列去重已实现于 `recommendations.py`，2026-08-14 复核结案）。

### 10.3 推荐与 AI 创作连接

- `MUST` 支持在创作向导、长篇规划、章节续写、章节 Pipeline、润色、去 AI 味和内容审计中选择 `preference_profile_id`。
- `MUST` 只向 prompt 注入摘要和结构化偏好，不直接拼接大量正文；注入强度支持关闭、轻度、标准、强化。
- `MUST` 画像默认本地保存，可删除；前端不默认展开敏感正文证据。

## 11. AI 创作工作台

### 11.1 已有创作能力

`CURRENT/PARTIAL` AI 工作台提供 Provider、Agent、续写、改写、创作向导、长篇规划、章节管理、草稿版本、风格蒸馏、小说蒸馏、语义检索、内容审计、自动摘要、伏笔管理和章节 Pipeline。Provider 适配范围以当前代码为准，至少覆盖 OpenAI-compatible、Anthropic、xAI 以及 README/CLAUDE 列出的 Moonshot、Qwen 和自定义 Provider；旧计划中“暂不支持 Gemini”不应被写成永久限制。

### 11.2 项目、章节和 Pipeline

- `MUST` AI 项目保存项目资料、世界观/角色事实、蒸馏内容、风格控制、章节、草稿、状态、摘要和伏笔，保存操作互不覆盖。
- `MUST` 长篇规划、章节细纲、续写、对话润色、心理描写、去 AI 味、审计和状态维护按阶段输出，结构化阶段不能误当正文阶段。
- `MUST` Pipeline 支持续写 → 润色 → 审计等组合，并记录每步状态、错误、摘要和可重试信息；取消不留下半成品。
- `MUST` Retrieval 支持 TF-IDF 和可选 embedding；Qwen embedding 向量按 float32 BLOB 保存，按内容 hash 去重，旧 JSON 向量保持可读，初始化失败时回退 TF-IDF，不对空索引调用 API；运行期远程错误应向调用方明确报告。
- `MUST` 远程 embedding 服务只接收检索所需的章节摘要、关键事件和查询，不得接收 refresh token、Cookie、API key 或默认完整正文；启用前应提示用户确认服务商的数据策略。

### 11.3 风格控制与封面

- `MUST` 项目级风格控制包含 explicitness、lyricism、pacing、darkness、vulgarity 滑块和标签，独立写入 `settings_json.style_control`，不覆盖 `longform_plan`。
- `MUST` 风格注入规划、细纲、续写、Pipeline 续写、对话/心理润色；审计、状态、摘要等结构化任务不注入。
- `MUST` 封面只接受 JPEG、PNG、WebP；扩展名、MIME 和文件魔数同时校验，最大 10 MiB，路径只能位于 `public_dir`，使用原子写入/替换。
- `MUST` 项目总览采用一个主面板和三个分区（资料与进度、蒸馏内容、风格控制），保留三组独立保存操作，不使用嵌套卡片。
- `MUST` 自动写作 `/dashboard/ai` 与创作向导 `/dashboard/wizard` 使用独立模板和状态边界；深链接兼容，自动写作页不初始化向导会话，向导页不初始化章节工作区。

## 12. AI 模型目录、模型池与统一路由

### 12.1 目标和兼容性

- `MUST` Provider 可安全发现并同步结构化模型目录，用户可建立有序 primary/secondary/grok/custom 模型池、后备池和 Agent 绑定。
- `MUST` 既有固定 Agent 自动迁移为 `binding_type=fixed`；ID、Provider、模型、Prompt、参数、启用状态和既有固定调用语义不变。
- `MUST` Agent 支持 fixed 与 pool 两种绑定；固定 Agent 不自动跨 Provider 切换，模型为空时使用 Provider default model，两者为空时在网络请求前返回中文配置错误。
- `MUST` 所有 AI 生成通过单一 `ModelRouter` 入口，业务层不得直接调用 `provider.stream_generate()`。显式豁免：Provider 实现内部、Router 内部，以及 `ai/services/admin.py:458` 的 Provider 连通性测试（该测试的目的就是验证单一 Provider 的直连可用性，不应经过路由与故障转移）。

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

### 12.4 审计、续接和同步

- `MUST` 每个 job 保存候选快照 hash、候选索引、池版本、Agent 版本、Provider 配置 hash 和实际 attempt；attempt 保存状态、阶段、模型、脱敏错误和时间。
- `MUST` `partial` 是正式终态，不等同 running；提供基于原 snapshot 的手动“下一个模型继续”，不得按新配置重新解析候选。
- `MUST` job owner、lease、heartbeat、generation 和终态使用 CAS；stale job 只回收过期租约，不能误杀其他进程的有效任务。
- `MUST` Provider 同步支持 queued/running/needs_empty_confirmation/succeeded/failed/cancelled，空目录必须二次确认；同步 operation 保存最小状态，默认 3 天清理。
- `MUST` 单页同步响应最多 4 MiB、单次最多 20 MiB、最多 100 页和 5,000 个模型；同步总超时 10 分钟；复用现有 SSRF/DNS/Host-SNI/禁重定向/脱敏。
- `DONE` AI 模型目录、模型池与统一路由第一阶段 Task 1-22 已完成，包括 Schema、目录同步、池图与 CAS、`ModelRouter`、全调用链迁移、审计、手工续接、设置页和日志页；验收以对应提交及全量测试为准。
- `OUT` 第一阶段不实现跨任务健康计数、冷却、权重轮询、成本排序和后台定时目录刷新。

## 13. 成人描写局部润色 Agent

- `DONE` 核心流程只处理用户明确选中的一个连续片段；前后文只读，不能整章、多片段或默认接入 Pipeline。实现覆盖 Dashboard 认证、角色确认、Provider scope、固定安全/事实审查、候选校验、乐观锁应用和脱敏存储。
- `MUST` 只接受已认证 Dashboard 会话；未配置 token、未登录、owner 不匹配或可猜 job ID 均拒绝，不能使用 tokenless 单用户例外。
- `MUST` 候选必须通过服务端固定的 `adult_safety_review` 和 `adult_fact_guard`；这两个阶段不是普通 Agent CRUD，也不能被用户编辑或跳过。
- `MUST` 参与者必须是结构化确认的成年虚构人物；未成年人、年龄不明、现实人物、新人物或身份/关系/同意不确定时 fail-closed。
- `MUST` 写作、两项审查均使用统一 `ModelRouter`；成人模块不得直接读取模型池 SQL，不得硬编码 Grok/xAI。
- `MUST` 写作、`adult_safety_review`、`adult_fact_guard` 三个阶段可使用的 Provider 范围必须在启用功能前向用户明确展示并确认。
- `MUST` Provider delta 只在服务端内存缓冲；完整事实、安全、差异和策略校验通过后才能发送候选，partial 缓冲必须丢弃。
- `MUST` 应用时在 `BEGIN IMMEDIATE` 内重验章节 revision、正文和片段 hash、角色事实、策略、binding、owner 和 warning acknowledgment，再以乐观锁写回。
- `MUST` 成人审计输入、通用日志和应用记录不得保存原片段、上下文、完整 Prompt、Provider 原始响应、未完成/安全阻断候选或 API key；完整校验后的未应用候选只允许临时保存在 owner-scoped `ai_jobs.output_text`，应用或保留期清理后删除。
- `PARTIAL` 当前成人请求虽然保留 `preference_profile_id` 与注入强度字段，但阅读页未发送、服务端未将画像注入成人 Prompt；取消回调也未传入 ModelRouter，progress 在同步路由完成后才发送。详见 `docs/AUDIT_REPORT_2026-08-13.md`。

## 14. 视觉、响应式与可访问性

- `MUST` 遵循 Library OS 设计：浅色工作台、稳定的 shell/page 容器、清晰层级、有限圆角、统一 token、可扫描表格和可预测的操作反馈。
- `MUST` 桌面使用约 260px 固定侧栏；1024px 以下切换底部导航，900-1024px 以下的复杂双栏改为单列，移动端页面 padding 保持 16-22px。
- `MUST` 桌面和移动端均不产生横向溢出；固定格式元素使用稳定尺寸，动态文字不得覆盖相邻内容。
- `MUST` 共享组件、按钮、表单、表格、badge、modal、terminal/log block 和空状态遵循 `library-os-style-guide.md`；页面不能嵌套卡片制造多重容器。
- `MUST` 交互控件提供键盘焦点、语义标签、足够对比度、可读错误、加载/空/失败/成功状态；不要把重要信息只放在颜色或 hover 中。
- `MUST` 前端使用安全文本渲染，避免 `v-html`；SSE、任务取消、重试和过期数据均有明确状态。
- `SUPERSEDED` 早期“三角融合”、蓝紫粉渐变 Logo 提案已被 2026-08-04 的 README/静态 Logo 刷新实施替代；当前 Logo 文件和 README 才是视觉事实。

## 15. 部署与运维

- `CURRENT` 支持本机启动、`deploy.sh` 的 venv + Nginx + systemd 部署；旧 `scripts/install_server.sh` 仅作为遗留 timer 部署参考。
- `MUST` 对外部署时 Dashboard 仅监听本机 Flask 端口，由 Nginx/Cloudflare 提供代理和 TLS。
- `PLANNED/待核实` Cloudflare 方案要求固定域名 `pixiv.dongboapp.com`、橙云代理、`Full (strict)`、Origin CA 证书、80 永久跳转 HTTPS、源站 Flask 监听 `127.0.0.1:5011`；私钥只在服务器生成并保持 `0600`，不进入仓库、日志或对话。计划复选框未维护，实际部署状态必须通过服务器核验。
- `MUST` 部署文档说明备份 `data/`、SQLite、`.env`、AI secret 和救援 token 轮换；日志和数据库权限不能向公众开放。
- `OUT` Docker、多账号、插件系统、移动端 App、国际化和更多 Provider 在 README/旧路线图中属于未来愿景，未被当前规格确认。

## 16. 非功能、性能与安全验收

### 16.1 数据和安全

- API key、refresh token、Dashboard token、救援 token、Prompt、正文、请求体、完整响应头不得出现在日志、SSE、快照、attempt 或错误响应中。
- 所有外部 URL、上传文件、模板变量、搜索表达式和导出正文必须经过相应的 SSRF、路径、XSS、注入和大小限制。
- 迁移、目录刷新、模型同步确认、池成员替换、AI 应用和删除恢复必须可回滚或保留上一份有效快照。
- 真实 Pixiv、AI、DNS、部署网络不参与默认 pytest；测试使用临时数据库、目录、HTTP 和 DNS fixture。

### 16.2 性能目标

- 全文搜索目标：1 万本规模下小于 100ms；同步速度目标约 100 本/分钟，实际受限速和远端响应影响。
- 救援目录第一页目标不超过 500ms，完整刷新目标不超过 10 秒；列表查询不得读取全文正文。
- 单 job 的候选、请求和总 deadline 遵守第 12.3 节上限；模型同步遵守第 12.4 节上限。
- AI 使用 SSE 流式输出，正常路径首 token 目标小于 2 秒；超时、取消和断连必须释放资源。

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
- 任务状态、单 active job、调度 cron、日志投影、3 天清理和取消终态。
- 救援资格、覆盖优先级、目录重建/增量回滚、来源筛选、stale/503、Token 和 userscript 安全。
- 偏好空数据、短文本过滤、画像 JSON、推荐去重/评分/反馈/屏蔽、AI 不可用降级。
- Provider SSRF、模型规范化、池循环/CAS、路由切换、partial、续接快照和敏感字段脱敏。
- 成人 Agent 认证、成年事实、策略不可编辑、事实保护、hash/revision 乐观锁和日志脱敏。
- 前端深链接、响应式溢出、键盘可达、加载/空/错误态、SSE 重连和不使用 `v-html`。

### 17.3 覆盖率说明

当前 `pyproject.toml` 没有强制覆盖率阈值。归档路线图提出过“核心同步逻辑 80%”这一历史目标，除非重新批准，不将其视为当前发布阻断条件；行为风险应通过针对性回归测试覆盖。

## 18. 实现状态与优先级

### 18.1 已有基础（`DONE/CURRENT`）

- 核心 Pixiv 同步、SQLite/文件归档、Web 仪表盘、EPUB、任务调度和共享 JobRunner。
- 任务取消硬化、Provider key 加密、SSRF/DNS/代理信任边界、AI 导入校验、测试隔离和多轮审计修复。
- 偏好本地统计、推荐核心、反馈/屏蔽基础、统一日志投影、关键词清洗和项目风格后端。
- 救援单项实时 API、userscript、正文完整度字段、预计算目录和来源展示的基础 Task 1-5。
- AI 模型目录、模型池与统一路由第一阶段 Task 1-22：目录同步、池图与 CAS、`ModelRouter`、全调用链、审计、`partial`、手工续接、设置页和日志页。

### 18.2 当前主线（`MUST/PLANNED`）

1. 完成救援目录剩余纠错/删除接线、前端完整筛选、部署性能验收和全量回归。
2. 完成偏好 AI 总结、推荐失败/取消隔离、搜索计划 CRUD、屏蔽标签/待同步/立即同步和成人入口的偏好注入。
3. 补齐成人局部润色的实时 progress、取消/断连传播和对应回归测试；核心安全审查与事实保护边界已实施。

### 18.3 后续体验改进

- 完成 AI 项目总览单面板、自动写作/向导模板拆分的最终复核和视觉回归。
- 完成封面在小说库、AI 阅读和项目总览的一致展示；补充风格控制 UI 和标签。
- 统一任务日志筛选、详情、过期提示和移动端布局；保持 AI `ai_jobs` 与同步 `task_logs` 的分表边界。

## 19. 历史候选与明确非目标

以下需求曾出现在归档路线图、审计建议或早期愿景中，但没有被当前统一规格确认。它们必须作为候选记录，不能在实现中默认展开：

- 首次配置向导（Token → 同步配置 → 首次同步）。
- 统一 API 错误响应的全面重构、Blueprint 拆分、PostgreSQL 抽象和更大规模存储重构。
- Docker 部署、移动端 App、多账号、插件系统、国际化、监控告警。
- 高级 embedding/多模型投票、定时自动推书、复杂社交推荐和成本/权重调度。
- 自动点赞、收藏、关注、评论、绕过 Pixiv 限制、默认下载全部推荐全文。
- 成人整章/多片段批处理、外部身份猜测、大型 NLP 编辑器依赖。

旧归档中已经完成的并发、外键、事务、模块拆分、CLI/Web 共享任务和 Qwen embedding 工作，作为当前质量约束继承，但不重复创建实施任务。

## 20. 来源覆盖与文档治理

### 20.1 正式文档来源

| 来源组 | 文件 |
|--------|------|
| 根目录与开发指导 | `README.md`、`CLAUDE.md`、`assets/logo-design.md` |
| 当前参考 | `docs/INDEX.md`、`docs/AUDIT_REPORT_2026-07-02.md`、`docs/AUDIT_REPORT_2026-07-03.md`、`docs/AUDIT_REPORT_2026-08-13.md`、`docs/frontend-api-contract.md`、`docs/frontend-pages.md`、`docs/library-os-style-guide.md`、`docs/JOB_SYSTEM.md`、`docs/MODEL_ROUTING_GUIDE.md`、`docs/RESCUE_USER_GUIDE.md`、`docs/ADULT_POLISH_USER_GUIDE.md`、`docs/PREFERENCE_RECOMMENDER_REQUIREMENTS.md`、`docs/QWEN_EMBEDDING_INTEGRATION.md` |
| 历史顶层快照 | `docs/API_COMPLETE.md`、`docs/AI_WRITING_STUDIO_PLAN.md`、`KNOWLEDGE_GRAPH.md` |
| 活跃规格与计划 | `docs/superpowers/specs/` 与 `docs/superpowers/plans/`；AI 模型第一阶段以 `2026-07-27-ai-model-catalog-pools-unified-requirements.md` 和已完成实施计划为追溯基线 |
| 归档顶层 | `docs/archive/` 下审计、完成报告、优化路线图和模块拆分文档 |
| 归档 superpowers | `docs/archive/superpowers/plans/` 与 `docs/archive/superpowers/specs/` 下 Qwen embedding、CLI/Web Job、统一任务队列文档 |

### 20.2 补充资料来源

- `.superpowers/sdd/`：模型 Task 1-4 brief/report、救援 Task 1-5 brief/report、进度账本及 review diff；只接受能与提交、最终报告或最新统一需求相互印证的内容。
- `.monkeycode/specs/novel-tabs-and-user-detail/requirements.md` 与 `design.md`：小说标签、系列详情、关注用户状态、用户详情和全量备份需求。
- `memory/`：2026-07-06 审计和八项优化进度；仅用于发现未回写的缺口，不作为正式契约。

### 20.3 已知文档质量问题

1. `.superpowers/sdd/progress.md` 的 AI Task 3 状态落后于 Task 3 report、提交记录和 2026-07-27 统一需求，应以后者为准。
2. `.superpowers/sdd/task-1-brief.md` 与 `model-task-1-brief.md` 内容相同，无法与同名救援 report 可靠配对；必须用模型前缀和最终报告消歧。
3. 救援 Task 3 report 含多轮复审，后文修正了前文的 meta、删除和历史 membership 语义，应采用文件最后的复审结论。
4. 多份计划复选框未维护，但代码和下游文档已证明部分能力存在；状态不能只由 checkbox 推断。
5. Cloudflare 计划未勾选，而救援设计把 HTTPS 写作已有约束；实际部署状态必须通过服务器检查，不能仅凭仓库文档宣称完成。
6. 当前 API 同时存在 raw JSON 与 `{ok,data}`/`{ok,error}` envelope；统一响应形状是后续重构目标，不能假设现有所有端点已经一致。
7. README 写“10 个定时任务”，历史知识图谱出现过 8/9 个版本；准确数量必须从当前 task registry 生成，不采用历史硬编码数字。
8. Embedding 索引文件名在文档间存在差异；实际名称必须以当前代码和迁移测试为准。

## 21. 维护规则

- 新增或变更功能时，先更新本文件对应的需求、状态和来源，再修改 README、API 契约、页面清单或实施计划。
- 任何新增 `SUPERSEDED` 关系必须说明替代文件、替代范围和仍保留的接口部分。
- 需求状态变更必须附提交、测试或完成报告；“计划复选框已勾选”不足以证明实现完成。
- API、数据库 Schema、认证、安全和 userscript 变更必须同时更新契约、回归测试和本文件的验收矩阵。
- 归档文件只追加历史说明，不重新成为当前目标；真正重新启用历史候选时，必须创建新的已确认规格并注明范围。
