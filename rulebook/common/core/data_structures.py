"""Pydantic schema for the Minimal Repair Interface v2 stack.

Layout follows doc/plan.md §4-11. This module defines:

1.  `LocalSchemaView` / `LocalSchemaViewDiagnostics` ——按需 schema 视图
2.  `QuestionContract` / `PredManifestation` / `RuntimeCaseView` ——runtime 可见信号
3.  `CaseAudit` ——wrong case 审计产物
4.  `RepairSkeleton` / `InstantiationSlot` / `BranchRule` / `Guardrail` / `ErrorInstanceV2`
5.  `Action` / `ActionCandidates` ——Compiler IO（约束生成契约）
6.  `ActionRealizationTrace` ——bounded autonomy rewrite 回填
7.  `C0Snapshot` / `UpstreamFingerprint` ——replay 基础设施
8.  `GroupSummary` / `LibraryStateV2` ——三层记忆对象容器
9.  `ReplayMetrics` / `PromotionTestResult` / `ConflictScanReport`

All IDs are strings (case_id / group_id / pattern_id / action_id)。
All lists default to [] via `Field(default_factory=list)`。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from method.EEA.rulebook.common.core.vocabulary import (
    ActionPrimitive,
    AnswerSlotType,
    Confidence,
    EditScope,
    GrainType,
    GroupStatus,
    GroupType,
    Locus,
    OpFamily,
    OpType,
    OutputContract,
    PredManifestationType,
    QuestionTargetRole,
    RiskLevel,
    RouteCue,
    TargetFamily,
)


# =============================================================================
# 1. LocalSchemaView （doc/plan.md §4.2）
# =============================================================================


class PkFkEdge(BaseModel):
    """单条 PK/FK 边。source/target 是 `table.column` 形式。"""

    source: str
    target: str
    kind: str = "fk"  # fk / implied / weak


class ColumnHint(BaseModel):
    """来自列说明/metadata 的局部语义提示。"""

    table: str
    column: str
    role_family: Optional[str] = None  # identifier / name / metric / temporal / ...
    note: Optional[str] = None


class SchemaSource(BaseModel):
    """LocalSchemaView 的表来自哪——用于追溯和 diagnostic。"""

    from_pred: List[str] = Field(default_factory=list)
    from_question: List[str] = Field(default_factory=list)
    from_memory: List[str] = Field(default_factory=list)
    two_hop_extension: List[str] = Field(default_factory=list)


class LocalSchemaView(BaseModel):
    """Per-case 动态构造的局部 schema 视图。"""

    db_id: str
    tables: List[str] = Field(default_factory=list)
    columns_by_table: Dict[str, List[str]] = Field(default_factory=dict)
    pk_fk_edges: List[PkFkEdge] = Field(default_factory=list)
    semantic_hints: List[ColumnHint] = Field(default_factory=list)
    source: SchemaSource = Field(default_factory=SchemaSource)


class LocalSchemaViewDiagnostics(BaseModel):
    """Codex Patch 1：schema 召回失败的独立诊断通道。

    这些 miss 不进 memory demotion 信号——它们是 schema view 问题，不是
    pattern 问题。避免范畴错误污染 demotion 决策。
    """

    missing_bridge_paths: List[str] = Field(default_factory=list)
    missing_column_candidates: List[str] = Field(default_factory=list)
    missing_role_family_matches: List[str] = Field(default_factory=list)
    two_hop_extension_denied: List[str] = Field(default_factory=list)
    notes: Optional[str] = None


# =============================================================================
# 2. RuntimeCaseView （doc/plan.md §5）
# =============================================================================


class QuestionContract(BaseModel):
    """题面侧的"答案合同"——想要什么实体、什么槽位、什么粒度、什么运算。"""

    target_entity_roles: List[QuestionTargetRole] = Field(default_factory=list)
    answer_slot_types: List[AnswerSlotType] = Field(default_factory=list)
    grain: Optional[GrainType] = None
    operators: List[OpType] = Field(default_factory=list)
    route_cues: List[RouteCue] = Field(default_factory=list)
    specific_entity_mentions: List[str] = Field(default_factory=list)
    """具体命名实体（"Maya Mclean"、"October Meeting"）——slot filling 值，不作 tag。"""

    summary: Optional[str] = None


class PredManifestation(BaseModel):
    """当前 pred_sql 的抽象症状描述。不含具体列/表名的 tag；具体名字在 tables/columns。"""

    top1_sql: str
    tables: List[str] = Field(default_factory=list)
    columns: List[str] = Field(default_factory=list)
    select_shape: Optional[str] = None
    group_order_shape: Optional[str] = None
    manifestation_types: List[PredManifestationType] = Field(default_factory=list)
    structure_flags: Dict[str, bool] = Field(default_factory=dict)
    """直接并入 DeltaFlags 结构维度或其子集（来自 common/structure_delta.DeltaFlags）。"""

    summary: Optional[str] = None


class CandidateSetSummary(BaseModel):
    """C0 候选集的摘要（不存原始 SQL，避免体积膨胀）。"""

    size: int = 0
    top1_hash: Optional[str] = None
    beam_present: bool = False
    candidate_summaries: List[Dict[str, Any]] = Field(default_factory=list)
    """Compact answer-blind C0/top-k facts: rank, hash, tables, output shape."""


class QuestionSignalView(BaseModel):
    """Answer-blind question-side signals used for audit and future trigger design."""

    answer_slot_cues: List[str] = Field(default_factory=list)
    operation_cues: List[str] = Field(default_factory=list)
    grain_cue: Optional[str] = None
    route_cues: List[str] = Field(default_factory=list)
    named_mentions: List[str] = Field(default_factory=list)
    schema_mention_hits: List[str] = Field(default_factory=list)
    negative_question_cues: List[str] = Field(default_factory=list)


class PredSqlSignalView(BaseModel):
    """Answer-blind pred-SQL-side signals.

    This intentionally describes shape and route generically instead of naming a
    specific known error family.
    """

    select_arity: Optional[int] = None
    select_items: List[str] = Field(default_factory=list)
    select_role_profile: List[str] = Field(default_factory=list)
    output_shape_current: Dict[str, Any] = Field(default_factory=dict)
    tables_used: List[str] = Field(default_factory=list)
    join_graph: List[Dict[str, Any]] = Field(default_factory=list)
    predicate_profile: Dict[str, Any] = Field(default_factory=dict)
    aggregate_profile: Dict[str, Any] = Field(default_factory=dict)
    group_order_profile: Dict[str, Any] = Field(default_factory=dict)
    runtime_structure_flags: Dict[str, bool] = Field(default_factory=dict)


class SchemaSignalView(BaseModel):
    """Schema-side signals from the local schema view."""

    local_tables: List[str] = Field(default_factory=list)
    columns_by_table: Dict[str, List[str]] = Field(default_factory=dict)
    column_role_hints: Dict[str, str] = Field(default_factory=dict)
    relation_hints: List[Dict[str, Any]] = Field(default_factory=list)
    schema_tags: List[str] = Field(default_factory=list)
    schema_recall_diagnostics: Dict[str, Any] = Field(default_factory=dict)


class CaseSignalView(BaseModel):
    """Runtime-visible signal bundle.

    This is produced without gold SQL. Runtime trigger may read this answer-blind
    view; offline-only delta fields remain separate in ``DeltaSignature``.
    """

    schema_version: str = "signal-schema-v0"
    db_id: str
    case_id: str = ""
    question_view: QuestionSignalView = Field(default_factory=QuestionSignalView)
    pred_sql_view: PredSqlSignalView = Field(default_factory=PredSqlSignalView)
    schema_view: SchemaSignalView = Field(default_factory=SchemaSignalView)
    runtime_signature: Dict[str, Any] = Field(default_factory=dict)


class OutputShapeDelta(BaseModel):
    """Offline pred-vs-gold output shape delta."""

    current_arity: Optional[int] = None
    target_arity: Optional[int] = None
    arity_delta: Optional[int] = None
    """target_arity - current_arity. Numeric, not an enum such as 1TO3."""
    arity_direction: Optional[str] = None
    """increase / decrease / same / unknown."""
    current_roles: List[str] = Field(default_factory=list)
    target_roles: List[str] = Field(default_factory=list)
    current_grain: Optional[str] = None
    target_grain: Optional[str] = None
    operation: Optional[str] = None


class DeltaSignature(BaseModel):
    """Offline answer-aware delta bundle for distillation and audit.

    This is not runtime-visible because it can use gold SQL and execution
    comparison.
    """

    schema_version: str = "signal-schema-v0"
    db_id: str
    case_id: str = ""
    output_shape_delta: Optional[OutputShapeDelta] = None
    structural_delta: Dict[str, Any] = Field(default_factory=dict)
    semantic_delta: Dict[str, Any] = Field(default_factory=dict)
    execution_delta: Dict[str, Any] = Field(default_factory=dict)
    repair_delta: Dict[str, Any] = Field(default_factory=dict)
    delta_confidence: Dict[str, Any] = Field(default_factory=dict)


class SignalAtom(BaseModel):
    """One auditable signal with provenance and runtime-visibility metadata."""

    name: str
    channel: str
    """question / pred / schema / execution / llm / manual / replay."""
    source: str = "code"
    """code / llm / manual / replay."""
    role: str = "descriptive"
    """decisive / descriptive / negative / diagnostic."""
    runtime_visible: bool = True
    confidence: float = 1.0
    evidence_span: Optional[str] = None
    notes: Optional[str] = None


class CaseSignalBundle(BaseModel):
    """Audit-only signal provenance bundle.

    Runtime may use the answer-blind signals, but offline-only/execution-only
    entries are explicitly marked and must not become trigger keys.
    """

    schema_version: str = "signal-bundle-v0"
    db_id: str
    case_id: str = ""
    question_signals: List[SignalAtom] = Field(default_factory=list)
    pred_signals: List[SignalAtom] = Field(default_factory=list)
    schema_signals: List[SignalAtom] = Field(default_factory=list)
    execution_only_signals: List[SignalAtom] = Field(default_factory=list)
    filtered_runtime_pred_tags: List[str] = Field(default_factory=list)
    filter_reasons: Dict[str, str] = Field(default_factory=dict)
    stable_signatures: Dict[str, Any] = Field(default_factory=dict)


class RuntimeCaseView(BaseModel):
    """Runtime 可见信号的统一收束。trigger / compiler / rewrite 只能读此对象。"""

    db_id: str
    case_id: str
    question: str
    evidence: Optional[str] = None

    question_contract: QuestionContract
    pred_manifestation: PredManifestation
    candidate_set_summary: CandidateSetSummary = Field(default_factory=CandidateSetSummary)
    local_schema_view: LocalSchemaView
    case_signal_view: Optional[CaseSignalView] = None
    """Phase A audit-only signal bundle. Existing trigger/compile/rewrite paths ignore it."""
    case_signal_bundle: Optional[CaseSignalBundle] = None
    """Phase A provenance bundle. Runtime behavior ignores it unless explicitly audited."""


# =============================================================================
# 3. CaseAudit （doc/plan.md §6）
# =============================================================================


class ExecutionComparison(BaseModel):
    """结构化 pred vs gold 执行对比。

    用途：
    - Auditor 借此做"执行等价 → 主错在 SELECT"的判断（R-equivalence 规则）
    - Extractor 独立读取此字段，不必只依赖 Auditor 的 minimal_fix 自然语言摘要
      （解决执行证据在压成字符串后信息丢失的问题）
    - runtime 层禁止使用（answer-blind 约束）——仅限离线 audit / distill 阶段
    """

    pred_row_count: Optional[int] = None
    gold_row_count: Optional[int] = None
    row_sets_equivalent: Optional[bool] = None
    """忽略行顺序但保留列顺序、重复行和基础值类型后的结果是否等价。None = 无法判断。"""
    column_count_pred: Optional[int] = None
    column_count_gold: Optional[int] = None
    pred_error: Optional[str] = None
    gold_error: Optional[str] = None
    projected_dtype_changed: Optional[bool] = None
    """第 i 列 pred 是 text/name-like 而 gold 是 integer/identifier-like 之类的差异。"""
    comparison_unknown_reason: Optional[str] = None
    """row_sets_equivalent=None 时的原因，如 truncated / timeout / execution_error。"""
    truncated_sides: List[str] = Field(default_factory=list)
    """因 row_sample_limit 截断的一侧：pred / gold。"""
    sampled_pred_row_count: Optional[int] = None
    sampled_gold_row_count: Optional[int] = None
    row_sample_limit: Optional[int] = None
    notes: Optional[str] = None


class CaseAudit(BaseModel):
    """对 wrong case 的最小可验证修复审计。只在 enhanced 仍错时生成。

    `candidate_fix_sql` 是 auditor 提出的候选 SQL，不代表已经验证正确。
    `validated_sql` 仅在 code side 跑过 execution comparison 后填写。
    `minimal_fix` 不是整条 rewrite，是相对 pred 的最小 diff。
    """

    db_id: str
    case_id: str
    question: str
    evidence: Optional[str] = None
    pred_sql: str
    gold_sql: str
    execution_trace: Optional[str] = None
    """保留原 free-form trace 字段（若 caller 想额外注入日志）。"""
    execution_comparison: Optional[ExecutionComparison] = None
    """结构化 execution 对比——落盘到 CaseAudit，供 Extractor 独立使用。"""
    delta_signature: Optional[DeltaSignature] = None
    """Phase A offline signal bundle. Not available to answer-blind runtime."""
    final_error_reason: str
    minimal_fix: str
    candidate_fix_sql: Optional[str] = None
    """LLM-proposed SQL after directly applying minimal_fix. Not execution-validated by default."""
    minimal_patch_ops: List[Dict[str, Any]] = Field(default_factory=list)
    """Structured minimal patch summary emitted by the auditor for downstream normalization."""
    effect_axis_hint: Optional[str] = None
    """Offline hint from the auditor about the likely contrastive repair axis."""
    secondary_differences: List[str] = Field(default_factory=list)
    validated_sql: Optional[str] = None
    error_locus_hint: Optional[Locus] = None
    """Auditor 给的初步 locus 归类，后续 ErrorInstance 可修正。"""
    confidence: Confidence = Confidence.LOW


# =============================================================================
# 4. ErrorInstanceV2 （doc/plan.md §7）
# =============================================================================


class RepairSkeletonStructural(BaseModel):
    """修复骨架的结构层——四元组。"""

    locus: Locus
    op_family: OpFamily
    target_family: TargetFamily
    output_contract: OutputContract = OutputContract.UNCHANGED
    """Legacy exact contract. Do not use as generic N-to-M pattern boundary."""
    output_shape_delta: Optional[OutputShapeDelta] = None
    """Structured SELECT shape delta used by formation / compiler decisions."""
    legacy_signature: Optional[str] = None
    """旧 repair_signature_dict_from_sql 的输出，保留作 compatibility / veto 信号。"""


class RepairSkeletonSemantic(BaseModel):
    """修复骨架的语义层。"""

    intent: str
    """一句话概括为什么这样修（不是 why 错，是 repair goal）。"""
    family_hint: Optional[str] = None
    """修复所属的接口家族提示（output_contract / fact_route / grain / ...）。"""
    notes: Optional[str] = None


class RepairSkeleton(BaseModel):
    structural: RepairSkeletonStructural
    semantic: RepairSkeletonSemantic


class InstantiationSlot(BaseModel):
    """运行时要填的槽位。required/optional 由 Codex Patch 4 明确。"""

    name: str
    kind: str
    """slot 类型（column / table / value / grain_key / edit_scope / ...）。"""
    required: bool = True
    """Codex Patch 4：required vs optional 明确区分。optional slot 缺失不阻塞 action 产出。"""
    allowed_role_families: List[str] = Field(default_factory=list)
    """candidate 枚举用：此 slot 允许哪些 role_family 的列/表/值。"""
    description: Optional[str] = None


class BranchRule(BaseModel):
    """分支条件。多于 RISK_HIGH_BRANCH_COUNT 时 risk_level 升 high。"""

    if_condition: str
    then_action: str
    else_action: Optional[str] = None


class Guardrail(BaseModel):
    """负向约束——什么情况下不应触发或不应做此动作。"""

    description: str
    kind: str  # negative_evidence / anti_trigger / scope_limit / ...


class RepairProgramStep(BaseModel):
    """A case-extracted repair-program step.

    The compiler may only instantiate steps that were extracted into memory.
    This object describes what was learned from an audited case; it is not a
    runtime rule template generated by the compiler.
    """

    step_id: str
    op: str
    """Mechanical edit DSL op, e.g. SELECT_REPLACE_SLOT or WHERE_ADD_CONDITION."""
    locus: str
    """SQL surface region. Kept as string for forward-compatible edit DSLs."""
    is_dependency: bool = False
    required: bool = True
    slots: List[InstantiationSlot] = Field(default_factory=list)
    guards: List[Guardrail] = Field(default_factory=list)
    arguments: Dict[str, Any] = Field(default_factory=dict)
    source_evidence: List[str] = Field(default_factory=list)
    origin: str = "case_extracted"
    """case_extracted / group_merged."""
    extraction_source: str = "llm_explicit"
    """llm_explicit / group_merged."""
    supporting_case_ids: List[str] = Field(default_factory=list)


class PossibleEffectAxis(BaseModel):
    """LLM-side hypothesis about the contrastive repair axis.

    This is advisory offline evidence. The code-side effect discovery step still
    constructs the final ``ContrastiveRepairEffect`` objects from pred-vs-target
    SQL evidence and execution comparison.
    """

    axis: str
    source_state_summary: Optional[str] = None
    target_state_summary: Optional[str] = None
    delta_summary: Optional[str] = None
    primary_likelihood: Optional[str] = None
    why: Optional[str] = None


class RepairInsightSignature(BaseModel):
    """Case-local semantic repair insight derived from one audited failure.

    ``effect axis`` is only a coarse retrieval namespace. This object is the
    case-derived explanation of the stable source->target contrast that may be
    shared across cases. It must stay schema/case-label agnostic: concrete refs
    may appear only as evidence, never as hard-coded matching rules.
    """

    schema_version: str = "repair-insight-signature-v0"
    interface_key: str = ""
    """Short normalized phrase generated from the case, e.g. "drop extra output side preserving scope"."""
    source_misread: str = ""
    """What the current S0 answers/routes/counts as if it were the contract."""
    target_preference: str = ""
    """What the audited repair shows the target contract prefers."""
    repair_interface: str = ""
    """Reusable source->target repair intent, answer-blind and DB-label agnostic."""
    binding_slots: List[Dict[str, Any]] = Field(default_factory=list)
    """Runtime-bindable slot specs: name/kind/source_or_target/role families/description."""
    preserve_invariants: List[str] = Field(default_factory=list)
    """Constraints that must survive the repair, such as scope/grain/route contracts."""
    negative_guards: List[str] = Field(default_factory=list)
    """When this insight must not trigger or be merged."""
    axis_links: List[Dict[str, Any]] = Field(default_factory=list)
    """Links to broad effect axes with concise evidence; axes remain advisory."""
    evidence: Dict[str, Any] = Field(default_factory=dict)
    """Question/pred/fix/execution evidence snippets supporting the insight."""
    confidence: str = "medium"


class CanonicalRoleRef(BaseModel):
    """A schema-agnostic reference to a SQL role.

    Concrete table/column names stay available for compiler instantiation, but
    grouping/promotion should compare the role fields instead of the concrete
    identifiers.
    """

    ref_id: str
    source: str = "sql"
    """pred_sql / target_sql / repair_step / schema."""
    table: Optional[str] = None
    column: Optional[str] = None
    expression: Optional[str] = None
    slot_index: Optional[int] = None
    sql_role: Optional[str] = None
    """output_slot / predicate_ref / join_endpoint / table_node / ..."""
    column_role: Optional[str] = None
    """identifier / name / measure / temporal / label / code / other."""
    path_role: Optional[str] = None
    """Structural path role such as output_slot_0 or join_endpoint."""
    direct_role_path: Optional[str] = None
    """Canonical direct endpoint path, e.g. direct:connected.atom_id."""
    derived_role_path: Optional[str] = None
    """Derived path routed through a direct endpoint, e.g. direct:connected.atom_id."""
    role_side_group: Optional[str] = None
    """Generic side group shared by direct/joined variants of the same answer side."""
    side_key: Optional[str] = None
    """Concrete side identity inside a role_side_group."""
    relation_role: Optional[str] = None
    """Graph-derived relation role; never derived from fixed business words."""
    evidence: Dict[str, Any] = Field(default_factory=dict)


class ContrastiveRepairEffect(BaseModel):
    """Offline-learned source→target repair effect above executable SQL edits.

    Phase 1 uses this as the main learning object. Runtime/action compiler
    failures may update triggerability/actionability, but they must not erase
    the offline effect itself.
    """

    effect_id: str
    axis: str
    source_state: Dict[str, Any] = Field(default_factory=dict)
    target_state: Dict[str, Any] = Field(default_factory=dict)
    delta: Dict[str, Any] = Field(default_factory=dict)
    role: str = "primary"
    """primary / dependency / accessory / noise."""
    triggerability: Dict[str, Any] = Field(default_factory=dict)
    actionability: Dict[str, Any] = Field(default_factory=dict)
    evidence: Dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0


class RepairEffectSignature(BaseModel):
    """Answer-blind repair effect summary above raw canonical ops."""

    effect_candidates: List[ContrastiveRepairEffect] = Field(default_factory=list)
    """Primary Phase-1 contrastive effects. Fixed fields below are compatibility summaries."""
    output_effect: Dict[str, Any] = Field(default_factory=dict)
    relation_effect: Dict[str, Any] = Field(default_factory=dict)
    predicate_scope_effect: Dict[str, Any] = Field(default_factory=dict)
    grain_effect: Dict[str, Any] = Field(default_factory=dict)
    field_binding_effect: Dict[str, Any] = Field(default_factory=dict)
    ranking_effect: Dict[str, Any] = Field(default_factory=dict)


class CanonicalRepairOp(BaseModel):
    """One normalized repair operation extracted from a concrete case."""

    op_id: str
    op_type: str
    """Case-extracted edit DSL op, e.g. SELECT_DROP_SLOT."""
    locus: str
    role_refs: List[CanonicalRoleRef] = Field(default_factory=list)
    arguments: Dict[str, Any] = Field(default_factory=dict)
    invariants: List[str] = Field(default_factory=list)
    source_step_ids: List[str] = Field(default_factory=list)
    supporting_case_ids: List[str] = Field(default_factory=list)
    confidence: float = 1.0


class CanonicalRepairIR(BaseModel):
    """Offline canonical repair program for one audited case."""

    schema_version: str = "canonical-repair-ir-v0"
    db_id: str
    case_id: str
    source_role_graph: Dict[str, Any] = Field(default_factory=dict)
    target_role_graph: Dict[str, Any] = Field(default_factory=dict)
    program_ops: List[CanonicalRepairOp] = Field(default_factory=list)
    core_ops: List[Dict[str, Any]] = Field(default_factory=list)
    """Required non-dependency ops extracted from this case's repair_program."""
    accessory_ops: List[Dict[str, Any]] = Field(default_factory=list)
    """Optional/dependency ops extracted from this case's repair_program."""
    repair_effect_signature: Optional[RepairEffectSignature] = None
    repair_insight_signature: Optional[RepairInsightSignature] = None
    """Case-local semantic insight used for shared-memory grouping and audit."""
    target_invariants: List[str] = Field(default_factory=list)
    """Answer-aware invariants learned offline from pred-vs-target SQL evidence."""
    invariants: List[str] = Field(default_factory=list)
    unresolved_variation_axes: List[str] = Field(default_factory=list)
    """Case-level canonicalization blockers. Empty for normal singletons."""
    normalizer_warnings: List[str] = Field(default_factory=list)


