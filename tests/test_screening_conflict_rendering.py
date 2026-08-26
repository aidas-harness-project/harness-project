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
    rendered = screening.summary_fact_text(facts, "primary_diagnosis")

    assert rendered != "확인 불가", (
        "a disputed diagnosis reads as one the records could not establish")
    assert "요추1번 압박골절" in rendered
    assert "Non traumatic" in rendered


def test_a_conflicted_line_says_the_sources_disagree():
    """The reader needs to know WHY two values are shown, not just see two."""
    facts = {"primary_diagnosis": _conflict_fact(
        "primary_diagnosis", ["A진단", "B진단"])}
    rendered = screening.summary_fact_text(facts, "primary_diagnosis")
    assert "불일치" in rendered or "상충" in rendered, rendered


def test_a_conflicted_line_names_its_ledger_entry():
    """The summary must point at the entry that carries the full disagreement.

    Without the reference a reader sees two values and has no way to reach the
    professional_summary, the sources, or the disposition -- all of which live
    on the ledger entry. CASE_7008's is CONFLICT_1 on `primary_diagnosis`.
    """
    facts = {"primary_diagnosis": _conflict_fact(
        "primary_diagnosis", ["요추1번 압박공절", "Non traumatic Compression fracture"])}
    entries = {"CONFLICT_1": {"field_or_topic": "primary_diagnosis",
                              "verdict": "deferred_to_report"}}
    rendered = screening.summary_fact_text(facts, "primary_diagnosis", entries)
    assert "CONFLICT_1" in rendered, rendered


def test_the_reference_is_omitted_when_no_entry_matches():
    """A candidate that never reached the ledger has no id to cite, and an
    invented one would point at nothing."""
    facts = {"primary_diagnosis": _conflict_fact(
        "primary_diagnosis", ["A진단", "B진단"])}
    rendered = screening.summary_fact_text(
        facts, "primary_diagnosis",
        {"CONFLICT_1": {"field_or_topic": "diagnosis_site"}})
    assert "CONFLICT" not in rendered, rendered
    assert "불일치" in rendered


def test_a_genuinely_absent_diagnosis_still_reads_확인_불가():
    """The guard must not cost the honest case its honest wording."""
    facts = {"primary_diagnosis": {
        "field_id": "primary_diagnosis",
        "resolution_status": "unavailable",
        "unavailable_reason": "not_mentioned",
        "selected_observation_ids": [],
        "observations": [],
    }}
    assert screening.summary_fact_text(facts, "primary_diagnosis") == "확인 불가"


def test_an_asserted_diagnosis_renders_its_value():
    facts = {"primary_diagnosis": {
        "field_id": "primary_diagnosis",
        "resolution_status": "asserted",
        "selected_observation_ids": ["OBS_1"],
        "observations": [{"observation_id": "OBS_1", "value": "요추1번 압박골절",
                          "evidence_references": []}],
    }}
    assert screening.summary_fact_text(facts, "primary_diagnosis") == "요추1번 압박골절"


# ---------------------------------------- generalized beyond the diagnosis --
# The defect was never specific to `primary_diagnosis`; that is only where it
# was first seen. Every section-1 line runs through `_first_value`, so an
# accident date or a KCD code two sources disagree about rendered 확인 불가 the
# same way. Measured on the CASE_7* corpus (2026-08-26): 101 conflicted fields
# across 24 cases, of which only 25 have a conflict-ledger entry -- so for 76 of
# them the summary line was the ONLY place the disagreement could have shown,
# and it said the records established nothing.


def test_a_conflicted_accident_date_names_the_disagreement():
    """`accident_date` carried `format: date` in the schema, which is why this
    also required dropping that format -- a disagreement is not a date."""
    facts = {"accident_date": _conflict_fact(
        "accident_date", ["2024-03-11", "2024-03-13"])}
    rendered = screening.summary_fact_text(facts, "accident_date")
    assert rendered != "확인 불가"
    assert "2024-03-11" in rendered and "2024-03-13" in rendered


def test_a_conflicted_kcd_code_names_the_disagreement():
    facts = {"diagnosis_code": _conflict_fact(
        "diagnosis_code", ["S52590", "S62101"])}
    rendered = screening.summary_fact_text(facts, "diagnosis_code")
    assert "S52590" in rendered and "S62101" in rendered


