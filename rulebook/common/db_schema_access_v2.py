"""SqliteDBSchemaAccess ——按需 schema 访问实现.

Phase 1 实现，满足 local_schema_v2.DBSchemaAccess Protocol。

设计决策：
- **复用** DeepEye-SQL 的 `app.db_utils.schema` 模块（已有 BIRD 特例修复 + LRU 缓存）
- 动态 import，缺失时 fall back 到本模块内的 sqlite 基础实现（不带 BIRD 修复）
- 本层不做连接池，每个 call 都经过 deepeye 的 thread-local sqlite 连接 + 1000-entry LRU

依赖可选性：
- 若 sys.path 能拿到 deepeye：用 deepeye
- 若拿不到：用本模块内置 fallback（见 ``_fallback_*`` 函数）
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from .data_structures_v2 import ColumnHint, PkFkEdge


# =============================================================================
# 动态 import deepeye 工具（失败则走 fallback）
# =============================================================================


def _try_import_deepeye():
    """尝试加载 deepeye 的 db_utils.schema 模块。失败返回 None。"""
    try:
        import sys
        deepeye_path = "/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL"
        if deepeye_path not in sys.path:
            sys.path.insert(0, deepeye_path)
        from app.db_utils import schema as _de_schema  # type: ignore
        return _de_schema
    except Exception:
        return None


_DE_SCHEMA = _try_import_deepeye()


# =============================================================================
# Fallback 基础实现（仅在 deepeye 不可用时使用，不含 BIRD 修复）
# =============================================================================


def _fallback_list_tables(db_path: Path) -> List[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _fallback_get_columns(db_path: Path, table: str) -> List[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.execute(f"PRAGMA table_info(`{table}`)")
        return [row[1] for row in cur.fetchall()]
    finally:
        conn.close()


def _fallback_get_foreign_keys(
    db_path: Path, table: str
) -> List[tuple[str, str, str, str]]:
    """退化版 FK 读取。返回 (source_table, source_col, target_table, target_col)。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cur = conn.execute(f"PRAGMA foreign_key_list(`{table}`)")
        rows = cur.fetchall()
        out = []
        for row in rows:
            # row: (id, seq, table, from, to, on_update, on_delete, match)
            target_table = row[2]
            source_col = row[3]
            target_col = row[4]
            if target_col is None:
                continue  # fallback 不尝试修复
            out.append((table, source_col, target_table, target_col))
        return out
    finally:
        conn.close()


# =============================================================================
# SqliteDBSchemaAccess
# =============================================================================


