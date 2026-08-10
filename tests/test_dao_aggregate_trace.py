"""Tests for `dao.py aggregate-trace` / `read-timing-summary` and the
interval math in tools/trace_aggregate.py.

The self-time tests carry most of the weight here. A parent that fans out to
parallel children does not satisfy `duration == sum(children)`, so subtracting
a SUM produces a negative parent self-time and blames the wrong op. Under real
parallelism -- the condition this instrumentation exists to measure -- only the
interval UNION is correct.
"""
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import trace_aggregate  # noqa: E402
import dao  # noqa: E402


BASE_WALL = datetime(2026, 8, 10, 3, 0, 0, tzinfo=timezone.utc)


def _span(span_id, op, *, category="compute", parent=None, start=0.0,
          duration=1.0, status="ok", attrs=None, pid=1234, wall_offset=None):
    return {
        "schema_version": "0.1",
        "span_id": span_id,
        "parent_span_id": parent,
        "run_id": "RUN_20260810_001",
        "case_id": "CASE_999",
        "doc_id": None,
        "page": None,
        "op": op,
        "category": category,
        "t_start_wall": (BASE_WALL + timedelta(
            seconds=start if wall_offset is None else wall_offset)).isoformat(),
        "t_start_mono": start,
        "duration_s": duration,
        "status": status,
        "attempt": None,
        "shard_id": f"{pid}_1_a",
        "pid": pid,
        "thread": 1,
        "attrs": attrs or {},
    }


