# Current EEA Implementation Overview

This document is a fast map of the current EEA codebase. It reflects the
post-selection integration path and the current runtime/update/evolve stack.

## 1. End-to-End Flow

```text
DeepEye completes generation / revise / selection and chooses S0
  -> DeepEye sends S0 + question/evidence + C0 candidate context to EEA runtime
  -> EEA builds RuntimeCaseView and current runtime signals
  -> EEA checks memory trigger contracts in LibraryStateV2
  -> If no runtime-usable memory passes the gate: return no_match / no_action
  -> If memory passes: enumerate schema-legal action candidates on current S0
  -> ActionCompiler selects bounded actions
  -> EEA renders a rewrite hint from those actions
  -> DeepEye rewrites only S0 into S1 and guards S1
  -> DeepEye selects between S0 and S1
  -> If S0 is wrong, DeepEye sends update request to EEA
  -> EEA audits S0 vs gold, extracts an ErrorInstance, normalizes it into memory
  -> EEA accumulates singleton memory
  -> EEA runs a full-prefix local evolve over the current LibraryStateV2
  -> Replay-gated promoted memories become visible to the next runtime case
  -> End of database: EEA runs replay-gated final evolve/freeze
```

Runtime is answer-blind. Gold SQL and execution comparison are used only by
update/evolution, never by runtime trigger/compile/rewrite.

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

`focus_case_ids` is now audit-only in the DeepEye adapter. It records which new
case caused this update, but it must not filter the clustering/evolution
candidates. This avoids the previous failure mode where q249/q253 could only
see their own singleton during online update, while offline clustering could see
q206/q249/q253 together.

Online local evolve only promotes a strict pattern to runtime use when it can
construct a replay case loader from the run work directory. If `work_root` or
`db_path` is missing, EEA keeps the newly accumulated singleton but does not
manufacture runtime-usable group memory.

Existing runtime memories are preserved unless a replay-gated promoted object
explicitly supersedes them. A new singleton can be absorbed into an existing
pattern only through the same shared repair program and replay validation path;
non-matching cases do not invalidate the old pattern.

The replay evolution boundary now consumes strict pattern candidates only.
Experience-family candidates are no longer generated, promoted, or triggered.
Offline pattern candidates may coexist with their source singletons until replay
promotion proves that the pattern is safe for runtime use.

## 2. Runtime Path

### `common/runtime/runtime.py`

Main answer-blind runtime entry.

Responsibilities:

- Build `RuntimeCaseView` from question, evidence, selected SQL `S0`, schema,
  and `c0_candidates`.
- Build current runtime-visible signals from the current SQL and local schema.
- Build closed-vocabulary `bias_recognition_signals` for pattern recognition,
  such as `has_pair_role_side_output`, `select_arity_ge_2`,
  `no_distinct_on_pair_output`, aggregate cues, route cues, and order/group
  cues. These signals answer only whether current `S0` shows the same root
  bias; they do not choose the concrete repair.
- Gate memory objects through three layers:
  - lightweight bias recognition for `pattern` memories that carry
    `InstantiationProgram.bias_recognition_contract`
  - executable trigger contract
  - applicability checks
    - `program_envelope.source_antipatterns`
    - `program_envelope.target_invariants`
    - `required_role_slots`
    - `negative_guards`
  - branch-level runtime selection for `pattern`
    - pattern root matching happens before branch selection
    - only replay-validated `program_envelope.runtime_branches` can be selected
    - required branch signals must match the current `RuntimeCaseView`
    - branch dry-run must bind every required bundle on current `S0`
    - zero matching branch returns no match
    - multiple matching branches return `branch_selection_ambiguous`
    - if bias recognition succeeds but no branch can bind, runtime records
      `pattern_recognized_branch_unbindable` and does not enter rewrite
  - compiler dry-run
    - non-empty candidate enumeration
    - required bundle coverage
    - bundle budget check
    - branch-selection ambiguity on bundle candidates
- Canonically merge compatible matched memories before treating them as
  conflicting.
  Runtime first compares root-bias contracts. Multiple candidates under the same
  root bias are not allowed to block each other only because branch/action
  contracts differ; runtime keeps the top root-compatible candidates within the
  normal selection budget and lets branch/compiler decide the executable path.
- Return matched groups, compiler output, rewrite hint, and a runtime guard
  consumed by DeepEye.
