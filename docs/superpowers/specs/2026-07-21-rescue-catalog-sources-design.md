# 救援目录预计算与来源展示设计

## 决策状态

本设计已确认，替代《拯救成功与 Pixiv 原站救援阅读设计》中“后台列表查询时实时扫描全部正文并计算救援状态”的实现方式。

替代范围仅包括后台救援目录的生成、查询、筛选和卡片展示。油猴脚本使用的单项只读 API 仍按当前数据库事实实时校验，避免目录过期时错误开放普通私人备份。

## 背景与问题

当前 `GET /api/dashboard/rescues` 会在每次请求时执行以下工作：

1. 聚合所有系列及其章节正文完整度；
2. 扫描所有具有正文的失效小说；
3. 对候选小说逐项执行明细查询；
4. 在 Python 中完成筛选、排序和分页。

服务器当前约有 6268 本小说、272 个系列和 4593 项救援结果。第一页 12 条记录实测约需 48.7 秒，其中系列聚合约 11.2 秒、正文候选扫描约 21.2 秒、4579 次逐项查询约 18.6 秒。

与此同时，现有 `sources` 表已经记录小说的发现来源，但救援列表没有返回这些来源，也不能按来源筛选。

## 目标

- 将救援列表改为预计算目录，页面请求不再扫描正文；
- 将救援内容区分为系列、系列单章和独立小说；
- 展示每项救援数据的全部来源，并显示相关作者名；
- 支持按内容类型和来源筛选；
- 保持现有救援资格和完整度判断规则；
- 保持油猴脚本单项读取的实时安全校验；
- 在刷新失败时继续提供上一次成功目录。

## 不在本次范围内

- 修改 Pixiv 远端状态的判定规则；
- 修改油猴脚本的触发条件或正文渲染方式；
- 给只读救援 API 增加来源字段；
- 保存救援状态的完整历史时间线；
- 新增独立日志页面；
- 重构无关的同步任务或小说库页面。

## 已确认的产品规则

### 内容类型

救援目录使用三个 `content_kind`：

- `series`：系列级救援条目；界面显示“系列”；
- `series_chapter`：属于某系列、但以单章身份独立获救的小说；
- `standalone`：不属于任何系列的独立小说。

系列是否完整由独立的 `rescue_state=success|partial` 表达。界面不使用“完整系列”作为内容类型文案，避免出现“完整系列 / 部分救援”的矛盾组合。

父系列已经进入救援目录时，其章节不再重复显示为系列单章。只有父系列未形成救援条目、章节自身满足单篇救援规则时，才显示为系列单章。

### 来源

来源从现有 `sources` 表和 `series.is_subscribed` 推导，归一化为以下类型：

| 原始事实 | `source_kind` | 界面标签 |
| --- | --- | --- |
| `bookmark_public`、`bookmark_private` 或其他 `bookmark_%` | `bookmark` | 我的收藏 |
| `subscribed_series` 或 `series.is_subscribed=1` | `subscribed_series` | 我的追更 |
| `following_user_scan` | `following_user` | 关注用户：作者名 |
| `user_backup` | `user_backup` | 用户备份：作者名 |
| 未识别来源 | `other` | 其他来源 |

同一救援条目保留全部来源。收藏的公开/私密来源合并为一个“我的收藏”；关注用户和用户备份按来源用户 ID 去重，并通过 `users` 表解析作者名。

系列条目聚合全部本地章节来源，并额外读取 `series.is_subscribed`。同一来源归一化后只显示一次。

## 数据模型

### 正文完整度辅助列

为 `novel_texts` 增加：

```sql
has_content INTEGER NOT NULL DEFAULT 0
```

`upsert_novel_text()` 根据 `bool(text_raw.strip())` 写入该值。旧数据在迁移时执行一次完整回填。救援目录构建只读取 `has_content`，不反复对大段 `text_raw` 执行 `TRIM()`。

### `rescue_catalog`

```sql
CREATE TABLE IF NOT EXISTS rescue_catalog (
    item_type TEXT NOT NULL CHECK (item_type IN ('novel', 'series')),
    item_id INTEGER NOT NULL,
    content_kind TEXT NOT NULL CHECK (
        content_kind IN ('series', 'series_chapter', 'standalone')
    ),
    series_id INTEGER,
    title TEXT NOT NULL,
    user_id INTEGER NOT NULL DEFAULT 0,
    author_name TEXT NOT NULL DEFAULT '',
    cover_url TEXT,
    rescue_state TEXT NOT NULL CHECK (rescue_state IN ('success', 'partial')),
    remote_status TEXT NOT NULL,
    eligibility_reason TEXT NOT NULL,
    expected_count INTEGER,
    local_count INTEGER NOT NULL DEFAULT 0,
    complete_count INTEGER NOT NULL DEFAULT 0,
    last_checked_at TEXT,
    updated_at TEXT,
    refreshed_at TEXT NOT NULL,
    PRIMARY KEY (item_type, item_id)
);

CREATE INDEX IF NOT EXISTS idx_rescue_catalog_kind_state
    ON rescue_catalog(content_kind, rescue_state);
CREATE INDEX IF NOT EXISTS idx_rescue_catalog_checked
    ON rescue_catalog(last_checked_at DESC, item_id DESC);
CREATE INDEX IF NOT EXISTS idx_rescue_catalog_updated
    ON rescue_catalog(updated_at DESC, item_id DESC);
```

