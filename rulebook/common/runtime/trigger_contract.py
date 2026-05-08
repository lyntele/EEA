"""Runtime trigger-contract materialization and validation.

Legacy trigger signatures are useful migration inputs, but runtime matching
must consume only an executable ``trigger_contract`` when the memory object is
already executable.  Replay-audit visible patterns may remain runtime-visible
with invalid contracts so runtime/replay can record why they fail to trigger.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, List, Tuple

from method.EEA.rulebook.common.core.data_structures import GroupSummary, LibraryStateV2, TriggerContract
from method.EEA.rulebook.common.core.vocabulary import GroupType


OUTPUT_DECREASE_PROGRAM_TYPES = {
    "select_drop",
    "select_replace",
    "output_decrease",
    "role_side_selection",
    "relation_output_contract",
}

SOURCE_PAIR_SIGNALS = {
    "pred.output_grain=pair_rows",
    "pred.output_arity=2",
    "pred.pair_output=True",
    "pred.role_side_pair_output=True",
}

BROAD_RUNTIME_SIGNALS = {
    "pred.select_arity_present=True",
    "pred.has_select=True",
    "pred.has_join=True",
    "pred.has_where=True",
}


def _payload(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return dict(getattr(value, "__dict__", {}) or {})


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return [str(value)] if str(value) else []


def _dedup(values: Iterable[Any]) -> List[str]:
    return sorted({str(value) for value in values if str(value)})


def _program_type_from_contract(contract: Dict[str, Any]) -> str:
    action_contract = _payload(contract.get("action_contract"))
    program_type = str(action_contract.get("program_type") or "").strip()
    if program_type:
        return program_type
    lowering = _as_list(action_contract.get("lowering_families"))
    if len(lowering) == 1:
        return lowering[0]
    synthesized = _payload(action_contract.get("synthesized_program"))
    synthesized_type = str(synthesized.get("program_type") or "").strip()
    if synthesized_type:
        return synthesized_type
    locus = str(action_contract.get("locus") or "").upper()
    op_family = str(action_contract.get("op_family") or "").lower()
    target_family = str(action_contract.get("target_family") or "").lower()
    if locus == "SELECT" and op_family == "drop":
        return "select_drop"
    if locus == "SELECT" and op_family == "replace":
        return "select_replace"
    if locus == "SELECT" and target_family in {"shape", "slot"}:
        return "select_replace"
    return ""


def _has_program(contract: Dict[str, Any]) -> bool:
    action_contract = _payload(contract.get("action_contract"))
    if _program_type_from_contract(contract):
        return True
    if action_contract.get("repair_program"):
        return True
    synthesized = _payload(action_contract.get("synthesized_program"))
    return bool(synthesized.get("ops"))


def _is_non_broad(signal: str) -> bool:
    text = str(signal or "")
    return bool(text and text not in BROAD_RUNTIME_SIGNALS and not text.startswith("program."))


def sanitize_trigger_contract(contract: Any) -> Dict[str, Any]:
    """Remove known-invalid runtime negatives and normalize program metadata."""
    payload = deepcopy(_payload(contract))
    if not payload:
        return {}
    action_contract = _payload(payload.get("action_contract"))
    program_type = _program_type_from_contract(payload)
    if program_type:
        action_contract["program_type"] = program_type
    payload["action_contract"] = action_contract

    if program_type in OUTPUT_DECREASE_PROGRAM_TYPES:
        negatives = {
            str(signal)
            for signal in (payload.get("negative_signals") or [])
            if str(signal) and str(signal) not in SOURCE_PAIR_SIGNALS
        }
        payload["negative_signals"] = sorted(negatives)
    return payload


def is_contract_runtime_executable(contract: Any) -> bool:
    payload = sanitize_trigger_contract(contract)
    if not payload:
        return False
    required = [sig for sig in _as_list(payload.get("required_signals")) if _is_non_broad(sig)]
    variants = [
        [sig for sig in _as_list(variant) if _is_non_broad(sig)]
        for variant in (payload.get("variant_required_signal_sets") or [])
        if isinstance(variant, (list, tuple, set)) and variant
    ]
    variants = [variant for variant in variants if variant]
    decisive = [
        sig for sig in _as_list(payload.get("decisive_pred_signals")) if _is_non_broad(sig)
    ]
    return bool((required or variants) and (decisive or variants) and _has_program(payload))


def _source_shape(contract: Dict[str, Any]) -> Dict[str, Any]:
    source = _payload(contract.get("source_case_contract"))
    return _payload(source.get("output_shape_current"))


def _pair_source_signals(contract: Dict[str, Any]) -> List[str]:
    shape = _source_shape(contract)
    arity = shape.get("arity")
    grain = str(shape.get("grain") or "")
    signals: List[str] = []
    if arity is not None:
        signals.append(f"pred.output_arity={arity}")
        signals.append(f"pred.select_arity={arity}")
    if str(arity) == "2":
        signals.append("pred.pair_output=True")
    if grain:
        signals.append(f"pred.output_grain={grain}")
    if grain == "pair_rows":
        signals.append("pred.pair_output=True")
    return _dedup(signals)


def materialize_contract_from_legacy_signature(
    *,
    obj: GroupSummary,
    legacy_signature: Any = None,
    schema_view: Any = None,
) -> Dict[str, Any]:
    """Build an executable runtime contract from legacy runtime-visible tags."""
    del schema_view  # Reserved for role-graph-backed migrations.
    contract = sanitize_trigger_contract(obj.trigger_contract)
    legacy = legacy_signature if legacy_signature is not None else obj.trigger_signature
    required_pred_tags = _as_list(getattr(legacy, "required_pred_tags", None))
    if not required_pred_tags:
        required_pred_tags = _as_list(_payload(legacy).get("required_pred_tags"))

    program_type = _program_type_from_contract(contract)
    action_contract = _payload(contract.get("action_contract"))
    if program_type:
        action_contract["program_type"] = program_type
    contract["action_contract"] = action_contract

    required = set(_as_list(contract.get("required_signals")))
    variants = [
        _dedup(variant)
        for variant in (contract.get("variant_required_signal_sets") or [])
        if isinstance(variant, (list, tuple, set))
    ]
    decisive = set(_as_list(contract.get("decisive_pred_signals")))

    tag_signals = {
        f"pred.decisive_tag={tag}"
        for tag in required_pred_tags
        if str(tag) and str(tag) not in {"select_arity_mismatch", "output_arity_mismatch"}
    }
    decisive.update(tag_signals)

    pair_legacy = "pair_output" in {str(tag) for tag in required_pred_tags}
    pair_signals = _pair_source_signals(contract)
    if pair_legacy and program_type in OUTPUT_DECREASE_PROGRAM_TYPES:
        required.update(signal for signal in pair_signals if signal.startswith("pred.output_arity="))
        if "pred.output_arity=2" in pair_signals:
            variants.extend(
                [
                    ["pred.pair_output=True"],
                    ["pred.output_grain=pair_rows"],
                    ["pred.role_side_pair_output=True"],
                ]
            )
            decisive.update(
                {
                    "pred.pair_output=True",
                    "pred.output_grain=pair_rows",
                    "pred.role_side_pair_output=True",
                }
            )
        action_contract.setdefault(
            "required_role_slots",
            ["source_output_pair", "keep_output_slot", "drop_output_slot"],
        )
        action_contract.setdefault("binding_policy", "canonical_role_side_or_output_pair")
        action_contract.setdefault("requires_unique_dry_run_action", True)
    elif tag_signals:
        required.update(tag_signals)

    if program_type in OUTPUT_DECREASE_PROGRAM_TYPES:
        negative = {
            str(signal)
            for signal in _as_list(contract.get("negative_signals"))
            if str(signal) not in SOURCE_PAIR_SIGNALS
        }
        contract["negative_signals"] = sorted(negative)

    contract["required_signals"] = _dedup(required)
    contract["variant_required_signal_sets"] = [
        _dedup(variant)
        for variant in variants
        if any(_is_non_broad(signal) for signal in variant)
    ]
    contract["decisive_pred_signals"] = _dedup(decisive)
    contract["action_contract"] = action_contract
    contract.setdefault("trigger_policy", {})
    contract["trigger_policy"].setdefault("requires_binder_dry_run", True)
    contract.setdefault("max_actions", 1)
    return sanitize_trigger_contract(contract)


def ensure_materialized_trigger_contract(
    memory_object: GroupSummary,
    schema_view: Any = None,
) -> Tuple[GroupSummary, Dict[str, Any]]:
    """Ensure runtime-visible objects expose a non-empty executable contract."""
    contract = sanitize_trigger_contract(memory_object.trigger_contract)
    status = "ok"
    if not is_contract_runtime_executable(contract):
        contract = materialize_contract_from_legacy_signature(
            obj=memory_object,
            legacy_signature=memory_object.trigger_signature,
            schema_view=schema_view,
        )
        status = "materialized_from_legacy_signature"

    if is_contract_runtime_executable(contract):
        memory_object.trigger_contract = TriggerContract.model_validate(contract)
        memory_object.runtime_contract_status = status
        return memory_object, {"status": status, "runtime_executable": True}

    memory_object.trigger_contract = TriggerContract.model_validate(contract or {})
    memory_object.runtime_usable = False
    memory_object.runtime_contract_status = "invalid_empty_contract"
    blockers = list(memory_object.runtime_blockers or [])
    if "empty_or_unmaterializable_trigger_contract" not in blockers:
        blockers.append("empty_or_unmaterializable_trigger_contract")
    memory_object.runtime_blockers = blockers
    return memory_object, {
        "status": "blocked",
        "runtime_executable": False,
        "reason": "empty_or_unmaterializable_trigger_contract",
    }


def materialize_library_runtime_contracts(
    library: LibraryStateV2,
    schema_view: Any = None,
) -> Tuple[LibraryStateV2, Dict[str, Any]]:
    report = {"checked": 0, "materialized": 0, "blocked": 0}
    for group in [*(library.patterns or []), *(library.singletons or [])]:
        if not group.runtime_usable:
            continue
        report["checked"] += 1
        was_runtime_visible_pattern = group.group_type == GroupType.PATTERN
        _, validation = ensure_materialized_trigger_contract(group, schema_view=schema_view)
        if validation.get("status") == "materialized_from_legacy_signature":
            report["materialized"] += 1
        if not validation.get("runtime_executable"):
            report["blocked"] += 1
            if was_runtime_visible_pattern:
                group.runtime_usable = True
                group.runtime_contract_status = str(validation.get("status") or "blocked")
    return library, report


__all__ = [
    "OUTPUT_DECREASE_PROGRAM_TYPES",
    "SOURCE_PAIR_SIGNALS",
    "ensure_materialized_trigger_contract",
    "is_contract_runtime_executable",
    "materialize_contract_from_legacy_signature",
    "materialize_library_runtime_contracts",
    "sanitize_trigger_contract",
]
