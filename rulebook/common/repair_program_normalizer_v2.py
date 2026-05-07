"""Normalize case-level repair evidence into canonical repair IR.

The executable op in this layer must come from the audited case's extracted
``repair_program``. Role graphs, SQL deltas, and invariants are evidence used
for anti-unification; they must not be converted into pre-defined error types.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .data_structures_v2 import (
    CanonicalRepairIR,
    CanonicalRepairOp,
    CaseAudit,
    ErrorInstanceV2,
    RepairEffectSignature,
    RuntimeCaseView,
)
from .contrastive_repair_effect_v2 import discover_contrastive_repair_effects
from .role_graph_normalizer_v2 import RoleGraphNormalizer, role_refs_from_graph


def _payload(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return dict(getattr(value, "__dict__", {}) or {})


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return [str(value)] if str(value) else []


def _shape_delta(formation_signals: Optional[Dict[str, Any]], error_instance: ErrorInstanceV2) -> Dict[str, Any]:
    formation = formation_signals or {}
    delta = _payload(formation.get("delta"))
    shape = _payload(delta.get("output_shape_delta"))
    if not shape:
        structural = error_instance.repair_skeleton.structural
        shape_obj = getattr(structural, "output_shape_delta", None)
        shape = _payload(shape_obj)
    current = shape.get("current_arity")
    target = shape.get("target_arity")
    try:
        if current is not None and target is not None:
            current_i = int(current)
            target_i = int(target)
            shape["current_arity"] = current_i
            shape["target_arity"] = target_i
            delta_value = target_i - current_i
            shape["arity_delta"] = delta_value
            shape["arity_direction"] = (
                "increase" if delta_value > 0 else "decrease" if delta_value < 0 else "same"
            )
    except Exception:
        shape.setdefault("arity_direction", "unknown")
    return shape


def _table_delta(formation_signals: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    formation = formation_signals or {}
    delta = _payload(formation.get("delta"))
    return _payload(delta.get("table_set_delta"))


def _predicate_delta(formation_signals: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    formation = formation_signals or {}
    delta = _payload(formation.get("delta"))
    return {
        "predicate_count_delta": _payload(delta.get("predicate_count_delta")),
        "group_by_count_delta": _payload(delta.get("group_by_count_delta")),
        "order_by_count_delta": _payload(delta.get("order_by_count_delta")),
    }


def _target_output_refs(target_graph: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(target_graph.get("output_refs") or [])


def _source_output_refs(source_graph: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(source_graph.get("output_refs") or [])


def _output_shape(graph: Dict[str, Any]) -> Dict[str, Any]:
    return _payload(graph.get("output_shape"))


def _has_distinct_output(graph: Dict[str, Any]) -> bool:
    return bool(_output_shape(graph).get("has_distinct"))


def _role_summary(refs: List[Dict[str, Any]]) -> List[str]:
    return [str(ref.get("column_role") or "other") for ref in refs]


def _relation_key(relation: Dict[str, Any]) -> str:
    key = str(relation.get("canonical_key") or "").lower()
    if key:
        return key
    left = _payload(relation.get("left"))
    right = _payload(relation.get("right"))
    left_key = f"{str(left.get('table') or '').lower()}.{str(left.get('column') or '').lower()}"
    right_key = f"{str(right.get('table') or '').lower()}.{str(right.get('column') or '').lower()}"
    parts = sorted(part for part in (left_key, right_key) if part != ".")
    return "=".join(parts)


def _relation_role_key(relation: Dict[str, Any]) -> str:
    role_key = str(relation.get("role_key") or "")
    if role_key:
        return role_key
    left = _payload(relation.get("left"))
    right = _payload(relation.get("right"))
    return "=".join(
        sorted(
            role
            for role in (
                str(left.get("column_role") or ""),
                str(right.get("column_role") or ""),
            )
            if role
        )
    )


def _relation_invariants(
    *,
    source_graph: Dict[str, Any],
    target_graph: Dict[str, Any],
) -> List[str]:
    source_keys = {
        _relation_key(relation)
        for relation in source_graph.get("equality_relations") or []
        if _relation_key(relation)
    }
    invariants: List[str] = []
    seen: set[str] = set()
    for relation in target_graph.get("equality_relations") or []:
        key = _relation_key(relation)
        if not key or key in seen:
            continue
        seen.add(key)
        role_key = _relation_role_key(relation)
        invariants.append(f"target_relation_equality={key}")
        if role_key:
            invariants.append(f"target_relation_role_equality={role_key}")
        if key not in source_keys:
            invariants.append(f"target_added_relation_equality={key}")
    return sorted(set(invariants))


def _normalize_step_op(step: Dict[str, Any]) -> str:
    raw = str(step.get("op") or "").strip()
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").upper()
    return normalized


def _canonicalize_step_op(
    *,
    step: Dict[str, Any],
    locus: str,
    shape: Dict[str, Any],
    source_output_refs: List[Dict[str, Any]],
    target_output_refs: List[Dict[str, Any]],
) -> str:
    """Normalize the explicit case step into the minimal executable op.

    The op still originates from the extracted repair step. This only rewrites
    surface DSL labels when the audited SQL delta proves the actual operation
    is narrower. Example: an extractor may call a 2->1 projection repair
    "replace select slot"; if the target output is a subset of the source
    outputs, the executable canonical program is a drop/collapse of the extra
    output side, not a free replacement with any schema column.
    """
    op_type = _normalize_step_op(step)
    if (
        locus.upper() == "SELECT"
        and op_type in {"SELECT_REPLACE_SLOT", "REPLACE_SELECT_SLOT"}
        and str(shape.get("arity_direction") or "").lower() == "decrease"
        and _is_target_output_subset(
            source_output_refs=source_output_refs,
            target_output_refs=target_output_refs,
        )
    ):
        return "SELECT_DROP_SLOT"
    return op_type


def _is_explicit_case_step(step: Dict[str, Any]) -> bool:
    """Only explicit case-extracted repair steps can become executable ops."""
    if not _normalize_step_op(step):
        return False
    origin = str(step.get("origin") or "case_extracted")
    source = str(step.get("extraction_source") or "llm_explicit")
    return origin == "case_extracted" and source == "llm_explicit"


def _slot_signature(step: Dict[str, Any]) -> List[Dict[str, Any]]:
    slots: List[Dict[str, Any]] = []
    for slot in step.get("slots") or []:
        payload = _payload(slot)
        if not payload:
            continue
        slots.append(
            {
                "kind": str(payload.get("kind") or ""),
                "required": bool(payload.get("required", True)),
                "allowed_role_families": sorted(
                    str(role)
                    for role in (payload.get("allowed_role_families") or [])
                    if str(role)
                ),
            }
        )
    return sorted(slots, key=lambda item: (item["kind"], item["required"], item["allowed_role_families"]))


def _ref_identity(ref: Dict[str, Any]) -> str:
    table = str(ref.get("table") or "").lower()
    column = str(ref.get("column") or "").lower()
    expr = str(ref.get("expression") or "").lower()
    return f"{table}.{column}" if table or column else expr


def _is_target_output_subset(
    *,
    source_output_refs: List[Dict[str, Any]],
    target_output_refs: List[Dict[str, Any]],
) -> bool:
    source_keys = {_ref_identity(ref) for ref in source_output_refs if _ref_identity(ref)}
    target_keys = {_ref_identity(ref) for ref in target_output_refs if _ref_identity(ref)}
    return bool(source_keys and target_keys and target_keys <= source_keys)


def _output_refs_equivalent(
    source_output_refs: List[Dict[str, Any]],
    target_output_refs: List[Dict[str, Any]],
) -> bool:
    if len(source_output_refs) != len(target_output_refs):
        return False
    if not source_output_refs or not target_output_refs:
        return True
    for source_ref, target_ref in zip(source_output_refs, target_output_refs):
        source_key = _ref_identity(source_ref)
        target_key = _ref_identity(target_ref)
        if source_key and target_key and source_key == target_key:
            continue
        if str(source_ref.get("expression") or "").strip().lower() != str(
            target_ref.get("expression") or ""
        ).strip().lower():
            return False
    return True


def _output_delta_op_type(
    *,
    shape: Dict[str, Any],
    source_output_refs: List[Dict[str, Any]],
    target_output_refs: List[Dict[str, Any]],
) -> str:
    direction = str(shape.get("arity_direction") or "").lower()
    if direction == "decrease" and _is_target_output_subset(
        source_output_refs=source_output_refs,
        target_output_refs=target_output_refs,
    ):
        return "SELECT_DROP_SLOT"
    if direction == "increase":
        return "SELECT_ADD_SLOT"
    return "SELECT_REPLACE_SLOT"


def _inferred_output_delta_step(
    *,
    shape: Dict[str, Any],
    source_output_refs: List[Dict[str, Any]],
    target_output_refs: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Create a case-derived SELECT op from the audited output delta.

    This is not a predefined error label. It is a normalized representation of
    the concrete source/target projection delta in the current audited case, and
    is only emitted when the two SQL role graphs disagree on output slots.
    """
    if not source_output_refs or not target_output_refs:
        return None
    if _output_refs_equivalent(source_output_refs, target_output_refs):
        return None
    if not shape.get("arity_direction"):
        source_arity = len(source_output_refs)
        target_arity = len(target_output_refs)
        shape["current_arity"] = source_arity
        shape["target_arity"] = target_arity
        shape["arity_delta"] = target_arity - source_arity
        shape["arity_direction"] = (
            "increase"
            if target_arity > source_arity
            else "decrease"
            if target_arity < source_arity
            else "same"
        )
    return {
        "step_id": "sql_delta_output_projection",
        "op": _output_delta_op_type(
            shape=shape,
            source_output_refs=source_output_refs,
            target_output_refs=target_output_refs,
        ),
        "locus": "SELECT",
        "is_dependency": False,
        "required": True,
        "slots": [],
        "guards": ["source_target_output_slots_differ"],
        "arguments": {
            "source": "source_target_output_delta",
        },
        "origin": "case_extracted",
        "extraction_source": "sql_delta",
    }


