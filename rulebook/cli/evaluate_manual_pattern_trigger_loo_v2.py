#!/usr/bin/env python3
"""Evaluate runtime pattern triggering against manual pattern groups.

This is an audit tool. It does not mutate the library, does not call rewrite,
and does not use the held-out target gold SQL for runtime triggering.

The evaluation asks two questions for each manually labeled formal pattern:

1. Positive leave-one-out: when one member is held out, does the current runtime
   trigger select a memory object backed by the other members of the same
   manual pattern?
2. Noise pressure: when near-but-different cases are used as targets, does the
   same positive memory object get selected incorrectly?

The script uses existing LibraryStateV2 JSON files as the memory source. This
keeps the test focused on runtime trigger/compiler behavior. It projects the
held-out case out of candidate memory object metadata to avoid self replay, but
the input library itself may have been built offline with all cases; this is
reported as ``evaluation_mode=project_existing_library``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


def _add_paths(deepeye_root: Path, ace_root: Path) -> None:
    for path in (str(ace_root), str(deepeye_root)):
        if path not in sys.path:
            sys.path.insert(0, path)


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")
    tmp.replace(path)


def _dump_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str))
            f.write("\n")
    tmp.replace(path)


def _parse_csv(raw: str) -> Optional[Set[str]]:
    if not raw.strip():
        return None
    return {item.strip() for item in raw.split(",") if item.strip()}


def _case_sort_key(value: Any) -> Tuple[int, Any]:
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)


def _model_payload(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_model_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_model_payload(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _model_payload(item) for key, item in value.items()}
    return value


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _sql_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    sql = getattr(value, "sql", None)
    if sql is not None:
        return str(sql)
    return str(value)


def _short_text(value: Any, limit: int = 500) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


@dataclass(frozen=True)
class ManualGroup:
    db_id: str
    section: str
    index: int
    label: str
    name: str
    case_ids: Tuple[str, ...]
    payload: Dict[str, Any]


@dataclass
class CaseInput:
    db_id: str
    case_id: str
    question: str
    evidence: str
    candidates: List[str]
    input_pkl: str


def _manual_groups_for_db(payload: Any, db_id: str) -> List[ManualGroup]:
    db_payload = next(
        (item for item in payload or [] if isinstance(item, dict) and str(item.get("db_id")) == db_id),
        {},
    )
    out: List[ManualGroup] = []
    section_specs = (
        ("patterns", "pattern", "pattern_name"),
        ("experience_families", "family", "family_name"),
    )
    for section, prefix, name_key in section_specs:
        for idx, group in enumerate(db_payload.get(section) or [], start=1):
            case_ids = tuple(str(case_id) for case_id in (group.get("case_ids") or []))
            name = str(group.get(name_key) or group.get("pattern_name") or f"{prefix}_{idx}")
            out.append(
                ManualGroup(
                    db_id=db_id,
                    section=section,
                    index=idx,
                    label=f"{prefix}:{idx}:{name}",
                    name=name,
                    case_ids=case_ids,
                    payload=dict(group),
                )
            )
    singleton_cases = [str(case_id) for case_id in (db_payload.get("singletons") or [])]
    for idx, case_id in enumerate(singleton_cases, start=1):
        out.append(
            ManualGroup(
                db_id=db_id,
                section="singletons",
                index=idx,
                label=f"singleton:{case_id}",
                name=f"singleton:{case_id}",
                case_ids=(case_id,),
                payload={"case_ids": [case_id]},
            )
        )
    return out


def _manual_case_labels(groups: Sequence[ManualGroup]) -> Dict[str, str]:
    labels: Dict[str, str] = {}
    for group in groups:
        for case_id in group.case_ids:
            labels[str(case_id)] = group.label
    return labels


def _manual_pattern_groups(groups: Sequence[ManualGroup], min_size: int) -> List[ManualGroup]:
    return [
        group
        for group in groups
        if group.section == "patterns" and len(group.case_ids) >= min_size
    ]


def _library_path(library_root: Path, db_id: str, filename: str) -> Path:
    return library_root / db_id / filename


def _load_work_roots(args: argparse.Namespace) -> Dict[str, Path]:
    roots: Dict[str, Path] = {}
    if args.work_roots_json:
        payload = _load_json(Path(args.work_roots_json).resolve())
        if isinstance(payload, dict):
            for db_id, path in payload.items():
                roots[str(db_id)] = Path(str(path)).resolve()
        elif isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and item.get("db_id") and item.get("work_root"):
                    roots[str(item["db_id"])] = Path(str(item["work_root"])).resolve()
    if args.work_root and args.db_ids:
        db_ids = _parse_csv(args.db_ids) or set()
        if len(db_ids) == 1:
            roots[next(iter(db_ids))] = Path(args.work_root).resolve()
    return roots


def _load_case_inputs(work_root: Path, load_dataset: Any) -> Dict[str, CaseInput]:
    cases: Dict[str, CaseInput] = {}
    for input_pkl in sorted(work_root.glob("qid_*/rewrite_input.pkl"), key=lambda p: _case_sort_key(p.parent.name.split("_")[-1])):
        dataset = load_dataset(str(input_pkl))
        if not dataset:
            continue
        item = dataset[0]
        candidates = [_sql_text(value) for value in list(item.sql_candidates or [])]
        if not candidates:
            continue
        case_id = str(item.question_id)
        cases[case_id] = CaseInput(
            db_id=str(item.database_id),
            case_id=case_id,
            question=str(item.question),
            evidence=str(item.evidence or ""),
            candidates=candidates,
            input_pkl=str(input_pkl),
        )
    return cases


def _clone_group_for_memory(
    group: Any,
    *,
    remove_case_ids: Set[str],
    force_runtime_usable: bool,
    ensure_materialized_trigger_contract: Any,
) -> Any:
    clone = group.model_copy(deep=True)
    remaining = [str(case_id) for case_id in (clone.case_ids or []) if str(case_id) not in remove_case_ids]
    clone.case_ids = remaining
    clone.support = len(remaining)
    if force_runtime_usable:
        clone.runtime_usable = True
    ensure_materialized_trigger_contract(clone)
    return clone


def _all_memory_objects(library: Any) -> List[Any]:
    return [
        *list(library.patterns or []),
        *list(library.experience_families or []),
        *list(library.singletons or []),
    ]


def _group_type_value(group: Any) -> str:
    return _enum_value(getattr(group, "group_type", ""))


def _group_status_value(group: Any) -> str:
    return _enum_value(getattr(group, "status", ""))


def _group_label_counts(group: Any, case_labels: Dict[str, str]) -> Dict[str, int]:
    counts = Counter(case_labels.get(str(case_id), "unlabeled") for case_id in (group.case_ids or []))
    return dict(sorted(counts.items()))


def _build_runtime_view(
    *,
    case: CaseInput,
    db_root: Path,
    build_runtime_case_view: Any,
    SqliteDBSchemaAccess: Any,
    memory_tables: Optional[Sequence[str]] = None,
    memory_group_ids: Optional[Sequence[str]] = None,
) -> Any:
    db_path = db_root / case.db_id / f"{case.db_id}.sqlite"
    access = SqliteDBSchemaAccess(
        db_id=case.db_id,
        db_path=str(db_path),
        database_dir=str(db_path.parent),
    )
    kwargs = {}
    if memory_tables:
        kwargs["t_mem"] = list(memory_tables)
        kwargs["allow_two_hop_justifications"] = list(memory_group_ids or [])
    return build_runtime_case_view(
        db_id=case.db_id,
        case_id=case.case_id,
        question=case.question,
        evidence=case.evidence,
        pred_top1_sql=case.candidates[0],
        c0_candidate_sqls=list(case.candidates),
        access=access,
        candidate_set_size=len(case.candidates),
        **kwargs,
    )


def _case_signal_similarity(left: Set[str], right: Set[str]) -> float:
    if not left and not right:
        return 0.0
    return len(left & right) / len(left | right)


def _select_noise_targets(
    *,
    positive_group: ManualGroup,
    candidate_case_ids: Iterable[str],
    case_signals: Dict[str, Set[str]],
    max_noise: int,
) -> List[Dict[str, Any]]:
    if max_noise <= 0:
        return []
    positive_cases = set(positive_group.case_ids)
    positive_signal_union: Set[str] = set()
    for case_id in positive_cases:
        positive_signal_union |= set(case_signals.get(str(case_id), set()))
    rows: List[Dict[str, Any]] = []
    for case_id in candidate_case_ids:
        text_id = str(case_id)
        if text_id in positive_cases or text_id not in case_signals:
            continue
        score = _case_signal_similarity(positive_signal_union, case_signals[text_id])
        rows.append({"case_id": text_id, "similarity": score})
    rows.sort(key=lambda row: (-float(row["similarity"]), _case_sort_key(row["case_id"])))
    return rows[:max_noise]


def _select_noise_memory_groups(
    *,
    all_groups: Sequence[Any],
    positive_group_ids: Set[str],
    positive_case_ids: Set[str],
    target_case_id: str,
    noise_target_ids: Set[str],
    max_noise_memory_groups: int,
) -> List[Any]:
    rows: List[Tuple[int, Tuple[int, Any], Any]] = []
    for group in all_groups:
        group_id = str(group.group_id)
        group_cases = {str(case_id) for case_id in (group.case_ids or [])}
        if group_id in positive_group_ids:
            continue
        if target_case_id in group_cases:
            continue
        if group_cases & positive_case_ids:
            continue
        priority = 0 if (group_cases & noise_target_ids) else 1
        rows.append((priority, _case_sort_key(min(group_cases, key=_case_sort_key) if group_cases else group_id), group))
    rows.sort(key=lambda row: (row[0], row[1]))
    groups = [row[2] for row in rows]
    if max_noise_memory_groups > 0:
        groups = groups[:max_noise_memory_groups]
    return groups


def _candidate_counts(candidate_sets: Sequence[Any]) -> Dict[str, int]:
    return {
        _enum_value(getattr(candidate_set, "primitive", "")): len(getattr(candidate_set, "candidates", []) or [])
        for candidate_set in candidate_sets
    }


def _candidate_empty_reasons(candidate_sets: Sequence[Any]) -> Dict[str, str]:
    return {
        _enum_value(getattr(candidate_set, "primitive", "")): str(getattr(candidate_set, "empty_reason", "") or "")
        for candidate_set in candidate_sets
        if not (getattr(candidate_set, "candidates", []) or [])
    }


def _run_trial(
    *,
    trial_kind: str,
    target_case_id: str,
    positive_manual_group: ManualGroup,
    positive_memory_groups: Sequence[Any],
    noise_memory_groups: Sequence[Any],
    target_cases: Dict[str, CaseInput],
    case_labels: Dict[str, str],
    db_root: Path,
    max_selected: int,
    build_runtime_case_view: Any,
    trigger_memory_objects: Any,
    enumerate_candidates: Any,
    SqliteDBSchemaAccess: Any,
    LibraryStateV2: Any,
    GroupType: Any,
    _current_contract_signals: Any,
    _memory_schema_tables: Any,
) -> Dict[str, Any]:
    case = target_cases[target_case_id]
    positive_case_ids = set(positive_manual_group.case_ids)
    library = LibraryStateV2(db_id=case.db_id)
    library.patterns = [
        group
        for group in [*list(positive_memory_groups), *list(noise_memory_groups)]
        if _group_type_value(group) == _enum_value(GroupType.PATTERN)
    ]
    library.experience_families = [
        group
        for group in noise_memory_groups
        if _group_type_value(group) == _enum_value(GroupType.FAMILY)
    ]
    library.singletons = [
        group
        for group in noise_memory_groups
        if _group_type_value(group) == _enum_value(GroupType.SINGLETON)
    ]
    case_view = _build_runtime_view(
        case=case,
        db_root=db_root,
        build_runtime_case_view=build_runtime_case_view,
        SqliteDBSchemaAccess=SqliteDBSchemaAccess,
    )
    trigger_result = trigger_memory_objects(
        library=library,
        case_view=case_view,
        db_id=case.db_id,
        max_selected=max_selected,
        allow_self_singleton_replay=False,
    )
    selected_groups = list(trigger_result.selected_groups or [])
    memory_tables = _memory_schema_tables(selected_groups)
    if memory_tables:
        case_view_for_compiler = _build_runtime_view(
            case=case,
            db_root=db_root,
            build_runtime_case_view=build_runtime_case_view,
            SqliteDBSchemaAccess=SqliteDBSchemaAccess,
            memory_tables=memory_tables,
            memory_group_ids=[group.group_id for group in selected_groups],
        )
    else:
        case_view_for_compiler = case_view
    candidate_sets, schema_diag = enumerate_candidates(
        case_view=case_view_for_compiler,
        memory_objects=selected_groups,
    )
    selected_positive_group_ids = [
        group.group_id
        for group in selected_groups
        if {str(case_id) for case_id in (group.case_ids or [])} & positive_case_ids
    ]
    selected_noise_group_ids = [
        group.group_id
        for group in selected_groups
        if not ({str(case_id) for case_id in (group.case_ids or [])} & positive_case_ids)
    ]
    compiler_counts = _candidate_counts(candidate_sets)
    compiler_candidate_total = sum(compiler_counts.values())
    top_audits = []
    for audit in list(trigger_result.candidates or [])[:12]:
        top_audits.append(
            {
                "group_id": audit.group_id,
                "gate_passed": bool(audit.gate_passed),
                "gate_reasons": list(audit.gate_reasons or []),
                "required_signal_hits": list(audit.required_signal_hits or []),
                "required_signal_misses": list(audit.required_signal_misses or []),
                "negative_signal_hits": list(audit.negative_signal_hits or []),
                "binder_dry_run_success": bool(audit.binder_dry_run_success),
                "final_score": float(audit.final_score or 0.0),
            }
        )
    current_signals = sorted(_current_contract_signals(case_view))
    return {
        "trial_kind": trial_kind,
        "db_id": case.db_id,
        "target_case_id": target_case_id,
        "target_manual_label": case_labels.get(target_case_id, "unlabeled"),
        "manual_pattern_label": positive_manual_group.label,
        "manual_pattern_name": positive_manual_group.name,
        "manual_pattern_case_ids": list(positive_manual_group.case_ids),
        "question": _short_text(case.question),
        "evidence": _short_text(case.evidence),
        "pred_top1_sql": _short_text(case.candidates[0], limit=900),
        "positive_memory_group_ids": [group.group_id for group in positive_memory_groups],
        "positive_memory_case_ids": sorted(
            {
                str(case_id)
                for group in positive_memory_groups
                for case_id in (group.case_ids or [])
            },
            key=_case_sort_key,
        ),
        "noise_memory_group_ids": [group.group_id for group in noise_memory_groups],
        "selected_group_ids": [group.group_id for group in selected_groups],
        "selected_group_types": {
            group.group_id: _group_type_value(group)
            for group in selected_groups
        },
        "selected_group_case_ids": {
            group.group_id: list(group.case_ids or [])
            for group in selected_groups
        },
        "selected_group_manual_label_counts": {
            group.group_id: _group_label_counts(group, case_labels)
            for group in selected_groups
        },
        "selected_positive_group_ids": selected_positive_group_ids,
        "selected_noise_group_ids": selected_noise_group_ids,
        "positive_hit": bool(selected_positive_group_ids),
        "false_trigger_for_pattern": trial_kind == "noise" and bool(selected_positive_group_ids),
        "wrong_group_hit": trial_kind == "positive" and bool(selected_groups) and not bool(selected_positive_group_ids),
        "trigger_status": "selected" if selected_groups else "no_match",
        "compiler_candidate_counts": compiler_counts,
        "compiler_candidate_total": compiler_candidate_total,
        "compiler_bindable": compiler_candidate_total > 0,
        "compiler_empty_reasons": _candidate_empty_reasons(candidate_sets),
        "schema_diagnostics": _model_payload(schema_diag),
        "top_trigger_audits": top_audits,
        "current_signal_sample": current_signals[:80],
        "current_signal_count": len(current_signals),
    }


def _summarize(rows: Sequence[Dict[str, Any]], skipped: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    positives = [row for row in rows if row.get("trial_kind") == "positive"]
    noises = [row for row in rows if row.get("trial_kind") == "noise"]
    positive_hits = [row for row in positives if row.get("positive_hit")]
    positive_wrong = [row for row in positives if row.get("wrong_group_hit")]
    noise_false = [row for row in noises if row.get("false_trigger_for_pattern")]
    bindable_positive_hits = [
        row
        for row in positive_hits
        if row.get("compiler_bindable")
    ]
    per_pattern: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("manual_pattern_label"))
        entry = per_pattern.setdefault(
            key,
            {
                "db_id": row.get("db_id"),
                "manual_pattern_case_ids": row.get("manual_pattern_case_ids"),
                "positive_trials": 0,
                "positive_hits": 0,
                "positive_wrong_group_hits": 0,
                "positive_bindable_hits": 0,
                "noise_trials": 0,
                "noise_false_triggers": 0,
            },
        )
        if row.get("trial_kind") == "positive":
            entry["positive_trials"] += 1
            entry["positive_hits"] += int(bool(row.get("positive_hit")))
            entry["positive_wrong_group_hits"] += int(bool(row.get("wrong_group_hit")))
            entry["positive_bindable_hits"] += int(
                bool(row.get("positive_hit")) and bool(row.get("compiler_bindable"))
            )
        else:
            entry["noise_trials"] += 1
            entry["noise_false_triggers"] += int(bool(row.get("false_trigger_for_pattern")))
    return {
        "evaluation_mode": "project_existing_library",
        "total_trials": len(rows),
        "positive_trials": len(positives),
        "positive_hits": len(positive_hits),
        "positive_wrong_group_hits": len(positive_wrong),
        "positive_trigger_recall": len(positive_hits) / len(positives) if positives else 0.0,
        "positive_bindable_hits": len(bindable_positive_hits),
        "positive_bindable_recall": len(bindable_positive_hits) / len(positives) if positives else 0.0,
        "noise_trials": len(noises),
        "noise_false_triggers": len(noise_false),
        "noise_false_trigger_rate": len(noise_false) / len(noises) if noises else 0.0,
        "skipped_trials": len(skipped),
        "skip_reasons": dict(Counter(str(row.get("reason")) for row in skipped)),
        "per_pattern": dict(sorted(per_pattern.items())),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manual_groups_json", required=True)
    parser.add_argument("--library_root", required=True, help="Root containing {db_id}/{library_filename}")
    parser.add_argument("--library_filename", default="library_families.json")
    parser.add_argument("--bird_db_root", required=True)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--work_root", default="", help="Single-db .state/work root; requires one --db_ids value")
    parser.add_argument("--work_roots_json", default="", help="JSON mapping db_id -> .state/work root")
    parser.add_argument("--db_ids", default="", help="Comma-separated DB ids; empty means all manual DBs with inputs")
    parser.add_argument("--pattern_min_size", type=int, default=2)
    parser.add_argument("--max_patterns", type=int, default=0)
    parser.add_argument("--max_noise_targets_per_positive", type=int, default=3)
    parser.add_argument("--max_noise_memory_groups", type=int, default=16)
    parser.add_argument("--max_selected", type=int, default=2)
    parser.add_argument(
        "--respect_runtime_usable",
        action="store_true",
        help="Do not force copied memory objects runtime_usable for audit.",
    )
    parser.add_argument("--deepeye_root", default="/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL")
    parser.add_argument("--ace_root", default="/data/liuyining/ace4sql")
    args = parser.parse_args(argv)

    _add_paths(Path(args.deepeye_root).resolve(), Path(args.ace_root).resolve())

    from app.dataset import load_dataset  # noqa: WPS433
    from method.EEA.rulebook.common.action_compiler_v2 import enumerate_candidates  # noqa: WPS433
    from method.EEA.rulebook.common.data_structures_v2 import LibraryStateV2  # noqa: WPS433
    from method.EEA.rulebook.common.db_schema_access_v2 import SqliteDBSchemaAccess  # noqa: WPS433
    from method.EEA.rulebook.common.runtime_v2 import (  # noqa: WPS433
        _current_contract_signals,
        _memory_schema_tables,
        build_runtime_case_view,
        trigger_memory_objects,
    )
    from method.EEA.rulebook.common.trigger_contract_v2 import ensure_materialized_trigger_contract  # noqa: WPS433
    from method.EEA.rulebook.common.vocabulary_v2 import GroupType  # noqa: WPS433

    manual_payload = _load_json(Path(args.manual_groups_json).resolve())
    library_root = Path(args.library_root).resolve()
    db_root = Path(args.bird_db_root).resolve()
    output_dir = Path(args.output_dir).resolve()
    work_roots = _load_work_roots(args)
    wanted_dbs = _parse_csv(args.db_ids)
    manual_db_ids = [
        str(item.get("db_id"))
        for item in manual_payload
        if isinstance(item, dict) and item.get("db_id")
    ]
    db_ids = [db_id for db_id in manual_db_ids if wanted_dbs is None or db_id in wanted_dbs]

    rows: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    db_reports: List[Dict[str, Any]] = []

    for db_id in db_ids:
        work_root = work_roots.get(db_id)
        if work_root is None:
            skipped.append({"db_id": db_id, "reason": "missing_work_root"})
            continue
        library_json = _library_path(library_root, db_id, args.library_filename)
        if not library_json.exists():
            skipped.append({"db_id": db_id, "reason": "missing_library_json", "path": str(library_json)})
            continue
        manual_groups = _manual_groups_for_db(manual_payload, db_id)
        case_labels = _manual_case_labels(manual_groups)
        manual_patterns = _manual_pattern_groups(manual_groups, min_size=args.pattern_min_size)
        if args.max_patterns > 0:
            manual_patterns = manual_patterns[: args.max_patterns]
        target_cases = _load_case_inputs(work_root, load_dataset)
        source_library = LibraryStateV2.model_validate(_load_json(library_json))
        source_groups = _all_memory_objects(source_library)
        case_signals: Dict[str, Set[str]] = {}
        for case_id, case in target_cases.items():
            if case.db_id != db_id:
                continue
            try:
                view = _build_runtime_view(
                    case=case,
                    db_root=db_root,
                    build_runtime_case_view=build_runtime_case_view,
                    SqliteDBSchemaAccess=SqliteDBSchemaAccess,
                )
                case_signals[case_id] = set(_current_contract_signals(view))
            except Exception as exc:
                skipped.append(
                    {
                        "db_id": db_id,
                        "case_id": case_id,
                        "reason": "case_signal_build_failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

        for manual_pattern in manual_patterns:
            pattern_case_ids = set(manual_pattern.case_ids)
            for target_case_id in manual_pattern.case_ids:
                if target_case_id not in target_cases:
                    skipped.append(
                        {
                            "db_id": db_id,
                            "manual_pattern_label": manual_pattern.label,
                            "target_case_id": target_case_id,
                            "reason": "missing_target_rewrite_input",
                        }
                    )
                    continue
                held_in = pattern_case_ids - {target_case_id}
                positive_source_groups = [
                    group
                    for group in source_library.patterns or []
                    if ({str(case_id) for case_id in (group.case_ids or [])} & held_in)
                ]
                positive_source_groups = [
                    group
                    for group in positive_source_groups
                    if _group_status_value(group) == "active"
                ]
                if not positive_source_groups:
                    skipped.append(
                        {
                            "db_id": db_id,
                            "manual_pattern_label": manual_pattern.label,
                            "target_case_id": target_case_id,
                            "reason": "no_existing_pattern_memory_for_held_in_cases",
                            "held_in_case_ids": sorted(held_in, key=_case_sort_key),
                        }
                    )
                    continue
                positive_memory_groups = [
                    _clone_group_for_memory(
                        group,
                        remove_case_ids={target_case_id},
                        force_runtime_usable=not args.respect_runtime_usable,
                        ensure_materialized_trigger_contract=ensure_materialized_trigger_contract,
                    )
                    for group in positive_source_groups
                ]
                positive_memory_groups = [group for group in positive_memory_groups if group.case_ids]
                if not positive_memory_groups:
                    skipped.append(
                        {
                            "db_id": db_id,
                            "manual_pattern_label": manual_pattern.label,
                            "target_case_id": target_case_id,
                            "reason": "positive_memory_empty_after_projection",
                        }
                    )
                    continue

                noise_target_rows = _select_noise_targets(
                    positive_group=manual_pattern,
                    candidate_case_ids=case_labels,
                    case_signals=case_signals,
                    max_noise=args.max_noise_targets_per_positive,
                )
                noise_target_ids = {str(row["case_id"]) for row in noise_target_rows}
                positive_memory_group_ids = {str(group.group_id) for group in positive_source_groups}
                noise_source_groups = _select_noise_memory_groups(
                    all_groups=source_groups,
                    positive_group_ids=positive_memory_group_ids,
                    positive_case_ids=pattern_case_ids,
                    target_case_id=target_case_id,
                    noise_target_ids=noise_target_ids,
                    max_noise_memory_groups=args.max_noise_memory_groups,
                )
                noise_memory_groups = [
                    _clone_group_for_memory(
                        group,
                        remove_case_ids={target_case_id},
                        force_runtime_usable=not args.respect_runtime_usable,
                        ensure_materialized_trigger_contract=ensure_materialized_trigger_contract,
                    )
                    for group in noise_source_groups
                ]
                noise_memory_groups = [group for group in noise_memory_groups if group.case_ids]

                try:
                    rows.append(
                        _run_trial(
                            trial_kind="positive",
                            target_case_id=target_case_id,
                            positive_manual_group=manual_pattern,
                            positive_memory_groups=positive_memory_groups,
                            noise_memory_groups=noise_memory_groups,
                            target_cases=target_cases,
                            case_labels=case_labels,
                            db_root=db_root,
                            max_selected=args.max_selected,
                            build_runtime_case_view=build_runtime_case_view,
                            trigger_memory_objects=trigger_memory_objects,
                            enumerate_candidates=enumerate_candidates,
                            SqliteDBSchemaAccess=SqliteDBSchemaAccess,
                            LibraryStateV2=LibraryStateV2,
                            GroupType=GroupType,
                            _current_contract_signals=_current_contract_signals,
                            _memory_schema_tables=_memory_schema_tables,
                        )
                    )
                except Exception as exc:
                    skipped.append(
                        {
                            "db_id": db_id,
                            "manual_pattern_label": manual_pattern.label,
                            "target_case_id": target_case_id,
                            "reason": "positive_trial_failed",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )

                for noise_row in noise_target_rows:
                    noise_case_id = str(noise_row["case_id"])
                    if noise_case_id not in target_cases:
                        continue
                    try:
                        trial = _run_trial(
                            trial_kind="noise",
                            target_case_id=noise_case_id,
                            positive_manual_group=manual_pattern,
                            positive_memory_groups=positive_memory_groups,
                            noise_memory_groups=noise_memory_groups,
                            target_cases=target_cases,
                            case_labels=case_labels,
                            db_root=db_root,
                            max_selected=args.max_selected,
                            build_runtime_case_view=build_runtime_case_view,
                            trigger_memory_objects=trigger_memory_objects,
                            enumerate_candidates=enumerate_candidates,
                            SqliteDBSchemaAccess=SqliteDBSchemaAccess,
                            LibraryStateV2=LibraryStateV2,
                            GroupType=GroupType,
                            _current_contract_signals=_current_contract_signals,
                            _memory_schema_tables=_memory_schema_tables,
                        )
                        trial["noise_similarity_to_positive_pattern"] = noise_row["similarity"]
                        rows.append(trial)
                    except Exception as exc:
                        skipped.append(
                            {
                                "db_id": db_id,
                                "manual_pattern_label": manual_pattern.label,
                                "target_case_id": noise_case_id,
                                "reason": "noise_trial_failed",
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )

        db_reports.append(
            {
                "db_id": db_id,
                "library_json": str(library_json),
                "work_root": str(work_root),
                "manual_pattern_count": len(manual_patterns),
                "available_case_inputs": len(target_cases),
                "source_patterns": len(source_library.patterns or []),
                "source_families": len(source_library.experience_families or []),
                "source_singletons": len(source_library.singletons or []),
            }
        )

    summary = _summarize(rows, skipped)
    payload = {
        "summary": summary,
        "config": {
            "manual_groups_json": str(Path(args.manual_groups_json).resolve()),
            "library_root": str(library_root),
            "library_filename": args.library_filename,
            "bird_db_root": str(db_root),
            "db_ids": db_ids,
            "pattern_min_size": args.pattern_min_size,
            "max_patterns": args.max_patterns,
            "max_noise_targets_per_positive": args.max_noise_targets_per_positive,
            "max_noise_memory_groups": args.max_noise_memory_groups,
            "max_selected": args.max_selected,
            "respect_runtime_usable": bool(args.respect_runtime_usable),
            "forced_runtime_usable_for_audit": not bool(args.respect_runtime_usable),
        },
        "db_reports": db_reports,
    }
    _dump_json(output_dir / "manual_pattern_trigger_loo_summary.json", payload)
    _dump_jsonl(output_dir / "manual_pattern_trigger_loo_cases.jsonl", rows)
    _dump_jsonl(output_dir / "manual_pattern_trigger_loo_skipped.jsonl", skipped)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
