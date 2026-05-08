"""Offline strict-pattern formation for EEA v2 singleton libraries.

The current runtime-learning path intentionally keeps only singleton and formal
pattern memories.  Legacy "experience family" clustering is no longer a
promotion or trigger source; broad pair evidence is retained only as audit and
candidate retrieval context.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from method.EEA.rulebook.common.core.data_structures import (
    BiasRecognitionContract,
    CoreInterface,
    GroupFormationEvidence,
    GroupLifecycle,
    GroupMemberEvidence,
    GroupSummary,
    InstantiationSlot,
    InstantiationProgram,
    LibraryStateV2,
    ModelProfile,
    RepairSkeleton,
    RepairSkeletonSemantic,
    RepairSkeletonStructural,
    TriggerSignature,
    TriggerContract,
)
from method.EEA.rulebook.common.analysis.signal_summary import (
    build_trigger_contract,
    compact_synthesized_program_for_memory,
)
from method.EEA.rulebook.common.learning.shared_program_synthesizer import (
    canonical_op_lowering_family,
    repair_program_steps_from_canonical_program,
    synthesize_shared_program,
)
from method.EEA.rulebook.common.runtime.trigger_contract import (
    ensure_materialized_trigger_contract,
    materialize_library_runtime_contracts,
)
from method.EEA.rulebook.common.core.vocabulary import (
    BIAS_RECOGNITION_SIGNAL_VOCABULARY,
    Confidence,
    GrainType,
    GroupStatus,
    GroupType,
    Locus,
    LOCAL_REORGANIZE_NEIGHBOR_MAX,
    OpFamily,
    OpType,
    OutputContract,
    TargetFamily,
)


_PAIR_SCORE_CACHE: Dict[str, "PairScore"] = {}
ONLINE_EVOLUTION_MAX_CANDIDATES_PER_FOCUS = 48
PATTERN_ADMISSION_PROMPT_BUDGET_CHARS = 24000
PATTERN_ADMISSION_MAX_GROUP_CARDS = 4
PATTERN_ADMISSION_PAIR_PER_RELATION_LIMIT = 2
PATTERN_ADMISSION_COMPACT_PAIR_PER_RELATION_LIMIT = 1
PATTERN_ADMISSION_MAX_REPRESENTATIVE_PAIRS = 40


def _model_dump(obj: Any) -> Dict[str, Any]:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return dict(obj)
    return dict(getattr(obj, "__dict__", {}) or {})


def _as_set(values: Iterable[Any]) -> Set[str]:
    return {str(value).strip() for value in values if str(value).strip()}


def _signal_payload(group: GroupSummary) -> Dict[str, Any]:
    payload = getattr(group, "formation_signals", None) or {}
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    return dict(payload) if isinstance(payload, dict) else {}


def _has_signal_payload(group: GroupSummary) -> bool:
    payload = _signal_payload(group)
    return bool(payload.get("delta") or payload.get("pred_current"))


def _has_required_pattern_signals(group: GroupSummary) -> bool:
    """Return whether a singleton carries the required repair-trace evidence.

    Missing Phase-A/repair-effect evidence must not fall back to legacy overlap
    clustering.  The safe behavior is to keep the object as a singleton and
    report ``signal_missing`` in pair audit.
    """
    payload = _signal_payload(group)
    ir = dict((payload.get("canonical_repair_ir") or {}) or {})
    if not ir:
        return False
    effect_signature = _model_dump(ir.get("repair_effect_signature") or {})
    effect_candidates = [
        _model_dump(item)
        for item in (effect_signature.get("effect_candidates") or [])
        if _model_dump(item).get("axis")
    ]
    return bool(payload.get("repair_insight_signature")) and bool(effect_candidates)


def _signal_delta(group: GroupSummary) -> Dict[str, Any]:
    return dict((_signal_payload(group).get("delta") or {}) or {})


def _signal_pred_current(group: GroupSummary) -> Dict[str, Any]:
    return dict((_signal_payload(group).get("pred_current") or {}) or {})


def _signal_axes(group: GroupSummary) -> Set[str]:
    return _as_set(_signal_delta(group).get("delta_axes") or [])


def _repair_insight_interface_key(group: GroupSummary) -> str:
    insight = dict((_signal_payload(group).get("repair_insight_signature") or {}) or {})
    for key in ("interface_key", "repair_interface", "target_preference"):
        value = str(insight.get(key) or "").strip()
        if value:
            return value
    return ""


def _group_repair_program(group: GroupSummary) -> List[Dict[str, Any]]:
    contract = _model_dump(getattr(group, "trigger_contract", None))
    action_contract = _model_dump(contract.get("action_contract") or {})
    steps = action_contract.get("repair_program") or []
    out: List[Dict[str, Any]] = []
    for step in steps or []:
        payload = _model_dump(step)
        if payload:
            out.append(payload)
    return out


def _canonical_payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _short_text(value: Any, *, limit: int = 320) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _repair_step_key(step: Dict[str, Any]) -> Tuple[str, str, str, bool, bool, str, str, str]:
    return (
        str(step.get("step_id") or ""),
        str(step.get("op") or ""),
        str(step.get("locus") or ""),
        bool(step.get("is_dependency") or False),
        bool(step.get("required", True)),
        _canonical_payload(step.get("slots") or []),
        _canonical_payload(step.get("guards") or []),
        _canonical_payload(step.get("arguments") or {}),
    )


def _merge_repair_programs(groups: Sequence[GroupSummary]) -> List[Dict[str, Any]]:
    """Keep only repair steps supported by every member.

    This prevents a dependency observed in one singleton from becoming a family
    or pattern rule unless all merged members contain the same extracted step.
    """
    if not groups:
        return []
    programs = [_group_repair_program(group) for group in groups]
    if not all(programs):
        return []
    common_keys = set(_repair_step_key(step) for step in programs[0])
    for program in programs[1:]:
        common_keys &= {_repair_step_key(step) for step in program}
    merged: List[Dict[str, Any]] = []
    case_ids = [case_id for group in groups for case_id in group.case_ids]
    for key in sorted(common_keys):
        representative_step = next(
            step for step in programs[0] if _repair_step_key(step) == key
        )
        payload = dict(representative_step)
        payload["origin"] = "group_merged"
        payload["extraction_source"] = "group_merged"
        payload["supporting_case_ids"] = sorted({str(case_id) for case_id in case_ids})
        evidence = list(payload.get("source_evidence") or [])
        evidence.append("merged only because every member exposed this repair step")
        payload["source_evidence"] = evidence
        merged.append(payload)
    return merged


def _shape_delta(group: GroupSummary) -> Dict[str, Any]:
    shape = dict((_signal_delta(group).get("output_shape_delta") or {}) or {})
    if not shape:
        structural = group.core_interface.repair_skeleton_prototype.structural
        payload = getattr(structural, "output_shape_delta", None)
        if payload is not None:
            shape = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else dict(payload)
    return _enrich_shape_delta(shape)


def _enrich_shape_delta(shape: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(shape or {})
    current = out.get("current_arity")
    target = out.get("target_arity")
    if current is not None and target is not None:
        delta = int(target) - int(current)
        out["arity_delta"] = delta
        out["arity_direction"] = "increase" if delta > 0 else "decrease" if delta < 0 else "same"
    return out


def _shape_key(group: GroupSummary) -> Tuple[str, str, str, str]:
    shape = _shape_delta(group)
    return (
        str(shape.get("operation")),
        str(shape.get("arity_direction")),
        str(shape.get("target_grain")),
        str(shape.get("current_grain")),
    )


def _shape_payload_compatibility(left_shape: Dict[str, Any], right_shape: Dict[str, Any]) -> float:
    left_shape = _enrich_shape_delta(left_shape)
    right_shape = _enrich_shape_delta(right_shape)
    if not left_shape or not right_shape:
        return 0.0
    parts = []
    for key in ("operation", "arity_direction", "current_grain", "target_grain"):
        left_value = left_shape.get(key)
        right_value = right_shape.get(key)
        if left_value is None or right_value is None:
            continue
        parts.append(1.0 if str(left_value) == str(right_value) else 0.0)
    left_roles = _as_set(left_shape.get("current_roles") or [])
    right_roles = _as_set(right_shape.get("current_roles") or [])
    if left_roles or right_roles:
        parts.append(jaccard(left_roles, right_roles))
    left_target_roles = _as_set(left_shape.get("target_roles") or [])
    right_target_roles = _as_set(right_shape.get("target_roles") or [])
    if left_target_roles or right_target_roles:
        parts.append(jaccard(left_target_roles, right_target_roles))
    return sum(parts) / len(parts) if parts else 0.0


def _shape_compatibility(left: GroupSummary, right: GroupSummary) -> float:
    left_shape = _shape_delta(left)
    right_shape = _shape_delta(right)
    if not left_shape or not right_shape:
        return 0.0
    return _shape_payload_compatibility(left_shape, right_shape)


def _legacy_family(group: GroupSummary) -> str:
    delta = _signal_delta(group)
    value = delta.get("legacy_primary_edit_family")
    if value:
        return str(value)
    signature = str(delta.get("legacy_signature") or "")
    if signature:
        return signature.split("|", 1)[0]
    structural = group.core_interface.repair_skeleton_prototype.structural
    return str(structural.legacy_signature or "").split("|", 1)[0]


def _legacy_compatibility(left: GroupSummary, right: GroupSummary) -> float:
    left_family = _legacy_family(left)
    right_family = _legacy_family(right)
    if not left_family or not right_family:
        return 0.0
    return 1.0 if left_family == right_family else 0.0


def _slot_kind_keys(group: GroupSummary) -> Set[str]:
    keys: Set[str] = set()
    for slot in group.instantiation_program.slots or []:
        required = "required" if slot.required else "optional"
        roles = ",".join(sorted(_as_set(slot.allowed_role_families)))
        keys.add(f"{slot.kind}:{roles}:{required}")
    return keys


def _canonical_lowering_families(group: GroupSummary) -> Set[str]:
    signals = _signal_payload(group)
    ir = dict((signals.get("canonical_repair_ir") or {}) or {})
    families: Set[str] = set()
    for op in ir.get("program_ops") or []:
        payload = _model_dump(op)
        family = canonical_op_lowering_family(
            payload.get("op_type"),
            payload.get("locus"),
        )
        if family:
            families.add(str(family))
    return families


def _shape_retrieval_key(group: GroupSummary) -> Tuple[str, ...]:
    shape = _enrich_shape_delta(_shape_delta(group))
    return (
        str(shape.get("operation") or ""),
        str(shape.get("arity_direction") or ""),
        str(shape.get("current_grain") or ""),
        str(shape.get("target_grain") or ""),
        f"subset={bool(shape.get('target_is_subset_of_source'))}",
    )


def _primary_effect_rows(group: GroupSummary) -> Tuple[Tuple[str, ...], ...]:
    signals = _signal_payload(group)
    ir = dict((signals.get("canonical_repair_ir") or {}) or {})
    return _primary_effect_core_signature(ir)


def _evolution_card(group: GroupSummary) -> Dict[str, Any]:
    """Small immutable card used for cache keys and candidate indexing.

    It deliberately excludes full SQL traces, role graphs, trigger contracts,
    and canonical op arguments.  Those heavy objects are only loaded after a
    pair passes broad retrieval and needs semantic/program review.
    """
    return {
        "group_id": group.group_id,
        "version": int(group.version or 0),
        "case_ids": [str(case_id) for case_id in group.case_ids or []],
        "db_id": group.db_id,
        "effect_core": [list(item) for item in _primary_effect_rows(group)],
        "delta_axes": sorted(_signal_axes(group)),
        "shape_key": list(_shape_retrieval_key(group)),
        "lowering_families": sorted(_canonical_lowering_families(group)),
        "repair_insight_interface": _repair_insight_interface_key(group),
    }


def _retrieval_keys_for_card(card: Dict[str, Any]) -> Set[Tuple[str, str]]:
    keys: Set[Tuple[str, str]] = set()
    db_id = str(card.get("db_id") or "")
    for effect in card.get("effect_core") or []:
        effect_key = _canonical_payload(effect)
        if effect_key:
            keys.add((db_id, f"effect:{effect_key}"))

    axes = [str(axis) for axis in card.get("delta_axes") or [] if str(axis)]
    shape_key = tuple(str(item) for item in card.get("shape_key") or [])
    shape_family = "|".join(shape_key[:5])
    for axis in axes:
        if shape_family:
            keys.add((db_id, f"axis_shape:{axis}|{shape_family}"))
        for family in card.get("lowering_families") or []:
            keys.add((db_id, f"axis_lowering:{axis}|{family}"))

    interface = str(card.get("repair_insight_interface") or "").strip()
    if interface:
        keys.add((db_id, f"insight:{interface}"))
    return keys


def _retrieval_key_label(key: Tuple[str, str]) -> str:
    return f"{key[0]}::{key[1]}"


def _retrieval_key_reason(key: Tuple[str, str]) -> str:
    payload = str(key[1] if len(key) > 1 else "")
    return payload.split(":", 1)[0] if ":" in payload else payload


def _build_retrieval_index(
    groups: Sequence[GroupSummary],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[Tuple[str, str], Set[str]]]:
    cards_by_id = {group.group_id: _evolution_card(group) for group in groups}
    buckets: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    for group_id, card in cards_by_id.items():
        for key in _retrieval_keys_for_card(card):
            buckets[key].add(group_id)
    return cards_by_id, buckets


def _candidate_pair_keys(
    groups: Sequence[GroupSummary],
    *,
    focus_case_ids: Optional[Set[str]] = None,
) -> List[Tuple[str, str]]:
    """Return high-recall candidate pairs without O(n^2) full enumeration.

    Online evolution only needs pairs involving the newly accumulated case(s);
    final offline evolution passes no focus ids and gets all indexed candidate
    pairs.  The index uses case-derived abstract signals, not fixed DB/table
    vocabularies.
    """
    cards_by_id, buckets = _build_retrieval_index(groups)

    focus_ids = {str(case_id) for case_id in (focus_case_ids or set()) if str(case_id)}
    if focus_ids:
        focus_group_ids = {
            group.group_id
            for group in groups
            if {str(case_id) for case_id in group.case_ids or []} & focus_ids
        }
    else:
        focus_group_ids = {group.group_id for group in groups}

    candidates: Set[Tuple[str, str]] = set()
    if focus_ids:
        for group_id in sorted(focus_group_ids):
            peer_scores: Counter[str] = Counter()
            for key in _retrieval_keys_for_card(cards_by_id.get(group_id, {})):
                for peer_id in buckets.get(key, set()):
                    if peer_id != group_id:
                        peer_scores[peer_id] += 1
            for peer_id, _count in peer_scores.most_common(
                ONLINE_EVOLUTION_MAX_CANDIDATES_PER_FOCUS
            ):
                candidates.add(tuple(sorted((group_id, peer_id))))
    else:
        for members in buckets.values():
            ordered = sorted(members)
            for index, left_id in enumerate(ordered):
                for right_id in ordered[index + 1 :]:
                    candidates.add((left_id, right_id))

    return sorted(candidates)


def _retrieval_reasons_for_pair(
    left_id: str,
    right_id: str,
    *,
    cards_by_id: Dict[str, Dict[str, Any]],
) -> List[str]:
    left_keys = _retrieval_keys_for_card(cards_by_id.get(left_id, {}))
    right_keys = _retrieval_keys_for_card(cards_by_id.get(right_id, {}))
    return sorted({_retrieval_key_reason(key) for key in (left_keys & right_keys) if key})


def _retrieval_audit(
    groups: Sequence[GroupSummary],
    *,
    focus_case_ids: Optional[Set[str]],
    candidate_keys: Sequence[Tuple[str, str]],
    pair_scores: Dict[Tuple[str, str], PairScore],
) -> Dict[str, Any]:
    cards_by_id, buckets = _build_retrieval_index(groups)
    focus_ids = {str(case_id) for case_id in (focus_case_ids or set()) if str(case_id)}
    focus_group_ids = {
        group.group_id
        for group in groups
        if {str(case_id) for case_id in group.case_ids or []} & focus_ids
    } if focus_ids else set(cards_by_id)
    bucket_hits_by_focus: Dict[str, List[Dict[str, Any]]] = {}
    retrieval_keys_by_focus: Dict[str, List[str]] = {}
    for group_id in sorted(focus_group_ids):
        keys = sorted(_retrieval_keys_for_card(cards_by_id.get(group_id, {})))
        retrieval_keys_by_focus[group_id] = [_retrieval_key_label(key) for key in keys]
        rows: List[Dict[str, Any]] = []
        for key in keys:
            peers = sorted(peer_id for peer_id in buckets.get(key, set()) if peer_id != group_id)
            if peers:
                rows.append(
                    {
                        "key": _retrieval_key_label(key),
                        "reason": _retrieval_key_reason(key),
                        "peer_group_ids": peers[:80],
                        "peer_count": len(peers),
                    }
                )
        bucket_hits_by_focus[group_id] = rows

    retrieved_pairs: List[Dict[str, Any]] = []
    for left_id, right_id in candidate_keys:
        pair = pair_scores.get(tuple(sorted((left_id, right_id))))
        left_card = cards_by_id.get(left_id, {})
        right_card = cards_by_id.get(right_id, {})
        row = {
            "left_group_id": left_id,
            "right_group_id": right_id,
            "left_case_ids": list(left_card.get("case_ids") or []),
            "right_case_ids": list(right_card.get("case_ids") or []),
            "retrieval_reasons": _retrieval_reasons_for_pair(
                left_id,
                right_id,
                cards_by_id=cards_by_id,
            ),
            "score_status": "scored" if pair is not None else "not_scored",
        }
        if pair is not None:
            row.update(
                {
                    "accepted": bool(pair.accepted),
                    "semantic_relation": pair.semantic_relation,
                    "score": pair.score,
                    "program_compatible": bool(pair.program_compatible),
                    "program_blockers": list(pair.program_blockers or [])[:12],
                    "failure_taxonomy": list(pair.failure_taxonomy or [])[:12],
                    "broad_retrieval_reasons": list(pair.broad_retrieval_reasons or [])[:12],
                }
            )
        retrieved_pairs.append(row)

    unrecalled_focus_summary = []
    retrieved_focus_ids = {
        group_id
        for left_id, right_id in candidate_keys
        for group_id in (left_id, right_id)
        if group_id in focus_group_ids
    }
    for group_id in sorted(focus_group_ids - retrieved_focus_ids):
        unrecalled_focus_summary.append(
            {
                "focus_group_id": group_id,
                "case_ids": list(cards_by_id.get(group_id, {}).get("case_ids") or []),
                "retrieved_peer_count": 0,
                "reason": "no_shared_retrieval_key",
            }
        )

    return {
        "schema_version": "formation-retrieval-audit-v1",
        "retrieval_mode": "focus_case_ids" if focus_ids else "all_indexed_candidates",
        "focus_case_ids": sorted(focus_ids),
        "focus_group_ids": sorted(focus_group_ids),
        "active_singleton_count": len(groups),
        "candidate_pair_count": len(candidate_keys),
        "scored_pair_count": len(pair_scores),
        "focus_cards": [cards_by_id[group_id] for group_id in sorted(focus_group_ids)],
        "retrieval_keys_by_focus_group": retrieval_keys_by_focus,
        "bucket_hits_by_focus_group": bucket_hits_by_focus,
        "retrieved_pairs": retrieved_pairs[:240],
        "retrieved_pair_count": len(retrieved_pairs),
        "unrecalled_focus_summary": unrecalled_focus_summary,
    }


def _shape_broad_overlap(left_shape: Dict[str, Any], right_shape: Dict[str, Any]) -> bool:
    left_shape = _enrich_shape_delta(left_shape)
    right_shape = _enrich_shape_delta(right_shape)
    if not left_shape or not right_shape:
        return False
    for key in ("operation", "arity_direction", "current_grain", "target_grain"):
        left_value = str(left_shape.get(key) or "").strip()
        right_value = str(right_shape.get(key) or "").strip()
        if left_value and right_value and left_value == right_value:
            return True
    for key in ("current_roles", "target_roles"):
        if _as_set(left_shape.get(key) or []) & _as_set(right_shape.get(key) or []):
            return True
    return False


def _broad_retrieval_reasons(
    left: GroupSummary,
    right: GroupSummary,
    *,
    signal_axes_overlap: float,
    shape_compat: float,
    legacy_compat: float,
    slot_overlap: float,
    question_overlap: float,
    manifest_overlap: float,
    structural_compat: float,
) -> Tuple[str, ...]:
    """High-recall pair retrieval signals from current repair traces only.

    These signals only decide whether a pair deserves semantic/program review.
    They must not be interpreted as evidence that the pair can be merged.
    Legacy question/manifest/structural/slot overlap intentionally does not
    participate in retrieval; missing current signals keep the objects as
    singletons instead of promoting via fallback similarity.
    """
    reasons: Set[str] = set()
    if _signal_axes(left) & _signal_axes(right):
        reasons.add("shared_effect_axis")
    if _shape_broad_overlap(_shape_delta(left), _shape_delta(right)):
        reasons.add("shared_output_shape_delta")
    if _canonical_lowering_families(left) & _canonical_lowering_families(right):
        reasons.add("shared_action_lowering_family")
    if _slot_kind_keys(left) & _slot_kind_keys(right):
        reasons.add("shared_slot_kind")
    left_interface = _repair_insight_interface_key(left)
    right_interface = _repair_insight_interface_key(right)
    if left_interface and left_interface == right_interface:
        reasons.add("shared_repair_insight_interface")
    # Preserve audit values in the function signature to make the retrieval
    # boundary explicit even when individual dimensions are only used for logs.
    _ = (
        signal_axes_overlap,
        shape_compat,
        legacy_compat,
        question_overlap,
        manifest_overlap,
        structural_compat,
        slot_overlap,
    )
    return tuple(sorted(reasons))


def jaccard(left: Iterable[Any], right: Iterable[Any]) -> float:
    left_set = _as_set(left)
    right_set = _as_set(right)
    if not left_set and not right_set:
        return 1.0
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def _slot_keys(group: GroupSummary) -> Set[str]:
    keys: Set[str] = set()
    for slot in group.instantiation_program.slots or []:
        role_part = ",".join(sorted(_as_set(slot.allowed_role_families)))
        keys.add(f"{slot.name}:{slot.kind}:{role_part}:{slot.required}")
    return keys


def _question_tags(group: GroupSummary) -> Set[str]:
    return _as_set(group.core_interface.question_family_tags) | _as_set(
        group.trigger_signature.required_question_tags
    )


_RUNTIME_REQUIRED_QUESTION_TAGS = {
    *(item.value for item in OpType),
    *(item.value for item in GrainType),
}


def _runtime_required_question_tags(tags: Iterable[Any]) -> List[str]:
    return sorted(
        {
            str(tag)
            for tag in tags
            if str(tag) and str(tag) in _RUNTIME_REQUIRED_QUESTION_TAGS
        }
    )


def _manifest_tags(group: GroupSummary) -> Set[str]:
    return _as_set(group.core_interface.pred_family_tags) | _as_set(
        group.trigger_signature.required_pred_tags
    ) | _as_set(group.trigger_signature.decisive_antipatterns)


def output_contract_compatible(left: OutputContract, right: OutputContract) -> float:
    """Legacy fallback only; shape delta compatibility is the primary path."""
    if left == right:
        return 1.0
    if left == OutputContract.UNCHANGED:
        return 0.5
    if right == OutputContract.UNCHANGED:
        return 0.5
    return 0.0


def structural_compatibility(left: RepairSkeleton, right: RepairSkeleton) -> float:
    left_s = left.structural
    right_s = right.structural
    left_shape = (
        left_s.output_shape_delta.model_dump(mode="json")
        if left_s.output_shape_delta is not None
        else {}
    )
    right_shape = (
        right_s.output_shape_delta.model_dump(mode="json")
        if right_s.output_shape_delta is not None
        else {}
    )
    shape_score = _shape_payload_compatibility(left_shape, right_shape) if left_shape or right_shape else 0.5
    parts = [
        1.0 if left_s.locus == right_s.locus else 0.0,
        1.0 if left_s.op_family == right_s.op_family else 0.0,
        1.0 if left_s.target_family == right_s.target_family else 0.0,
        shape_score,
    ]
    return sum(parts) / len(parts)


def _hard_conflict(left: GroupSummary, right: GroupSummary) -> Optional[str]:
    if left.db_id != right.db_id:
        return "different_db_id"
    if left.status != GroupStatus.ACTIVE or right.status != GroupStatus.ACTIVE:
        return "inactive_group"

    if not _output_grain_compatible(left, right):
        return "output_grain_conflict"

    left_negative = _as_set(left.trigger_signature.negative_evidence)
    right_negative = _as_set(right.trigger_signature.negative_evidence)
    if left_negative & (_question_tags(right) | _manifest_tags(right)):
        return "left_negative_evidence_hits_right"
    if right_negative & (_question_tags(left) | _manifest_tags(left)):
        return "right_negative_evidence_hits_left"

    return None


_ABSOLUTE_HARD_CONFLICTS = {
    "different_db_id",
    "inactive_group",
    "left_negative_evidence_hits_right",
    "right_negative_evidence_hits_left",
}

_DIRECT_MERGE_ONLY_VETOES = {
    "output_grain_conflict",
}


def _is_absolute_conflict(veto_reason: Optional[str]) -> bool:
    return bool(veto_reason and veto_reason in _ABSOLUTE_HARD_CONFLICTS)


def _is_direct_merge_only_veto(veto_reason: Optional[str]) -> bool:
    return bool(veto_reason and veto_reason in _DIRECT_MERGE_ONLY_VETOES)


def _blockers_contain(blockers: Sequence[Any], marker: str) -> bool:
    return marker in ";".join(str(item) for item in blockers or [])


def _pair_semantic_relation(
    *,
    accepted: bool,
    veto_reason: Optional[str],
    broad_retrieval_reasons: Sequence[str],
    program_blockers: Sequence[Any],
) -> str:
    if accepted:
        return "compatible"
    if _is_absolute_conflict(veto_reason):
        return "hard_conflict"
    if _is_direct_merge_only_veto(veto_reason) and broad_retrieval_reasons:
        return "direct_merge_veto"
    if _blockers_contain(program_blockers, "insight_judge_partial"):
        return "partial"
    if _blockers_contain(program_blockers, "insight_judge_conflict"):
        return "conflict"
    if _blockers_contain(program_blockers, "core_program_signature_conflict"):
        return "core_program_signature_conflict"
    if broad_retrieval_reasons:
        return "semantic_review_rejected"
    return "not_candidate"


@dataclass(frozen=True)
class PairScore:
    left_group_id: str
    right_group_id: str
    left_case_ids: Tuple[str, ...]
    right_case_ids: Tuple[str, ...]
    question_overlap: float
    manifest_overlap: float
    structural_compat: float
    slot_overlap: float
    score: float
    accepted: bool
    veto_reason: Optional[str] = None
    signal_axes_overlap: float = 0.0
    shape_compat: float = 0.0
    legacy_compat: float = 0.0
    program_compatible: bool = False
    program_blockers: Tuple[str, ...] = ()
    program_spans_output_grain: bool = False
    program_has_substantive_target_contract: bool = False
    effect_signature_count: int = 0
    shared_program_basis: str = ""
    failure_taxonomy: Tuple[str, ...] = ()
    broad_retrieval_reasons: Tuple[str, ...] = ()
    semantic_relation: str = ""
    branchable_for_pattern: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "left_group_id": self.left_group_id,
            "right_group_id": self.right_group_id,
            "left_case_ids": list(self.left_case_ids),
            "right_case_ids": list(self.right_case_ids),
            "question_overlap": self.question_overlap,
            "manifest_overlap": self.manifest_overlap,
            "structural_compat": self.structural_compat,
            "slot_overlap": self.slot_overlap,
            "score": self.score,
            "accepted": self.accepted,
            "veto_reason": self.veto_reason,
            "score_only_candidate": bool(self.score > 0.0 and not self.accepted),
            "signal_axes_overlap": self.signal_axes_overlap,
            "shape_compat": self.shape_compat,
            "legacy_compat": self.legacy_compat,
            "broad_retrieval_reasons": list(self.broad_retrieval_reasons),
            "program_compatible": self.program_compatible,
            "program_blockers": list(self.program_blockers),
            "program_spans_output_grain": self.program_spans_output_grain,
            "program_has_substantive_target_contract": self.program_has_substantive_target_contract,
            "effect_signature_count": self.effect_signature_count,
            "shared_program_basis": self.shared_program_basis,
            "failure_taxonomy": list(self.failure_taxonomy),
            "semantic_relation": self.semantic_relation,
            "branchable_for_pattern": self.branchable_for_pattern,
        }


def _program_effect_candidates(program: Any) -> List[Dict[str, Any]]:
    if program is None:
        return []
    signature = getattr(program, "repair_effect_signature", None)
    if signature is None:
        return []
    payload = _model_dump(signature)
    return [
        _model_dump(item)
        for item in (payload.get("effect_candidates") or [])
        if _model_dump(item).get("axis")
    ]


def _operation_signature(payload: Dict[str, Any]) -> Dict[str, Any]:
    args = _model_dump(payload.get("arguments") or {})
    return _model_dump(args.get("operation_signature") or args.get("shared_signature") or {})


def _primary_effect_core_signature(ir: Dict[str, Any]) -> Tuple[Tuple[str, ...], ...]:
    """Pattern identity from primary contrastive effects, not accessory edits.

    The same stable bias can have different branch/accessory policies
    (deduplicate, ranking, target-only predicate, route cleanup). Those policies
    are still available to the compiler, but they must not split the root
    pattern identity when the primary source->target effect is the same.
    """
    repair_effect_signature = _model_dump(ir.get("repair_effect_signature") or {})
    rows: Set[Tuple[str, ...]] = set()
    for effect in repair_effect_signature.get("effect_candidates") or []:
        payload = _model_dump(effect)
        role = str(payload.get("role") or "primary").strip().lower()
        if role not in {"primary", "core"}:
            continue
        axis = str(payload.get("axis") or "").strip()
        if not axis:
            continue
        delta = _model_dump(payload.get("delta") or {})
        kind = str(delta.get("kind") or "").strip()
        if axis == "output_shape_delta":
            rows.add(
                (
                    "effect",
                    axis,
                    kind,
                    str(delta.get("arity_direction") or ""),
                    f"{delta.get('source_arity')}->{delta.get('target_arity')}",
                    f"subset={bool(delta.get('target_is_subset_of_source'))}",
                )
            )
            continue
        if axis in {"aggregation_unit_delta", "grain_delta"}:
            rows.add(
                (
                    "effect",
                    axis,
                    kind,
                    f"aggregate={delta.get('source_has_aggregate')}->{delta.get('target_has_aggregate')}",
                    f"distinct={delta.get('source_has_distinct')}->{delta.get('target_has_distinct')}",
                    str(delta.get("arity_direction") or ""),
                )
            )
            continue
        rows.add(
            (
                "effect",
                axis,
                kind,
                str(delta.get("arity_direction") or ""),
                str(delta.get("direction") or ""),
            )
        )
    return tuple(sorted(rows))


def _program_core_signature(group: GroupSummary) -> Tuple[Tuple[str, ...], ...]:
    """Core executable repair package learned from this case.

    This is a mechanism-level guard: broad semantic similarity is not enough if
    members require different numbers or kinds of primary executable edits.
    Concrete table/column names are intentionally excluded.
    """
    signals = _signal_payload(group)
    ir = dict((signals.get("canonical_repair_ir") or {}) or {})
    effect_signature = _primary_effect_core_signature(ir)
    if effect_signature:
        return effect_signature
    counts: Counter[Tuple[str, str, str]] = Counter()
    for op in ir.get("program_ops") or []:
        payload = _model_dump(op)
        args = _model_dump(payload.get("arguments") or {})
        identity_role = str(
            args.get("identity_role") or payload.get("identity_role") or "core"
        ).strip().lower()
        if identity_role in {"accessory", "dependency", "branch", "noise"}:
            continue
        signature = _operation_signature(payload)
        is_dependency = bool(signature.get("is_dependency") or payload.get("is_dependency") or False)
        required = bool(signature.get("required", payload.get("required", True)))
        if is_dependency or not required:
            continue
        role_delta = _model_dump(signature.get("role_delta") or {})
        arity_direction = str(role_delta.get("arity_direction") or "")
        counts[
            (
                str(payload.get("op_type") or ""),
                str(payload.get("locus") or ""),
                arity_direction,
            )
        ] += 1
    if not counts:
        for op in ir.get("core_ops") or []:
            payload = _model_dump(op)
            counts[
                (
                    str(payload.get("op_type") or ""),
                    str(payload.get("locus") or ""),
                    "",
                )
            ] += 1
    return tuple(
        sorted(
            (f"{op_type}x{count}", locus, arity_direction)
            for (op_type, locus, arity_direction), count in counts.items()
            if op_type
        )
    )


def _program_dependency_signature(group: GroupSummary, *, required_only: bool) -> Tuple[Tuple[str, str], ...]:
    signals = _signal_payload(group)
    ir = dict((signals.get("canonical_repair_ir") or {}) or {})
    counts: Counter[Tuple[str, str]] = Counter()
    for op in ir.get("program_ops") or []:
        payload = _model_dump(op)
        signature = _operation_signature(payload)
        is_dependency = bool(signature.get("is_dependency") or payload.get("is_dependency") or False)
        if not is_dependency:
            continue
        required = bool(signature.get("required", payload.get("required", True)))
        if required_only and not required:
            continue
        if not required_only and required:
            continue
        key = (str(payload.get("op_type") or ""), str(payload.get("locus") or ""))
        if key[0]:
            counts[key] += 1
    return tuple(
        sorted(
            (f"{op_type}x{count}", locus)
            for (op_type, locus), count in counts.items()
        )
    )


def _core_signature_buckets(
    groups: Sequence[GroupSummary],
) -> Dict[Tuple[Tuple[str, ...], ...], List[GroupSummary]]:
    buckets: Dict[Tuple[Tuple[str, ...], ...], List[GroupSummary]] = defaultdict(list)
    for group in groups:
        buckets[_program_core_signature(group)].append(group)
    return buckets


def _taxonomy_from_blockers(
    blockers: Sequence[Any],
    *,
    veto_reason: Optional[str] = None,
    program_compatible: bool = False,
    effect_signature_count: int = 0,
) -> Tuple[str, ...]:
    labels: Set[str] = set()
    if veto_reason:
        labels.add("hard_conflict")
    blocker_text = ";".join(str(item) for item in blockers or [])
    if "effect_missing" in blocker_text:
        labels.add("effect_missing")
    if (
        "missing_shared_effect_program" in blocker_text
        or "effect_too_specific" in blocker_text
        or "no_shared_effect" in blocker_text
    ):
        labels.add("effect_too_specific")
    if "shared_program_lost_effect" in blocker_text:
        labels.add("shared_program_lost_effect")
        labels.add("legacy_fallback_leak")
    if "insight_" in blocker_text or "case_local_insight" in blocker_text:
        labels.add("case_local_insight_conflict")
    if "core_program_signature_conflict" in blocker_text:
        labels.add("core_program_signature_conflict")
    if not program_compatible and not labels:
        labels.add("compat_too_strict")
    if program_compatible and effect_signature_count <= 0:
        labels.add("shared_program_lost_effect")
    return tuple(sorted(labels))


def _shared_program_pair_compatibility(
    left: GroupSummary,
    right: GroupSummary,
) -> Tuple[bool, Tuple[str, ...], bool, bool, int, str]:
    result = synthesize_shared_program([left, right], require_effect_program=True)
    coverage = result.coverage
    blockers = tuple(str(item) for item in (coverage.blockers or []) if str(item))
    effect_signature_count = len(_program_effect_candidates(result.program))
    if result.program is not None and effect_signature_count <= 0:
        blockers = (*blockers, "shared_program_lost_effect")
    compatible = bool(
        result.program is not None
        and float(coverage.compile_coverage or 0.0) >= 1.0
        and float(coverage.mean_action_count or 0.0) <= 3.0
        and not blockers
        and effect_signature_count > 0
    )
    spans_output_grain = bool(
        result.program is not None
        and any(str(op.locus or "").upper() != "SELECT" for op in result.program.ops or [])
    )
    has_substantive_target_contract = (
        _has_substantive_target_contract(result.program.target_invariants)
        if result.program is not None
        else False
    )
    return (
        compatible,
        blockers,
        spans_output_grain,
        has_substantive_target_contract,
        effect_signature_count,
        result.synthesis_basis,
    )


def _output_grain_compatible(left: GroupSummary, right: GroupSummary) -> bool:
    left_shape = _shape_delta(left)
    right_shape = _shape_delta(right)
    for key in ("current_grain", "target_grain"):
        left_grain = str(left_shape.get(key) or "")
        right_grain = str(right_shape.get(key) or "")
        if left_grain and right_grain and left_grain != right_grain:
            return False
    return True


def _current_grain_compatible(left: GroupSummary, right: GroupSummary) -> bool:
    left_grain = str(_shape_delta(left).get("current_grain") or "")
    right_grain = str(_shape_delta(right).get("current_grain") or "")
    return not left_grain or not right_grain or left_grain == right_grain


def _has_substantive_target_contract(invariants: Sequence[Any]) -> bool:
    values = {str(item) for item in invariants or [] if str(item)}
    if any(item.startswith("target_relation_equality=") for item in values):
        return True
    if "target_output_subset_of_source_outputs" in values:
        return True
    if any(item.startswith("target_output_arity=") for item in values) and any(
        item.startswith("target_output_roles=") for item in values
    ):
        return True
    return False


def _pair_score_cache_key(left: GroupSummary, right: GroupSummary) -> str:
    payload = {
        "left": _evolution_card(left),
        "right": _evolution_card(right),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def score_pair(left: GroupSummary, right: GroupSummary) -> PairScore:
    cache_key = _pair_score_cache_key(left, right)
    cached = _PAIR_SCORE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    veto = _hard_conflict(left, right)
    program_compatible = False
    program_blockers: Tuple[str, ...] = (veto,) if veto is not None else ()
    program_spans_output_grain = False
    program_has_substantive_target_contract = False
    effect_signature_count = 0
    shared_program_basis = ""
    signal_mode = _has_signal_payload(left) and _has_signal_payload(right)
    signal_axes_overlap = 0.0
    shape_compat = 0.0
    legacy_compat = 0.0
    broad_retrieval_reasons: Tuple[str, ...] = ()
    required_signals_present = _has_required_pattern_signals(left) and _has_required_pattern_signals(right)
    question_overlap = jaccard(_signal_axes(left), _signal_axes(right))
    manifest_overlap = _shape_compatibility(left, right)
    structural_compat = 0.0
    slot_overlap = jaccard(_slot_kind_keys(left), _slot_kind_keys(right))
    signal_axes_overlap = question_overlap
    shape_compat = manifest_overlap
    legacy_compat = _legacy_compatibility(left, right)
    lowering_overlap = jaccard(_canonical_lowering_families(left), _canonical_lowering_families(right))
    interface_overlap = 1.0 if (
        _repair_insight_interface_key(left)
        and _repair_insight_interface_key(left) == _repair_insight_interface_key(right)
    ) else 0.0
    score = (
        0.40 * signal_axes_overlap
        + 0.25 * shape_compat
        + 0.20 * lowering_overlap
        + 0.15 * interface_overlap
    )
    if not _is_absolute_conflict(veto):
        if required_signals_present:
            broad_retrieval_reasons = _broad_retrieval_reasons(
                left,
                right,
                signal_axes_overlap=signal_axes_overlap,
                shape_compat=shape_compat,
                legacy_compat=legacy_compat,
                slot_overlap=slot_overlap,
                question_overlap=question_overlap,
                manifest_overlap=manifest_overlap,
                structural_compat=structural_compat,
            )
        else:
            program_blockers = (*program_blockers, "signal_missing")
    if veto is None and broad_retrieval_reasons:
        (
            program_compatible,
            program_blockers,
            program_spans_output_grain,
            program_has_substantive_target_contract,
            effect_signature_count,
            shared_program_basis,
        ) = _shared_program_pair_compatibility(left, right)
        if program_compatible and shared_program_basis != "effect":
            program_compatible = False
            program_blockers = (*program_blockers, "missing_effect_backed_shared_program")
        if program_compatible and _program_core_signature(left) != _program_core_signature(right):
            # Direct family merging still requires one executable core package,
            # but pattern admission may treat this as a finite branch axis.
            program_compatible = False
            program_blockers = (*program_blockers, "core_program_signature_conflict")
    elif veto is None:
        program_blockers = ("no_broad_retrieval_signal",)
    accepted = bool(veto is None and program_compatible)
    failure_taxonomy = _taxonomy_from_blockers(
        program_blockers,
        veto_reason=veto,
        program_compatible=program_compatible,
        effect_signature_count=effect_signature_count,
    )
    semantic_relation = _pair_semantic_relation(
        accepted=accepted,
        veto_reason=veto,
        broad_retrieval_reasons=broad_retrieval_reasons,
        program_blockers=program_blockers,
    )
    branchable_for_pattern = bool(broad_retrieval_reasons) and semantic_relation in {
        "compatible",
        "partial",
        "direct_merge_veto",
        "core_program_signature_conflict",
    }
    pair = PairScore(
        left_group_id=left.group_id,
        right_group_id=right.group_id,
        left_case_ids=tuple(left.case_ids),
        right_case_ids=tuple(right.case_ids),
        question_overlap=question_overlap,
        manifest_overlap=manifest_overlap,
        structural_compat=structural_compat,
        slot_overlap=slot_overlap,
        score=score,
        accepted=accepted,
        veto_reason=veto,
        signal_axes_overlap=signal_axes_overlap,
        shape_compat=shape_compat,
        legacy_compat=legacy_compat,
        program_compatible=program_compatible,
        program_blockers=program_blockers,
        program_spans_output_grain=program_spans_output_grain,
        program_has_substantive_target_contract=program_has_substantive_target_contract,
        effect_signature_count=effect_signature_count,
        shared_program_basis=shared_program_basis,
        failure_taxonomy=failure_taxonomy,
        broad_retrieval_reasons=broad_retrieval_reasons,
        semantic_relation=semantic_relation,
        branchable_for_pattern=branchable_for_pattern,
    )
    _PAIR_SCORE_CACHE[cache_key] = pair
    return pair


class _UnionFind:
    def __init__(self, items: Sequence[str]) -> None:
        self.parent = {item: item for item in items}

    def find(self, item: str) -> str:
        parent = self.parent[item]
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root

    def components(self) -> List[List[str]]:
        buckets: Dict[str, List[str]] = defaultdict(list)
        for item in self.parent:
            buckets[self.find(item)].append(item)
        return [sorted(values) for values in buckets.values()]


def _pair_key(left: GroupSummary, right: GroupSummary) -> Tuple[str, str]:
    return tuple(sorted((left.group_id, right.group_id)))


def _ensure_pair_scores_for_groups(
    groups: Sequence[GroupSummary],
    pair_scores: Dict[Tuple[str, str], PairScore],
) -> int:
    """Fill the local all-pairs scores required after focus retrieval.

    Focus retrieval deliberately keeps the online candidate set small.  Once a
    connected component is selected for admission/build/reporting, downstream
    code needs complete intra-component evidence and must not assume the focus
    retrieval map already contains every member pair.
    """

    added = 0
    for index, left in enumerate(groups):
        for right in groups[index + 1 :]:
            key = _pair_key(left, right)
            if key in pair_scores:
                continue
            pair_scores[key] = score_pair(left, right)
            added += 1
    return added


def _completed_pair_scores_for_groups(
    groups: Sequence[GroupSummary],
    pair_scores: Dict[Tuple[str, str], PairScore],
) -> Dict[Tuple[str, str], PairScore]:
    _ensure_pair_scores_for_groups(groups, pair_scores)
    keys = {
        _pair_key(left, right)
        for index, left in enumerate(groups)
        for right in groups[index + 1 :]
    }
    return {key: pair_scores[key] for key in keys if key in pair_scores}


def _central_member(groups: Sequence[GroupSummary], pair_scores: Dict[Tuple[str, str], PairScore]) -> GroupSummary:
    if len(groups) == 1:
        return groups[0]
    _ensure_pair_scores_for_groups(groups, pair_scores)
    best = groups[0]
    best_score = -1.0
    for group in groups:
        scores = []
        for other in groups:
            if group.group_id == other.group_id:
                continue
            key = _pair_key(group, other)
            scores.append(pair_scores[key].score)
        avg = sum(scores) / len(scores) if scores else 0.0
        if avg > best_score:
            best = group
            best_score = avg
    return best


def _majority(values: Iterable[str], *, min_ratio: float = 0.5) -> List[str]:
    values = [value for value in values if value]
    if not values:
        return []
    counts = Counter(values)
    threshold = max(1, math.ceil(len(values) * min_ratio))
    return sorted([value for value, count in counts.items() if count >= threshold])


def _family_id(db_id: str, case_ids: Sequence[str]) -> str:
    sorted_ids = sorted([str(case_id) for case_id in case_ids], key=lambda value: int(value))
    digest = hashlib.sha1(",".join(sorted_ids).encode("utf-8")).hexdigest()[:8]
    return f"grp-fam-{db_id}-{sorted_ids[0]}-{sorted_ids[-1]}-{digest}"


def _pattern_id(db_id: str, case_ids: Sequence[str]) -> str:
    sorted_ids = sorted([str(case_id) for case_id in case_ids], key=lambda value: int(value))
    digest = hashlib.sha1(("pattern:" + ",".join(sorted_ids)).encode("utf-8")).hexdigest()[:8]
    return f"grp-pat-{db_id}-{sorted_ids[0]}-{sorted_ids[-1]}-{digest}"


def _slots_from_synthesized_program(program: Any) -> List[InstantiationSlot]:
    """Derive runtime slots from the shared program, never from a representative member."""
    if program is None:
        return []
    slots: List[InstantiationSlot] = []
    seen: Set[Tuple[str, str, bool, Tuple[str, ...]]] = set()
    for op in getattr(program, "ops", []) or []:
        op_payload = _model_dump(op)
        args = _model_dump(op_payload.get("arguments") or {})
        shared_signature = _model_dump(args.get("shared_signature") or {})
        for raw_slot in shared_signature.get("common_slot_signature") or []:
            payload = _model_dump(raw_slot)
            if not payload:
                continue
            kind = str(payload.get("kind") or payload.get("slot_kind") or "").strip()
            if not kind:
                continue
            allowed_roles = sorted(
                _as_set(
                    payload.get("allowed_role_families")
                    or payload.get("role_families")
                    or payload.get("roles")
                    or []
                )
            )
            required = bool(payload.get("required", True))
            name = str(payload.get("name") or f"program_slot_{len(slots) + 1}")
            key = (name, kind, required, tuple(allowed_roles))
            if key in seen:
                continue
            seen.add(key)
            slots.append(
                InstantiationSlot(
                    name=name,
                    kind=kind,
                    required=required,
                    allowed_role_families=allowed_roles,
                    description=payload.get("description"),
                )
            )
    return slots


def _program_structural_skeleton(
    program: Any,
    fallback: RepairSkeleton,
) -> RepairSkeleton:
    """Build family hard-contract skeleton from the canonical program.

    Representative member skeletons are audit summaries only. Runtime-facing
    family/pattern contracts must be derived from the synthesized program so a
    single member's local repair shape cannot leak into hard gates.
    """
    ops = list(getattr(program, "ops", []) or []) if program is not None else []
    if not ops:
        return fallback
    op_payload = _model_dump(ops[0])
    op_type = str(op_payload.get("op_type") or "").upper()
    raw_locus = str(op_payload.get("locus") or Locus.SELECT.value).upper()
    try:
        locus = Locus(raw_locus)
    except Exception:
        locus = Locus.SELECT
    op_family = OpFamily.REPLACE
    if "DROP" in op_type or "DELETE" in op_type:
        op_family = OpFamily.DROP
    elif "ADD" in op_type or "INSERT" in op_type:
        op_family = OpFamily.ADD
    elif "BRIDGE" in op_type:
        op_family = OpFamily.BRIDGE
        locus = Locus.BRIDGE
    elif "REROUTE" in op_type or "RELATION" in op_type:
        op_family = OpFamily.REROUTE
        locus = Locus.FACT_ROUTE if locus == Locus.SELECT else locus
    target_family = TargetFamily.SLOT
    if locus in {Locus.JOIN, Locus.BRIDGE}:
        target_family = TargetFamily.BRIDGE_PATH
    elif locus in {Locus.WHERE, Locus.PREDICATE, Locus.SCOPE}:
        target_family = TargetFamily.CONDITION
    elif locus == Locus.GRAIN:
        target_family = TargetFamily.GRAIN_KEY
    args = _model_dump(op_payload.get("arguments") or {})
    shape = (
        _model_dump(args.get("output_shape_delta") or {})
        or _model_dump(_model_dump(args.get("shared_signature") or {}).get("output_shape_delta") or {})
        or _model_dump(_model_dump(args.get("shared_arguments") or {}).get("output_shape_delta") or {})
    )
    structural = RepairSkeletonStructural(
        locus=locus,
        op_family=op_family,
        target_family=target_family,
        output_contract=OutputContract.UNCHANGED,
        output_shape_delta=shape or None,
        legacy_signature=None,
    )
    return RepairSkeleton(
        structural=structural,
        semantic=RepairSkeletonSemantic(
            intent="canonical shared repair program",
            family_hint=str(getattr(program, "program_type", "") or "") or None,
            notes="Derived from synthesized_program; representative skeleton is audit-only.",
        ),
    )


def _program_repair_goal(program: Any) -> str:
    if program is None or not getattr(program, "ops", None):
        return (
            "Offline family candidate without a compilable shared program; "
            "representative details are audit-only."
        )
    ops = [
        f"{str(getattr(op, 'op_type', '') or '')}@{str(getattr(op, 'locus', '') or '')}"
        for op in (getattr(program, "ops", []) or [])
        if str(getattr(op, "op_type", "") or "")
    ]
    invariants = [
        str(item)
        for item in (getattr(program, "target_invariants", []) or [])
        if str(item)
    ][:6]
    parts = ["Offline family candidate with synthesized canonical repair program"]
    if getattr(program, "program_type", None):
        parts.append(f"canonical_program={getattr(program, 'program_type')}")
    if ops:
        parts.append("ops=" + ",".join(ops[:6]))
    if invariants:
        parts.append("target_invariants=" + ",".join(invariants))
    return "; ".join(parts)


def _build_family(
    groups: Sequence[GroupSummary],
    pair_scores: Dict[Tuple[str, str], PairScore],
    *,
    runtime_usable: bool = False,
    require_effect_program: bool = False,
    group_type: GroupType = GroupType.PATTERN,
    formation_version: Optional[str] = None,
    promotion_state: str = "offline_pattern_candidate",
    pattern_admission: Optional[Dict[str, Any]] = None,
) -> GroupSummary:
    groups = sorted(groups, key=lambda group: int(group.case_ids[0]))
    pair_scores = _completed_pair_scores_for_groups(groups, pair_scores)
    db_id = groups[0].db_id
    case_ids = sorted(
        [case_id for group in groups for case_id in group.case_ids],
        key=lambda value: int(str(value)),
    )
    representative = _central_member(groups, pair_scores)
    now = datetime.utcnow().isoformat()

    question_tags = _majority(
        tag for group in groups for tag in _question_tags(group)
    )
    pred_tags = _majority(tag for group in groups for tag in _manifest_tags(group))
    required_question_raw = (
        sorted(set.intersection(*[_question_tags(group) for group in groups])) if groups else []
    )
    required_question = _runtime_required_question_tags(required_question_raw)
    required_pred = sorted(set.intersection(*[_manifest_tags(group) for group in groups])) if groups else []

    shared_axes = sorted(set.intersection(*[_signal_axes(group) for group in groups])) if all(
        _has_signal_payload(group) for group in groups
    ) else []
    member_signal_summaries = {
        str(group.case_ids[0]): {
            "group_id": group.group_id,
            "delta_axes": sorted(_signal_axes(group)),
            "shape_key": list(_shape_key(group)),
            "legacy_family": _legacy_family(group),
        }
        for group in groups
    }

    synthesis = synthesize_shared_program(
        groups,
        require_effect_program=require_effect_program,
    )
    synthesized_program = synthesis.program
    program_coverage = synthesis.coverage
    repair_goal = _program_repair_goal(synthesized_program)
    skeleton = _program_structural_skeleton(
        synthesized_program,
        representative.core_interface.repair_skeleton_prototype,
    )
    program_slots = _slots_from_synthesized_program(synthesized_program)
    merged_repair_program = repair_program_steps_from_canonical_program(
        synthesized_program
    )
    if not merged_repair_program:
        # Backward compatibility for pre-canonical libraries. Promotion will
        # still require a synthesized program, so this cannot silently become a
        # runtime pattern.
        merged_repair_program = _merge_repair_programs(groups)
    formation_signals = {
        "schema_version": (
            "formation-pattern-signal-v0"
            if group_type == GroupType.PATTERN
            else "formation-family-signal-v0"
        ),
        "representative_group_id": representative.group_id,
        "pred_current": {} if synthesized_program is not None else _signal_pred_current(representative),
        "delta": {} if synthesized_program is not None else _signal_delta(representative),
        "representative_snapshot": {
            "pred_current": _signal_pred_current(representative),
            "delta": _signal_delta(representative),
            "repair_skeleton": _model_dump(representative.core_interface.repair_skeleton_prototype),
        },
        "repair_skeleton": skeleton.model_dump(mode="json") if hasattr(skeleton, "model_dump") else _model_dump(skeleton),
        "shared_delta_axes": shared_axes,
        "member_signals": member_signal_summaries,
        "repair_program": merged_repair_program,
        "synthesized_program": (
            compact_synthesized_program_for_memory(synthesized_program)
            if synthesized_program is not None
            else None
        ),
        "repair_insight_signature": (
            synthesized_program.repair_insight_signature.model_dump(mode="json")
            if synthesized_program is not None
            and synthesized_program.repair_insight_signature is not None
            else None
        ),
        "program_coverage": program_coverage.model_dump(mode="json"),
        "synthesis_basis": synthesis.synthesis_basis,
        "effect_first_required": bool(require_effect_program),
    }
    if pattern_admission is not None:
        formation_signals["pattern_admission"] = dict(pattern_admission)
    trigger_contract = build_trigger_contract(
        formation_signals=formation_signals,
        error_instance=None,
        decisive_pred_signals=required_pred,
        decisive_question_signals=required_question,
        negative_signals=[],
        max_actions=3,
    )
    member_evidence: List[GroupMemberEvidence] = []
    for group in groups:
        evidence = getattr(group, "formation_evidence", None)
        member_evidence.extend(list(getattr(evidence, "member_evidence", []) or []))
    accepted_pair_payloads = [
        pair.to_dict() for pair in pair_scores.values() if pair.accepted
    ]
    rejected_pair_payloads = [
        pair.to_dict()
        for pair in sorted(pair_scores.values(), key=lambda pair: pair.score, reverse=True)
        if not pair.accepted
    ][:50]

    resolved_formation_version = formation_version or (
        "offline-effect-family-v1"
        if require_effect_program
        else "offline-family-clique-v1"
    )
    family = GroupSummary(
        group_id=(
            _pattern_id(db_id, case_ids)
            if group_type == GroupType.PATTERN
            else _family_id(db_id, case_ids)
        ),
        group_type=group_type,
        db_id=db_id,
        case_ids=case_ids,
        support=len(case_ids),
        confidence=Confidence.MEDIUM if len(case_ids) >= 3 else Confidence.LOW,
        version=0,
        runtime_usable=runtime_usable,
        status=GroupStatus.ACTIVE,
        core_interface=CoreInterface(
            question_family_tags=question_tags,
            pred_family_tags=pred_tags,
            repair_goal=repair_goal,
            repair_skeleton_prototype=skeleton,
        ),
        instantiation_program=InstantiationProgram(
            shared=bool(synthesized_program and synthesized_program.ops),
            shared_status=(
                "structural"
                if bool(synthesized_program and synthesized_program.ops)
                else "none"
            ),
            template=None,
            slots=program_slots,
            branch_rules=[],
            repair_program=merged_repair_program,
            synthesized_program=synthesized_program,
            program_coverage=program_coverage,
        ),
        trigger_signature=TriggerSignature(
            required_question_tags=required_question,
            required_pred_tags=required_pred,
            decisive_antipatterns=[],
            negative_evidence=[],
        ),
        guardrails=[],
        action_realization_traces=[],
        model_profile=ModelProfile(),
        replay_history=[],
        trigger_contract=trigger_contract,
        formation_signals=formation_signals,
        formation_evidence=GroupFormationEvidence(
            member_evidence=member_evidence,
            pair_scores=[pair.to_dict() for pair in pair_scores.values()],
            accepted_edges=accepted_pair_payloads,
            rejected_edge_sample=rejected_pair_payloads,
            formation_version=(
                resolved_formation_version
            ),
            review_status="unreviewed",
        ),
        lifecycle=GroupLifecycle(
            formation_parent_ids=[group.group_id for group in groups],
            promotion_state=promotion_state,
        ),
        created_at=now,
        last_updated_at=now,
    )
    if family.runtime_usable:
        ensure_materialized_trigger_contract(family)
    return family


def build_family_from_groups(
    groups: Sequence[GroupSummary],
    *,
    runtime_usable: bool = False,
    require_effect_program: bool = False,
    group_type: GroupType = GroupType.PATTERN,
) -> GroupSummary:
    """Build one shared-program object from already selected compatible members.

    This is used by replay promotion to construct leave-one-out training
    memories. It intentionally reuses the same generic pair scorer as offline
    formation and does not inspect database-specific names.
    """
    groups = list(groups)
    pair_scores: Dict[Tuple[str, str], PairScore] = {}
    for i, left in enumerate(groups):
        for right in groups[i + 1 :]:
            key = tuple(sorted((left.group_id, right.group_id)))
            pair_scores[key] = score_pair(left, right)
    return _build_family(
        groups,
        pair_scores,
        runtime_usable=runtime_usable,
        require_effect_program=require_effect_program,
        group_type=group_type,
    )


def _coherent_components(
    component: Sequence[str],
    pair_scores: Dict[Tuple[str, str], PairScore],
    by_id: Dict[str, GroupSummary],
) -> List[List[str]]:
    """Split transitive components into all-pairs-compatible subgroups.

    Union-find is good at finding connected neighborhoods, but a connected chain
    A-B-C does not prove A and C share the same repair interface. Family
    candidates must be internally coherent before replay promotion can consider
    them.
    """
    clusters: List[List[str]] = []
    for group_id in sorted(component):
        best_index: Optional[int] = None
        best_score = -1.0
        for index, cluster in enumerate(clusters):
            scores: List[float] = []
            accepted = True
            for other_id in cluster:
                key = tuple(sorted((group_id, other_id)))
                pair = pair_scores.get(key)
                if pair is None or not pair.accepted:
                    accepted = False
                    break
                scores.append(pair.score)
            if not accepted:
                continue
            candidate_cluster = [*cluster, group_id]
            if not _component_program_coherent(
                [by_id[item] for item in candidate_cluster if item in by_id]
            ):
                continue
            avg_score = sum(scores) / len(scores) if scores else 0.0
            if avg_score > best_score:
                best_score = avg_score
                best_index = index
        if best_index is None:
            clusters.append([group_id])
        else:
            clusters[best_index].append(group_id)
    return clusters


def _component_program_coherent(groups: Sequence[GroupSummary]) -> bool:
    if len(groups) < 2:
        return True
    result = synthesize_shared_program(groups, require_effect_program=True)
    program = result.program
    if program is None:
        return False
    if result.coverage.blockers:
        return False
    if float(result.coverage.compile_coverage or 0.0) < 1.0:
        return False
    if not _program_effect_candidates(program):
        return False
    return True


_PATTERN_ADMISSION_CACHE: Dict[str, Dict[str, Any]] = {}
_INSIGHT_PATTERN_SLICER_CACHE: Dict[str, Dict[str, Any]] = {}


def _case_id_sort_key(value: Any) -> Tuple[int, str]:
    text = str(value)
    return (int(text), text) if text.isdigit() else (10**12, text)


def _stable_bias_frame(group: GroupSummary) -> Dict[str, Any]:
    """Compact, insight-first view used before formal pattern admission.

    This frame intentionally makes stable bias fields prominent and demotes SQL
    edit packages to branch evidence. It is the unit passed to the slicer so
    large retrieved components are not split by repair path before semantic
    review.
    """
    signals = _signal_payload(group)
    ir = dict((signals.get("canonical_repair_ir") or {}) or {})
    insight = dict((signals.get("repair_insight_signature") or {}) or {})
    effects = []
    repair_effect_signature = _model_dump(ir.get("repair_effect_signature") or {})
    for effect in repair_effect_signature.get("effect_candidates") or []:
        payload = _model_dump(effect)
        effects.append(
            {
                "axis": payload.get("axis"),
                "role": payload.get("role"),
                "delta_kind": _model_dump(payload.get("delta") or {}).get("kind"),
                "primitive": _model_dump(payload.get("actionability") or {}).get("primitive"),
                "triggerability": _model_dump(payload.get("triggerability") or {}),
            }
        )
    binding_slots = []
    for slot in list(insight.get("binding_slots") or [])[:8]:
        payload = _model_dump(slot)
        binding_slots.append(
            {
                "kind": payload.get("kind"),
                "source_or_target": payload.get("source_or_target"),
                "required": payload.get("required"),
                "allowed_role_families": list(payload.get("allowed_role_families") or [])[:6],
            }
        )
    return {
        "case_ids": [str(case_id) for case_id in group.case_ids],
        "group_id": group.group_id,
        "interface_key": insight.get("interface_key"),
        "source_misread": insight.get("source_misread"),
        "target_preference": insight.get("target_preference"),
        "repair_interface": insight.get("repair_interface"),
        "binding_slots": binding_slots,
        "preserve_invariants": list(insight.get("preserve_invariants") or [])[:8],
        "negative_guards": list(insight.get("negative_guards") or [])[:8],
        "axis_links": list(insight.get("axis_links") or [])[:8],
        "delta_axes": sorted(_signal_axes(group)),
        "shape_delta": _shape_delta(group),
        "canonical_lowering_families": sorted(_canonical_lowering_families(group)),
        "core_program_signature": [list(item) for item in _program_core_signature(group)],
        "required_dependency_signature": [
            list(item) for item in _program_dependency_signature(group, required_only=True)
        ],
        "optional_dependency_signature": [
            list(item) for item in _program_dependency_signature(group, required_only=False)
        ],
        "effects": effects[:8],
    }


def _pattern_case_card(group: GroupSummary) -> Dict[str, Any]:
    signals = _signal_payload(group)
    ir = dict((signals.get("canonical_repair_ir") or {}) or {})
    insight = dict((signals.get("repair_insight_signature") or {}) or {})
    program_core = []
    for op in (ir.get("program_ops") or [])[:6]:
        payload = _model_dump(op)
        args = _model_dump(payload.get("arguments") or {})
        signature = _model_dump(args.get("operation_signature") or args.get("shared_signature") or {})
        role_delta = _model_dump(signature.get("role_delta") or {})
        program_core.append(
            {
                "op_type": payload.get("op_type"),
                "locus": payload.get("locus"),
                "lowering_family": canonical_op_lowering_family(
                    payload.get("op_type"),
                    payload.get("locus"),
                ),
                "is_dependency": bool(signature.get("is_dependency") or payload.get("is_dependency") or False),
                "required": bool(signature.get("required", payload.get("required", True))),
                "identity_role": str(args.get("identity_role") or payload.get("identity_role") or ""),
                "runtime_policy": str(args.get("runtime_policy") or payload.get("runtime_policy") or ""),
                "role_delta": {
                    "arity_direction": role_delta.get("arity_direction"),
                    "target_output_subset_of_source": role_delta.get("target_output_subset_of_source"),
                    "source_output_roles": list(role_delta.get("source_output_roles") or [])[:6],
                    "target_output_roles": list(role_delta.get("target_output_roles") or [])[:6],
                },
                "slot_signature": list(signature.get("slot_signature") or args.get("step_slots") or [])[:6],
                "invariants": [_short_text(item, limit=120) for item in list(payload.get("invariants") or [])[:6]],
            }
        )
    effects = []
    repair_effect_signature = _model_dump(ir.get("repair_effect_signature") or {})
    for effect in repair_effect_signature.get("effect_candidates") or []:
        payload = _model_dump(effect)
        effects.append(
            {
                "axis": payload.get("axis"),
                "role": payload.get("role"),
                "delta": _model_dump(payload.get("delta") or {}),
                "actionability": _model_dump(payload.get("actionability") or {}),
                "triggerability": _model_dump(payload.get("triggerability") or {}),
            }
        )
    return {
        "case_ids": [str(case_id) for case_id in group.case_ids],
        "group_id": group.group_id,
        "stable_bias_frame": _stable_bias_frame(group),
        "delta_axes": sorted(_signal_axes(group)),
        "shape_delta": _shape_delta(group),
        "legacy_family": _legacy_family(group),
        "canonical_lowering_families": sorted(_canonical_lowering_families(group)),
        "core_program_signature": [list(item) for item in _program_core_signature(group)],
        "required_dependency_signature": [
            list(item) for item in _program_dependency_signature(group, required_only=True)
        ],
        "optional_dependency_signature": [
            list(item) for item in _program_dependency_signature(group, required_only=False)
        ],
        "repair_insight": {
            "interface_key": _short_text(insight.get("interface_key"), limit=220),
            "source_misread": _short_text(insight.get("source_misread"), limit=220),
            "target_preference": _short_text(insight.get("target_preference"), limit=220),
            "repair_interface": _short_text(insight.get("repair_interface"), limit=220),
            "binding_slots": list(insight.get("binding_slots") or [])[:8],
            "preserve_invariants": list(insight.get("preserve_invariants") or [])[:8],
            "negative_guards": list(insight.get("negative_guards") or [])[:8],
            "axis_links": list(insight.get("axis_links") or [])[:8],
        },
        "program_core": program_core,
        "effects": effects[:8],
    }


def _pattern_pair_decision(
    pair: PairScore,
    *,
    include_blockers: bool = True,
) -> Dict[str, Any]:
    payload = {
        "left_case_ids": list(pair.left_case_ids),
        "right_case_ids": list(pair.right_case_ids),
        "semantic_relation": pair.semantic_relation,
        "branchable_for_pattern": pair.branchable_for_pattern,
        "veto_reason": pair.veto_reason,
        "broad_retrieval_reasons": list(pair.broad_retrieval_reasons),
        "failure_taxonomy": list(pair.failure_taxonomy),
    }
    if include_blockers:
        payload["program_blockers"] = list(pair.program_blockers)[:12]
    else:
        payload["program_blocker_categories"] = list(pair.failure_taxonomy)
    return payload


def _pattern_root_identity_key(group: GroupSummary) -> Tuple[str, ...]:
    """Root-bias identity for superseding prefix pattern rediscoveries."""

    admission = dict((_signal_payload(group).get("pattern_admission") or {}) or {})
    program = _model_dump(getattr(group.instantiation_program, "synthesized_program", None))
    effect_rows = []
    for effect in _program_effect_candidates(getattr(group.instantiation_program, "synthesized_program", None)):
        payload = _model_dump(effect)
        delta = _model_dump(payload.get("delta") or {})
        effect_rows.append(
            (
                str(payload.get("axis") or ""),
                str(payload.get("role") or ""),
                str(delta.get("kind") or ""),
                str(delta.get("arity_direction") or ""),
                str(delta.get("direction") or ""),
            )
        )
    stable_bias = str(admission.get("stable_bias_key") or "").strip().lower()
    interface = str(admission.get("primary_repair_interface") or "").strip().lower()
    if effect_rows or stable_bias or interface:
        return (
            "root",
            stable_bias,
            interface,
            _canonical_payload(sorted(effect_rows)),
        )
    return (
        "program",
        _canonical_payload(
            [
                (
                    str(item.get("op_family") or item.get("action_lowering_family") or ""),
                    str(item.get("target_family") or item.get("effect_axis") or ""),
                )
                for item in (program.get("ops") or [])
                if _model_dump(item)
            ]
        ),
    )


def _pattern_identity_key(group: GroupSummary) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[Tuple[str, str, str], ...]]:
    """Stable identity for deduping repeatedly rediscovered pattern candidates."""
    case_key = tuple(sorted((str(case_id) for case_id in group.case_ids or []), key=_case_id_sort_key))
    root_key = _pattern_root_identity_key(group)
    program = _model_dump(getattr(group.instantiation_program, "synthesized_program", None))
    op_key = tuple(
        sorted(
            (
                str(item.get("canonical_op_id") or item.get("op_id") or ""),
                str(item.get("op_family") or item.get("action_lowering_family") or ""),
                str(item.get("target_family") or item.get("effect_axis") or ""),
            )
            for item in (program.get("ops") or [])
            if _model_dump(item)
        )
    )
    return case_key, root_key, op_key


def _dedupe_patterns(patterns: Sequence[GroupSummary]) -> List[GroupSummary]:
    by_identity: Dict[Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[Tuple[str, str, str], ...]], GroupSummary] = {}
    by_root: Dict[Tuple[str, ...], List[GroupSummary]] = defaultdict(list)
    for pattern in patterns or []:
        if pattern.group_type != GroupType.PATTERN:
            continue
        key = _pattern_identity_key(pattern)
        existing = by_identity.get(key)
        if existing is None:
            by_identity[key] = pattern
            continue
        if pattern.runtime_usable and not existing.runtime_usable:
            by_identity[key] = pattern
            continue
        if pattern.runtime_usable == existing.runtime_usable and str(pattern.version) > str(existing.version):
            by_identity[key] = pattern
    for pattern in by_identity.values():
        by_root[_pattern_root_identity_key(pattern)].append(pattern)
    kept: List[GroupSummary] = []
    for _root_key, root_patterns in by_root.items():
        root_patterns = sorted(
            root_patterns,
            key=lambda group: (
                len({str(case_id) for case_id in group.case_ids or []}),
                bool(group.runtime_usable),
                str(group.version),
            ),
            reverse=True,
        )
        selected_for_root: List[GroupSummary] = []
        for pattern in root_patterns:
            case_set = {str(case_id) for case_id in pattern.case_ids or []}
            if any(case_set and case_set <= {str(case_id) for case_id in existing.case_ids or []} for existing in selected_for_root):
                continue
            selected_for_root.append(pattern)
        kept.extend(selected_for_root)
    return sorted(
        kept,
        key=lambda group: (
            tuple(sorted((str(case_id) for case_id in group.case_ids or []), key=_case_id_sort_key)),
            group.group_id,
        ),
    )


def _pair_decision_context(
    pairs: Sequence[PairScore],
    *,
    per_relation_limit: int = PATTERN_ADMISSION_PAIR_PER_RELATION_LIMIT,
) -> Dict[str, Any]:
    """Compact pair context for LLM admission and reports.

    Pair decisions are second-order audit evidence.  The LLM receives relation
    counts plus representative edges; full O(n^2) pair payloads stay out of the
    prompt.
    """
    relation_counts = dict(Counter(pair.semantic_relation for pair in pairs))
    selected: List[PairScore] = []
    seen_keys: Set[Tuple[str, str]] = set()

    def add_pair(pair: PairScore) -> None:
        key = tuple(sorted((pair.left_group_id, pair.right_group_id)))
        if key in seen_keys:
            return
        seen_keys.add(key)
        selected.append(pair)

    relation_order = [
        "compatible",
        "partial",
        "direct_merge_veto",
        "core_program_signature_conflict",
        "conflict",
        "semantic_review_rejected",
        "hard_conflict",
        "not_candidate",
    ]
    pairs_by_relation: Dict[str, List[PairScore]] = defaultdict(list)
    for pair in pairs:
        pairs_by_relation[str(pair.semantic_relation or "not_candidate")].append(pair)
    for relation in relation_order:
        ranked = sorted(
            pairs_by_relation.get(relation, []),
            key=lambda pair: (
                -float(pair.score or 0.0),
                tuple(pair.left_case_ids),
                tuple(pair.right_case_ids),
            ),
        )
        for pair in ranked[:per_relation_limit]:
            add_pair(pair)

    covered_case_ids = {
        str(case_id)
        for pair in selected
        for case_id in [*pair.left_case_ids, *pair.right_case_ids]
    }
    all_case_ids = {
        str(case_id)
        for pair in pairs
        for case_id in [*pair.left_case_ids, *pair.right_case_ids]
    }
    ranked_all = sorted(
        list(pairs or []),
        key=lambda pair: (
            relation_order.index(pair.semantic_relation)
            if pair.semantic_relation in relation_order
            else len(relation_order),
            -float(pair.score or 0.0),
            tuple(pair.left_case_ids),
            tuple(pair.right_case_ids),
        ),
    )
    for case_id in sorted(all_case_ids - covered_case_ids, key=_case_id_sort_key):
        if len(selected) >= PATTERN_ADMISSION_MAX_REPRESENTATIVE_PAIRS:
            break
        candidates = [
            pair
            for pair in ranked_all
            if case_id
            in (
                {str(item) for item in pair.left_case_ids}
                | {str(item) for item in pair.right_case_ids}
            )
        ]
        if candidates:
            add_pair(candidates[0])
            covered_case_ids = {
                str(member_case_id)
                for pair in selected
                for member_case_id in [*pair.left_case_ids, *pair.right_case_ids]
            }
    return {
        "schema_version": "pair-decision-context-v1",
        "pair_count": len(list(pairs or [])),
        "semantic_relation_counts": relation_counts,
        "covered_case_ids": sorted(covered_case_ids, key=_case_id_sort_key),
        "uncovered_case_ids": sorted(all_case_ids - covered_case_ids, key=_case_id_sort_key),
        "representative_pairs": [
            _pattern_pair_decision(pair, include_blockers=False) for pair in selected
        ],
    }


def _case_card_case_ids(card: Dict[str, Any]) -> Set[str]:
    return {str(case_id) for case_id in (card.get("case_ids") or []) if str(case_id)}


def _sample_pattern_case_cards(
    cards: Sequence[Dict[str, Any]],
    pair_context: Dict[str, Any],
    *,
    max_cards: int,
) -> List[Dict[str, Any]]:
    """Budgeted admission sample that preserves representative-edge evidence."""
    sorted_cards = sorted(
        list(cards or []),
        key=lambda card: _case_id_sort_key((card.get("case_ids") or [""])[0]),
    )
    selected: List[Dict[str, Any]] = []
    selected_ids: Set[int] = set()

    def add_card(card: Dict[str, Any]) -> None:
        if len(selected) >= max_cards:
            return
        identity = id(card)
        if identity in selected_ids:
            return
        selected_ids.add(identity)
        selected.append(card)

    priority_case_ids: List[str] = []
    for pair in pair_context.get("representative_pairs") or []:
        payload = _model_dump(pair)
        for side in ("left_case_ids", "right_case_ids"):
            for case_id in payload.get(side) or []:
                text = str(case_id)
                if text and text not in priority_case_ids:
                    priority_case_ids.append(text)

    for case_id in priority_case_ids:
        for card in sorted_cards:
            if case_id in _case_card_case_ids(card):
                add_card(card)
                break
        if len(selected) >= max_cards:
            break

    for card in sorted_cards:
        add_card(card)
        if len(selected) >= max_cards:
            break

    return selected


def _slicer_pair_decisions(pairs: Sequence[PairScore]) -> List[Dict[str, Any]]:
    """Keep slicer pair context compact and representative.

    The slicer needs examples of relation types, not every pair in a large
    component. Sending all pair decisions made large databases slow and noisy.
    """
    priority = {
        "compatible": 0,
        "partial": 1,
        "direct_merge_veto": 2,
        "core_program_signature_conflict": 3,
        "conflict": 4,
        "semantic_review_rejected": 5,
        "not_candidate": 6,
        "hard_conflict": 7,
    }
    pair_list = list(pairs or [])
    ranked = sorted(
        pair_list,
        key=lambda pair: (
            priority.get(pair.semantic_relation, 99),
            -float(pair.score or 0.0),
            tuple(pair.left_case_ids),
            tuple(pair.right_case_ids),
        ),
    )
    selected: List[PairScore] = []
    seen_keys: Set[Tuple[str, str]] = set()

    def add(pair: PairScore) -> None:
        key = (pair.left_group_id, pair.right_group_id)
        if key in seen_keys or len(selected) >= 40:
            return
        seen_keys.add(key)
        selected.append(pair)

    by_relation: Dict[str, List[PairScore]] = defaultdict(list)
    for pair in ranked:
        by_relation[pair.semantic_relation].append(pair)
    for relation in sorted(by_relation, key=lambda item: priority.get(item, 99)):
        for pair in by_relation[relation][:4]:
            add(pair)

    covered_case_ids = {
        str(case_id)
        for pair in selected
        for case_id in [*pair.left_case_ids, *pair.right_case_ids]
    }
    all_case_ids = {
        str(case_id)
        for pair in pair_list
        for case_id in [*pair.left_case_ids, *pair.right_case_ids]
    }
    for case_id in sorted(all_case_ids - covered_case_ids, key=_case_id_sort_key):
        candidates = [
            pair
            for pair in ranked
            if case_id
            in (
                {str(item) for item in pair.left_case_ids}
                | {str(item) for item in pair.right_case_ids}
            )
        ]
        if candidates:
            add(candidates[0])
    return [_pattern_pair_decision(pair, include_blockers=False) for pair in selected]


def _component_summary(
    groups: Sequence[GroupSummary],
    member_pair_scores: Sequence[PairScore],
) -> Dict[str, Any]:
    return {
        "db_id": groups[0].db_id if groups else "",
        "case_ids": sorted(
            {str(case_id) for group in groups for case_id in group.case_ids},
            key=_case_id_sort_key,
        ),
        "shared_delta_axes": sorted(
            set.intersection(*[_signal_axes(group) for group in groups])
            if all(_signal_axes(group) for group in groups)
            else set()
        ),
        "union_delta_axes": sorted(
            {axis for group in groups for axis in _signal_axes(group)}
        ),
        "shared_lowering_families": sorted(
            set.intersection(*[_canonical_lowering_families(group) for group in groups])
            if all(_canonical_lowering_families(group) for group in groups)
            else set()
        ),
        "core_program_signature_counts": {
            _canonical_payload([list(item) for item in signature]): len(bucket)
            for signature, bucket in _core_signature_buckets(groups).items()
        },
        "semantic_relation_counts": dict(
            Counter(pair.semantic_relation for pair in member_pair_scores)
        ),
    }


def _call_insight_pattern_slicer(
    *,
    groups: Sequence[GroupSummary],
    member_pair_scores: Sequence[PairScore],
) -> Dict[str, Any]:
    frames = [_stable_bias_frame(group) for group in groups]
    pair_decisions = _slicer_pair_decisions(member_pair_scores)
    component_summary = _component_summary(groups, member_pair_scores)
    cache_key = hashlib.sha1(
        _canonical_payload(
            {
                "stable_bias_frames": frames,
                "pair_decisions": pair_decisions,
                "component_summary": component_summary,
            }
        ).encode("utf-8")
    ).hexdigest()
    if cache_key in _INSIGHT_PATTERN_SLICER_CACHE:
        return dict(_INSIGHT_PATTERN_SLICER_CACHE[cache_key])

    from method.EEA.rulebook.common.llm.utils import call_llm
    from method.EEA.rulebook.common.llm.prompts.insight_pattern_slicer import build_insight_pattern_slicer_prompt

    prompt = build_insight_pattern_slicer_prompt(
        stable_bias_frames_json=json.dumps(frames, ensure_ascii=False, indent=2, default=str),
        pair_semantic_decisions_json=json.dumps(
            pair_decisions,
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        component_summary_json=json.dumps(
            component_summary,
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
    )
    raw = call_llm(
        prompt,
        expect_json=True,
        stage="insight_pattern_slicer",
        trace_context={
            "case_ids": component_summary.get("case_ids", []),
            "group_count": len(groups or []),
            "pair_count": len(member_pair_scores or []),
        },
    )
    response = dict(raw) if isinstance(raw, dict) else {}
    _INSIGHT_PATTERN_SLICER_CACHE[cache_key] = response
    return response


def _call_pattern_admission_judge(
    *,
    groups: Sequence[GroupSummary],
    member_pair_scores: Sequence[PairScore],
    slicer_candidate: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    case_cards = [_pattern_case_card(group) for group in groups]
    pair_decision_context = _pair_decision_context(member_pair_scores)
    component_summary = _component_summary(groups, member_pair_scores)
    if slicer_candidate:
        component_summary = dict(component_summary)
        component_summary["slicer_hypothesis"] = {
            "candidate_id": slicer_candidate.get("candidate_id"),
            "stable_bias_hypothesis": slicer_candidate.get("stable_bias_hypothesis"),
            "branch_hypothesis": slicer_candidate.get("branch_hypothesis"),
            "why_grouped": slicer_candidate.get("why_grouped"),
        }
    cache_key = hashlib.sha1(
        _canonical_payload(
            {
                "case_cards": case_cards,
                "pair_decision_context": pair_decision_context,
                "component_summary": component_summary,
            }
        ).encode("utf-8")
    ).hexdigest()
    if cache_key in _PATTERN_ADMISSION_CACHE:
        return _attach_validated_bias_recognition_contract(
            dict(_PATTERN_ADMISSION_CACHE[cache_key])
        )

    from method.EEA.rulebook.common.llm.utils import call_llm
    from method.EEA.rulebook.common.llm.prompts.pattern_admission_judge import build_pattern_admission_judge_prompt

    def build_prompt(
        cards: Sequence[Dict[str, Any]],
        pair_context: Dict[str, Any],
        summary: Dict[str, Any],
        *,
        compact: bool = False,
    ) -> str:
        dumps_kwargs = (
            {"ensure_ascii": False, "default": str, "separators": (",", ":")}
            if compact
            else {"ensure_ascii": False, "indent": 2, "default": str}
        )
        return build_pattern_admission_judge_prompt(
            case_cards_json=json.dumps(list(cards), **dumps_kwargs),
            pair_semantic_decisions_json=json.dumps(pair_context, **dumps_kwargs),
            component_summary_json=json.dumps(summary, **dumps_kwargs),
        )

    prompt = build_pattern_admission_judge_prompt(
        case_cards_json=json.dumps(case_cards, ensure_ascii=False, indent=2, default=str),
        pair_semantic_decisions_json=json.dumps(pair_decision_context, ensure_ascii=False, indent=2, default=str),
        component_summary_json=json.dumps(component_summary, ensure_ascii=False, indent=2, default=str),
    )
    sampled_case_ids: List[str] = []
    if len(prompt) > PATTERN_ADMISSION_PROMPT_BUDGET_CHARS:
        compact_pair_decision_context = _pair_decision_context(
            member_pair_scores,
            per_relation_limit=PATTERN_ADMISSION_COMPACT_PAIR_PER_RELATION_LIMIT,
        )
        sampled_cards = _sample_pattern_case_cards(
            case_cards,
            compact_pair_decision_context,
            max_cards=PATTERN_ADMISSION_MAX_GROUP_CARDS,
        )
        sampled_case_ids = [
            str(case_id)
            for card in sampled_cards
            for case_id in (card.get("case_ids") or [])
        ]
        compact_summary = dict(component_summary)
        compact_summary["all_case_ids"] = component_summary.get("case_ids", [])
        compact_summary["sampled_case_ids"] = sampled_case_ids
        compact_summary["sampling_policy"] = (
            "case_cards_sampled_from_representative_pairs_for_prompt_budget; "
            "full ids remain in component summary"
        )
        pair_decision_context = compact_pair_decision_context
        component_summary = compact_summary
        case_cards = sampled_cards
        prompt = build_prompt(case_cards, pair_decision_context, component_summary, compact=True)
    raw = call_llm(
        prompt,
        expect_json=True,
        stage="pattern_admission_judge",
        trace_context={
            "case_ids": component_summary.get("case_ids", []),
            "group_count": len(groups or []),
            "pair_count": len(member_pair_scores or []),
            "representative_pair_count": len(
                pair_decision_context.get("representative_pairs") or []
            ),
            "prompt_chars": len(prompt),
            "sampled_case_ids": sampled_case_ids,
        },
    )
    response = dict(raw) if isinstance(raw, dict) else {}
    response = _attach_validated_bias_recognition_contract(response)
    _PATTERN_ADMISSION_CACHE[cache_key] = response
    return response


def _bias_signal_from_runtime_signal(signal: str) -> Optional[str]:
    text = str(signal or "")
    mapping = {
        "pred.role_side_pair_output=True": "has_pair_role_side_output",
        "pred.same_relation_two_role_sides=True": "same_relation_two_role_sides",
        "pred.pair_output=True": "select_arity_ge_2",
        "pred.output_arity=2": "select_arity_ge_2",
        "pred.select_arity=2": "select_arity_ge_2",
        "pred.has_aggregate=True": "has_aggregate_in_select",
        "pred.has_distinct_aggregate=True": "answer_unit_count_distinct",
        "pred.has_group_by=True": "has_group_by",
    }
    if text in mapping:
        return mapping[text]
    if text.startswith("pred.output_arity="):
        try:
            return "select_arity_ge_2" if int(text.rsplit("=", 1)[1]) >= 2 else None
        except Exception:
            return None
    if text.startswith("pred.select_arity="):
        try:
            return "select_arity_ge_2" if int(text.rsplit("=", 1)[1]) >= 2 else None
        except Exception:
            return None
    return None


def _sanitize_bias_text(value: Any, limit: int = 80) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_\\- ]+", "", text)
    return re.sub(r"\\s+", "_", text)[:limit]


def _validated_bias_recognition_contract_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    brc = _model_dump(raw.get("bias_recognition_contract") or {})
    if not brc:
        return {}
    sigs = [
        str(signal)
        for signal in (brc.get("recognition_signals") or [])
        if str(signal) in BIAS_RECOGNITION_SIGNAL_VOCABULARY
    ]
    anti = [
        str(signal)
        for signal in (brc.get("anti_signals") or [])
        if str(signal) in BIAS_RECOGNITION_SIGNAL_VOCABULARY
    ]
    sigs = sorted(dict.fromkeys(sigs))
    anti = sorted(dict.fromkeys(anti))
    if not (3 <= len(sigs) <= 6):
        return {}
    try:
        threshold = float(brc.get("min_signal_overlap", 0.6) or 0.6)
    except Exception:
        threshold = 0.6
    threshold = min(1.0, max(0.0, threshold))
    return {
        "schema_version": "bias-recognition-v1",
        "bias_motif": _sanitize_bias_text(brc.get("bias_motif")),
        "answer_shape_hint": _sanitize_bias_text(brc.get("answer_shape_hint"), limit=40),
        "recognition_signals": sigs,
        "anti_signals": anti,
        "min_signal_overlap": threshold,
    }


def _attach_validated_bias_recognition_contract(response: Dict[str, Any]) -> Dict[str, Any]:
    payload = _validated_bias_recognition_contract_payload(response)
    if payload:
        response["bias_recognition_contract_validated"] = payload
    return response


def _fallback_bias_recognition_contract(groups: Sequence[GroupSummary]) -> Optional[BiasRecognitionContract]:
    votes: Counter[str] = Counter()
    for group in groups:
        contract = _model_dump(getattr(group, "trigger_contract", None))
        signal_rows: List[str] = []
        signal_rows.extend(str(item) for item in (contract.get("required_signals") or []) if str(item))
        signal_rows.extend(str(item) for item in (contract.get("decisive_pred_signals") or []) if str(item))
        for variant in contract.get("variant_required_signal_sets") or []:
            if isinstance(variant, (list, tuple, set)):
                signal_rows.extend(str(item) for item in variant if str(item))
        for signal in signal_rows:
            mapped = _bias_signal_from_runtime_signal(signal)
            if mapped:
                votes[mapped] += 1
    selected = [signal for signal, _count in votes.most_common(6)]
    if len(selected) < 3:
        return None
    return BiasRecognitionContract(
        bias_motif="fallback_from_runtime_signals",
        answer_shape_hint="other",
        recognition_signals=sorted(selected[:6]),
        anti_signals=[],
        min_signal_overlap=0.6,
    )


def _member_pair_scores(
    groups: Sequence[GroupSummary],
    pair_scores: Dict[Tuple[str, str], PairScore],
) -> List[PairScore]:
    _ensure_pair_scores_for_groups(groups, pair_scores)
    out: List[PairScore] = []
    for index, left in enumerate(groups):
        for right in groups[index + 1 :]:
            key = _pair_key(left, right)
            pair = pair_scores.get(key)
            if pair is not None:
                out.append(pair)
    return out


def _pattern_admitted_case_ids(
    response: Dict[str, Any],
    available_case_ids: Set[str],
) -> List[str]:
    raw = [str(item) for item in (response.get("accepted_case_ids") or []) if str(item)]
    selected = [case_id for case_id in raw if case_id in available_case_ids]
    return sorted(set(selected), key=_case_id_sort_key)


def _case_ids_for_group(group: GroupSummary) -> Set[str]:
    return {str(case_id) for case_id in (group.case_ids or []) if str(case_id)}


def _primary_repair_loci(group: GroupSummary) -> Set[str]:
    loci = {
        str(row[1]).upper()
        for row in _program_core_signature(group)
        if len(row) > 1 and str(row[1]).strip()
    }
    if loci:
        return loci
    structural = group.core_interface.repair_skeleton_prototype.structural
    locus = str(getattr(structural.locus, "value", structural.locus) or "").upper()
    return {locus} if locus else set()


def _pair_supports_root_membership(
    pair: PairScore,
    *,
    left: GroupSummary,
    right: GroupSummary,
) -> bool:
    """Whether a pair is enough to keep a case inside a root-pattern review.

    This is not a merge decision.  It only says the pair shares enough
    case-derived root evidence that executable differences should be reviewed as
    possible branches instead of silently dropping the member.
    """

    if _is_absolute_conflict(pair.veto_reason):
        return False
    if not pair.broad_retrieval_reasons:
        return False
    relation = str(pair.semantic_relation or "")
    if relation == "compatible":
        return True
    if relation != "partial":
        return False
    strong_reasons = {
        "shared_primary_repair_locus",
        "shared_root_effect_axis_with_same_target_invariant_family",
    }
    return bool(strong_reasons & {str(item) for item in pair.broad_retrieval_reasons})


def _root_membership_closure(
    *,
    groups: Sequence[GroupSummary],
    pair_scores: Dict[Tuple[str, str], PairScore],
    accepted_case_ids: Sequence[str],
    excluded_case_ids: Sequence[str],
) -> Dict[str, Any]:
    """Close over recalled root-compatible members before branch admission.

    LLM admission is allowed to identify a clean root subset, but omitted
    component members must be made explicit.  Cases with root-compatible pair
    evidence to an accepted seed are added as branch-unassigned root members;
    explicit LLM exclusions are preserved as rejected unless a later LLM run
    admits them.
    """

    accepted: Set[str] = {str(case_id) for case_id in accepted_case_ids if str(case_id)}
    excluded: Set[str] = {str(case_id) for case_id in excluded_case_ids if str(case_id)}
    all_case_ids = {
        str(case_id)
        for group in groups
        for case_id in (group.case_ids or [])
        if str(case_id)
    }
    membership_status: Dict[str, Dict[str, Any]] = {
        case_id: {
            "status": "accepted_root_by_judge"
            if case_id in accepted
            else "rejected_root_by_judge"
            if case_id in excluded
            else "retrieved_but_not_admitted",
            "evidence_pair_ids": [],
            "reason": "",
        }
        for case_id in sorted(all_case_ids, key=_case_id_sort_key)
    }
    if len(accepted) < 2:
        return {
            "accepted_case_ids": sorted(accepted, key=_case_id_sort_key),
            "added_case_ids": [],
            "membership_status_by_case": membership_status,
            "not_closed_case_ids": sorted(all_case_ids - accepted, key=_case_id_sort_key),
            "closure_reason": "below_min_judge_accepted_seed",
        }

    added: Set[str] = set()
    not_closed: Set[str] = set()
    changed = True
    while changed:
        changed = False
        accepted_seed_groups = [
            group for group in groups if _case_ids_for_group(group) & accepted
        ]
        for group in groups:
            group_case_ids = _case_ids_for_group(group)
            if group_case_ids & accepted:
                continue
            if group_case_ids <= excluded:
                not_closed |= group_case_ids
                continue
            supporting_pairs: List[PairScore] = []
            hard_conflicts: List[str] = []
            for seed in accepted_seed_groups:
                key = _pair_key(group, seed)
                pair = pair_scores.get(key)
                if pair is None:
                    continue
                if _is_absolute_conflict(pair.veto_reason):
                    hard_conflicts.append(str(pair.veto_reason))
                    continue
                if _pair_supports_root_membership(pair, left=group, right=seed):
                    supporting_pairs.append(pair)
            if supporting_pairs:
                accepted |= group_case_ids
                added |= group_case_ids
                not_closed -= group_case_ids
                changed = True
                for case_id in group_case_ids:
                    membership_status[case_id] = {
                        "status": "accepted_root_by_mechanical_branch_closure",
                        "evidence_pair_ids": [
                            f"{pair.left_group_id}::{pair.right_group_id}"
                            for pair in supporting_pairs[:6]
                        ],
                        "reason": (
                            "component member has case-derived root evidence to an "
                            "accepted seed; executable differences remain branch evidence"
                        ),
                    }
            else:
                not_closed |= group_case_ids
                for case_id in group_case_ids:
                    if case_id in membership_status and membership_status[case_id]["status"] != "rejected_root_by_judge":
                        membership_status[case_id] = {
                            "status": "retrieved_but_not_root_closed",
                            "evidence_pair_ids": [],
                            "reason": ";".join(sorted(set(hard_conflicts))) or "no_root_membership_pair_to_accepted_seed",
                        }

    return {
        "accepted_case_ids": sorted(accepted, key=_case_id_sort_key),
        "added_case_ids": sorted(added, key=_case_id_sort_key),
        "membership_status_by_case": membership_status,
        "not_closed_case_ids": sorted(not_closed - accepted, key=_case_id_sort_key),
        "closure_reason": "root_membership_pair_evidence",
    }


def _llm_membership_by_case(
    response: Dict[str, Any],
    available_case_ids: Set[str],
) -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    allowed = {str(case_id) for case_id in available_case_ids}
    for item in response.get("membership_by_case") or []:
        payload = _model_dump(item)
        case_id = str(payload.get("case_id") or "")
        if not case_id or case_id not in allowed:
            continue
        rows[case_id] = {
            "judge_status": str(payload.get("status") or ""),
            "judge_reason": str(payload.get("reason") or ""),
        }
    return rows


def _merge_membership_audit(
    closure_status: Dict[str, Dict[str, Any]],
    judge_status: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for case_id, status in closure_status.items():
        row = dict(status)
        if case_id in judge_status:
            row.update(judge_status[case_id])
        else:
            row["judge_status"] = row.get("judge_status", "")
            row["judge_reason"] = row.get("judge_reason", "")
        merged[case_id] = row
    return merged


def _branch_specs_covering_cases(
    *,
    response: Dict[str, Any],
    admitted_groups: Sequence[GroupSummary],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Ensure every root-admitted member has an explicit branch assignment.

    Branch specs here are admission-time audit contracts.  Runtime branches are
    still synthesized from extracted repair programs and replay-gated later.
    """

    admitted_case_ids = {
        str(case_id)
        for group in admitted_groups
        for case_id in (group.case_ids or [])
        if str(case_id)
    }
    branch_specs = [
        _model_dump(spec) for spec in (response.get("branch_specs") or []) if _model_dump(spec)
    ]
    covered_case_ids = {
        str(case_id)
        for spec in branch_specs
        for case_id in (spec.get("case_ids") or [])
        if str(case_id)
    }
    missing = admitted_case_ids - covered_case_ids
    if not missing:
        return branch_specs, []

    added: List[str] = []
    for signature, bucket in _core_signature_buckets(admitted_groups).items():
        bucket_case_ids = sorted(
            {
                str(case_id)
                for group in bucket
                for case_id in (group.case_ids or [])
                if str(case_id)
            },
            key=_case_id_sort_key,
        )
        if not (set(bucket_case_ids) & missing):
            continue
        digest = hashlib.sha1(
            _canonical_payload([list(item) for item in signature]).encode("utf-8")
        ).hexdigest()[:8]
        branch_specs.append(
            {
                "branch_id": f"mechanical_branch_{digest}",
                "case_ids": bucket_case_ids,
                "required_interface_delta": (
                    "branch derived from extracted repair effect/core package; "
                    "runtime must select by branch signals and compiler binding"
                ),
                "selection_signal": (
                    "answer-blind source_state/branch required signals plus "
                    "schema-legal compiler binding"
                ),
                "origin": "mechanical_branch_coverage",
                "core_program_signature": [list(item) for item in signature],
            }
        )
        added.extend(case_id for case_id in bucket_case_ids if case_id in missing)
    return branch_specs, sorted(set(added), key=_case_id_sort_key)


