# 项目审计报告（2026-09-14）

## 0. 范围与验证方法

本轮对 HEAD `d7425ba` 做全项目只读审计：`src/pixiv_novel_sync/` 43k 行 Python、22k 行模板、38k 行测试、`docs/` 全部活跃文档与 4 份「进行中」计划、部署脚本、userscript。按 10 个子系统并行逐行阅读，每条发现都经过源码交叉核对，标注「已复现」的条目在临时数据库或临时脚本上实际跑出了结果。本报告只记录代码事实与待修复项，未修改任何仓库文件。

验证结果：

- `python -m compileall -q src tests` 通过。
- 全量 `pytest`：**1492 passed, 4 skipped**（357 s）。CLAUDE.md 写的 1308 已过时。
- `pyflakes src`：6 处未使用导入（`webapp.py:34,54,55` 三处为「供测试使用」的再导出，`ai/service.py:3` `create_provider` 真未使用），无未定义名。
- `vulture --min-confidence 80`：仅 `storage/connection.py:140` `__exit__` 的三个未用参数，无实质死代码命中（死代码需人工判定，见 §4）。
- 未发现纯 CPU 空转死循环。所有分页循环都有硬上限；仍有一类「无上限」是网络层的：AI Provider 的 SSE 流在上游只发注释行时会无限期挂住（§2.5）。

上轮审计（2026-08-13）的 4 条 P0 全部已修复，P1 大部分已修复；逐条核实见 §9。

工作树里有一个游离的 `.claude/worktrees/objective-napier-aeb3a6`（detached HEAD `fa7ee2e`，含已删除的 `dashboard_ai.html` 副本），与主干无关，建议 `git worktree remove` 掉，避免全仓 grep 时干扰。

---

## 1. 总览

| 严重度 | 数量 | 说明 |
|---|---|---|
| P0 | 3 | 功能性断裂：登录页 Token 落盘、向导导入弹窗、AI 创作中文预算 |
| P1 | 27 | 明显功能错误、任务系统锁死、数据一致性、无上限资源占用、成人润色恢复非同构 |
| P2 | 40+ | 健壮性、边界、并发、契约错配 |
| P3 | 60+ | 死代码、无用接口、微优化、文案 |

**最值得先看的 10 条**（每条只需几行到几十行改动，但影响面大）：

1. `token_login.html` 三处按已脱敏的 `refresh_token` 字段判成败，手动 OAuth 兑换成功却永不落盘（§2.1）。
2. 调度器「停止」与「提交」竞态会留下一个永远 `CANCEL_REQUESTED` 的幽灵 job，之后所有任务提交都被拒，直到进程重启（§2.2）。
3. `init_schema()` 在每个 Web 请求上执行，7k 篇库实测 1.1 s，且每次抢写锁、跑两次 `PRAGMA foreign_key_check`（§2.3）。
4. `cleanup_stale_pending` 零命中时不提交，写锁被挂到整个待删除检测任务结束，期间所有页面 30 s 后 500（§2.3）。
5. AI 创作 `generation.py` 按字符裁剪、Router 按 UTF-8 字节校验，中文输入超过约 3900 字即全部候选 `context_overflow`（§2.4）。
6. 创作向导「导入项目」弹窗传的是 `:open`，组件只认 `is-open`，弹窗永远不渲染（§2.4）。
7. Provider `max_retries=0` / `temperature=0` 被 `or 默认值` 吞掉，上轮修的「尊重 max_retries=0」在真实配置下不可达（§2.5）。
8. `http://localhost` 在 `PIXIV_AI_ALLOW_PRIVATE_HOSTS=1` 下仍被拒（`::1` 命中 `is_reserved`），保存通过、健康横幅绿、运行时全失败（§2.5）。
9. `following_novels` 被让位或取消时不落水位线，`preemptible=True` 的前提不成立，每次让位浪费一整轮槽位（§2.2）。
10. 本机无 `DASHBOARD_TOKEN` 模式完全没有 CSRF 与 Host 校验，任意网页可用表单 POST 触发「确认移除」删库删文件（§2.6）。

---

## 2. 必须优先修复（P0 / P1）

### 2.1 鉴权与登录

**[P0] Token 登录页三条 OAuth 路径都读已脱敏的 `refresh_token`**
`templates/token_login.html:182,221,251` ↔ `web/utils.py:39-48`、`webapp.py:1094-1119`。后端 `_oauth_task_public_payload` 只返回 `has_refresh_token`（`tests/test_webapp_security.py:165,202` 断言脱敏），前端却 `if (resp.ok && data.refresh_token)` 才调 `/api/save-token`。结果：粘贴回调 URL 兑换成功 → 页面显示「失败：无效链接」，Token 永不落盘；自动登录 worker 已 `save_to_env` 却显示「登录成功但未提取到 Token」。已存在的 `POST /oauth/save/<task_id>` 只在永远不会触发的 `?oauth_task=` 回调路径被调用。
修法：三处改判 `data.has_refresh_token`；兑换成功后调 `POST /oauth/save/<currentOAuthTaskId>`；补一个 grep 模板的回归测试。

**[P1] 无 `DASHBOARD_TOKEN` 的本机模式无 CSRF、无 Host 校验**
`webapp.py:776-794`：token 为空时只判 `_is_local_request()` 放行，CSRF 只在 `session.authenticated` 分支检查。`POST /api/dashboard/pending-deletions/<id>/confirm`（删库 + 删文件）、`/api/cache/clear`、`/api/dashboard/sync/<task>` 都可被恶意页面用顶层表单 POST 命中；DNS rebinding 后可读私密收藏。生产在 nginx 后且 XFF 头会被拒，所以只影响本机自用场景。
修法：CSRF 检查提到 `if not token` 分支之外（`base.html` 的 `csrfFetch` 本来就无条件带头，不会破坏前端）；加 Host 白名单。

**[P1] `deploy.sh` 在目录存在但无 `.git` 时 `rm -rf` 整个安装目录**
`deploy.sh:38-51`：else 分支直接 `rm -rf "$INSTALL_DIR"` 后重新 clone，`data/` 数据库与文库一起删。用户在第 21-29 行已答「是」更新，若 `.git` 缺失或损坏即静默丢数据。
修法：clone 到临时目录再 rsync 代码文件，排除 `data/ .env config/config.yaml`；或 `rm -rf` 前检测 `data/` 存在即中止。

### 2.2 同步引擎与任务管线

**[P1] 调度器 stop 与 submit 竞态让共享 JobManager 永久拒绝新任务**
`web/managers.py:802-818`：`stop_event` 在提交后置位时，只 `cancel_task(job.job_id)` 然后 `return True`，从不调 `run_task`。`request_cancel` 只把 QUEUED 改成 `CANCEL_REQUESTED`（`jobs/manager.py:96-107`），只有 `JobRunner.run` 会转成 `CANCELLED`。`_has_active_shared_jobs`（`webapp.py:516-519`）把 `CANCEL_REQUESTED` 算活跃 → 之后所有 `_submit_shared_job` 抛「已有同步任务正在运行」，`task_logs` 那行永远 `running`。已复现。`tests/test_webapp_jobs.py:112` 目前把这个行为钉死为预期。
修法：该分支仍调用 `run_task`（runner 会立即 `mark_cancelled`），或显式 `mark_cancelled` + 回写日志；同步改测试。

**[P1] `_submit_shared_job` 里 `create_task_log` 抛异常留下同样的幽灵 QUEUED job**
`webapp.py:578-595`：`submit()` 成功后 `create_task_log` 因 busy/磁盘满抛异常，job 留在 `_jobs` 永不运行。且这段 DB IO 在 `shared_job_manager._lock` 内执行，worker 的每次 `add_log` 都被阻塞。
修法：先建日志再 submit，或 except 里 `mark_failed`。

**[P1] `following_novels` 被取消 / 让位时不保存 `user_last_synced` 水位**
`sync_engine.py:956-972,1164-1175,1260`：`_save_watermark()` 只在三个正常路径调用，作者循环无 `try/finally`。`InterruptedError` 穿出后已扫作者全部丢失，下轮重新挑中同一批。`docs/JOB_SYSTEM.md:159` 「已完成的部分已入库，下轮从水位继续」对轮转不成立。
修法：作者循环包 `try/finally: _save_watermark()`。

**[P1] 「预检查」（sync_check）结果从未被正式同步消费**
`jobs/quick_sync.py:213` 以 `sync_check_scope=job_id` 写入，`quick_sync.py:72` / `jobs/tasks.py:175` 正式同步用默认 scope `"_"` → `get_sync_check_list("_")` 恒空。唯一能桥接的 `latest_matching_sync_check_scope` 在 `SyncJobManager`（`web/managers.py:941`）上，该类在 `src/` 零实例。连带 `sync_engine.py:1053-1060` 的「连续 30 本已存在就停」永远走不到，每本已归档小说每轮仍打两次 API。
修法：要么透传 scope，要么删掉整套 sync_check + `SyncJobManager` + `check_bookmarks_existence`；`existing_streak` 改查 `novel_archive_complete`。

**[P1] `user_backup` 被 `max_pages_per_run=2` 截成 60 本且不标 `truncated`**
`jobs/services.py:161-174`：`max_pages = settings.sync.max_pages_per_run or 200`，触顶 `break` 不写任何标记；`run_scheduled_user_backup` 的 offset 照常前进，该作者被当作已全量备份。
修法：独立上限 + `truncated/incomplete`。

### 2.3 存储层

**[P1] `cleanup_stale_pending` 零命中时不提交，写锁挂到任务结束**
`storage/pending_and_watermarks.py:172-192`：`if total_count: self._commit_if_needed()`，0 行时隐式 BEGIN 的写锁一直持有到 `jobs/services.py:411` 才提交，中间是逐个 `novel_detail` + 限速 sleep。已复现：其它连接写入 `database is locked`，本连接再进 `transaction()` 抛 `cannot start a transaction within a transaction`。
修法：无条件 `_commit_if_needed()`，或整段 `with self.transaction()`。根治见 §8.1。

