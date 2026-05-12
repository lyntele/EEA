#!/usr/bin/env python3
"""Dry-run probe v2 for group-level LLM-authored source_signals.

This probe does not modify runtime/admission/schema code. It asks the LLM to
write shared source_signals for a whole pattern seed group, then mechanically
checks whether those signals separate known positive/negative anchor cases.
"""

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

DEFAULT_RUN_ROOT = Path(
    "/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL/workspace/rulebook_runs"
)
DEFAULT_DEV_JSON = Path("/data/liuyining/ace4sql/bench/bird/dev/dev.json")
DEFAULT_OUTPUT_DIR = RULEBOOK_ROOT / "workspace" / "probes" / "p0b_llm_signal_probe_v2"
DEFAULT_REPORT = RULEBOOK_ROOT / "doc" / "r_v2_e_phase2_acceptance.md"
RUN_SUFFIX = "postsel_v1_qwen3coderflash_20260510_164346"

GROUPS: Dict[str, Dict[str, Any]] = {
    "thrombosis_distinct_count_subject": {
        "db": "thrombosis_prediction",
        "seed_qids": ["1172", "1257"],
        "cross": [("1267", False), ("1278", False), ("1280", False), ("1308", False)],
    },
    "studentclub_event_type_lookup": {
        "db": "student_club",
        "seed_qids": ["1418"],
        "cross": [("1422", True)],
    },
    "toxicology_drop_extra_role_side": {
        "db": "toxicology",
        "seed_qids": ["268", "277", "307"],
        "cross": [("268", True), ("277", True), ("307", True)],
    },
}

TARGET_CASES: Dict[str, str] = {
    "1172": "thrombosis_prediction",
    "1257": "thrombosis_prediction",
    "1267": "thrombosis_prediction",
    "1278": "thrombosis_prediction",
    "1280": "thrombosis_prediction",
    "1308": "thrombosis_prediction",
    "1418": "student_club",
    "1422": "student_club",
    "268": "toxicology",
    "277": "toxicology",
    "307": "toxicology",
}

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

PROMPT_TEMPLATE = """You are analyzing a NL2SQL repair pattern.

Below are seed cases that belong to the SAME repair pattern. Your task is to write source_signals that are SHARED by this seed group in S0. A future case must satisfy these source_signals before it can reuse this group's repair hint.

Each source_signal must be machine-verifiable against a future S0 SQL string.

STRICT OUTPUT FORMAT:
1. Output ONLY valid JSON: {{"source_signals": ["<check_type>:<expression>", ...]}}
2. Each signal must be exactly <check_type>:<expression>.
3. Write at most 6 signals. Prefer 2-4 broad but discriminative signals.
4. Allowed examples, but you may invent an equally mechanical check_type:
   - s0_contains_token:COUNT(*)
   - s0_not_contains_token:DISTINCT
   - s0_regex_match:\\bCOUNT\\s*\\(\\s*\\*\\s*\\)
   - s0_join_table_count:>=2
   - s0_select_column_count:==1

DO NOT write case-specific signals:
- Do NOT include single-quoted literal values such as 'MU 215', '+-', or 'TR000_1_2'.
- Do NOT include a full SELECT, WHERE, or JOIN clause as a substring.
- Do NOT include table/column aliases such as a1, a2, T1, l, p, e, or c.
- Do NOT write natural-language explanations.

Seed group:
{seed_cases_json}

Write shared source_signals for this seed group."""


def _norm_qid(value: Any) -> str:
    text = str(value or "").strip()
    return text[1:] if text.lower().startswith("q") else text


