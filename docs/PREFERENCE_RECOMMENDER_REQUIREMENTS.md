# 性癖分析与 Pixiv 推书模块需求文档

> 版本：v0.1  
> 日期：2026-06-01  
> 适用项目：pixiv-novel-sync  
> 目标：基于已归档小说，自动分析个人偏好画像，生成关键词/搜索词/标签，并在 Pixiv 中发现可能感兴趣的单篇小说或系列，同时把偏好画像接入 AI 创作流程。

---

## 1. 背景与现状

当前项目已经具备以下基础能力：

- Pixiv 小说归档：收藏、关注用户小说、追更系列同步。
- 本地 SQLite 存储：`novels`、`novel_texts`、`series`、`users`、`sources`、`novel_fts`。
- 小说元数据：标题、简介、标签、作者、字数、收藏数、浏览数、系列 ID。
- 小说正文：原文、Markdown、FTS 全文搜索。
- AI 创作工作台：Provider、Agent、风格蒸馏、小说蒸馏、长篇规划、章节续写、章节 Pipeline、项目状态记忆、检索模块。

新增模块应复用这些能力，不重新做独立系统。

---

## 2. 产品目标

### 2.1 核心目标

1. 从已归档小说中分析用户长期偏好画像。
2. 自动提取：
   - 关键标签；
   - 关键搜索词；
   - 常见题材/关系/情境/叙事偏好；
   - 正向偏好与负向排除项；
   - 作者、系列、长度、热度、更新时间等阅读倾向。
3. 使用画像生成 Pixiv 搜索计划。
4. 自动搜索并筛选候选小说/系列。
5. 对候选内容打分、解释推荐原因、去重并保存推荐结果。
6. 将偏好画像接入 AI 写文流程，用于创作向导、长篇规划、章节续写、润色和审计。

### 2.2 非目标

第一版不做：

- 自动收藏、点赞、关注、评论。
- 自动绕过 Pixiv 可见性、年龄限制或访问限制。
- 下载所有推荐结果全文作为默认行为。
- 训练本地模型或微调模型。
- 多用户画像系统。
- 复杂社交推荐。

---

## 3. 用户故事

### 3.1 偏好分析

作为用户，我希望系统读取我已经归档的小说，自动总结我偏好的标签、关键词、题材、关系、情境和文风，这样我不用手动整理搜索词。

### 3.2 搜索词生成

作为用户，我希望系统基于偏好画像生成多组 Pixiv 搜索词，包括宽泛搜索、精准搜索、排除词和标签组合，这样我能更高效发现新小说。

### 3.3 自动推书

作为用户，我希望系统自动在 Pixiv 搜索候选小说，并筛掉太短或不符合偏好的内容：

- 单篇小说：正文长度必须 >= 5,000 字。
- 系列小说：系列所有章节总字数必须 >= 20,000 字。
- 不设字数上限。

### 3.4 推荐解释

作为用户，我希望每条推荐都有解释：命中了哪些偏好、哪些标签、哪些关键词、为什么值得看、有什么风险点或不确定性。

### 3.5 AI 写文调优

作为用户，我希望 AI 写文模块能引用我的偏好画像，在构思、规划、续写和润色时更贴近我喜欢的题材、节奏、冲突和氛围。

---

## 4. 模块范围

建议新增一级功能入口：`偏好分析` 或放入 `AI 创作` 的子页：`偏好画像 / 推书`。

推荐拆成四个子模块：

1. 偏好画像分析器：Preference Analyzer
2. 搜索词生成器：Search Query Generator
3. Pixiv 推书检索器：Recommendation Finder
4. AI 创作偏好注入器：Writing Preference Injector

---

## 5. 数据来源

### 5.1 本地归档数据

优先使用本地数据库：

- `novels`
  - `novel_id`
  - `title`
  - `caption`
  - `user_id`
  - `series_id`
  - `text_length`
  - `total_bookmarks`
  - `total_views`
  - `tags_json`
  - `create_date`
  - `x_restrict`
  - `raw_json`
