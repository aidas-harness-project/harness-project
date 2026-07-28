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
subcommands, PLUS patch-manifest-document below (added specifically to
extend it to document_manifest.json, the one file this bit the hardest --
see known-gaps.md item 7). Every OTHER write-contract target still has its
data assembled by the calling agent *before* the call (via a separate,
unlocked read-contract earlier) -- the agent, not the DAO, is still
responsible for re-reading fresh data right before building what it hands
to write-contract for those files. Waiting for the lock before writing
prevents write/write corruption, not a stale read that already happened
outside this call.

Likewise this tool does not implement P4's retry-once-then-halt loop --
write-contract makes exactly one write+validate attempt and reports
pass/fail. Retrying means the agent regenerating content, which this tool
cannot do; the orchestrator/agent owns that loop.

Subcommands:
    read-document-text CASE_ID DOC_ID
    read-page-text CASE_ID DOC_ID PAGE
    read-ground-truth CASE_ID --caller-stage STAGE --version {v1|v2}
    read-contract CASE_ID FILENAME
    write-contract CASE_ID FILENAME --data-file PATH --schema-name NAME
        [--run-id RUN_ID] [--stage STAGE]
    patch-manifest-document CASE_ID DOC_ID --fields-file PATH --held-by NAME --run-id RUN_ID
        [--stage STAGE]
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
    check-forbidden-expressions DOC_PATH
    update-run-state CASE_ID RUN_ID STAGE STATUS --held-by NAME
        (STATUS in pending|in_progress|failed|skipped; 'passed' is refused here --
         a stage passes only via finalize-stage, atomically with its snapshot)
    migrate-run-state-v03 CASE_ID RUN_ID --held-by NAME
        (audited one-time repair for legacy dependency-invalid or
         passed-without-backup entries; invalid passes are downgraded, never
         given fabricated backups)
    finalize-stage CASE_ID RUN_ID STAGE --held-by NAME
        (dependency-checked; builds+validates the P10 snapshot, then records
         status=passed + backup_path + completed_at in one run-state lock)
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
    read-human-review-ledger CASE_ID
    record-human-review CASE_ID --artifact-kind {policy_audit_finding|unpaged_physical_exclusion}
        --artifact-id DOC_ID --target-key KEY --decision {accepted_risk|verified|rejected}
        --reviewer NAME --note TEXT --held-by NAME --run-id RUN_ID
        (the DAO-only path for a genuine human decision on a policy artifact;
         computes the reviewed artifact's canonical hash itself and returns an
         HR- review UID the artifact then references -- a machine-written
         contract cannot self-declare a human decision with a name string)
    policy-snapshot CASE_ID --document-id DOC_ID [--document-id DOC_ID ...]
        (prints the upstream_policy_snapshot a policy-referencing contract must
         carry; the DAO recomputes and re-verifies it at write time)
"""
import argparse
import json
import os
import hashlib
import re
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from _validation import load_registry, validate_instance
import _cross_contract
import dao_transaction
import source_provenance
import stage_dependencies
import segment_lineage
import policy_completeness
import policy_uid
import policy_uid_resolver
import policy_audit
import policy_roles
import human_review

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
DATA = ROOT / "data"
FORBIDDEN_TEMPLATE = ROOT / "templates" / "forbidden-expressions.md"
KST = timezone(timedelta(hours=9))


def now_iso() -> str:
    return datetime.now(KST).isoformat()


# --- path-safety choke point (closes traversal via case_id/doc_id/filename) ---
# The DAO is documented as the sole safe boundary keeping callers inside
# outputs/ and data/. That is only true if a crafted case_id/doc_id/filename
# cannot escape the tree. Every case-scoped path is built through these guards.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _require_safe_id(kind: str, value: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        sys.exit(f"error: unsafe {kind} {value!r} -- must be [A-Za-z0-9_]+ with no path separators")
    return value


def _require_within(base: Path, *parts: str) -> Path:
    """Join parts under base and refuse anything that escapes it (traversal)."""
    for p in parts:
        if not isinstance(p, str) or not p or "\x00" in p or Path(p).is_absolute():
            sys.exit(f"error: unsafe path component {p!r}")
    candidate = base.joinpath(*parts)
    base_r = base.resolve()
    cand_r = candidate.resolve()
    if cand_r != base_r and base_r not in cand_r.parents:
        sys.exit(f"error: path escapes {base} -- refusing {candidate}")
    return candidate


def case_dir(case_id: str) -> Path:
    _require_safe_id("case_id", case_id)
    d = _require_within(OUTPUTS, case_id)
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
    _require_safe_id("case_id", case_id)
    _require_safe_id("doc_id", doc_id)
    return _require_within(DATA / "processed", case_id, doc_id)


# ---------------------------------------------------------------- locking --

def lock_path(target: Path) -> Path:
    return target.with_name(target.name + ".lock")


def read_lock(target: Path):
    lp = lock_path(target)
    if not lp.exists():
        return None
    try:
        return load_json(lp)
    except FileNotFoundError:
        # The holder released between exists() and read_text(). The blocking
        # acquire loop will immediately retry the atomic O_EXCL create.
        return None
    except (json.JSONDecodeError, ValueError):
        # A lock created by O_EXCL but not yet content-filled (tiny race window):
        # it IS held, we just can't read who by yet. Report a placeholder rather
        # than crash or treat it as free.
        return {"held_by": "unknown", "run_id": "unknown", "purpose": "lock being written"}


def acquire_lock(target: Path, held_by: str, run_id: str, purpose: str):
    """Returns None on success, or the existing lock dict if already held.

    Uses an atomic O_CREAT|O_EXCL create so two racing callers cannot both
    observe 'no lock' and both acquire it (the prior read-then-write was TOCTOU
    -- fleet review proved 5 processes acquiring one lock). Exactly one caller's
    create succeeds; every other gets FileExistsError and reports the holder."""
    lp = lock_path(target)
    lp.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return read_lock(target) or {"held_by": "unknown", "run_id": "unknown",
                                     "purpose": "already held"}
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"held_by": held_by, "run_id": run_id,
                   "started_at": now_iso(), "purpose": purpose}, f, ensure_ascii=False, indent=2)
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
    manifest = read_contract_data(args.case_id, "document_manifest.json")
    if manifest is not None:
        entry = next(
            (item for item in manifest.get("documents", []) if item.get("document_id") == args.doc_id),
            None,
        )
        if entry and entry.get("downstream_disposition") == "expert_review_only":
            print(
                f"NON_TEXT_EXPERT_REVIEW_ONLY: {args.doc_id} is human-verified non-text visual evidence. "
                "No processed text exists and automated downstream use is prohibited."
            )
            return 1
    processed = DATA / "processed" / args.case_id / args.doc_id
    redacted = processed / "redacted_text.md"
    if redacted.exists():
        print(str(redacted))
        return 0
    print(f"NOT_EXTRACTED: {args.doc_id} has no processed text yet. "
          f"Invoke document-pipeline to produce it -- do not read the raw source directly (harness-guardrails P2).")
    return 1


def cmd_read_page_text(args):
    """Read one validated checkpoint-1 page from the processed layer.

    This command exists for checkpoint 2 so redaction never opens a raw
    source or reaches into data/processed outside the DAO boundary.
    """
    if args.page < 1:
        print(f"ERROR: page must be >= 1 (got {args.page})")
        return 1
    page_path = processed_dir(args.case_id, args.doc_id) / f"page_{args.page:03d}.md"
    if not page_path.exists():
        print(f"NOT_EXTRACTED: {args.doc_id} page {args.page} has no validated processed text yet. "
              "Complete document-pipeline checkpoint 1 first.")
        return 1
    print(page_path.read_text(encoding="utf-8"), end="")
    return 0


def human_review_flag_path(case_id: str, version: str) -> Path:
    return case_dir(case_id) / f"_human_review_complete_{version}.flag"


def human_review_complete_any(case_id: str) -> bool:
    """Whether ANY draft version's D1 human-review gate has been marked
    complete (Part 11I).

    `evaluation` is one canonical run-state stage covering both the v1 and v2
    comparison, so the run-state gate asks the weaker question -- "has a real
    reviewed draft been signed off at all" -- while `read-ground-truth` keeps
    asking the strict per-version question. Both must hold for evaluation to
    do anything: this one lets the stage START, that one lets it READ.

    Only versions that mark-human-review-complete actually produces count; the
    flag itself is only writable after expert_review_{version}.json exists and
    passes its schema, so this cannot be satisfied by an empty file dropped in
    by hand under some invented version name.
    """
    directory = case_dir(case_id)
    if not directory.is_dir():
        return False
    return any(
        human_review_flag_path(case_id, version).exists()
        for version in ("v1", "v2")
    )


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
    p = _require_within(case_dir(args.case_id), args.filename)
    if not p.exists():
        print(f"NOT_FOUND: {p}")
        return 1
    print(p.read_text(encoding="utf-8"))
    return 0


def read_contract_data(case_id: str, filename: str):
    """DAO-owned structured contract read for in-process pipeline tools."""
    p = _require_within(case_dir(case_id), filename)
    return load_json(p)


def _redacted_text_for_doc(case_id, doc_id):
    """The processed/redacted policy text a normalized_policy_clause file claims
    to quote. None when the document was never processed -- the cross-contract
    check turns that into SourceUnavailable, never a clean pass."""
    if not doc_id:
        return None
    redacted = processed_dir(case_id, doc_id) / "redacted_text.md"
    if not redacted.exists():
        return None
    return redacted.read_text(encoding="utf-8")


def _contract_sha256(case_id: str, filename: str) -> str | None:
    path = _require_within(case_dir(case_id), filename)
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_entry_sha256(case_id: str, doc_id: str) -> str | None:
    """Digest of one document's manifest entry, page_map included.

    An audit is bound to the logical->physical mapping its provenance is
    checked against (Part 11F): if a segment's page_map is edited, every
    physical-page claim downstream of it changes meaning, so the audit must go
    stale even though no policy contract was touched. Serialised with sorted
    keys so key ordering can never make an unchanged entry look changed.
    """
    manifest = read_contract_data(case_id, "document_manifest.json")
    if manifest is None:
        return None
    entry = next(
        (d for d in manifest.get("documents", [])
         if d.get("document_id") == doc_id), None)
    if entry is None:
        return None
    encoded = json.dumps(
        entry, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parent_coverage_name_for(case_id: str, doc_id: str) -> str | None:
    """The parent-coverage contract filename governing this document, if any:
    the document's own when it is a physical parent, otherwise its parent's."""
    manifest = read_contract_data(case_id, "document_manifest.json")
    if manifest is None:
        return None
    entry = next(
        (d for d in manifest.get("documents", [])
         if d.get("document_id") == doc_id), None)
    if entry is None:
        return None
    parent_id = (
        entry.get("source_document_id")
        if entry.get("document_role") == "segment" else doc_id)
    if not parent_id:
        return None
    return f"policy_parent_coverage_{parent_id}.json"


def _policy_audit_context(case_id: str, doc_id: str):
    normalized_name = f"normalized_policy_clause_{doc_id}.json"
    inventory_name = f"policy_boundary_inventory_{doc_id}.json"
    reference_name = f"reference_table_{doc_id}.json"
    normalized = read_contract_data(case_id, normalized_name)
    inventory = read_contract_data(case_id, inventory_name)
    reference = read_contract_data(case_id, reference_name)
    # The audit binds to its SOURCES too, not only the contracts derived from
    # them (Part 11F). Boundary offsets and evidence quotes are expressed
    # against exact processed bytes and an exact page_map; a change to either
    # invalidates conclusions drawn from them even if no contract was rewritten.
    source_text = _redacted_text_for_doc(case_id, doc_id)
    coverage_name = _parent_coverage_name_for(case_id, doc_id)
    hashes = {
        "normalized_sha256": _contract_sha256(case_id, normalized_name),
        "inventory_sha256": _contract_sha256(case_id, inventory_name),
        "reference_table_sha256": _contract_sha256(case_id, reference_name),
        "source_text_sha256": (
            hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            if source_text is not None else None),
        "manifest_entry_sha256": _manifest_entry_sha256(case_id, doc_id),
        "parent_coverage_sha256": (
            _contract_sha256(case_id, coverage_name) if coverage_name else None),
    }
    return hashes, normalized, inventory, reference


def _segment_text_reader(case_id):
    """Reader closure segment_lineage.validate_segment_lineage needs: given a
    document_id, return that document's derived/redacted processed text, or
    None. Uses the standard processed path (data/processed/CASE/DOC/redacted_text.md),
    which is where a segment's derived_text_path points."""
    def _read(document_id):
        return _redacted_text_for_doc(case_id, document_id)
    return _read


def _classification_reader(case_id):
    """Reader closure for classification/manifest consistency: given a
    document_id, return its classification_result_{id}.json dict, or None."""
    def _read(document_id):
        return read_contract_data(case_id, f"classification_result_{document_id}.json")
    return _read


def _validate_manifest_lineage(case_id, manifest) -> list:
    """Segment-lineage errors for a manifest about to be written. Empty = clean."""
    return segment_lineage.validate_segment_lineage(manifest, _segment_text_reader(case_id))


def _canonical_uid_finalize_blockers(case_id: str, doc_id: str) -> list[str]:
    """Re-recompute every canonical UID of a document at finalization.

    Same reasoning as Part 11F's re-run of the source checks: a contract whose
    UIDs verified at write time can be invalidated afterwards by a source-text
    revision, and finalization is where the stage's claims become citable by
    downstream stages. Write-time verification proves the UIDs were canonical
    once; only this proves they still are.
    """
    if uid_scheme_for(case_id, doc_id) != "canonical_v1":
        return []
    blockers: list[str] = []
    for filename, schema_name in (
        (f"policy_boundary_inventory_{doc_id}.json",
         policy_completeness.INVENTORY_SCHEMA),
        (f"normalized_policy_clause_{doc_id}.json",
         _cross_contract.NORMALIZED_POLICY_CLAUSE_SCHEMA),
        (f"reference_table_{doc_id}.json",
         _cross_contract.REFERENCE_TABLE_SCHEMA),
        (f"policy_audit_result_{doc_id}.json", policy_audit.AUDIT_SCHEMA),
        (f"policy_parent_coverage_{doc_id}.json",
         policy_completeness.PARENT_COVERAGE_SCHEMA),
    ):
        contract = read_contract_data(case_id, filename)
        if contract is None:
            # Whether a contract is OWED is decided by policy_roles above; an
            # absent one is not this check's business.
            continue
        blockers.extend(
            f"canonical UID: {filename}: {error}"
            for error in _canonical_uid_errors(
                case_id, filename, schema_name, contract))
    return blockers


def _automated_policy_documents(manifest: dict) -> list[dict]:
    """The policy documents this pipeline processes automatically.

    One definition, used by both the completion gate and the P0-3 scheme gate,
    so a document cannot be in scope for one and out of scope for the other.
    """
    return [
        d for d in (manifest or {}).get("documents", [])
        if (
            d.get("document_type") == "insurance_policy"
            and d.get("downstream_disposition") == "automated_text_pipeline"
        )
    ]


def _policy_layer_document_ids(case_id: str) -> set[str]:
    """Every document whose UID verification the policy layer stands on.

    The manifest's automated policy documents, plus any further document a
    parent-coverage contract accounts for pages of. The second part matters
    because a segmented_parent's coverage can cite an owning segment that the
    manifest's own filter might not reach the same way -- and a parent whose
    coverage rests on an unverified segment is not a verified parent.
    """
    manifest = read_contract_data(case_id, "document_manifest.json")
    doc_ids = {
        d.get("document_id") for d in _automated_policy_documents(manifest)}
    for doc_id in list(doc_ids):
        coverage = read_contract_data(
            case_id, f"policy_parent_coverage_{doc_id}.json")
        for page in (coverage or {}).get("pages") or []:
            if not isinstance(page, dict):
                continue
            for key in ("owning_document_id", "owner_document_id",
                        "reference_table_document_id"):
                if page.get(key):
                    doc_ids.add(page[key])
    return {d for d in doc_ids if d}


def _policy_layer_scheme_blockers(case_id: str, action: str) -> list[str]:
    """P0-3: the whole policy layer must be canonical_v1 for `action`.

    Used by policy_clause_processing's completion gate and, transitively, by
    every stage downstream of it. A manifest with no automated policy document
    is not silently clear here -- an empty scope would mean "nothing to verify,
    therefore verified", which is the exact shape of the defect this closes.
    """
    manifest = read_contract_data(case_id, "document_manifest.json")
    if manifest is None:
        return ["document_manifest.json is missing -- the policy documents "
                f"{action} depends on cannot be enumerated, so their UID "
                "verification state cannot be established"]
    doc_ids = _policy_layer_document_ids(case_id)
    if not doc_ids:
        return ["no automated insurance_policy document is registered -- "
                f"{action} cannot rest on a policy layer that does not exist"]
    return _canonical_state_blockers(case_id, doc_ids, action)


