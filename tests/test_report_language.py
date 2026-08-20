"""The screening report is a Korean deliverable, in Korean.

CLAUDE.md: documentation, code and agent definitions are English; the two
exceptions are raw source material and "the actual deliverable documents the
pipeline produces (screening report, draft report -- Korean, since they're
submitted to Korean-speaking professionals)".

The report was assembled from prose written in English at three producers --
the case-type assessor's `reason`, the policy linker's `uncertainty_reason`
and per-requirement `reason`, and the selective driver's `resolution_reason`
-- plus domain and field labels taken from the routing config, which had only
English names. CASE_489's published report therefore read

    - 개인보험: 해당 — A documented external injury event makes this a
      personal-insurance review target.
    - Diagnosis
      - Clinical department: 정형외과

to a 손해사정사. These tests pin the language of what reaches the reader.
Codes (`field_id`, `case_type`, `status`) stay English: they are identifiers,
not prose, and the report never prints them raw.
"""
from __future__ import annotations

import re

import claim_analysis_case_types as case_types
import claim_analysis_policy_links as policy_links
import medical_document_routing as routing
import run_claim_analysis_selective as selective


HANGUL = re.compile(r"[가-힣]")


# ---------------------------------------------------------- config labels --

def test_every_domain_and_field_carries_a_korean_label():
    config = routing.load_routing_config()
    for domain in config["domains"]:
        assert HANGUL.search(domain["label_ko"]), domain["code"]
    for field in config["fields"]:
        assert HANGUL.search(field["label_ko"]), field["field_id"]


def test_the_english_label_is_kept_beside_it():
    """`label` is what the code, tests and changelog name a field by. The
    Korean name is added beside it, not swapped in for it."""
    config = routing.load_routing_config()
    for field in config["fields"]:
        assert field["label"] and not HANGUL.search(field["label"])


# ------------------------------------------------------- case type reasons --

def test_absent_trigger_reason_is_korean():
    status, _, reason = case_types._assess_boolean_trigger(
        None, positive_label="예", negative_label="아니오")
    assert status == "uncertain"
    assert HANGUL.search(reason), reason


def test_unavailable_trigger_reason_is_korean():
    _, _, reason = case_types._assess_boolean_trigger(
        {"resolution_status": "unavailable"},
        positive_label="예", negative_label="아니오")
    assert HANGUL.search(reason), reason


def test_conflicting_trigger_reason_is_korean():
    _, _, reason = case_types._assess_boolean_trigger(
        {"resolution_status": "conflict"},
        positive_label="예", negative_label="아니오")
    assert HANGUL.search(reason), reason


def test_work_context_silence_reason_is_korean():
    _, _, reason = case_types._assess_work_context(None)
    assert HANGUL.search(reason), reason


def test_every_case_type_verdict_reason_is_korean():
    """The whole assessor, driven end to end on a case that establishes an
    injury event and nothing else -- the CASE_489 shape."""
    claim_facts = [{
        "field_id": "injury_event_present",
        "resolution_status": "asserted",
        "selected_observation_ids": ["OBS_1"],
        "observations": [{"observation_id": "OBS_1", "value": True,
                          "document_id": "DOC_013", "page": 1,
                          "quote": "P.I> 전일 slip down."}],
    }]
    assessments = case_types.assess_case_types(claim_facts)
    assert assessments
    for assessment in assessments:
        assert HANGUL.search(assessment["reason"]), assessment


# ----------------------------------------------------------- policy links --

def test_no_policy_layer_reason_is_korean():
    claim_facts = [{
        "field_id": "surgery_or_major_procedure_status",
        "resolution_status": "asserted",
        "selected_observation_ids": ["OBS_1"],
        "observations": [{"observation_id": "OBS_1", "value": True,
                          "document_id": "DOC_014", "page": 1,
                          "quote": "수 술 기 록"}],
    }]
    links = policy_links.build_policy_links(
        claim_facts=claim_facts, manifest={"documents": []}, index=None,
        verify_quote=lambda *a: None)
    assert links
    for link in links:
        assert HANGUL.search(link["uncertainty_reason"] or ""), link


# ------------------------------------------------- selective driver reasons --

def test_field_stop_reasons_are_korean():
    """`resolution_reason` is printed verbatim in section 7 (주요 미확인 항목)."""
    outcome = selective.FieldExtractionOutcome(
        field_id="accident_date", domain_code="event_timeline", grade="A")
    assert HANGUL.search(outcome.reason), outcome.reason


def test_the_two_filing_route_reasons_are_korean_and_distinct():
    """Both print in the report, and they say different things about the
    case: one route never triggered, the other has no producer at all."""
    deferred = selective.FILING_ROUTE_DEFERRED_REASON
    not_triggered = selective.FILING_ROUTE_NOT_TRIGGERED_REASON
    assert HANGUL.search(deferred) and HANGUL.search(not_triggered)
    assert deferred != not_triggered
