"""runtime chain —— answer-blind library retrieval → Compiler → Rewrite.

与 case_pipeline 的开发期流程（含 Auditor / Extractor，看 gold）严格分离。
runtime 函数绝不接收 ``gold_sql``。

组件：
- ``build_runtime_case_view``：answer-blind，从 question+evidence+pred_top1+schema 构 view
- ``retrieve_groups``：从 library 按 db_id + trigger_signature 匹配 GroupSummary
- ``run_memory_rewrite_runtime``：端到端 runtime entry。retrieval → Compiler → Rewrite → C1

接入 DeepEye 的 memory_rewrite 管线时，``run_memory_rewrite_runtime`` 是唯一对外函数。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from method.EEA.rulebook.common.runtime.action_compiler import enumerate_candidates
from method.EEA.rulebook.common.analysis.code_preprocess import (
    build_case_signal_bundle,
    build_case_signal_view,
    compute_structure_bundle,
    extract_candidate_pred_tags,
    extract_candidate_question_tags,
    extract_tables_from_pred,
    extract_tables_from_question,
)
from method.EEA.rulebook.common.core.data_structures import (
    ActionCompilerOutput,
    CandidateSetSummary,
    GroupSummary,
    LibraryStateV2,
    LocalSchemaView,
    PredManifestation,
    QuestionContract,
    RuntimeCaseView,
    TriggerCandidateAudit,
    TriggerResult,
)
from method.EEA.rulebook.common.io.db_schema_access import SqliteDBSchemaAccess
from method.EEA.rulebook.common.llm.nodes import run_action_compiler, run_hint_instantiation, run_memory_rewrite
from method.EEA.rulebook.common.io.local_schema import build_local_schema_view
from method.EEA.rulebook.common.analysis.role_graph_normalizer import RoleGraphNormalizer
from method.EEA.rulebook.common.runtime.trigger_contract import is_contract_runtime_executable, sanitize_trigger_contract
from method.EEA.rulebook.common.core.vocabulary import (
    AnswerSlotType,
    BIAS_RECOGNITION_SIGNAL_VOCABULARY,
    EditScope,
    GrainType,
    GroupType,
    OpType,
    PredManifestationType,
    QuestionTargetRole,
    RouteCue,
)


_EDIT_SCOPE_ORDER = [
    "SELECT",
    "FROM",
    "JOIN",
    "WHERE",
    "GROUP_BY",
    "HAVING",
    "ORDER_BY",
    "LIMIT",
    "SUBQUERY",
]
_BINDER_DRY_RUN_CACHE: Dict[str, Tuple[List[Tuple[str, Any]], str]] = {}
_TWO_STAGE_PATTERN_TRIGGER_ENABLED = (
    os.environ.get("EEA_PATTERN_TWO_STAGE_TRIGGER", "1").strip().lower()
    not in {"0", "false", "no", "off"}
)


# =============================================================================
# Runtime Case View 构造（answer-blind）
# =============================================================================


def _tags_as_enum(tags: List[str], enum_cls) -> List:
    values = {m.value for m in enum_cls}
    out = []
    for t in tags:
        if t in values:
            try:
                out.append(enum_cls(t))
            except Exception:
                pass
    return out


def _candidate_sql_text(value: Any) -> str:
    """Extract SQL text from either legacy strings or contract-shaped entries."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("sql") or "").strip()
    return str(getattr(value, "sql", "") or "").strip()


def _candidate_sql_texts(candidates: Optional[Sequence[Any]]) -> List[str]:
    out: List[str] = []
    for candidate in candidates or []:
        sql = _candidate_sql_text(candidate)
        if sql:
            out.append(sql)
    return out


def build_runtime_case_view(
    *,
    db_id: str,
    case_id: str,
    question: str,
    evidence: str,
    pred_top1_sql: str,
    access: SqliteDBSchemaAccess,
    c0_candidate_sqls: Optional[Sequence[Any]] = None,
    t_mem: Optional[List[str]] = None,
    allow_two_hop_justifications: Optional[List[str]] = None,
    candidate_set_size: int = 1,
) -> RuntimeCaseView:
    """Answer-blind runtime case view。

    不看 gold、不跑 execution comparison、不调 Auditor/Extractor。只用 code 侧的
    轻量启发式从 pred_top1 + question + schema 构造 view。

    参数
    ----
    t_mem :
        若已完成 library retrieval（命中的 Group schema hints），这里传 memory
        objects 关注的表。没有就空。
    """
    from method.EEA.rulebook.common.analysis.structure_family import cached_ast_signature

    pred_ast = cached_ast_signature(pred_top1_sql) or {}
    c0_sqls: List[str] = []
    for sql in [pred_top1_sql, *_candidate_sql_texts(c0_candidate_sqls)]:
        text = _candidate_sql_text(sql)
        if text and text not in c0_sqls:
            c0_sqls.append(text)

    t_pred = extract_tables_from_pred(pred_ast)
    for candidate_sql in c0_sqls[1:5]:
        candidate_ast = cached_ast_signature(candidate_sql) or {}
        t_pred.extend(extract_tables_from_pred(candidate_ast))
    t_pred = list(dict.fromkeys(t_pred))

    def _candidate_sql_summary(rank: int, sql: str) -> Dict[str, Any]:
        ast = cached_ast_signature(sql) or {}
        shape = ast.get("select_shape") or {}
        return {
            "rank": rank,
            "sql_hash": hashlib.sha1(str(sql or "").encode("utf-8")).hexdigest()[:16],
            "is_top1": rank == 0,
            "tables": list(ast.get("tables_involved") or [])[:12],
            "select_expressions": [
                str(expr)[:160] for expr in (ast.get("select_expressions") or [])[:8]
            ],
            "select_shape": {
                "output_arity": shape.get("output_arity"),
                "has_distinct": bool(shape.get("has_distinct")),
                "has_aggregate": bool(shape.get("has_aggregate")),
            },
        }
    all_tables = list(access.list_tables())
    t_q = extract_tables_from_question(
        question=question, evidence=evidence, all_tables=all_tables
    )
    t_mem_list = list(t_mem or [])

    view, _diag = build_local_schema_view(
        db_id=db_id,
        access=access,
        t_pred=t_pred,
        t_q=t_q,
        t_mem=t_mem_list,
        allow_two_hop_justifications=allow_two_hop_justifications,
    )

    # 候选 tags —— runtime 无 gold，用 runtime_mode=True 只 emit pred-only 可见的 tag
    cand_q_tags = extract_candidate_question_tags(question=question, evidence=evidence)
    cand_p_tags = extract_candidate_pred_tags(
        pred_ast=pred_ast,
        gold_ast={},
        structure_flags={},
        execution_comparison=None,
        runtime_mode=True,
    )

    # question_contract 分桶
    target_roles = _tags_as_enum(cand_q_tags, QuestionTargetRole) or [
        QuestionTargetRole.PRIMARY_ENTITY
    ]
    slot_types = _tags_as_enum(cand_q_tags, AnswerSlotType)
    operators = _tags_as_enum(cand_q_tags, OpType)
    route_cues = _tags_as_enum(cand_q_tags, RouteCue)
    grains = _tags_as_enum(cand_q_tags, GrainType)
    grain = grains[0] if grains else None

    question_contract = QuestionContract(
        target_entity_roles=target_roles,
        answer_slot_types=slot_types,
        grain=grain,
        operators=operators,
        route_cues=route_cues,
        specific_entity_mentions=[],
        summary=None,
    )

    # pred_manifestation
    pred_tables = list(pred_ast.get("tables_involved") or [])
    pred_cols: List[str] = [str(e) for e in (pred_ast.get("select_expressions") or [])]
    manifestation_types = _tags_as_enum(cand_p_tags, PredManifestationType)

    shape_bits: List[str] = []
    shape = pred_ast.get("select_shape") or {}
    if shape.get("output_arity") is not None:
        shape_bits.append(f"arity={shape['output_arity']}")
    if shape.get("has_distinct"):
        shape_bits.append("distinct")
    if shape.get("has_aggregate"):
        shape_bits.append("aggregate")

    pred_manifestation = PredManifestation(
        top1_sql=pred_top1_sql[:2000],
        tables=pred_tables,
        columns=pred_cols,
        select_shape=",".join(shape_bits) or None,
        group_order_shape=None,
        manifestation_types=manifestation_types,
        structure_flags={},  # runtime 无 gold；pred-only flags 可留空
        summary=None,
    )
    case_signal_view = build_case_signal_view(
        db_id=db_id,
        case_id=case_id,
        question=question,
        evidence=evidence,
        pred_sql=pred_top1_sql,
        pred_ast=pred_ast,
        local_schema_view=view,
        local_schema_diagnostics=_diag,
        candidate_question_tags=cand_q_tags,
        candidate_pred_tags=cand_p_tags,
        structure_flags={},
    )
    case_signal_bundle = build_case_signal_bundle(
        db_id=db_id,
        case_id=case_id,
        case_signal_view=case_signal_view,
        delta_signature=None,
        candidate_question_tags=cand_q_tags,
        candidate_pred_tags=cand_p_tags,
        execution_comparison=None,
        legacy_signature="",
        legacy_signature_dict={},
    )

    view_obj = RuntimeCaseView(
        db_id=db_id,
        case_id=case_id,
        question=question,
        evidence=evidence or None,
        question_contract=question_contract,
        pred_manifestation=pred_manifestation,
        candidate_set_summary=CandidateSetSummary(
            size=max(candidate_set_size, len(c0_sqls) or 1),
            top1_hash=hashlib.sha1(str(pred_top1_sql or "").encode("utf-8")).hexdigest()[:16],
            beam_present=len(c0_sqls) > 1,
            candidate_summaries=[
                _candidate_sql_summary(rank, sql) for rank, sql in enumerate(c0_sqls[:5])
            ],
        ),
        local_schema_view=view,
        case_signal_view=case_signal_view,
        case_signal_bundle=case_signal_bundle,
    )
    return view_obj.model_copy(
        update={"bias_recognition_signals": compute_bias_recognition_signals(view_obj)}
    )


def _memory_schema_tables_from_value(value: Any) -> List[str]:
    """Extract memory-backed schema table hints from canonical evidence.

    This is used after trigger selection only. It treats table names embedded in
    the matched memory object's canonical target refs/invariants as schema hints
    for LocalSchemaView, not as runtime trigger keys.
    """
    tables: List[str] = []

    def add_table(raw: Any) -> None:
        name = str(raw or "").strip().strip('"`[]')
        if not name:
            return
        if "." in name:
            name = name.split(".", 1)[0].strip('"`[]')
        if name and name.lower() not in {"select", "from", "where", "join"}:
            tables.append(name)

    def walk(obj: Any) -> None:
        payload = _payload(obj)
        if payload:
            source = str(payload.get("source") or "")
            if source == "target_sql":
                add_table(payload.get("table"))
            table_delta = _payload(payload.get("table_set_delta"))
            for table in table_delta.get("gold_only") or []:
                add_table(table)
            for relation in payload.get("target_equality_relations") or []:
                relation_payload = _payload(relation)
                for side in ("left", "right"):
                    add_table(_payload(relation_payload.get(side)).get("table"))
            for key in (
                "canonical_refs",
                "role_refs",
                "target_output_refs",
                "member_argument_variants",
                "ops",
                "repair_program",
            ):
                for item in payload.get(key) or []:
                    walk(item)
            for invariant in payload.get("target_invariants") or payload.get("canonical_target_invariants") or []:
                for match in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\.", str(invariant)):
                    add_table(match)
            for key in (
                "action_contract",
                "arguments",
                "canonical_arguments",
                "shared_arguments",
                "synthesized_program",
                "trigger_contract",
            ):
                if key in payload:
                    walk(payload.get(key))
            return
        if isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(value)
    seen: set[str] = set()
    out: List[str] = []
    for table in tables:
        key = table.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(table)
    return out


def _memory_schema_tables(groups: Sequence[GroupSummary]) -> List[str]:
    tables: List[str] = []
    for group in groups:
        tables.extend(_memory_schema_tables_from_value(getattr(group, "trigger_contract", None)))
        tables.extend(_memory_schema_tables_from_value(getattr(group, "instantiation_program", None)))
        tables.extend(_memory_schema_tables_from_value(getattr(group, "formation_signals", None)))
    seen: set[str] = set()
    out: List[str] = []
    for table in tables:
        key = table.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(table)
    return out


# =============================================================================
# Runtime Trigger（plan.md §9.2 / §10.4）
# =============================================================================


def _tag_overlap_score(required: List[str], case_tags: List[str]) -> float:
    """Required-tag recall. Empty required tags are not a positive signal."""
    if not required:
        return 0.0
    req_set = {r.lower() for r in required}
    case_set = {c.lower() for c in case_tags}
    hit = len(req_set & case_set)
    return hit / max(len(req_set), 1)


def _case_question_tags(case_view: RuntimeCaseView) -> List[str]:
    tags: List[str] = []
    for role in case_view.question_contract.target_entity_roles:
        tags.append(role.value)
    for st in case_view.question_contract.answer_slot_types:
        tags.append(st.value)
    for op in case_view.question_contract.operators:
        tags.append(op.value)
    for rc in case_view.question_contract.route_cues:
        tags.append(rc.value)
    if case_view.question_contract.grain:
        tags.append(case_view.question_contract.grain.value)
    return tags


def _case_pred_tags(case_view: RuntimeCaseView) -> List[str]:
    return [m.value for m in case_view.pred_manifestation.manifestation_types]


def _question_contract_signals(case_view: RuntimeCaseView) -> Set[str]:
    signals: Set[str] = set()
    for role in case_view.question_contract.target_entity_roles:
        signals.add(f"question.role={role.value}")
    for slot_type in case_view.question_contract.answer_slot_types:
        signals.add(f"question.slot={slot_type.value}")
    for operator in case_view.question_contract.operators:
        signals.add(f"question.operator={operator.value}")
    for route in case_view.question_contract.route_cues:
        signals.add(f"question.route={route.value}")
    if case_view.question_contract.grain:
        signals.add(f"question.grain={case_view.question_contract.grain.value}")
    return signals


