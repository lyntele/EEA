"""LLM node wrappers for the v2 pipeline.

当前实现的节点：
- ``run_wrong_case_auditor`` —— wrong-case audit, emits ``candidate_fix_sql``
- ``run_error_instance_extractor`` —— executable repair hypotheses
- ``run_action_compiler`` —— selector over code-enumerated candidates
- ``run_memory_rewrite`` —— bounded rewrite from structured actions
- ``run_hint_instantiation`` —— readability-only hint rewrite

职责边界：
- 结构参数、candidate 枚举、coverage、runtime gate 由 code side 负责
- LLM 负责解释、假设、候选选择、以及受限改写
- 不允许 LLM 自行发明 compiler 参数或替代 execution validation

设计要点：
- 每个节点 = 序列化输入 → 构造 prompt → call_llm(expect_json=True) → 反序列化
- ``call_llm`` 已含 JSON 容错重试（common/llm_utils_v2.py）
- 返回 Pydantic 对象，若解析失败抛 RuntimeError
- 入口即清理 SOCKS/HTTP 代理，避免 httpx 在特定环境下 import 失败
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple


# ------------------- 环境准备（必须在 import openai/httpx 之前执行） -------------------


def _strip_proxy_env() -> None:
    """清理进程环境里的 HTTP/SOCKS proxy 变量。

    当前开发环境下全局 shell 代理（127.0.0.1:7897 socks）会让 httpx import 时
    要求 socksio。HKUST-GZ 端点本地可达，不需要任何代理。
    """
    for key in (
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "all_proxy",
    ):
        os.environ.pop(key, None)


_strip_proxy_env()


# ------------------- 公共工具 -------------------


def _json_dump(obj: Any) -> str:
    """把 pydantic/dataclass/普通 dict 序列化成 LLM prompt 里 embed 的 JSON 字符串。"""
    try:
        # Pydantic v2
        if hasattr(obj, "model_dump"):
            return json.dumps(obj.model_dump(mode="python"), ensure_ascii=False, indent=2, default=str)
        if dataclasses.is_dataclass(obj):
            return json.dumps(dataclasses.asdict(obj), ensure_ascii=False, indent=2, default=str)
        return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    except Exception as exc:
        raise RuntimeError(f"Failed to serialize object for prompt: {exc}") from exc


def _string_list(value: Any, *, limit: int = 12) -> List[str]:
    if value is None:
        raw: List[Any] = []
    elif isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    out: List[str] = []
    for item in raw:
        text = str(item).strip()
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _dict_list(value: Any, *, limit: int = 8) -> List[Dict[str, Any]]:
    if isinstance(value, dict):
        raw: List[Any] = [value]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = []
    out: List[Dict[str, Any]] = []
    for item in raw:
        payload = _payload_for_prompt(item)
        if isinstance(payload, dict) and payload:
            out.append(payload)
        if len(out) >= limit:
            break
    return out


def _call_llm_json(
    prompt: str,
    *,
    stage: str,
    trace_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Call the shared LLM helper while preserving old test monkeypatches."""
    from .llm_utils_v2 import call_llm

    try:
        return call_llm(
            prompt,
            expect_json=True,
            stage=stage,
            trace_context=trace_context or {},
        )  # type: ignore[return-value]
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        return call_llm(prompt, expect_json=True)  # type: ignore[return-value]


def _payload_for_prompt(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="python")
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if isinstance(obj, dict):
        return dict(obj)
    if isinstance(obj, list):
        return [_payload_for_prompt(item) for item in obj]
    if isinstance(obj, tuple):
        return [_payload_for_prompt(item) for item in obj]
    return obj


