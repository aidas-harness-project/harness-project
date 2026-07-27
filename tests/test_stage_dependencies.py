"""Pure unit tests for the stage dependency graph (stage_dependencies.py).

The DAO-level enforcement is exercised in test_dao_run_state.py; these test the
graph logic in isolation so a future edit to the graph is caught here directly.
"""
import stage_dependencies as sd


def _state(*stage_status_pairs):
    return {"stages": [{"stage_name": n, "status": s} for n, s in stage_status_pairs]}


def test_document_processing_requires_intake_only_when_present():
    # No intake entry -> intake prerequisite is dropped (fork/harness flows).
    assert sd.check_dependencies("document_processing", "in_progress", _state()) == []
    # intake present but not passed -> blocked.
    blockers = sd.check_dependencies("document_processing", "in_progress",
                                     _state(("intake", "in_progress")))
    assert any("intake" in b for b in blockers)
    # intake present and passed -> allowed.
    assert sd.check_dependencies("document_processing", "in_progress",
                                 _state(("intake", "passed"))) == []


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
    whole graph, and left `evaluation` (the sole ground-truth exception) with
    no prerequisite at all. Both are now closed: every canonical stage has an
    entry, and a name that is not one of them is refused."""
    assert sd.requires("evaluation") == ("critic_v1",)
    blockers = sd.check_dependencies("not_a_real_stage", "in_progress", _state())
    assert any("unknown stage" in b for b in blockers), blockers
    # Refused for every target status, not only advancement.
    assert sd.check_dependencies("not_a_real_stage", "failed", _state())
    assert sd.check_dependencies("not_a_real_stage", "skipped", _state())
