from __future__ import annotations

from types import SimpleNamespace

import pytest

from method.EEA.rulebook.common.runtime.action_compiler import (
    _current_output_shape_for_compiler,
    _variant_requires_relation_reroute,
    enumerate_candidates,
)
from method.EEA.rulebook.common.learning.accumulate import error_instance_to_singleton
from method.EEA.rulebook.common.learning.evolution import (
    compact_evolution_report,
    evolve_library_with_replay,
)
from method.EEA.rulebook.common.io.execution_compare import _normalize_row_multiset
from method.EEA.rulebook.common.llm.nodes import (
    _action_compiler_prompt_payloads,
    _compact_case_audit_for_extractor,
    _deterministic_canonical_fallback_actions,
    _enforce_rewrite_contract_absence_checks,
    _enforce_rewrite_scope,
    _runtime_actions_prompt_payload,
    _rewrite_contract_prompt_payload,
    _reroute_candidate_covers_projection_actions,
    _reroute_candidate_has_missing_relation_for_projection,
    run_action_compiler,
    run_memory_rewrite,
    run_wrong_case_auditor,
)
from method.EEA.rulebook.common.core.data_structures import (
    ActionCandidate,
    ActionCandidateSet,
    Action,
    CandidateSetSummary,
    CaseAudit,
    CaseSignalView,
    CanonicalRepairIR,
    CanonicalRepairOp,
    CoreInterface,
    EditScope,
    ErrorInstanceV2,
    GroupSummary,
    InstantiationSlot,
    InstantiationProgram,
    LibraryStateV2,
    LocalSchemaViewDiagnostics,
    LocalSchemaView,
    OutputShapeDelta,
    PkFkEdge,
    PredManifestation,
    PredSqlSignalView,
    ProgramCoverage,
    QuestionContract,
    QuestionFeatures,
    RepairEffectSignature,
    RepairInsightSignature,
    RepairProgramStep,
    RepairSkeleton,
    RepairSkeletonSemantic,
    RepairSkeletonStructural,
    RuntimeCaseView,
    PredSqlFeatures,
    PromotionTestResult,
    ReplayMetrics,
    TriggerCandidateAudit,
    TriggerSignature,
)
from method.EEA.rulebook.common.learning.pattern_formation import (
    _program_structural_skeleton,
    build_family_from_groups,
)
from method.EEA.rulebook.common.learning.case_pipeline import _build_runtime_case_view
from method.EEA.rulebook.common.learning.program_coverage import CompilerCoverageValidator
from method.EEA.rulebook.common.learning.promotion import (
    _contract_program_issues,
    _formal_replay_row_passed,
    _metrics_from_rows,
    _not_actionable_reason,
    _replay_one_holdout,
    apply_promotion_decision,
    integrate_promoted_groups,
)
from method.EEA.rulebook.common.runtime.runtime import (
    _compiler_dry_run_gate,
    _select_groups_with_shared_current_transform,
    build_runtime_case_view,
    build_current_case_signals,
    build_runtime_rewrite_guard,
    _memory_schema_tables_from_value,
    contract_steps_changed_sql,
    rewrite_realization_origin_from_result,
    trigger_memory_objects,
)
from method.EEA.rulebook.common.analysis.repair_program_normalizer import RepairProgramNormalizer
from method.EEA.rulebook.common.analysis.role_graph_normalizer import RoleGraphNormalizer
from method.EEA.rulebook.common.analysis.signal_summary import (
    _compact_retrieval_evidence,
    build_formation_signals,
    build_trigger_contract,
)
from method.EEA.rulebook.common.learning.shared_program_synthesizer import (
    coverage_for_singleton_program,
    singleton_program_from_ir,
    synthesize_shared_program,
)
from method.EEA.rulebook.common.runtime.trigger_contract import (
    ensure_materialized_trigger_contract,
    is_contract_runtime_executable,
    materialize_library_runtime_contracts,
)
from method.EEA.rulebook.common.analysis.structure_family import cached_ast_signature
from method.EEA.rulebook.common.core.vocabulary import (
    ActionPrimitive,
    Confidence,
    GroupStatus,
    GroupType,
    Locus,
    OpFamily,
    OutputContract,
    RiskLevel,
    TargetFamily,
)


def _apply_rewrite_contract_dependencies(*_args, **_kwargs):
    pytest.skip(
        "Post-LLM deterministic dependency rewriting was removed; rewrite "
        "tests should assert contract payloads or fail-closed validation."
    )


@pytest.fixture(autouse=True)
def _default_shared_insight_judge(monkeypatch):
    """Keep unit tests offline after shared-insight compatibility became explicit."""
    from method.EEA.rulebook.common.learning import pattern_formation as pattern_formation_module
    from method.EEA.rulebook.common.learning import shared_program_synthesizer as shared_program_synthesizer_module

    monkeypatch.setattr(
        shared_program_synthesizer_module,
        "_call_shared_insight_judge",
        lambda **_: {
            "compatibility": "compatible",
            "shared_interface_key": "shared test repair interface",
            "shared_insight": {
                "source_misread": "test fixtures share a repair symptom",
                "target_preference": "test fixtures share a target preference",
                "repair_interface": "shared test repair interface",
                "binding_slots": [],
                "preserve_invariants": ["preserve current scope"],
                "negative_guards": [],
                "axis_links": [
                    {
                        "axis": "output_shape_delta",
                        "role": "primary",
                        "evidence": "test fixture default judge",
                    }
                ],
            },
            "conflict_reasons": [],
            "lost_constraints": [],
            "unresolved_axes": [],
            "required_code_checks": ["compiler coverage"],
        },
    )
    monkeypatch.setattr(
        pattern_formation_module,
        "_call_insight_pattern_slicer",
        lambda groups, **_: {
            "candidate_groups": [
                {
                    "candidate_id": "unit_test_all_cases",
                    "case_ids": [
                        str(case_id)
                        for group in groups
                        for case_id in group.case_ids
                    ],
                    "stable_bias_hypothesis": "unit test all-case candidate",
                    "branch_hypothesis": "unit test branch hypothesis",
                    "why_grouped": "unit tests keep slicer deterministic",
                }
            ],
            "rejected_case_ids": [],
            "rationale": "unit test deterministic slicer",
        },
    )
    monkeypatch.setattr(
        pattern_formation_module,
        "_call_pattern_admission_judge",
        lambda **_: {
            "admit_pattern": False,
            "accepted_case_ids": [],
            "excluded_case_ids": [],
            "stable_bias_key": "",
            "primary_repair_interface": "",
            "branch_axes": [],
            "branch_specs": [],
            "negative_guards": [],
            "required_code_checks": [],
            "reject_reason": "unit_test_default_no_pattern_admission",
            "rationale": "unit tests opt out of LLM pattern admission by default",
        },
    )


def _skeleton() -> RepairSkeleton:
    return RepairSkeleton(
        structural=RepairSkeletonStructural(
            locus=Locus.SELECT,
            op_family=OpFamily.DROP,
            target_family=TargetFamily.SHAPE,
            output_contract=OutputContract.UNCHANGED,
        ),
        semantic=RepairSkeletonSemantic(intent="retain the structurally correct side"),
    )


def _operation_signature(
    *,
    op_type: str,
    locus: str,
    direction: str = "decrease",
) -> dict:
    return {
        "step_op": op_type,
        "locus": locus,
        "is_dependency": False,
        "required": True,
        "slot_signature": [],
        "role_delta": {
            "arity_direction": direction,
            "source_output_roles": ["identifier", "identifier"],
            "target_output_roles": ["identifier"],
            "target_output_subset_of_source": True,
        },
    }


def _repair_insight(interface_key: str) -> RepairInsightSignature:
    return RepairInsightSignature(
        interface_key=interface_key,
        source_misread=f"source follows {interface_key} pre-repair symptom",
        target_preference=f"target requires {interface_key} repair result",
        repair_interface=interface_key,
        binding_slots=[
            {
                "name": "source_answer_slot",
                "kind": "column",
                "source_or_target": "source",
                "required": True,
                "allowed_role_families": ["identifier", "other"],
            }
        ],
        preserve_invariants=["preserve current scope"],
        negative_guards=["do not apply if the current SQL already satisfies the target answer unit"],
        axis_links=[
            {
                "axis": "output_shape_delta",
                "role": "primary",
                "evidence": "synthetic canonical-program test fixture",
            }
        ],
        evidence={"source": "test_fixture"},
        confidence="high",
    )


def _repair_effect_signature(
    *,
    case_id: str,
    axis: str = "output_shape_delta",
    kind: str = "output_subset",
    primitive: str = "DROP_SIDE",
) -> RepairEffectSignature:
    return RepairEffectSignature.model_validate(
        {
            "effect_candidates": [
                {
                    "effect_id": f"effect:{case_id}:{axis}",
                    "axis": axis,
                    "source_state": {"output": {"arity": 2}},
                    "target_state": {"output": {"arity": 1}},
                    "delta": {
                        "kind": kind,
                        "arity_direction": "decrease",
                        "target_is_subset_of_source": kind == "output_subset",
                    },
                    "role": "primary",
                    "triggerability": {
                        "source_visible_in_runtime": True,
                        "target_bindable_from_schema_or_memory": True,
                    },
                    "actionability": {"primitive": primitive},
                    "evidence": {"source": "test_fixture"},
                    "confidence": 0.95,
                }
            ]
        }
    )


def _insight_key_for_fixture(op_type: str, locus: str) -> str:
    if str(locus).upper() in {"JOIN", "BRIDGE"}:
        return "add bridge path preserving output contract"
    if "WHERE" in str(op_type).upper() or str(locus).upper() in {"WHERE", "PREDICATE"}:
        return "replace predicate scope preserving output contract"
    return "drop extra output side preserving scope"


def test_family_program_skeleton_keeps_generic_output_shape_delta() -> None:
    shape = {
        "current_arity": 3,
        "target_arity": 2,
        "arity_delta": -1,
        "arity_direction": "decrease",
        "current_grain": "row_result",
        "target_grain": "row_result",
    }
    program = SimpleNamespace(
        program_type="select_drop",
        ops=[
            SimpleNamespace(
                op_type="SELECT_DROP_SLOT",
                locus="SELECT",
                arguments={"shared_arguments": {"output_shape_delta": shape}},
            )
        ],
    )

    skeleton = _program_structural_skeleton(program, _skeleton())

    payload = skeleton.structural.output_shape_delta.model_dump(mode="json")
    for key, value in shape.items():
        assert payload[key] == value
    assert skeleton.structural.output_contract == OutputContract.UNCHANGED


def test_execution_row_equivalence_preserves_column_order_and_duplicates() -> None:
    assert _normalize_row_multiset([("TR001_2", "TR001_6")]) != _normalize_row_multiset(
        [("TR001_6", "TR001_2")]
    )
    assert _normalize_row_multiset([(1,)]) != _normalize_row_multiset([("1",)])
    assert _normalize_row_multiset([("A", "B"), ("A", "B")]) != _normalize_row_multiset(
        [("A", "B")]
    )
    assert _normalize_row_multiset([("B",), ("A",)]) == _normalize_row_multiset(
        [("A",), ("B",)]
    )


def test_unknown_replay_rows_are_reported_and_block_formal_support() -> None:
    row = {
        "eligible_for_formal_promotion": True,
        "holdout_in_training": False,
        "compile_pass": True,
        "action_count": 1,
        "selection_origins": ["llm_selected"],
        "improved": True,
        "regressed": False,
        "comparison_unknown": True,
        "comparison_unknown_reasons": ["truncated"],
    }

    assert not _formal_replay_row_passed(row)
    metrics = _metrics_from_rows([row], version=0)
    assert metrics.replay_improvement == 0.0
    assert metrics.replay_improvement_llm_selected == 0.0
    assert metrics.comparison_unknown_count == 1
    assert metrics.comparison_unknown_rate == 1.0
    assert metrics.comparison_unknown_reasons == {"truncated": 1}


def _ir(
    case_id: str,
    op_type: str = "SELECT_DROP_SLOT",
    *,
    locus: str = "SELECT",
    invariants: list[str] | None = None,
    include_output_refs: bool = True,
) -> CanonicalRepairIR:
    invariants = invariants or [
        "output_arity_direction=decrease",
        "target_output_subset_of_source_outputs",
        "source_output_roles=identifier,identifier",
        "target_output_roles=identifier",
    ]
    source_output_refs = [
        {
            "ref_id": "pred_sql:output:0",
            "source": "pred_sql",
            "table": "a",
            "column": "id",
            "expression": "a.id",
            "slot_index": 0,
            "sql_role": "output_slot",
            "path_role": "output_slot_0",
        },
        {
            "ref_id": "pred_sql:output:1",
            "source": "pred_sql",
            "table": "b",
            "column": "id",
            "expression": "b.id",
            "slot_index": 1,
            "sql_role": "output_slot",
            "path_role": "output_slot_1",
        },
    ] if include_output_refs else []
    target_output_refs = [
        {
            "ref_id": "target_sql:output:0",
            "source": "target_sql",
            "table": "a",
            "column": "id",
            "expression": "a.id",
            "slot_index": 0,
            "sql_role": "output_slot",
            "path_role": "output_slot_0",
        }
    ] if include_output_refs else []
    insight = _repair_insight(_insight_key_for_fixture(op_type, locus))
    effect_signature = _repair_effect_signature(
        case_id=case_id,
        axis="source_route_delta" if str(locus).upper() in {"JOIN", "BRIDGE"} else "output_shape_delta",
        kind="add_relation" if str(locus).upper() in {"JOIN", "BRIDGE"} else "output_subset",
        primitive="INSERT_BRIDGE" if str(locus).upper() in {"JOIN", "BRIDGE"} else "DROP_SIDE",
    )
    return CanonicalRepairIR(
        db_id="toy",
        case_id=case_id,
        program_ops=[
            CanonicalRepairOp(
                op_id=f"{case_id}:op",
                op_type=op_type,
                locus=locus,
                role_refs=source_output_refs + target_output_refs,
                arguments={
                    "operation_signature": _operation_signature(
                        op_type=op_type,
                        locus=locus,
                    ),
                    "output_shape_delta": {
                        "current_arity": 2,
                        "target_arity": 1,
                        "arity_direction": "decrease",
                    },
                    "source_output_roles": ["identifier", "identifier"],
                    "target_output_roles": ["identifier"],
                    "source_output_refs": source_output_refs,
                    "target_output_refs": target_output_refs,
                    "repair_effect_signature": effect_signature.model_dump(mode="json"),
                    "repair_insight_signature": insight.model_dump(mode="json"),
                },
                invariants=invariants,
                supporting_case_ids=[case_id],
            )
        ],
        core_ops=[
            {
                "op_id": f"{case_id}:op",
                "op_type": op_type,
                "locus": locus,
                "required": True,
                "is_dependency": False,
                "origin": "case_extracted",
                "extraction_source": "llm_explicit",
                "supporting_case_ids": [case_id],
            }
        ],
        target_invariants=[
            item for item in invariants if item.startswith("target_")
        ],
        invariants=invariants,
        repair_effect_signature=effect_signature,
        repair_insight_signature=insight,
    )


def _singleton(
    case_id: str,
    op_type: str = "SELECT_DROP_SLOT",
    *,
    locus: str = "SELECT",
    invariants: list[str] | None = None,
    include_output_refs: bool = True,
) -> GroupSummary:
    ir = _ir(
        case_id,
        op_type=op_type,
        locus=locus,
        invariants=invariants,
        include_output_refs=include_output_refs,
    )
    program = singleton_program_from_ir(ir)
    return GroupSummary(
        group_id=f"grp-sing-toy-{case_id}",
        group_type=GroupType.SINGLETON,
        db_id="toy",
        case_ids=[case_id],
        support=1,
        confidence=Confidence.LOW,
        runtime_usable=True,
        status=GroupStatus.ACTIVE,
        core_interface=CoreInterface(
            question_family_tags=[],
            pred_family_tags=[],
            repair_goal="drop the extra structural side",
            repair_skeleton_prototype=_skeleton(),
        ),
        instantiation_program=InstantiationProgram(
            shared=True,
            repair_program=[],
            synthesized_program=program,
            program_coverage=coverage_for_singleton_program(program),
        ),
        trigger_signature=TriggerSignature(),
        formation_signals={"canonical_repair_ir": ir.model_dump(mode="json")},
    )


def _output_patch_singleton(
    case_id: str,
    *,
    op_type: str,
    current_arity: int,
    target_arity: int,
    current_grain: str = "row_result",
    target_grain: str = "row_result",
) -> GroupSummary:
    source_refs = [
        {
            "ref_id": f"pred_sql:output:{idx}",
            "source": "pred_sql",
            "table": "source_table",
            "column": f"source_col_{idx}",
            "expression": f"s.source_col_{idx}",
            "slot_index": idx,
            "sql_role": "output_slot",
            "column_role": "other",
            "path_role": f"output_slot_{idx}",
        }
        for idx in range(current_arity)
    ]
    target_refs = [
        {
            "ref_id": f"target_sql:output:{idx}",
            "source": "target_sql",
            "table": "target_table",
            "column": f"target_col_{idx}",
            "expression": f"t.target_col_{idx}",
            "slot_index": idx,
            "sql_role": "output_slot",
            "column_role": "other",
            "path_role": f"output_slot_{idx}",
        }
        for idx in range(target_arity)
    ]
    shape = {
        "current_arity": current_arity,
        "target_arity": target_arity,
        "arity_delta": target_arity - current_arity,
        "arity_direction": "increase"
        if target_arity > current_arity
        else "decrease"
        if target_arity < current_arity
        else "same",
        "current_roles": ["column_expr"] * current_arity,
        "target_roles": ["column_expr"] * target_arity,
        "current_grain": current_grain,
        "target_grain": target_grain,
        "operation": "add_output_slot" if target_arity > current_arity else "no_output_shape_change",
    }
    invariants = [
        f"source_output_arity={current_arity}",
        f"target_output_arity={target_arity}",
        "target_output_roles=" + ",".join(["other"] * target_arity),
    ]
    insight = _repair_insight(
        "patch aggregate output slot preserving aggregate grain"
        if any("aggregate" in grain for grain in (current_grain, target_grain))
        else "patch output slots preserving scope"
    )
    effect_signature = _repair_effect_signature(
        case_id=case_id,
        kind="output_patch",
        primitive="REPLACE_SELECT_SLOT",
    )
    ir = CanonicalRepairIR(
        db_id="toy",
        case_id=case_id,
        program_ops=[
            CanonicalRepairOp(
                op_id=f"{case_id}:output_patch",
                op_type=op_type,
                locus="SELECT",
                role_refs=source_refs + target_refs,
                arguments={
                    "operation_signature": {
                        "step_op": op_type,
                        "locus": "SELECT",
                        "is_dependency": False,
                        "required": True,
                        "slot_signature": [],
                        "role_delta": {
                            "arity_direction": shape["arity_direction"],
                            "source_output_roles": ["other"] * current_arity,
                            "target_output_roles": ["other"] * target_arity,
                            "target_output_subset_of_source": False,
                        },
                    },
                    "output_shape_delta": shape,
                    "source_output_shape": {
                        "arity": current_arity,
                        "roles": ["other"] * current_arity,
                        "grain": current_grain,
                        "has_aggregate": False,
                        "has_distinct": False,
                    },
                    "target_output_shape": {
                        "arity": target_arity,
                        "roles": ["other"] * target_arity,
                        "grain": target_grain,
                        "has_aggregate": False,
                        "has_distinct": False,
                    },
                    "source_output_roles": ["other"] * current_arity,
                    "target_output_roles": ["other"] * target_arity,
                    "source_output_refs": source_refs,
                    "target_output_refs": target_refs,
                    "target_invariants": [
                        f"target_output_arity={target_arity}",
                        "target_output_roles=" + ",".join(["other"] * target_arity),
                    ],
                    "repair_effect_signature": effect_signature.model_dump(mode="json"),
                    "repair_insight_signature": insight.model_dump(mode="json"),
                },
                invariants=invariants,
                supporting_case_ids=[case_id],
            )
        ],
        core_ops=[
            {
                "op_id": f"{case_id}:output_patch",
                "op_type": op_type,
                "locus": "SELECT",
                "required": True,
                "is_dependency": False,
                "origin": "case_extracted",
                "extraction_source": "llm_explicit",
                "supporting_case_ids": [case_id],
            }
        ],
        target_invariants=[
            f"target_output_arity={target_arity}",
            "target_output_roles=" + ",".join(["other"] * target_arity),
        ],
        invariants=invariants,
        repair_effect_signature=effect_signature,
        repair_insight_signature=insight,
    )
    program = singleton_program_from_ir(ir)
    group = _singleton(case_id, op_type=op_type)
    group.formation_signals["canonical_repair_ir"] = ir.model_dump(mode="json")
    group.instantiation_program.synthesized_program = program
    group.instantiation_program.program_coverage = coverage_for_singleton_program(program)
    group.core_interface.repair_skeleton_prototype.structural.output_shape_delta = (
        OutputShapeDelta.model_validate(shape)
    )
    return group


def test_synthesizer_promotes_shared_case_extracted_program() -> None:
    result = synthesize_shared_program([_singleton("206"), _singleton("249")])

    assert result.program is not None
    assert result.coverage.compile_coverage == 1.0
    assert result.program.ops[0].op_type == "SELECT_DROP_SLOT"
    assert result.program.program_type == "select_drop"
    assert result.program.core_ops
    assert result.program.repair_insight_signature is not None


def test_synthesizer_blocks_same_interface_with_conflicting_insight_target(monkeypatch) -> None:
    from method.EEA.rulebook.common.learning import shared_program_synthesizer as shared_program_synthesizer_module

    left = _singleton("206")
    right = _singleton("249")
    ir = CanonicalRepairIR.model_validate(right.formation_signals["canonical_repair_ir"])
    insight = _repair_insight("drop extra output side preserving scope")
    insight.target_preference = "target requires keeping the second output side"
    ir.repair_insight_signature = insight
    for op in ir.program_ops:
        op.arguments["repair_insight_signature"] = insight.model_dump(mode="json")
    right.formation_signals["canonical_repair_ir"] = ir.model_dump(mode="json")
    right.formation_signals["repair_insight_signature"] = insight.model_dump(mode="json")
    right.instantiation_program.synthesized_program = singleton_program_from_ir(ir)
    right.instantiation_program.program_coverage = coverage_for_singleton_program(
        right.instantiation_program.synthesized_program
    )
    monkeypatch.setattr(
        shared_program_synthesizer_module,
        "_call_shared_insight_judge",
        lambda **_: {
            "compatibility": "conflict",
            "conflict_reasons": ["target contract is opposite"],
            "lost_constraints": ["would drop a side that this member requires keeping"],
            "shared_insight": {},
        },
    )

    result = synthesize_shared_program([left, right], require_effect_program=True)

    assert result.program is None
    assert any("insight_judge_conflict" in item for item in result.coverage.blockers)


def test_synthesizer_accepts_semantically_compatible_insight_wording(monkeypatch) -> None:
    from method.EEA.rulebook.common.learning import shared_program_synthesizer as shared_program_synthesizer_module

    left = _singleton("206")
    right = _singleton("249")
    ir = CanonicalRepairIR.model_validate(right.formation_signals["canonical_repair_ir"])
    insight = _repair_insight("replace pair output with single answer unit")
    insight.target_preference = "target keeps one bound answer-side output and preserves filters"
    ir.repair_insight_signature = insight
    for op in ir.program_ops:
        op.arguments["repair_insight_signature"] = insight.model_dump(mode="json")
    right.formation_signals["canonical_repair_ir"] = ir.model_dump(mode="json")
    right.formation_signals["repair_insight_signature"] = insight.model_dump(mode="json")
    right.instantiation_program.synthesized_program = singleton_program_from_ir(ir)
    right.instantiation_program.program_coverage = coverage_for_singleton_program(
        right.instantiation_program.synthesized_program
    )
    monkeypatch.setattr(
        shared_program_synthesizer_module,
        "_call_shared_insight_judge",
        lambda **_: {
            "compatibility": "compatible",
            "shared_interface_key": "drop extra paired output side preserving scope",
            "shared_insight": {
                "source_misread": "prediction treats paired output sides as answer units",
                "target_preference": "target uses one bound answer-side output",
                "repair_interface": "drop the extra paired output side while preserving scope",
                "binding_slots": [],
                "preserve_invariants": ["preserve current scope"],
                "negative_guards": [],
                "axis_links": [
                    {
                        "axis": "output_shape_delta",
                        "role": "primary",
                        "evidence": "same output-side contract",
                    }
                ],
            },
            "conflict_reasons": [],
            "lost_constraints": [],
            "required_code_checks": ["compiler coverage"],
        },
    )

    result = synthesize_shared_program([left, right], require_effect_program=True)

    assert result.program is not None
    assert result.coverage.compile_coverage == 1.0
    assert result.program.repair_insight_signature is not None
    assert result.program.repair_insight_signature.interface_key == (
        "drop extra paired output side preserving scope"
    )


