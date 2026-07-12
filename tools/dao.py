"""The DAO -- the sole data-access path for every agent in the harness.

No agent reads/writes outputs/, data/, or ledger/run-state files directly.
Every access goes through one of this CLI's subcommands, so the guardrails in
harness-guardrails (P1/P2/P5/P6/P7/P10) and harness-guardrails-dev (D1/D2)
are enforced structurally rather than relying on an agent remembering the rule.

This tool DOES implement P5's mid-run poll-and-wait loop itself (30s
interval, 15min cap -- LOCK_POLL_INTERVAL_SECONDS/LOCK_MAX_WAIT_SECONDS
below) rather than leaving it to the calling agent. Every lock acquisition
in this file blocks until the lock clears or the cap is hit, at which point
it reports the lock's contents and the caller halts, same outcome P5
always specified -- what changed is who owns the wait. This also closes a
correctness gap, not just a convenience one: read-modify-write subcommands
(add-conflict-entry, set-conflict-verdict, update-run-state,
set-ledger-status) now hold the lock across their entire read+modify+write,
not just the final write, so the read they act on is guaranteed fresh --
nothing else can have modified the file since this call started waiting.

Caveat: this guarantee is specific to those single-call read-modify-write
subcommands. write-contract's data is assembled by the calling agent
*before* the call (via a separate, unlocked read-contract earlier) -- the
agent, not the DAO, is still responsible for re-reading fresh data right
before building what it hands to write-contract. Waiting for the lock
before writing prevents write/write corruption, not a stale read that
already happened outside this call. See known-gaps.md's note on
document_manifest.json for the concrete case this affects.

Likewise this tool does not implement P4's retry-once-then-halt loop --
write-contract makes exactly one write+validate attempt and reports
pass/fail. Retrying means the agent regenerating content, which this tool
cannot do; the orchestrator/agent owns that loop.

Subcommands:
    read-document-text CASE_ID DOC_ID
    read-ground-truth CASE_ID --caller-stage STAGE --version {v1|v2}
    read-contract CASE_ID FILENAME
    write-contract CASE_ID FILENAME --data-file PATH --schema-name NAME
        [--run-id RUN_ID] [--stage STAGE]
    write-page-text CASE_ID DOC_ID PAGE --text-file PATH --held-by NAME --run-id RUN_ID
    write-redacted-text CASE_ID DOC_ID --text-file PATH --held-by NAME --run-id RUN_ID
    write-text CASE_ID FILENAME --text-file PATH --held-by NAME --run-id RUN_ID
    write-reviewed-draft CASE_ID {v1|v2} --text-file PATH --held-by NAME --run-id RUN_ID
    check-lock CASE_ID FILENAME
    read-ledger CASE_ID
    set-ledger-status CASE_ID FILE_NAME STATUS --held-by NAME --run-id RUN_ID
        [--reviewer NAME] [--reason TEXT]
    check-source-ledger-clear CASE_ID
    read-evidence-tags DOC_PATH
    update-run-state CASE_ID RUN_ID STAGE STATUS --held-by NAME
    set-human-input-status CASE_ID STAGE {waiting|received} --held-by NAME --run-id RUN_ID
        [--description TEXT]  (required when status is waiting)
    request-expert-review CASE_ID {v1|v2} --held-by NAME --run-id RUN_ID
    mark-human-review-complete CASE_ID {v1|v2} --reviewer NAME --held-by NAME --run-id RUN_ID
    get-last-passed-stage CASE_ID
    snapshot-backup CASE_ID RUN_ID STAGE --held-by NAME
    read-conflict-ledger CASE_ID
    add-conflict-entry CASE_ID --stage STAGE --topic TOPIC --sources-file PATH
        --held-by NAME --run-id RUN_ID
    set-conflict-verdict CASE_ID CONFLICT_ID VERDICT --note TEXT --held-by NAME --run-id RUN_ID
    check-conflicts-clear CASE_ID
"""
import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from _validation import load_registry, validate_instance

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
DATA = ROOT / "data"
KST = timezone(timedelta(hours=9))


def now_iso() -> str:
    return datetime.now(KST).isoformat()


