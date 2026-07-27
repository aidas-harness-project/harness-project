"""Part 11I: the completed dependency graph, fail-closed unknowns, and the
invalidation cascade.

Two shapes are being closed here.

The first is the graph itself. It used to stop at `screening_report`, and
everything after it -- draft, critic, denial validation, evaluation -- fell
through to a permissive default. So `draft_report_v2` could be recorded
`passed` in a run where nothing had ever been drafted, and `evaluation`, the
single stage allowed to read ground truth, had no upstream prerequisite of any
kind. An unlisted stage name was likewise free passage past every gate.

The second is staleness. A stage recorded `passed` is a claim about work done
against particular upstream bytes. When those bytes move -- the policy stage is
demoted, a normalized clause contract is rewritten -- the claim stops being
true, and it must not be left standing for a later resume to trust.
"""
import json

import pytest

import dao
import stage_dependencies as sd


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _stage(name, status="passed"):
    entry = {"stage_name": name, "status": status, "attempt_count": 1}
    if status == "passed":
        entry["backup_path"] = f"outputs/CASE_030/_backups/step_01_{name}"
    return entry


def _state(*entries):
    return {"case_id": "CASE_030", "run_id": "RUN_20260724_001",
            "stages": list(entries)}


# --- the graph reaches the end of the pipeline -----------------------------

def test_every_canonical_stage_has_declared_prerequisites():
    """The run-state enum is the pipeline's stage list. A stage that exists
    there but not here would silently inherit 'no prerequisites', which is the
    exact permissive default Part 11I removed -- so the two must agree."""
    schema = json.loads(
        (dao.ROOT / "schemas" / "run_state.schema.json").read_text(
            encoding="utf-8"))
    canonical = set(schema["$defs"]["stage_name"]["enum"])
    assert sd.KNOWN_STAGES == canonical


def test_screening_report_cannot_start_without_claim_analysis():
    blockers = sd.check_dependencies(
        "screening_report", "in_progress",
        _state(_stage("policy_clause_processing")))
    assert any("'claim_analysis' is absent" in b for b in blockers), blockers


def test_draft_cannot_start_without_screening_report():
    blockers = sd.check_dependencies(
        "draft_report_v1", "in_progress",
        _state(_stage("claim_analysis"), _stage("consistency_check")))
    assert any("'screening_report' is absent" in b for b in blockers), blockers


def test_draft_v2_cannot_start_without_denial_validation():
    blockers = sd.check_dependencies(
        "draft_report_v2", "in_progress", _state(_stage("draft_report_v1")))
    assert any("'denial_validation' is absent" in b for b in blockers), blockers


def test_evaluation_cannot_start_without_the_critic_pass():
    blockers = sd.check_dependencies(
        "evaluation", "in_progress", _state(_stage("draft_report_v1")),
        human_review_complete=True)
    assert any("'critic_v1' is absent" in b for b in blockers), blockers


def test_evaluation_cannot_start_without_human_review():
    """D1's gate is not in run-state, so the DAO supplies the answer. Not
    supplying it must block: 'nobody told us' is not 'yes'."""
    state = _state(_stage("draft_report_v1"), _stage("critic_v1"))
    assert sd.check_dependencies(
        "evaluation", "in_progress", state, human_review_complete=True) == []
    for answer in (False, None):
        blockers = sd.check_dependencies(
            "evaluation", "in_progress", state, human_review_complete=answer)
        assert any("human review is" in b for b in blockers), (answer, blockers)


def test_denial_response_is_not_a_prerequisite_of_screening_report():
    """Dependency-triggered, not phase-gated (pipeline.md). Most cases have no
    insurer response, and requiring one would block them all."""
    assert "denial_response" not in sd.requires("screening_report")
    assert sd.requires("denial_response") == ("document_processing",)


