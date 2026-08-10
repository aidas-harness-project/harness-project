"""T1: the two SLA boundary markers.

The measured window is intake-approval-complete -> draft_report_v1 finalized.
critic_v1 is deliberately OUTSIDE it (user decision 2026-08-10: what the
practice delivers is the report; critic is review, not production) while still
running, because evaluation depends on it.

The markers live in dao.py rather than the frontend on purpose: the backend
launches the agent detached and never waits, so it cannot observe when
draft_report_v1 finalizes, and /run-status polling is exactly the lazily
written timing the plan rejects for ended_at. Putting them at the DAO choke
point also means a CLI-driven or scripted run is measured identically to a
UI-driven one.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import dao  # noqa: E402
import trace as trace_mod  # noqa: E402
import trace_aggregate  # noqa: E402

RUN_ID = "RUN_20260810_001"


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture(autouse=True)
def _case(tmp_path, monkeypatch):
    monkeypatch.delenv(trace_mod.TRACE_ENV, raising=False)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    monkeypatch.setattr(dao, "OUTPUTS", outputs)
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.05)
    trace_mod.reset()
    # Route shards into the same sandbox the DAO writes to.
    real_configure = trace_mod.configure
    monkeypatch.setattr(
        trace_mod, "configure",
        lambda case_id, run_id, root=None: real_configure(case_id, run_id, root=outputs))
    yield outputs
    trace_mod.reset()


def _write_ledger(outputs, statuses):
    case = outputs / "CASE_999"
    case.mkdir(parents=True, exist_ok=True)
    (case / "_source_ledger.json").write_text(json.dumps({
        "case_id": "CASE_999",
        "files": [{"file_name": f"f{i}.pdf", "review_status": s}
                  for i, s in enumerate(statuses)],
    }), encoding="utf-8")
    return case


def _markers(outputs, run_id=RUN_ID):
    d = outputs / "CASE_999" / "_trace" / run_id / "markers"
    return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []


def _spans(outputs, run_id=RUN_ID):
    spans_dir = outputs / "CASE_999" / "_trace" / run_id / "spans"
    spans, _, _ = trace_aggregate.read_shards(spans_dir)
    return spans


# ----------------------------------------------------------- start marker --

def test_clear_ledger_emits_the_start_marker(_case):
    _write_ledger(_case, ["approved", "approved"])
    rc = dao.cmd_check_source_ledger_clear(_Args(case_id="CASE_999", run_id=RUN_ID))
    assert rc == 0
    assert _markers(_case) == ["sla.phase1.start"]
    ops = [s["op"] for s in _spans(_case)]
    assert ops.count("sla.phase1.start") == 1


def test_pending_ledger_emits_nothing(_case):
    """The window must not open while intake review is unfinished -- that is
    the whole definition of the start point."""
    _write_ledger(_case, ["approved", "pending"])
    assert dao.cmd_check_source_ledger_clear(_Args(case_id="CASE_999", run_id=RUN_ID)) == 1
    assert _markers(_case) == []


def test_rejected_ledger_emits_nothing(_case):
    _write_ledger(_case, ["rejected"])
    assert dao.cmd_check_source_ledger_clear(_Args(case_id="CASE_999", run_id=RUN_ID)) == 1
    assert _markers(_case) == []


def test_repeated_clear_checks_emit_the_start_marker_once(_case):
    """check-source-ledger-clear is a query an agent may legitimately call
    many times. A window with two starts has no defined width."""
    _write_ledger(_case, ["approved"])
    for _ in range(5):
        dao.cmd_check_source_ledger_clear(_Args(case_id="CASE_999", run_id=RUN_ID))
    assert _markers(_case) == ["sla.phase1.start"]
    assert [s["op"] for s in _spans(_case)].count("sla.phase1.start") == 1


def test_without_run_id_the_check_behaves_exactly_as_before(_case):
    """--run-id is optional so existing callers of this read-only query keep
    working; they simply are not measured."""
    _write_ledger(_case, ["approved"])
    assert dao.cmd_check_source_ledger_clear(_Args(case_id="CASE_999", run_id=None)) == 0
    assert _markers(_case) == []
    assert not (_case / "CASE_999" / "_trace").exists()


# ------------------------------------------------------------- end marker --

def _finalize(monkeypatch, stage, ok=True):
    """Drive cmd_snapshot_backup with _finalize_stage stubbed: this test is
    about which stage closes the window, not about snapshot mechanics (covered
    by the run-state suite)."""
    state = {"stages": [{"stage_name": stage, "backup_path": f"_backups/step_01_{stage}"}]}
    monkeypatch.setattr(dao, "_finalize_stage",
                        lambda *a, **k: state if ok else None)
    return dao.cmd_snapshot_backup(
        _Args(case_id="CASE_999", run_id=RUN_ID, stage=stage, held_by="dev"))


def test_finalizing_draft_report_v1_emits_the_end_marker(_case, monkeypatch):
    assert _finalize(monkeypatch, "draft_report_v1") == 0
    assert _markers(_case) == ["sla.phase1.end"]
    assert [s["op"] for s in _spans(_case)].count("sla.phase1.end") == 1


def test_finalizing_critic_v1_does_not_close_the_window(_case, monkeypatch):
    """critic_v1 still runs -- evaluation depends on it -- but it is outside
    the measured window by decision, not by accident."""
    assert _finalize(monkeypatch, "critic_v1") == 0
    assert _markers(_case) == []


@pytest.mark.parametrize("stage", ["document_processing", "screening_report",
                                   "claim_analysis", "evaluation"])
def test_no_other_stage_closes_the_window(_case, monkeypatch, stage):
    assert _finalize(monkeypatch, stage) == 0
    assert _markers(_case) == []


def test_a_failed_finalize_never_closes_the_window(_case, monkeypatch):
    """The window must not close on a stage that did not actually pass."""
    assert _finalize(monkeypatch, "draft_report_v1", ok=False) == 1
    assert _markers(_case) == []


def test_refinalizing_draft_report_v1_emits_the_end_marker_once(_case, monkeypatch):
    _finalize(monkeypatch, "draft_report_v1")
    _finalize(monkeypatch, "draft_report_v1")
    assert [s["op"] for s in _spans(_case)].count("sla.phase1.end") == 1


def test_sla_end_stage_is_draft_report_v1_not_critic():
    """Pins the decision itself: if this constant is changed back, the change
    is deliberate and visible rather than silent."""
    assert dao.SLA_END_STAGE == "draft_report_v1"


# --------------------------------------------------------- window closure --

def test_both_markers_produce_a_real_active_s(_case, monkeypatch):
    """The end-to-end point of T1: with both markers present, aggregate-trace
    yields an active_s instead of the n/a it reported before."""
    _write_ledger(_case, ["approved"])
    dao.cmd_check_source_ledger_clear(_Args(case_id="CASE_999", run_id=RUN_ID))
    _finalize(monkeypatch, "draft_report_v1")

    assert dao.cmd_aggregate_trace(_Args(
        case_id="CASE_999", run_id=RUN_ID, held_by="dev",
        input_class="S", cold_or_warm="cold")) == 0
    summary = json.loads(
        (_case / "CASE_999" / "_timing_summary.json").read_text(encoding="utf-8"))
    assert summary["active_s"] is not None
    assert summary["sla_wall_clock_s"] is not None
    assert summary["active_s"] >= 0


def test_markers_are_excluded_from_p10_snapshots(_case, monkeypatch):
    """_trace/ holds the markers too, and it is snapshot-excluded -- a marker
    copied into a backup would look like a second emission on restore."""
    _write_ledger(_case, ["approved"])
    dao.cmd_check_source_ledger_clear(_Args(case_id="CASE_999", run_id=RUN_ID))
    dest = dao._build_snapshot_atomic("CASE_999", "document_processing", {"stages": []})
    assert not (dest / "_trace").exists()


def test_marker_failure_never_breaks_the_operation(_case, monkeypatch):
    """A lost measurement must not fail the pipeline action that triggered it."""
    _write_ledger(_case, ["approved"])
    monkeypatch.setattr(dao, "trace_spans_dir",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert dao.cmd_check_source_ledger_clear(_Args(case_id="CASE_999", run_id=RUN_ID)) == 0


def test_trace_disabled_emits_no_markers(_case, monkeypatch):
    monkeypatch.setenv(trace_mod.TRACE_ENV, "0")
    _write_ledger(_case, ["approved"])
    assert dao.cmd_check_source_ledger_clear(_Args(case_id="CASE_999", run_id=RUN_ID)) == 0
    assert _markers(_case) == []


def test_cli_accepts_the_optional_run_id():
    parser = dao.build_parser()
    args = parser.parse_args(["check-source-ledger-clear", "CASE_999"])
    assert args.run_id is None
    args = parser.parse_args(["check-source-ledger-clear", "CASE_999",
                              "--run-id", RUN_ID])
    assert args.run_id == RUN_ID
