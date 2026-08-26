"""A disputed value must not render as an absent one.

Section 1's summary lines take `_first_value`, which returns None for any
field whose `resolution_status` is not `asserted`. A `conflict` field
therefore renders 확인 불가 -- the same words the report uses when no source
mentioned the field at all.

Measured on CASE_7008 (2026-08-26). Line 4 of the report reads

    - 주요 진단명: 확인 불가

while line 71 of the SAME report carries both readings in full: 진단서
(DOC_002) records `요추1번 압박골절` with no qualifier, and 초진기록 (DOC_004)
records `Non traumatic Compression fracture vertebra, lumbar region`. That is
not a records gap -- it is the question the case turns on, because 외상성 vs
비외상성 decides whether the 상해 담보 applies at all. A reader of the summary
line is told the diagnosis could not be established; a reader of section 6 is
told two sources disagree about it.

`resolve_withdrawn_conflicts` already promotes candidates consistency_check
judged `consistent`, precisely so an agreed value stops printing 확인 불가
(CASE_049). What is left after that promotion is a REAL disagreement, and it
needs its own words rather than the vocabulary of absence.
"""
from __future__ import annotations

import run_screening_report as screening


def _conflict_fact(field_id, values, *, document_ids=("DOC_002", "DOC_004")):
    return {
        "field_id": field_id,
        "resolution_status": "conflict",
        "selected_observation_ids": [],
        "conflict_candidate_ids": ["CAC_0001"],
        "observations": [
            {
                "observation_id": f"CAO_000{i}",
                "value_state": "asserted",
                "value": value,
                "evidence_references": [{
                    "document_id": doc, "page": 1, "quote": value,
                    "start_char": 0, "end_char": len(value)}],
            }
            for i, (value, doc) in enumerate(zip(values, document_ids), start=1)
        ],
    }


def test_a_conflicted_diagnosis_does_not_read_as_unestablished():
    facts = {"primary_diagnosis": _conflict_fact(
        "primary_diagnosis",
        ["요추1번 압박골절", "Non traumatic Compression fracture"])}
    rendered = screening.summary_diagnosis_text(facts)

    assert rendered != "확인 불가", (
        "a disputed diagnosis reads as one the records could not establish")
    assert "요추1번 압박골절" in rendered
    assert "Non traumatic" in rendered


def test_a_conflicted_line_says_the_sources_disagree():
    """The reader needs to know WHY two values are shown, not just see two."""
    facts = {"primary_diagnosis": _conflict_fact(
        "primary_diagnosis", ["A진단", "B진단"])}
    rendered = screening.summary_diagnosis_text(facts)
    assert "불일치" in rendered or "상충" in rendered, rendered


def test_a_genuinely_absent_diagnosis_still_reads_확인_불가():
    """The guard must not cost the honest case its honest wording."""
    facts = {"primary_diagnosis": {
        "field_id": "primary_diagnosis",
        "resolution_status": "unavailable",
        "unavailable_reason": "not_mentioned",
        "selected_observation_ids": [],
        "observations": [],
    }}
    assert screening.summary_diagnosis_text(facts) == "확인 불가"


def test_an_asserted_diagnosis_renders_its_value():
    facts = {"primary_diagnosis": {
        "field_id": "primary_diagnosis",
        "resolution_status": "asserted",
        "selected_observation_ids": ["OBS_1"],
        "observations": [{"observation_id": "OBS_1", "value": "요추1번 압박골절",
                          "evidence_references": []}],
    }}
    assert screening.summary_diagnosis_text(facts) == "요추1번 압박골절"
