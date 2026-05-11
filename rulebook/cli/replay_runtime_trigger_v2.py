#!/usr/bin/env python3
"""Replay post-selection runtime trigger through compiler, without rewrite.

This probe reads saved ``eea_runtime_request.json`` files from DeepEye
post-selection runs. It calls ``prepare_rewrite_plan`` so trigger, compiler,
and hint-instantiation audit fields are produced, but it never calls the SQL
rewrite LLM.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def _add_paths(deepeye_root: Path, ace_root: Path) -> None:
    for path in (str(ace_root), str(deepeye_root)):
        if path not in sys.path:
            sys.path.insert(0, path)


def _dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")


def _parse_case_ids(raw: str) -> Optional[set[str]]:
    if not raw.strip():
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}


def _case_sort_key(path: Path) -> int:
    try:
        return int(path.name.split("_")[-1])
    except Exception:
        return 0


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


def _candidate_sqls(request: Dict[str, Any]) -> List[str]:
    sqls: List[str] = []
    selected = str(request.get("selected_sql") or "").strip()
    if selected:
        sqls.append(selected)
    for item in request.get("pre_eea_candidates") or []:
        sql = str((item or {}).get("sql") or "").strip()
        if sql and sql not in sqls:
            sqls.append(sql)
    return sqls


def _runtime_status(reason: str) -> str:
    if reason == "ready":
        return "ready"
    if reason in {
        "passthrough_no_action",
        "passthrough_action_count",
        "passthrough_escape_hatch",
        "passthrough_guard_scope_inconsistent",
        "passthrough_unsafe_hint",
    }:
        return "no_action"
    if reason == "passthrough_no_match":
        return "no_match"
    if reason.startswith("retrieval_exception") or reason.startswith("compiler_exception"):
        return "error"
    return reason or "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library_json", required=True, help="Serialized LibraryStateV2 JSON")
    parser.add_argument("--work_root", required=True, help="Run .state/work directory")
    parser.add_argument("--bird_db_root", required=True, help="BIRD dev_databases directory")
    parser.add_argument("--output_json", required=True)
    parser.add_argument("--case_ids", default="", help="Comma-separated qids to replay")
    parser.add_argument("--deepeye_root", default="/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL")
    parser.add_argument("--ace_root", default="/data/liuyining/ace4sql")
    args = parser.parse_args()

    deepeye_root = Path(args.deepeye_root).resolve()
    ace_root = Path(args.ace_root).resolve()
    _add_paths(deepeye_root, ace_root)

    from method.EEA.rulebook.common.core.data_structures import LibraryStateV2  # noqa: WPS433
    from method.EEA.rulebook.common.runtime.runtime import prepare_rewrite_plan  # noqa: WPS433
    from method.EEA.rulebook.common.runtime.trigger_contract import (  # noqa: WPS433
        materialize_library_runtime_contracts,
    )

    with Path(args.library_json).open("r", encoding="utf-8") as f:
        library = LibraryStateV2.model_validate(json.load(f))
    materialize_library_runtime_contracts(library)

    work_root = Path(args.work_root).resolve()
    db_root = Path(args.bird_db_root).resolve()
    wanted_case_ids = _parse_case_ids(args.case_ids)

    rows: List[Dict[str, Any]] = []
    for case_dir in sorted(work_root.glob("qid_*"), key=_case_sort_key):
        case_id = case_dir.name.split("_")[-1]
        if wanted_case_ids is not None and case_id not in wanted_case_ids:
            continue
        request_path = case_dir / "eea_runtime_request.json"
        row: Dict[str, Any] = {"question_id": int(case_id), "case_dir": str(case_dir)}
        if not request_path.exists():
            row["runtime_status"] = "error"
            row["reason"] = "missing_eea_runtime_request"
            rows.append(row)
            continue

        try:
            request = json.load(request_path.open("r", encoding="utf-8"))
            db_id = str(request.get("db_id") or "")
            selected_sql = str(request.get("selected_sql") or "")
            c0_candidates = _candidate_sqls(request)
            db_path = db_root / db_id / f"{db_id}.sqlite"
            plan = prepare_rewrite_plan(
                db_id=db_id,
                case_id=str(request.get("case_id") or case_id),
                question=str(request.get("question") or ""),
                evidence=str(request.get("evidence") or ""),
                pred_top1_sql=selected_sql,
                c0_candidates=c0_candidates,
                library=library,
                db_path=str(db_path),
                database_dir=str(db_path.parent),
            )
            summary = dict(plan.get("runtime_audit_summary") or {})
            compiler = dict(summary.get("compiler") or {})
            hint_audit = dict(summary.get("hint_audit") or {})
            reason = str(plan.get("reason") or "")
            row.update(
                {
                    "db_id": db_id,
                    "question": str(request.get("question") or ""),
                    "selected_sql": selected_sql,
                    "runtime_status": _runtime_status(reason),
                    "reason": reason,
                    "rewrite_enabled_reason": str(plan.get("rewrite_enabled_reason") or ""),
                    "matched_group_ids": [
                        str(getattr(group, "group_id", "") or "")
                        for group in (plan.get("matched_groups") or [])
                    ],
                    "compiler": compiler,
                    "actions": compiler.get("actions") or [],
                    "action_primitives": [
                        str(action.get("primitive") or "")
                        for action in (compiler.get("actions") or [])
                    ],
                    "candidate_counts_by_primitive": compiler.get(
                        "candidate_counts_by_primitive"
                    )
                    or {},
                    "empty_reason_counts": compiler.get("empty_reason_counts") or {},
                    "wuv2_2_disabled_reason_count": sum(
                        int(count)
                        for key, count in (compiler.get("empty_reason_counts") or {}).items()
                        if "wuv2_2_seed_target_binding_disabled" in str(key)
                    ),
                    "hint_audit": hint_audit,
                    "raw_hint": str(plan.get("raw_hint") or ""),
                    "instantiated_hint": str(plan.get("instantiated_hint") or ""),
                    "hint_applicable": bool(plan.get("hint_applicable", False)),
                    "hint_instantiation_notes": str(
                        plan.get("hint_instantiation_notes") or ""
                    ),
                    "trigger": summary.get("trigger") or {},
                }
            )
        except Exception as exc:
            row.update(
                {
                    "runtime_status": "error",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "matched_group_ids": [],
                    "actions": [],
                    "action_primitives": [],
                    "candidate_counts_by_primitive": {},
                    "empty_reason_counts": {},
                    "wuv2_2_disabled_reason_count": 0,
                    "hint_audit": {},
                }
            )
        rows.append(row)

    summary = {
        "library_json": str(Path(args.library_json).resolve()),
        "work_root": str(work_root),
        "bird_db_root": str(db_root),
        "case_ids": [row.get("question_id") for row in rows],
        "total_cases": len(rows),
        "runtime_status_counts": {},
        "ready_cases": [
            row.get("question_id") for row in rows if row.get("runtime_status") == "ready"
        ],
        "wuv2_2_disabled_reason_count": sum(
            int(row.get("wuv2_2_disabled_reason_count") or 0) for row in rows
        ),
        "hint_introduces_non_primitive_action_cases": [
            row.get("question_id")
            for row in rows
            if (row.get("hint_audit") or {}).get("hint_introduces_non_primitive_action")
        ],
    }
    for row in rows:
        status = str(row.get("runtime_status") or "unknown")
        summary["runtime_status_counts"][status] = (
            int(summary["runtime_status_counts"].get(status) or 0) + 1
        )

    _dump_json(Path(args.output_json).resolve(), {"summary": summary, "cases": rows})
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