def test_synthesizer_blocks_partial_insight_as_non_mergeable(monkeypatch) -> None:
    from method.EEA.rulebook.common.learning import shared_program_synthesizer as shared_program_synthesizer_module

    left = _singleton("206")
    right = _singleton("249")
    ir = CanonicalRepairIR.model_validate(right.formation_signals["canonical_repair_ir"])
    insight = _repair_insight("replace pair output with single answer unit")
    insight.target_preference = "target may keep either answer side depending on binding"
    ir.repair_insight_signature = insight
    for op in ir.program_ops:
        op.arguments["repair_insight_signature"] = insight.model_dump(mode="json")
    right.formation_signals["canonical_repair_ir"] = ir.model_dump(mode="json")
    right.formation_signals["repair_insight_signature"] = insight.model_dump(mode="json")
    right.instantiation_program.synthesized_program = singleton_program_from_ir(ir)
    right.instantiation_program.program_coverage = coverage_for_singleton_program(
        right.instantiation_program.synthesized_program
    )
    monkeypatch.setattr(
        shared_program_synthesizer_module,
        "_call_shared_insight_judge",
        lambda **_: {
            "compatibility": "partial",
            "shared_interface_key": "",
            "shared_insight": {},
            "conflict_reasons": [],
            "lost_constraints": [],
            "unresolved_axes": ["which answer side is canonical remains unresolved"],
        },
    )

    result = synthesize_shared_program([left, right], require_effect_program=True)

    assert result.program is None
    assert any("insight_judge_partial" in item for item in result.coverage.blockers)


def test_synthesizer_generalizes_select_add_and_replace_as_output_patch() -> None:
    result = synthesize_shared_program(
        [
            _output_patch_singleton(
                "23",
                op_type="SELECT_REPLACE_SLOT",
                current_arity=2,
                target_arity=2,
            ),
            _output_patch_singleton(
                "51",
                op_type="SELECT_REPLACE_SLOT",
                current_arity=2,
                target_arity=2,
            ),
            _output_patch_singleton(
                "87",
                op_type="SELECT_ADD_SLOT",
                current_arity=1,
                target_arity=2,
            ),
        ]
    )

    assert result.program is not None
    assert result.coverage.compile_coverage == 1.0
    assert result.program.ops[0].op_type == "SELECT_OUTPUT_PATCH"
    assert result.program.program_type == "select_output_patch"


def test_synthesizer_prefers_effect_bucket_before_legacy_op_buckets(monkeypatch) -> None:
    from method.EEA.rulebook.common.learning import shared_program_synthesizer as shared_program_synthesizer_module

    left = _output_patch_singleton(
        "left",
        op_type="SELECT_REPLACE_SLOT",
        current_arity=2,
        target_arity=2,
    )
    right = _output_patch_singleton(
        "right",
        op_type="SELECT_REPLACE_SLOT",
        current_arity=2,
        target_arity=2,
    )
    ir = CanonicalRepairIR.model_validate(right.formation_signals["canonical_repair_ir"])
    ir.program_ops[0].op_type = "SELECT_OUTPUT_PATCH"
    right.formation_signals["canonical_repair_ir"] = ir.model_dump(mode="json")
    right.instantiation_program.synthesized_program = singleton_program_from_ir(ir)
    right.instantiation_program.program_coverage = coverage_for_singleton_program(
        right.instantiation_program.synthesized_program
    )

    monkeypatch.setattr(shared_program_synthesizer_module, "_ops_by_bucket", lambda _ir: {})
    monkeypatch.setattr(
        shared_program_synthesizer_module, "_ops_by_generalized_bucket", lambda _ir: {}
    )

    result = synthesize_shared_program([left, right])

    assert result.program is not None
    assert result.coverage.compile_coverage == 1.0
    assert result.program.repair_effect_signature is not None
    assert result.program.program_envelope is not None
    assert result.program.program_envelope.target_effects


def test_family_formation_uses_program_compatibility_without_overmerging_grain() -> None:
    row_replace = _output_patch_singleton(
        "1",
        op_type="SELECT_REPLACE_SLOT",
        current_arity=2,
        target_arity=2,
        current_grain="row_result",
        target_grain="row_result",
    )
    row_replace_same_core = _output_patch_singleton(
        "2",
        op_type="SELECT_REPLACE_SLOT",
        current_arity=2,
        target_arity=2,
        current_grain="row_result",
        target_grain="row_result",
    )
    row_add = _output_patch_singleton(
        "3",
        op_type="SELECT_ADD_SLOT",
        current_arity=1,
        target_arity=2,
        current_grain="row_result",
        target_grain="row_result",
    )
    aggregate_replace = _output_patch_singleton(
        "4",
        op_type="SELECT_REPLACE_SLOT",
        current_arity=1,
        target_arity=1,
        current_grain="scalar_aggregate",
        target_grain="scalar_aggregate",
    )
    library = LibraryStateV2(
        db_id="toy",
        singletons=[row_replace, row_replace_same_core, row_add, aggregate_replace],
    )

    formed, _report = build_family_from_groups([row_replace, row_replace_same_core]), None
    output_library, _formation_report = build_family_from_groups([row_replace, row_replace_same_core]), None
    family_library, _ = __import__(
        "method.EEA.rulebook.common.learning.pattern_formation",
        fromlist=["form_offline_families"],
    ).form_offline_families(library)

    assert formed.instantiation_program.synthesized_program is not None
    assert output_library.instantiation_program.synthesized_program is not None
    assert [family.case_ids for family in family_library.experience_families] == [["1", "2"]]
    assert [singleton.case_ids for singleton in family_library.singletons] == [["3"], ["4"]]


def test_pipeline_runtime_view_keeps_pred_only_source_signals() -> None:
    pred_sql = (
        "SELECT a1.element, a2.element "
        "FROM connected c "
        "JOIN atom a1 ON c.atom_id = a1.atom_id "
        "JOIN atom a2 ON c.atom_id2 = a2.atom_id "
        "WHERE c.bond_id = 'TR004_8_9'"
    )
    case_view = _build_runtime_case_view(
        db_id="toy",
        case_id="206",
        question="What elements are in the TR004_8_9 bond atoms?",
        evidence="TR004_8_9 bond atoms refers to bond_id = 'TR004_8_9';",
        code_prepared={
            "_pred_sql": pred_sql,
            "pred_ast": cached_ast_signature(pred_sql) or {},
            "candidate_question_tags": [],
            "candidate_pred_tags": ["select_arity_mismatch"],
            "local_schema_view": LocalSchemaView(
                db_id="toy",
                tables=["connected", "atom"],
                columns_by_table={
                    "connected": ["bond_id", "atom_id", "atom_id2"],
                    "atom": ["atom_id", "element"],
                },
            ),
            "local_schema_diagnostics": LocalSchemaViewDiagnostics(),
        },
    )

    signals = build_formation_signals(case_signal_view=case_view.case_signal_view)
    pred_current = signals["pred_current"]

    assert case_view.case_signal_view is not None
    assert case_view.case_signal_bundle is not None
    assert pred_current["select_arity"] == 2
    assert pred_current["output_shape_current"]["arity"] == 2
    assert pred_current["table_count_bucket"] != "0"
    assert pred_current["predicate_count_bucket"] == "1"


def test_retrieval_evidence_projects_full_role_graph_fields() -> None:
    error = SimpleNamespace(
        canonical_repair_ir={
            "source_role_graph": {
                "alias_path_roles": {
                    "p": ["join_path:posts.owneruserid=users.id"],
                },
                "table_relation_roles": {"posts": "fact", "users": "entity"},
                "output_refs": [{"column_role": "resource url"}],
                "predicate_refs": [{"column_role": "primary name"}],
            },
            "target_role_graph": {
                "alias_path_roles": {
                    "ph": ["join_path:posthistory.postid=posts.id"],
                },
                "table_relation_roles": {
                    "posts": "fact",
                    "postHistory": "activity",
                    "users": "entity",
                },
                "output_refs": [{"column_role": "owner user"}],
                "predicate_refs": [{"column_role": "activity type"}],
                "equality_relations": [
                    {"canonical_key": "postHistory.PostId=posts.Id"},
                ],
            },
            "target_invariants": [
                "target_added_relation_equality=postHistory.UserId=users.Id",
            ],
        },
        repair_skeleton=SimpleNamespace(
            structural=SimpleNamespace(locus=SimpleNamespace(value="JOIN"))
        ),
    )

    evidence = _compact_retrieval_evidence(error)

    assert evidence["schema_version"] == "retrieval-evidence-v0"
    assert evidence["gold_join_edges"] == ["posthistory.postid=posts.id"]
    assert evidence["pred_join_edges"] == ["posts.owneruserid=users.id"]
    assert evidence["gold_only_tables"] == ["posthistory"]
    assert evidence["pred_only_tables"] == []
    assert evidence["target_output_role"] == "owner user"
    assert evidence["source_output_role"] == "resource url"
    assert evidence["target_relation_equalities"] == [
        "posthistory.postid=posts.id",
        "posthistory.userid=users.id",
    ]
    assert evidence["predicate_column_roles"] == ["activity type", "primary name"]
    assert evidence["primary_repair_locus"] == "join"


def test_evolution_helper_preserves_singletons_for_offline_families_without_replay() -> None:
    singleton_a = _pair_output_singleton("206")
    singleton_b = _pair_output_singleton("249")
    ensure_materialized_trigger_contract(singleton_a)
    ensure_materialized_trigger_contract(singleton_b)
    library = LibraryStateV2(
        db_id="toy",
        singletons=[singleton_a, singleton_b],
        cases_processed=2,
    )

    evolved, report = evolve_library_with_replay(
        library=library,
        event_kind="final_evolve_and_freeze",
        case_loader=None,
        db_path=None,
        promotion_min_support=2,
    )

    assert report["promotion_skipped_reason"] == "missing_case_loader_or_db_path"
    assert report["candidate_family_count"] >= 1
    assert evolved.experience_families
    assert evolved.experience_families[0].runtime_usable is False
    assert [group.status for group in evolved.singletons] == [
        GroupStatus.ACTIVE,
        GroupStatus.ACTIVE,
    ]
    assert [group.runtime_usable for group in evolved.singletons] == [True, True]


def test_role_graph_detects_count_distinct_output_shape() -> None:
    graph = RoleGraphNormalizer().normalize_sql(
        sql="SELECT COUNT(DISTINCT t.account_id) FROM trans t",
        source="pred_sql",
    )

    assert graph["output_shape"]["has_aggregate"] is True
    assert graph["output_shape"]["has_distinct"] is True


def test_role_graph_binds_projection_to_join_path_for_same_table_aliases() -> None:
    pred_graph = RoleGraphNormalizer().normalize_sql(
        sql=(
            "SELECT a1.element, a2.element "
            "FROM connected c "
            "JOIN atom a1 ON c.atom_id = a1.atom_id "
            "JOIN atom a2 ON c.atom_id2 = a2.atom_id"
        ),
        source="pred_sql",
    )
    target_graph = RoleGraphNormalizer().normalize_sql(
        sql=(
            "SELECT T1.element "
            "FROM atom AS T1 "
            "JOIN connected AS T2 ON T1.atom_id = T2.atom_id"
        ),
        source="target_sql",
    )

    pred_paths = [ref["path_role"] for ref in pred_graph["output_refs"]]
    target_path = target_graph["output_refs"][0]["path_role"]

    assert pred_graph["aliases"]["a1"] == "atom"
    assert pred_paths[0] == target_path
    assert pred_paths[1] != target_path
    assert "connected.atom_id2" in pred_paths[1]


def test_role_graph_unifies_direct_and_joined_role_side_groups() -> None:
    joined_graph = RoleGraphNormalizer().normalize_sql(
        sql=(
            "SELECT a1.element, a2.element "
            "FROM connected c "
            "JOIN atom a1 ON c.atom_id = a1.atom_id "
            "JOIN atom a2 ON c.atom_id2 = a2.atom_id"
        ),
        source="pred_sql",
    )
    direct_graph = RoleGraphNormalizer().normalize_sql(
        sql="SELECT c.atom_id, c.atom_id2 FROM connected c",
        source="pred_sql",
    )

    joined_left = joined_graph["output_refs"][0]
    joined_right = joined_graph["output_refs"][1]
    direct_left = direct_graph["output_refs"][0]
    direct_right = direct_graph["output_refs"][1]

    assert joined_left["derived_role_path"] == direct_left["direct_role_path"]
    assert joined_right["derived_role_path"] == direct_right["direct_role_path"]
    assert joined_left["role_side_group"] == direct_left["role_side_group"]
    assert joined_right["role_side_group"] == direct_right["role_side_group"]
    assert joined_left["role_side_group"] == joined_right["role_side_group"]
    assert joined_left["side_key"] != joined_right["side_key"]


def test_normalizer_infers_select_contract_step_for_aggregate_unit_delta() -> None:
    error_instance = ErrorInstanceV2(
        db_id="toy",
        case_id="150",
        question_features=QuestionFeatures(),
        pred_sql_features=PredSqlFeatures(),
        deep_bias="counted distinct entity instead of answer unit",
        repair_goal="count the target output unit",
        repair_skeleton=RepairSkeleton(
            structural=RepairSkeletonStructural(
                locus=Locus.WHERE,
                op_family=OpFamily.REPLACE,
                target_family=TargetFamily.CONDITION,
                output_contract=OutputContract.UNCHANGED,
                output_shape_delta=OutputShapeDelta(
                    current_arity=1,
                    target_arity=1,
                    arity_delta=0,
                    arity_direction="same",
                    current_grain="scalar_aggregate",
                    target_grain="scalar_aggregate",
                ),
            ),
            semantic=RepairSkeletonSemantic(intent="replace predicate surface"),
        ),
        repair_program=[
            RepairProgramStep(
                step_id="where_1",
                op="WHERE_REPLACE_CONDITION",
                locus="WHERE",
                origin="case_extracted",
                extraction_source="llm_explicit",
            )
        ],
        rewrite_hint_proto="Use the target predicate and count unit.",
    )
    audit = CaseAudit(
        db_id="toy",
        case_id="150",
        question="How many accounts match?",
        pred_sql=(
            "SELECT COUNT(DISTINCT a.account_id) "
            "FROM account a JOIN trans t ON a.account_id = t.account_id "
            "WHERE t.bank = 'AB'"
        ),
        gold_sql=(
            "SELECT COUNT(T2.account_id) "
            "FROM account AS T2 JOIN trans AS T3 ON T2.account_id = T3.account_id "
            "WHERE T3.bank = 'AB'"
        ),
        final_error_reason="aggregation unit differs",
        minimal_fix="count account_id without DISTINCT",
    )

    ir = RepairProgramNormalizer().normalize_error_instance(
        error_instance=error_instance,
        case_audit=audit,
        formation_signals={
            "delta": {
                "delta_axes": ["aggregation_unit_delta"],
                "output_shape_delta": {
                    "current_arity": 1,
                    "target_arity": 1,
                    "arity_delta": 0,
                    "arity_direction": "same",
                    "current_grain": "scalar_aggregate",
                    "target_grain": "scalar_aggregate",
                },
            }
        },
    )

    effects = list(ir.repair_effect_signature.effect_candidates or [])
    assert effects
    assert any(effect.axis == "aggregation_unit_delta" for effect in effects)

    assert any(op.op_type == "WHERE_REPLACE_CONDITION" for op in ir.program_ops)
    assert any(
        op.op_type == "SELECT_REPLACE_SLOT"
        and op.arguments["source_step_id"] == "sql_delta_output_contract"
        for op in ir.program_ops
    )
    select_op = next(
        op
        for op in ir.program_ops
        if op.arguments["source_step_id"] == "sql_delta_output_contract"
    )
    assert select_op.arguments["source_output_shape"]["has_distinct"] is True
    assert "target_drops_distinct_output" in select_op.invariants


def test_synthesizer_aligns_by_contrastive_effect_before_op_surface() -> None:
    left = _output_patch_singleton(
        "206",
        op_type="SELECT_DROP_SLOT",
        current_arity=2,
        target_arity=1,
    )
    right = _output_patch_singleton(
        "307",
        op_type="SELECT_REPLACE_SLOT",
        current_arity=2,
        target_arity=1,
    )

    base_effect_payload = {
        "axis": "output_shape_delta",
        "source_state": {
            "output": {
                "arity": 2,
                "role_counts": {"identifier": 2},
                "direct_role_path_count": 2,
                "derived_role_path_count": 0,
                "role_side_group_count": 2,
                "example_refs": [],
            }
        },
        "target_state": {
            "output": {
                "arity": 1,
                "role_counts": {"identifier": 1},
                "example_refs": [],
            }
        },
        "delta": {
            "kind": "output_subset",
            "source_arity": 2,
            "target_arity": 1,
            "arity_delta": -1,
            "arity_direction": "decrease",
            "target_is_subset_of_source": True,
        },
        "role": "primary",
        "triggerability": {
            "source_visible_in_runtime": True,
            "target_bindable_from_schema_or_memory": True,
        },
        "actionability": {
            "primitive": "DROP_SIDE",
            "arguments_bindable": "unknown",
            "branch_count": 1,
            "branch_selection_answer_blind": True,
        },
        "evidence": {"source": "test_contrastive_effect"},
        "confidence": 0.95,
    }

    for group in (left, right):
        ir = CanonicalRepairIR.model_validate(group.formation_signals["canonical_repair_ir"])
        effect_payload = {
            **base_effect_payload,
            "source_state": {
                "output": {
                    "arity": 2,
                    "role_counts": {"identifier": 2}
                    if ir.case_id == "206"
                    else {"name": 2},
                    "direct_role_path_count": 2 if ir.case_id == "206" else 0,
                    "derived_role_path_count": 0 if ir.case_id == "206" else 2,
                    "role_side_group_count": 2,
                    "example_refs": [
                        {"style": "direct_endpoint"}
                        if ir.case_id == "206"
                        else {"style": "joined_role_side"}
                    ],
                }
            },
        }
        effect = {
            **effect_payload,
            "effect_id": f"effect:{ir.case_id}:output_shape_delta:test",
        }
        signature = RepairEffectSignature.model_validate({"effect_candidates": [effect]})
        ir.repair_effect_signature = signature
        for op in ir.program_ops:
            op.arguments["repair_effect_signature"] = signature.model_dump(mode="json")
        group.formation_signals["canonical_repair_ir"] = ir.model_dump(mode="json")
        group.instantiation_program.synthesized_program = singleton_program_from_ir(ir)
        group.instantiation_program.program_coverage = coverage_for_singleton_program(
            group.instantiation_program.synthesized_program
        )

    result = synthesize_shared_program([left, right])

    assert result.program is not None
    assert result.coverage.compile_coverage == 1.0
    assert result.program.repair_effect_signature is not None
    merged_effects = result.program.repair_effect_signature.effect_candidates
    assert merged_effects
    assert merged_effects[0].axis == "output_shape_delta"
    assert result.program.program_envelope is not None
    assert result.program.program_envelope.source_antipatterns[0]["kind"] == "contrastive_source_state"


def test_coverage_accepts_singleton_direct_output_shape_delta() -> None:
    singleton = _singleton("206")
    coverage = CompilerCoverageValidator().validate_group(singleton)

    assert coverage.compile_coverage == 1.0
    assert coverage.static_program_coverage == 1.0
    assert coverage.runtime_binding_coverage == 0.0
    assert coverage.member_candidate_coverage == 1.0
    assert coverage.blockers == []


def test_coverage_uses_runtime_member_binding_when_case_views_are_supplied() -> None:
    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    good_view = RuntimeCaseView(
        db_id="toy",
        case_id="206",
        question="Return one endpoint.",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
            select_shape="arity=2",
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id"]},
        ),
    )
    bad_view = RuntimeCaseView(
        db_id="toy",
        case_id="249",
        question="Return one endpoint.",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id FROM a",
            columns=["a.id"],
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a"],
            columns_by_table={"a": ["id"]},
        ),
    )

    coverage = CompilerCoverageValidator().validate_group(
        family,
        member_case_views={"206": good_view, "249": bad_view},
    )

    assert coverage.compile_coverage == 0.5
    assert coverage.static_program_coverage == 1.0
    assert coverage.runtime_binding_coverage == 0.5
    assert coverage.member_candidate_coverage == 0.5
    assert coverage.compile_success_by_member == {"206": True, "249": False}
    assert coverage.failure_reasons["249"] in {
        "compiler_no_candidates",
        "canonical_ops_unbound:206:op",
    }


def test_contract_accepts_singleton_direct_shape_with_explicit_step() -> None:
    singleton = _singleton("206")
    singleton.instantiation_program.repair_program = [
        RepairProgramStep(
            step_id="s1",
            op="SELECT_DROP_SLOT",
            locus="SELECT",
            origin="case_extracted",
            extraction_source="llm_explicit",
            supporting_case_ids=["206"],
        )
    ]
    singleton.trigger_contract.action_contract = {
        "repair_program": [
            step.model_dump(mode="json")
            for step in singleton.instantiation_program.repair_program
        ]
    }

    assert _contract_program_issues(singleton) == []


def test_synthesizer_blocks_groups_without_shared_canonical_ops() -> None:
    result = synthesize_shared_program(
        [_singleton("206", "SELECT_DROP_SLOT"), _singleton("198", "JOIN_ADD_BRIDGE", locus="JOIN")]
    )

    assert result.program is None
    assert "no_shared_canonical_program" in result.coverage.blockers


def test_synthesizer_anti_unifies_surface_step_variants_with_shared_invariant() -> None:
    bridge_invariants = [
        "target_requires_additional_table",
        "target_join_path_expanded",
    ]
    result = synthesize_shared_program(
        [
            _singleton("198", "JOIN_ADD_TABLE", locus="JOIN", invariants=bridge_invariants),
            _singleton("207", "JOIN_ADD_BRIDGE", locus="JOIN", invariants=bridge_invariants),
        ]
    )

    assert result.program is not None
    assert result.coverage.compile_coverage == 1.0
    assert result.program.ops[0].op_type == "JOIN_ADD_BRIDGE"
    assert result.program.program_type == "join_bridge"
    assert result.program.target_invariants == sorted(bridge_invariants)
    shared_signature = result.program.ops[0].arguments["shared_signature"]
    assert shared_signature["common_invariants"] == sorted(bridge_invariants)
    assert shared_signature["member_op_types"] == ["JOIN_ADD_BRIDGE", "JOIN_ADD_TABLE"]


def test_family_formation_stores_synthesized_program_not_surface_intersection() -> None:
    family = build_family_from_groups([_singleton("206"), _singleton("249")])

    assert family.instantiation_program.synthesized_program is not None
    assert family.instantiation_program.program_coverage is not None
    assert family.instantiation_program.program_coverage.compile_coverage == 1.0
    assert family.instantiation_program.repair_program[0].op == "SELECT_DROP_SLOT"


def test_family_slots_come_from_synthesized_program_not_representative() -> None:
    left = _singleton("206")
    right = _singleton("249")
    left.instantiation_program.slots = [
        InstantiationSlot(
            name="representative_only",
            kind="column",
            required=True,
            allowed_role_families=["nonexistent_role"],
        )
    ]

    family = build_family_from_groups([left, right])

    assert family.instantiation_program.slots == []
    assert family.formation_signals["representative_group_id"] == left.group_id


def test_family_trigger_contract_keeps_member_variant_shape_alternatives() -> None:
    contract = build_trigger_contract(
        formation_signals={
            "pred_current": {
                "select_arity": 1,
                "output_shape_current": {
                    "arity": 1,
                    "grain": "row_result",
                    "roles": ["other"],
                },
            },
            "repair_skeleton": _skeleton().model_dump(mode="json"),
            "synthesized_program": {
                "ops": [
                    {
                        "arguments": {
                            "member_argument_variants": [
                                {
                                    "source_output_shape": {
                                        "arity": 1,
                                        "grain": "row_result",
                                        "roles": ["other"],
                                        "has_aggregate": False,
                                    }
                                },
                                {
                                    "source_output_shape": {
                                        "arity": 3,
                                        "grain": "scalar_aggregate",
                                        "roles": ["other", "other", "other"],
                                        "has_aggregate": True,
                                    }
                                },
                            ]
                        }
                    }
                ]
            },
        }
    )

    assert "pred.select_arity=1" not in contract["required_signals"]
    assert "pred.select_arity_present=True" not in contract["required_signals"]
    assert "pred.select_arity_present=True" in contract["audit_signals"]
    assert any(
        "pred.select_arity=3" in signal_set
        and "pred.has_aggregate=True" in signal_set
        for signal_set in contract["variant_required_signal_sets"]
    )