def _write_shards(case_dir: Path, spans, run_id="RUN_20260810_001",
                  extra_lines=()):
    spans_dir = case_dir / "_trace" / run_id / "spans"
    spans_dir.mkdir(parents=True, exist_ok=True)
    by_shard = {}
    for span in spans:
        by_shard.setdefault(span["shard_id"], []).append(span)
    for shard, records in by_shard.items():
        lines = [json.dumps(r, ensure_ascii=False) for r in records]
        (spans_dir / f"{shard}.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
    if extra_lines:
        with open(spans_dir / "extra.jsonl", "a", encoding="utf-8") as fh:
            for line in extra_lines:
                fh.write(line + "\n")
    return spans_dir


# ------------------------------------------------------------- self time --

def test_self_time_subtracts_the_union_not_the_sum_of_parallel_children():
    """Four 8s children running concurrently inside a 10s parent.

    Sum would be 32s -> a negative parent self-time. The union of their
    occupied intervals is 8s, so the parent's own time is 2s."""
    spans = [_span("root", "pool.ocr_pages", start=0.0, duration=10.0)]
    for i in range(4):
        spans.append(_span(f"kid{i}", "ocr.page", parent="root",
                           start=1.0, duration=8.0))
    self_times = trace_aggregate.compute_self_times(spans)
    assert self_times["root"] == pytest.approx(2.0)
    assert all(self_times[f"kid{i}"] == pytest.approx(8.0) for i in range(4))
    # Total self-time exceeds the parent's duration -- that gap IS the
    # parallel speedup, not an error.
    assert sum(self_times.values()) == pytest.approx(34.0)


def test_self_time_of_sequential_children_is_their_gap():
    spans = [
        _span("root", "stage.x", start=0.0, duration=10.0),
        _span("a", "step.a", parent="root", start=0.0, duration=3.0),
        _span("b", "step.b", parent="root", start=4.0, duration=3.0),
    ]
    self_times = trace_aggregate.compute_self_times(spans)
    assert self_times["root"] == pytest.approx(4.0)


def test_partially_overlapping_children_are_counted_once():
    spans = [
        _span("root", "stage.x", start=0.0, duration=10.0),
        _span("a", "step.a", parent="root", start=1.0, duration=4.0),
        _span("b", "step.b", parent="root", start=3.0, duration=4.0),
    ]
    # Union of [1,5] and [3,7] is [1,7] = 6s, so the parent owns 4s.
    assert trace_aggregate.compute_self_times(spans)["root"] == pytest.approx(4.0)


def test_child_running_past_the_parent_is_clipped_to_the_parent_window():
    """Parent occupies [0,5]; the child starts at 3 and outlives it. Only the
    2s of overlap is deducted, leaving the parent 3s of its own -- deducting
    the child's full 100s would drive the parent negative."""
    spans = [
        _span("root", "stage.x", start=0.0, duration=5.0),
        _span("a", "step.a", parent="root", start=3.0, duration=100.0),
    ]
    assert trace_aggregate.compute_self_times(spans)["root"] == pytest.approx(3.0)


def test_self_time_is_never_negative():
    spans = [
        _span("root", "stage.x", start=0.0, duration=1.0),
        _span("a", "step.a", parent="root", start=0.0, duration=50.0),
    ]
    assert trace_aggregate.compute_self_times(spans)["root"] == 0.0


def test_cross_process_child_is_not_subtracted_from_its_parent():
    """t_start_mono is only comparable inside one process. A child from
    another pid is left in the rollups on its own merits but is not aligned
    against a clock it cannot be compared to."""
    spans = [
        _span("root", "stage.x", start=0.0, duration=10.0, pid=1),
        _span("a", "subprocess.dao", parent="root", start=99999.0,
              duration=4.0, pid=2),
    ]
    self_times = trace_aggregate.compute_self_times(spans)
    assert self_times["root"] == pytest.approx(10.0)
    assert self_times["a"] == pytest.approx(4.0)


def test_orphan_span_whose_parent_is_absent_is_treated_as_a_root():
    spans = [_span("a", "ocr.page", parent="missing", start=0.0, duration=2.0)]
    self_times = trace_aggregate.compute_self_times(spans)
    assert self_times["a"] == pytest.approx(2.0)
    path = trace_aggregate.compute_critical_path(spans, self_times)
    assert [p["op"] for p in path] == ["ocr.page"]


# -------------------------------------------------------- critical path --

def test_critical_path_follows_the_most_expensive_chain():
    spans = [
        _span("root", "stage.dp", start=0.0, duration=20.0),
        _span("cheap", "step.cheap", parent="root", start=0.0, duration=1.0),
        _span("heavy", "step.heavy", parent="root", start=2.0, duration=15.0),
        _span("leaf", "provider.transcribe_image", category="provider",
              parent="heavy", start=3.0, duration=14.0),
    ]
    self_times = trace_aggregate.compute_self_times(spans)
    path = trace_aggregate.compute_critical_path(spans, self_times)
    assert [p["op"] for p in path] == [
        "stage.dp", "step.heavy", "provider.transcribe_image"]
    assert path[-1]["self_time_s"] == pytest.approx(14.0)
    assert sum(p["share_of_total"] for p in path) <= 1.0 + 1e-6


def test_critical_path_terminates_on_a_cycle():
    """A corrupted shard could in principle produce a parent cycle. Break it
    rather than following it forever."""
    spans = [
        _span("a", "op.a", parent="b", start=0.0, duration=1.0),
        _span("b", "op.b", parent="a", start=0.0, duration=1.0),
    ]
    self_times = trace_aggregate.compute_self_times(spans)
    path = trace_aggregate.compute_critical_path(spans, self_times)
    assert len(path) <= len(spans) + 1


def test_critical_path_of_no_spans_is_empty():
    assert trace_aggregate.compute_critical_path([], {}) == []


# ------------------------------------------------------------- SLA window --

def test_sla_active_subtracts_human_wait_from_wall_clock():
    spans = [
        _span("s", "sla.phase1.start", category="marker", start=0.0,
              duration=0.0, wall_offset=0),
        _span("h", "human.wait", category="human_wait", start=10.0,
              duration=120.0, wall_offset=10),
        _span("e", "sla.phase1.end", category="marker", start=600.0,
              duration=0.0, wall_offset=600),
    ]
    wall, human, active = trace_aggregate.compute_sla(spans)
    assert wall == pytest.approx(600.0)
    assert human == pytest.approx(120.0)
    assert active == pytest.approx(480.0)


def test_two_overlapping_human_gates_are_not_deducted_twice():
    """Deducting a sum here would make active_s smaller than the work took."""
    spans = [
        _span("s", "sla.phase1.start", category="marker", wall_offset=0, duration=0.0),
        _span("h1", "human.wait", category="human_wait", wall_offset=10, duration=100.0),
        _span("h2", "human.wait", category="human_wait", wall_offset=60, duration=100.0),
        _span("e", "sla.phase1.end", category="marker", wall_offset=600, duration=0.0),
    ]
    _, human, active = trace_aggregate.compute_sla(spans)
    assert human == pytest.approx(150.0)
    assert active == pytest.approx(450.0)


def test_human_wait_is_clipped_to_the_sla_window():
    spans = [
        _span("s", "sla.phase1.start", category="marker", wall_offset=0, duration=0.0),
        _span("h", "human.wait", category="human_wait", wall_offset=500, duration=1000.0),
        _span("e", "sla.phase1.end", category="marker", wall_offset=600, duration=0.0),
    ]
    _, human, active = trace_aggregate.compute_sla(spans)
    assert human == pytest.approx(100.0)
    assert active == pytest.approx(500.0)


def test_missing_end_marker_yields_null_sla_not_a_guess():
    spans = [
        _span("s", "sla.phase1.start", category="marker", wall_offset=0, duration=0.0),
        _span("x", "ocr.page", start=0.0, duration=5.0),
    ]
    wall, _, active = trace_aggregate.compute_sla(spans)
    assert wall is None
    assert active is None


# ---------------------------------------------------------------- shards --

def test_torn_final_line_is_dropped_and_counted(tmp_path):
    spans = [_span("a", "ocr.page", start=0.0, duration=1.0)]
    spans_dir = _write_shards(tmp_path, spans,
                              extra_lines=['{"span_id": "b", "op": "trunc'])
    parsed, dropped, shard_count = trace_aggregate.read_shards(spans_dir)
    assert [p["span_id"] for p in parsed] == ["a"]
    assert dropped == 1
    assert shard_count == 2


def test_non_span_json_line_is_dropped(tmp_path):
    spans_dir = _write_shards(
        tmp_path, [_span("a", "ocr.page")],
        extra_lines=['{"not": "a span"}', '[]', '"just a string"'])
    parsed, dropped, _ = trace_aggregate.read_shards(spans_dir)
    assert len(parsed) == 1
    assert dropped == 3


def test_blank_lines_are_not_counted_as_drops(tmp_path):
    spans_dir = _write_shards(tmp_path, [_span("a", "ocr.page")],
                              extra_lines=["", "   "])
    _, dropped, _ = trace_aggregate.read_shards(spans_dir)
    assert dropped == 0


def test_missing_spans_dir_reads_as_empty(tmp_path):
    parsed, dropped, shards = trace_aggregate.read_shards(tmp_path / "nope")
    assert (parsed, dropped, shards) == ([], 0, 0)


# --------------------------------------------------------------- rollups --

def test_rollups_and_stats_are_computed_from_the_spans():
    spans = [
        _span("root", "stage.dp", start=0.0, duration=20.0),
        _span("pool", "pool.ocr_pages", parent="root", start=0.0, duration=18.0,
              attrs={"worker_count": 4, "observed_max_concurrency": 2}),
        _span("p1", "provider.transcribe_image", category="provider",
              parent="pool", start=1.0, duration=8.0,
              attrs={"attempts": 2, "retry_reason_code": "timeout"}),
        _span("p2", "provider.transcribe_image", category="provider",
              parent="pool", start=1.0, duration=8.0, status="error",
              attrs={"attempts": 1}),
        _span("l1", "lock.acquire", category="lock", parent="root",
              start=19.0, duration=0.5, attrs={"wait_s": 30.0, "poll_count": 1}),
        _span("l2", "lock.hold", category="lock", parent="root",
              start=19.5, duration=0.4, attrs={"held_s": 0.4}),
    ]
    summary = trace_aggregate.summarize(
        spans, case_id="CASE_999", run_id="RUN_20260810_001",
        generated_at="2026-08-10T12:00:00+09:00", dropped_span_lines=0,
        shard_count=1)

    assert summary["by_category"]["provider"]["count"] == 2
    assert summary["by_category"]["provider"]["error_count"] == 1
    assert summary["by_op"]["provider.transcribe_image"]["count"] == 2
    assert summary["worker_config"] == {"ocr_pages": 4}
    # Configured 4, observed 2 -- the divergence is the finding.
    assert summary["observed_max_concurrency"] == {"ocr_pages": 2}
    assert summary["retry_stats"]["provider_calls"] == 2
    assert summary["retry_stats"]["provider_errors"] == 1
    assert summary["retry_stats"]["total_attempts"] == 3
    assert summary["retry_stats"]["by_reason_code"] == {"timeout": 1}
    assert summary["lock_stats"]["acquire_count"] == 1
    assert summary["lock_stats"]["total_wait_s"] == pytest.approx(30.0)
    assert summary["lock_stats"]["total_held_s"] == pytest.approx(0.4)
    assert summary["span_count"] == 6


def test_dropped_attrs_are_carried_into_the_summary():
    spans = [_span("a", "provider.x", category="provider")]
    spans[0]["dropped_attrs"] = 3
    summary = trace_aggregate.summarize(
        spans, case_id="CASE_999", run_id="RUN_20260810_001",
        generated_at="2026-08-10T12:00:00+09:00", dropped_span_lines=0,
        shard_count=1)
    assert summary["dropped_attrs"] == 3


def test_summary_validates_against_its_schema():
    from _validation import load_registry, validate_instance
    spans = [
        _span("s", "sla.phase1.start", category="marker", wall_offset=0, duration=0.0),
        _span("root", "stage.dp", start=0.0, duration=20.0),
        _span("p", "provider.transcribe_image", category="provider",
              parent="root", start=1.0, duration=8.0),
        _span("e", "sla.phase1.end", category="marker", wall_offset=600, duration=0.0),
    ]
    summary = trace_aggregate.summarize(
        spans, case_id="CASE_999", run_id="RUN_20260810_001",
        generated_at="2026-08-10T12:00:00+09:00", dropped_span_lines=2,
        shard_count=3, input_class="S", cold_or_warm="cold")
    schemas, registry = load_registry()
    assert validate_instance(summary, "timing_summary.schema.json",
                             schemas, registry) == []


# ------------------------------------------------------ dao command paths --

@pytest.fixture
def case(tmp_path, monkeypatch):
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    monkeypatch.setattr(dao, "OUTPUTS", outputs)
    # P5's real cadence is a 30s poll up to 15 minutes. The held-lock test
    # below deliberately hits a lock it can never win, so without this the
    # suite would sit in that loop for a quarter of an hour. Same override
    # every other lock-touching test file uses.
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.05)
    return outputs / "CASE_999"


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _agg_args(**kw):
    base = dict(case_id="CASE_999", run_id="RUN_20260810_001", held_by="dev",
                input_class=None, cold_or_warm=None)
    base.update(kw)
    return _Args(**base)


