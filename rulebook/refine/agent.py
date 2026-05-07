from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from method.EEA.rulebook.common.config import RulebookConfig, load_config
from method.EEA.rulebook.common.llm_client import LLMClient
from method.EEA.rulebook.common.schema import (
    BranchPlan,
    BranchStepRecord,
    BranchTrace,
    CaseInput,
    CaseTrace,
    GlobalDecision,
)
from method.EEA.rulebook.refine.session import RefinementContext, RefinementSession
from method.EEA.rulebook.refine.signals import compare_results, compute_fingerprints, execute_sql, parse_ast_signature, run_probes
from method.EEA.rulebook.refine.checker import validate_sql_columns


def _score_mismatch(mismatch: Any, *, weights: Optional[Dict[str, float]] = None) -> float:
    """
    Convert a mismatch struct into a scalar score for ranking candidates.
    Higher is better. Correct results get a very large score.
    """
    if getattr(mismatch, "mismatch_type", None) == "correct":
        return 1e9

    w = weights or {"row_count": 0.4, "scalar": 0.3, "jaccard": 0.3}
    d = getattr(mismatch, "distance", {}) or {}

    # row_count_ratio close to 1.0 is best; map via log-distance and clamp.
    row_ratio = d.get("row_count_ratio")
    row_score = 0.0
    try:
        if isinstance(row_ratio, (int, float)) and row_ratio > 0:
            import math

            dist = abs(math.log(float(row_ratio)))
            row_score = 1.0 - min(dist, 2.0) / 2.0
    except Exception:
        row_score = 0.0

    # scalar_log_ratio close to 0 is best.
    scalar_lr = d.get("scalar_log_ratio")
    scalar_score = 0.0
    try:
        if isinstance(scalar_lr, (int, float)):
            dist = abs(float(scalar_lr))
            scalar_score = 1.0 - min(dist, 2.0) / 2.0
    except Exception:
        scalar_score = 0.0

    # jaccard_at_k close to 1 is best.
    j = d.get("jaccard_at_k")
    jaccard_score = float(j) if isinstance(j, (int, float)) else 0.0

    score = (
        float(w.get("row_count", 0.4)) * row_score
        + float(w.get("scalar", 0.3)) * scalar_score
        + float(w.get("jaccard", 0.3)) * jaccard_score
    )
    return float(score)


def _normalize_sql(sql: str) -> str:
    return " ".join((sql or "").split())


def _normalize_text(text: str) -> str:
    return " ".join((text or "").lower().split())


def _execution_brief(result: Any) -> Dict[str, Any]:
    return {
        "result_type": getattr(result, "result_type", None),
        "row_count": getattr(result, "row_count", None),
        "columns": getattr(result, "columns", None),
        "sample_rows": getattr(result, "sample_rows", None),
        "scalar_value": getattr(result, "scalar_value", None),
        "error": getattr(result, "error", None),
    }


def _sql_similarity(sql1: str, sql2: str) -> float:
    if not sql1 or not sql2:
        return 0.0
    import re

    tokens1 = set(re.findall(r"\w+", _normalize_sql(sql1).lower()))
    tokens2 = set(re.findall(r"\w+", _normalize_sql(sql2).lower()))
    if not tokens1 or not tokens2:
        return 0.0
    return len(tokens1 & tokens2) / len(tokens1 | tokens2)


def _is_minimality_violation(candidate_sql: str, current_sql: str, gold_sql: str) -> bool:
    gold_similarity = _sql_similarity(candidate_sql, gold_sql)
    current_similarity = _sql_similarity(candidate_sql, current_sql)
    return gold_similarity > 0.78 and gold_similarity > current_similarity + 0.18


