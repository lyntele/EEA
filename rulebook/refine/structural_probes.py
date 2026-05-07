from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import sqlglot
from sqlglot import exp

from method.EEA.rulebook.refine.sql_parser import parse_sql_structure
import sqlite3

from method.EEA.rulebook.refine.utils import execute_and_summarize


@dataclass
class JoinStepTrace:
    counts: List[Optional[int]]
    ratios: List[Optional[float]]
    divergence_k: Optional[int]
    divergence_ratio: Optional[float]


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_scalar_from_summary(summary) -> Optional[int]:
    if summary.result_type not in {"success", "empty_result"}:
        return None
    if not summary.sample_rows:
        return 0
    first_row = summary.sample_rows[0]
    if not first_row:
        return 0
    return _safe_int(first_row[0])


def _execute_count(db_path: str, sql: str, timeout: int) -> Optional[int]:
    if not sql:
        return None
    summary = execute_and_summarize(db_path, sql, timeout=timeout)
    return _extract_scalar_from_summary(summary)


def _split_conditions(expr: exp.Expression) -> List[str]:
    if isinstance(expr, exp.And):
        return _split_conditions(expr.left) + _split_conditions(expr.right)
    return [str(expr)]


def _extract_alias_map(parsed: exp.Expression) -> Dict[str, str]:
    alias_map: Dict[str, str] = {}
    for table in parsed.find_all(exp.Table):
        alias = table.alias_or_name
        alias_map[alias] = table.name
    return alias_map


def _build_count_sql(
    from_clause: Optional[exp.From],
    joins: List[exp.Join],
    where_clause: Optional[exp.Where],
    dialect: str,
) -> str:
    if not from_clause:
        return ""
    sql = "SELECT COUNT(*) AS cnt"
    sql += f" {from_clause.sql(dialect=dialect)}"
    for join in joins:
        sql += f" {join.sql(dialect=dialect)}"
    if where_clause:
        sql += f" {where_clause.sql(dialect=dialect)}"
    return sql


def table_set_probe(pred_sql: str, gold_sql: str) -> Dict[str, Any]:
    pred_struct = parse_sql_structure(pred_sql)
    gold_struct = parse_sql_structure(gold_sql)
    pred_tables = sorted(set(pred_struct.tables))
    gold_tables = sorted(set(gold_struct.tables))
    diff = {
        "pred_only": sorted(set(pred_tables) - set(gold_tables)),
        "gold_only": sorted(set(gold_tables) - set(pred_tables)),
    }
    return {
        "pred_tables": pred_tables,
        "gold_tables": gold_tables,
        "diff": diff,
    }


def join_graph_probe(pred_sql: str, gold_sql: str) -> Dict[str, Any]:
    pred_struct = parse_sql_structure(pred_sql)
    gold_struct = parse_sql_structure(gold_sql)
    pred_joins = [
        (j.table, j.join_type, j.on_condition) for j in pred_struct.joins
    ]
    gold_joins = [
        (j.table, j.join_type, j.on_condition) for j in gold_struct.joins
    ]
    pred_set = set(pred_joins)
    gold_set = set(gold_joins)
    diff = {
        "pred_only": sorted(pred_set - gold_set),
        "gold_only": sorted(gold_set - pred_set),
    }
    return {
        "pred_joins": pred_joins,
        "gold_joins": gold_joins,
        "diff": diff,
    }


def join_step_cardinality_trace(
    db_path: str,
    sql: str,
    timeout: int = 30,
    dialect: str = "sqlite",
) -> JoinStepTrace:
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return JoinStepTrace(counts=[], ratios=[], divergence_k=None, divergence_ratio=None)

    from_clause = parsed.find(exp.From)
    joins = list(parsed.find_all(exp.Join))
    counts: List[Optional[int]] = []
    ratios: List[Optional[float]] = []

    base_sql = _build_count_sql(from_clause, [], None, dialect)
    base_count = _execute_count(db_path, base_sql, timeout)
    counts.append(base_count)

    prev_count = base_count
    for idx in range(len(joins)):
        step_sql = _build_count_sql(from_clause, joins[: idx + 1], None, dialect)
        step_count = _execute_count(db_path, step_sql, timeout)
        counts.append(step_count)
        if prev_count and step_count is not None and prev_count != 0:
            ratios.append(step_count / prev_count)
        else:
            ratios.append(None)
        prev_count = step_count

    return JoinStepTrace(counts=counts, ratios=ratios, divergence_k=None, divergence_ratio=None)


def predicate_selectivity_trace(
    db_path: str,
    sql: str,
    timeout: int = 30,
    dialect: str = "sqlite",
) -> List[Dict[str, Any]]:
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return []

    from_clause = parsed.find(exp.From)
    joins = list(parsed.find_all(exp.Join))
    where_clause = parsed.find(exp.Where)
    if not from_clause or not where_clause:
        return []

    base_sql = _build_count_sql(from_clause, joins, None, dialect)
    base_count = _execute_count(db_path, base_sql, timeout)
    if not base_count:
        return []

    predicates = _split_conditions(where_clause.this)
    results = []
    for predicate in predicates:
        where = exp.Where(this=sqlglot.parse_one(predicate, dialect=dialect))
        pred_sql = _build_count_sql(from_clause, joins, where, dialect)
        pred_count = _execute_count(db_path, pred_sql, timeout)
        ratio = (pred_count / base_count) if pred_count is not None else None
        results.append({
            "predicate": predicate,
            "count": pred_count,
            "ratio": ratio,
        })
    return results


