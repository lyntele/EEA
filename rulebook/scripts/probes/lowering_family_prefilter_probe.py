#!/usr/bin/env python3
"""Dry-run the lowering-family pair prefilter on a serialized library."""

from __future__ import annotations

import argparse
import json
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, List

from method.EEA.rulebook.common.core.data_structures import LibraryStateV2
from method.EEA.rulebook.common.learning.pattern_formation import _canonical_lowering_families


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--library_json", required=True)
    parser.add_argument("--output_json")
    args = parser.parse_args()

    with Path(args.library_json).open("r", encoding="utf-8") as handle:
        library = LibraryStateV2.model_validate(json.load(handle))

    singletons = list(library.singletons or [])
    pairs: List[Dict[str, Any]] = []
    comparable = 0
    skipped = 0
    for left, right in combinations(singletons, 2):
        left_families = sorted(_canonical_lowering_families(left))
        right_families = sorted(_canonical_lowering_families(right))
        if not left_families or not right_families:
            continue
        comparable += 1
        incompatible = not (set(left_families) & set(right_families))
        if incompatible:
            skipped += 1
        pairs.append(
            {
                "left_group_id": left.group_id,
                "right_group_id": right.group_id,
                "left_case_ids": [str(case_id) for case_id in (left.case_ids or [])],
                "right_case_ids": [str(case_id) for case_id in (right.case_ids or [])],
                "left_lowering_families": left_families,
                "right_lowering_families": right_families,
                "lowering_family_incompatible": incompatible,
            }
        )

    summary = {
        "library_json": str(Path(args.library_json).resolve()),
        "singletons": len(singletons),
        "total_pairs": len(singletons) * (len(singletons) - 1) // 2,
        "comparable_pairs": comparable,
        "skipped_pairs": skipped,
        "skip_rate": (skipped / comparable) if comparable else 0.0,
    }
    payload = {"summary": summary, "pairs": pairs}
    if args.output_json:
        Path(args.output_json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
