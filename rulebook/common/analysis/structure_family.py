from __future__ import annotations

import json
import re
from collections import Counter
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from method.EEA.rulebook.refine.signals import parse_ast_signature


PATTERN_SCHEMA_VERSION = "pattern-schema-v0.2.0"
TRIGGER_STRATEGY_VERSION = "structure-first-v1"
PROMPT_BUNDLE_VERSION = "manual-valid-error-case-v1"
STRUCTURE_SIGNATURE_VERSION = "structure-signature-v1"
DISCOVERY_STRATEGY_VERSION = "coverage-first-v1"
EVOLUTION_STRATEGY_VERSION = "fork-only-v1"
PRIMARY_EDIT_MAJORITY_THRESHOLD = 0.70
PRIMARY_EDIT_ABSOLUTE_THRESHOLD = 0.80

SPECIAL_FUNCTION_FAMILIES = (
    "aggregate",
    "window",
    "conditional_expr",
    "type_cast",
    "string",
    "date_time",
    "math",
    "subquery",
    "set_operation",
    "cte",
)

WRAPPER_FUNCTION_FAMILIES = {"subquery", "set_operation", "cte"}
HARD_JOIN_PATH_FUNCTION_FAMILIES = {"conditional_expr", "type_cast", "aggregate", "window"}
SOFT_JOIN_PATH_FUNCTION_FAMILIES = {"aggregate"}


def _item_get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _item_delta_flags_payload(item: Any) -> Dict[str, bool] | None:
    structural_delta = _item_get(item, "structural_delta")
    if structural_delta is None:
        return None
    delta_flags = _item_get(structural_delta, "delta_flags")
    if delta_flags is None:
        return None
    if isinstance(delta_flags, dict):
        return {str(field): bool(value) for field, value in delta_flags.items()}
    return {
        field: bool(getattr(delta_flags, field))
        for field in getattr(delta_flags, "__dataclass_fields__", {}).keys()
    }


@lru_cache(maxsize=4096)
def cached_ast_signature(sql: str) -> Dict[str, Any]:
    ast = parse_ast_signature(sql or "")
    if hasattr(ast, "model_dump"):
        return ast.model_dump()
    return ast.dict()


def column_role(ref: str) -> str:
    token = str(ref or "").split(".")[-1].strip("`\"[]").lower()
    if not token:
        return "other"
    if token == "id" or token.endswith("_id") or "uuid" in token or token.endswith("_key") or token == "key":
        return "identifier"
    if "code" in token:
        return "code"
    if "name" in token or "title" in token:
        return "name"
    if "label" in token or "flag" in token or "status" in token:
        return "label"
    if "date" in token or "time" in token or token.endswith("_at"):
        return "temporal"
    if "count" in token or "num" in token or "ratio" in token or "pct" in token:
        return "measure"
    return "other"


def normalize_column_ref(ref: str) -> str:
    text = str(ref or "").strip().strip("`\"[]").lower()
    if not text:
        return ""
    # Drop table / alias prefixes, preserve the underlying column-like token.
    if "." in text:
        text = text.split(".")[-1]
    return text.strip()


def normalized_group_by_keys_from_ast(ast: Dict[str, Any]) -> Tuple[str, ...]:
    keys = []
    for key in list(ast.get("group_by_keys") or []):
        normalized = normalize_column_ref(str(key or ""))
        if normalized:
            keys.append(normalized)
    return tuple(sorted(set(keys)))


def _normalize_join_identifier(token: str) -> str:
    cleaned = normalize_column_ref(token)
    if cleaned:
        return cleaned
    return str(token or "").strip().strip("`\"[]").lower()


def _normalize_join_condition(on_clause: str) -> Tuple[str, ...]:
    clause = str(on_clause or "").strip().lower()
    if not clause:
        return tuple()

    predicates: List[str] = []
    for left, right in re.findall(r"([a-zA-Z_][\w\.\[\]`\"]*)\s*=\s*([a-zA-Z_][\w\.\[\]`\"]*)", clause):
        norm_left = _normalize_join_identifier(left)
        norm_right = _normalize_join_identifier(right)
        if not norm_left or not norm_right:
            continue
        predicates.append("=".join(sorted((norm_left, norm_right))))

    if predicates:
        return tuple(sorted(set(predicates)))

    fallback = " ".join(clause.split())
    if not fallback:
        return tuple()
    return (fallback,)


def normalized_join_key_tokens_from_ast(ast: Dict[str, Any]) -> Tuple[str, ...]:
    tokens: List[str] = []
    for edge in ast.get("join_edges") or []:
        join_type = str(edge.get("join_type") or "").strip().lower()
        on_clause = str(edge.get("on_clause") or edge.get("on") or "")
        normalized_predicates = _normalize_join_condition(on_clause)
        for predicate in normalized_predicates:
            if predicate:
                if join_type:
                    tokens.append(f"{join_type}:{predicate}")
                else:
                    tokens.append(predicate)
    return tuple(sorted(set(tokens)))


def _sorted_unique(values: Iterable[str]) -> List[str]:
    return sorted({str(v) for v in values if str(v).strip()})


def ast_table_family(ast: Dict[str, Any]) -> Dict[str, Any]:
    tables = _sorted_unique(ast.get("tables_involved") or [])
    join_tables = _sorted_unique(edge.get("table", "") for edge in (ast.get("join_edges") or []))
    join_skeleton = sorted(
        f"{edge.get('table', '')}:{edge.get('join_type', '')}"
        for edge in (ast.get("join_edges") or [])
        if edge.get("table")
    )
    return {
        "from_table": str(ast.get("from_table") or ""),
        "tables": tables,
        "join_tables": join_tables,
        "join_skeleton": join_skeleton,
    }


def ast_output_contract_family(ast: Dict[str, Any]) -> Dict[str, Any]:
    select_refs = _sorted_unique(ast.get("select_column_refs") or [])
    select_exprs = _sorted_unique(ast.get("select_expressions") or [])
    roles = [column_role(ref) for ref in select_refs] or [column_role(expr) for expr in select_exprs]
    role_counts = Counter(role for role in roles if role != "other")
    shape = ast.get("select_shape") or {}
    return {
        "output_arity": int(shape.get("output_arity", 0) or 0),
        "has_distinct": bool(shape.get("has_distinct", False)),
        "has_aggregate": bool(shape.get("has_aggregate", False)),
        "select_refs": select_refs,
        "select_exprs": select_exprs,
        "column_roles": sorted(role_counts.keys()),
        "column_role_counts": dict(sorted(role_counts.items())),
    }


