# CLAUDE.md

Guidance for agents working in this repository.

## Current System

This repository contains the v2/post-selection EEA Rulebook implementation.
The old v1 trace clustering and distilled-pattern pipeline has been removed
from the active code path.

Current flow:

```text
S0 selected by DeepEye
  -> runtime trigger over LibraryStateV2
  -> matched memory object
  -> ActionCompiler candidate enumeration and selection
  -> natural-language hint instantiation
  -> DeepEye rewrites S0 and selects S0/S1
  -> wrong S0 enters EEA update
  -> singleton/family evolution
  -> replay-gated final freeze
```

## Active Directories

```text
common/     v2 implementation contracts and runtime/update/evolution modules
cli/        v2 validation, replay, conversion, and audit entry points
refine/     legacy SQL parser/refiner support; not the current EEA entry point
doc/        current manifests and implementation notes
tests/      canonical program and runtime regression tests
```

## Files To Prefer

- Runtime trigger and hint response: `common/runtime_v2.py`
- Wrong-case accumulation: `common/accumulate_v2.py`
- Online/final evolution: `common/evolution_v2.py`
- Action enumeration/selection: `common/action_compiler_v2.py`
- Canonical repair program normalization: `common/repair_program_normalizer_v2.py`
- Trigger contracts: `common/trigger_contract_v2.py`
- Signal construction: `common/signal_summary_v2.py`
- Shared program synthesis and promotion: `common/shared_program_synthesizer_v2.py`, `common/promotion_v2.py`
- Deterministic structure deltas: `common/structure_delta_v2.py`
- LLM JSON calls: `common/llm_utils_v2.py`

## Repository Hygiene

- Do not reintroduce `distill/cluster` or the old `extract_patterns` pipeline.
- Do not commit generated outputs, `.state`, `workspace`, logs, pycache, or API keys.
- Keep `presentation/` and `refine/` unless the user explicitly asks to remove them.
- `common/config.toml` must use an empty or placeholder `api_key` in published commits.
