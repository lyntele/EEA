#!/usr/bin/env python3
"""Offline compiler replay for v2 manual/formal pattern candidates.

This tool bypasses runtime trigger intentionally: each selected pattern is
directly supplied to the Action Compiler for its member cases. It does not run
Memory Rewrite and does not mark patterns runtime-usable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def _add_paths(deepeye_root: Path, ace_root: Path) -> None:
    for path in (str(Path(__file__).resolve().parents[1]), str(ace_root), str(deepeye_root)):
        if path not in sys.path:
            sys.path.insert(0, path)


def _dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")


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
        return {str(key): _payload(item) for key, item in value.items()}
    return value


_POSTHOC_NEEDLES = [
    "gold",
    "gold_sql",
    "benchmark",
    "benchmark answer",
    "ground-truth",
    "ground truth",
    "validated_sql",
    "execution_comparison",
    "row_sets_equivalent",
    "correct answer",
    "正确答案",
]


def _sanitize_text_for_compiler(text: str) -> str:
    """Remove offline/posthoc wording before memory text enters compiler prompts."""
    replacements = [
        ("gold_sql", "target_sql"),
        ("validated_sql", "redacted_sql"),
        ("execution_comparison", "redacted_comparison"),
        ("row_sets_equivalent", "redacted_row_equivalence"),
        ("benchmark answer", "target answer"),
        ("benchmark", "dataset"),
        ("ground-truth", "target"),
        ("ground truth", "target"),
        ("correct answer", "target answer"),
        ("gold", "pattern contract"),
        ("正确答案", "目标输出"),
    ]
    out = str(text)
    for src, dst in replacements:
        out = out.replace(src, dst)
        out = out.replace(src.upper(), dst)
        out = out.replace(src.capitalize(), dst)
    return out


def _sanitize_payload_for_compiler(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_text_for_compiler(value)
    if isinstance(value, list):
        return [_sanitize_payload_for_compiler(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_payload_for_compiler(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_payload_for_compiler(item) for key, item in value.items()}
    return value


def _parse_csv(raw: str) -> Optional[set[str]]:
    if not raw.strip():
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}


def _sql_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    sql = getattr(value, "sql", None)
    if sql is not None:
        return str(sql)
    return str(value)


def _case_sort_key(value: str) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _candidate_summary(candidate_sets: List[Any]) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    total = 0
    for candidate_set in candidate_sets:
        primitive = getattr(candidate_set, "primitive", "")
        primitive_value = str(getattr(primitive, "value", primitive))
        count = len(getattr(candidate_set, "candidates", []) or [])
        total += count
        rows.append(
            {
                "primitive": primitive_value,
                "candidate_count": count,
                "empty_reason": getattr(candidate_set, "empty_reason", None),
            }
        )
    return {"total_candidates": total, "by_primitive": rows}


def _action_summary(compiler_output: Any) -> Dict[str, Any]:
    if compiler_output is None:
        return {"action_count": 0, "actions": []}
    actions = []
    for action in getattr(compiler_output, "actions", []) or []:
        primitive = getattr(action, "primitive", "")
        actions.append(
            {
                "action_id": getattr(action, "action_id", ""),
                "primitive": str(getattr(primitive, "value", primitive)),
                "source_group_id": getattr(action, "source_group_id", ""),
                "selected_candidate_id": getattr(action, "selected_candidate_id", None),
                "risk": str(getattr(getattr(action, "risk", ""), "value", getattr(action, "risk", ""))),
                "used_escape_hatch": bool(getattr(action, "used_escape_hatch", False)),
                "arguments": getattr(action, "arguments", {}),
                "rationale_short": getattr(action, "rationale_short", ""),
            }
        )
    return {"action_count": len(actions), "actions": actions}


def _candidate_by_id(candidate_sets: List[Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for candidate_set in candidate_sets:
        primitive = getattr(candidate_set, "primitive", "")
        primitive_value = str(getattr(primitive, "value", primitive))
        for candidate in getattr(candidate_set, "candidates", []) or []:
            candidate_id = str(getattr(candidate, "candidate_id", "") or "")
            if not candidate_id:
                continue
            out[candidate_id] = {
                "primitive": primitive_value,
                "arguments": getattr(candidate, "arguments", {}) or {},
                "source_group_id": getattr(candidate, "source_group_id", None),
                "source_group_type": str(
                    getattr(getattr(candidate, "source_group_type", None), "value", getattr(candidate, "source_group_type", None))
                ),
            }
    return out


def _candidate_validation(
    *,
    action_summary: Dict[str, Any],
    candidate_sets: List[Any],
) -> Dict[str, Any]:
    by_id = _candidate_by_id(candidate_sets)
    rows: List[Dict[str, Any]] = []
    all_passed = True
    for action in action_summary.get("actions") or []:
        candidate_id = action.get("selected_candidate_id")
        candidate = by_id.get(str(candidate_id or ""))
        row = {
            "action_id": action.get("action_id"),
            "selected_candidate_id": candidate_id,
            "candidate_found": candidate is not None,
            "primitive_match": False,
            "source_group_match": False,
            "arguments_match": False,
        }
        if candidate is not None:
            row.update(
                {
                    "primitive_match": candidate.get("primitive") == action.get("primitive"),
                    "source_group_match": candidate.get("source_group_id") == action.get("source_group_id"),
                    "arguments_match": candidate.get("arguments") == (action.get("arguments") or {}),
                    "candidate_arguments": candidate.get("arguments"),
                }
            )
        passed = bool(
            row["candidate_found"]
            and row["primitive_match"]
            and row["source_group_match"]
            and row["arguments_match"]
        )
        row["passed"] = passed
        all_passed = all_passed and passed
        rows.append(row)
    return {
        "all_passed": all_passed,
        "checked_actions": len(rows),
        "checks": rows,
    }


def _compiler_selection_diagnostics(compiler_output: Any) -> Dict[str, Any]:
    notes = ""
    if compiler_output is not None and getattr(compiler_output, "schema_diagnostics", None) is not None:
        notes = str(getattr(compiler_output.schema_diagnostics, "notes", "") or "")
    raw_action_count: Optional[int] = None
    final_action_count: Optional[int] = None
    import re as _re

    match = _re.search(
        r"raw_action_count=(\d+);\s*final_action_count=(\d+)",
        notes,
    )
    if match:
        raw_action_count = int(match.group(1))
        final_action_count = int(match.group(2))
    validation_issue_markers = [
        "deduplicated_semantic_actions",
        "parse_exception",
        "missing_selected_candidate_id",
        "unknown_selected_candidate_id",
        "duplicate_selected_candidate_id",
        "escape_hatch_with_selected_candidate_id",
        "extra_escape_hatch_action",
        "action_count_contract_enforced",
    ]
    return {
        "notes": notes,
        "raw_action_count": raw_action_count,
        "final_action_count": final_action_count,
        "validation_issues": [
            marker for marker in validation_issue_markers if marker in notes
        ],
    }


def _posthoc_field_scan(payload: Any) -> Dict[str, bool]:
    text = json.dumps(_payload(payload), ensure_ascii=False, default=str).lower()
    return {needle: needle in text for needle in _POSTHOC_NEEDLES}


def _strip_diagnostic_scans(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_diagnostic_scans(item) for item in value]
    if isinstance(value, tuple):
        return [_strip_diagnostic_scans(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _strip_diagnostic_scans(item)
            for key, item in value.items()
            if key not in {"compiler_memory_posthoc_scan", "posthoc_field_scan"}
        }
    return value


def _rows_memory_posthoc_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    hits: List[Dict[str, Any]] = []
    for row in rows:
        scan = row.get("compiler_memory_posthoc_scan") or {}
        hit_keys = [key for key, value in scan.items() if value]
        if hit_keys:
            hits.append(
                {
                    "pattern_id": row.get("pattern_id"),
                    "case_id": row.get("case_id"),
                    "hits": hit_keys,
                }
            )
    return {"all_false": not hits, "hits": hits}


def _bounded_pass(
    action_summary: Dict[str, Any],
    expected_primitive: str,
    max_actions: int,
    compiler_selection_diagnostics: Optional[Dict[str, Any]] = None,
) -> bool:
    diagnostics = compiler_selection_diagnostics or {}
    if diagnostics.get("validation_issues"):
        return False
    actions = action_summary.get("actions") or []
    if len(actions) != max_actions:
        return False
    for action in actions:
        if action.get("used_escape_hatch"):
            return False
        if not action.get("selected_candidate_id"):
            return False
        if expected_primitive and action.get("primitive") != expected_primitive:
            return False
    return True


def _pattern_skeleton(group: Any) -> Dict[str, Any]:
    structural = group.core_interface.repair_skeleton_prototype.structural
    return {
        "locus": structural.locus.value,
        "op_family": structural.op_family.value,
        "target_family": structural.target_family.value,
        "output_contract": structural.output_contract.value,
    }


def _compiler_safe_group(group: Any) -> Any:
    """Return a copy stripped of offline-only/posthoc evidence for compiler prompts."""
    from method.EEA.rulebook.common.data_structures_v2 import (
        GroupFormationEvidence,
        GroupLifecycle,
        GroupSummary,
        ModelProfile,
    )

    trigger_contract = group.trigger_contract.model_copy(
        update={
            "source_case_contract": {},
        }
    )
    safe_group = group.model_copy(
        update={
            "formation_signals": {},
            "formation_evidence": GroupFormationEvidence(
                formation_version="compiler-replay-sanitized",
                review_status=group.formation_evidence.review_status,
            ),
            "trigger_contract": trigger_contract,
            "trigger_policy": group.trigger_policy.model_copy(
                update={"notes": "sanitized for compiler replay; offline evidence stripped"}
            ),
            "model_profile": ModelProfile(),
            "replay_history": [],
            "action_realization_traces": [],
            "lifecycle": GroupLifecycle(
                promotion_state=group.lifecycle.promotion_state,
            ),
            "created_at": None,
            "last_updated_at": None,
        }
    )
    return GroupSummary.model_validate(
        _sanitize_payload_for_compiler(safe_group.model_dump(mode="json"))
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library_json", required=True)
    parser.add_argument("--work_root", required=True)
    parser.add_argument("--bird_db_root", required=True)
    parser.add_argument("--output_json", required=True)
    parser.add_argument("--output_dir", default="")
    parser.add_argument("--pattern_ids", default="")
    parser.add_argument("--case_ids", default="")
    parser.add_argument("--max_rows", type=int, default=0)
    parser.add_argument("--no_llm", action="store_true", help="Only enumerate code candidates; skip LLM selection.")
    parser.add_argument("--expected_primitive", default="")
    parser.add_argument("--max_actions_per_case", type=int, default=1)
    parser.add_argument("--deepeye_root", default="/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL")
    parser.add_argument("--ace_root", default="/data/liuyining/ace4sql")
    args = parser.parse_args(argv)

    _add_paths(Path(args.deepeye_root).resolve(), Path(args.ace_root).resolve())

    from app.dataset import load_dataset  # noqa: WPS433
    from method.EEA.rulebook.common.action_compiler_v2 import enumerate_candidates  # noqa: WPS433
    from method.EEA.rulebook.common.data_structures_v2 import LibraryStateV2  # noqa: WPS433
    from method.EEA.rulebook.common.db_schema_access_v2 import SqliteDBSchemaAccess  # noqa: WPS433
    from method.EEA.rulebook.common.llm_nodes_v2 import run_action_compiler  # noqa: WPS433
    from method.EEA.rulebook.common.runtime_v2 import build_runtime_case_view  # noqa: WPS433

    library = LibraryStateV2.model_validate(_load_json(Path(args.library_json)))
    work_root = Path(args.work_root).resolve()
    db_root = Path(args.bird_db_root).resolve()
    output_json = Path(args.output_json).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else output_json.parent / "cases"
    wanted_patterns = _parse_csv(args.pattern_ids)
    wanted_cases = _parse_csv(args.case_ids)

    patterns = list(library.patterns)
    if wanted_patterns is not None:
        patterns = [pattern for pattern in patterns if pattern.group_id in wanted_patterns]

    rows: List[Dict[str, Any]] = []
    for pattern in patterns:
        case_ids = sorted([str(case_id) for case_id in pattern.case_ids], key=_case_sort_key)
        if wanted_cases is not None:
            case_ids = [case_id for case_id in case_ids if case_id in wanted_cases]
        for case_id in case_ids:
            if args.max_rows and len(rows) >= args.max_rows:
                break
            case_dir = work_root / f"qid_{case_id}"
            input_pkl = case_dir / "rewrite_input.pkl"
            row: Dict[str, Any] = {
                "pattern_id": pattern.group_id,
                "pattern_runtime_usable": pattern.runtime_usable,
                "pattern_review_status": pattern.formation_evidence.review_status,
                "pattern_skeleton": _pattern_skeleton(pattern),
                "case_id": case_id,
                "compiler_memory_sanitized": True,
                "status": "running",
            }
            try:
                if not input_pkl.exists():
                    raise RuntimeError("missing_rewrite_input")
                dataset = load_dataset(str(input_pkl))
                if not dataset:
                    raise RuntimeError("empty_dataset")
                item = dataset[0]
                candidates = [_sql_text(value) for value in list(item.sql_candidates or [])]
                if not candidates:
                    raise RuntimeError("no_sql_candidates")
                db_id = str(item.database_id)
                db_path = db_root / db_id / f"{db_id}.sqlite"
                access = SqliteDBSchemaAccess(
                    db_id=db_id,
                    db_path=str(db_path),
                    database_dir=str(db_path.parent),
                )
                case_view = build_runtime_case_view(
                    db_id=db_id,
                    case_id=str(item.question_id),
                    question=str(item.question),
                    evidence=str(item.evidence or ""),
                    pred_top1_sql=candidates[0],
                    c0_candidate_sqls=candidates,
                    access=access,
                    t_mem=[],
                    candidate_set_size=len(candidates),
                )
                compiler_pattern = _compiler_safe_group(pattern)
                compiler_memory_posthoc_scan = _posthoc_field_scan(compiler_pattern)
                candidate_sets, schema_diag = enumerate_candidates(
                    case_view=case_view,
                    memory_objects=[compiler_pattern],
                )
                candidate_summary = _candidate_summary(candidate_sets)
                compiler_output = None
                if not args.no_llm and candidate_summary["total_candidates"] > 0:
                    compiler_output = run_action_compiler(
                        runtime_case_view=case_view,
                        memory_objects=[compiler_pattern],
                        precomputed_candidate_sets=candidate_sets,
                        precomputed_schema_diagnostics=schema_diag,
                    )
                action_summary = _action_summary(compiler_output)
                candidate_validation = _candidate_validation(
                    action_summary=action_summary,
                    candidate_sets=candidate_sets,
                )
                compiler_selection_diagnostics = _compiler_selection_diagnostics(compiler_output)
                bounded_pass = (
                    _bounded_pass(
                        action_summary,
                        expected_primitive=args.expected_primitive,
                        max_actions=max(1, args.max_actions_per_case),
                        compiler_selection_diagnostics=compiler_selection_diagnostics,
                    )
                    if not args.no_llm
                    else None
                )
                row.update(
                    {
                        "status": "ok",
                        "db_id": db_id,
                        "question": str(item.question),
                        "top1_sql": candidates[0],
                        "candidate_summary": candidate_summary,
                        "schema_diagnostics": _payload(schema_diag),
                        "compiler_ran": not args.no_llm and candidate_summary["total_candidates"] > 0,
                        "compiler_memory_posthoc_scan": compiler_memory_posthoc_scan,
                        "compiler_selection_diagnostics": compiler_selection_diagnostics,
                        "action_summary": action_summary,
                        "selected_candidate_validation": candidate_validation,
                        "bounded_check": {
                            "enabled": not args.no_llm,
                            "expected_primitive": args.expected_primitive,
                            "max_actions_per_case": max(1, args.max_actions_per_case),
                            "passed": bounded_pass,
                        },
                        "compile_status": (
                            "actions"
                            if action_summary["action_count"] > 0
                            else "candidates_only"
                            if candidate_summary["total_candidates"] > 0
                            else "no_candidates"
                        ),
                    }
                )
            except Exception as exc:
                row.update({"status": "exception", "error": f"{type(exc).__name__}: {exc}"})
            rows.append(row)
            _dump_json(output_dir / f"{pattern.group_id}__qid_{case_id}.json", row)
        if args.max_rows and len(rows) >= args.max_rows:
            break

    attempted = [row for row in rows if row.get("status") == "ok"]
    with_candidates = [row for row in attempted if (row.get("candidate_summary") or {}).get("total_candidates", 0) > 0]
    with_actions = [row for row in attempted if (row.get("action_summary") or {}).get("action_count", 0) > 0]
    bounded_rows = [
        row
        for row in attempted
        if (row.get("bounded_check") or {}).get("passed") is True
    ]
    summary = {
        "library_json": str(Path(args.library_json).resolve()),
        "pattern_count": len(patterns),
        "rows": len(rows),
        "ok_rows": len(attempted),
        "exception_rows": len([row for row in rows if row.get("status") == "exception"]),
        "candidate_coverage": (len(with_candidates) / len(attempted)) if attempted else 0.0,
        "compile_coverage": (len(with_actions) / len(attempted)) if attempted else 0.0,
        "bounded_coverage": (len(bounded_rows) / len(attempted)) if attempted and not args.no_llm else None,
        "llm_enabled": not args.no_llm,
    }
    payload = {"summary": summary, "rows": rows}
    payload["compiler_memory_posthoc_summary"] = _rows_memory_posthoc_summary(rows)
    payload["posthoc_field_scan"] = _posthoc_field_scan(_strip_diagnostic_scans(payload))
    _dump_json(output_json, payload)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["exception_rows"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
