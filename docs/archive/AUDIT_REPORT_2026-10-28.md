# 全项目审查报告（2026-10-28）

基线：main `51f8611`（2026-10-27，救援脚本 0.12.0），全量 `python -m pytest -q` 1497 passed / 6 skipped（Windows）。

方法：七个切片并行审查（同步与调度、存储与推荐、Web 后端与安全、AI 子系统、救援油猴脚本、前端模板、活跃文档），主循环逐条复核 P2 与主要性能结论。性能数字来自合成库：6000 本小说、300 作者、300 系列，正文中位约 1.5 万字，库 835 MB；前端数字是 Playwright 移动视口 + CDP 4 倍 CPU 降速。除注明外均为 Windows 开发机实测，Linux 生产需复测。本轮只改文档，不改代码。

标记：✔ 主循环已独立核实（读码或复现）；○ 子代理复现，主循环未重做。

## 结论

> 修复进度不回写本报告：2026-10-29 已修 A1、S1–S4、D1–D3、F2、W4、W6 与性能 P3 / P4，最新状态见 [项目现状](../STATUS.md)。

没有 P1。确认 12 项 P2（其中 9 项主循环已独立核实），40 条 P3（部分条目合并了同类问题），23 项可量化的性能改进；死代码集中在 OAuth 回调残留、待删除 confirmed 分支与少量只写不读的字段，AI 子系统 91.8% 的代码在 main 上可达。

建议顺序：

1. **先改生产配置（不动代码）**：W1。`.env` 设 `PIXIV_COOKIE_SECURE=1`，Nginx 接 Cloudflare 真实 IP 后开 `DASHBOARD_TRUST_PROXY=1`。
2. **一批小而确定的修复，每项带回归测试，不改变可见行为**：A1（一行）、S2（防覆写）、S1（错误页）、S3 / S4（Cookie 与沉默的绿色）、D1（目录 pending）、D2 / D3（has_content）、F2（未保存提示）、W4 / W6、P3 / P4（接口体积）。
3. **会改变可见行为、需要真机看一眼的**：F1（阅读页与 EPUB 解析正文标记）、Tailwind 换静态 CSS、油猴 U1–U5 与 P16 / P17（升 0.12.1）。
4. **需要拍板的**：cron 重启补偿、user_backup 失败阈值与是否每本重抓、统一需求 §9.3 与 0.12.0 行为的冲突、旧 timer 部署路径是否继续保留。

## 1 缺陷

### P2