- Runtime branch selection scopes the matched pattern before compiler:
  sibling branch bundles, branch-specific source antipatterns, target effects,
  target invariants, role slots, negative guards, and program ops are removed,
  so `ActionCompiler` can only select candidates from the selected branch.
- Scope/preserve guard fields are currently audit-only:
  EEA still emits `audited_allowed_edit_scope`,
  `audited_must_preserve_tables`, and `audited_must_preserve_predicates`, but
  exposes broad `allowed_edit_scope` and empty preserve lists to avoid hard
  blocking valid dependency rewrites. DeepEye still keeps parse/schema/execution
  safety checks.

Important outputs:

- `matched_group_ids`
- `selected_branch_ids`
- `compiler_output.actions`
- `hint`
- `guard`
- `reason = no_match / no_action / blocked / ready`

### `common/runtime/trigger_contract.py`

Runtime contract materialization and validation.

Responsibilities:

- Sanitize runtime trigger contracts.
- Materialize contracts from legacy signatures when needed.
- Decide whether a memory object is runtime-executable.
- Remove broad/non-substantive trigger signals from hard runtime use.

### `common/analysis/signal_summary.py`

Builds formation-time summaries and runtime trigger contracts.

Responsibilities:

- Turn question/pred/schema/repair evidence into abstract signals.
- Build `variant_required_signal_sets` and `canonical_discriminants`.
- Attach runtime-facing action contract fields:
  - `required_role_slots`
  - `required_target_invariants`
  - `program_envelope`
  - `selection_policy`
  - `compiler_deterministic`
    - runtime-deterministic now requires `runtime_binding_coverage >= 1.0`
    - static compile coverage no longer upgrades runtime selection policy by itself

### `common/analysis/role_graph_normalizer.py`

Schema-agnostic SQL role graph normalization.

Responsibilities:

- Identify output refs, predicate refs, join refs, path roles, and relation roles.
- Normalize role-side structure so direct and joined variants can meet at the
  same runtime contract:
  - `direct_role_path`
  - `derived_role_path`
  - `role_side_group`
  - `side_key`
- Provide canonical references used by trigger contracts and canonical repair
  programs.
- Stay generic: no database-specific table-name or case-id rules.

## 3. Action Compiler And Rewrite Path

### `common/runtime/action_compiler.py`

Code-side candidate enumeration for the current SQL.

Responsibilities:

- Enumerate schema-legal action candidates from current `S0`, local schema, and
  matched memory.
- Bind only from current-case/schema/canonical refs. No free-form LLM argument
  invention.
- Expose candidate provenance and required edit scopes.
- Attach bundle-aware execution metadata to every candidate:
  - `bundle_id`
  - `effect_kind`
  - `bound_branch_id`
  - `cleanup_edits`
  - `counts_as_action`
  - `bundle_selection_key`

Current implemented primitives:

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
  - LLM is a selector over code-enumerated candidates
  - deterministic unique preselection can bypass the LLM
  - deterministic fallback now reasons over bundle ids, not only raw
    `canonical_op_id`
  - no post-selection dependency action is appended after bundle selection;
    cleanup stays inside the selected bundle contract
  - prompt schema context is candidate-linked:
    it keeps the current SQL role refs, candidate argument refs, related
    tables, related columns, related FK/PK edges, and matching semantic hints;
    full schema is used only as a fallback when no candidate/schema refs are
    available
- `run_memory_rewrite`
  - actions are authoritative
  - `natural_language_hint` is explanatory only and cannot fill missing args
  - code side fail-closes when any required action is missing a trace,
    lacks required bindings, or is returned as unrealized
- `run_hint_instantiation`
  - readability helper only
  - does not decide applicability

### `common/llm/prompts/`

Prompt builders for current nodes.

Files:

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
- `compatibility_judge.py`
  - advisory-only explainer
  - not a hard gate for runtime or promotion

## 4. Update / Audit / Memory Construction

### `common/learning/accumulate.py`

Wrong-case update entry.

Responsibilities:

- Accept wrong `S0` update requests.
- Run audit + extraction + normalization.
- Build singleton memory and merge it into `LibraryStateV2`.

### `common/learning/case_pipeline.py`

One-case offline/update pipeline.

Responsibilities:

- Deterministic preprocessing
- wrong-case audit
- error instance extraction
- canonical repair normalization

Important behavior:

- If the auditor emits `candidate_fix_sql`, code can validate it via execution
  comparison before treating it as `validated_sql`.

### `common/analysis/code_preprocess.py`

Deterministic preprocessing before LLM audit/extraction.

