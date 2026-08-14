"""Dispatch boundaries split a stage's residual into round trip vs operator work.

A stage's marker window minus the agent's own reported duration left a residual
that nothing explained. Measured on CASE_142: 578.3s across nine stages, of
which finalize and snapshot -- the obvious suspects -- accounted for 2.79s. The
other 575.5s was split between dispatch round-trip and operator-side work done
while the marker happened to be open, and nothing on disk could tell them apart.

That distinction decides what a timing figure means. Round trip is structural
and can be engineered against; operator-side work is one person's habits. Until
they separate, a per-stage number cannot honestly be called a stage cost.
"""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import dao
import trace as trace_mod
import trace_aggregate


BASE = datetime(2026, 8, 14, 0, 0, 0, tzinfo=timezone.utc)
RUN = "RUN_20260814_300"
CASE = "CASE_998"


@pytest.fixture(autouse=True)
def _reset_trace_state():
    trace_mod.reset()
    yield
    trace_mod.reset()


class _Args:
    """argparse.Namespace stand-in with the defaults the CLI declares."""

    def __init__(self, **kw):
        self.case_id = CASE
        self.run_id = RUN
        self.stage = "claim_analysis"
        self.started_at = BASE.isoformat()
        self.duration_s = 678.4
        self.agent_reported_s = 645.9
        self.agent_kind = "claim-analysis"
        self.attempt = 1
        self.outcome = "completed"
        for key, value in kw.items():
            setattr(self, key, value)


