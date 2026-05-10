"""Serial library evolution helpers for EEA v2.

The current learning path keeps singleton sources and strict formal patterns.
Experience-family formation is disabled as a runtime/promotion source.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from method.EEA.rulebook.common.core.data_structures import GroupSummary, LibraryStateV2
from method.EEA.rulebook.common.learning.pattern_formation import form_offline_families
from method.EEA.rulebook.common.learning.promotion import (
    CaseLoader,
    apply_promotion_decision,
    integrate_promoted_groups,
    run_promotion_test,
)
from method.EEA.rulebook.common.runtime.trigger_contract import (
    ensure_materialized_trigger_contract,
    materialize_library_runtime_contracts,
)
from method.EEA.rulebook.common.core.vocabulary import Confidence, GroupStatus, GroupType


_LAST_PATTERN_DEDUP_AUDIT: List[Dict[str, Any]] = []
_PATTERN_EQUIVALENCE_CACHE: Dict[str, Dict[str, Any]] = {}


def _payload(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_payload(item) for item in value]
    return value


def _status_value(group: GroupSummary) -> str:
    status = getattr(group, "status", "")
    return str(getattr(status, "value", status))


def _library_counts(library: LibraryStateV2) -> Dict[str, int]:
    return {
        "patterns": len(library.patterns or []),
        "experience_families": len(library.experience_families or []),
        "singletons": len(library.singletons or []),
        "cases_processed": int(library.cases_processed or 0),
    }


def _local_evolve_replay_enabled() -> bool:
    raw = str(os.getenv("EEA_LOCAL_EVOLVE_REPLAY", "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _mark_local_evolve_runtime_visible(group: GroupSummary) -> Tuple[GroupSummary, Dict[str, Any]]:
    promoted = group.model_copy(deep=True)
    promoted.group_type = GroupType.PATTERN
    promoted.runtime_usable = True
    promoted.runtime_blockers = []
    promoted.confidence = Confidence.MEDIUM
    promoted.lifecycle.promotion_state = "runtime_visible_local_evolve_audit_only"
    promoted.lifecycle.quarantine_reason = "local_evolve_replay_deferred_to_final_freeze"
    promoted, contract_audit = ensure_materialized_trigger_contract(promoted)
    program = getattr(promoted.instantiation_program, "synthesized_program", None)
    envelope = getattr(program, "program_envelope", None) if program is not None else None
    envelope_payload = _payload(envelope)
    if envelope_payload:
        updated_branches: List[Dict[str, Any]] = []
        lightweight_count = 0
        for branch in envelope_payload.get("runtime_branches") or []:
            payload = dict(_payload(branch) or {})
            has_runtime_binding = bool(
                payload.get("bundled_op_ids")
                or payload.get("allowed_primitives")
                or payload.get("bundle_ids")
            )
            if has_runtime_binding and payload.get("runtime_usable") is not True:
                payload["runtime_usable"] = True
                payload["runtime_validation_policy"] = "local_evolve_lightweight_binder_gated"
                payload["runtime_blockers"] = []
                payload["cross_case_replay_pending"] = True
                lightweight_count += 1
            elif payload.get("runtime_usable"):
                lightweight_count += 1
            updated_branches.append(payload)
        if updated_branches:
            envelope_payload["runtime_branches"] = updated_branches
            envelope_payload["branch_selection_contract"] = {
                **dict(envelope_payload.get("branch_selection_contract") or {}),
                "selection_unit": "runtime_branch",
                "runtime_usable_branch_count": lightweight_count,
                "runtime_validation_policy": "local_evolve_lightweight_binder_gated",
            }
            updated_envelope = (
                envelope.model_copy(update=envelope_payload)
                if hasattr(envelope, "model_copy")
                else envelope_payload
            )
            promoted = promoted.model_copy(
                update={
                    "instantiation_program": promoted.instantiation_program.model_copy(
                        update={
                            "synthesized_program": program.model_copy(
                                update={"program_envelope": updated_envelope}
                            )
                        }
                    )
                }
            )
            contract_audit = {
                **dict(contract_audit or {}),
                "lightweight_runtime_branch_count": lightweight_count,
                "lightweight_runtime_branch_policy": "local_evolve_lightweight_binder_gated",
            }
    if not bool(contract_audit.get("runtime_executable")):
        promoted.runtime_usable = True
        promoted.runtime_contract_status = str(contract_audit.get("status") or "blocked")
    return promoted, contract_audit


def _pattern_recognition_contract_payload(group: GroupSummary) -> Dict[str, Any]:
    program = getattr(group, "instantiation_program", None)
    contract = getattr(program, "pattern_recognition_contract", None)
    payload = _payload(contract) or {}
    if payload:
        return payload
    trigger_contract = _payload(getattr(group, "trigger_contract", None)) or {}
    pre_condition = _payload(trigger_contract.get("pre_condition")) or {}
    if not pre_condition:
        return {}
    return {
        "pre_question_signature": pre_condition.get("pre_question_signature")
        or pre_condition.get("pre_question_signature_local")
        or "",
        "pre_sql_signature": pre_condition.get("pre_sql_signature")
        or pre_condition.get("pre_sql_signature_local")
        or "",
        "observed_failure_summary": pre_condition.get("observed_failure_summary")
        or pre_condition.get("observed_failure_local")
        or "",
        "repair_direction": pre_condition.get("repair_direction")
        or pre_condition.get("repair_direction_local")
        or "",
    }


def _normalized_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _pattern_precondition_key(group: GroupSummary) -> Tuple[str, str, str]:
    payload = _pattern_recognition_contract_payload(group)
    return (
        _normalized_text(payload.get("pre_question_signature")),
        _normalized_text(payload.get("pre_sql_signature")),
        _normalized_text(payload.get("repair_direction")),
    )


def _pattern_equivalence_judge(
    left: GroupSummary,
    right: GroupSummary,
) -> Dict[str, Any]:
    left_payload = _pattern_recognition_contract_payload(left)
    right_payload = _pattern_recognition_contract_payload(right)
    key = json.dumps(
        {
            "left": left_payload,
            "right": right_payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    cached = _PATTERN_EQUIVALENCE_CACHE.get(digest)
    if cached is not None:
        return dict(cached)
    try:
        from method.EEA.rulebook.common.llm.prompts.pattern_equivalence_judge import (
            build_pattern_equivalence_judge_prompt,
        )
        from method.EEA.rulebook.common.llm.utils import call_llm

        prompt = build_pattern_equivalence_judge_prompt(
            left_json=json.dumps(left_payload, ensure_ascii=False, indent=2, default=str),
            right_json=json.dumps(right_payload, ensure_ascii=False, indent=2, default=str),
        )
        raw = call_llm(
            prompt,
            expect_json=True,
            stage="pattern_equivalence_judge",
            trace_context={
                "left_group_id": left.group_id,
                "right_group_id": right.group_id,
            },
        )
        payload = dict(raw) if isinstance(raw, dict) else {}
    except Exception as exc:  # pragma: no cover - defensive LLM path.
        payload = {
            "relation": "disjoint",
            "confidence": 0.0,
            "reason": f"judge_error:{type(exc).__name__}",
        }
    relation = str(payload.get("relation") or "disjoint")
    if relation not in {"equivalent", "left_subsumes_right", "right_subsumes_left", "disjoint"}:
        relation = "disjoint"
    try:
        confidence = float(payload.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0
    out = {
        "relation": relation,
        "confidence": max(0.0, min(1.0, confidence)),
        "reason": str(payload.get("reason") or "")[:240],
    }
    _PATTERN_EQUIVALENCE_CACHE[digest] = out
    return dict(out)


def _pattern_root_key(group: GroupSummary) -> Tuple[str, str, str, str, str]:
    program = getattr(group.instantiation_program, "synthesized_program", None)
    contract_payload = _payload(getattr(group, "trigger_contract", None)) or {}
    action_contract = _payload(contract_payload.get("action_contract")) or {}
    envelope = _payload(getattr(program, "program_envelope", None)) or {}
    pre_q, pre_sql, repair_direction = _pattern_precondition_key(group)
    return (
        pre_q,
        pre_sql,
        repair_direction,
        str(
            _payload(getattr(group, "formation_signals", None))
            .get("pattern_admission", {})
            .get("primary_repair_interface")
            or ""
        ),
        str(action_contract.get("op_family") or (envelope.get("action_envelope") or {}).get("op_family") or ""),
    )


def _pattern_action_family_key(group: GroupSummary) -> Tuple[str, str]:
    program = getattr(group.instantiation_program, "synthesized_program", None)
    envelope = _payload(getattr(program, "program_envelope", None)) or {}
    contract_payload = _payload(getattr(group, "trigger_contract", None)) or {}
    action_contract = _payload(contract_payload.get("action_contract")) or {}
    op_family = str(
        action_contract.get("op_family")
        or _payload(envelope.get("action_envelope")).get("op_family")
        or ""
    )
    core_ops = []
    for op in list(getattr(program, "ops", []) or []):
        payload = _payload(op)
        args = _payload(payload.get("arguments"))
        signature = _payload(args.get("operation_signature") or args.get("shared_signature"))
        if payload.get("is_dependency") or signature.get("is_dependency"):
            continue
        op_type = str(payload.get("op_type") or "").upper()
        if op_type:
            core_ops.append(op_type)
    if core_ops:
        return op_family, ",".join(sorted(set(core_ops)))
    return op_family, ""


def _patterns_are_same_abstract_root(left: GroupSummary, right: GroupSummary) -> Tuple[bool, Dict[str, Any]]:
    left_cases = {str(case_id) for case_id in (left.case_ids or [])}
    right_cases = {str(case_id) for case_id in (right.case_ids or [])}
    shared_cases = left_cases & right_cases
    action_left = _pattern_action_family_key(left)
    action_right = _pattern_action_family_key(right)
    precondition_left = _pattern_precondition_key(left)
    precondition_right = _pattern_precondition_key(right)
    has_left_precondition = any(precondition_left)
    has_right_precondition = any(precondition_right)
    precondition_match = not (has_left_precondition and has_right_precondition) or precondition_left == precondition_right
    equivalence_audit: Dict[str, Any] = {}
    if not precondition_match and has_left_precondition and has_right_precondition:
        equivalence_audit = _pattern_equivalence_judge(left, right)
        precondition_match = (
            equivalence_audit.get("relation")
            in {"equivalent", "left_subsumes_right", "right_subsumes_left"}
            and float(equivalence_audit.get("confidence") or 0.0) >= 0.6
        )
    subset_match = bool(left_cases <= right_cases or right_cases <= left_cases)
    audit: Dict[str, Any] = {
        "left_group_id": left.group_id,
        "right_group_id": right.group_id,
        "left_case_ids": sorted(left_cases),
        "right_case_ids": sorted(right_cases),
        "case_overlap_size": len(shared_cases),
        "case_subset_match": subset_match,
        "action_family_key_left": list(action_left),
        "action_family_key_right": list(action_right),
        "action_family_match": action_left == action_right,
        "pre_condition_key_left": list(precondition_left),
        "pre_condition_key_right": list(precondition_right),
        "pre_condition_match": precondition_match,
        "pre_condition_equivalence_judge": equivalence_audit,
        "decision": False,
        "reject_reason": "",
    }
    if len(shared_cases) < 2 and not (left_cases <= right_cases or right_cases <= left_cases):
        audit["reject_reason"] = "case_overlap_or_subset_failed"
        return False, audit
    if action_left != action_right:
        audit["reject_reason"] = "action_family_mismatch"
        return False, audit
    if not precondition_match:
        audit["reject_reason"] = "pre_condition_signature_mismatch"
        return False, audit
    audit["decision"] = True
    return True, audit


def _merge_same_root_pattern_component(component: Sequence[GroupSummary]) -> GroupSummary:
    base = sorted(
        component,
        key=lambda group: (
            len({str(case_id) for case_id in group.case_ids or []}),
            bool(group.runtime_usable),
            str(group.version),
        ),
        reverse=True,
    )[0]
    case_ids = sorted(
        {
            str(case_id)
            for group in component
            for case_id in (group.case_ids or [])
        },
        key=lambda value: (0, int(value)) if value.isdigit() else (1, value),
    )
    return base.model_copy(
        update={
            "case_ids": case_ids,
            "support": len(case_ids),
        }
    )


def _merge_overlapping_same_root_patterns(patterns: Sequence[GroupSummary]) -> List[GroupSummary]:
    global _LAST_PATTERN_DEDUP_AUDIT
    _LAST_PATTERN_DEDUP_AUDIT = []
    rows = list(patterns or [])
    if len(rows) <= 1:
        return rows
    visited: Set[int] = set()
    merged: List[GroupSummary] = []
    for idx, pattern in enumerate(rows):
        if idx in visited:
            continue
        stack = [idx]
        component_indexes: Set[int] = set()
        while stack:
            current = stack.pop()
            if current in component_indexes:
                continue
            component_indexes.add(current)
            for other_idx, other in enumerate(rows):
                if other_idx in component_indexes:
                    continue
                same_root, audit = _patterns_are_same_abstract_root(rows[current], other)
                if not same_root and len(_LAST_PATTERN_DEDUP_AUDIT) < 500:
                    _LAST_PATTERN_DEDUP_AUDIT.append(audit)
                if same_root:
                    stack.append(other_idx)
        visited.update(component_indexes)
        component = [rows[item] for item in sorted(component_indexes)]
        if len(component) == 1:
            merged.append(component[0])
        else:
            merged.append(_merge_same_root_pattern_component(component))
    return merged


def last_pattern_dedup_audit() -> List[Dict[str, Any]]:
    return [dict(row) for row in _LAST_PATTERN_DEDUP_AUDIT]


def _nested_pattern_supersedes(left: GroupSummary, right: GroupSummary) -> bool:
    left_cases = {str(case_id) for case_id in (left.case_ids or [])}
    right_cases = {str(case_id) for case_id in (right.case_ids or [])}
    if not left_cases or not left_cases < right_cases:
        return False
    left_key = _pattern_root_key(left)
    right_key = _pattern_root_key(right)
    if any(left_key) and any(right_key) and left_key == right_key:
        return True
    left_family = _pattern_action_family_key(left)
    right_family = _pattern_action_family_key(right)
    # When the smaller pattern has no stronger contradictory action family,
    # keep only the broader admitted root. This prevents online admission from
    # leaving [A,B], [A,B,C], ... versions of the same abstract pattern.
    if not any(left_family) or not any(right_family):
        return True
    return left_family == right_family


def _pattern_has_executable_program(group: GroupSummary) -> bool:
    program = getattr(group.instantiation_program, "synthesized_program", None)
    if program is None:
        return False
    return bool(list(getattr(program, "ops", []) or []))


def _deduplicate_nested_patterns(patterns: Iterable[GroupSummary]) -> List[GroupSummary]:
    rows = [
        pattern
        for pattern in (patterns or [])
        if _pattern_has_executable_program(pattern)
    ]
    superseded: Set[str] = set()
    for left in rows:
        left_id = str(left.group_id)
        left_cases = {str(case_id) for case_id in (left.case_ids or [])}
        if not left_cases:
            continue
        for right in rows:
            right_id = str(right.group_id)
            if left_id == right_id:
                continue
            right_cases = {str(case_id) for case_id in (right.case_ids or [])}
            if len(right_cases) <= len(left_cases):
                continue
            if _nested_pattern_supersedes(left, right):
                superseded.add(left_id)
                break
    return [pattern for pattern in rows if str(pattern.group_id) not in superseded]


def _merge_patterns_without_absorbing_singletons(
    library: LibraryStateV2,
    patterns: Iterable[GroupSummary],
) -> None:
    by_id = {str(group.group_id): group for group in (library.patterns or [])}
    for pattern in patterns:
        by_id[str(pattern.group_id)] = pattern
    deduped = _deduplicate_nested_patterns(by_id.values())
    deduped = _merge_overlapping_same_root_patterns(deduped)
    library.patterns = sorted(
        deduped,
        key=lambda group: (
            int(group.case_ids[0])
            if group.case_ids and str(group.case_ids[0]).isdigit()
            else 0,
            str(group.group_id),
        ),
    )


def _signature_phrases_for_pattern(pattern: GroupSummary) -> Set[str]:
    payload = _pattern_recognition_contract_payload(pattern)
    phrases: Set[str] = set()
    for field in (
        "pre_question_signature",
        "pre_sql_signature",
        "observed_failure_summary",
        "repair_direction",
    ):
        text = _normalized_text(payload.get(field))
        if not text:
            continue
        for chunk in re.split(r"[.;,\n]+", text):
            phrase = " ".join(chunk.split())
            if 3 <= len(phrase) <= 120:
                phrases.add(phrase)
        tokens = re.findall(r"[a-z0-9_]+", text)
        for size in (2, 3):
            for index in range(0, max(0, len(tokens) - size + 1)):
                phrase = " ".join(tokens[index : index + size])
                if 5 <= len(phrase) <= 80:
                    phrases.add(phrase)
    return phrases


def _update_signature_phrase_catalog(library: LibraryStateV2) -> None:
    catalog = dict(getattr(library, "signature_phrase_catalog", {}) or {})
    for pattern in library.patterns or []:
        for phrase in _signature_phrases_for_pattern(pattern):
            row = dict(catalog.get(phrase) or {})
            row.setdefault("first_seen_pattern_id", pattern.group_id)
            support_ids = {
                str(item)
                for item in (row.get("support_pattern_ids") or [])
                if str(item)
            }
            support_ids.add(str(pattern.group_id))
            row["support_pattern_ids"] = sorted(support_ids)
            row["support_pattern_count"] = len(support_ids)
            row["last_seen_pattern_id"] = pattern.group_id
            catalog[phrase] = row
    library.signature_phrase_catalog = catalog


def _compact_pattern_report(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "group_id": str(row.get("group_id") or ""),
        "case_ids": [str(case_id) for case_id in (row.get("case_ids") or [])],
        "support": int(row.get("support") or 0),
        "runtime_usable": bool(row.get("runtime_usable", False)),
        "promotion_state": str(row.get("promotion_state") or ""),
        "stable_bias_key": row.get("stable_bias_key"),
        "primary_repair_interface": row.get("primary_repair_interface"),
        "reject_reason": row.get("reject_reason"),
        "effect_axes": list(row.get("effect_axes") or [])[:8],
        "manual_label_counts": dict(row.get("manual_label_counts") or {}),
    }


def _compact_promotion_result(row: Dict[str, Any]) -> Dict[str, Any]:
    metrics = dict(row.get("replay_metrics") or {})
    formal_metrics = dict(row.get("formal_replay_metrics") or {})
    promoted = dict(row.get("promoted_group") or {})
    branch_runtime = dict(row.get("branch_runtime") or {})
    return {
        "group_id": str(row.get("group_id") or promoted.get("group_id") or ""),
        "eligible": bool(row.get("eligible", False)),
        "reason": str(row.get("reason") or "")[:1000],
        "formal_promotion_blocker": str(row.get("formal_promotion_blocker") or "")[:1000],
        "support_protocol_passed": bool(row.get("support_protocol_passed", False)),
        "replay_metrics": {
            key: metrics.get(key)
            for key in (
                "compile_coverage",
                "replay_improvement",
                "replay_regression",
                "sample_size",
                "comparison_unknown_rate",
            )
        },
        "formal_replay_metrics": {
            key: formal_metrics.get(key)
            for key in (
                "compile_coverage",
                "replay_improvement_llm_selected",
                "replay_improvement_deterministic_unique",
                "replay_regression",
                "sample_size",
                "formal_eligible_sample_size",
            )
        },
        "promoted_group": {
            "group_id": str(promoted.get("group_id") or ""),
            "group_type": str(promoted.get("group_type") or ""),
            "runtime_usable": bool(promoted.get("runtime_usable", False)),
            "status": str(promoted.get("status") or ""),
            "promotion_state": str(promoted.get("promotion_state") or ""),
        },
        "branch_runtime": {
            "runtime_usable_branch_count": int(
                branch_runtime.get("runtime_usable_branch_count") or 0
            ),
            "runtime_usable_branch_ids": list(
                branch_runtime.get("runtime_usable_branch_ids") or []
            )[:12],
            "runtime_superseded_case_ids": list(
                branch_runtime.get("runtime_superseded_case_ids") or []
            )[:24],
        },
    }


def compact_evolution_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """Return a log-safe summary for online adapters.

    Full formation/replay reports contain pair decisions and replay rows that
    grow with the whole prefix. They are useful offline, but should not be
    embedded in per-case runtime logs.
    """
    formation = dict(report.get("formation") or {})
    patterns = [
        _compact_pattern_report(dict(row))
        for row in (formation.get("patterns") or [])[:50]
        if isinstance(row, dict)
    ]
    promotion_results = [
        _compact_promotion_result(dict(row))
        for row in (report.get("promotion_results") or [])[:50]
        if isinstance(row, dict)
    ]
    return {
        "schema_version": "evolution-report-compact-v1",
        "event_kind": str(report.get("event_kind") or ""),
        "focus_case_ids": [str(case_id) for case_id in (report.get("focus_case_ids") or [])],
        "library_counts_before": dict(report.get("library_counts_before") or {}),
        "library_counts_after": dict(report.get("library_counts_after") or {}),
        "formation_counts": {
            "input": dict(formation.get("input_counts") or {}),
            "output": dict(formation.get("output_counts") or {}),
        },
        "pattern_extension_candidates": list(
            formation.get("pattern_extension_candidates") or []
        )[:20],
        "pattern_dedup_audit": list(report.get("pattern_dedup_audit") or [])[:50],
        "candidate_pattern_count": int(report.get("candidate_pattern_count") or 0),
        "candidate_evolved_object_count": int(report.get("candidate_evolved_object_count") or 0),
        "promotion_skipped_reason": report.get("promotion_skipped_reason"),
        "final_freeze_skipped": bool(report.get("final_freeze_skipped", False)),
        "promoted_runtime_objects": list(report.get("promoted_runtime_objects") or []),
        "promotion_results": promotion_results,
        "patterns": patterns,
        "runtime_contract_validation": dict(report.get("runtime_contract_validation") or {}),
        "signature_phrase_catalog_size": int(report.get("signature_phrase_catalog_size") or 0),
        "freeze_manifest": dict(report.get("freeze_manifest") or {}),
        "learned_but_no_future_opportunity": list(
            report.get("learned_but_no_future_opportunity") or []
        ),
    }


def _source_singletons_by_case_id(library: LibraryStateV2) -> Dict[str, GroupSummary]:
    """Return per-case singleton source programs, including archived ones."""
    sources: Dict[str, GroupSummary] = {}
    for group in library.singletons or []:
        if group.group_type != GroupType.SINGLETON or len(group.case_ids or []) != 1:
            continue
        source = group.model_copy(deep=True)
        source.status = GroupStatus.ACTIVE
        source.runtime_usable = True
        ensure_materialized_trigger_contract(source)
        sources[str(group.case_ids[0])] = source
    return sources


def _active_evolution_candidates(
    formed_library: LibraryStateV2,
    *,
    focus_case_ids: Optional[Set[str]] = None,
) -> List[GroupSummary]:
    by_id: Dict[str, GroupSummary] = {}
    for family in list(formed_library.patterns or []):
        if family.group_type != GroupType.PATTERN:
            continue
        if _status_value(family) != GroupStatus.ACTIVE.value:
            continue
        by_id[family.group_id] = family
    # Focus is audit context for the event that triggered this evolution cycle;
    # replay/promotion candidates come from the whole current memory prefix.
    _ = focus_case_ids
    return sorted(
        by_id.values(),
        key=lambda group: (min(map(str, group.case_ids or [""])), group.group_id),
    )


def freeze_library_manifest(
    *,
    library: LibraryStateV2,
    reason: str,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build and optionally write a compact freeze manifest."""
    manifest: Dict[str, Any] = {
        "schema_version": "library-freeze-v1",
        "reason": reason,
        "db_id": library.db_id,
        "cases_processed": int(library.cases_processed or 0),
        "library_counts": _library_counts(library),
        "group_versions": {},
        "frozen_at_utc": datetime.utcnow().isoformat(),
    }
    for group in [
        *(library.patterns or []),
        *(library.singletons or []),
    ]:
        manifest["group_versions"][group.group_id] = {
            "group_type": str(getattr(group.group_type, "value", group.group_type)),
            "version": int(group.version or 0),
            "case_ids": list(group.case_ids or []),
            "runtime_usable": bool(group.runtime_usable),
            "status": str(getattr(group.status, "value", group.status)),
            "promotion_state": group.lifecycle.promotion_state,
        }
    if output_path is not None:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        manifest["path"] = str(output_path)
    return manifest


