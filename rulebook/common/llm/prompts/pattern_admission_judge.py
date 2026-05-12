"""Pattern admission judge prompt.

This judge runs after conservative candidate construction and pair-level
semantic checks. It is intentionally offline-only: it may admit a pattern
candidate, but it must not invent executable actions or runtime trigger rules.
"""

from __future__ import annotations


PATTERN_ADMISSION_JUDGE_PROMPT = """\
Task:
Decide whether a retrieved group of audited failed cases exposes one stable
benchmark/model bias that should become an offline formal-pattern candidate.

This is conservative group-level admission, not pair merging.

Boundary:
- You are NOT writing SQL.
- You are NOT creating a rewrite hint.
- You are NOT inventing compiler arguments.
- You are NOT promoting the candidate to runtime.
- First decide the root pattern: whether the cases share the same source
  misconception / target preference / repair effect. Then assign admitted
  members to finite branches. Do not decide root membership from SQL edit path
  alone.
- You may admit a pattern only when the cases share a stable source->target
  root bias AND each admitted case can either be assigned to an answer-blind
  branch or explicitly marked as not runtime-usable/offline-only.
- The candidate may contain different extracted core repair packages. Treat
  those differences as branch/accessory evidence first. Reject only when the
  difference changes the source misconception, target preference, answer unit,
  metric/formula, or non-branchable scope contract.
- Pair context is auxiliary audit evidence. Use case_cards as the primary
  evidence for stable bias and repair interface. Do not admit a pattern only
  because representative pairs look compatible, and do not reject solely
  because one representative pair is partial when accepted_case_ids can isolate
  a clean subset.
- Branches are allowed only when each branch can be selected from the current
  question/pred/schema/memory signals, not from gold SQL or final answers.
- Finite dependency/action branches may be admitted only when they remain inside
  the same root bias. If a branch changes target preference, answer unit,
  ranking metric, formula, predicate metric, or non-branchable aggregation
  contract, reject or exclude it.
- If a retrieved component contains a clean stable subset plus unrelated cases,
  admit the clean subset and list the unrelated cases in excluded_case_ids.
  Do not reject the whole component when accepted_case_ids can isolate one
  reusable pattern of size at least two.
- If only a loose family resemblance exists, reject formal pattern admission.

Definitions:
- stable_bias_key: a short source->target contrast shared by the admitted cases,
  e.g. "drop extra paired output side while preserving filter scope".
- primary_repair_interface: the abstract action interface compiler must later
  instantiate, e.g. "select one canonical answer unit from a paired output".
- branch_axis: a finite unresolved variation such as "which output side is kept"
  or "whether a dependency such as DISTINCT is needed". Branches must be
  selected by local signals and compiler coverage, not by hand-coded case IDs.
- core_program_signature: the extracted SQL-edit package for one member. It is
  branch evidence, not root-pattern identity. A different core signature can
  remain in the same pattern only if it is a finite lowering branch under the
  same source misconception and target preference.
- dependency branch: a supporting edit inside the same answer-unit contract.
  Examples include duplicate removal, route cleanup, or preserving equivalent
  predicate scope while applying the same core output repair. A dependency is
  NOT branchable when it introduces a different target preference such as a
  different ranking metric, formula, aggregation grain, predicate metric, or
  answer unit.
- negative guard: a condition showing when this pattern must not absorb a case.
- accepted_case_ids: the subset, if any, that truly shares this pattern. Exclude
  members that only match a broad axis but require a different repair program.
- membership_by_case: every candidate case should be accounted for as
  accepted_root, rejected_root, branch_unassigned, or offline_only. Do not omit a
  case silently just because its card was sampled or its branch is not yet
  runtime-safe.
- When excluding a case from a slicer_hypothesis, explain the concrete conflict
  in source_misread, target_preference, answer unit, or scope contract. Do not
  exclude only because it needs an additional join, predicate move, DISTINCT, or
  route cleanup if that extra edit is a finite branch of the same stable bias.
- loose generic interfaces such as "add missing name slot", "fix selected
  column", or "adjust join" are not enough. The admitted cases must share the
  same source misread and target preference, not merely the same SQL surface
  edit family.

Positive examples:
- Several cases output both sides of a relationship while the target keeps one
  answer unit. Some cases need a dependency such as duplicate removal and some
  do not. Admit if the core bias is the same and the dependency is a finite
  branch extracted from member repair programs; require compiler/replay checks
  before runtime use if branch selection is not yet proven.
- Several cases use a proxy/display slot while the target consistently uses a
  canonical answer slot. Admit if the source slot role, target slot role, and
  preserve invariants are shared even when concrete table/column names differ.
- Several cases miss a materialized ranking output under the same ranking
  contract. Admit if metric binding and output role are stable.

Negative examples:
- Same broad output_shape_delta but one case adds a missing metric column while
  another drops an extra paired output side. Reject or exclude one side: they do
  not share one repair interface.
- Same database and relation-like SQL, but one case asks for local attributes
  and another asks for a bidirectional pair count. Reject: shared schema surface
  is not a shared repair program.
- One case adds an entity/name output required by a multi-part question while
  another changes a ranking metric or answer unit. Reject even if both touch
  SELECT: the source misread and target preference are different.
- One case only completes an output identity while another also changes the
  semantic predicate/ranking contract. Reject if that extra contract is not a
  cleanup branch of the same stable bias.
- Same join shape but one repair changes the answer unit, another changes route
  scope, and another changes aggregation grain. Reject formal pattern; this is
  at most a family.
- Broad source/target insight such as "proxy to canonical", "fix selected
  column", "row to entity", or "reroute to correct source" is not enough when
  concrete answer-unit or action interface differs.
- Do not admit because of case ids, manual labels, exact table names, exact
  column names, or database-specific vocabulary.

Runtime-use requirement:
- recognition.question_precondition and recognition.sql_precondition are later
  used by a runtime judge on a new answer-blind case. They must be neither
  narrower than the admitted members nor so broad that unrelated cases match.
- recognition.question_precondition describes only the question-side target
  intent observable from the natural-language question. Do not put SQL-side
  failure mechanisms such as wrong route, bridge misuse, duplicated joins, or
  proxy source choice into the question precondition unless the question itself
  states them.
- recognition.sql_precondition describes the current pred_sql source-side shape
  or antipattern. SQL-side misconception belongs here, not in the question
  precondition.
- recognition.grounded_anchors must contain at least two code-verifiable
  anchors observed in admitted members. Anchors are generic roles/path facts,
  not exact table names, exact columns, aliases, or case ids.
- applicability is audit-only during admission. Write only a concise
  intent_description explaining when the repair seems applicable.
- applicability.selected_source_facts is the only admission-time runtime gate
  field. Choose exact fact strings only from every admitted seed member's
  case_cards_sql_view.source_state_facts. Do not invent fact types, rewrite
  fact strings, copy SQL snippets, or include case-specific literals. If no
  shared source facts are safe, return an empty list and gate_status
  "disabled_empty_facts". Code will re-check and filter your selection, and
  will disable the gate for single-seed patterns.
- Do not write executable predicates, extra signals, fact kinds, or negative
  guards beyond selected_source_facts; regression guards are learned later only
  from real regression feedback.
- binding.source_slots and binding.target_slots must be literal-free. Use
  role_family/path_role/relation_role/answer_unit_role only. Never write table,
  column, expression, alias, SQL text, or seed case target names in binding.
- binding.allowed_operations must only use ActionPrimitive values from
  action_primitives below.
- Calibrate each signature against the admitted members before returning it.
  First draft the signature, then list admitted members it would match or miss,
  then rewrite it if estimated_recall is below 0.8.
- Phrase signatures around abstract roles and failure context. Avoid exact
  entity names, aliases, case ids, and database-specific labels.

Signature calibration examples:
- GOOD: "question asks for one side or one role of an entity relationship while
  the current SQL still exposes multiple relationship-side outputs".
- BAD too narrow: "question asks for a single named endpoint in one specific
  relationship table". This misses valid plural or differently worded members.
- BAD too vague: "question asks about related entities". This can trigger
  unrelated relationship questions with a different target preference.

Return strict JSON only:
{{
  "admit_pattern": true,
  "accepted_case_ids": ["..."],
  "excluded_case_ids": ["..."],
  "stable_bias_key": "short normalized phrase",
  "primary_repair_interface": "answer-blind repair interface compiler must instantiate later",
  "recognition": {{
    "question_precondition_draft": "initial abstract question precondition",
    "question_self_check": {{
      "matched_member_case_ids": ["..."],
      "missed_member_case_ids": ["..."],
      "estimated_recall": 0.0,
      "rewrite_needed": true
    }},
    "question_precondition": "abstract question type shared by accepted members; no case ids or table names",
    "sql_precondition_draft": "initial abstract SQL precondition",
    "sql_self_check": {{
      "matched_member_case_ids": ["..."],
      "missed_member_case_ids": ["..."],
      "estimated_recall": 0.0,
      "rewrite_needed": true
    }},
    "sql_precondition": "abstract pred_sql shape shared by accepted members; prefer role_family language",
    "observed_failure_summary": "common observed pred-vs-target difference; audit only, not runtime trigger",
    "repair_direction": "actionable common repair direction for branch/binder instantiation",
    "grounded_anchors": [
      {{
        "kind": "column_role | path_role | relation_role | aggregate_kind | operation_family | answer_unit",
        "role_family": "role family if applicable, no table/column literal",
        "path_role": "path role if applicable",
        "relation_role": "relation role if applicable",
        "value": "generic non-literal value if needed",
        "source_channel": "question | pred_sql | schema | delta",
        "support_case_ids": ["..."],
        "evidence": {{"why_grounded": "short audit text"}}
      }}
    ]
  }},
  "applicability": {{
    "intent_description": "audit-only sentence describing when this repair seems applicable; no executable predicates",
    "selected_source_facts": ["exact fact string copied from every admitted seed member's source_state_facts"],
    "gate_status": "enabled | disabled_empty_facts | disabled_single_seed",
    "regression_negative_guards": [],
    "evidence": {{"why_applicable": "short audit text"}}
  }},
  "binding": {{
    "source_slots": [
      {{
        "kind": "answer_slot | predicate_slot | join_path | aggregate_slot",
        "role_family": "source role family, no literal name",
        "path_role": "path role if applicable",
        "relation_role": "relation role if applicable",
        "answer_unit_role": "answer unit role if applicable",
        "optional": false,
        "constraints": {{}}
      }}
    ],
    "target_slots": [
      {{
        "kind": "answer_slot | predicate_slot | join_path | aggregate_slot",
        "role_family": "target role family, no literal name",
        "path_role": "path role if applicable",
        "relation_role": "relation role if applicable",
        "answer_unit_role": "answer unit role if applicable",
        "optional": false,
        "constraints": {{}}
      }}
    ],
    "allowed_operations": ["ActionPrimitive value from action_primitives"],
    "preserve_invariants": ["scope/grain/answer-unit invariant"],
    "evidence": {{"why_bindable": "short audit text"}}
  }},
  "branch_axes": [
    {{
      "name": "finite branch axis",
      "why_needed": "why members vary",
      "selection_signal": "current-case signal that can select branch without gold"
    }}
  ],
  "branch_specs": [
    {{
      "branch_id": "short branch id",
      "case_ids": ["..."],
      "required_interface_delta": "what differs inside the same stable bias"
    }}
  ],
  "membership_by_case": [
    {{
      "case_id": "...",
      "status": "accepted_root | rejected_root | branch_unassigned | offline_only",
      "reason": "why this case belongs, conflicts, or cannot yet be branched"
    }}
  ],
  "negative_guards": ["conditions that must prevent trigger/absorption"],
  "required_code_checks": ["compiler/replay checks still required before runtime use"],
  "reject_reason": "",
  "rationale": "one concise sentence"
}}

If not admitting, return the same object shape with:
{{
  "admit_pattern": false,
  "accepted_case_ids": [],
  "excluded_case_ids": ["all or relevant ids"],
  "stable_bias_key": "",
  "primary_repair_interface": "",
  "recognition": {{
    "question_precondition_draft": "",
    "question_self_check": {{"matched_member_case_ids": [], "missed_member_case_ids": [], "estimated_recall": 0.0, "rewrite_needed": false}},
    "question_precondition": "",
    "sql_precondition_draft": "",
    "sql_self_check": {{"matched_member_case_ids": [], "missed_member_case_ids": [], "estimated_recall": 0.0, "rewrite_needed": false}},
    "sql_precondition": "",
    "observed_failure_summary": "",
    "repair_direction": "",
    "grounded_anchors": []
  }},
  "applicability": {{
    "intent_description": "",
    "selected_source_facts": [],
    "gate_status": "disabled_empty_facts",
    "regression_negative_guards": [],
    "evidence": {{}}
  }},
  "binding": {{
    "source_slots": [],
    "target_slots": [],
    "allowed_operations": [],
    "preserve_invariants": [],
    "evidence": {{}}
  }},
  "branch_axes": [],
  "branch_specs": [],
  "membership_by_case": [],
  "negative_guards": [],
  "required_code_checks": [],
  "reject_reason": "why this is not one stable pattern",
  "rationale": "one concise sentence"
}}

Data:

case_cards_question_view (USE ONLY THIS WHEN WRITING pre_question_signature):
{case_cards_question_view_json}

case_cards_sql_view (USE ONLY THIS WHEN WRITING pre_sql_signature AND selected_source_facts):
{case_cards_sql_view_json}

case_cards_shared_view (USE FOR observed_failure_summary AND repair_direction):
{case_cards_shared_view_json}

pair_semantic_decisions (relation counts plus representative pairs only):
{pair_semantic_decisions_json}

component_summary:
{component_summary_json}

action_primitives:
{action_primitives_json}

binding_slot_guidance:
{binding_slot_guidance_json}

Pattern recognition contract:
- If admit_pattern is true, fill recognition, applicability, and binding.
- recognition.question_precondition and recognition.sql_precondition are runtime
  preconditions: describe what a future answer-blind case should look like
  before knowing gold.
- recognition.observed_failure_summary is audit-only. Do not phrase it as
  something runtime can check.
- recognition.repair_direction must be actionable enough for branch + binder to
  instantiate; if it cannot be actionable, reject admission.
- applicability.intent_description is audit-only at admission time.
- applicability.selected_source_facts is a strict source-state gate: select
  exact shared strings from the admitted members' source_state_facts only.
  Never invent, paraphrase, or specialize fact strings. If a fact is not present
  in every admitted seed member, omit it.
- Do not invent other pre-rewrite predicates or negative guards. Runtime
  negative guards are learned only from real regressions.
- binding slots must be literal-free. Use role_family/path_role/relation_role
  language from the case cards. Do not copy exact table names, column names, SQL
  aliases, expressions, database-specific labels, or case ids.
- If either self-check has estimated_recall below 0.8 for the accepted members,
  rewrite the precondition before returning. If no precondition can reach that
  recall while staying precise, reject admission.
"""


def build_pattern_admission_judge_prompt(
    *,
    case_cards_question_view_json: str,
    case_cards_sql_view_json: str,
    case_cards_shared_view_json: str,
    pair_semantic_decisions_json: str,
    component_summary_json: str,
    action_primitives_json: str,
    binding_slot_guidance_json: str,
) -> str:
    return PATTERN_ADMISSION_JUDGE_PROMPT.format(
        case_cards_question_view_json=case_cards_question_view_json,
        case_cards_sql_view_json=case_cards_sql_view_json,
        case_cards_shared_view_json=case_cards_shared_view_json,
        pair_semantic_decisions_json=pair_semantic_decisions_json,
        component_summary_json=component_summary_json,
        action_primitives_json=action_primitives_json,
        binding_slot_guidance_json=binding_slot_guidance_json,
    )


__all__ = ["PATTERN_ADMISSION_JUDGE_PROMPT", "build_pattern_admission_judge_prompt"]
