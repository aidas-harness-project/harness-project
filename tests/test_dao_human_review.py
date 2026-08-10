"""P7 human-input tracking and the deferred Unit 11 handoff boundary.

Human-review completion records a versioned future handoff prerequisite; it
never unlocks ground-truth access in the local Units 1-7 harness.
"""
import json

import pytest

import dao


@pytest.fixture(autouse=True)
def fast_lock_wait(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.05)


VALID_EXPERT_REVIEW = {
    "case_id": "CASE_009", "component": "human-review", "status": "success",
    "reviewer_id": "Dev", "reviewer_role": "손해사정사",
    "reviewed_document": "outputs/CASE_009/draft_report_v1_reviewed.md",
    "overall_approved": True,
    "findings_disposition": [{"finding_ref": "CF-1", "disposition": "accepted"}],
    "reviewed_at": "2026-07-13T10:00:00+09:00",
}


def _write_expert_review(isolated_dao, version="v1", data=None):
    path = isolated_dao / "outputs" / "CASE_009" / f"expert_review_{version}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data or VALID_EXPERT_REVIEW), encoding="utf-8")
    return path


# ------------------------------------------------------- human_input_status --

def test_waiting_requires_description(isolated_dao, make_args):
    rc = dao.cmd_set_human_input_status(make_args(stage="human_review_v1", status="waiting", description=None))
    assert rc == 1


def test_waiting_then_received_round_trip(isolated_dao, make_args):
    rc1 = dao.cmd_set_human_input_status(make_args(stage="human_review_v1", status="waiting", description="expert review of v1"))
    assert rc1 == 0
    state = dao.load_run_state("CASE_009")
    entry = state["human_input_status"][0]
    assert entry["status"] == "waiting"
    assert entry["description"] == "expert review of v1"
    assert entry["received_at"] is None

    rc2 = dao.cmd_set_human_input_status(make_args(stage="human_review_v1", status="received"))
    assert rc2 == 0
    state = dao.load_run_state("CASE_009")
    entry = state["human_input_status"][0]
    assert entry["status"] == "received"
    assert entry["received_at"] is not None
    assert entry["description"] == "expert review of v1", "the original description is preserved, not cleared"


def test_received_with_no_waiting_entry_fails(isolated_dao, make_args):
    rc = dao.cmd_set_human_input_status(make_args(stage="human_review_v1", status="received"))
    assert rc == 1


def test_entries_are_never_deleted_only_appended_or_updated(isolated_dao, make_args):
    """P7: the full history of what was waited on stays visible."""
    dao.cmd_set_human_input_status(make_args(stage="human_review_v1", status="waiting", description="v1 review"))
    dao.cmd_set_human_input_status(make_args(stage="human_review_v1", status="received"))
    dao.cmd_set_human_input_status(make_args(stage="human_review_v2", status="waiting", description="v2 review"))

    state = dao.load_run_state("CASE_009")
    assert len(state["human_input_status"]) == 2
    assert state["human_input_status"][0]["description"] == "v1 review"
    assert state["human_input_status"][0]["status"] == "received"
    assert state["human_input_status"][1]["description"] == "v2 review"
    assert state["human_input_status"][1]["status"] == "waiting"


def test_received_flips_the_most_recent_waiting_entry_for_that_stage(isolated_dao, make_args):
    """If two 'waiting' episodes for the same stage somehow coexist (a
    prior receive was skipped), 'received' resolves the most recent one,
    not an arbitrary one."""
    dao.cmd_set_human_input_status(make_args(stage="human_review_v1", status="waiting", description="old, forgotten"))
    dao.cmd_set_human_input_status(make_args(stage="human_review_v1", status="waiting", description="current"))

    dao.cmd_set_human_input_status(make_args(stage="human_review_v1", status="received"))

    state = dao.load_run_state("CASE_009")
    assert state["human_input_status"][0]["status"] == "waiting", "the older one is untouched"
    assert state["human_input_status"][1]["status"] == "received"
    assert state["human_input_status"][1]["description"] == "current"


def test_request_expert_review_wraps_with_fixed_description(isolated_dao, make_args):
    rc = dao.cmd_request_expert_review(make_args(version="v1"))
    assert rc == 0
    state = dao.load_run_state("CASE_009")
    entry = state["human_input_status"][0]
    assert entry["stage_name"] == "human_review_v1"
    assert entry["status"] == "waiting"
    assert "draft_report_v1_reviewed.md" in entry["description"]


# ---------------------------------------------------------- D1 review gate --