def case_dir(case_id: str) -> Path:
    d = OUTPUTS / case_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def atomic_write_json(path: Path, obj) -> None:
    """Write to a temp file in the same directory, then atomically replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def processed_dir(case_id: str, doc_id: str) -> Path:
    return DATA / "processed" / case_id / doc_id


# ---------------------------------------------------------------- locking --

def lock_path(target: Path) -> Path:
    return target.with_name(target.name + ".lock")


def read_lock(target: Path):
    lp = lock_path(target)
    return load_json(lp) if lp.exists() else None


def acquire_lock(target: Path, held_by: str, run_id: str, purpose: str):
    """Returns None on success, or the existing lock dict if already held."""
    existing = read_lock(target)
    if existing is not None:
        return existing
    atomic_write_json(lock_path(target), {
        "held_by": held_by, "run_id": run_id, "started_at": now_iso(), "purpose": purpose,
    })
    return None


def release_lock(target: Path) -> None:
    lp = lock_path(target)
    if lp.exists():
        lp.unlink()


# P5's mid-run poll-and-wait cadence -- module-level, not bound into a
# function default, so tests can monkeypatch dao.LOCK_POLL_INTERVAL_SECONDS
# / dao.LOCK_MAX_WAIT_SECONDS to tiny values instead of a test waiting 15
# real minutes to see a timeout.
LOCK_POLL_INTERVAL_SECONDS = 30
LOCK_MAX_WAIT_SECONDS = 900


def acquire_lock_blocking(target: Path, held_by: str, run_id: str, purpose: str):
    """Like acquire_lock, but waits for an existing lock to clear instead of
    failing immediately -- see the module docstring. Returns None on success
    (lock acquired -- everything after this point is reading fresh state,
    nothing else could have written since), or the lock dict still held once
    LOCK_MAX_WAIT_SECONDS is exceeded (same failure contract as acquire_lock).
    """
    waited = 0.0
    while True:
        existing = acquire_lock(target, held_by, run_id, purpose)
        if existing is None:
            return None
        if waited >= LOCK_MAX_WAIT_SECONDS:
            return existing
        time.sleep(LOCK_POLL_INTERVAL_SECONDS)
        waited += LOCK_POLL_INTERVAL_SECONDS


# ------------------------------------------------------------- run-state --

def run_state_path(case_id: str) -> Path:
    return case_dir(case_id) / "_run_state.json"


def load_run_state(case_id: str) -> dict:
    p = run_state_path(case_id)
    existing = load_json(p)
    if existing is not None:
        return existing
    return {"case_id": case_id, "run_id": None, "created_at": now_iso(),
            "updated_at": now_iso(), "stages": [], "human_input_status": []}


def save_run_state(case_id: str, state: dict) -> None:
    state["updated_at"] = now_iso()
    atomic_write_json(run_state_path(case_id), state)


# ------------------------------------------------------------------ nouns --

def cmd_read_document_text(args):
    processed = DATA / "processed" / args.case_id / args.doc_id
    redacted = processed / "redacted_text.md"
    if redacted.exists():
        print(str(redacted))
        return 0
    print(f"NOT_EXTRACTED: {args.doc_id} has no processed text yet. "
          f"Invoke document-pipeline to produce it -- do not read the raw source directly (harness-guardrails P2).")
    return 1


def human_review_flag_path(case_id: str, version: str) -> Path:
    return case_dir(case_id) / f"_human_review_complete_{version}.flag"


def cmd_read_ground_truth(args):
    if args.caller_stage != "evaluation":
        print(f"DENIED: ground truth may only be read by the evaluation stage (harness-guardrails-dev D1). "
              f"caller_stage={args.caller_stage!r} is not permitted. This is logged as a potential violation.")
        return 1
    review_flag = human_review_flag_path(args.case_id, args.version)
    if not review_flag.exists():
        print(f"DENIED: human review is not yet marked complete for {args.version} of this case. "
              f"evaluation may not read ground truth until review is confirmed (D1) -- "
              f"see dao.py mark-human-review-complete.")
        return 1
    gt_dir = DATA / "ground_truth" / args.case_id
    print(str(gt_dir))
    return 0


def cmd_read_contract(args):
    p = case_dir(args.case_id) / args.filename
    if not p.exists():
        print(f"NOT_FOUND: {p}")
        return 1
    print(p.read_text(encoding="utf-8"))
    return 0


def cmd_write_contract(args):
    target = case_dir(args.case_id) / args.filename
    existing_lock = acquire_lock_blocking(target, args.held_by, args.run_id, args.purpose or f"write {args.filename}")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    try:
        data = json.loads(Path(args.data_file).read_text(encoding="utf-8"))
        schemas, registry = load_registry()
        schema_name = args.schema_name
        if schema_name not in schemas:
            print(f"FAIL: no schema named {schema_name} in schemas/")
            return 1
        errors = validate_instance(data, schema_name, schemas, registry)
        if errors:
            print(f"FAIL: schema validation errors for {target}:")
            for e in errors:
                print(f"  - {e}")
            return 1
        atomic_write_json(target, data)
        print(f"PASS: wrote {target}")
        if args.stage:
            # A different target (_run_state.json, not this contract file) --
            # no deadlock risk nesting this inside the contract file's lock.
            state = _update_run_state(args.case_id, args.run_id, args.stage, "passed", args.held_by)
            if state is None:
                print("WARNING: contract write succeeded, but run-state could not be updated (see LOCKED above) -- "
                      "run-state may now lag behind actual progress; retry the run-state update.")
        return 0
    finally:
        release_lock(target)


def cmd_check_lock(args):
    target = case_dir(args.case_id) / args.filename
    lock = read_lock(target)
    if lock is None:
        print(json.dumps({"locked": False}))
    else:
        print(json.dumps({"locked": True, **lock}))
    return 0


def cmd_write_page_text(args):
    """Writes data/processed/CASE_XXX/DOC_XXX/page_NNN.md -- the processed-layer
    write path document-pipeline's OCR checkpoint needs. Plain text, not a JSON
    contract, so this is locked+atomic but not schema-validated."""
    target = processed_dir(args.case_id, args.doc_id) / f"page_{args.page:03d}.md"
    existing_lock = acquire_lock_blocking(target, args.held_by, args.run_id, args.purpose or f"write page {args.page}")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    try:
        text = Path(args.text_file).read_text(encoding="utf-8")
        atomic_write_text(target, text)
        print(f"PASS: wrote {target}")
        return 0
    finally:
        release_lock(target)


def cmd_write_redacted_text(args):
    """Writes data/processed/CASE_XXX/DOC_XXX/redacted_text.md."""
    target = processed_dir(args.case_id, args.doc_id) / "redacted_text.md"
    existing_lock = acquire_lock_blocking(target, args.held_by, args.run_id, args.purpose or "write redacted text")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    try:
        text = Path(args.text_file).read_text(encoding="utf-8")
        atomic_write_text(target, text)
        print(f"PASS: wrote {target}")
        return 0
    finally:
        release_lock(target)


def _write_text_locked(case_id, filename, text_file, held_by, run_id, purpose=None):
    """Locked+atomic text write to outputs/CASE_XXX/FILENAME -- no schema
    validation, since there's nothing to validate a free-form text file
    against. Shared by cmd_write_text and cmd_write_reviewed_draft, same
    pattern as _update_run_state being shared by cmd_update_run_state and
    cmd_snapshot_backup."""
    target = case_dir(case_id) / filename
    existing_lock = acquire_lock_blocking(target, held_by, run_id, purpose or f"write {filename}")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    try:
        text = Path(text_file).read_text(encoding="utf-8")
        atomic_write_text(target, text)
        print(f"PASS: wrote {target}")
        return 0
    finally:
        release_lock(target)


def cmd_write_text(args):
    """Generic locked+atomic text write to outputs/CASE_XXX/FILENAME.
    Symmetric to write-contract's arbitrary-filename JSON write, for a
    free-form text artifact instead (e.g. an annotated document -- there's
    nothing to schema-validate)."""
    return _write_text_locked(args.case_id, args.filename, args.text_file, args.held_by, args.run_id, args.purpose)


def cmd_write_reviewed_draft(args):
    """Purpose-built wrapper around _write_text_locked for critic's
    annotated draft_report_v{version}_reviewed.md -- keeps that filename
    convention defined in exactly one place rather than every caller
    constructing it by hand."""
    if args.version not in ("v1", "v2"):
        sys.exit(f"error: version must be v1 or v2 -- got {args.version!r}")
    filename = f"draft_report_{args.version}_reviewed.md"
    return _write_text_locked(args.case_id, filename, args.text_file, args.held_by, args.run_id, args.purpose)


# ------------------------------------------------------------ src ledger --

def source_ledger_path(case_id: str) -> Path:
    return case_dir(case_id) / "_source_ledger.json"


def cmd_read_ledger(args):
    p = source_ledger_path(args.case_id)
    if not p.exists():
        print(f"NOT_FOUND: {p}")
        return 1
    print(p.read_text(encoding="utf-8"))
    return 0


def cmd_set_ledger_status(args):
    p = source_ledger_path(args.case_id)
    existing_lock = acquire_lock_blocking(p, args.held_by, args.run_id, f"set-ledger-status {args.file_name} -> {args.status}")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    try:
        ledger = load_json(p)
        if ledger is None:
            print(f"NOT_FOUND: {p}")
            return 1
        if args.status == "approved" and not args.reviewer:
            print("ERROR: --reviewer is required to set status approved")
            return 1
        if args.status == "rejected" and not args.reason:
            print("ERROR: --reason is required to set status rejected")
            return 1
        found = False
        for entry in ledger["files"]:
            if entry["file_name"] == args.file_name:
                entry["review_status"] = args.status
                entry["reviewed_by"] = args.reviewer
                entry["reviewed_at"] = now_iso()
                entry["rejection_reason"] = args.reason if args.status == "rejected" else None
                found = True
                break
        if not found:
            print(f"NOT_FOUND: no entry for file {args.file_name!r} in ledger")
            return 1
        ledger["updated_at"] = now_iso()
        atomic_write_json(p, ledger)
        print(f"OK: {args.file_name} -> {args.status}")
        return 0
    finally:
        release_lock(p)


def cmd_check_source_ledger_clear(args):
    ledger = load_json(source_ledger_path(args.case_id))
    if ledger is None:
        print(json.dumps({"clear": False, "error": "ledger not found"}))
        return 1
    pending = [e["file_name"] for e in ledger["files"] if e["review_status"] == "pending"]
    rejected = [e["file_name"] for e in ledger["files"] if e["review_status"] == "rejected"]
    clear = not pending and not rejected
    print(json.dumps({"clear": clear, "pending": pending, "rejected": rejected}))
    return 0 if clear else 1


# --------------------------------------------------------- evidence tags --

TAG_RE = re.compile(r"\[E(\d+)\]")


def cmd_read_evidence_tags(args):
    doc_path = Path(args.doc_path)
    sidecar_path = doc_path.with_suffix(".evidence.json")
    if not doc_path.exists():
        print(f"NOT_FOUND: {doc_path}")
        return 1
    text = doc_path.read_text(encoding="utf-8")
    tags_in_doc = {f"E{m}" for m in TAG_RE.findall(text)}
    sidecar = load_json(sidecar_path) or {"citations": []}
    tags_in_sidecar = {c["tag"] for c in sidecar.get("citations", [])}
    orphaned = sorted(tags_in_doc - tags_in_sidecar)
    unused = sorted(tags_in_sidecar - tags_in_doc)
    ok = not orphaned and not unused
    print(json.dumps({"consistent": ok, "orphaned_tags": orphaned, "unused_citations": unused}))
    return 0 if ok else 1


# ---------------------------------------------------------------- run state ops --

def _update_run_state(case_id, run_id, stage, status, held_by, backup_path=None):
    """Holds the run-state lock across the whole read+modify+write, not just
    the write -- see acquire_lock_blocking. Returns the updated state on
    success, or None if the lock never cleared (caller reports and halts)."""
    target = run_state_path(case_id)
    existing_lock = acquire_lock_blocking(target, held_by, run_id or "unknown", f"update run-state: {stage} -> {status}")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return None
    try:
        state = load_run_state(case_id)
        state["run_id"] = run_id or state.get("run_id")
        stages = state["stages"]
        entry = next((s for s in stages if s["stage_name"] == stage), None)
        if entry is None:
            entry = {"stage_name": stage, "status": "pending", "started_at": None,
                      "completed_at": None, "attempt_count": 0, "backup_path": None}
            stages.append(entry)
        if status == "in_progress":
            entry["started_at"] = entry["started_at"] or now_iso()
            entry["attempt_count"] += 1
        if status in ("passed", "failed"):
            entry["completed_at"] = now_iso()
        entry["status"] = status
        if backup_path:
            entry["backup_path"] = backup_path
        save_run_state(case_id, state)
        return state
    finally:
        release_lock(target)


def cmd_update_run_state(args):
    state = _update_run_state(args.case_id, args.run_id, args.stage, args.status, args.held_by)
    if state is None:
        return 1
    print(f"OK: {args.stage} -> {args.status}")
    return 0


def _set_human_input_status(case_id, stage, status, description, held_by, run_id):
    """P7's human-input wait tracking, in _run_state.json's human_input_status
    array. Holds the run-state lock across the whole read+modify+write, same
    discipline as _update_run_state. Entries are never deleted -- 'waiting'
    appends a new entry, 'received' finds and updates the most recent
    matching 'waiting' entry in place, so the full history of what was
    waited on stays visible (P7)."""
    target = run_state_path(case_id)
    existing_lock = acquire_lock_blocking(target, held_by, run_id or "unknown", f"set human_input_status: {stage} -> {status}")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    try:
        state = load_run_state(case_id)
        entries = state.setdefault("human_input_status", [])
        if status == "waiting":
            if not description:
                print("ERROR: a description is required when status is waiting")
                return 1
            entries.append({
                "stage_name": stage, "status": "waiting",
                "description": description, "requested_at": now_iso(), "received_at": None,
            })
        else:  # received
            entry = next((e for e in reversed(entries) if e["stage_name"] == stage and e["status"] == "waiting"), None)
            if entry is None:
                print(f"NOT_FOUND: no 'waiting' human_input_status entry for stage {stage!r}")
                return 1
            entry["status"] = "received"
            entry["received_at"] = now_iso()
        save_run_state(case_id, state)
        print(f"OK: {stage} -> {status}")
        return 0
    finally:
        release_lock(target)


def cmd_set_human_input_status(args):
    """Generic write path for P7 -- see harness-guardrails P7. Usable by any
    stage that needs to wait on a human, not just the critic->evaluation
    handoff (that handoff has its own narrow wrapper, request-expert-review,
    built on this)."""
    return _set_human_input_status(args.case_id, args.stage, args.status, args.description, args.held_by, args.run_id)


def cmd_request_expert_review(args):
    """Purpose-built wrapper around set-human-input-status for the
    critic -> human review -> evaluation handoff. stage_name is
    'evaluation' -- that's the stage actually blocked/pending, matching P7's
    'naming exactly which stage... is pending.' Keeps the description
    convention defined in one place rather than every caller constructing
    it by hand."""
    description = f"expert review of draft_report_{args.version}_reviewed.md"
    return _set_human_input_status(args.case_id, "evaluation", "waiting", description, args.held_by, args.run_id)


def cmd_mark_human_review_complete(args):
    """Creates the versioned D1 gate (_human_review_complete_v{version}.flag)
    that read-ground-truth checks -- the actual mechanism letting evaluation
    access ground truth for that version. Requires expert_review_v{version}.json
    to already exist and pass schema validation first: you cannot claim
    review is complete without real recorded review content backing it --
    an actor self-certifying "reviewed" without real evidence is exactly the
    CASE_002 failure shape (see known-gaps.md item 2), just at a different
    gate. --reviewer is required for the same accountability reason
    set-ledger-status's approved status requires one."""
    expert_review_path = case_dir(args.case_id) / f"expert_review_{args.version}.json"
    data = load_json(expert_review_path)
    if data is None:
        print(f"BLOCKED: {expert_review_path} does not exist yet -- write it first "
              f"(the transcribed human review content, via write-contract) before marking review complete.")
        return 1
    schemas, registry = load_registry()
    errors = validate_instance(data, "expert_review.schema.json", schemas, registry)
    if errors:
        print(f"BLOCKED: {expert_review_path} exists but fails its own schema validation -- fix it first:")
        for e in errors:
            print(f"  - {e}")
        return 1

    target = human_review_flag_path(args.case_id, args.version)
    existing_lock = acquire_lock_blocking(target, args.held_by, args.run_id, f"mark human review complete ({args.version})")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    try:
        atomic_write_json(target, {
            "case_id": args.case_id, "version": args.version, "reviewer": args.reviewer,
            "marked_complete_at": now_iso(),
        })
    finally:
        release_lock(target)

    status_rc = _set_human_input_status(args.case_id, "evaluation", "received", None, args.held_by, args.run_id)
    if status_rc != 0:
        print("note: no matching 'waiting' human_input_status entry was found to flip to 'received' -- "
              "the flag was still created (that's the actual D1 gate), but the wait-tracking history "
              "is incomplete for this version.")
    print(f"OK: {target} created -- evaluation may now read ground truth for {args.version} (D1 exception unlocked).")
    return 0


