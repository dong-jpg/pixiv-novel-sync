from __future__ import annotations

import codecs
import json
import re
import time
from collections.abc import Iterator
from typing import Any

import requests

from .models import AIProviderConfig, AIStreamChunk


class AIProviderError(RuntimeError):
    pass


_SECRET_PATTERNS = [
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{6,}"),
    re.compile(r"sk-[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(x-api-key\"?\s*[:=]\s*\"?)[A-Za-z0-9._\-]{6,}"),
    re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?)[A-Za-z0-9._\-]{6,}"),
]


def _redact_secrets(text: str) -> str:
    """Strip credential-looking substrings from upstream error text.

    Some gateways echo the request (including the ``Authorization`` header) in
    4xx bodies. Those bodies flow into ``ai_jobs.error_message`` and the SSE
    error event, so the decrypted key could leak; redact before surfacing.
    """
    if not text:
        return text
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def _iter_sse_lines(response: requests.Response) -> Iterator[str]:
    """Yield text lines from a streaming response with correct UTF-8 decoding.

    ``requests.iter_lines(decode_unicode=True)`` decodes each network chunk
    independently, so a multi-byte UTF-8 character split across a chunk boundary
    becomes mojibake — very likely with Chinese output. We buffer raw bytes,
    split on newlines ourselves, and decode through one incremental decoder.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    for chunk in response.iter_content(chunk_size=None):
        if not chunk:
            continue
        buffer += decoder.decode(chunk)
        while True:
            idx = buffer.find("\n")
            if idx == -1:
                break
            yield buffer[:idx]
            buffer = buffer[idx + 1:]
    tail = decoder.decode(b"", final=True)
    if tail:
        buffer += tail
    if buffer:
        yield buffer


def _progress(phase: str, message: str, **data: Any) -> AIStreamChunk:
    return AIStreamChunk(type="progress", data={"phase": phase, "message": message, **data})


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
        # 7.7: 尊重max_retries配置,不强制最小值3
        max_retries = max(0, self.config.max_retries)
        last_error: str | None = None
        produced_output = False
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
                    if response.status_code in (500, 502, 503, 504, 408, 429):
                        last_error = f"HTTP {response.status_code}: {_redact_secrets(response.text[:200])}"
                        if attempt < max_retries:
                            yield _progress(
                                "retry",
                                f"流式请求返回 HTTP {response.status_code}，准备第 {attempt + 1} 次重试",
                                provider="openai_compatible",
                                status_code=response.status_code,
                                attempt=attempt + 1,
                                max_retries=max_retries,
                            )
                            time.sleep(2 ** attempt)
                            continue
                        raise AIProviderError(f"AI API 网关错误 {response.status_code}（已重试 {max_retries} 次）：{last_error}")
                    if response.status_code >= 400:
                        raise AIProviderError(_safe_http_error(response))
                    emitted_delta = False
                    for raw_line in _iter_sse_lines(response):
                        if not raw_line:
                            continue
                        line = raw_line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            if not emitted_delta:
                                yield _progress(
                                    "fallback",
                                    "流式请求没有返回正文，切换为非流式请求",
                                    provider="openai_compatible",
                                )
                                yield from self._non_stream_generate(url, headers, payload, max_retries_override=3)
                            else:
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
                            emitted_delta = True
                            produced_output = True
                            yield AIStreamChunk(type="delta", text=text)
                    if not emitted_delta:
                        yield _progress(
                            "fallback",
                            "流式请求结束但没有返回正文，切换为非流式请求",
                            provider="openai_compatible",
                        )
                        yield from self._non_stream_generate(url, headers, payload, max_retries_override=3)
                    return
            except requests.RequestException as exc:
                last_error = str(exc)
                if produced_output:
                    # The stream already delivered partial text to the caller; retrying
                    # re-sends the whole prompt and duplicates output in the saved job.
                    raise AIProviderError(f"AI API 流式中断（已输出部分内容，不再重试）：{last_error}") from exc
                if attempt < max_retries:
                    yield _progress(
                        "retry",
                        f"流式请求失败，准备第 {attempt + 1} 次重试",
                        provider="openai_compatible",
                        attempt=attempt + 1,
                        max_retries=max_retries,
                    )
                    time.sleep(2 ** attempt)
                    continue
                raise AIProviderError(f"AI API 请求失败（已重试 {max_retries} 次）：{last_error}") from exc

    def _non_stream_generate(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        max_retries_override: int | None = None,
    ) -> Iterator[AIStreamChunk]:
        """非流式调用：一次性获取完整响应。"""
        payload_copy = {**payload, "stream": False}
        # 7.7: 尊重max_retries配置,不强制最小值3
        max_retries = max(0, max_retries_override) if max_retries_override is not None else max(0, self.config.max_retries)
        last_error: str | None = None
        for attempt in range(max_retries + 1):
            try:
                response = requests.post(
                    url,
                    headers=headers,
                    json=payload_copy,
                    timeout=self.config.timeout_seconds,
                    proxies=self._proxies(),
                )
                if response.status_code in (500, 502, 503, 504, 408, 429):
                    last_error = f"HTTP {response.status_code}"
                    if attempt < max_retries:
                        time.sleep(2 ** attempt)
                        continue
                    raise AIProviderError(f"AI API 网关错误 {response.status_code}（已重试 {max_retries} 次）")
                if response.status_code >= 400:
                    raise AIProviderError(_safe_http_error(response))
                data = response.json()
                choices = data.get("choices") or []
                if not choices:
                    raise AIProviderError(f"AI API 返回空 choices（模型可能不支持此请求）: {_redact_secrets(str(data)[:200])}")
                message = choices[0].get("message", {})
                text = message.get("content") or ""
                if text:
                    yield AIStreamChunk(type="delta", text=text)
                yield AIStreamChunk(type="done")
                return
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
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
            "stream": self.config.stream_enabled,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        if not self.config.stream_enabled:
            yield from self._non_stream_generate(url, headers, payload)
            return
        # 7.7: 尊重max_retries配置,不强制最小值3
        max_retries = max(0, self.config.max_retries)
        last_error: str | None = None
        produced_output = False
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
                    if response.status_code in (500, 502, 503, 504, 408, 429):
                        last_error = f"HTTP {response.status_code}: {_redact_secrets(response.text[:200])}"
                        if attempt < max_retries:
                            yield _progress(
                                "retry",
                                f"Anthropic 流式请求返回 HTTP {response.status_code}，准备第 {attempt + 1} 次重试",
                                provider="anthropic",
                                status_code=response.status_code,
                                attempt=attempt + 1,
                                max_retries=max_retries,
                            )
                            time.sleep(2 ** attempt)
                            continue
                        raise AIProviderError(f"Anthropic API 网关错误 {response.status_code}（已重试 {max_retries} 次）：{last_error}")
                    if response.status_code >= 400:
                        raise AIProviderError(_safe_http_error(response))
                    emitted_delta = False
                    for raw_line in _iter_sse_lines(response):
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
                                emitted_delta = True
                                produced_output = True
                                yield AIStreamChunk(type="delta", text=text)
                        elif event_type == "message_stop":
                            if not emitted_delta:
                                yield _progress(
                                    "fallback",
                                    "Anthropic 流式请求没有返回正文，切换为非流式请求",
                                    provider="anthropic",
                                )
                                yield from self._non_stream_generate(url, headers, payload, max_retries_override=3)
                            else:
                                yield AIStreamChunk(type="done")
                            return
                        elif event_type == "error":
                            error = event.get("error") or {}
                            raise AIProviderError(_redact_secrets(str(error.get("message") or "Anthropic API 返回错误")))
                    if not emitted_delta:
                        yield _progress(
                            "fallback",
                            "Anthropic 流式请求结束但没有返回正文，切换为非流式请求",
                            provider="anthropic",
                        )
                        yield from self._non_stream_generate(url, headers, payload, max_retries_override=3)
                    return
            except requests.RequestException as exc:
                last_error = str(exc)
                if produced_output:
                    # Partial text already streamed to the caller; retrying would duplicate it.
                    raise AIProviderError(f"Anthropic 流式中断（已输出部分内容，不再重试）：{last_error}") from exc
                if attempt < max_retries:
                    yield _progress(
                        "retry",
                        f"Anthropic 流式请求失败，准备第 {attempt + 1} 次重试",
                        provider="anthropic",
                        attempt=attempt + 1,
                        max_retries=max_retries,
                    )
                    time.sleep(2 ** attempt)
                    continue
                raise AIProviderError(f"AI API 请求失败（已重试 {max_retries} 次）：{last_error}") from exc

    def _non_stream_generate(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        max_retries_override: int | None = None,
    ) -> Iterator[AIStreamChunk]:
        """非流式 fallback：关闭 stream 一次性获取完整响应。"""
        payload_copy = {**payload, "stream": False}
        max_retries = max(0, max_retries_override) if max_retries_override is not None else max(3, self.config.max_retries)
        for attempt in range(max_retries + 1):
            try:
                response = requests.post(
                    url,
                    headers=headers,
                    json=payload_copy,
                    timeout=self.config.timeout_seconds,
                    proxies=self._proxies(),
                )
                if response.status_code in (500, 502, 503, 504, 408, 429):
                    if attempt < max_retries:
                        time.sleep(2 ** attempt)
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
                    time.sleep(2 ** attempt)
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
            return _redact_secrets(f"AI API 返回错误 {response.status_code}：{message}")
    except ValueError:
        pass
    text = _redact_secrets(response.text[:500]) if response.text else ""
    return f"AI API 返回错误 {response.status_code}：{text}"