def ast_function_family(ast: Dict[str, Any]) -> Dict[str, bool]:
    return {
        "aggregate": bool((ast.get("select_shape") or {}).get("has_aggregate", False)),
        "window": bool(ast.get("window_functions")),
        "conditional_expr": bool(ast.get("conditional_exprs")),
        "type_cast": bool(ast.get("type_casts")),
        "string": bool(ast.get("string_functions")),
        "date_time": bool(ast.get("date_time_functions")),
        "math": bool(ast.get("math_functions")),
        "subquery": bool(ast.get("has_subquery", False)),
        "set_operation": bool(ast.get("set_operations")),
        "cte": bool(ast.get("has_cte", False)),
    }


def ast_predicate_family(ast: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "comparison_operators": _sorted_unique(ast.get("comparison_operators") or []),
        "logical_operators": _sorted_unique(ast.get("logical_operators") or []),
        "null_checks": _sorted_unique(ast.get("null_checks") or []),
        "predicate_values": _sorted_unique(ast.get("predicate_values") or []),
    }


def build_case_structure_summary(pred_ast: Dict[str, Any], gold_ast: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "pred_table_family": ast_table_family(pred_ast),
        "gold_table_family": ast_table_family(gold_ast),
        "pred_output_contract": ast_output_contract_family(pred_ast),
        "gold_output_contract": ast_output_contract_family(gold_ast),
        "pred_function_family": ast_function_family(pred_ast),
        "gold_function_family": ast_function_family(gold_ast),
        "pred_predicate_family": ast_predicate_family(pred_ast),
        "gold_predicate_family": ast_predicate_family(gold_ast),
    }


