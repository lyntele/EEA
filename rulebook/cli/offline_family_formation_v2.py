#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional


REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from method.EEA.rulebook.common.family_formation_v2 import (  # noqa: E402
    form_offline_families,
    load_library_state,
)


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _model_dump(obj: Any) -> Dict[str, Any]:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return dict(obj)


def _add_optional_paths(*paths: Path) -> None:
    for path in paths:
        value = str(path.resolve())
        if value not in sys.path:
            sys.path.insert(0, value)


def _augment_library_signals(
    library: Any,
    *,
    work_root: Optional[str],
    bird_db_root: Optional[str],
    deepeye_root: str,
    ace_root: str,
) -> int:
    """Attach compact formation signals from saved rewrite inputs.

    This is offline-only and uses each case's saved top-1 pred SQL plus gold SQL
    to reconstruct the delta summary for existing singletons.
    """
    if not work_root:
        return 0
    if not bird_db_root:
        raise ValueError("--bird_db_root is required when --work_root is provided")

    _add_optional_paths(Path(ace_root), Path(deepeye_root))

    from app.dataset import load_dataset  # noqa: WPS433
    from method.EEA.rulebook.common.code_preprocess_v2 import preprocess_case  # noqa: WPS433
    from method.EEA.rulebook.common.db_schema_access_v2 import SqliteDBSchemaAccess  # noqa: WPS433
    from method.EEA.rulebook.common.signal_summary_v2 import build_formation_signals  # noqa: WPS433

    by_case_id = {}
    for group in list(library.singletons or []):
        if len(group.case_ids) == 1:
            by_case_id[str(group.case_ids[0])] = group

    root = Path(work_root)
    db_root = Path(bird_db_root)
    attached = 0
    for case_dir in sorted(root.glob("qid_*"), key=lambda p: int(p.name.split("_")[-1])):
        case_id = case_dir.name.split("_")[-1]
        group = by_case_id.get(str(case_id))
        if group is None:
            continue
        input_pkl = case_dir / "rewrite_input.pkl"
        if not input_pkl.exists():
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
        signals = build_formation_signals(
            case_signal_view=prepared.get("case_signal_view"),
            delta_signature=prepared.get("delta_signature"),
        )
        signals["case_id"] = str(item.question_id)
        group.formation_signals = signals
        attached += 1
    return attached


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build offline EEA v2 experience-family candidates from singleton libraries."
    )
    parser.add_argument("--library_json", required=True, help="Input v2 final_library.json")
    parser.add_argument("--output_library_json", required=True, help="Output v2 library JSON")
    parser.add_argument("--report_json", required=True, help="Output family formation report JSON")
    parser.add_argument(
        "--manual_groups_json",
        default=None,
        help="Optional doc/db_pattern_groups.json for pairwise alignment diagnostics",
    )
    parser.add_argument(
        "--max_neighbor_edges",
        type=int,
        default=5,
        help="Max accepted merge edges per singleton before connected-component formation",
    )
    parser.add_argument(
        "--mark_runtime_usable_after_replay",
        action="store_true",
        help=(
            "Deprecated safety switch. Direct runtime marking is disabled; "
            "compiler replay/promotion must happen in the dedicated replay path."
        ),
    )
    parser.add_argument(
        "--work_root",
        default=None,
        help="Optional run .state/work directory for reconstructing formation signals",
    )
    parser.add_argument(
        "--case_ids",
        default=None,
        help=(
            "Comma-separated case ids for a focused diagnostic run. Pair "
            "enumeration is restricted to these active singletons; non-focus "
            "singletons are preserved unchanged in the output library."
        ),
    )
    parser.add_argument(
        "--bird_db_root",
        default=None,
        help="BIRD dev_databases root, required with --work_root",
    )
    parser.add_argument(
        "--deepeye_root",
        default="/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL",
    )
    parser.add_argument("--ace_root", default="/data/liuyining/ace4sql")
    args = parser.parse_args(argv)

    library_path = Path(args.library_json)
    output_library_path = Path(args.output_library_json)
    report_path = Path(args.report_json)
    manual_payload = _load_json(Path(args.manual_groups_json)) if args.manual_groups_json else None

    library = load_library_state(_load_json(library_path))
    focus_case_ids = {
        value.strip()
        for value in str(args.case_ids or "").split(",")
        if value.strip()
    }
    attached_signals = _augment_library_signals(
        library,
        work_root=args.work_root,
        bird_db_root=args.bird_db_root,
        deepeye_root=args.deepeye_root,
        ace_root=args.ace_root,
    )

    output_library, report = form_offline_families(
        library,
        manual_groups=manual_payload,
        max_neighbor_edges=args.max_neighbor_edges,
        mark_runtime_usable=args.mark_runtime_usable_after_replay,
        focus_case_ids=focus_case_ids,
    )

    report = {
        "source_library": str(library_path.resolve()),
        "output_library": str(output_library_path.resolve()),
        "attached_formation_signals": attached_signals,
        **report,
    }
    _dump_json(output_library_path, _model_dump(output_library))
    _dump_json(report_path, report)
    print(
        "offline_family_formation_v2: "
        f"families={report['output_counts']['new_experience_families']} "
        f"remaining_singletons={report['output_counts']['remaining_singletons']} "
        f"report={report_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