def _compact_local_schema_view_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Keep schema prompts structural and bounded.

    Runtime code still keeps full LocalSchemaView objects. LLM prompts only need
    table/column names, graph edges, role families, and source provenance; long
    column-description notes make prompts noisy and can break large schemas.
    """
    edges = []
    for edge in payload.get("pk_fk_edges") or []:
        edge_payload = _payload_for_prompt(edge)
        if isinstance(edge_payload, dict):
            edges.append(
                {
                    "source": edge_payload.get("source"),
                    "target": edge_payload.get("target"),
                    "kind": edge_payload.get("kind", "fk"),
                }
            )
    hints = []
    seen_hints: set[tuple[str, str, str]] = set()
    for hint in payload.get("semantic_hints") or []:
        hint_payload = _payload_for_prompt(hint)
        if not isinstance(hint_payload, dict):
            continue
        row = {
            "table": hint_payload.get("table"),
            "column": hint_payload.get("column"),
            "role_family": hint_payload.get("role_family"),
        }
        key = (
            str(row.get("table") or ""),
            str(row.get("column") or ""),
            str(row.get("role_family") or ""),
        )
        if key in seen_hints:
            continue
        seen_hints.add(key)
        hints.append(row)
    return {
        "db_id": payload.get("db_id"),
        "tables": list(payload.get("tables") or []),
        "columns_by_table": {
            str(table): [str(column) for column in columns or []]
            for table, columns in (payload.get("columns_by_table") or {}).items()
        },
        "pk_fk_edges": edges,
        "semantic_hints": hints,
        "source": payload.get("source") or {},
    }


def _compact_role_ref(ref: Any) -> Dict[str, Any]:
    payload = _payload_for_prompt(ref)
    if not isinstance(payload, dict):
        return {}
    return {
        "table": payload.get("table"),
        "column": payload.get("column"),
        "expression": payload.get("expression"),
        "sql_role": payload.get("sql_role"),
        "column_role": payload.get("column_role"),
        "path_role": payload.get("path_role"),
        "relation_role": payload.get("relation_role"),
    }


def _canonical_lowering_family_from_op(op: Dict[str, Any]) -> Optional[str]:
    args = op.get("arguments") if isinstance(op.get("arguments"), dict) else {}
    shared_signature = (
        args.get("shared_signature")
        if isinstance(args.get("shared_signature"), dict)
        else {}
    )
    lowering = shared_signature.get("lowering_family")
    return str(lowering) if lowering else None


def _compact_canonical_op_for_compiler(op: Any) -> Dict[str, Any]:
    payload = _payload_for_prompt(op)
    if not isinstance(payload, dict):
        return {}
    args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
    shared_arguments = (
        args.get("shared_arguments")
        if isinstance(args.get("shared_arguments"), dict)
        else {}
    )
    return {
        "op_id": payload.get("op_id"),
        "lowering_family": _canonical_lowering_family_from_op(payload),
        "identity_role": args.get("identity_role") or payload.get("identity_role"),
        "runtime_policy": args.get("runtime_policy") or payload.get("runtime_policy"),
        "output_shape_delta": (
            args.get("output_shape_delta")
            or shared_arguments.get("output_shape_delta")
            or {}
        ),
        "target_invariants": list(shared_arguments.get("target_invariants") or [])[:12],
        "unresolved_variation_axes": list(
            shared_arguments.get("unresolved_variation_axes") or []
        ),
    }


def _compact_program_envelope_summary(envelope: Any) -> Dict[str, Any]:
    payload = _payload_for_prompt(envelope)
    if not isinstance(payload, dict) or not payload:
        return {}
    action_bundle = payload.get("action_bundle") if isinstance(payload.get("action_bundle"), dict) else {}
    target_contract = (
        payload.get("target_effect_contract")
        if isinstance(payload.get("target_effect_contract"), dict)
        else {}
    )
    return {
        "schema_version": payload.get("schema_version"),
        "source_antipattern_count": len(payload.get("source_antipatterns") or []),
        "target_effect_count": len(payload.get("target_effects") or []),
        "lowering_branch_count": len(payload.get("lowering_branches") or []),
        "runtime_branch_count": len(payload.get("runtime_branches") or []),
        "action_bundle_op_count": len(action_bundle.get("ops") or []),
        "target_invariants": _string_list(
            payload.get("target_invariants") or target_contract.get("target_invariants"),
            limit=12,
        ),
        "required_role_slots": _string_list(payload.get("required_role_slots"), limit=12),
        "negative_guards": _string_list(payload.get("negative_guards"), limit=12),
        "repair_insight_signature": _compact_repair_insight_signature(
            payload.get("repair_insight_signature")
        ),
    }


def _compact_synthesized_program_for_compiler(program: Any) -> Dict[str, Any]:
    payload = _payload_for_prompt(program)
    if not isinstance(payload, dict):
        return {}
    return {
        "program_id": payload.get("program_id"),
        "ops": [
            _compact_canonical_op_for_compiler(op)
            for op in (payload.get("ops") or [])
        ],
        "target_invariants": _string_list(payload.get("target_invariants"), limit=12),
        "unresolved_variation_axes": _string_list(
            payload.get("unresolved_variation_axes"), limit=12
        ),
        "shared_invariants": _string_list(payload.get("shared_invariants"), limit=12),
        "repair_insight_signature": _compact_repair_insight_signature(
            payload.get("repair_insight_signature")
            or _payload_for_prompt(payload.get("program_envelope") or {}).get("repair_insight_signature")
        ),
        "program_envelope_summary": _compact_program_envelope_summary(
            payload.get("program_envelope") or {}
        ),
        "unsupported_ops": _string_list(payload.get("unsupported_ops"), limit=12),
    }


def _compact_repair_insight_signature(insight: Any) -> Dict[str, Any]:
    payload = _payload_for_prompt(insight)
    if not isinstance(payload, dict):
        return {}
    return {
        "interface_key": _short_text(payload.get("interface_key"), 160),
        "source_misread": _short_text(payload.get("source_misread"), 320),
        "target_preference": _short_text(payload.get("target_preference"), 320),
        "repair_interface": _short_text(payload.get("repair_interface"), 320),
        "binding_slots": _dict_list(payload.get("binding_slots"), limit=8),
        "preserve_invariants": _string_list(payload.get("preserve_invariants"), limit=10),
        "negative_guards": _string_list(payload.get("negative_guards"), limit=10),
        "axis_links": _dict_list(payload.get("axis_links"), limit=8),
        "confidence": payload.get("confidence"),
    }


def _compact_case_audit_for_extractor(case_audit: Any) -> Dict[str, Any]:
    """Extractor needs the audited repair, not a second full gold-bearing case."""
    payload = _payload_for_prompt(case_audit)
    if not isinstance(payload, dict):
        return {}
    return {
        "db_id": payload.get("db_id"),
        "case_id": payload.get("case_id"),
        "final_error_reason": _short_text(payload.get("final_error_reason"), 600),
        "minimal_fix": _short_text(payload.get("minimal_fix"), 800),
        "candidate_fix_sql": payload.get("candidate_fix_sql") or payload.get("validated_sql"),
        "minimal_patch_ops": list(payload.get("minimal_patch_ops") or [])[:8],
        "effect_axis_hint": payload.get("effect_axis_hint"),
        "secondary_differences": list(payload.get("secondary_differences") or [])[:8],
        "validated_sql": payload.get("validated_sql"),
        "error_locus_hint": _enum_value(payload.get("error_locus_hint")),
        "confidence": _enum_value(payload.get("confidence")),
    }


def _compact_prompt_payload(payload: Any) -> Any:
    if isinstance(payload, list):
        return [_compact_prompt_payload(item) for item in payload]
    if not isinstance(payload, dict):
        return payload

    compacted = {str(key): _compact_prompt_payload(value) for key, value in payload.items()}
    compacted.pop("supporting_case_ids", None)
    compacted.pop("member_op_ids", None)
    if "canonical_program_op" in compacted:
        compacted["canonical_program_op"] = {"omitted_from_prompt": True}
    if "canonical_refs" in compacted and isinstance(compacted["canonical_refs"], list):
        compacted["canonical_refs"] = [
            _compact_role_ref(ref) for ref in compacted["canonical_refs"][:8]
        ]
    if "member_argument_variants" in compacted:
        compacted["member_argument_variants"] = {
            "omitted_from_prompt": True,
            "count": len(compacted.get("member_argument_variants") or []),
        }
    if isinstance(compacted.get("repair_insight_signature"), dict):
        compacted["repair_insight_signature"] = _compact_repair_insight_signature(
            compacted.get("repair_insight_signature")
        )
    if {"db_id", "tables", "columns_by_table"}.issubset(compacted):
        return _compact_local_schema_view_payload(compacted)
    if isinstance(compacted.get("local_schema_view"), dict):
        compacted["local_schema_view"] = _compact_local_schema_view_payload(
            compacted["local_schema_view"]
        )
    return compacted


def _short_text(value: Any, limit: int = 360) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "...[truncated]"


def _enum_value(value: Any) -> Any:
    if value is None:
        return None
    return getattr(value, "value", value)


def _enum_name(value: Any) -> str:
    return str(_enum_value(value) or "").strip().upper()


def _compact_slot_payload(slot: Any) -> Dict[str, Any]:
    payload = _payload_for_prompt(slot)
    if not isinstance(payload, dict):
        return {}
    return {
        "name": payload.get("name"),
        "kind": payload.get("kind"),
        "required": bool(payload.get("required", True)),
        "allowed_role_families": list(payload.get("allowed_role_families") or [])[:12],
        "description": _short_text(payload.get("description"), 180),
    }


def _compact_guardrail_payload(guardrail: Any) -> Dict[str, Any]:
    payload = _payload_for_prompt(guardrail)
    if not isinstance(payload, dict):
        return {}
    return {
        "kind": payload.get("kind"),
        "description": _short_text(payload.get("description"), 240),
    }


def _compact_branch_rule_payload(rule: Any) -> Dict[str, Any]:
    payload = _payload_for_prompt(rule)
    if not isinstance(payload, dict):
        return {}
    return {
        "if_condition": _short_text(payload.get("if_condition"), 240),
        "then_action": _short_text(payload.get("then_action"), 240),
        "else_action": _short_text(payload.get("else_action"), 180),
    }


def _compact_repair_skeleton_payload(skeleton: Any) -> Dict[str, Any]:
    payload = _payload_for_prompt(skeleton)
    if not isinstance(payload, dict):
        return {}
    structural = payload.get("structural") if isinstance(payload.get("structural"), dict) else {}
    semantic = payload.get("semantic") if isinstance(payload.get("semantic"), dict) else {}
    return {
        "structural": {
            "locus": _enum_value(structural.get("locus")),
            "op_family": _enum_value(structural.get("op_family")),
            "target_family": _enum_value(structural.get("target_family")),
            "output_contract": _enum_value(structural.get("output_contract")),
            "output_shape_delta": structural.get("output_shape_delta") or {},
        },
        "semantic": {
            "intent": _short_text(semantic.get("intent"), 300),
            "family_hint": _short_text(semantic.get("family_hint"), 180),
        },
    }


def _compact_runtime_case_view_payload(case_view: Any) -> Dict[str, Any]:
    payload = _payload_for_prompt(case_view)
    if not isinstance(payload, dict):
        return {}
    question_contract = (
        payload.get("question_contract")
        if isinstance(payload.get("question_contract"), dict)
        else {}
    )
    pred = (
        payload.get("pred_manifestation")
        if isinstance(payload.get("pred_manifestation"), dict)
        else {}
    )
    return {
        "db_id": payload.get("db_id"),
        "case_id": payload.get("case_id"),
        "question": payload.get("question"),
        "evidence": payload.get("evidence"),
        "question_contract": {
            "target_entity_roles": [
                _enum_value(item) for item in (question_contract.get("target_entity_roles") or [])
            ],
            "answer_slot_types": [
                _enum_value(item) for item in (question_contract.get("answer_slot_types") or [])
            ],
            "grain": _enum_value(question_contract.get("grain")),
            "operators": [_enum_value(item) for item in (question_contract.get("operators") or [])],
            "route_cues": [_enum_value(item) for item in (question_contract.get("route_cues") or [])],
            "specific_entity_mentions": list(question_contract.get("specific_entity_mentions") or [])[:12],
            "summary": _short_text(question_contract.get("summary"), 240),
        },
        "pred_manifestation": {
            "top1_sql": pred.get("top1_sql"),
            "tables": list(pred.get("tables") or []),
            "columns": list(pred.get("columns") or []),
            "select_shape": pred.get("select_shape"),
            "group_order_shape": pred.get("group_order_shape"),
            "manifestation_types": [
                _enum_value(item) for item in (pred.get("manifestation_types") or [])
            ],
            "structure_flags": pred.get("structure_flags") or {},
            "summary": _short_text(pred.get("summary"), 240),
        },
        "candidate_set_summary": payload.get("candidate_set_summary") or {},
        "local_schema_view": _compact_local_schema_view_payload(
            payload.get("local_schema_view") or {}
        ),
        "sql_role_graph": _compact_sql_role_graph_for_prompt(case_view),
    }


def _compact_sql_role_graph_for_prompt(case_view: Any) -> Dict[str, Any]:
    """Expose current SQL role facts needed for canonical action selection.

    This is answer-blind: it is derived only from the current pred SQL and local
    schema. Source/target repair graphs remain offline-only.
    """
    payload = _payload_for_prompt(case_view)
    if not isinstance(payload, dict):
        return {}
    pred = payload.get("pred_manifestation") if isinstance(payload.get("pred_manifestation"), dict) else {}
    sql = str(pred.get("top1_sql") or "")
    schema_view = getattr(case_view, "local_schema_view", None)
    if not sql or schema_view is None:
        return {}
    try:
        from .role_graph_normalizer_v2 import RoleGraphNormalizer

        graph = RoleGraphNormalizer().normalize_sql(
            sql=sql,
            schema_view=schema_view,
            source="pred_sql",
        )
    except Exception:
        return {}

    def refs(section: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for ref in (graph.get(section) or [])[:12]:
            compact = _compact_role_ref(ref)
            if compact:
                rows.append(compact)
        return rows

    return {
        "output_refs": refs("output_refs"),
        "join_refs": refs("join_refs"),
        "predicate_refs": refs("predicate_refs"),
        "table_relation_roles": {
            str(table): list(roles)[:8] if isinstance(roles, list) else roles
            for table, roles in (graph.get("table_relation_roles") or {}).items()
        },
    }


def _collect_schema_refs(value: Any) -> Tuple[Set[str], Set[Tuple[str, str]]]:
    tables: Set[str] = set()
    columns: Set[Tuple[str, str]] = set()

    def add_ref(table: Any, column: Any = None) -> None:
        table_text = str(table or "").strip()
        column_text = str(column or "").strip()
        if table_text:
            tables.add(table_text)
        if table_text and column_text:
            columns.add((table_text, column_text))

    def walk(item: Any) -> None:
        payload = _payload_for_prompt(item)
        if isinstance(payload, dict):
            table = payload.get("table") or payload.get("source_table") or payload.get("target_table")
            column = (
                payload.get("column")
                or payload.get("source_column")
                or payload.get("target_column")
            )
            if table:
                add_ref(table, column)
            for value in payload.values():
                walk(value)
            return
        if isinstance(payload, list):
            for value in payload:
                walk(value)
            return
        if not isinstance(payload, str):
            return
        for table, column in re.findall(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b",
            payload,
        ):
            add_ref(table, column)

    walk(value)
    return tables, columns


def _candidate_linked_schema_prompt_payload(
    *,
    runtime_case_view: Any,
    candidate_sets: List[Any],
) -> Dict[str, Any]:
    full_schema = _compact_local_schema_view_payload(
        _payload_for_prompt(getattr(runtime_case_view, "local_schema_view", None))
    )
    needed_tables: Set[str] = set()
    needed_columns: Set[Tuple[str, str]] = set()

    role_graph = _compact_sql_role_graph_for_prompt(runtime_case_view)
    for section in ("output_refs", "join_refs", "predicate_refs"):
        tables, columns = _collect_schema_refs(role_graph.get(section) or [])
        needed_tables.update(tables)
        needed_columns.update(columns)

    case_payload = _payload_for_prompt(runtime_case_view)
    pred = case_payload.get("pred_manifestation") if isinstance(case_payload, dict) else {}
    if isinstance(pred, dict):
        needed_tables.update(str(table) for table in (pred.get("tables") or []) if str(table))

    for candidate_set in candidate_sets or []:
        for candidate in getattr(candidate_set, "candidates", []) or []:
            tables, columns = _collect_schema_refs(getattr(candidate, "arguments", {}) or {})
            needed_tables.update(tables)
            needed_columns.update(columns)

    if not needed_tables:
        return {
            **full_schema,
            "source": {
                **(full_schema.get("source") or {}),
                "prompt_scope": "full_schema_fallback_no_candidate_refs",
            },
        }

    needed_lower = {table.lower() for table in needed_tables}
    columns_by_table = {
        table: columns
        for table, columns in (full_schema.get("columns_by_table") or {}).items()
        if table.lower() in needed_lower
    }
    edges = []
    for edge in full_schema.get("pk_fk_edges") or []:
        edge_payload = _payload_for_prompt(edge)
        source = str(edge_payload.get("source") or "")
        target = str(edge_payload.get("target") or "")
        source_table = source.split(".", 1)[0].lower() if "." in source else source.lower()
        target_table = target.split(".", 1)[0].lower() if "." in target else target.lower()
        if source_table in needed_lower or target_table in needed_lower:
            edges.append(edge_payload)

    column_lower = {
        (table.lower(), column.lower())
        for table, column in needed_columns
        if table and column
    }
    hints = []
    for hint in full_schema.get("semantic_hints") or []:
        table = str(hint.get("table") or "")
        column = str(hint.get("column") or "")
        if table.lower() not in needed_lower:
            continue
        if column_lower and (table.lower(), column.lower()) not in column_lower:
            continue
        hints.append(hint)

    return {
        "db_id": full_schema.get("db_id"),
        "tables": [table for table in full_schema.get("tables") or [] if str(table).lower() in needed_lower],
        "columns_by_table": columns_by_table,
        "pk_fk_edges": edges[:80],
        "semantic_hints": hints[:80],
        "source": {
            **(full_schema.get("source") or {}),
            "prompt_scope": "candidate_linked_schema",
            "table_count_before": len(full_schema.get("tables") or []),
            "table_count_after": len(columns_by_table),
        },
    }


def _compact_repair_step_contract(step: Any) -> Dict[str, Any]:
    payload = _payload_for_prompt(step)
    if not isinstance(payload, dict):
        return {}
    return {
        "step_id": payload.get("step_id"),
        "op": payload.get("op"),
        "locus": payload.get("locus"),
        "is_dependency": bool(payload.get("is_dependency") or False),
        "required": bool(payload.get("required", True)),
        "slots": [
            _compact_slot_payload(slot)
            for slot in (payload.get("slots") or [])[:8]
        ],
        "guards": [
            _compact_guardrail_payload(guard)
            for guard in (payload.get("guards") or [])[:8]
        ],
    }


def _compact_trigger_contract_for_compiler(contract: Any) -> Dict[str, Any]:
    payload = _payload_for_prompt(contract)
    if not isinstance(payload, dict):
        return {}
    action = payload.get("action_contract") if isinstance(payload.get("action_contract"), dict) else {}
    return {
        "schema_version": payload.get("schema_version"),
        "max_actions": payload.get("max_actions"),
        "required_signals": _string_list(payload.get("required_signals"), limit=20),
        "decisive_pred_signals": _string_list(payload.get("decisive_pred_signals"), limit=20),
        "negative_signals": _string_list(payload.get("negative_signals"), limit=20),
        "variant_required_signal_sets": [
            _string_list(signal_set, limit=12)
            for signal_set in (payload.get("variant_required_signal_sets") or [])[:6]
        ],
        "canonical_discriminants": _string_list(
            payload.get("canonical_discriminants"), limit=20
        ),
        "trigger_policy": {
            "allow_out_of_variant_generalization": (
                (payload.get("trigger_policy") or {}).get("allow_out_of_variant_generalization")
                if isinstance(payload.get("trigger_policy"), dict)
                else None
            ),
            "min_canonical_discriminants": (
                (payload.get("trigger_policy") or {}).get("min_canonical_discriminants")
                if isinstance(payload.get("trigger_policy"), dict)
                else None
            ),
            "requires_binder_dry_run": (
                (payload.get("trigger_policy") or {}).get("requires_binder_dry_run")
                if isinstance(payload.get("trigger_policy"), dict)
                else None
            ),
        },
        "action_contract": {
            "locus": action.get("locus"),
            "op_family": action.get("op_family"),
            "target_family": action.get("target_family"),
            "output_shape_delta": action.get("output_shape_delta") or {},
            "answer_unit_contract": action.get("answer_unit_contract") or {},
            "slot_kinds": _string_list(action.get("slot_kinds"), limit=12),
            "selection_policy": action.get("selection_policy"),
            "compiler_deterministic": action.get("compiler_deterministic"),
            "lowering_families": _string_list(action.get("lowering_families"), limit=12),
            "required_role_slots": _string_list(action.get("required_role_slots"), limit=12),
            "required_target_invariants": _string_list(
                action.get("required_target_invariants"), limit=12
            ),
            "program_envelope_summary": _compact_program_envelope_summary(
                action.get("program_envelope") or {}
            ),
        },
    }


def _compact_trigger_signature_for_compiler(signature: Any) -> Dict[str, Any]:
    payload = _payload_for_prompt(signature)
    if not isinstance(payload, dict):
        return {}
    return {
        "required_question_tags": list(payload.get("required_question_tags") or [])[:20],
        "required_pred_tags": list(payload.get("required_pred_tags") or [])[:20],
        "decisive_antipatterns": list(payload.get("decisive_antipatterns") or [])[:20],
        "negative_evidence": list(payload.get("negative_evidence") or [])[:20],
    }


def _trigger_audit_by_group(trigger_result: Any) -> Dict[str, Dict[str, Any]]:
    payload = _payload_for_prompt(trigger_result)
    if not isinstance(payload, dict):
        return {}
    rows: Dict[str, Dict[str, Any]] = {}
    for audit in payload.get("candidates") or []:
        item = _payload_for_prompt(audit)
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("group_id") or "")
        if not group_id:
            continue
        rows[group_id] = {
            "gate_passed": bool(item.get("gate_passed")),
            "gate_reasons": list(item.get("gate_reasons") or [])[:12],
            "required_signal_hits": list(item.get("required_signal_hits") or [])[:20],
            "required_signal_misses": list(item.get("required_signal_misses") or [])[:20],
            "negative_signal_hits": list(item.get("negative_signal_hits") or [])[:20],
            "optional_signal_hits": list(item.get("optional_signal_hits") or [])[:20],
            "variant_required_match": bool(item.get("variant_required_match")),
            "canonical_discriminant_hits": list(
                item.get("canonical_discriminant_hits") or []
            )[:20],
            "binder_dry_run_success": bool(item.get("binder_dry_run_success")),
        }
    return rows


def _memory_objects_prompt_payload(
    memory_objects: List[Any],
    *,
    trigger_result: Any = None,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    trigger_audits = _trigger_audit_by_group(trigger_result)
    for group in memory_objects or []:
        payload = _payload_for_prompt(group)
        if not isinstance(payload, dict):
            continue
        group_id = payload.get("group_id")
        core = payload.get("core_interface") if isinstance(payload.get("core_interface"), dict) else {}
        instantiation = (
            payload.get("instantiation_program")
            if isinstance(payload.get("instantiation_program"), dict)
            else {}
        )
        rows.append(
            {
                "group_id": group_id,
                "group_type": _enum_value(payload.get("group_type")),
                "db_id": payload.get("db_id"),
                "runtime_usable": payload.get("runtime_usable"),
                "core_interface": {
                    "repair_goal": _short_text(core.get("repair_goal"), 360),
                },
                "instantiation_program": {
                    "shared": instantiation.get("shared"),
                    "template_omitted_from_prompt": bool(instantiation.get("template")),
                    "slots": [
                        _compact_slot_payload(slot)
                        for slot in (instantiation.get("slots") or [])[:12]
                    ],
                    "branch_rules": [
                        _compact_branch_rule_payload(rule)
                        for rule in (instantiation.get("branch_rules") or [])[:8]
                    ],
                    "repair_program": [
                        _compact_repair_step_contract(step)
                        for step in (instantiation.get("repair_program") or [])[:8]
                    ],
                    "synthesized_program": _compact_synthesized_program_for_compiler(
                        instantiation.get("synthesized_program")
                    ),
                },
                "trigger_contract_omitted_from_compiler_prompt": True,
                "trigger_signature_omitted_from_compiler_prompt": True,
                "trigger_match_summary": trigger_audits.get(str(group_id or ""), {}),
                "repair_insight_signature": _compact_repair_insight_signature(
                    (payload.get("formation_signals") or {}).get("repair_insight_signature")
                    or ((payload.get("trigger_contract") or {}).get("action_contract") or {}).get("repair_insight_signature")
                ),
            }
        )
    return rows


def _compact_memory_alignment_payload(alignment: Any) -> Dict[str, Any]:
    payload = _payload_for_prompt(alignment)
    if not isinstance(payload, dict):
        return {}
    keep_keys = {
        "score",
        "pair_mentioned",
        "source_mentioned",
        "target_mentioned",
        "memory_mentioned_count",
        "current_select_cooccurrence_count",
        "question_column_hit_count",
        "question_table_hit_count",
    }
    out = {key: payload.get(key) for key in keep_keys if key in payload}
    for key in (
        "current_select_cooccurrences",
        "question_hit_columns",
        "question_hit_tables",
        "keep_terms_in_keep_side",
        "keep_terms_in_drop_side",
        "drop_terms_in_drop_side",
        "drop_terms_in_keep_side",
    ):
        if key in payload:
            out[key] = list(payload.get(key) or [])[:8]
    return out


_ACTION_COMPILER_PROMPT_ARGUMENT_KEYS = {
    "location",
    "predicate_index",
    "predicate_ref",
    "side_index",
    "side_indexes",
    "keep_side_indexes",
    "drop_binding_source",
    "drop_reason",
    "from_expr",
    "from_exprs",
    "source_slot_index",
    "source_slot_indexes",
    "drop_count",
    "replace_count",
    "target_slot_count",
    "target_columns",
    "to_table",
    "to_column",
    "to_role_family",
    "replacement_scope",
    "from_predicate",
    "drop_condition",
    "drop_condition_refs",
    "keep_conditions",
    "keep_condition_refs",
    "to_predicate",
    "from_scope",
    "to_scope",
    "target_aggregate_expr",
    "preserve_denominator_scope",
    "bridge_table",
    "source_table_column",
    "target_table_column",
    "target_output_refs",
    "target_relation_edges",
    "reroute_reason",
    "hops",
    "source_grain",
    "target_grain",
    "source_anchor",
    "target_anchor",
    "aggregate_rewrite",
    "current_expr",
    "target_expr",
    "preserve_join_path",
    "ranking_expr",
    "metric_expr",
    "window_fn",
    "tie_policy",
    "target_order_by",
    "target_limit",
    "output_shape_delta",
    "required_edit_scopes",
    "compiled_from_program_id",
    "canonical_contract",
    "effect_contract",
    "canonical_ref_binding",
    "memory_alignment",
}


def _compact_action_candidate_arguments(args: Any) -> Dict[str, Any]:
    payload = _payload_for_prompt(args)
    if not isinstance(payload, dict):
        return {}
    out: Dict[str, Any] = {}
    for key in sorted(_ACTION_COMPILER_PROMPT_ARGUMENT_KEYS):
        if key not in payload:
            continue
        value = payload.get(key)
        if key == "memory_alignment":
            out[key] = _compact_memory_alignment_payload(value)
        elif key == "canonical_contract":
            contract = _payload_for_prompt(value)
            if isinstance(contract, dict):
                out[key] = {
                    "program_id": contract.get("program_id"),
                    "op_id": contract.get("op_id"),
                    "lowering_family": contract.get("lowering_family"),
                    "output_shape_delta": contract.get("output_shape_delta") or {},
                    "target_invariants": list(contract.get("target_invariants") or [])[:12],
                    "target_output_refs": list(contract.get("target_output_refs") or [])[:12],
                    "target_relation_edges": list(contract.get("target_relation_edges") or [])[:8],
                    "unresolved_variation_axes": list(
                        contract.get("unresolved_variation_axes") or []
                    ),
                }
        elif key == "effect_contract":
            contract = _payload_for_prompt(value)
            if isinstance(contract, dict):
                out[key] = {
                    "interface_key": _short_text(contract.get("interface_key"), 160),
                    "repair_interface": _short_text(contract.get("repair_interface"), 320),
                    "source_misread": _short_text(contract.get("source_misread"), 240),
                    "target_preference": _short_text(contract.get("target_preference"), 240),
                    "preserve_invariants": list(contract.get("preserve_invariants") or [])[:10],
                    "negative_guards": list(contract.get("negative_guards") or [])[:10],
                    "axis_links": list(contract.get("axis_links") or [])[:8],
                }
        elif isinstance(value, list):
            out[key] = value[:20]
        else:
            out[key] = value
    if "repair_program" in payload:
        out["repair_program_omitted_from_prompt"] = {
            "reason": "duplicated in memory_objects.instantiation_program; full steps stay on selected candidate for rewrite",
            "count": len(payload.get("repair_program") or []),
        }
    return out


def _compact_repair_program_steps_for_runtime_prompt(steps: Any) -> List[Dict[str, Any]]:
    if not isinstance(steps, list):
        return []
    rows: List[Dict[str, Any]] = []
    for step in steps[:6]:
        payload = _payload_for_prompt(step)
        if not isinstance(payload, dict):
            continue
        arguments = _payload_for_prompt(payload.get("arguments") or {})
        if not isinstance(arguments, dict):
            arguments = {}
        safe_arguments = {
            str(key): value
            for key, value in arguments.items()
            if str(key)
            not in {
                "canonical_refs",
                "canonical_arguments",
                "canonical_invariants",
                "canonical_op_type",
                "source_case_contract",
                "source_evidence",
                "member_argument_variants",
            }
        }
        rows.append(
            {
                "step_id": payload.get("step_id"),
                "op": payload.get("op"),
                "locus": payload.get("locus"),
                "is_dependency": bool(payload.get("is_dependency") or False),
                "required": bool(payload.get("required", True)),
                "slots": list(payload.get("slots") or [])[:8],
                "guards": list(payload.get("guards") or [])[:8],
                "arguments": safe_arguments,
            }
        )
    return rows


def _normalize_rewrite_expr(expr: Any) -> str:
    text = str(expr or "").strip()
    text = re.split(r"\s+as\s+", text, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    text = re.sub(r"\s+", " ", text)
    return text.strip("`\"[]").lower()


def _aliases_from_exprs(exprs: List[Any]) -> Set[str]:
    aliases: Set[str] = set()
    for expr in exprs or []:
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_$]*)\s*\.", str(expr or ""))
        if match:
            aliases.add(match.group(1).lower())
    return aliases


def _top_level_join_blocks(sql: str) -> List[Dict[str, Any]]:
    text = str(sql or "")
    pattern = re.compile(
        r"\s+JOIN\s+(?P<table>[A-Za-z_][A-Za-z0-9_$\.]*)(?:\s+(?:AS\s+)?(?P<alias>[A-Za-z_][A-Za-z0-9_$]*))?\s+ON\s+.*?(?=\s+(?:JOIN|WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|UNION|EXCEPT|INTERSECT)\b|$)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    blocks: List[Dict[str, Any]] = []
    for match in pattern.finditer(text):
        table = str(match.group("table") or "").strip()
        alias = str(match.group("alias") or table.split(".")[-1]).strip()
        if not table:
            continue
        blocks.append(
            {
                "table": table,
                "alias": alias,
                "sql": match.group(0).strip(),
                "span": [match.start(), match.end()],
            }
        )
    return blocks


def _alias_referenced_outside_span(
    sql: str,
    alias: str,
    span: List[int],
    *,
    dropped_select_exprs: Optional[List[str]] = None,
) -> bool:
    if not alias or len(span) != 2:
        return False
    text = str(sql or "")
    dropped = {
        _normalize_rewrite_expr(expr)
        for expr in (dropped_select_exprs or [])
        if _normalize_rewrite_expr(expr)
    }
    for expr in _selected_exprs(text):
        if _normalize_rewrite_expr(expr) in dropped:
            continue
        if re.search(rf"\b{re.escape(alias)}\s*\.", expr, flags=re.IGNORECASE):
            return True
    outside = text[: int(span[0])] + text[int(span[1]) :]
    bounds = _selected_clause_bounds(outside)
    if bounds:
        outside = outside[: bounds[0]] + outside[bounds[1] :]
    return bool(re.search(rf"\b{re.escape(alias)}\s*\.", outside, flags=re.IGNORECASE))


def _bound_join_blocks_for_aliases(
    sql: str,
    aliases: Set[str],
    *,
    dropped_select_exprs: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    wanted = {alias.lower() for alias in aliases if alias}
    for block in _top_level_join_blocks(sql):
        alias = str(block.get("alias") or "").lower()
        table_tail = str(block.get("table") or "").split(".")[-1].lower()
        if alias not in wanted and table_tail not in wanted:
            continue
        span = list(block.get("span") or [])
        rows.append(
            {
                "table": block.get("table"),
                "alias": block.get("alias"),
                "sql": block.get("sql"),
                "external_reference_found": _alias_referenced_outside_span(
                    sql,
                    alias,
                    span,
                    dropped_select_exprs=dropped_select_exprs,
                ),
                "external_reference_policy": (
                    "References inside this JOIN block's own ON clause are not "
                    "external dependencies; fail only if the alias is referenced "
                    "outside the block after applying primary edits."
                ),
            }
        )
    return rows


def _selected_clause_bounds(sql: str) -> Optional[tuple[int, int]]:
    match = re.search(
        r"\bselect\b\s+(?:distinct\s+)?(.+?)\s+\bfrom\b",
        str(sql or ""),
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    return match.span(1)


def _rewrite_contract_prompt_payload(
    *,
    actions: List[Any],
    current_sql: str,
    natural_language_hint: str = "",
) -> Dict[str, Any]:
    contract: Dict[str, Any] = {
        "schema_version": "rewrite-contract-v1",
        "boundary": (
            "Rewrite is an executor. Triggering, memory selection, branch "
            "selection, candidate selection, and argument binding are already done."
        ),
        "current_sql_must_be_rewritten": True,
        "primary_edits": [],
        "dependency_edits": [],
        "allowed_scopes": [],
        "required_absence_checks": [],
        "preserve_constraints": [
            "Preserve predicates, literals, grouping, ordering, and joins unless an edit below explicitly changes them.",
        ],
        "failure_conditions": [
            "A required edit target is absent from current_sql.",
            "Applying an edit would touch a scope not listed in allowed_scopes.",
            "Applying an edit would violate preserve_constraints.",
        ],
        "natural_language_hint": _short_text(natural_language_hint, 320),
    }
    allowed_scopes: Set[str] = set()
    selected_exprs = _selected_exprs(current_sql)
    normalized_current_exprs = {
        _normalize_rewrite_expr(expr): expr for expr in selected_exprs if _normalize_rewrite_expr(expr)
    }
    for action in actions or []:
        payload = _payload_for_prompt(action)
        if not isinstance(payload, dict):
            continue
        action_id = str(payload.get("action_id") or "")
        primitive = _enum_name(payload.get("primitive"))
        args = _payload_for_prompt(payload.get("arguments") or {})
        if not isinstance(args, dict):
            args = {}
        scopes = {
            _enum_name(scope)
            for scope in (payload.get("allowed_edit_scope") or args.get("required_edit_scopes") or [])
            if _enum_name(scope)
        }
        allowed_scopes.update(scopes)
        if primitive == "DROP_SELECT_SLOT":
            from_exprs = [
                str(expr).strip()
                for expr in (args.get("from_exprs") or [args.get("from_expr")])
                if str(expr or "").strip()
            ]
            bound_exprs = [
                normalized_current_exprs.get(_normalize_rewrite_expr(expr), str(expr).strip())
                for expr in from_exprs
                if _normalize_rewrite_expr(expr)
            ]
            contract["primary_edits"].append(
                {
                    "action_id": action_id,
                    "primitive": primitive,
                    "edit": "remove_select_expressions",
                    "scope": "SELECT",
                    "required": True,
                    "bound_expressions": bound_exprs,
                    "expected_drop_count": args.get("drop_count") or len(bound_exprs),
                    "selected_candidate_id": payload.get("selected_candidate_id"),
                    "binding_status": "bound" if bound_exprs else "unbound",
                }
            )
            for expr in bound_exprs:
                contract["required_absence_checks"].append(
                    {
                        "action_id": action_id,
                        "scope": "SELECT",
                        "text": expr,
                        "reason": "removed select expression must not remain in rewrite_sql",
                    }
                )
            aliases = _aliases_from_exprs(bound_exprs or from_exprs)
            for step in args.get("repair_program") or []:
                step_payload = _payload_for_prompt(step)
                if not isinstance(step_payload, dict):
                    continue
                op = str(step_payload.get("op") or "").strip().upper()
                if op in {"JOIN_DROP", "JOIN_DROP_TABLE", "DROP_JOIN", "DROP_JOIN_TABLE"}:
                    join_blocks = _bound_join_blocks_for_aliases(
                        current_sql,
                        aliases,
                        dropped_select_exprs=bound_exprs or from_exprs,
                    )
                    contract["dependency_edits"].append(
                        {
                            "action_id": action_id,
                            "step_id": step_payload.get("step_id"),
                            "op": op,
                            "edit": "remove_join_blocks",
                            "scope": "JOIN",
                            "required": bool(step_payload.get("required", True)),
                            "bound_join_blocks": join_blocks,
                            "binding_status": "bound" if join_blocks else "unbound",
                        }
                    )
                    for block in join_blocks:
                        if str(block.get("sql") or ""):
                            contract["required_absence_checks"].append(
                                {
                                    "action_id": action_id,
                                    "scope": "JOIN",
                                    "text": block.get("sql"),
                                    "reason": "removed join block must not remain in rewrite_sql",
                                }
                            )
                else:
                    contract["dependency_edits"].append(
                        {
                            "action_id": action_id,
                            "step_id": step_payload.get("step_id"),
                            "op": op,
                            "edit": "apply_explicit_dependency_step",
                            "scope": step_payload.get("locus"),
                            "required": bool(step_payload.get("required", True)),
                            "arguments": _payload_for_prompt(step_payload.get("arguments") or {}),
                            "binding_status": "provided_by_action_contract",
                        }
                    )
        else:
            contract["primary_edits"].append(
                {
                    "action_id": action_id,
                    "primitive": primitive,
                    "edit": "apply_bound_action_arguments",
                    "required": True,
                    "arguments": _compact_action_candidate_arguments(args),
                    "selected_candidate_id": payload.get("selected_candidate_id"),
                    "binding_status": "provided_by_action_contract",
                }
            )
    contract["allowed_scopes"] = sorted(allowed_scopes)
    return contract


def _rewrite_schema_context_prompt_payload(
    *,
    local_schema_view: Any,
    rewrite_contract: Dict[str, Any],
) -> Dict[str, Any]:
    _ = local_schema_view
    tables: Set[str] = set()
    aliases: Set[str] = set()
    for edit in rewrite_contract.get("dependency_edits") or []:
        for block in edit.get("bound_join_blocks") or []:
            if str(block.get("table") or ""):
                tables.add(str(block.get("table")))
            if str(block.get("alias") or ""):
                aliases.add(str(block.get("alias")))
    return {
        "policy": "minimal_bound_context",
        "reason": "Rewrite contract is bound to current SQL; full schema is omitted unless an edit needs new schema objects.",
        "referenced_tables": sorted(tables),
        "referenced_aliases": sorted(aliases),
    }


def _prompt_payload_audit(payloads: Dict[str, Any]) -> Dict[str, Any]:
    rows: Dict[str, Any] = {}
    for name, payload in payloads.items():
        text = _json_dump_prompt(payload)
        rows[name] = {
            "chars": len(text),
            "top_level_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
            "sha1": hashlib.sha1(text.encode("utf-8")).hexdigest()[:12],
        }
    return rows


def _runtime_action_prompt_payload(action: Any) -> Dict[str, Any]:
    payload = _payload_for_prompt(action)
    if not isinstance(payload, dict):
        return {}
    arguments = _payload_for_prompt(payload.get("arguments") or {})
    if not isinstance(arguments, dict):
        arguments = {}
    compact_args = _compact_action_candidate_arguments(arguments)
    repair_steps = _compact_repair_program_steps_for_runtime_prompt(
        arguments.get("repair_program") or []
    )
    if repair_steps:
        compact_args["repair_program"] = repair_steps
    allowed_scope = [
        _enum_value(scope) for scope in (payload.get("allowed_edit_scope") or [])
    ]
    allows_select = "SELECT" in {str(scope).upper() for scope in allowed_scope}
    has_select_dependency = any(
        str(step.get("locus") or "").upper() == "SELECT"
        for step in repair_steps
    )
    if not allows_select and not has_select_dependency:
        compact_args.pop("target_output_refs", None)
        contract = compact_args.get("canonical_contract")
        if isinstance(contract, dict):
            contract.pop("target_output_refs", None)
    return {
        "action_id": payload.get("action_id"),
        "source_group_id": payload.get("source_group_id"),
        "source_group_type": _enum_value(payload.get("source_group_type")),
        "primitive": _enum_value(payload.get("primitive")),
        "arguments": compact_args,
        "allowed_edit_scope": allowed_scope,
        "risk": _enum_value(payload.get("risk")),
        "priority": payload.get("priority"),
        "selected_candidate_id": payload.get("selected_candidate_id"),
    }


def _runtime_actions_prompt_payload(actions: List[Any]) -> List[Dict[str, Any]]:
    return [
        payload
        for payload in (_runtime_action_prompt_payload(action) for action in actions or [])
        if payload
    ]


def _repair_program_steps_from_actions(actions: List[Any]) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    for action in actions or []:
        payload = _payload_for_prompt(action)
        if not isinstance(payload, dict):
            continue
        arguments = _payload_for_prompt(payload.get("arguments") or {})
        if not isinstance(arguments, dict):
            continue
        for step in arguments.get("repair_program") or []:
            step_payload = _payload_for_prompt(step)
            if isinstance(step_payload, dict):
                steps.append(step_payload)
    return steps


def _contract_dependency_steps(actions: List[Any], op_name: str) -> List[Dict[str, Any]]:
    wanted = str(op_name or "").strip().upper()
    return [
        step
        for step in _repair_program_steps_from_actions(actions)
        if str(step.get("op") or "").strip().upper() == wanted
        and bool(step.get("is_dependency") or False)
    ]


def _has_contract_dependency_step(actions: List[Any], op_name: str) -> bool:
    return bool(_contract_dependency_steps(actions, op_name))


def _action_has_allowed_scope(actions: List[Any], scope: str) -> bool:
    wanted = str(scope or "").strip().upper()
    for action in actions or []:
        payload = _payload_for_prompt(action)
        if not isinstance(payload, dict):
            continue
        scopes = {_enum_name(item) for item in (payload.get("allowed_edit_scope") or [])}
        if wanted in scopes:
            return True
    return False


def _selected_exprs(sql: str) -> List[str]:
    match = re.search(r"\bselect\b\s+(?:distinct\s+)?(.+?)\s+\bfrom\b", str(sql or ""), re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    segment = match.group(1)
    exprs: List[str] = []
    current: List[str] = []
    depth = 0
    for char in segment:
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        if char == "," and depth == 0:
            expr = "".join(current).strip()
            if expr:
                exprs.append(expr)
            current = []
            continue
        current.append(char)
    expr = "".join(current).strip()
    if expr:
        exprs.append(expr)
    return exprs


def _expr_is_identifier_like(expr: str) -> bool:
    text = str(expr or "").strip().strip('"`[]').lower()
    if not text:
        return False
    if "(" in text:
        return False
    column = text.rsplit(".", 1)[-1].strip('"`[]')
    return column == "id" or column.endswith("_id") or column.endswith("id") or "uuid" in column


def _select_has_visible_duplicate_risk(sql: str) -> bool:
    text = str(sql or "")
    if not re.search(r"\bjoin\b", text, flags=re.IGNORECASE):
        return False
    if re.search(r"\bgroup\s+by\b", text, flags=re.IGNORECASE):
        return False
    if re.search(r"\b(count|sum|avg|min|max)\s*\(", text, flags=re.IGNORECASE):
        return False
    exprs = _selected_exprs(text)
    if not exprs:
        return False
    return any(not _expr_is_identifier_like(expr) for expr in exprs)


def _has_bound_select_repair(actions: List[Any]) -> bool:
    select_primitives = {
        "ADD_SELECT_SLOT",
        "REPLACE_SELECT_SLOT",
        "DROP_SELECT_SLOT",
        "DROP_SIDE",
    }
    for action in actions or []:
        payload = _payload_for_prompt(action)
        if not isinstance(payload, dict):
            continue
        primitive = _enum_name(payload.get("primitive"))
        if primitive not in select_primitives:
            continue
        arguments = _payload_for_prompt(payload.get("arguments") or {})
        if not isinstance(arguments, dict):
            continue
        if any(arguments.get(key) for key in ("from_expr", "from_exprs", "target_columns")):
            return True
    return False


def _top_level_sql_for_alias_scan(sql: str) -> str:
    text = str(sql or "")
    chars: List[str] = []
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
            chars.append(" ")
            continue
        if char == ")" and depth:
            depth -= 1
            chars.append(" ")
            continue
        chars.append(char if depth == 0 else " ")
    return "".join(chars)


def _sql_alias_map(sql: str) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    reserved_aliases = {
        "JOIN",
        "INNER",
        "LEFT",
        "RIGHT",
        "FULL",
        "CROSS",
        "OUTER",
        "NATURAL",
        "ON",
        "WHERE",
        "GROUP",
        "ORDER",
        "HAVING",
        "LIMIT",
        "UNION",
        "INTERSECT",
        "EXCEPT",
    }
    pattern = re.compile(
        r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_\"`\[\].]*)"
        r"(?:\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*))?",
        re.IGNORECASE,
    )
    for match in pattern.finditer(_top_level_sql_for_alias_scan(sql)):
        table = match.group(1).strip('"`[]')
        alias = (match.group(2) or table).strip('"`[]')
        if alias.upper() in reserved_aliases:
            alias = table
        if table:
            key = table.lower()
            if key not in aliases or (aliases[key].lower() == key and alias.lower() != key):
                aliases[key] = alias
    return aliases


def _alias_to_table_map(sql: str) -> Dict[str, str]:
    return {alias.lower(): table for table, alias in _sql_alias_map(sql).items()}


def _unique_top_level_table_alias_map(sql: str) -> Dict[str, str]:
    reserved = {
        "join",
        "inner",
        "left",
        "right",
        "full",
        "cross",
        "outer",
        "natural",
        "on",
        "where",
        "group",
        "order",
        "having",
        "limit",
        "union",
        "intersect",
        "except",
    }
    table_aliases: Dict[str, set[str]] = {}
    pattern = re.compile(
        r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_\"`\[\].]*)"
        r"(?:\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*))?",
        re.IGNORECASE,
    )
    for match in pattern.finditer(_top_level_sql_for_alias_scan(sql)):
        table = match.group(1).strip('"`[]').rsplit(".", 1)[-1]
        alias = (match.group(2) or table).strip('"`[]')
        if not table or alias.lower() in reserved:
            alias = table
        table_aliases.setdefault(table.lower(), set()).add(alias)
    out: Dict[str, str] = {}
    for table, aliases in table_aliases.items():
        if len({alias.lower() for alias in aliases}) != 1:
            continue
        alias = next(iter(aliases))
        if alias.lower() != table:
            out[table] = alias
    return out


def _deep_alias_to_table_map(sql: str) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    reserved = {
        "join",
        "inner",
        "left",
        "right",
        "full",
        "cross",
        "outer",
        "natural",
        "on",
        "where",
        "group",
        "order",
        "having",
        "limit",
        "union",
        "intersect",
        "except",
    }
    pattern = re.compile(
        r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_\"`\[\].]*)"
        r"(?:\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*))?",
        re.IGNORECASE,
    )
    for match in pattern.finditer(str(sql or "")):
        table = match.group(1).strip('"`[]').rsplit(".", 1)[-1]
        alias = (match.group(2) or table).strip('"`[]')
        if not table or alias.lower() in reserved:
            alias = table
        aliases.setdefault(alias.lower(), table)
        aliases.setdefault(table.lower(), table)
    return aliases


def _is_top_level_offset(text: str, offset: int) -> bool:
    depth = 0
    for char in str(text or "")[:offset]:
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
    return depth == 0


def _replace_top_level_qualified_ref(
    *,
    text: str,
    qualifier: str,
    column: str,
    replacement: str,
) -> str:
    pattern = re.compile(
        rf"\b{re.escape(qualifier)}\.{re.escape(column)}\b",
        flags=re.IGNORECASE,
    )
    return pattern.sub(
        lambda match: replacement if _is_top_level_offset(text, match.start()) else match.group(0),
        text,
    )


def _split_top_level_select_clause(sql: str) -> Optional[tuple[str, str, str]]:
    text = str(sql or "")
    select_match = _top_level_keyword_match(text, r"\bselect\b")
    from_match = _top_level_keyword_match(text, r"\bfrom\b")
    if not select_match or not from_match or from_match.start() <= select_match.end():
        return None
    return text[: select_match.end()], text[select_match.end() : from_match.start()], text[from_match.start() :]


def _replace_qualified_ref_outside_quotes_and_subqueries(
    *,
    text: str,
    qualifier: str,
    column: str,
    replacement_qualifier: str,
) -> tuple[str, bool]:
    pattern = re.compile(
        rf"\b{re.escape(qualifier)}\.{re.escape(column)}\b",
        flags=re.IGNORECASE,
    )
    out: List[str] = []
    i = 0
    depth = 0
    quote: Optional[str] = None
    changed = False
    while i < len(text):
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == quote:
                if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                    out.append(text[i + 1])
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in {"'", '"', "`"}:
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "[":
            quote = "]"
            out.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
            out.append(ch)
            i += 1
            continue
        if ch == ")" and depth:
            depth -= 1
            out.append(ch)
            i += 1
            continue
        match = pattern.match(text, i)
        if match and depth == 0:
            out.append(f"{replacement_qualifier}.{column}")
            i = match.end()
            changed = True
            continue
        out.append(ch)
        i += 1
    return "".join(out), changed


def _action_bound_select_target_refs(actions: List[Any]) -> List[Dict[str, str]]:
    refs: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for action in actions or []:
        payload = _payload_for_prompt(action)
        if not isinstance(payload, dict):
            continue
        scopes = {_enum_name(item) for item in (payload.get("allowed_edit_scope") or [])}
        if "SELECT" not in scopes:
            continue
        primitive = _enum_name(payload.get("primitive"))
        if primitive not in {"ADD_SELECT_SLOT", "REPLACE_SELECT_SLOT"}:
            continue
        args = _payload_for_prompt(payload.get("arguments") or {})
        if not isinstance(args, dict):
            continue
        for item in list(args.get("target_output_refs") or []) + list(args.get("target_columns") or []):
            row = _payload_for_prompt(item)
            if not isinstance(row, dict):
                continue
            table = str(row.get("table") or row.get("target_table") or "").strip()
            column = str(row.get("column") or row.get("target_column") or "").strip()
            if not table or not column:
                continue
            key = (table.lower(), column.lower())
            if key in seen:
                continue
            seen.add(key)
            refs.append({"table": table, "column": column})
    return refs


def _split_top_level_where(sql: str) -> Optional[tuple[str, str, str]]:
    text = str(sql or "")
    where_match = next(
        (
            match
            for match in re.finditer(r"\bwhere\b", text, flags=re.IGNORECASE)
            if _is_top_level_offset(text, match.start())
        ),
        None,
    )
    if not where_match:
        return None
    after_where = text[where_match.end():]
    boundary_match = next(
        (
            match
            for match in re.finditer(
                r"\b(group\s+by|order\s+by|having|limit|union|intersect|except)\b",
                after_where,
                flags=re.IGNORECASE,
            )
            if _is_top_level_offset(after_where, match.start())
        ),
        None,
    )
    if boundary_match:
        return (
            text[:where_match.end()],
            after_where[:boundary_match.start()],
            after_where[boundary_match.start():],
        )
    return text[:where_match.end()], after_where, ""


def _rewrite_alias_refs_to_tables(text: str, sql: str) -> str:
    rewritten = str(text or "")
    for alias, table in _alias_to_table_map(sql).items():
        rewritten = re.sub(
            rf"\b{re.escape(alias)}\.([A-Za-z_][A-Za-z0-9_]*)\b",
            lambda match, table=table: f"{table}.{match.group(1)}",
            rewritten,
            flags=re.IGNORECASE,
        )
    return rewritten


def _rewrite_alias_refs_to_tables_deep(text: str, sql: str) -> str:
    rewritten = str(text or "")
    for alias, table in _deep_alias_to_table_map(sql).items():
        rewritten = re.sub(
            rf"\b{re.escape(alias)}\.([A-Za-z_][A-Za-z0-9_]*)\b",
            lambda match, table=table: f"{table}.{match.group(1)}",
            rewritten,
            flags=re.IGNORECASE,
        )
    return rewritten


def _compact_sql_fragment(text: str) -> str:
    return re.sub(r"[\s`\"\[\]]+", "", str(text or "").lower())


def _target_predicate_rows_from_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for step in steps:
        args = _payload_for_prompt(step.get("arguments") or {})
        if not isinstance(args, dict):
            continue
        payload = _payload_for_prompt(args.get("policy_payload") or {})
        if not isinstance(payload, dict):
            continue
        for row in payload.get("target_predicates") or []:
            row_payload = _payload_for_prompt(row)
            if isinstance(row_payload, dict):
                rows.append(row_payload)
    return rows


def _target_order_rows_from_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for step in steps:
        args = _payload_for_prompt(step.get("arguments") or {})
        if not isinstance(args, dict):
            continue
        payload = _payload_for_prompt(args.get("policy_payload") or {})
        if not isinstance(payload, dict):
            continue
        for row in payload.get("target_order_by") or []:
            row_payload = _payload_for_prompt(row)
            if not isinstance(row_payload, dict):
                continue
            key = json.dumps(row_payload, ensure_ascii=False, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            rows.append(row_payload)
    return rows


def _target_limit_from_steps(steps: List[Dict[str, Any]]) -> Optional[str]:
    for step in steps:
        args = _payload_for_prompt(step.get("arguments") or {})
        if not isinstance(args, dict):
            continue
        payload = _payload_for_prompt(args.get("policy_payload") or {})
        if not isinstance(payload, dict):
            continue
        value = payload.get("target_limit")
        if value is None:
            continue
        text = str(value).strip()
        if re.fullmatch(r"\d+", text):
            return text
    return None


def _source_ranking_predicate_rows_from_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for step in steps:
        args = _payload_for_prompt(step.get("arguments") or {})
        if not isinstance(args, dict):
            continue
        payload = _payload_for_prompt(args.get("policy_payload") or {})
        if not isinstance(payload, dict):
            continue
        for row in payload.get("source_ranking_predicates") or []:
            row_payload = _payload_for_prompt(row)
            if not isinstance(row_payload, dict):
                continue
            key = json.dumps(row_payload, ensure_ascii=False, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            rows.append(row_payload)
    return rows


def _render_target_predicate_for_sql(row: Dict[str, Any], sql: str) -> Optional[str]:
    predicate = str(row.get("predicate") or "").strip()
    refs = [
        ref for ref in (_payload_for_prompt(item) for item in (row.get("refs") or []))
        if isinstance(ref, dict)
    ]
    aliases = _sql_alias_map(sql)
    for ref in refs:
        ref_table = str(ref.get("table") or "").strip()
        if ref_table and ref_table.lower() not in aliases:
            return None
    first_ref = refs[0] if refs else {}
    table = str(first_ref.get("table") or "").strip()
    column = str(first_ref.get("column") or "").strip()
    alias = aliases.get(table.lower(), table)
    if column and re.search(r"\bis\s+null\b", predicate, flags=re.IGNORECASE):
        prefix = f"{alias}." if alias else ""
        if re.search(r"\bnot\b.*\bis\s+null\b|\bis\s+not\s+null\b", predicate, flags=re.IGNORECASE):
            return f"{prefix}{column} IS NOT NULL"
        return f"{prefix}{column} IS NULL"
    if not predicate:
        return None
    rendered = predicate
    for ref in refs:
        ref_table = str(ref.get("table") or "").strip()
        ref_column = str(ref.get("column") or "").strip()
        ref_alias = aliases.get(ref_table.lower(), ref_table)
        if not ref_column or not ref_alias:
            continue
        rendered = re.sub(
            rf"\b[A-Za-z_][A-Za-z0-9_]*\.{re.escape(ref_column)}\b",
            f"{ref_alias}.{ref_column}",
            rendered,
        )
    return rendered


def _rewrite_unbound_target_predicate_refs(sql: str, row: Dict[str, Any]) -> str:
    refs = [
        ref for ref in (_payload_for_prompt(item) for item in (row.get("refs") or []))
        if isinstance(ref, dict)
    ]
    if not refs:
        return sql
    aliases = _sql_alias_map(sql)
    alias_to_table = _alias_to_table_map(sql)
    where_parts = _split_top_level_where(sql)
    if not where_parts:
        return sql
    head, where_segment, tail = where_parts
    rewritten_where = where_segment
    for ref in refs:
        table = str(ref.get("table") or "").strip()
        column = str(ref.get("column") or "").strip()
        if not table or not column:
            continue
        bound_alias = aliases.get(table.lower())
        if not bound_alias:
            continue
        for qualifier in set(
            match.group(1)
            for match in re.finditer(
                rf"\b([A-Za-z_][A-Za-z0-9_]*)\.{re.escape(column)}\b",
                rewritten_where,
            )
        ):
            if qualifier.lower() in alias_to_table:
                continue
            rewritten_where = _replace_top_level_qualified_ref(
                text=rewritten_where,
                qualifier=qualifier,
                column=column,
                replacement=f"{bound_alias}.{column}",
            )
    return f"{head}{rewritten_where}{tail}"


def _predicate_already_present(sql: str, predicate: str) -> bool:
    compact_sql = _compact_sql_fragment(sql)
    compact_predicate = _compact_sql_fragment(predicate)
    if not compact_predicate:
        return True
    not_null = re.search(
        r"\b([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\s+is\s+not\s+null\b",
        str(predicate or ""),
        flags=re.IGNORECASE,
    )
    if not_null:
        ref = _compact_sql_fragment(not_null.group(1))
        if f"{ref}isnotnull" in compact_sql or f"not{ref}isnull" in compact_sql:
            return True
    return compact_predicate in compact_sql


def _insert_where_predicate(sql: str, predicate: str) -> str:
    text = str(sql or "").strip()
    boundary = re.search(
        r"\s+\b(group\s+by|order\s+by|having|limit|union|intersect|except)\b",
        text,
        flags=re.IGNORECASE,
    )
    insert_at = boundary.start() if boundary else len(text)
    head = text[:insert_at].rstrip()
    tail = text[insert_at:].lstrip()
    if re.search(r"\bwhere\b", head, flags=re.IGNORECASE):
        rewritten = f"{head} AND {predicate}"
    else:
        rewritten = f"{head} WHERE {predicate}"
    return f"{rewritten} {tail}".strip() if tail else rewritten


def _split_top_level_and(segment: str) -> List[str]:
    text = str(segment or "")
    parts: List[str] = []
    start = 0
    depth = 0
    i = 0
    while i < len(text):
        char = text[i]
        if char == "(":
            depth += 1
            i += 1
            continue
        if char == ")" and depth:
            depth -= 1
            i += 1
            continue
        if (
            depth == 0
            and text[i : i + 3].lower() == "and"
            and (i == 0 or not text[i - 1].isalnum())
            and (i + 3 >= len(text) or not text[i + 3].isalnum())
        ):
            part = text[start:i].strip()
            if part:
                parts.append(part)
            start = i + 3
            i += 3
            continue
        i += 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _predicate_matches_source_ranking(row: Dict[str, Any], predicate: str, sql: str) -> bool:
    normalized = _compact_sql_fragment(_rewrite_alias_refs_to_tables_deep(predicate, sql))
    aggregate = str(row.get("aggregate") or "").strip().lower()
    if aggregate and f"{aggregate}(" not in normalized:
        return False
    refs = [
        ref for ref in (_payload_for_prompt(item) for item in (row.get("refs") or []))
        if isinstance(ref, dict)
    ]
    if not refs:
        return False
    for ref in refs:
        column = _compact_sql_fragment(str(ref.get("column") or ""))
        table = _compact_sql_fragment(str(ref.get("table") or ""))
        if not table or not column:
            continue
        if f"{table}.{column}" in normalized:
            return True
    return False


def _drop_source_ranking_predicates(sql: str, rows: List[Dict[str, Any]]) -> tuple[str, int]:
    where_parts = _split_top_level_where(sql)
    if not where_parts or not rows:
        return sql, 0
    head, where_segment, tail = where_parts
    predicates = _split_top_level_and(where_segment)
    if not predicates:
        return sql, 0
    kept: List[str] = []
    dropped = 0
    for predicate in predicates:
        if any(_predicate_matches_source_ranking(row, predicate, sql) for row in rows):
            dropped += 1
            continue
        kept.append(predicate)
    if not dropped:
        return sql, 0
    if kept:
        return f"{head} {' AND '.join(kept)}{tail}", dropped
    prefix = re.sub(r"\bwhere\b\s*$", "", head, flags=re.IGNORECASE).rstrip()
    return f"{prefix} {tail.lstrip()}".strip(), dropped


def _render_target_order_expr_for_sql(row: Dict[str, Any], sql: str) -> Optional[str]:
    expression = str(row.get("expression") or "").strip()
    refs = [
        ref for ref in (_payload_for_prompt(item) for item in (row.get("refs") or []))
        if isinstance(ref, dict)
    ]
    aliases = _sql_alias_map(sql)
    if not expression:
        return None
    for ref in refs:
        ref_table = str(ref.get("table") or "").strip()
        if ref_table and ref_table.lower() not in aliases:
            return None
    rendered = expression
    for ref in refs:
        ref_table = str(ref.get("table") or "").strip()
        ref_column = str(ref.get("column") or "").strip()
        ref_alias = aliases.get(ref_table.lower(), ref_table) if ref_table else ""
        if not ref_column or not ref_alias:
            continue
        rendered = re.sub(
            rf"\b[A-Za-z_][A-Za-z0-9_]*\.{re.escape(ref_column)}\b",
            f"{ref_alias}.{ref_column}",
            rendered,
        )
    return rendered


def _top_level_keyword_match(sql: str, pattern: str) -> Optional[re.Match[str]]:
    text = str(sql or "")
    return next(
        (
            match
            for match in re.finditer(pattern, text, flags=re.IGNORECASE)
            if _is_top_level_offset(text, match.start())
        ),
        None,
    )


def _strip_top_level_order_preserving_limit(sql: str) -> tuple[str, str]:
    text = str(sql or "").strip()
    order_match = _top_level_keyword_match(text, r"\border\s+by\b")
    limit_match = _top_level_keyword_match(text, r"\blimit\s+\d+\b")
    if order_match is not None:
        base = text[: order_match.start()].rstrip()
    elif limit_match is not None:
        base = text[: limit_match.start()].rstrip()
    else:
        base = text
    limit_clause = text[limit_match.start():].strip() if limit_match is not None else ""
    return base, limit_clause


def _apply_target_order_by(sql: str, rows: List[Dict[str, Any]]) -> tuple[str, int]:
    clauses: List[str] = []
    for row in rows:
        expression = _render_target_order_expr_for_sql(row, sql)
        if not expression:
            continue
        direction = str(row.get("direction") or "ASC").strip().upper()
        if direction not in {"ASC", "DESC"}:
            direction = "ASC"
        clauses.append(f"{expression} {direction}")
    if not clauses:
        return sql, 0
    base, limit_clause = _strip_top_level_order_preserving_limit(sql)
    rewritten = f"{base} ORDER BY {', '.join(clauses)}"
    if limit_clause:
        rewritten = f"{rewritten} {limit_clause}"
    return rewritten, len(clauses)


def _apply_target_limit(sql: str, limit_value: str) -> tuple[str, bool]:
    if not re.fullmatch(r"\d+", str(limit_value or "").strip()):
        return sql, False
    text = str(sql or "").strip()
    limit_match = _top_level_keyword_match(text, r"\blimit\s+\d+\b")
    base = text[: limit_match.start()].rstrip() if limit_match else text
    return f"{base} LIMIT {str(limit_value).strip()}", True


def _relation_edge_endpoints(edge: Dict[str, Any]) -> Optional[tuple[str, str, str, str]]:
    left = _payload_for_prompt(edge.get("left") or {})
    right = _payload_for_prompt(edge.get("right") or {})
    if not isinstance(left, dict) or not isinstance(right, dict):
        return None
    left_table = str(left.get("table") or "").strip()
    left_column = str(left.get("column") or "").strip()
    right_table = str(right.get("table") or "").strip()
    right_column = str(right.get("column") or "").strip()
    if not all((left_table, left_column, right_table, right_column)):
        return None
    return left_table, left_column, right_table, right_column


def _edge_condition(edge: Dict[str, Any]) -> Optional[str]:
    endpoints = _relation_edge_endpoints(edge)
    if not endpoints:
        return None
    left_table, left_column, right_table, right_column = endpoints
    return f"{left_table}.{left_column} = {right_table}.{right_column}"


def _relation_edge_present(sql: str, edge: Dict[str, Any]) -> bool:
    endpoints = _relation_edge_endpoints(edge)
    if not endpoints:
        return True
    left_table, left_column, right_table, right_column = endpoints
    normalized = _compact_sql_fragment(_rewrite_alias_refs_to_tables(sql, sql))
    forward = _compact_sql_fragment(f"{left_table}.{left_column}={right_table}.{right_column}")
    reverse = _compact_sql_fragment(f"{right_table}.{right_column}={left_table}.{left_column}")
    return forward in normalized or reverse in normalized


def _target_relation_edges_from_actions(actions: List[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for action in actions or []:
        payload = _payload_for_prompt(action)
        if not isinstance(payload, dict):
            continue
        if _enum_name(payload.get("primitive")) != "REROUTE_FACT":
            continue
        args = _payload_for_prompt(payload.get("arguments") or {})
        if not isinstance(args, dict):
            continue
        for edge in args.get("target_relation_edges") or []:
            edge_payload = _payload_for_prompt(edge)
            if not isinstance(edge_payload, dict):
                continue
            key = str(edge_payload.get("canonical_key") or _edge_condition(edge_payload) or "")
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(edge_payload)
    return rows


def _target_output_refs_from_reroute_actions(actions: List[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for action in actions or []:
        payload = _payload_for_prompt(action)
        if not isinstance(payload, dict):
            continue
        if _enum_name(payload.get("primitive")) != "REROUTE_FACT":
            continue
        args = _payload_for_prompt(payload.get("arguments") or {})
        if not isinstance(args, dict):
            continue
        for ref in args.get("target_output_refs") or []:
            ref_payload = _payload_for_prompt(ref)
            if isinstance(ref_payload, dict):
                rows.append(ref_payload)
    return rows


def _reroute_fact_has_allowed_rebuild_scope(actions: List[Any]) -> bool:
    required = {"SELECT", "FROM", "JOIN"}
    for action in actions or []:
        payload = _payload_for_prompt(action)
        if not isinstance(payload, dict):
            continue
        if _enum_name(payload.get("primitive")) != "REROUTE_FACT":
            continue
        scopes = {_enum_name(item) for item in (payload.get("allowed_edit_scope") or [])}
        if required <= scopes:
            return True
    return False


def _ordered_target_output_refs(refs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        refs,
        key=lambda ref: (
            1 if ref.get("slot_index") is None else 0,
            int(ref.get("slot_index") or 0),
            str(ref.get("expression") or ""),
        ),
    )


def _referenced_tables_in_fragment(fragment: str) -> set[str]:
    return {
        match.group(1).lower()
        for match in re.finditer(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_][A-Za-z0-9_]*\b",
            str(fragment or ""),
        )
    }


def _extract_where_filters_for_reroute(
    sql: str,
    target_edges: List[Dict[str, Any]],
    *,
    allowed_tables: set[str],
) -> tuple[List[str], str]:
    text = str(sql or "").strip()
    tail_match = _top_level_keyword_match(text, r"\b(group\s+by|order\s+by|having|limit)\b")
    tail = text[tail_match.start():].strip() if tail_match else ""
    try:
        from .structure_family import cached_ast_signature

        ast = cached_ast_signature(text) or {}
        predicates = [str(item) for item in (ast.get("predicates") or []) if str(item).strip()]
    except Exception:
        predicates = []
    if not predicates:
        where_parts = _split_top_level_where(text)
        predicates = _split_top_level_and(where_parts[1]) if where_parts else []
    edge_fragments = {
        _compact_sql_fragment(condition)
        for condition in (_edge_condition(edge) for edge in target_edges)
        if condition
    }
    filters: List[str] = []
    seen_filters: set[str] = set()
    for predicate in predicates:
        normalized = _rewrite_alias_refs_to_tables_deep(predicate.strip(), sql)
        compact = _compact_sql_fragment(normalized)
        if not compact:
            continue
        if compact in edge_fragments:
            continue
        referenced_tables = _referenced_tables_in_fragment(normalized)
        if not referenced_tables:
            continue
        if referenced_tables and not referenced_tables <= allowed_tables:
            continue
        if compact in seen_filters:
            continue
        seen_filters.add(compact)
        filters.append(normalized)
    return filters, tail


def _select_prefix_for_sql(sql: str) -> str:
    return "SELECT DISTINCT" if re.search(r"\bselect\s+distinct\b", str(sql or ""), flags=re.IGNORECASE) else "SELECT"


def _build_join_from_target_edges(
    *,
    start_table: str,
    target_edges: List[Dict[str, Any]],
) -> tuple[Optional[str], List[str]]:
    if not start_table:
        return None, []
    visited = {start_table}
    pending = list(target_edges)
    joins: List[str] = [f"FROM {start_table}"]
    extra_conditions: List[str] = []
    while pending:
        progressed = False
        for edge in list(pending):
            endpoints = _relation_edge_endpoints(edge)
            if not endpoints:
                pending.remove(edge)
                progressed = True
                continue
            left_table, left_column, right_table, right_column = endpoints
            if left_table in visited and right_table not in visited:
                joins.append(
                    f"JOIN {right_table} ON {left_table}.{left_column} = {right_table}.{right_column}"
                )
                visited.add(right_table)
                pending.remove(edge)
                progressed = True
                break
            if right_table in visited and left_table not in visited:
                joins.append(
                    f"JOIN {left_table} ON {left_table}.{left_column} = {right_table}.{right_column}"
                )
                visited.add(left_table)
                pending.remove(edge)
                progressed = True
                break
            if left_table in visited and right_table in visited:
                condition = _edge_condition(edge)
                if condition:
                    extra_conditions.append(condition)
                pending.remove(edge)
                progressed = True
                break
        if not progressed:
            return None, []
    return " ".join(joins), extra_conditions


def _rewrite_reroute_fact_from_target_edges(sql: str, actions: List[Any]) -> Optional[str]:
    if not _reroute_fact_has_allowed_rebuild_scope(actions):
        return None
    target_edges = _target_relation_edges_from_actions(actions)
    if not target_edges or all(_relation_edge_present(sql, edge) for edge in target_edges):
        return None
    output_refs = [
        ref
        for ref in _ordered_target_output_refs(_target_output_refs_from_reroute_actions(actions))
        if str(ref.get("table") or "").strip() and str(ref.get("column") or "").strip()
    ]
    if not output_refs:
        return None
    allowed_tables = {
        table.lower()
        for edge in target_edges
        for endpoints in [_relation_edge_endpoints(edge)]
        if endpoints
        for table in (endpoints[0], endpoints[2])
    }
    allowed_tables.update(str(ref.get("table") or "").strip().lower() for ref in output_refs)
    allowed_tables.discard("")
    start_table = str(output_refs[0].get("table") or "").strip()
    from_clause, extra_conditions = _build_join_from_target_edges(
        start_table=start_table,
        target_edges=target_edges,
    )
    if not from_clause:
        return None
    select_exprs = [
        f"{str(ref.get('table')).strip()}.{str(ref.get('column')).strip()}"
        for ref in output_refs
    ]
    filters, tail = _extract_where_filters_for_reroute(
        sql,
        target_edges,
        allowed_tables=allowed_tables,
    )
    where_conditions = filters + extra_conditions
    rewritten = f"{_select_prefix_for_sql(sql)} {', '.join(select_exprs)} {from_clause}"
    if where_conditions:
        rewritten = f"{rewritten} WHERE {' AND '.join(where_conditions)}"
    if tail:
        rewritten = f"{rewritten} {tail}"
    if not all(_relation_edge_present(rewritten, edge) for edge in target_edges):
        return None
    return rewritten


def _trace_action_id(trace: Any) -> str:
    if isinstance(trace, dict):
        return str(trace.get("action_id") or "")
    return str(getattr(trace, "action_id", "") or "")


def _trace_edits(trace: Any) -> List[Any]:
    if isinstance(trace, dict):
        return list(trace.get("edits") or [])
    return list(getattr(trace, "edits", []) or [])


def _edit_location(edit: Any) -> str:
    if isinstance(edit, dict):
        return _enum_name(edit.get("location"))
    return _enum_name(getattr(edit, "location", None))


def _trace_notes(trace: Any) -> str:
    if isinstance(trace, dict):
        return str(trace.get("notes") or "")
    return str(getattr(trace, "notes", "") or "")


def _trace_realized(trace: Any) -> bool:
    if isinstance(trace, dict):
        return bool(trace.get("realized"))
    return bool(getattr(trace, "realized", False))


def _copy_trace(trace: Any, updates: Dict[str, Any]) -> Any:
    if hasattr(trace, "model_copy"):
        return trace.model_copy(update=updates)
    if isinstance(trace, dict):
        out = dict(trace)
        out.update(updates)
        return out
    for key, value in updates.items():
        try:
            setattr(trace, key, value)
        except Exception:
            pass
    return trace


def _allowed_scopes_by_action(actions: List[Any]) -> Dict[str, set[str]]:
    out: Dict[str, set[str]] = {}
    for action in actions or []:
        payload = _payload_for_prompt(action)
        if not isinstance(payload, dict):
            continue
        action_id = str(payload.get("action_id") or "")
        if not action_id:
            continue
        out[action_id] = {
            _enum_name(scope)
            for scope in (payload.get("allowed_edit_scope") or [])
            if _enum_name(scope)
        }
    return out


def _enforce_rewrite_scope(
    *,
    original_sql: str,
    rewrite_sql: Optional[str],
    actions: List[Any],
    traces: List[Any],
    contract_steps_applied: List[str],
) -> tuple[Optional[str], List[Any], List[str]]:
    if not rewrite_sql:
        return rewrite_sql, traces, contract_steps_applied

    allowed_by_action = _allowed_scopes_by_action(actions)
    updated_traces: List[Any] = []
    fail_reasons: List[str] = []

    for trace in traces or []:
        action_id = _trace_action_id(trace)
        allowed = allowed_by_action.get(action_id, set())
        removed_locations: List[str] = []
        for edit in _trace_edits(trace):
            location = _edit_location(edit)
            if allowed and location and location not in allowed:
                removed_locations.append(location)

        if removed_locations:
            fail_reasons.append(
                f"{action_id or 'unknown'}:out_of_scope="
                + ",".join(sorted(set(removed_locations)))
            )
            note = (
                _trace_notes(trace)
                + " | scope_enforced: fail closed on out-of-scope edits "
                + ",".join(sorted(set(removed_locations)))
            ).strip(" |")
            trace = _copy_trace(
                trace,
                {
                    "realized": False,
                    "scope_violation": True,
                    "notes": note,
                },
            )
        updated_traces.append(trace)

    if fail_reasons:
        note = "scope_enforced_fail_closed:" + "|".join(sorted(set(fail_reasons)))
        if note not in contract_steps_applied:
            contract_steps_applied.append(note)
        return original_sql, updated_traces, contract_steps_applied
    return rewrite_sql, updated_traces, contract_steps_applied


def _action_payload_map(actions: List[Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for action in actions or []:
        payload = _payload_for_prompt(action)
        if not isinstance(payload, dict):
            continue
        action_id = str(payload.get("action_id") or "")
        if action_id:
            out[action_id] = payload
    return out


def _has_bound_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _required_binding_groups_for_primitive(primitive: str) -> List[tuple[str, ...]]:
    mapping: Dict[str, List[tuple[str, ...]]] = {
        "ADD_SELECT_SLOT": [("target_columns", "target_output_refs")],
        "REPLACE_SELECT_SLOT": [
            ("from_exprs", "from_expr"),
            ("target_columns", "target_expr", "target_output_refs"),
        ],
        "DROP_SELECT_SLOT": [("from_exprs", "from_expr")],
        "DROP_SIDE": [("from_exprs", "from_expr", "predicate_ref", "drop_condition", "from_predicate")],
        "INSERT_BRIDGE": [("target_relation_edges",)],
        "REROUTE_FACT": [("target_relation_edges",)],
        "CHANGE_GRAIN": [("source_grain",), ("target_grain", "aggregate_rewrite", "target_anchor")],
        "MOVE_CONDITION": [("predicate_ref",), ("from_scope",), ("to_scope",)],
        "SWITCH_CANONICAL_FIELD": [
            ("current_expr", "from_exprs"),
            ("target_expr", "target_columns", "target_output_refs"),
        ],
        "MATERIALIZE_RANKING_OUTPUT": [("ranking_expr", "metric_expr"), ("window_fn",)],
    }
    return mapping.get(str(primitive or "").strip().upper(), [])


def _missing_required_binding_groups(primitive: str, arguments: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    for group in _required_binding_groups_for_primitive(primitive):
        if any(_has_bound_value(arguments.get(key)) for key in group):
            continue
        missing.append("/".join(group))
    return missing


def _fail_closed_rewrite_contract(
    *,
    original_sql: str,
    rewrite_sql: Optional[str],
    actions: List[Any],
    traces: List[Any],
    contract_steps_applied: List[str],
) -> tuple[Optional[str], List[Any], List[str]]:
    action_payloads = _action_payload_map(actions)
    if not action_payloads:
        return rewrite_sql, traces, contract_steps_applied

    traces_by_action: Dict[str, List[Any]] = {}
    for trace in traces or []:
        action_id = _trace_action_id(trace)
        traces_by_action.setdefault(action_id, []).append(trace)

    updated_traces: List[Any] = []
    fail_reasons: List[str] = []

    for action_id, payload in action_payloads.items():
        primitive = _enum_name(payload.get("primitive"))
        arguments = _payload_for_prompt(payload.get("arguments") or {})
        if not isinstance(arguments, dict):
            arguments = {}
        missing_bindings = _missing_required_binding_groups(primitive, arguments)
        rows = list(traces_by_action.pop(action_id, []))

        if not rows:
            fail_reasons.append(f"{action_id}:missing_trace")
            updated_traces.append(
                {
                    "action_id": action_id,
                    "realized": False,
                    "edits": [],
                    "scope_violation": False,
                    "notes": "rewrite_contract_fail_closed: missing action trace",
                }
            )
            continue

        if len(rows) > 1:
            fail_reasons.append(f"{action_id}:multiple_traces")

        trace = rows[0]
        edits = _trace_edits(trace)
        notes = _trace_notes(trace)
        realized = _trace_realized(trace)
        trace_failures: List[str] = []
        if missing_bindings:
            trace_failures.append(
                "missing_action_bindings=" + ",".join(sorted(missing_bindings))
            )
        if realized and not edits:
            trace_failures.append("realized_without_edits")
        if not realized:
            trace_failures.append("unrealized_required_action")
        if trace_failures:
            fail_reasons.append(f"{action_id}:" + ";".join(trace_failures))
            trace = _copy_trace(
                trace,
                {
                    "realized": False,
                    "notes": (
                        notes
                        + " | rewrite_contract_fail_closed: "
                        + ";".join(trace_failures)
                    ).strip(" |"),
                },
            )
        updated_traces.append(trace)

    for unknown_action_id, extra_rows in sorted(traces_by_action.items()):
        fail_reasons.append(f"{unknown_action_id or 'unknown'}:unexpected_trace")
        for trace in extra_rows:
            updated_traces.append(
                _copy_trace(
                    trace,
                    {
                        "realized": False,
                        "notes": (
                            _trace_notes(trace)
                            + " | rewrite_contract_fail_closed: unexpected trace without matching action"
                        ).strip(" |"),
                    },
                )
            )

    if not fail_reasons:
        return rewrite_sql, updated_traces, contract_steps_applied

    note = "rewrite_contract_fail_closed:" + "|".join(sorted(set(fail_reasons)))
    if note not in contract_steps_applied:
        contract_steps_applied.append(note)
    return original_sql, updated_traces, contract_steps_applied


def _enforce_rewrite_contract_absence_checks(
    *,
    original_sql: str,
    rewrite_sql: Optional[str],
    rewrite_contract: Dict[str, Any],
    traces: List[Any],
    contract_steps_applied: List[str],
) -> tuple[Optional[str], List[Any], List[str]]:
    if not rewrite_sql:
        return rewrite_sql, traces, contract_steps_applied
    failures: List[Dict[str, str]] = []
    lowered = str(rewrite_sql).lower()
    for check in rewrite_contract.get("required_absence_checks") or []:
        payload = _payload_for_prompt(check)
        text = str(payload.get("text") or "").strip()
        if text and text.lower() in lowered:
            failures.append(
                {
                    "action_id": str(payload.get("action_id") or ""),
                    "text": text,
                }
            )
    if not failures:
        return rewrite_sql, traces, contract_steps_applied
    note = "rewrite_contract_absence_failed:" + "|".join(
        f"{item['action_id']}:{item['text']}" for item in failures[:6]
    )
    failed_action_ids = {item["action_id"] for item in failures if item["action_id"]}
    updated_traces: List[Any] = []
    for trace in traces or []:
        action_id = _trace_action_id(trace)
        if action_id in failed_action_ids:
            updated_traces.append(
                _copy_trace(
                    trace,
                    {
                        "realized": False,
                        "notes": (_trace_notes(trace) + " | " + note).strip(" |"),
                    },
                )
            )
        else:
            updated_traces.append(trace)
    if not updated_traces:
        for action_id in sorted(failed_action_ids):
            updated_traces.append(
                {
                    "action_id": action_id,
                    "realized": False,
                    "edits": [],
                    "scope_violation": False,
                    "notes": note,
                }
            )
    if note not in contract_steps_applied:
        contract_steps_applied.append(note)
    return original_sql, updated_traces, contract_steps_applied


def _candidate_prompt_checks(candidate: Dict[str, Any]) -> Dict[str, Any]:
    args = _payload_for_prompt(candidate.get("arguments") or {})
    schema_legal = bool(candidate.get("schema_legal", True))
    reject_reasons: List[str] = []
    if not schema_legal:
        reject_reasons.append("schema_illegal")
    canonical_axes = list(args.get("canonical_unresolved_variation_axes") or [])
    binding_status = "ambiguous" if canonical_axes else "unique"
    memory_alignment_payload = _payload_for_prompt(args.get("memory_alignment") or {})
    memory_score = memory_alignment_payload.get("score")
    memory_alignment = "unknown"
    if memory_alignment_payload:
        try:
            memory_alignment = "fail" if float(memory_score or 0.0) < 0 else "pass"
        except Exception:
            memory_alignment = "unknown"
    direction_check = "pass"
    if memory_alignment == "fail":
        direction_check = "fail"
        reject_reasons.append("memory_direction_conflict")
    candidate_contract_status = "blocked" if reject_reasons else "executable"
    compatibility = "exact" if candidate_contract_status == "executable" else "conflict"
    return {
        "compatibility": compatibility,
        "binding_status": binding_status,
        "direction_check": direction_check,
        "memory_alignment_status": memory_alignment,
        "candidate_contract_status": candidate_contract_status,
        "reject_reasons": reject_reasons,
    }


def _candidate_sets_prompt_payload(candidate_sets: List[Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for candidate_set in candidate_sets or []:
        payload = _payload_for_prompt(candidate_set)
        if not isinstance(payload, dict):
            continue
        candidates: List[Dict[str, Any]] = []
        for candidate in payload.get("candidates") or []:
            cand = _payload_for_prompt(candidate)
            if not isinstance(cand, dict):
                continue
            checks = _candidate_prompt_checks(cand)
            candidates.append(
                {
                    "candidate_id": cand.get("candidate_id"),
                    "source_group_id": cand.get("source_group_id"),
                    "source_group_type": _enum_value(cand.get("source_group_type")),
                    "schema_legal": cand.get("schema_legal", True),
                    "provenance": _short_text(cand.get("provenance"), 180),
                    "compatibility": checks["compatibility"],
                    "binding_status": checks["binding_status"],
                    "direction_check": checks["direction_check"],
                    "memory_alignment_status": checks["memory_alignment_status"],
                    "candidate_contract_status": checks["candidate_contract_status"],
                    "reject_reasons": checks["reject_reasons"],
                    "arguments": _compact_action_candidate_arguments(
                        cand.get("arguments") or {}
                    ),
                }
            )
        rows.append(
            {
                "primitive": _enum_value(payload.get("primitive")),
                "empty_reason": payload.get("empty_reason"),
                "candidate_count": len(candidates),
                "candidates": candidates,
            }
        )
    return rows


def _action_compiler_prompt_payloads(
    *,
    runtime_case_view: Any,
    memory_objects: List[Any],
    candidate_sets: List[Any],
    schema_diagnostics_pre: Any,
    trigger_result: Any = None,
) -> Dict[str, Any]:
    runtime_payload = _compact_runtime_case_view_payload(runtime_case_view)
    runtime_payload["local_schema_view"] = _candidate_linked_schema_prompt_payload(
        runtime_case_view=runtime_case_view,
        candidate_sets=candidate_sets or [],
    )
    return {
        "runtime_case_view": runtime_payload,
        "memory_objects": _memory_objects_prompt_payload(
            memory_objects or [],
            trigger_result=trigger_result,
        ),
        "candidate_sets": _candidate_sets_prompt_payload(candidate_sets or []),
        "schema_diagnostics_pre": _compact_prompt_payload(
            _payload_for_prompt(schema_diagnostics_pre)
        ),
    }


def _json_dump_prompt(obj: Any) -> str:
    try:
        return json.dumps(
            _compact_prompt_payload(_payload_for_prompt(obj)),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to serialize compact prompt object: {exc}") from exc


def _json_dump_prompt_contract(obj: Any) -> str:
    """Serialize the pre-Phase-A prompt contract.

    Phase A signals are audit-only. They are kept on Pydantic objects for
    logging, but must not enter LLM prompts until a later phase explicitly opts
    into using them for trigger/compiler/rewrite behavior.
    """
    payload = _payload_for_prompt(obj)
    if not isinstance(payload, dict):
        return _json_dump_prompt(payload)
    if isinstance(payload, dict):
        payload.pop("case_signal_view", None)
        payload.pop("case_signal_bundle", None)
        payload.pop("delta_signature", None)
    return _json_dump_prompt(payload)


def _enum_values(enum_cls) -> str:
    """返回 'A | B | C' 形式的枚举值展示串，供 prompt 里提示。"""
    return " | ".join(m.value for m in enum_cls)


def _coerce_output_contract(value: Any):
    """Keep legacy output_contract non-decisive; generic shape lives elsewhere."""
    from .vocabulary_v2 import OutputContract

    _ = value
    return OutputContract.UNCHANGED


def _coerce_locus(value: Any):
    """Normalize common SQL-surface synonyms into the closed Locus enum."""
    from .vocabulary_v2 import Locus

    raw = str(value or "").strip().upper()
    aliases = {
        "FROM": "JOIN",
        "FROM_JOIN": "JOIN",
        "HAVING": "SCOPE",
        "CASE": "SCOPE",
        "SELECT_LIST": "SELECT",
        "PROJECTION": "SELECT",
        "ORDER": "ORDER_BY",
        "GROUP": "GROUP_BY",
    }
    return Locus(aliases.get(raw, raw))


def _output_shape_delta_from_code_prepared(code_prepared: Dict[str, Any]):
    from .data_structures_v2 import OutputShapeDelta

    delta_signature = code_prepared.get("delta_signature")
    shape = None
    if delta_signature is not None and getattr(delta_signature, "output_shape_delta", None) is not None:
        shape = delta_signature.output_shape_delta
    elif isinstance(delta_signature, dict):
        shape = delta_signature.get("output_shape_delta")
    if shape is None:
        return None
    payload = shape.model_dump(mode="json") if hasattr(shape, "model_dump") else dict(shape)
    current = payload.get("current_arity")
    target = payload.get("target_arity")
    if current is not None and target is not None:
        delta = int(target) - int(current)
        payload["arity_delta"] = delta
        payload["arity_direction"] = "increase" if delta > 0 else "decrease" if delta < 0 else "same"
    return OutputShapeDelta.model_validate(payload)


def _action_signal_flags(actions: List[Any]) -> set[str]:
    primitive_map = {
        "ADD_SELECT_SLOT": {"add", "select"},
        "REPLACE_SELECT_SLOT": {"replace", "select"},
        "DROP_SELECT_SLOT": {"drop", "select"},
        "REROUTE_FACT": {"reroute"},
        "INSERT_BRIDGE": {"add", "join"},
        "CHANGE_GRAIN": {"replace"},
        "MOVE_CONDITION": {"move", "condition"},
        "DROP_SIDE": {"drop"},
        "SWITCH_CANONICAL_FIELD": {"replace"},
        "MATERIALIZE_RANKING_OUTPUT": {"add", "rank", "select"},
    }
    flags: set[str] = set()
    for action in actions or []:
        if hasattr(action, "primitive"):
            primitive = getattr(action, "primitive")
        elif isinstance(action, dict):
            primitive = action.get("primitive")
        else:
            primitive = ""
        primitive_text = str(getattr(primitive, "value", primitive) or "")
        flags.update(primitive_map.get(primitive_text, set()))
    return flags


def _validate_hint_instantiation_result(
    *,
    raw_hint: str,
    instantiated_hint: str,
    applicable: bool,
    notes: str,
    question: str,
    evidence: str,
    pred_top1_sql: str,
    local_schema_view: Any,
    actions: List[Any],
) -> Dict[str, Any]:
    """Validate a readability-only hint against action-grounded runtime state.

    ``raw_hint`` is expected to be a code-rendered action brief, not a
    source-case template. Applicability here therefore only decides whether the
    rewritten hint stays aligned with the same grounded actions.
    """
    _ = (raw_hint, question, evidence, pred_top1_sql, local_schema_view)
    action_flags = sorted(_action_signal_flags(actions))
    note_suffix = f" | action_primitives={action_flags}" if action_flags else ""
    return {
        "instantiated_hint": instantiated_hint,
        "applicable": bool(applicable),
        "instantiation_notes": (notes or "") + note_suffix,
        "rewrite_allowed": True,
    }


def _target_ref_pairs_from_action_args(args: Dict[str, Any]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for item in list(args.get("target_output_refs") or []) + list(args.get("target_columns") or []):
        row = _payload_for_prompt(item)
        if not isinstance(row, dict):
            continue
        table = str(row.get("table") or row.get("target_table") or "").strip().lower()
        column = str(row.get("column") or row.get("target_column") or "").strip().lower()
        if table or column:
            pairs.add((table, column))
    return pairs


def _reroute_candidate_covers_projection_actions(
    candidate: Any,
    group_actions: List[Any],
) -> bool:
    candidate_args = _payload_for_prompt(getattr(candidate, "arguments", {}) or {})
    if not isinstance(candidate_args, dict):
        return False
    candidate_pairs = _target_ref_pairs_from_action_args(candidate_args)
    if not candidate_pairs:
        return False
    column_counts: Dict[str, int] = {}
    for _, column in candidate_pairs:
        if column:
            column_counts[column] = column_counts.get(column, 0) + 1
    required_pairs: set[tuple[str, str]] = set()
    for action in group_actions:
        args = _payload_for_prompt(getattr(action, "arguments", {}) or {})
        if isinstance(args, dict):
            required_pairs.update(_target_ref_pairs_from_action_args(args))
    if not required_pairs:
        return False
    for table, column in required_pairs:
        if table:
            if (table, column) not in candidate_pairs:
                return False
            continue
        if not column or column_counts.get(column, 0) != 1:
            return False
    return True


def _reroute_candidate_has_missing_relation_for_projection(
    candidate: Any,
    group_actions: List[Any],
    current_sql: str,
) -> bool:
    target_tables: set[str] = set()
    for action in group_actions:
        args = _payload_for_prompt(getattr(action, "arguments", {}) or {})
        if not isinstance(args, dict):
            continue
        for table, _column in _target_ref_pairs_from_action_args(args):
            if table:
                target_tables.add(table)
    if not target_tables:
        return False
    candidate_args = _payload_for_prompt(getattr(candidate, "arguments", {}) or {})
    if not isinstance(candidate_args, dict):
        return False
    for edge in candidate_args.get("target_relation_edges") or []:
        edge_payload = _payload_for_prompt(edge)
        if not isinstance(edge_payload, dict):
            continue
        if _relation_edge_present(current_sql, edge_payload):
            continue
        endpoints = _relation_edge_endpoints(edge_payload)
        if not endpoints:
            continue
        left_table, _left_column, right_table, _right_column = endpoints
        if left_table.lower() in target_tables or right_table.lower() in target_tables:
            return True
    return False


def _action_dedupe_key(action: Any) -> Optional[tuple[str, str, str]]:
    primitive = str(getattr(getattr(action, "primitive", ""), "value", getattr(action, "primitive", "")))
    args = getattr(action, "arguments", {}) or {}
    source_group_id = str(getattr(action, "source_group_id", "") or "")
    if primitive in {"DROP_SELECT_SLOT", "DROP_SIDE"}:
        from_exprs = [
            str(value).strip().lower()
            for value in (args.get("from_exprs") or [])
            if str(value).strip()
        ]
        if from_exprs:
            return ("drop_select_exprs", source_group_id, "|".join(sorted(from_exprs)))
        from_expr = str(args.get("from_expr") or "").strip().lower()
        if from_expr:
            return ("drop_select_expr", source_group_id, from_expr)
        from_predicate = str(args.get("from_predicate") or "").strip().lower()
        to_predicate = str(args.get("to_predicate") or "").strip().lower()
        if from_predicate and to_predicate:
            return ("drop_where_side", source_group_id, f"{from_predicate}->{to_predicate}")
    return None


def _deduplicate_semantic_actions(actions: List[Any]) -> tuple[List[Any], List[str]]:
    """Collapse duplicate actions that realize the same code-enumerated edit.

    This is a generic post-selection validator: it does not choose among
    different edits, only removes duplicate representations of the same edit
    target (for example DROP_SELECT_SLOT and DROP_SIDE both dropping the same
    SELECT expression).
    """
    seen: set[tuple[str, str, str]] = set()
    kept: List[Any] = []
    dropped: List[str] = []
    for action in actions:
        key = _action_dedupe_key(action)
        if key is not None and key in seen:
            dropped.append(
                str(getattr(action, "action_id", "") or getattr(action, "selected_candidate_id", ""))
            )
            continue
        if key is not None:
            seen.add(key)
        kept.append(action)
    return kept, dropped


def _group_type_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _group_max_actions(group: Any) -> int:
    contract = getattr(group, "trigger_contract", None)
    max_actions = getattr(contract, "max_actions", None)
    if max_actions is None and isinstance(contract, dict):
        max_actions = contract.get("max_actions")
    try:
        parsed = int(max_actions)
        if parsed > 0:
            return min(parsed, 3)
    except Exception:
        pass

    group_type = _group_type_value(getattr(group, "group_type", ""))
    if group_type == "family":
        return 3
    return 1


def _synthesized_program_op_ids(group: Any) -> set[str]:
    instantiation = getattr(group, "instantiation_program", None)
    program = getattr(instantiation, "synthesized_program", None)
    ops = getattr(program, "ops", None)
    if ops is None and isinstance(program, dict):
        ops = program.get("ops")
    return {
        str(getattr(op, "op_id", "") or (op.get("op_id") if isinstance(op, dict) else "") or "")
        for op in (ops or [])
        if str(getattr(op, "op_id", "") or (op.get("op_id") if isinstance(op, dict) else "") or "")
    }


def _payload_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return dict(getattr(value, "__dict__", {}) or {})


def _synthesized_program_bundle_ids(group: Any) -> set[str]:
    instantiation = getattr(group, "instantiation_program", None)
    program = getattr(instantiation, "synthesized_program", None)
    envelope = _payload_dict(getattr(program, "program_envelope", None))
    action_envelope = _payload_dict(envelope.get("action_envelope"))
    bundle_ids = {
        str(_payload_dict(bundle).get("bundle_id") or "")
        for bundle in (action_envelope.get("bundles") or [])
        if str(_payload_dict(bundle).get("bundle_id") or "")
    }
    if bundle_ids:
        return bundle_ids
    return _synthesized_program_op_ids(group)


def _synthesized_program_bundle_lookup(group: Any) -> Dict[str, str]:
    instantiation = getattr(group, "instantiation_program", None)
    program = getattr(instantiation, "synthesized_program", None)
    envelope = _payload_dict(getattr(program, "program_envelope", None))
    action_envelope = _payload_dict(envelope.get("action_envelope"))
    lookup: Dict[str, str] = {}
    for bundle in (action_envelope.get("bundles") or []):
        payload = _payload_dict(bundle)
        bundle_id = str(payload.get("bundle_id") or "")
        if not bundle_id:
            continue
        for op_id in payload.get("bundled_op_ids") or []:
            if str(op_id):
                lookup[str(op_id)] = bundle_id
        for op_id in payload.get("cleanup_op_ids") or []:
            if str(op_id):
                lookup[str(op_id)] = bundle_id
    return lookup


def _candidate_bundle_key(candidate: Any) -> str:
    args = getattr(candidate, "arguments", {}) or {}
    if not isinstance(args, dict):
        args = {}
    return (
        str(getattr(candidate, "bundle_id", "") or "")
        or str(args.get("bundle_id") or "")
        or str(args.get("canonical_op_id") or "")
    )


def _candidate_bundle_key_for_group(candidate: Any, group: Any) -> str:
    args = getattr(candidate, "arguments", {}) or {}
    if not isinstance(args, dict):
        args = {}
    explicit = str(getattr(candidate, "bundle_id", "") or "") or str(args.get("bundle_id") or "")
    if explicit:
        return explicit
    op_id = str(args.get("canonical_op_id") or "")
    if not op_id:
        return ""
    return _synthesized_program_bundle_lookup(group).get(op_id, op_id)


def _candidate_bundle_selection_key(candidate: Any) -> str:
    args = getattr(candidate, "arguments", {}) or {}
    if not isinstance(args, dict):
        args = {}
    return str(getattr(candidate, "bundle_selection_key", "") or "") or str(
        args.get("bundle_selection_key") or ""
    )


def _bundle_primary_match(candidate_set: Any, candidate: Any) -> bool:
    args = getattr(candidate, "arguments", {}) or {}
    if not isinstance(args, dict):
        args = {}
    primary = str(getattr(candidate, "bundle_primary_primitive", "") or "") or str(
        args.get("bundle_primary_primitive") or ""
    )
    primitive = str(getattr(getattr(candidate_set, "primitive", None), "value", getattr(candidate_set, "primitive", "")))
    return bool(primary and primary == primitive)


def _group_action_contract(group: Any) -> Dict[str, Any]:
    contract = getattr(group, "trigger_contract", None)
    action_contract = getattr(contract, "action_contract", None)
    if action_contract is None and isinstance(contract, dict):
        action_contract = contract.get("action_contract")
    return dict(action_contract or {}) if isinstance(action_contract, dict) else {}


def _group_selection_policy(group: Any) -> str:
    action_contract = _group_action_contract(group)
    policy = str(action_contract.get("selection_policy") or "llm_required").strip()
    return policy if policy in {"llm_required", "deterministic_allowed", "deterministic_only"} else "llm_required"


def _group_compiler_deterministic(group: Any) -> bool:
    action_contract = _group_action_contract(group)
    return bool(action_contract.get("compiler_deterministic"))


def _deterministic_canonical_fallback_actions(
    *,
    candidate_sets: List[Any],
    memory_objects: List[Any],
    Action: Any,
    EditScope: Any,
    GroupType: Any,
    RiskLevel: Any,
) -> tuple[List[Any], List[str]]:
    """Select a pre-enumerated canonical candidate only when binding is unique.

    This does not invent arguments. It only chooses candidates already produced
    by ActionCompiler lowering for synthesized canonical programs. If a
    canonical op has multiple schema-legal candidates, this function deliberately
    refuses to choose: that is an instantiation ambiguity and must be resolved by
    the selector or reported to replay.
    """
    group_by_id = {
        str(getattr(group, "group_id", "") or ""): group
        for group in (memory_objects or [])
        if str(getattr(group, "group_id", "") or "")
    }
    required_by_group = {
        group_id: _synthesized_program_bundle_ids(group)
        for group_id, group in group_by_id.items()
    }
    if not any(required_by_group.values()):
        return [], []

    by_op: Dict[tuple[str, str], List[tuple[Any, Any]]] = {}
    for candidate_set in candidate_sets or []:
        for candidate in getattr(candidate_set, "candidates", []) or []:
            if not bool(getattr(candidate, "schema_legal", True)):
                continue
            args = getattr(candidate, "arguments", {}) or {}
            if not isinstance(args, dict):
                continue
            group_id = str(getattr(candidate, "source_group_id", "") or "")
            group = group_by_id.get(group_id)
            op_id = _candidate_bundle_key_for_group(candidate, group)
            if not op_id or not group_id:
                continue
            if op_id not in required_by_group.get(group_id, set()):
                continue
            by_op.setdefault((group_id, op_id), []).append((candidate_set, candidate))
    if not by_op:
        return [], ["no_unique_canonical_candidate:no_candidates"]

    selected: List[Any] = []
    notes: List[str] = []
    action_counts_by_group: Dict[str, int] = {}
    for (group_id, op_id), rows in sorted(by_op.items()):
        group = group_by_id.get(group_id)
        selection_policy = _group_selection_policy(group)
        if selection_policy != "deterministic_only":
            notes.append(f"{group_id}:{op_id}:deterministic_selection_not_allowed")
            continue
        if not _group_compiler_deterministic(group):
            notes.append(f"{group_id}:{op_id}:compiler_not_deterministic")
            continue
        preferred_rows = [row for row in rows if _bundle_primary_match(row[0], row[1])]
        if len(preferred_rows) == 1:
            candidate_set, candidate = preferred_rows[0]
        elif len(rows) == 1:
            candidate_set, candidate = rows[0]
        else:
            selection_keys = {
                _candidate_bundle_selection_key(candidate)
                for _candidate_set, candidate in rows
                if _candidate_bundle_selection_key(candidate)
            }
            if len(selection_keys) == 1:
                candidate_set, candidate = rows[0]
            else:
                notes.append(
                    f"{group_id}:{op_id}:ambiguous_candidate_count={len(rows)}"
                )
                continue
        max_actions = _group_max_actions(group)
        if action_counts_by_group.get(group_id, 0) >= max_actions:
            notes.append(f"{getattr(candidate, 'candidate_id', '')}:capacity")
            continue
        args = dict(getattr(candidate, "arguments", {}) or {})
        note_id = str(args.get("canonical_op_id") or "") or op_id
        if not args.get("bundle_id"):
            args["bundle_id"] = op_id
        allowed_scope: List[Any] = []
        for scope in args.get("required_edit_scopes") or []:
            try:
                allowed_scope.append(EditScope(str(scope)))
            except Exception:
                continue
        src_type = getattr(candidate, "source_group_type", None)
        if src_type is None:
            group = group_by_id.get(group_id)
            src_type = getattr(group, "group_type", None) or GroupType.PATTERN
        selected.append(
            Action(
                action_id=f"auto-canonical-{len(selected) + 1}",
                source_group_id=group_id,
                source_group_type=src_type,
                primitive=getattr(candidate_set, "primitive", None),
                arguments=args,
                selected_candidate_id=str(getattr(candidate, "candidate_id", "") or ""),
                rationale_short=(
                    "deterministic fallback selected a schema-legal candidate "
                    "for a synthesized canonical repair op"
                ),
                priority=0.5,
                risk=RiskLevel.MEDIUM,
                allowed_edit_scope=allowed_scope,
                used_escape_hatch=False,
                selection_origin="deterministic_unique",
                selection_policy=selection_policy,
                fallback_used=True,
                fallback_reason="llm_selected_no_actions_unique_canonical_candidate",
            )
        )
        action_counts_by_group[group_id] = action_counts_by_group.get(group_id, 0) + 1
        notes.append(f"{group_id}:{note_id}:{getattr(candidate, 'candidate_id', '')}")

    return selected, notes


def _required_canonical_op_ids(memory_objects: List[Any]) -> set[str]:
    return {
        op_id
        for group in (memory_objects or [])
        for op_id in _synthesized_program_bundle_ids(group)
        if op_id
    }


def _selected_canonical_op_ids(actions: List[Any]) -> set[str]:
    return {
        str(getattr(action, "arguments", {}).get("bundle_id") or "")
        or str(getattr(action, "arguments", {}).get("canonical_op_id") or "")
        for action in (actions or [])
        if (
            str(getattr(action, "arguments", {}).get("bundle_id") or "")
            or str(getattr(action, "arguments", {}).get("canonical_op_id") or "")
        )
    }


def _deterministic_unique_preselection(
    *,
    candidate_sets: List[Any],
    memory_objects: List[Any],
    Action: Any,
    EditScope: Any,
    GroupType: Any,
    RiskLevel: Any,
) -> tuple[List[Any], List[str]]:
    actions, notes = _deterministic_canonical_fallback_actions(
        candidate_sets=candidate_sets,
        memory_objects=memory_objects,
        Action=Action,
        EditScope=EditScope,
        GroupType=GroupType,
        RiskLevel=RiskLevel,
    )
    required_ids = _required_canonical_op_ids(memory_objects)
    if not required_ids:
        return [], notes
    if _selected_canonical_op_ids(actions) >= required_ids:
        return actions, notes
    return [], notes


def _selection_summary(
    *,
    actions: List[Any],
    raw_action_count: int,
    final_action_count: int,
    skipped_actions: List[str],
) -> Dict[str, Any]:
    origins: Dict[str, int] = {}
    policies: Dict[str, int] = {}
    fallback_reasons: Dict[str, int] = {}
    for action in actions:
        origin = str(getattr(action, "selection_origin", None) or "none")
        policy = str(getattr(action, "selection_policy", None) or "llm_required")
        origins[origin] = origins.get(origin, 0) + 1
        policies[policy] = policies.get(policy, 0) + 1
        if bool(getattr(action, "fallback_used", False)):
            reason = str(getattr(action, "fallback_reason", None) or "fallback")
            fallback_reasons[reason] = fallback_reasons.get(reason, 0) + 1
    return {
        "raw_action_count": raw_action_count,
        "final_action_count": final_action_count,
        "selection_origins": origins,
        "selection_policies": policies,
        "fallback_used_count": sum(fallback_reasons.values()),
        "fallback_reasons": fallback_reasons,
        "skipped_actions": list(skipped_actions),
    }


def _action_sort_key(item: tuple[int, Any]) -> tuple[float, int, int, int]:
    index, action = item
    risk_value = _group_type_value(getattr(action, "risk", "medium"))
    risk_rank = {"low": 2, "medium": 1, "high": 0}.get(risk_value, 1)
    selected = 1 if getattr(action, "selected_candidate_id", None) else 0
    try:
        priority = float(getattr(action, "priority", 0.5) or 0.5)
    except Exception:
        priority = 0.5
    return (priority, selected, risk_rank, -index)


def _enforce_action_count_contract(
    actions: List[Any],
    memory_objects: List[Any],
) -> tuple[List[Any], List[str]]:
    """Apply group-level action count contracts after LLM selection.

    The LLM still performs semantic selection, but the compiler output must
    obey the memory object's contract before it reaches rewrite.
    """
    if not actions:
        return actions, []

    limits = {
        str(getattr(group, "group_id", "") or ""): _group_max_actions(group)
        for group in memory_objects or []
        if str(getattr(group, "group_id", "") or "")
    }

    grouped: Dict[str, List[tuple[int, Any]]] = {}
    for index, action in enumerate(actions):
        source_group_id = str(getattr(action, "source_group_id", "") or "")
        grouped.setdefault(source_group_id, []).append((index, action))

    keep_indexes: set[int] = set()
    dropped: List[str] = []
    for source_group_id, rows in grouped.items():
        fallback_type = _group_type_value(getattr(rows[0][1], "source_group_type", "singleton"))
        max_actions = limits.get(source_group_id, 3 if fallback_type == "family" else 1)
        if len(rows) <= max_actions:
            keep_indexes.update(index for index, _ in rows)
            continue
        ranked = sorted(rows, key=_action_sort_key, reverse=True)
        keep_indexes.update(index for index, _ in ranked[:max_actions])
        for _, action in ranked[max_actions:]:
            dropped.append(
                str(getattr(action, "action_id", "") or getattr(action, "selected_candidate_id", ""))
            )

    kept = [action for index, action in enumerate(actions) if index in keep_indexes]
    if len(kept) > 3:
        ranked = sorted(enumerate(kept), key=_action_sort_key, reverse=True)
        keep_global = {index for index, _ in ranked[:3]}
        for index, action in ranked[3:]:
            dropped.append(
                str(getattr(action, "action_id", "") or getattr(action, "selected_candidate_id", ""))
            )
        kept = [action for index, action in enumerate(kept) if index in keep_global]

    return kept, dropped


# ------------------- Node 1: Wrong Case Auditor -------------------


def run_wrong_case_auditor(
    *,
    question: str,
    evidence: str,
    pred_sql: str,
    gold_sql: str,
    local_schema_view,
    execution_result: str = "",
    execution_comparison=None,
):
    """调 LLM 做 wrong case audit。

    参数
    ----
    execution_result     : 序列化后的执行证据字符串（喂给 Auditor prompt）
    execution_comparison : 结构化 ExecutionComparison；会原样挂到 CaseAudit.execution_comparison
                          供 Extractor 独立读取。

    返回
    ----
    CaseAudit
    """
    from .data_structures_v2 import CaseAudit, Confidence, Locus
    from .prompts_v2 import build_wrong_case_auditor_prompt
    from .llm_utils_v2 import call_llm

    prompt = build_wrong_case_auditor_prompt(
        question=question,
        evidence=evidence or "",
        pred_sql=pred_sql,
        gold_sql=gold_sql,
        local_schema_view_json=_json_dump_prompt(local_schema_view),
        execution_result=execution_result or "",
    )

    raw: Dict[str, Any] = _call_llm_json(
        prompt,
        stage="wrong_case_auditor",
        trace_context={"case_id": getattr(local_schema_view, "case_id", "")},
    )  # type: ignore[assignment]

    # 容错：optional 字段缺失就填默认
    locus_hint_raw = (raw.get("error_locus_hint") or "").strip()
    locus_hint: Optional[Locus] = None
    if locus_hint_raw:
        try:
            locus_hint = _coerce_locus(locus_hint_raw)
        except Exception:
            locus_hint = None

    confidence_raw = (raw.get("confidence") or "low").strip().lower()
    try:
        confidence = Confidence(confidence_raw)
    except Exception:
        confidence = Confidence.LOW

    return CaseAudit(
        db_id=local_schema_view.db_id if hasattr(local_schema_view, "db_id") else "",
        case_id="",  # 由 caller 设置（该节点只负责语义字段）
        question=question,
        evidence=evidence or None,
        pred_sql=pred_sql,
        gold_sql=gold_sql,
        execution_trace=execution_result or None,
        execution_comparison=execution_comparison,
        final_error_reason=str(raw.get("final_error_reason") or "").strip(),
        minimal_fix=str(raw.get("minimal_fix") or "").strip(),
        candidate_fix_sql=(
            str(raw.get("candidate_fix_sql")).strip()
            or str(raw.get("validated_sql")).strip()
            or None
        )
        if raw.get("candidate_fix_sql") is not None or raw.get("validated_sql") is not None
        else None,
        minimal_patch_ops=[
            dict(item)
            for item in (raw.get("minimal_patch_ops") or [])
            if isinstance(item, dict)
        ],
        effect_axis_hint=(
            str(raw.get("effect_axis_hint")).strip()
            if raw.get("effect_axis_hint") is not None
            else None
        ),
        secondary_differences=[
            str(item).strip()
            for item in (raw.get("secondary_differences") or [])
            if str(item).strip()
        ],
        validated_sql=(str(raw.get("validated_sql")).strip() or None) if raw.get("validated_sql") else None,
        error_locus_hint=locus_hint,
        confidence=confidence,
    )


# ------------------- Node 2: ErrorInstance Extractor -------------------


def run_error_instance_extractor(
    *,
    runtime_case_view,
    case_audit,
    code_prepared: Dict[str, Any],
):
    """调 LLM 做 ErrorInstance 抽取，产出 ErrorInstanceV2 对象。

    ``code_prepared`` 由 ``code_preprocess_v2.preprocess_case`` 的返回构成，至少含：
    - structure_flags (Dict[str, bool])
    - legacy_signature (str)
    - candidate_question_tags (List[str])
    - candidate_pred_tags (List[str])
    """
    from .data_structures_v2 import (
        BranchRule,
        ErrorInstanceV2,
        Guardrail,
        InstantiationSlot,
        PossibleEffectAxis,
        PredSqlFeatures,
        QuestionFeatures,
        RepairInsightSignature,
        RepairProgramStep,
        RepairSkeleton,
        RepairSkeletonSemantic,
        RepairSkeletonStructural,
        RiskLevel,
    )
    from .prompts_v2 import build_error_instance_extractor_prompt
    from .vocabulary_v2 import Locus, OpFamily, OutputContract, TargetFamily
    from .llm_utils_v2 import call_llm

    # prompt 里 embed 的 code_prepared payload 保持紧凑
    code_payload = {
        "structure_flags": code_prepared.get("structure_flags", {}),
        "legacy_signature": code_prepared.get("legacy_signature", ""),
        "candidate_question_tags": code_prepared.get("candidate_question_tags", []),
        "candidate_pred_tags": code_prepared.get("candidate_pred_tags", []),
    }

    # execution_comparison 原样透传给 Extractor——不从 case_audit 里递归 dump（体积
    # 更可控），保证 Extractor 能 **独立读取** 执行证据，不必只依赖 minimal_fix。
    exec_cmp_payload = "(not available)"
    if getattr(case_audit, "execution_comparison", None) is not None:
        exec_cmp_payload = _json_dump(case_audit.execution_comparison)

    prompt = build_error_instance_extractor_prompt(
        runtime_case_view_json=_json_dump_prompt_contract(runtime_case_view),
        case_audit_json=_json_dump_prompt(_compact_case_audit_for_extractor(case_audit)),
        code_prepared_json=_json_dump(code_payload),
        locus_enum=_enum_values(Locus),
        op_family_enum=_enum_values(OpFamily),
        target_family_enum=_enum_values(TargetFamily),
        output_contract_enum="UNCHANGED",
        execution_comparison_json=exec_cmp_payload,
    )

    raw: Dict[str, Any] = _call_llm_json(
        prompt,
        stage="error_instance_extractor",
        trace_context={"case_id": getattr(runtime_case_view, "case_id", "")},
    )  # type: ignore[assignment]

    # question_features / pred_sql_features
    qf_raw = raw.get("question_features") or {}
    pf_raw = raw.get("pred_sql_features") or {}

    question_features = QuestionFeatures(
        decisive_tags=list(qf_raw.get("decisive_tags") or []),
        descriptive_tags=list(qf_raw.get("descriptive_tags") or []),
        summary=(qf_raw.get("summary") or None),
    )
    pred_sql_features = PredSqlFeatures(
        decisive_tags=list(pf_raw.get("decisive_tags") or []),
        descriptive_tags=list(pf_raw.get("descriptive_tags") or []),
        summary=(pf_raw.get("summary") or None),
        structure_flags=dict(
            pf_raw.get("structure_flags") or code_prepared.get("structure_flags") or {}
        ),
    )

    # repair_skeleton
    rs_raw = raw.get("repair_skeleton") or {}
    rs_struct_raw = rs_raw.get("structural") or {}
    rs_sem_raw = rs_raw.get("semantic") or {}
    try:
        structural = RepairSkeletonStructural(
            locus=_coerce_locus(rs_struct_raw.get("locus")),
            op_family=OpFamily(str(rs_struct_raw.get("op_family"))),
            target_family=TargetFamily(str(rs_struct_raw.get("target_family"))),
            output_contract=_coerce_output_contract(rs_struct_raw.get("output_contract")),
            output_shape_delta=_output_shape_delta_from_code_prepared(code_prepared),
            legacy_signature=(rs_struct_raw.get("legacy_signature") or code_prepared.get("legacy_signature")),
        )
    except Exception as exc:
        raise RuntimeError(
            f"Invalid repair_skeleton.structural from LLM: {rs_struct_raw!r} ({exc})"
        ) from exc

    semantic = RepairSkeletonSemantic(
        intent=str(rs_sem_raw.get("intent") or "").strip() or "(intent missing)",
        family_hint=(rs_sem_raw.get("family_hint") or None),
        notes=(rs_sem_raw.get("notes") or None),
    )

    repair_skeleton = RepairSkeleton(structural=structural, semantic=semantic)

    # instantiation_slots
    instantiation_slots: List[InstantiationSlot] = []
    for slot_raw in raw.get("instantiation_slots") or []:
        try:
            instantiation_slots.append(
                InstantiationSlot(
                    name=str(slot_raw.get("name")),
                    kind=str(slot_raw.get("kind")),
                    required=bool(slot_raw.get("required", True)),
                    allowed_role_families=list(slot_raw.get("allowed_role_families") or []),
                    description=slot_raw.get("description"),
                )
            )
        except Exception:
            continue

    branch_rules: List[BranchRule] = []
    for br_raw in raw.get("branch_rules") or []:
        try:
            branch_rules.append(
                BranchRule(
                    if_condition=str(br_raw.get("if_condition") or ""),
                    then_action=str(br_raw.get("then_action") or ""),
                    else_action=br_raw.get("else_action"),
                )
            )
        except Exception:
            continue

    guardrails: List[Guardrail] = []
    for g_raw in raw.get("guardrails") or []:
        try:
            guardrails.append(
                Guardrail(
                    description=str(g_raw.get("description") or ""),
                    kind=str(g_raw.get("kind") or "negative_evidence"),
                )
            )
        except Exception:
            continue

    def _parse_step_slots(values: Any) -> List[InstantiationSlot]:
        parsed: List[InstantiationSlot] = []
        for slot_raw in values or []:
            if isinstance(slot_raw, InstantiationSlot):
                parsed.append(slot_raw)
                continue
            if not isinstance(slot_raw, dict):
                continue
            try:
                parsed.append(
                    InstantiationSlot(
                        name=str(slot_raw.get("name")),
                        kind=str(slot_raw.get("kind")),
                        required=bool(slot_raw.get("required", True)),
                        allowed_role_families=list(slot_raw.get("allowed_role_families") or []),
                        description=slot_raw.get("description"),
                    )
                )
            except Exception:
                continue
        return parsed

    def _parse_step_guards(values: Any) -> List[Guardrail]:
        parsed: List[Guardrail] = []
        for guard_raw in values or []:
            if isinstance(guard_raw, Guardrail):
                parsed.append(guard_raw)
                continue
            if not isinstance(guard_raw, dict):
                continue
            try:
                parsed.append(
                    Guardrail(
                        description=str(guard_raw.get("description") or ""),
                        kind=str(guard_raw.get("kind") or "scope_limit"),
                    )
                )
            except Exception:
                continue
        return parsed

    def _parse_source_evidence(values: Any) -> List[str]:
        if values is None:
            return []
        if isinstance(values, str):
            text = values.strip()
            return [text] if text else []
        return [str(value).strip() for value in values or [] if str(value).strip()]

    # repair_program is the only place where dependency edits may enter memory.
    repair_program: List[RepairProgramStep] = []
    for index, step_raw in enumerate(raw.get("repair_program") or []):
        if not isinstance(step_raw, dict):
            continue
        step_op = str(step_raw.get("op") or "").strip()
        if not step_op:
            # Missing op means the case did not expose an executable edit DSL
            # step. Do not synthesize one from the repair skeleton.
            continue
        try:
            repair_program.append(
                RepairProgramStep(
                    step_id=str(step_raw.get("step_id") or f"step_{index + 1}"),
                    op=step_op,
                    locus=str(step_raw.get("locus") or structural.locus.value),
                    is_dependency=bool(step_raw.get("is_dependency") or False),
                    required=bool(step_raw.get("required", True)),
                    slots=_parse_step_slots(step_raw.get("slots")),
                    guards=_parse_step_guards(step_raw.get("guards")),
                    arguments=dict(step_raw.get("arguments") or {}),
                    source_evidence=_parse_source_evidence(step_raw.get("source_evidence")),
                    origin=str(step_raw.get("origin") or "case_extracted"),
                    extraction_source=str(step_raw.get("extraction_source") or "llm_explicit"),
                    supporting_case_ids=[
                        str(v)
                        for v in (step_raw.get("supporting_case_ids") or [runtime_case_view.case_id])
                        if str(v)
                    ],
                )
            )
        except Exception:
            continue

    risk_level_raw = (raw.get("risk_level") or "medium").strip().lower()
    try:
        risk_level = RiskLevel(risk_level_raw)
    except Exception:
        risk_level = RiskLevel.MEDIUM

    possible_effect_axes: List[PossibleEffectAxis] = []
    for axis_raw in raw.get("possible_effect_axes") or []:
        if not isinstance(axis_raw, dict):
            continue
        axis = str(axis_raw.get("axis") or "").strip()
        if not axis:
            continue
        try:
            possible_effect_axes.append(
                PossibleEffectAxis(
                    axis=axis,
                    source_state_summary=axis_raw.get("source_state_summary"),
                    target_state_summary=axis_raw.get("target_state_summary"),
                    delta_summary=axis_raw.get("delta_summary"),
                    primary_likelihood=axis_raw.get("primary_likelihood"),
                    why=axis_raw.get("why"),
                )
            )
        except Exception:
            continue

    repair_insight_signature = None
    insight_raw = raw.get("repair_insight_signature")
    if isinstance(insight_raw, dict) and insight_raw:
        try:
            repair_insight_signature = RepairInsightSignature(
                interface_key=str(insight_raw.get("interface_key") or "").strip(),
                source_misread=str(insight_raw.get("source_misread") or "").strip(),
                target_preference=str(insight_raw.get("target_preference") or "").strip(),
                repair_interface=str(insight_raw.get("repair_interface") or "").strip(),
                binding_slots=[
                    dict(item)
                    for item in _dict_list(insight_raw.get("binding_slots"), limit=12)
                    if isinstance(item, dict)
                ],
                preserve_invariants=_string_list(
                    insight_raw.get("preserve_invariants"), limit=20
                ),
                negative_guards=_string_list(
                    insight_raw.get("negative_guards"), limit=20
                ),
                axis_links=[
                    dict(item)
                    for item in _dict_list(insight_raw.get("axis_links"), limit=12)
                    if isinstance(item, dict)
                ],
                evidence=dict(insight_raw.get("evidence") or {}),
                confidence=str(insight_raw.get("confidence") or "medium").strip().lower()
                or "medium",
            )
        except Exception:
            repair_insight_signature = None

    return ErrorInstanceV2(
        db_id=runtime_case_view.db_id,
        case_id=runtime_case_view.case_id,
        question_features=question_features,
        pred_sql_features=pred_sql_features,
        deep_bias=str(raw.get("deep_bias") or "").strip() or "(deep_bias missing)",
        repair_goal=str(raw.get("repair_goal") or "").strip() or "(repair_goal missing)",
        repair_skeleton=repair_skeleton,
        instantiation_slots=instantiation_slots,
        branch_rules=branch_rules,
        guardrails=guardrails,
        repair_program=repair_program,
        source_antipattern_hypothesis=dict(raw.get("source_antipattern_hypothesis") or {}),
        target_invariant_hypothesis=dict(raw.get("target_invariant_hypothesis") or {}),
        possible_effect_axes=possible_effect_axes,
        repair_insight_signature=repair_insight_signature,
        core_vs_accessory=dict(raw.get("core_vs_accessory") or {}),
        uncertain_axes=[
            str(item).strip()
            for item in (raw.get("uncertain_axes") or [])
            if str(item).strip()
        ],
        rewrite_hint_proto=str(raw.get("rewrite_hint_proto") or "").strip(),
        risk_level=risk_level,
    )


# ------------------- Node 3: Action Compiler -------------------


def run_action_compiler(
    *,
    runtime_case_view,
    memory_objects: List[Any],
    precomputed_candidate_sets: Optional[List[Any]] = None,
    precomputed_schema_diagnostics: Optional[Any] = None,
    trigger_result: Any = None,
):
    """Phase 1.4（δ）：消费 List[GroupSummary] 做 candidate 预枚举 + LLM selection。

    参数
    ----
    memory_objects :
        已从 library 检索到的 GroupSummary 列表（已通过 trigger_signature 匹配）。
        空列表意味着 runtime 没匹配到 memory，Compiler 会产空 candidate_sets，
        caller 应直接 skip rewrite（等效 C0 passthrough）。
    """
    from .action_compiler_v2 import enumerate_candidates
    from .data_structures_v2 import (
        Action,
        ActionCompilerOutput,
        EditScope,
        GroupType,
        LocalSchemaViewDiagnostics,
        RiskLevel,
    )
    from .prompts_v2 import build_action_compiler_prompt
    from .llm_utils_v2 import call_llm

    # 1. 代码预枚举（多 Group merge；每个 candidate 自带 source_group_id）
    if precomputed_candidate_sets is None or precomputed_schema_diagnostics is None:
        candidate_sets, pre_diag = enumerate_candidates(
            case_view=runtime_case_view, memory_objects=memory_objects
        )
    else:
        candidate_sets = precomputed_candidate_sets
        pre_diag = precomputed_schema_diagnostics

    candidate_by_id: Dict[str, Any] = {}
    primitive_by_candidate_id: Dict[str, Any] = {}
    group_by_id = {
        str(getattr(group, "group_id", "") or ""): group
        for group in (memory_objects or [])
        if str(getattr(group, "group_id", "") or "")
    }
    for cs in candidate_sets:
        for cand in getattr(cs, "candidates", []) or []:
            cid = str(getattr(cand, "candidate_id", "") or "")
            if not cid:
                continue
            candidate_by_id[cid] = cand
            primitive_by_candidate_id[cid] = getattr(cs, "primitive", None)

    skipped_actions: List[str] = []
    seen_candidate_ids: set[str] = set()
    actions: List[Action] = []
    raw_diag: Dict[str, Any] = {}
    raw_action_count = 0

    preselected_actions, preselected_notes = _deterministic_unique_preselection(
        candidate_sets=candidate_sets,
        memory_objects=memory_objects or [],
        Action=Action,
        EditScope=EditScope,
        GroupType=GroupType,
        RiskLevel=RiskLevel,
    )
    if preselected_actions:
        actions.extend(preselected_actions)
        seen_candidate_ids.update(
            str(getattr(action, "selected_candidate_id", "") or "")
            for action in preselected_actions
            if str(getattr(action, "selected_candidate_id", "") or "")
        )
        skipped_actions.append(
            "deterministic_preselected:" + ",".join(preselected_notes or ["unique_exact_candidates"])
        )
        raw_action_count = len(actions)
    else:
        # 2. 构 prompt 并调 LLM. The prompt payload is an explicit runtime DTO:
        # offline member evidence remains in logs/library, not in the LLM context.
        prompt_payloads = _action_compiler_prompt_payloads(
            runtime_case_view=runtime_case_view,
            memory_objects=memory_objects or [],
            candidate_sets=candidate_sets,
            schema_diagnostics_pre=pre_diag,
            trigger_result=trigger_result,
        )
        prompt = build_action_compiler_prompt(
            runtime_case_view_json=_json_dump_prompt(prompt_payloads["runtime_case_view"]),
            memory_objects_json=_json_dump_prompt(prompt_payloads["memory_objects"]),
            candidate_sets_json=_json_dump_prompt(prompt_payloads["candidate_sets"]),
            schema_diagnostics_pre_json=_json_dump_prompt(prompt_payloads["schema_diagnostics_pre"]),
        )
        raw: Dict[str, Any] = _call_llm_json(
            prompt,
            stage="action_compiler",
            trace_context={
                "case_id": getattr(runtime_case_view, "case_id", ""),
                "memory_object_count": len(memory_objects or []),
                "candidate_set_count": len(candidate_sets or []),
            },
        )  # type: ignore[assignment]
        raw_diag = raw.get("schema_diagnostics") or {}

        # 3. 反序列化 actions
        for item in raw.get("actions", []) or []:
            try:
                selected_cand_id = (
                    str(item.get("selected_candidate_id")).strip()
                    if item.get("selected_candidate_id") is not None
                    else None
                )
                used_escape_hatch = bool(item.get("used_escape_hatch") or False)
                selected_candidate = candidate_by_id.get(selected_cand_id or "")

                if used_escape_hatch:
                    skipped_actions.append("escape_hatch_disabled")
                    continue
                elif not selected_cand_id:
                    skipped_actions.append("missing_selected_candidate_id")
                    continue
                elif selected_candidate is None:
                    skipped_actions.append(f"unknown_selected_candidate_id:{selected_cand_id}")
                    continue
                elif selected_cand_id in seen_candidate_ids:
                    skipped_actions.append(f"duplicate_selected_candidate_id:{selected_cand_id}")
                    continue
                else:
                    seen_candidate_ids.add(selected_cand_id)

                primitive = primitive_by_candidate_id.get(selected_cand_id or "") or item.get("primitive")
                arguments = item.get("arguments") or {}
                if selected_candidate is not None:
                    # The compiler contract is "code enumerates, LLM selects".
                    # Do not trust free-form LLM arguments when a candidate id is present.
                    arguments = dict(getattr(selected_candidate, "arguments", {}) or {})
                allowed_scope_raw = item.get("allowed_edit_scope") or []
                if selected_candidate is not None:
                    for scope in (arguments.get("required_edit_scopes") or []):
                        if scope not in allowed_scope_raw:
                            allowed_scope_raw.append(scope)
                allowed_scope: List[EditScope] = []
                for s in allowed_scope_raw:
                    try:
                        allowed_scope.append(EditScope(str(s)))
                    except Exception:
                        continue
                risk_raw = (item.get("risk") or "medium").strip().lower()
                try:
                    risk = RiskLevel(risk_raw)
                except Exception:
                    risk = RiskLevel.MEDIUM
                src_type_raw = (item.get("source_group_type") or "singleton").strip().lower()
                try:
                    src_type = GroupType(src_type_raw)
                except Exception:
                    src_type = GroupType.SINGLETON

                if selected_candidate is not None:
                    sgid = str(getattr(selected_candidate, "source_group_id", "") or "")
                    candidate_source_type = getattr(selected_candidate, "source_group_type", None)
                    if candidate_source_type is not None:
                        src_type = candidate_source_type
                else:
                    sgid = str(item.get("source_group_id") or "")
                selection_policy = _group_selection_policy(group_by_id.get(sgid))

                actions.append(
                    Action(
                        action_id=str(item.get("action_id") or f"auto-{len(actions)}"),
                        source_group_id=sgid,
                        source_group_type=src_type,
                        primitive=primitive,
                        arguments=arguments,
                        selected_candidate_id=selected_cand_id,
                        rationale_short=str(item.get("rationale_short") or "").strip(),
                        priority=float(item.get("priority") or 0.5),
                        risk=risk,
                        allowed_edit_scope=allowed_scope,
                        used_escape_hatch=False,
                        selection_origin="llm_selected",
                        selection_policy=selection_policy,
                        fallback_used=False,
                        fallback_reason=None,
                    )
                )
            except Exception:
                # 单条 action 解析失败不中断
                skipped_actions.append("parse_exception")
                continue

        if not actions:
            fallback_actions, fallback_notes = _deterministic_canonical_fallback_actions(
                candidate_sets=candidate_sets,
                memory_objects=memory_objects or [],
                Action=Action,
                EditScope=EditScope,
                GroupType=GroupType,
                RiskLevel=RiskLevel,
            )
            if fallback_actions:
                actions.extend(fallback_actions)
                seen_candidate_ids.update(
                    str(getattr(action, "selected_candidate_id", "") or "")
                    for action in fallback_actions
                    if str(getattr(action, "selected_candidate_id", "") or "")
                )
                skipped_actions.append(
                    "deterministic_canonical_fallback_selected:"
                    + ",".join(fallback_notes)
                )
            elif fallback_notes:
                skipped_actions.append(
                    "deterministic_canonical_fallback_not_applied:"
                    + ",".join(fallback_notes)
                )
        raw_action_count = len(actions)
    skipped_actions.append("bundle_cleanup_dependency_audit_only")

    actions, deduped_actions = _deduplicate_semantic_actions(actions)
    if deduped_actions:
        skipped_actions.append("deduplicated_semantic_actions:" + ",".join(deduped_actions))
    actions, contract_dropped_actions = _enforce_action_count_contract(actions, memory_objects)
    if contract_dropped_actions:
        skipped_actions.append(
            "action_count_contract_enforced:" + ",".join(contract_dropped_actions)
        )
    final_action_count = len(actions)

    # 4. diagnostics 合并
    notes = raw_diag.get("notes")
    count_note = (
        f"compiler_selection_counts: raw_action_count={raw_action_count}; "
        f"final_action_count={final_action_count}"
    )
    notes = (str(notes or "") + " | " + count_note).strip(" |")
    if skipped_actions:
        suffix = "compiler_selection_validation_skipped=" + ",".join(skipped_actions)
        notes = (str(notes or "") + " | " + suffix).strip(" |")
    diag = LocalSchemaViewDiagnostics(
        missing_bridge_paths=list(raw_diag.get("missing_bridge_paths") or pre_diag.missing_bridge_paths),
        missing_column_candidates=list(raw_diag.get("missing_column_candidates") or pre_diag.missing_column_candidates),
        missing_role_family_matches=list(raw_diag.get("missing_role_family_matches") or []),
        two_hop_extension_denied=list(raw_diag.get("two_hop_extension_denied") or []),
        notes=notes,
    )

    return ActionCompilerOutput(
        actions=actions,
        schema_diagnostics=diag,
        escape_hatch_log=None,
        selection_summary=_selection_summary(
            actions=actions,
            raw_action_count=raw_action_count,
            final_action_count=final_action_count,
            skipped_actions=skipped_actions,
        ),
    )


# ------------------- Node 4: Memory Rewrite (bounded autonomy) -------------------


def run_memory_rewrite(
    *,
    question: str,
    evidence: str,
    c0_top1_sql: str,
    actions: List[Any],
    local_schema_view,
    natural_language_hint: str = "",
):
    """调 LLM 做 bounded-autonomy 的 Memory Rewrite。

    返回 dict：{
      "rewrite_sql": str,
      "action_realization_traces": List[ActionRealizationTrace],
      "contract_steps_applied": List[str],
      "notes": Optional[str],
    }

    空 actions 直接返回 c0_top1_sql（等效 passthrough）——不走 LLM，保证 runtime
    在 memory 未命中时无开销。
    """
    from .data_structures_v2 import ActionRealizationTrace, EditScope, SqlEditTrace
    from .prompts_v2 import build_memory_rewrite_prompt
    from .llm_utils_v2 import call_llm

    if not actions:
        return {
            "rewrite_sql": c0_top1_sql,
            "action_realization_traces": [],
            "contract_steps_applied": [],
            "dependency_repairs_applied": [],
            "notes": "no actions; passthrough",
        }

    rewrite_contract = _rewrite_contract_prompt_payload(
        actions=actions,
        current_sql=c0_top1_sql,
        natural_language_hint=natural_language_hint or "",
    )
    schema_context = _rewrite_schema_context_prompt_payload(
        local_schema_view=local_schema_view,
        rewrite_contract=rewrite_contract,
    )
    prompt_audit = _prompt_payload_audit(
        {
            "rewrite_contract": rewrite_contract,
            "schema_context": schema_context,
        }
    )

    prompt = build_memory_rewrite_prompt(
        question=question,
        evidence=evidence or "",
        c0_top1_sql=c0_top1_sql,
        rewrite_contract_json=_json_dump_prompt(rewrite_contract),
        schema_context_json=_json_dump_prompt(schema_context),
        natural_language_hint=natural_language_hint or "",
    )

    raw: Dict[str, Any] = _call_llm_json(
        prompt,
        stage="memory_rewrite",
        trace_context={
            "action_count": len(actions or []),
            "selected_candidate_ids": [
                str(_payload_for_prompt(action).get("selected_candidate_id") or "")
                for action in actions or []
                if isinstance(_payload_for_prompt(action), dict)
            ],
            "rewrite_contract_audit": prompt_audit,
        },
    )  # type: ignore[assignment]

    # realization traces 反序列化
    traces: List[ActionRealizationTrace] = []
    for t in raw.get("action_realization_traces") or []:
        try:
            edits: List[SqlEditTrace] = []
            for e in t.get("edits") or []:
                try:
                    loc_raw = str(e.get("location") or "").strip().upper()
                    loc = EditScope(loc_raw) if loc_raw else EditScope.SELECT
                    edits.append(
                        SqlEditTrace(
                            edit_kind=str(e.get("edit_kind") or ""),
                            location=loc,
                            before_snippet=e.get("before_snippet"),
                            after_snippet=e.get("after_snippet"),
                        )
                    )
                except Exception:
                    continue
            traces.append(
                ActionRealizationTrace(
                    action_id=str(t.get("action_id") or ""),
                    realized=bool(t.get("realized") or False),
                    edits=edits,
                    scope_violation=bool(t.get("scope_violation") or False),
                    notes=t.get("notes"),
                )
            )
        except Exception:
            continue

    _rewrite_raw = raw.get("rewrite_sql")
    _rewrite_sql = (
        str(_rewrite_raw).strip() if _rewrite_raw is not None else None
    ) or None  # 空串 → None，让上层"空则 failure"语义生效
    contract_steps_applied = list(raw.get("contract_steps_applied") or [])
    dependency_repairs_applied = list(raw.get("dependency_repairs_applied") or [])
    _rewrite_sql, traces, contract_steps_applied = _enforce_rewrite_scope(
        original_sql=c0_top1_sql,
        rewrite_sql=_rewrite_sql,
        actions=actions,
        traces=traces,
        contract_steps_applied=contract_steps_applied,
    )
    _rewrite_sql, traces, contract_steps_applied = _fail_closed_rewrite_contract(
        original_sql=c0_top1_sql,
        rewrite_sql=_rewrite_sql,
        actions=actions,
        traces=traces,
        contract_steps_applied=contract_steps_applied,
    )
    _rewrite_sql, traces, contract_steps_applied = _enforce_rewrite_contract_absence_checks(
        original_sql=c0_top1_sql,
        rewrite_sql=_rewrite_sql,
        rewrite_contract=rewrite_contract,
        traces=traces,
        contract_steps_applied=contract_steps_applied,
    )
    return {
        "rewrite_sql": _rewrite_sql,
        "action_realization_traces": traces,
        "contract_steps_applied": contract_steps_applied,
        "rewrite_contract": rewrite_contract,
        "prompt_payload_audit": prompt_audit,
        # Legacy output key retained for old log readers. The current prompt
        # records explicit repair_program steps under contract_steps_applied.
        "dependency_repairs_applied": dependency_repairs_applied,
        "notes": raw.get("notes"),
    }


def run_hint_instantiation(
    *,
    raw_hint: str,
    question: str,
    evidence: str,
    pred_top1_sql: str,
    local_schema_view,
    actions: List[Any],
) -> Dict[str, Any]:
    """Phase 1.5e —— 把 code-rendered repair brief 改写到 current case。

    Returns dict:
    {
      "instantiated_hint": str,
      "applicable": bool,
      "instantiation_notes": str,
    }

    空 raw_hint 直接返回 applicable=False（无需 LLM call）。
    """
    from .prompts_v2 import build_hint_instantiation_prompt
    from .llm_utils_v2 import call_llm

    if not raw_hint or not raw_hint.strip():
        return {
            "instantiated_hint": "",
            "applicable": False,
            "instantiation_notes": "raw_hint is empty",
        }

    actions_payload = _runtime_actions_prompt_payload(actions)

    prompt = build_hint_instantiation_prompt(
        raw_hint=raw_hint,
        question=question,
        evidence=evidence or "",
        pred_top1_sql=pred_top1_sql,
        local_schema_view_json=_json_dump_prompt(local_schema_view),
        actions_json=_json_dump_prompt(actions_payload),
    )

    raw: Dict[str, Any] = _call_llm_json(
        prompt,
        stage="hint_instantiation",
        trace_context={"action_count": len(actions or [])},
    )  # type: ignore[assignment]

    instantiated = str(raw.get("instantiated_hint") or "").strip()
    applicable = bool(raw.get("applicable", False))
    notes = str(raw.get("instantiation_notes") or "").strip()

    # 防御：applicable=True 但 instantiated 空 → 视为不可用
    if applicable and not instantiated:
        applicable = False
        notes = (notes + " | empty instantiated_hint despite applicable=true").strip()

    return _validate_hint_instantiation_result(
        raw_hint=raw_hint,
        instantiated_hint=instantiated,
        applicable=applicable,
        notes=notes,
        question=question,
        evidence=evidence or "",
        pred_top1_sql=pred_top1_sql,
        local_schema_view=local_schema_view,
        actions=actions,
    )


__all__ = [
    "run_wrong_case_auditor",
    "run_error_instance_extractor",
    "run_action_compiler",
    "run_memory_rewrite",
    "run_hint_instantiation",
]
