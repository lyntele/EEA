"""SQL structural-delta utilities used by the current v2 EEA pipeline.

The old v1 distillation package also contained a copy of these helpers. The
current system no longer uses the v1 trace -> cluster -> distilled-pattern
pipeline, but v2 still needs deterministic pred-vs-gold structure deltas while
building error instances and update memories. Keeping the small shared subset
here removes that dependency without changing the v2 behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set


@dataclass
class JoinEdge:
    table: str
    join_type: str
    on_clause: str


@dataclass
class DeltaFlags:
    """Pred-vs-gold SQL syntax difference vector."""

    join_tables_differ: bool = False
    join_on_differ: bool = False
    join_type_differ: bool = False
    select_columns_differ: bool = False
    output_arity_differ: bool = False
    distinct_differ: bool = False
    group_by_differ: bool = False
    order_by_differ: bool = False
    asc_desc_differ: bool = False
    limit_differ: bool = False
    having_differ: bool = False
    aggregate_differ: bool = False
    window_function_differ: bool = False
    date_time_function_differ: bool = False
    string_function_differ: bool = False
    math_function_differ: bool = False
    conditional_expr_differ: bool = False
    type_cast_differ: bool = False
    where_differ: bool = False
    null_handling_differ: bool = False
    where_value_differ: bool = False
    comparison_operator_differ: bool = False
    logical_operator_differ: bool = False
    subquery_differ: bool = False
    set_operation_differ: bool = False
    cte_differ: bool = False

    def to_vector(self) -> List[bool]:
        return [
            self.join_tables_differ,
            self.join_on_differ,
            self.join_type_differ,
            self.select_columns_differ,
            self.output_arity_differ,
            self.distinct_differ,
            self.group_by_differ,
            self.order_by_differ,
            self.asc_desc_differ,
            self.limit_differ,
            self.having_differ,
            self.aggregate_differ,
            self.window_function_differ,
            self.date_time_function_differ,
            self.string_function_differ,
            self.math_function_differ,
            self.conditional_expr_differ,
            self.type_cast_differ,
            self.where_differ,
            self.null_handling_differ,
            self.where_value_differ,
            self.comparison_operator_differ,
            self.logical_operator_differ,
            self.subquery_differ,
            self.set_operation_differ,
            self.cte_differ,
        ]

    def active_dimensions(self) -> List[str]:
        names = [
            "join_tables",
            "join_on",
            "join_type",
            "select_columns",
            "output_arity",
            "distinct",
            "group_by",
            "order_by",
            "asc_desc",
            "limit",
            "having",
            "aggregate",
            "window_function",
            "date_time_function",
            "string_function",
            "math_function",
            "conditional_expr",
            "type_cast",
            "where",
            "null_handling",
            "where_value",
            "comparison_operator",
            "logical_operator",
            "subquery",
            "set_operation",
            "cte",
        ]
        return [name for name, active in zip(names, self.to_vector()) if active]

    @staticmethod
    def hamming_distance(a: "DeltaFlags", b: "DeltaFlags") -> int:
        return sum(left != right for left, right in zip(a.to_vector(), b.to_vector()))


@dataclass
class StructuralDelta:
    delta_flags: DeltaFlags
    pred_only_joins: List[JoinEdge]
    gold_only_joins: List[JoinEdge]
    pred_tables: Set[str]
    gold_tables: Set[str]
    output_arity_pred: int
    output_arity_gold: int
    has_distinct_pred: bool
    has_distinct_gold: bool
    has_aggregate_pred: bool
    has_aggregate_gold: bool
    pred_only_predicates: List[str]
    gold_only_predicates: List[str]
    group_by_pred: List[str]
    group_by_gold: List[str]
    order_by_pred: List[Any]
    order_by_gold: List[Any]
    limit_pred: Optional[int] = None
    limit_gold: Optional[int] = None
    has_subquery_pred: bool = False
    has_subquery_gold: bool = False


def compute_delta_flags(
    pred_ast: Dict[str, Any],
    gold_ast: Dict[str, Any],
    join_edges_diff: Dict[str, Any],
) -> DeltaFlags:
    """Mechanically compare two AST summaries without schema/domain knowledge."""

    pred_shape = pred_ast.get("select_shape", {}) or {}
    gold_shape = gold_ast.get("select_shape", {}) or {}

    return DeltaFlags(
        join_tables_differ=(
            set(pred_ast.get("tables_involved", []) or [])
            != set(gold_ast.get("tables_involved", []) or [])
        ),
        join_on_differ=bool(join_edges_diff.get("pred_only") or join_edges_diff.get("gold_only")),
        join_type_differ=_any_join_type_only_diff(join_edges_diff),
        select_columns_differ=_select_columns_diff(pred_ast, gold_ast),
        output_arity_differ=(
            pred_shape.get("output_arity", 0) != gold_shape.get("output_arity", 0)
        ),
        distinct_differ=(
            pred_shape.get("has_distinct", False) != gold_shape.get("has_distinct", False)
        ),
        group_by_differ=(
            (pred_ast.get("group_by_keys", []) or []) != (gold_ast.get("group_by_keys", []) or [])
        ),
        order_by_differ=(
            (pred_ast.get("order_by", []) or []) != (gold_ast.get("order_by", []) or [])
        ),
        asc_desc_differ=_asc_desc_diff(pred_ast, gold_ast),
        limit_differ=pred_ast.get("limit") != gold_ast.get("limit"),
        having_differ=bool((pred_ast.get("having", []) or []) != (gold_ast.get("having", []) or [])),
        aggregate_differ=(
            pred_shape.get("has_aggregate", False) != gold_shape.get("has_aggregate", False)
        ),
        window_function_differ=_function_category_diff(pred_ast, gold_ast, "window_functions"),
        date_time_function_differ=_function_category_diff(pred_ast, gold_ast, "date_time_functions"),
        string_function_differ=_function_category_diff(pred_ast, gold_ast, "string_functions"),
        math_function_differ=_function_category_diff(pred_ast, gold_ast, "math_functions"),
        conditional_expr_differ=_function_category_diff(pred_ast, gold_ast, "conditional_exprs"),
        type_cast_differ=_function_category_diff(pred_ast, gold_ast, "type_casts"),
        where_differ=(
            set(pred_ast.get("predicates", []) or []) != set(gold_ast.get("predicates", []) or [])
        ),
        null_handling_differ=_set_field_diff(pred_ast, gold_ast, "null_checks"),
        where_value_differ=_set_field_diff(pred_ast, gold_ast, "predicate_values"),
        comparison_operator_differ=_set_field_diff(pred_ast, gold_ast, "comparison_operators"),
        logical_operator_differ=(
            (pred_ast.get("logical_operators", []) or [])
            != (gold_ast.get("logical_operators", []) or [])
        ),
        subquery_differ=(
            pred_ast.get("has_subquery", False) != gold_ast.get("has_subquery", False)
        ),
        set_operation_differ=(
            (pred_ast.get("set_operations", []) or [])
            != (gold_ast.get("set_operations", []) or [])
        ),
        cte_differ=pred_ast.get("has_cte", False) != gold_ast.get("has_cte", False),
    )


def build_structural_delta(
    pred_ast: Dict[str, Any],
    gold_ast: Dict[str, Any],
    join_edges_diff: Dict[str, Any],
) -> StructuralDelta:
    pred_shape = pred_ast.get("select_shape", {}) or {}
    gold_shape = gold_ast.get("select_shape", {}) or {}
    pred_predicates = set(pred_ast.get("predicates", []) or [])
    gold_predicates = set(gold_ast.get("predicates", []) or [])

    return StructuralDelta(
        delta_flags=compute_delta_flags(pred_ast, gold_ast, join_edges_diff),
        pred_only_joins=[
            JoinEdge(table=edge[0], join_type=edge[1], on_clause=edge[2])
            for edge in join_edges_diff.get("pred_only", [])
        ],
        gold_only_joins=[
            JoinEdge(table=edge[0], join_type=edge[1], on_clause=edge[2])
            for edge in join_edges_diff.get("gold_only", [])
        ],
        pred_tables=set(pred_ast.get("tables_involved", []) or []),
        gold_tables=set(gold_ast.get("tables_involved", []) or []),
        output_arity_pred=int(pred_shape.get("output_arity", 0) or 0),
        output_arity_gold=int(gold_shape.get("output_arity", 0) or 0),
        has_distinct_pred=bool(pred_shape.get("has_distinct", False)),
        has_distinct_gold=bool(gold_shape.get("has_distinct", False)),
        has_aggregate_pred=bool(pred_shape.get("has_aggregate", False)),
        has_aggregate_gold=bool(gold_shape.get("has_aggregate", False)),
        pred_only_predicates=list(pred_predicates - gold_predicates),
        gold_only_predicates=list(gold_predicates - pred_predicates),
        group_by_pred=list(pred_ast.get("group_by_keys", []) or []),
        group_by_gold=list(gold_ast.get("group_by_keys", []) or []),
        order_by_pred=list(pred_ast.get("order_by", []) or []),
        order_by_gold=list(gold_ast.get("order_by", []) or []),
        limit_pred=pred_ast.get("limit"),
        limit_gold=gold_ast.get("limit"),
        has_subquery_pred=bool(pred_ast.get("has_subquery", False)),
        has_subquery_gold=bool(gold_ast.get("has_subquery", False)),
    )


def _any_join_type_only_diff(join_edges_diff: Dict[str, Any]) -> bool:
    pred_only = join_edges_diff.get("pred_only", [])
    gold_only = join_edges_diff.get("gold_only", [])
    for pred_edge in pred_only:
        for gold_edge in gold_only:
            if pred_edge[0] == gold_edge[0] and pred_edge[2] == gold_edge[2] and pred_edge[1] != gold_edge[1]:
                return True
    return False


def _select_columns_diff(pred_ast: Dict[str, Any], gold_ast: Dict[str, Any]) -> bool:
    pred_exprs = pred_ast.get("select_expressions", []) or []
    gold_exprs = gold_ast.get("select_expressions", []) or []
    if pred_exprs or gold_exprs:
        return pred_exprs != gold_exprs
    pred_refs = pred_ast.get("select_column_refs", []) or []
    gold_refs = gold_ast.get("select_column_refs", []) or []
    if pred_refs or gold_refs:
        return pred_refs != gold_refs
    return pred_ast.get("select_shape", {}) != gold_ast.get("select_shape", {})


def _asc_desc_diff(pred_ast: Dict[str, Any], gold_ast: Dict[str, Any]) -> bool:
    pred_ob = pred_ast.get("order_by", []) or []
    gold_ob = gold_ast.get("order_by", []) or []
    if len(pred_ob) != len(gold_ob):
        return False
    for pred_item, gold_item in zip(pred_ob, gold_ob):
        if isinstance(pred_item, dict) and isinstance(gold_item, dict):
            if pred_item.get("column") == gold_item.get("column") and pred_item.get("direction") != gold_item.get("direction"):
                return True
    return False


def _function_category_diff(pred_ast: Dict[str, Any], gold_ast: Dict[str, Any], category_key: str) -> bool:
    pred_funcs = set(pred_ast.get(category_key, []) or [])
    gold_funcs = set(gold_ast.get(category_key, []) or [])
    if not pred_funcs and not gold_funcs:
        return False
    return pred_funcs != gold_funcs


def _set_field_diff(pred_ast: Dict[str, Any], gold_ast: Dict[str, Any], key: str) -> bool:
    pred_values = set(pred_ast.get(key, []) or [])
    gold_values = set(gold_ast.get(key, []) or [])
    if not pred_values and not gold_values:
        return False
    return pred_values != gold_values
