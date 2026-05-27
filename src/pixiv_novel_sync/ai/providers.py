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
        """自动检测并拼接 /v1 路径。"""
        base_url = (self.config.base_url or self.default_base_url).rstrip("/")
        # 已经包含 /v1 则不重复拼接
        if base_url.endswith("/v1") or "/v1/" in base_url:
            return base_url
        # 官方 API 地址不需要拼接（已经内置 /v1）
        official_hosts = ("api.openai.com", "api.deepseek.com", "api.x.ai", "api.anthropic.com")
        from urllib.parse import urlparse
        host = urlparse(base_url).hostname or ""
        if host in official_hosts:
            return base_url
        # 其他地址（自建网关等）自动拼接 /v1
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
        try:
            with requests.post(
                url,
                headers=headers,
                json=payload,
                stream=True,
                timeout=self.config.timeout_seconds,
                proxies=self._proxies(),
            ) as response:
                if response.status_code >= 500:
                    # 流式失败，fallback 到非流式
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
        except requests.RequestException as exc:
            raise AIProviderError(f"AI API 请求失败：{exc}") from exc

    def _non_stream_generate(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> Iterator[AIStreamChunk]:
        """非流式调用：一次性获取完整响应。"""
        payload_copy = {**payload, "stream": False}
        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload_copy,
                timeout=max(self.config.timeout_seconds, 300),  # 非流式至少 5 分钟
                proxies=self._proxies(),
            )
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
        except requests.RequestException as exc:
            raise AIProviderError(f"AI API 请求失败：{exc}") from exc


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
        try:
            with requests.post(
                url,
                headers=headers,
                json=payload,
                stream=True,
                timeout=self.config.timeout_seconds,
                proxies=self._proxies(),
            ) as response:
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
        except requests.RequestException as exc:
            raise AIProviderError(f"AI API 请求失败：{exc}") from exc


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
    try:
        payload = response.json()
        message = payload.get("error", {}).get("message") if isinstance(payload.get("error"), dict) else payload.get("error")
        if message:
            return f"AI API 返回错误 {response.status_code}：{message}"
    except ValueError:
        pass
    text = response.text[:500] if response.text else ""
    return f"AI API 返回错误 {response.status_code}：{text}"
