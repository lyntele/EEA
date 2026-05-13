#!/usr/bin/env python3
"""Validate retrieval root keys against manual pattern pairs.

This probe is read-only. It uses previously dumped case tool payloads from
`workspace/probes/pattern_clustering_signal_audit`, reprojects retrieval
evidence, and computes pair coverage for the manual patterns targeted by
`doc/retrieval_root_evidence_plan.md`.
"""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

from method.EEA.rulebook.common.analysis.signal_summary import _compact_retrieval_evidence


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "workspace" / "probes" / "pattern_clustering_signal_audit"
DEFAULT_GT = ROOT / "scripts" / "probes" / "manual_pattern_ground_truth.json"
DEFAULT_OUTPUT = ROOT / "workspace" / "probes" / "retrieval_key_pair_coverage"

TARGET_PATTERNS = {
    "card_games": {
        "card_legalities_uuid_to_card_grain": (3, 6),
        "card_named_card_anchor_to_set_layer": (4, 10),
    },
    "codebase_community": {
        "code_editor_to_owner_user": (1, 1),
        "code_user_post_relation_via_posthistory": (18, 21),
    },
    "formula_1": {
        "f1_circuit_info_url": (3, 3),
        "f1_driver_standings_path": (18, 21),
    },
    "toxicology": {
        "tox_bond_pair_to_connected_atom_single_column": (28, 28),
        "tox_bond_condition_to_molecule_scope": (30, 45),
    },
}

ROOT_PREFIXES = (
    "gold_edge:",
    "target_role:",
    "target_eq:",
    "gold_only_table:",
    "predicate_role:",
)


def _norm_qid(value: Any) -> str:
    text = str(value or "").strip()
    return text[1:] if text.lower().startswith("q") else text


def _root_keys(db_id: str, evidence: Mapping[str, Any]) -> Set[Tuple[str, str]]:
    keys: Set[Tuple[str, str]] = set()
    for edge in evidence.get("gold_join_edges") or []:
        if str(edge or "").strip():
            keys.add((db_id, f"gold_edge:{str(edge).strip()}"))
    role = str(evidence.get("target_output_role") or "").strip()
    if role:
        keys.add((db_id, f"target_role:{role}"))
    for equality in evidence.get("target_relation_equalities") or []:
        if str(equality or "").strip():
            keys.add((db_id, f"target_eq:{str(equality).strip()}"))
    for table in evidence.get("gold_only_tables") or []:
        if str(table or "").strip():
            keys.add((db_id, f"gold_only_table:{str(table).strip()}"))
    for predicate_role in evidence.get("predicate_column_roles") or []:
        if str(predicate_role or "").strip():
            keys.add((db_id, f"predicate_role:{str(predicate_role).strip()}"))
    return keys


def _case_evidence(case_payload: Mapping[str, Any]) -> Dict[str, Any]:
    tools = case_payload.get("tools") if isinstance(case_payload.get("tools"), dict) else {}
    ir = tools.get("canonical_repair_ir") if isinstance(tools.get("canonical_repair_ir"), dict) else {}
    error = SimpleNamespace(canonical_repair_ir=ir, repair_skeleton={})
    return _compact_retrieval_evidence(error)


