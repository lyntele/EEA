#!/usr/bin/env python3
"""Summarize B4 structural validation failures from v9 family reports."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


DBS = ("toxicology", "codebase_community")


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _candidate_key(candidate: dict[str, Any]) -> str:
    cids = candidate.get("component_case_ids") or candidate.get("admitted_case_ids") or []
    return ",".join(str(cid) for cid in cids)


def collect(root: Path) -> dict[str, Any]:
    frequency: Counter[str] = Counter()
    per_candidate: list[dict[str, Any]] = []
    candidate_counts: Counter[str] = Counter()
    run_summaries: dict[str, dict[str, Any]] = {}
    pattern_summaries: dict[str, list[dict[str, Any]]] = {}

    for db in DBS:
        db_dir = root / db
        summary_path = db_dir / "summary.json"
        if summary_path.exists():
            summary = (_load_json(summary_path).get("summary") or {})
            run_summaries[db] = {
                "total_cases": summary.get("total_cases"),
                "strict_contract_issue_count": summary.get("strict_contract_issue_count"),
                "ready_cases": summary.get("ready_cases"),
                "triggered_cases": summary.get("triggered_cases"),
                "library_counts": summary.get("library_counts") or {},
                "strict_contract_issues": (_load_json(summary_path).get("strict_contract_issues") or []),
            }

        lib_path = db_dir / "library.json"
        if lib_path.exists():
            lib = _load_json(lib_path)
            patterns = lib.get("patterns") or []
            pattern_summaries[db] = [
                {
                    "group_id": pat.get("group_id"),
                    "case_ids": pat.get("case_ids") or [],
                }
                for pat in patterns
            ]

        report_dir = db_dir / "family_reports"
        for report_path in sorted(report_dir.glob("local_evolve_after_qid_*.json")):
            report = _load_json(report_path)
            candidates = (
                report.get("formation", {}).get("pattern_admission_candidates", [])
                or []
            )
            for candidate in candidates:
                blocker = str(candidate.get("admission_blocker") or "")
                if blocker:
                    candidate_counts[blocker] += 1
                if blocker != "structural_contract_validation_failed":
                    continue
                errors = [
                    str(err)
                    for err in (candidate.get("structural_contract_validation_errors") or [])
                    if str(err)
                ]
                frequency.update(errors or ["<missing_error_detail>"])
                per_candidate.append(
                    {
                        "db": db,
                        "report": report_path.name,
                        "component_case_ids": candidate.get("component_case_ids") or [],
                        "admitted_case_ids": candidate.get("admitted_case_ids") or [],
                        "recognition_payload_present": bool(
                            candidate.get("recognition_payload_present")
                        ),
                        "structural_contract_validation_passed": bool(
                            candidate.get("structural_contract_validation_passed")
                        ),
                        "errors": errors,
                    }
                )

    return {
        "root": str(root),
        "frequency": dict(frequency),
        "per_candidate": per_candidate,
        "candidate_blockers": dict(candidate_counts),
        "run_summaries": run_summaries,
        "pattern_summaries": pattern_summaries,
    }


def render_markdown(data: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Structural Validation Errors v9")
    lines.append("")
    lines.append("Source: `outputs/retrieval_root_evidence_v9/{toxicology,codebase_community}/family_reports`.")
    lines.append("")
    lines.append("## Run summary")
    lines.append("")
    lines.append("| db | total_cases | strict_contract_issues | ready | triggered | patterns | notes |")
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    for db in DBS:
        summary = data["run_summaries"].get(db, {})
        counts = summary.get("library_counts") or {}
        issues = summary.get("strict_contract_issues") or []
        note = ""
        if issues:
            issue_bits = []
            for issue in issues:
                issue_bits.append(
                    f"case {issue.get('case_id')}: {', '.join(issue.get('issues') or [])}"
                )
            note = "; ".join(issue_bits)
        lines.append(
            "| {db} | {total} | {strict} | {ready} | {triggered} | {patterns} | {note} |".format(
                db=db,
                total=summary.get("total_cases", ""),
                strict=summary.get("strict_contract_issue_count", ""),
                ready=summary.get("ready_cases", ""),
                triggered=summary.get("triggered_cases", ""),
                patterns=counts.get("patterns", ""),
                note=note,
            )
        )
    lines.append("")
    lines.append("## Error frequency")
    lines.append("")
    lines.append("| error type | count |")
    lines.append("|---|---:|")
    frequency = Counter(data["frequency"])
    if frequency:
        for error, count in frequency.most_common():
            lines.append(f"| `{error}` | {count} |")
    else:
        lines.append("| `<none>` | 0 |")
    lines.append("")
    lines.append("## Candidate blocker frequency")
    lines.append("")
    lines.append("| blocker | count |")
    lines.append("|---|---:|")
    blocker_counts = Counter(data["candidate_blockers"])
    if blocker_counts:
        for blocker, count in blocker_counts.most_common():
            lines.append(f"| `{blocker}` | {count} |")
    else:
        lines.append("| `<none>` | 0 |")
    lines.append("")
    lines.append("## Per-candidate")
    lines.append("")
    if data["per_candidate"]:
        for item in data["per_candidate"]:
            cids = ",".join(str(cid) for cid in item["component_case_ids"])
            errors = ", ".join(f"`{err}`" for err in item["errors"]) or "`<missing>`"
            lines.append(
                "- db={db} report={report} cids=[{cids}] recognition_payload_present={present} errors=[{errors}]".format(
                    db=item["db"],
                    report=item["report"],
                    cids=cids,
                    present=str(item["recognition_payload_present"]).lower(),
                    errors=errors,
                )
            )
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Pattern snapshot")
    lines.append("")
    for db in DBS:
        lines.append(f"### {db}")
        patterns = data["pattern_summaries"].get(db, [])
        if not patterns:
            lines.append("- none")
            continue
        for pat in patterns:
            cids = ",".join(str(cid) for cid in pat["case_ids"])
            lines.append(f"- `{pat['group_id']}` cases=[{cids}]")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("outputs/retrieval_root_evidence_v9"),
        help="Root containing toxicology/ and codebase_community/ v9 outputs.",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=Path("doc/structural_validation_errors_v9.md"),
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=Path("outputs/retrieval_root_evidence_v9/structural_validation_errors_v9.json"),
    )
    args = parser.parse_args()

    data = collect(args.root)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(render_markdown(data), encoding="utf-8")
    print(f"wrote {args.output_md}")
    print(f"wrote {args.output_json}")


if __name__ == "__main__":
    main()
