from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "pixiv_novel_sync" / "templates"
DOCS = ROOT / "docs"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_base_template_defines_library_os_design_system():
    html = read(TEMPLATES / "base.html")

    assert "data-theme=\"library-os\"" in html
    assert "--library-bg" in html
    assert "--library-accent" in html
    assert "library-shell" in html
    assert "library-sidebar" in html
    assert "library-main" in html


def test_library_main_can_shrink_to_mobile_viewport():
    html = read(TEMPLATES / "base.html")
    library_main_rule = html.split(".library-main {", 1)[1].split("}", 1)[0]

    assert "min-width: 0" in library_main_rule


def test_global_components_use_library_os_classes():
    html = read(TEMPLATES / "vue_components.html")

    assert "library-nav-link" in html
    assert "library-badge" in html
    assert "library-modal" in html
    assert "Library OS" in html


def test_dashboard_pages_are_marked_as_library_pages():
    pages = [
        "dashboard.html",
        "dashboard_follows.html",
        "dashboard_novels.html",
        "dashboard_novel_detail.html",
        "dashboard_series_detail.html",
        "dashboard_user_detail.html",
        "dashboard_pending_deletions.html",
        "dashboard_logs.html",
        "dashboard_settings_sync.html",
        "dashboard_settings_models.html",
        "dashboard_settings_agents.html",
        "dashboard_settings_system.html",
        "dashboard_preferences.html",
    ]

    for page in pages:
        html = read(TEMPLATES / page)
        assert "library-page" in html, page
        assert "library-page-header" in html, page


def test_dashboard_drops_activity_and_scheduler_panels_keeps_error_state():
    """首页下方的「最近活动」时间线与「定时任务」列表面板已移除（推书列表上来后
    它们信息重复），但推书结果的错误态保留。"""
    html = read(TEMPLATES / "dashboard.html")

    # 两个面板不再存在
    assert 'data-dashboard-card="activity"' not in html
    assert 'data-dashboard-card="scheduler"' not in html
    assert "最近活动" not in html
    assert "activityList" not in html
    assert "autoTasksList" not in html
    # 推书错误态保留
    assert "recommendationError" in html
    assert "推荐结果加载失败" in html
    assert "retryRecommendationItems" in html


def test_current_frontend_docs_describe_task_logs_and_ai_pages():
    readme = read(ROOT / "README.md")
    contract = read(DOCS / "frontend-api-contract.md")

    # 保留期从硬编码 3 天改成可配置的 sync.task_log_retention_days（默认 14 天）。
    # README 必须写出真实默认值和配置项名：按「只留 3 天」去规划「上线后观察一周再
    # 调限速参数」是行不通的，而这个误解正是文档没跟上代码造成的。
    assert "保留 14 天" in readme
    assert "task_log_retention_days" in readme
    assert "默认保留 3 天" not in readme
    assert "/dashboard/novels?category=rescue" in readme
    assert "userscripts/pixiv-rescue.user.js" in readme
    assert "| `/dashboard/logs` | `dashboard_logs.html` | 任务日志 |" in contract
    # AI 写作页面（wizard / AI 阅读页 / 项目封面）已随 ai-writing 分支剥离，
    # 契约不再记载这些路由；main 上幸存的 AI 端点断言见 test_ai_model_docs.py。


def test_task_logs_template_has_complete_ai_filters_and_details():
    html = read(TEMPLATES / "dashboard_logs.html")

    assert "filters.status" in html
    assert "/api/dashboard/ai/jobs/" in html
    assert "selectedLog.job_id || selectedLog.id" in html
    # 天数下拉不能给出超过默认保留期的选项：库里只留 14 天，列出 30 天只会让用户
    # 看到一段永远空着的窗口。原来这条断言写死「不许出现 7 天」，因为那时只留 3 天。
    assert '<option value="14">14 天</option>' in html
    assert '<option value="30">' not in html
    assert '<option value="90">' not in html
    assert "keyword_clean" in html
    assert "'cancelled': { label: '已取消'" in html
    assert 'v-html="formatResult(log)"' not in html
    assert "selectedLog.output_text" in html
    assert "formatJson(selectedLog.input)" in html
    assert "formatJson(selectedLog.output)" in html


