#!/usr/bin/env python3
"""Run a small multi-database v2 validation suite from a manifest.

The suite intentionally separates cheap answer-blind trigger replay from the
more expensive compiler/rewrite replay. Each subprocess writes its stdout and
stderr to a log file under the validation output directory.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "doc" / "multidb_quick_validation_manifest.json"
DEFAULT_BIRD_DB_ROOT = "/data/liuyining/ace4sql/bench/bird/dev/dev_databases"
DEFAULT_DEEPEYE_ROOT = "/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL"
DEFAULT_ACE_ROOT = "/data/liuyining/ace4sql"


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")


def _case_ids_arg(case_ids: Sequence[Any]) -> str:
    return ",".join(str(case_id) for case_id in case_ids)


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")


def _read_summary(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    payload = _load_json(path)
    if isinstance(payload, dict):
        return dict(payload.get("summary") or {})
    return {}


def _db_paths(output_dir: Path, db_id: str) -> Dict[str, Path]:
    db_dir = output_dir / db_id
    return {
        "db_dir": db_dir,
        "trigger_json": db_dir / "trigger.json",
        "rewrite_json": db_dir / "rewrite.json",
        "rewrite_cases_dir": db_dir / "rewrite_cases",
        "logs_dir": output_dir / "logs",
    }


def _selected_case_ids(trigger_json: Path) -> List[int]:
    if not trigger_json.exists():
        return []
    payload = _load_json(trigger_json)
    rows = payload.get("cases") if isinstance(payload, dict) else []
    selected: List[int] = []
    for row in rows or []:
        if row.get("selected_group_ids"):
            selected.append(int(row["question_id"]))
    return selected


def _run_tasks_parallel(
    tasks: Sequence[tuple[int, str, Sequence[str], Path]],
    *,
    jobs: int,
    dry_run: bool,
) -> Dict[int, int]:
    if not tasks:
        return {}
    max_workers = max(1, min(jobs, len(tasks)))
    if max_workers == 1:
        return {
            index: _run_command(cmd, log_path, dry_run)
            for index, _label, cmd, log_path in tasks
        }

    results: Dict[int, int] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_task = {
            executor.submit(_run_command, cmd, log_path, dry_run): (index, label, log_path)
            for index, label, cmd, log_path in tasks
        }
        for future in concurrent.futures.as_completed(future_to_task):
            index, label, log_path = future_to_task[future]
            try:
                results[index] = int(future.result())
            except Exception as exc:  # pragma: no cover - defensive wrapper for subprocess orchestration.
                results[index] = 1
                with log_path.open("a", encoding="utf-8") as log_file:
                    log_file.write(f"\nrunner_exception[{label}]={exc!r}\n")
    return results


def _run_command(cmd: Sequence[str], log_path: Path, dry_run: bool) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = " ".join(cmd)
    with log_path.open("w", encoding="utf-8") as log_file:
        log_file.write(f"$ {rendered}\n")
        log_file.write(f"started_utc={dt.datetime.now(dt.timezone.utc).isoformat()}\n")
        log_file.flush()
        if dry_run:
            log_file.write("dry_run=true\n")
            return 0
        proc = subprocess.run(
            list(cmd),
            cwd=str(REPO_ROOT),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        log_file.write(f"finished_utc={dt.datetime.now(dt.timezone.utc).isoformat()}\n")
        log_file.write(f"returncode={proc.returncode}\n")
        return int(proc.returncode)


def _base_args(manifest: Dict[str, Any]) -> List[str]:
    return [
        "--bird_db_root",
        str(manifest.get("bird_db_root") or DEFAULT_BIRD_DB_ROOT),
        "--deepeye_root",
        str(manifest.get("deepeye_root") or DEFAULT_DEEPEYE_ROOT),
        "--ace_root",
        str(manifest.get("ace_root") or DEFAULT_ACE_ROOT),
    ]


def _trigger_cmd(
    *,
    manifest: Dict[str, Any],
    db_cfg: Dict[str, Any],
    output_json: Path,
    augment_memory_contracts: bool,
    allow_self_singleton_replay: bool,
) -> List[str]:
    cmd = [
        sys.executable,
        str(REPO_ROOT / "cli" / "replay_runtime_trigger_v2.py"),
        "--library_json",
        str(db_cfg["library_json"]),
        "--work_root",
        str(db_cfg["work_root"]),
        "--output_json",
        str(output_json),
        "--max_selected",
        str(db_cfg.get("max_selected", manifest.get("max_selected", 1))),
        "--case_ids",
        _case_ids_arg(db_cfg.get("case_ids") or []),
        *_base_args(manifest),
    ]
    if augment_memory_contracts:
        cmd.append("--augment_memory_contracts")
    if allow_self_singleton_replay or bool(db_cfg.get("allow_self_singleton_replay")):
        cmd.append("--allow_self_singleton_replay")
    return cmd


def _rewrite_cmd(
    *,
    manifest: Dict[str, Any],
    db_cfg: Dict[str, Any],
    output_json: Path,
    output_dir: Path,
    case_ids: Sequence[Any],
    augment_memory_contracts: bool,
    allow_self_singleton_replay: bool,
) -> List[str]:
    cmd = [
        sys.executable,
        str(REPO_ROOT / "cli" / "replay_runtime_rewrite_v2.py"),
        "--library_json",
        str(db_cfg["library_json"]),
        "--work_root",
        str(db_cfg["work_root"]),
        "--output_json",
        str(output_json),
        "--output_dir",
        str(output_dir),
        "--case_ids",
        _case_ids_arg(case_ids),
        *_base_args(manifest),
    ]
    if augment_memory_contracts:
        cmd.append("--augment_memory_contracts")
    if allow_self_singleton_replay or bool(db_cfg.get("allow_self_singleton_replay")):
        cmd.append("--allow_self_singleton_replay")
    if bool(db_cfg.get("rewrite_all_candidates", False)):
        cmd.append("--rewrite_all_candidates")
    row_sample_limit = db_cfg.get("row_sample_limit", manifest.get("row_sample_limit"))
    if row_sample_limit:
        cmd.extend(["--row_sample_limit", str(row_sample_limit)])
    return cmd


def _totals(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    improved: Dict[str, List[int]] = {}
    regressions: Dict[str, List[int]] = {}
    for row in rows:
        db_id = row["db_id"]
        rewrite_summary = row.get("rewrite_summary") or {}
        improved[db_id] = list(rewrite_summary.get("improved_cases") or [])
        regressions[db_id] = list(rewrite_summary.get("regression_cases") or [])
    return {
        "databases": len(rows),
        "manifest_cases": sum(len(row.get("case_ids") or []) for row in rows),
        "trigger_cases": sum(int((row.get("trigger_summary") or {}).get("total_cases") or 0) for row in rows),
        "trigger_selected_cases": sum(
            int((row.get("trigger_summary") or {}).get("selected_cases") or 0) for row in rows
        ),
        "rewrite_cases": sum(int((row.get("rewrite_summary") or {}).get("total_cases") or 0) for row in rows),
        "ready_cases": sum(int((row.get("rewrite_summary") or {}).get("ready_cases") or 0) for row in rows),
        "rewritten_cases": sum(
            int((row.get("rewrite_summary") or {}).get("rewritten_cases") or 0) for row in rows
        ),
        "improved_cases": improved,
        "regression_cases": regressions,
    }


def _as_str_set(values: Sequence[Any]) -> set[str]:
    return {str(value) for value in values}


def _rewrite_cases_by_id(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    rows = payload.get("cases") if isinstance(payload, dict) else []
    return {str(row.get("question_id")): row for row in rows or [] if row.get("question_id") is not None}


def _has_contract_enforcement(compiler_output: Dict[str, Any]) -> bool:
    diagnostics = compiler_output.get("schema_diagnostics") or {}
    notes = str(diagnostics.get("notes") or "")
    return "action_count_contract_enforced" in notes


def _payload(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(getattr(value, "__dict__", {}) or {})


def _canonical_payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _repair_step_key(step: Dict[str, Any]) -> tuple[str, str, str, bool, bool, str, str, str]:
    return (
        str(step.get("step_id") or ""),
        str(step.get("op") or ""),
        str(step.get("locus") or ""),
        bool(step.get("is_dependency") or False),
        bool(step.get("required", True)),
        _canonical_payload(step.get("slots") or []),
        _canonical_payload(step.get("guards") or []),
        _canonical_payload(step.get("arguments") or {}),
    )


def _contract_repair_steps_for_memory(
    memory: Dict[str, Any],
) -> List[Dict[str, Any]]:
    contract = _payload(memory.get("trigger_contract") or {})
    action_contract = _payload(contract.get("action_contract") or {})
    steps = action_contract.get("repair_program") or []
    return [_payload(step) for step in steps if _payload(step)]


def _contract_step_keys_by_group(
    case_row: Dict[str, Any],
) -> Dict[str, set[tuple[str, str, str, bool, bool, str, str, str]]]:
    out: Dict[str, set[tuple[str, str, str, bool, bool, str, str, str]]] = {}
    for memory in case_row.get("matched_memory_objects") or []:
        group_id = str(memory.get("group_id") or "")
        steps = _contract_repair_steps_for_memory(memory)
        out[group_id] = {
            _repair_step_key(step)
            for step in steps
        }
    return out


def _rewrite_gate_issues_for_case(
    *,
    db_id: str,
    case_id: str,
    case_row: Dict[str, Any],
    expected_pattern_ids: set[str],
    expected_memory_types: set[str],
    require_contract_repair_program: bool,
    require_llm_explicit_repair_program: bool,
) -> List[str]:
    issues: List[str] = []
    prefix = f"{db_id}:{case_id}"

    if case_row.get("plan_reason") != "ready":
        issues.append(f"{prefix}: plan_reason={case_row.get('plan_reason')!r}")

    matched_ids = _as_str_set(case_row.get("matched_group_ids") or [])
    if expected_pattern_ids and matched_ids != expected_pattern_ids:
        issues.append(
            f"{prefix}: matched_group_ids={sorted(matched_ids)} expected={sorted(expected_pattern_ids)}"
        )

    for memory in case_row.get("matched_memory_objects") or []:
        group_type = str(memory.get("group_type") or "")
        if expected_memory_types and group_type not in expected_memory_types:
            issues.append(f"{prefix}: unexpected_memory_type={memory.get('group_type')!r}")
        memory_repair_steps = _contract_repair_steps_for_memory(
            memory
        )
        if not memory_repair_steps:
            issues.append(
                f"{prefix}: memory_missing_contract_repair_program={memory.get('group_id')!r}"
            )
        for step_payload in memory_repair_steps:
            origin = str(step_payload.get("origin") or "")
            if origin not in {"case_extracted", "group_merged"}:
                issues.append(
                    f"{prefix}: invalid_memory_repair_step_origin="
                    f"{memory.get('group_id')!r}:{origin!r}"
                )
            extraction_source = str(step_payload.get("extraction_source") or "")
            if require_llm_explicit_repair_program and extraction_source not in {"llm_explicit", "group_merged"}:
                issues.append(
                    f"{prefix}: non_explicit_memory_repair_step="
                    f"{memory.get('group_id')!r}:{extraction_source!r}"
                )

    compiler_output = case_row.get("compiler_output") or {}
    actions = compiler_output.get("actions") or []
    contract_step_keys = _contract_step_keys_by_group(case_row)
    if not actions:
        issues.append(f"{prefix}: compiler_no_actions")
    if compiler_output.get("escape_hatch_log"):
        issues.append(f"{prefix}: compiler_escape_hatch_log_present")
    if _has_contract_enforcement(compiler_output):
        issues.append(f"{prefix}: action_count_contract_enforced")
    for action in actions:
        if action.get("used_escape_hatch"):
            issues.append(f"{prefix}: action_used_escape_hatch={action.get('action_id')!r}")
        if not action.get("selected_candidate_id"):
            issues.append(f"{prefix}: action_missing_selected_candidate_id={action.get('action_id')!r}")
        arguments = action.get("arguments") or {}
        if arguments.get("dependency_repairs"):
            issues.append(f"{prefix}: legacy_dependency_repairs_present={action.get('action_id')!r}")
        repair_steps = arguments.get("repair_program") or []
        if not repair_steps:
            issues.append(f"{prefix}: action_missing_repair_program={action.get('action_id')!r}")
        source_group_id = str(action.get("source_group_id") or "")
        allowed_keys = contract_step_keys.get(source_group_id, set())
        for step in repair_steps:
            step_payload = _payload(step)
            key = _repair_step_key(step_payload)
            if key not in allowed_keys:
                issues.append(
                    f"{prefix}: uncontracted_repair_step={action.get('action_id')!r}:{key}"
                )
            origin = str(step_payload.get("origin") or "")
            allowed_origins = {"case_extracted", "group_merged"}
            if origin not in allowed_origins:
                issues.append(
                    f"{prefix}: invalid_repair_step_origin={action.get('action_id')!r}:{origin!r}"
                )
            extraction_source = str(step_payload.get("extraction_source") or "")
            if (
                require_llm_explicit_repair_program
                and extraction_source not in {"llm_explicit", "group_merged"}
            ):
                issues.append(
                    f"{prefix}: non_explicit_action_repair_step="
                    f"{action.get('action_id')!r}:{extraction_source!r}"
                )
    rewrites = case_row.get("rewrites") or []
    if not rewrites:
        issues.append(f"{prefix}: no_rewrite_rows")
    for rewrite in rewrites:
        candidate_index = rewrite.get("candidate_index")
        rewrite_prefix = f"{prefix}:candidate_{candidate_index}"
        if not rewrite.get("rewrite_sql"):
            issues.append(f"{rewrite_prefix}: rewrite_sql_empty")
        if rewrite.get("rewrite_status") in {"exception", "error", "gold_error"}:
            issues.append(f"{rewrite_prefix}: rewrite_status={rewrite.get('rewrite_status')!r}")
        traces = rewrite.get("action_realization_traces") or []
        if actions and len(traces) < len(actions):
            issues.append(
                f"{rewrite_prefix}: action_trace_count={len(traces)} action_count={len(actions)}"
            )
        for trace in traces:
            if not trace.get("realized"):
                issues.append(f"{rewrite_prefix}: action_not_realized={trace.get('action_id')!r}")
            if trace.get("scope_violation"):
                issues.append(f"{rewrite_prefix}: action_scope_violation={trace.get('action_id')!r}")
            action_by_id = {str(action.get("action_id") or ""): action for action in actions}
            source_action = action_by_id.get(str(trace.get("action_id") or ""))
            allowed_scopes = {
                str(scope)
                for scope in ((source_action or {}).get("allowed_edit_scope") or [])
            }
            for edit in trace.get("edits") or []:
                location = str(edit.get("location") or "")
                if allowed_scopes and location and location not in allowed_scopes:
                    issues.append(
                        f"{rewrite_prefix}: edit_outside_allowed_scope="
                        f"{trace.get('action_id')!r}:{location}"
                    )
        if rewrite.get("dependency_repairs_applied"):
            issues.append(f"{rewrite_prefix}: legacy_dependency_repairs_applied")

    pred_status = str(case_row.get("pred_status") or "")
    c0_status = str(case_row.get("c0_status") or pred_status)
    best_rewrite_status = str(case_row.get("best_rewrite_status") or "")
    c1_status = str(case_row.get("c1_status") or best_rewrite_status)
    if c0_status == "equivalent" and c1_status != "equivalent":
        issues.append(f"{prefix}: regression c0=equivalent c1={c1_status!r}")
    if c0_status != "equivalent" and c1_status != "equivalent":
        issues.append(f"{prefix}: not_improved c0={c0_status!r} c1={c1_status!r}")

    return issues


def _audit_strict_rewrite_gates(
    *,
    manifest: Dict[str, Any],
    rows: Sequence[Dict[str, Any]],
    output_dir: Path,
    require_contract_repair_program: bool,
    require_llm_explicit_repair_program: bool,
) -> Dict[str, Any]:
    issues: List[str] = []
    by_db = {row["db_id"]: row for row in rows}
    for db_cfg in manifest.get("databases") or []:
        db_id = str(db_cfg["db_id"])
        rewrite_path = _db_paths(output_dir, db_id)["rewrite_json"]
        if not rewrite_path.exists():
            issues.append(f"{db_id}: missing_rewrite_json={rewrite_path}")
            continue
        payload = _load_json(rewrite_path)
        cases_by_id = _rewrite_cases_by_id(payload if isinstance(payload, dict) else {})
        expected_cases = [str(case_id) for case_id in (db_cfg.get("rewrite_case_ids") or db_cfg.get("case_ids") or [])]
        expected_patterns = _as_str_set(db_cfg.get("expected_pattern_ids") or [])
        expected_memory_types = _as_str_set(
            db_cfg.get("expected_memory_types")
            or manifest.get("expected_memory_types")
            or ["pattern"]
        )
        db_requires_contract_repair = True
        db_requires_llm_explicit_repair = bool(
            require_llm_explicit_repair_program
            or manifest.get("require_llm_explicit_repair_program")
            or db_cfg.get("require_llm_explicit_repair_program")
        )
        row = by_db.get(db_id) or {}
        if row.get("rewrite_returncode", 0):
            issues.append(f"{db_id}: rewrite_returncode={row.get('rewrite_returncode')}")
        for case_id in expected_cases:
            case_row = cases_by_id.get(case_id)
            if case_row is None:
                issues.append(f"{db_id}:{case_id}: missing_case_row")
                continue
            issues.extend(
                _rewrite_gate_issues_for_case(
                    db_id=db_id,
                    case_id=case_id,
                    case_row=case_row,
                    expected_pattern_ids=expected_patterns,
                    expected_memory_types=expected_memory_types,
                    require_contract_repair_program=db_requires_contract_repair,
                    require_llm_explicit_repair_program=db_requires_llm_explicit_repair,
                )
            )
    return {
        "passed": not issues,
        "issues": issues,
        "issue_count": len(issues),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--output_dir", default="")
    parser.add_argument("--stage", choices=("trigger", "rewrite", "all"), default="trigger")
    parser.add_argument(
        "--jobs",
        type=int,
        default=0,
        help="Number of databases to run in parallel. Defaults to all databases, capped at 4.",
    )
    parser.add_argument(
        "--rewrite_scope",
        choices=("manifest", "triggered"),
        default="triggered",
        help="Use manifest rewrite_case_ids/case_ids, or only cases selected by trigger replay.",
    )
    parser.add_argument("--augment_memory_contracts", action="store_true")
    parser.add_argument(
        "--allow_self_singleton_replay",
        action="store_true",
        help="Validation-only: pass through to replay CLIs so singleton source cases can self-replay.",
    )
    parser.add_argument("--dry_run", action="store_true")
    parser.add_argument(
        "--strict_rewrite_gates",
        action="store_true",
        help="Fail the run unless per-case rewrite acceptance gates pass.",
    )
    parser.add_argument(
        "--require_contract_repair_program",
        action="store_true",
        help=(
            "Deprecated compatibility flag. Strict rewrite gates always require "
            "case-extracted or group-merged repair_program steps."
        ),
    )
    parser.add_argument(
        "--require_llm_explicit_repair_program",
        action="store_true",
        help=(
            "With --strict_rewrite_gates, reject repair_program steps produced by "
            "pipeline fallbacks; only llm_explicit or group_merged steps are allowed."
        ),
    )
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest).resolve()
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError("--manifest must contain a JSON object")
    if bool(manifest.get("augment_memory_contracts_allowed")) is False and args.augment_memory_contracts:
        raise ValueError("manifest disallows --augment_memory_contracts for this validation")
    if args.strict_rewrite_gates and args.augment_memory_contracts:
        raise ValueError("--strict_rewrite_gates cannot be combined with --augment_memory_contracts")

    output_dir = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else REPO_ROOT / "outputs" / f"multidb_quick_validation_{_timestamp()}"
    )
    db_configs = list(manifest.get("databases") or [])
    jobs = args.jobs if args.jobs > 0 else min(4, max(1, len(db_configs)))
    rows: List[Dict[str, Any]] = []
    for db_cfg in db_configs:
        db_id = str(db_cfg["db_id"])
        paths = _db_paths(output_dir, db_id)
        trigger_json = paths["trigger_json"]
        rewrite_json = paths["rewrite_json"]
        row: Dict[str, Any] = {
            "db_id": db_id,
            "case_ids": list(db_cfg.get("case_ids") or []),
            "description": db_cfg.get("description", ""),
            "trigger_json": str(trigger_json),
            "rewrite_json": str(rewrite_json),
            "library_json": str(db_cfg.get("library_json", "")),
            "work_root": str(db_cfg.get("work_root", "")),
        }
        row["trigger_summary"] = _read_summary(trigger_json)
        row["rewrite_summary"] = _read_summary(rewrite_json)
        rows.append(row)

    if args.stage in {"trigger", "all"}:
        trigger_tasks: List[tuple[int, str, Sequence[str], Path]] = []
        for index, db_cfg in enumerate(db_configs):
            db_id = str(db_cfg["db_id"])
            paths = _db_paths(output_dir, db_id)
            cmd = _trigger_cmd(
                manifest=manifest,
                db_cfg=db_cfg,
                output_json=paths["trigger_json"],
                augment_memory_contracts=args.augment_memory_contracts,
                allow_self_singleton_replay=bool(
                    args.allow_self_singleton_replay
                    or manifest.get("allow_self_singleton_replay")
                ),
            )
            trigger_tasks.append(
                (index, f"{db_id}.trigger", cmd, paths["logs_dir"] / f"{db_id}.trigger.log")
            )
        trigger_results = _run_tasks_parallel(trigger_tasks, jobs=jobs, dry_run=args.dry_run)
        for index, returncode in trigger_results.items():
            db_id = rows[index]["db_id"]
            rows[index]["trigger_returncode"] = returncode
            rows[index]["trigger_summary"] = _read_summary(_db_paths(output_dir, db_id)["trigger_json"])
        _dump_json(
            output_dir / "summary.json",
            {
                "manifest": str(manifest_path),
                "output_dir": str(output_dir),
                "runner": {
                    "stage": args.stage,
                    "rewrite_scope": args.rewrite_scope,
                    "jobs": jobs,
                    "augment_memory_contracts": bool(args.augment_memory_contracts),
                    "allow_self_singleton_replay": bool(args.allow_self_singleton_replay),
                    "dry_run": bool(args.dry_run),
                },
                "databases": rows,
                "totals": _totals(rows),
            },
        )

    if args.stage in {"rewrite", "all"}:
        rewrite_tasks: List[tuple[int, str, Sequence[str], Path]] = []
        for index, db_cfg in enumerate(db_configs):
            db_id = str(db_cfg["db_id"])
            paths = _db_paths(output_dir, db_id)
            if args.rewrite_scope == "triggered":
                rewrite_case_ids = _selected_case_ids(paths["trigger_json"])
            else:
                rewrite_case_ids = list(db_cfg.get("rewrite_case_ids") or db_cfg.get("case_ids") or [])
            rows[index]["rewrite_case_ids"] = rewrite_case_ids
            if rewrite_case_ids:
                cmd = _rewrite_cmd(
                    manifest=manifest,
                    db_cfg=db_cfg,
                    output_json=paths["rewrite_json"],
                    output_dir=paths["rewrite_cases_dir"],
                    case_ids=rewrite_case_ids,
                    augment_memory_contracts=args.augment_memory_contracts,
                    allow_self_singleton_replay=bool(
                        args.allow_self_singleton_replay
                        or manifest.get("allow_self_singleton_replay")
                    ),
                )
                rewrite_tasks.append(
                    (index, f"{db_id}.rewrite", cmd, paths["logs_dir"] / f"{db_id}.rewrite.log")
                )
            else:
                rows[index]["rewrite_returncode"] = 0
                rows[index]["rewrite_skipped_reason"] = "no_rewrite_case_ids"
        rewrite_results = _run_tasks_parallel(rewrite_tasks, jobs=jobs, dry_run=args.dry_run)
        for index, returncode in rewrite_results.items():
            db_id = rows[index]["db_id"]
            rows[index]["rewrite_returncode"] = returncode
        for index, row in enumerate(rows):
            db_id = row["db_id"]
            row["rewrite_summary"] = _read_summary(_db_paths(output_dir, db_id)["rewrite_json"])

    summary = {
        "manifest": str(manifest_path),
        "output_dir": str(output_dir),
        "runner": {
            "stage": args.stage,
            "rewrite_scope": args.rewrite_scope,
            "jobs": jobs,
            "augment_memory_contracts": bool(args.augment_memory_contracts),
            "allow_self_singleton_replay": bool(args.allow_self_singleton_replay),
            "dry_run": bool(args.dry_run),
            "strict_rewrite_gates": bool(args.strict_rewrite_gates),
            "require_contract_repair_program": bool(args.require_contract_repair_program),
            "require_llm_explicit_repair_program": bool(args.require_llm_explicit_repair_program),
        },
        "databases": rows,
        "totals": _totals(rows),
    }
    if args.strict_rewrite_gates and args.stage in {"rewrite", "all"} and not args.dry_run:
        summary["strict_rewrite_gate"] = _audit_strict_rewrite_gates(
            manifest=manifest,
            rows=rows,
            output_dir=output_dir,
            require_contract_repair_program=bool(args.require_contract_repair_program),
            require_llm_explicit_repair_program=bool(args.require_llm_explicit_repair_program),
        )
    _dump_json(output_dir / "summary.json", summary)
    print(json.dumps(summary["totals"], ensure_ascii=False, indent=2))
    failed = [row for row in rows if row.get("trigger_returncode", 0) or row.get("rewrite_returncode", 0)]
    if args.strict_rewrite_gates and not (summary.get("strict_rewrite_gate") or {}).get("passed", True):
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