- `novel_texts`
  - `text_raw`
  - `text_markdown`
  - `text_hash`
- `series`
  - `series_id`
  - `title`
  - `description`
  - `user_id`
  - `total_novels`
- `users`
  - 作者名、账号
- `sources`
  - 来源类型：收藏、关注用户、追更系列等
- `novel_fts`
  - 标题、简介、作者、正文全文搜索

### 5.2 Pixiv 远程数据

第一版建议使用现有 Pixiv 认证能力，新增搜索能力：

- 关键词搜索小说。
- 标签搜索小说。
- 获取小说详情。
- 获取系列详情与章节列表。
- 获取章节字数，用于系列总字数过滤。

需要在实现前确认 `pixivpy3` 当前版本可用方法，例如：

- `search_novel(...)`
- `novel_detail(...)`
- `novel_series(...)`
- `novel_series_detail(...)`

如果 App API 搜索能力不足，再考虑 Web API fallback，但必须复用现有 cookie、限速和错误处理机制。

---

## 6. 偏好画像需求

### 6.1 画像维度

偏好画像应输出结构化 JSON，至少包含：

```json
{
  "version": 1,
  "summary": "总体偏好摘要",
  "positive_preferences": {
    "tags": [],
    "keywords": [],
    "themes": [],
    "relationship_dynamics": [],
    "scenes_or_situations": [],
    "tone": [],
    "pacing": [],
    "narrative_patterns": []
  },
  "negative_preferences": {
    "excluded_tags": [],
    "excluded_keywords": [],
    "avoid_themes": []
  },
  "search_strategy": {
    "primary_tags": [],
    "secondary_tags": [],
    "broad_queries": [],
    "precise_queries": [],
    "experimental_queries": [],
    "exclude_terms": []
  },
  "reading_bias": {
    "preferred_min_length": 0,
    "preferred_series": true,
    "preferred_authors": [],
    "common_x_restrict": [],
    "bookmark_range": null,
    "view_range": null
  },
  "confidence": {
    "overall": 0.0,
    "based_on_novel_count": 0,
    "based_on_total_chars": 0
  }
}
```

### 6.2 分析输入范围

用户可选择：

- 全部归档小说。
- 仅收藏来源。
- 仅私密/公开收藏。
- 仅追更系列。
- 指定作者。
- 指定标签。
- 指定时间范围。
- 排除过短小说。
- 排除已删除/不可见小说。

默认建议：

- 使用全部本地可见正文。
- 排除正文长度 < 1,000 的文本。
- 优先分析收藏与追更来源。

### 6.3 分析方式

建议两层分析：

#### 本地统计层

不依赖 LLM，稳定产出：

- 标签频次。
- 标签共现。
- 标题关键词频次。
- 简介关键词频次。
- 正文关键词/bigram 频次。
- 作者偏好。
- 单篇/系列占比。
- 字数分布。
- x_restrict 分布。
- 收藏数/浏览数分布。

#### AI 总结层

调用现有 AI Provider，基于抽样文本、统计结果和元数据总结：

- 主题偏好。
- 情境偏好。
- 关系动力。
- 文风偏好。
- 剧情结构偏好。
- 搜索词建议。
- 排除项建议。

### 6.4 分析任务形态

偏好分析应作为后台任务运行：

- 支持流式进度。
- 支持 job 记录。
- 支持失败重试。
- 支持保存多个画像版本。
- 支持手动命名画像。
- 支持设置一个默认画像。

---

## 7. 推书需求

### 7.1 搜索计划生成

基于默认偏好画像生成搜索计划：

