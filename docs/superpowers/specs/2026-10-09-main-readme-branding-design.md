# main 分支：私人书库品牌与自述设计

状态：设计已获维护者确认；本文件记录已确认的范围，不新增功能需求。

## 目标与边界

main 面向需要同步、归档与阅读 Pixiv 小说的个人用户，定位为 **私人 Pixiv 小说书库**。
标语：**把喜欢的小说，留在自己的书库里。**

- 为 main 新增 `assets/main-logo.svg`，并重写根目录 `README.md`。
- `ai-writing` 的 README 与品牌资源原样保留。
- 旧 `assets/logo.svg`、`assets/logo-mark.svg`、`assets/logo-design.md` 不替换、不删除。
- 不修改业务代码、网页 favicon、GitHub 账号或仓库设置，不操作服务器。
- 本次仅本地交付；推送、部署由维护者另行决定。
- 保留工作区内已有服务器文档修改，不能混入本次提交。

## 图标

使用「打开的书＋同步环」，表达阅读、持有本地副本与持续同步，而不是写作。
图形为方形、自包含的静态 SVG；README 标题和标语使用真实文本，不嵌入图中。

- 主色为青蓝，同步环使用两段清晰箭头；墨蓝底板确保明暗背景一致。
- 书页使用浅色，尽量减少细线与装饰，32px 仍能辨认书本与同步方向。
- 不依赖字体、脚本、外部图片、远程资源、滤镜或动画。
- SVG 提供 `title`、`desc` 与可访问名称；README 图片提供有意义的 `alt`。
- README 图标展示宽度不超过 128px；在手机宽度与明暗模式下实看验证。

## README 信息架构

1. 图标、项目名、定位、标语与简短导航。
2. 分支选择：main 是私人书库；写作需求链接到 `ai-writing`，不挤占主要介绍。
3. 核心体验：同步收藏/关注/追更系列、本地归档、全文检索、移动阅读与 EPUB、规则推荐、救援。
4. 快速开始：选择 main、Python 虚拟环境、安装、复制配置、启动与访问地址。
5. 部署与安全：Web 部署入口、HTTPS、鉴权与会话、备份和日志留存。
6. 可选进阶：AI 模型目录、模型池、关键词清洗及 Provider 隐私边界。
7. 文档与开发入口、许可证和反馈。

首屏不保留带日期的审计/交付流水账，不使用三栏 HTML 表格。
中文为主，短段落与单栏列表优先；版本状态和历史报告由文档索引承载。

## 必须与 main 实现一致

- Python >=3.10；Flask + SQLite，无前端构建步骤。
- 安装：`pip install -e .`；测试依赖：`pip install -e ".[test]"`。
- 配置：`.env.example` → `.env`；`config/config.yaml.example` → `config/config.yaml`。
- 启动：`pixiv-novel-sync web-token-ui`；默认 `127.0.0.1:5010`，书库入口 `/dashboard`。
- 同步命令：`pixiv-novel-sync sync bookmark following_novels subscribed_series`。
- 任务日志默认保留 14 天，通过 `sync.task_log_retention_days` 配置。
- 推荐以本地统计和规则为主；main 的 AI 生成仅用于偏好关键词清洗，失败时降级。
- 不把 AI Provider 配置写成同步/阅读的先决条件。
- 不承诺完整离线网页：页面仍使用 CDN，已归档文件可本地保存与导出阅读。
- 救援仅读取已经存在的私人本地备份，不能恢复从未归档的作品或绕过访问限制。
- 保留救援入口 `/dashboard/novels?category=rescue` 与 `userscripts/pixiv-rescue.user.js`。
- 用户脚本需浏览器/扩展支持，不能宣称安卓原生 Chrome 默认支持。
- 公网部署必须设置 `DASHBOARD_TOKEN` 并使用 HTTPS；HTTPS 下显式配置 `PIXIV_COOKIE_SECURE=1`。
- `PIXIV_FLASK_SECRET` 应保持稳定；仅私人设备可选保持登录 30 天，密码不保存到浏览器缓存。
- 不公开 `.env`、令牌、数据库、私密备份与日志；备份覆盖配置、数据库和归档文件。
- AI 进阶说明保留模型同步端点、16 个候选 / 32 次网络请求 / 30 分钟预算与跨 Provider Prompt 披露。

## 验收

- 先增加可失败的 SVG / README 契约测试，再实施；已有 README 文档断言保持通过。
- 所有新增相对链接存在，文内锚点有效；SVG 为合法、无外部引用的 XML。
- 实看 32px 图标、明暗背景和 390px 手机宽度；桌面首屏层级清晰。
- 独立复核本次差异；确认 ai-writing 和原有未提交修改的文件指纹不变。
- 本地提交只包含本次设计、计划、README、图标和测试，不推送、不部署。
