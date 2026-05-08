#!/usr/bin/env python3
"""Replay v2 runtime trigger on saved per-case rewrite inputs.

This is an offline audit tool. It does not call an LLM. Current-case trigger is
answer-blind; when requested, saved source-case gold SQL is used only to rebuild
memory-object trigger contracts for legacy libraries that predate contract
storage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List


def _dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")


def _add_paths(deepeye_root: Path, ace_root: Path) -> None:
    for path in (str(ace_root), str(deepeye_root)):
        if path not in sys.path:
            sys.path.insert(0, path)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _parse_case_ids(raw: str) -> set[str] | None:
    if not raw.strip():
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library_json", required=True, help="Serialized LibraryStateV2 JSON")
    parser.add_argument("--work_root", required=True, help="Run .state/work directory with qid_*/rewrite_input.pkl")
    parser.add_argument("--bird_db_root", required=True, help="BIRD dev_databases directory")
    parser.add_argument("--deepeye_root", default="/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL")
    parser.add_argument("--ace_root", default="/data/liuyining/ace4sql")
    parser.add_argument("--output_json", required=True)
    parser.add_argument("--max_selected", type=int, default=1)
    parser.add_argument("--case_ids", default="", help="Comma-separated qids to replay; empty means all")
    parser.add_argument(
        "--augment_memory_contracts",
        action="store_true",
        help="Offline-only: rebuild singleton trigger_contract from saved source cases.",
    )
    parser.add_argument(
        "--allow_self_singleton_replay",
        action="store_true",
        help="Validation-only: allow singleton source cases to replay against themselves.",
    )
    args = parser.parse_args()

    deepeye_root = Path(args.deepeye_root).resolve()
    ace_root = Path(args.ace_root).resolve()
    _add_paths(deepeye_root, ace_root)

    from app.dataset import load_dataset  # noqa: WPS433
    from method.EEA.rulebook.common.core.data_structures import LibraryStateV2  # noqa: WPS433
    from method.EEA.rulebook.common.analysis.code_preprocess import preprocess_case  # noqa: WPS433
    from method.EEA.rulebook.common.io.db_schema_access import SqliteDBSchemaAccess  # noqa: WPS433
    from method.EEA.rulebook.common.runtime.runtime import (  # noqa: WPS433
        build_runtime_case_view,
        trigger_memory_objects,
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
    wanted_case_ids = _parse_case_ids(args.case_ids)
    if args.augment_memory_contracts:
        by_case_id = {
            str(group.case_ids[0]): group
            for group in library.singletons
            if len(group.case_ids) == 1
        }
        for case_dir in sorted(work_root.glob("qid_*"), key=lambda p: int(p.name.split("_")[-1])):
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
                pred_sql=str(candidates[0]),
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
        materialize_library_runtime_contracts(library)
    rows: List[Dict[str, Any]] = []
    for case_dir in sorted(work_root.glob("qid_*"), key=lambda p: int(p.name.split("_")[-1])):
        if wanted_case_ids is not None and case_dir.name.split("_")[-1] not in wanted_case_ids:
            continue
        input_pkl = case_dir / "rewrite_input.pkl"
        if not input_pkl.exists():
            continue
        dataset = load_dataset(str(input_pkl))
        if not dataset:
            continue
        item = dataset[0]
        candidates = list(item.sql_candidates or [])
        if not candidates:
            rows.append(
                {
                    "question_id": int(item.question_id),
                    "db_id": str(item.database_id),
                    "selected_group_ids": [],
                    "reason": "no_candidates",
                }
            )
            continue

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
            pred_top1_sql=str(candidates[0]),
            c0_candidate_sqls=[str(candidate) for candidate in candidates],
            access=access,
            candidate_set_size=len(candidates),
        )
        trigger_result = trigger_memory_objects(
            library=library,
            case_view=case_view,
            db_id=db_id,
            max_selected=args.max_selected,
            allow_self_singleton_replay=bool(args.allow_self_singleton_replay),
        )
        rows.append(
            {
                "question_id": int(item.question_id),
                "db_id": db_id,
                "question_tags": [
                    _enum_value(value)
                    for value in (
                        list(case_view.question_contract.target_entity_roles)
                        + list(case_view.question_contract.answer_slot_types)
                        + list(case_view.question_contract.operators)
                        + list(case_view.question_contract.route_cues)
                    )
                ]
                + (
                    [_enum_value(case_view.question_contract.grain)]
                    if case_view.question_contract.grain
                    else []
                ),
                "pred_tags": [
                    _enum_value(value)
                    for value in case_view.pred_manifestation.manifestation_types
                ],
                "selected_group_ids": [group.group_id for group in trigger_result.selected_groups],
                "candidates": [
                    cand.model_dump(mode="json")
                    if hasattr(cand, "model_dump")
                    else dict(cand)
                    for cand in trigger_result.candidates
                ],
            }
        )

    selected_rows = [row for row in rows if row.get("selected_group_ids")]
    summary = {
        "library_json": str(Path(args.library_json).resolve()),
        "work_root": str(work_root),
        "bird_db_root": str(db_root),
        "total_cases": len(rows),
        "selected_cases": len(selected_rows),
        "max_selected": args.max_selected,
        "augment_memory_contracts": bool(args.augment_memory_contracts),
        "allow_self_singleton_replay": bool(args.allow_self_singleton_replay),
    }
    _dump_json(Path(args.output_json).resolve(), {"summary": summary, "cases": rows})
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
