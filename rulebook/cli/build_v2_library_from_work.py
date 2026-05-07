#!/usr/bin/env python3
"""Build a formal v2 singleton library from saved DeepEye work cases.

For each saved ``rewrite_input.pkl`` this runs the full v2 offline path:
preprocess -> execution comparison -> wrong-case auditor -> error-instance
extractor -> singleton accumulate. It calls the configured LLM and writes
per-case artifacts after every case so interrupted runs can resume.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _add_paths(deepeye_root: Path, ace_root: Path) -> None:
    for path in (str(ace_root), str(deepeye_root)):
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


def _model_payload(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_model_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_model_payload(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _model_payload(v) for k, v in value.items()}
    return value


def _load_or_create_library(path: Path, db_id: str, LibraryStateV2: Any) -> Any:
    if path.exists():
        return LibraryStateV2.model_validate(_load_json(path))
    return LibraryStateV2(db_id=db_id)


def _replace_singleton(library: Any, singleton: Any) -> None:
    existing = [group for group in library.singletons if group.group_id != singleton.group_id]
    existing.append(singleton)
    library.singletons = sorted(existing, key=lambda group: int(str(group.case_ids[0])))
    library.cases_processed = len(library.singletons)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db_id", required=True)
    parser.add_argument("--work_root", required=True)
    parser.add_argument("--bird_db_root", required=True)
    parser.add_argument("--output_json", required=True)
    parser.add_argument("--summary_json", required=True)
    parser.add_argument("--case_output_dir", required=True)
    parser.add_argument("--case_ids", default="")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--fail_fast", action="store_true")
    parser.add_argument("--deepeye_root", default="/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL")
    parser.add_argument("--ace_root", default="/data/liuyining/ace4sql")
    args = parser.parse_args(argv)

    _add_paths(Path(args.deepeye_root).resolve(), Path(args.ace_root).resolve())

    from app.dataset import load_dataset  # noqa: WPS433
    from method.EEA.rulebook.common.accumulate_v2 import error_instance_to_singleton  # noqa: WPS433
    from method.EEA.rulebook.common.data_structures_v2 import LibraryStateV2  # noqa: WPS433
    from method.EEA.rulebook.common.pipeline_v2 import run_error_instance_pipeline  # noqa: WPS433

    output_json = Path(args.output_json).resolve()
    summary_json = Path(args.summary_json).resolve()
    case_output_dir = Path(args.case_output_dir).resolve()
    work_root = Path(args.work_root).resolve()
    db_root = Path(args.bird_db_root).resolve()
    wanted = _parse_case_ids(args.case_ids)

    library = (
        _load_or_create_library(output_json, args.db_id, LibraryStateV2)
        if args.resume
        else LibraryStateV2(db_id=args.db_id)
    )
    completed_ids = {str(group.case_ids[0]) for group in library.singletons}

    case_dirs = sorted(work_root.glob("qid_*"), key=_case_sort_key)
    if wanted is not None:
        case_dirs = [path for path in case_dirs if path.name.split("_")[-1] in wanted]

    rows: List[Dict[str, Any]] = []
    if args.resume and summary_json.exists():
        old_summary = _load_json(summary_json)
        rows = list(old_summary.get("cases") or [])

    seen_rows = {str(row.get("case_id")): row for row in rows}

    for case_dir in case_dirs:
        case_id = case_dir.name.split("_")[-1]
        if args.resume and case_id in completed_ids:
            seen_rows.setdefault(
                case_id,
                {"case_id": case_id, "status": "ok", "resume_skipped": True},
            )
            continue

        started = time.time()
        row: Dict[str, Any] = {
            "case_id": case_id,
            "case_dir": str(case_dir),
            "status": "running",
        }
        seen_rows[case_id] = row
        _dump_json(summary_json, _summary_payload(args.db_id, output_json, seen_rows))

        input_pkl = case_dir / "rewrite_input.pkl"
        try:
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

            pred_sql = candidates[0]
            db_path = db_root / db_id / f"{db_id}.sqlite"
            result = run_error_instance_pipeline(
                db_id=db_id,
                case_id=case_id,
                question=str(item.question),
                evidence=str(item.evidence or ""),
                pred_sql=pred_sql,
                gold_sql=gold_sql,
                db_path=str(db_path),
                database_dir=str(db_path.parent),
                run_compiler=False,
            )
            singleton = error_instance_to_singleton(
                result.error_instance,
                runtime_usable=True,
                case_audit=result.case_audit,
                runtime_case_view=result.runtime_case_view,
            )
            _replace_singleton(library, singleton)

            artifact = {
                "case_id": case_id,
                "db_id": db_id,
                "question": str(item.question),
                "evidence": str(item.evidence or ""),
                "top1_sql": pred_sql,
                "gold_sql": gold_sql,
                "runtime_case_view": _model_payload(result.runtime_case_view),
                "case_audit": _model_payload(result.case_audit),
                "error_instance": _model_payload(result.error_instance),
                "code_prepared_summary": _model_payload(result.code_prepared_summary),
                "singleton": _model_payload(singleton),
            }
            _dump_json(case_output_dir / f"qid_{case_id}.json", artifact)
            _dump_json(output_json, _model_payload(library))

            row.update(
                {
                    "status": "ok",
                    "elapsed_sec": round(time.time() - started, 3),
                    "group_id": singleton.group_id,
                    "repair_skeleton": _model_payload(
                        singleton.core_interface.repair_skeleton_prototype
                    ),
                    "trigger_required_signal_count": len(
                        singleton.trigger_contract.required_signals
                    ),
                    "trigger_optional_signal_count": len(
                        singleton.trigger_contract.optional_signals
                    ),
                }
            )
        except Exception as exc:
            row.update(
                {
                    "status": "exception",
                    "elapsed_sec": round(time.time() - started, 3),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            if args.fail_fast:
                _dump_json(summary_json, _summary_payload(args.db_id, output_json, seen_rows))
                raise
        _dump_json(summary_json, _summary_payload(args.db_id, output_json, seen_rows))

    payload = _summary_payload(args.db_id, output_json, seen_rows)
    _dump_json(summary_json, payload)
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    return 1 if payload["summary"]["failed_cases"] else 0


def _summary_payload(db_id: str, output_json: Path, rows_by_case: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    rows = [
        rows_by_case[key]
        for key in sorted(rows_by_case, key=lambda value: int(value) if value.isdigit() else value)
    ]
    ok_cases = [int(row["case_id"]) for row in rows if row.get("status") == "ok"]
    failed_cases = [
        {"case_id": int(row["case_id"]), "error": row.get("error", row.get("status"))}
        for row in rows
        if row.get("status") not in {"ok", "running"}
    ]
    running_cases = [int(row["case_id"]) for row in rows if row.get("status") == "running"]
    return {
        "summary": {
            "db_id": db_id,
            "output_json": str(output_json),
            "total_rows": len(rows),
            "ok_count": len(ok_cases),
            "ok_cases": ok_cases,
            "running_cases": running_cases,
            "failed_cases": failed_cases,
        },
        "cases": rows,
    }


if __name__ == "__main__":
    raise SystemExit(main())
