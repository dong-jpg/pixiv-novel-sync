# 拯救成功与 Pixiv 原站救援阅读设计

> [!IMPORTANT]
> 后台“拯救成功”列表的实时全库计算方案已被
> [救援目录预计算与来源展示设计](2026-07-21-rescue-catalog-sources-design.md)
> 替代。单项只读救援 API 和油猴脚本仍按本文的实时资格校验执行。

## 目标

为已经完整备份到本地、但在 Pixiv 上已删除或受限的小说和系列提供一套可核验的“拯救成功”视图，并允许用户通过油猴脚本在 Pixiv 原页面中读取私人备份。

本次交付包含：

- 小说库中的“拯救成功”Tab；
- 小说与系列的实时救援判定；
- 人工纠错；
- 独立只读救援 Token；
- 版本化只读救援 API；
- 可本地安装的油猴脚本；
- 对应的数据库、接口、页面、安全和浏览器测试。

## 不在本次范围内

- 模型池、Provider 模型发现和成人润色 Agent；
- 将油猴脚本自动发布到 GreasyFork；
- 对正常可访问的 Pixiv 内容做替换或增强；
- 公开分享、搜索引擎索引或无 Token 访问私人备份；
- 在救援 API v1 中提供本地封面二进制代理；
- 保存完整的救援状态历史和状态变化审计时间线。

模型池作为后续独立规格处理，避免其数据库迁移和故障切换逻辑干扰本功能。

## 现有系统约束

- 小说已经具有 `status` 和 `last_checked_at`，状态包括 `normal`、`deleted`、`restricted` 和 `unknown`。
- 系列已经具有 `status` 和 `last_checked_at`，状态包括 `normal`、`deleted` 和 `unknown`。
- `novel_archive_complete()` 已能判断单篇小说是否具有非空正文。
- `series.total_novels` 是当前数据库记录的系列总章节数；当该值小于等于 `0` 时，视为未知。
- `pending_deletions.restored` 表示取消本地删除，且历史记录会被清理，不能复用为救援状态。
- 管理后台使用会话认证和 CSRF；油猴脚本不能复用具有后台管理权限的 `DASHBOARD_TOKEN`。
- 对外域名固定为 `https://pixiv.dongboapp.com`，Cloudflare 和源站 HTTPS 已配置。

## 总体架构

救援状态不写入物化记录表，而是在查询时根据三类事实实时计算：

1. Pixiv 最后检查状态；
2. 本地正文和系列章节覆盖情况；
3. 用户保存的人工纠错。

只新增人工纠错表和单 Token 配置表。实时计算避免正文被删除、Pixiv 状态恢复或系列章节变化后留下过期的“拯救成功”记录。

系统分为四个边界清晰的组件：

- `storage/rescue.py`：表结构之外的救援查询、完整度计算和纠错读写；
- `rescue_web.py`：管理 API、只读 API、Token 校验和响应字段白名单；
- `dashboard_novels.html` 与详情页：救援 Tab 和人工纠错入口；
- `userscripts/pixiv-rescue.user.js`：仅在 Pixiv 原内容失效时渲染救援视图。

## 数据模型

### `rescue_overrides`

```sql
CREATE TABLE IF NOT EXISTS rescue_overrides (
    item_type TEXT NOT NULL CHECK (item_type IN ('novel', 'series')),
    item_id INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('include', 'exclude')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (item_type, item_id)
);
```

`include` 只把 Pixiv 可用性视为“已失效”；`exclude` 只把 Pixiv 可用性视为“仍可访问”。两者都不能绕过正文完整性条件。

删除纠错记录表示恢复自动判断。删除小说或系列时同步删除对应纠错记录，避免留下无主数据。

### `rescue_api_token`