def test_trigger_contract_requires_runtime_binding_for_deterministic_selection() -> None:
    contract = build_trigger_contract(
        formation_signals={
            "pred_current": {
                "select_arity": 2,
                "output_shape_current": {"arity": 2, "roles": ["other", "other"]},
            },
            "repair_skeleton": _skeleton().model_dump(mode="json"),
            "synthesized_program": {
                "ops": [{"op_id": "shared-op-1"}],
                "program_envelope": {
                    "action_envelope": {"max_actions_hint": 1, "bundles": [{"bundle_id": "b1"}]}
                },
            },
            "program_coverage": {
                "compile_coverage": 1.0,
                "static_program_coverage": 1.0,
                "runtime_binding_coverage": 0.0,
            },
        }
    )

    assert contract["action_contract"]["compiler_deterministic"] is False
    assert contract["action_contract"]["selection_policy"] == "llm_required"


def test_runtime_trigger_allows_substantive_variant_only_contract() -> None:
    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    family.runtime_usable = True
    family.trigger_contract = family.trigger_contract.model_copy(
        update={
            "required_signals": [],
            "variant_required_signal_sets": [
                ["pred.select_arity=2", "pred.output_role=identifier"]
            ],
            "decisive_pred_signals": [],
        }
    )
    library = LibraryStateV2(db_id="toy", experience_families=[family])
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="253",
        question="Which endpoint should be returned?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
            select_shape="arity=2",
        ),
        case_signal_view=CaseSignalView(
            db_id="toy",
            pred_sql_view=PredSqlSignalView(
                select_arity=2,
                output_shape_current={"arity": 2, "roles": ["identifier"]},
            ),
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id"]},
        ),
    )

    result = trigger_memory_objects(library=library, case_view=case_view, db_id="toy")

    assert [group.group_id for group in result.selected_groups] == [family.group_id]
    assert any("variant_required_signals_matched" in audit.gate_reasons for audit in result.candidates)


def test_runtime_pattern_soft_required_miss_requires_binder_success() -> None:
    pattern = build_family_from_groups([_singleton("206"), _singleton("249")], runtime_usable=True)
    pattern.trigger_contract = pattern.trigger_contract.model_copy(
        update={
            "required_signals": ["pred.output_role=missing_role"],
            "variant_required_signal_sets": [],
            "decisive_pred_signals": ["pred.output_role=identifier"],
        }
    )
    library = LibraryStateV2(db_id="toy", patterns=[pattern])
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="253",
        question="Which endpoint should be returned?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
            select_shape="arity=2",
        ),
        case_signal_view=CaseSignalView(
            db_id="toy",
            pred_sql_view=PredSqlSignalView(
                select_arity=2,
                output_shape_current={"arity": 2, "roles": ["identifier"]},
            ),
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id"]},
        ),
    )

    result = trigger_memory_objects(library=library, case_view=case_view, db_id="toy")

    assert [group.group_id for group in result.selected_groups] == [pattern.group_id]
    assert any(
        "pattern_soft_required_signals_recovered_by_binder" in audit.gate_reasons
        for audit in result.candidates
        if audit.group_id == pattern.group_id
    )


def test_runtime_trigger_rejects_broad_presence_only_variant_contract() -> None:
    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    family.runtime_usable = True
    family.trigger_contract = family.trigger_contract.model_copy(
        update={
            "required_signals": [],
            "variant_required_signal_sets": [["pred.select_arity_present=True"]],
            "decisive_pred_signals": [],
        }
    )
    library = LibraryStateV2(db_id="toy", experience_families=[family])
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="253",
        question="Which endpoint should be returned?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
            select_shape="arity=2",
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id"]},
        ),
    )

    result = trigger_memory_objects(library=library, case_view=case_view, db_id="toy")

    assert result.selected_groups == []
    assert any(
        "variant_required_signals_non_substantive" in audit.gate_reasons
        for audit in result.candidates
    )


def test_trigger_contract_filters_representative_role_artifacts_from_variant_gates() -> None:
    contract = build_trigger_contract(
        formation_signals={
            "pred_current": {
                "select_arity": 2,
                "output_shape_current": {"arity": 2, "roles": ["other", "other"]},
            },
            "repair_skeleton": _skeleton().model_dump(mode="json"),
            "synthesized_program": {
                "ops": [
                    {
                        "arguments": {
                            "member_argument_variants": [
                                {
                                    "source_output_shape": {
                                        "arity": 2,
                                        "roles": ["other", "other"],
                                        "has_aggregate": False,
                                    },
                                    "source_output_refs": [
                                        {
                                            "sql_role": "output_slot",
                                            "column_role": "other",
                                            "path_role": "output_slot_0",
                                            "relation_role": "unknown_table",
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                ]
            },
        }
    )

    variant_text = "\n".join(
        signal
        for signal_set in contract["variant_required_signal_sets"]
        for signal in signal_set
    )
    required_text = "\n".join(contract["required_signals"])

    assert "pred.select_arity=2" in variant_text
    assert "pred.contains_path_role=output_slot_0" not in variant_text
    assert "pred.contains_relation_role=unknown_table" not in variant_text
    assert "pred.contains_column_role=other" not in variant_text
    assert "output_slot_0" not in required_text
    assert "unknown_table" not in required_text
    discriminants_text = "\n".join(contract["canonical_discriminants"])
    assert "pred.contains_path_role=output_slot_0" not in discriminants_text
    assert "pred.contains_relation_role=unknown_table" not in discriminants_text
    assert "pred.contains_column_role=other" not in discriminants_text


def test_rewrite_realization_origin_ignores_non_mutating_contract_notes() -> None:
    assert not contract_steps_changed_sql(["SELECT_ENFORCE_DISTINCT already present"])
    assert not contract_steps_changed_sql(
        ["SELECT_ENFORCE_DISTINCT not applied: guard_not_bound_or_no_visible_duplicate_risk"]
    )
    assert contract_steps_changed_sql(
        ["SELECT_ENFORCE_DISTINCT applied from explicit repair_program dependency"]
    )

    result = {
        "contract_steps_applied": ["SELECT_ENFORCE_DISTINCT already present"],
        "dependency_repairs_applied": [],
    }
    assert rewrite_realization_origin_from_result(result) == "memory_rewrite_llm"

    changed = {
        "contract_steps_applied": ["WHERE_DROP_RANKING_PREDICATE dropped 1 source ranking predicate(s)"],
        "dependency_repairs_applied": [],
    }
    assert (
        rewrite_realization_origin_from_result(changed)
        == "post_rewrite_contract_realization"
    )


def test_runtime_family_requires_synthesized_program_even_with_repair_program() -> None:
    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    family.runtime_usable = True
    family.instantiation_program = family.instantiation_program.model_copy(
        update={
            "synthesized_program": None,
            "shared": False,
            "shared_status": "none",
            "repair_program": [
                RepairProgramStep(
                    step_id="legacy",
                    op="SELECT_DROP_SLOT",
                    locus="SELECT",
                )
            ],
        }
    )
    family.trigger_contract = family.trigger_contract.model_copy(
        update={
            "required_signals": ["pred.output_role=identifier"],
            "variant_required_signal_sets": [],
            "decisive_pred_signals": ["pred.output_role=identifier"],
            "action_contract": {
                "repair_program": [{"op": "SELECT_DROP_SLOT", "locus": "SELECT"}],
                "has_synthesized_program": False,
            },
        }
    )
    library = LibraryStateV2(db_id="toy", experience_families=[family])
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="253",
        question="Which endpoint should be returned?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
            select_shape="arity=2",
        ),
        case_signal_view=CaseSignalView(
            db_id="toy",
            pred_sql_view=PredSqlSignalView(
                select_arity=2,
                output_shape_current={"arity": 2, "roles": ["identifier"]},
            ),
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id"]},
        ),
    )

    result = trigger_memory_objects(library=library, case_view=case_view, db_id="toy")

    assert result.selected_groups == []
    assert any(
        "runtime_group_missing_synthesized_program" in audit.gate_reasons
        for audit in result.candidates
    )


def test_family_runtime_contract_uses_program_not_representative_snapshot() -> None:
    left = _singleton("206")
    right = _singleton("249")
    left.formation_signals["pred_current"] = {"table_count": 99}
    right.formation_signals["pred_current"] = {"table_count": 88}

    family = build_family_from_groups([left, right])

    assert family.formation_signals["pred_current"] == {}
    assert family.formation_signals["delta"] == {}
    assert family.formation_signals["representative_snapshot"]["pred_current"]
    assert (
        family.core_interface.repair_skeleton_prototype.semantic.notes
        == "Derived from synthesized_program; representative skeleton is audit-only."
    )
    assert "206" not in family.core_interface.repair_goal
    assert "249" not in family.core_interface.repair_goal
    assert family.instantiation_program.synthesized_program is not None


def test_current_case_signals_include_canonical_role_discriminants() -> None:
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="253",
        question="Which endpoint should be returned?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
            select_shape="arity=2",
        ),
        case_signal_view=CaseSignalView(
            db_id="toy",
            pred_sql_view=PredSqlSignalView(
                select_arity=2,
                select_role_profile=["identifier", "identifier"],
                output_shape_current={"arity": 2, "roles": ["identifier"]},
                join_graph=[
                    {
                        "role_key": "identifier=identifier",
                        "left": {
                            "column_role": "identifier",
                            "path_role": "join_endpoint:a.id=b.a_id:a.id",
                            "relation_role": "source_endpoint",
                        },
                        "right": {
                            "column_role": "identifier",
                            "path_role": "join_endpoint:a.id=b.a_id:b.a_id",
                            "relation_role": "target_endpoint",
                        },
                    }
                ],
            ),
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id"]},
        ),
    )

    signals = build_current_case_signals(case_view)

    assert "pred.contains_column_role=identifier" in signals
    assert "pred.contains_sql_role=join_endpoint" in signals
    assert "pred.contains_path_role=join_endpoint:a.id=b.a_id:a.id" in signals
    assert "pred.contains_relation_role=source_endpoint" in signals


def test_summary_uses_c0_not_top1_for_gain_accounting() -> None:
    from method.EEA.rulebook.cli.run_online_e2e_validation import _summary_payload

    args = SimpleNamespace(
        db_id="toy",
        work_root="/tmp/work",
        rewrite_all_candidates=True,
        accumulate_sql_source="top1",
        row_sample_limit=1,
        promotion_interval=1,
        family_runtime_policy="replay_gated",
        manual_groups_json="",
        strict_contract_policy="continue",
        max_cases=0,
        case_ids="",
    )
    rows = [
        {
            "question_id": 1,
            "pred_status": "not_equivalent",
            "c0_status": "equivalent",
            "final_status": "equivalent",
            "rewrites": [{"rewrite_status": "equivalent"}],
            "memory_gain_origin": "llm_action_selection",
        },
        {
            "question_id": 2,
            "pred_status": "not_equivalent",
            "c0_status": "not_equivalent",
            "final_status": "equivalent",
            "rewrites": [{"rewrite_status": "equivalent"}],
            "memory_gain_origin": "llm_action_selection",
        },
    ]

    payload = _summary_payload(
        args=args,
        rows=rows,
        library=LibraryStateV2(db_id="toy"),
        started_at=0.0,
        family_reports=[],
    )

    assert payload["summary"]["baseline_equivalent_count"] == 1
    assert payload["summary"]["final_equivalent_count"] == 2
    assert payload["summary"]["improved_cases"] == [2]
    assert payload["summary"]["memory_gain_by_origin"] == {"llm_action_selection": [2]}


def test_runtime_trigger_generalized_canonical_gate_bypasses_legacy_decisive_gate() -> None:
    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    family.runtime_usable = True
    family.trigger_contract = family.trigger_contract.model_copy(
        update={
            "required_signals": [],
            "variant_required_signal_sets": [["pred.output_role=missing_role"]],
            "canonical_discriminants": ["pred.output_role=identifier"],
            "decisive_pred_signals": [],
            "trigger_policy": {
                "allow_out_of_variant_generalization": True,
                "min_canonical_discriminants": 1,
                "requires_binder_dry_run": True,
            },
        }
    )
    library = LibraryStateV2(db_id="toy", experience_families=[family])
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="253",
        question="Which endpoint should be returned?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
            select_shape="arity=2",
        ),
        case_signal_view=CaseSignalView(
            db_id="toy",
            pred_sql_view=PredSqlSignalView(
                select_arity=2,
                output_shape_current={"arity": 2, "roles": ["identifier"]},
            ),
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id"]},
        ),
    )

    result = trigger_memory_objects(library=library, case_view=case_view, db_id="toy")

    assert [group.group_id for group in result.selected_groups] == [family.group_id]
    assert any("generalized_canonical_gate_passed" in audit.gate_reasons for audit in result.candidates)


def test_runtime_trigger_ignores_program_discriminants_for_current_case_gate() -> None:
    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    family.runtime_usable = True
    family.trigger_contract = family.trigger_contract.model_copy(
        update={
            "required_signals": [],
            "variant_required_signal_sets": [["pred.output_role=missing_role"]],
            "canonical_discriminants": ["program.lowering_family=select_drop"],
            "decisive_pred_signals": [],
            "trigger_policy": {
                "allow_out_of_variant_generalization": True,
                "min_canonical_discriminants": 1,
                "requires_binder_dry_run": False,
            },
        }
    )
    library = LibraryStateV2(db_id="toy", experience_families=[family])
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="253",
        question="Which endpoint should be returned?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
            select_shape="arity=2",
        ),
        case_signal_view=CaseSignalView(
            db_id="toy",
            pred_sql_view=PredSqlSignalView(
                select_arity=2,
                output_shape_current={"arity": 2, "roles": ["identifier"]},
            ),
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id"]},
        ),
    )

    result = trigger_memory_objects(library=library, case_view=case_view, db_id="toy")

    assert result.selected_groups == []
    assert any(
        "canonical_discriminants_insufficient" in audit.gate_reasons
        for audit in result.candidates
    )


def test_trigger_contract_filters_broad_signals_from_hard_gates() -> None:
    contract = build_trigger_contract(
        formation_signals={
            "pred_current": {
                "select_arity": 2,
                "output_shape_current": {"arity": 2, "roles": []},
            },
            "repair_skeleton": RepairSkeleton(
                structural=RepairSkeletonStructural(
                    locus=Locus.SELECT,
                    op_family=OpFamily.ADD,
                    target_family=TargetFamily.SHAPE,
                    output_contract=OutputContract.UNCHANGED,
                    output_shape_delta=OutputShapeDelta(
                        current_arity=2,
                        target_arity=3,
                        arity_direction="increase",
                    ),
                ),
                semantic=RepairSkeletonSemantic(intent="add missing output column"),
            ).model_dump(mode="json"),
        }
    )

    assert "pred.select_arity_present=True" not in contract["required_signals"]
    assert "pred.select_arity_present=True" not in contract["decisive_pred_signals"]
    assert "pred.select_arity_present=True" in contract["audit_signals"]


def test_synthesized_program_without_program_signals_does_not_use_representative_pred_required() -> None:
    contract = build_trigger_contract(
        formation_signals={
            "pred_current": {
                "select_arity": 2,
                "output_shape_current": {"arity": 2, "roles": ["identifier"]},
            },
            "repair_skeleton": _skeleton().model_dump(mode="json"),
            "synthesized_program": {
                "ops": [
                    {
                        "op_id": "shared:opaque",
                        "op_type": "OPAQUE_REPAIR",
                        "locus": "OPAQUE",
                        "arguments": {"shared_signature": {}},
                    }
                ]
            },
        }
    )

    assert "pred.select_arity=2" not in contract["required_signals"]
    assert "pred.output_role=identifier" not in contract["required_signals"]
    assert "pred.select_arity=2" not in contract["decisive_pred_signals"]


def test_action_compiler_lowers_canonical_role_side_to_drop_side() -> None:
    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="253",
        question="Which endpoint should be returned?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
        ),
        candidate_set_summary=CandidateSetSummary(
            size=2,
            top1_hash="top1",
            beam_present=True,
            candidate_summaries=[
                {
                    "rank": 0,
                    "sql_hash": "top1",
                    "is_top1": True,
                    "tables": ["a", "b"],
                    "select_shape": {"output_arity": 2},
                },
                {
                    "rank": 1,
                    "sql_hash": "beam1",
                    "is_top1": False,
                    "tables": ["a"],
                    "select_shape": {"output_arity": 1},
                },
            ],
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id", "element"], "b": ["id"]},
        ),
    )

    candidate_sets, _diag = enumerate_candidates(
        case_view=case_view,
        memory_objects=[family],
    )
    drop_side = next(cs for cs in candidate_sets if cs.primitive == ActionPrimitive.DROP_SIDE)
    drop_select = next(cs for cs in candidate_sets if cs.primitive == ActionPrimitive.DROP_SELECT_SLOT)

    assert drop_side.candidates
    assert drop_select.candidates
    assert drop_side.candidates[0].arguments["canonical_op_type"] == "SELECT_DROP_SLOT"
    assert drop_side.candidates[0].arguments["compiled_from_program_id"]
    assert "canonical_program_op" not in drop_side.candidates[0].arguments
    assert drop_side.candidates[0].arguments["canonical_contract"]["op_type"] == "SELECT_DROP_SLOT"


def test_deterministic_fallback_requires_explicit_compiler_deterministic_contract() -> None:
    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    family = family.model_copy(
        update={
            "trigger_contract": family.trigger_contract.model_copy(
                update={
                    "action_contract": {
                        **family.trigger_contract.action_contract,
                        "selection_policy": "deterministic_only",
                        "compiler_deterministic": True,
                    }
                }
            )
        }
    )
    program = family.instantiation_program.synthesized_program
    assert program is not None
    op_id = program.ops[0].op_id
    candidate_sets = [
        ActionCandidateSet(
            primitive=ActionPrimitive.DROP_SELECT_SLOT,
            candidates=[
                ActionCandidate(
                    candidate_id="cand-1",
                    arguments={"canonical_op_id": op_id},
                    provenance="unit_test",
                    source_group_id=family.group_id,
                    source_group_type=GroupType.FAMILY,
                )
            ],
        )
    ]

    actions, notes = _deterministic_canonical_fallback_actions(
        candidate_sets=candidate_sets,
        memory_objects=[family],
        Action=Action,
        EditScope=EditScope,
        GroupType=GroupType,
        RiskLevel=RiskLevel,
    )

    assert len(actions) == 1
    assert actions[0].selection_origin == "deterministic_unique"
    assert actions[0].fallback_used is True
    assert notes == [f"{family.group_id}:{op_id}:cand-1"]

    blocked_contract = family.trigger_contract.model_copy(
        update={
            "action_contract": {
                **family.trigger_contract.action_contract,
                "selection_policy": "llm_required",
                "compiler_deterministic": False,
            }
        }
    )
    blocked = family.model_copy(update={"trigger_contract": blocked_contract})
    blocked_actions, blocked_notes = _deterministic_canonical_fallback_actions(
        candidate_sets=candidate_sets,
        memory_objects=[blocked],
        Action=Action,
        EditScope=EditScope,
        GroupType=GroupType,
        RiskLevel=RiskLevel,
    )

    assert blocked_actions == []


def test_deterministic_fallback_prefers_bundle_primary_primitive() -> None:
    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    family = family.model_copy(
        update={
            "trigger_contract": family.trigger_contract.model_copy(
                update={
                    "action_contract": {
                        **family.trigger_contract.action_contract,
                        "selection_policy": "deterministic_only",
                        "compiler_deterministic": True,
                    }
                }
            )
        }
    )
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="307",
        question="Return one endpoint.",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id"]},
        ),
    )

    candidate_sets, _diag = enumerate_candidates(
        case_view=case_view,
        memory_objects=[family],
    )
    actions, _notes = _deterministic_canonical_fallback_actions(
        candidate_sets=candidate_sets,
        memory_objects=[family],
        Action=Action,
        EditScope=EditScope,
        GroupType=GroupType,
        RiskLevel=RiskLevel,
    )

    assert len(actions) == 1
    assert actions[0].arguments["bundle_id"]
    assert actions[0].primitive in {
        ActionPrimitive.DROP_SIDE,
        ActionPrimitive.DROP_SELECT_SLOT,
    }


def test_compiler_dry_run_uses_bundle_budget_instead_of_raw_op_count() -> None:
    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="307",
        question="Return one endpoint.",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id"]},
        ),
    )

    ok, reasons = _compiler_dry_run_gate(
        group=family,
        case_view=case_view,
        contract=family.trigger_contract.model_dump(mode="json"),
    )

    assert ok is True
    assert not any("missing_required_ops" in reason for reason in reasons)
    assert not any("action_budget_exceeded" in reason for reason in reasons)


def test_compiler_dry_run_reports_branch_selection_ambiguity(monkeypatch) -> None:
    from method.EEA.rulebook.common.runtime import runtime as runtime_module

    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="307",
        question="Return one endpoint.",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id"]},
        ),
    )
    contract = family.trigger_contract.model_dump(mode="json")
    contract["action_contract"]["program_envelope"]["branch_selection_contract"] = {
        "requires_current_variant_binding": True
    }
    bundle_id = contract["action_contract"]["program_envelope"]["action_envelope"][
        "bundles"
    ][0]["bundle_id"]
    monkeypatch.setattr(
        runtime_module,
        "_binder_dry_run_candidates",
        lambda group, case_view: (
            [
                SimpleNamespace(
                    bundle_id=bundle_id,
                    arguments={"bundle_id": bundle_id, "bundle_selection_key": "left"},
                ),
                SimpleNamespace(
                    bundle_id=bundle_id,
                    arguments={"bundle_id": bundle_id, "bundle_selection_key": "right"},
                ),
            ],
            "binder_candidates_available",
        ),
    )

    ok, reasons = _compiler_dry_run_gate(
        group=family,
        case_view=case_view,
        contract=contract,
    )

    assert ok is False
    assert any("branch_selection_ambiguous:" in reason for reason in reasons)


def test_trigger_reports_target_invariant_unbindable() -> None:
    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    contract = family.trigger_contract.model_dump(mode="json")
    contract["action_contract"]["program_envelope"]["target_invariants"] = [
        {
            "kind": "target_added_relation_equality",
            "value": "missing_table.id=a.id",
        }
    ]
    patched_family = family.model_copy(update={"trigger_contract": contract})
    library = LibraryStateV2(db_id="toy", experience_families=[patched_family])
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="307",
        question="Return one endpoint.",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
            select_shape="arity=2",
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id"]},
        ),
    )

    result = trigger_memory_objects(library=library, case_view=case_view, db_id="toy")

    assert result.selected_groups == []
    assert any(
        "target_invariant_unbindable:target_added_relation_equality"
        in ",".join(audit.gate_reasons)
        for audit in result.candidates
        if audit.group_id == patched_family.group_id
    )


def test_action_compiler_fails_closed_when_subset_drop_slot_is_unbound() -> None:
    family = build_family_from_groups(
        [
            _singleton("206", op_type="SELECT_REPLACE_SLOT", include_output_refs=False),
            _singleton("249", op_type="SELECT_REPLACE_SLOT", include_output_refs=False),
        ]
    )
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="253",
        question="Which endpoint should be returned?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
        ),
        candidate_set_summary=CandidateSetSummary(
            size=2,
            top1_hash="top1",
            beam_present=True,
            candidate_summaries=[
                {
                    "rank": 0,
                    "sql_hash": "top1",
                    "is_top1": True,
                    "tables": ["a", "b"],
                    "select_shape": {"output_arity": 2},
                },
                {
                    "rank": 1,
                    "sql_hash": "beam1",
                    "is_top1": False,
                    "tables": ["a"],
                    "select_shape": {"output_arity": 1},
                },
            ],
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id"]},
        ),
    )

    candidate_sets, _diag = enumerate_candidates(
        case_view=case_view,
        memory_objects=[family],
    )
    drop_side = next(cs for cs in candidate_sets if cs.primitive == ActionPrimitive.DROP_SIDE)
    drop_select = next(cs for cs in candidate_sets if cs.primitive == ActionPrimitive.DROP_SELECT_SLOT)

    assert not drop_side.candidates
    assert not drop_select.candidates
    assert "target_output_subset_slot_binding_unresolved" in str(drop_side.empty_reason)
    assert "target_output_subset_slot_binding_unresolved" in str(drop_select.empty_reason)


