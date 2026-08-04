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

业务生成路径统一经过 ModelRouter；固定 Agent 保持原 Provider/模型语义，池绑定 Agent 才会按候选顺序 fallback。

模型目录可通过 \`/api/dashboard/ai/providers/<provider_id>/models/sync\` 同步，也可以保留手工模型。模型池会按成员顺序和后备池展开候选；单个 job 最多尝试 16 个候选、发起 32 次网络请求、运行 30 分钟。

### 智能推荐

/dashboard/preferences 提供偏好画像、搜索计划、推荐任务、结果反馈和屏蔽管理。推荐逻辑以本地统计和可解释规则为主，AI 用于补充总结和解释。

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
- [贡献指南](AGENTS.md)：目录结构、命令、测试和 PR 约定。

## Deploy

根目录 deploy.sh 是推荐的 Web 部署入口，会配置 venv、Nginx 和 systemd。scripts/install_server.sh 仅保留给旧 timer 同步部署场景。

对外部署时务必设置 DASHBOARD_TOKEN，并使用 HTTPS。备份时同时保存 data/、SQLite 数据库、.env、AI secret 和救援 Token。

## License

本项目采用 [MIT License](LICENSE) 开源协议。

## Support

- 问题反馈：[GitHub Issues](https://github.com/dong-jpg/pixiv-novel-sync/issues)
- 功能建议：[GitHub Discussions](https://github.com/dong-jpg/pixiv-novel-sync/discussions)