def test_task_logs_template_surfaces_abort_and_incomplete_markers():
    """限流熔断/本轮没跑完必须在详情页统计面板里露出来。

    生产事故：状态检查被限流熔断，只查了 30/800 篇就中止，统计面板是白名单渲染，
    aborted_reason 一个字都不显示，运维看到的只有绿色「成功·完成」。
    """
    html = read(TEMPLATES / "dashboard_logs.html")

    assert "selectedLog.stats.aborted_reason" in html
    assert "中止原因" in html
    assert "selectedLog.stats.incomplete" in html
    assert "本轮未完成" in html
    assert "selectedLog.stats.remaining" in html
    assert "剩余待检查" in html
    # 轮转进度用「从未同步数 / 最久多少天」，不用 users_remaining——后者等于
    # 候选数减 users_limit，每轮恒定（生产上永远 251），摆在面板里是个不动的数字。
    assert "selectedLog.stats.rotation_never_synced" in html
    assert "从未同步的作者" in html
    assert "selectedLog.stats.rotation_oldest_age_days" in html
    assert "selectedLog.stats.rotation_deprioritized" in html
    assert "selectedLog.stats.users_remaining" not in html
    assert "selectedLog.stats.series_remaining" in html
    assert "selectedLog.stats.truncated" in html
    # 中止原因要翻成中文，别把 rate_limited 直接甩给运维
    assert "rate_limited" in html
    assert "suspicious_missing_streak" in html


def test_task_logs_template_sync_filter_uses_current_task_type_keys():
    """T1-10：同步筛选项的值必须对齐真实 task_type（bookmark / following_users）。

    旧值用的是过时的 key，选中后按值过滤永远匹配不到任何日志行。
    """
    html = read(TEMPLATES / "dashboard_logs.html")

    assert "value: 'bookmark'" in html
    assert "value: 'following_users'" in html


def test_task_logs_template_treats_naive_timestamps_as_utc():
    """T1-10：后端存的是无时区 UTC 串，格式化前补 Z 才不会被当成本地时间偏移。"""
    html = read(TEMPLATES / "dashboard_logs.html")

    assert "dateStr + 'Z'" in html


def test_library_contains_rescue_tab_and_api_contract():
    html = read(TEMPLATES / "dashboard_novels.html")

    assert "filters.category = 'rescue'" in html
    assert "['bookmark', 'following', 'rescue']" in html
    assert "/api/dashboard/rescues" in html
    assert "rescueFilters.state" in html
    assert "rescueFilters.item_type" in html
    assert '<option v-if="filters.category !== \'rescue\'" value="bookmarks_desc">' in html
    assert '<option v-if="filters.category !== \'rescue\'" value="views_desc">' in html
    assert "完整救援" in html
    assert "部分救援" in html
    assert "来自私人备份" in html


def test_rescue_library_exposes_content_and_source_filters():
    html = read(TEMPLATES / "dashboard_novels.html")

    assert "rescueFilters.content_kind" in html
    assert "rescueFilters.source_kind" in html
    assert "item.content_kind_label" in html
    assert "item.sources" in html
    assert "data.refreshed_at" in html
    assert "content_kind: rescueFilters.content_kind" in html
    assert "source_kind: rescueFilters.source_kind" in html
    assert "series_chapter" in html
    assert "subscribed_series" in html
    assert "h-10 overflow-hidden" in html
    assert "source.label" in html
    assert "rescueCatalog.stale" in html
    assert "item.content_kind === 'series'" in html
    assert "rescueFilters.item_type, rescueFilters.content_kind, rescueFilters.source_kind" in html


def test_rescue_catalog_time_uses_local_display_and_surfaces_backend_error():
    html = read(TEMPLATES / "dashboard_novels.html")

    assert "toLocaleString('zh-CN'" in html
    assert "error.value = displayError" in html
    assert "err.message" in html