def test_action_compiler_fails_closed_when_unresolved_output_variant_does_not_match() -> None:
    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    op = family.instantiation_program.synthesized_program.ops[0]
    op.arguments.setdefault("shared_arguments", {})["unresolved_variation_axes"] = [
        "output_shape.current_arity",
        "output_shape.current_grain",
    ]
    for variant in op.arguments.get("member_argument_variants") or []:
        variant["source_output_shape"] = {
            "arity": 2,
            "has_aggregate": False,
            "has_distinct": False,
        }

    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="scalar",
        question="How many ids?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT COUNT(DISTINCT a.id) FROM a",
            columns=["COUNT(DISTINCT a.id)"],
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id"]},
        ),
    )

    candidate_sets, _diag = enumerate_candidates(
        case_view=case_view,
        memory_objects=[family],
    )
    drop_select = next(cs for cs in candidate_sets if cs.primitive == ActionPrimitive.DROP_SELECT_SLOT)

    assert not drop_select.candidates
    assert "current_output_shape_unmatched_to_program_variants" in str(drop_select.empty_reason)


def test_action_compiler_refines_coarse_runtime_roles_from_projection_names() -> None:
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="role-refine",
        question="List client and district.",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql=(
                "SELECT c.client_id, d.district_name "
                "FROM client c JOIN district d ON c.district_id = d.district_id"
            ),
            columns=["c.client_id", "d.district_name"],
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["client", "district"],
            columns_by_table={
                "client": ["client_id", "district_id"],
                "district": ["district_id", "district_name"],
            },
        ),
        case_signal_view=CaseSignalView(
            db_id="toy",
            case_id="role-refine",
            pred_sql_view=PredSqlSignalView(
                select_arity=2,
                output_shape_current={
                    "arity": 2,
                    "grain": "row_result",
                    "roles": ["identifier_like", "column_expr"],
                },
            ),
        ),
    )

    shape = _current_output_shape_for_compiler(case_view)

    assert shape["roles"] == ["identifier_like", "name"]


def test_action_compiler_splits_arity_increase_refs_into_replace_and_add() -> None:
    family = build_family_from_groups(
        [
            _singleton("180", "SELECT_REPLACE_SLOT"),
            _singleton("193", "SELECT_REPLACE_SLOT"),
        ]
    )
    family.instantiation_program.slots = [
        InstantiationSlot(name="target_column", kind="column")
    ]
    op = family.instantiation_program.synthesized_program.ops[0]
    target_refs = [
        {
            "ref_id": "target_sql:output:0",
            "source": "target_sql",
            "table": "alpha",
            "column": "alpha_id",
            "expression": "a.alpha_id",
            "slot_index": 0,
            "sql_role": "output_slot",
        },
        {
            "ref_id": "target_sql:output:1",
            "source": "target_sql",
            "table": "beta",
            "column": "beta_id",
            "expression": "b.beta_id",
            "slot_index": 1,
            "sql_role": "output_slot",
        },
        {
            "ref_id": "target_sql:output:2",
            "source": "target_sql",
            "table": "beta",
            "column": "beta_code",
            "expression": "b.beta_code",
            "slot_index": 2,
            "sql_role": "output_slot",
        },
    ]
    source_refs = [
        {
            "ref_id": "pred_sql:output:0",
            "source": "pred_sql",
            "table": "alpha",
            "column": "alpha_id",
            "expression": "a.alpha_id",
            "slot_index": 0,
            "sql_role": "output_slot",
        },
        {
            "ref_id": "pred_sql:output:1",
            "source": "pred_sql",
            "table": "beta",
            "column": "beta_label",
            "expression": "b.beta_label",
            "slot_index": 1,
            "sql_role": "output_slot",
        },
    ]
    source_relations = [
        {
            "left": {"table": "alpha", "column": "beta_id"},
            "right": {"table": "beta", "column": "beta_id"},
            "canonical_key": "alpha.beta_id=beta.beta_id",
        }
    ]
    target_relations = [
        {
            "left": {"table": "alpha", "column": "gamma_id"},
            "right": {"table": "gamma", "column": "gamma_id"},
            "canonical_key": "alpha.gamma_id=gamma.gamma_id",
        },
        {
            "left": {"table": "gamma", "column": "beta_id"},
            "right": {"table": "beta", "column": "beta_id"},
            "canonical_key": "beta.beta_id=gamma.beta_id",
        },
    ]
    op.arguments["shared_arguments"]["output_shape_delta"] = {
        "current_arity": 2,
        "target_arity": 3,
        "arity_delta": 1,
        "arity_direction": "increase",
    }
    op.arguments["shared_arguments"]["target_output_refs"] = target_refs
    op.arguments["shared_arguments"]["source_output_refs"] = source_refs
    op.arguments["member_argument_variants"] = [
        {
            "output_shape_delta": {
                "current_arity": 2,
                "target_arity": 3,
                "arity_delta": 1,
                "arity_direction": "increase",
            },
            "source_output_shape": {
                "arity": 2,
                "roles": ["identifier", "name"],
                "has_aggregate": False,
                "has_distinct": False,
            },
            "source_output_refs": source_refs,
            "target_output_refs": target_refs,
            "source_equality_relations": source_relations,
            "target_equality_relations": target_relations,
        }
    ]

    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="mixed-projection",
        question="List each alpha id, beta id, and beta code.",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql=(
                "SELECT a.alpha_id, b.beta_label "
                "FROM alpha a JOIN beta b ON a.beta_id = b.beta_id"
            ),
            columns=["a.alpha_id", "b.beta_label"],
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["alpha", "beta", "gamma"],
            columns_by_table={
                "alpha": ["alpha_id", "beta_id", "gamma_id"],
                "beta": ["beta_id", "beta_label", "beta_code"],
                "gamma": ["gamma_id", "beta_id"],
            },
        ),
    )

    candidate_sets, _diag = enumerate_candidates(
        case_view=case_view,
        memory_objects=[family],
    )
    add_select = next(cs for cs in candidate_sets if cs.primitive == ActionPrimitive.ADD_SELECT_SLOT)
    replace_select = next(
        cs for cs in candidate_sets if cs.primitive == ActionPrimitive.REPLACE_SELECT_SLOT
    )
    reroute = next(cs for cs in candidate_sets if cs.primitive == ActionPrimitive.REROUTE_FACT)

    assert add_select.candidates
    assert {
        candidate.arguments["target_columns"][0]["target_column"]
        for candidate in add_select.candidates
    } == {"beta_code"}
    assert {
        ref["column"]
        for candidate in add_select.candidates
        for ref in candidate.arguments["target_output_refs"]
    } == {"beta_code"}
    assert {
        ref["column"]
        for candidate in add_select.candidates
        for ref in candidate.arguments["canonical_contract"]["target_output_refs"]
    } == {"beta_code"}
    assert replace_select.candidates
    assert {
        (
            tuple(candidate.arguments["source_slot_indexes"]),
            candidate.arguments["target_columns"][0]["target_column"],
        )
        for candidate in replace_select.candidates
    } == {((1,), "beta_id")}
    assert {
        ref["column"]
        for candidate in replace_select.candidates
        for ref in candidate.arguments["target_output_refs"]
    } == {"beta_id"}
    assert {
        ref["column"]
        for candidate in replace_select.candidates
        for ref in candidate.arguments["canonical_contract"]["target_output_refs"]
    } == {"beta_id"}
    assert reroute.candidates
    assert {
        edge["canonical_key"]
        for candidate in reroute.candidates
        for edge in candidate.arguments["target_relation_edges"]
    } == {"alpha.gamma_id=gamma.gamma_id", "beta.beta_id=gamma.beta_id"}
    assert all(
        "SUBQUERY" not in set(candidate.arguments["required_edit_scopes"])
        for candidate in reroute.candidates
    )
    assert all(
        {"FROM", "JOIN", "SELECT"} <= set(candidate.arguments["required_edit_scopes"])
        for candidate in reroute.candidates
    )


def test_variant_requires_relation_reroute_only_for_target_relation_delta() -> None:
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="relation-delta",
        question="List beta codes.",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT b.beta_code FROM alpha a JOIN beta b ON a.beta_id = b.beta_id",
            columns=["b.beta_code"],
        ),
        local_schema_view=LocalSchemaView(db_id="toy"),
    )
    source_relations = [
        {
            "left": {"table": "alpha", "column": "beta_id"},
            "right": {"table": "beta", "column": "beta_id"},
            "canonical_key": "alpha.beta_id=beta.beta_id",
        }
    ]
    unchanged_variant = {
        "source_equality_relations": source_relations,
        "target_equality_relations": source_relations,
        "source_output_refs": [
            {"table": "beta", "column": "beta_label", "slot_index": 0}
        ],
        "target_output_refs": [
            {"table": "beta", "column": "beta_code", "slot_index": 0}
        ],
    }
    reroute_variant = {
        **unchanged_variant,
        "target_equality_relations": [
            {
                "left": {"table": "alpha", "column": "gamma_id"},
                "right": {"table": "gamma", "column": "gamma_id"},
                "canonical_key": "alpha.gamma_id=gamma.gamma_id",
            }
        ],
    }
    add_relation_variant = {
        **unchanged_variant,
        "source_equality_relations": [],
        "target_equality_relations": reroute_variant["target_equality_relations"],
    }

    assert not _variant_requires_relation_reroute(unchanged_variant, case_view)
    assert _variant_requires_relation_reroute(reroute_variant, case_view)
    assert _variant_requires_relation_reroute(add_relation_variant, case_view)


def test_reroute_dependency_coverage_requires_precise_table_binding() -> None:
    selected_projection = SimpleNamespace(
        arguments={
            "target_output_refs": [
                {"table": "requested_table", "column": "shared_id"}
            ]
        }
    )
    wrong_table_candidate = SimpleNamespace(
        arguments={
            "target_output_refs": [
                {"table": "other_table", "column": "shared_id"}
            ]
        }
    )
    exact_candidate = SimpleNamespace(
        arguments={
            "target_output_refs": [
                {"table": "requested_table", "column": "shared_id"}
            ]
        }
    )
    tableless_projection = SimpleNamespace(
        arguments={
            "target_output_refs": [
                {"column": "shared_id"}
            ]
        }
    )
    ambiguous_column_candidate = SimpleNamespace(
        arguments={
            "target_output_refs": [
                {"table": "left_table", "column": "shared_id"},
                {"table": "right_table", "column": "shared_id"},
            ]
        }
    )

    assert not _reroute_candidate_covers_projection_actions(
        wrong_table_candidate,
        [selected_projection],
    )
    assert _reroute_candidate_covers_projection_actions(
        exact_candidate,
        [selected_projection],
    )
    assert not _reroute_candidate_covers_projection_actions(
        ambiguous_column_candidate,
        [tableless_projection],
    )


def test_reroute_dependency_requires_missing_relation_for_projection_target() -> None:
    add_account_projection = SimpleNamespace(
        arguments={
            "target_output_refs": [
                {"table": "account", "column": "account_id"}
            ]
        }
    )
    add_district_projection = SimpleNamespace(
        arguments={
            "target_output_refs": [
                {"table": "district", "column": "district_id"}
            ]
        }
    )
    reroute_candidate = SimpleNamespace(
        arguments={
            "target_output_refs": [
                {"table": "account", "column": "account_id"},
                {"table": "district", "column": "district_id"},
            ],
            "target_relation_edges": [
                {
                    "left": {"table": "disp", "column": "account_id"},
                    "right": {"table": "account", "column": "account_id"},
                    "canonical_key": "account.account_id=disp.account_id",
                },
                {
                    "left": {"table": "account", "column": "district_id"},
                    "right": {"table": "district", "column": "district_id"},
                    "canonical_key": "account.district_id=district.district_id",
                },
            ],
        }
    )

    sql_with_account_route = (
        "SELECT client.client_id FROM client "
        "JOIN disp ON client.client_id = disp.client_id "
        "JOIN account ON disp.account_id = account.account_id "
        "JOIN district ON account.district_id = district.district_id"
    )
    sql_missing_account_route = (
        "SELECT client.client_id, district.name FROM disp "
        "JOIN client ON disp.client_id = client.client_id "
        "JOIN district ON client.district_id = district.district_id"
    )

    assert not _reroute_candidate_has_missing_relation_for_projection(
        reroute_candidate,
        [add_account_projection],
        sql_with_account_route,
    )
    assert _reroute_candidate_has_missing_relation_for_projection(
        reroute_candidate,
        [add_district_projection],
        sql_missing_account_route,
    )


def _compiler_runtime_case(sql: str) -> RuntimeCaseView:
    return RuntimeCaseView(
        db_id="toy",
        case_id="compiler-reroute",
        question="List the requested account and district values.",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql=sql,
            tables=["client", "disp", "account", "district"],
            columns=["client.client_id"],
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["client", "disp", "account", "district"],
            columns_by_table={
                "client": ["client_id", "district_id"],
                "disp": ["client_id", "account_id"],
                "account": ["account_id", "district_id"],
                "district": ["district_id", "name"],
            },
        ),
    )


def test_runtime_rewrite_guard_preserves_uneditable_anchor_tables_and_predicates() -> None:
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="guard-select-only",
        question="List student names for older students.",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql=(
                "SELECT a.name FROM student AS a "
                "JOIN school AS s ON a.school_id = s.id "
                "WHERE a.age > 10"
            ),
            tables=["student", "school"],
            columns=["a.name"],
        ),
        local_schema_view=LocalSchemaView(db_id="toy"),
    )
    action = Action(
        action_id="a1",
        source_group_id="g1",
        source_group_type=GroupType.PATTERN,
        primitive=ActionPrimitive.REPLACE_SELECT_SLOT,
        arguments={},
        rationale_short="replace output slot",
        risk=RiskLevel.LOW,
        allowed_edit_scope=[EditScope.SELECT],
    )

    guard = build_runtime_rewrite_guard(case_view=case_view, actions=[action])

    assert guard["allowed_edit_scope"] == ["SELECT"]
    assert "FROM" in guard["forbidden_scope"]
    assert "JOIN" in guard["forbidden_scope"]
    assert guard["must_preserve_tables"] == ["student", "school"]
    assert guard["must_preserve_predicates"] == ["a.age > 10"]


def test_runtime_rewrite_guard_completes_scope_from_action_primitive() -> None:
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="guard-drop-select-with-join-cleanup",
        question="List the remaining role-side value.",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql=(
                "SELECT a1.element, a2.element FROM connected AS c "
                "JOIN atom AS a1 ON c.atom_id = a1.atom_id "
                "JOIN atom AS a2 ON c.connected_atom_id = a2.atom_id "
                "WHERE c.bond_id = 'B1'"
            ),
            tables=["connected", "atom"],
            columns=["a1.element", "a2.element"],
        ),
        local_schema_view=LocalSchemaView(db_id="toy"),
    )
    action = Action(
        action_id="a1",
        source_group_id="g1",
        source_group_type=GroupType.PATTERN,
        primitive=ActionPrimitive.DROP_SELECT_SLOT,
        arguments={"required_edit_scopes": ["JOIN"]},
        rationale_short="drop redundant output side and its join dependency",
        risk=RiskLevel.MEDIUM,
        allowed_edit_scope=[EditScope.JOIN],
    )

    guard = build_runtime_rewrite_guard(case_view=case_view, actions=[action])

    assert guard["allowed_edit_scope"] == ["SELECT", "JOIN"]
    assert "SELECT" not in guard["forbidden_scope"]
    assert "JOIN" not in guard["forbidden_scope"]
    assert guard["must_preserve_predicates"] == ["c.bond_id = 'B1'"]


def test_runtime_rewrite_guard_does_not_preserve_predicates_when_where_edit_allowed() -> None:
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="guard-where",
        question="List student names for older students.",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT name FROM student WHERE age > 10",
            tables=["student"],
            columns=["name"],
        ),
        local_schema_view=LocalSchemaView(db_id="toy"),
    )
    action = Action(
        action_id="a1",
        source_group_id="g1",
        source_group_type=GroupType.PATTERN,
        primitive=ActionPrimitive.MOVE_CONDITION,
        arguments={},
        rationale_short="move predicate",
        risk=RiskLevel.MEDIUM,
        allowed_edit_scope=[EditScope.WHERE],
    )

    guard = build_runtime_rewrite_guard(case_view=case_view, actions=[action])

    assert guard["allowed_edit_scope"] == ["WHERE"]
    assert guard["must_preserve_predicates"] == []
    assert guard["must_preserve_tables"] == ["student"]
    assert guard["risk"] == "medium"


def test_runtime_rewrite_guard_preserves_where_and_having_independently() -> None:
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="guard-having",
        question="List classes with many older students.",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql=(
                "SELECT class_id, COUNT(*) FROM student "
                "WHERE age > 10 GROUP BY class_id HAVING COUNT(*) > 2"
            ),
            tables=["student"],
            columns=["class_id", "COUNT(*)"],
        ),
        local_schema_view=LocalSchemaView(db_id="toy"),
    )
    having_action = Action(
        action_id="a1",
        source_group_id="g1",
        source_group_type=GroupType.PATTERN,
        primitive=ActionPrimitive.MOVE_CONDITION,
        arguments={},
        rationale_short="edit having",
        risk=RiskLevel.MEDIUM,
        allowed_edit_scope=[EditScope.HAVING],
    )
    where_action = having_action.model_copy(update={"allowed_edit_scope": [EditScope.WHERE]})

    having_guard = build_runtime_rewrite_guard(case_view=case_view, actions=[having_action])
    where_guard = build_runtime_rewrite_guard(case_view=case_view, actions=[where_action])

    assert having_guard["must_preserve_predicates"] == ["age > 10"]
    assert where_guard["must_preserve_predicates"] == ["HAVING COUNT(*) > 2"]


def test_runtime_case_view_accepts_contract_shaped_candidate_context() -> None:
    access = SimpleNamespace(
        list_tables=lambda: ["student", "school"],
        get_columns=lambda table: {
            "student": ["id", "name", "school_id"],
            "school": ["id", "name"],
        }.get(table, []),
        get_pk_fk_edges=lambda tables=None: [],
        get_column_hints=lambda tables=None: [],
        get_column_note=lambda table, column: None,
    )

    case_view = build_runtime_case_view(
        db_id="toy",
        case_id="post-selection-candidates",
        question="List student names.",
        evidence="",
        pred_top1_sql="SELECT name FROM student",
        access=access,
        c0_candidate_sqls=[
            {
                "rank": 0,
                "sql": "SELECT name FROM student",
                "source": "base_selected_anchor",
            },
            {
                "rank": 1,
                "sql": "SELECT s.name FROM student AS s JOIN school AS sc ON s.school_id = sc.id",
                "source": "revision",
            },
        ],
        candidate_set_size=2,
    )

    summaries = case_view.candidate_set_summary.candidate_summaries
    assert case_view.candidate_set_summary.beam_present is True
    assert len(summaries) == 2
    assert summaries[0]["tables"] == ["student"]
    assert summaries[1]["tables"] == ["school", "student"]
    assert all("rank" not in str(item.get("tables", "")) for item in summaries)


def _compiler_group(
    group_id: str,
    *,
    max_actions: int = 3,
    compiler_deterministic: bool = True,
    selection_policy: str | None = None,
) -> GroupSummary:
    group = _singleton("compiler-seed")
    group.group_id = group_id
    group.group_type = GroupType.FAMILY
    group.trigger_contract.max_actions = max_actions
    group.trigger_contract.action_contract = {
        **group.trigger_contract.action_contract,
        "selection_policy": (
            selection_policy
            or ("deterministic_allowed" if compiler_deterministic else "llm_required")
        ),
        "compiler_deterministic": compiler_deterministic,
    }
    return group


def _compiler_candidate_sets(group_id: str) -> list[ActionCandidateSet]:
    target_account_ref = {
        "table": "account",
        "column": "account_id",
        "slot_index": 1,
        "expression": "account.account_id",
    }
    target_district_ref = {
        "table": "district",
        "column": "district_id",
        "slot_index": 2,
        "expression": "district.district_id",
    }
    target_edges = [
        {
            "left": {"table": "account", "column": "district_id"},
            "right": {"table": "district", "column": "district_id"},
            "canonical_key": "account.district_id=district.district_id",
        }
    ]
    return [
        ActionCandidateSet(
            primitive=ActionPrimitive.ADD_SELECT_SLOT,
            candidates=[
                ActionCandidate(
                    candidate_id="cand-add-district",
                    source_group_id=group_id,
                    source_group_type=GroupType.FAMILY,
                    provenance="test",
                    arguments={
                        "target_output_refs": [target_district_ref],
                        "target_columns": [
                            {
                                "target_table": "district",
                                "target_column": "district_id",
                            }
                        ],
                        "required_edit_scopes": ["SELECT"],
                    },
                )
            ],
        ),
        ActionCandidateSet(
            primitive=ActionPrimitive.REPLACE_SELECT_SLOT,
            candidates=[
                ActionCandidate(
                    candidate_id="cand-replace-account",
                    source_group_id=group_id,
                    source_group_type=GroupType.FAMILY,
                    provenance="test",
                    arguments={
                        "source_slot_indexes": [0],
                        "target_output_refs": [target_account_ref],
                        "target_columns": [
                            {
                                "target_table": "account",
                                "target_column": "account_id",
                            }
                        ],
                        "required_edit_scopes": ["SELECT"],
                    },
                )
            ],
        ),
        ActionCandidateSet(
            primitive=ActionPrimitive.REROUTE_FACT,
            candidates=[
                ActionCandidate(
                    candidate_id="cand-reroute-account-district",
                    source_group_id=group_id,
                    source_group_type=GroupType.FAMILY,
                    provenance="test",
                    arguments={
                        "target_output_refs": [
                            target_account_ref,
                            target_district_ref,
                        ],
                        "target_relation_edges": target_edges,
                        "required_edit_scopes": ["FROM", "JOIN", "SELECT"],
                    },
                )
            ],
        ),
    ]


def _patch_compiler_llm(monkeypatch, selected_candidate_ids: list[str]) -> None:
    def fake_call_llm(_prompt, expect_json=True):
        return {
            "actions": [
                {
                    "action_id": f"llm-select-{candidate_id}",
                    "selected_candidate_id": candidate_id,
                    "allowed_edit_scope": ["SELECT"],
                    "rationale_short": "select enumerated candidate",
                    "priority": 0.8,
                    "risk": "medium",
                }
                for candidate_id in selected_candidate_ids
            ],
            "schema_diagnostics": {"notes": "stubbed compiler llm"},
        }

    monkeypatch.setattr(
        "method.EEA.rulebook.common.llm.utils.call_llm",
        fake_call_llm,
    )


def test_action_compiler_does_not_append_relation_reroute_dependency_post_selection(monkeypatch) -> None:
    group_id = "grp-compiler-reroute"
    _patch_compiler_llm(
        monkeypatch,
        ["cand-add-district", "cand-replace-account"],
    )

    output = run_action_compiler(
        runtime_case_view=_compiler_runtime_case(
            "SELECT client.client_id FROM client "
            "JOIN disp ON client.client_id = disp.client_id "
            "JOIN district ON client.district_id = district.district_id"
        ),
        memory_objects=[
            _compiler_group(
                group_id,
                max_actions=3,
                selection_policy="deterministic_only",
            )
        ],
        precomputed_candidate_sets=_compiler_candidate_sets(group_id),
        precomputed_schema_diagnostics=LocalSchemaViewDiagnostics(),
    )

    primitives = [action.primitive for action in output.actions]

    assert primitives == [
        ActionPrimitive.ADD_SELECT_SLOT,
        ActionPrimitive.REPLACE_SELECT_SLOT,
    ]
    assert "bundle_cleanup_dependency_audit_only" in str(output.schema_diagnostics.notes)


def test_action_compiler_skips_reroute_when_projection_route_already_exists(monkeypatch) -> None:
    group_id = "grp-compiler-reroute"
    _patch_compiler_llm(monkeypatch, ["cand-add-district"])

    output = run_action_compiler(
        runtime_case_view=_compiler_runtime_case(
            "SELECT client.client_id FROM client "
            "JOIN disp ON client.client_id = disp.client_id "
            "JOIN account ON disp.account_id = account.account_id "
            "JOIN district ON account.district_id = district.district_id"
        ),
        memory_objects=[
            _compiler_group(
                group_id,
                max_actions=3,
                selection_policy="deterministic_only",
            )
        ],
        precomputed_candidate_sets=_compiler_candidate_sets(group_id),
        precomputed_schema_diagnostics=LocalSchemaViewDiagnostics(),
    )

    assert [action.primitive for action in output.actions] == [
        ActionPrimitive.ADD_SELECT_SLOT
    ]
    assert "bundle_cleanup_dependency_audit_only" in str(output.schema_diagnostics.notes)


