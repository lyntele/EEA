from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import sqlglot
from sqlglot import exp


WINDOW_FUNCTION_NAMES = {
    "ROW_NUMBER", "RANK", "DENSE_RANK", "LAG", "LEAD", "FIRST_VALUE",
    "LAST_VALUE", "NTILE", "PERCENT_RANK", "CUME_DIST",
}
DATE_TIME_FUNCTION_NAMES = {
    "DATE", "DATETIME", "TIME", "STRFTIME", "JULIANDAY", "TIMEDIFF",
    "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP", "EXTRACT",
}
STRING_FUNCTION_NAMES = {
    "SUBSTR", "SUBSTRING", "CONCAT", "REPLACE", "TRIM", "LTRIM", "RTRIM",
    "UPPER", "LOWER", "LENGTH", "LIKE", "ILIKE", "INSTR",
}
MATH_FUNCTION_NAMES = {
    "ROUND", "ABS", "MOD", "CEIL", "CEILING", "FLOOR", "POWER", "SQRT",
    "RAND", "RANDOM",
}
CONDITIONAL_EXPRESSION_NAMES = {"CASE", "IF", "IIF", "COALESCE", "IFNULL", "NULLIF"}
TYPE_CAST_NAMES = {"CAST", "TRY_CAST", "SAFE_CAST"}


@dataclass
class JoinInfo:
    table: str
    join_type: str
    on_condition: str


@dataclass
class SQLStructure:
    from_table: str = ""
    tables: List[str] = field(default_factory=list)
    joins: List[JoinInfo] = field(default_factory=list)
    where_conditions: List[str] = field(default_factory=list)
    group_by_columns: List[str] = field(default_factory=list)
    having_conditions: List[str] = field(default_factory=list)
    select_expressions: List[str] = field(default_factory=list)
    select_column_refs: List[str] = field(default_factory=list)
    order_by: List[Dict[str, str]] = field(default_factory=list)
    limit: Optional[int] = None
    has_distinct: bool = False
    has_subquery: bool = False
    subqueries: List["SQLStructure"] = field(default_factory=list)
    has_cte: bool = False
    comparison_operators: List[str] = field(default_factory=list)
    logical_operators: List[str] = field(default_factory=list)
    null_checks: List[str] = field(default_factory=list)
    predicate_values: List[str] = field(default_factory=list)
    window_functions: List[str] = field(default_factory=list)
    date_time_functions: List[str] = field(default_factory=list)
    string_functions: List[str] = field(default_factory=list)
    math_functions: List[str] = field(default_factory=list)
    conditional_exprs: List[str] = field(default_factory=list)
    type_casts: List[str] = field(default_factory=list)
    set_operations: List[str] = field(default_factory=list)


def parse_sql_structure(sql: str, dialect: str = "sqlite") -> SQLStructure:
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
        return _extract_structure(parsed)
    except Exception:
        return SQLStructure()


def _extract_structure(node: exp.Expression) -> SQLStructure:
    struct = SQLStructure()

    from_clause = node.find(exp.From)
    if from_clause and from_clause.this is not None:
        struct.from_table = _extract_table_name(from_clause.this)

    for table in node.find_all(exp.Table):
        table_name = table.name
        if table_name:
            struct.tables.append(table_name)

    for join in node.find_all(exp.Join):
        table_name = _extract_table_name(join.this)
        struct.joins.append(
            JoinInfo(
                table=table_name,
                join_type=str(join.kind or "INNER").upper(),
                on_condition=_normalized_sql(join.args.get("on", "")),
            )
        )

    where = node.find(exp.Where)
    if where:
        struct.where_conditions = _split_conditions(where.this)

    group = node.find(exp.Group)
    if group:
        struct.group_by_columns = [_normalized_sql(col) for col in group.expressions]

    having = node.find(exp.Having)
    if having:
        struct.having_conditions = _split_conditions(having.this)

    select = node.find(exp.Select)
    if select:
        struct.select_expressions = [_normalized_sql(col) for col in select.expressions]
        struct.select_column_refs = sorted(
            {
                _normalized_column(col)
                for expr_node in select.expressions
                for col in expr_node.find_all(exp.Column)
            }
        )
        struct.has_distinct = bool(select.args.get("distinct", False))

    order = node.find(exp.Order)
    if order:
        struct.order_by = [_extract_order_expr(item) for item in order.expressions]

    limit = node.find(exp.Limit)
    if limit and limit.this and getattr(limit.this, "this", None) is not None:
        try:
            struct.limit = int(limit.this.this)
        except (TypeError, ValueError):
            struct.limit = None

    subqueries = list(node.find_all(exp.Subquery))
    struct.has_subquery = len(subqueries) > 0
    for sq in subqueries:
        if sq.this is not None:
            struct.subqueries.append(_extract_structure(sq.this))

    struct.has_cte = node.find(exp.With) is not None
    struct.comparison_operators = sorted(_collect_comparison_operators(node))
    struct.logical_operators = sorted(_collect_logical_operators(node))
    struct.null_checks = sorted(_collect_null_checks(struct.where_conditions + struct.having_conditions))
    struct.predicate_values = sorted(_collect_predicate_values(where, having))

    function_families = _collect_function_families(node)
    struct.window_functions = sorted(function_families["window_functions"])
    struct.date_time_functions = sorted(function_families["date_time_functions"])
    struct.string_functions = sorted(function_families["string_functions"])
    struct.math_functions = sorted(function_families["math_functions"])
    struct.conditional_exprs = sorted(function_families["conditional_exprs"])
    struct.type_casts = sorted(function_families["type_casts"])
    struct.set_operations = sorted(_collect_set_operations(node))

    struct.tables = sorted(set(struct.tables))
    return struct


