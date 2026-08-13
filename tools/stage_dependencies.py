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

Part 11I completed the graph. It previously stopped at `screening_report`, and
every later stage (draft/critic/denial_validation/human review) fell through to a
permissive default -- so `draft_report_v2` could be recorded `passed` in a run
where nothing had ever been drafted, and `evaluation` (the sole D1 exception,
the one stage allowed to read ground truth) had no prerequisite at all. Two
things changed:

  * every canonical stage in `run_state.schema.json`'s enum now has an explicit
    entry here, and `KNOWN_STAGES` is asserted against that enum by the tests,
    so adding a stage to the pipeline without deciding its prerequisites fails
    loudly rather than inheriting "no prerequisites";
  * an unknown stage is now FAIL-CLOSED. The old permissive default meant a
    typo'd or newly-invented stage name was the easiest way to advance past
    every gate in this module. Refusing a name we were never told about costs
    one line in this file; accepting it costs the whole graph.

Local Evaluation is deliberately absent. The terminal local stages record
human-owned review and the future external Unit 11 handoff boundary.
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
    # --- Phase 1 ---------------------------------------------------------
    "intake": (),
    # DEPRECATED, retained so a run written before 2026-08-05 (CASE_112) still
    # resolves. Nothing new records it -- see the schema's stage_name note.
    "document_segmentation": ("intake",),
    # Segmentation is a CHECKPOINT inside this stage, not a stage before it.
    # With OCR ahead of the split, the bundle is OCR'd and redacted, then split,
    # then its children are classified and redacted -- so document_processing
    # runs on both sides of segmentation and cannot wait on it. The human bundle
    # decision and the boundary-approval gate are enforced on the manifest
    # (check_segmentation_ready, segment_case.split readiness), not here.
    "document_processing": ("intake",),
    "indexing": ("document_processing",),
    "policy_clause_processing": ("document_processing",),
    "claim_analysis": ("policy_clause_processing",),
    # Dependency-triggered, not phase-gated (pipeline.md): it needs an
    # insurer-response document's processed text and nothing else. It is NOT a
    # prerequisite of screening_report -- most cases have no insurer response.
    "denial_response": ("document_processing",),
    "consistency_check": ("claim_analysis",),
    "screening_report": ("claim_analysis", "consistency_check"),
    "draft_report_v1": ("screening_report",),
    "critic_v1": ("draft_report_v1",),
    "human_review_v1": ("critic_v1",),
    # --- Phase 2 ---------------------------------------------------------
    # Validates the insurer's stated denial reasons against the case's
    # evidence, so it needs both: the reasons (denial_response) and the
    # evidence base the consistency check cleared. It does NOT require the v1
    # draft -- rebuttal points are built from evidence, not from the draft.
    "denial_validation": ("denial_response", "consistency_check"),
    # v2 is an UPDATE of v1 that incorporates rebuttal points and must dispose
    # of the v1 critic's binding findings, so all three are prerequisites.
    "draft_report_v2": ("draft_report_v1", "critic_v1", "denial_validation"),
    "critic_v2": ("draft_report_v2",),
    "human_review_v2": ("critic_v2",),
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


# Stages that need a gate this module cannot see in run-state. `evaluation` is
# the only one: D1's human-review completion is a flag file on disk, owned by
# the DAO. Listed here so the rule lives with the rest of the graph.
HUMAN_REVIEW_GATED_STAGES = frozenset()

KNOWN_STAGES = frozenset(_REQUIRES)


def is_skippable(stage: str) -> bool:
    """May this stage legitimately be recorded `skipped`?"""
    return stage in SKIPPABLE_STAGES


def is_known(stage: str) -> bool:
    """Whether this module was told about `stage` at all. An unknown stage is
    refused, never treated as dependency-free -- see the module docstring."""
    return stage in KNOWN_STAGES


def requires(stage: str) -> tuple:
    """Hard upstream prerequisites for `stage`.

    Returns () for an unknown stage too, but callers must NOT read that as
    "no prerequisites" -- check `is_known` first. `check_dependencies` does.
    """
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


def parallel_frontier(state: dict, candidates=None) -> tuple[str, ...]:
    """Return graph-ready stages without taking any scheduling action.

    This is intentionally narrower than an orchestrator: it sees only static
    run-state dependencies.  Dynamic document availability, conflict/lock/
    medical gates, T13 attempt markers, retries, and dispatch all stay with
    the caller.  An absent or pending stage is eligible only when the same
    hard dependency gate that protects ``in_progress`` permits it.
    """
    statuses = _stage_status_map(state)
    requested = KNOWN_STAGES if candidates is None else frozenset(candidates)
    ready = []
    for stage in sorted(requested):
        if not is_known(stage):
            continue
        if statuses.get(stage) not in (None, "pending"):
            continue
        if not check_dependencies(stage, "in_progress", state):
            ready.append(stage)
    return tuple(ready)


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


def check_dependencies(stage: str, target_status: str, state: dict,
                        human_review_complete: bool | None = None) -> list:
    """Return blocker strings for advancing `stage` to `target_status`.

    Empty list = allowed. Non-empty = refuse (the caller prints them and does
    not persist). Only `in_progress` and `passed` transitions are gated --
    `pending`, `failed`, and `skipped` never have upstream prerequisites (you
    can always mark a stage failed or not-run).

    `skipped` additionally requires the stage to be in SKIPPABLE_STAGES; a
    non-skippable stage cannot be recorded skipped.

    An unknown `stage` is refused outright, for every target status: a name
    this module has never heard of is a typo or an invention, and either way
    granting it free passage past the whole graph is the wrong default.

    `human_review_complete` is the DAO-supplied answer to D1's on-disk gate for
    HUMAN_REVIEW_GATED_STAGES. None (the default) blocks: not being told is not
    the same as being told yes.
    """
    errors = []

    if not is_known(stage):
        errors.append(
            f"unknown stage {stage!r} -- not one of the canonical stages "
            f"{sorted(KNOWN_STAGES)}; an unrecognized stage name is refused "
            "rather than treated as having no prerequisites"
        )
        return errors

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

    if stage in HUMAN_REVIEW_GATED_STAGES and human_review_complete is not True:
        shown = ("not supplied by the caller" if human_review_complete is None
                 else "not marked complete")
        errors.append(
            f"cannot advance {stage!r} to {target_status!r}: human review is "
            f"{shown} -- {stage!r} is the sole ground-truth exception "
            "(harness-guardrails-dev D1) and may only run after a real "
            "recorded expert review has been marked complete"
        )

    return errors
