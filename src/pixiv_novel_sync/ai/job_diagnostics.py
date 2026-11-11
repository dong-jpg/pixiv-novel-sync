from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .invocation_snapshot import PlannedCandidateSnapshot
from .model_router import ModelRouteConflictError, ModelRouter


def project_main_job_diagnostics(job: Mapping[str, Any]) -> dict[str, Any]:
    """Extend stored job rows without rereading live Provider configuration."""

    result = dict(job)
    result["validation_status"] = job.get("validation_status") or "unknown"
    result["can_continue"] = False
    result["continue_unavailable_reason"] = "当前任务类型暂不支持手动候选继续"
    result["snapshot_version"] = None
    result["invocation_plan"] = None
    result["plan_unavailable_reason"] = None
    payload = job.get("candidate_snapshot")
    if payload is None:
        return result
    try:
        snapshot = ModelRouter.candidate_snapshot_from_payload(
            payload,
            job.get("candidate_snapshot_hash"),
        )
    except ModelRouteConflictError:
        result["plan_unavailable_reason"] = "保存的调用计划无法读取"
    else:
        result["snapshot_version"] = getattr(snapshot, "version", 1)
        if isinstance(snapshot, PlannedCandidateSnapshot):
            result["invocation_plan"] = snapshot.plan.to_public_dict()
    return result
