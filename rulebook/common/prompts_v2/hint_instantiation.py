"""Hint Instantiation prompt —— 把 code-rendered action brief 改写成可读 hint。

Phase 1.5e 中，本节点不再处理 source-case template。它接收的 `raw_hint`
已经是 code side 根据当前 structured actions 渲染出的 action brief。

职责：
- 只做 readability rewrite
- 不改变修复方向
- 不补结构参数
- 若与 actions 冲突，返回空 hint
"""

from __future__ import annotations


HINT_INSTANTIATION_PROMPT = """\
Task:
You are rewriting a code-rendered action brief into a concise, readable hint
for the current case. The raw_hint is already grounded in the current case's
structured actions. Your job is readability improvement only, not semantic
expansion.

Your job:

1. Read the raw_hint, the current case (question, evidence, pred_top1_sql,
   local_schema_view), and the compiler-produced actions (already
   instantiated to the current SQL).
2. Rewrite the raw hint into a concise, current-case-readable hint only when it
   is directionally consistent with the actions. The rewritten hint must:
   - Preserve the original repair direction (do NOT change drop -> add etc.)
   - Reference the actual column / table / alias names from the current
     pred_top1_sql or local_schema_view
   - Stay concise (1-3 sentences) and imperative
3. If the raw hint conflicts with the already selected actions, or would
   require inventing arguments not present in the actions, set
   ``applicable=false``, return an empty hint, and explain why.

Strict rules:

A. Do not invent new repair directions not in the raw_hint.
B. Do not reference gold / execution results / ground-truth.
C. The actions list is authoritative. Your hint only helps explain those
   already-instantiated actions.
D. Do not decide whether rewrite should proceed. Applicability is already
   decided by the structured actions and runtime contract.
E. If the raw_hint's direction conflicts with the actions, set
   ``applicable=false``. Do not silently rewrite the hint into a different
   repair direction.

Output JSON schema:
{{
  "instantiated_hint": "the rewritten, readable hint",
  "applicable": true/false,
  "instantiation_notes": "what changed from raw to instantiated, or why not applicable"
}}

Data:

raw_hint:
{raw_hint}

question:
{question}

evidence:
{evidence}

pred_top1_sql:
{pred_top1_sql}

local_schema_view:
{local_schema_view_json}

actions (already instantiated):
{actions_json}
"""


def build_hint_instantiation_prompt(
    *,
    raw_hint: str,
    question: str,
    evidence: str,
    pred_top1_sql: str,
    local_schema_view_json: str,
    actions_json: str,
) -> str:
    return HINT_INSTANTIATION_PROMPT.format(
        raw_hint=raw_hint or "(empty)",
        question=question,
        evidence=evidence or "(empty)",
        pred_top1_sql=pred_top1_sql,
        local_schema_view_json=local_schema_view_json,
        actions_json=actions_json,
    )


__all__ = ["HINT_INSTANTIATION_PROMPT", "build_hint_instantiation_prompt"]