**[P1] `init_schema()` 每个 Web 请求都跑一遍**
`webapp.py:70-82 _open_database`、`rescue_web.py:151`、`preference_web.py:28`、`web/managers.py:445/656/735` 每次新建 `Database` 后立即 `init_schema()`。7k 篇 / 61 MB 库实测 1.04–1.43 s；其中 `_migrate_ai_tables` 与 `_migrate_adult_polish_tables` 各拿一次 `BEGIN IMMEDIATE`，`_migrate_core_foreign_keys` 与 adult 迁移各跑一次全库 `PRAGMA foreign_key_check`，`_migrate_rescue_tables` 每次 `INSERT OR IGNORE ... SELECT`。生产库 1 GB+ 时每个页面付 ≥1 s，且与同步任务争写锁。救援公开 API 每请求开两次 DB（鉴权一次、视图一次）= 两次 `init_schema`；另一线程持写锁 3 s 时实测 `init_schema` 等 3.06 s。
修法：`init_schema` 只在进程启动跑一次（`ai/services/core.py:128-134` 已用 `_initialized_paths` 做到）；`foreign_key_check` 与回填只在真正建表/重建时做。

### 2.4 AI 创作

**[P0] 输入按「字符」裁剪、路由按「UTF-8 字节」校验**
`ai/services/generation.py:155-158,174,241,578,650-653,709-723` 用 `get_tail_context(context, input_budget)` 取字符；`ai/model_router.py:951-965 _candidate_fits` 用 `encode("utf-8")` 字节；基类 `estimate_message_tokens` 恒返回 `None`（`providers.py:442-447`），永远走字节估算。默认 Agent 16k 窗口 → `input_budget≈11.7k`，中文 1 字 = 3 字节，超过约 3900 字即所有候选 `context_overflow`，job `failed: route_exhausted`。已复现：`stream_continue / rewrite / audit / plan` 对 22000 字中文全部失败；`_smart_context` 分段 8000 字 = 24k 字节 > 16k 窗口，内部摘要同样 overflow 后静默退回 tail。受影响入口：章节页「生成构思」、Pipeline 的 `deai` 与 `audit` 步骤、向导蒸馏。`projects.py:42-90` 的 `_fit_tail_text_messages / _fit_route_messages` 是字节口径，所以只有 `generation.py` 这一族错。
修法：把两个字节口径 fitter 挪到 `core.py`，`generation.py` 五个流一律改用；`_smart_context` 段长与蒸馏批大小改按字节。顺手让 `PromptBudget` 用 `chunking.estimate_token_count`（中文 1.5 字/token）替代字节，目前按字节把中文高估约 4.5 倍，只用了窗口 15%。

**[P0] 向导「导入项目 / 导入素材」弹窗永远不渲染**
`templates/dashboard_wizard.html:146,162` 传 `:open` 与 `size`，`vue_components.html:183` `app-modal` 只声明 `isOpen`，`v-if="isOpen"` 恒假。自拆页提交 `00778f3` 起即如此；`tests/test_ai_page_routes.py` 只校验 `@event` 导出不校验 prop。这是向导产物落地的唯一入口。
修法：改 `:is-open`，删 `size`。

**[P1] 创作向导无历史裁剪，累计超预算后每条都失败，且用户消息先落库导致重试重复**
`ai/services/chat_wizard.py:107-146`：先 `append_ai_chat_message(user)`，只按条数截断（默认 40），整段历史无字节 fit。已复现：6 轮后 `Prompt 内容超过可用输入预算`，重试再插一条 user。会话从此报废。
修法：按字节从最旧轮丢历史；用户消息在 `_start_route_job` 成功后再写。

**[P1] 笔记页「AI 自动回收」按钮永远失败**
`dashboard_ai_notes.html:260-270` 不带 `chapter_id`，后端 `projects.py:1627-1632` 要求它 → `章节内容为空`。

**[P1] Pipeline 弹窗「提取摘要」「润色」快捷按钮永远失败且无提示**
`dashboard_ai_chapters.html:881-911`：`agent_id: 0` 硬编码，`onEvent` 不处理 `error`。后端 → `Agent 不存在`。

**[P1] Pipeline 结束后 `openChapter()` 重置工作区，「自动重试」永不执行、进度与输出被清空**
`dashboard_ai_chapters.html:866 → 622-623 resetChapterWorkspace() → 589-599`，然后 `:869-873` 从已清空的 `pipelineSteps` 找 `failed` 恒为空。弹窗文案「所有产出已独立保存」实际是不离开页面也不保留。
修法：拆 `resetForChapterSwitch()` 与 `reloadCurrentChapter()`。

**[P1] Pipeline 客户端断连后章节 `metadata.pipeline.status` 永久 `running`**
`projects.py:1828-2226` 顶层无 `except GeneratorExit`，终态只在正常走完后写。已复现。前端 `dashboard_ai_pipeline_modal.html` 也没有取消按钮。
修法：顶层 `try/finally`，未写终态则写 `cancelled`。

### 2.5 AI 路由、Provider、目录

**[P1] `max_retries=0` / `timeout_seconds=0` / `temperature=0` 被 `or 默认值` 吞掉**
`storage/ai/core.py:220-223,370-372`、`ai/services/admin.py:1487-1490,1506-1509`：`int(row.get("max_retries") or 2)`。已复现：创建时 0 → 2，加载时 0 → 2；`temperature=0` → 0.8。上轮修的 Provider 层 `max(0, config.max_retries)` 永远收到 ≥2；测试都直接构造 `AIProviderConfig(max_retries=0)` 没走 DB 往返。
修法：`default if x is None else x`；补 DB 往返回归测试。

**[P1] `http://localhost` / `http://[::1]` 在 opt-in 下仍被运行时拒绝**
`ai/providers.py:105` 先判 `is_reserved`（Python 把 `::/8` 整段标 reserved，`::1` 命中）再判 `allow_private`；`_resolve_target :177-181` 对 getaddrinfo 每个地址都拒，`localhost` 解析出 `::1` 即整体失败。保存（`resolve=False`）和健康 lint 都通过 → 假绿。错误提示还让用户去设本来已经设了的环境变量。
修法：先判 `is_loopback`（opt-in 放行）再判 `is_reserved`；补 `resolve=True` 的测试。

**[P1] Anthropic Provider `base_url` 以 `/v1` 结尾时目录同步正常、生成打到 `/v1/v1/messages`**
`providers.py:1060-1065`（发现请求判 `endswith("/v1")`）vs `:1094-1095`（生成无条件拼 `/v1/messages`）。设置页 `applyV1Suggestion`（`dashboard_settings_models.html:813-817`）在探测 404 时主动建议追加 `/v1`，追加后探测通过、落库后所有生成 404。
修法：提炼 `_resolve_base_url()` 两处共用。

### 2.6 偏好与推荐

**[P1] 单个候选的系列字数拉取出网络错误，整轮推荐失败并丢弃全部已暂存候选**
`recommendations.py:99-119,344-362`：try 只包 `_search_novels`，`_candidate_to_item` 在 try 外，`_series_length` 只捕 `TypeError`。已复现：`novel_series` 抛 `PixivError` → run `failed`、items 空。
修法：候选级 try/except；`search_novel / novel_series` 加 `retry_on_pixiv_error`。

**[P1] 「不感兴趣」「屏蔽作者」之后条目仍留在默认列表，界面零变化**
`preference_web.py:186-201`、`storage/recommendations.py:411-461` 不传 `status` 时不过滤；模板无状态徽标；`muteAuthor` 只置当前一条。需求 13.4 未达成，用户以为点击无效。

**[P1] `preference_analyze` 无条件覆盖默认画像**
`jobs/tasks.py:373-387`：`name / refined_keywords / excluded_keywords / avoid_themes` 全部被 `rebuilt` 覆盖。已复现：AI 清洗抖动一次或 0 篇新小说时，上一轮精炼词与手工排除词全部丢失。代码 docstring 自己邀请用户手工编辑，`PUT /profiles/<id>` 也在契约里。判定为缺陷。
修法：existing 存在时合并而非覆盖；`processed_this_run == 0` 时跳过 AI 调用并沿用旧值。

**[P1] 全部查询都失败时 run 与任务日志仍绿色**
`recommendations.py:103-105,132-135`：异常只 `errors += 1`，循环后无条件 `succeeded`；`_task_log_status_for_stats` 只看顶层，嵌套 `stats.errors` 永不检查。

### 2.7 前端（非 AI 页）

**[P1] 首页运行状态条把 job_id 钉死在页面加载时那一个**
`dashboard.html:255-265`：mount 时取一次 `latest_job`，此后每 3 s 带旧 job_id 轮询。出现「当前无运行任务」与「停止当前」按钮同时显示。`auto-sync/status.current_job` 已有却未用。

**[P1] 任务日志「类型」筛选里 `bookmarks` / `following_list` 两项永远查不到**
`dashboard_logs.html:369-370` vs `web/managers.py:73-76` 别名表：落库只会是 `bookmark` / `following_users`。

**[P1] 任务日志页时间少显示一个时区差（Safari `Invalid Date`）**
`dashboard_logs.html:642-645` `new Date('YYYY-MM-DD HH:MM:SS')` 无时区；其它页（`dashboard.html:316`、`dashboard_novels.html:578`）都显式补 `Z`。

**[P1] 收藏卡片的作者头像与 R-18 徽标永远不显示**
`dashboard_novels.html:161,192-195` 读 `x_restrict` / `author_avatar_url`，`storage/bookmarks.py:54-72` 的 SELECT 没有这两个字段（`list_following_series` 有）。

### 2.8 救援

**[P1]（同 §2.3）** `init_schema` 每请求执行对救援公开 API 影响最大：鉴权 + 视图两次开库，目录全量重建（实测 20k 篇 2.3 s，且每个 sync 任务收尾都跑）期间所有请求排队。

### 2.9 成人润色

该子系统的 fail-closed 骨架（HMAC 时序比较、owner 作用域、raw text 双向拦截、边界 token、候选严格解析、provider-scope hash、全部 revision CAS、ReDoS、循环上限、Provider 层取消）逐项核查未发现漏洞。下列问题全部是「该通过的没通过」或「恢复非同构」，修复都不放宽任何校验。`tests/test_ai_adult_*.py` 64 passed，说明这些缺陷均未被现有测试覆盖。

