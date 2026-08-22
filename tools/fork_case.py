"""Forks a case's downstream-explorable state into a new case_id, reusing
already-completed expensive work (OCR, redaction, chunking) instead of
re-running it.

Why this exists: P10's snapshot-backup only versions outputs/ (contract
JSON, ledgers, run-state) -- never data/processed/ or data/raw/, which live
under a separate root and aren't checkpointed per step. And case_id is the
primary key almost everywhere in the DAO (locks, ledgers, run-state,
conflict ledger) -- there's no run_id-scoped branching. So a genuine branch
needs its own case_id, not just a different run_id pointed at the same
outputs/ tree.

Hard constraint: every schema's case_id field is pattern-locked to
^CASE_[0-9]+$ -- no letters, no descriptive suffix (common_component_output
.schema.json and every ledger/run-state schema). A fork's case_id is just
the next free CASE_NNN; the actual branch relationship (forked from which
case, at which step, why) lives in _fork_record.json instead, since the id
itself can't carry that.

What gets copied, by default:
    outputs/{source}/           -> outputs/{new}/            (always --
                                    case_id fields inside every copied JSON
                                    are rewritten to the new case_id, then
                                    each file is re-validated against its
                                    own schema to confirm the fork didn't
                                    corrupt anything)
    data/processed/{source}/    -> data/processed/{new}/      (always)
    data/raw/{source}/          -> data/raw/{new}/            (only --include-raw)
    data/ground_truth/{source}/ -> data/ground_truth/{new}/   (only
                                    --include-ground-truth -- copies real
                                    answer-key material; prints a loud
                                    warning when used, see D1)

_backups/ and *.lock files are never copied -- a stale lock in a fresh
branch would incorrectly look like an in-progress write, and a fork starts
its own backup history rather than inheriting the source's.

The forked _source_ledger.json keeps the source case's approved/rejected
statuses as-is -- it's a copy of already-reviewed content, not new raw
input, so resetting every entry to pending would force a pointless
re-review of files a human already looked at. This means a forked case_id's
D2 approvals did NOT come from an independent human review of that specific
case_id -- _fork_record.json exists specifically so nobody mistakes a fork
for a freshly-reviewed case.

Refuses to fork if any *.lock file is present anywhere under the source
root being copied -- a lock present means a write may be in progress or was
interrupted; forking possibly-half-written state would just propagate the
problem into the branch (same "don't poll, don't assume stale" discipline
P5 uses for a lock found at run start).

Known limitation, documented rather than silently assumed away: case_id
auto-assignment (scan existing CASE_NNN dirs, use the next number) has a
TOCTOU race if two fork operations run concurrently -- acceptable for a
manual, occasional-use tool at this project's scale, not hardened for
concurrent multi-actor use.

Usage:
    python tools/fork_case.py SOURCE_CASE_ID --label "..." --held-by NAME --run-id RUN_ID
    python tools/fork_case.py SOURCE_CASE_ID --from-step 3 --label "..." --held-by NAME --run-id RUN_ID
    python tools/fork_case.py SOURCE_CASE_ID --through-stage document_processing --label "..." --held-by NAME --run-id RUN_ID
    python tools/fork_case.py SOURCE_CASE_ID --reconstruct-through-stage document_processing --label "..." --held-by NAME --run-id RUN_ID
    python tools/fork_case.py SOURCE_CASE_ID --include-raw --include-ground-truth --label "..." --held-by NAME --run-id RUN_ID

Stage-cut semantics:
    `--through-stage document_processing` creates a cold fork whose next
    dependency frontier is `policy_clause_processing`. It is useful for a
    policy/scheduler experiment, but it is NOT immediately claim-analysis
    ready: claim_analysis is structurally blocked until policy clause
    processing has genuinely passed in that fork. For a V4a claim-analysis
    comparison, create the two document-processing forks, complete the same
    policy preparation stage in each through the normal pipeline, and only
    then begin the A/B claim-analysis runs. This command never carries a
    policy passed status or policy contract forward by assumption.

Reconstruction semantics:
    `--reconstruct-through-stage document_processing` is only for an unpassed
    source boundary. It validates retained Stage-2 artifacts before creating a
    target directory and never treats downstream contracts as proof.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from dao import (case_dir, atomic_write_json, make_history_boundary, now_iso,
                 OUTPUTS, DATA)
from _validation import load_registry, validate_instance, schema_name_for
# tools/trace.py, not the stdlib `trace` module.
import trace as trace_mod

CASE_ID_DIR_RE = re.compile(r"^CASE_(\d+)$")

# --------------------------------------------------------------------------
# Stage-cut fork (--through-stage document_processing)
# --------------------------------------------------------------------------
# The allowlist below is derived from stage OWNERSHIP, not from filenames: a
# contract is retained only if the stage that writes it is at or before the
# cut. `stage_dependencies` supplies the graph, and the per-document contract
# families come from the document-pipeline spec (checkpoints 1-3), which is
# what actually produces them.
#
# Retaining by prefix would be guesswork; a name like `case_type_result.json`
# gives no structural signal about which stage owns it. So each entry here is
# either an exact filename or a documented per-document family, and anything
# unrecognized is EXCLUDED rather than carried over -- a fork that silently
# inherits an unknown contract is exactly the "falsely complete" state
# requirement 6 exists to prevent.

#: Case-level artifacts written at or before document_processing.
_STAGE_CUT_KEEP_FILES = frozenset({
    "document_manifest.json",   # document-pipeline's own manifest state
    "page_chunks.json",         # checkpoint 3 output, consumed by later stages
    "_source_ledger.json",      # reviewed D2 intake state -- avoids re-review
    "_revision_index.json",     # processed-text revision binding
    "_segment_derivation_index.json",
    "_table_region_index.json",
})

#: Per-document contract families from document-pipeline checkpoints 1-3.
_STAGE_CUT_KEEP_PREFIXES = (
    "ocr_result_",
    "classification_result_",
    "redaction_result_",
    "segmentation_proposal_",
)

#: Directories that would make a fresh fork look warm, mid-attempt, or
#: already-reviewed. `_driver_receipts` covers per-unit driver candidates too,
#: since candidates live underneath it.
_STAGE_CUT_DROP_DIRS = frozenset({
    "_backups", "_trace", "_driver_receipts", "_ocr_scratch",
    "_medical_review", "_human_review",
})

STAGE_CUT_SUPPORTED = ("document_processing",)


def _retained_at_cut(name: str) -> bool:
    """Whether one outputs/ entry belongs on the post-document_processing side."""
    if name in _STAGE_CUT_KEEP_FILES:
        return True
    return any(name.startswith(prefix) and name.endswith(".json")
               for prefix in _STAGE_CUT_KEEP_PREFIXES)


def _stage_status(state: dict, stage: str) -> str | None:
    for entry in state.get("stages", []):
        if isinstance(entry, dict) and entry.get("stage_name") == stage:
            return entry.get("status")
    return None


# The three roots a case owns. A fork copies all three, so a path under any of
# them must follow the copy -- a fork that keeps pointing at the source is not
# a branch, it is an alias. Measured 2026-08-14 on a real fork: 773 processed
# paths, 22 outputs paths and 10 raw paths carried the source case id, and the
# raw ones are what OCR opens, so the two arms of an A/B silently shared input
# files while each had its own untouched copy on disk.
#
# Anchored on `<root>/<CASE_ID>/` rather than a bare case-id replace: the id
# also appears as a value (`"case_id": "CASE_142"`), inside prose in review
# notes, and in `_fork_record.json`'s own lineage fields, where rewriting it
# would erase the record of where the fork came from.
_CASE_PATH_ROOTS = ("data/processed", "data/raw", "outputs")


def _rewrite_case_paths(text: str, source_case_id: str, new_case_id: str) -> str:
    for root in _CASE_PATH_ROOTS:
        text = text.replace(f"{root}/{source_case_id}/",
                            f"{root}/{new_case_id}/")
        # Windows-style separators appear in absolute paths recorded by tools
        # that used os.path (backup_path is the common one).
        text = text.replace(f"{root}\\{source_case_id}\\",
                            f"{root}\\{new_case_id}\\")
    return text


def _rewrite_reconstructed_value(value, source_case_id: str, new_case_id: str):
    """Rewrite fork-owned identity and case-root path fields in memory."""
    if isinstance(value, dict):
        return {
            key: (new_case_id if key == "case_id" and child == source_case_id
                  else _rewrite_reconstructed_value(child, source_case_id, new_case_id))
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_reconstructed_value(item, source_case_id, new_case_id)
                for item in value]
    if isinstance(value, str):
        return _rewrite_case_paths(value, source_case_id, new_case_id)
    return value


def _source_json(path: Path, failures: list[str]):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        failures.append(f"{path.name}: unreadable JSON ({exc})")
        return None


def _adapt_legacy_source_ledger(source_ledger: dict, source_case_id: str,
                                new_case_id: str, schemas, registry,
                                source_bytes: bytes):
    """Return a target-only snapshot for the one supported legacy ledger shape.

    This is intentionally narrower than a schema migration: only a ledger that
    has *all three* modern envelope fields absent may be adapted, its original
    file entries must validate unchanged, and every entry must already be
    approved.  The source dict is never mutated.
    """
    rewritten = _rewrite_reconstructed_value(source_ledger, source_case_id, new_case_id)
    normal_errors = validate_instance(rewritten, "source_ledger.schema.json", schemas, registry)
    if not normal_errors:
        return rewritten, None
    missing = {"ledger_version", "history_boundary", "operations"}
    present = missing.intersection(source_ledger)
    if present:
        return None, ("_source_ledger.json: schema-invalid and not the supported "
                      "legacy shape (partial modern envelope present: "
                      + ", ".join(sorted(present)) + ")")
    allowed_missing_errors = {
        "(root): 'ledger_version' is a required property",
        "(root): 'history_boundary' is a required property",
        "(root): 'operations' is a required property",
    }
    if set(normal_errors) != allowed_missing_errors:
        return None, ("_source_ledger.json: schema-invalid and not the supported "
                      "legacy shape -- " + "; ".join(normal_errors))
    files = source_ledger.get("files")
    if not isinstance(files, list) or not files:
        return None, "_source_ledger.json: legacy ledger has no file entries"
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            return None, f"_source_ledger.json: files[{index}] is not an object"
        probe = {
            "ledger_version": "source_ledger.v0.4", "case_id": new_case_id,
            "source_dir": source_ledger.get("source_dir"), "files": [entry],
            "history_boundary": make_history_boundary([entry], mode="legacy_snapshot"),
            "operations": [],
        }
        entry_errors = validate_instance(probe, "source_ledger.schema.json", schemas, registry)
        if entry_errors:
            return None, (f"_source_ledger.json: files[{index}] is invalid -- "
                          + "; ".join(entry_errors))
        if entry.get("review_status") != "approved":
            return None, (f"_source_ledger.json: files[{index}] review_status is "
                          f"{entry.get('review_status')!r}, not approved")
    target = dict(rewritten)
    target["ledger_version"] = "source_ledger.v0.4"
    target["history_boundary"] = make_history_boundary(files, mode="legacy_snapshot")
    target["operations"] = []
    migrated_errors = validate_instance(target, "source_ledger.schema.json", schemas, registry)
    if migrated_errors:
        return None, ("_source_ledger.json: generated legacy snapshot is invalid -- "
                      + "; ".join(migrated_errors))
    return target, {
        "migration_kind": "legacy_source_ledger_snapshot",
        "source_ledger_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "target_history_boundary_sha256": target["history_boundary"]["baseline_sha256"],
        "migrated_fields": ["ledger_version", "history_boundary", "operations"],
        "migrated_at": now_iso(),
    }


def _boundary_digest(artifacts: dict[str, object], processed_files: list[Path],
                     source_case_id: str) -> str:
    """Bind names and bytes without putting source text in provenance."""
    digest = hashlib.sha256()
    for name, data in sorted(artifacts.items()):
        digest.update(name.encode("utf-8")); digest.update(b"\0")
        digest.update(json.dumps(data, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False).encode("utf-8"))
        digest.update(b"\0")
    root = DATA / "processed" / source_case_id
    for path in sorted(processed_files):
        digest.update(str(path.relative_to(root)).replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def prepare_reconstructed_document_processing(
        source_case_id: str, source_root: Path, new_case_id: str) -> dict:
    """Validate an unpassed Stage-2 boundary before making a target directory."""
    failures: list[str] = []
    source_state = _source_json(source_root / "_run_state.json", failures)
    if not isinstance(source_state, dict):
        sys.exit("error: reconstruction refused -- " + "; ".join(failures))
    source_status = _stage_status(source_state, "document_processing")
    if source_status == "passed":
        sys.exit("error: reconstruction is only for an unpassed document_processing "
                 "boundary; use --through-stage document_processing instead.")

    schemas, registry = load_registry()
    artifacts: dict[str, object] = {}
    ledger_migration = None
    for item in sorted(source_root.iterdir()):
        if not item.is_file() or not _retained_at_cut(item.name):
            continue
        data = _source_json(item, failures)
        if data is not None:
            if item.name == "_source_ledger.json" and isinstance(data, dict):
                adapted, migration = _adapt_legacy_source_ledger(
                    data, source_case_id, new_case_id, schemas, registry,
                    item.read_bytes())
                if migration is not None:
                    ledger_migration = migration
                if adapted is None:
                    failures.append(migration)
                else:
                    artifacts[item.name] = adapted
            else:
                artifacts[item.name] = _rewrite_reconstructed_value(
                    data, source_case_id, new_case_id)
    for required in ("document_manifest.json", "page_chunks.json", "_revision_index.json",
                     "_source_ledger.json"):
        if required not in artifacts:
            failures.append(f"{required}: required retained Stage-2 artifact is missing")

    manifest = artifacts.get("document_manifest.json")
    documents = manifest.get("documents") if isinstance(manifest, dict) else None
    active = [doc for doc in documents or [] if isinstance(doc, dict) and
              doc.get("downstream_disposition") in {
                  "automated_text_pipeline", "text_only_no_normalization"}]
    if not active:
        failures.append("document_manifest.json: no active text documents")
    revision = artifacts.get("_revision_index.json")
    revision_ids = {entry.get("document_id") for entry in revision.get("documents", [])
                    if isinstance(entry, dict)} if isinstance(revision, dict) else set()
    chunks = artifacts.get("page_chunks.json")
    chunk_ids = {entry.get("document_id") for entry in chunks.get("chunks", [])
                 if isinstance(entry, dict)} if isinstance(chunks, dict) else set()
    excluded_ids = {entry.get("document_id") for entry in chunks.get("excluded_documents", [])
                    if isinstance(entry, dict)} if isinstance(chunks, dict) else set()
    processed_root = DATA / "processed" / source_case_id
    processed_files: list[Path] = []
    for doc in active:
        document_id = doc.get("document_id")
        if not isinstance(document_id, str):
            failures.append("document_manifest.json: active document without document_id")
            continue
        text_path = processed_root / document_id / "redacted_text.md"
        if not text_path.is_file():
            failures.append(f"{document_id}: missing processed redacted_text.md")
        else:
            processed_files.append(text_path)
        if document_id not in revision_ids:
            failures.append(f"{document_id}: missing current revision entry")
        if document_id not in chunk_ids and document_id not in excluded_ids:
            failures.append(f"{document_id}: missing page-chunk or exclusion record")
        if doc.get("ocr_status") == "completed" and f"ocr_result_{document_id}.json" not in artifacts:
            failures.append(f"{document_id}: missing OCR result")
        if doc.get("document_type") is not None and \
                f"classification_result_{document_id}.json" not in artifacts:
            failures.append(f"{document_id}: missing classification result")
        if doc.get("redacted_text_path") is not None and \
                f"redaction_result_{document_id}.json" not in artifacts:
            failures.append(f"{document_id}: missing redaction result")
    for name, data in sorted(artifacts.items()):
        schema_name = schema_name_for(Path(name))
        if schema_name:
            errors = validate_instance(data, schema_name, schemas, registry)
            if errors:
                failures.append(f"{name}: fails {schema_name} after case-id rewrite -- "
                                + "; ".join(errors))
    if failures:
        sys.exit("error: reconstruction validation failed before target creation:\n  - "
                 + "\n  - ".join(failures))
    return {
        "source_state": source_state,
        "source_document_processing_status": source_status,
        "artifacts": artifacts,
        "retained_boundary_sha256": _boundary_digest(artifacts, processed_files, source_case_id),
        "validated_at": now_iso(),
        "legacy_source_ledger_migration": ledger_migration,
    }


def verify_stage_cut_source(source_case_id: str, source_root: Path,
                            through_stage: str) -> dict:
    """Refuse unless the source genuinely completed the cut stage.

    Checked through the governed artifacts themselves rather than a filename
    survey: run-state must record the stage `passed`, the manifest must exist
    with documents, and every text-processed document must actually have
    redacted text on disk. A source that merely *looks* finished because later
    contracts are present is refused -- CASE_140 is exactly that shape, with
    every downstream contract written while run-state still says
    `document_processing: in_progress`.
    """
    if through_stage not in STAGE_CUT_SUPPORTED:
        sys.exit(f"error: --through-stage supports {list(STAGE_CUT_SUPPORTED)} -- "
                 f"got {through_stage!r}")

    state_path = source_root / "_run_state.json"
    if not state_path.exists():
        sys.exit(f"error: {source_case_id} has no _run_state.json -- cannot confirm "
                 f"{through_stage} completed, refusing to cut.")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"error: {source_case_id} _run_state.json is unreadable: {exc}")

    status = _stage_status(state, through_stage)
    if status != "passed":
        shown = status if status is not None else "absent (never recorded)"
        sys.exit(
            f"error: refusing to cut {source_case_id} at {through_stage} -- its "
            f"run-state records that stage as {shown}, not 'passed'. Later "
            f"contracts existing on disk is NOT evidence the stage completed; "
            f"fork from a source whose run-state actually records the pass."
        )

    manifest_path = source_root / "document_manifest.json"
    if not manifest_path.exists():
        sys.exit(f"error: {source_case_id} has no document_manifest.json -- "
                 f"{through_stage} state is incomplete, refusing to cut.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"error: {source_case_id} document_manifest.json is unreadable: {exc}")
    documents = [d for d in manifest.get("documents", []) if isinstance(d, dict)]
    if not documents:
        sys.exit(f"error: {source_case_id} document_manifest.json lists no documents -- "
                 f"nothing to resume from, refusing to cut.")

    text_dispositions = {"automated_text_pipeline", "text_only_no_normalization"}
    processed_root = DATA / "processed" / source_case_id
    missing = [
        doc["document_id"] for doc in documents
        if doc.get("downstream_disposition") in text_dispositions
        and not (processed_root / str(doc.get("document_id")) / "redacted_text.md").exists()
    ]
    if missing:
        sys.exit(
            f"error: refusing to cut {source_case_id} at {through_stage} -- "
            f"{len(missing)} text-processed document(s) have no redacted text on "
            f"disk: {missing[:5]}{'...' if len(missing) > 5 else ''}. The fork "
            f"would not carry the inputs claim-analysis needs."
        )

    intake_status = _stage_status(state, "intake")
    return {"documents": len(documents), "intake_status": intake_status,
            "source_state": state}


def _enforce_medical_gate_on_fork(state: dict) -> dict:
    """A fork starts under the medical gate, whatever its source did.

    `medical_gate_status: never_evaluated` marks a run that executed before
    the 2026-08-14 gate restore. The scoping decision behind it was narrow:
    do not retroactively fail runs that FINISHED without a check that was not
    running at the time. A fork is not one of those. It carries a new
    `run_id`, its stages are about to execute again, and the check exists now
    -- so inheriting the exemption would let a brand-new run skip the gate for
    no reason other than the age of the case it was copied from.

    Observed on CASE_022 (fork of CASE_142, a pre-restore case):
    `check-medical-reviews-clear` exited 1 while `claim_analysis` finalized
    `passed`. Nothing was bypassed -- `_medical_gate_applies` requires
    `enforced` and this said `never_evaluated` -- which is exactly the
    problem: the exemption propagated silently through a copy.

    Deliberately unconditional. Reading the source's value and preserving it
    when already `enforced` would be the same result by a longer route, and a
    branch here would eventually be read as "sometimes a fork is exempt".
    """
    state["medical_gate_status"] = "enforced"
    return state


def build_stage_cut_run_state(*, new_case_id: str, run_id: str,
                              through_stage: str, source_state: dict) -> dict:
    """A truthful fresh run state for the cut fork.

    Only the cut stage and (when the source recorded it) intake are carried as
    `passed`. Everything downstream is simply absent rather than written as
    `pending`: run-state treats an unrecorded stage as not-yet-run, and
    `parallel_frontier` already offers an absent stage, so inventing pending
    rows would add unverified claims without changing behavior.

    No timing markers, no human_input_status and no inherited attempt history
    are carried -- an inherited attempt is what makes a fork look mid-run, and
    a carried-over review status would be a fabricated human action.
    `attempt_count` is reset to 1: the fork has not retried anything, and
    carrying the source's count forward would spend P9's 3-attempt budget on
    attempts this case never made.

    `backup_path` points at a fork-origin snapshot built INSIDE the new case
    (see `write_fork_origin_snapshot`). Copying the source's path would satisfy
    the schema's non-null rule while naming a directory the fork does not
    contain -- a dangling reference that breaks P10's actual invariant, which
    is that a passed stage is restorable from the snapshot it names.
    """
    now = now_iso()

    def entry(stage: str) -> dict:
        return {
            "stage_name": stage,
            "status": "passed",
            # Explicit nulls, not omitted keys. The stage-item schema declares
            # both as nullable and names no `required` list, so omitting them
            # validated -- and the DAO's `in_progress` path then read
            # `entry["started_at"]` directly and raised KeyError on the first
            # transition of every forked case (CASE_054, 2026-08-22). Null is
            # the honest value: the fork inherited this stage's OUTPUT, it did
            # not run it, so there is no start or completion time to claim.
            "started_at": None,
            "completed_at": None,
            "attempt_count": 1,
            "backup_path": fork_origin_backup_path(new_case_id, stage),
        }

    stages = []
    if _stage_status(source_state, "intake") == "passed":
        stages.append(entry("intake"))
    stages.append(entry(through_stage))
    return {
        "run_state_version": "run_state.v0.3",
        "case_id": new_case_id,
        "run_id": run_id,
        "created_at": now,
        "updated_at": now,
        "medical_review_adopted": False,
        # Explicit, not omitted. An absent value is stamped `never_evaluated`
        # on first touch, which would hand a freshly built fork the exemption
        # meant for runs that finished before the gate existed.
        "medical_gate_status": "enforced",
        "stages": stages,
    }


def fork_origin_backup_path(new_case_id: str, stage: str) -> str:
    """The repo-relative path a stage-cut fork's passed stage points at.

    Every retained stage shares ONE snapshot. A cut does not replay the
    source's step history, so numbering these as separate steps would invent a
    sequence the fork never executed; `step_01_fork_origin` says plainly that
    the fork begins here.
    """
    return f"outputs/{new_case_id}/_backups/step_01_fork_origin"


def write_fork_origin_snapshot(new_case_id: str, run_state: dict) -> Path:
    """Materialize the snapshot the fork's passed stages point at.

    P10's invariant is not "the field is non-null" -- it is that a passed stage
    is restorable from the snapshot it names. So the snapshot must contain the
    fork's OWN retained artifacts, and is therefore built after the cut copy
    rather than inherited: the source's historical backup holds the full warm
    tree, so restoring from it would resurrect exactly the downstream contracts
    the cut removed.

    Assembled in a sibling temp dir and moved with a single os.replace, the
    same way `dao._build_snapshot_atomic` does it, so an interrupted build
    leaves an orphan tmp dir rather than a half-populated backup a restore
    could mistake for complete.
    """
    dest_case = case_dir(new_case_id)
    backups = dest_case / "_backups"
    backups.mkdir(parents=True, exist_ok=True)
    dest = backups / "step_01_fork_origin"
    if dest.exists():
        raise FileExistsError(f"refusing to overwrite immutable backup {dest}")
    tmp = backups / f".tmp_snapshot_fork_origin_{uuid.uuid4().hex}"
    tmp.mkdir(parents=True)
    try:
        for item in dest_case.iterdir():
            if item.name in ("_backups", "_trace") or item.name.endswith(".lock"):
                continue
            if item.is_file():
                shutil.copy2(item, tmp / item.name)
            elif item.is_dir():
                shutil.copytree(item, tmp / item.name, dirs_exist_ok=True)
        # The snapshot records the finalized state, matching what a restore
        # should bring back -- not a pre-finalization view.
        atomic_write_json(tmp / "_run_state.json", run_state)
        os.replace(tmp, dest)
    except Exception:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        raise
    return dest


def copy_stage_cut_outputs(source_root: Path, new_case_id: str,
                           run_id: str, through_stage: str,
                           source_state: dict) -> tuple[list[str], list[str], list[str], dict]:
    """Copy only the retained side of the cut, then rebuild run state.

    Returns (copied, excluded, warnings, rebuilt_run_state). The caller
    finishes the fork by writing `_fork_record.json` and then calling
    `write_fork_origin_snapshot` with the returned state.
    """
    dest = case_dir(new_case_id)
    copied: list[str] = []
    excluded: list[str] = []
    for item in sorted(source_root.iterdir()):
        if item.name.endswith(".lock") or item.is_dir():
            excluded.append(item.name)
            continue
        if not _retained_at_cut(item.name):
            excluded.append(item.name)
            continue
        shutil.copy2(item, dest / item.name)
        copied.append(item.name)

    state = build_stage_cut_run_state(
        new_case_id=new_case_id, run_id=run_id, through_stage=through_stage,
        source_state=source_state)
    atomic_write_json(dest / "_run_state.json", state)
    copied.append("_run_state.json (rebuilt)")

    schemas, registry = load_registry()
    warnings: list[str] = []
    for json_path in sorted(dest.rglob("*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("case_id") is not None:
            data["case_id"] = new_case_id
            atomic_write_json(json_path, data)
        schema_name = schema_name_for(json_path)
        if schema_name:
            errors = validate_instance(data, schema_name, schemas, registry)
            if errors:
                warnings.append(f"{json_path.name}: fails {schema_name} after case_id "
                                "rewrite -- " + "; ".join(errors))

    # The snapshot is NOT built here. It must also capture _fork_record.json,
    # which the caller writes after this function returns, so building it now
    # would produce a snapshot a restore could not reproduce the case from.
    return copied, sorted(excluded), warnings, state


def materialize_reconstructed_outputs(prepared: dict, new_case_id: str,
                                      run_id: str) -> tuple[list[str], list[str], dict]:
    """Materialize only a boundary that was fully validated before target creation."""
    dest = case_dir(new_case_id)
    copied = []
    for name, data in prepared["artifacts"].items():
        atomic_write_json(dest / name, data)
        copied.append(name)
    state = build_stage_cut_run_state(
        new_case_id=new_case_id, run_id=run_id,
        through_stage="document_processing", source_state=prepared["source_state"])
    atomic_write_json(dest / "_run_state.json", state)
    copied.append("_run_state.json (reconstructed)")
    return copied, [], state


def next_free_case_id() -> str:
    """Scans every root a case_id directory could exist under and returns
    the next free CASE_NNN, zero-padded to 3 digits (matching the existing
    CASE_001/CASE_002/CASE_009 convention). Non-numeric dirs (CASE_DEMO,
    CASE_SMOKE) are ignored -- they predate or fall outside the
    ^CASE_[0-9]+$ schema pattern and aren't part of this numbering.

    Normally max+1, so a fork gets a fresh id above everything on disk and
    numbering stays chronological.

    The 3-digit ceiling is real and load-bearing:
    `human_review_ledger.schema.json` pins case_id to ^CASE_[0-9]{3}$ --
    exactly three digits, unlike the ^CASE_[0-9]+$ every other schema uses.
    With CASE_999 present, plain max+1 returned CASE_1000; the fork then
    copied every file, rewrote the case_id into each, and only afterwards
    reported that the id it had just chosen was invalid -- leaving a case
    that could never accept a human-review write.

    So above the ceiling it falls back to the lowest free id rather than
    emitting an unusable one. That is a deliberate second choice: reusing a
    gap loses chronological ordering and can resurrect an id with history
    attached (CASE_002 is free only because its files were rejected in the
    D1 incident), which is why it is the fallback and not the rule.
    """
    used: set[int] = set()
    for root in (OUTPUTS, DATA / "raw", DATA / "processed", DATA / "ground_truth"):
        if not root.exists():
            continue
        for d in root.iterdir():
            m = CASE_ID_DIR_RE.match(d.name)
            if m:
                used.add(int(m.group(1)))
    candidate = (max(used) + 1) if used else 1
    if candidate <= 999:
        return f"CASE_{candidate:03d}"
    for n in range(1, 1000):
        if n not in used:
            return f"CASE_{n:03d}"
    raise RuntimeError(
        "no free CASE_NNN id remains: CASE_001..CASE_999 are all in use. The "
        "3-digit ceiling is a schema constraint (human_review_ledger.schema"
        ".json pins ^CASE_[0-9]{3}$), so going wider needs a schema change, "
        "not a change here."
    )


def resolve_source_root(source_case_id: str, from_step: int | None) -> Path:
    if from_step is None:
        root = case_dir(source_case_id)
        if not root.exists() or not any(root.iterdir()):
            sys.exit(f"error: {root} doesn't exist or is empty -- nothing to fork from.")
        return root
    backups_dir = OUTPUTS / source_case_id / "_backups"
    if not backups_dir.exists():
        sys.exit(f"error: no _backups/ found for {source_case_id} -- can't fork from step {from_step}.")
    matches = sorted(backups_dir.glob(f"step_{from_step:02d}_*"))
    if not matches:
        available = sorted(p.name for p in backups_dir.iterdir())
        sys.exit(f"error: no backup matching step {from_step} -- available: {available}")
    if len(matches) > 1:
        sys.exit(f"error: ambiguous step {from_step} -- multiple matches: {[m.name for m in matches]}")
    return matches[0]


def check_no_active_locks(source_root: Path) -> None:
    locks = list(source_root.rglob("*.lock"))
    if locks:
        sys.exit(f"error: refusing to fork -- {len(locks)} lock file(s) present under {source_root}, "
                  f"a write may be in progress or was interrupted: {[str(p) for p in locks]}")


def copy_outputs_and_rewrite_case_id(source_root: Path, new_case_id: str,
                                     source_case_id: str | None = None) -> list[str]:
    """Returns the list of validation warnings (empty if everything that has
    a schema still validates after the case_id rewrite).

    Rewrites BOTH the `case_id` field and every path under a case root
    (`data/processed`, `data/raw`, `outputs`). Only the field was rewritten
    until 2026-08-14, so a fork's manifest kept pointing at the source case's
    raw PDFs -- and the DAO dutifully read them, giving a "branch" that shared
    input files with its parent while its own copies sat unused. `_fork_record`
    is excluded because its whole job is to record the source id.
    """
    dest = case_dir(new_case_id)
    for item in source_root.iterdir():
        if item.name == "_backups" or item.name.endswith(".lock"):
            continue
        if item.is_file():
            shutil.copy2(item, dest / item.name)
        elif item.is_dir():
            shutil.copytree(item, dest / item.name, dirs_exist_ok=True)

    schemas, registry = load_registry()
    warnings = []
    for json_path in sorted(dest.rglob("*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or "case_id" not in data:
            continue
        if source_case_id and json_path.name != "_fork_record.json":
            data = _rewrite_reconstructed_value(data, source_case_id, new_case_id)
        data["case_id"] = new_case_id
        if json_path.name == "_run_state.json":
            data = _enforce_medical_gate_on_fork(data)
        atomic_write_json(json_path, data)
        schema_name = schema_name_for(json_path)
        if schema_name:
            errors = validate_instance(data, schema_name, schemas, registry)
            if errors:
                warnings.append(f"{json_path.name}: fails {schema_name} after case_id rewrite -- "
                                 + "; ".join(errors))
    return warnings


def copy_data_tree(subdir_name: str, source_case_id: str, new_case_id: str) -> Path | None:
    src = DATA / subdir_name / source_case_id
    if not src.exists():
        return None
    dest = DATA / subdir_name / new_case_id
    shutil.copytree(src, dest, dirs_exist_ok=True)
    if subdir_name == "raw":
        record_path = dest / "_intake_record.json"
        if record_path.exists():
            record = json.loads(record_path.read_text(encoding="utf-8"))
            if isinstance(record, dict) and "case_id" in record:
                record["case_id"] = new_case_id
                atomic_write_json(record_path, record)
    return dest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source_case_id")
    ap.add_argument("--from-step", type=int, help="Fork from a specific P10 backup step instead of current state")
    ap.add_argument(
        "--through-stage", choices=list(STAGE_CUT_SUPPORTED), default=None,
        help="Stage-cut fork: copy only artifacts written at or before this "
             "stage, and rebuild a fresh run state. Mutually exclusive with "
             "--from-step. Refuses unless the source's run-state genuinely "
             "records the stage as passed.",
    )
    ap.add_argument(
        "--reconstruct-through-stage", choices=list(STAGE_CUT_SUPPORTED), default=None,
        help="Reconstruct an independently validated unpassed stage boundary. "
             "Mutually exclusive with --through-stage and --from-step; on any "
             "validation failure no target case directory is created.",
    )
    ap.add_argument("--label", required=True, help="Human-readable description of what this branch is testing")
    ap.add_argument("--include-raw", action="store_true")
    ap.add_argument("--include-ground-truth", action="store_true",
                     help="Copies real answer-key material into the new case_id -- use deliberately, not by default")
    ap.add_argument("--held-by", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument(
        "--new-case-id", default=None, metavar="CASE_NNN",
        help="Target case id instead of the auto-assigned one. Needed once "
             "the tree reaches the CASE_999 ceiling, where auto-assignment "
             "falls back to the lowest free number and can hand back an id "
             "with history attached (CASE_002 is free only because the D1 "
             "incident rejected its files).",
    )
    args = ap.parse_args()
    # Switch tracing on before any instrumented path runs. This tool does not
    # raise spans itself, but the dao/provider calls below do -- and without
    # this they are discarded silently (see trace.configure_from_args).
    trace_mod.configure_from_args(args)

    if not re.match(r"^CASE_\d+$", args.source_case_id):
        sys.exit(f"error: source_case_id must match CASE_NNN -- got {args.source_case_id!r}")

    selected_boundary_modes = sum(value is not None for value in (
        args.from_step, args.through_stage, args.reconstruct_through_stage))
    if selected_boundary_modes > 1:
        sys.exit("error: --from-step, --through-stage, and --reconstruct-through-stage "
                 "are mutually exclusive -- each names a different source boundary.")
    if args.reconstruct_through_stage is not None and \
            (args.include_raw or args.include_ground_truth):
        sys.exit("error: reconstruction forks retain only Stage-2 outputs and "
                 "data/processed; raw and ground-truth copying is refused.")

    source_root = resolve_source_root(args.source_case_id, args.from_step)
    check_no_active_locks(source_root)

    if args.new_case_id is not None:
        # Held to the STRICTER of the two patterns in the schema set: every
        # schema accepts ^CASE_[0-9]+$, but human_review_ledger.schema.json
        # pins exactly three digits, and an id that satisfies only the looser
        # one produces a case that copies fine and then cannot take a
        # human-review write.
        if not re.fullmatch(r"CASE_\d{3}", args.new_case_id):
            sys.exit(f"error: --new-case-id must match CASE_NNN (exactly 3 "
                     f"digits) -- got {args.new_case_id!r}")
        new_case_id = args.new_case_id
    else:
        new_case_id = next_free_case_id()
    # Do NOT call dao.case_dir before reconstruction validation: case_dir makes
    # the directory as a side effect, which would turn a failed validation into
    # the partial target this mode exists to prevent.
    dest = OUTPUTS / new_case_id
    if dest.exists() and any(dest.iterdir()):
        sys.exit(f"error: {dest} already exists and isn't empty -- refusing to fork into it. "
                 f"(TOCTOU race with a concurrent fork? re-run.)")

    cut_copied: list[str] = []
    cut_excluded: list[str] = []
    reconstructed = args.reconstruct_through_stage is not None
    prepared = None
    if reconstructed:
        # This must stay before dest.mkdir and every copy operation.
        prepared = prepare_reconstructed_document_processing(
            args.source_case_id, source_root, new_case_id)
        dest.mkdir(parents=True, exist_ok=False)
        cut_copied, cut_excluded, cut_state = materialize_reconstructed_outputs(
            prepared, new_case_id, args.run_id)
        warnings = []
    elif args.through_stage is not None:
        probe = verify_stage_cut_source(args.source_case_id, source_root,
                                        args.through_stage)
        cut_copied, cut_excluded, warnings, cut_state = copy_stage_cut_outputs(
            source_root, new_case_id, args.run_id, args.through_stage,
            probe["source_state"])
    else:
        warnings = copy_outputs_and_rewrite_case_id(
            source_root, new_case_id, args.source_case_id)

    copy_data_tree("processed", args.source_case_id, new_case_id)
    raw_copied = args.include_raw and copy_data_tree("raw", args.source_case_id, new_case_id) is not None
    gt_copied = False
    if args.include_ground_truth:
        gt_copied = copy_data_tree("ground_truth", args.source_case_id, new_case_id) is not None
        if gt_copied:
            print(f"WARNING: copied data/ground_truth/{args.source_case_id}/ -> data/ground_truth/{new_case_id}/ "
                  f"-- real answer-key material now exists under a second case_id. D1 still applies: "
                  f"only the evaluation stage may ever read it, only after human review is confirmed complete.")

    fork_record = {
        "new_case_id": new_case_id,
        "forked_from": args.source_case_id,
        "forked_at_step": (f"step_{args.from_step:02d}" if args.from_step is not None
                           else (f"reconstructed_through_stage:{args.reconstruct_through_stage}"
                                 if reconstructed else (f"through_stage:{args.through_stage}"
                                                       if args.through_stage is not None else "current"))),
        "forked_at": now_iso(),
        "label": args.label,
        "included_raw": raw_copied,
        "included_ground_truth": gt_copied,
        "ledger_carried_forward": True,
        "held_by": args.held_by,
        "run_id": args.run_id,
    }
    if reconstructed:
        fork_record["reconstruction"] = {
            "boundary": "document_processing",
            "source_document_processing_status": prepared["source_document_processing_status"],
            "validated_at": prepared["validated_at"],
            "retained_boundary_sha256": prepared["retained_boundary_sha256"],
        }
        if prepared["legacy_source_ledger_migration"] is not None:
            fork_record["legacy_source_ledger_migration"] = \
                prepared["legacy_source_ledger_migration"]
    atomic_write_json(dest / "_fork_record.json", fork_record)

    if args.through_stage is not None or reconstructed:
        # Last, so the snapshot the passed stages point at contains the whole
        # finished fork -- including this provenance record. P10's invariant is
        # restorability, not merely a non-null field.
        snapshot = write_fork_origin_snapshot(new_case_id, cut_state)
        cut_copied.append(f"{snapshot.name}/ (fork-origin snapshot)")

    print(f"OK: forked {args.source_case_id} ({'step_' + f'{args.from_step:02d}' if args.from_step is not None else 'current state'}) "
          f"-> {new_case_id}")
    print(f"  label: {args.label}")
    if args.through_stage is not None or reconstructed:
        mode = (f"reconstructed boundary through {args.reconstruct_through_stage}"
                if reconstructed else f"stage-cut through {args.through_stage}")
        print(f"  mode: {mode} "
              f"(downstream artifacts deliberately absent)")
        print(f"  outputs/ copied ({len(cut_copied)}): {', '.join(cut_copied)}")
        print(f"  outputs/ excluded ({len(cut_excluded)}): "
              f"{', '.join(cut_excluded[:12])}"
              f"{' ...' if len(cut_excluded) > 12 else ''}")
    else:
        print(f"  outputs/: copied, case_id fields rewritten")
    print(f"  data/processed/: copied")
    print(f"  data/raw/: {'copied' if raw_copied else 'not copied (pass --include-raw)'}")
    print(f"  data/ground_truth/: {'copied' if gt_copied else 'not copied (pass --include-ground-truth)'}")
    if warnings:
        print(f"  WARNING: {len(warnings)} file(s) failed schema validation after the case_id rewrite:")
        for w in warnings:
            print(f"    - {w}")
        sys.exit(1)


if __name__ == "__main__":
    main()
