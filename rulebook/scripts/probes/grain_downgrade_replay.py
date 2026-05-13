#!/usr/bin/env python3
"""Replay selected v6 singleton pair scores after B2 grain-veto downgrade."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from method.EEA.rulebook.common.core.data_structures import GroupSummary, LibraryStateV2
from method.EEA.rulebook.common.learning.pattern_formation import score_pair


DEFAULT_RUN_ROOT = Path("/data/liuyining/ace4sql/method/EEA/rulebook/outputs/retrieval_root_evidence_v6")
DEFAULT_OUTPUT_JSON = Path("/data/liuyining/ace4sql/method/EEA/rulebook/workspace/probes/grain_downgrade_pair_replay.json")
DEFAULT_OUTPUT_MD = Path("/data/liuyining/ace4sql/method/EEA/rulebook/doc/grain_downgrade_pair_replay.md")

PAIR_SPECS: Dict[str, List[Tuple[str, str, str]]] = {
    "codebase_community": [
        ("posthistory", "602", "631"),
        ("posthistory", "631", "635"),
        ("posthistory", "631", "640"),
        ("posthistory", "631", "652"),
        ("posthistory", "635", "640"),
        ("posthistory", "635", "652"),
        ("posthistory", "640", "652"),
        ("editor_to_owner_control", "581", "582"),
    ],
    "toxicology": [
        ("toxA", "198", "201"),
        ("toxA", "201", "207"),
        ("toxA", "207", "326"),
        ("toxA", "326", "328"),
        ("toxA", "335", "338"),
        ("toxA", "263", "269"),
        ("mixed_control", "219", "239"),
        ("mixed_control", "201", "239"),
        ("mixed_control", "239", "317"),
    ],
}


def _load_library(path: Path) -> LibraryStateV2:
    with path.open("r", encoding="utf-8") as handle:
        return LibraryStateV2.model_validate(json.load(handle))


def _singleton_by_case(library: LibraryStateV2) -> Dict[str, GroupSummary]:
    out: Dict[str, GroupSummary] = {}
    for group in library.singletons or []:
        for case_id in group.case_ids or []:
            out[str(case_id)] = group
    return out


def _old_semantic_relation(pair: Any) -> str:
    blockers = [str(item) for item in pair.program_blockers or []]
    blocker_text = ";".join(blockers)
    if "grain_treated_as_branch_axis:output_grain_conflict" in blocker_text:
        return "direct_merge_veto"
    return str(pair.semantic_relation or "")


def _old_branchable(pair: Any) -> bool:
    return bool(pair.broad_retrieval_reasons) and _old_semantic_relation(pair) in {"compatible", "partial"}


def _row(db_id: str, label: str, left: GroupSummary, right: GroupSummary) -> Dict[str, Any]:
    pair = score_pair(left, right)
    old_relation = _old_semantic_relation(pair)
    old_branchable = _old_branchable(pair)
    return {
        "db_id": db_id,
        "label": label,
        "left_case_id": str(left.case_ids[0]) if left.case_ids else "",
        "right_case_id": str(right.case_ids[0]) if right.case_ids else "",
        "old_semantic_relation": old_relation,
        "new_semantic_relation": pair.semantic_relation,
        "old_branchable": old_branchable,
        "new_branchable": pair.branchable_for_pattern,
        "veto_reason": pair.veto_reason,
        "program_compatible": pair.program_compatible,
        "program_blockers": list(pair.program_blockers or []),
        "failure_taxonomy": list(pair.failure_taxonomy or []),
        "broad_retrieval_reasons": list(pair.broad_retrieval_reasons or []),
        "changed": old_relation != pair.semantic_relation or old_branchable != pair.branchable_for_pattern,
    }


def _sort_case(case_id: str) -> Any:
    return int(case_id) if str(case_id).isdigit() else str(case_id)


def _summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    target_labels = {"posthistory", "toxA"}
    target_rows = [row for row in rows if row["label"] in target_labels]
    converted = [
        row for row in target_rows
        if row["old_semantic_relation"] == "direct_merge_veto"
        and row["new_semantic_relation"] in {"compatible", "partial"}
    ]
    control_rows = [row for row in rows if row["label"].endswith("control") or row["label"] == "mixed_control"]
    control_regressions = [
        row for row in control_rows
        if row["old_semantic_relation"] == "compatible"
        and row["new_semantic_relation"] != "compatible"
    ]
    return {
        "target_pairs": len(target_rows),
        "target_converted_from_direct_merge_veto": len(converted),
        "target_conversion_rate": (len(converted) / len(target_rows)) if target_rows else 0.0,
        "control_pairs": len(control_rows),
        "control_compatible_to_incompatible": len(control_regressions),
    }


def _render(rows: List[Dict[str, Any]], summary: Dict[str, Any]) -> str:
    lines = [
        "# Grain Downgrade Pair Replay",
        "",
        "Recomputed selected v6 singleton pairs with the B2 grain-veto downgrade patch.",
        "",
        "## Summary",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| target pairs | {summary['target_pairs']} |",
        f"| target converted from direct_merge_veto | {summary['target_converted_from_direct_merge_veto']} |",
        f"| target conversion rate | {summary['target_conversion_rate']:.2%} |",
        f"| control pairs | {summary['control_pairs']} |",
        f"| control compatible to incompatible | {summary['control_compatible_to_incompatible']} |",
        "",
        "## Pairs",
        "",
        "| db | label | pair | old relation | new relation | old branchable | new branchable | broad reasons | blockers |",
        "|---|---|---|---|---|---:|---:|---|---|",
    ]
    for row in rows:
        pair = f"{row['left_case_id']}-{row['right_case_id']}"
        lines.append(
            f"| {row['db_id']} | {row['label']} | {pair} | "
            f"{row['old_semantic_relation']} | {row['new_semantic_relation']} | "
            f"{str(row['old_branchable']).lower()} | {str(row['new_branchable']).lower()} | "
            f"{', '.join(row['broad_retrieval_reasons'])} | {', '.join(str(item) for item in row['program_blockers'])} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run_root", default=str(DEFAULT_RUN_ROOT))
    parser.add_argument("--output_json", default=str(DEFAULT_OUTPUT_JSON))
    parser.add_argument("--output_md", default=str(DEFAULT_OUTPUT_MD))
    args = parser.parse_args()

    run_root = Path(args.run_root).resolve()
    rows: List[Dict[str, Any]] = []
    missing: List[Dict[str, str]] = []
    for db_id, specs in PAIR_SPECS.items():
        library = _load_library(run_root / db_id / "library.json")
        by_case = _singleton_by_case(library)
        for label, left_case, right_case in specs:
            left = by_case.get(left_case)
            right = by_case.get(right_case)
            if left is None or right is None:
                missing.append({
                    "db_id": db_id,
                    "label": label,
                    "left_case_id": left_case,
                    "right_case_id": right_case,
                })
                continue
            rows.append(_row(db_id, label, left, right))
    rows.sort(key=lambda row: (row["db_id"], row["label"], _sort_case(row["left_case_id"]), _sort_case(row["right_case_id"])))
    summary = _summarize(rows)
    payload = {
        "schema_version": "grain-downgrade-pair-replay-v1",
        "run_root": str(run_root),
        "summary": summary,
        "missing_pairs": missing,
        "rows": rows,
    }
    output_json = Path(args.output_json).resolve()
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    output_md = Path(args.output_md).resolve()
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text(_render(rows, summary), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if missing:
        print(json.dumps({"missing_pairs": missing}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