**[P1] 别名子串掩码 + 恢复时一律写回正名，应用到章节的正文被静默篡改，本地校验全部放行**
`ai/adult_prompt.py:181-189,279-287`（掩码对 before/target/after 全量替换）、`:488-489`（`pattern.sub(lambda m: token_map[m.group(0)].canonical_name, ...)`）。设置页允许 1 个码点的别名（`services/adult.py:247` 无最小长度，测试套件自己就用 `name[:1]`）。已复现：角色「林舟」别名「林」，原文「森林里，林舟看见一片树林」→ 恢复后「森林舟里，林舟看见一片树林舟」，`run_local_adult_checks` 阻断集为空，可直接应用。即使别名不短，合法使用的别名也会被改写（「小安握住他的手」→「安娜握住他的手」），因为 `_identity_sets`（`adult_validation.py:394-397`）按「同一 owner 任一名字出现即可」判定。
修法：掩码 token 携带表面形式（每个 (character, variant) 一个 token），恢复时写回原字串；`_normalize_character` 拒绝长度 <2 的别名。

**[P1] 占位符后紧跟空白 / 换行被误判为「占位符变体」，整个任务 `safety_blocked`**
`ai/adult_prompt.py:448-454`：`"".join(re.escape(char) + "[\\s…]*" for char in token)` 在末字符后也追加了 `[\s]*`，贪婪吞掉 token 之后的换行，`group(0) != token` 恒成立。目标片段里角色名位于行尾 / 段尾或后跟空格（西文名）时，模型原样回显即失败，记为「成人润色角色身份映射无效」。已在真实管线复现。
修法：`"[\\s…]*".join(re.escape(c) for c in token)`。

**[P1] 参与者校验前后矛盾：上下文角色「必须列入参与者」，列入后本地校验又要求「必须出现在目标片段」，必然失败且在主生成付费之后**
预检 `services/adult.py:2089-2103,2181-2185` 扫描 `before + target + after`；本地校验 `adult_validation.py:366-367` 的 `original_ids` 只来自 `target`（`services/adult.py:1169-1170`）。已复现：只列目标角色 → 预检拒绝「上下文中的已确认角色必须列入参与者」；两者都列 → 生成后 `participant_mapping_unknown`（在 `safety_critical` 集合内）。testkit 默认 payload 同样触发，端到端测试全部改成了 `[CHARACTER_A_ID]` 绕开。
修法：两条规则取同一命名范围（预检只扫 `target`），并把「参与者必须在目标片段被点名」提前到 `prepare_adult_job`。

**[P1] 审查 Agent 硬编码 `context_window=16_000 / max_tokens=2_000`，按 UTF-8 字节估算，目标片段超过约 2000 中文字即「审查不可用」；主 Agent 默认 `max_tokens=12_000` 让 input_budget 只剩 3722 字节**
`services/adult.py:387-388,2851-2852`；估算器同 §2.4。契约允许 12000 码点。审查预算只在主生成完成后才算（`prepare_adult_job` 只算主阶段）；`ModelRouteError` 不是 `AIServiceError`，被泛化成「成人润色前置校验失败」。设置页只提交 name / binding / provider / model / pool，用户无法改这两个值。实测：目标 3000 字审查必失败；章节 1500 字主生成必失败。
修法：`prepare_adult_job` 同时预估两个审查阶段预算并在开 SSE 前拒绝；审查窗口取 `min(候选模型窗口, 常量)`；设置页暴露两个字段；`ModelRouteError` 映射为带原文的 400。

**[P1] 阅读页「重新生成」按钮必然 403**
`templates/dashboard_ai_reader.html:423-436`：请求 headers 只有 `Content-Type`，不带 `X-Adult-Access-Token`，且 `resetAdultCandidate()` 在发请求前先清空 token；服务端 `ai_web.py:1429-1434` 先验 access token。`tests/test_ai_adult_frontend.py:45` 只 grep HTML。

---

## 3. P2（健壮性、边界、并发、契约）

### 同步 / 任务

- 9 处 `except Exception` 会吞掉退避里抛出的 `InterruptedError`：`sync_engine.py:645-649, 1020-1024, 846-851, 1234-1238, 406-410, 441-445, 464-468, 1943-1951, 2038-2046`。取消被降级为「翻页失败」继续跑。CLAUDE.md 说的「companion test」只覆盖系列章节循环那一处。
- 收藏 / 关注作者翻页 API 三次重试失败后静默 `break`，stats 全 0，任务记绿色成功（`sync_engine.py:645,1020,1234`）。
- 订阅系列熔断把任何异常（SQLite busy、脏数据）都计入 `consecutive_fetch_failures`，连续 5 次即 `rate_limited`（`sync_engine.py:1473-1708` 一个大 try）。这正是「所有 aborted_reason 迄今全是误报」的模式。
- `meta_hash` 含 `total_bookmarks / total_view` 等易变字段，内容哈希增量几乎从不命中，每轮重写 `meta.json`、`novels`、`novel_texts`、FTS（`sync_engine.py:2223-2316`）。
- `run_scheduled_user_backup` 单用户失败率超阈值抛 `RuntimeError` → 整批失败且 offset 不前进，坏用户每轮阻塞轮转（`quick_sync.py:135-162`、`services.py:194-199`；`processed` 在检查后才自增，前 5 本失败 2 本即中止）。
- 手动触发的任务没有任何取消入口：`request_cancel` 只作为调度器回调，`/auto-sync/stop-task` 只认 `_current_task_job_id`（`webapp.py:1723`、`managers.py:376-384`）。
- 不限量模式（`users_limit=0`，代码默认）命中 `max_items_per_run` 直接 return 不标 `truncated`（`sync_engine.py:1247-1250`）。
- `_fetch_remote_bookmark_ids` 硬上限 200 页：收藏 > 6000 的账号 `pending_deletion_detection` 永远 `RuntimeError` 且无提示（待确认）。

### Web / 设置 / 部署

- `PIXIV_VERIFY_SSL=`（空值）→ `_parse_bool('')` 返回 False，关闭 TLS 校验（`settings.py:160,270-274`）。`PIXIV_TIMEOUT=` 空 → `int("")` 异常 → 固定 30 忽略 YAML；`PIXIV_PROXY` 用 `or` 无法用空值清掉 YAML 代理。
- `/api/save-token` 的 `refresh_token` 无字符白名单，可向 `.env` 注入换行（`webapp.py:1022`、`oauth_helper.py:132-149`）；`user_id` 非数字 500。
- `hmac.compare_digest` 遇非 ASCII 抛 `TypeError` → 500：`DASHBOARD_TOKEN` 含中文时无法登录，登录页输入中文 500 且不计入限流（`webapp.py:872,793`）。
- 时区从不校验：保存与 cron-preview 都静默回落 UTC，预览还回显 `valid: true` + 原时区名（`managers.py:1096`、`webapp.py:1475-1479`）。
- `SESSION_COOKIE_SECURE` 推断在 `.env` 加载之前执行（`webapp.py:486-500`），`flask --app` / 测试直接 `create_app()` 时读不到 `DASHBOARD_TRUST_PROXY`。
- 确认删除把 `trash.stage()` 文件搬移放在 `BEGIN IMMEDIATE` 内（`webapp.py:1973-2001`），大系列慢盘超 30 s 让同步线程 `database is locked`；`_remove_archive_files_atomic` 是正确顺序。
- `/proxy/image` 把上游 3xx 当 200 返回，nginx `proxy_cache_valid 200 365d` 把重定向页缓存一年（`webapp.py:911-913`）。
- `update.sh` 每次把含密钥的 `.env` 复制到 `/tmp/pixiv-novel-sync-backup.<ts>/` 且永不清理。
- systemd unit `PATH` 只含 venv，`Xvfb` 永远找不到，Web Cookie 自动刷新退化为 headless（`deploy.sh:127`、`playwright_login.py:329-352`）；`deploy.sh` 从未 `playwright install`。
- `deploy.sh` 依赖 `setfacl` 但未装 `acl`（待确认）；nginx 配置硬编码 `pixiv.dongboapp.com` 与证书路径，`nginx -t && restart` 在 `set -e` 下失败不中止。
- `scripts/install_server.sh` 与它安装的 unit 不自洽：不把代码放进 `APP_DIR`，`pixivsync` 用户对 `data/` 无写权限；`test_deployment_contract.py` 只比字符串测不出。

### 存储

- `_rebuild_table_with_foreign_key` 非事务（`schema.py:263-273`）：老库迁移中途崩溃留下空 `novel_texts` + 满数据 `novel_texts_old`，下次启动静默跳过。已复现。
- `delete_user` 不删该作者的 `series` 行，「我的追更」显示幽灵系列（`users.py:356-417`）。已复现。
- 作者改名 / 作品改标题后旧归档目录成为孤儿，封面与删除路径按新目录算错（`storage_files.py:41-44,127-147`）。
- 多个分页接口排序键无唯一 tiebreaker（`novels.py:441-447` `updated_desc` 把默认的 `novel_id DESC` 去掉了；`users.py:232,302,344`；`series.py:594`；`tasks.py:205,393`；`pending_and_watermarks.py:507`）。
- `GET /api/dashboard/logs` `page_size=0` → 500，`page_size=-1` → 全表倾倒（`webapp.py:1739`、`storage/tasks.py:88`）。
- `_lock` 与 `transaction()` 锁序不一致，一个实例被多线程共用时 30 s 假死（`connection.py:164-188` vs 40 处 `with self._lock`）。目前每请求新建实例所以是潜在问题。
- `upsert_subscribed_series` 的 `cover_url` 用 `COALESCE` 只挡 NULL 不挡 `''`，watchlist 路径会把封面覆盖成空串（`series.py:44`、`sync_engine.py:1396`）。

### AI 路由 / Provider

- `validate_pool_graph` 每次池写入都校验全图，Provider 停用 / 模型停用不校验：一个池变坏后所有池的写操作全被拒，且报错不指明是哪个池（`storage/ai/pools.py:195,242,307`）。已复现。
- `_import_available_models` 在每次 `init_schema()` 都重跑，删掉的人工模型会「复活」，`copyFromProvider` 还把老列表带进新 Provider（`model_schema.py:484-527`、`dashboard_settings_models.html:788-791`）。已复现。
- 任何成功同步过目录的 Provider 都无法删除（`storage/ai/core.py:286-297` 前置拒绝让 schema 的 `ON DELETE CASCADE` 永远用不上）。
- `AIProvider._request` 在持有 `_adapter_lock` 期间发起网络请求，同一 Provider 实例上的并发请求串行化到响应头到达；非流式模式即整段生成时间（`providers.py:500-524`）。
- 普通 AI job 没有服务端取消入口；`RouteRequest.is_cancelled` 在 `_stream_route` 恒为 None；30 分钟 deadline 只在候选切换与网络请求前检查。上游只发 `: keepalive` 注释行时请求线程无限期挂住，job 永远 `running`（`core.py:595-609`、`model_router.py:886-889`、`providers.py:860-864`）。
- 429 / 5xx / 网络错误一律 `scope="provider"` → 一次按模型限流就短路同 Provider 所有其它候选（`providers.py:1451-1539`、`model_router.py:1350`）。待确认是否刻意。
- 健康投影三处假绿：不试解密 API Key；固定 Agent 的 `required_capabilities` / 缺模型不检查；池 Agent 的能力过滤不检查（`admin.py:487-489,660-693`）。
- 生成路径的密钥脱敏只有正则（`sk-` / `bearer` 等），xAI 的 `xai-…` 与自建网关 key 不命中；模型同步路径有字面值替换而生成路径没有（`providers.py:279-298,1509`）。

