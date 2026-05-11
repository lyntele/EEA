"""Code-derived repair cards for WUv2-4 retrieval buckets.

The card is intentionally deterministic. It uses SQL AST shape plus existing
schema role hints already present in ``LocalSchemaView``; it never calls an LLM.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import sqlglot
from sqlglot import exp

from method.EEA.rulebook.common.core.data_structures import (
    CaseAudit,
    ErrorInstanceV2,
    GroupSummary,
    LocalSchemaView,
)


def _payload(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    return dict(getattr(value, "__dict__", {}) or {})


def _as_list(values: Any) -> List[str]:
    if values is None:
        return []
    if isinstance(values, (list, tuple, set)):
        return [str(value) for value in values if str(value)]
    return [str(values)] if str(values) else []


def _parse_sql(sql: str) -> Optional[exp.Expression]:
    text = str(sql or "").strip()
    if not text:
        return None
    try:
        return sqlglot.parse_one(text, read="sqlite")
    except Exception:
        try:
            return sqlglot.parse_one(text)
        except Exception:
            return None


def _alias_map(ast: Optional[exp.Expression]) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    if ast is None:
        return aliases
    for table in ast.find_all(exp.Table):
        name = str(table.name or "").strip()
        alias = str(table.alias_or_name or name).strip()
        if name:
            aliases[name.lower()] = name
        if alias and name:
            aliases[alias.lower()] = name
    return aliases


def _role_hint_map(schema_view: Optional[LocalSchemaView]) -> Dict[Tuple[str, str], str]:
    roles: Dict[Tuple[str, str], str] = {}
    if schema_view is None:
        return roles
    for hint in getattr(schema_view, "semantic_hints", []) or []:
        role = str(getattr(hint, "role_family", "") or "").strip()
        table = str(getattr(hint, "table", "") or "").strip().lower()
        column = str(getattr(hint, "column", "") or "").strip().lower()
        if role and table and column:
            roles[(table, column)] = role
    return roles


def _infer_expression_role(expr_value: Any) -> str:
    text = str(expr_value or "").strip().lower()
    bare = text.split(".")[-1].strip("`\"[]")
    if not bare:
        return "unknown"
    if re.search(r"\b(row_number|rank|dense_rank)\s*\(", text):
        return "window_expr"
    if re.search(r"\b(count|sum|avg|min|max|median)\s*\(", text):
        return "aggregate_expr"
    if "case when" in text:
        return "conditional_expr"
    if any(op in text for op in ("||", "+", "-", "*", "/")) and text != bare:
        return "computed_expr"
    if (
        bare == "id"
        or bare.endswith("_id")
        or bare.endswith("id")
        or re.search(r"(?:^|_)id\d+$", bare)
        or re.search(r"_id\d+$", bare)
        or bare.endswith("_key")
        or bare == "key"
    ):
        return "identifier_like"
    return "column_expr"


def _column_role(
    column: exp.Column,
    *,
    aliases: Mapping[str, str],
    roles: Mapping[Tuple[str, str], str],
) -> str:
    column_name = str(column.name or "").strip()
    table_name = str(column.table or "").strip()
    resolved_table = aliases.get(table_name.lower(), table_name).lower()
    if resolved_table and column_name:
        role = roles.get((resolved_table, column_name.lower()))
        if role:
            return role
    if column_name:
        matching = {
            role
            for (table, col), role in roles.items()
            if col == column_name.lower() and (not resolved_table or table == resolved_table)
        }
        if len(matching) == 1:
            return next(iter(matching))
    return _infer_expression_role(column.sql())


def _first_column_role(
    expression: exp.Expression,
    *,
    aliases: Mapping[str, str],
    roles: Mapping[Tuple[str, str], str],
) -> str:
    for column in expression.find_all(exp.Column):
        return _column_role(column, aliases=aliases, roles=roles)
    return _infer_expression_role(expression.sql())


def _select_expressions(ast: Optional[exp.Expression]) -> List[exp.Expression]:
    if ast is None:
        return []
    expressions = ast.args.get("expressions") or []
    return [item for item in expressions if isinstance(item, exp.Expression)]


def _aggregate_name(expression: exp.Expression) -> str:
    for cls, name in (
        (exp.Count, "count"),
        (exp.Sum, "sum"),
        (exp.Avg, "avg"),
        (exp.Max, "max"),
        (exp.Min, "min"),
    ):
        if isinstance(expression, cls) or expression.find(cls):
            return name
    return ""


def _is_count_star(expression: exp.Expression) -> bool:
    text = expression.sql(dialect="sqlite").lower().replace(" ", "")
    return text.startswith("count(*)") or "count(*)" in text


def _is_distinct_count(expression: exp.Expression) -> bool:
    text = expression.sql(dialect="sqlite").lower()
    return "count" in text and "distinct" in text


def _derive_answer_unit_from_sql(
    sql: str,
    schema_view: Optional[LocalSchemaView],
) -> Dict[str, Any]:
    ast = _parse_sql(sql)
    aliases = _alias_map(ast)
    roles = _role_hint_map(schema_view)
    projections = _select_expressions(ast)
    if not projections:
        return {"kind": "unknown"}

    if len(projections) == 1:
        expression = projections[0]
        aggregate = _aggregate_name(expression)
        if _is_count_star(expression):
            return {"kind": "row_count"}
        if _is_distinct_count(expression):
            return {
                "kind": "distinct_entity_count",
                "entity_role": _first_column_role(expression, aliases=aliases, roles=roles),
            }
        if aggregate:
            return {
                "kind": "aggregate_value",
                "aggregate_kind": aggregate,
                "metric_role": _first_column_role(expression, aliases=aliases, roles=roles),
            }
        if expression.find(exp.Case):
            return {"kind": "conditional_value", "column_role": "conditional_expr"}
        column = expression.find(exp.Column)
        if column is not None:
            return {
                "kind": "single_column",
                "column_role": _column_role(column, aliases=aliases, roles=roles),
            }
        return {
            "kind": "single_expression",
            "expression_role": _infer_expression_role(expression.sql()),
        }

    projection_roles = [
        _first_column_role(expression, aliases=aliases, roles=roles)
        for expression in projections
    ]
    if len(projections) == 2:
        return {"kind": "paired_columns", "role_pair": projection_roles}
    return {
        "kind": "multi_column",
        "arity": len(projections),
        "roles": projection_roles,
    }


def _answer_unit_from_shape(roles: Sequence[Any], grain: Any = None) -> Dict[str, Any]:
    clean_roles = [str(role) for role in roles or [] if str(role)]
    grain_text = str(grain or "")
    if len(clean_roles) == 1:
        role = clean_roles[0]
        if role == "aggregate_expr":
            return {"kind": "aggregate_value", "aggregate_kind": "unknown", "metric_role": role}
        return {"kind": "single_column", "column_role": role, "grain": grain_text}
    if len(clean_roles) == 2:
        return {"kind": "paired_columns", "role_pair": clean_roles, "grain": grain_text}
    if clean_roles:
        return {"kind": "multi_column", "arity": len(clean_roles), "roles": clean_roles, "grain": grain_text}
    return {"kind": "unknown"}


def _effect_axes_from_error_instance(error_instance: ErrorInstanceV2) -> List[str]:
    axes: List[str] = []
    if error_instance.canonical_repair_ir and error_instance.canonical_repair_ir.repair_effect_signature:
        for effect in error_instance.canonical_repair_ir.repair_effect_signature.effect_candidates:
            axis = str(getattr(effect, "axis", "") or "").strip()
            if axis and axis not in axes:
                axes.append(axis)
    for item in error_instance.possible_effect_axes or []:
        axis = str(getattr(item, "axis", "") or "").strip()
        if axis and axis not in axes:
            axes.append(axis)
    return axes


def _operation_family_from_payloads(*payloads: Mapping[str, Any]) -> List[str]:
    families: List[str] = []
    for payload in payloads:
        # Retrieval needs a high-recall repair family, not the exact branch
        # action.  Concrete op verbs such as add/replace/move/reroute are
        # intentionally excluded here and left for admission/branch checks.
        for value in (
            payload.get("locus"),
            payload.get("target_family"),
            payload.get("output_shape_operation"),
        ):
            text = str(value or "").strip()
            if text and text not in families:
                families.append(text)
    return families


def _operation_family_from_error_instance(error_instance: ErrorInstanceV2) -> List[str]:
    structural = error_instance.repair_skeleton.structural
    shape = _payload(getattr(structural, "output_shape_delta", None))
    return _operation_family_from_payloads(
        {
            "locus": getattr(getattr(structural, "locus", None), "value", structural.locus),
            "op_family": getattr(getattr(structural, "op_family", None), "value", structural.op_family),
            "target_family": getattr(getattr(structural, "target_family", None), "value", structural.target_family),
            "output_shape_operation": shape.get("operation"),
        }
    )


def _preserve_invariants_from_error_instance(error_instance: ErrorInstanceV2) -> List[str]:
    values: List[str] = []
    insight = error_instance.repair_insight_signature
    if insight is not None:
        values.extend(str(item) for item in insight.preserve_invariants or [] if str(item))
    if error_instance.canonical_repair_ir is not None:
        values.extend(str(item) for item in error_instance.canonical_repair_ir.target_invariants or [] if str(item))
        values.extend(str(item) for item in error_instance.canonical_repair_ir.invariants or [] if str(item))
    for step in error_instance.repair_program or []:
        payload = _payload(step)
        values.extend(str(item) for item in payload.get("invariants") or [] if str(item))
    return sorted(set(values))


def derive_repair_card(
    error_instance: ErrorInstanceV2,
    schema_view: Optional[LocalSchemaView],
    *,
    case_audit: Optional[CaseAudit] = None,
) -> Dict[str, Any]:
    """Derive the WUv2-4 repair-card bucket evidence for one wrong case."""
    pred_sql = str(getattr(case_audit, "pred_sql", "") or "")
    gold_sql = str(getattr(case_audit, "gold_sql", "") or "")
    source_answer_unit = (
        _derive_answer_unit_from_sql(pred_sql, schema_view)
        if pred_sql
        else {"kind": "unknown"}
    )
    target_answer_unit = (
        _derive_answer_unit_from_sql(gold_sql, schema_view)
        if gold_sql
        else {"kind": "unknown"}
    )
    return {
        "schema_version": "repair-card-v1",
        "db_id": error_instance.db_id,
        "case_id": str(error_instance.case_id),
        "effect_axis": _effect_axes_from_error_instance(error_instance),
        "source_answer_unit": source_answer_unit,
        "target_answer_unit": target_answer_unit,
        "operation_family": _operation_family_from_error_instance(error_instance),
        "changed_locus": [
            str(getattr(error_instance.repair_skeleton.structural.locus, "value", error_instance.repair_skeleton.structural.locus))
        ],
        "preserve_invariants": _preserve_invariants_from_error_instance(error_instance),
    }


def repair_card_from_group_summary(group: GroupSummary) -> Dict[str, Any]:
    """Best-effort WUv2-4 repair card for already serialized libraries."""
    signals = _payload(getattr(group, "formation_signals", None))
    existing = _payload(signals.get("repair_card"))
    if existing:
        return existing
    delta = _payload(signals.get("delta"))
    shape = _payload(delta.get("output_shape_delta"))
    skeleton = _payload(signals.get("repair_skeleton"))
    structural = _payload(skeleton.get("structural"))
    ir = _payload(signals.get("canonical_repair_ir"))
    effect_signature = _payload(ir.get("repair_effect_signature"))
    axes = [
        str(effect.get("axis"))
        for effect in (effect_signature.get("effect_candidates") or [])
        if _payload(effect).get("axis")
    ] or _as_list(delta.get("delta_axes"))
    source_answer_unit = _answer_unit_from_shape(
        shape.get("current_roles") or [],
        shape.get("current_grain"),
    )
    target_answer_unit = _answer_unit_from_shape(
        shape.get("target_roles") or [],
        shape.get("target_grain"),
    )
    operation_family = _operation_family_from_payloads(
        {
            "locus": structural.get("locus"),
            "op_family": structural.get("op_family"),
            "target_family": structural.get("target_family"),
            "output_shape_operation": shape.get("operation") or delta.get("output_shape_operation"),
        }
    )
    return {
        "schema_version": "repair-card-v1-compat",
        "db_id": str(getattr(group, "db_id", "") or ""),
        "case_id": str((getattr(group, "case_ids", None) or [""])[0]),
        "effect_axis": sorted(set(axes)),
        "source_answer_unit": source_answer_unit,
        "target_answer_unit": target_answer_unit,
        "operation_family": operation_family,
        "changed_locus": [str(structural.get("locus") or "")] if structural.get("locus") else [],
        "preserve_invariants": sorted(set(_as_list(ir.get("target_invariants")) + _as_list(ir.get("invariants")))),
    }


__all__ = ["derive_repair_card", "repair_card_from_group_summary"]