Responsibilities:

- Parse pred/gold SQL structure
- Build local schema views
- Build answer-blind case signal views
- Build offline-only delta signatures

### `common/analysis/structure_delta.py`

Deterministic pred-vs-gold structural delta.

Responsibilities:

- Compute generic SQL structural differences without case rules.
- Feed downstream audit/extraction/normalization.

### `common/common/analysis/repair_program_normalizer.py`

Canonical repair IR extraction.

Responsibilities:

- Convert extracted repair hypotheses into canonical repair ops.
- Call `common/analysis/contrastive_repair_effect.py` to derive Phase-1
  `ContrastiveRepairEffect` candidates from the audited pred-vs-target trace.
- Populate:
  - `operation_signature`
  - `repair_effect_signature`
    - `effect_candidates`
  - canonical refs
  - target invariants
  - accessory policies
  - unresolved variation axes
  - `output_path_delta`
  - `relation_delta`
  - `predicate_scope_delta`
  - `grain_delta`

### `common/common/analysis/contrastive_repair_effect.py`

Phase-1 offline effect discovery.

Responsibilities:

- Convert one audited repair trace into `ContrastiveRepairEffect` candidates.
- Use only case-local SQL/role-graph deltas, execution/audit hypotheses, and
  schema-derived roles.
- Emit the real effect axes from `expert_report_3.md`, including output shape,
  route, predicate scope, grain, aggregation unit, proxy/storage slot, ranking,
  formula, temporal, role-anchor, and multi-output contract deltas.
- Keep concrete table/column/path details as evidence, not as compatibility
  rules.
- Never use case ids, database names, manual labels, fixed column-pair rules, or
  weighted similarity.

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
  sees compact case cards plus pair relation counts and representative pairs,
  not all O(n^2) pair decisions.
- Build patterns root-first:
  - component retrieval uses case-derived effect/source/target signals
  - root admission decides shared source misconception / target preference
  - admitted patterns now also carry a `bias_recognition_contract`: a compact
    root-bias recognition contract with 3-6 closed-vocabulary phenomenon
    signals. It is stored on `InstantiationProgram`, not used as an executable
    rewrite program.
  - `core_program_signature`, DISTINCT, join cleanup, route/grain/action
    differences are branch/accessory evidence after root admission, not
    pre-admission split keys
  - every recalled component member must be reported as accepted, rejected,
    retrieved-but-not-admitted, or mechanically root-closed into a branch slot
  - mechanical branch closure is generic: it uses branchable pair evidence from
    the case's own repair trace and never DB/table/qid-specific rules
- Pair score computation is cached by singleton signal/contract hash. The
  insight slicer receives at most representative pair decisions by relation and
  case coverage, not a full component pair matrix.
- Root closure is intentionally conservative: `compatible` pairs can close a
  root; `partial` pairs need explicit strong root evidence; veto/conflict
  relations cannot mechanically absorb members.
- Keep `experience_families` empty in the evolved library. The schema field may
  exist for compatibility, but it is not a runtime or promotion source.

### `common/learning/shared_program_synthesizer.py`

Synthesizes shared canonical repair programs.

Responsibilities:

- Build `CanonicalRepairProgram` from compatible ops.
- Effect-first synthesis is now the primary shared-program path.
  - shared ops are matched by `RepairEffectSignature.effect_candidates`
  - compatibility compares abstract effect axis/source/target/delta/actionability, not exact SQL patch or concrete column path
  - legacy exact/generalized/invariant buckets are not allowed to create a shared program when required effect/insight signals are missing
- Preserve and merge effect-level structure:
  - `ContrastiveRepairEffect`
  - `RepairEffectSignature.effect_candidates`
  - bundle-aware `action_envelope`
- Preserve delta-carrying signature sections through shared synthesis:
  - `output_path_delta`
  - `relation_delta`
  - `predicate_scope_delta`
  - `grain_delta`
- Attach `program_envelope`:
  - `source_antipatterns`
  - `target_effects`
  - `target_invariants`
  - `allowed_action_primitives`
  - `action_envelope`
    - `bundles`
    - `max_actions_hint`
  - `lowering_branches`
  - `runtime_branches`
    - branch id
    - branch bundle ids
    - replay/runtime usable state
    - answer-blind required and negative signals
    - allowed primitives and edit scopes
    - replay metrics and runtime blockers
  - `branch_selection_contract`
  - `required_role_slots`
  - `negative_guards`
  - `unresolved_variation_axes`
