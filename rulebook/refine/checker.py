from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

import sqlglot
from sqlglot import exp


@dataclass
class SQLComplianceReport:
    valid: bool
    violations: List[str]
    tables_referenced: List[str]
    columns_referenced: List[Tuple[Optional[str], str]]


def _lower_set(items: List[str]) -> Set[str]:
    return {str(x).lower() for x in items if str(x)}


def validate_sql_columns(
    sql: str,
    *,
    schema_tables: Dict[str, List[str]],
    dialect: str = "sqlite",
) -> SQLComplianceReport:
    """
    Best-effort schema compliance check:
    - Tables referenced must exist in schema_tables (case-insensitive).
    - Qualified columns (t.col) must exist in the resolved table when possible.
    - Unqualified columns must exist in at least one referenced base table.

    Notes:
    - Handles table aliases for simple FROM/JOIN chains.
    - Skips column validation when the qualifier refers to a subquery alias.
    - This is conservative: it may allow some invalid queries and may flag some
      advanced queries; caller should still rely on execution errors as truth.
    """
    sql = (sql or "").strip()
    if not sql:
        return SQLComplianceReport(
            valid=False,
            violations=["EMPTY_SQL"],
            tables_referenced=[],
            columns_referenced=[],
        )

    violations: List[str] = []
    tables_ref: List[str] = []
    cols_ref: List[Tuple[Optional[str], str]] = []

    schema_tables_lc = {k.lower(): k for k in (schema_tables or {}).keys()}
    schema_cols_lc = {k.lower(): _lower_set(v) for k, v in (schema_tables or {}).items()}

    try:
        parsed = sqlglot.parse_one(sql, dialect=dialect)
    except Exception as exc:
        return SQLComplianceReport(
            valid=False,
            violations=[f"PARSE_ERROR: {exc}"],
            tables_referenced=[],
            columns_referenced=[],
        )

    # Collect subquery aliases so we don't incorrectly validate columns qualified by them.
    subquery_aliases: Set[str] = set()
    for subq in parsed.find_all(exp.Subquery):
        alias = subq.alias
        if alias and alias != subq:
            name = alias.this.name if getattr(alias, "this", None) is not None else None
            if name:
                subquery_aliases.add(str(name).lower())

    # Build alias -> base table mapping for simple table refs.
    alias_to_table: Dict[str, str] = {}
    for tbl in parsed.find_all(exp.Table):
        base = str(tbl.name or "").strip()
        if not base:
            continue
        tables_ref.append(base)
        base_lc = base.lower()
        # Map base name to itself.
        alias_to_table[base_lc] = base_lc
        # Map alias if present.
        if tbl.alias and tbl.alias != tbl:
            alias_name = tbl.alias_or_name
            if alias_name:
                alias_to_table[str(alias_name).lower()] = base_lc

        if base_lc not in schema_tables_lc:
            violations.append(f"UNKNOWN_TABLE: {base}")

    referenced_base_tables: Set[str] = set(alias_to_table.values())

    # Validate columns.
    for col in parsed.find_all(exp.Column):
        col_name = str(col.name or "").strip()
        if not col_name or col_name == "*":
            continue
        table_qual = str(col.table or "").strip() if col.table else None
        table_qual_lc = table_qual.lower() if table_qual else None
        cols_ref.append((table_qual, col_name))

        # Qualified column: try to resolve alias -> base table.
        if table_qual_lc:
            if table_qual_lc in subquery_aliases:
                continue
            resolved = alias_to_table.get(table_qual_lc, table_qual_lc)
            if resolved not in schema_tables_lc:
                # Could be a CTE/subquery alias (not always captured); do not hard-fail,
                # but record a soft violation for debugging.
                violations.append(f"UNKNOWN_COLUMN_QUALIFIER: {table_qual}")
                continue
            allowed = schema_cols_lc.get(resolved, set())
            if col_name.lower() not in allowed:
                violations.append(f"UNKNOWN_COLUMN: {table_qual}.{col_name}")
            continue

        # Unqualified: must exist in at least one referenced base table.
        if referenced_base_tables:
            ok_any = False
            for base_lc in referenced_base_tables:
                allowed = schema_cols_lc.get(base_lc, set())
                if col_name.lower() in allowed:
                    ok_any = True
                    break
            if not ok_any:
                violations.append(f"UNKNOWN_COLUMN_UNQUALIFIED: {col_name}")

    valid = not any(v.startswith(("PARSE_ERROR", "EMPTY_SQL", "UNKNOWN_TABLE", "UNKNOWN_COLUMN")) for v in violations)
    return SQLComplianceReport(
        valid=valid,
        violations=violations,
        tables_referenced=sorted(set(tables_ref)),
        columns_referenced=cols_ref,
    )


