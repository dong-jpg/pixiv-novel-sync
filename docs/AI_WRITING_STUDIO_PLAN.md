# AI 创作工作台实施计划

> 状态：Phase 1 基础实现已落地，待人工联调外部 API。  
> 最近更新：2026-05-26  
> 用途：记录 AI 创作工作台的产品决策、技术方案和后续实时变更。

## 1. 当前确认的产品决策

- 模块入口名称：`AI 创作`。
- 入口位置：dashboard 左侧导航，建议放在 `小说归档` 之后。
- 第一版目标：先做成 **AI 创作工作台 + 风格/小说蒸馏资料库 + 多 Agent/API 调度器**，不做训练模型。
- 第一版输出方式：使用 **流式输出**，不先做轮询版。
- API key：服务端 **加密存储**，前端永不返回明文。
- Provider 第一版支持范围：
  - `openai_compatible`
  - `anthropic`
  - `xai`
- Provider 第一版暂不实现：
  - `gemini`
- 蒸馏小说定义：提取剧情、角色、世界观、设定、伏笔、时间线等结构化资料，不是训练模型。
- AI 生成内容可以保存为本地草稿，但不混入 Pixiv 原始归档表，单独放 `ai_drafts`。

## 2. 阶段规划

### Phase 1：基础 AI 调用 + 续写/改写

实现：

- API Provider 配置。
- Agent 配置。
- Provider 支持：
  - OpenAI-compatible。
  - Anthropic。
  - xAI。
- API key 加密存储。
- 从归档小说选择文本。
- 上传 `.txt` / `.md`。
- 手动粘贴文本。
- AI 续写。
- AI 改写。
- 流式输出。
- AI job 记录。
- 草稿保存。
- `/dashboard/ai` 前端页面。

不做：

- Gemini。
- 多模型对比。
- RAG。
- 微调。
- 本地训练。
- 复杂 diff 对比。
- Prompt 模板管理。

### Phase 2：风格蒸馏 / 小说蒸馏 ✅

已实现：

- 文本切块（`split_text_by_chars`）。
- 风格 profile（`ai_style_profiles` 完整 CRUD + 流式蒸馏）。
- 小说 profile（`ai_novel_profiles` 完整 CRUD + 流式蒸馏）。
- 续写/改写时引用 profile（`style_prompt`/`novel_prompt` 参数）。

### Phase 3：体验增强 ✅

已实现：

- 草稿版本历史（`parent_draft_id` 递归查询 + fork）。
- Prompt 模板管理（`ai_prompt_templates` 表 + CRUD + 内置模板种子）。
- 内容审计（`stream_audit` + 7 维度审查）。
- 长文本智能处理（`_smart_context` 自动摘要）。
- 任务历史查看（`list_ai_jobs` 分页+过滤）。

未实现（低优先级）：

- 多版本候选。
- diff 对比。
- Agent 一键复制。
- Token/费用估算。

## 3. 当前代码库关键位置

后端入口：

- `src/pixiv_novel_sync/webapp.py`
  - `SyncJobManager`：约 `webapp.py:903`。
  - `create_app()`：约 `webapp.py:1452`。
  - 小说列表 API：约 `webapp.py:1850`。
  - 小说详情 API：约 `webapp.py:1875`。
  - 设置 API：约 `webapp.py:2000`、`webapp.py:2005`。

数据库：

- `src/pixiv_novel_sync/storage_db.py`
  - `Database.init_schema()`：约 `storage_db.py:50`。
  - `list_recent_novels()`：约 `storage_db.py:408`。
  - `get_novel_detail()`：约 `storage_db.py:483`。
  - `list_bookmark_novels()`：约 `storage_db.py:745`。
  - `get_series_detail()`：约 `storage_db.py:877`。
  - `list_user_novels()`：约 `storage_db.py:998`。

前端导航：

- `src/pixiv_novel_sync/templates/vue_components.html`
  - `NAV_ITEMS`：约 `vue_components.html:18`。
  - 移动端导航过滤：约 `vue_components.html:114`。