def test_mark_human_review_complete_blocked_without_expert_review_json(isolated_dao, make_args):
    rc = dao.cmd_mark_human_review_complete(make_args(version="v1", reviewer="Dev"))
    assert rc == 1
    assert not dao.human_review_flag_path("CASE_009", "v1").exists()


def test_mark_human_review_complete_blocked_by_invalid_expert_review(isolated_dao, make_args):
    invalid = dict(VALID_EXPERT_REVIEW)
    del invalid["overall_approved"]  # required field missing
    _write_expert_review(isolated_dao, "v1", invalid)

    rc = dao.cmd_mark_human_review_complete(make_args(version="v1", reviewer="Dev"))

    assert rc == 1
    assert not dao.human_review_flag_path("CASE_009", "v1").exists()


def test_mark_human_review_complete_succeeds_with_valid_expert_review(isolated_dao, make_args):
    _write_expert_review(isolated_dao, "v1")

    rc = dao.cmd_mark_human_review_complete(make_args(version="v1", reviewer="Dev"))

    assert rc == 0
    flag_path = dao.human_review_flag_path("CASE_009", "v1")
    assert flag_path.exists()
    flag = json.loads(flag_path.read_text(encoding="utf-8"))
    assert flag["reviewer"] == "Dev"
    assert flag["version"] == "v1"


def test_mark_human_review_complete_also_flips_human_input_status(isolated_dao, make_args):
    dao.cmd_request_expert_review(make_args(version="v1"))
    _write_expert_review(isolated_dao, "v1")

    dao.cmd_mark_human_review_complete(make_args(version="v1", reviewer="Dev"))

    state = dao.load_run_state("CASE_009")
    assert state["human_input_status"][0]["status"] == "received"


def test_mark_human_review_complete_does_not_fail_if_no_waiting_entry_exists(isolated_dao, make_args):
    """The flag (the actual D1 gate) is what matters -- missing wait-tracking
    history shouldn't block the real gate from opening once real evidence
    (expert_review.json) exists."""
    _write_expert_review(isolated_dao, "v1")
    rc = dao.cmd_mark_human_review_complete(make_args(version="v1", reviewer="Dev"))
    assert rc == 0
    assert dao.human_review_flag_path("CASE_009", "v1").exists()


def test_v1_and_v2_flags_are_independent(isolated_dao, make_args):
    """A stale v1 flag must not look valid during v2's later review."""
    _write_expert_review(isolated_dao, "v1")
    dao.cmd_mark_human_review_complete(make_args(version="v1", reviewer="Dev"))

    assert dao.human_review_flag_path("CASE_009", "v1").exists()
    assert not dao.human_review_flag_path("CASE_009", "v2").exists()


def test_read_ground_truth_denied_without_flag(isolated_dao, make_args):
    rc = dao.cmd_read_ground_truth(make_args(caller_stage="evaluation", version="v1"))
    assert rc == 1


def test_read_ground_truth_denied_for_wrong_caller_stage(isolated_dao, make_args):
    _write_expert_review(isolated_dao, "v1")
    dao.cmd_mark_human_review_complete(make_args(version="v1", reviewer="Dev"))

    rc = dao.cmd_read_ground_truth(make_args(caller_stage="critic", version="v1"))
    assert rc == 1


def test_read_ground_truth_remains_denied_after_flag_set(isolated_dao, make_args):
    _write_expert_review(isolated_dao, "v1")
    dao.cmd_mark_human_review_complete(make_args(version="v1", reviewer="Dev"))

    rc = dao.cmd_read_ground_truth(make_args(caller_stage="evaluation", version="v1"))
    assert rc == 1


def test_read_ground_truth_v1_flag_does_not_unlock_v2(isolated_dao, make_args):
    _write_expert_review(isolated_dao, "v1")
    dao.cmd_mark_human_review_complete(make_args(version="v1", reviewer="Dev"))

    rc = dao.cmd_read_ground_truth(make_args(caller_stage="evaluation", version="v2"))
    assert rc == 1


def test_full_handoff_sequence(isolated_dao, make_args):
    """Human review completes the local slice without unlocking ground truth."""
    assert dao.cmd_request_expert_review(make_args(version="v1")) == 0
    assert dao.cmd_read_ground_truth(make_args(caller_stage="evaluation", version="v1")) == 1, \
        "still blocked -- review not actually done yet"

    _write_expert_review(isolated_dao, "v1")
    assert dao.cmd_mark_human_review_complete(make_args(version="v1", reviewer="Dev")) == 0

    assert dao.cmd_read_ground_truth(make_args(caller_stage="evaluation", version="v1")) == 1
    state = dao.load_run_state("CASE_009")
    assert state["human_input_status"][0]["status"] == "received"


