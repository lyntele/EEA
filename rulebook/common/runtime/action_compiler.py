"""Action Compiler: code-side candidate enumeration + LLM selection.

Phase 1.4（δ 策略，按 codex 建议）：

- enumerate_candidates 现在消费 ``memory_objects: List[GroupSummary]``（不再消费
  ErrorInstanceV2）——runtime 不看 gold，所以上游要么是 library 里已有的 Group，
  要么是在 accumulate 阶段从 ErrorInstance 转来的 singleton Group。
- primitive 函数抽象为 ``(case_view, skeleton, slots, group_id, group_type)``，
  内部逻辑几乎不变，只是数据源不再绑定 ErrorInstanceV2。
- 每个 ActionCandidate 记录 ``source_group_id`` / ``source_group_type``，
  LLM 选中后 Action.source_group_id 直接承继，保证 runtime attribution 可追溯。

Precision gates（Phase 1.3 保留）：
- DROP_SIDE 支持 SELECT pair output，也支持 WHERE/PREDICATE 中的 OR side
  candidates；后者只做结构化 side 枚举，不绑定具体库名/列名。
- ADD_SELECT_SLOT 的 pred dedup 用 ``_parse_expr_column`` normalize

已实现的 10 个 primitive:
  ADD_SELECT_SLOT / REPLACE_SELECT_SLOT / DROP_SELECT_SLOT / DROP_SIDE /
  INSERT_BRIDGE / REROUTE_FACT / CHANGE_GRAIN / MOVE_CONDITION /
  SWITCH_CANONICAL_FIELD / MATERIALIZE_RANKING_OUTPUT
"""

from __future__ import annotations

import hashlib
import json
import re
from itertools import combinations, permutations
from typing import Any, Dict, List, Optional, Sequence, Tuple

from method.EEA.rulebook.common.core.data_structures import (
    ActionCandidate,
    ActionCandidateSet,
    GroupSummary,
    InstantiationSlot,
    LocalSchemaView,
    LocalSchemaViewDiagnostics,
    PkFkEdge,
    RepairSkeletonStructural,
    RuntimeCaseView,
)
from method.EEA.rulebook.common.learning.shared_program_synthesizer import canonical_op_lowering_family
from method.EEA.rulebook.common.core.vocabulary import (
    ActionPrimitive,
    GroupType,
    Locus,
    OpFamily,
    SCHEMA_RECALL_MISS_CATEGORIES,
    TargetFamily,
)


# =============================================================================
# 辅助函数
# =============================================================================


MAX_MULTI_SLOT_COMBINATIONS = 80
MAX_MULTI_SLOT_OPTION_POOL = 12
MAX_PAIR_REPLACE_OPTION_POOL = 24
MAX_REPLACE_SELECT_CANDIDATES = 60
WUV2_2_SEED_TARGET_BINDING_DISABLED_REASON = (
    "wuv2_2_seed_target_binding_disabled_pending_binding_contract"
)


def _hash_cand(payload: str) -> str:
    """生成稳定 candidate_id，8 位短 hash。"""
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:8]


def _get_column_role(
    table: str, column: str, schema_view: LocalSchemaView
) -> Optional[str]:
    for hint in schema_view.semantic_hints:
        if hint.table.lower() == table.lower() and hint.column.lower() == column.lower():
            return hint.role_family
    return None


def _is_role_allowed(role: Optional[str], allowed: Sequence[str]) -> bool:
    """若 allowed 为空，默认接受任何 role；否则 role 必须命中集合。"""
    if not allowed:
        return True
    if role is None:
        return False
    return role in allowed


def _slots_of_kind(slots: Sequence[InstantiationSlot], kind: str) -> List[InstantiationSlot]:
    return [s for s in slots if s.kind == kind]


def _parse_expr_column(expr: str) -> Optional[str]:
    """从 "alias.col" / "col" / "COUNT(x.y)" 粗提取列名。"""
    if not expr:
        return None
    s = expr.strip()
    alias_match = re.search(
        r"\bas\s+(?:\"([^\"]+)\"|`([^`]+)`|\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_]*))\s*$",
        s,
        flags=re.IGNORECASE,
    )
    if alias_match:
        return next(value for value in alias_match.groups() if value is not None)
    if "(" in s and ")" in s:
        import re as _re
        m = _re.search(r"([\w\"`\[\]]+\.[\w\"`\[\]]+|\b[\w]+\b)\s*\)\s*$", s.rstrip(")"))
        if m:
            s = m.group(1)
    s = s.strip('"`[] ')
    if "." in s:
        s = s.rsplit(".", 1)[-1].strip('"`[]')
    if not s or not any(c.isalpha() or c == "_" for c in s):
        return None
    return s


def _text_tokens(value: str) -> List[str]:
    """Normalize SQL/question text into comparable lexical tokens.

    This is used only to order bounded candidate pools. It is not a trigger
    signal and does not encode database-specific vocabulary.
    """
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
    text = text.replace("_", " ").replace("-", " ")
    return [
        token.lower()
        for token in re.findall(r"[A-Za-z0-9]+", text)
        if len(token) > 1
    ]


def _token_variants(token: str) -> set[str]:
    token = str(token or "").lower()
    variants = {token} if token else set()
    if len(token) > 3 and token.endswith("s"):
        variants.add(token[:-1])
    if len(token) > 5 and token.endswith("ing"):
        variants.add(token[:-3])
    return variants


def _token_positions(text: str) -> Dict[str, int]:
    positions: Dict[str, int] = {}
    for idx, token in enumerate(_text_tokens(text)):
        for variant in _token_variants(token):
            positions.setdefault(variant, idx)
    return positions