def test_action_compiler_respects_group_capacity_before_auto_reroute(monkeypatch) -> None:
    group_id = "grp-compiler-reroute"
    _patch_compiler_llm(monkeypatch, ["cand-add-district"])

    output = run_action_compiler(
        runtime_case_view=_compiler_runtime_case(
            "SELECT client.client_id FROM client "
            "JOIN disp ON client.client_id = disp.client_id "
            "JOIN district ON client.district_id = district.district_id"
        ),
        memory_objects=[
            _compiler_group(
                group_id,
                max_actions=1,
                selection_policy="deterministic_only",
            )
        ],
        precomputed_candidate_sets=_compiler_candidate_sets(group_id),
        precomputed_schema_diagnostics=LocalSchemaViewDiagnostics(),
    )

    assert [action.primitive for action in output.actions] == [
        ActionPrimitive.ADD_SELECT_SLOT
    ]
    assert "bundle_cleanup_dependency_audit_only" in str(output.schema_diagnostics.notes)


def test_action_compiler_auto_reroute_requires_deterministic_contract(monkeypatch) -> None:
    group_id = "grp-compiler-reroute"
    _patch_compiler_llm(monkeypatch, ["cand-add-district"])

    output = run_action_compiler(
        runtime_case_view=_compiler_runtime_case(
            "SELECT client.client_id FROM client "
            "JOIN disp ON client.client_id = disp.client_id "
            "JOIN district ON client.district_id = district.district_id"
        ),
        memory_objects=[
            _compiler_group(
                group_id,
                max_actions=3,
                compiler_deterministic=False,
            )
        ],
        precomputed_candidate_sets=_compiler_candidate_sets(group_id),
        precomputed_schema_diagnostics=LocalSchemaViewDiagnostics(),
    )

    assert [action.primitive for action in output.actions] == [
        ActionPrimitive.ADD_SELECT_SLOT
    ]
    assert "bundle_cleanup_dependency_audit_only" in str(output.schema_diagnostics.notes)


def test_action_compiler_prompt_payload_contains_only_runtime_contract() -> None:
    import json

    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="253",
        question="Which endpoint should be returned?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
        ),
        candidate_set_summary=CandidateSetSummary(
            size=2,
            top1_hash="top1",
            beam_present=True,
            candidate_summaries=[
                {
                    "rank": 0,
                    "sql_hash": "top1",
                    "is_top1": True,
                    "tables": ["a", "b"],
                    "select_shape": {"output_arity": 2},
                },
                {
                    "rank": 1,
                    "sql_hash": "beam1",
                    "is_top1": False,
                    "tables": ["a"],
                    "select_shape": {"output_arity": 1},
                },
            ],
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id", "element"], "b": ["id"]},
        ),
    )

    candidate_sets, diag = enumerate_candidates(
        case_view=case_view,
        memory_objects=[family],
    )
    payloads = _action_compiler_prompt_payloads(
        runtime_case_view=case_view,
        memory_objects=[family],
        candidate_sets=candidate_sets,
        schema_diagnostics_pre=diag,
    )
    prompt_json = json.dumps(payloads, ensure_ascii=False, default=str)

    assert payloads["runtime_case_view"]["pred_manifestation"]["top1_sql"]
    assert "sql_role_graph" in payloads["runtime_case_view"]
    assert payloads["runtime_case_view"]["candidate_set_summary"]["candidate_summaries"]
    assert payloads["memory_objects"][0]["instantiation_program"]["synthesized_program"]["ops"]
    assert "trigger_match_summary" in payloads["memory_objects"][0]
    candidate_rows = [
        cand
        for candidate_set in payloads["candidate_sets"]
        for cand in candidate_set["candidates"]
    ]
    assert candidate_rows
    assert candidate_rows[0]["candidate_id"]
    assert candidate_rows[0]["compatibility"] in {"exact", "conflict"}
    assert candidate_rows[0]["binding_status"] in {"unique", "ambiguous"}
    assert candidate_rows[0]["candidate_contract_status"] in {"executable", "blocked"}
    assert "reject_reasons" in candidate_rows[0]
    assert "canonical_op_type" not in candidate_rows[0]["arguments"]
    assert "canonical_op_id" not in candidate_rows[0]["arguments"]
    assert candidate_rows[0]["arguments"]["canonical_contract"]["lowering_family"] == "select_drop"
    assert (
        payloads["memory_objects"][0]["instantiation_program"]["synthesized_program"]["program_envelope"]
    )
    program_ops = payloads["memory_objects"][0]["instantiation_program"]["synthesized_program"]["ops"]
    assert program_ops
    assert "op_type" not in program_ops[0]
    assert "locus" not in program_ops[0]
    assert "canonical_program_op" not in prompt_json
    assert "member_argument_variants" not in prompt_json
    assert "\"template\"" not in prompt_json
    assert "source_relation_edges" not in prompt_json
    assert "source_case_contract" not in prompt_json
    assert "trigger_signature" in prompt_json
    assert "repair_skeleton_prototype" not in prompt_json
    assert "program_coverage" not in prompt_json
    assert "program_type" not in prompt_json
    assert "core_ops" not in prompt_json
    assert "accessory_ops" not in prompt_json
    assert "synthesized_from_case_ids" not in prompt_json
    assert "supporting_case_ids" not in prompt_json
    assert "case_signal_view" not in prompt_json
    assert "case_signal_bundle" not in prompt_json


def test_action_compiler_lowers_case_extracted_bridge_to_insert_bridge() -> None:
    bridge_invariants = [
        "target_requires_additional_table",
        "target_join_path_expanded",
    ]
    family = build_family_from_groups(
        [
            _singleton("198", "JOIN_ADD_BRIDGE", locus="JOIN", invariants=bridge_invariants),
            _singleton("207", "JOIN_ADD_BRIDGE", locus="JOIN", invariants=bridge_invariants),
        ]
    )
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="208",
        question="How many atoms are in scope?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT bond.id FROM bond",
            tables=["bond"],
            columns=["bond.id"],
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["bond", "atom"],
            columns_by_table={"bond": ["id", "atom_id"], "atom": ["id"]},
            pk_fk_edges=[PkFkEdge(source="bond.atom_id", target="atom.id")],
        ),
    )

    candidate_sets, _diag = enumerate_candidates(
        case_view=case_view,
        memory_objects=[family],
    )
    bridge = next(cs for cs in candidate_sets if cs.primitive == ActionPrimitive.INSERT_BRIDGE)
    reroute = next(cs for cs in candidate_sets if cs.primitive == ActionPrimitive.REROUTE_FACT)

    assert bridge.candidates
    assert bridge.candidates[0].arguments["canonical_op_type"] == "JOIN_ADD_BRIDGE"
    assert "not required by synthesized canonical program" in reroute.empty_reason


def test_action_compiler_does_not_fallback_to_skeleton_without_canonical_program() -> None:
    legacy_group = _singleton("999")
    legacy_group.instantiation_program.synthesized_program = None
    legacy_group.instantiation_program.program_coverage = None
    legacy_group.instantiation_program.repair_program = []
    legacy_group.trigger_contract.action_contract = {}

    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="1000",
        question="Which endpoint should be returned?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id"]},
        ),
    )

    candidate_sets, _diag = enumerate_candidates(
        case_view=case_view,
        memory_objects=[legacy_group],
    )

    assert all(not candidate_set.candidates for candidate_set in candidate_sets)
    assert all(
        "missing_synthesized_canonical_program" in str(candidate_set.empty_reason)
        for candidate_set in candidate_sets
    )


def test_runtime_trigger_fails_closed_without_executable_action_contract() -> None:
    singleton = _singleton("999")
    singleton.trigger_contract.action_contract = {}
    library = LibraryStateV2(db_id="toy", singletons=[singleton])
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="1000",
        question="Which endpoint should be returned?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id"]},
        ),
    )

    result = trigger_memory_objects(library=library, case_view=case_view, db_id="toy")

    assert result.selected_groups == []
    assert any(
        "runtime_group_missing_executable_repair_contract" in audit.gate_reasons
        for audit in result.candidates
    )


def test_accumulate_singleton_attaches_canonical_ir_and_program() -> None:
    skeleton = _skeleton()
    error_instance = ErrorInstanceV2(
        db_id="toy",
        case_id="1",
        question_features=QuestionFeatures(),
        pred_sql_features=PredSqlFeatures(),
        deep_bias="extra output side",
        repair_goal="retain only the requested output side",
        repair_skeleton=skeleton,
        repair_program=[
            RepairProgramStep(
                step_id="s1",
                op="SELECT_DROP_SLOT",
                locus="SELECT",
                origin="case_extracted",
                extraction_source="llm_explicit",
                supporting_case_ids=["1"],
            )
        ],
        rewrite_hint_proto="Drop the extra selected side.",
    )
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="1",
        question="Which id is requested?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
        ),
        local_schema_view=LocalSchemaView(db_id="toy"),
    )
    audit = CaseAudit(
        db_id="toy",
        case_id="1",
        question="Which id is requested?",
        pred_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
        gold_sql="SELECT a.id FROM a JOIN b ON a.id = b.a_id",
        final_error_reason="extra output side",
        minimal_fix="drop b.id",
    )

    singleton = error_instance_to_singleton(
        error_instance,
        case_audit=audit,
        runtime_case_view=case_view,
    )

    assert singleton.formation_signals["canonical_repair_ir"]["program_ops"]
    assert singleton.formation_signals["canonical_repair_ir"]["core_ops"]
    assert singleton.instantiation_program.synthesized_program is not None
    assert singleton.instantiation_program.program_coverage is not None


def test_role_graph_extracts_alias_free_equality_relations() -> None:
    graph = RoleGraphNormalizer().normalize_sql(
        sql="SELECT a.id FROM atom AS a JOIN bond b ON a.molecule_id = b.molecule_id WHERE a.id = b.atom_id",
        source="target_sql",
    )

    keys = {item["canonical_key"] for item in graph["equality_relations"]}
    assert "atom.molecule_id=bond.molecule_id" in keys
    assert "atom.id=bond.atom_id" in keys


def test_repair_normalizer_records_target_relation_invariant_without_skeleton_fallback() -> None:
    error_instance = ErrorInstanceV2(
        db_id="toy",
        case_id="42",
        question_features=QuestionFeatures(),
        pred_sql_features=PredSqlFeatures(),
        deep_bias="missing relation scope",
        repair_goal="add the relation scope required by the validated fix",
        repair_skeleton=_skeleton(),
        repair_program=[
            RepairProgramStep(
                step_id="s1",
                op="JOIN_ADD_BRIDGE",
                locus="JOIN",
                origin="case_extracted",
                extraction_source="llm_explicit",
                supporting_case_ids=["42"],
            )
        ],
        rewrite_hint_proto="Add the required join relation.",
    )
    audit = CaseAudit(
        db_id="toy",
        case_id="42",
        question="How many atoms are in the molecule scope?",
        pred_sql="SELECT bond.id FROM bond",
        gold_sql="SELECT bond.id FROM bond JOIN atom ON atom.molecule_id = bond.molecule_id",
        final_error_reason="missing scope relation",
        minimal_fix="join atom on molecule_id",
    )

    ir = RepairProgramNormalizer().normalize_error_instance(
        error_instance=error_instance,
        case_audit=audit,
    )

    assert ir.core_ops[0]["op_type"] == "JOIN_ADD_BRIDGE"
    assert "target_added_relation_equality=atom.molecule_id=bond.molecule_id" in ir.target_invariants


def test_repair_normalizer_derives_output_op_from_sql_delta_not_skeleton() -> None:
    error_instance = ErrorInstanceV2(
        db_id="toy",
        case_id="43",
        question_features=QuestionFeatures(),
        pred_sql_features=PredSqlFeatures(),
        deep_bias="extra output side",
        repair_goal="drop extra side",
        repair_skeleton=_skeleton(),
        repair_program=[],
        rewrite_hint_proto="Drop the extra side.",
    )

    ir = RepairProgramNormalizer().normalize_error_instance(
        error_instance=error_instance,
        case_audit=CaseAudit(
            db_id="toy",
            case_id="43",
            question="Which id?",
            pred_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            gold_sql="SELECT a.id FROM a JOIN b ON a.id = b.a_id",
            final_error_reason="extra output",
            minimal_fix="drop b.id",
        ),
    )

    assert [op.op_type for op in ir.program_ops] == ["SELECT_DROP_SLOT"]
    assert ir.core_ops[0]["extraction_source"] == "sql_delta"
    assert "missing_repair_program" in ir.normalizer_warnings
    assert "no_canonical_ops" not in ir.normalizer_warnings


def test_repair_normalizer_does_not_create_op_when_no_step_and_no_sql_delta() -> None:
    error_instance = ErrorInstanceV2(
        db_id="toy",
        case_id="44",
        question_features=QuestionFeatures(),
        pred_sql_features=PredSqlFeatures(),
        deep_bias="unknown",
        repair_goal="no executable repair",
        repair_skeleton=_skeleton(),
        repair_program=[],
        rewrite_hint_proto="",
    )

    ir = RepairProgramNormalizer().normalize_error_instance(
        error_instance=error_instance,
        case_audit=CaseAudit(
            db_id="toy",
            case_id="44",
            question="Which id?",
            pred_sql="SELECT a.id FROM a",
            gold_sql="SELECT a.id FROM a",
            final_error_reason="unknown",
            minimal_fix="none",
        ),
    )

    assert ir.program_ops == []
    assert "missing_repair_program" in ir.normalizer_warnings
    assert "no_canonical_ops" in ir.normalizer_warnings


def test_repair_normalizer_records_target_only_predicate_dependency_policy() -> None:
    error_instance = ErrorInstanceV2(
        db_id="toy",
        case_id="45",
        question_features=QuestionFeatures(),
        pred_sql_features=PredSqlFeatures(),
        deep_bias="missing output and target predicate",
        repair_goal="replace selected school output",
        repair_skeleton=_skeleton(),
        repair_program=[
            RepairProgramStep(
                step_id="s1",
                op="SELECT_REPLACE_SLOT",
                locus="SELECT",
                origin="case_extracted",
                extraction_source="llm_explicit",
                supporting_case_ids=["45"],
            )
        ],
        rewrite_hint_proto="Replace the selected output.",
    )

    ir = RepairProgramNormalizer().normalize_error_instance(
        error_instance=error_instance,
        case_audit=CaseAudit(
            db_id="toy",
            case_id="45",
            question="Which school has the lowest score?",
            pred_sql="SELECT s.name FROM score s ORDER BY s.avg_score ASC LIMIT 1",
            gold_sql=(
                "SELECT s.school FROM score AS s "
                "WHERE s.avg_score IS NOT NULL "
                "ORDER BY s.avg_score ASC LIMIT 1"
            ),
            final_error_reason="missing target predicate",
            minimal_fix="add non-null score predicate",
        ),
    )

    policies = ir.program_ops[0].arguments["accessory_policies"]

    assert any(policy["op"] == "WHERE_ADD_CONDITION" for policy in policies)
    where_policy = next(policy for policy in policies if policy["op"] == "WHERE_ADD_CONDITION")
    assert where_policy["policy"] == "target_only_predicate_constraint"
    assert where_policy["target_predicates"][0]["refs"][0]["column"] == "avg_score"


def test_repair_normalizer_records_target_ranking_dependency_policy() -> None:
    error_instance = ErrorInstanceV2(
        db_id="toy",
        case_id="46",
        question_features=QuestionFeatures(),
        pred_sql_features=PredSqlFeatures(),
        deep_bias="missing ranking contract",
        repair_goal="replace selected output and keep target ranking",
        repair_skeleton=_skeleton(),
        repair_program=[
            RepairProgramStep(
                step_id="s1",
                op="SELECT_REPLACE_SLOT",
                locus="SELECT",
                origin="case_extracted",
                extraction_source="llm_explicit",
                supporting_case_ids=["46"],
            )
        ],
        rewrite_hint_proto="Replace the selected output.",
    )

    ir = RepairProgramNormalizer().normalize_error_instance(
        error_instance=error_instance,
        case_audit=CaseAudit(
            db_id="toy",
            case_id="46",
            question="Which school is farthest south?",
            pred_sql=(
                "SELECT sc.City FROM schools sc "
                "WHERE sc.Latitude = (SELECT MIN(Latitude) FROM schools)"
            ),
            gold_sql=(
                "SELECT sc.City FROM schools AS sc "
                "ORDER BY sc.Latitude ASC LIMIT 1"
            ),
            final_error_reason="missing target ranking",
            minimal_fix="order by latitude and limit one row",
        ),
    )

    policies = ir.program_ops[0].arguments["accessory_policies"]

    assert any(policy["op"] == "ORDER_BY_APPLY" for policy in policies)
    assert any(policy["op"] == "LIMIT_APPLY" for policy in policies)
    assert any(policy["op"] == "WHERE_DROP_RANKING_PREDICATE" for policy in policies)
    order_policy = next(policy for policy in policies if policy["op"] == "ORDER_BY_APPLY")
    limit_policy = next(policy for policy in policies if policy["op"] == "LIMIT_APPLY")
    drop_policy = next(policy for policy in policies if policy["op"] == "WHERE_DROP_RANKING_PREDICATE")
    assert order_policy["target_order_by"][0]["refs"][0]["table"] == "schools"
    assert order_policy["target_order_by"][0]["refs"][0]["column"] == "Latitude"
    assert limit_policy["target_limit"] == "1"
    assert drop_policy["source_ranking_predicates"][0]["aggregate"] == "MIN"


def test_repair_normalizer_prefers_validated_sql_over_gold_sql_when_available() -> None:
    error_instance = ErrorInstanceV2(
        db_id="toy",
        case_id="47",
        question_features=QuestionFeatures(),
        pred_sql_features=PredSqlFeatures(),
        deep_bias="validated SQL omitted a target output",
        repair_goal="add the missing target output",
        repair_skeleton=_skeleton(),
        repair_program=[
            RepairProgramStep(
                step_id="s1",
                op="SELECT_REPLACE_SLOT",
                locus="SELECT",
                origin="case_extracted",
                extraction_source="llm_explicit",
                supporting_case_ids=["47"],
            )
        ],
        rewrite_hint_proto="Add the missing output.",
    )

    ir = RepairProgramNormalizer().normalize_error_instance(
        error_instance=error_instance,
        case_audit=CaseAudit(
            db_id="toy",
            case_id="47",
            question="Which ids?",
            pred_sql="SELECT a.id FROM a",
            gold_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            validated_sql="SELECT a.id FROM a",
            final_error_reason="missing target output",
            minimal_fix="add b.id",
        ),
    )

    target_refs = ir.program_ops[0].arguments["target_output_refs"]

    assert [ref["expression"] for ref in target_refs] == ["a.id"]


def test_repair_normalizer_falls_back_to_gold_sql_without_validated_sql() -> None:
    error_instance = ErrorInstanceV2(
        db_id="toy",
        case_id="47",
        question_features=QuestionFeatures(),
        pred_sql_features=PredSqlFeatures(),
        deep_bias="validated SQL unavailable",
        repair_goal="add the missing target output",
        repair_skeleton=_skeleton(),
        repair_program=[
            RepairProgramStep(
                step_id="s1",
                op="SELECT_REPLACE_SLOT",
                locus="SELECT",
                origin="case_extracted",
                extraction_source="llm_explicit",
                supporting_case_ids=["47"],
            )
        ],
        rewrite_hint_proto="Add the missing output.",
    )

    ir = RepairProgramNormalizer().normalize_error_instance(
        error_instance=error_instance,
        case_audit=CaseAudit(
            db_id="toy",
            case_id="47",
            question="Which ids?",
            pred_sql="SELECT a.id FROM a",
            gold_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            validated_sql="",
            final_error_reason="missing target output",
            minimal_fix="add b.id",
        ),
    )

    target_refs = ir.program_ops[0].arguments["target_output_refs"]

    assert [ref["expression"] for ref in target_refs] == ["a.id", "b.id"]


def test_extractor_compact_audit_omits_full_gold_and_execution_payload() -> None:
    audit = CaseAudit(
        db_id="toy",
        case_id="47",
        question="Which ids?",
        pred_sql="SELECT a.id FROM a",
        gold_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
        candidate_fix_sql="SELECT a.id FROM a",
        minimal_patch_ops=[{"clause": "SELECT", "edit_kind": "replace"}],
        secondary_differences=["join path also differs"],
        validated_sql="SELECT a.id FROM a",
        final_error_reason="missing target output",
        minimal_fix="add b.id",
        execution_result="row mismatch detail",
    )

    payload = _compact_case_audit_for_extractor(audit)

    assert payload["candidate_fix_sql"] == "SELECT a.id FROM a"
    assert payload["minimal_patch_ops"] == [{"clause": "SELECT", "edit_kind": "replace"}]
    assert payload["secondary_differences"] == ["join path also differs"]
    assert payload["validated_sql"] == "SELECT a.id FROM a"
    assert payload["minimal_fix"] == "add b.id"
    assert "gold_sql" not in payload
    assert "execution_result" not in payload
    assert "execution_comparison" not in payload


def test_promotion_preserves_no_lowering_rule_failure_reason() -> None:
    reason = _not_actionable_reason(
        plan={"reason": "ready"},
        compiler_output=object(),
        actions=[],
        empty_reasons=[
            {
                "primitive": "REROUTE_FACT",
                "reason": "no_lowering_rule: REROUTE_FACT requires fact-route binding",
            }
        ],
    )

    assert reason.startswith("no_lowering_rule")


def test_replay_metrics_count_only_pure_llm_rows_as_llm_selected() -> None:
    from method.EEA.rulebook.common.learning.promotion import _metrics_from_rows

    metrics = _metrics_from_rows(
        [
            {
                "compile_pass": True,
                "action_count": 2,
                "selection_origins": ["llm_selected", "deterministic_unique"],
                "improved": True,
                "regressed": False,
                "eligible_for_formal_promotion": True,
                "replay_mode": "leave_one_out_replay",
            },
            {
                "compile_pass": True,
                "action_count": 1,
                "selection_origins": ["llm_selected"],
                "improved": True,
                "regressed": False,
                "eligible_for_formal_promotion": True,
                "replay_mode": "leave_one_out_replay",
            },
            {
                "compile_pass": True,
                "action_count": 1,
                "selection_origins": ["deterministic_unique"],
                "improved": True,
                "regressed": False,
                "eligible_for_formal_promotion": True,
                "replay_mode": "leave_one_out_replay",
            },
        ],
        version=1,
    )

    assert metrics.replay_improvement_total == 1.0
    assert metrics.replay_improvement_llm_selected == 1 / 3
    assert metrics.replay_improvement_deterministic_unique == 1 / 3
    assert metrics.llm_selected_rate == 1 / 3
    assert metrics.fallback_selected_rate == 1 / 3


def test_unresolved_axes_block_formal_promotion_but_allow_runtime_pattern_audit() -> None:
    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    program = family.instantiation_program.synthesized_program
    assert program is not None
    family.instantiation_program = family.instantiation_program.model_copy(
        update={
            "synthesized_program": program.model_copy(
                update={"unresolved_variation_axes": ["slot_signature"]}
            )
        }
    )
    metrics = ReplayMetrics(
        version=1,
        compile_coverage=1.0,
        mean_action_count=1.0,
        replay_improvement=1.0,
        replay_regression=0.0,
        leave_one_out_done=True,
        sample_size=2,
    )
    result = PromotionTestResult(
        group_id=family.group_id,
        eligible=True,
        replay_metrics=metrics,
        shared_minimal_repair_interface=True,
        shared_instantiation_program=True,
    )

    promoted = apply_promotion_decision(family, result)

    assert promoted.group_type == GroupType.PATTERN
    assert promoted.runtime_usable is True
    assert promoted.lifecycle.promotion_state == "runtime_visible_replay_audit_only"


def test_formal_replay_blocker_keeps_pattern_runtime_visible_for_audit() -> None:
    family = build_family_from_groups(
        [_singleton("206"), _singleton("249"), _singleton("253")]
    )
    runtime_metrics = ReplayMetrics(
        version=1,
        compile_coverage=1.0,
        mean_action_count=1.0,
        replay_improvement=1.0,
        replay_regression=0.0,
        leave_one_out_done=True,
        sample_size=3,
        replay_improvement_llm_selected=1.0,
        replay_improvement_total=1.0,
    )
    formal_metrics = ReplayMetrics(
        version=1,
        compile_coverage=1.0,
        mean_action_count=1.0,
        replay_improvement=1.0,
        replay_regression=0.0,
        leave_one_out_done=True,
        sample_size=3,
        replay_improvement_llm_selected=0.0,
        replay_improvement_deterministic_unique=0.0,
        replay_improvement_total=1.0,
    )
    result = PromotionTestResult(
        group_id=family.group_id,
        eligible=True,
        replay_metrics=runtime_metrics,
        formal_replay_metrics=formal_metrics,
        formal_promotion_blocker="formal_replay_not_llm_selected",
        shared_minimal_repair_interface=True,
        shared_instantiation_program=True,
    )

    promoted = apply_promotion_decision(family, result)

    assert promoted.group_type == GroupType.PATTERN
    assert promoted.runtime_usable is True
    assert promoted.lifecycle.promotion_state == "runtime_visible_replay_audit_only"


