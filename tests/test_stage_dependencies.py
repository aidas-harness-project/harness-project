"""Pure unit tests for the stage dependency graph (stage_dependencies.py).

The DAO-level enforcement is exercised in test_dao_run_state.py; these test the
graph logic in isolation so a future edit to the graph is caught here directly.
"""
import stage_dependencies as sd


def _state(*stage_status_pairs):
    return {"stages": [{"stage_name": n, "status": s} for n, s in stage_status_pairs]}


def test_document_processing_requires_only_intake():
    """Segmentation is a checkpoint inside document_processing, not before it.

    Once OCR moved ahead of the split (2026-08-05), the bundle is OCR'd and
    redacted, THEN split, then its children are classified and redacted -- so
    document_processing runs on both sides of segmentation. Requiring a
    separate document_segmentation stage to PASS first became unsatisfiable:
    the work it names cannot finish until document_processing has already
    started. The human bundle decision and the boundary-approval gate are
    unaffected; they are enforced on the manifest by check_segmentation_ready
    and by segment_case.py's split readiness, not by this graph.
    """
    # No intake entry -> dropped for legacy fork/direct-harness flows; the
    # manifest preflight remains authoritative there.
    assert sd.check_dependencies("document_processing", "in_progress", _state()) == []
    # intake present but not passed -> blocked.
    blockers = sd.check_dependencies("document_processing", "in_progress",
                                     _state(("intake", "in_progress")))
    assert any("intake" in b for b in blockers)
    # Passed intake is now sufficient -- no second Stage-1 stage to wait on.
    assert sd.check_dependencies("document_processing", "in_progress",
                                 _state(("intake", "passed"))) == []


def test_a_legacy_run_recording_document_segmentation_still_advances():
    """CASE_112 recorded the two as separate stages; its record is not rewritten.

    That run really did execute split-then-OCR, so the entry is accurate
    history. Nothing new may write the value (the schema marks it deprecated),
    but a run that already carries it must not become un-resumable.
    """
    assert sd.check_dependencies("document_processing", "in_progress",
                                 _state(("intake", "passed"),
                                        ("document_segmentation", "passed"))) == []
    # Even mid-flight, an old segmentation entry does not gate anything now.
    assert sd.check_dependencies("document_processing", "in_progress",
                                 _state(("intake", "passed"),
                                        ("document_segmentation", "in_progress"))) == []


def test_policy_requires_document_processing_passed():
    assert sd.check_dependencies("policy_clause_processing", "in_progress",
                                 _state(("document_processing", "in_progress"))) != []
    assert sd.check_dependencies("policy_clause_processing", "in_progress",
                                 _state(("document_processing", "passed"))) == []


def test_absent_prerequisite_is_not_auto_satisfied():
    # claim_analysis needs policy_clause_processing passed; absent = blocked.
    blockers = sd.check_dependencies("claim_analysis", "in_progress",
                                     _state(("document_processing", "passed")))
    assert any("policy_clause_processing" in b and "absent" in b for b in blockers)


def test_screening_requires_both_claim_and_consistency():
    st = _state(("document_processing", "passed"), ("policy_clause_processing", "passed"),
                ("claim_analysis", "passed"))
    # consistency_check missing -> blocked.
    assert sd.check_dependencies("screening_report", "in_progress", st) != []
    st["stages"].append({"stage_name": "consistency_check", "status": "passed"})
    assert sd.check_dependencies("screening_report", "in_progress", st) == []


def test_draft_v2_requires_critic_v1_in_addition_to_draft_and_validation():
    """v2 must have the findings it is required to address, not merely v1."""
    state = _state(("draft_report_v1", "passed"),
                   ("denial_validation", "passed"))
    blockers = sd.check_dependencies("draft_report_v2", "in_progress", state)
    assert any("critic_v1" in blocker and "absent" in blocker
               for blocker in blockers), blockers

    state["stages"].append({"stage_name": "critic_v1", "status": "passed"})
    assert sd.check_dependencies("draft_report_v2", "in_progress", state) == []


def test_parallel_frontier_returns_only_static_graph_ready_candidates():
    state = _state(("document_processing", "passed"),
                   ("policy_clause_processing", "pending"),
                   ("denial_response", "pending"))
    assert sd.parallel_frontier(
        state, {"policy_clause_processing", "denial_response", "claim_analysis"}
    ) == ("denial_response", "policy_clause_processing")


def test_parallel_frontier_does_not_admit_started_or_blocked_stages():
    state = _state(("document_processing", "passed"),
                   ("policy_clause_processing", "in_progress"),
                   ("claim_analysis", "pending"))
    assert sd.parallel_frontier(
        state, {"policy_clause_processing", "claim_analysis"}
    ) == ()


def test_only_indexing_is_skippable():
    assert sd.is_skippable("indexing")
    assert not sd.is_skippable("policy_clause_processing")
    assert not sd.is_skippable("document_processing")


def test_skipped_target_rejected_for_non_skippable_stage():
    blockers = sd.check_dependencies("policy_clause_processing", "skipped", _state())
    assert any("not skippable" in b for b in blockers)
    assert sd.check_dependencies("indexing", "skipped", _state()) == []


def test_pending_and_failed_never_have_prerequisites():
    # You can always mark a stage failed or pending regardless of upstream.
    assert sd.check_dependencies("claim_analysis", "failed", _state()) == []
    assert sd.check_dependencies("claim_analysis", "pending", _state()) == []


def test_unknown_stage_is_fail_closed():
    """Part 11I reversed this. It used to assert that an unlisted stage was
    permissive -- which made inventing a stage name the cheapest way past the
    whole graph. That is now closed: a name that is not one of the canonical
    stages is refused for every target status.

    `evaluation` used to be asserted here as the sole ground-truth exception.
    It is no longer a canonical stage at all -- local Units 1-7 execution is
    forbidden pending the isolated Unit 11 service -- so it is now correctly
    refused by this same rule rather than carrying a prerequisite."""
    blockers = sd.check_dependencies("not_a_real_stage", "in_progress", _state())
    assert any("unknown stage" in b for b in blockers), blockers
    # Refused for every target status, not only advancement.
    assert sd.check_dependencies("not_a_real_stage", "failed", _state())
    assert sd.check_dependencies("not_a_real_stage", "skipped", _state())
