"""Tool spans name their owning stage, so concurrency stays measurable.

Before this, a span carried no stage at all and the aggregator inferred
ownership from whether the span's wall interval fell inside a stage's marker
window. That works only while one stage is open at a time. CASE_142 ran
`policy_clause_processing` and `denial_response` concurrently and every
per-stage tool figure came back null -- correctly, since a timestamp cannot
say which of two open stages a span belongs to, and charging it to both would
double-count.

The channel is the marker directory the DAO already writes, NOT an environment
variable: a stage spans processes the orchestrator never forks (a subagent's
tool call is a separate session, and each shell invocation starts a fresh
environment), so an exported variable provably cannot reach the tools whose
spans need attributing. Marker files are visible to every process.
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import dao
import trace as trace_mod
import trace_aggregate


BASE = datetime(2026, 8, 14, 0, 0, 0, tzinfo=timezone.utc)
RUN = "RUN_20260814_200"
CASE = "CASE_998"


@pytest.fixture(autouse=True)
def _reset_trace_state():
    trace_mod.reset()
    yield
    trace_mod.reset()


def _record(span_id, op, *, offset, duration=0.0, category="marker",
            attempt=None, attrs=None, **extra):
    record = {
        "schema_version": "0.1",
        "span_id": span_id,
        "parent_span_id": None,
        "run_id": RUN,
        "case_id": CASE,
        "doc_id": None,
        "page": None,
        "op": op,
        "category": category,
        "t_start_wall": (BASE + timedelta(seconds=offset)).isoformat(),
        "t_start_mono": offset,
        "duration_s": duration,
        "status": "ok",
        "attempt": attempt,
        "shard_id": "1_1_a",
        "pid": 1,
        "thread": 1,
        "attrs": attrs or {},
    }
    record.update(extra)
    return record


def _start(stage, attempt, offset):
    return _record(f"start-{stage}-{attempt}", "stage.attempt.start",
                   offset=offset, attempt=attempt,
                   attrs={"marker_kind": "stage_attempt_start",
                          "stage_name": stage})


def _end(stage, attempt, offset):
    return _record(f"end-{stage}-{attempt}", "stage.attempt.end",
                   offset=offset, attempt=attempt,
                   attrs={"marker_kind": "stage_attempt_end",
                          "stage_name": stage, "attempt_outcome": "passed"})


def _owned(span_id, stage, attempt, offset, duration):
    return _record(span_id, "dao.read_contract", offset=offset,
                   duration=duration, category="io",
                   stage_name=stage, stage_attempt=attempt)


def _unowned(span_id, offset, duration):
    return _record(span_id, "dao.read_contract", offset=offset,
                   duration=duration, category="io")


def _summary(spans, run_state_stages=None):
    return trace_aggregate.summarize(
        spans, case_id=CASE, run_id=RUN, generated_at=BASE.isoformat(),
        dropped_span_lines=0, shard_count=1,
        run_state_stages=run_state_stages)


def _by_name(summary):
    return {a["stage_name"]: a for a in summary["stage_attempts"]}


# --- the point of the exercise --------------------------------------------

def test_fully_owned_concurrent_stages_each_report_their_own_tool_time():
    """The CASE_142 scenario, with ownership recorded. Both attempts overlap
    completely, and neither figure may leak into the other."""
    summary = _summary([
        _start("denial_response", 1, 0),
        _start("policy_clause_processing", 1, 0),
        _owned("t1", "denial_response", 1, 10, 20),
        _owned("t2", "policy_clause_processing", 1, 15, 30),
        _end("policy_clause_processing", 1, 60),
        _end("denial_response", 1, 100),
    ])
    attempts = _by_name(summary)

    denial = attempts["denial_response"]
    policy = attempts["policy_clause_processing"]
    assert denial["attribution_status"] == "complete_explicit_ownership"
    assert policy["attribution_status"] == "complete_explicit_ownership"
    # Each sees only its own span, despite the windows overlapping.
    assert denial["observed_tool_overlap_s"] == pytest.approx(20.0)
    assert policy["observed_tool_overlap_s"] == pytest.approx(30.0)
    assert denial["unattributed_active_s"] == pytest.approx(80.0)
    assert policy["unattributed_active_s"] == pytest.approx(30.0)


def test_owned_spans_do_not_leak_into_a_concurrent_stage():
    """A span owned by one stage must contribute nothing to the other, even
    though it sits squarely inside the other's window."""
    summary = _summary([
        _start("denial_response", 1, 0),
        _start("policy_clause_processing", 1, 0),
        _owned("t1", "denial_response", 1, 10, 20),
        _end("policy_clause_processing", 1, 60),
        _end("denial_response", 1, 100),
    ])

    assert _by_name(summary)["policy_clause_processing"][
        "owned_tool_overlap_s"] == pytest.approx(0.0)


