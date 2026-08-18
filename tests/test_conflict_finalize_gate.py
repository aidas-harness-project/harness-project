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
    entry = {
        "conflict_id": "CONFLICT_1",
        "field_or_topic": "사고 장소의 물리적 규모",
        "raised_by_stage": "consistency_check",
        "raised_at": "2026-08-05T00:00:00+09:00",
        "verdict": verdict,
        "sources": [
            {"document_id": "DOC_001", "page": 9, "value": "좁음",
             "quote": "계단은 그리 넓지 않은바"},
            {"document_id": "DOC_002", "page": 4, "value": "큼",
             "quote": "규모가 상당히 큰 시설로"},
        ],
    }
    # An adjudicated entry must carry its disposition -- the schema requires
    # both the moment `verdict` leaves `pending` (2026-07-22 hardening).
    if verdict != "pending":
        entry["resolution_note"] = (
            "두 진술은 서로 다른 단위를 가리킨다 -- DOC_001은 계단, "
            "DOC_002는 시설 전체.")
        entry["resolved_at"] = "2026-08-05T01:00:00+09:00"
    (case / "_conflict_ledger.json").write_text(json.dumps({
        "case_id": "CASE_009",
        "created_at": "2026-08-05T00:00:00+09:00",
        "updated_at": "2026-08-05T00:00:00+09:00",
        "conflicts": [entry],
    }, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------- the helper ---

def test_pending_ids_reported(isolated_dao):
    _seed_ledger(isolated_dao)
    assert dao.pending_conflict_ids("CASE_009") == ["CONFLICT_1"]


@pytest.mark.parametrize(
    "verdict", ["resolved", "false_positive", "deferred_to_report"])
def test_adjudicated_entries_are_clear(isolated_dao, verdict):
    """All three verdicts unblock a stage. A `false_positive` is an
    adjudication, not an unresolved entry -- the ledger keeps the record either
    way. A `deferred_to_report` is not an adjudication at all, but it is still a
    human's disposition, so it too stops blocking."""
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


# ------------------------------------------- deferred_to_report (2026-08-18) ---
# A deferral says the disagreement is real and belongs to the screening
# report's human reader. Its whole point is that it unblocks the pipeline
# WITHOUT pretending anything was decided -- so the value is only honest if
# something checks the report actually carried it. These tests hold that pair
# together: unblocking, and the obligation the unblocking creates.

def _write_screening_report(isolated_dao, inconsistencies):
    """Place a screening_report.json with the given inconsistency entries.

    Only the fields the carry-check reads. It walks `inconsistencies` for
    `conflict_ref` and looks at nothing else, so a fuller report would add
    noise without adding coverage.
    """
    case = isolated_dao / "outputs" / "CASE_009"
    case.mkdir(parents=True, exist_ok=True)
    (case / "screening_report.json").write_text(json.dumps({
        "case_id": "CASE_009",
        "component": "screening-report",
        "inconsistencies": inconsistencies,
    }, ensure_ascii=False), encoding="utf-8")


def test_deferred_conflicts_are_listed_separately(isolated_dao):
    """Not pending, but not closed either -- the two questions have two
    answers, and collapsing them is what made `resolved` the only usable
    verdict in the first place."""
    _seed_ledger(isolated_dao, verdict="deferred_to_report")
    assert dao.pending_conflict_ids("CASE_009") == []
    assert dao.deferred_conflict_ids("CASE_009") == ["CONFLICT_1"]


def test_adjudicated_conflicts_are_not_deferred(isolated_dao):
    for verdict in ("resolved", "false_positive", "pending"):
        _seed_ledger(isolated_dao, verdict=verdict)
        assert dao.deferred_conflict_ids("CASE_009") == [], verdict


def test_uncarried_deferral_is_reported_by_id(isolated_dao):
    """The core of the feature. Without this check `deferred_to_report` is just
    `resolved` spelled differently."""
    _seed_ledger(isolated_dao, verdict="deferred_to_report")
    _write_screening_report(isolated_dao, [
        {"field": "삽입물 식별", "description": "설명만 있고 원장 연결이 없다",
         "severity": "high"},
    ])
    assert dao._uncarried_deferred_conflicts("CASE_009") == ["CONFLICT_1"]


def test_carried_deferral_discharges_the_obligation(isolated_dao):
    _seed_ledger(isolated_dao, verdict="deferred_to_report")
    _write_screening_report(isolated_dao, [
        {"field": "사고 장소의 물리적 규모", "description": "DOC_001과 DOC_002가 어긋난다",
         "severity": "high", "conflict_ref": "CONFLICT_1",
         "related_documents": ["DOC_001", "DOC_002"]},
    ])
    assert dao._uncarried_deferred_conflicts("CASE_009") == []


def test_carrying_a_different_conflict_does_not_satisfy_the_obligation(
        isolated_dao):
    """Guards the id comparison itself. A report that carries SOME conflict
    passes a truthiness check but not this one -- and a wrong reference is the
    realistic failure, since a report typically carries several."""
    _seed_ledger(isolated_dao, verdict="deferred_to_report")
    _write_screening_report(isolated_dao, [
        {"field": "무관한 항목", "description": "다른 충돌을 참조한다",
         "severity": "medium", "conflict_ref": "CONFLICT_7"},
    ])
    assert dao._uncarried_deferred_conflicts("CASE_009") == ["CONFLICT_1"]


def test_missing_report_does_not_satisfy_a_deferral(isolated_dao):
    """An absent contract is not a vacuous pass. If it were, the cheapest way
    past the gate would be to write no report at all."""
    _seed_ledger(isolated_dao, verdict="deferred_to_report")
    assert dao._uncarried_deferred_conflicts("CASE_009") == ["CONFLICT_1"]


def test_report_without_inconsistencies_array_does_not_satisfy_a_deferral(
        isolated_dao):
    """`inconsistencies` is optional in the schema, so its absence must read as
    'carried nothing', not as 'nothing to check'."""
    _seed_ledger(isolated_dao, verdict="deferred_to_report")
    case = isolated_dao / "outputs" / "CASE_009"
    case.mkdir(parents=True, exist_ok=True)
    (case / "screening_report.json").write_text(
        json.dumps({"case_id": "CASE_009", "component": "screening-report"}),
        encoding="utf-8")
    assert dao._uncarried_deferred_conflicts("CASE_009") == ["CONFLICT_1"]


@pytest.mark.parametrize("verdict", ["resolved", "false_positive", "pending"])
def test_no_deferral_means_the_report_is_never_consulted(isolated_dao, verdict):
    """A case with nothing deferred must be clear of this check whether or not
    it has a report at all -- the carry-check may not become a back-door
    requirement on every screening report. No report is written here, so a
    non-empty result could only come from consulting one that does not exist."""
    _seed_ledger(isolated_dao, verdict=verdict)
    assert dao._uncarried_deferred_conflicts("CASE_009") == []


def test_deferral_does_not_block_stages_before_the_report(
        isolated_dao, make_args, capsys):
    """The deferral's obligation lands on screening_report alone. Gating
    earlier stages on it would deadlock: the report that discharges the
    obligation cannot be written until those stages finalize.

    `indexing` is the one gated stage with no artifact gate of its own, so a
    refusal here could only be P6's -- the same reason the pending-path test
    above uses it."""
    _seed_ledger(isolated_dao, verdict="deferred_to_report")
    assert _finalize(isolated_dao, make_args, "indexing") == 0
    assert "P6" not in capsys.readouterr().out


def _reach_screening_report(isolated_dao, make_args):
    """Drive the real prerequisite chain up to (not including) screening_report.

    screening_report sits behind claim_analysis and consistency_check, and
    policy_clause_processing's own completeness gate sits behind those. An
    empty manifest plus a D5 declaration is the cheapest HONEST way through --
    it satisfies that gate by truthfully declaring a case with no 약관, rather
    than by stubbing the gate out. The manifest is empty rather than carrying a
    medical document because a listed document then owes a classification
    result to document_processing's own gate, which has nothing to do with this
    feature.
    """
    case = isolated_dao / "outputs" / "CASE_009"
    case.mkdir(parents=True, exist_ok=True)
    (case / "document_manifest.json").write_text(json.dumps({
        "case_id": "CASE_009", "documents": [],
    }, ensure_ascii=False), encoding="utf-8")
    ok, message = dao.declare_no_policy_documents(
        "CASE_009", reviewer="pyun", note="fixture case ships without 약관",
        held_by="test-agent", run_id="RUN_20260712_001")
    assert ok, message
    for stage in ("intake", "document_processing", "policy_clause_processing",
                  "claim_analysis", "consistency_check"):
        dao.cmd_update_run_state(make_args(stage=stage, status="in_progress"))
        assert dao.cmd_finalize_stage(make_args(stage=stage)) == 0, stage


def test_finalize_actually_consults_the_carry_check(
        isolated_dao, make_args, capsys):
    """The wiring, not the helper. Every test above passes if the check is
    written and then never called from finalize -- which is precisely the
    failure mode that made the ORIGINAL P6 gate prose-only for months: the
    query existed as a CLI command and the finalize path never ran it.

    Driven through the real chain, so the refusal is the one a run would hit.
    """
    _seed_ledger(isolated_dao, verdict="deferred_to_report")
    _reach_screening_report(isolated_dao, make_args)
    _write_screening_report(isolated_dao, [
        {"field": "사고 장소의 물리적 규모", "description": "원장 연결이 빠져 있다",
         "severity": "high"},
    ])
    capsys.readouterr()

    dao.cmd_update_run_state(
        make_args(stage="screening_report", status="in_progress"))
    assert dao.cmd_finalize_stage(make_args(stage="screening_report")) == 1
    out = capsys.readouterr().out
    # Several distinct refusals are reachable here; assert which one fired.
    assert "P6" in out and "CONFLICT_1" in out, out
    assert "conflict_ref" in out


def test_finalize_passes_once_the_report_carries_the_deferral(
        isolated_dao, make_args, capsys):
    """The other half of the pair: the same run that was refused above must
    succeed once the report carries the conflict, or the gate is simply an
    unconditional block on any deferral."""
    _seed_ledger(isolated_dao, verdict="deferred_to_report")
    _reach_screening_report(isolated_dao, make_args)
    _write_screening_report(isolated_dao, [
        {"field": "사고 장소의 물리적 규모", "description": "DOC_001과 DOC_002가 어긋난다",
         "severity": "high", "conflict_ref": "CONFLICT_1",
         "related_documents": ["DOC_001", "DOC_002"]},
    ])
    capsys.readouterr()

    dao.cmd_update_run_state(
        make_args(stage="screening_report", status="in_progress"))
    assert dao.cmd_finalize_stage(make_args(stage="screening_report")) == 0
    assert "P6" not in capsys.readouterr().out