def structure_summary_to_text(summary: Dict[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)


def _table_signature_from_pair(pred_ast: Dict[str, Any], gold_ast: Dict[str, Any]) -> Tuple[str, ...]:
    pred_family = ast_table_family(pred_ast)
    gold_family = ast_table_family(gold_ast)
    return tuple(
        [
            pred_family["from_table"],
            gold_family["from_table"],
            ",".join(pred_family["tables"]),
            ",".join(gold_family["tables"]),
        ]
    )


def _output_signature_from_pair(pred_ast: Dict[str, Any], gold_ast: Dict[str, Any]) -> Tuple[str, ...]:
    pred_output = ast_output_contract_family(pred_ast)
    gold_output = ast_output_contract_family(gold_ast)
    return tuple(
        [
            str(pred_output["output_arity"]),
            str(gold_output["output_arity"]),
            "distinct" if gold_output["has_distinct"] else "no_distinct",
            ",".join(gold_output["column_roles"]),
        ]
    )


def _function_signature_from_pair(pred_ast: Dict[str, Any], gold_ast: Dict[str, Any]) -> Tuple[str, ...]:
    pred_families = ast_function_family(pred_ast)
    gold_families = ast_function_family(gold_ast)
    active = sorted(
        {
            family
            for family in SPECIAL_FUNCTION_FAMILIES
            if pred_families.get(family) or gold_families.get(family)
        }
    )
    return tuple(active)


def coarse_signature_dict_from_sql(pred_sql: str, gold_sql: str) -> Dict[str, Any]:
    pred_ast = cached_ast_signature(pred_sql)
    gold_ast = cached_ast_signature(gold_sql)
    return {
        "table_signature": list(_table_signature_from_pair(pred_ast, gold_ast)),
        "output_signature": list(_output_signature_from_pair(pred_ast, gold_ast)),
        "function_signature": list(_function_signature_from_pair(pred_ast, gold_ast)),
    }


def coarse_signature_key_from_sql(pred_sql: str, gold_sql: str) -> Tuple[str, ...]:
    signature = coarse_signature_dict_from_sql(pred_sql, gold_sql)
    return (
        "|".join(signature["table_signature"]),
        "|".join(signature["output_signature"]),
        "|".join(signature["function_signature"]),
    )


def _sql_has_conditional_value_map(sql: str) -> bool:
    sql_upper = str(sql or "").upper()
    return bool(re.search(r"\b(CASE|IIF)\b", sql_upper))


def _sql_has_type_cast(sql: str) -> bool:
    sql_upper = str(sql or "").upper()
    return bool(re.search(r"\bCAST\s*\(", sql_upper))


def _sql_has_count_distinct(sql: str) -> bool:
    sql_upper = str(sql or "").upper()
    return bool(re.search(r"\bCOUNT\s*\(\s*DISTINCT\b", sql_upper))


def _sql_has_anti_relation(sql: str) -> bool:
    sql_upper = str(sql or "").upper()
    return bool(re.search(r"\bNOT\s+IN\b|\bNOT\s+EXISTS\b|\bEXCEPT\b", sql_upper))


def _join_edge_signature(ast: Dict[str, Any]) -> Tuple[Tuple[str, str], ...]:
    edges = []
    for edge in ast.get("join_edges") or []:
        normalized_predicates = _normalize_join_condition(str(edge.get("on_clause") or edge.get("on") or ""))
        normalized_on = " && ".join(normalized_predicates)
        edges.append(
            (
                str(edge.get("join_type") or ""),
                normalized_on,
            )
        )
    return tuple(sorted(edges))


def _repair_delta_flags_from_ast(pred_ast: Dict[str, Any], gold_ast: Dict[str, Any]) -> Dict[str, bool]:
    pred_table = ast_table_family(pred_ast)
    gold_table = ast_table_family(gold_ast)
    pred_output = ast_output_contract_family(pred_ast)
    gold_output = ast_output_contract_family(gold_ast)
    pred_funcs = ast_function_family(pred_ast)
    gold_funcs = ast_function_family(gold_ast)
    pred_predicate = ast_predicate_family(pred_ast)
    gold_predicate = ast_predicate_family(gold_ast)
    return {
        "join_tables_differ": set(pred_table["tables"]) != set(gold_table["tables"]),
        "join_on_differ": _join_edge_signature(pred_ast) != _join_edge_signature(gold_ast),
        "select_columns_differ": (
            pred_output["select_refs"] != gold_output["select_refs"]
            or pred_output["select_exprs"] != gold_output["select_exprs"]
        ),
        "output_arity_differ": pred_output["output_arity"] != gold_output["output_arity"],
        "aggregate_differ": pred_output["has_aggregate"] != gold_output["has_aggregate"],
        "conditional_expr_differ": pred_funcs["conditional_expr"] != gold_funcs["conditional_expr"],
        "type_cast_differ": pred_funcs["type_cast"] != gold_funcs["type_cast"],
        "where_differ": pred_predicate["predicate_values"] != gold_predicate["predicate_values"],
        "null_handling_differ": pred_predicate["null_checks"] != gold_predicate["null_checks"],
        "comparison_operator_differ": pred_predicate["comparison_operators"] != gold_predicate["comparison_operators"],
        "logical_operator_differ": pred_predicate["logical_operators"] != gold_predicate["logical_operators"],
    }


def repair_signature_dict_from_sql(
    pred_sql: str,
    gold_sql: str,
    *,
    delta_flags: Dict[str, bool] | None = None,
) -> Dict[str, Any]:
    pred_ast = cached_ast_signature(pred_sql)
    gold_ast = cached_ast_signature(gold_sql)
    flags = dict(delta_flags or _repair_delta_flags_from_ast(pred_ast, gold_ast))
    pred_output = ast_output_contract_family(pred_ast)
    gold_output = ast_output_contract_family(gold_ast)
    pred_projection_width = max(len(pred_output["select_refs"]), len(pred_output["select_exprs"]))
    gold_projection_width = max(len(gold_output["select_refs"]), len(gold_output["select_exprs"]))

    pred_has_conditional = bool(ast_function_family(pred_ast)["conditional_expr"]) or _sql_has_conditional_value_map(pred_sql)
    gold_has_conditional = bool(ast_function_family(gold_ast)["conditional_expr"]) or _sql_has_conditional_value_map(gold_sql)
    pred_has_cast = bool(ast_function_family(pred_ast)["type_cast"]) or _sql_has_type_cast(pred_sql)
    gold_has_cast = bool(ast_function_family(gold_ast)["type_cast"]) or _sql_has_type_cast(gold_sql)
    pred_has_count_distinct = _sql_has_count_distinct(pred_sql)
    gold_has_count_distinct = _sql_has_count_distinct(gold_sql)

    matched_conditions: List[str] = []

    is_value_mapping = bool(flags.get("conditional_expr_differ") or flags.get("type_cast_differ"))
    if gold_has_conditional and not pred_has_conditional:
        is_value_mapping = True
    if is_value_mapping:
        matched_conditions.append("value_mapping")

    is_projection_contract = bool(
        (
            flags.get("output_arity_differ")
            or pred_projection_width > gold_projection_width
        )
        and (gold_output["output_arity"] or gold_projection_width or 0) <= (pred_output["output_arity"] or pred_projection_width or 0)
        and not gold_output["has_aggregate"]
    )
    if is_projection_contract:
        matched_conditions.append("projection_contract")

    is_dedup_counting_unit = bool(
        pred_output["has_aggregate"]
        and gold_output["has_aggregate"]
        and gold_has_count_distinct
        and not pred_has_count_distinct
    )
    if is_dedup_counting_unit:
        matched_conditions.append("dedup_counting_unit")

    is_join_path = bool(flags.get("join_tables_differ") or flags.get("join_on_differ"))
    if is_join_path:
        matched_conditions.append("join_path")

    if is_value_mapping:
        primary_edit_family = "value_mapping"
    elif is_projection_contract:
        primary_edit_family = "projection_contract"
    elif is_dedup_counting_unit:
        primary_edit_family = "dedup_counting_unit"
    elif is_join_path:
        primary_edit_family = "join_path"
    else:
        primary_edit_family = "other"

    if gold_has_conditional and not pred_has_conditional:
        special_transform_requirement = "conditional_value_map"
    elif gold_has_cast and not pred_has_cast:
        special_transform_requirement = "type_cast_required"
    else:
        special_transform_requirement = "none"

    edit_subfamily = ""
    if primary_edit_family == "join_path":
        pred_table_family = ast_table_family(pred_ast)
        gold_table_family = ast_table_family(gold_ast)
        pred_tables = set(pred_table_family["tables"])
        gold_tables = set(gold_table_family["tables"])
        pred_join_count = len(pred_ast.get("join_edges") or [])
        gold_join_count = len(gold_ast.get("join_edges") or [])
        gold_aggregate_context = bool(
            gold_output["has_aggregate"]
            or (gold_ast.get("group_by_keys") or [])
            or gold_has_count_distinct
        )

        if _sql_has_anti_relation(pred_sql) or _sql_has_anti_relation(gold_sql):
            edit_subfamily = "anti_relation"
        elif gold_aggregate_context:
            edit_subfamily = "aggregate_relation"
        elif (
            (gold_tables - pred_tables)
            or gold_join_count > pred_join_count
            or (len(gold_tables) > len(pred_tables))
        ):
            edit_subfamily = "bridge_relation"
        elif (
            (pred_tables - gold_tables)
            or pred_join_count > gold_join_count
            or (len(pred_tables) > len(gold_tables))
        ):
            edit_subfamily = "direct_relation"
        else:
            edit_subfamily = "generic"

    return {
        "primary_edit_family": primary_edit_family,
        "edit_subfamily": edit_subfamily,
        "target_output_contract": f"{pred_output['output_arity']}->{gold_output['output_arity']}",
        "special_transform_requirement": special_transform_requirement,
        "matched_conditions": matched_conditions,
        "debug": {
            "edit_subfamily": edit_subfamily,
            "pred_has_conditional": pred_has_conditional,
            "gold_has_conditional": gold_has_conditional,
            "pred_has_type_cast": pred_has_cast,
            "gold_has_type_cast": gold_has_cast,
            "pred_has_count_distinct": pred_has_count_distinct,
            "gold_has_count_distinct": gold_has_count_distinct,
            "gold_has_aggregate": gold_output["has_aggregate"],
            "pred_output_arity": pred_output["output_arity"],
            "gold_output_arity": gold_output["output_arity"],
            "pred_join_count": len(pred_ast.get("join_edges") or []),
            "gold_join_count": len(gold_ast.get("join_edges") or []),
            "pred_tables": sorted(pred_tables) if primary_edit_family == "join_path" else [],
            "gold_tables": sorted(gold_tables) if primary_edit_family == "join_path" else [],
        },
    }


def repair_signature_key_from_sql(
    pred_sql: str,
    gold_sql: str,
    *,
    delta_flags: Dict[str, bool] | None = None,
) -> Tuple[str, str, str, str]:
    signature = repair_signature_dict_from_sql(pred_sql, gold_sql, delta_flags=delta_flags)
    return (
        str(signature["primary_edit_family"]),
        str(signature.get("edit_subfamily", "") or ""),
        str(signature["target_output_contract"]),
        str(signature["special_transform_requirement"]),
    )


def repair_signature_key_from_instance(instance: Any) -> Tuple[str, str, str, str]:
    return repair_signature_key_from_sql(
        getattr(instance, "pred_sql", ""),
        getattr(instance, "gold_sql", ""),
    )


def repair_signature_dict_from_instance(instance: Any) -> Dict[str, Any]:
    flags_payload = _item_delta_flags_payload(instance)
    return repair_signature_dict_from_sql(
        str(_item_get(instance, "pred_sql", "") or ""),
        str(_item_get(instance, "gold_sql", "") or ""),
        delta_flags=flags_payload,
    )


def repair_signature_support(instances: Sequence[Any]) -> Dict[str, int]:
    counter: Counter[str] = Counter()
    for inst in instances:
        counter["|".join(repair_signature_key_from_instance(inst))] += 1
    return dict(sorted(counter.items()))


def dominant_repair_signature(instances: Sequence[Any]) -> Dict[str, Any]:
    if not instances:
        return {}
    ordered = sorted(
        [repair_signature_dict_from_instance(inst) for inst in instances],
        key=lambda item: (
            -sum(1 for other in instances if repair_signature_key_from_instance(other) == (
                str(item["primary_edit_family"]),
                str(item.get("edit_subfamily", "") or ""),
                str(item["target_output_contract"]),
                str(item["special_transform_requirement"]),
            )),
            str(item["primary_edit_family"]),
            str(item.get("edit_subfamily", "") or ""),
            str(item["target_output_contract"]),
            str(item["special_transform_requirement"]),
        ),
    )
    return dict(ordered[0])


def _structural_delta_get(item: Any, key: str, default: Any = None) -> Any:
    structural_delta = _item_get(item, "structural_delta")
    if structural_delta is None:
        return default
    if isinstance(structural_delta, dict):
        return structural_delta.get(key, default)
    return getattr(structural_delta, key, default)


def _is_non_empty_summary_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _normalize_summary_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_normalize_summary_value(v) for v in value]
    if isinstance(value, list):
        return [_normalize_summary_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _normalize_summary_value(v) for k, v in value.items()}
    return value