def test_aggregate_trace_writes_a_validated_summary(case, capsys):
    case.mkdir(parents=True)
    _write_shards(case, [
        _span("s", "sla.phase1.start", category="marker", wall_offset=0, duration=0.0),
        _span("root", "stage.dp", start=0.0, duration=20.0),
        _span("e", "sla.phase1.end", category="marker", wall_offset=300, duration=0.0),
    ])
    assert dao.cmd_aggregate_trace(_agg_args(input_class="S", cold_or_warm="cold")) == 0
    written = json.loads((case / "_timing_summary.json").read_text(encoding="utf-8"))
    assert written["case_id"] == "CASE_999"
    assert written["span_count"] == 3
    assert written["active_s"] == pytest.approx(300.0)
    assert written["input_class"] == "S"
    assert written["cold_or_warm"] == "cold"
    assert "active_s=300.0" in capsys.readouterr().out


def test_aggregate_trace_leaves_no_lock_behind(case):
    case.mkdir(parents=True)
    _write_shards(case, [_span("a", "ocr.page")])
    dao.cmd_aggregate_trace(_agg_args())
    assert not (case / "_timing_summary.json.lock").exists()


def test_aggregate_trace_refuses_when_no_trace_exists(case, capsys):
    case.mkdir(parents=True)
    assert dao.cmd_aggregate_trace(_agg_args()) == 1
    assert "NO_TRACE" in capsys.readouterr().out
    assert not (case / "_timing_summary.json").exists()


