#!/usr/bin/env python3
"""Audit current case_signal_view coverage for P0b source-state gating.

This script is read-only with respect to EEA runtime/admission code. It loads
the historical full11 r1 logs, uses `selection.rewrite_only_selected_sql` as
the observed S0 when available, and dumps the existing pipeline's
case_signal_view for selected anchor cases.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping


ROOT = Path(__file__).resolve().parents[2]
RUN_ROOT = Path(
    "/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL/workspace/rulebook_runs"
)
DEV_JSON = Path("/data/liuyining/ace4sql/bench/bird/dev/dev.json")
DB_ROOT = Path("/data/liuyining/ace4sql/bench/bird/dev/dev_databases")
RUN_SUFFIX = "postsel_v1_qwen3coderflash_20260510_164346"
DEFAULT_OUTPUT = ROOT / "workspace" / "probes" / "case_signal_view_p0b_audit"
DEFAULT_REPORT = ROOT / "doc" / "case_signal_view_p0b_audit.md"

ANCHORS = [
    {"qid": "1172", "db_id": "thrombosis_prediction", "role": "seed"},
    {"qid": "1267", "db_id": "thrombosis_prediction", "role": "regress_selected_s1"},
    {"qid": "1418", "db_id": "student_club", "role": "seed"},
    {"qid": "268", "db_id": "toxicology", "role": "helped_selected_s1"},
    {"qid": "277", "db_id": "toxicology", "role": "helped_selected_s1"},
]


def _load_existing_audit_module():
    path = ROOT / "scripts" / "probes" / "p0b_existing_signal_audit.py"
    spec = importlib.util.spec_from_file_location("p0b_existing_signal_audit", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


existing_audit = _load_existing_audit_module()


def _norm_qid(value: Any) -> str:
    text = str(value or "").strip()
    return text[1:] if text.lower().startswith("q") else text


def _load_dev_rows() -> Dict[tuple[str, str], Mapping[str, Any]]:
    rows = json.loads(DEV_JSON.read_text(encoding="utf-8"))
    return {
        (str(row.get("db_id") or ""), _norm_qid(row.get("question_id"))): row
        for row in rows
        if isinstance(row, dict)
    }


def _load_log_rows() -> Dict[tuple[str, str], Mapping[str, Any]]:
    out: Dict[tuple[str, str], Mapping[str, Any]] = {}
    for anchor in ANCHORS:
        db_id = anchor["db_id"]
        path = RUN_ROOT / f"full11_{db_id}_{RUN_SUFFIX}" / "per_case_log.jsonl"
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                qid = _norm_qid(row.get("question_id"))
                if (db_id, qid) in {(item["db_id"], item["qid"]) for item in ANCHORS}:
                    out[(db_id, qid)] = row
    return out


def _case_payload(anchor: Mapping[str, Any], log_row: Mapping[str, Any], dev_row: Mapping[str, Any]) -> Dict[str, Any]:
    result = log_row.get("result") if isinstance(log_row.get("result"), dict) else {}
    selection = log_row.get("selection") if isinstance(log_row.get("selection"), dict) else {}
    db_id = str(anchor["db_id"])
    qid = str(anchor["qid"])
    pred_sql = str(
        selection.get("rewrite_only_selected_sql")
        or result.get("s0_sql")
        or result.get("baseline_sql")
        or result.get("generation_top1_sql")
        or ""
    )
    return {
        "qid": qid,
        "db_id": db_id,
        "role": anchor.get("role"),
        "baseline_correct": result.get("baseline_correct"),
        "enhanced_correct": result.get("enhanced_correct"),
        "question": str(log_row.get("question") or dev_row.get("question") or ""),
        "evidence": str(log_row.get("evidence") or dev_row.get("evidence") or ""),
        "pred_sql": pred_sql,
        "gold_sql": str(result.get("gold_sql") or dev_row.get("SQL") or ""),
        "db_path": str(DB_ROOT / db_id / f"{db_id}.sqlite"),
        "source_run": str(RUN_ROOT / f"full11_{db_id}_{RUN_SUFFIX}"),
        "pred_sql_source": (
            "selection.rewrite_only_selected_sql"
            if selection.get("rewrite_only_selected_sql")
            else "result.s0_sql_or_fallback"
        ),
    }


def _pred_summary(case_dump: Mapping[str, Any]) -> Dict[str, Any]:
    view = (((case_dump.get("tools") or {}).get("case_signal_view") or {}).get("pred_sql_view") or {})
    output_shape = view.get("output_shape_current") or {}
    aggregate = view.get("aggregate_profile") or {}
    predicate = view.get("predicate_profile") or {}
    group_order = view.get("group_order_profile") or {}
    return {
        "qid": case_dump.get("qid"),
        "db_id": case_dump.get("db_id"),
        "role": case_dump.get("role"),
        "pred_sql_source": case_dump.get("pred_sql_source"),
        "select_arity": view.get("select_arity"),
        "select_items": view.get("select_items"),
        "grain": output_shape.get("grain"),
        "shape_has_distinct": output_shape.get("has_distinct"),
        "shape_has_aggregate": output_shape.get("has_aggregate"),
        "tables_used": view.get("tables_used"),
        "join_count": len(view.get("join_graph") or []),
        "predicate_count": predicate.get("predicate_count"),
        "literal_count": predicate.get("literal_count"),
        "comparison_operators": predicate.get("comparison_operators"),
        "has_count": aggregate.get("has_count"),
        "has_count_star": aggregate.get("has_count_star"),
        "has_count_distinct": aggregate.get("has_count_distinct"),
        "has_sum": aggregate.get("has_sum"),
        "has_avg": aggregate.get("has_avg"),
        "group_by_count": group_order.get("group_by_count"),
        "order_by_count": group_order.get("order_by_count"),
    }


def _md(value: Any) -> str:
    text = " ".join(str(value).split())
    return text.replace("|", "\\|")


def _build_report(case_dumps: List[Mapping[str, Any]], summary_rows: List[Mapping[str, Any]]) -> str:
    lines = [
        "# P0b case_signal_view 现状 audit",
        "",
        "本报告只读当前实现，不新增信号抽取逻辑。输入 SQL 使用 r1 `selection.rewrite_only_selected_sql`，缺失时回退到 `result.s0_sql`。",
        "",
        "## Anchor Summary",
        "",
        "| qid | db | role | pred_sql_source | select_arity | distinct | count_star | count_distinct | join_count | predicate_count | grain |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in summary_rows:
        lines.append(
            "| q{qid} | {db_id} | {role} | {pred_sql_source} | {select_arity} | {shape_has_distinct} | {has_count_star} | {has_count_distinct} | {join_count} | {predicate_count} | {grain} |".format(
                **{key: _md(value) for key, value in row.items()}
            )
        )
    lines.extend(
        [
            "",
            "## Observed Coverage",
            "",
            "- `case_signal_view.pred_sql_view` 已经包含 `select_arity`、`output_shape_current.has_distinct`、`aggregate_profile.has_count_star`、`aggregate_profile.has_count_distinct`、`join_graph`、`predicate_profile.predicate_count/literal_count`、`group_order_profile`。",
            "- 因此 P0b 需要的 distinct / aggregate function / count-star / join-count / predicate-count 这类 SQL 结构观测并不是完全缺失；它们已经在现有 `case_signal_view` 内部存在。",
            "- q1172 与 q1267 已可由现有字段机械区分：q1172 是 `has_count_star=true / has_count_distinct=false / join_count=1`，q1267 的 r1 selected SQL 是 `has_count_star=false / has_count_distinct=true / join_count=2`。",
            "- 注意：`output_shape_current.has_distinct` 不覆盖 `COUNT(DISTINCT ...)`，q1267 这里仍为 `false`；aggregate-level DISTINCT 必须读 `aggregate_profile.has_count_distinct`。",
            "- 但是 `runtime_signature` 目前只带 `output_shape_current`、`tables_used`、schema resolvability 等摘要，不包含完整 `aggregate_profile` 和 `predicate_profile`；也没有一个去字面化的 flat fact set 可直接作为 hard gate。",
            "",
            "## Gaps For P0b",
            "",
            "- 缺的不是 AST 解析能力，而是从 `case_signal_view.pred_sql_view` 到 runtime hard-gate facts 的统一投影层。",
            "- 不能直接把 `select_items`、`predicate_profile.predicates`、`join_graph.on_clause` 当 gate，因为这些字段携带表名、列名、alias、字面值。",
            "- 需要扩展 `case_signal_view` 或其 runtime projection，生成统一、去字面化、可复算的 facts，例如 aggregate/count/distinct/select-arity/join-count/predicate-count/group-order 等结构事实。",
            "- P0b 的 LLM 只能从 seed cases 的这些既有 facts 里选择共享 facts；不能发明 check_type，也不能把原始 SQL substring 作为 signal。",
            "",
            "## Raw Summary JSON",
            "",
            "```json",
            json.dumps(summary_rows, ensure_ascii=False, indent=2),
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output_dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    dev_rows = _load_dev_rows()
    log_rows = _load_log_rows()
    case_dumps: List[Dict[str, Any]] = []
    summary_rows: List[Dict[str, Any]] = []
    for anchor in ANCHORS:
        key = (anchor["db_id"], anchor["qid"])
        case = _case_payload(anchor, log_rows[key], dev_rows.get(key, {}))
        dump = existing_audit._dump_case(case)
        dump["pred_sql_source"] = case["pred_sql_source"]
        out_path = args.output_dir / f"{anchor['qid']}.json"
        out_path.write_text(json.dumps(dump, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        case_dumps.append(dump)
        summary_rows.append(_pred_summary(dump))
        print(f"[case-signal-audit] dumped {anchor['db_id']} q{anchor['qid']} -> {out_path}")
    (args.output_dir / "summary.json").write_text(
        json.dumps({"cases": summary_rows}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    args.report.write_text(_build_report(case_dumps, summary_rows), encoding="utf-8")
    print(f"[case-signal-audit] wrote report -> {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
