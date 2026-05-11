"""Accumulate chain —— ErrorInstance → GroupSummary singleton formation.

按 codex 建议的最小路径：
- 不做 similarity merge / family promotion（留 Phase 1.5）
- 单例直接升 GroupSummary(group_type=singleton, support=1, runtime_usable=True)
- 字段映射来自 codex 分析（确保 runtime Compiler 能直接消费）

Phase 1.4b 的完整 accumulate loop 还会包装成 pipeline 层接口，但 singleton 转换
函数本身在这里独立实现，便于在 case_pipeline 的开发期 ErrorInstance→Compiler 链路
里也复用（把当前 extractor 产物临时转成 singleton 让 Compiler 可用）。
"""

from __future__ import annotations

import os
import hashlib
from datetime import datetime
from typing import List, Optional

from method.EEA.rulebook.common.core.data_structures import (
    CaseAudit,
    CoreInterface,
    ErrorInstanceV2,
    GroupFormationEvidence,
    GroupLifecycle,
    GroupMemberEvidence,
    GroupSummary,
    InstantiationProgram,
    LibraryStateV2,
    ModelProfile,
    PatternRecognitionContract,
    RuntimeCaseView,
    TriggerPolicy,
    TriggerSignature,
)
from method.EEA.rulebook.common.analysis.repair_program_normalizer import attach_canonical_repair_ir
from method.EEA.rulebook.common.analysis.repair_card_normalizer import derive_repair_card
from method.EEA.rulebook.common.analysis.signal_summary import (
    apply_delta_structural_override,
    build_formation_signals,
    build_trigger_contract,
    compact_canonical_repair_ir_for_memory,
    compact_synthesized_program_for_memory,
)
from method.EEA.rulebook.common.learning.shared_program_synthesizer import (
    coverage_for_singleton_program,
    singleton_program_from_ir,
)
from method.EEA.rulebook.common.runtime.trigger_contract import ensure_materialized_trigger_contract
from method.EEA.rulebook.common.core.vocabulary import (
    Confidence,
    EXECUTION_ONLY_PRED_TAGS,
    GrainType,
    GroupStatus,
    GroupType,
    OpType,
)


_ACCUMULATE_FAILURE_STREAK: List[dict[str, str]] = []


def _accumulate_fail_fast_threshold() -> int:
    """连续失败阈值；0 表示禁用 fail-fast。"""
    raw = str(
        os.getenv(
            "EEA_ACCUMULATE_FAIL_FAST_AFTER",
            os.getenv("EEA_V2_ACCUMULATE_FAIL_FAST_AFTER", "3"),
        )
        or ""
    ).strip()
    if not raw:
        return 3
    if raw.lower() in {"0", "off", "false", "disable", "disabled"}:
        return 0
    try:
        value = int(raw)
    except ValueError:
        return 3
    return max(value, 0)


def _reset_accumulate_failure_streak(*, db_id: str) -> None:
    if _ACCUMULATE_FAILURE_STREAK and _ACCUMULATE_FAILURE_STREAK[0]["db_id"] == db_id:
        _ACCUMULATE_FAILURE_STREAK.clear()


def _record_accumulate_failure(*, db_id: str, case_id: str, reason: str) -> Optional[str]:
    """记录连续失败；达到阈值时返回 fail-fast 消息。"""
    threshold = _accumulate_fail_fast_threshold()
    if threshold <= 0:
        return None
    if _ACCUMULATE_FAILURE_STREAK and _ACCUMULATE_FAILURE_STREAK[0]["db_id"] != db_id:
        _ACCUMULATE_FAILURE_STREAK.clear()
    _ACCUMULATE_FAILURE_STREAK.append(
        {
            "db_id": db_id,
            "case_id": str(case_id),
            "reason": reason,
        }
    )
    if len(_ACCUMULATE_FAILURE_STREAK) < threshold:
        return None
    failures = "; ".join(
        f"{item['case_id']} ({item['reason']})" for item in _ACCUMULATE_FAILURE_STREAK
    )
    return (
        "Aborting run after "
        f"{len(_ACCUMULATE_FAILURE_STREAK)} consecutive v2 accumulate failures "
        f"(threshold={threshold}) for db_id={db_id}. "
        f"Failure streak: {failures}. "
        "Set EEA_ACCUMULATE_FAIL_FAST_AFTER=0 to disable this guard."
    )


