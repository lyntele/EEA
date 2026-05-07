"""SQL role-graph normalization for canonical repair programs.

The normalizer is intentionally structural. It derives roles from SQL shape,
join topology, select positions, and schema role hints; it does not use
database-specific table-name dictionaries or fixed business vocabularies.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .data_structures_v2 import CanonicalRoleRef, LocalSchemaView


def _payload(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return dict(getattr(value, "__dict__", {}) or {})


def _clean_identifier(value: str) -> str:
    return str(value or "").strip().strip('"`[]')


def _column_role(column: Optional[str]) -> str:
    token = _clean_identifier(column or "").lower()
    if not token:
        return "other"
    if token == "id" or token.endswith("_id") or "uuid" in token or token.endswith("_key"):
        return "identifier"
    if "code" in token:
        return "code"
    if "name" in token or "title" in token:
        return "name"
    if "label" in token or "flag" in token or "status" in token:
        return "label"
    if "date" in token or "time" in token or token.endswith("_at"):
        return "temporal"
    if any(part in token for part in ("count", "num", "amount", "ratio", "pct", "score")):
        return "measure"
    return "other"


def _schema_column_role(
    *,
    table: Optional[str],
    column: Optional[str],
    schema_view: Optional[LocalSchemaView],
) -> str:
    if not table or not column or schema_view is None:
        return _column_role(column)
    for hint in schema_view.semantic_hints:
        if hint.table.lower() == table.lower() and hint.column.lower() == column.lower():
            return hint.role_family or _column_role(column)
    return _column_role(column)


def _sql_alias_map(sql: str) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    pattern = re.compile(
        r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_\"`\[\].]*)"
        r"(?:\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*))?",
        re.IGNORECASE,
    )
    stop_words = {"on", "where", "inner", "left", "right", "full", "cross", "join"}
    for match in pattern.finditer(str(sql or "")):
        table = _clean_identifier(match.group(1)).rsplit(".", 1)[-1]
        alias = _clean_identifier(match.group(2) or table)
        if alias and alias.lower() not in stop_words:
            aliases[alias.lower()] = table
        aliases.setdefault(table.lower(), table)
    return aliases


def _expr_table_column(
    expr: str,
    alias_map: Dict[str, str],
) -> Tuple[Optional[str], Optional[str]]:
    ref = _expr_table_column_ref(expr, alias_map)
    return ref.get("table"), ref.get("column")


def _expr_table_column_ref(
    expr: str,
    alias_map: Dict[str, str],
) -> Dict[str, Optional[str]]:
    text = str(expr or "").strip()
    match = re.search(
        r"([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*"
        r"(?:\"([^\"]+)\"|`([^`]+)`|\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_]*))",
        text,
    )
    if match:
        table_or_alias = _clean_identifier(match.group(1))
        column = _clean_identifier(next(value for value in match.groups()[1:] if value is not None))
        return {
            "alias": table_or_alias,
            "table": alias_map.get(table_or_alias.lower(), table_or_alias),
            "column": column,
        }
    bare = re.search(r"(?:\"([^\"]+)\"|`([^`]+)`|\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_]*))\s*$", text)
    if bare:
        value = _clean_identifier(next(item for item in bare.groups() if item is not None))
        if value and value.lower() not in {"select", "from", "where", "and", "or"}:
            return {"alias": None, "table": None, "column": value}
    return {"alias": None, "table": None, "column": None}


def _sql_has_distinct_output(sql: str) -> bool:
    text = str(sql or "")
    return bool(
        re.search(r"\bselect\s+distinct\b", text, flags=re.IGNORECASE)
        or re.search(r"\bcount\s*\(\s*distinct\b", text, flags=re.IGNORECASE)
    )


def _sql_has_aggregate_output(sql: str) -> bool:
    return bool(
        re.search(r"\b(count|sum|avg|min|max)\s*\(", str(sql or ""), flags=re.IGNORECASE)
    )


def _edge_endpoints(edge: Dict[str, Any], alias_map: Dict[str, str]) -> List[Tuple[str, str]]:
    text = " ".join(str(value or "") for value in edge.values())
    endpoints: List[Tuple[str, str]] = []
    for table_alias, column in re.findall(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\b",
        text,
    ):
        table = alias_map.get(table_alias.lower(), table_alias)
        endpoints.append((table, column))
    return endpoints


def _qualified_column_refs(text: str, alias_map: Dict[str, str]) -> List[Dict[str, str]]:
    refs: List[Dict[str, str]] = []
    pattern = re.compile(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*"
        r"(?:\"([^\"]+)\"|`([^`]+)`|\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_]*))\b"
    )
    for match in pattern.finditer(str(text or "")):
        table_or_alias = _clean_identifier(match.group(1))
        column = _clean_identifier(next(value for value in match.groups()[1:] if value is not None))
        table = alias_map.get(table_or_alias.lower(), table_or_alias)
        refs.append(
            {
                "alias": table_or_alias,
                "table": table,
                "column": column,
                "surface": match.group(0),
                "span_start": str(match.start()),
                "span_end": str(match.end()),
            }
        )
    return refs


def _equality_edges_from_expression(
    *,
    expression: str,
    alias_map: Dict[str, str],
    schema_view: Optional[LocalSchemaView],
    relation_roles: Dict[str, str],
    source: str,
    expression_index: int,
) -> List[Dict[str, Any]]:
    """Extract table.column = table.column relations from SQL text only."""
    refs = _qualified_column_refs(expression, alias_map)
    if len(refs) < 2:
        return []
    edges: List[Dict[str, Any]] = []
    # Work on equality-separated fragments so unrelated refs in the same
    # predicate do not become a synthetic relation.
    equality_pattern = re.compile(r"(?<![<>=!])=(?!=)")
    parts = equality_pattern.split(str(expression or ""))
    if len(parts) < 2:
        return []
    for left_text, right_text in zip(parts, parts[1:]):
        left_refs = _qualified_column_refs(left_text, alias_map)
        right_refs = _qualified_column_refs(right_text, alias_map)
        if not left_refs or not right_refs:
            continue
        left = left_refs[-1]
        right = right_refs[0]
        left_key = f"{left['table'].lower()}.{left['column'].lower()}"
        right_key = f"{right['table'].lower()}.{right['column'].lower()}"
        if left_key == right_key:
            continue
        left_role = _schema_column_role(
            table=left["table"],
            column=left["column"],
            schema_view=schema_view,
        )
        right_role = _schema_column_role(
            table=right["table"],
            column=right["column"],
            schema_view=schema_view,
        )
        relation_pair = sorted([left_key, right_key])
        edges.append(
            {
                "relation_id": f"{source}:equality:{expression_index}:{len(edges)}",
                "operator": "=",
                "left": {
                    "alias": left.get("alias"),
                    "table": left["table"],
                    "column": left["column"],
                    "column_role": left_role,
                    "relation_role": relation_roles.get(left["table"].lower(), "table_node"),
                },
                "right": {
                    "alias": right.get("alias"),
                    "table": right["table"],
                    "column": right["column"],
                    "column_role": right_role,
                    "relation_role": relation_roles.get(right["table"].lower(), "table_node"),
                },
                "canonical_key": "=".join(relation_pair),
                "role_key": "=".join(sorted([left_role, right_role])),
                "expression": str(expression),
            }
        )
    return edges


def _relation_roles(
    *,
    ast: Dict[str, Any],
    schema_view: Optional[LocalSchemaView],
    alias_map: Dict[str, str],
) -> Dict[str, str]:
    """Derive table roles from graph degree and FK topology only."""
    table_degree: Counter[str] = Counter()
    for edge in ast.get("join_edges") or []:
        payload = _payload(edge)
        joined_table = _clean_identifier(str(payload.get("table") or "")).rsplit(".", 1)[-1]
        if joined_table:
            table_degree[joined_table.lower()] += 1
        for table, _column in _edge_endpoints(payload, alias_map):
            table_degree[table.lower()] += 1

    schema_neighbors: Dict[str, set[str]] = defaultdict(set)
    if schema_view is not None:
        for edge in schema_view.pk_fk_edges:
            src_table = _clean_identifier(edge.source.split(".", 1)[0])
            dst_table = _clean_identifier(edge.target.split(".", 1)[0])
            if src_table and dst_table:
                schema_neighbors[src_table.lower()].add(dst_table.lower())
                schema_neighbors[dst_table.lower()].add(src_table.lower())

    tables = {
        _clean_identifier(table).rsplit(".", 1)[-1]
        for table in (ast.get("tables_involved") or [])
        if _clean_identifier(table)
    }
    from_table = _clean_identifier(str(ast.get("from_table") or "")).rsplit(".", 1)[-1]
    if from_table:
        tables.add(from_table)
    for edge in ast.get("join_edges") or []:
        table = _clean_identifier(str(_payload(edge).get("table") or "")).rsplit(".", 1)[-1]
        if table:
            tables.add(table)

    roles: Dict[str, str] = {}
    for table in tables:
        key = table.lower()
        if len(schema_neighbors.get(key, set())) >= 2:
            roles[key] = "connector_by_schema_degree"
        elif table_degree.get(key, 0) >= 2:
            roles[key] = "connector_by_query_degree"
        elif from_table and key == from_table.lower():
            roles[key] = "root_table"
        else:
            roles[key] = "table_node"
    return roles


def _alias_join_path_roles(equality_relations: Sequence[Dict[str, Any]]) -> Dict[str, List[str]]:
    roles: Dict[str, set[str]] = defaultdict(set)
    for relation in equality_relations or []:
        canonical_key = str(relation.get("canonical_key") or "")
        if not canonical_key:
            continue
        for side in ("left", "right"):
            endpoint = _payload(relation.get(side))
            alias = str(endpoint.get("alias") or "").strip().lower()
            if alias:
                roles[alias].add(f"join_path:{canonical_key}")
    return {alias: sorted(values) for alias, values in roles.items()}


def _output_path_role(
    *,
    alias: Optional[str],
    table: Optional[str],
    column: Optional[str],
    slot_index: int,
    alias_path_roles: Dict[str, List[str]],
) -> str:
    alias_key = str(alias or "").strip().lower()
    if alias_key and alias_path_roles.get(alias_key):
        table_part = _clean_identifier(table or "unknown_table").lower()
        column_part = _clean_identifier(column or "unknown_column").lower()
        return (
            "|".join(alias_path_roles[alias_key])
            + f":output:{table_part}.{column_part}"
        )
    return f"output_slot_{slot_index}"


def _join_endpoint_path_role(
    *,
    endpoint: Dict[str, Any],
    relation: Dict[str, Any],
) -> str:
    table = _clean_identifier(str(endpoint.get("table") or "")).lower()
    column = _clean_identifier(str(endpoint.get("column") or "")).lower()
    canonical_key = str(relation.get("canonical_key") or "")
    if canonical_key and table and column:
        return f"join_endpoint:{canonical_key}:{table}.{column}"
    return "join_endpoint"


def _side_group_column(column: Optional[str]) -> str:
    token = _clean_identifier(column or "").lower()
    return re.sub(r"\d+$", "", token)


def _side_key(table: Optional[str], column: Optional[str]) -> str:
    table_key = _clean_identifier(table or "").lower()
    column_key = _clean_identifier(column or "").lower()
    if table_key and column_key:
        return f"{table_key}.{column_key}"
    return ""


def _role_side_group(table: Optional[str], column: Optional[str]) -> str:
    table_key = _clean_identifier(table or "").lower()
    column_key = _side_group_column(column)
    if table_key and column_key:
        return f"{table_key}.{column_key}"
    return ""


def _direct_role_path(table: Optional[str], column: Optional[str]) -> str:
    key = _side_key(table, column)
    return f"direct:{key}" if key else ""


def _relation_role_rank(role: Optional[str]) -> int:
    value = str(role or "").strip().lower()
    if value in {"connector_by_schema_degree", "root_table"}:
        return 3
    if value == "connector_by_query_degree":
        return 2
    if value == "table_node":
        return 1
    return 0


def _preferred_side_endpoint(
    endpoint: Dict[str, Any],
    counterpart: Dict[str, Any],
) -> Dict[str, Any]:
    endpoint_table = str(endpoint.get("table") or "").strip().lower()
    counterpart_table = str(counterpart.get("table") or "").strip().lower()
    endpoint_column = str(endpoint.get("column") or "").strip().lower()
    counterpart_column = str(counterpart.get("column") or "").strip().lower()
    if (
        endpoint_table
        and counterpart_table
        and endpoint_table != counterpart_table
        and _side_group_column(endpoint_column) == _side_group_column(counterpart_column)
        and _side_group_column(endpoint_column)
    ):
        return counterpart
    endpoint_rank = _relation_role_rank(endpoint.get("relation_role"))
    counterpart_rank = _relation_role_rank(counterpart.get("relation_role"))
    endpoint_identifier = str(endpoint.get("column_role") or "").strip().lower() == "identifier"
    counterpart_identifier = str(counterpart.get("column_role") or "").strip().lower() == "identifier"
    if counterpart_rank > endpoint_rank:
        return counterpart
    if endpoint_rank > counterpart_rank:
        return endpoint
    if counterpart_identifier and not endpoint_identifier:
        return counterpart
    return endpoint


def _alias_side_bindings(
    equality_relations: Sequence[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    bindings: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    seen_by_alias: Dict[str, set[str]] = defaultdict(set)
    for relation in equality_relations or []:
        left = _payload(relation.get("left"))
        right = _payload(relation.get("right"))
        for endpoint, counterpart in ((left, right), (right, left)):
            alias = str(endpoint.get("alias") or "").strip().lower()
            if not alias:
                continue
            preferred = _preferred_side_endpoint(endpoint, counterpart)
            candidate_rows = [preferred]
            endpoint_key = _side_key(endpoint.get("table"), endpoint.get("column"))
            preferred_key = _side_key(preferred.get("table"), preferred.get("column"))
            if endpoint_key and endpoint_key != preferred_key:
                candidate_rows.append(endpoint)
            for chosen in candidate_rows:
                row = {
                    "direct_role_path": _direct_role_path(
                        chosen.get("table"),
                        chosen.get("column"),
                    ),
                    "role_side_group": _role_side_group(
                        chosen.get("table"),
                        chosen.get("column"),
                    ),
                    "side_key": _side_key(
                        chosen.get("table"),
                        chosen.get("column"),
                    ),
                    "canonical_key": str(relation.get("canonical_key") or ""),
                }
                row_key = json.dumps(row, sort_keys=True, ensure_ascii=False)
                if row["direct_role_path"] and row_key not in seen_by_alias[alias]:
                    seen_by_alias[alias].add(row_key)
                    bindings[alias].append(row)
    return bindings


def _select_output_side_binding(
    *,
    bindings: Sequence[Dict[str, Any]],
    table: Optional[str],
    column: Optional[str],
) -> Dict[str, Any]:
    expected_key = _side_key(table, column)
    expected_group = _role_side_group(table, column)
    for row in bindings or []:
        if str(row.get("side_key") or "") == expected_key and expected_key:
            return row
    for row in bindings or []:
        if str(row.get("role_side_group") or "") == expected_group and expected_group:
            return row
    return dict(bindings[0]) if bindings else {}


class RoleGraphNormalizer:
    """Build a canonical role graph from one SQL string."""

    schema_version = "role-graph-v0"

    def normalize_sql(
        self,
        *,
        sql: str,
        schema_view: Optional[LocalSchemaView] = None,
        source: str = "pred_sql",
    ) -> Dict[str, Any]:
        from .structure_family import cached_ast_signature

        ast = cached_ast_signature(sql or "") or {}
        alias_map = _sql_alias_map(sql)
        relation_roles = _relation_roles(
            ast=ast,
            schema_view=schema_view,
            alias_map=alias_map,
        )

        equality_relations: List[Dict[str, Any]] = []
        expression_index = 0
        for edge in ast.get("join_edges") or []:
            payload = _payload(edge)
            expression = str(payload.get("on_clause") or payload.get("on") or "")
            equality_relations.extend(
                _equality_edges_from_expression(
                    expression=expression,
                    alias_map=alias_map,
                    schema_view=schema_view,
                    relation_roles=relation_roles,
                    source=source,
                    expression_index=expression_index,
                )
            )
            expression_index += 1
        for predicate in ast.get("predicates") or []:
            equality_relations.extend(
                _equality_edges_from_expression(
                    expression=str(predicate),
                    alias_map=alias_map,
                    schema_view=schema_view,
                    relation_roles=relation_roles,
                    source=source,
                    expression_index=expression_index,
                )
            )
            expression_index += 1
        alias_path_roles = _alias_join_path_roles(equality_relations)
        alias_side_bindings = _alias_side_bindings(equality_relations)

        output_refs: List[CanonicalRoleRef] = []
        for index, expr in enumerate(ast.get("select_expressions") or []):
            expr_ref = _expr_table_column_ref(str(expr), alias_map)
            alias = expr_ref.get("alias")
            table = expr_ref.get("table")
            column = expr_ref.get("column")
            table_key = table.lower() if table else None
            side_binding = _select_output_side_binding(
                bindings=alias_side_bindings.get(str(alias or "").strip().lower(), []),
                table=table,
                column=column,
            )
            current_side_key = _side_key(table, column)
            binding_direct_path = str(side_binding.get("direct_role_path") or "")
            binding_side_key = str(side_binding.get("side_key") or "")
            is_direct_side = bool(binding_side_key and binding_side_key == current_side_key)
            output_refs.append(
                CanonicalRoleRef(
                    ref_id=f"{source}:output:{index}",
                    source=source,
                    table=table,
                    column=column,
                    expression=str(expr),
                    slot_index=index,
                    sql_role="output_slot",
                    column_role=_schema_column_role(
                        table=table,
                        column=column,
                        schema_view=schema_view,
                    ),
                    path_role=f"output_slot_{index}",
                    direct_role_path=(
                        binding_direct_path
                        if is_direct_side and binding_direct_path
                        else _direct_role_path(table, column) or None
                    ),
                    derived_role_path=(
                        None
                        if is_direct_side
                        else binding_direct_path or None
                    ),
                    role_side_group=str(side_binding.get("role_side_group") or "")
                    or _role_side_group(table, column)
                    or None,
                    side_key=str(side_binding.get("side_key") or "")
                    or _side_key(table, column)
                    or None,
                    relation_role=relation_roles.get(table_key or "", "unknown_table"),
                    evidence={"select_expression": str(expr), "source_alias": alias},
                )
            )

        join_refs: List[CanonicalRoleRef] = []
        for edge_index, edge in enumerate(ast.get("join_edges") or []):
            payload = _payload(edge)
            expression = str(payload.get("on_clause") or payload.get("on") or "")
            relations = _equality_edges_from_expression(
                expression=expression,
                alias_map=alias_map,
                schema_view=schema_view,
                relation_roles=relation_roles,
                source=source,
                expression_index=edge_index,
            )
            endpoint_rows: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
            for relation in relations:
                endpoint_rows.append((_payload(relation.get("left")), relation))
                endpoint_rows.append((_payload(relation.get("right")), relation))
            if not endpoint_rows:
                endpoint_rows = [
                    (
                        {"table": table, "column": column, "alias": None},
                        {"canonical_key": ""},
                    )
                    for table, column in _edge_endpoints(payload, alias_map)
                ]
            for endpoint_index, (endpoint, relation) in enumerate(endpoint_rows):
                table = str(endpoint.get("table") or "")
                column = str(endpoint.get("column") or "")
                join_refs.append(
                    CanonicalRoleRef(
                        ref_id=f"{source}:join:{edge_index}:{endpoint_index}",
                        source=source,
                        table=table,
                        column=column,
                        expression=str(payload.get("on_clause") or payload.get("on") or ""),
                        sql_role="join_endpoint",
                        column_role=_schema_column_role(
                            table=table,
                            column=column,
                            schema_view=schema_view,
                        ),
                        path_role=_join_endpoint_path_role(
                            endpoint=endpoint,
                            relation=relation,
                        ),
                        direct_role_path=_direct_role_path(table, column) or None,
                        role_side_group=_role_side_group(table, column) or None,
                        side_key=_side_key(table, column) or None,
                        relation_role=relation_roles.get(table.lower(), "table_node"),
                        evidence={
                            "join_edge": payload,
                            "source_alias": endpoint.get("alias"),
                            "canonical_key": relation.get("canonical_key"),
                        },
                    )
                )

        predicate_refs: List[CanonicalRoleRef] = []
        for predicate_index, predicate in enumerate(ast.get("predicates") or []):
            for ref_index, (table_alias, column) in enumerate(
                re.findall(
                    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\b",
                    str(predicate),
                )
            ):
                table = alias_map.get(table_alias.lower(), table_alias)
                predicate_refs.append(
                    CanonicalRoleRef(
                        ref_id=f"{source}:predicate:{predicate_index}:{ref_index}",
                        source=source,
                        table=table,
                        column=column,
                        expression=str(predicate),
                        sql_role="predicate_ref",
                        column_role=_schema_column_role(
                            table=table,
                            column=column,
                            schema_view=schema_view,
                        ),
                        path_role=f"predicate_{predicate_index}_ref_{ref_index}",
                        direct_role_path=_direct_role_path(table, column) or None,
                        role_side_group=_role_side_group(table, column) or None,
                        side_key=_side_key(table, column) or None,
                        relation_role=relation_roles.get(table.lower(), "table_node"),
                        evidence={"predicate": str(predicate)},
                    )
                )
        for ref in output_refs:
            alias = ref.evidence.get("source_alias") if ref.evidence else None
            ref.path_role = _output_path_role(
                alias=str(alias or "") or None,
                table=ref.table,
                column=ref.column,
                slot_index=int(ref.slot_index or 0),
                alias_path_roles=alias_path_roles,
            )

        shape = ast.get("select_shape") or {}
        has_distinct = bool(shape.get("has_distinct")) or _sql_has_distinct_output(sql)
        has_aggregate = bool(shape.get("has_aggregate")) or _sql_has_aggregate_output(sql)
        return {
            "schema_version": self.schema_version,
            "source": source,
            "sql_hash_key": _sql_hash_key(sql),
            "aliases": dict(sorted(alias_map.items())),
            "alias_path_roles": alias_path_roles,
            "tables": sorted(
                {
                    _clean_identifier(str(table)).rsplit(".", 1)[-1]
                    for table in (ast.get("tables_involved") or [])
                    if _clean_identifier(str(table))
                }
            ),
            "from_table": _clean_identifier(str(ast.get("from_table") or "")).rsplit(".", 1)[-1],
            "output_shape": {
                "arity": int(shape.get("output_arity") or len(output_refs) or 0),
                "has_distinct": has_distinct,
                "has_aggregate": has_aggregate,
                "roles": [ref.column_role for ref in output_refs],
            },
            "output_refs": [ref.model_dump(mode="json") for ref in output_refs],
            "join_refs": [ref.model_dump(mode="json") for ref in join_refs],
            "predicate_refs": [ref.model_dump(mode="json") for ref in predicate_refs],
            "join_edges": [_payload(edge) for edge in ast.get("join_edges") or []],
            "predicates": [str(item) for item in ast.get("predicates") or []],
            "equality_relations": equality_relations,
            "table_relation_roles": relation_roles,
        }


def _sql_hash_key(sql: str) -> str:
    import hashlib

    compact = " ".join(str(sql or "").split()).lower()
    return hashlib.sha1(compact.encode("utf-8")).hexdigest()[:12]


def role_refs_from_graph(
    graph: Dict[str, Any],
    *,
    sections: Sequence[str] = ("output_refs",),
) -> List[CanonicalRoleRef]:
    refs: List[CanonicalRoleRef] = []
    for section in sections:
        for payload in graph.get(section) or []:
            try:
                refs.append(CanonicalRoleRef.model_validate(payload))
            except Exception:
                continue
    return refs


__all__ = ["RoleGraphNormalizer", "role_refs_from_graph"]