def cmd_get_last_passed_stage(args):
    state = load_run_state(args.case_id)
    passed = [s["stage_name"] for s in state["stages"] if s["status"] == "passed"]
    print(passed[-1] if passed else "NONE")
    return 0


def cmd_snapshot_backup(args):
    src = case_dir(args.case_id)
    n = len([s for s in load_run_state(args.case_id)["stages"] if s.get("backup_path")]) + 1
    dest = src / "_backups" / f"step_{n:02d}_{args.stage}"
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in ("_backups",) or item.name.endswith(".lock"):
            continue
        if item.is_file():
            shutil.copy2(item, dest / item.name)
        elif item.is_dir():
            shutil.copytree(item, dest / item.name, dirs_exist_ok=True)
    state = _update_run_state(args.case_id, args.run_id, args.stage, "passed", args.held_by, backup_path=str(dest))
    if state is None:
        print(f"PARTIAL: snapshot written at {dest}, but run-state could not be updated (see LOCKED above) -- retry the run-state update.")
        return 1
    print(f"OK: snapshot at {dest}")
    return 0


# ------------------------------------------------------------ conflict ledger --

def conflict_ledger_path(case_id: str) -> Path:
    return case_dir(case_id) / "_conflict_ledger.json"


def load_conflict_ledger(case_id: str) -> dict:
    existing = load_json(conflict_ledger_path(case_id))
    if existing is not None:
        return existing
    return {"case_id": case_id, "created_at": now_iso(), "updated_at": now_iso(), "conflicts": []}


