"""Compatibility Judge prompt —— advisory-only shared-interface explainer.

该节点不再参与 runtime / promotion 的硬门。真正的 family/pattern gate 由：
- SharedProgramSynthesizer
- ProgramCoverage
- replay / leave-one-out

本 prompt 只负责解释可能的共享接口、变体轴、以及建议的代码检查点。
"""

from __future__ import annotations

COMPATIBILITY_JUDGE_PROMPT = """\
Task:
Audit whether a set of ErrorInstances appears to share a common repair
interface, and explain likely blockers or variation axes. This output is
advisory only; code-side synthesis, coverage, and replay remain authoritative.

Inputs:
- instances: a set of ErrorInstanceV2 (>= 2)
- test_mode: "pairwise" (pairwise judgment) / "family" (whole-family test) /
  "leave_one_out" (leave-one-out)
- candidate_instantiation_rule (optional): a draft unified instantiation rule to
  test

Analyze these independent dimensions:

1. shared_repair_skeleton
   - All members' repair_skeleton.structural.locus is identical or compatible.
   - op_family / target_family are identical or compatible. output_contract need
     not be identical but must not contradict — e.g. one member being 2->1 and
     another being 1->2 is a contradiction.
   - Write the reasoning to reasoning.skeleton.

2. shared_semantic_intent
   - All repair_goal values point in the same direction.
   - No opposing actions such as "add column" vs "drop column" or "add JOIN" vs
     "remove JOIN".
   - Write the reasoning to reasoning.intent.

3. shared_instantiation_shape
   - The `kind` set of instantiation_slots must match (slot names and
     allowed_role_families need not match).
   - The set of required slots must match.
   - Write the reasoning to reasoning.shape.

4. no_guardrail_conflict
   - For every pair (A, B), check whether A's guardrail would block B and vice
     versa.
   - Write the reasoning to reasoning.guardrail.

If test_mode == "leave_one_out":
- Explain which held-out members seem likely to violate the shared interface and why.

Strictly forbidden:
- Treating surface SQL similarity as sufficient evidence.
- Treating output_contract family membership alone as sufficient evidence.
- Declaring promotion/runtime eligibility. You are only producing audit notes.

Output JSON schema:
{{
  "advisory_only": true,
  "reason": "one-sentence summary",
  "reasoning": {{
    "skeleton": "...",
    "intent": "...",
    "shape": "...",
    "guardrail": "..."
  }},
  "semantic_alignment_notes": "...",
  "possible_shared_interface": "...",
  "variation_axes": [],
  "suspected_blockers": [],
  "recommended_code_checks": [],
  "leave_one_out_results": [
    {{
      "left_out_case_id": "...",
      "likely_rule_covers": true | false,
      "note": "..."
    }}
  ]
}}

Data:

test_mode: {test_mode}

instances:
{instances_json}

candidate_instantiation_rule:
{candidate_instantiation_rule}
"""


def build_compatibility_judge_prompt(
    *,
    instances_json: str,
    test_mode: str = "pairwise",
    candidate_instantiation_rule: str = "",
) -> str:
    return COMPATIBILITY_JUDGE_PROMPT.format(
        test_mode=test_mode,
        instances_json=instances_json,
        candidate_instantiation_rule=candidate_instantiation_rule or "(not provided)",
    )


__all__ = ["COMPATIBILITY_JUDGE_PROMPT", "build_compatibility_judge_prompt"]
