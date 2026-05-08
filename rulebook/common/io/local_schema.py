"""DBSchemaAccess and LocalSchemaView constructor interfaces.

Phase 0 **只定义接口**，不实现 sqlite 查询逻辑。Phase 1 会补上具体实现。
占位抽象保证：
- schema 访问 **按需**，不预建全库重索引（doc/plan.md §2.6）
- 构造 LocalSchemaView 的输入来源明确：`T_pred ∪ T_q ∪ T_mem`（doc/plan.md §4.2）
- 受限二跳扩展必须带 memory justification（不是自由探索）
- schema 召回失败走独立 diagnostics 通道（Codex Patch 1）
"""

from __future__ import annotations

from typing import List, Optional, Protocol

from method.EEA.rulebook.common.core.data_structures import (
    ColumnHint,
    LocalSchemaView,
    LocalSchemaViewDiagnostics,
    PkFkEdge,
    SchemaSource,
)


# =============================================================================
# DBSchemaAccess ——按需访问层（接口）
# Phase 0 只定 Protocol。Phase 1 实现时后端是 sqlite（复用 app/db_utils 或类似）
# =============================================================================


class DBSchemaAccess(Protocol):
    """按需的 schema 访问层。Phase 1 实现时应：
    - 内部可以缓存（LRU），但不做全库预索引
    - 线程/请求安全
    - get_columns / get_pk_fk / column_note 的 signature 保持稳定
    """

    db_id: str

    def list_tables(self) -> List[str]: ...

    def get_columns(self, table: str) -> List[str]: ...

    def get_pk_fk_edges(
        self,
        tables: Optional[List[str]] = None,
    ) -> List[PkFkEdge]:
        """返回 tables 子图内的 PK/FK 边。tables=None 表示全库。"""
        ...

    def get_column_note(self, table: str, column: str) -> Optional[ColumnHint]:
        """返回列的 metadata / description / role_family 提示。"""
        ...


# =============================================================================
# LocalSchemaView 构造入口（接口 + 占位实现）
# Phase 1 实现见 TODO 注释
# =============================================================================