- Emit generic lowering families and allowed primitive sets.

### `common/learning/program_coverage.py`

Coverage validator for shared programs.

Responsibilities:

- Separate:
  - `static_program_coverage`
  - `runtime_binding_coverage`
  - `member_candidate_coverage`
- Preserve backward-compatible `compile_coverage`
  - if runtime binding exists, it reflects `runtime_binding_coverage`
  - otherwise it falls back to static coverage

### `common/learning/evolution.py`

Online and final memory evolution.

Responsibilities:

- Local post-update evolve
- final evolve and freeze
- replay-gated runtime family / formal pattern decisions
- online evolve keeps singleton and pattern memories only; family is disabled
- full-pattern replay is diagnostic, while runtime exposure is controlled by
  branch-level replay/binding status. When a replay case loader is available,
  each evolved prefix runs branch-scoped member replay before a branch can become
  runtime visible.

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
  replay-usable.
- same-root runtime singleton/pattern conflict resolution:
  if multiple memories pass source trigger but have different learned action
  contracts, runtime first compares root-bias shape and then asks the compiler
  enumerator for current-case transform keys. These keys use only primitive,
  current bound arguments, and dependency repair steps, not source case ids,
  bundle ids, canonical program ids, or support evidence. Memories that compile
  to a shared current transform can enter compiler together; if no shared
  transform exists across the passed memories, runtime blocks the case as
  `ambiguous_current_transform`. The transform key is built from the
  `ActionCandidateSet` primitive plus non-empty current executable arguments
  after dropping identity/provenance/audit fields; learned metadata such as
  bundle/effect labels does not participate. An empty or incomplete transform
  key is not allowed to fall back into compiler execution.
- runtime-usable branch support controls which source singletons are
  superseded. If a pattern has only some usable branches, unsupported branch
  source singletons remain active instead of being removed by the root pattern.
- keep replay-derived `ProgramCoverage` split:
  - `static_program_coverage` remains the synthesis/static view
  - `runtime_binding_coverage` reflects replay/runtime binding evidence
  - `compile_coverage` stays as the backward-compatible alias
- cache replay rows by memory hash + holdout case + replay mode to avoid
  repeatedly replaying unchanged prefix memories during online local evolve

## 6. Core Contracts And Shared Types

### `common/core/data_structures.py`

Primary Pydantic contracts.

Important structures:

- `CaseAudit`
  - includes offline `effect_axis_hint`
- `ErrorInstanceV2`
  - includes advisory `possible_effect_axes`
- `ContrastiveRepairEffect`
- `RepairEffectSignature`
  - includes Phase-1 `effect_candidates`
- `CanonicalRepairIR`
- `CanonicalRepairProgram`
- `ProgramEnvelope`
- `ProgramCoverage`
- `ActionCandidateSet`
- `Action`
- `RuntimeCaseView`
- `GroupSummary`
- `LibraryStateV2`

### `common/core/vocabulary.py`

Shared enums and constants.

Contains:

- runtime signal enums
- action primitives
- edit scopes
- group lifecycle/status enums
- risk/confidence labels

## 7. Schema / Execution / Utilities

### `common/io/local_schema.py`

Builds local schema views for each case.

### `common/io/db_schema_access.py`

SQLite schema access helpers.

### `common/io/execution_compare.py`

Execution comparison utilities.

### `common/llm/utils.py`

Shared OpenAI-compatible JSON LLM caller.

### `common/structure_family.py`

Shared SQL AST/structure helper used by v2 and retained parser utilities.

### `common/schema.py` and `refine/`

Retained shared parser/legacy support. They are not the current EEA entry
point, but parts of the current code still reuse that parser stack.

## 8. CLI Entry Points

Current v2 CLIs:

- `cli/run_online_e2e_validation.py`
- `cli/run_multidb_validation.py`
- `cli/replay_runtime_trigger.py`
- `cli/replay_runtime_rewrite.py`
- `cli/replay_manual_pattern_compiler.py`
- `cli/offline_pattern_formation.py`
- `cli/build_library_from_work.py`
- `cli/convert_manual_patterns.py`
- `cli/audit_run.py`

Removed old v1 cluster pipeline:

- `online/`
- `distill/`
- old trace clustering and pattern distill CLIs

Retained intentionally:

- `presentation/`
- `refine/`

Reason:

The current EEA system is built around v2 update + runtime trigger/compiler +
replay-gated evolution, not the old v1 cluster pipeline.
