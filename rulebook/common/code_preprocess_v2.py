"""Code-side deterministic preprocessing for the v2 pipeline.

ErrorInstance Extractor 的 ``code + 一次 LLM`` 契约要求 code 部分先做完：
- AST 解析（复用 rulebook/common/structure_family.cached_ast_signature）
- DeltaFlags 26 维（复用 common/structure_delta_v2.compute_delta_flags）
- legacy_signature（复用 structure_family.repair_signature_dict_from_sql）
- candidate question tags（基于题面关键词的启发式匹配）
- candidate pred tags（基于 AST + DeltaFlags 的启发式映射）
- tables_from_pred / tables_from_question
- LocalSchemaView 构造（复用 local_schema_v2.build_local_schema_view）

这些字段稳定、可复现，是 LLM 的输入 payload，不放到 LLM 里做——避免早期错误冻结。
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Dict, List, Optional, Tuple

from .data_structures_v2 import (
    CaseSignalBundle,
    CaseSignalView,
    DeltaSignature,
    LocalSchemaView,
    LocalSchemaViewDiagnostics,
    OutputShapeDelta,
    PredSqlSignalView,
    QuestionSignalView,
    SchemaSignalView,
    SignalAtom,
)
from .local_schema_v2 import DBSchemaAccess, build_local_schema_view
from .vocabulary_v2 import (
    AnswerSlotType,
    GrainType,
    OpType,
    PredManifestationType,
    QuestionTargetRole,
    RouteCue,
    EXECUTION_ONLY_PRED_TAGS,
)


# =============================================================================
# Structure-side preprocessing（纯语法，无数据库调用）
# =============================================================================


def compute_structure_bundle(pred_sql: str, gold_sql: str) -> Dict[str, Any]:
    """算 pred_ast / gold_ast / join_edges_diff / structural_delta / structure_flags / legacy_signature。"""
    from .structure_family import cached_ast_signature, repair_signature_dict_from_sql
    from .structure_delta_v2 import build_structural_delta

    pred_ast: Dict[str, Any] = cached_ast_signature(pred_sql) or {}
    gold_ast: Dict[str, Any] = cached_ast_signature(gold_sql) or {}

    # join edges diff：沿用 census/trigger 已有的 tuple 表达
    def _edge_tuple(edge: Dict[str, Any]) -> Tuple[str, str, str]:
        return (
            str(edge.get("table") or ""),
            str(edge.get("join_type") or ""),
            str((edge.get("on_clause") or edge.get("on") or "")).strip().lower(),
        )

    pred_edges = {_edge_tuple(e) for e in (pred_ast.get("join_edges") or [])}
    gold_edges = {_edge_tuple(e) for e in (gold_ast.get("join_edges") or [])}
    join_edges_diff = {
        "pred_only": sorted(pred_edges - gold_edges),
        "gold_only": sorted(gold_edges - pred_edges),
    }

    structural_delta = build_structural_delta(pred_ast, gold_ast, join_edges_diff)
    # DeltaFlags 是 dataclass，取其 dict
    delta_flags = structural_delta.delta_flags
    structure_flags: Dict[str, bool] = {
        k: bool(v) for k, v in dataclasses.asdict(delta_flags).items() if isinstance(v, bool)
    }

    repair_sig_dict = repair_signature_dict_from_sql(pred_sql, gold_sql) or {}
    legacy_signature = "|".join(
        [
            str(repair_sig_dict.get("primary_edit_family") or ""),
            str(repair_sig_dict.get("edit_subfamily") or ""),
            str(repair_sig_dict.get("target_output_contract") or ""),
            str(repair_sig_dict.get("special_transform_requirement") or ""),
        ]
    )

    return {
        "pred_ast": pred_ast,
        "gold_ast": gold_ast,
        "join_edges_diff": join_edges_diff,
        "structural_delta": structural_delta,
        "structure_flags": structure_flags,
        "legacy_signature": legacy_signature,
        "legacy_signature_dict": repair_sig_dict,
    }


# =============================================================================
# Tables (T_pred / T_q)
# =============================================================================


def extract_tables_from_pred(pred_ast: Dict[str, Any]) -> List[str]:
    """从 pred 的 AST tables_involved 取表集合。"""
    tables = list(pred_ast.get("tables_involved") or [])
    from_table = str(pred_ast.get("from_table") or "").strip()
    if from_table:
        tables.append(from_table)
    for edge in pred_ast.get("join_edges") or []:
        tbl = str(edge.get("table") or "").strip()
        if tbl:
            tables.append(tbl)
    # dedup keep order
    seen = set()
    out: List[str] = []
    for t in tables:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def extract_tables_from_question(
    *,
    question: str,
    evidence: str,
    all_tables: List[str],
) -> List[str]:
    """从 question + evidence 文本里匹配出现的表名。简单子串+大小写容错启发式。

    生产版可以引入 LLM table hint，但首版不引入，避免额外 LLM 调用。
    """
    text = f" {question or ''} \n {evidence or ''} ".lower()
    hits: List[str] = []
    for tbl in all_tables:
        tbl_l = tbl.lower()
        # 纯子串匹配，允许下划线分词对齐
        alt = tbl_l.replace("_", " ")
        if tbl_l in text or (" " + alt + " ") in text:
            hits.append(tbl)
    return hits


# =============================================================================
# Candidate tag 启发式提取
# =============================================================================


_QUESTION_TAG_RULES: Dict[str, List[str]] = {
    # AnswerSlotType
    AnswerSlotType.ID.value: [r"\bids?\b", r"\bidentifiers?\b", r"\bidentifications?\b", r"identification number"],
    AnswerSlotType.NAME.value: [r"\bnames?\b", r"\bcalled\b"],
    AnswerSlotType.URL.value: [r"\burls?\b", r"\blinks?\b", r"\bwebsites?\b"],
    AnswerSlotType.DATE.value: [r"\bdates?\b", r"\byears?\b", r"\bwhen\b"],
    AnswerSlotType.TIME.value: [r"\btimes?\b", r"\bat what time\b"],
    AnswerSlotType.RANK.value: [r"\brank(ed|ing)?\b", r"\btop\s+\d+", r"\bhighest\b", r"\blowest\b"],
    AnswerSlotType.CATEGORY.value: [r"\bcategor(y|ies)\b", r"\btypes?\b", r"\bclass\b"],
    AnswerSlotType.COUNT.value: [r"\bhow many\b", r"\bnumber of\b", r"\bcount of\b"],
    AnswerSlotType.RATIO.value: [r"\bpercentage\b", r"\bratio\b", r"\bproportion\b", r"\bpercent\b", r"\b% "],
    AnswerSlotType.STATUS.value: [r"\bstatus(es)?\b", r"\bstate\b", r"\bactive\b"],
    AnswerSlotType.LOCATION.value: [r"\blocations?\b", r"\baddress(es)?\b", r"\bcit(y|ies)\b", r"\bcountry\b"],
    AnswerSlotType.LABEL.value: [r"\blabels?\b", r"\bflags?\b"],
    AnswerSlotType.CODE.value: [r"\bcodes?\b", r"\bzip\b"],
    # OpType
    OpType.COUNT.value: [r"\bhow many\b", r"\bnumber of\b", r"\bcount of\b"],
    OpType.DISTINCT_COUNT.value: [r"\bhow many (distinct|unique|different)\b"],
    OpType.SUM.value: [r"\btotal\b", r"\bsum of\b"],
    OpType.AVG.value: [r"\baverage\b", r"\bmean\b"],
    OpType.RATIO.value: [r"\bpercentage\b", r"\bratio\b", r"\bproportion\b"],
    OpType.RANK.value: [r"\brank\b"],
    OpType.TOPK.value: [r"\btop\s+\d+", r"\bfirst\s+\d+"],
    OpType.EXTREMUM.value: [r"\b(most|least|highest|lowest|maximum|minimum|max|min)\b"],
    OpType.CONDITIONAL_AGGREGATE.value: [r"\bpercentage of\b", r"\bshare of\b", r"\bamong\b.*\bhow many\b"],
    # RouteCue
    RouteCue.CONFIG.value: [r"\brequested\b", r"\bconfigured\b", r"\bhow often\b", r"\baim of\b", r"\bfrequency\b"],
    RouteCue.EXECUTED_FLOW.value: [r"\bmade (a )?(payment|transaction|purchase)\b", r"\bexecuted\b"],
    RouteCue.ROLE_ANCHOR.value: [r"\bmembers?\b", r"\bhead(s)?\b"],
}

_GRAIN_RULES: Dict[str, List[str]] = {
    GrainType.ENTITY.value: [r"\bhow many (accounts|customers|students|members|players)\b"],
    GrainType.ROW.value: [r"\bhow many (records|rows|entries|transactions)\b"],
}


def extract_candidate_question_tags(*, question: str, evidence: str = "") -> List[str]:
    """从 question + evidence 文本按词表做正则匹配，返回 candidate tag 列表。

    这个列表是 **candidate pool**——extractor LLM 会从中做 decisive vs descriptive
    的最终判决。
    """
    text = f"{question or ''} \n {evidence or ''}".lower()
    tags: List[str] = []
    seen: set[str] = set()
    for tag, patterns in {**_QUESTION_TAG_RULES, **_GRAIN_RULES}.items():
        for pat in patterns:
            if re.search(pat, text):
                if tag not in seen:
                    seen.add(tag)
                    tags.append(tag)
                break
    # 最后粗粒度加一个 target role tag（不判具体哪个实体）
    # PRIMARY_ENTITY 几乎总是命中——由 LLM 自己降级为 descriptive 即可
    tags.append(QuestionTargetRole.PRIMARY_ENTITY.value)
    return tags


def extract_candidate_pred_tags(
    *,
    pred_ast: Dict[str, Any],
    gold_ast: Dict[str, Any],
    structure_flags: Dict[str, bool],
    execution_comparison: Optional[Any] = None,
    runtime_mode: bool = False,
) -> List[str]:
    """从 AST + DeltaFlags 启发式 + execution_comparison 派生 PredManifestationType 候选 tag。

    execution_comparison 若提供（offline accumulate 阶段），额外 emit 两个 deterministic tag：
    - SELECT_ARITY_MISMATCH  ：pred/gold 列数不同
    - SELECT_ROLE_DTYPE_MISMATCH：列数相同但结果列 dtype 变化（如 name→identifier）

    这两个 tag 是 decisive 的——extractor prompt R6 看到它们就必须选 locus=SELECT。

    ``runtime_mode=True``（answer-blind runtime）：
    - 不 emit 任何依赖 gold 对比的 tag（如 missing_bridge / misroute_fact / missing_second_slot）
    - pair_output / select_single_id_only 等 pred-only 可识别的 tag 仍然 emit
    - execution_comparison 无论是否传入都忽略（runtime 没 gold）
    """
    tags: List[str] = []
    seen: set[str] = set()

    def add(t: PredManifestationType) -> None:
        v = t.value
        if v not in seen:
            seen.add(v)
            tags.append(v)

    # Execution-driven deterministic tags（仅 offline / 有 gold 时 emit）
    if not runtime_mode and execution_comparison is not None:
        col_p = getattr(execution_comparison, "column_count_pred", None)
        col_g = getattr(execution_comparison, "column_count_gold", None)
        dtype_changed = getattr(execution_comparison, "projected_dtype_changed", None)
        if col_p is not None and col_g is not None and col_p != col_g:
            add(PredManifestationType.SELECT_ARITY_MISMATCH)
        if col_p == col_g and dtype_changed:
            add(PredManifestationType.SELECT_ROLE_DTYPE_MISMATCH)

    # Helper：SELECT 表达式文本小写拼接
    pred_select_text = " ".join(str(e).lower() for e in (pred_ast.get("select_expressions") or []))
    gold_select_text = " ".join(str(e).lower() for e in (gold_ast.get("select_expressions") or []))

    # SELECT_SINGLE_ID_ONLY：pred 只有 1 个 id 列
    pred_arity = int((pred_ast.get("select_shape") or {}).get("output_arity") or 0)
    gold_arity = int((gold_ast.get("select_shape") or {}).get("output_arity") or 0)
    if pred_arity == 1 and ("_id" in pred_select_text or pred_select_text.strip().endswith("id")):
        add(PredManifestationType.SELECT_SINGLE_ID_ONLY)

    # MISSING_SECOND_SLOT：pred 比 gold 少一列（仅 offline，runtime 无 gold）
    if not runtime_mode and pred_arity and gold_arity and pred_arity < gold_arity:
        add(PredManifestationType.MISSING_SECOND_SLOT)

    # MISSING_RANK_SLOT：pred 有 ORDER BY 但 arity<=1（runtime 能判断 pred 侧，保留）
    if pred_ast.get("order_by") and pred_arity <= 1:
        add(PredManifestationType.MISSING_RANK_SLOT)

    # PAIR_OUTPUT：runtime_mode 下放宽到 pred arity >= 2（pred-only 可见）；
    # offline 下保留严格定义（pred 2 + gold 1）
    if runtime_mode:
        if pred_arity >= 2:
            add(PredManifestationType.PAIR_OUTPUT)
    else:
        if pred_arity == 2 and gold_arity == 1:
            add(PredManifestationType.PAIR_OUTPUT)

    # USED_DISTINCT_EARLY：pred 加了 DISTINCT 但 gold 没有（offline 专用）
    if not runtime_mode and structure_flags.get("distinct_differ"):
        if (pred_ast.get("select_shape") or {}).get("has_distinct"):
            if not (gold_ast.get("select_shape") or {}).get("has_distinct"):
                add(PredManifestationType.USED_DISTINCT_EARLY)

    # COUNT_DISTINCT_ENTITY：pred 用了 COUNT(DISTINCT ...)（pred-only）
    if "count(distinct" in pred_select_text:
        add(PredManifestationType.COUNT_DISTINCT_ENTITY)

    # COUNT_RAW_ROWS：pred 用 COUNT(*) 且 gold 用 COUNT(column)（offline 专用）
    if (
        not runtime_mode
        and "count(*)" in pred_select_text
        and "count(" in gold_select_text
        and "count(*)" not in gold_select_text
    ):
        add(PredManifestationType.COUNT_RAW_ROWS)

    # MISSING_BRIDGE / MISROUTE_FACT：需要对比 gold tables，仅 offline
    if not runtime_mode:
        pred_tables = set(t.lower() for t in (pred_ast.get("tables_involved") or []))
        gold_tables = set(t.lower() for t in (gold_ast.get("tables_involved") or []))
        if gold_tables - pred_tables:
            add(PredManifestationType.MISSING_BRIDGE)
        if pred_tables and gold_tables and not (pred_tables & gold_tables):
            add(PredManifestationType.MISROUTE_FACT)

    # JOIN_BOTH_SIDES：pred join_edges 里出现第二侧编号 key（形态信号）
    pred_edges_text = " ".join(
        str(e.get("on_clause") or "").lower() for e in (pred_ast.get("join_edges") or [])
    )
    if re.search(r"_id2\b|\.id2\b", pred_edges_text):
        add(PredManifestationType.JOIN_BOTH_SIDES)

    # WHERE_LABEL_IN_NUMERATOR：需要 gold CASE WHEN 对比，仅 offline。
    # The enum name is legacy; the signal condition is structural: a pred WHERE
    # filter becomes a gold CASE expression.
    if not runtime_mode and "case when" in gold_select_text and "case when" not in pred_select_text:
        pred_where = " ".join(str(p).lower() for p in (pred_ast.get("predicates") or []))
        if pred_where.strip():
            add(PredManifestationType.WHERE_LABEL_IN_NUMERATOR)

    # ROW_GRAIN_NOT_ENTITY：需要 gold GROUP BY，仅 offline
    if not runtime_mode:
        if (gold_ast.get("group_by_keys") or []) and not (pred_ast.get("group_by_keys") or []):
            if pred_ast.get("join_edges"):
                add(PredManifestationType.ROW_GRAIN_NOT_ENTITY)

    return tags


# =============================================================================
# Phase A signal views（audit-only）
# =============================================================================


def _dedup_keep_order(values: List[Any]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.lower()
        if key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _model_payload(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return dict(value)
    return {}


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _select_arity(ast: Dict[str, Any]) -> Optional[int]:
    shape = ast.get("select_shape") or {}
    arity = _safe_int(shape.get("output_arity"))
    if arity is not None:
        return arity
    exprs = ast.get("select_expressions") or []
    return len(exprs) if exprs else None


def _select_items(ast: Dict[str, Any]) -> List[str]:
    return [str(expr) for expr in (ast.get("select_expressions") or []) if str(expr).strip()]


def _tables_from_ast(ast: Dict[str, Any]) -> List[str]:
    return _dedup_keep_order(
        list(ast.get("tables_involved") or [])
        + [str(ast.get("from_table") or "")]
        + [str(edge.get("table") or "") for edge in (ast.get("join_edges") or [])]
    )


def _infer_expression_role(expr: Any) -> str:
    """Infer a coarse structural role without a hand-written semantic lexicon."""
    text = str(expr or "").strip().lower()
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


def _select_role_profile(ast: Dict[str, Any]) -> List[str]:
    refs = ast.get("select_column_refs") or []
    exprs = refs or _select_items(ast)
    return [_infer_expression_role(expr) for expr in exprs]


def _join_graph(ast: Dict[str, Any]) -> List[Dict[str, Any]]:
    graph: List[Dict[str, Any]] = []
    for edge in ast.get("join_edges") or []:
        if not isinstance(edge, dict):
            continue
        graph.append(
            {
                "table": str(edge.get("table") or ""),
                "join_type": str(edge.get("join_type") or ""),
                "on_clause": str(edge.get("on_clause") or edge.get("on") or ""),
            }
        )
    return graph


def _predicate_profile(ast: Dict[str, Any]) -> Dict[str, Any]:
    predicates = [str(p) for p in (ast.get("predicates") or []) if str(p).strip()]
    text = " ".join(predicates).lower()
    return {
        "predicate_count": len(predicates),
        "predicates": predicates[:20],
        "comparison_operators": _dedup_keep_order(list(ast.get("comparison_operators") or [])),
        "logical_operators": _dedup_keep_order(list(ast.get("logical_operators") or [])),
        "has_or": " or " in f" {text} " or "OR" in predicates,
        "has_negation": any(term in text for term in (" not ", "not exists", "not in", "!=", "<>")),
        "has_in_subquery": " in (" in text or "exists" in text,
        "literal_count": len(ast.get("predicate_values") or []),
    }


def _aggregate_profile(ast: Dict[str, Any]) -> Dict[str, Any]:
    select_text = " ".join(_select_items(ast)).lower()
    return {
        "has_aggregate": bool((ast.get("select_shape") or {}).get("has_aggregate")),
        "has_count": "count(" in select_text,
        "has_count_star": "count(*)" in select_text.replace(" ", ""),
        "has_count_distinct": "count(distinct" in select_text,
        "has_sum": "sum(" in select_text,
        "has_avg": "avg(" in select_text,
        "has_minmax": bool(re.search(r"\b(min|max)\s*\(", select_text)),
        "has_case_when": "case when" in select_text,
        "has_arithmetic_formula": any(op in select_text for op in ("/", "*", "+", "-")),
    }


def _group_order_profile(ast: Dict[str, Any]) -> Dict[str, Any]:
    group_keys = [str(key) for key in (ast.get("group_by_keys") or []) if str(key).strip()]
    order_by = ast.get("order_by") or []
    return {
        "group_by_count": len(group_keys),
        "group_by_keys": group_keys[:20],
        "order_by_count": len(order_by),
        "order_by": order_by[:20],
        "limit": ast.get("limit"),
        "has_window": bool(ast.get("window_functions")),
    }


def _grain_from_ast(ast: Dict[str, Any]) -> Optional[str]:
    group_keys = ast.get("group_by_keys") or []
    if group_keys:
        return "grouped_result"
    if (ast.get("select_shape") or {}).get("has_aggregate"):
        return "scalar_aggregate"
    arity = _select_arity(ast)
    roles = _select_role_profile(ast)
    if arity == 2 and roles.count("identifier_like") >= 2:
        return "pair_rows"
    if ast.get("limit") is not None:
        return "limited_rows"
    return "row_result" if arity else None


def _schema_mention_hits(question: str, evidence: str, local_schema_view: LocalSchemaView) -> List[str]:
    text = f" {question or ''} \n {evidence or ''} ".lower()
    hits: List[str] = []
    for table in local_schema_view.tables:
        table_l = table.lower()
        if table_l in text or table_l.replace("_", " ") in text:
            hits.append(f"table:{table}")
    for table, columns in local_schema_view.columns_by_table.items():
        for column in columns:
            column_l = column.lower()
            if column_l in text or column_l.replace("_", " ") in text:
                hits.append(f"column:{table}.{column}")
    return _dedup_keep_order(hits)[:50]


def _named_mentions(question: str, evidence: str) -> List[str]:
    text = f"{question or ''} {evidence or ''}"
    mentions: List[str] = []
    mentions.extend(re.findall(r"'([^']{2,80})'|\"([^\"]{2,80})\"", text))
    flattened: List[str] = []
    for item in mentions:
        if isinstance(item, tuple):
            flattened.extend([part for part in item if part])
        else:
            flattened.append(str(item))
    title_phrases = re.findall(r"\b[A-Z][A-Za-z0-9_-]+(?:\s+[A-Z][A-Za-z0-9_-]+){1,4}\b", text)
    flattened.extend(title_phrases)
    id_like_tokens = re.findall(r"\b[A-Z]{2,}[A-Z0-9_]*\d[A-Z0-9_]*\b", text)
    flattened.extend(id_like_tokens)
    return _dedup_keep_order(flattened)[:30]


def _negative_question_cues(question: str, evidence: str) -> List[str]:
    _ = (question, evidence)
    return []


def _question_signal_view(
    *,
    question: str,
    evidence: str,
    local_schema_view: LocalSchemaView,
    candidate_question_tags: List[str],
) -> QuestionSignalView:
    answer_slots = {item.value for item in AnswerSlotType}
    operations = {item.value for item in OpType}
    routes = {item.value for item in RouteCue}
    grains = {item.value for item in GrainType}
    grain_hits = [tag for tag in candidate_question_tags if tag in grains]
    return QuestionSignalView(
        answer_slot_cues=[tag for tag in candidate_question_tags if tag in answer_slots],
        operation_cues=[tag for tag in candidate_question_tags if tag in operations],
        grain_cue=grain_hits[0] if grain_hits else None,
        route_cues=[tag for tag in candidate_question_tags if tag in routes],
        named_mentions=_named_mentions(question, evidence),
        schema_mention_hits=_schema_mention_hits(question, evidence, local_schema_view),
        negative_question_cues=_negative_question_cues(question, evidence),
    )


def _column_role_hints(local_schema_view: LocalSchemaView) -> Dict[str, str]:
    hints: Dict[str, str] = {}
    for hint in local_schema_view.semantic_hints:
        key = f"{hint.table}.{hint.column}"
        role = hint.role_family or _infer_expression_role(hint.column)
        hints[key] = role
    for table, columns in local_schema_view.columns_by_table.items():
        for column in columns:
            key = f"{table}.{column}"
            hints.setdefault(key, _infer_expression_role(column))
    return hints


def _is_endpoint_like(column: str) -> bool:
    token = column.lower().strip("`\"[]")
    return (
        token == "id"
        or token.endswith("_id")
        or token.endswith("id")
        or re.search(r"(?:^|_)id\d+$", token) is not None
    )


def _has_sided_endpoint_pair(columns: List[str]) -> bool:
    tokens = [col.lower().strip("`\"[]") for col in columns]
    token_set = set(tokens)
    for token in tokens:
        if token.endswith("_id"):
            base = token[:-3]
            if f"{base}_id1" in token_set or f"{base}_id2" in token_set:
                return True
        match = re.match(r"(.+)_id([12])$", token)
        if match:
            base = match.group(1)
            if f"{base}_id" in token_set:
                return True
    return False


def _split_column_ref(ref: str) -> Tuple[str, str]:
    text = str(ref or "").strip().strip("`\"[]")
    if "." not in text:
        return "", text
    table, column = text.split(".", 1)
    return table.strip("`\"[]"), column.strip("`\"[]")


def _schema_edge_columns_by_table(local_schema_view: LocalSchemaView) -> Dict[str, List[str]]:
    """Columns participating in explicit schema edges, grouped by their own table.

    This is intentionally topology-based. It does not inspect table-name words
    such as "bond", "mapping", or "membership".
    """
    by_table: Dict[str, List[str]] = {}
    for edge in local_schema_view.pk_fk_edges:
        for ref in (edge.source, edge.target):
            table, column = _split_column_ref(ref)
            if table and column:
                by_table.setdefault(table, []).append(column)
    return {table: _dedup_keep_order(cols) for table, cols in by_table.items()}


def _relation_hints(local_schema_view: LocalSchemaView) -> List[Dict[str, Any]]:
    hints: List[Dict[str, Any]] = []
    edge_columns_by_table = _schema_edge_columns_by_table(local_schema_view)
    for table, columns in local_schema_view.columns_by_table.items():
        endpoint_columns = [col for col in columns if _is_endpoint_like(col)]
        edge_endpoint_columns = [
            col
            for col in endpoint_columns
            if col.lower() in {edge_col.lower() for edge_col in edge_columns_by_table.get(table, [])}
        ]
        if len(endpoint_columns) >= 2 and _has_sided_endpoint_pair(endpoint_columns):
            hints.append(
                {
                    "kind": "possible_relation_table",
                    "table": table,
                    "endpoint_columns": endpoint_columns[:8],
                    "evidence": "sided endpoint columns",
                }
            )
        elif len(edge_endpoint_columns) >= 2:
            hints.append(
                {
                    "kind": "possible_multi_fk_context",
                    "table": table,
                    "endpoint_columns": edge_endpoint_columns[:8],
                    "evidence": "multiple endpoint columns participate in schema edges",
                }
            )
        elif len(endpoint_columns) >= 2:
            hints.append(
                {
                    "kind": "possible_multi_identifier_context",
                    "table": table,
                    "endpoint_columns": endpoint_columns[:8],
                    "evidence": "multiple identifier-like columns without sided endpoint evidence",
                }
            )
        numbered_slot_columns = [
            col for col in columns if re.search(r"(?:^|_)(?:1|2|3|4|5)$", col.lower())
        ]
        if len(numbered_slot_columns) >= 2:
            hints.append(
                {
                    "kind": "possible_wide_slot_table",
                    "table": table,
                    "slot_columns": numbered_slot_columns[:12],
                    "evidence": "numbered sibling columns",
                }
            )
    if local_schema_view.pk_fk_edges:
        hints.append(
            {
                "kind": "explicit_schema_edges_present",
                "edge_count": len(local_schema_view.pk_fk_edges),
                "evidence": "pk/fk edges available in local schema view",
            }
        )
    return hints


def _schema_signal_view(
    *,
    local_schema_view: LocalSchemaView,
    local_schema_diagnostics: Optional[LocalSchemaViewDiagnostics] = None,
) -> SchemaSignalView:
    relation_hints = _relation_hints(local_schema_view)
    tags: List[str] = []
    for hint in relation_hints:
        kind = str(hint.get("kind") or "")
        if kind:
            tags.append(f"has_{kind}")
    diag_payload = _model_payload(local_schema_diagnostics)
    if any(diag_payload.get(key) for key in ("missing_bridge_paths", "missing_column_candidates")):
        tags.append("schema_recall_has_miss")
    return SchemaSignalView(
        local_tables=list(local_schema_view.tables),
        columns_by_table={tbl: list(cols) for tbl, cols in local_schema_view.columns_by_table.items()},
        column_role_hints=_column_role_hints(local_schema_view),
        relation_hints=relation_hints,
        schema_tags=_dedup_keep_order(tags),
        schema_recall_diagnostics=diag_payload,
    )


def _pred_sql_signal_view(
    *,
    pred_ast: Dict[str, Any],
    structure_flags: Optional[Dict[str, bool]] = None,
) -> PredSqlSignalView:
    select_items = _select_items(pred_ast)
    roles = _select_role_profile(pred_ast)
    arity = _select_arity(pred_ast)
    return PredSqlSignalView(
        select_arity=arity,
        select_items=select_items,
        select_role_profile=roles,
        output_shape_current={
            "arity": arity,
            "roles": roles,
            "grain": _grain_from_ast(pred_ast),
            "has_distinct": bool((pred_ast.get("select_shape") or {}).get("has_distinct")),
            "has_aggregate": bool((pred_ast.get("select_shape") or {}).get("has_aggregate")),
        },
        tables_used=_tables_from_ast(pred_ast),
        join_graph=_join_graph(pred_ast),
        predicate_profile=_predicate_profile(pred_ast),
        aggregate_profile=_aggregate_profile(pred_ast),
        group_order_profile=_group_order_profile(pred_ast),
        runtime_structure_flags={k: bool(v) for k, v in (structure_flags or {}).items() if bool(v)},
    )


def _runtime_signature(
    *,
    candidate_question_tags: List[str],
    candidate_pred_tags: List[str],
    pred_sql_view: PredSqlSignalView,
    schema_view: SchemaSignalView,
) -> Dict[str, Any]:
    return {
        "legacy_question_tags": list(candidate_question_tags),
        "legacy_pred_tags": list(candidate_pred_tags),
        "schema_tags": list(schema_view.schema_tags),
        "output_shape_current": dict(pred_sql_view.output_shape_current),
        "tables_used": list(pred_sql_view.tables_used),
        "slot_resolvability": {
            "local_table_count": len(schema_view.local_tables),
            "local_column_count": sum(len(cols) for cols in schema_view.columns_by_table.values()),
            "relation_hint_count": len(schema_view.relation_hints),
        },
    }


def _signal_atom(
    *,
    name: str,
    channel: str,
    source: str = "code",
    role: str = "descriptive",
    runtime_visible: bool = True,
    confidence: float = 1.0,
    evidence_span: Optional[str] = None,
    notes: Optional[str] = None,
) -> SignalAtom:
    return SignalAtom(
        name=name,
        channel=channel,
        source=source,
        role=role,
        runtime_visible=runtime_visible,
        confidence=confidence,
        evidence_span=evidence_span,
        notes=notes,
    )


def _question_signal_atoms(candidate_question_tags: List[str], question_view: QuestionSignalView) -> List[SignalAtom]:
    atoms: List[SignalAtom] = []
    operation_tags = set(question_view.operation_cues)
    route_tags = set(question_view.route_cues)
    grain_tags = {question_view.grain_cue} if question_view.grain_cue else set()
    for tag in candidate_question_tags:
        if tag in operation_tags or tag in route_tags or tag in grain_tags:
            role = "decisive"
        elif tag == QuestionTargetRole.PRIMARY_ENTITY.value:
            role = "diagnostic"
        else:
            role = "descriptive"
        atoms.append(
            _signal_atom(
                name=tag,
                channel="question",
                role=role,
                runtime_visible=True,
                confidence=0.8 if role == "decisive" else 0.55,
            )
        )
    for mention in question_view.schema_mention_hits:
        atoms.append(
            _signal_atom(
                name=f"schema_mention:{mention}",
                channel="question",
                role="descriptive",
                runtime_visible=True,
                confidence=0.7,
            )
        )
    return atoms


def _pred_signal_atoms(candidate_pred_tags: List[str]) -> Tuple[List[SignalAtom], List[SignalAtom], Dict[str, str]]:
    pred_atoms: List[SignalAtom] = []
    execution_atoms: List[SignalAtom] = []
    filter_reasons: Dict[str, str] = {}
    for tag in candidate_pred_tags:
        runtime_visible = tag not in EXECUTION_ONLY_PRED_TAGS
        reason = ""
        if not runtime_visible:
            reason = "requires gold SQL, execution comparison, or pred/gold structural comparison"
            filter_reasons[tag] = reason
        atom = _signal_atom(
            name=tag,
            channel="pred" if runtime_visible else "execution",
            role="decisive" if not runtime_visible else "descriptive",
            runtime_visible=runtime_visible,
            confidence=0.85 if not runtime_visible else 0.7,
            notes=reason or None,
        )
        if runtime_visible:
            pred_atoms.append(atom)
        else:
            execution_atoms.append(atom)
    return pred_atoms, execution_atoms, filter_reasons


def _schema_signal_atoms(schema_view: SchemaSignalView) -> List[SignalAtom]:
    atoms: List[SignalAtom] = []
    for tag in schema_view.schema_tags:
        atoms.append(
            _signal_atom(
                name=tag,
                channel="schema",
                role="diagnostic",
                runtime_visible=True,
                confidence=0.75,
            )
        )
    for hint in schema_view.relation_hints:
        kind = str(hint.get("kind") or "")
        if not kind:
            continue
        atoms.append(
            _signal_atom(
                name=f"relation_hint:{kind}",
                channel="schema",
                role="diagnostic",
                runtime_visible=True,
                confidence=0.7,
                evidence_span=str(hint.get("table") or hint.get("edge_count") or ""),
            )
        )
    return atoms


def _execution_signal_atoms(execution_comparison: Optional[Any]) -> List[SignalAtom]:
    payload = _model_payload(execution_comparison)
    if not payload:
        return []
    atoms: List[SignalAtom] = []
    pred_rows = payload.get("pred_row_count")
    gold_rows = payload.get("gold_row_count")
    if pred_rows is not None and gold_rows is not None and pred_rows != gold_rows:
        atoms.append(
            _signal_atom(
                name="execution.row_count_mismatch",
                channel="execution",
                role="diagnostic",
                runtime_visible=False,
                confidence=1.0,
                evidence_span=f"pred={pred_rows};gold={gold_rows}",
            )
        )
    if payload.get("row_sets_equivalent") is not None:
        atoms.append(
            _signal_atom(
                name=f"execution.row_sets_equivalent={payload.get('row_sets_equivalent')}",
                channel="execution",
                role="diagnostic",
                runtime_visible=False,
                confidence=1.0,
            )
        )
    if payload.get("projected_dtype_changed"):
        atoms.append(
            _signal_atom(
                name="execution.projected_dtype_changed",
                channel="execution",
                role="diagnostic",
                runtime_visible=False,
                confidence=1.0,
            )
        )
    if payload.get("pred_error"):
        atoms.append(
            _signal_atom(
                name="execution.pred_error",
                channel="execution",
                role="diagnostic",
                runtime_visible=False,
                confidence=1.0,
                evidence_span=str(payload.get("pred_error"))[:200],
            )
        )
    return atoms


def _stable_signatures(
    *,
    case_signal_view: CaseSignalView,
    delta_signature: Optional[DeltaSignature],
    legacy_signature: str,
    legacy_signature_dict: Dict[str, Any],
) -> Dict[str, Any]:
    pred = case_signal_view.pred_sql_view
    schema = case_signal_view.schema_view
    delta_payload = _model_payload(delta_signature)
    return {
        "question_signature": {
            "answer_slot_cues": list(case_signal_view.question_view.answer_slot_cues),
            "operation_cues": list(case_signal_view.question_view.operation_cues),
            "grain_cue": case_signal_view.question_view.grain_cue,
            "route_cues": list(case_signal_view.question_view.route_cues),
        },
        "pred_shape_signature": {
            "select_arity": pred.select_arity,
            "select_role_profile": list(pred.select_role_profile),
            "output_shape_current": dict(pred.output_shape_current),
            "predicate_profile": dict(pred.predicate_profile),
            "aggregate_profile": dict(pred.aggregate_profile),
            "group_order_profile": dict(pred.group_order_profile),
        },
        "schema_role_signature": {
            "local_tables": list(schema.local_tables),
            "relation_hint_kinds": _dedup_keep_order(
                [str(hint.get("kind") or "") for hint in schema.relation_hints]
            ),
            "column_role_counts": {
                role: list(schema.column_role_hints.values()).count(role)
                for role in sorted(set(schema.column_role_hints.values()))
            },
        },
        "slot_candidate_signature": dict(case_signal_view.runtime_signature.get("slot_resolvability") or {}),
        "delta_axes": list((delta_payload.get("semantic_delta") or {}).get("delta_axes") or []),
        "legacy_signature": legacy_signature,
        "legacy_signature_dict": dict(legacy_signature_dict),
    }


def build_case_signal_bundle(
    *,
    db_id: str,
    case_id: str,
    case_signal_view: CaseSignalView,
    delta_signature: Optional[DeltaSignature],
    candidate_question_tags: List[str],
    candidate_pred_tags: List[str],
    execution_comparison: Optional[Any],
    legacy_signature: str,
    legacy_signature_dict: Dict[str, Any],
) -> CaseSignalBundle:
    """Build audit-only signal provenance without changing runtime behavior."""
    pred_atoms, execution_pred_atoms, filter_reasons = _pred_signal_atoms(candidate_pred_tags)
    execution_atoms = execution_pred_atoms + _execution_signal_atoms(execution_comparison)
    return CaseSignalBundle(
        db_id=db_id,
        case_id=case_id,
        question_signals=_question_signal_atoms(
            candidate_question_tags,
            case_signal_view.question_view,
        ),
        pred_signals=pred_atoms,
        schema_signals=_schema_signal_atoms(case_signal_view.schema_view),
        execution_only_signals=execution_atoms,
        filtered_runtime_pred_tags=sorted(filter_reasons),
        filter_reasons=filter_reasons,
        stable_signatures=_stable_signatures(
            case_signal_view=case_signal_view,
            delta_signature=delta_signature,
            legacy_signature=legacy_signature,
            legacy_signature_dict=legacy_signature_dict,
        ),
    )


def build_case_signal_view(
    *,
    db_id: str,
    question: str,
    evidence: str,
    pred_sql: str,
    pred_ast: Dict[str, Any],
    local_schema_view: LocalSchemaView,
    candidate_question_tags: List[str],
    candidate_pred_tags: List[str],
    structure_flags: Optional[Dict[str, bool]] = None,
    local_schema_diagnostics: Optional[LocalSchemaViewDiagnostics] = None,
    case_id: str = "",
) -> CaseSignalView:
    """Build the answer-blind Phase A signal view.

    ``pred_sql`` is accepted for call-site clarity and future hashing, but Phase
    A avoids storing the full SQL again because RuntimeCaseView already has it.
    """
    _ = pred_sql
    question_view = _question_signal_view(
        question=question,
        evidence=evidence,
        local_schema_view=local_schema_view,
        candidate_question_tags=candidate_question_tags,
    )
    pred_sql_view = _pred_sql_signal_view(
        pred_ast=pred_ast,
        structure_flags=structure_flags,
    )
    schema_view = _schema_signal_view(
        local_schema_view=local_schema_view,
        local_schema_diagnostics=local_schema_diagnostics,
    )
    return CaseSignalView(
        db_id=db_id,
        case_id=case_id,
        question_view=question_view,
        pred_sql_view=pred_sql_view,
        schema_view=schema_view,
        runtime_signature=_runtime_signature(
            candidate_question_tags=candidate_question_tags,
            candidate_pred_tags=candidate_pred_tags,
            pred_sql_view=pred_sql_view,
            schema_view=schema_view,
        ),
    )


def _output_shape_delta(pred_ast: Dict[str, Any], gold_ast: Dict[str, Any]) -> OutputShapeDelta:
    current_arity = _select_arity(pred_ast)
    target_arity = _select_arity(gold_ast)
    current_roles = _select_role_profile(pred_ast)
    target_roles = _select_role_profile(gold_ast)
    arity_delta: Optional[int] = None
    arity_direction = "unknown"
    if current_arity is not None and target_arity is not None:
        arity_delta = target_arity - current_arity
        if arity_delta > 0:
            arity_direction = "increase"
        elif arity_delta < 0:
            arity_direction = "decrease"
        else:
            arity_direction = "same"
    operation: Optional[str]
    if current_arity is None or target_arity is None:
        operation = "unknown"
    elif current_arity < target_arity:
        operation = "add_output_slot"
    elif current_arity > target_arity:
        operation = "remove_output_slot"
    elif current_roles != target_roles:
        operation = "replace_or_reorder_output_slot"
    else:
        operation = "no_output_shape_change"
    return OutputShapeDelta(
        current_arity=current_arity,
        target_arity=target_arity,
        arity_delta=arity_delta,
        arity_direction=arity_direction,
        current_roles=current_roles,
        target_roles=target_roles,
        current_grain=_grain_from_ast(pred_ast),
        target_grain=_grain_from_ast(gold_ast),
        operation=operation,
    )


def _set_delta(left: List[str], right: List[str]) -> Dict[str, List[str]]:
    left_set = {v.lower(): v for v in left}
    right_set = {v.lower(): v for v in right}
    return {
        "pred_only": [left_set[key] for key in sorted(set(left_set) - set(right_set))],
        "gold_only": [right_set[key] for key in sorted(set(right_set) - set(left_set))],
        "shared": [left_set[key] for key in sorted(set(left_set) & set(right_set))],
    }


def _delta_axes(
    *,
    pred_ast: Dict[str, Any],
    gold_ast: Dict[str, Any],
    output_delta: OutputShapeDelta,
    structure_flags: Dict[str, bool],
) -> List[str]:
    axes: List[str] = []
    if output_delta.operation and output_delta.operation != "no_output_shape_change":
        axes.append("output_shape_delta")
    if output_delta.current_grain != output_delta.target_grain:
        axes.append("grain_delta")
    if _set_delta(_tables_from_ast(pred_ast), _tables_from_ast(gold_ast))["pred_only"] or _set_delta(
        _tables_from_ast(pred_ast), _tables_from_ast(gold_ast)
    )["gold_only"]:
        axes.append("source_route_delta")
    if _predicate_profile(pred_ast) != _predicate_profile(gold_ast):
        axes.append("predicate_scope_delta")
    if _aggregate_profile(pred_ast) != _aggregate_profile(gold_ast) or _group_order_profile(
        pred_ast
    ).get("group_by_keys") != _group_order_profile(gold_ast).get("group_by_keys"):
        axes.append("aggregation_unit_delta")
    if _group_order_profile(pred_ast).get("order_by") != _group_order_profile(gold_ast).get(
        "order_by"
    ) or pred_ast.get("limit") != gold_ast.get("limit"):
        axes.append("ranking_contract_delta")
    if any(
        structure_flags.get(flag)
        for flag in (
            "function_added",
            "function_removed",
            "function_changed",
            "select_expr_changed",
        )
    ):
        axes.append("formula_delta")
    if (
        output_delta.current_arity == output_delta.target_arity
        and output_delta.current_roles != output_delta.target_roles
    ):
        axes.append("proxy_slot_delta")
    return _dedup_keep_order(axes)


def build_delta_signature(
    *,
    db_id: str,
    pred_sql: str,
    gold_sql: str,
    pred_ast: Dict[str, Any],
    gold_ast: Dict[str, Any],
    legacy_signature: str,
    legacy_signature_dict: Dict[str, Any],
    structure_flags: Dict[str, bool],
    execution_comparison: Optional[Any] = None,
    case_id: str = "",
) -> DeltaSignature:
    """Build the answer-aware offline Phase A delta signature."""
    _ = (pred_sql, gold_sql)
    output_delta = _output_shape_delta(pred_ast, gold_ast)
    pred_tables = _tables_from_ast(pred_ast)
    gold_tables = _tables_from_ast(gold_ast)
    pred_select = _select_items(pred_ast)
    gold_select = _select_items(gold_ast)
    axes = _delta_axes(
        pred_ast=pred_ast,
        gold_ast=gold_ast,
        output_delta=output_delta,
        structure_flags=structure_flags,
    )
    execution_payload = _model_payload(execution_comparison)
    return DeltaSignature(
        db_id=db_id,
        case_id=case_id,
        output_shape_delta=output_delta,
        structural_delta={
            "legacy_signature": legacy_signature,
            "legacy_signature_dict": legacy_signature_dict,
            "structure_flags_true": [k for k, v in structure_flags.items() if v],
            "table_set_delta": _set_delta(pred_tables, gold_tables),
            "select_item_delta": _set_delta(pred_select, gold_select),
            "predicate_count_delta": {
                "pred": len(pred_ast.get("predicates") or []),
                "gold": len(gold_ast.get("predicates") or []),
            },
            "group_by_count_delta": {
                "pred": len(pred_ast.get("group_by_keys") or []),
                "gold": len(gold_ast.get("group_by_keys") or []),
            },
            "order_by_count_delta": {
                "pred": len(pred_ast.get("order_by") or []),
                "gold": len(gold_ast.get("order_by") or []),
            },
        },
        semantic_delta={
            "delta_axes": axes,
            "current_output_roles": output_delta.current_roles,
            "target_output_roles": output_delta.target_roles,
            "current_grain": output_delta.current_grain,
            "target_grain": output_delta.target_grain,
            "pred_aggregate_profile": _aggregate_profile(pred_ast),
            "gold_aggregate_profile": _aggregate_profile(gold_ast),
            "pred_predicate_profile": _predicate_profile(pred_ast),
            "gold_predicate_profile": _predicate_profile(gold_ast),
        },
        execution_delta=execution_payload,
        repair_delta={
            "output_shape_operation": output_delta.operation,
            "delta_axes": axes,
            "legacy_primary_edit_family": legacy_signature_dict.get("primary_edit_family"),
            "legacy_target_output_contract": legacy_signature_dict.get("target_output_contract"),
        },
        delta_confidence={
            "has_gold_sql": bool(gold_sql),
            "has_execution_feedback": bool(execution_payload),
            "pred_parse_ok": bool(pred_ast.get("parse_ok", True)),
            "gold_parse_ok": bool(gold_ast.get("parse_ok", True)),
            "answer_blind": False,
        },
    )


# =============================================================================
# 一站式入口
# =============================================================================


def preprocess_case(
    *,
    db_id: str,
    question: str,
    evidence: str,
    pred_sql: str,
    gold_sql: str,
    access: DBSchemaAccess,
    t_mem: Optional[List[str]] = None,
    allow_two_hop_justifications: Optional[List[str]] = None,
    execution_comparison: Optional[Any] = None,
    case_id: str = "",
) -> Dict[str, Any]:
    """一次性跑完 code 预处理，返回 LLM nodes 和 ErrorInstance 组装所需的全部字段。

    若 ``execution_comparison`` 已在 caller 端算好（通常由 pipeline_v2 在调用前做完），
    会透传给 ``extract_candidate_pred_tags`` 以 emit execution-driven deterministic tags。
    """

    bundle = compute_structure_bundle(pred_sql, gold_sql)
    pred_ast: Dict[str, Any] = bundle["pred_ast"]
    gold_ast: Dict[str, Any] = bundle["gold_ast"]

    t_pred = extract_tables_from_pred(pred_ast)
    all_tables = list(access.list_tables())
    t_q = extract_tables_from_question(
        question=question, evidence=evidence, all_tables=all_tables
    )
    t_mem = list(t_mem or [])

    view, diag = build_local_schema_view(
        db_id=db_id,
        access=access,
        t_pred=t_pred,
        t_q=t_q,
        t_mem=t_mem,
        allow_two_hop_justifications=allow_two_hop_justifications,
    )

    candidate_question_tags = extract_candidate_question_tags(
        question=question, evidence=evidence
    )
    candidate_pred_tags = extract_candidate_pred_tags(
        pred_ast=pred_ast,
        gold_ast=gold_ast,
        structure_flags=bundle["structure_flags"],
        execution_comparison=execution_comparison,
    )
    case_signal_view = build_case_signal_view(
        db_id=db_id,
        case_id=case_id,
        question=question,
        evidence=evidence,
        pred_sql=pred_sql,
        pred_ast=pred_ast,
        local_schema_view=view,
        local_schema_diagnostics=diag,
        candidate_question_tags=candidate_question_tags,
        candidate_pred_tags=candidate_pred_tags,
        structure_flags=bundle["structure_flags"],
    )
    delta_signature = build_delta_signature(
        db_id=db_id,
        case_id=case_id,
        pred_sql=pred_sql,
        gold_sql=gold_sql,
        pred_ast=pred_ast,
        gold_ast=gold_ast,
        legacy_signature=bundle["legacy_signature"],
        legacy_signature_dict=bundle["legacy_signature_dict"],
        structure_flags=bundle["structure_flags"],
        execution_comparison=execution_comparison,
    )
    case_signal_bundle = build_case_signal_bundle(
        db_id=db_id,
        case_id=case_id,
        case_signal_view=case_signal_view,
        delta_signature=delta_signature,
        candidate_question_tags=candidate_question_tags,
        candidate_pred_tags=candidate_pred_tags,
        execution_comparison=execution_comparison,
        legacy_signature=bundle["legacy_signature"],
        legacy_signature_dict=bundle["legacy_signature_dict"],
    )

    return {
        # structure-side
        "pred_ast": pred_ast,
        "gold_ast": gold_ast,
        "join_edges_diff": bundle["join_edges_diff"],
        "structural_delta": bundle["structural_delta"],
        "structure_flags": bundle["structure_flags"],
        "legacy_signature": bundle["legacy_signature"],
        "legacy_signature_dict": bundle["legacy_signature_dict"],
        # tables
        "t_pred": t_pred,
        "t_q": t_q,
        "t_mem": t_mem,
        # local schema
        "local_schema_view": view,
        "local_schema_diagnostics": diag,
        # candidate tags
        "candidate_question_tags": candidate_question_tags,
        "candidate_pred_tags": candidate_pred_tags,
        # Phase A signal audit
        "case_signal_view": case_signal_view,
        "case_signal_bundle": case_signal_bundle,
        "delta_signature": delta_signature,
    }


__all__ = [
    "compute_structure_bundle",
    "extract_tables_from_pred",
    "extract_tables_from_question",
    "extract_candidate_question_tags",
    "extract_candidate_pred_tags",
    "build_case_signal_view",
    "build_case_signal_bundle",
    "build_delta_signature",
    "preprocess_case",
]
