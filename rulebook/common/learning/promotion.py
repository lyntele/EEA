"""Replay-gated family/pattern promotion for EEA v2.

Promotion is deliberately separated from family formation. Formation may find
offline neighborhoods, but only replay can decide whether a memory object is
safe to enter runtime.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from collections import Counter
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from method.EEA.rulebook.common.core.data_structures import (
    GroupSummary,
    LibraryStateV2,
    ProgramCoverage,
    PromotionTestResult,
    ReplayMetrics,
)
from method.EEA.rulebook.common.io.execution_compare import run_execution_comparison
from method.EEA.rulebook.common.learning.pattern_formation import build_family_from_groups
from method.EEA.rulebook.common.learning.program_coverage import validate_group_program_coverage
from method.EEA.rulebook.common.runtime.runtime import (
    _evaluate_pattern_pre_condition,
    _filter_group_to_runtime_branch,
    prepare_rewrite_plan,
    rewrite_one_candidate,
    rewrite_realization_origin_from_result,
)
from method.EEA.rulebook.common.runtime.trigger_contract import ensure_materialized_trigger_contract
from method.EEA.rulebook.common.core.vocabulary import (
    Confidence,
    GroupStatus,
    GroupType,
    MAX_ACTION_COUNT_PER_CASE,
    MAX_REPLAY_REGRESSION,
    PATTERN_PROMOTION_MIN_COMPILE_COVERAGE,
    PATTERN_PROMOTION_MIN_REPLAY_IMPROVEMENT,
    PROMOTION_SUPPORT_MIN,
    RISK_HIGH_BRANCH_COUNT,
    RUNTIME_FAMILY_MIN_COMPILE_COVERAGE,
    RUNTIME_FAMILY_MIN_REPLAY_IMPROVEMENT,
)


CaseLoader = Callable[[str], Optional[Dict[str, Any]]]
_PROMOTION_REPLAY_CACHE: Dict[str, Dict[str, Any]] = {}
PATTERN_PRE_CONDITION_SELF_RECALL_MIN = 0.8


def _payload(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return dict(getattr(value, "__dict__", {}) or {})


def _replay_memory_hash(memory: GroupSummary) -> str:
    payload = {
        "group_id": memory.group_id,
        "group_type": str(getattr(memory.group_type, "value", memory.group_type)),
        "version": int(memory.version or 0),
        "case_ids": list(memory.case_ids or []),
        "runtime_usable": bool(memory.runtime_usable),
        "trigger_contract": _payload(getattr(memory, "trigger_contract", None)),
        "instantiation_program": _payload(getattr(memory, "instantiation_program", None)),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _promotion_replay_cache_key(
    *,
    memory: GroupSummary,
    holdout_case_id: str,
    replay_mode: str,
    training_case_ids: Sequence[str],
    db_path: str,
    database_dir: Optional[str],
    row_sample_limit: int,
    holdout_in_training: bool,
    eligible_for_formal_promotion: bool,
    case_record: Mapping[str, Any],
) -> str:
    payload = {
        "memory_hash": _replay_memory_hash(memory),
        "holdout_case_id": str(holdout_case_id),
        "replay_mode": replay_mode,
        "training_case_ids": [str(case_id) for case_id in training_case_ids],
        "db_path": str(db_path),
        "database_dir": str(database_dir or ""),
        "row_sample_limit": int(row_sample_limit),
        "holdout_in_training": bool(holdout_in_training),
        "eligible_for_formal_promotion": bool(eligible_for_formal_promotion),
        "case_record_hash": hashlib.sha1(
            json.dumps(
                {
                    "question": case_record.get("question"),
                    "evidence": case_record.get("evidence"),
                    "gold_sql": case_record.get("gold_sql"),
                    "candidates": list(case_record.get("candidates") or []),
                },
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
        ).hexdigest(),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _comparison_status(cmp_payload: Optional[Dict[str, Any]]) -> str:
    if not cmp_payload:
        return "not_run"
    if cmp_payload.get("pred_error"):
        return "error"
    if cmp_payload.get("gold_error"):
        return "gold_error"
    pred_cols = cmp_payload.get("column_count_pred")
    gold_cols = cmp_payload.get("column_count_gold")
    pred_rows = cmp_payload.get("pred_row_count")
    gold_rows = cmp_payload.get("gold_row_count")
    shape_matches = pred_cols is None or gold_cols is None or pred_cols == gold_cols
    row_counts_match = pred_rows is None or gold_rows is None or pred_rows == gold_rows
    if cmp_payload.get("row_sets_equivalent") is True and shape_matches and row_counts_match:
        return "equivalent"
    if cmp_payload.get("row_sets_equivalent") is True and not row_counts_match:
        return "row_count_mismatch"
    if cmp_payload.get("row_sets_equivalent") is True and not shape_matches:
        return "shape_mismatch"
    if cmp_payload.get("row_sets_equivalent") is False:
        return "different"
    return "unknown"


def _is_equivalent(status: str) -> bool:
    return status == "equivalent"


def _comparison_unknown_reason(cmp_payload: Optional[Dict[str, Any]]) -> str:
    if not cmp_payload:
        return "not_run"
    reason = str(cmp_payload.get("comparison_unknown_reason") or "").strip()
    if reason:
        return reason
    if cmp_payload.get("pred_error") or cmp_payload.get("gold_error"):
        return "execution_error"
    if cmp_payload.get("row_sets_equivalent") is None:
        return "unknown"
    return ""


def _row_comparison_unknown_reasons(row: Mapping[str, Any]) -> List[str]:
    reasons: List[str] = []
    for key in ("c0_comparison_unknown_reason", "rewrite_comparison_unknown_reason"):
        reason = str(row.get(key) or "").strip()
        if reason:
            reasons.append(reason)
    reasons.extend(str(reason) for reason in (row.get("comparison_unknown_reasons") or []) if str(reason))
    return sorted(set(reasons))


def _candidate_empty_reasons(candidate_sets: Sequence[Any]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for candidate_set in candidate_sets or []:
        primitive = getattr(candidate_set, "primitive", None)
        primitive_value = str(getattr(primitive, "value", primitive or ""))
        reason = str(getattr(candidate_set, "empty_reason", "") or "")
        if reason:
            rows.append({"primitive": primitive_value, "reason": reason})
    return rows


def _replay_trigger_diagnostics(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """Compact runtime trigger diagnostics for replay rows."""

    audit_summary = _payload(plan.get("runtime_audit_summary"))
    trigger = _payload(audit_summary.get("trigger"))
    compiler = _payload(audit_summary.get("compiler"))
    memory_selection = _payload(
        _payload(trigger.get("branch_selection_audit")).get("_memory_selection")
    )
    top_candidates: List[Dict[str, Any]] = []
    for row in trigger.get("top_candidates") or []:
        payload = _payload(row)
        if not payload:
            continue
        top_candidates.append(
            {
                "group_id": payload.get("group_id"),
                "group_type": payload.get("group_type"),
                "gate_passed": payload.get("gate_passed"),
                "top_reasons": list(payload.get("top_reasons") or [])[:12],
                "hard_gate_reasons": list(payload.get("hard_gate_reasons") or [])[:12],
                "matched_branch_ids": payload.get("matched_branch_ids") or {},
                "branch_blockers": list(payload.get("branch_blockers") or [])[:12],
                "compiler_candidate_reasons": list(
                    payload.get("compiler_candidate_reasons") or []
                )[:12],
            }
        )
    return {
        "rewrite_enabled_reason": audit_summary.get("rewrite_enabled_reason"),
        "trigger_blocker_counts": trigger.get("blocker_counts") or {},
        "top_candidate_reasons": top_candidates[:8],
        "selected_group_ids": list(trigger.get("selected_group_ids") or []),
        "selected_branch_ids": trigger.get("selected_branch_ids") or {},
        "memory_selection_audit": memory_selection,
        "compiler_empty_reason_counts": compiler.get("empty_reason_counts") or {},
    }


def _not_actionable_reason(
    *,
    plan: Mapping[str, Any],
    compiler_output: Any,
    actions: Sequence[Any],
    empty_reasons: Sequence[Dict[str, str]],
) -> str:
    lowering_reasons = [
        f"{item.get('primitive')}:{item.get('reason')}"
        for item in empty_reasons
        if "no_lowering_rule" in str(item.get("reason") or "")
    ]
    if lowering_reasons:
        return "no_lowering_rule;" + ";".join(lowering_reasons)
    if plan.get("reason") != "ready":
        return str(plan.get("reason") or "plan_not_ready")
    if compiler_output is None:
        return "compiler_output_missing"
    if not actions:
        return "compiler_no_actions"
    if getattr(compiler_output, "escape_hatch_log", None):
        return "compiler_escape_hatch_requires_review"
    if any(getattr(action, "used_escape_hatch", False) for action in actions):
        return "action_escape_hatch_requires_review"
    if len(actions) > MAX_ACTION_COUNT_PER_CASE:
        return "action_count_above_limit"
    return "compiler_not_actionable"


def _contract_repair_program(group: GroupSummary) -> List[Dict[str, Any]]:
    contract = _payload(getattr(group, "trigger_contract", None))
    action_contract = _payload(contract.get("action_contract"))
    steps = action_contract.get("repair_program") or []
    return [dict(step) for step in steps if isinstance(step, dict)]


def _contract_program_issues(
    group: GroupSummary,
    *,
    member_case_views: Optional[Mapping[str, Any]] = None,
    require_member_coverage: bool = True,
) -> List[str]:
    issues: List[str] = []
    program = getattr(group.instantiation_program, "synthesized_program", None)
    if program is None:
        issues.append("missing_synthesized_program")
    elif not program.ops:
        issues.append("empty_synthesized_program")
    if require_member_coverage:
        coverage = validate_group_program_coverage(
            group,
            member_case_views=member_case_views,
        )
        if coverage.blockers:
            issues.extend(coverage.blockers)
    steps = _contract_repair_program(group)
    if not steps:
        issues.append("missing_group_repair_program_steps")
    for step in steps:
        origin = str(step.get("origin") or "")
        source = str(step.get("extraction_source") or "")
        if origin not in {"case_extracted", "group_merged"}:
            issues.append(f"invalid_repair_step_origin:{origin}")
        if source not in {"llm_explicit", "group_merged"}:
            issues.append(f"non_explicit_repair_step:{source}")
    return issues


def _member_case_views_for_group(
    *,
    group: GroupSummary,
    case_loader: CaseLoader,
    db_path: str,
    database_dir: Optional[str],
) -> Dict[str, Any]:
    """Build runtime-visible member views for promotion coverage gates."""
    try:
        from method.EEA.rulebook.common.io.db_schema_access import SqliteDBSchemaAccess
        from method.EEA.rulebook.common.runtime.runtime import build_runtime_case_view, _memory_schema_tables
    except Exception:  # pragma: no cover - defensive import fallback.
        return {}

    try:
        access = SqliteDBSchemaAccess(
            db_id=group.db_id,
            db_path=db_path,
            database_dir=database_dir,
        )
    except Exception:
        return {}
    memory_tables = _memory_schema_tables([group])
    out: Dict[str, Any] = {}
    for case_id in group.case_ids:
        case_key = str(case_id)
        case = case_loader(case_key)
        if not case:
            continue
        candidates = list(case.get("candidates") or [])
        if not candidates:
            continue
        try:
            out[case_key] = build_runtime_case_view(
                db_id=group.db_id,
                case_id=case_key,
                question=str(case.get("question") or ""),
                evidence=str(case.get("evidence") or ""),
                pred_top1_sql=str(candidates[0]),
                c0_candidate_sqls=[str(candidate) for candidate in candidates],
                access=access,
                t_mem=memory_tables,
                allow_two_hop_justifications=[group.group_id],
                candidate_set_size=len(candidates),
            )
        except Exception:
            continue
    return out


def _pattern_precondition_self_recall(
    group: GroupSummary,
    member_case_views: Mapping[str, Any],
) -> Dict[str, Any]:
    contract = _payload(getattr(group.instantiation_program, "pattern_recognition_contract", None))
    if not contract:
        return {
            "schema_version": "pattern-precondition-self-recall-v1",
            "rate": 0.0,
            "matched_case_ids": [],
            "missed_case_ids": [str(case_id) for case_id in (group.case_ids or [])],
            "reason": "missing_pattern_recognition_contract",
        }
    rows: List[Dict[str, Any]] = []
    matched: List[str] = []
    missed: List[str] = []
    for case_id in group.case_ids or []:
        case_key = str(case_id)
        case_view = member_case_views.get(case_key)
        if case_view is None:
            missed.append(case_key)
            rows.append(
                {
                    "case_id": case_key,
                    "matched": False,
                    "reason": "missing_member_case_view",
                }
            )
            continue
        audit, ok, blockers = _evaluate_pattern_pre_condition(
            group=group,
            case_view=case_view,
        )
        if ok:
            matched.append(case_key)
        else:
            missed.append(case_key)
        rows.append(
            {
                "case_id": case_key,
                "matched": bool(ok),
                "blockers": [str(item) for item in blockers],
                "audit": audit,
            }
        )
    total = len(rows)
    rate = len(matched) / max(total, 1)
    return {
        "schema_version": "pattern-precondition-self-recall-v1",
        "rate": rate,
        "threshold": PATTERN_PRE_CONDITION_SELF_RECALL_MIN,
        "matched_case_ids": matched,
        "missed_case_ids": missed,
        "rows": rows,
        "reason": "ok" if rate >= PATTERN_PRE_CONDITION_SELF_RECALL_MIN else "pre_condition_self_recall_below_threshold",
    }


def _slot_signature_key(group: GroupSummary) -> str:
    rows = []
    for slot in group.instantiation_program.slots or []:
        payload = _payload(slot)
        rows.append(
            (
                str(payload.get("kind") or ""),
                bool(payload.get("required")),
                tuple(sorted(str(role) for role in payload.get("allowed_role_families") or [])),
            )
        )
    return repr(sorted(rows))


def _member_slot_signature_diverged(
    group: GroupSummary,
    source_singletons_by_case_id: Mapping[str, GroupSummary],
) -> bool:
    keys = {
        _slot_signature_key(source_singletons_by_case_id[str(case_id)])
        for case_id in group.case_ids
        if str(case_id) in source_singletons_by_case_id
    }
    return len(keys) > 1


def _training_memory_for_holdout(
    *,
    group: GroupSummary,
    source_singletons_by_case_id: Mapping[str, GroupSummary],
    holdout_case_id: str,
) -> Tuple[Optional[GroupSummary], str]:
    training = [
        source_singletons_by_case_id[str(case_id)]
        for case_id in group.case_ids
        if str(case_id) != str(holdout_case_id)
        and str(case_id) in source_singletons_by_case_id
    ]
    if not training:
        return None, "no_training_members_after_holdout"
    if len(training) == 1:
        singleton = training[0].model_copy(deep=True)
        singleton.runtime_usable = True
        ensure_materialized_trigger_contract(singleton)
        return singleton, "peer_singleton"
    pattern_training_memory = build_family_from_groups(
        training,
        runtime_usable=True,
        require_effect_program=True,
        group_type=GroupType.PATTERN,
    )
    return pattern_training_memory, "leave_one_out_pattern"


def _action_selection_origins(actions: Sequence[Any]) -> List[str]:
    origins = [
        str(getattr(action, "selection_origin", None) or "none")
        for action in actions or []
    ]
    return sorted({origin for origin in origins if origin})


def _primary_selection_origin(actions: Sequence[Any]) -> str:
    origins = _action_selection_origins(actions)
    if not origins:
        return "none"
    if len(origins) == 1:
        return origins[0]
    return "mixed"


def _action_bundle_ids(actions: Sequence[Any]) -> List[str]:
    values: set[str] = set()
    for action in actions or []:
        payload = _payload(action)
        args = _payload(payload.get("arguments"))
        for key in (
            "bundle_id",
            "bound_branch_id",
            "canonical_op_id",
            "op_id",
            "bundle_selection_key",
        ):
            value = str(args.get(key) or "").strip()
            if value:
                values.add(value)
    return sorted(values)


def _selected_branch_ids_from_plan(plan: Mapping[str, Any]) -> Dict[str, str]:
    trigger_result = plan.get("trigger_result")
    payload = _payload(trigger_result)
    return {
        str(group_id): str(branch_id)
        for group_id, branch_id in (payload.get("selected_branch_ids") or {}).items()
        if str(group_id) and str(branch_id)
    }


def _formal_replay_row_passed(row: Mapping[str, Any]) -> bool:
    """Whether one formal replay row is usable as independent support evidence."""
    return bool(
        row.get("eligible_for_formal_promotion")
        and not row.get("holdout_in_training")
        and not row.get("comparison_unknown")
        and not _row_comparison_unknown_reasons(row)
        and row.get("compile_pass")
        and int(row.get("action_count") or 0) > 0
        and row.get("improved")
        and not row.get("regressed")
    )


def _branch_bundle_ids(branch: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    for item in branch.get("bundle_ids") or []:
        value = str(item or "").strip()
        if value:
            values.add(value)
    for key in ("bundle_id", "branch_id"):
        value = str(branch.get(key) or "").strip()
        if value:
            values.add(value)
    return values


def _branch_id(branch: Mapping[str, Any]) -> str:
    return str(branch.get("branch_id") or branch.get("bundle_id") or "").strip()


def _row_selects_branch(row: Mapping[str, Any], branch: Mapping[str, Any]) -> bool:
    branch_id = _branch_id(branch)
    bundle_ids = _branch_bundle_ids(branch)
    selected_branch_id = str(row.get("selected_branch_id") or "").strip()
    if branch_id and selected_branch_id == branch_id:
        return True
    selected_branch_ids = {
        str(value).strip()
        for value in (_payload(row.get("selected_branch_ids")) or {}).values()
        if str(value).strip()
    }
    if branch_id and branch_id in selected_branch_ids:
        return True
    selected_bundle_ids = {
        str(value).strip()
        for value in (row.get("selected_bundle_ids") or [])
        if str(value).strip()
    }
    return bool(bundle_ids and selected_bundle_ids and bundle_ids & selected_bundle_ids)


def _branch_replay_row_passed(row: Mapping[str, Any], branch: Mapping[str, Any]) -> bool:
    """Whether one branch-scoped member replay row proves positive repair value."""
    return bool(_branch_replay_row_runtime_safe(row, branch) and row.get("improved"))


def _branch_replay_row_runtime_safe(row: Mapping[str, Any], branch: Mapping[str, Any]) -> bool:
    """Whether one branch-scoped member replay row validates this runtime branch.

    Branch replay is not formal promotion evidence: it can replay a member
    through the branch-specific memory to validate that runtime branch selection,
    compiler binding, and rewrite all follow the same executable branch. A
    branch can be runtime-usable when all support members compile safely and at
    least one member shows improvement; it does not need every support member to
    improve, because some support rows may already be close to the gold form.
    """
    return bool(
        _row_selects_branch(row, branch)
        and not row.get("comparison_unknown")
        and not _row_comparison_unknown_reasons(row)
        and row.get("compile_pass")
        and int(row.get("action_count") or 0) > 0
        and not row.get("regressed")
    )


def _branch_replay_failure_reasons(
    row: Mapping[str, Any],
    branch: Mapping[str, Any],
) -> List[str]:
    reasons: List[str] = []
    if not _row_selects_branch(row, branch):
        reasons.append("branch_not_selected")
    if row.get("comparison_unknown") or _row_comparison_unknown_reasons(row):
        reasons.append("comparison_unknown")
    if not row.get("compile_pass"):
        reasons.append("compile_failed")
    if int(row.get("action_count") or 0) <= 0:
        reasons.append("no_actions")
    if row.get("regressed"):
        reasons.append("regressed")
    if not row.get("improved"):
        reasons.append("not_improved")
    return reasons or ["unknown"]


def _metrics_from_rows(rows: Sequence[Mapping[str, Any]], *, version: int) -> ReplayMetrics:
    rows = list(rows or [])
    sample_size = len(rows)
    compile_passes = [row for row in rows if row.get("compile_pass")]
    action_counts = [int(row.get("action_count") or 0) for row in compile_passes]
    unknown_rows = [
        row
        for row in rows
        if row.get("comparison_unknown") or _row_comparison_unknown_reasons(row)
    ]
    unknown_row_ids = {id(row) for row in unknown_rows}
    improved = [row for row in rows if row.get("improved") and id(row) not in unknown_row_ids]
    regressed = [row for row in rows if row.get("regressed") and id(row) not in unknown_row_ids]
    llm_rows = [
        row
        for row in rows
        if set(row.get("selection_origins") or []) == {"llm_selected"}
    ]
    deterministic_rows = [
        row
        for row in rows
        if set(row.get("selection_origins") or []) == {"deterministic_unique"}
    ]
    no_action_rows = [row for row in rows if int(row.get("action_count") or 0) == 0]
    formal_eligible = [row for row in rows if row.get("eligible_for_formal_promotion")]
    unknown_reasons: Counter[str] = Counter()
    for row in unknown_rows:
        for reason in _row_comparison_unknown_reasons(row) or ["unknown"]:
            unknown_reasons[str(reason)] += 1
    return ReplayMetrics(
        version=version,
        compile_coverage=(len(compile_passes) / sample_size if sample_size else 0.0),
        mean_action_count=(
            sum(action_counts) / len(action_counts) if action_counts else 0.0
        ),
        replay_improvement=(len(improved) / sample_size if sample_size else 0.0),
        replay_regression=(len(regressed) / sample_size if sample_size else 0.0),
        leave_one_out_done=all(
            str(row.get("replay_mode") or "") in {"leave_one_out_replay", "pairwise_cross_replay"}
            for row in rows
        )
        if rows
        else False,
        sample_size=sample_size,
        replay_improvement_llm_selected=(
            len([
                row
                for row in llm_rows
                if row.get("improved") and id(row) not in unknown_row_ids
            ])
            / sample_size
            if sample_size
            else 0.0
        ),
        replay_improvement_deterministic_unique=(
            len([
                row
                for row in deterministic_rows
                if row.get("improved") and id(row) not in unknown_row_ids
            ])
            / sample_size
            if sample_size
            else 0.0
        ),
        replay_improvement_total=(len(improved) / sample_size if sample_size else 0.0),
        fallback_selected_rate=(
            len(deterministic_rows) / sample_size if sample_size else 0.0
        ),
        llm_selected_rate=(len(llm_rows) / sample_size if sample_size else 0.0),
        no_action_rate=(len(no_action_rows) / sample_size if sample_size else 0.0),
        formal_eligible_sample_size=len(formal_eligible),
        comparison_unknown_count=len(unknown_rows),
        comparison_unknown_rate=(len(unknown_rows) / sample_size if sample_size else 0.0),
        comparison_unknown_reasons=dict(unknown_reasons),
    )


def _group_compiler_deterministic(group: GroupSummary) -> bool:
    contract = _payload(getattr(group, "trigger_contract", None))
    action_contract = _payload(contract.get("action_contract"))
    return bool(action_contract.get("compiler_deterministic"))


def _group_selection_policy(group: GroupSummary) -> str:
    contract = _payload(getattr(group, "trigger_contract", None))
    action_contract = _payload(contract.get("action_contract"))
    policy = str(action_contract.get("selection_policy") or "llm_required").strip()
    return policy if policy in {"llm_required", "deterministic_allowed", "deterministic_only"} else "llm_required"


def _replay_one_holdout(
    *,
    group: GroupSummary,
    source_singletons_by_case_id: Mapping[str, GroupSummary],
    holdout_case_id: str,
    case_loader: CaseLoader,
    db_path: str,
    database_dir: Optional[str],
    row_sample_limit: int,
    memory_override: Optional[GroupSummary] = None,
    memory_kind_override: Optional[str] = None,
    replay_mode: str = "leave_one_out_replay",
    training_case_ids: Optional[Sequence[str]] = None,
    holdout_in_training: bool = False,
    eligible_for_formal_promotion: bool = True,
    forced_branch_id: Optional[str] = None,
    forced_branch_bundle_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "holdout_case_id": str(holdout_case_id),
        "replay_mode": replay_mode,
        "training_case_ids": [str(case_id) for case_id in (training_case_ids or [])],
        "holdout_in_training": bool(holdout_in_training),
        "eligible_for_formal_promotion": bool(eligible_for_formal_promotion),
        "compile_pass": False,
        "action_count": 0,
        "selection_origin": "none",
        "selection_origins": [],
        "improved": False,
        "regressed": False,
        "c0_candidates": [],
        "c1_candidates": [],
        "c1_evaluation_policy": "oracle_union_preserve_c0_top1",
        "final_selected_origin": "c0_top1_if_equivalent_else_rewrite_if_equivalent",
        "memory_rewrite_broke_c0_top1": False,
    }
    if forced_branch_id:
        row["forced_branch_id"] = str(forced_branch_id)
        row["forced_branch_bundle_ids"] = [
            str(item) for item in (forced_branch_bundle_ids or []) if str(item)
        ]
    if memory_override is not None:
        memory = memory_override.model_copy(deep=True)
        memory.runtime_usable = True
        ensure_materialized_trigger_contract(memory)
        memory_kind = memory_kind_override or "memory_override"
    else:
        memory, memory_kind = _training_memory_for_holdout(
            group=group,
            source_singletons_by_case_id=source_singletons_by_case_id,
            holdout_case_id=str(holdout_case_id),
        )
    row["training_memory_kind"] = memory_kind
    if memory is None:
        row["reason"] = memory_kind
        return row
    memory_case_ids = {str(case_id) for case_id in (memory.case_ids or [])}
    actual_holdout_in_training = str(holdout_case_id) in memory_case_ids
    if actual_holdout_in_training:
        row["holdout_in_training"] = True
        row["eligible_for_formal_promotion"] = False
        row["protocol_violation"] = "holdout_present_in_training_memory"
    contract_validation_mode = (
        "branch_scoped_runtime"
        if str(memory_kind) == "branch_member_replay"
        else "group_member_coverage"
    )
    memory_issues = _contract_program_issues(
        memory,
        require_member_coverage=contract_validation_mode != "branch_scoped_runtime",
    )
    if memory_issues:
        row["reason"] = "training_memory_contract_invalid"
        row["contract_issues"] = memory_issues
        row["contract_issue_count"] = len(memory_issues)
        row["contract_validation_mode"] = contract_validation_mode
        return row
    row["contract_validation_mode"] = contract_validation_mode
    case = case_loader(str(holdout_case_id))
    if not case:
        row["reason"] = "missing_case_record"
        return row
    cache_key = _promotion_replay_cache_key(
        memory=memory,
        holdout_case_id=str(holdout_case_id),
        replay_mode=replay_mode,
        training_case_ids=[str(case_id) for case_id in (training_case_ids or [])],
        db_path=db_path,
        database_dir=database_dir,
        row_sample_limit=row_sample_limit,
        holdout_in_training=holdout_in_training,
        eligible_for_formal_promotion=eligible_for_formal_promotion,
        case_record=case,
    )
    cached = _PROMOTION_REPLAY_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)

    def finish(payload: Dict[str, Any]) -> Dict[str, Any]:
        _PROMOTION_REPLAY_CACHE[cache_key] = dict(payload)
        return payload

    candidates = list(case.get("candidates") or [])
    gold_sql = str(case.get("gold_sql") or "")
    if not candidates or not gold_sql:
        row["reason"] = "missing_candidates_or_gold"
        return finish(row)
    top1_sql = str(candidates[0])
    c0_candidate_rows: List[Dict[str, Any]] = []
    lightweight_branch_replay = (
        str(replay_mode) == "branch_member_replay"
        and str(memory_kind) == "branch_member_replay"
        and _branch_memory_lightweight_validated(memory, forced_branch_id=forced_branch_id)
    )
    pred_cmp = None
    if lightweight_branch_replay:
        c0_candidate_rows = [
            {
                "candidate_index": idx,
                "sql": str(sql),
                "status": "skipped_lightweight_validated_in_local_evolve",
                "comparison_unknown_reason": "",
                "vs_gold": {},
            }
            for idx, sql in enumerate(candidates)
        ]
    else:
        for idx, sql in enumerate(candidates):
            cmp = run_execution_comparison(
                db_path=db_path,
                pred_sql=str(sql),
                gold_sql=gold_sql,
                row_sample_limit=row_sample_limit,
            )
            cmp_payload = _payload(cmp)
            if idx == 0:
                pred_cmp = cmp
            c0_candidate_rows.append(
                {
                    "candidate_index": idx,
                    "sql": str(sql),
                    "status": _comparison_status(cmp_payload),
                    "comparison_unknown_reason": _comparison_unknown_reason(cmp_payload),
                    "vs_gold": cmp_payload,
                }
            )
    row["c0_candidates"] = c0_candidate_rows
    pred_cmp_payload = _payload(pred_cmp)
    pred_status = (
        "skipped_lightweight_validated_in_local_evolve"
        if lightweight_branch_replay
        else _comparison_status(pred_cmp_payload)
    )
    pred_unknown_reason = (
        ""
        if lightweight_branch_replay
        else _comparison_unknown_reason(pred_cmp_payload)
    )
    c0_oracle_status = (
        "equivalent"
        if any(_is_equivalent(item.get("status", "")) for item in c0_candidate_rows)
        else pred_status
    )
    c0_status = pred_status
    row["c0_status"] = c0_status
    row["c0_comparison_unknown_reason"] = pred_unknown_reason
    row["comparison_unknown"] = bool(pred_unknown_reason)
    row["comparison_unknown_reasons"] = [pred_unknown_reason] if pred_unknown_reason else []
    row["c0_selection_source"] = "c0_top1"
    row["c0_oracle_status"] = c0_oracle_status
    row["c0_oracle_selection_source"] = (
        "c0_any_candidate"
        if _is_equivalent(c0_oracle_status) and not _is_equivalent(pred_status)
        else "c0_top1"
    )
    row["c0_evaluation_policy"] = (
        "branch_member_replay_skip_sql_execution_for_lightweight_validated"
        if lightweight_branch_replay
        else "strict_top1_selected_candidate"
    )
    if lightweight_branch_replay:
        row["branch_lightweight_replay_skip"] = True
        row["branch_lightweight_replay_skip_reason"] = "lightweight_validated_in_local_evolve"
    temp_library = LibraryStateV2(db_id=group.db_id)
    if memory.group_type == GroupType.SINGLETON:
        temp_library.singletons = [memory]
    else:
        temp_library.patterns = [memory]

    try:
        plan = prepare_rewrite_plan(
            db_id=group.db_id,
            case_id=str(holdout_case_id),
            question=str(case.get("question") or ""),
            evidence=str(case.get("evidence") or ""),
            pred_top1_sql=top1_sql,
            c0_candidates=list(candidates),
            library=temp_library,
            db_path=db_path,
            database_dir=database_dir,
        )
    except Exception as exc:  # pragma: no cover - defensive validation path
        row["reason"] = f"prepare_exception:{type(exc).__name__}"
        return finish(row)

    compiler_output = plan.get("compiler_output")
    actions = list(getattr(compiler_output, "actions", []) or [])
    selection_origins = _action_selection_origins(actions)
    selected_branch_ids = _selected_branch_ids_from_plan(plan)
    selected_branch_values = sorted(set(selected_branch_ids.values()))
    selected_bundle_ids = _action_bundle_ids(actions)
    empty_reasons = _candidate_empty_reasons(plan.get("compiler_candidate_sets") or [])
    trigger_diagnostics = _replay_trigger_diagnostics(plan)
    row.update(
        {
            "plan_reason": plan.get("reason"),
            "rewrite_enabled_reason": trigger_diagnostics.get("rewrite_enabled_reason"),
            "trigger_blocker_counts": trigger_diagnostics.get("trigger_blocker_counts"),
            "top_candidate_reasons": trigger_diagnostics.get("top_candidate_reasons"),
            "replay_trigger_diagnostics": trigger_diagnostics,
            "source_memory_object": {
                "group_id": memory.group_id,
                "group_type": str(getattr(memory.group_type, "value", memory.group_type)),
                "case_ids": list(memory.case_ids),
                "support": memory.support,
            },
            "matched_group_ids": [
                matched.group_id for matched in (plan.get("matched_groups") or [])
            ],
            "selected_branch_ids": selected_branch_ids,
            "selected_branch_id": selected_branch_values[0] if len(selected_branch_values) == 1 else "",
            "selected_bundle_ids": selected_bundle_ids,
                "pred_status": pred_status,
                "c0_status": c0_status,
                "action_count": len(actions),
            "selection_origin": _primary_selection_origin(actions),
            "selection_origins": selection_origins,
            "selection_summary": _payload(getattr(compiler_output, "selection_summary", None)),
            "actions": [_payload(action) for action in actions],
            "compiler_candidate_empty_reasons": list(empty_reasons),
            "rewrite_status": "not_run",
            "c1_status": c0_status,
            "final_selection_source": (
                str(row.get("c0_selection_source") or "c0_retained")
                if _is_equivalent(c0_status)
                else "none_equivalent"
            ),
        }
    )
    if (
        plan.get("reason") != "ready"
        or compiler_output is None
        or not actions
        or getattr(compiler_output, "escape_hatch_log", None)
        or any(getattr(action, "used_escape_hatch", False) for action in actions)
        or len(actions) > MAX_ACTION_COUNT_PER_CASE
    ):
        row["reason"] = _not_actionable_reason(
            plan=plan,
            compiler_output=compiler_output,
            actions=actions,
            empty_reasons=empty_reasons,
        )
        return finish(row)

    row["compile_pass"] = True
    if lightweight_branch_replay:
        row["reason"] = "lightweight_validated_in_local_evolve"
        row["skip_reason"] = "lightweight_validated_in_local_evolve"
        row["rewrite_status"] = "skipped_lightweight_validated_in_local_evolve"
        row["c1_status"] = row.get("c0_status", "skipped_lightweight_validated_in_local_evolve")
        row["improved"] = False
        row["regressed"] = False
        return finish(row)
    try:
        rewrite = rewrite_one_candidate(
            question=str(case.get("question") or ""),
            evidence=str(case.get("evidence") or ""),
            pred_sql=top1_sql,
            compiler_output=compiler_output,
            local_schema_view=plan["case_view"].local_schema_view,
            natural_language_hint=plan.get("repair_brief", "")
            or plan.get("instantiated_hint", "")
            or "",
        )
        rewrite_sql = str(rewrite.get("rewrite_sql") or "")
        rewrite_cmp = (
            run_execution_comparison(
                db_path=db_path,
                pred_sql=rewrite_sql,
                gold_sql=gold_sql,
                row_sample_limit=row_sample_limit,
            )
            if rewrite_sql
            else None
        )
        rewrite_cmp_payload = _payload(rewrite_cmp)
        rewrite_status = _comparison_status(rewrite_cmp_payload)
        rewrite_unknown_reason = _comparison_unknown_reason(rewrite_cmp_payload)
        unknown_reasons = sorted(
            {
                reason
                for reason in [pred_unknown_reason, rewrite_unknown_reason]
                if reason
            }
        )
        c1_equivalent = _is_equivalent(c0_status) or _is_equivalent(rewrite_status)
        c1_status = "equivalent" if c1_equivalent else rewrite_status
        row.update(
            {
                "rewrite_sql": rewrite_sql,
                "rewrite_status": rewrite_status,
                "rewrite_comparison_unknown_reason": rewrite_unknown_reason,
                "comparison_unknown": bool(unknown_reasons),
                "comparison_unknown_reasons": unknown_reasons,
                "c1_status": c1_status,
                "c1_candidates": [
                    {
                        "origin": "c0_original",
                        "candidate_index": item.get("candidate_index"),
                        "status": item.get("status"),
                        "sql": item.get("sql"),
                    }
                    for item in c0_candidate_rows
                ]
                + [
                    {
                        "origin": "rewrite_from_action",
                        "status": rewrite_status,
                        "sql": rewrite_sql,
                    },
                ],
                "contract_steps_applied": list(
                    rewrite.get("contract_steps_applied") or []
                ),
                "dependency_repairs_applied": list(
                    rewrite.get("dependency_repairs_applied") or []
                ),
                "rewrite_realization_origin": rewrite_realization_origin_from_result(
                    rewrite
                ),
            }
        )
        row["improved"] = (
            not unknown_reasons and not _is_equivalent(c0_status) and c1_equivalent
        )
        row["regressed"] = (
            not unknown_reasons and _is_equivalent(c0_status) and not c1_equivalent
        )
        row["memory_rewrite_broke_c0_top1"] = bool(
            not unknown_reasons
            and _is_equivalent(c0_status)
            and rewrite_status
            and not _is_equivalent(rewrite_status)
        )
        row["final_selection_source"] = (
            str(row.get("c0_selection_source") or "c0_retained")
            if _is_equivalent(c0_status)
            else "rewrite_from_action"
            if _is_equivalent(rewrite_status)
            else "none_equivalent"
        )
    except Exception as exc:  # pragma: no cover - defensive validation path
        row["reason"] = f"rewrite_exception:{type(exc).__name__}"
    return finish(row)


def run_promotion_test(
    *,
    group: GroupSummary,
    source_singletons_by_case_id: Mapping[str, GroupSummary],
    case_loader: CaseLoader,
    db_path: str,
    database_dir: Optional[str] = None,
    row_sample_limit: int = 5000,
) -> PromotionTestResult:
    """Run leave-one-out replay promotion test for one strict pattern candidate."""
    member_case_views = _member_case_views_for_group(
        group=group,
        case_loader=case_loader,
        db_path=db_path,
        database_dir=database_dir,
    )
    precondition_self_recall = _pattern_precondition_self_recall(
        group,
        member_case_views,
    )
    group_issues = _contract_program_issues(
        group,
        member_case_views=member_case_views,
    )
    shared_minimal_repair_interface = not group_issues
    group_program_coverage = validate_group_program_coverage(
        group,
        member_case_views=member_case_views,
    )
    shared_instantiation_program = bool(
        group.instantiation_program.shared
        and group.instantiation_program.synthesized_program is not None
        and bool(group.instantiation_program.synthesized_program.ops)
    )
    branch_rule_count = len(group.instantiation_program.branch_rules or [])
    slot_signature_diverged = _member_slot_signature_diverged(
        group, source_singletons_by_case_id
    )
    loo_rows = [
        _replay_one_holdout(
            group=group,
            source_singletons_by_case_id=source_singletons_by_case_id,
            holdout_case_id=str(case_id),
            case_loader=case_loader,
            db_path=db_path,
            database_dir=database_dir,
            row_sample_limit=row_sample_limit,
            replay_mode="leave_one_out_replay",
            training_case_ids=[
                str(peer_case_id)
                for peer_case_id in group.case_ids
                if str(peer_case_id) != str(case_id)
            ],
            holdout_in_training=False,
            eligible_for_formal_promotion=True,
        )
        for case_id in group.case_ids
    ]
    full_group_memory = group.model_copy(deep=True)
    full_group_memory.runtime_usable = True
    ensure_materialized_trigger_contract(full_group_memory)
    full_group_rows = [
        _replay_one_holdout(
            group=group,
            source_singletons_by_case_id=source_singletons_by_case_id,
            holdout_case_id=str(case_id),
            case_loader=case_loader,
            db_path=db_path,
            database_dir=database_dir,
            row_sample_limit=row_sample_limit,
            memory_override=full_group_memory,
            memory_kind_override="full_group_member_replay",
            replay_mode="full_group_member_replay",
            training_case_ids=[str(peer_case_id) for peer_case_id in group.case_ids],
            holdout_in_training=True,
            eligible_for_formal_promotion=False,
        )
        for case_id in group.case_ids
    ]
    branch_member_rows: List[Dict[str, Any]] = []
    for branch in _runtime_branch_rows_for_group(group):
        branch_id = _branch_id(branch)
        support_case_ids = _runtime_branch_support(branch, group.case_ids)
        if not branch_id or not support_case_ids:
            continue
        branch_memory = _branch_scoped_memory(
            group,
            branch,
            support_case_ids,
        )
        branch_bundle_ids = sorted(_branch_bundle_ids(branch))
        for case_id in support_case_ids:
            branch_member_rows.append(
                _replay_one_holdout(
                    group=group,
                    source_singletons_by_case_id=source_singletons_by_case_id,
                    holdout_case_id=str(case_id),
                    case_loader=case_loader,
                    db_path=db_path,
                    database_dir=database_dir,
                    row_sample_limit=row_sample_limit,
                    memory_override=branch_memory,
                    memory_kind_override="branch_member_replay",
                    replay_mode="branch_member_replay",
                    training_case_ids=support_case_ids,
                    holdout_in_training=True,
                    eligible_for_formal_promotion=False,
                    forced_branch_id=branch_id,
                    forced_branch_bundle_ids=branch_bundle_ids,
                )
            )
    pairwise_cross_rows: List[Dict[str, Any]] = []
    if int(group.support or 0) == 2:
        for case_id in group.case_ids:
            peer_ids = [
                str(peer_case_id)
                for peer_case_id in group.case_ids
                if str(peer_case_id) != str(case_id)
            ]
            if not peer_ids:
                continue
            peer_singleton = source_singletons_by_case_id.get(peer_ids[0])
            if peer_singleton is None:
                pairwise_cross_rows.append(
                    {
                        "holdout_case_id": str(case_id),
                        "replay_mode": "pairwise_cross_replay",
                        "training_case_ids": peer_ids,
                        "holdout_in_training": False,
                        "eligible_for_formal_promotion": True,
                        "compile_pass": False,
                        "action_count": 0,
                        "selection_origin": "none",
                        "selection_origins": [],
                        "improved": False,
                        "regressed": False,
                        "reason": "missing_peer_singleton_for_pairwise_cross_replay",
                    }
                )
                continue
            pairwise_cross_rows.append(
                _replay_one_holdout(
                    group=group,
                    source_singletons_by_case_id=source_singletons_by_case_id,
                    holdout_case_id=str(case_id),
                    case_loader=case_loader,
                    db_path=db_path,
                    database_dir=database_dir,
                    row_sample_limit=row_sample_limit,
                    memory_override=peer_singleton,
                    memory_kind_override="pairwise_cross_replay",
                    replay_mode="pairwise_cross_replay",
                    training_case_ids=peer_ids,
                    holdout_in_training=False,
                    eligible_for_formal_promotion=True,
                )
            )

    # support=2 has no independent heldout pattern to build; pairwise cross
    # replay is the formal evidence path. support>=3 uses LOO for runtime as
    # well, avoiding self-confirmation when enough members exist.
    rows = full_group_rows if int(group.support or 0) <= 2 else loo_rows
    runtime_family_evidence_mode = (
        "full_group_smoke_only"
        if int(group.support or 0) <= 2
        else "leave_one_out_replay"
    )
    formal_rows = pairwise_cross_rows if int(group.support or 0) == 2 else loo_rows
    metrics = _metrics_from_rows(rows, version=int(group.version or 0))
    formal_metrics = _metrics_from_rows(formal_rows, version=int(group.version or 0))
    formal_replay_modes = sorted(
        {str(row.get("replay_mode") or "") for row in formal_rows if row.get("replay_mode")}
    )
    formal_eligible_replay_rows = len(
        [row for row in formal_rows if row.get("eligible_for_formal_promotion")]
    )
    support_protocol_passed = bool(
        formal_rows
        and formal_eligible_replay_rows == len(formal_rows)
        and all(not row.get("holdout_in_training") for row in formal_rows)
        and all(_formal_replay_row_passed(row) for row in formal_rows)
        and (
            (
                int(group.support or 0) == 2
                and len(formal_rows) == 2
                and set(formal_replay_modes) == {"pairwise_cross_replay"}
            )
            or (
                int(group.support or 0) > 2
                and len(formal_rows) == int(group.support or 0)
                and set(formal_replay_modes) == {"leave_one_out_replay"}
            )
        )
    )
    sample_size = len(rows)
    action_counts = [
        int(row.get("action_count") or 0)
        for row in rows
        if row.get("compile_pass")
    ]
    compile_success_by_member = {
        str(row.get("holdout_case_id")): bool(row.get("compile_pass"))
        for row in rows
        if row.get("holdout_case_id") is not None
    }
    failed_members = [
        str(row.get("holdout_case_id"))
        for row in rows
        if row.get("holdout_case_id") is not None and not row.get("compile_pass")
    ]
    failure_reasons = {
        str(row.get("holdout_case_id")): str(row.get("reason") or row.get("plan_reason") or "compile_failed")
        for row in rows
        if row.get("holdout_case_id") is not None and not row.get("compile_pass")
    }
    replay_program_coverage = ProgramCoverage(
        total_cases=sample_size,
        covered_case_ids=[
            str(row.get("holdout_case_id"))
            for row in rows
            if row.get("holdout_case_id") is not None and row.get("compile_pass")
        ],
        uncovered_case_ids=failed_members,
        compile_coverage=metrics.compile_coverage,
        blockers=[] if not failed_members else ["member_candidate_binding_failed"],
        per_case={
            str(row.get("holdout_case_id")): {
                "covered": bool(row.get("compile_pass")),
                "action_count": int(row.get("action_count") or 0),
                "reason": row.get("reason") or row.get("plan_reason"),
                "training_memory_kind": row.get("training_memory_kind"),
            }
            for row in rows
            if row.get("holdout_case_id") is not None
        },
        compile_success_by_member=compile_success_by_member,
        failed_members=failed_members,
        failure_reasons=failure_reasons,
        mean_action_count=metrics.mean_action_count,
        core_op_coverage=metrics.compile_coverage,
        static_program_coverage=float(
            getattr(group_program_coverage, "static_program_coverage", 0.0)
            or getattr(group_program_coverage, "compile_coverage", 0.0)
            or 0.0
        ),
        runtime_binding_coverage=metrics.compile_coverage,
        member_candidate_coverage=metrics.compile_coverage,
        static_blockers=list(getattr(group_program_coverage, "static_blockers", []) or []),
        runtime_binding_blockers={} if not failed_members else {
            case_id: failure_reasons.get(case_id, "compile_failed")
            for case_id in failed_members
        },
    )
    reasons: List[str] = []
    soft_reasons: List[str] = []
    if group_issues:
        reasons.extend(group_issues)
    if not shared_instantiation_program:
        reasons.append("shared_instantiation_program_false")
    runtime_program_min = (
        1.0
        if int(group.support or 0) <= 2
        else RUNTIME_FAMILY_MIN_COMPILE_COVERAGE
    )
    if group_program_coverage.compile_coverage < runtime_program_min:
        reasons.append(
            "synthesized_program_coverage_below_runtime_threshold:"
            f"{group_program_coverage.compile_coverage:.3f}<{runtime_program_min:.3f}"
        )
    if branch_rule_count > RISK_HIGH_BRANCH_COUNT:
        reasons.append("branch_rules_exceed_split_threshold")
    if metrics.compile_coverage < RUNTIME_FAMILY_MIN_COMPILE_COVERAGE:
        reasons.append("compile_coverage_below_runtime_threshold")
    if metrics.mean_action_count > MAX_ACTION_COUNT_PER_CASE:
        reasons.append("mean_action_count_above_limit")
    if metrics.replay_improvement < RUNTIME_FAMILY_MIN_REPLAY_IMPROVEMENT:
        reasons.append("replay_improvement_below_runtime_threshold")
    if metrics.replay_regression > MAX_REPLAY_REGRESSION:
        reasons.append("replay_regression_above_limit")
    if metrics.comparison_unknown_count:
        soft_reasons.append(
            "replay_comparison_unknown_present:"
            f"count={metrics.comparison_unknown_count};"
            f"reasons={metrics.comparison_unknown_reasons}"
        )
    if len(set(action_counts)) > 1:
        # Action drift means the interface is not stable enough for a formal
        # pattern. Keep it visible to apply_promotion_decision instead of hiding
        # branch instability behind average replay metrics.
        soft_reasons.append("action_count_drift_detected")
    if group.replay_history:
        previous = group.replay_history[-1]
        if (
            metrics.compile_coverage < previous.compile_coverage
            or metrics.replay_improvement < previous.replay_improvement
            or metrics.replay_regression > previous.replay_regression
            ):
            reasons.append("version_replay_metrics_declined")
    formal_blockers: List[str] = []
    if (
        float(precondition_self_recall.get("rate") or 0.0)
        < PATTERN_PRE_CONDITION_SELF_RECALL_MIN
    ):
        formal_blockers.append(
            "pre_condition_self_recall_below_threshold:"
            f"{float(precondition_self_recall.get('rate') or 0.0):.3f}<"
            f"{PATTERN_PRE_CONDITION_SELF_RECALL_MIN:.3f}"
        )
    compiler_deterministic = _group_compiler_deterministic(group)
    formal_action_counts = [
        int(row.get("action_count") or 0)
        for row in formal_rows
        if row.get("compile_pass")
    ]
    if int(group.support or 0) == 2 and len(pairwise_cross_rows) < 2:
        formal_blockers.append("support2_pairwise_cross_replay_incomplete")
    if not support_protocol_passed:
        formal_blockers.append("formal_support_protocol_not_passed")
    if formal_metrics.sample_size == 0:
        formal_blockers.append("formal_replay_missing")
    if formal_metrics.compile_coverage < PATTERN_PROMOTION_MIN_COMPILE_COVERAGE:
        formal_blockers.append("formal_compile_coverage_below_pattern_threshold")
    if formal_metrics.replay_regression > MAX_REPLAY_REGRESSION:
        formal_blockers.append("formal_replay_regression_above_limit")
    if formal_metrics.comparison_unknown_count:
        formal_blockers.append(
            "formal_replay_comparison_unknown:"
            f"count={formal_metrics.comparison_unknown_count};"
            f"reasons={formal_metrics.comparison_unknown_reasons}"
        )
    if formal_metrics.mean_action_count > MAX_ACTION_COUNT_PER_CASE:
        formal_blockers.append("formal_mean_action_count_above_limit")
    strict_llm_improvement_ok = (
        formal_metrics.replay_improvement_llm_selected
        >= PATTERN_PROMOTION_MIN_REPLAY_IMPROVEMENT
    )
    deterministic_exception_ok = (
        compiler_deterministic
        and _group_selection_policy(group) == "deterministic_only"
        and formal_metrics.replay_improvement_deterministic_unique
        >= PATTERN_PROMOTION_MIN_REPLAY_IMPROVEMENT
    )
    if not (strict_llm_improvement_ok or deterministic_exception_ok):
        formal_blockers.append(
            "formal_replay_improvement_below_pattern_threshold:"
            f"llm={formal_metrics.replay_improvement_llm_selected:.3f};"
            f"deterministic={formal_metrics.replay_improvement_deterministic_unique:.3f};"
            f"compiler_deterministic={compiler_deterministic}"
        )
    if len(set(formal_action_counts)) > 1:
        formal_blockers.append("formal_action_count_drift_detected")
    eligible = not reasons
    reason_items = reasons + soft_reasons
    return PromotionTestResult(
        group_id=group.group_id,
        eligible=eligible,
        reason=";".join(reason_items) if reason_items else "pattern_replay_passed",
        replay_metrics=metrics,
        shared_minimal_repair_interface=shared_minimal_repair_interface,
        shared_instantiation_program=shared_instantiation_program,
        program_coverage=replay_program_coverage,
        formal_replay_metrics=formal_metrics,
        runtime_family_evidence_mode=runtime_family_evidence_mode,
        formal_promotion_blocker=(
            ";".join(formal_blockers) if formal_blockers else None
        ),
        support_protocol_passed=support_protocol_passed,
        formal_replay_modes=formal_replay_modes,
        formal_eligible_replay_rows=formal_eligible_replay_rows,
        leave_one_out_results=rows
        + [
            {
                "diagnostic": "leave_one_out_results",
                "used_for_metrics": int(group.support or 0) > 2,
                "used_for_formal_promotion": int(group.support or 0) > 2,
                "rows": loo_rows,
            },
            {
                "diagnostic": "pairwise_cross_replay",
                "used_for_metrics": False,
                "used_for_formal_promotion": int(group.support or 0) == 2,
                "rows": pairwise_cross_rows,
            },
            {
                "diagnostic": "full_group_member_replay",
                "used_for_metrics": int(group.support or 0) <= 2,
                "used_for_formal_promotion": False,
                "rows": full_group_rows,
            },
            {
                "diagnostic": "branch_member_replay",
                "used_for_metrics": False,
                "used_for_formal_promotion": False,
                "used_for_branch_runtime": True,
                "rows": branch_member_rows,
            },
        ]
        + [
            {
                "diagnostic": "slot_signature_divergence",
                "present": slot_signature_diverged,
                "blocking": False,
            },
            {
                "diagnostic": "pattern_pre_condition_self_recall",
                "blocking": (
                    float(precondition_self_recall.get("rate") or 0.0)
                    < PATTERN_PRE_CONDITION_SELF_RECALL_MIN
                ),
                **precondition_self_recall,
            }
        ],
    )


def _formal_support_shape_ok(group: GroupSummary, result: PromotionTestResult) -> bool:
    """Defense-in-depth for deserialized/manually assembled promotion results."""
    if not result.support_protocol_passed:
        return False
    support = int(group.support or 0)
    formal_metrics = result.formal_replay_metrics or result.replay_metrics
    modes = {str(mode) for mode in (result.formal_replay_modes or []) if str(mode)}
    if support == 2:
        expected_count = 2
        expected_modes = {"pairwise_cross_replay"}
    elif support > 2:
        expected_count = support
        expected_modes = {"leave_one_out_replay"}
    else:
        return False
    return bool(
        result.formal_eligible_replay_rows == expected_count
        and int(formal_metrics.sample_size or 0) == expected_count
        and modes == expected_modes
    )


def _pattern_has_admission_evidence(group: GroupSummary) -> bool:
    signals = _payload(getattr(group, "formation_signals", None))
    admission = _payload(signals.get("pattern_admission"))
    if not admission:
        return False
    return bool(
        admission.get("admit_pattern")
        or admission.get("accepted_case_ids")
        or admission.get("branch_specs")
    )


def _pattern_has_synthesized_program(group: GroupSummary) -> bool:
    program = getattr(getattr(group, "instantiation_program", None), "synthesized_program", None)
    return program is not None


def _runtime_visible_under_replay_audit_policy(group: GroupSummary) -> bool:
    if group.group_type == GroupType.PATTERN and len(group.case_ids or []) >= PROMOTION_SUPPORT_MIN:
        return True
    if group.group_type != GroupType.FAMILY:
        return False
    return bool(_pattern_has_synthesized_program(group) or _pattern_has_admission_evidence(group))


def _runtime_branch_support(branch: Dict[str, Any], fallback_case_ids: Sequence[str]) -> List[str]:
    support = [str(case_id) for case_id in (branch.get("support_case_ids") or []) if str(case_id)]
    _ = fallback_case_ids
    return support


def _runtime_branch_rows_for_group(group: GroupSummary) -> List[Dict[str, Any]]:
    program = getattr(group.instantiation_program, "synthesized_program", None)
    envelope = _payload(getattr(program, "program_envelope", None))
    return [
        _payload(branch)
        for branch in (envelope.get("runtime_branches") or envelope.get("lowering_branches") or [])
        if _payload(branch)
    ]


def _branch_memory_lightweight_validated(
    group: GroupSummary,
    *,
    forced_branch_id: Optional[str] = None,
) -> bool:
    for branch in _runtime_branch_rows_for_group(group):
        if forced_branch_id and _branch_id(branch) != str(forced_branch_id):
            continue
        policy = str(branch.get("runtime_validation_policy") or "")
        if bool(branch.get("cross_case_replay_pending")) and "lightweight" in policy:
            return True
        if policy in {
            "local_evolve_lightweight_binder_gated",
            "extended_in_place_lightweight_signal_gated",
        }:
            return True
    return False


def _branch_scoped_memory(
    group: GroupSummary,
    branch: Mapping[str, Any],
    support_case_ids: Sequence[str],
) -> GroupSummary:
    branch_payload = dict(branch)
    branch_payload["runtime_usable"] = True
    branch_payload["runtime_blockers"] = []
    memory = _filter_group_to_runtime_branch(group.model_copy(deep=True), branch_payload)
    memory.case_ids = [str(case_id) for case_id in support_case_ids]
    memory.support = len(memory.case_ids)
    memory.runtime_usable = True
    ensure_materialized_trigger_contract(memory)
    return memory


def _branch_replay_rows_from_result(result: PromotionTestResult) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for row in result.leave_one_out_results or []:
        payload = _payload(row)
        if payload.get("diagnostic") == "branch_member_replay":
            for nested in payload.get("rows") or []:
                nested_payload = _payload(nested)
                if str(nested_payload.get("replay_mode") or "") == "branch_member_replay":
                    rows.append(nested_payload)
            continue
        if str(payload.get("replay_mode") or "") == "branch_member_replay":
            rows.append(payload)
    deduped: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("forced_branch_id") or row.get("selected_branch_id") or ""),
            str(row.get("holdout_case_id") or ""),
            str(row.get("replay_mode") or ""),
        )
        if key[1]:
            deduped[key] = row
    return list(deduped.values())


def _branch_rows_for_branch(
    branch: Mapping[str, Any],
    branch_replay_rows: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    branch_id = _branch_id(branch)
    bundle_ids = _branch_bundle_ids(branch)
    out: List[Dict[str, Any]] = []
    for row in branch_replay_rows or []:
        payload = _payload(row)
        forced_branch_id = str(payload.get("forced_branch_id") or "").strip()
        forced_bundle_ids = {
            str(value).strip()
            for value in (payload.get("forced_branch_bundle_ids") or [])
            if str(value).strip()
        }
        if branch_id and forced_branch_id == branch_id:
            out.append(payload)
            continue
        if bundle_ids and forced_bundle_ids and bundle_ids & forced_bundle_ids:
            out.append(payload)
    return out


def _runtime_usable_branch_support_case_ids(group: GroupSummary) -> set[str]:
    program = getattr(group.instantiation_program, "synthesized_program", None)
    envelope = _payload(getattr(program, "program_envelope", None))
    case_ids: set[str] = set()
    for branch in envelope.get("runtime_branches") or []:
        payload = _payload(branch)
        if not bool(payload.get("runtime_usable")):
            continue
        case_ids.update(_runtime_branch_support(payload, group.case_ids))
    return case_ids


def _formal_rows_from_result(
    group: GroupSummary,
    result: PromotionTestResult,
) -> List[Dict[str, Any]]:
    support = int(group.support or 0)
    wanted_mode = "pairwise_cross_replay" if support == 2 else "leave_one_out_replay"
    rows: List[Dict[str, Any]] = []
    for row in result.leave_one_out_results or []:
        payload = _payload(row)
        if payload.get("diagnostic"):
            if str(payload.get("diagnostic")) in {
                "pairwise_cross_replay",
                "leave_one_out_results",
            }:
                for nested in payload.get("rows") or []:
                    nested_payload = _payload(nested)
                    if str(nested_payload.get("replay_mode") or "") == wanted_mode:
                        rows.append(nested_payload)
            continue
        if str(payload.get("replay_mode") or "") == wanted_mode:
            rows.append(payload)
    deduped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("holdout_case_id") or ""),
            str(row.get("replay_mode") or ""),
        )
        if key[0]:
            deduped[key] = row
    return list(deduped.values())


def _branch_replay_payload(
    *,
    branch_id: str,
    support_case_ids: Sequence[str],
    metrics: ReplayMetrics,
    replay_mode: str,
    failed_members: Sequence[str],
    blockers: Sequence[str],
) -> Dict[str, Any]:
    return {
        "schema_version": "branch-replay-metrics-v1",
        "branch_id": branch_id,
        "support_case_ids": [str(case_id) for case_id in support_case_ids],
        "compile_coverage": float(metrics.compile_coverage or 0.0),
        "replay_improvement": float(metrics.replay_improvement_llm_selected or metrics.replay_improvement or 0.0),
        "replay_regression": float(metrics.replay_regression or 0.0),
        "mean_action_count": float(metrics.mean_action_count or 0.0),
        "sample_size": int(metrics.sample_size or 0),
        "replay_mode": replay_mode,
        "failed_members": [str(item) for item in failed_members if str(item)],
        "blockers": [str(item) for item in blockers if str(item)],
    }


def _apply_branch_runtime_decision(
    group: GroupSummary,
    *,
    can_promote_pattern: bool,
    formal_metrics: ReplayMetrics,
    formal_blocker: Optional[str],
    branch_replay_rows: Sequence[Mapping[str, Any]],
) -> GroupSummary:
    program = getattr(group.instantiation_program, "synthesized_program", None)
    envelope = getattr(program, "program_envelope", None) if program is not None else None
    if envelope is None:
        return group
    envelope_payload = _payload(envelope)
    branches = [
        _payload(branch)
        for branch in (envelope_payload.get("runtime_branches") or envelope_payload.get("lowering_branches") or [])
        if _payload(branch)
    ]
    if not branches:
        return group.model_copy(
            update={
                "runtime_usable": False,
                "runtime_blockers": ["runtime_branch_contract_missing"],
            }
        )

    replay_mode = "branch_member_replay"
    updated_branches: List[Dict[str, Any]] = []
    usable_count = 0
    for branch in branches:
        branch_id = _branch_id(branch)
        support_case_ids = _runtime_branch_support(branch, group.case_ids)
        support_case_id_set = set(support_case_ids)
        branch_rows = [
            row
            for row in _branch_rows_for_branch(branch, branch_replay_rows)
            if str(row.get("holdout_case_id") or "") in support_case_id_set
        ]
        observed_support_ids = {
            str(row.get("holdout_case_id") or "")
            for row in branch_rows
            if str(row.get("holdout_case_id") or "")
        }
        missing_support_ids = sorted(support_case_id_set - observed_support_ids)
        extra_support_ids = sorted(observed_support_ids - support_case_id_set)
        branch_metrics = _metrics_from_rows(
            branch_rows,
            version=int(group.version or 0),
        )
        lightweight_skip_rows = [
            row
            for row in branch_rows
            if str(row.get("skip_reason") or "") == "lightweight_validated_in_local_evolve"
        ]
        failed_members = [
            str(row.get("holdout_case_id") or "")
            for row in branch_rows
            if row.get("holdout_case_id") is not None
            and not _branch_replay_row_runtime_safe(row, branch)
        ]
        improved_members = [
            str(row.get("holdout_case_id") or "")
            for row in branch_rows
            if row.get("holdout_case_id") is not None
            and _branch_replay_row_passed(row, branch)
        ]
        failed_member_reasons = {
            str(row.get("holdout_case_id") or ""): _branch_replay_failure_reasons(row, branch)
            for row in branch_rows
            if row.get("holdout_case_id") is not None
            and not _branch_replay_row_runtime_safe(row, branch)
        }
        blockers: List[str] = []
        if formal_blocker and "pre_condition_self_recall_below_threshold" in str(formal_blocker):
            blockers.append("pattern_pre_condition_self_recall_below_threshold")
        if not support_case_ids:
            blockers.append("branch_support_missing")
        if observed_support_ids != support_case_id_set:
            blockers.append(
                f"branch_runtime_replay_support_mismatch:{len(observed_support_ids)}/{len(support_case_ids)}"
            )
        if len(branch_rows) != len(observed_support_ids):
            blockers.append("branch_runtime_replay_duplicate_rows")
        if branch_rows and not all(_branch_replay_row_runtime_safe(row, branch) for row in branch_rows):
            blockers.append("branch_runtime_compile_or_regression_failed")
        if branch_metrics.replay_regression > MAX_REPLAY_REGRESSION:
            blockers.append("branch_replay_regression_above_limit")
        if branch_metrics.mean_action_count > MAX_ACTION_COUNT_PER_CASE:
            blockers.append("branch_action_count_above_limit")
        if branch_rows and not improved_members and len(lightweight_skip_rows) != len(branch_rows):
            blockers.append("branch_replay_no_improvement_evidence")
        branch_usable = bool(not blockers)
        pattern_blockers: List[str] = []
        if formal_blocker:
            pattern_blockers.append("pattern_formal_blocker:" + str(formal_blocker))
        if branch_usable:
            usable_count += 1
        updated = dict(branch)
        updated["schema_version"] = "runtime-branch-contract-v1"
        updated["branch_id"] = branch_id
        updated["support_case_ids"] = support_case_ids
        updated["runtime_usable"] = branch_usable
        updated["runtime_blockers"] = [] if branch_usable else blockers
        updated["pattern_level_blockers"] = pattern_blockers
        updated["runtime_validation_policy"] = "branch_support_all_safe_any_improved"
        updated["improved_support_case_ids"] = improved_members
        updated["failed_member_reasons"] = failed_member_reasons
        updated["missing_support_case_ids"] = missing_support_ids
        updated["extra_support_case_ids"] = extra_support_ids
        updated["replay_metrics"] = _branch_replay_payload(
            branch_id=branch_id,
            support_case_ids=support_case_ids,
            metrics=branch_metrics if branch_rows else formal_metrics,
            replay_mode=replay_mode,
            failed_members=failed_members,
            blockers=[*blockers, *pattern_blockers],
        )
        updated_branches.append(updated)

    envelope_payload["runtime_branches"] = updated_branches
    envelope_payload["branch_selection_contract"] = {
        **_payload(envelope_payload.get("branch_selection_contract")),
        "selection_unit": "runtime_branch",
        "runtime_usable_branch_count": usable_count,
    }
    updated_envelope = envelope.model_copy(update=envelope_payload) if hasattr(envelope, "model_copy") else envelope_payload
    updated_program = program.model_copy(update={"program_envelope": updated_envelope})
    updated_instantiation = group.instantiation_program.model_copy(
        update={"synthesized_program": updated_program}
    )
    return group.model_copy(
        update={
            "instantiation_program": updated_instantiation,
            "runtime_usable": bool(usable_count),
            "runtime_blockers": [] if usable_count else ["no_runtime_usable_branch"],
        }
    )


def apply_promotion_decision(
    group: GroupSummary,
    result: PromotionTestResult,
) -> GroupSummary:
    """Return a copy of ``group`` with replay-gated lifecycle metadata applied."""
    promoted = group.model_copy(deep=True)
    promoted.replay_history = list(promoted.replay_history) + [result.replay_metrics]
    if result.program_coverage is not None:
        current_status = getattr(promoted.instantiation_program, "shared_status", "none")
        if (
            result.program_coverage.compile_coverage > 0
            and current_status in {"none", "structural"}
            and promoted.instantiation_program.shared
        ):
            current_status = "compilable"
        promoted.instantiation_program = promoted.instantiation_program.model_copy(
            update={
                "program_coverage": result.program_coverage,
                "shared_status": current_status,
            }
        )
    promoted.last_updated_at = datetime.utcnow().isoformat()
    metrics = result.replay_metrics
    formal_metrics = result.formal_replay_metrics or result.replay_metrics
    reason_text = result.reason or ""
    action_count_drift = "action_count_drift_detected" in reason_text
    program = getattr(promoted.instantiation_program, "synthesized_program", None)
    unresolved_axes = list(getattr(program, "unresolved_variation_axes", []) or [])
    support = int(promoted.support or 0)
    formal_support_shape_ok = _formal_support_shape_ok(promoted, result)
    can_promote_pattern = (
        result.eligible
        and not result.formal_promotion_blocker
        and result.support_protocol_passed
        and formal_support_shape_ok
        and result.shared_minimal_repair_interface
        and result.shared_instantiation_program
        and not unresolved_axes
        and support >= PROMOTION_SUPPORT_MIN
        and formal_metrics.compile_coverage >= PATTERN_PROMOTION_MIN_COMPILE_COVERAGE
        and formal_metrics.replay_regression <= MAX_REPLAY_REGRESSION
        and formal_metrics.mean_action_count <= MAX_ACTION_COUNT_PER_CASE
        and (
            formal_metrics.replay_improvement_llm_selected
            >= PATTERN_PROMOTION_MIN_REPLAY_IMPROVEMENT
            or (
                _group_compiler_deterministic(promoted)
                and _group_selection_policy(promoted) == "deterministic_only"
                and formal_metrics.replay_improvement_deterministic_unique
                >= PATTERN_PROMOTION_MIN_REPLAY_IMPROVEMENT
            )
        )
        and not action_count_drift
    )
    promoted = _apply_branch_runtime_decision(
        promoted,
        can_promote_pattern=can_promote_pattern,
        formal_metrics=formal_metrics,
        formal_blocker=result.formal_promotion_blocker,
        branch_replay_rows=_branch_replay_rows_from_result(result),
    )
    promoted_program = getattr(promoted.instantiation_program, "synthesized_program", None)
    promoted_envelope = _payload(getattr(promoted_program, "program_envelope", None))
    branch_runtime_usable = any(
        bool(_payload(branch).get("runtime_usable"))
        for branch in (promoted_envelope.get("runtime_branches") or [])
    )
    if can_promote_pattern or branch_runtime_usable:
        promoted.instantiation_program = promoted.instantiation_program.model_copy(
            update={
                "shared_status": (
                    "formal_validated"
                    if can_promote_pattern
                    else "branch_runtime_validated"
                )
            }
        )
        promoted.group_type = GroupType.PATTERN
        promoted.runtime_usable = True if can_promote_pattern else bool(promoted.runtime_usable)
        if not promoted.runtime_usable:
            promoted.runtime_blockers = list(promoted.runtime_blockers or []) or [
                "no_runtime_usable_branch"
            ]
        else:
            promoted.runtime_blockers = []
        promoted.confidence = Confidence.HIGH if can_promote_pattern else Confidence.MEDIUM
        promoted.lifecycle.promotion_state = (
            "promoted_pattern_replay_gated"
            if can_promote_pattern
            else "runtime_branch_replay_gated"
        )
        promoted.lifecycle.quarantine_reason = (
            None if can_promote_pattern else result.formal_promotion_blocker
        )
        _contract_group, contract_audit = ensure_materialized_trigger_contract(promoted)
        if not bool(contract_audit.get("runtime_executable")):
            promoted.runtime_usable = True
            promoted.runtime_contract_status = str(contract_audit.get("status") or "blocked")
        return promoted
    if _runtime_visible_under_replay_audit_policy(promoted):
        current_status = getattr(promoted.instantiation_program, "shared_status", "none")
        promoted.instantiation_program = promoted.instantiation_program.model_copy(
            update={
                "shared_status": (
                    current_status
                    if current_status in {"formal_validated", "branch_runtime_validated", "compilable"}
                    else "replay_audit_visible"
                )
            }
        )
        promoted.group_type = GroupType.PATTERN
        promoted.runtime_usable = True
        promoted.runtime_blockers = []
        promoted.confidence = Confidence.MEDIUM
        promoted.lifecycle.promotion_state = "runtime_visible_replay_audit_only"
        promoted.lifecycle.quarantine_reason = ";".join(
            str(item)
            for item in (result.formal_promotion_blocker, result.reason)
            if str(item or "").strip()
        ) or None
        _contract_group, contract_audit = ensure_materialized_trigger_contract(promoted)
        if not bool(contract_audit.get("runtime_executable")):
            promoted.runtime_usable = True
            promoted.runtime_contract_status = str(contract_audit.get("status") or "blocked")
        return promoted
    promoted.runtime_usable = False
    promoted.group_type = GroupType.PATTERN
    promoted.lifecycle.promotion_state = "offline_pattern_replay_failed"
    promoted.lifecycle.quarantine_reason = result.reason
    if metrics.replay_regression > MAX_REPLAY_REGRESSION:
        promoted.status = GroupStatus.QUARANTINED
    return promoted


def integrate_promoted_groups(
    library: LibraryStateV2,
    promoted_groups: Sequence[GroupSummary],
) -> LibraryStateV2:
    """Mutate ``library`` with replay-tested evolved objects.

    Superseded singleton source records are kept as deprecated audit/replay
    evidence. They are no longer active runtime memory, but later pattern
    replay can still reconstruct per-case training memories without using gold
    at runtime.

    Replay-failed strict patterns may be retained as offline audit candidates,
    but experience-family runtime memory is no longer produced by this path.
    """
    def has_executable_program(group: GroupSummary) -> bool:
        program = getattr(group.instantiation_program, "synthesized_program", None)
        return bool(list(getattr(program, "ops", []) or []))

    for group in promoted_groups:
        if group.status != GroupStatus.ACTIVE:
            continue
        if group.group_type == GroupType.PATTERN and not has_executable_program(group):
            continue
        member_case_ids = {str(case_id) for case_id in group.case_ids}
        replay_audit_only_visible = (
            str(getattr(group.lifecycle, "promotion_state", "") or "")
            == "runtime_visible_replay_audit_only"
        )
        runtime_superseded_case_ids = (
            set()
            if replay_audit_only_visible
            else (_runtime_usable_branch_support_case_ids(group) if group.runtime_usable else set())
        ) or (member_case_ids if group.runtime_usable and not replay_audit_only_visible else set())
        existing_same = [
            existing
            for existing in (library.patterns + library.experience_families)
            if existing.group_id == group.group_id
        ]
        if (
            existing_same
            and any(existing.runtime_usable for existing in existing_same)
            and not group.runtime_usable
        ):
            continue

        if group.runtime_usable:
            updated_singletons: List[GroupSummary] = []
            for singleton in library.singletons:
                if not (set(map(str, singleton.case_ids)) & runtime_superseded_case_ids):
                    updated_singletons.append(singleton)
                    continue
                archived = singleton.model_copy(deep=True)
                archived.status = GroupStatus.DEPRECATED
                archived.runtime_usable = False
                archived.lifecycle.superseded_by_group_id = group.group_id
                archived.lifecycle.promotion_state = "superseded_source_singleton"
                updated_singletons.append(archived)
            library.singletons = updated_singletons

            for existing in [*library.patterns, *library.experience_families]:
                if existing.group_id == group.group_id:
                    continue
                existing_cases = {str(case_id) for case_id in existing.case_ids}
                if existing_cases and existing_cases <= runtime_superseded_case_ids:
                    existing.status = GroupStatus.DEPRECATED
                    existing.runtime_usable = False
                    existing.lifecycle.superseded_by_group_id = group.group_id
                    existing.lifecycle.promotion_state = "superseded_by_absorption"

        library.patterns = [
            existing for existing in library.patterns if existing.group_id != group.group_id
        ]
        library.experience_families = [
            existing
            for existing in library.experience_families
            if existing.group_id != group.group_id
        ]
        group.group_type = GroupType.PATTERN
        library.patterns.append(group)
        library.experience_families = []
    return library


__all__ = [
    "apply_promotion_decision",
    "integrate_promoted_groups",
    "run_promotion_test",
]