def _branch_spec_required_signals(
    *,
    branch_groups: Sequence[GroupSummary],
) -> List[str]:
    if not branch_groups:
        return []
    signal_sets = []
    for group in branch_groups:
        signals = {
            signal
            for signal in _manifest_tags(group)
            if str(signal).startswith("pred.")
        }
        if signals:
            signal_sets.append(signals)
    if not signal_sets:
        return []
    common = set.intersection(*signal_sets)
    return sorted(common)


def _merge_branch_rows_for_admission_spec(
    *,
    spec: Dict[str, Any],
    branch_groups: Sequence[GroupSummary],
    existing_rows: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    case_ids = sorted(
        {str(case_id) for case_id in (spec.get("case_ids") or []) if str(case_id)},
        key=_case_id_sort_key,
    )
    case_id_set = set(case_ids)
    matched = [
        dict(row)
        for row in existing_rows
        if case_id_set
        and (
            case_id_set
            & {str(item) for item in (row.get("support_case_ids") or []) if str(item)}
        )
    ]
    if not matched and len(existing_rows) == 1:
        matched = [dict(existing_rows[0])]

    digest_payload = {
        "branch_id": spec.get("branch_id"),
        "case_ids": case_ids,
        "core_program_signature": spec.get("core_program_signature") or [],
    }
    branch_id = str(spec.get("branch_id") or "").strip() or (
        "adm-br:" + hashlib.sha1(_canonical_payload(digest_payload).encode("utf-8")).hexdigest()[:12]
    )
    row: Dict[str, Any] = {
        "schema_version": "runtime-branch-contract-v1",
        "branch_id": branch_id,
        "support_case_ids": case_ids,
        "admission_branch_spec": spec,
        "admission_origin": str(spec.get("origin") or "pattern_admission"),
        "runtime_usable": False,
        "runtime_blockers": ["branch_not_replay_validated"],
        "replay_metrics": {},
    }
    if not matched:
        row.update(
            {
                "bundle_ids": [],
                "bundled_op_ids": [],
                "cleanup_op_ids": [],
                "required_signals": _branch_spec_required_signals(
                    branch_groups=branch_groups
                ),
                "negative_signals": [],
                "required_role_slots": [],
                "allowed_primitives": [],
                "allowed_edit_scope": [],
                "preserve_constraints": list(spec.get("preserve_constraints") or []),
                "source_antipatterns": [],
                "target_effects": [],
                "target_invariants": [],
                "negative_guards": [
                    {
                        "kind": "admission_branch_without_executable_bundle",
                        "branch_id": branch_id,
                    }
                ],
                "effect_kind": "",
                "lowering_family": "",
                "runtime_blockers": ["admission_branch_no_executable_bundle"],
            }
        )
        return row

    def merge_list(key: str) -> List[Any]:
        out: List[Any] = []
        seen: Set[str] = set()
        for source in [*matched, spec]:
            for item in source.get(key) or []:
                marker = _canonical_payload(item)
                if marker in seen:
                    continue
                seen.add(marker)
                out.append(item)
        return out

    required = set(merge_list("required_signals"))
    required.update(_branch_spec_required_signals(branch_groups=branch_groups))
    row.update(
        {
            "bundle_ids": [str(item) for item in merge_list("bundle_ids") if str(item)],
            "bundled_op_ids": [
                str(item) for item in merge_list("bundled_op_ids") if str(item)
            ],
            "cleanup_op_ids": [
                str(item) for item in merge_list("cleanup_op_ids") if str(item)
            ],
            "required_signals": sorted(required),
            "negative_signals": [str(item) for item in merge_list("negative_signals") if str(item)],
            "required_role_slots": merge_list("required_role_slots"),
            "allowed_primitives": [
                str(item) for item in merge_list("allowed_primitives") if str(item)
            ],
            "allowed_edit_scope": [
                str(item) for item in merge_list("allowed_edit_scope") if str(item)
            ],
            "preserve_constraints": merge_list("preserve_constraints"),
            "source_antipatterns": merge_list("source_antipatterns"),
            "target_effects": merge_list("target_effects"),
            "target_invariants": merge_list("target_invariants"),
            "negative_guards": merge_list("negative_guards"),
            "effect_kind": ",".join(
                sorted({str(item.get("effect_kind") or "") for item in matched if item.get("effect_kind")})
            ),
            "lowering_family": ",".join(
                sorted(
                    {
                        str(item.get("lowering_family") or "")
                        for item in matched
                        if item.get("lowering_family")
                    }
                )
            ),
        }
    )
    if any(str(item) == "REROUTE_FACT" for item in row.get("allowed_primitives") or []):
        constraints = list(row.get("preserve_constraints") or [])
        if "answer_unit_preserve" not in constraints:
            constraints.append("answer_unit_preserve")
        row["preserve_constraints"] = constraints
    return row


def _materialize_admission_branches(pattern: GroupSummary, groups: Sequence[GroupSummary]) -> GroupSummary:
    admission = dict((pattern.formation_signals or {}).get("pattern_admission") or {})
    branch_specs = [
        _model_dump(spec) for spec in (admission.get("branch_specs") or []) if _model_dump(spec)
    ]
    if not branch_specs:
        return pattern
    program = getattr(pattern.instantiation_program, "synthesized_program", None)
    if program is None or getattr(program, "program_envelope", None) is None:
        return pattern
    envelope_obj = program.program_envelope
    envelope_payload = _model_dump(envelope_obj)
    existing_rows = [
        _model_dump(row)
        for row in (envelope_payload.get("runtime_branches") or [])
        if _model_dump(row)
    ]
    group_by_case: Dict[str, List[GroupSummary]] = defaultdict(list)
    for group in groups:
        for case_id in group.case_ids or []:
            group_by_case[str(case_id)].append(group)
    runtime_rows: List[Dict[str, Any]] = []
    for spec in branch_specs:
        branch_case_ids = [str(case_id) for case_id in (spec.get("case_ids") or []) if str(case_id)]
        branch_groups = [
            group
            for case_id in branch_case_ids
            for group in group_by_case.get(case_id, [])
        ]
        runtime_rows.append(
            _merge_branch_rows_for_admission_spec(
                spec=spec,
                branch_groups=branch_groups,
                existing_rows=existing_rows,
            )
        )
    envelope_payload["runtime_branches"] = runtime_rows
    envelope_payload["branch_selection_contract"] = {
        **_model_dump(envelope_payload.get("branch_selection_contract") or {}),
        "selection_unit": "runtime_branch",
        "requires_current_variant_binding": True,
        "admission_branch_materialized": True,
    }
    updated_envelope = envelope_obj.model_copy(update=envelope_payload)
    updated_program = program.model_copy(update={"program_envelope": updated_envelope})
    updated_instantiation = pattern.instantiation_program.model_copy(
        update={"synthesized_program": updated_program}
    )
    pattern = pattern.model_copy(
        update={"instantiation_program": updated_instantiation}
    )
    trigger_contract = _model_dump(getattr(pattern, "trigger_contract", None))
    action_contract = _model_dump(trigger_contract.get("action_contract") or {})
    action_contract["program_envelope"] = envelope_payload
    trigger_contract["action_contract"] = action_contract
    if hasattr(pattern.trigger_contract, "model_copy"):
        pattern = pattern.model_copy(
            update={
                "trigger_contract": pattern.trigger_contract.model_copy(
                    update=trigger_contract
                )
            }
        )
    return pattern


def _sync_trigger_contract_from_envelope_and_admission(group: GroupSummary) -> GroupSummary:
    program = getattr(group.instantiation_program, "synthesized_program", None)
    envelope_obj = getattr(program, "program_envelope", None) if program is not None else None
    envelope = _model_dump(envelope_obj)
    if not envelope:
        return group
    runtime_branches = [
        _model_dump(branch)
        for branch in (envelope.get("runtime_branches") or [])
        if _model_dump(branch)
    ]
    trigger_contract = _model_dump(getattr(group, "trigger_contract", None))
    if runtime_branches:
        trigger_contract["runtime_branches"] = runtime_branches
        trigger_contract["required_signals"] = sorted(
            {
                str(signal)
                for branch in runtime_branches
                for signal in (branch.get("required_signals") or [])
                if str(signal)
            }
        )
    action_contract = _model_dump(trigger_contract.get("action_contract") or {})
    action_contract["program_envelope"] = envelope
    ops = list(getattr(program, "ops", []) or [])
    main_op = next(
        (
            _model_dump(op)
            for op in ops
            if not bool(_model_dump(op).get("is_dependency") or False)
        ),
        _model_dump(ops[0]) if ops else {},
    )
    locus = str(main_op.get("locus") or "").upper()
    op_type = str(main_op.get("op_type") or "").upper()
    if locus:
        action_contract["locus"] = locus
    if "DROP" in op_type:
        action_contract["op_family"] = "drop"
    elif "ADD" in op_type:
        action_contract["op_family"] = "add"
    elif "REPLACE" in op_type or "SWITCH" in op_type:
        action_contract["op_family"] = "replace"
    elif "REROUTE" in op_type:
        action_contract["op_family"] = "reroute"
    trigger_contract["action_contract"] = action_contract
    brc = getattr(group.instantiation_program, "bias_recognition_contract", None)
    if brc is not None:
        brc_payload = brc.model_dump(mode="json") if hasattr(brc, "model_dump") else _model_dump(brc)
        trigger_contract["canonical_discriminants"] = list(
            brc_payload.get("recognition_signals") or []
        )
    return group.model_copy(
        update={
            "trigger_contract": TriggerContract.model_validate(trigger_contract)
        }
    )


def _build_pattern_candidate(
    groups: Sequence[GroupSummary],
    pair_scores: Dict[Tuple[str, str], PairScore],
    *,
    admission_response: Dict[str, Any],
) -> GroupSummary:
    pattern = _build_family(
        groups,
        pair_scores,
        runtime_usable=False,
        require_effect_program=True,
        group_type=GroupType.PATTERN,
        formation_version="offline-branching-pattern-admission-v1",
        promotion_state="offline_pattern_candidate",
        pattern_admission=admission_response,
    )
    pattern.runtime_usable = False
    pattern.instantiation_program.shared_status = (
        "pattern_admission_only"
        if pattern.instantiation_program.synthesized_program is None
        else pattern.instantiation_program.shared_status
    )
    pattern = _materialize_admission_branches(pattern, groups)
    brc_payload = _validated_bias_recognition_contract_payload(admission_response)
    brc = (
        BiasRecognitionContract.model_validate(brc_payload)
        if brc_payload
        else _fallback_bias_recognition_contract(groups)
    )
    if brc is not None:
        pattern = pattern.model_copy(
            update={
                "instantiation_program": pattern.instantiation_program.model_copy(
                    update={"bias_recognition_contract": brc}
                )
            }
        )
    pattern = _sync_trigger_contract_from_envelope_and_admission(pattern)
    return pattern


def _group_effect_candidate_count(group: GroupSummary) -> int:
    signals = _signal_payload(group)
    ir = dict((signals.get("canonical_repair_ir") or {}) or {})
    repair_effect_signature = _model_dump(ir.get("repair_effect_signature") or {})
    return len(
        [
            item
            for item in (repair_effect_signature.get("effect_candidates") or [])
            if _model_dump(item).get("axis")
        ]
    )


def _slicer_candidate_case_sets(
    response: Dict[str, Any],
    available_case_ids: Set[str],
) -> List[Tuple[List[str], Dict[str, Any]]]:
    out: List[Tuple[List[str], Dict[str, Any]]] = []
    seen: Set[Tuple[str, ...]] = set()
    for index, raw_group in enumerate(response.get("candidate_groups") or []):
        payload = _model_dump(raw_group)
        case_ids = [
            str(case_id)
            for case_id in (payload.get("case_ids") or [])
            if str(case_id) in available_case_ids
        ]
        case_ids = sorted(set(case_ids), key=_case_id_sort_key)
        key = tuple(case_ids)
        if len(case_ids) < 2 or key in seen:
            continue
        seen.add(key)
        if not payload.get("candidate_id"):
            payload["candidate_id"] = f"insight_candidate_{index + 1}"
        out.append((case_ids, payload))
    return out


def _groups_for_case_ids(
    groups: Sequence[GroupSummary],
    case_ids: Sequence[str],
) -> List[GroupSummary]:
    selected = set(str(case_id) for case_id in case_ids)
    return [
        group
        for group in groups
        if {str(case_id) for case_id in group.case_ids or []} & selected
    ]


def _core_signature_branch_coverage(
    groups: Sequence[GroupSummary],
    response: Dict[str, Any],
) -> Dict[str, Any]:
    buckets = _core_signature_buckets(groups)
    admitted_case_ids = {
        str(case_id) for group in groups for case_id in group.case_ids or []
    }
    branch_specs = [
        _model_dump(spec) for spec in (response.get("branch_specs") or [])
    ]
    branch_case_ids = {
        str(case_id)
        for spec in branch_specs
        for case_id in (spec.get("case_ids") or [])
        if str(case_id)
    }
    has_conflict = len(buckets) > 1
    covered = (
        not has_conflict
        or (
            bool(response.get("branch_axes"))
            and bool(branch_specs)
            and admitted_case_ids <= branch_case_ids
        )
    )
    return {
        "has_core_signature_conflict": has_conflict,
        "covered": covered,
        "unique_core_signature_count": len(buckets),
        "branch_specs_cover_admitted_cases": admitted_case_ids <= branch_case_ids
        if branch_specs
        else False,
        "missing_branch_case_ids": sorted(
            admitted_case_ids - branch_case_ids,
            key=_case_id_sort_key,
        )
        if has_conflict
        else [],
        "core_program_signature_buckets": [
            {
                "core_program_signature": [list(item) for item in signature],
                "case_ids": sorted(
                    {
                        str(case_id)
                        for group in bucket
                        for case_id in group.case_ids or []
                    },
                    key=_case_id_sort_key,
                ),
            }
            for signature, bucket in buckets.items()
        ],
    }


def _build_pattern_admission_candidates(
    active_singletons: Sequence[GroupSummary],
    pair_scores: Dict[Tuple[str, str], PairScore],
    by_id: Dict[str, GroupSummary],
) -> Tuple[List[GroupSummary], List[Dict[str, Any]]]:
    candidate_edges = [
        pair for pair in pair_scores.values() if pair.branchable_for_pattern
    ]
    reports: List[Dict[str, Any]] = []
    patterns: List[GroupSummary] = []
    if not candidate_edges:
        return patterns, reports

    uf = _UnionFind([group.group_id for group in active_singletons])
    for edge in candidate_edges:
        uf.union(edge.left_group_id, edge.right_group_id)

    seen_case_sets: Set[Tuple[str, ...]] = set()
    for component in uf.components():
        if len(component) < 2:
            continue
        component_groups_all = [by_id[group_id] for group_id in component if group_id in by_id]
        component_pair_supplement_count = _ensure_pair_scores_for_groups(
            component_groups_all,
            pair_scores,
        )
        member_pair_supplement_count = component_pair_supplement_count
        component_pair_scores = _member_pair_scores(component_groups_all, pair_scores)
        available_component_case_ids = {
            str(case_id)
            for group in component_groups_all
            for case_id in group.case_ids or []
        }
        component_ids = [group.group_id for group in component_groups_all]
        try:
            response = _call_pattern_admission_judge(
                groups=component_groups_all,
                member_pair_scores=component_pair_scores,
                slicer_candidate=None,
            )
        except Exception as exc:
            reports.append(
                {
                    "component_group_ids": component_ids,
                    "component_case_ids": sorted(
                        available_component_case_ids,
                        key=_case_id_sort_key,
                    ),
                    "admitted": False,
                    "reason": f"pattern_admission_judge_error:{type(exc).__name__}",
                    "error": str(exc),
                    "formation_pair_scope": {
                        "scope": "component_all_pairs",
                        "component_pair_count": len(component_pair_scores),
                        "supplemented_pair_count": component_pair_supplement_count,
                    },
                    "pair_decision_context": _pair_decision_context(component_pair_scores),
                }
            )
            continue

        available_case_ids = set(available_component_case_ids)
        response = dict(response)
        accepted_before_closure = _pattern_admitted_case_ids(response, available_case_ids)
        excluded_before_closure = [
            str(item)
            for item in (response.get("excluded_case_ids") or [])
            if str(item) in available_case_ids
        ]
        judge_membership = _llm_membership_by_case(response, available_case_ids)
        closure = _root_membership_closure(
            groups=component_groups_all,
            pair_scores=pair_scores,
            accepted_case_ids=accepted_before_closure,
            excluded_case_ids=excluded_before_closure,
        )
        membership_status_by_case = _merge_membership_audit(
            closure["membership_status_by_case"],
            judge_membership,
        )
        admitted_case_ids = list(closure["accepted_case_ids"])
        response["accepted_case_ids_before_branch_closure"] = accepted_before_closure
        response["accepted_case_ids"] = admitted_case_ids
        response["excluded_case_ids_before_branch_closure"] = excluded_before_closure
        response["mechanical_branch_closure_added_case_ids"] = list(
            closure["added_case_ids"]
        )
        response["root_membership_status_by_case"] = membership_status_by_case
        response["retrieved_but_not_admitted_case_ids"] = list(
            closure["not_closed_case_ids"]
        )
        response["mechanical_branch_closure_override_reason"] = closure[
            "closure_reason"
        ]
        admitted_groups = [
            group
            for group in component_groups_all
            if _case_ids_for_group(group) & set(admitted_case_ids)
        ]
        branch_specs, mechanical_branch_case_ids = _branch_specs_covering_cases(
            response=response,
            admitted_groups=admitted_groups,
        )
        if branch_specs:
            response["branch_specs"] = branch_specs
        if mechanical_branch_case_ids:
            response["mechanical_branch_spec_added_case_ids"] = mechanical_branch_case_ids
            if not response.get("branch_axes"):
                response["branch_axes"] = [
                    {
                        "name": "mechanical_core_repair_branch",
                        "why_needed": (
                            "admitted root members expose finite executable "
                            "repair-package variation"
                        ),
                        "selection_signal": (
                            "runtime branch signals plus compiler binding; "
                            "gold SQL is not available at runtime"
                        ),
                        "origin": "mechanical_branch_coverage",
                    }
                ]
        case_key = tuple(admitted_case_ids)
        admitted = bool(response.get("admit_pattern")) and len(admitted_case_ids) >= 2
        admission_blocker = ""
        if bool(response.get("admit_pattern")) and not accepted_before_closure:
            admission_blocker = "missing_explicit_accepted_case_ids"
            response["reject_reason"] = (
                response.get("reject_reason")
                or "Pattern admission must explicitly list accepted_case_ids; "
                "code will not infer membership from the full candidate."
            )
        core_branch_coverage = (
            _core_signature_branch_coverage(admitted_groups, response)
            if admitted
            else {}
        )
        if core_branch_coverage:
            response["core_signature_branch_coverage"] = core_branch_coverage
        admitted_pair_supplement_count = 0
        if admitted and case_key not in seen_case_sets:
            admitted_pair_supplement_count = _ensure_pair_scores_for_groups(
                admitted_groups,
                pair_scores,
            )
            candidate_pattern = _build_pattern_candidate(
                admitted_groups,
                pair_scores,
                admission_response=response,
            )
            effect_backed = (
                candidate_pattern.formation_signals.get("synthesis_basis") == "effect"
                and bool(
                    _program_effect_candidates(
                        candidate_pattern.instantiation_program.synthesized_program
                    )
                )
            )
            member_effect_backed = all(
                _group_effect_candidate_count(group) > 0 for group in admitted_groups
            )
            if (
                core_branch_coverage.get("has_core_signature_conflict")
                and not core_branch_coverage.get("covered")
            ):
                admitted = False
                admission_blocker = "uncovered_core_branch"
                response["reject_reason"] = (
                    response.get("reject_reason")
                    or "Formal pattern admission found multiple core repair packages, "
                    "but branch_specs do not cover all admitted cases."
                )
                response["required_code_checks"] = [
                    *list(response.get("required_code_checks") or []),
                    "core repair package variations must be represented as branch_specs",
                ]
            elif core_branch_coverage.get("has_core_signature_conflict"):
                if not member_effect_backed:
                    admitted = False
                    admission_blocker = "missing_member_effect_evidence"
                    response["reject_reason"] = (
                        response.get("reject_reason")
                        or "Branched pattern admission requires each member to expose "
                        "at least one contrastive repair effect."
                    )
                else:
                    response["required_code_checks"] = [
                        *list(response.get("required_code_checks") or []),
                        "branch-specific effect-backed programs are required before runtime promotion",
                    ]
                    candidate_pattern.formation_signals["pattern_admission"] = response
                    seen_case_sets.add(case_key)
                    patterns.append(candidate_pattern)
            elif not effect_backed:
                admitted = False
                admission_blocker = "missing_effect_backed_shared_program"
                response["reject_reason"] = (
                    response.get("reject_reason")
                    or "Formal pattern admission requires an effect-backed shared program; "
                    "legacy exact-op agreement is not sufficient."
                )
                response["required_code_checks"] = [
                    *list(response.get("required_code_checks") or []),
                    "repair effect synthesis must produce a shared contrastive effect before runtime promotion",
                ]
            else:
                seen_case_sets.add(case_key)
                patterns.append(candidate_pattern)
        reports.append(
            {
                "component_group_ids": component_ids,
                "component_case_ids": sorted(available_case_ids, key=_case_id_sort_key),
                "admitted": admitted,
                "admission_blocker": admission_blocker,
                "admitted_case_ids": admitted_case_ids,
                "mechanical_branch_closure_added_case_ids": list(
                    closure["added_case_ids"]
                ),
                "mechanical_branch_spec_added_case_ids": response.get(
                    "mechanical_branch_spec_added_case_ids",
                    [],
                ),
                "excluded_case_ids": [
                    str(item)
                    for item in (response.get("excluded_case_ids") or [])
                    if str(item)
                ],
                "root_membership_status_by_case": membership_status_by_case,
                "retrieved_but_not_admitted_case_ids": list(
                    closure["not_closed_case_ids"]
                ),
                "stable_bias_key": response.get("stable_bias_key"),
                "primary_repair_interface": response.get("primary_repair_interface"),
                "branch_axes": response.get("branch_axes") or [],
                "branch_specs": response.get("branch_specs") or [],
                "negative_guards": response.get("negative_guards") or [],
                "required_code_checks": response.get("required_code_checks") or [],
                "reject_reason": response.get("reject_reason"),
                "formation_pair_scope": {
                    "scope": "component_root_first_all_pairs",
                    "component_pair_count": len(component_pair_scores),
                    "supplemented_pair_count": member_pair_supplement_count,
                    "admitted_pair_supplemented_count": (
                        admitted_pair_supplement_count if admitted else 0
                    ),
                },
                "judge_reject_reason_before_branch_closure": response.get(
                    "reject_reason"
                ),
                "excluded_case_ids_before_branch_closure": excluded_before_closure,
                "mechanical_branch_closure_override_reason": response.get(
                    "mechanical_branch_closure_override_reason"
                ),
                "core_signature_branch_coverage": core_branch_coverage,
                "rationale": response.get("rationale"),
                "pair_decision_context": _pair_decision_context(component_pair_scores),
            }
        )
    return patterns, reports


def _status_value(group: GroupSummary) -> str:
    status = getattr(group, "status", "")
    return str(status.value if hasattr(status, "value") else status)


def _manual_labels(manual_groups: Optional[Dict[str, Any]], db_id: str) -> Dict[str, str]:
    if not manual_groups:
        return {}
    db_payload = None
    if isinstance(manual_groups, list):
        for item in manual_groups:
            if isinstance(item, dict) and str(item.get("db_id")) == db_id:
                db_payload = item
                break
    elif isinstance(manual_groups, dict) and str(manual_groups.get("db_id")) == db_id:
        db_payload = manual_groups
    if not db_payload:
        return {}

    labels: Dict[str, str] = {}
    for section, prefix in (("patterns", "pattern"),):
        for index, group in enumerate(db_payload.get(section) or [], start=1):
            name = str(group.get("pattern_name") or group.get("family_name") or f"{prefix}_{index}")
            label = f"{prefix}:{name}"
            for case_id in group.get("case_ids") or []:
                labels[str(case_id)] = label
    for case_id in db_payload.get("singletons") or []:
        labels[str(case_id)] = f"singleton:{case_id}"
    return labels


def _pair_set(groups: Sequence[Sequence[str]]) -> Set[Tuple[str, str]]:
    pairs: Set[Tuple[str, str]] = set()
    for group in groups:
        values = sorted([str(value) for value in group], key=lambda value: int(value))
        for i in range(len(values)):
            for j in range(i + 1, len(values)):
                pairs.add((values[i], values[j]))
    return pairs


def _alignment_report(
    *,
    predicted_families: Sequence[GroupSummary],
    remaining_singletons: Sequence[GroupSummary],
    manual_labels: Dict[str, str],
) -> Dict[str, Any]:
    predicted_groups = [family.case_ids for family in predicted_families]
    predicted_pairs = _pair_set(predicted_groups)

    manual_group_to_cases: Dict[str, List[str]] = defaultdict(list)
    for case_id, label in manual_labels.items():
        if not label.startswith("singleton:"):
            manual_group_to_cases[label].append(case_id)
    manual_pairs = _pair_set(manual_group_to_cases.values())

    true_positive = len(predicted_pairs & manual_pairs)
    false_positive = len(predicted_pairs - manual_pairs)
    false_negative = len(manual_pairs - predicted_pairs)
    precision = true_positive / (true_positive + false_positive) if (true_positive + false_positive) else 0.0
    recall = true_positive / (true_positive + false_negative) if (true_positive + false_negative) else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall)
        else 0.0
    )

    purity_rows: List[Dict[str, Any]] = []
    for family in predicted_families:
        labels = [manual_labels.get(case_id, "unlabeled") for case_id in family.case_ids]
        counts = Counter(labels)
        majority_label, majority_count = counts.most_common(1)[0]
        purity_rows.append(
            {
                "group_id": family.group_id,
                "case_ids": list(family.case_ids),
                "manual_label_counts": dict(sorted(counts.items())),
                "majority_label": majority_label,
                "purity": majority_count / len(family.case_ids) if family.case_ids else 0.0,
            }
        )

    predicted_case_to_group: Dict[str, str] = {}
    for family in predicted_families:
        for case_id in family.case_ids:
            predicted_case_to_group[str(case_id)] = family.group_id
    for singleton in remaining_singletons:
        for case_id in singleton.case_ids:
            predicted_case_to_group[str(case_id)] = singleton.group_id

    fragmentation_rows: List[Dict[str, Any]] = []
    for label, case_ids in sorted(manual_group_to_cases.items()):
        present = [case_id for case_id in case_ids if case_id in predicted_case_to_group]
        predicted_buckets = sorted({predicted_case_to_group[case_id] for case_id in present})
        fragmentation_rows.append(
            {
                "manual_label": label,
                "manual_case_ids": sorted(case_ids, key=lambda value: int(value)),
                "covered_case_ids": sorted(present, key=lambda value: int(value)),
                "predicted_group_count": len(predicted_buckets),
                "predicted_groups": predicted_buckets,
            }
        )

    return {
        "pairwise": {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        },
        "purity": purity_rows,
        "fragmentation": fragmentation_rows,
    }