def cmd_read_conflict_ledger(args):
    print(json.dumps(load_conflict_ledger(args.case_id), ensure_ascii=False, indent=2))
    return 0


def cmd_add_conflict_entry(args):
    """Locked across the whole read+modify+write -- not just tidiness: `n`
    below is derived from the current conflicts list length, so two
    concurrent unlocked calls could both read the same length and both mint
    CONFLICT_1, colliding. Holding the lock through the read makes that
    structurally impossible, not just unlikely."""
    target = conflict_ledger_path(args.case_id)
    existing_lock = acquire_lock_blocking(target, args.held_by, args.run_id, f"add conflict entry ({args.topic})")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    try:
        ledger = load_conflict_ledger(args.case_id)
        sources = json.loads(Path(args.sources_file).read_text(encoding="utf-8"))
        n = len(ledger["conflicts"]) + 1
        ledger["conflicts"].append({
            "conflict_id": f"CONFLICT_{n}",
            "raised_by_stage": args.stage,
            "field_or_topic": args.topic,
            "sources": sources,
            "verdict": "pending",
            "resolution_note": None,
            "resolved_at": None,
        })
        ledger["updated_at"] = now_iso()
        atomic_write_json(target, ledger)
        print(f"OK: added CONFLICT_{n}")
        return 0
    finally:
        release_lock(target)


