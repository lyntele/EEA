"""V2 contract schemas for emergent pattern recognition.

The classes in this module keep the WUv2-5 contract split explicit:
recognition decides whether a new case looks like the bias, applicability
decides whether the repair is safe for the current case, and binding describes
how the compiler may derive slots without seed-case literals.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class GroundedAnchor(BaseModel):
    """Code-checkable evidence anchor used before LLM recognition."""

    kind: str = ""
    role_family: Optional[str] = None
    path_role: Optional[str] = None
    relation_role: Optional[str] = None
    value: Optional[str] = None
    source_channel: Optional[str] = None
    support_case_ids: List[str] = Field(default_factory=list)
    evidence: Dict[str, Any] = Field(default_factory=dict)


class RecognitionContract(BaseModel):
    """Answer-blind recognition contract."""

    question_precondition: str = ""
    sql_precondition: str = ""
    grounded_anchors: List[GroundedAnchor] = Field(default_factory=list)
    question_self_check: Dict[str, Any] = Field(default_factory=dict)
    sql_self_check: Dict[str, Any] = Field(default_factory=dict)
    observed_failure_summary: str = ""
    repair_direction: str = ""


class HistoricalRegressGuard(BaseModel):
    """Negative guard learned from a real runtime regression."""

    case_id: str = ""
    case_question_snippet: str = ""
    case_pred_sql_snippet: str = ""
    triggered_at_step: int = 0
    rewrite_failure_summary: str = ""
    matched_group_ids: List[str] = Field(default_factory=list)
    evidence: Dict[str, Any] = Field(default_factory=dict)


class ApplicabilityContract(BaseModel):
    """Regression-driven pre-rewrite contract.

    WUv2-5b deferred: admission only writes ``intent_description`` as audit.
    ``regression_negative_guards`` is retained in the schema for logged
    negative feedback, but runtime currently does not consume it until the
    guard semantics are redesigned.
    """

    intent_description: str = ""
    regression_negative_guards: List[HistoricalRegressGuard] = Field(default_factory=list)
    evidence: Dict[str, Any] = Field(default_factory=dict)


class BindingSlot(BaseModel):
    """Literal-free compiler binding slot.

    Slot fields describe roles, not seed table/column/expression names.
    """

    kind: str = ""
    role_family: Optional[str] = None
    path_role: Optional[str] = None
    relation_role: Optional[str] = None
    answer_unit_role: Optional[str] = None
    optional: bool = False
    constraints: Dict[str, Any] = Field(default_factory=dict)


class BindingContract(BaseModel):
    """Literal-free compiler binding contract."""

    source_slots: List[BindingSlot] = Field(default_factory=list)
    target_slots: List[BindingSlot] = Field(default_factory=list)
    allowed_operations: List[str] = Field(default_factory=list)
    preserve_invariants: List[str] = Field(default_factory=list)
    evidence: Dict[str, Any] = Field(default_factory=dict)


def _as_mapping(value: Any) -> Dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return dict(getattr(value, "__dict__", {}) or {})


class PatternRecognitionContractV2(BaseModel):
    """Compatibility wrapper for WUv2-5 three-contract pattern memory."""

    schema_version: str = "pattern-recognition-v2"
    recognition: RecognitionContract = Field(default_factory=RecognitionContract)
    applicability: ApplicabilityContract = Field(default_factory=ApplicabilityContract)
    binding: BindingContract = Field(default_factory=BindingContract)

    # Legacy mirrors. Existing runtime code reads these until WUv2-5b replaces
    # the trigger path with the full five-layer contract.
    pre_question_signature: str = ""
    pre_question_signature_self_check: Dict[str, Any] = Field(default_factory=dict)
    pre_sql_signature: str = ""
    pre_sql_signature_self_check: Dict[str, Any] = Field(default_factory=dict)
    observed_failure_summary: str = ""
    repair_direction: str = ""

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_payload(cls, data: Any) -> Any:
        payload = _as_mapping(data)
        recognition = _as_mapping(
            payload.get("recognition") or payload.get("recognition_contract") or {}
        )
        if not recognition:
            recognition = {
                "question_precondition": payload.get("pre_question_signature") or "",
                "sql_precondition": payload.get("pre_sql_signature") or "",
                "question_self_check": payload.get("pre_question_signature_self_check") or {},
                "sql_self_check": payload.get("pre_sql_signature_self_check") or {},
                "observed_failure_summary": payload.get("observed_failure_summary") or "",
                "repair_direction": payload.get("repair_direction") or "",
                "grounded_anchors": payload.get("grounded_anchors") or [],
            }
        else:
            recognition.setdefault(
                "question_precondition",
                recognition.get("pre_question_signature") or payload.get("pre_question_signature") or "",
            )
            recognition.setdefault(
                "sql_precondition",
                recognition.get("pre_sql_signature") or payload.get("pre_sql_signature") or "",
            )
            recognition.setdefault(
                "question_self_check",
                recognition.get("pre_question_signature_self_check")
                or payload.get("pre_question_signature_self_check")
                or {},
            )
            recognition.setdefault(
                "sql_self_check",
                recognition.get("pre_sql_signature_self_check")
                or payload.get("pre_sql_signature_self_check")
                or {},
            )
            recognition.setdefault(
                "observed_failure_summary",
                payload.get("observed_failure_summary") or "",
            )
            recognition.setdefault("repair_direction", payload.get("repair_direction") or "")
            recognition.setdefault("grounded_anchors", payload.get("grounded_anchors") or [])

        payload["recognition"] = recognition
        payload.setdefault(
            "applicability",
            payload.get("applicability_contract") or {},
        )
        payload.setdefault(
            "binding",
            payload.get("binding_contract") or {},
        )
        payload.setdefault(
            "pre_question_signature",
            recognition.get("question_precondition") or "",
        )
        payload.setdefault(
            "pre_question_signature_self_check",
            recognition.get("question_self_check") or {},
        )
        payload.setdefault("pre_sql_signature", recognition.get("sql_precondition") or "")
        payload.setdefault(
            "pre_sql_signature_self_check",
            recognition.get("sql_self_check") or {},
        )
        payload.setdefault(
            "observed_failure_summary",
            recognition.get("observed_failure_summary") or "",
        )
        payload.setdefault("repair_direction", recognition.get("repair_direction") or "")
        return payload

    @model_validator(mode="after")
    def _sync_legacy_mirrors(self) -> "PatternRecognitionContractV2":
        self.pre_question_signature = self.recognition.question_precondition
        self.pre_question_signature_self_check = dict(self.recognition.question_self_check or {})
        self.pre_sql_signature = self.recognition.sql_precondition
        self.pre_sql_signature_self_check = dict(self.recognition.sql_self_check or {})
        self.observed_failure_summary = self.recognition.observed_failure_summary
        self.repair_direction = self.recognition.repair_direction
        return self


__all__ = [
    "ApplicabilityContract",
    "BindingContract",
    "BindingSlot",
    "GroundedAnchor",
    "HistoricalRegressGuard",
    "PatternRecognitionContractV2",
    "RecognitionContract",
]