def test_aggregate_trace_refuses_when_every_line_is_unparseable(case, capsys):
    case.mkdir(parents=True)
    spans_dir = case / "_trace" / "RUN_20260810_001" / "spans"
    spans_dir.mkdir(parents=True)
    (spans_dir / "a.jsonl").write_text("{broken\n", encoding="utf-8")
    assert dao.cmd_aggregate_trace(_agg_args()) == 1
    assert "NO_TRACE" in capsys.readouterr().out


def test_aggregate_trace_reports_absent_markers_rather_than_a_fake_number(case, capsys):
    case.mkdir(parents=True)
    _write_shards(case, [_span("a", "ocr.page", start=0.0, duration=3.0)])
    assert dao.cmd_aggregate_trace(_agg_args()) == 0
    out = capsys.readouterr().out
    assert "active_s=n/a" in out
    written = json.loads((case / "_timing_summary.json").read_text(encoding="utf-8"))
    assert written["active_s"] is None
    assert written["sla_wall_clock_s"] is None


def test_aggregate_trace_respects_a_held_lock(case, capsys):
    case.mkdir(parents=True)
    _write_shards(case, [_span("a", "ocr.page")])
    target = case / "_timing_summary.json"
    assert dao.acquire_lock(target, "someone-else", "RUN_X", "busy") is None
    try:
        assert dao.cmd_aggregate_trace(_agg_args()) == 1
        assert "LOCKED" in capsys.readouterr().out
        assert not target.exists()
    finally:
        dao.release_lock(target)