模板打包：

- `pyproject.toml`
  - 当前 package data 已包含 `templates/*.html`。

## 4. 建议新增文件

```text
src/pixiv_novel_sync/ai/
  __init__.py
  crypto.py
  models.py
  providers.py
  prompts.py
  service.py
  chunking.py

src/pixiv_novel_sync/ai_web.py
src/pixiv_novel_sync/templates/dashboard_ai.html
```

## 5. 后端模块职责

### `ai/crypto.py`

负责 API key 加密和解密。

建议类：

```python
class AISecretManager:
    def encrypt(self, plaintext: str) -> str: ...
    def decrypt(self, ciphertext: str) -> str: ...
```

加密密钥来源：

```text
PIXIV_NOVEL_SYNC_AI_SECRET_KEY
```

规则：

- 保存 Provider API key 时必须加密。
- 前端只返回 `has_api_key`。
- 前端不返回 `api_key` 或 `api_key_encrypted`。
- 如果密钥缺失，禁止新增/更新 API key。
- 如果密钥变更导致无法解密，页面提示用户重新填写 API key。

建议依赖：

```toml
cryptography>=42
```

### `ai/models.py`

定义内部数据结构。

建议：

```python
@dataclass
class AIProviderConfig:
    id: int
    name: str
    provider_type: str
    base_url: str | None
    api_key: str | None
    default_model: str | None
    timeout_seconds: int
    max_retries: int
    enabled: bool

@dataclass
class AIAgentConfig:
    id: int
    name: str
    task_type: str
    provider_id: int
    model: str | None
    system_prompt: str
    temperature: float
    top_p: float
    max_tokens: int
    context_window: int
    enabled: bool

@dataclass
class AIStreamChunk:
    type: str
    text: str = ""
    data: dict[str, Any] | None = None
```

### `ai/providers.py`

统一不同模型服务的流式接口。

建议接口：

```python
class AIProvider:
    def stream_generate(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
    ) -> Iterator[AIStreamChunk]:
        ...
```

实现：

```text
OpenAICompatibleProvider
AnthropicProvider
XAIProvider
```

#### OpenAI-compatible

适配：

```http
POST {base_url}/chat/completions
```

请求体：

```json
{
  "model": "...",
  "messages": [],
  "temperature": 0.8,
  "top_p": 0.9,
  "max_tokens": 4000,
  "stream": true
}
```

解析：

```text
data: {"choices":[{"delta":{"content":"..."}}]}
data: [DONE]
```

#### Anthropic

适配：

```http
POST {base_url}/v1/messages
```

默认 `base_url`：

```text
https://api.anthropic.com
```

Header：

```text
x-api-key: ...
anthropic-version: 2023-06-01
```

流式事件重点处理：

```text
content_block_delta
message_stop
error
```

#### xAI

xAI 使用 OpenAI 兼容的 Chat Completions 风格，但作为独立 provider type 保留，方便默认 base_url、模型列表和后续差异化处理。

默认 `base_url`：

```text
https://api.x.ai/v1
```

适配：

```http
POST {base_url}/chat/completions
```

请求体同 OpenAI-compatible：

```json
{
  "model": "grok-...",
  "messages": [],
  "temperature": 0.8,
  "top_p": 0.9,
  "max_tokens": 4000,
  "stream": true
}
```

解析同 OpenAI-compatible SSE。

### `ai/prompts.py`

集中放 prompt 模板。

Phase 1 实现：

```python
def build_continue_messages(...): ...
def build_rewrite_messages(...): ...
```

Phase 2 再实现：

```python
def build_style_distill_messages(...): ...
def build_novel_distill_messages(...): ...
```

续写规则：

```text
你要续写，不要总结。
保持人物设定。
保持文风。
不要突然跳剧情。
不要解释写作过程。
只输出正文。
```

改写规则：