class SqliteDBSchemaAccess:
    """实现 DBSchemaAccess Protocol。单个 DB 一实例。

    构造参数
    --------
    db_id       : str         —— 数据库 id（通常 = sqlite 文件 stem）
    db_path     : str | Path  —— .sqlite 文件路径
    database_dir: str | Path | None —— BIRD database_description 所在目录
                  （通常 db_path.parent）。None 则不加载 description。
    """

    def __init__(
        self,
        db_id: str,
        db_path: str | Path,
        database_dir: Optional[str | Path] = None,
    ) -> None:
        self.db_id = db_id
        self.db_path = Path(db_path)
        if database_dir is None:
            self.database_dir = self.db_path.parent
        else:
            self.database_dir = Path(database_dir)
        self._description_cache: Optional[dict] = None

    # ------------------------------------------------------------------
    # Protocol 方法
    # ------------------------------------------------------------------

    def list_tables(self) -> List[str]:
        return self._list_tables_cached(str(self.db_path))

    def get_columns(self, table: str) -> List[str]:
        return self._get_columns_cached(str(self.db_path), table)

    def get_pk_fk_edges(self, tables: Optional[List[str]] = None) -> List[PkFkEdge]:
        """返回 tables 子图内的所有 PK/FK 边（去重）。tables=None → 全库。"""
        table_list = tables if tables is not None else self.list_tables()
        edges: list[PkFkEdge] = []
        seen: set[tuple[str, str]] = set()
        for tbl in table_list:
            for src_tbl, src_col, tgt_tbl, tgt_col in self._get_fks_cached(
                str(self.db_path), tbl
            ):
                # 若 tables 约束开启，target 也得在里面
                if tables is not None and tgt_tbl not in tables:
                    continue
                key = (f"{src_tbl}.{src_col}", f"{tgt_tbl}.{tgt_col}")
                if key in seen:
                    continue
                seen.add(key)
                edges.append(
                    PkFkEdge(
                        source=f"{src_tbl}.{src_col}",
                        target=f"{tgt_tbl}.{tgt_col}",
                        kind="fk",
                    )
                )
        return edges

    def get_column_note(self, table: str, column: str) -> Optional[ColumnHint]:
        """从 database_description/*.csv 取列描述（若可用）。未加载则返回 None。"""
        desc = self._get_description_dict()
        if not desc:
            return None
        table_desc = desc.get(table.lower())
        if not table_desc:
            return None
        col_desc = table_desc.get(column.lower())
        if not col_desc:
            return None
        expanded = col_desc.get("expanded_column_name") or ""
        col_description = col_desc.get("column_description") or ""
        data_format = col_desc.get("data_format") or ""
        value_description = col_desc.get("value_description") or ""
        note_pieces = [p for p in (expanded, col_description, value_description) if p]
        return ColumnHint(
            table=table,
            column=column,
            role_family=_guess_role_family(column, data_format, col_description),
            note=" | ".join(note_pieces) or None,
        )

    # ------------------------------------------------------------------
    # 缓存层
    # ------------------------------------------------------------------

    @staticmethod
    @lru_cache(maxsize=64)
    def _list_tables_cached(db_path_str: str) -> tuple[str, ...]:
        path = Path(db_path_str)
        if _DE_SCHEMA is not None:
            tbls = _DE_SCHEMA.load_table_names(path)
        else:
            tbls = _fallback_list_tables(path)
        return tuple(tbls)

    @staticmethod
    @lru_cache(maxsize=1024)
    def _get_columns_cached(db_path_str: str, table: str) -> tuple[str, ...]:
        path = Path(db_path_str)
        if _DE_SCHEMA is not None:
            cols = [name for name, _typ in _DE_SCHEMA.load_column_names_and_types(path, table)]
        else:
            cols = _fallback_get_columns(path, table)
        return tuple(cols)

    @staticmethod
    @lru_cache(maxsize=1024)
    def _get_fks_cached(
        db_path_str: str, table: str
    ) -> tuple[tuple[str, str, str, str], ...]:
        path = Path(db_path_str)
        try:
            if _DE_SCHEMA is not None:
                fks = _DE_SCHEMA.load_foreign_keys(path, table)
            else:
                fks = _fallback_get_foreign_keys(path, table)
        except Exception:
            # 某些表 PRAGMA 失败（不存在等），直接空
            return tuple()
        return tuple(fks)

    def _get_description_dict(self) -> dict:
        """Lazy 加载 database_description/*.csv。"""
        if self._description_cache is not None:
            return self._description_cache
        if _DE_SCHEMA is None:
            self._description_cache = {}
            return self._description_cache
        try:
            self._description_cache = _DE_SCHEMA.load_database_description(
                self.db_id, self.database_dir
            )
        except Exception:
            self._description_cache = {}
        return self._description_cache


# =============================================================================
# 辅助：从列名/描述粗猜 role_family
# Phase 1 不追求完美，仅作 LocalSchemaView.semantic_hints 的初始 tag
# =============================================================================


def _guess_role_family(
    column: str, data_format: str, description: str
) -> Optional[str]:
    """按 naming pattern 粗猜列语义 role。与 ontology 无关，仅用于 candidate 枚举。"""
    col_lower = column.lower()
    desc_lower = (description or "").lower()
    fmt_lower = (data_format or "").lower()

    if col_lower.endswith("_id") or col_lower == "id" or col_lower.startswith("link_to_"):
        return "identifier"
    if "_name" in col_lower or col_lower.endswith("name") or col_lower == "name":
        return "name"
    if col_lower.endswith("_code") or col_lower == "code" or col_lower == "zip":
        return "code"
    if col_lower.endswith("_date") or col_lower.endswith("_time") or "date" in fmt_lower:
        return "temporal"
    if "text" in fmt_lower and ("label" in col_lower or "description" in col_lower):
        return "label"
    if fmt_lower in ("integer", "real", "float", "numeric", "number"):
        return "measure"
    if "count" in desc_lower or "amount" in desc_lower or "price" in desc_lower:
        return "measure"
    return None


__all__ = ["SqliteDBSchemaAccess"]