- **W1 ✔ 生产环境里任何人都能让你暂时登不上。** `webapp.py:_client_addr` 在 `DASHBOARD_TRUST_PROXY` 未开时直接用 `remote_addr`；本机 Nginx 之后它恒为 127.0.0.1，登录失败限流与救援 API 限流于是全站共用一个桶。`is_blocked` 在校验密码之前，陌生人输错 5 次后你用正确密码也得 429（锁 300 秒）；每分钟 120 个无 Token 请求即可让油猴脚本全部 429。10-08 部署门禁记录显示当时生产既没开 `DASHBOARD_TRUST_PROXY` 也没设 `PIXIV_COOKIE_SECURE`，后者缺省时 30 天登录 cookie 也没有 Secure 标记（`.env.example` 的旧注释误称「检测到反代头会自动开启」）。现状需上服务器 `grep -E 'TRUST_PROXY|COOKIE_SECURE' .env` 确认。修法：`.env` 设 `PIXIV_COOKIE_SECURE=1`；Nginx 用 Cloudflare 的 `CF-Connecting-IP` 作真实 IP（`set_real_ip_from` CF 段 + `real_ip_header`），`X-Forwarded-For` 改为 `$remote_addr`，再开 `DASHBOARD_TRUST_PROXY=1`、跳数保持 1；源站 443 最好只放行 CF 段。
- **D1 ✔ 取消追更的系列，其章节仍以「我的追更」进救援目录。** `storage/rescue.py` 目录查询只排除章节自身的 pending 行，不看父系列；本人取消追更后系列条目被排除，但 Pixiv 删掉的章节仍作为 `series_chapter`、`personal_relation=True` 出现，正是 RESCUE_USER_GUIDE 要防的情况。增量刷新走同一查询。修法：pending 判定扩到父系列，人工 include 仍优先，章节自己有 bookmark 来源时保留。
- **D2 ✔ 救援 lookup 与正文页读整本正文来判断「有没有正文」。** `_series_summary_rows` 用 `TRIM(COALESCE(nt.text_raw,''))` 计算 `complete_count`，每次评估系列都读全部章节正文；`get_rescue_items_batch` 名为批量，实际逐 ID 调 `_novel_rescue_row`，每篇都取 `text_raw` 再丢弃。合成库上 lookup 40 个 ID 要 3.9–4.5 s、读盘 187 MB，改用已有的 `has_content` 后 270 ms、17.5 MB，结果一致。目录侧早已用 `has_content`，两边对「只含换行/全角空格」的章节判定也不一致（D3）。正文详情页每次打开也经 `evaluate_rescue_novel` 跑一遍父系列聚合。
- **F1 ✔ Web 阅读页与 EPUB 不解析 Pixiv 正文标记。** `dashboard_novel_detail.html:formattedContent` 只把 `text_raw` 逐行转义成 `<p>`，`[newpage]`、`[chapter:…]`、`[[rb:…]]`、`[[jumpuri:…]]` 等都按原文显示；`epub_exporter.py` 同样，且写死 `lang="ja"`。仓库里 `userscripts/pixiv-rescue.user.js` 已有按手机站规则、用 DOM + textContent 构建的安全实现，可抽出复用。
- **F2 ✔ 同步设置页「有未保存的修改」一打开就出现，保存后也消不掉。** `dashboard_settings_sync.html` 的 deep watch 用默认 `flush:'pre'`，回调在 `settingsReady=true` 之后才跑。修法：watch 改 `flush:'sync'`，或跨一个 `nextTick` 再置 `settingsReady`。
- **S1 ✔ 关注作者扫描与系列翻页把 Pixiv 错误页当成空页（A14-01 的漏网点）。** `sync_engine.py:_sync_author` 在请求前就写入 `user_last_synced`，`user_novels` 返回 `{"error": …}` 时经 `getattr(…, "novels", []) or []` 变成空列表、`next_url` 为空，作者被推后一整圈（约 12.8 天），stats 没有 failed / incomplete / aborted_reason，日志是绿色；系列 `last_order` 翻页同样静默停止。`response_is_pixiv_error_page` 只接入了 5 个分页站点，这两处没有。
- **S2 ✔ `novel_detail` 返回错误 JSON 时，已归档作品被覆写成占位数据，且此后不再修复。** pixivpy 的 `JsonDict.__getattr__` 对缺键返回 None，`_sync_novel_inner` 里 `getattr(detail, "novel", novel)` 的默认值永远不生效；若随后 `webview_novel` 成功，作品被写成 `title=novel_<id>`、`user_id=0`、`raw_json=NULL`、tags 为空，`archive_complete` 仍为真。需要 detail 失败而 webview 成功，概率低但不可逆（只有对关注作者每本重抓的 user_backup 可能碰巧修回）。修法：取到 detail 后先做错误页 / `detail.novel is None` 判定，按失败处理。生产是否已中招，可在服务器上只读检查：`sqlite3 -readonly data/state/pixiv_sync.db "SELECT n.novel_id, n.title, n.user_id FROM novels n JOIN novel_texts nt ON nt.novel_id = n.novel_id WHERE (n.title = 'novel_' || n.novel_id OR n.user_id = 0) AND nt.has_content = 1 LIMIT 20;"`
- **S3 ✔ 自动刷新的 Web Cookie 不进 `os.environ`（配置了账号密码时）。** `_save_web_cookie_to_env` 只写 `.env` 与本轮 settings 对象，`load_dotenv` 默认不覆盖已有变量，下一轮任务仍拿到旧 Cookie，每轮追更与待删检测都重新 401 并重跑一次 Playwright 登录（账号风控风险）。叠加：`playwright_login.py:_ensure_display` 启动 Xvfb 后设 `DISPLAY`、结束时不还原，同一进程第二次登录必败（Linux 上为推断）。照 `oauth_helper.save_to_env` 同步写 `os.environ`，并在 finally 里还原 DISPLAY。
- **S4 ○ Cookie 失效后两处「沉默的绿色」。** 追更同步在 Web API 401 且无法刷新时静默回落到数据库订阅清单，stats 没有任何标记（新追更永远进不来）；待删检测把 `truncated` / `aborted_reason` 嵌在 `bookmark` / `series` 子字典里，`_task_log_status_for_stats` 只看顶层。两者都记 succeeded。
- **S5 ○ `PIXIV_PROXY` 对 App API 无效（使用代理时）。** `auth.py` 设置的 `api.proxies` 是 pixivpy3 3.7.5 不读的属性，认证与收藏/关注/详情/正文/状态检查等请求都不走代理，`verify_ssl` 也从未传入；只有 Web 接口与资源下载显式传了 proxies。生产不用代理。
- **S6 ○ 新装且没有 Cookie 时，追更「方式2」回落把所有已归档小说所属的系列标成已订阅。** 之后按订阅爬全部章节，并把关注扫描/全量备份拿到的系列标成「我追更过」，污染救援目录的 personal_relation。生产已有 Cookie 与订阅行，不触发。
- **A1 ✔ `ai/providers.py:_iter_sse_lines` 引用了未定义的 `logger`。** 10-20 的修复批（`0c7578e`，已部署）加了「SSE 解码出现替换字符时告警」，但文件里没有 `import logging` / `logger`。流没收到 `[DONE]` 就结束、且解码出 U+FFFD 时抛 NameError：JSON 已收全的关键词清洗被记为 failed，attempt 永远停在 running（job 已不是 running，fail_stale 不收）。偏好画像本身不受影响（`clean_keywords` 外层兜底降级）。`python -m pyflakes src` 全仓只报这一处；该批次只跑了 `compileall`，它检查不出未定义名称。修法：补 logger 定义与回归测试；移植到 ai-writing 前先修。

### P3

同步与调度：

- S7 ✔ `jobs/services.py:_login` 先以 `stop_requested=None` 打了重试补丁，`BookmarkNovelSyncService` 带取消回调的补丁因幂等判定被跳过；经 `_login` 的路径（全量备份、三种状态检查、待删检测）重试退避退化成不可取消的整段 sleep，单次调用最长 35 秒。修时要同时改 `web/utils.py` 三处 `except Exception`，否则取消会被吞成 unknown。`RETRY_PATCHED_METHODS` 注释引用的 `tests/test_retry_coverage.py` 不存在，补丁的幂等与取消绑定零测试覆盖。
- S8 ○ 收藏预算：`content_unavailable` / failed 的条目计入 `synced_items` 却永不归档，每轮先重做一遍；预算触顶时不置 truncated。不可用件累积到 `max_items_per_run` 后，排在后面的未归档收藏永远同步不到，日志一直绿色。生产是否已触顶，看收藏任务的 `content_unavailable + failed` 是否每轮恒定。
- S9 ○ 「补缺资源」只看 assets 表：磁盘上被删或被截断的封面仍有记录，`archive_complete` 为真，强制重访也不重下；资源文件在 `os.replace` 前不 fsync。
- S10 ○ user_backup 与系列章节路径硬编码 `restrict="public"`：私密收藏的作品经这两条路径重访且正文有变时，整套文件写进 public 树、`restrict_value` 翻成 public，private 树里的封面成孤儿，EPUB 导出丢封面。
- S11 ○ 批量导出 zip 只用标题做文件名，同名作品互相覆盖；EPUB 见 F1，另外封面固定名 cover.jpg、正文是单个 XHTML（200 万字 6 MB）。
- S12 ○ `settings.py` 用 `str(sync_raw.get(...))`：手改 YAML 留空的 `auto_sync_timezone:` / `*_cron:` 变成字符串 "None"，时区回落 UTC 且每次告警，cron 回落 interval；`PIXIV_VERIFY_SSL` 拼错（如 flase）解析为 False（应 fail-closed）。只影响手改配置。
- S13 ○ `sync-bookmarks` 只在 truncated 时退出 1，收藏列表拉取失败（aborted_reason=fetch_failed）仍退出 0，旧 timer 看不出失败。