```text
保留原剧情事实。
不新增重大事件。
不删除关键信息。
按用户目标改写。
只输出改写后正文。
```

### `ai/service.py`

业务编排层。

建议类：

```python
class AIWritingService:
    def list_providers(...): ...
    def create_provider(...): ...
    def update_provider(...): ...
    def delete_provider(...): ...
    def test_provider(...): ...

    def list_agents(...): ...
    def create_agent(...): ...
    def update_agent(...): ...
    def delete_agent(...): ...

    def stream_continue(...): ...
    def stream_rewrite(...): ...
    def save_draft(...): ...
```

### `ai/chunking.py`

Phase 1 需要：

```python
def get_tail_context(text: str, context_chars: int) -> str: ...
def split_text_by_chars(text: str, max_chars: int) -> list[str]: ...
```

Phase 2 用于蒸馏分块汇总。

## 6. 数据库设计

在 `Database.init_schema()` 后新增 `_migrate_ai_tables()`。

建议新增表：

### `ai_providers`

```sql
CREATE TABLE IF NOT EXISTS ai_providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    provider_type TEXT NOT NULL,
    base_url TEXT,
    api_key_encrypted TEXT,
    default_model TEXT,
    available_models_json TEXT,
    timeout_seconds INTEGER NOT NULL DEFAULT 120,
    max_retries INTEGER NOT NULL DEFAULT 2,
    proxy TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### `ai_agents`

```sql
CREATE TABLE IF NOT EXISTS ai_agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    task_type TEXT NOT NULL,
    provider_id INTEGER NOT NULL,
    model TEXT,
    system_prompt TEXT NOT NULL,
    temperature REAL NOT NULL DEFAULT 0.8,
    top_p REAL NOT NULL DEFAULT 0.9,
    max_tokens INTEGER NOT NULL DEFAULT 4000,
    context_window INTEGER NOT NULL DEFAULT 16000,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

`task_type`：

```text
continue
rewrite
distill_style
distill_novel
general
```

### `ai_jobs`

```sql
CREATE TABLE IF NOT EXISTS ai_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,
    task_type TEXT NOT NULL,
    agent_id INTEGER,
    status TEXT NOT NULL DEFAULT 'running',
    input_json TEXT NOT NULL,
    output_text TEXT,
    output_json TEXT,
    error_message TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### `ai_drafts`

```sql
CREATE TABLE IF NOT EXISTS ai_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source_job_id TEXT,
    parent_draft_id INTEGER,
    style_profile_id INTEGER,
    novel_profile_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### `ai_documents`

```sql
CREATE TABLE IF NOT EXISTS ai_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### `ai_style_profiles`

Phase 2：

```sql
CREATE TABLE IF NOT EXISTS ai_style_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_type TEXT,
    source_ids_json TEXT,
    profile_json TEXT NOT NULL,
    sample_prompt TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### `ai_novel_profiles`

Phase 2：

```sql
CREATE TABLE IF NOT EXISTS ai_novel_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_type TEXT,
    source_ids_json TEXT,
    profile_json TEXT NOT NULL,
    continuation_prompt TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

## 7. 建议新增 Database 方法

```python
# Provider
def list_ai_providers(self) -> list[dict[str, Any]]: ...
def get_ai_provider(self, provider_id: int, include_secret: bool = False) -> dict[str, Any] | None: ...
def create_ai_provider(self, data: dict[str, Any]) -> int: ...
def update_ai_provider(self, provider_id: int, data: dict[str, Any]) -> None: ...
def delete_ai_provider(self, provider_id: int) -> None: ...

# Agent
def list_ai_agents(self) -> list[dict[str, Any]]: ...
def get_ai_agent(self, agent_id: int) -> dict[str, Any] | None: ...
def create_ai_agent(self, data: dict[str, Any]) -> int: ...
def update_ai_agent(self, agent_id: int, data: dict[str, Any]) -> None: ...
def delete_ai_agent(self, agent_id: int) -> None: ...

