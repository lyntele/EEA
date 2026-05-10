"""Runtime pre-condition similarity prompts."""

from __future__ import annotations


PATTERN_PRE_CONDITION_Q_PROMPT = """\
Task:
Decide whether the current question falls under the pattern question
pre-condition. This is a similarity judgment, not a correctness judgment.

Pattern pre_question_signature:
{signature}

Current question:
{question}

Evidence:
{evidence}

Rules:
- Answer-blind: do not infer or mention gold SQL, benchmark answers, or whether
  the current SQL is wrong.
- Return matches=true only when the question type is covered by the signature.
- If the signature is too vague or the question asks for a different answer
  unit/grain/metric, return matches=false.

Return strict JSON only:
{{"matches": true/false, "confidence": 0.0-1.0, "reason": "short reason"}}
"""


PATTERN_PRE_CONDITION_S_PROMPT = """\
Task:
Decide whether the current pred_sql exhibits the pattern SQL pre-condition.
This is a similarity judgment about SQL shape and schema roles, not a
correctness judgment.

Pattern pre_sql_signature:
{signature}

Current pred_sql:
{pred_sql}

Relevant schema with role_family hints:
{schema_excerpt_json}

Rules:
- Answer-blind: do not infer or mention gold SQL, benchmark answers, or whether
  pred_sql is wrong.
- Return matches=true only when the SQL shape described by the signature is
  visible in pred_sql/schema.
- Prefer role_family and SQL structure over exact table/column names.
- If the signature describes a different output grain, route, answer unit, or
  SQL shape, return matches=false.

Return strict JSON only:
{{"matches": true/false, "confidence": 0.0-1.0, "reason": "short reason"}}
"""


def build_pattern_pre_condition_q_prompt(
    *,
    signature: str,
    question: str,
    evidence: str = "",
) -> str:
    return PATTERN_PRE_CONDITION_Q_PROMPT.format(
        signature=signature,
        question=question,
        evidence=evidence,
    )


def build_pattern_pre_condition_s_prompt(
    *,
    signature: str,
    pred_sql: str,
    schema_excerpt_json: str,
) -> str:
    return PATTERN_PRE_CONDITION_S_PROMPT.format(
        signature=signature,
        pred_sql=pred_sql,
        schema_excerpt_json=schema_excerpt_json,
    )


__all__ = [
    "PATTERN_PRE_CONDITION_Q_PROMPT",
    "PATTERN_PRE_CONDITION_S_PROMPT",
    "build_pattern_pre_condition_q_prompt",
    "build_pattern_pre_condition_s_prompt",
]
