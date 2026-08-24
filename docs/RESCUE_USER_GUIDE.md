# 救援功能用户指南

本指南面向使用者，介绍 pixiv-novel-sync 的「救援」（Rescue）功能：当 Pixiv 上的小说或系列被删除、无法访问时，如何从你的私人备份中读取内容并在 Pixiv 页面上原地展示。

内容以源码实际行为为准（`src/pixiv_novel_sync/rescue_web.py`、`src/pixiv_novel_sync/storage/rescue.py`、`userscripts/pixiv-rescue.user.js`）。

---

## 1. 救援功能概述

### 是什么

同步工具会持续把你收藏 / 追更 / 关注作者的小说正文备份到本地 SQLite 数据库。「救援」功能在此之上提供两部分能力：

1. **救援目录（rescue catalog）**：后台从备份数据中筛选出「远端已不可用、但本地有完整正文」的小说与系列，生成一份可浏览、可筛选的目录（仪表盘中的救援列表）。
2. **只读救援 API + Tampermonkey 用户脚本**：当你在 Pixiv 上打开一个已删除的小说 / 系列页面时，用户脚本检测到页面失效，自动向你的服务器请求备份正文，并在原页面渲染出带「拯救数据」标识的阅读面板。

### 解决什么问题

- 作者删文、账号注销、作品被限制后，Pixiv 官方页面无法再阅读；
- 你本地其实早已备份了正文，但翻数据库很不方便；
- 救援功能让「打开原来的 Pixiv 链接」这个习惯继续有效——页面失效时无缝切换到私人备份。

> 注意：所有救援内容都会带有提示「内容来自私人备份，并非 Pixiv 官方恢复」。这只是你自己的备份视图，不是官方恢复。

### 收录条件（哪些内容会进入救援目录）

由 `storage/rescue.py` 中的目录构建逻辑决定：

- **独立小说 / 系列单章**：本地正文完整（`novel_texts.has_content = 1`），且远端状态为 `deleted` 或 `restricted`；
- **系列**：远端状态为 `deleted`，且至少有一章本地正文完整；
- 你可以通过**纠错（override）**手动干预：`include` 强制收录（即使远端状态未标记为不可用），`exclude` 强制排除；
- 系列的救援状态分为 `success`（本地章节数 ≥ 系列总章节数且全部正文完整）和 `partial`（部分备份）；被系列收录的章节不会再作为独立条目重复出现。

---

## 2. 安装 Tampermonkey 用户脚本与救援 Token

### 2.1 安装用户脚本

