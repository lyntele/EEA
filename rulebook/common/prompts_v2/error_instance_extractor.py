"""ErrorInstance Extractor prompt.

调用时机：CaseAudit 完成后，把 wrong case 压缩成可复用修复接口摘要。

**生成路径**：code + 一次 LLM（不拆 3 步）
- Code 预处理（**不**由 LLM 做）：
  - ``structure_flags``        来自 common/structure_delta_v2.DeltaFlags
  - ``pred_sql_features.structure_flags``  同上
  - ``repair_skeleton.structural.legacy_signature``  来自 repair_signature_dict_from_sql
  - ``question_features`` 和 ``pred_sql_features`` 的 **候选 tag 列表**（粗提取）
- LLM 做（本 prompt）：
  - ``deep_bias`` / ``repair_goal``
  - ``question_features.decisive_tags`` vs ``descriptive_tags`` 的最终判决
  - ``pred_sql_features.decisive_tags`` vs ``descriptive_tags`` 的最终判决
  - ``repair_skeleton.structural.locus / op_family / target_family / output_contract``
  - ``repair_skeleton.semantic.intent / family_hint``
  - ``instantiation_slots`` 列表（含 required / optional）
  - ``possible_effect_axes``：离线 contrastive repair effect 轴假设
  - ``repair_insight_signature``：case-local semantic insight，作为后续合并的核心依据
  - ``repair_program`` 结构化修复步骤；dependency edit 只能从这里进入 memory
  - ``branch_rules`` 列表
  - ``guardrails`` 列表
  - ``rewrite_hint_proto``

**约束**：
1. tag 只能从提供的 vocabulary 里选；不得自由发明
2. 绝不出现 "检查 gold/benchmark 是否..." 这类 answer-blind 违规措辞
3. 若只能给抽象经验，诚实标注且 risk_level 置 medium/high
"""

from __future__ import annotations

