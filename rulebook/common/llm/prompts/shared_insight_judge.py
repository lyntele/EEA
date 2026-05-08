"""Shared Insight Judge prompt.

This prompt compares compact case-local repair cards after code-side coarse
retrieval. It decides semantic compatibility only; code-side formation,
compiler coverage, and replay still decide whether a family/pattern can be
materialized or promoted.
"""

from __future__ import annotations

SHARED_INSIGHT_JUDGE_PROMPT = """\
Task:
Compare case-local repair insight cards and decide whether they can share one
reusable repair insight. These cards were already retrieved by code using broad
signals such as effect axes and canonical edit families.

Boundary:
- You are NOT clustering all cases.
- You are NOT selecting SQL edits or inventing compiler arguments.
- You are NOT deciding runtime promotion.
- You ONLY judge whether the member cards express the same source->target repair
  insight at an abstract, non-case-specific level.
- Code will still run canonical-program synthesis, compiler coverage, action
  count limits, guards, and replay after your answer.

Definitions:
- effect axis: a broad namespace such as output_shape_delta, source_route_delta,
  predicate_scope_delta, formula_delta, or ranking_contract_delta. Axis overlap
  is only a retrieval clue; it is never sufficient by itself.
- case-local insight: one audited failure's contrast: what the prediction
  misunderstood, what the target/gold prefers, what repair interface would
  correct it, which invariants must be preserved, and when not to apply it.
- shared insight: one answer-blind repair interface that explains every member
  card without losing a member's required preserve invariant or negative guard.
- core repair insight: the stable source->target mistake that decides whether
  members belong to the same reusable pattern.
- branch/accessory constraint: a member-local condition learned from its own
  repair trace, such as deduplicating the repaired output, applying target-only
  ordering, or preserving/removing a supporting route after the same core repair.
  These constraints may require separate runtime branches; they are not by
  themselves a conflict when the core repair insight is shared.
- compatible: all cards can be summarized by one shared repair interface; wording
  differences, different table/column names, and branch/accessory differences
  are allowed if one core repair insight covers every member.
- partial: cards point to the same broad experience, but a reusable repair
  interface still has unresolved axes or member-specific branches; safe only as
  offline family evidence, not as a direct runtime pattern.
- conflict: merging would lose a required condition, invert the operation, mix
  different answer units, or require incompatible actions.

Positive examples:
- Card A says pred outputs two answer sides and target drops one side while
  preserving the same filter scope. Card B says pred outputs a paired entity and
  target keeps one answer unit, with different table/column names. Compatible:
  shared interface is "drop extra paired output side preserving scope".
- Card A adds deduplication after dropping the extra output side, while Card B
  only drops the extra output side. Compatible if the shared core is the same;
  report deduplication as a branch/accessory check that code must preserve or
  select later.
- Card A says pred selects a proxy/display slot and target uses a canonical
  answer slot under the same predicates. Card B says the same with different
  schema identifiers. Compatible if source/target slot roles align and no
  member requires retaining the proxy slot.
- Card A says pred omits a rank column while already sorting by the rank metric.
  Card B says pred omits the materialized ranking output for a grouped count.
  Partial if both need "materialize ranking output" but metric binding differs.

Negative examples:
- Both cards have output_shape_delta, but one adds a missing metric column and
  the other drops an extra paired output side. Conflict: same axis, opposite
  repair interface.
- One card must preserve a predicate and another repair requires deleting that
  predicate. Conflict: lost preserve invariant.
- One card changes answer grain from row to aggregate and another only replaces
  a display slot while preserving grain. Partial or conflict unless one shared
  interface explicitly explains both.
- Do not mark compatible because of database names, case ids, manual group names,
  specific table/column names, or surface phrase overlap.

Return strict JSON only:
{{
  "compatibility": "compatible | partial | conflict",
  "shared_interface_key": "short normalized phrase if compatible, else empty",
  "shared_insight": {{
    "source_misread": "...",
    "target_preference": "...",
    "repair_interface": "...",
    "binding_slots": [...],
    "preserve_invariants": [...],
    "negative_guards": [...],
    "axis_links": [...]
  }},
  "conflict_reasons": ["..."],
  "lost_constraints": ["member constraint that would be lost if merged"],
  "unresolved_axes": ["axis or branch that remains unresolved for partial"],
  "required_code_checks": ["compiler/replay checks code must still perform"],
  "rationale": "one concise sentence"
}}

Data:

insight_cards:
{insight_cards_json}

effect_candidates:
{effect_candidates_json}

repair_program_summaries:
{repair_program_summaries_json}
"""


def build_shared_insight_judge_prompt(
    *,
    insight_cards_json: str,
    effect_candidates_json: str,
    repair_program_summaries_json: str,
) -> str:
    return SHARED_INSIGHT_JUDGE_PROMPT.format(
        insight_cards_json=insight_cards_json,
        effect_candidates_json=effect_candidates_json,
        repair_program_summaries_json=repair_program_summaries_json,
    )


__all__ = ["SHARED_INSIGHT_JUDGE_PROMPT", "build_shared_insight_judge_prompt"]
