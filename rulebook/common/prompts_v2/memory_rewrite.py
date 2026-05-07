"""Memory Rewrite prompt —— bounded rewrite_contract executor.

Rewrite 接收单条 S0 和已绑定的 rewrite_contract，产出一条 rewrite SQL。
ActionCompiler / runtime 已经完成触发、记忆选择、候选动作选择和参数绑定。
本阶段只执行合同，不重新选择 pattern、branch 或 action。

- **Actions are required edits**：每个 action 必须在最终 SQL 里落到某个具体 edit
- **Contract step only**：rewrite 只能执行 compiler action 中显式携带的
  repair_program steps；不得自行发明附带修复
- **Scope violation 记账**：若 rewrite 改动了 scope 外的部分，必须返回
  `scope_violation=true` 并解释；这类改动会被人工审查
- **Realization trace 必须填**：每个 action 哪个 SQL edit 来实现，
  对齐 data_structures_v2.ActionRealizationTrace

**answer-blind 约束**：
- rewrite 只能使用 question / evidence / pred_sql / rewrite_contract / minimal schema_context
- 不得引用 gold / execution 结果 / 正确答案
"""

from __future__ import annotations

MEMORY_REWRITE_PROMPT = """\
Task:
Execute the provided rewrite_contract against c0_top1_sql and return one
bounded rewritten SQL candidate.

Role boundary:
- Triggering, memory matching, branch selection, candidate selection, and
  argument binding are already complete.
- The rewrite_contract is the source of truth. Do not reinterpret it as a
  suggestion and do not invent missing parameters.
- Natural-language hint is explanatory only; it cannot add edits or broaden
  scope.

Execution rules:
- Apply every required primary_edit.
- Apply every required dependency_edit whose binding_status is bound.
- Modify only scopes listed in rewrite_contract.allowed_scopes.
- Preserve predicates, literals, GROUP BY, ORDER BY, LIMIT, and joins unless the
  contract explicitly edits them.
- For a bound remove_join_blocks dependency, remove exactly the listed SQL block.
  A join alias appearing inside that block's own ON clause is not an external
  dependency; fail only if the contract reports an external reference or a
  preserve constraint would be broken.
- All required_absence_checks must pass in rewrite_sql. If a check lists text
  that should be removed, rewrite_sql must not still contain that text.
- Do not report a primary/dependency edit as realized unless rewrite_sql
  actually reflects that edit and the trace lists the concrete before/after
  snippet.
- If a required edit has binding_status=unbound, or if applying it would violate
  scope/preserve constraints, leave SQL unchanged for that action and return
  realized=false with the exact reason.
- Do not use gold, execution results, or benchmark answers.

Output:
- rewrite_sql: final SQL after applying the contract, or the unchanged SQL if a
  required edit cannot be safely realized.
- action_realization_traces: one trace per action_id, with concrete edits.
- contract_steps_applied: short audit notes for dependency edits.

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

rewrite_contract:
{rewrite_contract_json}

schema_context:
{schema_context_json}
"""


def build_memory_rewrite_prompt(
    *,
    question: str,
    evidence: str,
    c0_top1_sql: str,
    rewrite_contract_json: str,
    schema_context_json: str,
    natural_language_hint: str = "",
) -> str:
    return MEMORY_REWRITE_PROMPT.format(
        question=question,
        evidence=evidence or "(empty)",
        c0_top1_sql=c0_top1_sql,
        natural_language_hint=natural_language_hint or "(none)",
        rewrite_contract_json=rewrite_contract_json,
        schema_context_json=schema_context_json,
    )


__all__ = ["MEMORY_REWRITE_PROMPT", "build_memory_rewrite_prompt"]