```json
{
  "profile_id": 1,
  "queries": [
    {
      "query": "搜索词",
      "type": "tag|keyword|combined|experimental",
      "expected_reason": "为什么搜这个",
      "exclude_terms": [],
      "limit": 30
    }
  ],
  "filters": {
    "single_min_chars": 5000,
    "series_min_total_chars": 20000,
    "exclude_archived": true,
    "exclude_recommended_before": true,
    "exclude_muted_authors": true,
    "exclude_muted_tags": true
  }
}
```

### 7.2 搜索执行

每次推书任务流程：

1. 读取默认偏好画像。
2. 生成或读取搜索计划。
3. 按搜索词调用 Pixiv 搜索。
4. 合并结果。
5. 去重：
   - 已归档 novel_id。
   - 本轮重复。
   - 历史推荐且用户标记为不感兴趣。
6. 获取候选详情。
7. 判断单篇/系列。
8. 字数过滤：
   - 单篇：`text_length >= 5000`。
   - 系列：系列章节总字数 `sum(text_length) >= 20000`。
9. 打分排序。
10. 保存推荐结果。
11. 展示推荐解释。

### 7.3 推荐结果字段

推荐项至少包含：

```json
{
  "item_type": "novel|series",
  "novel_id": null,
  "series_id": null,
  "title": "",
  "author_id": 0,
  "author_name": "",
  "caption": "",
  "tags": [],
  "x_restrict": 0,
  "text_length": 0,
  "series_total_text_length": 0,
  "series_total_novels": 0,
  "total_bookmarks": 0,
  "total_views": 0,
  "score": 0.0,
  "matched_tags": [],
  "matched_keywords": [],
  "matched_preferences": [],
  "risk_notes": [],
  "reason": "推荐理由",
  "source_query": "",
  "status": "new|viewed|saved|dismissed|muted"
}
```

### 7.4 打分规则

第一版采用可解释规则分：

- 标签命中：高权重。
- 标题/简介关键词命中：中高权重。
- 作者历史偏好：中权重。
- 系列总字数达标：加分。
- 收藏/浏览热度：轻微加分，不覆盖个人偏好。
- 与负向排除词冲突：扣分或剔除。
- 已归档/已推荐/已 dismiss：剔除或降权。

AI 可用于生成推荐解释，但排序核心不应完全依赖 AI，避免不可控和成本过高。

### 7.5 用户反馈闭环

推荐结果支持用户操作：

- 感兴趣。
- 不感兴趣。
- 屏蔽这个作者。
- 屏蔽这个标签。
- 加入待同步/待阅读。
- 立即同步该单篇。
- 立即同步该系列。

这些反馈应回写画像或作为下一次推荐过滤条件。

---

## 8. AI 写文模块接入需求

### 8.1 接入点

应接入现有 AI 创作流程：

- 创作向导。
- 长篇规划。
- 章节详细梗概扩写。
- 章节续写。
- 章节 Pipeline。
- 润色。
- 去 AI 味。
- 内容审计。

### 8.2 注入方式

新增 `preference_profile_id` 参数。

在 prompt 中注入：

```text
【用户偏好画像】
- 偏好标签：...
- 偏好关键词：...
- 偏好题材/关系/情境：...
- 偏好节奏/文风：...
- 应避免：...
- 本次创作应优先满足：...
```

### 8.3 强度控制

用户可选择偏好注入强度：

- 关闭。
- 轻度：只作为参考，不改变主线。
- 标准：影响题材、冲突、氛围和描写重点。
- 强化：主动围绕偏好设计章节爽点和情节回收。

### 8.4 安全边界

- 偏好画像应仅本地存储。
- 前端展示时避免默认展开过于敏感的完整正文证据。
- 允许用户删除画像和反馈记录。
- AI prompt 注入前应只取摘要和结构化偏好，不直接拼接大量原文。

---

## 9. 数据库设计建议

### 9.1 偏好画像表

```sql
CREATE TABLE IF NOT EXISTS preference_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    source_scope_json TEXT NOT NULL,
    stats_json TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### 9.2 推荐任务表

```sql
CREATE TABLE IF NOT EXISTS recommendation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    search_plan_json TEXT NOT NULL,
    stats_json TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);
