"""Prompt templates for the Minimal Repair Interface v2 stack.

Phase 0 delivers **首版（first-draft）** prompts for each LLM node. They
are expected to be iterated in Phase 1 as Claude reviews pattern-synthesis
quality against the manual ground truth (doc/db_pattern_groups.json).

Node map
--------
- ``wrong_case_auditor``      : offline post-mortem on failed cases
- ``error_instance_extractor``: CaseAudit + RuntimeCaseView → ErrorInstanceV2
- ``action_compiler``         : constrained selection over pre-enumerated
                                candidates
- ``memory_rewrite``          : bounded SQL rewrite that must realize the
                                provided structured actions
- ``hint_instantiation``      : readability-only rewrite of a code-rendered
                                action brief
- ``compatibility_judge``     : advisory-only shared-interface explainer for
                                audit/replay analysis
- ``shared_insight_judge``    : advisory-only comparison of case-local insight
                                cards
- ``insight_pattern_slicer``  : insight-first slicing of retrieved components
                                before formal pattern admission
- ``pattern_admission_judge`` : offline-only admission of stable pattern
                                candidates with finite branches
- ``schema_role_annotator``   : schema-level free-form role hints
- ``pattern_pre_condition_match``: runtime two-channel pre-condition matching
- ``pattern_equivalence_judge``: offline comparison of two pre-condition
                                contracts during pattern dedup
"""

from .action_compiler import ACTION_COMPILER_PROMPT, build_action_compiler_prompt
from .compatibility_judge import COMPATIBILITY_JUDGE_PROMPT, build_compatibility_judge_prompt
from .error_instance_extractor import (
    ERROR_INSTANCE_EXTRACTOR_PROMPT,
    build_error_instance_extractor_prompt,
)
from .hint_instantiation import HINT_INSTANTIATION_PROMPT, build_hint_instantiation_prompt
from .insight_pattern_slicer import (
    INSIGHT_PATTERN_SLICER_PROMPT,
    build_insight_pattern_slicer_prompt,
)
from .memory_rewrite import MEMORY_REWRITE_PROMPT, build_memory_rewrite_prompt
from .pattern_admission_judge import (
    PATTERN_ADMISSION_JUDGE_PROMPT,
    build_pattern_admission_judge_prompt,
)
from .pattern_pre_condition_match import (
    PATTERN_PRE_CONDITION_Q_PROMPT,
    PATTERN_PRE_CONDITION_S_PROMPT,
    build_pattern_pre_condition_q_prompt,
    build_pattern_pre_condition_s_prompt,
)
from .pattern_equivalence_judge import (
    PATTERN_EQUIVALENCE_JUDGE_PROMPT,
    build_pattern_equivalence_judge_prompt,
)
from .schema_role_annotator import (
    SCHEMA_ROLE_ANNOTATOR_PROMPT,
    build_schema_role_annotator_prompt,
)
from .shared_insight_judge import (
    SHARED_INSIGHT_JUDGE_PROMPT,
    build_shared_insight_judge_prompt,
)
from .wrong_case_auditor import WRONG_CASE_AUDITOR_PROMPT, build_wrong_case_auditor_prompt

__all__ = [
    "ACTION_COMPILER_PROMPT",
    "build_action_compiler_prompt",
    "COMPATIBILITY_JUDGE_PROMPT",
    "build_compatibility_judge_prompt",
    "ERROR_INSTANCE_EXTRACTOR_PROMPT",
    "build_error_instance_extractor_prompt",
    "HINT_INSTANTIATION_PROMPT",
    "build_hint_instantiation_prompt",
    "INSIGHT_PATTERN_SLICER_PROMPT",
    "build_insight_pattern_slicer_prompt",
    "MEMORY_REWRITE_PROMPT",
    "build_memory_rewrite_prompt",
    "PATTERN_ADMISSION_JUDGE_PROMPT",
    "build_pattern_admission_judge_prompt",
    "PATTERN_PRE_CONDITION_Q_PROMPT",
    "PATTERN_PRE_CONDITION_S_PROMPT",
    "build_pattern_pre_condition_q_prompt",
    "build_pattern_pre_condition_s_prompt",
    "PATTERN_EQUIVALENCE_JUDGE_PROMPT",
    "build_pattern_equivalence_judge_prompt",
    "SCHEMA_ROLE_ANNOTATOR_PROMPT",
    "build_schema_role_annotator_prompt",
    "SHARED_INSIGHT_JUDGE_PROMPT",
    "build_shared_insight_judge_prompt",
    "WRONG_CASE_AUDITOR_PROMPT",
    "build_wrong_case_auditor_prompt",
]
