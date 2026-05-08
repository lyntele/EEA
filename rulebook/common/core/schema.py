from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class IntentLevel(str, Enum):
    output_contract = "output_contract"
    scope_semantics = "scope_semantics"
    aggregation_semantics = "aggregation_semantics"
    join_path = "join_path"
    value_grounding = "value_grounding"
    predicate_logic = "predicate_logic"
    schema_binding = "schema_binding"
    subquery_form = "subquery_form"
    set_operation = "set_operation"


class ErrorOperator(str, Enum):
    missing_distinct = "missing_distinct"
    unnecessary_distinct = "unnecessary_distinct"
    missing_group_by = "missing_group_by"
    wrong_group_by_key = "wrong_group_by_key"
    wrong_aggregate_target = "wrong_aggregate_target"
    wrong_aggregate_scope = "wrong_aggregate_scope"
    wrong_select_columns = "wrong_select_columns"
    missing_required_columns = "missing_required_columns"
    extra_columns = "extra_columns"
    format_mapping_missing = "format_mapping_missing"
    wrong_join_chain = "wrong_join_chain"
    wrong_join_key = "wrong_join_key"
    wrong_join_type = "wrong_join_type"
    wrong_filter_column = "wrong_filter_column"
    wrong_filter_value = "wrong_filter_value"
    missing_filter = "missing_filter"
    wrong_topk_ties_handling = "wrong_topk_ties_handling"
    wrong_boolean_logic = "wrong_boolean_logic"
    missing_null_handling = "missing_null_handling"
    wrong_interval_boundary = "wrong_interval_boundary"


class RootCause(BaseModel):
    intent_level: List[IntentLevel] = Field(default_factory=list)
    error_operator: List[ErrorOperator] = Field(default_factory=list)
    score: float = 0.0
    evidence_used: List[str] = Field(default_factory=list)
    rationale: Optional[str] = None


class RootCauseBundle(BaseModel):
    top_candidates: List[RootCause] = Field(default_factory=list)
    chosen: Optional[RootCause] = None


class CaseMeta(BaseModel):
    case_id: str
    db_id: str
    db_path: str


class CaseNL(BaseModel):
    question: str
    evidence: Optional[str] = None


class CaseSQL(BaseModel):
    pred_sql: str
    gold_sql: str


class SchemaGraph(BaseModel):
    tables: Dict[str, List[str]] = Field(default_factory=dict)
    fk_edges: List[Dict[str, str]] = Field(default_factory=list)
    edge_properties: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class CaseSettings(BaseModel):
    K_per_branch: int = 6
    timeout_sec: int = 30
    probe_timeout_sec: int = 10
    n_plans: int = 3
    max_actions_per_plan: int = 3
    max_candidates_per_branch_step: int = 12


class CaseInput(BaseModel):
    meta: CaseMeta
    nl: CaseNL
    sql: CaseSQL
    schema: SchemaGraph
    column_descriptions: Dict[str, str] = Field(default_factory=dict)
    settings: CaseSettings = Field(default_factory=CaseSettings)


class SelectShape(BaseModel):
    output_arity: int = 0
    has_distinct: bool = False
    has_aggregate: bool = False


class ASTSignature(BaseModel):
    parse_ok: bool = True
    from_table: Optional[str] = None
    tables_involved: List[str] = Field(default_factory=list)
    join_edges: List[Dict[str, Any]] = Field(default_factory=list)
    predicates: List[str] = Field(default_factory=list)
    having: List[str] = Field(default_factory=list)
    select_expressions: List[str] = Field(default_factory=list)
    select_column_refs: List[str] = Field(default_factory=list)
    select_shape: SelectShape = Field(default_factory=SelectShape)
    group_by_keys: List[str] = Field(default_factory=list)
    order_by: List[Any] = Field(default_factory=list)
    limit: Optional[int] = None
    has_subquery: bool = False
    has_cte: bool = False
    comparison_operators: List[str] = Field(default_factory=list)
    logical_operators: List[str] = Field(default_factory=list)
    null_checks: List[str] = Field(default_factory=list)
    predicate_values: List[str] = Field(default_factory=list)
    window_functions: List[str] = Field(default_factory=list)
    date_time_functions: List[str] = Field(default_factory=list)
    string_functions: List[str] = Field(default_factory=list)
    math_functions: List[str] = Field(default_factory=list)
    conditional_exprs: List[str] = Field(default_factory=list)
    type_casts: List[str] = Field(default_factory=list)
    set_operations: List[str] = Field(default_factory=list)


class Fingerprints(BaseModel):
    join_graph_fp: Optional[str] = None
    select_shape_fp: Optional[str] = None
    predicate_fp: Optional[str] = None


