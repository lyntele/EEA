"""Memory Rewrite prompt —— bounded autonomy 契约.

这是 EEA 对下游 DeepEye 的唯一接入点。Rewrite 接收 C0（rewrite 前候选集）+ actions，
产出 rewrite SQL。契约（Codex 建议）：

- **Actions are required edits**：每个 action 必须在最终 SQL 里落到某个具体 edit
- **Contract step only**：rewrite 只能执行 compiler action 中显式携带的
  repair_program steps；不得自行发明附带修复
- **Scope violation 记账**：若 rewrite 改动了 scope 外的部分，必须返回
  `scope_violation=true` 并解释；这类改动会被人工审查
- **Realization trace 必须填**：每个 action 哪个 SQL edit 来实现，
  对齐 data_structures_v2.ActionRealizationTrace

**answer-blind 约束**：
- rewrite 只能使用 question / evidence / pred_sql / actions / local schema
- 不得引用 gold / execution 结果 / 正确答案
"""

from __future__ import annotations

MEMORY_REWRITE_PROMPT = """\
Task:
Perform an NL2SQL memory rewrite. Based on the given actions, produce a **bounded
rewrite** of the top-1 SQL in C0 to yield a new SQL candidate.

Inputs:
- question: natural-language question
- evidence: evidence attached to the question
- c0_top1_sql: top-1 SQL before rewrite
- actions: list produced by Action Compiler. Each action carries:
  - primitive (ADD_SELECT_SLOT / REPLACE_SELECT_SLOT / ... / MATERIALIZE_RANKING_OUTPUT)
  - arguments: what exactly to change
  - allowed_edit_scope: list of SQL regions rewrite may touch
  - rationale_short, priority, risk
- local_schema_view: localized schema of the current db

Rules (bounded autonomy):

A. Every action is a **required edit** — it must be realized in the final SQL.
   - If an action cannot be realized on the current case, return realized=false
     with explanation.
   - You may NOT silently ignore any action; every action must appear in
     action_realization_traces.
   - If an ADD_SELECT_SLOT action carries `target_columns`, add all listed
     columns in one bounded SELECT edit. If a DROP_SELECT_SLOT action carries
     `from_exprs`, remove all listed expressions in one bounded SELECT edit.
     If a REPLACE_SELECT_SLOT action carries `from_exprs`, `target_columns`,
     and `replace_count`, replace the listed projection slots together in one
     bounded SELECT edit. `target_slot_count`, `drop_count`, `replace_count`,
     and `output_shape_delta` describe the expected shape change.
   - If an action carries `compiled_from_program_id` or `canonical_op_type`,
     it is an instantiation of a learned canonical repair program. Use the
     current action arguments as the concrete binding. Do not copy identifiers
     from `canonical_refs`; those refs are source-case evidence only.
   - For INSERT_BRIDGE, edit only FROM/JOIN unless SELECT is explicitly present
     in `allowed_edit_scope` or in a repair_program step. Target projection refs
     are evidence, not permission to rewrite SELECT.
   - For REROUTE_FACT, replace the current FROM/JOIN/SUBQUERY relation path with
     the listed `target_relation_edges`; if `target_output_refs` are present and
     SELECT is in `allowed_edit_scope`, bind the projection to those target refs.
     Preserve predicates and literals unless an explicit repair_program step says
     otherwise.

B. Contract repair-program steps are allowed **only within allowed_edit_scope**.
   - Additional edits are allowed only when they are explicitly listed in
     `action.arguments.repair_program` as contract-extracted steps.
   - Do not add WHERE predicates, DISTINCT, GROUP BY edits, literal rewrites,
     or any other repair merely because it looks generally useful.
   - DISTINCT is allowed only when an explicit repair_program dependency step
     such as `SELECT_ENFORCE_DISTINCT` is present. In that case, implement it
     as a bounded SELECT-scope edit, usually by adding SELECT DISTINCT to the
     rewritten projection.
   - WHERE predicates may be added only when an explicit dependency step such
     as `WHERE_ADD_CONDITION` is present. In that case, add only the listed
     `target_predicates` from the step's policy payload, translating aliases to
     the current SQL when needed.
   - ORDER BY and LIMIT may be edited only when explicit dependency steps such
     as `ORDER_BY_APPLY` and `LIMIT_APPLY` are present. Use only the listed
     `target_order_by` and `target_limit` payload values.
   - If a repair_program step cannot be bound to the current SQL, leave it
     unrealized and explain in the trace notes instead of inventing a fallback.
   - If touching regions outside the declared scope is unavoidable to produce a
     legal SQL, set scope_violation=true and explain.

C. No full rewrite.
   - Keep the overall structure of c0_top1_sql intact.
   - Only modify regions declared by the actions and their explicit
     repair_program steps.

D. Answer-blind.
   - Do NOT reference gold / execution results / ground-truth outputs.
   - Use only question / evidence / pred / actions / schema.

E. Natural-language repair hint.
   - If a `natural_language_hint` is provided below, treat it as explanatory
     text only. It may improve readability, but it must NOT supply missing
     arguments or broaden the declared edit scope.
   - If the structured action arguments are insufficient to realize an action,
     mark that action unrealized instead of guessing from the hint.
   - If the hint is empty, proceed with actions alone.

F. If a required action cannot be realized:
   - Leave the SQL unchanged for that action.
   - Set realized=false for that action.
   - Explain the missing binding or blocked scope in the trace notes.

G. Primitive realization templates:
   - DROP_SELECT_SLOT: remove only the listed SELECT expressions.
   - DROP_SIDE: remove only the listed projection side or predicate side.
   - INSERT_BRIDGE: add only the listed bridge table/join path.
   - MOVE_CONDITION: move only the listed predicate from the listed source
     scope to the listed target scope.
   - CHANGE_GRAIN: apply only the listed aggregate/grain rewrite.
   - SWITCH_CANONICAL_FIELD: replace only the listed current expression with
     the listed target expression while preserving the join path.
   - MATERIALIZE_RANKING_OUTPUT: add only the listed ranking/metric output and
     its explicit ORDER/LIMIT contract.

H. Output:
   - One rewrite_sql (the final SQL).
   - One realization_trace per action:
     {{
       "action_id": "...",
       "realized": true/false,
       "edits": [
         {{"edit_kind": "add|remove|replace|move", "location": "SELECT|JOIN|...",
           "before_snippet": "optional", "after_snippet": "optional"}}
       ],
       "scope_violation": true/false,
       "notes": "optional"
     }}

Output JSON schema:
{{
  "rewrite_sql": "the rewritten SQL string",
  "action_realization_traces": [
    {{ "action_id": "...", "realized": true, "edits": [...], "scope_violation": false, "notes": null }},
    ...
  ],
  "contract_steps_applied": ["optional: one sentence per explicit repair_program step applied"],
  "notes": "optional"
}}

Data:

question:
{question}

evidence:
{evidence}

c0_top1_sql:
{c0_top1_sql}

natural_language_hint:
{natural_language_hint}

actions:
{actions_json}

local_schema_view:
{local_schema_view_json}
"""


def build_memory_rewrite_prompt(
    *,
    question: str,
    evidence: str,
    c0_top1_sql: str,
    actions_json: str,
    local_schema_view_json: str,
    natural_language_hint: str = "",
) -> str:
    return MEMORY_REWRITE_PROMPT.format(
        question=question,
        evidence=evidence or "(empty)",
        c0_top1_sql=c0_top1_sql,
        natural_language_hint=natural_language_hint or "(none)",
        actions_json=actions_json,
        local_schema_view_json=local_schema_view_json,
    )


__all__ = ["MEMORY_REWRITE_PROMPT", "build_memory_rewrite_prompt"]
