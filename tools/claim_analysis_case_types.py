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
    "liability": ("facility_defect_or_third_party_responsibility",
                  "liability_opinion_conclusion"),
}

# What `liability_opinion_conclusion` may say, and what it means for the type.
# An opinion finding the insured IS liable establishes the type. One finding no
# liability does NOT rule it out -- that is a party's position on the very
# question in dispute, not an affirmative statement that no third-party
# responsibility exists. On CASE_053 the insurer's opinion concluded 불성립
# while the claimant's concluded 성립 on identical facts; treating the first as
# `not_applicable` would let one side's counsel close the case type.
LIABILITY_OPINION_POSITIVE = frozenset({"성립"})
LIABILITY_OPINION_NEUTRAL = frozenset({"불성립", "판단유보"})

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
            "판정 기준이 되는 사실이 추출되지 않아, 확보된 자료만으로는 "
            "유형을 판정할 수 없습니다."
        )
    status = field.get("resolution_status")
    if status == "conflict":
        return "uncertain", [], (
            "판정 기준이 되는 사실에 대해 출처 간 기재가 다릅니다. 이 단계에서 "
            "결론을 내리지 않고 충돌 후보로 기록했습니다."
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
            "판정 기준이 되는 사실을 기재한 출처가 없습니다. 기재가 없다는 점을 "
            "부정 소견으로 보지는 않습니다."
        )
    observations = _selected_observations(field)
    if not observations:
        return "uncertain", [], "선택된 관측값에 판정 기준이 되는 사실이 없습니다."
    value = observations[0].get("value")
    evidence = _evidence_of(observations)
    if value is True:
        return "applicable", evidence, positive_label
    if value is False:
        return "not_applicable", evidence, negative_label
    if isinstance(value, str) and value.strip():
        return "applicable", evidence, positive_label
    return "uncertain", evidence, (
        "추출된 값만으로는 판정 기준이 되는 사실이 분명하지 않아 유형을 "
        "판정할 수 없습니다."
    )


def _assess_work_context(field: Mapping[str, Any] | None) -> tuple[str, list[dict], str]:
    if field is None or field.get("resolution_status") in {
        "unavailable", "not_applicable", "explicitly_absent", None
    }:
        return "uncertain", [], (
            "업무 수행 중 발생한 상해인지 자료에 기재되어 있지 않습니다. 기재가 "
            "없다는 점을 부정 소견으로 보지는 않습니다."
        )
    if field.get("resolution_status") == "conflict":
        return "uncertain", [], (
            "업무 관련성에 대해 출처 간 기재가 다릅니다. 이 단계에서 결론을 "
            "내리지 않고 충돌 후보로 기록했습니다."
        )
    observations = _selected_observations(field)
    if not observations:
        return "uncertain", [], "선택된 관측값에 업무 관련 정황이 없습니다."
    value = observations[0].get("value")
    evidence = _evidence_of(observations)
    if isinstance(value, str):
        if value in WORK_SUPPORTING:
            return "applicable", evidence, (
                "업무 수행 중 상해가 발생했다고 자료에 명시되어 있습니다."
            )
        if value in WORK_UNCERTAIN:
            return "uncertain", evidence, (
                "출퇴근, 출장 또는 회사 행사 중 발생한 정황입니다. 업무 관련성이 "
                "명시되어 있지 않아, 담당자 판단 전까지 불확실로 둡니다."
            )
        if value in WORK_NEGATIVE:
            return "not_applicable", evidence, (
                "업무와 관련이 없다고 기재한 출처가 있습니다."
            )
    return "uncertain", evidence, (
        "기재된 정황이 업무 수행 중이라는 판단으로 바로 이어지지 않습니다."
    )


