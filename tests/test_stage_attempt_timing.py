"""T13: cross-process stage-attempt timing without self-time double-counting."""
import json
from datetime import datetime, timedelta, timezone

import pytest

import dao
import trace as trace_mod
import trace_aggregate


BASE = datetime(2026, 8, 11, 0, 0, 0, tzinfo=timezone.utc)
RUN = "RUN_20260811_001"
CASE = "CASE_999"


@pytest.fixture(autouse=True)
def _reset_trace_state():
    trace_mod.reset()
    yield
    trace_mod.reset()


def _record(span_id, op, *, offset, duration=0.0, category="marker",
            attempt=None, attrs=None, status="ok"):
    return {
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
        "status": status,
        "attempt": attempt,
        "shard_id": "1_1_a",
        "pid": 1,
        "thread": 1,
        "attrs": attrs or {},
    }


def _start(stage, attempt, offset, suffix=""):
    return _record(
        f"start-{stage}-{attempt}{suffix}", "stage.attempt.start",
        offset=offset, attempt=attempt,
        attrs={"marker_kind": "stage_attempt_start", "stage_name": stage})


def _end(stage, attempt, offset, outcome="passed", suffix=""):
    return _record(
        f"end-{stage}-{attempt}{suffix}", "stage.attempt.end",
        offset=offset, attempt=attempt,
        status="ok" if outcome == "passed" else "error",
        attrs={"marker_kind": "stage_attempt_end", "stage_name": stage,
               "attempt_outcome": outcome})


def _summary(spans, run_state_stages=None):
    return trace_aggregate.summarize(
        spans, case_id=CASE, run_id=RUN,
        generated_at=BASE.isoformat(), dropped_span_lines=0, shard_count=1,
        run_state_stages=run_state_stages)


def test_closed_attempt_reports_wall_human_tool_union_and_unattributed():
    spans = [
        _start("claim_analysis", 1, 0),
        _record("tool-a", "dao.read_contract", offset=10, duration=30,
                category="io"),
        _record("tool-b", "dao.write_contract", offset=20, duration=30,
                category="io"),
        _record("human", "human.gate", offset=70, duration=10,
                category="human_wait"),
        _end("claim_analysis", 1, 100),
    ]

    summary = _summary(spans)
    attempt = summary["stage_attempts"][0]
    assert attempt["pairing_status"] == "complete"
    assert attempt["raw_wall_s"] == pytest.approx(100.0)
    assert attempt["human_wait_s"] == pytest.approx(10.0)
    assert attempt["active_wall_s"] == pytest.approx(90.0)
    # [10,40] union [20,50] = 40 seconds, not 60.
    assert attempt["observed_tool_overlap_s"] == pytest.approx(40.0)
    assert attempt["unattributed_active_s"] == pytest.approx(50.0)
    assert attempt["attribution_status"] == "complete"


def test_stage_markers_do_not_change_ordinary_self_time_rollups():
    tool = _record("tool", "provider.call", offset=10, duration=5,
                   category="provider")
    baseline = _summary([tool])
    measured = _summary([
        _start("claim_analysis", 1, 0), tool,
        _end("claim_analysis", 1, 20),
    ])

    assert measured["total_self_time_s"] == baseline["total_self_time_s"]
    assert measured["by_category"] == baseline["by_category"]
    assert measured["by_op"] == baseline["by_op"]
    assert measured["critical_path"] == baseline["critical_path"]


def test_overlapping_stage_attempts_fail_closed_for_tool_attribution():
    summary = _summary([
        _start("claim_analysis", 1, 0),
        _start("denial_response", 1, 10),
        _record("tool", "dao.read_contract", offset=20, duration=5,
                category="io"),
        _end("denial_response", 1, 40),
        _end("claim_analysis", 1, 50),
    ])

    assert len(summary["stage_attempts"]) == 2
    for attempt in summary["stage_attempts"]:
        assert attempt["active_wall_s"] is not None
        assert attempt["attribution_status"] == "overlapping_stage_attempts"
        assert attempt["observed_tool_overlap_s"] is None
        assert attempt["unattributed_active_s"] is None


