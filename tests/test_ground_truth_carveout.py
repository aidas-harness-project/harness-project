"""The 2026-08-22 D1 carve-out: one verification stage may read ground truth.

The PoC is validated by how closely a produced report agrees with the adjuster's
real report, and a scorer blind to the answer key cannot measure that. So
`screening_fidelity` was added to the stages `read-ground-truth` admits.

A carve-out is only as good as its edges, and these are the edges worth pinning:

  1. Every PRODUCING stage stays denied. The exception is agent-scoped; widening
     it to "the run may read ground truth now" is exactly the leak D1 exists to
     stop, so each producing stage name is asserted individually rather than by
     sampling one.
  2. The verification stage does not skip the OTHER gate. Being on the allowed
     list buys the stage check, not the second condition -- which differs by
     path: the draft path waits on a human sign-off, the fidelity path on the
     scored report's own run having finished.
  3. The allowed set stays small. A test that only checked "screening_fidelity
     passes" would not notice a third stage being appended later.
"""
import pytest

import dao


class _Args:
    def __init__(self, case_id="CASE_907", caller_stage="screening_fidelity",
                 version="screening", file=None, transcribe=False, list=False):
        self.case_id, self.caller_stage, self.version = case_id, caller_stage, version
        self.file, self.transcribe, self.list = file, transcribe, list


@pytest.fixture
def reviewed_case(isolated_dao):
    """Both second gates satisfied at once, since the two paths no longer share
    one: the draft path has its v2 human sign-off, and the fidelity path has a
    screening report whose own run finished."""
    import json

    gt_dir = isolated_dao / "data" / "ground_truth" / "CASE_907"
    gt_dir.mkdir(parents=True, exist_ok=True)
    (gt_dir / "GT_001.txt").write_text("answer key stand-in", encoding="utf-8")

    flag = dao.human_review_flag_path("CASE_907", "v2")
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("{}", encoding="utf-8")

    (dao.case_dir("CASE_907") / "screening_report.json").write_text(
        json.dumps({"case_id": "CASE_907"}), encoding="utf-8")
    state = dao.load_run_state("CASE_907")
    state["stages"] = [{"stage_name": "screening_report", "status": "passed",
                        "started_at": "2026-08-26T09:00:00+09:00",
                        "updated_at": "2026-08-26T09:10:00+09:00"}]
    dao.atomic_write_json(dao.run_state_path("CASE_907"), state)
    return isolated_dao


PRODUCING_STAGES = [
    "intake",
    "document_processing",
    "policy_clause_processing",
    "claim_analysis",
    "denial_response",
    "consistency_check",
    "screening_report",
    "draft_report_v1",
    "draft_report_v2",
    "critic_v1",
    "critic_v2",
    "denial_validation",
    "screening-rubric",
]


@pytest.mark.parametrize("stage", PRODUCING_STAGES)
def test_producing_stages_stay_denied(reviewed_case, capsys, stage):
    rc = dao.cmd_read_ground_truth(_Args(caller_stage=stage, list=True))
    assert rc == 1
    assert "DENIED" in capsys.readouterr().out


def test_verification_stage_is_admitted(reviewed_case):
    assert dao.cmd_read_ground_truth(_Args(list=True)) == 0


def test_verification_stage_still_needs_a_finished_report(reviewed_case, capsys):
    """Being on the allowed list buys the stage gate, not the second condition.
    For this path that second condition is the report's own run finishing --
    not a reader, which fidelity scoring deliberately does not wait on."""
    state = dao.load_run_state("CASE_907")
    state["stages"] = [{"stage_name": "screening_report", "status": "failed",
                        "started_at": "2026-08-26T09:00:00+09:00",
                        "updated_at": "2026-08-26T09:10:00+09:00"}]
    dao.atomic_write_json(dao.run_state_path("CASE_907"), state)

    rc = dao.cmd_read_ground_truth(_Args(list=True))
    assert rc == 1
    out = capsys.readouterr().out
    assert "DENIED" in out
    assert "screening_report as passed" in out


def test_the_exempt_agent_exists_and_matches_the_gates():
    """The gates admit a stage name; this checks an agent actually claims it.

    Seven files named screening-fidelity before the agent was written. A stage
    name the gates admit and no agent owns is the state where a human passes
    --caller-stage by hand and no written procedure says what may be done with
    what comes back.
    """
    from pathlib import Path

    agent = (
        Path(dao.__file__).resolve().parent.parent
        / ".claude" / "agents" / "screening-fidelity.md"
    ).read_text(encoding="utf-8")

    assert "--caller-stage screening_fidelity" in agent
    assert "write-verification-result" in agent

    # The write must not go through the generic contract path: that lands in
    # outputs/, where every producing stage can read the extracted answers.
    assert "write-contract CASE_ID screening_fidelity" not in agent

    # Condition 4 has no enforcement, so it has to be written down.
    assert "Do not put ground-truth prose in the result, in your reply" in agent


def test_review_token_must_match_what_the_caller_scores(reviewed_case, capsys):
    """The bug this closes: screening_fidelity was gated on the DRAFT review flag.

    That flag requires expert_review_v{n}.json, which a case stopping at
    screening_report can never produce -- CASE_705/710/711/712 each finish seven
    stages with no draft. The gate was unopenable by any legitimate run; the only
    way through would have been to fabricate a draft review.

    Both directions are pinned. A verification caller asking for v1/v2 is asking
    for that unopenable gate, and an evaluation caller asking for 'screening' is
    trying to open the answer key on a sign-off that says nothing about the
    artifact it scores.
    """
    rc = dao.cmd_read_ground_truth(_Args(version="v2", list=True))
    assert rc == 1
    out = capsys.readouterr().out
    assert "DENIED" in out and "screening" in out

    rc = dao.cmd_read_ground_truth(
        _Args(caller_stage="evaluation", version=dao.SCREENING_REVIEW_VERSION, list=True))
    assert rc == 1
    assert "DENIED" in capsys.readouterr().out


def test_evaluation_keeps_its_draft_review_gate(reviewed_case):
    """The fix must not loosen the path it was not about."""
    assert dao.cmd_read_ground_truth(
        _Args(caller_stage="evaluation", version="v2", list=True)) == 0


def test_allowed_set_is_exactly_two_stages():
    """Widening this set is a governance change (harness-guardrails-dev D1), so
    it should fail a test rather than pass review as a config tweak."""
    assert dao.GROUND_TRUTH_ALLOWED_STAGES == frozenset(
        {"evaluation", "screening_fidelity"}
    )