目录保存列表所需的展示快照，避免每次查询跨表聚合。正文不进入目录表。

### `rescue_catalog_sources`

```sql
CREATE TABLE IF NOT EXISTS rescue_catalog_sources (
    item_type TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    source_kind TEXT NOT NULL CHECK (
        source_kind IN (
            'bookmark', 'subscribed_series', 'following_user',
            'user_backup', 'other'
        )
    ),
    source_type TEXT NOT NULL,
    source_key TEXT NOT NULL DEFAULT '',
    source_user_id INTEGER,
    source_user_name TEXT,
    PRIMARY KEY (item_type, item_id, source_kind, source_key),
    FOREIGN KEY (item_type, item_id)
        REFERENCES rescue_catalog(item_type, item_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_rescue_catalog_sources_kind
    ON rescue_catalog_sources(source_kind, item_type, item_id);
```

收藏和追更使用空 `source_key` 实现类别级去重；关注用户和用户备份使用用户 ID 作为 `source_key`。

### `rescue_catalog_meta`

```sql
CREATE TABLE IF NOT EXISTS rescue_catalog_meta (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    refreshed_at TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL
);
```

只有完整刷新成功后才更新该单例。刷新失败时保留旧时间和旧统计。

## 目录生成规则

### 系列

沿用现有严格规则：

- 人工 `exclude` 时不进入目录；
- 人工 `include` 或 `series.status='deleted'` 时视为远端失效；
- `complete_count=0` 时不显示；
- `expected_count>0`、`local_count>=expected_count` 且 `complete_count=local_count` 时为 `success`；
- 其余有正文的失效系列为 `partial`。

### 系列单章与独立小说

- 人工 `exclude` 时不进入目录；
- 人工 `include` 或小说状态为 `deleted/restricted` 时视为远端失效；
- `novel_texts.has_content=1` 才能进入目录；
- `series_id IS NULL` 时为 `standalone`；
- `series_id IS NOT NULL` 且父系列未进入目录时为 `series_chapter`；
- 单篇条目始终为 `rescue_state='success'`。

## 刷新机制

### 完整刷新

`rebuild_rescue_catalog()` 使用集合式 SQL 在一个数据库事务内完成：

1. 根据当前状态、正文完整度和人工纠错生成系列目录；
2. 生成未被系列目录覆盖的系列单章和独立小说；
3. 聚合并归一化全部来源；
4. 更新 `rescue_catalog_meta`；
5. 提交事务。

事务内可以先删除旧派生数据再插入新数据；发生任何异常时整笔回滚，因此其他请求只能看到上一次成功版本或新版本，不会看到半成品或临时空目录。

完整刷新在以下任务成功结束后执行一次：

- 收藏小说同步；
- 关注用户小说同步；
- 追更系列同步；
- 用户全量备份；
- 小说状态检查；
- 系列状态检查。

调度器启动后，无论总定时同步开关是否开启，只要目录从未成功生成，就立即执行一次初始完整刷新。旧正文的 `has_content` 回填只在数据库迁移时执行一次。

### 增量刷新

- 小说人工纠错：刷新该小说及其父系列；
- 系列人工纠错：刷新该系列及其全部本地章节；
- 删除小说或系列：同步删除对应目录记录和来源记录；
- 非批量的正文、状态或来源变更：刷新受影响小说及父系列。

批量同步期间不逐条刷新目录，避免重复聚合；由任务成功结束后的完整刷新统一处理。

### 失败与过期

- 刷新失败时回滚并继续提供上一次成功目录；
- 失败原因、耗时和处理数量写入触发它的任务日志；
- 从未成功生成目录时，列表 API 返回 `503` 和“救援目录尚未生成”；
- `stale` 阈值为 `max(24, 2 * max(小说状态间隔, 系列状态间隔))` 小时；
- 超过阈值仍可读取旧目录，但页面显示“数据可能已过期”。

## 管理 API

### 列表参数

`GET /api/dashboard/rescues` 新增：

- `content_kind=all|series|series_chapter|standalone`；
- `source_kind=all|bookmark|subscribed_series|following_user|user_backup`。

已有 `state`、`search`、`sort`、`page` 和 `page_size` 保持不变。已有 `item_type=all|novel|series` 继续兼容；同时传入 `content_kind` 和 `item_type` 时，以更精确的 `content_kind` 为准。

来源筛选采用“包含即可”语义：多来源条目只要包含所选来源就进入结果。

所有筛选、搜索、排序、总数计算和分页均由 SQL 完成。分页后再用一次批量查询加载当前页全部来源，查询次数不能随目录总量增长。

### 列表响应

每个条目保留已有字段，并新增：

