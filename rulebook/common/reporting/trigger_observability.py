from __future__ import annotations

from typing import Any, Dict, List, Sequence, Set, Tuple

from method.EEA.rulebook.common.analysis.structure_family import (
    ast_output_contract_family,
    normalized_group_by_keys_from_ast,
    normalized_join_key_tokens_from_ast,
)


TRIGGER_VISIBLE_ANTI_PATTERN_TYPES = {
    "join_path_contains",
    "missing_distinct",
    "missing_function_family",
    "missing_null_check",
    "output_arity_mismatch",
    "projection_arity_reduction",
    "projection_arity_expansion",
    "excess_join_to_table",
    "missing_join_to_table",
    "aggregate_shape_mismatch",
    "select_expr_family_mismatch",
    "count_star_to_count_plain",
    "count_star_to_count_distinct",
    "count_plain_to_count_distinct",
    "extra_group_by_key",
    "missing_group_by_key",
    "output_role_substitution",
    "join_key_equivalence",
}


ANTI_PATTERN_OBSERVABILITY_WEIGHTS: Dict[str, float] = {
    "output_arity_mismatch": 1.0,
    "projection_arity_reduction": 0.95,
    "projection_arity_expansion": 0.9,
    "join_path_contains": 0.8,
    "excess_join_to_table": 0.7,
    "missing_join_to_table": 0.6,
    "aggregate_shape_mismatch": 0.45,
    "missing_distinct": 0.4,
    "missing_null_check": 0.35,
    "missing_function_family": 0.3,
    "select_expr_family_mismatch": 0.2,
    "count_star_to_count_plain": 0.8,
    "count_star_to_count_distinct": 0.85,
    "count_plain_to_count_distinct": 0.75,
    "extra_group_by_key": 0.65,
    "missing_group_by_key": 0.6,
    "output_role_substitution": 0.25,
    "join_key_equivalence": 0.45,
}


def ast_tables_present(ast: Dict[str, Any]) -> Set[str]:
    tables = set(str(t) for t in (ast.get("tables_involved") or []) if str(t).strip())
    from_table = str(ast.get("from_table") or "").strip()
    if from_table:
        tables.add(from_table)
    for edge in ast.get("join_edges") or []:
        table = str(edge.get("table") or "").strip()
        if table:
            tables.add(table)
    return tables


def aggregate_shape_from_ast(ast: Dict[str, Any]) -> str:
    select_shape = ast.get("select_shape") or {}
    if not bool(select_shape.get("has_aggregate", False)):
        return "none"
    expr_text = " ".join(str(expr).lower() for expr in (ast.get("select_expressions") or []))
    if "count(distinct" in expr_text:
        return "count_distinct"
    if "count(*)" in expr_text:
        return "count_star"
    if "count(" in expr_text:
        return "count_plain"
    if "sum(" in expr_text:
        return "sum"
    if "avg(" in expr_text:
        return "avg"
    if "max(" in expr_text:
        return "max"
    if "min(" in expr_text:
        return "min"
    return "aggregate_other"


def select_expr_family_from_ast(ast: Dict[str, Any]) -> str:
    output_family = ast_output_contract_family(ast)
    roles = list(output_family.get("column_roles") or [])
    if bool((ast.get("select_shape") or {}).get("has_aggregate", False)):
        return "aggregate"
    if roles:
        return "+".join(sorted(roles))
    expr_text = " ".join(str(expr).lower() for expr in (ast.get("select_expressions") or []))
    if "case when" in expr_text or " iif(" in f" {expr_text}":
        return "conditional_expression"
    if expr_text:
        return "expression"
    return "unknown"


def output_role_signature_from_ast(ast: Dict[str, Any]) -> Tuple[str, ...]:
    output_family = ast_output_contract_family(ast)
    roles = [str(role) for role in (output_family.get("column_roles") or []) if str(role).strip()]
    return tuple(sorted(set(roles)))


def group_by_signature_from_ast(ast: Dict[str, Any]) -> Tuple[str, ...]:
    return normalized_group_by_keys_from_ast(ast)


def join_key_signature_from_ast(ast: Dict[str, Any]) -> Tuple[str, ...]:
    return normalized_join_key_tokens_from_ast(ast)


def anti_pattern_is_trigger_visible(anti_pattern: Dict[str, Any]) -> bool:
    return str(anti_pattern.get("check_type") or "") in TRIGGER_VISIBLE_ANTI_PATTERN_TYPES


def compute_trigger_observability_from_anti_patterns(
    anti_patterns: Sequence[Dict[str, Any]],
) -> Tuple[float, str, List[Dict[str, Any]]]:
    visible = [dict(ap) for ap in anti_patterns if anti_pattern_is_trigger_visible(ap)]
    raw_score = 0.0
    operator_identity_score = 0.0
    for anti_pattern in visible:
        check_type = str(anti_pattern.get("check_type") or "")
        raw_support_ratio = anti_pattern.get("support_ratio", None)
        support_ratio = 0.5 if raw_support_ratio is None else float(raw_support_ratio or 0.0)
        contribution = float(ANTI_PATTERN_OBSERVABILITY_WEIGHTS.get(check_type, 0.0)) * support_ratio
        raw_score += contribution
        if str(anti_pattern.get("signal_role", "")) == "operator_identity":
            operator_identity_score += contribution
    score = min(raw_score, 1.0)
    if operator_identity_score > 0 and score >= 0.75:
        obs_class = "high"
    elif score >= 0.30:
        obs_class = "medium"
    else:
        obs_class = "low"
    return score, obs_class, visible


def pattern_trigger_observability_from_dict(pattern: Dict[str, Any]) -> Tuple[float, str, List[Dict[str, Any]]]:
    direct_score = pattern.get("trigger_observability_score")
    direct_class = pattern.get("trigger_observability_class")
    direct_visible = pattern.get("trigger_visible_anti_patterns")
    if direct_score is not None and direct_class is not None:
        try:
            score = float(direct_score)
            obs_class = str(direct_class)
            visible = [dict(item) for item in (direct_visible or [])]
            if score > 0.0 or obs_class != "low" or visible:
                return score, obs_class, visible
        except Exception:
            pass

    trigger = dict(pattern.get("trigger") or {})
    level_2 = dict(trigger.get("level_2") or {})
    if "trigger_observability_score" in level_2 and "trigger_observability_class" in level_2:
        score = float(level_2.get("trigger_observability_score", 0.0) or 0.0)
        obs_class = str(level_2.get("trigger_observability_class") or "low")
        visible = [dict(item) for item in (level_2.get("trigger_visible_anti_patterns") or [])]
        return score, obs_class, visible

    anti_patterns = list(level_2.get("sql_anti_patterns") or [])
    return compute_trigger_observability_from_anti_patterns(anti_patterns)
