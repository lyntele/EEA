#!/usr/bin/env python3
"""Build human-readable audit tables for an EEA v2 single-db run.

The audit joins per-case logs, rewrite provenance, final libraries, offline
family reports, and optional manual labels. It is intentionally offline-only
and never calls an LLM.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None


def _short(text: Any, limit: int = 220) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _extract_source_case_id(group_id: str) -> Optional[int]:
    match = re.search(r"grp-sing-[^-]+-(\d+)$", group_id or "")
    if not match:
        return None
    return int(match.group(1))


def build_manual_label_map(payload: Any, db_id: str) -> Dict[int, Dict[str, Any]]:
    """Return qid -> manual label metadata for one db.

    Labels intentionally use compact stable ids (P0/F0/S123) so trigger sources
    can be compared mechanically.
    """
    label_map: Dict[int, Dict[str, Any]] = {}
    db_payload = None
    for db in payload or []:
        if db.get("db_id") == db_id:
            db_payload = db
            break
    if not db_payload:
        return label_map

    for idx, group in enumerate(db_payload.get("patterns") or []):
        label = f"P{idx}"
        for case_id in group.get("case_ids") or []:
            qid = _as_int(case_id)
            if qid is not None:
                label_map[qid] = {
                    "label": label,
                    "kind": "pattern",
                    "name": group.get("pattern_name") or "",
                }

    for idx, group in enumerate(db_payload.get("experience_families") or []):
        label = f"F{idx}"
        for case_id in group.get("case_ids") or []:
            qid = _as_int(case_id)
            if qid is not None:
                label_map[qid] = {
                    "label": label,
                    "kind": "family",
                    "name": group.get("family_name") or "",
                }

    for case_id in db_payload.get("singletons") or []:
        qid = _as_int(case_id)
        if qid is not None and qid not in label_map:
            label_map[qid] = {
                "label": f"S{qid}",
                "kind": "singleton",
                "name": f"singleton:{qid}",
            }
    return label_map


def _library_groups(library: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for bucket in ("patterns", "experience_families", "singletons"):
        for group in library.get(bucket) or []:
            groups[group.get("group_id", "")] = group
    return groups


def _group_source_case_ids(group_id: str, group: Optional[Dict[str, Any]]) -> List[int]:
    if group:
        ids = [_as_int(case_id) for case_id in group.get("case_ids") or []]
        return [qid for qid in ids if qid is not None]
    source = _extract_source_case_id(group_id)
    return [source] if source is not None else []


def _source_labels(
    group_ids: Sequence[str],
    groups: Dict[str, Dict[str, Any]],
    manual_labels: Dict[int, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for group_id in group_ids:
        source_ids = _group_source_case_ids(group_id, groups.get(group_id))
        labels = []
        for qid in source_ids:
            meta = manual_labels.get(qid) or {
                "label": f"S{qid}",
                "kind": "singleton",
                "name": f"singleton:{qid}",
            }
            labels.append(
                {
                    "qid": qid,
                    "label": meta["label"],
                    "kind": meta["kind"],
                    "name": meta["name"],
                }
            )
        out.append({"group_id": group_id, "source_cases": labels})
    return out


def _plan_audit_for(run_dir: Path, qid: int) -> Dict[str, Any]:
    path = run_dir / ".state" / "work" / f"qid_{qid}" / "rewrite_output.provenance.json"
    payload = _load_json(path, {})
    return ((payload.get("cases") or {}).get(str(qid)) or {}).get("plan_audit") or {}


def _candidate_provenance_for(run_dir: Path, qid: int) -> List[Dict[str, Any]]:
    path = run_dir / ".state" / "work" / f"qid_{qid}" / "rewrite_output.provenance.json"
    payload = _load_json(path, {})
    return ((payload.get("cases") or {}).get(str(qid)) or {}).get("candidate_provenance") or []


def build_case_audit(
    *,
    run_dir: Path,
    manual_labels: Dict[int, Dict[str, Any]],
    groups: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows = _read_jsonl(run_dir / "per_case_log.jsonl")
    audit_rows: List[Dict[str, Any]] = []
    counters: Counter[str] = Counter()

    for row in rows:
        qid = int(row["question_id"])
        result = row.get("result") or {}
        trigger = row.get("trigger") or {}
        trace = trigger.get("trigger_trace") or {}
        rewrite = row.get("rewrite") or {}
        selection = row.get("selection") or {}
        manual = manual_labels.get(qid) or {
            "label": f"S{qid}",
            "kind": "unlabeled",
            "name": "",
        }

        matched_group_ids = list(trace.get("v2_matched_group_ids") or [])
        source_labels = _source_labels(matched_group_ids, groups, manual_labels)
        flat_source_labels = [
            source
            for group in source_labels
            for source in group.get("source_cases", [])
        ]
        same_label_sources = [
            source for source in flat_source_labels if source.get("label") == manual.get("label")
        ]
        diff_label_sources = [
            source for source in flat_source_labels if source.get("label") != manual.get("label")
        ]

        baseline_correct = bool(result.get("baseline_correct"))
        rewrite_only_correct = bool(result.get("rewrite_only_correct"))
        enhanced_correct = bool(result.get("enhanced_correct"))
        if not baseline_correct and enhanced_correct:
            outcome = "improved"
        elif baseline_correct and not enhanced_correct:
            outcome = "regressed"
        else:
            outcome = "neutral"

        plan_audit = _plan_audit_for(run_dir, qid)
        provenance = _candidate_provenance_for(run_dir, qid)
        rewritten_with_inapplicable_hint = [
            item
            for item in provenance
            if item.get("source") == "memory_rewrite"
            and item.get("hint_applicable") is False
        ]

        input_candidates = int(rewrite.get("input_candidates") or 0)
        output_candidates = int(rewrite.get("output_candidates") or 0)
        rewrote = output_candidates > input_candidates
        plan_reason = str(plan_audit.get("plan_reason") or "")
        hint_applicable = bool(plan_audit.get("hint_applicable", False))
        rewrite_allowed = bool(plan_audit.get("rewrite_allowed", plan_reason == "ready"))

        issue_flags: List[str] = []
        if matched_group_ids and not same_label_sources and manual.get("kind") in {"pattern", "family"}:
            issue_flags.append("trigger_no_same_manual_label")
        if len(matched_group_ids) >= 5:
            issue_flags.append("trigger_top5_saturated")
        if rewrote and plan_reason == "ready" and not hint_applicable:
            issue_flags.append("rewrite_despite_inapplicable_hint")
        if baseline_correct and rewrote:
            issue_flags.append("baseline_correct_touched")
        if outcome == "regressed":
            issue_flags.append("memory_regression")
        if outcome == "improved" and plan_reason == "ready" and not hint_applicable:
            issue_flags.append("improved_without_applicable_hint")

        for flag in issue_flags:
            counters[flag] += 1
        counters[f"outcome:{outcome}"] += 1
        counters[f"manual_kind:{manual.get('kind')}"] += 1

        audit_rows.append(
            {
                "qid": qid,
                "step_index": row.get("step_index"),
                "question": row.get("question"),
                "manual_label": manual.get("label"),
                "manual_kind": manual.get("kind"),
                "manual_name": manual.get("name"),
                "generation_top1_correct": bool(result.get("generation_top1_correct")),
                "baseline_correct": baseline_correct,
                "rewrite_only_correct": rewrite_only_correct,
                "enhanced_correct": enhanced_correct,
                "outcome": outcome,
                "n_matched": len(matched_group_ids),
                "matched_group_ids": matched_group_ids,
                "trigger_source_labels": source_labels,
                "same_manual_label_sources": same_label_sources,
                "diff_manual_label_sources": diff_label_sources,
                "plan_reason": plan_reason,
                "hint_applicable": hint_applicable,
                "rewrite_allowed": rewrite_allowed,
                "raw_hint": plan_audit.get("raw_hint") or "",
                "instantiated_hint": plan_audit.get("instantiated_hint") or "",
                "hint_instantiation_notes": plan_audit.get("hint_instantiation_notes") or "",
                "input_candidates": input_candidates,
                "output_candidates": output_candidates,
                "rewrote": rewrote,
                "rewrite_failures": int(rewrite.get("rewrite_failures") or 0),
                "memory_rewrite_candidates_with_inapplicable_hint": len(
                    rewritten_with_inapplicable_hint
                ),
                "selected_cluster_origin": selection.get("selected_cluster_origin"),
                "selected_pattern_ids": selection.get("selected_pattern_ids")
                or ([selection.get("selected_pattern_id")] if selection.get("selected_pattern_id") else []),
                "rewrite_only_selected_origin": selection.get("rewrite_only_selected_origin"),
                "rewrite_only_selected_pattern_ids": selection.get(
                    "rewrite_only_selected_pattern_ids"
                )
                or [],
                "issue_flags": issue_flags,
                "baseline_sql": result.get("baseline_sql") or "",
                "rewrite_only_sql": result.get("rewrite_only_sql") or "",
                "enhanced_sql": result.get("enhanced_sql") or "",
                "gold_sql": result.get("gold_sql") or "",
            }
        )

    summary = {
        "total_cases": len(audit_rows),
        "flag_counts": dict(counters),
        "matched_count_distribution": dict(
            Counter(str(row["n_matched"]) for row in audit_rows)
        ),
        "top_trigger_groups": Counter(
            group_id for row in audit_rows for group_id in row["matched_group_ids"]
        ).most_common(20),
    }
    return audit_rows, summary


def build_family_audit(
    *,
    report: Dict[str, Any],
    library: Dict[str, Any],
    manual_labels: Dict[int, Dict[str, Any]],
) -> Dict[str, Any]:
    families = []
    for family in report.get("families") or []:
        label_counts = family.get("manual_label_counts") or {}
        total = sum(label_counts.values()) if isinstance(label_counts, dict) else 0
        max_count = max(label_counts.values()) if label_counts else 0
        families.append(
            {
                "group_id": family.get("group_id"),
                "case_ids": family.get("case_ids") or [],
                "support": family.get("support"),
                "manual_label_counts": label_counts,
                "manual_label_purity": (max_count / total) if total else None,
                "mixed_manual_labels": len(label_counts) > 1,
                "question_family_tags": family.get("question_family_tags") or [],
                "pred_family_tags": family.get("pred_family_tags") or [],
                "representative_skeleton": family.get("representative_skeleton") or {},
                "pair_scores": family.get("pair_scores") or [],
            }
        )

    singleton_cases = {
        int(case_id)
        for group in library.get("singletons") or []
        for case_id in group.get("case_ids") or []
        if _as_int(case_id) is not None
    }

    family_membership: Dict[int, str] = {}
    for family in report.get("families") or []:
        for case_id in family.get("case_ids") or []:
            qid = _as_int(case_id)
            if qid is not None:
                family_membership[qid] = str(family.get("group_id"))

    manual_groups: Dict[str, Dict[str, Any]] = {}
    for qid, meta in manual_labels.items():
        if meta.get("kind") not in {"pattern", "family"}:
            continue
        entry = manual_groups.setdefault(
            meta["label"],
            {"label": meta["label"], "kind": meta["kind"], "name": meta["name"], "case_ids": []},
        )
        entry["case_ids"].append(qid)

    missed = []
    for label, group in sorted(manual_groups.items()):
        available = sorted(qid for qid in group["case_ids"] if qid in singleton_cases)
        if len(available) < 2:
            continue
        buckets: Dict[str, List[int]] = defaultdict(list)
        for qid in available:
            buckets[family_membership.get(qid, "unmerged")].append(qid)
        merged_pairs = sum(
            len(ids) * (len(ids) - 1) // 2 for gid, ids in buckets.items() if gid != "unmerged"
        )
        total_pairs = len(available) * (len(available) - 1) // 2
        missed.append(
            {
                "manual_label": label,
                "manual_kind": group["kind"],
                "manual_name": group["name"],
                "available_case_ids": available,
                "family_buckets": dict(sorted(buckets.items())),
                "merged_pair_coverage": (merged_pairs / total_pairs) if total_pairs else 0.0,
            }
        )

    return {
        "families": families,
        "mixed_family_count": sum(1 for family in families if family["mixed_manual_labels"]),
        "manual_group_merge_coverage": missed,
    }


def write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    fieldnames = [
        "qid",
        "step_index",
        "manual_label",
        "manual_kind",
        "manual_name",
        "baseline_correct",
        "rewrite_only_correct",
        "enhanced_correct",
        "outcome",
        "n_matched",
        "matched_group_ids",
        "same_manual_label_sources",
        "diff_manual_label_sources",
        "plan_reason",
        "hint_applicable",
        "rewrite_allowed",
        "raw_hint",
        "instantiated_hint",
        "hint_instantiation_notes",
        "input_candidates",
        "output_candidates",
        "memory_rewrite_candidates_with_inapplicable_hint",
        "selected_cluster_origin",
        "selected_pattern_ids",
        "issue_flags",
        "question",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            csv_row: Dict[str, Any] = {}
            for field in fieldnames:
                value = row.get(field)
                if isinstance(value, (list, dict)):
                    value = json.dumps(value, ensure_ascii=False)
                if field in {"question", "raw_hint", "instantiated_hint", "hint_instantiation_notes"}:
                    value = _short(value)
                csv_row[field] = value
            writer.writerow(csv_row)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run_dir", required=True)
    parser.add_argument("--manual_groups_json", default="")
    parser.add_argument("--output_dir", default="")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else run_dir / "audit"
    library = _load_json(run_dir / "final_library.json", {})
    db_id = library.get("db_id") or _load_json(run_dir / "summary.json", {}).get("db_id")
    manual_payload = _load_json(Path(args.manual_groups_json), []) if args.manual_groups_json else []
    manual_labels = build_manual_label_map(manual_payload, str(db_id))
    groups = _library_groups(library)

    case_rows, case_summary = build_case_audit(
        run_dir=run_dir,
        manual_labels=manual_labels,
        groups=groups,
    )
    family_report = _load_json(run_dir / "family_formation_report.json", {})
    family_audit = build_family_audit(
        report=family_report,
        library=library,
        manual_labels=manual_labels,
    )

    _dump_json(output_dir / "case_audit.json", case_rows)
    write_csv(output_dir / "case_audit.csv", case_rows)
    _dump_json(output_dir / "case_audit_summary.json", case_summary)
    _dump_json(output_dir / "family_merge_audit.json", family_audit)

    print(f"audit_run: cases={len(case_rows)} output_dir={output_dir}")
    print(json.dumps(case_summary, ensure_ascii=False, indent=2)[:3000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
