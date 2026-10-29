# Library OS Style Guide

Library OS 是本项目新的前端视觉语言：资料库管理台 + 阅读归档系统。目标是让同步任务、小说归档、作者系列、AI 创作和设置页面看起来属于同一个高质感工作台。

## Design principles

1. **资料库感**：页面像一个可长期维护的私人小说档案系统。
2. **低噪音**：浅色背景、清晰层级、少量高饱和强调色。
3. **操作明确**：同步、保存、检测等 mutation action 使用清晰主按钮。
4. **状态可扫读**：任务状态、日志级别、推荐反馈使用统一 badge。
5. **行为稳定**：视觉重写不改变现有 Vue state、API URL 或 SSE event。

## CSS tokens

Defined in `src/pixiv_novel_sync/templates/base.html`.

```css
:root {
  --library-bg: #f6f7fb;
  --library-surface: #ffffff;
  --library-surface-soft: #f9fbff;
  --library-ink: #162033;
  --library-muted: #6b7280;
  --library-faint: #8a94a6;
  --library-line: #e5e7ef;
  --library-accent: #4f7cff;
  --library-accent-strong: #3b63db;
  --library-success: #20b486;
  --library-warning: #d89016;
  --library-danger: #dc4b5d;
  --library-radius: 22px;
  --library-shadow: 0 18px 45px rgba(22, 32, 51, .07);
}
```

Required tokens for tests and future maintainers:

- `--library-bg`
- `--library-surface`
- `--library-accent`
- `--library-success`

## Layout

### Shell

- `library-shell`: full app flex layout。
- `library-sidebar`: fixed desktop sidebar, 260px wide。
- `library-main`: content region, `margin-left: 260px` on desktop。
- `mobile-bottom-bar`: mobile navigation, shown under 1024px。
- 手机最多五项：首页、书库、作者与系列、任务、更多。更多使用共享弹窗；阅读页以 `pns-reader-page` 替换为专用工具条，不能叠放两条底栏。
- 页面底部留白随实际底栏高度和 `env(safe-area-inset-bottom)` 调整；不能靠隐藏整页横滚掩盖不可达控件。

### Page

Every dashboard template should include:

```html
<div class="library-page ...">
  <div class="library-page-header ...">
    ...
  </div>
</div>
```

For pages that already have their own visible header, a screen-reader-only `library-page-header sr-only` is acceptable while the page is incrementally migrated.

## Typography

- Font stack: `Inter`, system UI, PingFang SC, Microsoft YaHei, Arial。
- Main titles: `library-title`, 34px desktop, 28px mobile。
- Metadata: 12-14px, `--library-muted`。
- Section titles: `library-section-title`, 17px, strong weight。

## Components

### Cards

Use `library-card` for contained information blocks.

```html
<section class="library-card">
  <h2 class="library-section-title">最近活动</h2>
</section>
```

### Panels

Use `library-panel` when the card contains its own table/list header and scroll area.

### Buttons

- `library-btn`: neutral action。
- `library-btn-primary`: primary mutation or main workflow action。

Guideline:

- Primary sync/save/generate actions use `library-btn library-btn-primary`。
- Secondary filter/refresh/export actions use `library-btn`。
- Destructive actions should still use red Tailwind classes or future `library-btn-danger`。
- 手机主要按钮、分页与弹窗操作触控范围至少44px；登录主控件至少48px。图标可小，命中区域不能随图标缩小。

### Forms

Use `library-input` for text/select/textarea when migrating markup.

手机输入文字至少16px；flex/grid 子项使用 `min-width: 0` 与限宽，长 Provider/Agent/模型选项不得撑宽整页。编辑操作应滚动定位并聚焦表单，错误靠近操作且不定时消失。

Focus state:

- accent border。
- soft blue focus ring。

### Tables

Use `library-table` for data tables.

```html
<table class="library-table">
  <thead>...</thead>
  <tbody>...</tbody>
</table>
```

Rows are visually separated through `border-spacing` and rounded row cells.

### Badges

Global component:

```html
<app-badge type="green">成功</app-badge>
```