class ProgramEnvelope(BaseModel):
    """Shared repair interface above raw executable ops.

    Ops still describe executable edits. The envelope explains when those edits
    apply, what invariants they target, and which branch constraints the runtime
    must respect.
    """

    schema_version: str = "program-envelope-v0"
    source_antipatterns: List[Dict[str, Any]] = Field(default_factory=list)
    target_effects: List[Dict[str, Any]] = Field(default_factory=list)
    target_invariants: List[Dict[str, Any]] = Field(default_factory=list)
    allowed_action_primitives: List[str] = Field(default_factory=list)
    action_envelope: Dict[str, Any] = Field(default_factory=dict)
    lowering_branches: List[Dict[str, Any]] = Field(default_factory=list)
    runtime_branches: List[Dict[str, Any]] = Field(default_factory=list)
    """Runtime-selectable branch contracts.

    ``lowering_branches`` is kept as synthesis/audit evidence.  Runtime must use
    these normalized branch rows: each row carries answer-blind preconditions,
    bundle/action limits, replay status, and blockers.
    """
    branch_selection_contract: Dict[str, Any] = Field(default_factory=dict)
    required_role_slots: List[Dict[str, Any]] = Field(default_factory=list)
    repair_insight_signature: Dict[str, Any] = Field(default_factory=dict)
    """Shared case-local insight carried into runtime/compiler prompts."""
    negative_guards: List[Dict[str, Any]] = Field(default_factory=list)
    unresolved_variation_axes: List[str] = Field(default_factory=list)


