"""Fine-grained medical document routing for Stage 2.

The broad document taxonomy remains authoritative for legacy consumers. This
module adds a gated medical kind/role layer used by selective Claim Analysis.
It never reads case data and never enables itself: activation is controlled by
the versioned routing configuration.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
import re
from typing import Any

from _validation import load_registry, validate_instance


ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = (
    ROOT / "config" / "claim_analysis" / "claim_analysis_routing_v0.1.json"
)

MEDICAL_SCHEMA_VERSION = "medical_document_classification.v0.1"


@lru_cache(maxsize=4)
def load_routing_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load and validate one immutable process-local config revision."""
    config = json.loads(path.read_text(encoding="utf-8"))
    schemas, registry = load_registry()
    errors = validate_instance(
        config, "claim_analysis_routing_config.schema.json", schemas, registry
    )
    if errors:
        raise ValueError(
            "invalid claim-analysis routing config: " + "; ".join(errors)
        )
    return config


def routing_enabled(config: dict[str, Any] | None = None) -> bool:
    selected = config if config is not None else load_routing_config()
    return selected.get("behavior_enabled") is True


def _collapsed(text: str) -> str:
    return re.sub(r"[\s·ㆍ・:：()\[\]_-]+", "", text or "").lower()


# Ordered from most specific to least specific. These patterns classify only a
# printed title that names a form. A generic RECORD/REPORT remains unresolved
# and goes to the existing LLM classification call when routing is enabled.
_TITLE_KIND_PATTERNS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("후유장해진단서", "후유장애진단서", "장해진단서", "장애진단서", "신체감정서"), "disability_assessment"),
    (("초진기록지", "초진기록", "초진진료기록"), "initial_visit_record"),
    (("최종진료기록", "진료종결기록", "최종외래기록"), "final_visit_record"),
    (("경과기록지", "경과기록", "progressnote"), "progress_record"),
    (("외래진료기록", "외래기록지", "외래기록"), "outpatient_record"),
    (("수술기록지", "수술기록", "수술보고서", "시술기록지", "시술기록"), "surgery_procedure_record"),
    (("영상판독지", "영상판독결과", "방사선판독지", "방사선판독결과"), "imaging_interpretation"),
    (("검사결과지", "검사결과보고서", "병리검사결과", "전기진단검사결과", "근전도검사결과"), "major_test_result"),
    (("입퇴원요약", "입퇴원확인서", "퇴원요약", "퇴원요약지"), "admission_discharge_summary"),
    (("응급실기록지", "응급실기록", "응급진료기록지", "응급진료기록", "응급의료기록"), "emergency_record"),
    (("약제비납입확인서", "약제비납부확인서"), "pharmacy_payment_confirmation"),
    (("진료비세부내역서", "진료비세부산정내역", "진료비상세내역서"), "medical_expense_itemization"),
    (("진료비계산서영수증", "진료비영수증"), "medical_expense_receipt"),
    (("처방내역", "투약내역", "치료내역"), "prescription_treatment_history"),
    (("간호기록지", "간호기록"), "nursing_routine_record"),
    (("진단서", "의사소견서", "소견서"), "diagnosis_certificate"),
)


def roles_for_kind(kind: str, config: dict[str, Any]) -> list[str]:
    for row in config.get("document_kinds", []):
        if row.get("kind") == kind:
            return list(row.get("default_roles", []))
    raise ValueError(f"medical document kind {kind!r} is absent from routing config")


def medical_kind_from_title(title: str | None) -> str | None:
    collapsed = _collapsed(title or "")
    collapsed = re.sub(r"(?:mcbride|ama)$", "", collapsed)
    if not collapsed:
        return None
    # A death certificate is a distinct legal/medical form and is outside the
    # current traumatic-injury PoC. It must reach the ambiguous/model path,
    # never inherit diagnosis-certificate priority from the generic suffix.
    if collapsed.endswith("사망진단서"):
        return None
    for markers, kind in _TITLE_KIND_PATTERNS:
        if any(collapsed.endswith(marker) for marker in markers):
            return kind
    return None


