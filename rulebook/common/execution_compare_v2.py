"""Execution comparison helper for the v2 offline pipeline.

用途：跑 pred_sql 和 gold_sql，产出 ``ExecutionComparison``。解决的问题：
- Auditor 当前 prompt 接受 execution_result 但 pipeline 没填
- Extractor 根本没有 execution 字段，只读 Auditor 压成 minimal_fix 的字符串
- 对 141 这类"多差异并存、结果集等价"的 case，没有执行证据 → LLM 会被最显眼的
  SQL 语法差异（如 HAVING SUM）锚定，错判主 locus

离线组件，绝不进 runtime（answer-blind 约束）。
"""

from __future__ import annotations

import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .data_structures_v2 import ExecutionComparison


def _normalize_value(value: Any) -> Tuple[str, str]:
    if value is None:
        return ("null", "")
    if isinstance(value, bool):
        return ("bool", "1" if value else "0")
    if isinstance(value, int):
        return ("int", str(value))
    if isinstance(value, float):
        return ("float", repr(value))
    if isinstance(value, bytes):
        return ("bytes", value.hex())
    return ("text", str(value))


def _normalize_row_multiset(rows: List[Tuple[Any, ...]]) -> Counter[Tuple[Tuple[str, str], ...]]:
    """Normalize rows for execution equivalence without losing column structure.

    Row order is ignored, matching EX-style unordered result comparison, but
    column order and duplicate rows are preserved.  The previous
    set-of-frozensets normalization incorrectly treated ``(A, B)`` as equal to
    ``(B, A)`` and collapsed duplicate values inside a row; that can make
    symmetric pair mistakes look correct and prevents online accumulation.
    """
    return Counter(tuple(_normalize_value(value) for value in row) for row in rows)


def _unknown_reason_from_errors(pred_err: Optional[str], gold_err: Optional[str]) -> str:
    joined = " ".join(str(item or "") for item in (pred_err, gold_err)).lower()
    if "interrupted" in joined or "timeout" in joined:
        return "timeout"
    if pred_err or gold_err:
        return "execution_error"
    return "unknown"