def test_by_stage_reports_attribution_complete_under_concurrency():
    summary = _summary([
        _start("denial_response", 1, 0),
        _start("policy_clause_processing", 1, 0),
        _owned("t1", "denial_response", 1, 10, 20),
        _owned("t2", "policy_clause_processing", 1, 15, 30),
        _end("policy_clause_processing", 1, 60),
        _end("denial_response", 1, 100),
    ])

    assert all(stage["attribution_complete"]
               for stage in summary["by_stage"].values())


# --- honesty guards: the fail-closed path must survive ---------------------

def test_unowned_span_in_a_contested_window_still_fails_closed():
    """Explicit ownership must not become a blanket pass. An unowned span
    inside two overlapping windows is exactly what the original fail-closed
    behaviour exists for -- charging it to both would double-count it."""
    summary = _summary([
        _start("denial_response", 1, 0),
        _start("policy_clause_processing", 1, 0),
        _unowned("t1", 10, 20),
        _end("policy_clause_processing", 1, 60),
        _end("denial_response", 1, 100),
    ])

    for attempt in summary["stage_attempts"]:
        assert attempt["attribution_status"] == "overlapping_stage_attempts"
        assert attempt["observed_tool_overlap_s"] is None
        assert attempt["unattributed_active_s"] is None


def test_contested_window_keeps_the_owned_lower_bound():
    """Falling back for the inferred part must not discard the part that WAS
    measured explicitly."""
    summary = _summary([
        _start("denial_response", 1, 0),
        _start("policy_clause_processing", 1, 0),
        _owned("t1", "denial_response", 1, 10, 20),
        _unowned("t2", 15, 5),
        _end("policy_clause_processing", 1, 60),
        _end("denial_response", 1, 100),
    ])
    denial = _by_name(summary)["denial_response"]

    assert denial["attribution_status"] == "overlapping_stage_attempts"
    assert denial["observed_tool_overlap_s"] is None
    assert denial["owned_tool_overlap_s"] == pytest.approx(20.0)


def test_sequential_stages_are_unaffected():
    """Non-overlapping stages never needed explicit ownership; the inferred
    path must keep working unchanged for every trace written before this."""
    summary = _summary([
        _start("claim_analysis", 1, 0),
        _unowned("t1", 10, 20),
        _end("claim_analysis", 1, 50),
        _start("denial_response", 1, 60),
        _unowned("t2", 70, 10),
        _end("denial_response", 1, 100),
    ])
    attempts = _by_name(summary)

    assert attempts["claim_analysis"]["attribution_status"] == "complete"
    assert attempts["claim_analysis"]["observed_tool_overlap_s"] == pytest.approx(20.0)
    assert attempts["denial_response"]["observed_tool_overlap_s"] == pytest.approx(10.0)


def test_ambiguous_span_is_reported_but_never_added_to_observed():
    """A span written while several stages were open names them all as
    candidates. That time is real and belongs to exactly one of them, so it is
    surfaced separately rather than silently attributed or silently dropped."""
    summary = _summary([
        _start("denial_response", 1, 0),
        _start("policy_clause_processing", 1, 0),
        _record("t1", "dao.read_contract", offset=10, duration=20,
                category="io",
                stage_candidates=[{"stage_name": "denial_response", "attempt": 1},
                                  {"stage_name": "policy_clause_processing",
                                   "attempt": 1}]),
        _end("policy_clause_processing", 1, 60),
        _end("denial_response", 1, 100),
    ])
    denial = _by_name(summary)["denial_response"]

    assert denial["ambiguous_tool_overlap_s"] == pytest.approx(20.0)
    assert denial["owned_tool_overlap_s"] == pytest.approx(0.0)
    assert denial["attribution_status"] == "overlapping_stage_attempts"


# --- trace.py: the writer side --------------------------------------------

