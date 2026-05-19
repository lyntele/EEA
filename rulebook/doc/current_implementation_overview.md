# Current EEA Implementation Overview

This document is a fast map of the current EEA codebase. It reflects the
post-selection integration path and the current runtime/update/evolve stack.

## 1. End-to-End Flow

```text
DeepEye completes generation / revise / selection and chooses S0
  -> DeepEye sends S0 + question/evidence + C0 candidate context to EEA runtime
  -> EEA builds RuntimeCaseView and current runtime signals
  -> EEA checks memory trigger contracts in LibraryStateV2 (answer-blind)
  -> For patterns, runtime evaluates branch-level contracts:
       only `runtime_usable=true` branches in `ProgramEnvelope.runtime_branches`
       are considered; the pattern is sliced to a single matched branch before
       reaching the compiler
  -> Runtime classifies each candidate into source_trigger_passed,
       hard_gate_reasons, deferred_instantiation_reasons,
       and compiler_candidate_reasons
  -> If no candidate survives the hard gates: return no_match / no_action
  -> If a memory passes: enumerate schema-legal action candidates on current S0
       (branch-scoped for matched patterns)
  -> ActionCompiler selects bounded actions
  -> Runtime compiles selected actions into a `rewrite_contract`
       (concrete edits + required_absence_checks + required_presence_checks)
  -> memory_rewrite LLM receives only S0 + rewrite_contract + minimal schema
  -> required_absence_checks are fail-closed: if the contract was not
       actually applied to S1, EEA falls back to S0
  -> DeepEye selects between S0 and the rewritten S1
  -> If S0 is wrong, DeepEye sends update request to EEA
  -> EEA audits S0 vs gold, extracts an ErrorInstance, normalizes it into memory
  -> EEA accumulates singleton memory
  -> EEA runs a full-prefix local evolve over the current LibraryStateV2
  -> Replay-gated promoted memories become visible to the next runtime case
  -> End of database: EEA runs replay-gated final evolve/freeze
```

Runtime is answer-blind. Gold SQL and execution comparison are used only by
update/evolution, never by runtime trigger / compile / rewrite. `natural_language_hint`
is still produced beside the `rewrite_contract` as a human-readable artifact,
but the contract — not the hint — drives the rewrite stage and the fail-closed
guards.

## 1.1 Two-Phase Boundary

The current rebuild is explicitly split into two phases.

Phase 1 is offline learning. It starts only after DeepEye has a wrong selected
SQL `S0` and sends the update request with `gold_sql` and execution comparison.
This phase may inspect `pred_sql -> gold_sql`, role graphs, AST deltas, and
auditor/extractor hypotheses. Its primary output is a learned
`ContrastiveRepairEffect`:

```text
source_state -> target_state
under one real repair axis
with primary/dependency/accessory/noise role
and triggerability/actionability diagnostics
```

Phase 1 does not require immediate runtime rewrite success. If an effect is
stable but cannot yet be bound or lowered, the memory should keep it as an
offline effect family and record the blocker instead of discarding the effect.

Phase 2 is online runtime use. It receives only `question + evidence + S0 +
schema + memory`. It must not inspect `gold_sql` or execution comparison. It
uses Phase-1 effects to decide whether the current S0 exposes the learned
`source_state`, whether the `target_state` is bindable from schema/memory, and
whether a bounded action/hint can be instantiated.

The current implementation work focuses on Phase 1 first: every wrong case
should produce auditable `effect_candidates`, and shared memory synthesis should
group cases by compatible effects before considering executable SQL-op details.

## 1.2 Online Accumulation And Local Evolve

Post-selection online update is designed to be semantically equivalent to
re-running offline construction on the history prefix seen so far:

```text
after wrong case k:
  accumulated library prefix = all prior active memory + singleton(k)
  local evolve input         = the whole prefix library
  local evolve output        = updated singletons/patterns
  runtime admission          = replay-gated when work_root + db_path are present
```

`focus_case_ids` is audit-only in the DeepEye adapter and in
`evolve_library_with_replay`. It records which new case triggered this update
but it must not filter the clustering/evolution candidates. The evolution
harness binds `focus_case_ids` only into the audit report; the candidate pool
comes from the whole prefix library. This avoids the previous failure mode
where q249/q253 could only see their own singleton during online update, while
offline clustering could see q206/q249/q253 together.

Online local evolve only promotes a strict pattern to runtime use when it can
construct a replay case loader from the run work directory. If `work_root` or
`db_path` is missing, EEA keeps the newly accumulated singleton but does not
manufacture runtime-usable group memory.

Existing runtime memories are preserved unless a replay-gated promoted object
explicitly supersedes them. A new singleton can be absorbed into an existing
pattern only through the same shared repair program and replay validation path;
non-matching cases do not invalidate the old pattern.

The replay evolution boundary now consumes strict pattern candidates only.
Experience-family candidates are no longer generated, promoted, or triggered;
`evolve_library_with_replay` explicitly clears `library.experience_families`
before formation. Offline pattern candidates may coexist with their source
singletons until replay promotion proves that the pattern is safe for runtime
use.

## 2. Runtime Path

### `common/runtime/runtime.py`

Main answer-blind runtime entry.

Responsibilities:

- Build `RuntimeCaseView` from question, evidence, selected SQL `S0`, schema,
  and `c0_candidates` (`build_runtime_case_view`, runtime.py:119-288).
- Build current runtime-visible signals from the current SQL and local
  schema view (`_current_contract_signals` → `build_current_case_signals`,
  runtime.py:744-749).
- Evaluate the two-channel `pattern_recognition_contract` when the memory
  object carries one (`_pattern_recognition_contract`, runtime.py:1342-1366;
  `_evaluate_pattern_pre_condition`, runtime.py:1528-1569). The contract is
  not a closed-vocabulary signal bag any more: the runtime ships a
  `pre_question_signature` and a `pre_sql_signature` to two LLM channels
  (`channel="q"`, `channel="s"`) and only records `pre_condition_matched`
  when both channels accept. Legacy `BiasRecognitionContract` is kept only
  for loading old libraries (`core/data_structures.py:1099-1107`); the
  closed-vocab `bias_recognition_signals` field on `RuntimeCaseView` is
  flagged as "Legacy compatibility field. New runtime paths must not
  populate or read it." (`core/data_structures.py:291-292`) and the v2
  builder leaves it unpopulated.