def _execute_with_columns(
    db_path: str,
    sql: str,
    timeout: int = 30,
    row_limit: Optional[int] = None,
) -> Tuple[Optional[List[Tuple[Any, ...]]], Optional[List[str]], Optional[str], bool]:
    """返回 (rows, column_names, error, truncated)。

    ``sqlite3`` 的 timeout 只处理锁等待，不限制长查询本身。Replay/finalize
    只需要 bounded execution evidence，因此这里同时设置 progress handler
    和 ``fetchmany(row_limit + 1)``，避免大结果集在 ``fetchall`` 阶段卡死。
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=float(timeout))
        try:
            deadline = time.monotonic() + max(float(timeout), 0.1)

            def _abort_when_timed_out() -> int:
                return 1 if time.monotonic() > deadline else 0

            conn.set_progress_handler(_abort_when_timed_out, 10000)
            cur = conn.execute(sql)
            if row_limit is None:
                rows = cur.fetchall()
                truncated = False
            else:
                limit = max(int(row_limit), 0)
                rows = cur.fetchmany(limit + 1)
                truncated = len(rows) > limit
                rows = rows[:limit]
            col_names = [d[0] for d in (cur.description or [])]
            return rows, col_names, None, truncated
        finally:
            conn.close()
    except Exception as exc:
        return None, None, str(exc), False


def _infer_col_type(rows: List[Tuple[Any, ...]], col_idx: int) -> Optional[str]:
    """从样本行粗略推断列类型（int / float / text / null）。"""
    for row in rows:
        if col_idx >= len(row):
            continue
        v = row[col_idx]
        if v is None:
            continue
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, int):
            return "int"
        if isinstance(v, float):
            return "float"
        return "text"
    return None


def run_execution_comparison(
    *,
    db_path: str,
    pred_sql: str,
    gold_sql: str,
    timeout: int = 30,
    row_sample_limit: int = 500,
) -> ExecutionComparison:
    """跑 pred/gold 并比较结果。

    参数
    ----
    row_sample_limit :
        rows set 等价比较有成本；只对行数 <= limit 的结果做 set 比较。
        超限时标记 ``row_sets_equivalent=None`` 并在 notes 里说明。
    """
    db = str(Path(db_path).expanduser().resolve())
    pred_rows, pred_cols, pred_err, pred_truncated = _execute_with_columns(
        db, pred_sql, timeout, row_limit=row_sample_limit
    )
    gold_rows, gold_cols, gold_err, gold_truncated = _execute_with_columns(
        db, gold_sql, timeout, row_limit=row_sample_limit
    )

    cmp = ExecutionComparison(
        pred_row_count=(
            len(pred_rows) if pred_rows is not None and not pred_truncated else None
        ),
        gold_row_count=(
            len(gold_rows) if gold_rows is not None and not gold_truncated else None
        ),
        column_count_pred=(len(pred_cols) if pred_cols is not None else None),
        column_count_gold=(len(gold_cols) if gold_cols is not None else None),
        pred_error=pred_err,
        gold_error=gold_err,
        sampled_pred_row_count=(len(pred_rows) if pred_rows is not None else None),
        sampled_gold_row_count=(len(gold_rows) if gold_rows is not None else None),
        row_sample_limit=int(row_sample_limit),
    )

    if pred_rows is None or gold_rows is None:
        cmp.comparison_unknown_reason = _unknown_reason_from_errors(pred_err, gold_err)
        cmp.notes = "execution failed on one side; comparison unavailable"
        return cmp

    # row-set equivalence：忽略行顺序，但保留列顺序和重复行计数。
    if pred_truncated or gold_truncated:
        cmp.row_sets_equivalent = None
        truncated_sides = []
        if pred_truncated:
            truncated_sides.append("pred")
        if gold_truncated:
            truncated_sides.append("gold")
        cmp.comparison_unknown_reason = "truncated"
        cmp.truncated_sides = truncated_sides
        cmp.notes = (
            f"row set equivalence not computed: sampled {row_sample_limit} rows; "
            f"truncated_sides={','.join(truncated_sides)}"
        )
    elif len(pred_rows) <= row_sample_limit and len(gold_rows) <= row_sample_limit:
        pred_multiset = _normalize_row_multiset(pred_rows)
        gold_multiset = _normalize_row_multiset(gold_rows)
        cmp.row_sets_equivalent = pred_multiset == gold_multiset
    else:
        cmp.row_sets_equivalent = None
        cmp.comparison_unknown_reason = "row_sample_limit_exceeded"
        cmp.notes = (
            f"row set equivalence not computed: sizes "
            f"pred={len(pred_rows)} gold={len(gold_rows)} exceed row_sample_limit={row_sample_limit}"
        )

    # projected dtype changed：看每列类型是否相同
    dtype_changed = False
    if pred_cols is not None and gold_cols is not None:
        n = min(len(pred_cols), len(gold_cols))
        for i in range(n):
            pt = _infer_col_type(pred_rows, i)
            gt = _infer_col_type(gold_rows, i)
            if pt != gt and pt is not None and gt is not None:
                dtype_changed = True
                break
    cmp.projected_dtype_changed = dtype_changed

    return cmp


def serialize_execution_comparison(cmp: ExecutionComparison) -> str:
    """把 ExecutionComparison 序列化成 Auditor prompt 的 execution_result 输入字符串。"""
    lines = []
    lines.append(f"pred_row_count: {cmp.pred_row_count}")
    lines.append(f"gold_row_count: {cmp.gold_row_count}")
    if cmp.row_sets_equivalent is not None:
        lines.append(f"row_sets_equivalent: {cmp.row_sets_equivalent}")
    else:
        lines.append("row_sets_equivalent: unknown")
    if cmp.comparison_unknown_reason:
        lines.append(f"comparison_unknown_reason: {cmp.comparison_unknown_reason}")
    if cmp.truncated_sides:
        lines.append(f"truncated_sides: {','.join(cmp.truncated_sides)}")
    if cmp.column_count_pred is not None:
        lines.append(f"column_count_pred: {cmp.column_count_pred}")
    if cmp.column_count_gold is not None:
        lines.append(f"column_count_gold: {cmp.column_count_gold}")
    if cmp.projected_dtype_changed is not None:
        lines.append(f"projected_dtype_changed: {cmp.projected_dtype_changed}")
    if cmp.pred_error:
        lines.append(f"pred_error: {cmp.pred_error}")
    if cmp.gold_error:
        lines.append(f"gold_error: {cmp.gold_error}")
    if cmp.notes:
        lines.append(f"notes: {cmp.notes}")
    return "\n".join(lines)


__all__ = ["run_execution_comparison", "serialize_execution_comparison"]