def test_dependents_of_reaches_the_end_of_the_pipeline():
    """The cascade is derived from the graph, so completing the graph
    automatically widened it -- that is the property being asserted, not the
    exact membership."""
    downstream = sd.dependents_of("policy_clause_processing")
    assert {"claim_analysis", "consistency_check", "screening_report",
            "draft_report_v1", "critic_v1", "draft_report_v2", "critic_v2",
            "denial_validation", "evaluation"} <= downstream
    assert "policy_clause_processing" not in downstream
    assert "document_processing" not in downstream


# --- the run-state cascade -------------------------------------------------

@pytest.fixture
def case(isolated_dao):
    out = isolated_dao / "outputs" / "CASE_030"
    out.mkdir(parents=True)
    return out


def test_demoting_policy_stage_invalidates_claim_analysis(case):
    """The required shape: policy_clause_processing goes failed, and a
    claim_analysis sitting `passed` on top of it does not stay passed."""
    _write_json(case / "_run_state.json", _state(
        _stage("document_processing"),
        _stage("policy_clause_processing"),
        _stage("claim_analysis"),
        _stage("screening_report"),
    ))
    state = dao._update_run_state(
        "CASE_030", "RUN_20260724_001", "policy_clause_processing", "failed",
        "policy-pipeline")
    assert state is not None
    statuses = {s["stage_name"]: s["status"] for s in state["stages"]}
    assert statuses["policy_clause_processing"] == "failed"
    assert statuses["claim_analysis"] == "failed"
    assert statuses["screening_report"] == "failed"
    # document_processing is upstream, not downstream -- untouched.
    assert statuses["document_processing"] == "passed"
    invalidated = next(
        s for s in state["stages"] if s["stage_name"] == "claim_analysis")
    assert "policy_clause_processing" in invalidated["invalidation_reason"]
    assert invalidated["invalidated_at"]


def test_demotion_to_pending_also_cascades(case):
    _write_json(case / "_run_state.json", _state(
        _stage("policy_clause_processing"), _stage("claim_analysis")))
    state = dao._update_run_state(
        "CASE_030", "RUN_20260724_001", "policy_clause_processing", "pending",
        "policy-pipeline")
    statuses = {s["stage_name"]: s["status"] for s in state["stages"]}
    assert statuses["claim_analysis"] == "failed"


def test_a_stage_that_was_not_passed_cascades_nothing(case):
    """Only leaving `passed` un-grounds downstream work. A stage moving
    in_progress -> failed never supported anything."""
    _write_json(case / "_run_state.json", _state(
        _stage("document_processing"),
        _stage("policy_clause_processing", "in_progress"),
        _stage("claim_analysis"),
    ))
    state = dao._update_run_state(
        "CASE_030", "RUN_20260724_001", "policy_clause_processing", "failed",
        "policy-pipeline")
    statuses = {s["stage_name"]: s["status"] for s in state["stages"]}
    assert statuses["claim_analysis"] == "passed"


def test_unknown_stage_is_refused_by_the_dao(case):
    _write_json(case / "_run_state.json", _state())
    assert dao._update_run_state(
        "CASE_030", "RUN_20260724_001", "not_a_real_stage", "in_progress",
        "someone") is None


def test_evaluation_finalize_needs_the_human_review_flag(case, monkeypatch):
    _write_json(case / "_run_state.json", _state(
        _stage("claim_analysis"), _stage("consistency_check"),
        _stage("screening_report"), _stage("draft_report_v1"),
        _stage("critic_v1")))
    assert dao._update_run_state(
        "CASE_030", "RUN_20260724_001", "evaluation", "in_progress",
        "evaluation") is None
    assert not dao.human_review_complete_any("CASE_030")
    # The flag mark-human-review-complete writes -- and only that flag.
    _write_json(dao.human_review_flag_path("CASE_030", "v1"),
                {"case_id": "CASE_030", "version": "v1", "reviewer": "Dev"})
    assert dao.human_review_complete_any("CASE_030")
    assert dao._update_run_state(
        "CASE_030", "RUN_20260724_001", "evaluation", "in_progress",
        "evaluation") is not None
