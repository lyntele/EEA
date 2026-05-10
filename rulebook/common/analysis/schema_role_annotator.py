"""LLM-backed schema role annotation with persistent per-DB cache.

The annotator is intentionally not a closed vocabulary. It stores free-form
``role_family`` phrases generated from schema evidence, then reuses them for
future LocalSchemaView construction.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from method.EEA.rulebook.common.core.config import RULEBOOK_ROOT
from method.EEA.rulebook.common.core.data_structures import ColumnHint, LocalSchemaView


def _payload(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_payload(item) for item in value]
    return value


def _cache_root(cache_root: Optional[str | Path] = None) -> Path:
    if cache_root:
        return Path(cache_root)
    raw = (
        os.getenv("RULEBOOK_SCHEMA_ROLE_CACHE_ROOT", "").strip()
        or os.getenv("RULEBOOK_CACHE_ROOT", "").strip()
    )
    if raw:
        return Path(raw) / "schema_roles"
    return RULEBOOK_ROOT / "workspace" / "schema_roles"


def _cache_path(db_id: str, cache_root: Optional[str | Path] = None) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in str(db_id))
    return _cache_root(cache_root) / f"{safe}.json"


def _load_cache(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"schema_version": "schema-role-cache-v1", "columns": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": "schema-role-cache-v1", "columns": {}}
    if not isinstance(payload, dict):
        return {"schema_version": "schema-role-cache-v1", "columns": {}}
    if not isinstance(payload.get("columns"), dict):
        payload["columns"] = {}
    return payload


def _write_cache(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _column_key(table: str, column: str) -> str:
    return f"{str(table).lower()}.{str(column).lower()}"


def _existing_hint_map(view: LocalSchemaView) -> Dict[Tuple[str, str], ColumnHint]:
    out: Dict[Tuple[str, str], ColumnHint] = {}
    for hint in view.semantic_hints or []:
        out[(hint.table.lower(), hint.column.lower())] = hint
    return out


def _apply_cache(view: LocalSchemaView, cache: Dict[str, Any]) -> Tuple[LocalSchemaView, List[Tuple[str, str]]]:
    columns_payload = cache.get("columns") if isinstance(cache.get("columns"), dict) else {}
    existing = _existing_hint_map(view)
    hints: List[ColumnHint] = []
    missing: List[Tuple[str, str]] = []
    for table in view.tables or []:
        for column in (view.columns_by_table or {}).get(table, []) or []:
            key = _column_key(table, column)
            row = columns_payload.get(key) if isinstance(columns_payload.get(key), dict) else {}
            current = existing.get((table.lower(), column.lower()))
            role_family = str(row.get("role_family") or "").strip()
            rationale = str(row.get("rationale") or "").strip()
            note_parts = []
            if current and current.note:
                note_parts.append(current.note)
            if rationale:
                note_parts.append(f"role rationale: {rationale}")
            if role_family:
                hints.append(
                    ColumnHint(
                        table=table,
                        column=column,
                        role_family=role_family,
                        note=" | ".join(note_parts) or None,
                    )
                )
            else:
                missing.append((table, column))
                if current is not None:
                    hints.append(
                        ColumnHint(
                            table=table,
                            column=column,
                            role_family=None,
                            note=current.note,
                        )
                    )
    return view.model_copy(update={"semantic_hints": hints}), missing


def _schema_payload(view: LocalSchemaView, missing: Iterable[Tuple[str, str]]) -> Dict[str, Any]:
    missing_set = {(table.lower(), column.lower()) for table, column in missing}
    note_map = _existing_hint_map(view)
    tables: List[Dict[str, Any]] = []
    for table in view.tables or []:
        columns = []
        for column in (view.columns_by_table or {}).get(table, []) or []:
            if (table.lower(), column.lower()) not in missing_set:
                continue
            hint = note_map.get((table.lower(), column.lower()))
            columns.append(
                {
                    "name": column,
                    "note": hint.note if hint is not None else None,
                    "fk_edges": [
                        _payload(edge)
                        for edge in view.pk_fk_edges
                        if str(edge.source).lower().startswith(f"{table.lower()}.")
                        or str(edge.target).lower().startswith(f"{table.lower()}.")
                    ][:12],
                }
            )
        if columns:
            tables.append({"name": table, "columns": columns})
    return {
        "db_id": view.db_id,
        "tables": tables,
        "local_tables": list(view.tables or []),
        "pk_fk_edges": [_payload(edge) for edge in (view.pk_fk_edges or [])[:80]],
    }


def _annotate_missing_with_llm(
    *,
    view: LocalSchemaView,
    missing: List[Tuple[str, str]],
) -> Dict[str, Dict[str, str]]:
    if not missing:
        return {}
    try:
        from method.EEA.rulebook.common.llm.prompts.schema_role_annotator import (
            build_schema_role_annotator_prompt,
        )
        from method.EEA.rulebook.common.llm.utils import call_llm

        prompt = build_schema_role_annotator_prompt(
            schema_payload_json=json.dumps(
                _schema_payload(view, missing),
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        raw = call_llm(
            prompt,
            expect_json=True,
            stage="schema_role_annotator",
            trace_context={"db_id": view.db_id, "table_count": len(view.tables or [])},
        )
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    rows: Dict[str, Dict[str, str]] = {}
    for table in raw.get("tables") or []:
        if not isinstance(table, dict):
            continue
        table_name = str(table.get("name") or "").strip()
        if not table_name:
            continue
        for column in table.get("columns") or []:
            if not isinstance(column, dict):
                continue
            column_name = str(column.get("name") or "").strip()
            role_family = str(column.get("role_family") or "").strip()
            if not column_name or not role_family:
                continue
            rows[_column_key(table_name, column_name)] = {
                "table": table_name,
                "column": column_name,
                "role_family": role_family[:120],
                "rationale": str(column.get("rationale") or "").strip()[:240],
                "source": "llm_schema_role_annotator",
                "updated_at_utc": datetime.utcnow().isoformat(),
            }
    return rows


def annotate_schema_roles(
    local_schema_view: LocalSchemaView,
    db_id: str,
    *,
    cache_root: Optional[str | Path] = None,
) -> LocalSchemaView:
    """Attach free-form schema role hints to a LocalSchemaView.

    If the cache and LLM are unavailable, this returns the same schema with
    existing notes preserved and empty role_family values. It must not raise in
    the runtime path.
    """
    if not isinstance(local_schema_view, LocalSchemaView):
        return local_schema_view
    path = _cache_path(db_id, cache_root)
    cache = _load_cache(path)
    view, missing = _apply_cache(local_schema_view, cache)
    if missing:
        learned = _annotate_missing_with_llm(view=local_schema_view, missing=missing)
        if learned:
            cache.setdefault("schema_version", "schema-role-cache-v1")
            cache["db_id"] = db_id
            cache["updated_at_utc"] = datetime.utcnow().isoformat()
            columns = cache.setdefault("columns", {})
            if isinstance(columns, dict):
                columns.update(learned)
                _write_cache(path, cache)
            view, _missing_after = _apply_cache(local_schema_view, cache)
    return view


__all__ = ["annotate_schema_roles"]