def _sequence_majority_summary(
    values: Sequence[Any],
    *,
    total: int,
    label: str,
) -> Dict[str, Any]:
    counter: Counter[Any] = Counter(values)
    non_empty_counter: Counter[Any] = Counter(v for v in values if _is_non_empty_summary_value(v))
    top_value: Any = None
    top_count = 0
    if non_empty_counter:
        top_value, top_count = non_empty_counter.most_common(1)[0]
    variants = [
        {
            "value": _normalize_summary_value(value),
            "count": count,
            "support_ratio": (count / total) if total else 0.0,
        }
        for value, count in non_empty_counter.most_common(5)
    ]
    support_ratio = (top_count / total) if total else 0.0
    return {
        "label": label,
        "dominant_value": _normalize_summary_value(top_value) if top_value is not None else [],
        "support_count": top_count,
        "support_ratio": support_ratio,
        "all_empty": not bool(non_empty_counter),
        "variants": variants,
    }


def _join_edge_tokens(edges: Any) -> Tuple[str, ...]:
    tokens: List[str] = []
    for edge in list(edges or []):
        table = str(_item_get(edge, "table", "") or "")
        join_type = str(_item_get(edge, "join_type", "") or "").lower()
        if not table:
            continue
        if join_type:
            tokens.append(f"{table}:{join_type}")
        else:
            tokens.append(table)
    return tuple(sorted(set(tokens)))