def _load_case_payloads(input_dir: Path, db_id: str) -> Dict[str, Mapping[str, Any]]:
    db_dir = input_dir / db_id
    payloads: Dict[str, Mapping[str, Any]] = {}
    for path in sorted(db_dir.glob("q*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payloads[_norm_qid(payload.get("qid") or path.stem)] = payload
    return payloads


def _load_patterns(gt_path: Path) -> Dict[str, Dict[str, List[str]]]:
    raw = json.loads(gt_path.read_text(encoding="utf-8"))
    out: Dict[str, Dict[str, List[str]]] = {}
    for db_id, patterns in (raw.get("databases") or {}).items():
        out[db_id] = {
            str(pattern.get("pattern_id")): [
                _norm_qid(qid) for qid in (pattern.get("case_ids") or [])
            ]
            for pattern in patterns
        }
    return out


def _covered_pairs(
    case_ids: Sequence[str],
    keys_by_case: Mapping[str, Set[Tuple[str, str]]],
) -> Tuple[int, int, List[Dict[str, Any]], Set[Tuple[str, str]]]:
    total = 0
    covered = 0
    pair_rows: List[Dict[str, Any]] = []
    shared_all: Set[Tuple[str, str]] = set(keys_by_case.get(case_ids[0], set())) if case_ids else set()
    for qid in case_ids[1:]:
        shared_all &= keys_by_case.get(qid, set())
    for left, right in itertools.combinations(case_ids, 2):
        total += 1
        shared = sorted(keys_by_case.get(left, set()) & keys_by_case.get(right, set()))
        if shared:
            covered += 1
        pair_rows.append(
            {
                "left": left,
                "right": right,
                "covered": bool(shared),
                "shared_keys": [f"{db}::{key}" for db, key in shared],
            }
        )
    return covered, total, pair_rows, shared_all


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--ground_truth", type=Path, default=DEFAULT_GT)
    parser.add_argument("--output_dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    manual = _load_patterns(args.ground_truth)
    results: List[Dict[str, Any]] = []
    all_pass = True
    for db_id, pattern_thresholds in TARGET_PATTERNS.items():
        payloads = _load_case_payloads(args.input_dir, db_id)
        evidence_by_case: Dict[str, Dict[str, Any]] = {}
        keys_by_case: Dict[str, Set[Tuple[str, str]]] = {}
        for qid, payload in payloads.items():
            evidence = _case_evidence(payload)
            evidence_by_case[qid] = evidence
            keys_by_case[qid] = _root_keys(db_id, evidence)
        for pattern_id, (min_covered, expected_total) in pattern_thresholds.items():
            case_ids = list(manual.get(db_id, {}).get(pattern_id, []))
            present = [qid for qid in case_ids if qid in keys_by_case]
            covered, total, pair_rows, shared_all = _covered_pairs(present, keys_by_case)
            passed = bool(total == expected_total and covered >= min_covered)
            all_pass = all_pass and passed
            results.append(
                {
                    "db_id": db_id,
                    "pattern_id": pattern_id,
                    "case_ids": case_ids,
                    "present_case_ids": present,
                    "covered_pairs": covered,
                    "total_pairs": total,
                    "target_min_covered": min_covered,
                    "target_total_pairs": expected_total,
                    "pass": passed,
                    "shared_all_keys": [f"{db}::{key}" for db, key in sorted(shared_all)],
                    "pairs": pair_rows,
                    "keys_by_case": {
                        qid: [f"{db}::{key}" for db, key in sorted(keys_by_case.get(qid, set()))]
                        for qid in present
                    },
                    "evidence_by_case": {qid: evidence_by_case.get(qid, {}) for qid in present},
                }
            )

    report = {
        "schema_version": "retrieval-key-pair-coverage-v0",
        "input_dir": str(args.input_dir),
        "all_pass": all_pass,
        "results": results,
    }
    (args.output_dir / "retrieval_key_pair_coverage.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    lines = [
        "# Retrieval Key Pair Coverage",
        "",
        f"Input: `{args.input_dir}`",
        "",
        "| db | pattern | covered / total | target | pass | shared by all cases |",
        "|---|---|---:|---:|---|---|",
    ]
    for row in results:
        lines.append(
            "| {db} | `{pattern}` | {covered}/{total} | ≥{target}/{target_total} | {passed} | {shared} |".format(
                db=row["db_id"],
                pattern=row["pattern_id"],
                covered=row["covered_pairs"],
                total=row["total_pairs"],
                target=row["target_min_covered"],
                target_total=row["target_total_pairs"],
                passed="PASS" if row["pass"] else "FAIL",
                shared=", ".join(f"`{key}`" for key in row["shared_all_keys"][:8]) or "-",
            )
        )
    (args.output_dir / "retrieval_key_pair_coverage.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"all_pass": all_pass, "results": [
        {
            "db_id": row["db_id"],
            "pattern_id": row["pattern_id"],
            "covered_pairs": row["covered_pairs"],
            "total_pairs": row["total_pairs"],
            "pass": row["pass"],
        }
        for row in results
    ]}, ensure_ascii=False, indent=2))
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