# ------------------------------- read-ground-truth content path (D1's read) --
# The 2026-07-22 fleet review added a Read deny-glob over data/ground_truth,
# correctly stopping a NON-evaluation agent from opening the answer key. But it
# gave the evaluation stage no replacement, and read-ground-truth returned only
# a directory path -- so the one stage D1 exempts was authorized-but-unable.
# CASE_907 hit it live. These pin the content path AND that adding it did not
# weaken the gate: authorization is still checked before any byte is read.

def _open_gate(isolated_dao, make_args, version="v1"):
    _write_expert_review(isolated_dao, version)
    dao.cmd_mark_human_review_complete(make_args(version=version, reviewer="Dev"))


def _gt_file(isolated_dao, name="GT_001.txt", text="사정 결론: 지급"):
    d = isolated_dao / "data" / "ground_truth" / "CASE_009"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def test_read_ground_truth_file_returns_content(isolated_dao, make_args, capsys):
    _open_gate(isolated_dao, make_args)
    _gt_file(isolated_dao, text="사정 결론: 지급")

    rc = dao.cmd_read_ground_truth(
        make_args(caller_stage="evaluation", version="v1", file="GT_001", list=False))
    assert rc == 0
    assert "사정 결론: 지급" in capsys.readouterr().out


def test_read_ground_truth_list_enumerates_ids(isolated_dao, make_args, capsys):
    _open_gate(isolated_dao, make_args)
    _gt_file(isolated_dao, "GT_001.txt")
    _gt_file(isolated_dao, "GT_002.txt")

    rc = dao.cmd_read_ground_truth(
        make_args(caller_stage="evaluation", version="v1", file=None, list=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "GT_001" in out and "GT_002" in out


def test_content_read_still_denied_for_non_evaluation_caller(isolated_dao, make_args, capsys):
    """The content path must not become a side door around the caller check."""
    _open_gate(isolated_dao, make_args)
    _gt_file(isolated_dao, text="답안지 내용")

    rc = dao.cmd_read_ground_truth(
        make_args(caller_stage="critic", version="v1", file="GT_001", list=False))
    assert rc == 1
    out = capsys.readouterr().out
    assert "DENIED" in out
    assert "답안지 내용" not in out, "content leaked to an unauthorized caller"


def test_content_read_still_denied_before_review_complete(isolated_dao, make_args, capsys):
    """No flag -> no bytes, even though the file exists and the caller is right."""
    _gt_file(isolated_dao, text="답안지 내용")

    rc = dao.cmd_read_ground_truth(
        make_args(caller_stage="evaluation", version="v1", file="GT_001", list=False))
    assert rc == 1
    out = capsys.readouterr().out
    assert "DENIED" in out
    assert "답안지 내용" not in out


def test_content_read_refuses_path_traversal(isolated_dao, make_args):
    """--file is a bare id; a traversal must not escape the ground-truth dir."""
    _open_gate(isolated_dao, make_args)
    _gt_file(isolated_dao)
    secret = isolated_dao / "data" / "raw" / "CASE_009"
    secret.mkdir(parents=True, exist_ok=True)
    (secret / "DOC_001.txt").write_text("raw source", encoding="utf-8")

    with pytest.raises(SystemExit):
        dao.cmd_read_ground_truth(make_args(
            caller_stage="evaluation", version="v1",
            file="../../raw/CASE_009/DOC_001", list=False))


def test_bare_call_still_works_without_a_ground_truth_dir(isolated_dao, make_args, capsys):
    """The bare form answers 'am I authorized, and where' -- a real answer even
    for a case whose ground truth has not been placed yet. Requiring the dir
    here would conflate 'not authorized' with 'no files', which broke two
    pre-existing tests when this content path was first added."""
    _open_gate(isolated_dao, make_args)

    rc = dao.cmd_read_ground_truth(
        make_args(caller_stage="evaluation", version="v1", file=None, list=False))
    assert rc == 0
    assert "ground_truth" in capsys.readouterr().out


def test_unknown_file_id_reports_known_ids(isolated_dao, make_args, capsys):
    _open_gate(isolated_dao, make_args)
    _gt_file(isolated_dao, "GT_001.txt")

    rc = dao.cmd_read_ground_truth(
        make_args(caller_stage="evaluation", version="v1", file="GT_999", list=False))
    assert rc == 1
    out = capsys.readouterr().out
    assert "NOT_FOUND" in out and "GT_001" in out
