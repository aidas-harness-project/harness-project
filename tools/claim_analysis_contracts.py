"""Semantic validation for selective Claim Analysis contracts.

JSON Schema owns local object shape. This module owns keyed uniqueness,
cross-reference closure, grade/read-mode invariants, and exact evidence-range
checks that JSON Schema cannot express.
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Callable, Iterable


ROOT = Path(__file__).resolve().parent.parent
ROUTING_CONFIG_PATH = (
    ROOT / "config" / "claim_analysis" / "claim_analysis_routing_v0.1.json"
)
CASE_TYPES = {
    "personal_insurance", "traffic_accident", "industrial_accident", "liability"
}
DOMAIN_CODES = {
    "event_timeline", "diagnosis", "diagnosis_basis", "treatment",
    "clinical_course_outcome", "prior_history_influences",
    "complications_new_problems", "disability",
}
COST_KINDS = {
    "medical_expense_receipt", "medical_expense_itemization",
    "pharmacy_payment_confirmation",
}


def _duplicates(values: Iterable[str]) -> list[str]:
    counts = Counter(values)
    return sorted(value for value, count in counts.items() if count > 1)


def load_default_routing_config() -> dict:
    return json.loads(ROUTING_CONFIG_PATH.read_text(encoding="utf-8"))


def validate_routing_config_semantics(config: dict) -> list[str]:
    errors: list[str] = []
    domains = config.get("domains") or []
    kinds = config.get("document_kinds") or []
    routes = config.get("source_routes") or []
    fields = config.get("fields") or []

    for duplicate in _duplicates(row.get("code") for row in domains):
        errors.append(f"domains: duplicate code {duplicate!r}")
    domain_codes = {row.get("code") for row in domains}
    if domain_codes != DOMAIN_CODES:
        errors.append(
            "domains: must contain exactly the eight accepted domain codes"
        )

    for duplicate in _duplicates(row.get("kind") for row in kinds):
        errors.append(f"document_kinds: duplicate kind {duplicate!r}")
    kind_codes = {row.get("kind") for row in kinds}
    for row in kinds:
        if row.get("kind") in COST_KINDS and (
            row.get("claim_analysis_read_mode") != "presence_only"
            or row.get("default_roles") != ["required_document_presence_only"]
        ):
            errors.append(
                f"document_kinds: cost kind {row.get('kind')!r} must be presence-only"
            )

    for duplicate in _duplicates(row.get("route_id") for row in routes):
        errors.append(f"source_routes: duplicate route_id {duplicate!r}")
    route_ids = {row.get("route_id") for row in routes}
    for route in routes:
        for group in route.get("priority_groups") or []:
            for kind in group:
                if kind not in kind_codes:
                    errors.append(
                        f"source route {route.get('route_id')!r} references unknown kind {kind!r}"
                    )
                if kind in COST_KINDS:
                    errors.append(
                        f"source route {route.get('route_id')!r} must not read cost kind {kind!r}"
                    )
        # An administrative route's whole safety property is that it holds no
        # medical ladder: that is what makes "opens no medical document" a
        # structural fact rather than a runtime promise. A conditional route
        # must also name a real condition -- a route gated on "always" is not
        # conditional, and one gated on a condition nothing evaluates would
        # never fire.
        route_id = route.get("route_id")
        source_kind = route.get("source_kind")
        condition = route.get("activation_condition")
        if source_kind == "administrative_or_intake":
            if route.get("priority_groups"):
                errors.append(
                    f"source route {route_id!r} is administrative and must rank no medical kinds"
                )
            if not route.get("non_medical_sources"):
                errors.append(
                    f"source route {route_id!r} is administrative and must name its non-medical sources"
                )
        elif source_kind == "medical_document" and route.get("non_medical_sources"):
            errors.append(
                f"source route {route_id!r} is a medical route and must not name non-medical sources"
            )
        if condition != "always" and source_kind != "administrative_or_intake":
            errors.append(
                f"source route {route_id!r}: only an administrative route may be conditionally activated"
            )

    for duplicate in _duplicates(row.get("field_id") for row in fields):
        errors.append(f"fields: duplicate field_id {duplicate!r}")
    for field in fields:
        field_id = field.get("field_id")
        if field.get("domain_code") not in domain_codes:
            errors.append(
                f"field {field_id!r} references unknown domain {field.get('domain_code')!r}"
            )
        route_id = field.get("source_route_id")
        if route_id is not None and route_id not in route_ids:
            errors.append(f"field {field_id!r} references unknown route {route_id!r}")
        grade = field.get("medical_advisory_grade")
        wave = field.get("extraction_wave")
        basis = field.get("activation_basis")
        if grade == "D" and (
            wave != "deferred" or basis != "deferred" or route_id is not None
        ):
            errors.append(f"field {field_id!r}: D grade must remain deferred")
        if grade == "C" and wave in {"A", "B"} and (
            wave != "B" or basis not in {"routing_override", "consistency_override"}
        ):
            errors.append(
                f"field {field_id!r}: active C grade requires a named B-wave override"
            )

    rules = config.get("case_type_rules") or []
    if Counter(row.get("case_type") for row in rules) != Counter(CASE_TYPES):
        errors.append("case_type_rules: each of the four case types must appear exactly once")

    for case_type, required_kinds in (
        config.get("required_documents_by_case_type") or {}
    ).items():
        if case_type not in CASE_TYPES:
            errors.append(f"required_documents: unknown case type {case_type!r}")
        for kind in required_kinds:
            if kind not in kind_codes:
                errors.append(
                    f"required_documents[{case_type!r}] references unknown kind {kind!r}"
                )
    return sorted(set(errors))


def _all_exact_references(value):
    if isinstance(value, dict):
        if {"document_id", "page", "quote", "start_char", "end_char"} <= value.keys():
            yield value
        for child in value.values():
            yield from _all_exact_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_exact_references(child)


def exact_evidence_document_ids(data: dict) -> list[str]:
    """Return cited document IDs in stable first-seen order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for reference in _all_exact_references(data):
        doc_id = reference.get("document_id")
        if isinstance(doc_id, str) and doc_id not in seen:
            seen.add(doc_id)
            ordered.append(doc_id)
    return ordered


