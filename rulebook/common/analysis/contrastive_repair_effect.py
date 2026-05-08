"""Contrastive repair-effect discovery for Phase-1 offline learning.

This module turns one audited pred->target repair trace into one or more
``ContrastiveRepairEffect`` objects. It intentionally uses only the current
case's SQL/role-graph deltas, execution/audit hypotheses, and schema-derived
roles. It must not use database names, case ids, manual labels, or fixed
column-pair rules as effect definitions.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence

from method.EEA.rulebook.common.core.data_structures import ContrastiveRepairEffect, PossibleEffectAxis


VALID_EFFECT_AXES = (
    "output_shape_delta",
    "multi_output_contract_delta",
    "grain_delta",
    "aggregation_unit_delta",
    "source_route_delta",
    "predicate_scope_delta",
    "role_anchor_delta",
    "temporal_scope_delta",
    "proxy_slot_delta",
    "storage_contract_delta",
    "formula_delta",
    "ranking_contract_delta",
)


def _payload(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return dict(getattr(value, "__dict__", {}) or {})


def _payload_list(values: Any) -> List[Dict[str, Any]]:
    if not isinstance(values, (list, tuple)):
        return []
    rows: List[Dict[str, Any]] = []
    for value in values:
        payload = _payload(value)
        if payload:
            rows.append(payload)
    return rows


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _stable_effect_id(case_id: str, axis: str, ordinal: int, payload: Dict[str, Any]) -> str:
    raw = "|".join([str(case_id), axis, str(ordinal), _canonical_json(payload)])
    return f"effect:{case_id}:{axis}:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:8]}"


def _as_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _role_counts(values: Iterable[Any]) -> Dict[str, int]:
    return dict(Counter(str(value) for value in values if str(value)))


def _ref_identity(ref: Dict[str, Any]) -> str:
    for key in ("side_key", "direct_role_path", "derived_role_path"):
        value = str(ref.get(key) or "").strip()
        if value:
            return value
    table = str(ref.get("table") or "").strip().lower()
    column = str(ref.get("column") or "").strip().lower()
    if table and column:
        return f"{table}.{column}"
    return str(ref.get("expression") or "").strip().lower()


def _path_kind(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "none"
    if text.startswith("join_path:"):
        return "join_path"
    if text.startswith("direct:"):
        return "direct"
    if text.startswith("output_slot_"):
        return "output_slot"
    return text.split(":", 1)[0] or "other"


def _ref_abstract(ref: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "sql_role": ref.get("sql_role"),
        "column_role": ref.get("column_role"),
        "relation_role": ref.get("relation_role"),
        "path_kind": _path_kind(ref.get("path_role")),
        "has_direct_role_path": bool(ref.get("direct_role_path")),
        "has_derived_role_path": bool(ref.get("derived_role_path")),
        "has_role_side_group": bool(ref.get("role_side_group")),
    }


def _refs_summary(refs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    abstract_refs = [_ref_abstract(ref) for ref in refs]
    return {
        "arity": len(refs),
        "role_sequence": [ref.get("column_role") for ref in abstract_refs],
        "role_counts": _role_counts(ref.get("column_role") for ref in abstract_refs),
        "path_kind_counts": _role_counts(ref.get("path_kind") for ref in abstract_refs),
        "direct_role_path_count": sum(1 for ref in abstract_refs if ref.get("has_direct_role_path")),
        "derived_role_path_count": sum(1 for ref in abstract_refs if ref.get("has_derived_role_path")),
        "role_side_group_count": sum(1 for ref in abstract_refs if ref.get("has_role_side_group")),
        "example_refs": [
            {
                "table": ref.get("table"),
                "column": ref.get("column"),
                "expression": ref.get("expression"),
                "slot_index": ref.get("slot_index"),
                "column_role": ref.get("column_role"),
                "path_role": ref.get("path_role"),
                "direct_role_path": ref.get("direct_role_path"),
                "derived_role_path": ref.get("derived_role_path"),
                "role_side_group": ref.get("role_side_group"),
            }
            for ref in refs[:6]
        ],
    }


def _graph_shape(graph: Dict[str, Any]) -> Dict[str, Any]:
    shape = _payload(graph.get("output_shape"))
    return {
        "arity": _as_int(shape.get("arity")),
        "roles": list(shape.get("roles") or []),
        "role_counts": _role_counts(shape.get("roles") or []),
        "has_aggregate": bool(shape.get("has_aggregate")),
        "has_distinct": bool(shape.get("has_distinct")),
    }


def _relation_summary(graph: Dict[str, Any]) -> Dict[str, Any]:
    relations = _payload_list(graph.get("equality_relations") or [])
    keys = sorted(str(item.get("canonical_key") or "") for item in relations if item.get("canonical_key"))
    role_keys = sorted(str(item.get("role_key") or "") for item in relations if item.get("role_key"))
    table_roles = _payload(graph.get("table_relation_roles"))
    return {
        "table_count": len(graph.get("tables") or []),
        "relation_count": len(keys),
        "relation_role_counts": _role_counts(role_keys),
        "table_relation_role_counts": _role_counts(table_roles.values()),
        "example_relation_keys": keys[:8],
        "example_relation_role_keys": role_keys[:8],
    }


def _predicate_summary(graph: Dict[str, Any]) -> Dict[str, Any]:
    refs = _payload_list(graph.get("predicate_refs") or [])
    predicates = [str(item) for item in (graph.get("predicates") or []) if str(item)]
    return {
        "predicate_count": len(predicates),
        "predicate_ref_role_counts": _role_counts(ref.get("column_role") for ref in refs),
        "example_predicates": predicates[:6],
    }


def _shape_from_legacy(
    *,
    shape: Dict[str, Any],
    output_effect: Dict[str, Any],
    source_output_refs: Sequence[Dict[str, Any]],
    target_output_refs: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    source_arity = _as_int(shape.get("current_arity") or output_effect.get("source_arity"))
    target_arity = _as_int(shape.get("target_arity") or output_effect.get("target_arity"))
    if source_arity is None:
        source_arity = len(source_output_refs) if source_output_refs else None
    if target_arity is None:
        target_arity = len(target_output_refs) if target_output_refs else None
    arity_delta = None
    direction = str(shape.get("arity_direction") or "").lower()
    if source_arity is not None and target_arity is not None:
        arity_delta = target_arity - source_arity
        direction = "increase" if arity_delta > 0 else "decrease" if arity_delta < 0 else "same"
    return {
        "source_arity": source_arity,
        "target_arity": target_arity,
        "arity_delta": arity_delta,
        "arity_direction": direction or "unknown",
    }


def _output_kind(
    *,
    shape_delta: Dict[str, Any],
    output_effect: Dict[str, Any],
    source_output_refs: Sequence[Dict[str, Any]],
    target_output_refs: Sequence[Dict[str, Any]],
) -> str:
    kind = str(output_effect.get("kind") or "").strip()
    if kind:
        return kind
    source_ids = {_ref_identity(ref) for ref in source_output_refs if _ref_identity(ref)}
    target_ids = {_ref_identity(ref) for ref in target_output_refs if _ref_identity(ref)}
    direction = str(shape_delta.get("arity_direction") or "").lower()
    if direction == "decrease" and target_ids and target_ids.issubset(source_ids):
        return "output_subset"
    if direction == "increase":
        return "output_expand"
    if source_ids and target_ids and source_ids != target_ids:
        return "output_replace"
    if direction in {"increase", "decrease"}:
        return "output_shape_change"
    return ""


def _output_primitive(kind: str, shape_delta: Dict[str, Any]) -> str:
    direction = str(shape_delta.get("arity_direction") or "").lower()
    if kind == "output_subset" or direction == "decrease":
        return "DROP_SIDE"
    if direction == "increase":
        return "ADD_SELECT_SLOT"
    if kind in {"output_replace", "output_shape_change"}:
        return "REPLACE_SELECT_SLOT"
    return ""


def _likelihood_confidence(value: Any) -> float:
    text = str(value or "").strip().lower()
    if text == "high":
        return 0.7
    if text == "medium":
        return 0.55
    if text == "low":
        return 0.4
    return 0.5


def _axis_hypotheses(
    *,
    possible_effect_axes: Sequence[PossibleEffectAxis],
    effect_axis_hint: Optional[str],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in possible_effect_axes or []:
        payload = _payload(item)
        axis = str(payload.get("axis") or "").strip()
        if axis in VALID_EFFECT_AXES:
            rows.append(payload)
    hint = str(effect_axis_hint or "").strip()
    if hint in VALID_EFFECT_AXES and not any(row.get("axis") == hint for row in rows):
        rows.append(
            {
                "axis": hint,
                "source_state_summary": None,
                "target_state_summary": None,
                "delta_summary": "auditor effect_axis_hint",
                "primary_likelihood": "medium",
                "why": "emitted by offline wrong-case auditor",
            }
        )
    return rows


def _normalize_role(role: str) -> str:
    value = str(role or "").strip().lower()
    return value if value in {"primary", "dependency", "accessory", "noise"} else "dependency"


def discover_contrastive_repair_effects(
    *,
    db_id: str,
    case_id: str,
    shape: Dict[str, Any],
    source_graph: Dict[str, Any],
    target_graph: Dict[str, Any],
    source_output_refs: Sequence[Dict[str, Any]],
    target_output_refs: Sequence[Dict[str, Any]],
    legacy_effect_signature: Dict[str, Any],
    predicate_delta: Optional[Dict[str, Any]] = None,
    table_delta: Optional[Dict[str, Any]] = None,
    delta_axes: Optional[Sequence[str]] = None,
    possible_effect_axes: Optional[Sequence[PossibleEffectAxis]] = None,
    effect_axis_hint: Optional[str] = None,
) -> List[ContrastiveRepairEffect]:
    """Discover offline effect candidates for one audited repair trace."""

    _ = db_id
    effects: List[ContrastiveRepairEffect] = []
    seen_axes: set[str] = set()

    def add_effect(
        *,
        axis: str,
        source_state: Dict[str, Any],
        target_state: Dict[str, Any],
        delta: Dict[str, Any],
        role: str,
        triggerability: Optional[Dict[str, Any]] = None,
        actionability: Optional[Dict[str, Any]] = None,
        evidence: Optional[Dict[str, Any]] = None,
        confidence: float = 1.0,
    ) -> None:
        if axis not in VALID_EFFECT_AXES:
            return
        payload = {
            "source_state": source_state,
            "target_state": target_state,
            "delta": delta,
            "role": _normalize_role(role),
        }
        effects.append(
            ContrastiveRepairEffect(
                effect_id=_stable_effect_id(str(case_id), axis, len(effects), payload),
                axis=axis,
                source_state=source_state,
                target_state=target_state,
                delta=delta,
                role=_normalize_role(role),
                triggerability=triggerability or {},
                actionability=actionability or {},
                evidence=evidence or {},
                confidence=float(confidence),
            )
        )
        seen_axes.add(axis)

    legacy = _payload(legacy_effect_signature)
    output_effect = _payload(legacy.get("output_effect"))
    relation_effect = _payload(legacy.get("relation_effect"))
    predicate_effect = _payload(legacy.get("predicate_scope_effect"))
    grain_effect = _payload(legacy.get("grain_effect"))
    field_effect = _payload(legacy.get("field_binding_effect"))
    ranking_effect = _payload(legacy.get("ranking_effect"))
    predicate_delta = _payload(predicate_delta)
    table_delta = _payload(table_delta)

    shape_delta = _shape_from_legacy(
        shape=shape,
        output_effect=output_effect,
        source_output_refs=source_output_refs,
        target_output_refs=target_output_refs,
    )
    output_kind = _output_kind(
        shape_delta=shape_delta,
        output_effect=output_effect,
        source_output_refs=source_output_refs,
        target_output_refs=target_output_refs,
    )
    if output_kind:
        primitive = _output_primitive(output_kind, shape_delta)
        add_effect(
            axis="output_shape_delta",
            source_state={
                "output": _refs_summary(source_output_refs),
                "shape": _graph_shape(source_graph),
            },
            target_state={
                "output": _refs_summary(target_output_refs),
                "shape": _graph_shape(target_graph),
            },
            delta={
                "kind": output_kind,
                **shape_delta,
                "target_is_subset_of_source": bool(output_effect.get("target_is_subset_of_source")),
                "kept_output_refs": list(output_effect.get("kept_output_refs") or []),
                "dropped_output_refs": list(output_effect.get("dropped_output_refs") or []),
            },
            role="primary",
            triggerability={
                "source_visible_in_runtime": bool(source_output_refs),
                "target_bindable_from_schema_or_memory": bool(target_output_refs),
                "gold_only_blocker": False,
            },
            actionability={
                "primitive": primitive,
                "arguments_bindable": "unknown",
                "branch_count": 1 if primitive else 0,
                "branch_selection_answer_blind": True if primitive else None,
            },
            evidence={
                "source": "code_pred_vs_target_output_delta",
                "legacy_output_effect": output_effect,
            },
            confidence=0.95 if output_effect else 0.85,
        )
        if max(shape_delta.get("source_arity") or 0, shape_delta.get("target_arity") or 0) > 1:
            add_effect(
                axis="multi_output_contract_delta",
                source_state={"output": _refs_summary(source_output_refs)},
                target_state={"output": _refs_summary(target_output_refs)},
                delta={"kind": "multi_output_contract_change", **shape_delta},
                role="dependency",
                triggerability={
                    "source_visible_in_runtime": bool(source_output_refs),
                    "target_bindable_from_schema_or_memory": bool(target_output_refs),
                    "gold_only_blocker": False,
                },
                actionability={
                    "primitive": primitive,
                    "arguments_bindable": "unknown",
                    "branch_count": 1 if primitive else 0,
                    "branch_selection_answer_blind": True if primitive else None,
                },
                evidence={"source": "code_output_arity_contract"},
                confidence=0.8,
            )

    relation_changed = bool(
        relation_effect.get("kind")
        or relation_effect.get("added_relation_equalities")
        or relation_effect.get("removed_relation_equalities")
        or set(source_graph.get("tables") or []) != set(target_graph.get("tables") or [])
        or table_delta
    )
    if relation_changed:
        add_effect(
            axis="source_route_delta",
            source_state={"route": _relation_summary(source_graph)},
            target_state={"route": _relation_summary(target_graph)},
            delta={
                "kind": relation_effect.get("kind") or "route_change",
                "table_set_delta": table_delta,
                "added_relation_equalities": list(relation_effect.get("added_relation_equalities") or []),
                "removed_relation_equalities": list(relation_effect.get("removed_relation_equalities") or []),
            },
            role="dependency" if "output_shape_delta" in seen_axes else "primary",
            triggerability={
                "source_visible_in_runtime": bool(source_graph.get("tables") or source_graph.get("equality_relations")),
                "target_bindable_from_schema_or_memory": bool(target_graph.get("tables") or target_graph.get("equality_relations")),
                "gold_only_blocker": False,
            },
            actionability={
                "primitive": "REROUTE_FACT" if relation_effect.get("added_relation_equalities") else "INSERT_BRIDGE",
                "arguments_bindable": "unknown",
                "branch_count": "unknown",
                "branch_selection_answer_blind": "unknown",
            },
            evidence={"source": "code_relation_or_table_delta", "legacy_relation_effect": relation_effect},
            confidence=0.85,
        )

    predicate_changed = bool(
        predicate_effect.get("kind")
        or predicate_effect.get("possible_scope_move")
        or predicate_effect.get("added_target_predicates")
        or predicate_effect.get("removed_source_predicates")
        or _payload(predicate_delta.get("predicate_count_delta"))
    )
    if predicate_changed:
        add_effect(
            axis="predicate_scope_delta",
            source_state={"predicates": _predicate_summary(source_graph)},
            target_state={"predicates": _predicate_summary(target_graph)},
            delta={
                "kind": predicate_effect.get("kind") or "predicate_scope_change",
                **predicate_effect,
            },
            role="dependency" if effects else "primary",
            triggerability={
                "source_visible_in_runtime": bool(source_graph.get("predicates")),
                "target_bindable_from_schema_or_memory": bool(target_graph.get("predicates")),
                "gold_only_blocker": False,
            },
            actionability={
                "primitive": "MOVE_CONDITION" if predicate_effect.get("possible_scope_move") else "DROP_SIDE",
                "arguments_bindable": "unknown",
                "branch_count": "unknown",
                "branch_selection_answer_blind": "unknown",
            },
            evidence={"source": "code_predicate_delta", "legacy_predicate_scope_effect": predicate_effect},
            confidence=0.8,
        )

    grain_changed = bool(grain_effect.get("kind") or grain_effect.get("grain_changed"))
    if grain_changed:
        add_effect(
            axis="grain_delta",
            source_state={"shape": _graph_shape(source_graph)},
            target_state={"shape": _graph_shape(target_graph)},
            delta={"kind": grain_effect.get("kind") or "grain_change", **grain_effect},
            role="dependency" if effects else "primary",
            triggerability={
                "source_visible_in_runtime": bool(source_graph.get("output_shape")),
                "target_bindable_from_schema_or_memory": bool(target_graph.get("output_shape")),
                "gold_only_blocker": False,
            },
            actionability={
                "primitive": "CHANGE_GRAIN",
                "arguments_bindable": "unknown",
                "branch_count": "unknown",
                "branch_selection_answer_blind": "unknown",
            },
            evidence={"source": "code_grain_delta", "legacy_grain_effect": grain_effect},
            confidence=0.8,
        )
    source_shape = _graph_shape(source_graph)
    target_shape = _graph_shape(target_graph)
    aggregate_changed = source_shape.get("has_aggregate") != target_shape.get("has_aggregate")
    distinct_changed = source_shape.get("has_distinct") != target_shape.get("has_distinct")
    if aggregate_changed or distinct_changed:
        distinct_only_accessory = bool(
            distinct_changed
            and not aggregate_changed
            and any(effect.role == "primary" for effect in effects)
        )
        add_effect(
            axis="aggregation_unit_delta",
            source_state={"shape": source_shape},
            target_state={"shape": target_shape},
            delta={
                "kind": "aggregation_unit_change",
                "source_has_aggregate": source_shape.get("has_aggregate"),
                "target_has_aggregate": target_shape.get("has_aggregate"),
                "source_has_distinct": source_shape.get("has_distinct"),
                "target_has_distinct": target_shape.get("has_distinct"),
            },
            role="accessory" if distinct_only_accessory else "dependency" if effects else "primary",
            actionability={
                "primitive": "CHANGE_GRAIN",
                "arguments_bindable": "unknown",
                "runtime_policy": "branch_accessory" if distinct_only_accessory else "required",
            },
            evidence={"source": "code_output_shape_aggregate_delta"},
            confidence=0.75,
        )

    if field_effect.get("kind") or (
        field_effect.get("source_output_refs")
        and field_effect.get("target_output_refs")
        and field_effect.get("source_output_refs") != field_effect.get("target_output_refs")
        and shape_delta.get("arity_direction") in {"same", "unknown", ""}
    ):
        source_roles = list(field_effect.get("source_output_roles") or [])
        target_roles = list(field_effect.get("target_output_roles") or [])
        axis = "proxy_slot_delta" if source_roles == target_roles else "storage_contract_delta"
        add_effect(
            axis=axis,
            source_state={"output": _refs_summary(source_output_refs), "roles": source_roles},
            target_state={"output": _refs_summary(target_output_refs), "roles": target_roles},
            delta={
                "kind": field_effect.get("kind") or "field_switch",
                "source_role_counts": _role_counts(source_roles),
                "target_role_counts": _role_counts(target_roles),
            },
            role="primary" if not effects else "dependency",
            triggerability={
                "source_visible_in_runtime": bool(source_output_refs),
                "target_bindable_from_schema_or_memory": bool(target_output_refs),
                "gold_only_blocker": False,
            },
            actionability={
                "primitive": "SWITCH_CANONICAL_FIELD",
                "arguments_bindable": "unknown",
                "branch_count": "unknown",
                "branch_selection_answer_blind": "unknown",
            },
            evidence={"source": "code_field_binding_delta", "legacy_field_binding_effect": field_effect},
            confidence=0.8,
        )

    order_delta = _payload(predicate_delta.get("order_by_count_delta"))
    if ranking_effect.get("kind") or order_delta:
        add_effect(
            axis="ranking_contract_delta",
            source_state={"shape": source_shape},
            target_state={"shape": target_shape},
            delta={"kind": ranking_effect.get("kind") or "ranking_contract_change", "order_by_count_delta": order_delta},
            role="dependency" if effects else "primary",
            actionability={"primitive": "MATERIALIZE_RANKING_OUTPUT", "arguments_bindable": "unknown"},
            evidence={"source": "code_order_or_ranking_delta", "legacy_ranking_effect": ranking_effect},
            confidence=0.7,
        )

    for axis in delta_axes or []:
        axis = str(axis or "").strip()
        if axis not in VALID_EFFECT_AXES or axis in seen_axes:
            continue
        add_effect(
            axis=axis,
            source_state={
                "output": _refs_summary(source_output_refs),
                "route": _relation_summary(source_graph),
                "predicates": _predicate_summary(source_graph),
                "shape": _graph_shape(source_graph),
            },
            target_state={
                "output": _refs_summary(target_output_refs),
                "route": _relation_summary(target_graph),
                "predicates": _predicate_summary(target_graph),
                "shape": _graph_shape(target_graph),
            },
            delta={
                "kind": "offline_delta_axis",
                "axis": axis,
            },
            role="primary" if not any(effect.role == "primary" for effect in effects) else "dependency",
            triggerability={
                "source_visible_in_runtime": "axis_dependent",
                "target_bindable_from_schema_or_memory": "axis_dependent",
                "gold_only_blocker": "unknown",
            },
            actionability={
                "primitive": "unknown",
                "arguments_bindable": "unknown",
                "branch_count": "unknown",
                "branch_selection_answer_blind": "unknown",
            },
            evidence={"source": "offline_delta_signature_axis"},
            confidence=0.6,
        )

    for hypothesis in _axis_hypotheses(
        possible_effect_axes=possible_effect_axes or [],
        effect_axis_hint=effect_axis_hint,
    ):
        axis = str(hypothesis.get("axis") or "")
        if axis in seen_axes:
            continue
        confidence = _likelihood_confidence(hypothesis.get("primary_likelihood"))
        add_effect(
            axis=axis,
            source_state={"summary": hypothesis.get("source_state_summary")},
            target_state={"summary": hypothesis.get("target_state_summary")},
            delta={"kind": "llm_hypothesized_effect", "summary": hypothesis.get("delta_summary")},
            role="primary" if confidence >= 0.65 and not any(effect.role == "primary" for effect in effects) else "dependency",
            triggerability={
                "source_visible_in_runtime": "unknown",
                "target_bindable_from_schema_or_memory": "unknown",
                "gold_only_blocker": "unknown",
            },
            actionability={
                "primitive": "unknown",
                "arguments_bindable": "unknown",
                "branch_count": "unknown",
                "branch_selection_answer_blind": "unknown",
            },
            evidence={"source": "llm_possible_effect_axis", "why": hypothesis.get("why")},
            confidence=confidence,
        )

    if effects and not any(effect.role == "primary" for effect in effects):
        effects[0].role = "primary"
    return effects


__all__ = ["VALID_EFFECT_AXES", "discover_contrastive_repair_effects"]
