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

### Forms

Use `library-input` for text/select/textarea when migrating markup.

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

1. Running job hero。
2. Stats cards。
3. Recent activity。
4. Auto-sync schedule。

Primary action: `开始同步`。

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

Use two-column layout on desktop:

- left tab navigation。
- right setting panels。

Each setting group should be a card/panel with explicit save action.

### Preferences

Use profile list + selected profile detail + recommendation queue.

### AI

AI page has the highest interaction risk. Apply Library OS gradually:

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
- Cards collapse to single column under 900-1024px。
- Page padding reduces to 16-22px on mobile。

## Accessibility notes

- Preserve semantic headings even when using `sr-only` transitional headers。
- Buttons must remain real `<button>` elements for Vue click handlers。
- Links must remain `<a>` for server route navigation。
- Loading and error states should be visible text, not icon-only。