def key_coverage_probe(
    db_path: str,
    sql: str,
    timeout: int = 30,
    dialect: str = "sqlite",
) -> List[Dict[str, Any]]:
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return []

    alias_map = _extract_alias_map(parsed)
    results: List[Dict[str, Any]] = []
    for join in parsed.find_all(exp.Join):
        on_expr = join.args.get("on")
        if not isinstance(on_expr, exp.EQ):
            continue
        left = on_expr.left
        right = on_expr.right
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            continue
        left_table = alias_map.get(left.table or "", left.table or "")
        right_table = alias_map.get(right.table or "", right.table or "")
        if not left_table or not right_table:
            continue
        left_col = left.name
        right_col = right.name
        left_total_sql = f"SELECT COUNT(DISTINCT `{left_col}`) FROM `{left_table}`"
        right_total_sql = f"SELECT COUNT(DISTINCT `{right_col}`) FROM `{right_table}`"
        left_in_right_sql = (
            f"SELECT COUNT(DISTINCT `{left_col}`) FROM `{left_table}` "
            f"WHERE `{left_col}` IN (SELECT `{right_col}` FROM `{right_table}`)"
        )
        right_in_left_sql = (
            f"SELECT COUNT(DISTINCT `{right_col}`) FROM `{right_table}` "
            f"WHERE `{right_col}` IN (SELECT `{left_col}` FROM `{left_table}`)"
        )
        left_total = _execute_count(db_path, left_total_sql, timeout)
        right_total = _execute_count(db_path, right_total_sql, timeout)
        left_in_right = _execute_count(db_path, left_in_right_sql, timeout)
        right_in_left = _execute_count(db_path, right_in_left_sql, timeout)
        left_ratio = (left_in_right / left_total) if left_total and left_in_right is not None else None
        right_ratio = (right_in_left / right_total) if right_total and right_in_left is not None else None
        results.append({
            "join_on": str(on_expr),
            "left": f"{left_table}.{left_col}",
            "right": f"{right_table}.{right_col}",
            "left_ratio": left_ratio,
            "right_ratio": right_ratio,
        })
    return results


def fanout_probe(
    db_path: str,
    sql: str,
    timeout: int = 30,
    dialect: str = "sqlite",
) -> Optional[float]:
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return None
    from_clause = parsed.find(exp.From)
    joins = list(parsed.find_all(exp.Join))
    where_clause = parsed.find(exp.Where)
    if not from_clause:
        return None
    base_sql = _build_count_sql(from_clause, joins, where_clause, dialect)
    base_count = _execute_count(db_path, base_sql, timeout)
    if not base_count:
        return None
    first_table = None
    for table in parsed.find_all(exp.Table):
        first_table = table.name
        break
    if not first_table:
        return None
    pk_columns = []
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            cursor = conn.cursor()
            cursor.execute(f"PRAGMA table_info(`{first_table}`);")
            for row in cursor.fetchall():
                if len(row) > 5 and row[5]:
                    pk_columns.append(row[1])
    except Exception:
        pk_columns = []
    if not pk_columns:
        return None
    pk_col = pk_columns[0]
    distinct_sql = _build_count_sql(from_clause, joins, where_clause, dialect).replace(
        "COUNT(*) AS cnt",
        f"COUNT(DISTINCT `{first_table}`.`{pk_col}`) AS cnt",
    )
    distinct_count = _execute_count(db_path, distinct_sql, timeout)
    if not distinct_count:
        return None
    return base_count / distinct_count


def run_structural_probes(
    db_path: str,
    pred_sql: str,
    gold_sql: str,
    timeout: int = 30,
    dialect: str = "sqlite",
) -> Dict[str, Any]:
    pred_trace = join_step_cardinality_trace(db_path, pred_sql, timeout, dialect)
    gold_trace = join_step_cardinality_trace(db_path, gold_sql, timeout, dialect)
    divergence_k = None
    divergence_ratio = None
    if pred_trace.ratios and gold_trace.ratios:
        max_idx = None
        max_gap = 0.0
        for idx, (pred_ratio, gold_ratio) in enumerate(zip(pred_trace.ratios, gold_trace.ratios)):
            if pred_ratio is None or gold_ratio is None or gold_ratio == 0:
                continue
            gap = abs(pred_ratio - gold_ratio) / abs(gold_ratio)
            if gap > max_gap:
                max_gap = gap
                max_idx = idx
                divergence_ratio = pred_ratio / gold_ratio
        divergence_k = max_idx
    return {
        "table_set": table_set_probe(pred_sql, gold_sql),
        "join_graph": join_graph_probe(pred_sql, gold_sql),
        "join_step_trace": {
            "pred_counts": pred_trace.counts,
            "gold_counts": gold_trace.counts,
            "pred_ratios": pred_trace.ratios,
            "gold_ratios": gold_trace.ratios,
            "divergence_k": divergence_k,
            "divergence_ratio": divergence_ratio,
        },
        "predicate_selectivity": {
            "pred": predicate_selectivity_trace(db_path, pred_sql, timeout, dialect),
            "gold": predicate_selectivity_trace(db_path, gold_sql, timeout, dialect),
        },
        "key_coverage": {
            "pred": key_coverage_probe(db_path, pred_sql, timeout, dialect),
            "gold": key_coverage_probe(db_path, gold_sql, timeout, dialect),
        },
        "fanout": {
            "pred": fanout_probe(db_path, pred_sql, timeout, dialect),
            "gold": fanout_probe(db_path, gold_sql, timeout, dialect),
        },
    }
