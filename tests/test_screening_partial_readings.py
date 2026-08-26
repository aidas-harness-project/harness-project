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


# ------------------------------- 3. a disputed field is an outstanding item --
# The same defect once more, in the one place left. `unconfirmed_section` took
# only `resolution_status == "unavailable"`, and section 6 takes only conflicts
# consistency_check CONFIRMED into the ledger -- so a conflict that never
# reached the ledger fell between them and the report omitted the field
# entirely, with no "see the conflict section" pointer either.
#
# Measured across the CASE_7* corpus (2026-08-26): of 101 conflicted fields, 19
# reached section 1's summary lines and 20 had a ledger entry, leaving **62
# shown nowhere at all** -- among them `diagnosis_laterality`, where 좌 against
# 우 decides which 담보 pays.


def _conflict_field(field_id: str, readings):
    return {
        "field_id": field_id,
        "resolution_status": "conflict",
        "stop_reason": "conflict_found",
        "resolution_reason": "두 출처가 서로 다른 값을 기재하고 있습니다",
        "selected_observation_ids": [],
        "conflict_candidate_ids": ["CAC_0001"],
        "observations": [
            {
                "observation_id": f"CAO_000{index}",
                "value_state": "asserted",
                "value": value,
                "evidence_references": [{
                    "document_id": document_id, "page": 1, "quote": value,
                    "start_char": 0, "end_char": len(str(value))}],
            }
            for index, (value, document_id) in enumerate(readings, start=1)
        ],
    }


def test_a_conflicted_field_reaches_the_rendered_section():
    report = _report([_conflict_field(
        "primary_diagnosis", [("좌측 골절", "DOC_002"), ("우측 골절", "DOC_005")])])
    assert "주요 진단명" in _section_seven(report)


def test_the_row_carries_both_readings_and_their_documents():
    """The row sends a reviewer to decide between two records, so it cannot
    then withhold which records to open."""
    report = _report([_conflict_field(
        "primary_diagnosis", [("좌측 골절", "DOC_002"), ("우측 골절", "DOC_005")])])
    rendered = _section_seven(report)
    assert "좌측 골절" in rendered and "우측 골절" in rendered
    assert "DOC_002" in rendered and "DOC_005" in rendered


def test_the_row_is_grouped_as_disputed():
    report = _report([_conflict_field(
        "primary_diagnosis", [("좌측 골절", "DOC_002"), ("우측 골절", "DOC_005")])])
    row = next(r for r in report["unconfirmed_items"]
               if r["field_id"] == "primary_diagnosis")
    assert row["gap_kind"] == "disputed"
    assert row["unavailable_reason"] == "conflict_unresolved"


def test_a_conflict_section_six_already_carries_produces_no_row():
    """The narrowing, and today the branch that fires for every real conflict.

    A conflict with a ledger entry is held by section 6 with the
    `professional_summary` written when the evidence was in hand, and routed by
    section 10 to a named reviewer. A row here would be a third mention
    carrying strictly less than either.

    Measured across the CASE_7* corpus (2026-08-26): all 25 surviving conflicts
    have an entry, so this is not an edge case -- it is the normal path.
    """
    report = screening.build_report(
        case_id="CASE_9003", run_id="RUN_20260826_1",
        claim_analysis={"case_id": "CASE_9003",
                        "claim_facts": [_conflict_field(
                            "primary_diagnosis",
                            [("좌측 골절", "DOC_002"), ("우측 골절", "DOC_005")])],
                        "case_type_assessment": [],
                        "required_document_checklist": []},
        consistency={"work_items": []}, config=CONFIG,
        conflict_entries={"CONFLICT_1": {"field_or_topic": "primary_diagnosis",
                                         "conflict_id": "CONFLICT_1"}})
    assert not [r for r in report["unconfirmed_items"]
                if r.get("gap_kind") == "disputed"], (
        "section 6 already carries this conflict with more than the row says")


def test_a_conflict_with_no_ledger_entry_still_renders():
    """The common case -- 76 of the corpus's 101 conflicts have no entry -- and
    the reason the row must carry the readings rather than point at section 6.
    """
    report = _report([_conflict_field(
        "primary_diagnosis", [("좌측 골절", "DOC_002"), ("우측 골절", "DOC_005")])])
    row = next(r for r in report["unconfirmed_items"]
               if r["field_id"] == "primary_diagnosis")
    assert "CONFLICT" not in row["reason"]
    assert len(row["partial_readings"]) == 2