def _payload(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    if isinstance(value, dict):
        return dict(value)
    return {}


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _case_signal_parts(case_view: RuntimeCaseView) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    signal_view = getattr(case_view, "case_signal_view", None)
    pred_sql_view = _payload(getattr(signal_view, "pred_sql_view", None))
    schema_view = _payload(getattr(signal_view, "schema_view", None))
    runtime_signature = _payload(getattr(signal_view, "runtime_signature", None))
    return pred_sql_view, schema_view, runtime_signature


def _case_output_shape(case_view: RuntimeCaseView) -> Dict[str, Any]:
    pred_sql_view, _, _ = _case_signal_parts(case_view)
    shape = pred_sql_view.get("output_shape_current") or {}
    if shape:
        return dict(shape)
    # Backward fallback for older RuntimeCaseView objects.
    out: Dict[str, Any] = {}
    select_shape = case_view.pred_manifestation.select_shape or ""
    for part in select_shape.split(","):
        part = part.strip()
        if part.startswith("arity="):
            out["arity"] = _int_or_zero(part.split("=", 1)[1])
        elif part == "distinct":
            out["has_distinct"] = True
        elif part == "aggregate":
            out["has_aggregate"] = True
    return out


def _bucket_count(value: Any) -> str:
    count = _int_or_zero(value)
    if count <= 0:
        return "0"
    if count == 1:
        return "1"
    if count == 2:
        return "2"
    return "3plus"


def _case_pred_current_summary(case_view: RuntimeCaseView) -> Dict[str, Any]:
    pred_sql_view, _, _ = _case_signal_parts(case_view)
    output_shape = _case_output_shape(case_view)
    predicate_profile = _payload(pred_sql_view.get("predicate_profile"))
    aggregate_profile = _payload(pred_sql_view.get("aggregate_profile"))
    group_order_profile = _payload(pred_sql_view.get("group_order_profile"))
    roles = list(output_shape.get("roles") or pred_sql_view.get("select_role_profile") or [])
    return {
        "select_arity": _int_or_zero(pred_sql_view.get("select_arity") or output_shape.get("arity")),
        "output_shape_current": {
            "arity": _int_or_zero(output_shape.get("arity") or pred_sql_view.get("select_arity")),
            "roles": roles,
            "grain": output_shape.get("grain"),
        },
        "join_count_bucket": _bucket_count(len(pred_sql_view.get("join_graph") or [])),
        "table_count_bucket": _bucket_count(len(pred_sql_view.get("tables_used") or [])),
        "predicate_count_bucket": _bucket_count(predicate_profile.get("predicate_count")),
        "predicate_literal_count_bucket": _bucket_count(predicate_profile.get("literal_count")),
        "has_or_predicate": bool(predicate_profile.get("has_or")),
        "has_negation_predicate": bool(predicate_profile.get("has_negation")),
        "has_aggregate": bool(aggregate_profile.get("has_aggregate")),
        "has_distinct_aggregate": bool(aggregate_profile.get("has_distinct_aggregate")),
        "has_case_when": bool(aggregate_profile.get("has_case_when")),
        "has_group_by": bool(group_order_profile.get("group_by_count")),
        "has_order_by": bool(group_order_profile.get("order_by_count")),
        "has_limit": group_order_profile.get("limit") is not None,
    }


def compute_bias_recognition_signals(case_view: RuntimeCaseView) -> Dict[str, bool]:
    """Compute closed-vocabulary phenomenon signals for pattern recognition.

    These signals are intentionally coarse and answer-blind. They are used only
    to decide whether a pattern's root bias may apply; branch binding and
    compiler dry-run remain the strict instantiation gate.
    """
    current = _case_pred_current_summary(case_view)
    current_signals = build_current_case_signals(case_view)
    pred_sql_view, _, _ = _case_signal_parts(case_view)
    top_sql = str(case_view.pred_manifestation.top1_sql or "")
    select_arity = _int_or_zero(current.get("select_arity"))
    join_count_bucket = str(current.get("join_count_bucket") or "")
    table_count_bucket = str(current.get("table_count_bucket") or "")
    predicate_count_bucket = str(current.get("predicate_count_bucket") or "")
    shape = _payload(current.get("output_shape_current"))
    roles = [str(role).strip() for role in (shape.get("roles") or []) if str(role).strip()]
    role_homogeneous = bool(roles) and len(set(roles)) == 1
    has_pair_role_side = (
        "pred.role_side_pair_output=True" in current_signals
        or bool(current_signals & {"pred.pair_output=True"})
    )
    has_distinct = bool(
        re.search(r"\bselect\s+distinct\b", top_sql, flags=re.IGNORECASE)
        or pred_sql_view.get("has_distinct")
        or _case_output_shape(case_view).get("has_distinct")
    )
    has_aggregate = bool(current.get("has_aggregate"))
    has_distinct_aggregate = bool(current.get("has_distinct_aggregate"))
    has_group_by = bool(current.get("has_group_by"))
    has_order_by_limit = bool(current.get("has_order_by")) and bool(current.get("has_limit"))
    signals = {
        "has_pair_role_side_output": has_pair_role_side,
        "same_relation_two_role_sides": "pred.same_relation_two_role_sides=True" in current_signals,
        "select_arity_ge_2": select_arity >= 2,
        "no_distinct_on_pair_output": has_pair_role_side and not has_distinct,
        "select_role_dtype_homogeneous": role_homogeneous,
        "has_aggregate_in_select": has_aggregate,
        "answer_unit_count_distinct": has_distinct_aggregate,
        "answer_unit_count_plain": has_aggregate and not has_distinct_aggregate,
        "answer_unit_scalar_aggregate": has_aggregate and select_arity <= 1 and not has_group_by,
        "has_join_chain_via_bridge_table": join_count_bucket in {"2", "3plus"} or table_count_bucket == "3plus",
        "has_direct_relation_join": join_count_bucket == "1",
        "has_predicate_outside_aggregate_scope": has_aggregate and predicate_count_bucket != "0" and not has_group_by,
        "has_group_by": has_group_by,
        "has_order_by_limit": has_order_by_limit,
    }
    return {
        key: bool(signals.get(key, False))
        for key in sorted(BIAS_RECOGNITION_SIGNAL_VOCABULARY)
    }


def _output_arity_signal(arity: Any) -> Optional[str]:
    try:
        value = int(arity)
    except Exception:
        return None
    if value > 2:
        return "pred.output_arity=>2"
    return f"pred.output_arity={value}"


def _pred_contract_signals_from_summary(pred_current: Dict[str, Any]) -> Set[str]:
    shape = _payload(pred_current.get("output_shape_current"))
    signals: Set[str] = set()
    arity = shape.get("arity") if shape else pred_current.get("select_arity")
    if arity is not None:
        signals.add(f"pred.select_arity={arity}")
        signals.add("pred.select_arity_present=True")
        arity_signal = _output_arity_signal(arity)
        if arity_signal:
            signals.add(arity_signal)
    roles = sorted(str(role) for role in (shape.get("roles") or []) if str(role))
    if roles:
        signals.add("pred.output_roles=" + ",".join(roles))
        for role in roles:
            signals.add(f"pred.output_role={role}")
    grain = shape.get("grain") if shape else None
    if grain:
        signals.add(f"pred.output_grain={grain}")
    if str(arity) == "2" or grain == "pair_rows":
        signals.add("pred.pair_output=True")
    for key in ("join_count_bucket", "table_count_bucket", "predicate_count_bucket"):
        value = pred_current.get(key)
        if value is not None:
            signals.add(f"pred.{key}={value}")
    if pred_current.get("predicate_literal_count_bucket") is not None:
        signals.add(
            f"pred.predicate_literal_count_bucket={pred_current.get('predicate_literal_count_bucket')}"
        )
    for key in (
        "has_or_predicate",
        "has_negation_predicate",
        "has_aggregate",
        "has_distinct_aggregate",
        "has_case_when",
        "has_group_by",
        "has_order_by",
        "has_limit",
    ):
        signals.add(f"pred.{key}={bool(pred_current.get(key))}")
    return signals


def _output_role_side_pair_signals(role_graph: Dict[str, Any]) -> Set[str]:
    """Derive pair/role-side facts from the current SQL role graph.

    The check is topology-based: two output slots either come through two
    different join paths to the same projected attribute, or are two distinct
    identifier-like columns on the same relation. It deliberately avoids
    database-specific table-name vocabulary.
    """
    signals: Set[str] = set()
    output_refs = [_payload(ref) for ref in (role_graph.get("output_refs") or [])]
    if len(output_refs) != 2:
        return signals

    signals.add("pred.output_arity=2")
    signals.add("pred.pair_output=True")
    for ref in output_refs:
        path_role = str(ref.get("path_role") or "").strip()
        if path_role:
            signals.add(f"pred.select_contains={path_role}")
        direct_role_path = str(ref.get("direct_role_path") or "").strip()
        derived_role_path = str(ref.get("derived_role_path") or "").strip()
        role_side_group = str(ref.get("role_side_group") or "").strip()
        side_key = str(ref.get("side_key") or "").strip()
        if direct_role_path:
            signals.add(f"pred.contains_direct_role_path={direct_role_path}")
        if derived_role_path:
            signals.add(f"pred.contains_derived_role_path={derived_role_path}")
        if role_side_group:
            signals.add(f"pred.contains_role_side_group={role_side_group}")
        if side_key:
            signals.add(f"pred.contains_side_key={side_key}")

    paths = [str(ref.get("path_role") or "") for ref in output_refs]
    output_tails = [
        path.split(":output:", 1)[1] if ":output:" in path else ""
        for path in paths
    ]
    join_prefixes = [
        path.split(":output:", 1)[0] if ":output:" in path else ""
        for path in paths
    ]
    if (
        output_tails[0]
        and output_tails[0] == output_tails[1]
        and join_prefixes[0]
        and join_prefixes[1]
        and join_prefixes[0] != join_prefixes[1]
    ):
        signals.add("pred.role_side_pair_output=True")
        signals.add("pred.same_relation_two_role_sides=True")

    tables = [str(ref.get("table") or "").lower() for ref in output_refs]
    columns = [str(ref.get("column") or "").lower() for ref in output_refs]
    column_roles = [str(ref.get("column_role") or "").lower() for ref in output_refs]
    side_groups = [str(ref.get("role_side_group") or "").lower() for ref in output_refs]
    side_keys = [str(ref.get("side_key") or "").lower() for ref in output_refs]
    if (
        side_groups[0]
        and side_groups[0] == side_groups[1]
        and side_keys[0]
        and side_keys[1]
        and side_keys[0] != side_keys[1]
    ):
        signals.add("pred.role_side_pair_output=True")
        signals.add("pred.same_relation_two_role_sides=True")
    if (
        tables[0]
        and tables[0] == tables[1]
        and columns[0]
        and columns[1]
        and columns[0] != columns[1]
        and all(role == "identifier" for role in column_roles)
    ):
        signals.add("pred.role_side_pair_output=True")
        signals.add("pred.same_relation_two_role_sides=True")
    return signals


def build_current_case_signals(
    runtime_case_view: RuntimeCaseView,
    sql_role_graph: Optional[Any] = None,
    local_schema_view: Optional[LocalSchemaView] = None,
) -> Set[str]:
    """Build the runtime-visible signal vocabulary used by trigger/replay.

    ``sql_role_graph`` and ``local_schema_view`` are accepted for the shared
    contract API; current implementation derives equivalent facts from
    RuntimeCaseView's signal bundle and LocalSchemaView.
    """
    case_view = runtime_case_view
    pred_sql_view, schema_view, _ = _case_signal_parts(case_view)
    signals = set(_pred_contract_signals_from_summary(_case_pred_current_summary(case_view)))
    signals |= _question_contract_signals(case_view)
    active_schema_view = local_schema_view or case_view.local_schema_view
    role_graph = _payload(sql_role_graph)
    if not role_graph:
        try:
            role_graph = RoleGraphNormalizer().normalize_sql(
                sql=case_view.pred_manifestation.top1_sql or "",
                schema_view=active_schema_view,
                source="pred_sql",
            )
        except Exception:
            role_graph = {}
    signals |= _output_role_side_pair_signals(role_graph)
    for section in ("output_refs", "join_refs", "predicate_refs"):
        for ref in role_graph.get(section) or []:
            payload = _payload(ref)
            sql_role = str(payload.get("sql_role") or "").strip()
            column_role = str(payload.get("column_role") or "").strip()
            path_role = str(payload.get("path_role") or "").strip()
            direct_role_path = str(payload.get("direct_role_path") or "").strip()
            derived_role_path = str(payload.get("derived_role_path") or "").strip()
            role_side_group = str(payload.get("role_side_group") or "").strip()
            side_key = str(payload.get("side_key") or "").strip()
            relation_role = str(payload.get("relation_role") or "").strip()
            if sql_role:
                signals.add(f"pred.contains_sql_role={sql_role}")
            if column_role:
                signals.add(f"pred.contains_column_role={column_role}")
            if path_role:
                signals.add(f"pred.contains_path_role={path_role}")
            if direct_role_path:
                signals.add(f"pred.contains_direct_role_path={direct_role_path}")
            if derived_role_path:
                signals.add(f"pred.contains_derived_role_path={derived_role_path}")
            if role_side_group:
                signals.add(f"pred.contains_role_side_group={role_side_group}")
            if side_key:
                signals.add(f"pred.contains_side_key={side_key}")
            if relation_role:
                signals.add(f"pred.contains_relation_role={relation_role}")
    for key, roles in (role_graph.get("table_relation_roles") or {}).items():
        role_values = roles if isinstance(roles, (list, tuple, set)) else [roles]
        for role in role_values:
            if str(role):
                signals.add(f"pred.contains_relation_role={role}")
    for index, role in enumerate(pred_sql_view.get("select_role_profile") or []):
        role = str(role or "").strip()
        if not role:
            continue
        signals.add(f"pred.output_slot_role={index}:{role}")
        signals.add(f"pred.contains_column_role={role}")
    for edge in pred_sql_view.get("join_graph") or []:
        payload = _payload(edge)
        if payload.get("role_key"):
            signals.add(f"pred.join_role_key={payload.get('role_key')}")
        for side in ("left", "right"):
            endpoint = _payload(payload.get(side))
            if endpoint.get("column_role"):
                signals.add(f"pred.contains_column_role={endpoint.get('column_role')}")
            if endpoint.get("path_role"):
                signals.add(f"pred.contains_path_role={endpoint.get('path_role')}")
            if endpoint.get("direct_role_path"):
                signals.add(
                    f"pred.contains_direct_role_path={endpoint.get('direct_role_path')}"
                )
            if endpoint.get("derived_role_path"):
                signals.add(
                    f"pred.contains_derived_role_path={endpoint.get('derived_role_path')}"
                )
            if endpoint.get("role_side_group"):
                signals.add(
                    f"pred.contains_role_side_group={endpoint.get('role_side_group')}"
                )
            if endpoint.get("side_key"):
                signals.add(f"pred.contains_side_key={endpoint.get('side_key')}")
            if endpoint.get("relation_role"):
                signals.add(f"pred.contains_relation_role={endpoint.get('relation_role')}")
            if endpoint:
                signals.add("pred.contains_sql_role=join_endpoint")
    if active_schema_view is not None:
        for hint in getattr(active_schema_view, "semantic_hints", []) or []:
            if getattr(hint, "role_family", None):
                signals.add(f"schema.contains_column_role={hint.role_family}")
    for tag in _case_pred_tags(case_view):
        signals.add(f"pred.decisive_tag={tag}")
    for tag in _case_question_tags(case_view):
        signals.add(f"question.decisive_tag={tag}")
    for tag in schema_view.get("schema_tags") or []:
        signals.add(f"schema.tag={tag}")
    for flag, enabled in (pred_sql_view.get("runtime_structure_flags") or {}).items():
        signals.add(f"pred.flag.{flag}={bool(enabled)}")
    return signals


def _current_contract_signals(case_view: RuntimeCaseView) -> Set[str]:
    return build_current_case_signals(
        runtime_case_view=case_view,
        sql_role_graph=None,
        local_schema_view=case_view.local_schema_view,
    )


def _group_trigger_contract(group: GroupSummary) -> Dict[str, Any]:
    contract = _payload(getattr(group, "trigger_contract", None))
    return sanitize_trigger_contract(contract)


def _contract_has_executable_program(contract: Dict[str, Any]) -> bool:
    if is_contract_runtime_executable(contract):
        return True
    action_contract = _payload(contract.get("action_contract"))
    synthesized_program = _payload(action_contract.get("synthesized_program"))
    has_synthesized_program = bool(synthesized_program.get("ops"))
    if action_contract.get("repair_program"):
        return True
    synthesized = _payload(action_contract.get("synthesized_program"))
    return bool(synthesized.get("ops"))


def _group_has_synthesized_program(group: GroupSummary, contract: Dict[str, Any]) -> bool:
    program = getattr(group.instantiation_program, "synthesized_program", None)
    if program is not None and getattr(program, "ops", None):
        return True
    action_contract = _payload(contract.get("action_contract"))
    if bool(action_contract.get("has_synthesized_program")):
        return True
    synthesized = _payload(action_contract.get("synthesized_program"))
    return bool(synthesized.get("ops"))


def _required_role_slot_rows(contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    action_contract = _payload(contract.get("action_contract"))
    rows: List[Dict[str, Any]] = []
    for slot in action_contract.get("required_role_slots") or []:
        if isinstance(slot, dict):
            payload = _payload(slot)
            if payload:
                rows.append(payload)
        elif str(slot):
            rows.append({"kind": str(slot), "required": True})
    envelope = _payload(action_contract.get("program_envelope"))
    for slot in envelope.get("required_role_slots") or []:
        payload = _payload(slot)
        if payload:
            rows.append(payload)
    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _program_envelope(contract: Dict[str, Any]) -> Dict[str, Any]:
    action_contract = _payload(contract.get("action_contract"))
    return _payload(action_contract.get("program_envelope"))


def _schema_columns_lower(view: LocalSchemaView) -> Set[Tuple[str, str]]:
    return {
        (str(table).lower(), str(column).lower())
        for table, columns in (view.columns_by_table or {}).items()
        for column in (columns or [])
    }


def _schema_role_families_lower(view: LocalSchemaView) -> Set[str]:
    roles: Set[str] = set()
    for hint in getattr(view, "semantic_hints", []) or []:
        role = str(getattr(hint, "role_family", "") or "").strip().lower()
        if role:
            roles.add(role)
    return roles


def _target_invariant_failures(
    *,
    envelope: Dict[str, Any],
    case_view: RuntimeCaseView,
) -> List[str]:
    failures: List[str] = []
    view = case_view.local_schema_view
    schema_tables = {str(table).lower() for table in (view.tables or [])}
    schema_columns = _schema_columns_lower(view)
    schema_roles = _schema_role_families_lower(view)
    for row in envelope.get("target_invariants") or []:
        payload = _payload(row)
        kind = str(payload.get("kind") or "").strip()
        value = str(payload.get("value") or "").strip()
        if not kind:
            continue
        if kind in {"target_added_relation_equality", "target_relation_equality"} and value:
            refs = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)", value)
            if refs and any(
                table.lower() not in schema_tables
                or (table.lower(), column.lower()) not in schema_columns
                for table, column in refs
            ):
                failures.append(f"target_invariant_unbindable:{kind}")
        elif kind == "target_output_roles" and value:
            roles = {
                role.strip().lower()
                for role in value.split(",")
                if role.strip() and role.strip().lower() not in {"other", "value"}
            }
            if roles and schema_roles and not roles <= schema_roles:
                failures.append(f"target_invariant_unbindable:{kind}")
    return failures


def _current_shape(case_view: RuntimeCaseView) -> Dict[str, Any]:
    return _payload(_case_pred_current_summary(case_view).get("output_shape_current"))


def _current_path_roles(current_signals: Set[str]) -> Set[str]:
    values: Set[str] = set()
    for signal in current_signals:
        if signal.startswith("pred.contains_path_role="):
            values.add(signal.split("=", 1)[1])
        elif signal.startswith("pred.select_contains="):
            values.add(signal.split("=", 1)[1])
        elif signal.startswith("pred.contains_direct_role_path="):
            values.add(signal.split("=", 1)[1])
        elif signal.startswith("pred.contains_derived_role_path="):
            values.add(signal.split("=", 1)[1])
    return values


def _current_role_side_groups(current_signals: Set[str]) -> Set[str]:
    return {
        signal.split("=", 1)[1]
        for signal in current_signals
        if signal.startswith("pred.contains_role_side_group=")
    }


def _source_antipattern_failures(
    *,
    envelope: Dict[str, Any],
    current_signals: Set[str],
    current_summary: Dict[str, Any],
) -> List[str]:
    failures: List[str] = []
    current_shape = _payload(current_summary.get("output_shape_current"))
    current_arity = _int_or_zero(current_shape.get("arity"))
    current_grain = str(current_shape.get("grain") or "")
    current_path_roles = _current_path_roles(current_signals)
    current_role_side_groups = _current_role_side_groups(current_signals)
    predicate_count_bucket = str(current_summary.get("predicate_count_bucket") or "")
    for row in envelope.get("source_antipatterns") or []:
        payload = _payload(row)
        kind = str(payload.get("kind") or "")
        if kind == "output_path_delta":
            if bool(payload.get("target_output_subset_of_source")) and current_arity < 2:
                failures.append("source_antipattern_output_subset_not_present")
            required_role_side_groups = [
                str(role)
                for role in (payload.get("source_role_side_groups") or [])
                if str(role)
            ]
            if required_role_side_groups:
                missing_groups = [
                    role
                    for role in required_role_side_groups
                    if role not in current_role_side_groups
                ]
                if missing_groups:
                    failures.append(
                        "source_antipattern_missing_role_side_groups:"
                        + ",".join(missing_groups[:4])
                    )
            else:
                required_roles = [
                    str(role)
                    for role in (payload.get("source_output_path_roles") or [])
                    if str(role)
                ]
                missing_roles = [
                    role for role in required_roles if role not in current_path_roles
                ]
                if missing_roles:
                    failures.append(
                        "source_antipattern_missing_output_path_roles:"
                        + ",".join(missing_roles[:4])
                    )
            requires_role_side_pair = bool(
                payload.get("same_table_multi_role_output")
                or payload.get("same_attribute_multi_role_output")
                or payload.get("source_role_side_groups")
            )
            if requires_role_side_pair and not (
                {"pred.role_side_pair_output=True", "pred.same_relation_two_role_sides=True"}
                & current_signals
            ):
                failures.append("source_antipattern_missing_role_side_pair_shape")
        elif kind == "predicate_scope_delta":
            if (
                payload.get("removed_source_predicates")
                or payload.get("added_target_predicates")
                or payload.get("possible_scope_move")
            ) and predicate_count_bucket == "0":
                failures.append("source_antipattern_missing_predicate_scope")
        elif kind == "grain_delta":
            source_grain = str(payload.get("source_grain") or "")
            if source_grain and current_grain and source_grain != current_grain:
                failures.append(
                    f"source_antipattern_grain_mismatch:{source_grain}!={current_grain}"
                )
    return failures


def _negative_guard_failures(
    *,
    envelope: Dict[str, Any],
    variant_required_match: bool,
    generalized_canonical_gate_passed: bool,
) -> List[str]:
    failures: List[str] = []
    for guard in envelope.get("negative_guards") or []:
        payload = _payload(guard)
        if str(payload.get("kind") or "") != "unresolved_variation_axis":
            continue
        axis = str(payload.get("axis") or "")
        if not variant_required_match and not generalized_canonical_gate_passed:
            failures.append(
                "negative_guard_unresolved_variation_axis"
                + (f":{axis}" if axis else "")
            )
    return failures


def _program_envelope_applicability(
    *,
    contract: Dict[str, Any],
    case_view: RuntimeCaseView,
    current_signals: Set[str],
    current_summary: Dict[str, Any],
    slot_overlap: float,
    variant_required_match: bool,
    generalized_canonical_gate_passed: bool,
) -> List[str]:
    reasons: List[str] = []
    slot_rows = _required_role_slot_rows(contract)
    if slot_rows and slot_overlap < 1.0:
        unresolved = sorted(
            {
                str(_payload(row).get("kind") or "")
                for row in slot_rows
                if str(_payload(row).get("kind") or "")
            }
        )
        suffix = ":" + ",".join(unresolved[:4]) if unresolved else ""
        reasons.append("required_role_slots_unbound" + suffix)
    envelope = _program_envelope(contract)
    if not envelope:
        return reasons
    branch_contract = _payload(envelope.get("branch_selection_contract"))
    branch_selection_unit = str(branch_contract.get("selection_unit") or "")
    has_runtime_branches = bool(envelope.get("runtime_branches"))
    if (
        bool(branch_contract.get("requires_current_variant_binding"))
        and branch_selection_unit != "runtime_branch"
        and not has_runtime_branches
        and not (variant_required_match or generalized_canonical_gate_passed)
    ):
        axes = [
            str(axis)
            for axis in (
                branch_contract.get("unresolved_variation_axes")
                or envelope.get("unresolved_variation_axes")
                or []
            )
            if str(axis)
        ]
        suffix = ":" + ",".join(axes[:4]) if axes else ""
        reasons.append("branch_selection_contract_unresolved" + suffix)
    reasons.extend(
        _source_antipattern_failures(
            envelope=envelope,
            current_signals=current_signals,
            current_summary=current_summary,
        )
    )
    reasons.extend(
        _negative_guard_failures(
            envelope=envelope,
            variant_required_match=variant_required_match,
            generalized_canonical_gate_passed=generalized_canonical_gate_passed,
        )
    )
    reasons.extend(
        _target_invariant_failures(
            envelope=envelope,
            case_view=case_view,
        )
    )
    return reasons


def _is_deferable_instantiation_reason(reason: str) -> bool:
    reason = str(reason or "")
    return reason.startswith("required_role_slots_unbound") or reason.startswith(
        "singleton_requires_unique_action"
    )


def _is_deferable_compiler_dry_run_reason(reason: str) -> bool:
    reason = str(reason or "")
    if not reason:
        return False
    if _is_deferable_instantiation_reason(reason):
        return True
    if reason.startswith("compiler_dry_run_no_candidates:"):
        return "binder_exception" not in reason and "compiler_exception" not in reason
    if reason.startswith("compiler_dry_run_missing_required_bundles:"):
        return True
    if reason.startswith("branch_binder_no_candidates:"):
        return "binder_exception" not in reason and "compiler_exception" not in reason
    if reason.startswith("branch_binder_missing_bundles:"):
        return True
    return False


def _split_instantiation_failures(
    reasons: Sequence[str],
    *,
    source_trigger_passed: bool,
) -> Tuple[List[str], List[str]]:
    hard: List[str] = []
    deferred: List[str] = []
    for reason in reasons or []:
        text = str(reason or "")
        if source_trigger_passed and _is_deferable_instantiation_reason(text):
            deferred.append(text)
        else:
            hard.append(text)
    return hard, deferred


def _split_compiler_dry_run_failures(
    reasons: Sequence[str],
    *,
    source_trigger_passed: bool,
) -> Tuple[List[str], List[str]]:
    hard: List[str] = []
    deferred: List[str] = []
    for reason in reasons or []:
        text = str(reason or "")
        if source_trigger_passed and _is_deferable_compiler_dry_run_reason(text):
            deferred.append(text)
        else:
            hard.append(text)
    return hard, deferred


def _binder_dry_run_candidate_rows(
    group: GroupSummary,
    case_view: RuntimeCaseView,
) -> Tuple[List[Tuple[str, Any]], str]:
    cache_key = _binder_dry_run_cache_key(group, case_view)
    cached = _BINDER_DRY_RUN_CACHE.get(cache_key)
    if cached is not None:
        return list(cached[0]), cached[1]
    try:
        candidate_sets, _diag = enumerate_candidates(
            case_view=case_view,
            memory_objects=[group],
        )
    except Exception as exc:  # pragma: no cover - defensive gate logging.
        return [], f"binder_exception:{type(exc).__name__}"
    rows: List[Tuple[str, Any]] = []
    for candidate_set in candidate_sets or []:
        primitive = getattr(candidate_set, "primitive", "")
        primitive_name = str(getattr(primitive, "value", primitive) or "")
        for candidate in getattr(candidate_set, "candidates", []) or []:
            if str(getattr(candidate, "source_group_id", "") or "") == str(group.group_id):
                rows.append((primitive_name, candidate))
    if rows:
        _BINDER_DRY_RUN_CACHE[cache_key] = (list(rows), "binder_candidates_available")
        return rows, "binder_candidates_available"
    empty_reasons = [
        str(getattr(candidate_set, "empty_reason", "") or "")
        for candidate_set in candidate_sets or []
        if getattr(candidate_set, "empty_reason", None)
    ]
    reason = ";".join(empty_reasons) or "binder_no_candidates"
    _BINDER_DRY_RUN_CACHE[cache_key] = ([], reason)
    return [], reason


def _binder_dry_run_candidates(
    group: GroupSummary,
    case_view: RuntimeCaseView,
) -> Tuple[List[Any], str]:
    rows, reason = _binder_dry_run_candidate_rows(group, case_view)
    return [candidate for _primitive, candidate in rows], reason


def _binder_dry_run_success(group: GroupSummary, case_view: RuntimeCaseView) -> Tuple[bool, str]:
    candidates, reason = _binder_dry_run_candidates(group, case_view)
    return bool(candidates), reason


def _required_program_op_ids(group: GroupSummary, contract: Dict[str, Any]) -> Set[str]:
    program = getattr(getattr(group, "instantiation_program", None), "synthesized_program", None)
    ops = getattr(program, "ops", None)
    if ops is None and isinstance(program, dict):
        ops = program.get("ops")
    if ops is None:
        action_contract = _payload(contract.get("action_contract"))
        synthesized = _payload(action_contract.get("synthesized_program"))
        ops = synthesized.get("ops")
    return {
        str(getattr(op, "op_id", "") or (_payload(op).get("op_id") if isinstance(op, dict) else "") or "")
        for op in (ops or [])
        if str(getattr(op, "op_id", "") or (_payload(op).get("op_id") if isinstance(op, dict) else "") or "")
    }


def _required_program_bundles(group: GroupSummary, contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    program = getattr(getattr(group, "instantiation_program", None), "synthesized_program", None)
    envelope = _payload(getattr(program, "program_envelope", None))
    if not envelope:
        action_contract = _payload(contract.get("action_contract"))
        envelope = _payload(action_contract.get("program_envelope"))
    action_envelope = _payload(envelope.get("action_envelope"))
    rows = [
        _payload(bundle)
        for bundle in (action_envelope.get("bundles") or [])
        if _payload(bundle)
    ]
    if rows:
        return rows
    return [
        {
            "bundle_id": op_id,
            "bundled_op_ids": [op_id],
            "counts_as_action": 1,
        }
        for op_id in sorted(_required_program_op_ids(group, contract))
    ]


def _binder_dry_run_cache_key(group: GroupSummary, case_view: RuntimeCaseView) -> str:
    schema_view = getattr(case_view, "local_schema_view", None)
    schema_payload = {
        "tables": list(getattr(schema_view, "tables", []) or []),
        "columns_by_table": getattr(schema_view, "columns_by_table", {}) or {},
        "pk_fk_edges": [
            _payload(edge)
            for edge in (getattr(schema_view, "pk_fk_edges", []) or [])
        ],
    }
    payload = {
        "group_id": group.group_id,
        "version": int(group.version or 0),
        "case_id": case_view.case_id,
        "db_id": case_view.db_id,
        "question_hash": hashlib.sha1(str(case_view.question or "").encode("utf-8")).hexdigest()[:16],
        "evidence_hash": hashlib.sha1(str(case_view.evidence or "").encode("utf-8")).hexdigest()[:16],
        "pred_sql_hash": hashlib.sha1(
            str(case_view.pred_manifestation.top1_sql or "").encode("utf-8")
        ).hexdigest()[:16],
        "schema_hash": hashlib.sha1(
            json.dumps(schema_payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()[:16],
        "trigger_contract": _payload(getattr(group, "trigger_contract", None)),
        "instantiation_program": _payload(getattr(group, "instantiation_program", None)),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _candidate_canonical_op_ids(candidates: Sequence[Any]) -> Set[str]:
    op_ids: Set[str] = set()
    for candidate in candidates or []:
        args = _payload(getattr(candidate, "arguments", None))
        op_id = str(args.get("canonical_op_id") or "")
        if op_id:
            op_ids.add(op_id)
    return op_ids


def _candidate_bundle_ids(candidates: Sequence[Any]) -> Set[str]:
    bundle_ids: Set[str] = set()
    for candidate in candidates or []:
        args = _payload(getattr(candidate, "arguments", None))
        bundle_id = (
            str(getattr(candidate, "bundle_id", "") or "")
            or str(args.get("bundle_id") or "")
            or str(args.get("canonical_op_id") or "")
        )
        if bundle_id:
            bundle_ids.add(bundle_id)
    return bundle_ids


def _bundle_branch_ambiguity_failures(
    *,
    candidates: Sequence[Any],
    required_bundles: Sequence[Dict[str, Any]],
    contract: Dict[str, Any],
) -> List[str]:
    envelope = _program_envelope(contract)
    branch_contract = _payload(envelope.get("branch_selection_contract"))
    if not bool(branch_contract.get("requires_current_variant_binding")):
        return []
    selection_keys_by_bundle: Dict[str, Set[str]] = {}
    for candidate in candidates or []:
        args = _payload(getattr(candidate, "arguments", None))
        bundle_id = (
            str(getattr(candidate, "bundle_id", "") or "")
            or str(args.get("bundle_id") or "")
            or str(args.get("canonical_op_id") or "")
        )
        if not bundle_id:
            continue
        selection_key = (
            str(getattr(candidate, "bundle_selection_key", "") or "")
            or str(args.get("bundle_selection_key") or "")
            or str(getattr(candidate, "candidate_id", "") or "")
        )
        if not selection_key:
            continue
        selection_keys_by_bundle.setdefault(bundle_id, set()).add(selection_key)
    failures: List[str] = []
    for bundle in required_bundles or []:
        bundle_id = str(_payload(bundle).get("bundle_id") or "")
        if not bundle_id:
            continue
        if len(selection_keys_by_bundle.get(bundle_id) or set()) > 1:
            failures.append(f"branch_selection_ambiguous:{bundle_id}")
    return failures


def _runtime_branch_rows(group: GroupSummary) -> List[Dict[str, Any]]:
    program = getattr(getattr(group, "instantiation_program", None), "synthesized_program", None)
    envelope = _payload(getattr(program, "program_envelope", None))
    if not envelope:
        contract = _group_trigger_contract(group)
        envelope = _program_envelope(contract)
    rows = [
        _payload(branch)
        for branch in (envelope.get("runtime_branches") or [])
        if _payload(branch)
    ]
    if rows:
        return rows
    # Backward compatibility for older frozen libraries. Lowering branches are
    # evidence-only; without runtime validation they stay non-usable.
    return [
        {
            **_payload(branch),
            "runtime_usable": False,
            "runtime_blockers": ["runtime_branch_contract_missing"],
        }
        for branch in (envelope.get("lowering_branches") or [])
        if _payload(branch)
    ]


def _runtime_branch_support(branch: Mapping[str, Any], fallback_case_ids: Sequence[str]) -> List[str]:
    support = [str(case_id) for case_id in (branch.get("support_case_ids") or []) if str(case_id)]
    return support or [str(case_id) for case_id in fallback_case_ids if str(case_id)]


def _runtime_branch_id(branch: Mapping[str, Any]) -> str:
    return str(branch.get("branch_id") or branch.get("bundle_id") or "").strip()


def _runtime_branch_bundle_ids(branch: Mapping[str, Any]) -> Set[str]:
    values: Set[str] = set()
    for item in branch.get("bundle_ids") or []:
        if str(item).strip():
            values.add(str(item).strip())
    bundle_id = str(branch.get("bundle_id") or "").strip()
    if bundle_id:
        values.add(bundle_id)
    return values


def _runtime_branch_required_signals(branch: Mapping[str, Any]) -> Set[str]:
    return {
        str(sig)
        for sig in (branch.get("required_signals") or [])
        if _is_substantive_hard_signal(str(sig))
    }


def _runtime_branch_negative_signals(branch: Mapping[str, Any]) -> Set[str]:
    return {
        str(sig)
        for sig in (branch.get("negative_signals") or [])
        if str(sig)
    }


def _bias_recognition_contract(group: GroupSummary) -> Dict[str, Any]:
    program = getattr(group, "instantiation_program", None)
    contract = getattr(program, "bias_recognition_contract", None)
    return _payload(contract)


def _case_bias_recognition_signals(case_view: RuntimeCaseView) -> Dict[str, bool]:
    signals = dict(getattr(case_view, "bias_recognition_signals", {}) or {})
    if not signals:
        signals = compute_bias_recognition_signals(case_view)
    return {
        str(key): bool(value)
        for key, value in signals.items()
        if str(key) in BIAS_RECOGNITION_SIGNAL_VOCABULARY
    }


def _evaluate_bias_recognition(
    *,
    group: GroupSummary,
    case_view: RuntimeCaseView,
) -> Tuple[Dict[str, Any], bool, List[str]]:
    contract = _bias_recognition_contract(group)
    if not contract:
        return {}, False, []
    case_signals = _case_bias_recognition_signals(case_view)
    recognition = [
        str(signal)
        for signal in (contract.get("recognition_signals") or [])
        if str(signal) in BIAS_RECOGNITION_SIGNAL_VOCABULARY
    ]
    anti = [
        str(signal)
        for signal in (contract.get("anti_signals") or [])
        if str(signal) in BIAS_RECOGNITION_SIGNAL_VOCABULARY
    ]
    anti_hits = [signal for signal in anti if case_signals.get(signal)]
    hits = [signal for signal in recognition if case_signals.get(signal)]
    total = len(recognition)
    try:
        threshold = float(contract.get("min_signal_overlap", 0.6) or 0.6)
    except Exception:
        threshold = 0.6
    overlap = len(hits) / max(total, 1)
    audit = {
        "schema_version": "bias-recognition-audit-v1",
        "bias_motif": str(contract.get("bias_motif") or ""),
        "answer_shape_hint": str(contract.get("answer_shape_hint") or ""),
        "recognition_signals": recognition,
        "anti_signals": anti,
        "matched_signals": hits,
        "anti_hits": anti_hits,
        "hit_count": len(hits),
        "total": total,
        "overlap": round(overlap, 6),
        "min_signal_overlap": threshold,
    }
    blockers: List[str] = []
    if anti_hits:
        blockers.append("bias_anti_signal_hit:" + ",".join(anti_hits[:6]))
    if total <= 0 or overlap < threshold:
        blockers.append(
            f"bias_recognition_signals_missed:{len(hits)}/{total}@{overlap:.2f}"
        )
    return audit, bool(not blockers), blockers


def _branch_candidate_matches(candidate: Any, branch: Mapping[str, Any]) -> bool:
    bundle_ids = _runtime_branch_bundle_ids(branch)
    args = _payload(getattr(candidate, "arguments", None))
    candidate_bundle_id = (
        str(getattr(candidate, "bundle_id", "") or "")
        or str(args.get("bundle_id") or "")
        or str(args.get("canonical_op_id") or "")
        or str(getattr(candidate, "bound_branch_id", "") or "")
    )
    if bundle_ids and candidate_bundle_id not in bundle_ids:
        return False
    allowed_primitives = {
        str(item)
        for item in (branch.get("allowed_primitives") or [])
        if str(item)
    }
    candidate_primitive = (
        str(getattr(candidate, "bundle_primary_primitive", "") or "")
        or str(args.get("primary_primitive") or "")
        or str(args.get("canonical_op") or "")
    )
    if allowed_primitives and candidate_primitive and candidate_primitive not in allowed_primitives:
        return False
    return True


def _filter_group_to_runtime_branch(
    group: GroupSummary,
    branch: Mapping[str, Any],
) -> GroupSummary:
    """Return a branch-scoped copy of a pattern memory object.

    Runtime branch selection is answer-blind. Once a unique branch is selected,
    the compiler must not see sibling branch bundles; otherwise it can still
    pick an action for the wrong repair variant.
    """
    branch_payload = dict(branch)
    branch_id = _runtime_branch_id(branch_payload)
    bundle_ids = _runtime_branch_bundle_ids(branch_payload)
    bundled_op_ids = {
        str(op_id)
        for op_id in [
            *(branch_payload.get("bundled_op_ids") or []),
            *(branch_payload.get("cleanup_op_ids") or []),
        ]
        if str(op_id)
    }
    filtered = group.model_copy(deep=True)

    program = getattr(filtered.instantiation_program, "synthesized_program", None)
    envelope_obj = getattr(program, "program_envelope", None) if program is not None else None
    envelope_payload = _payload(envelope_obj)
    if envelope_payload:
        action_envelope = _payload(envelope_payload.get("action_envelope"))
        bundles = [
            _payload(bundle)
            for bundle in (action_envelope.get("bundles") or [])
            if _payload(bundle)
        ]
        if bundle_ids:
            bundles = [
                bundle
                for bundle in bundles
                if str(bundle.get("bundle_id") or "") in bundle_ids
            ]
        action_envelope["bundles"] = bundles
        if branch_payload.get("allowed_primitives"):
            action_envelope["allowed_primitives"] = [
                str(item)
                for item in (branch_payload.get("allowed_primitives") or [])
                if str(item)
            ]
        envelope_payload["action_envelope"] = action_envelope
        envelope_payload["runtime_branches"] = [branch_payload]
        for envelope_key, branch_key in (
            ("source_antipatterns", "source_antipatterns"),
            ("target_effects", "target_effects"),
            ("target_invariants", "target_invariants"),
            ("required_role_slots", "required_role_slots"),
            ("negative_guards", "negative_guards"),
        ):
            envelope_payload[envelope_key] = list(branch_payload.get(branch_key) or [])
        envelope_payload["branch_selection_contract"] = {
            **_payload(envelope_payload.get("branch_selection_contract")),
            "selection_unit": "runtime_branch",
            "selected_branch_id": branch_id,
            "requires_current_variant_binding": False,
        }
        if bundled_op_ids and program is not None:
            for op_field in ("ops", "core_ops", "accessory_ops"):
                rows = list(getattr(program, op_field, []) or [])
                if not rows:
                    continue
                filtered_rows = [
                    row
                    for row in rows
                    if str(getattr(row, "op_id", "") or _payload(row).get("op_id") or "")
                    in bundled_op_ids
                ]
                if filtered_rows:
                    program = program.model_copy(update={op_field: filtered_rows})
        if envelope_obj is not None and hasattr(envelope_obj, "model_copy"):
            envelope_obj = envelope_obj.model_copy(update=envelope_payload)
        else:
            envelope_obj = envelope_payload
        if program is not None and hasattr(program, "model_copy"):
            program = program.model_copy(update={"program_envelope": envelope_obj})
            filtered = filtered.model_copy(
                update={
                    "instantiation_program": filtered.instantiation_program.model_copy(
                        update={"synthesized_program": program}
                    )
                }
            )

    trigger_contract = _payload(getattr(filtered, "trigger_contract", None))
    action_contract = _payload(trigger_contract.get("action_contract"))
    if envelope_payload:
        action_contract["program_envelope"] = envelope_payload
        trigger_contract["action_contract"] = action_contract
        branch_required = [
            str(signal)
            for signal in (branch_payload.get("required_signals") or [])
            if _is_substantive_hard_signal(str(signal))
        ]
        branch_negative = [
            str(signal)
            for signal in (branch_payload.get("negative_signals") or [])
            if str(signal)
        ]
        if branch_required:
            trigger_contract["required_signals"] = sorted(set(branch_required))
            trigger_contract["decisive_pred_signals"] = sorted(set(branch_required))
        if branch_negative:
            trigger_contract["negative_signals"] = sorted(set(branch_negative))
        if bundle_ids:
            trigger_contract["max_actions"] = max(
                1,
                sum(
                    int(_payload(bundle).get("counts_as_action") or 1)
                    for bundle in (
                        _payload(envelope_payload.get("action_envelope")).get("bundles")
                        or []
                    )
                ),
            )
        if hasattr(filtered.trigger_contract, "model_copy"):
            filtered = filtered.model_copy(
                update={
                    "trigger_contract": filtered.trigger_contract.model_copy(
                        update=trigger_contract
                    )
                }
            )
    return filtered


def _branch_dry_run_gate(
    *,
    group: GroupSummary,
    branch: Mapping[str, Any],
    case_view: RuntimeCaseView,
) -> Tuple[bool, List[str]]:
    scoped_group = _filter_group_to_runtime_branch(group, branch)
    candidates, dry_reason = _binder_dry_run_candidates(scoped_group, case_view)
    matching = [
        candidate
        for candidate in candidates
        if _branch_candidate_matches(candidate, branch)
    ]
    if not matching:
        return False, [f"branch_binder_no_candidates:{dry_reason}"]
    bundle_ids = _runtime_branch_bundle_ids(branch)
    covered = _candidate_bundle_ids(matching)
    missing = sorted(bundle_ids - covered)
    if missing:
        return False, ["branch_binder_missing_bundles:" + ",".join(missing[:4])]
    required_bundles = _required_program_bundles(
        scoped_group,
        _group_trigger_contract(scoped_group),
    )
    ambiguity = _bundle_branch_ambiguity_failures(
        candidates=matching,
        required_bundles=required_bundles,
        contract=_group_trigger_contract(scoped_group),
    )
    if ambiguity:
        return False, ambiguity
    return True, []


def _select_runtime_branch(
    *,
    group: GroupSummary,
    case_view: RuntimeCaseView,
    current_signals: Set[str],
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[str], int]:
    branches = _runtime_branch_rows(group)
    usable = [
        branch
        for branch in branches
        if bool(branch.get("runtime_usable"))
    ]
    candidate_branches = usable
    audit_rows: List[Dict[str, Any]] = []
    blockers: List[str] = []
    if not branches:
        reason = "runtime_branch_contract_missing"
        return None, audit_rows, [reason], 0
    if not candidate_branches:
        candidate_branches = [
            branch
            for branch in branches
            if _runtime_branch_bundle_ids(branch) or branch.get("allowed_primitives")
        ]
        if not candidate_branches:
            return None, audit_rows, ["runtime_usable_branch_missing"], 0
        blockers.append("runtime_usable_branch_missing_deferred_to_binder")

    matched: List[Dict[str, Any]] = []
    for branch in candidate_branches:
        branch_id = _runtime_branch_id(branch)
        required = _runtime_branch_required_signals(branch)
        negative = _runtime_branch_negative_signals(branch)
        missing = sorted(required - current_signals)
        negative_hits = sorted(negative & current_signals)
        branch_reasons: List[str] = []
        branch_deferred_reasons: List[str] = []
        if missing:
            branch_reasons.append("branch_required_signals_missed:" + ",".join(missing[:6]))
        if negative_hits:
            branch_reasons.append("branch_negative_signal_hit:" + ",".join(negative_hits[:6]))
        if not branch_reasons:
            dry_ok, dry_reasons = _branch_dry_run_gate(
                group=group,
                branch=branch,
                case_view=case_view,
            )
            if not dry_ok:
                hard_reasons, deferred_reasons = _split_compiler_dry_run_failures(
                    dry_reasons,
                    source_trigger_passed=True,
                )
                branch_reasons.extend(hard_reasons[:6])
                branch_deferred_reasons.extend(deferred_reasons[:6])
        gate_passed = not branch_reasons
        audit_rows.append(
            {
                "branch_id": branch_id,
                "gate_passed": gate_passed,
                "required_hits": sorted(required & current_signals),
                "required_misses": missing,
                "negative_hits": negative_hits,
                "bundle_ids": sorted(_runtime_branch_bundle_ids(branch)),
                "runtime_usable": bool(branch.get("runtime_usable")),
                "runtime_validation_policy": str(
                    branch.get("runtime_validation_policy")
                    or "binder_dry_run_fallback"
                ),
                "reasons": branch_reasons or ["branch_gate_passed"],
                "deferred_instantiation_reasons": branch_deferred_reasons,
            }
        )
        if gate_passed:
            matched.append(branch)
        else:
            blockers.extend(branch_reasons[:4])

    if len(matched) == 1:
        return matched[0], audit_rows, blockers, len(usable)
    if not matched:
        return None, audit_rows, blockers or ["runtime_branch_no_match"], len(usable)
    signature_to_branches: Dict[Tuple[Tuple[str, ...], Tuple[str, ...]], List[Dict[str, Any]]] = {}
    for branch in matched:
        signature = (
            tuple(sorted(_runtime_branch_bundle_ids(branch))),
            tuple(sorted(str(item) for item in (branch.get("allowed_primitives") or []) if str(item))),
        )
        signature_to_branches.setdefault(signature, []).append(branch)
    if len(signature_to_branches) == 1:
        equivalent = sorted(
            matched,
            key=lambda branch: (
                len(_runtime_branch_support(branch, group.case_ids)),
                _runtime_branch_id(branch),
            ),
            reverse=True,
        )
        selected = equivalent[0]
        selected_id = _runtime_branch_id(selected)
        for row in audit_rows:
            if str(row.get("branch_id") or "") == selected_id:
                row["reasons"] = [
                    *list(row.get("reasons") or []),
                    "equivalent_branch_bundle_selected",
                ]
            elif row.get("gate_passed"):
                row["equivalent_to_selected_branch_id"] = selected_id
        return selected, audit_rows, blockers, len(usable)
    return None, audit_rows, ["branch_selection_ambiguous"], len(usable)


def _compiler_dry_run_gate(
    *,
    group: GroupSummary,
    case_view: RuntimeCaseView,
    contract: Dict[str, Any],
) -> Tuple[bool, List[str]]:
    candidates, dry_reason = _binder_dry_run_candidates(group, case_view)
    if not candidates:
        return False, [f"compiler_dry_run_no_candidates:{dry_reason}"]
    reasons: List[str] = []
    required_bundles = _required_program_bundles(group, contract)
    required_bundle_ids = {
        str(bundle.get("bundle_id") or "")
        for bundle in required_bundles
        if str(bundle.get("bundle_id") or "")
    }
    covered_bundle_ids = _candidate_bundle_ids(candidates)
    missing = sorted(required_bundle_ids - covered_bundle_ids)
    if missing:
        reasons.append("compiler_dry_run_missing_required_bundles:" + ",".join(missing[:4]))
    reasons.extend(
        _bundle_branch_ambiguity_failures(
            candidates=candidates,
            required_bundles=required_bundles,
            contract=contract,
        )
    )
    try:
        max_actions = max(1, int(contract.get("max_actions") or 1))
    except Exception:
        max_actions = 1
    required_action_count = sum(
        int(_payload(bundle).get("counts_as_action") or 0)
        for bundle in required_bundles
    ) or 1
    if required_action_count > max_actions:
        reasons.append(
            f"compiler_dry_run_bundle_budget_exceeded:{required_action_count}>{max_actions}"
        )
    return not reasons, reasons


def _slot_resolvable_score(group: GroupSummary, case_view: RuntimeCaseView) -> float:
    required = [slot for slot in group.instantiation_program.slots if slot.required]
    if not required:
        return 1.0

    view = case_view.local_schema_view
    all_tables = {t.lower() for t in view.tables}
    all_columns = {
        (table.lower(), column.lower())
        for table, columns in view.columns_by_table.items()
        for column in columns
    }
    role_by_column: Dict[Tuple[str, str], str] = {}
    for hint in view.semantic_hints:
        if hint.role_family:
            role_by_column[(hint.table.lower(), hint.column.lower())] = hint.role_family.lower()

    resolved = 0
    for slot in required:
        kind = slot.kind.lower()
        allowed_roles = {role.lower() for role in slot.allowed_role_families}
        if kind == "table":
            if all_tables:
                resolved += 1
        elif kind == "column":
            if not allowed_roles and all_columns:
                resolved += 1
            elif any(role_by_column.get(col) in allowed_roles for col in all_columns):
                resolved += 1
        elif kind in {"condition", "value", "grain_key", "edit_scope"}:
            resolved += 1
        elif all_columns or all_tables:
            resolved += 1

    return resolved / max(len(required), 1)


def _negative_evidence_hit(group: GroupSummary, case_view: RuntimeCaseView) -> bool:
    text = " ".join(
        [
            case_view.question or "",
            case_view.evidence or "",
            case_view.pred_manifestation.top1_sql or "",
        ]
    ).lower()
    for item in group.trigger_signature.negative_evidence:
        token = str(item or "").strip().lower()
        if token and token in text:
            return True
    return False


def _singleton_exact_source_mismatches(
    *,
    source_contract: Dict[str, Any],
    current_summary: Dict[str, Any],
) -> List[str]:
    """Return source/current structural mismatches for singleton exact-trigger.

    Singleton is the cold-start object, not a reusable program. It may trigger
    only when the current runtime-visible manifestation is structurally close to
    the source case. Families/patterns carry broader variation after compiler
    coverage; singleton must not generalize from a single coarse signal such as
    select_arity_present.
    """
    mismatches: List[str] = []
    exact_keys = (
        "select_arity",
        "join_count_bucket",
        "table_count_bucket",
        "predicate_count_bucket",
        "predicate_literal_count_bucket",
        "has_or_predicate",
        "has_negation_predicate",
        "has_aggregate",
        "has_distinct_aggregate",
        "has_case_when",
        "has_group_by",
        "has_order_by",
        "has_limit",
    )
    for key in exact_keys:
        if key not in source_contract or key not in current_summary:
            continue
        if source_contract.get(key) != current_summary.get(key):
            mismatches.append(f"{key}:{source_contract.get(key)}!={current_summary.get(key)}")

    source_shape = _payload(source_contract.get("output_shape_current"))
    current_shape = _payload(current_summary.get("output_shape_current"))
    for key in ("arity", "grain"):
        if key not in source_shape or key not in current_shape:
            continue
        if source_shape.get(key) != current_shape.get(key):
            mismatches.append(
                f"output_shape.{key}:{source_shape.get(key)}!={current_shape.get(key)}"
            )
    source_roles = sorted(str(role) for role in (source_shape.get("roles") or []) if str(role))
    current_roles = sorted(str(role) for role in (current_shape.get("roles") or []) if str(role))
    if source_roles and current_roles and source_roles != current_roles:
        mismatches.append(f"output_shape.roles:{source_roles}!={current_roles}")

    return mismatches


def _runtime_program_type(contract: Dict[str, Any]) -> str:
    action_contract = _payload(contract.get("action_contract"))
    program_type = str(action_contract.get("program_type") or "").strip()
    if program_type:
        return program_type
    lowerings = [
        str(item)
        for item in (action_contract.get("lowering_families") or [])
        if str(item)
    ]
    if len(lowerings) == 1:
        return lowerings[0]
    synthesized = _payload(action_contract.get("synthesized_program"))
    return str(synthesized.get("program_type") or "").strip()


def _program_type_atom_from_op_type(op_type: str) -> str:
    normalized = str(op_type or "").strip().upper()
    if normalized in {"SELECT_DROP_SLOT", "DROP_SELECT_SLOT"}:
        return "select_drop"
    if normalized in {"SELECT_REPLACE_SLOT", "REPLACE_SELECT_SLOT"}:
        return "select_replace"
    if normalized == "DROP_SIDE":
        return "role_side_selection"
    if normalized == "MOVE_CONDITION":
        return "where_side_edit"
    if normalized == "INSERT_BRIDGE":
        return "join_bridge"
    if normalized == "REROUTE_FACT":
        return "fact_route_reroute"
    return ""


def _op_is_dependency(op: Any) -> bool:
    payload = _payload(op)
    arguments = _payload(payload.get("arguments"))
    signature = _payload(arguments.get("operation_signature"))
    return bool(payload.get("is_dependency") or signature.get("is_dependency"))


def _primary_program_type_from_executable_ops(
    group: GroupSummary,
    contract: Dict[str, Any],
) -> str:
    program = getattr(getattr(group, "instantiation_program", None), "synthesized_program", None)
    ops = getattr(program, "ops", None)
    if ops is None and isinstance(program, dict):
        ops = program.get("ops")
    if ops is None:
        action_contract = _payload(contract.get("action_contract"))
        synthesized = _payload(action_contract.get("synthesized_program"))
        ops = synthesized.get("ops")
    atoms = {
        atom
        for op in (ops or [])
        if not _op_is_dependency(op)
        for atom in [_program_type_atom_from_op_type(_payload(op).get("op_type"))]
        if atom
    }
    return "+".join(sorted(atoms))


def _canonical_program_type_from_memory(group: GroupSummary) -> str:
    signals = _payload(getattr(group, "formation_signals", None))
    canonical_ir = _payload(signals.get("canonical_repair_ir"))
    ops = [_payload(op) for op in (canonical_ir.get("program_ops") or []) if _payload(op)]
    primary_ops = [
        op
        for op in ops
        if not bool(
            _payload(_payload(op.get("arguments")).get("operation_signature")).get("is_dependency")
            or op.get("is_dependency")
        )
    ]
    if not primary_ops:
        primary_ops = ops
    atoms = {
        atom
        for op in primary_ops
        for atom in [_program_type_atom_from_op_type(str(op.get("op_type") or ""))]
        if atom
    }
    return "+".join(sorted(atoms))


def _dry_run_candidate_signature(candidate: Any) -> Tuple[Any, ...]:
    args = _payload(getattr(candidate, "arguments", None))
    bundle_id = (
        str(getattr(candidate, "bundle_id", "") or "")
        or str(args.get("bundle_id") or "")
        or str(args.get("canonical_op_id") or "")
    )
    bundle_selection_key = (
        str(getattr(candidate, "bundle_selection_key", "") or "")
        or str(args.get("bundle_selection_key") or "")
    )
    if bundle_id and bundle_selection_key:
        return ("bundle", bundle_id, bundle_selection_key)
    raw_indexes = args.get("side_indexes") or args.get("source_slot_indexes") or []
    indexes = list(raw_indexes) if isinstance(raw_indexes, list) else [raw_indexes]
    if args.get("side_index") is not None:
        indexes.append(args.get("side_index"))
    normalized_indexes: List[int] = []
    for value in indexes:
        try:
            normalized_indexes.append(int(value))
        except Exception:
            continue
    if normalized_indexes:
        return ("drop_indexes", tuple(sorted(set(normalized_indexes))))
    exprs = args.get("from_exprs") or []
    if not isinstance(exprs, list):
        exprs = [exprs]
    if args.get("from_expr") is not None:
        exprs.append(args.get("from_expr"))
    return ("exprs", tuple(sorted(str(expr).strip().lower() for expr in exprs if str(expr))))


def _singleton_canonical_exact_check(
    *,
    singleton: GroupSummary,
    case_view: RuntimeCaseView,
    contract: Dict[str, Any],
    current_summary: Dict[str, Any],
) -> Tuple[bool, List[str], bool]:
    """Canonical singleton exact-trigger.

    Exactness is defined by compatible runtime-visible shape plus a unique
    compiler dry-run action. It does not compare alias names or literal values.
    """
    reasons: List[str] = []
    program_type = _runtime_program_type(contract)
    if not program_type:
        program_type = _canonical_program_type_from_memory(singleton)
    allowed_program_types = {
        "select_drop",
        "select_replace",
        "output_decrease",
        "role_side_selection",
        "where_side_edit",
        "join_bridge",
        "fact_route_reroute",
    }
    program_type_atoms = {
        atom.strip() for atom in str(program_type or "").split("+") if atom.strip()
    }
    if not program_type_atoms or not program_type_atoms <= allowed_program_types:
        primary_program_type = _primary_program_type_from_executable_ops(singleton, contract)
        if not primary_program_type:
            primary_program_type = _canonical_program_type_from_memory(singleton)
        primary_atoms = {
            atom.strip() for atom in str(primary_program_type or "").split("+") if atom.strip()
        }
        if not primary_atoms or not primary_atoms <= allowed_program_types:
            reasons.append("unsupported_singleton_program_type")

    source_contract = _payload(contract.get("source_case_contract"))
    if not source_contract:
        reasons.append("trigger_contract_missing_source_case_contract")
    else:
        source_shape = _payload(source_contract.get("output_shape_current"))
        current_shape = _payload(current_summary.get("output_shape_current"))
        for key in ("arity", "grain"):
            if key not in source_shape or key not in current_shape:
                continue
            if source_shape.get(key) != current_shape.get(key):
                reasons.append(
                    f"canonical_source_shape_mismatch:{key}:"
                    f"{source_shape.get(key)}!={current_shape.get(key)}"
                )
        source_roles = sorted(str(role) for role in (source_shape.get("roles") or []) if str(role))
        current_roles = sorted(str(role) for role in (current_shape.get("roles") or []) if str(role))
        if source_roles and current_roles and source_roles != current_roles:
            reasons.append(
                f"canonical_source_shape_mismatch:roles:{source_roles}!={current_roles}"
            )
    candidates, dry_reason = _binder_dry_run_candidates(singleton, case_view)
    dry_success = False
    if not candidates:
        reasons.append(f"required_role_slots_unbound:{dry_reason}")
    else:
        signatures = {
            _dry_run_candidate_signature(candidate)
            for candidate in candidates
        }
        dry_success = len(signatures) == 1
        if not dry_success:
            reasons.append(f"singleton_requires_unique_action:{len(signatures)}")
    return not reasons, reasons, dry_success


def _is_substantive_hard_signal(signal: str) -> bool:
    signal = str(signal or "")
    if not signal:
        return False
    if signal in {
        "pred.select_arity_present=True",
        "pred.has_select=True",
        "pred.has_join=True",
        "pred.has_where=True",
    }:
        return False
    if signal.startswith("program."):
        return False
    if signal in {
        "pred.has_aggregate=False",
        "pred.has_group_by=False",
        "pred.has_order_by=False",
        "pred.has_limit=False",
        "pred.has_case_when=False",
        "pred.has_or_predicate=False",
        "pred.has_negation_predicate=False",
        "pred.has_distinct_aggregate=False",
    }:
        return False
    if signal in {
        "pred.output_role=other",
        "pred.output_roles=other",
        "pred.contains_column_role=other",
        "pred.contains_sql_role=output_slot",
        "pred.contains_sql_role=predicate_ref",
        "pred.contains_sql_role=join_endpoint",
        "pred.contains_relation_role=unknown_table",
        "pred.contains_relation_role=table_node",
        "pred.contains_relation_role=root_table",
        "pred.contains_relation_role=connector_by_query_degree",
        "pred.contains_relation_role=connector_by_schema_degree",
    }:
        return False
    if re.match(r"^pred\.contains_path_role=output_slot_\d+$", signal):
        return False
    if re.match(r"^pred\.contains_path_role=predicate_\d+_ref_\d+$", signal):
        return False
    return True


def _substantive_signal_set(signal_set: set[str]) -> bool:
    return any(_is_substantive_hard_signal(signal) for signal in signal_set)


def _gate_group(
    *,
    group: GroupSummary,
    case_view: RuntimeCaseView,
    q_tags: Sequence[str],
    p_tags: Sequence[str],
    allow_self_singleton_replay: bool = False,
) -> TriggerCandidateAudit:
    question_overlap = _tag_overlap_score(group.trigger_signature.required_question_tags, list(q_tags))
    manifest_overlap = _tag_overlap_score(group.trigger_signature.required_pred_tags, list(p_tags))
    contract = _group_trigger_contract(group)
    action_contract = _payload(contract.get("action_contract"))
    has_synthesized_program = _group_has_synthesized_program(group, contract)
    current_signals = _current_contract_signals(case_view)
    current_summary = _case_pred_current_summary(case_view)
    required_signals = {
        str(sig)
        for sig in contract.get("required_signals") or []
        if _is_substantive_hard_signal(str(sig))
    }
    optional_signals = set(str(sig) for sig in contract.get("optional_signals") or [] if str(sig))
    negative_signals = set(str(sig) for sig in contract.get("negative_signals") or [] if str(sig))
    canonical_discriminants = {
        str(sig)
        for sig in contract.get("canonical_discriminants") or []
        if _is_substantive_hard_signal(str(sig))
    }
    trigger_policy = _payload(contract.get("trigger_policy"))
    raw_variant_required_signal_sets = [
        {str(sig) for sig in signal_set if str(sig)}
        for signal_set in contract.get("variant_required_signal_sets") or []
        if isinstance(signal_set, (list, tuple, set)) and signal_set
    ]
    variant_required_signal_sets = [
        signal_set
        for signal_set in raw_variant_required_signal_sets
        if _substantive_signal_set(signal_set)
    ]
    decisive_pred_signals = set(
        str(sig) for sig in contract.get("decisive_pred_signals") or [] if str(sig)
    )
    required_hits = sorted(required_signals & current_signals)
    required_misses = sorted(required_signals - current_signals)
    optional_hits = sorted(optional_signals & current_signals)
    negative_hits = sorted(negative_signals & current_signals)
    canonical_discriminant_hits = sorted(canonical_discriminants & current_signals)
    variant_required_match = bool(variant_required_signal_sets) and any(
        signal_set <= current_signals for signal_set in variant_required_signal_sets
    )
    defer_pattern_contract_to_branch = bool(
        group.group_type == GroupType.PATTERN and _runtime_branch_rows(group)
    )
    binder_dry_run_success = False
    generalized_canonical_gate_passed = False
    pattern_soft_required_signal_deferred = False
    slot_overlap = _slot_resolvable_score(group, case_view)
    selected_branch: Optional[Dict[str, Any]] = None
    selected_branch_id = ""
    matched_branch_ids: List[str] = []
    branch_match_audit: List[Dict[str, Any]] = []
    branch_blockers: List[str] = []
    branch_runtime_usable_count = 0
    dry_run_group = group
    dry_run_contract = contract
    deferred_instantiation_reasons: List[str] = []
    compiler_candidate_reasons: List[str] = []
    bias_recognition: Dict[str, Any] = {}
    bias_recognized = False
    diagnostic_only = False

    reasons: List[str] = []
    passed = True

    if group.db_id != case_view.db_id:
        passed = False
        reasons.append("db_id_mismatch")
    if not group.runtime_usable:
        passed = False
        reasons.append("runtime_usable_false")
    elif not is_contract_runtime_executable(contract):
        if defer_pattern_contract_to_branch:
            reasons.append("pattern_top_contract_deferred_to_branch")
        else:
            passed = False
            reasons.append("invalid_or_empty_trigger_contract")
    if str(group.status.value if hasattr(group.status, "value") else group.status) != "active":
        passed = False
        reasons.append("status_not_active")
    if (
        not allow_self_singleton_replay
        and group.group_type == GroupType.SINGLETON
        and str(case_view.case_id) in {str(case_id) for case_id in group.case_ids}
    ):
        passed = False
        reasons.append("self_singleton_replay_guard")
    if _negative_evidence_hit(group, case_view):
        passed = False
        reasons.append("negative_evidence_hit")
    if negative_hits:
        passed = False
        reasons.append("negative_contract_signal_hit")

    if (
        _TWO_STAGE_PATTERN_TRIGGER_ENABLED
        and passed
        and group.group_type == GroupType.PATTERN
        and _bias_recognition_contract(group)
    ):
        bias_recognition, bias_recognized, bias_blockers = _evaluate_bias_recognition(
            group=group,
            case_view=case_view,
        )
        if bias_blockers:
            passed = False
            reasons.extend(bias_blockers)
        else:
            reasons.append("bias_recognized")
            # Bias recognition is the lightweight stage-1 trigger. Strict
            # contract matching is intentionally deferred to runtime branch
            # selection and binder dry-run.
            variant_required_match = True
            required_misses = []

    if raw_variant_required_signal_sets and not variant_required_signal_sets:
        if defer_pattern_contract_to_branch:
            reasons.append("pattern_variant_contract_deferred_to_branch")
        else:
            passed = False
            reasons.append("variant_required_signals_non_substantive")
    if not required_signals and not variant_required_signal_sets:
        if defer_pattern_contract_to_branch:
            reasons.append("pattern_top_required_signals_deferred_to_branch")
        else:
            passed = False
            reasons.append("trigger_contract_missing_required_signals")
    elif required_misses:
        if defer_pattern_contract_to_branch:
            reasons.append("pattern_top_required_signals_deferred_to_branch")
            reasons.extend(f"top_required_miss:{item}" for item in required_misses[:6])
        elif group.group_type == GroupType.PATTERN and has_synthesized_program:
            pattern_soft_required_signal_deferred = True
            reasons.append("pattern_required_contract_signals_soft_missed")
            reasons.extend(f"soft_required_miss:{item}" for item in required_misses[:6])
        else:
            passed = False
            reasons.append("required_contract_signals_missed")
    if (
        pattern_soft_required_signal_deferred
        and passed
        and not variant_required_match
        and not generalized_canonical_gate_passed
    ):
        binder_dry_run_success, dry_reason = _binder_dry_run_success(group, case_view)
        if binder_dry_run_success:
            generalized_canonical_gate_passed = True
            reasons.append("pattern_soft_required_signals_recovered_by_binder")
        else:
            passed = False
            reasons.append("pattern_soft_required_signals_binder_failed")
            reasons.append(f"binder_reason:{dry_reason}")
    if (
        variant_required_signal_sets
        and not variant_required_match
        and not generalized_canonical_gate_passed
    ):
        allow_generalized = bool(
            trigger_policy.get("allow_out_of_variant_generalization", False)
        )
        if defer_pattern_contract_to_branch:
            reasons.append("pattern_variant_signals_deferred_to_branch")
        elif not allow_generalized:
            passed = False
            reasons.append("variant_required_signals_missed")
        elif not has_synthesized_program:
            passed = False
            reasons.append("variant_missed_without_synthesized_program")
        else:
            try:
                min_disc = int(trigger_policy.get("min_canonical_discriminants", 1) or 1)
            except Exception:
                min_disc = 1
            if len(canonical_discriminant_hits) < min_disc:
                passed = False
                reasons.append("canonical_discriminants_insufficient")
            elif bool(trigger_policy.get("requires_binder_dry_run", True)):
                binder_dry_run_success, dry_reason = _binder_dry_run_success(group, case_view)
                if not binder_dry_run_success:
                    passed = False
                    reasons.append("binder_dry_run_failed")
                    reasons.append(f"binder_reason:{dry_reason}")
                else:
                    generalized_canonical_gate_passed = True
                    reasons.append("generalized_canonical_gate_passed")
            else:
                binder_dry_run_success = True
                generalized_canonical_gate_passed = True
                reasons.append("generalized_canonical_gate_passed")
    elif variant_required_signal_sets and variant_required_match:
        reasons.append("variant_required_signals_matched")
    if group.group_type == GroupType.SINGLETON:
        exact_ok, exact_reasons, exact_dry_success = _singleton_canonical_exact_check(
            singleton=group,
            case_view=case_view,
            contract=contract,
            current_summary=current_summary,
        )
        binder_dry_run_success = binder_dry_run_success or exact_dry_success
        if not exact_ok:
            preliminary_source_trigger_passed = bool(
                variant_required_match
                or generalized_canonical_gate_passed
                or exact_dry_success
                or (required_signals and not required_misses)
            )
            hard_exact, deferred_exact = _split_instantiation_failures(
                exact_reasons,
                source_trigger_passed=preliminary_source_trigger_passed,
            )
            if hard_exact:
                passed = False
                reasons.append("singleton_canonical_exact_failed")
                reasons.extend(f"singleton_exact:{item}" for item in hard_exact[:6])
            if deferred_exact:
                deferred_instantiation_reasons.extend(
                    f"singleton_exact:{item}" for item in deferred_exact
                )
                reasons.append("singleton_canonical_exact_deferred_to_compiler")

    defer_pattern_applicability_to_branch = defer_pattern_contract_to_branch
    source_trigger_passed = bool(
        variant_required_match
        or generalized_canonical_gate_passed
        or binder_dry_run_success
        or (required_signals and not required_misses)
    )
    applicability_failures = []
    if not defer_pattern_applicability_to_branch:
        applicability_failures = _program_envelope_applicability(
            contract=contract,
            case_view=case_view,
            current_signals=current_signals,
            current_summary=current_summary,
            slot_overlap=slot_overlap,
            variant_required_match=variant_required_match,
            generalized_canonical_gate_passed=generalized_canonical_gate_passed,
        )
    if applicability_failures:
        hard_failures, deferred_failures = _split_instantiation_failures(
            applicability_failures,
            source_trigger_passed=source_trigger_passed,
        )
        if hard_failures:
            passed = False
            reasons.extend(hard_failures[:8])
        if deferred_failures:
            deferred_instantiation_reasons.extend(deferred_failures)
            reasons.append("role_slots_deferred_to_compiler")

    if (
        decisive_pred_signals
        and not (decisive_pred_signals & current_signals)
        and not variant_required_match
        and not generalized_canonical_gate_passed
    ):
        if defer_pattern_contract_to_branch:
            reasons.append("pattern_decisive_pred_signal_deferred_to_branch")
        else:
            passed = False
            reasons.append("decisive_pred_signal_missed")
    elif not decisive_pred_signals and not variant_required_match and not generalized_canonical_gate_passed:
        if defer_pattern_contract_to_branch:
            reasons.append("pattern_decisive_pred_signal_deferred_to_branch")
        else:
            passed = False
            reasons.append("trigger_contract_missing_decisive_pred_signal")

    if group.group_type == GroupType.PATTERN and not has_synthesized_program:
        passed = False
        reasons.append("runtime_group_missing_synthesized_program")
    if not _contract_has_executable_program(contract):
        if defer_pattern_contract_to_branch:
            reasons.append("pattern_executable_contract_deferred_to_branch")
        else:
            passed = False
            reasons.append("runtime_group_missing_executable_repair_contract")
    if group.group_type == GroupType.SINGLETON and int(contract.get("max_actions") or 1) > 1:
        passed = False
        reasons.append("singleton_contract_max_actions_gt_one")
    if group.group_type == GroupType.PATTERN:
        generic_required = required_signals <= {"pred.select_arity_present=True"}
        decisive_optional_hit = any(
            signal.startswith("pred.decisive_tag=")
            or signal.startswith("question.decisive_tag=")
            for signal in optional_hits
        )
        if (
            generic_required
            and not decisive_optional_hit
            and not variant_required_match
            and not generalized_canonical_gate_passed
        ):
            if defer_pattern_contract_to_branch:
                reasons.append("pattern_decisive_optional_signal_deferred_to_branch")
            else:
                passed = False
                reasons.append("runtime_group_missing_decisive_optional_signal_hit")

    if passed and group.group_type == GroupType.PATTERN:
        (
            selected_branch,
            branch_match_audit,
            branch_blockers,
            branch_runtime_usable_count,
        ) = _select_runtime_branch(
            group=group,
            case_view=case_view,
            current_signals=current_signals,
        )
        if selected_branch is None:
            passed = False
            if bias_recognized:
                diagnostic_only = True
                reasons.append("pattern_recognized_branch_unbindable")
            reasons.extend(branch_blockers[:8])
        else:
            selected_branch_id = _runtime_branch_id(selected_branch)
            matched_branch_ids = [selected_branch_id] if selected_branch_id else []
            selected_branch_audit = next(
                (
                    row
                    for row in branch_match_audit
                    if str(row.get("branch_id") or "") == selected_branch_id
                ),
                {},
            )
            for item in selected_branch_audit.get("deferred_instantiation_reasons") or []:
                deferred_instantiation_reasons.append(f"branch:{item}")
            dry_run_group = _filter_group_to_runtime_branch(group, selected_branch)
            dry_run_contract = _group_trigger_contract(dry_run_group)
            source_trigger_passed = True
            branch_slot_overlap = _slot_resolvable_score(dry_run_group, case_view)
            branch_applicability_failures = _program_envelope_applicability(
                contract=dry_run_contract,
                case_view=case_view,
                current_signals=current_signals,
                current_summary=current_summary,
                slot_overlap=branch_slot_overlap,
                variant_required_match=True,
                generalized_canonical_gate_passed=True,
            )
            if branch_applicability_failures:
                hard_failures, deferred_failures = _split_instantiation_failures(
                    branch_applicability_failures,
                    source_trigger_passed=True,
                )
                if hard_failures:
                    passed = False
                    reasons.extend(hard_failures[:8])
                if deferred_failures:
                    deferred_instantiation_reasons.extend(
                        f"branch:{item}" for item in deferred_failures
                    )
                    reasons.append("branch_role_slots_deferred_to_compiler")
            reasons.append("runtime_branch_selected:" + selected_branch_id)

    if passed:
        dry_run_ok, dry_run_reasons = _compiler_dry_run_gate(
            group=dry_run_group,
            case_view=case_view,
            contract=dry_run_contract,
        )
        binder_dry_run_success = binder_dry_run_success or dry_run_ok
        if not dry_run_ok:
            hard_reasons, deferred_reasons = _split_compiler_dry_run_failures(
                dry_run_reasons,
                source_trigger_passed=source_trigger_passed,
            )
            compiler_candidate_reasons.extend(dry_run_reasons)
            if deferred_reasons:
                deferred_instantiation_reasons.extend(
                    f"compiler:{item}" for item in deferred_reasons
                )
            if source_trigger_passed and not hard_reasons:
                reasons.append("compiler_dry_run_deferred_to_compiler")
            else:
                passed = False
                reasons.extend((hard_reasons or dry_run_reasons)[:6])
        else:
            reasons.append("compiler_dry_run_passed")

    if passed:
        reasons.append("gate_passed")
    reasons.append("legacy_tag_overlap_audit_only")
    signal_compat = 1.0 if passed else 0.0
    final_score = float(len(required_hits)) if passed else 0.0
    hard_gate_reasons = []
    if not passed:
        hard_gate_reasons = [
            reason
            for reason in reasons
            if reason
            and reason not in {
                "legacy_tag_overlap_audit_only",
                "variant_required_signals_matched",
                "singleton_canonical_exact_deferred_to_compiler",
                "role_slots_deferred_to_compiler",
                "branch_role_slots_deferred_to_compiler",
                "compiler_dry_run_deferred_to_compiler",
                "pattern_top_contract_deferred_to_branch",
                "pattern_variant_contract_deferred_to_branch",
                "pattern_top_required_signals_deferred_to_branch",
                "pattern_variant_signals_deferred_to_branch",
                "pattern_decisive_pred_signal_deferred_to_branch",
                "pattern_executable_contract_deferred_to_branch",
                "pattern_decisive_optional_signal_deferred_to_branch",
            }
            and not reason.startswith("soft_required_miss:")
            and not reason.startswith("top_required_miss:")
        ]

    return TriggerCandidateAudit(
        group_id=group.group_id,
        group_type=group.group_type,
        gate_passed=passed,
        gate_reasons=reasons,
        question_overlap=round(question_overlap, 6),
        manifest_overlap=round(manifest_overlap, 6),
        signal_compat=round(signal_compat, 6),
        structural_compat=round(signal_compat, 6),
        slot_overlap=round(slot_overlap, 6),
        final_score=round(final_score, 6),
        contract_version=str(contract.get("schema_version") or ""),
        required_signal_hits=required_hits,
        required_signal_misses=required_misses,
        negative_signal_hits=negative_hits,
        optional_signal_hits=optional_hits,
        variant_required_match=variant_required_match,
        canonical_discriminant_hits=canonical_discriminant_hits,
        binder_dry_run_success=binder_dry_run_success,
        trigger_policy=trigger_policy,
        selected_branch_id=selected_branch_id or None,
        matched_branch_ids=matched_branch_ids,
        branch_match_audit=branch_match_audit,
        branch_blockers=branch_blockers,
        branch_runtime_usable_count=branch_runtime_usable_count,
        source_trigger_passed=source_trigger_passed,
        hard_gate_reasons=hard_gate_reasons,
        deferred_instantiation_reasons=deferred_instantiation_reasons,
        compiler_candidate_reasons=compiler_candidate_reasons,
        bias_recognition=bias_recognition,
        bias_recognized=bias_recognized,
        diagnostic_only=diagnostic_only,
    )


def _group_sort_key(item: Tuple[GroupSummary, TriggerCandidateAudit]) -> Tuple[float, int, int, str]:
    group, audit = item
    type_priority = {
        GroupType.PATTERN: 3,
        GroupType.SINGLETON: 1,
    }.get(group.group_type, 0)
    return (
        float(len(audit.required_signal_hits)),
        type_priority,
        int(group.support or 0),
        group.group_id,
    )


_CONTRACT_IDENTITY_KEYS = {
    "action_id",
    "bundle_id",
    "candidate_id",
    "case_id",
    "case_ids",
    "effect_id",
    "group_id",
    "memory_id",
    "op_id",
    "canonical_op_id",
    "canonical_program_id",
    "program_id",
    "selected_candidate_id",
    "source_case_id",
    "source_case_ids",
    "step_id",
    "supporting_case_ids",
    "source_evidence",
    "support_evidence",
    "extraction_source",
    "origin",
    "notes",
    "rationale",
}


def _semantic_contract_payload(value: Any) -> Any:
    """Drop source identity fields before comparing action contracts.

    Runtime compatibility should compare the learned repair program, not the
    singleton/pattern object that happened to produce that program.
    """
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, set):
        return [_semantic_contract_payload(item) for item in sorted(value, key=str)]
    if isinstance(value, (list, tuple)):
        return [_semantic_contract_payload(item) for item in value]
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="python")
    elif isinstance(value, Mapping):
        payload = dict(value)
    else:
        payload = dict(getattr(value, "__dict__", {}) or {})
        if not payload:
            return str(value)
    if isinstance(payload, dict):
        out: Dict[str, Any] = {}
        for key, item in payload.items():
            key_text = str(key)
            if key_text in _CONTRACT_IDENTITY_KEYS:
                continue
            out[key_text] = _semantic_contract_payload(item)
        return out
    if isinstance(payload, list):
        return [_semantic_contract_payload(item) for item in payload]
    return payload


def _stable_json(value: Any) -> str:
    payload = _semantic_contract_payload(value)
    if payload in (None, "", [], {}):
        return ""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def _coarse_output_shape_delta(action_contract: Mapping[str, Any]) -> Dict[str, Any]:
    shape = _payload(action_contract.get("output_shape_delta"))
    keys = (
        "operation",
        "op",
        "direction",
        "from_arity",
        "to_arity",
        "current_arity",
        "target_arity",
        "current_grain",
        "target_grain",
        "grain_delta",
    )
    return {
        key: shape.get(key)
        for key in keys
        if shape.get(key) not in (None, "", [], {})
    }


def _root_effect_payload(signals: Mapping[str, Any], action_contract: Mapping[str, Any]) -> Dict[str, Any]:
    """Return case-derived root-bias fields, excluding branch/accessory details."""

    insight = _payload(signals.get("repair_insight_signature"))
    admission = _payload(signals.get("pattern_admission"))
    effect = (
        signals.get("root_effect")
        or signals.get("effect_core")
        or signals.get("shared_effect")
        or insight.get("effect_core")
        or {}
    )
    delta_axes = (
        signals.get("root_delta_axes")
        or signals.get("delta_axes")
        or insight.get("delta_axes")
        or []
    )
    interface = str(
        admission.get("primary_repair_interface")
        or insight.get("interface_key")
        or insight.get("repair_interface")
        or ""
    ).strip().lower()
    return {
        "stable_bias_key": str(admission.get("stable_bias_key") or "").strip().lower(),
        "repair_interface": interface,
        "output_shape_delta": _coarse_output_shape_delta(action_contract),
        "effect_core": _semantic_contract_payload(effect),
        "delta_axes": sorted(str(item) for item in (delta_axes or []) if str(item)),
    }


def _action_contract_key(group: GroupSummary) -> Tuple[str, ...]:
    contract = _group_trigger_contract(group)
    action = _payload(contract.get("action_contract"))
    envelope = _payload(action.get("program_envelope"))
    action_shape = _payload(action.get("output_shape_delta"))
    answer_unit_contract = _payload(action.get("answer_unit_contract"))
    synthesized_program = _semantic_contract_payload(action.get("synthesized_program"))
    repair_program = _semantic_contract_payload(action.get("repair_program") or [])
    required_role_slots = [
        str(item)
        if not isinstance(item, dict)
        else json.dumps(_semantic_contract_payload(item), sort_keys=True, ensure_ascii=False, default=str)
        for item in (_required_role_slot_rows(contract) or [])
    ]
    required_target_invariants = sorted(
        str(item)
        for item in (action.get("required_target_invariants") or [])
        if str(item)
    )
    allowed_primitives = sorted(
        str(item)
        for item in (_payload(envelope.get("action_envelope")).get("allowed_primitives") or [])
        if str(item)
    )
    bundle_rows = []
    for row in (_payload(envelope.get("action_envelope")).get("bundles") or []):
        payload = _payload(row)
        if not payload:
            continue
        bundle_rows.append(
            json.dumps(
                {
                    "effect_kind": payload.get("effect_kind"),
                    "primary_primitive": payload.get("primary_primitive"),
                    "role_side_group": payload.get("role_side_group"),
                    "target_invariants": _semantic_contract_payload(
                        payload.get("target_invariants") or []
                    ),
                    "counts_as_action": payload.get("counts_as_action"),
                },
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
        )
    lowering_families = sorted(
        str(item) for item in (action.get("lowering_families") or []) if str(item)
    )
    key = (
        str(action.get("locus") or ""),
        str(action.get("op_family") or ""),
        str(action.get("target_family") or ""),
        json.dumps(action_shape, sort_keys=True, ensure_ascii=False, default=str),
        json.dumps(answer_unit_contract, sort_keys=True, ensure_ascii=False, default=str),
        json.dumps(synthesized_program, sort_keys=True, ensure_ascii=False, default=str),
        json.dumps(repair_program, sort_keys=True, ensure_ascii=False, default=str),
        ",".join(sorted(bundle_rows)),
        ",".join(allowed_primitives),
        ",".join(lowering_families),
        ",".join(sorted(required_role_slots)),
        ",".join(required_target_invariants),
    )
    if not any(key):
        return ("missing_action_contract", str(group.group_id))
    return key


def _root_bias_contract_key(group: GroupSummary) -> Tuple[str, ...]:
    """Root-pattern key used before treating action branches as conflicts."""

    signals = _payload(getattr(group, "formation_signals", None))
    contract = _group_trigger_contract(group)
    action = _payload(contract.get("action_contract"))
    root_payload = _root_effect_payload(signals, action)
    if any(value not in (None, "", [], {}) for value in root_payload.values()):
        structured_root_present = bool(
            root_payload.get("output_shape_delta")
            or root_payload.get("effect_core")
            or root_payload.get("delta_axes")
        )
        repair_interface = "" if structured_root_present else str(root_payload.get("repair_interface") or "")
        return (
            "root_bias",
            str(root_payload.get("stable_bias_key") or ""),
            repair_interface,
            _stable_json(root_payload.get("output_shape_delta")),
            _stable_json(root_payload.get("effect_core")),
            ",".join(root_payload.get("delta_axes") or []),
        )
    required = sorted(
        str(sig)
        for sig in (contract.get("canonical_discriminants") or contract.get("required_signals") or [])
        if _is_substantive_hard_signal(str(sig))
    )
    if required:
        return ("root_signals", ",".join(required))
    return ("missing_root_bias", str(group.group_id))


def _candidate_transform_key(candidate: Any, *, primitive_name: str = "") -> Tuple[str, ...]:
    """Key for the concrete transform that code can enumerate on this case.

    This is deliberately current-case specific. It compares primitive plus
    bound arguments, not the source memory id that produced the candidate.
    """

    arguments = dict(getattr(candidate, "arguments", {}) or {})
    executable_args = _executable_transform_arguments(arguments)
    repair_program = []
    for step in dict(arguments).get("repair_program") or []:
        step_payload = _semantic_contract_payload(step)
        if not isinstance(step_payload, dict):
            continue
        step_args = _executable_transform_arguments(step_payload.get("arguments") or {})
        if not step_args:
            continue
        repair_program.append(
            {
                "locus": step_payload.get("locus"),
                "op": step_payload.get("op"),
                "arguments": step_args,
                "is_dependency": bool(step_payload.get("is_dependency")),
                "required": bool(step_payload.get("required", True)),
            }
        )
    if executable_args and repair_program:
        executable_args["repair_program"] = repair_program
    primitive_text = str(primitive_name or "").strip()
    args_json = _stable_json(executable_args)
    if not primitive_text or not args_json:
        return ()
    return (primitive_text, args_json)


_TRANSFORM_ARGUMENT_NON_EXECUTABLE_KEYS = {
    "bound_branch_id",
    "bundle_id",
    "bundle_primary_primitive",
    "bundle_selection_key",
    "canonical_contract",
    "cleanup_edits",
    "canonical_invariants",
    "canonical_op_type",
    "canonical_op_id",
    "canonical_refs",
    "canonical_target_invariants",
    "canonical_unresolved_variation_axes",
    "compiled_from_program_id",
    "counts_as_action",
    "effect_contract",
    "effect_kind",
    "from_expr",
    "from_exprs",
    "memory_alignment",
    "provenance",
    "repair_program",
    "repair_effect_signature",
    "rationale",
    "source_alias",
    "source_aliases",
    "source_columns",
    "source_evidence",
    "source_expression",
    "source_expressions",
    "source_table",
    "source_tables",
    "source_table_column",
    "source_table_columns",
    "supporting_case_ids",
    "target_alias",
    "target_aliases",
    "target_columns",
    "target_expression",
    "target_expressions",
    "target_invariants",
    "target_table",
    "target_tables",
    "target_table_column",
    "target_table_columns",
    "unresolved_variation_axes",
}

_TRANSFORM_ARGUMENT_WEAK_ONLY_KEYS = {
    "output_shape_delta",
    "required_edit_scopes",
}


def _executable_transform_arguments(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """Keep current executable arguments while dropping provenance/audit data."""

    out: Dict[str, Any] = {}
    for key, value in dict(arguments or {}).items():
        key_text = str(key)
        if key_text in _TRANSFORM_ARGUMENT_NON_EXECUTABLE_KEYS:
            continue
        if key_text in _CONTRACT_IDENTITY_KEYS:
            continue
        payload = _semantic_contract_payload(value)
        if payload in (None, "", [], {}):
            continue
        out[key_text] = payload
    if not any(key not in _TRANSFORM_ARGUMENT_WEAK_ONLY_KEYS for key in out):
        return {}
    return out


def _current_transform_keys_for_group(
    group: GroupSummary,
    case_view: Optional[RuntimeCaseView],
) -> Tuple[Set[Tuple[str, ...]], str]:
    if case_view is None:
        return set(), "case_view_missing"
    rows, reason = _binder_dry_run_candidate_rows(group, case_view)
    keys: Set[Tuple[str, ...]] = set()
    incomplete_count = 0
    for primitive_name, candidate in rows:
        key = _candidate_transform_key(candidate, primitive_name=primitive_name)
        if key:
            keys.add(key)
        else:
            incomplete_count += 1
    if keys:
        return keys, reason
    if rows and incomplete_count:
        return set(), "incomplete_transform_key:empty_executable_args"
    return set(), reason or "no_current_transform_key"


def _select_groups_with_shared_current_transform(
    selection_pool: Sequence[Tuple[GroupSummary, TriggerCandidateAudit]],
    *,
    max_selected: int,
    case_view: Optional[RuntimeCaseView],
    audit: Dict[str, Any],
) -> Tuple[List[GroupSummary], str]:
    """Select the largest same-root subset that shares a current transform."""

    transform_audit: Dict[str, Any] = {}
    keys_by_group: Dict[str, Set[Tuple[str, ...]]] = {}
    item_by_group_id: Dict[str, Tuple[GroupSummary, TriggerCandidateAudit]] = {}
    for item in selection_pool:
        group, _candidate_audit = item
        group_id = str(group.group_id)
        item_by_group_id[group_id] = item
        keys, reason = _current_transform_keys_for_group(group, case_view)
        keys_by_group[group_id] = set(keys)
        transform_audit[group_id] = {
            "reason": reason,
            "transform_key_count": len(keys),
            "transform_keys": [
                hashlib.sha1(
                    json.dumps(key, sort_keys=True, ensure_ascii=False, default=str).encode(
                        "utf-8"
                    )
                ).hexdigest()[:12]
                for key in sorted(keys)
            ][:8],
        }
    audit["current_transform_audit"] = transform_audit
    key_to_group_ids: Dict[Tuple[str, ...], Set[str]] = {}
    for group_id, keys in keys_by_group.items():
        for key in keys:
            key_to_group_ids.setdefault(key, set()).add(group_id)
    if not key_to_group_ids:
        audit["resolution"] = "same_root_no_transform_key"
        return [], "ambiguous_current_transform"

    def key_hash(key: Tuple[str, ...]) -> str:
        return hashlib.sha1(
            json.dumps(key, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()[:12]

    def best_item_sort_key(group_id: str) -> Tuple[float, int, int, str]:
        return _group_sort_key(item_by_group_id[group_id])

    ranked_keys = sorted(
        key_to_group_ids,
        key=lambda key: (
            len(key_to_group_ids[key]),
            max(best_item_sort_key(group_id) for group_id in key_to_group_ids[key]),
            key_hash(key),
        ),
        reverse=True,
    )
    selected_key = ranked_keys[0]
    selected_group_ids = key_to_group_ids[selected_key]
    if len(selected_group_ids) < 2:
        audit["resolution"] = "same_root_distinct_current_transforms"
        audit["transform_key_groups"] = [
            {
                "transform_key_hash": key_hash(key),
                "group_ids": sorted(key_to_group_ids[key]),
                "group_count": len(key_to_group_ids[key]),
            }
            for key in ranked_keys[:12]
        ]
        return [], "ambiguous_current_transform"
    bucket_items = sorted(
        [item_by_group_id[group_id] for group_id in selected_group_ids],
        key=_group_sort_key,
        reverse=True,
    )
    selected = [group for group, _audit in bucket_items[: max(1, max_selected)]]
    selected_ids = {str(group.group_id) for group in selected}
    dropped_ids = [
        str(group.group_id)
        for group, _audit in selection_pool
        if str(group.group_id) not in selected_ids
    ]
    audit["resolution"] = (
        "same_root_current_transform"
        if len(selected_ids) == len(selection_pool)
        else "max_shared_current_transform_subset"
    )
    audit["selected_transform_key_hash"] = key_hash(selected_key)
    audit["selected_transform_group_ids"] = sorted(selected_ids)
    audit["dropped_transform_group_ids"] = sorted(dropped_ids)
    audit["transform_key_groups"] = [
        {
            "transform_key_hash": key_hash(key),
            "group_ids": sorted(key_to_group_ids[key]),
            "group_count": len(key_to_group_ids[key]),
        }
        for key in ranked_keys[:12]
    ]
    audit["selected_group_ids"] = [str(group.group_id) for group in selected]
    return selected, ""


def _select_compatible_groups(
    passed: List[Tuple[GroupSummary, TriggerCandidateAudit]],
    *,
    max_selected: int,
    case_view: Optional[RuntimeCaseView] = None,
) -> Tuple[List[GroupSummary], str, Dict[str, Any]]:
    audit: Dict[str, Any] = {
        "schema_version": "memory-selection-audit-v1",
        "policy": "pattern_first_root_bias_then_current_transform",
        "passed_group_ids": [str(group.group_id) for group, _audit in passed],
    }
    if not passed:
        return [], "", audit
    passed = sorted(passed, key=_group_sort_key, reverse=True)
    shared_passed = [
        item
        for item in passed
        if item[0].group_type == GroupType.PATTERN
    ]
    non_pattern_passed = [
        item
        for item in passed
        if item[0].group_type != GroupType.PATTERN
    ]

    def _singleton_fallback(reason: str) -> Optional[Tuple[List[GroupSummary], str, Dict[str, Any]]]:
        if not non_pattern_passed:
            return None
        selected = [non_pattern_passed[0][0]]
        audit["resolution"] = "singleton_top1_after_pattern_ambiguity"
        audit["selected_group_ids"] = [str(selected[0].group_id)]
        audit["fallback_reason"] = reason
        return selected, "", audit

    selection_pool = shared_passed or passed
    audit["selection_pool_group_ids"] = [str(group.group_id) for group, _audit in selection_pool]
    audit["selection_pool_kind"] = "pattern" if shared_passed else "singleton_or_mixed"
    buckets: Dict[Tuple[str, ...], List[Tuple[GroupSummary, TriggerCandidateAudit]]] = {}
    for item in selection_pool:
        buckets.setdefault(_action_contract_key(item[0]), []).append(item)
    audit["action_contract_bucket_count"] = len(buckets)
    if len(buckets) > 1:
        root_buckets: Dict[Tuple[str, ...], List[Tuple[GroupSummary, TriggerCandidateAudit]]] = {}
        for item in selection_pool:
            root_buckets.setdefault(_root_bias_contract_key(item[0]), []).append(item)
        audit["root_bias_bucket_count"] = len(root_buckets)
        if len(root_buckets) == 1:
            selected, reason = _select_groups_with_shared_current_transform(
                selection_pool,
                max_selected=max_selected,
                case_view=case_view,
                audit=audit,
            )
            if reason == "ambiguous_current_transform" and not shared_passed:
                selected = [selection_pool[0][0]]
                audit["resolution"] = "singleton_top1_current_transform_fallback"
                audit["selected_group_ids"] = [str(selected[0].group_id)]
                audit["fallback_reason"] = reason
                return selected, "", audit
            if shared_passed and reason == "ambiguous_current_transform":
                fallback = _singleton_fallback(reason)
                if fallback is not None:
                    return fallback
            return selected, reason, audit
        audit["resolution"] = "root_bias_conflict"
        if shared_passed:
            fallback = _singleton_fallback("conflicting_root_bias_contracts")
            if fallback is not None:
                return fallback
        return [], "conflicting_root_bias_contracts", audit
    bucket_items = next(iter(buckets.values()))
    if len(bucket_items) > 1:
        selected, reason = _select_groups_with_shared_current_transform(
            bucket_items,
            max_selected=max_selected,
            case_view=case_view,
            audit=audit,
        )
        if reason == "ambiguous_current_transform" and not shared_passed:
            selected = [bucket_items[0][0]]
            audit["resolution"] = "singleton_top1_current_transform_fallback"
            audit["selected_group_ids"] = [str(selected[0].group_id)]
            audit["fallback_reason"] = reason
            return selected, "", audit
        if shared_passed and reason == "ambiguous_current_transform":
            fallback = _singleton_fallback(reason)
            if fallback is not None:
                return fallback
        return selected, reason, audit
    selected = [group for group, _audit in bucket_items[: max(1, max_selected)]]
    audit["resolution"] = "single_action_contract_bucket"
    audit["selected_group_ids"] = [str(group.group_id) for group in selected]
    return selected, "", audit


def _memory_max_actions(groups: Sequence[GroupSummary]) -> int:
    limits: List[int] = []
    for group in groups:
        contract = _group_trigger_contract(group)
        try:
            limits.append(max(1, int(contract.get("max_actions") or 1)))
        except Exception:
            limits.append(1 if group.group_type == GroupType.SINGLETON else 3)
    return min(limits) if limits else 1


def _rewrite_hint_template(group: GroupSummary) -> str:
    """Return the template intended for rewrite hint instantiation.

    ``instantiation_program.template`` remains the group-level memory text used
    by the compiler for semantic selection. Some replay/promotion flows need a
    safer rewrite-facing template that tells the rewriter to rely only on the
    current compiler actions, not on source-case examples.
    """
    contract = _group_trigger_contract(group)
    action_contract = _payload(contract.get("action_contract"))
    override = str(action_contract.get("rewrite_hint_template") or "").strip()
    if override:
        return override
    # Legacy templates can be source-case-specific. Runtime rewrite can proceed
    # with structured actions only; do not pass old templates into prompts.
    return ""


def _action_argument_text(args: Dict[str, Any], keys: Sequence[str]) -> str:
    values: List[str] = []
    for key in keys:
        value = args.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            parts = [str(item).strip() for item in value if str(item).strip()]
            if parts:
                values.append(f"{key}=" + ",".join(parts[:4]))
            continue
        values.append(f"{key}={str(value).strip()}")
    return "; ".join(values)


def _dependency_action_text(args: Dict[str, Any]) -> str:
    notes: List[str] = []
    cleanup_payloads = [
        item if isinstance(item, dict) else {"op_id": str(item)}
        for item in (args.get("cleanup_edits") or [])
        if item
    ]
    if (
        str(args.get("distinct_dependency") or "") == "SELECT_ENFORCE_DISTINCT"
        or any(str(item.get("kind") or "") == "enforce_distinct" for item in cleanup_payloads)
    ):
        notes.append("also add SELECT DISTINCT to preserve row-result semantics after dropping an output side")
    if any(
        str(item.get("kind") or "") == "drop_unreferenced_join_for_dropped_select_alias"
        for item in cleanup_payloads
    ):
        notes.append("also remove any JOIN block whose alias becomes unreferenced after the selected output side is dropped")
    for step in args.get("repair_program") or []:
        row = _payload(step)
        if not bool(row.get("is_dependency") or False):
            continue
        op = str(row.get("op") or "").upper()
        locus = str(row.get("locus") or "").upper()
        if "DISTINCT" in op:
            notes.append("also preserve/enforce DISTINCT exactly when required by the action")
        elif locus == "JOIN":
            notes.append("also apply the dependent JOIN cleanup carried by the action")
    cleanup_edits = [
        str(item.get("op_id") or item.get("id") or "")
        for item in cleanup_payloads
        if str(item.get("op_id") or item.get("id") or "")
    ]
    if cleanup_edits:
        notes.append("also remove dependent cleanup edit ids=" + ",".join(cleanup_edits[:4]))
    preserve = [str(item) for item in (args.get("preserve_invariants") or []) if str(item)]
    if preserve:
        notes.append("preserve invariants=" + ",".join(preserve[:6]))
    return " ".join(dict.fromkeys(notes))


def _render_action_repair_brief(actions: Sequence[Any]) -> str:
    parts: List[str] = []
    for action in actions or []:
        payload = _payload(action)
        primitive_value = payload.get("primitive")
        primitive = str(getattr(primitive_value, "value", primitive_value) or "")
        args = _payload(payload.get("arguments"))
        if primitive == "ADD_SELECT_SLOT":
            detail = _action_argument_text(args, ("target_columns", "target_output_refs"))
            parts.append(f"Add the target select slot(s) grounded by the action. {detail}".strip())
        elif primitive in {"REPLACE_SELECT_SLOT", "SWITCH_CANONICAL_FIELD"}:
            detail = _action_argument_text(args, ("from_exprs", "current_expr", "target_columns", "target_expr"))
            parts.append(f"Replace the current projection with the target field(s) from the action. {detail}".strip())
        elif primitive in {"DROP_SELECT_SLOT", "DROP_SIDE"}:
            detail = _action_argument_text(args, ("from_exprs", "from_expr", "predicate_ref", "drop_condition"))
            dependency = _dependency_action_text(args)
            parts.append(
                f"Remove only the side selected by the action. {detail} {dependency}".strip()
            )
        elif primitive in {"INSERT_BRIDGE", "REROUTE_FACT"}:
            detail = _action_argument_text(args, ("target_relation_edges", "target_output_refs"))
            preserve = ""
            if bool(args.get("answer_unit_preserve")):
                preserve = (
                    " Preserve the current answer unit, SELECT/COUNT expression, "
                    "and aggregation grain unless another selected action explicitly changes them."
                )
            dependency = _dependency_action_text(args)
            parts.append(
                f"Use the action's bound join path or fact route. {detail}.{preserve} {dependency}".strip()
            )
        elif primitive == "MOVE_CONDITION":
            detail = _action_argument_text(args, ("predicate_ref", "from_scope", "to_scope"))
            parts.append(f"Move the bound predicate to the target scope selected by the action. {detail}".strip())
        elif primitive == "CHANGE_GRAIN":
            detail = _action_argument_text(args, ("source_grain", "target_grain", "target_anchor"))
            parts.append(f"Change the aggregation grain exactly as bound by the action. {detail}".strip())
        elif primitive == "MATERIALIZE_RANKING_OUTPUT":
            detail = _action_argument_text(args, ("ranking_expr", "metric_expr", "target_limit"))
            parts.append(f"Materialize the ranking output defined by the action contract. {detail}".strip())
        else:
            parts.append(f"Apply structured action {primitive} only with its bound arguments.")
    return " ".join(part for part in parts if part).strip()


def _preserve_dependency_clauses_in_hint(raw_hint: str, instantiated_hint: str) -> str:
    """Keep structured dependency obligations if hint instantiation drops them.

    The instantiation LLM may simplify wording, but it must not erase executable
    dependencies emitted by the action compiler. This post-check is contract
    preservation, not case-specific repair logic.
    """
    raw = str(raw_hint or "")
    hint = str(instantiated_hint or "").strip()
    raw_lower = raw.lower()
    hint_lower = hint.lower()
    additions: List[str] = []
    has_join_cleanup_dependency = (
        "unreferenced" in raw_lower and "join" in raw_lower
    )
    has_join_cleanup_hint = bool(
        re.search(
            r"\b(?:remove|drop)\w*\b[^.]{0,80}\bjoin\b|\bjoin\b[^.]{0,80}\b(?:remove|drop)\w*\b",
            hint_lower,
        )
    )
    has_join_preserve_conflict = (
        has_join_cleanup_dependency
        and "join" in hint_lower
        and any(
            token in hint_lower
            for token in ("retain", "retained", "keep", "kept", "preserve", "preserved", "ensure")
        )
        and not has_join_cleanup_hint
    )
    if has_join_preserve_conflict:
        return raw.strip()
    if "select distinct" in raw_lower and "distinct" not in hint_lower:
        additions.append("Also add SELECT DISTINCT when dropping the output side would otherwise duplicate result rows.")
    if has_join_cleanup_dependency and not has_join_cleanup_hint:
        additions.append("Also remove any JOIN block whose alias becomes unreferenced after the selected output side is dropped.")
    if not additions:
        return hint
    return " ".join([hint, *additions]).strip()


def _risk_rank(action: Any) -> int:
    risk = str(getattr(getattr(action, "risk", ""), "value", getattr(action, "risk", ""))).lower()
    return {"low": 0, "medium": 1, "high": 2}.get(risk, 1)


def _scope_value(scope: Any) -> str:
    raw = getattr(scope, "value", scope)
    return str(raw or "").strip().upper()


def _action_primitive_name(action: Any) -> str:
    primitive = getattr(action, "primitive", None)
    return str(getattr(primitive, "value", primitive) or "").strip().upper()


def _append_scope(scopes: List[str], scope: Any) -> None:
    value = _scope_value(scope)
    if value and value in _EDIT_SCOPE_ORDER and value not in scopes:
        scopes.append(value)


def _primitive_required_scopes(primitive: str) -> List[str]:
    """Return the SQL regions intrinsically changed by one action primitive.

    This is an action-schema contract, not a database/case rule. Candidate
    enumeration can add dependency scopes through ``required_edit_scopes`` and
    repair-program dependency steps.
    """
    primitive = str(primitive or "").strip().upper()
    if primitive in {
        "ADD_SELECT_SLOT",
        "REPLACE_SELECT_SLOT",
        "DROP_SELECT_SLOT",
        "SELECT_ENFORCE_DISTINCT",
        "SWITCH_CANONICAL_FIELD",
        "MATERIALIZE_RANKING_OUTPUT",
    }:
        return ["SELECT"]
    if primitive == "INSERT_BRIDGE":
        return ["JOIN"]
    if primitive == "REROUTE_FACT":
        return ["FROM", "JOIN"]
    if primitive in {"DROP_SIDE", "MOVE_CONDITION"}:
        return []
    if primitive == "CHANGE_GRAIN":
        return ["SELECT", "GROUP_BY"]
    return []


def _action_required_scopes(action: Any) -> List[str]:
    args = _payload(getattr(action, "arguments", None))
    scopes: List[str] = []
    for scope in _primitive_required_scopes(_action_primitive_name(action)):
        _append_scope(scopes, scope)
    for scope in args.get("required_edit_scopes") or []:
        _append_scope(scopes, scope)
    for step in args.get("repair_program") or []:
        row = _payload(step)
        if bool(row.get("is_dependency") or False):
            _append_scope(scopes, row.get("locus"))
    if (
        _action_primitive_name(action) == "REROUTE_FACT"
        and args.get("target_output_refs")
        and not bool(args.get("answer_unit_preserve"))
    ):
        _append_scope(scopes, "SELECT")
    return scopes


def _normalize_action_scope_contracts(
    compiler_output: ActionCompilerOutput,
) -> Tuple[ActionCompilerOutput, Dict[str, Any]]:
    """Make action scopes self-consistent before guard/hint emission.

    The compiler still selects code-enumerated candidates. Runtime then
    completes the bounded edit contract from the candidate/action schema so a
    downstream guard never rejects a valid rewrite merely because a required
    dependency scope was omitted from the LLM JSON.
    """
    normalized_actions: List[Any] = []
    notes: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for action in compiler_output.actions or []:
        required = _action_required_scopes(action)
        existing: List[str] = []
        for scope in getattr(action, "allowed_edit_scope", []) or []:
            _append_scope(existing, scope)
        merged = list(existing)
        added: List[str] = []
        for scope in required:
            if scope not in merged:
                merged.append(scope)
                added.append(scope)
        if required and not merged:
            failures.append(
                {
                    "action_id": str(getattr(action, "action_id", "") or ""),
                    "primitive": _action_primitive_name(action),
                    "reason": "required_scope_unavailable",
                }
            )
        if added:
            notes.append(
                {
                    "action_id": str(getattr(action, "action_id", "") or ""),
                    "primitive": _action_primitive_name(action),
                    "added_scopes": added,
                }
            )
        allowed_edit_scope = []
        for scope in _EDIT_SCOPE_ORDER:
            if scope not in merged:
                continue
            try:
                allowed_edit_scope.append(EditScope(scope))
            except Exception:
                continue
        if hasattr(action, "model_copy"):
            action = action.model_copy(update={"allowed_edit_scope": allowed_edit_scope})
        normalized_actions.append(action)

    normalized = compiler_output.model_copy(update={"actions": normalized_actions})
    diag = normalized.schema_diagnostics
    if notes:
        existing_notes = str(getattr(diag, "notes", "") or "")
        extra = "guard_scope_contract_completed:" + json.dumps(
            notes[:12], ensure_ascii=False, sort_keys=True
        )
        diag = diag.model_copy(update={"notes": (existing_notes + " | " + extra).strip(" |")})
        normalized = normalized.model_copy(update={"schema_diagnostics": diag})
    return normalized, {
        "schema_version": "action-scope-contract-v1",
        "completed": notes,
        "failures": failures,
    }


def _action_allowed_scopes(actions: Sequence[Any]) -> List[str]:
    scopes: Set[str] = set()
    for action in actions or []:
        for scope in [
            *list(getattr(action, "allowed_edit_scope", []) or []),
            *_action_required_scopes(action),
        ]:
            value = _scope_value(scope)
            if value:
                scopes.add(value)
    return [scope for scope in _EDIT_SCOPE_ORDER if scope in scopes]


def _anchor_ast_for_guard(case_view: Optional[RuntimeCaseView]) -> Dict[str, Any]:
    if case_view is None:
        return {}
    try:
        from method.EEA.rulebook.common.analysis.structure_family import cached_ast_signature

        return cached_ast_signature(case_view.pred_manifestation.top1_sql or "") or {}
    except Exception:
        return {}


def _guard_table_preserve_set(
    *,
    ast: Dict[str, Any],
    case_view: Optional[RuntimeCaseView],
    allowed_scopes: Set[str],
) -> List[str]:
    """Derive table-preservation constraints from the edit scope contract."""
    tables = [
        str(table).strip()
        for table in (
            ast.get("tables_involved")
            or (case_view.pred_manifestation.tables if case_view is not None else [])
            or []
        )
        if str(table).strip()
    ]
    from_table = str(ast.get("from_table") or "").strip()
    join_tables = [
        str(edge.get("table") or "").strip()
        for edge in (ast.get("join_edges") or [])
        if str(edge.get("table") or "").strip()
    ]
    preserve: List[str] = []
    if "FROM" not in allowed_scopes and from_table:
        preserve.append(from_table)
    if "JOIN" not in allowed_scopes:
        preserve.extend(join_tables or [table for table in tables if table != from_table])
    if "FROM" not in allowed_scopes and "JOIN" not in allowed_scopes:
        preserve.extend(tables)
    return list(dict.fromkeys(preserve))


def _guard_predicate_preserve_set(
    *,
    ast: Dict[str, Any],
    allowed_scopes: Set[str],
) -> List[str]:
    """Derive predicate-preservation constraints from the edit scope contract."""
    predicates: List[str] = []
    if "WHERE" not in allowed_scopes:
        predicates.extend(
            [
                " ".join(str(predicate or "").split())[:240]
                for predicate in (ast.get("predicates") or [])
                if str(predicate or "").strip()
            ]
        )
    if "HAVING" not in allowed_scopes:
        predicates.extend(
            [
                "HAVING " + " ".join(str(predicate or "").split())[:233]
                for predicate in (ast.get("having") or [])
                if str(predicate or "").strip()
            ]
        )
    return list(dict.fromkeys(predicates[:20]))


def build_runtime_rewrite_guard(
    *,
    case_view: Optional[RuntimeCaseView],
    actions: Sequence[Any],
) -> Dict[str, Any]:
    """Build the post-selection rewrite guard exposed to method adapters.

    The guard is derived from the selected anchor SQL and compiler
    ``allowed_edit_scope``. It does not encode database-specific cases.
    """
    action_list = list(actions or [])
    allowed_scope_list = _action_allowed_scopes(action_list)
    allowed_scopes = set(allowed_scope_list)
    audited_forbidden_scope = [
        scope for scope in _EDIT_SCOPE_ORDER if scope not in allowed_scopes
    ]
    ast = _anchor_ast_for_guard(case_view)
    anchor_sql = (
        str(case_view.pred_manifestation.top1_sql or "")
        if case_view is not None
        else ""
    )
    risk = "low"
    for action in action_list:
        action_risk = str(
            getattr(getattr(action, "risk", ""), "value", getattr(action, "risk", "medium"))
            or "medium"
        ).lower()
        if {"low": 0, "medium": 1, "high": 2}.get(action_risk, 1) > {
            "low": 0,
            "medium": 1,
            "high": 2,
        }.get(risk, 0):
            risk = action_risk
    return {
        "schema_version": "runtime-rewrite-guard-v1",
        "scope_guard_mode": "audit_only",
        "allowed_edit_scope": list(_EDIT_SCOPE_ORDER),
        "forbidden_scope": [],
        "must_preserve_tables": [],
        "must_preserve_predicates": [],
        "audited_allowed_edit_scope": allowed_scope_list,
        "audited_forbidden_scope": audited_forbidden_scope,
        "audited_must_preserve_tables": _guard_table_preserve_set(
            ast=ast,
            case_view=case_view,
            allowed_scopes=allowed_scopes,
        ),
        "audited_must_preserve_predicates": _guard_predicate_preserve_set(
            ast=ast,
            allowed_scopes=allowed_scopes,
        ),
        "risk": risk,
        "action_count": len(action_list),
        "action_ids": [
            str(getattr(action, "action_id", "") or "")
            for action in action_list
            if str(getattr(action, "action_id", "") or "")
        ],
        "anchor_sql_hash": hashlib.sha1(anchor_sql.encode("utf-8")).hexdigest()[:16]
        if anchor_sql
        else "",
    }


def _compact_trigger_candidate(audit: Any) -> Dict[str, Any]:
    payload = _payload(audit)
    reasons = [str(item) for item in (payload.get("gate_reasons") or []) if str(item)]
    hard_reasons = [
        str(item) for item in (payload.get("hard_gate_reasons") or []) if str(item)
    ]
    deferred_reasons = [
        str(item)
        for item in (payload.get("deferred_instantiation_reasons") or [])
        if str(item)
    ]
    compiler_reasons = [
        str(item)
        for item in (payload.get("compiler_candidate_reasons") or [])
        if str(item)
    ]
    return {
        "group_id": str(payload.get("group_id") or ""),
        "group_type": str(payload.get("group_type") or ""),
        "gate_passed": bool(payload.get("gate_passed", False)),
        "top_reasons": reasons[:8],
        "source_trigger_passed": bool(payload.get("source_trigger_passed", False)),
        "hard_gate_reasons": hard_reasons[:8],
        "deferred_instantiation_reasons": deferred_reasons[:8],
        "compiler_candidate_reasons": compiler_reasons[:8],
        "required_hit_count": len(payload.get("required_signal_hits") or []),
        "required_miss_count": len(payload.get("required_signal_misses") or []),
        "optional_hit_count": len(payload.get("optional_signal_hits") or []),
        "variant_required_match": bool(payload.get("variant_required_match", False)),
        "binder_dry_run_success": bool(payload.get("binder_dry_run_success", False)),
        "selected_branch_id": str(payload.get("selected_branch_id") or ""),
        "branch_runtime_usable_count": int(payload.get("branch_runtime_usable_count") or 0),
        "branch_blockers": [str(item) for item in (payload.get("branch_blockers") or [])[:6]],
        "bias_recognized": bool(payload.get("bias_recognized", False)),
        "diagnostic_only": bool(payload.get("diagnostic_only", False)),
        "bias_recognition": payload.get("bias_recognition") or {},
        "final_score": payload.get("final_score"),
    }


def _compact_trigger_result(trigger_result: Any) -> Dict[str, Any]:
    payload = _payload(trigger_result)
    candidates = list(payload.get("candidates") or [])
    passed = [row for row in candidates if bool(_payload(row).get("gate_passed", False))]
    blocker_counts: Dict[str, int] = {}
    stage_1_bias_recognized_count = 0
    stage_1_bias_signals_missed_count = 0
    stage_2_branch_ready_count = 0
    stage_2_branch_unbindable_count = 0
    for row in candidates:
        item = _payload(row)
        if item.get("bias_recognized"):
            stage_1_bias_recognized_count += 1
        reasons_all = [
            str(reason)
            for reason in (
                item.get("hard_gate_reasons")
                or item.get("gate_reasons")
                or []
            )
            if str(reason)
        ]
        if any(reason.startswith("bias_recognition_signals_missed") for reason in reasons_all):
            stage_1_bias_signals_missed_count += 1
        if item.get("bias_recognized") and item.get("selected_branch_id"):
            stage_2_branch_ready_count += 1
        if any(reason == "pattern_recognized_branch_unbindable" for reason in reasons_all):
            stage_2_branch_unbindable_count += 1
        if item.get("gate_passed"):
            continue
        reason = str(reasons_all[0] if reasons_all else "unknown")
        blocker_counts[reason] = blocker_counts.get(reason, 0) + 1
    return {
        "strategy_version": str(payload.get("strategy_version") or ""),
        "selected_group_ids": [
            str(_payload(group).get("group_id") or "")
            for group in (payload.get("selected_groups") or [])
            if str(_payload(group).get("group_id") or "")
        ],
        "candidate_count": len(candidates),
        "passed_count": len(passed),
        "selected_branch_ids": {
            str(key): str(value)
            for key, value in (payload.get("selected_branch_ids") or {}).items()
            if str(key) and str(value)
        },
        "branch_selection_audit": payload.get("branch_selection_audit") or {},
        "stage_1_bias_recognized_count": stage_1_bias_recognized_count,
        "stage_1_bias_signals_missed_count": stage_1_bias_signals_missed_count,
        "stage_2_branch_ready_count": stage_2_branch_ready_count,
        "stage_2_branch_unbindable_count": stage_2_branch_unbindable_count,
        "top_candidates": [_compact_trigger_candidate(row) for row in candidates[:12]],
        "blocker_counts": dict(
            sorted(blocker_counts.items(), key=lambda item: (-item[1], item[0]))[:20]
        ),
    }


def _compact_compiler_output(
    compiler_output: Optional[ActionCompilerOutput],
    candidate_sets: Optional[Sequence[Any]],
) -> Dict[str, Any]:
    actions = list(getattr(compiler_output, "actions", []) or []) if compiler_output else []
    empty_reasons: Dict[str, int] = {}
    candidate_counts: Dict[str, int] = {}
    for candidate_set in candidate_sets or []:
        primitive = str(getattr(getattr(candidate_set, "primitive", ""), "value", getattr(candidate_set, "primitive", "")) or "")
        count = len(getattr(candidate_set, "candidates", []) or [])
        if primitive:
            candidate_counts[primitive] = candidate_counts.get(primitive, 0) + count
        reason = str(getattr(candidate_set, "empty_reason", "") or "")
        if reason:
            key = f"{primitive}:{reason}" if primitive else reason
            empty_reasons[key] = empty_reasons.get(key, 0) + 1
    return {
        "action_count": len(actions),
        "actions": [
            {
                "action_id": str(getattr(action, "action_id", "") or ""),
                "source_group_id": str(getattr(action, "source_group_id", "") or ""),
                "source_group_type": str(
                    getattr(getattr(action, "source_group_type", ""), "value", getattr(action, "source_group_type", ""))
                    or ""
                ),
                "primitive": _action_primitive_name(action),
                "selected_candidate_id": str(getattr(action, "selected_candidate_id", "") or ""),
                "allowed_edit_scope": _action_allowed_scopes([action]),
                "selection_origin": str(getattr(action, "selection_origin", "") or ""),
                "risk": str(getattr(getattr(action, "risk", ""), "value", getattr(action, "risk", "")) or ""),
            }
            for action in actions
        ],
        "candidate_counts_by_primitive": dict(sorted(candidate_counts.items())),
        "empty_reason_counts": dict(
            sorted(empty_reasons.items(), key=lambda item: (-item[1], item[0]))[:20]
        ),
        "schema_notes": str(
            getattr(getattr(compiler_output, "schema_diagnostics", None), "notes", "")
            or ""
        )[:800]
        if compiler_output is not None
        else "",
    }


def _runtime_audit_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    compiler_output = payload.get("compiler_output")
    candidate_sets = payload.get("compiler_candidate_sets") or []
    matched = list(payload.get("matched_groups") or [])
    return {
        "schema_version": "runtime-audit-summary-v1",
        "reason": str(payload.get("reason") or ""),
        "rewrite_enabled_reason": str(payload.get("rewrite_enabled_reason") or ""),
        "matched_group_ids": [
            str(getattr(group, "group_id", "") or "") for group in matched
        ],
        "matched_group_types": [
            str(getattr(getattr(group, "group_type", ""), "value", getattr(group, "group_type", "")) or "")
            for group in matched
        ],
        "trigger": _compact_trigger_result(payload.get("trigger_result")),
        "compiler": _compact_compiler_output(compiler_output, candidate_sets),
        "guard": {
            "scope_guard_mode": str((payload.get("guard") or {}).get("scope_guard_mode") or ""),
            "allowed_edit_scope": list((payload.get("guard") or {}).get("allowed_edit_scope") or []),
            "must_preserve_tables": list((payload.get("guard") or {}).get("must_preserve_tables") or [])[:20],
            "must_preserve_predicates": list((payload.get("guard") or {}).get("must_preserve_predicates") or [])[:20],
            "audited_allowed_edit_scope": list((payload.get("guard") or {}).get("audited_allowed_edit_scope") or []),
            "audited_must_preserve_tables": list((payload.get("guard") or {}).get("audited_must_preserve_tables") or [])[:20],
            "audited_must_preserve_predicates": list((payload.get("guard") or {}).get("audited_must_preserve_predicates") or [])[:20],
            "risk": str((payload.get("guard") or {}).get("risk") or ""),
            "action_count": int((payload.get("guard") or {}).get("action_count") or 0),
        },
        "guard_scope_contract": payload.get("guard_scope_contract") or {},
    }


def _finalize_rewrite_plan_payload(
    payload: Dict[str, Any],
    *,
    c0_candidate_count: int,
) -> Dict[str, Any]:
    compiler_output = payload.get("compiler_output")
    actions = (
        list(getattr(compiler_output, "actions", []) or [])
        if compiler_output is not None
        else []
    )
    payload.setdefault(
        "guard",
        build_runtime_rewrite_guard(
            case_view=payload.get("case_view"),
            actions=actions,
        ),
    )
    payload.setdefault(
        "candidate_context",
        {
            "schema_version": "candidate-context-v1",
            "mode": "anchor_rewrite_context_only",
            "anchor_sql_field": "pred_top1_sql",
            "candidate_context_field": "c0_candidates",
            "candidate_count": int(c0_candidate_count),
            "rewrite_target_count": 1,
            "notes": (
                "c0_candidates/pre_eea_candidates summarize the pre-EEA "
                "candidate context; only pred_top1_sql/selected_sql is the "
                "rewrite anchor."
            ),
        },
    )
    payload.setdefault("runtime_audit_summary", _runtime_audit_summary(payload))
    return payload


def _enforce_action_count_contract(
    compiler_output: ActionCompilerOutput,
    matched_groups: Sequence[GroupSummary],
) -> ActionCompilerOutput:
    """Enforce memory-object action count after LLM selection.

    The compiler prompt asks the LLM to select alternatives, but runtime must
    still enforce the contract mechanically. This is a generic safety layer, not
    a case-specific rule.
    """
    max_actions = _memory_max_actions(matched_groups)
    actions = list(compiler_output.actions or [])
    if len(actions) <= max_actions:
        return compiler_output
    if any(getattr(action, "used_escape_hatch", False) for action in actions):
        return compiler_output

    ranked = sorted(
        enumerate(actions),
        key=lambda item: (
            -float(getattr(item[1], "priority", 0.0) or 0.0),
            _risk_rank(item[1]),
            item[0],
        ),
    )
    keep_indexes = sorted(idx for idx, _action in ranked[:max_actions])
    kept = [actions[idx] for idx in keep_indexes]
    dropped = [
        getattr(action, "action_id", "")
        or str(getattr(action, "selected_candidate_id", ""))
        for idx, action in enumerate(actions)
        if idx not in keep_indexes
    ]
    diag = compiler_output.schema_diagnostics
    existing_notes = str(getattr(diag, "notes", "") or "")
    extra = (
        f"action_count_contract_enforced: kept={len(kept)} "
        f"max_actions={max_actions}; dropped={dropped}"
    )
    diag = diag.model_copy(update={"notes": (existing_notes + " | " + extra).strip(" |")})
    return compiler_output.model_copy(update={"actions": kept, "schema_diagnostics": diag})


def trigger_memory_objects(
    *,
    library: LibraryStateV2,
    case_view: RuntimeCaseView,
    db_id: str,
    max_selected: int = 2,
    allow_self_singleton_replay: bool = False,
) -> TriggerResult:
    """Layered runtime trigger with auditable gate reasons.

    This trigger uses boolean contracts: required signals must all match,
    negative signals must not match, and slots must be resolvable. Legacy
    question/pred tag overlap is retained only in audit fields.
    """
    if library.db_id != db_id:
        return TriggerResult(strategy_version="v2-contract-trigger-20260427")

    q_tags = _case_question_tags(case_view)
    p_tags = _case_pred_tags(case_view)
    audits: List[TriggerCandidateAudit] = []
    passed: List[Tuple[GroupSummary, TriggerCandidateAudit]] = []

    for source in (library.patterns, library.singletons):
        for g in source:
            audit = _gate_group(
                group=g,
                case_view=case_view,
                q_tags=q_tags,
                p_tags=p_tags,
                allow_self_singleton_replay=allow_self_singleton_replay,
            )
            audits.append(audit)
            if audit.gate_passed:
                selected_group = g
                if (
                    g.group_type == GroupType.PATTERN
                    and getattr(audit, "selected_branch_id", None)
                ):
                    branch = next(
                        (
                            row
                            for row in _runtime_branch_rows(g)
                            if _runtime_branch_id(row) == str(audit.selected_branch_id)
                        ),
                        None,
                    )
                    if branch is not None:
                        selected_group = _filter_group_to_runtime_branch(g, branch)
                passed.append((selected_group, audit))

    selected, selection_reason, selection_audit = _select_compatible_groups(
        passed,
        max_selected=max_selected,
        case_view=case_view,
    )
    if selection_reason:
        selected_ids = {group.group_id for group, _audit in passed}
        updated_audits: List[TriggerCandidateAudit] = []
        for audit in audits:
            if audit.group_id in selected_ids and audit.gate_passed:
                audit = audit.model_copy(
                    update={
                        "gate_passed": False,
                        "gate_reasons": list(audit.gate_reasons) + [selection_reason],
                        "hard_gate_reasons": list(audit.hard_gate_reasons)
                        + [selection_reason],
                        "signal_compat": 0.0,
                        "structural_compat": 0.0,
                        "final_score": 0.0,
                    }
                )
            updated_audits.append(audit)
        audits = updated_audits
    final_selected_ids = {str(group.group_id) for group in selected}
    branch_selection_audit: Dict[str, Any] = {"_memory_selection": selection_audit}
    branch_selection_audit.update(
        {
            audit.group_id: {
                "selected_branch_id": getattr(audit, "selected_branch_id", None),
                "matched_branch_ids": list(getattr(audit, "matched_branch_ids", []) or []),
                "branch_runtime_usable_count": int(
                    getattr(audit, "branch_runtime_usable_count", 0) or 0
                ),
                "branch_blockers": list(getattr(audit, "branch_blockers", []) or []),
                "branch_match_audit": list(getattr(audit, "branch_match_audit", []) or []),
            }
            for audit in audits
            if getattr(audit, "branch_match_audit", None)
        }
    )
    return TriggerResult(
        strategy_version="v2-contract-trigger-20260427",
        selected_groups=selected,
        selected_branch_ids={
            audit.group_id: str(audit.selected_branch_id)
            for _group, audit in passed
            if getattr(audit, "selected_branch_id", None)
            and str(audit.group_id) in final_selected_ids
        },
        branch_selection_audit=branch_selection_audit,
        candidates=sorted(
            audits,
            key=lambda a: (a.gate_passed, a.final_score, a.group_id),
            reverse=True,
        ),
    )


def retrieve_groups(
    *,
    library: LibraryStateV2,
    case_view: RuntimeCaseView,
    db_id: str,
    min_tag_overlap: float = 0.5,
    max_groups: int = 1,
) -> List[GroupSummary]:
    """Backward-compatible wrapper around ``trigger_memory_objects``."""
    _ = min_tag_overlap
    result = trigger_memory_objects(
        library=library,
        case_view=case_view,
        db_id=db_id,
        max_selected=max_groups,
    )
    return result.selected_groups


def _filter_candidate_sets_to_selected_branches(
    candidate_sets: Sequence[Any],
    matched_groups: Sequence[GroupSummary],
) -> List[Any]:
    branch_by_group: Dict[str, Dict[str, Any]] = {}
    for group in matched_groups or []:
        if group.group_type != GroupType.PATTERN:
            continue
        branches = _runtime_branch_rows(group)
        if len(branches) == 1:
            branch_by_group[str(group.group_id)] = branches[0]
    if not branch_by_group:
        return list(candidate_sets or [])

    filtered_sets: List[Any] = []
    for candidate_set in candidate_sets or []:
        original_candidates = list(getattr(candidate_set, "candidates", []) or [])
        kept = [
            candidate
            for candidate in original_candidates
            if str(getattr(candidate, "source_group_id", "") or "") not in branch_by_group
            or _branch_candidate_matches(
                candidate,
                branch_by_group[str(getattr(candidate, "source_group_id", "") or "")],
            )
        ]
        if len(kept) == len(original_candidates):
            filtered_sets.append(candidate_set)
            continue
        empty_reason = getattr(candidate_set, "empty_reason", None)
        if original_candidates and not kept:
            empty_reason = "selected_runtime_branch_no_candidates"
        if hasattr(candidate_set, "model_copy"):
            filtered_sets.append(
                candidate_set.model_copy(
                    update={"candidates": kept, "empty_reason": empty_reason}
                )
            )
        else:
            candidate_set.candidates = kept
            candidate_set.empty_reason = empty_reason
            filtered_sets.append(candidate_set)
    return filtered_sets


# =============================================================================
# Runtime entry point
# =============================================================================


def prepare_rewrite_plan(
    *,
    db_id: str,
    case_id: str,
    question: str,
    evidence: str,
    pred_top1_sql: str,
    c0_candidates: Optional[Sequence[Any]] = None,
    library: LibraryStateV2,
    db_path: str,
    database_dir: Optional[str] = None,
    allow_self_singleton_replay: bool = False,
) -> Dict[str, Any]:
    """Retrieval + Compiler + Hint Instantiation，一个 case 跑一次。产 actions + hint
    给后续 per-candidate rewrite 复用。

    返回 dict：
    {
      "case_view": RuntimeCaseView or None,
      "matched_groups": List[GroupSummary],
      "compiler_output": ActionCompilerOutput or None,
      "instantiated_hint": str,        # Phase 1.5e 新增；不可用时为 ""
      "raw_hint": str,                  # 来自 top-1 matched group 的 template
      "hint_applicable": bool,          # instantiation 判定 raw hint 是否可移植到 current case
      "hint_instantiation_notes": str,
      "reason": str,                    # "passthrough_no_match" / "passthrough_no_action" / "ready"
    }

    语义对齐 v1：v1 的 trigger 阶段一次性产 hint，后续 per-candidate rewrite 复用。
    v2 拆分同理：retrieval + compiler + instantiation 一次出 actions+hint，
    per-candidate rewrite 只调 rewrite LLM。
    """
    c0_candidate_sqls = _candidate_sql_texts(c0_candidates)
    c0_candidate_count = len(c0_candidate_sqls)

    def finish(payload: Dict[str, Any]) -> Dict[str, Any]:
        return _finalize_rewrite_plan_payload(
            payload,
            c0_candidate_count=c0_candidate_count,
        )

    try:
        access = SqliteDBSchemaAccess(db_id=db_id, db_path=db_path, database_dir=database_dir)

        case_view = build_runtime_case_view(
            db_id=db_id,
            case_id=case_id,
            question=question,
            evidence=evidence or "",
            pred_top1_sql=pred_top1_sql,
            c0_candidate_sqls=c0_candidate_sqls,
            access=access,
            candidate_set_size=len(c0_candidate_sqls or [pred_top1_sql]),
        )

        trigger_result = trigger_memory_objects(
            library=library,
            case_view=case_view,
            db_id=db_id,
            allow_self_singleton_replay=allow_self_singleton_replay,
        )
        matched = trigger_result.selected_groups
        memory_tables = _memory_schema_tables(matched)
        if memory_tables:
            case_view = build_runtime_case_view(
                db_id=db_id,
                case_id=case_id,
                question=question,
                evidence=evidence or "",
                pred_top1_sql=pred_top1_sql,
                c0_candidate_sqls=c0_candidate_sqls,
                access=access,
                t_mem=memory_tables,
                allow_two_hop_justifications=[group.group_id for group in matched],
                candidate_set_size=len(c0_candidate_sqls or [pred_top1_sql]),
            )
    except Exception as exc:
        # 整个 retrieval 链路异常 → 降级为 passthrough，保持返回 dict shape 一致
        return finish({
            "case_view": None,
            "matched_groups": [],
            "trigger_result": TriggerResult(strategy_version="v2-contract-trigger-20260427"),
            "compiler_candidate_sets": [],
            "compiler_schema_diagnostics_pre": None,
            "compiler_output": None,
            "instantiated_hint": "",
            "raw_hint": "",
            "repair_brief": "",
            "hint_applicable": False,
            "rewrite_allowed": False,
            "rewrite_enabled_reason": "retrieval_exception",
            "hint_instantiation_notes": f"retrieval_error:{type(exc).__name__}: {exc}",
            "reason": f"retrieval_exception:{type(exc).__name__}",
        })

    if not matched:
        return finish({
            "case_view": case_view,
            "matched_groups": [],
            "trigger_result": trigger_result,
            "compiler_candidate_sets": [],
            "compiler_schema_diagnostics_pre": None,
            "compiler_output": None,
            "instantiated_hint": "",
            "raw_hint": "",
            "repair_brief": "",
            "hint_applicable": False,
            "rewrite_allowed": False,
            "rewrite_enabled_reason": "no_trigger_match",
            "hint_instantiation_notes": "",
            "reason": "passthrough_no_match",
        })

    try:
        compiler_candidate_sets, compiler_pre_diag = enumerate_candidates(
            case_view=case_view, memory_objects=matched
        )
        compiler_candidate_sets = _filter_candidate_sets_to_selected_branches(
            compiler_candidate_sets,
            matched,
        )
        has_compiler_candidates = any(cs.candidates for cs in compiler_candidate_sets)
        if not has_compiler_candidates:
            return finish({
                "case_view": case_view,
                "matched_groups": matched,
                "trigger_result": trigger_result,
                "compiler_candidate_sets": compiler_candidate_sets,
                "compiler_schema_diagnostics_pre": compiler_pre_diag,
                "compiler_output": ActionCompilerOutput(schema_diagnostics=compiler_pre_diag),
                "instantiated_hint": "",
                "raw_hint": "",
                "repair_brief": "",
                "hint_applicable": False,
                "rewrite_allowed": False,
                "rewrite_enabled_reason": "compiler_no_candidates",
                "hint_instantiation_notes": "",
                "reason": "passthrough_no_action",
            })
        compiler_output: ActionCompilerOutput = run_action_compiler(
            runtime_case_view=case_view,
            memory_objects=matched,
            precomputed_candidate_sets=compiler_candidate_sets,
            precomputed_schema_diagnostics=compiler_pre_diag,
            trigger_result=trigger_result,
        )
        compiler_output = _enforce_action_count_contract(compiler_output, matched)
        compiler_output, guard_scope_contract = _normalize_action_scope_contracts(
            compiler_output
        )
    except Exception as exc:
        return finish({
            "case_view": case_view,
            "matched_groups": matched,
            "trigger_result": trigger_result,
            "compiler_candidate_sets": [],
            "compiler_schema_diagnostics_pre": None,
            "compiler_output": None,
            "instantiated_hint": "",
            "raw_hint": "",
            "repair_brief": "",
            "hint_applicable": False,
            "rewrite_allowed": False,
            "rewrite_enabled_reason": "compiler_exception",
            "hint_instantiation_notes": f"compiler_error:{type(exc).__name__}: {exc}",
            "reason": f"compiler_exception:{type(exc).__name__}",
        })
    if (guard_scope_contract.get("failures") or []):
        return finish({
            "case_view": case_view,
            "matched_groups": matched,
            "trigger_result": trigger_result,
            "compiler_candidate_sets": compiler_candidate_sets,
            "compiler_schema_diagnostics_pre": compiler_pre_diag,
            "compiler_output": compiler_output,
            "instantiated_hint": "",
            "raw_hint": "",
            "repair_brief": "",
            "hint_applicable": False,
            "rewrite_allowed": False,
            "rewrite_enabled_reason": "guard_scope_inconsistent",
            "guard_scope_contract": guard_scope_contract,
            "hint_instantiation_notes": "guard_scope_inconsistent",
            "reason": "passthrough_guard_scope_inconsistent",
        })
    if not compiler_output.actions:
        return finish({
            "case_view": case_view,
            "matched_groups": matched,
            "trigger_result": trigger_result,
            "compiler_candidate_sets": compiler_candidate_sets,
            "compiler_schema_diagnostics_pre": compiler_pre_diag,
            "compiler_output": compiler_output,
            "instantiated_hint": "",
            "raw_hint": "",
            "repair_brief": "",
            "hint_applicable": False,
            "rewrite_allowed": False,
            "guard_scope_contract": guard_scope_contract,
            "rewrite_enabled_reason": "compiler_no_actions",
            "hint_instantiation_notes": "",
            "reason": "passthrough_no_action",
        })
    if compiler_output.escape_hatch_log or any(a.used_escape_hatch for a in compiler_output.actions):
        return finish({
            "case_view": case_view,
            "matched_groups": matched,
            "trigger_result": trigger_result,
            "compiler_candidate_sets": compiler_candidate_sets,
            "compiler_schema_diagnostics_pre": compiler_pre_diag,
            "compiler_output": compiler_output,
            "instantiated_hint": "",
            "raw_hint": "",
            "repair_brief": "",
            "hint_applicable": False,
            "rewrite_allowed": False,
            "guard_scope_contract": guard_scope_contract,
            "rewrite_enabled_reason": "escape_hatch_requires_review",
            "hint_instantiation_notes": "",
            "reason": "passthrough_escape_hatch",
        })
    if any(g.group_type == GroupType.SINGLETON for g in matched) and len(compiler_output.actions) > 1:
        return finish({
            "case_view": case_view,
            "matched_groups": matched,
            "trigger_result": trigger_result,
            "compiler_candidate_sets": compiler_candidate_sets,
            "compiler_schema_diagnostics_pre": compiler_pre_diag,
            "compiler_output": compiler_output,
            "instantiated_hint": "",
            "raw_hint": "",
            "repair_brief": "",
            "hint_applicable": False,
            "rewrite_allowed": False,
            "guard_scope_contract": guard_scope_contract,
            "rewrite_enabled_reason": "singleton_action_count_gt_one",
            "hint_instantiation_notes": "",
            "reason": "passthrough_action_count",
        })

    # Primary hint is code-rendered from structured actions. Hint instantiation
    # may rewrite it for readability, but must not add information.
    raw_hint = _render_action_repair_brief(compiler_output.actions)

    instantiated_hint = ""
    hint_applicable = False
    hint_notes = ""
    rewrite_allowed = True
    if raw_hint:
        try:
            inst = run_hint_instantiation(
                raw_hint=raw_hint,
                question=question,
                evidence=evidence or "",
                pred_top1_sql=pred_top1_sql,
                local_schema_view=case_view.local_schema_view,
                actions=compiler_output.actions,
            )
            instantiated_hint = _preserve_dependency_clauses_in_hint(
                raw_hint,
                inst["instantiated_hint"],
            )
            hint_applicable = inst["applicable"]
            hint_notes = inst["instantiation_notes"]
            rewrite_allowed = bool(inst.get("rewrite_allowed", True))
        except Exception as exc:
            # Instantiation 失败时不把 code-rendered repair brief 直接送入
            # rewrite。后面的 repair_brief gate 会让该 case passthrough。
            hint_applicable = False
            rewrite_allowed = True
            hint_notes = f"instantiation_error:{type(exc).__name__}"

    if not raw_hint:
        return finish({
            "case_view": case_view,
            "matched_groups": matched,
            "trigger_result": trigger_result,
            "compiler_candidate_sets": compiler_candidate_sets,
            "compiler_schema_diagnostics_pre": compiler_pre_diag,
            "compiler_output": compiler_output,
            "instantiated_hint": "",
            "raw_hint": "",
            "repair_brief": "",
            "hint_applicable": False,
            "rewrite_allowed": True,
            "guard_scope_contract": guard_scope_contract,
            "rewrite_enabled_reason": "ready_with_actions_no_repair_brief",
            "hint_instantiation_notes": "structured actions did not yield a repair brief",
            "reason": "ready",
        })
    if not hint_applicable or not instantiated_hint:
        return finish({
            "case_view": case_view,
            "matched_groups": matched,
            "trigger_result": trigger_result,
            "compiler_candidate_sets": compiler_candidate_sets,
            "compiler_schema_diagnostics_pre": compiler_pre_diag,
            "compiler_output": compiler_output,
            "instantiated_hint": "",
            "raw_hint": raw_hint,
            "repair_brief": "",
            "hint_applicable": False,
            "rewrite_allowed": True,
            "guard_scope_contract": guard_scope_contract,
            "rewrite_enabled_reason": "ready_with_actions_hint_not_applicable",
            "hint_instantiation_notes": hint_notes,
            "reason": "ready",
        })

    if not rewrite_allowed:
        return finish({
            "case_view": case_view,
            "matched_groups": matched,
            "trigger_result": trigger_result,
            "compiler_candidate_sets": compiler_candidate_sets,
            "compiler_schema_diagnostics_pre": compiler_pre_diag,
            "compiler_output": compiler_output,
            "instantiated_hint": "",
            "raw_hint": raw_hint,
            "repair_brief": "",
            "hint_applicable": False,
            "hint_instantiation_notes": hint_notes,
            "rewrite_allowed": False,
            "guard_scope_contract": guard_scope_contract,
            "rewrite_enabled_reason": "brief_action_conflict",
            "reason": "passthrough_unsafe_hint",
        })

    return finish({
        "case_view": case_view,
        "matched_groups": matched,
        "trigger_result": trigger_result,
        "compiler_candidate_sets": compiler_candidate_sets,
        "compiler_schema_diagnostics_pre": compiler_pre_diag,
        "compiler_output": compiler_output,
        "instantiated_hint": instantiated_hint,
        "raw_hint": raw_hint,
        "repair_brief": instantiated_hint,
        "hint_applicable": hint_applicable,
        "hint_instantiation_notes": hint_notes,
        "rewrite_allowed": True,
        "guard_scope_contract": guard_scope_contract,
        "rewrite_enabled_reason": "ready_with_repair_brief_and_actions",
        "reason": "ready",
    })


def rewrite_one_candidate(
    *,
    question: str,
    evidence: str,
    pred_sql: str,
    compiler_output: ActionCompilerOutput,
    local_schema_view: LocalSchemaView,
    natural_language_hint: str = "",
) -> Dict[str, Any]:
    """只跑 rewrite LLM（per-candidate）。复用 prepare_rewrite_plan 的 compiler_output / local_schema_view。

    返回 dict：
    {
      "rewrite_sql": str or None,
      "action_realization_traces": List[dict],
      "contract_steps_applied": List[str],
    }
    """
    rewrite_result = run_memory_rewrite(
        question=question,
        evidence=evidence or "",
        c0_top1_sql=pred_sql,
        actions=compiler_output.actions,
        local_schema_view=local_schema_view,
        natural_language_hint=natural_language_hint,
    )
    return {
        "rewrite_sql": rewrite_result["rewrite_sql"],
        "action_realization_traces": [
            t.model_dump() if hasattr(t, "model_dump") else t
            for t in rewrite_result["action_realization_traces"]
        ],
        "dependency_repairs_applied": list(rewrite_result["dependency_repairs_applied"]),
        "contract_steps_applied": list(rewrite_result.get("contract_steps_applied") or []),
        "rewrite_contract": rewrite_result.get("rewrite_contract") or {},
        "prompt_payload_audit": rewrite_result.get("prompt_payload_audit") or {},
    }


def contract_steps_changed_sql(contract_steps_applied: Sequence[Any]) -> bool:
    """Return whether recorded deterministic contract steps changed SQL.

    Contract logs can include audit notes such as "already present" or
    "not applied". Those should not be counted as deterministic realization
    gains; only notes that describe an applied/drop/restored SQL edit do.
    """
    for raw in contract_steps_applied or []:
        note = str(raw or "").strip().lower()
        if not note:
            continue
        if "already present" in note or "not applied" in note:
            continue
        if (
            " applied" in note
            or note.startswith("reroute_fact ")
            or " dropped " in note
            or note.startswith("scope_enforced:")
        ):
            return True
    return False


def rewrite_realization_origin_from_result(result: Dict[str, Any]) -> str:
    if result.get("dependency_repairs_applied") or contract_steps_changed_sql(
        result.get("contract_steps_applied") or []
    ):
        return "post_rewrite_contract_realization"
    return "memory_rewrite_llm"


def run_memory_rewrite_runtime(
    *,
    db_id: str,
    case_id: str,
    question: str,
    evidence: str,
    pred_top1_sql: str,
    c0_candidates: Optional[Sequence[Any]] = None,
    library: LibraryStateV2,
    db_path: str,
    database_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """答案-盲 runtime 入口（单 case 单 candidate 的组合形式，保留为 backward-compat）。

    内部复用 prepare_rewrite_plan + rewrite_one_candidate。per-candidate pool loop 应直接
    调 prepare + rewrite_one_candidate 避免 compiler LLM 被重复调用。
    """
    plan = prepare_rewrite_plan(
        db_id=db_id,
        case_id=case_id,
        question=question,
        evidence=evidence,
        pred_top1_sql=pred_top1_sql,
        c0_candidates=c0_candidates,
        library=library,
        db_path=db_path,
        database_dir=database_dir,
    )
    reason = plan["reason"]
    matched = plan["matched_groups"]
    compiler_output = plan["compiler_output"]

    if reason != "ready":
        return {
            "rewrite_sql": None,
            "matched_group_ids": [g.group_id for g in matched],
            "compiler_output": compiler_output,
            "action_realization_traces": [],
            "dependency_repairs_applied": [],
            "contract_steps_applied": [],
            "reason": reason,
        }

    # Phase 1.5e：用 plan 里 instantiation 后的 hint（针对 current case 改写过），不是 raw template
    _hint = plan.get("repair_brief", "") or plan.get("instantiated_hint", "") or ""

    rewrite_result = rewrite_one_candidate(
        question=question,
        evidence=evidence or "",
        pred_sql=pred_top1_sql,
        compiler_output=compiler_output,
        local_schema_view=plan["case_view"].local_schema_view,
        natural_language_hint=_hint,
    )
    return {
        "rewrite_sql": rewrite_result["rewrite_sql"],
        "matched_group_ids": [g.group_id for g in matched],
        "compiler_output": compiler_output,
        "action_realization_traces": rewrite_result["action_realization_traces"],
        "dependency_repairs_applied": rewrite_result["dependency_repairs_applied"],
        "contract_steps_applied": rewrite_result.get("contract_steps_applied") or [],
        "reason": "rewritten",
    }


__all__ = [
    "build_runtime_case_view",
    "build_runtime_rewrite_guard",
    "retrieve_groups",
    "prepare_rewrite_plan",
    "rewrite_one_candidate",
    "contract_steps_changed_sql",
    "rewrite_realization_origin_from_result",
    "run_memory_rewrite_runtime",
]
