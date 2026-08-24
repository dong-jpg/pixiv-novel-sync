<div align="center">
  <img src="assets/logo.svg" alt="Pixiv Novel Sync" width="420"/>

  # Pixiv Novel Sync

  <strong>从收藏，到灵感，再到下一章。</strong>

  <p>本地归档 Pixiv 小说，整理创作素材，用 AI 辅助写作，并基于阅读偏好发现新作品。</p>

  ![Python](https://img.shields.io/badge/Python-3.10+-2A211B.svg)
  ![Flask](https://img.shields.io/badge/Flask-3.x-B75A3C.svg)
  ![SQLite](https://img.shields.io/badge/SQLite-local--first-4A7C74.svg)
  ![License](https://img.shields.io/badge/license-MIT-6E5D50.svg)

  <a href="#features">Features</a> ·
  <a href="#quick-start">Quick Start</a> ·
  <a href="#docs">Docs</a>
</div>

---

## Features

<table>
<tr>
<td width="33%" valign="top">

### Library

- 同步公开收藏、私密收藏、关注用户作品和追更系列。
- 保存标题、标签、作者、系列、正文、封面和插图等本地资料。
- 提供全文搜索、阅读进度、EPUB 导出和待删除恢复。
- 通过 userscript 在 Pixiv 原站失效页面读取本地救援备份。

</td>
<td width="33%" valign="top">

### Writing Studio

- 管理 AI 项目、长篇规划、章节、草稿和 Pipeline。
- 支持续写、改写、润色、内容审计、摘要和伏笔维护。
- 可从范文或本地小说蒸馏风格、设定和上下文。
- 支持 Provider 模型目录、有序模型池和统一 ModelRouter fallback。

</td>
<td width="33%" valign="top">

### Discovery

- 从本地小说库统计标签、关键词、作者、长度和来源偏好。
- 生成搜索计划并自动执行 Pixiv 检索。
- 对候选作品打分、去重、解释命中理由并保存结果。
- 支持不感兴趣、屏蔽作者、屏蔽标签和反馈闭环。

</td>
</tr>
</table>

## Quick Start

### 1. 创建虚拟环境

~~~bash
python -m venv .venv
~~~

Linux / macOS:

~~~bash
source .venv/bin/activate
~~~

Windows PowerShell:

~~~powershell
.venv\Scripts\Activate.ps1
~~~

### 2. 安装

~~~bash
pip install -e .
~~~

开发和测试环境：

~~~bash
pip install -e ".[test]"
~~~

### 3. 创建本地配置

~~~bash
cp .env.example .env
cp config/config.yaml.example config/config.yaml
~~~

至少在 .env 中配置 Pixiv refresh token。也可以启动服务后访问 http://localhost:5010/token-login 通过 OAuth 登录生成 token。

### 4. 启动 Web UI

~~~bash
pixiv-novel-sync web-token-ui
~~~

默认访问 http://127.0.0.1:5010/dashboard。

## Core Workflows

### 同步归档

在 Dashboard 中启动同步任务，或通过 CLI 手动同步主要来源：

~~~bash
pixiv-novel-sync sync bookmark following_novels subscribed_series
~~~

任务统一写入日志页，可取消、筛选和查看进度；任务日志默认保留 3 天。自动同步的 interval 和 cron 配置在 config/config.yaml 中维护。

### AI 创作

- /dashboard/ai：项目、长篇规划、章节、草稿、Pipeline 和 AI 小说库。
- /dashboard/wizard：创作向导、蒸馏档案和导入流程。
- /dashboard/settings：AI Provider、模型目录、模型池和 Agent 绑定。

所有业务生成路径统一经过 ModelRouter。固定 Agent 保持指定 Provider/模型语义，池绑定 Agent 按候选快照顺序执行 fallback；一次请求可能触达多个 Provider，前端必须展示并确认完整 Provider 范围。

模型目录可通过 \`/api/dashboard/ai/providers/<provider_id>/models/sync\` 同步，也可以保留手工模型。模型池按成员顺序和后备池展开候选；单个 job 最多尝试 16 个候选、发起 32 次网络请求、运行 30 分钟。

### 成人本地润色 Agent

成人润色只处理用户在章节阅读页选中的连续片段。配置顺序是：

1. 设置稳定的 `DASHBOARD_TOKEN`，并登录 Dashboard。
2. 在 Provider 模型目录中同步或手工添加可路由模型，再按需要建立固定或池绑定。
3. 创建或启用 `task_type=adult_polish` 的 Agent；普通 Agent CRUD 不会暴露它的删除/停用入口。
4. 在设置页为 `safety` 和 `fact_guard` 两个 review binding 配置支持 `json` 的固定模型或模型池。
5. 建立结构化的虚构角色记录，填写年龄依据和 `fictional=true`；启用项目成人内容后，确认当前角色 revision。
6. 阅读页先获取并确认当前 Provider scope，再生成候选；warning、Provider scope、角色或章节 revision 变化都必须重新生成。

成人路由不支持无 token 的本地单用户例外，也不会自动加入普通 Pipeline。候选正文仅在未应用期间按三天策略保留；应用后任务正文会清理，应用记录只保留章节/候选/校验/策略和 Provider snapshot hash 等元数据，不保留正文。固定安全策略、两阶段 JSON review、角色事实、锁定词和章节范围任一校验失败都会 fail closed。连接中断时可使用同一 job 的 signed events 恢复脱敏校验和候选状态。完整的请求字段、SSE 事件和错误语义见 [`docs/frontend-api-contract.md`](docs/frontend-api-contract.md)。

### 智能推荐

/dashboard/preferences 提供偏好画像、搜索计划、推荐任务、结果反馈和屏蔽管理。当前推荐逻辑以本地统计和可解释规则为主；AI 仅用于关键词清洗，结构化偏好总结和 AI 推荐解释仍未接入。

推荐既可在页面手动触发，也可在 /dashboard/settings#scheduler 配置定时执行（`auto_sync_recommendation_run_*`，默认关闭）。启用前需先生成默认偏好画像，否则任务会失败；定时推荐与其他同步任务共用同一个任务队列，不会并行抢 Pixiv 搜索配额。

### Pixiv 原站救援阅读

1. 在 /dashboard/settings#rescue-api 生成独立救援 Token，明文只显示一次。
2. 安装 userscripts/pixiv-rescue.user.js，并通过油猴菜单写入救援 Token。
3. 脚本只在 Pixiv 小说或系列明确失效时读取本地备份；正常页面不会请求救援 API。

Dashboard 内的救援目录入口是 /dashboard/novels?category=rescue。

## Configuration & Security

| 配置 | 用途 |
|------|------|
| .env | 本地 secret、Pixiv token、Dashboard token 和 AI 加密密钥 |
| config/config.yaml | 同步任务、限速、存储目录、自动调度和 cron |
| DASHBOARD_TOKEN | 公网或反向代理部署必须配置；留空时仅允许本机访问 |
| PIXIV_NOVEL_SYNC_AI_SECRET_KEY | 保存 AI Provider key 前必须配置，配置后应保持稳定 |

不要提交 .env、data/、生成数据库、日志或真实 token。AI Provider key 会加密保存；模型池 fallback 可能把同一 Prompt 发给多个 Provider，保存配置前请确认数据范围。

## Development

项目代码位于 src/pixiv_novel_sync/：

~~~text
src/pixiv_novel_sync/
├── cli.py                 # 命令入口
├── webapp.py              # Flask 应用工厂
├── ai/                    # Provider、ModelRouter、创作服务和检索
├── jobs/                  # 共享 JobSpec、JobRunner 和任务分派
├── storage/               # SQLite mixin、schema 和各领域存储
├── web/                   # Web 管理器和工具函数
└── templates/             # 服务端渲染页面和 Vue islands
~~~

常用检查：

~~~bash
python -m pytest -q
python -m pytest tests/test_preferences.py -q
python -m compileall -q src
git diff --check
~~~

当前 pyproject.toml 只配置 pytest 的 testpaths 和 pythonpath；Black、Flake8、Pylint、mypy 尚未作为仓库必跑工具配置。

## Docs

- [文档索引](docs/INDEX.md)：当前文档入口、历史归档和开发计划。
- [统一项目需求](docs/UNIFIED_PROJECT_REQUIREMENTS.md)：现行需求、状态和来源追溯。
- [前端 API 契约](docs/frontend-api-contract.md)：Dashboard 页面依赖的 API 形状。
- [页面清单](docs/frontend-pages.md)：路由、模板和前端入口。
- [任务系统](docs/JOB_SYSTEM.md)：Job 管线、状态机、取消协议和 auto_sync 配置。
- [开发约定](CLAUDE.md)：目录结构、命令、测试和代码约定。

## Deploy

根目录 deploy.sh 是推荐的 Web 部署入口，会配置 venv、Nginx 和 systemd。scripts/install_server.sh 仅保留给旧 timer 同步部署场景。

对外部署时务必设置 DASHBOARD_TOKEN，并使用 HTTPS。备份时同时保存 data/、SQLite 数据库、.env、AI secret 和救援 Token。

## License

本项目采用 [MIT License](LICENSE) 开源协议。

## Support

- 问题反馈：[GitHub Issues](https://github.com/dong-jpg/pixiv-novel-sync/issues)
- 功能建议：[GitHub Discussions](https://github.com/dong-jpg/pixiv-novel-sync/discussions)