def evolve_library_with_replay(
    *,
    library: LibraryStateV2,
    event_kind: str,
    case_loader: Optional[CaseLoader] = None,
    db_path: Optional[str] = None,
    database_dir: Optional[str] = None,
    manual_groups: Optional[Dict[str, Any]] = None,
    focus_case_ids: Optional[Iterable[str]] = None,
    max_neighbor_edges: int = 5,
    family_runtime_policy: str = "replay_gated",
    promotion_min_support: int = 2,
    row_sample_limit: int = 5000,
    freeze_output_path: Optional[Path] = None,
    skip_replay_freeze: bool = False,
) -> Tuple[LibraryStateV2, Dict[str, Any]]:
    """Run one serial singleton->pattern evolution cycle."""
    focus_set = {str(case_id) for case_id in (focus_case_ids or set())}
    working_library = library.model_copy(deep=True)
    working_library.experience_families = []
    before_counts = _library_counts(working_library)
    if event_kind == "final_evolve_and_freeze" and skip_replay_freeze:
        _update_signature_phrase_catalog(working_library)
        manifest = freeze_library_manifest(
            library=working_library,
            reason="final_evolve_and_freeze_skipped",
            output_path=freeze_output_path,
        )
        report = {
            "event_kind": event_kind,
            "focus_case_ids": sorted(focus_set),
            "library_counts_before": before_counts,
            "library_counts_after": _library_counts(working_library),
            "formation": {
                "input_counts": before_counts,
                "output_counts": _library_counts(working_library),
                "patterns": [],
                "families": [],
            },
            "candidate_family_count": 0,
            "candidate_evolved_object_count": 0,
            "candidate_pattern_count": 0,
            "promotion_results": [],
            "promoted_runtime_objects": [],
            "promotion_skipped_reason": "skip_final_freeze",
            "pattern_dedup_audit": [],
            "runtime_contract_validation": {},
            "signature_phrase_catalog_size": len(
                getattr(working_library, "signature_phrase_catalog", {}) or {}
            ),
            "freeze_manifest": {
                **manifest,
                "skipped": True,
                "skip_reason": "skip_final_freeze",
            },
            "final_freeze_skipped": True,
        }
        return working_library, report
    formed_library, formation_report = form_offline_families(
        working_library,
        manual_groups=manual_groups,
        max_neighbor_edges=max_neighbor_edges,
        mark_runtime_usable=False,
        focus_case_ids=focus_set or None,
    )
    family_candidates = _active_evolution_candidates(
        formed_library,
        focus_case_ids=focus_set or None,
    )
    report: Dict[str, Any] = {
        "event_kind": event_kind,
        "focus_case_ids": sorted(focus_set),
        "library_counts_before": before_counts,
        "formation": _payload(formation_report),
        "candidate_family_count": 0,
        "candidate_evolved_object_count": len(family_candidates),
        "candidate_pattern_count": len(
            [
                group
                for group in family_candidates
                if group.group_type == GroupType.PATTERN
            ]
        ),
        "promotion_results": [],
        "promoted_runtime_objects": [],
        "promotion_skipped_reason": None,
        "pattern_dedup_audit": last_pattern_dedup_audit(),
    }

    can_run_replay = bool(case_loader is not None and db_path)
    local_replay_deferred = (
        event_kind == "local_evolve"
        and family_runtime_policy == "replay_gated"
        and int(working_library.cases_processed or 0) >= int(promotion_min_support)
        and not _local_evolve_replay_enabled()
    )
    if local_replay_deferred:
        promoted_groups: List[GroupSummary] = []
        report["promotion_skipped_reason"] = "local_evolve_replay_deferred_to_final_freeze"
        for family in family_candidates:
            promoted, contract_audit = _mark_local_evolve_runtime_visible(family)
            promoted_groups.append(promoted)
            result_payload = {
                "group_id": promoted.group_id,
                "eligible": False,
                "reason": "local_evolve_replay_deferred_to_final_freeze",
                "formal_promotion_blocker": "local_evolve_replay_deferred_to_final_freeze",
                "runtime_family_evidence_mode": "local_evolve_audit_visible",
                "contract_audit": _payload(contract_audit),
                "replay_metrics": {
                    "version": int(promoted.version or 0),
                    "sample_size": 0,
                    "leave_one_out_done": False,
                },
                "formal_replay_metrics": {
                    "version": int(promoted.version or 0),
                    "sample_size": 0,
                    "leave_one_out_done": False,
                },
                "promoted_group": {
                    "group_id": promoted.group_id,
                    "group_type": str(getattr(promoted.group_type, "value", promoted.group_type)),
                    "runtime_usable": bool(promoted.runtime_usable),
                    "status": str(getattr(promoted.status, "value", promoted.status)),
                    "promotion_state": promoted.lifecycle.promotion_state,
                },
            }
            report["promotion_results"].append(result_payload)
            if promoted.runtime_usable:
                report["promoted_runtime_objects"].append(result_payload["promoted_group"])
        _merge_patterns_without_absorbing_singletons(working_library, promoted_groups)
    elif (
        family_runtime_policy == "replay_gated"
        and int(working_library.cases_processed or 0) >= int(promotion_min_support)
        and can_run_replay
    ):
        source_singletons = _source_singletons_by_case_id(working_library)
        promoted_groups: List[GroupSummary] = []
        for family in family_candidates:
            result = run_promotion_test(
                group=family,
                source_singletons_by_case_id=source_singletons,
                case_loader=case_loader,
                db_path=str(db_path),
                database_dir=database_dir,
                row_sample_limit=row_sample_limit,
            )
            promoted = apply_promotion_decision(family, result)
            promoted_groups.append(promoted)
            result_payload = _payload(result)
            result_payload["promoted_group"] = {
                "group_id": promoted.group_id,
                "group_type": str(getattr(promoted.group_type, "value", promoted.group_type)),
                "runtime_usable": bool(promoted.runtime_usable),
                "status": str(getattr(promoted.status, "value", promoted.status)),
                "promotion_state": promoted.lifecycle.promotion_state,
            }
            program = getattr(promoted.instantiation_program, "synthesized_program", None)
            envelope = _payload(getattr(program, "program_envelope", None)) or {}
            runtime_branches = [
                dict(branch)
                for branch in (envelope.get("runtime_branches") or [])
                if isinstance(branch, dict)
            ]
            result_payload["branch_runtime"] = {
                "runtime_usable_branch_count": sum(
                    1 for branch in runtime_branches if bool(branch.get("runtime_usable"))
                ),
                "runtime_usable_branch_ids": [
                    str(branch.get("branch_id") or "")
                    for branch in runtime_branches
                    if bool(branch.get("runtime_usable"))
                ],
                "runtime_superseded_case_ids": sorted(
                    {
                        str(case_id)
                        for branch in runtime_branches
                        if bool(branch.get("runtime_usable"))
                        for case_id in (branch.get("support_case_ids") or [])
                        if str(case_id)
                    }
                ),
            }
            report["promotion_results"].append(result_payload)
            if promoted.runtime_usable:
                report["promoted_runtime_objects"].append(result_payload["promoted_group"])
        integrate_promoted_groups(working_library, promoted_groups)
        _merge_patterns_without_absorbing_singletons(working_library, working_library.patterns)
    else:
        if family_runtime_policy != "replay_gated":
            report["promotion_skipped_reason"] = f"family_runtime_policy={family_runtime_policy}"
        elif int(working_library.cases_processed or 0) < int(promotion_min_support):
            report["promotion_skipped_reason"] = "below_promotion_min_support"
        elif not can_run_replay:
            report["promotion_skipped_reason"] = "missing_case_loader_or_db_path"
        # Keep discovered strict patterns offline when replay cannot run, but
        # do not remove or deactivate their source singletons. Runtime admission
        # still requires a later replay-gated cycle.
        integrate_promoted_groups(working_library, family_candidates)
        _merge_patterns_without_absorbing_singletons(working_library, working_library.patterns)

    report["library_counts_after"] = _library_counts(working_library)
    report["pattern_dedup_audit"] = last_pattern_dedup_audit()
    _library, contract_report = materialize_library_runtime_contracts(working_library)
    _update_signature_phrase_catalog(working_library)
    report["runtime_contract_validation"] = contract_report
    report["signature_phrase_catalog_size"] = len(
        getattr(working_library, "signature_phrase_catalog", {}) or {}
    )
    if event_kind == "final_evolve_and_freeze" and report["promoted_runtime_objects"]:
        report["learned_but_no_future_opportunity"] = [
            dict(item) for item in report["promoted_runtime_objects"]
        ]
    if event_kind == "final_evolve_and_freeze" or freeze_output_path is not None:
        report["freeze_manifest"] = freeze_library_manifest(
            library=working_library,
            reason=event_kind,
            output_path=freeze_output_path,
        )
    return working_library, report


