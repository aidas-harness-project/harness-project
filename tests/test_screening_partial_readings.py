"""A cited value the run could not trust must still reach the reviewer.

Two defects, one section. `claim_analysis` publishes a field as `unavailable`
with `unavailable_reason: partial_reading_only` when a priority source DID cite
a value that failed the trusted-value requirements. The evidence is preserved
in the contract precisely so a person can read it and decide.

Section 7 then did two things that undid that:

1. **The rows were dropped entirely.** The renderer iterated a hardcoded four
   `gap_kind` values -- records_gap, disputed, not_searched, out_of_scope --
   while `UNAVAILABLE_KIND` maps six. `partial_reading` and `source_blank` were
   in `unconfirmed_items` in the JSON and absent from the markdown beside it,
   with nothing indicating anything was missing. Measured across the CASE_7*
   corpus (2026-08-26): **132 of 734 rows never reached a reader** -- 72 fields
   holding a real cited quote and 60 printed-and-blank cells.

2. **The surviving text named no source.** The row's own sentence reads
   "인용은 보존되어 있으므로 검토자가 원문을 확인해야 합니다", and the row
   carried no quote, no document id and no page. It told a reviewer to go and
   check a source, then withheld which one -- so confirming a single field
   meant opening the JSON contract by hand.

Both are the same underlying mistake as the 확인 불가 rendering: a value the
pipeline holds, described to the reader as something it does not have.
"""
from __future__ import annotations

import json
from pathlib import Path

import run_screening_report as screening

ROOT = Path(__file__).resolve().parents[1]


def _partial_field(field_id: str, readings, *, reason="부분 기재"):
    """A field shaped like a real `partial_reading_only` publish."""
    return {
        "field_id": field_id,
        "resolution_status": "unavailable",
        "unavailable_reason": "partial_reading_only",
        "stop_reason": "partial_value_only",
        "resolution_reason": reason,
        "selected_observation_ids": [],
        "observations": [
            {
                "observation_id": f"CAO_000{index}",
                "value_state": "asserted",
                "value": value,
                "partial_reading": True,
                "complete": complete,
                "unambiguous": unambiguous,
                "evidence_references": [{
                    "document_id": document_id, "page": 1, "quote": quote,
                    "start_char": 0, "end_char": len(quote)}],
            }
            for index, (value, complete, unambiguous, document_id, quote)
            in enumerate(readings, start=1)
        ],
    }


def _blank_field(field_id: str):
    return {
        "field_id": field_id,
        "resolution_status": "unavailable",
        "unavailable_reason": "printed_but_blank",
        "stop_reason": "printed_but_blank_only",
        "resolution_reason": "서식에 항목이 인쇄되어 있으나 칸이 비어 있습니다",
        "selected_observation_ids": [],
        "observations": [{
            "observation_id": "CAO_0001",
            "value_state": "printed_but_blank",
            "reason": "빈 칸",
            "evidence_references": [{
                "document_id": "DOC_002", "page": 1, "quote": "| 기왕증 | |",
                "start_char": 0, "end_char": 11}],
        }],
    }


CONFIG = {
    "domains": [],
    "fields": [
        {"field_id": "documented_disability_rate", "extraction_wave": "B",
         "label_ko": "기존 평가상 장해율"},
        {"field_id": "patient_reported_prior_same_site_history",
         "extraction_wave": "B", "label_ko": "환자 진술 기왕증"},
        {"field_id": "primary_diagnosis", "extraction_wave": "A",
         "label_ko": "주요 진단명"},
    ],
}


def _report(facts):
    return screening.build_report(
        case_id="CASE_9003", run_id="RUN_20260826_1",
        claim_analysis={"case_id": "CASE_9003", "claim_facts": facts,
                        "case_type_assessment": [],
                        "required_document_checklist": []},
        consistency={"work_items": []}, config=CONFIG, conflict_entries={})


def _section_seven(report):
    for section in screening.markdown_sections(report):
        if "미확인" in section["heading"]:
            return section["content"]
    raise AssertionError("section 7 was not rendered at all")


# ------------------------------------------------- 1. the dropped rows --

def test_a_partial_reading_row_reaches_the_rendered_section():
    report = _report([_partial_field(
        "documented_disability_rate",
        [(10, True, False, "DOC_002", "약간의 신경장해 (지급율 10%)")])])
    assert _section_seven(report).count("기존 평가상 장해율") == 1


def test_a_printed_but_blank_row_reaches_the_rendered_section():
    report = _report([_blank_field("patient_reported_prior_same_site_history")])
    assert "환자 진술 기왕증" in _section_seven(report)


