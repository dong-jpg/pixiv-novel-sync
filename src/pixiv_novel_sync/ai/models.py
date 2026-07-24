from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class AIProviderConfig:
    id: int
    name: str
    provider_type: str
    base_url: str | None
    api_key: str | None
    default_model: str | None
    timeout_seconds: int = 120
    max_retries: int = 2
    proxy: str | None = None
    context_window: int = 128000
    stream_enabled: bool = True
    enabled: bool = True


@dataclass(slots=True)
class AIAgentConfig:
    id: int
    name: str
    task_type: str
    provider_id: int
    model: str | None
    system_prompt: str
    temperature: float = 0.8
    top_p: float = 0.9
    max_tokens: int = 4000
    context_window: int = 16000
    enabled: bool = True


@dataclass(slots=True)
class AIStreamChunk:
    type: str
    text: str = ""
    data: dict[str, Any] | None = None


@dataclass(slots=True)
class ModelListResult:
    """Provider 模型发现的结构化结果信封。

    ``complete`` 表示分页已明确到达最后一页；``empty_authoritative`` 表示适配器
    确认该 Provider 的空目录具有权威性（默认适配器为 False）。任一字段不符合
    规格时，同步整体失败并保留旧目录，不能靠空列表长度推断。
    """

    models: list[dict[str, Any]]
    complete: bool
    empty_authoritative: bool
    pages: int
    result_digest: str
    partial_reason: str | None = None