### AI 创作

- `stream_response` 把生成器首段的 `AIServiceError`（未选步骤、章节为空）统一吞成「AI 响应中断」（`ai_web.py:687-691`）。
- 章节保存无乐观锁；续写自动保存用启动时快照拼接，会用旧基线覆盖并发编辑（`storage/ai/writing.py:171-199`、`projects.py:1055,1127`）。`chapter_revision` 列已存在只被成人流用。
- 「追加到正文」在自动保存后仍可点，重复追加（`chapters.html:117,718-723`）。
- 项目页「生成全书规划」「扩写详细梗概」不先写回 `style_control`，读到上次保存的风格（`dashboard_ai_project.html:639-663`）；CLAUDE.md 只写了章节页这条耦合。
- TF-IDF 检索缓存在 `index_chapter` 后不失效（`retrieval.py:78-109`）。已复现。
- `_extract_json_object` 括号配平不跳过字符串，`evidence` 里出现 `}` 整体解析失败（`projects.py:432-460`）。已复现。
- 「保存草稿」是只写功能，GET/PUT/DELETE/history/fork 五个路由零调用。
- Agent 表单没有 `context_window` / `top_p` 输入，用户无法在 UI 里缓解 overflow；`task_type` 下拉缺 `polish_dialogue / polish_psychology / chat`。

### 偏好推荐

- 「排除已归档」只看 novel_id，已归档系列的新章节会让系列再次被推荐（`recommendations.py:190,207-214`）。
- 同作者标签重合去重把同作者不同作品几乎全压掉（≥3 共同标签且 ≥70% 即判重，Pixiv 作者惯用固定标签组）；`preferred_authors` 从未参与评分；`get_recent_recommendation_items` 每个候选查一次 DB（600 次 / 轮）。
- `precise_queries` 六条全部复用同一个关键词；`broad_queries` 与 `primary_tags` 完全重复被去重吃掉，`keyword` 型查询一条都没有（`preferences.py:209-216`）。已复现。
- 推书完成提示永远「保存 0 条」（读不存在的 `result.result.stats.saved`，实际在 `stats.stats.saved`）；失败只显示 "failed"。
- 前端 5 分钟硬超时后误报「任务超时」而任务仍在跑。
- `POST /recommendations/run` 原样接受客户端 `search_plan`，20 条上限只对服务端计划生效；非 dict 元素直接 AttributeError。
- 字数加分对通过过滤的候选恒定（系列必 +10、单篇必 +5），`total_views` 存了不用。
- 定时分析每次都调 AI 清洗，即使 0 篇新小说。

### 救援

- 增量刷新与全量重建对同一数据给出不同目录：`refresh_rescue_item` 会丢掉 `series_id` 指向不存在系列的章节（`rescue.py:761-777` vs `:252-338`）。已复现。
- 无效 Token 请求在限速之前就触发两次 DB 打开 + `init_schema`，限速器保护不了数据库（`rescue_web.py:158-175`）。
- 目录重建触发点太多：bookmark 同步几乎不改变目录内容却每次重建。

### 成人润色

- 主生成期间客户端断连无法被察觉：delta 不向上 yield（`services/adult.py:2538-2539`），整个 token 流期间 Web 层没有 socket 写入，`GeneratorExit` 要等生成结束才触发；审查阶段 progress 仍是批量补发（`:1224-1306`，用同步 `execute()`）。上轮 P1 只修了一半。
- `_diff_summary` 用字符级 `SequenceMatcher(autojunk=False)`，O(n·m) 不可取消：实测 12000×36000 随机中文 12.4 s，重复文本 121.9 s，在 SSE 线程同步执行且 apply 路径再算一次（`adult_validation.py:208-227`）。
- 非可见阻断项（`locked_term_missing`、`number_changed`、`age_changed` 等）不在短路集，要等两次付费审查后才在 finalize 抛错，并以「候选保存失败」报出（`services/adult.py:1194-1222,1029-1033`）。
- 取消发生在审查 / 收口阶段时，SSE 终态码（`review_unavailable` / `validation_failed`）与 DB 状态 `cancelled` 不一致（`:810-817,1243-1260,1337-1353`）。
- 成人主 / 子任务不写 `route_deadline_at`（`storage/ai/adult.py:666-674,750-762`），`fail_stale_ai_jobs` 只在启动时执行，卡住的 job 重启前不回收。CLAUDE.md 的「30-min deadline」对成人不成立。
- 设置页给年龄默认值 18，编辑年龄为 null 的角色保存即变 18（`dashboard_settings_adult.html:190,291`），把服务端「年龄必须明确」的断言变成免填项。
- 阅读页无取消入口（`/cancel` 无前端调用方）；断连恢复后若任务仍在跑，UI 永久「生成中」，只能刷新页面丢 token。
- `ai_adult_policy_state` 只有 `INSERT OR IGNORE` 种子，无升级路径：策略 `version` 升级后旧库成人生成永久不可用（待确认是否有意）。

### 前端

- 小说 / 系列详情页各自重写 CSRF 与 fetch 只读 `data.error`（`novel_detail.html:439-490`、`series_detail.html:203-254`）；`tests/test_frontend_library_os.py:395-407` 断言必须含 `ensureCsrfToken` 字面量，**测试在锁定这个反模式**。
- 日志页手写 SSE 里再复制 `ensureCsrfToken`（`dashboard_logs.html:540-603`）。
- Provider 探测结果复选框是死 UI：勾选后保存不会写入（`dashboard_settings_models.html:807-843`）。
- 待确认删除页：检测任务 `cancelled / queued` 时轮询永不停止，`setInterval` 无 unmount 清理；列表加载失败渲染成空态。
- 全站时间戳处理不一致：`user_detail.html:79`、`series_detail.html:58`、`pending_deletions.html:87`、`follows.html:104`、`settings_models.html:132` 原样输出 UTC。
- `app-modal` 无 `role="dialog"` / `aria-modal` / Esc / 焦点管理；16 个 `<img>` 无 `alt`；AI 设置三页 14 个输入只有 placeholder。
- 登录页显示空壳侧栏（`token_login.html:7-10` + 未注册的 `<app-sidebar-nav>`）。
- Tailwind 与 Vue 全靠 CDN 且无 SRI，国内服务器 CDN 抖动即白屏。
- 同步设置页「保存后重启生效」文案不准确：调度器每轮重读配置，只是已排好的 `next_run` 要等该任务下次触发后才按新 cron 走。

---

## 4. P3、死代码与无用接口

### 4.1 无生产调用方的路由（全仓 grep 模板 / userscript / CLI 核实）

| 路由 | 位置 | 状态 |
|---|---|---|
| `POST /api/auth/logout` | `webapp.py:885` | 仅测试；UI 无退出登录按钮 |
| `POST /oauth/start`、`GET /oauth/callback`、`POST /oauth/sync-callback/<id>` | `webapp.py:1030,1052,1076` | Pixiv 固定 redirect_uri 永不回调；`sync-callback` 让客户端覆写 `task.state` 使后续校验形同虚设 |
| `GET /api/dashboard/follows` | `webapp.py:1149` | 契约写「Used by: follows page」，页面实际用 `/api/dashboard/users` |
| `GET/POST/DELETE /api/dashboard/novels/<id>/progress` | `webapp.py:1199-1234` | 阅读进度只存 localStorage，三端点无人调用 |
| `POST /api/dashboard/novels/export-epub` | `webapp.py:1236` | 无 UI 按钮；README 宣称的 EPUB 导出是半成品 |
| `POST /api/dashboard/settings`（全量） | `webapp.py:1403` | 页面全部走 PUT 分区端点 |
| `POST /api/dashboard/sync/start`、`/check-bookmarks` | `webapp.py:1496,1507` | 仅测试 / 无 |
| `DELETE /api/dashboard/novels/<id>`、`/users/<id>`、`/bookmarks/<id>` | `webapp.py:1854-1916` | 无模板调用 |
| `GET /api/dashboard/pending-deletions/count` | `webapp.py:1947` | 被 `shell-data` 取代 |
| `POST /api/dashboard/settings/reload` | `webapp.py:2137` | 无调用；调用会把 interval 型任务顺延一整周期 |
| `POST /api/dashboard/ai/documents/manual` | `ai_web.py:1189` | 无 |
| `POST /api/dashboard/ai/continue/stream`、`/rewrite/stream`、`/audit/stream` | `ai_web.py:1197-1204,1748` | service 方法被 pipeline 内部用，HTTP 路由无人用 |
| `GET /drafts`、`PUT/DELETE /drafts/<id>`、`/history`、`/fork` | `ai_web.py:1518-1566` | 只写不读 |
| `POST /api/dashboard/ai/jobs/cleanup` | `ai_web.py:1631` | 调度器直接调 `db.cleanup_ai_jobs` |
| `/api/dashboard/ai/prompt-templates` 全部 6 条 | `ai_web.py:1787-1826` | 无 |
| `GET /chat/sessions/<id>/preview` | `ai_web.py` | 待确认 |
| `DELETE /api/dashboard/ai/model-sync-operations/<id>` | `ai_web.py:823` | 无「取消同步」按钮；用户对 10 分钟同步无中断手段 |
| `GET /api/dashboard/recommendations/runs`、`POST /profiles/<id>/default`、`DELETE /profiles/<id>` | `preference_web.py` | 仅测试 |

合计约 35 条。其中「实现了但前端没接」的功能：阅读进度、EPUB 导出、删除小说/作者/收藏、草稿管理、取消模型同步、退出登录。

### 4.2 死代码 / 仅测试引用

