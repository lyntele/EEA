#!/usr/bin/env python3
"""Convert manually labeled formal patterns into v2 pattern candidates.

The produced patterns are offline-only: ``runtime_usable=false``. They are
inputs for compiler replay and promotion checks, not runtime memory.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")


def _group_by_case(library: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for bucket in ("singletons", "patterns", "experience_families"):
        for group in library.get(bucket) or []:
            for case_id in group.get("case_ids") or []:
                out[str(case_id)] = group
    return out


def _skeleton_payload(group: Dict[str, Any]) -> Dict[str, Any]:
    return (
        (((group.get("core_interface") or {}).get("repair_skeleton_prototype") or {}).get("structural"))
        or {}
    )


def _skeleton_key(group: Dict[str, Any]) -> str:
    skel = _skeleton_payload(group)
    return "|".join(
        str(skel.get(key) or "")
        for key in ("locus", "op_family", "target_family", "output_contract")
    )


def _shape_delta_payload(group: Dict[str, Any]) -> Dict[str, Any]:
    delta = ((group.get("formation_signals") or {}).get("delta") or {})
    shape = dict((delta.get("output_shape_delta") or {}) or {})
    if not shape:
        shape = dict((_skeleton_payload(group).get("output_shape_delta") or {}) or {})
    current = shape.get("current_arity")
    target = shape.get("target_arity")
    if current is not None and target is not None:
        arity_delta = int(target) - int(current)
        shape["arity_delta"] = arity_delta
        shape["arity_direction"] = (
            "increase" if arity_delta > 0 else "decrease" if arity_delta < 0 else "same"
        )
    return shape


def _abstract_interface_key(group: Dict[str, Any]) -> str:
    skel = _skeleton_payload(group)
    shape = _shape_delta_payload(group)
    if shape:
        shape_key = "|".join(
            [
                f"shape_op={shape.get('operation')}",
                f"shape_dir={shape.get('arity_direction')}",
                f"target_grain={shape.get('target_grain')}",
                f"source_grain={shape.get('current_grain')}",
            ]
        )
    else:
        shape_key = "shape=unavailable"
    return "|".join(
        [
            str(skel.get("locus") or ""),
            str(skel.get("op_family") or ""),
            str(skel.get("target_family") or ""),
            shape_key,
        ]
    )


def _representative_group(groups: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = Counter(_abstract_interface_key(group) for group in groups)
    best_key, _ = counts.most_common(1)[0]
    return next(group for group in groups if _abstract_interface_key(group) == best_key)


def _merge_member_evidence(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    members: List[Dict[str, Any]] = []
    for group in groups:
        evidence = (group.get("formation_evidence") or {}).get("member_evidence") or []
        if evidence:
            members.extend(evidence)
        else:
            for case_id in group.get("case_ids") or []:
                members.append({"case_id": str(case_id), "legacy_only": True})
    return members


def _manual_db(payload: Any, db_id: str) -> Dict[str, Any]:
    for item in payload or []:
        if item.get("db_id") == db_id:
            return item
    return {}


def _manual_pattern_to_group(
    *,
    db_id: str,
    index: int,
    manual: Dict[str, Any],
    source_groups: List[Dict[str, Any]],
    covered_case_ids: List[str],
    GroupSummary: Any,
    GroupType: Any,
    GroupStatus: Any,
    Confidence: Any,
    GroupFormationEvidence: Any,
    GroupLifecycle: Any,
    pattern_id_suffix: str = "",
) -> Any:
    rep = _representative_group(source_groups)
    now = datetime.utcnow().isoformat()
    skeleton_keys = sorted({_skeleton_key(group) for group in source_groups})
    interface_keys = sorted({_abstract_interface_key(group) for group in source_groups})
    review_status = "manual_pattern_candidate"
    if len(interface_keys) > 1:
        review_status = "manual_pattern_candidate_mixed_action_interface"
    pattern_name = str(manual.get("pattern_name") or f"manual_pattern_{index}")
    source_templates: List[str] = []
    seen_templates: set[str] = set()
    for group in source_groups:
        source_template = str(((group.get("instantiation_program") or {}).get("template")) or "").strip()
        if source_template and source_template not in seen_templates:
            seen_templates.add(source_template)
            source_templates.append(source_template)
    hint_parts = [
        "Observed member instantiations:\n" + "\n".join(source_templates)
        if source_templates
        else "",
        manual.get("instantiation_rule"),
        manual.get("rewrite_hint"),
        manual.get("shared_fix_template"),
    ]
    template = "\n".join(str(part) for part in hint_parts if part)
    payload = {
        "group_id": f"manual-pattern-{db_id}-p{index}{pattern_id_suffix}",
        "group_type": GroupType.PATTERN.value,
        "db_id": db_id,
        "case_ids": list(covered_case_ids),
        "support": len(covered_case_ids),
        "confidence": Confidence.MEDIUM.value,
        "version": 0,
        "runtime_usable": False,
        "status": GroupStatus.ACTIVE.value,
        "core_interface": {
            "question_family_tags": sorted(
                {
                    tag
                    for group in source_groups
                    for tag in ((group.get("core_interface") or {}).get("question_family_tags") or [])
                }
            ),
            "pred_family_tags": sorted(
                {
                    tag
                    for group in source_groups
                    for tag in ((group.get("core_interface") or {}).get("pred_family_tags") or [])
                }
            ),
            "repair_goal": manual.get("shared_experience")
            or manual.get("shared_reason")
            or ((rep.get("core_interface") or {}).get("repair_goal") or pattern_name),
            "repair_skeleton_prototype": (rep.get("core_interface") or {}).get("repair_skeleton_prototype"),
        },
        "instantiation_program": {
            "shared": len(interface_keys) == 1,
            "template": template or manual.get("rewrite_hint") or pattern_name,
            "slots": (rep.get("instantiation_program") or {}).get("slots") or [],
            "branch_rules": (rep.get("instantiation_program") or {}).get("branch_rules") or [],
        },
        "trigger_signature": {
            "required_question_tags": [],
            "required_pred_tags": [],
            "decisive_antipatterns": [],
            "negative_evidence": [],
        },
        "trigger_contract": {
            "schema_version": "manual-pattern-offline-v0",
            "required_signals": [],
            "optional_signals": [],
            "negative_signals": [],
            "decisive_pred_signals": [],
            "action_contract": {"max_actions": 1},
            "source_case_contract": {},
            "max_actions": 1,
        },
        "guardrails": (rep.get("guardrails") or []),
        "formation_evidence": GroupFormationEvidence(
            member_evidence=_merge_member_evidence(source_groups),
            manual_alignment_snapshot={
                "manual_label": f"P{index}",
                "manual_name": pattern_name,
                "manual_case_ids": [str(case_id) for case_id in manual.get("case_ids") or []],
                "skeleton_keys": skeleton_keys,
                "abstract_interface_keys": interface_keys,
                "signal_axes": manual.get("abstract_signal_axes") or manual.get("signal_axes") or [],
            },
            formation_version="manual-pattern-conversion-v0",
            review_status=review_status,
        ).model_dump(mode="json"),
        "trigger_policy": {
            "schema_version": "trigger-policy-v0",
            "runtime_visible_only": True,
            "threshold_version": "offline-only",
            "notes": "manual pattern candidate; runtime_usable=false until compiler replay passes",
        },
        "lifecycle": GroupLifecycle(
            formation_parent_ids=[str(group.get("group_id")) for group in source_groups],
            promotion_state="manual_formal_pattern_candidate",
        ).model_dump(mode="json"),
        "created_at": now,
        "last_updated_at": now,
    }
    return GroupSummary.model_validate(payload)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library_json", required=True)
    parser.add_argument("--manual_groups_json", required=True)
    parser.add_argument("--db_id", required=True)
    parser.add_argument("--output_json", required=True)
    parser.add_argument("--report_json", required=True)
    parser.add_argument(
        "--include_nonformal_candidates",
        action="store_true",
        help=(
            "Also emit mixed-skeleton/non-shared manual groups as offline pattern "
            "candidates. Default is false so Step-1 formal replay only sees "
            "formal-eligible patterns."
        ),
    )
    args = parser.parse_args(argv)

    from common.data_structures_v2 import GroupFormationEvidence, GroupLifecycle, GroupSummary, LibraryStateV2
    from common.vocabulary_v2 import Confidence, GroupStatus, GroupType

    library_payload = _load_json(Path(args.library_json))
    manual_payload = _load_json(Path(args.manual_groups_json))
    manual_db = _manual_db(manual_payload, args.db_id)
    if not manual_db:
        raise SystemExit(f"No manual db_id={args.db_id!r} in {args.manual_groups_json}")

    source_by_case = _group_by_case(library_payload)
    patterns: List[Any] = []
    report_rows: List[Dict[str, Any]] = []
    for index, manual in enumerate(manual_db.get("patterns") or []):
        case_ids = [str(case_id) for case_id in manual.get("case_ids") or []]
        source_groups = [source_by_case[case_id] for case_id in case_ids if case_id in source_by_case]
        missing = [case_id for case_id in case_ids if case_id not in source_by_case]
        grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for group in source_groups:
            grouped[_abstract_interface_key(group)].append(group)
        if not grouped:
            grouped["missing_interface"] = []

        interface_items = sorted(grouped.items(), key=lambda item: item[0])
        for interface_index, (interface_key, interface_source_groups) in enumerate(interface_items):
            covered_case_ids = sorted(
                {
                    str(member_case_id)
                    for group in interface_source_groups
                    for member_case_id in group.get("case_ids", [])
                },
                key=lambda value: (0, int(value)) if str(value).isdigit() else (1, str(value)),
            )
            skeleton_keys = sorted({_skeleton_key(group) for group in interface_source_groups})
            interface_keys = sorted({_abstract_interface_key(group) for group in interface_source_groups})
            blocked_reasons: List[str] = []
            if len(interface_source_groups) < 2:
                blocked_reasons.append("support_lt_2")
            if len(interface_keys) != 1:
                blocked_reasons.append("mixed_action_interface")
            formal_replay_eligible = not blocked_reasons
            suffix = f"/I{interface_index}" if len(interface_items) > 1 else ""
            pattern_id_suffix = f"-i{interface_index}" if len(interface_items) > 1 else ""
            row = {
                "manual_label": f"P{index}{suffix}",
                "parent_manual_label": f"P{index}",
                "pattern_name": manual.get("pattern_name"),
                "case_ids": case_ids,
                "covered_case_ids": covered_case_ids,
                "missing_case_ids": sorted(
                    set(missing)
                    | {
                        str(member_case_id)
                        for group in source_groups
                        if group not in interface_source_groups
                        for member_case_id in group.get("case_ids", [])
                    },
                    key=lambda value: (0, int(value)) if str(value).isdigit() else (1, str(value)),
                ),
                "source_group_ids": [group.get("group_id") for group in interface_source_groups],
                "skeleton_keys": skeleton_keys,
                "legacy_skeleton_keys": skeleton_keys,
                "abstract_interface_keys": interface_keys,
                "parent_abstract_interface_keys": sorted(grouped),
                "formal_replay_eligible": formal_replay_eligible,
                "blocked_reasons": blocked_reasons,
                "converted": False,
            }
            if formal_replay_eligible or (
                args.include_nonformal_candidates and len(interface_source_groups) >= 2
            ):
                pattern = _manual_pattern_to_group(
                    db_id=args.db_id,
                    index=index,
                    manual=manual,
                    source_groups=interface_source_groups,
                    covered_case_ids=covered_case_ids,
                    GroupSummary=GroupSummary,
                    GroupType=GroupType,
                    GroupStatus=GroupStatus,
                    Confidence=Confidence,
                    GroupFormationEvidence=GroupFormationEvidence,
                    GroupLifecycle=GroupLifecycle,
                    pattern_id_suffix=pattern_id_suffix,
                )
                patterns.append(pattern)
                row["converted"] = True
                row["review_status"] = pattern.formation_evidence.review_status
            report_rows.append(row)

    output_library = LibraryStateV2.model_validate(library_payload)
    output_library.patterns = list(output_library.patterns) + patterns
    _dump_json(Path(args.output_json), output_library.model_dump(mode="json"))
    report = {
        "db_id": args.db_id,
        "input_library_json": str(Path(args.library_json).resolve()),
        "manual_groups_json": str(Path(args.manual_groups_json).resolve()),
        "converted_patterns": len(patterns),
        "patterns": report_rows,
    }
    _dump_json(Path(args.report_json), report)
    print(json.dumps({"converted_patterns": len(patterns)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
