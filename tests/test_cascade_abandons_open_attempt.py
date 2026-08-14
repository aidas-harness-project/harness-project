"""A cascade that invalidates an in_progress stage must close its attempt.

CASE_142 found this the way only a run can: `enable-canonical-uids` demoted
`policy_clause_processing` from in_progress to failed, which is by design, but
emitted no attempt marker. The start marker stayed open forever, and
aggregate-trace reported the whole run as incomplete coverage for a stage that
had demonstrably stopped -- while the real cost of that attempt was
unrecoverable, since no end timestamp existed.

The fix reuses the `abandoned` marker T13 already defined for interrupted
attempts rather than inventing a second mechanism. Duration stays null: the
abandon timestamp is when the cascade ran, not when work stopped, and inventing
a duration from it would be exactly the marker-time-as-cost error the run_state
timing fields carry an explicit warning about.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import dao
import trace as trace_mod
import trace_aggregate


BASE = datetime(2026, 8, 14, 0, 0, 0, tzinfo=timezone.utc)
RUN = "RUN_20260814_009"
CASE = "CASE_999"


@pytest.fixture(autouse=True)
def _reset_trace_state():
    trace_mod.reset()
    yield
    trace_mod.reset()


def _record(span_id, op, *, offset, duration=0.0, category="marker",
            attempt=None, attrs=None, status="ok"):
    return {
        "schema_version": "0.1",
        "span_id": span_id,
        "parent_span_id": None,
        "run_id": RUN,
        "case_id": CASE,
        "doc_id": None,
        "page": None,
        "op": op,
        "category": category,
        "t_start_wall": (BASE + timedelta(seconds=offset)).isoformat(),
        "t_start_mono": offset,
        "duration_s": duration,
        "status": status,
        "attempt": attempt,
        "shard_id": "1_1_a",
        "pid": 1,
        "thread": 1,
        "attrs": attrs or {},
    }


def _start(stage, attempt, offset):
    return _record(
        f"start-{stage}-{attempt}", "stage.attempt.start",
        offset=offset, attempt=attempt,
        attrs={"marker_kind": "stage_attempt_start", "stage_name": stage})


def _abandoned(stage, attempt, offset):
    return _record(
        f"abandoned-{stage}-{attempt}", "stage.attempt.abandoned",
        offset=offset, attempt=attempt, status="error",
        attrs={"marker_kind": "stage_attempt_abandoned", "stage_name": stage,
               "attempt_outcome": "interrupted"})


def _summary(spans, run_state_stages=None):
    return trace_aggregate.summarize(
        spans, case_id=CASE, run_id=RUN,
        generated_at=BASE.isoformat(), dropped_span_lines=0, shard_count=1,
        run_state_stages=run_state_stages)


# --- aggregator: abandoned is settled, open is not -------------------------

def test_abandoned_attempt_is_distinguished_from_still_open():
    """Without the fix both look identical: start with no end."""
    abandoned = _summary([
        _start("policy_clause_processing", 1, 0),
        _abandoned("policy_clause_processing", 1, 60),
    ])["stage_attempts"][0]
    still_open = _summary([
        _start("policy_clause_processing", 1, 0),
    ])["stage_attempts"][0]

    assert abandoned["pairing_status"] == "abandoned"
    assert still_open["pairing_status"] == "open"
    assert abandoned["pairing_status"] != still_open["pairing_status"]


def test_abandoned_attempt_reports_interrupted_outcome_and_null_duration():
    attempt = _summary([
        _start("policy_clause_processing", 1, 0),
        _abandoned("policy_clause_processing", 1, 60),
    ])["stage_attempts"][0]

    assert attempt["outcome"] == "interrupted"
    # A cascade timestamp is not a work boundary. Deriving 60s from it would
    # be a fabricated cost figure.
    assert attempt["raw_wall_s"] is None
    assert attempt["active_wall_s"] is None
    assert attempt["unattributed_active_s"] is None


def test_abandoned_attempt_does_not_block_complete_coverage():
    """The CASE_142 symptom: one invalidation made the whole run look unmeasured."""
    stages = [
        {"stage_name": "policy_clause_processing", "status": "failed",
         "attempt_count": 1},
    ]
    summary = _summary([
        _start("policy_clause_processing", 1, 0),
        _abandoned("policy_clause_processing", 1, 60),
    ], run_state_stages=stages)

    coverage = summary["stage_coverage"]
    assert coverage["abandoned_attempts"] == 1
    assert coverage["open_attempts"] == 0
    assert coverage["coverage_complete"] is True


def test_still_open_attempt_keeps_coverage_incomplete():
    """The distinction must not be a blanket pass: a genuinely open attempt
    is still missing information, and saying otherwise would hide a crash."""
    stages = [
        {"stage_name": "policy_clause_processing", "status": "in_progress",
         "attempt_count": 1},
    ]
    summary = _summary([
        _start("policy_clause_processing", 1, 0),
    ], run_state_stages=stages)

    assert summary["stage_coverage"]["open_attempts"] == 1
    assert summary["stage_coverage"]["coverage_complete"] is False


def test_abandoned_attempt_counted_per_stage():
    by_stage = _summary([
        _start("policy_clause_processing", 1, 0),
        _abandoned("policy_clause_processing", 1, 60),
    ])["by_stage"]["policy_clause_processing"]

    assert by_stage["abandoned_attempt_count"] == 1
    assert by_stage["open_attempt_count"] == 0
    assert by_stage["total_attempt_active_wall_s"] == 0.0


def test_abandoned_summary_is_schema_valid():
    import _validation

    schemas, registry = _validation.load_registry()
    summary = _summary([
        _start("policy_clause_processing", 1, 0),
        _abandoned("policy_clause_processing", 1, 60),
    ], run_state_stages=[
        {"stage_name": "policy_clause_processing", "status": "failed",
         "attempt_count": 1}])
    summary.setdefault("case_id", CASE)

    errors = _validation.validate_instance(
        summary, "timing_summary.schema.json", schemas, registry)
    assert errors == [], errors


# --- DAO: the cascade actually emits the marker ----------------------------

def _bootstrap_case(tmp_path, monkeypatch, stage, status, attempt_count):
    """A minimal on-disk case whose run-state has one stage in `status`."""
    outputs = tmp_path / "outputs"
    (outputs / CASE).mkdir(parents=True)
    monkeypatch.setattr(dao, "OUTPUTS", outputs)
    trace_mod.reset()

    state = {
        "run_state_version": "run_state.v0.3",
        "case_id": CASE,
        "run_id": RUN,
        "created_at": dao.now_iso(),
        "updated_at": dao.now_iso(),
        "stages": [{
            "stage_name": stage, "status": status,
            "started_at": dao.now_iso(), "completed_at": None,
            "attempt_count": attempt_count, "backup_path": None,
        }],
        "human_input_status": [],
        "medical_review_adopted": False,
    }
    dao.save_run_state(CASE, state)
    return outputs


def _marker_files(outputs, kind, stage):
    marker_dir = outputs / CASE / "_trace" / RUN / "markers"
    if not marker_dir.exists():
        return []
    return sorted(p.name for p in marker_dir.glob(f"stage.attempt.{kind}.{stage}.*.json"))


def test_policy_layer_cascade_emits_abandoned_marker(tmp_path, monkeypatch):
    """This is the exact CASE_142 path: enable-canonical-uids invalidates the
    policy layer while the stage is in_progress with an open attempt."""
    stage = "policy_clause_processing"
    outputs = _bootstrap_case(tmp_path, monkeypatch, stage, "in_progress", 1)

    dao._invalidate_policy_layer(
        CASE, "canonical UID activation", "tester", RUN)

    assert _marker_files(outputs, "abandoned", stage) == [
        f"stage.attempt.abandoned.{stage}.1.json"]
    payload = json.loads(
        (outputs / CASE / "_trace" / RUN / "markers"
         / f"stage.attempt.abandoned.{stage}.1.json").read_text(encoding="utf-8"))
    assert payload["attempt_outcome"] == "interrupted"
    assert payload["stage_name"] == stage


def test_dependent_cascade_emits_abandoned_marker(tmp_path, monkeypatch):
    stage = "claim_analysis"
    outputs = _bootstrap_case(tmp_path, monkeypatch, stage, "in_progress", 1)

    dao._invalidate_dependents(
        CASE, "policy_clause_processing", "upstream changed", "tester", RUN)

    assert _marker_files(outputs, "abandoned", stage) == [
        f"stage.attempt.abandoned.{stage}.1.json"]


def test_cascade_over_passed_stage_emits_no_attempt_marker(tmp_path, monkeypatch):
    """A `passed` stage has no OPEN attempt -- its end marker was already
    written. Emitting a second terminal marker would make the attempt look
    both passed and interrupted."""
    stage = "claim_analysis"
    outputs = _bootstrap_case(tmp_path, monkeypatch, stage, "passed", 1)

    dao._invalidate_dependents(
        CASE, "policy_clause_processing", "upstream changed", "tester", RUN)

    assert _marker_files(outputs, "abandoned", stage) == []


def test_cascade_with_zero_attempts_emits_no_marker(tmp_path, monkeypatch):
    """attempt_count 0 means an incidental contract write advanced the stage
    to in_progress without an explicit dispatch -- there is no attempt to
    close, and inventing attempt 1 would fabricate a dispatch that never
    happened."""
    stage = "claim_analysis"
    outputs = _bootstrap_case(tmp_path, monkeypatch, stage, "in_progress", 0)

    dao._invalidate_dependents(
        CASE, "policy_clause_processing", "upstream changed", "tester", RUN)

    assert _marker_files(outputs, "abandoned", stage) == []