def cmd_set_conflict_verdict(args):
    target = conflict_ledger_path(args.case_id)
    existing_lock = acquire_lock_blocking(target, args.held_by, args.run_id, f"set verdict on {args.conflict_id}")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    try:
        ledger = load_conflict_ledger(args.case_id)
        entry = next((c for c in ledger["conflicts"] if c["conflict_id"] == args.conflict_id), None)
        if entry is None:
            print(f"NOT_FOUND: {args.conflict_id}")
            return 1
        entry["verdict"] = args.verdict
        entry["resolution_note"] = args.note
        entry["resolved_at"] = now_iso()
        ledger["updated_at"] = now_iso()
        atomic_write_json(target, ledger)
        print(f"OK: {args.conflict_id} -> {args.verdict}")
        return 0
    finally:
        release_lock(target)


def cmd_check_conflicts_clear(args):
    ledger = load_conflict_ledger(args.case_id)
    pending = [c["conflict_id"] for c in ledger["conflicts"] if c["verdict"] == "pending"]
    clear = not pending
    print(json.dumps({"clear": clear, "pending": pending}))
    return 0 if clear else 1


# ------------------------------------------------------------------- main --

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("read-document-text"); p.add_argument("case_id"); p.add_argument("doc_id")
    p.set_defaults(fn=cmd_read_document_text)

    p = sub.add_parser("read-ground-truth"); p.add_argument("case_id"); p.add_argument("--caller-stage", required=True)
    p.add_argument("--version", required=True, choices=["v1", "v2"])
    p.set_defaults(fn=cmd_read_ground_truth)

    p = sub.add_parser("read-contract"); p.add_argument("case_id"); p.add_argument("filename")
    p.set_defaults(fn=cmd_read_contract)

    p = sub.add_parser("write-contract")
    p.add_argument("case_id"); p.add_argument("filename")
    p.add_argument("--data-file", required=True); p.add_argument("--schema-name", required=True)
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.add_argument("--purpose"); p.add_argument("--stage")
    p.set_defaults(fn=cmd_write_contract)

    p = sub.add_parser("write-page-text")
    p.add_argument("case_id"); p.add_argument("doc_id"); p.add_argument("page", type=int)
    p.add_argument("--text-file", required=True)
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True); p.add_argument("--purpose")
    p.set_defaults(fn=cmd_write_page_text)

    p = sub.add_parser("write-redacted-text")
    p.add_argument("case_id"); p.add_argument("doc_id")
    p.add_argument("--text-file", required=True)
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True); p.add_argument("--purpose")
    p.set_defaults(fn=cmd_write_redacted_text)

    p = sub.add_parser("write-text")
    p.add_argument("case_id"); p.add_argument("filename")
    p.add_argument("--text-file", required=True)
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True); p.add_argument("--purpose")
    p.set_defaults(fn=cmd_write_text)

    p = sub.add_parser("write-reviewed-draft")
    p.add_argument("case_id"); p.add_argument("version", choices=["v1", "v2"])
    p.add_argument("--text-file", required=True)
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True); p.add_argument("--purpose")
    p.set_defaults(fn=cmd_write_reviewed_draft)

    p = sub.add_parser("check-lock"); p.add_argument("case_id"); p.add_argument("filename")
    p.set_defaults(fn=cmd_check_lock)

    p = sub.add_parser("read-ledger"); p.add_argument("case_id")
    p.set_defaults(fn=cmd_read_ledger)

    p = sub.add_parser("set-ledger-status")
    p.add_argument("case_id"); p.add_argument("file_name"); p.add_argument("status", choices=["pending", "approved", "rejected"])
    p.add_argument("--reviewer"); p.add_argument("--reason")
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_set_ledger_status)

    p = sub.add_parser("check-source-ledger-clear"); p.add_argument("case_id")
    p.set_defaults(fn=cmd_check_source_ledger_clear)

    p = sub.add_parser("read-evidence-tags"); p.add_argument("doc_path")
    p.set_defaults(fn=cmd_read_evidence_tags)

    p = sub.add_parser("update-run-state")
    p.add_argument("case_id"); p.add_argument("run_id"); p.add_argument("stage")
    p.add_argument("status", choices=["pending", "in_progress", "passed", "failed"])
    p.add_argument("--held-by", required=True)
    p.set_defaults(fn=cmd_update_run_state)

    p = sub.add_parser("set-human-input-status")
    p.add_argument("case_id"); p.add_argument("stage")
    p.add_argument("status", choices=["waiting", "received"])
    p.add_argument("--description")
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_set_human_input_status)

    p = sub.add_parser("request-expert-review")
    p.add_argument("case_id"); p.add_argument("version", choices=["v1", "v2"])
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_request_expert_review)

    p = sub.add_parser("mark-human-review-complete")
    p.add_argument("case_id"); p.add_argument("version", choices=["v1", "v2"])
    p.add_argument("--reviewer", required=True)
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_mark_human_review_complete)

    p = sub.add_parser("get-last-passed-stage"); p.add_argument("case_id")
    p.set_defaults(fn=cmd_get_last_passed_stage)

    p = sub.add_parser("snapshot-backup")
    p.add_argument("case_id"); p.add_argument("run_id"); p.add_argument("stage")
    p.add_argument("--held-by", required=True)
    p.set_defaults(fn=cmd_snapshot_backup)

    p = sub.add_parser("read-conflict-ledger"); p.add_argument("case_id")
    p.set_defaults(fn=cmd_read_conflict_ledger)

    p = sub.add_parser("add-conflict-entry")
    p.add_argument("case_id"); p.add_argument("--stage", required=True)
    p.add_argument("--topic", required=True); p.add_argument("--sources-file", required=True)
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_add_conflict_entry)

    p = sub.add_parser("set-conflict-verdict")
    p.add_argument("case_id"); p.add_argument("conflict_id")
    p.add_argument("verdict", choices=["resolved", "false_positive"]); p.add_argument("--note", required=True)
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_set_conflict_verdict)

    p = sub.add_parser("check-conflicts-clear"); p.add_argument("case_id")
    p.set_defaults(fn=cmd_check_conflicts_clear)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