存储与推荐：

- D3 ✔ 读接口的 `complete_count` 用 SQL `TRIM`（只剥空格），目录用 `has_content`（Python `strip()`）；只含换行或全角空格的章节，目录显示 partial、`get_rescue_series` 却回 success。随 D2 一起修。
- D4 ○ 删除小说会清掉 `preference_analyzed_novels` 标记但不扣减已累加的词频；作品被同步带回后再分析，词频与 `novel_count` 翻倍。修法要改 `tests/test_delete_user_orphans.py` 锁定的行为。
- D5 ✔ `prune_preference_term_noise` 在词项表超过 30 万行后每批都删 `count<2`，包括本批刚加的词；每批各出现一次的词永远攒不起来，画像结果取决于分批顺序。改为周期性或全局阈值清理。
- D6 ○ 在 `read_transaction()` 里调用 `transaction()` 会直接报「cannot start a transaction within a transaction」；目前无调用方触发。

Web 后端：

- W2 ○ `web/utils.py:_yaml_write_lock` 只包住写文件那一步，`set_auto_sync_enabled` 与 `save_sync_settings` 都在锁外读旧 YAML；两处并发保存时后写者用旧快照覆盖前者（复现中 `auto_sync_enabled` 的修改丢失）。10-20 的「写锁」修复只完成一半：锁要包住读-改-写全程。
- W3 ○ `SettingsManager.load` 无代际：缓存过期重读期间另一线程保存并 invalidate，旧值会被写回缓存，新 cron 可能晚一个周期生效。加 generation 计数。
- W4 ✔ `DELETE /api/dashboard/bookmarks/<id>` 不调 `refresh_rescue_item`，删完收藏后救援页仍显示「我的收藏」，而实时的 `personal_hold` 已是 False。
- W5 ✔ 救援封面视图设的 `private, max-age=86400` 被同文件 `_rescue_security_headers` 一律改成 `no-store`，视图里那两行是死逻辑。二选一：保持 no-store 并删掉视图里的缓存设置（R-18 封面不落浏览器磁盘更稳），或对 `/cover` 跳过覆写。
- W6 ✔ `/api/rescue/v1/lookup` 去重用 `chunk not in novel_ids`，拿 str 和 list[int] 比较，永远不生效。
- W7 ○ 进度保存、EPUB 导出、cron 预览、定时同步开关等 5 个接口在请求体不是 JSON 对象或字段类型不对时回 HTML 415/500，前端 `errorText` 读不到错误。统一用 `ai_web.py` 的 `require_json_object` 式校验。
- W8 ○ `_run_shared_web_job` 里 `settings_manager.load()` 与建库在 try 之外，出异常时该行 task_logs 一直停在 running，直到下次重启。
- W9 ✔ 推荐卡片可以「屏蔽作者」，但屏蔽列表的查看与解除（`GET/DELETE /api/dashboard/recommendations/mutes`）没有任何前端入口，屏蔽后无法撤销；标签屏蔽也没有入口。
- W10 ○ `preference_web.py` / `ai_web.py` 的 `fail()` 把所有异常（含 database is locked）都回 400 + 原始异常文本；冲突应回 409，服务端错误 500 并只回通用文案。

救援油猴脚本（行号指 `userscripts/pixiv-rescue.user.js` 0.12.0）：

- U1 ✔ `apiGet` 先 `JSON.parse` 再看状态码（354–370）：部署重启期间 Nginx 502 / Cloudflare 52x 的 HTML 错误页被判为 `invalid_response`，阅读层显示「备份数据没有完整读到」，列表页则完全没有提示。先按状态码分类。
- U2 ○ GM API 晚注入时每次重试失败都会 prepend 一条红条，不去重、关不掉，功能恢复后仍盖住页头（292–300、2822–2830，待真机）。
- U3 ○ 「作品情报」面板（z-index 2147483500）在 SPA 切页时不清理，会盖住新页面（2084/2469）。
- U4 ○ 过期的 `?p=N` 超出页数时服务端钳到末页，翻页条却按请求页画（1922、1985–2019）。改用 `data.page`。
- U5 ○ 救援 API 限流只按地址（指南写的是「IP + Token 前缀」），封面与正文共用 120 次/60 秒；120 话的系列一路滑到底 4 秒内发 87 个请求，超过约 115 张未缓存封面就 429，之后 `onFail` 永久换成占位图不再重试。封面单独限流，或下次进入视口时重试。与 W1 叠加时更容易触发。
- U6 ○ 语言前缀正则只认两个字母，`/zh-tw/` 页注入后静默（1424）；手机站是否用这个前缀待确认。
- U7 ○ `/^\n+|\n+$/` 遇到页内连续 k 个空行是 O(k²)（1134），1 万个空行 1.5 s；只有恶意输入会触发。