def _policy_completion_blockers(case_id: str) -> list[str]:
    """Return every condition that prevents policy_clause_processing finalize.

    Persistence-time checks protect each inventory write. This final gate
    additionally requires one normalized contract and one fully-accounted
    inventory for every automated policy source, and rejects unresolved
    review/extraction boundaries.
    """
    blockers = []
    manifest = read_contract_data(case_id, "document_manifest.json")
    if manifest is None:
        return ["document_manifest.json is missing"]
    policy_docs = _automated_policy_documents(manifest)
    if not policy_docs:
        return ["no automated insurance_policy document is registered"]

    schemas, registry = load_registry()
    # What each policy document owes is DECLARED (policy_processing_role) and
    # verified against its real source and real children -- never inferred from
    # which artifacts happen to exist (Part 11H). The old inference read "no
    # clause contract was written" as "no clause contract is owed", so skipping
    # normalization was self-exempting.
    blockers.extend(policy_roles.check_policy_processing_roles(
        manifest,
        normalized_for=lambda d: read_contract_data(
            case_id, f"normalized_policy_clause_{d}.json"),
        reference_table_for=lambda d: read_contract_data(
            case_id, f"reference_table_{d}.json"),
        redacted_text_for=lambda d: _redacted_text_for_doc(case_id, d),
        parent_coverage_for=lambda d: read_contract_data(
            case_id, f"policy_parent_coverage_{d}.json"),
    ))

    # P0-3: every automated policy document in the case must be canonical_v1
    # before the stage may finalize -- not merely the ones that happen to carry
    # a contract. Driven by the manifest's own document set, so each structural
    # shape is covered uniformly: a plain clause document, a
    # reference_table_only segment, a segmented_parent, and the segments a
    # parent's coverage accounts for. A mixed set (one canonical, one legacy)
    # fails on the legacy member, so the stage cannot finalize while any part
    # of the policy layer it publishes is unverified.
    blockers.extend(_policy_layer_scheme_blockers(
        case_id, "finalizing 'policy_clause_processing'"))

    for doc in policy_docs:
        doc_id = doc.get("document_id")
        blockers.extend(
            f"{doc_id}: {error}"
            for error in _canonical_uid_finalize_blockers(case_id, doc_id))
        normalized_name = f"normalized_policy_clause_{doc_id}.json"
        inventory_name = f"policy_boundary_inventory_{doc_id}.json"
        audit_name = f"policy_audit_result_{doc_id}.json"
        role = policy_roles.declared_role(doc)

        if role == "segmented_parent":
            # Normalization is delegated to its segments; parent-coverage
            # (below) governs its completeness. The role check above already
            # confirmed the segments and the coverage contract are real.
            continue

        reference_table = read_contract_data(
            case_id, f"reference_table_{doc_id}.json")
        normalized = read_contract_data(case_id, normalized_name)
        inventory = read_contract_data(case_id, inventory_name)
        # An appendix/별표 segment carries structured tables, not policy
        # clauses. It still owes a complete boundary inventory (every page
        # accounted for), but no clause contract or clause-audit -- and only
        # because the verified role says so, not because none was written.
        if role == "reference_table_only":
            if inventory is None:
                blockers.append(f"{doc_id}: missing {inventory_name}")
                continue
            schemas, registry = load_registry()
            reference_errors = validate_instance(
                reference_table, _cross_contract.REFERENCE_TABLE_SCHEMA,
                schemas, registry)
            blockers.extend(
                f"{doc_id}: reference table schema: {error}"
                for error in reference_errors)
            try:
                reference_source_errors = _cross_contract.check_reference_table(
                    reference_table,
                    f"reference_table_{doc_id}.json",
                    _redacted_text_for_doc(case_id, doc_id),
                )
            except _cross_contract.SourceUnavailable as exc:
                reference_source_errors = [f"source unavailable: {exc}"]
            blockers.extend(
                f"{doc_id}: reference table: {error}"
                for error in reference_source_errors)
            blockers.extend(
                f"{doc_id}: unresolved reference table: {error}"
                for error in
                _cross_contract.unresolved_reference_table_reviews(
                    reference_table))
            inventory_errors = validate_instance(
                inventory, policy_completeness.INVENTORY_SCHEMA,
                schemas, registry)
            blockers.extend(
                f"{doc_id}: inventory schema: {error}"
                for error in inventory_errors)
            blockers.extend(
                f"{doc_id}: {error}"
                for error in policy_completeness.check_policy_boundary_inventory(
                    inventory, inventory_name,
                    _redacted_text_for_doc(case_id, doc_id), None))
            blockers.extend(
                f"{doc_id}: unresolved boundary: {error}"
                for error in policy_completeness.unresolved_boundaries(inventory))
            continue

        if normalized is None:
            blockers.append(f"{doc_id}: missing {normalized_name}")
            continue
        if not normalized.get("clauses"):
            blockers.append(f"{doc_id}: normalized clauses is empty")
        normalized_errors = validate_instance(
            normalized, "normalized_policy_clause.schema.json",
            schemas, registry)
        blockers.extend(
            f"{doc_id}: normalized schema: {error}"
            for error in normalized_errors)
        # Re-run the FULL cross-contract check against the source as it is NOW,
        # not just the schema shape (Part 11F). Without this, a contract written
        # while its evidence matched could survive a later change to
        # redacted_text.md: the write-time check passed once, and finalize only
        # re-validated the JSON's shape. Every quote is re-verified here.
        try:
            normalized_source_errors = \
                _cross_contract.check_normalized_policy_clause(
                    normalized, normalized_name,
                    _redacted_text_for_doc(case_id, doc_id))
        except _cross_contract.SourceUnavailable as exc:
            normalized_source_errors = [f"source unavailable: {exc}"]
        blockers.extend(
            f"{doc_id}: normalized source: {error}"
            for error in normalized_source_errors)
        if inventory is None:
            blockers.append(f"{doc_id}: missing {inventory_name}")
            continue
        inventory_errors = validate_instance(
            inventory, policy_completeness.INVENTORY_SCHEMA,
            schemas, registry)
        blockers.extend(
            f"{doc_id}: inventory schema: {error}"
            for error in inventory_errors)
        blockers.extend(
            f"{doc_id}: {error}"
            for error in policy_completeness.check_policy_boundary_inventory(
                inventory,
                inventory_name,
                _redacted_text_for_doc(case_id, doc_id),
                normalized,
            )
        )
        blockers.extend(
            f"{doc_id}: evidence-boundary binding: {error}"
            for error in
            policy_completeness.check_clause_evidence_within_boundaries(
                normalized, inventory,
                _redacted_text_for_doc(case_id, doc_id))
        )
        blockers.extend(
            f"{doc_id}: unresolved boundary: {error}"
            for error in policy_completeness.unresolved_boundaries(inventory)
        )
        blockers.extend(
            f"{doc_id}: reference table link: {error}"
            for error in _reference_table_link_errors(case_id, normalized)
        )
        audit = read_contract_data(case_id, audit_name)
        if audit is None:
            blockers.append(f"{doc_id}: missing {audit_name}")
            continue
        audit_errors = validate_instance(
            audit, policy_audit.AUDIT_SCHEMA, schemas, registry)
        blockers.extend(
            f"{doc_id}: audit schema: {error}" for error in audit_errors)
        hashes, current_normalized, current_inventory, current_reference = \
            _policy_audit_context(case_id, doc_id)
        blockers.extend(
            f"{doc_id}: audit: {error}"
            for error in policy_audit.check_policy_audit(
                audit,
                audit_name,
                hashes,
                current_normalized,
                current_inventory,
                current_reference,
            )
        )
        blockers.extend(
            f"{doc_id}: unresolved audit: {error}"
            for error in policy_audit.unresolved_findings(audit)
        )
        blockers.extend(
            f"{doc_id}: audit human-provenance: {error}"
            for error in human_review.check_audit_human_provenance(
                audit, load_human_review_ledger(case_id), doc_id)
        )

    # Parent-level whole-page coverage. The per-document checks above only see
    # pages a segment already owns; they cannot detect a parent-PDF page that
    # was never carved into any segment. Every PHYSICAL (non-segment)
    # insurance_policy parent must additionally declare a parent-coverage
    # contract that accounts for its full 1..N logical page range, with no
    # unresolved (review_required/extraction_failed) page. CASE_030's 133
    # unowned pages (front matter, 별표 appendix, referenced laws) are exactly
    # what this catches.
    physical_parents = [
        d for d in policy_docs
        if d.get("document_role") != "segment"
    ]
    for parent in physical_parents:
        pid = parent.get("document_id")
        coverage_name = f"policy_parent_coverage_{pid}.json"
        coverage = read_contract_data(case_id, coverage_name)
        if coverage is None:
            blockers.append(
                f"{pid}: missing {coverage_name} -- the physical policy parent "
                "has no whole-page coverage accounting (every logical page must "
                "be owned, a reference table, or explicitly excluded with reason)")
            continue
        coverage_errors = validate_instance(
            coverage, policy_completeness.PARENT_COVERAGE_SCHEMA,
            schemas, registry)
        blockers.extend(
            f"{pid}: parent-coverage schema: {error}"
            for error in coverage_errors)
        blockers.extend(
            f"{pid}: parent-coverage: {error}"
            for error in policy_completeness.check_policy_parent_coverage(
                coverage,
                coverage_name,
                manifest,
                lambda doc_id: read_contract_data(
                    case_id, f"reference_table_{doc_id}.json") if doc_id else None,
                _redacted_text_for_doc(case_id, pid),
            )
        )
        blockers.extend(
            f"{pid}: unresolved parent page: {error}"
            for error in policy_completeness.unresolved_parent_pages(coverage)
        )
        blockers.extend(
            f"{pid}: unpaged human-provenance: {error}"
            for error in human_review.check_unpaged_human_provenance(
                coverage, load_human_review_ledger(case_id), pid)
        )
    return blockers


def _reference_table_link_errors(case_id: str, normalized: dict) -> list[str]:
    """Resolve every clause-to-table link by immutable table/row UID."""
    errors = []
    cache = {}
    for clause_index, clause in enumerate(normalized.get("clauses") or []):
        for ref_index, ref in enumerate(clause.get("reference_table_refs") or []):
            loc = f"clauses[{clause_index}].reference_table_refs[{ref_index}]"
            doc_id = ref.get("document_id")
            if doc_id not in cache:
                table_name = f"reference_table_{doc_id}.json"
                table_contract = read_contract_data(case_id, table_name)
                cache[doc_id] = table_contract
                if table_contract is not None:
                    schemas, registry = load_registry()
                    schema_errors = validate_instance(
                        table_contract,
                        _cross_contract.REFERENCE_TABLE_SCHEMA,
                        schemas,
                        registry,
                    )
                    errors.extend(
                        f"{table_name}: schema: {error}"
                        for error in schema_errors)
                    try:
                        source_errors = _cross_contract.check_reference_table(
                            table_contract,
                            table_name,
                            _redacted_text_for_doc(case_id, doc_id),
                        )
                    except _cross_contract.SourceUnavailable as exc:
                        source_errors = [f"source unavailable: {exc}"]
                    errors.extend(
                        f"{table_name}: {error}" for error in source_errors)
            table_contract = cache[doc_id]
            if table_contract is None:
                errors.append(
                    f"{loc}: reference_table_{doc_id}.json does not exist")
                continue
            table = next(
                (item for item in table_contract.get("tables") or []
                 if item.get("table_uid") == ref.get("table_uid")),
                None,
            )
            if table is None:
                errors.append(
                    f"{loc}: table_uid {ref.get('table_uid')!r} does not resolve")
                continue
            available_rows = {
                row.get("row_uid") for row in table.get("rows") or []}
            missing_rows = set(ref.get("row_uids") or []) - available_rows
            if missing_rows:
                errors.append(
                    f"{loc}: row_uids do not resolve: {sorted(missing_rows)}")
    return errors


