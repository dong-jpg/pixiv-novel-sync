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
- ✅ 8 个独立定时任务
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
| 📚 **小说库** | 浏览、搜索、排序、导出 EPUB |
| 👥 **关注管理** | 关注用户列表、状态监控、单独备份 |
| 🤖 **AI 创作** | 草稿管理、项目管理、模型配置 |
| 🎯 **偏好推荐** | 画像分析、推荐运行、结果浏览 |
| 📋 **任务日志** | 实时日志、筛选、自动刷新 |
| ⚙️ **设置中心** | 同步配置、定时任务、限速参数 |

---

## 🚀 快速开始

### 系统要求

- Python 3.10+
- 4GB+ RAM
- 10GB+ 存储空间（根据同步数量）

### 安装步骤

#### 1. 克隆仓库

```bash
git clone https://github.com/你的用户名/pixiv-novel-sync.git
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

#### 4. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 文件，填入你的 Pixiv refresh_token
```

> 💡 **获取 Token**: 启动服务后访问 `http://localhost:5010/token-login`，通过 OAuth 授权自动获取

#### 5. 启动服务

```bash
python -m pixiv_novel_sync.webapp
```

访问 `http://localhost:5010/dashboard` 开始使用！

---

## 📚 使用文档

### 基础配置

#### 环境变量 (`.env`)

```env
# Pixiv 认证（必需）
PIXIV_REFRESH_TOKEN=your_refresh_token_here

# Dashboard 密码保护（可选，推荐公网部署时启用）
DASHBOARD_TOKEN=your_secure_password

# Flask 密钥（自动生成）
PIXIV_FLASK_SECRET=auto_generated

# AI Provider API Keys（使用 AI 功能时需要）
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
MOONSHOT_API_KEY=sk-...
```

#### 同步配置 (`config/config.yaml`)

```yaml
sync:
  # 下载设置
  download_assets: true           # 下载封面与插图
  write_markdown: true             # 生成 Markdown 格式
  write_raw_text: true             # 保存原始文本
  
  # 同步范围
  bookmark_restricts:
    - public                       # 同步公开收藏
    - private                      # 同步私密收藏
  
  # 限速参数（避免触发 Pixiv 限流）
  max_items_per_run: 50            # 单次最大同步数
  max_pages_per_run: 5             # 单次最大页数
  delay_seconds_between_items: 1.0 # 请求间隔
  delay_seconds_between_pages: 1.0 # 翻页间隔
  delay_seconds_between_skips: 0.1 # 跳过时延迟
  
  # 定时任务（Cron 表达式）
  auto_sync_enabled: false
  auto_sync_bookmarks_cron: "0 */6 * * *"           # 每6小时同步收藏
  auto_sync_following_novels_cron: "0 */6 * * *"    # 每6小时同步关注
  auto_sync_subscribed_series_cron: "0 */6 * * *"   # 每6小时同步追更
  auto_sync_user_status_cron: "0 0 * * *"           # 每天检查用户状态
```

### 常用操作

#### 手动同步

```bash
# 同步收藏小说
curl -X POST http://localhost:5010/api/dashboard/sync/bookmarks

# 同步关注用户小说
curl -X POST http://localhost:5010/api/dashboard/sync/following-novels

# 同步追更系列
curl -X POST http://localhost:5010/api/dashboard/sync/subscribed-series
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

#### AI 创作（API）

```bash
# 续写小说
curl -X POST http://localhost:5010/api/ai/drafts/continue \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "your_agent_id",
    "context": "小说前文...",
    "instruction": "继续写下去，增加冲突"
  }'
```

### 数据目录结构

```
data/
├── library/               # 小说库
│   ├── public/           # 公开收藏
│   │   └── user_12345/
│   │       └── novel_67890/
│   │           ├── meta.json      # 元数据
│   │           ├── text.txt       # 原始文本
│   │           ├── text.md        # Markdown
│   │           └── assets/        # 封面插图
│   └── private/          # 私密收藏
├── state/
│   └── pixiv_sync.db    # SQLite 数据库
└── ai/
    ├── drafts/          # AI 草稿
    ├── projects/        # 写作项目
    └── retrieval/       # 检索索引
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
1. 在 `.env` 中添加对应 API Key（如 `OPENAI_API_KEY`）
2. 访问 Dashboard → AI 创作 → 设置
3. 选择 Provider 并填入 API Key
4. 点击"测试连接"验证配置
5. 可配置多个 Provider 作为 fallback
</details>

<details>
<summary><strong>Q: 数据库文件越来越大怎么办？</strong></summary>

**A**: 
```bash
# 清理过期日志（保留最近 7 天）
sqlite3 data/state/pixiv_sync.db "DELETE FROM task_logs WHERE created_at < datetime('now', '-7 days');"

# 真空优化数据库
sqlite3 data/state/pixiv_sync.db "VACUUM;"

# 或使用 API
curl -X POST http://localhost:5010/api/cache/clear
```
</details>

<details>
<summary><strong>Q: 可以在服务器上部署吗？</strong></summary>

**A**: 可以！推荐使用反向代理：

```nginx
# Nginx 配置示例
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:5010;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

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

<a href="https://github.com/你的用户名/pixiv-novel-sync/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=你的用户名/pixiv-novel-sync" />
</a>

---

## 📄 许可证

本项目采用 [MIT License](LICENSE) 开源协议。

---

## 💬 社区与支持

- **问题反馈**: [GitHub Issues](https://github.com/你的用户名/pixiv-novel-sync/issues)
- **功能建议**: [GitHub Discussions](https://github.com/你的用户名/pixiv-novel-sync/discussions)
- **技术文档**: [Wiki](https://github.com/你的用户名/pixiv-novel-sync/wiki)

---

## ⭐ Star 历史

[![Star History Chart](https://api.star-history.com/svg?repos=你的用户名/pixiv-novel-sync&type=Date)](https://star-history.com/#你的用户名/pixiv-novel-sync&Date)

---

<div align="center">
  <sub>Built with ❤️ by the community</sub>
  <br/>
  <sub>如果这个项目对你有帮助，请给一个 ⭐️ Star 支持一下！</sub>
</div>
