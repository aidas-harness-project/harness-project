"""P6's conflict gate, enforced by finalize-stage rather than by prose.

harness-guardrails P6 says a stage proceeds only once every conflict entry for
the case reads `resolved` or `false_positive`, and pipeline.md called
screening_report "gated on check-conflicts-clear". Neither was true of the
code: check-conflicts-clear existed only as a CLI command an agent had to
remember to run, and the finalize path never consulted the ledger at all.
Verified on CASE_909 -- screening_report finalized cleanly with CONFLICT_1
still pending. Nothing went wrong there only because the conflict happened to
be adjudicated first.
"""
import json

import pytest

import dao
import stage_dependencies


@pytest.fixture(autouse=True)
def fast_lock_wait(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0.05)


def _seed_ledger(isolated_dao, verdict="pending"):
    case = isolated_dao / "outputs" / "CASE_009"
    case.mkdir(parents=True, exist_ok=True)
    (case / "_conflict_ledger.json").write_text(json.dumps({
        "case_id": "CASE_009",
        "created_at": "2026-08-05T00:00:00+09:00",
        "updated_at": "2026-08-05T00:00:00+09:00",
        "conflicts": [{
            "conflict_id": "CONFLICT_1",
            "topic": "사고 장소의 물리적 규모",
            "raised_by_stage": "consistency_check",
            "raised_at": "2026-08-05T00:00:00+09:00",
            "verdict": verdict,
            "sources": [
                {"document_id": "DOC_001", "page": 9, "value": "좁음",
                 "quote": "계단은 그리 넓지 않은바"},
                {"document_id": "DOC_002", "page": 4, "value": "큼",
                 "quote": "규모가 상당히 큰 시설로"},
            ],
        }],
    }, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------- the helper ---

def test_pending_ids_reported(isolated_dao):
    _seed_ledger(isolated_dao)
    assert dao.pending_conflict_ids("CASE_009") == ["CONFLICT_1"]


@pytest.mark.parametrize("verdict", ["resolved", "false_positive"])
def test_adjudicated_entries_are_clear(isolated_dao, verdict):
    """Both verdicts close a conflict. A `false_positive` is an adjudication,
    not an unresolved entry -- the ledger keeps the record either way."""
    _seed_ledger(isolated_dao, verdict=verdict)
    assert dao.pending_conflict_ids("CASE_009") == []


def test_no_ledger_is_clear_not_an_error(isolated_dao):
    (isolated_dao / "outputs" / "CASE_009").mkdir(parents=True, exist_ok=True)
    assert dao.pending_conflict_ids("CASE_009") == []


# -------------------------------------------------------- the stage scope ---

def test_every_gated_name_is_a_real_stage():
    """A typo here would silently gate nothing -- the set is matched by name
    against the stage the caller passes, so an unknown name can never match."""
    unknown = dao.CONFLICT_GATED_STAGES - set(stage_dependencies.KNOWN_STAGES)
    assert unknown == set()


def test_consistency_check_is_not_gated():
    """It is the stage that RAISES conflicts. Gating it would stop it
    finalizing the finding it just recorded -- a deadlock, since only a human
    verdict clears the entry and the stage cannot pass to reach one."""
    assert "consistency_check" not in dao.CONFLICT_GATED_STAGES


def test_stages_before_any_comparison_are_not_gated():
    for stage in ("intake", "document_processing", "indexing"):
        assert stage not in dao.CONFLICT_GATED_STAGES


def test_stages_that_reason_from_the_facts_are_gated():
    for stage in ("claim_analysis", "screening_report", "draft_report_v1",
                  "denial_validation"):
        assert stage in dao.CONFLICT_GATED_STAGES


# ---------------------------------------------------- the wiring itself -----

def _finalize(isolated_dao, make_args, stage):
    """Drive the real command, not the helper. Every test above passes if the
    gate is computed and then never consulted -- which is the exact failure
    this change exists to fix."""
    for upstream in ("intake", "document_processing"):
        dao.cmd_update_run_state(
            make_args(stage=upstream, status="in_progress"))
        assert dao.cmd_finalize_stage(make_args(stage=upstream)) == 0, upstream
    dao.cmd_update_run_state(make_args(stage=stage, status="in_progress"))
    return dao.cmd_finalize_stage(make_args(stage=stage))


def test_finalize_refuses_a_gated_stage_while_pending(
        isolated_dao, make_args, capsys):
    _seed_ledger(isolated_dao)
    # indexing is a pass-through with no artifacts of its own, so a refusal
    # here can only come from the conflict gate.
    rc = _finalize(isolated_dao, make_args, "policy_clause_processing")
    assert rc == 1
    out = capsys.readouterr().out
    assert "P6" in out
    assert "CONFLICT_1" in out
    assert "set-conflict-verdict" in out


def test_finalize_proceeds_once_adjudicated(isolated_dao, make_args, capsys):
    _seed_ledger(isolated_dao, verdict="resolved")
    rc = _finalize(isolated_dao, make_args, "indexing")
    assert rc == 0
    assert "P6" not in capsys.readouterr().out
