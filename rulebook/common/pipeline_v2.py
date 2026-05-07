"""End-to-end vertical slice pipeline for Phase 1.2.

管线：
    raw case (question/evidence/pred/gold + db) →
        1. code_preprocess_v2.preprocess_case  (deterministic)
        2. _build_runtime_case_view              (deterministic, 把 candidate tags 收束成 QuestionContract)
        3. llm_nodes_v2.run_wrong_case_auditor   (LLM)
        4. llm_nodes_v2.run_error_instance_extractor  (LLM)
    → ErrorInstanceV2 (+ 中间产物供 Claude 开发期直接对比审查)

这是 Phase 1 的第一个 vertical slice。跑通后可以把 ErrorInstanceV2 / CaseAudit /
RuntimeCaseView 一起 dump 给 Claude，逐字段对照人工清单做方向性判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .code_preprocess_v2 import (
    build_case_signal_bundle,
    build_case_signal_view,
    extract_candidate_pred_tags,
    preprocess_case,
)
from .data_structures_v2 import (
    ActionCompilerOutput,
    CandidateSetSummary,
    CaseAudit,
    ErrorInstanceV2,
    ExecutionComparison,
    PredManifestation,
    QuestionContract,
    RuntimeCaseView,
)
from .db_schema_access_v2 import SqliteDBSchemaAccess
from .execution_compare_v2 import run_execution_comparison, serialize_execution_comparison
from .llm_nodes_v2 import (
    run_action_compiler,
    run_error_instance_extractor,
    run_wrong_case_auditor,
)
from .vocabulary_v2 import (
    AnswerSlotType,
    EXECUTION_ONLY_PRED_TAGS,
    GrainType,
    OpType,
    PredManifestationType,
    QuestionTargetRole,
    RouteCue,
)


# =============================================================================
# RuntimeCaseView 组装（纯 code，从 candidate tags 分桶 + AST 字段填充）
# =============================================================================


def _tags_filtered(tags: List[str], enum_cls) -> List:
    values = {m.value for m in enum_cls}
    out = []
    for t in tags:
        if t in values:
            try:
                out.append(enum_cls(t))
            except Exception:
                pass
    return out


def _build_runtime_case_view(
    *,
    db_id: str,
    case_id: str,
    question: str,
    evidence: str,
    code_prepared: dict,
) -> RuntimeCaseView:
    pred_ast = code_prepared["pred_ast"]
    candidate_question_tags: List[str] = code_prepared["candidate_question_tags"]
    candidate_pred_tags: List[str] = code_prepared["candidate_pred_tags"]

    # question_contract：分桶 candidate tags
    target_roles = _tags_filtered(candidate_question_tags, QuestionTargetRole)
    if not target_roles:
        target_roles = [QuestionTargetRole.PRIMARY_ENTITY]

    slot_types = _tags_filtered(candidate_question_tags, AnswerSlotType)
    operators = _tags_filtered(candidate_question_tags, OpType)
    route_cues = _tags_filtered(candidate_question_tags, RouteCue)
    grains = _tags_filtered(candidate_question_tags, GrainType)
    grain = grains[0] if grains else None

    question_contract = QuestionContract(
        target_entity_roles=target_roles,
        answer_slot_types=slot_types,
        grain=grain,
        operators=operators,
        route_cues=route_cues,
        specific_entity_mentions=[],  # Phase 1.2 不做自动抽取
        summary=None,
    )

    # pred_manifestation
    pred_tables = list(pred_ast.get("tables_involved") or [])
    pred_columns: List[str] = []
    for expr in pred_ast.get("select_expressions") or []:
        pred_columns.append(str(expr))

    runtime_pred_tags = extract_candidate_pred_tags(
        pred_ast=pred_ast,
        gold_ast={},
        structure_flags={},
        execution_comparison=None,
        runtime_mode=True,
    )
    runtime_pred_tags = [
        tag for tag in runtime_pred_tags if tag not in EXECUTION_ONLY_PRED_TAGS
    ]
    manifestation_types = _tags_filtered(runtime_pred_tags, PredManifestationType)
    pred_manifestation = PredManifestation(
        top1_sql=_truncate(code_prepared.get("_pred_sql", "")),
        tables=pred_tables,
        columns=pred_columns,
        select_shape=_summarize_shape(pred_ast.get("select_shape") or {}),
        group_order_shape=_summarize_group_order(pred_ast),
        manifestation_types=manifestation_types,
        # RuntimeCaseView is answer-blind. Gold/execution-derived flags remain
        # in code_prepared / execution_comparison for offline extractor use.
        structure_flags={},
        summary=None,
    )

    case_signal_view = build_case_signal_view(
        db_id=db_id,
        case_id=case_id,
        question=question,
        evidence=evidence,
        pred_sql=code_prepared.get("_pred_sql", ""),
        pred_ast=pred_ast,
        local_schema_view=code_prepared["local_schema_view"],
        local_schema_diagnostics=code_prepared["local_schema_diagnostics"],
        candidate_question_tags=candidate_question_tags,
        candidate_pred_tags=runtime_pred_tags,
        structure_flags={},
    )
    case_signal_bundle = build_case_signal_bundle(
        db_id=db_id,
        case_id=case_id,
        case_signal_view=case_signal_view,
        delta_signature=None,
        candidate_question_tags=candidate_question_tags,
        candidate_pred_tags=runtime_pred_tags,
        execution_comparison=None,
        legacy_signature="",
        legacy_signature_dict={},
    )

    return RuntimeCaseView(
        db_id=db_id,
        case_id=case_id,
        question=question,
        evidence=evidence or None,
        question_contract=question_contract,
        pred_manifestation=pred_manifestation,
        candidate_set_summary=CandidateSetSummary(size=1, top1_hash="", beam_present=False),
        local_schema_view=code_prepared["local_schema_view"],
        # This is rebuilt from pred-only inputs. The answer-aware signal bundle
        # from preprocess_case remains available separately in code_prepared.
        case_signal_view=case_signal_view,
        case_signal_bundle=case_signal_bundle,
    )


def _truncate(s: str, limit: int = 2000) -> str:
    if not s:
        return ""
    if len(s) <= limit:
        return s
    return s[:limit] + "..."


def _summarize_shape(shape: dict) -> Optional[str]:
    if not shape:
        return None
    parts = []
    arity = shape.get("output_arity")
    if arity is not None:
        parts.append(f"arity={arity}")
    if shape.get("has_distinct"):
        parts.append("distinct")
    if shape.get("has_aggregate"):
        parts.append("aggregate")
    return ",".join(parts) or None


def _summarize_group_order(pred_ast: dict) -> Optional[str]:
    group_keys = pred_ast.get("group_by_keys") or []
    order_by = pred_ast.get("order_by") or []
    parts = []
    if group_keys:
        parts.append(f"group_by={len(group_keys)}")
    if order_by:
        parts.append(f"order_by={len(order_by)}")
    limit = pred_ast.get("limit")
    if limit is not None:
        parts.append(f"limit={limit}")
    return ",".join(parts) or None


# =============================================================================
# 主入口
# =============================================================================


@dataclass
class ErrorInstancePipelineOutput:
    """vertical slice 的完整产物，便于 Claude 直接对比审查。"""

    error_instance: ErrorInstanceV2
    runtime_case_view: RuntimeCaseView
    case_audit: CaseAudit
    code_prepared_summary: dict
    compiler_output: Optional[ActionCompilerOutput] = None
    """Phase 1.3 起：可选的 Action Compiler 产物。若 caller 传
    ``run_compiler=True``，pipeline 会把 error_instance 当单例 memory 跑 Compiler。"""


def run_error_instance_pipeline(
    *,
    db_id: str,
    case_id: str,
    question: str,
    evidence: str,
    pred_sql: str,
    gold_sql: str,
    db_path: str,
    database_dir: Optional[str] = None,
    execution_result: str = "",
    execution_comparison: Optional[ExecutionComparison] = None,
    skip_auto_execution: bool = False,
    run_compiler: bool = False,
) -> ErrorInstancePipelineOutput:
    """跑完整 vertical slice：RuntimeCaseView → CaseAudit → ErrorInstanceV2。

    默认会自动跑 pred/gold 的 execution 对比并流入 Auditor + Extractor。如果 caller
    已经预先算好 ``execution_comparison`` 可以显式传入，pipeline 会跳过内部计算。
    设置 ``skip_auto_execution=True`` 可完全禁用（offline test 用）。
    """

    # 1. Execution comparison（离线组件，runtime 禁止使用）
    # 先于 preprocess，以便 execution-driven deterministic tags
    # （SELECT_ARITY_MISMATCH / SELECT_ROLE_DTYPE_MISMATCH）能进入 candidate_pred_tags
    if execution_comparison is None and not skip_auto_execution:
        execution_comparison = run_execution_comparison(
            db_path=db_path, pred_sql=pred_sql, gold_sql=gold_sql
        )
    if not execution_result and execution_comparison is not None:
        execution_result = serialize_execution_comparison(execution_comparison)

    # 2. code preprocess（接收 execution_comparison 派生 deterministic tags）
    access = SqliteDBSchemaAccess(
        db_id=db_id, db_path=db_path, database_dir=database_dir
    )
    code_prepared = preprocess_case(
        db_id=db_id,
        case_id=case_id,
        question=question,
        evidence=evidence,
        pred_sql=pred_sql,
        gold_sql=gold_sql,
        access=access,
        execution_comparison=execution_comparison,
    )
    code_prepared["_pred_sql"] = pred_sql

    # 3. RuntimeCaseView
    runtime_case_view = _build_runtime_case_view(
        db_id=db_id,
        case_id=case_id,
        question=question,
        evidence=evidence,
        code_prepared=code_prepared,
    )

    # 4. Wrong Case Auditor
    case_audit = run_wrong_case_auditor(
        question=question,
        evidence=evidence,
        pred_sql=pred_sql,
        gold_sql=gold_sql,
        local_schema_view=runtime_case_view.local_schema_view,
        execution_result=execution_result,
        execution_comparison=execution_comparison,
    )
    # 节点返回的 CaseAudit 里 case_id 是空的，由 caller 补
    case_audit = case_audit.model_copy(
        update={
            "case_id": case_id,
            "db_id": db_id,
            "delta_signature": code_prepared.get("delta_signature"),
        }
    )
    if case_audit.candidate_fix_sql and not case_audit.validated_sql and not skip_auto_execution:
        candidate_cmp = run_execution_comparison(
            db_path=db_path,
            pred_sql=case_audit.candidate_fix_sql,
            gold_sql=gold_sql,
        )
        if bool(getattr(candidate_cmp, "row_sets_equivalent", False)):
            case_audit = case_audit.model_copy(
                update={"validated_sql": case_audit.candidate_fix_sql}
            )

    # 5. ErrorInstance Extractor
    error_instance = run_error_instance_extractor(
        runtime_case_view=runtime_case_view,
        case_audit=case_audit,
        code_prepared=code_prepared,
    )
    error_instance = error_instance.model_copy(
        update={"case_signal_bundle": code_prepared.get("case_signal_bundle")}
    )

    # 6. Action Compiler (optional in Phase 1.3+)
    # Phase 1.4（δ 策略）：Compiler 消费 GroupSummary，不再消费 ErrorInstanceV2。
    # 开发期路径里，把 extractor 产的 ErrorInstance 即时转成 singleton 作为 memory。
    compiler_output: Optional[ActionCompilerOutput] = None
    if run_compiler:
        from .accumulate_v2 import error_instance_to_singleton
        from .runtime_v2 import build_runtime_case_view as build_answer_blind_runtime_case_view

        dev_singleton = error_instance_to_singleton(
            error_instance,
            runtime_usable=True,
            case_audit=case_audit,
            runtime_case_view=runtime_case_view,
        )
        compiler_case_view = build_answer_blind_runtime_case_view(
            db_id=db_id,
            case_id=case_id,
            question=question,
            evidence=evidence,
            pred_top1_sql=pred_sql,
            access=access,
        )
        compiler_output = run_action_compiler(
            runtime_case_view=compiler_case_view,
            memory_objects=[dev_singleton],
        )

    # 7. 组装 summary（仅用于开发期审查，不进 runtime）
    code_prepared_summary = {
        "t_pred": code_prepared["t_pred"],
        "t_q": code_prepared["t_q"],
        "t_mem": code_prepared["t_mem"],
        "legacy_signature": code_prepared["legacy_signature"],
        "legacy_signature_dict": code_prepared["legacy_signature_dict"],
        "structure_flags_true": [
            k for k, v in code_prepared["structure_flags"].items() if v
        ],
        "candidate_question_tags": code_prepared["candidate_question_tags"],
        "candidate_pred_tags": code_prepared["candidate_pred_tags"],
        "local_schema_tables": code_prepared["local_schema_view"].tables,
        "local_schema_diagnostics": code_prepared["local_schema_diagnostics"].model_dump(),
        "case_signal_view": (
            code_prepared["case_signal_view"].model_dump()
            if code_prepared.get("case_signal_view") is not None
            else None
        ),
        "case_signal_bundle": (
            code_prepared["case_signal_bundle"].model_dump()
            if code_prepared.get("case_signal_bundle") is not None
            else None
        ),
        "delta_signature": (
            code_prepared["delta_signature"].model_dump()
            if code_prepared.get("delta_signature") is not None
            else None
        ),
        "execution_comparison": (
            execution_comparison.model_dump() if execution_comparison is not None else None
        ),
    }

    return ErrorInstancePipelineOutput(
        error_instance=error_instance,
        runtime_case_view=runtime_case_view,
        case_audit=case_audit,
        code_prepared_summary=code_prepared_summary,
        compiler_output=compiler_output,
    )


__all__ = ["run_error_instance_pipeline", "ErrorInstancePipelineOutput"]
