#!/usr/bin/env python3
"""Offline probe for branch required signals derived from repair output roles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from method.EEA.rulebook.common.core.data_structures import GroupSummary
from method.EEA.rulebook.common.io.db_schema_access import SqliteDBSchemaAccess
from method.EEA.rulebook.common.learning.pattern_formation import _branch_spec_required_signals
from method.EEA.rulebook.common.runtime.runtime import build_current_case_signals, build_runtime_case_view


DEFAULT_BATCH = Path(
    "/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL/workspace/rulebook_runs/"
    "e2e11_qwen3coder_openrouter_20260518_141502"
)
DEFAULT_DB_ROOT = Path("/data/liuyining/ace4sql/bench/bird/dev/dev_databases")


def _candidate_sqls(request: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    selected = str(request.get("selected_sql") or "").strip()
    if selected:
        out.append(selected)
    for item in request.get("pre_eea_candidates") or []:
        sql = str((item or {}).get("sql") or "").strip()
        if sql and sql not in out:
            out.append(sql)
    return out


def _load_singleton(library_path: Path, case_id: str) -> GroupSummary:
    library = json.loads(library_path.read_text())
    for raw in library.get("singletons") or []:
        if case_id in {str(item) for item in (raw.get("case_ids") or [])}:
            return GroupSummary.model_validate(raw)
    raise KeyError(f"singleton for case {case_id} not found in {library_path}")


def _case_signals(work_root: Path, db_root: Path, case_id: str) -> List[str]:
    request_path = work_root / f"qid_{case_id}" / "eea_runtime_request.json"
    request = json.loads(request_path.read_text())
    db_id = str(request.get("db_id") or "")
    db_path = db_root / db_id / f"{db_id}.sqlite"
    access = SqliteDBSchemaAccess(db_id, str(db_path), str(db_path.parent))
    view = build_runtime_case_view(
        db_id=db_id,
        case_id=case_id,
        question=str(request.get("question") or ""),
        evidence=str(request.get("evidence") or ""),
        pred_top1_sql=str(request.get("selected_sql") or ""),
        c0_candidate_sqls=_candidate_sqls(request),
        access=access,
    )
    return sorted(build_current_case_signals(view))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch_dir", type=Path, default=DEFAULT_BATCH)
    parser.add_argument("--db_root", type=Path, default=DEFAULT_DB_ROOT)
    parser.add_argument("--output_json", type=Path, default=Path("/tmp/branch_role_signal_probe.json"))
    args = parser.parse_args()

    run_dir = next(args.batch_dir.glob("e2e11_toxicology_qwen3coder_openrouter_directs1_*"))
    library_path = run_dir / ".state" / "library_latest.json"
    work_root = run_dir / ".state" / "work"
    singleton = _load_singleton(library_path, "206")
    required = _branch_spec_required_signals(branch_groups=[singleton])
    role_required = [item for item in required if item.startswith("pred.contains_column_role=")]
    q249 = _case_signals(work_root, args.db_root, "249")
    q216 = _case_signals(work_root, args.db_root, "216")
    payload = {
        "library_json": str(library_path),
        "work_root": str(work_root),
        "singleton_group_id": singleton.group_id,
        "required_signals": required,
        "role_required_signals": role_required,
        "q249_matches_role_signals": sorted(set(role_required) & set(q249)),
        "q216_matches_role_signals": sorted(set(role_required) & set(q216)),
        "pass": bool(role_required)
        and bool(set(role_required) & set(q249))
        and not bool(set(role_required) & set(q216)),
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
