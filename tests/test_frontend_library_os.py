from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "pixiv_novel_sync" / "templates"
DOCS = ROOT / "docs"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# dashboard_ai.html 已按 docs/superpowers/specs/2026-09-02-dashboard-ai-page-split-design.md
# 拆成四个一级页面。dashboard_ai_pipeline_modal.html 不在其中：它只是章节页 include
# 进来的 markup partial，没有自己的路由，也没有自己的 setup()。
AI_PAGES = (
    "dashboard_ai_projects.html",
    "dashboard_ai_project.html",
    "dashboard_ai_chapters.html",
    "dashboard_ai_notes.html",
)


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
        "dashboard_settings_adult.html",
        "dashboard_settings_system.html",
        "dashboard_preferences.html",
        *AI_PAGES,
        "dashboard_wizard.html",
    ]

    for page in pages:
        html = read(TEMPLATES / page)
        assert "library-page" in html, page
        assert "library-page-header" in html, page


def test_ai_and_wizard_templates_do_not_embed_other_workspace():
    """AI 创作四页与创作向导互不夹带对方的工作区。

    拆页后 ai 侧从一个模板变成四个，逐页校验；夹带的判据不变——出现对方独有的
    状态名 / 表单名 / 端点，就说明又把两套工作区揉回一页了。
    """
    wizard = read(TEMPLATES / "dashboard_wizard.html")
    wizard_only = (
        "loadChatSessions",
        "openNewWizardSession",
        "/api/dashboard/ai/chat/",
        "showImportMaterialModal",
        "distillForm",
        "readerView",
        "providerForm",
        "continueForm",
        "auditForm",
        "promptForm",
        "'novel-search':",
        "'series-search':",
        # 旧版靠 pageMode / v-if="false" 在一个模板里塞两套页面，拆分后不许回来
        "pageMode",
        'v-if="false"',
    )

    for page in AI_PAGES:
        html = read(TEMPLATES / page)
        for token in wizard_only:
            assert token not in html, f"{page}: {token}"

    assert "loadChapterDashboard" not in wizard
    assert "startChapterPipeline" not in wizard
    assert "pageMode" not in wizard


def test_ai_pages_share_complete_output_panel_component():
    # 输出面板组件只有「会产出正文」的页面才用得上：章节页（自动写作）与创作向导
    chapters = read(TEMPLATES / "dashboard_ai_chapters.html")
    wizard = read(TEMPLATES / "dashboard_wizard.html")
    panel = read(TEMPLATES / "dashboard_ai_output_panel.html")

    include = '{% include "dashboard_ai_output_panel.html" %}'
    assert include in chapters
    assert include in wizard
    assert "window.aiOutputPanelComponent" in chapters
    assert "window.aiOutputPanelComponent" in wizard
    assert "emits: ['save', 'detect']" in panel
    assert "showDetect" in panel


def test_wizard_preserves_distill_sources_and_merge_controls():
    wizard = read(TEMPLATES / "dashboard_wizard.html")

    assert 'value="archive_novel"' in wizard
    assert 'value="archive_series"' in wizard
    assert 'value="document"' in wizard
    assert "distillForm.full_text" in wizard
    assert "distillForm.batch_size" in wizard
    assert "importOverwriteFields" in wizard
    assert "payload.overwrite_fields" in wizard


def test_dashboard_cards_stretch_and_recommendations_have_error_state():
    html = read(TEMPLATES / "dashboard.html")

    assert 'data-dashboard-card="activity"' in html
    assert 'data-dashboard-card="scheduler"' in html
    assert html.count("h-full flex flex-col") >= 2
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
    assert "/dashboard/wizard" in contract
    assert "/dashboard/novels/ai/<project_id>" in contract
    assert "/api/dashboard/ai/projects/<project_id>/cover" in contract


def test_frontend_pages_document_current_ai_boundaries():
    pages = read(DOCS / "frontend-pages.md")
    studio = read(DOCS / "AI_WRITING_STUDIO_PLAN.md")

    assert "AI 创作小说" in pages
    assert "`/dashboard/wizard`" in pages
    assert "`dashboard_ai_reader.html`" in pages
    assert "AI 创作任务已迁移到全局任务日志" in studio


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
    assert "polish_dialogue" in html
    assert "polish_psychology" in html
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
    assert "selectedLog.stats.users_remaining" in html
    assert "selectedLog.stats.series_remaining" in html
    assert "selectedLog.stats.truncated" in html
    # 中止原因要翻成中文，别把 rate_limited 直接甩给运维
    assert "rate_limited" in html
    assert "suspicious_missing_streak" in html


