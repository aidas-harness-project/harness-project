"""Stage cost, reported so it cannot be read as a sum of parallel work.

Twice in one session an assistant read `_timing_summary.json` and reported a
stage's cost wrong, in opposite directions:

* `observed_tool_overlap_s` (already a union) was described as "tool time"
  and its complement as "time no tool was running" -- which happened to be
  right, but for the wrong reason;
* then, correcting that, the SUM of `provider.transcribe_image` self-time
  (1344.7s across 34 calls) was reported as the wall cost of a stage whose
  whole attempt was 1300.6s. Those 34 calls ran 6.3x parallel and occupied
  214.0s of wall.

A number that exceeds the window it sits inside is the tell, and nothing in
the summary makes the distinction legible: `by_op` carries self-time sums,
`by_stage` carries unions, and both are bare floats.

`stage_cost_rows` computes both from the spans and labels them, so a caller
cannot pick up the sum believing it is elapsed time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import trace_aggregate


_EPOCH = datetime(2026, 8, 20, tzinfo=timezone.utc)


def _at(offset_s: float) -> str:
    return (_EPOCH + timedelta(seconds=offset_s)).isoformat()


def _span(op, start, duration, *, stage="document_processing", category="provider"):
    return {
        "op": op, "category": category, "duration_s": duration,
        "stage_name": stage, "stage_attempt": 1,
        # Offset in seconds from a fixed epoch. Formatting `start` straight
        # into the seconds field silently produced "00:00:200.000" for any
        # offset past a minute, which does not parse and dropped the span.
        "t_start_wall": _at(start),
    }


def test_parallel_calls_report_wall_below_their_sum():
    """Four 10s calls that all start together cost 10s, not 40s."""
    spans = [_span("provider.transcribe_image", 0, 10) for _ in range(4)]
    row = trace_aggregate.stage_cost_rows(spans)["document_processing"]
    assert row["tool_wall_s"] == pytest.approx(10.0)
    assert row["tool_self_time_s"] == pytest.approx(40.0)
    assert row["max_concurrency"] == pytest.approx(4.0)


def test_sequential_calls_report_wall_equal_to_their_sum():
    spans = [_span("provider.classify_document", i * 10, 10) for i in range(3)]
    row = trace_aggregate.stage_cost_rows(spans)["document_processing"]
    assert row["tool_wall_s"] == pytest.approx(30.0)
    assert row["tool_self_time_s"] == pytest.approx(30.0)
    assert row["max_concurrency"] == pytest.approx(1.0)


def test_partial_overlap_counts_the_union_once():
    """0-10 and 5-15 occupy 15s of wall, not 20."""
    spans = [_span("provider.transcribe_image", 0, 10),
             _span("provider.transcribe_image", 5, 10)]
    row = trace_aggregate.stage_cost_rows(spans)["document_processing"]
    assert row["tool_wall_s"] == pytest.approx(15.0)
    assert row["tool_self_time_s"] == pytest.approx(20.0)


def test_each_stage_keeps_its_own_wall():
    spans = [_span("provider.transcribe_image", 0, 10),
             _span("provider.extract", 0, 30, stage="claim_analysis")]
    rows = trace_aggregate.stage_cost_rows(spans)
    assert rows["document_processing"]["tool_wall_s"] == pytest.approx(10.0)
    assert rows["claim_analysis"]["tool_wall_s"] == pytest.approx(30.0)


def test_a_stage_with_no_tool_spans_reports_zero_not_missing():
    """claim_analysis on CASE_489 held 0.5s of tools across 15 minutes. Zero
    is the finding; absent would read as 'not measured'."""
    rows = trace_aggregate.stage_cost_rows([])
    assert rows == {}
    rows = trace_aggregate.stage_cost_rows([
        _span("marker.start", 0, 0, category="marker")])
    assert rows.get("document_processing", {}).get("tool_wall_s", 0) == 0


def test_the_top_operations_are_ranked_by_wall_not_by_sum():
    """The ranking that matters: 34 parallel OCR calls summing to 1344s cost
    less wall than one 400s serial driver, and a report ordered by sum puts
    them the wrong way round."""
    spans = [_span("provider.transcribe_image", 0, 100) for _ in range(10)]
    spans.append(_span("stage2.driver", 200, 400, category="compute"))
    row = trace_aggregate.stage_cost_rows(spans)["document_processing"]
    assert [op for op, _ in row["top_ops_by_wall"]][0] == "stage2.driver"


def test_container_spans_do_not_appear_as_operations():
    """`pool.*` and `stage.*` WRAP their children and carry category
    "compute", so an unfiltered ranking lists a pool alongside the very calls
    it contains -- on CASE_489, `pool.documents` and `stage.document` both
    ranked above the OCR pool they enclose. The stage total is a union and is
    unaffected; the per-operation ranking is not."""
    spans = [
        _span("pool.ocr_pages", 0, 100, category="compute"),
        _span("provider.transcribe_image", 0, 50),
        _span("provider.transcribe_image", 50, 50),
    ]
    row = trace_aggregate.stage_cost_rows(spans)["document_processing"]
    ops = [op for op, _ in row["top_ops_by_wall"]]
    assert "pool.ocr_pages" not in ops
    assert ops == ["provider.transcribe_image"]


def test_a_container_still_counts_toward_the_stage_wall():
    """Excluding containers from the RANKING must not shrink the stage total:
    a pool that ran while nothing else was traced still occupied that wall."""
    spans = [_span("pool.ocr_pages", 0, 100, category="compute")]
    row = trace_aggregate.stage_cost_rows(spans)["document_processing"]
    assert row["tool_wall_s"] == pytest.approx(100.0)