def shared_edit_operator_summary(
    instances: Sequence[Any],
    *,
    majority_threshold: float = PRIMARY_EDIT_MAJORITY_THRESHOLD,
    absolute_threshold: float = PRIMARY_EDIT_ABSOLUTE_THRESHOLD,
) -> Dict[str, Any]:
    total = len(instances)
    dominant_signature = dominant_repair_signature(instances)
    primary_edit_family = str(dominant_signature.get("primary_edit_family", "") or "")

    if total == 0:
        return {
            "primary_edit_family": primary_edit_family,
            "cluster_size": 0,
            "majority_threshold": majority_threshold,
            "absolute_threshold": absolute_threshold,
            "output_contract": {},
            "aggregate_context": {},
            "pred_only_tables": {},
            "gold_only_tables": {},
            "pred_only_joins": {},
            "gold_only_joins": {},
            "dominant_actions": [],
            "purity_score": 0.0,
            "distill_purity_gate_passed": False,
            "gate_reasons": ["empty_cluster"],
        }

    output_contract_values = [
        str(repair_signature_dict_from_instance(inst).get("target_output_contract", "") or "")
        for inst in instances
    ]
    aggregate_context_values = [
        bool(
            _structural_delta_get(inst, "has_aggregate_gold", False)
            or (_structural_delta_get(inst, "group_by_gold", []) or [])
            or bool(_structural_delta_get(inst, "has_distinct_gold", False))
        )
        for inst in instances
    ]
    pred_only_tables_values: List[Tuple[str, ...]] = []
    gold_only_tables_values: List[Tuple[str, ...]] = []
    pred_only_join_values: List[Tuple[str, ...]] = []
    gold_only_join_values: List[Tuple[str, ...]] = []

    for inst in instances:
        pred_tables = set(_structural_delta_get(inst, "pred_tables", []) or [])
        gold_tables = set(_structural_delta_get(inst, "gold_tables", []) or [])
        pred_only_tables_values.append(tuple(sorted(pred_tables - gold_tables)))
        gold_only_tables_values.append(tuple(sorted(gold_tables - pred_tables)))
        pred_only_join_values.append(_join_edge_tokens(_structural_delta_get(inst, "pred_only_joins", []) or []))
        gold_only_join_values.append(_join_edge_tokens(_structural_delta_get(inst, "gold_only_joins", []) or []))

    output_contract_summary = _sequence_majority_summary(
        output_contract_values,
        total=total,
        label="output_contract",
    )
    aggregate_context_summary = _sequence_majority_summary(
        aggregate_context_values,
        total=total,
        label="aggregate_context",
    )
    pred_only_tables_summary = _sequence_majority_summary(
        pred_only_tables_values,
        total=total,
        label="pred_only_tables",
    )
    gold_only_tables_summary = _sequence_majority_summary(
        gold_only_tables_values,
        total=total,
        label="gold_only_tables",
    )
    pred_only_joins_summary = _sequence_majority_summary(
        pred_only_join_values,
        total=total,
        label="pred_only_joins",
    )
    gold_only_joins_summary = _sequence_majority_summary(
        gold_only_join_values,
        total=total,
        label="gold_only_joins",
    )

    join_action_candidates = [
        pred_only_tables_summary,
        gold_only_tables_summary,
        pred_only_joins_summary,
        gold_only_joins_summary,
    ]
    dominant_actions = [
        {
            "label": item["label"],
            "value": item["dominant_value"],
            "support_ratio": item["support_ratio"],
            "support_count": item["support_count"],
        }
        for item in join_action_candidates
        if (not item["all_empty"]) and item["support_ratio"] >= majority_threshold
    ]
    dominant_actions.sort(key=lambda item: (-float(item["support_ratio"]), str(item["label"])))
    purity_score = max(
        [float(item["support_ratio"]) for item in join_action_candidates if not item["all_empty"]],
        default=0.0,
    )

    gate_reasons: List[str] = []
    if primary_edit_family != "join_path":
        gate_passed = True
        gate_reasons.append("non_join_path_family")
    elif dominant_actions:
        gate_passed = True
        gate_reasons.append("dominant_join_action_found")
    else:
        gate_passed = False
        gate_reasons.append("no_majority_primary_edit_action")

    gate_reasons.append(
        f"purity_score={purity_score:.2f},majority_threshold={majority_threshold:.2f},absolute_threshold={absolute_threshold:.2f}"
    )

    return {
        "primary_edit_family": primary_edit_family,
        "cluster_size": total,
        "majority_threshold": majority_threshold,
        "absolute_threshold": absolute_threshold,
        "output_contract": output_contract_summary,
        "aggregate_context": aggregate_context_summary,
        "pred_only_tables": pred_only_tables_summary,
        "gold_only_tables": gold_only_tables_summary,
        "pred_only_joins": pred_only_joins_summary,
        "gold_only_joins": gold_only_joins_summary,
        "dominant_actions": dominant_actions,
        "purity_score": purity_score,
        "distill_purity_gate_passed": gate_passed,
        "gate_reasons": gate_reasons,
    }


def category_aliases_from_instances(instances: Sequence[Any]) -> List[str]:
    return sorted(
        {
            str(getattr(inst, "error_category", "") or "")
            for inst in instances
            if str(getattr(inst, "error_category", "") or "").strip()
        }
    )


def dominant_category_from_instances(instances: Sequence[Any]) -> str:
    aliases = category_aliases_from_instances(instances)
    if not aliases:
        return ""
    counts: Counter[str] = Counter(
        str(getattr(inst, "error_category", "") or "")
        for inst in instances
        if str(getattr(inst, "error_category", "") or "").strip()
    )
    return sorted(aliases, key=lambda cat: (-counts[cat], cat))[0]


def dominant_parent_candidate_from_instances(instances: Sequence[Any]) -> str:
    counts: Counter[str] = Counter(
        str(getattr(inst, "evolution_parent_candidate", "") or "")
        for inst in instances
        if str(getattr(inst, "evolution_parent_candidate", "") or "").strip()
    )
    if not counts:
        return ""
    return sorted(counts.keys(), key=lambda key: (-counts[key], key))[0]


def build_repair_signature_diagnostics(instances: Sequence[Any]) -> Dict[str, Any]:
    ordered = sorted(instances, key=lambda inst: int(getattr(inst, "case_id", 0)))
    per_case: Dict[str, Dict[str, Any]] = {}
    same_category_diff_repair_signature: List[Dict[str, Any]] = []
    same_category_same_repair_but_struct_incompatible: List[Dict[str, Any]] = []
    cross_category_same_repair_and_struct_compatible: List[Dict[str, Any]] = []

    for inst in ordered:
        signature = repair_signature_dict_from_instance(inst)
        per_case[str(getattr(inst, "case_id", ""))] = {
            "db_id": str(getattr(inst, "db_id", "") or ""),
            "error_category": str(getattr(inst, "error_category", "") or ""),
            "active_dimensions": list(getattr(getattr(inst, "structural_delta", None), "delta_flags", None).active_dimensions())
            if getattr(getattr(inst, "structural_delta", None), "delta_flags", None) is not None
            else [],
            "matched_conditions": list(signature["matched_conditions"]),
            "primary_edit_family": str(signature["primary_edit_family"]),
            "edit_subfamily": str(signature.get("edit_subfamily", "") or ""),
            "target_output_contract": str(signature["target_output_contract"]),
            "special_transform_requirement": str(signature["special_transform_requirement"]),
        }

    for idx, left in enumerate(ordered):
        left_case = str(getattr(left, "case_id", ""))
        left_signature = per_case[left_case]
        for right in ordered[idx + 1 :]:
            if str(getattr(left, "db_id", "")) != str(getattr(right, "db_id", "")):
                continue
            right_case = str(getattr(right, "case_id", ""))
            right_signature = per_case[right_case]
            left_key = (
                left_signature["primary_edit_family"],
                left_signature["edit_subfamily"],
                left_signature["target_output_contract"],
                left_signature["special_transform_requirement"],
            )
            right_key = (
                right_signature["primary_edit_family"],
                right_signature["edit_subfamily"],
                right_signature["target_output_contract"],
                right_signature["special_transform_requirement"],
            )
            compatible = instances_structurally_compatible(left, right)

            if (
                left_signature["error_category"] == right_signature["error_category"]
                and left_key != right_key
            ):
                same_category_diff_repair_signature.append(
                    {
                        "case_a": left_case,
                        "case_b": right_case,
                        "db_id": str(getattr(left, "db_id", "") or ""),
                        "error_category": left_signature["error_category"],
                        "repair_signature_a": left_key,
                        "repair_signature_b": right_key,
                    }
                )

            if (
                left_signature["error_category"] == right_signature["error_category"]
                and left_key == right_key
                and not compatible
            ):
                same_category_same_repair_but_struct_incompatible.append(
                    {
                        "case_a": left_case,
                        "case_b": right_case,
                        "db_id": str(getattr(left, "db_id", "") or ""),
                        "error_category": left_signature["error_category"],
                        "repair_signature": left_key,
                    }
                )

            if (
                left_signature["error_category"] != right_signature["error_category"]
                and left_key == right_key
                and compatible
            ):
                cross_category_same_repair_and_struct_compatible.append(
                    {
                        "case_a": left_case,
                        "case_b": right_case,
                        "db_id": str(getattr(left, "db_id", "") or ""),
                        "error_category_a": left_signature["error_category"],
                        "error_category_b": right_signature["error_category"],
                        "repair_signature": left_key,
                    }
                )

    return {
        "repair_signature_by_case": per_case,
        "same_category_diff_repair_signature": same_category_diff_repair_signature,
        "same_category_diff_repair_signature_count": len(same_category_diff_repair_signature),
        "same_category_same_repair_but_struct_incompatible": same_category_same_repair_but_struct_incompatible,
        "same_category_same_repair_but_struct_incompatible_count": len(same_category_same_repair_but_struct_incompatible),
        "cross_category_same_repair_and_struct_compatible": cross_category_same_repair_and_struct_compatible,
        "cross_category_same_repair_and_struct_compatible_count": len(cross_category_same_repair_and_struct_compatible),
    }


