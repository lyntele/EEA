#!/usr/bin/env python3
"""Coverage report for retrieval-root-evidence v6 admission candidates."""
from __future__ import annotations

import argparse
import json
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

DEFAULT_RUN_ROOT = Path("/data/liuyining/ace4sql/method/EEA/rulebook/outputs/retrieval_root_evidence_v6")
DEFAULT_MANUAL = Path("/data/liuyining/ace4sql/method/EEA/rulebook/scripts/probes/manual_pattern_ground_truth.json")
DEFAULT_DOC = Path("/data/liuyining/ace4sql/method/EEA/rulebook/doc/manual_pattern_retrieval_coverage_v6.md")
DEFAULT_JSON = Path("/data/liuyining/ace4sql/method/EEA/rulebook/doc/manual_pattern_retrieval_coverage_v6.json")


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")


def _case_set(values: Iterable[Any]) -> Set[str]:
    return {str(value) for value in values if str(value)}


def _trace_candidates(trace_path: Path) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    if not trace_path.exists():
        return candidates
    for index, line in enumerate(trace_path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("stage") != "pattern_admission_judge":
            continue
        context = row.get("context") or {}
        case_ids = sorted(_case_set(context.get("case_ids") or []), key=lambda x: int(x) if x.isdigit() else x)
        if not case_ids:
            continue
        candidates.append(
            {
                "trace_index": index,
                "case_ids": case_ids,
                "case_id_set": set(case_ids),
                "status": row.get("status"),
                "prompt_chars": context.get("prompt_chars"),
                "group_count": context.get("group_count"),
                "pair_count": context.get("pair_count"),
                "sampled_case_ids": list(context.get("sampled_case_ids") or []),
            }
        )
    return candidates


def _manual_patterns(manual: Dict[str, Any], db_id: str, run_case_ids: Set[str]) -> List[Dict[str, Any]]:
    rows = []
    for pattern in ((manual.get("databases") or {}).get(db_id) or []):
        cases = sorted(_case_set(pattern.get("case_ids") or []), key=lambda x: int(x) if x.isdigit() else x)
        if len(set(cases) & run_case_ids) < 2:
            continue
        rows.append({**pattern, "case_ids": cases, "case_id_set": set(cases)})
    return rows


def _pair_covered(pattern_cases: Set[str], candidates: List[Dict[str, Any]]) -> Set[tuple[str, str]]:
    covered: Set[tuple[str, str]] = set()
    for cand in candidates:
        overlap = sorted(pattern_cases & cand["case_id_set"], key=lambda x: int(x) if x.isdigit() else x)
        for left, right in combinations(overlap, 2):
            covered.add((left, right))
    return covered


def _analyze_db(db_id: str, run_root: Path, manual: Dict[str, Any]) -> Dict[str, Any]:
    db_dir = run_root / db_id
    trace_path = db_dir / "eea_llm_trace.jsonl"
    summary_path = db_dir / "summary.json"
    candidates = _trace_candidates(trace_path)
    run_case_ids: Set[str] = set()
    if summary_path.exists():
        payload = _load_json(summary_path)
        for row in payload.get("cases") or []:
            if row.get("question_id") is not None:
                run_case_ids.add(str(row.get("question_id")))
    if not run_case_ids:
        for cand in candidates:
            run_case_ids.update(cand["case_id_set"])
    patterns = _manual_patterns(manual, db_id, run_case_ids)
    pattern_by_case: Dict[str, Set[str]] = {}
    for pattern in patterns:
        for case_id in pattern["case_ids"]:
            pattern_by_case.setdefault(case_id, set()).add(pattern["pattern_id"])

    complete_candidates = []
    mixed_candidates = []
    for cand in candidates:
        contained = []
        touched_patterns = set()
        for case_id in cand["case_ids"]:
            touched_patterns.update(pattern_by_case.get(case_id, set()))
        for pattern in patterns:
            if pattern["case_id_set"] <= cand["case_id_set"]:
                contained.append(pattern["pattern_id"])
        if contained:
            complete_candidates.append({**{k: v for k, v in cand.items() if k != "case_id_set"}, "complete_patterns": contained})
        if len(touched_patterns) > 1:
            mixed_candidates.append({**{k: v for k, v in cand.items() if k != "case_id_set"}, "touched_patterns": sorted(touched_patterns)})

    per_pattern = []
    for pattern in patterns:
        cases = pattern["case_id_set"]
        total_pairs = len(list(combinations(cases, 2)))
        covered_pairs = _pair_covered(cases, candidates)
        best_candidate = None
        best_overlap: Set[str] = set()
        fragments = []
        for cand in candidates:
            overlap = cases & cand["case_id_set"]
            if len(overlap) > len(best_overlap):
                best_overlap = overlap
                best_candidate = cand
            if len(overlap) >= 2:
                fragments.append(sorted(overlap, key=lambda x: int(x) if x.isdigit() else x))
        full = any(cases <= cand["case_id_set"] for cand in candidates)
        no_pair = len(covered_pairs) == 0 and total_pairs > 0
        per_pattern.append(
            {
                "pattern_id": pattern["pattern_id"],
                "label": pattern.get("label", ""),
                "case_ids": pattern["case_ids"],
                "full": full,
                "max_co_cases": len(best_overlap),
                "max_co_case_ids": sorted(best_overlap, key=lambda x: int(x) if x.isdigit() else x),
                "best_candidate_case_ids": list(best_candidate.get("case_ids") or []) if best_candidate else [],
                "fragments": fragments,
                "fragment_count": len(fragments),
                "pair_covered": len(covered_pairs),
                "pair_total": total_pairs,
                "no_pair_co_candidate": no_pair,
            }
        )

    return {
        "db_id": db_id,
        "trace_path": str(trace_path),
        "summary_path": str(summary_path),
        "run_case_count": len(run_case_ids),
        "candidate_count": len(candidates),
        "complete_candidate_count": len(complete_candidates),
        "mixed_candidate_count": len(mixed_candidates),
        "patterns_total": len(patterns),
        "patterns_fully_co_candidate": sum(1 for p in per_pattern if p["full"]),
        "no_full_co_candidate": sum(1 for p in per_pattern if not p["full"]),
        "no_pair_co_candidate": sum(1 for p in per_pattern if p["no_pair_co_candidate"]),
        "candidates": [{k: v for k, v in cand.items() if k != "case_id_set"} for cand in candidates],
        "complete_candidates": complete_candidates,
        "mixed_candidates": mixed_candidates,
        "per_pattern": per_pattern,
    }


def _render_markdown(payload: Dict[str, Any]) -> str:
    lines = ["# Manual Pattern Retrieval Coverage v6", ""]
    totals = payload["totals"]
    gates = payload.get("phase1_gates") or []
    lines += [
        "## Summary",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| databases | {totals['databases']} |",
        f"| candidates | {totals['candidate_count']} |",
        f"| complete candidates | {totals['complete_candidate_count']} |",
        f"| mixed candidates | {totals['mixed_candidate_count']} |",
        f"| manual patterns | {totals['patterns_total']} |",
        f"| patterns fully co-candidate | {totals['patterns_fully_co_candidate']} |",
        f"| no full co-candidate | {totals['no_full_co_candidate']} |",
        f"| no pair co-candidate | {totals['no_pair_co_candidate']} |",
        "",
    ]
    if gates:
        lines += [
            "## Phase 1 Gate",
            "",
            "| check | expected | actual | status |",
            "|---|---|---:|---|",
        ]
        for gate in gates:
            lines.append(
                f"| {gate['check']} | {gate['expected']} | {gate['actual']} | {gate['status']} |"
            )
        lines.append("")
    for db in payload["databases"]:
        lines += [
            f"## {db['db_id']}",
            "",
            "| metric | value |",
            "|---|---:|",
            f"| candidates | {db['candidate_count']} |",
            f"| complete candidates | {db['complete_candidate_count']} |",
            f"| mixed candidates | {db['mixed_candidate_count']} |",
            f"| patterns fully co-candidate | {db['patterns_fully_co_candidate']} / {db['patterns_total']} |",
            f"| no full co-candidate | {db['no_full_co_candidate']} |",
            f"| no pair co-candidate | {db['no_pair_co_candidate']} |",
            "",
            "| pattern | full | max co-cases | pair coverage | fragments |",
            "|---|---|---:|---:|---:|",
        ]
        for row in db["per_pattern"]:
            lines.append(
                f"| `{row['pattern_id']}` | {'yes' if row['full'] else 'no'} | "
                f"{row['max_co_cases']}/{len(row['case_ids'])} | "
                f"{row['pair_covered']}/{row['pair_total']} | {row['fragment_count']} |"
            )
        if db["complete_candidates"]:
            lines += ["", "### Complete Candidates", ""]
            for cand in db["complete_candidates"]:
                lines.append(f"- cases={cand['case_ids']} patterns={cand['complete_patterns']}")
        if db["mixed_candidates"]:
            lines += ["", "### Mixed Candidates", ""]
            for cand in db["mixed_candidates"]:
                lines.append(f"- cases={cand['case_ids']} touched={cand['touched_patterns']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _find_pattern(databases: List[Dict[str, Any]], pattern_id: str) -> Dict[str, Any] | None:
    for db in databases:
        for pattern in db.get("per_pattern") or []:
            if pattern.get("pattern_id") == pattern_id:
                return pattern
    return None


def _phase1_gates(databases: List[Dict[str, Any]], totals: Dict[str, Any]) -> List[Dict[str, Any]]:
    bond_pair = _find_pattern(databases, "tox_bond_pair_to_connected_atom_single_column")
    posthistory = _find_pattern(databases, "code_user_post_relation_via_posthistory")
    gates = [
        {
            "check": "complete candidates",
            "expected": ">= 5",
            "actual": totals["complete_candidate_count"],
            "status": "PASS" if totals["complete_candidate_count"] >= 5 else "FAIL",
        },
        {
            "check": "mixed candidates",
            "expected": "<= 1",
            "actual": totals["mixed_candidate_count"],
            "status": "PASS" if totals["mixed_candidate_count"] <= 1 else "FAIL",
        },
        {
            "check": "no pair co-candidate",
            "expected": "<= 1",
            "actual": totals["no_pair_co_candidate"],
            "status": "PASS" if totals["no_pair_co_candidate"] <= 1 else "FAIL",
        },
        {
            "check": "toxicology bond_pair complete",
            "expected": "complete",
            "actual": "yes" if bond_pair and bond_pair.get("full") else (f"no ({bond_pair.get('max_co_cases')}/{len(bond_pair.get('case_ids') or [])})" if bond_pair else "missing"),
            "status": "PASS" if bond_pair and bond_pair.get("full") else "FAIL",
        },
        {
            "check": "codebase posthistory complete",
            "expected": "complete",
            "actual": "yes" if posthistory and posthistory.get("full") else (f"no ({posthistory.get('max_co_cases')}/{len(posthistory.get('case_ids') or [])})" if posthistory else "missing"),
            "status": "PASS" if posthistory and posthistory.get("full") else "FAIL",
        },
        {
            "check": "admission_judge calls",
            "expected": ">= 5",
            "actual": totals["candidate_count"],
            "status": "PASS" if totals["candidate_count"] >= 5 else "FAIL",
        },
    ]
    return gates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run_root", default=str(DEFAULT_RUN_ROOT))
    parser.add_argument("--manual_patterns", default=str(DEFAULT_MANUAL))
    parser.add_argument("--db_ids", default="toxicology,codebase_community")
    parser.add_argument("--output_md", default=str(DEFAULT_DOC))
    parser.add_argument("--output_json", default=str(DEFAULT_JSON))
    args = parser.parse_args()

    run_root = Path(args.run_root).resolve()
    manual = _load_json(Path(args.manual_patterns).resolve())
    databases = [
        _analyze_db(db_id.strip(), run_root, manual)
        for db_id in args.db_ids.split(",")
        if db_id.strip()
    ]
    totals = {
        "databases": len(databases),
        "candidate_count": sum(db["candidate_count"] for db in databases),
        "complete_candidate_count": sum(db["complete_candidate_count"] for db in databases),
        "mixed_candidate_count": sum(db["mixed_candidate_count"] for db in databases),
        "patterns_total": sum(db["patterns_total"] for db in databases),
        "patterns_fully_co_candidate": sum(db["patterns_fully_co_candidate"] for db in databases),
        "no_full_co_candidate": sum(db["no_full_co_candidate"] for db in databases),
        "no_pair_co_candidate": sum(db["no_pair_co_candidate"] for db in databases),
    }
    gates = _phase1_gates(databases, totals)
    payload = {
        "schema_version": "manual-pattern-retrieval-coverage-v6",
        "run_root": str(run_root),
        "manual_patterns": str(Path(args.manual_patterns).resolve()),
        "totals": totals,
        "phase1_gates": gates,
        "databases": databases,
    }
    output_json = Path(args.output_json).resolve()
    output_md = Path(args.output_md).resolve()
    _dump_json(output_json, payload)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text(_render_markdown(payload), encoding="utf-8")
    print(json.dumps(totals, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
