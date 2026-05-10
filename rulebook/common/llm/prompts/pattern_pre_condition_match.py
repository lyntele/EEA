"""Runtime pre-condition similarity prompts."""

from __future__ import annotations


PATTERN_PRE_CONDITION_Q_PROMPT = """\
Task:
Decide whether the current question falls under the pattern question
pre-condition. This is a similarity judgment, not a correctness judgment.

Pattern pre_question_signature:
{signature}

Pattern repair_direction:
{repair_direction}

Current question:
{question}

Evidence:
{evidence}

Rules:
- Answer-blind: do not infer or mention gold SQL, benchmark answers, or whether
  the current SQL is wrong.
- Return matches=true only when the question-side target intent is covered by
  the signature.
- The question channel should not require the question to mention a SQL-side
  failure mechanism. Wrong route, bridge misuse, duplicated joins, proxy source
  choice, and other pred_sql misconceptions are checked by the SQL channel.
- Do not reject solely because the question uses a different wording, singular
  vs plural phrasing, or branch-level answer unit, when it still expresses the
  same question-side target preference described by the signature.
- Return matches=false when the current question points to a different source
  misconception, target preference, metric/formula, or non-branchable scope.

Calibration examples:
- GOOD match: the signature says "asks for one role of a relationship while the
  SQL exposes multiple relationship-side outputs"; the current question asks
  for either side, one side, or a plural set of that role.
- GOOD match: the signature says "asks for entities satisfying a relation or
  attribute constraint"; the current question asks for the same target entities
  even if the wrong join route is only visible in pred_sql.
- BAD reject: the signature says "drop an extra relationship-side output" but
  the current question asks for a different aggregate metric or ranking target.

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

Pattern repair_direction:
{repair_direction}

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
- Do not reject solely because alias names, column names, or branch-level
  dependencies differ. Those are handled by branch selection and compiler
  binding after this pre-condition check.
- Return matches=false when the SQL lacks the source-side shape in the
  signature or expresses a different non-branchable grain, route scope, metric,
  or answer contract.

Calibration examples:
- GOOD match: the signature says "pred_sql exposes multiple role-side outputs
  from a relationship-like structure"; the current SQL uses different aliases
  but still exposes multiple role-side outputs.
- BAD reject: the signature says "extra relationship-side output" but the
  current SQL is only missing a ranking output or formula column.

Return strict JSON only:
{{"matches": true/false, "confidence": 0.0-1.0, "reason": "short reason"}}
"""


def build_pattern_pre_condition_q_prompt(
    *,
    signature: str,
    repair_direction: str = "",
    question: str,
    evidence: str = "",
) -> str:
    return PATTERN_PRE_CONDITION_Q_PROMPT.format(
        signature=signature,
        repair_direction=repair_direction,
        question=question,
        evidence=evidence,
    )


def build_pattern_pre_condition_s_prompt(
    *,
    signature: str,
    repair_direction: str = "",
    pred_sql: str,
    schema_excerpt_json: str,
) -> str:
    return PATTERN_PRE_CONDITION_S_PROMPT.format(
        signature=signature,
        repair_direction=repair_direction,
        pred_sql=pred_sql,
        schema_excerpt_json=schema_excerpt_json,
    )


__all__ = [
    "PATTERN_PRE_CONDITION_Q_PROMPT",
    "PATTERN_PRE_CONDITION_S_PROMPT",
    "build_pattern_pre_condition_q_prompt",
    "build_pattern_pre_condition_s_prompt",
]