def _assess_liability(
    defect_field: Mapping[str, Any] | None,
    opinion_field: Mapping[str, Any] | None,
) -> tuple[str, list[dict], str]:
    """Liability reads the accident facts OR a legal opinion, whichever answers.

    Added 2026-08-21. The type used to rest on
    `facility_defect_or_third_party_responsibility` alone, which routes to
    medical documents -- and no 진료기록 discusses stair de-icing, so on
    CASE_053 the field was `unavailable` and the type came back `uncertain`
    while two 법률의견서 in the same case argued the question directly.
    """
    status, evidence, reason = _assess_boolean_trigger(
        defect_field,
        positive_label=(
            "사고 경위에 시설 하자 또는 제3자의 작위·부작위 책임이 "
            "특정되어 있습니다."
        ),
        negative_label=(
            "제3자 또는 시설 책임이 없다고 기재한 출처가 있습니다."
        ),
    )
    if status == "applicable":
        return status, evidence, reason

    opinions = _selected_observations(opinion_field) if opinion_field else []
    values = [observation.get("value") for observation in opinions]
    supporting = [observation for observation in opinions
                  if observation.get("value") in LIABILITY_OPINION_POSITIVE]
    if supporting:
        opposed = any(value in LIABILITY_OPINION_NEUTRAL for value in values)
        return ("applicable", _evidence_of(supporting),
                "법률의견서가 배상책임 성립을 명시했습니다."
                + (" 다른 의견서는 반대 결론이므로 성립 여부 자체가 쟁점입니다."
                   if opposed else ""))
    if values:
        return ("uncertain", [],
                "법률의견서가 배상책임 불성립 또는 판단유보로 회신했습니다. "
                "이는 다투는 쟁점에 대한 일방의 견해이므로, 배상책임 해당 "
                "없음으로 단정하지 않습니다.")
    return status, evidence, reason


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
                    "차량 또는 교통사고가 사고 경위에 명시되어 있습니다."
                ),
                negative_label=(
                    "사고 경위에 차량이 관여하지 않았다고 기재한 출처가 있습니다."
                ),
            )
        elif case_type == "liability":
            status, evidence, reason = _assess_liability(
                fields.get("facility_defect_or_third_party_responsibility"),
                fields.get("liability_opinion_conclusion"),
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
                    "외상 사고가 자료에 기재되어 있어 개인보험 검토 대상입니다. "
                    "계약 존재 여부와 접수 상태는 이 단계에서 판정하지 않습니다."
                ),
                negative_label=(
                    "외상 사고가 없었다고 기재한 출처가 있습니다."
                ),
            )

        # A field only counts as having triggered the verdict if it actually
        # carries a value. `field_id in fields` was enough while every type had
        # ONE trigger, but liability now has two and only one may have answered:
        # on CASE_053 the medical-routed defect field is `unavailable` while the
        # legal opinion establishes the type, and naming both would credit an
        # empty field with the finding.
        triggered = [
            field_id for field_id in trigger_ids
            if status == "applicable"
            and fields.get(field_id, {}).get("resolution_status") == "asserted"
        ]
        if status == "applicable" and not triggered:
            # Never assert `applicable` without naming the field that carried it;
            # the schema requires at least one triggered field and one exact
            # reference for that status.
            status = "uncertain"
            reason = (
                "판정 기준이 되는 사실을 추출된 항목에 연결하지 못했습니다."
            )
            evidence = []
        if status == "applicable" and not evidence:
            status = "uncertain"
            reason = (
                "판정 기준이 되는 사실에 정확한 출처 근거가 없어 유형을 "
                "단정하지 않습니다."
            )

        assessments.append({
            "case_type": case_type,
            "status": status,
            "triggered_field_ids": sorted(triggered) if status == "applicable" else [],
            "conflicting_field_ids": sorted(conflicting),
            "filing_status": filing.get(case_type, "unknown"),
            "reason": reason,
            # Both decided verdicts carry their basis. `not_applicable` needs
            # it as much as `applicable` does -- it is an affirmative negative
            # from a source, and the schema requires the quote that established
            # it. Only `uncertain` shows none, because nothing decided it.
            "evidence_references": (
                evidence if status in {"applicable", "not_applicable"} else []),
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
