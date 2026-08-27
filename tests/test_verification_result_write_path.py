"""write-verification-result / read-verification-result: keeping a score out of
reach of the stages it scores.

The fidelity result names ground-truth VALUES -- the dates, codes, amounts and
issue labels the comparison turned on. That is not the answer key's prose, which
makes it worse rather than better: it is the answer key's answers, already
extracted.

D1 says the result is terminal. Under `outputs/` that would be a promise and
nothing more -- `read-contract` takes no caller stage, and no deny glob covers
`outputs/`. So the file lives inside `data/ground_truth/`, which every agent is
already denied, and is reached only through these two gated commands.

The failure being closed is concrete: score a report, rerun `screening_report`,
and an agent scanning its case artifacts reads the answer values out of the score
file and writes them into the report. The next score rises because the report
copied the answer.

What is pinned here: the destination is the denied tree and not outputs; every
producing stage is refused; the filename is not a general write hatch into
ground truth; the human-review precondition holds on the write too; and the
content is schema-validated before it lands.
"""
import json

import pytest

import dao


CASE = "CASE_907"
FILENAME = "screening_fidelity_result_v1.json"

GT_REF = {"file": "GT_001.pdf", "locator": "p.3 사정 결과"}


def _dim(dimension_id, weight, score, **extra):
    base = {
        "dimension_id": dimension_id,
        "weight": weight,
        "applicable": True,
        "na_reason": None,
        "score": score,
        "rationale": "정답지 항목과 대조함",
        "screening_quotes": ["- 주요 진단명: 천골의 골절 [E1]"],
    }
    base.update(extra)
    return base


def _result():
    return {
        "case_id": CASE,
        "run_id": "RUN_20260826_001",
        "component": "screening-fidelity",
        "status": "success",
        "created_at": "2026-08-26T10:00:00+09:00",
        "schema_version": "screening_fidelity_result.v0.1",
        "rubric_version": "screening_fidelity.v0.1",
        "routing_config_version": "claim_analysis_routing.v0.1",
        "target_report_path": "outputs/CASE_907/screening_report.md",
        "target_report_sha256": "c" * 64,
        "ground_truth_access": {
            "version": "screening",
            "caller_stage": "screening_fidelity",
            "screening_stage_passed": True,
            "ground_truth_files": ["GT_001.pdf"],
        },
        "dimensions": [
            _dim("F1", 50, 90.0, fact_match_rate=0.9,
                 field_comparisons=[{
                     "field_id": "accident_date",
                     "field_name": "사고일",
                     "field_grade": "B",
                     "field_kind": "fact",
                     "core_field": True,
                     "screening_value": None,
                     "ground_truth_value": "2024-11-13",
                     "match_kind": "missing_in_screening",
                     "screening_absence_kind": "declared",
                     "screening_quote": None,
                     "ground_truth_ref": GT_REF,
                 }]),
            _dim("F2", 30, 70.0, issue_matches=[], screening_only_issues=[]),
            _dim("F3", 20, 85.0, document_comparisons=[]),
            _dim("F4", 0, None, conclusion_agreement="screening_declined"),
        ],
        "fidelity_score": 83.0,
        "core_field_mismatch": False,
        "verdict": "aligned",
        "out_of_universe_items": [],
        "findings": [],
    }


class _WriteArgs:
    def __init__(self, tmp_path, case_id=CASE, filename=FILENAME,
                 caller_stage="screening_fidelity", version="screening", data=None):
        self.case_id, self.filename = case_id, filename
        self.caller_stage, self.version = caller_stage, version
        self.held_by, self.run_id, self.purpose = "screening-fidelity", "RUN_20260826_001", None
        payload = _result() if data is None else data
        path = tmp_path / "payload.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.data_file = str(path)


class _ReadArgs:
    def __init__(self, case_id=CASE, filename=FILENAME,
                 caller_stage="screening_fidelity", version="screening"):
        self.case_id, self.filename = case_id, filename
        self.caller_stage, self.version = caller_stage, version


@pytest.fixture
def reviewed(isolated_dao):
    """A case with a screening report its own run finished.

    That is this path's precondition -- an integrity check, not a reader gate.
    Fidelity scoring measures pipeline performance, so it does not wait on a
    human having read the report (owner's decision, 2026-08-26).
    """
    (dao.case_dir(CASE) / "screening_report.json").write_text(
        json.dumps({"case_id": CASE}), encoding="utf-8")
    state = dao.load_run_state(CASE)
    state["stages"] = [{"stage_name": "screening_report", "status": "passed",
                        "started_at": "2026-08-26T09:00:00+09:00",
                        "updated_at": "2026-08-26T09:10:00+09:00"}]
    dao.atomic_write_json(dao.run_state_path(CASE), state)
    return isolated_dao


