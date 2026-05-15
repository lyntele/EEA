"""Patch existing library patterns with aggregated retrieval_evidence.

This is a probe-only utility for validating runtime route-evidence matching on
libraries produced before pattern-level retrieval_evidence was persisted.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping


def _items(values: Iterable[Any]) -> List[str]:
    return [str(value) for value in values if str(value)]


def aggregate(members: List[Mapping[str, Any]]) -> Dict[str, Any]:
    gold_tables: set[str] = set()
    pred_tables: set[str] = set()
    edges: set[str] = set()
    roles: List[str] = []
    eqs: set[str] = set()
    for member in members:
        ev = (member.get("formation_signals") or {}).get("retrieval_evidence") or {}
        gold_tables.update(_items(ev.get("gold_only_tables") or []))
        pred_tables.update(_items(ev.get("pred_only_tables") or []))
        edges.update(_items(ev.get("gold_join_edges") or []))
        role = str(ev.get("target_output_role") or "").strip()
        if role:
            roles.append(role)
        eqs.update(_items(ev.get("target_relation_equalities") or []))
    majority_role = Counter(roles).most_common(1)
    return {
        "schema_version": "pattern-route-evidence-v0",
        "gold_only_tables": sorted(gold_tables),
        "pred_only_tables": sorted(pred_tables),
        "gold_join_edges": sorted(edges),
        "target_output_role": majority_role[0][0] if majority_role else "",
        "target_relation_equalities": sorted(eqs),
        "member_count": len(members),
    }


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: patch_library_retrieval_evidence.py <library_json> <output_json>",
            file=sys.stderr,
        )
        return 2
    lib_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    lib = json.loads(lib_path.read_text())
    by_cid: Dict[str, Mapping[str, Any]] = {}
    for singleton in lib.get("singletons") or []:
        for case_id in singleton.get("case_ids") or []:
            by_cid[str(case_id)] = singleton

    patched = 0
    for pattern in lib.get("patterns") or []:
        members = [
            by_cid[str(case_id)]
            for case_id in pattern.get("case_ids") or []
            if str(case_id) in by_cid
        ]
        pattern.setdefault("formation_signals", {})["retrieval_evidence"] = aggregate(members)
        patched += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(lib, ensure_ascii=False, indent=2, default=str))
    print(f"Patched {patched} patterns -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