def test_rescue_detail_pages_support_manual_override_with_csrf():
    novel = read(TEMPLATES / "dashboard_novel_detail.html")
    series = read(TEMPLATES / "dashboard_series_detail.html")

    for html, item_type in ((novel, "novel"), (series, "series")):
        assert "rescueOverride" in html
        assert "rescueMessage" in html
        assert "ensureCsrfToken" in html
        assert "X-CSRF-Token" in html
        assert f"const itemType = '{item_type}'" in html
        assert "/api/dashboard/rescue-overrides/" in html
        assert "saveRescueOverride" in html
        assert "clearRescueOverride" in html

    assert "complete_count" in series
    assert "expected_count" in series


def test_settings_contains_rescue_token_rotation():
    # 救援 API Token 随设置页拆分落到系统维护页（原 #rescue-api 分区）
    html = read(TEMPLATES / "dashboard_settings_system.html")

    assert "rescue-api" in html
    assert "/api/dashboard/rescue-token/status" in html
    assert "/api/dashboard/rescue-token/rotate" in html
    assert "rescueTokenPlaintext" in html
    assert "closeRescueToken" in html
    assert "copyRescueToken" in html


def test_frontend_contract_documents_exist_and_cover_core_topics():
    contract = read(DOCS / "frontend-api-contract.md")
    pages = read(DOCS / "frontend-pages.md")
    style = read(DOCS / "library-os-style-guide.md")

    for endpoint in [
        "GET /api/dashboard/status",
        "GET /api/dashboard/novels",
        "GET /api/dashboard/logs",
        "GET /api/dashboard/settings",
        "GET /proxy/image?url=...",
    ]:
        assert endpoint in contract

    for route in [
        "/dashboard",
        "/dashboard/novels",
        "/dashboard/preferences",
        "/token-login",
    ]:
        assert route in pages

    for token in ["--library-bg", "--library-surface", "--library-accent", "library-card", "library-table"]:
        assert token in style


def test_rescue_pages_and_api_contract_are_documented():
    pages = read(DOCS / "frontend-pages.md")
    contract = read(DOCS / "frontend-api-contract.md")

    assert "/dashboard/novels?category=rescue" in pages
    assert "userscripts/pixiv-rescue.user.js" in pages
    assert "拯救成功" in pages
    assert "救援 Token" in pages
    for endpoint in [
        "GET /api/dashboard/rescues",
        "PUT /api/dashboard/rescue-overrides/<item_type>/<item_id>",
        "DELETE /api/dashboard/rescue-overrides/<item_type>/<item_id>",
        "GET /api/dashboard/rescue-token/status",
        "POST /api/dashboard/rescue-token/rotate",
        "GET /api/rescue/v1/novels/<novel_id>",
        "GET /api/rescue/v1/series/<series_id>",
        "GET /api/rescue/v1/series/<series_id>/chapters",
    ]:
        assert endpoint in contract
    for security_term in [
        "Authorization: Bearer",
        "X-CSRF-Token",
        "Cache-Control: no-store",
        "X-Robots-Tag",
        "401",
        "404",
        "405",
        "429",
        "source_notice",
    ]:
        assert security_term in contract


def test_dashboard_header_holds_stats_without_manual_sync_controls():
    """统计上移到顶部横条；系列限制输入与三个同步按钮已移除。"""
    html = read(TEMPLATES / "dashboard.html")

    # 四项统计在 header 内
    header = html.split("</header>")[0]
    for label in ("小说总数", "关注作者", "追更系列", "待确认"):
        assert label in header, label

    # 已移除的控件
    assert "seriesSyncLimit" not in html
    assert "startManualSync" not in html
    assert "系列限制" not in html
    assert "开始同步" not in html
    assert "预检查</button>" not in html