def _scratch_case(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    outputs = tmp / "outputs"
    (outputs / CASE).mkdir(parents=True)
    monkeypatch.setattr(dao, "OUTPUTS", outputs)
    trace_mod.reset()
    return outputs


def _spans_for(case_id, run_id):
    spans, dropped, _ = trace_aggregate.read_shards(
        dao.trace_spans_dir(case_id, run_id))
    assert dropped == 0
    return {s["op"]: s for s in spans}


def test_writer_stamps_the_single_open_stage_onto_a_span(monkeypatch):
    _scratch_case(monkeypatch)
    dao._emit_stage_attempt_marker(CASE, RUN, "denial_response", 1, "start")

    with trace_mod.span("tool.work", category="io", case_id=CASE):
        pass

    span = _spans_for(CASE, RUN)["tool.work"]
    assert span["stage_name"] == "denial_response"
    assert span["stage_attempt"] == 1


def test_writer_records_candidates_when_several_stages_are_open(monkeypatch):
    _scratch_case(monkeypatch)
    dao._emit_stage_attempt_marker(CASE, RUN, "denial_response", 1, "start")
    dao._emit_stage_attempt_marker(
        CASE, RUN, "policy_clause_processing", 1, "start")
    trace_mod._stage_cache.update({"signature": None, "stages": ()})

    with trace_mod.span("tool.work", category="io", case_id=CASE):
        pass

    span = _spans_for(CASE, RUN)["tool.work"]
    assert "stage_name" not in span
    assert {c["stage_name"] for c in span["stage_candidates"]} == {
        "denial_response", "policy_clause_processing"}


def test_writer_reattributes_after_a_stage_closes(monkeypatch):
    """A long-lived process outlives stage transitions. Resolving once at
    configure() would mislabel every span after the first."""
    _scratch_case(monkeypatch)
    dao._emit_stage_attempt_marker(CASE, RUN, "denial_response", 1, "start")
    dao._emit_stage_attempt_marker(
        CASE, RUN, "policy_clause_processing", 1, "start")
    dao._emit_stage_attempt_marker(
        CASE, RUN, "policy_clause_processing", 1, "end", outcome="passed")
    trace_mod._stage_cache.update({"signature": None, "stages": ()})

    with trace_mod.span("tool.work", category="io", case_id=CASE):
        pass

    assert _spans_for(CASE, RUN)["tool.work"]["stage_name"] == "denial_response"


def test_abandoned_marker_closes_the_attempt_for_attribution(monkeypatch):
    """An abandoned attempt is terminal: a later span must not be attributed
    to a stage a cascade already ended."""
    _scratch_case(monkeypatch)
    dao._emit_stage_attempt_marker(CASE, RUN, "denial_response", 1, "start")
    dao._emit_stage_attempt_marker(
        CASE, RUN, "policy_clause_processing", 1, "start")
    dao._emit_stage_attempt_marker(
        CASE, RUN, "policy_clause_processing", 1, "abandoned",
        outcome="interrupted")
    trace_mod._stage_cache.update({"signature": None, "stages": ()})

    with trace_mod.span("tool.work", category="io", case_id=CASE):
        pass

    assert _spans_for(CASE, RUN)["tool.work"]["stage_name"] == "denial_response"


def test_stage_markers_themselves_carry_no_inferred_ownership(monkeypatch):
    """Reading the marker set to label a marker would be circular."""
    _scratch_case(monkeypatch)
    dao._emit_stage_attempt_marker(CASE, RUN, "denial_response", 1, "start")
    dao._emit_stage_attempt_marker(
        CASE, RUN, "policy_clause_processing", 1, "start")

    for span in _spans_for(CASE, RUN).values():
        if span["op"].startswith("stage.attempt."):
            assert "stage_candidates" not in span


def test_no_markers_means_no_stage_fields(monkeypatch):
    """Pre-existing traces have no marker directory. They must keep working
    exactly as before rather than acquiring an invented stage."""
    _scratch_case(monkeypatch)
    trace_mod.configure(CASE, RUN, root=dao.OUTPUTS)

    with trace_mod.span("tool.work", category="io", case_id=CASE):
        pass

    span = _spans_for(CASE, RUN)["tool.work"]
    assert "stage_name" not in span
    assert "stage_candidates" not in span


def test_summary_with_explicit_ownership_is_schema_valid():
    import _validation

    schemas, registry = _validation.load_registry()
    summary = _summary([
        _start("denial_response", 1, 0),
        _start("policy_clause_processing", 1, 0),
        _owned("t1", "denial_response", 1, 10, 20),
        _owned("t2", "policy_clause_processing", 1, 15, 30),
        _end("policy_clause_processing", 1, 60),
        _end("denial_response", 1, 100),
    ])
    summary.setdefault("case_id", CASE)

    errors = _validation.validate_instance(
        summary, "timing_summary.schema.json", schemas, registry)
    assert errors == [], errors