@pytest.mark.parametrize(
    ("spans", "expected"),
    [
        ([_start("claim_analysis", 1, 0)], "open"),
        ([_end("claim_analysis", 1, 10)], "orphan_end"),
        ([_start("claim_analysis", 1, 0),
          _start("claim_analysis", 1, 1, "-dup"),
          _end("claim_analysis", 1, 10)], "duplicate_marker"),
        ([_start("claim_analysis", 1, 10),
          _end("claim_analysis", 1, 0)], "invalid_order"),
    ],
)
def test_non_unique_or_invalid_marker_pairs_never_invent_duration(spans, expected):
    attempt = _summary(spans)["stage_attempts"][0]
    assert attempt["pairing_status"] == expected
    assert attempt["raw_wall_s"] is None
    assert attempt["active_wall_s"] is None
    assert attempt["unattributed_active_s"] is None


def test_run_state_reconciliation_reports_missing_attempt_but_not_skipped_stage():
    summary = _summary([], run_state_stages=[
        {"stage_name": "claim_analysis", "status": "passed", "attempt_count": 1},
        {"stage_name": "indexing", "status": "skipped", "attempt_count": 0},
    ])

    coverage = summary["stage_coverage"]
    assert coverage["run_state_attempts_expected"] == 1
    assert coverage["missing_marker_attempts"] == 1
    assert coverage["coverage_complete"] is False
    assert summary["by_stage"]["indexing"]["skipped"] is True


def test_all_attempt_cost_keeps_failed_retry_instead_of_hiding_it():
    summary = _summary([
        _start("claim_analysis", 1, 0),
        _end("claim_analysis", 1, 10, outcome="failed"),
        _start("claim_analysis", 2, 20),
        _end("claim_analysis", 2, 50, outcome="passed"),
    ])

    stage = summary["by_stage"]["claim_analysis"]
    assert stage["attempt_count_observed"] == 2
    assert stage["failed_attempt_count"] == 1
    assert stage["total_attempt_active_wall_s"] == pytest.approx(40.0)
    assert stage["passed_attempt_active_wall_s"] == pytest.approx(30.0)


def _dao_spans(case_id, run_id):
    spans, dropped, _ = trace_aggregate.read_shards(
        dao.trace_spans_dir(case_id, run_id))
    assert dropped == 0
    return spans


def test_incidental_write_does_not_swallow_the_explicit_begin_that_follows(
        isolated_dao, make_args, run_id):
    """An incidental contract write advances pending->in_progress without
    opening an attempt. The orchestrator's real dispatch still has to be
    measured.

    Before the fix, the idempotency guard keyed on status alone, so the
    incidental `in_progress` looked like an already-open attempt: the explicit
    begin returned early, no start marker was emitted, and the stage ran and
    passed contributing no measured interval at all -- exactly the blind spot
    T13 exists to remove.
    """
    contract = isolated_dao / "_classification.json"
    contract.write_text(json.dumps({
        "case_id": "CASE_009", "component": "document-pipeline",
        "status": "success", "document_id": "DOC_001",
        "predicted_document_type": "insurance_policy", "confidence": 0.95,
        "review_required": False,
        "evidence_references": [
            {"document_id": "DOC_001", "page": 1, "quote": "Disability Table"}],
    }, ensure_ascii=False), encoding="utf-8")
    assert dao.cmd_write_contract(make_args(
        case_id="CASE_009", filename="classification_result_DOC_001.json",
        data_file=str(contract),
        schema_name="classification_result.schema.json",
        held_by="document-pipeline", run_id=run_id,
        stage="document_processing")) == 0

    entry = dao.load_run_state("CASE_009")["stages"][0]
    assert entry["status"] == "in_progress"
    assert entry["attempt_count"] == 0, "incidental write must not open an attempt"
    assert not [s for s in _dao_spans("CASE_009", run_id)
                if s["op"] == trace_aggregate.STAGE_ATTEMPT_START_OP]

    assert dao.cmd_update_run_state(make_args(
        case_id="CASE_009", run_id=run_id, stage="document_processing",
        status="in_progress")) == 0

    entry = dao.load_run_state("CASE_009")["stages"][0]
    assert entry["attempt_count"] == 1
    assert entry["started_at"] is not None
    starts = [s for s in _dao_spans("CASE_009", run_id)
              if s["op"] == trace_aggregate.STAGE_ATTEMPT_START_OP]
    assert len(starts) == 1 and starts[0]["attempt"] == 1

    assert dao.cmd_snapshot_backup(make_args(
        case_id="CASE_009", run_id=run_id,
        stage="document_processing")) == 0
    spans = _dao_spans("CASE_009", run_id)
    summary = _summary(
        spans, run_state_stages=dao.load_run_state("CASE_009")["stages"])
    assert summary["stage_coverage"]["missing_marker_attempts"] == 0
    assert summary["stage_coverage"]["coverage_complete"] is True
    assert summary["stage_attempts"][0]["raw_wall_s"] is not None