def _runtime_visible_tags(tags: List[str]) -> List[str]:
    """过滤掉 execution-only 的 pred tags——runtime 看不到这些 tag，
    用它们做 trigger_signature 会让 singleton 永远命中不了。"""
    return [t for t in tags if t not in EXECUTION_ONLY_PRED_TAGS]


_RUNTIME_REQUIRED_QUESTION_TAGS = {
    *(item.value for item in OpType),
    *(item.value for item in GrainType),
}


def _runtime_required_question_tags(tags: List[str]) -> List[str]:
    """Keep only abstract operation/grain question gates.

    Route-cue tags come from lexical hints and are kept as audit evidence, but
    they are not stable enough to be required runtime trigger signals.
    """
    return [tag for tag in tags if tag in _RUNTIME_REQUIRED_QUESTION_TAGS]


def _skeleton_key_from_structural(structural: object) -> str:
    shape = getattr(structural, "output_shape_delta", None)
    shape_payload = shape.model_dump(mode="json") if hasattr(shape, "model_dump") else dict(shape or {})
    shape_key = "shape=none"
    if shape_payload:
        shape_key = "|".join(
            [
                f"shape_op={shape_payload.get('operation')}",
                f"shape_dir={shape_payload.get('arity_direction')}",
                f"source_grain={shape_payload.get('current_grain')}",
                f"target_grain={shape_payload.get('target_grain')}",
            ]
        )
    return "|".join(
        [
            getattr(structural.locus, "value", str(structural.locus)),
            getattr(structural.op_family, "value", str(structural.op_family)),
            getattr(structural.target_family, "value", str(structural.target_family)),
            shape_key,
        ]
    )


def _skeleton_key(error_instance: ErrorInstanceV2) -> str:
    return _skeleton_key_from_structural(error_instance.repair_skeleton.structural)


def _slot_signature(error_instance: ErrorInstanceV2) -> dict[str, object]:
    return {
        "required": sorted(
            f"{slot.kind}:{slot.name}" for slot in error_instance.instantiation_slots if slot.required
        ),
        "optional": sorted(
            f"{slot.kind}:{slot.name}" for slot in error_instance.instantiation_slots if not slot.required
        ),
        "role_families": sorted(
            {
                role
                for slot in error_instance.instantiation_slots
                for role in slot.allowed_role_families
            }
        ),
    }


def _build_member_evidence(
    *,
    error_instance: ErrorInstanceV2,
    runtime_case_view: Optional[RuntimeCaseView],
    formation_signals: Optional[dict[str, object]] = None,
) -> GroupFormationEvidence:
    bundle = None
    if runtime_case_view is not None:
        bundle = runtime_case_view.case_signal_bundle
    if bundle is None:
        bundle = error_instance.case_signal_bundle
    schema_signature = {}
    if bundle is not None:
        schema_signature = dict(bundle.stable_signatures.get("schema_role_signature") or {})
    skeleton_for_key = error_instance.repair_skeleton
    if formation_signals is not None:
        skeleton_for_key = apply_delta_structural_override(
            error_instance.repair_skeleton,
            formation_signals,
        )
    member = GroupMemberEvidence(
        case_id=str(error_instance.case_id),
        question_signals=list(bundle.question_signals) if bundle is not None else [],
        pred_signals=list(bundle.pred_signals) if bundle is not None else [],
        schema_signals=list(bundle.schema_signals) if bundle is not None else [],
        execution_only_signals=list(bundle.execution_only_signals) if bundle is not None else [],
        skeleton_key=_skeleton_key_from_structural(skeleton_for_key.structural),
        slot_signature=_slot_signature(error_instance),
        schema_signature=schema_signature,
        rewrite_hint_proto_hash=hashlib.sha1(
            (error_instance.rewrite_hint_proto or "").encode("utf-8")
        ).hexdigest()[:16],
    )
    return GroupFormationEvidence(
        member_evidence=[member],
        formation_version="singleton-phase-a-audit-v0",
        review_status="unreviewed",
    )