class CanonicalRepairProgram(BaseModel):
    """A synthesized repair program shared by a family or pattern."""

    schema_version: str = "canonical-repair-program-v0"
    program_id: str
    program_type: Optional[str] = None
    """Audit-only summary derived from shared executable ops; not a trigger key."""
    ops: List[CanonicalRepairOp] = Field(default_factory=list)
    core_ops: List[Dict[str, Any]] = Field(default_factory=list)
    accessory_ops: List[Dict[str, Any]] = Field(default_factory=list)
    repair_effect_signature: Optional[RepairEffectSignature] = None
    repair_insight_signature: Optional[RepairInsightSignature] = None
    """Shared insight synthesized from member case-local insights."""
    target_invariants: List[str] = Field(default_factory=list)
    unresolved_variation_axes: List[str] = Field(default_factory=list)
    program_envelope: Optional[ProgramEnvelope] = None
    variables: Dict[str, Any] = Field(default_factory=dict)
    shared_invariants: List[str] = Field(default_factory=list)
    synthesized_from_case_ids: List[str] = Field(default_factory=list)
    compiler_supported_ops: List[str] = Field(default_factory=list)
    unsupported_ops: List[str] = Field(default_factory=list)


class ProgramCoverage(BaseModel):
    """Compiler/replay coverage metadata for a synthesized program."""

    schema_version: str = "program-coverage-v0"
    total_cases: int = 0
    covered_case_ids: List[str] = Field(default_factory=list)
    uncovered_case_ids: List[str] = Field(default_factory=list)
    compile_coverage: float = 0.0
    static_program_coverage: float = 0.0
    runtime_binding_coverage: float = 0.0
    member_candidate_coverage: float = 0.0
    blockers: List[str] = Field(default_factory=list)
    static_blockers: List[str] = Field(default_factory=list)
    runtime_binding_blockers: Dict[str, str] = Field(default_factory=dict)
    per_case: Dict[str, Any] = Field(default_factory=dict)
    compile_success_by_member: Dict[str, bool] = Field(default_factory=dict)
    failed_members: List[str] = Field(default_factory=list)
    failure_reasons: Dict[str, str] = Field(default_factory=dict)
    mean_action_count: float = 0.0
    core_op_coverage: float = 0.0