前端模板：

- F3 ○ 日志页 `fetchData` 没有请求代号，快速切换筛选或翻页时旧响应会覆盖新结果；分页不像其他列表那样把 page 夹到 total_pages；多处错误没走 `errorText`。
- F4 ○ 首页 `onMounted` 不等 `fetchStatus()` 就调 `fetchJobStatus()`，后者因 loading 立即返回，任务正在跑时也按空闲 20 s 布防。
- F5 ○ `/token-login`：自动登录不看 `resp.ok`，403/500 时仍进入「正在后台模拟登录」并开始轮询；直接填入凭证失败只显示「失败」；整页不调 `initVueApp`，侧栏和底栏是空元素。
- F6 ○ `csrfFetch` 的 token 只缓存一次：另一个标签页重新登录后（`session.clear()` 换 token），旧页面所有变更请求都 403。遇到 CSRF 403 时重取一次 token 再重试。
- F7 ○ 「保存 Agent」没有进行中标记，新建时连点两次发两次 POST；书库底部「共 N 条」显示两遍；设置页 `showSaveStatus` 不清旧定时器，4 秒内连点两次保存时第一次的定时器会提前抹掉第二次的结果。
- F8 ○ 阅读进度：本机 localStorage 有记录就永远不看服务器进度；服务器进度是整数百分比，长篇里 1% 约 6 屏。

AI 子系统（均用临时库加假 HTTP 层或离线纯函数复现，没发网络请求）：

- A2 ○ 流式 `delta.content`（Anthropic 为 `delta.text`）没有 `isinstance(str)` 检查（`providers.py:983,1268`，非流式 1097 行有）。网关返回数组分段内容时拼接抛 TypeError，`keyword_clean.py` 的 except 里再拼一次又抛，异常逃出 `clean_keywords`，违背「任何异常都降级」；`jobs/tasks.py` 外层兜住，画像不受影响，但 job 先卡 running、后被改成 partial。
- A3 ○ 遗留 v1 密文：`resolve_candidates` 用 v1 密文算 `provider_config_hash`，随后 `_load_provider_config` 透明升级并回写 v2，执行快照校验重算 hash 对不上，报「Provider 配置已变更」：首次清洗必败、第二次成功。`model_sync.py:124` 已规避同一顺序问题，路由没有。
- A4 ○ 路由心跳线程（`model_router.py:917-932`）对 `heartbeat_ai_job` / `ai_job_should_stop` 没有异常保护，一次 `database is locked` 线程就静默退出，租约停止续期，仍在输出的 job 可能被 fail_stale 误判。`model_sync.py` 的心跳有保护，照抄即可。
- A5 ○ 偏好分析的取消与调度让位传不进路由：`_stream_route` 构造 `RouteRequest` 不传 `is_cancelled`，只能等 AI 调用结束（默认 120 s × 3 次，叠加非流式回退最坏约 12 分钟），期间占着全局唯一的任务槽；终态语义正确。
- A6 ○ 钉 IP 后 requests 按 IP 判断 `NO_PROXY`：`NO_PROXY=localhost,.lan` 时原 URL 绕过代理，钉成 127.0.0.1 / 192.168.x 后不再绕过，本机或局域网 http 模型（需 `PIXIV_AI_ALLOW_PRIVATE_HOSTS=1`）的 Authorization 会明文发给环境代理；https 不受影响。
- A7 ○ probe-models 借用已保存的 Key 时只比对 base_url，provider_type 与 proxy 取自请求：两边 base_url 都为空时改 provider_type，会把 Anthropic Key 当 Bearer 发到 OpenAI 默认地址（需已登录）。
- A8 ○ MODEL_ROUTING_GUIDE 说运行中截止超限以 `route_budget_exhausted` 失败，实际记为 cancelled 且没有原因（被 `test_ai_model_router.py` 锁定），健康横幅看不到超时；`test_deadline_expiring_during_stream_cancels_before_the_delta` 余量只有 0–1 秒，机器忙时偶发失败（与 09-28 的 flaky 记录一致）。

## 2 逻辑与方案优化

不是缺陷，但有更稳或更省的做法；标「需拍板」的会改变被测试锁定的现行为。

- 调度器 cron 任务没有重启补偿（需拍板）：`web/managers.py:_restore_schedule_from_task_logs` 只处理没配 cron 的任务，而生产 11 个任务全是 cron。崩溃或部署打断后，错过的槽直接跳到下一槽，被杀掉的那一轮也丢：user_status 最长丢 2 天，user_backup（单轮约 2.3 小时）在运行中被 `./update.sh` 重启会丢 3 天进度。可用 `croniter.get_prev` 求最近应跑的槽，上次成功早于它就按现有错峰补跑；现行为被 `test_scheduler_persistence.py::test_cron_tasks_are_not_restored_from_history` 锁定。
- `user_last_synced` 在作者「开始」时就写：被让位、取消或出错的作者都算扫过，推后整圈（S1 的放大器）。改为自然结束才写；失败保留旧值并计数，连续 3 次失败照常写入，防止坏作者霸占队首。
- user_backup 失败比例阈值没有最小样本（需拍板）：前 4 本里失败 1 本就超过 20%，整个用户中止，轮转里被跳过一整圈。加最小样本（如已处理 ≥ 10 本）；现行为被 `test_backup_partial_completion.py` 锁定为有意设计。
- 让位护栏重复：让位后 600 s 的续跑必然落在 6 小时冷却期内，`MAX_CONSECUTIVE_PREEMPTIONS=2` 几乎不可达。只保留冷却期，或在 JOB_SYSTEM 里写明。
- 油猴 lookup 回调直接写卡片，不核对它是否仍是那部作品；若 Vue 翻页复用节点，可能画到别的卡上。加守卫：卡片仍在 DOM、仍是 card-pending、链接 ID 没变（Vue 是否复用节点待真机）。系列目录首次加载失败时没有「重试」按钮；观察器里延长 `fastScanUntil` 的那行在首个 15 秒后不再起作用；`window.GM_xxx = noop` 兜底改用局部变量更稳（待确认）。
- 油猴启动角标在所有 Pixiv 页（含健康页）显示 6 秒；健康页测试查的是 `[data-pixiv-rescue]`，角标用 `data-pixiv-rescue-boot`，「零 DOM 改动」断言管不到它。可限定到目标页，或为诊断保留。
- 油猴脚本里硬编码的服务器域名建议保留：零配置、更新身份稳定，域名本来就能在证书透明日志查到，没有 Token 的安装在发请求前就被拒绝；代价是 fork 要同时改三处（都被测试锁定）。
- 截止超时与用户取消共用 cancelled 终态，`ai_jobs` 里两者无法区分（见 A8）；cancelled 的 CAS 可附 `error_message`。