def build_local_schema_view(
    *,
    db_id: str,
    access: "DBSchemaAccess",
    t_pred: List[str],
    t_q: List[str],
    t_mem: List[str],
    allow_two_hop_justifications: Optional[List[str]] = None,
) -> tuple[LocalSchemaView, LocalSchemaViewDiagnostics]:
    """构造 per-case LocalSchemaView。

    参数
    ----
    t_pred : 来自 pred_top1 FROM/JOIN/CTE/subquery 的表集
    t_q    : 来自 question + evidence 的候选表（字符串/说明匹配 + 可选轻量 LLM table hint）
    t_mem  : 来自命中的 memory object 的 schema hints
    allow_two_hop_justifications :
        若此列表非空，每项是 memory object id——表示允许做一次受限二跳扩展，
        但必须由某 memory object 明确 justify。空列表/None 表示禁止二跳。

    返回
    ----
    (LocalSchemaView, LocalSchemaViewDiagnostics)
    """

    # 大小写归一：sqlite 表名对大小写不敏感，但 PRAGMA 返回的是实际 casing。
    # 我们保留实际 casing 作为 canonical，按 lower-case 做集合去重。
    all_tables_real = access.list_tables()
    table_map: dict[str, str] = {t.lower(): t for t in all_tables_real}

    def _canonicalize(raw: List[str]) -> tuple[List[str], List[str]]:
        """返回 (existing_tables_canonical, missing_table_names)。"""
        existing: List[str] = []
        missing: List[str] = []
        seen: set[str] = set()
        for name in raw:
            key = name.strip().lower()
            if not key:
                continue
            if key in table_map:
                canonical = table_map[key]
                if canonical not in seen:
                    seen.add(canonical)
                    existing.append(canonical)
            else:
                missing.append(name)
        return existing, missing

    pred_canon, pred_missing = _canonicalize(t_pred)
    q_canon, q_missing = _canonicalize(t_q)
    mem_canon, mem_missing = _canonicalize(t_mem)

    # t_core = 并集，保留加入顺序
    t_core: List[str] = []
    seen_core: set[str] = set()
    for tbl in pred_canon + q_canon + mem_canon:
        if tbl not in seen_core:
            seen_core.add(tbl)
            t_core.append(tbl)

    def _outgoing_edges(src_table: str) -> list[PkFkEdge]:
        """从 src_table 出发的 FK 边（不受 tables 约束，方便一跳/二跳扩展）。"""
        # 取全库 edges 里 source_table = src_table 的子集
        return [
            e
            for e in access.get_pk_fk_edges()
            if e.source.split(".", 1)[0].lower() == src_table.lower()
        ]

    # 一跳邻居：把 t_core 每张表的 FK target 纳入 t_core
    one_hop_added: List[str] = []
    for tbl in list(t_core):
        for edge in _outgoing_edges(tbl):
            tgt_table = edge.target.split(".", 1)[0]
            tgt_canon = table_map.get(tgt_table.lower(), tgt_table)
            if tgt_canon in table_map.values() and tgt_canon not in seen_core:
                seen_core.add(tgt_canon)
                t_core.append(tgt_canon)
                one_hop_added.append(tgt_canon)

    # 受限二跳：仅在有 memory justification 时展开
    two_hop_added: List[str] = []
    denied: List[str] = []
    if allow_two_hop_justifications:
        for tbl in list(one_hop_added):
            for edge in _outgoing_edges(tbl):
                tgt_table = edge.target.split(".", 1)[0]
                tgt_canon = table_map.get(tgt_table.lower(), tgt_table)
                if tgt_canon not in seen_core and tgt_canon in table_map.values():
                    seen_core.add(tgt_canon)
                    t_core.append(tgt_canon)
                    two_hop_added.append(tgt_canon)
    else:
        # 记录被拒绝的潜在二跳（从 one_hop 可达但未展开的 FK target）
        for tbl in one_hop_added:
            for edge in _outgoing_edges(tbl):
                tgt_table = edge.target.split(".", 1)[0]
                tgt_canon = table_map.get(tgt_table.lower(), tgt_table)
                if tgt_canon not in seen_core and tgt_canon in table_map.values():
                    denied.append(tgt_canon)

    # pk_fk_edges：t_core × t_core 子图（不含 two_hop denied 之外的表）
    t_core_set = {t.lower() for t in t_core}
    pk_fk_edges: list[PkFkEdge] = []
    for edge in access.get_pk_fk_edges():
        src = edge.source.split(".", 1)[0].lower()
        tgt = edge.target.split(".", 1)[0].lower()
        if src in t_core_set and tgt in t_core_set:
            pk_fk_edges.append(edge)

    # columns_by_table：为 t_core 中所有表取列
    columns_by_table: dict[str, List[str]] = {}
    for tbl in t_core:
        columns_by_table[tbl] = list(access.get_columns(tbl))

    # semantic_hints：对 t_core 里每个表每个列尝试取 ColumnHint
    semantic_hints: List[ColumnHint] = []
    for tbl, cols in columns_by_table.items():
        for col in cols:
            hint = access.get_column_note(tbl, col)
            if hint is not None:
                semantic_hints.append(hint)

    view = LocalSchemaView(
        db_id=db_id,
        tables=t_core,
        columns_by_table=columns_by_table,
        pk_fk_edges=pk_fk_edges,
        semantic_hints=semantic_hints,
        source=SchemaSource(
            from_pred=pred_canon,
            from_question=q_canon,
            from_memory=mem_canon,
            two_hop_extension=two_hop_added,
        ),
    )

    diagnostics = LocalSchemaViewDiagnostics(
        missing_bridge_paths=sorted(set(denied)),
        missing_column_candidates=[],
        missing_role_family_matches=[],
        two_hop_extension_denied=sorted(set(denied)),
        notes=(
            None
            if not (pred_missing or q_missing or mem_missing)
            else (
                "Unknown table names requested: "
                + ", ".join(sorted(set(pred_missing + q_missing + mem_missing)))
            )
        ),
    )
    return view, diagnostics


__all__ = [
    "DBSchemaAccess",
    "build_local_schema_view",
]