def _scratch_case(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    outputs = tmp / "outputs"
    (outputs / CASE).mkdir(parents=True)
    monkeypatch.setattr(dao, "OUTPUTS", outputs)
    trace_mod.reset()
    return outputs


def _summarize(run_state_stages=None):
    spans, dropped, _ = trace_aggregate.read_shards(
        dao.trace_spans_dir(CASE, RUN))
    assert dropped == 0
    return trace_aggregate.summarize(
        spans, case_id=CASE, run_id=RUN, generated_at=BASE.isoformat(),
        dropped_span_lines=0, shard_count=1,
        run_state_stages=run_state_stages)


# --- the split itself ------------------------------------------------------

def test_round_trip_is_the_dispatch_minus_the_agents_own_time(monkeypatch):
    """CASE_142's claim_analysis: 678.4s marker window, 645.9s agent."""
    _scratch_case(monkeypatch)
    assert dao.cmd_record_dispatch(_Args()) == 0

    stage = _summarize()["by_stage"]["claim_analysis"]
    assert stage["dispatch_count"] == 1
    assert stage["dispatch_wall_s"] == pytest.approx(678.4)
    assert stage["dispatch_round_trip_s"] == pytest.approx(32.5)


def test_dispatch_never_enters_observed_tool_time(monkeypatch):
    """A dispatch CONTAINS the subagent's work. Folding it into tool time would
    report model thinking as observed tool time and drive
    unattributed_active_s to ~0 -- false precision, which is the exact thing
    T13's naming exists to avoid.

    The dispatch is placed INSIDE a real marker window with a real tool span
    beside it, because that is the only arrangement where the two behaviours
    differ: with no window the attribution arithmetic never runs, and 0.0 comes
    out either way.
    """
    _scratch_case(monkeypatch)
    trace_mod.configure(CASE, RUN, root=dao.OUTPUTS)

    # The marker window must be WIDE for this to test anything: two markers
    # emitted back to back leave a ~0.02s window, and every interval inside it
    # clips to zero regardless of which category it belongs to. So the window
    # is written directly at known wall times rather than via the DAO's
    # now_iso() markers.
    trace_mod.event(
        "stage.attempt.start", category="marker", case_id=CASE, attempt=1,
        marker_kind="stage_attempt_start", stage_name="claim_analysis")
    trace_mod.closed_interval(
        "dao.read_contract", category="io",
        t_start_wall=(BASE + timedelta(seconds=10)).isoformat(),
        duration_s=5.0, case_id=CASE)
    assert dao.cmd_record_dispatch(_Args(
        started_at=(BASE + timedelta(seconds=20)).isoformat(),
        duration_s=600.0, agent_reported_s=590.0)) == 0

    spans, _, _ = trace_aggregate.read_shards(dao.trace_spans_dir(CASE, RUN))
    # Re-anchor the two markers onto BASE .. BASE+700 so the window contains
    # the tool span and the dispatch.
    for span in spans:
        if span["op"] == "stage.attempt.start":
            span["t_start_wall"] = BASE.isoformat()
    spans.append({
        **spans[0], "span_id": "synthetic-end", "op": "stage.attempt.end",
        "t_start_wall": (BASE + timedelta(seconds=700)).isoformat(),
        "attrs": {"marker_kind": "stage_attempt_end",
                  "stage_name": "claim_analysis",
                  "attempt_outcome": "passed"},
    })
    summary = trace_aggregate.summarize(
        spans, case_id=CASE, run_id=RUN, generated_at=BASE.isoformat(),
        dropped_span_lines=0, shard_count=1)
    stage = summary["by_stage"]["claim_analysis"]
    # Only the 5s tool span is observed. Folding the dispatch in would report
    # ~600s here and leave almost nothing unattributed.
    assert stage["observed_tool_overlap_s"] == pytest.approx(5.0, abs=1.0)
    assert stage["unattributed_active_s"] > 100.0
    # The dispatch is still accounted for -- separately.
    assert stage["dispatch_round_trip_s"] == pytest.approx(10.0)
    # And it must not be counted as a stage attempt either.
    assert stage["attempt_count_observed"] == 1


def test_dispatch_absent_reports_null_not_zero(monkeypatch):
    """Zero would claim the round trip was measured and found to be nothing.
    A stage nobody recorded a dispatch for has no measurement at all."""
    outputs = _scratch_case(monkeypatch)
    dao._emit_stage_attempt_marker(CASE, RUN, "consistency_check", 1, "start")
    dao._emit_stage_attempt_marker(
        CASE, RUN, "consistency_check", 1, "end", outcome="passed")

    stage = _summarize()["by_stage"]["consistency_check"]
    assert stage["dispatch_count"] == 0
    assert stage["dispatch_wall_s"] is None
    assert stage["dispatch_round_trip_s"] is None


def test_dispatch_without_agent_duration_leaves_round_trip_null(monkeypatch):
    """The wall is known, the split is not. Reporting 678.4s of round trip
    would attribute the agent's entire run to orchestration overhead."""
    _scratch_case(monkeypatch)
    assert dao.cmd_record_dispatch(_Args(agent_reported_s=None)) == 0

    stage = _summarize()["by_stage"]["claim_analysis"]
    assert stage["dispatch_wall_s"] == pytest.approx(678.4)
    assert stage["dispatch_round_trip_s"] is None


def test_several_dispatches_on_one_stage_accumulate(monkeypatch):
    """A retried stage dispatches more than once; both round trips are real."""
    _scratch_case(monkeypatch)
    assert dao.cmd_record_dispatch(_Args()) == 0
    assert dao.cmd_record_dispatch(_Args(
        started_at=(BASE + timedelta(seconds=700)).isoformat(),
        duration_s=100.0, agent_reported_s=90.0, attempt=2)) == 0

    stage = _summarize()["by_stage"]["claim_analysis"]
    assert stage["dispatch_count"] == 2
    assert stage["dispatch_wall_s"] == pytest.approx(778.4)
    assert stage["dispatch_round_trip_s"] == pytest.approx(42.5)


def test_parallel_stages_keep_their_own_dispatches(monkeypatch):
    """CASE_142 ran screening_report and denial_validation concurrently. A
    dispatch names its stage, so overlap cannot mix them."""
    _scratch_case(monkeypatch)
    assert dao.cmd_record_dispatch(_Args(
        stage="screening_report", duration_s=827.5,
        agent_reported_s=781.6)) == 0
    assert dao.cmd_record_dispatch(_Args(
        stage="denial_validation", duration_s=826.4,
        agent_reported_s=727.6)) == 0

    by_stage = _summarize()["by_stage"]
    assert by_stage["screening_report"]["dispatch_round_trip_s"] == pytest.approx(45.9)
    assert by_stage["denial_validation"]["dispatch_round_trip_s"] == pytest.approx(98.8)


# --- refusals: a wrong number is worse than a missing one ------------------

def test_agent_time_exceeding_the_dispatch_is_refused(monkeypatch):
    """A negative round trip reads as a measurement rather than the mistake it
    is. The agent cannot outlast the dispatch that contains it."""
    _scratch_case(monkeypatch)
    assert dao.cmd_record_dispatch(
        _Args(duration_s=100.0, agent_reported_s=200.0)) == 1
    assert _summarize()["by_stage"] == {}


def test_negative_duration_is_refused(monkeypatch):
    _scratch_case(monkeypatch)
    assert dao.cmd_record_dispatch(_Args(duration_s=-1.0)) == 1


def test_negative_agent_duration_is_refused(monkeypatch):
    _scratch_case(monkeypatch)
    assert dao.cmd_record_dispatch(_Args(agent_reported_s=-1.0)) == 1


def test_unparseable_start_time_is_refused(monkeypatch):
    """A dispatch must land where it happened so an aggregator can place it
    inside the window that contains it."""
    _scratch_case(monkeypatch)
    assert dao.cmd_record_dispatch(_Args(started_at="yesterday")) == 1


def test_failed_dispatch_is_recorded_with_error_status(monkeypatch):
    """A dispatch that failed still consumed wall time and still owes its
    round trip -- dropping it would understate the stage."""
    _scratch_case(monkeypatch)
    assert dao.cmd_record_dispatch(_Args(outcome="failed")) == 0

    spans, _, _ = trace_aggregate.read_shards(dao.trace_spans_dir(CASE, RUN))
    dispatch = [s for s in spans if s.get("category") == "dispatch"]
    assert len(dispatch) == 1
    assert dispatch[0]["status"] == "error"
    assert _summarize()["by_stage"]["claim_analysis"]["dispatch_count"] == 1


# --- content safety -------------------------------------------------------

def test_dispatch_attrs_are_filtered_like_every_other_category(monkeypatch):
    """The attr allow-list is what keeps a prompt or a page of case material
    out of a trace file. A new category must not be an exemption."""
    _scratch_case(monkeypatch)
    trace_mod.configure(CASE, RUN, root=dao.OUTPUTS)
    trace_mod.closed_interval(
        "dispatch.subagent", category="dispatch",
        t_start_wall=BASE.isoformat(), duration_s=1.0, case_id=CASE,
        stage_name="claim_analysis",
        secret_prompt="환자 홍길동의 진단 내용")

    spans, _, _ = trace_aggregate.read_shards(dao.trace_spans_dir(CASE, RUN))
    attrs = spans[0]["attrs"]
    assert "secret_prompt" not in attrs
    assert spans[0]["dropped_attrs"] == 1


def test_summary_with_dispatch_is_schema_valid(monkeypatch):
    import _validation

    _scratch_case(monkeypatch)
    dao._emit_stage_attempt_marker(CASE, RUN, "claim_analysis", 1, "start")
    assert dao.cmd_record_dispatch(_Args()) == 0
    dao._emit_stage_attempt_marker(
        CASE, RUN, "claim_analysis", 1, "end", outcome="passed")

    summary = _summarize(run_state_stages=[
        {"stage_name": "claim_analysis", "status": "passed", "attempt_count": 1}])
    summary.setdefault("case_id", CASE)

    schemas, registry = _validation.load_registry()
    errors = _validation.validate_instance(
        summary, "timing_summary.schema.json", schemas, registry)
    assert errors == [], errors