## 3 性能

前几项收益最大；「不改变可见行为」的项都可以单独做。

- **P1 前端 ✔ Tailwind Play CDN 换成静态 CSS。** `base.html` 加载未锁版本的 `cdn.tailwindcss.com`（约 400 KB 脚本，运行时 JIT，并监听 DOM 变动重新生成样式）。按 22 个模板生成的静态 CSS 41 KB（gzip 7.6 KB），覆盖 CDN 实际生成的全部类；各页首屏快 0.55–0.93 s，主线程阻塞少 0.5–0.9 s，阅读页滚动平均帧 76 → 54 ms。仍不需要构建步骤：提交生成好的 CSS，只在改样式时手动重生成一次，并加一个测试比对模板里的类与 CSS。顺带消除未锁版本的供应链风险。
- **P2 存储 ✔** D2 的 lookup：3.9–4.5 s → 270 ms。
- **P3 Web ✔ 正文详情接口发两份正文且中文被转义。** `/api/dashboard/novels/<id>` 同时返回 `text_raw` 与 `text_markdown`（后者是标题 + 简介 + 同一正文）；Flask 默认 `ensure_ascii`，汉字变成 `\uXXXX`。20 万字小说 2.4 MB，只发一份 UTF-8 是 0.61 MB；救援正文同理（1.22 MB → 0.61 MB，手机端 `JSON.parse` 约 400 ms → 7–34 ms）。设 `app.json.ensure_ascii = False`（或只对这两类端点），详情只在 `text_raw` 为空时回 markdown。
- **P4 Web ✔ 日志列表 `SELECT *`。** `storage/tasks.py:get_task_logs` 每行同时返回 `logs_json` 原串与解析后的 `logs`，列表页根本不用（详情另走 `/api/dashboard/logs/<id>`）；一页最多 4–5 MB，开「自动刷新」后每 10 s 一次，且后台标签页不停。列表只取摘要列，去掉 `logs_json` 后查询 66 → 15 ms。
- **P5 Web ○** `_job_to_dict_unified` 总带上完整 job.logs（上限 500 条）并 deepcopy；首页、壳组件与 auto-sync 状态轮询都用不到。加 `logs_tail` 参数。
- **P6 前端 ○** 封面与头像没有一处 `loading="lazy"`：60 张卡的书库页首屏只看得到 1 张，却同时发出 60 个图片请求，而 waitress 只有 8 个线程。
- **P7 前端/Web ○** 手机端侧栏 `display:none`，但仍挂载并在每次导航发 3 个请求（`shell-data`、`status`、`auto-sync/status`）；`status` 还顺带跑 4 个 COUNT(*) 与完整 latest_job。合并进 `shell-data`，手机端不挂载侧栏。
- **P8 Web ○** 每个已登录请求认证门约 40 ms（开关两次 SQLite 连接，每次执行 `PRAGMA journal_mode=WAL`）。WAL 是持久化到库文件的，只需在 init_schema 设一次；认证结果可按 token_hash 做 30–60 s 进程内缓存。
- **P9 Nginx ✔/○** `/proxy/image` 的响应带 `Vary: Cookie`（Flask 会话被认证门访问过），Nginx 按整串 Cookie 分别缓存，365 天的图片缓存基本只在同一会话内命中，形同虚设。接受「已缓存的公开 pximg 图无需登录」就在该 location 加 `proxy_ignore_headers Vary;`；否则删掉 proxy_cache，改由 Flask 回 `Cache-Control: private, max-age=31536000, immutable`。另外上游 404 被改写成 502，Nginx 无法负缓存，失效封面（救援场景很多）每次都穿透到 pximg：404/410 应原样回 404。书库卡片可优先用本地已下载封面。
- **P10 存储 ○ 缺索引**（EXPLAIN 为 SCAN 或 TEMP B-TREE）：`novels(total_bookmarks DESC, novel_id DESC)` 按收藏排序 520 → 11 ms；`series(is_subscribed, last_seen_at DESC, series_id DESC)` 追更页 97 → 23 ms；`recommendation_items(run_id DESC, score DESC, id DESC)` 首页推荐 236 → 10 ms；`recommendation_items(novel_id)` 删除小说 72 → 8.6 ms。`recommendation_items` 没有清理机制，随推荐轮次线性增长。
- **P11 存储 ○** 统计未分析小说时读整段 `text_raw`（4.5 s、410 MB），改 `has_content` 后 0.67 s；只影响首跑或重置。
- **P12 存储 ○** `init_schema` 稳态约 950 ms，主要是两个 `IS NULL` 回填探针各做一次全表扫描；Web 进程只付一次，CLI 每次调用都付。
- **P13 存储 ○ 救援目录重建可先筛 ID。** 全量重建扫全部 novels（`status` 列在 `raw_json` 之后，要读溢出页）并聚合全部系列章节再在 Python 里丢掉九成；先用 `idx_novels_status`、`series.status='deleted'` 与 include 覆盖取出 ID 再走原查询：1.8–2.1 s → 652 ms，读盘 89.5 → 11.2 MB，目录逐条一致，持写锁时间同比缩短。这不是被否决的「增量化」，仍是同一事务的全量重建。注意 10-20 报告里的「5000 本 577 ms」没有留下测量条件，仓库里的目录性能测试夹具正文只有两个字，接近生产的合成库上约 2 s，不能沿用旧结论；生产实际耗时可查 task_logs 的 `rescue_catalog_duration_ms`。
- **P14 存储 ○** 偏好词频 UPSERT 前先 `rows.sort()`，按 B 树顺序写入：28 万键并入 35 万行 156.7 → 82.1 s（Zipf 合成数据）。不要删 count 索引，删了 top80 查询会从 2.6 ms 变成 16–23 s。
- **P15 Web ○** YAML 解析换 `CSafeLoader`：69 → 6 ms（设置缓存 5 s TTL，过期后的那次请求受益）。
- **P16 油猴 ✔ 列表扫描。** `cardIsBroken` 对每个链接往上爬 4 层，每层都取祖先容器的 `textContent`（1460–1478）。祖先文本必然包含子孙文本，某层超过阈值后更高层只需查 className，结果等价。30/60 张卡单轮 14.5/59 ms → 3.2/9.1 ms（4 倍降速下 183/529 → 46/98 ms）；扫描前 15 秒每 500 ms 一次、之后每 3 秒一次，外加每批 DOM 变动一次。
- **P17 油猴 ○** `readCachedLookup` 每张卡都解析整份缓存（400 条 114 KB，每次 5 ms，4 倍降速 48 ms），每轮扫描只读一次即可；`URL.createObjectURL` 只建不回收（120 话的系列 84 个），用完 revoke（`apiGetBlobUrl` 函数名被测试锁定，要保留）。
- **P18 油猴 ○** 没有 `[newpage]` 的 20 万字正文首次布局慢（CDP 估 2–4 s，待真机）；真机确实卡再做分批追加。

