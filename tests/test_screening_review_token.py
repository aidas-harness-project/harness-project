"""mark-human-review-complete CASE_ID screening -- the sign-off a screening-only
case can actually give.

The draft flags (v1/v2) require expert_review_v{n}.json before they will be
created. A case that stops at screening_report never produces one:
CASE_705/710/711/712 each finish seven stages ending at screening_report, with
no draft report, no critic pass and no expert review. Gating screening-fidelity
scoring on that flag made a gate no legitimate run could open — the only way
through would have been to fabricate a draft review, which is the CASE_002
self-certification shape at a different gate.

`screening` is a separate token with substance matched to its own artifact:
the report has to exist and its stage has to have passed. The human stays in
the loop exactly as before — running the command is the human act.
"""
import json

import pytest

import dao


CASE = "CASE_009"


class _Args:
    def __init__(self, case_id=CASE, version="screening", reviewer="pyun",
                 held_by="operator", run_id="RUN_20260826_001"):
        self.case_id, self.version, self.reviewer = case_id, version, reviewer
        self.held_by, self.run_id = held_by, run_id


def _finish_screening(root, status="passed"):
    (dao.case_dir(CASE) / "screening_report.json").write_text(
        json.dumps({"case_id": CASE}), encoding="utf-8")
    state = dao.load_run_state(CASE)
    state["stages"] = [{"stage_name": "screening_report", "status": status,
                        "started_at": "2026-08-26T09:00:00+09:00",
                        "updated_at": "2026-08-26T09:10:00+09:00"}]
    dao.atomic_write_json(dao.run_state_path(CASE), state)


def test_a_finished_screening_report_can_be_signed_off(isolated_dao, capsys):
    _finish_screening(isolated_dao)
    assert dao.cmd_mark_human_review_complete(_Args()) == 0
    assert "D1 exception unlocked" in capsys.readouterr().out

    flag = dao.human_review_flag_path(CASE, dao.SCREENING_REVIEW_VERSION)
    assert flag.exists()
    record = json.loads(flag.read_text(encoding="utf-8"))
    assert record["version"] == "screening"
    assert record["reviewer"] == "pyun", "the sign-off names who gave it"


def test_no_report_means_nothing_to_review(isolated_dao, capsys):
    assert dao.cmd_mark_human_review_complete(_Args()) == 1
    out = capsys.readouterr().out
    assert "BLOCKED" in out and "no screening report" in out
    assert not dao.human_review_flag_path(CASE, dao.SCREENING_REVIEW_VERSION).exists()


def test_an_unfinished_stage_cannot_be_signed_off(isolated_dao, capsys):
    """A report whose stage never passed is a partial artifact, not a reviewed
    one -- signing it off would open ground truth against a half-run."""
    _finish_screening(isolated_dao, status="failed")
    assert dao.cmd_mark_human_review_complete(_Args()) == 1
    assert "screening_report as passed" in capsys.readouterr().out


def test_the_screening_token_does_not_grant_a_draft_review(isolated_dao):
    """Signing off the screening report must not create the flag `evaluation`
    reads. Different artifact, different sign-off."""
    _finish_screening(isolated_dao)
    assert dao.cmd_mark_human_review_complete(_Args()) == 0
    for draft_version in ("v1", "v2"):
        assert not dao.human_review_flag_path(CASE, draft_version).exists()


def test_draft_versions_keep_requiring_an_expert_review(isolated_dao, capsys):
    """The screening path is additive: it must not weaken the draft gate."""
    _finish_screening(isolated_dao)
    assert dao.cmd_mark_human_review_complete(_Args(version="v1")) == 1
    assert "expert_review_v1.json does not exist" in capsys.readouterr().out