```

### 9.3 推荐结果表

```sql
CREATE TABLE IF NOT EXISTS recommendation_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    profile_id INTEGER NOT NULL,
    item_type TEXT NOT NULL,
    novel_id INTEGER,
    series_id INTEGER,
    title TEXT NOT NULL,
    author_id INTEGER,
    author_name TEXT,
    caption TEXT,
    tags_json TEXT NOT NULL,
    text_length INTEGER NOT NULL DEFAULT 0,
    series_total_text_length INTEGER NOT NULL DEFAULT 0,
    series_total_novels INTEGER NOT NULL DEFAULT 0,
    total_bookmarks INTEGER NOT NULL DEFAULT 0,
    total_views INTEGER NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 0,
    reason TEXT,
    matched_json TEXT NOT NULL,
    source_query TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(item_type, novel_id, series_id)
);
```

### 9.4 屏蔽与反馈表

```sql
CREATE TABLE IF NOT EXISTS recommendation_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_type TEXT NOT NULL,
    novel_id INTEGER,
    series_id INTEGER,
    author_id INTEGER,
    feedback_type TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recommendation_mutes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mute_type TEXT NOT NULL,
    mute_value TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(mute_type, mute_value)
);
```

---

## 10. 后端 API 建议

### 10.1 偏好画像

- `GET /api/dashboard/preferences/profiles`
- `GET /api/dashboard/preferences/profiles/<id>`
- `POST /api/dashboard/preferences/profiles/analyze/stream`
- `POST /api/dashboard/preferences/profiles/<id>/default`
- `PUT /api/dashboard/preferences/profiles/<id>`
- `DELETE /api/dashboard/preferences/profiles/<id>`

### 10.2 搜索计划

- `POST /api/dashboard/recommendations/search-plan`
- `POST /api/dashboard/recommendations/search-plan/stream`

### 10.3 推书

- `POST /api/dashboard/recommendations/run/stream`
- `GET /api/dashboard/recommendations/runs`
- `GET /api/dashboard/recommendations/runs/<id>`
- `GET /api/dashboard/recommendations/items`
- `POST /api/dashboard/recommendations/items/<id>/feedback`
- `POST /api/dashboard/recommendations/items/<id>/sync`

### 10.4 屏蔽

- `GET /api/dashboard/recommendations/mutes`
- `POST /api/dashboard/recommendations/mutes`
- `DELETE /api/dashboard/recommendations/mutes/<id>`

---

## 11. 前端页面建议

### 11.1 偏好画像页

功能：

- 选择分析范围。
- 选择 AI Agent。
- 启动分析。
- 展示进度。
- 展示统计结果。
- 展示 AI 总结。
- 保存画像。
- 设置默认画像。

### 11.2 推书页

功能：

- 选择画像。
- 生成搜索计划。
- 编辑搜索词。
- 设置过滤条件：
  - 单篇最小字数，默认 5,000。
  - 系列最小总字数，默认 20,000。
  - 排除已归档。
  - 排除已推荐。
  - 排除屏蔽作者/标签。
- 启动搜索。
- 推荐列表：卡片式展示。
- 推荐解释。
- 用户反馈按钮。
- 一键同步单篇/系列。

### 11.3 AI 创作页接入

在项目设置或生成面板新增：

- 偏好画像选择器。
- 偏好注入强度。
- 本次额外偏好说明。

---

## 12. 实施任务列表

### Phase A：本地偏好统计

1. 新增数据库迁移：`preference_profiles`。
2. 新增 `preferences` 后端模块。
3. 实现本地数据聚合查询：标签、关键词、作者、字数、来源、系列占比。
4. 实现正文关键词抽取。
5. 实现偏好统计 JSON 输出。
6. 新增偏好画像 CRUD API。
7. 新增偏好画像页面基础 UI。
8. 添加单元测试：统计聚合、空数据、短文本过滤、标签解析。

### Phase B：AI 偏好总结

1. 新增偏好分析 prompt。
2. 复用现有 AI Provider 流式接口。
3. 实现 `stream_analyze_preferences`。
4. 支持分批抽样与汇总，避免超上下文。
5. 保存 AI 输出为 `profile_json`。
6. 支持默认画像。
7. 添加测试：prompt 构建、JSON 解析、失败降级。

### Phase C：搜索词生成

1. 新增搜索计划 prompt。
2. 实现基于画像的规则搜索词生成。
3. 实现 AI 搜索词增强。
4. 支持用户编辑搜索计划。
5. 保存历史搜索计划到 recommendation run。
6. 添加测试：搜索词去重、排除词合并、空画像处理。

### Phase D：Pixiv 推书检索

1. 验证并封装 Pixiv 小说搜索 API。
2. 新增推荐运行表 `recommendation_runs`。
3. 新增推荐结果表 `recommendation_items`。
4. 实现搜索执行、分页、限速、错误处理。
5. 实现候选详情获取。
6. 实现已归档去重。
7. 实现单篇字数过滤：>= 5,000。
8. 实现系列总字数过滤：>= 20,000。
9. 实现推荐打分。
10. 实现推荐解释生成。
11. 添加测试：去重、字数过滤、系列聚合、评分排序。

### Phase E：反馈闭环

1. 新增反馈表。
2. 新增屏蔽表。
3. 实现推荐项状态更新。
4. 实现屏蔽作者、屏蔽标签。
5. 下一次推荐自动应用反馈过滤。
6. 添加测试：屏蔽过滤、dismiss 过滤、反馈持久化。

### Phase F：AI 创作接入

1. 在写作项目设置中加入 `preference_profile_id` 与注入强度。
2. 在创作向导 prompt 注入偏好摘要。
3. 在长篇规划 prompt 注入偏好摘要。
4. 在章节续写 prompt 注入偏好摘要。
5. 在章节 Pipeline 中允许每步读取偏好上下文。
6. 添加测试：偏好关闭/轻度/标准/强化四种 prompt 输出。

### Phase G：产品化与联调

1. 推书页面完善。
2. 任务日志接入。
3. 推荐运行进度流式展示。
4. 错误提示和重试。
5. 隐私说明和删除入口。
6. 浏览器端联调。
7. 端到端验收。

---

## 13. 验收清单

### 13.1 偏好画像验收

- [ ] 可以选择分析范围并启动分析。
- [ ] 没有 AI Provider 时，仍能生成本地统计画像。
- [ ] 有 AI Provider 时，能生成结构化偏好总结。
- [ ] 画像包含标签、关键词、题材、关系、情境、文风、排除项。
- [ ] 可以保存多个画像。
- [ ] 可以设置默认画像。
- [ ] 可以删除画像。
- [ ] 大量小说分析不会阻塞 Web 主线程。
- [ ] 分析失败时有可读错误信息。

### 13.2 搜索计划验收

- [ ] 可以基于画像生成搜索词。
- [ ] 搜索词包含宽泛、精准、实验性查询。
- [ ] 可以编辑搜索计划。
- [ ] 排除词会被保存并应用。
- [ ] 重复搜索词会被自动去重。

### 13.3 推书验收

- [ ] 可以启动 Pixiv 推书任务。
- [ ] 可以看到流式搜索进度。
- [ ] 已归档小说不会重复推荐。
- [ ] 单篇小说小于 5,000 字会被过滤。
- [ ] 系列总字数小于 20,000 字会被过滤。
- [ ] 推荐结果包含标题、作者、标签、字数、热度、推荐理由。
- [ ] 推荐结果按可解释分数排序。
- [ ] 每条推荐能展示命中的标签、关键词和偏好。
- [ ] 推荐任务失败后不会污染已有推荐结果。

### 13.4 反馈闭环验收

- [ ] 可以标记感兴趣/不感兴趣。
- [ ] 可以屏蔽作者。
- [ ] 可以屏蔽标签。
- [ ] 被屏蔽作者/标签不会再次出现在推荐中。
- [ ] dismiss 的推荐不会在默认列表重复出现。
- [ ] 可以查看和取消屏蔽项。

### 13.5 AI 创作接入验收

- [ ] 写作项目可以选择偏好画像。
- [ ] 可以选择偏好注入强度。
- [ ] 关闭时 prompt 不包含偏好画像。
- [ ] 轻度/标准/强化模式 prompt 内容不同且可预览。
- [ ] 创作向导能使用偏好画像产出更贴近偏好的构思。
- [ ] 长篇规划能引用偏好画像。
- [ ] 章节续写能引用偏好画像。
- [ ] Pipeline 不会因为画像缺失失败。

### 13.6 隐私与安全验收

- [ ] 偏好画像仅本地保存。
- [ ] 用户可删除画像、推荐历史、反馈记录。
- [ ] 不自动点赞、收藏、关注或评论。
- [ ] 不绕过 Pixiv 权限限制。
- [ ] API key 仍遵循现有加密存储规则。
- [ ] 前端不默认展示大段敏感原文证据。

---

## 14. 推荐 MVP 范围

第一版最小可用版本建议只做：

1. 本地偏好统计。
2. AI 偏好总结。
3. 搜索词生成。
4. 手动点击执行 Pixiv 搜索。
5. 单篇/系列字数过滤。
6. 推荐列表与推荐解释。
7. 不感兴趣、屏蔽作者、屏蔽标签。
8. AI 创作中只接入长篇规划和章节续写两个点。

暂缓：

- 自动定时推书。
- 多画像融合。
- 推荐结果全文下载后再二次分析。
- 高级 embedding 推荐。
- 多模型投票。

---

## 15. 技术风险

1. Pixiv 搜索 API 能力不确定：需要先验证 `pixivpy3` 搜索小说接口。
2. 系列总字数计算成本可能较高：需要缓存系列章节详情。
3. AI 分析可能输出非 JSON：需要保留 raw output 并支持重试解析。
4. 偏好画像内容较敏感：需要本地存储、可删除、前端谨慎展示。
5. 推荐排序容易过拟合热门标签：规则分应保留探索性搜索。
6. 长正文分析成本高：需要抽样、分批、摘要汇总。

---

## 16. 建议代码落点

后端：

- `src/pixiv_novel_sync/preferences.py`：偏好统计、画像分析、搜索词生成。
- `src/pixiv_novel_sync/recommendations.py`：Pixiv 搜索、筛选、打分、推荐结果持久化。
- `src/pixiv_novel_sync/preference_web.py`：偏好与推荐 API。
- `src/pixiv_novel_sync/ai/prompts.py`：新增偏好分析、搜索计划、偏好注入 prompt。
- `src/pixiv_novel_sync/ai/service.py`：创作流程读取偏好画像并注入 prompt。
- `src/pixiv_novel_sync/storage_db.py`：新增表和 CRUD。

前端：

- `src/pixiv_novel_sync/templates/dashboard_preferences.html`
- 或合并进现有 `dashboard_ai.html` 的子 tab。

测试：

- `tests/test_preferences.py`
- `tests/test_recommendations.py`
- `tests/test_ai_preference_injection.py`

---

## 17. 开发顺序建议

优先顺序：

1. 数据库迁移与本地统计。
2. 偏好画像保存和展示。
3. Pixiv 搜索 API 验证小实验。
4. 推荐筛选与字数规则。
5. 推荐列表页面。
6. 用户反馈过滤。
7. AI 写文接入。
8. 产品化联调。

这样可以先得到可用的画像和推荐闭环，再逐步增强 AI 总结与创作调优。
