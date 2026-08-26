"""Which lane's rule governs the medical clearance gate, and in what order.

The gate exists because a published canonical medical revision carries a
review obligation. The selective lane does not publish one -- it reads case
documents and says so -- but "this case has a selective result" must never
become a way to shed an obligation that a canonical revision already created.
Hence a precedence, not a toggle:

1. canonical revision present -> clearance required, selective result or not
2. no canonical revision, validated selective v0.1 result -> not required
3. neither -> unchanged, fail closed

Case 1 is the one worth guarding: it is the only ordering that can silently
drop a real review obligation, and it is only observable when BOTH artifacts
are on disk at once.
"""
from __future__ import annotations

import json

import dao


SELECTIVE_RESULT = {
    "schema_version": "claim_analysis_result.v0.1",
    "medical_projection_status": "not_configured",
    "claim_facts": [],
}

GATED_STATE = {
    "case_id": "CASE_9001",
    "medical_gate_status": "enforced",
    "medical_review_adopted": True,
}


def _write_selective_result(root, case_id="CASE_9001", **overrides):
    payload = dict(SELECTIVE_RESULT)
    payload.update(overrides)
    case_dir = root / "outputs" / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "claim_analysis_result.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _canonical_revision_present(monkeypatch, present: bool) -> None:
    if present:
        monkeypatch.setattr(
            dao, "_load_medical_revision",
            lambda case_id, sha: ({"schema_version": "medical_variables.v0.1"}, None))
    else:
        monkeypatch.setattr(
            dao, "_load_medical_revision",
            lambda case_id, sha: (None, "medical variable contract or revision not found"))


def _gate_applies(monkeypatch) -> None:
    monkeypatch.setattr(dao, "_medical_gate_applies", lambda state: True)


# ----------------------------------------------------------- precedence --

def test_canonical_revision_requires_clearance_even_with_a_selective_result(
    isolated_dao, monkeypatch
) -> None:
    """Rule 1. A selective result does not discharge a canonical obligation.

    Both artifacts exist. Reading the selective result first -- the obvious
    implementation -- would waive a review the published revision genuinely
    owes, and nothing downstream would show that it happened.
    """
    _gate_applies(monkeypatch)
    _canonical_revision_present(monkeypatch, True)
    _write_selective_result(isolated_dao)

    assert dao.source_grounded_lane_only("CASE_9001") is False
    assert dao._medical_clearance_required(
        GATED_STATE, "claim_analysis", "passed", case_id="CASE_9001") is True


def test_selective_only_case_does_not_require_clearance(
    isolated_dao, monkeypatch
) -> None:
    """Rule 2. No canonical revision means no canonical conclusion to clear."""
    _gate_applies(monkeypatch)
    _canonical_revision_present(monkeypatch, False)
    _write_selective_result(isolated_dao)

    assert dao.source_grounded_lane_only("CASE_9001") is True
    assert dao._medical_clearance_required(
        GATED_STATE, "claim_analysis", "passed", case_id="CASE_9001") is False


def test_neither_artifact_present_stays_fail_closed(
    isolated_dao, monkeypatch
) -> None:
    """Rule 3. Absence of evidence about the lane is not a waiver."""
    _gate_applies(monkeypatch)
    _canonical_revision_present(monkeypatch, False)

    assert dao.source_grounded_lane_only("CASE_9001") is False
    assert dao._medical_clearance_required(
        GATED_STATE, "claim_analysis", "passed", case_id="CASE_9001") is True


def test_a_non_selective_result_does_not_open_the_lane(
    isolated_dao, monkeypatch
) -> None:
    """Only a result that actually declares the source-grounded position counts.

    A contract missing `medical_projection_status`, or carrying some other
    schema version, is not this lane and must not inherit its treatment.
    """
    _gate_applies(monkeypatch)
    _canonical_revision_present(monkeypatch, False)

    _write_selective_result(isolated_dao, medical_projection_status="configured")
    assert dao.source_grounded_lane_only("CASE_9001") is False

    _write_selective_result(isolated_dao, schema_version="claim_analysis_result.v9.9")
    assert dao.source_grounded_lane_only("CASE_9001") is False


def test_without_a_case_id_the_gate_stays_closed(isolated_dao, monkeypatch) -> None:
    """The probe needs a case to look at; absent one, the safe answer is 'gated'."""
    _gate_applies(monkeypatch)
    _canonical_revision_present(monkeypatch, False)
    _write_selective_result(isolated_dao)

    assert dao._medical_clearance_required(
        GATED_STATE, "claim_analysis", "passed") is True


# ------------------------------------------------ the canonical lane is intact --

def test_post_medical_stages_are_unaffected_by_the_lane_probe(
    isolated_dao, monkeypatch
) -> None:
    """The carve-out is scoped to claim_analysis and does not leak downstream."""
    _gate_applies(monkeypatch)
    _canonical_revision_present(monkeypatch, False)
    _write_selective_result(isolated_dao)

    for stage in sorted(dao.POST_MEDICAL_STAGES):
        assert dao._medical_clearance_required(
            GATED_STATE, stage, "passed", case_id="CASE_9001") is True


def test_an_unenforced_gate_is_still_unenforced(isolated_dao, monkeypatch) -> None:
    """The pre-existing waiver keeps working; this change did not replace it."""
    monkeypatch.setattr(dao, "_medical_gate_applies", lambda state: False)
    _canonical_revision_present(monkeypatch, True)

    assert dao._medical_clearance_required(
        GATED_STATE, "claim_analysis", "passed", case_id="CASE_9001") is False


# ------------------------------------------------------------- reporting --

def test_the_lane_is_never_reported_as_cleared(
    isolated_dao, monkeypatch, make_args, capsys
) -> None:
    """Not-required is not approval, and the wording has to keep them apart.

    A reviewer scanning run records for 'cleared' must not find this case among
    them: nothing was reviewed, and nothing was waived. What happened is that
    no canonical conclusion existed for the gate to be about.
    """
    _canonical_revision_present(monkeypatch, False)
    _write_selective_result(isolated_dao)
    monkeypatch.setattr(dao, "validated_run_state", lambda case_id, allow_missing: None)
    monkeypatch.setattr(
        dao, "cmd_check_clear",
        lambda module, args: (_ for _ in ()).throw(
            AssertionError("the selective lane must not consult the review ledger")),
        raising=False,
    )

    args = make_args(case_id="CASE_9001", run_id="RUN_20260819_1")
    assert dao.cmd_check_medical_reviews_clear(args) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["gate"] == "not_required_source_grounded_lane"
    for forbidden in ("cleared", "approved", "passed"):
        assert forbidden not in payload["gate"]


def test_the_check_agrees_with_the_gate_it_fronts(
    isolated_dao, monkeypatch, make_args
) -> None:
    """A pre-pass check that disagrees with its own gate is the CASE_028 stall.

    There, the gate waived clearance while the check failed closed, and the run
    sat `in_progress` forever with every contract already written. The two must
    answer the same question the same way.
    """
    _canonical_revision_present(monkeypatch, False)
    _write_selective_result(isolated_dao)
    monkeypatch.setattr(dao, "validated_run_state", lambda case_id, allow_missing: None)
    monkeypatch.setattr(dao, "_medical_gate_applies", lambda state: True)

    gate_requires = dao._medical_clearance_required(
        GATED_STATE, "claim_analysis", "passed", case_id="CASE_9001")
    args = make_args(case_id="CASE_9001", run_id="RUN_20260819_1")
    check_passes = dao.cmd_check_medical_reviews_clear(args) == 0

    assert gate_requires is False and check_passes is True
