"""Insight-first pattern candidate slicer prompt."""

from __future__ import annotations


INSIGHT_PATTERN_SLICER_PROMPT = """\
Task:
Split a retrieved component of audited failed cases into candidate groups that
may share one stable benchmark/model bias.

This is candidate slicing, not final pattern admission.

Boundary:
- You are NOT deciding runtime usability.
- You are NOT writing SQL or rewrite hints.
- You are NOT requiring identical SQL edit paths.
- You ARE grouping cases by the underlying stable bias:
  source_misread + target_preference + answer-unit/scope contract.
- Different repair paths such as JOIN_REROUTE, SELECT_REPLACE, or
  JOIN_ADD_BRIDGE may stay in one candidate when they are plausible branches of
  the same stable bias.
- Prefer high recall. If a case plausibly shares the same stable bias but has a
  different SQL edit path, keep it in the candidate and let formal admission
  decide. Exclude only when source_misread, target_preference, answer unit, or
  scope contract clearly differs.
- Pair-level blocker categories are audit context from older pair scoring. They
  must not override the stable-bias frame.
- Do not group cases only because they share a broad effect axis, table shape,
  or SQL edit family.
- Do not use case ids, manual labels, database-specific vocabulary, or exact
  table/column names as grouping rules.

Definitions:
- stable_bias_hypothesis: the shared source->target contrast that explains why
  the prediction fails and why the target consistently prefers another contract.
- branch_hypothesis: how different repair paths could be finite branches of the
  same interface. If no coherent branch hypothesis exists, do not group.
- candidate_groups: groups that deserve formal pattern admission review. They
  may later be rejected.

Positive examples:
- Cases all expand a local relation/scope into a broader entity scope, but one
  needs a join reroute and another needs an output replacement. Keep together
  if the same local-scope target preference explains both.
- Cases all route a metric or answer slot to an authoritative source rather
  than a transactional/proxy source. Keep together even if some members also
  need SELECT replacement, JOIN reroute, or aggregation cleanup branches.
- Cases all replace a proxy answer unit with a canonical answer unit, while some
  need a display-column replacement and others need a route cleanup. Keep
  together if the answer-unit contract is the same.

Negative examples:
- One case fixes an aggregation grain, another fixes a case-sensitive literal,
  and another adds a ranking metric. Do not group just because SELECT changes.
- One case drops an extra paired output side while another adds a missing
  entity identity slot. Do not group: target preference differs.
- Same broad effect axis but source_misread or target_preference conflict.

Return strict JSON only:
{{
  "candidate_groups": [
    {{
      "candidate_id": "short stable id",
      "case_ids": ["..."],
      "stable_bias_hypothesis": "shared source->target contrast",
      "branch_hypothesis": "why repair path variation may be one interface",
      "why_grouped": "concise evidence from frames"
    }}
  ],
  "rejected_case_ids": ["..."],
  "rationale": "one concise sentence"
}}

Data:

stable_bias_frames:
{stable_bias_frames_json}

pair_semantic_decisions:
{pair_semantic_decisions_json}

component_summary:
{component_summary_json}
"""


def build_insight_pattern_slicer_prompt(
    *,
    stable_bias_frames_json: str,
    pair_semantic_decisions_json: str,
    component_summary_json: str,
) -> str:
    return INSIGHT_PATTERN_SLICER_PROMPT.format(
        stable_bias_frames_json=stable_bias_frames_json,
        pair_semantic_decisions_json=pair_semantic_decisions_json,
        component_summary_json=component_summary_json,
    )


__all__ = [
    "INSIGHT_PATTERN_SLICER_PROMPT",
    "build_insight_pattern_slicer_prompt",
]
