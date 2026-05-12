#!/usr/bin/env python3
"""Compute r_v2_e Phase 2 acceptance metrics.

The script is intentionally read-only. It consumes a completed multi-DB run
directory plus a hand-transcribed manual pattern ground-truth JSON, then writes
machine-readable and human-readable metric reports.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


PASS_THRESHOLDS = {
    "m1_complete_pattern_count": 8,
    "m2_ready_rate": 50 / 1534,
    "m3_rewrite_landing_rate": 0.90,
    "m4_helped_count": 20,
    "m4_regressed_count": 5,
    "m5_singleton_utility_rate": 0.30,
    "m6_min_pattern_net_rate": -0.50,
    "m6_mean_pattern_net_rate": 0.20,
    "m7_helped_trace_rate": 1.0,
}


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _case_id(value: Any) -> str:
    return str(value or "").strip()


def _case_ids(values: Any) -> Set[str]:
    if not isinstance(values, list):
        return set()
    return {_case_id(v) for v in values if _case_id(v)}


def _runtime_status(row: Mapping[str, Any]) -> str:
    eea_runtime = row.get("eea_runtime") or {}
    if isinstance(eea_runtime, Mapping) and eea_runtime.get("status"):
        return str(eea_runtime.get("status"))
    trigger_trace = ((row.get("trigger") or {}).get("trigger_trace") or {})
    return str(trigger_trace.get("runtime_status") or "unknown")


def _matched_group_ids(row: Mapping[str, Any]) -> List[str]:
    eea_runtime = row.get("eea_runtime") or {}
    if isinstance(eea_runtime, Mapping) and isinstance(eea_runtime.get("matched_group_ids"), list):
        return [str(x) for x in eea_runtime.get("matched_group_ids") if str(x)]
    trigger_trace = ((row.get("trigger") or {}).get("trigger_trace") or {})
    if isinstance(trigger_trace.get("matched_group_ids"), list):
        return [str(x) for x in trigger_trace.get("matched_group_ids") if str(x)]
    audit_summary = (((trigger_trace.get("audit") or {}).get("summary") or {}) if isinstance(trigger_trace, Mapping) else {})
    if isinstance(audit_summary.get("matched_group_ids"), list):
        return [str(x) for x in audit_summary.get("matched_group_ids") if str(x)]
    return []


def _result_flags(row: Mapping[str, Any]) -> Tuple[Optional[bool], Optional[bool]]:
    result = row.get("result") or {}
    baseline = result.get("s0_correct")
    if baseline is None:
        baseline = result.get("baseline_correct")
    enhanced = result.get("enhanced_correct")
    if enhanced is None:
        enhanced = result.get("rewrite_only_correct")
    return baseline if isinstance(baseline, bool) else None, enhanced if isinstance(enhanced, bool) else None


def _rewrite_sql(row: Mapping[str, Any]) -> str:
    result = row.get("result") or {}
    for key in ("s1_sql", "rewrite_only_sql", "enhanced_sql"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    rewrite = row.get("rewrite") or {}
    candidates = rewrite.get("output_candidates") if isinstance(rewrite, Mapping) else None
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            for key in ("rewrite_sql", "sql", "candidate_sql"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _nested_get(obj: Any, path: Sequence[str]) -> Any:
    current = obj
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _has_complete_trace(row: Mapping[str, Any]) -> bool:
    matched = bool(_matched_group_ids(row))
    status = _runtime_status(row)
    hint = bool(((row.get("eea_runtime") or {}).get("hint") or "").strip()) if isinstance(row.get("eea_runtime"), Mapping) else False
    audit = _nested_get(row, ["eea_runtime", "audit", "summary"]) or _nested_get(row, ["trigger", "trigger_trace", "audit", "summary"])
    actions = _nested_get(audit, ["compiler", "actions"]) if isinstance(audit, Mapping) else None
    if actions is None:
        actions = _nested_get(row, ["eea_runtime", "audit", "summary", "compiler", "actions"])
    rewrite_sql = bool(_rewrite_sql(row))
    return status == "ready" and matched and hint and isinstance(actions, list) and rewrite_sql


def _run_db_dirs(run_dir: Path) -> List[Path]:
    dirs = []
    for child in sorted(run_dir.iterdir()):
        if child.is_dir() and (child / "summary.json").exists():
            dirs.append(child)
    return dirs


def _library_groups(library: Mapping[str, Any], key: str) -> List[Mapping[str, Any]]:
    value = library.get(key)
    return value if isinstance(value, list) else []


def _compute_m1(db_dir: Path, manual_patterns: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    details: List[Dict[str, Any]] = []
    complete = 0
    total = 0
    by_db: Dict[str, Dict[str, Any]] = {}

    for db_id, patterns in sorted((manual_patterns.get("databases") or {}).items()):
        library = _load_json(db_dir / db_id / "final_library.json", default={})
        actual_patterns = _library_groups(library or {}, "patterns")
        db_total = 0
        db_complete = 0
        for manual in patterns:
            total += 1
            db_total += 1
            expected = _case_ids(manual.get("case_ids"))
            best = {
                "group_id": None,
                "recall": 0.0,
                "intersection": [],
                "actual_case_ids": [],
            }
            for actual in actual_patterns:
                actual_cases = _case_ids(actual.get("case_ids"))
                if not expected:
                    continue
                intersection = expected & actual_cases
                recall = len(intersection) / len(expected)
                if recall > best["recall"]:
                    best = {
                        "group_id": actual.get("group_id"),
                        "recall": recall,
                        "intersection": sorted(intersection, key=lambda x: int(x) if x.isdigit() else x),
                        "actual_case_ids": sorted(actual_cases, key=lambda x: int(x) if x.isdigit() else x),
                    }
            is_complete = best["recall"] >= 0.8
            complete += int(is_complete)
            db_complete += int(is_complete)
            details.append(
                {
                    "db_id": db_id,
                    "manual_pattern_id": manual.get("pattern_id"),
                    "label": manual.get("label", ""),
                    "expected_case_ids": sorted(expected, key=lambda x: int(x) if x.isdigit() else x),
                    "best_actual_group_id": best["group_id"],
                    "best_recall": best["recall"],
                    "intersection": best["intersection"],
                    "actual_case_ids": best["actual_case_ids"],
                    "complete_hit": is_complete,
                }
            )
        by_db[db_id] = {"complete": db_complete, "total": db_total, "rate": db_complete / db_total if db_total else 0.0}

    metric = {
        "name": "M1_pattern_recall",
        "complete_pattern_count": complete,
        "manual_pattern_count": total,
        "complete_pattern_rate": complete / total if total else 0.0,
        "complete_threshold": PASS_THRESHOLDS["m1_complete_pattern_count"],
        "passed": complete >= PASS_THRESHOLDS["m1_complete_pattern_count"],
        "by_db": by_db,
    }
    return metric, details


def compute_metrics(run_dir: Path, manual_patterns_path: Path) -> Dict[str, Any]:
    manual_patterns = _load_json(manual_patterns_path, default={})
    db_dirs = _run_db_dirs(run_dir)
    all_rows: List[Dict[str, Any]] = []
    summaries: Dict[str, Any] = {}
    libraries: Dict[str, Any] = {}

    for db_path in db_dirs:
        db_id = db_path.name
        summaries[db_id] = _load_json(db_path / "summary.json", default={}) or {}
        libraries[db_id] = _load_json(db_path / "final_library.json", default={}) or {}
        for row in _iter_jsonl(db_path / "per_case_log.jsonl"):
            all_rows.append(row)

    total_cases = len(all_rows)
    ready_rows = [row for row in all_rows if _runtime_status(row) == "ready"]
    ready_count = len(ready_rows)
    ready_rate = ready_count / total_cases if total_cases else 0.0
    rewrite_landed = [row for row in ready_rows if _rewrite_sql(row)]
    rewrite_landing_rate = len(rewrite_landed) / ready_count if ready_count else 0.0

    helped_rows: List[Dict[str, Any]] = []
    regressed_rows: List[Dict[str, Any]] = []
    for row in all_rows:
        baseline, enhanced = _result_flags(row)
        if baseline is False and enhanced is True:
            helped_rows.append(row)
        if baseline is True and enhanced is False:
            regressed_rows.append(row)

    m1, m1_details = _compute_m1(run_dir, manual_patterns)

    total_singletons = 0
    singleton_triggered: Dict[str, Set[str]] = defaultdict(set)
    singleton_cases: Dict[str, Set[str]] = {}
    pattern_cases: Dict[str, Set[str]] = {}
    for db_id, library in libraries.items():
        for singleton in _library_groups(library, "singletons"):
            gid = str(singleton.get("group_id") or "")
            if not gid:
                continue
            total_singletons += 1
            singleton_cases[gid] = _case_ids(singleton.get("case_ids"))
        for pattern in _library_groups(library, "patterns"):
            gid = str(pattern.get("group_id") or "")
            if gid:
                pattern_cases[gid] = _case_ids(pattern.get("case_ids"))

    pattern_trigger_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"trigger_count": 0, "helped_count": 0, "regressed_count": 0})
    for row in all_rows:
        qid = _case_id(row.get("question_id"))
        baseline, enhanced = _result_flags(row)
        for gid in _matched_group_ids(row):
            if gid in singleton_cases and qid not in singleton_cases[gid]:
                singleton_triggered[gid].add(qid)
            if gid in pattern_cases:
                stats = pattern_trigger_stats[gid]
                stats["trigger_count"] += 1
                if baseline is False and enhanced is True:
                    stats["helped_count"] += 1
                if baseline is True and enhanced is False:
                    stats["regressed_count"] += 1

    singleton_utility_count = len(singleton_triggered)
    singleton_utility_rate = singleton_utility_count / total_singletons if total_singletons else 0.0

    pattern_net_rates = []
    for gid, stats in pattern_trigger_stats.items():
        trigger_count = stats["trigger_count"]
        if trigger_count:
            net_rate = (stats["helped_count"] - stats["regressed_count"]) / trigger_count
            stats["net_rate"] = net_rate
            pattern_net_rates.append(net_rate)
    min_net_rate = min(pattern_net_rates) if pattern_net_rates else None
    mean_net_rate = sum(pattern_net_rates) / len(pattern_net_rates) if pattern_net_rates else None

    helped_trace_complete = sum(1 for row in helped_rows if _has_complete_trace(row))
    helped_trace_rate = helped_trace_complete / len(helped_rows) if helped_rows else 1.0

    status_counts = Counter(_runtime_status(row) for row in all_rows)
    db_summaries = {}
    for db_id, summary in summaries.items():
        db_rows = [row for row in all_rows if row.get("db_id") == db_id]
        db_summaries[db_id] = {
            "total_cases": len(db_rows),
            "baseline_correct": summary.get("baseline_correct"),
            "enhanced_correct": summary.get("enhanced_correct"),
            "delta": (summary.get("enhanced_correct") or 0) - (summary.get("baseline_correct") or 0),
            "ready_count": sum(1 for row in db_rows if _runtime_status(row) == "ready"),
            "helped_qids": [str(x) for x in summary.get("improved_qids") or []],
            "regressed_qids": [str(x) for x in summary.get("regressed_qids") or []],
        }

    metrics = {
        "schema_version": "r-v2-e-phase2-metrics-v1",
        "run_dir": str(run_dir),
        "manual_patterns": str(manual_patterns_path),
        "db_count": len(db_dirs),
        "total_cases": total_cases,
        "status_counts": dict(status_counts),
        "db_summaries": db_summaries,
        "metrics": {
            "M1": m1,
            "M2": {
                "name": "M2_trigger_ready_rate",
                "ready_count": ready_count,
                "total_cases": total_cases,
                "ready_rate": ready_rate,
                "ready_count_threshold": 50,
                "passed": ready_count >= 50,
            },
            "M3": {
                "name": "M3_rewrite_landing_rate",
                "rewrite_landed_count": len(rewrite_landed),
                "ready_count": ready_count,
                "rewrite_landing_rate": rewrite_landing_rate,
                "threshold": PASS_THRESHOLDS["m3_rewrite_landing_rate"],
                "passed": rewrite_landing_rate >= PASS_THRESHOLDS["m3_rewrite_landing_rate"] if ready_count else False,
            },
            "M4": {
                "name": "M4_helped_regressed",
                "helped_count": len(helped_rows),
                "regressed_count": len(regressed_rows),
                "helped_threshold": PASS_THRESHOLDS["m4_helped_count"],
                "regressed_max": PASS_THRESHOLDS["m4_regressed_count"],
                "passed": len(helped_rows) >= PASS_THRESHOLDS["m4_helped_count"] and len(regressed_rows) <= PASS_THRESHOLDS["m4_regressed_count"],
                "helped_qids": [str(row.get("question_id")) for row in helped_rows],
                "regressed_qids": [str(row.get("question_id")) for row in regressed_rows],
            },
            "M5": {
                "name": "M5_singleton_utility_rate",
                "singleton_triggered_count": singleton_utility_count,
                "total_singletons": total_singletons,
                "singleton_utility_rate": singleton_utility_rate,
                "threshold": PASS_THRESHOLDS["m5_singleton_utility_rate"],
                "passed": singleton_utility_rate >= PASS_THRESHOLDS["m5_singleton_utility_rate"] if total_singletons else False,
                "triggered_singletons": {gid: sorted(qids, key=lambda x: int(x) if x.isdigit() else x) for gid, qids in sorted(singleton_triggered.items())},
            },
            "M6": {
                "name": "M6_pattern_net_benefit_distribution",
                "triggered_pattern_count": len(pattern_trigger_stats),
                "min_pattern_net_rate": min_net_rate,
                "mean_pattern_net_rate": mean_net_rate,
                "min_net_rate_threshold": PASS_THRESHOLDS["m6_min_pattern_net_rate"],
                "mean_net_rate_threshold": PASS_THRESHOLDS["m6_mean_pattern_net_rate"],
                "passed": (
                    min_net_rate is not None
                    and mean_net_rate is not None
                    and min_net_rate >= PASS_THRESHOLDS["m6_min_pattern_net_rate"]
                    and mean_net_rate >= PASS_THRESHOLDS["m6_mean_pattern_net_rate"]
                ),
                "pattern_stats": dict(sorted(pattern_trigger_stats.items())),
            },
            "M7": {
                "name": "M7_helped_trace_completeness",
                "helped_trace_complete": helped_trace_complete,
                "helped_count": len(helped_rows),
                "helped_trace_rate": helped_trace_rate,
                "threshold": PASS_THRESHOLDS["m7_helped_trace_rate"],
                "passed": helped_trace_rate >= PASS_THRESHOLDS["m7_helped_trace_rate"],
            },
        },
        "m1_details": m1_details,
    }
    return metrics


def _pass_text(value: bool) -> str:
    return "PASS" if value else "FAIL"


def write_markdown(metrics: Mapping[str, Any], path: Path) -> None:
    lines: List[str] = []
    lines.append("# r_v2_e Phase 2 Metrics")
    lines.append("")
    lines.append(f"- run_dir: `{metrics.get('run_dir')}`")
    lines.append(f"- total_cases: {metrics.get('total_cases')}")
    lines.append(f"- status_counts: `{metrics.get('status_counts')}`")
    lines.append("")
    lines.append("## M1-M7 Summary")
    lines.append("")
    lines.append("| Metric | Value | Threshold | Status |")
    lines.append("|---|---:|---:|---|")
    m = metrics["metrics"]
    lines.append(
        f"| M1 pattern recall | {m['M1']['complete_pattern_count']}/{m['M1']['manual_pattern_count']} ({m['M1']['complete_pattern_rate']:.3f}) | >= {m['M1']['complete_threshold']} complete | {_pass_text(m['M1']['passed'])} |"
    )
    lines.append(
        f"| M2 trigger ready | {m['M2']['ready_count']}/{m['M2']['total_cases']} ({m['M2']['ready_rate']:.3f}) | >= {m['M2']['ready_count_threshold']} ready | {_pass_text(m['M2']['passed'])} |"
    )
    lines.append(
        f"| M3 rewrite landing | {m['M3']['rewrite_landed_count']}/{m['M3']['ready_count']} ({m['M3']['rewrite_landing_rate']:.3f}) | >= {m['M3']['threshold']:.2f} | {_pass_text(m['M3']['passed'])} |"
    )
    lines.append(
        f"| M4 helped/regressed | +{m['M4']['helped_count']} / -{m['M4']['regressed_count']} | helped >= {m['M4']['helped_threshold']}, regressed <= {m['M4']['regressed_max']} | {_pass_text(m['M4']['passed'])} |"
    )
    lines.append(
        f"| M5 singleton utility | {m['M5']['singleton_triggered_count']}/{m['M5']['total_singletons']} ({m['M5']['singleton_utility_rate']:.3f}) | >= {m['M5']['threshold']:.2f} | {_pass_text(m['M5']['passed'])} |"
    )
    min_net = m["M6"]["min_pattern_net_rate"]
    mean_net = m["M6"]["mean_pattern_net_rate"]
    min_text = "n/a" if min_net is None else f"{min_net:.3f}"
    mean_text = "n/a" if mean_net is None else f"{mean_net:.3f}"
    lines.append(
        f"| M6 pattern net | min {min_text}, mean {mean_text} | min >= {m['M6']['min_net_rate_threshold']:.2f}, mean >= {m['M6']['mean_net_rate_threshold']:.2f} | {_pass_text(m['M6']['passed'])} |"
    )
    lines.append(
        f"| M7 helped trace | {m['M7']['helped_trace_complete']}/{m['M7']['helped_count']} ({m['M7']['helped_trace_rate']:.3f}) | {m['M7']['threshold']:.2f} | {_pass_text(m['M7']['passed'])} |"
    )
    lines.append("")
    lines.append("## DB Summary")
    lines.append("")
    lines.append("| DB | Total | Baseline | Enhanced | Delta | Ready | Helped | Regressed |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for db_id, s in sorted(metrics.get("db_summaries", {}).items()):
        lines.append(
            f"| {db_id} | {s['total_cases']} | {s.get('baseline_correct')} | {s.get('enhanced_correct')} | {s.get('delta')} | {s.get('ready_count')} | {len(s.get('helped_qids') or [])} | {len(s.get('regressed_qids') or [])} |"
        )
    lines.append("")
    lines.append("## M1 Details")
    lines.append("")
    lines.append("| DB | Manual Pattern | Recall | Best Actual Group | Complete |")
    lines.append("|---|---|---:|---|---|")
    for row in metrics.get("m1_details", []):
        lines.append(
            f"| {row['db_id']} | {row['manual_pattern_id']} | {row['best_recall']:.3f} | {row.get('best_actual_group_id') or ''} | {_pass_text(bool(row.get('complete_hit')))} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run_dir", required=True, type=Path)
    parser.add_argument("--manual_patterns", required=True, type=Path)
    parser.add_argument("--output_dir", type=Path, default=None)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    output_dir = (args.output_dir or run_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    metrics = compute_metrics(run_dir, args.manual_patterns.resolve())
    json_path = output_dir / "r_v2_e_metrics.json"
    md_path = output_dir / "r_v2_e_metrics.md"
    json_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_markdown(metrics, md_path)
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
