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
api.py                             Public EEA API consumed by DeepEye.

common/
  core/                            Data structures, enums, config, base schemas.
  io/                              DB schema access, local schema, execution compare.
  analysis/                        SQL structure, signals, role graph, repair IR.
  learning/                        Accumulate, pattern formation, promotion, evolution.
  runtime/                         Runtime trigger, branch match, compiler, hints.
  llm/                             LLM client, JSON helpers, prompt builders.
  reporting/                       Coverage, trigger observability, version metadata.

cli/
  run_online_e2e_validation.py         Online replay/update validation harness.
  run_multidb_validation.py            Multi-database validation orchestrator.
  replay_runtime_trigger.py            Trigger-only replay.
  replay_runtime_rewrite.py            Runtime rewrite replay.
  replay_manual_pattern_compiler.py    Compiler coverage replay for manual groups.
  offline_pattern_formation.py         Offline pattern formation check.
  build_library_from_work.py           Build library from DeepEye work dirs.
  convert_manual_patterns.py           Convert manual pattern groups into library.
  audit_run.py                         Inspect a run directory.

refine/
  Legacy SQL parsing/refiner support kept because current utilities still
  reuse the SQL structure parser. It is not the current EEA entry point.

tests/
  test_canonical_program.py            Regression coverage for canonical program,
                                      compiler, trigger, and rewrite behavior.
```

## Configuration

EEA resolves LLM config in this order:

1. `RULEBOOK_CONFIG_PATH`, `RULEBOOK_API_PROFILE_PATH`, `RULEBOOK_API_PROFILE`,
   or `API_PROFILE` if set.
2. Local legacy `common/config.toml` if present.
3. The current DeepEye integration profile at
   `../deepeye/DeepEye-SQL/rulebook_experiments/configs/api_profile_openrouter.toml`.

Both the legacy `[llm]` shape and DeepEye's `[rulebook.openrouter]` /
`[rulebook.model_map]` profile shape are supported. Do not commit real API keys.

Environment overrides:

```text
RULEBOOK_CONFIG_PATH
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