class QuestionFeatures(BaseModel):
    """ErrorInstance 上的题面特征摘要。"""

    decisive_tags: List[str] = Field(default_factory=list)
    """必须命中的强识别 tag（triggering 强信号）。"""
    descriptive_tags: List[str] = Field(default_factory=list)
    summary: Optional[str] = None


class PredSqlFeatures(BaseModel):
    """ErrorInstance 上的 pred 表现特征摘要。"""

    decisive_tags: List[str] = Field(default_factory=list)
    descriptive_tags: List[str] = Field(default_factory=list)
    summary: Optional[str] = None
    structure_flags: Dict[str, bool] = Field(default_factory=dict)


class ErrorInstanceV2(BaseModel):
    """一个错误 case 被压缩后的可复用修复接口摘要。

    **生成路径**：code deterministic 提取 structure_flags + candidate tags，
    一次 LLM 产出所有因果链字段（deep_bias / repair_goal / repair_skeleton /
    instantiation_slots / branch_rules / guardrails / rewrite_hint_proto）。
    不拆 3 步 LLM——字段间的一致性耦合 by design。
    """

    db_id: str
    case_id: str

    question_features: QuestionFeatures
    pred_sql_features: PredSqlFeatures

    deep_bias: str
    """为什么会错——深层偏差总结。"""
    repair_goal: str
    """修复后想回到的接口层面。"""

    repair_skeleton: RepairSkeleton

    instantiation_slots: List[InstantiationSlot] = Field(default_factory=list)
    branch_rules: List[BranchRule] = Field(default_factory=list)
    guardrails: List[Guardrail] = Field(default_factory=list)
    repair_program: List[RepairProgramStep] = Field(default_factory=list)
    """Structured repair steps extracted from this case. Dependency steps must
    come from here before they can enter any runtime contract."""
    canonical_repair_ir: Optional[CanonicalRepairIR] = None
    """Offline canonical repair program. Runtime never derives this from gold;
    it only consumes synthesized programs already stored in memory objects."""
    source_antipattern_hypothesis: Dict[str, Any] = Field(default_factory=dict)
    target_invariant_hypothesis: Dict[str, Any] = Field(default_factory=dict)
    possible_effect_axes: List[PossibleEffectAxis] = Field(default_factory=list)
    repair_insight_signature: Optional[RepairInsightSignature] = None
    """Case-local insight card. Broad axes alone must not justify grouping."""
    core_vs_accessory: Dict[str, Any] = Field(default_factory=dict)
    uncertain_axes: List[str] = Field(default_factory=list)

    rewrite_hint_proto: str
    """原型级 rewrite 提示（自然语言，给 Memory Rewrite 参考）。"""

    risk_level: RiskLevel = RiskLevel.MEDIUM
    case_signal_bundle: Optional[CaseSignalBundle] = None
    """Phase A audit-only provenance carried beside the LLM summary."""