- `web/managers.py:920-992 SyncJobManager` 与 `:245-259 SyncJobState`：`src` 内唯一引用是 `webapp.py:34` 的再导出；`web/utils.py:170-175 _job_to_dict_unified` 的 else 分支随之成死分支。
- `sync_engine.py:302-372 check_bookmarks_existence`：docstring 自认「仅测试/兼容用途」，与 `check_all_existence` 重复 70 行。
- `sync_engine.py:756-759,809-816` `sync_following_list` 的「防误删」：函数没有任何 DELETE，且 API 返回空时把 `stats["users"]` 伪造成整张表行数。
- `web/utils.py:577-607 _remove_archive_files` 与 `storage_files.py:149-198 remove_novel_archive`：仅 `tests/test_archive_integrity.py` 用，`webapp._ArchiveTrash.stage()` 复制了同一段逻辑。
- `settings.py:437-449` pytz 分支、`:456-458,479-530 _simple_cron_next_run`：`pyproject` 要求 `python>=3.10` 且 `croniter` 硬依赖，永不执行。
- `storage/pending_and_watermarks.py:101 restore_pending_deletion`（非原子版）、`storage/schema.py:316 _fix_stale_running_logs`：完全无调用。
- `storage/novels.py:542 NovelsMixin.export_stats` 被 `storage_db.py:50 Database.export_stats` 逐字重复并遮蔽。
- `storage/rescue.py:795 get_rescue_catalog_item`、`:821 list_rescue_catalog_sources`、`storage/recommendations.py:333 get_recommendation_run`、`preferences.py:70 analyze_local`：仅测试。
- `storage/ai/model_sync.py:607 cleanup_model_sync_operations`：有定义无调用，文档说的「3 天清理」未接线。
- `ai/providers.py:706-714 ModelListResult` 三个字段是常量，`empty_authoritative` 分支死代码；`_parse_retry_after` 解析后无人消费。
- `ai/retrieval.py:557-561` sentence-transformers 分支：`core.py:117-123` 从不传 `use_embeddings`，不可达。
- `jobs/tasks.py:417-418` `elif event_type == "rate_limit"`：`recommendations.py` 从未 emit。
- 成人：`services/adult.py:1783 revalidate_stored_candidate`、`storage/ai/adult.py:476 get_adult_review_bindings`、`adult_policies.load_adult_policy` 仅测试；`AdultRouteRequest.participant_facts / protected_terms` 被设置但 `to_route_request` 不透传；`PreparedAdultJob.before / after / project / chapter_content / prompt_budget / access_token` 构造后无人读取（`access_token_hash` 比较是同义反复：apply 前先用当前请求 token 覆盖哈希再比较，必然相等）；`validate_adult_stream_preflight`（`ai_web.py:428-454`）与 `prepare_adult_job` 完全重叠；`_stream_prepared_adult_polish` 的非流式回退只服务于 fake router。
- `webapp.py:703/710` `_AUTH_EXEMPT_PATHS` 含 `/nginx-health`，Flask 无该路由。
- `cli.py:128 parser.error(...)` 不可达；`cli.py:104 sync-bookmarks` 忽略返回值，`truncated` 也退出 0。
- `base.html:11 darkMode: 'class'` 从未使用；`library-btn / library-input / library-table / library-panel` 等类除分页外 0 处使用。
- `dashboard_logs.html:678 'following_series': '关注系列'` 不是任何 task_type。
- `dashboard_novels.html:416,464` `rescueFilters.item_type` 恒为 `'all'` 无控件。
- 冗余索引：`idx_assets_novel_id`、`idx_sources_novel_id`、`idx_ai_jobs_job_id`（已被 UNIQUE 自动索引覆盖）；`idx_sources_source_type`、`idx_rescue_overrides_action`、`idx_reading_progress_status`、`idx_reading_progress_last_read`（无查询使用）；`idx_task_logs_auto_sync` 让优化器放弃 `started_at` 索引。
- 缺失索引：三张表的 `last_checked_at` 轮转查询走 `SCAN + TEMP B-TREE`（`ORDER BY (x IS NOT NULL), x` 前缀多余且阻止索引）；`novels WHERE status='deleted'` 全表扫。

### 4.3 其它 P3

- `jobs/runner.py:644-645` `active_claim.finish()` 返回 False 时不写终态（理论不可达）。
- `web/managers.py:876-880` 让位判定在 `request_cancel` 被拒时仍记一次让位并缩到 600 s 后。
- `jobs/quick_sync.py:72` `run_bookmark_sync` 不设 `service.stop_requested`；`:86` 在 web worker 线程 `print` JSON 到 stdout。
- `jobs/services.py:136` `run_scheduled_user_backup` 每个用户重新 OAuth 登录。
- `storage_files.py:116` `download_asset` 裸 `time.sleep`；`ai/services/generation.py:344,471` 蒸馏批间 `time.sleep(2)` 不可取消。
- `storage/users.py:26` `raw_json != '{}'` 守卫可被 `'"{}"'` 绕过（`sync_engine.py:2278` 传 `stable_json_dumps("{}")`）。
- `delete_novel / delete_user` 不清 `reading_progress`、`preference_analyzed_novels`；删项目不清 `ai_chat_sessions.imported_project_id`。
- `storage/schema.py:819-830 _migrate_ai_tables` 用 `try/except: pass` 代替 `PRAGMA table_info` 守卫。
- `storage/tasks.py:401-403 get_ai_task_logs` N+1。
- `storage/recommendations.py:495-506 create_recommendation_mute` 冲突时返回错误 id。
- `.trash/<uuid>` 在 `commit()` 失败或进程崩溃后永久残留，无启动清扫。
- 无 `wal_checkpoint` / `journal_size_limit`，长任务期间 WAL 涨到任务结束（待确认）。
- `webapp.py:451-461` `.env.example` 的 `PIXIV_FLASK_SECRET=` 空行会让 `.env` 出现两条同名键。
- 登录后不重建 session、无过期；`/api/health` 免鉴权泄露版本号与运行任务数。
- `hmac` 与「任务已在运行」状态码不一致（400 / 500 / 409 混用）。
- 生产用 Werkzeug 开发服务器（`cli.py:118`），无请求超时；可换 `waitress` 单进程多线程不破坏调度器注册表。
- nginx 无 `client_max_body_size`（`documents/upload` 大文本 413，待确认）。
- 状态更新把「无 / （无）」当伏笔插入（`projects.py:1390-1411`）；`PUT /states/<state_type>` 接受任意 `state_type` 且注入 prompt；章节归属未校验（跨项目 chapter_id）；`create_chapters_from_plan` 非事务；章节列表接口拉全量正文；`ai_jobs.input_json` 存整份 payload 含粘贴全文。
- 向导导入边角：`chat_wizard.py:423` 条件表达式优先级错，`:366-370` `chapter_number` 必须 int（模型输出 `"1"` 整次失败），`projects.py:1600-1604` 伏笔回收把用户 `notes` 覆盖成 evidence，`notes.html:252` 手动标记回收把 `resolved_chapter` 写成章节数。
- userscript：单章加载失败清空整个系列面板；非 401 失败一律「未找到可用的救援数据」；失效判定扫描整页 `body.textContent` 误伤正文含「作品不存在」的页面；SPA 站内跳转不重新触发；`@connect` 硬编码单域名。
- 小说详情页把 `rescue_state == 'partial'` 显示成「当前未进入救援列表」；详情页评估与公开 API 不受 pending 排除，与目录列表口径不一致；恢复后目录刷新失败返回 500 并把 sqlite 原文给前端。
- 反代未设 `DASHBOARD_TRUST_PROXY` 时救援限速退化为全局单桶；没有单独的 rescue token 撤销接口。
- 任务命名四处不一致（`TASK_LABELS` / `dashboard.html` / `dashboard_logs.html` / `settings_sync.html` 各一份）；页面标题 / 侧栏 / sr-only 三处叫法不一致。
- 硬编码 `#0096fa` 绕过 token（`novel_detail.html` 9 处、`token_login.html` 4 处、`ai_reader.html` 6 处）；`.line-clamp-2`、`.custom-select` 多份重复。
- 首页 3 s 轮询无退避；侧栏 footer 每页加载调 `/api/dashboard/status` 触发 4 个 COUNT 只为拿用户名。
- `stopAutoTask` 把「当前没有任务」的 200 `{ok:false}` 当成功静默；首页 `isTaskRunning` 只认 `running`；日志页 `queued / cancel_requested` 显示英文原文。
- 用户详情页把网络错误报成「未找到该用户」；向导页七个异步操作无 try/catch；Agent 页批量改绑不校验表单；系统页复制 Token 反馈显示在弹窗背后；成人设置页 403 文案「forbidden」不可操作。
- 移动端底栏 6 项 10px 字号，缺「待确认删除」与「任务日志」入口。

---

## 5. 需求覆盖矩阵

状态：完整 / 部分 / 缺失 / 不一致（代码行为与文档描述相反）。

### 5.1 同步、调度、限速、存储

