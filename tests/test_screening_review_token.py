"""The screening-fidelity precondition: a finished report, not a reader.

Two decisions are pinned here, and they pull in opposite directions on purpose.

**No reader gate.** Fidelity scoring measures how the PIPELINE performed. Its
input is a finished screening report, and whether a person has read that report
says nothing about the measurement (owner's decision, 2026-08-26). Gating it on
a human sign-off would have made the score wait on an unrelated act — and the
first attempt did worse than that, reusing the DRAFT review flag, which a case
stopping at screening_report can never produce.

**An integrity condition instead.** The report must exist and its own run must
have finished. CASE_712 is the live reason: document_processing left
in_progress, four later stages failed, and a screening_report.md sat in the
directory anyway. A score over that is a number about nothing.

The draft path keeps its human gate untouched — it scores the deliverable, where
the ordering protects the deliverable from being fixed against the answer key.
"""
import json

import pytest

import dao


CASE = "CASE_009"


class _MarkArgs:
    def __init__(self, case_id=CASE, version="screening", reviewer="pyun",
                 held_by="operator", run_id="RUN_20260826_001"):
        self.case_id, self.version, self.reviewer = case_id, version, reviewer
        self.held_by, self.run_id = held_by, run_id


class _ReadArgs:
    def __init__(self, case_id=CASE, caller_stage="screening_fidelity",
                 version="screening", file=None, transcribe=False, list=True):
        self.case_id, self.caller_stage, self.version = case_id, caller_stage, version
        self.file, self.transcribe, self.list = file, transcribe, list


def _finish_screening(status="passed"):
    (dao.case_dir(CASE) / "screening_report.json").write_text(
        json.dumps({"case_id": CASE}), encoding="utf-8")
    state = dao.load_run_state(CASE)
    state["stages"] = [{"stage_name": "screening_report", "status": status,
                        "started_at": "2026-08-26T09:00:00+09:00",
                        "updated_at": "2026-08-26T09:10:00+09:00"}]
    dao.atomic_write_json(dao.run_state_path(CASE), state)


def _place_ground_truth(root):
    gt = root / "data" / "ground_truth" / CASE
    gt.mkdir(parents=True, exist_ok=True)
    (gt / "GT_001.txt").write_text("answer key stand-in", encoding="utf-8")


def test_a_finished_report_opens_ground_truth_with_no_sign_off(isolated_dao, capsys):
    _finish_screening()
    _place_ground_truth(isolated_dao)

    assert not dao.human_review_flag_path(CASE, "v1").exists()
    assert not dao.human_review_flag_path(CASE, "v2").exists()
    assert dao.cmd_read_ground_truth(_ReadArgs()) == 0
    assert "GT_001" in capsys.readouterr().out


def test_a_report_whose_run_failed_is_refused(isolated_dao, capsys):
    _finish_screening(status="failed")
    _place_ground_truth(isolated_dao)

    assert dao.cmd_read_ground_truth(_ReadArgs()) == 1
    out = capsys.readouterr().out
    assert "DENIED" in out and "screening_report as passed" in out


def test_no_report_at_all_is_refused(isolated_dao, capsys):
    _place_ground_truth(isolated_dao)
    assert dao.cmd_read_ground_truth(_ReadArgs()) == 1
    assert "no scorable screening report" in capsys.readouterr().out


def test_screening_is_not_a_sign_off_token(isolated_dao, capsys):
    """The command used to mint a 'screening' flag. It must not any more: two
    mechanisms claiming the same precondition is how they drift apart."""
    _finish_screening()
    assert dao.cmd_mark_human_review_complete(_MarkArgs()) == 1
    out = capsys.readouterr().out
    assert "not a review token" in out
    assert not dao.human_review_flag_path(CASE, "screening").exists()


def test_draft_versions_keep_requiring_an_expert_review(isolated_dao, capsys):
    """Removing the reader gate on one path must not loosen the other."""
    _finish_screening()
    assert dao.cmd_mark_human_review_complete(_MarkArgs(version="v1")) == 1
    assert "expert_review_v1.json does not exist" in capsys.readouterr().out


def test_evaluation_still_needs_its_draft_flag(isolated_dao, capsys):
    _finish_screening()
    _place_ground_truth(isolated_dao)
    rc = dao.cmd_read_ground_truth(_ReadArgs(caller_stage="evaluation", version="v2"))
    assert rc == 1
    out = capsys.readouterr().out
    assert "DENIED" in out and "human review" in out