def _effect_axes_from_program(program: Any) -> List[str]:
    return sorted(
        {
            str(row.get("axis"))
            for row in _program_effect_candidates(program)
            if str(row.get("axis") or "")
        }
    )


def _failure_taxonomy_counts(pairs: Sequence[PairScore]) -> Dict[str, int]:
    counts: Counter[str] = Counter()
    for pair in pairs:
        for label in pair.failure_taxonomy:
            counts[str(label)] += 1
    return dict(sorted(counts.items()))


def _library_invariant_report(library: LibraryStateV2) -> Dict[str, Any]:
    active_groups = [
        *list(library.patterns or []),
        *list(library.singletons or []),
    ]
    case_locations: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for group in active_groups:
        if _status_value(group) != str(GroupStatus.ACTIVE.value):
            continue
        for case_id in group.case_ids or []:
            case_locations[str(case_id)].append(
                {
                    "group_id": group.group_id,
                    "group_type": str(group.group_type.value if hasattr(group.group_type, "value") else group.group_type),
                    "runtime_usable": bool(group.runtime_usable),
                }
            )
    duplicate_active_case_memberships = {
        case_id: locations
        for case_id, locations in case_locations.items()
        if sum(1 for location in locations if bool(location["runtime_usable"])) > 1
    }
    return {
        "duplicate_runtime_case_memberships": duplicate_active_case_memberships,
        "offline_pattern_singleton_overlap_allowed": True,
        "passed": not duplicate_active_case_memberships,
    }


