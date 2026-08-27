"""Serving ground truth from a handoff bundle instead of data/ground_truth/.

A corpus split (`_workspace/corpus-split`) intakes the raw half of each source
PDF and quarantines the answer-key half. The source ledger then approves one
file classified 'raw', and `data/ground_truth/CASE_ID/` stays legitimately
empty -- so `read-ground-truth --list` exits 0 with no rows: authorized, and
nothing to read. On the 2026-08-27 eval_20260827 bundle that was 39 scorable
cases with no reachable answer key.

The rejected alternative was re-intaking those cases, which would rewrite an
approved D2 ledger to add a `ground_truth` classification no human reviewer
approved -- forging a review record to satisfy a path check. `--bundle` reads
the answer key where it already sits instead.

The edges worth pinning are the ones that make `--bundle` a SOURCE selector and
not a hole:

  1. It does not buy the stage gate. A producing stage passing --bundle is
     still denied, or the flag would be a way around D1 rather than a way to
     the same authorized read.
  2. It does not buy the integrity gate. A report whose own run failed is still
     unscorable.
  3. It serves ONE case's files. A bundle root holds every case's answer key
     side by side, so a case-blind directory listing would hand CASE_A the key
     to CASE_B.
  4. It cannot escape _handoff/. The bundle is a name, not a path.
  5. The bundle's per-case OUTPUT folder is not ground truth and is never
     served as though it were.
"""
import json

import pytest

import dao


class _Args:
    def __init__(self, case_id="CASE_8002", caller_stage="screening_fidelity",
                 version="screening", file=None, transcribe=False, list=False,
                 bundle=None):
        self.case_id, self.caller_stage, self.version = case_id, caller_stage, version
        self.file, self.transcribe, self.list = file, transcribe, list
        self.bundle = bundle


def _finished_screening_case(case_id):
    (dao.case_dir(case_id) / "screening_report.json").write_text(
        json.dumps({"case_id": case_id}), encoding="utf-8")
    state = dao.load_run_state(case_id)
    state["stages"] = [{"stage_name": "screening_report", "status": "passed",
                        "started_at": "2026-08-27T09:00:00+09:00",
                        "updated_at": "2026-08-27T09:10:00+09:00"}]
    dao.atomic_write_json(dao.run_state_path(case_id), state)


@pytest.fixture
def bundled_case(isolated_dao, monkeypatch):
    """Two cases in one bundle, each with its own answer key, and a per-case
    output folder beside them -- the real eval_20260827 shape, which is what
    makes the cross-case and output-folder assertions meaningful."""
    bundle_root = isolated_dao / "_handoff"
    bundle = bundle_root / "eval_20260827"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "CASE_8002_GT.pdf").write_bytes(b"%PDF-1.4 case 8002 answer key")
    (bundle / "CASE_8003_GT.pdf").write_bytes(b"%PDF-1.4 case 8003 answer key")
    # The bundle also carries pipeline outputs per case. Not ground truth.
    outputs = bundle / "CASE_8002"
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "screening_report.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(dao, "GROUND_TRUTH_BUNDLE_ROOT", bundle_root)

    _finished_screening_case("CASE_8002")
    _finished_screening_case("CASE_8003")
    return isolated_dao


def test_bundle_serves_the_case_ground_truth(bundled_case, capsys):
    rc = dao.cmd_read_ground_truth(
        _Args(list=True, bundle="eval_20260827"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "CASE_8002_GT" in out


def test_bundle_serves_only_the_requested_case(bundled_case, capsys):
    """The failure this pins: a bundle root lists every case's answer key, so
    serving the directory would hand this case the other cases' keys."""
    dao.cmd_read_ground_truth(_Args(list=True, bundle="eval_20260827"))
    out = capsys.readouterr().out
    assert "CASE_8002_GT" in out
    assert "CASE_8003" not in out


def test_bundle_does_not_serve_the_output_folder(bundled_case, capsys):
    """The bundle's CASE_XXXX/ folder holds pipeline artifacts. Serving them
    through the ground-truth door would relabel ordinary outputs as the answer
    key, and (worse) route them past the deny globs as privileged material."""
    dao.cmd_read_ground_truth(_Args(list=True, bundle="eval_20260827"))
    out = capsys.readouterr().out
    assert "screening_report" not in out


def test_bundle_does_not_buy_the_stage_gate(bundled_case, capsys):
    rc = dao.cmd_read_ground_truth(
        _Args(caller_stage="claim_analysis", list=True, bundle="eval_20260827"))
    assert rc == 1
    assert "DENIED" in capsys.readouterr().out


def test_bundle_does_not_buy_the_integrity_gate(bundled_case, capsys):
    """A --bundle read of a report whose own run failed is still a number about
    nothing, and is refused for the same reason the tree path refuses it."""
    state = dao.load_run_state("CASE_8002")
    state["stages"] = [{"stage_name": "screening_report", "status": "failed",
                        "started_at": "2026-08-27T09:00:00+09:00",
                        "updated_at": "2026-08-27T09:10:00+09:00"}]
    dao.atomic_write_json(dao.run_state_path("CASE_8002"), state)

    rc = dao.cmd_read_ground_truth(_Args(list=True, bundle="eval_20260827"))
    assert rc == 1
    out = capsys.readouterr().out
    assert "DENIED" in out
    assert "screening_report as passed" in out


def test_bundle_name_cannot_escape_the_handoff_root(bundled_case):
    """The bundle is a NAME under _handoff/, not a path. Otherwise the flag
    would turn one authorized read into an arbitrary-directory reader."""
    escape = "..{}data{}ground_truth".format(dao.os.sep, dao.os.sep)
    _dir, err = dao._resolve_bundle_gt(escape, "CASE_8002")
    assert err is not None
    assert "DENIED" in err


def test_absent_bundle_is_reported_not_guessed(bundled_case, capsys):
    rc = dao.cmd_read_ground_truth(_Args(list=True, bundle="no_such_bundle"))
    assert rc == 1
    assert "NOT_FOUND" in capsys.readouterr().out


def test_case_missing_from_bundle_is_reported(bundled_case, capsys):
    """Distinct from an absent bundle: the bundle is real, this case is not in
    it (excluded / pii_hold rows in the index). That must not read as an empty
    answer key, which would score as total disagreement.

    The case needs a finished screening report first, or the integrity gate
    fires and this asserts nothing about the bundle lookup.
    """
    _finished_screening_case("CASE_8042")

    rc = dao.cmd_read_ground_truth(
        _Args(case_id="CASE_8042", list=True, bundle="eval_20260827"))
    assert rc == 1
    out = capsys.readouterr().out
    assert "NOT_FOUND" in out
    assert "CASE_8042_GT" in out


def test_tree_path_is_unchanged_when_no_bundle_given(isolated_dao):
    """The default source stays data/ground_truth/. --bundle is opt-in, so an
    existing caller keeps reading the intaked answer key it always read."""
    gt_dir = isolated_dao / "data" / "ground_truth" / "CASE_8002"
    gt_dir.mkdir(parents=True, exist_ok=True)
    (gt_dir / "GT_001.txt").write_text("intaked answer key", encoding="utf-8")
    _finished_screening_case("CASE_8002")

    assert dao.cmd_read_ground_truth(_Args(list=True)) == 0