def _first_non_empty(mapping: Mapping[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


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
        if qid and db:
            out[(db, qid)] = row
    return out


def _extract_case(row: Mapping[str, Any], path: Path, dev_row: Mapping[str, Any]) -> Dict[str, Any]:
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    selection = row.get("selection") if isinstance(row.get("selection"), dict) else {}
    qid = _norm_qid(row.get("question_id") or row.get("case_id") or row.get("qid"))
    db = str(row.get("db_id") or "").strip()
    pred_sql = _first_non_empty(selection, ("rewrite_only_selected_sql",))
    if not pred_sql:
        pred_sql = _first_non_empty(
            result,
            ("s0_sql", "baseline_sql", "generation_top1_sql", "pred_sql", "predicted_sql"),
        )
    question = _first_non_empty(row, ("question", "nl", "query"))
    if not question:
        question = str(dev_row.get("question") or "")
    evidence = str(row.get("evidence") or dev_row.get("evidence") or "")
    gold_sql = _first_non_empty(result, ("gold_sql", "gold", "ground_truth_sql"))
    if not gold_sql:
        gold_sql = str(dev_row.get("SQL") or "")
    return {
        "case_id": qid,
        "db": db,
        "question": question,
        "evidence": evidence,
        "pred_sql": pred_sql,
        "gold_sql": gold_sql,
        "baseline_correct": result.get("baseline_correct", row.get("baseline_correct")),
        "enhanced_correct": result.get("enhanced_correct", row.get("enhanced_correct")),
        "source_log": str(path),
        "pred_sql_source": "selection.rewrite_only_selected_sql" if selection.get("rewrite_only_selected_sql") else "result.s0_sql_or_fallback",
    }


def load_cases(run_root: Path, dev_json: Path) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    dev_rows = _load_dev_rows(dev_json)
    cases: Dict[str, Dict[str, Any]] = {}
    skipped: List[Dict[str, Any]] = []
    dbs = sorted(set(TARGET_CASES.values()))
    for db in dbs:
        pattern = str(run_root / f"full11_{db}_{RUN_SUFFIX}" / "per_case_log.jsonl")
        for raw_path in sorted(glob.glob(pattern)):
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
                    if TARGET_CASES.get(qid) != db:
                        continue
                    payload = _extract_case(row, path, dev_rows.get((db, qid), {}))
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
    for qid, db in TARGET_CASES.items():
        if qid not in cases:
            skipped.append({"case_id": qid, "db": db, "reason": "case_not_found_or_skipped"})
    return cases, skipped


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
        return 0
    from_idx = _find_keyword_top_level(sql, "FROM", select_idx + len("SELECT"))
    if from_idx < 0:
        return 0
    clause = sql[select_idx + len("SELECT") : from_idx].strip()
    return len(_split_top_level_commas(clause)) if clause else 0


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
    return len([part for part in re.split(r"\bAND\b", clause, flags=re.IGNORECASE) if part.strip()])


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
        return expr.lower() in sql.lower()
    if check_type == "s0_not_contains_token":
        return expr.lower() not in sql.lower()
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


def _strict_parse_llm_json(text: str) -> Dict[str, Any]:
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON is not an object")
    signals = parsed.get("source_signals")
    if not isinstance(signals, list) or not all(isinstance(item, str) for item in signals):
        raise ValueError("source_signals must be a list of strings")
    return parsed


def _check_type(signal: str) -> str:
    if not isinstance(signal, str) or ":" not in signal:
        return "<unparseable>"
    return signal.split(":", 1)[0].strip() or "<unparseable>"


def _signal_format_ok(signal: str) -> bool:
    if not isinstance(signal, str) or ":" not in signal:
        return False
    check_type, expr = signal.split(":", 1)
    check_type = check_type.strip()
    expr = expr.strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+", check_type) or not expr:
        return False
    if check_type == "s0_regex_match":
        try:
            re.compile(expr, flags=re.IGNORECASE)
        except re.error:
            return False
    if check_type.endswith("_count"):
        return re.fullmatch(r"\s*(>=|<=|==)\s*\d+\s*", expr) is not None
    return True


def _case_specific_filter_reason(signal: str) -> str:
    if not isinstance(signal, str) or ":" not in signal:
        return "invalid_signal_format"
    check_type, expr = signal.split(":", 1)
    expr = expr.strip()
    if re.search(r"'[^']*'", expr):
        return "signal_filtered_case_specific:single_quoted_literal"
    if check_type.strip() in {"s0_contains_token", "s0_not_contains_token"}:
        upper = expr.upper()
        if upper.startswith("SELECT ") and (" FROM " in upper or " WHERE " in upper):
            return "signal_filtered_case_specific:full_select_clause"
        if upper.startswith("WHERE ") or re.search(r"\bWHERE\b.+", upper):
            return "signal_filtered_case_specific:where_clause_substring"
        if re.search(r"\bJOIN\b.+\bON\b", upper):
            return "signal_filtered_case_specific:join_clause_substring"
    alias_patterns = [
        r"\b(?:a\d+|t\d+)\b",
        r"\b(?:l|p|e|c)\s*\.",
        r"\bAS\s+(?:a\d+|t\d+|l|p|e|c)\b",
    ]
    for pattern in alias_patterns:
        if re.search(pattern, expr, flags=re.IGNORECASE):
            return "signal_filtered_case_specific:alias_literal"
    return ""


def filter_signals(raw_signals: Iterable[str]) -> Tuple[List[str], List[Dict[str, Any]]]:
    kept: List[str] = []
    filtered: List[Dict[str, Any]] = []
    for signal in raw_signals:
        reason = _case_specific_filter_reason(signal)
        if reason:
            filtered.append({"signal": signal, "reason": reason})
        else:
            kept.append(signal)
    if len(kept) > 6:
        for signal in kept[6:]:
            filtered.append(
                {
                    "signal": signal,
                    "reason": "signal_filtered_case_specific:too_many_signals",
                }
            )
        kept = kept[:6]
    return kept, filtered


def build_prompt(group_id: str, group: Mapping[str, Any], cases: Mapping[str, Mapping[str, Any]]) -> str:
    seed_cases = []
    for qid in group["seed_qids"]:
        case = cases[qid]
        seed_cases.append(
            {
                "case_id": qid,
                "db": case["db"],
                "question": case["question"],
                "evidence": case.get("evidence") or "",
                "wrong_sql_s0": case["pred_sql"],
                "gold_sql": case["gold_sql"],
            }
        )
    return PROMPT_TEMPLATE.format(
        seed_cases_json=json.dumps(
            {"group_id": group_id, "seed_cases": seed_cases},
            ensure_ascii=False,
            indent=2,
        )
    )


def call_group_llm(
    group_id: str,
    group: Mapping[str, Any],
    cases: Mapping[str, Mapping[str, Any]],
    output_dir: Path,
) -> Dict[str, Any]:
    from method.EEA.rulebook.common.llm.utils import call_llm

    prompt = build_prompt(group_id, group, cases)
    attempts: List[Dict[str, Any]] = []
    parsed: Optional[Dict[str, Any]] = None
    parse_failure = ""
    api_failure = ""
    for attempt in range(1, 4):
        try:
            response = call_llm(
                prompt,
                expect_json=False,
                stage="p0b_llm_signal_probe_v2",
                trace_context={"group_id": group_id, "attempt": attempt},
            )
            raw_response = str(response)
            attempt_payload: Dict[str, Any] = {"attempt": attempt, "response": raw_response}
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
            attempts.append({"attempt": attempt, "api_error": api_failure, "parse_ok": False})
    raw_signals = parsed.get("source_signals", []) if parsed else []
    kept_signals, filtered = filter_signals(raw_signals)
    payload = {
        "group_id": group_id,
        "db": group["db"],
        "seed_qids": list(group["seed_qids"]),
        "prompt": prompt,
        "attempts": attempts,
        "parsed": parsed,
        "raw_source_signals": raw_signals,
        "source_signals": kept_signals,
        "filtered_signals": filtered,
        "parse_failure": parse_failure if parsed is None and parse_failure else "",
        "api_failure": api_failure if parsed is None and api_failure else "",
    }
    (output_dir / f"{group_id}_llm.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def evaluate_groups(
    group_payloads: Mapping[str, Mapping[str, Any]],
    cases: Mapping[str, Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    sanity_rows: List[Dict[str, Any]] = []
    cross_rows: List[Dict[str, Any]] = []
    for group_id, group in GROUPS.items():
        signals = list(group_payloads.get(group_id, {}).get("source_signals") or [])
        for qid in group["seed_qids"]:
            per_signal = [
                {"signal": signal, "result": verify_signal(signal, cases[qid]["pred_sql"])}
                for signal in signals
            ]
            actual = bool(per_signal) and all(item["result"] is True for item in per_signal)
            sanity_rows.append(
                {
                    "group_id": group_id,
                    "case_id": qid,
                    "expected": True,
                    "actual": actual,
                    "per_signal": per_signal,
                    "status": "ok" if actual is True else "mismatch",
                }
            )
        for qid, expected in group["cross"]:
            per_signal = [
                {"signal": signal, "result": verify_signal(signal, cases[qid]["pred_sql"])}
                for signal in signals
            ]
            actual = bool(per_signal) and all(item["result"] is True for item in per_signal)
            cross_rows.append(
                {
                    "group_id": group_id,
                    "case_id": qid,
                    "expected": expected,
                    "actual": actual,
                    "per_signal": per_signal,
                    "status": "ok" if actual == expected else "mismatch",
                }
            )
    return sanity_rows, cross_rows


def quality_metrics(group_payloads: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    raw = [
        signal
        for payload in group_payloads.values()
        for signal in payload.get("raw_source_signals", [])
    ]
    kept = [
        signal
        for payload in group_payloads.values()
        for signal in payload.get("source_signals", [])
    ]
    filtered = [
        item
        for payload in group_payloads.values()
        for item in payload.get("filtered_signals", [])
    ]
    compliant = [signal for signal in raw if _signal_format_ok(signal)]
    supported = [signal for signal in kept if _check_type(signal) in SUPPORTED_CHECK_TYPES]
    return {
        "raw_signal_count": len(raw),
        "kept_signal_count": len(kept),
        "filtered_signal_count": len(filtered),
        "format_compliant_signals": len(compliant),
        "format_compliance_rate": (len(compliant) / len(raw)) if raw else 0.0,
        "supported_kept_signals": len(supported),
        "supported_kept_signal_rate": (len(supported) / len(kept)) if kept else 0.0,
        "avg_kept_signals_per_group": len(kept) / len(GROUPS),
        "check_type_distribution": dict(Counter(_check_type(signal) for signal in kept)),
        "filtered_reason_distribution": dict(Counter(item.get("reason") for item in filtered)),
    }


def compute_flags(
    sanity_rows: List[Mapping[str, Any]],
    cross_rows: List[Mapping[str, Any]],
    metrics: Mapping[str, Any],
) -> Dict[str, Any]:
    flags: Dict[str, Any] = {}
    if float(metrics.get("format_compliance_rate") or 0.0) < 0.70:
        flags["halt"] = "format_compliance_below_70_percent"
    sanity_mismatches = [row for row in sanity_rows if row.get("status") != "ok"]
    if sanity_mismatches:
        flags["halt"] = "seed_self_sanity_failed"
        flags["seed_self_sanity_mismatch_count"] = len(sanity_mismatches)
        flags["seed_self_sanity_mismatch_cases"] = [
            f"{row.get('group_id')}:q{row.get('case_id')}" for row in sanity_mismatches
        ]
    thrombosis = [
        row
        for row in cross_rows
        if row.get("group_id") == "thrombosis_distinct_count_subject"
        and row.get("case_id") in {"1267", "1278", "1280"}
    ]
    blocked = [row for row in thrombosis if row.get("actual") is False]
    flags["thrombosis_block_rate_1267_1278_1280"] = f"{len(blocked)}/{len(thrombosis)}"
    if thrombosis and not blocked:
        flags["halt"] = "thrombosis_block_rate_0_of_3"
    student = next(
        (
            row
            for row in cross_rows
            if row.get("group_id") == "studentclub_event_type_lookup"
            and row.get("case_id") == "1422"
        ),
        None,
    )
    if student and student.get("actual") is False:
        flags["halt"] = "studentclub_q1418_group_blocks_q1422"
        flags["student_club_q1418_blocks_q1422"] = True
    return flags


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _md(value: Any, limit: int = 240) -> str:
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
    cases: Mapping[str, Mapping[str, Any]],
    skipped: List[Mapping[str, Any]],
    group_payloads: Mapping[str, Mapping[str, Any]],
    sanity_rows: List[Mapping[str, Any]],
    cross_rows: List[Mapping[str, Any]],
    metrics: Mapping[str, Any],
    flags: Mapping[str, Any],
) -> str:
    source_dump = {
        group_id: {
            "raw_source_signals": payload.get("raw_source_signals", []),
            "kept_source_signals": payload.get("source_signals", []),
            "filtered_signals": payload.get("filtered_signals", []),
        }
        for group_id, payload in group_payloads.items()
    }
    case_appendix = {
        f"{case['db']} q{qid}": {
            "role": TARGET_CASES.get(qid),
            "question": case.get("question"),
            "pred_sql_source": case.get("pred_sql_source"),
            "pred_sql": case.get("pred_sql"),
            "gold_sql": case.get("gold_sql"),
            "baseline_correct": case.get("baseline_correct"),
            "enhanced_correct": case.get("enhanced_correct"),
        }
        for qid, case in sorted(cases.items(), key=lambda item: int(item[0]))
    }
    lines: List[str] = [
        "## §P0b-llm-signal-probe-v2",
        "",
        "Dry-run v2 probe for group-level LLM-authored `source_signals`. This run uses r1 `selection.rewrite_only_selected_sql` as S0 and filters case-specific signals before verification.",
        "",
        "### Group Source Signals",
        "",
        "```json",
        _json_dump(source_dump),
        "```",
        "",
        "### Seed Sanity",
        "",
        "| Group | Seed | Expected | Actual | Per-signal results | Status |",
        "|---|---|---:|---:|---|---|",
    ]
    for row in sanity_rows:
        per_signal = "; ".join(
            f"{_md(item['signal'], 120)} => {_boolish(item['result'])}"
            for item in row.get("per_signal", [])
        ) or "<none>"
        lines.append(
            f"| {row['group_id']} | q{row['case_id']} | {row['expected']} | {row['actual']} | {per_signal} | {row['status']} |"
        )
    lines.extend(
        [
            "",
            "### Cross-Check Matrix",
            "",
            "| Group | Target | Expected | Actual | Per-signal results | Status |",
            "|---|---|---:|---:|---|---|",
        ]
    )
    for row in cross_rows:
        per_signal = "; ".join(
            f"{_md(item['signal'], 120)} => {_boolish(item['result'])}"
            for item in row.get("per_signal", [])
        ) or "<none>"
        lines.append(
            f"| {row['group_id']} | q{row['case_id']} | {row['expected']} | {row['actual']} | {per_signal} | {row['status']} |"
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
    if skipped:
        lines.extend(["", "### Skipped / Missing Data", "", "```json", _json_dump(skipped), "```"])
    lines.extend(
        [
            "",
            "### Case Data Appendix",
            "",
            "```json",
            _json_dump(case_appendix),
            "```",
        ]
    )

    thrombosis_block_rate = flags.get("thrombosis_block_rate_1267_1278_1280", "n/a")
    student_bad = bool(flags.get("student_club_q1418_blocks_q1422"))
    if flags.get("halt") == "seed_self_sanity_failed":
        conclusion = (
            f"Conclusion: group-level LLM source_signals improved over v1 on the explicit cross checks, "
            f"but are still not sufficient for P0b because some emitted signals fail on their own seed S0. "
            f"thrombosis block rate={thrombosis_block_rate}; student_club transfer blocked={student_bad}."
        )
    elif "halt" in flags:
        conclusion = (
            f"Conclusion: group-level LLM source_signals are not sufficient as-is for P0b. "
            f"thrombosis block rate={thrombosis_block_rate}; student_club transfer blocked={student_bad}."
        )
    else:
        conclusion = (
            f"Conclusion: group-level LLM source_signals are feasible for a P0b implementation probe. "
            f"thrombosis block rate={thrombosis_block_rate}; student_club transfer blocked={student_bad}."
        )
    lines.extend(["", conclusion, ""])
    return "\n".join(lines)


def _replace_or_append_section(existing: str, section: str) -> str:
    marker = "## §P0b-llm-signal-probe-v2"
    if marker not in existing:
        prefix = existing.rstrip() + "\n\n" if existing.strip() else ""
        return prefix + section.rstrip() + "\n"
    before, rest = existing.split(marker, 1)
    next_match = re.search(r"\n## ", rest)
    after = rest[next_match.start() :] if next_match else ""
    return before.rstrip() + "\n\n" + section.rstrip() + "\n" + after


def write_report(report_path: Path, section: str) -> None:
    existing = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_replace_or_append_section(existing, section), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run_root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--dev_json", type=Path, default=DEFAULT_DEV_JSON)
    parser.add_argument("--output_dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--report_path", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    cases, skipped = load_cases(args.run_root, args.dev_json)
    (args.output_dir / "case_data.json").write_text(
        json.dumps({"cases": cases, "skipped": skipped}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[p0b-v2] loaded {len(cases)}/{len(TARGET_CASES)} target cases")
    if skipped:
        print(f"[p0b-v2] skipped/missing entries: {len(skipped)}")

    group_payloads: Dict[str, Dict[str, Any]] = {}
    for group_id, group in GROUPS.items():
        missing = [qid for qid in group["seed_qids"] if qid not in cases]
        if missing:
            print(f"[p0b-v2] skip group {group_id}: missing seeds {missing}")
            continue
        print(f"[p0b-v2] calling LLM for group {group_id}")
        group_payloads[group_id] = call_group_llm(group_id, group, cases, args.output_dir)
        payload = group_payloads[group_id]
        print(
            f"[p0b-v2] {group_id} raw={len(payload.get('raw_source_signals') or [])} "
            f"kept={len(payload.get('source_signals') or [])} "
            f"filtered={len(payload.get('filtered_signals') or [])}"
        )

    sanity_rows, cross_rows = evaluate_groups(group_payloads, cases)
    metrics = quality_metrics(group_payloads)
    flags = compute_flags(sanity_rows, cross_rows, metrics)
    summary = {
        "loaded_case_count": len(cases),
        "target_case_count": len(TARGET_CASES),
        "skipped": skipped,
        "group_payloads": {
            group_id: {
                "db": payload.get("db"),
                "seed_qids": payload.get("seed_qids"),
                "raw_source_signals": payload.get("raw_source_signals"),
                "source_signals": payload.get("source_signals"),
                "filtered_signals": payload.get("filtered_signals"),
                "parse_failure": payload.get("parse_failure"),
                "api_failure": payload.get("api_failure"),
            }
            for group_id, payload in group_payloads.items()
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
    section = build_report(cases, skipped, group_payloads, sanity_rows, cross_rows, metrics, flags)
    write_report(args.report_path, section)
    print(f"[p0b-v2] format_compliance_rate={metrics['format_compliance_rate']:.3f}")
    print(f"[p0b-v2] kept_signals={metrics['kept_signal_count']} filtered={metrics['filtered_signal_count']}")
    if flags:
        print(f"[p0b-v2] flags={json.dumps(flags, ensure_ascii=False, sort_keys=True)}")
    print(f"[p0b-v2] wrote report section to {args.report_path}")
    print(f"[p0b-v2] wrote JSON traces to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
