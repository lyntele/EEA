"""Runtime applicability guard similarity prompt."""

from __future__ import annotations


REGRESSION_GUARD_MATCH_PROMPT = """\
Task:
Decide whether the current answer-blind case is similar to a historical case
where this memory object caused a regression. This is a safety similarity
judgment, not a correctness judgment.

Historical regression guard:
- case_id: {guard_case_id}
- question snippet: {guard_question}
- pred_sql snippet: {guard_pred_sql}
- rewrite failure summary: {guard_summary}

Current case:
- question: {current_question}
- evidence: {current_evidence}
- pred_sql: {current_pred_sql}

Rules:
- Answer-blind: do not infer or mention gold SQL, benchmark answers, or whether
  the current SQL is correct or wrong.
- Return matches=true only when the current question and pred_sql expose the
  same kind of situation that made the historical rewrite unsafe.
- Return matches=false when the overlap is only broad topic/database/schema
  similarity, or when the current SQL shape/answer unit differs in a way that
  would make the historical regression not informative.
- Do not invent a new guard. This call only compares the current case to the
  provided historical guard.

Return strict JSON only:
{{"matches": true/false, "confidence": 0.0-1.0, "reason": "short reason"}}
"""


def build_regression_guard_match_prompt(
    *,
    guard_case_id: str,
    guard_question: str,
    guard_pred_sql: str,
    guard_summary: str,
    current_question: str,
    current_evidence: str,
    current_pred_sql: str,
) -> str:
    return REGRESSION_GUARD_MATCH_PROMPT.format(
        guard_case_id=guard_case_id,
        guard_question=guard_question,
        guard_pred_sql=guard_pred_sql,
        guard_summary=guard_summary,
        current_question=current_question,
        current_evidence=current_evidence,
        current_pred_sql=current_pred_sql,
    )


__all__ = [
    "REGRESSION_GUARD_MATCH_PROMPT",
    "build_regression_guard_match_prompt",
]
