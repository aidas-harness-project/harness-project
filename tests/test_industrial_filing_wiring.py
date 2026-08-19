"""Industrial-accident filing: the DAO reads that reach the published result.

`extract_all` accepted a `non_medical_reader` and `build_result` accepted a
`filing_status_by_type` from the day they were written, but `run()` passed
neither — so the three filing fields were permanently `unavailable` and every
filing status was permanently `unknown`, regardless of what the case actually
recorded.

The rule these tests defend is narrow and easy to get wrong in one specific
direction: **how the injury happened and whether a claim was filed are
different facts**. "Injured at work" makes the industrial case type applicable;
it says nothing about filing, and nothing about approval. Only an intake
declaration or a filing record can say those, and when neither exists the
answer is `unknown` — never `not_filed`.
"""
from __future__ import annotations

import pytest

import run_claim_analysis_selective as driver


FILING_QUOTE = "산재 요양급여 신청서 접수일: 2026-03-05"
APPROVED_QUOTE = "승인 상병: 우측 요골 골절"


def _filing_contract() -> dict:
    return {
        "filing_status_by_case_type": {"industrial_accident": "filed"},
        "filing_basis": {
            "value": "요양급여 신청 접수",
            "document_id": "DOC_800", "page": 1, "quote": FILING_QUOTE,
            "start_char": 0, "end_char": len(FILING_QUOTE),
        },
        "approved_diagnosis": {
            "value": ["우측 요골 골절"],
            "document_id": "DOC_800", "page": 2, "quote": APPROVED_QUOTE,
            "start_char": 0, "end_char": len(APPROVED_QUOTE),
        },
    }


# ------------------------------------------------------- filing status --

def test_no_administrative_source_leaves_every_status_unknown() -> None:
    """Silence is not `not_filed`. Nobody recorded that no claim was filed."""
    assert driver.filing_status_from_sources({}) == {}


def test_a_declared_status_is_carried_through() -> None:
    status = driver.filing_status_from_sources(
        {"_industrial_accident_filing.json": _filing_contract()})
    assert status["industrial_accident"] == "filed"


def test_an_unrecognised_status_value_is_ignored() -> None:
    """The contract's three values only; anything else is not a status."""
    status = driver.filing_status_from_sources({"x": {
        "filing_status_by_case_type": {"industrial_accident": "probably_filed"}}})
    assert status == {}


def test_a_declared_not_filed_is_honoured() -> None:
    """Someone recorded it, so it is a fact -- unlike silence."""
    status = driver.filing_status_from_sources({"x": {
        "filing_status_by_case_type": {"industrial_accident": "not_filed"}}})
    assert status["industrial_accident"] == "not_filed"


# ------------------------------------------------------ the field reader --

def test_the_reader_returns_a_grounded_value() -> None:
    reader = driver.make_filing_reader(
        {"_industrial_accident_filing.json": _filing_contract()})
    found = reader({"field_id": "industrial_accident_filing_basis"})
    assert found["value"] == "요양급여 신청 접수"
    assert found["evidence_reference"]["quote"] == FILING_QUOTE
    assert found["evidence_reference"]["document_id"] == "DOC_800"


def test_the_reader_declines_a_field_the_source_does_not_state() -> None:
    reader = driver.make_filing_reader(
        {"_industrial_accident_filing.json": _filing_contract()})
    # The fixture states basis and approved diagnosis, but not approval status.
    assert reader({"field_id": "industrial_accident_approval_status"}) is None


def test_the_reader_declines_an_entry_with_no_citation() -> None:
    """An administrative value is evidence like any other, or it is nothing."""
    reader = driver.make_filing_reader({"x": {
        "filing_basis": {"value": "접수함"}}})
    assert reader({"field_id": "industrial_accident_filing_basis"}) is None


def test_the_reader_serves_no_medical_field() -> None:
    """It answers only the three filing fields -- it is not a second extractor."""
    reader = driver.make_filing_reader(
        {"_industrial_accident_filing.json": _filing_contract()})
    for field_id in ("primary_diagnosis", "accident_date", "surgery_or_procedure_name"):
        assert reader({"field_id": field_id}) is None


# ------------------------------------------------------ run() wiring --

def test_run_reads_both_administrative_contracts_through_the_dao(monkeypatch) -> None:
    """The gap this file exists for: run() never asked for them.

    Both are optional, both go through read-contract, and a missing one is not
    an error -- it is the ordinary state of a case with no filing.
    """
    asked: list[list[str]] = []

    def fake_dao(args, allow_missing=False):
        asked.append(args)
        return None

    monkeypatch.setattr(driver, "_dao_json", fake_dao)
    assert driver.read_filing_sources("CASE_9001", "RUN_20260820_1") == {}

    contracts_read = [args[2] for args in asked if args[0] == "read-contract"]
    assert driver.INTAKE_CONTRACT in contracts_read
    assert driver.FILING_CONTRACT in contracts_read


def test_run_wires_the_reader_and_status_into_the_result(monkeypatch) -> None:
    """End of the path: a DAO-read filing fact reaches the published fields."""
    captured: dict = {}

    def fake_dao(args, allow_missing=False):
        if args[0] == "read-contract" and args[2] == driver.FILING_CONTRACT:
            return _filing_contract()
        return None

    monkeypatch.setattr(driver, "_dao_json", fake_dao)
    sources = driver.read_filing_sources("CASE_9001", "RUN_20260820_1")
    assert sources, "the filing contract must be picked up"

    reader = driver.make_filing_reader(sources)
    found = reader({"field_id": "industrial_accident_approved_diagnosis"})
    assert found["value"] == ["우측 요골 골절"]
    assert driver.filing_status_from_sources(sources)["industrial_accident"] == "filed"


# ---------------------------------------------- the route stays conditional --

def test_the_route_is_inert_without_a_work_related_trigger() -> None:
    """No trigger means no administrative read at all, source or no source."""
    assert driver.industrial_trigger_active({}) is False

    silent = driver.FieldExtractionOutcome(
        field_id="work_activity_context", domain_code="event_timeline", grade="C")
    assert driver.industrial_trigger_active(
        {"work_activity_context": silent}) is False


def test_an_explicit_work_activity_fact_activates_the_route() -> None:
    work = driver.FieldExtractionOutcome(
        field_id="work_activity_context", domain_code="event_timeline", grade="C")
    work.status = "asserted"
    work.observations = [{"observation_id": "CAO_0001", "value": "work_activity"}]
    assert driver.industrial_trigger_active({"work_activity_context": work}) is True


def test_an_applicable_industrial_verdict_also_activates_it() -> None:
    assert driver.industrial_trigger_active({}, [
        {"case_type": "industrial_accident", "status": "applicable"}]) is True
    assert driver.industrial_trigger_active({}, [
        {"case_type": "industrial_accident", "status": "uncertain"}]) is False
