#!/usr/bin/env python3
"""Dump compact pattern signatures from a library or run work root.

The output is intentionally self-contained enough to compare pre/post
emergence-refactor libraries without replaying experiments.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _dump_json(path: Optional[Path], payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if path is None:
        print(text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def _payload(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_payload(item) for item in value]
    return value


def _library_path_from_work_root(work_root: Path) -> Path:
    candidates = [
        work_root / "final_library.json",
        work_root / "library.json",
        work_root / "library_latest.json",
        work_root / "eea_finalize_response.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"No library JSON found under {work_root}")


def _extract_library_payload(path: Path) -> Dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"Library JSON must be an object: {path}")
    if isinstance(payload.get("library"), dict):
        return dict(payload["library"])
    if isinstance(payload.get("final_library"), dict):
        return dict(payload["final_library"])
    if isinstance(payload.get("library_state"), dict):
        return dict(payload["library_state"])
    return payload


def _compact_repair_insight(group: Dict[str, Any]) -> Dict[str, Any]:
    signals = group.get("formation_signals") if isinstance(group.get("formation_signals"), dict) else {}
    singleton = signals.get("singleton") if isinstance(signals.get("singleton"), dict) else {}
    insight = singleton.get("repair_insight_signature") if isinstance(singleton.get("repair_insight_signature"), dict) else {}
    if insight:
        return {
            "interface_key": insight.get("interface_key"),
            "source_misread": insight.get("source_misread"),
            "target_preference": insight.get("target_preference"),
            "repair_interface": insight.get("repair_interface"),
            "preserve_invariants": list(insight.get("preserve_invariants") or []),
        }
    admission = signals.get("pattern_admission") if isinstance(signals.get("pattern_admission"), dict) else {}
    return {
        "stable_bias_key": admission.get("stable_bias_key"),
        "primary_repair_interface": admission.get("primary_repair_interface"),
    }


def _compact_instantiation(group: Dict[str, Any]) -> Dict[str, Any]:
    program = group.get("instantiation_program") if isinstance(group.get("instantiation_program"), dict) else {}
    synthesized = program.get("synthesized_program") if isinstance(program.get("synthesized_program"), dict) else {}
    envelope = synthesized.get("program_envelope") if isinstance(synthesized.get("program_envelope"), dict) else {}
    runtime_branches = [
        {
            "branch_id": branch.get("branch_id"),
            "support_case_ids": list(branch.get("support_case_ids") or []),
            "runtime_usable": bool(branch.get("runtime_usable")),
            "runtime_blockers": list(branch.get("runtime_blockers") or []),
            "allowed_primitives": list(branch.get("allowed_primitives") or []),
        }
        for branch in (envelope.get("runtime_branches") or [])
        if isinstance(branch, dict)
    ]
    return {
        "shared": bool(program.get("shared")),
        "shared_status": program.get("shared_status"),
        "bias_recognition_contract": program.get("bias_recognition_contract") or {},
        "pattern_recognition_contract": program.get("pattern_recognition_contract") or {},
        "runtime_branches": runtime_branches,
    }


def _compact_action_contract(trigger_contract: Dict[str, Any]) -> Dict[str, Any]:
    action_contract = (
        trigger_contract.get("action_contract")
        if isinstance(trigger_contract.get("action_contract"), dict)
        else {}
    )
    repair_program = [
        {
            "step_id": step.get("step_id"),
            "op": step.get("op"),
            "locus": step.get("locus"),
            "is_dependency": bool(step.get("is_dependency")),
            "required": bool(step.get("required", True)),
        }
        for step in (action_contract.get("repair_program") or [])[:12]
        if isinstance(step, dict)
    ]
    return {
        "locus": action_contract.get("locus"),
        "op_family": action_contract.get("op_family"),
        "target_family": action_contract.get("target_family"),
        "output_shape_delta": action_contract.get("output_shape_delta") or {},
        "max_actions": action_contract.get("max_actions"),
        "program_type": action_contract.get("program_type"),
        "selection_policy": action_contract.get("selection_policy"),
        "repair_program": repair_program,
        "rewrite_hint_template": action_contract.get("rewrite_hint_template"),
    }


def _pattern_rows(library: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for group in library.get("patterns") or []:
        if not isinstance(group, dict):
            continue
        trigger_contract = group.get("trigger_contract") if isinstance(group.get("trigger_contract"), dict) else {}
        formation_signals = group.get("formation_signals") if isinstance(group.get("formation_signals"), dict) else {}
        rows.append(
            {
                "group_id": group.get("group_id"),
                "db_id": group.get("db_id"),
                "case_ids": list(group.get("case_ids") or []),
                "support": group.get("support"),
                "runtime_usable": bool(group.get("runtime_usable")),
                "runtime_contract_status": group.get("runtime_contract_status"),
                "runtime_blockers": list(group.get("runtime_blockers") or []),
                "promotion_state": (group.get("lifecycle") or {}).get("promotion_state")
                if isinstance(group.get("lifecycle"), dict)
                else None,
                "trigger_contract_required_signals": list(trigger_contract.get("required_signals") or []),
                "trigger_contract_variant_sets": list(trigger_contract.get("variant_required_signal_sets") or []),
                "trigger_contract_action_contract": _compact_action_contract(trigger_contract),
                "repair_insight_signature": _compact_repair_insight(group),
                "formation_pattern_admission": formation_signals.get("pattern_admission") or {},
                "instantiation_program": _compact_instantiation(group),
            }
        )
    return rows


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library_json", default="", help="Path to final_library.json/library.json.")
    parser.add_argument("--work_root", default="", help="Run directory containing a library JSON.")
    parser.add_argument("--output_json", default="", help="Output JSON path. Defaults to stdout.")
    args = parser.parse_args(argv)

    if not args.library_json and not args.work_root:
        parser.error("one of --library_json or --work_root is required")

    library_path = (
        Path(args.library_json).resolve()
        if args.library_json
        else _library_path_from_work_root(Path(args.work_root).resolve())
    )
    library = _extract_library_payload(library_path)
    payload = {
        "schema_version": "pattern-signature-audit-v1",
        "library_path": str(library_path),
        "db_id": library.get("db_id"),
        "counts": {
            "patterns": len(library.get("patterns") or []),
            "singletons": len(library.get("singletons") or []),
            "experience_families": len(library.get("experience_families") or []),
            "cases_processed": int(library.get("cases_processed") or 0),
        },
        "patterns": _pattern_rows(library),
    }
    output_path = Path(args.output_json).resolve() if args.output_json else None
    _dump_json(output_path, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
