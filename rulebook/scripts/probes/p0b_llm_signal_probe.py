#!/usr/bin/env python3
"""Dry-run probe for LLM-authored source_signals at admission time."""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple


RULEBOOK_ROOT = Path(__file__).resolve().parents[2]
EEA_ROOT = RULEBOOK_ROOT.parent
ACE_ROOT = EEA_ROOT.parent.parent
if str(ACE_ROOT) not in sys.path:
    sys.path.insert(0, str(ACE_ROOT))

DEFAULT_LOG_GLOB = (
    "/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL/workspace/"
    "rulebook_runs/full11_*_postsel_v1_qwen3coderflash_20260510_164346/"
    "per_case_log.jsonl"
)
DEFAULT_DEV_JSON = Path("/data/liuyining/ace4sql/bench/bird/dev/dev.json")
DEFAULT_OUTPUT_DIR = RULEBOOK_ROOT / "workspace" / "probes" / "p0b_llm_signal_probe"
DEFAULT_SCRIPT_REPORT = RULEBOOK_ROOT / "doc" / "r_v2_e_phase2_acceptance.md"

TARGET_CASES: Dict[str, Dict[str, Any]] = {
    "1172": {"db": "thrombosis_prediction", "role": "seed"},
    "1257": {"db": "thrombosis_prediction", "role": "seed"},
    "1267": {"db": "thrombosis_prediction", "role": "regress"},
    "1278": {"db": "thrombosis_prediction", "role": "regress"},
    "1280": {"db": "thrombosis_prediction", "role": "regress"},
    "1418": {"db": "student_club", "role": "seed"},
    "1422": {"db": "student_club", "role": "future"},
    "268": {"db": "toxicology", "role": "seed"},
    "277": {"db": "toxicology", "role": "seed"},
    "307": {"db": "toxicology", "role": "seed"},
}
SEED_QIDS = ["1172", "1257", "1418", "268", "277", "307"]
CROSS_CHECKS: List[Tuple[str, str, bool]] = [
    ("1172", "1267", False),
    ("1172", "1278", False),
    ("1172", "1280", False),
    ("1257", "1267", False),
    ("1257", "1278", False),
    ("1257", "1280", False),
    ("1418", "1422", True),
    ("268", "277", True),
    ("268", "307", True),
    ("277", "268", True),
    ("277", "307", True),
    ("307", "268", True),
    ("307", "277", True),
]
SUPPORTED_CHECK_TYPES = {
    "s0_contains_token",
    "s0_not_contains_token",
    "s0_regex_match",
    "s0_join_table_count",
    "s0_select_column_count",
    "s0_distinct_count",
    "s0_count_star_count",
    "s0_count_distinct_count",
    "s0_where_condition_count",
}

PROMPT_TEMPLATE = """You are analyzing a NL2SQL repair case to extract machine-verifiable preconditions.

Given a question, a wrong SQL (S0), and the correct SQL (gold), your task is to write a list of source_signals that describe syntactic conditions that S0 must satisfy for this repair to be applicable.

These signals will be used to mechanically check future SQL queries. They must be precise enough to distinguish cases where this repair applies from cases where it does not.

STRICT FORMAT RULES:
1. Each signal must be in the format: <check_type>:<expression>
2. check_type must be one of these mechanical checks (or a new check_type you invent that is equally mechanical):
   - s0_contains_token: case-insensitive substring present in S0
   - s0_not_contains_token: case-insensitive substring NOT present in S0
   - s0_regex_match: Python re.search pattern matches S0
   - s0_join_table_count:>=N or <=N or ==N: number of JOINs in S0
   - s0_select_column_count:>=N or <=N or ==N: number of columns in SELECT clause
3. You MUST NOT write free natural language. Every signal must parse as <check_type>:<expression>.
4. Write 2-5 signals. Fewer tight signals are better than many loose ones.
5. Output ONLY valid JSON: {{"source_signals": ["<check_type>:<expression>", ...]}}

Question: {question}

Wrong SQL (S0):
{pred_sql}

Correct SQL (gold):
{gold_sql}

What source_signals describe what S0 must look like for this repair to apply?"""