```sql
CREATE TABLE IF NOT EXISTS rescue_api_token (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    token_hash TEXT NOT NULL,
    token_prefix TEXT NOT NULL,
    rotated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

第一版只允许一个活动救援 Token。Token 格式为 `rsq_` 加至少 256 位随机数据。数据库只保存 SHA-256 摘要和用于识别的前缀，不保存明文。

轮换时覆盖单例记录并仅在响应中返回一次明文；旧 Token 立即失效。Token 状态接口只返回是否已配置、前缀和轮换时间。

## 实时判定规则

### 单篇小说

先计算 Pixiv 失效状态：

- 人工 `exclude`：视为未失效；
- 人工 `include`：视为已失效；
- 无人工纠错：`novels.status IN ('deleted', 'restricted')` 时视为已失效。

单篇小说只有同时满足以下条件时才进入救援列表：

- Pixiv 状态视为已失效；
- `novel_texts.text_raw` 去除空白后非空。

单篇小说只有 `success`，没有 `partial`。

### 系列

先计算 Pixiv 失效状态：

- 人工 `exclude`：视为未失效；
- 人工 `include`：视为已失效；
- 无人工纠错：`series.status = 'deleted'` 时视为已失效。

为每个系列计算：

- `expected_count`：`series.total_novels`；
- `local_count`：本地 `novels` 中属于该系列的章节数；
- `complete_count`：属于该系列且正文非空的章节数。

状态规则：

- `success`：系列视为已失效，`expected_count > 0`，`local_count >= expected_count`，并且 `complete_count = local_count`；
- `partial`：系列视为已失效，`complete_count > 0`，但不满足 `success`；
- 不显示：系列未失效或 `complete_count = 0`。

当 `expected_count <= 0` 时不能判为 `success`，只能在有正文时判为 `partial`。这是严格判定 A，避免把章节总数未知的备份误报为完整救援。

### 系列章节读取

小说库的救援 Tab 只显示一张系列卡片，不把章节重复显示为单篇救援项。

只读 API 允许读取救援系列中的已备份章节。即使章节自身状态仍是 `normal`，只要父系列判为 `success` 或 `partial`，且该章节正文非空，`GET /api/rescue/v1/novels/<id>` 仍可返回正文。响应必须写明 `eligibility_reason = "parent_series_unavailable"`。

人工排除父系列后，章节不能再通过父系列关系获得读取资格；章节自身仍可按单篇规则独立判定。

## 小说库与后台交互

### “拯救成功”Tab

在现有“收藏小说 / 系列小说 / AI 创作小说”后增加“拯救成功”。页面沿用小说库网格、卡片尺寸、分页、搜索和排序样式，不创建新的一级菜单。

Tab 支持：

- 状态筛选：全部、完整救援、部分救援；
- 类型筛选：全部类型、小说、系列；
- 标题和作者搜索；
- 按 Pixiv 最后检查时间或本地最近更新时间排序。

卡片统一返回并显示：

- `item_type`、`item_id`、标题、作者和封面 URL；
- `rescue_state`；
- Pixiv 原状态和最后检查时间；
- 系列的 `complete_count / expected_count`，总数未知时显示“总章节数未知”；
- “来自私人备份”标识。

点击小说进入现有小说阅读页，点击系列进入现有系列详情页。

### 人工纠错

小说和系列详情页增加操作菜单：

- 标记 Pixiv 已失效：保存 `include`；
- 标记 Pixiv 仍可访问：保存 `exclude`；
- 恢复自动判断：删除纠错记录。

保存 `include` 后若正文或系列完整度不足，页面必须显示当前仍为“不完整”或“部分救援”，不得显示成功。

所有写操作继续使用后台会话认证和现有 CSRF 防护。

### Token 管理

设置页增加“救援 API”区域，显示固定 API 地址、Token 是否配置、Token 前缀和轮换时间。用户点击生成或重新生成后，明文 Token 只在结果对话框中出现一次，并提供复制命令。

前端离开结果对话框后不能再次读取明文 Token。

## 管理 API

### 列表

`GET /api/dashboard/rescues`

查询参数：

- `page`，默认 `1`；
- `page_size`，默认 `12`，最大 `100`；
- `state`：`all/success/partial`；
- `item_type`：`all/novel/series`；
- `search`；
- `sort`：`checked_desc/updated_desc`。

响应沿用现有分页结构：`items`、`page`、`page_size`、`total`、`total_pages`。

### 人工纠错

`PUT /api/dashboard/rescue-overrides/<item_type>/<item_id>`

请求体：

```json
{
  "action": "include",
  "note": "Pixiv 页面已确认失效"
}
```

服务端校验类型、动作、对象存在性和备注长度。备注最长 500 个字符。

`DELETE /api/dashboard/rescue-overrides/<item_type>/<item_id>` 删除纠错并恢复自动判断。

### Token

- `GET /api/dashboard/rescue-token/status`
- `POST /api/dashboard/rescue-token/rotate`

轮换接口返回：

```json
{
  "ok": true,
  "data": {
    "token": "<仅本次返回的救援 Token>",
    "token_prefix": "rsq_abcd",
    "rotated_at": "2026-07-21T00:00:00Z"
  }
}
```

只有轮换响应包含 `token`。

## 只读救援 API v1

### 认证

路径前缀为 `/api/rescue/v1/`。该前缀从后台会话认证中单独分流，但每个请求必须通过救援 Bearer Token 校验。

只接受：

```http
Authorization: Bearer <救援 Token>
```

不接受查询参数、Cookie 或 `DASHBOARD_TOKEN`。缺少或错误 Token 返回 `401` 和 `WWW-Authenticate: Bearer`。不允许的方法返回 `405`。

Token 摘要使用恒定时间比较。每个来源 IP 与 Token 组合限制为每分钟 120 次请求，超限返回 `429`。

所有响应增加：

- `Cache-Control: no-store`；
- `X-Robots-Tag: noindex, nofollow, noarchive`；
- `X-Content-Type-Options: nosniff`。

不配置宽泛 CORS；油猴脚本通过 `GM_xmlhttpRequest` 和固定 `@connect` 访问。

### 单篇正文

`GET /api/rescue/v1/novels/<id>`

仅在单篇规则成立，或父系列规则允许时返回 `200`。不满足条件和对象不存在均返回 `404`，避免通过 API 枚举普通私人收藏。

字段白名单：

- 小说 ID、标题、作者名、系列 ID；
- 简介、标签、创建时间、正文；
- Pixiv 原状态、最后检查时间；
- `rescue_state`、`eligibility_reason`；
- “来自私人备份，非 Pixiv 官方恢复”的固定声明。

不返回 `raw_json`、本地文件路径、数据库字段全集或后台配置。

### 系列元数据

`GET /api/rescue/v1/series/<id>`

仅对 `success` 或 `partial` 系列返回元数据、作者、说明、完整度和固定声明，不返回全部正文。

### 系列目录

`GET /api/rescue/v1/series/<id>/chapters`

支持 `page` 和 `page_size`，默认 `100`，最大 `100`。只返回具有非空正文的章节元数据、章节序号和对应小说 API 地址。

目录按 `create_date ASC, novel_id ASC` 排序。响应明确包含完整度和是否为部分救援。

## 油猴脚本

### 文件与权限

脚本保存为 `userscripts/pixiv-rescue.user.js`，第一版由用户本地安装，不自动发布到第三方平台。

匹配：

- `https://www.pixiv.net/novel/show.php*`；
- `https://www.pixiv.net/novel/series/*`。