def test_identical_readings_are_not_shown_as_a_disagreement():
    """CASE_7015 records `diagnosis_code` as a conflict between `S52590` and
    `s52590` -- one code written twice, differing only in case. Rendering
    "자료 간 불일치" there would manufacture a question for a reviewer to
    resolve, which is the mirror of the defect this file exists for.
    """
    facts = {"diagnosis_code": _conflict_fact(
        "diagnosis_code", ["S52590", "S52590"])}
    rendered = screening.summary_fact_text(facts, "diagnosis_code")
    assert rendered == "S52590", rendered
    assert "불일치" not in rendered


def test_a_conflict_with_no_readable_values_falls_back():
    """A conflict whose observations carry no value must not print an empty
    disagreement -- there is nothing to show a reviewer."""
    facts = {"primary_diagnosis": {
        "field_id": "primary_diagnosis",
        "resolution_status": "conflict",
        "selected_observation_ids": [],
        "observations": [{"observation_id": "OBS_1", "value": None,
                          "evidence_references": []}],
    }}
    assert screening.summary_fact_text(facts, "primary_diagnosis") == "확인 불가"


def test_a_conflicted_treatment_period_travels_as_text_not_a_block():
    """The period is the one summary fact rendered from a dict (start ~ end).
    A conflict has no single start to put there, so it must travel beside the
    block or it falls back into the 확인 불가 pile the rest of this file is
    about."""
    field = _conflict_fact("treatment_period", ["2024-03-11 ~ 2024-05-02",
                                                "2024-03-11 ~ 2024-06-30"])
    rendered = screening.conflict_text(field, "treatment_period")
    assert rendered is not None
    assert "2024-05-02" in rendered and "2024-06-30" in rendered


# ---------------------------------- the wiring, not just the helper ---------
# Reverting the `build_report` call sites to `_first_value` left every test
# above passing, because they exercise the helper directly. A helper that
# renders correctly and is not called is the same defect with extra steps, so
# these assert on the assembled contract.


def _minimal_analysis(facts):
    return {
        "case_id": "CASE_9002",
        "claim_facts": list(facts),
        "case_type_assessment": [],
        "required_document_checklist": [],
    }


def _built(facts, conflict_entries=None):
    return screening.build_report(
        case_id="CASE_9002", run_id="RUN_20260826_1",
        claim_analysis=_minimal_analysis(facts),
        consistency={"work_items": []}, config={"domains": [], "fields": []},
        conflict_entries=conflict_entries or {},
    )


def test_build_report_renders_a_conflicted_accident_date():
    report = _built([_conflict_fact("accident_date", ["2024-03-11", "2024-03-13"])])
    assert report["case_summary"]["accident_date"] != "확인 불가"
    assert "2024-03-11" in report["case_summary"]["accident_date"]


def test_build_report_renders_a_conflicted_kcd_code():
    report = _built([_conflict_fact("diagnosis_code", ["S52590", "S62101"])])
    assert "S62101" in report["case_summary"]["kcd_code"]


def test_build_report_carries_a_conflicted_period_as_text():
    report = _built([_conflict_fact(
        "treatment_period", ["2024-03-11 ~ 2024-05-02", "2024-03-11 ~ 2024-06-30"])])
    summary = report["case_summary"]
    assert "treatment_period" not in summary, (
        "a conflicted period has no single start_date to publish")
    assert "2024-06-30" in summary["treatment_period_text"]


def test_the_built_report_still_validates():
    """The rendered disagreement travels through the same schema every real
    write does.

    `screening_report_selective.schema.json` -- NOT `screening_report`, which
    belongs to the legacy lane. `run_screening_report.SCHEMA` names the
    selective one and that is what `write-contract` validates against, so a
    test pointed at the other passes while the real write fails. That mistake
    was made while writing this file: the format was dropped from the legacy
    schema and all five real screening tests still failed.

    `accident_date` carried `format: date` and the format checker IS enabled
    (`_validation.validate_instance`), so this is the assertion that catches an
    unpropagated axis -- a failure mode this project has hit repeatedly."""
    import sys
    from pathlib import Path
    tools = str(Path(__file__).resolve().parents[1] / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from _validation import load_registry, validate_instance

    report = _built([
        _conflict_fact("accident_date", ["2024-03-11", "2024-03-13"]),
        _conflict_fact("treatment_period",
                       ["2024-03-11 ~ 2024-05-02", "2024-03-11 ~ 2024-06-30"]),
    ])
    report["report_path"] = "outputs/CASE_9002/screening_report.md"
    schemas, registry = load_registry()
    errors = validate_instance(
        report, "screening_report_selective.schema.json", schemas, registry)
    assert not errors, errors