Rendered class includes `library-badge`.

Recommended mapping:

- `green`: success/completed/synced。
- `blue` or `brand`: running/in progress/info。
- `yellow`: warning/pending。
- `red`: failed/error/destructive。
- `gray`: neutral/unknown。

### Modals

Global component class includes `library-modal`.

Used for log detail, confirmations, and settings dialogs.

`app-modal` 默认允许遮罩关闭，可用 `:close-on-backdrop="false"` 保护一次性内容。窗口可滚动且 footer 可换行；Tab 留在顶层窗口、Escape 只关闭顶层、关闭后恢复触发点焦点。不要绕开共用背景滚动锁或另写互相冲突的弹窗锁。Token 复制结果必须在弹窗内展示，明确关闭后清空明文。

弹窗内发起的操作必须在当前弹窗内回显结果，不能只调用位于遮罩下的全局 toast。错误持续到用户处理/重试，使用可访问的 alert；很长的结果文本应有可滚动、可聚焦的有限高度区域。

### Terminal/log blocks

Use `library-terminal` for streaming logs or AI job output.

Recommended log color mapping:

- info: slate text。
- success: green。
- warning: amber。
- error: red。

## Page-specific guidance

### Dashboard

Visual hierarchy:

1. Stats card：标题 + 四项核心统计，底部是运行状态条（定时同步开关、当前任务与进度、「停止当前」）。
2. 最近推书结果（单列卡片 + 分页）。

「最近活动」时间线与「定时任务」列表已移除：任务历史归任务日志页，逐项调度归设置页调度表。手动同步入口在 `/dashboard/settings/sync`，首页没有 `开始同步` 按钮。

Primary action: 运行状态条里的「启用/停用定时同步」。

### Novels

Layout:

- Filter/search hero at top。
- Card grid or table list。
- Cover thumbnails are rounded rectangles。
- Tags and status use badges。

### Novel detail

The reader page can keep immersive behavior, but should still identify as Library OS through `library-reader-page` and use Library OS color tokens where possible.

### Authors and series

Use people/collection cards:

- avatar or cover。
- name/title。
- metadata counts。
- status badge。

### Logs

Use a table or split table/detail layout.

- Filters in a `library-card`。
- Log detail in `app-modal` or `library-terminal`。

### Settings

设置是四个独立路由（sync / models / agents / system），页顶共用横向导航条 `dashboard_settings_nav.html`（`library-card` 内一排可换行的链接），不再是左侧 tab + 右侧面板的单页双栏。

Each setting group should be a card/panel with explicit save action.

### Preferences

推荐结果在前（与首页相同的单列卡片、过滤与分页），画像、增量分析与搜索计划折叠在其后的 `<details>`「画像与搜索设置」里；页面围绕默认画像，不做多画像列表。

### AI

AI 创作页（仅在 `ai-writing` 分支）交互风险最高；main 只有模型 / Agent 两个设置页，同样适用下面的「Do not change」。Apply Library OS gradually:

- outer shell and tabs first。
- cards/panels second。
- stream/log/status components third。

Do not change:

- SSE endpoints。
- event names。
- payload keys。
- long-running job state transitions。

## Responsive rules

- Desktop: fixed sidebar + content margin。
- Tablet/mobile: hide sidebar, show bottom nav。
- 推荐结果使用单列阅读卡片；管理表单在手机堆叠。封面书库可保留紧凑双列，不将其与推荐文字卡片混为一种布局。
- Page padding reduces to 16-22px on mobile。
- 验收至少覆盖360/390/430px、短横屏与桌面；以实际 DOM 尺寸和交互检查整页溢出，不将 Node/CSS 断言冒充浏览器或真机验收。
- 宽表仅在局部容器横滚；推荐标题、理由和按钮先换行或分区，不能让固定宽度的附属列挤走正文。

## Accessibility notes

- Preserve semantic headings even when using `sr-only` transitional headers。
- Buttons must remain real `<button>` elements for Vue click handlers。
- Links must remain `<a>` for server route navigation。
- Loading and error states should be visible text, not icon-only。
