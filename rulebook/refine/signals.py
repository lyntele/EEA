from __future__ import annotations

"""
Signal extraction utilities for the Rulebook agent.

This module provides the current Rulebook signal stack:
  - SQL AST parsing (`parse_ast_signature`)
  - Fingerprint computation (`compute_fingerprints`)
  - Execution + result comparison (`execute_sql`, `compare_results`)
  - Structural probes (`run_probes`)

The implementation is now self-contained under `rulebook/refine`
and no longer depends on the legacy `method.EEA.teacher` package.
"""

import hashlib
import json
import math
from typing import Any, Dict, List, Optional, Tuple

import sqlglot
from sqlglot import exp

from method.EEA.rulebook.common.schema import (
    ASTSignature,
    ExecutionResult,
    Fingerprints,
    Mismatch,
    SelectShape,
    SignalsDetails,
    SignalsSummary,
)
from method.EEA.rulebook.refine.sql_parser import parse_sql_structure
from method.EEA.rulebook.refine.structural_probes import run_structural_probes
from method.EEA.rulebook.refine.utils import SQLResultSummary, execute_and_summarize


# ---------------------------------------------------------------------------
# 1. SQL execution + comparison
# ---------------------------------------------------------------------------


def _sqlresult_to_execution(result: SQLResultSummary) -> ExecutionResult:
    """Convert teacher-style SQLResultSummary to Rulebook ExecutionResult."""
    # Decide high-level result_type
    if result.result_type == "execution_error":
        result_type = "error"
    elif result.result_type == "timeout":
        result_type = "timeout"
    else:
        # Treat all non-error / non-timeout as regular query results.
        result_type = "rows"

    # Infer scalar vs multi-row
    result_kind: Optional[str] = None
    scalar_value: Optional[Any] = None
    rows = result.sample_rows or []
    if rows and len(rows) == 1 and result.columns and len(result.columns) == 1:
        result_kind = "single_row"
        scalar_value = rows[0][0]
    elif rows and len(rows) > 1:
        result_kind = "multi_row"

    return ExecutionResult(
        result_type=result_type,
        result_kind=result_kind,
        columns=result.columns or [],
        row_count=result.row_count,
        sample_rows=[list(r) for r in rows],
        row_hash=result.row_hash,
        scalar_value=scalar_value,
        error=result.error,
    )


def execute_sql(db_path: str, sql: str, timeout: int = 30) -> ExecutionResult:
    """
    Execute SQL against a SQLite database and summarize the result.
    """
    summary = execute_and_summarize(db_path, sql, timeout=timeout)
    return _sqlresult_to_execution(summary)


def _jaccard_at_k(pred_rows: List[List[Any]], gold_rows: List[List[Any]], k: int = 10) -> Optional[float]:
    if not gold_rows and not pred_rows:
        return 1.0
    pred_set = {tuple(map(str, r)) for r in pred_rows[:k]}
    gold_set = {tuple(map(str, r)) for r in gold_rows[:k]}
    if not pred_set and not gold_set:
        return 1.0
    if not pred_set or not gold_set:
        return 0.0
    inter = len(pred_set & gold_set)
    union = len(pred_set | gold_set)
    if union == 0:
        return 1.0
    return inter / union


def compare_results(pred: ExecutionResult, gold: ExecutionResult) -> Mismatch:
    """
    Compare two execution results and produce a structured Mismatch.

    Priority order (highest first):
        error > timeout > empty_result > row_count_mismatch > value_mismatch > correct
    """
    # Default distance metrics
    distance: Dict[str, Optional[float]] = {
        "row_count_ratio": None,
        "jaccard_at_k": None,
        "scalar_log_ratio": None,
    }
    warnings: List[str] = []

    # Error / timeout handling
    if pred.result_type == "error":
        return Mismatch(mismatch_type="error", subtype=None, warnings=warnings, distance=distance)
    if pred.result_type == "timeout":
        return Mismatch(mismatch_type="timeout", subtype=None, warnings=warnings, distance=distance)

    # Empty result vs non-empty
    if pred.row_count == 0 and gold.row_count > 0:
        if gold.columns and pred.columns != gold.columns:
            warnings.append("schema_diff")
        return Mismatch(
            mismatch_type="empty_result",
            subtype=None,
            warnings=warnings,
            distance=distance,
        )

    # Compute basic distances where possible
    if gold.row_count > 0:
        distance["row_count_ratio"] = pred.row_count / gold.row_count

    distance["jaccard_at_k"] = _jaccard_at_k(pred.sample_rows, gold.sample_rows)

    if (
        pred.scalar_value is not None
        and gold.scalar_value is not None
        and isinstance(pred.scalar_value, (int, float))
        and isinstance(gold.scalar_value, (int, float))
        and gold.scalar_value not in (0, 0.0)
    ):
        try:
            ratio = float(pred.scalar_value) / float(gold.scalar_value)
            if ratio > 0:
                distance["scalar_log_ratio"] = math.log(ratio)
        except Exception:
            distance["scalar_log_ratio"] = None

    # Row-count mismatch
    if pred.row_count != gold.row_count:
        return Mismatch(
            mismatch_type="row_count_mismatch",
            subtype=None,
            warnings=warnings,
            distance=distance,
        )

    # Schema / value mismatch
    if pred.columns != gold.columns:
        warnings.append("schema_diff")

    if pred.row_hash and gold.row_hash and pred.row_hash != gold.row_hash:
        return Mismatch(
            mismatch_type="value_mismatch",
            subtype=None,
            warnings=warnings,
            distance=distance,
        )

    return Mismatch(
        mismatch_type="correct",
        subtype=None,
        warnings=warnings,
        distance=distance,
    )


