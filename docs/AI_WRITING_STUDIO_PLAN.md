# AI 创作工作台实施计划

> [!WARNING]
> **历史快照，不是当前事实来源。** 本文档保留阶段性产品决策与实施记录，部分模块、接口和状态描述已经过时。当前前端依赖请查阅 [frontend-api-contract.md](frontend-api-contract.md)，最终行为以代码为准。

> 状态：AI 创作工作台已进入长篇项目化写作阶段；核心链路已落地，待系统性联调、测试与体验收敛。  
> 最近更新：2026-06-01  
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
- 当前实现已从“续写/改写工具”升级为“长篇小说创作工作台”：支持创作向导、写作项目、长篇规划、章节续写、章节流水线、伏笔/状态记忆、检索、审计和润色。
- 后续优化重点不再是继续堆功能，而是围绕 **稳定性、可解释性、自动保存、流程收敛、测试覆盖、上下文质量** 做产品化。

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

### Phase 4：写作项目系统 + 深度优化 ✅

已实现：

- 写作项目：`ai_writing_projects`，保存项目名称、简介、大纲、关联档案、项目设置。
- 章节管理：`ai_chapters`，支持章节序号、标题、正文、摘要、关键事件、大纲和元数据。
- 项目状态记忆：`ai_project_states`，维护角色状态、剧情进展、世界观状态、伏笔追踪等持久上下文。
- 伏笔管理：`ai_foreshadows`，支持 pending/approaching/resolved 三态和超期/临近提醒。
- 项目级上下文构建：自动注入项目大纲、状态记忆、伏笔提醒、前章摘要、上一章末尾。
- 章节续写：`stream_chapter_continue` 基于项目上下文与章节大纲续写。
- 语义检索：`TFIDFRetriever` 零依赖 fallback，可选 `EmbeddingRetriever`。
- 内容审计增强：本地 AI 痕迹规则检测结果可注入 LLM 审计。

### Phase 5：长篇创作闭环与产品化 ✅/进行中

已实现：

- 创作向导多轮对话：支持素材收集、会话预览和一键导入项目。
- 长篇规划：按目标总字数、章节数参考和单章字数参考生成全书规划，并写入项目 `settings.longform_plan`。
- 详细章节梗概扩写：将章节概要批量扩展为 `detailed_outline`、`scene_beats`、`writing_notes`。
- 批量建章：从长篇规划创建缺失章节，保留章节元数据。
- 章节后处理：章节摘要/关键事件提取、对话润色、心理描写润色、伏笔自动回收、章节 dashboard。
- 章节 Pipeline：串联续写、润色、去 AI 味、摘要、状态更新、伏笔处理、审计、规则检测、检索索引等步骤。

进行中/待强化：

- 前端流程需要进一步收敛成“向导 → 规划 → 章节 → Pipeline → 回看/修正”的主路径，降低 10+ tabs 的认知负担。
- 长篇规划 JSON 解析、章节导入、pipeline 中间态已具备 raw output 重试导入和 metadata 展示，后续重点是浏览器联调与交互减负。
- 自动保存和断线续跑基础能力已落地：章节续写会自动写回正文，pipeline 单步状态可回看；后续可继续增强断线恢复与断点续跑。


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
- 长篇规划、详细梗概、创作向导、章节 Pipeline 相关测试。

## 14. 健壮性检查结论与优化方向（2026-06-01）

### 14.1 当前完成度

按代码现状评估：

- Phase 1-3：已完成，具备 Provider/Agent、续写/改写、草稿、文档、蒸馏、审计、模板和任务历史。
- Phase 4：已完成主体能力，具备项目化写作、章节、状态记忆、伏笔、检索和章节续写。
- Phase 5：已完成核心链路，但仍处于产品化完善阶段，重点是稳定性、流程收敛和测试覆盖。

总体判断：当前项目已经不是“AI 续写插件”，而是“本地长篇小说创作工作台”。下一步优先级应从新增功能转向闭环质量。

### 14.2 代码健壮性风险

1. **Pipeline/长篇规划仍强依赖模型严格输出 JSON/固定标记**
   - 长篇规划、详细梗概、伏笔回收、创作向导导入等逻辑依赖模型输出 JSON 或 `=== section ===`。
   - 当前已具备 fenced JSON/前后说明剥离、READY JSON fallback、伏笔回收 warning 等基础容错，但还没有“用户编辑原始输出后重试导入”的闭环。
   - 建议：统一增加 JSON 修复/二次解析层；失败时保存原始输出并允许前端手动修正后继续。