def test_dashboard_drops_inline_running_log_terminal():
    """运行中任务的实时日志框已移除，改为跳转任务日志页。"""
    html = read(TEMPLATES / "dashboard.html")

    assert "logContainer" not in html
    assert "logLevelClass" not in html
    assert "logPrefix" not in html
    assert "latestJob.logs" not in html
    # 保留轻量运行提示并指向任务日志页
    assert 'href="/dashboard/logs"' in html
    assert "任务执行中" in html


def test_dashboard_puts_recommendations_above_activity():
    """推书结果是头部横条之后唯一的正文区块（下方两个面板已移除）。"""
    html = read(TEMPLATES / "dashboard.html")

    assert html.count("最近推书结果") == 1
    assert html.index("最近推书结果") > html.index("</header>")
    # 推书区块是 </header> 之后第一个也是最后一个区块
    body = html.split("</header>", 1)[1]
    assert body.count("<section") == 1


def test_recommendation_cards_link_to_pixiv_original_not_local_detail():
    """推来的书尚未归档，本地 /dashboard/novels|series 详情页会打不开。

    卡片必须指向 Pixiv 原站（show.php / series），否则用户只能看介绍、点不进去。
    """
    dashboard = read(TEMPLATES / "dashboard.html")
    preferences = read(TEMPLATES / "dashboard_preferences.html")

    assert "https://www.pixiv.net/novel/show.php?id=" in dashboard
    assert "https://www.pixiv.net/novel/series/" in dashboard
    # 首页不能再用本地详情页当推荐卡片的跳转目标
    assert "'/dashboard/novels/' +" not in dashboard
    assert "'/dashboard/series/' +" not in dashboard

    assert "https://www.pixiv.net/novel/show.php?id=" in preferences
    assert "itemUrl" in preferences


def test_dashboard_cards_use_library_os_surface_classes():
    """控制台迁移到 library OS：头部横条与推书区块都走 library-card 表面，标题用
    library-section-title，统计小卡片补上 shadow-sm（base.html 把它映射成
    --library-shadow）。下方两个面板移除后页面只剩这两块。"""
    html = read(TEMPLATES / "dashboard.html")

    # 头部横条与推书区块都走 library-card 表面
    assert html.count("library-card") >= 2
    # 标题走 library-section-title，不再是裸 text-sm font-bold
    assert "library-section-title" in html
    assert "text-sm font-bold text-gray-800" not in html

    # 统计小卡片必须拿到 library 阴影（base.html 的 .library-page .shadow-sm 覆盖）
    header = html.split("</header>")[0]
    assert header.count("shadow-sm") >= 4


def test_dashboard_recommendations_are_a_paged_list_not_a_card_grid():
    """首页推书改为列表 + 翻页，最新一轮在前（后端按 run_id 倒序）。

    卡片网格一屏只放得下 6 条且没有翻页，历史结果根本翻不到；列表 + app-pagination
    才能承载「最新在前、往下翻页」。
    """
    html = read(TEMPLATES / "dashboard.html")

    assert "app-pagination" in html
    assert "recommendationPage" in html
    assert "recommendationTotalPages" in html
    # 分页信封由带 page 参数的请求取回
    assert "recommendations/items?page=" in html
    # 不再是三列卡片网格
    assert 'class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">' not in html


def test_dashboard_header_is_a_rounded_library_card_not_a_square_sticky_bar():
    """截图里控制台头部是通栏直角横条，与下方 22px 圆角卡片割裂。

    头部改为页内第一张 library-card，与其余区块同一套表面；sticky 通栏横条移除。
    """
    html = read(TEMPLATES / "dashboard.html")

    assert "sticky top-0 z-30 bg-white/80 backdrop-blur-md" not in html
    header = html.split("</header>")[0]
    assert 'class="library-card"' in header


def test_dashboard_current_task_name_uses_chinese_labels():
    """横条上「任务执行中」旁显示当前活动任务名，按 task_type 映射成中文，
    不再显示英文内部键。活动列表面板移除后，映射表仍由 currentTaskName 使用。"""
    html = read(TEMPLATES / "dashboard.html")

    assert "TASK_TYPE_LABELS" in html
    assert "currentTaskName" in html
    assert "novel_status: '检查小说状态'" in html


