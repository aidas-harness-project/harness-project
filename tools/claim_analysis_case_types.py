"""Deterministic four-way case-type assessment for Claim Analysis.

The four types are **independent labels, not a partition**. One accident can be
a traffic accident AND an industrial accident AND a liability case at once, and
the personal-insurance label is close to always-on for a documented injury. So
this module evaluates each type separately against its own evidence rule and
never normalizes the four into a single winner.

Three statuses, and the difference between two of them is the whole point:

* `applicable`   -- an extracted field asserts the type's positive fact, and the
                   observation carrying it has exact evidence.
* `uncertain`   -- the type's evidence is absent, partial, only contextual
                   (commute/business trip/company event for industrial), or the
                   deciding field is itself in conflict.
* `not_applicable` -- only when a source affirmatively states the negative, never
                   from silence.

`absence_means_false: false` in the routing config is what forbids the fourth,
tempting reading: "nothing said a car was involved, so it is not a traffic
accident". Silence is `uncertain` here, always.

Filing status (접수 여부) is deliberately NOT inferred from the medical record.
Its only authoritative source is intake; missing intake means `unknown`.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence


CASE_TYPES = (
    "personal_insurance", "traffic_accident", "industrial_accident", "liability",
)

# The field each type reads its positive fact from. These are the routing
# config's own `event_timeline` fields; naming them here keeps the judgment
# rule visible in one place rather than spread through the driver.
TYPE_TRIGGER_FIELDS: dict[str, tuple[str, ...]] = {
    "personal_insurance": ("injury_event_present", "accident_mechanism"),
    "traffic_accident": ("vehicle_involvement",),
    "industrial_accident": ("work_activity_context",),
    "liability": ("facility_defect_or_third_party_responsibility",),
}

# `work_activity_context` is an enum whose values do not all mean the same
# thing. Only explicit work activity supports 산재; the three context values are
# exactly the cases the config's `uncertain_fact` calls out as NOT establishing
# work connection on their own.
WORK_SUPPORTING = frozenset({"work_activity", "on_duty"})
WORK_UNCERTAIN = frozenset({"commute", "business_trip", "company_event"})
WORK_NEGATIVE = frozenset({"not_work_related"})


def _field_by_id(claim_facts: Sequence[Mapping[str, Any]]) -> dict[str, dict]:
    return {row["field_id"]: dict(row) for row in claim_facts}


def _selected_observations(field: Mapping[str, Any]) -> list[dict]:
    """The observation(s) whose value the field actually reports."""
    selected = set(field.get("selected_observation_ids") or [])
    return [
        dict(observation)
        for observation in field.get("observations") or []
        if observation.get("observation_id") in selected
    ]


def _absence_observations(field: Mapping[str, Any]) -> list[dict]:
    """Readings where a source AFFIRMATIVELY stated the thing is not present.

    These are findings, not silence, and they are the only route to a
    `not_applicable` type verdict -- which is why they are collected separately
    from the selected ones (an `explicitly_absent` field elects no value, so
    `selected_observation_ids` is empty by contract).
    """
    return [
        dict(observation)
        for observation in field.get("observations") or []
        if observation.get("value_state") == "explicitly_absent"
    ]


def _evidence_of(observations: Sequence[Mapping[str, Any]]) -> list[dict]:
    references: list[dict] = []
    for observation in observations:
        for reference in observation.get("evidence_references") or []:
            if reference not in references:
                references.append(dict(reference))
    return references


def _assess_boolean_trigger(
    field: Mapping[str, Any] | None,
    *,
    positive_label: str,
    negative_label: str,
) -> tuple[str, list[dict], str]:
    """Status for a type whose trigger field is a boolean-ish assertion."""
    if field is None:
        return "uncertain", [], (
            "The deciding fact was not extracted, so the type cannot be judged "
            "from the available records."
        )
    status = field.get("resolution_status")
    if status == "conflict":
        return "uncertain", [], (
            "Sources disagree on the deciding fact; the disagreement is "
            "recorded as a conflict candidate rather than resolved here."
        )
    if status == "explicitly_absent":
        # A source stated the fact is NOT present. That is affirmative negative
        # evidence -- the one thing that earns `not_applicable` -- so it must
        # not be flattened into `uncertain` alongside silence.
        absent = _absence_observations(field)
        if absent:
            return "not_applicable", _evidence_of(absent), negative_label
    if status in {"unavailable", "not_applicable"}:
        return "uncertain", [], (
            "No source states the deciding fact. Absence of a statement is not "
            "treated as a negative finding."
        )
    observations = _selected_observations(field)
    if not observations:
        return "uncertain", [], "No selected observation carries the deciding fact."
    value = observations[0].get("value")
    evidence = _evidence_of(observations)
    if value is True:
        return "applicable", evidence, positive_label
    if value is False:
        return "not_applicable", evidence, negative_label
    if isinstance(value, str) and value.strip():
        return "applicable", evidence, positive_label
    return "uncertain", evidence, (
        "The extracted value does not state the deciding fact clearly enough "
        "to judge the type."
    )


def _assess_work_context(field: Mapping[str, Any] | None) -> tuple[str, list[dict], str]:
    if field is None or field.get("resolution_status") in {
        "unavailable", "not_applicable", "explicitly_absent", None
    }:
        return "uncertain", [], (
            "The records do not state whether the injury occurred during work "
            "activity. Silence is not treated as a negative finding."
        )
    if field.get("resolution_status") == "conflict":
        return "uncertain", [], (
            "Sources disagree on the work context; the disagreement is recorded "
            "as a conflict candidate rather than resolved here."
        )
    observations = _selected_observations(field)
    if not observations:
        return "uncertain", [], "No selected observation carries the work context."
    value = observations[0].get("value")
    evidence = _evidence_of(observations)
    if isinstance(value, str):
        if value in WORK_SUPPORTING:
            return "applicable", evidence, (
                "The records explicitly document the injury occurring during "
                "work activity."
            )
        if value in WORK_UNCERTAIN:
            return "uncertain", evidence, (
                "The context is a commute, business trip, or company event. "
                "Work connection is not explicit, so the type stays uncertain "
                "pending a human determination."
            )
        if value in WORK_NEGATIVE:
            return "not_applicable", evidence, (
                "A source states the injury was not work-related."
            )
    return "uncertain", evidence, (
        "The recorded work context does not map to an explicit work-activity "
        "finding."
    )


def assess_case_types(
    claim_facts: Sequence[Mapping[str, Any]],
    *,
    filing_status_by_type: Mapping[str, str] | None = None,
) -> list[dict]:
    """One independent assessment per case type, in a stable order.

    `filing_status_by_type` carries intake's recorded filing facts. A type
    absent from that mapping is `unknown` -- never `not_filed`, which is a
    positive claim that intake would have had to state.
    """
    fields = _field_by_id(claim_facts)
    filing = dict(filing_status_by_type or {})
    assessments: list[dict] = []

    for case_type in CASE_TYPES:
        trigger_ids = TYPE_TRIGGER_FIELDS[case_type]
        conflicting = [
            field_id for field_id in trigger_ids
            if fields.get(field_id, {}).get("resolution_status") == "conflict"
        ]

        if case_type == "industrial_accident":
            status, evidence, reason = _assess_work_context(
                fields.get("work_activity_context")
            )
        elif case_type == "traffic_accident":
            status, evidence, reason = _assess_boolean_trigger(
                fields.get("vehicle_involvement"),
                positive_label=(
                    "The injury event explicitly involves a vehicle or traffic "
                    "accident."
                ),
                negative_label=(
                    "A source states no vehicle was involved in the injury event."
                ),
            )
        elif case_type == "liability":
            status, evidence, reason = _assess_boolean_trigger(
                fields.get("facility_defect_or_third_party_responsibility"),
                positive_label=(
                    "The injury narrative identifies a facility defect or a "
                    "responsible act or omission by another party."
                ),
                negative_label=(
                    "A source states no third-party or facility responsibility "
                    "applies."
                ),
            )
        else:
            # Personal insurance is the deliberately broad label: a documented
            # external injury event makes the case a review target even when no
            # contract or filing record is in hand. The config states this
            # directly, and it is why `injury_event_present` is its trigger
            # rather than any contract-side fact.
            status, evidence, reason = _assess_boolean_trigger(
                fields.get("injury_event_present"),
                positive_label=(
                    "A documented external injury event makes this a "
                    "personal-insurance review target. Contract existence and "
                    "filing status are not determined at this stage."
                ),
                negative_label=(
                    "A source states no external injury event occurred."
                ),
            )

        triggered = [
            field_id for field_id in trigger_ids
            if status == "applicable" and field_id in fields
        ]
        if status == "applicable" and not triggered:
            # Never assert `applicable` without naming the field that carried it;
            # the schema requires at least one triggered field and one exact
            # reference for that status.
            status = "uncertain"
            reason = (
                "The deciding fact could not be bound to an extracted field."
            )
            evidence = []
        if status == "applicable" and not evidence:
            status = "uncertain"
            reason = (
                "The deciding fact carries no exact source evidence, so the "
                "type is not asserted."
            )

        assessments.append({
            "case_type": case_type,
            "status": status,
            "triggered_field_ids": sorted(triggered) if status == "applicable" else [],
            "conflicting_field_ids": sorted(conflicting),
            "filing_status": filing.get(case_type, "unknown"),
            "reason": reason,
            "evidence_references": evidence if status == "applicable" else [],
        })
    return assessments


def selected_case_types(assessments: Sequence[Mapping[str, Any]]) -> list[str]:
    """Types whose required-document checklist applies.

    Both `applicable` and `uncertain` count: a checklist exists to tell a
    reviewer what is missing, and an uncertain type is exactly the one whose
    missing document would settle it. Only `not_applicable` -- an affirmative
    negative from a source -- drops out.
    """
    return [
        row["case_type"] for row in assessments
        if row.get("status") in {"applicable", "uncertain"}
    ]