class ExecutionResult(BaseModel):
    result_type: str
    result_kind: Optional[str] = None
    columns: Optional[List[str]] = None
    row_count: int = 0
    sample_rows: List[List[Any]] = Field(default_factory=list)
    row_hash: Optional[str] = None
    scalar_value: Optional[Any] = None
    error: Optional[str] = None


class Mismatch(BaseModel):
    mismatch_type: str
    subtype: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)
    distance: Dict[str, Optional[float]] = Field(default_factory=dict)


class SignalsSummary(BaseModel):
    base_row_ratio: Optional[float] = None
    base_row_ratio_bucket: Optional[str] = None
    fanout_ratio_pred: Optional[float] = None
    fanout_bucket: Optional[str] = None
    coverage_min: Optional[float] = None
    coverage_bucket: Optional[str] = None
    join_edges_diff: Optional[bool] = None
    divergence_step: Optional[int] = None
    output_arity_pred: Optional[int] = None
    output_arity_gold: Optional[int] = None
    output_arity_mismatch: Optional[bool] = None
    has_aggregate_pred: Optional[bool] = None
    has_limit_pred: Optional[bool] = None
    has_order_by_pred: Optional[bool] = None
    selectivity_outlier: Optional[bool] = None


class SignalsDetails(BaseModel):
    pred_ast: Dict[str, Any] = Field(default_factory=dict)
    gold_ast: Dict[str, Any] = Field(default_factory=dict)
    pred_exec: Dict[str, Any] = Field(default_factory=dict)
    gold_exec: Dict[str, Any] = Field(default_factory=dict)
    join_step_trace: Dict[str, Any] = Field(default_factory=dict)
    join_edges_diff_detail: Dict[str, Any] = Field(default_factory=dict)
    key_coverage: List[Dict[str, Any]] = Field(default_factory=list)
    fanout: Dict[str, Any] = Field(default_factory=dict)
    predicate_selectivity: List[Dict[str, Any]] = Field(default_factory=list)
    output_shape_probe: Dict[str, Any] = Field(default_factory=dict)


class PlanAction(BaseModel):
    op: str
    args: Dict[str, Any] = Field(default_factory=dict)


class BranchPlan(BaseModel):
    plan_id: str
    branch_role: str
    reasoning: str
    actions: List[PlanAction] = Field(default_factory=list)
    expected_ast_diff: Dict[str, bool] = Field(default_factory=dict)
    source: str = "LLM"


class PlannerOutput(BaseModel):
    plans: List[BranchPlan] = Field(default_factory=list)
    source: str = "LLM"


class DSLSpec(BaseModel):
    allowed_ops: List[str] = Field(default_factory=list)
    op_descriptions: Dict[str, str] = Field(default_factory=dict)
    n_plans: int = 3
    max_actions_per_plan: int = 3
    plan_diversity_requirement: List[str] = Field(default_factory=list)


class BranchState(BaseModel):
    branch_id: str
    t: int = 0
    base_sql: str
    plan: BranchPlan
    budget_remaining: int
    status: str = "ACTIVE"
    history: List[BranchStepRecord] = Field(default_factory=list)


class BranchCandidate(BaseModel):
    candidate_id: str
    sql: str
    source: str
    op_sequence: List[str] = Field(default_factory=list)
    op_args_sigs: List[str] = Field(default_factory=list)


class CandidatePool(BaseModel):
    branch_id: str
    t: int
    candidates: List[BranchCandidate] = Field(default_factory=list)


class ASTDiffSignature(BaseModel):
    join_edges_changed: int = 0
    distinct_changed: bool = False
    predicates_changed: int = 0
    select_cols_changed: int = 0
    groupby_changed: bool = False
    order_by_changed: bool = False
    limit_changed: bool = False
    edit_cost: int = 0


class CheckResult(BaseModel):
    candidate_id: str
    valid: bool = True
    violations: List[str] = Field(default_factory=list)
    ast_diff_signature: ASTDiffSignature = Field(default_factory=ASTDiffSignature)


class EvalCandidate(BaseModel):
    candidate_id: str
    sql: str
    mismatch: Mismatch
    pred_exec_brief: Dict[str, Any] = Field(default_factory=dict)
    score: float = 0.0
    verdict: str = "WRONG"


class BranchStepDecision(BaseModel):
    branch_id: str
    t: int
    best: Optional[EvalCandidate] = None
    ranked_topk: List[EvalCandidate] = Field(default_factory=list)
    stop_branch: bool = False


class GlobalDecision(BaseModel):
    global_status: str = "RUNNING"
    winner_branch_id: Optional[str] = None
    winner_sql: Optional[str] = None
    stop_reason: Optional[str] = None


