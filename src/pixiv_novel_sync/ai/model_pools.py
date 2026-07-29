"""模型池后备图的无副作用校验。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class ModelPoolValidationError(ValueError):
    """模型池配置不满足领域约束。"""


class ModelPoolConflictError(RuntimeError):
    """模型池写入与当前版本或引用状态冲突。"""


def _member_is_effective(member: Mapping[str, Any]) -> bool:
    return bool(member.get("enabled", 1)) and bool(member.get("routable", True))


def expand_pool_ids(
    root_pool_id: int,
    pools: Mapping[int, Mapping[str, Any]],
) -> tuple[int, ...]:
    """按后备顺序展开池 ID。"""

    expanded: list[int] = []
    seen: set[int] = set()
    current_id: int | None = int(root_pool_id)
    while current_id is not None:
        if current_id in seen:
            raise ModelPoolValidationError("模型池后备链存在循环")
        pool = pools.get(current_id)
        if pool is None:
            raise ModelPoolValidationError("模型池或后备模型池不存在")
        seen.add(current_id)
        expanded.append(current_id)
        fallback_id = pool.get("fallback_pool_id")
        current_id = None if fallback_id is None else int(fallback_id)
    return tuple(expanded)


def validate_pool_graph(
    pools: Sequence[Mapping[str, Any]],
    members: Mapping[int, Sequence[Mapping[str, Any]]],
    root_pool_id: int | None = None,
) -> None:
    """校验模型池后备图当前不存在循环。"""

    by_id = {int(pool["id"]): pool for pool in pools}
    if root_pool_id is not None:
        root = by_id.get(int(root_pool_id))
        if root is None:
            raise ModelPoolValidationError("绑定的模型池不存在")
        if not bool(root.get("enabled")):
            raise ModelPoolValidationError("绑定的模型池必须启用")
    visiting: set[int] = set()
    visited: set[int] = set()

    def visit(pool_id: int) -> None:
        if pool_id in visiting:
            raise ModelPoolValidationError("模型池后备链存在循环")
        if pool_id in visited:
            return
        visiting.add(pool_id)
        fallback_id = by_id[pool_id].get("fallback_pool_id")
        if fallback_id is not None and int(fallback_id) in by_id:
            visit(int(fallback_id))
        visiting.remove(pool_id)
        visited.add(pool_id)

    for current_id in by_id:
        visit(current_id)

    for current_id in by_id:
        expanded_ids = expand_pool_ids(current_id, by_id)
        if len(expanded_ids) > 8:
            raise ModelPoolValidationError("模型池后备链深度不能超过 8")
        candidates: set[tuple[Any, Any]] = set()
        for expanded_id in expanded_ids:
            for member in members.get(expanded_id, ()):
                if not _member_is_effective(member):
                    continue
                if "provider_id" in member and "model_key" in member:
                    candidate = (member["provider_id"], member["model_key"])
                else:
                    candidate = ("provider_model_id", member.get("provider_model_id"))
                candidates.add(candidate)
        if len(candidates) > 64:
            raise ModelPoolValidationError("模型池后备链最多包含 64 个有效候选")

    for pool_id, pool in by_id.items():
        pool_members = members.get(pool_id, ())
        if len(pool_members) > 64:
            raise ModelPoolValidationError("单个模型池最多包含 64 个成员")
        has_effective_member = any(_member_is_effective(member) for member in pool_members)
        if bool(pool.get("enabled")) and not has_effective_member:
            raise ModelPoolValidationError("启用的模型池不能为空或没有可用成员")
        if bool(pool.get("enabled")):
            for fallback_id in expand_pool_ids(pool_id, by_id)[1:]:
                if not bool(by_id[fallback_id].get("enabled")):
                    raise ModelPoolValidationError(
                        "启用模型池的后备模型池也必须启用"
                    )
                fallback_members = members.get(fallback_id, ())
                if not any(_member_is_effective(member) for member in fallback_members):
                    raise ModelPoolValidationError(
                        "启用模型池的后备模型池不能为空或没有可用成员"
                    )
