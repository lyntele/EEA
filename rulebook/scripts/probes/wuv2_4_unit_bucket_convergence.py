#!/usr/bin/env python3
"""WUv2-4 unit probe for structured repair-card retrieval buckets.

This is not an online run. It checks whether known same-source manual groups
share the new code-derived retrieval keys when reconstructed from existing
full11 run artifacts.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


RUN_ROOT = Path(
    "/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL/workspace/rulebook_runs"
)
DEFAULT_RUNS = {
    "codebase_community": "full11_codebase_community_postsel_v1_qwen3coderflash_20260510_164346",
    "financial": "full11_financial_postsel_v1_qwen3coderflash_20260510_164346",
    "european_football_2": "full11_european_football_2_postsel_v1_qwen3coderflash_20260510_164346",
}

PROBE_GROUPS = [
    {
        "db_id": "codebase_community",
        "label": "editor_to_owner",
        "case_ids": ["581", "582"],
        "min_shared_members": 2,
        "note": "manual formal pattern: editor -> Owner",
    },
    {
        "db_id": "codebase_community",
        "label": "comment_time",
        "case_ids": ["616", "617"],
        "min_shared_members": 2,
        "note": "manual formal pattern: comment time",
    },
    {
        "db_id": "codebase_community",
        "label": "comment_score",
        "case_ids": ["709", "710"],
        "min_shared_members": 2,
        "note": "manual formal pattern: comment score",
    },
    {
        "db_id": "financial",
        "label": "id_output",
        "case_ids": ["141", "180", "193"],
        "min_shared_members": 2,
        "note": "manual formal pattern: ID output; expectation is at least two merge",
    },
    {
        "db_id": "european_football_2",
        "label": "match_home_player_wide_table",
        "case_ids": ["1119", "1120", "1121"],
        "min_shared_members": 2,
        "note": "manual same-source group; expectation is at least two merge",
    },
    {
        "db_id": "european_football_2",
        "label": "avg_to_count_formula",
        "case_ids": ["1068", "1093"],
        "min_shared_members": 2,
        "note": "manual formal pattern: AVG -> count-like formula correction",
    },
    {
        "db_id": "european_football_2",
        "label": "league_match_multi_output",
        "case_ids": ["1038", "1085"],
        "min_shared_members": 2,
        "note": "manual formal pattern pair used to fill the incomplete EF2 row in the task",
    },
]


def _add_paths(ace_root: Path) -> None:
    path = str(ace_root)
    if path not in sys.path:
        sys.path.insert(0, path)


def _dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload if isinstance(payload, dict) else {}


def _case_dir(run_name: str, case_id: str) -> Path:
    return RUN_ROOT / run_name / ".state" / "work" / f"qid_{case_id}"


def _request_payload(run_name: str, case_id: str) -> Dict[str, Any]:
    base = _case_dir(run_name, case_id)
    for name in ("eea_update_request.json", "eea_official_reconcile_request.json"):
        path = base / name
        if path.exists():
            return _load_json(path)
    path = base / "eea_runtime_request.json"
    return _load_json(path) if path.exists() else {}


def _group_for_case(groups: Sequence[Any], case_id: str) -> Optional[Any]:
    matches = [
        group
        for group in groups
        if case_id in {str(item) for item in getattr(group, "case_ids", []) or []}
    ]
    if not matches:
        return None
    singletons = [group for group in matches if len(getattr(group, "case_ids", []) or []) == 1]
    if singletons:
        return singletons[0]
    return sorted(matches, key=lambda group: len(getattr(group, "case_ids", []) or []))[0]


def _interface_key(group: Any) -> str:
    signals = getattr(group, "formation_signals", None) or {}
    if hasattr(signals, "model_dump"):
        signals = signals.model_dump(mode="json")
    insight = dict((signals.get("repair_insight_signature") or {}) or {})
    return str(
        insight.get("interface_key")
        or insight.get("repair_interface")
        or insight.get("target_preference")
        or ""
    )


def _primary_keys(keys: Sequence[Tuple[str, str]]) -> List[str]:
    return sorted(str(key[1]) for key in keys if "answer_unit_op:" in str(key[1]))


def _shared_key_stats(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    primary_counter: Counter[str] = Counter()
    any_counter: Counter[str] = Counter()
    for row in rows:
        primary_counter.update(row.get("primary_keys") or [])
        any_counter.update(row.get("bucket_keys") or [])
    primary_key, primary_count = ("", 0)
    if primary_counter:
        primary_key, primary_count = primary_counter.most_common(1)[0]
    any_key, any_count = ("", 0)
    if any_counter:
        any_key, any_count = any_counter.most_common(1)[0]
    return {
        "best_primary_key": primary_key,
        "best_primary_key_member_count": primary_count,
        "best_any_key": any_key,
        "best_any_key_member_count": any_count,
    }


def _report_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# WUv2-4 单元级 probe(retrieval bucket key 派生验证)",
        "",
        "不跑 online；只验证 `derive_repair_card` + `_retrieval_keys_for_card` 在已知 v1 同源案例上的 bucket 行为。",
        "",
        "说明: legacy full11 `final_library.json` 没有 WUv2-4 新字段，本 probe 用 final library 的 group 结构补 operation family，并优先用对应 `.state/work/qid_*/eea_update_request.json` 的 S0/gold SQL 重新派生 answer unit。",
        "",
        "## case 对 bucket 汇合验证",
        "",
        "| db | case 对 | v1 interface_key(选其一) | best primary key(count) | used shared bucket(count) | 汇合? | 备注 |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in payload.get("groups") or []:
        lines.append(
            "| {db} | {cases} | `{interface}` | `{primary}` | `{used}` | {ok} | {note} |".format(
                db=row.get("db_id", ""),
                cases="/".join(str(item) for item in row.get("case_ids") or []),
                interface=str(row.get("sample_interface_key") or "")[:80],
                primary=(
                    f"{str(row.get('best_primary_key') or '')[:100]} "
                    f"({row.get('best_primary_key_member_count') or 0})"
                ),
                used=(
                    f"{str(row.get('best_any_key') or '')[:100]} "
                    f"({row.get('best_any_key_member_count') or 0})"
                ),
                ok="是" if row.get("converged") else "否",
                note=row.get("note", ""),
            )
        )
    primary_count = sum(1 for row in payload.get("groups") or [] if row.get("primary_converged"))
    lines.extend(
        [
            "",
            "## 总结",
            "",
            f"- 7 个 case 组中汇合 {payload.get('converged_group_count')}/7 个。",
            f"- 其中 exact `answer_unit_op:*` 主键汇合 {primary_count}/7 个；其余依赖 `axis:*` 粗筛轴进入后续 semantic/program 判断。",
            f"- 未汇合 case 组: {', '.join(payload.get('not_converged_labels') or []) or '无'}。",
            f"- 判定: {'符合 quick probe 标准' if payload.get('converged_group_count', 0) >= 5 else '不符合 quick probe 标准'}。",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output_json", default="/tmp/wuv2_4_unit_probe.json")
    parser.add_argument(
        "--output_report",
        default="/data/liuyining/ace4sql/method/EEA/rulebook/doc/wuv2_4_unit_probe_report.md",
    )
    parser.add_argument("--ace_root", default="/data/liuyining/ace4sql")
    args = parser.parse_args()

    _add_paths(Path(args.ace_root).resolve())

    from method.EEA.rulebook.common.analysis.repair_card_normalizer import (  # noqa: WPS433
        _derive_answer_unit_from_sql,
        repair_card_from_group_summary,
    )
    from method.EEA.rulebook.common.core.data_structures import LibraryStateV2  # noqa: WPS433
    from method.EEA.rulebook.common.learning.pattern_formation import (  # noqa: WPS433
        _retrieval_keys_for_card,
    )

    libraries: Dict[str, Any] = {}
    for db_id, run_name in DEFAULT_RUNS.items():
        path = RUN_ROOT / run_name / "final_library.json"
        libraries[db_id] = LibraryStateV2.model_validate(_load_json(path))

    group_rows: List[Dict[str, Any]] = []
    for spec in PROBE_GROUPS:
        db_id = str(spec["db_id"])
        run_name = DEFAULT_RUNS[db_id]
        library = libraries[db_id]
        all_groups = list(library.patterns) + list(library.singletons)
        case_rows: List[Dict[str, Any]] = []
        for case_id in spec["case_ids"]:
            group = _group_for_case(all_groups, str(case_id))
            if group is None:
                case_rows.append(
                    {
                        "case_id": str(case_id),
                        "missing": True,
                        "bucket_keys": [],
                        "primary_keys": [],
                    }
                )
                continue
            repair_card = repair_card_from_group_summary(group)
            request = _request_payload(run_name, str(case_id))
            selected_sql = str(request.get("selected_sql") or "").strip()
            gold_sql = str(request.get("gold_sql") or "").strip()
            if selected_sql:
                repair_card["source_answer_unit"] = _derive_answer_unit_from_sql(
                    selected_sql,
                    None,
                )
            if gold_sql:
                repair_card["target_answer_unit"] = _derive_answer_unit_from_sql(
                    gold_sql,
                    None,
                )
            card = {
                "db_id": db_id,
                "case_ids": [str(case_id)],
                "repair_card": repair_card,
                "delta_axes": list(repair_card.get("effect_axis") or []),
            }
            keys = sorted(_retrieval_keys_for_card(card))
            labels = [str(key[1]) for key in keys]
            case_rows.append(
                {
                    "case_id": str(case_id),
                    "group_id": getattr(group, "group_id", ""),
                    "group_type": str(getattr(getattr(group, "group_type", ""), "value", getattr(group, "group_type", ""))),
                    "interface_key": _interface_key(group),
                    "repair_card": repair_card,
                    "bucket_keys": labels,
                    "primary_keys": _primary_keys(keys),
                }
            )
        stats = _shared_key_stats(case_rows)
        min_shared = int(spec.get("min_shared_members") or len(spec["case_ids"]))
        primary_converged = int(stats.get("best_primary_key_member_count") or 0) >= min_shared
        converged = primary_converged
        if not converged:
            # Axis keys remain the documented coarse recall fallback.  This
            # keeps the report explicit when exact answer-unit keys are too
            # narrow on legacy artifacts.
            converged = int(stats.get("best_any_key_member_count") or 0) >= min_shared
        sample_interface = next(
            (str(row.get("interface_key") or "") for row in case_rows if row.get("interface_key")),
            "",
        )
        group_rows.append(
            {
                "db_id": db_id,
                "label": spec["label"],
                "case_ids": list(spec["case_ids"]),
                "min_shared_members": min_shared,
                "sample_interface_key": sample_interface,
                "note": spec["note"],
                "cases": case_rows,
                "converged": bool(converged),
                "primary_converged": bool(primary_converged),
                **stats,
            }
        )

    not_converged = [row["label"] for row in group_rows if not row["converged"]]
    payload = {
        "schema_version": "wuv2-4-unit-probe-v1",
        "runs": DEFAULT_RUNS,
        "groups": group_rows,
        "converged_group_count": sum(1 for row in group_rows if row["converged"]),
        "not_converged_labels": not_converged,
    }
    _dump_json(Path(args.output_json).resolve(), payload)
    _write_text(Path(args.output_report).resolve(), _report_markdown(payload))
    print(
        json.dumps(
            {
                "output_json": str(Path(args.output_json).resolve()),
                "output_report": str(Path(args.output_report).resolve()),
                "converged_group_count": payload["converged_group_count"],
                "not_converged_labels": not_converged,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
