"""The industrial-accident filing route, and why it is deferred rather than done.

This file replaces `test_industrial_filing_wiring.py`, which tested a reader
over two contracts -- `_intake_declaration.json` and
`_industrial_accident_filing.json` -- that nothing in this repository produces.
No schema, no DAO subcommand, no writer, no intake step, no classification kind.
The tests passed because their own fixtures constructed the file contents and
handed them straight to the reader.

That is worth naming precisely, because it looked exactly like working code:
the reader was careful, refused an entry with no citation, refused a medical
field, and never inferred filing from the accident. All correct, and all
unreachable. A phantom contract does not fail -- it reports `unavailable` on
every real case forever, while the code and its tests suggest a working
integration.

So the route is deferred, and these tests pin the deferral itself: the status
is `unknown` (never `not_filed`), the fields resolve `unavailable` for a reason
that names the missing producer rather than a missing document, and the one
rule that must survive whoever builds it later is still enforced -- filing is
never inferred from how the accident happened.
"""
from __future__ import annotations

import copy

import pytest

import claim_analysis_case_types as case_types
import claim_analysis_contracts as contracts
import run_claim_analysis_selective as driver


# ------------------------------------------------- the phantom is really gone --

def test_the_unproduced_contract_names_are_not_read_anywhere() -> None:
    """The reader and its filenames are removed, not merely unused.

    Left in place, they would keep advertising an integration that does not
    exist -- and the next person to touch this would have no way to tell the
    dead seam from a live one.
    """
    for name in ("INTAKE_CONTRACT", "FILING_CONTRACT", "read_filing_sources",
                 "make_filing_reader", "filing_status_from_sources",
                 "FILING_FIELD_SOURCES"):
        assert not hasattr(driver, name), f"{name} should have been removed"


def test_the_deferral_is_recorded_where_a_reader_would_look() -> None:
    reason = driver.FILING_ROUTE_DEFERRED_REASON
    assert "no producer" in reason
    assert "unknown" in reason
    assert "deferred" in reason


# ------------------------------------------------------------ filing status --

def test_every_case_type_reports_unknown_filing_status() -> None:
    """Empty mapping: `assess_case_types` already renders that as `unknown`."""
    assert driver.filing_status_by_case_type() == {}


def test_silence_never_becomes_not_filed() -> None:
    """The one direction that would be a fabricated administrative finding.

    `not_filed` asserts somebody recorded that no claim was made. Nobody did.
    """
    assessments = case_types.assess_case_types(
        [], filing_status_by_type=driver.filing_status_by_case_type())
    industrial = next(row for row in assessments
                      if row["case_type"] == "industrial_accident")
    assert industrial["filing_status"] == "unknown"


def test_a_work_accident_does_not_imply_a_filing(monkeypatch) -> None:
    """Applicable 산재 and filing status are independent facts.

    This is the inference the whole deferral exists to keep out: an accident at
    work makes the type applicable and says nothing about whether a claim was
    filed.
    """
    claim_facts = [{
        "field_id": "work_activity_context",
        "resolution_status": "asserted",
        "selected_observation_ids": ["CAO_0001"],
        "observations": [{
            "observation_id": "CAO_0001", "value": "work_activity",
            "evidence_references": [{
                "document_id": "DOC_001", "page": 1,
                "quote": "작업 중 사다리에서 추락", "start_char": 0, "end_char": 13}],
        }],
    }]
    assessments = case_types.assess_case_types(
        claim_facts, filing_status_by_type=driver.filing_status_by_case_type())
    industrial = next(row for row in assessments
                      if row["case_type"] == "industrial_accident")

    assert industrial["status"] == "applicable"
    assert industrial["filing_status"] == "unknown"


# ----------------------------------------------------- the fields themselves --

def _filing_outcomes(trigger_value: str | None) -> dict:
    config = copy.deepcopy(contracts.load_default_routing_config())
    documents = [driver.selection.DocumentRef("DOC_001", "diagnosis_certificate")]

    pages = {("DOC_001", 1): "진 단 서\n작업 중 사다리에서 추락\n"}

    def extract(document_id, kind, field_rows):
        if trigger_value is None:
            return {}
        wanted = {row["field_id"] for row in field_rows}
        if "work_activity_context" not in wanted:
            return {}
        return {"work_activity_context": {
            "value": trigger_value, "page": 1, "quote": "작업 중 사다리에서 추락"}}

    outcomes, _ = driver.extract_all(
        config=config, documents=documents, extract=extract, page_text=pages,
        observation_ids=driver._observation_id_sequence())
    return {outcome.field_id: outcome for outcome in outcomes}


