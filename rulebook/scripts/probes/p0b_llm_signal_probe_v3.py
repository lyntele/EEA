#!/usr/bin/env python3
"""Dry-run probe v3 for group-level source_signals with seed sanity filtering.

This is intentionally a probe-only wrapper over p0b_llm_signal_probe_v2. It
does not modify runtime/admission/schema code.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple


THIS_FILE = Path(__file__).resolve()
RULEBOOK_ROOT = THIS_FILE.parents[2]
V2_PATH = THIS_FILE.with_name("p0b_llm_signal_probe_v2.py")
DEFAULT_OUTPUT_DIR = RULEBOOK_ROOT / "workspace" / "probes" / "p0b_llm_signal_probe_v3"
DEFAULT_REPORT = RULEBOOK_ROOT / "doc" / "r_v2_e_phase2_acceptance.md"


def _load_v2_module():
    spec = importlib.util.spec_from_file_location("p0b_llm_signal_probe_v2", V2_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load v2 probe from {V2_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


v2 = _load_v2_module()


def seed_sanity_filter(
    payload: Mapping[str, Any],
    group: Mapping[str, Any],
    cases: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    """Drop signals that do not pass every seed S0 in the group."""
    kept: List[str] = []
    seed_filtered: List[Dict[str, Any]] = []
    seed_results: List[Dict[str, Any]] = []
    for signal in payload.get("source_signals", []) or []:
        per_seed = [
            {
                "case_id": qid,
                "result": v2.verify_signal(signal, str(cases[qid]["pred_sql"])),
            }
            for qid in group["seed_qids"]
        ]
        seed_results.append({"signal": signal, "per_seed": per_seed})
        if all(item["result"] is True for item in per_seed):
            kept.append(signal)
        else:
            seed_filtered.append(
                {
                    "signal": signal,
                    "reason": "signal_filtered_seed_sanity",
                    "per_seed": per_seed,
                }
            )
    gate_enabled = len(kept) >= 2
    out = dict(payload)
    out["pre_seed_sanity_source_signals"] = list(payload.get("source_signals", []) or [])
    out["source_signals"] = kept
    out["seed_sanity_filtered_signals"] = seed_filtered
    out["seed_sanity_results"] = seed_results
    out["hard_gate_enabled"] = gate_enabled
    out["gate_disabled_reason"] = "" if gate_enabled else "gate_disabled_insufficient_signals"
    return out


def evaluate_groups(
    group_payloads: Mapping[str, Mapping[str, Any]],
    cases: Mapping[str, Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    sanity_rows: List[Dict[str, Any]] = []
    cross_rows: List[Dict[str, Any]] = []
    for group_id, group in v2.GROUPS.items():
        payload = group_payloads.get(group_id, {})
        signals = list(payload.get("source_signals") or [])
        gate_enabled = bool(payload.get("hard_gate_enabled"))
        for qid in group["seed_qids"]:
            per_signal = [
                {"signal": signal, "result": v2.verify_signal(signal, cases[qid]["pred_sql"])}
                for signal in signals
            ]
            actual = bool(per_signal) and all(item["result"] is True for item in per_signal)
            sanity_rows.append(
                {
                    "group_id": group_id,
                    "case_id": qid,
                    "expected": True,
                    "actual": actual,
                    "gate_enabled": gate_enabled,
                    "per_signal": per_signal,
                    "status": "ok" if actual is True else "mismatch",
                }
            )
        for qid, expected in group["cross"]:
            per_signal = [
                {"signal": signal, "result": v2.verify_signal(signal, cases[qid]["pred_sql"])}
                for signal in signals
            ]
            if not gate_enabled:
                actual: bool | None = None
                status = "gate_disabled"
            else:
                actual = bool(per_signal) and all(item["result"] is True for item in per_signal)
                status = "ok" if actual == expected else "mismatch"
            cross_rows.append(
                {
                    "group_id": group_id,
                    "case_id": qid,
                    "expected": expected,
                    "actual": actual,
                    "gate_enabled": gate_enabled,
                    "per_signal": per_signal,
                    "status": status,
                }
            )
    return sanity_rows, cross_rows


def quality_metrics(group_payloads: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    raw = [
        signal
        for payload in group_payloads.values()
        for signal in payload.get("raw_source_signals", [])
    ]
    case_filtered = [
        item
        for payload in group_payloads.values()
        for item in payload.get("filtered_signals", [])
    ]
    seed_filtered = [
        item
        for payload in group_payloads.values()
        for item in payload.get("seed_sanity_filtered_signals", [])
    ]
    final = [
        signal
        for payload in group_payloads.values()
        for signal in payload.get("source_signals", [])
    ]
    compliant = [signal for signal in raw if v2._signal_format_ok(signal)]
    enabled = [gid for gid, payload in group_payloads.items() if payload.get("hard_gate_enabled")]
    disabled = [
        gid
        for gid, payload in group_payloads.items()
        if not payload.get("hard_gate_enabled")
    ]
    return {
        "raw_signal_count": len(raw),
        "case_specific_filtered_count": len(case_filtered),
        "seed_sanity_filtered_count": len(seed_filtered),
        "final_signal_count": len(final),
        "format_compliance_rate": (len(compliant) / len(raw)) if raw else 0.0,
        "avg_final_signals_per_group": len(final) / len(v2.GROUPS),
        "hard_gate_enabled_groups": enabled,
        "hard_gate_disabled_groups": disabled,
        "check_type_distribution": dict(v2.Counter(v2._check_type(signal) for signal in final)),
        "case_specific_filtered_reason_distribution": dict(
            v2.Counter(item.get("reason") for item in case_filtered)
        ),
        "seed_sanity_filtered_by_group": {
            gid: len(payload.get("seed_sanity_filtered_signals", []) or [])
            for gid, payload in group_payloads.items()
        },
    }


def compute_flags(
    group_payloads: Mapping[str, Mapping[str, Any]],
    cross_rows: List[Mapping[str, Any]],
    metrics: Mapping[str, Any],
) -> Dict[str, Any]:
    flags: Dict[str, Any] = {}
    if float(metrics.get("format_compliance_rate") or 0.0) < 0.70:
        flags["halt"] = "format_compliance_below_70_percent"
    thrombosis_rows = [
        row
        for row in cross_rows
        if row.get("group_id") == "thrombosis_distinct_count_subject"
        and row.get("case_id") in {"1267", "1278", "1280"}
    ]
    thrombosis_blocked = [
        row for row in thrombosis_rows if row.get("gate_enabled") and row.get("actual") is False
    ]
    flags["thrombosis_block_rate_1267_1278_1280"] = (
        f"{len(thrombosis_blocked)}/{len(thrombosis_rows)}"
    )
    if len(thrombosis_blocked) < len(thrombosis_rows):
        flags["halt"] = "thrombosis_block_rate_below_3_of_3"
    student_row = next(
        (
            row
            for row in cross_rows
            if row.get("group_id") == "studentclub_event_type_lookup"
            and row.get("case_id") == "1422"
        ),
        None,
    )
    if not student_row or student_row.get("actual") is not True:
        flags["halt"] = "studentclub_q1418_group_blocks_q1422"
    tox_rows = [
        row
        for row in cross_rows
        if row.get("group_id") == "toxicology_drop_extra_role_side"
        and row.get("case_id") in {"277", "307"}
    ]
    tox_bad = [
        row
        for row in tox_rows
        if row.get("gate_enabled") and row.get("actual") is False
    ]
    if tox_bad:
        flags["halt"] = "toxicology_strong_sibling_unexpectedly_blocked"
        flags["toxicology_unexpected_blocked_cases"] = [f"q{row['case_id']}" for row in tox_bad]
    disabled = [
        gid
        for gid, payload in group_payloads.items()
        if not payload.get("hard_gate_enabled")
    ]
    if disabled:
        flags["gate_disabled_groups"] = disabled
    return flags


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _md(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value).split())
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text.replace("|", "\\|")


def _boolish(value: Any) -> str:
    if value is True:
        return "pass"
    if value is False:
        return "fail"
    return str(value)


def build_report(
    group_payloads: Mapping[str, Mapping[str, Any]],
    sanity_rows: List[Mapping[str, Any]],
    cross_rows: List[Mapping[str, Any]],
    metrics: Mapping[str, Any],
    flags: Mapping[str, Any],
) -> str:
    source_dump = {
        gid: {
            "raw_source_signals": payload.get("raw_source_signals", []),
            "case_specific_filtered": payload.get("filtered_signals", []),
            "pre_seed_sanity_source_signals": payload.get("pre_seed_sanity_source_signals", []),
            "seed_sanity_filtered": payload.get("seed_sanity_filtered_signals", []),
            "final_source_signals": payload.get("source_signals", []),
            "hard_gate_enabled": payload.get("hard_gate_enabled"),
            "gate_disabled_reason": payload.get("gate_disabled_reason"),
        }
        for gid, payload in group_payloads.items()
    }
    lines: List[str] = [
        "## §P0b-llm-signal-probe-v3",
        "",
        "Dry-run v3 probe for group-level LLM `source_signals` with two filters: case-specific signal filtering and seed_sanity_filter. Cross-checks use only the final filtered signals.",
        "",
        "### Filtered Source Signals",
        "",
        "```json",
        _json_dump(source_dump),
        "```",
        "",
        "### Seed Sanity After Filter",
        "",
        "| Group | Seed | Gate Enabled | Actual | Per-signal results | Status |",
        "|---|---|---:|---:|---|---|",
    ]
    for row in sanity_rows:
        per_signal = "; ".join(
            f"{_md(item['signal'])} => {_boolish(item['result'])}"
            for item in row.get("per_signal", [])
        ) or "<none>"
        lines.append(
            f"| {row['group_id']} | q{row['case_id']} | {row['gate_enabled']} | {row['actual']} | {per_signal} | {row['status']} |"
        )
    lines.extend(
        [
            "",
            "### Cross-Check Matrix After Filter",
            "",
            "| Group | Target | Expected | Actual | Gate Enabled | Per-signal results | Status |",
            "|---|---|---:|---:|---:|---|---|",
        ]
    )
    for row in cross_rows:
        per_signal = "; ".join(
            f"{_md(item['signal'])} => {_boolish(item['result'])}"
            for item in row.get("per_signal", [])
        ) or "<none>"
        lines.append(
            f"| {row['group_id']} | q{row['case_id']} | {row['expected']} | {row['actual']} | {row['gate_enabled']} | {per_signal} | {row['status']} |"
        )
    lines.extend(
        [
            "",
            "### Quality Metrics",
            "",
            "```json",
            _json_dump(metrics),
            "```",
            "",
            "### Flags",
            "",
            "```json",
            _json_dump(flags),
            "```",
        ]
    )
    if "halt" in flags:
        conclusion = f"Conclusion: v3 filter probe did not pass the P0b entry gate. halt={flags['halt']}."
    else:
        conclusion = (
            "Conclusion: v3 filter probe passed the requested P0b entry checks: thrombosis blocks 3/3, "
            "student_club q1422 passes, and toxicology strong siblings are not unexpectedly blocked."
        )
    lines.extend(["", conclusion, ""])
    return "\n".join(lines)


def _replace_or_append_section(existing: str, section: str) -> str:
    marker = "## §P0b-llm-signal-probe-v3"
    if marker not in existing:
        prefix = existing.rstrip() + "\n\n" if existing.strip() else ""
        return prefix + section.rstrip() + "\n"
    before, rest = existing.split(marker, 1)
    next_match = re.search(r"\n## ", rest)
    after = rest[next_match.start() :] if next_match else ""
    return before.rstrip() + "\n\n" + section.rstrip() + "\n" + after


def write_report(report_path: Path, section: str) -> None:
    existing = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    report_path.write_text(_replace_or_append_section(existing, section), encoding="utf-8")


def maybe_reuse_v2_payload(
    group_id: str,
    group: Mapping[str, Any],
    cases: Mapping[str, Mapping[str, Any]],
    output_dir: Path,
    reuse_v2_dir: Path | None,
) -> Dict[str, Any]:
    if reuse_v2_dir:
        path = reuse_v2_dir / f"{group_id}_llm.json"
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            copied = dict(payload)
            copied["reused_from_v2"] = str(path)
            return copied
    return v2.call_group_llm(group_id, group, cases, output_dir)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run_root", type=Path, default=v2.DEFAULT_RUN_ROOT)
    parser.add_argument("--dev_json", type=Path, default=v2.DEFAULT_DEV_JSON)
    parser.add_argument("--output_dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--report_path", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--reuse_v2_dir", type=Path, default=None)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    cases, skipped = v2.load_cases(args.run_root, args.dev_json)
    (args.output_dir / "case_data.json").write_text(
        json.dumps({"cases": cases, "skipped": skipped}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[p0b-v3] loaded {len(cases)}/{len(v2.TARGET_CASES)} target cases")
    if skipped:
        print(f"[p0b-v3] skipped/missing entries: {len(skipped)}")

    group_payloads: Dict[str, Dict[str, Any]] = {}
    for group_id, group in v2.GROUPS.items():
        missing = [qid for qid in group["seed_qids"] if qid not in cases]
        if missing:
            print(f"[p0b-v3] skip group {group_id}: missing seeds {missing}")
            continue
        print(f"[p0b-v3] {'reusing' if args.reuse_v2_dir else 'calling LLM for'} group {group_id}")
        payload = maybe_reuse_v2_payload(group_id, group, cases, args.output_dir, args.reuse_v2_dir)
        payload = seed_sanity_filter(payload, group, cases)
        group_payloads[group_id] = payload
        (args.output_dir / f"{group_id}_llm.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            f"[p0b-v3] {group_id} final={len(payload.get('source_signals') or [])} "
            f"gate={payload.get('hard_gate_enabled')} "
            f"seed_filtered={len(payload.get('seed_sanity_filtered_signals') or [])}"
        )

    sanity_rows, cross_rows = evaluate_groups(group_payloads, cases)
    metrics = quality_metrics(group_payloads)
    flags = compute_flags(group_payloads, cross_rows, metrics)
    summary = {
        "loaded_case_count": len(cases),
        "target_case_count": len(v2.TARGET_CASES),
        "skipped": skipped,
        "group_payloads": {
            gid: {
                "db": payload.get("db"),
                "seed_qids": payload.get("seed_qids"),
                "raw_source_signals": payload.get("raw_source_signals"),
                "case_specific_filtered": payload.get("filtered_signals"),
                "pre_seed_sanity_source_signals": payload.get("pre_seed_sanity_source_signals"),
                "seed_sanity_filtered_signals": payload.get("seed_sanity_filtered_signals"),
                "source_signals": payload.get("source_signals"),
                "hard_gate_enabled": payload.get("hard_gate_enabled"),
                "gate_disabled_reason": payload.get("gate_disabled_reason"),
            }
            for gid, payload in group_payloads.items()
        },
        "sanity_rows": sanity_rows,
        "cross_rows": cross_rows,
        "metrics": metrics,
        "flags": flags,
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    section = build_report(group_payloads, sanity_rows, cross_rows, metrics, flags)
    write_report(args.report_path, section)
    print(f"[p0b-v3] flags={json.dumps(flags, ensure_ascii=False, sort_keys=True)}")
    print(f"[p0b-v3] wrote report section to {args.report_path}")
    print(f"[p0b-v3] wrote JSON traces to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