def test_dao_begin_is_idempotent_and_retry_needs_a_terminal_close(
        isolated_dao, make_args, run_id):
    args = make_args(
        case_id="CASE_009", run_id=run_id,
        stage="document_processing", status="in_progress")
    assert dao.cmd_update_run_state(args) == 0
    assert dao.cmd_update_run_state(args) == 0

    state = dao.load_run_state("CASE_009")
    assert state["stages"][0]["attempt_count"] == 1
    starts = [s for s in _dao_spans("CASE_009", run_id)
              if s["op"] == trace_aggregate.STAGE_ATTEMPT_START_OP]
    assert len(starts) == 1
    assert starts[0]["attempt"] == 1

    assert dao.cmd_update_run_state(make_args(
        case_id="CASE_009", run_id=run_id, stage="document_processing",
        status="failed", attempt_outcome="partial")) == 0
    assert dao.cmd_update_run_state(args) == 0
    state = dao.load_run_state("CASE_009")
    assert state["stages"][0]["attempt_count"] == 2

    spans = _dao_spans("CASE_009", run_id)
    starts = [s for s in spans if s["op"] == trace_aggregate.STAGE_ATTEMPT_START_OP]
    ends = [s for s in spans if s["op"] == trace_aggregate.STAGE_ATTEMPT_END_OP]
    assert [s["attempt"] for s in starts] == [1, 2]
    assert len(ends) == 1
    assert ends[0]["attempt"] == 1
    assert ends[0]["attrs"]["attempt_outcome"] == "partial"


def test_successful_finalize_closes_the_active_attempt_once(
        isolated_dao, make_args, run_id):
    begin = make_args(
        case_id="CASE_009", run_id=run_id, stage="intake",
        status="in_progress")
    assert dao.cmd_update_run_state(begin) == 0
    assert dao.cmd_snapshot_backup(make_args(
        case_id="CASE_009", run_id=run_id, stage="intake")) == 0

    ends = [s for s in _dao_spans("CASE_009", run_id)
            if s["op"] == trace_aggregate.STAGE_ATTEMPT_END_OP]
    assert len(ends) == 1
    assert ends[0]["attempt"] == 1
    assert ends[0]["attrs"]["stage_name"] == "intake"
    assert ends[0]["attrs"]["attempt_outcome"] == "passed"

    # Re-finalization is historical behavior, but it cannot mint another end
    # for the already-closed attempt.
    assert dao.cmd_snapshot_backup(make_args(
        case_id="CASE_009", run_id=run_id, stage="intake")) == 0
    ends = [s for s in _dao_spans("CASE_009", run_id)
            if s["op"] == trace_aggregate.STAGE_ATTEMPT_END_OP]
    assert len(ends) == 1


def test_interrupted_close_leaves_duration_open(
        isolated_dao, make_args, run_id):
    assert dao.cmd_update_run_state(make_args(
        case_id="CASE_009", run_id=run_id, stage="document_processing",
        status="in_progress")) == 0
    assert dao.cmd_update_run_state(make_args(
        case_id="CASE_009", run_id=run_id, stage="document_processing",
        status="failed", attempt_outcome="interrupted")) == 0

    spans = _dao_spans("CASE_009", run_id)
    assert not [s for s in spans if s["op"] == trace_aggregate.STAGE_ATTEMPT_END_OP]
    abandoned = [s for s in spans
                 if s["op"] == trace_aggregate.STAGE_ATTEMPT_ABANDONED_OP]
    assert len(abandoned) == 1
    summary = _summary(spans, run_state_stages=dao.load_run_state("CASE_009")["stages"])
    attempt = summary["stage_attempts"][0]
    assert attempt["outcome"] == "interrupted"
    assert attempt["pairing_status"] == "open"
    assert attempt["raw_wall_s"] is None
