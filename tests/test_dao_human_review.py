"""dao.py's P7 human-input tracking (set-human-input-status,
request-expert-review) and D1's evaluation gate (mark-human-review-complete,
read-ground-truth's per-version flag check). Closes the gap found in the
end-to-end pipeline review: neither mechanism had any write path before this
-- evaluation could never be legitimately unblocked for any case.
"""
import json

import pytest

import dao


@pytest.fixture(autouse=True)
def fast_lock_wait(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.05)


VALID_EXPERT_REVIEW = {
    "case_id": "CASE_009", "component": "evaluation", "status": "success",
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
    rc = dao.cmd_set_human_input_status(make_args(stage="evaluation", status="waiting", description=None))
    assert rc == 1


def test_waiting_then_received_round_trip(isolated_dao, make_args):
    rc1 = dao.cmd_set_human_input_status(make_args(stage="evaluation", status="waiting", description="expert review of v1"))
    assert rc1 == 0
    state = dao.load_run_state("CASE_009")
    entry = state["human_input_status"][0]
    assert entry["status"] == "waiting"
    assert entry["description"] == "expert review of v1"
    assert entry["received_at"] is None

    rc2 = dao.cmd_set_human_input_status(make_args(stage="evaluation", status="received"))
    assert rc2 == 0
    state = dao.load_run_state("CASE_009")
    entry = state["human_input_status"][0]
    assert entry["status"] == "received"
    assert entry["received_at"] is not None
    assert entry["description"] == "expert review of v1", "the original description is preserved, not cleared"


def test_received_with_no_waiting_entry_fails(isolated_dao, make_args):
    rc = dao.cmd_set_human_input_status(make_args(stage="evaluation", status="received"))
    assert rc == 1


def test_entries_are_never_deleted_only_appended_or_updated(isolated_dao, make_args):
    """P7: the full history of what was waited on stays visible."""
    dao.cmd_set_human_input_status(make_args(stage="evaluation", status="waiting", description="v1 review"))
    dao.cmd_set_human_input_status(make_args(stage="evaluation", status="received"))
    dao.cmd_set_human_input_status(make_args(stage="evaluation", status="waiting", description="v2 review"))

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
    dao.cmd_set_human_input_status(make_args(stage="evaluation", status="waiting", description="old, forgotten"))
    dao.cmd_set_human_input_status(make_args(stage="evaluation", status="waiting", description="current"))

    dao.cmd_set_human_input_status(make_args(stage="evaluation", status="received"))

    state = dao.load_run_state("CASE_009")
    assert state["human_input_status"][0]["status"] == "waiting", "the older one is untouched"
    assert state["human_input_status"][1]["status"] == "received"
    assert state["human_input_status"][1]["description"] == "current"


def test_request_expert_review_wraps_with_fixed_description(isolated_dao, make_args):
    rc = dao.cmd_request_expert_review(make_args(version="v1"))
    assert rc == 0
    state = dao.load_run_state("CASE_009")
    entry = state["human_input_status"][0]
    assert entry["stage_name"] == "evaluation"
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


def test_read_ground_truth_allowed_after_flag_set(isolated_dao, make_args):
    _write_expert_review(isolated_dao, "v1")
    dao.cmd_mark_human_review_complete(make_args(version="v1", reviewer="Dev"))

    rc = dao.cmd_read_ground_truth(make_args(caller_stage="evaluation", version="v1"))
    assert rc == 0


def test_read_ground_truth_v1_flag_does_not_unlock_v2(isolated_dao, make_args):
    _write_expert_review(isolated_dao, "v1")
    dao.cmd_mark_human_review_complete(make_args(version="v1", reviewer="Dev"))

    rc = dao.cmd_read_ground_truth(make_args(caller_stage="evaluation", version="v2"))
    assert rc == 1


def test_full_handoff_sequence(isolated_dao, make_args):
    """The whole chain in order: critic finishes -> request review ->
    human reviews, expert_review.json gets written -> mark complete ->
    evaluation can read ground truth."""
    assert dao.cmd_request_expert_review(make_args(version="v1")) == 0
    assert dao.cmd_read_ground_truth(make_args(caller_stage="evaluation", version="v1")) == 1, \
        "still blocked -- review not actually done yet"

    _write_expert_review(isolated_dao, "v1")
    assert dao.cmd_mark_human_review_complete(make_args(version="v1", reviewer="Dev")) == 0

    assert dao.cmd_read_ground_truth(make_args(caller_stage="evaluation", version="v1")) == 0
    state = dao.load_run_state("CASE_009")
    assert state["human_input_status"][0]["status"] == "received"