# =============================================================================
# 5. Action / Action Compiler IO （doc/plan.md §9.4）
# 约束生成契约：candidates 由代码从 LocalSchemaView + memory 预枚举。LLM 只 select
# =============================================================================


class ActionCandidate(BaseModel):
    """单个 primitive 下，代码预枚举的 candidate argument。

    Compiler LLM 必须从这里 select，不能自由发明 target_column/target_table
    （hallucination 防线）。"""

    candidate_id: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    provenance: str
    """候选来源（pk_fk_graph / memory_hint / local_schema 等），便于 debug。"""
    schema_legal: bool = True
    """此 candidate 在 LocalSchemaView 下是否合法。非法 candidate 应直接过滤，不进入 LLM。"""
    source_group_id: Optional[str] = None
    """Phase 1.4（δ 策略）：多 GroupSummary 匹配时，每个 candidate 记录来源 group。
    最终 Action.source_group_id 必须从此字段带入（保证 action → runtime memory attribution）。"""
    source_group_type: Optional[GroupType] = None
    bundle_id: Optional[str] = None
    effect_kind: Optional[str] = None
    bound_branch_id: Optional[str] = None
    cleanup_edits: List[Dict[str, Any]] = Field(default_factory=list)
    counts_as_action: int = 1
    bundle_selection_key: Optional[str] = None
    bundle_primary_primitive: Optional[str] = None