# ---------------------------------------------------------------------------
# 2. AST parsing + fingerprints
# ---------------------------------------------------------------------------


def parse_ast_signature(sql: str, dialect: str = "sqlite") -> ASTSignature:
    """
    Parse SQL into a coarse ASTSignature using sqlglot and the teacher parser.
    """
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return ASTSignature(parse_ok=False)

    struct = parse_sql_structure(sql, dialect=dialect)

    tables_involved = sorted(set(struct.tables))

    # Represent join edges using ON conditions; this is sufficient for
    # high-level reasoning and fingerprints.
    join_edges: List[Dict[str, Any]] = []
    for j in struct.joins:
        join_edges.append({"table": j.table, "join_type": j.join_type, "on": j.on_condition})

    predicates = list(struct.where_conditions)

    # Approximate select shape
    output_arity = len(struct.select_expressions)
    has_distinct = struct.has_distinct
    has_aggregate = False
    try:
        # Look for any aggregate functions in the parsed tree.
        has_aggregate = any(isinstance(node, exp.AggFunc) for node in parsed.find_all(exp.Func))
    except Exception:
        # Fallback: simple heuristic on SQL text
        upper_sql = sql.upper()
        has_aggregate = any(tok in upper_sql for tok in ("COUNT(", "SUM(", "AVG(", "MIN(", "MAX("))

    select_shape = SelectShape(
        output_arity=output_arity,
        has_distinct=has_distinct,
        has_aggregate=has_aggregate,
    )

    group_by_keys = list(struct.group_by_columns)
    order_by = list(struct.order_by)
    limit = struct.limit
    has_subquery = struct.has_subquery

    return ASTSignature(
        parse_ok=True,
        from_table=struct.from_table or None,
        tables_involved=tables_involved,
        join_edges=join_edges,
        predicates=predicates,
        having=list(struct.having_conditions),
        select_expressions=list(struct.select_expressions),
        select_column_refs=list(struct.select_column_refs),
        select_shape=select_shape,
        group_by_keys=group_by_keys,
        order_by=order_by,
        limit=limit,
        has_subquery=has_subquery,
        has_cte=bool(struct.has_cte),
        comparison_operators=list(struct.comparison_operators),
        logical_operators=list(struct.logical_operators),
        null_checks=list(struct.null_checks),
        predicate_values=list(struct.predicate_values),
        window_functions=list(struct.window_functions),
        date_time_functions=list(struct.date_time_functions),
        string_functions=list(struct.string_functions),
        math_functions=list(struct.math_functions),
        conditional_exprs=list(struct.conditional_exprs),
        type_casts=list(struct.type_casts),
        set_operations=list(struct.set_operations),
    )


def _stable_hash(payload: Any) -> str:
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def compute_fingerprints(ast: ASTSignature) -> Fingerprints:
    """
    Compute simple, stable fingerprints from an ASTSignature.
    """
    join_graph_fp = _stable_hash(ast.join_edges)
    select_shape_fp = _stable_hash(
        {
            "output_arity": ast.select_shape.output_arity,
            "has_distinct": ast.select_shape.has_distinct,
            "has_aggregate": ast.select_shape.has_aggregate,
        }
    )
    predicate_fp = _stable_hash(ast.predicates)

    return Fingerprints(
        join_graph_fp=join_graph_fp,
        select_shape_fp=select_shape_fp,
        predicate_fp=predicate_fp,
    )


# ---------------------------------------------------------------------------
# 3. Structural probes
# ---------------------------------------------------------------------------


def _bucket_row_ratio(r: Optional[float]) -> Optional[str]:
    if r is None:
        return None
    if r < 0.5:
        return "UNDER_0.5"
    if r <= 2.0:
        return "0.5-2"
    return "OVER_2"


def _bucket_fanout(v: Optional[float]) -> Optional[str]:
    if v is None:
        return "NA"
    if v < 1.5:
        return "NORMAL"
    if v < 3.0:
        return "HIGH_1.5-3"
    return "HIGH_3+"


def _bucket_coverage(v: Optional[float]) -> Optional[str]:
    if v is None:
        return "NA"
    if v >= 0.8:
        return "GOOD_0.8+"
    if v >= 0.5:
        return "MEDIUM_0.5-0.8"
    return "LOW_<0.5"