def _column_name_tokens(option: Dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for token in _text_tokens(str(option.get("target_column") or "")):
        tokens.update(_token_variants(token))
    return tokens


def _table_name_tokens(option: Dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for token in _text_tokens(str(option.get("target_table") or "")):
        tokens.update(_token_variants(token))
    return tokens


def _rank_column_options_for_text(
    options: Sequence[Dict[str, Any]],
    *,
    question: str,
    evidence: str,
) -> List[Dict[str, Any]]:
    """Order schema columns for multi-slot candidate budgeting.

    The rank is generic lexical alignment with runtime-visible text. It avoids
    hardcoded domain terms while keeping multi-slot enumeration bounded.
    """
    positions = _token_positions(f"{question} {evidence}")
    missing_pos = len(positions) + 10_000

    def _rank(option: Dict[str, Any]) -> tuple[int, int, int, int, str, str]:
        column_tokens = _column_name_tokens(option)
        table_tokens = _table_name_tokens(option)
        column_positions = [positions[token] for token in column_tokens if token in positions]
        table_positions = [positions[token] for token in table_tokens if token in positions]
        first_column_pos = min(column_positions) if column_positions else missing_pos
        first_table_pos = min(table_positions) if table_positions else missing_pos
        return (
            first_column_pos,
            -len(set(column_positions)),
            first_table_pos,
            -len(set(table_positions)),
            str(option.get("target_table") or "").lower(),
            str(option.get("target_column") or "").lower(),
        )

    return sorted([dict(option) for option in options], key=_rank)


def _option_key(option: Dict[str, Any]) -> str:
    return f"{option.get('target_table')}::{option.get('target_column')}"


def _memory_mentioned_options(
    options: Sequence[Dict[str, Any]],
    *,
    memory_text: str,
) -> List[Dict[str, Any]]:
    raw_text = str(memory_text or "")
    text = raw_text.lower()
    if not text:
        return []

    def _first_mention(option: Dict[str, Any]) -> Optional[int]:
        table_raw = str(option.get("target_table") or "")
        column_raw = str(option.get("target_column") or "")
        table = table_raw.lower()
        column = column_raw.lower()
        if not column:
            return None
        hits: List[int] = []

        code_patterns = [
            rf"\b{re.escape(table)}\s*\.\s*\"?{re.escape(column)}\"?\b"
            if table
            else "",
            rf"\b[A-Za-z_][A-Za-z0-9_]*\s*\.\s*\"?{re.escape(column)}\"?\b",
            rf"`{re.escape(column)}`",
            rf"\"{re.escape(column)}\"",
        ]
        # Plain identifier mentions are treated as code-like only when the
        # schema name itself carries identifier structure. Natural words such
        # as "street" in "mailing street" should not become memory targets.
        if re.search(r"[a-z][A-Z]|\d|[_\s()/%-]", column_raw):
            code_patterns.append(rf"\b{re.escape(column)}\b")
        for pattern in code_patterns:
            if not pattern:
                continue
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                hits.append(match.start())

        arrow_pattern = re.compile(r"(?:->|=>|改成|切回|优先)\s*([^。\n；;，,]+)")
        for match in arrow_pattern.finditer(raw_text):
            rhs = match.group(1).lower()
            if re.search(rf"\b{re.escape(column)}\b", rhs, flags=re.IGNORECASE):
                hits.append(match.start(1))
        return min(hits) if hits else None

    ranked: List[tuple[int, str, str, Dict[str, Any]]] = []
    for option in options:
        first = _first_mention(option)
        if first is None:
            continue
        ranked.append(
            (
                first,
                str(option.get("target_table") or "").lower(),
                str(option.get("target_column") or "").lower(),
                dict(option),
            )
        )
    return [row[3] for row in sorted(ranked)]


def _add_memory_alignment(
    *,
    target_columns: Sequence[Dict[str, Any]],
    memory_text: str,
    question: str,
    evidence: str,
    pred_exprs: Sequence[str] = (),
) -> Dict[str, Any]:
    memory_mentioned = _memory_mentioned_options(target_columns, memory_text=memory_text)
    memory_keys = {_option_key(option).lower() for option in memory_mentioned}
    text_positions = _token_positions(f"{question} {evidence}")
    question_hit_columns: List[str] = []
    question_hit_tables: List[str] = []
    current_select_cooccurrences: List[str] = []
    clauses = re.split(r"[\n。；;]", str(memory_text or ""))
    for option in target_columns:
        target_variants = _identifier_variants(
            f"{option.get('target_table')}.{option.get('target_column')}"
        )
        target_variants.extend(_identifier_variants(str(option.get("target_column") or "")))
        if any(token in text_positions for token in _column_name_tokens(option)):
            question_hit_columns.append(
                f"{option.get('target_table')}.{option.get('target_column')}"
            )
        if any(token in text_positions for token in _table_name_tokens(option)):
            question_hit_tables.append(
                f"{option.get('target_table')}.{option.get('target_column')}"
            )
        for clause in clauses:
            if not _memory_mentions_any(clause, target_variants):
                continue
            if any(_memory_mentions_source_expr(clause, expr) for expr in pred_exprs):
                current_select_cooccurrences.append(
                    f"{option.get('target_table')}.{option.get('target_column')}"
                )
                break
    return {
        "memory_mentioned_count": sum(
            1 for option in target_columns if _option_key(option).lower() in memory_keys
        ),
        "current_select_cooccurrence_count": len(current_select_cooccurrences),
        "current_select_cooccurrences": current_select_cooccurrences,
        "question_column_hit_count": len(question_hit_columns),
        "question_table_hit_count": len(question_hit_tables),
        "question_hit_columns": question_hit_columns,
        "question_hit_tables": question_hit_tables,
    }


def _add_candidate_sort_key(candidate: ActionCandidate) -> tuple[int, int, int, int, int, int, str]:
    args = candidate.arguments or {}
    alignment = args.get("memory_alignment") or {}
    target_columns = args.get("target_columns") or []
    shape_delta = args.get("output_shape_delta") or {}
    try:
        target_slot_count = int(args.get("target_slot_count") or len(target_columns) or 1)
    except Exception:
        target_slot_count = len(target_columns) or 1
    try:
        shape_delta_count = abs(int(shape_delta.get("arity_delta") or 0))
    except Exception:
        shape_delta_count = 0
    slot_count_matches_shape = target_slot_count == shape_delta_count if shape_delta_count else False
    target_key = ",".join(
        f"{item.get('target_table')}.{item.get('target_column')}" for item in target_columns
    )
    if slot_count_matches_shape:
        return (
            0,
            -int(alignment.get("current_select_cooccurrence_count") or 0),
            -int(alignment.get("memory_mentioned_count") or 0),
            -int(alignment.get("question_column_hit_count") or 0),
            -int(alignment.get("question_table_hit_count") or 0),
            target_slot_count,
            target_key.lower(),
        )
    return (
        1,
        -int(alignment.get("question_column_hit_count") or 0),
        -int(alignment.get("question_table_hit_count") or 0),
        -int(alignment.get("memory_mentioned_count") or 0),
        -int(alignment.get("current_select_cooccurrence_count") or 0),
        target_slot_count,
        target_key.lower(),
    )


def _identifier_variants(value: str) -> List[str]:
    raw = str(value or "").strip()
    variants = [raw]
    bare = _parse_expr_column(raw)
    if bare:
        variants.append(bare)
    out: List[str] = []
    seen: set[str] = set()
    for variant in variants:
        cleaned = variant.strip().strip('"`[]')
        if not cleaned:
            continue
        key = cleaned.lower()
        if key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


def _memory_mentions_any(text: str, variants: Sequence[str]) -> bool:
    lowered = str(text or "").lower()
    for variant in variants:
        raw = str(variant or "").strip().strip('"`[]')
        if not raw:
            continue
        candidates = {raw.lower()}
        if "." in raw:
            candidates.add(raw.rsplit(".", 1)[-1].strip('"`[]').lower())
        for candidate in candidates:
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(candidate)}(?![A-Za-z0-9_])"
            if re.search(pattern, lowered):
                return True
    return False


def _memory_mentions_source_expr(text: str, pred_expr: str) -> bool:
    lowered = str(text or "").lower()
    raw = str(pred_expr or "").strip()
    variants = {
        raw,
        raw.replace('"', ""),
        raw.replace("`", ""),
        raw.replace("[", "").replace("]", ""),
    }
    for variant in variants:
        cleaned = variant.strip().lower()
        if cleaned and cleaned in lowered:
            return True
    return False


def _replace_memory_alignment(
    *,
    pred_expr: str,
    target_table: str,
    target_column: str,
    memory_text: str,
) -> Dict[str, Any]:
    source_variants = _identifier_variants(pred_expr)
    target_variants = _identifier_variants(f"{target_table}.{target_column}")
    target_variants.extend(_identifier_variants(target_column))
    source_mentioned = _memory_mentions_source_expr(memory_text, pred_expr)
    target_mentioned = _memory_mentions_any(memory_text, target_variants)
    pair_mentioned = False
    if source_mentioned and target_mentioned:
        for clause in re.split(r"[\n。；;]", str(memory_text or "")):
            if _memory_mentions_source_expr(clause, pred_expr) and _memory_mentions_any(
                clause, target_variants
            ):
                pair_mentioned = True
                break
    return {
        "source_mentioned": source_mentioned,
        "target_mentioned": target_mentioned,
        "pair_mentioned": pair_mentioned,
        "source_terms": source_variants,
        "target_terms": target_variants,
    }


def _dedupe_column_options(options: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for option in options:
        key = (
            str(option.get("target_table") or "").lower(),
            str(option.get("target_column") or "").lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(option))
    return out


def _replace_candidate_sort_key(candidate: ActionCandidate) -> tuple[int, int, int, int, int, int, str]:
    args = candidate.arguments or {}
    alignment = args.get("memory_alignment") or {}
    replace_count = int(args.get("replace_count") or 1)
    source_slot_index = int((args.get("source_slot_indexes") or [args.get("source_slot_index", 0)])[0] or 0)
    target_columns = args.get("target_columns") or []
    target_key = ",".join(
        f"{item.get('target_table')}.{item.get('target_column')}" for item in target_columns
    )
    return (
        0 if args.get("canonical_ref_binding") else 1,
        0 if replace_count > 1 else 1,
        0 if alignment.get("pair_mentioned") else 1,
        0 if alignment.get("source_mentioned") and alignment.get("target_mentioned") else 1,
        0 if alignment.get("source_mentioned") else 1,
        source_slot_index,
        target_key.lower(),
    )


def _compact_repair_program_for_action(
    repair_program: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Keep action contracts compact enough for compiler/rewrite prompts.

    The executable contract is in op/locus/slots/guards/arguments. Long evidence
    text is audit metadata and should not be repeated in every candidate row.
    """
    out: List[Dict[str, Any]] = []
    for step in repair_program:
        payload = dict(step)
        evidence = payload.get("source_evidence") or []
        if isinstance(evidence, str):
            evidence_items = [evidence]
        else:
            evidence_items = [str(item) for item in evidence if str(item)]
        payload["source_evidence"] = [item[:240] for item in evidence_items[:2]]
        payload["supporting_case_ids"] = [
            str(item) for item in (payload.get("supporting_case_ids") or [])[:8]
        ]
        out.append(payload)
    return out


def _pair_output_distinct_dependency_args(
    case_view: RuntimeCaseView,
    args: Dict[str, Any],
) -> Dict[str, Any]:
    current_shape = _current_output_shape_for_compiler(case_view)
    try:
        current_arity = int(current_shape.get("arity") or 0)
    except Exception:
        current_arity = 0
    if (
        current_arity < 2
        or bool(current_shape.get("has_distinct"))
        or bool(current_shape.get("has_aggregate"))
    ):
        return args
    out = dict(args)
    scopes = list(out.get("required_edit_scopes") or [])
    if "SELECT" not in scopes:
        scopes.append("SELECT")
    out["required_edit_scopes"] = scopes
    out["distinct_dependency"] = "SELECT_ENFORCE_DISTINCT"
    preserve = {
        str(item)
        for item in (out.get("preserve_invariants") or [])
        if str(item)
    }
    preserve.add("SELECT_ENFORCE_DISTINCT")
    out["preserve_invariants"] = sorted(preserve)
    cleanup = [
        item if isinstance(item, dict) else {"kind": str(item)}
        for item in (out.get("cleanup_edits") or [])
        if item
    ]
    if not any(str(item.get("kind") or "") == "enforce_distinct" for item in cleanup):
        cleanup.append({"kind": "enforce_distinct"})
    out["cleanup_edits"] = cleanup
    return out


def _drop_select_alias_cleanup_dependency_args(args: Dict[str, Any]) -> Dict[str, Any]:
    exprs = [str(expr).strip() for expr in (args.get("from_exprs") or []) if str(expr).strip()]
    if args.get("from_expr") is not None and str(args.get("from_expr")).strip():
        exprs.append(str(args.get("from_expr")).strip())
    if not exprs:
        return args
    out = dict(args)
    scopes = list(out.get("required_edit_scopes") or [])
    if "JOIN" not in scopes:
        scopes.append("JOIN")
    out["required_edit_scopes"] = scopes
    cleanup = [
        item if isinstance(item, dict) else {"op_id": str(item)}
        for item in (out.get("cleanup_edits") or [])
        if item
    ]
    if not any(
        str(item.get("kind") or "") == "drop_unreferenced_join_for_dropped_select_alias"
        for item in cleanup
    ):
        cleanup.append({"kind": "drop_unreferenced_join_for_dropped_select_alias"})
    out["cleanup_edits"] = cleanup
    return out


def _sql_alias_map(sql: str) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    pattern = re.compile(
        r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_\"`\[\].]*)"
        r"(?:\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*))?",
        re.IGNORECASE,
    )
    for match in pattern.finditer(str(sql or "")):
        table = match.group(1).strip('"`[]')
        alias = (match.group(2) or table.rsplit(".", 1)[-1]).strip('"`[]')
        if alias and alias.lower() not in {"on", "where", "inner", "left", "right", "full", "cross"}:
            aliases[alias.lower()] = table.rsplit(".", 1)[-1]
        bare_table = table.rsplit(".", 1)[-1]
        aliases.setdefault(bare_table.lower(), bare_table)
    return aliases


def _expr_table_column(expr: str, alias_map: Dict[str, str]) -> tuple[Optional[str], Optional[str]]:
    text = str(expr or "").strip()
    match = re.search(
        r"([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*"
        r"(?:\"([^\"]+)\"|`([^`]+)`|\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_]*))",
        text,
    )
    if match:
        table_or_alias = match.group(1).strip('"`[]')
        column = next(
            value for value in match.groups()[1:] if value is not None
        ).strip('"`[]')
        return alias_map.get(table_or_alias.lower(), table_or_alias), column
    column = _parse_expr_column(text)
    return None, column


def _same_source_target(
    source_ref: tuple[Optional[str], Optional[str]],
    target: Dict[str, Any],
) -> bool:
    source_table, source_col = source_ref
    target_table = str(target.get("target_table") or "")
    target_col = str(target.get("target_column") or "")
    if not source_col or source_col.lower() != target_col.lower():
        return False
    if source_table:
        return source_table.lower() == target_table.lower()
    return True


def _ref_identity(ref: Dict[str, Any]) -> str:
    evidence = _payload(ref.get("evidence"))
    table = str(evidence.get("original_table") or "").strip().lower()
    column = str(evidence.get("original_column") or "").strip().lower()
    expression = str(evidence.get("original_expression") or ref.get("expression") or "").strip().lower()
    if table or column:
        return f"{table}.{column}"
    return expression


def _canonical_output_ref_variants(
    canonical_op: Dict[str, Any],
    key: str,
) -> List[List[Dict[str, Any]]]:
    args = _payload(canonical_op.get("arguments"))
    direct = [_payload(ref) for ref in (args.get(key) or []) if _payload(ref)]
    if direct:
        return [direct]
    variants: List[List[Dict[str, Any]]] = []
    for variant in args.get("member_argument_variants") or []:
        payload = _payload(variant)
        refs = [_payload(ref) for ref in (payload.get(key) or []) if _payload(ref)]
        if refs:
            variants.append(refs)
    if variants:
        return variants
    ref_source = "target_sql" if key == "target_output_refs" else "pred_sql"
    role_refs = [
        _payload(ref)
        for ref in (args.get("canonical_refs") or canonical_op.get("role_refs") or [])
        if _payload(ref)
    ]
    refs = [
        ref
        for ref in role_refs
        if str(ref.get("source") or "") == ref_source
        and str(ref.get("sql_role") or "") == "output_slot"
    ]
    return [refs] if refs else []


def _canonical_keep_slot_indexes(canonical_op: Dict[str, Any]) -> List[int]:
    """Infer source output slots retained by a target-output-subset contract."""
    if not _canonical_target_output_subset(canonical_op):
        return []
    source_variants = _canonical_output_ref_variants(canonical_op, "source_output_refs")
    target_variants = _canonical_output_ref_variants(canonical_op, "target_output_refs")
    variant_keeps: List[frozenset[int]] = []
    for index, source_refs in enumerate(source_variants):
        target_refs = target_variants[min(index, len(target_variants) - 1)] if target_variants else []
        if not source_refs or not target_refs:
            continue
        keep: set[int] = set()
        for target_ref in target_refs:
            target_identity = _ref_identity(target_ref)
            target_path = str(target_ref.get("path_role") or "")
            target_slot = target_ref.get("slot_index")
            matches: List[Dict[str, Any]] = []
            if target_path:
                matches = [
                    ref
                    for ref in source_refs
                    if str(ref.get("path_role") or "") == target_path
                    and _ref_identity(ref) == target_identity
                ]
                if not matches:
                    path_matches = [
                        ref for ref in source_refs if str(ref.get("path_role") or "") == target_path
                    ]
                    if len(path_matches) == 1:
                        matches = path_matches
            if not matches and target_slot is not None:
                matches = [
                    ref
                    for ref in source_refs
                    if ref.get("slot_index") == target_slot
                    and _ref_identity(ref) == target_identity
                ]
            if not matches:
                identity_matches = [
                    ref for ref in source_refs if _ref_identity(ref) == target_identity
                ]
                if len(identity_matches) == 1:
                    matches = identity_matches
            for ref in matches:
                try:
                    keep.add(int(ref.get("slot_index")))
                except Exception:
                    continue
        if keep:
            variant_keeps.append(frozenset(keep))
    if not variant_keeps:
        return []
    if len(set(variant_keeps)) == 1:
        return sorted(next(iter(set(variant_keeps))))
    return []


def _candidate_side_indexes(candidate: ActionCandidate) -> set[int]:
    args = candidate.arguments or {}
    raw_values = args.get("side_indexes") or args.get("source_slot_indexes") or []
    values = list(raw_values) if isinstance(raw_values, list) else [raw_values]
    if args.get("side_index") is not None:
        values.append(args.get("side_index"))
    indexes: set[int] = set()
    for value in values:
        try:
            indexes.add(int(value))
        except Exception:
            continue
    return indexes


def _narrow_select_drop_candidates(
    *,
    candidate_set: ActionCandidateSet,
    canonical_op: Dict[str, Any],
    pred_exprs: Sequence[str],
) -> ActionCandidateSet:
    shape = _canonical_shape(canonical_op)
    requires_subset_binding = (
        str(shape.get("arity_direction") or "").lower() == "decrease"
        and _canonical_target_output_subset(canonical_op)
    )
    keep_indexes = _canonical_keep_slot_indexes(canonical_op)
    if not keep_indexes or not pred_exprs:
        if requires_subset_binding:
            return candidate_set.model_copy(
                update={
                    "candidates": [],
                    "empty_reason": "role_slot_unbound:target_output_subset_slot_binding_unresolved",
                }
            )
        return candidate_set
    drop_indexes = sorted(set(range(len(pred_exprs))) - set(keep_indexes))
    if not drop_indexes:
        return candidate_set.model_copy(
            update={
            "candidates": [],
            "empty_reason": "ambiguous_keep_drop_side:target_output_subset_binding_has_no_drop_slot",
        }
        )
    narrowed: List[ActionCandidate] = []
    for candidate in candidate_set.candidates or []:
        if _candidate_side_indexes(candidate) != set(drop_indexes):
            continue
        args = dict(candidate.arguments or {})
        args["keep_side_indexes"] = keep_indexes
        args["drop_binding_source"] = "canonical_target_output_subset"
        args["drop_reason"] = "target_output_subset_of_source_outputs"
        narrowed.append(candidate.model_copy(update={"arguments": args}))
    if not narrowed:
        return candidate_set.model_copy(
            update={
                "candidates": [],
                "empty_reason": (
                    "ambiguous_keep_drop_side:target_output_subset_slot_binding_unmatched:"
                    f" keep={keep_indexes} drop={drop_indexes}"
                ),
            }
        )
    return candidate_set.model_copy(update={"candidates": narrowed, "empty_reason": None})


def _option_first_column_position(
    option: Dict[str, Any],
    positions: Dict[str, int],
) -> Optional[int]:
    hits = [positions[token] for token in _column_name_tokens(option) if token in positions]
    return min(hits) if hits else None


def _rank_target_sequences(
    sequences: Sequence[tuple[Dict[str, Any], ...]],
    *,
    source_refs: Sequence[tuple[Optional[str], Optional[str]]],
    option_rank: Dict[str, int],
    question_positions: Dict[str, int],
) -> List[tuple[Dict[str, Any], ...]]:
    def _option_key(option: Dict[str, Any]) -> str:
        return f"{option.get('target_table')}::{option.get('target_column')}"

    def _rank(sequence: tuple[Dict[str, Any], ...]) -> tuple[int, int, int, int, str]:
        no_op_positions = sum(
            1
            for idx, option in enumerate(sequence)
            if idx < len(source_refs) and _same_source_target(source_refs[idx], option)
        )
        text_positions = [
            _option_first_column_position(option, question_positions)
            for option in sequence
        ]
        order_inversions = 0
        for left_idx, left_pos in enumerate(text_positions):
            if left_pos is None:
                continue
            for right_pos in text_positions[left_idx + 1 :]:
                if right_pos is not None and left_pos > right_pos:
                    order_inversions += 1
        known_positions = [pos for pos in text_positions if pos is not None]
        duplicate_position_penalty = len(known_positions) - len(set(known_positions))
        rank_sum = sum(option_rank.get(_option_key(option), 10_000) for option in sequence)
        lexical = "|".join(
            f"{option.get('target_table')}.{option.get('target_column')}" for option in sequence
        ).lower()
        return (no_op_positions, order_inversions, duplicate_position_penalty, rank_sum, lexical)

    return sorted(sequences, key=_rank)


def _shape_delta_payload(skeleton: RepairSkeletonStructural) -> Dict[str, Any]:
    shape = getattr(skeleton, "output_shape_delta", None)
    payload = shape.model_dump(mode="json") if hasattr(shape, "model_dump") else dict(shape or {})
    current = payload.get("current_arity")
    target = payload.get("target_arity")
    try:
        if current is not None and target is not None:
            delta = int(target) - int(current)
            payload["arity_delta"] = delta
            payload["arity_direction"] = (
                "increase" if delta > 0 else "decrease" if delta < 0 else "same"
            )
    except Exception:
        payload.setdefault("arity_direction", "unknown")
    return payload


def _select_slot_delta(
    skeleton: RepairSkeletonStructural,
    *,
    expected_direction: str,
) -> tuple[int, Dict[str, Any]]:
    """Return how many SELECT slots this action should add/drop.

    The count comes from structured output shape delta, not legacy labels. If
    the delta is unavailable or points in another direction, fall back to the
    old single-slot primitive behavior.
    """
    shape = _shape_delta_payload(skeleton)
    direction = str(shape.get("arity_direction") or "").lower()
    if direction != expected_direction:
        return 1, shape
    try:
        delta = abs(int(shape.get("arity_delta") or 0))
    except Exception:
        delta = 0
    return max(1, delta), shape


def _split_or_sides(predicate: str) -> List[str]:
    """Split a predicate into top-level OR alternatives.

    This is deliberately structural rather than schema-specific. It does not
    decide which side is right; it only exposes alternatives for compiler
    selection.
    """
    text = str(predicate or "").strip()
    if not text:
        return []

    def _strip_outer_parens(value: str) -> str:
        value = value.strip()
        while value.startswith("(") and value.endswith(")"):
            depth = 0
            balanced_outer = True
            quote: Optional[str] = None
            for pos, ch in enumerate(value):
                if quote:
                    if ch == quote:
                        if pos + 1 < len(value) and value[pos + 1] == quote:
                            continue
                        quote = None
                    continue
                if ch in {"'", '"', "`"}:
                    quote = ch
                    continue
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0 and pos != len(value) - 1:
                        balanced_outer = False
                        break
                    if depth < 0:
                        balanced_outer = False
                        break
            if not balanced_outer or depth != 0:
                break
            value = value[1:-1].strip()
        return value

    parts: List[str] = []
    start = 0
    depth = 0
    quote: Optional[str] = None
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if quote:
            if ch == quote:
                # SQL string escaping can repeat the quote char.
                if idx + 1 < len(text) and text[idx + 1] == quote:
                    idx += 2
                    continue
                quote = None
            idx += 1
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            idx += 1
            continue
        if ch == "(":
            depth += 1
            idx += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            idx += 1
            continue
        if (
            depth == 0
            and text[idx : idx + 2].lower() == "or"
            and (idx == 0 or not (text[idx - 1].isalnum() or text[idx - 1] == "_"))
            and (idx + 2 >= len(text) or not (text[idx + 2].isalnum() or text[idx + 2] == "_"))
        ):
            parts.append(_strip_outer_parens(text[start:idx]))
            idx += 2
            start = idx
            continue
        idx += 1
    parts.append(_strip_outer_parens(text[start:]))
    return [part for part in parts if part]


def _condition_aliases(condition: str) -> set[str]:
    return set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\.", str(condition or "")))


def _edge_payload(edge: Any) -> Dict[str, Any]:
    if isinstance(edge, dict):
        return dict(edge)
    return {"edge": str(edge)}


def _related_join_edges(ast: Dict[str, Any], condition: str) -> List[Dict[str, Any]]:
    aliases = _condition_aliases(condition)
    if not aliases:
        return []
    out: List[Dict[str, Any]] = []
    for edge in ast.get("join_edges") or []:
        text = " ".join(str(value) for value in _edge_payload(edge).values())
        if any(re.search(rf"\b{re.escape(alias)}\s*\.", text) for alias in aliases):
            out.append(_edge_payload(edge))
    return out


def _payload(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return dict(obj)
    return dict(getattr(obj, "__dict__", {}) or {})


def _repair_program_from_group(group: GroupSummary) -> List[Dict[str, Any]]:
    contract = _payload(getattr(group, "trigger_contract", None))
    action_contract = _payload(contract.get("action_contract"))
    steps = action_contract.get("repair_program") or []
    out: List[Dict[str, Any]] = []
    for step in steps or []:
        payload = _payload(step)
        if not payload:
            continue
        origin = str(payload.get("origin") or "")
        source = str(payload.get("extraction_source") or "")
        if origin not in {"case_extracted", "group_merged"}:
            continue
        if source not in {"llm_explicit", "group_merged"}:
            continue
        if not str(payload.get("op") or "").strip():
            continue
        out.append(payload)
    return out


def _synthesized_program_from_group(group: GroupSummary) -> Dict[str, Any]:
    instantiation = _payload(getattr(group, "instantiation_program", None))
    program = _payload(instantiation.get("synthesized_program"))
    if program:
        return program
    contract = _payload(getattr(group, "trigger_contract", None))
    action_contract = _payload(contract.get("action_contract"))
    return _payload(action_contract.get("synthesized_program"))


def _canonical_ops_from_group(group: GroupSummary) -> List[Dict[str, Any]]:
    program = _synthesized_program_from_group(group)
    ops = program.get("ops") or []
    return [dict(op) for op in ops if isinstance(op, dict)]


def _canonical_shape(op: Dict[str, Any]) -> Dict[str, Any]:
    args = _payload(op.get("arguments"))
    shape = _payload(args.get("output_shape_delta"))
    if shape:
        return shape
    shared = _payload(args.get("shared_arguments"))
    shape = _payload(shared.get("output_shape_delta"))
    if shape:
        return shape
    for variant in args.get("member_argument_variants") or []:
        payload = _payload(variant)
        shape = _payload(payload.get("output_shape_delta"))
        if shape:
            return shape
    skeleton = _payload(args.get("skeleton"))
    shape = _payload(skeleton.get("output_shape_delta"))
    if shape:
        return shape
    return {}


def _canonical_role_delta(op: Dict[str, Any]) -> Dict[str, Any]:
    args = _payload(op.get("arguments"))
    signature = _payload(args.get("operation_signature"))
    role_delta = _payload(signature.get("role_delta"))
    if role_delta:
        return role_delta
    shared_signature = _payload(args.get("shared_signature"))
    role_delta = _payload(shared_signature.get("generalized_role_delta"))
    if role_delta:
        return role_delta
    for variant in args.get("member_argument_variants") or []:
        payload = _payload(variant)
        signature = _payload(payload.get("operation_signature"))
        role_delta = _payload(signature.get("role_delta"))
        if role_delta:
            return role_delta
    return {}


def _canonical_signature_section(op: Dict[str, Any], key: str) -> Dict[str, Any]:
    args = _payload(op.get("arguments"))
    signature = _payload(args.get("operation_signature"))
    section = _payload(signature.get(key))
    if section:
        return section
    shared_signature = _payload(args.get("shared_signature"))
    section = _payload(shared_signature.get(key))
    if section:
        return section
    for variant in args.get("member_argument_variants") or []:
        payload = _payload(variant)
        variant_signature = _payload(payload.get("operation_signature"))
        section = _payload(variant_signature.get(key))
        if section:
            return section
    return {}


def _canonical_target_output_subset(op: Dict[str, Any]) -> bool:
    role_delta = _canonical_role_delta(op)
    value = role_delta.get("target_output_subset_of_source")
    if isinstance(value, bool):
        return value
    if str(value).lower() == "true":
        return True
    invariants = {str(item) for item in (op.get("invariants") or [])}
    if "target_output_subset_of_source_outputs" in invariants:
        return True
    args = _payload(op.get("arguments"))
    shared_signature = _payload(args.get("shared_signature"))
    common = {str(item) for item in (shared_signature.get("common_invariants") or [])}
    return "target_output_subset_of_source_outputs" in common


def _effective_lowering_family(op: Dict[str, Any]) -> str:
    """Return the executable lowering family implied by the canonical contract."""
    lowering = canonical_op_lowering_family(
        str(op.get("op_type") or ""),
        str(op.get("locus") or "").upper(),
    )
    shape = _canonical_shape(op)
    if (
        lowering == "select_replace"
        and str(shape.get("arity_direction") or "").lower() == "decrease"
        and _canonical_target_output_subset(op)
    ):
        return "select_drop"
    if (
        lowering == "select_replace"
        and str(shape.get("arity_direction") or "").lower() == "increase"
    ):
        return "select_add"
    return lowering


def _lowering_family_from_shape(
    *,
    canonical_op: Dict[str, Any],
    shape: Dict[str, Any],
) -> str:
    base = _effective_lowering_family(canonical_op)
    if base not in {"select_output_patch", "select_replace"}:
        return base
    direction = str(shape.get("arity_direction") or "").lower()
    if direction == "increase":
        return "select_add"
    if direction == "decrease" and _canonical_target_output_subset(canonical_op):
        return "select_drop"
    return "select_replace"


def _lowering_family_for_current(
    *,
    canonical_op: Dict[str, Any],
    matched_variants: Sequence[Dict[str, Any]],
) -> str:
    variant_shape = _shared_variant_output_shape_delta(matched_variants)
    if variant_shape:
        return _lowering_family_from_shape(
            canonical_op=canonical_op,
            shape=variant_shape,
        )
    return _effective_lowering_family(canonical_op)


def _primitive_for_lowering_family(lowering_family: str) -> ActionPrimitive:
    if lowering_family == "select_drop":
        return ActionPrimitive.DROP_SELECT_SLOT
    if lowering_family == "where_side_edit":
        return ActionPrimitive.DROP_SIDE
    if lowering_family == "join_bridge":
        return ActionPrimitive.INSERT_BRIDGE
    if lowering_family in {"join_reroute", "fact_route_reroute"}:
        return ActionPrimitive.REROUTE_FACT
    if lowering_family == "select_add":
        return ActionPrimitive.ADD_SELECT_SLOT
    if lowering_family == "select_output_patch":
        return ActionPrimitive.REPLACE_SELECT_SLOT
    if lowering_family == "select_replace":
        return ActionPrimitive.REPLACE_SELECT_SLOT
    return ActionPrimitive.ADD_SELECT_SLOT


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _canonical_member_variants(canonical_op: Dict[str, Any]) -> List[Dict[str, Any]]:
    args = _payload(canonical_op.get("arguments"))
    return [
        payload
        for variant in (args.get("member_argument_variants") or [])
        if (payload := _payload(variant))
    ]


def _infer_output_roles_from_current_projection(case_view: RuntimeCaseView) -> List[str]:
    view = case_view.local_schema_view
    alias_map = _sql_alias_map(getattr(case_view.pred_manifestation, "top1_sql", "") or "")
    roles: List[str] = []
    for expr in getattr(case_view.pred_manifestation, "columns", None) or []:
        table, column = _expr_table_column(expr, alias_map)
        role: Optional[str] = None
        if table and column:
            role = _get_column_role(table, column, view)
        if role is None and column:
            matches = [
                (tbl, col)
                for tbl, cols in view.columns_by_table.items()
                for col in cols
                if col.lower() == column.lower()
            ]
            if len(matches) == 1:
                role = _get_column_role(matches[0][0], matches[0][1], view)
        roles.append(str(role or "other"))
    return roles


def _current_output_shape_for_compiler(case_view: RuntimeCaseView) -> Dict[str, Any]:
    signal_view = getattr(case_view, "case_signal_view", None)
    pred_sql_view = _payload(getattr(signal_view, "pred_sql_view", None))
    shape = _payload(pred_sql_view.get("output_shape_current"))
    aggregate_profile = _payload(pred_sql_view.get("aggregate_profile"))

    out: Dict[str, Any] = {}
    arity = shape.get("arity") or pred_sql_view.get("select_arity")
    if arity is None:
        arity = len(getattr(case_view.pred_manifestation, "columns", None) or [])
    if arity is not None:
        try:
            out["arity"] = int(arity)
        except Exception:
            pass

    if shape.get("grain"):
        out["grain"] = shape.get("grain")
    roles = list(shape.get("roles") or pred_sql_view.get("select_role_profile") or [])
    inferred_roles = _infer_output_roles_from_current_projection(case_view)
    if roles and inferred_roles and len(roles) == len(inferred_roles):
        roles = [
            inferred
            if _normalized_output_role(role) == "other"
            and _normalized_output_role(inferred) != "other"
            else role
            for role, inferred in zip(roles, inferred_roles)
        ]
    if inferred_roles and (
        not roles
        or all(_normalized_output_role(role) == "other" for role in roles)
    ):
        roles = inferred_roles
    if roles:
        out["roles"] = [str(role) for role in roles if str(role)]

    select_shape = str(getattr(case_view.pred_manifestation, "select_shape", "") or "")
    sql = str(getattr(case_view.pred_manifestation, "top1_sql", "") or "")
    out["has_aggregate"] = bool(
        shape.get("has_aggregate")
        or aggregate_profile.get("has_aggregate")
        or "aggregate" in select_shape.lower()
        or re.search(r"\b(count|sum|avg|min|max)\s*\(", sql, flags=re.IGNORECASE)
    )
    out["has_distinct"] = bool(
        shape.get("has_distinct")
        or aggregate_profile.get("has_distinct_aggregate")
        or "distinct" in select_shape.lower()
        or re.search(r"\bselect\s+distinct\b|\bcount\s*\(\s*distinct\b", sql, flags=re.IGNORECASE)
    )
    return out


def _variant_source_output_shape(variant: Dict[str, Any]) -> Dict[str, Any]:
    shape = _payload(variant.get("source_output_shape"))
    delta = _payload(variant.get("output_shape_delta"))
    if not shape and delta:
        shape = {}
    if delta:
        if "arity" not in shape and delta.get("current_arity") is not None:
            shape["arity"] = delta.get("current_arity")
        if "grain" not in shape and delta.get("current_grain") is not None:
            shape["grain"] = delta.get("current_grain")
    if "roles" not in shape and variant.get("source_output_roles"):
        shape["roles"] = list(variant.get("source_output_roles") or [])
    return shape


def _normalized_output_role(role: Any) -> str:
    value = str(role or "").strip().lower()
    if value in {"identifier_like", "identifier", "code"}:
        return "identifier"
    if value in {"name", "label", "title"}:
        return "name"
    if value in {"column_expr", "other"}:
        return "other"
    return value


def _expr_is_identifier_like(expr: str) -> bool:
    text = str(expr or "").strip().strip('"`[]').lower()
    if not text or "(" in text:
        return False
    column = text.rsplit(".", 1)[-1].strip('"`[]')
    return column == "id" or column.endswith("_id") or column.endswith("id") or "uuid" in column


def _output_roles_compatible(source_roles: Sequence[Any], current_roles: Sequence[Any]) -> bool:
    source = [_normalized_output_role(role) for role in source_roles if str(role)]
    current = [_normalized_output_role(role) for role in current_roles if str(role)]
    if not source or not current:
        return True
    if len(source) != len(current):
        return False
    return sorted(source) == sorted(current)


def _variant_matches_current_output_shape(
    variant: Dict[str, Any],
    current_shape: Dict[str, Any],
) -> bool:
    source_shape = _variant_source_output_shape(variant)
    checks = 0

    if source_shape.get("arity") is not None and current_shape.get("arity") is not None:
        checks += 1
        if _int_or_zero(source_shape.get("arity")) != _int_or_zero(current_shape.get("arity")):
            return False

    if source_shape.get("grain") and current_shape.get("grain"):
        checks += 1
        if str(source_shape.get("grain")) != str(current_shape.get("grain")):
            return False

    if source_shape.get("roles") and current_shape.get("roles"):
        checks += 1
        if not _output_roles_compatible(
            source_shape.get("roles") or [],
            current_shape.get("roles") or [],
        ):
            return False

    return checks > 0


def _canonical_unresolved_axes(canonical_op: Dict[str, Any]) -> List[str]:
    args = _payload(canonical_op.get("arguments"))
    shared = _payload(args.get("shared_arguments"))
    signature = _payload(args.get("shared_signature"))
    axes = list(shared.get("unresolved_variation_axes") or [])
    axes.extend(signature.get("unresolved_variation_axes") or [])
    return [str(axis) for axis in axes if str(axis)]


def _requires_current_variant_binding(canonical_op: Dict[str, Any]) -> bool:
    return any(
        axis.startswith("output_shape.") or axis == "slot_signature"
        for axis in _canonical_unresolved_axes(canonical_op)
    )


def _matching_member_variants_for_current(
    *,
    canonical_op: Dict[str, Any],
    case_view: RuntimeCaseView,
) -> List[Dict[str, Any]]:
    variants = _canonical_member_variants(canonical_op)
    if not variants:
        return []
    current_shape = _current_output_shape_for_compiler(case_view)
    return [
        variant
        for variant in variants
        if _variant_matches_current_output_shape(variant, current_shape)
    ]


def _shared_variant_output_shape_delta(
    variants: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    shapes: List[Dict[str, Any]] = [
        _payload(variant.get("output_shape_delta"))
        for variant in variants
        if _payload(variant.get("output_shape_delta"))
    ]
    if not shapes:
        return {}
    first = shapes[0]
    if all(json.dumps(shape, sort_keys=True, default=str) == json.dumps(first, sort_keys=True, default=str) for shape in shapes):
        return dict(first)
    return {}


def _program_id_from_group(group: GroupSummary) -> Optional[str]:
    program = _synthesized_program_from_group(group)
    return str(program.get("program_id") or "") or None


def _canonical_refs_for_action(canonical_op: Dict[str, Any]) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    for ref in (_payload(canonical_op.get("arguments")).get("canonical_refs") or [])[:8]:
        payload = _payload(ref)
        if not payload:
            continue
        if str(payload.get("source") or "") == "target_sql":
            refs.append(_role_profile_from_target_ref(payload))
        else:
            refs.append(
                {
                    "table": payload.get("table"),
                    "column": payload.get("column"),
                    "expression": payload.get("expression"),
                    "sql_role": payload.get("sql_role"),
                    "column_role": payload.get("column_role"),
                    "path_role": payload.get("path_role"),
                    "relation_role": payload.get("relation_role"),
                }
            )
    return refs


def _meaningful_role_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower() in {"unknown", "unknown_table", "table_node", "other", "none", "null"}:
        return ""
    return text


def _role_profile_from_target_ref(ref: Dict[str, Any]) -> Dict[str, Any]:
    """Executable role profile for a target output ref.

    Legacy role refs can still carry table/column/expression for audit, but
    runtime candidate binding must not execute those seed literals directly.
    """
    evidence = dict(_payload(ref.get("evidence")))
    for source_key, evidence_key in (
        ("table", "original_table"),
        ("column", "original_column"),
        ("expression", "original_expression"),
    ):
        value = ref.get(source_key)
        if value and evidence_key not in evidence:
            evidence[evidence_key] = value
    return {
        "slot_index": ref.get("slot_index"),
        "sql_role": ref.get("sql_role"),
        "column_role": _meaningful_role_value(ref.get("column_role")),
        "path_role": _meaningful_role_value(ref.get("path_role")),
        "direct_role_path": _meaningful_role_value(ref.get("direct_role_path")),
        "derived_role_path": _meaningful_role_value(ref.get("derived_role_path")),
        "role_side_group": _meaningful_role_value(ref.get("role_side_group")),
        "side_key": _meaningful_role_value(ref.get("side_key")),
        "relation_role": _meaningful_role_value(ref.get("relation_role")),
        "evidence": evidence,
    }


def _role_profile_has_binding_signal(ref: Dict[str, Any]) -> bool:
    return any(
        _meaningful_role_value(ref.get(key))
        for key in (
            "column_role",
            "path_role",
            "direct_role_path",
            "derived_role_path",
            "role_side_group",
            "side_key",
            "relation_role",
        )
    )


def _option_matches_role_profile(option: Dict[str, Any], ref: Dict[str, Any]) -> bool:
    expected_role = _meaningful_role_value(ref.get("column_role")).lower()
    option_role = _meaningful_role_value(option.get("target_role_family")).lower()
    if expected_role and option_role != expected_role:
        return False
    return True


def _runtime_binding_contract_for_action(canonical_op: Dict[str, Any]) -> Dict[str, Any]:
    """Return the future WUv2-5 binding contract, if present.

    WUv2-2 deliberately refuses to execute seed target refs without this
    contract. Seed target role refs are evidence, not executable binding.
    """
    args = _payload(canonical_op.get("arguments"))
    for key in ("binding_contract", "runtime_binding_contract"):
        payload = _payload(args.get(key) or canonical_op.get(key))
        if payload:
            return payload
    return {}


def _target_output_refs_for_action(
    canonical_op: Dict[str, Any],
    *,
    member_variants: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    if not _runtime_binding_contract_for_action(canonical_op):
        return []

    refs: List[Dict[str, Any]] = []

    def add_ref(ref: Any) -> None:
        payload = _payload(ref)
        if not payload:
            return
        if str(payload.get("source") or "") != "target_sql":
            return
        if str(payload.get("sql_role") or "") != "output_slot":
            return
        profile = _role_profile_from_target_ref(payload)
        if not _role_profile_has_binding_signal(profile):
            return
        refs.append(profile)

    args = _payload(canonical_op.get("arguments"))
    variants = list(member_variants or [])
    if variants:
        for variant in variants:
            for ref in _payload(variant).get("target_output_refs") or []:
                add_ref(ref)
    else:
        for ref in canonical_op.get("role_refs") or []:
            add_ref(ref)
        for ref in args.get("canonical_refs") or []:
            add_ref(ref)
        variants = list(args.get("member_argument_variants") or [])
    for variant in variants:
        for ref in _payload(variant).get("target_output_refs") or []:
            add_ref(ref)

    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for ref in refs:
        key = json.dumps(ref, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out[:12]


def _target_ref_keys(refs: Sequence[Dict[str, Any]]) -> tuple[set[tuple[str, str]], set[str]]:
    pairs: set[tuple[str, str]] = set()
    columns: set[str] = set()
    for ref in refs or []:
        role = _meaningful_role_value(ref.get("column_role")).lower()
        path = _meaningful_role_value(ref.get("path_role")).lower()
        if role:
            columns.add(f"role:{role}")
        if path:
            pairs.add(("path", path))
    return pairs, columns


def _output_ref_keys_from_exprs(case_view: RuntimeCaseView) -> List[tuple[str, str]]:
    alias_map = _sql_alias_map(getattr(case_view.pred_manifestation, "top1_sql", "") or "")
    keys: List[tuple[str, str]] = []
    for expr in getattr(case_view.pred_manifestation, "columns", None) or []:
        table, column = _expr_table_column(expr, alias_map)
        keys.append((str(table or "").lower(), str(column or "").lower()))
    return keys


def _output_ref_keys_from_refs(refs: Sequence[Dict[str, Any]]) -> List[tuple[str, str]]:
    keys: List[tuple[str, str]] = []
    for ref in _ordered_output_refs(refs):
        keys.append(
            (
                str(ref.get("role_side_group") or ref.get("path_role") or "").strip().lower(),
                str(ref.get("column_role") or "").strip().lower(),
            )
        )
    return keys


def _output_ref_sequence_compatible(
    expected: Sequence[tuple[str, str]],
    current: Sequence[tuple[str, str]],
) -> bool:
    if not expected or not current:
        return True
    if len(expected) != len(current):
        return False
    for (exp_table, exp_col), (cur_table, cur_col) in zip(expected, current):
        if not exp_col or not cur_col:
            return True
        if exp_table and cur_table and exp_table == cur_table and exp_col == cur_col:
            continue
        if exp_col == cur_col:
            continue
        return False
    return True


def _output_key_matches_ref(
    current: tuple[str, str],
    ref: Dict[str, Any],
) -> bool:
    cur_table, cur_col = current
    ref_table = str(ref.get("role_side_group") or ref.get("path_role") or "").strip().lower()
    ref_col = str(ref.get("column_role") or "").strip().lower()
    if not cur_col or not ref_col:
        return False
    return cur_col == ref_col or bool(ref_table and cur_table == ref_table)


def _int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(value)
    except Exception:
        return None


def _partition_target_output_refs_by_current_projection(
    *,
    case_view: RuntimeCaseView,
    target_refs: Sequence[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split target projection refs into same-slot replacements and added slots.

    This is derived only from current SQL output slots and target SQL refs
    carried by memory. It intentionally does not encode database- or case-level
    repair rules.
    """
    current_keys = _output_ref_keys_from_exprs(case_view)
    current_arity = len(current_keys)
    replacement_refs: List[Dict[str, Any]] = []
    add_refs: List[Dict[str, Any]] = []

    for ref in _ordered_output_refs(target_refs):
        slot_index = _int_or_none(ref.get("slot_index"))
        if slot_index is not None and 0 <= slot_index < current_arity:
            if not _output_key_matches_ref(current_keys[slot_index], ref):
                replacement_refs.append(ref)
            continue
        if slot_index is not None and slot_index >= current_arity:
            add_refs.append(ref)
            continue
        if not any(_output_key_matches_ref(current_key, ref) for current_key in current_keys):
            add_refs.append(ref)

    return replacement_refs, add_refs


def _target_columns_supported_by_refs(
    target_columns: Sequence[Dict[str, Any]],
    refs: Sequence[Dict[str, Any]],
) -> bool:
    if not target_columns or not refs:
        return True
    ref_pairs, ref_columns = _target_ref_keys(refs)
    if not ref_pairs and not ref_columns:
        return True
    for column in target_columns:
        role = _meaningful_role_value(column.get("target_role_family")).lower()
        if not role:
            return False
        if f"role:{role}" in ref_columns:
            continue
        return False
    return True


def _target_column_matches_ref(column: Dict[str, Any], ref: Dict[str, Any]) -> bool:
    return _option_matches_role_profile(column, ref)


def _scoped_target_output_refs_for_candidate(
    *,
    primitive: ActionPrimitive,
    args: Dict[str, Any],
    target_output_refs: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if primitive not in {
        ActionPrimitive.ADD_SELECT_SLOT,
        ActionPrimitive.REPLACE_SELECT_SLOT,
    }:
        return [dict(ref) for ref in target_output_refs]
    target_columns = [_payload(item) for item in (args.get("target_columns") or [])]
    if not target_columns:
        return [dict(ref) for ref in target_output_refs]

    candidate_slots = {
        index
        for column in target_columns
        if (index := _int_or_none(column.get("target_slot_index"))) is not None
    }
    if primitive == ActionPrimitive.REPLACE_SELECT_SLOT:
        candidate_slots.update(
            index
            for value in (args.get("source_slot_indexes") or [])
            if (index := _int_or_none(value)) is not None
        )

    scoped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for ref in _ordered_output_refs(target_output_refs):
        ref_slot = _int_or_none(ref.get("slot_index"))
        if candidate_slots and ref_slot is not None and ref_slot not in candidate_slots:
            continue
        if not any(_target_column_matches_ref(column, ref) for column in target_columns):
            continue
        key = json.dumps(ref, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        scoped.append(dict(ref))
    return scoped


def _ordered_output_refs(refs: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = [_payload(ref) for ref in refs or [] if _payload(ref)]
    return sorted(
        rows,
        key=lambda ref: (
            1 if ref.get("slot_index") is None else 0,
            int(ref.get("slot_index") or 0),
            str((ref.get("evidence") or {}).get("original_expression") or ""),
            str(ref.get("column_role") or ""),
        ),
    )


def _schema_column_option_from_ref(
    ref: Dict[str, Any],
    view: LocalSchemaView,
) -> Optional[Dict[str, Any]]:
    expected_role = _meaningful_role_value(ref.get("column_role"))
    if not expected_role:
        return None
    options: List[Dict[str, Any]] = []
    for table, cols in view.columns_by_table.items():
        for column in cols:
            role = _get_column_role(table, column, view)
            option = {
                "target_table": table,
                "target_column": column,
                "target_role_family": role,
                "target_role_profile": dict(ref),
                "target_slot_index": ref.get("slot_index"),
            }
            if _option_matches_role_profile(option, ref):
                options.append(option)
    ranked = sorted(
        options,
        key=lambda option: (
            str(option.get("target_table") or "").lower(),
            str(option.get("target_column") or "").lower(),
        ),
    )
    return ranked[0] if ranked else None


def _schema_column_options_from_refs(
    refs: Sequence[Dict[str, Any]],
    view: LocalSchemaView,
) -> List[Dict[str, Any]]:
    options: List[Dict[str, Any]] = []
    for ref in _ordered_output_refs(refs):
        option = _schema_column_option_from_ref(ref, view)
        if option is None:
            return []
        options.append(option)
    return options


def _target_relation_edges_for_action(
    canonical_op: Dict[str, Any],
    *,
    member_variants: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    args = _payload(canonical_op.get("arguments"))
    shared = _payload(args.get("shared_arguments"))
    rows: List[Dict[str, Any]] = []
    for relation in args.get("target_equality_relations") or []:
        payload = _payload(relation)
        if payload:
            rows.append(payload)
    for relation in shared.get("target_equality_relations") or []:
        payload = _payload(relation)
        if payload:
            rows.append(payload)
    variants = list(member_variants or [])
    if not variants:
        variants = list(args.get("member_argument_variants") or [])
    for variant in variants:
        for relation in _payload(variant).get("target_equality_relations") or []:
            payload = _payload(relation)
            if payload:
                rows.append(payload)
    relation_delta = _payload(_payload(args.get("operation_signature")).get("relation_delta"))
    relation_effect = _payload(_payload(args.get("repair_effect_signature")).get("relation_effect"))
    for expr in [
        *list(relation_delta.get("target_relation_equalities") or []),
        *list(relation_delta.get("added_relation_equalities") or []),
        *list(relation_effect.get("target_relation_paths") or []),
        *list(relation_effect.get("added_relation_equalities") or []),
    ]:
        edge = _relation_edge_from_equality(str(expr or ""))
        if edge:
            rows.append(edge)
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for relation in rows:
        key = str(relation.get("canonical_key") or relation.get("expression") or "")
        if not key:
            key = json.dumps(relation, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(relation)
    return out[:8]


def _relation_edge_from_equality(expr: str) -> Optional[Dict[str, Any]]:
    text = str(expr or "").strip()
    if not text:
        return None
    match = re.fullmatch(
        r"\s*([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*",
        text,
    )
    if not match:
        return None
    left_table, left_column, right_table, right_column = match.groups()
    left = {"table": left_table, "column": left_column}
    right = {"table": right_table, "column": right_column}
    canonical_key = "=".join(
        sorted(
            [
                f"{left_table.lower()}.{left_column.lower()}",
                f"{right_table.lower()}.{right_column.lower()}",
            ]
        )
    )
    return {
        "left": left,
        "right": right,
        "canonical_key": canonical_key,
        "expression": f"{left_table}.{left_column} = {right_table}.{right_column}",
    }


def _program_bundles_from_group(group: GroupSummary) -> List[Dict[str, Any]]:
    program = getattr(group.instantiation_program, "synthesized_program", None)
    if program is None:
        return []
    envelope = _payload(getattr(program, "program_envelope", None))
    action_envelope = _payload(envelope.get("action_envelope"))
    return [
        _payload(bundle)
        for bundle in (action_envelope.get("bundles") or [])
        if _payload(bundle)
    ]


def _bundle_metadata_for_canonical_op(
    *,
    group: GroupSummary,
    canonical_op: Dict[str, Any],
) -> Dict[str, Any]:
    op_id = str(canonical_op.get("op_id") or "")
    for bundle in _program_bundles_from_group(group):
        op_ids = {
            str(item)
            for item in (bundle.get("bundled_op_ids") or [])
            if str(item)
        }
        cleanup_ids = {
            str(item)
            for item in (bundle.get("cleanup_op_ids") or [])
            if str(item)
        }
        if op_id and (op_id in op_ids or op_id in cleanup_ids):
            return bundle
    return {
        "bundle_id": op_id,
        "effect_kind": "",
        "primary_primitive": "",
        "cleanup_op_ids": [],
        "counts_as_action": 1,
    }


def _bundle_selection_key(arguments: Dict[str, Any]) -> str:
    raw_indexes = arguments.get("side_indexes")
    if raw_indexes is None:
        raw_indexes = arguments.get("source_slot_indexes")
    if raw_indexes is None:
        indexes: List[Any] = []
    elif isinstance(raw_indexes, list):
        indexes = list(raw_indexes)
    else:
        indexes = [raw_indexes]
    if arguments.get("side_index") is not None:
        indexes.append(arguments.get("side_index"))
    normalized_indexes: List[str] = []
    for value in indexes:
        try:
            normalized_indexes.append(str(int(value)))
        except Exception:
            continue
    if normalized_indexes:
        return "side:" + ",".join(sorted(set(normalized_indexes)))
    exprs = list(arguments.get("from_exprs") or [])
    if arguments.get("from_expr") is not None:
        exprs.append(arguments.get("from_expr"))
    if exprs:
        return "expr:" + "|".join(sorted(str(expr).strip().lower() for expr in exprs if str(expr)))
    target_columns = []
    for row in arguments.get("target_columns") or []:
        payload = _payload(row)
        target_columns.append(
            f"{str(payload.get('target_table') or '').lower()}.{str(payload.get('target_column') or '').lower()}"
        )
    if target_columns:
        return "cols:" + "|".join(sorted(target_columns))
    relation_keys = [
        str(_payload(edge).get("canonical_key") or "")
        for edge in (arguments.get("target_relation_edges") or [])
        if str(_payload(edge).get("canonical_key") or "")
    ]
    if relation_keys:
        return "rel:" + "|".join(sorted(relation_keys))
    return ""


def _answer_unit_preserve_for_reroute(shape: Dict[str, Any]) -> bool:
    if not shape:
        return True
    arity_delta = shape.get("arity_delta")
    if arity_delta not in (None, "", 0, "0"):
        return False
    current_arity = shape.get("current_arity")
    target_arity = shape.get("target_arity")
    if current_arity is not None and target_arity is not None and str(current_arity) != str(target_arity):
        return False
    current_grain = str(shape.get("current_grain") or "")
    target_grain = str(shape.get("target_grain") or "")
    if current_grain and target_grain and current_grain != target_grain:
        return False
    return True


def _canonical_contract_for_action(
    *,
    program_id: Optional[str],
    canonical_op: Dict[str, Any],
    member_variants: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Compact executable canonical contract attached to concrete candidates.

    Full synthesized ops can contain member-level variants and source-case
    evidence. Runtime only needs the op identity, lowering family, shared shape,
    role refs, and invariants; selected candidates keep concrete bindings in
    their own action arguments.
    """
    args = _payload(canonical_op.get("arguments"))
    shared_signature = _payload(args.get("shared_signature"))
    shared_arguments = _payload(args.get("shared_arguments"))
    return {
        "program_id": program_id,
        "op_id": str(canonical_op.get("op_id") or ""),
        "op_type": str(canonical_op.get("op_type") or ""),
        "locus": str(canonical_op.get("locus") or ""),
        "lowering_family": _effective_lowering_family(canonical_op),
        "repair_effect_signature": _payload(args.get("repair_effect_signature")),
        "output_shape_delta": _shared_variant_output_shape_delta(member_variants or [])
        or _canonical_shape(canonical_op),
        "canonical_refs": _canonical_refs_for_action(canonical_op),
        "invariants": list(canonical_op.get("invariants") or [])[:20],
        "target_invariants": list(shared_arguments.get("target_invariants") or [])[:20],
        "target_output_refs": _target_output_refs_for_action(
            canonical_op,
            member_variants=member_variants,
        ),
        "target_relation_edges": _target_relation_edges_for_action(
            canonical_op,
            member_variants=member_variants,
        ),
        "unresolved_variation_axes": list(
            shared_arguments.get("unresolved_variation_axes") or []
        ),
        "common_invariants": list(shared_signature.get("common_invariants") or [])[:20],
    }


def _effect_contract_for_action(
    *,
    group: GroupSummary,
    canonical_op: Dict[str, Any],
) -> Dict[str, Any]:
    args = _payload(canonical_op.get("arguments"))
    insight = _payload(args.get("repair_insight_signature"))
    if not insight:
        program = _synthesized_program_from_group(group)
        insight = _payload(program.get("repair_insight_signature"))
    if not insight:
        contract = _payload(getattr(group, "trigger_contract", None))
        action_contract = _payload(contract.get("action_contract"))
        insight = _payload(action_contract.get("repair_insight_signature"))
    if not insight:
        return {}
    return {
        "interface_key": insight.get("interface_key"),
        "source_misread": insight.get("source_misread"),
        "target_preference": insight.get("target_preference"),
        "repair_interface": insight.get("repair_interface"),
        "binding_slots": list(insight.get("binding_slots") or [])[:12],
        "preserve_invariants": list(insight.get("preserve_invariants") or [])[:12],
        "negative_guards": list(insight.get("negative_guards") or [])[:12],
        "axis_links": list(insight.get("axis_links") or [])[:12],
    }


def _lowered_skeleton_for_canonical_op(
    *,
    skeleton: RepairSkeletonStructural,
    op: Dict[str, Any],
) -> RepairSkeletonStructural:
    lowering_family = _effective_lowering_family(op)
    return _lowered_skeleton_for_family(
        skeleton=skeleton,
        lowering_family=lowering_family,
    )


def _lowered_skeleton_for_family(
    *,
    skeleton: RepairSkeletonStructural,
    lowering_family: str,
) -> RepairSkeletonStructural:
    if lowering_family == "select_drop":
        return skeleton.model_copy(
            update={
                "locus": Locus.SELECT,
                "op_family": OpFamily.DROP,
                "target_family": TargetFamily.SLOT,
            }
        )
    if lowering_family == "select_add":
        return skeleton.model_copy(
            update={
                "locus": Locus.SELECT,
                "op_family": OpFamily.ADD,
                "target_family": TargetFamily.SLOT,
            }
        )
    if lowering_family == "select_replace":
        return skeleton.model_copy(
            update={
                "locus": Locus.SELECT,
                "op_family": OpFamily.REPLACE,
                "target_family": TargetFamily.SLOT,
            }
        )
    if lowering_family == "select_output_patch":
        return skeleton.model_copy(
            update={
                "locus": Locus.SELECT,
                "op_family": OpFamily.REPLACE,
                "target_family": TargetFamily.SLOT,
            }
        )
    if lowering_family == "where_side_edit":
        return skeleton.model_copy(
            update={
                "locus": Locus.SCOPE,
                "op_family": OpFamily.NARROW,
                "target_family": TargetFamily.CONDITION,
            }
        )
    if lowering_family == "join_bridge":
        return skeleton.model_copy(
            update={
                "locus": Locus.BRIDGE,
                "op_family": OpFamily.BRIDGE,
                "target_family": TargetFamily.BRIDGE_PATH,
            }
        )
    return skeleton


def _canonical_base_skeleton(
    *,
    canonical_op: Dict[str, Any],
    lowering_family: str,
) -> RepairSkeletonStructural:
    """Build compiler skeleton from the canonical op, not a representative case."""
    raw_locus = str(canonical_op.get("locus") or Locus.SELECT.value).upper()
    try:
        locus = Locus(raw_locus)
    except Exception:
        locus = Locus.SELECT
    skeleton = RepairSkeletonStructural(
        locus=locus,
        op_family=OpFamily.REPLACE,
        target_family=TargetFamily.SLOT,
    )
    return _lowered_skeleton_for_family(
        skeleton=skeleton,
        lowering_family=lowering_family,
    )


def _annotate_canonical_candidates(
    *,
    candidate_set: ActionCandidateSet,
    case_view: RuntimeCaseView,
    group: GroupSummary,
    canonical_op: Dict[str, Any],
    member_variants: Optional[Sequence[Dict[str, Any]]] = None,
) -> ActionCandidateSet:
    program_id = _program_id_from_group(group)
    variant_shape = _shared_variant_output_shape_delta(member_variants or [])
    bundle_meta = _bundle_metadata_for_canonical_op(
        group=group,
        canonical_op=canonical_op,
    )
    annotated: List[ActionCandidate] = []
    for candidate in candidate_set.candidates:
        args = dict(candidate.arguments or {})
        scoped_repair_program = _repair_program_for_canonical_op(
            args.get("repair_program") or [],
            canonical_op,
            member_variants=member_variants,
        )
        if "repair_program" in args:
            args["repair_program"] = scoped_repair_program
        args["required_edit_scopes"] = _required_edit_scopes(
            _primary_scope_for_primitive(candidate_set.primitive),
            scoped_repair_program,
        )
        args["compiled_from_program_id"] = program_id
        args["canonical_op_type"] = str(canonical_op.get("op_type") or "")
        args["canonical_op_id"] = str(canonical_op.get("op_id") or "")
        args["canonical_refs"] = _canonical_refs_for_action(canonical_op)
        args["canonical_invariants"] = list(canonical_op.get("invariants") or [])[:20]
        shared_arguments = _payload(_payload(canonical_op.get("arguments")).get("shared_arguments"))
        args["canonical_target_invariants"] = list(
            shared_arguments.get("target_invariants") or []
        )[:20]
        if variant_shape:
            args["output_shape_delta"] = variant_shape
        target_output_refs = _target_output_refs_for_action(
            canonical_op,
            member_variants=member_variants,
        )
        target_relation_edges = _target_relation_edges_for_action(
            canonical_op,
            member_variants=member_variants,
        )
        if candidate_set.primitive in {
            ActionPrimitive.ADD_SELECT_SLOT,
            ActionPrimitive.REPLACE_SELECT_SLOT,
        } and not _target_columns_supported_by_refs(
            args.get("target_columns") or [],
            target_output_refs,
        ):
            continue
        scoped_target_output_refs = _scoped_target_output_refs_for_candidate(
            primitive=candidate_set.primitive,
            args=args,
            target_output_refs=target_output_refs,
        )
        if target_output_refs and candidate_set.primitive in {
            ActionPrimitive.ADD_SELECT_SLOT,
            ActionPrimitive.REPLACE_SELECT_SLOT,
            ActionPrimitive.REROUTE_FACT,
        }:
            args["target_output_refs"] = scoped_target_output_refs
        if target_relation_edges and candidate_set.primitive in {
            ActionPrimitive.INSERT_BRIDGE,
            ActionPrimitive.REROUTE_FACT,
        }:
            args["target_relation_edges"] = target_relation_edges
        if candidate_set.primitive == ActionPrimitive.REROUTE_FACT:
            if _reroute_conflicts_current_answer_unit(case_view, args):
                continue
            scopes = list(args.get("required_edit_scopes") or [])
            for scope in ("FROM", "JOIN"):
                if scope not in scopes:
                    scopes.append(scope)
            preserve_answer_unit = _answer_unit_preserve_for_reroute(
                _payload(args.get("output_shape_delta"))
            )
            args["answer_unit_preserve"] = preserve_answer_unit
            if preserve_answer_unit:
                args["preserve_invariants"] = sorted(
                    {
                        *[str(item) for item in (args.get("preserve_invariants") or []) if str(item)],
                        "answer_unit_preserve",
                        "preserve_select_projection",
                        "preserve_aggregation_grain",
                    }
                )
                args.pop("target_output_refs", None)
            if args.get("target_output_refs") and not preserve_answer_unit and "SELECT" not in scopes:
                scopes.append("SELECT")
            args["required_edit_scopes"] = scopes
        args["canonical_unresolved_variation_axes"] = list(
            shared_arguments.get("unresolved_variation_axes") or []
        )
        args["canonical_contract"] = _canonical_contract_for_action(
            program_id=program_id,
            canonical_op=canonical_op,
            member_variants=member_variants,
        )
        args["effect_contract"] = _effect_contract_for_action(
            group=group,
            canonical_op=canonical_op,
        )
        if candidate_set.primitive in {
            ActionPrimitive.ADD_SELECT_SLOT,
            ActionPrimitive.REPLACE_SELECT_SLOT,
        }:
            args["canonical_contract"]["target_output_refs"] = scoped_target_output_refs
        args["bundle_id"] = str(bundle_meta.get("bundle_id") or "")
        args["effect_kind"] = str(bundle_meta.get("effect_kind") or "")
        args["bound_branch_id"] = str(bundle_meta.get("bundle_id") or "")
        args["cleanup_edits"] = [
            *list(args.get("cleanup_edits") or []),
            *list(bundle_meta.get("cleanup_op_ids") or []),
        ]
        if candidate_set.primitive == ActionPrimitive.DROP_SELECT_SLOT:
            args = _drop_select_alias_cleanup_dependency_args(args)
        if candidate_set.primitive in {ActionPrimitive.DROP_SELECT_SLOT, ActionPrimitive.DROP_SIDE}:
            args = _pair_output_distinct_dependency_args(case_view, args)
        args["counts_as_action"] = int(bundle_meta.get("counts_as_action") or 1)
        args["bundle_primary_primitive"] = str(bundle_meta.get("primary_primitive") or "")
        args["bundle_selection_key"] = _bundle_selection_key(args)
        annotated.append(
            candidate.model_copy(
                update={
                    "arguments": args,
                    "bundle_id": str(bundle_meta.get("bundle_id") or "") or None,
                    "effect_kind": str(bundle_meta.get("effect_kind") or "") or None,
                    "bound_branch_id": str(bundle_meta.get("bundle_id") or "") or None,
                    "cleanup_edits": [
                        item if isinstance(item, dict) else {"op_id": str(item)}
                        for item in (args.get("cleanup_edits") or [])
                        if item
                    ],
                    "counts_as_action": int(bundle_meta.get("counts_as_action") or 1),
                    "bundle_selection_key": args["bundle_selection_key"] or None,
                    "bundle_primary_primitive": str(bundle_meta.get("primary_primitive") or "") or None,
                }
            )
        )
    empty_reason = candidate_set.empty_reason
    if candidate_set.candidates and not annotated:
        empty_reason = "target_columns_not_supported_by_current_variant_refs"
    return candidate_set.model_copy(
        update={"candidates": annotated, "empty_reason": empty_reason}
    )


def _required_edit_scopes(primary_scope: str, repair_program: Sequence[Dict[str, Any]]) -> List[str]:
    scopes = [primary_scope]
    for step in repair_program:
        if not bool(step.get("is_dependency") or False):
            continue
        scope = str(step.get("locus") or "").upper()
        if scope and scope not in scopes:
            scopes.append(scope)
    return scopes


def _primary_scope_for_primitive(primitive: ActionPrimitive) -> str:
    if primitive in {
        ActionPrimitive.ADD_SELECT_SLOT,
        ActionPrimitive.REPLACE_SELECT_SLOT,
        ActionPrimitive.DROP_SELECT_SLOT,
        ActionPrimitive.SELECT_ENFORCE_DISTINCT,
        ActionPrimitive.SWITCH_CANONICAL_FIELD,
        ActionPrimitive.MATERIALIZE_RANKING_OUTPUT,
    }:
        return "SELECT"
    if primitive == ActionPrimitive.INSERT_BRIDGE:
        return "JOIN"
    if primitive == ActionPrimitive.REROUTE_FACT:
        return "FROM"
    if primitive in {ActionPrimitive.DROP_SIDE, ActionPrimitive.MOVE_CONDITION}:
        return "WHERE"
    return "SELECT"


def _repair_program_for_canonical_op(
    repair_program: Sequence[Dict[str, Any]],
    canonical_op: Dict[str, Any],
    member_variants: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    op_id = str(canonical_op.get("op_id") or "")
    op_type = str(canonical_op.get("op_type") or "")
    _ = member_variants
    out: List[Dict[str, Any]] = []
    for step in repair_program or []:
        payload = dict(step)
        if bool(payload.get("is_dependency") or False):
            out.append(payload)
            continue
        args = _payload(payload.get("arguments"))
        if op_id and str(args.get("canonical_op_id") or "") == op_id:
            out.append(payload)
            continue
        if not op_id and op_type and str(args.get("canonical_op_type") or "") == op_type:
            out.append(payload)
    return out


def _column_refs(condition: str) -> List[str]:
    refs: List[str] = []
    for match in re.finditer(
        r"\b([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*|[A-Za-z_][A-Za-z0-9_]*)\b",
        str(condition or ""),
    ):
        value = match.group(1)
        if value.upper() in {
            "AND",
            "OR",
            "NOT",
            "LIKE",
            "IN",
            "IS",
            "NULL",
            "BETWEEN",
        }:
            continue
        refs.append(value)
    return refs


def _memory_text(group: GroupSummary) -> str:
    parts: List[str] = [
        str(group.core_interface.repair_goal or ""),
        str(group.instantiation_program.template or ""),
    ]
    for rule in group.instantiation_program.branch_rules or []:
        parts.append(str(getattr(rule, "condition", "") or ""))
        parts.append(str(getattr(rule, "action_hint", "") or ""))
    return "\n".join(part for part in parts if part)


def _identifier_terms(text: str) -> List[str]:
    terms: List[str] = []
    for raw in re.findall(r"`([^`]+)`", text):
        terms.append(raw)
    for raw in re.findall(
        r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?\b", text
    ):
        if raw.lower() in {
            "and",
            "or",
            "not",
            "select",
            "where",
            "join",
            "from",
            "count",
            "distinct",
            "case",
            "when",
            "then",
            "else",
            "end",
            "null",
            "is",
            "in",
            "like",
            "as",
            "sql",
            "question",
        }:
            continue
        terms.append(raw)
    out: List[str] = []
    seen: set[str] = set()
    for term in terms:
        term = term.strip().strip("`\"[]")
        if not term:
            continue
        key = term.lower()
        if key not in seen:
            seen.add(key)
            out.append(term)
    return out


def _memory_direction_terms(memory_text: str) -> Dict[str, List[str]]:
    keep_keywords = (
        "keep",
        "retain",
        "preserve",
        "only keep",
        "保留",
        "只保留",
        "只在",
    )
    drop_keywords = (
        "drop",
        "remove",
        "delete",
        "exclude",
        "删",
        "删掉",
        "删除",
        "去掉",
        "不要",
        "不再",
    )
    keep_terms: List[str] = []
    drop_terms: List[str] = []
    clauses = re.split(r"[\n。；;，,]", str(memory_text or ""))
    for clause in clauses:
        lower = clause.lower()
        terms = _identifier_terms(clause)
        if any(keyword in lower for keyword in keep_keywords) or any(
            keyword in clause for keyword in keep_keywords
        ):
            keep_terms.extend(terms)
        if any(keyword in lower for keyword in drop_keywords) or any(
            keyword in clause for keyword in drop_keywords
        ):
            drop_terms.extend(terms)

    def _dedup(values: List[str]) -> List[str]:
        out: List[str] = []
        seen: set[str] = set()
        for value in values:
            key = value.lower()
            if key not in seen:
                seen.add(key)
                out.append(value)
        return out

    return {"keep_terms": _dedup(keep_terms), "drop_terms": _dedup(drop_terms)}


def _term_hits(text: str, terms: Sequence[str]) -> List[str]:
    haystack = str(text or "").lower()
    hits: List[str] = []
    for term in terms:
        raw = str(term or "").lower()
        if not raw:
            continue
        variants = {raw}
        if "." in raw:
            variants.add(raw.rsplit(".", 1)[-1])
        matched = False
        for variant in variants:
            if not variant:
                continue
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(variant)}(?![A-Za-z0-9_])"
            if re.search(pattern, haystack):
                matched = True
                break
        if matched:
            hits.append(term)
    return hits


def _where_side_memory_alignment(
    *,
    drop_text: str,
    keep_text: str,
    memory_text: str,
) -> Dict[str, Any]:
    terms = _memory_direction_terms(memory_text)
    keep_terms = terms["keep_terms"]
    drop_terms = terms["drop_terms"]
    keep_in_keep = _term_hits(keep_text, keep_terms)
    keep_in_drop = _term_hits(drop_text, keep_terms)
    drop_in_drop = _term_hits(drop_text, drop_terms)
    drop_in_keep = _term_hits(keep_text, drop_terms)
    score = len(keep_in_keep) + len(drop_in_drop) - len(keep_in_drop) - len(drop_in_keep)
    return {
        "score": score,
        "keep_terms": keep_terms,
        "drop_terms": drop_terms,
        "keep_terms_in_keep_side": keep_in_keep,
        "keep_terms_in_drop_side": keep_in_drop,
        "drop_terms_in_drop_side": drop_in_drop,
        "drop_terms_in_keep_side": drop_in_keep,
    }


def _ast_for_sql(sql: str) -> Dict[str, Any]:
    from method.EEA.rulebook.common.analysis.structure_family import cached_ast_signature

    return cached_ast_signature(sql or "") or {}


def _current_predicate_rows(case_view: RuntimeCaseView) -> List[Dict[str, Any]]:
    ast = _ast_for_sql(getattr(case_view.pred_manifestation, "top1_sql", "") or "")
    rows: List[Dict[str, Any]] = []
    for scope, values in (
        ("WHERE", ast.get("predicates") or []),
        ("HAVING", ast.get("having") or []),
    ):
        for index, predicate in enumerate(values):
            text = str(predicate or "").strip()
            if not text:
                continue
            rows.append(
                {
                    "scope": scope,
                    "predicate": text,
                    "normalized": re.sub(r"\s+", " ", text).strip().lower(),
                    "index": index,
                }
            )
    return rows


def _aggregate_select_exprs(case_view: RuntimeCaseView) -> List[str]:
    rows: List[str] = []
    for expr in getattr(case_view.pred_manifestation, "columns", None) or []:
        text = str(expr or "").strip()
        if re.search(r"\b(count|sum|avg|min|max)\s*\(", text, flags=re.IGNORECASE):
            rows.append(text)
    return rows


def _canonical_accessory_policies(
    canonical_op: Dict[str, Any],
    *,
    member_variants: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    args = _payload(canonical_op.get("arguments"))
    rows: List[Dict[str, Any]] = []
    for policy in args.get("accessory_policies") or []:
        payload = _payload(policy)
        if payload:
            rows.append(payload)
    for variant in member_variants or []:
        for policy in _payload(variant).get("accessory_policies") or []:
            payload = _payload(policy)
            if payload:
                rows.append(payload)
    return rows


def _current_anchor_expr(case_view: RuntimeCaseView) -> str:
    ast = _ast_for_sql(getattr(case_view.pred_manifestation, "top1_sql", "") or "")
    group_by = [str(item).strip() for item in (ast.get("group_by") or []) if str(item).strip()]
    if group_by:
        return group_by[0]
    for expr in getattr(case_view.pred_manifestation, "columns", None) or []:
        text = str(expr or "").strip()
        if _expr_is_identifier_like(text):
            return text
    return str((getattr(case_view.pred_manifestation, "columns", None) or [""])[0] or "")


# =============================================================================
# Primitive 级枚举（接收抽象 skeleton + slots，不依赖具体 memory object 类型）
# =============================================================================


def _enumerate_add_select_slot(
    *,
    case_view: RuntimeCaseView,
    skeleton: RepairSkeletonStructural,
    slots: Sequence[InstantiationSlot],
    group_id: str,
    group_type: GroupType,
    memory_text: str = "",
    repair_program: Sequence[Dict[str, Any]] = (),
    preferred_target_refs: Sequence[Dict[str, Any]] = (),
    strict_preferred_targets: bool = False,
) -> ActionCandidateSet:
    candidates: List[ActionCandidate] = []
    empty_reason: Optional[str] = None

    if skeleton.op_family != OpFamily.ADD:
        return ActionCandidateSet(
            primitive=ActionPrimitive.ADD_SELECT_SLOT,
            candidates=[],
            empty_reason="op_family is not add",
        )
    if skeleton.locus != Locus.SELECT:
        return ActionCandidateSet(
            primitive=ActionPrimitive.ADD_SELECT_SLOT,
            candidates=[],
            empty_reason="locus is not SELECT",
        )

    col_slots = _slots_of_kind(slots, "column")
    view = case_view.local_schema_view

    if not col_slots:
        empty_reason = "no instantiation_slot of kind=column"
    else:
        allowed_all: set[str] = set()
        for slot in col_slots:
            for role in slot.allowed_role_families:
                allowed_all.add(role)

        # 规范化 pred_cols：既存完整表达式也存 bare 列名，避免 "c.client_id" 不匹配 "client_id"
        pred_cols_set: set[str] = set()
        for expr in case_view.pred_manifestation.columns or []:
            pred_cols_set.add(expr.lower())
            bare = _parse_expr_column(expr)
            if bare:
                pred_cols_set.add(bare.lower())

        column_options: List[Dict[str, Any]] = []
        for table, cols in view.columns_by_table.items():
            for col in cols:
                role = _get_column_role(table, col, view)
                if allowed_all and not _is_role_allowed(role, list(allowed_all)):
                    continue
                if col.lower() in pred_cols_set:
                    continue
                column_options.append(
                    {
                        "target_table": table,
                        "target_column": col,
                        "target_role_family": role,
                    }
                )

        shape_slot_count, shape_payload = _select_slot_delta(
            skeleton,
            expected_direction="increase",
        )
        try:
            shape_current_arity = int(shape_payload.get("current_arity") or 0)
        except Exception:
            shape_current_arity = 0
        current_arity = len(case_view.pred_manifestation.columns or [])
        target_counts: List[int] = []
        if shape_slot_count > 1 and (not shape_current_arity or current_arity <= shape_current_arity):
            target_counts.append(shape_slot_count)
        # Pattern arity is learned from source members. Current cases can already
        # contain part of the answer interface, so always expose single-slot add
        # alternatives as well. The compiler still selects exactly one candidate.
        target_counts.append(1)
        target_counts = sorted(set(count for count in target_counts if count > 0), reverse=True)

        if not preferred_target_refs:
            return ActionCandidateSet(
                primitive=ActionPrimitive.ADD_SELECT_SLOT,
                candidates=[],
                empty_reason=WUV2_2_SEED_TARGET_BINDING_DISABLED_REASON,
            )
        preferred_options = _schema_column_options_from_refs(preferred_target_refs, view)
        if strict_preferred_targets and preferred_target_refs:
            option_pool = _dedupe_column_options(preferred_options)
        else:
            ranked_by_question = _rank_column_options_for_text(
                column_options,
                question=str(case_view.question or ""),
                evidence=str(case_view.evidence or ""),
            )
            memory_targets = _memory_mentioned_options(
                ranked_by_question,
                memory_text=memory_text,
            )
            option_pool = _dedupe_column_options(
                [
                    *preferred_options,
                    *memory_targets,
                    *ranked_by_question[:MAX_MULTI_SLOT_OPTION_POOL],
                ]
            )
        contract_repair_program = [dict(step) for step in repair_program]

        for target_slot_count in target_counts:
            if len(option_pool) < target_slot_count:
                empty_reason = empty_reason or (
                    f"need {target_slot_count} additional SELECT slots but only "
                    f"{len(option_pool)} role-compatible columns are available"
                )
                continue
            if target_slot_count > 1:
                source_sequences = combinations(option_pool, target_slot_count)
            else:
                source_sequences = ((option,) for option in option_pool)
            for combo in source_sequences:
                target_columns = [dict(item) for item in combo]
                combo_key = ",".join(
                    f"{item['target_table']}.{item['target_column']}" for item in target_columns
                )
                candidates.append(
                    ActionCandidate(
                        candidate_id=_hash_cand(
                            f"ADDN|{group_id}|{target_slot_count}|{combo_key}"
                        ),
                        arguments={
                            "target_columns": target_columns,
                            "target_slot_count": target_slot_count,
                            "output_shape_delta": shape_payload,
                            "memory_alignment": _add_memory_alignment(
                                target_columns=target_columns,
                                memory_text=memory_text,
                                question=str(case_view.question or ""),
                                evidence=str(case_view.evidence or ""),
                                pred_exprs=case_view.pred_manifestation.columns or [],
                            ),
                            "repair_program": contract_repair_program,
                            "required_edit_scopes": _required_edit_scopes(
                                "SELECT",
                                contract_repair_program,
                            ),
                        },
                        provenance=(
                            f"group={group_id};local_schema_multi_select:{combo_key}"
                        ),
                        schema_legal=True,
                        source_group_id=group_id,
                        source_group_type=group_type,
                    )
                )

        if not candidates:
            empty_reason = empty_reason or (
                f"no column matches allowed_role_families={sorted(allowed_all) or 'any'}; "
                f"schema recall miss?"
            )
        else:
            candidates = sorted(candidates, key=_add_candidate_sort_key)[
                :MAX_MULTI_SLOT_COMBINATIONS
            ]

    return ActionCandidateSet(
        primitive=ActionPrimitive.ADD_SELECT_SLOT,
        candidates=candidates,
        empty_reason=empty_reason,
    )


def _enumerate_replace_select_slot(
    *,
    case_view: RuntimeCaseView,
    skeleton: RepairSkeletonStructural,
    slots: Sequence[InstantiationSlot],
    group_id: str,
    group_type: GroupType,
    memory_text: str = "",
    repair_program: Sequence[Dict[str, Any]] = (),
    preferred_target_refs: Sequence[Dict[str, Any]] = (),
    strict_preferred_targets: bool = False,
) -> ActionCandidateSet:
    candidates: List[ActionCandidate] = []
    empty_reason: Optional[str] = None

    if skeleton.op_family != OpFamily.REPLACE:
        return ActionCandidateSet(
            primitive=ActionPrimitive.REPLACE_SELECT_SLOT,
            candidates=[],
            empty_reason="op_family is not replace",
        )
    if skeleton.locus != Locus.SELECT:
        return ActionCandidateSet(
            primitive=ActionPrimitive.REPLACE_SELECT_SLOT,
            candidates=[],
            empty_reason="locus is not SELECT",
        )

    view = case_view.local_schema_view
    pred_exprs = case_view.pred_manifestation.columns or []
    col_slots = _slots_of_kind(slots, "column")
    required_column_slot_count = sum(1 for slot in col_slots if getattr(slot, "required", True))

    allowed: set[str] = set()
    for s in col_slots:
        for r in s.allowed_role_families:
            allowed.add(r)

    shape_payload = _shape_delta_payload(skeleton)
    contract_repair_program = [dict(step) for step in repair_program]
    if not preferred_target_refs:
        return ActionCandidateSet(
            primitive=ActionPrimitive.REPLACE_SELECT_SLOT,
            candidates=[],
            empty_reason=WUV2_2_SEED_TARGET_BINDING_DISABLED_REASON,
        )
    direction = str(shape_payload.get("arity_direction") or "").lower()
    try:
        current_arity = int(shape_payload.get("current_arity") or len(pred_exprs) or 0)
    except Exception:
        current_arity = len(pred_exprs)
    try:
        target_arity = int(shape_payload.get("target_arity") or current_arity)
    except Exception:
        target_arity = current_arity
    replace_count = 1
    if (
        direction == "same"
        and current_arity == target_arity
        and current_arity > 1
        and required_column_slot_count > 1
    ):
        replace_count = min(current_arity, required_column_slot_count)

    target_options: List[Dict[str, Any]] = []
    for table, cols in view.columns_by_table.items():
        for col in cols:
            role = _get_column_role(table, col, view)
            if allowed and not _is_role_allowed(role, list(allowed)):
                continue
            target_options.append(
                {
                    "target_table": table,
                    "target_column": col,
                    "target_role_family": role,
                }
            )

    ref_target_columns = _schema_column_options_from_refs(preferred_target_refs, view)
    if ref_target_columns and len(pred_exprs) >= len(ref_target_columns):
        slot_indexes = [
            _int_or_none(option.get("target_slot_index"))
            for option in ref_target_columns
        ]
        if (
            all(index is not None for index in slot_indexes)
            and len(set(slot_indexes)) == len(slot_indexes)
            and all(0 <= int(index) < len(pred_exprs) for index in slot_indexes if index is not None)
        ):
            source_indexes = [int(index) for index in slot_indexes if index is not None]
        else:
            source_indexes = list(range(len(ref_target_columns)))
        source_exprs = [pred_exprs[index] for index in source_indexes]
        target_key = ",".join(
            f"{item['target_table']}.{item['target_column']}" for item in ref_target_columns
        )
        candidates.append(
            ActionCandidate(
                candidate_id=_hash_cand(
                    f"REPREF|{group_id}|{','.join(source_exprs)}->{target_key}"
                ),
                arguments={
                    "from_exprs": source_exprs,
                    "source_slot_indexes": source_indexes,
                    "target_columns": [dict(item) for item in ref_target_columns],
                    "replace_count": len(ref_target_columns),
                    "output_shape_delta": shape_payload,
                    "replacement_scope": "select_projection",
                    "canonical_ref_binding": True,
                    "repair_program": contract_repair_program,
                    "required_edit_scopes": _required_edit_scopes(
                        "SELECT",
                        contract_repair_program,
                    ),
                },
                provenance=f"group={group_id};target_output_refs_projection={target_key}",
                source_group_id=group_id,
                source_group_type=group_type,
            )
        )
    if strict_preferred_targets and preferred_target_refs:
        if not candidates:
            empty_reason = (
                "preferred target refs could not be bound to current SELECT slots"
            )
        return ActionCandidateSet(
            primitive=ActionPrimitive.REPLACE_SELECT_SLOT,
            candidates=candidates,
            empty_reason=empty_reason,
        )

    # Pattern-level same-arity SELECT replacements often represent a projection
    # interface, not independent per-column actions. Expose a single candidate
    # that rewrites the projection as a unit while keeping single-slot
    # alternatives available for lower-grain memories.
    if replace_count > 1:
        if len(pred_exprs) < replace_count:
            empty_reason = (
                f"need to replace {replace_count} SELECT slots from output_shape_delta "
                f"but pred SELECT has only {len(pred_exprs)} columns"
            )
        elif len(target_options) < replace_count:
            empty_reason = (
                f"need {replace_count} target SELECT slots from output_shape_delta "
                f"but only {len(target_options)} role-compatible columns are available"
            )
        else:
            source_combos = list(combinations(enumerate(pred_exprs), replace_count))
            ranked_targets = _rank_column_options_for_text(
                target_options,
                question=str(case_view.question or ""),
                evidence=" ".join([str(case_view.evidence or ""), str(memory_text or "")]),
            )
            if replace_count <= 2:
                option_pool_size = MAX_PAIR_REPLACE_OPTION_POOL
            elif replace_count == 3:
                option_pool_size = MAX_MULTI_SLOT_OPTION_POOL
            else:
                option_pool_size = max(replace_count, replace_count + 4)
            memory_targets = _memory_mentioned_options(
                ranked_targets,
                memory_text=memory_text,
            )
            option_pool = _dedupe_column_options(
                [
                    *memory_targets,
                    *ranked_targets[: max(replace_count, option_pool_size)],
                ]
            )
            option_rank = {
                f"{option.get('target_table')}::{option.get('target_column')}": index
                for index, option in enumerate(option_pool)
            }
            alias_map = _sql_alias_map(case_view.pred_manifestation.top1_sql)
            question_positions = _token_positions(
                " ".join([str(case_view.question or ""), str(case_view.evidence or "")])
            )
            combo_count = 0
            for source_combo in source_combos:
                source_indexes = [idx for idx, _ in source_combo]
                source_exprs = [expr for _, expr in source_combo]
                source_refs = [_expr_table_column(expr, alias_map) for expr in source_exprs]
                source_key = ",".join(f"{idx}:{expr}" for idx, expr in source_combo)
                if replace_count <= 3:
                    target_sequences = _rank_target_sequences(
                        list(permutations(option_pool, replace_count)),
                        source_refs=source_refs,
                        option_rank=option_rank,
                        question_positions=question_positions,
                    )
                else:
                    target_sequences = list(permutations(option_pool, replace_count))
                for target_sequence in target_sequences:
                    combo_count += 1
                    target_columns = [dict(item) for item in target_sequence]
                    target_key = ",".join(
                        f"{item['target_table']}.{item['target_column']}"
                        for item in target_columns
                    )
                    candidates.append(
                        ActionCandidate(
                            candidate_id=_hash_cand(f"REPN|{group_id}|{source_key}->{target_key}"),
                            arguments={
                                "from_exprs": source_exprs,
                                "source_slot_indexes": source_indexes,
                                "target_columns": target_columns,
                                "replace_count": replace_count,
                                "output_shape_delta": shape_payload,
                                "replacement_scope": "select_projection",
                                "repair_program": contract_repair_program,
                                "required_edit_scopes": _required_edit_scopes(
                                    "SELECT",
                                    contract_repair_program,
                                ),
                            },
                            provenance=(
                                f"group={group_id};pred_select_exprs={source_key}; "
                                f"target_projection={target_key}"
                            ),
                            source_group_id=group_id,
                            source_group_type=group_type,
                        )
                    )
                    if combo_count >= MAX_MULTI_SLOT_COMBINATIONS:
                        break
                if combo_count >= MAX_MULTI_SLOT_COMBINATIONS:
                    break

    for source_index, pred_expr in enumerate(pred_exprs):
        pred_col = _parse_expr_column(pred_expr)
        if not pred_col:
            continue
        for option in target_options:
            table = option["target_table"]
            col = option["target_column"]
            if col.lower() == pred_col.lower():
                continue
            role = option["target_role_family"]
            candidates.append(
                ActionCandidate(
                    candidate_id=_hash_cand(f"REP|{group_id}|{pred_expr}|{table}.{col}"),
                    arguments={
                        "from_expr": pred_expr,
                        "from_exprs": [pred_expr],
                        "source_slot_index": source_index,
                        "source_slot_indexes": [source_index],
                        "to_table": table,
                        "to_column": col,
                        "to_role_family": role,
                        "target_columns": [dict(option)],
                        "replace_count": 1,
                        "output_shape_delta": shape_payload,
                        "repair_program": contract_repair_program,
                        "required_edit_scopes": _required_edit_scopes(
                            "SELECT",
                            contract_repair_program,
                        ),
                        "memory_alignment": _replace_memory_alignment(
                            pred_expr=pred_expr,
                            target_table=table,
                            target_column=col,
                            memory_text=memory_text,
                        ),
                    },
                    provenance=(
                        f"group={group_id};pred_expr={pred_expr}; candidate={table}.{col}"
                        + (f"|role={role}" if role else "")
                    ),
                    source_group_id=group_id,
                    source_group_type=group_type,
                )
            )

    if not candidates:
        empty_reason = "no role-compatible replacement column found in local schema"
    else:
        candidates = sorted(candidates, key=_replace_candidate_sort_key)[
            :MAX_REPLACE_SELECT_CANDIDATES
        ]

    return ActionCandidateSet(
        primitive=ActionPrimitive.REPLACE_SELECT_SLOT,
        candidates=candidates,
        empty_reason=empty_reason,
    )


def _enumerate_drop_select_slot(
    *,
    case_view: RuntimeCaseView,
    skeleton: RepairSkeletonStructural,
    slots: Sequence[InstantiationSlot],
    group_id: str,
    group_type: GroupType,
    repair_program: Sequence[Dict[str, Any]] = (),
) -> ActionCandidateSet:
    candidates: List[ActionCandidate] = []
    empty_reason: Optional[str] = None

    if skeleton.op_family != OpFamily.DROP:
        return ActionCandidateSet(
            primitive=ActionPrimitive.DROP_SELECT_SLOT,
            candidates=[],
            empty_reason="op_family is not drop",
        )
    if skeleton.locus != Locus.SELECT:
        return ActionCandidateSet(
            primitive=ActionPrimitive.DROP_SELECT_SLOT,
            candidates=[],
            empty_reason="locus is not SELECT",
        )

    pred_exprs = case_view.pred_manifestation.columns or []
    contract_repair_program = [dict(step) for step in repair_program]
    drop_count, shape_payload = _select_slot_delta(
        skeleton,
        expected_direction="decrease",
    )
    if drop_count > 1:
        if len(pred_exprs) < drop_count:
            empty_reason = (
                f"need to drop {drop_count} SELECT slots from output_shape_delta "
                f"but pred SELECT has only {len(pred_exprs)} columns"
            )
        else:
            for combo in combinations(enumerate(pred_exprs), drop_count):
                indexes = [idx for idx, _ in combo]
                exprs = [expr for _, expr in combo]
                combo_key = ",".join(f"{idx}:{expr}" for idx, expr in combo)
                candidates.append(
                    ActionCandidate(
                        candidate_id=_hash_cand(f"DROPN|{group_id}|{combo_key}"),
                        arguments=_pair_output_distinct_dependency_args(case_view, _drop_select_alias_cleanup_dependency_args({
                            "from_exprs": exprs,
                            "side_indexes": indexes,
                            "drop_count": drop_count,
                            "output_shape_delta": shape_payload,
                            "repair_program": contract_repair_program,
                            "required_edit_scopes": _required_edit_scopes(
                                "SELECT",
                                contract_repair_program,
                            ),
                        })),
                        provenance=f"group={group_id};pred_select_exprs={combo_key}",
                        source_group_id=group_id,
                        source_group_type=group_type,
                    )
                )
    else:
        for idx, expr in enumerate(pred_exprs):
            candidates.append(
                ActionCandidate(
                    candidate_id=_hash_cand(f"DROP|{group_id}|{expr}"),
                    arguments=_pair_output_distinct_dependency_args(case_view, _drop_select_alias_cleanup_dependency_args({
                        "from_expr": expr,
                        "from_exprs": [expr],
                        "side_index": idx,
                        "side_indexes": [idx],
                        "drop_count": 1,
                        "output_shape_delta": shape_payload,
                        "repair_program": contract_repair_program,
                        "required_edit_scopes": _required_edit_scopes(
                            "SELECT",
                            contract_repair_program,
                        ),
                    })),
                    provenance=f"group={group_id};pred_select_expr={expr}",
                    source_group_id=group_id,
                    source_group_type=group_type,
                )
            )

    if not candidates:
        empty_reason = empty_reason or "pred SELECT has no columns"

    return ActionCandidateSet(
        primitive=ActionPrimitive.DROP_SELECT_SLOT,
        candidates=candidates,
        empty_reason=empty_reason,
    )


def _enumerate_drop_side(
    *,
    case_view: RuntimeCaseView,
    skeleton: RepairSkeletonStructural,
    slots: Sequence[InstantiationSlot],
    group_id: str,
    group_type: GroupType,
    memory_text: str = "",
    repair_program: Sequence[Dict[str, Any]] = (),
) -> ActionCandidateSet:
    candidates: List[ActionCandidate] = []
    empty_reason: Optional[str] = None

    contract_repair_program = [dict(step) for step in repair_program]

    if skeleton.locus in (Locus.WHERE, Locus.PREDICATE, Locus.SCOPE):
        if skeleton.target_family != TargetFamily.CONDITION:
            return ActionCandidateSet(
                primitive=ActionPrimitive.DROP_SIDE,
                candidates=[],
                empty_reason=f"target_family={skeleton.target_family.value} is not condition",
            )
        if skeleton.op_family not in (
            OpFamily.REPLACE,
            OpFamily.DROP,
            OpFamily.COLLAPSE,
            OpFamily.NARROW,
            OpFamily.BROADEN,
        ):
            return ActionCandidateSet(
                primitive=ActionPrimitive.DROP_SIDE,
                candidates=[],
                empty_reason=f"op_family={skeleton.op_family.value} is not side-compatible",
            )
        from method.EEA.rulebook.common.analysis.structure_family import cached_ast_signature

        ast = cached_ast_signature(case_view.pred_manifestation.top1_sql) or {}
        for predicate_index, predicate in enumerate(ast.get("predicates") or []):
            sides = _split_or_sides(str(predicate))
            if len(sides) < 2:
                continue
            for side_index, side in enumerate(sides):
                keep_conditions = [value for idx, value in enumerate(sides) if idx != side_index]
                drop_edges = _related_join_edges(ast, side)
                keep_related_edges = [
                    edge
                    for condition in keep_conditions
                    for edge in _related_join_edges(ast, condition)
                ]
                drop_text = " ".join(
                    [side] + [str(value) for edge in drop_edges for value in edge.values()]
                )
                keep_text = " ".join(
                    keep_conditions
                    + [str(value) for edge in keep_related_edges for value in edge.values()]
                )
                candidates.append(
                    ActionCandidate(
                        candidate_id=_hash_cand(
                            f"WHERE_SIDE|{group_id}|{predicate_index}|{side_index}|{predicate}"
                        ),
                        arguments={
                            "location": "WHERE",
                            "predicate_index": predicate_index,
                            "side_index": side_index,
                            "from_predicate": str(predicate),
                            "drop_condition": side,
                            "drop_condition_refs": _column_refs(side),
                            "keep_conditions": keep_conditions,
                            "keep_condition_refs": [
                                ref for condition in keep_conditions for ref in _column_refs(condition)
                            ],
                            "to_predicate": " OR ".join(keep_conditions),
                            "related_join_edges_to_drop": drop_edges,
                            "related_join_edges_to_keep": keep_related_edges,
                            "repair_program": contract_repair_program,
                            "required_edit_scopes": _required_edit_scopes(
                                "WHERE",
                                contract_repair_program,
                            ),
                            "memory_alignment": _where_side_memory_alignment(
                                drop_text=drop_text,
                                keep_text=keep_text,
                                memory_text=memory_text,
                            ),
                        },
                        provenance=(
                            f"group={group_id};where_predicate[{predicate_index}]"
                            f";or_side[{side_index}]={side}"
                        ),
                        source_group_id=group_id,
                        source_group_type=group_type,
                    )
                )
        if not candidates:
            empty_reason = "no OR condition sides found in WHERE/PREDICATE"
        return ActionCandidateSet(
            primitive=ActionPrimitive.DROP_SIDE,
            candidates=candidates,
            empty_reason=empty_reason,
        )

    if skeleton.locus != Locus.SELECT:
        return ActionCandidateSet(
            primitive=ActionPrimitive.DROP_SIDE,
            candidates=[],
            empty_reason=f"locus={skeleton.locus.value} is not SELECT",
        )
    op = skeleton.op_family
    if op not in (OpFamily.DROP, OpFamily.COLLAPSE):
        return ActionCandidateSet(
            primitive=ActionPrimitive.DROP_SIDE,
            candidates=[],
            empty_reason=f"op_family={op.value} is not drop/collapse",
        )

    pred_exprs = case_view.pred_manifestation.columns or []
    if len(pred_exprs) < 2:
        return ActionCandidateSet(
            primitive=ActionPrimitive.DROP_SIDE,
            candidates=[],
            empty_reason="pred arity < 2; no pair to drop a side from",
        )

    for idx, expr in enumerate(pred_exprs):
        candidates.append(
            ActionCandidate(
                candidate_id=_hash_cand(f"SIDE|{group_id}|{idx}|{expr}"),
                arguments=_pair_output_distinct_dependency_args(case_view, {
                    "side_index": idx,
                    "side_indexes": [idx],
                    "from_expr": expr,
                    "from_exprs": [expr],
                    "repair_program": contract_repair_program,
                    "required_edit_scopes": _required_edit_scopes(
                        "SELECT",
                        contract_repair_program,
                    ),
                }),
                provenance=f"group={group_id};pair_side[{idx}]={expr}",
                source_group_id=group_id,
                source_group_type=group_type,
            )
        )

    if not candidates:
        empty_reason = "unable to enumerate pair sides"

    return ActionCandidateSet(
        primitive=ActionPrimitive.DROP_SIDE,
        candidates=candidates,
        empty_reason=empty_reason,
    )


def _relation_endpoint_key(endpoint: Dict[str, Any]) -> str:
    table = str(endpoint.get("table") or "").strip().lower()
    column = str(endpoint.get("column") or "").strip().lower()
    return f"{table}.{column}" if table or column else ""


def _relation_key_from_payload(relation: Dict[str, Any]) -> str:
    key = str(relation.get("canonical_key") or "").strip().lower()
    if key:
        return key
    left = _relation_endpoint_key(_payload(relation.get("left")))
    right = _relation_endpoint_key(_payload(relation.get("right")))
    parts = sorted(part for part in (left, right) if part)
    return "=".join(parts)


def _relation_key_set(relations: Sequence[Dict[str, Any]]) -> set[str]:
    return {
        key
        for relation in relations or []
        if (key := _relation_key_from_payload(_payload(relation)))
    }


def _current_relation_key_set(case_view: RuntimeCaseView) -> set[str]:
    sql = str(getattr(case_view.pred_manifestation, "top1_sql", "") or "")
    alias_map = _sql_alias_map(sql)
    try:
        from method.EEA.rulebook.common.analysis.structure_family import cached_ast_signature

        ast = cached_ast_signature(sql) or {}
    except Exception:
        ast = {}
    keys: set[str] = set()
    for row in ast.get("join_edges") or []:
        on_expr = str(_payload(row).get("on") or "")
        for match in re.finditer(
            r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)",
            on_expr,
        ):
            left_alias, left_col, right_alias, right_col = match.groups()
            left_table = alias_map.get(left_alias.lower(), left_alias)
            right_table = alias_map.get(right_alias.lower(), right_alias)
            edge = _relation_edge_from_equality(
                f"{left_table}.{left_col} = {right_table}.{right_col}"
            )
            if edge:
                keys.add(_relation_key_from_payload(edge))
    return keys


def _output_refs_same(
    source_refs: Sequence[Dict[str, Any]],
    target_refs: Sequence[Dict[str, Any]],
) -> bool:
    source = _output_ref_keys_from_refs(source_refs)
    target = _output_ref_keys_from_refs(target_refs)
    if len(source) != len(target):
        return False
    for (src_table, src_col), (tgt_table, tgt_col) in zip(source, target):
        if src_col and tgt_col and src_col == tgt_col:
            if not src_table or not tgt_table or src_table == tgt_table:
                continue
        return False
    return bool(source or target)


def _output_ref_columns(
    refs: Sequence[Dict[str, Any]],
) -> List[str]:
    return [
        str(_payload(ref).get("column") or "").strip().lower()
        for ref in refs or []
        if str(_payload(ref).get("column") or "").strip()
    ]


def _canonical_op_preserves_answer_unit_under_reroute(canonical_op: Dict[str, Any]) -> bool:
    args = _payload(canonical_op.get("arguments"))
    shape = _payload(args.get("output_shape_delta"))
    if not _answer_unit_preserve_for_reroute(shape):
        return False
    skeleton = _payload(args.get("skeleton"))
    output_contract = str(skeleton.get("output_contract") or "").strip().upper()
    source_cols = _output_ref_columns(args.get("source_output_refs") or [])
    target_cols = _output_ref_columns(args.get("target_output_refs") or [])
    same_output_column = bool(source_cols and target_cols and source_cols == target_cols)
    has_route_delta = bool(_target_relation_edges_for_action(canonical_op))
    return has_route_delta and (output_contract == "UNCHANGED" or same_output_column)


def _selected_output_columns(case_view: RuntimeCaseView) -> List[str]:
    alias_map = _sql_alias_map(str(getattr(case_view.pred_manifestation, "top1_sql", "") or ""))
    cols: List[str] = []
    for expr in getattr(case_view.pred_manifestation, "columns", None) or []:
        _table, column = _expr_table_column(str(expr or ""), alias_map)
        if column:
            cols.append(column.strip().lower())
    return cols


def _selected_output_ref_keys(case_view: RuntimeCaseView) -> List[Tuple[str, str]]:
    alias_map = _sql_alias_map(str(getattr(case_view.pred_manifestation, "top1_sql", "") or ""))
    keys: List[Tuple[str, str]] = []
    for expr in getattr(case_view.pred_manifestation, "columns", None) or []:
        table, column = _expr_table_column(str(expr or ""), alias_map)
        if column:
            keys.append((str(table or "").strip().lower(), column.strip().lower()))
    return keys


def _output_ref_keys(refs: Sequence[Dict[str, Any]]) -> List[Tuple[str, str]]:
    return [
        (
            str(_payload(ref).get("table") or "").strip().lower(),
            str(_payload(ref).get("column") or "").strip().lower(),
        )
        for ref in refs or []
        if str(_payload(ref).get("column") or "").strip()
    ]


def _reroute_conflicts_current_answer_unit(
    case_view: RuntimeCaseView,
    args: Dict[str, Any],
) -> bool:
    shape = _current_output_shape_for_compiler(case_view)
    if not bool(shape.get("has_aggregate")):
        return False
    current_keys = _selected_output_ref_keys(case_view)
    target_keys = _output_ref_keys(args.get("target_output_refs") or [])
    if not current_keys or not target_keys:
        return False
    return current_keys != target_keys


def _canonical_op_reroute_variant(canonical_op: Dict[str, Any]) -> Dict[str, Any]:
    args = _payload(canonical_op.get("arguments"))
    relation_delta = _payload(_payload(args.get("operation_signature")).get("relation_delta"))
    relation_effect = _payload(_payload(args.get("repair_effect_signature")).get("relation_effect"))
    source_edges = [
        _payload(item)
        for item in (args.get("source_equality_relations") or [])
        if _payload(item)
    ]
    target_edges = _target_relation_edges_for_action(canonical_op)
    for expr in [
        *list(relation_delta.get("source_relation_equalities") or []),
        *list(relation_effect.get("source_relation_paths") or []),
    ]:
        edge = _relation_edge_from_equality(str(expr or ""))
        if edge:
            source_edges.append(edge)
    return {
        "source_equality_relations": source_edges,
        "target_equality_relations": target_edges,
        "source_output_refs": [_payload(item) for item in (args.get("source_output_refs") or [])],
        "target_output_refs": [_payload(item) for item in (args.get("target_output_refs") or [])],
        "output_shape_delta": _payload(args.get("output_shape_delta")),
    }


def _variant_requires_relation_reroute(variant: Dict[str, Any], case_view: RuntimeCaseView) -> bool:
    source_relations = [_payload(item) for item in (variant.get("source_equality_relations") or [])]
    target_relations = [_payload(item) for item in (variant.get("target_equality_relations") or [])]
    source_keys = _relation_key_set(source_relations)
    target_keys = _relation_key_set(target_relations)
    if not target_keys:
        return False
    if source_keys and target_keys <= source_keys:
        return False
    current_keys = _current_relation_key_set(case_view)
    if not current_keys:
        return False
    if target_keys <= current_keys:
        return False
    if source_keys and not (source_keys & current_keys):
        return False
    return True


def _enumerate_reroute_fact(
    *,
    case_view: RuntimeCaseView,
    group_id: str,
    group_type: GroupType,
    member_variants: Sequence[Dict[str, Any]],
    canonical_op: Optional[Dict[str, Any]] = None,
    repair_program: Sequence[Dict[str, Any]] = (),
) -> ActionCandidateSet:
    if canonical_op is None or not _runtime_binding_contract_for_action(canonical_op):
        return ActionCandidateSet(
            primitive=ActionPrimitive.REROUTE_FACT,
            candidates=[],
            empty_reason=WUV2_2_SEED_TARGET_BINDING_DISABLED_REASON,
        )
    candidates: List[ActionCandidate] = []
    variants = list(member_variants or [])
    if not variants and canonical_op:
        fallback = _canonical_op_reroute_variant(canonical_op)
        if fallback.get("target_equality_relations"):
            variants = [fallback]
    for variant in variants:
        payload = _payload(variant)
        if not _variant_requires_relation_reroute(payload, case_view):
            continue
        target_edges = [_payload(item) for item in (payload.get("target_equality_relations") or [])]
        target_refs = [_payload(item) for item in (payload.get("target_output_refs") or [])]
        if not target_edges:
            continue
        key = json.dumps(
            {
                "target_edges": target_edges,
                "target_refs": target_refs,
                "shape": _payload(payload.get("output_shape_delta")),
            },
            sort_keys=True,
            default=str,
            ensure_ascii=False,
        )
        candidates.append(
            ActionCandidate(
                candidate_id=_hash_cand(f"REROUTE|{group_id}|{key}"),
                arguments={
                    "target_relation_edges": target_edges,
                    "target_output_refs": target_refs,
                    "output_shape_delta": _payload(payload.get("output_shape_delta")),
                    "reroute_reason": "source_target_relation_delta",
                    "repair_program": [dict(step) for step in repair_program],
                    "required_edit_scopes": _required_edit_scopes(
                        "FROM",
                        [*repair_program, {"locus": "JOIN"}, {"locus": "SELECT"}],
                    ),
                },
                provenance=f"group={group_id};target_relation_reroute:{_hash_cand(key)}",
                source_group_id=group_id,
                source_group_type=group_type,
            )
        )
    return ActionCandidateSet(
        primitive=ActionPrimitive.REROUTE_FACT,
        candidates=candidates,
        empty_reason=None if candidates else "no_current_variant_relation_reroute_delta",
    )


def _enumerate_insert_bridge(
    *,
    case_view: RuntimeCaseView,
    skeleton: RepairSkeletonStructural,
    slots: Sequence[InstantiationSlot],
    group_id: str,
    group_type: GroupType,
    repair_program: Sequence[Dict[str, Any]] = (),
) -> ActionCandidateSet:
    candidates: List[ActionCandidate] = []
    empty_reason: Optional[str] = None

    if skeleton.locus not in (Locus.JOIN, Locus.BRIDGE):
        return ActionCandidateSet(
            primitive=ActionPrimitive.INSERT_BRIDGE,
            candidates=[],
            empty_reason=f"locus={skeleton.locus.value} is not JOIN/BRIDGE",
        )

    view = case_view.local_schema_view
    contract_repair_program = [dict(step) for step in repair_program]
    t_pred = {t.lower() for t in case_view.pred_manifestation.tables}
    all_tables_lower = {t.lower() for t in view.tables}

    one_hop_targets: set[str] = set()
    for edge in view.pk_fk_edges:
        src_tbl = edge.source.split(".", 1)[0]
        tgt_tbl = edge.target.split(".", 1)[0]
        if src_tbl.lower() in t_pred and tgt_tbl.lower() in all_tables_lower and tgt_tbl.lower() not in t_pred:
            one_hop_targets.add(tgt_tbl)
        if tgt_tbl.lower() in t_pred and src_tbl.lower() in all_tables_lower and src_tbl.lower() not in t_pred:
            one_hop_targets.add(src_tbl)

    for tgt in sorted(one_hop_targets):
        edge = next(
            (
                e
                for e in view.pk_fk_edges
                if (
                    e.source.split(".", 1)[0].lower() in t_pred
                    and e.target.split(".", 1)[0].lower() == tgt.lower()
                )
                or (
                    e.target.split(".", 1)[0].lower() in t_pred
                    and e.source.split(".", 1)[0].lower() == tgt.lower()
                )
            ),
            None,
        )
        if edge is None:
            continue
        candidates.append(
            ActionCandidate(
                candidate_id=_hash_cand(f"BRIDGE1|{group_id}|{edge.source}->{edge.target}"),
                arguments={
                    "bridge_table": tgt,
                    "source_table_column": edge.source,
                    "target_table_column": edge.target,
                    "hops": 1,
                    "repair_program": contract_repair_program,
                    "required_edit_scopes": _required_edit_scopes(
                        "JOIN",
                        contract_repair_program,
                    ),
                },
                provenance=f"group={group_id};pk_fk:{edge.source}->{edge.target}",
                source_group_id=group_id,
                source_group_type=group_type,
            )
        )

    if not candidates:
        empty_reason = SCHEMA_RECALL_MISS_CATEGORIES[0]  # "missing_bridge_path"

    return ActionCandidateSet(
        primitive=ActionPrimitive.INSERT_BRIDGE,
        candidates=candidates,
        empty_reason=empty_reason,
    )


def _enumerate_move_condition(
    *,
    case_view: RuntimeCaseView,
    group_id: str,
    group_type: GroupType,
    canonical_op: Dict[str, Any],
    repair_program: Sequence[Dict[str, Any]] = (),
) -> ActionCandidateSet:
    predicate_scope_delta = _canonical_signature_section(canonical_op, "predicate_scope_delta")
    current_predicates = _current_predicate_rows(case_view)
    if not current_predicates:
        return ActionCandidateSet(
            primitive=ActionPrimitive.MOVE_CONDITION,
            candidates=[],
            empty_reason="no_current_predicate_to_move",
        )
    removed = {
        str(item).strip().lower()
        for item in (predicate_scope_delta.get("removed_source_predicates") or [])
        if str(item).strip()
    }
    relevant_rows = [
        row for row in current_predicates if not removed or row["normalized"] in removed
    ]
    if not relevant_rows:
        return ActionCandidateSet(
            primitive=ActionPrimitive.MOVE_CONDITION,
            candidates=[],
            empty_reason="current_predicates_do_not_match_scope_move_evidence",
        )
    aggregate_exprs = _aggregate_select_exprs(case_view)
    grain_delta = _canonical_signature_section(canonical_op, "grain_delta")
    target_scope = "CASE_NUMERATOR" if (
        aggregate_exprs
        or bool(grain_delta.get("target_has_aggregate"))
        or bool(predicate_scope_delta.get("possible_scope_move"))
    ) else "HAVING"
    contract_repair_program = [dict(step) for step in repair_program]
    candidates: List[ActionCandidate] = []
    for row in relevant_rows:
        key = f"{row['scope']}|{row['index']}|{row['predicate']}"
        candidates.append(
            ActionCandidate(
                candidate_id=_hash_cand(f"MOVECOND|{group_id}|{key}"),
                arguments={
                    "predicate_ref": row["predicate"],
                    "predicate_index": row["index"],
                    "from_scope": row["scope"],
                    "to_scope": target_scope,
                    "target_aggregate_expr": aggregate_exprs[0] if aggregate_exprs else "",
                    "preserve_denominator_scope": True,
                    "repair_program": contract_repair_program,
                    "required_edit_scopes": _required_edit_scopes(
                        "WHERE" if row["scope"] == "WHERE" else row["scope"],
                        contract_repair_program,
                    ),
                },
                provenance=f"group={group_id};predicate_move[{row['scope']}:{row['index']}]",
                source_group_id=group_id,
                source_group_type=group_type,
            )
        )
    return ActionCandidateSet(
        primitive=ActionPrimitive.MOVE_CONDITION,
        candidates=candidates,
        empty_reason=None if candidates else "predicate_scope_move_not_bindable",
    )


def _enumerate_change_grain(
    *,
    case_view: RuntimeCaseView,
    group_id: str,
    group_type: GroupType,
    canonical_op: Dict[str, Any],
    repair_program: Sequence[Dict[str, Any]] = (),
) -> ActionCandidateSet:
    if not _runtime_binding_contract_for_action(canonical_op):
        return ActionCandidateSet(
            primitive=ActionPrimitive.CHANGE_GRAIN,
            candidates=[],
            empty_reason=WUV2_2_SEED_TARGET_BINDING_DISABLED_REASON,
        )
    grain_delta = _canonical_signature_section(canonical_op, "grain_delta")
    shape = _canonical_shape(canonical_op)
    source_grain = (
        grain_delta.get("source_grain")
        or shape.get("current_grain")
        or _current_output_shape_for_compiler(case_view).get("grain")
    )
    target_grain = grain_delta.get("target_grain") or shape.get("target_grain")
    source_has_distinct = bool(grain_delta.get("source_has_distinct"))
    target_has_distinct = bool(grain_delta.get("target_has_distinct"))
    target_has_aggregate = bool(grain_delta.get("target_has_aggregate"))
    if not (
        grain_delta.get("grain_changed")
        or (source_has_distinct != target_has_distinct)
        or target_has_aggregate != bool(grain_delta.get("source_has_aggregate"))
    ):
        return ActionCandidateSet(
            primitive=ActionPrimitive.CHANGE_GRAIN,
            candidates=[],
            empty_reason="no_grain_delta_for_change_grain",
        )
    aggregate_rewrite = "GROUP_KEY_CHANGE"
    if not source_has_distinct and target_has_distinct:
        aggregate_rewrite = "COUNT_TO_COUNT_DISTINCT"
    elif source_has_distinct and not target_has_distinct:
        aggregate_rewrite = "COUNT_DISTINCT_TO_COUNT"
    target_refs = _target_output_refs_for_action(canonical_op)
    current_keys = _selected_output_ref_keys(case_view)
    target_keys = _output_ref_keys(target_refs)
    if bool(_current_output_shape_for_compiler(case_view).get("has_aggregate")) and current_keys and target_keys and current_keys != target_keys:
        return ActionCandidateSet(
            primitive=ActionPrimitive.CHANGE_GRAIN,
            candidates=[],
            empty_reason="aggregate_answer_unit_mismatch",
        )
    target_anchor = (
        str(((target_refs[0] or {}).get("evidence") or {}).get("original_expression") or "")
        if target_refs
        else ""
    ) or _current_anchor_expr(case_view)
    contract_repair_program = [dict(step) for step in repair_program]
    return ActionCandidateSet(
        primitive=ActionPrimitive.CHANGE_GRAIN,
        candidates=[
            ActionCandidate(
                candidate_id=_hash_cand(f"GRAIN|{group_id}|{source_grain}->{target_grain}|{aggregate_rewrite}"),
                arguments={
                    "source_grain": source_grain,
                    "target_grain": target_grain,
                    "source_anchor": _current_anchor_expr(case_view),
                    "target_anchor": target_anchor,
                    "aggregate_rewrite": aggregate_rewrite,
                    "output_shape_delta": shape,
                    "repair_program": contract_repair_program,
                    "required_edit_scopes": _required_edit_scopes(
                        "SELECT",
                        contract_repair_program,
                    ),
                },
                provenance=f"group={group_id};grain_change:{source_grain}->{target_grain}",
                source_group_id=group_id,
                source_group_type=group_type,
            )
        ],
        empty_reason=None,
    )


def _enumerate_switch_canonical_field(
    *,
    case_view: RuntimeCaseView,
    skeleton: RepairSkeletonStructural,
    slots: Sequence[InstantiationSlot],
    group_id: str,
    group_type: GroupType,
    memory_text: str = "",
    repair_program: Sequence[Dict[str, Any]] = (),
    preferred_target_refs: Sequence[Dict[str, Any]] = (),
) -> ActionCandidateSet:
    shape = _shape_delta_payload(skeleton)
    if str(shape.get("arity_direction") or "").lower() not in {"", "same"}:
        return ActionCandidateSet(
            primitive=ActionPrimitive.SWITCH_CANONICAL_FIELD,
            candidates=[],
            empty_reason="canonical_field_switch_requires_same_output_arity",
        )
    replace_set = _enumerate_replace_select_slot(
        case_view=case_view,
        skeleton=skeleton,
        slots=slots,
        group_id=group_id,
        group_type=group_type,
        memory_text=memory_text,
        repair_program=repair_program,
        preferred_target_refs=preferred_target_refs,
        strict_preferred_targets=bool(preferred_target_refs),
    )
    converted: List[ActionCandidate] = []
    for candidate in replace_set.candidates or []:
        args = dict(candidate.arguments or {})
        source_exprs = list(args.get("from_exprs") or [])
        target_columns = list(args.get("target_columns") or [])
        if not source_exprs or not target_columns:
            continue
        target_expr = ",".join(
            f"{item.get('target_table')}.{item.get('target_column')}"
            for item in target_columns
            if item.get("target_column")
        )
        args.update(
            {
                "current_expr": source_exprs[0],
                "target_expr": target_expr,
                "preserve_join_path": True,
            }
        )
        converted.append(
            candidate.model_copy(update={"arguments": args})
        )
    return ActionCandidateSet(
        primitive=ActionPrimitive.SWITCH_CANONICAL_FIELD,
        candidates=converted,
        empty_reason=replace_set.empty_reason if not converted else None,
    )


def _enumerate_materialize_ranking_output(
    *,
    case_view: RuntimeCaseView,
    group_id: str,
    group_type: GroupType,
    canonical_op: Dict[str, Any],
    member_variants: Sequence[Dict[str, Any]],
    repair_program: Sequence[Dict[str, Any]] = (),
) -> ActionCandidateSet:
    if not _runtime_binding_contract_for_action(canonical_op):
        return ActionCandidateSet(
            primitive=ActionPrimitive.MATERIALIZE_RANKING_OUTPUT,
            candidates=[],
            empty_reason=WUV2_2_SEED_TARGET_BINDING_DISABLED_REASON,
        )
    policies = [
        policy
        for policy in _canonical_accessory_policies(
            canonical_op,
            member_variants=member_variants,
        )
        if str(policy.get("op") or "").upper() in {"ORDER_BY_APPLY", "LIMIT_APPLY"}
    ]
    if not policies:
        return ActionCandidateSet(
            primitive=ActionPrimitive.MATERIALIZE_RANKING_OUTPUT,
            candidates=[],
            empty_reason="no_ranking_accessory_policy",
        )
    target_order = []
    target_limit = None
    for policy in policies:
        if policy.get("target_order_by"):
            target_order = list(policy.get("target_order_by") or [])
        if policy.get("target_limit") is not None:
            target_limit = policy.get("target_limit")
    ranking_expr = ""
    if target_order:
        ranking_expr = str(_payload(target_order[0]).get("expression") or "")
    metric_expr = ""
    target_refs = _target_output_refs_for_action(
        canonical_op,
        member_variants=member_variants,
    )
    if target_refs:
        metric_expr = str(_payload(target_refs[0]).get("expression") or "")
    if not metric_expr:
        aggregate_exprs = _aggregate_select_exprs(case_view)
        metric_expr = aggregate_exprs[0] if aggregate_exprs else ""
    tie_policy = "single_top" if str(target_limit or "") == "1" else "preserve_ties"
    window_fn = "ROW_NUMBER" if tie_policy == "single_top" else "RANK"
    contract_repair_program = [dict(step) for step in repair_program]
    return ActionCandidateSet(
        primitive=ActionPrimitive.MATERIALIZE_RANKING_OUTPUT,
        candidates=[
            ActionCandidate(
                candidate_id=_hash_cand(f"RANKOUT|{group_id}|{ranking_expr}|{metric_expr}|{target_limit}"),
                arguments={
                    "ranking_expr": ranking_expr,
                    "metric_expr": metric_expr,
                    "window_fn": window_fn,
                    "tie_policy": tie_policy,
                    "target_order_by": target_order[:4],
                    "target_limit": target_limit,
                    "repair_program": contract_repair_program,
                    "required_edit_scopes": _required_edit_scopes(
                        "SELECT",
                        contract_repair_program + [{"locus": "ORDER_BY"}, {"locus": "LIMIT"}],
                    ),
                },
                provenance=f"group={group_id};ranking_contract",
                source_group_id=group_id,
                source_group_type=group_type,
            )
        ],
        empty_reason=None,
    )


_PRIMITIVE_ORDER = [
    ActionPrimitive.ADD_SELECT_SLOT,
    ActionPrimitive.REPLACE_SELECT_SLOT,
    ActionPrimitive.DROP_SELECT_SLOT,
    ActionPrimitive.DROP_SIDE,
    ActionPrimitive.INSERT_BRIDGE,
    ActionPrimitive.REROUTE_FACT,
    ActionPrimitive.CHANGE_GRAIN,
    ActionPrimitive.MOVE_CONDITION,
    ActionPrimitive.SWITCH_CANONICAL_FIELD,
    ActionPrimitive.MATERIALIZE_RANKING_OUTPUT,
]


_IMPLEMENTED = {
    ActionPrimitive.ADD_SELECT_SLOT: _enumerate_add_select_slot,
    ActionPrimitive.REPLACE_SELECT_SLOT: _enumerate_replace_select_slot,
    ActionPrimitive.DROP_SELECT_SLOT: _enumerate_drop_select_slot,
    ActionPrimitive.DROP_SIDE: _enumerate_drop_side,
    ActionPrimitive.INSERT_BRIDGE: _enumerate_insert_bridge,
    ActionPrimitive.REROUTE_FACT: _enumerate_reroute_fact,
    ActionPrimitive.CHANGE_GRAIN: _enumerate_change_grain,
    ActionPrimitive.MOVE_CONDITION: _enumerate_move_condition,
    ActionPrimitive.SWITCH_CANONICAL_FIELD: _enumerate_switch_canonical_field,
    ActionPrimitive.MATERIALIZE_RANKING_OUTPUT: _enumerate_materialize_ranking_output,
}


def _merge_candidate_set(
    target: ActionCandidateSet,
    incoming: ActionCandidateSet,
) -> ActionCandidateSet:
    candidates = list(target.candidates or []) + list(incoming.candidates or [])
    reasons = [
        reason
        for reason in (target.empty_reason, incoming.empty_reason)
        if reason and not candidates
    ]
    return target.model_copy(
        update={
            "candidates": candidates,
            "empty_reason": "; ".join(reasons) if reasons else None,
        }
    )


def _enumerate_for_canonical_op(
    *,
    case_view: RuntimeCaseView,
    group: GroupSummary,
    canonical_op: Dict[str, Any],
    repair_program: Sequence[Dict[str, Any]],
    memory_text: str,
) -> List[ActionCandidateSet]:
    slots = group.instantiation_program.slots
    op_type = str(canonical_op.get("op_type") or "")
    matched_variants = _matching_member_variants_for_current(
        canonical_op=canonical_op,
        case_view=case_view,
    )
    lowering_family = _lowering_family_for_current(
        canonical_op=canonical_op,
        matched_variants=matched_variants,
    )
    # Scoped runtime branches may narrow a select_replace canonical program to a
    # drop-only executable branch. Honor that branch contract before lowering.
    allowed = _branch_allowed_primitives(group)
    if allowed:
        drop_primitives = {ActionPrimitive.DROP_SIDE, ActionPrimitive.DROP_SELECT_SLOT}
        if allowed & drop_primitives and lowering_family == "select_replace":
            lowering_family = "select_drop"
    skeleton = _canonical_base_skeleton(
        canonical_op=canonical_op,
        lowering_family=lowering_family,
    )
    if (
        _requires_current_variant_binding(canonical_op)
        and _canonical_member_variants(canonical_op)
        and not matched_variants
        and lowering_family != "join_bridge"
    ):
        return [
            ActionCandidateSet(
                primitive=_primitive_for_lowering_family(lowering_family),
                candidates=[],
                empty_reason="current_output_shape_unmatched_to_program_variants",
            )
        ]
    if lowering_family == "select_drop":
        drop_side_skeleton = skeleton.model_copy(
            update={
                "locus": Locus.SELECT,
                "op_family": OpFamily.DROP,
                "target_family": TargetFamily.SHAPE,
            }
        )
        drop_select_skeleton = skeleton.model_copy(
            update={
                "locus": Locus.SELECT,
                "op_family": OpFamily.DROP,
                "target_family": TargetFamily.SLOT,
            }
        )
        sets = [
            _enumerate_drop_side(
                case_view=case_view,
                skeleton=drop_side_skeleton,
                slots=slots,
                group_id=group.group_id,
                group_type=group.group_type,
                memory_text=memory_text,
                repair_program=repair_program,
            ),
            _enumerate_drop_select_slot(
                case_view=case_view,
                skeleton=drop_select_skeleton,
                slots=slots,
                group_id=group.group_id,
                group_type=group.group_type,
                repair_program=repair_program,
            ),
        ]
        pred_exprs = case_view.pred_manifestation.columns or []
        sets = [
            _narrow_select_drop_candidates(
                candidate_set=candidate_set,
                canonical_op=canonical_op,
                pred_exprs=pred_exprs,
            )
            for candidate_set in sets
        ]
    elif lowering_family == "where_side_edit":
        sets = [
            _enumerate_drop_side(
                case_view=case_view,
                skeleton=skeleton,
                slots=slots,
                group_id=group.group_id,
                group_type=group.group_type,
                memory_text=memory_text,
                repair_program=repair_program,
            ),
            _enumerate_move_condition(
                case_view=case_view,
                group_id=group.group_id,
                group_type=group.group_type,
                canonical_op=canonical_op,
                repair_program=repair_program,
            ),
        ]
    elif lowering_family == "join_bridge":
        bridge_skeleton = skeleton.model_copy(
            update={
                "locus": Locus.BRIDGE,
                "op_family": OpFamily.BRIDGE,
                "target_family": TargetFamily.BRIDGE_PATH,
            }
        )
        reroute_set = _enumerate_reroute_fact(
            case_view=case_view,
            group_id=group.group_id,
            group_type=group.group_type,
            member_variants=matched_variants,
            canonical_op=canonical_op,
            repair_program=repair_program,
        )
        if reroute_set.candidates:
            sets = [reroute_set]
        else:
            sets = [
                _enumerate_insert_bridge(
                    case_view=case_view,
                    skeleton=bridge_skeleton,
                    slots=slots,
                    group_id=group.group_id,
                    group_type=group.group_type,
                    repair_program=repair_program,
                ),
            ]
    elif lowering_family == "select_add":
        target_output_refs = _target_output_refs_for_action(
            canonical_op,
            member_variants=matched_variants,
        )
        replacement_refs, add_refs = _partition_target_output_refs_by_current_projection(
            case_view=case_view,
            target_refs=target_output_refs,
        )
        replace_skeleton = skeleton.model_copy(
            update={
                "locus": Locus.SELECT,
                "op_family": OpFamily.REPLACE,
                "target_family": TargetFamily.SLOT,
            }
        )
        sets = [
            _enumerate_add_select_slot(
                case_view=case_view,
                skeleton=skeleton,
                slots=slots,
                group_id=group.group_id,
                group_type=group.group_type,
                memory_text=memory_text,
                repair_program=repair_program,
                preferred_target_refs=add_refs or target_output_refs,
                strict_preferred_targets=bool(add_refs),
            )
        ]
        if replacement_refs:
            sets.append(
                _enumerate_replace_select_slot(
                    case_view=case_view,
                    skeleton=replace_skeleton,
                    slots=slots,
                    group_id=group.group_id,
                    group_type=group.group_type,
                    memory_text=memory_text,
                    repair_program=repair_program,
                    preferred_target_refs=replacement_refs,
                    strict_preferred_targets=True,
                )
            )
        reroute_set = _enumerate_reroute_fact(
            case_view=case_view,
            group_id=group.group_id,
            group_type=group.group_type,
            member_variants=matched_variants,
            canonical_op=canonical_op,
            repair_program=repair_program,
        )
        if reroute_set.candidates:
            sets.append(reroute_set)
        change_grain_set = _enumerate_change_grain(
            case_view=case_view,
            group_id=group.group_id,
            group_type=group.group_type,
            canonical_op=canonical_op,
            repair_program=repair_program,
        )
        if change_grain_set.candidates:
            sets.append(change_grain_set)
        ranking_set = _enumerate_materialize_ranking_output(
            case_view=case_view,
            group_id=group.group_id,
            group_type=group.group_type,
            canonical_op=canonical_op,
            member_variants=matched_variants,
            repair_program=repair_program,
        )
        if ranking_set.candidates:
            sets.append(ranking_set)
    elif lowering_family == "select_replace":
        if _canonical_op_preserves_answer_unit_under_reroute(canonical_op):
            reroute_set = _enumerate_reroute_fact(
                case_view=case_view,
                group_id=group.group_id,
                group_type=group.group_type,
                member_variants=matched_variants,
                canonical_op=canonical_op,
                repair_program=repair_program,
            )
            if reroute_set.candidates:
                sets = [reroute_set]
            else:
                sets = []
        else:
            sets = []
        preferred_refs = _target_output_refs_for_action(
            canonical_op,
            member_variants=matched_variants,
        )
        if not sets:
            sets = [
                _enumerate_replace_select_slot(
                    case_view=case_view,
                    skeleton=skeleton,
                    slots=slots,
                    group_id=group.group_id,
                    group_type=group.group_type,
                    memory_text=memory_text,
                    repair_program=repair_program,
                    preferred_target_refs=preferred_refs,
                    strict_preferred_targets=bool(preferred_refs),
                )
            ]
            switch_set = _enumerate_switch_canonical_field(
                case_view=case_view,
                skeleton=skeleton,
                slots=slots,
                group_id=group.group_id,
                group_type=group.group_type,
                memory_text=memory_text,
                repair_program=repair_program,
                preferred_target_refs=preferred_refs,
            )
            if switch_set.candidates:
                sets.append(switch_set)
        change_grain_set = _enumerate_change_grain(
            case_view=case_view,
            group_id=group.group_id,
            group_type=group.group_type,
            canonical_op=canonical_op,
            repair_program=repair_program,
        )
        if change_grain_set.candidates:
            sets.append(change_grain_set)
        ranking_set = _enumerate_materialize_ranking_output(
            case_view=case_view,
            group_id=group.group_id,
            group_type=group.group_type,
            canonical_op=canonical_op,
            member_variants=matched_variants,
            repair_program=repair_program,
        )
        if ranking_set.candidates:
            sets.append(ranking_set)
        reroute_set = _enumerate_reroute_fact(
            case_view=case_view,
            group_id=group.group_id,
            group_type=group.group_type,
            member_variants=matched_variants,
            canonical_op=canonical_op,
            repair_program=repair_program,
        )
        if reroute_set.candidates:
            existing_reroute = any(cs.primitive == ActionPrimitive.REROUTE_FACT for cs in sets)
            if not existing_reroute:
                sets.append(reroute_set)
    else:
        return [
            ActionCandidateSet(
                primitive=ActionPrimitive.ADD_SELECT_SLOT,
                candidates=[],
                empty_reason=f"unsupported canonical op for compiler lowering: {op_type}",
            )
        ]
    return [
        _annotate_canonical_candidates(
            candidate_set=candidate_set,
            case_view=case_view,
            group=group,
            canonical_op=canonical_op,
            member_variants=matched_variants,
        )
        for candidate_set in sets
    ]


def _branch_allowed_primitives(group: GroupSummary) -> Optional[set[ActionPrimitive]]:
    """Read branch allowed_primitives from the scoped group's action_envelope."""
    program = getattr(group.instantiation_program, "synthesized_program", None)
    if program is None:
        return None
    envelope = _payload(getattr(program, "program_envelope", None))
    action_envelope = _payload(envelope.get("action_envelope"))
    raw = action_envelope.get("allowed_primitives")
    if not raw:
        return None
    allowed: set[ActionPrimitive] = set()
    for item in raw:
        text = str(item).strip()
        try:
            allowed.add(ActionPrimitive(text))
        except ValueError:
            continue
    return allowed if allowed else None


def _enumerate_for_group(
    *,
    case_view: RuntimeCaseView,
    group: GroupSummary,
) -> List[ActionCandidateSet]:
    """单个 Group 的 per-primitive 枚举。"""
    mem_text = _memory_text(group)
    repair_program = _compact_repair_program_for_action(_repair_program_from_group(group))
    canonical_ops = _canonical_ops_from_group(group)
    if canonical_ops:
        merged: Dict[ActionPrimitive, ActionCandidateSet] = {
            primitive: ActionCandidateSet(
                primitive=primitive,
                candidates=[],
                empty_reason="not required by synthesized canonical program",
            )
            for primitive in _PRIMITIVE_ORDER
        }
        for canonical_op in canonical_ops:
            lowered_sets = _enumerate_for_canonical_op(
                case_view=case_view,
                group=group,
                canonical_op=canonical_op,
                repair_program=repair_program,
                memory_text=mem_text,
            )
            for candidate_set in lowered_sets:
                merged[candidate_set.primitive] = _merge_candidate_set(
                    merged[candidate_set.primitive],
                    candidate_set,
                )
        allowed = _branch_allowed_primitives(group)
        if allowed:
            for primitive in list(merged.keys()):
                if primitive not in allowed and merged[primitive].candidates:
                    merged[primitive] = ActionCandidateSet(
                        primitive=primitive,
                        candidates=[],
                        empty_reason=(
                            f"excluded_by_branch_allowed_primitives:{primitive.value}"
                        ),
                    )
        return list(merged.values())

    return [
        ActionCandidateSet(
            primitive=primitive,
            candidates=[],
            empty_reason="missing_synthesized_canonical_program",
        )
        for primitive in _PRIMITIVE_ORDER
    ]


def enumerate_candidates(
    *,
    case_view: RuntimeCaseView,
    memory_objects: List[GroupSummary],
) -> tuple[List[ActionCandidateSet], LocalSchemaViewDiagnostics]:
    """对 case_view + 匹配的 GroupSummary 列表，产出 per-primitive candidate 集合。

    多 Group 命中时：同 primitive 的 candidates 合并到一个 ActionCandidateSet 里，
    每个 candidate 带 source_group_id 区分。Compiler LLM 会综合全部 candidates
    做 selection，每个 action 的 source_group_id 自动从 selected candidate 带入。

    返回 (candidate_sets, schema_diagnostics)。
    """
    merged: Dict[ActionPrimitive, ActionCandidateSet] = {}
    for primitive in _PRIMITIVE_ORDER:
        merged[primitive] = ActionCandidateSet(
            primitive=primitive, candidates=[], empty_reason=None
        )

    # 若无 memory 命中，直接返回全空集合（runtime 会 skip compiler）
    if not memory_objects:
        for primitive in _PRIMITIVE_ORDER:
            merged[primitive] = ActionCandidateSet(
                primitive=primitive,
                candidates=[],
                empty_reason="no memory object matched for this case",
            )
        diag = LocalSchemaViewDiagnostics()
        return list(merged.values()), diag

    # 每个 Group 独立做枚举，然后合并
    per_group_empty_reasons: Dict[ActionPrimitive, List[str]] = {
        p: [] for p in _PRIMITIVE_ORDER
    }
    for group in memory_objects:
        group_sets = _enumerate_for_group(case_view=case_view, group=group)
        for cs in group_sets:
            if cs.candidates:
                merged[cs.primitive].candidates.extend(cs.candidates)
            elif cs.empty_reason:
                per_group_empty_reasons[cs.primitive].append(
                    f"[{group.group_id}] {cs.empty_reason}"
                )

    # 对合并后仍无 candidates 的 primitive，聚合 empty_reason
    for primitive, cs in merged.items():
        if not cs.candidates:
            reasons = per_group_empty_reasons.get(primitive) or []
            cs.empty_reason = "; ".join(reasons) if reasons else "no candidates"

    # Schema recall diagnostics
    diag = LocalSchemaViewDiagnostics()
    for cs in merged.values():
        if cs.empty_reason and "schema recall miss" in cs.empty_reason.lower():
            diag.missing_column_candidates.append(cs.primitive.value)
        elif cs.empty_reason and SCHEMA_RECALL_MISS_CATEGORIES[0] in cs.empty_reason:
            diag.missing_bridge_paths.append(cs.primitive.value)

    return list(merged.values()), diag


__all__ = ["enumerate_candidates"]