def _trigger_key_status(
    *,
    required_signals: List[str],
    required_pred_tags: List[str],
    filtered_tags: List[str],
) -> str:
    if required_signals:
        return "contract_runtime_visible"
    if required_pred_tags:
        return "legacy_pred_runtime_visible"
    if filtered_tags:
        return "only_offline_tags_filtered"
    return "empty"


def _build_trigger_policy(
    *,
    trigger_contract: object,
    runtime_pred_decisive: List[str],
    filtered_pred_tags: List[str],
) -> TriggerPolicy:
    contract_payload = (
        trigger_contract.model_dump(mode="json")
        if hasattr(trigger_contract, "model_dump")
        else dict(trigger_contract or {})
    )
    sources = {
        signal: "singleton_source_case_runtime_visible_contract"
        for signal in contract_payload.get("required_signals", [])
    }
    notes = (
        "runtime_trigger_key_status="
        f"{_trigger_key_status(required_signals=list(sources), required_pred_tags=runtime_pred_decisive, filtered_tags=filtered_pred_tags)}; "
        f"filtered_pred_tags={filtered_pred_tags}"
    )
    return TriggerPolicy(
        required_signal_sources=sources,
        required_signal_support_ratio={signal: 1.0 for signal in sources},
        negative_signal_sources={
            signal: "source_case_guardrail_or_contract"
            for signal in contract_payload.get("negative_signals", [])
        },
        runtime_visible_only=True,
        threshold_version="singleton-contract-v0",
        notes=notes,
    )


