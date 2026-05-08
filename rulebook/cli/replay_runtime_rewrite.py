#!/usr/bin/env python3
"""Replay v2 runtime trigger -> compiler -> hint -> rewrite on saved cases.

This is an offline validation tool. Runtime planning remains answer-blind; gold
SQL is used only after rewrite to evaluate whether the produced SQL improves
over C0.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


def _dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")


def _add_paths(deepeye_root: Path, ace_root: Path) -> None:
    for path in (str(ace_root), str(deepeye_root)):
        if path not in sys.path:
            sys.path.insert(0, path)


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


def _parse_case_ids(raw: str) -> Optional[set[str]]:
    if not raw.strip():
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}


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


def _is_equivalent(status: Any) -> bool:
    return str(status or "") == "equivalent"


def _action_primitives(actions: Sequence[Any]) -> List[str]:
    primitives: List[str] = []
    for action in actions:
        primitive = getattr(action, "primitive", None)
        primitives.append(str(getattr(primitive, "value", primitive) or ""))
    return primitives


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


def _augment_memory_contracts(
    *,
    library: Any,
    work_root: Path,
    db_root: Path,
    load_dataset: Any,
    preprocess_case: Any,
    SqliteDBSchemaAccess: Any,
    apply_delta_structural_override: Any,
    build_formation_signals: Any,
    build_trigger_contract: Any,
) -> int:
    by_case_id = {
        str(group.case_ids[0]): group
        for group in library.singletons
        if len(group.case_ids) == 1
    }
    updated = 0
    for case_dir in sorted(work_root.glob("qid_*"), key=_case_sort_key):
        case_id = case_dir.name.split("_")[-1]
        group = by_case_id.get(str(case_id))
        input_pkl = case_dir / "rewrite_input.pkl"
        if group is None or not input_pkl.exists():
            continue
        dataset = load_dataset(str(input_pkl))
        if not dataset:
            continue
        item = dataset[0]
        candidates = list(item.sql_candidates or [])
        gold_sql = str(getattr(item, "gold_sql", "") or "")
        if not candidates or not gold_sql:
            continue
        db_id = str(item.database_id)
        db_path = db_root / db_id / f"{db_id}.sqlite"
        access = SqliteDBSchemaAccess(
            db_id=db_id,
            db_path=str(db_path),
            database_dir=str(db_path.parent),
        )
        prepared = preprocess_case(
            db_id=db_id,
            case_id=str(item.question_id),
            question=str(item.question),
            evidence=str(item.evidence or ""),
            pred_sql=_sql_text(candidates[0]),
            gold_sql=gold_sql,
            access=access,
            execution_comparison=None,
        )
        formation_signals = build_formation_signals(
            case_signal_view=prepared.get("case_signal_view"),
            delta_signature=prepared.get("delta_signature"),
        )
        formation_signals["case_id"] = str(item.question_id)
        group.core_interface.repair_skeleton_prototype = apply_delta_structural_override(
            group.core_interface.repair_skeleton_prototype,
            formation_signals,
        )
        formation_signals["repair_skeleton"] = group.core_interface.repair_skeleton_prototype.model_dump(
            mode="json"
        )
        group.formation_signals = formation_signals
        group.trigger_contract = build_trigger_contract(
            formation_signals=formation_signals,
            decisive_pred_signals=list(group.trigger_signature.required_pred_tags),
            decisive_question_signals=list(group.trigger_signature.required_question_tags),
            negative_signals=list(group.trigger_signature.negative_evidence),
            max_actions=1,
        )
        updated += 1
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library_json", required=True, help="Serialized LibraryStateV2 JSON")
    parser.add_argument("--work_root", required=True, help="Run .state/work directory")
    parser.add_argument("--bird_db_root", required=True, help="BIRD dev_databases directory")
    parser.add_argument("--output_json", required=True)
    parser.add_argument("--output_dir", default="", help="Optional directory for per-case JSON files")
    parser.add_argument("--case_ids", default="", help="Comma-separated qids to run; empty means all")
    parser.add_argument("--max_cases", type=int, default=0)
    parser.add_argument(
        "--row_sample_limit",
        type=int,
        default=500,
        help="Maximum row count for exact row-set execution equivalence checks.",
    )
    parser.add_argument("--augment_memory_contracts", action="store_true")
    parser.add_argument("--rewrite_all_candidates", action="store_true")
    parser.add_argument(
        "--allow_self_singleton_replay",
        action="store_true",
        help="Validation-only: allow singleton source cases to replay against themselves.",
    )
    parser.add_argument("--deepeye_root", default="/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL")
    parser.add_argument("--ace_root", default="/data/liuyining/ace4sql")
    args = parser.parse_args()

    deepeye_root = Path(args.deepeye_root).resolve()
    ace_root = Path(args.ace_root).resolve()
    _add_paths(deepeye_root, ace_root)

    from app.dataset import load_dataset  # noqa: WPS433
    from method.EEA.rulebook.common.analysis.code_preprocess import preprocess_case  # noqa: WPS433
    from method.EEA.rulebook.common.core.data_structures import LibraryStateV2  # noqa: WPS433
    from method.EEA.rulebook.common.io.db_schema_access import SqliteDBSchemaAccess  # noqa: WPS433
    from method.EEA.rulebook.common.io.execution_compare import run_execution_comparison  # noqa: WPS433
    from method.EEA.rulebook.common.runtime.runtime import (  # noqa: WPS433
        prepare_rewrite_plan,
        rewrite_realization_origin_from_result,
        rewrite_one_candidate,
    )
    from method.EEA.rulebook.common.runtime.trigger_contract import (  # noqa: WPS433
        materialize_library_runtime_contracts,
    )
    from method.EEA.rulebook.common.analysis.signal_summary import (  # noqa: WPS433
        apply_delta_structural_override,
        build_formation_signals,
        build_trigger_contract,
    )

    with Path(args.library_json).open("r", encoding="utf-8") as f:
        library = LibraryStateV2.model_validate(json.load(f))
    materialize_library_runtime_contracts(library)

    work_root = Path(args.work_root).resolve()
    db_root = Path(args.bird_db_root).resolve()
    output_json = Path(args.output_json).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else output_json.parent / "runtime_rewrite_cases"
    wanted_case_ids = _parse_case_ids(args.case_ids)

    augmented_contracts = 0
    if args.augment_memory_contracts:
        augmented_contracts = _augment_memory_contracts(
            library=library,
            work_root=work_root,
            db_root=db_root,
            load_dataset=load_dataset,
            preprocess_case=preprocess_case,
            SqliteDBSchemaAccess=SqliteDBSchemaAccess,
            apply_delta_structural_override=apply_delta_structural_override,
            build_formation_signals=build_formation_signals,
            build_trigger_contract=build_trigger_contract,
        )
        materialize_library_runtime_contracts(library)

    rows: List[Dict[str, Any]] = []
    case_dirs = sorted(work_root.glob("qid_*"), key=_case_sort_key)
    if wanted_case_ids is not None:
        case_dirs = [p for p in case_dirs if p.name.split("_")[-1] in wanted_case_ids]
    if args.max_cases > 0:
        case_dirs = case_dirs[: args.max_cases]

    for case_dir in case_dirs:
        case_id = case_dir.name.split("_")[-1]
        input_pkl = case_dir / "rewrite_input.pkl"
        row: Dict[str, Any] = {
            "question_id": int(case_id),
            "case_dir": str(case_dir),
        }
        if not input_pkl.exists():
            row["reason"] = "missing_rewrite_input"
            rows.append(row)
            continue

        try:
            dataset = load_dataset(str(input_pkl))
            if not dataset:
                row["reason"] = "empty_dataset"
                rows.append(row)
                continue
            item = dataset[0]
            candidates = [_sql_text(value) for value in list(item.sql_candidates or [])]
            gold_sql = str(getattr(item, "gold_sql", "") or "")
            if not candidates:
                row["reason"] = "no_candidates"
                rows.append(row)
                continue

            db_id = str(item.database_id)
            db_path = db_root / db_id / f"{db_id}.sqlite"
            top1_sql = candidates[0]
            plan = prepare_rewrite_plan(
                db_id=db_id,
                case_id=str(item.question_id),
                question=str(item.question),
                evidence=str(item.evidence or ""),
                pred_top1_sql=top1_sql,
                c0_candidates=candidates,
                library=library,
                db_path=str(db_path),
                database_dir=str(db_path.parent),
                allow_self_singleton_replay=bool(args.allow_self_singleton_replay),
            )
            compiler_output = plan.get("compiler_output")
            matched_groups = list(plan.get("matched_groups") or [])
            trigger_result = plan.get("trigger_result")

            pred_cmp = (
                run_execution_comparison(
                    db_path=str(db_path),
                    pred_sql=top1_sql,
                    gold_sql=gold_sql,
                    row_sample_limit=args.row_sample_limit,
                )
                if gold_sql
                else None
            )
            c0_candidates: List[Dict[str, Any]] = []
            if gold_sql:
                for idx, candidate_sql in enumerate(candidates):
                    if idx == 0 and pred_cmp is not None:
                        candidate_cmp = pred_cmp
                    else:
                        candidate_cmp = run_execution_comparison(
                            db_path=str(db_path),
                            pred_sql=candidate_sql,
                            gold_sql=gold_sql,
                            row_sample_limit=args.row_sample_limit,
                        )
                    c0_candidates.append(
                        {
                            "candidate_index": idx,
                            "sql": candidate_sql,
                            "status": _comparison_status(_payload(candidate_cmp)),
                            "vs_gold": _payload(candidate_cmp),
                        }
                    )
            pred_status = _comparison_status(_payload(pred_cmp))
            c0_oracle_status = (
                "equivalent"
                if any(_is_equivalent(item.get("status", "")) for item in c0_candidates)
                else pred_status
            )
            c0_status = pred_status

            rewrites: List[Dict[str, Any]] = []
            candidate_sqls = candidates if args.rewrite_all_candidates else [top1_sql]
            if plan.get("reason") == "ready" and compiler_output is not None:
                for idx, pred_sql in enumerate(candidate_sqls):
                    rewrite_row: Dict[str, Any] = {
                        "candidate_index": idx,
                        "input_sql": pred_sql,
                    }
                    try:
                        rewrite_result = rewrite_one_candidate(
                            question=str(item.question),
                            evidence=str(item.evidence or ""),
                            pred_sql=pred_sql,
                            compiler_output=compiler_output,
                            local_schema_view=plan["case_view"].local_schema_view,
                            natural_language_hint=plan.get("repair_brief", "")
                            or plan.get("instantiated_hint", "")
                            or "",
                        )
                        rewrite_sql = rewrite_result.get("rewrite_sql")
                        rewrite_cmp = (
                            run_execution_comparison(
                                db_path=str(db_path),
                                pred_sql=str(rewrite_sql or ""),
                                gold_sql=gold_sql,
                                row_sample_limit=args.row_sample_limit,
                            )
                            if rewrite_sql and gold_sql
                            else None
                        )
                        rewrite_row.update(
                            {
                                "rewrite_sql": rewrite_sql,
                                "rewrite_vs_gold": _payload(rewrite_cmp),
                                "rewrite_status": _comparison_status(_payload(rewrite_cmp)),
                                "action_realization_traces": rewrite_result.get(
                                    "action_realization_traces", []
                                ),
                                "dependency_repairs_applied": rewrite_result.get(
                                    "dependency_repairs_applied", []
                                ),
                                "contract_steps_applied": rewrite_result.get(
                                    "contract_steps_applied", []
                                ),
                                "rewrite_realization_origin": rewrite_realization_origin_from_result(
                                    rewrite_result
                                ),
                            }
                        )
                    except Exception as exc:
                        rewrite_row.update(
                            {
                                "rewrite_sql": None,
                                "rewrite_status": "exception",
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                    rewrites.append(rewrite_row)

            rewrite_statuses = [item.get("rewrite_status") for item in rewrites]
            raw_best_rewrite_status = (
                "equivalent"
                if "equivalent" in rewrite_statuses
                else (rewrite_statuses[0] if rewrite_statuses else "not_run")
            )
            c1_equivalent = _is_equivalent(c0_status) or _is_equivalent(raw_best_rewrite_status)
            c1_status = "equivalent" if c1_equivalent else raw_best_rewrite_status
            row.update(
                {
                    "db_id": db_id,
                    "question": str(item.question),
                    "evidence": str(item.evidence or ""),
                    "gold_sql": gold_sql,
                    "top1_sql": top1_sql,
                    "candidate_count": len(candidates),
                    "plan_reason": plan.get("reason"),
                    "rewrite_enabled_reason": plan.get("rewrite_enabled_reason"),
                    "matched_group_ids": [group.group_id for group in matched_groups],
                    "matched_memory_objects": _selected_memory_payload(matched_groups),
                    "trigger_result": {
                        "strategy_version": getattr(trigger_result, "strategy_version", ""),
                        "selected_group_ids": [
                            group.group_id for group in getattr(trigger_result, "selected_groups", [])
                        ],
                        "candidates": _payload(getattr(trigger_result, "candidates", [])),
                    },
                    "compiler_candidate_sets": _payload(plan.get("compiler_candidate_sets")),
                    "compiler_output": _payload(compiler_output),
                    "action_primitives": (
                        _action_primitives(compiler_output.actions) if compiler_output else []
                    ),
                    "raw_hint": plan.get("raw_hint", ""),
                    "instantiated_hint": plan.get("instantiated_hint", ""),
                    "hint_applicable": bool(plan.get("hint_applicable", False)),
                    "hint_instantiation_notes": plan.get("hint_instantiation_notes", ""),
                    "pred_vs_gold": _payload(pred_cmp),
                    "pred_status": pred_status,
                    "c0_candidates": c0_candidates,
                    "c0_status": c0_status,
                    "c0_selection_source": "c0_top1",
                    "c0_oracle_status": c0_oracle_status,
                    "c0_oracle_selection_source": (
                        "c0_any_candidate"
                        if _is_equivalent(c0_oracle_status) and not _is_equivalent(pred_status)
                        else "c0_top1"
                    ),
                    "c0_evaluation_policy": "strict_top1_selected_candidate",
                    "rewrites": rewrites,
                    "raw_best_rewrite_status": raw_best_rewrite_status,
                    "best_rewrite_status": c1_status,
                    "c1_status": c1_status,
                    "final_selection_source": (
                        "c0_any_candidate"
                        if _is_equivalent(c0_status) and not _is_equivalent(pred_status)
                        else "c0_top1"
                        if _is_equivalent(c0_status)
                        else "memory_rewrite"
                        if _is_equivalent(raw_best_rewrite_status)
                        else "none_equivalent"
                    ),
                }
            )
        except Exception as exc:
            row.update(
                {
                    "plan_reason": "exception",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

        rows.append(row)
        _dump_json(output_dir / f"qid_{case_id}.json", row)

    improved = [
        row
        for row in rows
        if not _is_equivalent(row.get("c0_status", row.get("pred_status")))
        and _is_equivalent(row.get("c1_status", row.get("best_rewrite_status")))
    ]
    regressions = [
        row
        for row in rows
        if _is_equivalent(row.get("c0_status", row.get("pred_status")))
        and not _is_equivalent(row.get("c1_status", row.get("best_rewrite_status")))
    ]
    summary = {
        "library_json": str(Path(args.library_json).resolve()),
        "work_root": str(work_root),
        "bird_db_root": str(db_root),
        "case_ids": [row.get("question_id") for row in rows],
        "total_cases": len(rows),
        "ready_cases": sum(1 for row in rows if row.get("plan_reason") == "ready"),
        "rewritten_cases": sum(1 for row in rows if row.get("rewrites")),
        "improved_cases": [row.get("question_id") for row in improved],
        "regression_cases": [row.get("question_id") for row in regressions],
        "pred_equivalent_cases": [
            row.get("question_id") for row in rows if row.get("pred_status") == "equivalent"
        ],
        "rewrite_equivalent_cases": [
            row.get("question_id") for row in rows if row.get("best_rewrite_status") == "equivalent"
        ],
        "augment_memory_contracts": bool(args.augment_memory_contracts),
        "augmented_contracts": augmented_contracts,
        "rewrite_all_candidates": bool(args.rewrite_all_candidates),
        "allow_self_singleton_replay": bool(args.allow_self_singleton_replay),
        "row_sample_limit": args.row_sample_limit,
    }
    payload = {"summary": summary, "cases": rows}
    _dump_json(output_json, payload)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
