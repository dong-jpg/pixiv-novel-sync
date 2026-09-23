from __future__ import annotations

import json
import re
import sqlite3
import time
import unicodedata
from collections.abc import Iterator, Mapping
from typing import Any

from ...storage.ai.core import (
    AIJobConflictError,
    AIProviderReferenceError,
)
from ...storage_db import Database
from ..model_catalog import (
    ModelCatalogConflictError,
    ModelCatalogValidationError,
    normalize_capabilities,
)
from ..model_pools import (
    ModelPoolConflictError,
    ModelPoolValidationError,
    expand_pool_ids,
)
from ..model_router import (
    MAX_CANDIDATE_ATTEMPTS,
    MAX_NETWORK_REQUESTS,
    MAX_POOL_NODES,
    MAX_RESOLVED_CANDIDATES,
    CandidateSnapshot,
    ModelRouteConflictError,
    ModelRouter,
)
from ..model_sync import ModelSyncConflictError
from ..models import AIAgentConfig, AIProviderConfig, AIStreamChunk
from ..providers import ProviderConfigError, create_provider, validate_base_url
from ..prompts import DEFAULT_KEYWORD_CLEAN_PROMPT
from .core import (
    AINotFoundError,
    AIConflictError,
    AIServiceError,
)


_MANUAL_MODEL_CREATE_FIELDS = {
    "model_key",
    "enabled",
    "manual_display_name",
    "manual_capabilities",
    "manual_context_window",
}
_MANUAL_MODEL_UPDATE_FIELDS = {
    "enabled",
    "manual_display_name",
    "manual_capabilities",
    "manual_context_window",
}
_MODEL_POOL_FIELDS = {
    "name",
    "description",
    "pool_kind",
    "fallback_pool_id",
    "enabled",
}
_RESUME_FIELDS = {
    "parent_job_id",
    "idempotency_key",
    "candidate_snapshot_hash",
    "resume_candidate_index",
}
_RESUME_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_FORBIDDEN_AGENT_POLICY_FIELDS = frozenset(
    {
        "policy_id",
        "policy_text",
        "output_schema",
        "safety_policy_hash",
        "validator_policy_hash",
        "binding_version",
    }
)