# Job
def create_ai_job(...): ...
def update_ai_job(...): ...
def get_ai_job(...): ...

# Draft
def list_ai_drafts(...): ...
def get_ai_draft(...): ...
def create_ai_draft(...): ...
def update_ai_draft(...): ...
def delete_ai_draft(...): ...

# Document
def create_ai_document(...): ...
def get_ai_document(...): ...
```

## 8. Web/API 设计

建议新增：

```text
src/pixiv_novel_sync/ai_web.py
```

注册函数：

```python
def register_ai_routes(app: Flask, settings: Settings) -> None:
    ...
```

在 `create_app()` 中调用：

```python
from .ai_web import register_ai_routes
register_ai_routes(app, settings)
```

### 页面

```http
GET /dashboard/ai
```

### Provider

```http
GET    /api/dashboard/ai/providers
POST   /api/dashboard/ai/providers
PUT    /api/dashboard/ai/providers/<id>
DELETE /api/dashboard/ai/providers/<id>
POST   /api/dashboard/ai/providers/<id>/test
```

### Agent

```http
GET    /api/dashboard/ai/agents
POST   /api/dashboard/ai/agents
PUT    /api/dashboard/ai/agents/<id>
DELETE /api/dashboard/ai/agents/<id>
```

### 续写流式接口

```http
POST /api/dashboard/ai/continue/stream
```

SSE 事件：

```text
event: metadata
data: {"job_id":"..."}

event: delta
data: {"text":"..."}

event: done
data: {"job_id":"...","chars":1234}

event: error
data: {"message":"..."}
```

### 改写流式接口

```http
POST /api/dashboard/ai/rewrite/stream
```

### 草稿

```http
GET    /api/dashboard/ai/drafts
POST   /api/dashboard/ai/drafts
PUT    /api/dashboard/ai/drafts/<id>
DELETE /api/dashboard/ai/drafts/<id>
```

### 文档

```http
POST /api/dashboard/ai/documents/upload
POST /api/dashboard/ai/documents/manual
GET  /api/dashboard/ai/documents/<id>
```

### 归档选择

优先复用现有：

```http
GET /api/dashboard/novels
GET /api/dashboard/novels/<novel_id>
```

如 AI 页面需要轻量搜索，再新增：

```http
GET /api/dashboard/ai/archive/search?q=...
```

## 9. 流式实现建议

Flask 使用：

```python
return Response(
    stream_with_context(generator()),
    mimetype="text/event-stream",
    headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    },
)
```

生成流程：

```python
def generate():
    job_id = uuid.uuid4().hex
    db.create_ai_job(...)
    yield sse("metadata", {"job_id": job_id})

    output_parts = []
    try:
        for chunk in service.stream_continue(...):
            if chunk.type == "delta":
                output_parts.append(chunk.text)
                yield sse("delta", {"text": chunk.text})

        db.update_ai_job(..., status="succeeded", output_text="".join(output_parts))
        yield sse("done", {...})
    except Exception as exc:
        db.update_ai_job(..., status="failed", error_message=safe_error_message(exc))
        yield sse("error", {"message": safe_error_message(exc)})