2. **Provider fallback 体验仍需可视化**
   - `OpenAICompatibleProvider` 和 `AnthropicProvider` 已限制流式失败后只做一次非流式 fallback，避免重复等待放大。
   - 风险：网关故障时用户仍只能看到前端等待，缺少“正在重试/切换非流式”的明确反馈。
   - 建议：Provider 层或 service 层透出 progress 事件，提示重试次数、fallback 状态和预计风险。

3. **流式长任务中间态恢复不足**
   - 续写的 `_smart_context` 失败已能创建并记录失败 job；长篇规划/详细梗概也会记录失败输出。
   - 风险：章节 Pipeline 多步骤输出仍需要更细粒度状态，客户端断开后无法从失败步骤继续。
   - 建议：pipeline 每步写入状态、耗时、输出摘要、warnings 和 step checkpoint。

4. **检索库与主库分离，生命周期仍需收敛**
   - `ai_retrieval.db` / `ai_retrieval_vec.db` 独立于主 SQLite。
   - 风险：章节删除、内容更新、项目删除时检索索引可能滞后；备份/迁移也容易遗漏。
   - 建议：在章节更新/删除、项目删除后统一触发重建或清理；文档说明备份需要包含检索库。

5. **创作向导默认 prompt 题材导向过强**
   - `DEFAULT_WIZARD_PROMPT` 明确偏向商业小说、虐恋甜文、反差堕落、特殊性癖等。
   - 风险：作为通用 AI 创作入口时过度窄化，影响其他题材；也可能让用户误以为系统默认鼓励特定风格。
   - 建议：拆成“通用创作向导”与“题材模板”，默认使用中性 prompt，特殊题材作为可选模板。

6. **模块复杂度集中在 `AIWritingService`**
   - `service.py` 已承担 Provider 配置、草稿、蒸馏、项目、长篇规划、chat、pipeline、润色、检索等多职责。
   - 风险：后续修改容易引入回归，测试粒度也会变粗。
   - 建议：按领域拆分为 ProviderService、DraftService、ProjectService、PlanningService、PipelineService、ChatService，先从 pipeline 和 project 拆起。

7. **长篇规划/详细梗概的数据库写入已批量化**
   - 当前已用 `list_ai_chapter_refs()` 避免读取整章正文，并新增 `update_ai_chapters_outlines_and_metadata()` 在单个事务内批量更新章节 outline 与 metadata。
   - 风险：章节很多时仍需关注单次批量事务大小；SQLite 小规模长篇项目可接受。
   - 建议：后续如继续扩展为超长篇项目，再把 settings 回写、章节 patch、检索索引刷新纳入更完整的事务/队列策略。
### 14.3 无用或可收敛实现

- `stream_plan` 与 `stream_longform_plan` 都承担“规划”职责，建议在前端明确一个是短段续写构思、一个是全书规划，避免用户混淆。
- `ai_drafts` 与 `ai_chapters.content` 都可保存 AI 正文，建议明确：草稿用于临时片段，章节用于项目正式正文；前端提供“保存到草稿/写入章节”的清晰分流。
- Prompt 模板、Agent system_prompt、内置 prompt 三套 prompt 来源并存，建议增加“实际生效 prompt 预览”，否则调试困难。
- `EmbeddingRetriever` 是可选能力，但当前创建入口默认 `use_embeddings=False`，属于潜在能力；文档和 UI 应标注“实验性/未默认启用”。

### 14.4 下一步需求优先级

P0：稳定闭环

- 长篇规划、详细梗概、伏笔回收、向导导入已支持从 raw output / job output_text 重试导入。
- pipeline 已记录顶层 status/current_step/warnings/duration，单步失败后标记 partial 并保留 warnings。
- 章节续写已支持流式自动保存，异常/断连时尽量保留 partial output。
- Provider 重试/fallback 已通过 SSE progress 明确展示给前端。

P1：体验收敛

- 首页主路径改为：创作向导 → 建项目 → 全书规划 → 生成章节 → 章节 Pipeline → 审计/修正。
- 将 Provider/Agent/Prompt 模板等设置项下沉到“设置”，减少主写作界面 tab 数。
- 当前上下文预览接口已落地，可显示本次发给模型的项目上下文、章节正文片段、章节大纲和 prompt 预览。