def test_result_lands_in_the_denied_tree_not_outputs(reviewed, tmp_path):
    assert dao.cmd_write_verification_result(_WriteArgs(tmp_path)) == 0

    written = reviewed / "data" / "ground_truth" / CASE / "_verification" / FILENAME
    assert written.exists(), "the result must live beside the ground truth it describes"
    assert json.loads(written.read_text(encoding="utf-8"))["case_id"] == CASE

    # The whole point: nothing lands where a producing stage can reach it.
    assert not (reviewed / "outputs" / CASE / FILENAME).exists()


PRODUCING_STAGES = [
    "claim_analysis",
    "denial_response",
    "consistency_check",
    "screening_report",
    "draft_report_v1",
    "critic_v1",
    "evaluation",
    "screening-rubric",
]


@pytest.mark.parametrize("stage", PRODUCING_STAGES)
def test_other_stages_can_neither_write_nor_read(reviewed, tmp_path, capsys, stage):
    assert dao.cmd_write_verification_result(
        _WriteArgs(tmp_path, caller_stage=stage)) == 1
    assert "DENIED" in capsys.readouterr().out

    # And a result that already exists stays out of reach.
    assert dao.cmd_write_verification_result(_WriteArgs(tmp_path)) == 0
    assert dao.cmd_read_verification_result(_ReadArgs(caller_stage=stage)) == 1
    assert "DENIED" in capsys.readouterr().out


def test_evaluation_is_not_admitted_here(reviewed, tmp_path, capsys):
    """read-ground-truth admits `evaluation` because that command predates the
    carve-out. This path is narrower on purpose: only the stage that produces
    the artifact may write or read it."""
    assert "evaluation" in dao.GROUND_TRUTH_ALLOWED_STAGES
    assert "evaluation" not in dao.VERIFICATION_STAGES
    assert dao.cmd_write_verification_result(
        _WriteArgs(tmp_path, caller_stage="evaluation")) == 1
    assert "DENIED" in capsys.readouterr().out


def test_filename_is_not_a_write_hatch_into_ground_truth(reviewed, tmp_path, capsys):
    """Without this, the command would be a way to put arbitrary files inside a
    tree nothing else may write."""
    for filename in ("GT_001.pdf", "notes.json", "screening_fidelity_result.json",
                     "screening_fidelity_result_v1.json.bak"):
        assert dao.cmd_write_verification_result(
            _WriteArgs(tmp_path, filename=filename)) == 1, filename
        assert "DENIED" in capsys.readouterr().out


def test_traversal_out_of_the_verification_dir_is_refused(reviewed, tmp_path, capsys):
    """The filename gate runs first, so a traversal attempt is refused as a bad
    filename rather than reaching the path guard -- either way nothing is
    written outside the verification directory."""
    assert dao.cmd_write_verification_result(
        _WriteArgs(tmp_path, filename="../../../outputs/CASE_907/leak.json")) == 1
    assert "DENIED" in capsys.readouterr().out
    assert not (reviewed / "outputs" / CASE / "leak.json").exists()


def test_write_requires_a_report_its_run_finished(reviewed, tmp_path, capsys):
    """CASE_712 is the live example: document_processing left in_progress, four
    later stages failed, and a screening_report.md in the directory anyway. A
    score over that measures nothing about the pipeline."""
    state = dao.load_run_state(CASE)
    state["stages"] = [{"stage_name": "screening_report", "status": "failed",
                        "started_at": "2026-08-26T09:00:00+09:00",
                        "updated_at": "2026-08-26T09:10:00+09:00"}]
    dao.atomic_write_json(dao.run_state_path(CASE), state)
    assert dao.cmd_write_verification_result(_WriteArgs(tmp_path)) == 1
    out = capsys.readouterr().out
    assert "DENIED" in out and "screening_report as passed" in out


def test_invalid_content_does_not_land(reviewed, tmp_path, capsys):
    broken = _result()
    broken["core_field_mismatch"] = True   # schema pins the verdict to divergent
    assert dao.cmd_write_verification_result(
        _WriteArgs(tmp_path, data=broken)) == 1
    assert "schema validation errors" in capsys.readouterr().out
    assert not (reviewed / "data" / "ground_truth" / CASE / "_verification" / FILENAME).exists()


def test_round_trip_through_the_gated_read(reviewed, tmp_path, capsys):
    assert dao.cmd_write_verification_result(_WriteArgs(tmp_path)) == 0
    capsys.readouterr()  # drop the write's OK line so the read's output stands alone
    assert dao.cmd_read_verification_result(_ReadArgs()) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "aligned"


def test_missing_result_reports_not_found(reviewed, capsys):
    assert dao.cmd_read_verification_result(_ReadArgs()) == 1
    assert "NOT_FOUND" in capsys.readouterr().out