```

注意：

- 不返回 API key。
- 不完整透传外部 API 原始错误体。
- 客户端断开时保证 DB job 不长期停留在 `running`。

## 10. 前端页面设计

新增模板：

```text
src/pixiv_novel_sync/templates/dashboard_ai.html
```

页面 tab：

```text
续写
改写
草稿
Agent 设置
API 设置
```

Phase 2 再增加：

```text
风格蒸馏
小说蒸馏
风格档案
小说档案
```

核心状态：

```js
const tabs = ['continue', 'rewrite', 'drafts', 'agents', 'providers']
const providers = ref([])
const agents = ref([])
const selectedAgentId = ref(null)
const streamOutput = ref('')
const streaming = ref(false)
```

前端读取 SSE：

- 使用 `fetch`。
- 使用 `res.body.getReader()`。
- 使用 `TextDecoder`。
- 用 buffer 处理 chunk 半包。
- 按 `\n\n` 切分事件。

## 11. 隐私与安全提示

AI 页面必须显示：

```text
你选择的文本会发送到当前 Agent 绑定的 AI API。
请确认 API 服务商可信。
```

Provider 页面可显示：

```text
provider_type
base_url
default_model
has_api_key
enabled
```

禁止显示：

```text
api_key
api_key_encrypted
```

## 12. 推荐实现顺序

1. 新增依赖 `cryptography`。
2. 新增 AI 数据表和 `Database` 方法。
3. 新增 `ai/crypto.py`。
4. 新增 `ai/providers.py`，实现：
   - OpenAI-compatible。
   - Anthropic。
   - xAI。
5. 新增 `ai/prompts.py`。
6. 新增 `ai/service.py`。
7. 新增 `ai_web.py`。
8. 新增 `dashboard_ai.html`。
9. 修改 `vue_components.html` 增加 `AI 创作` 导航。
10. 手动测试：
    - 创建 Provider。
    - 测试 Provider。
    - 创建 Agent。
    - 手动输入续写。
    - 从归档小说续写。
    - 改写。
    - 保存草稿。
11. 补充最小自动化测试。

## 13. 测试计划

建议新增：

```text
tests/test_ai_crypto.py
tests/test_ai_provider_payloads.py
tests/test_ai_db.py
```

重点测试：

- API key 加密后 DB 不含明文。
- Provider list 不返回密钥。
- OpenAI-compatible 请求 body 正确。
- Anthropic 请求 body 正确。
- xAI 请求 body 正确。
- SSE 格式输出正确。
- `archive_novel` 能读取 `get_novel_detail()` 的文本。
- AI job 成功/失败状态能写入 DB。

## 14. 风险点

1. **三类 Provider 首版同时做，适配复杂度较高**  
   xAI 可以复用 OpenAI-compatible 解析，但仍建议作为独立 provider type。

2. **流式输出与 DB job 状态同步**  
   用户关闭页面时要避免 job 长期处于 `running`。

3. **加密密钥丢失或变化**  
   旧 API key 会无法解密，需要 UI 提示重新填写。

4. **隐私风险**  
   存档小说文本会发送给外部 API，需要明确提示用户。

5. **长文本上下文控制**  
   Phase 1 默认只发送末尾 N 字，不直接发送整本。

## 15. 变更记录

### 2026-05-26

- 建立 AI 创作工作台实施计划文档。
- 确认第一版 Provider 支持 `openai_compatible`、`anthropic`、`xai`。
- 变更：第一版暂不实现 `gemini`。
- 变更：增加 xAI Provider 格式，默认按 OpenAI-compatible Chat Completions/SSE 适配。
- 已落地 Phase 1 基础实现：AI 数据表、加密密钥管理、Provider/Agent CRUD、流式续写/改写、草稿、文档、`/dashboard/ai` 页面和导航入口。
- 已新增 `.env.example` 示例：`PIXIV_NOVEL_SYNC_AI_SECRET_KEY`，用于加密 AI Provider API key。
- 已补充 Provider/Agent 编辑能力：列表中可载入配置后使用 `PUT` 更新，API key 不回显，编辑时留空则保持旧 key。
- 已修复流式任务预校验失败时尝试更新不存在 job 的问题。
- 已补充 `.txt` / `.md` 上传接口 `/api/dashboard/ai/documents/upload`，限制 UTF-8、后缀和 5MB 大小，并在页面续写/改写中支持上传文档作为输入。
- 已补充 Provider 禁用校验：禁用的 Provider 不会被调用。
- 已通过本地验证：`python -m compileall src/pixiv_novel_sync`、AI Provider/Agent HTTP CRUD 冒烟、流式预校验错误路径、文档上传接口、禁用 Provider 路径。
- 待联调：真实 OpenAI-compatible、Anthropic、xAI API 调用；浏览器端交互细节；客户端断连时的 job 状态表现。

### 2026-05-26（Phase 2/3 全面升级）

参考 inkos、MuMuAINovel、ai-novel-writer 三个开源项目，完成以下功能升级：

**数据库层（storage_db.py）：**
- 新增 `ai_prompt_templates` 表及索引。
- 补全 `ai_jobs`：`list_ai_jobs`（分页+过滤）、`delete_ai_job`。
- 新增 `ai_style_profiles` 完整 CRUD（6 个方法）。
- 新增 `ai_novel_profiles` 完整 CRUD（6 个方法）。
- 补全 `ai_documents`：`list_ai_documents`、`delete_ai_document`。
- 新增 `ai_prompt_templates` 完整 CRUD（5 个方法）。
- 新增 `ai_drafts.get_ai_draft_history`（递归版本链查询）。

**Prompt 层（ai/prompts.py）：**
- 新增 `build_style_distill_messages` — 风格蒸馏 prompt（参考 inkos 风格指纹提取）。
- 新增 `build_novel_distill_messages` — 小说蒸馏 prompt（提取角色/世界观/伏笔/时间线）。
- 新增 `build_audit_messages` — 内容审计 prompt（7 维度质量审查，参考 inkos 33 维度审计简化版）。
- 新增 `build_summarize_messages` — 摘要提取 prompt（长文本智能处理）。

**服务层（ai/service.py）：**
- 新增 `list_jobs`、`get_job` — 任务历史查询。
- 新增 `stream_distill_style`、`save_style_profile`、`list/get/update/delete_style_profile`。
- 新增 `stream_distill_novel`、`save_novel_profile`、`list/get/update/delete_novel_profile`。
- 新增 `stream_audit` — 流式内容审计。
- 新增 `list/get/create/update/delete_prompt_template`、`seed_builtin_templates`。
- 新增 `get_draft_history`、`fork_draft` — 草稿版本历史。
- 新增 `_smart_context` — 长文本智能上下文处理（自动摘要+末尾上下文）。
- 修改 `stream_continue`：支持 `smart_context` 开关、`style_prompt`/`novel_prompt` 引用。
- `_normalize_agent_payload` 新增 `audit` 类型支持。

**文本切块（ai/chunking.py）：**
- 新增 `estimate_token_count` — token 数估算。
- 新增 `needs_summarization` — 判断是否需要摘要。

**路由层（ai_web.py）：**
- 新增 `/api/dashboard/ai/jobs`、`/api/dashboard/ai/jobs/<job_id>` — 任务历史。
- 新增 `/api/dashboard/ai/drafts/<id>/history`、`/api/dashboard/ai/drafts/<id>/fork` — 草稿版本。
- 新增 `/api/dashboard/ai/distill/style/stream`、`/api/dashboard/ai/style-profiles/*` — 风格蒸馏。
- 新增 `/api/dashboard/ai/distill/novel/stream`、`/api/dashboard/ai/novel-profiles/*` — 小说蒸馏。
- 新增 `/api/dashboard/ai/audit/stream` — 内容审计。
- 新增 `/api/dashboard/ai/prompt-templates/*` — Prompt 模板管理。

**前端（dashboard_ai.html）：**
- tabs 从 5 个扩展到 10 个：续写、改写、内容审计、蒸馏、草稿、任务历史、档案、Prompt 模板、Agent 设置、API 设置。
- 续写：新增智能上下文开关、风格/小说档案选择器。
- 改写：新增归档小说选择支持。
- 新增内容审计 tab：支持多维度选择、归档小说/文档输入。
- 新增蒸馏 tab：支持风格/小说蒸馏、分块大小配置。
- 新增任务历史 tab：支持按类型/状态过滤、查看输出详情。
- 新增档案 tab：风格档案和小说档案列表、JSON 查看。
- 新增 Prompt 模板 tab：分类筛选、内置模板初始化、自定义模板 CRUD。
- 草稿：新增版本历史、创建新版本、删除功能。
- Agent 设置：task_type 新增 audit/distill_style/distill_novel 选项。

### 2026-05-28（Phase 4: 写作项目系统 + 深度优化）

参考 inkos（多 Agent 流水线 + 真相文件）、MuMuAINovel（RAG + 大纲驱动 + 伏笔三态）、ai-novel-writer（规则检测 + 铁律）三个开源项目，完成以下功能升级：

**P0 - Anthropic Provider 重试加固（providers.py）：**
- `AnthropicProvider` 对齐 OpenAI 的重试逻辑：502/503/504/408/429 指数退避。
- 新增 `_non_stream_generate` 非流式 fallback（解析 Anthropic content blocks）。
- 重试耗尽后自动降级到非流式调用。

**P1 - 写作项目系统（storage_db.py + service.py + ai_web.py）：**
- 新增 `ai_writing_projects` 表：项目名、大纲、关联风格/小说档案、设置。
- 新增 `ai_chapters` 表：章节序号、标题、内容、摘要、关键事件、大纲、字数。
- 新增 `ai_project_states` 表：持久化状态记忆（character_state / plot_progress / world_state）。
- 新增 `ai_foreshadows` 表：伏笔三态追踪（pending / approaching / resolved）。
- 完整 CRUD：项目 5 方法、章节 7 方法、伏笔 6 方法、状态 4 方法。
- 新增 `build_project_context`：自动构建项目级上下文（大纲 + 状态 + 伏笔提醒 + 前章摘要 + 上章末尾）。
- 新增 `stream_chapter_continue`：基于项目上下文的章节续写。
- 新增 `stream_update_project_state`：章节完成后 LLM 自动更新状态记忆 + 自动提取新伏笔。
- 路由：`/api/dashboard/ai/projects/*`、`/api/dashboard/ai/chapters/*`、`/api/dashboard/ai/foreshadows/*`。

**P2 - 持久化上下文记忆：**
- `ai_project_states` 表 UPSERT 机制，每次续写后可自动更新。
- `stream_update_project_state` 解析 LLM 输出的 `=== section ===` 格式，分别保存各状态类型。
- 续写时通过 `build_project_context` 自动注入历史状态，无需每次重新摘要全文。

**P3 - 伏笔管理三态：**
- `get_approaching_foreshadows`：当前章节 >= target - 2 时提醒。
- `get_overdue_foreshadows`：超过目标章节仍未回收时警告。
- 续写 prompt 自动注入超期/即将到期伏笔列表。
- `stream_update_project_state` 自动从 LLM 输出中提取新伏笔。

**P4 - detection 整合进审计：**
- `stream_audit` 调用前先跑 `detect_ai_tells` 规则检测。
- 规则检测结果（得分 + 问题列表）注入 LLM 审计 prompt 的 `rule_detection_context`。
- metadata 事件返回 `rule_detection.score` 和 `issues_count`，前端可展示。
- `build_audit_messages` 新增 `rule_detection_context` 参数。

**P5 - 大纲驱动续写：**
- `ai_chapters.outline` 字段存储章节大纲。
- `stream_chapter_continue` 自动加载章节 outline 作为 `plan_text`。
- 支持先用 `stream_plan` 生成构思 → 保存到章节 outline → 续写时自动引用。

**P6 - 语义检索（ai/retrieval.py）：**
- 新增 `TFIDFRetriever`：零依赖，基于字符 bigram + TF-IDF 余弦相似度。
- 新增 `EmbeddingRetriever`：可选 sentence-transformers，本地 embedding 语义检索。
- `create_retriever` 工厂函数：自动降级（无 sentence-transformers 时用 TFIDF）。
- `index_chapter_for_retrieval`：索引章节摘要和关键事件。
- `search_project_context`：按语义相关性召回历史片段。
- 路由：`POST /projects/<id>/chapters/<id>/index`、`GET /projects/<id>/search?q=...`。