| 需求 | 状态 | 备注 |
|---|---|---|
| 收藏 / 关注作者 / 订阅系列 / 三类状态巡检 / 待删除检测 / 用户备份 / 快速同步 | 完整 | 全部经共享管线；但 `user_backup` 截断不标 `truncated`（§2.2） |
| 内容哈希增量 | 不一致 | `meta_hash` 含实时计数字段，写盘增量实际几乎不命中 |
| 预检查（sync_check）加速 | 缺失 | 结果从未被消费（§2.2） |
| 水位续跑 / 让位可恢复 | 部分 | `following_novels` 取消路径不落水位 |
| 三态状态判定、双熔断、按 `last_checked_at` 轮转、`restricted_streak` 降频、`no_novels` 降频 | 完整 | 但系列熔断计数把非网络异常也算进去 |
| `partial` 终态、`rotation_pending` 例外、8 KB stats 裁剪 | 完整 | JOB_SYSTEM §3.5 未提 `rotation_pending / truncated` |
| 调度器优先级 / 让位 / 退避 / 从 task_logs 恢复 / 错峰 / 预算接口 / cron 预览 | 完整 | 让位在 `request_cancel` 被拒时仍记一次 |
| 手动任务可取消 | 缺失 | 只有调度器提交的任务能停 |
| 日志保留期可配（默认 14） | 完整 | JOB_SYSTEM / UNIFIED / README:145 / ADULT 指南仍写 3 天 |
| 阶段一吞吐（配额、系列独立分页、收藏独立分页、`FOLLOWING_LIST_MAX_PAGES`、FTS rowid 对齐） | 完整 | 全部落地并有测试 |
| 阶段二预算（`SCHEDULER_TASK_CONFIGS`、11 条 cron、三处一致断言） | 完整 | example 时区 Asia/Seoul，与 08-28 总计划 Self-Review #5 矛盾 |
| 阶段三设置五页、`SETTINGS_SECTIONS` 全划分、公共层 | 完整 | 计划引用的 `test_frontend_shared_layer.py` 不存在，断言在 `test_ai_page_routes.py` |
| task_logs owner lease / heartbeat（08-14 运行时完整性计划 T1） | 缺失 | 只完成「启动一次性回收」；`fail_stale_task_logs` 仍无条件 UPDATE |
| `sync/pagination.py` PaginationGuard（T2） | 已被替代 | 硬上限齐全，无重复 cursor 检测 |
| trash manifest + 启动重放（T4） | 部分 | 同卷 `.trash` + DB 失败回滚有；manifest / 启动扫描无 |
| 用户备份「网络失败自动重试 3 次」（UNIFIED 8.3） | 部分 | 只有 API 层装饰器重试，无任务级 |
| 待删除宽限期由配置控制（UNIFIED 5.3） | 不一致 | `pending_deletion_grace_period_days` 已删除并主动擦除 |
| `novel_status_batch_size` 可配（JOB_SYSTEM §5） | 不一致 | 不是 `SyncSettings` 字段，恒为常量 800 |
| 阅读进度（README:32） | 部分 | 后端三端点 + 表存在，前端只用 localStorage |
| EPUB 导出（README:32） | 部分 | 单路由存在，无 UI 入口 |
| 全文搜索（README:32） | 部分 | FTS5 表与 `escape_fts_query` 存在，`/api/dashboard/novels` 的 `search` 走 FTS 但 UI 无说明 |

### 5.2 鉴权、部署

| 需求 | 状态 | 备注 |
|---|---|---|
| Token 登录 / 会话 / CSRF / 代理头信任 | 部分 | 本机模式无 CSRF；非 ASCII token 无法登录 |
| OAuth PKCE 三条路径 | 缺失（前端） | 后端完整，前端字段错配导致手动路径不可用（§2.1） |
| 自动登录（Playwright） | 部分 | systemd PATH 无 Xvfb；deploy 不装浏览器 |
| `deploy.sh` / `update.sh` 幂等安全 | 部分 | `rm -rf` 数据路径；`/tmp` 密钥残留；nginx 硬编码域名 |
| legacy `install_server.sh` | 不一致 | 与 unit 不自洽 |
| `.env.example` 权威列表 | 部分 | `PIXIV_DASHBOARD_TOKEN`、`TRUSTED_FORWARDED_HOSTS`、`PIXIV_NGINX_CACHE_DIR`、`ENV_PATH`、embedding 四个变量未声明；`pixiv.username/password` YAML 路径未声明 |
| env 覆盖 YAML | 部分 | 只对 `pixiv / storage / dashboard_token` 成立，`sync.*` 无 env 覆盖 |

### 5.3 偏好与推荐（PREFERENCE_RECOMMENDER_REQUIREMENTS.md）

| 需求 | 状态 |
|---|---|
| 六类 term 本地统计、长度/来源/限制分布、画像 JSON 骨架、版本号 | 完整 |
| 分析范围（来源/公私/作者/标签/时间）、scope fingerprint | 缺失 |
| 排除失效项（`status='deleted'`） | 部分 |
| 热度分布（收藏/浏览） | 缺失（列已 SELECT 未用） |
| 正向维度 `relationship_dynamics / tone / pacing / narrative_patterns` | 缺失；`preference_context.py` strong 档消费 `narrative_patterns` 而无生产者 |
| 负向 `excluded_keywords / avoid_themes` | 部分，且被分析任务覆盖 |
| 多画像 / 命名 / 默认 / 删除 UI | 部分（API 完整，UI 无） |
| AI 结构化总结 | 缺失（README 已如实声明只做关键词清洗） |
| 流式进度三端点、`runs/<id>`、`items/<id>/sync` | 缺失 |
| 搜索计划四型 | 部分：`keyword` 型永不产出；`exclude_terms` 缺失；用户编辑/保存缺失 |
| 详情补全 | 部分：只对系列调 `novel_series` |
| 去重（本轮 / 历史 / 已归档 / 屏蔽） | 部分：已归档系列漏；默认排除全部历史推荐比需求严格 |
| 评分 | 部分：作者偏好缺；热度上限 15 > 单标签 12，与「轻微」相反；字数加分恒定 |
| 反馈：感兴趣 / 不感兴趣 / 屏蔽作者 | 完整（后端）；dismiss 后不从默认列表消失 |
| 反馈：屏蔽标签、待阅读/待同步、立即同步、查看/取消屏蔽、删除历史 | 缺失或仅 API |
| run 级原子发布、取消传播 | 完整（上轮 P0 已修） |
| 定时推荐 | 完整 |
| 偏好注入 AI 创作（向导 / 规划 / 续写 / Pipeline / 润色 / 审计 / 四档） | 完整 |
| 偏好注入成人润色 | 部分：字段解析并进 hash，未注入 prompt |
| 「Pipeline 不会因画像缺失失败」（§13.5） | 不一致：画像被删后所有生成入口抛错，`delete_preference_profile` 不清项目引用 |

### 5.4 AI 创作、路由、目录（AI_WRITING_STUDIO_PLAN.md、MODEL_ROUTING_GUIDE.md）

| 需求 | 状态 |
|---|---|
| 项目 CRUD、蒸馏四源、风格五滑块、封面上传（三重校验 + 原子写）、长篇规划 / 细纲、续写 / 改写、对话 / 心理润色、去 AI 味、审计、摘要、伏笔、状态记忆、语义检索、草稿版本、向导 + READY 导入、阅读页 | 完整 |
| 风格只注入规划 / 细纲 / 续写 / 润色，不注入审计 / 状态 / 摘要 | 完整 |
| Pipeline 每步状态 / 可重试 / 取消不留半成品 | 部分：断连不收尾；无取消按钮；无单步重试 |
| 中文输入预算 | 不一致（§2.4 P0） |
| 三类 Provider、`validate_base_url`、DNS pinning、禁重定向、证书校验、key 加密 v2、脱敏 | 完整；但 `localhost` opt-in 不可用、脱敏无字面值替换 |
| secret 变更时健康横幅提示 | 缺失（只在真正调用时报解密失败） |
| Retry-After | 部分：解析了无人消费 |
| 模型目录事实源、同步只碰 `discovered_*`、消失标记不删、`model_key` 不透明、canonical digest、operation 状态机、4MiB/20MiB/100 页/5000 模型、confirm-empty CAS、启动 reconcile | 完整 |
| `available_models_json` 退役 | 部分：字段仍可写、迁移每次重跑 |
| 模型池展开 / 循环检测 / 深度 8 / 64 / CAS 409 / 整批替换 | 完整；全图校验副作用见 §3 |
| 业务生成全经 `ModelRouter` | 完整（AST 守卫只覆盖 5 个方法，建议补全） |
| PromptBudget 公式、`context_overflow`、首 delta 前切换 / 之后 partial、取消不切换、Provider 短路、16/32/64/8、快照 hash、attempt 审计、`/continue`、owner token、lease / heartbeat CAS | 完整 |
| 30 分钟 deadline | 部分：只在候选切换与请求前检查 |
| 普通 job 服务端取消 | 缺失 |
| 检索 TF-IDF → API embedding → sentence-transformers | 部分：本地模型分支不可达 |
| SSE 事件不发 key / prompt；断线后可查 job 恢复 | 完整 |
| 固定 Agent 迁移、默认模型回落、能力校验 | 完整 |
| 健康横幅、探测端点、批量改绑、Agent 搜索多选、Provider 继承健康 | 完整；健康三处假绿见 §3 |
| Gemini、多版本候选 / diff / Agent 复制 / 费用估算 | 未实现（计划已标注） |

### 5.5 成人润色（ADULT_POLISH_USER_GUIDE.md、UNIFIED §13）

| 需求 | 状态 |
|---|---|
| HMAC 10 分钟 access token、事件六类白名单、两阶段 JSON review、角色 / 章节 revision CAS、`warning_ack_hash`、apply 在 `BEGIN IMMEDIATE` 内重验、应用后清理 `output_text`、不硬编码 xAI、不直读池 SQL、无 token 403、普通 Agent CRUD 不暴露成人 Agent、候选只在服务端缓冲 | 完整 |
| 取消回调传入 Router（三阶段）、断连传播 | 完整（上轮 P0 已修）；但主生成期间无 yield 导致断连不可察觉 |
| 主写作阶段实时 progress | 完整 |
| 审查阶段实时 progress | 缺失（阶段后批量） |
| 别名掩码 / 恢复同构 | 不一致（§2.9 P1，正文被篡改） |
| 目标片段 20–12000 码点 | 不一致：审查预算实际上限约 2000 中文字 |
| 参与者规则 | 不一致：预检与本地校验范围矛盾 |
| 阅读页重新生成 / 取消 / 断连恢复 | 缺失或已坏 |
| 偏好注入成人 prompt | 缺失（字段解析并进 hash，未注入） |
| 候选保留 3 天 | 不一致：走 14 天 |
| 30 分钟 deadline | 不一致：成人 job 不写 deadline |
| 策略升级路径 | 缺失（待确认是否有意） |

### 5.6 救援（RESCUE_USER_GUIDE.md、08-14 收尾计划）

| 需求 | 状态 |
|---|---|
| 资格规则（独立小说 / 系列 success / partial / 父项去重 / exclude 优先 / include 只修远端） | 完整 |
| pending 排除（只 pending，confirmed / restored 不排，include 覆盖） | 完整 |
| `personal_relation` | 完整 |
| `source_url` 原站链接 | 部分：列与回填有，救援列表 / 只读 API / 模板均未输出，提交 `3e9857e` 是半成品 |
| 预计算目录 + 事务化重建 + 增量刷新 | 完整；`refresh_rescue_entities` 名不存在但等价实现在，INDEX「未开始」会误导 |
| 触发点（同步 / 快速同步 / 执行器 / 首轮 / 纠错 / 删除 / pending） | 完整；「正文写入触发刷新」只存在于文档 |
| 列表 API 筛选 / 排序 / 503 / stale / 转义 / 查询计数断言 | 完整 |
| 4593 项性能 fixture、500ms/10s 门槛、`verify_rescue_performance.py` | 缺失 |
| 只读 API：Bearer / SHA-256 / 不接受 Cookie / 只 GET / 限流 / no-store / 500 不泄露 / 不枚举 | 完整；限流在鉴权后 |
| userscript 契约 | 完整 |
| Token 撤销接口 | 缺失（只能轮换） |