1. 在浏览器安装 [Tampermonkey](https://www.tampermonkey.net/) 扩展；
2. 打开仓库中的 `userscripts/pixiv-rescue.user.js`，将其内容添加为新的用户脚本（或通过 Tampermonkey 的「实用工具 → 导入」）；
3. 脚本只在以下页面生效：
   - `https://www.pixiv.net/novel/show.php*`（小说页）
   - `https://www.pixiv.net/novel/series/*`（系列页）
4. 脚本默认请求的服务器地址由脚本内的 `API_ORIGIN` 常量决定。若你的部署域名不同，需要修改脚本开头的 `API_ORIGIN` 以及 `@connect` / `@namespace` 中的域名。

脚本的触发逻辑是**保守的**：只有当页面出现明确的失效标记（如「この作品は削除されています」「该作品已被删除」「Page not found」等），且在约 1.2 秒的等待窗口内页面始终没有渲染出正常正文 / 章节列表时，才会请求救援 API。正常页面不会被改动。

### 2.2 生成救援 Token（写入 / 轮换）

救援 API 使用独立的**救援 Token**认证。Token 由服务端管理路由生成，全局只有一个（单例存储）：

- `GET /api/dashboard/rescue-token/status` — 查看当前 Token 状态，返回：
  - `configured`：是否已配置；
  - `token_prefix`：Token 前 12 个字符（用于识别，不含完整值）；
  - `rotated_at`：最近一次轮换时间。
- `POST /api/dashboard/rescue-token/rotate` — 生成新 Token 并**立即替换旧 Token**，返回：
  - `token`：完整 Token（形如 `rsq_` 开头的随机串）。**这是唯一能看到完整 Token 的时机**，服务端只保存其 SHA-256 哈希；
  - `token_prefix`、`rotated_at`。

这两个路由属于仪表盘管理路由，受 `DASHBOARD_TOKEN` / 本地访问保护（见第 4 节）。通常你在仪表盘的设置页操作即可。

重要行为：

- **轮换即失效**：调用 rotate 后旧 Token 立刻不可用，所有安装了脚本的浏览器都需要重新填写；
- Token 丢失无法找回（服务端只有哈希），只能重新轮换。

### 2.3 在脚本中填写 Token

安装脚本后，在 Pixiv 页面点击 Tampermonkey 图标，脚本菜单里有两项命令：

- **「设置或更新救援 Token」**：弹窗粘贴 rotate 返回的完整 Token。Token 只保存在 Tampermonkey 的脚本存储（`GM_setValue`）里，不会写入页面或 Cookie；
- **「清除救援 Token」**：删除已保存的 Token。

未设置 Token 时访问失效页面，脚本会提示「未设置救援 Token，请通过油猴菜单完成设置」。

---

## 3. 救援目录：增量刷新与筛选

### 3.1 目录列表接口

仪表盘救援列表由 `GET /api/dashboard/rescues` 提供，支持以下查询参数：

| 参数 | 取值 | 说明 |
|---|---|---|
| `page` / `page_size` | 数字（page_size 上限 100，默认 12） | 分页 |
| `state` | `all` / `success` / `partial` | 救援状态（success=完整备份，partial=部分备份） |
| `item_type` | `all` / `novel` / `series` | 条目类型（仅在未指定 `content_kind` 时生效） |
| `content_kind` | `all` / `series` / `series_chapter` / `standalone` | 内容形态：系列 / 系列单章 / 独立小说。指定后优先于 `item_type` |
| `source_kind` | `all` / `bookmark` / `subscribed_series` / `following_user` / `user_backup` | 内容进入备份的来源（见下） |
| `search` | 任意文本 | 按标题 / 作者名做大小写不敏感的子串搜索 |
| `sort` | `checked_desc`（默认）/ `updated_desc` | 按最近检查时间 / 最近更新时间倒序 |

参数取值非法会返回 400 及中文错误信息（如「content_kind 参数无效」）。

### 3.2 来源筛选（source_kind）

每个救援条目会标注它是通过哪些同步渠道进入备份的，列表中显示为来源标签：

- `bookmark` — 「我的收藏」；
- `subscribed_series` — 「我的追更」（已订阅系列）;
- `following_user` — 「关注用户：某某」；
- `user_backup` — 「用户备份：某某」（整用户备份）；
- 其他未知来源统一归为「其他来源」。

一个条目可以同时有多个来源；`source_kind` 筛选时只要命中其中一个即匹配。系列的来源由其所有章节的来源汇总而来（订阅的系列还会额外带上 `subscribed_series`）。

### 3.3 目录的全量重建与增量刷新

- 目录是一张**派生快照表**（`rescue_catalog`），由后台任务全量重建（`rebuild_rescue_catalog`），每次重建记录 `refreshed_at` 时间戳；
- 当你在仪表盘提交或删除某条**纠错**时，服务端会对该条目做**增量刷新**（`refresh_rescue_item`）：只重算该条目、它所属 / 曾属的系列，以及受影响的章节，而不重建整个目录。刷新范围会沿「小说 ↔ 系列」的当前与历史归属关系自动扩散，保证系列统计和单章去重始终一致。

**哪些任务会重建目录**（没有独立的「重建救援目录」按钮，重建是这些任务的收尾步骤）：

| 触发方式 | 说明 |
|---|---|
| 小说状态检查（`novel_status`） | 设置页 `/dashboard/settings#manual`「手工同步」的「检查小说状态」按钮，或对应定时任务；最常用的重建方式 |
| 系列状态检查（`series_status`） | 同上，「检查系列状态」按钮 |
| 关注用户小说同步（`following_novels`） | 同步收尾时重建 |
| 追更系列同步（`subscribed_series`） | 同步收尾时重建 |
| 服务启动首轮 | 调度器发现 `rescue_catalog_meta` 为空时自动做一次首次重建 |

注意：任务被取消或被熔断中止（`stopped` / `aborted_reason`）时会**跳过**重建，避免用不完整的状态数据覆盖目录。此时任务日志显示黄色「部分完成」，目录仍是上一次的快照。收藏同步（`bookmark`）不重建目录。

因此想立刻刷新救援目录，最直接的做法是在 `/dashboard/settings#manual` 点「检查小说状态」。注意小说状态检查每轮只处理 800 篇（按最久未检查优先轮转），库很大时需要跑多轮才能覆盖全部。

### 3.4 纠错（override）

- `PUT /api/dashboard/rescue-overrides/<item_type>/<item_id>`，JSON 体：
  - `action`：`include`（强制收录）或 `exclude`（强制排除），必填；
  - `note`：备注，可选，最多 500 字符；
  - `item_type` 必须是 `novel` 或 `series`；对象必须已存在于本地库中，否则返回「救援对象不存在」。
- `DELETE /api/dashboard/rescue-overrides/<item_type>/<item_id>` — 撤销纠错，恢复按远端状态自动判定。

纠错写入总是先提交；若之后的增量目录刷新失败，接口会返回 500「救援纠错已保存，但目录增量刷新失败」——此时纠错本身已生效，稍后重试（再次提交同一纠错或等待全量重建）即可让目录同步。

---

## 4. 只读救援 API 与 Token 隔离

### 4.1 只读 API `/api/rescue/v1/*`

供用户脚本（或你自己的客户端）使用的公开只读端点，全部只支持 GET：

- `GET /api/rescue/v1/novels/<novel_id>` — 单篇小说（含 `text_raw` 正文、标题、作者、标签、救援状态等字段）；
- `GET /api/rescue/v1/series/<series_id>` — 系列元数据（标题、简介、封面、`expected_count` / `local_count` / `complete_count` 备份进度等）；
- `GET /api/rescue/v1/series/<series_id>/chapters?page=&page_size=` — 系列章节目录（按创建时间排序编号，page_size 上限 100，每章附 `api_path` 指向对应小说端点）。

统一约定：

- 认证方式：请求头 `Authorization: Bearer <救援Token>`；
- 响应信封：`{"ok": true, "data": {...}}`；出错时 `{"ok": false, "error": "..."}`；
- 每个成功响应的 `data` 都带 `source_notice`：「内容来自私人备份，并非 Pixiv 官方恢复」；
- 只返回**符合救援条件**的内容——远端仍正常在线的小说 / 系列即使有备份也会返回 404「救援内容不存在」；
- 响应头强制 `Cache-Control: no-store`、`X-Robots-Tag: noindex, nofollow, noarchive`，防止缓存与搜索引擎收录；
- 限流：按「客户端 IP + Token 前缀」滑动窗口限流，每 60 秒最多 120 次请求，超出返回 429「救援 API 请求过于频繁」。

### 4.2 救援 Token 与 DASHBOARD_TOKEN 的区别

两套 Token 完全隔离，互不通用：

| | 救援 Token | DASHBOARD_TOKEN |
|---|---|---|
| 保护范围 | 仅 `/api/rescue/v1/*` 只读端点 | 仪表盘及全部管理 API（含救援目录管理、纠错、Token 轮换） |
| 权限 | 只读，只能读取已判定为「可救援」的内容 | 完整管理权限 |
| 存放位置 | 数据库中只存 SHA-256 哈希；明文只在轮换时返回一次，由 Tampermonkey 保存 | 环境变量 / 配置（`.env`） |
| 使用方 | Tampermonkey 脚本（会随请求发到浏览器侧） | 只应由你本人在仪表盘使用 |
| 泄露影响 | 他人可读取你的救援备份；轮换一次即可作废 | 他人可完全控制服务，必须立刻更换并检查 |

因此：**绝不要把 DASHBOARD_TOKEN 填进油猴脚本**。脚本一侧只需要救援 Token；即使脚本或浏览器侧泄露，攻击者也拿不到任何管理能力，且你随时可以通过 rotate 一键作废。

反之，救援 Token 无法访问任何 `/api/dashboard/*` 路由——Token 状态查询与轮换本身就受 DASHBOARD_TOKEN 保护。

---

## 5. 故障排查

### 503「救援目录尚未生成」

- 现象：仪表盘救援列表返回 503，错误信息「救援目录尚未生成」。
- 原因：目录快照表还从未完成过第一次全量重建（`rescue_catalog_meta` 为空），常见于全新部署或刚清空数据库。
- 处理：等待 / 触发一次目录重建任务后重试。此错误只影响仪表盘列表；只读 API `/api/rescue/v1/*` 不依赖目录快照，不受影响。

### 列表带有 `stale: true` 标记

- 现象：`GET /api/dashboard/rescues` 响应的 `data.stale` 为 `true`，仪表盘可能显示「目录数据可能已过期」类提示。
- 判定规则：目录的 `refreshed_at` 距今超过阈值即视为过期。阈值取「小说状态检查间隔」与「系列状态检查间隔」两者较大值的 2 倍，且不低于 24 小时（对应 `auto_sync_novel_status_interval_hours` / `auto_sync_series_status_interval_hours` 配置）。`refreshed_at` 缺失也视为 stale。
- 含义：列表仍然可用，只是快照可能落后于最新的状态检查结果。等待下一次自动重建，或手动触发重建即可。

### 401 未授权（脚本提示 Token 问题）

- 「未设置救援 Token，请通过油猴菜单完成设置」— 脚本存储里没有 Token，按第 2.3 节设置；
- 「救援 Token 无效，请在网站设置页轮换后重新填写」— 服务端返回 401，多数是 Token 已被轮换过。到仪表盘重新 rotate 并把新 Token 填入脚本；
- 服务端从未生成过 Token（status 显示 `configured: false`）时，任何请求也会得到 401「需要救援 Token / 救援 Token 无效」。

### 429 请求过于频繁

单个「IP + Token」组合 60 秒内超过 120 次请求会被限流。正常阅读不会触发；如果触发，稍等一分钟再试。

### 404「救援内容不存在」

- 该 ID 在本地库中不存在；或
- 该内容不满足救援条件（远端仍在线、本地正文不完整、被 `exclude` 纠错排除等）。
- 若你确认远端已删除但状态还没同步到本地，可等待下一次状态检查，或在仪表盘对该条目提交 `include` 纠错。

### 脚本没有在失效页面上显示救援面板

- 确认页面 URL 是 `novel/show.php?id=...` 或 `/novel/series/<id>`；
- 确认页面确实出现了失效文案（脚本按固定的日 / 中 / 英文案匹配，见脚本内 `UNAVAILABLE_MARKERS`）；页面能正常显示正文时脚本不会介入；
- 检查 Tampermonkey 是否允许连接你的服务器域名（`@connect` 声明）；
- 「无法连接救援服务 / 响应超时」提示说明服务器不可达（超时 15 秒），检查服务与域名解析。

### 500「救援纠错已保存，但目录增量刷新失败」

纠错本身已写入成功，只是派生目录没刷新。重试同一操作，或等待下一次全量重建即可恢复一致。