def policy_document_digest(case_id: str, doc_id: str) -> str:
    """The upstream policy snapshot digest for one document (Part 11I).

    A downstream artifact does not merely point INTO the policy layer, it is
    an assertion about what that layer said at the moment it was written. So
    it has to record which bytes it read. `_policy_audit_context` already
    computes the full set that matters -- normalized contract, boundary
    inventory, reference tables, processed source text, manifest entry
    (page_map included), governing parent coverage -- and the audit itself
    binds to exactly those, so reusing it keeps the downstream binding and the
    audit binding from ever drifting apart.

    Missing pieces are hashed as JSON null rather than skipped: "this document
    has no reference table" and "this document's reference table was deleted"
    must produce different digests from a state where one exists.
    """
    hashes, _, _, _ = _policy_audit_context(case_id, doc_id)
    encoded = json.dumps(hashes, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def policy_snapshot_for(case_id: str, doc_ids) -> dict:
    """The `upstream_policy_snapshot` value a downstream artifact must carry
    for the given referenced documents. Deterministic: sorted, so the digest
    depends on WHICH documents were read, never on the order they happened to
    be referenced in."""
    documents = [
        {"document_id": doc_id,
         "digest_sha256": policy_document_digest(case_id, doc_id)}
        for doc_id in sorted(set(doc_ids))
    ]
    encoded = json.dumps(documents, sort_keys=True, separators=(",", ":"))
    return {
        "documents": documents,
        "snapshot_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def _upstream_snapshot_errors(case_id: str, data: dict, doc_ids) -> list[str]:
    """Verify the recorded upstream snapshot against the live policy layer.

    Enforced only when the artifact actually carries policy references, so a
    coverage_result that matched no clause at all is unaffected. Where refs do
    exist the field is mandatory -- an artifact that cites the policy layer
    without recording which version of it, cannot be checked for staleness
    later, and 'cannot be checked' is the state this whole part exists to
    remove.
    """
    expected = policy_snapshot_for(case_id, doc_ids)
    recorded = data.get("upstream_policy_snapshot")
    if recorded is None:
        return [
            "upstream_policy_snapshot is missing -- an artifact that references "
            "policy clauses must record the policy snapshot it was derived "
            f"from (expected snapshot_sha256 {expected['snapshot_sha256']}; "
            "compute it with `dao.py policy-snapshot`)"
        ]
    errors = []
    recorded_docs = {
        entry.get("document_id"): entry.get("digest_sha256")
        for entry in recorded.get("documents") or []
    }
    expected_docs = {
        entry["document_id"]: entry["digest_sha256"]
        for entry in expected["documents"]
    }
    for doc_id, digest in expected_docs.items():
        if doc_id not in recorded_docs:
            errors.append(
                f"upstream_policy_snapshot does not cover referenced document "
                f"{doc_id}")
        elif recorded_docs[doc_id] != digest:
            errors.append(
                f"upstream_policy_snapshot for {doc_id} is stale: recorded "
                f"{recorded_docs[doc_id]!r}, current {digest!r} -- the policy "
                "layer changed after this artifact was derived from it")
    for doc_id in recorded_docs.keys() - expected_docs.keys():
        errors.append(
            f"upstream_policy_snapshot records {doc_id}, which this artifact "
            "does not reference -- the snapshot must describe exactly the "
            "documents it read")
    if not errors and recorded.get("snapshot_sha256") != expected["snapshot_sha256"]:
        errors.append(
            "upstream_policy_snapshot.snapshot_sha256 does not match its own "
            f"documents list (recorded {recorded.get('snapshot_sha256')!r}, "
            f"expected {expected['snapshot_sha256']!r})")
    return errors


def _policy_stage_passed_errors(case_id: str) -> list[str]:
    """`policy_clause_processing` must be currently `passed` before anything
    downstream may cite it (Part 11I).

    Every per-document check below asks "is this contract internally sound".
    None of them ask the question the completion gate asks: did the policy
    stage as a whole ever clear -- every document accounted for, every role
    verified, every unresolved boundary closed. Without this, a run could cite
    a clean-looking clause out of a policy stage that is sitting `failed`,
    which is exactly CASE_030's shape.
    """
    state = load_run_state(case_id)
    status = next(
        (entry.get("status") for entry in state.get("stages", [])
         if entry.get("stage_name") == "policy_clause_processing"),
        None)
    if status == "passed":
        return []
    shown = status if status is not None else "absent (never recorded)"
    return [
        f"policy_clause_processing is {shown}, not 'passed' -- downstream "
        "artifacts may not reference policy clauses until the policy stage "
        "itself has cleared its completion gate"
    ]


def _parent_coverage_current_errors(case_id: str, doc_id: str) -> list[str]:
    """The parent coverage governing `doc_id` must exist and still validate.

    A segment's clause is only trustworthy if the parent it was carved from is
    still fully accounted for. Re-running the check (rather than trusting the
    fact that it passed once at write time) is the point: the parent's page
    map or processed text may have moved since.
    """
    coverage_name = _parent_coverage_name_for(case_id, doc_id)
    if coverage_name is None:
        return []
    parent_id = coverage_name[len("policy_parent_coverage_"):-len(".json")]
    manifest = read_contract_data(case_id, "document_manifest.json")
    if manifest is None:
        return ["document_manifest.json is missing"]
    parent_entry = next(
        (d for d in manifest.get("documents", [])
         if d.get("document_id") == parent_id), None)
    # Only a segmented physical parent owes a coverage contract; a standalone
    # policy document has no parent to account for.
    if parent_entry is None or parent_entry.get("document_role") == "segment":
        return []
    if policy_roles.declared_role(parent_entry) != "segmented_parent":
        return []
    coverage = read_contract_data(case_id, coverage_name)
    if coverage is None:
        return [f"{coverage_name} is missing"]
    errors = [
        f"parent coverage: {error}"
        for error in policy_completeness.check_policy_parent_coverage(
            coverage, coverage_name, manifest,
            lambda d: read_contract_data(
                case_id, f"reference_table_{d}.json") if d else None,
            _redacted_text_for_doc(case_id, parent_id),
        )
    ]
    errors.extend(
        f"parent coverage: unresolved page: {error}"
        for error in policy_completeness.unresolved_parent_pages(coverage))
    errors.extend(
        f"parent coverage: unpaged human-provenance: {error}"
        for error in human_review.check_unpaged_human_provenance(
            coverage, load_human_review_ledger(case_id), parent_id))
    return errors


def _downstream_policy_ref_errors(
        case_id: str, schema_name: str, data: dict) -> list[str]:
    """Resolve downstream policy UIDs only against current, clear audits."""
    refs = []
    if schema_name == "coverage_result.schema.json":
        refs = [
            (f"coverages[{index}].matched_clause_ref",
             coverage.get("matched_clause_ref"))
            for index, coverage in enumerate(data.get("coverages") or [])
            if coverage.get("matched_clause_ref") is not None
        ]
    elif schema_name == "requirement_matching_result.schema.json":
        refs = [
            (f"coverage_requirements[{coverage_index}].requirements"
             f"[{requirement_index}].clause_ref",
             requirement.get("clause_ref"))
            for coverage_index, coverage in enumerate(
                data.get("coverage_requirements") or [])
            for requirement_index, requirement in enumerate(
                coverage.get("requirements") or [])
            if requirement.get("clause_ref") is not None
        ]
    elif schema_name == "denial_reason_result.schema.json":
        refs = [
            (f"denial_reasons[{reason_index}].policy_matches[{match_index}]",
             match)
            for reason_index, reason in enumerate(data.get("denial_reasons") or [])
            for match_index, match in enumerate(reason.get("policy_matches") or [])
        ]

    if not refs:
        return []

    # P0-3, checked BEFORE the recorded stage status. A run-state entry saying
    # `policy_clause_processing: passed` is a historical claim; it may have
    # been recorded before canonical verification existed, or against documents
    # that are still legacy today. Trusting it would let the one field a stale
    # run-state can assert stand in for the verification it was never subject
    # to -- so the live scheme of the actually-referenced documents is what
    # decides, and the recorded status is an additional requirement, not a
    # substitute.
    errors = _canonical_state_blockers(
        case_id, [ref.get("document_id") for _, ref in refs],
        f"writing the policy-referencing contract {schema_name}")

    # The whole policy stage must currently hold, not just the individual
    # contracts this artifact happens to cite (Part 11I).
    errors.extend(_policy_stage_passed_errors(case_id))
    errors.extend(_upstream_snapshot_errors(
        case_id, data, [ref.get("document_id") for _, ref in refs]))

    checked_docs = {}
    for loc, ref in refs:
        doc_id = ref.get("document_id")
        if doc_id not in checked_docs:
            normalized = read_contract_data(
                case_id, f"normalized_policy_clause_{doc_id}.json")
            audit = read_contract_data(
                case_id, f"policy_audit_result_{doc_id}.json")
            doc_errors = []
            if normalized is None:
                doc_errors.append("normalized policy contract is missing")
            if audit is None:
                doc_errors.append("policy audit is missing")
            if normalized is not None and audit is not None:
                hashes, current_normalized, inventory, reference = \
                    _policy_audit_context(case_id, doc_id)
                doc_errors.extend(policy_audit.check_policy_audit(
                    audit,
                    f"policy_audit_result_{doc_id}.json",
                    hashes,
                    current_normalized,
                    inventory,
                    reference,
                ))
                doc_errors.extend(policy_audit.unresolved_findings(audit))
            doc_errors.extend(_parent_coverage_current_errors(case_id, doc_id))
            # Resolving a downstream reference by string match against the
            # clause contract only proves the two files agree. Under
            # canonical_v1 the cited contract's own UIDs are recomputed from
            # source here, so a fabricated PC/CI written identically into both
            # the clause contract and the artifact citing it is refused rather
            # than mutually confirmed (P0-2).
            doc_errors.extend(
                f"canonical UID: {error}"
                for error in _canonical_uid_errors(
                    case_id, f"normalized_policy_clause_{doc_id}.json",
                    _cross_contract.NORMALIZED_POLICY_CLAUSE_SCHEMA,
                    normalized or {}))
            checked_docs[doc_id] = (normalized, doc_errors)
        normalized, doc_errors = checked_docs[doc_id]
        errors.extend(f"{loc}: {error}" for error in doc_errors)
        if normalized is None:
            continue
        clause = next(
            (item for item in normalized.get("clauses") or []
             if item.get("clause_uid") == ref.get("clause_uid")),
            None,
        )
        if clause is None:
            errors.append(
                f"{loc}: clause_uid {ref.get('clause_uid')!r} does not resolve")
            continue
        condition_uid = ref.get("condition_uid")
        if condition_uid is not None:
            available = {
                item.get("condition_uid")
                for bucket in _cross_contract.CONDITION_BUCKETS
                for item in clause.get(bucket) or []}
            if condition_uid not in available:
                errors.append(
                    f"{loc}: condition_uid {condition_uid!r} does not resolve "
                    "within the referenced clause")
    return errors


def _run_cross_contract(case_id, filename, schema_name, data, target) -> int:
    """Run the registered cross-contract checks for schema_name (schema-shape
    validation already passed by the time this is called). Returns 0 to allow
    the write, 1 to refuse it -- same fail-before-persist contract the schema
    errors use. SourceUnavailable is a refusal, not a pass: a contract whose
    source cannot be verified must never be written."""
    if schema_name == _cross_contract.NORMALIZED_POLICY_CLAUSE_SCHEMA:
        target_doc = _cross_contract.doc_id_from_filename(filename)

        # The cited document must be a registered automated-text source in the
        # manifest. CASE_030's DOC_004/005/006 were cited by normalized clause
        # files while absent from the manifest entirely -- a segment posing as
        # a first-class document with no verifiable lineage. Refuse that here,
        # before the (also-required) quote-verification below.
        manifest = read_contract_data(case_id, "document_manifest.json")
        if manifest is None:
            print(f"FAIL: cross-contract source could not be verified for {target}:")
            print(f"  - NO_MANIFEST: document_manifest.json does not exist -- the cited "
                  f"document {target_doc} cannot be confirmed as a registered case document")
            return 1
        lineage_errors = _validate_manifest_lineage(case_id, manifest)
        if lineage_errors:
            print(f"FAIL: segment-lineage validation errors for {target} -- source changed "
                  "or no longer matches its manifest:")
            for e in lineage_errors:
                print(f"  - {e}")
            return 1
        if target_doc is not None and not segment_lineage.is_registered_automated_source(manifest, target_doc):
            print(f"FAIL: cross-contract validation errors for {target}:")
            print(f"  - UNREGISTERED_SOURCE: {target_doc} is not a manifest document with "
                  f"downstream_disposition=automated_text_pipeline -- a normalized clause file "
                  f"may only cite a registered, automated-text document (segment or physical)")
            return 1
        target_entry = next(
            (d for d in manifest.get("documents", [])
             if d.get("document_id") == target_doc), None)
        if target_entry is not None and target_entry.get("document_type") != "insurance_policy":
            print(f"FAIL: cross-contract validation errors for {target}:")
            print(f"  - WRONG_DOCUMENT_TYPE: {target_doc} is "
                  f"{target_entry.get('document_type')!r}, not 'insurance_policy'")
            return 1
        if target_doc is not None and not segment_lineage.parent_processing_complete(manifest, target_doc):
            print(f"FAIL: cross-contract validation errors for {target}:")
            print(f"  - PARENT_INCOMPLETE: {target_doc} is a segment whose physical parent's "
                  f"processing is not complete -- the segment cannot be used downstream yet")
            return 1

        redacted_text = _redacted_text_for_doc(case_id, target_doc)
        try:
            errors = _cross_contract.check_normalized_policy_clause(data, filename, redacted_text)
        except _cross_contract.SourceUnavailable as exc:
            print(f"FAIL: cross-contract source could not be verified for {target}:")
            print(f"  - SOURCE_UNAVAILABLE: {exc}")
            return 1
        if errors:
            print(f"FAIL: cross-contract validation errors for {target}:")
            for e in errors:
                print(f"  - {e}")
            return 1
        link_errors = _reference_table_link_errors(case_id, data)
        if link_errors:
            print(f"FAIL: reference-table link errors for {target}:")
            for error in link_errors:
                print(f"  - {error}")
            return 1
    elif schema_name == _cross_contract.REFERENCE_TABLE_SCHEMA:
        target_doc = _cross_contract.doc_id_from_filename(filename)
        manifest = read_contract_data(case_id, "document_manifest.json")
        if manifest is None:
            print(f"FAIL: no document_manifest.json -- cannot verify {target}")
            return 1
        lineage_errors = _validate_manifest_lineage(case_id, manifest)
        if lineage_errors:
            print(f"FAIL: segment-lineage validation errors for {target}:")
            for error in lineage_errors:
                print(f"  - {error}")
            return 1
        entry = next(
            (item for item in manifest.get("documents", [])
             if item.get("document_id") == target_doc),
            None,
        )
        if (
            entry is None
            or entry.get("document_type") != "insurance_policy"
            or entry.get("downstream_disposition") != "automated_text_pipeline"
        ):
            print(
                f"FAIL: {target_doc} is not a registered automated "
                "insurance_policy source")
            return 1
        if (
            target_doc is not None
            and not segment_lineage.parent_processing_complete(
                manifest, target_doc)
        ):
            print(
                f"FAIL: {target_doc} is a segment whose physical parent's "
                "processing is incomplete")
            return 1
        try:
            errors = _cross_contract.check_reference_table(
                data, filename, _redacted_text_for_doc(case_id, target_doc))
        except _cross_contract.SourceUnavailable as exc:
            print(f"FAIL: cross-contract source could not be verified for {target}:")
            print(f"  - SOURCE_UNAVAILABLE: {exc}")
            return 1
        if errors:
            print(f"FAIL: reference-table validation errors for {target}:")
            for error in errors:
                print(f"  - {error}")
            return 1
    elif schema_name == policy_completeness.INVENTORY_SCHEMA:
        target_doc = policy_completeness.doc_id_from_inventory_filename(filename)
        manifest = read_contract_data(case_id, "document_manifest.json")
        if manifest is None:
            print(f"FAIL: no document_manifest.json -- cannot verify {target}")
            return 1
        entry = next(
            (d for d in manifest.get("documents", [])
             if d.get("document_id") == target_doc), None)
        if (
            entry is None
            or entry.get("document_type") != "insurance_policy"
            or entry.get("downstream_disposition") != "automated_text_pipeline"
        ):
            print(f"FAIL: {target_doc} is not a registered automated insurance_policy source")
            return 1
        normalized = read_contract_data(
            case_id, f"normalized_policy_clause_{target_doc}.json")
        errors = policy_completeness.check_policy_boundary_inventory(
            data,
            filename,
            _redacted_text_for_doc(case_id, target_doc),
            normalized,
        )
        errors.extend(
            policy_completeness.check_clause_evidence_within_boundaries(
                normalized, data, _redacted_text_for_doc(case_id, target_doc)))
        if errors:
            print(f"FAIL: policy completeness errors for {target}:")
            for error in errors:
                print(f"  - {error}")
            return 1
    elif schema_name == policy_audit.AUDIT_SCHEMA:
        target_doc = policy_audit.doc_id_from_filename(filename)
        manifest = read_contract_data(case_id, "document_manifest.json")
        entry = next(
            (item for item in (manifest or {}).get("documents", [])
             if item.get("document_id") == target_doc),
            None,
        )
        if (
            entry is None
            or entry.get("document_type") != "insurance_policy"
            or entry.get("downstream_disposition") != "automated_text_pipeline"
        ):
            print(
                f"FAIL: {target_doc} is not a registered automated "
                "insurance_policy source")
            return 1
        lineage_errors = _validate_manifest_lineage(case_id, manifest)
        if lineage_errors:
            print(f"FAIL: segment-lineage validation errors for {target}:")
            for error in lineage_errors:
                print(f"  - {error}")
            return 1
        hashes, normalized, inventory, reference = _policy_audit_context(
            case_id, target_doc)
        if normalized is None or inventory is None:
            print(
                f"FAIL: audit requires current normalized clause and boundary "
                f"inventory contracts for {target_doc}")
            return 1
        errors = policy_audit.check_policy_audit(
            data, filename, hashes, normalized, inventory, reference)
        if errors:
            print(f"FAIL: policy audit validation errors for {target}:")
            for error in errors:
                print(f"  - {error}")
            return 1
        provenance_errors = human_review.check_audit_human_provenance(
            data, load_human_review_ledger(case_id), target_doc)
        if provenance_errors:
            print(f"FAIL: policy audit human-provenance errors for {target}:")
            for error in provenance_errors:
                print(f"  - {error}")
            return 1
    elif schema_name == policy_completeness.PARENT_COVERAGE_SCHEMA:
        manifest = read_contract_data(case_id, "document_manifest.json")
        if manifest is None:
            print(f"FAIL: no document_manifest.json -- cannot verify {target}")
            return 1
        coverage_doc = policy_completeness.doc_id_from_parent_coverage_filename(
            filename)
        errors = policy_completeness.check_policy_parent_coverage(
            data, filename, manifest,
            lambda doc_id: read_contract_data(
                case_id, f"reference_table_{doc_id}.json") if doc_id else None,
            _redacted_text_for_doc(case_id, coverage_doc),
        )
        if errors:
            print(f"FAIL: parent-coverage validation errors for {target}:")
            for error in errors:
                print(f"  - {error}")
            return 1
        provenance_errors = human_review.check_unpaged_human_provenance(
            data, load_human_review_ledger(case_id), coverage_doc)
        if provenance_errors:
            print(f"FAIL: parent-coverage human-provenance errors for {target}:")
            for error in provenance_errors:
                print(f"  - {error}")
            return 1
    return 0


def _referenced_policy_documents(case_id: str, filename: str,
                                  data: dict) -> list[str]:
    """Every document a policy-layer contract's claims are expressed against.

    Usually just the one named in the filename; a parent-coverage contract and
    an audit can read several, which is why the binding is a map rather than a
    single hash.
    """
    doc_ids = set()
    own = _cross_contract.doc_id_from_filename(filename)
    if own:
        doc_ids.add(own)
    if data.get("source_document_id"):
        doc_ids.add(data["source_document_id"])
    for page in data.get("pages") or []:
        if not isinstance(page, dict):
            continue
        for key in (
                "owning_document_id",  # historical spelling
                "owner_document_id",
                "reference_table_document_id"):
            if page.get(key):
                doc_ids.add(page[key])
    for clause in data.get("clauses") or []:
        if not isinstance(clause, dict):
            continue
        for reference in clause.get("reference_table_refs") or []:
            if isinstance(reference, dict) and reference.get("document_id"):
                doc_ids.add(reference["document_id"])
    return sorted(doc_ids)


def _source_revision_binding_errors(case_id: str, filename: str,
                                     data: dict) -> list[str]:
    """Bind a policy-layer contract to the exact registered source revision.

    Enforced per document, and only once that document is `canonical_v1`. A
    legacy document keeps its existing contracts readable and migratable --
    making the field unconditionally required would strand every pre-11J
    artifact and destroy the audit trail this part is trying to build -- but
    once canonical is switched on, new work must state which registered bytes
    it was derived from, and that statement is checked against the current
    pointer rather than believed.
    """
    doc_ids = _referenced_policy_documents(case_id, filename, data)
    canonical = [d for d in doc_ids if uid_scheme_for(case_id, d) == "canonical_v1"]
    if not canonical:
        return []

    binding = data.get("source_text_revision")
    if binding is None:
        return [
            "source_text_revision is missing -- "
            f"{', '.join(canonical)} is canonical_v1, so a contract must "
            "record the registered source revision its offsets, quotes and "
            "UIDs were computed against (see `dao.py read-revision-index`)"
        ]
    recorded = {
        item.get("document_id"): item.get("revision_sha256")
        for item in binding.get("documents") or []
    }
    errors = []
    for doc_id in canonical:
        entry = revision_entry_for(case_id, doc_id)
        current = (entry or {}).get("current_revision_sha256")
        if doc_id not in recorded:
            errors.append(
                f"source_text_revision does not cover {doc_id}, which this "
                "contract is expressed against")
        elif recorded[doc_id] != current:
            errors.append(
                f"source_text_revision for {doc_id} is stale: recorded "
                f"{recorded[doc_id]!r}, current {current!r} -- the source text "
                "was revised after this contract was derived from it")
    return errors


def _physical_page_for(case_id: str, doc_id: str, logical_page: int):
    """The immutable parent's physical page for a logical page.

    Canonical identity is keyed to the PHYSICAL page, so a segment must
    resolve through its page_map. A physical document's logical page IS its
    physical page. Returns None when a segment's map does not cover the page,
    which is a refusal upstream -- never a silent fallback to the logical
    number, since that would mint the same UID for two different pages of the
    same parent.
    """
    manifest = read_contract_data(case_id, "document_manifest.json")
    entry = next(
        (d for d in (manifest or {}).get("documents", [])
         if d.get("document_id") == doc_id), None)
    if entry is None:
        return None
    if entry.get("document_role") != "segment":
        return logical_page
    for mapping in entry.get("page_map") or []:
        if mapping.get("logical_page") == logical_page:
            return mapping.get("source_physical_page")
    return None


def _registered_pdf_digest(case_id: str, doc_id: str):
    """The immutable original's digest for a document, or its parent's.

    A segment's identity is keyed to the PDF it was carved out of, not to the
    segment file, so the parent is consulted first -- two segments of one
    policy must not mint different UIDs for the same physical page.
    """
    manifest = read_contract_data(case_id, "document_manifest.json") or {}
    documents = manifest.get("documents", [])
    entry = next(
        (d for d in documents if d.get("document_id") == doc_id), None)
    parent_id = (entry or {}).get("source_document_id") or doc_id
    for candidate in (parent_id, doc_id):
        found = next(
            (d for d in documents if d.get("document_id") == candidate), None)
        if found and found.get("source_pdf_sha256"):
            return found["source_pdf_sha256"]
    return None


def _uid_source_context(case_id: str, doc_id: str):
    """Build the resolver's SourceContext, or explain why it cannot be built.

    Returns `(context, errors)`. A context that cannot be built is always a
    non-empty error list under canonical_v1 -- never a quiet `(None, [])`,
    which is precisely the shape of the defect this part closes.
    """
    pdf_digest = _registered_pdf_digest(case_id, doc_id)
    if not pdf_digest:
        return None, [
            f"{doc_id} is canonical_v1 but no immutable source digest is "
            "recorded for it or its parent -- UIDs cannot be recomputed, and "
            "an unverifiable UID is not a passing one"
        ]
    text = _registered_revision_text(case_id, doc_id)
    if text is None:
        return None, [
            f"{doc_id} is canonical_v1 but has no registered source-text "
            "revision to recompute UIDs against"
        ]
    try:
        pages = policy_completeness.split_pages(text)
    except Exception as exc:  # noqa: BLE001
        return None, [
            f"{doc_id}: registered source text is unusable for UID "
            f"recomputation: {exc}"
        ]
    context = policy_uid_resolver.SourceContext(
        doc_id, pdf_digest, pages,
        lambda logical: _physical_page_for(case_id, doc_id, logical))
    return context, []


def _registered_revision_text(case_id: str, doc_id: str):
    """The bytes of the CURRENT registered revision, not whatever is on disk.

    Verifying against `redacted_text.md` directly would re-derive UIDs from
    text that may have been replaced since the revision pointer was set, which
    would make a stale contract look canonical. The pointer is the authority;
    the working file is only a copy of it.
    """
    entry = revision_entry_for(case_id, doc_id)
    current = (entry or {}).get("current_revision_sha256")
    if not current:
        return None
    path = revision_file_path(case_id, doc_id, current)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


# Which recomputation each policy-layer schema owes. A schema absent from this
# map carries no UIDs of its own; one present here is checked in full. Adding a
# UID-bearing schema without an entry is the failure mode that produced P0-2,
# so `_canonical_uid_errors` refuses an unmapped policy-layer schema outright
# rather than treating it as nothing to check.
_UID_BEARING_SCHEMAS = frozenset({
    policy_completeness.INVENTORY_SCHEMA,
    _cross_contract.NORMALIZED_POLICY_CLAUSE_SCHEMA,
    _cross_contract.REFERENCE_TABLE_SCHEMA,
})

# Policy-layer schemas that legitimately carry no source-derived UID of their
# own. They REFERENCE UIDs minted elsewhere (an audit finding cites a clause; a
# parent-coverage entry cites a table), and those references are resolved
# against the recomputed originals by `_canonical_uid_reference_errors` --
# named explicitly so a new schema cannot join this set by omission.
_UID_REFERENCING_SCHEMAS = frozenset({
    policy_audit.AUDIT_SCHEMA,
    policy_completeness.PARENT_COVERAGE_SCHEMA,
})


def _canonical_uid_errors(case_id: str, filename: str, schema_name: str,
                           data: dict) -> list[str]:
    """Recompute EVERY canonical UID in a contract from registered source
    provenance, and refuse any submitted value that does not match (P0-2).

    The predecessor recomputed only `page_spans` and returned `[]` for any
    contract lacking that key -- which is every contract except the boundary
    inventory, and even there it left PB/PC/CI unchecked. Six of seven UID
    kinds were consequently enforced by their format regex alone, so the same
    fabricated string reused consistently across artifacts passed every gate.

    Under canonical_v1 there is no "nothing to verify" branch: a UID-bearing
    contract with no resolvable provenance is refused, and a policy-layer
    schema this function does not know how to check is refused too. Both are
    fail-closed by construction, since the alternative is reporting success for
    a derivation nobody performed.

    Scoped to canonical_v1 documents, like the revision binding -- legacy
    artifacts stay readable and are never rewritten.
    """
    doc_id = _cross_contract.doc_id_from_filename(filename)
    if not doc_id or uid_scheme_for(case_id, doc_id) != "canonical_v1":
        return []

    if schema_name in _UID_REFERENCING_SCHEMAS:
        return _canonical_uid_reference_errors(
            case_id, doc_id, data, schema_name)

    if schema_name not in _UID_BEARING_SCHEMAS:
        return [
            f"{filename}: {schema_name} is a canonical_v1 policy-layer "
            "contract with no UID recomputation rule -- refusing rather than "
            "passing it unchecked, which is how the whole UID layer came to be "
            "unverified (see policy_uid_resolver.py)"
        ]

    context, errors = _uid_source_context(case_id, doc_id)
    if errors:
        return errors

    if schema_name == policy_completeness.INVENTORY_SCHEMA:
        return policy_uid_resolver.check_boundary_inventory(context, data)

    if schema_name == _cross_contract.REFERENCE_TABLE_SCHEMA:
        return policy_uid_resolver.check_reference_tables(context, data)

    # normalized_policy_clause: PCs are parented on boundaries defined in the
    # inventory, so the inventory must exist and its PBs must independently
    # recompute. A clause cannot mint its own parent.
    inventory = read_contract_data(
        case_id, f"policy_boundary_inventory_{doc_id}.json")
    if inventory is None:
        return [
            f"{doc_id}: no policy_boundary_inventory_{doc_id}.json -- clause "
            "UIDs are derived from their parent boundary, which only the "
            "inventory defines, so they cannot be recomputed without it"
        ]
    boundary_index = policy_uid_resolver.boundary_uid_index(context, inventory)
    errors = policy_uid_resolver.check_normalized_clauses(
        context, data, boundary_index)
    errors.extend(_canonical_clause_table_reference_errors(case_id, data))
    return errors


def _canonical_uid_reference_errors(case_id: str, doc_id: str,
                                     data: dict,
                                     schema_name: str | None = None) -> list[str]:
    """Resolve every UID a non-minting policy contract cites.

    An audit finding or a coverage entry does not create identifiers, it points
    at them. The check is therefore not recomputation but resolution: each
    cited UID must exist in the contract that owns it, and that owner's own
    UIDs are recomputed here rather than read, so a fabricated PC cannot be
    laundered by writing it into both the clause contract and the audit that
    cites it.
    """
    if schema_name == policy_completeness.PARENT_COVERAGE_SCHEMA:
        return _canonical_parent_table_reference_errors(case_id, data)

    context, errors = _uid_source_context(case_id, doc_id)
    if errors:
        return errors

    inventory = read_contract_data(
        case_id, f"policy_boundary_inventory_{doc_id}.json")
    normalized = read_contract_data(
        case_id, f"normalized_policy_clause_{doc_id}.json")
    tables = read_contract_data(case_id, f"reference_table_{doc_id}.json")

    boundary_index = policy_uid_resolver.boundary_uid_index(
        context, inventory or {})
    known_boundaries = set(boundary_index)
    known_clauses: set[str] = set()
    known_conditions: set[str] = set()
    if normalized is not None:
        clause_errors = policy_uid_resolver.check_normalized_clauses(
            context, normalized, boundary_index)
        # Only UIDs from a clause contract that itself recomputes cleanly may
        # be cited; otherwise a bad clause contract would legitimize the
        # references pointing at it.
        if not clause_errors:
            for clause in normalized.get("clauses") or []:
                known_clauses.add(clause.get("clause_uid"))
                for bucket in policy_uid_resolver._CONDITION_BUCKETS:
                    for condition in clause.get(bucket) or []:
                        known_conditions.add(condition.get("condition_uid"))
    known_tables: set[str] = set()
    known_rows: set[str] = set()
    if tables is not None and not policy_uid_resolver.check_reference_tables(
            context, tables):
        for table in tables.get("tables") or []:
            known_tables.add(table.get("table_uid"))
            for row in table.get("rows") or []:
                known_rows.add(row.get("row_uid"))

    known = {
        "PB": (known_boundaries, "boundary inventory"),
        "PC": (known_clauses, "normalized clause contract"),
        "CI": (known_conditions, "normalized clause contract"),
        "RT": (known_tables, "reference table contract"),
        "RR": (known_rows, "reference table contract"),
    }
    reference_errors: list[str] = []
    for location, uid in _iter_uid_references(data):
        prefix = uid.split("-", 1)[0]
        if prefix not in known:
            reference_errors.append(
                f"{location}: {uid!r} is not a UID kind this contract may "
                "reference")
            continue
        pool, owner = known[prefix]
        if uid not in pool:
            reference_errors.append(
                f"{location}: {uid!r} does not resolve to a canonically "
                f"recomputed UID in {doc_id}'s {owner} -- a referenced "
                "identifier must exist in the artifact that mints it, and that "
                "artifact's own UIDs must recompute from source")
    return reference_errors


def _canonical_table_pools(case_id: str, doc_id: str) -> tuple[dict, list[str]]:
    """Recompute one table-owning document and return its canonical UID pools."""
    empty = {
        "RT": set(), "RR": set(), "RC": set(), "rows_by_table": {},
    }
    if uid_scheme_for(case_id, doc_id) != "canonical_v1":
        return empty, [
            f"{doc_id} is not canonical_v1 -- a cross-document table "
            "reference may not rely on an unverified legacy UID"
        ]
    context, errors = _uid_source_context(case_id, doc_id)
    if errors:
        return empty, errors
    tables = read_contract_data(case_id, f"reference_table_{doc_id}.json")
    if tables is None:
        return empty, [
            f"reference_table_{doc_id}.json does not exist"
        ]
    table_errors = policy_uid_resolver.check_reference_tables(context, tables)
    if table_errors:
        return empty, [
            f"reference_table_{doc_id}.json: {error}"
            for error in table_errors
        ]
    pools = {
        "RT": set(), "RR": set(), "RC": set(), "rows_by_table": {},
    }
    for table in tables.get("tables") or []:
        table_uid = table.get("table_uid")
        pools["RT"].add(table_uid)
        rows = set()
        for row in table.get("rows") or []:
            row_uid = row.get("row_uid")
            rows.add(row_uid)
            pools["RR"].add(row_uid)
            for cell in row.get("cells") or []:
                pools["RC"].add(cell.get("cell_uid"))
        pools["rows_by_table"][table_uid] = rows
    return pools, []


def _canonical_parent_table_reference_errors(
        case_id: str, data: dict) -> list[str]:
    """Resolve parent-coverage RTs in the document that actually owns them."""
    errors: list[str] = []
    cache: dict[str, tuple[dict, list[str]]] = {}
    for index, page in enumerate(data.get("pages") or []):
        table_uid = page.get("table_uid")
        if not table_uid:
            continue
        owner = page.get("reference_table_document_id")
        loc = f"$.pages[{index}]"
        if not owner:
            errors.append(
                f"{loc}: table_uid {table_uid!r} has no "
                "reference_table_document_id")
            continue
        if owner not in cache:
            cache[owner] = _canonical_table_pools(case_id, owner)
        pools, owner_errors = cache[owner]
        errors.extend(f"{loc}: {error}" for error in owner_errors)
        if not owner_errors and table_uid not in pools["RT"]:
            errors.append(
                f"{loc}: {table_uid!r} does not resolve to a canonically "
                f"recomputed table in reference_table_{owner}.json")
    return errors


def _canonical_clause_table_reference_errors(
        case_id: str, data: dict) -> list[str]:
    """Recompute cross-document RT/RR references carried by clauses."""
    errors: list[str] = []
    cache: dict[str, tuple[dict, list[str]]] = {}
    for clause_index, clause in enumerate(data.get("clauses") or []):
        for ref_index, reference in enumerate(
                clause.get("reference_table_refs") or []):
            loc = (
                f"clauses[{clause_index}].reference_table_refs[{ref_index}]")
            owner = reference.get("document_id")
            if not owner:
                errors.append(f"{loc}: no document_id")
                continue
            if owner not in cache:
                cache[owner] = _canonical_table_pools(case_id, owner)
            pools, owner_errors = cache[owner]
            errors.extend(f"{loc}: {error}" for error in owner_errors)
            if owner_errors:
                continue
            table_uid = reference.get("table_uid")
            if table_uid not in pools["RT"]:
                errors.append(
                    f"{loc}: {table_uid!r} does not resolve to a canonically "
                    f"recomputed table in reference_table_{owner}.json")
                continue
            owned_rows = pools["rows_by_table"].get(table_uid, set())
            for row_uid in reference.get("row_uids") or []:
                if row_uid not in owned_rows:
                    errors.append(
                        f"{loc}: row_uid {row_uid!r} is not a canonical row of "
                        f"table {table_uid!r} in {owner}")
    return errors


_UID_REFERENCE_KEYS = (
    "boundary_uid", "clause_uid", "condition_uid", "table_uid", "row_uid",
    "cell_uid", "span_uid",
)
_UID_REFERENCE_LIST_KEYS = ("row_uids", "source_boundary_uids")


def _iter_uid_references(node, path="$"):
    """Every UID-shaped value anywhere in a contract, with its JSON path.

    Walked generically rather than per-schema on purpose: a reference added to
    a nested object in a future schema revision is then covered the day it
    appears, instead of silently joining the unchecked set -- the exact way the
    original gap widened.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            location = f"{path}.{key}"
            if key in _UID_REFERENCE_KEYS and isinstance(value, str) and value:
                yield location, value
            elif key in _UID_REFERENCE_LIST_KEYS and isinstance(value, list):
                for index, item in enumerate(value):
                    if isinstance(item, str) and item:
                        yield f"{location}[{index}]", item
            else:
                yield from _iter_uid_references(value, location)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _iter_uid_references(item, f"{path}[{index}]")


def _protected_manifest_errors(case_id: str, proposed: dict) -> list[str]:
    """Refuse any caller-side difference in a DAO-owned manifest field.

    Applied to the WHOLE manifest write, not only the per-document patch path
    (Part 11J commit b). Sealing only `patch-manifest-document` would leave
    `write-contract document_manifest.json` as an open door to the same
    fields, and a seal with a door next to it is decoration. Insertion,
    modification, deletion, and rollback-to-an-earlier-value are all refused
    identically -- especially deletion, since a caller able to strip a sealed
    field could supply its own value on the next write.

    A document the current manifest does not yet contain may not arrive
    carrying sealed fields either: the DAO derives them from evidence it has
    observed, and it has observed none for a document it has never seen.
    """
    current = read_contract_data(case_id, "document_manifest.json") or {}
    existing = {
        d.get("document_id"): d for d in current.get("documents", [])}
    errors = []
    for entry in proposed.get("documents", []):
        doc_id = entry.get("document_id")
        errors.extend(source_provenance.protected_field_errors(
            existing.get(doc_id), entry, location=str(doc_id)))
    # A document present before and absent now would take its sealed
    # provenance with it; deleting the entry is how you would erase a
    # source_pdf_sha256 that no longer matched.
    for doc_id, entry in existing.items():
        if doc_id in {d.get("document_id") for d in proposed.get("documents", [])}:
            continue
        if any(field in entry for field in source_provenance.PROTECTED_MANIFEST_FIELDS):
            errors.append(
                f"{doc_id}: entry carries DAO-owned provenance and may not be "
                "removed by a manifest write -- deleting the entry would erase "
                "the sealed fields along with it")
    return errors


# Contracts that constitute the policy layer a downstream artifact's
# upstream_policy_snapshot is computed over. Rewriting one moves the ground
# under every stage that cited it, so it triggers the Part 11F/11I cascade.
_POLICY_LAYER_SCHEMAS = frozenset({
    _cross_contract.NORMALIZED_POLICY_CLAUSE_SCHEMA,
    policy_completeness.INVENTORY_SCHEMA,
    policy_completeness.PARENT_COVERAGE_SCHEMA,
    policy_audit.AUDIT_SCHEMA,
    "reference_table.schema.json",
})


def _policy_write_scheme_blockers(case_id: str, filename: str,
                                   data: dict) -> list[str]:
    """Refuse a policy-layer write against any non-canonical document (P0-3).

    Every document the contract is expressed against must be canonical_v1 --
    including the case where the filename names one document and the body
    references others (parent coverage over several owning documents). A
    partially-canonical set is refused whole: a contract whose claims about
    DOC_A were recomputed and whose claims about DOC_B were not is not half
    verified, it is unverified with a verified-looking part.

    A contract whose subject document cannot be determined at all is also
    refused. Being unable to name what is being written about is precisely the
    state in which no gate below can apply.
    """
    doc_ids = _referenced_policy_documents(case_id, filename, data)
    if not doc_ids:
        return [
            f"{filename}: the document this policy-layer contract is expressed "
            "against could not be determined (no document id in the filename, "
            "no source_document_id in the body) -- refused, because no UID "
            "verification can be scoped to an unidentified document"
        ]
    return _canonical_state_blockers(
        case_id, doc_ids, f"writing the policy-layer contract {filename}")


def cmd_write_contract(args):
    target = _require_within(case_dir(args.case_id), args.filename)
    existing_lock = acquire_lock_blocking(target, args.held_by, args.run_id, args.purpose or f"write {args.filename}")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    policy_layer_changed = None
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
        if schema_name in _POLICY_LAYER_SCHEMAS:
            # P0-3, and deliberately BEFORE the binding/UID checks: those two
            # are scoped to canonical_v1 documents and return [] for anything
            # else, so without this gate an unregistered or legacy document
            # reached `atomic_write_json` with its UIDs never recomputed. The
            # scheme gate is what makes the recomputation gate reachable.
            scheme_blockers = _policy_write_scheme_blockers(
                args.case_id, args.filename, data)
            if scheme_blockers:
                print(f"FAIL: {args.filename} targets documents whose UIDs are "
                      "not canonically verified -- not written:")
                for blocker in scheme_blockers:
                    print(f"  - {blocker}")
                return 1
            binding_errors = _source_revision_binding_errors(
                args.case_id, args.filename, data)
            if binding_errors:
                print(f"FAIL: {args.filename} is not bound to the current "
                      "registered source revision -- not written:")
                for error in binding_errors:
                    print(f"  - {error}")
                return 1
            uid_errors = _canonical_uid_errors(
                args.case_id, args.filename, schema_name, data)
            if uid_errors:
                print(f"FAIL: {args.filename} carries non-canonical UIDs -- "
                      "not written:")
                for error in uid_errors:
                    print(f"  - {error}")
                return 1
        if (
            _cross_contract.has_cross_contract_check(schema_name)
            or schema_name == policy_completeness.INVENTORY_SCHEMA
            or schema_name == policy_completeness.PARENT_COVERAGE_SCHEMA
            or schema_name == policy_audit.AUDIT_SCHEMA
        ):
            rc = _run_cross_contract(args.case_id, args.filename, schema_name, data, target)
            if rc != 0:
                return rc
        if schema_name in {
            "coverage_result.schema.json",
            "requirement_matching_result.schema.json",
            "denial_reason_result.schema.json",
        }:
            ref_errors = _downstream_policy_ref_errors(
                args.case_id, schema_name, data)
            if ref_errors:
                print(
                    f"FAIL: {schema_name} has stale, unresolved, or invalid "
                    "policy references:")
                for error in ref_errors:
                    print(f"  - {error}")
                return 1
        if schema_name == "document_manifest.schema.json":
            sealed_errors = _protected_manifest_errors(args.case_id, data)
            if sealed_errors:
                print(f"FAIL: DAO-owned provenance fields may not be written "
                      f"through write-contract for {target} -- not written:")
                for e in sealed_errors:
                    print(f"  - {e}")
                return 1
            lineage_errors = _validate_manifest_lineage(args.case_id, data)
            if lineage_errors:
                print(f"FAIL: segment-lineage validation errors for {target} -- not written:")
                for e in lineage_errors:
                    print(f"  - {e}")
                return 1
        # Captured before the write so the cascade below fires on a real change
        # only -- rewriting identical bytes must not invalidate anything.
        previous_contract = load_json(target) if target.exists() else None
        atomic_write_json(target, data)
        print(f"PASS: wrote {target}")
        if (schema_name in _POLICY_LAYER_SCHEMAS
                and previous_contract is not None
                and previous_contract != data):
            policy_layer_changed = args.filename
        if args.stage:
            # A different target (_run_state.json, not this contract file) --
            # no deadlock risk nesting this inside the contract file's lock.
            # A single contract write marks the stage in_progress, never passed:
            # passing a stage is a deliberate finalize-stage call (snapshot +
            # passed, atomic), not a side effect of one write. dep_check='soft'
            # so an unmet upstream dependency doesn't fail an otherwise-valid
            # contract write -- it just declines to advance the stage.
            state = _update_run_state(args.case_id, args.run_id, args.stage, "in_progress",
                                      args.held_by, dep_check="soft")
            if state is None:
                print("WARNING: contract write succeeded, but run-state could not be updated (see LOCKED above) -- "
                      "run-state may now lag behind actual progress; retry the run-state update.")
    finally:
        release_lock(target)

    # After the contract lock is released: the cascade takes the run-state
    # lock, and a downstream stage recorded `passed` against the previous
    # version of this policy contract is now a claim about bytes that no
    # longer exist (Part 11I).
    #
    # The contract write is already durable here, so a failed cascade cannot be
    # undone -- but it must not be reported as success either. Part 11J commit
    # a: surface it as a non-zero exit naming the exact repair, rather than the
    # WARNING-and-exit-0 that let a stale `passed` stand unnoticed.
    if policy_layer_changed is not None:
        try:
            _invalidate_dependents(
                args.case_id, "policy_clause_processing",
                f"upstream policy_clause_processing changed: "
                f"{policy_layer_changed} rewritten",
                args.held_by, args.run_id, strict=True)
        except CascadeFailed as exc:
            print(f"FAIL: {args.filename} was written, but downstream stages "
                  f"could not be invalidated -- {exc}")
            print("  downstream stages may still show 'passed' against the "
                  "previous policy contract; rerun this write, or demote "
                  "policy_clause_processing via update-run-state, before "
                  "trusting any downstream result")
            return 1
    return 0


# Manifest fields that downstream provenance is expressed against. Changing one
# moves the ground under every stage validated on top of it, so it triggers the
# Part 11F invalidation cascade. Deliberately narrow: descriptive metadata
# (classification_confidence, ocr_quality, …) does not invalidate anything.
_PROVENANCE_MANIFEST_FIELDS = frozenset({
    "page_map",
    "segment_page_ranges",
    "source_document_id",
    "source_total_pages",
    "derived_text_sha256",
    "derived_text_path",
    "redacted_text_path",
    "ocr_text_path",
    "document_role",
    "document_type",
    "downstream_disposition",
})


def patch_manifest_document(case_id: str, document_id: str, fields: dict, held_by: str, run_id: str,
                             stage: str | None = None, purpose: str | None = None):
    """Atomically read-modify-write a single document's fields in
    document_manifest.json, under one lock hold -- closes the residual gap
    write-contract leaves open for this file specifically (see the module
    docstring's caveat and known-gaps.md item 7): a caller using
    read-contract + write-contract reads BEFORE acquiring the lock, so a
    concurrent write between that read and the later write-contract call
    would be silently lost. Here the read happens after the lock is held,
    so it's guaranteed fresh. Returns (ok: bool, message: str) so both the
    CLI wrapper and in-process callers (run_checkpoint1.py) share one
    implementation instead of duplicating the read+merge+validate+write
    logic locally."""
    target = case_dir(case_id) / "document_manifest.json"
    existing_lock = acquire_lock_blocking(target, held_by, run_id, purpose or f"patch document_manifest.json ({document_id})")
    if existing_lock is not None:
        return False, (f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
                        f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
    provenance_changed: list[str] = []
    try:
        if not target.exists():
            return False, f"FAIL: no document_manifest.json for {case_id}"
        manifest = json.loads(target.read_text(encoding="utf-8"))
        doc = next((d for d in manifest["documents"] if d["document_id"] == document_id), None)
        if doc is None:
            return False, f"FAIL: document_id {document_id} not found in document_manifest.json"
        # DAO-owned provenance is refused here as well as in write-contract --
        # a seal on one path with the other left open is decoration
        # (Part 11J commit b).
        sealed_errors = source_provenance.protected_field_errors(
            doc, {**doc, **fields}, location=document_id)
        if sealed_errors:
            return False, (
                "FAIL: DAO-owned provenance fields may not be patched -- not "
                "written:\n" + "\n".join(f"  - {e}" for e in sealed_errors))
        # Which incoming fields actually change a value downstream provenance is
        # expressed against (Part 11F)? Metadata like classification_confidence
        # does not; the page mapping and source identity do.
        provenance_changed = [
            key for key in fields
            if key in _PROVENANCE_MANIFEST_FIELDS and doc.get(key) != fields[key]
        ]
        doc.update(fields)
        manifest["updated_at"] = now_iso()
        errors = _schema_check(manifest, "document_manifest.schema.json")
        if errors:
            return False, "FAIL: schema validation errors for " + str(target) + " -- not written:\n" + \
                "\n".join(f"  - {e}" for e in errors)
        lineage_errors = _validate_manifest_lineage(case_id, manifest)
        if lineage_errors:
            return False, "FAIL: segment-lineage validation errors for " + str(target) + " -- not written:\n" + \
                "\n".join(f"  - {e}" for e in lineage_errors)
        atomic_write_json(target, manifest)
        if stage:
            # in_progress, not passed -- see cmd_write_contract: patching one
            # document's manifest fields is progress within document_processing,
            # never the whole stage finalizing. dep_check='soft' so a manifest
            # patch never fails on run-state dependencies.
            state = _update_run_state(case_id, run_id, stage, "in_progress", held_by, dep_check="soft")
            if state is None:
                result = True, f"PASS: patched {document_id} in {target}\n" \
                    "WARNING: patch succeeded, but run-state could not be updated (lock contention) -- " \
                    "run-state may now lag behind actual progress; retry the run-state update."
            else:
                result = True, f"PASS: patched {document_id} in {target}"
        else:
            result = True, f"PASS: patched {document_id} in {target}"
    finally:
        release_lock(target)
    # Cascade outside the manifest lock: a changed page_map/source identity
    # moves the ground every downstream stage's provenance was checked against.
    # The patch is durable by now, so a failed cascade cannot be rolled back --
    # but it is reported as a failure rather than swallowed (Part 11J commit a).
    if provenance_changed:
        try:
            _invalidate_dependents(
                case_id, "document_processing",
                f"upstream document_processing changed: {document_id} manifest "
                f"{', '.join(sorted(provenance_changed))} updated",
                held_by, run_id, strict=True)
        except CascadeFailed as exc:
            return False, (
                f"PATCHED BUT NOT INVALIDATED: {document_id} manifest "
                f"{', '.join(sorted(provenance_changed))} updated, but "
                f"downstream stages could not be invalidated -- {exc}. "
                "Downstream stages may still show 'passed' against the "
                "previous provenance; repeat this patch, or demote "
                "document_processing via update-run-state, before trusting "
                "any downstream result.")
    return result


def cmd_patch_manifest_document(args):
    fields = json.loads(Path(args.fields_file).read_text(encoding="utf-8"))
    ok, message = patch_manifest_document(args.case_id, args.doc_id, fields, args.held_by, args.run_id,
                                           stage=args.stage, purpose=args.purpose)
    print(message)
    return 0 if ok else 1


def cmd_check_lock(args):
    target = _require_within(case_dir(args.case_id), args.filename)
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


def revision_index_path(case_id: str) -> Path:
    return case_dir(case_id) / "_revision_index.json"


def load_revision_index(case_id: str) -> dict:
    data = load_json(revision_index_path(case_id))
    if data is None:
        return {"case_id": case_id, "documents": []}
    return data


def revision_entry_for(case_id: str, doc_id: str):
    for entry in load_revision_index(case_id).get("documents", []):
        if entry.get("document_id") == doc_id:
            return entry
    return None


def uid_scheme_for(case_id: str, doc_id: str) -> str:
    """The document's UID verification state -- three values, not two (P0-3).

    A document with no revision entry returns `unregistered`, NOT `legacy`.
    The old conflation is what made canonical verification opt-in: every
    canonical check was scoped to `uid_scheme == canonical_v1`, and both
    "recorded as pre-canonical" and "the DAO has never heard of this document"
    fell out of that scope identically. Simply never calling
    enable-canonical-uids therefore skipped the entire UID layer.

    An unrecognized on-disk value is returned verbatim so callers refuse it
    explicitly (source_provenance.uid_scheme_blockers) rather than mapping it
    onto a known-good state.
    """
    entry = revision_entry_for(case_id, doc_id)
    if entry is None:
        return source_provenance.UNREGISTERED
    scheme = entry.get("uid_scheme")
    if scheme is None:
        # An entry exists but does not say. Schema-required, so this is a
        # tampered or partially-written index -- not a legacy document.
        return "missing"
    return scheme


def _canonical_state_blockers(case_id: str, doc_ids, action: str) -> list[str]:
    """Every document in `doc_ids` must be canonical_v1 for `action` to proceed.

    The single choke-point behind P0-3's three gates (policy-layer write,
    policy_clause_processing finalization, downstream policy reference). Sorted
    and deduplicated so a contract citing one document twice reports once, and
    so a mixed canonical/legacy set reports every offending document rather
    than the first one encountered.
    """
    blockers: list[str] = []
    for doc_id in sorted({d for d in doc_ids if d}):
        blockers.extend(source_provenance.uid_scheme_blockers(
            uid_scheme_for(case_id, doc_id), doc_id, action))
    return blockers


def registered_source_pdf_sha256(case_id: str, doc_id: str):
    """Hash the registered raw source file ourselves (Part 11J commit b).

    `source_pdf_sha256` is the first input to every canonical UID, so a
    caller-supplied value would let a UID be minted against a file the case
    never registered -- the one input that establishes WHICH document this is
    would establish nothing. The manifest's file_path is only used to locate
    the file; the digest always comes from reading it.
    """
    manifest = read_contract_data(case_id, "document_manifest.json")
    if manifest is None:
        return None
    entry = next(
        (d for d in manifest.get("documents", [])
         if d.get("document_id") == doc_id), None)
    if entry is None or not entry.get("file_path"):
        return None
    # file_path is repo-relative ("data/raw/CASE_030/DOC_005.pdf"). Resolve it
    # against DATA's parent rather than the module-level ROOT so it follows
    # the same redirection every other DAO path does -- a path that ignored
    # the configured data root would read outside the case it belongs to.
    path = _require_within(DATA.parent, entry["file_path"])
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _register_revision(case_id, doc_id, text, revision_sha, held_by, run_id,
                        supersedes=None, lock_already_held=False):
    """Append a revision to the DAO-owned index and point `current` at it.

    Every field is derived here, from the manifest and the registered bytes --
    uid_stability from extraction_method, the extractor profile from what the
    DAO observed, page digests from the text itself. Nothing is accepted from
    a caller, because a caller that could state its own stability or
    provenance could state whatever made its UIDs look verified.

    Called INSIDE the revision transaction, after downstream invalidation and
    immediately before the pointer flip, so the index and the pointer move
    together or not at all.
    """
    manifest = read_contract_data(case_id, "document_manifest.json")
    entry = next(
        (d for d in (manifest or {}).get("documents", [])
         if d.get("document_id") == doc_id), None)

    index = load_revision_index(case_id)
    documents = index.setdefault("documents", [])
    doc_entry = next(
        (d for d in documents if d.get("document_id") == doc_id), None)
    if doc_entry is None:
        doc_entry = {
            "document_id": doc_id,
            "current_revision_sha256": revision_sha,
            # New documents start legacy. canonical_v1 is only reachable
            # through the dedicated verified command (commit c) -- defaulting
            # to canonical would claim a verification that has not run.
            "uid_scheme": "legacy",
            "revisions": [],
        }
        documents.append(doc_entry)

    known = {r.get("revision_sha256") for r in doc_entry["revisions"]}
    if revision_sha not in known:
        doc_entry["revisions"].append({
            "revision_sha256": revision_sha,
            "registered_at": now_iso(),
            "registered_by": held_by,
            "run_id": run_id,
            "uid_stability": source_provenance.derive_uid_stability(entry),
            "extractor": source_provenance.extractor_profile(
                entry, "dao.write-redacted-text"),
            "page_text_sha256": source_provenance.page_text_digests(text),
            "supersedes": supersedes,
        })
    doc_entry["current_revision_sha256"] = revision_sha

    errors = _schema_check(index, "revision_index.schema.json")
    if errors:
        raise dao_transaction.TransactionAborted(
            "revision index would be schema-invalid: " + "; ".join(errors))

    target = revision_index_path(case_id)
    if not lock_already_held:
        existing_lock = acquire_lock_blocking(
            target, held_by, run_id or "unknown",
            f"register revision for {doc_id}")
        if existing_lock is not None:
            raise dao_transaction.TransactionAborted(
                f"revision index is locked by {existing_lock['held_by']}")
    try:
        atomic_write_json(target, index)
    finally:
        if not lock_already_held:
            release_lock(target)
    return doc_entry


def revisions_dir(case_id: str, doc_id: str) -> Path:
    return processed_dir(case_id, doc_id) / "_revisions"


def revision_file_path(case_id: str, doc_id: str, revision_sha: str) -> Path:
    return revisions_dir(case_id, doc_id) / f"{revision_sha}.md"


def cmd_write_redacted_text(args):
    """Register a source-text revision as one fail-closed transaction.

    Rewriting processed text is not a file write. Every policy boundary offset,
    every evidence quote, and every UID computed from source bytes is expressed
    against these exact bytes, so replacing them moves the ground under work
    already recorded as passed. Three things must move together: the text, the
    run-state that recorded what was validated against it, and the pointer
    naming the current revision.

    Ordering (see dao_transaction): everything that can fail runs first, and
    the pointer -- the only observable "this is now current" -- flips last.

      1. read + hash the new text; identical bytes are a no-op success
      2. write the new revision file (unreferenced; a stray copy harms nothing)
      3. build the prospective run-state and VALIDATE it
      4. journal the intent
      5. invalidate downstream stages -- conservatively, BEFORE the switch
      6. flip the pointer
      7. clear the journal

    A crash between 5 and 6 leaves the OLD text with stages already
    invalidated: over-invalidation, costing a rerun. A crash after 6 leaves the
    new text with those same stages invalidated. The state this ordering makes
    unreachable is the dangerous one -- new text alongside a stale `passed`.

    The previous implementation did the reverse: it wrote the text, reported
    success, then attempted the cascade, which returned quietly on lock
    contention or a schema failure. The write was durable by then, so a failed
    cascade produced exactly the state it existed to prevent, at exit code 0.
    """
    case_directory = case_dir(args.case_id)
    blockers = dao_transaction.pending_journal_errors(case_directory)
    if blockers:
        for blocker in blockers:
            print(f"BLOCKED: {blocker}")
        return 1

    target = processed_dir(args.case_id, args.doc_id) / "redacted_text.md"
    text = Path(args.text_file).read_text(encoding="utf-8")
    previous = target.read_text(encoding="utf-8") if target.exists() else None
    revision_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

    # Locks in the global order (dao_transaction.LOCK_ORDER): run_state before
    # current_pointer. Taking them the other way round is how two concurrent
    # DAO operations deadlock, so the order is asserted rather than assumed.
    lock_kinds = ("run_state", "current_pointer")
    order_errors = dao_transaction.check_lock_order(lock_kinds)
    if order_errors:
        for error in order_errors:
            print(f"FAIL: {error}")
        return 1

    state_target = run_state_path(args.case_id)
    changed = previous is not None and previous != text
    held_state_lock = False
    if changed:
        existing_lock = acquire_lock_blocking(
            state_target, args.held_by, args.run_id,
            f"revise source text for {args.doc_id}")
        if existing_lock is not None:
            print(f"LOCKED: held_by={existing_lock['held_by']} "
                  f"run_id={existing_lock['run_id']} -- source text NOT "
                  "revised (the downstream invalidation must be applied in "
                  "the same operation, so a contended run-state aborts the "
                  "whole revision rather than writing text nobody invalidated)")
            return 1
        held_state_lock = True

    try:
        existing_lock = acquire_lock_blocking(
            target, args.held_by, args.run_id,
            args.purpose or "write redacted text")
        if existing_lock is not None:
            print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
                  f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
            return 1
        try:
            if not changed:
                # First write, or identical bytes. Nothing downstream was ever
                # derived from different text, so there is nothing to
                # invalidate and no transaction to journal.
                atomic_write_text(
                    revision_file_path(args.case_id, args.doc_id, revision_sha),
                    text)
                try:
                    _register_revision(
                        args.case_id, args.doc_id, text, revision_sha,
                        args.held_by, args.run_id, supersedes=None)
                except dao_transaction.TransactionAborted as exc:
                    print(f"FAIL: source text NOT registered -- {exc}")
                    return 1
                atomic_write_text(target, text)
                print(f"PASS: wrote {target} (revision {revision_sha[:12]})")
                return 0

            # Step 2: the revision file is written before the pointer moves.
            # Until the pointer names it, it is an unreferenced copy.
            atomic_write_text(
                revision_file_path(args.case_id, args.doc_id, revision_sha),
                text)

            reason = (f"upstream document_processing changed: source text "
                      f"revised for {args.doc_id} (revision "
                      f"{revision_sha[:12]})")
            # Steps 3-5: validate and apply the invalidation FIRST. strict=True
            # turns a contended lock or an invalid prospective state into an
            # exception instead of a silent [] -- nothing irreversible has
            # happened yet, so aborting here is clean.
            dao_transaction.write_journal(case_directory, {
                "operation": "revise_source_text",
                "case_id": args.case_id,
                "document_id": args.doc_id,
                "from_revision": hashlib.sha256(
                    previous.encode("utf-8")).hexdigest(),
                "to_revision": revision_sha,
                "status": "invalidating",
                "started_at": now_iso(),
            })
            try:
                _invalidate_dependents(
                    args.case_id, "document_processing", reason,
                    args.held_by, args.run_id, strict=True,
                    lock_already_held=True)
            except CascadeFailed as exc:
                dao_transaction.clear_journal(case_directory)
                print(f"FAIL: source text NOT revised -- {exc}")
                print("  the current revision pointer is unchanged; retry once "
                      "the run-state is writable")
                return 1

            # Register the revision (append-only history + current pointer)
            # immediately before the switch, so the index and the text move
            # together. A failure here still leaves the OLD text current.
            try:
                _register_revision(
                    args.case_id, args.doc_id, text, revision_sha,
                    args.held_by, args.run_id,
                    supersedes=hashlib.sha256(
                        previous.encode("utf-8")).hexdigest())
            except dao_transaction.TransactionAborted as exc:
                dao_transaction.clear_journal(case_directory)
                print(f"FAIL: source text NOT revised -- {exc}")
                print("  downstream stages were invalidated conservatively; "
                      "the current revision pointer is unchanged")
                return 1

            # Step 6: the only irreversible, observable step, and it is last.
            atomic_write_text(target, text)
            dao_transaction.clear_journal(case_directory)
            print(f"PASS: wrote {target} (revision {revision_sha[:12]})")
            print(f"REVISED: {args.doc_id} source text replaced; downstream "
                  "stages invalidated before the switch")
            return 0
        finally:
            release_lock(target)
    finally:
        if held_state_lock:
            release_lock(state_target)


class CascadeFailed(Exception):
    """The invalidation cascade could not be applied (Part 11J commit a).

    Distinct from "nothing needed invalidating". `_invalidate_dependents`
    returned [] for both, and every caller discarded it, so a lock it could not
    take or a run-state it could not validate became a silent success on top of
    a durable write. Callers now have to handle this explicitly, and the
    ordering guarantees nothing irreversible has happened when it is raised.
    """


def _invalidate_dependents(case_id, upstream_stage, reason, held_by, run_id,
                            strict=False, lock_already_held=False):
    """Invalidate every stage that transitively depends on `upstream_stage`.

    Part 11F. A downstream stage recorded `passed` was derived from upstream
    bytes -- exact processed text, exact page_map offsets. When those bytes
    change, the recorded status is a claim about work that was never done
    against the current source, so it must not stand. Affected stages move to
    `failed` with an explicit invalidation_reason; the historical backup_path
    is kept (that snapshot really was taken, and P10 never rewrites a backup).

    `upstream_stage` itself is NOT touched: these writes are how that stage
    produces its own output, so invalidating it would fail the very stage
    doing the work.

    Runs entirely under the run-state lock (P5) and validates before writing,
    the same fail-don't-persist contract every other run-state writer uses. A
    missing/unreadable run-state is a no-op, not an error -- there is nothing
    recorded to invalidate.
    """
    target = run_state_path(case_id)
    if not target.exists():
        return []
    dependents = stage_dependencies.dependents_of(upstream_stage)
    if not dependents:
        return []
    # `lock_already_held` is for callers running this INSIDE a transaction that
    # took the run-state lock first (revise-source-text). Re-acquiring would
    # self-deadlock against the caller's own lock, and releasing the caller's
    # lock to take it here would reopen the window the ordering exists to
    # close -- another writer could pass a stage between the invalidation and
    # the pointer flip. The lock is not re-entrant, so the caller declares it.
    if not lock_already_held:
        existing_lock = acquire_lock_blocking(
            target, held_by, run_id or "unknown",
            f"invalidate stages downstream of {upstream_stage}")
        if existing_lock is not None:
            message = (f"could not invalidate downstream stages -- "
                       f"held_by={existing_lock['held_by']} "
                       f"run_id={existing_lock['run_id']}")
            if strict:
                raise CascadeFailed(message)
            print(f"LOCKED: {message}")
            return []
    try:
        state = load_run_state(case_id)
        changed = []
        for entry in state.get("stages", []):
            if entry.get("stage_name") not in dependents:
                continue
            if entry.get("status") not in ("passed", "in_progress"):
                continue
            entry["status"] = "failed"
            entry["invalidated_at"] = now_iso()
            entry["invalidation_reason"] = reason
            changed.append(entry.get("stage_name"))
        if not changed:
            return []
        state["updated_at"] = now_iso()
        errors = _schema_check(state, "run_state.schema.json")
        if errors:
            if strict:
                raise CascadeFailed(
                    "downstream invalidation would make run-state "
                    "schema-invalid: " + "; ".join(errors))
            print("WARNING: downstream invalidation would make run-state "
                  "schema-invalid; not written:")
            for error in errors:
                print(f"  - {error}")
            return []
        save_run_state(case_id, state)
        print(f"INVALIDATED (upstream {upstream_stage} changed): "
              f"{', '.join(sorted(changed))}")
        return changed
    finally:
        # Only release what this call acquired. Releasing a lock the caller
        # owns would leave the rest of its transaction running unprotected.
        if not lock_already_held:
            release_lock(target)


def _invalidate_policy_layer(case_id, reason, held_by, run_id):
    """Invalidate `policy_clause_processing` AND everything downstream of it.

    P0-3's stale cascade for enable-canonical-uids. `_invalidate_dependents`
    deliberately spares the upstream stage itself (its writes are how that
    stage does its job); here the policy stage is exactly what must stop
    standing, because every artifact it published carries UIDs that were never
    recomputed.

    One locked read-modify-write covering both the stage and its dependents, so
    there is no window in which the policy stage is demoted while a downstream
    stage still claims `passed` against it. Raises CascadeFailed on lock
    contention or a schema-invalid result -- the caller must not report success
    for a transition whose invalidation did not land.
    """
    target = run_state_path(case_id)
    if not target.exists():
        return []
    affected = ({"policy_clause_processing"}
                | stage_dependencies.dependents_of("policy_clause_processing"))
    existing_lock = acquire_lock_blocking(
        target, held_by, run_id or "unknown",
        "invalidate the policy layer for canonical UID activation")
    if existing_lock is not None:
        raise CascadeFailed(
            "could not invalidate the policy stage and its downstream -- "
            f"held_by={existing_lock['held_by']} "
            f"run_id={existing_lock['run_id']}")
    try:
        state = load_run_state(case_id)
        changed = []
        for entry in state.get("stages", []):
            if entry.get("stage_name") not in affected:
                continue
            # Idempotent: a stage already failed/pending was never claiming a
            # result derived from unverified UIDs, so re-running activation on
            # an already-invalidated case changes nothing and still succeeds.
            if entry.get("status") not in ("passed", "in_progress"):
                continue
            entry["status"] = "failed"
            entry["invalidated_at"] = now_iso()
            entry["invalidation_reason"] = reason
            changed.append(entry.get("stage_name"))
        if not changed:
            return []
        state["updated_at"] = now_iso()
        errors = _schema_check(state, "run_state.schema.json")
        if errors:
            raise CascadeFailed(
                "policy-layer invalidation would make run-state "
                "schema-invalid: " + "; ".join(errors))
        save_run_state(case_id, state)
        return changed
    finally:
        release_lock(target)


def _write_text_locked(case_id, filename, text_file, held_by, run_id, purpose=None):
    """Locked+atomic text write to outputs/CASE_XXX/FILENAME -- no schema
    validation, since there's nothing to validate a free-form text file
    against. Shared by cmd_write_text and cmd_write_reviewed_draft, same
    pattern as _update_run_state being shared by cmd_update_run_state and
    cmd_snapshot_backup."""
    target = _require_within(case_dir(case_id), filename)
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
        errors = _schema_check(ledger, "source_ledger.schema.json")
        if errors:
            print(f"FAIL: schema validation errors for {p} -- not written:")
            for e in errors:
                print(f"  - {e}")
            return 1
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

# --------------------------------------------------------- forbidden expressions --

_MD_TABLE_ROW = re.compile(r"^\|(.+)\|$")


def _normalize_expr(s: str) -> str:
    """Normalize a forbidden-expression phrase or a draft line for matching:
    straighten curly double-quotes, strip one layer of surrounding double-quotes,
    collapse internal whitespace runs to a single space. Deliberately literal --
    this is a floor, not a paraphrase detector."""
    s = s.replace("“", '"').replace("”", '"')
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1].strip()
    return s


def _load_forbidden_phrases(template_path: Path) -> list[str]:
    """Return the normalized first-column ('위험 표현') phrases from the markdown
    table in templates/forbidden-expressions.md. Returns [] if no parseable table
    (caller treats that as a setup failure, never a clean draft). Raises
    FileNotFoundError if the file is absent."""
    text = template_path.read_text(encoding="utf-8")  # raises FileNotFoundError if absent
    phrases = []
    for line in text.splitlines():
        m = _MD_TABLE_ROW.match(line.strip())
        if not m:
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if not cells:
            continue
        first = cells[0]
        # Skip the header row and the |---|---| separator row.
        if first in ("위험 표현", "") or set(first) <= set("-: "):
            continue
        norm = _normalize_expr(first)
        if norm:
            phrases.append(norm)
    return phrases


def cmd_check_forbidden_expressions(args):
    """Deterministic floor: scan a rendered draft for the listed literal phrases
    in templates/forbidden-expressions.md. Record-only -- the critic decides
    passed. Not exhaustive; the semantic/implied cases are the critic's P3 pass."""
    draft_path = Path(args.doc_path)
    try:
        phrases = _load_forbidden_phrases(FORBIDDEN_TEMPLATE)
    except FileNotFoundError:
        print(f"NO_TEMPLATE: {FORBIDDEN_TEMPLATE}")
        return 2
    if not phrases:
        print(f"NO_TEMPLATE: {FORBIDDEN_TEMPLATE}")
        return 2
    if not draft_path.exists():
        print(f"NOT_FOUND: {draft_path}")
        return 1

    raw_lines = draft_path.read_text(encoding="utf-8").splitlines()
    norm_lines = [_normalize_expr(ln) for ln in raw_lines]
    norm_full = _normalize_expr(" ".join(raw_lines))

    hits = []
    for phrase in phrases:
        line_no = None
        for i, nl in enumerate(norm_lines, start=1):
            if phrase in nl:
                line_no = i
                break
        if line_no is None and phrase in norm_full:
            line_no = None  # present but spans soft-wrapped lines
        elif line_no is None:
            continue
        hits.append({"phrase": phrase, "line": line_no})

    clean = not hits
    print(json.dumps({
        "clean": clean,
        "hits": hits,
        "source": str(FORBIDDEN_TEMPLATE.relative_to(ROOT)),
        "note": "listed literal phrases only; not exhaustive -- semantic P3 coverage is the critic's",
    }, ensure_ascii=False))
    return 0 if clean else 1


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


def _schema_check(data: dict, schema_name: str) -> list:
    """Validates data against schema_name, returning error strings (empty
    means valid). Used by the shared-state write paths (source ledger,
    run-state, conflict ledger) -- these build their own structures rather
    than accepting arbitrary agent-supplied JSON the way write-contract
    does, so a failure here means a bug in this file's own construction
    logic or a pre-existing malformed file, not bad agent output. No P4
    self-correction-retry step, just fail loud and don't persist -- same
    contract as write-contract's own validation failure path."""
    schemas, registry = load_registry()
    return validate_instance(data, schema_name, schemas, registry)


# ---------------------------------------------------------------- run state ops --

def _update_run_state(case_id, run_id, stage, status, held_by, backup_path=None,
                       finalize=False, dep_check="hard"):
    """Holds the run-state lock across the whole read+modify+write, not just
    the write -- see acquire_lock_blocking. Returns the updated state on
    success, or None if the lock never cleared, a dependency is unmet, or the
    resulting state fails schema validation (caller reports and halts).

    Two guards were added for CASE_030 (see stage_dependencies.py and the v0.3
    run_state schema):

    - status='passed' is ONLY reachable with finalize=True. A stage passes
      exactly once, atomically with its snapshot, through finalize-stage; a
      bare update-run-state can never mint a passed stage (and the schema
      additionally requires a non-null backup_path on any passed stage, so an
      attempt to do it by hand would fail validation regardless).
    - dependency graph: advancing a stage to in_progress/passed is refused if
      its upstream prerequisites are not passed (or dependency-accepted
      skipped). dep_check='hard' refuses and returns None; dep_check='soft'
      (used by write-contract/patch-manifest's incidental --stage progress
      marker) skips the advance with a warning but still writes the contract,
      since the *contract* is valid even when the stage cannot yet advance.
    """
    if status == "passed" and not finalize:
        print("REFUSED: a stage may not be set 'passed' directly via update-run-state -- "
              "passing a stage is atomic with its P10 snapshot; use finalize-stage. "
              "(harness-guardrails P10)")
        return None

    target = run_state_path(case_id)
    existing_lock = acquire_lock_blocking(target, held_by, run_id or "unknown", f"update run-state: {stage} -> {status}")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return None
    demoted_from = None
    try:
        state = load_run_state(case_id)
        state["run_id"] = run_id or state.get("run_id")

        blockers = stage_dependencies.check_dependencies(
            stage, status, state, human_review_complete_any(case_id))
        if blockers:
            if dep_check == "soft":
                print(f"NOTE: stage {stage!r} not advanced to {status!r} -- unmet dependencies:")
                for b in blockers:
                    print(f"  - {b}")
                # Return the unchanged state so callers treat this as non-fatal
                # (the contract write itself already succeeded).
                return state
            print(f"REFUSED: cannot advance run-state for {target}:")
            for b in blockers:
                print(f"  - {b}")
            return None

        stages = state["stages"]
        entry = next((s for s in stages if s["stage_name"] == stage), None)
        if entry is None:
            entry = {"stage_name": stage, "status": "pending", "started_at": None,
                      "completed_at": None, "attempt_count": 0, "backup_path": None}
            stages.append(entry)
        # A stage leaving `passed` un-grounds everything derived from it, so
        # note the demotion here (inside the lock, where the old status is
        # authoritative) and cascade after the lock is released -- the cascade
        # takes the same lock (Part 11I).
        if (entry.get("status") == "passed"
                and status in ("failed", "pending", "in_progress")):
            demoted_from = "passed"
        if status == "in_progress":
            entry["started_at"] = entry["started_at"] or now_iso()
            entry["attempt_count"] += 1
        if status in ("passed", "failed", "skipped"):
            entry["completed_at"] = now_iso()
        entry["status"] = status
        if backup_path:
            entry["backup_path"] = backup_path
        errors = _schema_check(state, "run_state.schema.json")
        if errors:
            print(f"FAIL: schema validation errors for {target} -- not written:")
            for e in errors:
                print(f"  - {e}")
            return None
        save_run_state(case_id, state)
        updated = state
    finally:
        release_lock(target)

    if demoted_from is not None:
        # A demotion whose cascade fails leaves dependents claiming `passed`
        # against a stage that no longer holds. The demotion itself is already
        # written, so this returns None (caller reports failure) rather than
        # handing back a state that looks successfully demoted (Part 11J).
        try:
            _invalidate_dependents(
                case_id, stage,
                f"upstream {stage} left {demoted_from} (now {status}) -- every "
                "dependent result was derived from a stage that no longer holds",
                held_by, run_id, strict=True)
        except CascadeFailed as exc:
            print(f"FAIL: {stage} was demoted to {status!r}, but its dependent "
                  f"stages could not be invalidated -- {exc}")
            print("  dependent stages may still show 'passed'; repeat this "
                  "update once the run-state is writable")
            return None
        # Re-read: the cascade may have demoted dependents after `updated` was
        # captured, and returning the pre-cascade snapshot would hand the
        # caller stages that are already invalidated.
        updated = load_run_state(case_id)
    return updated


def cmd_update_run_state(args):
    state = _update_run_state(args.case_id, args.run_id, args.stage, args.status, args.held_by)
    if state is None:
        return 1
    print(f"OK: {args.stage} -> {args.status}")
    return 0


def cmd_migrate_run_state_v03(args):
    """Migrate legacy v0.2 state into the v0.3 invariants through the DAO.

    A legacy pass without a P10 backup, or with unmet prerequisites, is not
    preserved or given a fabricated backup. It is downgraded to failed so the
    stage must genuinely rerun, and the change remains in migration_history.
    """
    target = run_state_path(args.case_id)
    existing_lock = acquire_lock_blocking(
        target, args.held_by, args.run_id, "migrate run-state to v0.3")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    try:
        state = load_run_state(args.case_id)
        state["run_id"] = args.run_id or state.get("run_id")
        changes = []
        for entry in state.get("stages", []):
            if entry.get("status") != "passed":
                continue
            stage = entry.get("stage_name")
            reasons = []
            if not entry.get("backup_path"):
                reasons.append("missing P10 backup_path")
            blockers = stage_dependencies.check_dependencies(
                stage, "passed", state, human_review_complete_any(args.case_id))
            if blockers:
                reasons.append("unmet dependencies: " + "; ".join(blockers))
            if reasons:
                entry["status"] = "failed"
                entry["completed_at"] = now_iso()
                entry["backup_path"] = None
                changes.append(
                    f"{stage}: passed->failed ({' | '.join(reasons)})")

        if not changes:
            errors = _schema_check(state, "run_state.schema.json")
            if errors:
                print(f"REFUSED: run-state has non-migratable schema errors in {target}:")
                for e in errors:
                    print(f"  - {e}")
                return 1
            print("NOOP: run-state already satisfies v0.3 invariants")
            return 0

        state.setdefault("migration_history", []).append({
            "migration_id": "run_state_v02_to_v03",
            "migrated_at": now_iso(),
            "held_by": args.held_by,
            "changes": changes,
        })
        errors = _schema_check(state, "run_state.schema.json")
        if errors:
            print(f"FAIL: migrated run-state is schema-invalid for {target} -- not written:")
            for e in errors:
                print(f"  - {e}")
            return 1
        save_run_state(args.case_id, state)
        print("OK: migrated run-state to v0.3:")
        for change in changes:
            print(f"  - {change}")
        return 0
    finally:
        release_lock(target)


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
        # Populate run_id from the arg (mirrors _update_run_state). Without this
        # a human-input write on a fresh case left run_id=None -> schema-invalid
        # state (fleet F2 root cause); the write is validated below regardless.
        state["run_id"] = run_id or state.get("run_id")
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
        # Validate before persisting, same fail-don't-persist contract as
        # _update_run_state (fleet F2: this writer was skipping the check, so
        # e.g. request-expert-review before run_id is set wrote an invalid
        # _run_state.json every downstream schema-gated writer would reject).
        errors = _schema_check(state, "run_state.schema.json")
        if errors:
            print("SCHEMA_FAIL: run_state.json invalid; not written:")
            for e in errors:
                print(f"  - {e}")
            return 1
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


def _build_snapshot_atomic(case_id: str, stage: str, prospective_state: dict) -> Path:
    """Build a full cumulative snapshot of a case's outputs (P10) and place it
    at its final _backups/step_<N>_<stage>/ path atomically.

    The copy is assembled into a sibling temp directory first; only once every
    file/dir has copied successfully is it moved into place with a single
    os.replace (atomic on the same filesystem). A crash mid-copy therefore
    leaves an orphan .tmp_snapshot_* dir -- never a half-populated real
    backup a restore could mistake for complete. Any pre-existing orphan for
    this exact destination is cleared first so a retry after a prior failure
    starts clean. Returns the final destination path.
    """
    src = case_dir(case_id)
    backups = src / "_backups"
    backups.mkdir(parents=True, exist_ok=True)
    # Published backups are immutable. Pick the first unused sequence number;
    # never delete or replace an existing destination.
    n = 1
    while (backups / f"step_{n:02d}_{stage}").exists():
        n += 1
    dest = backups / f"step_{n:02d}_{stage}"
    tmp = backups / f".tmp_snapshot_{stage}_{uuid.uuid4().hex}"
    tmp.mkdir(parents=True)
    try:
        for item in src.iterdir():
            if item.name in ("_backups",) or item.name.endswith(".lock"):
                continue
            if item.is_file():
                shutil.copy2(item, tmp / item.name)
            elif item.is_dir():
                shutil.copytree(item, tmp / item.name, dirs_exist_ok=True)
        # A restored snapshot must contain the finalized state, not the
        # pre-finalization state that was live when copying began.
        atomic_write_json(tmp / "_run_state.json", prospective_state)
        if dest.exists():
            raise FileExistsError(f"refusing to overwrite immutable backup {dest}")
        os.replace(tmp, dest)
    except Exception:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        raise
    return dest


def _finalize_stage(case_id, run_id, stage, held_by):
    """Atomically pass a stage: dependency-check -> build+validate snapshot ->
    record status=passed + backup_path + completed_at under one run-state lock.

    Order matters. Dependencies are checked FIRST (against current state,
    outside the eventual finalize lock -- a dependency can't un-pass between
    the check and the write, since nothing else passes stages concurrently in
    this single-writer-per-run model). The snapshot is built and confirmed on
    disk BEFORE any run-state field flips to passed, so a snapshot failure
    leaves the stage un-passed (P10: 'if a later stage crashes ... revert to
    the most recent intact step'). Returns the updated state, or None on any
    failure (dependency unmet, snapshot failure, lock contention, schema
    failure) -- in every None case the stage is NOT passed.
    """
    target = run_state_path(case_id)
    existing_lock = acquire_lock_blocking(
        target, held_by, run_id or "unknown", f"finalize stage: {stage}")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return None
    published = None
    try:
        # Keep the run-state lock from dependency check through snapshot
        # publication and the live state write, so no run-state transition can
        # interleave with finalization.
        state = load_run_state(case_id)
        blockers = stage_dependencies.check_dependencies(
            stage, "passed", state, human_review_complete_any(case_id))
        if blockers:
            print(f"REFUSED: cannot finalize {stage!r} -- unmet dependencies:")
            for b in blockers:
                print(f"  - {b}")
            return None

        if stage == "document_processing":
            manifest = read_contract_data(case_id, "document_manifest.json")
            if manifest is not None:
                mismatch = segment_lineage.check_classification_manifest_consistency(
                    manifest, _classification_reader(case_id),
                    require_complete=True)
                if mismatch:
                    print("REFUSED: cannot finalize 'document_processing' -- "
                          "manifest/classification inconsistency:")
                    for m in mismatch:
                        print(f"  - {m}")
                    return None
                lineage_errors = _validate_manifest_lineage(case_id, manifest)
                if lineage_errors:
                    print("REFUSED: cannot finalize 'document_processing' -- "
                          "segment-lineage errors:")
                    for e in lineage_errors:
                        print(f"  - {e}")
                    return None

        if stage == "policy_clause_processing":
            completion_blockers = _policy_completion_blockers(case_id)
            if completion_blockers:
                print("REFUSED: cannot finalize 'policy_clause_processing' -- "
                      "policy completeness gate is not clear:")
                for blocker in completion_blockers:
                    print(f"  - {blocker}")
                return None
        elif stage in stage_dependencies.dependents_of(
                "policy_clause_processing"):
            # P0-3. The dependency check above only asks whether
            # policy_clause_processing is RECORDED passed; that record can
            # predate canonical verification entirely. A stage built on top of
            # a legacy policy layer is a stage built on unverified UIDs, so the
            # live scheme is re-checked here rather than inferred from a past
            # status -- the same reasoning as Part 11F re-running source checks
            # at finalization instead of trusting the write-time pass.
            scheme_blockers = _policy_layer_scheme_blockers(
                case_id, f"finalizing {stage!r}")
            if scheme_blockers:
                print(f"REFUSED: cannot finalize {stage!r} -- the policy layer "
                      "it depends on is not canonically verified:")
                for blocker in scheme_blockers:
                    print(f"  - {blocker}")
                return None

        state["run_id"] = run_id or state.get("run_id")
        entry = next(
            (s for s in state["stages"] if s["stage_name"] == stage), None)
        if entry is None:
            entry = {
                "stage_name": stage, "status": "pending",
                "started_at": None, "completed_at": None,
                "attempt_count": 0, "backup_path": None,
            }
            state["stages"].append(entry)

        # Reserve the path while holding the run-state lock. The builder uses
        # the same first-unused rule, so the prospective state and snapshot
        # point at the identical immutable directory.
        backups = case_dir(case_id) / "_backups"
        n = 1
        while (backups / f"step_{n:02d}_{stage}").exists():
            n += 1
        reserved_dest = backups / f"step_{n:02d}_{stage}"
        entry["status"] = "passed"
        entry["completed_at"] = now_iso()
        entry["backup_path"] = str(reserved_dest)
        state["updated_at"] = now_iso()
        errors = _schema_check(state, "run_state.schema.json")
        if errors:
            print(f"FAIL: finalized run-state would be schema-invalid for {target} -- not written:")
            for e in errors:
                print(f"  - {e}")
            return None

        try:
            published = _build_snapshot_atomic(case_id, stage, state)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL: snapshot for stage {stage!r} could not be built -- "
                  f"stage NOT passed: {exc}")
            return None
        if not published.exists() or not published.is_dir():
            print(f"FAIL: snapshot at {published} was not published -- "
                  f"stage {stage!r} NOT passed")
            return None
        if published != reserved_dest:
            shutil.rmtree(published, ignore_errors=True)
            published = None
            print("FAIL: reserved snapshot path drifted during finalize -- "
                  "stage NOT passed")
            return None
        try:
            atomic_write_json(target, state)
        except Exception as exc:  # noqa: BLE001
            # This newly published directory was never recorded as a backup;
            # remove only the orphan. Existing backups are never touched.
            shutil.rmtree(published, ignore_errors=True)
            published = None
            print("FAIL: snapshot published but run-state write failed -- "
                  f"stage NOT passed: {exc}")
            return None
        return state
    finally:
        release_lock(target)


def cmd_snapshot_backup(args):
    """Backward-compatible alias for finalize-stage: snapshot + passed, atomic.
    Kept so existing callers/tests using 'snapshot-backup' keep working; new
    code should read this as 'finalize this stage'."""
    state = _finalize_stage(args.case_id, args.run_id, args.stage, args.held_by)
    if state is None:
        return 1
    entry = next(s for s in state["stages"] if s["stage_name"] == args.stage)
    print(f"OK: finalized {args.stage} -> passed, snapshot at {entry['backup_path']}")
    return 0


def cmd_finalize_stage(args):
    return cmd_snapshot_backup(args)


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
        errors = _schema_check(ledger, "conflict_ledger.schema.json")
        if errors:
            print(f"FAIL: schema validation errors for {target} -- not written:")
            for e in errors:
                print(f"  - {e}")
            return 1
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
        errors = _schema_check(ledger, "conflict_ledger.schema.json")
        if errors:
            print(f"FAIL: schema validation errors for {target} -- not written:")
            for e in errors:
                print(f"  - {e}")
            return 1
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


# --------------------------------------------------- human-review ledger --

def human_review_ledger_path(case_id: str) -> Path:
    return case_dir(case_id) / "_human_review_ledger.json"


def load_human_review_ledger(case_id: str) -> dict | None:
    """The DAO-only human-review ledger, or None if none exists yet. None is a
    real state -- a case with no human reviews recorded -- and every verifier
    treats a missing/None ledger as 'no such review', never as a pass."""
    return load_json(human_review_ledger_path(case_id))


def cmd_record_source_digest(args):
    """Record source_pdf_sha256 by hashing the registered raw file itself.

    The only write path for this field. It is the first input to every
    canonical UID, so accepting a caller-supplied value would let a UID be
    minted against a file the case never registered -- and a submitted digest
    that disagrees with the file on disk is refused rather than recorded,
    since the disagreement is the finding.
    """
    actual = registered_source_pdf_sha256(args.case_id, args.doc_id)
    if actual is None:
        print(f"BLOCKED: no readable registered raw source for {args.doc_id} "
              "-- source_pdf_sha256 is derived from the file itself and cannot "
              "be recorded without it")
        return 1
    if args.expect and args.expect != actual:
        print(f"REFUSED: submitted digest {args.expect!r} does not match the "
              f"registered raw source ({actual!r}) -- the file this document "
              "was extracted from is not the file the case registered")
        return 1

    target = case_dir(args.case_id) / "document_manifest.json"
    existing_lock = acquire_lock_blocking(
        target, args.held_by, args.run_id,
        f"record source digest for {args.doc_id}")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} "
              f"run_id={existing_lock['run_id']}")
        return 1
    try:
        manifest = json.loads(target.read_text(encoding="utf-8"))
        entry = next((d for d in manifest["documents"]
                      if d["document_id"] == args.doc_id), None)
        if entry is None:
            print(f"FAIL: {args.doc_id} is not in document_manifest.json")
            return 1
        recorded = entry.get("source_pdf_sha256")
        if recorded is not None and recorded != actual:
            print(f"REFUSED: {args.doc_id} already records "
                  f"{recorded!r}; the registered file now hashes to {actual!r} "
                  "-- an immutable raw source changed, which is a case-integrity "
                  "problem, not a field to overwrite")
            return 1
        entry["source_pdf_sha256"] = actual
        manifest["updated_at"] = now_iso()
        errors = _schema_check(manifest, "document_manifest.schema.json")
        if errors:
            print(f"FAIL: manifest would be schema-invalid -- not written:")
            for error in errors:
                print(f"  - {error}")
            return 1
        atomic_write_json(target, manifest)
        print(f"PASS: {args.doc_id} source_pdf_sha256 = {actual}")
        return 0
    finally:
        release_lock(target)


