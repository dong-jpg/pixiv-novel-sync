<div align="center">
  <img src="assets/logo.svg" alt="Pixiv Novel Sync" width="200"/>
  
  # Pixiv Novel Sync
  
  **小说归档 · AI 创作 · 智能推荐**
  
  ![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)
  ![Flask](https://img.shields.io/badge/Flask-3.0+-green.svg)
  ![License](https://img.shields.io/badge/license-MIT-orange.svg)
  ![Status](https://img.shields.io/badge/status-active-success.svg)
  
  [功能特性](#-功能特性) • [快速开始](#-快速开始) • [使用文档](#-使用文档) • [开发指南](#-开发指南) • [常见问题](#-常见问题)
</div>

---

## 📖 项目简介

**Pixiv Novel Sync** 是一个功能强大的 Pixiv 小说管理平台，集成了**增量归档**、**AI 创作辅助**、**智能推荐**三大核心功能。

### 为什么选择它？

- 🔄 **自动化归档** - 定时同步收藏、关注、追更，永久保存喜爱的作品
- 🤖 **AI 创作工作台** - 续写、改写、长篇规划，支持多种 AI 模型
- 🎯 **智能推荐系统** - 基于阅读偏好自动发现新作品
- 📱 **现代 Web 界面** - 响应式设计，桌面/移动端都能流畅使用
- 🔐 **隐私优先** - 本地运行，数据完全掌控
- 🚀 **高性能** - SQLite + 文件系统双存储，全文搜索秒级响应

---

## ✨ 功能特性

### 🗂️ 全面的同步能力

<table>
<tr>
<td width="50%">

#### 📚 内容同步
- ✅ 收藏小说（公开+私密）
- ✅ 关注用户作品
- ✅ 追更系列
- ✅ 小说元数据（标题/简介/标签/统计数据）
- ✅ 完整正文（原始文本 + Markdown）
- ✅ 封面与插图下载

</td>
<td width="50%">

#### ⏰ 自动化管理
- ✅ 10 个独立定时任务
- ✅ Cron 表达式自定义
- ✅ 智能限速与顺延机制
- ✅ 状态检查（用户/小说/系列）
- ✅ 待删除检测与确认
- ✅ 任务日志与进度监控

</td>
</tr>
</table>

### 🤖 AI 创作工作台

<table>
<tr>
<td width="33%">

#### ✍️ 创作辅助
- AI 续写
- AI 改写
- 创作向导
- 长篇规划
- 章节管理
- 草稿版本控制

</td>
<td width="33%">

#### 🎨 内容优化
- 对话润色
- 心理描写增强
- 去 AI 味处理
- 内容审计
- 自动摘要生成
- 伏笔管理

</td>
<td width="33%">

#### 🔌 多模型支持
- OpenAI (GPT-4/o1)
- Anthropic (Claude)
- xAI (Grok)
- Moonshot
- Qwen/通义千问
- 自定义 Provider

</td>
</tr>
</table>

**特色功能**:
- 📝 **写作项目系统** - 项目化管理长篇创作
- 🧠 **语义检索** - TF-IDF + Embedding 双模式，快速查找上下文
- 🎭 **风格蒸馏** - 从范文中学习写作风格
- 📖 **小说蒸馏** - 提取小说设定作为创作参考
- 🔄 **章节 Pipeline** - 续写 → 润色 → 审计一键完成

### 🎯 智能推荐系统

<table>
<tr>
<td width="50%">

#### 📊 偏好分析
- 标签频次统计
- 关键词提取
- 作者偏好分析
- 字数分布画像
- 系列偏好占比

</td>
<td width="50%">

#### 🔍 智能推书
- 搜索计划生成
- 自动 Pixiv 检索
- 字数筛选（单篇≥5k，系列≥20k）
- 智能去重（标题相似度）
- 打分排序
- 反馈闭环（屏蔽作者/标签）

</td>
</tr>
</table>

### 🌐 完善的 Web 界面

| 页面 | 功能 |
|------|------|
| 📊 **Dashboard** | 系统状态、同步统计、快速操作 |
| 📚 **小说库** | 浏览、搜索、排序、导出 EPUB、查看拯救成功数据 |
| 👥 **关注管理** | 关注用户列表、状态监控、单独备份 |
| 🤖 **AI 创作** | 自动写作、创作向导、蒸馏档案、AI 小说阅读 |
| 🎯 **偏好推荐** | 画像分析、推荐运行、结果浏览 |
| 📋 **任务日志** | 实时日志、筛选、自动刷新 |
| ⚙️ **设置中心** | 同步配置、定时任务、限速参数 |

AI 功能入口：

- `/dashboard/ai`：自动写作项目、全书规划、章节和 Pipeline。
- `/dashboard/wizard`：创作向导与蒸馏档案。
- `/dashboard/novels?category=ai`：AI 创作小说库。
- `/dashboard/novels?category=rescue`：完整或部分救援的 Pixiv 小说与系列。

Pixiv 原站救援阅读：

1. 在 `/dashboard/settings#rescue-api` 生成独立救援 Token；完整明文只显示一次。
2. 本地安装 `userscripts/pixiv-rescue.user.js`，通过油猴菜单录入救援 Token。
3. 脚本只在 Pixiv 小说或系列明确失效时读取私人备份；正常页面不请求救援 API，也不修改原内容。

---

## 🚀 快速开始

### 系统要求

- Python 3.10+
- 4GB+ RAM
- 10GB+ 存储空间（根据同步数量）

### 安装步骤

#### 1. 克隆仓库

```bash
git clone https://github.com/dong-jpg/pixiv-novel-sync.git
cd pixiv-novel-sync
```

#### 2. 创建虚拟环境

```bash
# Linux/macOS
python -m venv .venv
source .venv/bin/activate

# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1

# Windows (Git Bash)
python -m venv .venv
source .venv/Scripts/activate
```

#### 3. 安装依赖

```bash
pip install -e .
```

#### 4. 创建本地配置

```bash
cp .env.example .env
cp config/config.yaml.example config/config.yaml
# 编辑 .env，至少填入 Pixiv refresh_token
```

> 💡 **获取 Token**: 启动服务后访问 `http://localhost:5010/token-login`，通过 OAuth 授权自动获取

#### 5. 启动服务

```bash
pixiv-novel-sync web-token-ui
```

访问 `http://localhost:5010/dashboard` 开始使用！

---

## 📚 使用文档

### 基础配置

#### 环境变量 (`.env`)

完整变量、用途与安全说明以 [`.env.example`](.env.example) 为准。首次使用先复制该文件，生产环境不要提交生成的 `.env`。

```env
# Pixiv 认证（必需）
PIXIV_REFRESH_TOKEN=your_refresh_token_here

# Dashboard 密码保护（公网/反代部署必须配置；留空时仅允许本机访问）
DASHBOARD_TOKEN=your_secure_password

# Flask session 密钥（留空时首次启动会安全生成随机值并写回 .env；生成后请保持稳定）
PIXIV_FLASK_SECRET=

# AI Provider API key 加密密钥（保存 AI Provider 前必须配置，并保持稳定）
PIXIV_NOVEL_SYNC_AI_SECRET_KEY=your_stable_secret
```

#### 同步配置 (`config/config.yaml`)

配置结构、默认值与字段说明以 [`config/config.yaml.example`](config/config.yaml.example) 为准：

```bash
cp config/config.yaml.example config/config.yaml
# 按需编辑 config/config.yaml
```

### 常用操作

#### 手动同步

```bash
# 依次同步收藏、关注用户小说和订阅系列
pixiv-novel-sync sync bookmark following_novels subscribed_series
```

#### EPUB 导出

```bash
# 导出单本小说
curl -X POST http://localhost:5010/api/dashboard/novels/export-epub \
  -H "Content-Type: application/json" \
  -d '{"novel_ids": [123456]}'

# 批量导出
curl -X POST http://localhost:5010/api/dashboard/novels/export-epub \
  -H "Content-Type: application/json" \
  -d '{"novel_ids": [123456, 234567, 345678]}'
```

#### AI 创作

启动 Web 服务后访问 [`/dashboard/ai`](http://localhost:5010/dashboard/ai) 使用 AI 创作工作台。当前页面依赖的接口以 [`docs/frontend-api-contract.md`](docs/frontend-api-contract.md) 为准。

### 数据目录结构

```
data/
├── library/               # 小说库
│   ├── public/            # 公开收藏归档
│   │   └── authors/<user_id>_<author>/novels/<novel_id>_<title_hash>/
│   │       ├── meta.json
│   │       ├── text.txt
│   │       ├── text.md
│   │       └── assets/
│   └── private/           # 私密收藏归档，内部布局同 public
└── state/
    └── pixiv_sync.db      # SQLite 数据库（含 AI 创作数据）
```

---

## 🛠️ 开发指南

### 项目结构

```
src/pixiv_novel_sync/
├── webapp.py                 # 主 Flask 应用
├── ai_web.py                 # AI 功能路由
├── preference_web.py         # 推荐系统路由
├── sync_engine.py            # 同步引擎
├── auth.py                   # Pixiv 认证
├── settings.py               # 配置管理
│
├── jobs/                     # 任务管理
│   ├── manager.py           # 任务调度器
│   ├── runner.py            # 任务执行器
│   └── tasks.py             # 任务定义
│
├── storage/                  # 存储层（模块化）
│   ├── connection.py        # 数据库连接
│   ├── schema.py            # 数据库 Schema
│   ├── novels.py            # 小说存储
│   ├── users.py             # 用户存储
│   ├── series.py            # 系列存储
│   ├── bookmarks.py         # 收藏管理
│   ├── recommendations.py   # 推荐存储
│   └── ai/                  # AI 相关存储
│
├── ai/                       # AI 创作模块
│   ├── providers.py         # AI Provider 抽象
│   ├── prompts.py           # Prompt 模板
│   ├── retrieval.py         # 语义检索
│   ├── chunking.py          # 文本分块
│   ├── detection.py         # AI 检测
│   └── services/            # AI 服务
│       ├── core.py          # 核心服务
│       ├── generation.py    # 生成服务
│       ├── projects.py      # 项目管理
│       └── chat_wizard.py   # 创作向导
│
├── web/                      # Web 层
│   ├── managers.py          # Web 管理器
│   └── utils.py             # Web 工具
│
└── templates/                # HTML 模板（14个页面）
```

### 运行测试

```bash
# 安装测试依赖
pip install -e ".[test]"

# 运行所有测试
pytest

# 运行特定模块测试
pytest tests/test_preferences.py

# 查看覆盖率
pytest --cov=pixiv_novel_sync --cov-report=html
```

### 代码规范

```bash
# 代码格式化
black src/

# 代码检查
flake8 src/
pylint src/

# 类型检查
mypy src/
```

### 贡献指南

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启 Pull Request

---

## ❓ 常见问题

<details>
<summary><strong>Q: Token 登录一直失败怎么办？</strong></summary>

**A**: 按以下步骤排查：
1. 检查网络连接，确保能访问 Pixiv
2. 如果在国内，尝试使用代理/VPN
3. 使用 OAuth 授权失败时，点击"使用旧版 gppt fallback"
4. 确保浏览器没有阻止弹窗
5. 清除浏览器 Pixiv 相关 Cookie 后重试
</details>

<details>
<summary><strong>Q: 同步卡住不动了怎么办？</strong></summary>

**A**: 可能原因和解决方法：
1. **触发限流** - 增大 `delay_seconds_between_items` 参数（改为 2.0 或 3.0）
2. **网络超时** - 检查网络连接，考虑配置代理
3. **任务冲突** - 停止当前任务，检查是否有多个任务同时运行
4. **查看日志** - 访问日志页面查看详细错误信息
</details>

<details>
<summary><strong>Q: 如何配置 AI Provider？</strong></summary>

**A**: 
1. 在 `.env` 中配置稳定的 `PIXIV_NOVEL_SYNC_AI_SECRET_KEY`
2. 访问 Dashboard → AI 创作 → 设置
3. 选择 Provider，并在页面中填入对应 API Key
4. 点击"测试连接"验证配置
5. 可以配置多个 Provider，但当前每个 Agent 只绑定一个 Provider（或该 Provider 下的固定模型）；尚不支持跨 Provider fallback。API Key 会加密保存，不会在接口中回显
</details>

<details>
<summary><strong>Q: 数据库文件越来越大怎么办？</strong></summary>

**A**: 
以下维护命令需要先安装 `sqlite3` 命令行工具（例如 Debian/Ubuntu 使用 `sudo apt install sqlite3`）。操作前请停止服务并备份数据库。

```bash
# 清理过期日志（同步任务与 AI 创作任务默认保留 3 天）
sqlite3 data/state/pixiv_sync.db "DELETE FROM task_logs WHERE created_at < datetime('now', '-3 days'); DELETE FROM ai_jobs WHERE created_at < datetime('now', '-3 days');"

# 真空优化数据库
sqlite3 data/state/pixiv_sync.db "VACUUM;"
```
</details>

<details>
<summary><strong>Q: 可以在服务器上部署吗？</strong></summary>

**A**: 可以。根目录 [`deploy.sh`](deploy.sh) 是推荐且唯一的 Web 部署入口，会配置虚拟环境、Nginx 和 systemd 服务：

```bash
./deploy.sh
```

[`scripts/install_server.sh`](scripts/install_server.sh) 仅保留给旧 timer 同步部署的历史/高级场景，不用于部署 Web 服务。

**安全建议**:
- 务必设置 `DASHBOARD_TOKEN` 环境变量
- 使用 HTTPS（通过 Let's Encrypt）
- 配置防火墙规则
</details>

<details>
<summary><strong>Q: 支持 Docker 部署吗？</strong></summary>

**A**: Docker 支持正在开发中，预计下个版本发布。目前建议使用虚拟环境部署。
</details>

---

## 📊 性能指标

| 指标 | 数值 |
|------|------|
| 同步速度 | ~100 本小说/分钟（取决于限速设置） |
| 全文搜索 | <100ms（1万本小说规模） |
| AI 响应 | 流式输出，首 token <2s |
| 内存占用 | ~200MB（不含 AI 模型） |
| 数据库大小 | ~1MB/100本小说 |

---

## 🗺️ 路线图

### ✅ 已完成
- [x] 核心同步功能
- [x] AI 创作工作台
- [x] 智能推荐系统
- [x] Web 管理界面
- [x] EPUB 导出
- [x] 模块化架构重构
- [x] 任务取消硬化(2026-07-02 审计修复:AI 幻觉 import / EPUB 注入 / 同步取消链 / 配置漂移,详见 [docs/AUDIT_REPORT_2026-07-02.md](docs/AUDIT_REPORT_2026-07-02.md))
- [x] 死代码清理 + EPUB 回归修复 + 文档归档(2026-07-03 审计,详见 [docs/AUDIT_REPORT_2026-07-03.md](docs/AUDIT_REPORT_2026-07-03.md))

### 🚧 进行中
- [ ] AI 偏好总结（推荐系统 Phase B）
- [ ] AI 创作偏好注入
- [ ] 并发安全性加固

### 📋 计划中
- [ ] Docker 部署支持
- [ ] 多账号管理
- [ ] 插件系统
- [ ] 移动端 App
- [ ] 更多 AI Provider (Gemini, Claude Opus)
- [ ] 国际化支持

---

## 🤝 贡献者

感谢所有为本项目做出贡献的开发者！

<a href="https://github.com/dong-jpg/pixiv-novel-sync/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=dong-jpg/pixiv-novel-sync" />
</a>

---

## 📄 许可证

本项目采用 [MIT License](LICENSE) 开源协议。

---

## 💬 社区与支持

- **问题反馈**: [GitHub Issues](https://github.com/dong-jpg/pixiv-novel-sync/issues)
- **功能建议**: [GitHub Discussions](https://github.com/dong-jpg/pixiv-novel-sync/discussions)
- **技术文档**: [Wiki](https://github.com/dong-jpg/pixiv-novel-sync/wiki)

---

## ⭐ Star 历史

[![Star History Chart](https://api.star-history.com/svg?repos=dong-jpg/pixiv-novel-sync&type=Date)](https://star-history.com/#dong-jpg/pixiv-novel-sync&Date)

---

<div align="center">
  <sub>Built with ❤️ by the community</sub>
  <br/>
  <sub>如果这个项目对你有帮助，请给一个 ⭐️ Star 支持一下！</sub>
</div>