def _extract_table_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Table):
        return node.name
    return _normalized_sql(node)


def _extract_order_expr(node: exp.Expression) -> Dict[str, str]:
    if isinstance(node, exp.Ordered):
        return {
            "column": _normalized_sql(node.this),
            "direction": "DESC" if bool(node.args.get("desc")) else "ASC",
        }
    return {"column": _normalized_sql(node), "direction": "ASC"}


def _normalized_sql(expr: exp.Expression | str | None) -> str:
    if expr is None:
        return ""
    if isinstance(expr, str):
        return " ".join(expr.split())
    return " ".join(expr.sql().split())


def _normalized_column(column: exp.Column) -> str:
    table = column.table
    name = column.name
    return f"{table}.{name}" if table else name


def _split_conditions(expr: exp.Expression) -> List[str]:
    if isinstance(expr, exp.And):
        return _split_conditions(expr.left) + _split_conditions(expr.right)
    return [_normalized_sql(expr)]


def _collect_comparison_operators(node: exp.Expression) -> Set[str]:
    operators: Set[str] = set()
    mapping = {
        exp.EQ: "=",
        exp.NEQ: "!=",
        exp.GT: ">",
        exp.GTE: ">=",
        exp.LT: "<",
        exp.LTE: "<=",
        exp.Like: "LIKE",
        exp.ILike: "ILIKE",
        exp.In: "IN",
        exp.Between: "BETWEEN",
    }
    for current in node.walk():
        for exp_type, label in mapping.items():
            if isinstance(current, exp_type):
                operators.add(label)
                break
    return operators


def _collect_logical_operators(node: exp.Expression) -> Set[str]:
    operators: Set[str] = set()
    mapping = {
        exp.And: "AND",
        exp.Or: "OR",
        exp.Not: "NOT",
    }
    for current in node.walk():
        for exp_type, label in mapping.items():
            if isinstance(current, exp_type):
                operators.add(label)
                break
    return operators


def _collect_null_checks(conditions: List[str]) -> Set[str]:
    checks: Set[str] = set()
    for condition in conditions:
        upper = condition.upper()
        if " IS NOT NULL" in upper:
            checks.add("IS NOT NULL")
        if " IS NULL" in upper and "IS NOT NULL" not in upper:
            checks.add("IS NULL")
        if "COALESCE(" in upper:
            checks.add("COALESCE")
    return checks


def _collect_predicate_values(where: exp.Where | None, having: exp.Having | None) -> Set[str]:
    values: Set[str] = set()
    for clause in (where, having):
        if clause is None or clause.this is None:
            continue
        for literal in clause.this.find_all(exp.Literal):
            values.add(_normalized_sql(literal))
    return values


def _collect_function_families(node: exp.Expression) -> Dict[str, Set[str]]:
    families: Dict[str, Set[str]] = {
        "window_functions": set(),
        "date_time_functions": set(),
        "string_functions": set(),
        "math_functions": set(),
        "conditional_exprs": set(),
        "type_casts": set(),
    }

    for current in node.walk():
        name = _function_name(current)
        if isinstance(current, exp.Window):
            families["window_functions"].add("WINDOW")
        if isinstance(current, exp.Case):
            families["conditional_exprs"].add("CASE")
        if isinstance(current, exp.Cast):
            families["type_casts"].add("CAST")
        if not name:
            continue
        if name in WINDOW_FUNCTION_NAMES:
            families["window_functions"].add(name)
        if name in DATE_TIME_FUNCTION_NAMES:
            families["date_time_functions"].add(name)
        if name in STRING_FUNCTION_NAMES:
            families["string_functions"].add(name)
        if name in MATH_FUNCTION_NAMES:
            families["math_functions"].add(name)
        if name in CONDITIONAL_EXPRESSION_NAMES:
            families["conditional_exprs"].add(name)
        if name in TYPE_CAST_NAMES:
            families["type_casts"].add(name)

    return families


def _function_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name or "").upper()
    if isinstance(node, exp.Cast):
        return "CAST"
    if isinstance(node, exp.Case):
        return "CASE"
    if hasattr(node, "sql_name"):
        try:
            sql_name = node.sql_name()
            if sql_name:
                return str(sql_name).upper()
        except Exception:
            pass
    key = getattr(node, "key", "")
    if key:
        return str(key).upper()
    return ""


def _collect_set_operations(node: exp.Expression) -> Set[str]:
    operations: Set[str] = set()
    mapping = {
        exp.Union: "UNION",
        exp.Except: "EXCEPT",
        exp.Intersect: "INTERSECT",
    }
    for current in node.walk():
        for exp_type, label in mapping.items():
            if isinstance(current, exp_type):
                operations.add(label)
                break
    return operations


def extract_base_count_sql(sql: str, dialect: str = "sqlite") -> str:
    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
        from_clause = parsed.find(exp.From)
        where_clause = parsed.find(exp.Where)

        count_sql = "SELECT COUNT(*) AS cnt"
        if from_clause:
            count_sql += f" {from_clause.sql(dialect=dialect)}"
        for join in parsed.find_all(exp.Join):
            count_sql += f" {join.sql(dialect=dialect)}"
        if where_clause:
            count_sql += f" {where_clause.sql(dialect=dialect)}"

        return count_sql
    except Exception:
        return ""