def test_readings_differing_only_in_case_produce_no_row():
    """CASE_7015's `diagnosis_code`: `S52590` against `s52590`, one code twice.
    Listing it as an item to resolve manufactures work."""
    report = _report([_conflict_field(
        "documented_disability_rate", [("S52590", "DOC_002"),
                                       ("s52590", "DOC_005")])])
    assert not [r for r in report["unconfirmed_items"]
                if r.get("gap_kind") == "disputed"]


def test_a_conflict_row_carries_no_why_marker():
    """`why` names which trusted-value requirement a PARTIAL reading failed. A
    conflict's readings each met the bar -- what is unresolved is which one the
    field takes -- so there is nothing to name."""
    report = _report([_conflict_field(
        "primary_diagnosis", [("좌측 골절", "DOC_002"), ("우측 골절", "DOC_005")])])
    row = next(r for r in report["unconfirmed_items"]
               if r["field_id"] == "primary_diagnosis")
    assert all("why" not in reading for reading in row["partial_readings"])
    assert "()" not in _section_seven(report), (
        "an empty parenthesis was rendered where `why` would have gone")


def test_a_conflict_with_no_ledger_entry_is_the_only_one_that_gets_a_row():
    """The narrowing must not become a hole.

    Section 7 gives up its `disputed` rows on the claim that section 6 has
    them. That claim holds only while every surviving conflict reaches the
    ledger -- if one stops doing so, section 6 will not carry it either and the
    field goes back to being omitted, which is the defect this whole file is
    about. Asserted directly rather than left to the corpus check below, so it
    holds on a case that has not been run yet.
    """
    report = _report([_conflict_field(
        "primary_diagnosis", [("좌측 골절", "DOC_002"), ("우측 골절", "DOC_005")])])
    rows = [r for r in report["unconfirmed_items"]
            if r.get("gap_kind") == "disputed"]
    assert len(rows) == 1, "a ledger-less conflict must still get a row"
    assert len(rows[0]["partial_readings"]) == 2


def test_no_surviving_conflict_is_omitted_from_every_section():
    """The property, over the real corpus: a conflict that survived
    consistency_check appears in section 1, or the ledger (section 6), or as a
    section 7 row. Never nowhere.

    This is the check that would have caught the miscount made while building
    this feature -- 62 conflicts were believed unshown because
    `resolve_withdrawn_conflicts` was fed `consistency_check_workitems.json`
    instead of the `evidence_validation_result.json` the driver reads. Reading
    the same contract the driver does is the point.
    """
    import pytest

    cases = sorted((ROOT / "outputs").glob("CASE_7*/claim_analysis_result.json"))
    if not cases:
        pytest.skip("no CASE_7* corpus in this checkout")

    config = json.loads(
        (ROOT / "config" / "claim_analysis" /
         "claim_analysis_routing_v0.1.json").read_text(encoding="utf-8"))
    summary_fields = set(screening.SUMMARY_FACT_FIELDS.values())
    orphans = []
    for path in cases:
        folder = path.parent
        validation = folder / "evidence_validation_result.json"
        if not validation.exists():
            continue
        analysis = json.loads(path.read_text(encoding="utf-8"))
        consistency = json.loads(validation.read_text(encoding="utf-8"))
        ledger_path = folder / "_conflict_ledger.json"
        entries = {}
        if ledger_path.exists():
            entries = {row["conflict_id"]: row for row in
                       json.loads(ledger_path.read_text(encoding="utf-8"))
                       .get("conflicts") or []}
        report = screening.build_report(
            case_id=folder.name, run_id="RUN_20260826_1",
            claim_analysis=analysis, consistency=consistency, config=config,
            conflict_entries=entries)

        facts = {row["field_id"]: row for row in analysis["claim_facts"]}
        resolved = screening.resolve_withdrawn_conflicts(facts, consistency)
        in_ledger = {entry.get("field_or_topic") for entry in entries.values()}
        in_seven = {row["field_id"] for row in report["unconfirmed_items"]
                    if row.get("gap_kind") == "disputed"}
        for field_id, field in resolved.items():
            if field.get("resolution_status") != "conflict":
                continue
            if field_id in summary_fields or field_id in in_ledger                     or field_id in in_seven:
                continue
            orphans.append(f"{folder.name}/{field_id}")
    assert not orphans, (
        "these conflicts survived review and appear in NO section: " +
        ", ".join(orphans))