def discovery_bucket_key_from_sql(pred_sql: str, gold_sql: str) -> Tuple[str, ...]:
    """Backward-compatible alias for the active repair-signature discovery key."""
    return repair_signature_key_from_sql(pred_sql, gold_sql)


def instances_structurally_compatible(a: Any, b: Any) -> bool:
    return structural_compatibility_explain(a, b)["compatible"]


def structural_compatibility_explain(a: Any, b: Any) -> Dict[str, Any]:
    a_pred = cached_ast_signature(str(_item_get(a, "pred_sql", "") or ""))
    a_gold = cached_ast_signature(str(_item_get(a, "gold_sql", "") or ""))
    b_pred = cached_ast_signature(str(_item_get(b, "pred_sql", "") or ""))
    b_gold = cached_ast_signature(str(_item_get(b, "gold_sql", "") or ""))
    a_signature = repair_signature_dict_from_instance(a)
    b_signature = repair_signature_dict_from_instance(b)

    table_family = _table_family_analysis(a_pred, a_gold, b_pred, b_gold)
    output_contract = _output_contract_analysis(a_pred, a_gold, b_pred, b_gold)
    function_family = _function_family_analysis(
        a_pred,
        a_gold,
        b_pred,
        b_gold,
        primary_edit_family_a=str(a_signature.get("primary_edit_family", "") or ""),
        primary_edit_family_b=str(b_signature.get("primary_edit_family", "") or ""),
    )
    compatible = bool(
        table_family["compatible"]
        and output_contract["compatible"]
        and function_family["compatible"]
    )
    blocked_gates = [
        gate
        for gate, payload in (
            ("table_family", table_family),
            ("output_contract", output_contract),
            ("function_family", function_family),
        )
        if not payload["compatible"]
    ]
    return {
        "compatible": compatible,
        "blocked_gates": blocked_gates,
        "repair_signature_a": dict(a_signature),
        "repair_signature_b": dict(b_signature),
        "table_family": table_family,
        "output_contract": output_contract,
        "function_family": function_family,
    }


def _table_family_analysis(
    a_pred: Dict[str, Any],
    a_gold: Dict[str, Any],
    b_pred: Dict[str, Any],
    b_gold: Dict[str, Any],
) -> Dict[str, Any]:
    a_tables = set(ast_table_family(a_pred)["tables"]) | set(ast_table_family(a_gold)["tables"])
    b_tables = set(ast_table_family(b_pred)["tables"]) | set(ast_table_family(b_gold)["tables"])
    reasons: List[str] = []
    if not a_tables or not b_tables:
        reasons.append("empty_table_family")
        return {
            "compatible": True,
            "reasons": reasons,
            "left_tables": sorted(a_tables),
            "right_tables": sorted(b_tables),
            "overlap": 0,
            "union": 0,
            "jaccard": 1.0,
            "coverage": 1.0,
        }
    overlap = len(a_tables & b_tables)
    union = len(a_tables | b_tables)
    if union == 0:
        reasons.append("empty_union")
        return {
            "compatible": True,
            "reasons": reasons,
            "left_tables": sorted(a_tables),
            "right_tables": sorted(b_tables),
            "overlap": overlap,
            "union": union,
            "jaccard": 1.0,
            "coverage": 1.0,
        }
    if overlap == 0:
        reasons.append("no_table_overlap")
        return {
            "compatible": False,
            "reasons": reasons,
            "left_tables": sorted(a_tables),
            "right_tables": sorted(b_tables),
            "overlap": overlap,
            "union": union,
            "jaccard": 0.0,
            "coverage": 0.0,
        }
    smaller = min(len(a_tables), len(b_tables))
    if smaller and overlap == smaller:
        reasons.append("subset_table_overlap")
        return {
            "compatible": True,
            "reasons": reasons,
            "left_tables": sorted(a_tables),
            "right_tables": sorted(b_tables),
            "overlap": overlap,
            "union": union,
            "jaccard": overlap / union if union else 1.0,
            "coverage": 1.0,
        }
    jaccard = overlap / union
    coverage = overlap / smaller if smaller else 1.0
    if jaccard < 0.30 and coverage < 0.60:
        reasons.append(f"low_table_overlap:jaccard={jaccard:.2f},coverage={coverage:.2f}")
        compatible = False
    else:
        reasons.append(f"table_overlap:jaccard={jaccard:.2f},coverage={coverage:.2f}")
        compatible = True
    return {
        "compatible": compatible,
        "reasons": reasons,
        "left_tables": sorted(a_tables),
        "right_tables": sorted(b_tables),
        "overlap": overlap,
        "union": union,
        "jaccard": jaccard,
        "coverage": coverage,
    }