def cmd_enable_canonical_uids(args):
    """Activate canonical_v1 UID verification for one document.

    A dedicated, verified command rather than a writable field. Everything it
    checks is a precondition for canonical UIDs meaning anything:

      - the raw source must be readable and its digest recorded, because
        source_pdf_sha256 is the first identity input;
      - a source-text revision must be registered, because a UID is computed
        from registered bytes and verifying against unregistered text would
        re-derive whatever happens to be on disk;
      - for a segment, the page map must be present, because identity is keyed
        to the PHYSICAL page of the immutable parent, not the logical one.

    One-way: there is no disable. A verification that can be switched off is
    not a guarantee -- writing an arbitrary UID would only require turning it
    off first.
    """
    doc_id = args.doc_id
    manifest = read_contract_data(args.case_id, "document_manifest.json")
    if manifest is None:
        print("BLOCKED: document_manifest.json does not exist")
        return 1
    entry = next((d for d in manifest.get("documents", [])
                  if d.get("document_id") == doc_id), None)
    if entry is None:
        print(f"BLOCKED: {doc_id} is not a registered document")
        return 1

    blockers = []
    actual = registered_source_pdf_sha256(args.case_id, doc_id)
    recorded = entry.get("source_pdf_sha256")
    if actual is None:
        blockers.append(
            "the registered raw source is missing or unreadable -- "
            "source_pdf_sha256 is the first identity input of every canonical "
            "UID and cannot be derived without it")
    elif recorded is None:
        blockers.append(
            "source_pdf_sha256 is not recorded yet -- run record-source-digest "
            "first (the DAO hashes the registered file itself)")
    elif recorded != actual:
        blockers.append(
            f"recorded source_pdf_sha256 {recorded!r} does not match the "
            f"registered raw source ({actual!r})")

    revision_entry = revision_entry_for(args.case_id, doc_id)
    if revision_entry is None:
        blockers.append(
            "no source-text revision is registered -- a canonical UID is "
            "computed from registered bytes, and verifying against "
            "unregistered text would just re-derive whatever is on disk")

    if entry.get("document_role") == "segment" and not entry.get("page_map"):
        blockers.append(
            "this segment has no page_map -- canonical identity is keyed to "
            "the physical page of the immutable parent, which a segment "
            "cannot resolve without one")

    if blockers:
        print(f"BLOCKED: cannot enable canonical_v1 for {doc_id}:")
        for blocker in blockers:
            print(f"  - {blocker}")
        return 1

    target = revision_index_path(args.case_id)
    existing_lock = acquire_lock_blocking(
        target, args.held_by, args.run_id, f"enable canonical UIDs ({doc_id})")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} "
              f"run_id={existing_lock['run_id']}")
        return 1
    try:
        index = load_revision_index(args.case_id)
        doc_entry = next((d for d in index.get("documents", [])
                          if d.get("document_id") == doc_id), None)
        if doc_entry is None:
            # Re-read under the lock: the precondition pass above ran before
            # acquiring it, so the entry could have gone between the two.
            print(f"REFUSED: {doc_id} has no revision entry -- there is no "
                  "recorded state to transition, and activation may not "
                  "create one")
            return 1
        # Activation ALWAYS mutates a record that already exists, so a missing
        # uid_scheme here is damage rather than a fresh document. Passing that
        # distinction explicitly is what stops a corrupt entry being repaired
        # into canonical_v1 by the very command that grants verification.
        previous_scheme = doc_entry.get("uid_scheme")
        errors = source_provenance.scheme_transition_errors(
            previous_scheme, "canonical_v1", entry_exists=True)
        if errors:
            for error in errors:
                print(f"REFUSED: {error}")
            return 1
        doc_entry["uid_scheme"] = "canonical_v1"
        schema_errors = _schema_check(index, "revision_index.schema.json")
        if schema_errors:
            print("FAIL: revision index would be schema-invalid -- not written:")
            for error in schema_errors:
                print(f"  - {error}")
            return 1

        # P0-3's stale cascade, run BEFORE the scheme flip so a failure leaves
        # nothing half-transitioned. Every artifact this document's policy
        # stage published was written under legacy rules -- its UIDs were never
        # recomputed -- so the moment canonical verification switches on, the
        # policy stage and everything downstream of it are claims about
        # unverified identifiers and must stop standing. They are NOT rewritten
        # here: migrating an artifact is authoring work, and inventing UIDs on
        # a caller's behalf is the thing this whole layer exists to prevent.
        if previous_scheme != "canonical_v1":
            try:
                invalidated = _invalidate_policy_layer(
                    args.case_id,
                    f"canonical_v1 UID verification enabled for {doc_id}: "
                    "policy artifacts written under the legacy scheme were "
                    "never UID-verified and must be rewritten canonically",
                    args.held_by, args.run_id)
            except CascadeFailed as exc:
                print(f"FAIL: {doc_id} was NOT switched to canonical_v1 -- the "
                      f"policy stage and its downstream could not be "
                      f"invalidated: {exc}")
                print("  activating canonical UIDs while a stage still claims "
                      "'passed' against unverified artifacts would be exactly "
                      "the fail-open state this gate exists to prevent; "
                      "resolve the run-state lock and retry")
                return 1
        else:
            invalidated = []

        atomic_write_json(target, index)
        print(f"PASS: {doc_id} is now canonical_v1 -- every policy UID on this "
              "document is recomputed and must match. This cannot be undone.")
        if invalidated:
            print("INVALIDATED (legacy policy artifacts are no longer "
                  f"citable): {', '.join(sorted(invalidated))}")
            print("  rewrite this document's policy contracts with canonically "
                  "derived UIDs, then re-finalize policy_clause_processing.")
        return 0
    finally:
        release_lock(target)