def test_support2_runtime_can_use_full_group_while_formal_uses_cross(monkeypatch) -> None:
    from method.EEA.rulebook.common.learning import promotion as promotion_module

    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    source_singletons = {case_id: _singleton(case_id) for case_id in family.case_ids}
    static_coverage = ProgramCoverage(
        total_cases=2,
        covered_case_ids=list(family.case_ids),
        compile_coverage=1.0,
        static_program_coverage=0.75,
        runtime_binding_coverage=0.0,
        member_candidate_coverage=0.0,
        mean_action_count=1.0,
        core_op_coverage=1.0,
    )

    monkeypatch.setattr(
        promotion_module,
        "_member_case_views_for_group",
        lambda **_: {},
    )
    monkeypatch.setattr(promotion_module, "_contract_program_issues", lambda *_, **__: [])
    monkeypatch.setattr(
        promotion_module,
        "validate_group_program_coverage",
        lambda *_, **__: static_coverage,
    )

    def fake_replay_one_holdout(**kwargs):
        holdout = str(kwargs["holdout_case_id"])
        replay_mode = str(kwargs["replay_mode"])
        if replay_mode == "full_group_member_replay":
            return {
                "holdout_case_id": holdout,
                "replay_mode": replay_mode,
                "training_case_ids": [str(case_id) for case_id in family.case_ids],
                "holdout_in_training": True,
                "eligible_for_formal_promotion": False,
                "compile_pass": True,
                "action_count": 1,
                "selection_origin": "llm_selected",
                "selection_origins": ["llm_selected"],
                "improved": True,
                "regressed": False,
            }
        return {
            "holdout_case_id": holdout,
            "replay_mode": replay_mode,
            "training_case_ids": [
                str(case_id) for case_id in family.case_ids if str(case_id) != holdout
            ],
            "holdout_in_training": False,
            "eligible_for_formal_promotion": True,
            "compile_pass": False,
            "action_count": 0,
            "selection_origin": "none",
            "selection_origins": [],
            "improved": False,
            "regressed": False,
            "reason": "loo_compile_failed",
        }

    monkeypatch.setattr(promotion_module, "_replay_one_holdout", fake_replay_one_holdout)

    result = promotion_module.run_promotion_test(
        group=family,
        source_singletons_by_case_id=source_singletons,
        case_loader=lambda _: {},
        db_path="/tmp/unused.sqlite",
    )
    promoted = apply_promotion_decision(family, result)

    assert result.runtime_family_evidence_mode == "full_group_smoke_only"
    assert result.replay_metrics.compile_coverage == 1.0
    assert result.program_coverage is not None
    assert result.program_coverage.compile_coverage == 1.0
    assert result.program_coverage.runtime_binding_coverage == 1.0
    assert result.program_coverage.member_candidate_coverage == 1.0
    assert result.program_coverage.static_program_coverage == 0.75
    assert result.replay_metrics.replay_improvement == 1.0
    assert result.formal_replay_metrics.compile_coverage == 0.0
    assert result.formal_replay_modes == ["pairwise_cross_replay"]
    assert result.support_protocol_passed is False
    assert "formal_compile_coverage_below_pattern_threshold" in (
        result.formal_promotion_blocker or ""
    )
    assert promoted.group_type == GroupType.PATTERN
    assert promoted.runtime_usable is True
    assert promoted.lifecycle.promotion_state == "runtime_visible_replay_audit_only"


def test_replay_audit_visible_pattern_does_not_supersede_singletons() -> None:
    singleton_a = _singleton("206")
    singleton_b = _singleton("249")
    family = build_family_from_groups([singleton_a, singleton_b])
    result = PromotionTestResult(
        group_id=family.group_id,
        eligible=False,
        reason="compile_coverage_below_runtime_threshold",
        replay_metrics=ReplayMetrics(
            version=1,
            compile_coverage=0.0,
            mean_action_count=0.0,
            replay_improvement=0.0,
            replay_regression=0.0,
            sample_size=2,
        ),
        shared_minimal_repair_interface=True,
        shared_instantiation_program=True,
    )
    promoted = apply_promotion_decision(family, result)
    library = LibraryStateV2(db_id="toy", singletons=[singleton_a, singleton_b])

    integrate_promoted_groups(library, [promoted])

    assert [group.group_id for group in library.patterns] == [
        promoted.group_id
    ]
    assert library.patterns[0].runtime_usable is True
    assert library.patterns[0].lifecycle.promotion_state == "runtime_visible_replay_audit_only"
    materialize_library_runtime_contracts(library)
    assert library.patterns[0].runtime_usable is True
    assert [group.status for group in library.singletons] == [
        GroupStatus.ACTIVE,
        GroupStatus.ACTIVE,
    ]


def test_same_root_transform_selection_requires_shared_subset(monkeypatch) -> None:
    from method.EEA.rulebook.common.runtime import runtime as runtime_module

    groups = [_singleton("206"), _singleton("249"), _singleton("253")]
    for group in groups:
        group.group_type = GroupType.PATTERN
    audits = [
        TriggerCandidateAudit(
            group_id=group.group_id,
            group_type=group.group_type,
            gate_passed=True,
            final_score=float(index),
        )
        for index, group in enumerate(groups)
    ]

    key_map = {
        groups[0].group_id: {("shared",), ("left",)},
        groups[1].group_id: {("shared",), ("right",)},
        groups[2].group_id: {("other",)},
    }
    monkeypatch.setattr(
        runtime_module,
        "_current_transform_keys_for_group",
        lambda group, _case_view: (key_map[group.group_id], "ok"),
    )
    audit = {}

    selected, reason = _select_groups_with_shared_current_transform(
        list(zip(groups, audits)),
        max_selected=3,
        case_view=None,
        audit=audit,
    )

    assert reason == ""
    assert [group.group_id for group in selected] == [groups[1].group_id, groups[0].group_id]
    assert audit["resolution"] == "max_shared_current_transform_subset"
    assert audit["dropped_transform_group_ids"] == [groups[2].group_id]

    monkeypatch.setattr(
        runtime_module,
        "_current_transform_keys_for_group",
        lambda group, _case_view: ({(group.group_id,)}, "ok"),
    )
    audit = {}
    selected, reason = _select_groups_with_shared_current_transform(
        list(zip(groups, audits)),
        max_selected=3,
        case_view=None,
        audit=audit,
    )

    assert selected == []
    assert reason == "ambiguous_current_transform"
    assert audit["resolution"] == "same_root_distinct_current_transforms"


def test_replay_one_holdout_derives_holdout_in_training_from_memory() -> None:
    singleton = _singleton("206")
    row = _replay_one_holdout(
        group=singleton,
        source_singletons_by_case_id={"206": singleton},
        holdout_case_id="206",
        case_loader=lambda _: None,
        db_path="/tmp/unused.sqlite",
        database_dir=None,
        row_sample_limit=10,
        memory_override=singleton,
        memory_kind_override="pairwise_cross_replay",
        replay_mode="pairwise_cross_replay",
        training_case_ids=["249"],
        holdout_in_training=False,
        eligible_for_formal_promotion=True,
    )

    assert row["holdout_in_training"] is True
    assert row["eligible_for_formal_promotion"] is False
    assert row["protocol_violation"] == "holdout_present_in_training_memory"


def test_apply_promotion_rechecks_formal_support_shape() -> None:
    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    family.trigger_contract = family.trigger_contract.model_copy(
        update={
            "action_contract": {
                **family.trigger_contract.action_contract,
                "selection_policy": "deterministic_only",
                "compiler_deterministic": True,
            }
        }
    )
    metrics = ReplayMetrics(
        version=1,
        compile_coverage=1.0,
        mean_action_count=1.0,
        replay_improvement=1.0,
        replay_regression=0.0,
        leave_one_out_done=True,
        sample_size=2,
        replay_improvement_llm_selected=0.0,
        replay_improvement_deterministic_unique=1.0,
        replay_improvement_total=1.0,
    )
    result = PromotionTestResult(
        group_id=family.group_id,
        eligible=True,
        replay_metrics=metrics,
        formal_replay_metrics=metrics,
        support_protocol_passed=True,
        formal_replay_modes=["leave_one_out_replay"],
        formal_eligible_replay_rows=2,
        shared_minimal_repair_interface=True,
        shared_instantiation_program=True,
    )

    promoted = apply_promotion_decision(family, result)

    assert promoted.group_type == GroupType.PATTERN
    assert promoted.runtime_usable is True
    assert promoted.lifecycle.promotion_state == "runtime_visible_replay_audit_only"


def test_deterministic_allowed_metrics_do_not_promote_formal_pattern() -> None:
    family = build_family_from_groups(
        [_singleton("206"), _singleton("249"), _singleton("253")]
    )
    family.trigger_contract = family.trigger_contract.model_copy(
        update={
            "action_contract": {
                **family.trigger_contract.action_contract,
                "selection_policy": "deterministic_allowed",
                "compiler_deterministic": True,
            }
        }
    )
    metrics = ReplayMetrics(
        version=1,
        compile_coverage=1.0,
        mean_action_count=1.0,
        replay_improvement=1.0,
        replay_regression=0.0,
        leave_one_out_done=True,
        sample_size=3,
        replay_improvement_llm_selected=0.0,
        replay_improvement_deterministic_unique=1.0,
        replay_improvement_total=1.0,
    )
    result = PromotionTestResult(
        group_id=family.group_id,
        eligible=True,
        replay_metrics=metrics,
        formal_replay_metrics=metrics,
        support_protocol_passed=True,
        formal_replay_modes=["leave_one_out_replay"],
        formal_eligible_replay_rows=3,
        shared_minimal_repair_interface=True,
        shared_instantiation_program=True,
    )

    promoted = apply_promotion_decision(family, result)

    assert promoted.group_type == GroupType.PATTERN
    assert promoted.runtime_usable is True
    assert promoted.lifecycle.promotion_state == "runtime_visible_replay_audit_only"


def test_apply_promotion_requires_formal_support_protocol_for_pattern() -> None:
    family = build_family_from_groups(
        [_singleton("206"), _singleton("249"), _singleton("253")]
    )
    metrics = ReplayMetrics(
        version=1,
        compile_coverage=1.0,
        mean_action_count=1.0,
        replay_improvement=1.0,
        replay_regression=0.0,
        leave_one_out_done=True,
        sample_size=3,
        replay_improvement_llm_selected=1.0,
        replay_improvement_total=1.0,
    )
    result = PromotionTestResult(
        group_id=family.group_id,
        eligible=True,
        replay_metrics=metrics,
        formal_replay_metrics=metrics,
        shared_minimal_repair_interface=True,
        shared_instantiation_program=True,
    )

    promoted = apply_promotion_decision(family, result)

    assert promoted.group_type == GroupType.PATTERN
    assert promoted.runtime_usable is True
    assert promoted.lifecycle.promotion_state == "runtime_visible_replay_audit_only"


def test_deterministic_only_metrics_can_promote_formal_pattern() -> None:
    family = build_family_from_groups(
        [_singleton("206"), _singleton("249"), _singleton("253")]
    )
    family.trigger_contract = family.trigger_contract.model_copy(
        update={
            "action_contract": {
                **family.trigger_contract.action_contract,
                "selection_policy": "deterministic_only",
                "compiler_deterministic": True,
            }
        }
    )
    metrics = ReplayMetrics(
        version=1,
        compile_coverage=1.0,
        mean_action_count=1.0,
        replay_improvement=1.0,
        replay_regression=0.0,
        leave_one_out_done=True,
        sample_size=3,
        replay_improvement_llm_selected=0.0,
        replay_improvement_deterministic_unique=1.0,
        replay_improvement_total=1.0,
    )
    result = PromotionTestResult(
        group_id=family.group_id,
        eligible=True,
        replay_metrics=metrics,
        formal_replay_metrics=metrics,
        support_protocol_passed=True,
        formal_replay_modes=["leave_one_out_replay"],
        formal_eligible_replay_rows=3,
        shared_minimal_repair_interface=True,
        shared_instantiation_program=True,
    )

    promoted = apply_promotion_decision(family, result)

    assert promoted.group_type == GroupType.PATTERN
    assert promoted.lifecycle.promotion_state == "promoted_pattern_replay_gated"


def test_runtime_action_prompt_strips_source_case_evidence() -> None:
    payload = _runtime_actions_prompt_payload(
        [
            {
                "action_id": "a1",
                "source_group_id": "g1",
                "source_group_type": "family",
                "primitive": "DROP_SELECT_SLOT",
                "selected_candidate_id": "c1",
                "arguments": {
                    "from_expr": "b.id",
                    "from_exprs": ["b.id"],
                    "canonical_op_type": "SELECT_DROP_SLOT",
                    "canonical_refs": [{"table": "source_case"}],
                    "canonical_contract": {
                        "program_id": "p1",
                        "op_id": "op1",
                        "lowering_family": "select_drop",
                        "canonical_refs": [{"table": "source_case"}],
                    },
                    "repair_program": [
                        {
                            "step_id": "s1",
                            "op": "SELECT_DROP_SLOT",
                            "locus": "SELECT",
                            "source_evidence": ["source case wording"],
                            "supporting_case_ids": ["206"],
                            "arguments": {
                                "canonical_op_type": "SELECT_DROP_SLOT",
                                "canonical_refs": [{"table": "source_case"}],
                                "canonical_program_id": "p1",
                            },
                        }
                    ],
                },
            }
        ]
    )
    rendered = str(payload)

    assert "source_evidence" not in rendered
    assert "supporting_case_ids" not in rendered
    assert "canonical_refs" not in rendered
    assert "canonical_op_type" not in rendered
    assert payload[0]["arguments"]["repair_program"][0]["arguments"]["canonical_program_id"] == "p1"


def test_runtime_action_prompt_keeps_dependency_repair_program_steps() -> None:
    payload = _runtime_actions_prompt_payload(
        [
            {
                "action_id": "a1",
                "source_group_id": "g1",
                "source_group_type": "family",
                "primitive": "DROP_SELECT_SLOT",
                "selected_candidate_id": "c1",
                "arguments": {
                    "from_expr": "b.id",
                    "from_exprs": ["b.id"],
                    "repair_program": [
                        {
                            "step_id": "s1",
                            "op": "SELECT_DROP_SLOT",
                            "locus": "SELECT",
                            "is_dependency": False,
                            "required": True,
                            "arguments": {"canonical_program_id": "p1"},
                        },
                        {
                            "step_id": "s1_accessory",
                            "op": "SELECT_ENFORCE_DISTINCT",
                            "locus": "SELECT",
                            "is_dependency": True,
                            "required": False,
                            "arguments": {"accessory_policy": "conditional_target_distinct"},
                        },
                    ],
                },
            }
        ]
    )

    steps = payload[0]["arguments"]["repair_program"]

    assert [step["op"] for step in steps] == [
        "SELECT_DROP_SLOT",
        "SELECT_ENFORCE_DISTINCT",
    ]
    assert steps[1]["is_dependency"] is True


def test_runtime_action_prompt_omits_projection_refs_without_select_scope() -> None:
    payload = _runtime_actions_prompt_payload(
        [
            {
                "action_id": "a1",
                "source_group_id": "g1",
                "source_group_type": "family",
                "primitive": "INSERT_BRIDGE",
                "selected_candidate_id": "c1",
                "allowed_edit_scope": ["JOIN"],
                "arguments": {
                    "bridge_table": "bridge",
                    "target_output_refs": [{"table": "target", "column": "name"}],
                    "source_relation_edges": [{"left": "old.id", "right": "b.id"}],
                    "target_relation_edges": [{"left": "a.id", "right": "b.id"}],
                    "canonical_contract": {
                        "program_id": "p1",
                        "op_id": "op1",
                        "lowering_family": "join_bridge",
                        "target_output_refs": [{"table": "target", "column": "name"}],
                        "target_relation_edges": [{"left": "a.id", "right": "b.id"}],
                    },
                    "repair_program": [
                        {
                            "step_id": "s1",
                            "op": "JOIN_ADD_BRIDGE",
                            "locus": "JOIN",
                            "is_dependency": False,
                        }
                    ],
                },
            }
        ]
    )

    args = payload[0]["arguments"]

    assert "target_output_refs" not in args
    assert "source_relation_edges" not in args
    assert "target_output_refs" not in args["canonical_contract"]
    assert args["target_relation_edges"]


def test_memory_schema_tables_recurse_into_action_contract() -> None:
    payload = {
        "action_contract": {
            "repair_program": [
                {
                    "op": "JOIN_ADD_BRIDGE",
                    "arguments": {
                        "canonical_refs": [
                            {
                                "source": "target_sql",
                                "sql_role": "output_slot",
                                "table": "target_table",
                                "column": "target_col",
                            }
                        ],
                        "source_output_refs": [
                            {
                                "source": "pred_sql",
                                "sql_role": "output_slot",
                                "table": "source_noise",
                                "column": "source_col",
                            }
                        ],
                        "source_equality_relations": [
                            {
                                "left": {"table": "source_left", "column": "id"},
                                "right": {"table": "source_right", "column": "id"},
                            }
                        ],
                        "shared_arguments": {
                            "target_equality_relations": [
                                {
                                    "left": {"table": "bridge_table", "column": "id"},
                                    "right": {"table": "source_table", "column": "id"},
                                }
                            ]
                        },
                    },
                }
            ]
        }
    }

    assert _memory_schema_tables_from_value(payload) == [
        "target_table",
        "bridge_table",
        "source_table",
    ]


def test_rewrite_contract_payload_binds_select_drop_and_join_dependency() -> None:
    sql = (
        "SELECT DISTINCT a1.element, a2.element FROM bond b "
        "JOIN connected c ON b.bond_id = c.bond_id "
        "JOIN atom a1 ON c.atom_id = a1.atom_id "
        "JOIN atom a2 ON c.atom_id2 = a2.atom_id "
        "WHERE b.bond_type = '#'"
    )
    contract = _rewrite_contract_prompt_payload(
        actions=[
            {
                "action_id": "a1",
                "primitive": "DROP_SELECT_SLOT",
                "allowed_edit_scope": ["SELECT", "JOIN"],
                "selected_candidate_id": "c1",
                "arguments": {
                    "from_exprs": ["a2.element"],
                    "drop_count": 1,
                    "repair_program": [
                        {
                            "step_id": "dep1",
                            "op": "JOIN_DROP_TABLE",
                            "required": True,
                            "is_dependency": True,
                        }
                    ],
                },
            }
        ],
        current_sql=sql,
        natural_language_hint="Remove the extra output side.",
    )

    assert contract["schema_version"] == "rewrite-contract-v1"
    assert contract["allowed_scopes"] == ["JOIN", "SELECT"]
    assert contract["primary_edits"][0]["bound_expressions"] == ["a2.element"]
    join_blocks = contract["dependency_edits"][0]["bound_join_blocks"]
    assert join_blocks[0]["table"] == "atom"
    assert join_blocks[0]["alias"] == "a2"
    assert join_blocks[0]["sql"] == "JOIN atom a2 ON c.atom_id2 = a2.atom_id"
    assert join_blocks[0]["external_reference_found"] is False
    absence_texts = {row["text"] for row in contract["required_absence_checks"]}
    assert "a2.element" in absence_texts
    assert "JOIN atom a2 ON c.atom_id2 = a2.atom_id" in absence_texts


def test_rewrite_contract_absence_checks_fail_closed_without_sql_patch() -> None:
    original_sql = "SELECT a1.element, a2.element FROM atom a1 JOIN atom a2 ON a1.id = a2.id"
    rewrite_sql, traces, notes = _enforce_rewrite_contract_absence_checks(
        original_sql=original_sql,
        rewrite_sql=original_sql,
        rewrite_contract={
            "required_absence_checks": [
                {"action_id": "a1", "text": "a2.element", "scope": "SELECT"}
            ]
        },
        traces=[
            {
                "action_id": "a1",
                "realized": True,
                "edits": [{"location": "SELECT", "edit_kind": "remove"}],
            }
        ],
        contract_steps_applied=[],
    )

    assert rewrite_sql == original_sql
    assert traces[0]["realized"] is False
    assert notes == ["rewrite_contract_absence_failed:a1:a2.element"]


def test_rewrite_contract_dependency_applies_distinct_only_when_explicit() -> None:
    actions = [
        {
            "primitive": "DROP_SELECT_SLOT",
            "arguments": {
                "from_exprs": ["b.element"],
                "repair_program": [
                    {
                        "op": "SELECT_ENFORCE_DISTINCT",
                        "is_dependency": True,
                    }
                ]
            }
        }
    ]

    rewrite_sql, contract_steps, dependency_steps = _apply_rewrite_contract_dependencies(
        rewrite_sql="SELECT a.element FROM connected c JOIN atom a ON c.atom_id = a.atom_id",
        actions=actions,
        contract_steps_applied=[],
        dependency_repairs_applied=[],
    )

    assert rewrite_sql == "SELECT DISTINCT a.element FROM connected c JOIN atom a ON c.atom_id = a.atom_id"
    assert contract_steps
    assert not dependency_steps

    enum_action = Action(
        action_id="a1",
        source_group_id="g1",
        source_group_type=GroupType.FAMILY,
        primitive=ActionPrimitive.DROP_SELECT_SLOT,
        selected_candidate_id="c1",
        rationale_short="drop extra slot",
        allowed_edit_scope=[EditScope.SELECT],
        arguments={
            "from_exprs": ["b.element"],
            "repair_program": [
                {
                    "op": "SELECT_ENFORCE_DISTINCT",
                    "is_dependency": True,
                }
            ],
        },
    )
    enum_rewrite_sql, _, _ = _apply_rewrite_contract_dependencies(
        rewrite_sql="SELECT a.element FROM connected c JOIN atom a ON c.atom_id = a.atom_id",
        actions=[enum_action],
        contract_steps_applied=[],
        dependency_repairs_applied=[],
    )

    assert enum_rewrite_sql.startswith("SELECT DISTINCT")

    unchanged, _, _ = _apply_rewrite_contract_dependencies(
        rewrite_sql="SELECT a.element FROM connected c JOIN atom a ON c.atom_id = a.atom_id",
        actions=[],
        contract_steps_applied=[],
        dependency_repairs_applied=[],
    )

    assert unchanged == "SELECT a.element FROM connected c JOIN atom a ON c.atom_id = a.atom_id"

    no_visible_risk, guard_notes, dependency_notes = _apply_rewrite_contract_dependencies(
        rewrite_sql="SELECT a.atom_id FROM connected c JOIN atom a ON c.atom_id = a.atom_id",
        actions=actions,
        contract_steps_applied=[],
        dependency_repairs_applied=[],
    )

    assert no_visible_risk == "SELECT a.atom_id FROM connected c JOIN atom a ON c.atom_id = a.atom_id"
    assert guard_notes
    assert not dependency_notes


def test_rewrite_contract_dependency_applies_target_only_where_condition() -> None:
    actions = [
        {
            "action_id": "a1",
            "primitive": "REPLACE_SELECT_SLOT",
            "allowed_edit_scope": ["SELECT", "WHERE"],
            "arguments": {
                "from_exprs": ["s.sname", "sc.MailStreet"],
                "target_columns": [
                    {"target_table": "schools", "target_column": "MailStreet"},
                    {"target_table": "schools", "target_column": "School"},
                ],
                "repair_program": [
                    {
                        "op": "WHERE_ADD_CONDITION",
                        "locus": "WHERE",
                        "is_dependency": True,
                        "arguments": {
                            "policy_payload": {
                                "target_predicates": [
                                    {
                                        "predicate": "NOT T1.AvgScrRead IS NULL",
                                        "refs": [
                                            {
                                                "table": "satscores",
                                                "column": "AvgScrRead",
                                            }
                                        ],
                                    }
                                ]
                            }
                        },
                    }
                ],
            },
        }
    ]

    rewrite_sql, contract_steps, dependency_steps = _apply_rewrite_contract_dependencies(
        rewrite_sql=(
            "SELECT sc.MailStreet, sc.School FROM schools sc "
            "INNER JOIN satscores s ON s.cds = sc.CDSCode "
            "ORDER BY s.AvgScrRead ASC LIMIT 1"
        ),
        actions=actions,
        contract_steps_applied=[],
        dependency_repairs_applied=[],
    )

    assert "WHERE s.AvgScrRead IS NOT NULL ORDER BY" in rewrite_sql
    assert contract_steps
    assert dependency_steps == []