---

## 6. 文档与代码不一致

| 文档 | 说法 | 代码事实 |
|---|---|---|
| CLAUDE.md:31 / INDEX.md:114 | 测试 `1308 passed` / `1258 passed` | 1492 passed（本轮实测） |
| CLAUDE.md | 「a companion test fails if a broad `except Exception` swallows the `InterruptedError`」 | 只覆盖系列章节循环，其余 9 处仍吞 |
| CLAUDE.md | 「Per-novel work is content-hash incremental」 | `meta_hash` 含实时计数，几乎不命中 |
| CLAUDE.md | 「`Retry-After` is only parsed for AI providers」 | 解析了但无人消费 |
| CLAUDE.md / frontend-pages.md | 只有章节页需在生成前写回 `style_control` | 项目页两个生成入口同样需要但没做 |
| CLAUDE.md / INDEX.md | 4 份 08-14 计划「均未实施」 | 字面正确（新文件一个没建），但约 60% 目标已以其它方式完成（推荐原子发布、成人取消传播、救援刷新、部署契约、`datetime` 注解）；应改为「部分以其它方式实现，剩余：task_logs lease、pagination guard、trash manifest、AI 总结/解释、成人偏好注入、审查阶段实时 progress」 |
| JOB_SYSTEM.md:75 | 「清理超过 3 天…`cleanup_old_task_logs(days=3)`」 | 读 `task_log_retention_days` 默认 14 |
| JOB_SYSTEM.md:82 | 「web 停止按钮调用 `request_cancel`」 | 手动任务无取消入口 |
| JOB_SYSTEM.md:95 / §3.5 | 「带 `aborted_reason` 或 `incomplete` 一律 partial」 | 还包括 `truncated`；`incomplete + rotation_pending` 保持 succeeded；§3.5 未提两者 |
| JOB_SYSTEM.md:159,161 | 让位后「已完成的部分已入库，下轮从水位继续」 | `following_novels` 取消路径不保存水位 |
| JOB_SYSTEM.md:65 | `_has_any_running_web_job` 是「软约束」 | 在 `_lock` 内执行，是硬约束，且会因 `CANCEL_REQUESTED` 永久锁死 |
| JOB_SYSTEM.md:138 | `max_pages_per_run` 只用于关注作者 / 系列 | `user_backup` 也用它 |
| JOB_SYSTEM.md §5 | `novel_status_batch_size` 可配 | 非 `SyncSettings` 字段，恒 800 |
| UNIFIED §7.2 / README:145 / ADULT 指南:31 | 日志 / 候选保留 3 天 | 14 天 |
| UNIFIED §5.3 | 待删除宽限期可配 | 已删除 |
| UNIFIED §10.2 / §18 / PREFERENCE §18 | 推荐失败/取消隔离 MISSING | 已实现 |
| UNIFIED:310 | 成人「取消回调未传入 Router、progress 完成后才发」 | 取消与主阶段实时 progress 已实现；剩审查阶段与偏好注入 |
| UNIFIED:201 | rescue 触发点行号 | 全部过期；`novels.py:385` 正文写入触发不存在；`rescue_web.py:111-122` 不是「上传」 |
| UNIFIED §12.1 | 连接测试在 `admin.py:458` | 已漂移到 762 |
| PREFERENCE §7.4 | 热度「轻微」加分、作者「中权重」 | 热度上限 15 > 标签 12；作者项不存在 |
| PREFERENCE §7.2 | 去重只排除 dismissed 历史 | 默认排除全部历史推荐 |
| PREFERENCE §13.5 | Pipeline 不因画像缺失失败 | 画像被删即全部抛错 |
| PREFERENCE §10 | 五个 stream / runs / sync 端点 | 不存在；api-contract 与代码一致，是需求超前 |
| RESCUE_USER_GUIDE:135 | 「收藏同步不重建目录」 | `quick_sync.py:84` 无条件重建 |
| RESCUE_USER_GUIDE §3.3 | 触发表 | 遗漏 `user_backup`、`pending_deletion_detection` |
| RESCUE_USER_GUIDE:129,137 | `/dashboard/settings#manual` | 锚点不存在 |
| RESCUE_USER_GUIDE §4.1 | pending 排除 | 未说明详情页与公开 API 不排除 |
| ADULT_POLISH_USER_GUIDE:15-18 | 在 `/dashboard/settings` 配置 | 已拆为 `/settings/models`、`/settings/adult`；页签名实际「成人描写润色」 |
| ADULT_POLISH_USER_GUIDE | 「参与者必须是已确认的成年虚构角色」 | 还隐含「必须在目标片段被点名」且与预检矛盾 |
| ADULT_POLISH_USER_GUIDE / api-contract | 目标片段 20–12000 码点 | 审查预算实际上限约 2000 中文字 |
| ADULT_POLISH_USER_GUIDE | access token 有效期 10 分钟 | `/events` 每次重放都签发新 token，可无限续期 |
| ADULT_POLISH_USER_GUIDE | 「通过校验但尚未应用的候选会临时保存在 output_text」 | 未通过结构校验的候选也保存 |
| MODEL_ROUTING_GUIDE:100 | 「Provider 禁用会直接报错」 | 池路径静默跳过，只有 fixed 报错 |
| MODEL_ROUTING_GUIDE:112 | 「优先用 Provider 自带估算器」 | 无任何 Provider 实现 |
| MODEL_ROUTING_GUIDE:141 | provider 级错误只举鉴权 / 配置 | 429 / 5xx / 网络全是 provider 级并短路 |
| frontend-api-contract | `GET /api/dashboard/follows` Used by follows page | 无人调用 |
| frontend-api-contract | `/novels/{id}/progress` Used by 阅读页 | 无人调用 |
| frontend-api-contract | `GET /novels/{id}` 字段 `user_name`、`text` | 实际 `author_name`、`text_raw / text_markdown` |
| frontend-api-contract | `logs` `days=1\|3` | 1–90；页面 1/3/7/14；`category=ai` 成人 403 未记录 |
| frontend-api-contract | `users` 有 search；`page_size` 生效 | 无 search；固定 12 |
| frontend-api-contract | 目录响应含 `models_synced_at / models_sync_error` | 在 Provider 行上，不在目录响应 |
| frontend-api-contract | 成人 `cancel` 「请求体为空对象」 | 代码不读 body |
| frontend-api-contract | 成人 stream 字段清单 | 缺 `preference_profile_id / strength`（代码接受但不使用） |
| frontend-api-contract | 「AI 与偏好用 `{ok,data}`」 | 实际三种形状：raw / 半信封 `{ok, ...平铺}` / 全信封；`detail` 形状未提；`preference_web.fail` 一律 400 |
| frontend-pages.md | partial 表 | 漏 `dashboard_ai_health_band.html` |
| frontend-pages.md:87 / storage/utils.py:6-8 | 推荐项有 `source_url` | 无该字段，两个模板各自拼 URL |
| frontend-pages.md | 系统页「调低会立刻丢掉」 | 每小时清理一次 |
| library-os-style-guide | Buttons / Forms / Tables 用 `library-*` 类 | 除分页外 0 处使用 |
| library-os-style-guide | 日志四色 | `logs.html:324` 仅 error 一色 |
| AI_WRITING_STUDIO_PLAN §8 | `/documents/<id>`、`/archive/search` | 不存在（实际 `/series/search`） |
| AI_WRITING_STUDIO_PLAN §14.4 | 「断连保留 partial」「fallback 已通过 SSE 展示」 | pipeline 不成立；前端渲染为「处理中…」 |
| 08-28 总计划 Self-Review #5 | example 保持 UTC | 落地为 Asia/Seoul |
| 08-14 spec §4.2 / §4.3 | `following_max_novels_per_author` 默认 20、`series_max_pages_per_run` 默认 10 | 代码默认 `None`，20 / 10 只在 example |

---

## 7. 体验优化点（按影响排序）

### 全站

1. **列表状态不进 URL**：小说库 / 作者 / 日志 / 待删除 / 用户详情的筛选、页码、搜索都不 `replaceState`，从详情「返回列表」永远回到默认 tab 第 1 页。这是浏览大库最痛的一点。
2. **反馈方式五花八门**：`alert`（首页 / 系列 / 待删除 / 日志）、页顶 message 4 s（偏好 / 模型 / Agent / 成人 / 向导）、底部 toast（作者详情）、就地文字（系统 / 同步 / 小说详情）。base.html 提供 `window.toast()` 统一。
3. **确认框全是浏览器 `confirm`**，与 `app-modal` 割裂。
4. **时间戳**：base.html 提供 `formatDbTime`，全站替换 UTC 原样输出。
5. **导航来处**：详情页侧栏不高亮、各自硬编码返回目标。
6. **任务命名统一**：后端下发 `TASK_LABELS`，删三份前端副本。

### 首页

- 运行状态条以 `auto-sync/status.current_job` 为唯一数据源，显示 `task_name · phase · current/total`（后端已有 `progress.phase / current_novel`）；`stopAutoTask` 对不可让位任务（追更系列）加确认。
- 推书列表加「感兴趣 / 不感兴趣」快捷反馈与「隐藏已反馈」。

### 小说库 / 详情

- AI 创作 tab 的排序下拉无效但仍可选；拯救 tab `item_type` 过滤无 UI。
- 阅读进度只存 localStorage，换设备即丢；后端三端点已在，接上即可。
- 救援面板「标记已失效 / 仍可访问」缺一句解释会出现在 / 不出现在哪个列表；`partial` 状态显示成「未进入救援列表」。
- 「返回列表」按 `document.referrer` 或 `?from=` 判断来处。

### 任务日志

- 筛选需手点「筛选」但 category 切换立即清空其它条件；小说库是即改即查，应统一。
- 窄屏 8 列 `whitespace-nowrap` 只能横向滚动，最有用的「结果」列在最右；折叠成卡片。
- 详情弹窗执行日志只有 `error` 一色。

### 偏好与推荐

- 「任务状态」直接显示英文；5 分钟超时文案误导；「生成搜索计划」与「执行推书」可合并；初始无 loading 先闪空态。