def run_probes(
    db_path: str,
    pred_sql: str,
    gold_sql: str,
    timeout: int = 30,
    dialect: str = "sqlite",
) -> Tuple[SignalsSummary, SignalsDetails]:
    """
    Run structural probes and summarize them into SignalsSummary/Details.
    """
    # First, execute the full queries once for logging purposes.
    pred_exec = execute_sql(db_path, pred_sql, timeout=timeout)
    gold_exec = execute_sql(db_path, gold_sql, timeout=timeout)

    # Structural probes (table set, join graph, key coverage, fanout, etc.)
    probes = run_structural_probes(
        db_path=db_path,
        pred_sql=pred_sql,
        gold_sql=gold_sql,
        timeout=timeout,
        dialect=dialect,
    )

    # Base row ratio
    base_row_ratio: Optional[float] = None
    if gold_exec.row_count > 0:
        base_row_ratio = pred_exec.row_count / gold_exec.row_count

    # Key coverage stats
    coverage_min: Optional[float] = None
    key_cov = probes.get("key_coverage", {})
    key_items: List[Dict[str, Any]] = []
    for side in ("pred", "gold"):
        for item in key_cov.get(side, []):
            enriched = dict(item)
            enriched["side"] = side
            key_items.append(enriched)
    ratios: List[float] = []
    for item in key_items:
        for field in ("left_ratio", "right_ratio"):
            val = item.get(field)
            if isinstance(val, (int, float)):
                ratios.append(float(val))
    if ratios:
        coverage_min = min(ratios)

    # Fanout statistics
    fanout = probes.get("fanout", {})
    fanout_ratio_pred = fanout.get("pred")

    # Join edges diff
    join_graph = probes.get("join_graph", {})
    join_diff = join_graph.get("diff", {})
    join_edges_diff = bool(join_diff.get("pred_only") or join_diff.get("gold_only"))

    # Join divergence info
    join_step = probes.get("join_step_trace", {})
    divergence_step = join_step.get("divergence_k")
    divergence_ratio = join_step.get("divergence_ratio")

    # Output / shape probes from AST
    ast_pred = parse_ast_signature(pred_sql, dialect=dialect)
    ast_gold = parse_ast_signature(gold_sql, dialect=dialect)

    output_arity_pred = ast_pred.select_shape.output_arity
    output_arity_gold = ast_gold.select_shape.output_arity
    output_arity_mismatch = output_arity_pred != output_arity_gold

    has_aggregate_pred = ast_pred.select_shape.has_aggregate
    has_limit_pred = ast_pred.limit is not None
    has_order_by_pred = bool(ast_pred.order_by)

    # Predicate selectivity outlier
    pred_sel = probes.get("predicate_selectivity", {}).get("pred", [])
    selectivity_outlier = False
    for item in pred_sel:
        ratio = item.get("ratio")
        if isinstance(ratio, (int, float)):
            if ratio < 0.01 or ratio > 0.99:
                selectivity_outlier = True
                break

    summary = SignalsSummary(
        base_row_ratio=base_row_ratio,
        base_row_ratio_bucket=_bucket_row_ratio(base_row_ratio),
        fanout_ratio_pred=fanout_ratio_pred,
        fanout_bucket=_bucket_fanout(fanout_ratio_pred),
        coverage_min=coverage_min,
        coverage_bucket=_bucket_coverage(coverage_min),
        join_edges_diff=join_edges_diff,
        divergence_step=divergence_step,
        output_arity_pred=output_arity_pred,
        output_arity_gold=output_arity_gold,
        output_arity_mismatch=output_arity_mismatch,
        has_aggregate_pred=has_aggregate_pred,
        has_limit_pred=has_limit_pred,
        has_order_by_pred=has_order_by_pred,
        selectivity_outlier=selectivity_outlier,
    )

    # Flatten predicate selectivity for detailed logging
    ps_raw = probes.get("predicate_selectivity", {})
    ps_items: List[Dict[str, Any]] = []
    for side in ("pred", "gold"):
        for item in ps_raw.get(side, []):
            enriched = dict(item)
            enriched["side"] = side
            ps_items.append(enriched)

    details = SignalsDetails(
        pred_ast=ast_pred.dict(),
        gold_ast=ast_gold.dict(),
        pred_exec=pred_exec.dict(),
        gold_exec=gold_exec.dict(),
        join_step_trace=probes.get("join_step_trace", {}),
        join_edges_diff_detail=join_diff,
        key_coverage=key_items,
        fanout=fanout,
        predicate_selectivity=ps_items,
        output_shape_probe={
            "pred": {
                "output_arity": output_arity_pred,
                "has_distinct": ast_pred.select_shape.has_distinct,
                "has_aggregate": ast_pred.select_shape.has_aggregate,
            },
            "gold": {
                "output_arity": output_arity_gold,
                "has_distinct": ast_gold.select_shape.has_distinct,
                "has_aggregate": ast_gold.select_shape.has_aggregate,
            },
        },
    )

    return summary, details