def _aggregate_output_contract_changed(
    *,
    source_graph: Dict[str, Any],
    target_graph: Dict[str, Any],
    formation_signals: Optional[Dict[str, Any]],
) -> bool:
    source_shape = _output_shape(source_graph)
    target_shape = _output_shape(target_graph)
    if bool(source_shape.get("has_distinct")) != bool(target_shape.get("has_distinct")):
        return True
    if bool(source_shape.get("has_aggregate")) != bool(target_shape.get("has_aggregate")):
        return True
    axes = _as_list(_payload((formation_signals or {}).get("delta")).get("delta_axes"))
    return "aggregation_unit_delta" in axes or "grain_delta" in axes


def _inferred_output_contract_step(
    *,
    shape: Dict[str, Any],
    source_graph: Dict[str, Any],
    target_graph: Dict[str, Any],
    source_output_refs: List[Dict[str, Any]],
    target_output_refs: List[Dict[str, Any]],
    formation_signals: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Create a SELECT contract op when aggregate/output semantics differ.

    This is derived from the audited source/target output contract itself, not
    from a named database pattern. It covers cases where the concrete output
    column is the same but the answer unit changes, such as COUNT(DISTINCT x)
    versus COUNT(x).
    """
    if not source_output_refs or not target_output_refs:
        return None
    if not _aggregate_output_contract_changed(
        source_graph=source_graph,
        target_graph=target_graph,
        formation_signals=formation_signals,
    ):
        return None
    return {
        "step_id": "sql_delta_output_contract",
        "op": "SELECT_REPLACE_SLOT",
        "locus": "SELECT",
        "is_dependency": False,
        "required": True,
        "slots": [],
        "guards": ["source_target_output_contract_differs"],
        "arguments": {
            "source": "source_target_output_contract_delta",
            "source_output_shape": _output_shape(source_graph),
            "target_output_shape": _output_shape(target_graph),
        },
        "origin": "case_extracted",
        "extraction_source": "sql_delta",
    }


def _operation_signature(
    *,
    step: Dict[str, Any],
    op_type: str,
    locus: str,
    shape: Dict[str, Any],
    source_output_refs: List[Dict[str, Any]],
    target_output_refs: List[Dict[str, Any]],
    source_graph: Dict[str, Any],
    target_graph: Dict[str, Any],
    predicate_delta: Dict[str, Any],
) -> Dict[str, Any]:
    output_path_delta = _output_path_delta(
        source_output_refs=source_output_refs,
        target_output_refs=target_output_refs,
    )
    relation_delta = _relation_delta(
        source_graph=source_graph,
        target_graph=target_graph,
    )
    predicate_scope_delta = _predicate_scope_delta(
        source_graph=source_graph,
        target_graph=target_graph,
        predicate_delta=predicate_delta,
    )
    grain_delta = _grain_delta(
        shape=shape,
        source_graph=source_graph,
        target_graph=target_graph,
    )
    return {
        "step_op": op_type,
        "locus": locus,
        "is_dependency": bool(step.get("is_dependency") or False),
        "required": bool(step.get("required", True)),
        "slot_signature": _slot_signature(step),
        "role_delta": {
            "arity_direction": str(shape.get("arity_direction") or ""),
            "source_output_roles": _role_summary(source_output_refs),
            "target_output_roles": _role_summary(target_output_refs),
            "target_output_subset_of_source": _is_target_output_subset(
                source_output_refs=source_output_refs,
                target_output_refs=target_output_refs,
            ),
        },
        "output_path_delta": output_path_delta,
        "relation_delta": relation_delta,
        "predicate_scope_delta": predicate_scope_delta,
        "grain_delta": grain_delta,
    }


def _path_role_or_identity(ref: Dict[str, Any]) -> str:
    payload = _payload(ref)
    return (
        str(payload.get("derived_role_path") or "")
        or str(payload.get("direct_role_path") or "")
        or str(payload.get("role_side_group") or "")
        or str(payload.get("path_role") or "")
        or str(payload.get("relation_role") or "")
        or _ref_identity(payload)
    )


def _output_path_delta(
    *,
    source_output_refs: List[Dict[str, Any]],
    target_output_refs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    source_paths = [_path_role_or_identity(ref) for ref in source_output_refs if _path_role_or_identity(ref)]
    target_paths = [_path_role_or_identity(ref) for ref in target_output_refs if _path_role_or_identity(ref)]
    source_keys = {_ref_identity(ref) for ref in source_output_refs if _ref_identity(ref)}
    target_keys = {_ref_identity(ref) for ref in target_output_refs if _ref_identity(ref)}
    source_tables = {
        str(_payload(ref).get("table") or "").lower()
        for ref in source_output_refs
        if str(_payload(ref).get("table") or "")
    }
    source_columns = {
        str(_payload(ref).get("column") or "").lower()
        for ref in source_output_refs
        if str(_payload(ref).get("column") or "")
    }
    source_side_groups = sorted(
        {
            str(_payload(ref).get("role_side_group") or "")
            for ref in source_output_refs
            if str(_payload(ref).get("role_side_group") or "")
        }
    )
    target_side_groups = sorted(
        {
            str(_payload(ref).get("role_side_group") or "")
            for ref in target_output_refs
            if str(_payload(ref).get("role_side_group") or "")
        }
    )
    source_side_keys = sorted(
        {
            str(_payload(ref).get("side_key") or "")
            for ref in source_output_refs
            if str(_payload(ref).get("side_key") or "")
        }
    )
    target_side_keys = sorted(
        {
            str(_payload(ref).get("side_key") or "")
            for ref in target_output_refs
            if str(_payload(ref).get("side_key") or "")
        }
    )
    source_direct_paths = sorted(
        {
            str(_payload(ref).get("direct_role_path") or "")
            for ref in source_output_refs
            if str(_payload(ref).get("direct_role_path") or "")
        }
    )
    target_direct_paths = sorted(
        {
            str(_payload(ref).get("direct_role_path") or "")
            for ref in target_output_refs
            if str(_payload(ref).get("direct_role_path") or "")
        }
    )
    source_derived_paths = sorted(
        {
            str(_payload(ref).get("derived_role_path") or "")
            for ref in source_output_refs
            if str(_payload(ref).get("derived_role_path") or "")
        }
    )
    target_derived_paths = sorted(
        {
            str(_payload(ref).get("derived_role_path") or "")
            for ref in target_output_refs
            if str(_payload(ref).get("derived_role_path") or "")
        }
    )
    kept = sorted(source_keys & target_keys)
    dropped = sorted(source_keys - target_keys)
    added = sorted(target_keys - source_keys)
    return {
        "source_output_path_roles": source_paths,
        "target_output_path_roles": target_paths,
        "source_role_side_groups": source_side_groups,
        "target_role_side_groups": target_side_groups,
        "source_side_keys": source_side_keys,
        "target_side_keys": target_side_keys,
        "source_direct_role_paths": source_direct_paths,
        "target_direct_role_paths": target_direct_paths,
        "source_derived_role_paths": source_derived_paths,
        "target_derived_role_paths": target_derived_paths,
        "kept_output_refs": kept,
        "dropped_output_refs": dropped,
        "added_output_refs": added,
        "target_output_subset_of_source": _is_target_output_subset(
            source_output_refs=source_output_refs,
            target_output_refs=target_output_refs,
        ),
        "same_table_multi_role_output": len(source_tables) == 1 and len(source_output_refs) > 1,
        "same_attribute_multi_role_output": len(source_columns) == 1 and len(source_output_refs) > 1,
    }


def _repair_effect_signature(
    *,
    shape: Dict[str, Any],
    source_graph: Dict[str, Any],
    target_graph: Dict[str, Any],
    source_output_refs: List[Dict[str, Any]],
    target_output_refs: List[Dict[str, Any]],
    predicate_delta: Dict[str, Any],
) -> Dict[str, Any]:
    output_path_delta = _output_path_delta(
        source_output_refs=source_output_refs,
        target_output_refs=target_output_refs,
    )
    relation_delta = _relation_delta(
        source_graph=source_graph,
        target_graph=target_graph,
    )
    predicate_scope_delta = _predicate_scope_delta(
        source_graph=source_graph,
        target_graph=target_graph,
        predicate_delta=predicate_delta,
    )
    grain_delta = _grain_delta(
        shape=shape,
        source_graph=source_graph,
        target_graph=target_graph,
    )
    direction = str(shape.get("arity_direction") or "").lower()
    output_kind = ""
    if direction == "decrease" and output_path_delta.get("target_output_subset_of_source"):
        output_kind = "output_subset"
    elif direction == "increase":
        output_kind = "output_expand"
    elif source_output_refs and target_output_refs and not _output_refs_equivalent(
        source_output_refs,
        target_output_refs,
    ):
        output_kind = "output_replace"
    relation_kind = ""
    if relation_delta.get("added_relation_equalities"):
        relation_kind = "add_relation"
    elif relation_delta.get("removed_relation_equalities"):
        relation_kind = "remove_relation"
    predicate_kind = ""
    if predicate_scope_delta.get("possible_scope_move"):
        predicate_kind = "predicate_move"
    elif predicate_scope_delta.get("added_target_predicates"):
        predicate_kind = "predicate_add"
    elif predicate_scope_delta.get("removed_source_predicates"):
        predicate_kind = "predicate_drop"
    grain_kind = "grain_change" if grain_delta.get("grain_changed") else ""
    source_keys = sorted(_ref_identity(ref) for ref in source_output_refs if _ref_identity(ref))
    target_keys = sorted(_ref_identity(ref) for ref in target_output_refs if _ref_identity(ref))
    field_kind = ""
    if source_keys and target_keys and source_keys != target_keys and shape.get("arity_direction") in {"same", "", None}:
        field_kind = "field_switch"
    ranking_kind = ""
    return {
        "output_effect": {
            "kind": output_kind,
            "source_arity": shape.get("current_arity"),
            "target_arity": shape.get("target_arity"),
            "target_is_subset_of_source": bool(
                output_path_delta.get("target_output_subset_of_source")
            ),
            "kept_output_refs": output_path_delta.get("kept_output_refs") or [],
            "dropped_output_refs": output_path_delta.get("dropped_output_refs") or [],
            "source_role_side_groups": output_path_delta.get("source_role_side_groups") or [],
            "target_role_side_groups": output_path_delta.get("target_role_side_groups") or [],
        },
        "relation_effect": {
            "kind": relation_kind,
            "source_relation_paths": relation_delta.get("source_relation_equalities") or [],
            "target_relation_paths": relation_delta.get("target_relation_equalities") or [],
            "added_relation_equalities": relation_delta.get("added_relation_equalities") or [],
            "removed_relation_equalities": relation_delta.get("removed_relation_equalities") or [],
            "target_scope_key": relation_delta.get("target_scope_relation_key"),
        },
        "predicate_scope_effect": {
            "kind": predicate_kind,
            "source_scope": "WHERE",
            "target_scope": "WHERE" if not predicate_scope_delta.get("possible_scope_move") else "CASE_OR_HAVING",
            "denominator_preserved": bool(
                predicate_scope_delta.get("possible_scope_move")
            ),
            **predicate_scope_delta,
        },
        "grain_effect": {
            "kind": grain_kind,
            **grain_delta,
        },
        "field_binding_effect": {
            "kind": field_kind,
            "source_output_refs": source_keys,
            "target_output_refs": target_keys,
            "source_output_roles": _role_summary(source_output_refs),
            "target_output_roles": _role_summary(target_output_refs),
        },
        "ranking_effect": {
            "kind": ranking_kind,
        },
    }


def _relation_delta(
    *,
    source_graph: Dict[str, Any],
    target_graph: Dict[str, Any],
) -> Dict[str, Any]:
    source_relations = [_payload(item) for item in (source_graph.get("equality_relations") or [])]
    target_relations = [_payload(item) for item in (target_graph.get("equality_relations") or [])]
    source_keys = sorted({_relation_key(item) for item in source_relations if _relation_key(item)})
    target_keys = sorted({_relation_key(item) for item in target_relations if _relation_key(item)})
    source_set = set(source_keys)
    target_set = set(target_keys)
    role_equalities = sorted(
        {
            _relation_role_key(item)
            for item in target_relations
            if _relation_role_key(item)
        }
    )
    return {
        "source_relation_equalities": source_keys,
        "target_relation_equalities": target_keys,
        "added_relation_equalities": sorted(target_set - source_set),
        "removed_relation_equalities": sorted(source_set - target_set),
        "target_relation_role_equalities": role_equalities,
    }


def _predicate_scope_delta(
    *,
    source_graph: Dict[str, Any],
    target_graph: Dict[str, Any],
    predicate_delta: Dict[str, Any],
) -> Dict[str, Any]:
    source_predicates = [
        _normalize_predicate_text(predicate)
        for predicate in (source_graph.get("predicates") or [])
        if _normalize_predicate_text(predicate)
    ]
    target_predicates = [
        _normalize_predicate_text(predicate)
        for predicate in (target_graph.get("predicates") or [])
        if _normalize_predicate_text(predicate)
    ]
    source_set = set(source_predicates)
    target_set = set(target_predicates)
    predicate_count_delta = _payload(predicate_delta.get("predicate_count_delta"))
    return {
        "source_scope": "WHERE" if source_predicates else None,
        "target_scope": "WHERE" if target_predicates else None,
        "source_predicate_signatures": source_predicates,
        "target_predicate_signatures": target_predicates,
        "removed_source_predicates": sorted(source_set - target_set),
        "added_target_predicates": sorted(target_set - source_set),
        "predicate_count_delta": predicate_count_delta,
        "possible_scope_move": bool(source_predicates and source_set != target_set),
    }


def _grain_delta(
    *,
    shape: Dict[str, Any],
    source_graph: Dict[str, Any],
    target_graph: Dict[str, Any],
) -> Dict[str, Any]:
    source_shape = _output_shape(source_graph)
    target_shape = _output_shape(target_graph)
    current_grain = shape.get("current_grain") or source_shape.get("grain")
    target_grain = shape.get("target_grain") or target_shape.get("grain")
    return {
        "source_grain": current_grain,
        "target_grain": target_grain,
        "current_arity": shape.get("current_arity") or source_shape.get("arity"),
        "target_arity": shape.get("target_arity") or target_shape.get("arity"),
        "arity_direction": shape.get("arity_direction"),
        "source_has_aggregate": bool(source_shape.get("has_aggregate")),
        "target_has_aggregate": bool(target_shape.get("has_aggregate")),
        "source_has_distinct": bool(source_shape.get("has_distinct")),
        "target_has_distinct": bool(target_shape.get("has_distinct")),
        "grain_changed": bool(current_grain and target_grain and str(current_grain) != str(target_grain)),
    }


def _distinct_accessory_policy(
    *,
    source_graph: Dict[str, Any],
    target_graph: Dict[str, Any],
    shape: Dict[str, Any],
    step: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Infer a conditional DISTINCT dependency from case-extracted evidence.

    The signal is answer-aware but learned offline from the audited case's own
    source/target SQL. It does not name any database, table, column, or qid.
    """
    if not _has_distinct_output(target_graph) or _has_distinct_output(source_graph):
        return None
    if str(shape.get("arity_direction") or "").lower() not in {"same", "decrease"}:
        return None
    step_args = _payload(step.get("arguments"))
    step_text = " ".join(
        [
            str(step.get("op") or ""),
            str(step_args.get("replacement") or ""),
            str(step_args.get("new_column") or ""),
            " ".join(str(item) for item in (step.get("source_evidence") or [])),
        ]
    ).lower()
    if "distinct" not in step_text and not _has_distinct_output(target_graph):
        return None
    return {
        "op": "SELECT_ENFORCE_DISTINCT",
        "locus": "SELECT",
        "is_dependency": True,
        "required": False,
        "policy": "conditional_target_distinct",
        "guards": [
            "target_sql_has_distinct",
            "source_sql_not_distinct",
            "apply_only_with_bound_select_repair",
        ],
        "source_output_shape": _output_shape(source_graph),
        "target_output_shape": _output_shape(target_graph),
    }


def _normalize_predicate_text(predicate: str) -> str:
    text = str(predicate or "").strip().lower()
    text = re.sub(r"[`\"\[\]]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _top_level_offset(text: str, offset: int) -> bool:
    depth = 0
    for char in str(text or "")[:offset]:
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
    return depth == 0


def _sql_alias_map(sql: str) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    pattern = re.compile(
        r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_\"`\[\].]*)"
        r"(?:\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*))?",
        re.IGNORECASE,
    )
    reserved = {"on", "where", "inner", "left", "right", "full", "cross", "join", "order", "limit"}
    masked = []
    depth = 0
    for char in str(sql or ""):
        if char == "(":
            depth += 1
            masked.append(" ")
            continue
        if char == ")" and depth:
            depth -= 1
            masked.append(" ")
            continue
        masked.append(char if depth == 0 else " ")
    for match in pattern.finditer("".join(masked)):
        table = str(match.group(1) or "").strip().strip('"`[]').rsplit(".", 1)[-1]
        alias = str(match.group(2) or table).strip().strip('"`[]')
        if not table:
            continue
        if alias.lower() in reserved:
            alias = table
        aliases[alias.lower()] = table
        aliases.setdefault(table.lower(), table)
    return aliases


def _limit_literal(sql: str) -> Optional[str]:
    text = str(sql or "")
    for match in re.finditer(r"\blimit\s+(\d+)\b", text, flags=re.IGNORECASE):
        if _top_level_offset(text, match.start()):
            return match.group(1)
    return None


def _order_ref_payloads(expr: str, sql: str) -> List[Dict[str, Any]]:
    alias_map = _sql_alias_map(sql)
    refs: List[Dict[str, Any]] = []
    pattern = re.compile(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*"
        r"(?:\"([^\"]+)\"|`([^`]+)`|\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_]*))\b"
    )
    for match in pattern.finditer(str(expr or "")):
        table_alias = str(match.group(1) or "")
        resolved_column = next(item for item in match.groups()[1:] if item)
        table = alias_map.get(table_alias.lower(), table_alias)
        refs.append(
            {
                "table": table,
                "column": resolved_column,
                "expression": str(expr),
            }
        )
    if refs:
        return refs
    match = re.search(
        r"(?:\"([^\"]+)\"|`([^`]+)`|\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_]*))\s*$",
        str(expr or "").strip(),
    )
    if match:
        column = next(item for item in match.groups() if item)
        refs.append({"table": None, "column": column, "expression": str(expr)})
    return refs


def _order_by_rows(sql: str) -> List[Dict[str, Any]]:
    from .structure_family import cached_ast_signature

    ast = cached_ast_signature(sql or "") or {}
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(ast.get("order_by") or []):
        payload = _payload(item)
        expr = str(payload.get("column") or payload.get("expression") or "").strip()
        if not expr:
            continue
        direction = str(payload.get("direction") or "ASC").strip().upper() or "ASC"
        refs = _order_ref_payloads(expr, sql)
        normalized_expr = _normalize_predicate_text(expr)
        key = f"{normalized_expr}|{direction}"
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "expression": expr,
                "normalized_expression": normalized_expr,
                "direction": direction,
                "slot_index": index,
                "refs": refs,
            }
        )
    return rows


