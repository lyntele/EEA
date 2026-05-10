"""Prompt for comparing two emergent pattern recognition contracts."""

from __future__ import annotations


PATTERN_EQUIVALENCE_JUDGE_PROMPT = """\
Task:
Compare two pattern recognition contracts. Decide whether they describe the
same underlying pre-condition, whether one subsumes the other, or whether they
are disjoint.

Pattern A:
{left_json}

Pattern B:
{right_json}

Definitions:
- equivalent: both contracts describe the same recurring question/pred_sql
  pre-condition and repair direction, even if phrased differently.
- left_subsumes_right: A is a broader description that covers B.
- right_subsumes_left: B is a broader description that covers A.
- disjoint: the answer unit, SQL pre-condition, or repair direction differs.

Rules:
- Do not use gold SQL or benchmark correctness.
- Prefer semantic equivalence over exact wording.
- Reject as disjoint when the repair direction or answer unit is materially
  different.
- Do not invent a new pattern; only compare these two contracts.

Return strict JSON only:
{{"relation": "equivalent|left_subsumes_right|right_subsumes_left|disjoint",
  "confidence": 0.0-1.0,
  "reason": "short reason"}}
"""


def build_pattern_equivalence_judge_prompt(
    *,
    left_json: str,
    right_json: str,
) -> str:
    return PATTERN_EQUIVALENCE_JUDGE_PROMPT.format(
        left_json=left_json,
        right_json=right_json,
    )


__all__ = [
    "PATTERN_EQUIVALENCE_JUDGE_PROMPT",
    "build_pattern_equivalence_judge_prompt",
]