### 同步设置

- 手动触发后只打一行「已加入队列」，无进度、无日志链接。
- 「启用全局同步」与首页「启用定时同步」两个开关语义相近位置分离。
- 时区下拉只有 8 个值；改动后无「未保存」提示；cron 输入框 `w-36` 手机上难输。

### 模型 / Agent / 成人设置

- 反馈在页顶 4 s 消失，模型池编辑器在下半部，保存后要滚上去才看到。
- Provider 删除确认不显示 `bound_agent_count`；「同步模型」无取消按钮。
- Agent 「编辑」在手机上不滚到顶部表单；「一键初始化」不说明会创建什么。
- 成人页 403 应识别并提示「需要配置 DASHBOARD_TOKEN 并登录」；角色表无别名列；停用后无恢复视图。

### AI 创作

- Pipeline 完成即丢进度与输出（§2.4 P1）；错误全变「AI 响应中断」；两个死按钮。
- 生成中允许编辑并保存正文，结束后表单被强制覆盖；切章无未保存提示；所有保存 / 删除按钮无 loading / 禁用。
- SSE 中断后没有「到任务日志用下一个模型继续」的引导。
- 路由 progress 只渲染成「处理中…」。
- `detectAITells` 用 `alert()`。
- 阅读页应用成人候选后跳回第 1 章。

### 登录

- 三个卡片编号 1/2/3 但第 2 张默认隐藏；无 `<form>` Enter 不提交；失败原因被吞。

---

## 8. 更优实现建议（只列明显更简单或更正确的）

1. **SQLite 连接改 `isolation_level=None` + 显式 `transaction()`**（`connection.py:120`）。全仓对 `db.conn` 的裸 DML 只在 storage 内部，切换后 `cleanup_stale_pending` 那类「隐式事务忘提交」整类消失，`_migrate_novel_fts_rowid:383` 的补丁式 `_commit_if_needed()` 与对应防御测试都不再需要。
2. **`init_schema` 拆成 `ensure_schema()`（进程级一次）与 `open()`（每请求）**；`_migrate_*` 里的 UPDATE / INSERT / CREATE INDEX 先 SELECT 判断有行才写，让请求路径只剩只读语句。
3. **统一 AI 预算单位**：把 `_utf8_tail / _fit_route_messages / _fit_tail_text_messages` 提到 `services/core.py`，`generation.py` 与 `chat_wizard.py` 全部改用；`PromptBudget` 增加 `"heuristic"` 估算器用 `chunking.estimate_token_count`。一处改动同时修 P0 与向导 P1。
4. **`_extract_json_object` 用 `json.JSONDecoder.raw_decode`** 替换 20 行手写配平。
5. **Pipeline 顶层 `try/finally` 收口**，比逐步骤补 `GeneratorExit` 更不易漏。
6. **章节 PUT 复用已有的 `chapter_revision` 做 CAS**（`update_ai_chapter` 加 `expected_revision` + `WHERE chapter_revision=?`）。
7. **`resetChapterWorkspace` 拆成 `resetForChapterSwitch()` 与 `reloadCurrentChapter()`**。
8. **救援认证不碰库**：`(token_hash, token_prefix)` 缓存到 `app.extensions`，顺序改为 IP 限速 → 缓存比对 → 视图单次开库。
9. **救援增量与全量共用一个资格判定入口**：`refresh_rescue_item` 删掉 `existing_series_ids` 过滤（`rescue.py:761-777`）即可，比新增 `refresh_rescue_entities` 更小。
10. **归档目录持久化**：`novels.archive_dir` 一列，同步时写入，删除 / 封面直接读，消除改名孤儿。
11. **推荐**：`existing` 在 `run()` 开头加载一次传入（600 次查询降为 1）；手工覆盖独立列，分析任务永不触碰；`run()` 不返回 `items`。
12. **`_is_blocked_ip` 先判 `is_loopback` 再判 `is_reserved`**。
13. **`validate_pool_graph` 只校验受影响子图**，或错误带池名并把预存在的坏池降为 warning。
14. **删掉 sync_check 整套**（`SyncJobManager`、`sync_check.py`、`check_bookmarks_existence`、`/check-bookmarks`），`existing_streak` 改查库。
15. **`save_sync_settings` 后对 cron 变更的任务重算 `next_run`**，删掉 `/settings/reload`。
16. **状态轮转查询** `ORDER BY last_checked_at, id` + 三张表各一个 `(last_checked_at, id)` 索引。
17. **`meta_hash` 剔除易变字段**，或文本未变时跳过 FTS 与正文重写。
18. **Werkzeug → waitress**（单进程多线程，不破坏调度器注册表）。

---

## 9. 上轮审计（2026-08-13）条目核实结果

| 条目 | 状态 | 证据 |
|---|---|---|
| P0 运行中任务日志被任意请求误判失败 | 已修复 | `schema.py:144-148` 不再在 `init_schema` 调用；`fail_stale_task_logs` 仅 `create_app` 一次。但实现仍无条件 UPDATE，无 lease |
| P0 推荐任务取消报 succeeded | 已修复 | `jobs/tasks.py:435-439` re-raise；`tests/test_jobs_tasks.py:866-910` |
| P0 推荐失败/取消污染已有结果 | 已修复 | `recommendations.py:86,131-143` 单事务发布；`tests/test_recommendations.py:512-615`。残余：单候选异常整轮作废、全失败仍绿（§2.6） |
| P0 成人取消未传入 ModelRouter | 已修复 | `adult.py:640-672 _adult_cancel_checker`，主写作 `:2521`、两个审查 `:1232-1241,1286`；断连 `ai_web.py:598-603` |
| P1 成人 SSE progress 延迟 | 部分修复 | 主写作走 `execute_stream` 实时 yield（`adult.py:2498-2545`）；审查阶段仍 `execute()` 后批量 flush（`:1224-1306`，测试名即「staged progress」） |
| P1 分页缺硬上限 / 重复游标 | 上限已修；游标未做 | 收藏 100、作者作品、关注列表 50、系列、user_backup 200、预检查 2000、取消检测 200；但 user_backup 被 `max_pages_per_run=2` 污染 |
| P1 Provider 空流 fallback / Anthropic 非流式忽略 `max_retries=0` | Provider 层已修，真实配置不可达 | `providers.py:874-1284` `max(0, config)`；但 DB 往返把 0 变 2（§2.5） |
| P1 legacy systemd 路径 / 用户不一致 | unit 已改；脚本仍不自洽 | `deploy/systemd` 用 `pixivsync` + `/opt/.../app`；`install_server.sh` 不把代码放进 `APP_DIR` |
| P1 归档删除一致性 | 已修复 | `_ArchiveTrash` 先搬后删、DB 失败回滚；残余：孤儿 `.trash`、confirm 在事务内 stage |
| P1 pending restore 非原子 | 已修复 | `restore_pending_deletion_atomic`；旧方法成死代码 |
| 中优 `_simple_cron_next_run` `datetime` 注解 | 已修复 | `settings.py:4` 模块级 import |
| 死代码 `SyncJobManager / SyncJobState` | 仍存在 | 且现在是唯一持有 `latest_matching_sync_check_scope` 的地方 |
| pyflakes 清理 | 部分 | 仍 6 条 |
| 偶发 `test_dashboard_rotates_single_active_token` | 本轮全量未复现 | — |

---

## 10. 建议的修复顺序

**第一批（各几行到几十行，先止血）**

1. `token_login.html` 三处 `has_refresh_token` + 调 `/oauth/save/<id>`。
2. `dashboard_wizard.html` 两处 `:is-open`。
3. `_run_single_task` 停止分支仍调用 `run_task` 或显式 `mark_cancelled`；`_submit_shared_job` 先建日志再 submit。
4. `cleanup_stale_pending` 无条件提交。
5. `sync_following_novels` 作者循环 `try/finally: _save_watermark()`。
6. `storage/ai/core.py` 与 `admin.py` 的 `or 默认值` 改 `None` 判断。
7. `_is_blocked_ip` 先判 loopback。
8. Anthropic `_resolve_base_url` 两处共用。
9. `recommendations.py:116` 候选级 try/except；全失败判黄/判红。
10. `dashboard_logs.html` 类型下拉值与时区；`dashboard.html` 状态条数据源。
11. 成人：`_token_variant_issue` 正则只在字符之间允许空白；`dashboard_ai_reader.html` 重新生成带 `X-Adult-Access-Token` 且 reset 延后。

**第二批（结构性，各需要一次设计）**

12. `init_schema` 进程级一次 + 迁移只读化；连接 `isolation_level=None`。
13. AI 预算单位统一（同时修 P0、向导 P1、PromptBudget 中文高估、成人审查 / 主 Agent 预算）。
14. Pipeline 顶层收口 + 前端工作区拆分 + 两个死按钮 + 错误透传。
15. 成人别名掩码携带表面形式 + 拒绝单字别名；参与者规则统一到 `target` 并前移到预检；非可见阻断立即 fail-closed；主生成期间节流 keepalive progress；成人 job 写 deadline + 调度器周期调 `fail_stale_ai_jobs`。
16. 本机模式 CSRF + Host 白名单。
17. `deploy.sh` 去掉 `rm -rf` 路径；`update.sh` 清理 `/tmp` 备份；unit PATH；nginx 模板化。
18. 推荐默认列表排除 dismissed / muted + 状态徽标；分析任务合并而非覆盖；`precise_queries` 配对；系列级归档排除。
19. 手动任务取消入口；`user_backup` 独立上限 + `truncated`；9 处 `except InterruptedError: raise`；系列熔断只对 `novel_series` 计数。

**第三批（清理与文档）**

20. 删 sync_check 整套、`SyncJobManager`、cron 回退、非原子 restore、`_fix_stale_running_logs`、`export_stats` 重复、冗余索引、约 35 条无调用路由（保留在契约里的标「仅 API」）、成人 `validate_adult_stream_preflight` 与 `access_token_hash` 同义反复。
21. 五处手写 fetch 迁 `csrfFetch / errorText` 并改 `test_frontend_library_os.py:395-407` 的断言方向。
22. 文档：§6 表逐条修正；INDEX 把 4 份 08-14 计划改为「部分已按其它方式实现」；CLAUDE.md 测试数与两处描述；`docs/superpowers/plans/2026-08-28-sync-throughput-and-budget.md` 标注「已拆为 phase1-3」。