def error_instance_to_singleton(
    error_instance: ErrorInstanceV2,
    *,
    group_id: Optional[str] = None,
    runtime_usable: bool = True,
    case_audit: Optional[CaseAudit] = None,
    runtime_case_view: Optional[RuntimeCaseView] = None,
) -> GroupSummary:
    """把 ErrorInstanceV2 直接升级成 singleton GroupSummary。

    字段映射（codex review 建议）：
    - core_interface.repair_skeleton_prototype = error_instance.repair_skeleton
    - core_interface.repair_goal = error_instance.repair_goal
    - core_interface.question_family_tags = error_instance.question_features.decisive_tags
    - core_interface.pred_family_tags = error_instance.pred_sql_features.decisive_tags
    - instantiation_program.shared = True
    - instantiation_program.slots = error_instance.instantiation_slots
    - instantiation_program.branch_rules = error_instance.branch_rules
    - trigger_signature.required_question_tags = error_instance.question_features.decisive_tags
    - trigger_signature.required_pred_tags = error_instance.pred_sql_features.decisive_tags
    - guardrails = error_instance.guardrails
    - group_type = singleton；support = 1；runtime_usable=True（否则第二 case 不会触发）

    Phase 1.5 后，如果引入相似度合并，合并后的 GroupSummary 由
    ``merge_singletons_to_family`` 产出（未实现）；本函数只管 singleton 形成。
    """
    group_id = group_id or f"grp-sing-{error_instance.db_id}-{error_instance.case_id}"
    formation_signals = build_formation_signals(
        case_signal_view=(
            runtime_case_view.case_signal_view if runtime_case_view is not None else None
        ),
        delta_signature=case_audit.delta_signature if case_audit is not None else None,
        error_instance=error_instance,
    )
    error_instance = attach_canonical_repair_ir(
        error_instance=error_instance,
        case_audit=case_audit,
        runtime_case_view=runtime_case_view,
        formation_signals=formation_signals,
    )
    formation_signals["canonical_repair_ir"] = (
        compact_canonical_repair_ir_for_memory(error_instance.canonical_repair_ir)
        if error_instance.canonical_repair_ir is not None
        else None
    )
    formation_signals["repair_insight_signature"] = (
        error_instance.repair_insight_signature.model_dump(mode="json")
        if error_instance.repair_insight_signature is not None
        else None
    )
    singleton_program = (
        singleton_program_from_ir(error_instance.canonical_repair_ir)
        if error_instance.canonical_repair_ir is not None
        else None
    )
    singleton_coverage = (
        coverage_for_singleton_program(singleton_program)
        if singleton_program is not None
        else None
    )
    if singleton_program is not None:
        formation_signals["synthesized_program"] = compact_synthesized_program_for_memory(
            singleton_program
        )
    if singleton_coverage is not None:
        formation_signals["program_coverage"] = singleton_coverage.model_dump(mode="json")
    formation_signals["repair_card"] = derive_repair_card(
        error_instance,
        runtime_case_view.local_schema_view if runtime_case_view is not None else None,
        case_audit=case_audit,
    )

    core_interface = CoreInterface(
        question_family_tags=list(error_instance.question_features.decisive_tags),
        pred_family_tags=list(error_instance.pred_sql_features.decisive_tags),
        repair_goal=error_instance.repair_goal,
        repair_skeleton_prototype=apply_delta_structural_override(
            error_instance.repair_skeleton,
            formation_signals,
        ),
    )

    instantiation_program = InstantiationProgram(
        shared=bool(singleton_program and singleton_program.ops),
        shared_status=(
            "structural"
            if bool(singleton_program and singleton_program.ops)
            else "none"
        ),
        template=error_instance.rewrite_hint_proto or None,
        slots=list(error_instance.instantiation_slots),
        branch_rules=list(error_instance.branch_rules),
        repair_program=list(error_instance.repair_program),
        synthesized_program=singleton_program,
        program_coverage=singleton_coverage,
        pattern_recognition_contract=PatternRecognitionContract(
            pre_question_signature=error_instance.pre_question_signature_local or "",
            pre_sql_signature=error_instance.pre_sql_signature_local or "",
            observed_failure_summary=error_instance.observed_failure_local or "",
            repair_direction=error_instance.repair_direction_local or "",
        ),
    )

    # trigger_signature 必须只包含 runtime 可重现的 tag，否则 runtime 检索永远不命中。
    # 把 decisive_tags 里 execution-only 的（select_arity_mismatch 等）筛掉；
    # 这些 tag 保留在 core_interface.pred_family_tags / question_family_tags 作为离线
    # 分析用途，runtime trigger 只看过滤后的集合。
    runtime_pred_decisive = _runtime_visible_tags(
        list(error_instance.pred_sql_features.decisive_tags)
    )
    filtered_pred_tags = [
        tag
        for tag in error_instance.pred_sql_features.decisive_tags
        if tag in EXECUTION_ONLY_PRED_TAGS
    ]
    # 若 filter 后空，fallback 用 descriptive_tags 里的 runtime-visible tag，避免 singleton
    # 完全没 trigger key。
    if not runtime_pred_decisive:
        filtered_pred_tags.extend(
            tag
            for tag in error_instance.pred_sql_features.descriptive_tags
            if tag in EXECUTION_ONLY_PRED_TAGS
        )
        runtime_pred_decisive = _runtime_visible_tags(
            list(error_instance.pred_sql_features.descriptive_tags)
        )

    runtime_question_decisive = _runtime_required_question_tags(
        list(error_instance.question_features.decisive_tags)
    )

    trigger_signature = TriggerSignature(
        required_question_tags=runtime_question_decisive,
        required_pred_tags=runtime_pred_decisive,
        decisive_antipatterns=[],
        negative_evidence=[],
    )
    trigger_contract = build_trigger_contract(
        formation_signals=formation_signals,
        error_instance=error_instance,
        decisive_pred_signals=runtime_pred_decisive,
        decisive_question_signals=runtime_question_decisive,
        negative_signals=list(trigger_signature.negative_evidence),
        max_actions=1,
    )
    formation_evidence = _build_member_evidence(
        error_instance=error_instance,
        runtime_case_view=runtime_case_view,
        formation_signals=formation_signals,
    )
    trigger_policy = _build_trigger_policy(
        trigger_contract=trigger_contract,
        runtime_pred_decisive=runtime_pred_decisive,
        filtered_pred_tags=sorted(set(filtered_pred_tags)),
    )

    now = datetime.utcnow().isoformat()
    singleton = GroupSummary(
        group_id=group_id,
        group_type=GroupType.SINGLETON,
        db_id=error_instance.db_id,
        case_ids=[error_instance.case_id],
        support=1,
        confidence=Confidence.LOW,
        version=0,
        runtime_usable=runtime_usable,
        status=GroupStatus.ACTIVE,
        core_interface=core_interface,
        instantiation_program=instantiation_program,
        trigger_signature=trigger_signature,
        trigger_contract=trigger_contract,
        guardrails=list(error_instance.guardrails),
        action_realization_traces=[],
        model_profile=ModelProfile(),
        replay_history=[],
        formation_signals=formation_signals,
        formation_evidence=formation_evidence,
        trigger_policy=trigger_policy,
        lifecycle=GroupLifecycle(
            formation_parent_ids=[str(error_instance.case_id)],
            promotion_state="singleton_seed",
        ),
        created_at=now,
        last_updated_at=now,
    )
    ensure_materialized_trigger_contract(singleton)
    return singleton