def test_every_published_row_is_rendered():
    """The property that makes the loop total, rather than four more names.

    A `gap_kind` added to UNAVAILABLE_KIND but forgotten here still renders --
    at the end, unlabelled if need be, but present. Silence is the one outcome
    this section may not produce.
    """
    report = _report([
        _partial_field("documented_disability_rate",
                       [(10, True, False, "DOC_002", "지급율 10%")]),
        _blank_field("patient_reported_prior_same_site_history"),
    ])
    rendered = _section_seven(report)
    for row in report["unconfirmed_items"]:
        assert row["label"] in rendered, (
            f"{row['label']} ({row['gap_kind']}) is in unconfirmed_items and "
            f"absent from the section a reviewer reads")


def test_an_unknown_gap_kind_is_still_rendered():
    """Directly: the sweep, not the ordered tuple, is what guarantees it."""
    report = _report([_partial_field(
        "documented_disability_rate",
        [(10, True, False, "DOC_002", "지급율 10%")])])
    report["unconfirmed_items"].append({
        "field_id": "invented", "label": "새로운 종류",
        "gap_kind": "a_kind_nobody_added_yet", "reason": "테스트"})
    assert "새로운 종류" in _section_seven(report)


# ---------------------------------------- 2. the readings it points at --

def test_the_row_names_the_document_it_sends_the_reviewer_to():
    report = _report([_partial_field(
        "documented_disability_rate",
        [(10, True, False, "DOC_002", "약간의 신경장해 (지급율 10%)")])])
    rendered = _section_seven(report)
    assert "10" in rendered
    assert "DOC_002" in rendered, (
        "the row tells the reviewer to check the source and does not say which")


def test_the_reading_says_which_requirement_it_failed():
    """`불완전` and `다의적` ask different things of a reader: one says read the
    rest of the page, the other says decide between readings."""
    report = _report([_partial_field(
        "documented_disability_rate",
        [(10, False, True, "DOC_002", "지급율 10%")])])
    assert "불완전" in _section_seven(report)

    report = _report([_partial_field(
        "documented_disability_rate",
        [(10, True, False, "DOC_002", "지급율 10%")])])
    assert "다의적" in _section_seven(report)


def test_both_failures_are_named_together():
    report = _report([_partial_field(
        "documented_disability_rate",
        [(10, False, False, "DOC_002", "지급율 10%")])])
    assert "불완전·다의적" in _section_seven(report)


def test_every_reading_is_listed_not_only_the_first():
    """CASE_7003's 시설 하자 field holds three readings across three documents.
    Showing one would make a disagreement look like a single weak value."""
    report = _report([_partial_field(
        "documented_disability_rate",
        [(10, True, False, "DOC_002", "지급율 10%"),
         (15, True, False, "DOC_004", "지급율 15%")])])
    rendered = _section_seven(report)
    assert "DOC_002" in rendered and "DOC_004" in rendered


def test_a_blank_row_carries_no_readings():
    """A printed-and-blank cell is not a value a reviewer confirms; inventing a
    reading for it would be the mirror of the defect."""
    report = _report([_blank_field("patient_reported_prior_same_site_history")])
    row = report["unconfirmed_items"][0]
    assert "partial_readings" not in row


def test_the_report_still_validates():
    """`partial_readings` is a new key on a previously unconstrained object --
    the schema's own comment records that as how a field went unpublished
    before, so it is declared rather than left to `additionalProperties`."""
    import sys
    tools = str(ROOT / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from _validation import load_registry, validate_instance

    report = _report([
        _partial_field("documented_disability_rate",
                       [(10, False, False, "DOC_002", "지급율 10%")]),
        _blank_field("patient_reported_prior_same_site_history"),
    ])
    report["report_path"] = "outputs/CASE_9003/screening_report.md"
    schemas, registry = load_registry()
    errors = validate_instance(
        report, "screening_report_selective.schema.json", schemas, registry)
    assert not errors, errors


def test_the_corpus_rows_that_were_dropped_are_real():
    """Guards the measurement this file's docstring rests on, so the numbers
    stay checkable rather than becoming folklore. Skips when the corpus is not
    present, since the fix does not depend on it."""
    import pytest

    reports = sorted((ROOT / "outputs").glob("CASE_7*/screening_report.json"))
    if not reports:
        pytest.skip("no CASE_7* corpus in this checkout")
    kinds = set()
    for path in reports:
        data = json.loads(path.read_text(encoding="utf-8"))
        for row in data.get("unconfirmed_items") or []:
            kinds.add(row.get("gap_kind") or "records_gap")
    assert {"partial_reading", "source_blank"} <= kinds, (
        f"the corpus no longer holds the dropped kinds; found {sorted(kinds)}")