class RulebookAgent:
    def __init__(self, config: Optional[RulebookConfig] = None):
        self.config = config or load_config()
        self.llm_client = LLMClient(self.config.llm, timeout=self.config.rulebook.llm_timeout)

    def run_single_case(self, case: CaseInput, mode: Optional[str] = None) -> CaseTrace:
        """
        Run a single case with the refiner-only pipeline.

        `mode` is kept only for backwards compatibility with older callers.
        """
        resolved_mode = (mode or "refiner").lower()
        if resolved_mode != "refiner":
            raise ValueError("Legacy mode has been removed. Use refiner mode only.")
        return self._run_single_case_refiner_only(case)

    def _run_single_case_refiner_only(self, case: CaseInput) -> CaseTrace:
        start_time = time.time()
        db_path = case.meta.db_path
        initial_sql = case.sql.pred_sql
        gold_sql = case.sql.gold_sql

        ast_pred = parse_ast_signature(initial_sql)
        fingerprints = compute_fingerprints(ast_pred)

        pred_exec = execute_sql(db_path, initial_sql, timeout=case.settings.timeout_sec)
        gold_exec = execute_sql(db_path, gold_sql, timeout=case.settings.timeout_sec)
        initial_mismatch = compare_results(pred_exec, gold_exec)
        signals_summary, signals_details = run_probes(
            db_path=db_path,
            pred_sql=initial_sql,
            gold_sql=gold_sql,
            timeout=case.settings.probe_timeout_sec,
        )

        refiner_plan = BranchPlan(
            plan_id="REFINER",
            branch_role="REFINEMENT_ONLY",
            reasoning="Interactive refinement-only loop.",
            actions=[],
            expected_ast_diff={},
            source="REFINER",
        )
        refiner_trace = BranchTrace(plan=refiner_plan.dict())

        total_exec_count = 2
        llm_calls = 0

        if initial_mismatch.mismatch_type == "correct":
            refiner_trace.final_status = "DONE"
            refiner_trace.final_sql = initial_sql
            return self._build_case_trace(
                case=case,
                initial_mismatch=initial_mismatch,
                signals_summary=signals_summary,
                signals_details=signals_details,
                fingerprints=fingerprints,
                refiner_trace=refiner_trace,
                global_decision=GlobalDecision(
                    global_status="FIXED",
                    winner_branch_id="REFINER",
                    winner_sql=initial_sql,
                    stop_reason="INITIAL_SQL_ALREADY_CORRECT",
                ),
                total_exec_count=total_exec_count,
                llm_calls=llm_calls,
                start_time=start_time,
            )

        if not self.config.rulebook.llm_enabled or not self.llm_client.is_ready():
            refiner_trace.final_status = "DEAD"
            return self._build_case_trace(
                case=case,
                initial_mismatch=initial_mismatch,
                signals_summary=signals_summary,
                signals_details=signals_details,
                fingerprints=fingerprints,
                refiner_trace=refiner_trace,
                global_decision=GlobalDecision(
                    global_status="STUCK",
                    stop_reason="LLM_NOT_CONFIGURED",
                ),
                total_exec_count=total_exec_count,
                llm_calls=llm_calls,
                start_time=start_time,
            )

        session = RefinementSession(
            llm_client=self.llm_client,
            context=RefinementContext(case=case, branch_id="REFINER"),
            base_sql=initial_sql,
        )

        current_sql = initial_sql
        current_exec = pred_exec
        current_mismatch = initial_mismatch
        global_decision = GlobalDecision(global_status="RUNNING")

        effective_K = int(case.settings.K_per_branch)
        if self.config.rulebook.adaptive_budget:
            # Optional legacy heuristic: hard structural cases often need more turns.
            if signals_summary.join_edges_diff:
                effective_K = max(effective_K, 18)
            if "schema_diff" in (initial_mismatch.warnings or []):
                effective_K = max(effective_K, 12)
        effective_K = min(effective_K, 30)

        # Multi-candidate settings
        n_candidates = min(3, int(case.settings.max_candidates_per_branch_step or 3))
        if n_candidates < 1:
            n_candidates = 1

        best_score_so_far = _score_mismatch(current_mismatch, weights=self.config.rulebook.ranking_weights)
        stagnation = 0

        # Keep track of the previous turn's candidate column checks so that
        # the refiner can explicitly see UNKNOWN_COLUMN-style violations and
        # avoid repeating them.
        prev_candidate_checks: Optional[List[Dict[str, Any]]] = None
        visited_sql_norms = {_normalize_sql(initial_sql)}
        last_plan_fingerprint = ""
        repeat_plan_count = 0
        last_outcome_summary: Dict[str, Any] = {
            "mismatch_type": initial_mismatch.mismatch_type,
            "distance": initial_mismatch.distance,
            "exec_brief": _execution_brief(pred_exec),
        }

        for t in range(1, effective_K + 1):
            try:
                refinement_step, _ = session.step(
                    mismatch=current_mismatch,
                    signals_summary=signals_summary,
                    signals_details=signals_details,
                    extra_context={
                        "t": t,
                        "n_candidates": n_candidates,
                        "prev_candidate_checks": (prev_candidate_checks or [])[:5],
                        "current_exec_brief": _execution_brief(current_exec),
                        "gold_exec_brief": _execution_brief(gold_exec),
                        "last_outcome_summary": last_outcome_summary,
                        "last_plan_fingerprint": last_plan_fingerprint,
                        "repeat_plan_count": repeat_plan_count,
                    },
                )
                llm_calls += 1
            except Exception:
                global_decision = GlobalDecision(
                    global_status="STUCK",
                    stop_reason="REFINER_ERROR",
                )
                refiner_trace.final_status = "DEAD"
                break

            plan_fingerprint = _normalize_text(refinement_step.edit_plan)
            if plan_fingerprint and plan_fingerprint == last_plan_fingerprint:
                repeat_plan_count += 1
            else:
                repeat_plan_count = 0
            last_plan_fingerprint = plan_fingerprint

            # Candidate pool: prefer exactly one minimal SQL per turn. We still
            # accept optional fallback candidates for compatibility, but the
            # prompt now asks the model for a single proposal.
            raw_pool: List[str] = []
            primary = (refinement_step.sql or "").strip() if refinement_step.sql is not None else ""
            if primary:
                raw_pool.append(primary)
            if isinstance(getattr(refinement_step, "candidates", None), list):
                for item in refinement_step.candidates:
                    if isinstance(item, str) and item.strip():
                        raw_pool.append(item.strip())
            if not raw_pool:
                raw_pool = [current_sql]

            # Dedup
            seen: set = set()
            pool: List[str] = []
            for s in raw_pool:
                key = _normalize_sql(s)
                if key in seen:
                    continue
                seen.add(key)
                pool.append(s)

            # Column-level compliance check (skip invalid candidates before execution)
            checks = []
            valid_pool: List[str] = []
            for s in pool:
                norm_sql = _normalize_sql(s)
                report = validate_sql_columns(s, schema_tables=case.schema.tables)
                minimality_violation = _is_minimality_violation(s, current_sql, gold_sql)
                repeated_sql = norm_sql in visited_sql_norms
                checks.append(
                    {
                        "sql": s,
                        "valid": report.valid and not minimality_violation and not repeated_sql,
                        "minimality_violation": minimality_violation,
                        "repeated_sql": repeated_sql,
                        "violations": report.violations[:10],
                    }
                )
                if report.valid and not minimality_violation and not repeated_sql:
                    valid_pool.append(s)

            # Cache the checks for the next refiner step so that the LLM can
            # see detailed schema violations (including UNKNOWN_COLUMN) and
            # explicitly correct them.
            prev_candidate_checks = checks

            if not valid_pool:
                # Fall back to executing the primary (or current) SQL to preserve behavior.
                valid_pool = [primary or current_sql]

            evaluated: List[Dict[str, Any]] = []
            best = None
            best_sql = None
            best_mismatch = None
            best_score = float("-inf")
            best_exec_brief: Dict[str, Any] = {}
            best_exec_result = None
            best_sort_key = None

            for idx, cand_sql in enumerate(valid_pool[: int(case.settings.max_candidates_per_branch_step or 12)]):
                pred_exec_new = execute_sql(db_path, cand_sql, timeout=case.settings.timeout_sec)
                mismatch_after = compare_results(pred_exec_new, gold_exec)
                total_exec_count += 1
                score = _score_mismatch(mismatch_after, weights=self.config.rulebook.ranking_weights)

                # If join_edges_diff is flagged at the case level, gently
                # prefer candidates that actually change the join structure
                # relative to the current SQL, and penalize candidates that
                # keep using tables not present in the gold-side AST.
                if signals_summary.join_edges_diff:
                    try:
                        ast_current = parse_ast_signature(current_sql)
                        ast_cand = parse_ast_signature(cand_sql)
                        joins_changed = ast_current.join_edges != ast_cand.join_edges
                    except Exception:
                        joins_changed = False

                    # Use gold-side AST tables from signals_details as a soft
                    # target for the intended join graph.
                    gold_tables: List[str] = []
                    try:
                        gold_ast = signals_details.gold_ast if signals_details is not None else {}
                        if isinstance(gold_ast, dict):
                            gold_tables = [str(t) for t in gold_ast.get("tables_involved", [])]
                    except Exception:
                        gold_tables = []
                    gold_table_set = set(gold_tables)

                    # Re-parse candidate tables if possible.
                    cand_tables: List[str] = []
                    try:
                        cand_tables = list(ast_cand.tables_involved)
                    except Exception:
                        cand_tables = []
                    cand_table_set = {str(t) for t in cand_tables}

                    # Bonus for actually changing joins when we know join_edges_diff=True.
                    if joins_changed:
                        score *= 1.1

                    # Mild penalty for tables that only appear on the pred side
                    # but not in the gold-side AST (e.g., over-using bridging tables).
                    if gold_table_set:
                        extra_tables = cand_table_set - gold_table_set
                        if extra_tables:
                            score *= 0.95
                current_similarity = _sql_similarity(cand_sql, current_sql)
                gold_similarity = _sql_similarity(cand_sql, gold_sql)
                try:
                    ast_current = parse_ast_signature(current_sql)
                    ast_cand = parse_ast_signature(cand_sql)
                    ast_change_count = 0 if ast_current.join_edges == ast_cand.join_edges else 1
                except Exception:
                    ast_change_count = 0
                verdict = "CORRECT" if mismatch_after.mismatch_type == "correct" else "WRONG"
                item = {
                    "candidate_id": f"REFINER_{t}_{idx}",
                    "sql": cand_sql,
                    "verdict": verdict,
                    "score": score,
                    "current_similarity": current_similarity,
                    "gold_similarity": gold_similarity,
                    "mismatch": mismatch_after.dict(),
                    "pred_exec_brief": {
                        "result_type": pred_exec_new.result_type,
                        "row_count": pred_exec_new.row_count,
                        "columns": pred_exec_new.columns,
                        "scalar_value": pred_exec_new.scalar_value,
                        "error": pred_exec_new.error,
                    },
                }
                evaluated.append(item)
                sort_key = (
                    round(score, 8),
                    1 if mismatch_after.mismatch_type == "correct" else 0,
                    round(current_similarity, 8),
                    -round(gold_similarity, 8),
                    ast_change_count,
                    -idx,
                )
                if best_sort_key is None or sort_key > best_sort_key:
                    best_sort_key = sort_key
                    best_score = score
                    best_sql = cand_sql
                    best_mismatch = mismatch_after
                    best_exec_brief = item["pred_exec_brief"]
                    best_exec_result = pred_exec_new
                    best = {
                        "candidate_id": item["candidate_id"],
                        "sql": item["sql"],
                        "verdict": item["verdict"],
                        "score": item["score"],
                        "mismatch": item["mismatch"],
                    }

            # Safety: if something went wrong, keep current.
            if best_sql is None or best_mismatch is None:
                best_sql = current_sql
                best_mismatch = current_mismatch
                best_score = best_score_so_far
                best_exec_result = current_exec
                best_exec_brief = _execution_brief(current_exec)
                best = {
                    "candidate_id": f"REFINER_{t}_fallback",
                    "sql": best_sql,
                    "verdict": "WRONG",
                    "score": best_score,
                    "mismatch": best_mismatch.dict(),
                }

            mismatch_after = best_mismatch

            distance_delta = None
            key_signal_changes: Dict[str, Any] = {
                "refinement": {
                    "observed_diffs": refinement_step.observed_diffs,
                    "edit_plan": refinement_step.edit_plan,
                    "predicted_secondary_diffs": refinement_step.predicted_secondary_diffs,
                    "followup_strategy": refinement_step.followup_strategy,
                    "plan_fingerprint": plan_fingerprint,
                    "repeat_plan_count": repeat_plan_count,
                    "sql_suggested": refinement_step.sql,
                    "n_candidates_returned": len(getattr(refinement_step, "candidates", []) or []),
                    "match_ok": refinement_step.match_ok,
                    "no_diff": refinement_step.no_diff,
                }
            }
            key_signal_changes["candidate_checks"] = checks
            key_signal_changes["best_pred_exec_brief"] = best_exec_brief
            before_ratio = current_mismatch.distance.get("row_count_ratio")
            after_ratio = mismatch_after.distance.get("row_count_ratio")
            if before_ratio is not None and after_ratio is not None:
                distance_delta = after_ratio - before_ratio
                key_signal_changes["row_count_ratio_before"] = before_ratio
                key_signal_changes["row_count_ratio_after"] = after_ratio
            refiner_trace.steps.append(
                BranchStepRecord(
                    t=t,
                    base_sql=current_sql,
                    mismatch_before=current_mismatch.dict(),
                    signals_before=signals_summary.dict() if t == 1 else {},
                    candidate_pool_size=len(raw_pool),
                    after_dedup_size=len(pool),
                    after_checker_valid=len([c for c in checks if c.get("valid")]),
                    evaluated=evaluated,
                    best=best,
                    mismatch_after=mismatch_after.dict(),
                    distance_delta=distance_delta,
                    key_signal_changes=key_signal_changes,
                )
            )

            if mismatch_after.mismatch_type == "correct":
                global_decision = GlobalDecision(
                    global_status="FIXED",
                    winner_branch_id="REFINER",
                    winner_sql=best_sql,
                    stop_reason="REFINER_CORRECT",
                )
                refiner_trace.final_status = "DONE"
                refiner_trace.final_sql = best_sql
                break

            # Track stagnation based on score improvement.
            if best_score <= best_score_so_far + 1e-6:
                stagnation += 1
            else:
                best_score_so_far = best_score
                stagnation = 0

            # If model claims no-diff and we didn't improve for a few turns, stop.
            if refinement_step.no_diff and stagnation >= 2:
                global_decision = GlobalDecision(
                    global_status="STUCK",
                    stop_reason="REFINER_NO_DIFF",
                )
                refiner_trace.final_status = "DEAD"
                break

            # If we stagnate for several turns, allow a small adaptive extension (up to cap) by increasing n_candidates.
            if stagnation >= 3 and n_candidates < 6:
                n_candidates = min(6, n_candidates + 1)

            if t >= effective_K:
                global_decision = GlobalDecision(
                    global_status="STUCK",
                    stop_reason="REFINER_BUDGET_EXHAUSTED",
                )
                refiner_trace.final_status = "DEAD"
                break

            current_sql = best_sql
            current_exec = best_exec_result or current_exec
            current_mismatch = mismatch_after
            visited_sql_norms.add(_normalize_sql(current_sql))
            last_outcome_summary = {
                "mismatch_type": mismatch_after.mismatch_type,
                "distance": mismatch_after.distance,
                "exec_brief": best_exec_brief,
            }

        refiner_trace.refinement_trace = session.trace
        return self._build_case_trace(
            case=case,
            initial_mismatch=initial_mismatch,
            signals_summary=signals_summary,
            signals_details=signals_details,
            fingerprints=fingerprints,
            refiner_trace=refiner_trace,
            global_decision=global_decision,
            total_exec_count=total_exec_count,
            llm_calls=llm_calls,
            start_time=start_time,
        )

    def _build_case_trace(
        self,
        case: CaseInput,
        initial_mismatch: Any,
        signals_summary: Any,
        signals_details: Any,
        fingerprints: Any,
        refiner_trace: BranchTrace,
        global_decision: GlobalDecision,
        total_exec_count: int,
        llm_calls: int,
        start_time: float,
    ) -> CaseTrace:
        return CaseTrace(
            case_id=case.meta.case_id,
            db_id=case.meta.db_id,
            question=case.nl.question,
            initial_pred_sql=case.sql.pred_sql,
            gold_sql=case.sql.gold_sql,
            initial_mismatch=initial_mismatch.dict(),
            initial_signals_summary=signals_summary.dict(),
            initial_signals_details=signals_details.dict(),
            ast_fp_pred=fingerprints.dict(),
            planner_source="REFINER_ONLY",
            branches={"REFINER": refiner_trace},
            global_result=global_decision,
            stats={
                "total_exec_count": total_exec_count,
                "total_llm_calls": llm_calls,
                "wall_time_sec": round(time.time() - start_time, 4),
            },
        )

    def _write_traces(self, results: List[CaseTrace], path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for item in results:
                f.write(json.dumps(item.dict(), ensure_ascii=False) + "\n")
        if path.suffix == ".jsonl":
            self._write_traces_pretty(results, path.with_suffix(".pretty.json"))

    def _write_traces_pretty(self, results: List[CaseTrace], path: Path) -> None:
        data = [item.dict() for item in results]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _write_summary(self, results: List[CaseTrace], path: Path) -> None:
        total = len(results)
        fixed = sum(1 for item in results if item.global_result.global_status == "FIXED")
        stuck = sum(1 for item in results if item.global_result.global_status == "STUCK")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "total": total,
                    "fixed": fixed,
                    "stuck": stuck,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
