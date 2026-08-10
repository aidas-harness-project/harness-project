"""run-state timing honesty (findings 2026-08-04 §4).

`started_at`/`completed_at` record when the ORCHESTRATOR moved a marker, not
how long a stage took. CASE_907 showed all four ways that misleads: 998 minutes
for document_processing (an overnight wall-clock gap on a retried stage), a
null start for policy_clause_processing, two stages whose spans overlap, and
0.0 minutes for draft_report_v2 (finalize called immediately after the marker
moved).

This does not add real work measurement -- that needs instrumentation at the
call sites, not a schema field. What it fixes is that the numbers no longer
*look* like durations they are not: the latest attempt is distinguishable from
the first, a missing start says so, and both descriptions state plainly that
these are marker times.
"""
import json

import pytest

import dao


RUN = "RUN_20260804_1"


def _state(case_id):
    return json.loads(dao.run_state_path(case_id).read_text(encoding="utf-8"))


def _stage(case_id, name):
    return next(s for s in _state(case_id)["stages"] if s["stage_name"] == name)


def test_first_attempt_sets_both_timestamps(isolated_dao):
    dao._update_run_state("CASE_009", RUN, "intake", "in_progress", "tester")
    entry = _stage("CASE_009", "intake")
    assert entry["started_at"] is not None
    assert entry["current_attempt_started_at"] == entry["started_at"]


def test_retry_moves_current_attempt_but_not_started_at(isolated_dao):
    """The 998-minute case. started_at is the stage's origin; without a
    separate current-attempt field, a stage retried the next morning reports
    the entire overnight gap as its duration."""
    dao._update_run_state("CASE_009", RUN, "intake", "in_progress", "tester")
    first = _stage("CASE_009", "intake")

    dao._update_run_state("CASE_009", RUN, "intake", "in_progress", "tester")
    second = _stage("CASE_009", "intake")

    assert second["started_at"] == first["started_at"], "origin must not move"
    assert second["current_attempt_started_at"] >= first["current_attempt_started_at"]
    assert second["attempt_count"] == 2


def test_terminal_status_without_a_start_marker_says_so(isolated_dao):
    """CASE_907's policy_clause_processing ended with started_at: null. A hole
    reads as missing data; the note states that the marker was never moved and
    no duration can be derived."""
    dao._update_run_state("CASE_009", RUN, "indexing", "failed", "tester")
    entry = _stage("CASE_009", "indexing")
    assert entry["started_at"] is None
    assert "no start marker" in (entry.get("timing_note") or "")


def test_no_timing_note_when_the_stage_was_started_properly(isolated_dao):
    dao._update_run_state("CASE_009", RUN, "intake", "in_progress", "tester")
    dao._update_run_state("CASE_009", RUN, "intake", "failed", "tester")
    entry = _stage("CASE_009", "intake")
    assert entry["started_at"] is not None
    assert entry.get("timing_note") is None


def test_completed_at_is_refreshed_on_each_terminal_transition(isolated_dao):
    dao._update_run_state("CASE_009", RUN, "intake", "in_progress", "tester")
    dao._update_run_state("CASE_009", RUN, "intake", "failed", "tester")
    first_completed = _stage("CASE_009", "intake")["completed_at"]
    dao._update_run_state("CASE_009", RUN, "intake", "in_progress", "tester")
    dao._update_run_state("CASE_009", RUN, "intake", "failed", "tester")
    assert _stage("CASE_009", "intake")["completed_at"] >= first_completed


def test_new_fields_are_additive_for_pre_existing_files():
    """A run-state written before 2026-08-04 has neither field and must stay
    valid -- these are diagnostics, not new obligations."""
    from _validation import load_registry, validate_instance
    schemas, registry = load_registry()
    legacy = {
        "case_id": "CASE_009",
        "run_id": RUN,
        "stages": [{"stage_name": "intake", "status": "passed", "started_at": None,
                    "completed_at": "2026-07-14T10:00:00+09:00", "attempt_count": 1,
                    "backup_path": "outputs/CASE_009/_backups/step_1_intake/"}],
    }
    errors = validate_instance(legacy, "run_state.schema.json", schemas, registry)
    assert not any("current_attempt_started_at" in str(e) or "timing_note" in str(e)
                   for e in errors), errors


def test_schema_documents_these_as_marker_times_not_durations():
    """The field descriptions are the actual deliverable here: the risk §4
    names is someone using (completed_at - started_at) as a cost figure to
    justify a design decision. If that warning is ever dropped, this fails."""
    import json as _json
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    schema = _json.loads((root / "schemas" / "run_state.schema.json").read_text(encoding="utf-8"))
    props = schema["$defs"]["stage_entry"]["properties"]
    started = props["started_at"]["description"]
    assert "MARKER-MOVEMENT" in started or "marker-movement" in started.lower()
    assert "998" in started, "the concrete counter-example keeps the warning credible"
    assert "cost" in started.lower()