```json
{
  "content_kind": "series_chapter",
  "content_kind_label": "系列单章",
  "sources": [
    {
      "kind": "bookmark",
      "label": "我的收藏",
      "user_id": null,
      "user_name": null
    },
    {
      "kind": "following_user",
      "label": "关注用户：作者名",
      "user_id": 123,
      "user_name": "作者名"
    }
  ]
}
```

分页数据同时返回：

```json
{
  "refreshed_at": "2026-07-21 18:00:00",
  "stale": false
}
```

来源排序固定为：我的收藏、我的追更、关注用户、用户备份、其他来源；同类用户来源按用户名称和用户 ID 排序。

本次不修改 `/api/rescue/v1/` 的字段白名单。油猴脚本单项读取继续调用现有实时判定方法。

## 小说库界面

“拯救成功”区域保留救援状态筛选，并调整为三组筛选器：

- 救援状态：全部、完整救援、部分救援；
- 内容类型：全部、系列、系列单章、独立小说；
- 拯救来源：全部、我的收藏、我的追更、关注用户、用户备份。

卡片展示规则：

- 封面左上显示内容类型；
- 封面右上显示完整救援或部分救援；
- 卡片正文增加固定高度的来源区域；
- 全部去重来源均可见，不折叠为单一主来源；
- 具体作者名过长时视觉省略，原生悬停提示显示完整标签；
- 来源区域允许换行，但所有卡片总高度保持一致；
- 没有可识别来源时显示“来源未记录”；
- 页面显示目录更新时间；过期时在时间旁显示警告。

跳转规则：

- 系列进入现有系列详情页；
- 系列单章进入对应小说阅读页；
- 独立小说进入对应小说阅读页。

移动端筛选器自然换行，不使用横向溢出容器；来源文字不得遮挡标题、作者或分页控件。

## 性能要求

- 列表查询不得读取 `novel_texts.text_raw`；
- 列表不得先加载全部目录再在 Python 中分页；
- SQL 查询次数保持常数，建议为总数、分页项目、当前页来源三次；
- 以服务器当前约 4593 项数据为基准，第一页接口响应目标不超过 500ms；
- 完整目录刷新目标不超过 10 秒，不计一次性旧正文 `has_content` 回填；
- 刷新不能让读请求观察到半成品目录。

自动化测试不使用脆弱的绝对耗时断言，而是验证固定查询次数、SQL 不读取正文和 SQL 层分页。部署验收再记录真实服务器耗时。

## 测试策略

### 存储测试

- `has_content` 对正文、空字符串和纯空白正文的判定；
- 系列、系列单章和独立小说分类；
- 父系列存在时章节去重；
- 完整与部分系列判定保持现有语义；
- 四类来源映射、具体作者名称和多来源去重；
- 系列聚合章节来源和 `is_subscribed`；
- 未知来源归入 `other`；
- 完整刷新成功更新元数据；
- 刷新失败回滚并保留旧目录；
- 人工纠错增量刷新；
- 删除实体清理目录。

### API 测试

- 三种内容类型筛选；
- 四种主要来源筛选；
- 状态、类型、来源和搜索组合筛选；
- 多来源条目可以被任一来源命中；
- 排序、分页和总数；
- `item_type` 兼容以及 `content_kind` 优先级；
- 来源对象字段、标签和固定排序；
- `refreshed_at`、`stale` 和未初始化 `503`；
- 列表 SQL 不包含 `text_raw`，查询次数不随目录总量增长；
- 只读救援 API 继续实时拒绝不满足资格的对象。

### 前端测试

- 三组筛选器正确传递参数并重置页码；
- 三种内容类型标签和跳转地址；
- 全部来源标签可见；
- 长作者名和四类来源同时存在时卡片高度稳定；
- 移动端筛选换行且无重叠；
- 目录过期和未生成错误状态。

### 完成验证

- 运行完整 `pytest`；
- 检查数据库迁移和一次性回填；
- 部署后记录目录条目数、刷新耗时和第一页接口耗时；
- 验证自动任务日志包含目录刷新结果；
- 验证油猴脚本读取行为未发生回归。

## 部署与回滚

1. 部署数据库迁移、目录构建和新列表接口；
2. 服务启动后自动执行首次目录刷新；
3. 确认目录数量与旧实时查询结果一致；
4. 部署并验证新筛选器和来源标签；
5. 完整测试通过后推送并运行服务器 `update.sh`；
6. 记录线上接口性能和任务日志。

回滚代码时保留新增表和辅助列，不删除用户数据。旧代码会忽略这些派生结构；再次升级时可以重新生成目录。

## 验收标准

- 页面能明确区分系列、系列单章和独立小说；
- 页面展示全部救援来源以及相关作者名；
- 内容类型和来源筛选结果准确；
- 系列章节不重复展示；
- 页面不再因全库正文扫描出现几十秒等待；
- 刷新失败不会清空上一次成功结果；
- 油猴脚本仍只读取实时满足救援资格的内容；
- 完整测试和服务器性能验收通过。