FILING_FIELDS = ("industrial_accident_filing_basis",
                 "industrial_accident_approval_status",
                 "industrial_accident_approved_diagnosis")


def test_the_filing_fields_are_unavailable_for_the_deferred_reason() -> None:
    """Triggered, and still unavailable -- because there is nothing to read."""
    outcomes = _filing_outcomes("work_activity")
    for field_id in FILING_FIELDS:
        outcome = outcomes[field_id]
        assert outcome.status == "unavailable"
        assert outcome.unavailable_reason == "outside_poc_scope"
        assert outcome.reason == driver.FILING_ROUTE_DEFERRED_REASON


def test_an_untriggered_route_says_the_trigger_never_fired() -> None:
    """A different reason, because it is a different fact about the case."""
    outcomes = _filing_outcomes(None)
    for field_id in FILING_FIELDS:
        outcome = outcomes[field_id]
        assert outcome.status == "unavailable"
        assert outcome.reason != driver.FILING_ROUTE_DEFERRED_REASON
        assert "never activated" in outcome.reason


def test_the_route_opens_no_document_either_way() -> None:
    """Deferred means costing nothing, not reading medical records instead."""
    config = copy.deepcopy(contracts.load_default_routing_config())
    config["fields"] = [row for row in config["fields"]
                        if row["field_id"] in FILING_FIELDS]
    opened: list[str] = []

    def extract(document_id, kind, field_rows):
        opened.append(document_id)
        return {}

    driver.extract_all(
        config=config,
        documents=[driver.selection.DocumentRef("DOC_001", "diagnosis_certificate")],
        extract=extract, page_text={("DOC_001", 1): "진 단 서"},
        observation_ids=driver._observation_id_sequence())

    assert opened == []


# ------------------------------------------------ the trigger, once defined --

@pytest.mark.parametrize("value", sorted(case_types.WORK_SUPPORTING))
def test_every_work_supporting_value_activates_the_route(value: str) -> None:
    """P0-5: the gate and the verdict read the same constant.

    `on_duty` used to be accepted by `assess_case_types` and rejected here, so
    one rule could call the case 산재 while the other refused it the route.
    """
    outcome = driver.FieldExtractionOutcome(
        field_id="work_activity_context", domain_code="event_timeline", grade="C")
    outcome.status = "asserted"
    outcome.observations = [{"observation_id": "CAO_0001", "value": value}]
    assert driver.industrial_trigger_active(
        {"work_activity_context": outcome}) is True


@pytest.mark.parametrize(
    "value", sorted(case_types.WORK_UNCERTAIN | case_types.WORK_NEGATIVE))
def test_a_non_supporting_value_does_not_activate_the_route(value: str) -> None:
    """통근·출장·행사 do not establish work connection on their own, and
    `not_work_related` affirmatively denies it."""
    outcome = driver.FieldExtractionOutcome(
        field_id="work_activity_context", domain_code="event_timeline", grade="C")
    outcome.status = "asserted"
    outcome.observations = [{"observation_id": "CAO_0001", "value": value}]
    assert driver.industrial_trigger_active(
        {"work_activity_context": outcome}) is False


def test_a_conflicted_or_silent_trigger_does_not_activate_the_route() -> None:
    silent = driver.FieldExtractionOutcome(
        field_id="work_activity_context", domain_code="event_timeline", grade="C")
    assert driver.industrial_trigger_active({}) is False
    assert driver.industrial_trigger_active(
        {"work_activity_context": silent}) is False

    conflicted = driver.FieldExtractionOutcome(
        field_id="work_activity_context", domain_code="event_timeline", grade="C")
    conflicted.status = "conflict"
    conflicted.observations = [
        {"observation_id": "CAO_0001", "value": "work_activity"},
        {"observation_id": "CAO_0002", "value": "not_work_related"},
    ]
    assert driver.industrial_trigger_active(
        {"work_activity_context": conflicted}) is False


def test_an_applicable_verdict_activates_it_independently() -> None:
    assert driver.industrial_trigger_active({}, [
        {"case_type": "industrial_accident", "status": "applicable"}]) is True
    assert driver.industrial_trigger_active({}, [
        {"case_type": "industrial_accident", "status": "uncertain"}]) is False
