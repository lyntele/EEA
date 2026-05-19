#!/usr/bin/env python3
"""Offline pair-scoring probe for root-evidence branch axes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

from method.EEA.rulebook.common.core.data_structures import GroupSummary
from method.EEA.rulebook.common.learning import shared_program_synthesizer as sps
from method.EEA.rulebook.common.learning.pattern_formation import score_pair


DEFAULT_BATCH = Path(
    "/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL/workspace/rulebook_runs/"
    "e2e11_qwen3coder_openrouter_20260518_141502"
)

PAIRS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "superhero": (("728", "726"),),
    "european_football_2": (("1087", "1064"),),
    "toxicology": (("206", "249"), ("249", "253")),
    "formula_1": (("891", "902"), ("855", "921")),
    "card_games": (("344", "361"), ("400", "430")),
}


def _library_path(batch_dir: Path, db_id: str) -> Path:
    matches = list(batch_dir.glob(f"e2e11_{db_id}_qwen3coder_openrouter_directs1_*/.state/library_latest.json"))
    if not matches:
        raise FileNotFoundError(f"library_latest.json not found for {db_id} under {batch_dir}")
    return matches[0]


def _singleton_by_case(library: Dict[str, Any]) -> Dict[str, GroupSummary]:
    by_case: Dict[str, GroupSummary] = {}
    for raw in library.get("singletons") or []:
        group = GroupSummary.model_validate(raw)
        for case_id in group.case_ids or []:
            by_case[str(case_id)] = group
    return by_case


def run_probe(batch_dir: Path, pairs: Dict[str, Iterable[Tuple[str, str]]]) -> Dict[str, Any]:
    results = []
    for db_id, db_pairs in pairs.items():
        library = json.loads(_library_path(batch_dir, db_id).read_text())
        by_case = _singleton_by_case(library)
        for left_id, right_id in db_pairs:
            pair = score_pair(by_case[left_id], by_case[right_id])
            results.append(
                {
                    "db_id": db_id,
                    "left_case_id": left_id,
                    "right_case_id": right_id,
                    "accepted": pair.accepted,
                    "semantic_relation": pair.semantic_relation,
                    "program_compatible": pair.program_compatible,
                    "program_blockers": list(pair.program_blockers),
                    "failure_taxonomy": list(pair.failure_taxonomy),
                    "broad_retrieval_reasons": list(pair.broad_retrieval_reasons),
                    "shared_program_basis": pair.shared_program_basis,
                    "score": pair.score,
                }
            )
    required = {
        ("superhero", "728", "726"),
        ("european_football_2", "1087", "1064"),
    }
    return {
        "batch_dir": str(batch_dir),
        "pass_required_pairs": all(
            row["accepted"]
            for row in results
            if (row["db_id"], row["left_case_id"], row["right_case_id"]) in required
        ),
        "pass_reference_pairs": all(
            row["accepted"]
            for row in results
            if (row["db_id"], row["left_case_id"], row["right_case_id"]) not in required
        ),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch_dir", type=Path, default=DEFAULT_BATCH)
    parser.add_argument("--output_json", type=Path, default=Path("/tmp/lowering_branch_axis_pair_probe.json"))
    args = parser.parse_args()
    # Keep this probe offline.  The old failure mode happened before the shared
    # insight LLM was reached; this monkeypatch checks that deterministic pair
    # gates now allow root-aligned branch-axis differences through to synthesis.
    sps._call_shared_insight_judge = lambda **_: {  # type: ignore[attr-defined]
        "compatibility": "compatible",
        "shared_interface_key": "offline-probe-compatible",
        "shared_insight": {
            "repair_interface": "offline-probe-compatible",
            "source_misread": "offline probe",
            "target_preference": "offline probe",
        },
    }
    payload = run_probe(args.batch_dir, PAIRS)
    payload["shared_insight_judge"] = "monkeypatched_compatible_offline_probe"
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    for row in payload["results"]:
        print(
            f"{row['db_id']} {row['left_case_id']}x{row['right_case_id']} "
            f"accepted={row['accepted']} relation={row['semantic_relation']} "
            f"blockers={row['program_blockers']}"
        )
    print("PASS" if payload["pass_required_pairs"] and payload["pass_reference_pairs"] else "FAIL")


if __name__ == "__main__":
    main()
