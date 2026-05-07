from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence


def _payload(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return dict(getattr(value, "__dict__", {}) or {})


def _groups(library_payload: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for key in ("patterns", "experience_families", "singletons"):
        for row in library_payload.get(key) or []:
            payload = _payload(row)
            if payload:
                payload["_bucket"] = key
                yield payload


def build_pool_coverage_report(
    library_payload: Dict[str, Any],
    per_case_rows: Sequence[Dict[str, Any]],
    *,
    source_library: str | None = None,
    source_per_case_log: str | None = None,
) -> Dict[str, Any]:
    groups = list(_groups(_payload(library_payload)))
    runtime_usable = [
        row
        for row in groups
        if bool(row.get("runtime_usable"))
    ]
    pooled_rows: List[Dict[str, Any]] = []
    for row in per_case_rows or []:
        feedback = _payload(_payload(row).get("feedback"))
        if str(feedback.get("action") or "").strip() == "pooled":
            pooled_rows.append(_payload(row))
    return {
        "source_library": source_library,
        "source_per_case_log": source_per_case_log,
        "runtime_pooled_count": len(pooled_rows),
        "final_instance_pool_size": 0,
        "final_instance_pool_case_ids": [],
        "instance_pool_by_repair_signature": [],
        "instance_pool_by_error_category": [],
        "instance_pool_by_blocked_reason": [],
        "pool_cluster_candidates": [],
        "pairwise_incompatibility_reasons": [],
        "softened_function_pair_count": 0,
        "theoretical_rescan_absorbable_count": 0,
        "theoretical_rescan_absorbable": [],
        "group_analyses": [],
        "library_group_count": len(groups),
        "runtime_usable_group_count": len(runtime_usable),
        "runtime_usable_family_count": sum(
            1 for row in runtime_usable if row.get("_bucket") == "experience_families"
        ),
        "runtime_usable_pattern_count": sum(
            1 for row in runtime_usable if row.get("_bucket") == "patterns"
        ),
    }
