"""Synthesize shared canonical repair programs from singleton evidence.

Executable canonical ops are learned from case-extracted repair steps. The
synthesizer may use role deltas and invariants to anti-unify across surface
step variants, but it must not promote a group merely because it matches a
predefined semantic error label.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .data_structures_v2 import (
    CanonicalRepairIR,
    CanonicalRepairOp,
    CanonicalRepairProgram,
    GroupSummary,
    ProgramEnvelope,
    ProgramCoverage,
    RepairEffectSignature,
    RepairInsightSignature,
)


COMPILER_SUPPORTED_CANONICAL_OPS = frozenset(
    {
        "SELECT_ADD_SLOT",
        "ADD_SELECT_SLOT",
        "SELECT_OUTPUT_PATCH",
        "SELECT_REPLACE_SLOT",
        "REPLACE_SELECT_SLOT",
        "SELECT_DROP_SLOT",
        "DROP_SELECT_SLOT",
        "JOIN_ADD_BRIDGE",
        "JOIN_ADD_TABLE",
        "BRIDGE_ADD_TABLE",
        "WHERE_DROP_CONDITION",
        "WHERE_REPLACE_CONDITION",
    }
)


def _payload(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return dict(getattr(value, "__dict__", {}) or {})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _normalize_op_type(op_type: Any) -> str:
    return str(op_type or "").strip().upper()


def _lowering_family(op_type: Any, locus: Any = "") -> str:
    op = _normalize_op_type(op_type)
    locus_u = str(locus or "").strip().upper()
    if op == "SELECT_OUTPUT_PATCH":
        return "select_output_patch"
    if op in {"SELECT_ADD_SLOT", "ADD_SELECT_SLOT"}:
        return "select_add"
    if op in {"SELECT_REPLACE_SLOT", "REPLACE_SELECT_SLOT"}:
        return "select_replace"
    if op in {"SELECT_DROP_SLOT", "DROP_SELECT_SLOT"}:
        return "select_drop"
    if op in {"JOIN_ADD_BRIDGE", "JOIN_ADD_TABLE", "BRIDGE_ADD_TABLE"}:
        return "join_bridge"
    if op in {"WHERE_DROP_CONDITION", "WHERE_REPLACE_CONDITION"}:
        return "where_side_edit"
    if locus_u == "SELECT" and "ADD" in op and "SLOT" in op:
        return "select_add"
    if locus_u == "SELECT" and "REPLACE" in op and "SLOT" in op:
        return "select_replace"
    if locus_u == "SELECT" and "DROP" in op and "SLOT" in op:
        return "select_drop"
    if locus_u in {"JOIN", "BRIDGE"} and ("BRIDGE" in op or "JOIN" in op) and "ADD" in op:
        return "join_bridge"
    if locus_u in {"WHERE", "PREDICATE", "SCOPE"} and (
        "DROP" in op or "REPLACE" in op or "REMOVE" in op
    ):
        return "where_side_edit"
    return ""


def canonical_op_lowering_family(op_type: Any, locus: Any = "") -> str:
    """Return the compiler lowering family for a case-extracted edit DSL op."""
    return _lowering_family(op_type, locus)


def _representative_op_type(member_ops: Sequence[CanonicalRepairOp], lowering_family: str) -> str:
    op_types = sorted({_normalize_op_type(op.op_type) for op in member_ops if _normalize_op_type(op.op_type)})
    if len(op_types) == 1:
        return op_types[0]
    if lowering_family == "select_output_patch":
        return "SELECT_OUTPUT_PATCH"
    if lowering_family == "select_add":
        return "SELECT_ADD_SLOT"
    if lowering_family == "select_replace":
        return "SELECT_REPLACE_SLOT"
    if lowering_family == "select_drop":
        return "SELECT_DROP_SLOT"
    if lowering_family == "join_bridge":
        return "JOIN_ADD_BRIDGE"
    if lowering_family == "where_side_edit":
        return "WHERE_REPLACE_CONDITION"
    return op_types[0] if op_types else ""


def _case_ids(groups: Sequence[GroupSummary]) -> List[str]:
    return sorted({str(case_id) for group in groups for case_id in group.case_ids})


def _program_id(db_id: str, case_ids: Sequence[str], keys: Sequence[Tuple[str, ...]]) -> str:
    raw = "|".join([db_id, ",".join(case_ids), _canonical_json(list(keys))])
    return "canon-prog-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _ir_from_group(group: GroupSummary) -> Optional[CanonicalRepairIR]:
    signals = _payload(getattr(group, "formation_signals", None))
    ir_payload = signals.get("canonical_repair_ir")
    if ir_payload:
        try:
            return CanonicalRepairIR.model_validate(ir_payload)
        except Exception:
            pass
    program = getattr(group.instantiation_program, "synthesized_program", None)
    if program is not None and len(group.case_ids) == 1:
        ops = [
            op if isinstance(op, CanonicalRepairOp) else CanonicalRepairOp.model_validate(op)
            for op in program.ops
        ]
        return CanonicalRepairIR(
            db_id=group.db_id,
            case_id=str(group.case_ids[0]),
            program_ops=ops,
        )
    return None


def _signature_payload(op: CanonicalRepairOp) -> Dict[str, Any]:
    args = _payload(op.arguments)
    signature = _payload(args.get("operation_signature") or args.get("shared_signature"))
    role_delta = _payload(signature.get("role_delta"))
    output_path_delta = _payload(signature.get("output_path_delta"))
    relation_delta = _payload(signature.get("relation_delta"))
    predicate_scope_delta = _payload(signature.get("predicate_scope_delta"))
    grain_delta = _payload(signature.get("grain_delta"))
    if not role_delta:
        role_delta = {
            "arity_direction": _payload(args.get("output_shape_delta")).get("arity_direction"),
            "source_output_roles": args.get("source_output_roles") or [],
            "target_output_roles": args.get("target_output_roles") or [],
        }
    if not output_path_delta:
        output_path_delta = _payload(args.get("output_path_delta"))
    if not relation_delta:
        relation_delta = _payload(args.get("relation_delta"))
    if not predicate_scope_delta:
        predicate_scope_delta = _payload(args.get("predicate_scope_delta"))
    if not grain_delta:
        grain_delta = _payload(args.get("grain_delta"))
    return {
        "lowering_family": _lowering_family(op.op_type, op.locus),
        "locus": str(signature.get("locus") or op.locus or "").upper(),
        "is_dependency": bool(signature.get("is_dependency") or False),
        "required": bool(signature.get("required", True)),
        "slot_signature": signature.get("slot_signature") or args.get("step_slots") or [],
        "role_delta": role_delta,
        "output_path_delta": output_path_delta,
        "relation_delta": relation_delta,
        "predicate_scope_delta": predicate_scope_delta,
        "grain_delta": grain_delta,
        "invariants": sorted(str(item) for item in (op.invariants or []) if str(item)),
    }


def _shared_signature_key(op: CanonicalRepairOp) -> Tuple[str, ...]:
    payload = _signature_payload(op)
    lowering = str(payload.get("lowering_family") or "")
    role_delta = _payload(payload.get("role_delta"))
    return (
        lowering,
        str(payload.get("locus") or ""),
        _canonical_json(payload.get("slot_signature") or []),
        _canonical_json(role_delta),
    )


def _bucket_key(op: CanonicalRepairOp) -> Tuple[str, str]:
    payload = _signature_payload(op)
    return (
        str(payload.get("lowering_family") or ""),
        str(payload.get("locus") or ""),
    )


def _generalized_bucket_key(op: CanonicalRepairOp) -> Tuple[str, str]:
    payload = _signature_payload(op)
    lowering = str(payload.get("lowering_family") or "")
    locus = str(payload.get("locus") or "")
    if locus == "SELECT" and lowering in {"select_add", "select_replace"}:
        return ("select_output_patch", locus)
    return lowering, locus


def _effect_candidates_from_payload(effect: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in effect.get("effect_candidates") or []:
        payload = _payload(item)
        axis = str(payload.get("axis") or "").strip()
        if not axis:
            continue
        rows.append(payload)
    return rows


def _effect_candidates_for_op(op: CanonicalRepairOp) -> List[Dict[str, Any]]:
    return _effect_candidates_from_payload(_repair_effect_signature_payload(op))


def _effect_output_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    output = _payload(state.get("output"))
    shape = _payload(state.get("shape"))
    side_binding_count = output.get("role_side_group_count")
    if side_binding_count is None:
        side_binding_count = int(output.get("direct_role_path_count") or 0) + int(
            output.get("derived_role_path_count") or 0
        )
    return {
        "arity": output.get("arity") if output.get("arity") is not None else shape.get("arity"),
        "side_binding_count": side_binding_count,
        "has_aggregate": shape.get("has_aggregate"),
        "has_distinct": shape.get("has_distinct"),
    }


def _effect_route_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    route = _payload(state.get("route"))
    return {
        "table_count": route.get("table_count"),
        "relation_count": route.get("relation_count"),
        "relation_role_counts": _payload(route.get("relation_role_counts")),
        "table_relation_role_counts": _payload(route.get("table_relation_role_counts")),
    }


def _effect_predicate_summary(state: Dict[str, Any]) -> Dict[str, Any]:
    predicates = _payload(state.get("predicates"))
    return {
        "predicate_count": predicates.get("predicate_count"),
        "predicate_ref_role_counts": _payload(predicates.get("predicate_ref_role_counts")),
    }


def _effect_candidate_signature(candidate: Dict[str, Any]) -> Dict[str, Any]:
    delta = _payload(candidate.get("delta"))
    actionability = _payload(candidate.get("actionability"))
    # Formation keys must represent the reusable repair effect, not the exact
    # source/target SQL snapshot.  Detailed state stays in member_variants when
    # effects are merged, so reviewers can still inspect whether a broadened
    # bucket caused a false positive.
    return {
        "axis": str(candidate.get("axis") or ""),
        "delta": {
            "kind": delta.get("kind"),
            "arity_direction": delta.get("arity_direction"),
            "target_is_subset_of_source": delta.get("target_is_subset_of_source"),
        },
        "primitive": str(actionability.get("primitive") or ""),
    }


def _effect_candidate_key(candidate: Dict[str, Any]) -> Tuple[str, str]:
    return ("contrastive_effect", _canonical_json(_effect_candidate_signature(candidate)))


def _candidate_role(candidate: Dict[str, Any]) -> str:
    return str(candidate.get("role") or "").strip().lower()


def _bucket_effect_candidates_for_op(op: CanonicalRepairOp) -> List[Dict[str, Any]]:
    candidates = [
        candidate
        for candidate in _effect_candidates_for_op(op)
        if _candidate_role(candidate) != "noise"
    ]
    primary = [candidate for candidate in candidates if _candidate_role(candidate) == "primary"]
    return sorted(primary or candidates, key=lambda item: _canonical_json(_effect_candidate_signature(item)))


def _ops_by_bucket(ir: CanonicalRepairIR) -> Dict[Tuple[str, str], CanonicalRepairOp]:
    out: Dict[Tuple[str, str], CanonicalRepairOp] = {}
    for op in ir.program_ops or []:
        key = _bucket_key(op)
        if not key[0]:
            continue
        out.setdefault(key, op)
    return out


def _ops_by_generalized_bucket(ir: CanonicalRepairIR) -> Dict[Tuple[str, str], CanonicalRepairOp]:
    out: Dict[Tuple[str, str], CanonicalRepairOp] = {}
    for op in ir.program_ops or []:
        key = _generalized_bucket_key(op)
        if not key[0]:
            continue
        out.setdefault(key, op)
    return out


def _effect_bucket_key(op: CanonicalRepairOp) -> Optional[Tuple[str, str]]:
    candidates = _bucket_effect_candidates_for_op(op)
    if candidates:
        return _effect_candidate_key(candidates[0])

    effect = _repair_effect_signature_payload(op)
    effect_payload = {
        key: _payload(effect.get(key))
        for key in (
            "output_effect",
            "relation_effect",
            "predicate_scope_effect",
            "grain_effect",
            "field_binding_effect",
            "ranking_effect",
        )
    }
    if not any(effect_payload.values()):
        return None
    signature = _signature_payload(op)
    bucket_payload = {
        "effect_kind": _effect_kind_from_op(op),
        "effect_signature": effect_payload,
        "role_side_group": _role_side_group_for_op(op),
        "target_invariants": _target_invariants_from_op(op),
        "allowed_action_primitives": _allowed_primitives_for_op(op),
        "required": bool(signature.get("required", True)),
        "is_dependency": bool(signature.get("is_dependency") or False),
    }
    return ("effect_program", _canonical_json(bucket_payload))


def _ops_by_effect_bucket(ir: CanonicalRepairIR) -> Dict[Tuple[str, str], CanonicalRepairOp]:
    out: Dict[Tuple[str, str], CanonicalRepairOp] = {}
    for op in ir.program_ops or []:
        key = _effect_bucket_key(op)
        if not key:
            continue
        out.setdefault(key, op)
    return out


def _recognized_invariant_envelope_tokens(op: CanonicalRepairOp) -> List[str]:
    signature = _signature_payload(op)
    output_path_delta = _payload(signature.get("output_path_delta"))
    relation_delta = _payload(signature.get("relation_delta"))
    predicate_scope_delta = _payload(signature.get("predicate_scope_delta"))
    grain_delta = _payload(signature.get("grain_delta"))
    tokens: set[str] = set()
    if (
        output_path_delta.get("target_output_subset_of_source")
        or "target_output_subset_of_source_outputs" in (op.invariants or [])
    ):
        tokens.add("target_output_subset_of_source_outputs")
    for invariant in list(_target_invariants_from_op(op)) + list(op.invariants or []):
        text = str(invariant)
        if not text:
            continue
        if text.startswith("target_added_relation_equality="):
            tokens.add(text)
        elif text.startswith("target_relation_role_equality="):
            tokens.add(text)
        elif text.startswith("target_output_arity="):
            tokens.add(text)
        elif text.startswith("target_output_roles="):
            tokens.add(text)
        elif text.startswith("target_output_grain="):
            tokens.add(text)
        elif text == "grain_changed":
            tokens.add(text)
    if relation_delta.get("added_relation_equalities"):
        for relation in relation_delta.get("added_relation_equalities") or []:
            tokens.add(f"target_added_relation_equality={relation}")
    if predicate_scope_delta and (
        predicate_scope_delta.get("removed_source_predicates")
        or predicate_scope_delta.get("added_target_predicates")
        or predicate_scope_delta.get("possible_scope_move")
    ):
        tokens.add("predicate_scope_delta")
    if grain_delta and any(
        grain_delta.get(field) not in (None, "", False)
        for field in (
            "source_grain",
            "target_grain",
            "grain_changed",
            "target_has_aggregate",
            "target_has_distinct",
        )
    ):
        tokens.add("grain_delta")
    return sorted(tokens)


def _invariant_envelope_bucket(op: CanonicalRepairOp) -> Optional[Tuple[str, str]]:
    lowering, locus = _generalized_bucket_key(op)
    if not lowering:
        return None
    signature = _signature_payload(op)
    role_delta = _payload(signature.get("role_delta"))
    role_delta_summary = {
        "arity_direction": role_delta.get("arity_direction"),
        "target_output_subset_of_source": role_delta.get("target_output_subset_of_source"),
        "source_output_roles": sorted(str(role) for role in (role_delta.get("source_output_roles") or []) if str(role)),
        "target_output_roles": sorted(str(role) for role in (role_delta.get("target_output_roles") or []) if str(role)),
        "common_source_output_roles": sorted(str(role) for role in (role_delta.get("common_source_output_roles") or []) if str(role)),
        "common_target_output_roles": sorted(str(role) for role in (role_delta.get("common_target_output_roles") or []) if str(role)),
    }
    tokens = _recognized_invariant_envelope_tokens(op)
    if not tokens and not any(role_delta_summary.values()):
        return None
    return (
        lowering,
        _canonical_json(
            {
                "locus": locus,
                "tokens": tokens,
                "role_delta": role_delta_summary,
            }
        ),
    )


def _ops_by_invariant_envelope(ir: CanonicalRepairIR) -> Dict[Tuple[str, str], CanonicalRepairOp]:
    out: Dict[Tuple[str, str], CanonicalRepairOp] = {}
    for op in ir.program_ops or []:
        key = _invariant_envelope_bucket(op)
        if not key:
            continue
        out.setdefault(key, op)
    return out


def _envelope_fallback_evidence_ok(member_ops: Sequence[CanonicalRepairOp]) -> bool:
    return all(_invariant_envelope_bucket(op) is not None for op in member_ops)


def _effect_fallback_evidence_ok(member_ops: Sequence[CanonicalRepairOp]) -> bool:
    return all(_effect_bucket_key(op) is not None for op in member_ops)


def _common_invariants(ops: Sequence[CanonicalRepairOp]) -> List[str]:
    sets = [
        set(str(item) for item in (op.invariants or []) if str(item))
        for op in ops
    ]
    if not sets:
        return []
    return sorted(set.intersection(*sets))


def _common_slot_signature(ops: Sequence[CanonicalRepairOp]) -> List[Dict[str, Any]]:
    sets: List[set[str]] = []
    by_key: Dict[str, Dict[str, Any]] = {}
    for op in ops:
        items = []
        for item in _signature_payload(op).get("slot_signature") or []:
            payload = _payload(item)
            if not payload:
                continue
            key = _canonical_json(payload)
            by_key[key] = payload
            items.append(key)
        if items:
            sets.append(set(items))
    if not sets:
        return []
    return [by_key[key] for key in sorted(set.intersection(*sets))]


def _generalized_role_delta(ops: Sequence[CanonicalRepairOp]) -> Dict[str, Any]:
    deltas = [_payload(_signature_payload(op).get("role_delta")) for op in ops]
    if not deltas:
        return {}
    out: Dict[str, Any] = {}
    for key in ("arity_direction", "target_output_subset_of_source"):
        values = [delta.get(key) for delta in deltas]
        if all(value not in (None, "") for value in values) and all(
            str(value) == str(values[0]) for value in values
        ):
            out[key] = values[0]
    for key in ("source_output_roles", "target_output_roles"):
        sequences = [tuple(str(role) for role in (delta.get(key) or [])) for delta in deltas]
        if all(sequences) and all(sequence == sequences[0] for sequence in sequences):
            out[key] = list(sequences[0])
            continue
        role_sets = [set(sequence) for sequence in sequences if sequence]
        if role_sets:
            common_roles = sorted(set.intersection(*role_sets))
            if common_roles:
                out[f"common_{key}"] = common_roles
    return out


def _shared_evidence_ok(ops: Sequence[CanonicalRepairOp]) -> bool:
    if _common_invariants(ops):
        return True
    if _common_slot_signature(ops):
        return True
    role_delta = _generalized_role_delta(ops)
    return bool(role_delta)


def _shared_program_key(
    bucket_key: Tuple[str, str],
    member_ops: Sequence[CanonicalRepairOp],
) -> Tuple[str, ...]:
    payloads = [_signature_payload(op) for op in member_ops]
    is_dependency_values = [bool(payload.get("is_dependency") or False) for payload in payloads]
    required_values = [bool(payload.get("required", True)) for payload in payloads]
    return (
        bucket_key[0],
        bucket_key[1],
        str(is_dependency_values[0]) if is_dependency_values and all(value == is_dependency_values[0] for value in is_dependency_values) else "mixed_dependency",
        str(required_values[0]) if required_values and all(value == required_values[0] for value in required_values) else "mixed_required",
        _canonical_json(_common_slot_signature(member_ops)),
        _canonical_json(_generalized_role_delta(member_ops)),
        _canonical_json(_common_invariants(member_ops)),
    )


def _shared_lowering_family(
    member_ops: Sequence[CanonicalRepairOp],
    fallback: str,
) -> str:
    generalized = [
        _generalized_bucket_key(op)[0]
        for op in member_ops
        if _generalized_bucket_key(op)[0]
    ]
    generalized_set = set(generalized)
    if len(generalized_set) == 1:
        return generalized[0]
    if generalized_set <= {"select_add", "select_replace", "select_output_patch"}:
        return "select_output_patch"
    effect_to_lowering = {
        "output_subset": "select_drop",
        "output_expand": "select_add",
        "output_replace": "select_replace",
        "field_switch": "select_replace",
        "add_relation": "join_bridge",
        "remove_relation": "join_bridge",
        "predicate_move": "where_side_edit",
        "predicate_add": "where_side_edit",
        "predicate_drop": "where_side_edit",
        "grain_change": "select_replace",
    }
    for kind in (_effect_kind_from_op(op) for op in member_ops):
        lowering = effect_to_lowering.get(str(kind))
        if lowering:
            return lowering
    return generalized[0] if generalized else fallback


def _common_scalar_arguments(ops: Sequence[CanonicalRepairOp]) -> Dict[str, Any]:
    if not ops:
        return {}
    arg_payloads = [_payload(op.arguments) for op in ops]
    keys = set(arg_payloads[0])
    for payload in arg_payloads[1:]:
        keys &= set(payload)
    common: Dict[str, Any] = {}
    for key in sorted(keys):
        values = [payload.get(key) for payload in arg_payloads]
        if all(_canonical_json(value) == _canonical_json(values[0]) for value in values):
            common[key] = values[0]
    shape_values = [_payload(payload.get("output_shape_delta")) for payload in arg_payloads]
    shape_common: Dict[str, Any] = {}
    for shape_key in (
        "current_arity",
        "target_arity",
        "arity_delta",
        "arity_direction",
        "operation",
        "current_grain",
        "target_grain",
        "current_roles",
        "target_roles",
    ):
        values = [shape.get(shape_key) for shape in shape_values if shape.get(shape_key) is not None]
        if values and len(values) == len(shape_values) and all(
            _canonical_json(value) == _canonical_json(values[0]) for value in values
        ):
            shape_common[shape_key] = values[0]
    if shape_common:
        common["output_shape_delta"] = shape_common
    return common


def _shared_variables(ops: Sequence[CanonicalRepairOp]) -> Dict[str, Any]:
    source_roles = []
    target_roles = []
    relation_roles = []
    for op in ops:
        args = _payload(op.arguments)
        source_roles.append(tuple(str(role) for role in args.get("source_output_roles") or []))
        target_roles.append(tuple(str(role) for role in args.get("target_output_roles") or []))
        relation_roles.append(
            tuple(
                sorted(
                    {
                        str(_payload(ref).get("relation_role"))
                        for ref in (_payload(ref) for ref in args.get("source_output_refs") or [])
                        if str(ref.get("relation_role") or "")
                    }
                )
            )
        )
    variables: Dict[str, Any] = {}
    if source_roles:
        variables["source_output_role_sequences"] = sorted(set(source_roles))
    if target_roles:
        variables["target_output_role_sequences"] = sorted(set(target_roles))
    if relation_roles:
        variables["source_relation_role_sets"] = sorted(set(relation_roles))
    variables["binding_policy"] = (
        "bind concrete tables/columns from the current runtime role graph; "
        "do not reuse source-case identifiers"
    )
    return variables


def _target_invariants_from_op(op: CanonicalRepairOp) -> List[str]:
    args = _payload(op.arguments)
    values = list(args.get("target_invariants") or [])
    values.extend(item for item in (op.invariants or []) if str(item).startswith("target_"))
    shared = _payload(args.get("shared_arguments"))
    values.extend(shared.get("target_invariants") or [])
    return sorted({str(value) for value in values if str(value)})


def _common_target_invariants(ops: Sequence[CanonicalRepairOp]) -> List[str]:
    sets = [set(_target_invariants_from_op(op)) for op in ops if _target_invariants_from_op(op)]
    if not sets:
        return []
    return sorted(set.intersection(*sets))


def _merge_signature_section_values(values: Sequence[Any]) -> Any:
    present = [value for value in values if value not in (None, "", [], {})]
    if not present:
        return {}
    if all(isinstance(value, dict) for value in present):
        merged: Dict[str, Any] = {}
        keys = sorted({str(key) for value in present for key in value.keys()})
        for key in keys:
            merged_value = _merge_signature_section_values(
                [value.get(key) for value in present if key in value]
            )
            if merged_value not in (None, "", [], {}):
                merged[key] = merged_value
        return merged
    if all(isinstance(value, list) for value in present):
        item_maps: List[Dict[str, Any]] = []
        item_sets: List[set[str]] = []
        for value in present:
            row_map: Dict[str, Any] = {}
            for item in value:
                key = _canonical_json(_payload(item))
                row_map[key] = item
            item_maps.append(row_map)
            item_sets.append(set(row_map))
        common_keys = set.intersection(*item_sets) if item_sets else set()
        return [item_maps[0][key] for key in sorted(common_keys)]
    canonical_values = {_canonical_json(_payload(value)) for value in present}
    if len(canonical_values) == 1:
        return present[0]
    return {}


def _shared_signature_section(
    member_ops: Sequence[CanonicalRepairOp],
    key: str,
) -> Dict[str, Any]:
    return _payload(
        _merge_signature_section_values(
            [_signature_section(op, key) for op in member_ops]
        )
    )


def _variation_axes(member_ops: Sequence[CanonicalRepairOp]) -> List[str]:
    axes: List[str] = []
    payloads = [_signature_payload(op) for op in member_ops]
    arg_payloads = [_payload(op.arguments) for op in member_ops]
    shape_values = [_payload(payload.get("output_shape_delta")) for payload in arg_payloads]
    for shape_key in ("current_arity", "target_arity", "arity_direction", "current_grain", "target_grain"):
        values = [
            str(shape.get(shape_key))
            for shape in shape_values
            if shape.get(shape_key) is not None
        ]
        if values and len(set(values)) > 1:
            axes.append(f"output_shape.{shape_key}")
    slot_payloads = [payload.get("slot_signature") or [] for payload in payloads]
    if len({_canonical_json(value) for value in slot_payloads}) > 1 and not _common_slot_signature(member_ops):
        axes.append("slot_signature")
    role_deltas = [_payload(payload.get("role_delta")) for payload in payloads]
    if len({_canonical_json(value) for value in role_deltas}) > 1 and not _generalized_role_delta(member_ops):
        axes.append("role_delta")
    target_invariant_sets = [set(_target_invariants_from_op(op)) for op in member_ops]
    if any(target_invariant_sets) and len({_canonical_json(sorted(value)) for value in target_invariant_sets}) > 1:
        if not _common_target_invariants(member_ops):
            axes.append("target_invariant")
    required_values = {str(payload.get("required", True)) for payload in payloads}
    if len(required_values) > 1:
        axes.append("requiredness")
    dependency_values = {str(payload.get("is_dependency") or False) for payload in payloads}
    if len(dependency_values) > 1:
        axes.append("dependency_role")
    return sorted(set(axes))


def _program_type_from_ops(ops: Sequence[CanonicalRepairOp]) -> str:
    families = []
    for op in ops:
        args = _payload(op.arguments)
        shared_signature = _payload(args.get("shared_signature"))
        family = str(shared_signature.get("lowering_family") or _lowering_family(op.op_type, op.locus))
        if family:
            families.append(family)
    return "+".join(sorted(set(families))) if families else None


def _unique_payloads(values: Iterable[Dict[str, Any]], limit: int = 6) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        payload = _payload(value)
        key = _canonical_json(payload)
        if key in seen:
            continue
        seen.add(key)
        rows.append(payload)
        if len(rows) >= limit:
            break
    return rows


def _merged_effect_candidates_from_ops(ops: Sequence[CanonicalRepairOp]) -> List[Dict[str, Any]]:
    if not ops:
        return []
    grouped: Dict[Tuple[str, str], List[Tuple[CanonicalRepairOp, Dict[str, Any]]]] = {}
    for op in ops:
        for candidate in _effect_candidates_for_op(op):
            if _candidate_role(candidate) == "noise":
                continue
            key = _effect_candidate_key(candidate)
            grouped.setdefault(key, []).append((op, candidate))

    merged_rows: List[Dict[str, Any]] = []
    op_count = len(ops)
    for key, pairs in sorted(grouped.items(), key=lambda item: _canonical_json(item[0])):
        if op_count > 1:
            supported_op_ids = {str(op.op_id) for op, _candidate in pairs}
            if len(supported_op_ids) < op_count:
                continue
        representative = pairs[0][1]
        signature = _effect_candidate_signature(representative)
        supporting_case_ids = sorted(
            {
                str(case_id)
                for op, _candidate in pairs
                for case_id in (op.supporting_case_ids or [])
                if str(case_id)
            }
        )
        roles = [_candidate_role(candidate) for _op, candidate in pairs]
        role = "primary" if "primary" in roles else roles[0] if roles else "dependency"
        actionability_rows = [_payload(candidate.get("actionability")) for _op, candidate in pairs]
        primitive_values = sorted(
            {
                str(row.get("primitive") or "")
                for row in actionability_rows
                if str(row.get("primitive") or "")
            }
        )
        confidence_values = [
            float(candidate.get("confidence") or 0.0)
            for _op, candidate in pairs
        ]
        effect_id_raw = _canonical_json({"key": key, "cases": supporting_case_ids})
        merged_rows.append(
            {
                "effect_id": "merged-effect-" + hashlib.sha1(effect_id_raw.encode("utf-8")).hexdigest()[:10],
                "axis": str(representative.get("axis") or ""),
                "source_state": {
                    "abstract": signature.get("source_state") or {},
                    "member_variants": _unique_payloads(
                        [_payload(candidate.get("source_state")) for _op, candidate in pairs]
                    ),
                },
                "target_state": {
                    "abstract": signature.get("target_state") or {},
                    "member_variants": _unique_payloads(
                        [_payload(candidate.get("target_state")) for _op, candidate in pairs]
                    ),
                },
                "delta": {
                    "abstract": signature.get("delta") or {},
                    "member_variants": _unique_payloads(
                        [_payload(candidate.get("delta")) for _op, candidate in pairs]
                    ),
                },
                "role": role,
                "triggerability": {
                    "support_count": len(supporting_case_ids) or len(pairs),
                    "source_visible_in_runtime": "member_specific",
                    "target_bindable_from_schema_or_memory": "member_specific",
                },
                "actionability": {
                    "primitive": primitive_values[0] if len(primitive_values) == 1 else "multiple",
                    "primitive_values": primitive_values,
                    "arguments_bindable": "member_specific",
                    "branch_selection_answer_blind": "member_specific",
                },
                "evidence": {
                    "source": "merged_contrastive_effect",
                    "compatibility_signature": signature,
                    "supporting_case_ids": supporting_case_ids,
                    "member_op_ids": sorted({str(op.op_id) for op, _candidate in pairs}),
                },
                "confidence": min(confidence_values) if confidence_values else 0.0,
            }
        )
    return merged_rows


def _program_repair_effect_signature_from_ops(
    ops: Sequence[CanonicalRepairOp],
) -> Optional[RepairEffectSignature]:
    if not ops:
        return None
    merged: Dict[str, Any] = {}
    merged["effect_candidates"] = _merged_effect_candidates_from_ops(ops)
    for key in (
        "output_effect",
        "relation_effect",
        "predicate_scope_effect",
        "grain_effect",
        "field_binding_effect",
        "ranking_effect",
    ):
        merged_value = _merge_signature_section_values(
            [_repair_effect_signature_payload(op).get(key) for op in ops]
        )
        merged[key] = _payload(merged_value)
    if not any(merged.values()):
        return None
    return RepairEffectSignature.model_validate(merged)


def _target_effect_rows(ops: Sequence[CanonicalRepairOp]) -> List[Dict[str, Any]]:
    effect = _program_repair_effect_signature_from_ops(ops)
    if effect is None:
        return []
    payload = effect.model_dump(mode="json")
    effect_candidates = _effect_candidates_from_payload(payload)
    if effect_candidates:
        return [
            {
                "effect_slot": "contrastive_effect",
                "effect_axis": candidate.get("axis"),
                "role": candidate.get("role"),
                "target_state": _payload(candidate.get("target_state")),
                "delta": _payload(candidate.get("delta")),
                "actionability": _payload(candidate.get("actionability")),
                "evidence": _payload(candidate.get("evidence")),
            }
            for candidate in effect_candidates
            if _candidate_role(candidate) != "noise"
        ]
    rows: List[Dict[str, Any]] = []
    for key in (
        "output_effect",
        "relation_effect",
        "predicate_scope_effect",
        "grain_effect",
        "field_binding_effect",
        "ranking_effect",
    ):
        row = _payload(payload.get(key))
        if not row:
            continue
        substantive = any(
            value not in (None, "", [], {}, False)
            for field, value in row.items()
            if field != "kind"
        )
        if not str(row.get("kind") or "").strip() and not substantive:
            continue
        rows.append({"effect_slot": key, **row})
    return rows


def _signature_section(op: CanonicalRepairOp, key: str) -> Dict[str, Any]:
    return _payload(_signature_payload(op).get(key))


def _required_role_slots_for_envelope(ops: Sequence[CanonicalRepairOp]) -> List[Dict[str, Any]]:
    slots = _common_slot_signature(ops)
    return [dict(_payload(slot)) for slot in slots if _payload(slot)]


def _source_antipatterns_from_ops(ops: Sequence[CanonicalRepairOp]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for op in ops:
        for candidate in _bucket_effect_candidates_for_op(op):
            row = {
                "kind": "contrastive_source_state",
                "effect_axis": candidate.get("axis"),
                "role": candidate.get("role"),
                "source_state": _payload(candidate.get("source_state")),
                "delta": _payload(candidate.get("delta")),
                "actionability": _payload(candidate.get("actionability")),
            }
            key = _canonical_json(row)
            if key not in seen:
                seen.add(key)
                rows.append(row)
    for op in ops:
        signature = _signature_payload(op)
        output_path_delta = _payload(signature.get("output_path_delta"))
        relation_delta = _payload(signature.get("relation_delta"))
        predicate_scope_delta = _payload(signature.get("predicate_scope_delta"))
        grain_delta = _payload(signature.get("grain_delta"))
        lowering = str(signature.get("lowering_family") or "")
        if output_path_delta:
            row = {
                "kind": "output_path_delta",
                "lowering_family": lowering,
                "target_output_subset_of_source": bool(
                    output_path_delta.get("target_output_subset_of_source")
                ),
                "same_table_multi_role_output": bool(
                    output_path_delta.get("same_table_multi_role_output")
                ),
                "same_attribute_multi_role_output": bool(
                    output_path_delta.get("same_attribute_multi_role_output")
                ),
                "source_output_path_roles": list(
                    output_path_delta.get("source_output_path_roles") or []
                )[:12],
            }
            key = _canonical_json(row)
            if key not in seen:
                seen.add(key)
                rows.append(row)
        if relation_delta and (
            relation_delta.get("added_relation_equalities")
            or relation_delta.get("removed_relation_equalities")
        ):
            row = {
                "kind": "relation_delta",
                "lowering_family": lowering,
                "added_relation_equalities": list(
                    relation_delta.get("added_relation_equalities") or []
                )[:8],
                "removed_relation_equalities": list(
                    relation_delta.get("removed_relation_equalities") or []
                )[:8],
            }
            key = _canonical_json(row)
            if key not in seen:
                seen.add(key)
                rows.append(row)
        if predicate_scope_delta and (
            predicate_scope_delta.get("removed_source_predicates")
            or predicate_scope_delta.get("added_target_predicates")
        ):
            row = {
                "kind": "predicate_scope_delta",
                "lowering_family": lowering,
                "removed_source_predicates": list(
                    predicate_scope_delta.get("removed_source_predicates") or []
                )[:6],
                "added_target_predicates": list(
                    predicate_scope_delta.get("added_target_predicates") or []
                )[:6],
                "possible_scope_move": bool(
                    predicate_scope_delta.get("possible_scope_move")
                ),
            }
            key = _canonical_json(row)
            if key not in seen:
                seen.add(key)
                rows.append(row)
        if grain_delta and any(
            grain_delta.get(field) not in (None, "", False)
            for field in ("source_grain", "target_grain", "grain_changed")
        ):
            row = {
                "kind": "grain_delta",
                "lowering_family": lowering,
                "source_grain": grain_delta.get("source_grain"),
                "target_grain": grain_delta.get("target_grain"),
                "grain_changed": bool(grain_delta.get("grain_changed")),
                "target_has_aggregate": bool(grain_delta.get("target_has_aggregate")),
                "target_has_distinct": bool(grain_delta.get("target_has_distinct")),
            }
            key = _canonical_json(row)
            if key not in seen:
                seen.add(key)
                rows.append(row)
    return rows


def _target_invariant_rows(values: Sequence[str]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for value in values or []:
        text = str(value)
        if not text:
            continue
        if "=" in text:
            kind, raw = text.split("=", 1)
            rows.append({"kind": kind, "value": raw})
        else:
            rows.append({"kind": text})
    return rows


def _repair_effect_signature_payload(op: CanonicalRepairOp) -> Dict[str, Any]:
    args = _payload(op.arguments)
    effect = _payload(args.get("repair_effect_signature"))
    if effect:
        return effect
    signature = _signature_payload(op)
    return {
        "output_effect": {
            "kind": (
                "output_subset"
                if _payload(signature.get("output_path_delta")).get("target_output_subset_of_source")
                else ""
            ),
            **_payload(signature.get("output_path_delta")),
        },
        "relation_effect": _payload(signature.get("relation_delta")),
        "predicate_scope_effect": _payload(signature.get("predicate_scope_delta")),
        "grain_effect": _payload(signature.get("grain_delta")),
        "field_binding_effect": {
            "kind": "",
            "source_output_roles": _payload(signature.get("role_delta")).get("source_output_roles") or [],
            "target_output_roles": _payload(signature.get("role_delta")).get("target_output_roles") or [],
        },
        "ranking_effect": {},
    }


def _repair_insight_payload(op: CanonicalRepairOp) -> Dict[str, Any]:
    args = _payload(op.arguments)
    insight = _payload(args.get("repair_insight_signature"))
    if insight:
        return insight
    for variant in args.get("member_argument_variants") or []:
        payload = _payload(variant)
        insight = _payload(payload.get("repair_insight_signature"))
        if insight:
            return insight
    return {}


def _normalize_insight_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = " ".join(text.replace("_", " ").replace("-", " ").split())
    return "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text).strip()


def _insight_interface_key(insight: Dict[str, Any]) -> str:
    key = _normalize_insight_key(insight.get("interface_key"))
    if key:
        return key
    return _normalize_insight_key(insight.get("repair_interface"))


def _normalized_statement_set(insights: Sequence[Dict[str, Any]], key: str) -> set[str]:
    return {
        _normalize_insight_key(insight.get(key))
        for insight in insights
        if _normalize_insight_key(insight.get(key))
    }


def _required_slot_keys(insight: Dict[str, Any], source_or_target: str) -> set[str]:
    wanted = str(source_or_target or "").strip().lower()
    keys: set[str] = set()
    for slot in insight.get("binding_slots") or []:
        payload = _payload(slot)
        if not payload or not bool(payload.get("required", True)):
            continue
        side = str(payload.get("source_or_target") or "").strip().lower()
        if wanted and side and side != wanted:
            continue
        kind = str(payload.get("kind") or "").strip().lower()
        roles = ",".join(
            sorted(str(role).strip().lower() for role in (payload.get("allowed_role_families") or []) if str(role).strip())
        )
        if kind or roles:
            keys.add(f"{side}:{kind}:{roles}")
    return keys


def _insight_constraint_blockers(insights: Sequence[Dict[str, Any]]) -> List[str]:
    blockers: List[str] = []
    target_preferences = _normalized_statement_set(insights, "target_preference")
    # target_preference is the case-local target contract. If the extractor
    # emits different stable target contracts for the same interface key, code
    # must fail closed instead of merging by the broad axis.
    if len(target_preferences) > 1:
        blockers.append(
            "insight_target_preference_conflict:" + "|".join(sorted(target_preferences)[:6])
        )

    for side in ("source", "target", "preserve"):
        slot_sets = [_required_slot_keys(insight, side) for insight in insights]
        populated = [values for values in slot_sets if values]
        if len(populated) >= 2 and not set.intersection(*populated):
            blockers.append(f"insight_{side}_slot_conflict")

    preserve_sets = [
        {
            _normalize_insight_key(item)
            for item in (insight.get("preserve_invariants") or [])
            if _normalize_insight_key(item)
        }
        for insight in insights
    ]
    populated_preserve = [values for values in preserve_sets if values]
    if len(populated_preserve) >= 2 and not set.intersection(*populated_preserve):
        blockers.append("insight_preserve_invariant_conflict")
    return blockers


def _merge_insight_rows(
    insights: Sequence[Dict[str, Any]],
    key: str,
    *,
    list_limit: int = 16,
) -> List[Any]:
    rows: List[Any] = []
    seen: set[str] = set()
    for insight in insights:
        values = insight.get(key) or []
        if isinstance(values, str):
            values = [values]
        for value in values:
            payload = _payload(value) if isinstance(value, dict) else value
            canonical = _canonical_json(payload)
            if canonical in seen:
                continue
            seen.add(canonical)
            rows.append(payload)
            if len(rows) >= list_limit:
                return rows
    return rows


_INSIGHT_JUDGE_CACHE: Dict[str, Dict[str, Any]] = {}


def _truncate_text(value: Any, limit: int = 500) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _compact_effect_for_judge(candidate: Dict[str, Any]) -> Dict[str, Any]:
    payload = _payload(candidate)
    delta = _payload(payload.get("delta"))
    actionability = _payload(payload.get("actionability"))
    triggerability = _payload(payload.get("triggerability"))
    return {
        "axis": payload.get("axis"),
        "role": payload.get("role"),
        "delta": {
            "kind": delta.get("kind"),
            "arity_direction": delta.get("arity_direction"),
            "target_is_subset_of_source": delta.get("target_is_subset_of_source"),
        },
        "primitive": actionability.get("primitive"),
        "triggerability": {
            "source_visible_in_runtime": triggerability.get("source_visible_in_runtime"),
            "target_bindable_from_schema_or_memory": triggerability.get("target_bindable_from_schema_or_memory"),
        },
        "confidence": payload.get("confidence"),
    }


def _compact_insight_for_judge(
    op: CanonicalRepairOp,
    insight: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "case_ids": [str(case_id) for case_id in (op.supporting_case_ids or [])],
        "interface_key": _truncate_text(insight.get("interface_key"), 180),
        "source_misread": _truncate_text(insight.get("source_misread"), 500),
        "target_preference": _truncate_text(insight.get("target_preference"), 500),
        "repair_interface": _truncate_text(insight.get("repair_interface"), 500),
        "binding_slots": [
            {
                key: _payload(slot).get(key)
                for key in ("name", "kind", "source_or_target", "required", "allowed_role_families", "description")
            }
            for slot in (insight.get("binding_slots") or [])[:8]
            if _payload(slot)
        ],
        "preserve_invariants": [
            _truncate_text(item, 220) for item in (insight.get("preserve_invariants") or [])[:8]
        ],
        "negative_guards": [
            _truncate_text(item, 220) for item in (insight.get("negative_guards") or [])[:8]
        ],
        "axis_links": [
            _payload(item) for item in (insight.get("axis_links") or [])[:8] if _payload(item)
        ],
        "evidence": _payload(insight.get("evidence")),
    }


def _repair_program_summary_for_judge(op: CanonicalRepairOp) -> Dict[str, Any]:
    args = _payload(op.arguments)
    shared_signature = _payload(args.get("shared_signature") or args.get("operation_signature"))
    signature = _signature_payload(op)
    return {
        "case_ids": [str(case_id) for case_id in (op.supporting_case_ids or [])],
        "op_type": op.op_type,
        "locus": op.locus,
        "lowering_family": _lowering_family(op.op_type, op.locus),
        "is_dependency": bool(shared_signature.get("is_dependency") or False),
        "required": bool(shared_signature.get("required", True)),
        "role_delta": _payload(signature.get("role_delta")),
        "slot_signature": signature.get("slot_signature") or [],
        "invariants": list(op.invariants or [])[:12],
    }


def _judge_cache_key(
    *,
    insight_cards: Sequence[Dict[str, Any]],
    effect_candidates: Sequence[Dict[str, Any]],
    repair_program_summaries: Sequence[Dict[str, Any]],
) -> str:
    return hashlib.sha1(
        _canonical_json(
            {
                "insight_cards": insight_cards,
                "effect_candidates": effect_candidates,
                "repair_program_summaries": repair_program_summaries,
            }
        ).encode("utf-8")
    ).hexdigest()


def _call_shared_insight_judge(
    *,
    member_ops: Sequence[CanonicalRepairOp],
    insights: Sequence[Dict[str, Any]],
    preliminary_blockers: Sequence[str],
) -> Dict[str, Any]:
    insight_cards = [
        _compact_insight_for_judge(op, insight)
        for op, insight in zip(member_ops, insights)
    ]
    effect_candidates = []
    for op in member_ops:
        effect_candidates.append(
            {
                "case_ids": [str(case_id) for case_id in (op.supporting_case_ids or [])],
                "effects": [
                    _compact_effect_for_judge(candidate)
                    for candidate in _effect_candidates_for_op(op)[:8]
                ],
            }
        )
    repair_program_summaries = [
        _repair_program_summary_for_judge(op) for op in member_ops
    ]
    cache_key = _judge_cache_key(
        insight_cards=insight_cards,
        effect_candidates=effect_candidates,
        repair_program_summaries=repair_program_summaries,
    )
    if cache_key in _INSIGHT_JUDGE_CACHE:
        return dict(_INSIGHT_JUDGE_CACHE[cache_key])

    from .llm_utils_v2 import call_llm
    from .prompts_v2.shared_insight_judge import build_shared_insight_judge_prompt

    prompt = build_shared_insight_judge_prompt(
        insight_cards_json=json.dumps(
            {
                "preliminary_code_conflicts": list(preliminary_blockers),
                "cards": insight_cards,
            },
            ensure_ascii=False,
            indent=2,
        ),
        effect_candidates_json=json.dumps(effect_candidates, ensure_ascii=False, indent=2),
        repair_program_summaries_json=json.dumps(
            repair_program_summaries,
            ensure_ascii=False,
            indent=2,
        ),
    )
    raw = call_llm(
        prompt,
        expect_json=True,
        stage="shared_insight_judge",
        trace_context={
            "member_count": len(member_ops or []),
            "case_ids": sorted(
                {
                    str(case_id)
                    for op in member_ops or []
                    for case_id in (getattr(op, "supporting_case_ids", []) or [])
                }
            ),
        },
    )
    response = dict(raw) if isinstance(raw, dict) else {}
    _INSIGHT_JUDGE_CACHE[cache_key] = response
    return response


def _repair_insight_from_judge_response(
    *,
    response: Dict[str, Any],
    member_ops: Sequence[CanonicalRepairOp],
    insights: Sequence[Dict[str, Any]],
    interface_keys: Sequence[str],
) -> RepairInsightSignature:
    shared = _payload(response.get("shared_insight"))
    axis_links = _merge_insight_rows(insights, "axis_links")
    compatibility = str(response.get("compatibility") or "").strip().lower()
    shared_interface_key = (
        _truncate_text(response.get("shared_interface_key"), 180)
        or _truncate_text(shared.get("repair_interface"), 180)
        or _truncate_text(shared.get("source_misread"), 180)
        or (interface_keys[0] if interface_keys else "shared repair insight")
    )
    evidence = {
        "source": "shared_insight_judge",
        "compatibility": compatibility,
        "supporting_case_ids": sorted(
            {
                str(case_id)
                for op in member_ops
                for case_id in (op.supporting_case_ids or [])
                if str(case_id)
            }
        ),
        "member_interface_keys": list(interface_keys),
        "conflict_reasons": list(response.get("conflict_reasons") or []),
        "lost_constraints": list(response.get("lost_constraints") or []),
        "unresolved_axes": list(response.get("unresolved_axes") or []),
        "required_code_checks": list(response.get("required_code_checks") or []),
        "rationale": response.get("rationale"),
    }
    return RepairInsightSignature(
        interface_key=shared_interface_key,
        source_misread=_truncate_text(
            shared.get("source_misread")
            or " / ".join(
                _truncate_text(insight.get("source_misread"), 240)
                for insight in insights
                if insight.get("source_misread")
            ),
            800,
        ),
        target_preference=_truncate_text(
            shared.get("target_preference")
            or " / ".join(
                _truncate_text(insight.get("target_preference"), 240)
                for insight in insights
                if insight.get("target_preference")
            ),
            800,
        ),
        repair_interface=_truncate_text(
            shared.get("repair_interface") or shared_interface_key,
            800,
        ),
        binding_slots=[
            row
            for row in (
                shared.get("binding_slots")
                if isinstance(shared.get("binding_slots"), list)
                else _merge_insight_rows(insights, "binding_slots", list_limit=20)
            )
            if isinstance(row, dict)
        ],
        preserve_invariants=[
            str(row)
            for row in (
                shared.get("preserve_invariants")
                if isinstance(shared.get("preserve_invariants"), list)
                else _merge_insight_rows(insights, "preserve_invariants", list_limit=20)
            )
            if str(row)
        ],
        negative_guards=[
            str(row)
            for row in (
                shared.get("negative_guards")
                if isinstance(shared.get("negative_guards"), list)
                else _merge_insight_rows(insights, "negative_guards", list_limit=20)
            )
            if str(row)
        ],
        axis_links=[
            row
            for row in (
                shared.get("axis_links")
                if isinstance(shared.get("axis_links"), list)
                else axis_links
            )
            if isinstance(row, dict)
        ],
        evidence=evidence,
        confidence="medium" if compatibility == "compatible" else "low",
    )


def _deterministic_shared_repair_insight(
    *,
    member_ops: Sequence[CanonicalRepairOp],
    insights: Sequence[Dict[str, Any]],
    interface_keys: Sequence[str],
) -> RepairInsightSignature:
    axis_links = _merge_insight_rows(insights, "axis_links")
    common_axis_values = sorted(
        set.intersection(
            *[
                {
                    str(_payload(axis).get("axis") or "")
                    for axis in (insight.get("axis_links") or [])
                    if str(_payload(axis).get("axis") or "")
                }
                for insight in insights
            ]
        )
        if all(insight.get("axis_links") for insight in insights)
        else set()
    )
    evidence = {
        "source": "deterministic_shared_case_local_insight",
        "supporting_case_ids": sorted(
            {
                str(case_id)
                for op in member_ops
                for case_id in (op.supporting_case_ids or [])
                if str(case_id)
            }
        ),
        "member_interface_keys": list(interface_keys),
        "common_axis_values": common_axis_values,
        "member_evidence": [
            _payload(insight.get("evidence")) for insight in insights[:6] if insight.get("evidence")
        ],
    }
    return RepairInsightSignature(
        interface_key=interface_keys[0],
        source_misread=" / ".join(
            row
            for row in [
                str(insight.get("source_misread") or "").strip()
                for insight in insights
            ]
            if row
        )[:800],
        target_preference=" / ".join(
            row
            for row in [
                str(insight.get("target_preference") or "").strip()
                for insight in insights
            ]
            if row
        )[:800],
        repair_interface=str(insights[0].get("repair_interface") or interface_keys[0]),
        binding_slots=[
            row
            for row in _merge_insight_rows(insights, "binding_slots", list_limit=20)
            if isinstance(row, dict)
        ],
        preserve_invariants=[
            str(row)
            for row in _merge_insight_rows(insights, "preserve_invariants", list_limit=20)
            if str(row)
        ],
        negative_guards=[
            str(row)
            for row in _merge_insight_rows(insights, "negative_guards", list_limit=20)
            if str(row)
        ],
        axis_links=[row for row in axis_links if isinstance(row, dict)],
        evidence=evidence,
        confidence="medium",
    )


def _shared_repair_insight_from_ops(
    member_ops: Sequence[CanonicalRepairOp],
) -> Tuple[Optional[RepairInsightSignature], List[str]]:
    insights = [_repair_insight_payload(op) for op in member_ops]
    missing_case_ids: List[str] = []
    missing_count = 0
    for op, insight in zip(member_ops, insights):
        if insight:
            continue
        missing_count += 1
        missing_case_ids.extend(str(case_id) for case_id in (op.supporting_case_ids or []) if str(case_id))
    if missing_count:
        suffix = ",".join(sorted(set(missing_case_ids))) or f"ops={missing_count}"
        return None, ["missing_case_local_insight:" + suffix]

    interface_keys = sorted(
        {
            _insight_interface_key(insight)
            for insight in insights
            if _insight_interface_key(insight)
        }
    )
    if not interface_keys:
        return None, ["missing_insight_interface_key"]

    preliminary_blockers: List[str] = []
    if len(interface_keys) > 1:
        preliminary_blockers.append(
            "insight_interface_conflict:" + "|".join(interface_keys[:6])
        )
    preliminary_blockers.extend(_insight_constraint_blockers(insights))

    try:
        judge_response = _call_shared_insight_judge(
            member_ops=member_ops,
            insights=insights,
            preliminary_blockers=preliminary_blockers,
        )
    except Exception as exc:
        return None, [
            *preliminary_blockers,
            f"insight_judge_error:{type(exc).__name__}",
        ]

    compatibility = str(judge_response.get("compatibility") or "").strip().lower()
    if compatibility == "compatible":
        return (
            _repair_insight_from_judge_response(
                response=judge_response,
                member_ops=member_ops,
                insights=insights,
                interface_keys=interface_keys,
            ),
            [],
        )
    if compatibility == "partial":
        unresolved = [
            str(item)
            for item in (judge_response.get("unresolved_axes") or [])
            if str(item)
        ]
        reasons = [
            str(reason)
            for reason in (judge_response.get("conflict_reasons") or [])
            if str(reason)
        ]
        suffix = "|".join((unresolved or reasons)[:6]) or "unresolved_shared_interface"
        return None, [
            *preliminary_blockers,
            "insight_judge_partial:" + suffix,
        ]
    reasons = [
        str(reason)
        for reason in (judge_response.get("conflict_reasons") or [])
        if str(reason)
    ]
    lost = [
        str(item)
        for item in (judge_response.get("lost_constraints") or [])
        if str(item)
    ]
    suffix = "|".join((reasons or lost)[:6]) or compatibility or "conflict"
    return None, [
        *preliminary_blockers,
        "insight_judge_conflict:" + suffix,
    ]


def _program_repair_insight_signature_from_ops(
    ops: Sequence[CanonicalRepairOp],
) -> Optional[RepairInsightSignature]:
    insight, blockers = _shared_repair_insight_from_ops(ops)
    return None if blockers else insight


def _allowed_primitives_for_op(op: CanonicalRepairOp) -> List[str]:
    signature = _signature_payload(op)
    lowering = str(signature.get("lowering_family") or "")
    predicate_scope_delta = _payload(signature.get("predicate_scope_delta"))
    grain_delta = _payload(signature.get("grain_delta"))
    output_path_delta = _payload(signature.get("output_path_delta"))
    args = _payload(op.arguments)
    accessory_policies = list(args.get("accessory_policies") or [])
    allowed: List[str] = []
    for candidate in _effect_candidates_for_op(op):
        actionability = _payload(candidate.get("actionability"))
        primitive = str(actionability.get("primitive") or "")
        if primitive and primitive != "unknown":
            allowed.append(primitive)
    if lowering == "select_drop":
        allowed.extend(["DROP_SELECT_SLOT", "DROP_SIDE"])
    elif lowering == "where_side_edit":
        allowed.append("DROP_SIDE")
        if predicate_scope_delta.get("possible_scope_move"):
            allowed.append("MOVE_CONDITION")
    elif lowering == "join_bridge":
        allowed.extend(["INSERT_BRIDGE", "REROUTE_FACT"])
    elif lowering == "select_add":
        allowed.append("ADD_SELECT_SLOT")
    elif lowering == "select_replace":
        allowed.append("REPLACE_SELECT_SLOT")
        if grain_delta.get("grain_changed"):
            allowed.append("CHANGE_GRAIN")
        if not grain_delta.get("grain_changed") and not output_path_delta.get("target_output_subset_of_source"):
            allowed.append("SWITCH_CANONICAL_FIELD")
    elif lowering == "select_output_patch":
        allowed.append("REPLACE_SELECT_SLOT")
    if any(
        str(_payload(policy).get("op") or "").upper() in {"ORDER_BY_APPLY", "LIMIT_APPLY"}
        for policy in accessory_policies
    ):
        allowed.append("MATERIALIZE_RANKING_OUTPUT")
    return sorted(dict.fromkeys(allowed))


def _effect_kind_from_op(op: CanonicalRepairOp) -> str:
    for candidate in _bucket_effect_candidates_for_op(op):
        delta = _payload(candidate.get("delta"))
        kind = str(delta.get("kind") or "").strip()
        if kind:
            return kind
        axis = str(candidate.get("axis") or "").strip()
        if axis:
            return axis
    effect = _repair_effect_signature_payload(op)
    for key in (
        "output_effect",
        "relation_effect",
        "predicate_scope_effect",
        "grain_effect",
        "field_binding_effect",
        "ranking_effect",
    ):
        kind = str(_payload(effect.get(key)).get("kind") or "").strip()
        if kind:
            return kind
    return str(_signature_payload(op).get("lowering_family") or "")


def _role_side_group_for_op(op: CanonicalRepairOp) -> str:
    effect = _repair_effect_signature_payload(op)
    output_effect = _payload(effect.get("output_effect"))
    groups = [
        str(item)
        for item in (output_effect.get("source_role_side_groups") or [])
        if str(item)
    ]
    return groups[0] if groups else ""


def _primary_primitive_for_op(op: CanonicalRepairOp) -> str:
    for candidate in _bucket_effect_candidates_for_op(op):
        primitive = str(_payload(candidate.get("actionability")).get("primitive") or "")
        if primitive and primitive != "unknown":
            return primitive
    allowed = _allowed_primitives_for_op(op)
    signature = _signature_payload(op)
    output_path_delta = _payload(signature.get("output_path_delta"))
    if (
        "DROP_SIDE" in allowed
        and output_path_delta.get("target_output_subset_of_source")
        and (
            output_path_delta.get("same_table_multi_role_output")
            or output_path_delta.get("same_attribute_multi_role_output")
            or output_path_delta.get("source_role_side_groups")
        )
    ):
        return "DROP_SIDE"
    if "MOVE_CONDITION" in allowed:
        return "MOVE_CONDITION"
    return allowed[0] if allowed else ""


def _bundle_rows_for_ops(ops: Sequence[CanonicalRepairOp]) -> List[Dict[str, Any]]:
    bundles: List[Dict[str, Any]] = []
    required_rows: List[Dict[str, Any]] = []
    for op in ops:
        signature = _signature_payload(op)
        effect_kind = _effect_kind_from_op(op)
        role_side_group = _role_side_group_for_op(op)
        target_invariants = _target_invariants_from_op(op)
        bundle_key = _canonical_json(
            {
                "effect_kind": effect_kind,
                "role_side_group": role_side_group,
                "target_invariants": target_invariants,
            }
        )
        bundle_id = "bundle:" + hashlib.sha1(bundle_key.encode("utf-8")).hexdigest()[:8]
        if bool(signature.get("is_dependency") or False) and required_rows:
            preferred = next(
                (
                    row
                    for row in required_rows
                    if row.get("effect_kind") == effect_kind
                    or not effect_kind
                ),
                required_rows[-1],
            )
            preferred.setdefault("cleanup_op_ids", []).append(str(op.op_id))
            preferred.setdefault("bundled_op_ids", []).append(str(op.op_id))
            continue
        row = {
            "bundle_id": bundle_id,
            "effect_kind": effect_kind,
            "primary_primitive": _primary_primitive_for_op(op),
            "lowering_family": str(signature.get("lowering_family") or ""),
            "role_side_group": role_side_group,
            "target_invariants": target_invariants,
            "bundled_op_ids": [str(op.op_id)],
            "cleanup_op_ids": [],
            "counts_as_action": 1,
        }
        bundles.append(row)
        required_rows.append(row)
    return bundles


def _lowering_branches_for_ops(
    ops: Sequence[CanonicalRepairOp],
    bundle_rows: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    bundle_by_op_id: Dict[str, Dict[str, Any]] = {}
    for bundle in bundle_rows or []:
        for op_id in bundle.get("bundled_op_ids") or []:
            bundle_by_op_id[str(op_id)] = bundle
    rows: List[Dict[str, Any]] = []
    for op in ops:
        signature = _signature_payload(op)
        bundle = bundle_by_op_id.get(str(op.op_id), {})
        rows.append(
            {
                "op_id": str(op.op_id),
                "bundle_id": str(bundle.get("bundle_id") or ""),
                "effect_kind": str(bundle.get("effect_kind") or ""),
                "lowering_family": str(signature.get("lowering_family") or ""),
                "required": bool(signature.get("required", True)),
                "is_dependency": bool(signature.get("is_dependency") or False),
                "variation_axes": _variation_axes([op]),
            }
        )
    return rows


def _branch_id_for_bundle(bundle: Dict[str, Any], op: CanonicalRepairOp) -> str:
    """Stable branch id from semantic/action shape, not concrete case ids."""
    raw = {
        "bundle_id": bundle.get("bundle_id"),
        "effect_kind": bundle.get("effect_kind"),
        "primary_primitive": bundle.get("primary_primitive"),
        "lowering_family": bundle.get("lowering_family"),
        "role_side_group": bundle.get("role_side_group"),
        "target_invariants": bundle.get("target_invariants") or [],
        "op_type": _normalize_op_type(op.op_type),
        "locus": str(op.locus or "").upper(),
    }
    return "br:" + hashlib.sha1(_canonical_json(raw).encode("utf-8")).hexdigest()[:12]


def _required_signals_for_branch(
    op: CanonicalRepairOp,
    bundle: Dict[str, Any],
) -> List[str]:
    """Derive answer-blind runtime preconditions from the source-side effect.

    These are deliberately generic structural facts already emitted by
    ``runtime_v2.build_current_case_signals``.  They do not include table names,
    column names, or manually labeled pattern ids.
    """
    args = _payload(op.arguments)
    signature = _signature_payload(op)
    output_shape = (
        _payload(args.get("output_shape_delta"))
        or _payload(_payload(args.get("shared_arguments")).get("output_shape_delta"))
        or _payload(_payload(args.get("shared_signature")).get("output_shape_delta"))
    )
    output_path_delta = _payload(signature.get("output_path_delta"))
    primitive = str(bundle.get("primary_primitive") or "")
    signals: List[str] = []
    current_arity = output_shape.get("current_arity")
    if current_arity is not None:
        signals.append(f"pred.select_arity={current_arity}")
        signals.append("pred.select_arity_present=True")
    if str(current_arity) == "2":
        signals.append("pred.pair_output=True")
    if primitive in {"DROP_SIDE", "DROP_SELECT_SLOT"}:
        if output_path_delta.get("target_output_subset_of_source"):
            signals.append("pred.select_arity_present=True")
        if (
            output_path_delta.get("same_table_multi_role_output")
            or output_path_delta.get("same_attribute_multi_role_output")
            or output_path_delta.get("source_role_side_groups")
        ):
            signals.append("pred.role_side_pair_output=True")
    return sorted(dict.fromkeys(signal for signal in signals if signal))


def _allowed_edit_scope_for_branch(op: CanonicalRepairOp, bundle: Dict[str, Any]) -> List[str]:
    locus = str(op.locus or "").upper()
    primitive = str(bundle.get("primary_primitive") or "")
    scopes: List[str] = []

    def add(scope: str) -> None:
        if scope and scope not in scopes:
            scopes.append(scope)

    if locus:
        add(locus)
    if primitive in {"DROP_SIDE", "DROP_SELECT_SLOT", "ADD_SELECT_SLOT", "REPLACE_SELECT_SLOT"}:
        add("SELECT")
    if primitive in {"DROP_SIDE", "REROUTE_FACT", "INSERT_BRIDGE"}:
        # Branches may carry cleanup joins as dependency edits; guard is audit-only
        # for scope, but the contract should still expose the dependency scope.
        add("JOIN")
    if primitive in {"MOVE_CONDITION"}:
        add("WHERE")
    return scopes


def _runtime_branches_for_ops(
    ops: Sequence[CanonicalRepairOp],
    bundle_rows: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    bundle_by_op_id: Dict[str, Dict[str, Any]] = {}
    for bundle in bundle_rows or []:
        for op_id in bundle.get("bundled_op_ids") or []:
            bundle_by_op_id[str(op_id)] = bundle

    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for op in ops:
        bundle = bundle_by_op_id.get(str(op.op_id), {})
        if not bundle:
            continue
        branch_id = _branch_id_for_bundle(bundle, op)
        if branch_id in seen:
            continue
        seen.add(branch_id)
        support_case_ids = sorted({str(case_id) for case_id in (op.supporting_case_ids or []) if str(case_id)})
        rows.append(
            {
                "schema_version": "runtime-branch-contract-v1",
                "branch_id": branch_id,
                "bundle_ids": [str(bundle.get("bundle_id") or "")],
                "bundled_op_ids": [
                    str(op_id)
                    for op_id in (bundle.get("bundled_op_ids") or [])
                    if str(op_id)
                ],
                "cleanup_op_ids": [
                    str(op_id)
                    for op_id in (bundle.get("cleanup_op_ids") or [])
                    if str(op_id)
                ],
                "support_case_ids": support_case_ids,
                "required_signals": _required_signals_for_branch(op, bundle),
                "negative_signals": [],
                "required_role_slots": _required_role_slots_for_envelope([op]),
                "allowed_primitives": [
                    str(bundle.get("primary_primitive") or "")
                ] if str(bundle.get("primary_primitive") or "") else [],
                "allowed_edit_scope": _allowed_edit_scope_for_branch(op, bundle),
                "preserve_constraints": [],
                "source_antipatterns": _source_antipatterns_from_ops([op]),
                "target_effects": _target_effect_rows([op]),
                "target_invariants": _target_invariant_rows(
                    list(bundle.get("target_invariants") or [])
                ),
                "negative_guards": _negative_guards_for_envelope(_variation_axes([op])),
                "effect_kind": str(bundle.get("effect_kind") or ""),
                "lowering_family": str(bundle.get("lowering_family") or ""),
                "runtime_usable": False,
                "runtime_blockers": ["branch_not_replay_validated"],
                "replay_metrics": {},
            }
        )
    return rows


def _negative_guards_for_envelope(unresolved_axes: Sequence[str]) -> List[Dict[str, Any]]:
    return [
        {"kind": "unresolved_variation_axis", "axis": str(axis)}
        for axis in unresolved_axes or []
        if str(axis)
    ]


def _program_envelope_from_ops(
    *,
    ops: Sequence[CanonicalRepairOp],
    target_invariants: Sequence[str],
    unresolved_axes: Sequence[str],
) -> ProgramEnvelope:
    bundle_rows = _bundle_rows_for_ops(ops)
    repair_insight_signature = _program_repair_insight_signature_from_ops(ops)
    allowed_primitives = sorted(
        {
            primitive
            for op in ops
            for primitive in _allowed_primitives_for_op(op)
            if primitive
        }
    )
    return ProgramEnvelope(
        source_antipatterns=_source_antipatterns_from_ops(ops),
        target_effects=_target_effect_rows(ops),
        target_invariants=_target_invariant_rows(target_invariants),
        allowed_action_primitives=allowed_primitives,
        action_envelope={
            "allowed_primitives": allowed_primitives,
            "max_actions_hint": sum(
                int(bundle.get("counts_as_action") or 0) for bundle in bundle_rows
            )
            or 1,
            "bundles": bundle_rows,
        },
        lowering_branches=_lowering_branches_for_ops(ops, bundle_rows),
        runtime_branches=_runtime_branches_for_ops(ops, bundle_rows),
        branch_selection_contract={
            "requires_current_variant_binding": bool(unresolved_axes or len(bundle_rows) > 1),
            "unresolved_variation_axes": list(unresolved_axes or []),
            "selection_unit": "runtime_branch",
        },
        required_role_slots=_required_role_slots_for_envelope(ops),
        repair_insight_signature=(
            repair_insight_signature.model_dump(mode="json")
            if repair_insight_signature is not None
            else {}
        ),
        negative_guards=_negative_guards_for_envelope(unresolved_axes),
        unresolved_variation_axes=list(unresolved_axes or []),
    )


def _program_op_summary(op: CanonicalRepairOp, role: str) -> Dict[str, Any]:
    args = _payload(op.arguments)
    shared_signature = _payload(args.get("shared_signature"))
    return {
        "op_id": op.op_id,
        "op_type": op.op_type,
        "locus": op.locus,
        "role": role,
        "lowering_family": shared_signature.get("lowering_family")
        or _lowering_family(op.op_type, op.locus),
        "supporting_case_ids": list(op.supporting_case_ids or []),
        "source_step_ids": list(op.source_step_ids or []),
    }


def _accessory_policies_from_op(op: CanonicalRepairOp) -> List[Dict[str, Any]]:
    args = _payload(op.arguments)
    variants = args.get("member_argument_variants") or []
    policies_by_key: Dict[str, Dict[str, Any]] = {}

    def binding_signature(policy_payload: Dict[str, Any]) -> Any:
        predicates = []
        for row in policy_payload.get("target_predicates") or []:
            row_payload = _payload(row)
            predicates.append(
                {
                    "normalized_predicate": row_payload.get("normalized_predicate")
                    or row_payload.get("predicate"),
                    "refs": [
                        {
                            "table": _payload(ref).get("table"),
                            "column": _payload(ref).get("column"),
                        }
                        for ref in (row_payload.get("refs") or [])
                    ],
                }
            )
        order_by = []
        for row in policy_payload.get("target_order_by") or []:
            row_payload = _payload(row)
            order_by.append(
                {
                    "normalized_expression": row_payload.get("normalized_expression")
                    or row_payload.get("expression"),
                    "direction": row_payload.get("direction"),
                    "refs": [
                        {
                            "table": _payload(ref).get("table"),
                            "column": _payload(ref).get("column"),
                        }
                        for ref in (row_payload.get("refs") or [])
                    ],
                }
            )
        source_ranking_predicates = []
        for row in policy_payload.get("source_ranking_predicates") or []:
            row_payload = _payload(row)
            source_ranking_predicates.append(
                {
                    "normalized_predicate": row_payload.get("normalized_predicate")
                    or row_payload.get("predicate"),
                    "aggregate": row_payload.get("aggregate"),
                    "refs": [
                        {
                            "table": _payload(ref).get("table"),
                            "column": _payload(ref).get("column"),
                        }
                        for ref in (row_payload.get("refs") or [])
                    ],
                }
            )
        return {
            "target_predicates": predicates,
            "target_order_by": order_by,
            "target_limit": policy_payload.get("target_limit"),
            "source_ranking_predicates": source_ranking_predicates,
        }

    for variant in variants:
        payload = _payload(variant)
        case_ids = [
            str(case_id)
            for case_id in (payload.get("supporting_case_ids") or [])
            if str(case_id)
        ]
        for policy in payload.get("accessory_policies") or []:
            policy_payload = _payload(policy)
            if not policy_payload:
                continue
            key = _canonical_json(
                {
                    "op": policy_payload.get("op"),
                    "locus": policy_payload.get("locus"),
                    "policy": policy_payload.get("policy"),
                    "guards": sorted(str(item) for item in (policy_payload.get("guards") or [])),
                    "binding_signature": binding_signature(policy_payload),
                }
            )
            row = policies_by_key.setdefault(
                key,
                {
                    **policy_payload,
                    "supporting_case_ids": [],
                    "support": 0,
                },
            )
            row["support"] = int(row.get("support") or 0) + 1
            row["supporting_case_ids"] = sorted(
                {
                    *[str(item) for item in (row.get("supporting_case_ids") or [])],
                    *case_ids,
                }
            )
    return sorted(
        policies_by_key.values(),
        key=lambda item: (
            str(item.get("op") or ""),
            str(item.get("policy") or ""),
            ",".join(str(case_id) for case_id in (item.get("supporting_case_ids") or [])),
        ),
    )


def _synthesize_op(
    *,
    key: Tuple[str, ...],
    member_ops: Sequence[CanonicalRepairOp],
    case_ids: Sequence[str],
) -> CanonicalRepairOp:
    representative = member_ops[0]
    lowering_family = _shared_lowering_family(member_ops, key[0])
    op_type = _representative_op_type(member_ops, lowering_family)
    invariants = sorted({item for op in member_ops for item in (op.invariants or [])})
    common_invariants = sorted(
        set.intersection(
            *[set(str(item) for item in (op.invariants or []) if str(item)) for op in member_ops]
        )
    ) if member_ops else []
    common_slots = _common_slot_signature(member_ops)
    generalized_role_delta = _generalized_role_delta(member_ops)
    target_invariants = _common_target_invariants(member_ops)
    variation_axes = _variation_axes(member_ops)
    output_path_delta = _shared_signature_section(member_ops, "output_path_delta")
    relation_delta = _shared_signature_section(member_ops, "relation_delta")
    predicate_scope_delta = _shared_signature_section(member_ops, "predicate_scope_delta")
    grain_delta = _shared_signature_section(member_ops, "grain_delta")
    repair_effect_signature = _program_repair_effect_signature_from_ops(member_ops)
    repair_insight_signature, insight_blockers = _shared_repair_insight_from_ops(member_ops)
    payloads = [_signature_payload(op) for op in member_ops]
    is_dependency_values = [bool(payload.get("is_dependency") or False) for payload in payloads]
    required_values = [bool(payload.get("required", True)) for payload in payloads]
    role_refs = []
    # Keep role refs as examples only. The compiler binds fresh refs from the
    # current case; these refs are not trigger keys.
    for op in member_ops[:3]:
        role_refs.extend(list(op.role_refs or [])[:8])
    return CanonicalRepairOp(
        op_id=f"shared:{lowering_family}:{hashlib.sha1(_canonical_json(key).encode('utf-8')).hexdigest()[:8]}",
        op_type=op_type,
        locus=str(_signature_payload(representative).get("locus") or representative.locus),
        role_refs=role_refs,
        arguments={
            "shared_signature": {
                "signature_key": list(key),
                "lowering_family": lowering_family,
                "common_invariants": common_invariants,
                "common_slot_signature": common_slots,
                "generalized_role_delta": generalized_role_delta,
                "output_path_delta": output_path_delta,
                "relation_delta": relation_delta,
                "predicate_scope_delta": predicate_scope_delta,
                "grain_delta": grain_delta,
                "target_invariants": target_invariants,
                "output_shape_delta": _payload(
                    _common_scalar_arguments(member_ops).get("output_shape_delta")
                ),
                "unresolved_variation_axes": variation_axes,
                "is_dependency": (
                    is_dependency_values[0]
                    if is_dependency_values and all(value == is_dependency_values[0] for value in is_dependency_values)
                    else None
                ),
                "required": (
                    required_values[0]
                    if required_values and all(value == required_values[0] for value in required_values)
                    else None
                ),
                "member_op_types": sorted({_normalize_op_type(op.op_type) for op in member_ops}),
            },
            "shared_arguments": {
                **_common_scalar_arguments(member_ops),
                "target_invariants": target_invariants,
                "unresolved_variation_axes": variation_axes,
                "output_path_delta": output_path_delta,
                "relation_delta": relation_delta,
                "predicate_scope_delta": predicate_scope_delta,
                "grain_delta": grain_delta,
            },
            "repair_effect_signature": (
                repair_effect_signature.model_dump(mode="json")
                if repair_effect_signature is not None
                else {}
            ),
            "repair_insight_signature": (
                repair_insight_signature.model_dump(mode="json")
                if repair_insight_signature is not None and not insight_blockers
                else {}
            ),
            "repair_insight_blockers": list(insight_blockers),
            "member_argument_variants": [
                {
                    **_payload(op.arguments),
                    "supporting_case_ids": list(op.supporting_case_ids or []),
                }
                for op in member_ops[:10]
            ],
            "canonical_refs": [
                _payload(ref) for ref in role_refs[:12]
            ],
        },
        invariants=invariants,
        source_step_ids=sorted({sid for op in member_ops for sid in op.source_step_ids}),
        supporting_case_ids=list(case_ids),
        confidence=min(op.confidence for op in member_ops) if member_ops else 0.0,
    )


@dataclass(frozen=True)
class SynthesisResult:
    program: Optional[CanonicalRepairProgram]
    coverage: ProgramCoverage
    synthesis_basis: str = ""
    fallback_allowed: bool = True


class SharedProgramSynthesizer:
    """Typed anti-unification over canonical ops."""

    def synthesize(
        self,
        groups: Sequence[GroupSummary],
        *,
        require_effect_program: bool = False,
    ) -> SynthesisResult:
        groups = list(groups)
        case_ids = _case_ids(groups)
        if not groups:
            return SynthesisResult(
                program=None,
                coverage=ProgramCoverage(blockers=["no_groups"]),
            )
        ir_by_case: Dict[str, CanonicalRepairIR] = {}
        missing_cases: List[str] = []
        for group in groups:
            ir = _ir_from_group(group)
            ids = [str(case_id) for case_id in group.case_ids]
            if ir is None:
                missing_cases.extend(ids)
                continue
            for case_id in ids:
                ir_by_case[case_id] = ir
        if missing_cases:
            return SynthesisResult(
                program=None,
                coverage=ProgramCoverage(
                    total_cases=len(case_ids),
                    covered_case_ids=[],
                    uncovered_case_ids=missing_cases,
                    compile_coverage=0.0,
                    blockers=["missing_canonical_repair_ir"],
                    per_case={case_id: {"covered": False, "reason": "missing_canonical_repair_ir"} for case_id in missing_cases},
                    compile_success_by_member={case_id: False for case_id in case_ids},
                    failed_members=missing_cases,
                    failure_reasons={case_id: "missing_canonical_repair_ir" for case_id in missing_cases},
                    core_op_coverage=0.0,
                ),
            )

        effect_op_maps = {
            case_id: _ops_by_effect_bucket(ir) for case_id, ir in ir_by_case.items()
        }
        skipped_insight_blockers: List[str] = []
        empty_signature_cases = sorted(
            case_id
            for case_id, ir in ir_by_case.items()
            if not any(
                _effect_bucket_key(op)
                or _bucket_key(op)[0]
                or _generalized_bucket_key(op)[0]
                or _invariant_envelope_bucket(op)
                for op in (ir.program_ops or [])
            )
        )
        if empty_signature_cases:
            return SynthesisResult(
                program=None,
                coverage=ProgramCoverage(
                    total_cases=len(case_ids),
                    covered_case_ids=[],
                    uncovered_case_ids=case_ids,
                    compile_coverage=0.0,
                    blockers=["missing_shared_operation_signature"],
                    per_case={
                        case_id: {
                            "covered": False,
                            "reason": (
                                "missing_shared_operation_signature"
                                if case_id in empty_signature_cases
                                else "no_group_shared_signature"
                            ),
                        }
                        for case_id in case_ids
                    },
                    compile_success_by_member={case_id: False for case_id in case_ids},
                    failed_members=case_ids,
                    failure_reasons={
                        case_id: (
                            "missing_shared_operation_signature"
                            if case_id in empty_signature_cases
                            else "no_group_shared_signature"
                        )
                        for case_id in case_ids
                    },
                    core_op_coverage=0.0,
                ),
            )

        common_bucket_keys: set[Tuple[str, str]] = (
            set(next(iter(effect_op_maps.values())).keys()) if effect_op_maps else set()
        )
        for op_map in effect_op_maps.values():
            common_bucket_keys &= set(op_map.keys())
        shared_items: List[Tuple[Tuple[str, ...], List[CanonicalRepairOp]]] = []
        for bucket in sorted(common_bucket_keys):
            member_ops = [
                effect_op_maps[case_id][bucket]
                for case_id in sorted(effect_op_maps)
            ]
            _shared_insight, insight_blockers = _shared_repair_insight_from_ops(member_ops)
            if insight_blockers:
                skipped_insight_blockers.extend(insight_blockers)
                continue
            if not (
                _shared_evidence_ok(member_ops)
                or _effect_fallback_evidence_ok(member_ops)
            ):
                continue
            shared_items.append((_shared_program_key(bucket, member_ops), member_ops))
        synthesis_basis = "effect" if shared_items else ""
        if not shared_items:
            op_maps = {case_id: _ops_by_bucket(ir) for case_id, ir in ir_by_case.items()}
            common_bucket_keys = (
                set(next(iter(op_maps.values())).keys()) if op_maps else set()
            )
            for op_map in op_maps.values():
                common_bucket_keys &= set(op_map.keys())
            for bucket in sorted(common_bucket_keys):
                member_ops = [op_maps[case_id][bucket] for case_id in sorted(op_maps)]
                _shared_insight, insight_blockers = _shared_repair_insight_from_ops(member_ops)
                if insight_blockers:
                    skipped_insight_blockers.extend(insight_blockers)
                    continue
                if not _shared_evidence_ok(member_ops):
                    continue
                shared_items.append((_shared_program_key(bucket, member_ops), member_ops))
            if shared_items:
                synthesis_basis = "legacy_exact_op"
        if not shared_items:
            generalized_op_maps = {
                case_id: _ops_by_generalized_bucket(ir)
                for case_id, ir in ir_by_case.items()
            }
            generalized_bucket_keys: set[Tuple[str, str]] = (
                set(next(iter(generalized_op_maps.values())).keys())
                if generalized_op_maps
                else set()
            )
            for op_map in generalized_op_maps.values():
                generalized_bucket_keys &= set(op_map.keys())
            for bucket in sorted(generalized_bucket_keys):
                member_ops = [
                    generalized_op_maps[case_id][bucket]
                    for case_id in sorted(generalized_op_maps)
                ]
                _shared_insight, insight_blockers = _shared_repair_insight_from_ops(member_ops)
                if insight_blockers:
                    skipped_insight_blockers.extend(insight_blockers)
                    continue
                if not _shared_evidence_ok(member_ops):
                    continue
                shared_items.append((_shared_program_key(bucket, member_ops), member_ops))
            if shared_items:
                synthesis_basis = "legacy_generalized_op"
        if not shared_items:
            envelope_op_maps = {
                case_id: _ops_by_invariant_envelope(ir)
                for case_id, ir in ir_by_case.items()
            }
            envelope_bucket_keys: set[Tuple[str, str]] = (
                set(next(iter(envelope_op_maps.values())).keys())
                if envelope_op_maps
                else set()
            )
            for op_map in envelope_op_maps.values():
                envelope_bucket_keys &= set(op_map.keys())
            for bucket in sorted(envelope_bucket_keys):
                member_ops = [
                    envelope_op_maps[case_id][bucket]
                    for case_id in sorted(envelope_op_maps)
                ]
                _shared_insight, insight_blockers = _shared_repair_insight_from_ops(member_ops)
                if insight_blockers:
                    skipped_insight_blockers.extend(insight_blockers)
                    continue
                if not (
                    _shared_evidence_ok(member_ops)
                    or _envelope_fallback_evidence_ok(member_ops)
                ):
                    continue
                shared_items.append((_shared_program_key(bucket, member_ops), member_ops))
            if shared_items:
                synthesis_basis = "legacy_invariant_envelope"
        if not shared_items:
            return SynthesisResult(
                program=None,
                coverage=ProgramCoverage(
                    total_cases=len(case_ids),
                    covered_case_ids=[],
                    uncovered_case_ids=case_ids,
                    compile_coverage=0.0,
                    blockers=["no_shared_canonical_program", *sorted(set(skipped_insight_blockers))],
                    per_case={case_id: {"covered": False, "reason": "no_shared_canonical_program", "insight_blockers": sorted(set(skipped_insight_blockers))} for case_id in case_ids},
                    compile_success_by_member={case_id: False for case_id in case_ids},
                    failed_members=case_ids,
                    failure_reasons={case_id: "no_shared_canonical_program" for case_id in case_ids},
                    core_op_coverage=0.0,
                ),
            )

        shared_ops: List[CanonicalRepairOp] = []
        ordered_items = sorted(shared_items, key=lambda item: item[0])
        for key, member_ops in ordered_items:
            shared_ops.append(
                _synthesize_op(key=key, member_ops=member_ops, case_ids=case_ids)
            )
        ordered_keys = [key for key, _member_ops in ordered_items]

        unsupported = sorted(
            {
                op.op_type
                for op in shared_ops
                if not _lowering_family(op.op_type, op.locus)
            }
        )
        supported = sorted(
            {
                op.op_type
                for op in shared_ops
                if _lowering_family(op.op_type, op.locus)
            }
        )
        unresolved_axes = sorted(
            {
                axis
                for op in shared_ops
                for axis in (
                    _payload(_payload(op.arguments).get("shared_signature")).get("unresolved_variation_axes")
                    or []
                )
                if str(axis)
            }
        )
        target_invariants = sorted(
            {
                str(invariant)
                for op in shared_ops
                for invariant in (
                    _payload(_payload(op.arguments).get("shared_arguments")).get("target_invariants")
                    or []
                )
                if str(invariant)
            }
        )
        blockers = [f"unsupported_canonical_op:{op}" for op in unsupported]
        covered_cases = [] if blockers else case_ids
        uncovered_cases = case_ids if blockers else []
        static_coverage = (len(covered_cases) / len(case_ids) if case_ids else 0.0)
        bundle_rows = _bundle_rows_for_ops(shared_ops)
        coverage = ProgramCoverage(
            total_cases=len(case_ids),
            covered_case_ids=covered_cases,
            uncovered_case_ids=uncovered_cases,
            compile_coverage=static_coverage,
            static_program_coverage=static_coverage,
            runtime_binding_coverage=0.0,
            member_candidate_coverage=static_coverage,
            blockers=blockers,
            static_blockers=blockers,
            runtime_binding_blockers={},
            per_case={
                case_id: {
                    "covered": not blockers,
                    "op_keys": [list(key) for key in ordered_keys],
                    "blockers": blockers,
                    "unresolved_variation_axes": unresolved_axes,
                    "synthesis_basis": synthesis_basis,
                }
                for case_id in case_ids
            },
            compile_success_by_member={case_id: not blockers for case_id in case_ids},
            failed_members=uncovered_cases,
            failure_reasons={case_id: ";".join(blockers) for case_id in uncovered_cases},
            mean_action_count=(
                float(
                    sum(int(bundle.get("counts_as_action") or 0) for bundle in bundle_rows)
                )
                if not blockers
                else 0.0
            ),
            core_op_coverage=0.0 if blockers else 1.0,
        )
        program = CanonicalRepairProgram(
            program_id=_program_id(groups[0].db_id, case_ids, ordered_keys),
            program_type=_program_type_from_ops(shared_ops),
            ops=shared_ops,
            core_ops=[
                _program_op_summary(op, "core")
                for op in shared_ops
                if not bool(_payload(_payload(op.arguments).get("shared_signature")).get("is_dependency") or False)
            ],
            accessory_ops=[
                _program_op_summary(op, "accessory")
                for op in shared_ops
                if bool(_payload(_payload(op.arguments).get("shared_signature")).get("is_dependency") or False)
            ],
            repair_effect_signature=_program_repair_effect_signature_from_ops(shared_ops),
            repair_insight_signature=_program_repair_insight_signature_from_ops(shared_ops),
            target_invariants=target_invariants,
            unresolved_variation_axes=unresolved_axes,
            program_envelope=_program_envelope_from_ops(
                ops=shared_ops,
                target_invariants=target_invariants,
                unresolved_axes=unresolved_axes,
            ),
            variables=_shared_variables(shared_ops),
            shared_invariants=sorted({item for op in shared_ops for item in op.invariants}),
            synthesized_from_case_ids=case_ids,
            compiler_supported_ops=supported,
            unsupported_ops=unsupported,
        )
        if require_effect_program:
            effect_payload = _payload(
                program.repair_effect_signature.model_dump(mode="json")
                if program.repair_effect_signature is not None
                else {}
            )
            if not effect_payload.get("effect_candidates"):
                coverage.blockers.append("shared_program_lost_effect")
                coverage.static_blockers.append("shared_program_lost_effect")
                coverage.covered_case_ids = []
                coverage.uncovered_case_ids = case_ids
                coverage.compile_coverage = 0.0
                coverage.static_program_coverage = 0.0
                coverage.member_candidate_coverage = 0.0
                coverage.core_op_coverage = 0.0
                coverage.compile_success_by_member = {
                    case_id: False for case_id in case_ids
                }
                coverage.failed_members = case_ids
                coverage.failure_reasons = {
                    case_id: "shared_program_lost_effect" for case_id in case_ids
                }
                for row in coverage.per_case.values():
                    row["covered"] = False
                    row.setdefault("blockers", []).append("shared_program_lost_effect")
        return SynthesisResult(
            program=program,
            coverage=coverage,
            synthesis_basis=synthesis_basis,
            fallback_allowed=not require_effect_program,
        )


def synthesize_shared_program(
    groups: Sequence[GroupSummary],
    *,
    require_effect_program: bool = False,
) -> SynthesisResult:
    return SharedProgramSynthesizer().synthesize(
        groups,
        require_effect_program=require_effect_program,
    )


def repair_program_steps_from_canonical_program(
    program: Optional[CanonicalRepairProgram],
) -> List[Dict[str, Any]]:
    if program is None:
        return []
    steps: List[Dict[str, Any]] = []
    for index, op in enumerate(program.ops or [], start=1):
        steps.append(
            {
                "step_id": f"canonical_shared_{index}",
                "op": op.op_type,
                "locus": op.locus,
                "is_dependency": False,
                "required": True,
                "slots": [],
                "guards": [],
                "arguments": {
                    "canonical_program_id": program.program_id,
                    "canonical_op_id": op.op_id,
                    "canonical_op_type": op.op_type,
                    "canonical_refs": [
                        _payload(ref) for ref in (op.role_refs or [])[:12]
                    ],
                    "canonical_arguments": _payload(op.arguments),
                    "canonical_invariants": list(op.invariants or []),
                },
                "source_evidence": [
                    "synthesized from canonical repair IR shared by member cases"
                ],
                "origin": "group_merged",
                "extraction_source": "group_merged",
                "supporting_case_ids": list(program.synthesized_from_case_ids),
            }
        )
        for policy_index, policy in enumerate(_accessory_policies_from_op(op), start=1):
            policy_op = str(policy.get("op") or "SELECT_ENFORCE_DISTINCT")
            steps.append(
                {
                    "step_id": f"canonical_shared_{index}_accessory_{policy_index}",
                    "op": policy_op,
                    "locus": str(policy.get("locus") or op.locus or "SELECT"),
                    "is_dependency": True,
                    "required": False,
                    "slots": [],
                    "guards": [
                        {
                            "kind": "scope_limit",
                            "description": str(guard),
                        }
                        for guard in (policy.get("guards") or [])
                        if str(guard)
                    ],
                    "arguments": {
                        "canonical_program_id": program.program_id,
                        "canonical_op_id": op.op_id,
                        "accessory_policy": str(policy.get("policy") or ""),
                        "policy_support": int(policy.get("support") or 0),
                        "policy_supporting_case_ids": [
                            str(case_id)
                            for case_id in (policy.get("supporting_case_ids") or [])
                        ],
                        "policy_payload": {
                            key: value
                            for key, value in _payload(policy).items()
                            if key
                            not in {
                                "support",
                                "supporting_case_ids",
                            }
                        },
                    },
                    "source_evidence": [
                        "inferred from member source/target SQL dependency delta"
                    ],
                    "origin": "group_merged",
                    "extraction_source": "group_merged",
                    "supporting_case_ids": [
                        str(case_id)
                        for case_id in (
                            policy.get("supporting_case_ids")
                            or program.synthesized_from_case_ids
                        )
                    ],
                }
            )
    return steps


def singleton_program_from_ir(ir: CanonicalRepairIR) -> CanonicalRepairProgram:
    ops = list(ir.program_ops or [])
    supported = sorted(
        {
            op.op_type
            for op in ops
            if _lowering_family(op.op_type, op.locus)
        }
    )
    unsupported = sorted(
        {
            op.op_type
            for op in ops
            if not _lowering_family(op.op_type, op.locus)
        }
    )
    return CanonicalRepairProgram(
        program_id=_program_id(ir.db_id, [ir.case_id], [_shared_signature_key(op) for op in ops]),
        program_type=_program_type_from_ops(ops),
        ops=ops,
        core_ops=list(ir.core_ops or []),
        accessory_ops=list(ir.accessory_ops or []),
        repair_effect_signature=ir.repair_effect_signature,
        repair_insight_signature=ir.repair_insight_signature,
        target_invariants=list(ir.target_invariants or []),
        unresolved_variation_axes=list(ir.unresolved_variation_axes or []),
        program_envelope=_program_envelope_from_ops(
            ops=ops,
            target_invariants=list(ir.target_invariants or []),
            unresolved_axes=list(ir.unresolved_variation_axes or []),
        ),
        variables={"binding_policy": "singleton exact runtime binding"},
        shared_invariants=list(ir.invariants or []),
        synthesized_from_case_ids=[str(ir.case_id)],
        compiler_supported_ops=supported,
        unsupported_ops=unsupported,
    )


def coverage_for_singleton_program(program: CanonicalRepairProgram) -> ProgramCoverage:
    case_ids = list(program.synthesized_from_case_ids or [])
    blockers = []
    if not program.ops:
        blockers.append("empty_synthesized_program")
    blockers.extend(f"unsupported_canonical_op:{op}" for op in program.unsupported_ops)
    covered = [] if blockers else case_ids
    static_coverage = (len(covered) / len(case_ids) if case_ids else 0.0)
    bundle_rows = _bundle_rows_for_ops(program.ops or [])
    return ProgramCoverage(
        total_cases=len(case_ids),
        covered_case_ids=covered,
        uncovered_case_ids=case_ids if blockers else [],
        compile_coverage=static_coverage,
        static_program_coverage=static_coverage,
        runtime_binding_coverage=0.0,
        member_candidate_coverage=static_coverage,
        blockers=blockers,
        static_blockers=blockers,
        runtime_binding_blockers={},
        per_case={
            case_id: {
                "covered": not blockers,
                "blockers": blockers,
            }
            for case_id in case_ids
        },
        compile_success_by_member={case_id: not blockers for case_id in case_ids},
        failed_members=case_ids if blockers else [],
        failure_reasons={case_id: ";".join(blockers) for case_id in case_ids if blockers},
        mean_action_count=(
            float(sum(int(bundle.get("counts_as_action") or 0) for bundle in bundle_rows))
            if not blockers
            else 0.0
        ),
        core_op_coverage=0.0 if blockers else 1.0,
    )


__all__ = [
    "COMPILER_SUPPORTED_CANONICAL_OPS",
    "SharedProgramSynthesizer",
    "SynthesisResult",
    "canonical_op_lowering_family",
    "coverage_for_singleton_program",
    "repair_program_steps_from_canonical_program",
    "singleton_program_from_ir",
    "synthesize_shared_program",
]
