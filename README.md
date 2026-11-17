<p align="center">
  <img src="assets/main-logo.svg" width="112" height="112" alt="Pixiv Novel Sync：书页与同步环图标">
</p>

# Pixiv Novel Sync

**把喜欢的小说，留在自己的书库里。**

同步 Pixiv 小说收藏，归档正文，在自己的私人书库中检索、阅读与发现下一部作品。

[书库体验](#书库体验) · [快速开始](#快速开始) · [部署与安全](#部署与安全) · [可选 AI](#可选进阶) · [文档](#文档与开发)

## 分支选择

- **main：私人书库。** 包含同步、归档、阅读、推荐、偏好 / XP 分析及远程执行试点；main 不包含写作模块。
- **ai-writing：书库与写作。** [ai-writing 分支](https://github.com/dong-jpg/pixiv-novel-sync/tree/ai-writing)包含 main 的全部功能，额外提供创作项目、章节、创作向导、风格蒸馏与润色。共享改动先进入 main，再合并到 ai-writing。

## 当前 ai-writing 分支

此分支继承 main 的完整私人书库，并提供 AI 创作项目、章节、创作向导、风格蒸馏、检索及成人局部润色。写作入口为 `/dashboard/ai`，额外配置位于 `/dashboard/settings/adult`。写作相关操作与限制见 [写作指南](docs/AI_WRITING_GUIDE.md)、[润色指南](docs/ADULT_POLISH_USER_GUIDE.md)和 [检索配置](docs/QWEN_EMBEDDING_INTEGRATION.md)。下方快速开始安装当前 ai-writing 分支；只需要书库时使用 main。

## 书库体验

- **同步与追更**：同步公开收藏、私密收藏、关注作者的作品和追更系列，可手动运行或按计划更新。
- **本地归档**：保存正文、作者、标签、系列与已下载的封面和插图，支持全文检索和待删除恢复。
- **移动阅读**：在手机浏览器阅读、保存阅读进度，也可导出 EPUB 到自己的阅读器。
- **规则推荐**：依据本地统计和规则整理阅读偏好、搜索 Pixiv 并筛选候选，支持反馈及屏蔽作者、标签。
- **本地救援**：从私人备份中查找失效作品，书库入口为 `/dashboard/novels?category=rescue`。
- **可选偏好学习**：结合已归档作品报告和明确的读后评价生成个人偏好 / XP 画像；全库及新增作品分析受累计授权预算限制。
- **远程执行试点**：VPS 保留书库，InstaBox 按需执行诊断和单本 EPUB 导出。收藏、关注与追更同步仍在 VPS 本地执行，详见 [远程能力边界](docs/REMOTE_EXECUTION.md)。

已归档文件可以本地保存或导出阅读；网页的 Vue 仍从 CDN 加载（样式已随仓库提供），不承诺完整离线网页。

### 原站救援阅读

救援仅读取已经存在的私人本地备份，不能恢复从未归档的作品，也不能绕过访问限制。

通过 [直装链接安装用户脚本](https://raw.githubusercontent.com/dong-jpg/pixiv-novel-sync/main/userscripts/pixiv-rescue.user.js)（[源码](userscripts/pixiv-rescue.user.js)），再在脚本菜单填写已有的独立救援 Token。尚未配置 Token 时，到 `/dashboard/settings/system` 生成并妥善保存（明文只显示一次；轮换会使所有设备的旧 Token 失效）。正常可读的小说 / 系列页不请求备份内容、不替换原内容；失效或状态不明时可查询备份，显式救援链接可直接打开备份阅读层。列表页可做一次 Token 探测，只重建失效卡片；手机新作页的「拯救系列」tab 则专门展示救援系列。Token 只保存在脚本的特权存储中、通过 `Authorization` 请求头发送，不写入 Pixiv 页面 DOM、Cookie 或查询参数。

脚本预设当前自用服务器，支持的管理器可通过同一 GitHub 来源检查更新，无需上架油叉；若浏览器只显示源码，按救援指南从链接或源码导入。

这需要支持用户脚本扩展的浏览器；安卓原生 Chrome 默认不支持。安装与连接设置见 [救援指南](docs/RESCUE_USER_GUIDE.md)。

## 快速开始

需要 Git 和 Python ≥ 3.10。项目使用 Flask + SQLite，无需前端构建。

### 1. 选择 ai-writing 并创建环境

~~~bash
git clone --branch ai-writing --single-branch https://github.com/dong-jpg/pixiv-novel-sync.git
cd pixiv-novel-sync
python -m venv .venv
~~~

### 2. 激活环境并安装

Linux / macOS：

~~~bash
source .venv/bin/activate
~~~

Windows PowerShell：

~~~powershell
.venv\Scripts\Activate.ps1
~~~

激活后安装：

~~~bash
pip install -e .
~~~

### 3. 复制本地配置

首次安装时复制示例；已有配置请勿覆盖。

Linux / macOS：

~~~bash
cp .env.example .env
cp config/config.yaml.example config/config.yaml
~~~

Windows PowerShell：

~~~powershell
Copy-Item .env.example .env
Copy-Item config/config.yaml.example config/config.yaml
~~~

同步前在 `.env` 填写 `PIXIV_REFRESH_TOKEN`，也可启动后通过 [本地 Token 登录](http://127.0.0.1:5010/token-login) 获取。追更系列另需配置登录 Pixiv 网页后的 `PIXIV_WEB_COOKIE`。

### 4. 启动书库与首次同步

~~~bash
pixiv-novel-sync web-token-ui
~~~

访问 [本地书库](http://127.0.0.1:5010/dashboard)。默认仅监听 `127.0.0.1:5010`，不直接对公网开放。

在 Dashboard 中启动同步，或另开位于项目目录、已激活虚拟环境的终端执行：

~~~bash
pixiv-novel-sync sync bookmark following_novels subscribed_series
~~~

## 部署与安全

Linux Web 部署入口是 [deploy.sh](deploy.sh)，使用 APT、Nginx 与 systemd，并配置虚拟环境。执行前先审阅脚本并备份；新安装默认 main（可设 `PIXIV_BRANCH=ai-writing`）。更新保留当前分支，只从 `origin/main` 或 `origin/ai-writing` 快进，有未提交代码或分叉时中止。[update.sh](update.sh)保留配置与旧提交记录，并核对本机健康接口；它不代替数据库和归档备份。

[scripts/install_server.sh](scripts/install_server.sh) 仅保留给旧 timer 同步部署，不是新版 Web 入口。

- **公网访问**：公网或反向代理部署必须为 `DASHBOARD_TOKEN` 设置强随机访问密码，并使用 HTTPS；留空时仅允许本机访问。HTTPS 下显式配置 `PIXIV_COOKIE_SECURE=1`，不要在公网关闭 Secure cookie。
- **会话密钥**：固定 `PIXIV_FLASK_SECRET` 并保持稳定，用于会话签名；变更后需要重新登录。
- **设备登录**：仅在私人设备选择“保持登录 30 天”（默认不勾选）。会话固定到期，不因访问无限续期；密码不保存到浏览器缓存。
- **敏感数据与备份**：不要公开 `.env`、令牌、数据库、私密备份与日志，也不要提交到仓库。备份应覆盖 `.env`、`config/config.yaml`、SQLite 数据库和归档文件；使用 AI 或救援时，同时妥善保管相应密钥与 Token。
- **任务与留存**：任务日志默认保留 14 天，通过 `sync.task_log_retention_days` 调整（系统维护页可改）。自动同步与限速在 `config/config.yaml` 中配置，详见 [任务系统](docs/JOB_SYSTEM.md)。

## 可选进阶

无需配置 AI Provider，也能同步、阅读和使用规则推荐。可选 AI 提供关键词清洗、已归档单篇/系列作品报告，以及基于明确阅读评价和有效作品报告的个人画像 AI 总结；推荐用可核对的反馈与画像特征生成搜索词和评分。实现、验收与实际部署状态见 [项目现状](docs/STATUS.md)。

关键词清洗（`clean_keywords`）失败时保留既有清洗结果或原始统计词，普通本地统计继续可用。推荐解释由确定性规则给出，能追溯命中的评价和画像特征。

- 在 `/dashboard/preferences?view=work-analysis` 选择一篇已归档小说或一个已归档系列，先核对当前正文范围、章序、可能使用的 Provider/模型及累计预算，再显式开始。整部优先，仅输入容量超限时分章/块；报告附源版本与短证据，输入覆盖、原作完整性和模型解释可靠性分开显示，不代表已核实完整原作或用户偏好。此入口只处理本次选择，不调用 Pixiv 补章。
- 在 `/dashboard/preferences?view=learning` 管理全库批次、后续新增/更新作品的自动分析，以及个人偏好画像。持续策略默认关闭，先确认范围、模型、每作品上限和跨批次累计总预算；同版本报告与已尝试目标不会反复付费，重启不重置额度或重播调用。批次发现结束后，仍可逐部查看报告状态。
- 阅读页与系列页可明确记录喜欢、不喜欢、中立或撤回。归档、收藏、感兴趣和仅已读都不会自动记为喜欢；个人画像只使用评价与有效报告，并需显式启用到推荐。反馈更新会影响下一轮搜索和排序，撤回或删除来源会使相关画像失效。操作步骤见 [偏好学习指南](docs/PREFERENCE_LEARNING_GUIDE.md)。
- 作品分析的暂停、取消与恢复使用业务检查点和累计预算，不开放旧 main AI job continue。正文不保存为完整输入重放副本，有界短证据可能与极短章节重合；正文/报告不进入普通调用历史、日志或浏览器持久缓存。分析可删除，但不承诺备份/WAL 的物理清除。
- AI 配置与单次文本调用基础设施独立运行，不依赖 Magpie 或额外 Agent 框架。
- 在 `/dashboard/settings/models?view=providers|models|groups` 管理 Provider、模型目录和路由组（原模型池）；旧 URL 不变。在 `/dashboard/settings/agents` 的“AI 任务”页选固定模型或路由组，查看配置候选链，并显式输入测试文本做零网络容量预检。
- 任务设置页的预检展示已保存的 system prompt 和完整 user 文本，按候选分别估算，不读取小说或创建 AI job；1M 候选可与被排除的 128k 候选并列，估算通过不代表服务商已接受真实 1M 请求。日志只展示服务端安全计划、业务校验与已报告用量；未知不按零处理，旧 AI job Continue 不可用。
- 模型目录可通过 `/api/dashboard/ai/providers/<provider_id>/models/sync` 同步，也可保留手工模型。模型池按成员顺序和后备池展开；单个 job 最多尝试 16 个候选、发起 32 次网络请求、运行 30 分钟。
- 保存 Provider API key 前须设置并保持 `PIXIV_NOVEL_SYNC_AI_SECRET_KEY` 稳定，用于加密保存凭据。跨 Provider fallback 可能将同一 Prompt 发给多个 Provider；启用前确认完整 Provider 范围，只提交你愿意交给这些服务的数据。

配置细节与调用预算见 [模型路由指南](docs/MODEL_ROUTING_GUIDE.md)。

## 文档与开发

- [文档索引](docs/INDEX.md)：使用说明、版本状态、历史报告和开发计划。
- [前端 API 契约](docs/frontend-api-contract.md) 与 [页面清单](docs/frontend-pages.md)：接口、路由和前端入口。
- [开发约定](CLAUDE.md)：目录结构、命令与代码约定。

代码位于 [src/pixiv_novel_sync/](src/pixiv_novel_sync/)。安装测试依赖并运行：

~~~bash
pip install -e ".[test]"
python -m pytest -q
~~~

## 许可证与反馈

本项目采用 [MIT License](LICENSE)。

问题、功能建议与使用交流请提交 [GitHub Issues](https://github.com/dong-jpg/pixiv-novel-sync/issues)。
