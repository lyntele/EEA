#!/usr/bin/env python3
"""Dump existing EEA source signals for P0b audit anchors.

This script intentionally defines no new signal extractor. It only orchestrates
existing pipeline utilities and writes their complete JSON payloads for later
diffing.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path
from typing import Any, Dict, List

from method.EEA.rulebook.common.analysis.code_preprocess import compute_structure_bundle
from method.EEA.rulebook.common.analysis.repair_program_normalizer import attach_canonical_repair_ir
from method.EEA.rulebook.common.analysis.role_graph_normalizer import RoleGraphNormalizer
from method.EEA.rulebook.common.analysis.signal_summary import (
    _pred_current_summary,
    build_formation_signals,
)
from method.EEA.rulebook.common.learning.case_pipeline import run_error_instance_pipeline
from method.EEA.rulebook.common.runtime.runtime import build_current_case_signals


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ANCHORS = ROOT / "scripts" / "probes" / "p0b_anchor_cases.json"
DEFAULT_OUTPUT = ROOT / "workspace" / "probes" / "p0b_existing_signal_audit"


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, set):
        return sorted(_jsonable(v) for v in value)
    return str(value)


def _tool_crash(name: str, exc: BaseException) -> Dict[str, Any]:
    return {
        "tool_crash": True,
        "tool": name,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


def _dump_case(case: Dict[str, Any]) -> Dict[str, Any]:
    db_id = str(case["db_id"])
    qid = str(case["qid"])
    db_path = str(case["db_path"])
    database_dir = str(Path(db_path).parent.parent)
    payload: Dict[str, Any] = {
        "qid": qid,
        "db_id": db_id,
        "baseline_correct": case.get("baseline_correct"),
        "enhanced_correct": case.get("enhanced_correct"),
        "role": case.get("role"),
        "question": case.get("question"),
        "evidence": case.get("evidence"),
        "pred_sql": case.get("pred_sql"),
        "gold_sql": case.get("gold_sql"),
        "db_path": db_path,
        "source_run": case.get("source_run"),
        "tools": {},
    }

    try:
        result = run_error_instance_pipeline(
            db_id=db_id,
            case_id=qid,
            question=str(case.get("question") or ""),
            evidence=str(case.get("evidence") or ""),
            pred_sql=str(case.get("pred_sql") or ""),
            gold_sql=str(case.get("gold_sql") or ""),
            db_path=db_path,
            database_dir=database_dir,
            run_compiler=False,
        )
    except Exception as exc:  # noqa: BLE001 - audit must preserve crash payload.
        payload["pipeline_crash"] = _tool_crash("run_error_instance_pipeline", exc)
        return payload

    code_summary = dict(result.code_prepared_summary or {})
    runtime_case_view = result.runtime_case_view
    case_signal_view = getattr(runtime_case_view, "case_signal_view", None)
    if case_signal_view is None:
        case_signal_view = code_summary.get("case_signal_view")

    try:
        payload["tools"]["case_signal_view"] = _jsonable(case_signal_view)
    except Exception as exc:  # noqa: BLE001
        payload["tools"]["case_signal_view"] = _tool_crash("build_case_signal_view", exc)

    role_graph_pred: Dict[str, Any] = {}
    role_graph_gold: Dict[str, Any] = {}
    try:
        normalizer = RoleGraphNormalizer()
        role_graph_pred = normalizer.normalize_sql(
            sql=str(case.get("pred_sql") or ""),
            schema_view=runtime_case_view.local_schema_view,
            source="pred_sql",
        )
        role_graph_gold = normalizer.normalize_sql(
            sql=str(case.get("gold_sql") or ""),
            schema_view=runtime_case_view.local_schema_view,
            source="gold_sql",
        )
        payload["tools"]["role_graph"] = {
            "pred_sql": _jsonable(role_graph_pred),
            "gold_sql": _jsonable(role_graph_gold),
        }
    except Exception as exc:  # noqa: BLE001
        payload["tools"]["role_graph"] = _tool_crash("RoleGraphNormalizer.normalize_sql", exc)

    try:
        payload["tools"]["current_case_signals"] = sorted(
            build_current_case_signals(
                runtime_case_view,
                sql_role_graph=role_graph_pred,
                local_schema_view=runtime_case_view.local_schema_view,
            )
        )
    except Exception as exc:  # noqa: BLE001
        payload["tools"]["current_case_signals"] = _tool_crash("build_current_case_signals", exc)

    try:
        payload["tools"]["pred_current_summary"] = _jsonable(
            _pred_current_summary(case_signal_view)
        )
    except Exception as exc:  # noqa: BLE001
        payload["tools"]["pred_current_summary"] = _tool_crash("_pred_current_summary", exc)

    try:
        formation_signals = build_formation_signals(
            case_signal_view=runtime_case_view.case_signal_view,
            delta_signature=result.case_audit.delta_signature,
            error_instance=result.error_instance,
        )
        enriched_error = attach_canonical_repair_ir(
            error_instance=result.error_instance,
            case_audit=result.case_audit,
            runtime_case_view=runtime_case_view,
            formation_signals=formation_signals,
        )
        payload["tools"]["canonical_repair_ir"] = _jsonable(enriched_error.canonical_repair_ir)
    except Exception as exc:  # noqa: BLE001
        payload["tools"]["canonical_repair_ir"] = _tool_crash("attach_canonical_repair_ir", exc)

    try:
        structure_bundle = compute_structure_bundle(
            str(case.get("pred_sql") or ""),
            str(case.get("gold_sql") or ""),
        )
        payload["tools"]["structure_delta"] = _jsonable(
            {
                "structural_delta": structure_bundle.get("structural_delta"),
                "structure_flags": structure_bundle.get("structure_flags"),
                "legacy_signature": structure_bundle.get("legacy_signature"),
                "legacy_signature_dict": structure_bundle.get("legacy_signature_dict"),
                "join_edges_diff": structure_bundle.get("join_edges_diff"),
                "pred_ast": structure_bundle.get("pred_ast"),
                "gold_ast": structure_bundle.get("gold_ast"),
            }
        )
    except Exception as exc:  # noqa: BLE001
        payload["tools"]["structure_delta"] = _tool_crash("compute_structure_bundle", exc)

    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--anchor_cases", type=Path, default=DEFAULT_ANCHORS)
    parser.add_argument("--output_dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    anchors: List[Dict[str, Any]] = json.loads(args.anchor_cases.read_text())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary: List[Dict[str, Any]] = []
    for case in anchors:
        qid = str(case["qid"])
        db_id = str(case["db_id"])
        out = _dump_case(case)
        out_path = args.output_dir / f"{qid}.json"
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        summary.append(
            {
                "qid": qid,
                "db_id": db_id,
                "role": case.get("role"),
                "output_json": str(out_path),
                "pipeline_crash": bool(out.get("pipeline_crash")),
                "tool_crashes": {
                    name: value
                    for name, value in (out.get("tools") or {}).items()
                    if isinstance(value, dict) and value.get("tool_crash")
                },
            }
        )
        print(f"[p0b-audit] dumped {db_id} q{qid} -> {out_path}")

    (args.output_dir / "summary.json").write_text(
        json.dumps(
            {
                "anchor_count": len(anchors),
                "cases": summary,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
