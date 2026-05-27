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
