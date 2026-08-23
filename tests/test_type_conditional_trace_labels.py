"""The trace must not say a document was skipped when stage 3-a read it.

Origin (CASE_713, 2026-08-23): `metrics.type_conditional_documents_read` was 3
and `claim_analysis_result.json` carried verbatim quotes from DOC_006/007/008,
while the SAME run's `claim_analysis_trace.json` listed all three as
`disposition: skipped / wave: not_read / reason: "no medical classification, so
no route reaches it"`. The disposition rows are built from the medical waves,
which run before 3-a exists, so they never saw the round's reads. Anyone
auditing cost or coverage from the trace alone would conclude 3-a never ran.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import run_claim_analysis_selective as driver


def _rows():
    return [
        {"document_id": "DOC_006", "disposition": "skipped", "wave": "not_read",
         "reason": "no medical classification, so no route reaches it",
         "field_ids": []},
        {"document_id": "DOC_007", "disposition": "skipped", "wave": "not_read",
         "reason": "no medical classification, so no route reaches it",
         "field_ids": []},
        {"document_id": "DOC_013", "disposition": "read", "wave": "B",
         "reason": "opened once for 3 routed field(s)",
         "field_ids": ["accident_mechanism"]},
    ]


def _plan():
    return [
        {"document_id": "DOC_006", "source_type": "legal_opinion",
         "priority_rank": 1,
         "field_ids": ["legal_basis_cited", "liability_opinion_conclusion"]},
        {"document_id": "DOC_007", "source_type": "insurer_response",
         "priority_rank": 2, "field_ids": ["liability_opinion_conclusion"]},
    ]


def test_documents_stage_3a_read_are_not_labelled_skipped():
    rows = _rows()
    driver._restamp_type_conditional(rows, _plan())
    by_id = {row["document_id"]: row for row in rows}

    for document_id in ("DOC_006", "DOC_007"):
        row = by_id[document_id]
        assert row["disposition"] == "read", (
            f"{document_id} was opened by stage 3-a but the trace still calls "
            f"it {row['disposition']!r}")
        assert row["wave"] == "type_conditional"
        assert "no route reaches it" not in row["reason"]
        assert row["field_ids"], "a read document must name the fields it served"


def test_the_source_type_reaches_the_reason():
    """A reviewer reading the trace should see WHY the document was opened --
    a 법률의견서 and an insurer letter are not interchangeable evidence."""
    rows = _rows()
    driver._restamp_type_conditional(rows, _plan())
    by_id = {row["document_id"]: row for row in rows}
    assert "legal_opinion" in by_id["DOC_006"]["reason"]
    assert "insurer_response" in by_id["DOC_007"]["reason"]


def test_medical_wave_rows_are_untouched():
    """Only planned documents are restamped; a wave-B read keeps its label."""
    rows = _rows()
    driver._restamp_type_conditional(rows, _plan())
    row = {r["document_id"]: r for r in rows}["DOC_013"]
    assert row["wave"] == "B"
    assert row["reason"] == "opened once for 3 routed field(s)"


def test_field_ids_union_does_not_drop_medical_routing():
    """A field can be routed medically AND re-opened by 3-a. Overwriting
    instead of unioning would understate one of the two."""
    rows = [{"document_id": "DOC_006", "disposition": "read", "wave": "B",
             "reason": "opened once for 1 routed field(s)",
             "field_ids": ["accident_date"]}]
    driver._restamp_type_conditional(rows, _plan())
    assert rows[0]["field_ids"] == [
        "accident_date", "legal_basis_cited", "liability_opinion_conclusion"]


def test_empty_plan_changes_nothing():
    rows = _rows()
    before = [dict(row) for row in rows]
    driver._restamp_type_conditional(rows, [])
    assert rows == before


def test_documents_read_metric_excludes_the_3a_round():
    """`type_conditional_documents_read` is kept separate from
    `documents_read` by the schema so an SLA regression in 3-a cannot hide in
    a number that scales with the field catalogue. Now that 3-a's documents are
    stamped `read`, the common count must exclude them by wave."""
    rows = _rows()
    driver._restamp_type_conditional(rows, _plan())
    trace = driver.build_trace(
        case_id="CASE_713", run_id="RUN_X",
        config={"config_version": "claim_analysis_routing.v0.1"},
        outcomes=[], document_dispositions=rows, provider_calls=7,
        type_conditional_documents=2, type_conditional_calls=2)
    assert trace["metrics"]["documents_read"] == 1, (
        "the two stage 3-a documents must not be counted as common-pass reads")
    assert trace["metrics"]["type_conditional_documents_read"] == 2
