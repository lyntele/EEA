"""Action Compiler prompt —— 约束生成契约.

Compiler 的核心设计：
- **代码**从 LocalSchemaView + memory objects 预枚举每个 primitive 的 candidate arguments
- LLM 只做 **selection**：从每个 primitive 的 candidates 里选 0 或 1 个，给 rationale
- **不允许**自由发明 target_column / target_table / expr —— 防止 hallucination
- No out-of-set actions: ActionCompiler is a strict selector. If no listed
  candidate fits, it emits no action and records the issue in diagnostics.

**Schema recall miss** 不是 memory 问题：
- 若某 primitive 的 candidates 为空，对应 ActionCandidateSet.empty_reason 会说明
  （missing_bridge_path / missing_column_candidate 等）
- 这些 reason 由 compiler **照抄**到输出 `schema_diagnostics`，不计入 memory demotion
"""

from __future__ import annotations

ACTION_COMPILER_PROMPT = """\
Task:
Generate concrete action candidates for an NL2SQL memory rewrite. Your job is
**selection** — pick from the candidates listed below. You do NOT invent arguments.

Inputs:
1. runtime_case_view: runtime-visible signals for the case, including a compact
   current-SQL role graph derived only from C0 top-1 and schema
2. memory_objects: compact matched singleton/family/pattern contracts, each with
   only the runtime-needed core interface, canonical instantiation program,
   trigger signature/contract, trigger match summary, and guardrails. Legacy
   source-case natural-language templates are intentionally omitted.
3. candidate_sets: **pre-enumerated** candidate summaries grouped by primitive.
   `selected_candidate_id` is authoritative; hidden full candidate arguments stay in code.
   Every candidate already carries code-side checks such as:
   - compatibility: exact | partial | conflict
   - binding_status: unique | ambiguous | unbound
   - direction_check: pass | fail
   - memory_alignment_status: pass | fail | unknown
   - candidate_contract_status: executable | blocked
   - reject_reasons: [] when the candidate is safe to choose
   - effect_contract: compact case-local insight that the candidate must realize
4. schema_diagnostics_pre: schema recall misses found during pre-enumeration

Rules:

A. Selection-first
   - Only candidates with compatibility=exact, candidate_contract_status=executable,
     direction_check=pass, and empty reject_reasons may be selected.
   - If multiple such candidates exist for one primitive, prefer the one with
     narrower `required_edit_scopes`, then stronger
     memory_alignment_status.
   - If no exact executable candidate exists, skip that primitive.
   - If a primitive has empty candidates, skip it and pass through schema_diagnostics
     as-is.
   - A candidate must realize the listed effect_contract. The contract is not a
     new source of schema identifiers; it only explains the already-enumerated
     candidate's repair intent.

B. No out-of-set actions
   - If no listed candidate fits, emit no action for that primitive.
   - Do not use escape hatch behavior. `used_escape_hatch` must always be false.
   - If the candidate set is insufficient, report it only in schema_diagnostics.notes.

C. Hard prohibitions
   - Do not fabricate target_column / target_table strings, even if they look
     plausible.
   - Do not reference gold / benchmark / ground-truth outputs (answer-blind rule).
   - Do not emit actions when the matching memory_object's guardrails are triggered.

D. Action count limits
   - Singleton hit → at most 1 action
   - Runtime family hit → at most 3 actions
   - Pattern hit → usually 1 action
   - Total actions per case ≤ 3
   - Candidate rows under the same primitive are alternatives. For example, if a
     2-column SELECT must become a 1-column SELECT, choose exactly one drop/collapse
     candidate; do not emit one action for each side.
   - Some SELECT candidates represent a multi-slot edit. If `arguments` contains
     `target_columns` and `target_slot_count`, select that whole candidate to add
     those slots together. If `arguments` contains `from_exprs` and `drop_count`,
     select that whole candidate to remove those slots together. If
     `REPLACE_SELECT_SLOT` contains `from_exprs`, `target_columns`, and
     `replace_count`, select that whole candidate to replace the projection
     together. Do not split a multi-slot candidate into several one-slot actions.
  - When a candidate includes `output_shape_delta`, respect its current arity,
    target arity, delta, and direction. These structured fields are the shape
    contract; legacy labels are not the general contract.
   - Direction matters. For DROP_SIDE / DROP_SELECT_SLOT candidates, the selected
     candidate's `from_expr` or `drop_condition` is the side that will be removed.
     `keep_conditions`, `to_predicate`, and `related_join_edges_to_keep` are the
     side that will remain. Match this direction to the memory object's
     instantiation_program; do not select a candidate merely because its
     `drop_condition` mentions a column from the memory text. If the memory says
     to keep a specific side, choose the candidate where that side is in
     `keep_conditions` / `to_predicate`, not in `drop_condition`.
   - Treat candidate-level checks as authoritative. If a candidate has
     `reject_reasons`, do not select it even if it appears semantically close.
   - The code-side candidate checks are already the executable contract. Do not
     reinterpret a candidate into a different semantic error type.
   - Do not select a candidate whose visible arguments contradict
     effect_contract.preserve_invariants or effect_contract.negative_guards.

E. Every action must include:
   - action_id (unique string)
   - source_group_id / source_group_type
   - primitive (ActionPrimitive enum)
   - arguments (copy the visible selected-candidate summary; do not invent any
     field omitted from the prompt. Runtime will restore the full code-side
     candidate arguments from selected_candidate_id.)
   - selected_candidate_id (the selected candidate_id)
   - rationale_short (one sentence)
   - priority (0.0-1.0, higher = higher priority)
   - risk (low / medium / high)
   - allowed_edit_scope: the SQL regions rewrite may touch (Memory Rewrite performs
     only the selected action and explicit repair_program steps within this scope)
   - used_escape_hatch (must be false)

Output JSON schema (aligns with ActionCompilerOutput):
{{
  "actions": [
    {{
      "action_id": "...",
      "source_group_id": "...",
      "source_group_type": "singleton | family | pattern",
      "primitive": "ActionPrimitive enum value",
      "arguments": {{...}},
      "selected_candidate_id": "candidate_id",
      "rationale_short": "...",
      "priority": 0.0,
      "risk": "low | medium | high",
      "allowed_edit_scope": ["SELECT", "JOIN", ...],
      "used_escape_hatch": false
    }}
  ],
  "schema_diagnostics": {{
    "missing_bridge_paths": [...],
    "missing_column_candidates": [...],
    "missing_role_family_matches": [...],
    "two_hop_extension_denied": [...]
  }}
}}

Data:

runtime_case_view:
{runtime_case_view_json}

memory_objects:
{memory_objects_json}

candidate_sets:
{candidate_sets_json}

schema_diagnostics_pre:
{schema_diagnostics_pre_json}
"""


def build_action_compiler_prompt(
    *,
    runtime_case_view_json: str,
    memory_objects_json: str,
    candidate_sets_json: str,
    schema_diagnostics_pre_json: str,
) -> str:
    return ACTION_COMPILER_PROMPT.format(
        runtime_case_view_json=runtime_case_view_json,
        memory_objects_json=memory_objects_json,
        candidate_sets_json=candidate_sets_json,
        schema_diagnostics_pre_json=schema_diagnostics_pre_json,
    )


__all__ = ["ACTION_COMPILER_PROMPT", "build_action_compiler_prompt"]
