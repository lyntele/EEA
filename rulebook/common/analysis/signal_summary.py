"""Compact signal summaries for offline formation and runtime matching.

These helpers only compress existing signal objects. They do not introduce
database-specific vocabulary or hard-coded domain rules.
"""

from __future__ import annotations

from collections import Counter
import json
import re
from typing import Any, Dict, Iterable, List, Optional

from method.EEA.rulebook.common.core.data_structures import CaseSignalView, DeltaSignature, ErrorInstanceV2, RepairSkeleton
from method.EEA.rulebook.common.core.vocabulary import GrainType, OpType


def _output_arity_signal(arity: Any) -> Optional[str]:
    value = _int_or_none(arity)
    if value is None:
        return None
    if value > 2:
        return "pred.output_arity=>2"
    return f"pred.output_arity={value}"


def _payload(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return dict(obj)
    return dict(getattr(obj, "__dict__", {}) or {})


def _compact_role_ref(ref: Any) -> Dict[str, Any]:
    payload = _payload(ref)
    keep = {
        "ref_id",
        "source",
        "table",
        "column",
        "expression",
        "slot_index",
        "sql_role",
        "column_role",
        "path_role",
        "direct_role_path",
        "derived_role_path",
        "role_side_group",
        "side_key",
        "relation_role",
    }
    return {key: payload.get(key) for key in keep if payload.get(key) is not None}


def _compact_role_refs(values: Any, *, limit: int = 12) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen = set()
    for value in values or []:
        payload = _compact_role_ref(value)
        if not payload:
            continue
        key = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        if key in seen:
            continue
        seen.add(key)
        rows.append(payload)
        if len(rows) >= limit:
            break
    return rows


def _compact_relation_rows(values: Any, *, limit: int = 16) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for value in values or []:
        payload = _payload(value)
        if payload:
            rows.append(payload)
        if len(rows) >= limit:
            break
    return rows


def _compact_effect_signature(value: Any) -> Dict[str, Any]:
    payload = _payload(value)
    if not payload:
        return {}
    return {
        "effect_candidates": [
            {
                "effect_id": _payload(item).get("effect_id"),
                "axis": _payload(item).get("axis"),
                "role": _payload(item).get("role"),
                "delta": _payload(_payload(item).get("delta")),
                "triggerability": _payload(_payload(item).get("triggerability")),
                "actionability": _payload(_payload(item).get("actionability")),
            }
            for item in (payload.get("effect_candidates") or [])[:8]
            if _payload(item).get("axis")
        ],
        "output_effect": _payload(payload.get("output_effect")),
        "relation_effect": _payload(payload.get("relation_effect")),
        "predicate_scope_effect": _payload(payload.get("predicate_scope_effect")),
        "grain_effect": _payload(payload.get("grain_effect")),
        "field_binding_effect": _payload(payload.get("field_binding_effect")),
        "ranking_effect": _payload(payload.get("ranking_effect")),
    }


def _compact_insight_signature(value: Any) -> Dict[str, Any]:
    payload = _payload(value)
    if not payload:
        return {}
    return {
        "schema_version": payload.get("schema_version"),
        "interface_key": payload.get("interface_key"),
        "source_misread": payload.get("source_misread"),
        "target_preference": payload.get("target_preference"),
        "repair_interface": payload.get("repair_interface"),
        "binding_slots": list(payload.get("binding_slots") or [])[:8],
        "preserve_invariants": list(payload.get("preserve_invariants") or [])[:8],
        "negative_guards": list(payload.get("negative_guards") or [])[:8],
        "axis_links": list(payload.get("axis_links") or [])[:8],
        "confidence": payload.get("confidence"),
    }


def _compact_op_arguments(arguments: Any) -> Dict[str, Any]:
    args = _payload(arguments)
    signature = _payload(args.get("operation_signature") or args.get("shared_signature"))
    compact_signature = {
        key: signature.get(key)
        for key in (
            "step_op",
            "locus",
            "is_dependency",
            "required",
            "slot_signature",
            "role_delta",
            "output_path_delta",
            "relation_delta",
            "predicate_scope_delta",
            "grain_delta",
        )
        if signature.get(key) is not None
    }
    return {
        "source_step_id": args.get("source_step_id"),
        "source_op": args.get("source_op"),
        "repair_locus": args.get("repair_locus"),
        "operation_signature": compact_signature,
        "output_shape_delta": _payload(args.get("output_shape_delta")),
        "source_output_shape": _payload(args.get("source_output_shape")),
        "target_output_shape": _payload(args.get("target_output_shape")),
        "source_output_roles": list(args.get("source_output_roles") or [])[:8],
        "target_output_roles": list(args.get("target_output_roles") or [])[:8],
        "source_output_refs": _compact_role_refs(args.get("source_output_refs") or [], limit=8),
        "target_output_refs": _compact_role_refs(args.get("target_output_refs") or [], limit=8),
        "table_set_delta": _payload(args.get("table_set_delta")),
        "predicate_delta": _payload(args.get("predicate_delta")),
        "repair_effect_signature": _compact_effect_signature(args.get("repair_effect_signature")),
        "repair_insight_signature": _compact_insight_signature(args.get("repair_insight_signature")),
        "target_invariants": list(args.get("target_invariants") or [])[:12],
        "source_equality_relations": _compact_relation_rows(args.get("source_equality_relations"), limit=12),
        "target_equality_relations": _compact_relation_rows(args.get("target_equality_relations"), limit=12),
        "step_slots": list(args.get("step_slots") or [])[:8],
        "step_arguments": _payload(args.get("step_arguments")),
        "identity_role": args.get("identity_role"),
        "runtime_policy": args.get("runtime_policy"),
        "effect_axis": args.get("effect_axis"),
        "accessory_policies": list(args.get("accessory_policies") or [])[:8],
    }


def _compact_canonical_op(op: Any) -> Dict[str, Any]:
    payload = _payload(op)
    if not payload:
        return {}
    return {
        "op_id": payload.get("op_id"),
        "op_type": payload.get("op_type"),
        "locus": payload.get("locus"),
        "role_refs": _compact_role_refs(payload.get("role_refs") or [], limit=12),
        "arguments": _compact_op_arguments(payload.get("arguments") or {}),
        "invariants": list(payload.get("invariants") or [])[:12],
        "source_step_ids": list(payload.get("source_step_ids") or [])[:8],
        "supporting_case_ids": list(payload.get("supporting_case_ids") or [])[:16],
        "confidence": payload.get("confidence", 1.0),
    }


def compact_canonical_repair_ir_for_memory(value: Any) -> Dict[str, Any]:
    payload = _payload(value)
    if not payload:
        return {}
    return {
        "schema_version": payload.get("schema_version") or "canonical-repair-ir-v0",
        "db_id": payload.get("db_id") or "",
        "case_id": str(payload.get("case_id") or ""),
        "source_role_graph": {
            "output_shape": _payload(_payload(payload.get("source_role_graph")).get("output_shape")),
        },
        "target_role_graph": {
            "output_shape": _payload(_payload(payload.get("target_role_graph")).get("output_shape")),
        },
        "program_ops": [
            compact_op for op in (payload.get("program_ops") or []) if (compact_op := _compact_canonical_op(op))
        ],
        "core_ops": list(payload.get("core_ops") or [])[:12],
        "accessory_ops": list(payload.get("accessory_ops") or [])[:12],
        "repair_effect_signature": _compact_effect_signature(payload.get("repair_effect_signature")),
        "repair_insight_signature": _compact_insight_signature(payload.get("repair_insight_signature")),
        "target_invariants": list(payload.get("target_invariants") or [])[:16],
        "invariants": list(payload.get("invariants") or [])[:16],
        "unresolved_variation_axes": list(payload.get("unresolved_variation_axes") or [])[:16],
        "normalizer_warnings": list(payload.get("normalizer_warnings") or [])[:16],
    }


def compact_synthesized_program_for_contract(value: Any) -> Dict[str, Any]:
    payload = _payload(value)
    if not payload:
        return {}
    envelope = _payload(payload.get("program_envelope"))
    return {
        "schema_version": payload.get("schema_version"),
        "program_id": payload.get("program_id"),
        "program_type": payload.get("program_type"),
        "op_count": len(payload.get("ops") or []),
        "core_op_count": len(payload.get("core_ops") or []),
        "accessory_op_count": len(payload.get("accessory_ops") or []),
        "lowering_families": sorted(
            {
                str(_payload(op).get("op_type") or "")
                for op in (payload.get("ops") or [])
                if _payload(op)
            }
        ),
        "repair_effect_signature": _compact_effect_signature(payload.get("repair_effect_signature")),
        "repair_insight_signature": _compact_insight_signature(payload.get("repair_insight_signature")),
        "target_invariants": list(payload.get("target_invariants") or [])[:16],
        "unresolved_variation_axes": list(payload.get("unresolved_variation_axes") or [])[:16],
        "shared_invariants": list(payload.get("shared_invariants") or [])[:16],
        "synthesized_from_case_ids": list(payload.get("synthesized_from_case_ids") or [])[:32],
        "program_envelope_summary": {
            "allowed_action_primitives": list(envelope.get("allowed_action_primitives") or [])[:12],
            "target_invariant_count": len(envelope.get("target_invariants") or []),
            "runtime_branch_count": len(envelope.get("runtime_branches") or []),
            "required_role_slot_count": len(envelope.get("required_role_slots") or []),
            "unresolved_variation_axes": list(envelope.get("unresolved_variation_axes") or [])[:16],
        },
    }


def compact_synthesized_program_for_memory(value: Any) -> Dict[str, Any]:
    payload = _payload(value)
    if not payload:
        return {}
    envelope = _payload(payload.get("program_envelope"))
    compact_envelope = {
        "schema_version": envelope.get("schema_version") or "program-envelope-v0",
        "source_antipatterns": list(envelope.get("source_antipatterns") or [])[:12],
        "target_effects": list(envelope.get("target_effects") or [])[:12],
        "target_invariants": list(envelope.get("target_invariants") or [])[:16],
        "allowed_action_primitives": list(envelope.get("allowed_action_primitives") or [])[:16],
        "action_envelope": _payload(envelope.get("action_envelope")),
        "lowering_branches": list(envelope.get("lowering_branches") or [])[:16],
        "runtime_branches": list(envelope.get("runtime_branches") or [])[:16],
        "branch_selection_contract": _payload(envelope.get("branch_selection_contract")),
        "required_role_slots": list(envelope.get("required_role_slots") or [])[:16],
        "repair_insight_signature": _compact_insight_signature(
            envelope.get("repair_insight_signature")
        ),
        "negative_guards": list(envelope.get("negative_guards") or [])[:16],
        "unresolved_variation_axes": list(envelope.get("unresolved_variation_axes") or [])[:16],
    }
    return {
        "schema_version": payload.get("schema_version") or "canonical-repair-program-v0",
        "program_id": payload.get("program_id") or "",
        "program_type": payload.get("program_type"),
        "ops": [
            compact_op for op in (payload.get("ops") or []) if (compact_op := _compact_canonical_op(op))
        ],
        "core_ops": list(payload.get("core_ops") or [])[:16],
        "accessory_ops": list(payload.get("accessory_ops") or [])[:16],
        "repair_effect_signature": _compact_effect_signature(payload.get("repair_effect_signature")),
        "repair_insight_signature": _compact_insight_signature(payload.get("repair_insight_signature")),
        "target_invariants": list(payload.get("target_invariants") or [])[:16],
        "unresolved_variation_axes": list(payload.get("unresolved_variation_axes") or [])[:16],
        "program_envelope": compact_envelope,
        "variables": _payload(payload.get("variables")),
        "shared_invariants": list(payload.get("shared_invariants") or [])[:16],
        "synthesized_from_case_ids": list(payload.get("synthesized_from_case_ids") or [])[:32],
    }


def _as_list(values: Any) -> List[str]:
    if values is None:
        return []
    if isinstance(values, (list, tuple, set)):
        return [str(value) for value in values if str(value)]
    return [str(values)] if str(values) else []


def _as_payload_list(values: Any) -> List[Dict[str, Any]]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        values = [values]
    out: List[Dict[str, Any]] = []
    for value in values:
        payload = _payload(value)
        if payload:
            out.append(payload)
    return out


def _int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _bool(value: Any) -> bool:
    return bool(value)


def _bucket_count(value: Any) -> int:
    """Compatibility wrapper that now preserves the raw count."""
    return _int_or_none(value) or 0


def _role_counts(roles: Iterable[Any]) -> Dict[str, int]:
    return dict(Counter(str(role) for role in roles if str(role)))


def _pred_current_summary(case_signal_view: Optional[CaseSignalView]) -> Dict[str, Any]:
    view = _payload(case_signal_view)
    pred = _payload(view.get("pred_sql_view"))
    output_shape = _payload(pred.get("output_shape_current"))
    predicate_profile = _payload(pred.get("predicate_profile"))
    aggregate_profile = _payload(pred.get("aggregate_profile"))
    group_order_profile = _payload(pred.get("group_order_profile"))
    roles = _as_list(output_shape.get("roles") or pred.get("select_role_profile"))
    join_count = len(pred.get("join_graph") or [])
    tables_used = _as_list(pred.get("tables_used"))
    return {
        "select_arity": _int_or_none(pred.get("select_arity") or output_shape.get("arity")),
        "output_shape_current": {
            "arity": _int_or_none(output_shape.get("arity") or pred.get("select_arity")),
            "roles": roles,
            "role_counts": _role_counts(roles),
            "grain": output_shape.get("grain"),
        },
        "join_count": _bucket_count(join_count),
        "table_count": _bucket_count(len(tables_used)),
        "predicate_count": _bucket_count(predicate_profile.get("predicate_count")),
        "predicate_literal_count": _bucket_count(predicate_profile.get("literal_count")),
        "source_state_facts": _as_list(pred.get("source_state_facts")),
        "has_or_predicate": _bool(predicate_profile.get("has_or")),
        "has_negation_predicate": _bool(predicate_profile.get("has_negation")),
        "has_aggregate": _bool(aggregate_profile.get("has_aggregate")),
        "has_distinct_aggregate": _bool(aggregate_profile.get("has_distinct_aggregate")),
        "has_case_when": _bool(aggregate_profile.get("has_case_when")),
        "has_group_by": _bool(group_order_profile.get("group_by_count")),
        "has_order_by": _bool(group_order_profile.get("order_by_count")),
        "has_limit": group_order_profile.get("limit") is not None,
    }


def _delta_summary(delta_signature: Optional[DeltaSignature]) -> Dict[str, Any]:
    delta = _payload(delta_signature)
    output_delta = _payload(delta.get("output_shape_delta"))
    structural_delta = _payload(delta.get("structural_delta"))
    semantic_delta = _payload(delta.get("semantic_delta"))
    repair_delta = _payload(delta.get("repair_delta"))
    legacy_dict = _payload(structural_delta.get("legacy_signature_dict"))
    axes = _as_list(semantic_delta.get("delta_axes") or repair_delta.get("delta_axes"))
    return {
        "delta_axes": axes,
        "output_shape_delta": output_delta,
        "legacy_signature": structural_delta.get("legacy_signature"),
        "legacy_primary_edit_family": repair_delta.get("legacy_primary_edit_family")
        or legacy_dict.get("primary_edit_family"),
        "legacy_target_output_contract": repair_delta.get("legacy_target_output_contract")
        or legacy_dict.get("target_output_contract"),
        "table_set_delta": structural_delta.get("table_set_delta") or {},
        "predicate_count_delta": structural_delta.get("predicate_count_delta") or {},
        "group_by_count_delta": structural_delta.get("group_by_count_delta") or {},
        "order_by_count_delta": structural_delta.get("order_by_count_delta") or {},
        "output_shape_operation": repair_delta.get("output_shape_operation")
        or output_delta.get("operation"),
    }


def build_formation_signals(
    *,
    case_signal_view: Optional[CaseSignalView] = None,
    delta_signature: Optional[DeltaSignature] = None,
    error_instance: Optional[ErrorInstanceV2] = None,
) -> Dict[str, Any]:
    """Build a compact, auditable signal payload for group formation.

    Runtime may use the source-side ``pred_current`` shape for compatibility,
    but the answer-aware ``delta`` section only represents what the memory item
    learned offline; it is never recomputed for the new runtime case.
    """
    error_payload = _payload(error_instance)
    repair_skeleton = _payload(error_payload.get("repair_skeleton"))
    pre_condition = {
        "schema_version": "pre-condition-local-v1",
        "pre_question_signature_local": error_payload.get("pre_question_signature_local"),
        "pre_sql_signature_local": error_payload.get("pre_sql_signature_local"),
        "observed_failure_local": error_payload.get("observed_failure_local"),
        "repair_direction_local": error_payload.get("repair_direction_local"),
    }
    pre_condition = {
        key: value
        for key, value in pre_condition.items()
        if key == "schema_version" or value not in (None, "", [], {})
    }
    return {
        "schema_version": "formation-signal-v0",
        "case_id": str(error_payload.get("case_id") or ""),
        "pred_current": _pred_current_summary(case_signal_view),
        "delta": _delta_summary(delta_signature),
        "pre_condition_local": pre_condition,
        "repair_skeleton": repair_skeleton,
        "repair_program": [
            _payload(step)
            for step in (error_payload.get("repair_program") or [])
            if _payload(step)
        ],
        "canonical_repair_ir": compact_canonical_repair_ir_for_memory(
            error_payload.get("canonical_repair_ir")
        ),
    }


def _output_contract_for_arity(current_arity: Optional[int], target_arity: Optional[int]) -> str:
    """Keep the legacy enum non-decisive; generic shape lives in output_shape_delta."""
    _ = current_arity, target_arity
    return "UNCHANGED"


def _enriched_output_shape_delta(shape: Dict[str, Any]) -> Dict[str, Any]:
    current_arity = _int_or_none(shape.get("current_arity"))
    target_arity = _int_or_none(shape.get("target_arity"))
    out = dict(shape)
    if current_arity is not None:
        out["current_arity"] = current_arity
    if target_arity is not None:
        out["target_arity"] = target_arity
    if current_arity is not None and target_arity is not None:
        arity_delta = target_arity - current_arity
        out["arity_delta"] = arity_delta
        out["arity_direction"] = (
            "increase" if arity_delta > 0 else "decrease" if arity_delta < 0 else "same"
        )
    else:
        out.setdefault("arity_direction", "unknown")
    return out


def _answer_unit_contract(
    *,
    output_shape_delta: Dict[str, Any],
    pred_current: Dict[str, Any],
) -> Dict[str, Any]:
    """Offline memory-side answer-unit contract.

    This is a structural summary learned from the source case's pred-vs-gold
    output shape. It intentionally avoids case ids, table names, column names,
    question keywords, and legacy fixed shape labels.
    """

    shape_delta = _enriched_output_shape_delta(_payload(output_shape_delta))
    source_shape = _payload(pred_current.get("output_shape_current"))
    source_arity = _int_or_none(source_shape.get("arity"))
    target_arity = _int_or_none(shape_delta.get("target_arity"))
    current_arity = _int_or_none(shape_delta.get("current_arity"))
    if source_arity is None:
        source_arity = current_arity
    contract = {
        "source": "offline_output_shape_delta",
        "source_output_arity": source_arity,
        "target_output_arity": target_arity,
        "arity_delta": shape_delta.get("arity_delta"),
        "arity_direction": shape_delta.get("arity_direction"),
        "operation": shape_delta.get("operation"),
        "source_grain": source_shape.get("grain") or shape_delta.get("current_grain"),
        "target_grain": shape_delta.get("target_grain"),
        "source_roles": list(source_shape.get("roles") or shape_delta.get("current_roles") or []),
        "target_roles": list(shape_delta.get("target_roles") or []),
        "changes_output_arity": (
            source_arity is not None
            and target_arity is not None
            and source_arity != target_arity
        ),
    }
    return {key: value for key, value in contract.items() if value not in (None, [], {})}


def derive_repair_structural_from_delta(formation_signals: Dict[str, Any]) -> Dict[str, str]:
    """Derive a generic repair skeleton override from answer-aware delta.

    This uses only structural output-shape facts learned offline from the source
    case. It does not encode database names, table names, or domain-specific
    lexical rules.
    """
    delta = _payload(formation_signals.get("delta"))
    shape = _payload(delta.get("output_shape_delta"))
    current_arity = _int_or_none(shape.get("current_arity"))
    target_arity = _int_or_none(shape.get("target_arity"))
    operation = str(shape.get("operation") or delta.get("output_shape_operation") or "")

    if current_arity is None or target_arity is None:
        return {}

    shape_delta = _enriched_output_shape_delta(shape)
    output_contract = _output_contract_for_arity(current_arity, target_arity)
    if operation == "remove_output_slot" or current_arity > target_arity:
        return {
            "locus": "SELECT",
            "op_family": "drop",
            "target_family": "slot",
            "output_contract": output_contract,
            "output_shape_delta": shape_delta,
        }
    if operation == "add_output_slot" or target_arity > current_arity:
        return {
            "locus": "SELECT",
            "op_family": "add",
            "target_family": "slot",
            "output_contract": output_contract,
            "output_shape_delta": shape_delta,
        }
    if operation in {"replace_output_slot", "replace_or_reorder_output_slot"}:
        return {
            "locus": "SELECT",
            "op_family": "replace",
            "target_family": "slot",
            "output_contract": output_contract,
            "output_shape_delta": shape_delta,
        }
    return {}


def apply_delta_structural_override(
    repair_skeleton: RepairSkeleton,
    formation_signals: Dict[str, Any],
) -> RepairSkeleton:
    """Return repair_skeleton with structural delta override applied when present."""
    override = derive_repair_structural_from_delta(formation_signals)
    if not override:
        return repair_skeleton
    structural_payload = repair_skeleton.structural.model_dump(mode="json")
    structural_payload.update(override)
    structural = type(repair_skeleton.structural).model_validate(structural_payload)
    return repair_skeleton.model_copy(update={"structural": structural})


def _pred_contract_signals(pred_current: Dict[str, Any]) -> List[str]:
    shape = _payload(pred_current.get("output_shape_current"))
    signals: List[str] = []
    arity = shape.get("arity") if shape else pred_current.get("select_arity")
    if arity is not None:
        signals.append(f"pred.select_arity={arity}")
        arity_signal = _output_arity_signal(arity)
        if arity_signal:
            signals.append(arity_signal)
    roles = sorted(_as_list(shape.get("roles") if shape else []))
    if roles:
        signals.append("pred.output_roles=" + ",".join(roles))
        for role in roles:
            signals.append(f"pred.output_role={role}")
    grain = shape.get("grain") if shape else None
    if grain:
        signals.append(f"pred.output_grain={grain}")
    if str(arity) == "2" or grain == "pair_rows":
        signals.append("pred.pair_output=True")
    for key in ("join_count", "table_count", "predicate_count"):
        value = pred_current.get(key)
        if value is not None:
            signals.append(f"pred.{key}={value}")
    if pred_current.get("predicate_literal_count") is not None:
        signals.append(
            f"pred.predicate_literal_count={pred_current.get('predicate_literal_count')}"
        )
    for key in (
        "has_or_predicate",
        "has_negation_predicate",
        "has_aggregate",
        "has_distinct_aggregate",
        "has_case_when",
        "has_group_by",
        "has_order_by",
        "has_limit",
    ):
        signals.append(f"pred.{key}={bool(pred_current.get(key))}")
    return sorted(set(signals))


_QUESTION_CORE_TAGS = {
    *(item.value for item in OpType),
    *(item.value for item in GrainType),
}


def _core_question_contract_signals(tags: Iterable[Any]) -> List[str]:
    """Question-side signals stable enough to act as boolean gates.

    Answer-slot labels such as ``name``/``id`` are useful audit evidence, but in
    the manual cases they often vary inside the same repair pattern. Runtime
    required gates therefore keep only operation and grain cues; route words
    remain audit evidence because they are too dependent on phrasing.
    """
    return sorted(
        {
            f"question.decisive_tag={tag}"
            for tag in (str(value) for value in tags if str(value))
            if tag in _QUESTION_CORE_TAGS
        }
    )


def _core_pred_contract_signals(
    pred_current: Dict[str, Any],
    structural: Dict[str, Any],
) -> List[str]:
    """Pred-side signals required by the generic action contract.

    The old contract required every observed source-case shape bit. That made
    singleton replay precise but blocked cross-case pattern evidence. This keeps
    only shape facts that are necessary for the repair skeleton itself; all
    remaining facts stay in optional audit signals.
    """
    shape = _payload(pred_current.get("output_shape_current"))
    signals: List[str] = []
    locus = str(structural.get("locus") or "")
    op_family = str(structural.get("op_family") or "")
    target_family = str(structural.get("target_family") or "")
    shape_delta = _payload(structural.get("output_shape_delta"))
    arity_direction = str(shape_delta.get("arity_direction") or "")
    shape_change = arity_direction in {"increase", "decrease"}
    arity = shape.get("arity") if shape else pred_current.get("select_arity")
    if arity is not None:
        signals.append(f"pred.select_arity={arity}")
        arity_signal = _output_arity_signal(arity)
        if arity_signal:
            signals.append(arity_signal)
    roles = sorted(set(_as_list(shape.get("roles") if shape else [])))

    if shape_change and arity_direction == "decrease":
        grain = shape.get("grain") if shape else None
        if grain:
            signals.append(f"pred.output_grain={grain}")
        if str(arity) == "2" or grain == "pair_rows":
            signals.append("pred.pair_output=True")

    if (locus == "SELECT" or target_family == "slot") and not shape_change:
        for role in roles:
            signals.append(f"pred.output_role={role}")
        grain = shape.get("grain") if shape else None
        if grain:
            signals.append(f"pred.output_grain={grain}")

    if locus in {"AGGREGATION", "GRAIN"} or op_family in {"recount", "collapse"}:
        signals.append(f"pred.has_aggregate={bool(pred_current.get('has_aggregate'))}")
        signals.append(f"pred.has_group_by={bool(pred_current.get('has_group_by'))}")
    if locus == "ORDER_BY":
        signals.append(f"pred.has_order_by={bool(pred_current.get('has_order_by'))}")
    if locus in {"WHERE", "SCOPE", "PREDICATE"} and pred_current.get("has_negation_predicate"):
        signals.append("pred.has_negation_predicate=True")
    if locus in {"WHERE", "SCOPE", "PREDICATE"} or target_family == "condition":
        if pred_current.get("predicate_count") is not None:
            signals.append(f"pred.predicate_count={pred_current.get('predicate_count')}")
        if pred_current.get("predicate_literal_count") is not None:
            signals.append(
                f"pred.predicate_literal_count={pred_current.get('predicate_literal_count')}"
            )

    return sorted(set(signals))


def _shape_variant_signals(
    shape: Dict[str, Any],
    *,
    output_delta: Optional[Dict[str, Any]] = None,
    lowering_family: str = "",
) -> List[str]:
    signals: List[str] = []
    output_delta = _payload(output_delta)
    arity = shape.get("arity")
    if arity is None:
        arity = output_delta.get("current_arity")
    if arity is not None:
        signals.append(f"pred.select_arity={arity}")
        arity_signal = _output_arity_signal(arity)
        if arity_signal:
            signals.append(arity_signal)
    if shape.get("has_aggregate") is not None:
        signals.append(f"pred.has_aggregate={bool(shape.get('has_aggregate'))}")
    roles = sorted(set(_as_list(output_delta.get("current_roles") or shape.get("roles"))))
    if roles:
        for role in roles:
            signals.append(f"pred.output_role={role}")
    grain = output_delta.get("current_grain") or shape.get("grain")
    if grain:
        signals.append(f"pred.output_grain={grain}")
    if str(arity) == "2" or grain == "pair_rows":
        signals.append("pred.pair_output=True")
    return sorted(set(signals))


def _non_broad_trigger_signals(signals: Iterable[Any]) -> List[str]:
    out: set[str] = set()
    for signal in signals:
        text = str(signal)
        if not text or text.startswith("program."):
            continue
        out.add(text)
    return sorted(out)


def _program_lowering_family(op: Dict[str, Any]) -> str:
    args = _payload(op.get("arguments"))
    shared = _payload(args.get("shared_signature"))
    family = str(shared.get("lowering_family") or "").strip()
    if family:
        return family
    op_type = str(op.get("op_type") or "").upper()
    locus = str(op.get("locus") or "").upper()
    if op_type == "SELECT_OUTPUT_PATCH":
        return "select_output_patch"
    if op_type in {"SELECT_ADD_SLOT", "ADD_SELECT_SLOT"}:
        return "select_add"
    if op_type in {"SELECT_REPLACE_SLOT", "REPLACE_SELECT_SLOT"}:
        return "select_replace"
    if op_type in {"SELECT_DROP_SLOT", "DROP_SELECT_SLOT"}:
        return "select_drop"
    if op_type in {"JOIN_ADD_BRIDGE", "JOIN_ADD_TABLE", "BRIDGE_ADD_TABLE"}:
        return "join_bridge"
    if op_type in {"WHERE_DROP_CONDITION", "WHERE_REPLACE_CONDITION"}:
        return "where_side_edit"
    if locus == "SELECT" and "SLOT" in op_type:
        if "ADD" in op_type:
            return "select_add"
        if "REPLACE" in op_type:
            return "select_replace"
        if "DROP" in op_type:
            return "select_drop"
    return ""


def _canonical_role_ref_signals(refs: Any, *, hard_gate: bool = False) -> List[str]:
    """Program-level role signals shared with runtime current-case signals.

    ``hard_gate=True`` is used for required/variant gates. It removes surface
    artifacts such as output slot indexes and unknown relation roles that come
    from a representative source SQL rather than the shared repair program.
    """
    signals: set[str] = set()
    for ref in refs or []:
        payload = _payload(ref)
        sql_role = str(payload.get("sql_role") or "").strip()
        column_role = str(payload.get("column_role") or "").strip()
        path_role = str(payload.get("path_role") or "").strip()
        direct_role_path = str(payload.get("direct_role_path") or "").strip()
        derived_role_path = str(payload.get("derived_role_path") or "").strip()
        role_side_group = str(payload.get("role_side_group") or "").strip()
        side_key = str(payload.get("side_key") or "").strip()
        relation_role = str(payload.get("relation_role") or "").strip()
        raw_signals: List[str] = []
        if sql_role:
            raw_signals.append(f"pred.contains_sql_role={sql_role}")
        if column_role:
            raw_signals.append(f"pred.contains_column_role={column_role}")
        if role_side_group:
            raw_signals.append(f"pred.contains_role_side_group={role_side_group}")
        if side_key:
            raw_signals.append(f"pred.contains_side_key={side_key}")
        if direct_role_path:
            raw_signals.append(f"pred.contains_direct_role_path={direct_role_path}")
        if derived_role_path:
            raw_signals.append(f"pred.contains_derived_role_path={derived_role_path}")
        if path_role and not (hard_gate and role_side_group):
            raw_signals.append(f"pred.contains_path_role={path_role}")
        if relation_role:
            raw_signals.append(f"pred.contains_relation_role={relation_role}")
        if hard_gate:
            signals.update(_non_broad_trigger_signals(raw_signals))
        else:
            signals.update(raw_signals)
    return sorted(signals)


def _program_variant_required_signal_sets(synthesized_program: Dict[str, Any]) -> List[List[str]]:
    """Runtime OR-gates for family member variants.

    A family can have a shared repair program while individual members expose
    different source shapes. Required signals cannot encode OR directly, so the
    contract keeps stable base signals in ``required_signals`` and stores
    member-shape alternatives here. Runtime must satisfy at least one set.
    """
    variants: List[List[str]] = []
    seen: set[str] = set()
    for op in synthesized_program.get("ops") or []:
        lowering_family = _program_lowering_family(_payload(op))
        args = _payload(_payload(op).get("arguments"))
        for variant in args.get("member_argument_variants") or []:
            variant_payload = _payload(variant)
            shape = _payload(variant_payload.get("source_output_shape"))
            output_delta = _payload(variant_payload.get("output_shape_delta"))
            signals = _shape_variant_signals(
                shape,
                output_delta=output_delta,
                lowering_family=lowering_family,
            )
            signals.extend(
                _canonical_role_ref_signals(
                    variant_payload.get("source_output_refs")
                    or variant_payload.get("source_refs")
                    or [],
                    hard_gate=True,
                )
            )
            signals = _non_broad_trigger_signals(signals)
            if not signals:
                continue
            key = "\n".join(signals)
            if key in seen:
                continue
            seen.add(key)
            variants.append(signals)
    return variants


def _program_required_pred_signals(
    pred_current: Dict[str, Any],
    synthesized_program: Dict[str, Any],
) -> List[str]:
    """Derive runtime gates from the executable shared program.

    Required signals should describe the input shape the compiler needs, not
    incidental details from a representative case. Case-specific predicate
    counts, literal counts, and table counts stay optional unless the canonical
    op itself edits predicate scope.
    """
    ops = [_payload(op) for op in synthesized_program.get("ops") or []]
    if not ops:
        return []
    variant_sets = _program_variant_required_signal_sets(synthesized_program)
    families = {_program_lowering_family(op) for op in ops}
    signals: set[str] = set()
    if variant_sets:
        common = set(variant_sets[0])
        for variant in variant_sets[1:]:
            common &= set(variant)
        signals.update(_non_broad_trigger_signals(common))
    if families & {"where_side_edit"}:
        if pred_current.get("predicate_count") is not None:
            signals.add(f"pred.predicate_count={pred_current.get('predicate_count')}")
    aggregate_values = set()
    for op in ops:
        args = _payload(op.get("arguments"))
        for variant in args.get("member_argument_variants") or []:
            shape_payload = _payload(_payload(variant).get("source_output_shape"))
            if shape_payload.get("has_aggregate") is not None:
                aggregate_values.add(bool(shape_payload.get("has_aggregate")))
    if len(aggregate_values) == 1:
        signals.add(f"pred.has_aggregate={next(iter(aggregate_values))}")
    return _non_broad_trigger_signals(signals)


def _program_canonical_discriminants(
    synthesized_program: Dict[str, Any],
    variant_required_signal_sets: List[List[str]],
) -> List[str]:
    signals: set[str] = set()
    for signal_set in variant_required_signal_sets:
        signals.update(_non_broad_trigger_signals(signal_set))
    for op in synthesized_program.get("ops") or []:
        payload = _payload(op)
        signals.update(
            _canonical_role_ref_signals(payload.get("role_refs") or [], hard_gate=True)
        )
        args = _payload(payload.get("arguments"))
        signals.update(
            _canonical_role_ref_signals(args.get("canonical_refs") or [], hard_gate=True)
        )
    return sorted(signals)


def _program_lowering_families(synthesized_program: Dict[str, Any]) -> List[str]:
    return sorted(
        {
            family
            for op in (synthesized_program.get("ops") or [])
            if (family := _program_lowering_family(_payload(op)))
        }
    )


def _required_role_slots_from_program(synthesized_program: Dict[str, Any]) -> List[str]:
    envelope = _payload(synthesized_program.get("program_envelope"))
    required_rows = envelope.get("required_role_slots") or []
    if required_rows:
        return [
            json.dumps(_payload(row), sort_keys=True, ensure_ascii=False, default=str)
            for row in required_rows
            if _payload(row)
        ]
    slots: set[str] = set()
    for op in synthesized_program.get("ops") or []:
        args = _payload(_payload(op).get("arguments"))
        shared = _payload(args.get("shared_signature"))
        for slot in shared.get("common_slot_signature") or []:
            if str(slot):
                slots.add(str(slot))
    return sorted(slots)


def _target_invariants_from_program(synthesized_program: Dict[str, Any]) -> List[str]:
    envelope = _payload(synthesized_program.get("program_envelope"))
    rows = envelope.get("target_invariants") or []
    if rows:
        values: List[str] = []
        for row in rows:
            payload = _payload(row)
            kind = str(payload.get("kind") or "")
            value = str(payload.get("value") or "")
            if kind and value:
                values.append(f"{kind}={value}")
            elif kind:
                values.append(kind)
        if values:
            return sorted(set(values))
    values: set[str] = set()
    for op in synthesized_program.get("ops") or []:
        payload = _payload(op)
        args = _payload(payload.get("arguments"))
        shared = _payload(args.get("shared_arguments"))
        for invariant in (
            shared.get("target_invariants")
            or payload.get("target_invariants")
            or []
        ):
            if str(invariant):
                values.add(str(invariant))
    return sorted(values)


def _source_free_rewrite_hint_template(
    *,
    structural: Dict[str, Any],
    synthesized_program: Dict[str, Any],
) -> str:
    """Return a runtime-safe hint that describes the contract, not a source SQL."""
    lowering = _program_lowering_families(synthesized_program)
    locus = str(structural.get("locus") or "").strip() or "the selected SQL scope"
    op_family = str(structural.get("op_family") or "").strip() or "apply"
    target_family = str(structural.get("target_family") or "").strip() or "the target slot"
    if lowering:
        return (
            "Apply the selected compiler actions for the learned "
            f"{', '.join(lowering)} repair program. Use only the current SQL "
            "bindings in the actions; do not copy identifiers from source cases."
        )
    return (
        f"Apply the selected {op_family} action in {locus} for {target_family}. "
        "Use only the current SQL bindings in the actions; do not copy source-case identifiers."
    )


def _signals_vary_across_variants(
    variant_sets: List[List[str]],
    prefix: str,
) -> bool:
    values = {
        signal
        for variant in variant_sets
        for signal in variant
        if signal.startswith(prefix)
    }
    return len(values) > 1


def _generalize_required_signals_for_variants(
    required: List[str],
    variant_sets: List[List[str]],
) -> List[str]:
    if not variant_sets:
        return sorted(set(required))
    generalized = set(required)
    if _signals_vary_across_variants(variant_sets, "pred.select_arity="):
        generalized = {
            signal for signal in generalized if not signal.startswith("pred.select_arity=")
        }
    if _signals_vary_across_variants(variant_sets, "pred.output_grain="):
        generalized = {
            signal for signal in generalized if not signal.startswith("pred.output_grain=")
        }
    if _signals_vary_across_variants(variant_sets, "pred.output_role="):
        generalized = {
            signal for signal in generalized if not signal.startswith("pred.output_role=")
        }
    if _signals_vary_across_variants(variant_sets, "pred.has_aggregate="):
        generalized = {
            signal for signal in generalized if not signal.startswith("pred.has_aggregate=")
        }
    return sorted(generalized)


def build_trigger_contract(
    *,
    formation_signals: Dict[str, Any],
    error_instance: Optional[ErrorInstanceV2] = None,
    decisive_pred_signals: Optional[List[str]] = None,
    decisive_question_signals: Optional[List[str]] = None,
    negative_signals: Optional[List[str]] = None,
    max_actions: int = 1,
) -> Dict[str, Any]:
    """Build a contract trigger from source-case signals and repair metadata."""
    error_payload = _payload(error_instance)
    skeleton = _payload(error_payload.get("repair_skeleton") or formation_signals.get("repair_skeleton"))
    structural = _payload(skeleton.get("structural"))
    structural.update(derive_repair_structural_from_delta(formation_signals))
    pred_current = _payload(formation_signals.get("pred_current"))
    pred_signals = _pred_contract_signals(pred_current)
    core_pred_signals = _core_pred_contract_signals(pred_current, structural)
    decisive = sorted(set(decisive_pred_signals or []))
    decisive = [f"pred.decisive_tag={tag}" for tag in decisive if tag]
    question_decisive = sorted(set(decisive_question_signals or []))
    question_decisive = [f"question.decisive_tag={tag}" for tag in question_decisive if tag]
    core_question_signals = _core_question_contract_signals(decisive_question_signals or [])
    required = sorted(set(core_pred_signals + core_question_signals))
    slots = error_payload.get("instantiation_slots") or []
    slot_kinds = sorted(
        {
            str(_payload(slot).get("kind"))
            for slot in slots
            if _payload(slot).get("kind")
        }
    )
    repair_program = [
        dict(step)
        for step in (
            error_payload.get("repair_program")
            or _as_payload_list(formation_signals.get("repair_program"))
        )
        if isinstance(step, dict) and step
    ]
    canonical_repair_ir = _payload(
        error_payload.get("canonical_repair_ir")
        or formation_signals.get("canonical_repair_ir")
    )
    synthesized_program = _payload(formation_signals.get("synthesized_program"))
    program_coverage = _payload(formation_signals.get("program_coverage"))
    repair_insight_signature = _payload(
        error_payload.get("repair_insight_signature")
        or formation_signals.get("repair_insight_signature")
        or _payload(canonical_repair_ir).get("repair_insight_signature")
        or synthesized_program.get("repair_insight_signature")
        or _payload(_payload(synthesized_program.get("program_envelope")).get("repair_insight_signature"))
    )
    program_unresolved_axes = [
        str(axis)
        for axis in (synthesized_program.get("unresolved_variation_axes") or [])
        if str(axis)
    ]
    program_compile_coverage = 0.0
    runtime_binding_coverage = 0.0
    try:
        program_compile_coverage = float(program_coverage.get("compile_coverage") or 0.0)
    except Exception:
        program_compile_coverage = 0.0
    try:
        runtime_binding_coverage = float(program_coverage.get("runtime_binding_coverage") or 0.0)
    except Exception:
        runtime_binding_coverage = 0.0
    compiler_deterministic = bool(
        synthesized_program.get("ops")
        and not program_unresolved_axes
        and runtime_binding_coverage >= 1.0
    )
    variant_required_signal_sets = _program_variant_required_signal_sets(synthesized_program)
    program_required = _program_required_pred_signals(pred_current, synthesized_program)
    has_synthesized_program = bool(synthesized_program.get("ops"))
    canonical_discriminants = _program_canonical_discriminants(
        synthesized_program,
        variant_required_signal_sets,
    )
    lowering_families = _program_lowering_families(synthesized_program)
    envelope = _payload(synthesized_program.get("program_envelope"))
    action_envelope = _payload(envelope.get("action_envelope"))
    try:
        derived_max_actions = int(action_envelope.get("max_actions_hint") or max_actions)
    except Exception:
        derived_max_actions = max_actions
    deterministic_only_families = {"select_drop", "where_side_edit"}
    selection_policy = "llm_required"
    if compiler_deterministic:
        selection_policy = (
            "deterministic_only"
            if lowering_families
            and set(lowering_families) <= deterministic_only_families
            else "deterministic_allowed"
        )
    if has_synthesized_program:
        # The canonical program is the executable contract. Keep noisy
        # representative-case skeleton signals as optional audit evidence.
        required = sorted(set(program_required + core_question_signals))
    required = _non_broad_trigger_signals(
        _generalize_required_signals_for_variants(
            required,
            variant_required_signal_sets,
        )
    )
    variant_decisive_pred_signals = sorted(
        {
            signal
            for variant in variant_required_signal_sets
            for signal in variant
        }
    )
    shape_delta = _payload(structural.get("output_shape_delta"))
    answer_unit_contract = _answer_unit_contract(
        output_shape_delta=shape_delta,
        pred_current=pred_current,
    )
    pre_condition = _payload(formation_signals.get("pre_condition_local"))
    contract_negative_signals = set(negative_signals or [])
    audit_signals = sorted(
        set(pred_signals)
        | set(decisive)
        | set(question_decisive)
        | set(variant_decisive_pred_signals)
        | set(_as_list((_payload(formation_signals.get("delta"))).get("delta_axes")))
        | {"pred.select_arity_present=True"}
    )
    optional_signals = sorted(set(audit_signals) - set(required))
    if has_synthesized_program:
        decisive_pred_source = variant_decisive_pred_signals or program_required
    else:
        decisive_pred_source = (
            variant_decisive_pred_signals
            or core_pred_signals
            or decisive
            or pred_signals
        )
    return {
        "schema_version": "trigger-contract-v0",
        "required_signals": required,
        "optional_signals": optional_signals,
        "audit_signals": audit_signals,
        "negative_signals": sorted(contract_negative_signals),
        "decisive_pred_signals": _non_broad_trigger_signals(decisive_pred_source),
        "variant_required_signal_sets": variant_required_signal_sets,
        "canonical_discriminants": canonical_discriminants,
        "trigger_policy": {
            "allow_out_of_variant_generalization": False,
            "min_canonical_discriminants": 1,
            "requires_binder_dry_run": True,
        },
        "action_contract": {
            "locus": structural.get("locus"),
            "op_family": structural.get("op_family"),
            "target_family": structural.get("target_family"),
            "output_shape_delta": structural.get("output_shape_delta"),
            "answer_unit_contract": answer_unit_contract,
            "legacy_output_contract": structural.get("output_contract"),
            "slot_kinds": slot_kinds,
            "max_actions": derived_max_actions,
            "repair_program": repair_program,
            "canonical_repair_ir_summary": compact_canonical_repair_ir_for_memory(
                canonical_repair_ir
            ),
            "synthesized_program_summary": compact_synthesized_program_for_contract(
                synthesized_program
            ),
            "program_coverage": program_coverage,
            "has_synthesized_program": bool(synthesized_program.get("ops")),
            "program_type": synthesized_program.get("program_type"),
            "selection_policy": selection_policy,
            "compiler_deterministic": compiler_deterministic,
            "lowering_families": lowering_families,
            "required_role_slots": _required_role_slots_from_program(synthesized_program),
            "required_target_invariants": _target_invariants_from_program(synthesized_program),
            "program_envelope_summary": compact_synthesized_program_for_contract(
                synthesized_program
            ).get("program_envelope_summary", {}),
            "repair_insight_signature": repair_insight_signature,
            "rewrite_hint_template": _source_free_rewrite_hint_template(
                structural=structural,
                synthesized_program=synthesized_program,
            ),
        },
        "source_case_contract": pred_current,
        "pre_condition": pre_condition,
        "max_actions": derived_max_actions,
    }


__all__ = [
    "apply_delta_structural_override",
    "build_formation_signals",
    "build_trigger_contract",
    "compact_canonical_repair_ir_for_memory",
    "compact_synthesized_program_for_memory",
    "compact_synthesized_program_for_contract",
    "derive_repair_structural_from_delta",
]
