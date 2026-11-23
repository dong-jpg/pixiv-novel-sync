from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import asdict, fields, replace
from types import MappingProxyType
from typing import Any

from ..ai.invocation import InvocationValidationError, normalize_text_invocation
from ..ai.invocation_candidates import collect_candidate_profiles
from ..ai.invocation_planner import plan_invocation
from ..ai.models import AIAgentConfig
from ..storage.ai.catalog import CatalogMixin
from .contracts import WorkAnalysisError, fingerprint


class _ConfigurationReader(CatalogMixin):
    def __init__(self, conn):
        self.conn = conn

    def read_transaction(self):
        return nullcontext(self.conn)

    def get_ai_provider(self, provider_id, include_secret=False):
        row = self.conn.execute("SELECT * FROM ai_providers WHERE id=?", (provider_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["stream_enabled"] = bool(result["stream_enabled"])
        if not include_secret:
            result.pop("api_key_encrypted", None)
        return result

    def get_ai_model_pool(self, pool_id):
        row = self.conn.execute("SELECT * FROM ai_model_pools WHERE id=?", (pool_id,)).fetchone()
        return dict(row) if row is not None else None


def snapshot_work_configuration(conn, agent_id: int, output_tokens: int):
    if type(agent_id) is not int or agent_id <= 0:
        raise WorkAnalysisError("invalid_agent", 400)
    if type(output_tokens) is not int or not 1 <= output_tokens <= 1_000_000:
        raise WorkAnalysisError("invalid_output_tokens", 400)
    row = conn.execute("SELECT * FROM ai_agents WHERE id=?", (agent_id,)).fetchone()
    if row is None:
        raise WorkAnalysisError("configuration_unavailable", 409)
    raw = dict(row)
    try:
        capabilities = json.loads(raw["required_capabilities_json"])
        if not isinstance(capabilities, list) or any(not isinstance(item, str) for item in capabilities):
            raise ValueError("invalid capabilities")
        raw["required_capabilities"] = tuple(capabilities)
        raw["enabled"] = bool(raw["enabled"])
        agent = AIAgentConfig(**{field.name: raw[field.name] for field in fields(AIAgentConfig)})
        reader = _ConfigurationReader(conn)
        profiles = collect_candidate_profiles(reader, agent)
    except (KeyError, TypeError, ValueError, InvocationValidationError):
        raise WorkAnalysisError("configuration_unavailable", 409) from None
    if not profiles:
        raise WorkAnalysisError("configuration_unavailable", 409)
    provider_ids = tuple(sorted({profile.provider_id for profile in profiles}))
    legacy_provider_ids = []
    for provider_id in provider_ids:
        provider = reader.get_ai_provider(provider_id, include_secret=True)
        ciphertext = provider.get("api_key_encrypted") if provider else None
        relevant = any(
            profile.provider_id == provider_id and profile.unavailable_reason is None
            and not set(agent.required_capabilities).difference(profile.capabilities)
            for profile in profiles
        )
        if relevant and (not isinstance(ciphertext, str) or not ciphertext.strip()):
            raise WorkAnalysisError("configuration_unavailable", 409)
        if isinstance(ciphertext, str) and ciphertext and not ciphertext.startswith("v2$"):
            legacy_provider_ids.append(provider_id)
    pools = []
    pool_id = agent.model_pool_id if agent.binding_type == "pool" else None
    seen = set()
    while pool_id is not None:
        if pool_id in seen or len(seen) >= 8:
            raise WorkAnalysisError("configuration_unavailable", 409)
        seen.add(pool_id)
        pool = reader.get_ai_model_pool(pool_id)
        if pool is None:
            raise WorkAnalysisError("configuration_unavailable", 409)
        members = [dict(item) for item in conn.execute(
            "SELECT provider_model_id,position,enabled FROM ai_model_pool_members "
            "WHERE pool_id=? ORDER BY position LIMIT 65", (pool_id,),
        ).fetchall()]
        if len(members) > 64:
            raise WorkAnalysisError("configuration_unavailable", 409)
        pools.append({
            "id": pool_id, "version": pool["version"], "enabled": bool(pool["enabled"]),
            "fallback_pool_id": pool["fallback_pool_id"], "members": members,
        })
        pool_id = pool["fallback_pool_id"]
    agent_identity = asdict(agent)
    agent_prompt = agent_identity.pop("system_prompt")
    if not isinstance(agent_prompt, str):
        raise WorkAnalysisError("configuration_unavailable", 409)
    agent_identity["system_prompt_hash"] = fingerprint(agent_prompt)
    identity = {
        "schema_version": 1, "agent_id": agent_id, "output_tokens": output_tokens,
        "agent": agent_identity, "profiles": [asdict(profile) for profile in profiles], "pools": pools,
    }
    configuration = {
        "schema_version": 1, "agent_id": agent_id, "output_tokens": output_tokens,
        "configuration_fingerprint": fingerprint(identity),
        "allowed_provider_ids": list(provider_ids), "legacy_provider_ids": legacy_provider_ids,
    }
    target_rows = []
    target_keys = set()
    for profile in profiles:
        if (profile.unavailable_reason is not None
                or set(agent.required_capabilities).difference(profile.capabilities)):
            continue
        key = (profile.provider_id, profile.model_key)
        if key in target_keys:
            continue
        target_keys.add(key)
        if len(target_keys) > 64:
            raise WorkAnalysisError("configuration_unavailable", 409)
        target_rows.append(MappingProxyType({
            "provider_id": profile.provider_id, "provider_name": profile.provider_name,
            "provider_model_id": profile.provider_model_id, "model_key": profile.model_key,
            "display_name": profile.display_name, "pool_id": profile.pool_id,
            "pool_name": profile.pool_name, "pool_position": profile.pool_position,
            "fallback_depth": profile.fallback_depth,
        }))
    work_targets = tuple(target_rows)

    def preflight(payload: Mapping[str, Any]):
        if not isinstance(payload, Mapping):
            raise WorkAnalysisError("invalid_request", 400)
        supplied = dict(payload)
        if supplied.get("max_tokens", output_tokens) != output_tokens:
            raise WorkAnalysisError("configuration_changed", 409)
        if "allowed_provider_ids" in supplied and supplied["allowed_provider_ids"] != list(provider_ids):
            raise WorkAnalysisError("configuration_changed", 409)
        supplied["max_tokens"] = output_tokens
        supplied["allowed_provider_ids"] = list(provider_ids)
        try:
            request = normalize_text_invocation(supplied, agent=agent)
            planned = plan_invocation(
                request, profiles, agent_context_window=agent.context_window,
                required_capabilities=agent.required_capabilities,
            )
            public = planned.to_public_dict()
            # The v2 exclusion list is display-bounded at 64, not exhaustive.
            can_segment = False
            seen_candidates = set()
            if not planned.can_run:
                minimum_request = replace(
                    request, messages=tuple(replace(message, content="") for message in request.messages),
                )
                for profile in profiles:
                    if (profile.unavailable_reason is not None
                            or set(agent.required_capabilities).difference(profile.capabilities)):
                        continue
                    identity = (profile.provider_id, profile.model_key)
                    if identity in seen_candidates:
                        continue
                    seen_candidates.add(identity)
                    single = plan_invocation(
                        request, (profile,), agent_context_window=agent.context_window,
                        required_capabilities=agent.required_capabilities,
                    )
                    if any(item.reason == "context_overflow" for item in single.excluded):
                        # Reuse the same candidate's framing and shrinking safety
                        # margin; fixed output/framing failure cannot be segmented.
                        minimum = plan_invocation(
                            minimum_request, (profile,), agent_context_window=agent.context_window,
                            required_capabilities=agent.required_capabilities,
                        )
                        if minimum.can_run:
                            can_segment = True
                            break
            public["work_can_segment"] = can_segment
            public["work_targets"] = [dict(target) for target in work_targets]
            return public
        except InvocationValidationError:
            raise WorkAnalysisError("configuration_unavailable", 409) from None

    return configuration, agent_prompt, preflight


def work_configuration_identity(conn, agent_id: int, output_tokens: int):
    configuration, agent_prompt, preflight = snapshot_work_configuration(conn, agent_id, output_tokens)
    return configuration
