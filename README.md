<p align="center">
  <img src="assets/main-logo.svg" width="112" height="112" alt="Pixiv Novel Sync：书页与同步环图标">
</p>

# Pixiv Novel Sync

**把喜欢的小说，留在自己的书库里。**

同步 Pixiv 小说收藏，归档正文，在自己的私人书库中检索、阅读与发现下一部作品。

[书库体验](#书库体验) · [快速开始](#快速开始) · [部署与安全](#部署与安全) · [可选 AI](#可选进阶) · [文档](#文档与开发)

## 分支选择

- **main：私人书库。** 面向同步、归档、阅读和规则推荐；main 不包含写作模块。
- **写作需求：** 请使用独立维护的 [ai-writing 分支](https://github.com/dong-jpg/pixiv-novel-sync/tree/ai-writing)，项目、章节和创作向导不在本分支中提供。

## 书库体验

- **同步与追更**：同步公开收藏、私密收藏、关注作者的作品和追更系列，可手动运行或按计划更新。
- **本地归档**：保存正文、作者、标签、系列与已下载的封面和插图，支持全文检索和待删除恢复。
- **移动阅读**：在手机浏览器阅读、保存阅读进度，也可导出 EPUB 到自己的阅读器。
- **规则推荐**：依据本地统计和规则整理阅读偏好、搜索 Pixiv 并筛选候选，支持反馈及屏蔽作者、标签。
- **本地救援**：从私人备份中查找失效作品，书库入口为 `/dashboard/novels?category=rescue`。

已归档文件可以本地保存或导出阅读；网页仍使用 CDN，不承诺完整离线网页。

### 原站救援阅读

救援仅读取已经存在的私人本地备份，不能恢复从未归档的作品，也不能绕过访问限制。

在 `/dashboard/settings/system` 生成独立救援 Token（明文只显示一次），安装 [用户脚本](userscripts/pixiv-rescue.user.js) 并在脚本菜单配置 Token，即可在 Pixiv 原站明确失效的小说或系列页读取备份。正常页面不会请求救援 API。

这需要支持用户脚本扩展的浏览器；安卓原生 Chrome 默认不支持。安装与连接设置见 [救援指南](docs/RESCUE_USER_GUIDE.md)。

## 快速开始

需要 Git 和 Python ≥ 3.10。项目使用 Flask + SQLite，无需前端构建。

### 1. 选择 main 并创建环境

~~~bash
git clone --branch main --single-branch https://github.com/dong-jpg/pixiv-novel-sync.git
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

Linux Web 部署入口是 [deploy.sh](deploy.sh)，使用 APT、Nginx 与 systemd，并配置虚拟环境。执行前先审阅脚本并备份；更新现有安装时会将安装目录代码重置为 `origin/main`。

[scripts/install_server.sh](scripts/install_server.sh) 仅保留给旧 timer 同步部署，不是新版 Web 入口。

- **公网访问**：公网或反向代理部署必须为 `DASHBOARD_TOKEN` 设置强随机访问密码，并使用 HTTPS；留空时仅允许本机访问。HTTPS 下显式配置 `PIXIV_COOKIE_SECURE=1`，不要在公网关闭 Secure cookie。
- **会话密钥**：固定 `PIXIV_FLASK_SECRET` 并保持稳定，用于会话签名；变更后需要重新登录。
- **设备登录**：仅在私人设备选择“保持登录 30 天”（默认不勾选）。会话固定到期，不因访问无限续期；密码不保存到浏览器缓存。
- **敏感数据与备份**：不要公开 `.env`、令牌、数据库、私密备份与日志，也不要提交到仓库。备份应覆盖 `.env`、`config/config.yaml`、SQLite 数据库和归档文件；使用 AI 或救援时，同时妥善保管相应密钥与 Token。
- **任务与留存**：任务日志默认保留 14 天，通过 `sync.task_log_retention_days` 调整（系统维护页可改）。自动同步与限速在 `config/config.yaml` 中配置，详见 [任务系统](docs/JOB_SYSTEM.md)。

## 可选进阶

无需配置 AI Provider，也能同步、阅读和使用规则推荐。main 的 AI 生成仅用于偏好关键词清洗（`clean_keywords`），失败时降级为原始统计词；未接入 AI 偏好总结或 AI 推荐解释。

- 在 `/dashboard/settings/models` 管理 Provider、模型目录和有序模型池；在 `/dashboard/settings/agents` 选择固定模型或模型池，并预览候选链。
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

问题与功能建议请提交 [GitHub Issues](https://github.com/dong-jpg/pixiv-novel-sync/issues)，交流使用经验可前往 [GitHub Discussions](https://github.com/dong-jpg/pixiv-novel-sync/discussions)。