- Gate memory objects through the following layers (`_gate_group`,
  runtime.py:~2290-2810):
  - executable trigger contract (`is_contract_runtime_executable`)
  - pattern-recognition pre-condition for any memory object that carries a
    `PatternRecognitionContract`; on match, runtime opens the path for
    **both pattern and singleton** memories (P0c, commit ee3dc87,
    runtime.py:2405-2421)
  - **route_evidence_fast_track** (runtime.py:2440-2577): patterns whose
    `formation_signals.retrieval_evidence` contains `gold_only_tables` or
    `gold_join_edges` can bypass multiple gates through a deterministic
    structural match. `_route_evidence_match_reasons` (runtime.py:534-585)
    compares the pattern's learned evidence against S0's `tables_used`,
    join graph signatures, and `target_output_role`. Three match types:
    `gold_only_tables_missing`, `gold_join_edges_missing`,
    `target_output_role_mismatch`. When match reasons exist AND group is
    PATTERN,
    `route_evidence_fast_track=True`. This:
    - forces `variant_required_match=True` (line 2549)
    - forces `generalized_canonical_gate_passed=True` (line 2550)
    - clears `required_misses` (line 2551)
    - defers `source_fact_misses` instead of hard-failing (line 2553-2557)
    - skips `pre_condition` Q/S LLM judge entirely (line 2562)
    - defers branch `required_signals` misses (line 1928-1932)
    **Known gap (2026-05-18):** this validates that the PATTERN has
    structural knowledge (gold uses a table S0 doesn't have), but does not
    validate that the CURRENT CASE needs that repair. For join_bridge
    patterns this causes cross-pattern misfires: e.g., a driverStandings
    pattern fires on cases that correctly use results because both have the
    same S0 table set. The deferred `source_state_facts` and skipped Q/S
    judge would otherwise filter these cases.
    **Known gap (2026-05-18):** `_source_antipattern_failures`
    (runtime.py:987-1058) processes `output_path_delta`,
    `predicate_scope_delta`, `grain_delta`, but does NOT process
    `relation_delta`. Pattern branches carry `relation_delta.removed` /
    `.added` (the join edges to remove/add), but this is only checked at
    compiler level (`_variant_requires_relation_reroute`,
    action_compiler.py:3492-3508), not at the gate level.
  - applicability checks driven by `program_envelope`
    (`_program_envelope_applicability`, runtime.py:984-1049)
    - `program_envelope.source_antipatterns`
    - `program_envelope.target_invariants` (skip `target_added_relation_equality`
      and `target_output_roles` when pattern adds new tables — commit 1860d36,
      0b922a6)
    - `program_envelope.required_role_slots`
    - `program_envelope.negative_guards`
  - branch-level runtime selection for `pattern` memories
    (`_select_runtime_branch`, runtime.py:1763-1871; invoked at
    runtime.py:2651-2707)
    - pattern root matching (group-level gates, recognition, applicability)
      runs before branch selection
    - branches with `runtime_usable=true` are preferred; if none, branches
      with non-empty `bundle_ids` or `allowed_primitives` are tried with a
      deferred reason `runtime_usable_branch_missing_deferred_to_binder`
    - per-branch `required_signals` must be present in current
      `RuntimeCaseView` signals (`_runtime_branch_required_signals`,
      runtime.py:1326-1331; check at runtime.py:1794-1801).
      **Known gap (HEAD, 2026-05-12):** these `required_signals` are
      synthesized by `_branch_spec_required_signals`
      (`learning/pattern_formation.py:3606-3624`) as the intersection of
      member `pred.*` manifest tags. The LLM-authored
      `admission_branch_spec.required_interface_delta` is a free-text
      summary and is **not translated** into runtime-consumable
      `required_signals`. In dumps observed on `r_v2_e thrombosis`,
      sibling branches end up with identical `required_signals` and are
      separated only by their `bundle_ids` / `allowed_primitives` /
      binder-dry-run outcomes.
    - per-branch dry-run must enumerate at least one candidate and cover
      every required bundle on current `S0` (`_branch_dry_run_gate`,
      runtime.py:1729-1760)
    - zero matching branches → `runtime_branch_no_match`
      (runtime.py:1842-1843)
    - multiple matching branches with distinct
      `(bundle_ids, allowed_primitives)` signatures →
      `branch_selection_ambiguous` (runtime.py:1871); multiple matches
      with identical signatures collapse into the single
      `equivalent_branch_bundle_selected` row (runtime.py:1844-1870)
    - if pattern pre-condition succeeds but no branch can bind, runtime
      records `pattern_pre_condition_matched_branch_unbindable` and sets
      `diagnostic_only=True` (runtime.py:2662-2666); the rewrite path is
      not entered
  - singleton strict instantiation (WUv2-3 / P0c, commits b8c4e5b +
    ee3dc87):
    - SINGLETON groups whose `instantiation_program.pattern_recognition_contract`
      carries a pre-condition plus a repair direction (or synthesized ops)
      become runtime-usable via
      `ensure_materialized_trigger_contract` (trigger_contract.py:299-312,
      status string `pre_condition_contract_runtime_entry`)
    - at gate time, singletons must additionally pass
      `_singleton_canonical_exact_check` (runtime.py:2508-2515); on
      failure, `_singleton_direction_isomorphic`
      (runtime.py:2238-2266) acts as a direction-only gate. Mismatch
      produces hard reason `singleton_direction_mismatch`
      (runtime.py:2525); isomorphism allows exact-check reasons to be
      deferred to the compiler (runtime.py:2537-2543)
    - the `singleton_strict_audit` block in the candidate audit
      (runtime.py:2795-2807) records
      `singleton_strict_mode="wuv2_3_pre_condition_audit_only"` (the
      original WUv2-3 label is retained for traceability),
      `singleton_pre_condition_matched`, and
      `singleton_canonical_exact_passed`
  - compiler dry-run (`_compiler_dry_run_gate`, runtime.py:1874-; called
    at runtime.py:2709-2732)
    - non-empty candidate enumeration
    - required bundle coverage
    - bundle budget check
    - branch-selection ambiguity on bundle candidates
- Canonically merge compatible matched memories before treating them as
  conflicting (`_select_groups_with_root_bias_buckets`,
  runtime.py:3280-3399). The policy
  `pattern_first_root_bias_then_current_transform`
  (runtime.py:3285) first buckets candidates by
  `_root_bias_contract_key` (runtime.py:3014-3043); multiple candidates
  under the same root bias do not block each other purely on
  branch/action contract differences. Conflicting root-bias contracts
  produce `root_bias_conflict` / `root_bias_conflict_best_pattern_bucket`
  / `conflicting_root_bias_contracts` audit outcomes.
- Return a rewrite plan that downstream DeepEye consumes
  (assembled around runtime.py:4876-4952; finalizer
  `_finalize_rewrite_plan_payload` adds the `guard` and audit summary
  at runtime.py:4129-4180).
- Runtime branch selection scopes the matched pattern before compiler:
  sibling branch bundles, branch-specific source antipatterns, target
  effects, target invariants, role slots, negative guards, and program
  ops are removed (`_filter_group_to_runtime_branch`,
  runtime.py:1598-1700), so `ActionCompiler` can only select candidates
  from the selected branch.
- Scope/preserve guard fields are currently audit-only
  (`build_runtime_rewrite_guard`, runtime.py:3885-3925):
  EEA emits `audited_allowed_edit_scope`, `audited_must_preserve_tables`,
  and `audited_must_preserve_predicates`, but exposes the broad
  `allowed_edit_scope=list(_EDIT_SCOPE_ORDER)` and empty preserve lists to
  avoid hard-blocking valid dependency rewrites. DeepEye still keeps
  parse/schema/execution safety checks.

Important fields on the returned plan (`prepare_rewrite_plan` payload
and finalizer):

- `case_view`, `matched_groups` / `matched_group_ids`
- `selected_branch_ids`
- `compiler_output` (with `.actions`)
- `raw_hint`, `instantiated_hint`, `repair_brief`, `hint_applicable`,
  `hint_instantiation_notes`, `hint_audit`
- `guard` (broad scope + audited fields) and `guard_scope_contract`
- `reason` ∈ {`passthrough_no_match`, `passthrough_no_action`,
  `passthrough_guard_scope_inconsistent`, `passthrough_escape_hatch`,
  `passthrough_action_count`, `passthrough_unsafe_hint`, `ready`}.
  Downstream `run_memory_rewrite_runtime` further emits `rewritten`
  after a successful rewrite (runtime.py:5086).

### `common/runtime/trigger_contract.py`

Runtime contract materialization and validation.

Responsibilities:

- Sanitize runtime trigger contracts (`sanitize_trigger_contract`,
  trigger_contract.py:124-142).
- Materialize contracts from legacy signatures when needed
  (`materialize_contract_from_legacy_signature` at
  trigger_contract.py:187-269; `ensure_materialized_trigger_contract`
  at trigger_contract.py:272-325).
- Decide whether a memory object is runtime-executable
  (`is_contract_runtime_executable`, trigger_contract.py:145-162).
- P0c (commit ee3dc87): if a SINGLETON's
  `instantiation_program.pattern_recognition_contract` carries a
  pre-condition plus a repair direction (or synthesized ops),
  `ensure_materialized_trigger_contract` sets `runtime_usable=True`
  with status `pre_condition_contract_runtime_entry`
  (trigger_contract.py:97-116 `_has_pre_condition_program`, 299-312
  branch). Singletons without that program remain blocked under
  `invalid_empty_contract`.
- Remove broad/non-substantive trigger signals from hard runtime use
  (`BROAD_RUNTIME_SIGNALS`, `_is_non_broad`, trigger_contract.py:33-38,
  119-121).

### `common/analysis/signal_summary.py`

Builds formation-time summaries and runtime trigger contracts.

Responsibilities:

- Turn question / pred / schema / repair evidence into abstract signals
  (`_program_required_pred_signals`, signal_summary.py:843-877;
  `_program_variant_required_signal_sets`, 802-840).
- Build `variant_required_signal_sets` (signal_summary.py:1163) and
  `canonical_discriminants` (signal_summary.py:1164, derived by
  `_program_canonical_discriminants` at 880-907).
- Attach runtime-facing action contract fields (block at
  signal_summary.py:1170-1198):
  - `required_role_slots`
  - `required_target_invariants`
  - `program_envelope_summary` (compact dict; the full `program_envelope`
    stays only inside the synthesized program object)
  - `selection_policy`
  - `compiler_deterministic`
    - runtime-deterministic now requires `runtime_binding_coverage >= 1.0`
      (signal_summary.py:1086-1090)
    - static `program_compile_coverage` is computed but no longer
      upgrades the runtime selection policy by itself
      (signal_summary.py:1076-1090)

### `common/analysis/role_graph_normalizer.py`

Schema-agnostic SQL role graph normalization.

Responsibilities:

- Identify output refs, predicate refs, join refs, path roles, and
  relation roles (`_relation_roles` at role_graph_normalizer.py:214,
  `_output_path_role` at 280, `_join_endpoint_path_role` at 299, plus
  the `CanonicalRoleRef` rows assembled in the main builder around
  role_graph_normalizer.py:491-540).
- Normalize role-side structure so direct and joined variants can meet
  at the same runtime contract:
  - `direct_role_path` (`_direct_role_path`, role_graph_normalizer.py:333)
  - `derived_role_path` (populated at role_graph_normalizer.py:525-529)
  - `role_side_group` (`_role_side_group`, role_graph_normalizer.py:325)
  - `side_key` (`_side_key`, role_graph_normalizer.py:317)
- Provide canonical references (`CanonicalRoleRef`) used by trigger
  contracts and canonical repair programs.
- Stay generic: no database-specific table-name or case-id rules
  (module docstring, role_graph_normalizer.py:1-6).

## 3. Action Compiler And Rewrite Path

### `common/runtime/action_compiler.py`

Code-side candidate enumeration for the current SQL.

Responsibilities:

- Enumerate schema-legal action candidates from current `S0`, local schema, and
  matched memory.
- Bind only from current-case/schema/canonical refs. No free-form LLM argument
  invention. (The action compiler node in `nodes.py` overwrites any
  LLM-supplied `arguments` with the selected candidate's bound arguments.)
- Expose candidate provenance and required edit scopes.
- Attach bundle-aware execution metadata to every candidate:
  - `bundle_id`
  - `effect_kind`
  - `bound_branch_id`
  - `cleanup_edits`
  - `counts_as_action`
  - `bundle_selection_key`

Current enumerated primitives (`_PRIMITIVE_ORDER` / `_IMPLEMENTED`):

- `ADD_SELECT_SLOT`
- `REPLACE_SELECT_SLOT`
- `DROP_SELECT_SLOT`
- `DROP_SIDE`
- `INSERT_BRIDGE`
- `REROUTE_FACT`
- `CHANGE_GRAIN`
- `MOVE_CONDITION`
- `SWITCH_CANONICAL_FIELD`
- `MATERIALIZE_RANKING_OUTPUT`

`SELECT_ENFORCE_DISTINCT` also exists in `vocabulary.ActionPrimitive`, but it
is not a standalone enumerator. It is attached as a `distinct_dependency`
rider on REPLACE_SELECT_SLOT candidates via
`_pair_output_distinct_dependency_args` (and is reflected in
`preserve_invariants` / `cleanup_edits`).

WUv2-2 binding-contract hard gate:

- `_runtime_binding_contract_for_action` reads `canonical_op.arguments.binding_contract`
  (or `runtime_binding_contract`) and returns `{}` if missing.
- `_target_output_refs_for_action` short-circuits to `[]` when no binding
  contract is present — seed target role refs are treated as evidence, not as
  executable binding.
- Target-binding primitives hard-skip with empty_reason
  `wuv2_2_seed_target_binding_disabled_pending_binding_contract` when the
  binding contract is missing: `ADD_SELECT_SLOT`, `REPLACE_SELECT_SLOT`,
  `REROUTE_FACT`, `CHANGE_GRAIN`, `MATERIALIZE_RANKING_OUTPUT`.
  `DROP_SELECT_SLOT` / `DROP_SIDE` / `INSERT_BRIDGE` / `MOVE_CONDITION` /
  `SWITCH_CANONICAL_FIELD` do not bind to target output refs and are not
  gated by this contract.
- The binding contract is materialized at pattern admission, not at extract
  time and not at runtime: `learning/pattern_formation.py::_build_pattern_candidate`
  computes the literal-free admission binding payload
  (`_runtime_binding_contract_payload`) and writes it onto every canonical op
  via `learning/shared_program_synthesizer.py::attach_binding_contract_to_program`.
  Without admission-level wiring, the compiler refuses to bind seed targets.

REROUTE_FACT / INSERT_BRIDGE contract precision (known gap, 2026-05-18):

- `_enumerate_reroute_fact` (action_compiler.py:3511-3575) produces candidates
  with `target_relation_edges` (the joins to ADD) and uses
  `_variant_requires_relation_reroute` (action_compiler.py:3492-3508) to verify
  S0 has the source join and lacks the target join. The compiler also produces
  bridge hint candidates from `target_added_relation_equality` invariants
  (commit 7d2b57a, `_bridge_hint_candidates_from_target_edges`).
- The `raw_hint` constructed from REROUTE_FACT carries `target_relation_edges`
  as JSON plus preserve-invariant tags. It specifies what to ADD but does NOT
  specify: (a) which join(s) in S0 to REMOVE, (b) which column references to
  REMAP (e.g., `res.position` → `ds.position`). The `relation_delta.removed`
  edges are present in the branch `source_antipatterns` but are not propagated
  to the rewrite contract.
- `run_hint_instantiation` LLM compresses the raw_hint to natural language
  ("Join X with Y, preserve existing filter"), further losing the precise
  add/remove/remap specification.
- Contrast with DROP_SIDE / DROP_SELECT_SLOT: their rewrite contracts are
  precise subtractions ("drop column X, drop JOIN Y ON Z") that the rewrite LLM
  can execute reliably. toxicology achieves 67% conversion (6/9 ready→helped)
  with these precise contracts. formula_1 achieves 0% conversion (0/11) with
  the imprecise REROUTE_FACT contract.
- To close this gap, the contract builder needs to: (1) read
  `relation_delta.removed` from the branch antipattern and identify the
  corresponding join clause in S0, (2) compute column-reference remapping from
  the removed table to the added table, (3) emit these as explicit
  `required_removal_edits` and `required_remap_edits` alongside the existing
  `target_relation_edges`.

### `common/llm/nodes.py`

LLM wrapper layer for the current v2 stack.

Responsibilities:

- Build compact prompt payloads.
- Call `common/llm/utils.py`.
- Parse LLM JSON back into typed v2 contracts.

Current runtime-facing node behavior:

- `run_wrong_case_auditor`
  - emits `candidate_fix_sql`
  - code side may later validate it into `validated_sql`
- `run_error_instance_extractor`
  - emits executable repair hypotheses plus hypotheses about source
    antipatterns / target invariants / uncertain axes
  - canonical authority stays in `common/analysis/repair_program_normalizer.py`
- `run_action_compiler`
  - LLM is a selector over code-enumerated candidates; `arguments` are always
    sourced from the selected candidate, never from the LLM's free-form output
  - deterministic unique preselection can bypass the LLM
    (`_deterministic_unique_preselection`)
  - deterministic fallback (`_deterministic_canonical_fallback_actions`)
    keys on `bundle_id` and `synthesized_program_bundle_ids`, not on raw
    `canonical_op_id`
  - no post-selection dependency action is appended after bundle selection;
    cleanup stays inside the selected bundle contract
    (marker `bundle_cleanup_dependency_audit_only` in `selection_summary`)
  - prompt schema context is candidate-linked
    (`_candidate_linked_schema_prompt_payload`): it keeps the current SQL role
    refs, candidate argument refs, related tables, related columns, related
    FK/PK edges, and matching semantic hints; full schema is used only as a
    fallback when no candidate/schema refs are available
- WUv2-2 prompt sanitization
  - `_compact_repair_program_steps_for_runtime_prompt` strips seed-binding
    argument keys (`alias`, `column`, `from_expr`, `target_table`,
    `target_output_refs`, `predicate`, ... ~40 keys) from any source-case
    `repair_program` steps before they reach the compiler/rewrite prompts.
  - `_memory_objects_prompt_payload` no longer inlines `repair_program`; it
    emits `repair_program_omitted_from_prompt` with the omitted count.
  - `_rewrite_contract_prompt_payload` records unbound dependency steps as
    `dependency_edits_omitted` instead of inlining them as executable edits.
- `run_memory_rewrite`
  - actions are authoritative
  - `natural_language_hint` is explanatory only and cannot fill missing args
  - code side fail-closes when any required action is missing a trace, has
    multiple traces, lacks required bindings, is realized without edits, or
    is returned as unrealized (`_fail_closed_rewrite_contract`).
    `required_absence_checks` and `required_presence_checks` are independently
    re-verified post-rewrite; failure reverts to `S0`
    (`_enforce_rewrite_contract_absence_checks`).
- `run_hint_instantiation`
  - readability helper only
  - does not decide applicability
  - Semantic audit lives in `runtime/runtime.py::_hint_instantiation_semantic_audit`
    (table `_PRIMITIVE_CORE_VERBS`). It checks that the rewritten hint
    preserves at least one core verb per emitted action primitive, surfaces a
    `hint_audit` block on the runtime audit summary, and never rewrites,
    rejects, or falls back on its own.

### `common/llm/prompts/`

Prompt builders for current nodes plus learning- and admission-side prompts.

Runtime / accumulate-path prompts:

- `wrong_case_auditor.py`
  - produces `candidate_fix_sql`, `minimal_patch_ops`,
    `secondary_differences`
- `error_instance_extractor.py`
  - produces repair hypotheses and runtime-facing hypotheses
- `action_compiler.py`
  - only selects from exact executable candidates
- `memory_rewrite.py`
  - realizes already-structured actions into SQL edits
- `hint_instantiation.py`
  - rewrites a code-rendered action brief into a readable hint
- `pattern_pre_condition_match.py`
  - runtime Q-side and S-side pre-condition matching prompts
    (`build_pattern_pre_condition_q_prompt` /
    `build_pattern_pre_condition_s_prompt`), wired from `runtime/runtime.py`

Learning / admission / offline-judge prompts:

- `compatibility_judge.py`
  - advisory-only explainer
  - not a hard gate for runtime or promotion (no node currently invokes it)
- `insight_pattern_slicer.py`
  - slices a candidate group by repair insight before the admission judge
    (called from `learning/pattern_formation.py`)
- `pattern_admission_judge.py`
  - budgeted admission judge that decides pattern promotion under a
    representative-pair-coverage sampling policy (called from
    `learning/pattern_formation.py`)
- `pattern_equivalence_judge.py`
  - equivalence judge for two pattern candidates (called from
    `learning/pattern_formation.py` and `learning/evolution.py`)
- `shared_insight_judge.py`
  - shared-insight check used inside shared-program synthesis (called from
    `learning/shared_program_synthesizer.py`)
- `schema_role_annotator.py`
  - schema role-family annotation prompt (called from
    `analysis/schema_role_annotator.py`)

## 4. Update / Audit / Memory Construction

### `common/learning/accumulate.py`

Wrong-case update entry. Entry function `accumulate_wrong_case` is the only
gold-aware path in EEA v2.

Responsibilities:

- Accept wrong `S0` update requests (`accumulate_wrong_case`).
- Run the one-case offline pipeline (`run_error_instance_pipeline`): code
  preprocessing, wrong-case audit, error-instance extraction.
- Build the singleton GroupSummary inline (`error_instance_to_singleton`):
  attach canonical repair IR, synthesize the singleton-level repair program,
  derive program coverage, derive repair card, populate
  `formation_signals.canonical_repair_ir / synthesized_program /
  program_coverage / repair_card / repair_insight_signature`, and write the
  per-case `InstantiationProgram` fields:
  - `synthesized_program` (CanonicalRepairProgram built from the IR)
  - `program_coverage`
  - `pattern_recognition_contract` (the current name; the legacy
    `bias_recognition_contract` is retained on
    `data_structures.InstantiationProgram` only as a deserialization
    compatibility field)
  - `slots`, `branch_rules`, `repair_program`, `template`
- Merge the singleton into `LibraryStateV2.singletons`
  (`append_singleton_to_library`).
- Filter execution-only pred tags out of `TriggerSignature.required_pred_tags`
  via `_runtime_visible_tags` so the runtime can actually retrieve the
  singleton (execution-only tags are still kept in `core_interface` for
  offline analysis).
- Maintain a per-`db_id` consecutive-failure streak; raise `SystemExit` when
  it crosses `EEA_ACCUMULATE_FAIL_FAST_AFTER` (default 3, set to `0` to
  disable).

Online enrichment is performed **at wrong-case accumulation time**: by the
time `accumulate_wrong_case` returns, the new singleton already carries
`instantiation_program.synthesized_program`,
`instantiation_program.pattern_recognition_contract`, and the matching
`formation_signals` payload. Local-evolve / finalize does not need to
backfill these fields on the singleton itself.

Regression negative feedback (`append_regression_negative_guard`,
`accumulate.py:534-620`) handles the `S0 correct -> final wrong` case by
appending a `HistoricalRegressGuard` to each matched pattern's
`pattern_recognition_contract.applicability.regression_negative_guards`. The
guard write path is still wired (used by WUv2-5c update), but the runtime
consumption path was deferred in commit `d4532d0` ("Defer WUv2-5b negative
guard runtime path"); `ApplicabilityContract` in
`common/core/data_structures_v2.py` documents this state explicitly. No
runtime reader currently consumes the guard payload until the negative-guard
semantics are redesigned.

### `common/learning/case_pipeline.py`

One-case offline/update pipeline. Entry function `run_error_instance_pipeline`
returns `ErrorInstancePipelineOutput(error_instance, runtime_case_view,
case_audit, code_prepared_summary, compiler_output?)`.

Responsibilities:

- Execution comparison between `pred_sql` and `gold_sql` (`run_execution_comparison`),
  unless `skip_auto_execution=True` or the caller passes a precomputed
  `execution_comparison`.
- Deterministic preprocessing via `code_preprocess.preprocess_case`
  (AST parsing, structure flags, delta signature, candidate question/pred
  tags, local schema view).
- Build an answer-blind `RuntimeCaseView` from `code_prepared`.
- Wrong-case audit via `llm.nodes.run_wrong_case_auditor`.
- Error-instance extraction via `llm.nodes.run_error_instance_extractor`.

Canonical repair normalization is **not** performed inside this pipeline; it
runs downstream inside `accumulate.error_instance_to_singleton` via
`analysis.repair_program_normalizer.attach_canonical_repair_ir`.

Important behavior:

- If the auditor emits `candidate_fix_sql` and no `validated_sql` is already
  set, the pipeline executes the candidate against the database and only
  promotes it to `validated_sql` when `row_sets_equivalent` is true.
- An optional `run_compiler=True` flag (dev-only) builds a transient dev
  singleton via `error_instance_to_singleton` and runs `run_action_compiler`
  against an answer-blind `RuntimeCaseView`. The accumulate path calls with
  `run_compiler=False`.

### `common/analysis/code_preprocess.py`

Deterministic preprocessing before LLM audit/extraction.

Responsibilities:

- Parse pred/gold SQL structure (`compute_structure_bundle`, reusing
  `analysis.structure_family.cached_ast_signature` and
  `analysis.structure_delta.build_structural_delta`).
- Build local schema views (`io.local_schema.build_local_schema_view`).
- Build answer-blind case signal views (`build_case_signal_view`,
  `build_case_signal_bundle`).
- Build offline-only `DeltaSignature` (with execution-derived tags such as
  SELECT_ARITY_MISMATCH allowed only on the offline path).
- Extract candidate question/pred tags (`extract_candidate_question_tags`,
  `extract_candidate_pred_tags`).

### `common/analysis/structure_delta.py`

Deterministic pred-vs-gold structural delta. 26-dim `DeltaFlags`.

Responsibilities:

- Compute generic SQL structural differences without case-specific rules
  (`compute_delta_flags`, `build_structural_delta`).
- Provide `JoinEdge`, `DeltaFlags`, and `StructuralDelta` dataclasses used by
  `code_preprocess` and downstream `delta_signature` consumers.
- Feed downstream audit/extraction/normalization (the resulting
  `delta_signature` is attached to `CaseAudit` and read by
  `repair_program_normalizer`).

### `common/analysis/repair_program_normalizer.py`

Canonical repair IR extraction. Entry function
`attach_canonical_repair_ir` (line 1599); class `RepairProgramNormalizer`
(line 1261). Called inline from `error_instance_to_singleton` when a wrong
case is accumulated, so the resulting `CanonicalRepairIR` is attached to the
`ErrorInstanceV2` before the singleton GroupSummary is built.

Responsibilities:

- Convert extracted repair hypotheses into canonical repair ops
  (`CanonicalRepairOp` with literal-free `role_refs`, plus `core_ops` /
  `accessory_ops` splits).
- Call `analysis.contrastive_repair_effect.discover_contrastive_repair_effects`
  on the audited pred-vs-target trace; store the resulting effects under
  `repair_effect_signature["effect_candidates"]`.
- Populate per-op `arguments` with `operation_signature`, which in turn
  carries the four directional deltas:
  - `output_path_delta`
  - `relation_delta`
  - `predicate_scope_delta`
  - `grain_delta`
- Populate the IR-level fields:
  - `repair_effect_signature` (including the embedded `effect_candidates`)
  - `target_invariants` (relation invariants + per-op `target_*` invariants)
  - `invariants` (union of all per-op invariants)
  - per-op `accessory_policies` (target ranking / target predicate /
    distinct accessory policies)
  - `role_refs` on each op (output-side refs only; target-side refs go
    through `_strip_target_role_ref_literals` to stay literal-free)
- `unresolved_variation_axes` is kept on the IR schema but left empty for
  normal case-level singletons (see `CanonicalRepairIR` docstring in
  `common/core/data_structures.py`).

### `common/analysis/contrastive_repair_effect.py`

Phase-1 offline effect discovery. Entry function
`discover_contrastive_repair_effects`.

Responsibilities:

- Convert one audited repair trace into `ContrastiveRepairEffect`
  candidates.
- Use only case-local SQL/role-graph deltas, execution/audit hypotheses,
  and schema-derived roles.
- Emit effects exclusively from the closed axis set declared as
  `VALID_EFFECT_AXES` in this module: `output_shape_delta`,
  `multi_output_contract_delta`, `grain_delta`, `aggregation_unit_delta`,
  `source_route_delta`, `predicate_scope_delta`, `role_anchor_delta`,
  `temporal_scope_delta`, `proxy_slot_delta`, `storage_contract_delta`,
  `formula_delta`, `ranking_contract_delta`.
- Keep concrete table/column/path details as evidence inside abstracted
  refs (`_ref_abstract`, `_refs_summary`), not as compatibility rules.
- Never use case ids, database names, manual labels, fixed column-pair
  rules, or weighted similarity. Effect ids are deterministic fingerprints
  (`_stable_effect_id`), not discriminators inside axis logic.

## 5. Shared Program / Singleton / Pattern / Promotion

### `common/learning/pattern_formation.py`

Build strict pattern candidates from singleton repair traces.

Responsibilities:

- Keep `singleton` as the default memory object for every accumulated wrong
  case.
- Form `pattern` candidates only from active singletons with case-local repair
  insight, contrastive effect candidates, and canonical repair IR.
- Do not use legacy question/manifest/structural/slot overlap as promotion
  evidence. If required formation signals are missing, keep the object as a
  singleton and report `signal_missing`.
- Use pair scores only for candidate retrieval and audit. Pattern admission LLM
  sees compact case cards plus per-relation pair counts and representative
  pair decisions, not the O(n^2) pair matrix.
- Build patterns root-first:
  - component retrieval uses case-derived effect/source/target signals
    via union-find over `branchable_for_pattern` edges that pass
    `_pair_supports_root_membership`
  - root admission decides shared source misconception / target preference
  - admitted patterns carry a structured `pattern_recognition_contract`
    (`PatternRecognitionContract`, an alias of `PatternRecognitionContractV2`)
    stored on `InstantiationProgram.pattern_recognition_contract`. The
    contract has three sub-blocks:
    - `recognition`: question/sql preconditions, `grounded_anchors`,
      `observed_failure_summary`, `repair_direction` (runtime-readable
      preconditions plus audit metadata).
    - `applicability`: `intent_description` (audit-only) and a
      `regression_negative_guards` list. WUv2-5b deferred the runtime use of
      these guards: the schema field is preserved for logged negative
      feedback, but the runtime path does not currently consume them and
      admission only emits an audit-only `intent_description`.
    - `binding`: literal-free `source_slots / target_slots`,
      `allowed_operations` (ActionPrimitive values only),
      `preserve_invariants`, and `evidence`.
    Legacy `pre_question_signature / pre_sql_signature / pre_*_self_check /
    observed_failure_summary / repair_direction` mirror fields are still
    written so the current runtime trigger path keeps working until the
    five-layer contract takes over (`PatternRecognitionContractV2`
    `_migrate_legacy_payload`). The previous v1 closed-vocabulary
    `BiasRecognitionContract` schema (3-6 closed phenomenon signals) remains
    in `data_structures.py` only for loading old libraries and is not
    written by new admission. The recognition contract is stored on
    `InstantiationProgram`, not used as an executable rewrite program.
  - after admission, the literal-free `binding` contract is wired into every
    canonical op (`_attach_runtime_binding_contract` ->
    `attach_binding_contract_to_program` in
    `shared_program_synthesizer.py`). Each `op.arguments` then carries
    `binding_contract`, `runtime_binding_contract`, `canonical_op_id`,
    `canonical_op_type`, and a `canonical_op` meta dict; the
    `program_envelope` is rebuilt from the updated ops. This is the runtime
    binding metadata the action compiler relies on (WUv2-2 disabled
    literal-seed target binding).
  - `core_program_signature`, DISTINCT, join cleanup, route/grain/action
    differences are branch/accessory evidence after root admission, not
    pre-admission split keys
  - every recalled component member is reported as
    `accepted_root_by_judge`, `rejected_root_by_judge`,
    `retrieved_but_not_admitted`, or mechanically root-closed into a branch
    slot via `_root_membership_closure`
  - mechanical branch closure is generic: it uses branchable pair evidence
    from the case's own repair trace and never DB/table/qid-specific rules
- Pair score computation is cached by `_pair_score_cache_key`, which hashes
  the compact `evolution_card` of each singleton, not full
  `formation_signals` / `trigger_contract`. The insight slicer receives at
  most representative pair decisions by relation and case coverage, not a
  full component pair matrix.
- Root closure is intentionally conservative: `compatible` pairs can close a
  root; `partial` pairs need explicit strong root evidence
  (`shared_primary_repair_locus` or
  `shared_root_effect_axis_with_same_target_invariant_family`); veto /
  absolute-conflict relations cannot mechanically absorb members.
- Keep `experience_families` empty in the evolved library. The schema field
  may exist for compatibility, but it is not a runtime or promotion source.

### `common/learning/shared_program_synthesizer.py`

Synthesizes shared canonical repair programs.

Responsibilities:

- Build `CanonicalRepairProgram` from compatible ops.
- Effect-first synthesis is the primary shared-program path.
  - shared ops are matched by `RepairEffectSignature.effect_candidates`
  - compatibility compares abstract effect axis/source/target/delta/actionability, not exact SQL patch or concrete column path
  - legacy `legacy_exact_op` / `legacy_generalized_op` /
    `legacy_invariant_envelope` bases still exist as fallbacks inside
    `SharedProgramSynthesizer.synthesize`, but when the caller (notably
    `_build_pattern_candidate`) passes `require_effect_program=True`, any
    synthesized program missing `repair_effect_signature.effect_candidates`
    is marked with the `shared_program_lost_effect` blocker and cannot
    produce a usable pattern program.
- Preserve and merge effect-level structure:
  - `ContrastiveRepairEffect`
  - `RepairEffectSignature.effect_candidates`
  - bundle-aware `action_envelope`
- Preserve delta-carrying signature sections through shared synthesis:
  - `output_path_delta`
  - `relation_delta`
  - `predicate_scope_delta`
  - `grain_delta`
- Attach `program_envelope` (`_program_envelope_from_ops`):
  - `source_antipatterns`
  - `target_effects`
  - `target_invariants`
  - `allowed_action_primitives`
  - `action_envelope`
    - `bundles`
    - `max_actions_hint`
  - `lowering_branches`
  - `runtime_branches`
    - `branch_id` (admission spec id if present, otherwise a stable hash from
      semantic shape via `_branch_id_for_bundle`; never derived from case ids)
    - `bundle_ids` / `bundled_op_ids` / `cleanup_op_ids`
    - `support_case_ids`
    - `required_signals`, `negative_signals`
    - `required_role_slots`
    - `allowed_primitives`, `allowed_edit_scope`, `preserve_constraints`
    - `source_antipatterns`, `target_effects`, `target_invariants`
    - `negative_guards`
    - `runtime_usable`, `runtime_blockers`, `replay_metrics` (filled later
      by promotion)
  - `branch_selection_contract`
  - `required_role_slots`
  - `negative_guards`
  - `unresolved_variation_axes`
- `runtime_branches[*].required_signals` come from two sources, in this order:
  1. `_required_signals_for_branch` derives generic structural facts from the
     branch's primary op + bundle (`pred.select_arity=<n>`,
     `pred.select_arity_present=True`, `pred.pair_output=True`,
     `pred.role_side_pair_output=True`).
  2. After pattern admission, `_merge_branch_rows_for_admission_spec`
     overlays branch-spec rows whose `required_signals` are the intersection
     of `pred.*` manifest tags across the branch's member singletons
     (`_branch_spec_required_signals`).
  Known gap: when every member of a pattern shares the same `pred.*`
  manifest (e.g. all branches in a pattern keep `select_arity=1`), every
  branch ends up exposing the same `required_signals` list. The LLM-authored
  `admission_branch_spec.required_interface_delta` is natural-language audit
  text and is **not** projected into `required_signals`. Branch selection at
  runtime currently distinguishes branches by binding/op-shape rather than
  by required_signals in this case.
- Emit generic lowering families and allowed primitive sets.
- `attach_binding_contract_to_program` (called from
  `pattern_formation._attach_runtime_binding_contract` after admission)
  carries the admission `binding` payload (`source_slots / target_slots /
  allowed_operations / preserve_invariants / evidence`) onto every
  canonical op's `arguments`, alongside `canonical_op_id /
  canonical_op_type / canonical_op` meta, and rebuilds the
  `program_envelope` from the updated ops.

### `common/learning/program_coverage.py`

Coverage validator for shared programs.

Responsibilities:

- Separate:
  - `static_program_coverage` (`validate_program`: static lowering-family
    check only)
  - `runtime_binding_coverage` (`validate_runtime_bindings`: runs the action
    compiler `enumerate_candidates` against each member RuntimeCaseView)
  - `member_candidate_coverage`
- Preserve backward-compatible `compile_coverage`
  - if runtime binding ran, it reflects `runtime_binding_coverage`
  - otherwise it falls back to static coverage

### `common/learning/evolution.py`

Online and final memory evolution.

Responsibilities:

- Local post-update evolve
- final evolve and freeze
- replay-gated formal pattern decisions (family layer is disabled — every
  `evolve_serial` call forces `working_library.experience_families = []`)
- online evolve keeps singleton and pattern memories only
- full-pattern replay is diagnostic, while runtime exposure is controlled by
  branch-level replay/binding status. When a replay case loader is available,
  each evolved prefix runs branch-scoped member replay before a branch can
  become runtime visible.
- `focus_case_ids` is audit-only at this layer too: it is propagated into
  `formation_audit` for reporting but does not filter pair-score or
  component pools.

### `common/learning/promotion.py`

Replay-based promotion logic.

Responsibilities:

- Promotion tests
- leave-one-out / replay accounting
- promotion blockers
- final runtime/formal state transitions
- branch-level runtime admission:
  each formal pattern carries per-branch replay status into
  `program_envelope.runtime_branches`; runtime never selects an unvalidated
  branch. Branch admission is computed from `branch_member_replay`: EEA first
  builds a branch-scoped memory containing only that branch's bundles/contracts,
  replays the branch's support cases, and checks branch execution directly.
  In this mode, contract validation does not reuse whole-group member coverage,
  because branch replay is the coverage test. A branch becomes runtime usable
  when every support row selects/binds the same branch, compiles with at least
  one action, has no comparison unknown and no regression, and at least one
  support row shows improvement. Whole-pattern blockers remain audit metadata;
  runtime visibility is determined by whether at least one branch is
  replay-usable (`runtime_usable_branch_count` >= 1, group-level
  `runtime_usable = bool(usable_count)`).
- runtime-usable branch support controls which source singletons are
  superseded. If a pattern has only some usable branches, unsupported branch
  source singletons remain active instead of being removed by the root pattern
  (`_runtime_usable_branch_support_case_ids`).
- keep replay-derived `ProgramCoverage` split:
  - `static_program_coverage` remains the synthesis/static view
  - `runtime_binding_coverage` reflects replay/runtime binding evidence
  - `compile_coverage` stays as the backward-compatible alias
- cache replay rows by memory hash + holdout case + replay mode + the
  training case prefix + db path + case-record hash (see
  `_promotion_replay_cache_key`) to avoid repeatedly replaying unchanged
  prefix memories during online local evolve.

> Note: same-root runtime singleton/pattern conflict resolution
> (`ambiguous_current_transform`, root-bias contract key, current-case
> transform keys via the compiler enumerator, blocking the case when no
> shared transform exists across the passed memories) lives in
> `common/runtime/runtime.py` (`_root_bias_contract_key`,
> `_candidate_transform_key`, `_current_transform_keys_for_group`,
> `_select_groups_with_shared_current_transform`), not in
> `promotion.py`. Promotion only decides whether a branch / pattern is
> runtime-visible; the conflict resolution at trigger time is a runtime
> concern.

## 6. Core Contracts And Shared Types

### `common/core/data_structures.py`

Primary Pydantic contracts (60+ classes). Important structures the rest of
the stack reads:

- `CaseAudit`
  - includes offline `effect_axis_hint`
- `ErrorInstanceV2`
  - includes advisory `possible_effect_axes`, `repair_insight_signature`,
    `canonical_repair_ir`, `source_antipattern_hypothesis`,
    `target_invariant_hypothesis`, and structured `repair_program`
- `ContrastiveRepairEffect`
- `RepairEffectSignature`
  - includes Phase-1 `effect_candidates`
- `CanonicalRepairIR`
- `CanonicalRepairProgram`
- `ProgramEnvelope`
  - carries `runtime_branches`, `branch_selection_contract`, `required_role_slots`,
    `repair_insight_signature`, and `negative_guards`
- `ProgramCoverage`
- `ActionCandidateSet`
- `Action`
- `RuntimeCaseView`
- `InstantiationProgram`
  - holds `synthesized_program`, `program_coverage`, and the recognition
    contracts; `bias_recognition_contract` is kept only for loading old
    libraries
- `BiasRecognitionContract` (legacy, loader-only)
- `PatternRecognitionContract`
  - compatibility alias for `PatternRecognitionContractV2`
- `TriggerContract`
  - carries `runtime_branches`, `variant_required_signal_sets`,
    `canonical_discriminants`, `decisive_pred_signals`, and the
    `trigger_policy` (binder dry-run, generalization gates)
- `TriggerCandidateAudit`
  - branch-level fields (`selected_branch_id`, `matched_branch_ids`,
    `branch_match_audit`, `branch_blockers`, `branch_runtime_usable_count`)
    and the two-layer signal classification
    (`source_trigger_passed`, `hard_gate_reasons`,
    `deferred_instantiation_reasons`, `compiler_candidate_reasons`),
    plus recognition audit fields (`bias_recognition`, `bias_recognized`,
    `pre_condition_match`, `pre_condition_matched`,
    `singleton_strict_audit`)
- `GroupSummary`
  - adds `runtime_contract_status`, `runtime_blockers`,
    `formation_evidence`, `trigger_policy`, and `lifecycle`
- `LibraryStateV2`

### `common/core/data_structures_v2.py`

WUv2-5a contract split. Imported back into `data_structures.py` so existing
runtime code keeps reading the legacy mirror fields:

- `GroundedAnchor`
  - code-checkable evidence anchor consumed before LLM recognition
- `RecognitionContract`
  - answer-blind preconditions: `question_precondition`,
    `sql_precondition`, `grounded_anchors`,
    `question_self_check`, `sql_self_check`,
    `observed_failure_summary`, `repair_direction`
- `HistoricalRegressGuard`
  - negative guard learned from a real runtime regression; carried in
    `ApplicabilityContract.regression_negative_guards`. WUv2-5b deferred
    the runtime path; admission only writes the guard as logged
    feedback and the runtime trigger does not consume it yet
- `ApplicabilityContract`
  - currently writes only `intent_description` as audit; the
    `regression_negative_guards` list is retained for negative feedback
- `BindingSlot`
  - literal-free compiler slot (`kind`, `role_family`, `path_role`,
    `relation_role`, `answer_unit_role`, `optional`, `constraints`)
- `BindingContract`
  - `source_slots`, `target_slots`, `allowed_operations`,
    `preserve_invariants`
- `PatternRecognitionContractV2`
  - three-contract wrapper (`recognition / applicability / binding`).
    A `model_validator` migrates old payloads from the legacy
    `pre_question_signature / pre_sql_signature / *_self_check /
    observed_failure_summary / repair_direction` fields, and the
    after-validator keeps those legacy mirrors in sync so existing
    runtime code continues to work

### `common/core/schema.py`

Auxiliary Pydantic types used by `refine/` and the analysis stack:
`IntentLevel`, `ErrorOperator`, and related schemas. Not the active EEA
runtime contracts but shared infrastructure for the SQL parser and
offline audits.

### `common/core/config.py`

Runtime configuration loader. Exposes `LLMSettings`, `load_config`, and the
default/legacy config paths (including the DeepEye api-profile fallback path).

### `common/core/vocabulary.py`

Shared enums and constants.

Contains:

- repair-skeleton enums (`Locus`, `OpFamily`, `TargetFamily`, `OutputContract`)
- question / answer enums (`QuestionTargetRole`, `AnswerSlotType`,
  `GrainType`, `OpType`, `RouteCue`, `PredManifestationType`)
- action primitives (`ActionPrimitive`)
- edit scopes (`EditScope`)
- group lifecycle/status enums (`GroupType`, `GroupStatus`)
- risk/confidence labels (`RiskLevel`, `Confidence`)
- the answer-blind filter set `EXECUTION_ONLY_PRED_TAGS` (drives
  `accumulate._runtime_visible_tags`)
- `SCHEMA_RECALL_MISS_CATEGORIES` for compiler diagnostics
- threshold constants:
  `PROMOTION_SUPPORT_MIN`, `MAX_ACTION_COUNT_PER_CASE`,
  `SINGLETON_MAX_ACTIONS`, `MAX_REPLAY_REGRESSION`,
  `RISK_HIGH_BRANCH_COUNT`, `RISK_HIGH_FREE_SLOT_COUNT`,
  `PROMOTION_REQUIRE_LEAVE_ONE_OUT`,
  `GLOBAL_CONFLICT_SCAN_EVERY_N_CASES`, plus the calibrated TBD
  weights collected in `TBD_THRESHOLDS`

## 7. Schema / Execution / Utilities

### `common/io/local_schema.py`

Builds local schema views for each case. Defines the `DBSchemaAccess` Protocol
and the `LocalSchemaView` / `LocalSchemaViewDiagnostics` constructor surface.

### `common/io/db_schema_access.py`

SQLite schema access helpers. Provides `SqliteDBSchemaAccess`, which reuses
DeepEye's `app.db_utils.schema` (with its BIRD-specific fixes and LRU cache)
when importable, and falls back to an in-module sqlite implementation
otherwise.

### `common/io/execution_compare.py`

Execution comparison utilities. Runs `pred_sql` and `gold_sql` and produces an
`ExecutionComparison`. Offline only — never used by the answer-blind runtime
path.

### `common/llm/client.py`

OpenAI-compatible client wrapper used by `utils.py`.

### `common/llm/utils.py`

Shared OpenAI-compatible JSON LLM caller. Loads config via
`common.core.config.load_config`, applies proxy-env scrubbing, and enforces a
hard timeout on synchronous calls.

### `common/llm/nodes.py`

LLM-call nodes (auditor / extractor / compiler / rewrite / hint). Hosts the
`rewrite_contract` payload assembly, the fail-closed contract checks
(`required_absence_checks` / `required_presence_checks`), and the prompt
audit instrumentation.

### `common/llm/prompts/`

Prompt template directory consumed by `nodes.py`.

### `common/analysis/structure_family.py`

Shared SQL AST/structure helper used by v2 and retained parser utilities. Imports
`parse_ast_signature` from `refine/signals.py`.

### `common/core/schema.py` and `refine/`

Retained shared parser/legacy support. `common/core/schema.py` exposes
`IntentLevel`, `ErrorOperator`, and related Pydantic types. `refine/` keeps
the legacy SQL parser stack (`agent.py`, `checker.py`, `prompts.py`,
`session.py`, `signals.py`, `sql_parser.py`, `structural_probes.py`,
`utils.py`). They are not the current EEA entry point, but parts of the
current code still reuse that parser stack.

## 8. CLI Entry Points

Current v2 CLIs (all under `rulebook/cli/`):

- `cli/run_online_e2e_validation.py`
  - online end-to-end harness; processes cases serially from an empty library
- `cli/run_multidb_validation.py`
  - manifest-driven multi-DB harness; separates cheap answer-blind trigger
    replay from the expensive compiler/rewrite replay
- `cli/replay_runtime_trigger.py`
  - offline trigger replay over saved per-case rewrite inputs; never calls an
    LLM
- `cli/replay_runtime_trigger_v2.py`
  - post-selection trigger replay reading saved `eea_runtime_request.json`
    files from DeepEye post-selection runs; calls `prepare_rewrite_plan` for
    audit but does not call the rewrite LLM
- `cli/replay_runtime_rewrite.py`
  - offline trigger -> compiler -> hint -> rewrite replay on saved cases
- `cli/replay_runtime_rewrite_v2.py`
  - post-selection trigger-through-rewrite replay over saved
    `eea_runtime_request.json` payloads
- `cli/replay_manual_pattern_compiler.py`
  - offline compiler replay for manual/formal pattern candidates; bypasses
    runtime trigger
- `cli/offline_pattern_formation.py`
  - offline family-formation check
- `cli/build_library_from_work.py`
  - builds a v2 singleton library from saved DeepEye work cases
- `cli/convert_manual_patterns.py`
  - converts manually labeled formal patterns into offline-only v2 pattern
    candidates (`runtime_usable=False`)
- `cli/audit_run.py`
  - builds human-readable audit tables for an EEA v2 single-db run
- `cli/audit_pattern_signatures.py`
  - dumps compact pattern signatures from a library or run work root
- `cli/evaluate_manual_pattern_trigger_loo.py`
  - leave-one-out evaluation of runtime triggering against manually labeled
    formal patterns

Removed old v1 cluster pipeline:

- `online/`
- `distill/`
- old trace clustering and pattern distill CLIs

Retained intentionally:

- `refine/`

(`presentation/` is no longer present in the repository; the CLAUDE.md
project doc still permits keeping it but the directory has already been
removed.)

Reason:

The current EEA system is built around v2 update + runtime trigger/compiler +
replay-gated evolution, not the old v1 cluster pipeline.

