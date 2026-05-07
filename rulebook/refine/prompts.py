from __future__ import annotations

import json
from typing import Any, Dict, List


def build_generator_messages(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Legacy candidate-generator prompt retained for compatibility with `repair.py`.
    """
    system = (
        "You are a SQL candidate generator. "
        "Return a small set of valid SQLite candidate queries that minimally edit BASE_SQL. "
        "Respond with a single JSON object only.\n"
        "Required format:\n"
        '{ "candidates": [{"sql": "...", "op_sequence": ["LLM_GENERATOR"], "op_args_sigs": []}] }\n'
        "Rules:\n"
        "- Only use tables and columns present in the provided schema.\n"
        "- Prefer minimal structural fixes over full rewrites.\n"
        "- Generate at most 3 candidates.\n"
        "- Do not include markdown fences or explanation text."
    )

    user = {
        "task": "Generate candidate SQL repairs.",
        "input": payload,
        "output_schema": {
            "candidates": [
                {
                    "sql": "string",
                    "op_sequence": ["string"],
                    "op_args_sigs": ["string"],
                }
            ]
        },
    }

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def build_refinement_messages(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Build messages for one refiner turn.

    The model receives both AGENT_SQL and GOLD_SQL, plus diagnostics and
    execution briefs, but is required to stay close to AGENT_SQL and make only
    minimal edits.
    """
    system = (
        "You are a SQL MINIMAL-REPAIR agent.\n"
        "Your job is to repair AGENT_SQL by making one minimal clause-level edit per turn, using GOLD_SQL only as a structural reference.\n"
        "You are operating in a TK-Boost-style diff/repair loop:\n"
        "- Compare AGENT_SQL against GOLD_SQL and the execution briefs before proposing any edit.\n"
        "- Prefer one minimal structural edit at a time: JOIN type/condition, SELECT expression, DISTINCT, WHERE predicate, GROUP BY, aggregation, or subquery shape.\n"
        "- Prefer cumulative forward progress. Do not oscillate between contradictory edits.\n"
        "- If extra_context.repeat_plan_count > 0, you are repeating yourself. You MUST switch to a different repair axis.\n"
        "- If extra_context.prev_candidate_checks is present, treat those invalid columns/qualifiers as hard constraints.\n"
        "- If extra_context.join_skeleton_candidates is present and join_edges_diff is true, align your edit with one of those skeletons when possible.\n"
        "- Use only tables and columns present in schema_brief/schema_context.\n"
        "- NEVER hardcode final output rows or hidden target values.\n"
        "- NEVER copy GOLD_SQL wholesale. Keep the edit close to AGENT_SQL.\n"
        "- If AGENT result is empty but GOLD result is not, do not assume the data is missing; diagnose the table/join/filter mismatch.\n"
        "\n"
        "OUTPUT RULES:\n"
        "- Return one JSON object only.\n"
        "- Propose exactly one SQL candidate in SQL. Do not return alternative SQL candidates unless absolutely necessary.\n"
        "- OBSERVED_DIFFS must be one short line of concrete differences tagged [STRUCTURAL] or [COSMETIC].\n"
        "- EDIT_PLAN must be one short line naming the single next minimal edit and include TYPE=[STRUCTURAL] or TYPE=[COSMETIC].\n"
        "- PREDICTED_SECONDARY_DIFFS should briefly state what may still remain after this edit.\n"
        "- FOLLOWUP_STRATEGY should be a short 1-2 step next plan; if no follow-up is needed, return an empty string.\n"
        "- SQL must be a minimal edit of AGENT_SQL, not a rewrite from scratch.\n"
        "- MATCH_OK is true only if the current AGENT_SQL already matches the target result.\n"
        "- NO_DIFF is true only if you cannot propose another meaningful edit.\n"
    )

    user = {
        "task": "Propose the next minimal SQL edit given the diagnostic signals.",
        "constraints": [
            "Base all reasoning on AGENT_SQL and the provided diagnostics.",
            "Do not invent tables or columns that are not in the schema_brief/schema_context.",
            "If you need to expose a new semantic field (e.g., a flag), construct it from existing columns using expressions (CASE, IIF, arithmetic, etc.) and only surface it via AS alias; never assume it exists as a stored column.",
            "When prev_candidate_checks reports UNKNOWN_COLUMN or related violations, you must avoid those exact identifiers and qualifiers in subsequent SQL.",
            "Do not hardcode hidden target outputs or overfit to specific literal values.",
            "Use GOLD_SQL as a structural reference, but keep your proposed SQL close to AGENT_SQL.",
            "If you repeated the same edit plan previously, change repair axis instead of repeating it.",
        ],
        "input": payload,
        "output_schema": {
            "OBSERVED_DIFFS": "string",
            "EDIT_PLAN": "string",
            "PREDICTED_SECONDARY_DIFFS": "string",
            "FOLLOWUP_STRATEGY": "string",
            "SQL": "string",
            "MATCH_OK": "boolean",
            "NO_DIFF": "boolean",
        },
    }

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]