def classification_from_title(
    title: str | None,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    kind = medical_kind_from_title(title)
    if kind is None:
        return None
    return {
        "schema_version": MEDICAL_SCHEMA_VERSION,
        "status": "deterministic_title",
        "kind": kind,
        "roles": roles_for_kind(kind, config),
        "candidates": [],
        "evidence_references": [{"page": 1, "quote": title}],
    }


def classification_from_model(
    parsed: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Normalize the fine-grained part of one already-paid classifier call."""
    quote = parsed.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError("fine-grained medical classifier omitted its evidence quote")
    known_kinds = {row["kind"] for row in config.get("document_kinds", [])}
    kind = parsed.get("medical_document_kind")
    candidates = parsed.get("medical_kind_candidates") or []

    if kind is not None:
        if kind not in known_kinds:
            raise ValueError(f"classifier returned unknown medical_document_kind {kind!r}")
        return {
            "schema_version": MEDICAL_SCHEMA_VERSION,
            "status": "llm_classified",
            "kind": kind,
            "roles": roles_for_kind(kind, config),
            "candidates": [],
            "evidence_references": [{"page": 1, "quote": quote}],
        }

    normalized_candidates = []
    for candidate in candidates[:3]:
        candidate_kind = candidate.get("kind")
        if candidate_kind not in known_kinds:
            raise ValueError(f"classifier returned unknown medical candidate {candidate_kind!r}")
        normalized_candidates.append({
            "kind": candidate_kind,
            "confidence": candidate["confidence"],
        })

    broad_type = parsed.get("predicted_document_type")
    if broad_type not in {"diagnosis_certificate", "medical_record", "imaging_report"}:
        return {
            "schema_version": MEDICAL_SCHEMA_VERSION,
            "status": "not_medical",
            "kind": None,
            "roles": [],
            "candidates": [],
            "evidence_references": [{"page": 1, "quote": quote}],
        }

    if not normalized_candidates:
        raise ValueError(
            "fine-grained medical classifier returned neither a kind nor candidates"
        )
    return {
        "schema_version": MEDICAL_SCHEMA_VERSION,
        "status": "ambiguous",
        "kind": None,
        "roles": [],
        "candidates": normalized_candidates,
        "ambiguity_reason": parsed.get("medical_ambiguity_reason") or (
            "The source text did not identify one medical form kind unambiguously."
        ),
        "evidence_references": [{"page": 1, "quote": quote}],
    }


def not_medical_classification(*, quote: str | None = None) -> dict[str, Any]:
    """Build a deterministic non-medical verdict without pretending a model ran."""
    evidence = []
    if isinstance(quote, str) and quote.strip():
        evidence.append({"page": 1, "quote": quote})
    return {
        "schema_version": MEDICAL_SCHEMA_VERSION,
        "status": "not_medical",
        "kind": None,
        "roles": [],
        "candidates": [],
        "evidence_references": evidence,
    }


def medical_prompt_contract(config: dict[str, Any]) -> str:
    kinds = ", ".join(row["kind"] for row in config.get("document_kinds", []))
    return f"""

Also classify the medical form for selective downstream reading. Add exactly
these three fields to the same JSON object:
- \"medical_document_kind\": one of [{kinds}], or null
- \"medical_kind_candidates\": up to 3 objects shaped
  {{\"kind\": \"<kind>\", \"confidence\": <0-1>}}
- \"medical_ambiguity_reason\": a short reason, or null

Use one medical_document_kind only when the text identifies the form. If the
document is non-medical, return null and an empty candidate list. If it is
medical but the form remains ambiguous, return null and 1-3 candidates. Do not
infer a form merely from the diagnosis or treatment mentioned in body prose.
"""