def test_rewrite_contract_dependency_skips_where_condition_when_target_table_absent() -> None:
    actions = [
        {
            "action_id": "a1",
            "primitive": "DROP_SELECT_SLOT",
            "allowed_edit_scope": ["SELECT", "WHERE"],
            "arguments": {
                "from_exprs": ["a.element"],
                "repair_program": [
                    {
                        "op": "WHERE_ADD_CONDITION",
                        "locus": "WHERE",
                        "is_dependency": True,
                        "arguments": {
                            "policy_payload": {
                                "target_predicates": [
                                    {
                                        "predicate": "T3.bond_id = 'TR001_10_11'",
                                        "refs": [
                                            {
                                                "table": "bond",
                                                "column": "bond_id",
                                            }
                                        ],
                                    }
                                ]
                            }
                        },
                    }
                ],
            },
        }
    ]

    rewrite_sql, contract_steps, _ = _apply_rewrite_contract_dependencies(
        rewrite_sql=(
            "SELECT a.element FROM connected c "
            "JOIN atom a ON c.atom_id = a.atom_id "
            "WHERE c.bond_id = 'TR004_8_9'"
        ),
        actions=actions,
        contract_steps_applied=[],
        dependency_repairs_applied=[],
    )

    assert "bond.bond_id" not in rewrite_sql
    assert contract_steps == ["WHERE_ADD_CONDITION not applied: no bindable new predicate"]


def test_rewrite_contract_dependency_binds_unaliased_table_without_keyword_alias() -> None:
    actions = [
        {
            "action_id": "a1",
            "primitive": "REPLACE_SELECT_SLOT",
            "allowed_edit_scope": ["SELECT", "WHERE"],
            "arguments": {
                "target_columns": [{"target_table": "atom", "target_column": "element"}],
                "repair_program": [
                    {
                        "op": "WHERE_ADD_CONDITION",
                        "locus": "WHERE",
                        "is_dependency": True,
                        "arguments": {
                            "policy_payload": {
                                "target_predicates": [
                                    {
                                        "predicate": "T1.element IS NOT NULL",
                                        "refs": [{"table": "atom", "column": "element"}],
                                    }
                                ]
                            }
                        },
                    }
                ],
            },
        }
    ]

    rewrite_sql, _, _ = _apply_rewrite_contract_dependencies(
        rewrite_sql=(
            "SELECT atom.element FROM atom "
            "JOIN connected ON atom.atom_id = connected.atom_id"
        ),
        actions=actions,
        contract_steps_applied=[],
        dependency_repairs_applied=[],
    )

    assert "JOIN.element" not in rewrite_sql
    assert "WHERE atom.element IS NOT NULL" in rewrite_sql


def test_rewrite_contract_dependency_rebinds_unbound_target_alias_predicate() -> None:
    actions = [
        {
            "action_id": "a1",
            "primitive": "REPLACE_SELECT_SLOT",
            "allowed_edit_scope": ["SELECT", "WHERE"],
            "arguments": {
                "target_columns": [{"target_table": "schools", "target_column": "School"}],
                "repair_program": [
                    {
                        "op": "WHERE_ADD_CONDITION",
                        "locus": "WHERE",
                        "is_dependency": True,
                        "arguments": {
                            "policy_payload": {
                                "target_predicates": [
                                    {
                                        "predicate": "NOT T1.AvgScrRead IS NULL",
                                        "refs": [
                                            {
                                                "table": "satscores",
                                                "column": "AvgScrRead",
                                            }
                                        ],
                                    }
                                ]
                            }
                        },
                    }
                ],
            },
        }
    ]

    rewrite_sql, _, _ = _apply_rewrite_contract_dependencies(
        rewrite_sql=(
            "SELECT sc.MailStreet, s.sname FROM satscores s "
            "JOIN schools sc ON s.cds = sc.CDSCode "
            "WHERE NOT T1.AvgScrRead IS NULL ORDER BY s.AvgScrRead ASC LIMIT 1"
        ),
        actions=actions,
        contract_steps_applied=[],
        dependency_repairs_applied=[],
    )

    assert "T1.AvgScrRead" not in rewrite_sql
    assert "NOT s.AvgScrRead IS NULL" in rewrite_sql
    assert "s.AvgScrRead IS NOT NULL" not in rewrite_sql


def test_rewrite_contract_dependency_rebinds_unbound_alias_only_inside_where() -> None:
    actions = [
        {
            "action_id": "a1",
            "primitive": "REPLACE_SELECT_SLOT",
            "allowed_edit_scope": ["WHERE"],
            "arguments": {
                "repair_program": [
                    {
                        "op": "WHERE_ADD_CONDITION",
                        "locus": "WHERE",
                        "is_dependency": True,
                        "arguments": {
                            "policy_payload": {
                                "target_predicates": [
                                    {
                                        "predicate": "T1.State = 'CA'",
                                        "refs": [{"table": "schools", "column": "State"}],
                                    }
                                ]
                            }
                        },
                    }
                ],
            },
        }
    ]

    rewrite_sql, _, _ = _apply_rewrite_contract_dependencies(
        rewrite_sql=(
            "SELECT T1.State FROM schools sc "
            "WHERE sc.CDSCode = '001' ORDER BY T1.State"
        ),
        actions=actions,
        contract_steps_applied=[],
        dependency_repairs_applied=[],
    )

    assert rewrite_sql.startswith("SELECT T1.State")
    assert "ORDER BY T1.State" in rewrite_sql
    assert "WHERE sc.CDSCode = '001' AND sc.State = 'CA'" in rewrite_sql


def test_rewrite_contract_dependency_applies_target_ranking_contract() -> None:
    actions = [
        {
            "action_id": "a1",
            "primitive": "REPLACE_SELECT_SLOT",
            "allowed_edit_scope": ["SELECT", "WHERE", "ORDER_BY", "LIMIT"],
            "arguments": {
                "repair_program": [
                    {
                        "op": "ORDER_BY_APPLY",
                        "locus": "ORDER_BY",
                        "is_dependency": True,
                        "arguments": {
                            "policy_payload": {
                                "target_order_by": [
                                    {
                                        "expression": "T2.Latitude",
                                        "direction": "ASC",
                                        "refs": [
                                            {
                                                "table": "schools",
                                                "column": "Latitude",
                                            }
                                        ],
                                    }
                                ],
                                "target_limit": "1",
                            }
                        },
                    },
                    {
                        "op": "LIMIT_APPLY",
                        "locus": "LIMIT",
                        "is_dependency": True,
                        "arguments": {
                            "policy_payload": {
                                "target_limit": "1",
                                "target_order_by": [
                                    {
                                        "expression": "T2.Latitude",
                                        "direction": "ASC",
                                        "refs": [
                                            {
                                                "table": "schools",
                                                "column": "Latitude",
                                            }
                                        ],
                                    }
                                ],
                            }
                        },
                    },
                    {
                        "op": "WHERE_DROP_RANKING_PREDICATE",
                        "locus": "WHERE",
                        "is_dependency": True,
                        "arguments": {
                            "policy_payload": {
                                "source_ranking_predicates": [
                                    {
                                        "predicate": "Latitude = (SELECT MIN(Latitude) FROM schools)",
                                        "aggregate": "MIN",
                                        "refs": [
                                            {
                                                "table": "schools",
                                                "column": "Latitude",
                                            }
                                        ],
                                    }
                                ]
                            }
                        },
                    },
                ],
            },
        }
    ]

    rewrite_sql, contract_steps, _ = _apply_rewrite_contract_dependencies(
        rewrite_sql=(
            "SELECT sc.City FROM schools sc "
            "WHERE sc.Latitude = (SELECT MIN(Latitude) FROM schools)"
        ),
        actions=actions,
        contract_steps_applied=[],
        dependency_repairs_applied=[],
    )

    assert rewrite_sql.endswith("ORDER BY sc.Latitude ASC LIMIT 1")
    assert "MIN(Latitude)" not in rewrite_sql
    assert "ORDER_BY_APPLY applied 1 expression" in " ".join(contract_steps)
    assert "LIMIT_APPLY applied LIMIT 1" in " ".join(contract_steps)
    assert "WHERE_DROP_RANKING_PREDICATE dropped 1" in " ".join(contract_steps)


def test_rewrite_contract_dependency_does_not_drop_ranking_predicate_for_different_table() -> None:
    actions = [
        {
            "action_id": "a1",
            "primitive": "REPLACE_SELECT_SLOT",
            "allowed_edit_scope": ["WHERE"],
            "arguments": {
                "repair_program": [
                    {
                        "op": "WHERE_DROP_RANKING_PREDICATE",
                        "locus": "WHERE",
                        "is_dependency": True,
                        "arguments": {
                            "policy_payload": {
                                "source_ranking_predicates": [
                                    {
                                        "predicate": "Latitude = (SELECT MIN(Latitude) FROM schools)",
                                        "aggregate": "MIN",
                                        "refs": [{"table": "schools", "column": "Latitude"}],
                                    }
                                ]
                            }
                        },
                    }
                ],
            },
        }
    ]
    original_sql = (
        "SELECT d.Name FROM districts d "
        "WHERE d.Latitude = (SELECT MIN(Latitude) FROM districts)"
    )

    rewrite_sql, contract_steps, _ = _apply_rewrite_contract_dependencies(
        rewrite_sql=original_sql,
        actions=actions,
        contract_steps_applied=[],
        dependency_repairs_applied=[],
    )

    assert rewrite_sql == original_sql
    assert "WHERE_DROP_RANKING_PREDICATE not applied" in " ".join(contract_steps)


def test_rewrite_contract_dependency_order_by_preserves_limit_without_limit_scope() -> None:
    actions = [
        {
            "action_id": "a1",
            "primitive": "REPLACE_SELECT_SLOT",
            "allowed_edit_scope": ["ORDER_BY"],
            "arguments": {
                "repair_program": [
                    {
                        "op": "ORDER_BY_APPLY",
                        "locus": "ORDER_BY",
                        "is_dependency": True,
                        "arguments": {
                            "policy_payload": {
                                "target_order_by": [
                                    {
                                        "expression": "T2.Latitude",
                                        "direction": "ASC",
                                        "refs": [{"table": "schools", "column": "Latitude"}],
                                    }
                                ]
                            }
                        },
                    }
                ],
            },
        }
    ]

    rewrite_sql, _, _ = _apply_rewrite_contract_dependencies(
        rewrite_sql="SELECT sc.City FROM schools sc LIMIT 5",
        actions=actions,
        contract_steps_applied=[],
        dependency_repairs_applied=[],
    )

    assert rewrite_sql == "SELECT sc.City FROM schools sc ORDER BY sc.Latitude ASC LIMIT 5"


def test_rewrite_contract_dependency_skips_target_ranking_without_scope() -> None:
    actions = [
        {
            "action_id": "a1",
            "primitive": "REPLACE_SELECT_SLOT",
            "allowed_edit_scope": ["SELECT"],
            "arguments": {
                "repair_program": [
                    {
                        "op": "ORDER_BY_APPLY",
                        "locus": "ORDER_BY",
                        "is_dependency": True,
                        "arguments": {
                            "policy_payload": {
                                "target_order_by": [
                                    {
                                        "expression": "T2.Latitude",
                                        "direction": "ASC",
                                        "refs": [{"table": "schools", "column": "Latitude"}],
                                    }
                                ]
                            }
                        },
                    }
                ],
            },
        }
    ]
    original_sql = "SELECT sc.City FROM schools sc"

    rewrite_sql, contract_steps, _ = _apply_rewrite_contract_dependencies(
        rewrite_sql=original_sql,
        actions=actions,
        contract_steps_applied=[],
        dependency_repairs_applied=[],
    )

    assert rewrite_sql == original_sql
    assert "ORDER_BY_APPLY not applied: order_by_scope_not_allowed" in contract_steps


def test_rewrite_contract_dependency_keeps_outer_alias_when_subquery_reuses_table() -> None:
    actions = [
        {
            "action_id": "a1",
            "primitive": "INSERT_BRIDGE",
            "allowed_edit_scope": ["JOIN", "WHERE"],
            "arguments": {
                "repair_program": [
                    {
                        "op": "WHERE_ADD_CONDITION",
                        "locus": "WHERE",
                        "is_dependency": True,
                        "arguments": {
                            "policy_payload": {
                                "target_predicates": [
                                    {
                                        "predicate": "T2.State = 'CA'",
                                        "refs": [{"table": "schools", "column": "State"}],
                                    }
                                ]
                            }
                        },
                    }
                ],
            },
        }
    ]

    rewrite_sql, _, _ = _apply_rewrite_contract_dependencies(
        rewrite_sql=(
            "SELECT T2.City FROM schools T2 JOIN frpm T1 ON T1.CDSCode = T2.CDSCode "
            "WHERE T2.State = 'CA' "
            "AND T2.Latitude = (SELECT MIN(Latitude) FROM schools WHERE State = 'CA')"
        ),
        actions=actions,
        contract_steps_applied=[],
        dependency_repairs_applied=[],
    )

    assert "schools.State" not in rewrite_sql
    assert rewrite_sql.count("T2.State = 'CA'") == 1


def test_rewrite_contract_dependency_does_not_use_subquery_alias_for_outer_predicate() -> None:
    actions = [
        {
            "action_id": "a1",
            "primitive": "INSERT_BRIDGE",
            "allowed_edit_scope": ["JOIN", "WHERE"],
            "arguments": {
                "repair_program": [
                    {
                        "op": "WHERE_ADD_CONDITION",
                        "locus": "WHERE",
                        "is_dependency": True,
                        "arguments": {
                            "policy_payload": {
                                "target_predicates": [
                                    {
                                        "predicate": "T2.State = 'CA'",
                                        "refs": [{"table": "schools", "column": "State"}],
                                    }
                                ]
                            }
                        },
                    }
                ],
            },
        }
    ]

    rewrite_sql, _, _ = _apply_rewrite_contract_dependencies(
        rewrite_sql=(
            "SELECT T2.City FROM schools T2 JOIN frpm T1 ON T1.CDSCode = T2.CDSCode "
            "WHERE T2.Latitude = (SELECT MIN(s.Latitude) FROM schools s WHERE s.State = 'CA')"
        ),
        actions=actions,
        contract_steps_applied=[],
        dependency_repairs_applied=[],
    )

    assert "AND T2.State = 'CA'" in rewrite_sql
    assert "AND s.State = 'CA'" not in rewrite_sql


def test_rewrite_contract_dependency_reroutes_to_target_relation_edges() -> None:
    actions = [
        {
            "action_id": "a1",
            "primitive": "REROUTE_FACT",
            "allowed_edit_scope": ["FROM", "JOIN", "SELECT"],
            "arguments": {
                "target_output_refs": [
                    {"table": "atom", "column": "element", "slot_index": 0}
                ],
                "target_relation_edges": [
                    {
                        "left": {"table": "atom", "column": "molecule_id"},
                        "right": {"table": "bond", "column": "molecule_id"},
                        "canonical_key": "atom.molecule_id=bond.molecule_id",
                    },
                    {
                        "left": {"table": "atom", "column": "atom_id"},
                        "right": {"table": "connected", "column": "atom_id"},
                        "canonical_key": "atom.atom_id=connected.atom_id",
                    },
                ],
                "repair_program": [
                    {
                        "op": "JOIN_ADD_BRIDGE",
                        "locus": "JOIN",
                        "is_dependency": False,
                    }
                ],
            },
        }
    ]

    rewrite_sql, contract_steps, _ = _apply_rewrite_contract_dependencies(
        rewrite_sql=(
            "SELECT DISTINCT T1.element FROM atom T1 "
            "JOIN bond T2 ON T1.molecule_id = T2.molecule_id "
            "JOIN connected T3 ON T2.bond_id = T3.bond_id "
            "JOIN oldtab o ON o.atom_id = T1.atom_id "
            "WHERE T2.bond_type = '=' AND o.status = 'A' AND status = 'A'"
        ),
        actions=actions,
        contract_steps_applied=[],
        dependency_repairs_applied=[],
    )

    assert "atom.molecule_id = bond.molecule_id" in rewrite_sql
    assert "atom.atom_id = connected.atom_id" in rewrite_sql
    assert "bond.bond_type = '='" in rewrite_sql
    assert "bond.bond_id = connected.bond_id" not in rewrite_sql
    assert "oldtab.status" not in rewrite_sql
    assert "status = 'A'" not in rewrite_sql
    assert "REROUTE_FACT target relation edges applied from action contract" in contract_steps


def test_rewrite_contract_dependency_reroute_preserves_allowed_subquery_filter() -> None:
    actions = [
        {
            "action_id": "a1",
            "primitive": "REROUTE_FACT",
            "allowed_edit_scope": ["FROM", "JOIN", "SELECT"],
            "arguments": {
                "target_output_refs": [
                    {"table": "atom", "column": "element", "slot_index": 0}
                ],
                "target_relation_edges": [
                    {
                        "left": {"table": "atom", "column": "molecule_id"},
                        "right": {"table": "bond", "column": "molecule_id"},
                        "canonical_key": "atom.molecule_id=bond.molecule_id",
                    },
                    {
                        "left": {"table": "atom", "column": "atom_id"},
                        "right": {"table": "connected", "column": "atom_id"},
                        "canonical_key": "atom.atom_id=connected.atom_id",
                    },
                ],
            },
        }
    ]

    rewrite_sql, _, _ = _apply_rewrite_contract_dependencies(
        rewrite_sql=(
            "SELECT DISTINCT element FROM ("
            "SELECT a1.element FROM bond b "
            "JOIN connected c ON b.bond_id = c.bond_id "
            "JOIN atom a1 ON c.atom_id = a1.atom_id "
            "WHERE b.bond_type = '=' "
            "UNION "
            "SELECT a2.element FROM bond b "
            "JOIN connected c ON b.bond_id = c.bond_id "
            "JOIN atom a2 ON c.atom_id2 = a2.atom_id "
            "WHERE b.bond_type = '=')"
        ),
        actions=actions,
        contract_steps_applied=[],
        dependency_repairs_applied=[],
    )

    assert "WHERE bond.bond_type = '='" in rewrite_sql
    assert rewrite_sql.count("bond.bond_type") == 1


def test_rewrite_contract_dependency_skips_reroute_without_rebuild_scope() -> None:
    actions = [
        {
            "action_id": "a1",
            "primitive": "REROUTE_FACT",
            "allowed_edit_scope": ["WHERE"],
            "arguments": {
                "target_output_refs": [
                    {"table": "atom", "column": "element", "slot_index": 0}
                ],
                "target_relation_edges": [
                    {
                        "left": {"table": "atom", "column": "molecule_id"},
                        "right": {"table": "bond", "column": "molecule_id"},
                        "canonical_key": "atom.molecule_id=bond.molecule_id",
                    }
                ],
            },
        }
    ]
    original_sql = "SELECT oldtab.name FROM oldtab WHERE status = 'A'"

    rewrite_sql, contract_steps, _ = _apply_rewrite_contract_dependencies(
        rewrite_sql=original_sql,
        actions=actions,
        contract_steps_applied=[],
        dependency_repairs_applied=[],
    )

    assert rewrite_sql == original_sql
    assert contract_steps == []


def test_rewrite_scope_enforcement_fails_closed_for_out_of_scope_edit() -> None:
    rewrite_sql, traces, contract_steps = _enforce_rewrite_scope(
        original_sql="SELECT a.name FROM a WHERE a.id = 1",
        rewrite_sql="SELECT b.name FROM a JOIN b ON a.id = b.a_id WHERE a.id = 1",
        actions=[
            {
                "action_id": "a1",
                "allowed_edit_scope": ["JOIN"],
            }
        ],
        traces=[
            {
                "action_id": "a1",
                "realized": True,
                "edits": [
                    {"edit_kind": "add", "location": "JOIN"},
                    {"edit_kind": "replace", "location": "SELECT"},
                ],
                "scope_violation": False,
            }
        ],
        contract_steps_applied=[],
    )

    assert rewrite_sql == "SELECT a.name FROM a WHERE a.id = 1"
    assert traces[0]["realized"] is False
    assert traces[0]["scope_violation"] is True
    assert "scope_enforced_fail_closed" in " ".join(contract_steps)


def _pair_output_singleton(case_id: str = "206") -> GroupSummary:
    singleton = _singleton(case_id)
    singleton.trigger_signature = TriggerSignature(required_pred_tags=["pair_output"])
    singleton.trigger_contract = singleton.trigger_contract.model_copy(
        update={
            "required_signals": [],
            "variant_required_signal_sets": [],
            "decisive_pred_signals": [],
            "negative_signals": ["pred.output_grain=pair_rows"],
            "action_contract": {
                "program_type": "select_drop",
                "output_shape_delta": {
                    "current_arity": 2,
                    "target_arity": 1,
                    "arity_delta": -1,
                    "arity_direction": "decrease",
                    "operation": "remove_output_slot",
                },
            },
            "source_case_contract": {
                "select_arity": 2,
                "output_shape_current": {
                    "arity": 2,
                    "roles": ["identifier_like", "identifier_like"],
                    "grain": "pair_rows",
                },
            },
        }
    )
    return singleton


def test_pair_output_singleton_contract_materializes_from_legacy_signature() -> None:
    singleton = _pair_output_singleton()

    ensure_materialized_trigger_contract(singleton)

    payload = singleton.trigger_contract.model_dump(mode="json")
    assert singleton.runtime_usable is True
    assert is_contract_runtime_executable(payload)
    assert "pred.output_arity=2" in payload["required_signals"]
    assert "pred.pair_output=True" in payload["decisive_pred_signals"]
    assert "pred.output_grain=pair_rows" not in payload["negative_signals"]


def test_runtime_signals_treat_pair_rows_as_positive_source_signal() -> None:
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="307",
        question="Which endpoint should be returned?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id WHERE b.id = 'x'",
            columns=["a.id", "b.id"],
            select_shape="arity=2",
        ),
        case_signal_view=CaseSignalView(
            db_id="toy",
            pred_sql_view=PredSqlSignalView(
                select_arity=2,
                output_shape_current={
                    "arity": 2,
                    "roles": ["identifier_like", "identifier_like"],
                    "grain": "pair_rows",
                },
                predicate_profile={"comparison_operators": ["="], "predicate_count": 1},
            ),
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id", "a_id"]},
        ),
    )

    signals = build_current_case_signals(case_view)

    assert "pred.output_arity=2" in signals
    assert "pred.output_grain=pair_rows" in signals
    assert "pred.pair_output=True" in signals


def test_pair_output_singleton_canonical_exact_triggers_and_dry_runs() -> None:
    singleton = _pair_output_singleton()
    ensure_materialized_trigger_contract(singleton)
    library = LibraryStateV2(db_id="toy", singletons=[singleton])
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="307",
        question="Which endpoint should be returned?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id WHERE b.id = 'y'",
            columns=["a.id", "b.id"],
            select_shape="arity=2",
        ),
        case_signal_view=CaseSignalView(
            db_id="toy",
            pred_sql_view=PredSqlSignalView(
                select_arity=2,
                output_shape_current={
                    "arity": 2,
                    "roles": ["identifier_like", "identifier_like"],
                    "grain": "pair_rows",
                },
                predicate_profile={"comparison_operators": ["="], "predicate_count": 1},
            ),
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id", "a_id"]},
        ),
    )

    result = trigger_memory_objects(library=library, case_view=case_view, db_id="toy")

    assert [group.group_id for group in result.selected_groups] == [singleton.group_id]
    audit = result.candidates[0]
    assert "gate_passed" in audit.gate_reasons
    assert audit.binder_dry_run_success is True


def test_singleton_exact_multi_action_dry_run_is_not_marked_success() -> None:
    singleton = _pair_output_singleton()
    singleton.instantiation_program = singleton.instantiation_program.model_copy(
        update={"synthesized_program": None, "program_coverage": None}
    )
    ensure_materialized_trigger_contract(singleton)
    library = LibraryStateV2(db_id="toy", singletons=[singleton])
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="307",
        question="Which endpoint should be returned?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id WHERE b.id = 'y'",
            columns=["a.id", "b.id"],
            select_shape="arity=2",
        ),
        case_signal_view=CaseSignalView(
            db_id="toy",
            pred_sql_view=PredSqlSignalView(
                select_arity=2,
                output_shape_current={
                    "arity": 2,
                    "roles": ["identifier_like", "identifier_like"],
                    "grain": "pair_rows",
                },
                predicate_profile={"comparison_operators": ["="], "predicate_count": 1},
            ),
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id", "a_id"]},
        ),
    )

    result = trigger_memory_objects(library=library, case_view=case_view, db_id="toy")

    assert result.selected_groups == []
    audit = result.candidates[0]
    assert "singleton_canonical_exact_failed" in audit.gate_reasons
    assert audit.binder_dry_run_success is False


