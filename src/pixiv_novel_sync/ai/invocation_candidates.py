"""Safe execution facts projected inside the caller's read transaction."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .invocation import InvocationValidationError
from .invocation_planner import CandidateProfile
from .model_catalog import ModelCatalogValidationError, normalize_model_key
from .model_sync import provider_model_sync_config_hash
from .models import AIAgentConfig

if TYPE_CHECKING:
    from ..storage_db import Database


def model_execution_config_hash(model_key: str, model: Mapping[str, Any] | None) -> str:
    fields = {
        "model_key": model_key,
        "registered": model is not None,
        "id": model.get("id") if model else None,
        "enabled": bool(model["enabled"]) if model else True,
        "manual": bool(model.get("manual")) if model else False,
        "discovered_available": bool(model.get("discovered_available")) if model else False,
        "capabilities": sorted(model.get("capabilities") or []) if model else [],
        "context_window": model.get("context_window") if model else None,
        "max_output_tokens": model.get("max_output_tokens") if model else None,
    }
    raw = json.dumps(fields, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _configuration_error(message: str) -> InvocationValidationError:
    return InvocationValidationError("invalid_configuration", message)


def _model_key(value: Any) -> str:
    try:
        return normalize_model_key(value)
    except ModelCatalogValidationError:
        raise _configuration_error("Configured model key is invalid") from None


def _make_profile(source_index, provider, model_key, model, *, pool=None, member=None, depth=0):
    reason = None
    if not bool(provider["enabled"]):
        reason = "provider_disabled"
    elif pool is not None and not bool(pool["enabled"]):
        reason = "pool_disabled"
    elif member is not None and not bool(member["enabled"]):
        reason = "member_disabled"
    elif model is not None and not bool(model["enabled"]):
        reason = "model_disabled"
    elif model is not None and not (model.get("manual") or model.get("discovered_available")):
        reason = "model_unavailable"
    return CandidateProfile(
        source_index=source_index, provider_id=int(provider["id"]), provider_name=provider["name"],
        provider_type=provider["provider_type"], model_key=model_key,
        provider_context_window=provider["context_window"],
        provider_config_hash=provider_model_sync_config_hash(provider),
        model_config_hash=model_execution_config_hash(model_key, model),
        provider_model_id=int(model["id"]) if model else None,
        display_name=model.get("display_name") if model else None,
        model_context_window=model.get("context_window") if model else None,
        max_output_tokens=model.get("max_output_tokens") if model else None,
        capabilities=tuple(model.get("capabilities") or ()) if model else (),
        pool_id=int(pool["id"]) if pool else None, pool_name=pool["name"] if pool else None,
        pool_version=int(pool["version"]) if pool else None,
        pool_position=int(member["position"]) if member else None,
        fallback_depth=depth, unavailable_reason=reason,
    )


def collect_candidate_profiles(db: Database, agent: AIAgentConfig) -> tuple[CandidateProfile, ...]:
    if not agent.enabled:
        raise _configuration_error("Agent is disabled")
    if agent.binding_type not in ("fixed", "pool"):
        raise _configuration_error("Agent binding type is invalid")
    provider_rows: dict[int, dict[str, Any]] = {}

    def provider_row(provider_id):
        if type(provider_id) is not int or provider_id <= 0:
            raise _configuration_error("Agent or model is missing its Provider")
        if provider_id not in provider_rows:
            row = db.get_ai_provider(provider_id, include_secret=True)
            if row is None:
                raise _configuration_error("Referenced Provider does not exist")
            provider_rows[provider_id] = row
        return provider_rows[provider_id]

    if agent.binding_type == "fixed":
        provider = provider_row(agent.provider_id)
        key = _model_key(agent.model if agent.model is not None else provider.get("default_model"))
        models = db.list_ai_provider_models(provider["id"], routable_only=False)["items"]
        model = next((item for item in models if item["model_key"] == key), None)
        return (_make_profile(0, provider, key, model),)

    pool_id = agent.model_pool_id
    if type(pool_id) is not int or pool_id <= 0:
        raise _configuration_error("Agent is missing its routing group")
    profiles: list[CandidateProfile] = []
    seen_pools: set[int] = set()
    while pool_id is not None:
        if pool_id in seen_pools:
            raise _configuration_error("Routing group fallback chain contains a cycle")
        if len(seen_pools) >= 8:
            raise _configuration_error("Routing group fallback chain exceeds eight nodes")
        depth = len(seen_pools)
        seen_pools.add(pool_id)
        pool = db.get_ai_model_pool(pool_id)
        if pool is None:
            raise _configuration_error("Referenced routing group does not exist")
        # The ordinary joined pool view hides orphan model references.
        members = db.conn.execute(
            "SELECT provider_model_id, position, enabled FROM ai_model_pool_members "
            "WHERE pool_id = ? ORDER BY position LIMIT 65", (pool_id,),
        ).fetchall()
        if len(members) > 64:
            raise _configuration_error("Routing group exceeds 64 members")
        for member in members:
            model = db.get_ai_provider_model(member["provider_model_id"])
            if model is None:
                raise _configuration_error("Referenced catalog model does not exist")
            provider = provider_row(model["provider_id"])
            profiles.append(_make_profile(
                len(profiles), provider, _model_key(model["model_key"]), model,
                pool=pool, member=member, depth=depth,
            ))
        pool_id = pool.get("fallback_pool_id")
    return tuple(profiles)