def test_aggregate_trace_does_not_persist_an_invalid_summary(case, capsys, monkeypatch):
    """Same fail/don't-persist contract as write-contract: a summary that
    fails its schema leaves no file behind."""
    case.mkdir(parents=True)
    _write_shards(case, [_span("a", "ocr.page")])

    real = trace_aggregate.summarize

    def broken(*a, **kw):
        out = real(*a, **kw)
        out["span_count"] = "not an integer"
        return out

    monkeypatch.setattr(trace_aggregate, "summarize", broken)
    assert dao.cmd_aggregate_trace(_agg_args()) == 1
    assert "schema validation errors" in capsys.readouterr().out
    assert not (case / "_timing_summary.json").exists()
    assert not (case / "_timing_summary.json.lock").exists()


def test_read_timing_summary_round_trips(case, capsys):
    case.mkdir(parents=True)
    _write_shards(case, [_span("a", "ocr.page")])
    dao.cmd_aggregate_trace(_agg_args())
    capsys.readouterr()
    assert dao.cmd_read_timing_summary(_Args(case_id="CASE_999")) == 0
    assert json.loads(capsys.readouterr().out)["case_id"] == "CASE_999"


def test_read_timing_summary_reports_absence(case, capsys):
    case.mkdir(parents=True)
    assert dao.cmd_read_timing_summary(_Args(case_id="CASE_999")) == 1
    assert "NOT_FOUND" in capsys.readouterr().out


def test_trace_spans_dir_refuses_a_traversing_run_id(case):
    with pytest.raises(SystemExit):
        dao.trace_spans_dir("CASE_999", "../../etc")


# ------------------------------------------------------------- P10 / CLI --

def test_snapshot_excludes_the_trace_tree(case):
    """_trace/ grows through the run and holds nothing restorable; copying it
    into every cumulative snapshot is the O(stages x tree) cost that makes
    finalize a serial tail."""
    case.mkdir(parents=True)
    _write_shards(case, [_span("a", "ocr.page")])
    (case / "coverage_result.json").write_text("{}", encoding="utf-8")
    dest = dao._build_snapshot_atomic("CASE_999", "document_processing", {"stages": []})
    assert (dest / "coverage_result.json").exists()
    assert not (dest / "_trace").exists()


def test_cli_exposes_both_subcommands():
    parser = dao.build_parser()
    args = parser.parse_args(["aggregate-trace", "CASE_999",
                              "--run-id", "RUN_20260810_001", "--held-by", "dev"])
    assert args.fn is dao.cmd_aggregate_trace
    args = parser.parse_args(["read-timing-summary", "CASE_999"])
    assert args.fn is dao.cmd_read_timing_summary


def test_aggregate_trace_runs_as_a_real_subprocess(tmp_path):
    """Exercise the actual CLI, not just the function: the argparse wiring and
    the module-level import of trace_aggregate are part of what ships."""
    # The subprocess resolves OUTPUTS itself, so this one test cannot be
    # redirected at tmp_path and must use a real (but unused) case id under
    # outputs/. It refuses to run rather than touch an id that exists, and
    # removes what it created either way. 999 is deliberately outside the
    # numbering any real case uses.
    outputs = ROOT / "outputs"
    case_id = "CASE_999"
    target = outputs / case_id
    if target.exists():
        pytest.skip(f"{target} already exists -- refusing to touch real output")
    try:
        _write_shards(target, [
            _span("s", "sla.phase1.start", category="marker", wall_offset=0, duration=0.0),
            _span("root", "stage.dp", start=0.0, duration=4.0),
            _span("e", "sla.phase1.end", category="marker", wall_offset=42, duration=0.0),
        ])
        proc = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "dao.py"), "aggregate-trace",
             case_id, "--run-id", "RUN_20260810_001", "--held-by", "dev"],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "active_s=42.0" in proc.stdout
        written = json.loads((target / "_timing_summary.json").read_text(encoding="utf-8"))
        assert written["span_count"] == 3
    finally:
        import shutil
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
