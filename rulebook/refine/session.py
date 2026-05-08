from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from method.EEA.rulebook.common.llm.client import LLMClient
from method.EEA.rulebook.refine.prompts import build_refinement_messages
from method.EEA.rulebook.common.core.schema import (
    CaseInput,
    Mismatch,
    RefinementSessionTrace,
    RefinementStep,
    RefinementTurnTrace,
    SignalsDetails,
    SignalsSummary,
)


@dataclass
class RefinementContext:
    """
    Immutable context for a refinement session.

    This keeps the high-level case metadata and static diagnostics that do not
    change across turns.
    """

    case: CaseInput
    branch_id: Optional[str] = None


@dataclass
class RefinementSession:
    """
    A lightweight interactive refinement session on top of a single base SQL.

    The session itself does not execute SQL; the caller is responsible for:
    - running the proposed SQL,
    - computing Mismatch / SignalsSummary,
    - feeding those back into subsequent turns via `step(...)`.
    """

    llm_client: LLMClient
    context: RefinementContext
    base_sql: str
    trace: RefinementSessionTrace = field(default_factory=lambda: RefinementSessionTrace(base_sql=""))

    def __post_init__(self) -> None:
        # Initialize trace with static metadata.
        self.trace.base_sql = self.base_sql
        self.trace.case_id = self.context.case.meta.case_id
        self.trace.branch_id = self.context.branch_id

    @property
    def turns(self) -> List[RefinementTurnTrace]:
        return self.trace.turns

    @property
    def latest_sql(self) -> str:
        if not self.turns:
            return self.base_sql
        last_step_sql = self.turns[-1].step.sql
        return last_step_sql or self.base_sql

    def _build_payload(
        self,
        mismatch: Mismatch,
        signals_summary: SignalsSummary,
        signals_details: Optional[SignalsDetails] = None,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Build a compact, LLM-friendly payload summarizing the current state.
        """
        case = self.context.case
        # Derive a focused schema subgraph similar to TK-Boost's schema snippets:
        # only include tables that actually appear in the current AST signatures.
        focused_tables: Dict[str, List[str]] = {}
        ast_pred = {}
        ast_gold = {}
        if signals_details is not None:
            try:
                ast_pred = signals_details.pred_ast or {}
            except Exception:
                ast_pred = {}
            try:
                ast_gold = signals_details.gold_ast or {}
            except Exception:
                ast_gold = {}
        tables_pred = ast_pred.get("tables_involved", []) if isinstance(ast_pred, dict) else []
        tables_gold = ast_gold.get("tables_involved", []) if isinstance(ast_gold, dict) else []
        focused_table_names = sorted({str(t) for t in (tables_pred or []) + (tables_gold or [])})
        for t in focused_table_names:
            cols = case.schema.tables.get(t, [])
            if cols:
                focused_tables[t] = cols

        payload: Dict[str, Any] = {
            "case_id": case.meta.case_id,
            "db_id": case.meta.db_id,
            "question": case.nl.question,
            "agent_sql": self.latest_sql,
            # Expose the GOLD SQL text so the refiner can compare structure
            # against the authoritative solution, while the prompt and agent
            # still enforce minimal edits relative to AGENT_SQL.
            "gold_sql": case.sql.gold_sql,
            "base_sql": self.base_sql,
            # Full schema plus a narrower, AST-driven focused subgraph.
            "schema_brief": {
                "tables": case.schema.tables,
                "fk_edges": case.schema.fk_edges,
            },
            "schema_context": {
                "focused_tables": focused_tables,
                "all_tables": case.schema.tables,
            },
            "mismatch": mismatch.dict(),
            "signals_summary": signals_summary.dict(),
            "signals_details": (signals_details.dict() if signals_details is not None else {}),
            "history": [
                {
                    "OBSERVED_DIFFS": turn.step.observed_diffs,
                    "EDIT_PLAN": turn.step.edit_plan,
                    "PREDICTED_SECONDARY_DIFFS": turn.step.predicted_secondary_diffs,
                    "FOLLOWUP_STRATEGY": turn.step.followup_strategy,
                    "SQL": turn.step.sql,
                    "MATCH_OK": turn.step.match_ok,
                    "NO_DIFF": turn.step.no_diff,
                }
                for turn in self.turns
            ],
        }

        # Merge caller-provided extra_context with automatically derived
        # join skeleton candidates (graph-level suggestions).
        merged_extra: Dict[str, Any] = dict(extra_context or {})

        # Lightweight graph-based join skeleton search:
        # when join_edges_diff is flagged and we have detailed AST / diff
        # info, derive 1–2 candidate join skeletons that the refiner can
        # use as high-level FROM/JOIN blueprints.
        try:
            if signals_details is not None and signals_summary.join_edges_diff:
                sd = signals_details
                pred_ast = sd.pred_ast or {}
                gold_ast = sd.gold_ast or {}
                join_diff = sd.join_edges_diff_detail or {}

                pred_joins = pred_ast.get("join_edges", []) if isinstance(pred_ast, dict) else []
                gold_joins = gold_ast.get("join_edges", []) if isinstance(gold_ast, dict) else []

                pred_only = join_diff.get("pred_only") or []
                gold_only = join_diff.get("gold_only") or []

                # Helper: normalize ON condition strings from diff entries.
                def _on_conditions(entries: Any) -> List[str]:
                    out: List[str] = []
                    if isinstance(entries, list):
                        for item in entries:
                            # Expected shape: [table, join_type, on_expr]
                            if isinstance(item, (list, tuple)) and len(item) >= 3:
                                on_expr = str(item[2]).strip()
                                if on_expr:
                                    out.append(on_expr)
                    return out

                pred_only_on = set(_on_conditions(pred_only))
                gold_only_on = set(_on_conditions(gold_only))

                # Candidate 1: pure gold-side join skeleton (if available).
                join_skeleton_candidates: List[Dict[str, Any]] = []
                if gold_joins:
                    gold_tables = gold_ast.get("tables_involved", []) if isinstance(gold_ast, dict) else []
                    # Pick a stable base table: first gold table that exists in schema.
                    base_table = None
                    for tname in gold_tables:
                        if tname in case.schema.tables:
                            base_table = tname
                            break
                    if base_table is None and gold_tables:
                        base_table = str(gold_tables[0])

                    if base_table is not None:
                        join_skeleton_candidates.append(
                            {
                                "skeleton_id": "gold_join_graph",
                                "source": "diagnostics_gold_ast",
                                "base_table": base_table,
                                "joins": [
                                    {
                                        "table": j.get("table"),
                                        "join_type": j.get("join_type"),
                                        "on": j.get("on"),
                                    }
                                    for j in gold_joins
                                ],
                            }
                        )

                # Candidate 2: blended skeleton — drop pred_only ON predicates
                # and splice in gold_only edges, preserving pred-side ordering.
                if pred_joins or gold_only_on:
                    # Start from pred-side join edges but remove those whose ON
                    # matches any pred_only edge.
                    mixed_joins: List[Dict[str, Any]] = []
                    for j in pred_joins:
                        on_expr = str(j.get("on", "")).strip()
                        if on_expr and on_expr in pred_only_on:
                            continue
                        mixed_joins.append(j)

                    # Append any gold_only ON predicates that are not already present.
                    existing_on = {str(j.get("on", "")).strip() for j in mixed_joins if j.get("on")}
                    for j in gold_joins:
                        on_expr = str(j.get("on", "")).strip()
                        if on_expr and on_expr in gold_only_on and on_expr not in existing_on:
                            mixed_joins.append(j)

                    if mixed_joins:
                        mixed_tables = pred_ast.get("tables_involved", []) if isinstance(pred_ast, dict) else []
                        if not mixed_tables and isinstance(gold_ast, dict):
                            mixed_tables = gold_ast.get("tables_involved", []) or []

                        base_table_mixed = None
                        for tname in mixed_tables:
                            if tname in case.schema.tables:
                                base_table_mixed = tname
                                break
                        if base_table_mixed is None and mixed_tables:
                            base_table_mixed = str(mixed_tables[0])

                        if base_table_mixed is not None:
                            join_skeleton_candidates.append(
                                {
                                    "skeleton_id": "mixed_join_graph",
                                    "source": "diagnostics_pred_gold_blend",
                                    "base_table": base_table_mixed,
                                    "joins": [
                                        {
                                            "table": j.get("table"),
                                            "join_type": j.get("join_type"),
                                            "on": j.get("on"),
                                        }
                                        for j in mixed_joins
                                    ],
                                }
                            )

                if join_skeleton_candidates:
                    merged_extra.setdefault("join_skeleton_candidates", join_skeleton_candidates)
        except Exception:
            # Graph-based join skeleton derivation is best-effort only and
            # must never break the core refinement loop.
            pass

        if merged_extra:
            payload["extra_context"] = merged_extra
        return payload

    def step(
        self,
        mismatch: Mismatch,
        signals_summary: SignalsSummary,
        signals_details: Optional[SignalsDetails] = None,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[RefinementStep, str]:
        """
        Run a single refinement turn.

        Returns:
            (RefinementStep, raw_response_text)
        """

        if not self.llm_client.is_ready():
            raise RuntimeError("LLM client is not configured for refinement session.")

        payload = self._build_payload(
            mismatch,
            signals_summary,
            signals_details=signals_details,
            extra_context=extra_context,
        )
        messages = build_refinement_messages(payload)

        start = time.time()
        # Use the public chat interface of LLMClient for consistency.
        response = self.llm_client.chat(messages, temperature=None)
        latency = time.time() - start

        if not response:
            raise RuntimeError("LLM refinement call failed or returned empty response.")

        raw_text = response.text
        try:
            obj = json.loads(raw_text)
        except Exception:
            # If the model wrapped JSON in markdown fences, try to extract the first JSON object/array.
            json_start = raw_text.find("{")
            json_end = raw_text.rfind("}")
            if json_start >= 0 and json_end > json_start:
                try:
                    obj = json.loads(raw_text[json_start : json_end + 1])
                except Exception as exc:  # pragma: no cover - defensive path
                    raise RuntimeError(f"Failed to parse refinement JSON: {exc}") from exc
            else:  # pragma: no cover - defensive path
                raise RuntimeError("Failed to locate JSON object in refinement response.")

        step = RefinementStep.parse_obj(obj)

        turn_trace = RefinementTurnTrace(
            step=step,
            raw_response_text=raw_text,
            llm_latency_sec=round(latency, 3),
        )
        self.trace.turns.append(turn_trace)

        return step, raw_text