def test_ai_project_pages_prefer_cover_url_with_gradient_fallback():
    novels = read(TEMPLATES / "dashboard_novels.html")
    reader = read(TEMPLATES / "dashboard_ai_reader.html")
    # 封面的上传/删除 UI 收敛在项目页（章节页只有只读镜像，见拆分设计 §4.4）
    studio = read(TEMPLATES / "dashboard_ai_project.html")

    assert "item.cover_url" in novels
    assert ":src=\"item.cover_url\"" in novels
    assert "project?.cover_url" in reader
    assert "currentProject?.cover_url" in studio
    assert "coverGradient" in reader
    assert "uploadProjectCover" in studio
    assert "deleteProjectCover" in studio
    assert "data.cover_url + '?v=' + Date.now()" in studio


def test_ai_dashboard_api_adds_csrf_to_mutating_requests():
    base = read(TEMPLATES / "base.html")

    for page in AI_PAGES:
        html = read(TEMPLATES / page)
        # CSRF 助手只允许有一份，在 base.html 里；页面自建副本会让全站版本形同虚设
        assert "async function csrfFetch" not in html, page
        assert "function ensureCsrfToken" not in html, page
        assert "'/api/csrf-token'" not in html, page
        # 页面不得绕过助手直接发请求，否则生产环境的 CSRF 门会把它变成 403
        assert "await fetch(" not in html, page
        assert "window.fetch(" not in html, page

    # 「每页都必须出现 window.csrfFetch」这条正向断言不放在这里：拆页后只用
    # window.aiApi / streamSSE 的页面同样合规，而且本文件的 read() 不剥注释，
    # 页头那句「本页不再自建副本（window.csrfFetch …）」就能让它假通过。
    # 带注释剥离的条件式版本在 tests/test_ai_page_routes.py。
    assert "window.csrfFetch = async function" in base
    assert "'/api/csrf-token'" in base
    assert "'X-CSRF-Token'" in base


def test_ai_project_overview_uses_single_panel_and_preserves_independent_actions():
    """拆页后「概览」不再是内层 tab，而是项目页本身的上半部分。

    切片起点从 v-show="projectDetailTab === 'overview'" 换成 data-overview-panel，
    终点还是长篇规划的分节注释——版式契约（一块面板、三个 section、三套独立保存
    按钮）一条没变，只是换了宿主文件。
    """
    html = read(TEMPLATES / "dashboard_ai_project.html")
    # 内层 tab 是被拆掉的病根，别让它以 v-show 的形式回来
    assert 'v-show="projectDetailTab' not in html
    assert html.count("data-overview-panel") == 1

    overview = html.split("data-overview-panel", 1)[1].split(
        "<!-- ═══ 长篇规划 ═══ -->",
        1,
    )[0]
    profile_save = html.split("async function saveProjectProfiles()", 1)[1].split(
        "function addStyleTag",
        1,
    )[0]

    assert overview.count("data-overview-section") == 3
    assert 'data-overview-section="project"' in overview
    assert 'data-overview-section="profiles"' in overview
    assert 'data-overview-section="style"' in overview
    assert "data-overview-card" not in overview
    assert "items-stretch" not in overview
    section_openings = [
        overview.split(f'data-overview-section="{name}"', 1)[1].split(">", 1)[0]
        for name in ("project", "profiles", "style")
    ]
    assert all("h-full" not in opening for opening in section_openings)

    project_section = overview.split('data-overview-section="project"', 1)[1].split(
        'data-overview-section="profiles"',
        1,
    )[0]
    profiles_section = overview.split('data-overview-section="profiles"', 1)[1].split(
        'data-overview-section="style"',
        1,
    )[0]
    style_section = overview.split('data-overview-section="style"', 1)[1]

    assert '@click="$refs.coverInput.click()"' in project_section
    assert '@click="deleteProjectCover"' in project_section
    assert '@click="saveProjectMeta"' in project_section
    assert '@click="saveProjectProfiles"' in profiles_section
    assert "/dashboard/wizard?tab=distill" in profiles_section
    assert '@click="saveProjectStyleControl"' in style_section
    assert "async function saveProjectStyleControl()" in html
    assert "settings:" not in profile_save