P2：质量提升

- 增加 Token/费用估算，生成前提示预计上下文和输出规模。
- 增加章节一致性检查：角色状态变化、伏笔状态、时间线冲突。
- 检索自动化已补齐基础生命周期：章节/项目删除会清索引，章节摘要/关键事件更新会刷新或删除索引。
- 详细梗概章节 outline/metadata 更新已改为批量写入；后续可继续评估 settings 回写与检索索引刷新是否需要纳入统一事务/队列。

P3：测试覆盖

- Provider 请求体与 SSE 解析单测。
- 加密迁移与密钥错误路径测试。
- 长篇规划 JSON 解析/归一化测试。
- 创作向导 READY JSON 解析测试。
- Pipeline 单步失败、断线取消、重试/继续测试。


### 2026-06-01（健壮性审查与需求收敛）

- 完成 AI 创作模块代码健壮性复查：当前 Phase 1-4 主体完成，Phase 5 核心链路已落地但仍需产品化闭环。
- 已修复路由层数字参数校验不一致、续写摘要失败无 job 记录、Provider fallback 重试放大、伏笔 JSON 解析静默失败等问题。
- 已优化长篇规划/详细梗概写入路径：新增章节轻量引用查询，避免为编号映射读取整章正文；详细梗概同步章节时合并 outline 与 metadata 写入；项目 settings 无变化时跳过回写。
- 需求优先级调整：下一步从新增功能转向稳定闭环、主流程收敛、上下文可解释、自动保存和测试覆盖。
- Provider 重试/fallback 已通过 SSE progress 透出：`openai_compatible` 与 `anthropic` 在 retry/fallback 时都会发送 phase/message/provider/status_code 等进度数据。
- 已补充 Provider fallback 进度事件测试，验证重试、fallback、非流式输出顺序。
- 章节续写已支持自动保存：直接章节续写会按时间/字符阈值写回 `ai_chapters.content`，失败或断连时也尽量保存 partial output；pipeline 子续写显式禁用 autosave，避免双重写回。
- Pipeline metadata 已收敛：`metadata.pipeline` 记录 `status/current_step/warnings/failed_steps/skipped_steps/duration_sec`，单步失败不中断后续步骤并标记 partial。
- Raw output 重试导入已落地：新增长篇规划、详细梗概、伏笔回收、创作向导 raw import 服务与 API，可从 `output_text/raw_output/job_id` 解析并重新应用。
- 上下文预览已落地：新增项目 context preview API，复用章节续写上下文构建与 `safe_prompt_preview()`。
- 检索生命周期已补齐：`BaseRetriever`/TF-IDF/Embedding 新增 `delete_chapter()`，项目/章节删除和章节摘要更新会同步检索索引。
- 创作向导 prompt 已拆分：`WIZARD_BASE_PROMPT` + `WIZARD_GENRE_PROMPTS` + `build_wizard_prompt()`，内置创作向导 Agent 统一使用 `DEFAULT_WIZARD_PROMPT`。
- 已新增/更新测试：`test_ai_service_stream_continue.py`、`test_ai_retrieval.py`、`test_ai_prompts.py` 等。
- 前端已接入 raw output 重试导入：长篇规划、详细梗概、伏笔回收和创作向导导入弹窗均可从原始输出恢复。
- 前端已接入章节上下文预览：章节详情页可查看 stats、项目上下文、完整上下文片段和 prompt 预览，并标识风格/小说档案注入状态。
- 前端已展示自动保存与 Pipeline 状态：章节续写完成后刷新正文并提示已自动保存；章节 dashboard 展示 pipeline status/current_step/warnings/duration/failed/skipped。
- 详细梗概同步章节已进一步批量化：`AIWritingService._apply_longform_plan_details()` 构造章节更新列表后调用 `Database.update_ai_chapters_outlines_and_metadata()`。
- `AIWritingService` 文件级拆分仍保留为后续重构项，本轮只补齐用户可见闭环和数据库批量写入。
- 已通过本地验证：`python -m pytest tests/test_ai_service_parsing.py tests/test_ai_web_int_parsing.py tests/test_ai_service_stream_continue.py tests/test_ai_providers_fallback.py tests/test_ai_retrieval.py tests/test_ai_prompts.py`。

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