class ActionCandidateSet(BaseModel):
    """单个 primitive 在当前 case 上的 candidate 集合。空集合意味着该 primitive 不可用。"""

    primitive: ActionPrimitive
    candidates: List[ActionCandidate] = Field(default_factory=list)
    empty_reason: Optional[str] = None
    """candidates 为空时的 reason（对齐 vocabulary.SCHEMA_RECALL_MISS_CATEGORIES）。"""


class ActionCompilerInput(BaseModel):
    """Compiler 的输入。"""

    runtime_case_view: RuntimeCaseView
    memory_objects: List["GroupSummary"] = Field(default_factory=list)
    candidate_sets: List[ActionCandidateSet] = Field(default_factory=list)
    """代码预枚举的 candidate 集合，按 primitive 分组。"""


class Action(BaseModel):
    """Compiler 输出的单个动作。"""

    action_id: str
    source_group_id: str
    source_group_type: GroupType

    primitive: ActionPrimitive
    arguments: Dict[str, Any] = Field(default_factory=dict)
    selected_candidate_id: Optional[str] = None
    """对应 ActionCandidate.candidate_id。escape hatch 下可为空但需 log。"""

    rationale_short: str
    priority: float = 0.5
    risk: RiskLevel = RiskLevel.MEDIUM
    allowed_edit_scope: List[EditScope] = Field(default_factory=list)
    """Codex / bounded autonomy：rewrite 可做 dependency repair，但仅限此 scope 内。"""

    used_escape_hatch: bool = False
    """True 时 selected_candidate_id 可能为空；必须进入人工审查队列。"""
    selection_origin: str = "none"
    """llm_selected / deterministic_unique / none."""
    selection_policy: str = "llm_required"
    """llm_required / deterministic_allowed / deterministic_only."""
    fallback_used: bool = False
    fallback_reason: Optional[str] = None


class EscapeHatchLog(BaseModel):
    """Codex Patch（Compiler escape hatch）：一次 justified out-of-set 提案。

    允许 compiler 在 candidate 集合不足时提出 1 个 out-of-set action，
    但必须 log 并进入人工审查队列——不会自动吸收/升级。
    """

    primitive: ActionPrimitive
    proposed_arguments: Dict[str, Any]
    justification: str
    case_id: str
    group_id: str


class ActionCompilerOutput(BaseModel):
    """Compiler 的输出。"""

    actions: List[Action] = Field(default_factory=list)
    schema_diagnostics: LocalSchemaViewDiagnostics = Field(default_factory=LocalSchemaViewDiagnostics)
    """Codex Patch 1：schema 召回失败单独出口。"""
    escape_hatch_log: Optional[EscapeHatchLog] = None
    selection_summary: Dict[str, Any] = Field(default_factory=dict)


# =============================================================================
# 6. ActionRealizationTrace （bounded autonomy rewrite 回填）
# Codex Patch 4：每次 rewrite 记录每个 action 实际在哪条 SQL edit 上实现
# =============================================================================


class SqlEditTrace(BaseModel):
    """一次 SQL edit 的描述。"""

    edit_kind: str  # add / remove / replace / move
    location: EditScope
    before_snippet: Optional[str] = None
    after_snippet: Optional[str] = None


class ActionRealizationTrace(BaseModel):
    """一个 action 在实际 rewrite 中被如何 realize。

    用途：
    - 归因：runtime 统计某 action 实际落到 SQL 里带来 gain 还是 regression
    - guardrail 检查：rewrite 有没有越出 `allowed_edit_scope`
    - pattern drift 检测：同一 pattern 的 action 被 realize 成不同方式 → 可能需要 split
    """

    action_id: str
    realized: bool
    edits: List[SqlEditTrace] = Field(default_factory=list)
    scope_violation: bool = False
    notes: Optional[str] = None


# =============================================================================
# 7. C0 Snapshot （doc/plan.md §9.1 + §11; Codex Patch 3）
# =============================================================================


class UpstreamFingerprint(BaseModel):
    """Codex Patch 3：C0 快照必须携带的 upstream generator 指纹。

    generator（model / beam / prompt）变了则 C0 作废，不能再用于 replay
    promotion/rollback 决策。"""

    base_model_id: str
    beam_size: int = 1
    temperature: float = 0.0
    prompt_hash: str
    """Memory Rewrite 接入点之前的 prompt 字符串 hash（SHA1 前 16 位即可）。"""


class C0Snapshot(BaseModel):
    """Rewrite 接入点之前的候选集合快照。

    绝不能包含任何 memory 生成候选——C0 定义就是 memory-free baseline。
    """

    db_id: str
    case_id: str
    candidates: List[str]
    """C0 中的 SQL 候选列表。通常 len == 1 或 beam size。"""
    top1_hash: str
    upstream_fingerprint: UpstreamFingerprint
    captured_at: str
    """ISO 时间戳。"""


class C1Result(BaseModel):
    """Rewrite 后的 C1（C0 + rewrite candidates）及最终选择。"""

    case_id: str
    c0_hash: str
    rewrite_candidates: List[str] = Field(default_factory=list)
    final_selected_sql: str
    selected_origin: str  # "c0_passthrough" / "rewrite_from_action" / ...
    selected_action_ids: List[str] = Field(default_factory=list)
    """从哪个/哪些 action realize 出来的。"""


# =============================================================================
# 8. GroupSummary / LibraryStateV2 （doc/plan.md §8）
# =============================================================================


class CoreInterface(BaseModel):
    """Group 的核心接口（家族共享签名）。"""

    question_family_tags: List[str] = Field(default_factory=list)
    pred_family_tags: List[str] = Field(default_factory=list)
    repair_goal: str
    repair_skeleton_prototype: RepairSkeleton