def form_offline_families(
    library: LibraryStateV2,
    *,
    manual_groups: Optional[Dict[str, Any]] = None,
    max_neighbor_edges: int = LOCAL_REORGANIZE_NEIGHBOR_MAX,
    mark_runtime_usable: bool = False,
    focus_case_ids: Optional[Set[str]] = None,
) -> Tuple[LibraryStateV2, Dict[str, Any]]:
    """Return a new library with strict pattern candidates plus a report."""
    if mark_runtime_usable:
        raise ValueError(
            "Direct runtime marking is disabled; use replay-gated pattern promotion."
        )
    focus_case_ids = {str(case_id) for case_id in (focus_case_ids or set())}
    active_singletons = [
        group
        for group in library.singletons
        if group.group_type == GroupType.SINGLETON and group.status == GroupStatus.ACTIVE
    ]
    # ``focus_case_ids`` identifies the newly arrived case(s), but formation
    # must still compare against the whole active singleton memory.  Otherwise
    # online local-evolve degenerates to one-item clustering and can never form
    # a pattern during a serial run.
    passthrough_active_singletons: List[GroupSummary] = []
    archived_singletons = [
        group
        for group in library.singletons
        if group.group_type != GroupType.SINGLETON or group.status != GroupStatus.ACTIVE
    ]
    by_id = {group.group_id: group for group in active_singletons}

    pair_scores: Dict[Tuple[str, str], PairScore] = {}
    accepted_edges: List[PairScore] = []
    rejected_edges: List[PairScore] = []
    candidate_keys = _candidate_pair_keys(
        active_singletons,
        focus_case_ids=focus_case_ids if focus_case_ids else None,
    )
    for left_id, right_id in candidate_keys:
        left = by_id.get(left_id)
        right = by_id.get(right_id)
        if left is None or right is None:
            continue
        pair = score_pair(left, right)
        key = tuple(sorted((left.group_id, right.group_id)))
        pair_scores[key] = pair
        if pair.accepted:
            accepted_edges.append(pair)
        else:
            rejected_edges.append(pair)

    retrieval_audit = _retrieval_audit(
        active_singletons,
        focus_case_ids=focus_case_ids if focus_case_ids else None,
        candidate_keys=candidate_keys,
        pair_scores=pair_scores,
    )
    retrieval_scored_pair_count = len(pair_scores)
    accepted_edges = sorted(accepted_edges, key=lambda pair: pair.score, reverse=True)
    pattern_candidates, pattern_admission_reports = _build_pattern_admission_candidates(
        active_singletons,
        pair_scores,
        by_id,
    )
    pattern_candidates = sorted(pattern_candidates, key=lambda group: int(group.case_ids[0]))
    # Strict patterns remain offline candidates until replay promotion.  Source
    # singletons stay active so online accumulation can still benefit from
    # single-case exact memory when a pattern has not yet passed replay.
    remaining_singletons = sorted(active_singletons, key=lambda group: int(group.case_ids[0]))
    output_singletons = sorted(
        [*archived_singletons, *passthrough_active_singletons, *remaining_singletons],
        key=lambda group: int(group.case_ids[0]) if group.case_ids and str(group.case_ids[0]).isdigit() else 0,
    )
    output_library = LibraryStateV2(
        db_id=library.db_id,
        patterns=_dedupe_patterns([*list(library.patterns), *pattern_candidates]),
        experience_families=[],
        singletons=output_singletons,
        cases_processed=library.cases_processed,
    )

    labels = _manual_labels(manual_groups, library.db_id)
    family_reports: List[Dict[str, Any]] = []
    complete_pair_scores = list(pair_scores.values())
    complete_accepted_edges = sorted(
        [pair for pair in complete_pair_scores if pair.accepted],
        key=lambda pair: pair.score,
        reverse=True,
    )
    complete_rejected_edges = sorted(
        [pair for pair in complete_pair_scores if not pair.accepted],
        key=lambda pair: pair.score,
        reverse=True,
    )

    pattern_reports = []
    for pattern in pattern_candidates:
        member_groups = [
            group for group in active_singletons if set(group.case_ids) <= set(pattern.case_ids)
        ]
        pair_keys = [
            tuple(sorted((left.group_id, right.group_id)))
            for idx, left in enumerate(member_groups)
            for right in member_groups[idx + 1 :]
        ]
        member_scores = [pair_scores[key] for key in pair_keys if key in pair_scores]
        manual_counts = Counter(labels.get(case_id, "unlabeled") for case_id in pattern.case_ids)
        admission = dict((pattern.formation_signals or {}).get("pattern_admission") or {})
        pattern_reports.append(
            {
                "group_id": pattern.group_id,
                "case_ids": list(pattern.case_ids),
                "support": pattern.support,
                "runtime_usable": pattern.runtime_usable,
                "promotion_state": pattern.lifecycle.promotion_state,
                "stable_bias_key": admission.get("stable_bias_key"),
                "primary_repair_interface": admission.get("primary_repair_interface"),
                "branch_axes": admission.get("branch_axes") or [],
                "branch_specs": admission.get("branch_specs") or [],
                "negative_guards": admission.get("negative_guards") or [],
                "required_code_checks": admission.get("required_code_checks") or [],
                "reject_reason": admission.get("reject_reason"),
                "judge_reject_reason_before_branch_closure": admission.get(
                    "judge_reject_reason_before_branch_closure"
                ),
                "excluded_case_ids_before_branch_closure": admission.get(
                    "excluded_case_ids_before_branch_closure",
                    [],
                ),
                "mechanical_branch_closure_override_reason": admission.get(
                    "mechanical_branch_closure_override_reason"
                ),
                "core_signature_branch_coverage": admission.get(
                    "core_signature_branch_coverage"
                ),
                "rationale": admission.get("rationale"),
                "accepted_case_ids_before_branch_closure": admission.get(
                    "accepted_case_ids_before_branch_closure",
                    list(pattern.case_ids),
                ),
                "synthesized_program": _model_dump(
                    pattern.instantiation_program.synthesized_program
                ),
                "effect_axes": _effect_axes_from_program(
                    pattern.instantiation_program.synthesized_program
                ),
                "effect_signature_count": len(
                    _program_effect_candidates(
                        pattern.instantiation_program.synthesized_program
                    )
                ),
                "synthesis_basis": pattern.formation_signals.get("synthesis_basis"),
                "program_coverage": _model_dump(
                    pattern.instantiation_program.program_coverage
                ),
                "manual_label_counts": dict(sorted(manual_counts.items())),
                "pair_semantic_relation_counts": dict(
                    Counter(pair.semantic_relation for pair in member_scores)
                ),
                "mechanical_branch_closure_added_case_ids": admission.get(
                    "mechanical_branch_closure_added_case_ids",
                    [],
                ),
                "pair_decision_context": _pair_decision_context(member_scores),
                "review_status": "needs_human_review",
            }
        )

    report = {
        "source_db_id": library.db_id,
        "input_counts": {
            "patterns": len(library.patterns),
            "experience_families": len(library.experience_families),
            "singletons": len(library.singletons),
            "active_singletons": len(active_singletons),
            "passthrough_active_singletons": len(passthrough_active_singletons),
            "archived_singletons": len(archived_singletons),
            "focus_case_ids": sorted(focus_case_ids),
            "candidate_pair_count": len(candidate_keys),
            "scored_pair_count": len(pair_scores),
            "retrieval_scored_pair_count": retrieval_scored_pair_count,
            "formation_supplemented_pair_count": max(
                0,
                len(pair_scores) - retrieval_scored_pair_count,
            ),
        },
        "output_counts": {
            "patterns": len(output_library.patterns),
            "new_pattern_candidates": len(pattern_candidates),
            "experience_families": len(output_library.experience_families),
            "new_experience_families": 0,
            "absorbed_experience_families": 0,
            "families_shadowed_by_patterns": 0,
            "singletons_shadowed_by_patterns": 0,
            "remaining_singletons": len(remaining_singletons),
            "archived_singletons": len(archived_singletons),
        },
        "thresholds": {
            "legacy_overlap_decision_signals": "disabled",
            "max_neighbor_edges": max_neighbor_edges,
            "online_max_candidates_per_focus": ONLINE_EVOLUTION_MAX_CANDIDATES_PER_FOCUS,
            "accepted_policy": (
                "strict_singleton_to_pattern_only; case-local insight, "
                "effect candidates, and canonical repair programs are required"
            ),
        },
        "patterns": pattern_reports,
        "pattern_admission_candidates": pattern_admission_reports,
        "stable_bias_frames": [],
        "insight_slicer_candidates": [],
        "pattern_candidate_generation_policy": (
            "root_first_pattern_admission; core_program_signature/action differences "
            "are branch evidence after root admission; insight slicer is disabled"
        ),
        "retrieval_audit": retrieval_audit,
        "formation_audit": {
            "pair_scope": "retrieval_plus_component_all_pairs",
            "retrieval_scored_pair_count": retrieval_scored_pair_count,
            "complete_pair_count": len(pair_scores),
            "supplemented_pair_count": max(
                0,
                len(pair_scores) - retrieval_scored_pair_count,
            ),
            "accepted_pair_count": len(complete_accepted_edges),
            "rejected_pair_count": len(complete_rejected_edges),
            "failure_taxonomy_counts": _failure_taxonomy_counts(complete_rejected_edges),
        },
        "core_signature_branch_coverage": [
            {
                "component_case_ids": report.get("component_case_ids"),
                "admitted_case_ids": report.get("admitted_case_ids"),
                "coverage": report.get("core_signature_branch_coverage"),
            }
            for report in pattern_admission_reports
            if report.get("core_signature_branch_coverage")
        ],
        "pattern_shadowed_groups": {
            "families": [],
            "singletons": [],
        },
        "library_invariants": _library_invariant_report(output_library),
        "families": family_reports,
        "absorption_candidates": [],
        "remaining_singletons": [
            {
                "group_id": group.group_id,
                "case_ids": list(group.case_ids),
                "skeleton": _model_dump(group.core_interface.repair_skeleton_prototype),
                "question_tags": sorted(_question_tags(group)),
                "pred_tags": sorted(_manifest_tags(group)),
                "manual_label": labels.get(group.case_ids[0], "unlabeled") if group.case_ids else "unlabeled",
            }
            for group in remaining_singletons
        ],
        "retrieval_accepted_edge_context": _pair_decision_context(accepted_edges),
        "accepted_edge_context": _pair_decision_context(complete_accepted_edges),
        "component_splits": [],
        "rejected_edge_sample": [
            _pattern_pair_decision(edge, include_blockers=False)
            for edge in complete_rejected_edges[:200]
        ],
        "failure_taxonomy_counts": _failure_taxonomy_counts(complete_rejected_edges),
        "manual_alignment": _alignment_report(
            predicted_families=[],
            remaining_singletons=remaining_singletons,
            manual_labels=labels,
        )
        if labels
        else None,
        "manual_pattern_alignment": _alignment_report(
            predicted_families=pattern_candidates,
            remaining_singletons=remaining_singletons,
            manual_labels=labels,
        )
        if labels
        else None,
    }
    return output_library, report


def load_library_state(payload: Dict[str, Any]) -> LibraryStateV2:
    """Parse v2 library payloads and fail clearly on legacy v1 libraries."""
    if "singletons" not in payload:
        raise ValueError("Expected v2 LibraryState payload with singletons")
    normalized = dict(payload)
    normalized["experience_families"] = []
    library = LibraryStateV2.model_validate(normalized)
    materialize_library_runtime_contracts(library)
    return library


__all__ = [
    "PairScore",
    "build_family_from_groups",
    "form_offline_families",
    "jaccard",
    "load_library_state",
    "output_contract_compatible",
    "score_pair",
    "structural_compatibility",
]