def test_ai_chapters_page_persists_style_control_before_pipeline():
    """跨文件断言：风格设定必须在启动 pipeline 前落库（原来同文件，拆页后分了两处）。

    后端 ai/services/projects.py:_project_style_control_prompt 是从**已落库**的项目
    记录读 style_control 的，所以章节页即使不提供编辑 UI，也必须在生成前幂等写回一次，
    否则这次生成用的是上一次保存的风格。
    """
    chapters = read(TEMPLATES / "dashboard_ai_chapters.html")

    assert "async function saveProjectStyleControl()" in chapters
    assert "async function saveProjectProfiles()" in chapters
    # 单章 pipeline 与批量 pipeline 两个入口都要写回
    assert chapters.count("await saveProjectStyleControl()") >= 2
    assert chapters.count("await saveProjectProfiles()") >= 2


def test_ai_project_overview_keeps_project_summary_compact_at_narrow_desktop():
    html = read(TEMPLATES / "dashboard_ai_project.html")
    overview = html.split("data-overview-panel", 1)[1].split(
        "<!-- ═══ 长篇规划 ═══ -->",
        1,
    )[0]
    project_section = overview.split('data-overview-section="project"', 1)[1].split(
        'data-overview-section="profiles"',
        1,
    )[0]

    assert "lg:grid-cols-[7rem_minmax(0,1.35fr)_minmax(16rem,1fr)]" in project_section


def test_library_contains_rescue_tab_and_api_contract():
    html = read(TEMPLATES / "dashboard_novels.html")

    assert "filters.category = 'rescue'" in html
    assert "['bookmark', 'following', 'ai', 'rescue']" in html
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
        "POST /api/dashboard/ai/continue/stream",
        "GET /proxy/image?url=...",
    ]:
        assert endpoint in contract

    for route in [
        "/dashboard",
        "/dashboard/novels",
        "/dashboard/preferences",
        "/dashboard/ai",
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


def test_ai_templates_expose_preference_profile_and_strength_controls() -> None:
    # 偏好画像的选择控件在项目页（章节页只有只读镜像）与创作向导
    for name in ("dashboard_ai_project.html", "dashboard_wizard.html"):
        html = read(TEMPLATES / name)
        assert "preference_profile_id" in html, name
        assert "preference_injection_strength" in html, name
        # 画像列表改由 base.html 的 window.aiApi 统一加载，页面只保留调用点
        assert "preferenceProfiles" in html, name
        for strength in ("off", "light", "standard", "strong"):
            assert strength in html, (name, strength)
    assert "/api/dashboard/preferences/profiles" in read(TEMPLATES / "base.html")


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
    """推书结果展示框位于「最近活动」之前。"""
    html = read(TEMPLATES / "dashboard.html")

    assert html.count("最近推书结果") == 1
    assert html.index("最近推书结果") < html.index("最近活动")


def test_dashboard_activity_titles_use_chinese_task_labels():
    """最近活动的任务名按 task_type 映射成中文，不再显示英文内部键。"""
    html = read(TEMPLATES / "dashboard.html")

    assert "TASK_TYPE_LABELS" in html
    assert "TASK_TYPE_LABELS[item.task_type]" in html
    assert "novel_status: '检查小说状态'" in html


def test_sidebar_footer_shows_own_account_with_premium_badge():
    """侧边栏展示本人账号与会员状态，而不是最近同步的作者。"""
    html = read(TEMPLATES / "vue_components.html")

    assert "user.is_premium" in html
    assert "PREMIUM" in html
    assert "普通账号" in html
    assert "未绑定用户" not in html


def test_sidebar_expands_settings_into_five_subpages():
    """设置已拆成五个一级页面，侧栏必须展开成二级并区分当前页。

    父项用 startsWith('/dashboard/settings') 匹配，五个子页会同时高亮同一项——
    看不出当前在哪一页；子项必须精确匹配。
    """
    html = read(TEMPLATES / "vue_components.html")

    for label, path in (
        ("同步与调度", "/dashboard/settings/sync"),
        ("模型与 Provider", "/dashboard/settings/models"),
        ("Agent 绑定", "/dashboard/settings/agents"),
        ("成人润色", "/dashboard/settings/adult"),
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
    for route, template in (
        ("/dashboard/settings/sync", "dashboard_settings_sync.html"),
        ("/dashboard/settings/models", "dashboard_settings_models.html"),
        ("/dashboard/settings/agents", "dashboard_settings_agents.html"),
        ("/dashboard/settings/adult", "dashboard_settings_adult.html"),
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

