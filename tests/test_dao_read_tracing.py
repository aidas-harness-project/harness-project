"""T13 follow-up: DAO read paths are instrumented, and stay optional.

The CASE_910 claim_analysis attempt measured 394.96s active wall with 0.034s
explained by tool spans, while the agent made 41 DAO calls -- almost all reads.
dao.py instrumented only `lock.acquire` and `validate.schema`, both on write
paths, so a read-heavy analysis stage looked free while occupying the whole
interval. These tests pin that reads now record, and that recording never
becomes a precondition for reading.
"""
import json

import pytest

import dao
import trace as trace_mod
import trace_aggregate


RUN = "RUN_20260811_777"


@pytest.fixture(autouse=True)
def _reset_trace_state():
    trace_mod.reset()
    yield
    trace_mod.reset()


def _read_spans(case_id, run_id):
    spans, dropped, _ = trace_aggregate.read_shards(
        dao.trace_spans_dir(case_id, run_id))
    assert dropped == 0
    return [s for s in spans if s["category"] == "io"]


def _seed_contract(isolated_dao, case_id="CASE_009"):
    case = dao.case_dir(case_id)
    case.mkdir(parents=True, exist_ok=True)
    (case / "case_type_result.json").write_text(
        json.dumps({"case_id": case_id, "component": "claim-analysis",
                    "status": "success"}, ensure_ascii=False),
        encoding="utf-8")


def test_read_contract_records_a_span_when_a_run_id_is_supplied(
        isolated_dao, make_args):
    _seed_contract(isolated_dao)
    trace_mod.configure("CASE_009", RUN, root=dao.OUTPUTS)

    assert dao.cmd_read_contract(make_args(
        case_id="CASE_009", filename="case_type_result.json",
        run_id=RUN)) == 0

    spans = _read_spans("CASE_009", RUN)
    ops = [s["op"] for s in spans]
    assert "dao.read_contract" in ops
    span = next(s for s in spans if s["op"] == "dao.read_contract")
    assert span["duration_s"] >= 0.0
    assert span["attrs"]["exit_code"] == 0
    # startup_s is a lower bound on per-process cost, never negative.
    assert span["attrs"]["startup_s"] >= 0.0


def test_a_read_without_a_run_id_still_works_and_records_nothing(
        isolated_dao, make_args):
    """Tracing is diagnostic. A read must not acquire a new failure mode."""
    _seed_contract(isolated_dao)
    # No trace_mod.configure() at all -- this is the ordinary ad-hoc read.
    assert not trace_mod.enabled()

    assert dao.cmd_read_contract(make_args(
        case_id="CASE_009", filename="case_type_result.json",
        run_id=None)) == 0

    assert not dao.trace_spans_dir("CASE_009", RUN).exists()


def test_read_span_preserves_the_commands_exit_code(isolated_dao, make_args):
    """A missing contract still returns 1, and the span records that."""
    dao.case_dir("CASE_009").mkdir(parents=True, exist_ok=True)
    trace_mod.configure("CASE_009", RUN, root=dao.OUTPUTS)

    assert dao.cmd_read_contract(make_args(
        case_id="CASE_009", filename="does_not_exist.json",
        run_id=RUN)) == 1

    span = next(s for s in _read_spans("CASE_009", RUN)
                if s["op"] == "dao.read_contract")
    assert span["attrs"]["exit_code"] == 1


def test_read_spans_are_ordinary_work_and_roll_up_into_by_op(
        isolated_dao, make_args):
    """They must land in the normal rollups, unlike stage-attempt markers.

    That is the entire point: this work was already happening and already
    inside a stage's wall clock -- it was simply invisible, so it showed up as
    unattributed. Once recorded it must be subtractable as observed tool
    overlap.
    """
    _seed_contract(isolated_dao)
    trace_mod.configure("CASE_009", RUN, root=dao.OUTPUTS)
    for _ in range(3):
        assert dao.cmd_read_contract(make_args(
            case_id="CASE_009", filename="case_type_result.json",
            run_id=RUN)) == 0

    spans, _, _ = trace_aggregate.read_shards(dao.trace_spans_dir("CASE_009", RUN))
    summary = trace_aggregate.summarize(
        spans, case_id="CASE_009", run_id=RUN,
        generated_at="2026-08-11T00:00:00+00:00",
        dropped_span_lines=0, shard_count=1)

    assert summary["by_op"]["dao.read_contract"]["count"] == 3
    assert summary["by_category"]["io"]["self_time_s"] > 0.0
    assert summary["total_self_time_s"] > 0.0


def test_trace_disabled_makes_read_instrumentation_a_full_no_op(
        isolated_dao, make_args, monkeypatch):
    _seed_contract(isolated_dao)
    monkeypatch.setenv("HARNESS_TRACE", "0")
    trace_mod.reset()

    assert dao.cmd_read_contract(make_args(
        case_id="CASE_009", filename="case_type_result.json",
        run_id=RUN)) == 0
    assert not dao.trace_spans_dir("CASE_009", RUN).exists()
