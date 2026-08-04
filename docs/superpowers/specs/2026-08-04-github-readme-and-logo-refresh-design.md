# GitHub README 与 Logo 刷新设计

> 日期：2026-08-04
> 状态：已确认，等待实施计划
> 范围：GitHub 项目介绍页 README.md、主 Logo、备用 mark 与 Logo 说明文档

## 1. 背景

当前 README.md 信息量过重，首屏视觉依赖旧版蓝紫粉 Logo、emoji 导航和大段功能堆叠，不能快速说明项目的真实定位：本地 Pixiv 小说归档、AI 创作工作台和偏好推荐。现有 assets/logo.svg 视觉较旧，缩小后识别度不足，也缺少可作为 GitHub avatar 的简洁 mark。

本次刷新以静态 Markdown、静态 SVG 和 GitHub 原生渲染能力为边界，不引入动画、外部图片、前端构建或动态效果。

## 2. 设计方向

采用用户选择的 B 方案：Writer's Desk。

- 视觉气质：温暖、克制、有编辑台和写作工具感。
- 主色：暖米白背景、深墨色文字、砖红主色，少量蓝绿色辅助色。
- 图标语义：折角文稿代表小说与资料库，短横线代表文本，右下角编辑笔尖/批注符号代表创作与润色。
- 静态优先：如果静态 SVG 和 Markdown 能达到足够效果，不加入动画。

## 3. README 信息架构

### 3.1 Hero

首屏保留最少但明确的信息：

- assets/logo.svg 主图；
- 项目名 Pixiv Novel Sync；
- 定位语：从收藏，到灵感，再到下一章。
- 一句说明：本地归档 Pixiv 小说，整理创作素材，用 AI 辅助写作，并基于偏好发现新作品；
- 技术徽章；
- 三个锚点入口：Features、Quick Start、Docs。

### 3.2 What it does

用三列呈现核心能力，每列只保留 3-4 条关键点：

- Library：收藏/关注/追更同步、本地全文库、EPUB 与救援阅读。
- Writing Studio：项目、长篇规划、章节 Pipeline、风格/小说蒸馏。
- Discovery：偏好画像、搜索计划、推荐打分、反馈与屏蔽。

### 3.3 Quick Start

快速开始提前到前半页，保持四步：

1. 创建虚拟环境；
2. 安装 pip install -e .；
3. 复制 .env.example 与 config/config.yaml.example；
4. 启动 pixiv-novel-sync web-token-ui。

### 3.4 Core Workflows

用短段落说明用户进入各主要能力的位置：

- 同步归档；
- AI 创作；
- 推荐；
- Pixiv 原站救援阅读。

### 3.5 Configuration & Security

集中说明 .env、DASHBOARD_TOKEN、PIXIV_NOVEL_SYNC_AI_SECRET_KEY 和本地优先边界。公网或反向代理部署必须配置 Dashboard token。

### 3.6 Development 与 Docs

开发区只保留当前真实命令：python -m pytest -q、定向 pytest、python -m compileall -q src。不要把未配置的 Black、Flake8、Pylint、mypy 写成必跑检查。

详细需求、API、页面清单和历史资料改由 docs/INDEX.md、docs/UNIFIED_PROJECT_REQUIREMENTS.md 和相关契约文档承载。

## 4. 图标与资源

本次实施应维护以下资源：

- assets/logo.svg：README 主图，包含主 mark 与项目文字排版，适合约 180-220px 展示。
- assets/logo-mark.svg：仅包含折角文稿 + 编辑标记，可用于 GitHub avatar、favicon 或小尺寸引用。
- assets/logo-design.md：替换旧蓝紫粉方案说明，记录当前静态设计规则、色值和使用建议。

图标必须在 32px 尺寸下仍能辨认文稿轮廓；SVG 不依赖外部字体、脚本或远程图片。

## 5. 非目标

- 不新增动态 README 效果或外部托管图片；
- 不引入前端构建工具；
- 不把 README 改成完整需求文档；
- 不重写项目功能或路由；
- 不在本轮处理 GitHub Social Preview 图片。

## 6. 验收标准

- README.md 首屏更简洁，定位、入口和核心能力在 GitHub 上无需滚动过多即可理解；
- assets/logo.svg 和 assets/logo-mark.svg 都是静态 SVG，路径可被 README 正确引用；
- assets/logo-design.md 与实际 Logo 一致，不再推荐旧方案；
- README 中命令与当前项目一致，不写入未配置为依赖的工具作为必跑检查；
- 运行 git diff --check，并对文档/资源做人工阅读检查。
