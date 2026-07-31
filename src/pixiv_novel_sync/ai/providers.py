from __future__ import annotations

import codecs
import ipaddress
import json
import math
import os
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from .model_catalog import (
    ModelCatalogValidationError,
    canonical_model_digest,
    normalize_model_record,
)
from .models import AIProviderConfig, AIStreamChunk, ModelListResult


class AIProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str = "unknown",
        scope: Literal["model", "provider"] = "provider",
        retry_after: float | None = None,
        finish_reason: str | None = None,
    ) -> None:
        if scope not in {"model", "provider"}:
            raise ValueError("AI Provider error scope 无效")
        if retry_after is not None:
            if isinstance(retry_after, bool) or not isinstance(retry_after, (int, float)):
                raise ValueError("retry_after 必须是非负数")
            retry_after = float(retry_after)
            if retry_after < 0 or not math.isfinite(retry_after):
                raise ValueError("retry_after 必须是非负数")
        super().__init__(_redact_secrets(str(message)))
        self.category = str(category or "unknown")
        self.scope = scope
        self.retry_after = retry_after
        self.finish_reason = finish_reason


class ProviderConfigError(ValueError):
    """Raised when a provider's ``base_url`` fails security validation."""


_MODEL_LIST_PAGE_BYTES = 4 * 1024 * 1024
_MODEL_LIST_TOTAL_BYTES = 20 * 1024 * 1024
_MODEL_LIST_MAX_PAGES = 100
_MODEL_LIST_MAX_MODELS = 5000


def _byte_limit_label(limit: int) -> str:
    mib = 1024 * 1024
    return f"{limit // mib} MiB" if limit % mib == 0 else f"{limit} 字节"


class ResponseByteBudget:
    """跨分页响应体字节预算。"""

    def __init__(self, limit: int = _MODEL_LIST_TOTAL_BYTES) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("响应体字节预算必须是正整数")
        self.limit = limit
        self.consumed = 0

    def consume(self, count: int) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("响应体字节数必须是非负整数")
        if self.consumed + count > self.limit:
            raise AIProviderError(
                f"模型目录响应累计超过 {_byte_limit_label(self.limit)} 上限"
            )
        self.consumed += count


def _allow_private_hosts() -> bool:
    return os.getenv("PIXIV_AI_ALLOW_PRIVATE_HOSTS", "").strip().lower() in {"1", "true", "yes", "on"}


def _is_blocked_ip(ip: ipaddress._BaseAddress, *, allow_private: bool) -> bool:
    """Return True if ``ip`` must never receive the decrypted API key.

    Link-local (cloud metadata 169.254.169.254, fe80::/10), multicast, reserved
    and unspecified addresses are always blocked. Loopback and private ranges are
    blocked unless the operator opts in via ``PIXIV_AI_ALLOW_PRIVATE_HOSTS`` (for
    self-hosted local/LAN model servers).
    """
    # IPv4-mapped IPv6 必须先展开，否则公网 IPv4 会被 IPv6 保留地址规则误拒。
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    if ip.is_global:
        return False
    if allow_private and (ip.is_private or ip.is_loopback):
        return False
    return True


