# EEA Rulebook

EEA Rulebook is the current post-selection error-experience memory module used
by DeepEye integration experiments. It does not run the old
`run_refine -> extract_patterns -> cluster -> distill pattern` pipeline.

The current path is:

```text
DeepEye selected SQL S0
  -> EEA runtime trigger over the current memory library
  -> action compiler instantiates matched memory on S0
  -> hint instantiation returns a natural-language rewrite hint
  -> DeepEye rewrites S0 into S1 and selects between S0/S1
  -> if S0 was wrong, EEA accumulates the case as memory
  -> local evolution and final replay-gated freeze update the library
```

## Main Code Layout

```text
common/
  runtime_v2.py                    Runtime trigger, matching, guard assembly.
  accumulate_v2.py                 Wrong-case audit -> singleton memory update.
  evolution_v2.py                  Online/local memory evolution and final freeze.
  action_compiler_v2.py            Code-side candidate enumeration + action selection.
  repair_program_normalizer_v2.py  Pred/gold repair program -> canonical repair IR.
  shared_program_synthesizer_v2.py Shared canonical program synthesis.
  program_coverage_v2.py           Compiler coverage validation for canonical programs.
  promotion_v2.py                  Replay-gated family/pattern promotion.
  family_formation_v2.py           Singleton/family grouping and replay checks.
  trigger_contract_v2.py           Runtime trigger contracts and executable checks.
  signal_summary_v2.py             Formation and trigger signal construction.
  role_graph_normalizer_v2.py      Schema-agnostic role graph extraction.
  structure_delta_v2.py            Deterministic pred-vs-gold SQL structure delta.
  llm_nodes_v2.py                  LLM node wrappers for audit/compiler/rewrite/hint.
  llm_utils_v2.py                  Shared robust JSON LLM call helper.
  code_preprocess_v2.py            Deterministic preprocessing before LLM audit.
  pipeline_v2.py                   Offline wrong-case processing pipeline.
  data_structures_v2.py            Pydantic contracts for v2 runtime/update objects.
  vocabulary_v2.py                 Enums for signals, actions, scopes, statuses.
  local_schema_v2.py               Per-case local schema recall.
  db_schema_access_v2.py           SQLite/schema access helpers.
  execution_compare_v2.py          SQL execution comparison.
  prompts_v2/                      Prompt builders used by v2 LLM nodes.

cli/
  run_online_e2e_validation_v2.py      Online replay/update validation harness.
  run_multidb_validation_v2.py         Multi-database validation orchestrator.
  replay_runtime_trigger_v2.py         Trigger-only replay.
  replay_runtime_rewrite_v2.py         Runtime rewrite replay.
  replay_manual_pattern_compiler_v2.py Compiler coverage replay for manual groups.
  offline_family_formation_v2.py       Offline family formation check.
  build_v2_library_from_work.py        Build v2 library from DeepEye work dirs.
  convert_manual_patterns_to_v2.py     Convert manual pattern groups into v2 library.
  audit_v2_run.py                      Inspect a v2 run directory.

refine/
  Legacy SQL parsing/refiner support kept because current v2 utilities still
  reuse the SQL structure parser. It is not the current EEA entry point.

tests/
  test_canonical_program_v2.py         Regression coverage for canonical program,
                                      compiler, trigger, and rewrite behavior.
```

## Configuration

The default config is `common/config.toml`. Do not commit real API keys.

Environment overrides:

```text
RULEBOOK_LLM_MODEL
RULEBOOK_LLM_BASE_URL
RULEBOOK_LLM_API_KEY
RULEBOOK_LLM_MAX_TOKENS
RULEBOOK_LLM_TEMPERATURE
RULEBOOK_LLM_HARD_TIMEOUT_SECONDS
```

## Notes

- Generated artifacts, `.state/`, `outputs/`, `workspace/`, pycache, and run
  logs are intentionally excluded from the published repository.
- The current integration contract is documented in
  `doc/current_implementation_overview.md` and the top-level EEA docs.