权限只包含：

- `GM_xmlhttpRequest`；
- `GM_getValue`；
- `GM_setValue`；
- `GM_registerMenuCommand`；
- `@connect pixiv.dongboapp.com`。

API 地址固定为 `https://pixiv.dongboapp.com`，不能从页面参数、远端响应或任意用户输入修改。

### Token 设置

油猴菜单提供“设置救援 Token”和“清除救援 Token”。Token 保存在油猴脚本存储中，不写入页面 DOM、URL、控制台或错误信息。

### 激活条件

脚本先等待 Pixiv 页面完成初始渲染，再判断页面状态：

- 能找到正常小说正文或正常系列目录：立即结束，不请求救援 API，不修改页面；
- 出现删除、受限、错误提示，或等待超时后仍无正文/目录：尝试调用救援 API；
- 页面状态无法确定：保守结束，不覆盖 Pixiv 内容。

正常内容优先是不可破坏的硬约束。

### 渲染

API 成功后，在 Pixiv 主内容区域插入独立救援视图：

- 顶部显眼显示“拯救数据”“来自私人备份”“非 Pixiv 官方恢复”；
- 单篇显示标题、作者、元数据和正文；
- 系列显示标题、作者、说明、完整度和章节目录；
- 点击救援目录章节时按需读取正文，不预取整套系列；
- 部分系列持续显示“部分救援”和覆盖率；
- 有远端封面 URL 时可尝试显示，封面失败不能阻止正文阅读。