def validate_claim_analysis_result_semantics(data: dict, config: dict) -> list[str]:
    errors = [f"config: {e}" for e in validate_routing_config_semantics(config)]
    if data.get("config_version") != config.get("config_version"):
        errors.append("config_version does not match the loaded routing config")

    assessments = data.get("case_type_assessment") or []
    if Counter(row.get("case_type") for row in assessments) != Counter(CASE_TYPES):
        errors.append(
            "case_type_assessment: each of the four case types must appear exactly once"
        )

    configured_fields = {row["field_id"]: row for row in config.get("fields") or []}
    fields = data.get("claim_facts") or []
    for duplicate in _duplicates(row.get("field_id") for row in fields):
        errors.append(f"claim_facts: duplicate field_id {duplicate!r}")

    observation_owner: dict[str, str] = {}
    field_observations: dict[str, set[str]] = {}
    for field in fields:
        field_id = field.get("field_id")
        configured = configured_fields.get(field_id)
        if configured is None:
            errors.append(f"claim_facts: unconfigured field_id {field_id!r}")
        elif field.get("domain_code") != configured.get("domain_code"):
            errors.append(
                f"field {field_id!r}: domain_code does not match routing config"
            )
        ids: list[str] = []
        for observation in field.get("observations") or []:
            observation_id = observation.get("observation_id")
            ids.append(observation_id)
            if observation_id in observation_owner:
                errors.append(
                    f"observation_id {observation_id!r} is duplicated across claim facts"
                )
            else:
                observation_owner[observation_id] = field_id
        for duplicate in _duplicates(ids):
            errors.append(f"field {field_id!r}: duplicate observation_id {duplicate!r}")
        owned = set(ids)
        field_observations[field_id] = owned
        for selected_id in field.get("selected_observation_ids") or []:
            if selected_id not in owned:
                errors.append(
                    f"field {field_id!r}: selected observation {selected_id!r} is not owned by the field"
                )

    candidates = data.get("conflict_candidates") or []
    for duplicate in _duplicates(row.get("conflict_candidate_id") for row in candidates):
        errors.append(f"conflict_candidates: duplicate id {duplicate!r}")
    candidate_by_id = {
        row.get("conflict_candidate_id"): row for row in candidates
    }
    for candidate in candidates:
        candidate_id = candidate.get("conflict_candidate_id")
        field_id = candidate.get("field_id")
        if field_id not in field_observations:
            errors.append(f"conflict candidate {candidate_id!r} references unknown field")
            continue
        for observation_id in candidate.get("observation_ids") or []:
            if observation_id not in field_observations[field_id]:
                errors.append(
                    f"conflict candidate {candidate_id!r} references an observation outside field {field_id!r}"
                )

    for field in fields:
        field_id = field.get("field_id")
        for candidate_id in field.get("conflict_candidate_ids") or []:
            candidate = candidate_by_id.get(candidate_id)
            if candidate is None:
                errors.append(f"field {field_id!r} references unknown conflict candidate {candidate_id!r}")
            elif candidate.get("field_id") != field_id:
                errors.append(f"field {field_id!r} references another field's conflict candidate")

    known_candidate_ids = set(candidate_by_id)
    for link in data.get("policy_links") or []:
        for requirement in link.get("requirements") or []:
            for candidate_id in requirement.get("conflict_candidate_ids") or []:
                if candidate_id not in known_candidate_ids:
                    errors.append(
                        f"requirement {requirement.get('requirement_id')!r} references unknown conflict candidate {candidate_id!r}"
                    )

    known_field_ids = set(configured_fields)
    for assessment in assessments:
        for key in ("triggered_field_ids", "conflicting_field_ids"):
            for field_id in assessment.get(key) or []:
                if field_id not in known_field_ids:
                    errors.append(
                        f"case type {assessment.get('case_type')!r} references unknown field {field_id!r}"
                    )

    known_kinds = {row["kind"] for row in config.get("document_kinds") or []}
    for item in data.get("required_document_checklist") or []:
        if item.get("document_kind") not in known_kinds:
            errors.append(
                f"required-document checklist references unknown kind {item.get('document_kind')!r}"
            )

    for reference in _all_exact_references(data):
        start, end = reference.get("start_char"), reference.get("end_char")
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or start >= end:
            errors.append(
                f"evidence {reference.get('document_id')} page {reference.get('page')}: invalid exact range"
            )
    return sorted(set(errors))


def verify_exact_evidence_references(
    data: dict,
    page_text: Callable[[str, int], str | None],
) -> list[str]:
    """Verify every exact evidence quote against its processed page body."""
    errors: list[str] = []
    for reference in _all_exact_references(data):
        doc_id, page = reference["document_id"], reference["page"]
        text = page_text(doc_id, page)
        if text is None:
            errors.append(f"{doc_id}: processed page {page} is unavailable")
            continue
        start, end = reference["start_char"], reference["end_char"]
        if end > len(text) or text[start:end] != reference["quote"]:
            errors.append(
                f"{doc_id}: quote does not exactly match processed page {page} at [{start}:{end}]"
            )
    return errors