def test_wrong_case_auditor_parses_candidate_fix_sql_and_patch_summary(monkeypatch) -> None:
    def fake_call_llm(_prompt, expect_json=True):
        return {
            "final_error_reason": "missing target output",
            "minimal_fix": "replace selected field",
            "candidate_fix_sql": "SELECT b.name FROM a JOIN b ON a.b_id = b.id",
            "minimal_patch_ops": [
                {
                    "clause": "SELECT",
                    "edit_kind": "replace",
                    "source_fragment": "a.id",
                    "target_fragment": "b.name",
                    "primary": True,
                }
            ],
            "secondary_differences": ["join alias cleanup"],
            "confidence": "medium",
        }

    monkeypatch.setattr(
        "method.EEA.rulebook.common.llm.utils.call_llm",
        fake_call_llm,
    )

    audit = run_wrong_case_auditor(
        question="Which names should be returned?",
        evidence="",
        pred_sql="SELECT a.id FROM a",
        gold_sql="SELECT b.name FROM a JOIN b ON a.b_id = b.id",
        local_schema_view=LocalSchemaView(db_id="toy"),
    )

    assert audit.candidate_fix_sql == "SELECT b.name FROM a JOIN b ON a.b_id = b.id"
    assert audit.validated_sql is None
    assert audit.minimal_patch_ops[0]["edit_kind"] == "replace"
    assert audit.secondary_differences == ["join alias cleanup"]


def test_action_primitive_support_matrix_is_fully_implemented() -> None:
    from method.EEA.rulebook.common.runtime.action_compiler import _IMPLEMENTED

    assert set(ActionPrimitive) == set(_IMPLEMENTED)


def test_prompt_contracts_reflect_current_llm_responsibilities() -> None:
    from method.EEA.rulebook.common.llm.prompts.action_compiler import ACTION_COMPILER_PROMPT
    from method.EEA.rulebook.common.llm.prompts.compatibility_judge import COMPATIBILITY_JUDGE_PROMPT
    from method.EEA.rulebook.common.llm.prompts.memory_rewrite import MEMORY_REWRITE_PROMPT

    assert "memory_alignment_status" in ACTION_COMPILER_PROMPT
    assert '"advisory_only": true' in COMPATIBILITY_JUDGE_PROMPT
    assert "must NOT supply missing" in MEMORY_REWRITE_PROMPT


def test_memory_rewrite_fails_closed_when_hint_would_fill_missing_action_bindings(
    monkeypatch,
) -> None:
    original_sql = "SELECT student.id FROM student"

    def fake_call_llm(*_args, **_kwargs):
        return {
            "rewrite_sql": "SELECT school.name FROM student JOIN school ON student.school_id = school.id",
            "action_realization_traces": [
                {
                    "action_id": "a1",
                    "realized": True,
                    "edits": [
                        {
                            "edit_kind": "replace",
                            "location": "SELECT",
                            "before_snippet": "student.id",
                            "after_snippet": "school.name",
                        }
                    ],
                    "scope_violation": False,
                    "notes": "used hint text",
                }
            ],
            "contract_steps_applied": [],
            "notes": "llm attempted rewrite from hint",
        }

    monkeypatch.setattr(
        "method.EEA.rulebook.common.llm.utils.call_llm",
        fake_call_llm,
    )

    result = run_memory_rewrite(
        question="Which school name should be returned?",
        evidence="",
        c0_top1_sql=original_sql,
        actions=[
            Action(
                action_id="a1",
                source_group_id="g1",
                source_group_type=GroupType.FAMILY,
                primitive=ActionPrimitive.REPLACE_SELECT_SLOT,
                arguments={
                    "from_exprs": ["student.id"],
                    # No target binding: if rewrite still changes SQL, it can only
                    # be coming from the natural-language hint.
                },
                rationale_short="replace current output",
                allowed_edit_scope=[EditScope.SELECT],
            )
        ],
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["student", "school"],
            columns_by_table={
                "student": ["id", "school_id"],
                "school": ["id", "name"],
            },
        ),
        natural_language_hint="Replace student.id with school.name.",
    )

    assert result["rewrite_sql"] == original_sql
    assert result["action_realization_traces"][0].realized is False
    assert "missing_action_bindings=target_columns/target_expr/target_output_refs" in (
        result["action_realization_traces"][0].notes or ""
    )
    assert any(
        str(step).startswith("rewrite_contract_fail_closed:")
        for step in result["contract_steps_applied"]
    )


def test_action_compiler_skips_llm_when_unique_deterministic_candidate_covers_required_op(
    monkeypatch,
) -> None:
    group_id = "grp-deterministic-only"
    group = _compiler_group(
        group_id,
        max_actions=1,
        selection_policy="deterministic_only",
    )
    group.instantiation_program.synthesized_program = {"ops": [{"op_id": "canon-add"}]}

    def fail_call_llm(*_args, **_kwargs):
        raise AssertionError("LLM should not be called for deterministic preselection")

    monkeypatch.setattr(
        "method.EEA.rulebook.common.llm.utils.call_llm",
        fail_call_llm,
    )

    output = run_action_compiler(
        runtime_case_view=_compiler_runtime_case("SELECT client.client_id FROM client"),
        memory_objects=[group],
        precomputed_candidate_sets=[
            ActionCandidateSet(
                primitive=ActionPrimitive.ADD_SELECT_SLOT,
                candidates=[
                    ActionCandidate(
                        candidate_id="cand-deterministic-add",
                        source_group_id=group_id,
                        source_group_type=GroupType.FAMILY,
                        provenance="test",
                        arguments={
                            "canonical_op_id": "canon-add",
                            "target_columns": [
                                {"target_table": "district", "target_column": "district_id"}
                            ],
                            "required_edit_scopes": ["SELECT"],
                        },
                    )
                ],
            )
        ],
        precomputed_schema_diagnostics=LocalSchemaViewDiagnostics(),
    )

    assert len(output.actions) == 1
    assert output.actions[0].selection_origin == "deterministic_unique"
    assert output.actions[0].selected_candidate_id == "cand-deterministic-add"


def test_extended_primitives_enumerate_positive_and_fail_closed() -> None:
    from method.EEA.rulebook.common.runtime.action_compiler import (
        _enumerate_change_grain,
        _enumerate_materialize_ranking_output,
        _enumerate_move_condition,
        _enumerate_switch_canonical_field,
    )

    move_view = RuntimeCaseView(
        db_id="toy",
        case_id="move-cond",
        question="Which classes have older students?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT class_id, COUNT(*) FROM student WHERE age > 10 GROUP BY class_id",
            tables=["student"],
            columns=["class_id", "COUNT(*)"],
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["student"],
            columns_by_table={"student": ["class_id", "age"]},
        ),
    )
    move_positive = _enumerate_move_condition(
        case_view=move_view,
        group_id="g1",
        group_type=GroupType.FAMILY,
        canonical_op={
            "arguments": {
                "operation_signature": {
                    "predicate_scope_delta": {
                        "removed_source_predicates": ["age > 10"],
                        "possible_scope_move": True,
                    },
                    "grain_delta": {"target_has_aggregate": True},
                }
            }
        },
        repair_program=[{"locus": "WHERE", "op": "WHERE_REPLACE_CONDITION"}],
    )
    move_negative = _enumerate_move_condition(
        case_view=RuntimeCaseView(
            db_id="toy",
            case_id="move-cond-none",
            question="Which classes?",
            question_contract=QuestionContract(),
            pred_manifestation=PredManifestation(
                top1_sql="SELECT class_id FROM student",
                tables=["student"],
                columns=["class_id"],
            ),
            local_schema_view=LocalSchemaView(
                db_id="toy",
                tables=["student"],
                columns_by_table={"student": ["class_id"]},
            ),
        ),
        group_id="g1",
        group_type=GroupType.FAMILY,
        canonical_op={"arguments": {"operation_signature": {"predicate_scope_delta": {}}}},
    )

    assert move_positive.candidates
    assert move_positive.candidates[0].arguments["predicate_ref"] == "age > 10"
    assert move_positive.candidates[0].arguments["to_scope"] == "CASE_NUMERATOR"
    assert move_negative.empty_reason == "no_current_predicate_to_move"

    grain_view = RuntimeCaseView(
        db_id="toy",
        case_id="grain-change",
        question="How many students are there?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT student.id FROM student",
            tables=["student"],
            columns=["student.id"],
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["student"],
            columns_by_table={"student": ["id"]},
        ),
    )
    grain_positive = _enumerate_change_grain(
        case_view=grain_view,
        group_id="g2",
        group_type=GroupType.FAMILY,
        canonical_op={
            "arguments": {
                "operation_signature": {
                    "grain_delta": {
                        "source_grain": "row_result",
                        "target_grain": "scalar_aggregate",
                        "grain_changed": True,
                        "source_has_aggregate": False,
                        "target_has_aggregate": True,
                    }
                },
                "output_shape_delta": {
                    "current_grain": "row_result",
                    "target_grain": "scalar_aggregate",
                },
            }
        },
        repair_program=[{"locus": "SELECT", "op": "SELECT_REPLACE_SLOT"}],
    )
    grain_negative = _enumerate_change_grain(
        case_view=grain_view,
        group_id="g2",
        group_type=GroupType.FAMILY,
        canonical_op={"arguments": {"operation_signature": {"grain_delta": {}}}},
    )

    assert grain_positive.candidates
    assert grain_positive.candidates[0].arguments["source_grain"] == "row_result"
    assert grain_positive.candidates[0].arguments["target_grain"] == "scalar_aggregate"
    assert grain_negative.empty_reason == "no_grain_delta_for_change_grain"

    switch_view = RuntimeCaseView(
        db_id="toy",
        case_id="switch-field",
        question="Which school name?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT student.id FROM student",
            tables=["student", "school"],
            columns=["student.id"],
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["student", "school"],
            columns_by_table={"student": ["id", "school_id"], "school": ["id", "name"]},
        ),
    )
    switch_positive = _enumerate_switch_canonical_field(
        case_view=switch_view,
        skeleton=RepairSkeletonStructural(
            locus=Locus.SELECT,
            op_family=OpFamily.REPLACE,
            target_family=TargetFamily.SLOT,
            output_contract=OutputContract.UNCHANGED,
            output_shape_delta=OutputShapeDelta(
                current_arity=1,
                target_arity=1,
                arity_direction="same",
            ),
        ),
        slots=[
            InstantiationSlot(
                name="target_column",
                kind="column",
                required=True,
                allowed_role_families=["name"],
            )
        ],
        group_id="g3",
        group_type=GroupType.FAMILY,
        preferred_target_refs=[
            {
                "source": "target_sql",
                "sql_role": "output_slot",
                "table": "school",
                "column": "name",
                "expression": "school.name",
                "slot_index": 0,
            }
        ],
    )
    switch_negative = _enumerate_switch_canonical_field(
        case_view=switch_view,
        skeleton=RepairSkeletonStructural(
            locus=Locus.SELECT,
            op_family=OpFamily.REPLACE,
            target_family=TargetFamily.SLOT,
            output_contract=OutputContract.UNCHANGED,
            output_shape_delta=OutputShapeDelta(
                current_arity=2,
                target_arity=1,
                arity_direction="decrease",
            ),
        ),
        slots=[
            InstantiationSlot(
                name="target_column",
                kind="column",
                required=True,
                allowed_role_families=["name"],
            )
        ],
        group_id="g3",
        group_type=GroupType.FAMILY,
        preferred_target_refs=[],
    )

    assert switch_positive.candidates
    assert switch_positive.candidates[0].arguments["current_expr"] == "student.id"
    assert switch_positive.candidates[0].arguments["target_expr"] == "school.name"
    assert switch_negative.empty_reason == "canonical_field_switch_requires_same_output_arity"

    ranking_view = RuntimeCaseView(
        db_id="toy",
        case_id="rank-output",
        question="Which city is farthest south?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT schools.City FROM schools",
            tables=["schools"],
            columns=["schools.City"],
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["schools"],
            columns_by_table={"schools": ["City", "Latitude"]},
        ),
    )
    ranking_variant = {
        "target_output_refs": [
            {
                "source": "target_sql",
                "sql_role": "output_slot",
                "table": "schools",
                "column": "City",
                "expression": "schools.City",
                "slot_index": 0,
            }
        ],
        "accessory_policies": [
            {
                "op": "ORDER_BY_APPLY",
                "target_order_by": [
                    {
                        "expression": "schools.Latitude",
                        "direction": "ASC",
                        "refs": [{"table": "schools", "column": "Latitude"}],
                    }
                ],
            },
            {"op": "LIMIT_APPLY", "target_limit": "1"},
        ],
    }
    ranking_positive = _enumerate_materialize_ranking_output(
        case_view=ranking_view,
        group_id="g4",
        group_type=GroupType.FAMILY,
        canonical_op={"arguments": {}},
        member_variants=[ranking_variant],
        repair_program=[{"locus": "ORDER_BY", "op": "ORDER_BY_APPLY"}],
    )
    ranking_negative = _enumerate_materialize_ranking_output(
        case_view=ranking_view,
        group_id="g4",
        group_type=GroupType.FAMILY,
        canonical_op={"arguments": {}},
        member_variants=[],
    )

    assert ranking_positive.candidates
    assert ranking_positive.candidates[0].arguments["window_fn"] == "ROW_NUMBER"
    assert ranking_positive.candidates[0].arguments["tie_policy"] == "single_top"
    assert ranking_negative.empty_reason == "no_ranking_accessory_policy"


def test_runtime_trigger_merges_same_canonical_envelope_instead_of_conflicting() -> None:
    singleton_a = _pair_output_singleton("206")
    singleton_b = _pair_output_singleton("249")
    ensure_materialized_trigger_contract(singleton_a)
    ensure_materialized_trigger_contract(singleton_b)
    library = LibraryStateV2(
        db_id="toy",
        singletons=[singleton_a, singleton_b],
    )
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="253",
        question="Which endpoint should be returned?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id, b.id FROM a JOIN b ON a.id = b.a_id",
            columns=["a.id", "b.id"],
            select_shape="arity=2",
        ),
        case_signal_view=CaseSignalView(
            db_id="toy",
            pred_sql_view=PredSqlSignalView(
                select_arity=2,
                output_shape_current={
                    "arity": 2,
                    "roles": ["identifier_like", "identifier_like"],
                    "grain": "pair_rows",
                },
                predicate_profile={"comparison_operators": ["="], "predicate_count": 1},
            ),
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a", "b"],
            columns_by_table={"a": ["id"], "b": ["id", "a_id"]},
        ),
    )

    result = trigger_memory_objects(library=library, case_view=case_view, db_id="toy")

    assert len(result.selected_groups) == 2
    assert all("conflicting_action_contracts" not in audit.gate_reasons for audit in result.candidates)


def test_runtime_applicability_rejects_missing_source_antipattern_shape() -> None:
    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    family.runtime_usable = True
    family.trigger_contract = family.trigger_contract.model_copy(
        update={
            "required_signals": ["pred.output_role=identifier"],
            "variant_required_signal_sets": [],
            "decisive_pred_signals": ["pred.output_role=identifier"],
            "action_contract": {
                **family.trigger_contract.action_contract,
                "program_envelope": {
                    "source_antipatterns": [
                        {
                            "kind": "output_path_delta",
                            "target_output_subset_of_source": True,
                            "source_output_path_roles": ["output_slot_0", "output_slot_1"],
                        }
                    ],
                    "required_role_slots": [],
                    "negative_guards": [],
                },
            },
        }
    )
    library = LibraryStateV2(db_id="toy", experience_families=[family])
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="single-output",
        question="Which endpoint should be returned?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id FROM a",
            columns=["a.id"],
        ),
        case_signal_view=CaseSignalView(
            db_id="toy",
            pred_sql_view=PredSqlSignalView(
                select_arity=1,
                output_shape_current={"arity": 1, "roles": ["identifier"]},
                select_role_profile=["identifier"],
            ),
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a"],
            columns_by_table={"a": ["id"]},
        ),
    )

    result = trigger_memory_objects(library=library, case_view=case_view, db_id="toy")

    assert result.selected_groups == []
    assert any(
        "source_antipattern_output_subset_not_present" in audit.gate_reasons
        for audit in result.candidates
    )


def test_runtime_applicability_rejects_unresolved_branch_selection_contract() -> None:
    family = build_family_from_groups([_singleton("206"), _singleton("249")])
    family.runtime_usable = True
    family.trigger_contract = family.trigger_contract.model_copy(
        update={
            "required_signals": ["pred.output_role=identifier"],
            "variant_required_signal_sets": [],
            "decisive_pred_signals": ["pred.output_role=identifier"],
            "action_contract": {
                **family.trigger_contract.action_contract,
                "program_envelope": {
                    "source_antipatterns": [],
                    "required_role_slots": [],
                    "negative_guards": [],
                    "branch_selection_contract": {
                        "requires_current_variant_binding": True,
                        "unresolved_variation_axes": ["slot_signature"],
                    },
                },
            },
        }
    )
    library = LibraryStateV2(db_id="toy", experience_families=[family])
    case_view = RuntimeCaseView(
        db_id="toy",
        case_id="single-output",
        question="Which endpoint should be returned?",
        question_contract=QuestionContract(),
        pred_manifestation=PredManifestation(
            top1_sql="SELECT a.id FROM a",
            columns=["a.id"],
        ),
        case_signal_view=CaseSignalView(
            db_id="toy",
            pred_sql_view=PredSqlSignalView(
                select_arity=1,
                output_shape_current={"arity": 1, "roles": ["identifier"]},
                select_role_profile=["identifier"],
            ),
        ),
        local_schema_view=LocalSchemaView(
            db_id="toy",
            tables=["a"],
            columns_by_table={"a": ["id"]},
        ),
    )

    result = trigger_memory_objects(library=library, case_view=case_view, db_id="toy")

    assert result.selected_groups == []
    assert any(
        "branch_selection_contract_unresolved:slot_signature" in audit.gate_reasons
        for audit in result.candidates
    )


def test_repair_program_dependency_steps_are_not_case_id_filtered() -> None:
    from method.EEA.rulebook.common.runtime.action_compiler import _repair_program_for_canonical_op

    repair_program = [
        {
            "step_id": "dep1",
            "op": "ORDER_BY_APPLY",
            "is_dependency": True,
            "supporting_case_ids": ["206"],
            "arguments": {"policy_supporting_case_ids": ["206"]},
        },
        {
            "step_id": "core1",
            "op": "SELECT_REPLACE_SLOT",
            "arguments": {"canonical_op_id": "op1"},
        },
    ]

    result = _repair_program_for_canonical_op(
        repair_program=repair_program,
        canonical_op={"op_id": "op1", "op_type": "SELECT_REPLACE_SLOT"},
        member_variants=[{"supporting_case_ids": ["249"]}],
    )

    assert [step["step_id"] for step in result] == ["dep1", "core1"]


def test_synthesizer_can_fallback_to_invariant_envelope_when_bucket_matching_is_unavailable(
    monkeypatch,
) -> None:
    from method.EEA.rulebook.common.learning import shared_program_synthesizer as shared_program_synthesizer_module

    def _scope_relation_singleton(case_id: str) -> GroupSummary:
        insight = _repair_insight("replace predicate scope preserving output contract")
        effect_signature = _repair_effect_signature(
            case_id=case_id,
            axis="predicate_scope_delta",
            kind="predicate_move",
            primitive="MOVE_CONDITION",
        )
        ir = CanonicalRepairIR(
            db_id="toy",
            case_id=case_id,
            program_ops=[
                CanonicalRepairOp(
                    op_id=f"{case_id}:scope_relation",
                    op_type="WHERE_REPLACE_CONDITION",
                    locus="WHERE",
                    arguments={
                        "operation_signature": {
                            "step_op": "WHERE_REPLACE_CONDITION",
                            "locus": "WHERE",
                            "required": True,
                            "is_dependency": False,
                            "slot_signature": [{"name": "predicate_ref", "kind": "predicate"}],
                            "role_delta": {
                                "arity_direction": "same",
                                "source_output_roles": ["identifier"],
                                "target_output_roles": ["identifier"],
                            },
                            "relation_delta": {
                                "added_relation_equalities": [
                                    "bond.atom_id=connected.atom_id"
                                ],
                            },
                            "predicate_scope_delta": {
                                "removed_source_predicates": ["bond.bond_id = connected.bond_id"],
                                "added_target_predicates": ["bond.atom_id = connected.atom_id"],
                                "possible_scope_move": True,
                            },
                            "grain_delta": {
                                "source_grain": "pair_rows",
                                "target_grain": "entity_rows",
                                "grain_changed": True,
                                "target_has_aggregate": False,
                                "target_has_distinct": False,
                            },
                        },
                        "target_invariants": [
                            "target_added_relation_equality=bond.atom_id=connected.atom_id",
                            "target_output_grain=entity_rows",
                        ],
                        "repair_effect_signature": effect_signature.model_dump(mode="json"),
                        "repair_insight_signature": insight.model_dump(mode="json"),
                    },
                    invariants=[
                        "target_added_relation_equality=bond.atom_id=connected.atom_id",
                        "target_output_grain=entity_rows",
                        "grain_changed",
                    ],
                    supporting_case_ids=[case_id],
                )
            ],
            core_ops=[
                {
                    "op_id": f"{case_id}:scope_relation",
                    "op_type": "WHERE_REPLACE_CONDITION",
                    "locus": "WHERE",
                    "required": True,
                    "is_dependency": False,
                    "origin": "case_extracted",
                    "extraction_source": "llm_explicit",
                    "supporting_case_ids": [case_id],
                }
            ],
            target_invariants=[
                "target_added_relation_equality=bond.atom_id=connected.atom_id",
                "target_output_grain=entity_rows",
            ],
            invariants=[
                "target_added_relation_equality=bond.atom_id=connected.atom_id",
                "target_output_grain=entity_rows",
                "grain_changed",
            ],
            repair_effect_signature=effect_signature,
            repair_insight_signature=insight,
        )
        program = singleton_program_from_ir(ir)
        return GroupSummary(
            group_id=f"grp-sing-toy-{case_id}",
            group_type=GroupType.SINGLETON,
            db_id="toy",
            case_ids=[case_id],
            support=1,
            confidence=Confidence.LOW,
            runtime_usable=True,
            status=GroupStatus.ACTIVE,
            core_interface=CoreInterface(
                question_family_tags=[],
                pred_family_tags=[],
                repair_goal="reroute predicate scope and relation equality",
                repair_skeleton_prototype=_skeleton(),
            ),
            instantiation_program=InstantiationProgram(
                shared=True,
                repair_program=[],
                synthesized_program=program,
                program_coverage=coverage_for_singleton_program(program),
            ),
            trigger_signature=TriggerSignature(),
            formation_signals={"canonical_repair_ir": ir.model_dump(mode="json")},
        )

    monkeypatch.setattr(shared_program_synthesizer_module, "_ops_by_bucket", lambda _ir: {})
    monkeypatch.setattr(shared_program_synthesizer_module, "_ops_by_generalized_bucket", lambda _ir: {})

    result = synthesize_shared_program(
        [
            _scope_relation_singleton("198"),
            _scope_relation_singleton("207"),
        ]
    )

    assert result.program is not None
    assert result.coverage.compile_coverage == 1.0
    assert result.program.program_envelope is not None
    assert {
        row["kind"] for row in result.program.program_envelope.source_antipatterns
    } >= {"relation_delta", "predicate_scope_delta", "grain_delta"}
    assert "MOVE_CONDITION" in (
        result.program.program_envelope.action_envelope.get("allowed_primitives") or []
    )


def test_runtime_repair_brief_is_rendered_from_actions() -> None:
    from method.EEA.rulebook.common.runtime.runtime import _render_action_repair_brief

    brief = _render_action_repair_brief(
        [
            Action(
                action_id="a1",
                source_group_id="g1",
                source_group_type=GroupType.FAMILY,
                primitive=ActionPrimitive.REPLACE_SELECT_SLOT,
                arguments={
                    "from_exprs": ["a.id"],
                    "target_columns": [{"target_table": "b", "target_column": "name"}],
                },
                rationale_short="replace current output",
            )
        ]
    )

    assert "Replace the current projection" in brief
    assert "a.id" in brief
    assert "target_columns" in brief