def test_dashboard_status_bar_keeps_autosync_toggle_and_stop():
    """移除「定时任务」面板后，定时同步全局开关与「停止当前」必须保留在横条里。

    这是全局启停定时同步的唯一 UI 入口（设置页只有逐任务的 cron / 开关，没有这个
    master toggle），随面板一起删掉就会丢功能。当前活动任务也并入横条显示。
    """
    html = read(TEMPLATES / "dashboard.html")

    # 全局开关：toggleAutoSync + 两个状态文案都在
    assert "toggleAutoSync" in html
    assert "停用定时同步" in html
    assert "启用定时同步" in html
    # 停止当前：stopAutoTask 仍绑定，且依赖 current_task_job_id 显隐
    assert "stopAutoTask" in html
    assert "停止当前" in html
    assert "autoSyncStatus?.current_task_job_id" in html
    # 当前活动任务名 + 进度并入横条
    assert "currentTaskName" in html
    assert "任务执行中" in html


def test_sidebar_footer_shows_own_account_with_premium_badge():
    """侧边栏展示本人账号与会员状态，而不是最近同步的作者。"""
    html = read(TEMPLATES / "vue_components.html")

    assert "user.is_premium" in html
    assert "PREMIUM" in html
    assert "普通账号" in html
    assert "未绑定用户" not in html


def test_sidebar_expands_settings_into_subpages():
    """设置已拆成四个一级页面，侧栏必须展开成二级并区分当前页。

    父项用 startsWith('/dashboard/settings') 匹配，四个子页会同时高亮同一项——
    看不出当前在哪一页；子项必须精确匹配。
    """
    html = read(TEMPLATES / "vue_components.html")

    for label, path in (
        ("同步与调度", "/dashboard/settings/sync"),
        ("模型与 Provider", "/dashboard/settings/models"),
        ("Agent 绑定", "/dashboard/settings/agents"),
        ("系统维护", "/dashboard/settings/system"),
    ):
        assert path in html, path
        assert label in html, label
    # 「设置」本身指向 /dashboard/settings（高亮前缀），链接落到同步页
    assert "item.href || item.path" in html
    assert "currentPath === child.path" in html


def test_settings_split_pages_and_new_endpoints_are_documented():
    """文档不能再指向已删除的 dashboard_settings.html，三个新端点要入契约。"""
    pages = read(DOCS / "frontend-pages.md")
    contract = read(DOCS / "frontend-api-contract.md")

    assert "dashboard_settings.html" not in pages
    assert "dashboard_settings.html" not in contract
    # 成人润色设置页随 AI 写作模块移到 ai-writing 分支，main 上只剩四个设置页
    assert "/dashboard/settings/adult" not in pages
    assert "dashboard_settings_adult.html" not in pages
    assert "/dashboard/settings/adult" not in contract
    assert "dashboard_settings_adult.html" not in contract
    for route, template in (
        ("/dashboard/settings/sync", "dashboard_settings_sync.html"),
        ("/dashboard/settings/models", "dashboard_settings_models.html"),
        ("/dashboard/settings/agents", "dashboard_settings_agents.html"),
        ("/dashboard/settings/system", "dashboard_settings_system.html"),
    ):
        assert route in pages, route
        assert template in pages, template
        assert route in contract, route
        assert template in contract, template

    for endpoint in (
        "PUT /api/dashboard/settings/<section>",
        "POST /api/dashboard/settings/cron-preview",
        "GET /api/dashboard/auto-sync/budget",
        "GET /api/dashboard/ai/agents/<agent_id>/candidates",
    ):
        assert endpoint in contract, endpoint

    # 手动触发的 task_type 白名单曾漏掉 subscribed_series，文档要与 task_map 一致
    for task_type in (
        "subscribed_series",
        "user_backup",
        "pending_deletion_detection",
        "preference_analyze",
        "recommendation_run",
    ):
        assert task_type in contract, task_type