def _order_signature(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    signature: List[Dict[str, Any]] = []
    for row in rows:
        refs = [
            {
                "table": str(_payload(ref).get("table") or "").lower(),
                "column": str(_payload(ref).get("column") or "").lower(),
            }
            for ref in row.get("refs") or []
        ]
        signature.append(
            {
                "expression": row.get("normalized_expression") or row.get("expression"),
                "direction": str(row.get("direction") or "").upper(),
                "refs": refs,
            }
        )
    return signature


def _target_ranking_accessory_policies(
    *,
    source_sql: str,
    target_sql: str,
) -> List[Dict[str, Any]]:
    source_order = _order_by_rows(source_sql)
    target_order = _order_by_rows(target_sql)
    source_limit = _limit_literal(source_sql)
    target_limit = _limit_literal(target_sql)
    order_changed = bool(target_order) and _order_signature(source_order) != _order_signature(target_order)
    limit_changed = target_limit is not None and target_limit != source_limit
    if not order_changed and not limit_changed:
        return []
    policies: List[Dict[str, Any]] = []
    if order_changed:
        policies.append(
            {
                "op": "ORDER_BY_APPLY",
                "locus": "ORDER_BY",
                "is_dependency": True,
                "required": False,
                "policy": "target_ranking_contract",
                "guards": [
                    "target_sql_order_by_differs_from_source_sql",
                    "apply_only_with_bound_core_repair",
                ],
                "target_order_by": target_order[:4],
                "target_limit": target_limit,
            }
        )
    if target_limit is not None and (limit_changed or order_changed):
        policies.append(
            {
                "op": "LIMIT_APPLY",
                "locus": "LIMIT",
                "is_dependency": True,
                "required": False,
                "policy": "target_ranking_contract",
                "guards": [
                    "target_sql_limit_required_by_ranking_contract",
                    "apply_only_with_bound_core_repair",
                ],
                "target_limit": target_limit,
                "target_order_by": target_order[:4],
            }
        )
    source_graph = (
        RoleGraphNormalizer().normalize_sql(
            sql=source_sql,
            schema_view=None,
            source="pred_sql",
        )
        if source_sql
        else {}
    )
    source_predicates = _source_extreme_ranking_predicates(
        source_graph=source_graph,
        source_sql=source_sql,
        target_order=target_order,
    )
    if source_predicates and order_changed and target_limit is not None:
        policies.append(
            {
                "op": "WHERE_DROP_RANKING_PREDICATE",
                "locus": "WHERE",
                "is_dependency": True,
                "required": False,
                "policy": "source_extreme_predicate_replaced_by_target_ranking",
                "guards": [
                    "source_sql_uses_extreme_predicate_for_target_order_column",
                    "target_sql_uses_order_by_limit_ranking",
                    "apply_only_with_bound_core_repair",
                ],
                "source_ranking_predicates": source_predicates[:4],
                "target_order_by": target_order[:4],
                "target_limit": target_limit,
            }
        )
    return policies


def _source_extreme_ranking_predicates(
    *,
    source_graph: Dict[str, Any],
    source_sql: str,
    target_order: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    source_tables = {
        str(table).strip().lower()
        for table in (source_graph.get("tables") or [])
        if str(table).strip()
    }
    alias_map = _sql_alias_map(source_sql)
    order_refs = [
        _payload(ref)
        for row in target_order
        for ref in (row.get("refs") or [])
        if _payload(ref).get("table") and _payload(ref).get("column")
    ]
    if not order_refs:
        return rows
    for predicate in source_graph.get("predicates") or []:
        predicate_text = str(predicate)
        for alias, table in alias_map.items():
            predicate_text = re.sub(
                rf"\b{re.escape(alias)}\.([A-Za-z_][A-Za-z0-9_]*)\b",
                lambda match, table=table: f"{table}.{match.group(1)}",
                predicate_text,
                flags=re.IGNORECASE,
            )
        normalized = _normalize_predicate_text(predicate_text)
        aggregate = ""
        if re.search(r"\bmin\s*\(", normalized):
            aggregate = "MIN"
        elif re.search(r"\bmax\s*\(", normalized):
            aggregate = "MAX"
        if not aggregate:
            continue
        matched_refs = []
        for ref in order_refs:
            table = str(ref.get("table") or "").lower()
            column = str(ref.get("column") or "")
            column_l = column.lower()
            qualified = f"{table}.{column_l}"
            if table and column_l and qualified in normalized:
                matched_refs.append(ref)
            elif (
                table
                and column_l
                and source_tables == {table}
                and re.search(rf"\b{re.escape(column_l)}\b", normalized)
            ):
                matched_refs.append(ref)
        if not matched_refs:
            continue
        rows.append(
            {
                "predicate": str(predicate),
                "normalized_predicate": normalized,
                "aggregate": aggregate,
                "refs": matched_refs,
            }
        )
    return rows


def _predicate_ref_payloads_for_text(
    graph: Dict[str, Any],
    predicate: str,
) -> List[Dict[str, Any]]:
    wanted = _normalize_predicate_text(predicate)
    refs: List[Dict[str, Any]] = []
    for ref in graph.get("predicate_refs") or []:
        payload = _payload(ref)
        evidence = _payload(payload.get("evidence"))
        ref_predicate = _normalize_predicate_text(evidence.get("predicate") or "")
        if wanted and ref_predicate and ref_predicate != wanted:
            continue
        refs.append(
            {
                "table": payload.get("table"),
                "column": payload.get("column"),
                "expression": payload.get("expression"),
                "column_role": payload.get("column_role"),
                "relation_role": payload.get("relation_role"),
            }
        )
    return refs


def _predicate_kind(predicate: str) -> str:
    text = _normalize_predicate_text(predicate)
    if re.search(r"\bis\s+not\s+null\b|\bnot\b.*\bis\s+null\b", text):
        return "is_not_null"
    if re.search(r"\bis\s+null\b", text):
        return "is_null"
    if re.search(r"\bbetween\b", text):
        return "between"
    if re.search(r"\blike\b", text):
        return "like"
    for operator in (">=", "<=", "!=", "<>", "=", ">", "<"):
        if operator in text:
            return f"comparison:{operator}"
    return "predicate"


def _predicate_signature(graph: Dict[str, Any], predicate: str) -> str:
    refs = _predicate_ref_payloads_for_text(graph, predicate)
    ref_keys = sorted(
        f"{str(ref.get('table') or '').lower()}.{str(ref.get('column') or '').lower()}"
        for ref in refs
        if str(ref.get("column") or "")
    )
    if ref_keys:
        return "|".join([_predicate_kind(predicate), ",".join(ref_keys)])
    return _normalize_predicate_text(predicate)


def _target_only_predicates(
    *,
    source_graph: Dict[str, Any],
    target_graph: Dict[str, Any],
) -> List[Dict[str, Any]]:
    source_keys = {
        _predicate_signature(source_graph, predicate)
        for predicate in (source_graph.get("predicates") or [])
        if _predicate_signature(source_graph, predicate)
    }
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for predicate in target_graph.get("predicates") or []:
        key = _predicate_signature(target_graph, predicate)
        if not key or key in source_keys or key in seen:
            continue
        refs = _predicate_ref_payloads_for_text(target_graph, str(predicate))
        if not refs:
            continue
        seen.add(key)
        rows.append(
            {
                "predicate": str(predicate),
                "normalized_predicate": _normalize_predicate_text(predicate),
                "predicate_signature": key,
                "refs": refs,
            }
        )
    return rows


def _target_predicate_accessory_policy(
    *,
    source_graph: Dict[str, Any],
    target_graph: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Infer concrete WHERE dependencies from audited target-only predicates.

    The policy is case-derived: it records predicates present in the validated
    SQL and absent from the source SQL. Runtime may use it only when the
    compiler selects the corresponding member variant.
    """
    target_only = _target_only_predicates(
        source_graph=source_graph,
        target_graph=target_graph,
    )
    if not target_only:
        return None
    return {
        "op": "WHERE_ADD_CONDITION",
        "locus": "WHERE",
        "is_dependency": True,
        "required": False,
        "policy": "target_only_predicate_constraint",
        "guards": [
            "target_sql_predicate_absent_from_source_sql",
            "apply_only_with_bound_core_repair",
        ],
        "target_predicates": target_only[:6],
    }


def _op_audit_payload(op: CanonicalRepairOp, step: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "op_id": op.op_id,
        "op_type": op.op_type,
        "locus": op.locus,
        "source_step_ids": list(op.source_step_ids or []),
        "supporting_case_ids": list(op.supporting_case_ids or []),
        "required": bool(step.get("required", True)),
        "is_dependency": bool(step.get("is_dependency") or False),
        "extraction_source": str(step.get("extraction_source") or ""),
        "origin": str(step.get("origin") or ""),
    }


class RepairProgramNormalizer:
    """Build a canonical case-level program from audit, SQL roles, and repair steps."""

    def __init__(self) -> None:
        self.role_graph_normalizer = RoleGraphNormalizer()

    def normalize_error_instance(
        self,
        *,
        error_instance: ErrorInstanceV2,
        case_audit: Optional[CaseAudit] = None,
        runtime_case_view: Optional[RuntimeCaseView] = None,
        formation_signals: Optional[Dict[str, Any]] = None,
    ) -> CanonicalRepairIR:
        pred_sql = ""
        target_sql = ""
        schema_view = None
        if runtime_case_view is not None:
            pred_sql = runtime_case_view.pred_manifestation.top1_sql
            schema_view = runtime_case_view.local_schema_view
        if case_audit is not None:
            pred_sql = pred_sql or case_audit.pred_sql
            # Prefer the audited minimal-fix SQL. Gold is an offline fallback
            # only; using it first can import unrelated benchmark-side edits
            # into the canonical repair program.
            target_sql = case_audit.validated_sql or case_audit.gold_sql
        source_graph = self.role_graph_normalizer.normalize_sql(
            sql=pred_sql,
            schema_view=schema_view,
            source="pred_sql",
        )
        target_graph = self.role_graph_normalizer.normalize_sql(
            sql=target_sql,
            schema_view=schema_view,
            source="target_sql",
        ) if target_sql else {}

        skeleton = _payload(error_instance.repair_skeleton)
        structural = _payload(skeleton.get("structural"))
        shape = _shape_delta(formation_signals, error_instance)
        table_delta = _table_delta(formation_signals)
        predicate_delta = _predicate_delta(formation_signals)
        formation_delta = _payload((formation_signals or {}).get("delta"))
        delta_axes = _as_list(formation_delta.get("delta_axes"))
        all_steps = [
            _payload(step)
            for step in (error_instance.repair_program or [])
            if _payload(step)
        ]
        steps = [step for step in all_steps if _is_explicit_case_step(step)]

        source_output_refs = _source_output_refs(source_graph)
        target_output_refs = _target_output_refs(target_graph)
        repair_effect_signature = _repair_effect_signature(
            shape=shape,
            source_graph=source_graph,
            target_graph=target_graph,
            source_output_refs=source_output_refs,
            target_output_refs=target_output_refs,
            predicate_delta=predicate_delta,
        )
        effect_candidates = discover_contrastive_repair_effects(
            db_id=error_instance.db_id,
            case_id=str(error_instance.case_id),
            shape=shape,
            source_graph=source_graph,
            target_graph=target_graph,
            source_output_refs=source_output_refs,
            target_output_refs=target_output_refs,
            legacy_effect_signature=repair_effect_signature,
            predicate_delta=predicate_delta,
            table_delta=table_delta,
            delta_axes=delta_axes,
            possible_effect_axes=list(error_instance.possible_effect_axes or []),
            effect_axis_hint=(
                getattr(case_audit, "effect_axis_hint", None)
                if case_audit is not None
                else None
            ),
        )
        repair_effect_signature["effect_candidates"] = [
            effect.model_dump(mode="json") for effect in effect_candidates
        ]
        role_refs = role_refs_from_graph(source_graph, sections=("output_refs", "predicate_refs", "join_refs"))
        target_role_refs = role_refs_from_graph(target_graph, sections=("output_refs",))
        all_role_refs = role_refs + target_role_refs

        ops: List[CanonicalRepairOp] = []
        core_ops: List[Dict[str, Any]] = []
        accessory_ops: List[Dict[str, Any]] = []

        def add_canonical_step(step: Dict[str, Any], index: int) -> None:
            locus = str(step.get("locus") or structural.get("locus") or "").upper()
            op_type = _canonicalize_step_op(
                step=step,
                locus=locus,
                shape=shape,
                source_output_refs=source_output_refs,
                target_output_refs=target_output_refs,
            )
            invariants = self._derive_invariants(
                shape=shape,
                source_graph=source_graph,
                target_graph=target_graph,
                predicate_delta=predicate_delta,
                source_output_refs=source_output_refs,
                target_output_refs=target_output_refs,
            )
            op = CanonicalRepairOp(
                op_id=f"{error_instance.case_id}:canonical:{index}",
                op_type=op_type,
                locus=locus or str(structural.get("locus") or ""),
                role_refs=all_role_refs,
                arguments={
                    "source_step_id": str(step.get("step_id") or f"step_{index}"),
                    "source_op": step.get("op"),
                    "repair_locus": locus,
                    "skeleton": structural,
                    "operation_signature": _operation_signature(
                        step=step,
                        op_type=op_type,
                        locus=locus,
                        shape=shape,
                        source_output_refs=source_output_refs,
                        target_output_refs=target_output_refs,
                        source_graph=source_graph,
                        target_graph=target_graph,
                        predicate_delta=predicate_delta,
                    ),
                    "output_shape_delta": shape,
                    "source_output_shape": _output_shape(source_graph),
                    "target_output_shape": _output_shape(target_graph),
                    "source_output_roles": _role_summary(source_output_refs),
                    "target_output_roles": _role_summary(target_output_refs),
                    "source_output_refs": source_output_refs,
                    "target_output_refs": target_output_refs,
                    "table_set_delta": table_delta,
                    "predicate_delta": predicate_delta,
                    "repair_effect_signature": repair_effect_signature,
                    "repair_insight_signature": (
                        error_instance.repair_insight_signature.model_dump(mode="json")
                        if error_instance.repair_insight_signature is not None
                        else {}
                    ),
                    "target_invariants": _relation_invariants(
                        source_graph=source_graph,
                        target_graph=target_graph,
                    ),
                    "source_equality_relations": list(source_graph.get("equality_relations") or []),
                    "target_equality_relations": list(target_graph.get("equality_relations") or []),
                    "step_slots": _slot_signature(step),
                    "step_arguments": _payload(step.get("arguments")),
                    "accessory_policies": [
                        policy
                        for policy in [
                            _distinct_accessory_policy(
                                source_graph=source_graph,
                                target_graph=target_graph,
                                shape=shape,
                                step=step,
                            ),
                            _target_predicate_accessory_policy(
                                source_graph=source_graph,
                                target_graph=target_graph,
                            ),
                            *_target_ranking_accessory_policies(
                                source_sql=pred_sql,
                                target_sql=target_sql,
                            ),
                        ]
                        if policy
                    ],
                },
                invariants=invariants,
                source_step_ids=[str(step.get("step_id") or f"step_{index}")],
                supporting_case_ids=[str(error_instance.case_id)],
                confidence=1.0,
            )
            ops.append(op)
            audit_payload = _op_audit_payload(op, step)
            if bool(step.get("required", True)) and not bool(step.get("is_dependency") or False):
                core_ops.append(audit_payload)
            else:
                accessory_ops.append(audit_payload)
            for policy_index, policy in enumerate(
                _payload(op.arguments).get("accessory_policies") or [],
                start=1,
            ):
                accessory_ops.append(
                    {
                        "op_id": f"{error_instance.case_id}:canonical:accessory:{policy_index}",
                        "op_type": str(policy.get("op") or "SELECT_ENFORCE_DISTINCT"),
                        "locus": str(policy.get("locus") or "SELECT"),
                        "source_step_ids": ["inferred_accessory_policy"],
                        "supporting_case_ids": [str(error_instance.case_id)],
                        "required": False,
                        "is_dependency": True,
                        "extraction_source": "case_inferred_from_target_sql",
                        "origin": "case_extracted",
                    }
                )

        for index, step in enumerate(steps):
            add_canonical_step(step, index)

        has_select_core = any(str(op.locus or "").upper() == "SELECT" for op in ops)
        inferred_output_step = _inferred_output_delta_step(
            shape=shape,
            source_output_refs=source_output_refs,
            target_output_refs=target_output_refs,
        )
        if inferred_output_step is not None and not has_select_core:
            add_canonical_step(inferred_output_step, len(ops))
        inferred_contract_step = _inferred_output_contract_step(
            shape=shape,
            source_graph=source_graph,
            target_graph=target_graph,
            source_output_refs=source_output_refs,
            target_output_refs=target_output_refs,
            formation_signals=formation_signals,
        )
        has_select_core = any(str(op.locus or "").upper() == "SELECT" for op in ops)
        if inferred_contract_step is not None and not has_select_core:
            add_canonical_step(inferred_contract_step, len(ops))

        warnings: List[str] = []
        if not pred_sql:
            warnings.append("missing_pred_sql")
        if not target_sql:
            warnings.append("missing_target_sql")
        if not all_steps:
            warnings.append("missing_repair_program")
        elif not steps:
            warnings.append("missing_explicit_repair_step")
        if not ops:
            warnings.append("no_canonical_ops")
        if not effect_candidates:
            warnings.append("insufficient_contrast_evidence")
        target_invariants = sorted(
            set(_relation_invariants(source_graph=source_graph, target_graph=target_graph))
            | {item for op in ops for item in op.invariants if item.startswith("target_")}
        )
        return CanonicalRepairIR(
            db_id=error_instance.db_id,
            case_id=str(error_instance.case_id),
            source_role_graph=source_graph,
            target_role_graph=target_graph,
            program_ops=ops,
            core_ops=core_ops,
            accessory_ops=accessory_ops,
            repair_effect_signature=RepairEffectSignature.model_validate(
                repair_effect_signature
            ),
            repair_insight_signature=error_instance.repair_insight_signature,
            target_invariants=target_invariants,
            invariants=sorted({item for op in ops for item in op.invariants}),
            unresolved_variation_axes=[],
            normalizer_warnings=warnings,
        )

    def _derive_invariants(
        self,
        *,
        shape: Dict[str, Any],
        source_graph: Dict[str, Any],
        target_graph: Dict[str, Any],
        predicate_delta: Dict[str, Any],
        source_output_refs: List[Dict[str, Any]],
        target_output_refs: List[Dict[str, Any]],
    ) -> List[str]:
        invariants: List[str] = []
        direction = str(shape.get("arity_direction") or "")
        if direction:
            invariants.append(f"output_arity_direction={direction}")
        if _is_target_output_subset(
            source_output_refs=source_output_refs,
            target_output_refs=target_output_refs,
        ):
            invariants.append("target_output_subset_of_source_outputs")
        source_output_arity = int(_payload(source_graph.get("output_shape")).get("arity") or 0)
        target_output_arity = int(_payload(target_graph.get("output_shape")).get("arity") or 0)
        if source_output_arity or target_output_arity:
            invariants.append(f"source_output_arity={source_output_arity}")
            invariants.append(f"target_output_arity={target_output_arity}")
        source_tables = set(str(table).lower() for table in source_graph.get("tables") or [])
        target_tables = set(str(table).lower() for table in target_graph.get("tables") or [])
        if target_tables - source_tables:
            invariants.append("target_requires_additional_table")
        if len(target_graph.get("join_edges") or []) > len(source_graph.get("join_edges") or []):
            invariants.append("target_join_path_expanded")
        invariants.extend(
            _relation_invariants(
                source_graph=source_graph,
                target_graph=target_graph,
            )
        )
        if source_graph.get("predicates"):
            invariants.append("source_has_predicate_scope")
        if _has_distinct_output(target_graph) and not _has_distinct_output(source_graph):
            invariants.append("target_requires_distinct_output")
        if _has_distinct_output(source_graph) and not _has_distinct_output(target_graph):
            invariants.append("target_drops_distinct_output")
        if _payload(source_graph.get("output_shape")).get("has_aggregate") != _payload(
            target_graph.get("output_shape")
        ).get("has_aggregate"):
            invariants.append("aggregate_output_contract_changed")
        if predicate_delta.get("predicate_count_delta"):
            invariants.append("predicate_count_changed")
        source_grain = _payload(source_graph.get("output_shape")).get("grain")
        target_grain = _payload(target_graph.get("output_shape")).get("grain")
        if source_grain:
            invariants.append(f"source_output_grain={source_grain}")
        if target_grain:
            invariants.append(f"target_output_grain={target_grain}")
        if source_grain and target_grain and str(source_grain) != str(target_grain):
            invariants.append("grain_changed")
        target_roles = _role_summary(_target_output_refs(target_graph))
        source_roles = _role_summary(_source_output_refs(source_graph))
        if target_roles:
            invariants.append("target_output_roles=" + ",".join(target_roles))
        if source_roles:
            invariants.append("source_output_roles=" + ",".join(source_roles))
        return sorted(set(invariants))


def attach_canonical_repair_ir(
    *,
    error_instance: ErrorInstanceV2,
    case_audit: Optional[CaseAudit] = None,
    runtime_case_view: Optional[RuntimeCaseView] = None,
    formation_signals: Optional[Dict[str, Any]] = None,
) -> ErrorInstanceV2:
    """Return ``error_instance`` with canonical_repair_ir populated."""
    if error_instance.canonical_repair_ir is not None:
        return error_instance
    ir = RepairProgramNormalizer().normalize_error_instance(
        error_instance=error_instance,
        case_audit=case_audit,
        runtime_case_view=runtime_case_view,
        formation_signals=formation_signals,
    )
    return error_instance.model_copy(update={"canonical_repair_ir": ir})


__all__ = ["RepairProgramNormalizer", "attach_canonical_repair_ir"]