class InstantiationProgram(BaseModel):
    """Group 的统一实例化程序。pattern 必须有；family 可选；singleton 就一条。"""

    shared: bool
    """Whether a structurally valid synthesized shared program exists."""
    shared_status: str = "none"
    """none / structural / compilable / runtime_validated / formal_validated."""
    template: Optional[str] = None
    slots: List[InstantiationSlot] = Field(default_factory=list)
    branch_rules: List[BranchRule] = Field(default_factory=list)
    repair_program: List[RepairProgramStep] = Field(default_factory=list)
    """Group-level executable repair program. Built from extracted member
    steps; compiler must not add steps that are absent here."""
    synthesized_program: Optional[CanonicalRepairProgram] = None
    """Canonical shared program synthesized from member ErrorInstance IRs."""
    program_coverage: Optional[ProgramCoverage] = None
    """Deterministic compiler coverage for the synthesized program."""


class TriggerSignature(BaseModel):
    """Group 的触发签名——question 侧 + pred 侧双向。"""

    required_question_tags: List[str] = Field(default_factory=list)
    required_pred_tags: List[str] = Field(default_factory=list)
    decisive_antipatterns: List[str] = Field(default_factory=list)
    negative_evidence: List[str] = Field(default_factory=list)


class TriggerContract(BaseModel):
    """Contract-style runtime trigger.

    Required signals are exact, answer-blind facts that must be reproducible from
    the new case's RuntimeCaseView. Optional signals are audit/ranking only and
    must not make a failed contract pass.
    """

    schema_version: str = "trigger-contract-v0"
    required_signals: List[str] = Field(default_factory=list)
    optional_signals: List[str] = Field(default_factory=list)
    negative_signals: List[str] = Field(default_factory=list)
    audit_signals: List[str] = Field(default_factory=list)
    decisive_pred_signals: List[str] = Field(default_factory=list)
    variant_required_signal_sets: List[List[str]] = Field(default_factory=list)
    canonical_discriminants: List[str] = Field(default_factory=list)
    trigger_policy: Dict[str, Any] = Field(
        default_factory=lambda: {
            "allow_out_of_variant_generalization": False,
            "min_canonical_discriminants": 1,
            "requires_binder_dry_run": True,
        }
    )
    action_contract: Dict[str, Any] = Field(default_factory=dict)
    source_case_contract: Dict[str, Any] = Field(default_factory=dict)
    max_actions: int = 1


class ModelProfile(BaseModel):
    """当前 DeepEye+Qwen3 下的 support / gain 统计。方法无关的 core ontology 之外。"""

    support_by_model: Dict[str, int] = Field(default_factory=dict)
    support_by_method: Dict[str, int] = Field(default_factory=dict)
    gain_by_model: Dict[str, float] = Field(default_factory=dict)
    trigger_precision_by_model: Dict[str, float] = Field(default_factory=dict)


class ReplayMetrics(BaseModel):
    """某个 Group 版本的 replay 指标。"""

    version: int
    compile_coverage: float = 0.0
    """能被 compiler 编成具体 action 的成员比例。"""
    mean_action_count: float = 0.0
    replay_improvement: float = 0.0
    """replay 下相对 C0 的修复率提升（0-1）。"""
    replay_regression: float = 0.0
    leave_one_out_done: bool = False
    """Codex Patch 2：promotion test 必须经过 leave-one-out。"""
    sample_size: int = 0
    replay_improvement_llm_selected: float = 0.0
    replay_improvement_deterministic_unique: float = 0.0
    replay_improvement_total: float = 0.0
    fallback_selected_rate: float = 0.0
    llm_selected_rate: float = 0.0
    no_action_rate: float = 0.0
    formal_eligible_sample_size: int = 0
    comparison_unknown_count: int = 0
    comparison_unknown_rate: float = 0.0
    comparison_unknown_reasons: Dict[str, int] = Field(default_factory=dict)


class BranchReplayMetrics(BaseModel):
    """Replay metrics for one runtime branch inside a pattern."""

    branch_id: str
    support_case_ids: List[str] = Field(default_factory=list)
    compile_coverage: float = 0.0
    replay_improvement: float = 0.0
    replay_regression: float = 0.0
    mean_action_count: float = 0.0
    sample_size: int = 0
    replay_mode: str = ""
    failed_members: List[str] = Field(default_factory=list)
    blockers: List[str] = Field(default_factory=list)


class GroupMemberEvidence(BaseModel):
    """Member-level evidence used for offline formation and review."""

    case_id: str
    question_signals: List[SignalAtom] = Field(default_factory=list)
    pred_signals: List[SignalAtom] = Field(default_factory=list)
    schema_signals: List[SignalAtom] = Field(default_factory=list)
    execution_only_signals: List[SignalAtom] = Field(default_factory=list)
    skeleton_key: Optional[str] = None
    slot_signature: Dict[str, Any] = Field(default_factory=dict)
    schema_signature: Dict[str, Any] = Field(default_factory=dict)
    rewrite_hint_proto_hash: Optional[str] = None
    manual_label: Optional[str] = None


class GroupFormationEvidence(BaseModel):
    """Offline formation evidence kept separate from runtime trigger policy."""

    schema_version: str = "group-formation-evidence-v0"
    member_evidence: List[GroupMemberEvidence] = Field(default_factory=list)
    pair_scores: List[Dict[str, Any]] = Field(default_factory=list)
    accepted_edges: List[Dict[str, Any]] = Field(default_factory=list)
    rejected_edge_sample: List[Dict[str, Any]] = Field(default_factory=list)
    manual_alignment_snapshot: Dict[str, Any] = Field(default_factory=dict)
    formation_version: str = ""
    review_status: str = "unreviewed"


class TriggerPolicy(BaseModel):
    """Auditable trigger policy metadata beyond the legacy bare tag lists."""

    schema_version: str = "trigger-policy-v0"
    required_signal_sources: Dict[str, str] = Field(default_factory=dict)
    required_signal_support_ratio: Dict[str, float] = Field(default_factory=dict)
    negative_signal_sources: Dict[str, str] = Field(default_factory=dict)
    runtime_visible_only: bool = True
    threshold_version: str = ""
    notes: Optional[str] = None


class GroupLifecycle(BaseModel):
    """Lifecycle metadata for safe singleton -> family -> pattern evolution."""

    superseded_by_group_id: Optional[str] = None
    formation_parent_ids: List[str] = Field(default_factory=list)
    promotion_state: str = "singleton_or_seed"
    quarantine_reason: Optional[str] = None