def _normalized_ip(ip: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def _parse_provider_url(base_url: str | None) -> tuple[str, Any, str, int, ipaddress._BaseAddress | None]:
    raw = (base_url or "").strip()
    if not raw:
        raise ProviderConfigError("base_url 不能为空")
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ProviderConfigError("base_url 必须使用 http 或 https 协议")
    host = parsed.hostname
    if not host:
        raise ProviderConfigError("base_url 缺少主机名")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProviderConfigError("base_url 端口无效") from exc
    if port is None:
        port = 443 if scheme == "https" else 80
    if port == 0:
        raise ProviderConfigError("base_url 端口不能为 0")

    try:
        literal_ip: ipaddress._BaseAddress | None = _normalized_ip(ipaddress.ip_address(host))
    except ValueError:
        literal_ip = None
    return raw.rstrip("/"), parsed, host, port, literal_ip


@dataclass(frozen=True)
class _ResolvedTarget:
    url: str
    hostname: str
    port: int
    host_header: str
    ip: str


def _resolve_target(base_url: str | None) -> _ResolvedTarget:
    normalized_url, parsed, host, port, literal_ip = _parse_provider_url(base_url)
    allow_private = _allow_private_hosts()
    addresses: list[ipaddress._BaseAddress] = []

    if literal_ip is not None:
        addresses.append(literal_ip)
    else:
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            raise ProviderConfigError(f"无法解析 base_url 主机名：{host}") from exc
        for info in infos:
            try:
                address = _normalized_ip(ipaddress.ip_address(info[4][0]))
            except ValueError:
                continue
            if address not in addresses:
                addresses.append(address)

    if not addresses:
        raise ProviderConfigError(f"无法解析 base_url 主机名：{host}")
    for address in addresses:
        if _is_blocked_ip(address, allow_private=allow_private):
            raise ProviderConfigError(
                f"base_url 指向受限地址（{address}）；如需访问本机/内网模型服务，请设置 PIXIV_AI_ALLOW_PRIVATE_HOSTS=1"
            )

    scheme = parsed.scheme.lower()
    if scheme != "https" and not all(address.is_loopback for address in addresses):
        raise ProviderConfigError("base_url 必须使用 https（本机回环地址除外）")

    default_port = 443 if scheme == "https" else 80
    host_header = f"[{host}]" if ":" in host else host
    if port != default_port:
        host_header = f"{host_header}:{port}"
    return _ResolvedTarget(
        url=normalized_url,
        hostname=host,
        port=port,
        host_header=host_header,
        ip=str(addresses[0]),
    )


def _pinned_url(target: _ResolvedTarget) -> str:
    parsed = urlparse(target.url)
    ip = ipaddress.ip_address(target.ip)
    authority = f"[{ip}]" if ip.version == 6 else str(ip)
    authority = f"{authority}:{target.port}"
    if "@" in parsed.netloc:
        authority = f"{parsed.netloc.rsplit('@', 1)[0]}@{authority}"
    return parsed._replace(netloc=authority).geturl()


def _origin_prefix(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc}/"


class _PinnedHostAdapter(HTTPAdapter):
    def __init__(self, *, hostname: str, ip: str, **kwargs: Any) -> None:
        self._hostname = hostname
        self._ip = ip
        super().__init__(**kwargs)

    def build_connection_pool_key_attributes(
        self,
        request: requests.PreparedRequest,
        verify: bool | str,
        cert: Any = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        host_params["host"] = self._ip
        if host_params["scheme"] == "https":
            pool_kwargs["assert_hostname"] = self._hostname
            pool_kwargs["server_hostname"] = self._hostname
        return host_params, pool_kwargs

    def send(
        self,
        request: requests.PreparedRequest,
        stream: bool = False,
        timeout: Any = None,
        verify: bool | str = True,
        cert: Any = None,
        proxies: Any = None,
    ) -> requests.Response:
        response = super().send(
            request,
            stream=stream,
            timeout=timeout,
            verify=verify,
            cert=cert,
            proxies=proxies,
        )
        if 300 <= response.status_code < 400:
            response.close()
            raise AIProviderError(f"AI API 拒绝重定向响应 {response.status_code}")
        return response


def validate_base_url(base_url: str | None, *, resolve: bool = True) -> str:
    """Validate a provider ``base_url`` before the decrypted key is sent to it.

    Guards against SSRF / credential exfiltration (H1): rejects non-http(s)
    schemes, requires HTTPS for non-loopback hosts, and — when ``resolve`` is set
    — resolves the hostname and rejects link-local / private / loopback targets
    (unless opted in). ``resolve=True`` is also used at request time to blunt DNS
    rebinding. Returns the normalized (rstrip'd) URL.
    """
    if resolve:
        return _resolve_target(base_url).url

    normalized_url, parsed, host, _port, literal_ip = _parse_provider_url(base_url)
    host_is_loopback = (
        (literal_ip is not None and literal_ip.is_loopback)
        or host.lower() in ("localhost", "localhost.localdomain")
    )
    if parsed.scheme.lower() != "https" and not host_is_loopback:
        raise ProviderConfigError("base_url 必须使用 https（本机回环地址除外）")
    return normalized_url


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


def _cancelled_error() -> AIProviderError:
    return AIProviderError(
        "AI 请求已取消",
        category="cancelled",
        scope="model",
        finish_reason="cancelled",
    )


def _require_enabled(config: AIProviderConfig) -> None:
    if not config.enabled:
        raise AIProviderError(
            "Provider 已禁用",
            category="configuration",
            scope="provider",
        )


def _runtime_config_error(error: ProviderConfigError) -> AIProviderError:
    message = str(error)
    category = "network" if "无法解析" in message else "configuration"
    return AIProviderError(message, category=category, scope="provider")


def _check_cancelled(is_cancelled: Callable[[], bool] | None) -> None:
    if is_cancelled is not None and is_cancelled():
        raise _cancelled_error()


def _before_network_request(
    request_guard: Callable[[], None] | None,
    is_cancelled: Callable[[], bool] | None,
) -> None:
    _check_cancelled(is_cancelled)
    if request_guard is not None:
        request_guard()


def _sleep_before_retry(
    seconds: float,
    is_cancelled: Callable[[], bool] | None,
) -> None:
    _check_cancelled(is_cancelled)
    time.sleep(seconds)


def _normalize_finish_reason(reason: Any) -> tuple[str, bool]:
    if not isinstance(reason, str) or not reason.strip():
        return "missing", False
    normalized = reason.strip().lower()
    if normalized == "stop":
        return "stop", True
    if normalized in {"complete", "end_turn", "stop_sequence"}:
        return "complete", True
    if normalized in {"length", "max_tokens", "max_output_tokens"}:
        return "length", False
    if normalized in {
        "content_filter",
        "content_filtered",
        "refusal",
        "safety",
        "blocked",
    }:
        return "content_filter", False
    return "missing", False


def _completion_chunk(
    reason: Any,
    *,
    output_present: bool,
    provider_label: str,
) -> AIStreamChunk:
    finish_reason, normal = _normalize_finish_reason(reason)
    if normal:
        if not output_present:
            raise AIProviderError(
                f"{provider_label} 返回空响应",
                category="empty_response",
                scope="model",
                finish_reason=finish_reason,
            )
        return AIStreamChunk(type="done", data={"finish_reason": finish_reason})
    raise AIProviderError(
        f"{provider_label} 未正常完成（finish_reason={finish_reason}）",
        category="incomplete_response",
        scope="model",
        finish_reason=finish_reason,
    )


class AIProvider:
    def __init__(self, config: AIProviderConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self._adapter_lock = threading.Lock()
        self._pinned_adapters: dict[str, _PinnedHostAdapter] = {}

    def close(self) -> None:
        with self._adapter_lock:
            self.session.close()
            self._pinned_adapters.clear()

    def estimate_message_tokens(
        self,
        messages: list[dict[str, str]],
    ) -> int | None:
        del messages
        return None

    def stream_generate(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
        *,
        request_guard: Callable[[], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[AIStreamChunk]:
        raise NotImplementedError

    def _proxies(self) -> dict[str, str] | None:
        if not self.config.proxy:
            return None
        return {"http": self.config.proxy, "https": self.config.proxy}

    def _request(
        self,
        method: str,
        url: str,
        *,
        max_body_bytes: int | None = None,
        byte_budget: ResponseByteBudget | None = None,
        deadline: float | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        normalized_method = str(method or "").strip().upper()
        if not normalized_method:
            raise ValueError("HTTP method 不能为空")
        if max_body_bytes is not None and (
            isinstance(max_body_bytes, bool)
            or not isinstance(max_body_bytes, int)
            or max_body_bytes <= 0
        ):
            raise ValueError("max_body_bytes 必须是正整数")

        if deadline is not None and time.monotonic() >= deadline:
            raise AIProviderError("模型目录同步超过截止时间")

        target = _resolve_target(url)
        pinned_url = _pinned_url(target)
        prefix = _origin_prefix(pinned_url)
        requested_stream = bool(kwargs.get("stream", False))

        supplied_headers = kwargs.pop("headers", None) or {}
        headers = {key: value for key, value in supplied_headers.items() if key.lower() != "host"}
        headers["Host"] = target.host_header
        kwargs["allow_redirects"] = False
        kwargs["stream"] = True
        with self._adapter_lock:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AIProviderError("模型目录同步超过截止时间")
                configured_timeout = kwargs.get("timeout")
                if isinstance(configured_timeout, (int, float)):
                    kwargs["timeout"] = min(float(configured_timeout), remaining)
                elif configured_timeout is None:
                    kwargs["timeout"] = remaining
            adapter = self._pinned_adapters.get(prefix)
            if adapter is None:
                adapter = _PinnedHostAdapter(hostname=target.hostname, ip=target.ip)
                self.session.mount(prefix, adapter)
                self._pinned_adapters[prefix] = adapter
            request_method = getattr(self.session, normalized_method.lower(), None)
            if callable(request_method):
                response = request_method(pinned_url, headers=headers, **kwargs)
            else:
                response = self.session.request(
                    normalized_method,
                    pinned_url,
                    headers=headers,
                    **kwargs,
                )
        if 300 <= response.status_code < 400:
            response.close()
            raise AIProviderError(f"AI API 拒绝重定向响应 {response.status_code}")

        if max_body_bytes is not None:
            body = bytearray()
            try:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise AIProviderError("模型目录同步超过截止时间")
                    if not chunk:
                        continue
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    elif not isinstance(chunk, (bytes, bytearray)):
                        raise AIProviderError("模型目录响应包含无效字节块")
                    if len(body) + len(chunk) > max_body_bytes:
                        raise AIProviderError(
                            "模型目录单页响应超过 "
                            f"{_byte_limit_label(max_body_bytes)} 上限"
                        )
                    if byte_budget is not None:
                        byte_budget.consume(len(chunk))
                    body.extend(chunk)
            except BaseException:
                response.close()
                raise
            response._content = bytes(body)
            response._content_consumed = True
        elif not requested_stream:
            _ = response.content
        return response

    def _post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._request("POST", url, **kwargs)

    def _model_discovery_request(self) -> tuple[str, dict[str, str], str]:
        raise NotImplementedError

    @staticmethod
    def _decode_model_page(response: requests.Response) -> Mapping[str, Any]:
        try:
            text = response.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AIProviderError("模型目录响应不是有效 UTF-8") from exc
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIProviderError("模型目录响应不是有效 JSON") from exc
        if not isinstance(payload, Mapping):
            raise AIProviderError("模型目录响应必须是 JSON 对象")
        return payload

    @staticmethod
    def _model_page_cursor(
        payload: Mapping[str, Any],
    ) -> tuple[bool, str | None]:
        for marker in ("partial", "truncated"):
            if marker in payload:
                value = payload[marker]
                if not isinstance(value, bool):
                    raise AIProviderError(f"模型目录 {marker} 标记类型无效")
                if value:
                    raise AIProviderError("模型目录分页结果不完整")
        if "complete" in payload:
            complete = payload["complete"]
            if not isinstance(complete, bool):
                raise AIProviderError("模型目录 complete 标记类型无效")
            if not complete:
                raise AIProviderError("模型目录分页结果不完整")

        has_more_present = "has_more" in payload
        raw_has_more = payload.get("has_more")
        if has_more_present and not isinstance(raw_has_more, bool):
            raise AIProviderError("模型目录 has_more 必须是布尔值")

        cursors: list[str] = []
        for key in ("next", "after", "last_id"):
            if key not in payload:
                continue
            value = payload[key]
            if value is not None and not isinstance(value, str):
                raise AIProviderError("模型目录分页游标必须是字符串")
            if isinstance(value, str) and value:
                cursors.append(value)
        if len(set(cursors)) > 1:
            raise AIProviderError("模型目录分页游标字段冲突")
        cursor = cursors[0] if cursors else None

        forward_cursor = bool(payload.get("next") or payload.get("after"))
        if not has_more_present:
            has_more = bool(payload.get("next") or payload.get("after"))
        else:
            has_more = raw_has_more
        if has_more is False and forward_cursor:
            raise AIProviderError("模型目录分页状态与下一页游标冲突")
        if has_more and not cursor:
            raise AIProviderError("模型目录声明下一页但缺少分页游标")
        return has_more, cursor

    def list_models(
        self,
        *,
        on_page: Callable[[int, int], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        deadline: float | None = None,
    ) -> ModelListResult:
        url, headers, cursor_param = self._model_discovery_request()
        budget = ResponseByteBudget()
        models_by_key: dict[str, dict[str, Any]] = {}
        seen_cursors: set[str] = set()
        cursor: str | None = None
        page_count = 0

        while True:
            if is_cancelled is not None and is_cancelled():
                raise AIProviderError("模型目录发现已取消")
            request_kwargs: dict[str, Any] = {
                "headers": headers,
                "timeout": self.config.timeout_seconds,
                "proxies": self._proxies(),
            }
            if cursor is not None:
                request_kwargs["params"] = {cursor_param: cursor}
            try:
                response = self._request(
                    "GET",
                    url,
                    max_body_bytes=_MODEL_LIST_PAGE_BYTES,
                    byte_budget=budget,
                    deadline=deadline,
                    **request_kwargs,
                )
            except requests.RequestException as exc:
                raise AIProviderError(
                    f"模型目录请求失败：{_redact_secrets(str(exc))}"
                ) from exc

            with response:
                if response.status_code >= 400:
                    raise AIProviderError(
                        f"模型目录请求返回 HTTP {response.status_code}"
                    )
                payload = self._decode_model_page(response)

            raw_models = payload.get("data")
            if not isinstance(raw_models, list):
                raise AIProviderError("模型目录响应缺少模型数组 data")
            for raw_model in raw_models:
                if not isinstance(raw_model, Mapping):
                    raise AIProviderError("模型记录必须是对象")
                try:
                    model = normalize_model_record(raw_model)
                except (ModelCatalogValidationError, UnicodeError) as exc:
                    raise AIProviderError(f"模型记录无效：{exc}") from exc
                models_by_key[model["model_key"]] = model
                if len(models_by_key) > _MODEL_LIST_MAX_MODELS:
                    raise AIProviderError(
                        f"模型目录最多包含 {_MODEL_LIST_MAX_MODELS} 个模型"
                    )

            page_count += 1
            has_more, next_cursor = self._model_page_cursor(payload)
            if on_page is not None:
                on_page(page_count, len(models_by_key))
            if not has_more:
                break
            if len(models_by_key) >= _MODEL_LIST_MAX_MODELS:
                raise AIProviderError(
                    f"模型目录已达到 {_MODEL_LIST_MAX_MODELS} 个模型，不能继续分页"
                )
            if page_count >= _MODEL_LIST_MAX_PAGES:
                raise AIProviderError(
                    f"模型目录最多读取 {_MODEL_LIST_MAX_PAGES} 页"
                )
            assert next_cursor is not None
            if next_cursor in seen_cursors:
                raise AIProviderError("模型目录分页游标循环")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        models = list(models_by_key.values())
        return ModelListResult(
            models=models,
            complete=True,
            empty_authoritative=False,
            pages=page_count,
            result_digest=canonical_model_digest(models),
            partial_reason=None,
        )


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

    def _model_discovery_request(self) -> tuple[str, dict[str, str], str]:
        if not self.config.api_key:
            raise AIProviderError(
                "Provider 未配置 API key",
                category="configuration",
                scope="provider",
            )
        return (
            f"{self._resolve_base_url()}/models",
            {
                "Authorization": f"Bearer {self.config.api_key}",
                "Accept": "application/json",
            },
            "after",
        )

    def stream_generate(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
        *,
        request_guard: Callable[[], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[AIStreamChunk]:
        _require_enabled(self.config)
        if not self.config.api_key:
            raise AIProviderError(
                "Provider 未配置 API key",
                category="configuration",
                scope="provider",
            )
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
            yield from self._stream_chat_completions(
                url,
                headers,
                payload,
                request_guard=request_guard,
                is_cancelled=is_cancelled,
            )
        else:
            yield from self._non_stream_generate(
                url,
                headers,
                payload,
                request_guard=request_guard,
                is_cancelled=is_cancelled,
            )

    def _stream_chat_completions(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        *,
        request_guard: Callable[[], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[AIStreamChunk]:
        # 7.7: 尊重max_retries配置,不强制最小值3
        max_retries = max(0, self.config.max_retries)
        produced_output = False
        for attempt in range(max_retries + 1):
            try:
                _before_network_request(request_guard, is_cancelled)
                with self._post(
                    url,
                    headers=headers,
                    json=payload,
                    stream=True,
                    timeout=self.config.timeout_seconds,
                    proxies=self._proxies(),
                ) as response:
                    if response.status_code in (500, 502, 503, 504, 408, 429):
                        error = _http_provider_error(
                            response,
                            model=str(payload.get("model") or ""),
                        )
                        if attempt < max_retries:
                            _check_cancelled(is_cancelled)
                            yield _progress(
                                "retry",
                                f"流式请求返回 HTTP {response.status_code}，准备第 {attempt + 1} 次重试",
                                provider="openai_compatible",
                                status_code=response.status_code,
                                attempt=attempt + 1,
                                max_retries=max_retries,
                            )
                            _sleep_before_retry(2 ** attempt, is_cancelled)
                            continue
                        raise error
                    if response.status_code >= 400:
                        raise _http_provider_error(
                            response,
                            model=str(payload.get("model") or ""),
                        )
                    emitted_delta = False
                    finish_reason: Any = None
                    for raw_line in _iter_sse_lines(response):
                        _check_cancelled(is_cancelled)
                        if not raw_line:
                            continue
                        line = raw_line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            if not emitted_delta:
                                _check_cancelled(is_cancelled)
                                yield _progress(
                                    "fallback",
                                    "流式请求没有返回正文，切换为非流式请求",
                                    provider="openai_compatible",
                                )
                                yield from self._non_stream_generate(
                                    url,
                                    headers,
                                    payload,
                                    max_retries_override=3,
                                    request_guard=request_guard,
                                    is_cancelled=is_cancelled,
                                )
                            else:
                                _check_cancelled(is_cancelled)
                                yield _completion_chunk(
                                    finish_reason,
                                    output_present=True,
                                    provider_label="OpenAI-compatible API",
                                )
                            return
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(event, Mapping):
                            raise AIProviderError(
                                "OpenAI-compatible API 流事件必须是 JSON 对象",
                                category="invalid_response",
                                scope="provider",
                            )
                        choices = event.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        if not isinstance(choice, Mapping):
                            continue
                        if choice.get("finish_reason") is not None:
                            finish_reason = choice.get("finish_reason")
                        delta = choice.get("delta") or {}
                        if not isinstance(delta, Mapping):
                            continue
                        text = delta.get("content") or ""
                        if text:
                            emitted_delta = True
                            produced_output = True
                            _check_cancelled(is_cancelled)
                            yield AIStreamChunk(type="delta", text=text)
                    if not emitted_delta:
                        _check_cancelled(is_cancelled)
                        yield _progress(
                            "fallback",
                            "流式请求结束但没有返回正文，切换为非流式请求",
                            provider="openai_compatible",
                        )
                        yield from self._non_stream_generate(
                            url,
                            headers,
                            payload,
                            max_retries_override=3,
                            request_guard=request_guard,
                            is_cancelled=is_cancelled,
                        )
                    else:
                        _check_cancelled(is_cancelled)
                        yield _completion_chunk(
                            finish_reason,
                            output_present=True,
                            provider_label="OpenAI-compatible API",
                        )
                    return
            except ProviderConfigError as exc:
                raise _runtime_config_error(exc) from exc
            except requests.RequestException as exc:
                error = _request_provider_error(
                    exc,
                    provider_label="OpenAI-compatible API",
                )
                if produced_output:
                    # The stream already delivered partial text to the caller; retrying
                    # re-sends the whole prompt and duplicates output in the saved job.
                    raise error from exc
                if attempt < max_retries:
                    _check_cancelled(is_cancelled)
                    yield _progress(
                        "retry",
                        f"流式请求失败，准备第 {attempt + 1} 次重试",
                        provider="openai_compatible",
                        attempt=attempt + 1,
                        max_retries=max_retries,
                    )
                    _sleep_before_retry(2 ** attempt, is_cancelled)
                    continue
                raise error from exc

    def _non_stream_generate(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        max_retries_override: int | None = None,
        *,
        request_guard: Callable[[], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[AIStreamChunk]:
        """非流式调用：一次性获取完整响应。"""
        payload_copy = {**payload, "stream": False}
        # 7.7: 尊重max_retries配置,不强制最小值3
        max_retries = max(0, max_retries_override) if max_retries_override is not None else max(0, self.config.max_retries)
        for attempt in range(max_retries + 1):
            try:
                _before_network_request(request_guard, is_cancelled)
                response = self._post(
                    url,
                    headers=headers,
                    json=payload_copy,
                    timeout=self.config.timeout_seconds,
                    proxies=self._proxies(),
                )
                with response:
                    if response.status_code in (500, 502, 503, 504, 408, 429):
                        error = _http_provider_error(
                            response,
                            model=str(payload.get("model") or ""),
                        )
                        if attempt < max_retries:
                            _sleep_before_retry(2 ** attempt, is_cancelled)
                            continue
                        raise error
                    if response.status_code >= 400:
                        raise _http_provider_error(
                            response,
                            model=str(payload.get("model") or ""),
                        )
                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise AIProviderError(
                            "OpenAI-compatible API 返回无效 JSON",
                            category="invalid_response",
                            scope="provider",
                        ) from exc
                    choices = (data.get("choices") or []) if isinstance(data, Mapping) else []
                    if not choices or not isinstance(choices[0], Mapping):
                        raise AIProviderError(
                            "OpenAI-compatible API 返回空 choices",
                            category="empty_response",
                            scope="model",
                            finish_reason="missing",
                        )
                    choice = choices[0]
                    message = choice.get("message") or {}
                    text = (message.get("content") or "") if isinstance(message, Mapping) else ""
                    if not isinstance(text, str):
                        text = ""
                    if text:
                        _check_cancelled(is_cancelled)
                        yield AIStreamChunk(type="delta", text=text)
                    _check_cancelled(is_cancelled)
                    yield _completion_chunk(
                        choice.get("finish_reason"),
                        output_present=bool(text),
                        provider_label="OpenAI-compatible API",
                    )
                    return
            except ProviderConfigError as exc:
                raise _runtime_config_error(exc) from exc
            except requests.RequestException as exc:
                if attempt < max_retries:
                    _sleep_before_retry(2 ** attempt, is_cancelled)
                    continue
                raise _request_provider_error(
                    exc,
                    provider_label="OpenAI-compatible API",
                ) from exc


class XAIProvider(OpenAICompatibleProvider):
    default_base_url = "https://api.x.ai/v1"


class AnthropicProvider(AIProvider):
    default_base_url = "https://api.anthropic.com"

    def _model_discovery_request(self) -> tuple[str, dict[str, str], str]:
        if not self.config.api_key:
            raise AIProviderError(
                "Provider 未配置 API key",
                category="configuration",
                scope="provider",
            )
        base_url = (self.config.base_url or self.default_base_url).rstrip("/")
        endpoint = (
            f"{base_url}/models"
            if base_url.endswith("/v1")
            else f"{base_url}/v1/models"
        )
        return (
            endpoint,
            {
                "x-api-key": self.config.api_key,
                "anthropic-version": "2023-06-01",
                "Accept": "application/json",
            },
            "after_id",
        )

    def stream_generate(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        top_p: float,
        max_tokens: int,
        *,
        request_guard: Callable[[], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[AIStreamChunk]:
        _require_enabled(self.config)
        if not self.config.api_key:
            raise AIProviderError(
                "Provider 未配置 API key",
                category="configuration",
                scope="provider",
            )
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
            yield from self._non_stream_generate(
                url,
                headers,
                payload,
                request_guard=request_guard,
                is_cancelled=is_cancelled,
            )
            return
        # 7.7: 尊重max_retries配置,不强制最小值3
        max_retries = max(0, self.config.max_retries)
        produced_output = False
        for attempt in range(max_retries + 1):
            try:
                _before_network_request(request_guard, is_cancelled)
                with self._post(
                    url,
                    headers=headers,
                    json=payload,
                    stream=True,
                    timeout=self.config.timeout_seconds,
                    proxies=self._proxies(),
                ) as response:
                    if response.status_code in (500, 502, 503, 504, 408, 429):
                        error = _http_provider_error(response, model=model)
                        if attempt < max_retries:
                            _check_cancelled(is_cancelled)
                            yield _progress(
                                "retry",
                                f"Anthropic 流式请求返回 HTTP {response.status_code}，准备第 {attempt + 1} 次重试",
                                provider="anthropic",
                                status_code=response.status_code,
                                attempt=attempt + 1,
                                max_retries=max_retries,
                            )
                            _sleep_before_retry(2 ** attempt, is_cancelled)
                            continue
                        raise error
                    if response.status_code >= 400:
                        raise _http_provider_error(response, model=model)
                    emitted_delta = False
                    stop_reason: Any = None
                    for raw_line in _iter_sse_lines(response):
                        _check_cancelled(is_cancelled)
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
                        if not isinstance(event, Mapping):
                            raise AIProviderError(
                                "Anthropic API 流事件必须是 JSON 对象",
                                category="invalid_response",
                                scope="provider",
                            )
                        event_type = event.get("type")
                        if event_type == "message_start":
                            message = event.get("message") or {}
                            if isinstance(message, Mapping) and message.get("stop_reason") is not None:
                                stop_reason = message.get("stop_reason")
                        elif event_type == "content_block_delta":
                            delta = event.get("delta") or {}
                            text = (delta.get("text") or "") if isinstance(delta, Mapping) else ""
                            if text:
                                emitted_delta = True
                                produced_output = True
                                _check_cancelled(is_cancelled)
                                yield AIStreamChunk(type="delta", text=text)
                        elif event_type == "message_delta":
                            delta = event.get("delta") or {}
                            if isinstance(delta, Mapping) and delta.get("stop_reason") is not None:
                                stop_reason = delta.get("stop_reason")
                        elif event_type == "message_stop":
                            if not emitted_delta:
                                _check_cancelled(is_cancelled)
                                yield _progress(
                                    "fallback",
                                    "Anthropic 流式请求没有返回正文，切换为非流式请求",
                                    provider="anthropic",
                                )
                                yield from self._non_stream_generate(
                                    url,
                                    headers,
                                    payload,
                                    max_retries_override=3,
                                    request_guard=request_guard,
                                    is_cancelled=is_cancelled,
                                )
                            else:
                                _check_cancelled(is_cancelled)
                                yield _completion_chunk(
                                    stop_reason,
                                    output_present=True,
                                    provider_label="Anthropic API",
                                )
                            return
                        elif event_type == "error":
                            error = event.get("error") or {}
                            raise _event_provider_error(error, provider_label="Anthropic API")
                    if not emitted_delta:
                        _check_cancelled(is_cancelled)
                        yield _progress(
                            "fallback",
                            "Anthropic 流式请求结束但没有返回正文，切换为非流式请求",
                            provider="anthropic",
                        )
                        yield from self._non_stream_generate(
                            url,
                            headers,
                            payload,
                            max_retries_override=3,
                            request_guard=request_guard,
                            is_cancelled=is_cancelled,
                        )
                    else:
                        _check_cancelled(is_cancelled)
                        yield _completion_chunk(
                            stop_reason,
                            output_present=True,
                            provider_label="Anthropic API",
                        )
                    return
            except ProviderConfigError as exc:
                raise _runtime_config_error(exc) from exc
            except requests.RequestException as exc:
                error = _request_provider_error(
                    exc,
                    provider_label="Anthropic API",
                )
                if produced_output:
                    # Partial text already streamed to the caller; retrying would duplicate it.
                    raise error from exc
                if attempt < max_retries:
                    _check_cancelled(is_cancelled)
                    yield _progress(
                        "retry",
                        f"Anthropic 流式请求失败，准备第 {attempt + 1} 次重试",
                        provider="anthropic",
                        attempt=attempt + 1,
                        max_retries=max_retries,
                    )
                    _sleep_before_retry(2 ** attempt, is_cancelled)
                    continue
                raise error from exc

    def _non_stream_generate(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        max_retries_override: int | None = None,
        *,
        request_guard: Callable[[], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[AIStreamChunk]:
        """非流式 fallback：关闭 stream 一次性获取完整响应。"""
        payload_copy = {**payload, "stream": False}
        max_retries = max(0, max_retries_override) if max_retries_override is not None else max(3, self.config.max_retries)
        for attempt in range(max_retries + 1):
            try:
                _before_network_request(request_guard, is_cancelled)
                response = self._post(
                    url,
                    headers=headers,
                    json=payload_copy,
                    timeout=self.config.timeout_seconds,
                    proxies=self._proxies(),
                )
                with response:
                    if response.status_code in (500, 502, 503, 504, 408, 429):
                        error = _http_provider_error(
                            response,
                            model=str(payload.get("model") or ""),
                        )
                        if attempt < max_retries:
                            _sleep_before_retry(2 ** attempt, is_cancelled)
                            continue
                        raise error
                    if response.status_code >= 400:
                        raise _http_provider_error(
                            response,
                            model=str(payload.get("model") or ""),
                        )
                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise AIProviderError(
                            "Anthropic API 返回无效 JSON",
                            category="invalid_response",
                            scope="provider",
                        ) from exc
                    content_blocks = (data.get("content") or []) if isinstance(data, Mapping) else []
                    text_parts: list[str] = []
                    for block in content_blocks:
                        if not isinstance(block, Mapping):
                            continue
                        if block.get("type") == "text":
                            text = block.get("text") or ""
                            if isinstance(text, str):
                                text_parts.append(text)
                    text = "".join(text_parts)
                    if text:
                        _check_cancelled(is_cancelled)
                        yield AIStreamChunk(type="delta", text=text)
                    _check_cancelled(is_cancelled)
                    yield _completion_chunk(
                        data.get("stop_reason") if isinstance(data, Mapping) else None,
                        output_present=bool(text),
                        provider_label="Anthropic API",
                    )
                    return
            except ProviderConfigError as exc:
                raise _runtime_config_error(exc) from exc
            except requests.RequestException as exc:
                if attempt < max_retries:
                    _sleep_before_retry(2 ** attempt, is_cancelled)
                    continue
                raise _request_provider_error(
                    exc,
                    provider_label="Anthropic API",
                ) from exc


def create_provider(config: AIProviderConfig) -> AIProvider:
    provider_type = config.provider_type
    if provider_type == "openai_compatible":
        return OpenAICompatibleProvider(config)
    if provider_type == "anthropic":
        return AnthropicProvider(config)
    if provider_type == "xai":
        return XAIProvider(config)
    raise AIProviderError(
        f"不支持的 Provider 类型：{provider_type}",
        category="configuration",
        scope="provider",
    )


def _response_error_details(response: requests.Response) -> tuple[str, str]:
    # 强制按 UTF-8 解码（很多上游网关 Content-Type 不带 charset，requests 会按 latin-1 解析导致中文乱码）
    if not response.encoding or response.encoding.lower() in ("iso-8859-1", "latin-1"):
        response.encoding = "utf-8"
    detail_parts: list[str] = []
    display_message = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                for key in ("type", "code", "param", "message"):
                    value = err.get(key)
                    if isinstance(value, str) and value:
                        detail_parts.append(value)
                display_message = str(err.get("message") or err.get("type") or "")
            else:
                for value in (err, payload.get("message"), payload.get("detail")):
                    if isinstance(value, str) and value:
                        detail_parts.append(value)
                        if not display_message:
                            display_message = value
    except ValueError:
        pass
    if not display_message:
        try:
            display_message = str(response.text or "")[:500]
        except Exception:
            display_message = ""
        if display_message:
            detail_parts.append(display_message)
    safe_message = _redact_secrets(display_message)[:500]
    safe_details = _redact_secrets(" ".join(detail_parts))[:2000].lower()
    return safe_message, safe_details


def _parse_retry_after(response: requests.Response) -> float | None:
    headers = getattr(response, "headers", None) or {}
    raw_value: Any = None
    try:
        raw_value = headers.get("Retry-After") or headers.get("retry-after")
    except AttributeError:
        return None
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    value = raw_value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def _classify_provider_failure(
    status_code: int,
    details: str,
    *,
    model: str = "",
) -> tuple[str, Literal["model", "provider"]]:
    normalized = details.lower()
    model_name = model.strip().lower()
    model_tagged = "model" in normalized or bool(model_name and model_name in normalized)
    if any(
        marker in normalized
        for marker in (
            "insufficient_quota",
            "quota exhausted",
            "quota_exhausted",
            "billing quota",
            "account quota",
        )
    ) or status_code == 402:
        return "quota_exhausted", "provider"
    if status_code in {401, 403} or any(
        marker in normalized
        for marker in ("authentication_error", "invalid api key", "unauthorized")
    ):
        return "authentication", "provider"
    if status_code == 429 or any(
        marker in normalized for marker in ("rate_limit", "rate limit", "too many requests")
    ):
        return "rate_limited", "provider"
    if any(
        marker in normalized
        for marker in (
            "context_length",
            "context length",
            "maximum context",
            "max context",
            "too many tokens",
            "prompt is too long",
        )
    ):
        return "context_overflow", "model"
    if model_tagged and any(
        marker in normalized
        for marker in (
            "not found",
            "not_found",
            "does not exist",
            "unknown model",
            "unsupported model",
            "model_not_found",
        )
    ):
        return "model_not_found", "model"
    if model_tagged and any(
        marker in normalized
        for marker in ("model_timeout", "timed out", "timeout")
    ):
        return "timeout", "model"
    if model_tagged and any(
        marker in normalized
        for marker in (
            "unavailable",
            "overloaded",
            "rejected",
            "not supported",
            "unsupported",
        )
    ):
        return "model_rejected", "model"
    if status_code == 404:
        return "configuration", "provider"
    if status_code in {408, 500, 502, 503, 504} or status_code >= 500:
        return "gateway", "provider"
    if status_code >= 400:
        return "request_rejected", "provider"
    return "upstream_error", "provider"


def _http_provider_error(
    response: requests.Response,
    *,
    model: str = "",
) -> AIProviderError:
    message, details = _response_error_details(response)
    category, scope = _classify_provider_failure(
        int(response.status_code),
        details,
        model=model,
    )
    suffix = f"：{message}" if message else ""
    return AIProviderError(
        f"AI API 返回错误 {response.status_code}{suffix}",
        category=category,
        scope=scope,
        retry_after=_parse_retry_after(response),
    )


def _request_provider_error(
    error: requests.RequestException,
    *,
    provider_label: str,
) -> AIProviderError:
    message = _redact_secrets(str(error))[:500]
    normalized = message.lower()
    if isinstance(error, requests.Timeout) and "model" in normalized:
        category = "timeout"
        scope: Literal["model", "provider"] = "model"
    elif isinstance(error, requests.exceptions.SSLError):
        category = "tls"
        scope = "provider"
    else:
        category = "network"
        scope = "provider"
    return AIProviderError(
        f"{provider_label} 请求失败：{message}",
        category=category,
        scope=scope,
    )


def _event_provider_error(error: Any, *, provider_label: str) -> AIProviderError:
    if isinstance(error, Mapping):
        parts = [
            value
            for value in (error.get("type"), error.get("code"), error.get("message"))
            if isinstance(value, str) and value
        ]
    else:
        parts = [str(error)] if error else []
    details = _redact_secrets(" ".join(parts))[:2000]
    category, scope = _classify_provider_failure(0, details)
    return AIProviderError(
        f"{provider_label} 返回错误：{details or '未知错误'}",
        category=category,
        scope=scope,
    )


def _safe_http_error(response: requests.Response) -> str:
    message, _details = _response_error_details(response)
    suffix = f"：{message}" if message else ""
    return f"AI API 返回错误 {response.status_code}{suffix}"