- **P19 同步 ○ user_backup 逐本无条件重抓。** 每本都 `novel_detail` + `webview_novel` 两次请求，逐本没有间隔，是仓库里唯一不遵守请求间隔的批量路径，也是单轮最长 2.3 小时的主因（约 8000 本 × 约 1 s）。先查 `novel_archive_complete` 跳过已完整的、每周一次全量刷新，最多省约 90%；但它是唯一会重抓已归档正文的路径（也是 S2 唯一的修复途径），改之前要确认这是不是有意的。
- **P20 同步 ○** 已归档判定是纯本地点查，却仍 sleep `delay_seconds_between_skips`：收藏每轮约 20 页 × 30 条 × 0.1 s ≈ 60 s，6 轮/天约 6 分钟。本地跳过不 sleep，真实请求之间的间隔不变（不影响 15.6% 占空比结论）。收藏历史补齐后每轮仍走满 20 页，可在连续 N 页全部已完整时停，每天一次全量扫描回填中段缺口。
- **P21 同步 ○** 待删检测对已标 deleted 的候选每轮仍重查 `novel_detail`（失败分支不 sleep，连发）；novel_status 每批约 10% 是已知删除的重复确认。候选先按 `status='deleted'` 过滤，deleted 仿 `NO_NOVELS_RECHECK_DAYS` 降频。
- **P22 AI ○** `model_router.py:_matching_catalog_model` 每次把整个 Provider 目录物化成 dict（每行两次 JSON 解析）再线性查 model_key，`ai_health` 还按每个固定 Agent 再扫一遍：300 模型 0.21 s，5000 模型 / 10 个 Agent 10.5 s。改成按 `(provider_id, model_key)` 点查。
- **P23 AI ○** `storage/ai/catalog.py:upsert_discovered_models` 没变化也逐行重写：5000 模型原样重同步 2.7–2.9 s，全程持写锁。改 `INSERT … ON CONFLICT DO UPDATE … WHERE` 有变化才写；`NOT IN` 的 5000 个占位符依赖 SQLite ≥ 3.32，换临时表可消除。

## 4 死代码与残留

可直接删除（整仓 grep 无生产调用、无测试锁定）：