def append_singleton_to_library(
    library: LibraryStateV2, singleton: GroupSummary
) -> LibraryStateV2:
    """把 singleton 加入 library.singletons。Phase 1.4b：不做去重/合并。"""
    # 防重复 group_id（Phase 1.4 简单：同 id 不重复加）
    existing_ids = {g.group_id for g in library.singletons}
    if singleton.group_id in existing_ids:
        return library
    library.singletons.append(singleton)
    library.cases_processed += 1
    return library


def accumulate_wrong_case(
    *,
    db_id: str,
    case_id: str,
    question: str,
    evidence: str,
    pred_sql: str,
    gold_sql: str,
    db_path: str,
    library: LibraryStateV2,
    database_dir: Optional[str] = None,
) -> tuple[GroupSummary, "object"]:
    """v2 accumulate 主入口：对一个仍错的 case，产出 singleton 并加入 library。

    这是 **看 gold 的离线 accumulate 阶段**——区别于 runtime（answer-blind）。
    在 DeepEye 的 memory_rewrite 管线里，这个函数应该在 enhanced != gold 之后调用。

    返回 (singleton, case_audit)——singleton 已进 library，caller 可记日志。
    """
    # 延迟 import 避开循环依赖（case_pipeline import accumulate，反之也会）
    from method.EEA.rulebook.common.learning.case_pipeline import run_error_instance_pipeline

    try:
        result = run_error_instance_pipeline(
            db_id=db_id,
            case_id=case_id,
            question=question,
            evidence=evidence,
            pred_sql=pred_sql,
            gold_sql=gold_sql,
            db_path=db_path,
            database_dir=database_dir,
            run_compiler=False,  # accumulate 不需要跑 compiler
        )
    except Exception as exc:
        fail_fast_message = _record_accumulate_failure(
            db_id=db_id,
            case_id=str(case_id),
            reason=f"{type(exc).__name__}: {exc}",
        )
        if fail_fast_message:
            raise SystemExit(fail_fast_message) from exc
        raise
    singleton = error_instance_to_singleton(
        result.error_instance,
        runtime_usable=True,
        case_audit=result.case_audit,
        runtime_case_view=result.runtime_case_view,
    )
    append_singleton_to_library(library, singleton)
    _reset_accumulate_failure_streak(db_id=db_id)
    return singleton, result.case_audit


__all__ = [
    "error_instance_to_singleton",
    "append_singleton_to_library",
    "accumulate_wrong_case",
]
