from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import requests

from .models import AIProviderConfig, AIStreamChunk


class AIProviderError(RuntimeError):
    pass


class AIProvider:
    def __init__(self, config: AIProviderConfig) -> None:
        self.config = config

    def stream_generate(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
    ) -> Iterator[AIStreamChunk]:
        raise NotImplementedError

    def _proxies(self) -> dict[str, str] | None:
        if not self.config.proxy:
            return None
        return {"http": self.config.proxy, "https": self.config.proxy}


class OpenAICompatibleProvider(AIProvider):
    default_base_url = "https://api.openai.com/v1"

    def _resolve_base_url(self) -> str:
        """决定最终请求的 base URL。

        规则（优先级从高到低）：
        1. 用户已显式包含 /v1（结尾或路径中段）→ 原样使用
        2. 官方 host（api.openai.com / api.deepseek.com / api.x.ai）→ 原样使用
        3. base_url 已经有自定义路径段（path 不是空也不是 "/"）→ 视为完整路径，不追加
           如：`https://gateway.cc/codex` → `https://gateway.cc/codex/chat/completions`
        4. 否则自动拼 `/v1`（典型自建网关的根 URL）
        """
        base_url = (self.config.base_url or self.default_base_url).rstrip("/")
        if base_url.endswith("/v1") or "/v1/" in base_url:
            return base_url
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        host = parsed.hostname or ""
        official_hosts = ("api.openai.com", "api.deepseek.com", "api.x.ai", "api.anthropic.com")
        if host in official_hosts:
            return base_url
        # 已有自定义路径段（如 /codex / /api/openai 等）→ 不再拼 /v1
        path = parsed.path or ""
        if path and path not in ("", "/"):
            return base_url
        # 根 URL → 自动拼 /v1
        return f"{base_url}/v1"

    def stream_generate(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
    ) -> Iterator[AIStreamChunk]:
        if not self.config.api_key:
            raise AIProviderError("Provider 未配置 API key")
        base_url = self._resolve_base_url()
        url = f"{base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "stream": self.config.stream_enabled,
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        if self.config.stream_enabled:
            yield from self._stream_chat_completions(url, headers, payload)
        else:
            yield from self._non_stream_generate(url, headers, payload)

    def _stream_chat_completions(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        max_retries = max(1, self.config.max_retries)
        last_error: str | None = None
        for attempt in range(max_retries + 1):
            try:
                with requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    stream=True,
                    timeout=self.config.timeout_seconds,
                    proxies=self._proxies(),
                ) as response:
                    if response.status_code in (502, 503, 504, 408, 429):
                        last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                        if attempt < max_retries:
                            import time as _t
                            _t.sleep(2 ** attempt)
                            continue
                        # 重试耗尽，fallback 到非流式
                        yield from self._non_stream_generate(url, headers, payload)
                        return
                    if response.status_code >= 500:
                        # 其他 5xx，fallback 到非流式
                        yield from self._non_stream_generate(url, headers, payload)
                        return
                    if response.status_code >= 400:
                        raise AIProviderError(_safe_http_error(response))
                    response.encoding = "utf-8"
                    for raw_line in response.iter_lines(decode_unicode=True):
                        if not raw_line:
                            continue
                        line = raw_line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            yield AIStreamChunk(type="done")
                            return
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = event.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        text = delta.get("content") or ""
                        if text:
                            yield AIStreamChunk(type="delta", text=text)
                    return
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt < max_retries:
                    import time as _t
                    _t.sleep(2 ** attempt)
                    continue
                raise AIProviderError(f"AI API 请求失败（已重试 {max_retries} 次）：{last_error}") from exc

    def _non_stream_generate(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        """非流式调用：一次性获取完整响应。"""
        payload_copy = {**payload, "stream": False}
        max_retries = max(1, self.config.max_retries)
        last_error: str | None = None
        for attempt in range(max_retries + 1):
            try:
                response = requests.post(
                    url,
                    headers=headers,
                    json=payload_copy,
                    timeout=max(self.config.timeout_seconds, 300),
                    proxies=self._proxies(),
                )
                if response.status_code in (502, 503, 504, 408, 429):
                    last_error = f"HTTP {response.status_code}"
                    if attempt < max_retries:
                        import time as _t
                        _t.sleep(2 ** attempt)
                        continue
                    raise AIProviderError(f"AI API 网关错误 {response.status_code}（已重试 {max_retries} 次）")
                if response.status_code >= 400:
                    raise AIProviderError(_safe_http_error(response))
                data = response.json()
                choices = data.get("choices") or []
                if not choices:
                    raise AIProviderError(f"AI API 返回空 choices（模型可能不支持此请求）: {str(data)[:200]}")
                message = choices[0].get("message", {})
                text = message.get("content") or ""
                if text:
                    yield AIStreamChunk(type="delta", text=text)
                yield AIStreamChunk(type="done")
                return
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt < max_retries:
                    import time as _t
                    _t.sleep(2 ** attempt)
                    continue
                raise AIProviderError(f"AI API 请求失败（已重试 {max_retries} 次）：{last_error}") from exc


class XAIProvider(OpenAICompatibleProvider):
    default_base_url = "https://api.x.ai/v1"


class AnthropicProvider(AIProvider):
    default_base_url = "https://api.anthropic.com"

    def stream_generate(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
    ) -> Iterator[AIStreamChunk]:
        if not self.config.api_key:
            raise AIProviderError("Provider 未配置 API key")
        base_url = (self.config.base_url or self.default_base_url).rstrip("/")
        url = f"{base_url}/v1/messages"
        system_parts: list[str] = []
        anthropic_messages: list[dict[str, str]] = []
        for message in messages:
            role = message.get("role") or "user"
            content = message.get("content") or ""
            if role == "system":
                system_parts.append(content)
            elif role == "assistant":
                anthropic_messages.append({"role": "assistant", "content": content})
            else:
                anthropic_messages.append({"role": "user", "content": content})
        payload: dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        max_retries = max(1, self.config.max_retries)
        last_error: str | None = None
        for attempt in range(max_retries + 1):
            try:
                with requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    stream=True,
                    timeout=self.config.timeout_seconds,
                    proxies=self._proxies(),
                ) as response:
                    if response.status_code in (502, 503, 504, 408, 429):
                        last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                        if attempt < max_retries:
                            import time as _t
                            _t.sleep(2 ** attempt)
                            continue
                        # 重试耗尽，fallback 到非流式
                        yield from self._non_stream_generate(url, headers, payload)
                        return
                    if response.status_code >= 500:
                        # 其他 5xx，fallback 到非流式
                        yield from self._non_stream_generate(url, headers, payload)
                        return
                    if response.status_code >= 400:
                        raise AIProviderError(_safe_http_error(response))
                    response.encoding = "utf-8"
                    for raw_line in response.iter_lines(decode_unicode=True):
                        if not raw_line:
                            continue
                        line = raw_line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        event_type = event.get("type")
                        if event_type == "content_block_delta":
                            delta = event.get("delta") or {}
                            text = delta.get("text") or ""
                            if text:
                                yield AIStreamChunk(type="delta", text=text)
                        elif event_type == "message_stop":
                            yield AIStreamChunk(type="done")
                            return
                        elif event_type == "error":
                            error = event.get("error") or {}
                            raise AIProviderError(str(error.get("message") or "Anthropic API 返回错误"))
                    return
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt < max_retries:
                    import time as _t
                    _t.sleep(2 ** attempt)
                    continue
                raise AIProviderError(f"AI API 请求失败（已重试 {max_retries} 次）：{last_error}") from exc

    def _non_stream_generate(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        """非流式 fallback：关闭 stream 一次性获取完整响应。"""
        payload_copy = {**payload, "stream": False}
        max_retries = max(1, self.config.max_retries)
        for attempt in range(max_retries + 1):
            try:
                response = requests.post(
                    url,
                    headers=headers,
                    json=payload_copy,
                    timeout=max(self.config.timeout_seconds, 300),
                    proxies=self._proxies(),
                )
                if response.status_code in (502, 503, 504, 408, 429):
                    if attempt < max_retries:
                        import time as _t
                        _t.sleep(2 ** attempt)
                        continue
                    raise AIProviderError(f"Anthropic API 网关错误 {response.status_code}（已重试 {max_retries} 次）")
                if response.status_code >= 400:
                    raise AIProviderError(_safe_http_error(response))
                data = response.json()
                content_blocks = data.get("content") or []
                text_parts: list[str] = []
                for block in content_blocks:
                    if block.get("type") == "text":
                        text_parts.append(block.get("text") or "")
                text = "".join(text_parts)
                if text:
                    yield AIStreamChunk(type="delta", text=text)
                yield AIStreamChunk(type="done")
                return
            except requests.RequestException as exc:
                if attempt < max_retries:
                    import time as _t
                    _t.sleep(2 ** attempt)
                    continue
                raise AIProviderError(f"Anthropic API 请求失败（已重试 {max_retries} 次）：{exc}") from exc


def create_provider(config: AIProviderConfig) -> AIProvider:
    provider_type = config.provider_type
    if provider_type == "openai_compatible":
        return OpenAICompatibleProvider(config)
    if provider_type == "anthropic":
        return AnthropicProvider(config)
    if provider_type == "xai":
        return XAIProvider(config)
    raise AIProviderError(f"不支持的 Provider 类型：{provider_type}")


def _safe_http_error(response: requests.Response) -> str:
    # 强制按 UTF-8 解码（很多上游网关 Content-Type 不带 charset，requests 会按 latin-1 解析导致中文乱码）
    if not response.encoding or response.encoding.lower() in ("iso-8859-1", "latin-1"):
        response.encoding = "utf-8"
    try:
        payload = response.json()
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                message = err.get("message") or err.get("type")
            else:
                message = err or payload.get("message") or payload.get("detail")
        else:
            message = None
        if message:
            return f"AI API 返回错误 {response.status_code}：{message}"
    except ValueError:
        pass
    text = response.text[:500] if response.text else ""
    return f"AI API 返回错误 {response.status_code}：{text}"