class AIAdminMixin:
    @staticmethod
    def _resume_request_payload(
        parent_job_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise AIServiceError("请求体必须是 JSON 对象")
        data = dict(payload)
        unknown = sorted(set(data) - _RESUME_FIELDS)
        if unknown:
            raise AIServiceError(f"不允许提交字段：{', '.join(unknown)}")
        if set(data) != _RESUME_FIELDS:
            missing = sorted(_RESUME_FIELDS - set(data))
            raise AIServiceError(f"继续请求缺少字段：{', '.join(missing)}")
        if not isinstance(parent_job_id, str) or not parent_job_id:
            raise AIServiceError("父 AI job 标识无效")
        body_parent = data["parent_job_id"]
        if body_parent != parent_job_id:
            raise AIServiceError("路径中的父 AI job 与请求体不一致")
        if (
            not isinstance(body_parent, str)
            or len(body_parent) > 128
            or any(ord(character) < 0x20 for character in body_parent)
        ):
            raise AIServiceError("父 AI job 标识无效")
        idempotency_key = data["idempotency_key"]
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.isascii()
            or not (16 <= len(idempotency_key) <= 128)
            or any(not (0x21 <= ord(character) <= 0x7E) for character in idempotency_key)
        ):
            raise AIServiceError("idempotency_key 必须是 16-128 位 ASCII 字符")
        snapshot_hash = data["candidate_snapshot_hash"]
        if (
            not isinstance(snapshot_hash, str)
            or _RESUME_HASH_PATTERN.fullmatch(snapshot_hash) is None
        ):
            raise AIServiceError("candidate_snapshot_hash 必须是 64 位小写十六进制摘要")
        resume_index = data["resume_candidate_index"]
        if (
            isinstance(resume_index, bool)
            or not isinstance(resume_index, int)
            or resume_index < 0
        ):
            raise AIServiceError("resume_candidate_index 必须是非负整数")
        return {
            "parent_job_id": body_parent,
            "idempotency_key": idempotency_key,
            "candidate_snapshot_hash": snapshot_hash,
            "resume_candidate_index": resume_index,
        }

    @staticmethod
    def _resume_next_candidate_index(
        snapshot: CandidateSnapshot,
        attempts: list[dict[str, Any]],
    ) -> int:
        attempted_indices: list[int] = []
        for attempt in attempts:
            if attempt.get("stage") != "main":
                continue
            if attempt.get("status") == "running":
                raise AIConflictError("父 AI job 仍有未完成的模型尝试")
            candidate_hash = attempt.get("candidate_list_hash")
            if candidate_hash != snapshot.snapshot_hash:
                raise AIConflictError("父 AI job 的尝试记录不属于当前候选快照")
            provider_id = attempt.get("provider_id")
            model_key = attempt.get("model_key")
            provider_model_id = attempt.get("provider_model_id")
            matches = [
                candidate
                for candidate in snapshot.candidates
                if candidate.provider_id == provider_id
                and candidate.model_key == model_key
                and (
                    provider_model_id is None
                    or candidate.provider_model_id == provider_model_id
                )
            ]
            if len(matches) != 1:
                raise AIConflictError("父 AI job 的尝试无法匹配候选快照")
            attempted_indices.append(matches[0].candidate_index)
        next_index = max(attempted_indices, default=-1) + 1
        if next_index >= len(snapshot.candidates):
            raise AIConflictError("候选快照没有未尝试的模型")
        return next_index

    def _replay_resume_child(
        self,
        db: Database,
        child_id: str,
    ) -> Iterator[AIStreamChunk]:
        child = db.get_ai_job(child_id)
        if child is None:
            raise AIConflictError("继续任务 child job 不存在")
        return self._stream_replayed_route_job(child)

    def stream_job_with_next_model(
        self,
        job_id: str,
        payload: Mapping[str, Any],
    ) -> Iterator[AIStreamChunk]:
        """在返回 SSE 前校验并准备手动候选继续任务。"""

        request_data = self._resume_request_payload(job_id, payload)
        db = self._db()
        try:
            parent = db.get_ai_job(job_id)
            if parent is None:
                raise AINotFoundError("父 AI job 不存在")
            if parent.get("status") not in {
                "succeeded",
                "failed",
                "partial",
                "cancelled",
            }:
                raise AIConflictError("父 AI job 尚未进入终态")
            if parent.get("candidate_snapshot_hash") != request_data[
                "candidate_snapshot_hash"
            ]:
                raise AIConflictError("候选快照摘要不匹配")
            snapshot_payload = parent.get("candidate_snapshot")
            if not isinstance(snapshot_payload, Mapping):
                raise AIConflictError("父 AI job 缺少候选快照")
            try:
                snapshot = ModelRouter.candidate_snapshot_from_payload(
                    snapshot_payload,
                    request_data["candidate_snapshot_hash"],
                )
            except ModelRouteConflictError as exc:
                raise AIConflictError(str(exc)) from exc
            expected_index = self._resume_next_candidate_index(
                snapshot,
                parent.get("attempts") or [],
            )
            if request_data["resume_candidate_index"] != expected_index:
                raise AIConflictError(
                    f"resume_candidate_index 必须是 {expected_index}"
                )

            # main 分支已剥离写作流，任何历史任务都没有可恢复的流式生成处理器。
            raise AIConflictError("该任务类型暂不支持手动候选继续")
        except AIJobConflictError as exc:
            raise AIConflictError(str(exc)) from exc
        finally:
            db.close()

    @staticmethod
    def _reject_unknown_fields(
        payload: dict[str, Any],
        allowed: set[str],
    ) -> None:
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise AIServiceError(f"不允许提交字段：{', '.join(unknown)}")

    @staticmethod
    def _require_boolean(payload: dict[str, Any], field: str) -> None:
        if field in payload and not isinstance(payload[field], bool):
            raise AIServiceError(f"{field} 必须是布尔值")

    @staticmethod
    def _reject_control_text(value: Any, field: str, max_length: int) -> str:
        if not isinstance(value, str):
            raise AIServiceError(f"{field} 必须是字符串")
        if any(unicodedata.category(character) == "Cc" for character in value):
            raise AIServiceError(f"{field} 不能包含控制字符")
        if len(value) > max_length:
            raise AIServiceError(f"{field} 不能超过 {max_length} 个字符")
        return value

    @staticmethod
    def _expected_version(payload: dict[str, Any]) -> int:
        value = payload.get("expected_version")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AIServiceError("expected_version 必须是非负整数")
        return value

    @classmethod
    def _normalize_model_pool_payload(
        cls,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = dict(payload)
        if "name" in normalized:
            normalized["name"] = cls._reject_control_text(
                normalized["name"],
                "name",
                100,
            )
        if "description" in normalized:
            normalized["description"] = cls._reject_control_text(
                normalized["description"],
                "description",
                2000,
            )
        if "fallback_pool_id" in normalized:
            fallback_id = normalized["fallback_pool_id"]
            if fallback_id is not None and (
                isinstance(fallback_id, bool)
                or not isinstance(fallback_id, int)
                or fallback_id <= 0
            ):
                raise AIServiceError("fallback_pool_id 必须是正整数或 null")
        return normalized

    @staticmethod
    def _require_provider_row(db: Database, provider_id: int) -> dict[str, Any]:
        provider = db.get_ai_provider(provider_id)
        if provider is None:
            raise AINotFoundError("Provider 不存在")
        return provider

    @staticmethod
    def _require_provider_model_row(db: Database, model_id: int) -> dict[str, Any]:
        model = db.get_ai_provider_model(model_id)
        if model is None:
            raise AINotFoundError("Provider 模型不存在")
        return model

    @staticmethod
    def _require_model_pool_row(db: Database, pool_id: int) -> dict[str, Any]:
        pool = db.get_ai_model_pool(pool_id)
        if pool is None:
            raise AINotFoundError("模型池不存在")
        return pool

    @staticmethod
    def provider_config_lint(
        provider: Mapping[str, Any],
        *,
        bound_agent_count: int = 0,
        routable_models: int = 0,
        pool_referenced: bool = False,
    ) -> list[dict[str, str]]:
        """只看 Provider 行本身就能下的结论，零网络。

        第一条判据刻意调用运行时那个 validate_base_url（resolve=False 跳过 DNS）：
        结论由构造保证与运行时一致。另写一份 scheme 规则必然与 providers.py 漂移，
        届时横幅报「健康」而任务照样失败——比没有横幅更坏。

        base_url 允许留空（表示用适配器默认地址），此时跳过这条判据。

        disabled_but_bound 定为 will_fail 而非 warn，依据是路由的真实行为：
        model_router.py:_provider_row 对已禁用 Provider 直接抛
        ModelRouteError("Provider 已禁用")，固定绑定没有任何降级余地。
        bound_agent_count 只数固定绑定（池绑定的 provider_id 为 NULL，
        见 ai_agents 的 CHECK），所以数出来的每个 Agent 都是真的会失败。

        「停着的」Provider 不算「坏的」：已停用、且没有任何东西依赖它时，
        缺 Key 与空目录都不报。catalog.py:155 对已停用 Provider 把 routable 硬置 0，
        不豁免的话每个备用 Provider 都会常驻一条警告加一条 will_fail，
        横幅上的黄色就不再稀有——同 CLAUDE.md 对 partial 状态的态度。
        base_url 与 disabled_but_bound 不在豁免范围内：前者是配置本身写错了，
        后者恰恰以「有人绑着」为前提。

        pool_referenced 与 bound_agent_count 回答的是**两个不同的问题**，
        所以是两个参数而不是一个合计：

        - bound_agent_count 问「有多少 Agent 必然随它一起失败」，只数固定绑定。
          disabled_but_bound 是 will_fail，这一档要求「必然失败」，只有固定绑定
          给得起这个保证；池绑定在池里还有别的可路由成员时会优雅降级（运行时
          model_router.py:508-513 逐成员挑），把池绑定算进这个计数会造出假红。
        - pool_referenced 问「有没有任何东西依赖它」，池引用满足这个。停用一个
          被池引用的 Provider 不是「停着」，是把池里的成员抽走了：
          catalog.py:76 的 routable 因 provider_enabled 为假而全灭，运行时
          model_router.py:551-552 抛 ModelRouteError("模型池没有可用模型")。
          豁免它就会让这种事故形状全绿——假绿比没有横幅更坏。
        """
        findings: list[dict[str, str]] = []
        base_url = provider.get("base_url")
        if base_url:
            try:
                validate_base_url(str(base_url), resolve=False)
            except (ProviderConfigError, ValueError) as exc:
                # 兜到裸 ValueError 是必要的：_parse_provider_url 的 urlparse(raw)
                # 在任何 try 之外（providers.py:123），IPv6 括号不配对（http://[::1）
                # 会抛 ValueError("Invalid IPv6 URL")。健康投影把整个 ai_health()
                # 包在 except Exception 里，漏一个就是整条横幅消失——恰好是配置坏掉、
                # 最需要它说话的时候。裸异常的消息是英文的，不能直接进中文界面。
                # ProviderConfigError 目前继承自 ValueError，写成元组是为了它哪天
                # 改了基类也不至于漏掉。
                message = (
                    str(exc)
                    if isinstance(exc, ProviderConfigError)
                    else "base_url 格式非法，无法解析"
                )
                findings.append(
                    {"level": "will_fail", "code": "base_url", "message": message}
                )
        parked = (
            not provider.get("enabled")
            and not bound_agent_count
            and not pool_referenced
        )
        if not parked and not provider.get("has_api_key"):
            findings.append(
                {"level": "will_fail", "code": "api_key", "message": "未保存 API Key"}
            )
        if not provider.get("enabled") and bound_agent_count:
            findings.append({
                "level": "will_fail",
                "code": "disabled_but_bound",
                "message": (
                    f"Provider 已停用，绑在这里的 {bound_agent_count} 个 Agent 会直接失败"
                ),
            })
        if not parked and not routable_models:
            findings.append({
                "level": "warn",
                "code": "no_routable_model",
                "message": "目录里没有可路由模型，模型池选不出成员",
            })
        return findings

    def ai_health(self, *, days: int = 7) -> dict[str, Any]:
        """AI 配置的只读健康投影。零网络：不发起任何 Provider 请求。

        投影字段是白名单挑出来的，不是把库里的行往外递：list_ai_providers() 是
        SELECT *，带着 base_url / proxy / models_sync_owner。这个横幅常驻三个设置页，
        没有任何理由让它复述连接地址（见 spec 的响应约定）。
        """
        db = self._db()
        try:
            # 整块读包进一个 read_transaction：并发的模型同步在两次读之间提交，
            # 就会产出 models_synced_at 与 routable_models 互不匹配的一行。
            # （read_transaction 可嵌套，list_ai_provider_models 内部那层照旧。）
            with db.read_transaction():
                providers = db.list_ai_providers()
                agents = db.list_ai_agents()
                pools = {int(p["id"]): p for p in db.list_ai_model_pools()}
                attempt_health = db.get_provider_attempt_health(days=days)
                job_failures = db.get_ai_job_failure_summary(days=days)
                routable: dict[int, int] = {}
                # provider_model_id -> routable，供池成员过滤复用运行时判据。
                # 不额外查库：这里本来就要为每个 Provider 调一次，只是把 items 也留下。
                routable_by_model: dict[int, bool] = {}
                for provider_row in providers:
                    catalog = db.list_ai_provider_models(int(provider_row["id"]))
                    routable[int(provider_row["id"])] = int(catalog["routable"])
                    for item in catalog["items"]:
                        routable_by_model[int(item["id"])] = bool(item["routable"])
        finally:
            db.close()

        # 只数固定绑定：池绑定的 provider_id 为 NULL（ai_agents 的 CHECK），
        # 所以这里数出来的每个 Agent 都真的会随该 Provider 一起失败。
        bound: dict[int, int] = {}
        # 「经由模型池被依赖」的 Provider：单独一个集合，只喂给 parked 豁免判定，
        # 不并进 bound_agent_count（理由见 provider_config_lint 的 docstring）。
        pool_referenced: set[int] = set()
        for agent in agents:
            provider_id = agent.get("provider_id")
            if agent.get("binding_type") == "fixed" and provider_id:
                bound[int(provider_id)] = bound.get(int(provider_id), 0) + 1
            elif agent.get("binding_type") == "pool":
                try:
                    chain = expand_pool_ids(int(agent.get("model_pool_id") or 0), pools)
                except ModelPoolValidationError:
                    # 池不存在或后备链成环：这个 Agent 无论如何都跑不通，
                    # 也就谈不上"依赖某个 Provider"，跳过。
                    continue
                for pool_id in chain:
                    pool = pools.get(pool_id) or {}
                    if not bool(pool.get("enabled")):
                        continue
                    for member in pool.get("members", []):
                        if not member.get("enabled"):
                            continue
                        member_provider_id = member.get("provider_id")
                        if member_provider_id:
                            # 刻意不要求 routable：Provider 一停用，它名下每个模型的
                            # routable 就是假（catalog.py:76 的 provider_enabled），
                            # 拿它当条件会自相引用 —— 停用后立刻"没人依赖"，
                            # parked 永久豁免，正是要堵的那个洞。
                            pool_referenced.add(int(member_provider_id))

        provider_status: dict[int, str] = {}
        provider_items: list[dict[str, Any]] = []
        for row in providers:
            # 已停用的 Provider 照样列出：它是否还有 Agent 绑着，是这个视图最重要的事实，
            # 按 enabled 过滤恰好会藏起 2026-09-03 那次事故的形状。
            provider_id = int(row["id"])
            findings = self.provider_config_lint(
                row,
                bound_agent_count=bound.get(provider_id, 0),
                routable_models=routable.get(provider_id, 0),
                pool_referenced=provider_id in pool_referenced,
            )
            status = (
                "will_fail" if any(f["level"] == "will_fail" for f in findings)
                else ("warn" if findings else "healthy")
            )
            provider_status[provider_id] = status
            provider_items.append({
                "id": provider_id,
                "name": row.get("name"),
                "enabled": bool(row.get("enabled")),
                "status": status,
                "findings": findings,
                "bound_agent_count": bound.get(provider_id, 0),
                "routable_models": routable.get(provider_id, 0),
                "models_synced_at": row.get("models_synced_at"),
                "models_sync_error": row.get("models_sync_error"),
                "attempts": attempt_health.get(provider_id),
            })

        agent_items = [
            self._agent_health_item(agent, provider_status, pools, routable_by_model)
            for agent in agents
        ]
        unhealthy = sum(1 for item in agent_items if item["status"] == "will_fail")
        return {
            "window_days": int(days),
            "providers": provider_items,
            "agents": agent_items,
            "ai_job_failures": job_failures,
            "totals": {
                "providers": len(provider_items),
                "providers_will_fail": sum(
                    1 for item in provider_items if item["status"] == "will_fail"
                ),
                "routable_models": sum(routable.values()),
                "agents": len(agent_items),
                "agents_unhealthy": unhealthy,
            },
        }

    @staticmethod
    def _agent_health_item(
        agent: Mapping[str, Any],
        provider_status: Mapping[int, str],
        pools: Mapping[int, Mapping[str, Any]],
        routable_by_model: Mapping[int, bool],
    ) -> dict[str, Any]:
        """Agent 的状态是**继承**来的：它自己没坏，是它绑的东西坏了。

        已停用的 Agent 一律报 healthy：运行时第一道门就是
        model_router.py:650-652 的 `if not agent.enabled: raise
        ModelRouteError("Agent 已禁用")`，它永远走不到解析候选那一步，
        **不可能因为这里所报的原因失败**。停一个 Provider 连带停掉它那批 Agent
        是最自然的运维动作，此时 Provider 侧按 parked 规则正确变绿，
        Agent 侧若整批亮红，横幅就在自相矛盾。

        池绑定要走完整条后备链，判据与运行时一致：model_router.py:492-506 逐个池
        往后备走，跳过已停用的池，只要链上任何一个成员可路由就不算失败。只看根池
        会把「根池坏了但后备池好着」误报成必失败——而配了后备池恰恰是为了这种情况。
        expand_pool_ids 是 _validate_agent_binding 绑定校验用的同一个展开器。

        成员过滤除了池 enabled 与成员行 enabled，还必须过 routable
        （model_router.py:509-513 那一道）：routable 的定义是
        `enabled AND (manual OR discovered_available) AND provider_enabled`
        （catalog.py:76），所以成员的模型行被停用、或成员所属 Provider 被停用时，
        运行时都不产生候选，最终抛 ModelRouteError("模型池没有可用模型")。
        少过这一道就是假绿，而假绿比没有横幅更坏。
        """
        status, reason = "healthy", ""
        if not agent.get("enabled"):
            status, reason = "healthy", "Agent 已停用，不参与路由"
        elif agent.get("binding_type") == "pool":
            try:
                chain = expand_pool_ids(int(agent.get("model_pool_id") or 0), pools)
            except ModelPoolValidationError:
                # 池不存在或后备链成环：运行时同样走不通，但原因不是「成员都坏了」
                status, reason = "will_fail", "绑定的模型池不存在或后备链有环"
            else:
                # 已停用的池被运行时整段跳过，不贡献任何候选
                enabled_members = [
                    member
                    for pool_id in chain
                    if bool((pools.get(pool_id) or {}).get("enabled"))
                    for member in (pools.get(pool_id) or {}).get("members", [])
                    if member.get("enabled")
                ]
                # 再过运行时那道 routable，成员的模型行或其 Provider 被停用都在此落选
                members = [
                    member
                    for member in enabled_members
                    if routable_by_model.get(int(member.get("provider_model_id") or 0))
                ]
                if not enabled_members:
                    status, reason = "will_fail", "绑定的模型池没有启用成员"
                elif not members:
                    # 与上一句刻意分开：成员开关本来就是开着的，让用户去开它只会白跑一趟
                    status, reason = (
                        "will_fail",
                        "模型池及其后备池里没有可路由的模型（成员模型被停用，或其 Provider 已停用）",
                    )
                elif all(
                    provider_status.get(int(m.get("provider_id") or 0)) == "will_fail"
                    for m in members
                ):
                    status, reason = (
                        "will_fail",
                        "模型池及其后备池里每个成员的 Provider 都配置必失败",
                    )
        else:
            provider_id = int(agent.get("provider_id") or 0)
            if not provider_id:
                status, reason = "will_fail", "没有绑定 Provider"
            elif provider_status.get(provider_id) == "will_fail":
                status, reason = "will_fail", "绑定的 Provider 配置必失败"
        return {
            "id": int(agent["id"]),
            "name": agent.get("name"),
            "task_type": agent.get("task_type"),
            "status": status,
            "reason": reason,
        }

    def list_providers(self) -> list[dict[str, Any]]:
        db = self._db()
        try:
            providers = db.list_ai_providers()
            for provider in providers:
                provider.pop("models_sync_owner", None)
            return providers
        finally:
            db.close()

    def create_provider(self, payload: dict[str, Any]) -> int:
        data = self._normalize_provider_payload(payload, require_key=bool(payload.get("api_key")))
        db = self._db()
        try:
            return db.create_ai_provider(data)
        finally:
            db.close()

    def update_provider(self, provider_id: int, payload: dict[str, Any]) -> list[dict[str, str]]:
        data = self._normalize_provider_payload(payload, require_key=False, partial=True)
        db = self._db()
        try:
            db.update_ai_provider(provider_id, data)
            self._invalidate_provider(provider_id)
            if "enabled" in data and not data["enabled"]:
                return self._lint_provider(db, provider_id)
            return []
        finally:
            db.close()

    def _lint_provider(self, db: Database, provider_id: int) -> list[dict[str, str]]:
        """停用之后立刻用和健康横幅同一套规则给一次提示。不拦保存。"""
        row = db.get_ai_provider(provider_id)
        if row is None:
            return []
        bound = db.conn.execute(
            """
            SELECT COUNT(*) FROM ai_agents
            WHERE binding_type = 'fixed' AND provider_id = ?
            """,
            (provider_id,),
        ).fetchone()[0]
        catalog = db.list_ai_provider_models(provider_id)
        pool_hit = db.conn.execute(
            """
            SELECT 1
            FROM ai_model_pool_members AS pm
            JOIN ai_provider_models AS m ON m.id = pm.provider_model_id
            JOIN ai_model_pools AS p ON p.id = pm.pool_id
            WHERE m.provider_id = ? AND pm.enabled = 1 AND p.enabled = 1
            LIMIT 1
            """,
            (provider_id,),
        ).fetchone()
        return self.provider_config_lint(
            row,
            bound_agent_count=int(bound),
            routable_models=int(catalog.get("routable") or 0),
            pool_referenced=pool_hit is not None,
        )

    def delete_provider(self, provider_id: int) -> None:
        db = self._db()
        try:
            try:
                db.delete_ai_provider(provider_id)
            except AIProviderReferenceError as exc:
                raise AIConflictError(str(exc)) from exc
            self._invalidate_provider(provider_id)
        finally:
            db.close()

    def test_provider(self, provider_id: int) -> dict[str, Any]:
        db = self._db()
        try:
            provider_config = self._load_provider_config(db, provider_id)
        finally:
            db.close()
        model = provider_config.default_model
        if not model:
            # 死循环：测试要模型 → 模型要同步 → 同步要先保存。目录里已有可路由模型时
            # 直接借第一个来测，别把用户卡在「先去填默认模型」。
            catalog = self.list_provider_models(provider_id, routable_only=True)
            items = catalog.get("items") or []
            if items:
                model = items[0].get("model_key")
        if not model:
            raise AIServiceError(
                "这个 Provider 还没有可用模型：先点「获取模型列表」，或在高级设置里填默认模型"
            )
        provider = self._get_provider(provider_config)
        started = time.time()
        text_parts: list[str] = []
        for chunk in provider.stream_generate(
            [{"role": "user", "content": "请只回复 OK。"}],
            model=model,
            temperature=0,
            top_p=1,
            max_tokens=32,
        ):
            if chunk.type == "delta":
                text_parts.append(chunk.text)
        return {"ok": True, "model": model, "latency_ms": int((time.time() - started) * 1000), "text": "".join(text_parts).strip()[:100]}

    _NON_CHAT_CAPABILITY_HINTS = ("embed", "rerank", "asr", "speech", "audio", "image", "vision")

    def probe_provider_models(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """用表单里的凭据拉一次模型列表用于预览。**不写任何库表。**

        与 model_sync 的分工：落库目录永远只有 model_sync 一个写入方，这里只回一份
        预览清单，让错的配置在落库前就被拦下（spec §4.2）。
        """
        base_url = (str(payload.get("base_url") or "")).strip() or None
        provider_type = str(payload.get("provider_type") or "openai_compatible")
        api_key = payload.get("api_key") or None
        provider_id = payload.get("provider_id")

        if provider_id is not None and not api_key:
            db = self._db()
            try:
                row = db.get_ai_provider(int(provider_id), include_secret=True)
            finally:
                db.close()
            if row is None:
                raise AINotFoundError("Provider 不存在")
            # 借库里那把 Key 时地址必须逐字相同：否则这个端点等于「把加密存好的 Key
            # 发到我指定的任意地址」，而 validate_base_url 拦不住它（目标可以是完全
            # 合法的 https 公网主机）。
            if (row.get("base_url") or None) != base_url:
                raise AIServiceError(
                    "借用已保存的 API Key 时不能同时改地址：请在表单里重新填入 Key"
                )
            api_key = self.secret_manager.decrypt(row.get("api_key_encrypted"))
        if not api_key:
            raise AIServiceError("请填入 API Key")

        config = AIProviderConfig(
            id=0,
            name="probe",
            provider_type=provider_type,
            base_url=base_url,
            api_key=api_key,
            default_model=None,
            timeout_seconds=int(payload.get("timeout_seconds") or 120),
            context_window=int(payload.get("context_window") or 128000),
        )
        provider = create_provider(config)
        started = time.time()
        try:
            result = provider.list_models(deadline=time.monotonic() + 60)
        finally:
            provider.close()

        items = []
        for model in result.models:
            capabilities = list(model.get("capabilities") or [])
            lowered = " ".join(capabilities).lower()
            suggested = not any(hint in lowered for hint in self._NON_CHAT_CAPABILITY_HINTS)
            items.append({
                "model_key": model.get("model_key"),
                "display_name": model.get("display_name"),
                "capabilities": capabilities,
                "context_window": model.get("context_window"),
                "suggested": suggested,
            })
        return {
            "latency_ms": int((time.time() - started) * 1000),
            "complete": bool(result.complete),
            "partial_reason": result.partial_reason,
            "items": items,
        }

    def list_provider_models(
        self,
        provider_id: int,
        *,
        search: str | None = None,
        routable_only: bool = False,
        enabled_only: bool = False,
    ) -> dict[str, Any]:
        if search is not None:
            search = self._reject_control_text(search, "search", 300)
            if len(search.encode("utf-8")) > 1200:
                raise AIServiceError("search 不能超过 1200 个 UTF-8 字节")
        db = self._db()
        try:
            self._require_provider_row(db, provider_id)
            return db.list_ai_provider_models(
                provider_id,
                search=search,
                routable_only=bool(routable_only),
                enabled_only=bool(enabled_only),
            )
        finally:
            db.close()

    def create_manual_model(
        self,
        provider_id: int,
        payload: dict[str, Any],
    ) -> int:
        self._reject_unknown_fields(payload, _MANUAL_MODEL_CREATE_FIELDS)
        self._require_boolean(payload, "enabled")
        data = dict(payload)
        data["provider_id"] = provider_id
        db = self._db()
        try:
            self._require_provider_row(db, provider_id)
            try:
                return db.create_ai_provider_model(data)
            except ModelCatalogValidationError as exc:
                raise AIServiceError(str(exc)) from exc
            except sqlite3.IntegrityError as exc:
                raise AIServiceError("该 Provider 已存在同名模型") from exc
        finally:
            db.close()

    def update_provider_model(
        self,
        model_id: int,
        payload: dict[str, Any],
    ) -> None:
        self._reject_unknown_fields(payload, _MANUAL_MODEL_UPDATE_FIELDS)
        if not payload:
            raise AIServiceError("至少提交一个可写模型字段")
        self._require_boolean(payload, "enabled")
        db = self._db()
        try:
            self._require_provider_model_row(db, model_id)
            try:
                db.update_ai_provider_model(model_id, payload)
            except ModelCatalogValidationError as exc:
                raise AIServiceError(str(exc)) from exc
        finally:
            db.close()

    def delete_provider_model(self, model_id: int) -> None:
        db = self._db()
        try:
            self._require_provider_model_row(db, model_id)
            try:
                db.remove_ai_provider_model_manual(model_id)
            except ModelCatalogConflictError as exc:
                raise AIConflictError(str(exc)) from exc
        finally:
            db.close()

    def start_model_sync(self, provider_id: int) -> dict[str, Any]:
        db = self._db()
        try:
            self._require_provider_row(db, provider_id)
        finally:
            db.close()
        try:
            return super().start_model_sync(provider_id)
        except ModelSyncConflictError as exc:
            data = (
                {"operation_id": exc.existing_operation_id}
                if exc.existing_operation_id
                else None
            )
            raise AIConflictError(str(exc), data=data) from exc

    def get_model_sync_operation(self, operation_id: str) -> dict[str, Any]:
        try:
            return super().get_model_sync_operation(operation_id)
        except ModelSyncConflictError as exc:
            raise AINotFoundError("模型同步 operation 不存在") from exc

    def cancel_model_sync(self, operation_id: str) -> bool:
        self.get_model_sync_operation(operation_id)
        return super().cancel_model_sync(operation_id)

    def confirm_model_sync_empty(
        self,
        operation_id: str,
        generation: int,
        result_digest: str,
    ) -> dict[str, int]:
        self.get_model_sync_operation(operation_id)
        try:
            return super().confirm_model_sync_empty(
                operation_id,
                generation,
                result_digest,
            )
        except ModelSyncConflictError as exc:
            raise AIConflictError(str(exc)) from exc

    def iter_model_sync_events(
        self,
        operation_id: str,
        poll_interval: float = 0.25,
    ):
        self.get_model_sync_operation(operation_id)
        return super().iter_model_sync_events(
            operation_id,
            poll_interval=poll_interval,
        )

    def list_model_pools(self) -> list[dict[str, Any]]:
        db = self._db()
        try:
            return db.list_ai_model_pools()
        finally:
            db.close()

    def get_model_pool(self, pool_id: int) -> dict[str, Any]:
        db = self._db()
        try:
            return self._require_model_pool_row(db, pool_id)
        finally:
            db.close()

    def create_model_pool(self, payload: dict[str, Any]) -> int:
        self._reject_unknown_fields(payload, _MODEL_POOL_FIELDS)
        self._require_boolean(payload, "enabled")
        data = self._normalize_model_pool_payload(payload)
        db = self._db()
        try:
            try:
                return db.create_ai_model_pool(data)
            except ModelPoolValidationError as exc:
                raise AIServiceError(str(exc)) from exc
            except sqlite3.IntegrityError as exc:
                raise AIServiceError("模型池名称重复或引用无效") from exc
        finally:
            db.close()

    def update_model_pool(
        self,
        pool_id: int,
        payload: dict[str, Any],
    ) -> int:
        self._reject_unknown_fields(payload, _MODEL_POOL_FIELDS | {"expected_version"})
        expected_version = self._expected_version(payload)
        patch = {key: value for key, value in payload.items() if key != "expected_version"}
        self._require_boolean(patch, "enabled")
        patch = self._normalize_model_pool_payload(patch)
        db = self._db()
        try:
            self._require_model_pool_row(db, pool_id)
            try:
                return db.update_ai_model_pool(pool_id, patch, expected_version)
            except ModelPoolConflictError as exc:
                raise AIConflictError(str(exc)) from exc
            except ModelPoolValidationError as exc:
                raise AIServiceError(str(exc)) from exc
            except sqlite3.IntegrityError as exc:
                raise AIServiceError("模型池名称重复或引用无效") from exc
        finally:
            db.close()

    def replace_model_pool_members(
        self,
        pool_id: int,
        payload: dict[str, Any],
    ) -> int:
        self._reject_unknown_fields(payload, {"expected_version", "members"})
        expected_version = self._expected_version(payload)
        members = payload.get("members")
        if not isinstance(members, list):
            raise AIServiceError("members 必须是数组")
        normalized: list[dict[str, Any]] = []
        for index, member in enumerate(members):
            if not isinstance(member, dict):
                raise AIServiceError(f"members[{index}] 必须是对象")
            self._reject_unknown_fields(member, {"provider_model_id", "enabled"})
            model_id = member.get("provider_model_id")
            if isinstance(model_id, bool) or not isinstance(model_id, int) or model_id <= 0:
                raise AIServiceError(
                    f"members[{index}].provider_model_id 必须是正整数"
                )
            self._require_boolean(member, "enabled")
            normalized.append(
                {
                    "provider_model_id": model_id,
                    "enabled": member.get("enabled", True),
                }
            )
        db = self._db()
        try:
            self._require_model_pool_row(db, pool_id)
            try:
                return db.replace_ai_model_pool_members(
                    pool_id,
                    normalized,
                    expected_version,
                )
            except ModelPoolConflictError as exc:
                raise AIConflictError(str(exc)) from exc
            except ModelPoolValidationError as exc:
                raise AIServiceError(str(exc)) from exc
        finally:
            db.close()

    def delete_model_pool(self, pool_id: int) -> None:
        db = self._db()
        try:
            self._require_model_pool_row(db, pool_id)
            try:
                db.delete_ai_model_pool(pool_id)
            except ModelPoolConflictError as exc:
                raise AIConflictError(str(exc)) from exc
        finally:
            db.close()

    def list_model_pool_attempts(
        self,
        pool_id: int,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        db = self._db()
        try:
            self._require_model_pool_row(db, pool_id)
            return db.list_ai_model_pool_attempts(pool_id, limit=limit)
        finally:
            db.close()

    def preview_agent_candidates(self, agent_id: int) -> dict[str, Any]:
        """解析并返回该 Agent 的候选模型链，不发起任何真实生成请求。

        配置界面此前完全看不出「这个 Agent 实际会依次调用哪些模型」，只能等任务
        跑完去日志页事后看。这里复用 ModelRouter 的解析路径，保证预览顺序与真实
        执行顺序永远一致；同时只回传路由决策需要的字段——Provider 的密文、
        base_url、配置哈希一律不出网，避免预览成为机密的旁路出口。
        """
        db = self._db()
        try:
            agent = self._load_agent_config(db, agent_id)
        finally:
            db.close()

        snapshot = self.model_router.resolve_candidates(agent, stage="main")
        candidates = [
            {
                "order": index + 1,
                "provider_id": candidate.provider_id,
                "provider_name": candidate.provider_name,
                "model_key": candidate.model_key,
                "provider_model_id": candidate.provider_model_id,
                "pool_id": candidate.pool_id,
                "pool_name": candidate.pool_name,
                "pool_position": candidate.pool_position,
                "fallback_depth": candidate.fallback_depth,
                # 固定绑定没有池节点，界面直接显示「fixed」；池绑定显示命中的池名，
                # 这样一眼能看出候选是主池还是第几级后备池给出来的。
                "source": candidate.pool_name or "fixed",
                "capabilities": list(candidate.capabilities),
                "context_window": candidate.context_window,
            }
            for index, candidate in enumerate(snapshot.candidates)
        ]
        first = candidates[0] if candidates else {}
        return {
            "agent_id": agent.id,
            "agent_name": agent.name,
            "task_type": agent.task_type,
            "binding_type": agent.binding_type,
            "pool_id": first.get("pool_id"),
            "pool_name": first.get("pool_name"),
            "candidates": candidates,
            "limits": {
                "max_candidate_attempts": MAX_CANDIDATE_ATTEMPTS,
                "max_network_requests": MAX_NETWORK_REQUESTS,
                "max_resolved_candidates": MAX_RESOLVED_CANDIDATES,
                "max_pool_nodes": MAX_POOL_NODES,
            },
        }

    def list_agents(self) -> list[dict[str, Any]]:
        db = self._db()
        try:
            return list(db.list_ai_agents())
        finally:
            db.close()

    def create_agent(self, payload: dict[str, Any]) -> int:
        data = self._normalize_agent_payload(payload)
        db = self._db()
        try:
            with db.transaction():
                self._validate_agent_binding(db, data)
                return db.create_ai_agent(data)
        finally:
            db.close()

    def update_agent(self, agent_id: int, payload: dict[str, Any]) -> None:
        data = self._normalize_agent_payload(payload, partial=True)
        db = self._db()
        try:
            with db.transaction():
                existing = db.get_ai_agent(agent_id)
                if not existing:
                    raise AIServiceError("Agent 不存在")
                merged = {**existing, **data}
                binding_type = merged.get("binding_type") or "fixed"
                update_data = dict(data)
                update_data["binding_type"] = binding_type
                if binding_type == "pool":
                    if data.get("provider_id") is not None or data.get("model") is not None:
                        raise AIServiceError("固定模型和模型池不能同时提交")
                    merged["provider_id"] = None
                    merged["model"] = None
                    update_data["provider_id"] = None
                    update_data["model"] = None
                    if merged.get("model_pool_id") is None:
                        raise AIServiceError("缺少 Agent 字段：model_pool_id")
                else:
                    if data.get("model_pool_id") is not None:
                        raise AIServiceError("固定模型和模型池不能同时提交")
                    merged["model_pool_id"] = None
                    update_data["model_pool_id"] = None
                    if merged.get("provider_id") is None:
                        raise AIServiceError("缺少 Agent 字段：provider_id")
                self._validate_agent_binding(db, merged)
                db.update_ai_agent(agent_id, update_data)
        finally:
            db.close()

    def delete_agent(self, agent_id: int) -> None:
        db = self._db()
        try:
            existing = db.get_ai_agent(agent_id)
            if not existing:
                raise AIServiceError("Agent 不存在")
            db.delete_ai_agent(agent_id)
        finally:
            db.close()

    def update_agent_bindings(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """批量改绑 / 批量启停。"""
        raw_ids = payload.get("agent_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise AIServiceError("agent_ids 必须是非空数组")
        agent_ids = [int(value) for value in raw_ids]

        db = self._db()
        try:
            by_id = {int(a["id"]): a for a in db.list_ai_agents()}
            missing = [i for i in agent_ids if i not in by_id]
            if missing:
                raise AINotFoundError(f"Agent 不存在：{missing}")
            if "enabled" in payload:
                self._require_boolean(payload, "enabled")
                updated = db.set_ai_agents_enabled(agent_ids, bool(payload["enabled"]))
            else:
                binding = payload.get("binding")
                if not isinstance(binding, dict):
                    raise AIServiceError("binding 必须是对象")
                binding_type = binding.get("binding_type") or "fixed"
                if binding_type not in ("fixed", "pool"):
                    raise AIServiceError("binding_type 只能是 fixed 或 pool")
                if binding_type == "fixed" and not binding.get("provider_id"):
                    raise AIServiceError("固定绑定必须指定 provider_id")
                if binding_type == "pool" and not binding.get("model_pool_id"):
                    raise AIServiceError("池绑定必须指定 model_pool_id")
                # ai_agents 上那条 CHECK 要求两个绑定字段互斥，且 pool 时 model 必须为
                # NULL（model_schema.py:164-172），不归一化就会被 SQLite 整条拒掉。
                normalized = {
                    "binding_type": binding_type,
                    "model": binding.get("model") if binding_type == "fixed" else None,
                    "provider_id": binding.get("provider_id") if binding_type == "fixed" else None,
                    "model_pool_id": binding.get("model_pool_id") if binding_type == "pool" else None,
                }
                updated = db.update_ai_agent_bindings(agent_ids, normalized)
        finally:
            db.close()
        return {"updated": int(updated)}

    def _normalize_provider_payload(self, payload: dict[str, Any], require_key: bool = False, partial: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {}
        keys = ["name", "provider_type", "base_url", "default_model", "available_models", "timeout_seconds", "max_retries", "proxy", "context_window", "stream_enabled", "enabled"]
        for key in keys:
            if key in payload:
                data[key] = payload[key]
        if not partial:
            for key in ("name", "provider_type"):
                if not data.get(key):
                    raise AIServiceError(f"缺少 Provider 字段：{key}")
        # H1: 校验 base_url，阻止 SSRF / 密钥外泄。写入时不做 DNS 解析（避免把
        # 配置保存耦合到网络可达性），请求时再带解析校验抵御 rebinding。
        if data.get("base_url") is not None:
            base_url = str(data["base_url"]).strip()
            if base_url:
                try:
                    data["base_url"] = validate_base_url(base_url, resolve=False)
                except ProviderConfigError as exc:
                    raise AIServiceError(str(exc)) from exc
            else:
                data["base_url"] = None
        if data.get("provider_type") not in {None, "openai_compatible", "anthropic", "xai"}:
            raise AIServiceError("不支持的 Provider 类型")
        api_key = str(payload.get("api_key") or "")
        if api_key:
            data["api_key_encrypted"] = self.secret_manager.encrypt(api_key)
        elif require_key:
            raise AIServiceError("缺少 API key")
        return data

    def _normalize_agent_payload(self, payload: dict[str, Any], partial: bool = False) -> dict[str, Any]:
        forbidden = sorted(_FORBIDDEN_AGENT_POLICY_FIELDS.intersection(payload))
        if forbidden:
            raise AIServiceError(
                f"内部策略字段不允许通过普通 Agent 接口提交：{', '.join(forbidden)}"
            )
        task_type = payload.get("task_type")
        data = {
            key: payload[key]
            for key in (
                "name",
                "task_type",
                "binding_type",
                "provider_id",
                "model",
                "model_pool_id",
                "required_capabilities",
                "system_prompt",
                "temperature",
                "top_p",
                "max_tokens",
                "context_window",
                "enabled",
            )
            if key in payload
        }
        if (
            "required_capabilities" not in data
            and "required_capabilities_json" in payload
        ):
            try:
                legacy_capabilities = json.loads(
                    str(payload["required_capabilities_json"])
                )
            except (TypeError, ValueError) as exc:
                raise AIServiceError("required_capabilities_json 必须是有效 JSON") from exc
            data["required_capabilities"] = legacy_capabilities
        binding_type = data.get("binding_type")
        if binding_type is None and not partial:
            binding_type = "fixed"
            data["binding_type"] = binding_type
        if binding_type is not None and binding_type not in {"fixed", "pool"}:
            raise AIServiceError("不支持的 Agent 绑定类型")
        if binding_type == "pool" and (
            data.get("provider_id") is not None or data.get("model") is not None
        ):
            raise AIServiceError("固定模型和模型池不能同时提交")
        if binding_type == "fixed" and data.get("model_pool_id") is not None:
            raise AIServiceError("固定模型和模型池不能同时提交")
        if "required_capabilities" in data:
            try:
                capabilities = normalize_capabilities(
                    data["required_capabilities"],
                    reject_unknown=True,
                )
            except ModelCatalogValidationError as exc:
                raise AIServiceError(str(exc)) from exc
            data["required_capabilities"] = sorted(capabilities)
        if not partial:
            for key in ("name", "task_type", "system_prompt"):
                if not data.get(key):
                    raise AIServiceError(f"缺少 Agent 字段：{key}")
            binding_key = "provider_id" if binding_type == "fixed" else "model_pool_id"
            if not data.get(binding_key):
                raise AIServiceError(f"缺少 Agent 字段：{binding_key}")
        if data.get("task_type") not in {None, "general", "keyword_clean"}:
            raise AIServiceError("不支持的 Agent 类型")
        return data

    def _validate_agent_binding(self, db: Database, data: dict[str, Any]) -> None:
        if data.get("binding_type", "fixed") == "pool":
            pool_id = int(data["model_pool_id"])
            pools = db.list_ai_model_pools()
            pools_by_id = {int(pool["id"]): pool for pool in pools}
            pool = pools_by_id.get(pool_id)
            if pool is None:
                raise AIServiceError("绑定的模型池不存在")
            if not bool(pool.get("enabled")):
                raise AIServiceError("绑定的模型池必须启用")
            try:
                expanded_ids = expand_pool_ids(pool_id, pools_by_id)
            except ModelPoolValidationError as exc:
                raise AIServiceError(str(exc)) from exc

            candidates: list[dict[str, Any]] = []
            for expanded_id in expanded_ids:
                expanded_pool = pools_by_id[expanded_id]
                if not bool(expanded_pool.get("enabled")):
                    raise AIServiceError("绑定链中的后备模型池必须启用")
                for member in expanded_pool.get("members") or []:
                    if not bool(member.get("enabled")):
                        continue
                    model = db.get_ai_provider_model(
                        int(member["provider_model_id"])
                    )
                    if model and bool(model.get("routable")):
                        candidates.append(model)
            if not candidates:
                raise AIServiceError("绑定的模型池不能为空或没有可用候选")

            required = set(data.get("required_capabilities") or [])
            if required and not any(
                required.issubset(set(candidate["capabilities"]))
                for candidate in candidates
            ):
                raise AIServiceError("模型池没有满足全部必需能力的可用候选")
            return
        provider_id = int(data["provider_id"])
        provider = db.get_ai_provider(provider_id)
        if not provider:
            raise AIServiceError("Provider 不存在")
        if not bool(provider.get("enabled")):
            raise AIServiceError("Provider 已禁用")

        required = set(data.get("required_capabilities") or [])
        if not required:
            return
        model_key = data.get("model") or provider.get("default_model")
        models = db.list_ai_provider_models(
            provider_id,
            routable_only=True,
        )["items"]
        catalog_model = next(
            (item for item in models if item["model_key"] == model_key),
            None,
        )
        if catalog_model is None:
            raise AIServiceError("声明能力要求时，固定模型必须存在于可用模型目录")
        missing = sorted(required.difference(catalog_model["capabilities"]))
        if missing:
            raise AIServiceError(f"固定模型缺少必需能力：{', '.join(missing)}")

    def _load_provider_config(self, db: Database, provider_id: int) -> AIProviderConfig:
        row = db.get_ai_provider(provider_id, include_secret=True)
        if not row:
            raise AIServiceError("Provider 不存在")
        if not bool(row.get("enabled")):
            raise AIServiceError("Provider 已禁用")
        stored_cipher = row.get("api_key_encrypted")
        api_key = self.secret_manager.decrypt(stored_cipher)
        # L4: 若命中已废弃的 v1（无盐 SHA-256）KDF，解密成功后透明升级到 v2 回写。
        if api_key and self.secret_manager.is_legacy_ciphertext(stored_cipher):
            try:
                db.update_ai_provider(provider_id, {"api_key_encrypted": self.secret_manager.encrypt(api_key)})
            except Exception:
                pass  # 升级失败不影响本次使用；下次再试
        return AIProviderConfig(
            id=int(row["id"]), name=row["name"], provider_type=row["provider_type"],
            base_url=row.get("base_url"), api_key=api_key, default_model=row.get("default_model"),
            timeout_seconds=int(row["timeout_seconds"]) if row.get("timeout_seconds") is not None else 120,
            max_retries=int(row["max_retries"]) if row.get("max_retries") is not None else 2,
            proxy=row.get("proxy"),
            context_window=int(row["context_window"]) if row.get("context_window") is not None else 128000,
            stream_enabled=bool(row.get("stream_enabled", 1)),
            enabled=bool(row.get("enabled")),
        )

    def _load_agent_config(self, db: Database, agent_id: int) -> AIAgentConfig:
        row = db.get_ai_agent(agent_id)
        if not row:
            raise AIServiceError("Agent 不存在")
        if not bool(row.get("enabled")):
            raise AIServiceError("Agent 已禁用")
        provider_id = row.get("provider_id")
        return AIAgentConfig(
            id=int(row["id"]), name=row["name"], task_type=row["task_type"],
            provider_id=int(provider_id) if provider_id is not None else None,
            model=row.get("model"), system_prompt=row["system_prompt"],
            temperature=float(row["temperature"]) if row.get("temperature") is not None else 0.8,
            top_p=float(row["top_p"]) if row.get("top_p") is not None else 0.9,
            max_tokens=int(row["max_tokens"]) if row.get("max_tokens") is not None else 4000,
            context_window=int(row["context_window"]) if row.get("context_window") is not None else 16000,
            enabled=bool(row.get("enabled")),
            binding_type=row.get("binding_type") or "fixed",
            model_pool_id=(
                int(row["model_pool_id"])
                if row.get("model_pool_id") is not None
                else None
            ),
            required_capabilities=tuple(row.get("required_capabilities") or []),
            binding_version=int(row.get("binding_version") or 1),
        )

    def list_jobs(self, task_type: str | None = None, status: str | None = None,
                  page: int = 1, page_size: int = 20) -> dict[str, Any]:
        db = self._db()
        try:
            return db.list_ai_jobs(task_type=task_type, status=status, page=page, page_size=page_size)
        finally:
            db.close()

    def get_job(self, job_id: str) -> dict[str, Any]:
        db = self._db()
        try:
            job = db.get_ai_job(job_id)
            if not job:
                raise AIServiceError("任务不存在")
            return job
        finally:
            db.close()

    def cleanup_jobs(
        self,
        keep_days: int = 3,
        keep_failed_days: int | None = None,
        owner_scope: str | None = None,
    ) -> int:
        """清理超期的 ai_jobs 历史记录，返回删除条数。"""
        db = self._db()
        try:
            return db.cleanup_ai_jobs(
                keep_days=keep_days,
                keep_failed_days=keep_failed_days,
                owner_scope=owner_scope,
            )
        finally:
            db.close()

    def seed_builtin_agents(self, provider_id: int) -> dict[str, int]:
        """初始化内置 Agent（幂等，同名则跳过）。返回 {name: id}。"""
        agents = [
            {
                "name": "全能助手",
                "task_type": "general",
                "system_prompt": "你是专业的中文文本处理助手，按用户要求完成整理、提炼、分析等任务。始终以专业、认真的态度完成。",
                "temperature": 0.8,
                "max_tokens": 4000,
                "context_window": 16000,
            },
            {
                "name": "关键词清洗师",
                "task_type": "keyword_clean",
                "system_prompt": DEFAULT_KEYWORD_CLEAN_PROMPT,
                "temperature": 0.3,
                "max_tokens": 2000,
                "context_window": 16000,
            },
        ]
        db = self._db()
        created: dict[str, int] = {}
        try:
            existing = db.list_ai_agents()
            existing_names = {a["name"] for a in existing}
            for a in agents:
                if a["name"] not in existing_names:
                    agent_id = db.create_ai_agent({**a, "provider_id": provider_id, "enabled": True})
                    created[a["name"]] = agent_id
                else:
                    for ea in existing:
                        if ea["name"] == a["name"]:
                            created[a["name"]] = ea["id"]
                            break
        finally:
            db.close()
        return created
