"""单站签到状态边界；不注册远程任务，不向上游提交签到。

原项目的 ``Runner.run(execute=False)`` 仍可写本地回执、冷却和浏览器状态。
这里的只读仅指不调用签到提交或重新登录；网关守卫只作用于自己的实例。
``daily-checkin`` 是可选部署依赖，导入本模块不会导入该项目或打开其数据。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import hashlib
import hmac
from importlib import import_module
from pathlib import Path
import re
from typing import Callable, Mapping, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import RemoteError, canonical_bytes, require_fields


CHECKIN_STATUS_KIND = "checkin.status.v1"
_REQUEST_FIELDS = frozenset({"protocol", "site_id", "config_version", "business_day", "timezone",
                             "expected_identity", "session_binding"})
_RESULT_FIELDS = frozenset({"protocol", "site_id", "business_day", "request_sha256", "status",
                            "identity_match", "evidence", "reason"})
_STATUSES = frozenset({"due", "already_done", "login_required", "uncertain", "unavailable", "expired"})
_EVIDENCE = frozenset({"live_status", "local_receipt", "none"})
_REASONS = frozenset({"ok", "login_required", "identity_mismatch", "disabled", "timezone_unverified",
                     "business_day_expired", "business_day_not_started", "ambiguous_status",
                     "receipt_date_mismatch", "unverified_identity", "site_unavailable"})
_CONFIRMED = frozenset({"success", "already"})


def _site_id(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value) is None:
        raise RemoteError("invalid_checkin_site", 400)
    return value


def _day(value: object) -> str:
    try:
        if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
            raise ValueError
        date.fromisoformat(value)
    except ValueError:
        raise RemoteError("invalid_checkin_business_day", 400) from None
    return value


def _zone(value: object) -> ZoneInfo:
    try:
        if (not isinstance(value, str) or len(value) > 64
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*", value) is None):
            raise ValueError
        return ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        raise RemoteError("invalid_checkin_timezone", 400) from None


def _tag(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise RemoteError("invalid_checkin_binding", 400)
    return value


def _version(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise RemoteError("invalid_checkin_config_version", 400)
    return value


def _identity_key(value: object) -> bytes:
    if not isinstance(value, bytes) or not 32 <= len(value) <= 128:
        raise RemoteError("invalid_checkin_identity_key", 400)
    return value


def pseudonymous_identity(key: bytes, site_id: str, uid: str) -> str:
    """配置侧生成站点隔离的账户代号；key 和原 UID 均不进入任务消息。"""
    _identity_key(key)
    _site_id(site_id)
    if (not isinstance(uid, str) or not 1 <= len(uid) <= 256
            or any(ord(char) < 32 or ord(char) == 127 for char in uid)):
        raise RemoteError("invalid_checkin_identity", 400)
    value = b"pixiv-checkin-identity-v1\0" + site_id.encode() + b"\0" + uid.encode("utf-8")
    return hmac.new(key, value, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class CheckinStatusRequest:
    protocol: int
    site_id: str
    config_version: int
    business_day: str
    timezone: str
    expected_identity: str = field(repr=False)
    session_binding: str = field(repr=False)

    def __post_init__(self):
        if type(self.protocol) is not int or self.protocol != 1:
            raise RemoteError("unsupported_checkin_protocol", 400)
        _site_id(self.site_id)
        _version(self.config_version)
        _day(self.business_day)
        _zone(self.timezone)
        _tag(self.expected_identity)
        _tag(self.session_binding)

    @classmethod
    def from_dict(cls, value: object) -> "CheckinStatusRequest":
        return cls(**require_fields(value, set(_REQUEST_FIELDS)))

    def to_dict(self) -> dict:
        return {name: getattr(self, name) for name in sorted(_REQUEST_FIELDS)}

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical_bytes(self.to_dict())).hexdigest()


@dataclass(frozen=True, slots=True)
class CheckinStatusResult:
    protocol: int
    site_id: str
    business_day: str
    request_sha256: str
    status: str
    identity_match: bool | None
    evidence: str
    reason: str

    def __post_init__(self):
        if type(self.protocol) is not int or self.protocol != 1:
            raise RemoteError("unsupported_checkin_protocol", 400)
        _site_id(self.site_id)
        _day(self.business_day)
        _tag(self.request_sha256)
        if (not isinstance(self.status, str) or self.status not in _STATUSES
                or not isinstance(self.evidence, str) or self.evidence not in _EVIDENCE
                or not isinstance(self.reason, str) or self.reason not in _REASONS
                or (self.identity_match is not None and type(self.identity_match) is not bool)):
            raise RemoteError("invalid_checkin_result", 400)
        if self.status in {"due", "already_done"}:
            live = self.identity_match is True and self.evidence == "live_status"
            cached = self.status == "already_done" and self.identity_match is None and self.evidence == "local_receipt"
            if not (live or cached) or self.reason != "ok":
                raise RemoteError("invalid_checkin_result", 400)
        if self.status == "expired" and (self.evidence != "none" or self.identity_match is not None
                                        or self.reason != "business_day_expired"):
            raise RemoteError("invalid_checkin_result", 400)

    @classmethod
    def from_dict(cls, value: object, *, request: CheckinStatusRequest | None = None) -> "CheckinStatusResult":
        result = cls(**require_fields(value, set(_RESULT_FIELDS)))
        if request is not None and (result.site_id != request.site_id
                                    or result.business_day != request.business_day
                                    or not hmac.compare_digest(result.request_sha256, request.sha256)):
            raise RemoteError("checkin_result_mismatch", 409)
        return result

    def to_dict(self) -> dict:
        return {name: getattr(self, name) for name in sorted(_RESULT_FIELDS)}


@dataclass(frozen=True, slots=True)
class CheckinSiteBinding:
    """受信任的本地配置描述；不包含站点 URL、凭据或浏览器路径。"""
    site_id: str
    config_version: int
    timezone: str
    identity: str | None = field(repr=False)
    session_binding: str = field(repr=False)
    enabled: bool = True
    timezone_verified: bool = False

    def __post_init__(self):
        _site_id(self.site_id)
        _version(self.config_version)
        _zone(self.timezone)
        if self.identity is not None:
            _tag(self.identity)
        _tag(self.session_binding)
        if type(self.enabled) is not bool or type(self.timezone_verified) is not bool:
            raise RemoteError("invalid_checkin_configuration", 400)


@dataclass(frozen=True, slots=True)
class GatewayObservation:
    """网关内部白名单投影；不得携带原始响应、UID、奖励或错误文本。"""
    state: str
    business_day: str | None
    identity_match: bool | None
    evidence: str = "none"


class CheckinStatusGateway(Protocol):
    def describe_site(self, site_id: str) -> CheckinSiteBinding: ...

    def read_status(self, request: CheckinStatusRequest) -> GatewayObservation: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _day_relation(request: CheckinStatusRequest, clock: Callable[[], datetime]) -> int:
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise RemoteError("invalid_checkin_clock", 400)
    today = now.astimezone(_zone(request.timezone)).date().isoformat()
    return (today > request.business_day) - (today < request.business_day)


def _check_binding(request: CheckinStatusRequest, binding: CheckinSiteBinding) -> str | None:
    if not isinstance(binding, CheckinSiteBinding):
        raise RemoteError("invalid_checkin_gateway", 503)
    if (binding.site_id != request.site_id or binding.config_version != request.config_version
            or binding.timezone != request.timezone
            or not hmac.compare_digest(binding.session_binding, request.session_binding)
            or (binding.identity is not None and not hmac.compare_digest(binding.identity, request.expected_identity))):
        raise RemoteError("checkin_binding_mismatch", 409)
    if not binding.enabled:
        return "disabled"
    if binding.identity is None:
        return "login_required"
    if not binding.timezone_verified:
        return "timezone_unverified"
    return None


def _result(request: CheckinStatusRequest, status: str, reason: str, *,
            identity_match: bool | None = None, evidence: str = "none") -> CheckinStatusResult:
    return CheckinStatusResult(1, request.site_id, request.business_day, request.sha256,
                               status, identity_match, evidence, reason)


def _gateway_call(method, *args):
    try:
        return method(*args)
    except RemoteError:
        raise
    except Exception:
        raise RemoteError("checkin_gateway_unavailable", 503) from None


def execute_checkin_status(request: CheckinStatusRequest | dict, gateway: CheckinStatusGateway, *,
                           clock: Callable[[], datetime] = _utc_now) -> CheckinStatusResult:
    """查询一个被冻结的站点日；跨日、身份或会话变化均不能发布成功。"""
    if not isinstance(request, CheckinStatusRequest):
        request = CheckinStatusRequest.from_dict(request)
    relation = _day_relation(request, clock)
    if relation:
        return _result(request, "expired" if relation > 0 else "unavailable",
                       "business_day_expired" if relation > 0 else "business_day_not_started")
    blocked = _check_binding(request, _gateway_call(gateway.describe_site, request.site_id))
    if blocked:
        return _result(request, "login_required" if blocked == "login_required" else "unavailable", blocked)
    observation = _gateway_call(gateway.read_status, request)
    relation = _day_relation(request, clock)
    if relation:
        return _result(request, "expired" if relation > 0 else "unavailable",
                       "business_day_expired" if relation > 0 else "business_day_not_started")
    blocked = _check_binding(request, _gateway_call(gateway.describe_site, request.site_id))
    if blocked:
        return _result(request, "login_required" if blocked == "login_required" else "unavailable", blocked)
    if (not isinstance(observation, GatewayObservation) or not isinstance(observation.state, str)
            or not isinstance(observation.evidence, str) or observation.evidence not in _EVIDENCE
            or (observation.identity_match is not None and type(observation.identity_match) is not bool)):
        raise RemoteError("invalid_checkin_gateway", 503)
    identity, evidence = observation.identity_match, observation.evidence
    if observation.state in {"expired", "not_due"}:
        return _result(request, "expired" if observation.state == "expired" else "unavailable",
                       "business_day_expired" if observation.state == "expired" else "business_day_not_started")
    if identity is False or observation.state == "identity_mismatch":
        return _result(request, "login_required", "identity_mismatch", identity_match=False)
    if observation.state == "needs_login":
        return _result(request, "login_required", "login_required", identity_match=identity)
    if observation.business_day != request.business_day:
        return _result(request, "uncertain", "receipt_date_mismatch", identity_match=identity)
    if observation.state in _CONFIRMED | {"ready", "due"}:
        confirmed = observation.state in _CONFIRMED
        valid = identity is True and evidence == "live_status"
        valid = valid or (confirmed and identity is None and evidence == "local_receipt")
        if valid:
            return _result(request, "already_done" if confirmed else "due", "ok",
                           identity_match=identity, evidence=evidence)
        return _result(request, "uncertain", "unverified_identity", identity_match=identity)
    if observation.state in {"pending", "uncertain", "unknown"}:
        return _result(request, "uncertain", "ambiguous_status", identity_match=identity, evidence=evidence)
    return _result(request, "unavailable", "site_unavailable", identity_match=identity, evidence=evidence)


class _GuardAbort(RuntimeError):
    pass


@dataclass(slots=True)
class _ReadGuard:
    gateway: "DailyCheckinRunnerGateway"
    request: CheckinStatusRequest
    violation: bool = False
    binding_changed: bool = False
    day_relation: int = 0
    identity_match: bool | None = None
    status_state: str | None = None
    status_day: str | None = None

    def check(self):
        relation = _day_relation(self.request, self.gateway.clock)
        if relation:
            self.day_relation = relation
            raise _GuardAbort("checkin_business_day_changed")
        try:
            blocked = _check_binding(self.request, self.gateway.describe_site(self.request.site_id))
            if blocked:
                raise RemoteError("checkin_binding_mismatch", 409)
        except RemoteError:
            self.binding_changed = True
            raise _GuardAbort("checkin_binding_mismatch") from None

    def forbid(self):
        # 记录独立于异常传播；原 Runner 吞掉异常后也不能伪造正常结果。
        self.violation = True
        raise _GuardAbort("checkin_mutation_blocked")


class _ReadOnlyTransport:
    def __init__(self, transport, guard: _ReadGuard):
        self._transport, self._guard = transport, guard

    def request(self, method, path, **kwargs):
        self._guard.check()
        if not isinstance(method, str) or method.upper() not in {"GET", "HEAD"}:
            self._guard.forbid()
        if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//")
                or "\\" in path or any(ord(char) < 32 or ord(char) == 127 for char in path)):
            self._guard.forbid()
        return self._transport.request(method, path, **kwargs)

    def fresh_login(self, *args, **kwargs):
        self._guard.forbid()

    def detect_challenge(self):
        self._guard.forbid()


class _ReadOnlyTransportFactory:
    def __init__(self, factory, guard: _ReadGuard):
        self._factory, self._guard = factory, guard

    @contextmanager
    def open(self, site):
        self._guard.check()
        if site.id != self._guard.request.site_id:
            self._guard.binding_changed = True
            raise _GuardAbort("checkin_binding_mismatch")
        with self._factory.open(site) as transport:
            self._guard.check()
            yield _ReadOnlyTransport(transport, self._guard)


class _ReadOnlyAdapter:
    def __init__(self, adapter, guard: _ReadGuard):
        self._adapter, self._guard = adapter, guard

    def identity(self):
        self._guard.check()
        result = self._adapter.identity()
        actual = pseudonymous_identity(self._guard.gateway.identity_key, self._guard.request.site_id,
                                      getattr(result, "uid", None))
        self._guard.identity_match = hmac.compare_digest(actual, self._guard.request.expected_identity)
        if not self._guard.identity_match:
            raise _GuardAbort("checkin_identity_mismatch")
        return result

    def status(self, day):
        self._guard.check()
        if day != self._guard.request.business_day or self._guard.identity_match is not True:
            raise _GuardAbort("checkin_status_before_identity")
        result = self._adapter.status(day)
        self._guard.status_state = getattr(result, "state", None)
        self._guard.status_day = getattr(result, "day", None)
        return result

    def submit(self, *args, **kwargs):
        self._guard.forbid()


class DailyCheckinRunnerGateway:
    """仅通过原项目公共 Runner 运行；实例私有守卫不修改全局网络行为。

    ``session_bindings`` 必须由部署方维护，每个 profile 登录会话代次改变时更新。
    此类不扫描 profile、不提取 Cookie，也不替部署方建立会话权威或备份策略。
    """

    def __init__(self, *, store, transport_factory, identity_key: bytes,
                 session_bindings: Mapping[str, str], clock: Callable[[], datetime] = _utc_now,
                 runner_factory=None, adapter_factory=None):
        self.store, self.transport_factory = store, transport_factory
        self.identity_key = _identity_key(identity_key)
        self.session_bindings, self.clock = session_bindings, clock
        self._runner_factory, self._adapter_factory = runner_factory, adapter_factory

    def describe_site(self, site_id: str) -> CheckinSiteBinding:
        _site_id(site_id)
        try:
            site = self.store.get_site(site_id)
            session = self.session_bindings[site_id]
        except KeyError:
            raise RemoteError("checkin_site_not_configured", 404) from None
        except Exception:
            raise RemoteError("checkin_local_state_unavailable", 503) from None
        identity = pseudonymous_identity(self.identity_key, site.id, site.expected_uid) if site.expected_uid else None
        return CheckinSiteBinding(site.id, site.version, site.timezone, identity, session,
                                  site.enabled, site.timezone_verified)

    def _factories(self):
        try:
            runner = self._runner_factory or import_module("daily_checkin.runner").Runner
            adapter = self._adapter_factory or import_module("daily_checkin.adapters").SiteAdapter
            return runner, adapter
        except (ImportError, AttributeError):
            raise RemoteError("checkin_dependency_unavailable", 503) from None

    def read_status(self, request: CheckinStatusRequest) -> GatewayObservation:
        if not isinstance(request, CheckinStatusRequest):
            request = CheckinStatusRequest.from_dict(request)
        guard = _ReadGuard(self, request)
        runner_factory, adapter_factory = self._factories()
        results = None

        def fixed_adapter(site, transport):
            guard.check()
            return _ReadOnlyAdapter(adapter_factory(site, transport), guard)

        try:
            guard.check()
            runner = runner_factory(self.store, _ReadOnlyTransportFactory(self.transport_factory, guard),
                                    solver=None, clock=self.clock, adapter_factory=fixed_adapter)
            results = runner.run([request.site_id], execute=False)
        except Exception:
            # 项目错误内容可能含站点响应或路径；只保留下面的固定分类。
            pass
        if guard.violation:
            raise RemoteError("checkin_mutation_blocked", 403)
        if guard.binding_changed:
            raise RemoteError("checkin_binding_mismatch", 409)
        if guard.identity_match is False:
            return GatewayObservation("identity_mismatch", request.business_day, False)
        relation = guard.day_relation or _day_relation(request, self.clock)
        if relation:
            return GatewayObservation("expired" if relation > 0 else "not_due", request.business_day, None)
        if not isinstance(results, list) or len(results) != 1:
            return GatewayObservation("failed", request.business_day, guard.identity_match)
        result = results[0]
        state, day = getattr(result, "state", None), getattr(result, "day", None)
        if getattr(result, "site_id", None) != request.site_id or not isinstance(state, str):
            raise RemoteError("invalid_checkin_gateway", 503)
        evidence = "live_status" if guard.status_state is not None else "none"
        if state in _CONFIRMED:
            if evidence == "live_status":
                if guard.status_state not in _CONFIRMED or guard.status_day != request.business_day:
                    state, evidence = "uncertain", "none"
            else:
                site = self.store.get_site(request.site_id)
                receipt = self.store.receipt(request.site_id, site.expected_uid, request.business_day)
                if (isinstance(receipt, dict) and receipt.get("state") in _CONFIRMED
                        and receipt.get("site_id") == request.site_id and receipt.get("uid") == site.expected_uid
                        and receipt.get("day") == day == request.business_day):
                    evidence = "local_receipt"
                else:
                    state = "uncertain"
        return GatewayObservation(state, day, guard.identity_match, evidence)


@dataclass(frozen=True, slots=True)
class CheckinLocalConfig:
    """受信任的部署配置，绝不能从远程任务正文反序列化。"""
    data_dir: Path
    identity_key: bytes = field(repr=False)
    session_bindings: Mapping[str, str] = field(repr=False)

    def __post_init__(self):
        if (not isinstance(self.data_dir, Path) or not self.data_dir.is_absolute()
                or self.data_dir.parent == self.data_dir):
            raise RemoteError("invalid_checkin_data_directory", 400)
        _identity_key(self.identity_key)
        if not isinstance(self.session_bindings, Mapping) or not self.session_bindings:
            raise RemoteError("invalid_checkin_configuration", 400)
        for site_id, session in self.session_bindings.items():
            _site_id(site_id)
            _tag(session)


def create_local_gateway(config: CheckinLocalConfig, *,
                         clock: Callable[[], datetime] = _utc_now) -> DailyCheckinRunnerGateway:
    """部署方显式调用后才初始化私有项目状态；不搜索环境、路径或登录资料。"""
    if not isinstance(config, CheckinLocalConfig):
        raise RemoteError("invalid_checkin_configuration", 400)
    try:
        store_type = import_module("daily_checkin.storage").Store
        browser_type = import_module("daily_checkin.browser").BrowserFactory
    except (ImportError, AttributeError):
        raise RemoteError("checkin_dependency_unavailable", 503) from None
    try:
        store, browser = store_type(config.data_dir), browser_type(config.data_dir)
    except Exception:
        raise RemoteError("checkin_local_state_unavailable", 503) from None
    return DailyCheckinRunnerGateway(store=store, transport_factory=browser, identity_key=config.identity_key,
                                    session_bindings=config.session_bindings, clock=clock)