- `jobs/services.py:_list_all_users`；`storage/rescue.py:has_rescue_series_cover`（只在注释里被提到）；`webapp.py` 的 `_AutoSyncSchedulerOwner.sync_job_manager`、`app.config["run_shared_job"]`、认证门里的 `/static/` 豁免（包内没有 static 目录）；`/api/save-token` 里永不成立的 `user_id <= 0` 分支。
- `web/utils.py:_external_base_url` → `OAuthTask.callback_url` → `_build_login_url(callback_url=…)` 整条链与 `oauth_helper.py:pixiv_callback_url` 字段：拼出的 `/oauth/callback` 指向已删除的路由，前端不读；`.env.example` 的 `PIXIV_EXTERNAL_BASE_URL` 已改注为「历史遗留」，删代码时一并删除。
- `token_login.html` 的 `?oauth_task=` / `?error=` 分支及其唯一使用的 `GET /oauth/task/<id>`（与 `/api/token-jobs/<id>` 逐字相同）。
- `dashboard_follows.html` 的「已清空 / cleared」tab（没有任何代码写入该状态）；`dashboard_novels.html:formatCatalogDate` 里写成 `\\d` 的偏移量正则永不匹配；`window.aiApi.preferenceProfiles`；`base.html` 里没有模板使用的 `library-hero/subtitle/panel/title/input/table`（删前核对 style guide 与 `test_frontend_library_os.py`）。
- 存储：`pending_deletions` 的 `confirmed` 状态从不落盘（确认时同一事务就删掉了该行），`pending_and_watermarks.py` 的 confirmed 分支与 `webapp.py` 确认路由里的补偿 UPDATE 不可达；`users.py` 清理 `item_type='user'` 的 pending（只会写入 novel/series）；`rescue_catalog.refreshed_at` 每行都写、只读 meta 表那份；`_novel_evaluation_payload` 的 `has_content is None` 分支；`get_rescue_items_batch` 里的 `if …: pass`。
- 油猴脚本：对 `="badge"` 的检查（0.5.0 后无代码写入）、`rescueErrorText` 的非 pageLevel 分支、「这一页没有拯救系列」分支。
- 同步：`sync_following_list` 全表读 `existing_users` 只为一条 debug 日志；追更 `stats["assets_downloaded"]` 初始化为 0 后从不累加（任务日志恒显示 0）；`bookmark_{restrict}` 水位、`following_novels.user_max_ids / last_sync_time`、`user_backup_rotation.last_sync_time` 只写不读；`sync_subscribed_series` 里恒真的 `if not series_list`；`_collect_following_users` 的返回注解写二元组、实际返回三元组；调度器 `_run_single_task` 的 `job is None` 分支生产不可达，日常占槽被记成 error 级日志。

保留，但应知道它们的状态：

- `storage/novels.py:list_series_novel_texts`：main 无调用，ai-writing 在用；为降低分支分叉保留。
- `dashboard_logs.html` 的「续跑下一个模型」面板：`RESUMABLE_TASK_TYPES` 是空集，在 main 上不可达，`window.streamSSE` 因此在 main 上没有可达的调用方；两者被 `test_ai_model_ui.py` / `test_mobile_reading_runtime.cjs` 锁定，且与 ai-writing 共享。
- AI 可达性：用 `sys.setprofile` 从 main 的真实入口（全部 AI 路由、`clean_keywords` 的成功/切换/坏 JSON/取消、启动对账、调度维护、目录同步）追踪，9992 行有效代码中 91.8% 所在的函数被执行到。约 350 行只有 ai-writing 在用（续跑链 `_stream_with_route_resume`、`_stream_replayed_route_job`、`create_or_get_ai_resume_job`、`_cancel_route_job`、`validate_resume_snapshot`、`ModelRouter.execute` 等），为降低分支分叉保留；约 80 行两个分支都不用：`ai/services/admin.py` 的 `list_jobs` / `get_job` 可删，`update_ai_job`、`delete_ai_job`、`prepare_model_routing_downgrade` 只有测试引用。`storage/ai/model_sync.py:cleanup_model_sync_operations` 注释写「仅测试/兼容」，实际调度器每小时调用，别删，改注释。
- 仅测试使用：`JobManager.mark_succeeded`、`PreferenceAnalyzer.analyze_local`、`record_asset`、`get_existing_novel_ids`、`list_recommendation_runs`、`delete_user`（路由已删）、`get_all_novel_ids`、`get_all_series_ids`。`recommendation_runs` 的 `search_plan_json/stats_json/finished_at` 只写不读，但表仍充当 run_id 序列。
- `JobSource.SYSTEMD`、`SCHEDULER_PRIORITY_HIGHEST` 无引用；Nginx 的 `location ~ /stream$` 在 main 上匹配不到任何路由（ai-writing 用）；`proxy_set_header Host ""` 与 `Connection "upgrade"` 是无害残留。
- vulture 标出的路由函数、`before_request/after_request/context_processor` 钩子、`get_expiration_time`（Flask SessionInterface 回调）都是框架注册，不是死代码。

## 5 部署与工程

- update.sh 服务起不来时只打印日志、仍以 0 退出，没有记录旧提交以便回退，`sleep 2` 后的 `is-active` 也测不到启动后几秒才崩的情况。建议：先记下 `git rev-parse HEAD`，重启后循环 curl `/api/health` 约 30 秒，失败时 `exit 1` 并打印回退命令。
- deploy.sh 无条件 `playwright install chromium`（约 150 MB，且没装系统依赖），pyproject 把 playwright 列为硬依赖，而它只用于可选的自动登录与 Web Cookie 自动刷新。可改为可选依赖（`.[autologin]`），调用点已经是延迟导入，需补 ImportError 的友好提示。
- systemd 单元在 deploy.sh（heredoc）与 update.sh（printf）各写一份，改一处容易漏另一处；可以像 Nginx 配置那样提交模板文件。
- 安全加固：没有 CSP 与 HSTS（CF 边缘是否已加待确认）；Vue 脚本没有 SRI；Tailwind 未锁版本（P1 一并解决）。AI 的 SSRF 防护只剩 6to4 `2002::/16` 这一个理论缺口。
- 分支移植：ai-writing 缺 main 的 crypto KDF 进程级缓存与非流式回退限读；移植前先修 A1。
- 仓库卫生：远端分支 `claude/inspiring-chatterjee-cb1ec0` 与 main 的 `49de193` patch 等价；`.claude/worktrees/` 下两个分离 HEAD 工作树都已并入 main 且无改动；未跟踪的 `docs/codex-for-oss-application.md` 含个人邮箱，不要提交进公开仓库。本轮已删除误提交的 `audit_routes.py` / `audit_routes_out.txt`。

