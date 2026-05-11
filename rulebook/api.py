from __future__ import annotations

from method.EEA.rulebook.common.core.data_structures import (
    GroupSummary,
    LibraryStateV2,
    RuntimeCaseView,
)
from method.EEA.rulebook.common.core.schema import (
    CaseInput,
    CaseMeta,
    CaseNL,
    CaseSQL,
    CaseSettings,
    SchemaGraph,
)
from method.EEA.rulebook.common.analysis.structure_family import (
    DISCOVERY_STRATEGY_VERSION,
    EVOLUTION_STRATEGY_VERSION,
    PATTERN_SCHEMA_VERSION,
    PROMPT_BUNDLE_VERSION,
    TRIGGER_STRATEGY_VERSION,
    build_repair_signature_diagnostics,
    cached_ast_signature,
    discovery_bucket_key_from_sql,
    instances_structurally_compatible,
    repair_signature_dict_from_sql,
)
from method.EEA.rulebook.common.io.db_schema_access import SqliteDBSchemaAccess
from method.EEA.rulebook.common.io.execution_compare import (
    run_execution_comparison,
    serialize_execution_comparison,
)
from method.EEA.rulebook.common.learning.accumulate import (
    accumulate_wrong_case,
    append_regression_negative_guard,
    error_instance_to_singleton,
)
from method.EEA.rulebook.common.learning.evolution import (
    compact_evolution_report,
    evolve_library_with_replay,
    final_evolve_and_freeze,
)
from method.EEA.rulebook.common.reporting.pool_coverage import build_pool_coverage_report
from method.EEA.rulebook.common.reporting.trigger_observability import (
    pattern_trigger_observability_from_dict,
)
from method.EEA.rulebook.common.reporting.versioning import (
    build_run_metadata,
    detect_git_context,
    get_pipeline_version,
    write_run_metadata,
)
from method.EEA.rulebook.common.runtime.runtime import (
    _memory_schema_tables,
    _case_pred_current_summary,
    _current_contract_signals,
    build_current_case_signals,
    build_runtime_case_view,
    prepare_rewrite_plan,
    retrieve_groups,
    rewrite_one_candidate,
    run_memory_rewrite_runtime,
    trigger_memory_objects,
)

__all__ = [
    "GroupSummary",
    "LibraryStateV2",
    "RuntimeCaseView",
    "CaseInput",
    "CaseMeta",
    "CaseNL",
    "CaseSQL",
    "CaseSettings",
    "SchemaGraph",
    "SqliteDBSchemaAccess",
    "DISCOVERY_STRATEGY_VERSION",
    "EVOLUTION_STRATEGY_VERSION",
    "PATTERN_SCHEMA_VERSION",
    "PROMPT_BUNDLE_VERSION",
    "TRIGGER_STRATEGY_VERSION",
    "_case_pred_current_summary",
    "_current_contract_signals",
    "_memory_schema_tables",
    "accumulate_wrong_case",
    "append_regression_negative_guard",
    "build_repair_signature_diagnostics",
    "build_current_case_signals",
    "build_pool_coverage_report",
    "build_run_metadata",
    "build_runtime_case_view",
    "cached_ast_signature",
    "compact_evolution_report",
    "detect_git_context",
    "discovery_bucket_key_from_sql",
    "error_instance_to_singleton",
    "evolve_library_with_replay",
    "final_evolve_and_freeze",
    "get_pipeline_version",
    "instances_structurally_compatible",
    "pattern_trigger_observability_from_dict",
    "prepare_rewrite_plan",
    "repair_signature_dict_from_sql",
    "retrieve_groups",
    "rewrite_one_candidate",
    "run_execution_comparison",
    "run_memory_rewrite_runtime",
    "serialize_execution_comparison",
    "trigger_memory_objects",
    "write_run_metadata",
]