class GroupSummary(BaseModel):
    """三层记忆对象的统一 schema（singleton / family / pattern）。"""

    group_id: str
    group_type: GroupType
    db_id: str

    case_ids: List[str] = Field(default_factory=list)
    support: int = 0
    confidence: Confidence = Confidence.LOW
    version: int = 0
    runtime_usable: bool = False
    """仅当 ReplayMetrics 通过 runtime 准入门槛才置 True。"""
    runtime_contract_status: str = ""
    """Audit status for the executable runtime trigger contract."""
    runtime_blockers: List[str] = Field(default_factory=list)
    """Non-runtime blockers discovered while validating this memory object."""
    status: GroupStatus = GroupStatus.ACTIVE

    core_interface: CoreInterface
    instantiation_program: InstantiationProgram
    trigger_signature: TriggerSignature
    trigger_contract: TriggerContract = Field(default_factory=TriggerContract)
    guardrails: List[Guardrail] = Field(default_factory=list)

    # Codex Patch 4：action-realization trace（runtime 每次 rewrite 回填）
    action_realization_traces: List[ActionRealizationTrace] = Field(default_factory=list)

    # Model/Method profile
    model_profile: ModelProfile = Field(default_factory=ModelProfile)

    # Replay metrics 版本化
    replay_history: List[ReplayMetrics] = Field(default_factory=list)

    # Audit / provenance
    formation_signals: Dict[str, Any] = Field(default_factory=dict)
    """Compact member-level signals used by offline formation and runtime source matching."""
    formation_evidence: GroupFormationEvidence = Field(default_factory=GroupFormationEvidence)
    trigger_policy: TriggerPolicy = Field(default_factory=TriggerPolicy)
    lifecycle: GroupLifecycle = Field(default_factory=GroupLifecycle)
    created_at: Optional[str] = None
    last_updated_at: Optional[str] = None


class LibraryStateV2(BaseModel):
    """单库的 library 全状态。"""

    db_id: str
    patterns: List[GroupSummary] = Field(default_factory=list)
    experience_families: List[GroupSummary] = Field(default_factory=list)
    singletons: List[GroupSummary] = Field(default_factory=list)
    # case 出入 library 的次数——global conflict scan 的触发依据
    cases_processed: int = 0


class TriggerCandidateAudit(BaseModel):
    """Audit record for one runtime trigger candidate.

    This is answer-blind: it records only scores/gates derived from the current
    RuntimeCaseView and the memory object.
    """

    group_id: str
    group_type: GroupType
    gate_passed: bool
    gate_reasons: List[str] = Field(default_factory=list)
    question_overlap: float = 0.0
    """Legacy tag overlap retained for audit only; runtime gates do not depend on it."""
    manifest_overlap: float = 0.0
    """Legacy tag overlap retained for audit only; runtime gates do not depend on it."""
    signal_compat: float = 0.0
    """1.0 iff the boolean trigger contract passed; retained for old log readers."""
    structural_compat: float = 0.0
    slot_overlap: float = 0.0
    final_score: float = 0.0
    contract_version: Optional[str] = None
    required_signal_hits: List[str] = Field(default_factory=list)
    required_signal_misses: List[str] = Field(default_factory=list)
    negative_signal_hits: List[str] = Field(default_factory=list)
    optional_signal_hits: List[str] = Field(default_factory=list)
    variant_required_match: bool = False
    canonical_discriminant_hits: List[str] = Field(default_factory=list)
    binder_dry_run_success: bool = False
    trigger_policy: Dict[str, Any] = Field(default_factory=dict)
    selected_branch_id: Optional[str] = None
    matched_branch_ids: List[str] = Field(default_factory=list)
    branch_match_audit: List[Dict[str, Any]] = Field(default_factory=list)
    branch_blockers: List[str] = Field(default_factory=list)
    branch_runtime_usable_count: int = 0
    source_trigger_passed: bool = False
    hard_gate_reasons: List[str] = Field(default_factory=list)
    deferred_instantiation_reasons: List[str] = Field(default_factory=list)
    compiler_candidate_reasons: List[str] = Field(default_factory=list)


class TriggerResult(BaseModel):
    """Runtime trigger output shared by logging, compiler, and rewrite planning."""

    strategy_version: str
    selected_groups: List[GroupSummary] = Field(default_factory=list)
    candidates: List[TriggerCandidateAudit] = Field(default_factory=list)
    selected_branch_ids: Dict[str, str] = Field(default_factory=dict)
    branch_selection_audit: Dict[str, Any] = Field(default_factory=dict)


# =============================================================================
# 9. Promotion / Conflict scan / Attribution （doc/plan.md §10 + §11; Codex Patch 2/5）
# =============================================================================


class PromotionTestResult(BaseModel):
    """Codex Patch 2：family→pattern promotion test 的结果。"""

    group_id: str
    eligible: bool
    reason: Optional[str] = None
    replay_metrics: ReplayMetrics
    shared_minimal_repair_interface: bool
    shared_instantiation_program: bool
    program_coverage: Optional[ProgramCoverage] = None
    leave_one_out_results: List[Dict[str, Any]] = Field(default_factory=list)
    """每次 leave-one-out 的样本结果（留出成员 id + 是否编译通过 + 方向是否一致）。"""
    formal_replay_metrics: Optional[ReplayMetrics] = None
    runtime_family_evidence_mode: str = ""
    formal_promotion_blocker: Optional[str] = None
    support_protocol_passed: bool = False
    formal_replay_modes: List[str] = Field(default_factory=list)
    formal_eligible_replay_rows: int = 0


class ConflictScanReport(BaseModel):
    """Codex Patch 5：周期性 global conflict scan 的报告。"""

    triggered_at_case_index: int
    scanned_pairs: int
    conflicts_found: List[Dict[str, Any]] = Field(default_factory=list)
    """每个 conflict：{group_id_a, group_id_b, overlap_score, conflict_reason}。"""
    recommendations: List[str] = Field(default_factory=list)
    """比如 "split group_id_a"、"quarantine group_id_b"、"merge both"。"""


class MemoryInducedAttribution(BaseModel):
    """C0→C1 归因记录，决定某次 runtime event 是 gain / regression / neutral。"""

    case_id: str
    baseline_c0_correct: bool
    final_c1_correct: bool
    final_from_rewrite_branch: bool
    triggered_group_ids: List[str] = Field(default_factory=list)
    classification: str
    """memory_induced_gain / memory_induced_regression / memory_neutral / baseline_neutral / ..."""


# Forward reference resolution for ActionCompilerInput (uses GroupSummary before declaration).
ActionCompilerInput.model_rebuild()