def cmd_read_revision_index(args):
    print(json.dumps(load_revision_index(args.case_id),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_policy_snapshot(args):
    """Print the upstream_policy_snapshot value for the given documents.

    An agent writing a policy-referencing contract needs the exact digests the
    DAO will recompute at write time; making it derive them by hand would mean
    reading `outputs/` directly, which P2 forbids. So the DAO computes and
    prints them, and still verifies at write time -- printing here is a
    convenience, never the authority.
    """
    doc_ids = sorted(set(args.document_id))
    missing = [
        doc_id for doc_id in doc_ids
        if read_contract_data(
            args.case_id, f"normalized_policy_clause_{doc_id}.json") is None
    ]
    if missing:
        print(f"BLOCKED: no normalized policy contract for {', '.join(missing)} -- "
              "a snapshot may only be issued for documents the policy layer has "
              "actually produced")
        return 1
    print(json.dumps(policy_snapshot_for(args.case_id, doc_ids),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_read_human_review_ledger(args):
    ledger = load_human_review_ledger(args.case_id)
    if ledger is None:
        ledger = {"case_id": args.case_id, "records": []}
    print(json.dumps(ledger, ensure_ascii=False, indent=2))
    return 0


def cmd_record_human_review(args):
    """Record a genuine human decision, DAO-only (never write-contract). The
    invocation itself is trusted to be a human action (same model as
    mark-human-review-complete). What the DAO enforces structurally:

      - the target artifact must exist and pass its own schema first;
      - the review is bound to the artifact's CANONICAL (UID-stripped) bytes,
        which the DAO computes here -- the caller cannot supply a hash;
      - the target item (a finding_uid, or an unpaged physical page) must
        actually exist in that artifact and, for a finding, be the kind of
        thing a human accepts (a residual-risk acceptance).

    The returned review_uid is what the agent then writes into the artifact's
    human_review_uid field; because the hash is over the UID-stripped form,
    inserting it does not invalidate the record.
    """
    kind = args.artifact_kind
    if kind == "policy_audit_finding":
        artifact_name = f"policy_audit_result_{args.artifact_id}.json"
        schema_name = policy_audit.AUDIT_SCHEMA
    elif kind == "unpaged_physical_exclusion":
        artifact_name = f"policy_parent_coverage_{args.artifact_id}.json"
        schema_name = policy_completeness.PARENT_COVERAGE_SCHEMA
    else:
        print(f"FAIL: unknown artifact_kind {kind!r}")
        return 1

    artifact = read_contract_data(args.case_id, artifact_name)
    if artifact is None:
        print(f"FAIL: target artifact {artifact_name} does not exist -- nothing to review")
        return 1
    schemas, registry = load_registry()
    schema_errors = validate_instance(artifact, schema_name, schemas, registry)
    if schema_errors:
        print(f"FAIL: {artifact_name} does not pass its own schema; fix it before reviewing:")
        for e in schema_errors:
            print(f"  - {e}")
        return 1

    # The reviewed item must really exist in the artifact.
    if kind == "policy_audit_finding":
        finding = next(
            (f for f in artifact.get("findings") or []
             if f.get("finding_uid") == args.target_key), None)
        if finding is None:
            print(f"FAIL: finding_uid {args.target_key!r} is not present in {artifact_name}")
            return 1
        # The review is recorded against the finding's SUBSTANCE (bound via the
        # canonical, disposition-stripped hash), so it may be filed while the
        # finding is still 'open' -- the agent then flips status to
        # accepted_risk and writes the returned UID. The canonical hash is
        # invariant to that transition, so the record stays valid.
    else:  # unpaged_physical_exclusion
        if not args.target_key.startswith("physical:"):
            print("FAIL: target_key for unpaged_physical_exclusion must be 'physical:<N>'")
            return 1
        try:
            physical = int(args.target_key.split(":", 1)[1])
        except (ValueError, IndexError):
            print("FAIL: target_key must be 'physical:<integer>'")
            return 1
        page = next(
            (p for p in artifact.get("unpaged_physical_pages") or []
             if p.get("physical_page") == physical), None)
        if page is None:
            print(f"FAIL: unpaged physical page {physical} is not present in {artifact_name}")
            return 1

    artifact_sha = human_review.canonical_artifact_sha256(artifact)
    reviewed_at = now_iso()
    review_uid = human_review.compute_review_uid(
        args.case_id, kind, args.artifact_id, args.target_key,
        artifact_sha, reviewed_at)

    target = human_review_ledger_path(args.case_id)
    existing_lock = acquire_lock_blocking(
        target, args.held_by, args.run_id, f"record human review ({kind}:{args.target_key})")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    try:
        ledger = load_human_review_ledger(args.case_id)
        if ledger is None:
            ledger = {"case_id": args.case_id, "records": []}
        if human_review.find_record(ledger, review_uid) is not None:
            print(f"OK: identical review already recorded ({review_uid}) -- no change")
            return 0
        ledger["records"].append({
            "review_uid": review_uid,
            "reviewer": args.reviewer,
            "artifact_kind": kind,
            "artifact_id": args.artifact_id,
            "target_key": args.target_key,
            "artifact_sha256": artifact_sha,
            "decision": args.decision,
            "reviewed_at": reviewed_at,
            "note": args.note,
        })
        errors = _schema_check(ledger, human_review.HUMAN_REVIEW_LEDGER_SCHEMA)
        if errors:
            print(f"FAIL: schema validation errors for {target} -- not written:")
            for e in errors:
                print(f"  - {e}")
            return 1
        atomic_write_json(target, ledger)
    finally:
        release_lock(target)
    print(f"OK: recorded {review_uid} ({kind} {args.decision} on {args.target_key}). "
          f"Write this review_uid into the artifact's human_review_uid field.")
    return 0


# ------------------------------------------------------------------- main --

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("read-document-text"); p.add_argument("case_id"); p.add_argument("doc_id")
    p.set_defaults(fn=cmd_read_document_text)

    p = sub.add_parser("read-page-text"); p.add_argument("case_id"); p.add_argument("doc_id")
    p.add_argument("page", type=int)
    p.set_defaults(fn=cmd_read_page_text)

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

    p = sub.add_parser("patch-manifest-document")
    p.add_argument("case_id"); p.add_argument("doc_id")
    p.add_argument("--fields-file", required=True, help="JSON object of field:value updates merged into this document's entry")
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.add_argument("--purpose"); p.add_argument("--stage")
    p.set_defaults(fn=cmd_patch_manifest_document)

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

    p = sub.add_parser("check-forbidden-expressions"); p.add_argument("doc_path")
    p.set_defaults(fn=cmd_check_forbidden_expressions)

    p = sub.add_parser("update-run-state")
    p.add_argument("case_id"); p.add_argument("run_id"); p.add_argument("stage")
    # 'passed' is intentionally NOT a choice here: passing a stage is done by
    # finalize-stage (snapshot + passed, atomic). 'skipped' is for optional
    # stages only (stage_dependencies.SKIPPABLE_STAGES); the dependency
    # validator rejects it for any non-skippable stage.
    p.add_argument("status", choices=["pending", "in_progress", "failed", "skipped"])
    p.add_argument("--held-by", required=True)
    p.set_defaults(fn=cmd_update_run_state)

    p = sub.add_parser("migrate-run-state-v03")
    p.add_argument("case_id"); p.add_argument("run_id")
    p.add_argument("--held-by", required=True)
    p.set_defaults(fn=cmd_migrate_run_state_v03)

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

    p = sub.add_parser("finalize-stage")
    p.add_argument("case_id"); p.add_argument("run_id"); p.add_argument("stage")
    p.add_argument("--held-by", required=True)
    p.set_defaults(fn=cmd_finalize_stage)

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

    p = sub.add_parser("read-human-review-ledger"); p.add_argument("case_id")
    p.set_defaults(fn=cmd_read_human_review_ledger)

    p = sub.add_parser("record-source-digest")
    p.add_argument("case_id")
    p.add_argument("--doc-id", dest="doc_id", required=True)
    p.add_argument("--expect", default=None,
                   help="optional: fail if the registered file does not hash to this")
    p.add_argument("--held-by", dest="held_by", required=True)
    p.add_argument("--run-id", dest="run_id", required=True)
    p.set_defaults(fn=cmd_record_source_digest)

    p = sub.add_parser("enable-canonical-uids")
    p.add_argument("case_id")
    p.add_argument("--doc-id", dest="doc_id", required=True)
    p.add_argument("--held-by", dest="held_by", required=True)
    p.add_argument("--run-id", dest="run_id", required=True)
    p.set_defaults(fn=cmd_enable_canonical_uids)

    p = sub.add_parser("read-revision-index"); p.add_argument("case_id")
    p.set_defaults(fn=cmd_read_revision_index)

    p = sub.add_parser("policy-snapshot")
    p.add_argument("case_id")
    p.add_argument("--document-id", required=True, action="append",
                   help="referenced policy document; repeat for each one")
    p.set_defaults(fn=cmd_policy_snapshot)

    p = sub.add_parser("record-human-review")
    p.add_argument("case_id")
    p.add_argument("--artifact-kind", required=True,
                   choices=["policy_audit_finding", "unpaged_physical_exclusion"])
    p.add_argument("--artifact-id", required=True,
                   help="the DOC_ID of the audit / parent-coverage artifact")
    p.add_argument("--target-key", required=True,
                   help="finding_uid, or 'physical:<N>' for an unpaged page")
    p.add_argument("--decision", required=True,
                   choices=["accepted_risk", "verified", "rejected"])
    p.add_argument("--reviewer", required=True)
    p.add_argument("--note", required=True)
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_record_human_review)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
