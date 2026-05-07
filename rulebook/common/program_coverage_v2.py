"""Compiler coverage validation for canonical repair programs."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from .data_structures_v2 import (
    CanonicalRepairProgram,
    GroupSummary,
    ProgramCoverage,
    RuntimeCaseView,
)
from .shared_program_synthesizer_v2 import canonical_op_lowering_family


def _payload(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return dict(getattr(value, "__dict__", {}) or {})


def _output_shape_delta(op: Any) -> Dict[str, Any]:
    """Read output shape from all canonical program forms.

    Singleton programs keep case-local arguments directly on the op. Shared
    family programs wrap the common fields under ``shared_arguments`` and keep
    source examples in ``member_argument_variants``. The validator must accept
    both layouts; otherwise a valid singleton can be rejected before replay.
    """
    args = _payload(getattr(op, "arguments", None))
    direct_shape = _payload(args.get("output_shape_delta"))
    if direct_shape:
        return direct_shape

    shared = _payload(args.get("shared_arguments"))
    shared_shape = _payload(shared.get("output_shape_delta"))
    if shared_shape:
        return shared_shape

    for variant in args.get("member_argument_variants") or []:
        variant_shape = _payload(_payload(variant).get("output_shape_delta"))
        if variant_shape:
            return variant_shape

    skeleton_shape = _payload(_payload(args.get("skeleton")).get("output_shape_delta"))
    if skeleton_shape:
        return skeleton_shape
    return {}


def _iter_memory_schema_refs(group: GroupSummary) -> List[Dict[str, Any]]:
    """Extract concrete target schema hints embedded in a memory contract."""
    rows: List[Dict[str, Any]] = []

    def add_ref(ref: Any) -> None:
        payload = _payload(ref)
        table = str(payload.get("table") or "").strip()
        column = str(payload.get("column") or "").strip()
        if table and column:
            rows.append({"table": table, "column": column})

    def add_relation(relation: Any) -> None:
        payload = _payload(relation)
        for side in ("left", "right"):
            add_ref(payload.get(side))

    program = getattr(group.instantiation_program, "synthesized_program", None)
    if program is not None:
        for op in program.ops or []:
            args = _payload(getattr(op, "arguments", None))
            for ref in getattr(op, "role_refs", []) or []:
                add_ref(ref)
            for key in ("canonical_refs", "target_output_refs"):
                for ref in args.get(key) or []:
                    add_ref(ref)
            shared = _payload(args.get("shared_arguments"))
            for ref in shared.get("target_output_refs") or []:
                add_ref(ref)
            for relation in shared.get("target_equality_relations") or []:
                add_relation(relation)
            for variant in args.get("member_argument_variants") or []:
                variant_payload = _payload(variant)
                for ref in variant_payload.get("target_output_refs") or []:
                    add_ref(ref)
                for relation in variant_payload.get("target_equality_relations") or []:
                    add_relation(relation)
    return rows


def _program_bundle_ids(program: Optional[CanonicalRepairProgram]) -> set[str]:
    payload = _payload(getattr(program, "program_envelope", None))
    action_envelope = _payload(payload.get("action_envelope"))
    bundle_ids = {
        str(_payload(bundle).get("bundle_id") or "")
        for bundle in (action_envelope.get("bundles") or [])
        if str(_payload(bundle).get("bundle_id") or "")
    }
    if bundle_ids:
        return bundle_ids
    return {
        str(op.op_id)
        for op in (getattr(program, "ops", None) or [])
        if str(getattr(op, "op_id", "") or "")
    }


def _case_view_with_memory_schema(
    case_view: RuntimeCaseView,
    group: GroupSummary,
) -> RuntimeCaseView:
    """Return a case view augmented with memory-provided target refs.

    Runtime rebuilds ``LocalSchemaView`` with memory tables after trigger
    selection. Unit/offline validation often only has the pre-trigger saved
    case view, so this mirrors the same contract at column-ref granularity
    without consulting gold SQL or database-specific rules.
    """
    schema = case_view.local_schema_view.model_copy(deep=True)
    tables = list(schema.tables or [])
    columns_by_table = {
        str(table): list(columns or [])
        for table, columns in (schema.columns_by_table or {}).items()
    }
    seen_tables = {table.lower() for table in tables}
    for ref in _iter_memory_schema_refs(group):
        table = str(ref.get("table") or "").strip()
        column = str(ref.get("column") or "").strip()
        if not table or not column:
            continue
        if table.lower() not in seen_tables:
            tables.append(table)
            seen_tables.add(table.lower())
        cols = columns_by_table.setdefault(table, [])
        if not any(existing.lower() == column.lower() for existing in cols):
            cols.append(column)
    augmented = schema.model_copy(
        update={
            "tables": tables,
            "columns_by_table": columns_by_table,
        }
    )
    return case_view.model_copy(update={"local_schema_view": augmented})


class CompilerCoverageValidator:
    """Deterministically decide whether a synthesized program is compiler-usable."""

    def validate_group(
        self,
        group: GroupSummary,
        *,
        member_case_views: Optional[Mapping[str, RuntimeCaseView]] = None,
    ) -> ProgramCoverage:
        program = getattr(group.instantiation_program, "synthesized_program", None)
        existing = getattr(group.instantiation_program, "program_coverage", None)
        case_ids = [str(case_id) for case_id in group.case_ids]
        if program is None:
            return ProgramCoverage(
                total_cases=len(case_ids),
                uncovered_case_ids=case_ids,
                compile_coverage=0.0,
                static_program_coverage=0.0,
                runtime_binding_coverage=0.0,
                member_candidate_coverage=0.0,
                blockers=["missing_synthesized_program"],
                static_blockers=["missing_synthesized_program"],
                runtime_binding_blockers={},
                per_case={
                    case_id: {"covered": False, "blockers": ["missing_synthesized_program"]}
                    for case_id in case_ids
                },
                compile_success_by_member={case_id: False for case_id in case_ids},
                failed_members=case_ids,
                failure_reasons={
                    case_id: "missing_synthesized_program" for case_id in case_ids
                },
                core_op_coverage=0.0,
            )
        coverage = self.validate_program(program=program, case_ids=case_ids)
        if member_case_views:
            coverage = self.validate_runtime_bindings(
                group=group,
                member_case_views=member_case_views,
                base_coverage=coverage,
            )
        if existing is None:
            return coverage
        merged_blockers = sorted(set(existing.blockers or []) | set(coverage.blockers or []))
        if merged_blockers:
            return coverage.model_copy(update={"blockers": merged_blockers})
        return coverage

    def validate_program(
        self,
        *,
        program: CanonicalRepairProgram,
        case_ids: Optional[List[str]] = None,
    ) -> ProgramCoverage:
        ids = case_ids or [str(case_id) for case_id in program.synthesized_from_case_ids]
        blockers: List[str] = []
        if not program.ops:
            blockers.append("empty_synthesized_program")
        unsupported = sorted(
            {
                str(op.op_type)
                for op in program.ops
                if not canonical_op_lowering_family(op.op_type, op.locus)
            }
        )
        blockers.extend(f"unsupported_canonical_op:{op}" for op in unsupported)
        for op in program.ops:
            args = _payload(getattr(op, "arguments", None))
            signature = _payload(args.get("operation_signature")) or _payload(
                args.get("shared_signature")
            )
            if not op.locus:
                blockers.append(f"missing_locus:{op.op_id}")
            if not signature:
                blockers.append(f"missing_operation_signature:{op.op_id}")
            lowering_family = canonical_op_lowering_family(op.op_type, op.locus)
            if lowering_family in {"select_add", "select_drop"}:
                shape = _output_shape_delta(op)
                if not shape.get("arity_direction"):
                    blockers.append(f"missing_output_shape_direction:{op.op_id}")
            if lowering_family in {"join_bridge", "where_side_edit"}:
                if not op.invariants:
                    blockers.append(f"missing_structural_invariants:{op.op_id}")
                target_invariants = (
                    list(getattr(program, "target_invariants", None) or [])
                    + list(args.get("target_invariants") or [])
                    + list(_payload(args.get("shared_arguments")).get("target_invariants") or [])
                )
                if not target_invariants:
                    blockers.append(f"missing_target_invariants:{op.op_id}")

        covered = [] if blockers else ids
        static_coverage = (len(covered) / len(ids) if ids else 0.0)
        return ProgramCoverage(
            total_cases=len(ids),
            covered_case_ids=covered,
            uncovered_case_ids=ids if blockers else [],
            compile_coverage=static_coverage,
            static_program_coverage=static_coverage,
            runtime_binding_coverage=0.0,
            member_candidate_coverage=static_coverage,
            blockers=sorted(set(blockers)),
            static_blockers=sorted(set(blockers)),
            runtime_binding_blockers={},
            per_case={
                case_id: {
                    "covered": not blockers,
                    "blockers": sorted(set(blockers)),
                }
                for case_id in ids
            },
            compile_success_by_member={case_id: not blockers for case_id in ids},
            failed_members=ids if blockers else [],
            failure_reasons={
                case_id: ";".join(sorted(set(blockers))) for case_id in ids if blockers
            },
            mean_action_count=float(len(program.ops or [])) if not blockers else 0.0,
            core_op_coverage=0.0 if blockers else 1.0,
        )

    def validate_runtime_bindings(
        self,
        *,
        group: GroupSummary,
        member_case_views: Mapping[str, RuntimeCaseView],
        base_coverage: Optional[ProgramCoverage] = None,
    ) -> ProgramCoverage:
        """Validate that each member can actually produce compiler candidates.

        Static coverage checks only that a canonical op has a lowering family.
        This method runs the code-side ActionCompiler enumerator against each
        member RuntimeCaseView, so unresolved slot/path/schema bindings are
        surfaced before replay promotion.
        """
        program = getattr(group.instantiation_program, "synthesized_program", None)
        case_ids = [str(case_id) for case_id in group.case_ids]
        if program is None:
            return self.validate_group(group)
        base = base_coverage or self.validate_program(program=program, case_ids=case_ids)
        if base.blockers:
            return base

        from .action_compiler_v2 import enumerate_candidates  # Local import avoids a module cycle.

        covered: List[str] = []
        failed: List[str] = []
        per_case: Dict[str, Any] = {}
        failure_reasons: Dict[str, str] = {}
        action_counts: List[int] = []
        required_op_ids = _program_bundle_ids(program)
        for case_id in case_ids:
            case_view = member_case_views.get(case_id)
            if case_view is None:
                failed.append(case_id)
                failure_reasons[case_id] = "missing_runtime_case_view"
                per_case[case_id] = {
                    "covered": False,
                    "reason": "missing_runtime_case_view",
                }
                continue
            case_view = _case_view_with_memory_schema(case_view, group)
            try:
                candidate_sets, diagnostics = enumerate_candidates(
                    case_view=case_view,
                    memory_objects=[group],
                )
            except Exception as exc:  # pragma: no cover - defensive validation path.
                failed.append(case_id)
                reason = f"candidate_enumeration_exception:{type(exc).__name__}"
                failure_reasons[case_id] = reason
                per_case[case_id] = {"covered": False, "reason": reason}
                continue
            candidates = [
                candidate
                for candidate_set in candidate_sets or []
                for candidate in (candidate_set.candidates or [])
                if str(candidate.source_group_id or "") == str(group.group_id)
            ]
            candidate_op_ids = {
                str(_payload(candidate.arguments).get("bundle_id") or "")
                or str(_payload(candidate.arguments).get("canonical_op_id") or "")
                for candidate in candidates
                if (
                    str(_payload(candidate.arguments).get("bundle_id") or "")
                    or str(_payload(candidate.arguments).get("canonical_op_id") or "")
                )
            }
            missing_ops = sorted(required_op_ids - candidate_op_ids)
            empty_reasons = [
                {
                    "primitive": str(getattr(candidate_set.primitive, "value", candidate_set.primitive)),
                    "empty_reason": candidate_set.empty_reason,
                }
                for candidate_set in candidate_sets or []
                if candidate_set.empty_reason
            ]
            if not candidates:
                failed.append(case_id)
                reason = "compiler_no_candidates"
                failure_reasons[case_id] = reason
                per_case[case_id] = {
                    "covered": False,
                    "reason": reason,
                    "empty_reasons": empty_reasons,
                    "schema_diagnostics": _payload(diagnostics),
                }
                continue
            if missing_ops:
                failed.append(case_id)
                reason = "canonical_ops_unbound:" + ",".join(missing_ops)
                failure_reasons[case_id] = reason
                per_case[case_id] = {
                    "covered": False,
                    "reason": reason,
                    "candidate_count": len(candidates),
                    "candidate_op_ids": sorted(candidate_op_ids),
                    "empty_reasons": empty_reasons,
                    "schema_diagnostics": _payload(diagnostics),
                }
                continue
            covered.append(case_id)
            action_counts.append(len(required_op_ids) or len(candidates))
            per_case[case_id] = {
                "covered": True,
                "candidate_count": len(candidates),
                "candidate_op_ids": sorted(candidate_op_ids),
                "empty_reasons": empty_reasons,
            }
        blockers = [] if not failed else ["member_candidate_binding_failed"]
        runtime_coverage = (len(covered) / len(case_ids) if case_ids else 0.0)
        return ProgramCoverage(
            total_cases=len(case_ids),
            covered_case_ids=covered,
            uncovered_case_ids=failed,
            compile_coverage=runtime_coverage,
            static_program_coverage=float(base.static_program_coverage or base.compile_coverage or 0.0),
            runtime_binding_coverage=runtime_coverage,
            member_candidate_coverage=runtime_coverage,
            blockers=blockers,
            static_blockers=list(base.static_blockers or base.blockers or []),
            runtime_binding_blockers=dict(failure_reasons),
            per_case=per_case,
            compile_success_by_member={
                case_id: case_id in set(covered) for case_id in case_ids
            },
            failed_members=failed,
            failure_reasons=failure_reasons,
            mean_action_count=(
                sum(action_counts) / len(action_counts) if action_counts else 0.0
            ),
            core_op_coverage=(
                1.0
                if not failed and required_op_ids
                else 0.0
                if required_op_ids
                else base.core_op_coverage
            ),
        )


def validate_group_program_coverage(
    group: GroupSummary,
    *,
    member_case_views: Optional[Mapping[str, RuntimeCaseView]] = None,
) -> ProgramCoverage:
    return CompilerCoverageValidator().validate_group(
        group,
        member_case_views=member_case_views,
    )


__all__ = ["CompilerCoverageValidator", "validate_group_program_coverage"]