def _norm_qid(value: Any) -> str:
    text = str(value or "").strip()
    return text[1:] if text.lower().startswith("q") else text


def _first_non_empty(mapping: Mapping[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _extract_case_fields(row: Mapping[str, Any], path: Path) -> Dict[str, Any]:
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    question = _first_non_empty(row, ("question", "nl", "query"))
    pred_sql = _first_non_empty(
        row,
        ("pred_sql", "predicted_sql", "prediction_sql", "s0_sql", "baseline_sql"),
    )
    if not pred_sql:
        pred_sql = _first_non_empty(
            result,
            ("pred_sql", "predicted_sql", "prediction_sql", "s0_sql", "baseline_sql", "generation_top1_sql"),
        )
    gold_sql = _first_non_empty(row, ("gold_sql", "gold", "ground_truth_sql"))
    if not gold_sql:
        gold_sql = _first_non_empty(result, ("gold_sql", "gold", "ground_truth_sql"))
    qid = _norm_qid(row.get("question_id") or row.get("case_id") or row.get("qid"))
    db = str(row.get("db_id") or "").strip()
    return {
        "case_id": qid,
        "db": db,
        "question": question,
        "pred_sql": pred_sql,
        "gold_sql": gold_sql,
        "baseline_correct": result.get("baseline_correct", row.get("baseline_correct")),
        "enhanced_correct": result.get("enhanced_correct", row.get("enhanced_correct")),
        "source_log": str(path),
    }


def _load_dev_rows(dev_json: Path) -> Dict[Tuple[str, str], Dict[str, Any]]:
    if not dev_json.exists():
        return {}
    rows = json.loads(dev_json.read_text(encoding="utf-8"))
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        qid = _norm_qid(row.get("question_id"))
        db = str(row.get("db_id") or "").strip()
        if not qid or not db:
            continue
        out[(db, qid)] = row
    return out


def _merge_dev_fields(payload: Dict[str, Any], dev_row: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(payload)
    if not str(merged.get("question") or "").strip():
        merged["question"] = str(dev_row.get("question") or "")
    if not str(merged.get("gold_sql") or "").strip():
        merged["gold_sql"] = str(dev_row.get("SQL") or dev_row.get("gold_sql") or "")
    if not str(merged.get("evidence") or "").strip():
        merged["evidence"] = str(dev_row.get("evidence") or "")
    return merged


def load_case_data(
    log_glob: str,
    *,
    dev_json: Path = DEFAULT_DEV_JSON,
) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    wanted_by_db_qid = {(meta["db"], qid) for qid, meta in TARGET_CASES.items()}
    dev_rows = _load_dev_rows(dev_json)
    cases: Dict[str, Dict[str, Any]] = {}
    skipped: List[Dict[str, Any]] = []
    for raw_path in sorted(glob.glob(log_glob)):
        path = Path(raw_path)
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    skipped.append(
                        {
                            "source_log": str(path),
                            "line_number": line_number,
                            "reason": f"json_decode_error:{exc}",
                        }
                    )
                    continue
                qid = _norm_qid(row.get("question_id") or row.get("case_id") or row.get("qid"))
                db = str(row.get("db_id") or "").strip()
                if (db, qid) not in wanted_by_db_qid:
                    continue
                payload = _extract_case_fields(row, path)
                payload = _merge_dev_fields(payload, dev_rows.get((db, qid), {}))
                missing = [
                    field
                    for field in ("question", "pred_sql", "gold_sql")
                    if not str(payload.get(field) or "").strip()
                ]
                if missing:
                    skipped.append(
                        {
                            "case_id": qid,
                            "db": db,
                            "source_log": str(path),
                            "line_number": line_number,
                            "reason": "missing_fields",
                            "missing_fields": missing,
                        }
                    )
                    continue
                cases[qid] = payload
    for qid, meta in TARGET_CASES.items():
        if qid not in cases:
            skipped.append(
                {
                    "case_id": qid,
                    "db": meta["db"],
                    "reason": "case_not_found_or_skipped",
                }
            )
    return cases, skipped


def build_prompt(case: Mapping[str, Any]) -> str:
    return PROMPT_TEMPLATE.format(
        question=case["question"],
        pred_sql=case["pred_sql"],
        gold_sql=case["gold_sql"],
    )


def _strict_parse_llm_json(text: str) -> Dict[str, Any]:
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON is not an object")
    signals = parsed.get("source_signals")
    if not isinstance(signals, list) or not all(isinstance(item, str) for item in signals):
        raise ValueError("source_signals must be a list of strings")
    return parsed


def call_signal_llm(case: Mapping[str, Any], output_dir: Path) -> Dict[str, Any]:
    from method.EEA.rulebook.common.llm.utils import call_llm

    prompt = build_prompt(case)
    qid = str(case["case_id"])
    db = str(case["db"])
    attempts: List[Dict[str, Any]] = []
    parsed: Optional[Dict[str, Any]] = None
    parse_failure = ""
    api_failure = ""
    for attempt in range(1, 3):
        try:
            response = call_llm(
                prompt,
                expect_json=False,
                stage="p0b_llm_signal_probe",
                trace_context={"db": db, "case_id": qid, "attempt": attempt},
            )
            raw_response = str(response)
            attempt_payload: Dict[str, Any] = {
                "attempt": attempt,
                "response": raw_response,
            }
            try:
                parsed = _strict_parse_llm_json(raw_response)
                attempt_payload["parse_ok"] = True
                attempts.append(attempt_payload)
                parse_failure = ""
                api_failure = ""
                break
            except Exception as exc:  # noqa: BLE001 - probe records parser failure.
                parse_failure = f"{type(exc).__name__}: {exc}"
                attempt_payload["parse_ok"] = False
                attempt_payload["parse_error"] = parse_failure
                attempts.append(attempt_payload)
        except Exception as exc:  # noqa: BLE001 - probe records API failure.
            api_failure = f"{type(exc).__name__}: {exc}"
            attempts.append(
                {
                    "attempt": attempt,
                    "api_error": api_failure,
                    "parse_ok": False,
                }
            )
    signals = parsed.get("source_signals", []) if parsed else []
    payload = {
        "db": db,
        "case_id": qid,
        "prompt": prompt,
        "attempts": attempts,
        "parsed": parsed,
        "source_signals": signals,
        "parse_failure": parse_failure if parsed is None and parse_failure else "",
        "api_failure": api_failure if parsed is None and api_failure else "",
    }
    out_path = output_dir / f"{db}_q{qid}_llm.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _count_joins(sql: str) -> int:
    return len(re.findall(r"\bJOIN\b", sql or "", flags=re.IGNORECASE))


def _split_top_level_commas(text: str) -> List[str]:
    parts: List[str] = []
    start = 0
    depth = 0
    quote: Optional[str] = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
            elif ch == "\\" and quote in {"'", '"'}:
                i += 1
            i += 1
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")" and depth > 0:
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
        i += 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _find_keyword_top_level(sql: str, keyword: str, start: int = 0) -> int:
    pattern = re.compile(rf"\b{re.escape(keyword)}\b", flags=re.IGNORECASE)
    depth = 0
    quote: Optional[str] = None
    i = start
    while i < len(sql):
        ch = sql[i]
        if quote:
            if ch == quote:
                quote = None
            elif ch == "\\" and quote in {"'", '"'}:
                i += 1
            i += 1
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")" and depth > 0:
            depth -= 1
            i += 1
            continue
        if depth == 0:
            match = pattern.match(sql, i)
            if match:
                return i
        i += 1
    return -1


def _select_column_count(sql: str) -> int:
    select_idx = _find_keyword_top_level(sql, "SELECT", 0)
    if select_idx < 0:
        match = re.search(r"\bSELECT\b", sql or "", flags=re.IGNORECASE)
        if not match:
            return 0
        select_idx = match.end() - len("SELECT")
    from_idx = _find_keyword_top_level(sql, "FROM", select_idx + len("SELECT"))
    if from_idx < 0:
        match = re.search(r"\bFROM\b", sql[select_idx + len("SELECT") :], flags=re.IGNORECASE)
        if not match:
            return 0
        from_idx = select_idx + len("SELECT") + match.start()
    clause = sql[select_idx + len("SELECT") : from_idx].strip()
    if not clause:
        return 0
    return len(_split_top_level_commas(clause))


def _where_condition_count(sql: str) -> int:
    where_idx = _find_keyword_top_level(sql, "WHERE", 0)
    if where_idx < 0:
        return 0
    end_candidates = [
        idx
        for keyword in ("GROUP", "ORDER", "HAVING", "LIMIT")
        for idx in [_find_keyword_top_level(sql, keyword, where_idx + len("WHERE"))]
        if idx >= 0
    ]
    end = min(end_candidates) if end_candidates else len(sql)
    clause = sql[where_idx + len("WHERE") : end]
    # Lightweight probe heuristic: count top-level AND separators plus one predicate.
    parts = re.split(r"\bAND\b", clause, flags=re.IGNORECASE)
    return len([part for part in parts if part.strip()])


def _count_for_check_type(check_type: str, sql: str) -> Optional[int]:
    if check_type == "s0_join_table_count":
        return _count_joins(sql)
    if check_type == "s0_select_column_count":
        return _select_column_count(sql)
    if check_type == "s0_distinct_count":
        return len(re.findall(r"\bDISTINCT\b", sql or "", flags=re.IGNORECASE))
    if check_type == "s0_count_star_count":
        return len(re.findall(r"\bCOUNT\s*\(\s*\*\s*\)", sql or "", flags=re.IGNORECASE))
    if check_type == "s0_count_distinct_count":
        return len(re.findall(r"\bCOUNT\s*\(\s*DISTINCT\b", sql or "", flags=re.IGNORECASE))
    if check_type == "s0_where_condition_count":
        return _where_condition_count(sql)
    return None


def _apply_count_expr(expr: str, count: int) -> bool | str:
    match = re.fullmatch(r"\s*(>=|<=|==)\s*(\d+)\s*", expr or "")
    if not match:
        return "invalid_count_expression"
    op, raw_n = match.groups()
    n = int(raw_n)
    if op == ">=":
        return count >= n
    if op == "<=":
        return count <= n
    return count == n


def verify_signal(signal_str: str, sql: str) -> bool | str:
    if not isinstance(signal_str, str) or ":" not in signal_str:
        return "invalid_signal_format"
    check_type, expr = signal_str.split(":", 1)
    check_type = check_type.strip()
    expr = expr.strip()
    if not check_type or not expr:
        return "invalid_signal_format"
    if check_type == "s0_contains_token":
        return sql.lower().find(expr.lower()) >= 0
    if check_type == "s0_not_contains_token":
        return sql.lower().find(expr.lower()) < 0
    if check_type == "s0_regex_match":
        try:
            return bool(re.search(expr, sql, flags=re.IGNORECASE))
        except re.error as exc:
            return f"invalid_regex:{exc}"
    if check_type.endswith("_count"):
        count = _count_for_check_type(check_type, sql)
        if count is None:
            return "unsupported_check_type"
        return _apply_count_expr(expr, count)
    return "unsupported_check_type"


def _signal_check_type(signal: str) -> str:
    if not isinstance(signal, str) or ":" not in signal:
        return "<unparseable>"
    return signal.split(":", 1)[0].strip() or "<unparseable>"


def _signal_format_ok(signal: str) -> bool:
    if not isinstance(signal, str) or ":" not in signal:
        return False
    check_type, expr = signal.split(":", 1)
    check_type = check_type.strip()
    expr = expr.strip()
    if not re.fullmatch(r"[a-zA-Z0-9_]+", check_type) or not expr:
        return False
    if check_type == "s0_regex_match":
        try:
            re.compile(expr, flags=re.IGNORECASE)
        except re.error:
            return False
    if check_type.endswith("_count"):
        return re.fullmatch(r"\s*(>=|<=|==)\s*\d+\s*", expr) is not None
    return True


def _signal_supported(signal: str) -> bool:
    return _signal_check_type(signal) in SUPPORTED_CHECK_TYPES


def _boolish(value: bool | str) -> str:
    if value is True:
        return "pass"
    if value is False:
        return "fail"
    return str(value)


def run_sanity(
    seed_payloads: Mapping[str, Mapping[str, Any]],
    cases: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for qid in SEED_QIDS:
        if qid not in seed_payloads or qid not in cases:
            continue
        for signal in seed_payloads[qid].get("source_signals") or []:
            result = verify_signal(signal, str(cases[qid]["pred_sql"]))
            rows.append(
                {
                    "seed": qid,
                    "db": cases[qid]["db"],
                    "signal": signal,
                    "result": result,
                }
            )
    return rows


def run_cross_checks(
    seed_payloads: Mapping[str, Mapping[str, Any]],
    cases: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for seed, target, expected in CROSS_CHECKS:
        if seed not in seed_payloads or seed not in cases or target not in cases:
            rows.append(
                {
                    "seed": seed,
                    "target": target,
                    "expected": expected,
                    "actual": None,
                    "per_signal": [],
                    "status": "skipped_missing_data",
                }
            )
            continue
        signals = list(seed_payloads[seed].get("source_signals") or [])
        per_signal = [
            {
                "signal": signal,
                "result": verify_signal(signal, str(cases[target]["pred_sql"])),
            }
            for signal in signals
        ]
        actual = bool(per_signal) and all(item["result"] is True for item in per_signal)
        rows.append(
            {
                "seed": seed,
                "target": target,
                "expected": expected,
                "actual": actual,
                "per_signal": per_signal,
                "status": "ok" if actual == expected else "mismatch",
            }
        )
    return rows


def quality_metrics(seed_payloads: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    signals_by_seed = {
        qid: list(seed_payloads.get(qid, {}).get("source_signals") or [])
        for qid in SEED_QIDS
    }
    all_signals = [signal for signals in signals_by_seed.values() for signal in signals]
    compliant = [signal for signal in all_signals if _signal_format_ok(signal)]
    supported = [signal for signal in all_signals if _signal_supported(signal)]
    left = set(signals_by_seed.get("1172") or [])
    right = set(signals_by_seed.get("1257") or [])
    union = left | right
    return {
        "total_signals": len(all_signals),
        "format_compliant_signals": len(compliant),
        "format_compliance_rate": (len(compliant) / len(all_signals)) if all_signals else 0.0,
        "supported_signals": len(supported),
        "supported_signal_rate": (len(supported) / len(all_signals)) if all_signals else 0.0,
        "unsupported_check_type_distribution": dict(
            Counter(
                _signal_check_type(signal)
                for signal in all_signals
                if not _signal_supported(signal)
            )
        ),
        "avg_signals_per_seed": (
            sum(len(signals) for signals in signals_by_seed.values()) / len(SEED_QIDS)
        ),
        "thrombosis_seed_overlap": (len(left & right) / len(union)) if union else 0.0,
        "check_type_distribution": dict(Counter(_signal_check_type(signal) for signal in all_signals)),
        "signals_per_seed": {qid: len(signals) for qid, signals in signals_by_seed.items()},
    }


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _md_escape(value: Any, limit: int = 240) -> str:
    text = str(value)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text.replace("|", "\\|")


def build_report(
    cases: Mapping[str, Mapping[str, Any]],
    skipped: List[Mapping[str, Any]],
    seed_payloads: Mapping[str, Mapping[str, Any]],
    sanity_rows: List[Mapping[str, Any]],
    cross_rows: List[Mapping[str, Any]],
    metrics: Mapping[str, Any],
    flags: Mapping[str, Any],
) -> str:
    source_dump = {
        f"{cases[qid]['db']} q{qid}" if qid in cases else f"q{qid}": seed_payloads.get(qid, {}).get(
            "source_signals", []
        )
        for qid in SEED_QIDS
    }
    lines: List[str] = [
        "## §P0b-llm-signal-probe",
        "",
        "Dry-run probe for whether admission-time LLM output can provide machine-verifiable `source_signals`. The probe only writes this script, workspace trace JSON, and this report section.",
        "",
        "### Source Signals",
        "",
        "```json",
        _json_dump(source_dump),
        "```",
        "",
        "### Sanity Check",
        "",
        "| Seed | Signal | Result |",
        "|---|---|---|",
    ]
    if sanity_rows:
        for row in sanity_rows:
            lines.append(
                f"| {row['db']} q{row['seed']} | `{_md_escape(row['signal'])}` | {_boolish(row['result'])} |"
            )
    else:
        lines.append("| `<none>` |  | skipped |")

    lines.extend(
        [
            "",
            "### Cross-Check Matrix",
            "",
            "| Seed | Target | Expected | Actual | Per-signal results | Status |",
            "|---|---|---:|---:|---|---|",
        ]
    )
    for row in cross_rows:
        per_signal = "; ".join(
            f"{_md_escape(item['signal'], 120)} => {_boolish(item['result'])}"
            for item in row.get("per_signal", [])
        )
        if not per_signal:
            per_signal = "<none>"
        lines.append(
            f"| q{row['seed']} | q{row['target']} | {row['expected']} | {row['actual']} | {per_signal} | {row['status']} |"
        )

    lines.extend(
        [
            "",
            "### Quality Metrics",
            "",
            "```json",
            _json_dump(metrics),
            "```",
        ]
    )
    if skipped:
        lines.extend(
            [
                "",
                "### Skipped / Missing Data",
                "",
                "```json",
                _json_dump(skipped),
                "```",
            ]
        )
    if flags:
        lines.extend(
            [
                "",
                "### Flags",
                "",
                "```json",
                _json_dump(flags),
                "```",
            ]
        )

    rate = float(metrics.get("format_compliance_rate") or 0.0)
    student_blocked = bool(flags.get("student_club_q1418_blocks_q1422"))
    thrombosis_failed = bool(flags.get("thrombosis_both_seed_sets_fail_to_block_all_regressions"))
    if rate < 0.70:
        conclusion = (
            "Conclusion: prompt design needs adjustment before P0b implementation, because fewer than 70% of emitted signals were supported and parseable. "
            "The current dry run is insufficient as implementation evidence even if some individual checks happen to pass."
        )
    elif student_blocked and thrombosis_failed:
        conclusion = (
            "Conclusion: LLM-written signals are machine-parseable but not safe evidence for P0b. "
            "They failed the negative-selectivity check on thrombosis regressions and overfit the student_club seed SQL enough to block the intended q1418 -> q1422 transfer."
        )
    elif student_blocked:
        conclusion = (
            "Conclusion: LLM-written signals are not yet safe evidence for P0b, because the student_club transfer case was blocked even though it should pass. "
            "The prompt needs a tighter notion of source-shape generality before these signals drive runtime branch selection."
        )
    elif thrombosis_failed:
        conclusion = (
            "Conclusion: LLM-written signals are format-feasible, but this run did not show enough negative selectivity on the thrombosis regression targets. "
            "P0b should not rely on these signals without an additional specificity constraint or reviewer gate."
        )
    else:
        conclusion = (
            "Conclusion: LLM-written signals look feasible for a P0b implementation dry run: they were machine-parseable, self-applicable, and matched the positive transfer checks while blocking the tested negative thrombosis cases. "
            "The next implementation should still treat unsupported or over-specific check types as hard admission failures."
        )
    lines.extend(["", conclusion, ""])
    return "\n".join(lines)


def _replace_or_append_section(existing: str, section: str) -> str:
    marker = "## §P0b-llm-signal-probe"
    if marker not in existing:
        prefix = existing.rstrip() + "\n\n" if existing.strip() else ""
        return prefix + section.rstrip() + "\n"
    before, rest = existing.split(marker, 1)
    next_match = re.search(r"\n## ", rest)
    if next_match:
        after = rest[next_match.start() :]
    else:
        after = ""
    return before.rstrip() + "\n\n" + section.rstrip() + "\n" + after


def write_report(report_path: Path, section: str) -> None:
    existing = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_replace_or_append_section(existing, section), encoding="utf-8")


def compute_flags(cross_rows: List[Mapping[str, Any]], metrics: Mapping[str, Any]) -> Dict[str, Any]:
    flags: Dict[str, Any] = {}
    if float(metrics.get("format_compliance_rate") or 0.0) < 0.70:
        flags["halt"] = "prompt design needs adjustment"
    thrombosis_targets = {"1267", "1278", "1280"}
    by_seed = {
        seed: [
            row
            for row in cross_rows
            if row.get("seed") == seed and row.get("target") in thrombosis_targets
        ]
        for seed in ("1172", "1257")
    }
    if all(
        rows and all(row.get("actual") is True for row in rows)
        for rows in by_seed.values()
    ):
        flags["thrombosis_both_seed_sets_fail_to_block_all_regressions"] = True
    student_row = next(
        (
            row
            for row in cross_rows
            if row.get("seed") == "1418" and row.get("target") == "1422"
        ),
        None,
    )
    if student_row and student_row.get("actual") is False:
        flags["student_club_q1418_blocks_q1422"] = True
    return flags


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log_glob", default=DEFAULT_LOG_GLOB)
    parser.add_argument("--dev_json", type=Path, default=DEFAULT_DEV_JSON)
    parser.add_argument("--output_dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--report_path", type=Path, default=DEFAULT_SCRIPT_REPORT)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    cases, skipped = load_case_data(args.log_glob, dev_json=args.dev_json)
    (args.output_dir / "case_data.json").write_text(
        json.dumps({"cases": cases, "skipped": skipped}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[p0b] loaded {len(cases)}/{len(TARGET_CASES)} target cases")
    if skipped:
        print(f"[p0b] skipped/missing entries: {len(skipped)}")

    seed_payloads: Dict[str, Dict[str, Any]] = {}
    for qid in SEED_QIDS:
        if qid not in cases:
            print(f"[p0b] skip q{qid}: missing case data")
            continue
        print(f"[p0b] calling LLM for {cases[qid]['db']} q{qid}")
        seed_payloads[qid] = call_signal_llm(cases[qid], args.output_dir)
        if seed_payloads[qid].get("api_failure"):
            print(f"[p0b] q{qid} api_failure={seed_payloads[qid]['api_failure']}")
        elif seed_payloads[qid].get("parse_failure"):
            print(f"[p0b] q{qid} parse_failure={seed_payloads[qid]['parse_failure']}")
        else:
            print(f"[p0b] q{qid} signals={len(seed_payloads[qid].get('source_signals') or [])}")

    sanity_rows = run_sanity(seed_payloads, cases)
    cross_rows = run_cross_checks(seed_payloads, cases)
    metrics = quality_metrics(seed_payloads)
    flags = compute_flags(cross_rows, metrics)

    summary = {
        "loaded_case_count": len(cases),
        "target_case_count": len(TARGET_CASES),
        "skipped": skipped,
        "seed_payloads": {
            qid: {
                "db": payload.get("db"),
                "case_id": payload.get("case_id"),
                "source_signals": payload.get("source_signals"),
                "parse_failure": payload.get("parse_failure"),
                "api_failure": payload.get("api_failure"),
            }
            for qid, payload in seed_payloads.items()
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
    section = build_report(cases, skipped, seed_payloads, sanity_rows, cross_rows, metrics, flags)
    write_report(args.report_path, section)
    print(f"[p0b] format_compliance_rate={metrics['format_compliance_rate']:.3f}")
    print(f"[p0b] avg_signals_per_seed={metrics['avg_signals_per_seed']:.2f}")
    if flags:
        print(f"[p0b] flags={json.dumps(flags, ensure_ascii=False, sort_keys=True)}")
    print(f"[p0b] wrote report section to {args.report_path}")
    print(f"[p0b] wrote JSON traces to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