## 6 文档

本轮整理（未提交）：

- 新增 [项目现状](../STATUS.md) 作为版本、测试基线、部署与待办的唯一入口；47 处指向旧整改状态的「当前状态」链接改指现状页（共改写 52 个文件的链接）。
- 15 份带日期的审计、部署、移动改造、整改与排查记录，以及 `API_COMPLETE.md`、根目录的 `KNOWLEDGE_GRAPH.md`、`.monkeycode/` 两份需求稿（共 19 个文件）移入 `docs/archive/`；重写 `docs/INDEX.md` 与 `archive/README.md`。94 个 Markdown 文件 0 个断链。
- CLAUDE.md 修正十余处漂移的行号与计数（改为函数名引用）、`sync-bookmarks` 的退出码、救援目录 503 的真实范围、`refresh_rescue_item` 不覆盖章节写入。
- 8 份活跃文档按代码修正 41 处：JOB_SYSTEM（删已移除的 `SYNC_CHECK`、补 partial 判定的 `truncated` / `rotation_pending`）、API 契约（删不存在的 `check-bookmarks`，补 `sync/cancel`、两个封面端点、`personal_hold`）、页面清单（救援脚本一节从 0.1.0 改写到 0.12.0）、救援指南、统一需求、模型路由速查、界面规范。
- `.env.example` 修正误导性注释（`PIXIV_COOKIE_SECURE` 留空并不会因反代头自动开启；用户名密码不能代替 refresh token），补 `PIXIV_NGINX_CACHE_DIR`；README 删除指向未开启的 GitHub Discussions 的死链；pyproject 修正作者与描述并补项目链接；`assets/logo-design.md` 注明它描述的是 ai-writing 的旧 Logo。

需要决定的文档问题：

- 统一需求 §9.3 的 MUST「正常页面不得请求救援 API」与 0.12.0 的有意行为冲突：列表页无失效卡时也发一次 Token 探测，新作页「好P友新作」tab 被改写为「拯救系列」，详情页在 Pixiv 返回 403 等状态不明时也会静默查备份。README 的「正常页面不会请求救援 API」同样措辞过宽。
- `delay_seconds_between_items/pages` 缺键时读取回落 0.0（不限速），保存却回落 1.0 并写回 YAML。
- 追更系列同步熔断后仍重建救援目录（状态检查熔断时则跳过），是否有意未确认。

## 7 已核实没问题的部分

记录在此，后续审查不必重查：

- 认证与会话：登录时 `session.clear()` 并换 256 位 id，服务端只存哈希，同浏览器旧记录同事务吊销；7 / 30 天绝对过期、请求不续期；登出删除服务端记录；改口令即全部失效；登录与救援 Token 均为常量时间比较；`safe_next_path` 防开放跳转；CSRF 在会话与本机两条分支都校验。
- 单活跃任务不变量、FinalizationClaim 仲裁、Runner 各路径都落终态；调度器按（优先级，最逾期）挑任务，让位后线程 join 完才释放信号量。
- 取消协议：调用可取消路径的宽 except 前都有 `except InterruptedError: raise`（例外是 S7 涉及的 `web/utils.py` 三处）；`sync_engine.py` 没有游离的 `time.sleep`。
- 重试名单：`src` 里 13 个 `api.xxx` 调用点中的 7 个数据方法全在 `RETRY_PATCHED_METHODS`。
- 状态检查：两个熔断计数相互独立，已知删除不清零 missing 连击；三个清单都按 `last_checked_at` 升序、NULL 在前。
- 存储：FTS 的读、写、删全部按 rowid（09 月 39 秒问题没有残留路径）；迁移幂等且按库路径缓存；连接按请求或任务独占；大 IN 列表按 900 分批；推荐三层去重正确，`archived_novel_ids` 只用于 `in`。
- XSS：阅读页 `v-html` 前逐行转义；油猴脚本没有 innerHTML / eval，恶意标题、作者、标签、简介与各类标记注入后危险节点为 0，jumpuri 只放行 http(s)。
- AI：重定向三层拒绝，IPv4 映射、NAT64、十进制/八进制、DNS 重绑定均被拦截；Key 与 prompt 不进日志；各项硬上限在存储层或解析时强制；模型目录同步的 CAS、租约与崩溃对账正确。

## 8 复核更正

子代理与旧报告中经复核不成立或需修正的说法，记录在此以免重查：

- 「`/api/auth/login`、`/api/auth/logout` 无调用方」（旧 `audit_routes_out.txt`）：login 是登录页本身，认证门用 `url_for("auth_login")` 跳转；logout 由页面登出按钮调用。
- 10-20 报告「`rebuild_rescue_catalog` 5000 本 577 ms」：测量条件没有留档，仓库里的目录性能测试夹具正文只有「正文」两个字；接近生产的合成库上约 2 s（见性能 P13）。
- 10-20 报告「`get_rescue_items_batch` 把行查询合并为批量 IN」：实际只合并了父系列评估与 hold 判定，行查询仍逐 ID（见 D2）。
- 10-20 报告「`_atomic_write_yaml` 随机临时名 + 写锁」：锁只包住写文件，读-改-写仍会丢更新（见 W2）。
- 10-14 整改状态记录过「两分支 compileall 与 pyflakes 均通过」，而 10-20 修复批只跑了 compileall，于是带进了 A1 的未定义名称。改代码后应同时跑 `python -m pyflakes src`（本机已装；未在 pyproject 声明）。
- 主循环一度认为 `window.streamSSE` 在 main 上有调用方：调用点在日志页续跑面板，而该面板因 `RESUMABLE_TASK_TYPES` 为空不可达，前端切片的「无可达调用方」成立。