def _output_contract_analysis(
    a_pred: Dict[str, Any],
    a_gold: Dict[str, Any],
    b_pred: Dict[str, Any],
    b_gold: Dict[str, Any],
) -> Dict[str, Any]:
    a_out = ast_output_contract_family(a_gold)
    b_out = ast_output_contract_family(b_gold)
    reasons: List[str] = []
    if a_out["output_arity"] and b_out["output_arity"] and a_out["output_arity"] != b_out["output_arity"]:
        reasons.append(f"output_arity_mismatch:{a_out['output_arity']}!={b_out['output_arity']}")
        return {
            "compatible": False,
            "reasons": reasons,
            "left": a_out,
            "right": b_out,
        }
    a_roles = set(a_out["column_roles"])
    b_roles = set(b_out["column_roles"])
    if a_roles and b_roles and not (a_roles & b_roles):
        reasons.append(f"column_role_mismatch:{sorted(a_roles)}!={sorted(b_roles)}")
        return {
            "compatible": False,
            "reasons": reasons,
            "left": a_out,
            "right": b_out,
        }
    if a_roles and b_roles:
        reasons.append(f"column_role_overlap:{sorted(a_roles & b_roles)}")
    else:
        reasons.append("column_role_overlap:unspecified")
    return {
        "compatible": True,
        "reasons": reasons,
        "left": a_out,
        "right": b_out,
    }


def _function_family_analysis(
    a_pred: Dict[str, Any],
    a_gold: Dict[str, Any],
    b_pred: Dict[str, Any],
    b_gold: Dict[str, Any],
    *,
    primary_edit_family_a: str = "",
    primary_edit_family_b: str = "",
) -> Dict[str, Any]:
    a_active = {
        family
        for family, enabled in ast_function_family(a_pred).items()
        if enabled
    } | {
        family
        for family, enabled in ast_function_family(a_gold).items()
        if enabled
    }
    b_active = {
        family
        for family, enabled in ast_function_family(b_pred).items()
        if enabled
    } | {
        family
        for family, enabled in ast_function_family(b_gold).items()
        if enabled
    }
    special_a = a_active & set(SPECIAL_FUNCTION_FAMILIES)
    special_b = b_active & set(SPECIAL_FUNCTION_FAMILIES)
    wrapper_insensitive_applied = (
        primary_edit_family_a == "join_path"
        and primary_edit_family_b == "join_path"
    )
    compare_a = set(special_a)
    compare_b = set(special_b)
    ignored_wrapper_families: List[str] = []
    ignored_soft_families: List[str] = []
    if wrapper_insensitive_applied:
        ignored_wrapper_families = sorted((special_a ^ special_b) & WRAPPER_FUNCTION_FAMILIES)
        compare_a -= WRAPPER_FUNCTION_FAMILIES
        compare_b -= WRAPPER_FUNCTION_FAMILIES
        ignored_soft_families = sorted((compare_a ^ compare_b) & SOFT_JOIN_PATH_FUNCTION_FAMILIES)
        compare_a -= SOFT_JOIN_PATH_FUNCTION_FAMILIES
        compare_b -= SOFT_JOIN_PATH_FUNCTION_FAMILIES
    compatible = compare_a == compare_b
    hard_left_only = sorted(compare_a - compare_b)
    hard_right_only = sorted(compare_b - compare_a)
    reasons: List[str] = []
    if compatible:
        reasons.append(f"special_function_match:{sorted(compare_a)}")
        if ignored_wrapper_families:
            reasons.append(f"ignored_wrapper_families:{ignored_wrapper_families}")
        if ignored_soft_families:
            reasons.append(f"ignored_soft_families:{ignored_soft_families}")
    else:
        reasons.append(f"special_function_mismatch:left={sorted(compare_a)},right={sorted(compare_b)}")
        if ignored_wrapper_families:
            reasons.append(f"ignored_wrapper_families:{ignored_wrapper_families}")
        if ignored_soft_families:
            reasons.append(f"ignored_soft_families:{ignored_soft_families}")
    return {
        "compatible": compatible,
        "reasons": reasons,
        "left_active": sorted(a_active),
        "right_active": sorted(b_active),
        "left_special": sorted(special_a),
        "right_special": sorted(special_b),
        "left_hard_special": sorted(compare_a),
        "right_hard_special": sorted(compare_b),
        "ignored_wrapper_families": ignored_wrapper_families,
        "ignored_soft_families": ignored_soft_families,
        "hard_function_mismatch": {
            "left_only": hard_left_only,
            "right_only": hard_right_only,
        },
        "wrapper_insensitive_applied": wrapper_insensitive_applied,
        "primary_edit_family_a": primary_edit_family_a,
        "primary_edit_family_b": primary_edit_family_b,
    }


def build_cluster_support_summary(instances: Sequence[Any]) -> Dict[str, Any]:
    total = len(instances)
    table_counter: Counter[str] = Counter()
    pred_from_counter: Counter[str] = Counter()
    gold_from_counter: Counter[str] = Counter()
    output_counter: Counter[str] = Counter()
    function_counter: Counter[str] = Counter()
    delta_counter: Counter[str] = Counter()

    for inst in instances:
        pred_ast = cached_ast_signature(getattr(inst, "pred_sql", ""))
        gold_ast = cached_ast_signature(getattr(inst, "gold_sql", ""))
        for table in ast_table_family(pred_ast)["tables"]:
            table_counter[table] += 1
        if ast_table_family(pred_ast)["from_table"]:
            pred_from_counter[ast_table_family(pred_ast)["from_table"]] += 1
        if ast_table_family(gold_ast)["from_table"]:
            gold_from_counter[ast_table_family(gold_ast)["from_table"]] += 1
        out = ast_output_contract_family(gold_ast)
        output_counter.update(out["column_roles"] or ["other"])
        for family, enabled in ast_function_family(pred_ast).items():
            if enabled:
                function_counter[family] += 1
        for family, enabled in ast_function_family(gold_ast).items():
            if enabled:
                function_counter[family] += 1
        delta_counter.update(getattr(inst.structural_delta.delta_flags, "active_dimensions")())

    threshold = max(1, int(total * 0.6))
    return {
        "support_threshold": threshold,
        "shared_tables": sorted([k for k, v in table_counter.items() if v >= threshold]),
        "pred_from_modes": pred_from_counter.most_common(3),
        "gold_from_modes": gold_from_counter.most_common(3),
        "shared_output_roles": sorted([k for k, v in output_counter.items() if v >= threshold and k != "other"]),
        "shared_function_families": sorted([k for k, v in function_counter.items() if v >= threshold]),
        "shared_delta_dimensions": sorted([k for k, v in delta_counter.items() if v >= threshold]),
        "table_support": dict(table_counter),
        "output_role_support": dict(output_counter),
        "function_support": dict(function_counter),
        "delta_support": dict(delta_counter),
    }


