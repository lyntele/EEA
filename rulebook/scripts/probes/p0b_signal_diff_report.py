#!/usr/bin/env python3
"""Generate P0b existing-signal audit diff tables."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "workspace" / "probes" / "p0b_existing_signal_audit"
DEFAULT_OUTPUT = ROOT / "doc" / "r_v2_e_phase2_acceptance.md"

Q1_PAIRS = [("1172", "1267"), ("1257", "1267"), ("1257", "1278"), ("1172", "1278")]
Q2_PAIR = ("1418", "1422")
SEED_QIDS = ["1172", "1257"]
REGRESS_QIDS = ["1267", "1278", "1280"]


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _short(value: str, limit: int = 180) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text.replace("|", "\\|")
    return (text[: limit - 3].rstrip() + "...").replace("|", "\\|")


def _flatten(value: Any, prefix: str = "") -> Dict[str, str]:
    out: Dict[str, str] = {}
    if isinstance(value, dict):
        if not value:
            out[prefix or "$"] = "{}"
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else str(key)
            out.update(_flatten(value[key], child))
    elif isinstance(value, list):
        out[prefix or "$"] = _canon(value)
        for idx, item in enumerate(value):
            child = f"{prefix}[{idx}]" if prefix else f"[{idx}]"
            out.update(_flatten(item, child))
    else:
        out[prefix or "$"] = _canon(value)
    return out


def _load_cases(input_dir: Path) -> Dict[str, Dict[str, Any]]:
    cases: Dict[str, Dict[str, Any]] = {}
    for path in sorted(input_dir.glob("*.json")):
        if path.name == "summary.json":
            continue
        payload = json.loads(path.read_text())
        cases[str(payload.get("qid"))] = payload
    return cases


def _flat_tools(case: Mapping[str, Any]) -> Dict[str, str]:
    return _flatten(case.get("tools") or {}, "tools")


def _table_for_pair(cases: Mapping[str, Mapping[str, Any]], left: str, right: str, *, label: str) -> str:
    left_flat = _flat_tools(cases[left])
    right_flat = _flat_tools(cases[right])
    keys = sorted(set(left_flat) | set(right_flat))
    lines = [
        f"### {label}: q{left} vs q{right}",
        "",
        "| 工具 / 字段 | q{} 值 | q{} 值 | 是否区分/同型 |".format(left, right),
        "|---|---|---|---|",
    ]
    for key in keys:
        left_value = left_flat.get(key, "<missing>")
        right_value = right_flat.get(key, "<missing>")
        marker = "同" if left_value == right_value else "异"
        lines.append(
            f"| `{key}` | {_short(left_value)} | {_short(right_value)} | {marker} |"
        )
    return "\n".join(lines)


def _same_value(path: str, flats: Mapping[str, Mapping[str, str]], qids: Sequence[str]) -> Tuple[bool, str]:
    values = [flats[qid].get(path, "<missing>") for qid in qids]
    return len(set(values)) == 1, values[0]


def _runtime_visible_path(path: str) -> bool:
    return path.startswith(
        (
            "tools.case_signal_view.",
            "tools.current_case_signals",
            "tools.pred_current_summary.",
            "tools.role_graph.pred_sql.",
        )
    )


def _candidate_field_table(cases: Mapping[str, Mapping[str, Any]]) -> str:
    flats = {qid: _flat_tools(case) for qid, case in cases.items()}
    all_paths = sorted(set().union(*(set(flat.keys()) for flat in flats.values())))
    rows: List[Tuple[str, str, str, bool]] = []
    for path in all_paths:
        seed_same, seed_value = _same_value(path, flats, SEED_QIDS)
        regress_same, regress_value = _same_value(path, flats, REGRESS_QIDS)
        if seed_same and regress_same and seed_value != regress_value:
            rows.append((path, seed_value, regress_value, _runtime_visible_path(path)))
    lines = [
        "## 表 3 — 关键差异候选",
        "",
        "筛选条件：q1172/q1257 内部一致，q1267/q1278/q1280 内部一致，且两组之间不同。",
        "",
        "| 工具 / 字段 | seed 组值 | regress 组值 | 是否 answer-blind/runtime-visible |",
        "|---|---|---|---|",
    ]
    for path, seed_value, regress_value, runtime_visible in rows:
        lines.append(
            f"| `{path}` | {_short(seed_value)} | {_short(regress_value)} | {runtime_visible} |"
        )
    if not rows:
        lines.append("| `<none>` |  |  |  |")
    lines.append("")
    lines.append(f"候选字段数：{len(rows)}；其中 runtime-visible 候选数：{sum(1 for row in rows if row[3])}。")
    return "\n".join(lines)


def _summary_conclusion(cases: Mapping[str, Mapping[str, Any]]) -> str:
    flats = {qid: _flat_tools(case) for qid, case in cases.items()}
    all_paths = sorted(set().union(*(set(flat.keys()) for flat in flats.values())))
    candidates: List[str] = []
    runtime_candidates: List[str] = []
    student_mismatch: List[str] = []
    for path in all_paths:
        seed_same, seed_value = _same_value(path, flats, SEED_QIDS)
        regress_same, regress_value = _same_value(path, flats, REGRESS_QIDS)
        if seed_same and regress_same and seed_value != regress_value:
            candidates.append(path)
            if _runtime_visible_path(path):
                runtime_candidates.append(path)
                if flats["1418"].get(path, "<missing>") != flats["1422"].get(path, "<missing>"):
                    student_mismatch.append(path)
    safe_runtime = [path for path in runtime_candidates if path not in student_mismatch]
    top_safe = ", ".join(f"`{path}`" for path in safe_runtime[:8]) or "无"
    if safe_runtime:
        route = (
            "现有工具存在可作为 A 路径继续审查的 answer-blind/runtime-visible 候选字段；"
            "这些字段同时满足 thrombosis seed/regress 分组差异和 q1418/q1422 同型。"
        )
    elif runtime_candidates:
        route = (
            "现有工具有 answer-blind/runtime-visible 区分字段，但这些字段在 q1418/q1422 上不同型，"
            "直接做 runtime hard match 会误拦 student_club 接力。"
        )
    else:
        route = (
            "现有工具的稳定区分主要不在 answer-blind/runtime-visible 字段上；"
            "仅靠 runtime source hard match 看不到足够安全的区分维度。"
        )
    return "\n".join(
        [
            "## 结论",
            "",
            f"{route} 当前自动筛出的全部候选字段数为 {len(candidates)}，runtime-visible 候选字段数为 {len(runtime_candidates)}，其中 q1418/q1422 同型的 runtime-visible 候选字段数为 {len(safe_runtime)}。",
            "",
            f"若走 A 路径，优先审查的 hard match 字段候选是：{top_safe}。若这些字段经人工确认不可作为 source hard match，则 P0b 应转到上游，即 hint 合成或 admission 入组阶段，而不是继续在 runtime 追加规则。",
        ]
    )


def build_report(cases: Mapping[str, Mapping[str, Any]]) -> str:
    lines = [
        "## P0b-existing-signal-audit",
        "",
        "本节只使用现有派生工具输出，不新增派生器、不新增 runtime 规则。完整原始 dump 位于 `workspace/probes/p0b_existing_signal_audit/`。",
        "",
        "## 表 1 — Q1 区分能力",
        "",
    ]
    for left, right in Q1_PAIRS:
        lines.append(_table_for_pair(cases, left, right, label="Q1"))
        lines.append("")
    lines.extend(
        [
            "## 表 2 — Q2 不误拦能力",
            "",
            _table_for_pair(cases, Q2_PAIR[0], Q2_PAIR[1], label="Q2"),
            "",
            _candidate_field_table(cases),
            "",
            _summary_conclusion(cases),
            "",
        ]
    )
    return "\n".join(lines)


def _replace_section(existing: str, section: str) -> str:
    marker = "## P0b-existing-signal-audit"
    if marker not in existing:
        prefix = existing.rstrip() + "\n\n" if existing.strip() else "# r_v2_e Phase 2 Acceptance\n\n"
        return prefix + section.rstrip() + "\n"
    before = existing.split(marker, 1)[0].rstrip()
    return before + "\n\n" + section.rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_dir", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output_md", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    cases = _load_cases(args.input_dir)
    required = sorted(set(sum(([a, b] for a, b in Q1_PAIRS), [])) | set(Q2_PAIR))
    missing = [qid for qid in required if qid not in cases]
    if missing:
        raise SystemExit(f"missing case dump(s): {missing}")
    section = build_report(cases)
    existing = args.output_md.read_text() if args.output_md.exists() else ""
    args.output_md.write_text(_replace_section(existing, section))
    print(f"[p0b-diff] wrote {args.output_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