class BranchStepRecord(BaseModel):
    t: int
    base_sql: str
    mismatch_before: Dict[str, Any] = Field(default_factory=dict)
    signals_before: Dict[str, Any] = Field(default_factory=dict)
    candidate_pool_size: int = 0
    after_dedup_size: int = 0
    after_checker_valid: int = 0
    evaluated: List[Dict[str, Any]] = Field(default_factory=list)
    best: Optional[Dict[str, Any]] = None
    mismatch_after: Dict[str, Any] = Field(default_factory=dict)
    distance_delta: Optional[float] = None
    key_signal_changes: Dict[str, Any] = Field(default_factory=dict)


class BranchTrace(BaseModel):
    plan: Dict[str, Any] = Field(default_factory=dict)
    steps: List[BranchStepRecord] = Field(default_factory=list)
    final_status: str = "DEAD"
    final_sql: Optional[str] = None
    # Optional: detailed refinement session trace, when refiner mode is enabled.
    refinement_trace: Optional["RefinementSessionTrace"] = None


class CaseTrace(BaseModel):
    case_id: str
    db_id: str
    question: str
    initial_pred_sql: str
    gold_sql: Optional[str] = None
    initial_mismatch: Optional[Dict[str, Any]] = None
    initial_signals_summary: Optional[Dict[str, Any]] = None
    initial_signals_details: Optional[Dict[str, Any]] = None
    ast_fp_pred: Optional[Dict[str, Any]] = None
    planner_source: str = "LLM"
    branches: Dict[str, BranchTrace] = Field(default_factory=dict)
    global_result: GlobalDecision = Field(default_factory=GlobalDecision)
    stats: Dict[str, Any] = Field(default_factory=dict)


class StepEvidence(BaseModel):
    t: int
    mismatch_type_before: str
    mismatch_type_after: str
    distance_delta: Optional[float] = None
    key_signal_bucket_changes: Dict[str, Any] = Field(default_factory=dict)
    ops_applied: List[str] = Field(default_factory=list)
    ast_diff_signature: Dict[str, Any] = Field(default_factory=dict)


class BranchOutcome(BaseModel):
    branch_id: str
    status: str
    best_trajectory_ops: List[str] = Field(default_factory=list)
    trajectory_evidence: List[StepEvidence] = Field(default_factory=list)
    final_mismatch_type: Optional[str] = None


class ClusterProfileProto(BaseModel):
    meta: Dict[str, Any] = Field(default_factory=dict)
    symptom_S0: Dict[str, Any] = Field(default_factory=dict)
    plans: List[Dict[str, Any]] = Field(default_factory=list)
    branch_outcomes: List[BranchOutcome] = Field(default_factory=list)
    winner: Dict[str, Any] = Field(default_factory=dict)
    winner_coarse_path_ops: List[str] = Field(default_factory=list)


class RefinementStep(BaseModel):
    """
    A single interactive refinement turn produced by the LLM.

    The field aliases intentionally mirror the TK-Boost protocol:
    - OBSERVED_DIFFS: short description of concrete diffs vs GOLD / target
    - EDIT_PLAN: minimal textual plan describing the next SQL edits
    - SQL: the next SQL candidate (or empty string if NO_DIFF / MATCH_OK)
    - MATCH_OK: whether the current SQL already matches the target
    - NO_DIFF: model cannot propose further meaningful edits
    """

    observed_diffs: str = Field(default="", alias="OBSERVED_DIFFS")
    edit_plan: str = Field(default="", alias="EDIT_PLAN")
    predicted_secondary_diffs: str = Field(default="", alias="PREDICTED_SECONDARY_DIFFS")
    followup_strategy: str = Field(default="", alias="FOLLOWUP_STRATEGY")
    sql: Optional[str] = Field(default=None, alias="SQL")
    candidates: List[str] = Field(default_factory=list, alias="CANDIDATES")
    match_ok: bool = Field(default=False, alias="MATCH_OK")
    no_diff: bool = Field(default=False, alias="NO_DIFF")

    class Config:
        allow_population_by_field_name = True


class RefinementTurnTrace(BaseModel):
    """
    Lightweight trace for one refinement turn, suitable for logging.
    """

    step: RefinementStep
    raw_response_text: str = ""
    llm_latency_sec: Optional[float] = None


class RefinementSessionTrace(BaseModel):
    """
    End-to-end trace for a refinement session on top of a single base SQL.
    """

    case_id: Optional[str] = None
    branch_id: Optional[str] = None
    base_sql: str
    turns: List[RefinementTurnTrace] = Field(default_factory=list)
