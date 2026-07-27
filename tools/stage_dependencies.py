"""The pipeline's stage dependency graph -- one source of truth.

Before this module, run-state advancement checked nothing but the schema enum:
any stage could be marked `passed` in any order, and CASE_030 proved the cost --
`policy_clause_processing` sat `passed` while `document_processing` was still
`in_progress` (attempt 3), and the "passed" stage had `backup_path: null`. A
downstream consumer keying on `get_last_passed_stage` would have resumed past a
foundation that never actually completed.

This module answers three questions the run-state writer needs and the schema
cannot express:

  1. requires(stage)   -- which upstream stages must be `passed` (or a
     dependency-permitted `skipped`) before this stage may start or finalize.
  2. is_skippable(stage) -- may this stage be recorded `skipped` at all, and
     does a dependent accept an upstream `skipped` in place of `passed`.
  3. check_dependencies(stage, target_status, state) -- the actual gate the
     DAO calls, returning a list of human-readable blocker strings (empty =
     allowed), the same contract validate_instance()/cross_contract use.

`requires` names a stage's *hard* upstream prerequisites. It is deliberately
NOT "the previous stage in the enum": `denial_response` is dependency-triggered
(pipeline.md) and is not a prerequisite of `screening_report` here, even though
it sits between `claim_analysis` and `screening_report` in the enum order. Enum
adjacency and dependency are different graphs; conflating them is the bug this
replaces.

Only stages this task's scope actually reasons about (through
`screening_report`) get explicit prerequisites. Later Phase-1/Phase-2 stages
(draft/critic/evaluation/denial_validation) are left with no hard prereqs here
rather than guessing a graph the task does not specify -- adding them is a
one-line change per stage when their gating is designed. An unknown or
unlisted stage is treated as having no prerequisites (permissive), so this
module never blocks a stage it was never told about.
"""

# --- optional stages -------------------------------------------------------
# A stage that the pipeline may legitimately not run. `indexing` is the
# Stage-3 adapter: pass-through by default, a genuine no-op unless enabled
# (pipeline.md). Recording it `skipped` is how a run says "this stage did not
# run, on purpose" -- distinct from `pending` (not reached yet) or a missing
# entry (which must NOT be read as "optional, therefore fine").
SKIPPABLE_STAGES = frozenset({
    "indexing",
})

# --- dependency graph ------------------------------------------------------
# stage -> tuple of upstream stage_names that must be `passed` (or a
# dependency-accepted `skipped`, see ACCEPTS_SKIPPED_FROM) before this stage
# may go `in_progress` or `passed`. Empty tuple = no hard prerequisite.
_REQUIRES = {
    "intake": (),
    "document_processing": ("intake",),
    "indexing": ("document_processing",),
    "policy_clause_processing": ("document_processing",),
    "claim_analysis": ("policy_clause_processing",),
    "consistency_check": ("claim_analysis",),
    "screening_report": ("claim_analysis", "consistency_check"),
}

# For each dependent stage, the subset of its prerequisites for which an
# upstream `skipped` is an acceptable substitute for `passed`. A skippable
# stage's dependents must opt in explicitly -- silence means `skipped` upstream
# does NOT satisfy the dependency. `indexing` is skippable AND its one
# dependent path accepts the skip: policy_clause_processing depends on
# document_processing, not indexing, so indexing being skipped never blocks
# anything downstream. (Listed for completeness / future skippable stages.)
_ACCEPTS_SKIPPED_FROM = {
    # dependent_stage: frozenset(upstream stages whose `skipped` is acceptable)
}


def is_skippable(stage: str) -> bool:
    """May this stage legitimately be recorded `skipped`?"""
    return stage in SKIPPABLE_STAGES


def requires(stage: str) -> tuple:
    """Hard upstream prerequisites for `stage` (empty if none/unknown)."""
    return _REQUIRES.get(stage, ())


def dependents_of(stage: str) -> set:
    """Every stage that transitively depends on `stage` (excluding itself).

    The reverse closure of _REQUIRES. Used by the DAO's invalidation cascade:
    when an upstream artifact changes, a downstream stage recorded `passed`
    was derived from bytes that no longer exist, so its status is a false
    claim -- it must be invalidated, not left standing (Part 11F).

    Because this is derived from _REQUIRES rather than maintained separately,
    extending the dependency graph automatically extends the cascade; the two
    can never drift apart.
    """
    direct = {s for s, prereqs in _REQUIRES.items() if stage in prereqs}
    out = set()
    frontier = list(direct)
    while frontier:
        current = frontier.pop()
        if current in out:
            continue
        out.add(current)
        frontier.extend(
            s for s, prereqs in _REQUIRES.items() if current in prereqs)
    out.discard(stage)
    return out


def _stage_status_map(state: dict) -> dict:
    """{stage_name: status} from a run-state dict. A stage with no entry is
    absent from the map -- callers must treat 'absent' as unmet, never as a
    silent pass (that is the whole point: an optional stage with no entry is
    NOT automatically satisfied)."""
    return {s["stage_name"]: s["status"] for s in state.get("stages", [])}


def _intake_present(state: dict) -> bool:
    """Whether this run actually has an intake stage entry. Per the task,
    `document_processing requires intake=passed` only *if intake exists in the
    run* -- some flows (a fork that reused already-intaken data, a direct
    document_processing test harness) legitimately never record intake. A
    prerequisite that names `intake` is dropped when no intake entry exists,
    but is enforced strictly the moment one does."""
    return "intake" in _stage_status_map(state)


def check_dependencies(stage: str, target_status: str, state: dict) -> list:
    """Return blocker strings for advancing `stage` to `target_status`.

    Empty list = allowed. Non-empty = refuse (the caller prints them and does
    not persist). Only `in_progress` and `passed` transitions are gated --
    `pending`, `failed`, and `skipped` never have upstream prerequisites (you
    can always mark a stage failed or not-run).

    `skipped` additionally requires the stage to be in SKIPPABLE_STAGES; a
    non-skippable stage cannot be recorded skipped.
    """
    errors = []

    if target_status == "skipped" and not is_skippable(stage):
        errors.append(
            f"stage {stage!r} is not skippable -- only {sorted(SKIPPABLE_STAGES)} "
            f"may be recorded 'skipped'"
        )
        return errors

    if target_status not in ("in_progress", "passed"):
        return errors

    status_map = _stage_status_map(state)
    accepts_skipped = _ACCEPTS_SKIPPED_FROM.get(stage, frozenset())

    for dep in requires(stage):
        # The one conditional prerequisite: intake only gates when it exists.
        if dep == "intake" and not _intake_present(state):
            continue
        dep_status = status_map.get(dep)
        ok_statuses = {"passed"}
        if dep in accepts_skipped:
            ok_statuses.add("skipped")
        if dep_status not in ok_statuses:
            shown = dep_status if dep_status is not None else "absent (never recorded)"
            errors.append(
                f"cannot advance {stage!r} to {target_status!r}: prerequisite "
                f"{dep!r} is {shown} -- must be {'/'.join(sorted(ok_statuses))} first"
            )

    return errors
