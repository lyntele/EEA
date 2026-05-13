#!/usr/bin/env python3
"""Run an online EEA v2 end-to-end validation from an empty library.

This is the standalone validation analogue of the planned DeepEye integration:
each case is processed in arrival order, using only the library state available
before that case. If the current case is still wrong after memory rewrite, the
case is accumulated into the library for future cases.

Runtime remains answer-blind. Gold SQL is used only after prediction/rewrite for
execution feedback and in the accumulate phase for wrong cases.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set


DEFAULT_BIRD_DB_ROOT = "/data/liuyining/ace4sql/bench/bird/dev/dev_databases"
DEFAULT_DEEPEYE_ROOT = "/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL"
DEFAULT_ACE_ROOT = "/data/liuyining/ace4sql"


def _add_paths(deepeye_root: Path, ace_root: Path) -> None:
    for path in (str(ace_root.resolve()), str(deepeye_root.resolve())):
        if path not in sys.path:
            sys.path.insert(0, path)


def _dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")
    tmp_path.replace(path)


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _payload(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_payload(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _payload(v) for k, v in value.items()}
    return value


def _parse_case_ids(raw: str) -> Optional[List[str]]:
    if not raw.strip():
        return None
    seen: Set[str] = set()
    ordered: List[str] = []
    for part in raw.split(","):
        case_id = part.strip()
        if not case_id or case_id in seen:
            continue
        seen.add(case_id)
        ordered.append(case_id)
    return ordered


def _case_sort_key(path: Path) -> int:
    try:
        return int(path.name.split("_")[-1])
    except Exception:
        return 0


def _sql_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    sql = getattr(value, "sql", None)
    if sql is not None:
        return str(sql)
    return str(value)


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
    if cmp_payload.get("gold_error"):
        return "gold_error"
    if cmp_payload.get("pred_error"):
        return "execution_error"
    if cmp_payload.get("row_sets_equivalent") is None:
        return "unknown"
    return ""


def _status_unknown_for_accumulate(status: str) -> bool:
    return str(status or "") in {"unknown", "not_run", "gold_error"}


def _row_comparison_unknown(row: Dict[str, Any]) -> bool:
    if row.get("comparison_unknown"):
        return True
    if row.get("c0_comparison_unknown_reason"):
        return True
    if row.get("final_comparison_unknown_reason"):
        return True
    return bool(row.get("comparison_unknown_reasons"))


def _selected_memory_payload(groups: Iterable[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for group in groups:
        skeleton = group.core_interface.repair_skeleton_prototype.structural
        rows.append(
            {
                "group_id": group.group_id,
                "group_type": str(getattr(group.group_type, "value", group.group_type)),
                "case_ids": list(group.case_ids),
                "support": group.support,
                "runtime_usable": bool(group.runtime_usable),
                "repair_goal": group.core_interface.repair_goal,
                "repair_skeleton": {
                    "locus": str(getattr(skeleton.locus, "value", skeleton.locus)),
                    "op_family": str(getattr(skeleton.op_family, "value", skeleton.op_family)),
                    "target_family": str(
                        getattr(skeleton.target_family, "value", skeleton.target_family)
                    ),
                    "output_contract": str(
                        getattr(skeleton.output_contract, "value", skeleton.output_contract)
                    ),
                },
                "template": group.instantiation_program.template,
                "trigger_contract": _payload(group.trigger_contract),
                "instantiation_program": _payload(group.instantiation_program),
                "formation_signals": _payload(group.formation_signals),
            }
        )
    return rows


def _canonical_payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _repair_step_key(step: Dict[str, Any]) -> tuple[str, str, str, bool, bool, str, str, str]:
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


def _contract_steps(memory: Dict[str, Any]) -> List[Dict[str, Any]]:
    contract = memory.get("trigger_contract") or {}
    action_contract = contract.get("action_contract") or {}
    return [dict(step) for step in (action_contract.get("repair_program") or []) if isinstance(step, dict)]


def _strict_contract_issues(row: Dict[str, Any]) -> List[str]:
    """Audit that runtime used only case-extracted/group-merged repair programs."""
    issues: List[str] = []
    if row.get("plan_reason") != "ready":
        return issues

    allowed_by_group: Dict[str, set[tuple[str, str, str, bool, bool, str, str, str]]] = {}
    for memory in row.get("matched_memory_objects") or []:
        group_id = str(memory.get("group_id") or "")
        steps = _contract_steps(memory)
        if not steps:
            issues.append(f"memory_missing_contract_repair_program:{group_id}")
        allowed_by_group[group_id] = {_repair_step_key(step) for step in steps}
        for step in steps:
            origin = str(step.get("origin") or "")
            source = str(step.get("extraction_source") or "")
            if origin not in {"case_extracted", "group_merged"}:
                issues.append(f"invalid_memory_repair_step_origin:{group_id}:{origin}")
            if source not in {"llm_explicit", "group_merged"}:
                issues.append(f"non_explicit_memory_repair_step:{group_id}:{source}")

    compiler_output = row.get("compiler_output") or {}
    if compiler_output.get("escape_hatch_log"):
        issues.append("compiler_escape_hatch_log_present")
    for action in compiler_output.get("actions") or []:
        action_id = str(action.get("action_id") or "")
        if action.get("used_escape_hatch"):
            issues.append(f"action_used_escape_hatch:{action_id}")
        arguments = action.get("arguments") or {}
        if arguments.get("dependency_repairs"):
            issues.append(f"legacy_dependency_repairs_present:{action_id}")
        source_group_id = str(action.get("source_group_id") or "")
        allowed = allowed_by_group.get(source_group_id, set())
        steps = arguments.get("repair_program") or []
        if not steps:
            issues.append(f"action_missing_repair_program:{action_id}")
        for step in steps:
            if not isinstance(step, dict):
                issues.append(f"invalid_action_repair_step_payload:{action_id}")
                continue
            key = _repair_step_key(step)
            if key not in allowed:
                issues.append(f"uncontracted_repair_step:{action_id}:{key}")
            origin = str(step.get("origin") or "")
            source = str(step.get("extraction_source") or "")
            if origin not in {"case_extracted", "group_merged"}:
                issues.append(f"invalid_action_repair_step_origin:{action_id}:{origin}")
            if source not in {"llm_explicit", "group_merged"}:
                issues.append(f"non_explicit_action_repair_step:{action_id}:{source}")

    for rewrite in row.get("rewrites") or []:
        if rewrite.get("dependency_repairs_applied"):
            issues.append(f"legacy_dependency_repairs_applied:candidate_{rewrite.get('candidate_index')}")
        for trace in rewrite.get("action_realization_traces") or []:
            if trace.get("scope_violation"):
                issues.append(f"action_scope_violation:{trace.get('action_id')}")
    return issues


def _singleton_contract_issues(singleton: Any) -> List[str]:
    payload = _selected_memory_payload([singleton])[0]
    group_id = str(payload.get("group_id") or "")
    issues: List[str] = []
    steps = _contract_steps(payload)
    if not steps:
        issues.append(f"new_singleton_missing_contract_repair_program:{group_id}")
    for step in steps:
        origin = str(step.get("origin") or "")
        source = str(step.get("extraction_source") or "")
        if origin != "case_extracted":
            issues.append(f"new_singleton_invalid_repair_step_origin:{group_id}:{origin}")
        if source != "llm_explicit":
            issues.append(f"new_singleton_non_explicit_repair_step:{group_id}:{source}")
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if "dependency_repairs" in text:
        issues.append(f"new_singleton_legacy_dependency_repairs_payload:{group_id}")
    return issues


def _remove_group_by_id(library: Any, group_id: str) -> None:
    library.singletons = [
        group for group in library.singletons if str(group.group_id) != str(group_id)
    ]
    library.experience_families = [
        group for group in library.experience_families if str(group.group_id) != str(group_id)
    ]
    library.patterns = [
        group for group in library.patterns if str(group.group_id) != str(group_id)
    ]
    library.cases_processed = max(0, int(library.cases_processed or 0) - 1)


def _library_counts(library: Any) -> Dict[str, int]:
    return {
        "patterns": len(library.patterns),
        "experience_families": len(library.experience_families),
        "singletons": len(library.singletons),
        "cases_processed": int(library.cases_processed or 0),
    }


def _summary_payload(
    *,
    args: argparse.Namespace,
    rows: Sequence[Dict[str, Any]],
    library: Any,
    started_at: float,
    family_reports: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    baseline_equiv = [
        row["question_id"]
        for row in rows
        if _is_equivalent(row.get("c0_status", row.get("pred_status", "")))
    ]
    final_equiv = [row["question_id"] for row in rows if _is_equivalent(row.get("final_status", ""))]
    improved_rows = [
        row
        for row in rows
        if not _is_equivalent(row.get("c0_status", row.get("pred_status", "")))
        and _is_equivalent(row.get("final_status", ""))
        and not _row_comparison_unknown(row)
    ]
    improved = [row["question_id"] for row in improved_rows]
    regressions = [
        row["question_id"]
        for row in rows
        if _is_equivalent(row.get("c0_status", row.get("pred_status", "")))
        and not _is_equivalent(row.get("final_status", ""))
        and not _row_comparison_unknown(row)
    ]
    memory_rewrite_broke_top1 = [
        row["question_id"]
        for row in rows
        if _is_equivalent(row.get("pred_status", ""))
        and not _row_comparison_unknown(row)
        and any(
            not rewrite.get("rewrite_comparison_unknown_reason")
            and rewrite.get("rewrite_status") not in {"equivalent", None, ""}
            for rewrite in (row.get("rewrites") or [])
        )
    ]
    not_improved = [
        row["question_id"]
        for row in rows
        if not _is_equivalent(row.get("c0_status", row.get("pred_status", "")))
        and not _is_equivalent(row.get("final_status", ""))
        and not _row_comparison_unknown(row)
    ]
    comparison_unknown_cases = [
        {
            "case_id": row["question_id"],
            "reasons": row.get("comparison_unknown_reasons") or [],
            "c0_reason": row.get("c0_comparison_unknown_reason"),
            "final_reason": row.get("final_comparison_unknown_reason"),
        }
        for row in rows
        if _row_comparison_unknown(row)
    ]
    strict_issues = [
        {
            "case_id": row["question_id"],
            "issues": (row.get("strict_contract_issues") or [])
            + (row.get("new_singleton_contract_issues") or []),
        }
        for row in rows
        if row.get("strict_contract_issues") or row.get("new_singleton_contract_issues")
    ]
    accumulated = [row["question_id"] for row in rows if row.get("accumulate_status") == "ok"]
    final_events = [
        event for event in family_reports if event.get("event_kind") == "final_evolve_and_freeze"
    ]
    learned_no_future = [
        item
        for event in final_events
        for item in (event.get("promoted_runtime_objects") or [])
    ]
    gain_by_origin: Dict[str, List[Any]] = {}
    for row in improved_rows:
        origin = str(row.get("memory_gain_origin") or "unknown")
        gain_by_origin.setdefault(origin, []).append(row["question_id"])
    return {
        "summary": {
            "db_id": args.db_id,
            "work_root": str(Path(args.work_root).resolve()),
            "total_cases": len(rows),
            "baseline_equivalent_count": len(baseline_equiv),
            "final_equivalent_count": len(final_equiv),
            "net_improvement": len(improved) - len(regressions),
            "improved_cases": improved,
            "regression_cases": regressions,
            "memory_rewrite_broke_top1_cases": memory_rewrite_broke_top1,
            "not_improved_cases": not_improved,
            "comparison_unknown_cases": comparison_unknown_cases,
            "comparison_unknown_count": len(comparison_unknown_cases),
            "ready_cases": sum(1 for row in rows if row.get("plan_reason") == "ready"),
            "rewritten_cases": sum(1 for row in rows if row.get("rewritten")),
            "triggered_cases": sum(1 for row in rows if row.get("matched_group_ids")),
            "accumulated_cases": accumulated,
            "accumulated_count": len(accumulated),
            "strict_contract_issue_count": sum(len(item["issues"]) for item in strict_issues),
            "library_counts": _library_counts(library),
            "serial_online_result": {
                "baseline_equivalent_count": len(baseline_equiv),
                "final_equivalent_count": len(final_equiv),
                "net_improvement": len(improved) - len(regressions),
                "improved_cases": improved,
                "regression_cases": regressions,
                "comparison_unknown_count": len(comparison_unknown_cases),
            },
            "post_run_learned_library": _library_counts(library),
            "learned_but_no_future_opportunity": learned_no_future,
            "elapsed_sec": round(time.time() - started_at, 3),
            "metric_mode": (
                "serial_top1_with_all_candidate_diagnostics"
                if args.rewrite_all_candidates
                else "serial_top1_single_shot_c0_to_c1"
            ),
            "memory_gain_by_origin": gain_by_origin,
        },
        "config": {
            "rewrite_all_candidates": bool(args.rewrite_all_candidates),
            "accumulate_sql_source": args.accumulate_sql_source,
            "row_sample_limit": args.row_sample_limit,
            "skip_rewrite": bool(getattr(args, "skip_rewrite", False)),
            "promotion_interval": args.promotion_interval,
            "family_runtime_policy": args.family_runtime_policy,
            "manual_groups_json": args.manual_groups_json,
            "strict_contract_policy": args.strict_contract_policy,
            "skip_final_freeze": bool(getattr(args, "skip_final_freeze", False)),
            "max_cases": args.max_cases,
            "case_ids": args.case_ids,
            "case_id_order_effective": list(
                getattr(args, "case_id_order_effective", []) or []
            ),
            "missing_requested_case_ids": list(
                getattr(args, "missing_requested_case_ids", []) or []
            ),
        },
        "strict_contract_issues": strict_issues,
        "family_reports": list(family_reports),
        "cases": list(rows),
    }


def _save_library_snapshots(
    *,
    library: Any,
    output_dir: Path,
    case_id: str,
    enabled: bool,
) -> None:
    if not enabled:
        return
    _dump_json(
        output_dir / "library_snapshots" / f"after_qid_{case_id}.json",
        _payload(library),
    )


def _maybe_form_families(
    *,
    library: Any,
    case_id: str,
    args: argparse.Namespace,
    output_dir: Path,
    work_root: Path,
    db_path: Path,
    load_dataset: Any,
    evolve_library_with_replay: Any,
    manual_groups: Optional[Dict[str, Any]],
    force: bool = False,
    event_kind: str = "periodic_evolve",
    focus_case_ids: Optional[Set[str]] = None,
) -> Optional[Dict[str, Any]]:
    processed = int(library.cases_processed or 0)
    if processed < args.promotion_min_support:
        return None
    if not force:
        if args.promotion_interval <= 0:
            return None
        if processed % args.promotion_interval != 0:
            return None
    case_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    def _load_case_record(load_case_id: str) -> Optional[Dict[str, Any]]:
        load_case_id = str(load_case_id)
        if load_case_id in case_cache:
            return case_cache[load_case_id]
        input_pkl = work_root / f"qid_{load_case_id}" / "rewrite_input.pkl"
        if not input_pkl.exists():
            case_cache[load_case_id] = None
            return None
        dataset = load_dataset(str(input_pkl))
        if not dataset:
            case_cache[load_case_id] = None
            return None
        item = dataset[0]
        case_cache[load_case_id] = {
            "db_id": str(item.database_id),
            "question": str(item.question),
            "evidence": str(item.evidence or ""),
            "candidates": [_sql_text(value) for value in list(item.sql_candidates or [])],
            "gold_sql": str(getattr(item, "gold_sql", "") or ""),
        }
        return case_cache[load_case_id]

    evolved_library, report = evolve_library_with_replay(
        library=library,
        event_kind=event_kind,
        case_loader=_load_case_record
        if args.family_runtime_policy == "replay_gated"
        else None,
        db_path=str(db_path) if args.family_runtime_policy == "replay_gated" else None,
        database_dir=str(db_path.parent),
        manual_groups=manual_groups,
        focus_case_ids=focus_case_ids,
        max_neighbor_edges=args.max_neighbor_edges,
        family_runtime_policy=args.family_runtime_policy,
        promotion_min_support=args.promotion_min_support,
        row_sample_limit=args.row_sample_limit,
        freeze_output_path=(
            output_dir / "library_freeze.json"
            if event_kind == "final_evolve_and_freeze"
            else None
        ),
        skip_replay_freeze=bool(
            event_kind == "final_evolve_and_freeze"
            and getattr(args, "skip_final_freeze", False)
        ),
    )
    library.patterns = list(evolved_library.patterns)
    library.experience_families = list(evolved_library.experience_families)
    library.singletons = list(evolved_library.singletons)
    library.cases_processed = int(evolved_library.cases_processed or 0)

    promoted_runtime_objects = list(report.get("promoted_runtime_objects") or [])
    promotion_results = list(report.get("promotion_results") or [])
    before_counts = report.get("library_counts_before") or {}
    after_counts = report.get("library_counts_after") or {}
    event = {
        "event_kind": event_kind,
        "case_id": int(case_id) if str(case_id).isdigit() else case_id,
        "processed": processed,
        "focus_case_ids": sorted(focus_case_ids or []),
        "runtime_usable": bool(promoted_runtime_objects),
        "library_mutated": before_counts != after_counts,
        "output_counts": after_counts,
        "promotion_policy": args.family_runtime_policy,
        "promotion_results": promotion_results,
        "promoted_runtime_objects": promoted_runtime_objects,
        "promotion_skipped_reason": report.get("promotion_skipped_reason"),
        "final_freeze_skipped": bool(report.get("final_freeze_skipped", False)),
        "freeze_manifest": report.get("freeze_manifest"),
        "new_experience_families": [
            {
                "group_id": family.get("group_id"),
                "case_ids": family.get("case_ids"),
                "support": family.get("support"),
                "runtime_usable": family.get("runtime_usable"),
            }
            for family in ((report.get("formation") or {}).get("families") or [])
        ],
    }
    if promotion_results:
        report["promotion_results"] = promotion_results
        report["promoted_runtime_objects"] = promoted_runtime_objects
    _dump_json(
        output_dir / "family_reports" / f"{event_kind}_after_qid_{case_id}.json",
        report,
    )
    return event


def _load_or_init_library(
    *,
    path: Path,
    db_id: str,
    resume: bool,
    LibraryStateV2: Any,
) -> Any:
    if resume and path.exists():
        library = LibraryStateV2.model_validate(_load_json(path))
        from method.EEA.rulebook.common.runtime.trigger_contract import (  # noqa: WPS433
            materialize_library_runtime_contracts,
        )

        materialize_library_runtime_contracts(library)
        return library
    return LibraryStateV2(db_id=db_id)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db_id", required=True)
    parser.add_argument("--work_root", required=True)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--bird_db_root", default=DEFAULT_BIRD_DB_ROOT)
    parser.add_argument("--deepeye_root", default=DEFAULT_DEEPEYE_ROOT)
    parser.add_argument("--ace_root", default=DEFAULT_ACE_ROOT)
    parser.add_argument("--case_ids", default="", help="Comma-separated qids; empty means all")
    parser.add_argument("--max_cases", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--fail_fast", action="store_true")
    parser.add_argument("--rewrite_all_candidates", action="store_true")
    parser.add_argument(
        "--accumulate_sql_source",
        choices=("top1", "first_rewrite", "best_rewrite"),
        default="top1",
        help="Which wrong SQL to audit when final output is still wrong.",
    )
    parser.add_argument("--row_sample_limit", type=int, default=5000)
    parser.add_argument("--promotion_interval", type=int, default=0)
    parser.add_argument("--promotion_min_support", type=int, default=2)
    parser.add_argument("--max_neighbor_edges", type=int, default=5)
    parser.add_argument(
        "--manual_groups_json",
        default="",
        help="Optional doc/db_pattern_groups.json for audit-only family alignment reports.",
    )
    parser.add_argument(
        "--strict_contract_policy",
        choices=("fail", "skip_accumulate", "continue"),
        default="fail",
        help="How to handle runtime or newly accumulated contract violations.",
    )
    parser.add_argument(
        "--family_runtime_policy",
        choices=("offline_only", "replay_gated"),
        default="offline_only",
        help=(
            "offline_only forms family candidates but keeps them out of runtime. "
            "replay_gated runs leave-one-out replay and only promotes passing families."
        ),
    )
    parser.add_argument("--save_library_snapshots", action="store_true")
    parser.add_argument(
        "--skip_rewrite",
        action="store_true",
        help=(
            "Run trigger/compiler/admission/evolution normally but skip memory "
            "rewrite LLM calls; final SQL remains S0."
        ),
    )
    parser.add_argument(
        "--skip-final-freeze",
        action="store_true",
        help="Skip final replay/freeze promotion and write the current incremental library.",
    )
    args = parser.parse_args(argv)

    started_at = time.time()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("EEA_LLM_TRACE_PATH", str(output_dir / "eea_llm_trace.jsonl"))
    _add_paths(Path(args.deepeye_root), Path(args.ace_root))

    from method.EEA.rulebook.common.core.config import load_config  # noqa: WPS433

    if not bool(args.skip_final_freeze):
        try:
            args.skip_final_freeze = bool(load_config().evolution.skip_final_freeze)
        except Exception:
            args.skip_final_freeze = True

    from app.dataset import load_dataset  # noqa: WPS433
    from method.EEA.rulebook.common.learning.accumulate import accumulate_wrong_case  # noqa: WPS433
    from method.EEA.rulebook.common.core.data_structures import LibraryStateV2  # noqa: WPS433
    from method.EEA.rulebook.common.learning.evolution import (  # noqa: WPS433
        evolve_library_with_replay,
        freeze_library_manifest,
    )
    from method.EEA.rulebook.common.io.execution_compare import run_execution_comparison  # noqa: WPS433
    from method.EEA.rulebook.common.runtime.runtime import (  # noqa: WPS433
        prepare_rewrite_plan,
        rewrite_realization_origin_from_result,
        rewrite_one_candidate,
    )

    work_root = Path(args.work_root).resolve()
    db_root = Path(args.bird_db_root).resolve()
    db_path = db_root / args.db_id / f"{args.db_id}.sqlite"
    library_path = output_dir / "library.json"
    library = _load_or_init_library(
        path=library_path,
        db_id=args.db_id,
        resume=args.resume,
        LibraryStateV2=LibraryStateV2,
    )
    manual_groups = (
        _load_json(Path(args.manual_groups_json).resolve())
        if args.manual_groups_json
        else None
    )

    wanted = _parse_case_ids(args.case_ids)
    all_case_dirs = sorted(work_root.glob("qid_*"), key=_case_sort_key)
    if wanted is not None:
        by_case_id = {path.name.split("_")[-1]: path for path in all_case_dirs}
        case_dirs = [by_case_id[case_id] for case_id in wanted if case_id in by_case_id]
        args.missing_requested_case_ids = [
            case_id for case_id in wanted if case_id not in by_case_id
        ]
    else:
        case_dirs = all_case_dirs
        args.missing_requested_case_ids = []
    if args.max_cases > 0:
        case_dirs = case_dirs[: args.max_cases]
    args.case_id_order_effective = [path.name.split("_")[-1] for path in case_dirs]

    rows: List[Dict[str, Any]] = []
    family_events: List[Dict[str, Any]] = []
    summary_path = output_dir / "summary.json"
    if args.resume and summary_path.exists():
        old_payload = _load_json(summary_path)
        rows = list(old_payload.get("cases") or [])
        family_events = list(old_payload.get("family_reports") or [])
    completed = {str(row.get("question_id")) for row in rows if row.get("status") == "done"}

    for case_dir in case_dirs:
        case_id = case_dir.name.split("_")[-1]
        if args.resume and case_id in completed:
            continue
        input_pkl = case_dir / "rewrite_input.pkl"
        row: Dict[str, Any] = {
            "question_id": int(case_id) if case_id.isdigit() else case_id,
            "case_dir": str(case_dir),
            "status": "running",
            "library_counts_before": _library_counts(library),
        }
        case_started = time.time()
        try:
            if not input_pkl.exists():
                raise RuntimeError("missing_rewrite_input")
            dataset = load_dataset(str(input_pkl))
            if not dataset:
                raise RuntimeError("empty_dataset")
            item = dataset[0]
            db_id = str(item.database_id)
            if db_id != args.db_id:
                raise RuntimeError(f"db_id_mismatch: expected={args.db_id} actual={db_id}")
            candidates = [_sql_text(value) for value in list(item.sql_candidates or [])]
            gold_sql = str(getattr(item, "gold_sql", "") or "")
            if not candidates:
                raise RuntimeError("no_sql_candidates")
            if not gold_sql:
                raise RuntimeError("missing_gold_sql")

            top1_sql = candidates[0]
            row.update(
                {
                    "db_id": db_id,
                    "question": str(item.question),
                    "evidence": str(item.evidence or ""),
                    "candidate_count": len(candidates),
                    "c0_snapshot": {
                        "candidate_count": len(candidates),
                        "top1_sql": top1_sql,
                        "candidates": list(candidates),
                    },
                    "top1_sql": top1_sql,
                    "gold_sql": gold_sql,
                }
            )

            pred_cmp = run_execution_comparison(
                db_path=str(db_path),
                pred_sql=top1_sql,
                gold_sql=gold_sql,
                row_sample_limit=args.row_sample_limit,
            )
            pred_cmp_payload = _payload(pred_cmp)
            pred_status = _comparison_status(pred_cmp_payload)
            pred_unknown_reason = _comparison_unknown_reason(pred_cmp_payload)
            row["pred_vs_gold"] = pred_cmp_payload
            row["pred_status"] = pred_status
            row["c0_comparison_unknown_reason"] = pred_unknown_reason
            row["comparison_unknown"] = bool(pred_unknown_reason)
            row["comparison_unknown_reasons"] = [pred_unknown_reason] if pred_unknown_reason else []
            c0_candidates: List[Dict[str, Any]] = [
                {
                    "candidate_index": 0,
                    "sql": top1_sql,
                    "status": pred_status,
                    "comparison_unknown_reason": pred_unknown_reason,
                    "vs_gold": pred_cmp_payload,
                }
            ]
            if args.rewrite_all_candidates:
                for idx, candidate_sql in enumerate(candidates[1:], start=1):
                    candidate_cmp = run_execution_comparison(
                        db_path=str(db_path),
                        pred_sql=candidate_sql,
                        gold_sql=gold_sql,
                        row_sample_limit=args.row_sample_limit,
                    )
                    candidate_cmp_payload = _payload(candidate_cmp)
                    c0_candidates.append(
                        {
                            "candidate_index": idx,
                            "sql": candidate_sql,
                            "status": _comparison_status(candidate_cmp_payload),
                            "comparison_unknown_reason": _comparison_unknown_reason(
                                candidate_cmp_payload
                            ),
                            "vs_gold": candidate_cmp_payload,
                        }
                    )
            row["c0_candidates"] = c0_candidates
            c0_oracle_status = (
                "equivalent"
                if any(_is_equivalent(item.get("status", "")) for item in c0_candidates)
                else pred_status
            )
            row["c0_status"] = pred_status
            row["c0_selection_source"] = "c0_top1"
            row["c0_oracle_status"] = c0_oracle_status
            row["c0_oracle_selection_source"] = (
                "c0_any_candidate"
                if c0_oracle_status == "equivalent" and not _is_equivalent(pred_status)
                else "c0_top1"
            )
            row["c0_evaluation_policy"] = "strict_top1_selected_candidate"
            row["c0_snapshot"]["candidates"] = [
                {
                    "candidate_index": item["candidate_index"],
                    "sql": item["sql"],
                    "status": item["status"],
                    "active_in_strict_c0": item["candidate_index"] == 0,
                }
                for item in c0_candidates
            ]

            plan = prepare_rewrite_plan(
                db_id=db_id,
                case_id=str(case_id),
                question=str(item.question),
                evidence=str(item.evidence or ""),
                pred_top1_sql=top1_sql,
                c0_candidates=candidates,
                library=library,
                db_path=str(db_path),
                database_dir=str(db_path.parent),
            )
            compiler_output = plan.get("compiler_output")
            matched_groups = list(plan.get("matched_groups") or [])
            trigger_result = plan.get("trigger_result")
            row.update(
                {
                    "plan_reason": plan.get("reason"),
                    "rewrite_enabled_reason": plan.get("rewrite_enabled_reason"),
                    "matched_group_ids": [group.group_id for group in matched_groups],
                    "matched_memory_objects": _selected_memory_payload(matched_groups),
                    "trigger_result": {
                        "strategy_version": getattr(trigger_result, "strategy_version", ""),
                        "selected_group_ids": [
                            group.group_id
                            for group in getattr(trigger_result, "selected_groups", [])
                        ],
                        "candidates": _payload(getattr(trigger_result, "candidates", [])),
                    },
                    "compiler_candidate_sets": _payload(plan.get("compiler_candidate_sets")),
                    "compiler_output": _payload(compiler_output),
                    "action_selection_summary": _payload(
                        getattr(compiler_output, "selection_summary", None)
                        if compiler_output is not None
                        else None
                    ),
                    "raw_hint": plan.get("raw_hint", ""),
                    "instantiated_hint": plan.get("instantiated_hint", ""),
                    "repair_brief": plan.get("repair_brief", ""),
                    "hint_applicable": bool(plan.get("hint_applicable", False)),
                    "hint_instantiation_notes": plan.get("hint_instantiation_notes", ""),
                }
            )

            rewrites: List[Dict[str, Any]] = []
            if plan.get("reason") == "ready" and compiler_output is not None:
                if args.skip_rewrite:
                    row["rewrite_skipped_reason"] = "skip_rewrite_cli_flag"
                else:
                    rewrite_inputs = candidates if args.rewrite_all_candidates else [top1_sql]
                    for idx, pred_sql in enumerate(rewrite_inputs):
                        rewrite_row: Dict[str, Any] = {
                            "candidate_index": idx,
                            "input_sql": pred_sql,
                        }
                        try:
                            result = rewrite_one_candidate(
                                question=str(item.question),
                                evidence=str(item.evidence or ""),
                                pred_sql=pred_sql,
                                compiler_output=compiler_output,
                                local_schema_view=plan["case_view"].local_schema_view,
                                natural_language_hint=plan.get("repair_brief", "")
                                or plan.get("instantiated_hint", "")
                                or "",
                            )
                            rewrite_sql = result.get("rewrite_sql")
                            rewrite_cmp = (
                                run_execution_comparison(
                                    db_path=str(db_path),
                                    pred_sql=str(rewrite_sql or ""),
                                    gold_sql=gold_sql,
                                    row_sample_limit=args.row_sample_limit,
                                )
                                if rewrite_sql
                                else None
                            )
                            rewrite_cmp_payload = _payload(rewrite_cmp)
                            rewrite_unknown_reason = _comparison_unknown_reason(rewrite_cmp_payload)
                            rewrite_row.update(
                                {
                                    "rewrite_sql": rewrite_sql,
                                    "rewrite_vs_gold": rewrite_cmp_payload,
                                    "rewrite_status": _comparison_status(rewrite_cmp_payload),
                                    "rewrite_comparison_unknown_reason": rewrite_unknown_reason,
                                    "action_realization_traces": result.get(
                                        "action_realization_traces", []
                                    ),
                                    "dependency_repairs_applied": result.get(
                                        "dependency_repairs_applied", []
                                    ),
                                    "contract_steps_applied": result.get(
                                        "contract_steps_applied", []
                                    ),
                                    "rewrite_contract": result.get("rewrite_contract", {}),
                                    "prompt_payload_audit": result.get(
                                        "prompt_payload_audit", {}
                                    ),
                                    "rewrite_realization_origin": rewrite_realization_origin_from_result(
                                        result
                                    ),
                                }
                            )
                            if result.get("rewrite_contract"):
                                _dump_json(
                                    output_dir
                                    / "cases"
                                    / f"qid_{case_id}_rewrite_contract_{idx}.json",
                                    result.get("rewrite_contract"),
                                )
                        except Exception as exc:
                            rewrite_row.update(
                                {
                                    "rewrite_sql": None,
                                    "rewrite_status": "exception",
                                    "error": f"{type(exc).__name__}: {exc}",
                                    "traceback": traceback.format_exc(),
                                }
                            )
                        rewrites.append(rewrite_row)
            row["rewrites"] = rewrites
            row["rewritten"] = bool(rewrites)
            row["strict_contract_issues"] = _strict_contract_issues(row)
            if row["strict_contract_issues"]:
                row["contract_violation_policy"] = args.strict_contract_policy
                if args.strict_contract_policy == "fail":
                    raise RuntimeError(
                        "strict_contract_issues: "
                        + "; ".join(str(item) for item in row["strict_contract_issues"])
                    )

            rewrite_statuses = [str(item.get("rewrite_status") or "") for item in rewrites]
            best_rewrite = next(
                (item for item in rewrites if item.get("rewrite_status") == "equivalent"),
                rewrites[0] if rewrites else None,
            )
            best_rewrite_status = (
                "equivalent"
                if "equivalent" in rewrite_statuses
                else (str(best_rewrite.get("rewrite_status")) if best_rewrite else "not_run")
            )
            c0_status = str(row.get("c0_status") or pred_status)
            final_status = (
                "equivalent"
                if _is_equivalent(c0_status)
                else best_rewrite_status
                if best_rewrite is not None
                else c0_status
            )
            final_unknown_reason = ""
            if _is_equivalent(c0_status):
                final_unknown_reason = ""
            elif best_rewrite:
                final_unknown_reason = str(
                    best_rewrite.get("rewrite_comparison_unknown_reason") or ""
                )
            elif _status_unknown_for_accumulate(final_status):
                final_unknown_reason = str(row.get("c0_comparison_unknown_reason") or final_status)
            row["best_rewrite_status"] = best_rewrite_status
            row["final_status"] = final_status
            row["final_comparison_unknown_reason"] = final_unknown_reason
            comparison_unknown_reasons = sorted(
                {
                    str(reason)
                    for reason in (
                        [row.get("c0_comparison_unknown_reason"), final_unknown_reason]
                        + [
                            rewrite.get("rewrite_comparison_unknown_reason")
                            for rewrite in rewrites
                        ]
                    )
                    if str(reason or "")
                }
            )
            row["comparison_unknown_reasons"] = comparison_unknown_reasons
            row["comparison_unknown"] = bool(comparison_unknown_reasons)
            row["final_selection_source"] = (
                str(row.get("c0_selection_source") or "c0_retained")
                if _is_equivalent(c0_status)
                else "memory_rewrite"
                if _is_equivalent(best_rewrite_status)
                else "none_equivalent"
            )
            c1_rewrite_candidates = [
                {
                    "origin": "memory_rewrite",
                    "candidate_index": rewrite.get("candidate_index"),
                    "status": rewrite.get("rewrite_status"),
                    "rewrite_sql": rewrite.get("rewrite_sql"),
                    "rewrite_realization_origin": rewrite.get("rewrite_realization_origin"),
                }
                for rewrite in rewrites
            ]
            row["c1_candidates"] = [
                {
                    "origin": "c0_original",
                    "candidate_index": candidate.get("candidate_index"),
                    "status": candidate.get("status"),
                    "sql": candidate.get("sql"),
                    "active_in_strict_c1": candidate.get("candidate_index") == 0,
                }
                for candidate in c0_candidates
            ] + c1_rewrite_candidates
            row["c1_snapshot"] = {
                "c0_candidates_retained": [
                    {
                        "candidate_index": candidate.get("candidate_index"),
                        "status": candidate.get("status"),
                        "active_in_strict_c1": candidate.get("candidate_index") == 0,
                    }
                    for candidate in c0_candidates
                ],
                "rewrite_candidates": c1_rewrite_candidates,
            }
            row["memory_gain_origin"] = "none"
            if (
                not _is_equivalent(c0_status)
                and _is_equivalent(best_rewrite_status)
                and best_rewrite
                and not _row_comparison_unknown(row)
                and not best_rewrite.get("rewrite_comparison_unknown_reason")
            ):
                selection_summary = row.get("action_selection_summary") or {}
                selection_origins = selection_summary.get("selection_origins") or {}
                realization_origin = str(
                    best_rewrite.get("rewrite_realization_origin")
                    or "memory_rewrite_llm"
                )
                if realization_origin == "post_rewrite_contract_realization":
                    row["memory_gain_origin"] = "deterministic_contract_realization"
                elif selection_origins == {"deterministic_unique": 1}:
                    row["memory_gain_origin"] = "deterministic_action_selection"
                elif selection_origins:
                    row["memory_gain_origin"] = "llm_action_selection"
                else:
                    row["memory_gain_origin"] = "memory_rewrite_llm"
            row["memory_rewrite_broke_top1"] = bool(
                _is_equivalent(pred_status)
                and not _row_comparison_unknown(row)
                and any(
                    not rewrite.get("rewrite_comparison_unknown_reason")
                    and rewrite.get("rewrite_status") not in {"equivalent", None, ""}
                    for rewrite in rewrites
                )
            )
            row["memory_rewrite_broke_c0"] = bool(
                _is_equivalent(c0_status)
                and not _row_comparison_unknown(row)
                and any(
                    not rewrite.get("rewrite_comparison_unknown_reason")
                    and rewrite.get("rewrite_status") not in {"equivalent", None, ""}
                    for rewrite in rewrites
                )
            )

            should_accumulate = not _is_equivalent(final_status)
            if _status_unknown_for_accumulate(final_status):
                should_accumulate = False
                row["accumulate_skip_reason"] = "final_comparison_unknown"
            if row.get("strict_contract_issues") and args.strict_contract_policy == "skip_accumulate":
                should_accumulate = False
            row["should_accumulate"] = should_accumulate
            if should_accumulate:
                accumulate_sql = top1_sql
                if args.accumulate_sql_source == "first_rewrite" and rewrites:
                    accumulate_sql = str(rewrites[0].get("rewrite_sql") or top1_sql)
                elif args.accumulate_sql_source == "best_rewrite" and best_rewrite:
                    accumulate_sql = str(best_rewrite.get("rewrite_sql") or top1_sql)
                try:
                    singleton, case_audit = accumulate_wrong_case(
                        db_id=db_id,
                        case_id=str(case_id),
                        question=str(item.question),
                        evidence=str(item.evidence or ""),
                        pred_sql=accumulate_sql,
                        gold_sql=gold_sql,
                        db_path=str(db_path),
                        library=library,
                        database_dir=str(db_path.parent),
                    )
                    row["accumulate_status"] = "ok"
                    row["accumulated_group_id"] = singleton.group_id
                    row["case_audit"] = _payload(case_audit)
                    singleton_issues = _singleton_contract_issues(singleton)
                    row["new_singleton_contract_issues"] = singleton_issues
                    if singleton_issues:
                        _remove_group_by_id(library, singleton.group_id)
                        row["accumulate_status"] = "contract_rejected"
                        row["contract_violation_policy"] = args.strict_contract_policy
                        if args.strict_contract_policy == "fail":
                            raise RuntimeError(
                                "new_singleton_contract_issues: "
                                + "; ".join(str(item) for item in singleton_issues)
                            )
                except Exception as exc:
                    row["accumulate_status"] = "exception"
                    row["accumulate_error"] = f"{type(exc).__name__}: {exc}"
                    row["accumulate_traceback"] = traceback.format_exc()
                    if args.fail_fast or (
                        args.strict_contract_policy == "fail"
                        and row.get("new_singleton_contract_issues")
                    ):
                        raise
            else:
                row["accumulate_status"] = (
                    "skipped_comparison_unknown"
                    if row.get("accumulate_skip_reason") == "final_comparison_unknown"
                    else "skipped_final_equivalent"
                )

            did_local_evolve = False
            if row.get("accumulate_status") == "ok":
                local_event = _maybe_form_families(
                    library=library,
                    case_id=str(case_id),
                    args=args,
                    output_dir=output_dir,
                    work_root=work_root,
                    db_path=db_path,
                    load_dataset=load_dataset,
                    evolve_library_with_replay=evolve_library_with_replay,
                    manual_groups=manual_groups,
                    force=True,
                    event_kind="local_evolve",
                    focus_case_ids={str(case_id)},
                )
                if local_event is not None:
                    did_local_evolve = True
                    family_events.append(local_event)

            if not did_local_evolve:
                event = _maybe_form_families(
                    library=library,
                    case_id=str(case_id),
                    args=args,
                    output_dir=output_dir,
                    work_root=work_root,
                    db_path=db_path,
                    load_dataset=load_dataset,
                    evolve_library_with_replay=evolve_library_with_replay,
                    manual_groups=manual_groups,
                )
                if event is not None:
                    family_events.append(event)

            row["library_counts_after"] = _library_counts(library)
            row["elapsed_sec"] = round(time.time() - case_started, 3)
            row["status"] = "done"

        except Exception as exc:
            row.update(
                {
                    "status": "exception",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                    "elapsed_sec": round(time.time() - case_started, 3),
                    "library_counts_after": _library_counts(library),
                }
            )
            if args.fail_fast or (
                args.strict_contract_policy == "fail"
                and (
                    row.get("strict_contract_issues")
                    or row.get("new_singleton_contract_issues")
                )
            ):
                rows.append(row)
                _dump_json(output_dir / "cases" / f"qid_{case_id}.json", row)
                _dump_json(library_path, _payload(library))
                _dump_json(
                    summary_path,
                    _summary_payload(
                        args=args,
                        rows=rows,
                        library=library,
                        started_at=started_at,
                        family_reports=family_events,
                    ),
                )
                raise

        rows.append(row)
        _dump_json(output_dir / "cases" / f"qid_{case_id}.json", row)
        _dump_json(library_path, _payload(library))
        _save_library_snapshots(
            library=library,
            output_dir=output_dir,
            case_id=str(case_id),
            enabled=args.save_library_snapshots,
        )
        _dump_json(
            summary_path,
            _summary_payload(
                args=args,
                rows=rows,
                library=library,
                started_at=started_at,
                family_reports=family_events,
            ),
        )

    final_event = _maybe_form_families(
        library=library,
        case_id="final",
        args=args,
        output_dir=output_dir,
        work_root=work_root,
        db_path=db_path,
        load_dataset=load_dataset,
        evolve_library_with_replay=evolve_library_with_replay,
        manual_groups=manual_groups,
        force=True,
        event_kind="final_evolve_and_freeze",
    )
    if final_event is not None:
        family_events.append(final_event)
        _dump_json(library_path, _payload(library))
    else:
        family_events.append(
            {
                "event_kind": "final_evolve_and_freeze",
                "case_id": "final",
                "processed": int(library.cases_processed or 0),
                "library_mutated": False,
                "reason": "insufficient_support_or_no_family_candidate",
                "freeze_manifest": freeze_library_manifest(
                    library=library,
                    reason="final_evolve_and_freeze_noop",
                    output_path=output_dir / "library_freeze.json",
                ),
            }
        )

    payload = _summary_payload(
        args=args,
        rows=rows,
        library=library,
        started_at=started_at,
        family_reports=family_events,
    )
    _dump_json(summary_path, payload)
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    return 1 if payload["summary"]["strict_contract_issue_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