正文和所有 API 文本只通过 `textContent` 或等价安全文本节点写入，正文容器使用 `white-space: pre-wrap` 保留换行。不得将正文拼接到 `innerHTML`。

API 未配置、Token 错误、限流、网络失败或备份不存在时，不删除 Pixiv 原错误页，只显示可关闭的非侵入式提示。

## 错误处理与日志

- 管理 API 使用现有 `{ok, data, error}` 风格和中文错误信息；
- 只读 API 使用标准 HTTP 状态码，不暴露数据库异常细节；
- SQLite 或文件读取异常记录到服务端日志，客户端只收到通用错误；
- 日志不得记录 Bearer Token 或小说正文；
- Token 轮换后，旧脚本请求得到 `401`，用户重新设置新 Token 即可；
- 系列分页中某章被删除或正文为空时，从目录结果中跳过，并以实时完整度为准。

## 测试策略

### 数据库与服务单元测试

- 单篇 `normal/deleted/restricted/unknown`；
- 单篇正文存在、空白和缺失；
- 人工 `include/exclude` 与恢复自动判断；
- 系列完整、部分、无正文和总章节数未知；
- 系列本地章节多于记录总数但存在空正文时不能成功；
- 父系列失效、章节自身正常时的读取资格；
- 删除小说或系列时清理纠错记录；
- Token 生成、摘要存储、轮换和旧 Token 失效。

### Web API 测试

- 管理列表筛选、搜索、排序和分页；
- 纠错参数、对象存在性、CSRF 和备注长度；
- 缺少、错误和正确 Bearer Token；
- 查询参数 Token 被拒绝；
- 普通私人作品返回 `404`；
- 响应字段白名单和安全响应头；
- `POST/PUT/DELETE` 对只读 API 返回 `405`；
- 限流返回 `429`。

### 前端与油猴脚本测试

- 小说库 Tab 切换、筛选、分页和混合卡片跳转；
- 详情页纠错操作及状态刷新；
- Token 明文只在轮换结果中出现；
- 正常 Pixiv 页面不发请求、不改 DOM；
- 删除小说页渲染救援正文；
- 删除系列页渲染目录并按需加载章节；
- API 文本不能注入 HTML 或脚本；
- 失败状态保留 Pixiv 原页面。

浏览器测试使用受控 Pixiv DOM fixture，不依赖真实 Pixiv 账号或线上页面稳定性。完成后运行项目完整 `pytest` 测试。

## 部署顺序

1. 部署数据库迁移、实时判定和管理 API；
2. 部署小说库 Tab、详情页纠错和 Token 管理；
3. 部署只读 API 并在后台生成首个 Token；
4. 安装本地油猴脚本并录入 Token；
5. 用受控 fixture 和实际失效 URL 做只读验收；
6. 推送 `main` 并运行服务器 `./update.sh`；
7. 验证 Cloudflare HTTPS、API 安全响应头和日志中无 Token/正文。

## 验收标准

- 小说库能准确区分完整救援与部分救援；
- 严格系列判定不会把总章节数未知或存在空正文的系列标记为成功；
- 人工纠错不能绕过正文完整性；
- 正常 Pixiv 页面不会被油猴脚本改写；
- 失效单篇和系列能在原页面区域读取私人备份；
- 页面明确声明数据来自私人备份而非 Pixiv 官方恢复；
- 救援 Token 与后台 Token 完全隔离，明文不落库；
- 未授权请求不能枚举或读取普通私人收藏；
- 新增测试和现有完整测试全部通过。