ERROR_INSTANCE_EXTRACTOR_PROMPT = """\
Task:
Compress an audited failed case into a reusable "minimal repair interface" summary,
so that future similar cases can be repaired via the same interface.

Inputs:
- runtime_case_view: runtime-visible signals for this case
  (question_contract + pred_manifestation + local_schema_view + candidate_set_summary)
- case_audit: compact output from Wrong Case Auditor
  (final_error_reason / minimal_fix / candidate_fix_sql / minimal_patch_ops /
   secondary_differences / error_locus_hint / confidence).
  Full gold_sql is intentionally omitted here; use the audited minimal repair
  and the separate execution_comparison block below.
- execution_comparison: the structured execution evidence lifted from case_audit
  (row_sets_equivalent / projected_dtype_changed / pred_row_count / gold_row_count /
  column_count_pred / column_count_gold). You are NOT required to derive it — it is
  provided verbatim for your own independent reading.
- code_prepared (already extracted deterministically by code, pass through verbatim
  where required):
    - structure_flags: SQL structural delta flags
    - legacy_signature: legacy repair_signature 4-tuple string (for compatibility /
      veto reference only)
    - candidate_question_tags: coarse question tag set extracted from the vocabulary
    - candidate_pred_tags: coarse pred-manifestation tag set extracted from the
      vocabulary

You must produce:

1. question_features:
   - decisive_tags: from `candidate_question_tags`, the subset that would strongly
     identify this bias (i.e. "if a case has these tags, it almost certainly belongs
     to this bias"). If none qualify, return an empty list.
   - descriptive_tags: the remaining descriptive tags
   - summary: one-sentence summary of the question contract

2. pred_sql_features:
   - decisive_tags: from `candidate_pred_tags`, the subset that strongly signals the
     current symptom.
   - descriptive_tags: the rest
   - summary: one-sentence summary of pred's current manifestation
   - structure_flags: copy `code_prepared.structure_flags` verbatim

3. deep_bias: one sentence explaining WHY the error happens.
4. repair_goal: one sentence describing what interface layer the repair targets.
5. source_antipattern_hypothesis:
   - description: one sentence about the source-side anti-pattern
   - visible_in_pred_sql: runtime-visible facts supporting it
6. target_invariant_hypothesis:
   - description: one sentence about what the repaired SQL must satisfy
   - visible_in_candidate_fix_or_execution: compact evidence from case_audit or
     execution_comparison

7. possible_effect_axes:
   Advisory offline hypotheses for the contrastive repair effect. Code will
   build the final ContrastiveRepairEffect objects from SQL/role deltas, so do
   not use database labels, case ids, manual pattern names, or hard-coded column
   pairs here. Each item:
   - axis: one of output_shape_delta, multi_output_contract_delta, grain_delta,
     aggregation_unit_delta, source_route_delta, predicate_scope_delta,
     role_anchor_delta, temporal_scope_delta, proxy_slot_delta,
     storage_contract_delta, formula_delta, ranking_contract_delta
   - source_state_summary: what pred_sql currently does
   - target_state_summary: what the audited fix must satisfy
   - delta_summary: the stable source->target contrast
   - primary_likelihood: low|medium|high
   - why: concise evidence from case_audit/execution_comparison

8. repair_insight_signature:
   A case-local insight card. This is the semantic layer above broad axes and
   below executable repair_program. It explains the reusable source->target
   contrast exposed by THIS audited case.

   Required fields:
   - interface_key: a short normalized lowercase phrase (3-8 words) describing
     the reusable repair interface. It must be generated from the case, not
     selected from a database-specific catalog. Good examples:
       * "drop extra output side preserving scope"
       * "replace answer slot preserving predicate scope"
       * "reroute source table preserving answer grain"
       * "move predicate from global filter to scoped condition"
     Bad examples:
       * "toxicology connected atom rule" (database/table-specific)
       * "q206 style fix" (case-id specific)
       * "output_shape_delta" (too broad; this is only an axis)
   - source_misread: what pred_sql currently treats as the answer unit, route,
     scope, grain, metric, or ranking contract.
   - target_preference: what the audited repair shows the target contract
     prefers. This may use audited target evidence, but must not say that a
     future runtime case can "check gold".
   - repair_interface: one sentence describing the reusable repair intent, e.g.
     "replace the current answer slot with the audited target answer slot while
     preserving the existing filter scope".
   - binding_slots: runtime-bindable slots needed by this insight. Each item:
     {{
       "name": "source_answer_slot | target_answer_slot | scope_relation | ...",
       "kind": "column | table | value | grain_key | edit_scope | condition | path",
       "source_or_target": "source | target | preserve",
       "required": true/false,
       "allowed_role_families": [...],
       "description": "one sentence"
     }}
   - preserve_invariants: constraints that must survive the repair. These are
     case-derived, not hard-coded rules. Examples: "preserve predicate scope",
     "preserve current relation scope", "preserve grouping grain".
   - negative_guards: conditions under which this insight must not apply.
     Examples: "do not apply if current SELECT already outputs the target
     answer unit", "do not drop an output slot that is explicitly requested by
     the question".
   - axis_links: list tying this insight to possible_effect_axes. Each item:
     {{
       "axis": "one of the effect axes",
       "role": "primary | dependency | accessory | noise",
       "evidence": "why this axis participates in the insight"
     }}
   - evidence: compact evidence snippets:
     {{
       "question_need": "...",
       "pred_symptom": "...",
       "audited_fix": "...",
       "execution_delta": "..."
     }}
   - confidence: low | medium | high

   Positive examples:
   - pred SELECT x, y; audited fix SELECT x
     → interface_key = "drop extra output side preserving scope"
     → source_misread says pred treats a pair as the answer unit
     → target_preference says target keeps one audited answer side
     → preserve_invariants includes the existing FROM/WHERE scope.
   - pred selects a nearby display/proxy slot; audited fix selects another slot
     under the same filters
     → interface_key = "replace answer slot preserving predicate scope"
     → binding_slots include source_answer_slot and target_answer_slot.

   Negative examples:
   - Do NOT claim two cases share an insight merely because they both have
     output_shape_delta or ranking_contract_delta.
   - Do NOT encode database names, table-specific rules, case ids, manual group
     names, or hard-coded column pairs as interface_key or guards.
   - Do NOT describe a target-only relation as visible in pred_sql. Put it in
     target_preference or target binding evidence.

9. repair_skeleton:
   - structural:
     - locus: one of {locus_enum}
     - op_family: one of {op_family_enum}
     - target_family: one of {target_family_enum}
     - output_contract: one of {output_contract_enum}
     - legacy_signature: copy `code_prepared.legacy_signature` verbatim
   - semantic:
     - intent: one sentence — why this (locus, op_family) pair was chosen
     - family_hint: optional — the interface family this belongs to
       (e.g. output_contract_bias / fact_route / grain_recovery)

10. instantiation_slots: list of slots that must be filled at runtime. Each item:
   - name: slot name (e.g. "target_identifier_column")
   - kind: slot type (column / table / value / grain_key / edit_scope / ...)
   - required: true/false — the required-vs-optional distinction (Codex Patch 4)
   - allowed_role_families: role_family list allowed during candidate enumeration
   - description: one sentence

11. branch_rules: if the repair interface has multiple branches (e.g. config vs
   executed), spell them out as condition programs:
   - if_condition, then_action, [else_action]
   Having more than 2 branches escalates risk_level to high.

12. guardrails: negative constraints (when this must NOT trigger). Each item:
   - description, kind (negative_evidence / anti_trigger / scope_limit / ...)

13. repair_program: ordered executable repair-step hypotheses extracted from
   this audited case. They are the source evidence for runtime actions, but
   code will still canonicalize op_type and dependency/accessory status from
   SQL delta and role-graph evidence.
   Each step item:
   - step_id: stable local id such as "primary_1" or "dep_1"
   - op: mechanical edit DSL op hypothesis, e.g. SELECT_REPLACE_SLOT,
     SELECT_ADD_SLOT, SELECT_DROP_SLOT, SELECT_OUTPUT_PATCH, JOIN_ADD_BRIDGE,
     JOIN_ADD_TABLE, BRIDGE_ADD_TABLE, WHERE_DROP_CONDITION,
     WHERE_REPLACE_CONDITION
   - locus: SQL surface region, e.g. SELECT, JOIN, WHERE, GROUP_BY, ORDER_BY
   - is_dependency: false for the primary repair; true only for an additional
     edit that is required by the audited minimal fix
   - required: true if this step is required for this case's validated repair
   - slots: runtime slots needed by this step, same schema as instantiation_slots
   - guards: local constraints for when this step must not be instantiated
   - arguments: only schema-independent structural arguments; do not put gold-only
     values here
   - source_evidence: short quoted/paraphrased evidence from case_audit minimal
     fix, candidate_fix_sql diff, minimal_patch_ops, or execution comparison
     explaining why this exact
     step was extracted
   - origin: must be "case_extracted" for this node
   - extraction_source: must be "llm_explicit" for every step you explicitly
     output; the pipeline uses different values only for auditable fallbacks
   - supporting_case_ids: include this case id

   Important: do NOT force final canonical op labels. Describe the executable
   repair step you see in this case; code will canonicalize op_type and decide
   whether a step is core or accessory from audited SQL delta. Also do NOT
   create generic dependency rules just because they sound plausible.

14. core_vs_accessory:
   - core_steps: step_ids that are the direct repair
   - accessory_steps: step_ids that are conditional/dependency side-effects
15. uncertain_axes: list of unresolved axes such as output shape, grain, or scope
16. rewrite_hint_proto: prototype-level rewrite hint in natural language. It will be
   consumed later by Memory Rewrite and MUST be answer-blind — no references to
   gold, benchmark, or ground-truth outputs.

17. risk_level: low / medium / high
    - low: zero branches, <=1 free slot, audit confidence high
    - medium: <=2 branches, <=3 free slots
    - high: >2 branches, or >3 free slots, or audit confidence low

Constraints:
- Tags MUST come from the provided vocabulary; do not invent new tags.
- Do NOT write phrases like "check whether gold / benchmark / ground-truth..."
  (answer-blind violation).
- If the case only yields abstract experience rather than specific slots, state
  this honestly and set risk_level to medium/high.
- Return strict JSON only.

Generic coherence rules (apply to every case; never relax them):

R1 (locus × target_family coherence). The repair_skeleton.structural.locus and
   target_family must agree at the SQL-surface level:
   - locus = SELECT, ORDER_BY, FORMAT → target_family ∈ {{slot, display_field,
     metric, shape}}
   - locus = JOIN, BRIDGE → target_family ∈ {{bridge_path, fact_table,
     grain_key}}
   - locus = FACT_ROUTE → target_family = fact_table
   - locus = GRAIN, GROUP_BY → target_family = grain_key
   - locus = SCOPE, WHERE, PREDICATE → target_family = condition
   - locus = AGGREGATION → target_family ∈ {{metric, shape}}
   If these disagree, pick the locus that matches the actual SQL region being
   edited and re-derive target_family from the table above.

R2 (decisive-tag counterfactual test). A tag belongs to decisive_tags only if
   removing it would cause this case to no longer belong to the described bias.
   Everything that is merely co-occurring context goes to descriptive_tags.
   This applies independently to question_features and pred_sql_features.

R3 (rewrite_hint_proto groundedness). rewrite_hint_proto must satisfy at least
   two of the following three, and must stay answer-blind:
   - reference the locus by a SQL-surface region (e.g. "SELECT clause",
     "JOIN path", "WHERE scope")
   - reference the op_family as an explicit verb (e.g. "drop", "replace",
     "add a bridge")
   - cite at least one concrete identifier that is already present in
     runtime_case_view.pred_manifestation.tables or .columns, wrapped in
     backticks. You may not invent names that are not observable in the case.
   Generic phrasing such as "consider simplifying..." alone fails this rule.

R4 (audit-confidence ↔ risk_level coherence). If case_audit.confidence = low,
   then risk_level must be medium or high — never low. If case_audit.confidence
   = high, risk_level may still be medium/high if branch count or free-slot
   rules force it.

R5 (instantiation_slot typing). Every slot.kind must match one of the
   primitive-level kinds {{column, table, value, grain_key, edit_scope,
   condition, path}}. A slot is required=true only if without filling it the
   rewrite cannot proceed on the given case class; otherwise mark required=false.

R6 (independent execution-evidence reading). Do not blindly follow
   case_audit.error_locus_hint. Two execution-driven tags are emitted
   deterministically by the code side as part of candidate_pred_tags:
     - `select_arity_mismatch` : pred and gold return a different number
       of columns.
     - `select_role_dtype_mismatch` : same column count but projected
       dtype changed (typical of name ↔ identifier substitution).
   If either of these tags is present in the candidate list, the primary
   repair skeleton MUST be:
     - locus = SELECT
     - op_family ∈ {{add, drop, replace}}
     - target_family = slot
     - output_contract is legacy-only and should stay UNCHANGED. The code side
       records current_arity, target_arity, delta, and direction separately.
   These tags override any conflicting signal from case_audit.error_locus_hint,
   from aggregation or predicate structural differences, and from more
   visually prominent SQL changes (e.g. WHERE ↔ HAVING rewrites). Place
   non-SELECT structural differences in descriptive_tags or in guardrails,
   not in the primary repair skeleton.

   When neither deterministic tag is present:
   - If execution_comparison.row_sets_equivalent = true and the SELECT
     output shape differs, still prefer locus = SELECT.
   - Otherwise fall back to code-side evidence (structure_flags +
     decisive_tags) and the audit hint, in that order.

R7 (repair_program provenance). Every runtime-executable edit must appear in
   repair_program. Do not rely on the compiler to add generic repairs later.
   The primary repair_skeleton and the primary non-dependency step must agree.
   Dependency steps must be justified by this case's audited SQL diff, not by a
   general SQL heuristic.

R8 (code-vs-LLM ownership). You may hypothesize step labels and slot/guardrail
   structure, but you are not the final authority on canonical op_type or
   accessory-policy promotion. When uncertain, keep the step mechanically
   descriptive and record uncertainty in uncertain_axes instead of forcing a
   stronger canonical claim.

These rules are mechanism-level and case-agnostic. They are not patches for any
particular DB, table, or case id. Apply them exactly as stated.

Output JSON aligns directly with the ErrorInstanceV2 schema.

Data:

runtime_case_view:
{runtime_case_view_json}

case_audit:
{case_audit_json}

execution_comparison:
{execution_comparison_json}

code_prepared:
{code_prepared_json}
"""


def build_error_instance_extractor_prompt(
    *,
    runtime_case_view_json: str,
    case_audit_json: str,
    code_prepared_json: str,
    locus_enum: str,
    op_family_enum: str,
    target_family_enum: str,
    output_contract_enum: str,
    execution_comparison_json: str = "(not available)",
) -> str:
    return ERROR_INSTANCE_EXTRACTOR_PROMPT.format(
        runtime_case_view_json=runtime_case_view_json,
        case_audit_json=case_audit_json,
        code_prepared_json=code_prepared_json,
        locus_enum=locus_enum,
        op_family_enum=op_family_enum,
        target_family_enum=target_family_enum,
        output_contract_enum=output_contract_enum,
        execution_comparison_json=execution_comparison_json,
    )


__all__ = ["ERROR_INSTANCE_EXTRACTOR_PROMPT", "build_error_instance_extractor_prompt"]