def final_evolve_and_freeze(
    *,
    library: LibraryStateV2,
    case_loader: Optional[CaseLoader] = None,
    db_path: Optional[str] = None,
    database_dir: Optional[str] = None,
    manual_groups: Optional[Dict[str, Any]] = None,
    max_neighbor_edges: int = 5,
    family_runtime_policy: str = "replay_gated",
    promotion_min_support: int = 2,
    row_sample_limit: int = 5000,
    freeze_output_path: Optional[Path] = None,
    skip_replay_freeze: bool = False,
) -> Tuple[LibraryStateV2, Dict[str, Any]]:
    """Run the plan.md final evolution boundary and freeze manifest."""
    return evolve_library_with_replay(
        library=library,
        event_kind="final_evolve_and_freeze",
        case_loader=case_loader,
        db_path=db_path,
        database_dir=database_dir,
        manual_groups=manual_groups,
        focus_case_ids=None,
        max_neighbor_edges=max_neighbor_edges,
        family_runtime_policy=family_runtime_policy,
        promotion_min_support=promotion_min_support,
        row_sample_limit=row_sample_limit,
        freeze_output_path=freeze_output_path,
        skip_replay_freeze=skip_replay_freeze,
    )


__all__ = [
    "compact_evolution_report",
    "evolve_library_with_replay",
    "final_evolve_and_freeze",
    "freeze_library_manifest",
]
