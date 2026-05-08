"""Wrong Case Auditor prompt.

调用时机：当 enhanced SQL 仍然与 gold 不等价时，对这一 case 做 **离线**
最小可验证修复审计。Auditor 是离线节点，允许看 gold 与执行结果。

输出 schema 对齐 `data_structures.CaseAudit`：
- final_error_reason (str)
- minimal_fix (str) —— 相对 pred 的最小 diff 描述
- candidate_fix_sql (str, 可选) —— 直接按 minimal_fix 改写出的候选 SQL；由 code side
  负责后续验证，LLM 不得宣称自己已执行验证
- minimal_patch_ops (list[dict]) —— 结构化最小 patch
- effect_axis_hint (str, 可选) —— 离线 contrastive repair effect 轴提示
- secondary_differences (list[str]) —— 次要差异，只做审计上下文
- error_locus_hint (Locus, 可选) —— 初步 locus 归类
- confidence (low/medium/high)

设计原则
- **不**给抽象标签（"输出错了"），必须给具体可验证修复
- 最小化：只改动必须改动的部分，不要整条重写
- candidate_fix_sql 若存在，必须是 minimal_fix 的直接应用结果
"""

from __future__ import annotations

WRONG_CASE_AUDITOR_PROMPT = """\
Task:
Perform post-mortem audit on a single failed NL2SQL case. The goal is not to assign
an abstract label but to identify the minimal verifiable repair.

Inputs:
- question: natural-language question
- evidence: hint attached to the question (may be empty)
- pred_sql: the system's current output (still incorrect)
- gold_sql: the reference SQL
- local_schema_view: localized schema visible for this case (tables, columns, PK/FK)
- execution_result: comparison of pred vs gold execution rows (if available)

Requirements:
1. Compare pred_sql against gold_sql and identify the primary divergence.
2. Propose only the minimal fix, not a full rewrite:
   - Column swap: name only the old and new columns.
   - Missing JOIN: name only the missing bridge.
   - Scope shift: name only the WHERE ↔ CASE move.
   - Grain change: name only the target grain level.
3. Set `error_locus_hint` to exactly one of:
   SELECT / JOIN / WHERE / GROUP_BY / ORDER_BY / FACT_ROUTE / GRAIN / SCOPE /
   AGGREGATION / FORMAT / PREDICATE / BRIDGE
4. If possible, emit `candidate_fix_sql` — the SQL obtained by directly applying
   minimal_fix on top of pred_sql. You cannot execute SQL yourself; code will
   validate it later. Do NOT claim that it is already verified.
5. Emit structured `minimal_patch_ops` describing the primary patch and any
   directly adjacent preserved fragments. Keep it clause-level, not full-rewrite.
6. Emit optional `effect_axis_hint` using one of:
   output_shape_delta, multi_output_contract_delta, grain_delta,
   aggregation_unit_delta, source_route_delta, predicate_scope_delta,
   role_anchor_delta, temporal_scope_delta, proxy_slot_delta,
   storage_contract_delta, formula_delta, ranking_contract_delta.
   This is only an offline hint. Do not use database-specific labels, case ids,
   manual pattern names, or hard-coded column-pair names.
   Interpret patch roles as:
   - primary: the core source->target contrast that makes pred wrong
   - dependency: a required cleanup caused by the primary patch
   - accessory: optional formatting/dedup/ranking cleanup
   - noise: unrelated SQL difference that should not define the repair effect
   Examples:
   - pred SELECT x, y; gold SELECT x
     → effect_axis_hint = output_shape_delta; the SELECT-slot drop is primary.
   - pred joins table T only because the dropped output y used it; gold no
     longer uses T
     → the join cleanup is dependency/accessory, not the primary effect.
   - pred and gold both rank by the same metric, but gold also outputs the
     metric column
     → output_shape_delta or multi_output_contract_delta is primary; ranking is
     contextual unless the ordering contract itself changed.
   Anti-example:
   - Do not mark a cleanup join drop as primary if the main audited repair is
     an output unit change.
7. Return strict JSON only. Do not add free-form explanation.

R-equivalence (execution-aware locus selection, applies whenever
execution_result is provided):
- When multiple SQL regions differ simultaneously and the execution_result
  shows that pred and gold return the same final entity set (or the row sets
  are equivalent modulo column representation), the primary divergence is the
  OUTPUT CONTRACT, not the aggregation or predicate difference. In that case:
    * error_locus_hint = SELECT
    * minimal_fix must describe the SELECT-slot swap (e.g. swap a name-like
      column for its identifier counterpart on the existing join graph)
    * aggregation / HAVING / predicate differences are secondary — do NOT
      name them as the primary fix even if they appear structurally larger
- Only when execution_result shows the result sets actually differ (or is
  unavailable) may the primary divergence be placed on other locus. A more
  structurally significant difference is NOT automatically the primary one
  when the rows are equivalent.
- If execution_result contains `projected_dtype_changed: true`, treat the
  column-type shift (e.g. text → integer) as strong evidence that the
  SELECT slot is the primary repair target, irrespective of other
  differences.

Output JSON schema:
{{
  "final_error_reason": "one sentence: why pred is wrong",
  "minimal_fix": "description of the minimal fix (not a full SQL)",
  "candidate_fix_sql": "SQL after directly applying minimal_fix; empty string if omitted",
  "minimal_patch_ops": [
    {{
      "clause": "SELECT|JOIN|WHERE|GROUP_BY|ORDER_BY|AGGREGATION|SCOPE|BRIDGE",
      "edit_kind": "add|drop|replace|move|reroute",
      "source_fragment": "optional",
      "target_fragment": "optional",
      "preserve_fragments": ["optional"],
      "primary": true
    }}
  ],
  "effect_axis_hint": "optional effect axis string",
  "secondary_differences": ["optional secondary differences"],
  "error_locus_hint": "one of the LOCUS enum values",
  "confidence": "low | medium | high"
}}

Data:

question:
{question}

evidence:
{evidence}

pred_sql:
{pred_sql}

gold_sql:
{gold_sql}

local_schema_view:
{local_schema_view_json}

execution_result:
{execution_result}
"""


def build_wrong_case_auditor_prompt(
    *,
    question: str,
    evidence: str,
    pred_sql: str,
    gold_sql: str,
    local_schema_view_json: str,
    execution_result: str,
) -> str:
    return WRONG_CASE_AUDITOR_PROMPT.format(
        question=question,
        evidence=evidence or "(empty)",
        pred_sql=pred_sql,
        gold_sql=gold_sql,
        local_schema_view_json=local_schema_view_json,
        execution_result=execution_result or "(not available)",
    )


__all__ = ["WRONG_CASE_AUDITOR_PROMPT", "build_wrong_case_auditor_prompt"]