def build_trigger_signature(instances: Sequence[Any]) -> Dict[str, Any]:
    total = len(instances)
    threshold = max(1, int(total * 0.6))

    table_counter: Counter[str] = Counter()
    table_set_counter: Counter[Tuple[str, ...]] = Counter()
    from_counter: Counter[str] = Counter()
    arity_counter: Counter[int] = Counter()
    role_counter: Counter[str] = Counter()
    function_counter: Counter[str] = Counter()
    null_counter: Counter[str] = Counter()
    delta_counter: Counter[str] = Counter()
    join_key_counter: Counter[str] = Counter()

    for inst in instances:
        pred_ast = cached_ast_signature(getattr(inst, "pred_sql", ""))
        table_family = ast_table_family(pred_ast)
        output_family = ast_output_contract_family(pred_ast)
        function_family = ast_function_family(pred_ast)
        predicate_family = ast_predicate_family(pred_ast)

        table_counter.update(table_family["tables"])
        table_set_counter[tuple(table_family["tables"])] += 1
        if table_family["from_table"]:
            from_counter[table_family["from_table"]] += 1
        if output_family["output_arity"]:
            arity_counter[output_family["output_arity"]] += 1
        role_counter.update(output_family["column_roles"] or ["other"])
        for family, enabled in function_family.items():
            if enabled:
                function_counter[family] += 1
        null_counter.update(predicate_family["null_checks"])
        delta_counter.update(getattr(inst.structural_delta.delta_flags, "active_dimensions")())
        join_key_counter.update(normalized_join_key_tokens_from_ast(pred_ast))

    return {
        "version": STRUCTURE_SIGNATURE_VERSION,
        "required_tables": sorted([k for k, v in table_counter.items() if v >= threshold]),
        "table_set_prototypes": [
            list(key)
            for key, count in sorted(table_set_counter.items())
            if count >= max(1, int(total * 0.4))
        ],
        "preferred_from_tables": sorted([k for k, v in from_counter.items() if v >= threshold]),
        "allowed_output_arities": sorted([k for k, v in arity_counter.items() if v >= threshold]),
        "preferred_output_roles": sorted([k for k, v in role_counter.items() if v >= threshold and k != "other"]),
        "required_function_families": sorted([k for k, v in function_counter.items() if v >= threshold]),
        "preferred_null_checks": sorted([k for k, v in null_counter.items() if v >= threshold]),
        "preferred_join_keys": sorted([k for k, v in join_key_counter.items() if v >= threshold]),
        "shared_delta_dimensions": sorted([k for k, v in delta_counter.items() if v >= threshold]),
    }


def score_case_against_trigger_signature(pred_ast: Dict[str, Any], signature: Dict[str, Any]) -> Dict[str, Any]:
    table_family = ast_table_family(pred_ast)
    output_family = ast_output_contract_family(pred_ast)
    function_family = ast_function_family(pred_ast)
    predicate_family = ast_predicate_family(pred_ast)
    join_key_tokens = set(normalized_join_key_tokens_from_ast(pred_ast))

    reasons: List[str] = []
    score = 0.0
    compatible = True

    required_tables = set(signature.get("required_tables") or [])
    case_tables = set(table_family["tables"])
    table_set_prototypes = [set(proto) for proto in (signature.get("table_set_prototypes") or []) if proto]
    if table_set_prototypes:
        best_proto_overlap = 0.0
        for proto in table_set_prototypes:
            union = len(proto | case_tables)
            overlap = len(proto & case_tables)
            best_proto_overlap = max(best_proto_overlap, overlap / union if union else 1.0)
        if best_proto_overlap < 0.5:
            compatible = False
            reasons.append(f"table_set_proto_miss={sorted(case_tables)}")
        else:
            score += best_proto_overlap
            reasons.append(f"table_set_proto_overlap={best_proto_overlap:.2f}")

    if required_tables:
        overlap = required_tables & case_tables
        if not overlap:
            compatible = False
            reasons.append("no_required_table_overlap")
        else:
            score += len(overlap) / max(len(required_tables), 1)
            reasons.append(f"table_overlap={sorted(overlap)}")

    preferred_from = set(signature.get("preferred_from_tables") or [])
    if preferred_from:
        if table_family["from_table"] in preferred_from:
            score += 0.5
            reasons.append(f"from_table={table_family['from_table']}")
        elif table_family["from_table"]:
            reasons.append(f"from_table_miss={table_family['from_table']}")

    allowed_arities = set(signature.get("allowed_output_arities") or [])
    if allowed_arities:
        arity = output_family["output_arity"]
        if arity not in allowed_arities:
            compatible = False
            reasons.append(f"output_arity_miss={arity}")
        else:
            score += 0.5
            reasons.append(f"output_arity={arity}")

    preferred_roles = set(signature.get("preferred_output_roles") or [])
    case_roles = set(output_family["column_roles"])
    if preferred_roles:
        if case_roles & preferred_roles:
            score += 0.5
            reasons.append(f"output_roles={sorted(case_roles & preferred_roles)}")
        else:
            reasons.append("output_role_miss")

    required_functions = set(signature.get("required_function_families") or [])
    active_functions = {k for k, enabled in function_family.items() if enabled}
    if required_functions:
        if required_functions & active_functions:
            score += 0.5
            reasons.append(f"function_overlap={sorted(required_functions & active_functions)}")
        else:
            reasons.append("function_overlap=none")

    preferred_null = set(signature.get("preferred_null_checks") or [])
    case_null = set(predicate_family["null_checks"])
    if preferred_null and (preferred_null & case_null):
        score += 0.25
        reasons.append(f"null_checks={sorted(preferred_null & case_null)}")

    preferred_join_keys = set(signature.get("preferred_join_keys") or [])
    if preferred_join_keys:
        if join_key_tokens & preferred_join_keys:
            score += 0.4
            reasons.append(f"join_keys={sorted(join_key_tokens & preferred_join_keys)}")
        else:
            reasons.append("join_key_miss")

    return {
        "compatible": compatible,
        "score": score,
        "table_family": table_family,
        "output_contract_family": output_family,
        "function_family": function_family,
        "predicate_family": predicate_family,
        "reasons": reasons,
    }
