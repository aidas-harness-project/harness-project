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
    read-page-text CASE_ID DOC_ID PAGE --caller-stage STAGE
    read-ground-truth CASE_ID --caller-stage STAGE --version {v1|v2}
        [--file GT_ID | --list]
    read-contract CASE_ID FILENAME
    check-segmentation-ready CASE_ID [--doc-id DOC_ID]
    set-segmentation-status CASE_ID DOC_ID {required|not_required}
        --reviewer NAME --held-by NAME --run-id RUN_ID [--note TEXT]
    write-contract CASE_ID FILENAME --data-file PATH --schema-name NAME
        [--run-id RUN_ID] [--stage STAGE]
    patch-manifest-document CASE_ID DOC_ID --fields-file PATH --held-by NAME --run-id RUN_ID
        [--stage STAGE]
    promote-policy-document CASE_ID DOC_ID --policy-processing-role ROLE
        --disputed-by TEXT --held-by NAME --run-id RUN_ID
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
    aggregate-trace CASE_ID --run-id RID --held-by NAME
        [--input-class {S-min|S|M|L|XL|embedded}] [--cold-or-warm {cold|warm}]
        (rolls this run's tools/trace.py span shards up into
         _timing_summary.json; run once, after the SLA end marker)
    read-timing-summary CASE_ID
    update-run-state CASE_ID RUN_ID STAGE STATUS --held-by NAME
        (STATUS in pending|in_progress|failed|skipped; 'passed' is refused here --
         a stage passes only via finalize-stage, atomically with its snapshot;
         failed accepts --attempt-outcome for T13/P9 diagnostics)
    migrate-run-state-v03 CASE_ID RUN_ID --held-by NAME
        (audited one-time repair for legacy dependency-invalid or
         passed-without-backup entries; invalid passes are downgraded, never
         given fabricated backups)
    reset-document-processing CASE_ID NEW_RUN_ID --held-by NAME
        --confirm-case-id CASE_ID
        (operator-requested cold Stage-2 restart. Moves existing Stage-2
        contracts to a recoverable project-local backup, resets the manifest
        to intake-owned fields, and issues a fresh run-state. Raw input and the
        reviewed source ledger are never touched.)
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
    register-segment-derivation CASE_ID --doc-id DOC_ID --pages SPEC
        --page-offset N --held-by NAME --run-id RUN_ID
        [--parent-document-id DOC_ID] [--page-map-file PATH]
        [--expect-parent-sha256 SHA]
        (P0-6, the ONLY writer of a segment's page_map. The DAO opens the
         registered parent PDF itself, extracts each physical page, hashes the
         text it read, and reads the printed page number off that page; a
         mapping is recorded only where the parent's own pages confirm it.
         --page-offset is a search hint, never a proof, and a submitted
         --page-map-file is compared, never stored. Issues a
         segment_page_map_v2 receipt into _segment_derivation_index.json and
         projects it into the manifest as one fail-closed transaction.)
    read-segment-derivation-index CASE_ID
    register-table-region CASE_ID --doc-id DOC_ID --page SEED --anchor TEXT
        --held-by NAME --run-id RUN_ID [--detector-profile NAME]
        (P0-8, the ONLY issuer of a table's authoritative source region. The
         DAO opens the registered PDF itself, runs a real table detector over
         the whole document, derives the table's full extent -- FOLLOWING
         continuations onto later pages itself -- and its row/header bands from
         the layout, then maps each band to exact offsets in the registered
         source-text revision via the PDF's own word geometry. --page is a SEED
         and --anchor a SELECTOR: they choose which detected candidate to start
         from and define nothing, so a caller cannot hide a continuation page
         by naming fewer pages, and an unresolvable continuation refuses rather
         than issuing a partial extent. Also records a candidate inventory of
         every table the scan found, so a detected table nobody extracted
         blocks finalization. Issues a table_region_v1 receipt into
         _table_region_index.json as one fail-closed transaction; a
         reference_table then cites it by table_region_receipt_id, and its
         source_regions must equal the derived extent exactly.)
    scan-table-candidates CASE_ID --doc-id DOC_ID --held-by NAME
        --run-id RUN_ID [--detector-profile NAME]
        (P0-8 follow-up 3. Records strict table candidates plus high-recall
         possible-table signals in a document, checksummed by scan_id. Separate from
         register-table-region on purpose: while the inventory was a side
         effect of registration, a document nobody registered a table for had
         NO scan, and the finalization gate read that absence as "nothing to
         check" -- so the way to hide a table was to never mention it. Every
         automated policy document must carry a current scan before
         policy_clause_processing may finalize, and finalization VERIFIES the
         scan rather than running one, so it stays a pure gate.)
    read-table-region-index CASE_ID
"""
import argparse
import functools
import hashlib
import json
import os
import hashlib
import platform
import re
import secrets
import shutil
import stat
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Mapping, NamedTuple

sys.stdout.reconfigure(encoding="utf-8")

from _validation import load_registry, validate_instance
import _cross_contract
import dao_transaction
import source_provenance
import stage_dependencies
import segment_lineage
import segment_derivation
import document_index
import table_region_provenance
import policy_completeness
import policy_uid
import policy_uid_resolver
import policy_audit
import policy_roles
import human_review
import llm_providers
import trace as trace_mod
import trace_aggregate

# Captured at import so a traced read can report part of the per-process cost it
# was charged before its own body began. Each CLI subcommand is its own process,
# so this is paid per call and is invisible to any span inside it. It is a LOWER
# BOUND -- the interpreter boot and the imports above this line are already spent
# by the time it runs. See _startup_seconds().
_PROCESS_START_WALL = time.time()

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
DATA = ROOT / "data"
FORBIDDEN_TEMPLATE = ROOT / "templates" / "forbidden-expressions.md"
KST = timezone(timedelta(hours=9))

MEDICAL_CONFIG_DIR = ROOT / "config" / "medical"
MEDICAL_STRUCTURING_CONFIG = MEDICAL_CONFIG_DIR / "medical_structuring_v0.1.json"
MEDICAL_PROJECTION_CONFIG = MEDICAL_CONFIG_DIR / "medical_projection_v0.1.json"
MEDICAL_REVIEW_ROLE_CONFIG = MEDICAL_CONFIG_DIR / "medical_review_roles_v0.1.json"
MEDICAL_REVIEW_REQUEST_CONFIG = (
    MEDICAL_CONFIG_DIR / "medical_review_request_v0.1.json"
)
MEDICAL_REFERRAL_POLICY_CONFIG = (
    MEDICAL_CONFIG_DIR / "medical_referral_policy_v0.1.json"
)

# --- POSIX/Windows portability for the medical write layer -------------------
# The medical-review modules were authored on Linux against O_DIRECTORY,
# O_NOFOLLOW and dir_fd-relative os.open/unlink/stat. Windows has none of the
# four (verified: os.supports_dir_fd is empty there), so the original code
# could not import, let alone run, on this machine -- which is why merge
# 3569d50 kept parent1's dao.py and the whole flow went dark.
#
# These constants degrade the FLAGS, never the CHECKS. The symlink and
# hardlink guards below are re-expressed with lstat-based equivalents, so a
# Windows run still refuses to follow a link or write through a shared inode;
# it just proves it with a different syscall. The one guarantee that is
# genuinely POSIX-only is directory-fsync durability, and that is reported
# honestly (see _fsync_directory) rather than silently skipped.
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_HAS_DIR_FD = os.open in os.supports_dir_fd and os.unlink in os.supports_dir_fd


def _fsync_directory(directory: Path) -> bool:
    """Best-effort parent-directory fsync. True when durability was confirmed.

    POSIX can fsync a directory handle; Windows cannot open one at all. The
    caller decides what an unconfirmed sync means -- this never pretends.
    """
    if not _O_DIRECTORY:
        return False
    try:
        fd = os.open(directory, os.O_RDONLY | _O_DIRECTORY)
    except OSError:
        return False
    try:
        os.fsync(fd)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def _is_private_regular_file(metadata) -> bool:
    """A managed file must be a regular file nobody else holds a link to.

    st_nlink is maintained on NTFS, so the hardlink half of this check is real
    on both platforms.
    """
    return stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1


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


class AtomicWriteCommittedError(OSError):
    """The destination was replaced, but directory durability was not confirmed."""


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Durably publish exact bytes without reformatting or a trailing newline.

    Ported from the medical branch. The POSIX original raised
    AtomicWriteCommittedError when the parent-directory fsync failed; on a
    platform with no directory handles at all there is nothing to fail, and
    raising on every single write would make the whole flow unusable. So the
    error keeps its exact meaning -- "the replace landed, durability did not" --
    and is raised only where that distinction is observable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if _O_DIRECTORY and not _fsync_directory(path.parent):
        raise AtomicWriteCommittedError(
            f"{path} was replaced but parent-directory durability was not confirmed"
        )


def atomic_create_bytes(path: Path, content: bytes) -> bool:
    """Durably create a file once; return False if it already exists.

    An existing file with identical bytes is a satisfied post-condition (the
    caller's retry), not a conflict. Differing bytes raise -- that is a real
    collision the caller must see.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    return atomic_create_bytes_in_directory(path.parent, path.name, content)


def atomic_create_json(path: Path, obj) -> bool:
    """Durably create a JSON file once; return False if it already exists."""
    return atomic_create_bytes(
        path,
        json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def atomic_create_bytes_in_directory(
    directory: Path,
    filename: str,
    content: bytes,
) -> bool:
    """Create or compare exact bytes for one private child of `directory`.

    The POSIX original pinned the directory with an O_DIRECTORY|O_NOFOLLOW
    handle and did every subsequent open/stat/unlink dir_fd-relative, so a
    symlink swapped in mid-call could not redirect the write. Windows offers
    no dir_fd, so the same guarantees are re-established per operation:

      * the directory itself must not be a symlink (lstat, not stat);
      * the created/inspected child must not be a symlink;
      * the child must be a regular file with st_nlink == 1, so no hardlink
        aliases the bytes we are about to trust.

    This is a narrower window than a pinned handle, not an equivalent one. It
    is the strongest check the platform allows, and the difference is recorded
    here rather than left for someone to rediscover.
    """
    if not filename or Path(filename).name != filename:
        raise ValueError(f"unsafe descriptor-relative filename: {filename!r}")
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError(
            f"cannot open managed directory without following links: {directory}"
        ) from exc
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(
            f"cannot open managed directory without following links: {directory}"
        )

    target = directory / filename
    try:
        fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
            0o600,
        )
    except FileExistsError:
        if target.is_symlink():
            raise ValueError(
                f"existing managed file is not a private regular file: {target}"
            )
        try:
            metadata = os.stat(target, follow_symlinks=False)
            existing = target.read_bytes()
        except OSError as exc:
            raise ValueError(
                f"cannot inspect existing managed file: {target}"
            ) from exc
        if not _is_private_regular_file(metadata):
            raise ValueError(
                f"existing managed file is not a private regular file: {target}"
            )
        if existing != content:
            raise ValueError(
                f"existing managed file content mismatch: {target}"
            )
        return False
    except OSError as exc:
        raise ValueError(f"cannot create managed file: {target}") from exc

    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    _fsync_directory(directory)
    return True


def remove_managed_file_if_content(
    directory: Path,
    filename: str,
    expected: bytes,
) -> bool:
    """Remove one private regular child only while its exact bytes still match.

    Every negative answer is a refusal to delete, never an exception: the
    caller is rolling back and must not be derailed by a file that already
    changed underneath it.
    """
    if not filename or Path(filename).name != filename:
        raise ValueError(f"unsafe descriptor-relative filename: {filename!r}")
    target = directory / filename
    try:
        if directory.is_symlink() or target.is_symlink():
            return False
        metadata = os.stat(target, follow_symlinks=False)
        if not _is_private_regular_file(metadata):
            return False
        if target.read_bytes() != expected:
            return False
        current = os.stat(target, follow_symlinks=False)
    except OSError:
        return False
    # Re-check identity: a swap between the read and the unlink would
    # otherwise delete a file whose contents we never verified.
    if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
        return False
    try:
        target.unlink()
    except OSError:
        return False
    _fsync_directory(directory)
    return True


def _restore_file_preimage(path: Path, preimage: bytes | None) -> None:
    """Restore one transaction participant exactly, under its existing lock.

    `None` means the file did not exist before the transition.  This helper is
    only used while rolling back a caught write failure; a hard process crash
    is represented by the durable pending journal instead.
    """
    if preimage is None:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".rollback{os.getpid()}")
    tmp.write_bytes(preimage)
    os.replace(tmp, path)


def processed_dir(case_id: str, doc_id: str) -> Path:
    _require_safe_id("case_id", case_id)
    _require_safe_id("doc_id", doc_id)
    return _require_within(DATA / "processed", case_id, doc_id)


# ---------------------------------------------------------------- locking --

def lock_path(target: Path) -> Path:
    return target.with_name(target.name + ".lock")


# A lock's owner, recorded so a waiter can tell a live holder from a dead one.
# `held_by` is a human label ("Claude", "Dev") and several processes legitimately
# share one, so it can never answer "is that holder still running?".
#
# The boot id is what makes the pid trustworthy. Pids are recycled, so a bare
# pid from a previous boot can match an unrelated live process and make a stale
# lock look held forever. Reclaim therefore requires the SAME machine and the
# SAME boot; anything else is left alone and waits out P5's cap as before.
_MACHINE_ID = f"{platform.node()}"


def _boot_id() -> str:
    """Identifies this boot of this machine. Falls back to the empty string
    when psutil is unavailable, which disables reclaim rather than guessing:
    a wrong reclaim breaks the mutual exclusion the lock exists to provide."""
    try:
        import psutil
    except ImportError:
        return ""
    try:
        return str(int(psutil.boot_time()))
    except Exception:
        return ""


def _lock_owner() -> dict:
    return {"machine": _MACHINE_ID, "pid": os.getpid(), "boot_id": _boot_id()}


def _owner_is_dead(owner) -> bool:
    """True only when the recorded owner is PROVABLY gone.

    Every uncertain case returns False (treat as held): a missing owner record
    (a lock written by an older version), a different machine or boot, no
    psutil, or any error interrogating the process. Waiting on an already-dead
    holder costs time; reclaiming a live holder's lock corrupts shared state.
    """
    if not isinstance(owner, dict):
        return False
    pid = owner.get("pid")
    if not isinstance(pid, int):
        return False
    if owner.get("machine") != _MACHINE_ID:
        return False
    boot = owner.get("boot_id")
    if not boot or boot != _boot_id():
        return False
    try:
        import psutil
    except ImportError:
        return False
    try:
        return not psutil.pid_exists(pid)
    except Exception:
        return False


def _reclaim_if_dead(target: Path, existing) -> bool:
    """Remove a lock whose owner is provably dead. Returns True if reclaimed.

    The unlink is guarded by the lock's own bytes: if the file changed between
    the staleness decision and the removal, somebody else acted on it first and
    this caller must not remove whatever is there now.
    """
    if not _owner_is_dead((existing or {}).get("owner")):
        return False
    lp = lock_path(target)
    try:
        current = lp.read_bytes()
    except FileNotFoundError:
        return True
    if json.loads(current.decode("utf-8")) != existing:
        return False
    try:
        lp.unlink()
    except FileNotFoundError:
        pass
    return True


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
                   "started_at": now_iso(), "purpose": purpose,
                   "owner": _lock_owner()}, f, ensure_ascii=False, indent=2)
    return None


# Windows holds a brief unlink-blocking handle whenever a waiter reads the
# lock file. Short and bounded on purpose: this runs on the success path of
# every write, so it must not become a stall of its own.
_RELEASE_RETRY_ATTEMPTS = 5
_RELEASE_RETRY_DELAY_SECONDS = 0.05


def release_lock(target: Path) -> None:
    """Release is atomic for the same reason acquire is: `exists()` then
    `unlink()` is a TOCTOU window, and a concurrent releaser landing inside it
    makes this raise FileNotFoundError. That crash surfaced for real on
    CASE_907 -- ten polarity workers, once the poll interval dropped to
    sub-second and commits actually overlapped -- and it aborts a caller that
    had already committed its write, so the work looks failed when it
    succeeded. Ask forgiveness, not permission: the post-condition wanted here
    is "no lock file remains", and another process having already removed it
    satisfies that.

    Windows adds a second way to lose that race. `read_lock` opens this file on
    every poll, and while any handle is open `unlink` raises PermissionError
    (WinError 32) instead of succeeding. Letting that escape is strictly worse
    than the FileNotFoundError above: the releaser dies AFTER committing its
    write and LEAVES THE LOCK BEHIND, so every other worker on a shared
    case-level file blocks on an owner that no longer exists. That is exactly
    how CASE_140 deadlocked -- one DOC_002 writer hit this and stalled the
    DOC_001/DOC_003 workers, whose own work was entirely independent.

    A brief retry covers the poll-read window, which is short by construction.
    If the file still cannot be removed, the lock is left for the dead-owner
    reclaim to collect rather than killing a caller whose write already
    succeeded -- releasing is cleanup, and failing it must not fail the work."""
    lp = lock_path(target)
    for attempt in range(_RELEASE_RETRY_ATTEMPTS):
        try:
            lp.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == _RELEASE_RETRY_ATTEMPTS - 1:
                break
            time.sleep(_RELEASE_RETRY_DELAY_SECONDS)


# P5's mid-run poll-and-wait cadence -- module-level, not bound into a
# function default, so tests can monkeypatch dao.LOCK_POLL_INTERVAL_SECONDS
# / dao.LOCK_MAX_WAIT_SECONDS to tiny values instead of a test waiting 15
# real minutes to see a timeout.
#
# 30s suits what P5 was written for: a lock held by a person or a long stage,
# where polling faster only burns cycles. It is badly wrong for a parallel
# batch of short commits -- `analyze-policy-polarity` holds the index lock for
# milliseconds to append one receipt, so N concurrent workers serialise at 30s
# apiece and wait orders of magnitude longer than the work takes (measured on
# CASE_907: ten workers, nine cache hits, 4.5 minutes of pure polling).
# Overridable per deployment rather than lowered outright, mirroring
# llm_providers.compare_text_timeout: the human-gate cadence stays as designed
# and a batch driver is a config change, not a patch.
LOCK_POLL_INTERVAL_ENV = "HARNESS_LOCK_POLL_INTERVAL_SECONDS"
_LOCK_POLL_INTERVAL_DEFAULT = 30.0


def _lock_poll_interval(env: Mapping[str, str] | None = None) -> float:
    """Resolve the poll interval. Invalid or non-positive values fall back to
    the default rather than raising -- a malformed interval must not turn every
    lock wait into a hard configuration failure, and a zero or negative value
    would spin."""
    source = os.environ if env is None else env
    raw = str(source.get(LOCK_POLL_INTERVAL_ENV, "")).strip()
    if not raw:
        return _LOCK_POLL_INTERVAL_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return _LOCK_POLL_INTERVAL_DEFAULT
    return value if value > 0 else _LOCK_POLL_INTERVAL_DEFAULT


LOCK_POLL_INTERVAL_SECONDS = _lock_poll_interval()
LOCK_MAX_WAIT_SECONDS = 900


def acquire_lock_blocking(target: Path, held_by: str, run_id: str, purpose: str):
    """Like acquire_lock, but waits for an existing lock to clear instead of
    failing immediately -- see the module docstring. Returns None on success
    (lock acquired -- everything after this point is reading fresh state,
    nothing else could have written since), or the lock dict still held once
    LOCK_MAX_WAIT_SECONDS is exceeded (same failure contract as acquire_lock).
    """
    # The highest-value span in the DAO: wait_s and poll_count together are
    # the whole contention story, and the repo's own comment above records 4.5
    # minutes of pure polling on CASE_907 without any way to see it in
    # aggregate. lock_kind carries the target's basename (a filename, not a
    # path -- trace's allow-list sanitises it to a bounded identifier) so a
    # rollup can say WHICH file serialises the run.
    with trace_mod.span("lock.acquire", category="lock",
                        lock_kind=target.name) as sp:
        waited = 0.0
        polls = 0
        reclaimed = 0
        while True:
            existing = acquire_lock(target, held_by, run_id, purpose)
            if existing is None:
                sp.set(wait_s=waited, poll_count=polls, acquired=True,
                       reclaimed_dead_locks=reclaimed)
                return None
            # A holder that died without releasing would otherwise hold this
            # target for the full cap and, through a shared case-level file,
            # stall every other worker behind it. Reclaim only when the owner
            # is provably gone, then retry the atomic create immediately --
            # winning that create is still what grants the lock, so two
            # waiters reclaiming at once cannot both proceed.
            if _reclaim_if_dead(target, existing):
                reclaimed += 1
                continue
            if waited >= LOCK_MAX_WAIT_SECONDS:
                sp.set(wait_s=waited, poll_count=polls, acquired=False)
                sp.set_status("error")
                return existing
            time.sleep(LOCK_POLL_INTERVAL_SECONDS)
            waited += LOCK_POLL_INTERVAL_SECONDS
            polls += 1


# --- owned-lock shim over this branch's O_EXCL lock model --------------------
# The medical-review modules call acquire_owned_lock[_blocking]/
# release_owned_lock, which on the Linux branch were fcntl.flock handles that
# retained kernel ownership and deliberately never unlinked the lock pathname
# (so a forked child could not release its parent's lock by pathname alone).
#
# Two reasons this is a shim rather than a port:
#   1. fcntl does not exist on Windows, and this branch's O_EXCL +
#      dead-owner-reclaim model is the one the user chose to keep (8d818ff,
#      fleet-review TOCTOU fix; decision recorded 2026-08-14).
#   2. Nothing here forks worker processes -- fork_case.py copies directories,
#      it does not fork -- so the fork-safety the owned-lock semantics bought
#      has no situation to protect in this codebase.
#
# The handle is opaque at all four call sites (verified: no attribute access on
# the returned object anywhere in tools/medical_*.py), so carrying the target
# is sufficient. The (lock, existing) tuple contract is preserved exactly.


class _OwnedPathLock:
    """Opaque handle pairing an acquired lock with the target it guards."""

    def __init__(self, target: Path):
        self.target = target
        self.released = False


def acquire_owned_lock(target: Path, held_by: str, run_id: str, purpose: str):
    """Non-blocking acquire. Returns (handle, None) or (None, existing_lock)."""
    existing = acquire_lock(target, held_by, run_id, purpose)
    if existing is not None:
        return None, existing
    return _OwnedPathLock(target), None


def acquire_owned_lock_blocking(target: Path, held_by: str, run_id: str,
                                purpose: str):
    """Blocking acquire with the same tuple contract as acquire_owned_lock."""
    existing = acquire_lock_blocking(target, held_by, run_id, purpose)
    if existing is not None:
        return None, existing
    return _OwnedPathLock(target), None


def release_owned_lock(lock: "_OwnedPathLock") -> None:
    """Idempotent release. Double-release is a no-op, matching the original."""
    if lock is None or lock.released:
        return
    release_lock(lock.target)
    lock.released = True


# ------------------------------------------------------------- run-state --

def run_state_path(case_id: str) -> Path:
    return case_dir(case_id) / "_run_state.json"


def load_run_state(case_id: str) -> dict:
    p = run_state_path(case_id)
    existing = load_json(p)
    if existing is not None:
        return _normalize_legacy_run_state(case_id, existing)
    return {
        "run_state_version": "run_state.v0.3",
        "case_id": case_id,
        "run_id": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "stages": [],
        "human_input_status": [],
        "medical_review_adopted": False,
        # A run state created from here on is born under the restored gate.
        "medical_gate_status": "enforced",
    }


def _normalize_legacy_run_state(case_id: str, state: dict) -> dict:
    """Give a pre-v0.3 run state the two fields the schema now requires.

    `run_state.schema.json` requires `run_state_version` and
    `medical_review_adopted`, but the run-state writer on this branch predates
    both (merge 3569d50 kept parent1's dao.py while the schema moved on), so a
    freshly created run state failed its own validation and no new case could
    open a stage at all. Transplanted from 633bd7b0.

    `medical_review_adopted` is derived from what is on disk rather than
    defaulted: a case that already carries medical-review artifacts has adopted
    the flow whether or not anything recorded the fact. The legacy Evaluation
    stage rename is kept as written -- a wait whose draft version cannot be
    read unambiguously raises rather than guessing which version it belonged to.
    """
    if "run_state_version" in state:
        return _stamp_medical_gate_status(case_id, state)
    state = json.loads(json.dumps(state))
    evaluation_versions = set()
    for entry in state.get("human_input_status", []):
        if entry.get("stage_name") != "evaluation":
            continue
        match = re.search(
            r"draft_report_v([12])(?:_reviewed)?\.md",
            entry.get("description", ""),
        )
        if match is None:
            raise ValueError(
                "legacy Evaluation wait has no unambiguous draft version")
        version = match.group(1)
        evaluation_versions.add(version)
        entry["stage_name"] = f"human_review_v{version}"
    for entry in state.get("stages", []):
        if entry.get("stage_name") != "evaluation":
            continue
        if len(evaluation_versions) != 1:
            raise ValueError(
                "legacy Evaluation stage has no unique human-review version")
        entry["stage_name"] = f"human_review_v{next(iter(evaluation_versions))}"
    state["run_state_version"] = "run_state.v0.3"
    state["medical_review_adopted"] = _medical_artifacts_present(case_id)
    return _stamp_medical_gate_status(case_id, state)


def _medical_artifacts_present(case_id: str) -> bool:
    """True when the case already carries medical-review state on disk.

    A symlink counts: the question is whether the name is claimed, not whether
    it resolves, so a dangling link cannot make an adopted case look unadopted.
    """
    directory = case_dir(case_id)
    return any(
        path.exists() or path.is_symlink()
        for path in (
            directory / "medical_variables.json",
            directory / "_medical_review_ledger.json",
            directory / "_medical_variable_revisions",
        )
    )


def _medical_gate_applies(state: dict) -> bool:
    """Whether the restored medical clearance gate governs this run.

    Scoped forward on purpose (user decision, 2026-08-14). Between merge
    3569d50 and the restore the gate did not exist, so 9 cases have
    claim_analysis 'passed' without it. Enforcing retroactively would mark
    finished runs -- several with recorded evaluation results -- as failed for
    a check that was not running when they executed, which would rewrite
    history rather than fix it.

    A run state carrying no `medical_gate_status` predates the restore and is
    NOT gated. It is stamped 'never_evaluated' on first touch so the record
    says the question was never asked, rather than leaving
    `medical_review_adopted: false` to be misread as 'asked and not required'.
    """
    return state.get("medical_gate_status") == "enforced"


def _stamp_medical_gate_status(case_id: str, state: dict) -> dict:
    """Stamp a pre-restore run state exactly once, without gating it."""
    if state.get("medical_gate_status") is None:
        state["medical_gate_status"] = "never_evaluated"
    return state


# --- medical-review paths and revision loading -------------------------------
# Thin delegations to tools/medical_repository.py and
# tools/medical_review_ledger.py, which survived merge 3569d50 intact. The
# adjudication logic lives there and is deliberately NOT reimplemented here --
# these restore the DAO-side entry points those modules call back into.


def medical_variables_path(case_id: str) -> Path:
    import medical_repository

    return medical_repository.medical_variables_path(
        sys.modules[__name__], case_id)


def medical_variable_revisions_dir(case_id: str) -> Path:
    import medical_repository

    return medical_repository.revisions_dir(sys.modules[__name__], case_id)


def medical_review_ledger_path(case_id: str) -> Path:
    from medical_review_ledger import ledger_path

    return ledger_path(sys.modules[__name__], case_id)


def _load_medical_revision(
    case_id: str, revision_sha: str | None
) -> tuple[dict | None, str | None]:
    import medical_repository

    return medical_repository.load_revision(
        sys.modules[__name__], case_id, revision_sha
    )


def load_medical_review_ledger(case_id: str) -> dict:
    from medical_review_ledger import load_ledger

    return load_ledger(sys.modules[__name__], case_id)


def _reconciliation_request(
    case_id: str,
    run_id: str,
    operation_id: str,
    ledger_sha256: str,
) -> dict:
    request = {
        "case_id": case_id,
        "run_id": run_id,
        "operation_id": operation_id,
    }
    if operation_id.startswith("medical-projection:"):
        request["medical_review_ledger_sha256"] = ledger_sha256
    return request


def _reconciliation_receipt_sha(operation: dict) -> str:
    fields = {
        key: operation[key]
        for key in (
            "operation_id",
            "request_sha256",
            "medical_review_ledger_sha256",
            "completed_at",
        )
    }
    return hashlib.sha256(_canonical_json_bytes(fields)).hexdigest()


def save_run_state(case_id: str, state: dict) -> None:
    state["updated_at"] = now_iso()
    atomic_write_json(run_state_path(case_id), state)


_DOCUMENT_PROCESSING_OUTPUT_PATTERNS = (
    "ocr_result_*.json",
    "classification_result_*.json",
    "redaction_result_*.json",
    "segmentation_proposal_*.json",
    "page_chunks.json",
)

_DOCUMENT_PROCESSING_DERIVED_MANIFEST_FIELDS = frozenset({
    "source_total_pages", "extraction_method", "downstream_disposition",
    "non_text_verification", "segmentation_proposal_path",
    "provisional_document_type", "provisional_type_label",
    "source_file_name", "source_page_start", "source_page_end",
    "document_role", "source_document_id", "page_map",
})


def _intake_state_document(document: dict) -> dict:
    """Return one physical intake document with Stage-2 projections cleared.

    A segmented child cannot be reconstructed from intake-owned fields alone,
    so the reset command refuses such cases before reaching this helper.
    Unknown non-Stage-2 fields are preserved; only fields whose owner is
    document_processing/segmentation are removed or reset.
    """
    reset = {
        key: value for key, value in document.items()
        if key not in _DOCUMENT_PROCESSING_DERIVED_MANIFEST_FIELDS
    }
    reset.update({
        "pages": None,
        "ocr_status": "pending",
        "segmentation_status": (
            "pending_review" if document.get("file_format") == "pdf"
            else "not_applicable"),
        "segmentation_reviewed_by": None,
        "segmentation_reviewed_at": None,
        "segmentation_review_note": None,
        "ocr_text_path": None,
        "ocr_quality": None,
        "uncertain_region_count": None,
        "cross_validation_status": None,
        "redacted_text_path": None,
        "document_type": None,
        "classification_confidence": None,
    })
    return reset


def reset_document_processing(case_id: str, new_run_id: str, held_by: str,
                              confirm_case_id: str) -> dict:
    """Recoverably reset Stage 2 after an explicit operator restart request.

    Existing contracts are MOVED, not deleted, to
    ``_stage2_reset_backups/<case>/<old>_to_<new>_<timestamp>/outputs``.
    Processed page text is deliberately left in place: checkpoint 1 rewrites
    every page through the DAO, while removing it here would require locking
    an unbounded file set and would make the reset less recoverable.  The
    authoritative OCR/classification/redaction contracts are removed, so
    run_checkpoint1 cannot take its ``already_extracted`` short-circuit.
    """
    _require_safe_id("case_id", case_id)
    _require_safe_id("run_id", new_run_id)
    if confirm_case_id != case_id:
        return {"status": "refused", "reason": (
            f"--confirm-case-id must exactly equal {case_id}")}

    case = case_dir(case_id)
    manifest_target = case / "document_manifest.json"
    state_target = run_state_path(case_id)
    if not manifest_target.exists():
        return {"status": "refused", "reason": "document_manifest.json is missing"}

    artifact_paths: list[Path] = []
    for pattern in _DOCUMENT_PROCESSING_OUTPUT_PATTERNS:
        artifact_paths.extend(path for path in case.glob(pattern) if path.is_file())
    artifact_paths = sorted(set(artifact_paths), key=lambda path: path.name)
    lock_targets = [manifest_target, state_target, *artifact_paths]

    # A restart is a run-start operation: any pre-existing lock halts
    # immediately. It must never enter the 15-minute mid-run polling loop.
    for target in lock_targets:
        existing = read_lock(target)
        if existing is not None:
            return {"status": "refused", "reason": "pre-existing lock",
                    "target": str(target), "lock": existing}

    acquired: list[Path] = []
    try:
        for target in lock_targets:
            existing = acquire_lock(
                target, held_by, new_run_id,
                f"cold reset document_processing for {case_id}")
            if existing is not None:
                return {"status": "refused", "reason": "lock race",
                        "target": str(target), "lock": existing}
            acquired.append(target)

        # Fresh reads happen only after all reset-owned targets are locked.
        manifest = load_json(manifest_target)
        old_state = load_json(state_target) or load_run_state(case_id)
        documents = (manifest or {}).get("documents", [])
        non_physical = [
            doc.get("document_id") for doc in documents
            if doc.get("document_role") == "segment"
            or doc.get("source_file_name")
            or doc.get("downstream_disposition") == "superseded_bundle"
        ]
        if non_physical:
            return {"status": "refused", "reason": (
                "case already contains segmented/superseded documents; cold "
                "reset cannot reconstruct intake state safely"),
                "documents": non_physical}

        reset_manifest = dict(manifest)
        reset_manifest["created_at"] = now_iso()
        reset_manifest["updated_at"] = now_iso()
        reset_manifest["documents"] = [
            _intake_state_document(doc) for doc in documents]
        reset_state = {
            "case_id": case_id,
            "run_id": new_run_id,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "stages": [],
            "human_input_status": [],
        }

        manifest_errors = _schema_check(
            reset_manifest, "document_manifest.schema.json")
        state_errors = _schema_check(reset_state, "run_state.schema.json")
        if manifest_errors or state_errors:
            return {"status": "refused", "reason": "reset state failed schema validation",
                    "manifest_errors": manifest_errors,
                    "run_state_errors": state_errors}

        old_run_id = old_state.get("run_id") or "NO_RUN"
        stamp = datetime.now(KST).strftime("%Y%m%dT%H%M%S")
        backup = _require_within(
            ROOT / "_stage2_reset_backups", case_id,
            f"{old_run_id}_to_{new_run_id}_{stamp}")
        output_backup = backup / "outputs"
        output_backup.mkdir(parents=True, exist_ok=False)
        shutil.copy2(manifest_target, output_backup / manifest_target.name)
        if state_target.exists():
            shutil.copy2(state_target, output_backup / state_target.name)
        for artifact in artifact_paths:
            shutil.move(str(artifact), str(output_backup / artifact.name))

        stale_scratch = case / "_stage2_scratch"
        if stale_scratch.exists():
            shutil.move(str(stale_scratch), str(output_backup / stale_scratch.name))

        atomic_write_json(manifest_target, reset_manifest)
        atomic_write_json(state_target, reset_state)
        return {
            "status": "reset",
            "case_id": case_id,
            "old_run_id": old_run_id,
            "new_run_id": new_run_id,
            "backup_path": str(backup),
            "contracts_archived": [path.name for path in artifact_paths],
            "processed_text_retained_for_overwrite": True,
        }
    finally:
        for target in reversed(acquired):
            release_lock(target)


def cmd_reset_document_processing(args):
    result = reset_document_processing(
        args.case_id, args.new_run_id, args.held_by, args.confirm_case_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "reset" else 1


# ------------------------------------------------------------------ nouns --


def traced_read(op: str):
    """Instrument a read subcommand so its cost stops landing in unattributed time.

    Read paths were the largest measurement hole T13 exposed. On CASE_910 the
    `claim_analysis` attempt measured 394.96s active wall, of which instrumented
    tool spans explained 0.034s -- and the agent had made 41 DAO calls, almost
    all of them reads. dao.py instrumented exactly two things (`lock.acquire`,
    `validate.schema`), both on write paths, so a stage that only reads looked
    free while occupying the entire interval.

    Two costs are recorded separately because they have different fixes:
    `duration_s` is the work inside the command, while `startup_s` is the
    interpreter-plus-import time paid before this function could run at all
    (measured at ~0.30s of a ~0.38s median `read-contract` call -- with 41 calls
    that is 12-16s of a stage's wall clock, and no amount of optimising the read
    body touches it).

    A read command that fails to record is a no-op, never an error: tracing is
    diagnostic, and P2's read paths must not acquire a failure mode they did not
    have before.
    """
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(args):
            if not trace_mod.enabled():
                return fn(args)
            with trace_mod.span(op, category="io",
                                case_id=getattr(args, "case_id", None),
                                doc_id=getattr(args, "doc_id", None)) as sp:
                rc = fn(args)
                try:
                    sp.set(exit_code=int(rc or 0), startup_s=_startup_seconds())
                except Exception:  # noqa: BLE001 -- never break a read
                    pass
                return rc
        return wrapper
    return decorate


def _startup_seconds() -> float:
    """Seconds from this module's import to the traced read, per process.

    Deliberately NOT the full interpreter startup: the clock starts when dao.py
    is imported, so the interpreter boot and dao's own import chain that ran
    BEFORE that line are excluded and this is a lower bound. Measuring the true
    process start needs psutil, which is not worth a DAO dependency for a
    diagnostic.

    The gap is not small and should not be read as noise. Externally timed, a
    `read-contract` subprocess takes ~0.376s median while bare `import dao`
    alone accounts for ~0.298s of it -- so a per-call figure of ~0.01s here
    means the remaining ~0.29s was spent before this clock started. The honest
    statement is that per-call CLI overhead is bounded below by this field and
    measured externally at roughly 0.3s.
    """
    return round(max(time.time() - _PROCESS_START_WALL, 0.0), 6)


@traced_read("dao.read_document_text")
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
        # A superseded bundle's redacted_text.md still sits on disk -- the split
        # hands each child its slice but does not delete the parent's copy, and
        # the manifest records the parent's redacted_text_path as null to say
        # so. Serving the file anyway made the manifest's statement advisory:
        # every page of it is ALSO a page of some child, so an analysis stage
        # reading both sees one page twice and can count it as two independent
        # sources agreeing. Found by consistency-check on CASE_909, which
        # excluded DOC_005 by hand because the DAO would not.
        if entry and entry.get("downstream_disposition") == "superseded_bundle":
            print(
                f"SUPERSEDED_BUNDLE: {args.doc_id} was split into per-document "
                "children, which own its pages now. Its text is retained for "
                "provenance only -- reading it alongside its children would "
                "double-count the same page as two sources. Read the children "
                "instead."
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


def _redacted_bundle_document(case_id: str, manifest_entry: dict) -> dict:
    """Return one driver-safe redacted document payload.

    This helper is deliberately DAO-local: downstream drivers receive content
    and revision binding, never a processed-layer pathname they could reopen.
    """
    doc_id = manifest_entry["document_id"]
    disposition = manifest_entry.get("downstream_disposition")
    if disposition == "expert_review_only":
        raise ValueError(
            f"NON_TEXT_EXPERT_REVIEW_ONLY: {doc_id} has no automated text input")
    if disposition == "superseded_bundle":
        raise ValueError(
            f"SUPERSEDED_BUNDLE: {doc_id} is provenance-only; read its active children")
    redacted = processed_dir(case_id, doc_id) / "redacted_text.md"
    if not redacted.exists():
        raise ValueError(f"NOT_EXTRACTED: {doc_id} has no redacted text")
    try:
        source = redacted.read_text(encoding="utf-8")
        pages = _cross_contract.split_pages(source)
    except Exception as exc:
        raise ValueError(f"UNREADABLE: {doc_id}: {exc}") from exc
    revision = revision_entry_for(case_id, doc_id) or {}
    return {
        "document_id": doc_id,
        # These are provenance fields, not paths. Denial-response uses them to
        # join split siblings before deciding which bundle to send to a worker.
        "source_file_name": manifest_entry.get("source_file_name") or manifest_entry.get("file_name"),
        "source_page_start": manifest_entry.get("source_page_start"),
        "source_page_end": manifest_entry.get("source_page_end"),
        "document_type": manifest_entry.get("document_type"),
        "source_text_revision_sha256": revision.get("current_revision_sha256"),
        "redacted_text_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "pages": [
            {"page": page, "text": pages[page]}
            for page in sorted(pages)
        ],
    }


def read_redacted_text_bundle_data(case_id: str, doc_ids: list[str]) -> dict:
    """DAO-owned content bundle for drivers; never returns processed paths."""
    if not doc_ids:
        raise ValueError("at least one --doc-id is required")
    if len(set(doc_ids)) != len(doc_ids):
        raise ValueError("duplicate --doc-id is not permitted")
    manifest = read_contract_data(case_id, "document_manifest.json")
    if manifest is None:
        raise ValueError(f"NOT_FOUND: {case_id} has no document_manifest.json")
    by_id = {entry.get("document_id"): entry for entry in manifest.get("documents", [])}
    documents = []
    for doc_id in doc_ids:
        entry = by_id.get(doc_id)
        if not isinstance(entry, dict):
            raise ValueError(f"UNKNOWN_DOCUMENT: {doc_id} is not in document_manifest.json")
        documents.append(_redacted_bundle_document(case_id, entry))
    return {"case_id": case_id, "documents": documents}


@traced_read("dao.read_redacted_text_bundle")
def cmd_read_redacted_text_bundle(args):
    try:
        print(json.dumps(
            read_redacted_text_bundle_data(args.case_id, args.doc_id),
            ensure_ascii=False,
        ))
        return 0
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 1


def _verify_driver_reference(case_id: str, reference: dict,
                             bundle_by_id: dict[str, dict]) -> dict:
    if not isinstance(reference, dict):
        raise ValueError("reference must be an object")
    doc_id = reference.get("document_id")
    page = reference.get("page")
    quote = reference.get("quote")
    if not isinstance(doc_id, str) or not doc_id:
        raise ValueError("reference.document_id must be a non-empty string")
    if not isinstance(page, int) or page < 1:
        raise ValueError(f"{doc_id}: reference.page must be a positive integer")
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError(f"{doc_id}: reference.quote must be non-empty")
    document = bundle_by_id.get(doc_id)
    if document is None:
        raise ValueError(f"{doc_id}: reference document was not included in this bundle")
    expected_revision = reference.get("source_text_revision_sha256")
    actual_revision = document["source_text_revision_sha256"]
    if expected_revision is not None and expected_revision != actual_revision:
        raise ValueError(f"{doc_id}: source_text_revision_sha256 is stale")
    expected_digest = reference.get("redacted_text_sha256")
    if expected_digest is not None and expected_digest != document["redacted_text_sha256"]:
        raise ValueError(f"{doc_id}: redacted_text_sha256 is stale")
    page_text = next((item["text"] for item in document["pages"]
                      if item["page"] == page), None)
    if page_text is None:
        raise ValueError(f"{doc_id}: page {page} is not present in processed text")
    start = reference.get("start_char")
    end = reference.get("end_char")
    if (start is None) != (end is None):
        raise ValueError(f"{doc_id}: start_char and end_char must be supplied together")
    if start is not None:
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or start >= end:
            raise ValueError(f"{doc_id}: invalid start_char/end_char range")
        if end > len(page_text) or page_text[start:end] != quote:
            raise ValueError(f"{doc_id}: quote does not exactly match the claimed page range")
    elif _cross_contract._normalize_ws(quote) not in _cross_contract._normalize_ws(page_text):
        raise ValueError(f"{doc_id}: quote is not present on page {page}")
    verified = {
        "document_id": doc_id,
        "page": page,
        "quote": quote,
        "source_text_revision_sha256": actual_revision,
        "redacted_text_sha256": document["redacted_text_sha256"],
    }
    if start is not None:
        verified.update({"start_char": start, "end_char": end})
    return verified


@traced_read("dao.verify_evidence_references")
def cmd_verify_evidence_references(args):
    try:
        payload = json.loads(Path(args.references_file).read_text(encoding="utf-8"))
        references = payload.get("references") if isinstance(payload, dict) else None
        if not isinstance(references, list) or not references:
            raise ValueError("references file must be an object with a non-empty references list")
        doc_ids = []
        for reference in references:
            doc_id = reference.get("document_id") if isinstance(reference, dict) else None
            if not isinstance(doc_id, str) or not doc_id:
                raise ValueError("every reference must name document_id")
            if doc_id not in doc_ids:
                doc_ids.append(doc_id)
        bundle = read_redacted_text_bundle_data(args.case_id, doc_ids)
        documents = {document["document_id"]: document for document in bundle["documents"]}
        # Preserve list order: callers can pair their candidate records with
        # responses deterministically without using a synthetic identifier.
        verified = [
            _verify_driver_reference(args.case_id, reference, documents)
            for reference in references
        ]
        print(json.dumps({"case_id": args.case_id, "verified_references": verified},
                         ensure_ascii=False))
        return 0
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1


def _driver_receipt_path(case_id: str, stage: str, unit_id: str) -> Path:
    if not stage_dependencies.is_known(stage):
        raise ValueError(f"unknown canonical stage: {stage}")
    _require_safe_id("unit_id", unit_id)
    return _require_within(case_dir(case_id), "_driver_receipts", stage,
                           f"{unit_id}.json")


@traced_read("dao.read_driver_receipt")
def cmd_read_driver_receipt(args):
    try:
        target = _driver_receipt_path(args.case_id, args.stage, args.unit_id)
        if not target.exists():
            print(f"NOT_FOUND: no driver receipt for {args.stage}/{args.unit_id}")
            return 1
        print(target.read_text(encoding="utf-8"))
        return 0
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 1


def cmd_write_driver_receipt(args):
    try:
        target = _driver_receipt_path(args.case_id, args.stage, args.unit_id)
        data = json.loads(Path(args.data_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    expected = {
        "case_id": args.case_id,
        "run_id": args.run_id,
        "stage": args.stage,
        "unit_id": args.unit_id,
    }
    if any(data.get(key) != value for key, value in expected.items()):
        print("FAIL: receipt identity must match case_id/run_id/stage/unit-id arguments")
        return 1
    existing_lock = acquire_lock_blocking(
        target, args.held_by, args.run_id,
        f"write driver receipt {args.stage}/{args.unit_id}")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    try:
        errors = _schema_check(data, "driver_receipt.schema.json")
        if errors:
            print("FAIL: driver receipt schema validation errors:")
            for error in errors:
                print(f"  - {error}")
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target, data)
        print(f"PASS: wrote driver receipt {args.stage}/{args.unit_id}")
        return 0
    finally:
        release_lock(target)


def _driver_candidate_path(case_id: str, stage: str, unit_id: str,
                           candidate_id: str) -> Path:
    if not stage_dependencies.is_known(stage):
        raise ValueError(f"unknown canonical stage: {stage}")
    _require_safe_id("unit_id", unit_id)
    _require_safe_id("candidate_id", candidate_id)
    return _require_within(case_dir(case_id), "_driver_receipts", stage,
                           unit_id, f"{candidate_id}.json")


@traced_read("dao.read_driver_candidates")
def cmd_read_driver_candidates(args):
    """List every completed sub-unit of one driver stage unit.

    Returned as a map keyed by candidate_id so a driver can decide reuse per
    unit without opening a case directory itself. A missing directory is an
    empty map, not an error: no unit has completed yet is the ordinary state
    of a first run, and making it an error would force every caller to treat
    a cold start as a failure.
    """
    try:
        base = _require_within(case_dir(args.case_id), "_driver_receipts",
                               args.stage, args.unit_id)
        if not stage_dependencies.is_known(args.stage):
            raise ValueError(f"unknown canonical stage: {args.stage}")
        _require_safe_id("unit_id", args.unit_id)
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 1
    candidates = {}
    if base.is_dir():
        for path in sorted(base.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"FAIL: unreadable driver candidate {path.name}: {exc}")
                return 1
            if not isinstance(data, dict) or data.get("candidate_id") != path.stem:
                print(f"FAIL: driver candidate {path.name} does not match its filename")
                return 1
            candidates[path.stem] = data
    print(json.dumps({"case_id": args.case_id, "stage": args.stage,
                      "unit_id": args.unit_id, "candidates": candidates},
                     ensure_ascii=False))
    return 0


def cmd_write_driver_candidate(args):
    """Publish one completed sub-unit result, schema-validated and locked."""
    try:
        target = _driver_candidate_path(args.case_id, args.stage, args.unit_id,
                                        args.candidate_id)
        data = json.loads(Path(args.data_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    expected = {
        "case_id": args.case_id,
        "run_id": args.run_id,
        "stage": args.stage,
        "unit_id": args.unit_id,
        "candidate_id": args.candidate_id,
    }
    if any(data.get(key) != value for key, value in expected.items()):
        print("FAIL: candidate identity must match "
              "case_id/run_id/stage/unit-id/candidate-id arguments")
        return 1
    existing_lock = acquire_lock_blocking(
        target, args.held_by, args.run_id,
        f"write driver candidate {args.stage}/{args.unit_id}/{args.candidate_id}")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    try:
        errors = _schema_check(data, "driver_candidate.schema.json")
        if errors:
            print("FAIL: driver candidate schema validation errors:")
            for error in errors:
                print(f"  - {error}")
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target, data)
        print(f"PASS: wrote driver candidate {args.stage}/{args.unit_id}/{args.candidate_id}")
        return 0
    finally:
        release_lock(target)


def _normalize_for_absence(text: str) -> str:
    """Fold away every difference that separates two spellings of one Korean term.

    This is the mirror image of _cross_contract._normalize_ws, and the
    asymmetry between them is what this function exists to end. That one
    collapses whitespace RUNS to a single space, which is right for proving a
    quote is PRESENT: it keeps word boundaries, so a verbatim check stays
    exact. Proving a term is ABSENT needs the opposite tolerance, because
    Korean legal text spells one term several ways -- `직접청구` / `직접 청구`
    -- and extraction adds mid-word line breaks on top (DOC_004 alone had 722).
    A search that folds neither reports 0 hits for a term printed on the page.

    So: NFKC (fullwidth/compatibility forms), then ALL whitespace removed --
    not collapsed. `직접 청구`, `직접\\n청구` and `직접청구` become one string.

    This deliberately over-matches. Removing spaces can join two unrelated
    words across a boundary, so a hit here is a CANDIDATE, not a finding --
    cmd_search_document_text returns surrounding context and never renders a
    verdict. Judging the context is the caller's job; the floor this provides
    is only that a negative claim can no longer rest on a spelling variant.
    """
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _search_one_document(case_id: str, doc_id: str, needle_norm: str,
                         context: int) -> tuple[list[dict], str | None]:
    """Find needle_norm in one document's processed text. Returns (hits, error).

    Matching runs on the normalized text, but every hit reports the SOURCE
    spelling from the original page -- an agent that has to write the quote
    down needs what the document actually prints, not the folded form.
    """
    redacted = DATA / "processed" / case_id / doc_id / "redacted_text.md"
    if not redacted.exists():
        return [], (f"NOT_EXTRACTED: {doc_id} has no processed text yet")
    try:
        pages = _cross_contract.split_pages(redacted.read_text(encoding="utf-8"))
    except Exception as exc:  # SourceUnavailable and friends -- fail loud, never silently 0-hit
        return [], f"UNREADABLE: {doc_id}: {exc}"

    hits = []
    for page_no in sorted(pages):
        raw = pages[page_no]
        # Map each normalized character back to its index in the raw page, so
        # a normalized match can be reported as the raw substring it came from.
        # Normalize PER RAW CHARACTER, not over the whole page. NFKC can change
        # a string's length (a compatibility char may expand to several), so
        # indices into a wholesale-normalized page do not line up with the raw
        # page -- an earlier version built the map that way and reported spans
        # shifted by one, surfacing `연현상으` for a `자연현상` match. Mapping each
        # raw char to the run of normalized chars it produces keeps every
        # normalized index traceable to the exact raw character it came from.
        norm_chars, back = [], []
        for i, ch in enumerate(raw):
            if ch.isspace():
                continue
            folded = unicodedata.normalize("NFKC", ch)
            for piece in folded:
                if piece.isspace():
                    continue
                norm_chars.append(piece)
                back.append(i)
        norm_page = "".join(norm_chars)

        start = norm_page.find(needle_norm)
        while start != -1:
            end = start + len(needle_norm) - 1
            raw_start, raw_end = back[start], back[end] + 1
            hits.append({
                "document_id": doc_id,
                "page": page_no,
                "matched_source_text": raw[raw_start:raw_end],
                "context": " ".join(raw[max(0, raw_start - context):raw_end + context].split()),
            })
            start = norm_page.find(needle_norm, start + 1)
    return hits, None


# Documents that are not searchable surfaces. expert_review_only has no
# processed text by design; superseded_bundle has text but every page of it
# is also a page of one of its children, so searching both reports one
# occurrence twice and inflates apparent corroboration.
_SEARCH_EXCLUDED = {"expert_review_only", "superseded_bundle"}


@traced_read("dao.search_document_text")
def cmd_search_document_text(args):
    """Whitespace/NFKC-insensitive search over processed text -- the floor under
    a negative claim.

    An agent asserting a clause/term is ABSENT ("no such provision exists in
    this policy") previously had only a raw substring search, which finds one
    spelling. On CASE_907 that produced three wrong absence findings in one
    day, one of which was recorded as a fabrication finding against a draft
    sentence that was in fact correct -- the policy DID grant a 직접 청구 right,
    spelled with a space.

    Output is deliberately shaped for the record: `searched_normalized` states
    what was actually looked for, so a 0-hit result documents the search rather
    than just asserting a conclusion. A reviewer can see which variant space
    was covered instead of taking the absence on faith.
    """
    needle_norm = _normalize_for_absence(args.term)
    if not needle_norm:
        print("EMPTY_TERM: search term normalizes to nothing")
        return 2

    _require_safe_id("case id", args.case_id)
    if args.all_docs and args.doc_id:
        print("error: pass either DOC_ID or --all-docs, not both")
        return 2

    if args.all_docs:
        manifest = read_contract_data(args.case_id, "document_manifest.json")
        if manifest is None:
            print(f"NOT_FOUND: no document_manifest.json for {args.case_id}")
            return 1
        # An expert_review_only document has no processed text by design; it is
        # not a searchable surface, and listing it as "unsearchable" every time
        # would train readers to skim past the field that matters. A
        # superseded_bundle is excluded for the opposite reason -- its text
        # very much exists, but every page of it is also a page of one of its
        # children, so searching both reports one hit twice and inflates the
        # apparent corroboration for whatever the caller is checking.
        doc_ids = [d.get("document_id") for d in manifest.get("documents", [])
                   if d.get("downstream_disposition") not in _SEARCH_EXCLUDED]
    elif args.doc_id:
        doc_ids = [args.doc_id]
    else:
        print("error: pass a DOC_ID or --all-docs")
        return 2

    # The disposition check has to run per document, not only when building the
    # --all-docs list: filtering the sweep alone left `search-document-text
    # CASE_ID DOC_005 <term>` serving the retained bundle in full (4 real hits
    # on CASE_909), so the double-count this exclusion exists to prevent was
    # still one explicit doc_id away. A caller naming the bundle directly gets
    # the reason rather than silence.
    excluded_dispositions = {}
    manifest_for_scope = read_contract_data(args.case_id, "document_manifest.json")
    if manifest_for_scope is not None:
        excluded_dispositions = {
            d.get("document_id"): d.get("downstream_disposition")
            for d in manifest_for_scope.get("documents", [])
            if d.get("downstream_disposition") in _SEARCH_EXCLUDED}

    hits, unsearched = [], []
    for doc_id in doc_ids:
        if not doc_id:
            continue
        _require_safe_id("document id", doc_id)
        if doc_id in excluded_dispositions:
            unsearched.append({
                "document_id": doc_id,
                "reason": f"{excluded_dispositions[doc_id]} -- not a searchable "
                          "surface. A superseded_bundle's every page is also a "
                          "page of one of its children, so searching it "
                          "alongside them reports one occurrence twice; an "
                          "expert_review_only document has no processed text "
                          "by design. Search the children instead."})
            continue
        doc_hits, error = _search_one_document(args.case_id, doc_id, needle_norm, args.context)
        hits.extend(doc_hits)
        if error:
            unsearched.append({"document_id": doc_id, "reason": error})

    print(json.dumps({
        "term": args.term,
        "searched_normalized": needle_norm,
        "normalization": "NFKC + all whitespace removed (matches 직접청구 / 직접 청구 / line-split)",
        "documents_searched": [d for d in doc_ids
                               if d not in {u["document_id"] for u in unsearched}],
        "documents_unsearched": unsearched,
        "hit_count": len(hits),
        "hits": hits,
        "note": ("hits are CANDIDATES -- whitespace folding can join unrelated words, so read "
                 "the context before concluding. 0 hits covers only this normalized form: it is "
                 "evidence about this spelling space, not proof the concept is absent."),
    }, ensure_ascii=False))
    # 0 = searched cleanly with hits, 1 = searched cleanly with none, 2 = could not search.
    if unsearched and not hits:
        return 2
    return 0 if hits else 1


PAGE_TEXT_ALLOWED_STAGES = frozenset({"document-pipeline"})

PAGE_TEXT_CAPABILITY_ENV = "HARNESS_CHECKPOINT2_CAPABILITY"


def capability_dir() -> Path:
    return ROOT / "_capabilities"


def _issue_page_text_capability(case_id: str, doc_id: str) -> tuple[str, Path]:
    """Mint the one-shot capability that authorizes pre-redaction reads.

    Returns (token, path). The token goes to the child DAO through its
    environment; a file named for the token's DIGEST is written to a directory
    the DAO owns. Verification checks that the digest of the presented token
    names an existing file, so the secret itself is never stored.

    The asymmetry is the point, and an earlier version of this got it wrong:
    comparing an env var against a second env var proves nothing, because a
    caller who sets both to the same value passes. Here the check depends on
    a file only the issuing process created, which a caller cannot fabricate
    without already being able to write into the repo's capability directory.

    Scoped to one case/document and deleted after the run
    (release_page_text_capability), so a leaked token is not reusable later or
    against other documents.
    """
    token = secrets.token_hex(32)
    directory = capability_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _capability_filename(token, case_id, doc_id)
    # 0o600 and O_EXCL: readable only by this user, and never silently
    # reusing a path that already exists.
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"case_id": case_id, "doc_id": doc_id, "issued_at": now_iso()}, handle)
    return token, path


def _capability_filename(token: str, case_id: str, doc_id: str) -> str:
    """Name a capability file by the digest of (token, case, document).

    Binding the scope into the digest means a token minted for one document
    does not verify for another, and storing only the digest means reading
    the directory does not hand anyone a usable token.
    """
    digest = hashlib.sha256(f"{token}:{case_id}:{doc_id}".encode("utf-8")).hexdigest()
    return f"{digest}.json"


def release_page_text_capability(path: Path) -> None:
    """Revoke the capability. Called in a finally, so an exception mid-run
    still ends with the token unusable."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _page_text_capability_ok(case_id: str, doc_id: str,
                              capability: str | None = None) -> bool:
    """Whether the caller presented a live capability for THIS document.

    `capability` is the in-process form: checkpoint 2 calling
    read_page_text_data() directly hands the token as an argument. The env
    var remains the subprocess form, and neither weakens the check -- both
    resolve to the same digest lookup, so a caller without a live token is
    refused either way.

    Passing it as an argument is strictly the safer of the two: the token
    never enters the environment block and so cannot be read out of a process
    listing or inherited by an unrelated grandchild.
    """
    presented = capability or os.environ.get(PAGE_TEXT_CAPABILITY_ENV)
    if not presented:
        return False
    return (capability_dir() / _capability_filename(presented, case_id, doc_id)).exists()


def read_page_text_data(case_id: str, doc_id: str, page: int, *,
                        caller_stage: str, capability: str) -> str:
    """In-process equivalent of `read-page-text`, for checkpoint 2.

    Exists so redact_document.py can stop paying ~0.38s of interpreter
    startup per page (measured) to read text this process could read
    directly. It enforces the SAME two gates as the CLI path, in the same
    order: the caller-stage allowlist, then the per-document capability.
    Being in-process is explicitly NOT a reason to skip them -- an importer
    is not more trusted than a subprocess, and the capability is what makes
    the stage claim verifiable rather than self-asserted.

    Raises PermissionError when a gate refuses, so a caller cannot mistake a
    denial message for page content the way a stdout-scraping subprocess
    caller could.
    """
    if caller_stage not in PAGE_TEXT_ALLOWED_STAGES:
        raise PermissionError(
            f"page text is pre-redaction and may only be read by "
            f"{sorted(PAGE_TEXT_ALLOWED_STAGES)} (harness-guardrails P2); "
            f"caller_stage={caller_stage!r} is not permitted")
    if not _page_text_capability_ok(case_id, doc_id, capability):
        raise PermissionError(
            "--caller-stage is self-asserted and is not sufficient on its own. "
            "Pre-redaction page text additionally requires checkpoint 2's run "
            "capability for this specific document.")
    if page < 1:
        raise ValueError(f"page must be >= 1 (got {page})")
    page_path = processed_dir(case_id, doc_id) / f"page_{page:03d}.md"
    if not page_path.exists():
        raise FileNotFoundError(
            f"NOT_EXTRACTED: {doc_id} page {page} has no validated processed "
            "text yet. Complete document-pipeline checkpoint 1 first.")
    return page_path.read_text(encoding="utf-8")


@traced_read("dao.read_page_text")
def cmd_read_page_text(args):
    """Read one validated checkpoint-1 page from the processed layer.

    This command exists for checkpoint 2 so redaction never opens a raw
    source or reaches into data/processed outside the DAO boundary.

    page_NNN.md is checkpoint 1 output -- BEFORE redaction -- so it still
    carries claimant-facing PII (names, addresses, phone numbers). Only the
    stage that owns checkpoint 2 has any business reading it; every analysis
    stage reads the redacted text via read-document-text instead.

    The caller gate is enforced here rather than left to agent prompts
    because a prompt cannot actually stop a call. CASE_901 is the proof: the
    denial-response agent misread read-document-text's path return as a
    failure, fell back to read-page-text, and read real names/addresses/phone
    numbers straight out of the pre-redaction layer. The agent specs were
    corrected (commit df7b123) and tests/test_redaction_boundary.py pins that
    wording, but a spec is guidance -- this is the structural half, mirroring
    the caller check read-ground-truth has always had for D1.

    --caller-stage alone is SELF-ASSERTED: any caller can type
    `--caller-stage document-pipeline`. So the flag is necessary but not
    sufficient -- the call must also present the capability
    redact_document.py mints per run and passes through the child
    environment. An agent shelling out to the DAO does not have it, so
    claiming the stage name no longer gets page text.

    What this does NOT do: stop an agent from opening
    data/processed/.../page_NNN.md with Read or `cat`. That is a filesystem
    permission question, not something the DAO can answer, and it is recorded
    honestly in known-gaps.md rather than described as sealed.
    """
    # NOTE: this CLI path and read_page_text_data() above enforce the same two
    # gates. They are kept as separate code because the CLI's contract is
    # "print a message, return an exit code" while the in-process contract is
    # "return text or raise" -- a stdout-scraping caller cannot distinguish a
    # denial string from page content, so the in-process form must raise.
    # tests/test_redaction_boundary.py pins that both refuse the same inputs.
    if args.caller_stage not in PAGE_TEXT_ALLOWED_STAGES:
        print(f"DENIED: page text is pre-redaction and may only be read by "
              f"{sorted(PAGE_TEXT_ALLOWED_STAGES)} (checkpoint 2's own input; harness-guardrails P2). "
              f"caller_stage={args.caller_stage!r} is not permitted. Analysis stages must use "
              f"read-document-text, which returns the path to the REDACTED text. "
              f"This is logged as a potential redaction-boundary violation.")
        return 1
    if not _page_text_capability_ok(args.case_id, args.doc_id):
        print("DENIED: --caller-stage is self-asserted and is not sufficient on its own. "
              "Pre-redaction page text additionally requires checkpoint 2's run capability, "
              "which tools/redact_document.py mints and passes to this process. A stage that "
              "merely names itself 'document-pipeline' does not get page text. Use "
              "read-document-text (returns the path to the REDACTED text) instead. "
              "This is logged as a potential redaction-boundary violation.")
        return 1
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


def _transcribe_ground_truth_ephemeral(path: Path, args) -> int:
    """Vision-read a SCANNED ground-truth PDF and return text without storing it.

    A scanned answer key had no sanctioned read path at all. The OCR pipeline
    is deliberately closed to ground truth (it writes into data/processed,
    which non-evaluation stages can read), and the 2026-07-22 Read deny-glob
    closed direct opening -- correct, but together they left the one stage D1
    exempts unable to read a scan by any legitimate means. On CASE_907 that
    forced a workaround: rendering the pages to .tmp/ and reading them there,
    which escaped the deny-glob (it is bound to a PATH, so copying out defeats
    it), passed no gate, and left no record.

    The cache-directory alternative was rejected for reproducing exactly that:
    a new persistent location holding answer-key content, protected only by
    the same path-bound convention that had already failed. Here the rendered
    pages live in a TemporaryDirectory removed in a finally, so no persistent
    artifact exists even if transcription raises. The value delivered is not
    access -- the workaround already had access -- but that the access is
    gated, attributable and logged, and leaves nothing behind to leak later.

    Authorization is NOT rechecked here: the caller already enforced
    caller_stage == evaluation and the human-review flag before any bytes were
    touched, and this runs strictly inside that branch.
    """
    import tempfile
    from llm_providers import build_provider, ProviderConfigError
    from ocr_extract import TRANSCRIBE_PROMPT, OCR_PROMPT_VERSION, split_to_page_images

    tmp_root = tempfile.mkdtemp(prefix="gt_transcribe_")
    try:
        page_images = split_to_page_images(path, Path(tmp_root))
        if not page_images:
            print(f"RENDER_FAILED: {path.name} produced no page images")
            return 1

        # transcribe_image confines the child's Read to the image's own parent
        # directory, and that directory here holds nothing but this one file's
        # rendered pages -- so the reader cannot reach the rest of
        # data/ground_truth even if the prompt is subverted.
        try:
            provider = build_provider()
        except ProviderConfigError as exc:
            print(f"PROVIDER_ERROR: {exc}")
            return 1

        print(f"<<<GROUND_TRUTH file={path.name} pages={len(page_images)} "
              f"method=ephemeral_vision>>>")
        for i, image_path in enumerate(page_images, start=1):
            result = provider.transcribe_image(image_path, TRANSCRIBE_PROMPT, OCR_PROMPT_VERSION)
            text = (getattr(result, "text", None) or "").strip()
            if not text:
                # Fail loud per page rather than emitting a silent blank that
                # an evaluator could read as "this page says nothing".
                print(f"<<<PAGE page={i}>>>")
                print(f"TRANSCRIPTION_FAILED: provider returned no text for page {i}")
                continue
            print(f"<<<PAGE page={i}>>>")
            print(text)
        return 0
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


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

    # Without --file/--list this prints the directory path and stops, which is
    # all it ever did. That was a real hole rather than a design: the 2026-07-22
    # fleet review added a Read deny-glob over data/ground_truth (correctly --
    # it stops a NON-evaluation agent reading the answer key), but gave the
    # evaluation stage no sanctioned replacement. So the one stage D1 exempts
    # was left authorized-but-unable: the gate says yes and hands back a path
    # it is separately forbidden to open. CASE_021 evaluated before that glob
    # landed; CASE_907 is the first case to hit it. --file/--list close it by
    # making this command carry CONTENT, so the authorization and the read are
    # the same logged event instead of two mechanisms that disagree.
    wants_content = getattr(args, "list", False) or getattr(args, "file", None)
    if wants_content and not gt_dir.is_dir():
        # Only the content forms need the directory to exist. The bare form
        # answers "am I authorized, and where", which is a real answer even for
        # a case whose ground truth has not been placed yet -- callers depend on
        # the gate's verdict being about authorization, not about file presence.
        print(f"NOT_FOUND: no ground-truth directory for {args.case_id} at {gt_dir}")
        return 1

    files = sorted(p for p in gt_dir.iterdir() if p.is_file()) if gt_dir.is_dir() else []
    if getattr(args, "list", False):
        for p in files:
            print(f"{p.stem}\t{p.name}\t{p.stat().st_size}")
        return 0

    target = getattr(args, "file", None)
    if not target:
        print(str(gt_dir))
        return 0

    _require_safe_id("ground-truth file id", target)
    matches = [p for p in files if p.stem == target]
    if not matches:
        print(f"NOT_FOUND: no ground-truth file with id {target!r} in {gt_dir}. "
              f"Known ids: {[p.stem for p in files]}")
        return 1
    path = matches[0]

    if path.suffix.lower() == ".pdf":
        from ocr_extract import pdf_embedded_page_texts
        pages = pdf_embedded_page_texts(path)
        if pages is None:
            if not getattr(args, "transcribe", False):
                print(f"NO_TEXT_LAYER: {path.name} has no whole-document embedded text layer. "
                      f"Ground truth is read-only reference material and is deliberately NOT "
                      f"put through the OCR pipeline (that would write it into data/processed, "
                      f"where non-evaluation stages can read it). Re-run with --transcribe for "
                      f"the sanctioned ephemeral vision read, which returns text without "
                      f"persisting the answer key anywhere on disk.")
                return 1
            return _transcribe_ground_truth_ephemeral(path, args)
        for i, text in enumerate(pages, start=1):
            print(f"<<<PAGE page={i}>>>")
            print(text)
        return 0

    from ocr_extract import decode_text_file
    text, _encoding = decode_text_file(path)  # returns (text, encoding_used)
    if not (text or "").strip():
        print(f"EMPTY: {path.name} decoded to no content")
        return 1
    print(text)
    return 0


@traced_read("dao.read_contract")
def cmd_read_contract(args):
    # The medical contracts are owned by purpose-built commands: the ledger and
    # the immutable revisions are never readable through the generic path, and
    # a target that is a symlink or carries extra hardlinks is refused rather
    # than read. Checked BEFORE the existence probe, so a refusal cannot be
    # turned into an oracle for whether protected state exists.
    # Resolve the path FIRST so a traversal attempt still exits hard, the way
    # it did before this guard existed -- a refusal that merely returns 1 would
    # downgrade a path-safety violation into an ordinary failure.
    p = _require_within(case_dir(args.case_id), args.filename)
    try:
        import medical_repository

        medical_repository.require_generic_read_allowed(
            sys.modules[__name__], args.case_id, args.filename
        )
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 1
    if not p.exists():
        print(f"NOT_FOUND: {p}")
        return 1
    print(p.read_text(encoding="utf-8"))
    return 0


def read_contract_data(case_id: str, filename: str):
    """DAO-owned structured contract read for in-process pipeline tools.

    Carries the same medical-ownership guard as the CLI path. In-process
    callers are the ones that matter most here: a pipeline tool importing the
    DAO would otherwise reach protected state that `read-contract` refuses,
    which is exactly the "guard on one path, nothing on the other" shape this
    repo has closed three times already.
    """
    import medical_repository

    medical_repository.require_generic_read_allowed(
        sys.modules[__name__], case_id, filename
    )
    p = _require_within(case_dir(case_id), filename)
    return load_json(p)


def read_generic_file_bytes(case_id: str, filename: str) -> bytes:
    """Read an allowed generic file, refusing medical-owned targets."""
    import medical_repository

    medical_repository.require_generic_target_allowed(
        sys.modules[__name__], case_id, filename
    )
    medical_repository.require_generic_read_allowed(
        sys.modules[__name__], case_id, filename
    )
    return _require_within(case_dir(case_id), filename).read_bytes()


def _effective_segmentation_status(document: dict) -> str:
    """Returns the structural Stage-1 status, including legacy inference.

    Old manifests predate ``segmentation_status``. A split child is safely
    recognizable from its source-page provenance, and non-PDF inputs never need
    PDF segmentation. Every other legacy PDF fails closed to pending_review so
    a missing field cannot bypass the new Stage-2 gate.
    """
    status = document.get("segmentation_status")
    if status:
        return status
    if document.get("downstream_disposition") == "superseded_bundle":
        return "completed"
    if isinstance(document.get("source_page_start"), int):
        return "completed"
    if document.get("file_format") != "pdf":
        return "not_applicable"
    return "pending_review"


def check_segmentation_ready(case_id: str, target_doc_id: str | None = None) -> dict:
    """Refuse a document that is not a valid processing target.

    Only one thing is refused now: the retained `superseded_bundle`, whose
    children replaced it. Processing it again would duplicate every page under
    a document that no longer represents anything.

    It used to also block any PDF awaiting a human bundle decision, or any
    bundle not yet split, case-wide. That existed because segmentation ran
    BEFORE OCR: an unsplit bundle would be read and classified as one document,
    and undoing it meant paying for OCR twice, so a person had to declare
    "bundle or not" before anything could run. With OCR first, a bundle is the
    normal thing to read -- pages are pages -- and classification happens after
    the split, so that failure cannot occur. Whether a PDF is a bundle is now an
    OUTPUT of segmentation (one proposed boundary means one document), not an
    input a human must supply in advance.

    The human judgement did not disappear; it moved to where the evidence is.
    A person approves the PROPOSED BOUNDARIES (`segment_case.py split` refuses
    without case-level and per-segment approval), having seen the titles the
    split would cut on, instead of guessing from a filename.
    """
    manifest = read_contract_data(case_id, "document_manifest.json")
    if manifest is None:
        return {
            "clear": False,
            "case_id": case_id,
            "target_document_id": target_doc_id,
            "blockers": [],
            "error": "document_manifest.json not found",
        }

    documents = manifest.get("documents", [])
    target = None
    if target_doc_id is not None:
        target = next((d for d in documents if d.get("document_id") == target_doc_id), None)
        if target is None:
            return {
                "clear": False,
                "case_id": case_id,
                "target_document_id": target_doc_id,
                "blockers": [],
                "error": f"document_id {target_doc_id} not found in document_manifest.json",
            }

    blockers = []
    if target is not None and target.get("downstream_disposition") == "superseded_bundle":
        blockers.append({
            "document_id": target_doc_id,
            "segmentation_status": "completed",
            "reason": "target is the retained superseded bundle; process its logical children instead",
        })

    return {
        "clear": not blockers,
        "case_id": case_id,
        "target_document_id": target_doc_id,
        "blockers": blockers,
        "error": None,
    }


def cmd_check_segmentation_ready(args):
    result = check_segmentation_ready(args.case_id, args.doc_id)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["clear"] else 1


def set_segmentation_status(case_id: str, document_id: str, status: str,
                            reviewer: str, note: str | None,
                            held_by: str, run_id: str):
    """Records the genuine human bundle/non-bundle decision via the DAO.

    Only the two review outcomes are accepted here. ``pending_review`` is owned
    by intake and ``completed`` by the split tool, so neither can be fabricated
    through this human-review command.
    """
    if status not in {"required", "not_required"}:
        return False, "FAIL: status must be required or not_required"
    target = case_dir(case_id) / "document_manifest.json"
    existing_lock = acquire_lock_blocking(
        target, held_by, run_id,
        f"record segmentation review for {document_id}: {status}",
    )
    if existing_lock is not None:
        return False, (
            f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
            f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}"
        )
    try:
        if not target.exists():
            return False, f"FAIL: no document_manifest.json for {case_id}"
        # Fresh read happens only after the lock is held. A concurrent split
        # therefore cannot turn the target into a superseded bundle between our
        # eligibility check and the write.
        manifest = json.loads(target.read_text(encoding="utf-8"))
        document = next(
            (d for d in manifest.get("documents", []) if d.get("document_id") == document_id),
            None,
        )
        if document is None:
            return False, f"FAIL: document_id {document_id} not found in document_manifest.json"
        if document.get("file_format") != "pdf":
            return False, f"FAIL: {document_id} is not a PDF; segmentation is not applicable"
        if document.get("downstream_disposition") == "superseded_bundle":
            return False, f"FAIL: {document_id} is already a superseded bundle"
        document.update({
            "segmentation_status": status,
            "segmentation_reviewed_by": reviewer,
            "segmentation_reviewed_at": now_iso(),
            "segmentation_review_note": note,
        })
        manifest["updated_at"] = now_iso()
        errors = _schema_check(manifest, "document_manifest.schema.json")
        if errors:
            return False, "FAIL: schema validation errors for " + str(target) + " -- not written:\n" + \
                "\n".join(f"  - {e}" for e in errors)
        atomic_write_json(target, manifest)
        return True, f"PASS: set {document_id} segmentation_status={status} in {target}"
    finally:
        release_lock(target)


def cmd_set_segmentation_status(args):
    ok, message = set_segmentation_status(
        args.case_id, args.doc_id, args.status, args.reviewer, args.note,
        args.held_by, args.run_id,
    )
    print(message)
    return 0 if ok else 1
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


def _classification_review_blockers(case_id: str, manifest: dict) -> list[str]:
    """Every classification read from PRE-REDACTION text that no human cleared.

    checkpoint 1 classifies from redacted_text.md when it exists and falls back
    to page_NNN.md when it does not -- a deliberate fallback, since a document
    that has not been redacted yet must still be classifiable. The fallback
    sets review_required and records classification_text_source:raw_page_text
    so the event is never silent.

    Until this gate existed, that was the whole of it: the flag was written and
    nothing on earth read it. No finalize check consulted it, and
    record-human-review had no artifact kind that could clear it, so a case
    could pass every stage carrying "a human must look at this" forever. A flag
    nobody reads and nobody can clear is not a notification.

    So the stage that CREATES the condition is the stage that refuses to
    finalize while it stands unreviewed. What the reviewer is actually asked is
    narrow and answerable: does the classification's evidence quote survive
    redaction? If it does, the same label is reachable from the redacted layer
    and no PII did load-bearing work -- which is the usual case and a quick
    confirmation. If it does not, the label rests on text the analysis side
    should never have seen, and that is worth catching before it propagates.
    """
    ledger = load_human_review_ledger(case_id)
    blockers: list[str] = []
    for doc in manifest.get("documents", []):
        doc_id = doc.get("document_id")
        if not doc_id:
            continue
        classification = read_contract_data(
            case_id, f"classification_result_{doc_id}.json")
        if classification is None:
            continue
        if classification.get("classification_text_source") != "raw_page_text":
            continue
        if not classification.get("review_required"):
            continue
        errors = human_review.verify_reference(
            ledger,
            classification.get("human_review_uid"),
            expected_kind="classification_review",
            expected_artifact_id=doc_id,
            expected_target_key="classification_text_source:raw_page_text",
            current_artifact_sha256=human_review.canonical_artifact_sha256(
                classification),
        )
        blockers.extend(f"{doc_id}: {error}" for error in errors)
    return blockers


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
    """The policy documents that owe a normalized clause contract: NONE.

    RETIRED 2026-08-15. Clause normalization is no longer produced by any
    stage, so nothing owes a clause contract and this scope is permanently
    empty. The function is kept rather than inlined as `[]` because its
    callers are the completion gate and the audit/inventory checks, and a
    named empty scope states WHY they clear where a bare literal would read
    as an oversight.

    Measured grounds, on the shipped corpus rather than a projection: 211
    policy documents across 4 pre-2026-08-04 cases carry
    `automated_text_pipeline`, and 5 clause files were ever produced (2.4%),
    all in CASE_030 -- whose policy stage is `failed` regardless. CASE_112
    marked 203 documents, produced 0, and reads `passed` only through a
    hand-edited `manual_override` that says as much in its own text. A gate
    nothing can satisfy is not satisfied; it is bypassed, and the bypass then
    has to be maintained.

    Normalization also never replaced source addressing: a normalized clause
    still carries `evidence_references: [{document_id, page, quote}]`
    internally (CASE_030 DOC_004 `CI-a5a6241c6e63996f`), so the UID layered
    on top of the very addressing it was meant to supersede. It classifies
    clauses; it does not select them, and selection is what the analysis
    stages actually spend their time on.

    Deliberately NOT done: removing `automated_text_pipeline` from the
    manifest enum. Four legacy cases record it, CASE_112 is still forked from,
    and rewriting those manifests would falsify how those runs really
    executed -- the same reasoning that keeps `document_segmentation` in the
    run_state enum. The value stays readable; the obligation is what is gone.
    """
    return []


def _text_processed_policy_documents(manifest: dict) -> list[dict]:
    """The policy documents whose processed text the pipeline produces and
    downstream stages may cite. Superset of the normalization scope."""
    return [
        d for d in (manifest or {}).get("documents", [])
        if (
            d.get("document_type") == "insurance_policy"
            and d.get("downstream_disposition")
            in policy_completeness._TEXT_PROCESSED
        )
    ]


def _policy_layer_document_ids(case_id: str) -> set[str]:
    """Every document whose UID verification the policy layer stands on.

    Every TEXT-processed policy document, plus any further document a
    parent-coverage contract accounts for pages of. The second part matters
    because a segmented_parent's coverage can cite an owning segment that the
    manifest's own filter might not reach the same way -- and a parent whose
    coverage rests on an unverified segment is not a verified parent.

    Scoped to text processing rather than to normalization: canonical UIDs are
    what make an evidence citation resolvable to one exact source occurrence,
    and downstream stages cite a policy document whether or not its clauses
    were normalized. Narrowing this to the normalization opt-in would leave the
    documents that actually get quoted unverified.
    """
    manifest = read_contract_data(case_id, "document_manifest.json")
    doc_ids = {
        d.get("document_id")
        for d in _text_processed_policy_documents(manifest)}
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
    # The stage may not finalize on a case whose policy documents were never
    # processed at all -- that was the CASE_112 failure, where an override
    # recorded `passed` over zero policy work. But "processed" is about TEXT:
    # a document at text_only_no_normalization has been OCR'd, redacted and
    # chunked, and every downstream stage can cite it. Requiring a normalized
    # contract here instead would mean a case can only clear this gate by
    # normalizing, which is exactly the obligation that value exists to lift.
    text_processed_policy_docs = [
        d for d in manifest.get("documents", [])
        if d.get("document_type") == "insurance_policy"
        and d.get("downstream_disposition") in policy_completeness._TEXT_PROCESSED
    ]
    if not text_processed_policy_docs:
        return ["no text-processed insurance_policy document is registered"]

    # One scope, permanently empty: `_automated_policy_documents` (see there
    # for the measured grounds). Everything this loop asks for -- clause
    # contract, boundary inventory, clause audit, parent coverage, and the
    # P0-8 table disposition -- was an obligation toward normalization, and
    # normalization is retired as of 2026-08-15.
    #
    # The P0-8 TABLE gate goes with it, and that is the non-obvious half.
    # Mid-change it was moved to the wider text-processed scope, on the
    # reasoning that "does this PDF contain an unaccounted table" survives
    # independently. Running it settled the question the other way: the gate
    # demands a document be SCANNED and every candidate DISPOSITIONED into a
    # `reference_table` contract or an explicit exclusion -- and the stage
    # that wrote `reference_table` went away with normalization. Keeping the
    # demand without its means of discharge is how CASE_112's
    # `manual_override` happened: a gate nobody can satisfy is not satisfied,
    # it is bypassed, and the bypass is what ends up in the record.
    #
    # Measured before deciding, on CASE_142's two born-digital policy PDFs
    # (323 pages): the detector finds 55 candidates and extracts all 55
    # cleanly, but 24 are single-row and 44 are two-column -- term-definition
    # boxes (`치료비 | 치료비라 함은...`) and blank intake forms
    # (`시설명세 | 명칭: 용도:`) that are tables only typographically. Seven
    # have four or more rows, and exactly one is substantive: DOC_010 p13's
    # 지연이자율표. Blocking a stage over 55 findings to protect one is the
    # wrong instrument.
    #
    # Detection itself is worth keeping and is NOT what is being retired here
    # -- it is the only thing that recovers row/column structure from a
    # born-digital policy, because embedded-text extraction flattens that
    # table into `기 간 / 지 급 이 자 / 지급기일의... / 보험계약대출이율`,
    # losing which value belongs to which period. That belongs in a derived
    # index the analysis stages can read, not in a finalize gate; planned as
    # follow-up with the clause index, since both exist to stop an agent
    # hunting through 323 chunks for something the page states plainly.
    normalization_docs = _automated_policy_documents(manifest)
    policy_docs = normalization_docs

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

        # P0-8 follow-up 2. Runs for EVERY automated policy document, before
        # any role-specific branching -- including the `continue` paths below,
        # which is exactly why it sits here. Whether a document contains tables
        # is a fact about the PDF, established by the DAO's own scan; it is not
        # implied by the role somebody declared and not implied by whether
        # anybody wrote a reference_table. A segmented_parent is the one
        # exception, and a structural one: it owns no text of its own, its
        # pages belong to its segments, and each segment is itself in this
        # loop -- scanning the parent as well would report every segment's
        # tables a second time as the parent's unhandled candidates.
        if role != "segmented_parent":
            blockers.extend(_table_region_finalize_blockers(case_id, doc_id))

        # Everything below this line is the NORMALIZED-CLAUSE obligation
        # (clause contract, boundary inventory, clause audit, parent
        # coverage), retired 2026-08-15 and scoped to `normalization_docs`,
        # which is empty. The table gate above is a separate obligation and
        # runs for every text-processed policy document, which is why the
        # split happens here rather than at the loop header.
        if doc not in normalization_docs:
            continue

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
    # Named `normalization_docs` rather than `policy_docs` even though the two
    # are now the same list: parent coverage proves a parent's pages are
    # accounted for by segments that NORMALIZE them, so it belongs to the
    # retired obligation by meaning, not just by current value. If a later
    # change reintroduces a wider policy scope, this must not follow it by
    # accident.
    physical_parents = [
        d for d in normalization_docs
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


def _uid_addressed_ref(ref: dict) -> bool:
    """Does this reference address a clause by canonical UID?

    The two addressing forms have different upstream requirements, and which
    one a reference uses is decided by whether it actually carries a
    `clause_uid` -- not by which contract it appears in.

    UID form requires a normalized clause contract, because that is the only
    place a `PC-<hex>` exists. Source form addresses the clause by
    `document_id` + `page` + verbatim `quote` and needs no contract; it is
    verified against the processed text instead. `matched_clause_ref` and
    `clause_ref` accept either (schema `oneOf`); a `denial_reason_result`
    `policy_match` is always source form -- it has no `clause_uid` field at
    all and grounds itself in `policy_clause_evidence_references`.

    Treating every reference as UID form is what made citing a
    `text_only_no_normalization` policy document impossible.
    """
    return bool((ref or {}).get("clause_uid"))


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
        # Both sides of a split outcome. An accepted coverage's policy_matches
        # bind to the policy layer exactly as a denial's do, so they owe the
        # same snapshot and the same canonical-UID state; leaving them out
        # would let an acceptance cite a stale or legacy policy document that a
        # denial in the same file could not.
        refs = [
            (f"denial_reasons[{reason_index}].policy_matches[{match_index}]",
             match)
            for reason_index, reason in enumerate(data.get("denial_reasons") or [])
            for match_index, match in enumerate(reason.get("policy_matches") or [])
        ]
        refs += [
            (f"accepted_coverages[{accepted_index}]"
             f".policy_matches[{match_index}]", match)
            for accepted_index, accepted in enumerate(
                data.get("accepted_coverages") or [])
            for match_index, match in enumerate(
                accepted.get("policy_matches") or [])
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
        # Keyed by (document, addressing form), not by document alone. The
        # per-document work below is shared, but part of its verdict depends on
        # which form the reference uses -- so caching on the document alone let
        # one UID-form reference's "normalized contract is missing" attach
        # itself to every source-form reference on the same document, failing
        # references that were perfectly well grounded.
        cache_key = (doc_id, _uid_addressed_ref(ref))
        if cache_key not in checked_docs:
            normalized = read_contract_data(
                case_id, f"normalized_policy_clause_{doc_id}.json")
            audit = read_contract_data(
                case_id, f"policy_audit_result_{doc_id}.json")
            doc_errors = []
            # A reference that addresses a clause BY canonical UID can only be
            # resolved against a normalized contract -- there is nowhere else
            # the UID exists, so a missing contract is a genuine unresolvable
            # reference. A reference that addresses a clause by
            # document/page/verbatim quote does not need one: normalization is
            # opt-in, and the quote is verified against the processed source by
            # `strict_evidence_reference` at this same write. Demanding a
            # contract for THAT shape made normalization mandatory for anyone
            # who merely cites a policy document, which is the obligation the
            # opt-in exists to lift.
            if _uid_addressed_ref(ref):
                if normalized is None:
                    # One cause, one error. Reporting a missing audit too
                    # would name a consequence of the same absence and bury
                    # the actionable line under a second, undiagnostic one.
                    doc_errors.append(
                        "normalized policy contract is missing -- this "
                        "reference addresses a clause by canonical UID, which "
                        "only a normalized contract defines. Either cite the "
                        "clause by document_id + page + verbatim quote, or "
                        "promote this document with "
                        "`dao.py promote-policy-document` and re-run the "
                        "policy stage")
                elif audit is None:
                    doc_errors.append("policy audit is missing")
            elif normalized is not None and audit is None:
                # It IS normalized, so the audit still governs those bytes.
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
            #
            # Only meaningful when a normalized contract exists: this recomputes
            # the UIDs *in that contract*. With no contract there are no UIDs to
            # recompute, and running it anyway demanded a boundary inventory
            # from a document that legitimately owes neither -- failing a
            # source-addressed reference for missing an artifact it never uses.
            if normalized is not None:
                doc_errors.extend(
                    f"canonical UID: {error}"
                    for error in _canonical_uid_errors(
                        case_id, f"normalized_policy_clause_{doc_id}.json",
                        _cross_contract.NORMALIZED_POLICY_CLAUSE_SCHEMA,
                        normalized))
            checked_docs[cache_key] = (normalized, doc_errors)
        normalized, doc_errors = checked_docs[cache_key]
        errors.extend(f"{loc}: {error}" for error in doc_errors)
        if not _uid_addressed_ref(ref):
            # Source form: the page+quote IS the address, so it gets verified
            # against the processed text. Without this a source-form reference
            # would fall through the `normalized is None` guard below and be
            # persisted unverified -- the exact unresolvable-reference state
            # the UID path refuses.
            #
            # Only for refs that carry the location INLINE
            # (`matched_clause_ref`, `clause_ref`). A `denial_reason_result`
            # policy_match is source-addressed too, but it grounds itself in
            # `policy_clause_evidence_references` rather than at top level, and
            # `_cross_contract.check_policy_matches` already verifies it there
            # against the same processed text. Passing the match object here
            # read page/quote off a level that never has them, so EVERY match
            # on a non-normalized document was rejected as "missing page or
            # quote" -- blocking the path this whole change exists to open.
            if schema_name != "denial_reason_result.schema.json":
                errors.extend(
                    f"{loc}: {error}" for error in
                    _cross_contract.source_addressed_ref_errors(
                        ref, lambda d: _redacted_text_for_doc(case_id, d)))
            continue
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
            errors = _cross_contract.check_normalized_policy_clause(
                data, filename, redacted_text)
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
        # P0-8 runs FIRST and independently: `check_reference_tables` derives
        # RT from `source_regions`, so a UID recomputed from a narrowed region
        # recomputes perfectly -- it is a real hash of real bytes, of the wrong
        # extent. Only the receipt can say whether that extent is the table's.
        errors = _table_region_binding_blockers(case_id, doc_id, data)
        errors.extend(policy_uid_resolver.check_reference_tables(context, data))
        return errors

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


# Case files the DAO issues itself. `write-contract` accepts an arbitrary
# filename, so without this a caller could mint a receipt index directly and
# then cite it -- the receipt would be exactly as authoritative as a real one,
# since the citing contract only asks whether the id resolves. P0-6's index is
# listed too: it was protected only by a forged receipt failing its currency
# checks, which is a second line of defence, not a seal.
_PROTECTED_CONTRACT_FILES = frozenset({
    "_table_region_index.json",
    # The polarity receipt index is no longer issued (the semantic layer was
    # removed), but existing cases still have one on disk and it stays
    # unwritable through write-contract: dropping it from this set would turn
    # a retired DAO-owned file into an agent-writable one.
    "_policy_polarity_semantic_index.json",
    "_segment_derivation_index.json",
    "_revision_index.json",
    "_run_state.json",
    "_transaction_journal.json",
})
def _invalidate_stale_downstream(args, data: dict, target: Path) -> None:
    """After denial_reason_result.json is rewritten, mark every downstream
    stage whose output no longer matches it as `pending` again.

    Called with the upstream file's lock still held, so the hash written into
    run-state is the one that just landed. `_update_run_state` takes a
    different file's lock (run-state), and never the reverse direction, so
    the two locks cannot deadlock against each other.

    Deliberately NOT a deletion. A stale validation is still evidence of what
    a stage concluded and why; discarding it would lose that, and P6's
    never-delete-conflicting-data discipline applies to superseded outputs as
    much as to contradictory sources. The file stays, its recorded
    source_denial_contract_hash marks it superseded, and the DAO refuses to
    rewrite it against the wrong upstream anyway.
    """
    if Path(args.filename).name != _cross_contract.DENIAL_REASONS:
        return
    case = case_dir(args.case_id)
    new_hash = _cross_contract.upstream_hash(data)
    stale = _cross_contract.stale_downstream(case, new_hash)
    if not stale:
        return
    print(f"NOTE: {_cross_contract.DENIAL_REASONS} changed; {len(stale)} downstream contract(s) "
          "no longer describe the current reason/match set:")
    for filename, stage, recorded in stale:
        shown = (recorded[:12] + "...") if recorded else "none recorded"
        print(f"  - {filename} (was {shown}) -> run-state stage {stage!r} reset to pending")
        state = _update_run_state(args.case_id, args.run_id, stage, "pending", args.held_by)
        if state is None:
            print(f"    WARNING: could not reset {stage!r} (see LOCKED above) -- that stage still "
                  "reads as passed while its output is stale; reset it before trusting the run.")
    print("  The stale files are left on disk on purpose (nothing is deleted). Re-run those "
          "stages against the current contract.")


def cmd_write_contract(args):
    normalized_filename = str(args.filename).replace("\\", "/")
    if (Path(args.filename).name in _PROTECTED_CONTRACT_FILES
            or normalized_filename.startswith("_driver_receipts/")):
        print(f"FAIL: {args.filename} is DAO-owned and has no write-contract "
              "path -- it is issued only by the DAO command that derives it, "
              "so that a citing contract's reference cannot be satisfied by a "
              "receipt the caller wrote for itself")
        return 1
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
            # The registry keys on full filenames, but only one agent spec
            # actually spells the argument out, so an agent reasonably passes
            # the bare contract name it sees everywhere else ("screening_report").
            # Refusing with "no such schema" sends it hunting for a schema that
            # is right there. Name the exact fix instead of failing blind; the
            # write is still refused, since guessing which schema was meant is
            # how the wrong contract gets validated against the wrong shape.
            suffixed = f"{schema_name}.schema.json"
            if suffixed in schemas:
                print(f"FAIL: no schema named {schema_name} in schemas/ -- "
                      f"use the full filename {suffixed!r} (the schema registry "
                      "keys on filenames, not bare contract names).")
            else:
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
        # Cross-contract invariants a single-document schema cannot see:
        # ids resolving against a sibling contract, id uniqueness, and
        # sibling-comparing field rules. Same fail/don't-persist contract as
        # schema validation above -- see tools/_cross_contract.py.
        cross_errors = _cross_contract.check(
            args.filename, data, case_dir(args.case_id),
            redacted_text_for=lambda d: _redacted_text_for_doc(args.case_id, d))
        if cross_errors:
            print(f"FAIL: cross-contract validation errors for {target}:")
            for e in cross_errors:
                print(f"  - {e}")
            return 1
        atomic_write_json(target, data)
        print(f"PASS: wrote {target}")
        if (schema_name in _POLICY_LAYER_SCHEMAS
                and previous_contract is not None
                and previous_contract != data):
            policy_layer_changed = args.filename
        # Rewriting an upstream contract can strand downstream ones that were
        # derived from the previous reason/match set. Nothing is deleted (P6:
        # data is never silently discarded) -- the affected stages are flipped
        # back to `pending` in run-state, which is the file that already
        # answers "where does this run actually stand". The stale contract
        # stays on disk, and its recorded hash is what marks it superseded.
        _invalidate_stale_downstream(args, data, target)
        if args.stage:
            # A different target (_run_state.json, not this contract file) --
            # no deadlock risk nesting this inside the contract file's lock.
            # A single contract write marks the stage in_progress, never passed:
            # passing a stage is a deliberate finalize-stage call (snapshot +
            # passed, atomic), not a side effect of one write. dep_check='soft'
            # so an unmet upstream dependency doesn't fail an otherwise-valid
            # contract write -- it just declines to advance the stage.
            state = _update_run_state(args.case_id, args.run_id, args.stage, "in_progress",
                                      args.held_by, dep_check="soft",
                                      explicit_attempt=False)
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


@trace_mod.traced("dao.patch_manifest_document")
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
        result = True, f"PASS: patched {document_id} in {target}"
    finally:
        release_lock(target)
    # Run-state marker outside the manifest lock, for the same reason the
    # cascade below is outside it -- and additionally because _update_run_state
    # acquires a SECOND lock (_run_state.json). Holding the manifest lock while
    # requesting that one is hold-and-wait: with document-level workers (T8),
    # worker A holding the manifest and waiting for run-state, while worker B
    # holds run-state and waits for the manifest, is a deadlock. P5 polls for
    # 15 minutes before giving up, so it would surface as an intermittent long
    # stall rather than a crash. Taking one lock at a time removes the
    # condition outright instead of managing the ordering.
    #
    # Nothing is lost by moving it: the marker is in_progress, not passed (see
    # cmd_write_contract -- patching one document's fields is progress within
    # document_processing, never the whole stage finalizing), it runs with
    # dep_check='soft' so it never fails the patch on run-state dependencies,
    # and a failure here was already only a warning on an otherwise successful
    # return. The manifest write above is durable before this runs.
    if stage:
        state = _update_run_state(
            case_id, run_id, stage, "in_progress", held_by,
            dep_check="soft", explicit_attempt=False)
        if state is None:
            result = True, f"PASS: patched {document_id} in {target}\n" \
                "WARNING: patch succeeded, but run-state could not be updated (lock contention) -- " \
                "run-state may now lag behind actual progress; retry the run-state update."
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


def promote_policy_document(case_id: str, document_id: str, policy_processing_role: str,
                            disputed_by: str, held_by: str, run_id: str,
                            purpose: str | None = None):
    """Move ONE policy document from `text_only_no_normalization` to
    `automated_text_pipeline` -- the opt-IN that makes it owe a normalized
    clause contract before `policy_clause_processing` may finalize.

    This is the missing half of making normalization opt-in. Defaulting an
    `insurance_policy` to `text_only_no_normalization` (run_checkpoint1's
    `default_disposition`) is only sound if something can later say "this
    specific document IS disputed, normalize it". Without that, the default
    is not an opt-in, it is a permanent exemption, and the policy gate clears
    every case at 0/0 for the uninteresting reason that nothing ever asks.

    Who asks: `denial-response`, which is the stage that learns which clauses
    a case actually turns on. Its `policy_match.document_id` names them. So
    `disputed_by` records the denial reason (or policy_match) that motivated
    the promotion -- a promotion with no stated dispute is how the opt-in
    would quietly drift back into "normalize everything".

    `policy_processing_role` is REQUIRED here, not optional. Promotion without
    it produces a manifest that `check_policy_processing_roles` rejects at
    finalize time -- technically safe (it blocks) but the failure surfaces one
    stage later than the mistake, against a document whose role the promoting
    caller knew and the finalizing caller does not. Demanding it here fails
    at the point of the decision.

    Deliberately NOT idempotent-silent on a document already promoted: that
    means two different reasons dispute the same document, which is ordinary,
    but it must not silently overwrite the first `disputed_by` record. The
    existing note is appended to, never replaced.

    Returns (ok, message). The cascade is inherited, not reimplemented:
    `downstream_disposition` is already a `_PROVENANCE_MANIFEST_FIELDS` entry,
    so `patch_manifest_document` invalidates `document_processing`'s dependents
    -- which includes `policy_clause_processing`. That matters because
    `denial_response` is a SIBLING of the policy stage in the dependency graph
    (both depend only on `document_processing`), so a promotion can legitimately
    arrive AFTER the policy stage already recorded `passed` at 0/0. Without the
    cascade that stale pass would stand over a document that now owes a
    contract; with it, the stage is demoted and must re-finalize.
    """
    if policy_processing_role not in policy_roles.POLICY_ROLES:
        return False, (
            f"FAIL: policy_processing_role must be one of "
            f"{sorted(policy_roles.POLICY_ROLES)}, got {policy_processing_role!r}")
    if not (disputed_by or "").strip():
        return False, (
            "FAIL: --disputed-by is required -- promotion is the claim that "
            "this case actually disputes this document, and an unexplained "
            "promotion turns the opt-in back into normalize-everything")

    manifest = read_contract_data(case_id, "document_manifest.json")
    if manifest is None:
        return False, f"FAIL: no document_manifest.json for {case_id}"
    doc = next((d for d in manifest.get("documents", [])
                if d.get("document_id") == document_id), None)
    if doc is None:
        return False, f"FAIL: document_id {document_id} not found in document_manifest.json"
    if doc.get("document_type") != "insurance_policy":
        return False, (
            f"FAIL: {document_id} is document_type "
            f"{doc.get('document_type')!r}, not 'insurance_policy' -- only a "
            "policy document owes a normalized clause contract")
    current = doc.get("downstream_disposition")
    if current not in ("text_only_no_normalization", "automated_text_pipeline"):
        return False, (
            f"FAIL: {document_id} is {current!r}; only a "
            "'text_only_no_normalization' document may be promoted. "
            "expert_review_only and superseded_bundle are excluded from text "
            "processing entirely -- promoting one would claim a normalized "
            "contract can be built from text the pipeline never extracted")

    note = f"disputed by {disputed_by.strip()}"
    existing = (doc.get("normalization_dispute_note") or "").strip()
    merged = f"{existing}; {note}" if existing and note not in existing else (existing or note)
    fields = {
        "downstream_disposition": "automated_text_pipeline",
        "policy_processing_role": policy_processing_role,
        "normalization_dispute_note": merged,
    }
    ok, message = patch_manifest_document(
        case_id, document_id, fields, held_by, run_id,
        stage="document_processing",
        purpose=purpose or f"promote {document_id} to normalization ({disputed_by.strip()})")
    if not ok:
        return ok, message
    if current == "automated_text_pipeline":
        return True, (f"{message}\nNOTE: {document_id} was already promoted; "
                      f"recorded the additional dispute ({disputed_by.strip()}) "
                      "and left the existing record intact.")
    return True, (
        f"{message}\n{document_id}: text_only_no_normalization -> "
        f"automated_text_pipeline (role={policy_processing_role}). It now owes "
        "a normalized clause contract; policy_clause_processing must "
        "re-finalize.")


def cmd_promote_policy_document(args):
    # DEPRECATED 2026-08-15. Retained so pre-2026-08-04 records stay
    # explicable, and because refusing outright would strand a case that
    # somehow still owes a clause contract. Measured grounds for retirement:
    # 211 documents were marked `automated_text_pipeline` across 4 legacy
    # cases and 5 clause files were ever produced (2.4%), while a normalized
    # clause still addresses its source by {document_id, page, quote}
    # internally -- so the UID never replaced source addressing, it layered on
    # top of it. The warning goes to stderr so piped JSON consumers are
    # unaffected.
    print("WARNING: promote-policy-document is DEPRECATED (2026-08-15). "
          "Policy clauses are addressed by {document_id, page, quote} against "
          "the processed text; normalization adds cost without changing which "
          "clause an agent must still select. Promoting demotes "
          "policy_clause_processing to failed and obliges a clause contract "
          "that no current stage produces.", file=sys.stderr)
    ok, message = promote_policy_document(
        args.case_id, args.doc_id, args.policy_processing_role,
        args.disputed_by, args.held_by, args.run_id, purpose=args.purpose)
    print(message)
    return 0 if ok else 1


def replace_manifest_documents(case_id: str, bundle_id: str, bundle_fields: dict,
                               new_documents: list, held_by: str, run_id: str,
                               stage: str | None = None, purpose: str | None = None):
    """Atomically replace ONE bundle's manifest entry with a modified bundle
    entry PLUS N new per-document entries -- segmentation's split step.

    Neither existing manifest write path fits: write-contract overwrites the
    whole file (and reads unlocked, before the lock, so a concurrent write is
    lost) and patch-manifest-document touches exactly one existing entry. Split
    is 'mutate the bundle in place AND append its children' as a single unit --
    if only half of it landed, the manifest would either lose the bundle's
    audit record or point at DOC_XXX entries with no bundle to trace them back
    to. So this does the whole thing under one lock hold, reading AFTER the lock
    (like patch_manifest_document, and unlike the generic read+write-contract
    path -- see known-gaps item 7), and validates before anything is written.

    bundle_fields is merged onto the existing bundle entry (typically
    downstream_disposition: superseded_bundle, ocr_status: not_applicable,
    segmentation_proposal_path, plus a null redacted_text_path the schema
    requires). new_documents are appended as-is. Returns (ok, message) -- the
    same tuple contract patch_manifest_document uses, so a CLI wrapper and an
    in-process caller (segment_case.py split) share one implementation.
    """
    target = case_dir(case_id) / "document_manifest.json"
    existing_lock = acquire_lock_blocking(target, held_by, run_id,
                                          purpose or f"split bundle {bundle_id} into {len(new_documents)} document(s)")
    if existing_lock is not None:
        return False, (f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
                        f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
    result = None
    try:
        if not target.exists():
            return False, f"FAIL: no document_manifest.json for {case_id}"
        manifest = json.loads(target.read_text(encoding="utf-8"))
        bundle = next((d for d in manifest["documents"] if d["document_id"] == bundle_id), None)
        if bundle is None:
            return False, f"FAIL: bundle document_id {bundle_id} not found in document_manifest.json"

        # New ids must not collide with anything already in the manifest -- the
        # bundle survives as a superseded record, so its id (and every existing
        # id) stays taken. Catching this here rather than trusting the caller
        # keeps the file from ever holding two entries with one id.
        existing_ids = {d["document_id"] for d in manifest["documents"]}
        new_ids = [d["document_id"] for d in new_documents]
        collisions = sorted(set(new_ids) & existing_ids)
        if collisions:
            return False, f"FAIL: new document ids already exist in the manifest: {collisions}"
        if len(new_ids) != len(set(new_ids)):
            return False, f"FAIL: new_documents contains duplicate document ids: {new_ids}"

        bundle.update(bundle_fields)
        manifest["documents"].extend(new_documents)
        manifest["updated_at"] = now_iso()

        errors = _schema_check(manifest, "document_manifest.schema.json")
        if errors:
            return False, "FAIL: schema validation errors for " + str(target) + " -- not written:\n" + \
                "\n".join(f"  - {e}" for e in errors)
        atomic_write_json(target, manifest)
        result = (True, f"PASS: split {bundle_id} into {len(new_documents)} document(s) in {target}")
    finally:
        release_lock(target)
    if stage:
        # Segmentation is a checkpoint inside document_processing. It cannot
        # finalize the stage while child classification, redaction and
        # case-wide chunking still remain. The orchestrator owns attempt
        # boundaries and finalization.
        state = _update_run_state(
            case_id, run_id, stage, "in_progress", held_by,
            dep_check="soft", explicit_attempt=False)
        if state is None:
            result = (True, result[1] + "\nWARNING: split succeeded, but run-state "
                      "could not be updated -- retry the progress update.")
    return result


def cmd_replace_manifest_documents(args):
    bundle_fields = json.loads(Path(args.bundle_fields_file).read_text(encoding="utf-8"))
    new_documents = json.loads(Path(args.new_documents_file).read_text(encoding="utf-8"))
    ok, message = replace_manifest_documents(
        args.case_id, args.bundle_id, bundle_fields, new_documents,
        args.held_by, args.run_id, stage=args.stage, purpose=args.purpose)
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


def _raw_file_entry_for(manifest, entry):
    """The manifest entry whose file_path locates `entry`'s bytes on disk.

    Usually `entry` itself. But a split child that inherited the parent's
    already-processed pages has no PDF of its own -- writing one produced 44MB
    from an 11.4MB intake on CASE_142 and nothing ever opened it -- so its
    bytes live in the parent named by source_file_name, and it is identified
    inside that parent by source_page_start/end.

    Resolving to the parent is what keeps the digest MEANINGFUL rather than
    merely available: `source_pdf_sha256` exists to answer "which registered
    file is this document made of", and for such a child the honest answer is
    the parent's file. Returning None instead would block canonical UID
    activation for every child of an OCR'd bundle, which is exactly the
    regression this function's own caller reports as
    "no readable registered raw source".

    Sibling children of one parent therefore share a digest. That is correct:
    they ARE the same file, and what distinguishes them is the page range,
    which the UID scheme carries separately.
    """
    if entry is None:
        return None
    if entry.get("file_path"):
        return entry
    source_name = entry.get("source_file_name")
    if not source_name:
        return None
    # The parent is the entry whose own file is named source_file_name. Match
    # on file_name rather than document_id: source_file_name records the raw
    # file, and a bundle keeps its file_path after being superseded.
    return next(
        (d for d in manifest.get("documents", [])
         if d.get("file_name") == source_name and d.get("file_path")), None)


def registered_source_pdf_sha256(case_id: str, doc_id: str):
    """Hash the registered raw source file ourselves (Part 11J commit b).

    `source_pdf_sha256` is the first input to every canonical UID, so a
    caller-supplied value would let a UID be minted against a file the case
    never registered -- the one input that establishes WHICH document this is
    would establish nothing. The manifest's file_path is only used to locate
    the file; the digest always comes from reading it.

    For a split child with no file of its own, the located file is its
    parent's -- see `_raw_file_entry_for`.
    """
    manifest = read_contract_data(case_id, "document_manifest.json")
    if manifest is None:
        return None
    entry = next(
        (d for d in manifest.get("documents", [])
         if d.get("document_id") == doc_id), None)
    entry = _raw_file_entry_for(manifest, entry)
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
        abandoned = []
        for entry in state.get("stages", []):
            if entry.get("stage_name") not in dependents:
                continue
            if entry.get("status") not in ("passed", "in_progress"):
                continue
            # An in_progress stage has an OPEN attempt marker. Invalidating it
            # without closing that marker leaves the attempt open forever, and
            # aggregate-trace then reports the whole run as incomplete coverage
            # for a stage that demonstrably stopped. Collect the close here and
            # emit after the write, so a failed write emits nothing.
            if entry.get("status") == "in_progress" and entry.get("attempt_count", 0) > 0:
                abandoned.append((entry.get("stage_name"), entry["attempt_count"]))
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
        for stage_name, attempt in abandoned:
            _emit_stage_attempt_marker(
                case_id, run_id, stage_name, attempt, "abandoned",
                outcome="interrupted")
        print(f"INVALIDATED (upstream {upstream_stage} changed): "
              f"{', '.join(sorted(changed))}")
        return changed
    finally:
        # Only release what this call acquired. Releasing a lock the caller
        # owns would leave the rest of its transaction running unprotected.
        if not lock_already_held:
            release_lock(target)


def _invalidate_policy_layer(case_id, reason, held_by, run_id,
                             lock_already_held=False):
    """Invalidate `policy_clause_processing` AND everything downstream of it.

    `lock_already_held` is for a caller running inside a larger transaction
    that already owns the run-state lock (P0-8's table-region commit). Same
    parameter and same reason as `_invalidate_dependents`: re-acquiring a lock
    this process already holds is a self-deadlock, and the alternative --
    releasing it around the cascade -- would open exactly the window the
    transaction exists to close.

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
    if not lock_already_held:
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
        abandoned = []
        for entry in state.get("stages", []):
            if entry.get("stage_name") not in affected:
                continue
            # Idempotent: a stage already failed/pending was never claiming a
            # result derived from unverified UIDs, so re-running activation on
            # an already-invalidated case changes nothing and still succeeds.
            if entry.get("status") not in ("passed", "in_progress"):
                continue
            # See _invalidate_dependents: an in_progress stage's attempt marker
            # is open, and this cascade is exactly what ends that attempt. The
            # policy stage itself is in `affected` here, so this is the path
            # that left CASE_142's attempt 1 open with no end marker.
            if entry.get("status") == "in_progress" and entry.get("attempt_count", 0) > 0:
                abandoned.append((entry.get("stage_name"), entry["attempt_count"]))
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
        for stage_name, attempt in abandoned:
            _emit_stage_attempt_marker(
                case_id, run_id, stage_name, attempt, "abandoned",
                outcome="interrupted")
        return changed
    finally:
        if not lock_already_held:
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
    if Path(args.filename).name in _PROTECTED_CONTRACT_FILES:
        print(
            f"FAIL: {args.filename} is DAO-owned and cannot be replaced "
            "through write-text -- use its dedicated issuing command")
        return 1
    # Medical-owned contracts need a path-aware check, not a basename one: the
    # immutable revisions live in a subdirectory, so matching on name alone
    # would let `_medical_variable_revisions/<sha>.json` through.
    try:
        import medical_repository

        medical_repository.require_generic_target_allowed(
            sys.modules[__name__], args.case_id, args.filename
        )
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 1
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

def _canonical_json_bytes(data) -> bytes:
    """Deterministic JSON bytes -- the digest basis for every ledger binding.

    Kept byte-compatible with `make_history_boundary` below, which sorts keys
    at hash time. That is what lets this validator accept a boundary written by
    the builder already on this branch: the stored `baseline_state` may carry a
    different key order, but the digest is taken over the sorted form either
    way. Verified against a real boundary before transplanting.
    """
    return json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _validate_generic_ledger(ledger: dict, case_id: str, schema_name: str,
                             *, replay_history: bool = True) -> None:
    """Schema-check a ledger, then prove every operation binds to real state.

    Transplanted from 633bd7b0 (see _normalize_legacy_run_state for why this
    was missing). Three bindings are checked, and each catches a different way
    a ledger can lie: the request digest catches a rewritten request, the
    result/target check catches an operation that claims to have changed an
    entry that does not exist or does not match, and the replay below catches
    a final state that no sequence of the recorded operations could produce.
    """
    errors = _schema_check(ledger, schema_name)
    if errors:
        raise ValueError(f"{schema_name} is invalid: " + "; ".join(errors))
    if ledger.get("case_id") != case_id:
        raise ValueError("ledger belongs to a different case")
    operation_ids = [op["operation_id"] for op in ledger["operations"]]
    if len(operation_ids) != len(set(operation_ids)):
        raise ValueError("ledger has duplicate operation_id")
    source_names = {e["file_name"] for e in ledger.get("files", [])}
    conflict_ids = {e["conflict_id"] for e in ledger.get("conflicts", [])}
    for operation in ledger["operations"]:
        request = operation["request"]
        if (request["case_id"] != case_id
                or request["operation_id"] != operation["operation_id"]
                or hashlib.sha256(_canonical_json_bytes(request)).hexdigest()
                != operation["request_sha256"]):
            raise ValueError("ledger operation request binding is invalid")
        payload = request["payload"]
        result = operation["result"]
        if schema_name == "source_ledger.schema.json":
            expected = {"action": "set_status",
                        "target_id": payload["file_name"],
                        "status": payload["status"]}
            target_exists = result["target_id"] in source_names
        elif request["action"] == "add":
            expected = {"action": "add", "target_id": result["target_id"],
                        "status": "pending"}
            target_exists = result["target_id"] in conflict_ids
            conflict = next(
                (e for e in ledger["conflicts"]
                 if e["conflict_id"] == result["target_id"]), None)
            target_exists = target_exists and conflict is not None and all((
                conflict["raised_by_stage"] == payload["stage"],
                conflict["field_or_topic"] == payload["topic"],
                conflict["sources"] == payload["sources"],
            ))
        else:
            expected = {"action": "set_verdict",
                        "target_id": payload["conflict_id"],
                        "status": payload["verdict"]}
            target_exists = result["target_id"] in conflict_ids
        if result != expected or not target_exists:
            raise ValueError("ledger operation result binding is invalid")
    if replay_history:
        _replay_generic_ledger_history(ledger, schema_name)


def _replay_generic_ledger_history(ledger: dict, schema_name: str) -> None:
    """Replay the operation log onto the baseline and demand the current state.

    This is the check that makes the ledger tamper-evident rather than merely
    well-formed: an entry edited in place without a matching operation shows up
    here as a replay mismatch, because the recorded history cannot produce it.
    """
    boundary = ledger["history_boundary"]
    baseline = boundary["baseline_state"]
    if boundary["baseline_sha256"] != hashlib.sha256(
            _canonical_json_bytes(baseline)).hexdigest():
        raise ValueError("ledger history boundary digest is invalid")
    replayed = json.loads(json.dumps(baseline))
    absorbed_count = boundary.get("absorbed_operation_count", 0)
    if absorbed_count > len(ledger["operations"]):
        raise ValueError("ledger absorbed-operation boundary exceeds history")
    absorbed = ledger["operations"][:absorbed_count]
    if "absorbed_operations_sha256" in boundary and boundary[
            "absorbed_operations_sha256"] != hashlib.sha256(
            _canonical_json_bytes(absorbed)).hexdigest():
        raise ValueError("ledger absorbed-operation boundary digest is invalid")
    for operation in ledger["operations"][absorbed_count:]:
        request = operation["request"]
        payload = request["payload"]
        result = operation["result"]
        completed_at = operation["completed_at"]
        if schema_name == "source_ledger.schema.json":
            entry = next((i for i in replayed
                          if i["file_name"] == payload["file_name"]), None)
            if entry is None:
                raise ValueError("ledger operation targets no baseline file")
            entry["review_status"] = payload["status"]
            entry["reviewed_by"] = payload["reviewer"]
            entry["reviewed_at"] = completed_at
            entry["rejection_reason"] = (
                payload["reason"] if payload["status"] == "rejected" else None)
        elif request["action"] == "add":
            replayed.append({
                "conflict_id": result["target_id"],
                "raised_by_stage": payload["stage"],
                "field_or_topic": payload["topic"],
                "sources": payload["sources"],
                "verdict": "pending",
                "resolution_note": None,
                "resolved_at": None,
            })
        else:
            entry = next((i for i in replayed
                          if i["conflict_id"] == payload["conflict_id"]), None)
            if entry is None:
                raise ValueError("ledger operation targets no replayed conflict")
            entry["verdict"] = payload["verdict"]
            entry["resolution_note"] = payload["note"]
            entry["resolved_at"] = completed_at
    current = (ledger["files"] if schema_name == "source_ledger.schema.json"
               else ledger["conflicts"])
    if replayed != current:
        raise ValueError("ledger state does not replay from its history boundary")


def _validate_predecessor_final_state(ledger: dict, schema_name: str) -> None:
    """Refuse to absorb a predecessor whose state contradicts its own receipts.

    Absorbing operations means trusting that the current state is what they
    produced. If the newest receipt for an entry disagrees with that entry, the
    ledger was edited outside its operation log, and freezing that into a new
    baseline would make the edit permanent and invisible.
    """
    if schema_name == "source_ledger.schema.json":
        latest = {}
        for operation in ledger["operations"]:
            latest[operation["request"]["payload"]["file_name"]] = operation
        current = {entry["file_name"]: entry for entry in ledger["files"]}
        for file_name, operation in latest.items():
            payload = operation["request"]["payload"]
            entry = current.get(file_name)
            if entry is None:
                raise ValueError(
                    "predecessor source ledger receipt targets a missing file")
            expected_reason = (
                payload["reason"] if payload["status"] == "rejected" else None)
            if (entry["review_status"] != payload["status"]
                    or entry.get("reviewed_by") != payload["reviewer"]
                    or entry.get("rejection_reason") != expected_reason):
                raise ValueError(
                    "predecessor source ledger contradicts its latest receipt")
        return
    expected = {}
    for operation in ledger["operations"]:
        request = operation["request"]
        payload = request["payload"]
        target_id = operation["result"]["target_id"]
        if request["action"] == "add":
            expected[target_id] = {
                "raised_by_stage": payload["stage"],
                "field_or_topic": payload["topic"],
                "sources": payload["sources"],
                "verdict": "pending",
                "resolution_note": None,
            }
        else:
            expected.setdefault(target_id, {})
            expected[target_id].update({
                "verdict": payload["verdict"],
                "resolution_note": payload["note"],
            })
    current = {entry["conflict_id"]: entry for entry in ledger["conflicts"]}
    for target_id, fields in expected.items():
        if target_id not in current:
            raise ValueError(
                "predecessor conflict ledger receipt targets a missing conflict")
        if any(current[target_id].get(k) != v for k, v in fields.items()):
            raise ValueError(
                "predecessor conflict ledger contradicts its latest receipt")


def _normalize_predecessor_timestamps(ledger: dict, schema_name: str) -> None:
    """Align an absorbed entry's timestamp with the receipt that produced it.

    The 60-second window is the tolerance for state authored just before its
    receipt was committed. A negative or larger gap means the two were not
    written by the same operation, so the pairing is refused rather than
    normalized into agreement.
    """
    latest = {}
    for operation in ledger["operations"]:
        payload = operation["request"]["payload"]
        target_id = (payload["file_name"]
                     if schema_name == "source_ledger.schema.json"
                     else operation["result"]["target_id"])
        latest[target_id] = operation
    state_key = ("files" if schema_name == "source_ledger.schema.json"
                 else "conflicts")
    id_key = "file_name" if state_key == "files" else "conflict_id"
    current = {entry[id_key]: entry for entry in ledger[state_key]}
    for target_id, operation in latest.items():
        entry = current[target_id]
        action = operation["request"]["action"]
        completed_at = operation["completed_at"]
        timestamp_key = "reviewed_at" if state_key == "files" else "resolved_at"
        if state_key == "conflicts" and action == "add":
            if entry.get(timestamp_key) is not None:
                raise ValueError(
                    "pending predecessor conflict has a resolution timestamp")
            continue
        authored_at = entry.get(timestamp_key)
        if not isinstance(authored_at, str):
            raise ValueError("predecessor receipt has no authored state timestamp")
        try:
            authored = datetime.fromisoformat(authored_at)
            completed = datetime.fromisoformat(completed_at)
        except ValueError as exc:
            raise ValueError("predecessor state timestamp is invalid") from exc
        delay = (completed - authored).total_seconds()
        if delay < 0 or delay > 60:
            raise ValueError(
                "predecessor state timestamp is inconsistent with its receipt")
        entry[timestamp_key] = completed_at


def _normalize_legacy_generic_ledger(ledger: dict, *, version: str,
                                     state_key: str, schema_name: str,
                                     predecessor_versions: set) -> dict:
    """Bring a pre-history-chain ledger up to the current version.

    A ledger written before the chain existed carries no operations to replay,
    so its current state becomes the baseline (`legacy_snapshot`) rather than
    being rejected -- the history starts now instead of pretending to reach
    back. A ledger that has operations but no boundary is validated WITHOUT
    replay first, then its operations are absorbed into the new boundary.
    """
    has_version = "ledger_version" in ledger
    has_operations = "operations" in ledger
    has_boundary = "history_boundary" in ledger
    if not has_version and not has_operations and not has_boundary:
        ledger = json.loads(json.dumps(ledger))
        ledger["ledger_version"] = version
        ledger["operations"] = []
        ledger["history_boundary"] = make_history_boundary(
            ledger[state_key], mode="legacy_snapshot",
            established_at=(ledger.get("updated_at") or ledger.get("created_at")
                            or "1970-01-01T00:00:00+00:00"))
        return ledger
    if (has_version and has_operations and not has_boundary
            and ledger.get("ledger_version") in predecessor_versions):
        candidate = json.loads(json.dumps(ledger))
        candidate["ledger_version"] = version
        candidate["history_boundary"] = make_history_boundary(
            candidate[state_key], mode="legacy_snapshot")
        _validate_generic_ledger(candidate, candidate.get("case_id"),
                                 schema_name, replay_history=False)
        _validate_predecessor_final_state(candidate, schema_name)
        absorbed = candidate["operations"]
        predecessor_state = json.loads(json.dumps(candidate[state_key]))
        _normalize_predecessor_timestamps(candidate, schema_name)
        candidate["history_boundary"] = make_history_boundary(
            candidate[state_key], mode="legacy_snapshot",
            absorbed_operations=absorbed,
            predecessor_state=predecessor_state)
        return candidate
    if not (has_version and has_operations and has_boundary):
        raise ValueError("ledger version/operation/history boundary is incomplete")
    return ledger


def _prepare_ledger_operation(ledger: dict, args, action: str,
                              payload: dict) -> tuple:
    """Build an operation envelope, or return the one already committed.

    The idempotency here is the point of `--operation-id`: a retried approval
    with the same id and the same request is a no-op that returns the original
    result, while the same id carrying a DIFFERENT request is refused rather
    than silently overwriting history.
    """
    operation_id = getattr(args, "operation_id", None)
    if not isinstance(operation_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", operation_id):
        raise ValueError("operation_id has an invalid format")
    request = {"case_id": args.case_id, "operation_id": operation_id,
               "action": action, "payload": payload}
    request_sha256 = hashlib.sha256(_canonical_json_bytes(request)).hexdigest()
    matches = [op for op in ledger["operations"]
               if op.get("operation_id") == operation_id]
    if len(matches) > 1:
        raise ValueError("duplicate committed operation_id")
    if matches:
        if matches[0].get("request_sha256") != request_sha256:
            raise ValueError("operation_id was committed for a different request")
        if matches[0].get("request") != request:
            raise ValueError("operation_id request envelope is inconsistent")
        result = matches[0].get("result")
        if not isinstance(result, dict):
            raise ValueError("committed operation result is invalid")
        return request, request_sha256, result
    return request, request_sha256, None


def _commit_ledger_operation(ledger: dict, args, request: dict,
                             request_sha256: str, result: dict,
                             completed_at: str) -> None:
    ledger["operations"].append({
        "operation_id": args.operation_id,
        "request": request,
        "request_sha256": request_sha256,
        "result": result,
        "completed_at": completed_at,
    })


def validated_source_ledger(case_id: str) -> dict:
    """The source ledger, proven to bind to its own recorded history.

    This is what `intake_case.py --execute` calls before copying anything: a
    ledger whose approvals cannot be replayed from its baseline is not an
    approval record, and D2's whole point is that the copy is gated on a real
    one.
    """
    ledger = load_json(source_ledger_path(case_id))
    if ledger is None:
        raise ValueError("source ledger not found")
    ledger = _normalize_legacy_generic_ledger(
        ledger, version="source_ledger.v0.4", state_key="files",
        schema_name="source_ledger.schema.json",
        predecessor_versions={"source_ledger.v0.3"})
    _validate_generic_ledger(ledger, case_id, "source_ledger.schema.json")
    return ledger


def validated_conflict_ledger(case_id: str, *, allow_missing: bool = False) -> dict:
    """The conflict ledger, same binding guarantee as the source ledger."""
    existing = load_json(conflict_ledger_path(case_id))
    if existing is not None:
        existing = _normalize_legacy_generic_ledger(
            existing, version="conflict_ledger.v0.3", state_key="conflicts",
            schema_name="conflict_ledger.schema.json",
            predecessor_versions={"conflict_ledger.v0.2"})
        _validate_generic_ledger(existing, case_id,
                                 "conflict_ledger.schema.json")
        return existing
    if not allow_missing:
        raise ValueError("conflict ledger is missing")
    return {
        "ledger_version": "conflict_ledger.v0.3",
        "case_id": case_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "conflicts": [],
        "history_boundary": make_history_boundary([], mode="native"),
        "operations": [],
    }


def validated_run_state(case_id: str, *, allow_missing: bool = False) -> dict:
    """The run state, schema-valid and free of duplicate ids.

    Narrower than 633bd7b0's version by design: the reconciliation-operation
    bindings it also checked belong to the medical-review flow, which is not
    transplanted here (see the C-scope note in the commit message). Everything
    checked below is independent of that flow.
    """
    path = run_state_path(case_id)
    path_missing = not path.exists()
    state = load_run_state(case_id)
    if _medical_artifacts_present(case_id):
        state["medical_review_adopted"] = True
    if allow_missing and path_missing:
        return state
    errors = _schema_check(state, "run_state.schema.json")
    if errors:
        raise ValueError("run state is invalid: " + "; ".join(errors))
    if state.get("case_id") != case_id:
        raise ValueError("run state belongs to a different case")
    stage_names = [entry["stage_name"] for entry in state["stages"]]
    if len(stage_names) != len(set(stage_names)):
        raise ValueError("run state has duplicate stage_name")
    human_input_ids = [entry["human_input_id"]
                       for entry in state.get("human_input_status", [])
                       if "human_input_id" in entry]
    if len(human_input_ids) != len(set(human_input_ids)):
        raise ValueError("run state has duplicate human_input_id")
    return state


def make_history_boundary(files, *, mode: str, established_at: str = None,
                          absorbed_operations: list = None,
                          forked_operations: list = None,
                          predecessor_state: list = None):
    """Make the canonical, lossless baseline binding for a ledger.

    The digest is over deterministic JSON of the exact entry list.  Callers
    receive an independent JSON-shaped copy, so later ledger mutations cannot
    retroactively change the history baseline this boundary attests to.

    `absorbed_operations` records operations that happened BEFORE this boundary
    was established -- an upgraded ledger keeps them in `operations` for the
    audit trail, but they are not replayed onto the new baseline, since the
    baseline already reflects them. Counting and digesting them is what stops
    that exemption from becoming a place to hide an extra operation.
    """
    if mode not in {"native", "legacy_snapshot"}:
        raise ValueError(f"unsupported source-ledger history mode: {mode!r}")
    baseline_state = json.loads(json.dumps(
        files, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    boundary = {
        "mode": mode,
        "established_at": established_at or now_iso(),
        "baseline_sha256": hashlib.sha256(
            _canonical_json_bytes(baseline_state)).hexdigest(),
        "baseline_state": baseline_state,
    }
    if absorbed_operations is not None:
        absorbed = json.loads(json.dumps(absorbed_operations))
        boundary["absorbed_operation_count"] = len(absorbed)
        boundary["absorbed_operations_sha256"] = hashlib.sha256(
            _canonical_json_bytes(absorbed)).hexdigest()
    if forked_operations is not None:
        forked = json.loads(json.dumps(forked_operations))
        boundary["forked_operation_count"] = len(forked)
        boundary["forked_operations_sha256"] = hashlib.sha256(
            _canonical_json_bytes(forked)).hexdigest()
    if predecessor_state is not None:
        predecessor = json.loads(json.dumps(predecessor_state))
        boundary["predecessor_state_sha256"] = hashlib.sha256(
            _canonical_json_bytes(predecessor)).hexdigest()
    return boundary

def source_ledger_path(case_id: str) -> Path:
    return case_dir(case_id) / "_source_ledger.json"


@traced_read("dao.read_ledger")
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
        # A v0.4 ledger carries a replayable history, so a status change has to
        # be RECORDED as an operation, not just written into the entry. Writing
        # it in place leaves a ledger whose current state cannot be replayed
        # from its own baseline -- which validated_source_ledger() rejects, and
        # rightly: that is indistinguishable from tampering. (This is what
        # CASE_142 hit: --init-ledger wrote v0.4 while this writer still wrote
        # v0.3-style.) A legacy ledger is upgraded here, on first mutation.
        #
        # The ledger is validated BEFORE it is extended: a writer that appends
        # to a tampered history would launder the tampering into a longer chain
        # that still verifies from its new baseline. Read-side validation alone
        # cannot prevent that, so the write path re-proves the chain too.
        try:
            ledger = _normalize_legacy_generic_ledger(
                ledger, version="source_ledger.v0.4", state_key="files",
                schema_name="source_ledger.schema.json",
                predecessor_versions={"source_ledger.v0.3"})
            _validate_generic_ledger(
                ledger, args.case_id, "source_ledger.schema.json")
        except ValueError as exc:
            print(f"ERROR: source ledger is not writable: {exc}")
            return 1

        payload = {"file_name": args.file_name, "status": args.status,
                   "reviewer": args.reviewer, "reason": args.reason}
        try:
            request, request_sha256, committed = _prepare_ledger_operation(
                ledger, args, "set_status", payload)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 1
        if committed is not None:
            print(f"OK: {args.file_name} -> {args.status} (already recorded"
                  f" under operation_id {args.operation_id})")
            return 0
        operation = (request, request_sha256)

        found = False
        completed_at = now_iso()
        for entry in ledger["files"]:
            if entry["file_name"] == args.file_name:
                entry["review_status"] = args.status
                entry["reviewed_by"] = args.reviewer
                entry["reviewed_at"] = completed_at
                entry["rejection_reason"] = args.reason if args.status == "rejected" else None
                found = True
                break
        if not found:
            print(f"NOT_FOUND: no entry for file {args.file_name!r} in ledger")
            return 1
        if operation is not None:
            request, request_sha256 = operation
            _commit_ledger_operation(
                ledger, args, request, request_sha256,
                {"action": "set_status", "target_id": args.file_name,
                 "status": args.status}, completed_at)
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
    # "Clear" has to mean the approvals are real, not merely that the strings
    # say "approved" -- this is the gate --execute and the SLA marker both hang
    # off, so a ledger whose history does not replay must not read as clear.
    try:
        ledger = validated_source_ledger(args.case_id)
    except ValueError as exc:
        if "not found" in str(exc):
            print(json.dumps({"clear": False, "error": "ledger not found"}))
        else:
            print(json.dumps({"clear": False, "error": str(exc)},
                             ensure_ascii=False))
        return 1
    pending = [e["file_name"] for e in ledger["files"] if e["review_status"] == "pending"]
    rejected = [e["file_name"] for e in ledger["files"] if e["review_status"] == "rejected"]
    clear = not pending and not rejected
    if clear:
        # SLA start (plan B10). This check is exactly the moment the plan
        # defines as "human approval finished, automatic execution resumes",
        # and it is a check every path already performs -- so the clock starts
        # here rather than in the frontend, and a CLI-driven or scripted run is
        # measured identically to a UI-driven one.
        #
        # It does NOT enforce who approved: D2 is PoC-only and the goal is
        # fewer human touchpoints, so once D2 is gone this check simply always
        # passes and the marker still fires at the same place.
        _emit_sla_marker(args.case_id, getattr(args, "run_id", None),
                         "sla.phase1.start")
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


TEMPLATE_REGISTRY = ROOT / "templates" / "registry.json"

# A numbered item: "1) ...". Deliberately NOT 가./나./다., which head sub-blocks
# rather than assert anything -- including them made every sub-heading a hit.
_NUMBERED_ITEM_RE = re.compile(r"^(\s*)[0-9]+\)\s")

# A bare 가./나./다. sub-heading -- a label with no predicate, e.g.
# "- 나. 특별약관의 적정성 여부". Only when nothing follows the topic: the same
# marker introducing a full sentence ("가. ... 특별약관은 ... 정한다") IS an
# assertion and stays in scope. Found on CASE_021 v2, where the bare form was
# the check's one clear false positive.
_BARE_SUBHEADING_RE = re.compile(r"^[-*\s]*[가-힣]\.\s*[^:.——]{0,40}$")

# A statutory or policy-clause reference. An untagged sentence naming one
# asserts that a specific provision exists and says something -- the shape of
# CASE_907's CF-1 paragraph.
_STATUTORY_REF_RE = re.compile(r"제\s*\d+\s*조|상법|민법|약관상|판례|특별약관|보통약관")

# Sentences that legitimately owe no citation: internal cross-references, P3
# hedges routing a question to a human, and explicit statements of
# indeterminacy. Without these the sibling rule flags the draft's own
# disciplined hedging, which would train the critic to ignore the check.
_NO_CITATION_OWED_RE = re.compile(
    r"참조|요함|따름|미확정|확인\s*불가|검토\s*의견이며|아래|위\s*\d+항|해당\s*없음|미확인")


def _analytical_patterns(template_key: str | None) -> list[str] | None:
    """Heading patterns for the sections that argue toward a conclusion.

    Section structure is the registry's job (open-decisions.md #2), so the
    scope of this check is declared there rather than hardcoded here -- a
    template that renames or renumbers its analysis sections updates one file.
    Returns None when the key is unknown or declares nothing, which the caller
    reports as a setup failure rather than silently scanning everything.
    """
    if not template_key:
        return None
    try:
        registry = json.loads(TEMPLATE_REGISTRY.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    entry = (registry.get("templates") or {}).get(template_key) or {}
    return entry.get("analytical_heading_patterns") or None


def find_untagged_claims(text: str, analytical_patterns: list[str]) -> list[dict]:
    """Lines in analytical sections that assert something but cite nothing.

    read-evidence-tags compares the tags PRESENT against the sidecar, so a
    paragraph with zero tags is in neither set and is structurally invisible to
    it -- making an untagged fabrication *less* detectable than a badly-tagged
    one (known-gaps item 38). This is the deterministic floor under that layer,
    the way check-forbidden-expressions floors the semantic P3 pass.

    Two signals, both measured against CASE_907's real drafts:

      statutory_ref_no_citation -- an untagged line naming a statute or policy
        clause. Precise: 2 hits on v1, one of them exactly the CF-1 paragraph.

      untagged_among_cited_siblings -- an untagged numbered item whose siblings
        at the same indent are cited (>=2 of them). This catches what the
        keyword signal cannot: CASE_907's IV-1-나 3) restated the insurer's
        argument with no citation and contains no statutory word at all, so the
        cheap keyword rule that known-gaps item 38 proposed would have MISSED
        it. The sibling asymmetry is the stronger signal -- a list whose other
        items all cite evidence declares by its own structure what the
        uncited one owes.

    Record-only, and hits are candidates: a hit means "a human should look",
    never an automatic failure. Sentences that legitimately owe no citation
    (cross-references, hedges, statements of absence) are excluded, because a
    check that flags the draft's own P3 discipline teaches the critic to
    ignore it.
    """
    compiled = [re.compile(p) for p in analytical_patterns]
    in_scope = False
    candidates = []          # (line_no, indent, text, tagged)
    for line_no, line in enumerate(text.split("\n"), start=1):
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            in_scope = any(rx.search(heading) for rx in compiled)
            continue
        if not in_scope or not line.strip():
            continue
        # Bold run-in headings ("**1. 손해배상책임**") label a block; they assert
        # nothing on their own.
        if line.strip().startswith("**") and line.strip().endswith("**"):
            continue
        if _BARE_SUBHEADING_RE.match(line.strip()):
            continue
        tagged = bool(TAG_RE.search(line))
        item = _NUMBERED_ITEM_RE.match(line)
        candidates.append((line_no, item.group(1) if item else None, line.strip(), tagged))

    findings = []
    for line_no, _indent, body, tagged in candidates:
        # The exemptions do NOT apply to this signal. They are line-wide, so a
        # sentence that both invokes a statute and refers to another section
        # would be excused by the cross-reference -- which is exactly what
        # happened to CASE_907's CF-1 line: "상법 제724조 제2항 및 약관상 ...
        # 직접청구 규정에 따른 ... 위 1항의 ... 따라 달라짐" was suppressed by
        # `위 1항`, silently dropping the single most important hit. Pointing at
        # another section does not discharge the duty to cite a provision you
        # assert the content of.
        if not tagged and _STATUTORY_REF_RE.search(body):
            findings.append({"line": line_no, "signal": "statutory_ref_no_citation",
                             "text": body[:200]})

    # Sibling asymmetry, grouped by indent among numbered items only.
    groups: dict[str, list[tuple]] = {}
    for line_no, indent, body, tagged in candidates:
        if indent is not None:
            groups.setdefault(indent, []).append((line_no, body, tagged))
    already = {f["line"] for f in findings}
    for _indent, group in groups.items():
        cited = [g for g in group if g[2]]
        if len(cited) < 2:
            # One cited sibling is too thin to call a norm for the list.
            continue
        for line_no, body, tagged in group:
            if tagged or line_no in already or _NO_CITATION_OWED_RE.search(body):
                continue
            findings.append({"line": line_no, "signal": "untagged_among_cited_siblings",
                             "text": body[:200]})

    return sorted(findings, key=lambda f: f["line"])


def split_core_field_accuracy(field_comparisons: list[dict]) -> dict:
    """Split core_field_accuracy into fact-extraction vs discretionary agreement.

    A single accuracy number silently measures different things depending on
    what the answer key happens to be. CASE_907's ground truth was a 손해액
    산정서, so normative values (과실률, which figure counts as 손해액) landed in
    the denominator and BOTH of its misses came from there -- its 8 fact fields
    matched 8/8. Reported as one number, 0.80 reads as worse than CASE_022's
    0.875 when the two are not measuring the same capability.

    Computed here rather than by hand: the CASE_112 run notes record a
    core_field_accuracy that was first assembled as 13/16 and corrected to
    12/16 before writing. Arithmetic over a list is not where judgment belongs.

    Unclassified comparisons (pre-2026-08-04 files, which have no field_kind)
    are counted in `unclassified` and excluded from BOTH sub-scores rather than
    being guessed into one -- an inferred classification would silently move
    the very numbers this split exists to keep honest.
    """
    buckets: dict[str, list[dict]] = {"fact": [], "discretionary": [], "unclassified": []}
    for comparison in field_comparisons or []:
        kind = comparison.get("field_kind")
        buckets[kind if kind in ("fact", "discretionary") else "unclassified"].append(comparison)

    def rate(items):
        if not items:
            return None
        return round(sum(1 for i in items if i.get("match")) / len(items), 4)

    return {
        "fact_extraction_score": rate(buckets["fact"]),
        "discretionary_agreement_score": rate(buckets["discretionary"]),
        "overall_score": rate(field_comparisons or []),
        "counts": {
            "fact": len(buckets["fact"]),
            "fact_matched": sum(1 for i in buckets["fact"] if i.get("match")),
            "discretionary": len(buckets["discretionary"]),
            "discretionary_matched": sum(1 for i in buckets["discretionary"] if i.get("match")),
            "unclassified": len(buckets["unclassified"]),
            "total": len(field_comparisons or []),
        },
    }


def cmd_split_core_field_accuracy(args):
    """Recompute a written evaluation_result's accuracy split from its own
    field_comparisons. Read-only -- prints, never writes."""
    _require_safe_id("case id", args.case_id)
    data = read_contract_data(args.case_id, args.filename)
    if data is None:
        print(f"NOT_FOUND: {args.filename} for {args.case_id}")
        return 1
    cfa = data.get("core_field_accuracy") or {}
    result = split_core_field_accuracy(cfa.get("field_comparisons") or [])
    result["recorded_score"] = cfa.get("score")
    if result["overall_score"] is not None and cfa.get("score") is not None:
        # A recorded score that disagrees with its own comparison list is a
        # counting error, the exact kind CASE_112's notes caught by hand.
        result["recorded_score_matches"] = abs(result["overall_score"] - cfa["score"]) < 0.005
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_check_untagged_claims(args):
    """Deterministic floor for the untagged-claim shape read-evidence-tags
    cannot see. Record-only -- the critic decides `passed`."""
    doc_path = Path(args.doc_path)
    if not doc_path.exists():
        print(f"NOT_FOUND: {doc_path}")
        return 1
    patterns = _analytical_patterns(args.template)
    if patterns is None:
        # Never fall back to scanning the whole document: a silent whole-file
        # scan would flood the caller with headings and facts sections and
        # read as "the check ran".
        print(f"NO_ANALYTICAL_SECTIONS: template {args.template!r} declares no "
              f"analytical_heading_patterns in {TEMPLATE_REGISTRY.name}")
        return 2

    text = doc_path.read_text(encoding="utf-8")
    # A known template whose headings appear nowhere in THIS document scans
    # zero lines and returns clean -- indistinguishable from a real pass, and
    # reported as one. Caught on CASE_909: rebuttal_points.md was checked with
    # the 배상책임_후유장해형 patterns (IV/V/VI of a draft report), matched no
    # section, and came back clean. NO_ANALYTICAL_SECTIONS already refuses an
    # unknown template for exactly this reason; an inapplicable one is the same
    # failure with a different cause, so it gets the same refusal rather than a
    # pass nobody can distinguish from evidence.
    # Match the scanner's own normalization -- it strips the markdown heading
    # marker before testing (`line.lstrip("#").strip()`), so testing the raw
    # line here would report every rendered document as unmatched.
    headings = [line.lstrip("#").strip() for line in text.split("\n")]
    matched = [p for p in patterns
               if any(re.match(p, h) for h in headings)]
    if not matched:
        print(f"NO_MATCHING_SECTIONS: none of template {args.template!r}'s "
              f"analytical headings appear in {doc_path.name} -- "
              f"{patterns}. Zero lines were scanned, so this is not a clean "
              "result. Pass the template this document was rendered from; a "
              "document kind with no registry contract (e.g. rebuttal_points) "
              "has no analytical scope to check and must not be reported as "
              "having passed one.")
        return 2

    findings = find_untagged_claims(text, patterns)
    print(json.dumps({
        "clean": not findings,
        "template": args.template,
        "analytical_sections": patterns,
        "sections_matched": matched,
        "findings": findings,
        "note": ("candidates, not verdicts -- an untagged line may legitimately owe no citation. "
                 "Scope is the analytical sections only; this is a floor under the critic's "
                 "reading, not a replacement for it."),
    }, ensure_ascii=False))
    return 0 if not findings else 1


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
    # Instrumented because load_registry() re-globs and re-parses all ~34
    # schema files on every call, with no cache, and this runs INSIDE locks --
    # so its cost is paid in held lock time, which bounds how well anything
    # can parallelise. T2 proposes caching it; this span is what will show
    # whether that was worth doing.
    with trace_mod.span("validate.schema", category="validate",
                        schema_name=schema_name) as sp:
        schemas, registry = load_registry()
        errors = validate_instance(data, schema_name, schemas, registry)
        sp.set(error_count=len(errors))
        return errors


# ---------------------------------------------------------------- run state ops --

def _update_run_state(case_id, run_id, stage, status, held_by, backup_path=None,
                       finalize=False, dep_check="hard", explicit_attempt=True,
                       attempt_outcome=None):
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
    emit_marker = None
    marker_attempt = None
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

        # Medical-appropriateness clearance. Deliberately consulted HERE, on
        # the real transition path, rather than only defined: the flag this
        # replaces was written by three call sites and read by none, which is
        # why the gate went dark for 53 cases without anything failing. A test
        # drives cmd_finalize_stage itself for the same reason.
        try:
            _require_transition_medical_clearance(
                case_id, state.get("run_id"), state, stage, status)
        except ValueError as exc:
            print(f"REFUSED: cannot advance run-state for {target}:")
            print(f"  - {exc}")
            return None

        stages = state["stages"]
        entry = next((s for s in stages if s["stage_name"] == stage), None)
        if entry is None:
            entry = {"stage_name": stage, "status": "pending", "started_at": None,
                      "completed_at": None, "attempt_count": 0, "backup_path": None}
            stages.append(entry)
        previous_status = entry.get("status")
        # A stage leaving `passed` un-grounds everything derived from it, so
        # note the demotion here (inside the lock, where the old status is
        # authoritative) and cascade after the lock is released -- the cascade
        # takes the same lock (Part 11I).
        if (entry.get("status") == "passed"
                and status in ("failed", "pending", "in_progress")):
            demoted_from = "passed"
        if status == "in_progress":
            if explicit_attempt:
                if previous_status == "in_progress" and entry.get("attempt_count", 0) > 0:
                    # An orchestrator replay is not a P9 retry. A retry must
                    # first close the prior attempt as failed/partial; treating
                    # a duplicate begin as idempotent prevents one invocation
                    # from becoming several attempts merely because a command
                    # was repeated.
                    #
                    # attempt_count > 0 is what makes this an *explicit* attempt
                    # rather than merely the in_progress status: an incidental
                    # contract write advances pending->in_progress while leaving
                    # attempt_count at 0, and treating that as an open attempt
                    # swallowed the real dispatch that followed -- the stage then
                    # ran, passed, and contributed no measured interval at all.
                    return state
                # started_at keeps the stage's first explicit origin;
                # current_attempt_started_at tracks this dispatch only.
                now = now_iso()
                entry["started_at"] = entry["started_at"] or now
                entry["current_attempt_started_at"] = now
                entry["attempt_count"] += 1
                marker_attempt = entry["attempt_count"]
                emit_marker = "start"
            elif previous_status not in ("failed", "passed", "skipped"):
                # Contract writes are checkpoints inside an invocation, not
                # invocation boundaries. Preserve the historical best-effort
                # pending->in_progress projection but do not move attempt
                # timestamps or increment the retry counter. If orchestration
                # skipped the explicit begin, aggregate-trace reports missing
                # coverage instead of inventing a start at the first output.
                pass
            else:
                # A durable output may still land after a stage failed, but an
                # incidental writer is not authorized to start a retry.
                return state
        if status in ("passed", "failed", "skipped"):
            entry["completed_at"] = now_iso()
            # A stage can reach a terminal status without ever having been
            # marked in_progress (CASE_907's policy_clause_processing ended
            # with started_at: null). Saying so is better than leaving a hole
            # that reads as missing data: the marker genuinely was never
            # moved, and no start time can be invented after the fact.
            if not entry.get("started_at"):
                entry["timing_note"] = ("no start marker was ever recorded for this stage -- "
                                        "completed_at is the only timestamp, and no duration "
                                        "can be derived from it")
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
        if (status == "failed" and previous_status == "in_progress"
                and entry.get("attempt_count", 0) > 0):
            marker_attempt = entry["attempt_count"]
            emit_marker = ("abandoned" if attempt_outcome == "interrupted"
                           else "end")
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
    if emit_marker == "start":
        _emit_stage_attempt_marker(
            case_id, run_id, stage, marker_attempt, "start")
    elif emit_marker == "end":
        _emit_stage_attempt_marker(
            case_id, run_id, stage, marker_attempt, "end",
            outcome=attempt_outcome or "failed")
    elif emit_marker == "abandoned":
        _emit_stage_attempt_marker(
            case_id, run_id, stage, marker_attempt, "abandoned",
            outcome="interrupted")
    return updated


def cmd_update_run_state(args):
    outcome = getattr(args, "attempt_outcome", None)
    if outcome is not None and args.status != "failed":
        print("REFUSED: --attempt-outcome is valid only with status 'failed'")
        return 1
    state = _update_run_state(
        args.case_id, args.run_id, args.stage, args.status, args.held_by,
        attempt_outcome=outcome)
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
            # Emit the wait as a human_wait span so active_s can deduct it.
            #
            # active_s is DEFINED as sla_wall_clock_s minus human_wait, and
            # trace_aggregate has computed that deduction (as an interval
            # union, so two gates open at once are not double-counted) since
            # the timing layer was written -- but NOTHING ever emitted a
            # human_wait span, so the deduction always subtracted zero. A
            # 30-minute SLA measured that way silently includes however long a
            # person took to answer, which is exactly the number the SLA is
            # not about.
            #
            # Emitted on 'received' rather than at 'waiting' because only now
            # is the interval closed; the span is back-dated to requested_at so
            # it lands inside the SLA window where the wait actually happened.
            _emit_human_wait_span(case_id, run_id, stage,
                                  entry.get("requested_at"), entry["received_at"])
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


@traced_read("dao.get_last_passed_stage")
def cmd_get_last_passed_stage(args):
    state = load_run_state(args.case_id)
    passed = [s["stage_name"] for s in state["stages"] if s["status"] == "passed"]
    print(passed[-1] if passed else "NONE")
    return 0


@trace_mod.traced("dao.snapshot", category="io")
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
            # _trace/ is diagnostic scratch, not contract data: nothing
            # downstream reads it and there is nothing in it to restore. It
            # also GROWS through the run, so copying it into every cumulative
            # snapshot is precisely the O(stages x tree) cost that makes
            # finalize a serial tail. The permanent artifact derived from it
            # (_timing_summary.json) is a normal file and is still snapshotted.
            if item.name in ("_backups", "_trace") or item.name.endswith(".lock"):
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


@trace_mod.traced("dao.finalize_stage")
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
    journal_blockers = dao_transaction.pending_journal_errors(
        case_dir(case_id))
    if journal_blockers:
        print(f"REFUSED: cannot finalize {stage!r} while a DAO transaction "
              "is incomplete:")
        for blocker in journal_blockers:
            print(f"  - {blocker}")
        return None
    target = run_state_path(case_id)
    existing_lock = acquire_lock_blocking(
        target, held_by, run_id or "unknown", f"finalize stage: {stage}")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return None
    published = None
    finalized_attempt = None
    finalized_state = None
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

        # Medical-appropriateness clearance, enforced on the finalize path as
        # well as on _update_run_state. These are two independent routes to
        # 'passed' -- finalize-stage does NOT delegate to _update_run_state --
        # so a gate wired into only one of them is a gate with a way around
        # it. Found exactly that way: the finalize test kept passing with the
        # _update_run_state call deleted, because it was refusing for an
        # unrelated policy-layer reason instead.
        try:
            _require_transition_medical_clearance(
                case_id, state.get("run_id"), state, stage, "passed")
        except ValueError as exc:
            print(f"REFUSED: cannot finalize {stage!r} -- medical clearance: {exc}")
            return None

        # P6. The guardrail says a stage proceeds only once every conflict
        # entry for the case reads resolved or false_positive, and pipeline.md
        # calls screening_report "gated on check-conflicts-clear". Neither was
        # true of the code: check-conflicts-clear existed only as a CLI command
        # an agent had to remember to run, and the finalize path never consulted
        # the ledger at all. Verified on CASE_909 -- screening_report finalized
        # cleanly with CONFLICT_1 still pending. Nothing went wrong there only
        # because the conflict happened to be adjudicated first.
        #
        # Scoped to the stages that CONSUME the case's factual picture. A
        # conflict is raised by consistency_check, so gating that stage would
        # make it unable to finalize the very finding it just recorded; intake
        # and document_processing run before any comparison exists to disagree
        # about. Everything downstream reasons FROM the contested facts, which
        # is exactly what the guardrail protects.
        if stage in CONFLICT_GATED_STAGES:
            pending = pending_conflict_ids(case_id)
            if pending:
                print(f"REFUSED: cannot finalize {stage!r} -- P6: the case has "
                      f"unresolved conflict ledger entries: {', '.join(pending)}")
                print("  Every entry must read 'resolved' or 'false_positive' "
                      "before a stage that reasons from the case's facts may "
                      "finalize. Adjudicate with `dao.py set-conflict-verdict "
                      "CASE_ID CONFLICT_ID {resolved|false_positive} --note ...` "
                      "-- a value is never silently discarded to close one.")
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
                # P0-6. Lineage above only established that the segment's own
                # declarations agree with each other. This asks the question
                # none of them can: does the parent PDF on disk right now still
                # confirm this mapping?
                derivation_blockers = _segment_derivation_blockers(
                    case_id, manifest)
                if derivation_blockers:
                    print("REFUSED: cannot finalize 'document_processing' -- "
                          "segment page maps are not bound to verified parent "
                          "provenance:")
                    for e in derivation_blockers:
                        print(f"  - {e}")
                    return None
                # This stage classifies, so this stage owns the consequence of
                # having classified from pre-redaction text.
                review_blockers = _classification_review_blockers(
                    case_id, manifest)
                if review_blockers:
                    print("REFUSED: cannot finalize 'document_processing' -- a "
                          "classification was produced from PRE-REDACTION page "
                          "text and no human review clears it:")
                    for e in review_blockers:
                        print(f"  - {e}")
                    print("  Review each one (does the classification's "
                          "evidence quote survive redaction?), then record it: "
                          "dao.py record-human-review CASE_ID --artifact-kind "
                          "classification_review --artifact-id DOC_XXX "
                          "--target-key classification_text_source:raw_page_text "
                          "--decision verified --reviewer NAME --note '...' "
                          "--held-by NAME --run-id RUN_ID, then write the "
                          "returned review_uid into the classification's "
                          "human_review_uid field.")
                    return None

        if stage == "policy_clause_processing":
            # P0-6 again, and not redundantly: document_processing may have
            # passed before a parent's raw bytes changed or a segment's text
            # was revised. Every policy clause offset and canonical UID is
            # keyed to the physical page this mapping names, so the policy
            # stage re-asks rather than inheriting an earlier answer -- the
            # same reasoning as Part 11F re-running source checks at
            # finalization instead of trusting the write-time pass.
            policy_manifest = read_contract_data(
                case_id, "document_manifest.json")
            if policy_manifest is not None:
                derivation_blockers = _segment_derivation_blockers(
                    case_id, policy_manifest)
                if derivation_blockers:
                    print("REFUSED: cannot finalize 'policy_clause_processing' "
                          "-- segment page maps are not bound to verified "
                          "parent provenance:")
                    for e in derivation_blockers:
                        print(f"  - {e}")
                    return None
            completion_blockers = _policy_completion_blockers(case_id)
            if completion_blockers:
                print("REFUSED: cannot finalize 'policy_clause_processing' -- "
                      "policy completeness gate is not clear:")
                for blocker in completion_blockers:
                    print(f"  - {blocker}")
                return None
        elif stage in stage_dependencies.dependents_of(
                "policy_clause_processing"):
            # P0-6 follow-up. A recorded policy pass can predate an out-of-band
            # parent-source change or a corrupted derivation receipt. Raw input
            # is immutable by rule, but a live finalization gate must still
            # refuse the representable bad state instead of relying on a
            # cascade that no DAO operation had a chance to trigger.
            downstream_manifest = read_contract_data(
                case_id, "document_manifest.json")
            if downstream_manifest is not None:
                derivation_blockers = _segment_derivation_blockers(
                    case_id, downstream_manifest)
                if derivation_blockers:
                    print(f"REFUSED: cannot finalize {stage!r} -- segment "
                          "derivation provenance is no longer current:")
                    for blocker in derivation_blockers:
                        print(f"  - {blocker}")
                    return None
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
        if entry.get("status") == "in_progress" and entry.get("attempt_count", 0) > 0:
            finalized_attempt = entry["attempt_count"]

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
        finalized_state = state
    finally:
        release_lock(target)
    if finalized_attempt is not None:
        _emit_stage_attempt_marker(
            case_id, run_id, stage, finalized_attempt, "end",
            outcome="passed")
    return finalized_state


def cmd_snapshot_backup(args):
    """Backward-compatible alias for finalize-stage: snapshot + passed, atomic.
    Kept so existing callers/tests using 'snapshot-backup' keep working; new
    code should read this as 'finalize this stage'."""
    state = _finalize_stage(args.case_id, args.run_id, args.stage, args.held_by)
    if state is None:
        return 1
    entry = next(s for s in state["stages"] if s["stage_name"] == args.stage)
    print(f"OK: finalized {args.stage} -> passed, snapshot at {entry['backup_path']}")
    # SLA end -- only on the success path, and only for the terminal stage. A
    # failed finalize returned above, so the window never closes on a stage
    # that did not actually pass.
    if args.stage == SLA_END_STAGE and _emit_sla_marker(
            args.case_id, args.run_id, "sla.phase1.end"):
        print(f"  sla.phase1.end recorded -- run `dao.py aggregate-trace "
              f"{args.case_id} --run-id {args.run_id} --held-by <name>` for timings")
    return 0


def cmd_finalize_stage(args):
    return cmd_snapshot_backup(args)


# ------------------------------------------------------------ conflict ledger --

def conflict_ledger_path(case_id: str) -> Path:
    return case_dir(case_id) / "_conflict_ledger.json"


def ensure_conflict_ledger(case_id: str, held_by: str, run_id: str) -> dict:
    """Initialize a zero-conflict ledger under DAO control, or return the
    existing one. Restored for the medical publication path, which requires a
    valid canonical conflict ledger before it will publish."""
    target = conflict_ledger_path(case_id)
    existing_lock = acquire_lock_blocking(
        target, held_by, run_id, "initialize canonical conflict ledger"
    )
    if existing_lock is not None:
        raise ValueError("conflict ledger remained locked")
    try:
        if target.exists() or target.is_symlink():
            return validated_conflict_ledger(case_id)
        ledger = validated_conflict_ledger(case_id, allow_missing=True)
        errors = _schema_check(ledger, "conflict_ledger.schema.json")
        if errors:
            raise ValueError(
                "generated conflict ledger is invalid: " + "; ".join(errors))
        atomic_write_json(target, ledger)
        return ledger
    finally:
        release_lock(target)


def load_conflict_ledger(case_id: str) -> dict:
    """The conflict ledger, upgraded to the history-tracked shape if needed.

    Unlike validated_conflict_ledger this does not replay -- callers that are
    about to WRITE validate explicitly first, and read-only callers should not
    acquire a new failure mode on a ledger they only count entries in.
    """
    existing = load_json(conflict_ledger_path(case_id))
    if existing is not None:
        return _normalize_legacy_generic_ledger(
            existing, version="conflict_ledger.v0.3", state_key="conflicts",
            schema_name="conflict_ledger.schema.json",
            predecessor_versions={"conflict_ledger.v0.2"})
    return {
        "ledger_version": "conflict_ledger.v0.3",
        "case_id": case_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "conflicts": [],
        "history_boundary": make_history_boundary([], mode="native"),
        "operations": [],
    }


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
        try:
            ledger = load_conflict_ledger(args.case_id)
            _validate_generic_ledger(ledger, args.case_id,
                                     "conflict_ledger.schema.json")
        except ValueError as exc:
            print(f"ERROR: conflict ledger is not writable: {exc}")
            return 1
        sources = json.loads(Path(args.sources_file).read_text(encoding="utf-8"))
        n = len(ledger["conflicts"]) + 1
        conflict_id = f"CONFLICT_{n}"
        payload = {"stage": args.stage, "topic": args.topic, "sources": sources}
        try:
            request, request_sha256, committed = _prepare_ledger_operation(
                ledger, args, "add", payload)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 1
        if committed is not None:
            print(f"OK: added {committed['target_id']} (already recorded under"
                  f" operation_id {args.operation_id})")
            return 0
        completed_at = now_iso()
        ledger["conflicts"].append({
            "conflict_id": conflict_id,
            "raised_by_stage": args.stage,
            "field_or_topic": args.topic,
            "sources": sources,
            "verdict": "pending",
            "resolution_note": None,
            "resolved_at": None,
        })
        _commit_ledger_operation(
            ledger, args, request, request_sha256,
            {"action": "add", "target_id": conflict_id, "status": "pending"},
            completed_at)
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
        try:
            ledger = load_conflict_ledger(args.case_id)
            _validate_generic_ledger(ledger, args.case_id,
                                     "conflict_ledger.schema.json")
        except ValueError as exc:
            print(f"ERROR: conflict ledger is not writable: {exc}")
            return 1
        entry = next((c for c in ledger["conflicts"] if c["conflict_id"] == args.conflict_id), None)
        if entry is None:
            print(f"NOT_FOUND: {args.conflict_id}")
            return 1
        payload = {"conflict_id": args.conflict_id, "verdict": args.verdict,
                   "note": args.note}
        try:
            request, request_sha256, committed = _prepare_ledger_operation(
                ledger, args, "set_verdict", payload)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 1
        if committed is not None:
            print(f"OK: {args.conflict_id} -> {args.verdict} (already recorded"
                  f" under operation_id {args.operation_id})")
            return 0
        completed_at = now_iso()
        entry["verdict"] = args.verdict
        entry["resolution_note"] = args.note
        entry["resolved_at"] = completed_at
        _commit_ledger_operation(
            ledger, args, request, request_sha256,
            {"action": "set_verdict", "target_id": args.conflict_id,
             "status": args.verdict}, completed_at)
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


def pending_conflict_ids(case_id: str) -> list[str]:
    """Conflict entries still awaiting a verdict. Empty = the case is clear.

    Shared by the CLI check and the finalize gate so the two cannot drift into
    disagreeing about what "clear" means -- the drift that let a documented
    gate be enforced in prose only.

    Validated, not merely read: "no pending verdicts" is only meaningful if the
    verdicts are the ones the ledger's own history records. A ledger that fails
    its chain raises rather than reporting clear, so P6 fails closed.
    """
    ledger = load_conflict_ledger(case_id)
    _validate_generic_ledger(ledger, case_id, "conflict_ledger.schema.json")
    return [c["conflict_id"] for c in ledger["conflicts"]
            if c.get("verdict") == "pending"]


# Stages a pending conflict blocks. consistency_check is deliberately absent:
# it is the stage that RAISES conflicts, so gating it would stop it finalizing
# the finding it just recorded. intake/document_processing run before any
# cross-document comparison exists. Everything else reasons from the case's
# factual picture, which is what P6 protects.
CONFLICT_GATED_STAGES = frozenset({
    "policy_clause_processing",
    "claim_analysis",
    "denial_response",
    "screening_report",
    "draft_report_v1",
    "draft_report_v2",
    "critic_v1",
    "critic_v2",
    "denial_validation",
    "evaluation",
})


@traced_read("dao.check_conflicts_clear")
def cmd_check_conflicts_clear(args):
    try:
        pending = pending_conflict_ids(args.case_id)
    except ValueError as exc:
        print(json.dumps({"clear": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    clear = not pending
    print(json.dumps({"clear": clear, "pending": pending}))
    return 0 if clear else 1


# ------------------------------------------------- medical review (T48) --
# Restored after merge 3569d50 discarded parent2's dao.py wholesale. The
# adjudication logic was never lost -- it lives in tools/medical_repository.py
# and tools/medical_review_ledger.py, which the merge kept. What went missing
# was every DAO-side entry point those modules are called through, which left
# them dead code: 53 cases on disk, zero medical artifacts, and a clearance
# gate that no longer ran.

POST_MEDICAL_STAGES = {
    "denial_response",
    "consistency_check",
    "screening_report",
    "draft_report_v1",
    "critic_v1",
    "human_review_v1",
    "denial_validation",
    "draft_report_v2",
    "critic_v2",
    "human_review_v2",
}


def _medical_clearance_required(
    state: dict,
    stage: str,
    status: str | None = None,
    *,
    snapshot: bool = False,
) -> bool:
    """Whether this transition must prove medical clearance first.

    `claim_analysis` is gated regardless of `medical_review_adopted`: the
    medical variables are an INPUT to that stage, so "this case has no medical
    review yet" is exactly the condition the gate exists to catch, not an
    exemption from it. Every later stage is gated only once the case has
    adopted the flow.
    """
    if not _medical_gate_applies(state):
        return False
    if stage == "claim_analysis":
        return snapshot or status == "passed"
    if not state.get("medical_review_adopted", False):
        return False
    return stage in POST_MEDICAL_STAGES and (
        snapshot or status in {"in_progress", "passed"}
    )


def _require_transition_medical_clearance(
    case_id: str,
    run_id: str,
    state: dict,
    stage: str,
    status: str | None = None,
    *,
    snapshot: bool = False,
) -> None:
    if not _medical_clearance_required(state, stage, status, snapshot=snapshot):
        return
    from medical_review_ledger import require_clearance

    require_clearance(sys.modules[__name__], case_id, run_id)


def cmd_write_medical_variables(args):
    import medical_repository

    return medical_repository.publish(sys.modules[__name__], args)


def cmd_read_medical_variables(args):
    import medical_repository

    return medical_repository.cmd_read_variables(sys.modules[__name__], args)


def cmd_read_medical_evidence(args):
    import medical_repository

    return medical_repository.cmd_read_evidence(sys.modules[__name__], args)


def cmd_check_medical_reviews_clear(args):
    from medical_review_ledger import cmd_check_clear

    return cmd_check_clear(sys.modules[__name__], args)


def _run_medical_review_mutation(command, args):
    """Fence every ledger mutation to the canonical run owner, then project
    the resulting waits back into run state."""
    state_path = run_state_path(args.case_id)
    owned_lock, existing_lock = acquire_owned_lock_blocking(
        state_path,
        args.held_by,
        args.run_id,
        "authorize canonical medical-review run owner",
    )
    if existing_lock is not None:
        print(
            f"LOCKED: held_by={existing_lock['held_by']} "
            f"run_id={existing_lock['run_id']}"
        )
        return 1
    assert owned_lock is not None
    try:
        try:
            state = validated_run_state(args.case_id, allow_missing=True)
        except ValueError as exc:
            print(f"BLOCKED: {exc}")
            return 1
        owner = state.get("run_id")
        if owner is None:
            variables, error = _load_medical_revision(args.case_id, None)
            if (
                error
                or variables is None
                or variables.get("run_id") != args.run_id
            ):
                print(
                    "BLOCKED: medical-review mutation does not match the "
                    "canonical revision run owner"
                )
                return 1
            state["run_id"] = args.run_id
            save_run_state(args.case_id, state)
        elif owner != args.run_id:
            print(
                "BLOCKED: medical-review mutation does not match the "
                "canonical run owner")
            return 1
        result = command(sys.modules[__name__], args)
    finally:
        release_owned_lock(owned_lock)
    if result == 0:
        from medical_review_ledger import reconcile_wait_projection

        projected, _, error = reconcile_wait_projection(
            sys.modules[__name__], args, blocking=False, automatic=True
        )
        if not projected:
            print(
                "WARNING: canonical medical-review mutation succeeded but "
                f"run-state wait projection requires reconciliation: {error}"
            )
    return result


def cmd_open_medical_review_item(args):
    from medical_review_ledger import cmd_open

    return _run_medical_review_mutation(cmd_open, args)


def cmd_record_medical_referral_decision(args):
    from medical_review_ledger import cmd_record_decision

    return _run_medical_review_mutation(cmd_record_decision, args)


def cmd_provide_medical_review_information(args):
    from medical_review_ledger import cmd_provide_information

    return _run_medical_review_mutation(cmd_provide_information, args)


def cmd_transition_medical_review(args):
    from medical_review_ledger import cmd_transition

    return _run_medical_review_mutation(cmd_transition, args)


def cmd_reconcile_medical_review_waits(args):
    from medical_review_ledger import reconcile_wait_projection

    projected, changed, error = reconcile_wait_projection(
        sys.modules[__name__], args
    )
    if not projected:
        print(f"FAIL: medical-review wait reconciliation failed: {error}")
        return 1
    print(json.dumps({
        "case_id": args.case_id,
        "changed": changed,
        "reconciled": True,
    }))
    return 0


def cmd_read_medical_review_ledger(args):
    from medical_review_ledger import cmd_read_ledger

    return cmd_read_ledger(sys.modules[__name__], args)


def cmd_read_medical_review_evidence(args):
    from medical_review_ledger import cmd_read_evidence

    return cmd_read_evidence(sys.modules[__name__], args)


def cmd_read_medical_review_outcomes(args):
    from medical_review_ledger import cmd_read_outcomes

    allowed_consumers = {
        "screening_report",
        "denial_validation",
        "draft_report_v1",
        "draft_report_v2",
    }
    if args.caller_stage not in allowed_consumers:
        print("BLOCKED: caller stage is not authorized for medical-review outcomes")
        return 1
    try:
        state = validated_run_state(args.case_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"BLOCKED: {exc}")
        return 1
    if state.get("run_id") != args.run_id:
        print("BLOCKED: outcome read does not match the canonical run owner")
        return 1
    stage = next(
        (
            item
            for item in state.get("stages", [])
            if item.get("stage_name") == args.caller_stage
        ),
        None,
    )
    if stage is None or stage.get("status") != "in_progress":
        print("BLOCKED: authorized outcome consumer stage is not in progress")
        return 1
    return cmd_read_outcomes(sys.modules[__name__], args)


# --------------------------------------------------- human-review ledger --

def human_review_ledger_path(case_id: str) -> Path:
    return case_dir(case_id) / "_human_review_ledger.json"


def load_human_review_ledger(case_id: str) -> dict | None:
    """The DAO-only human-review ledger, or None if none exists yet. None is a
    real state -- a case with no human reviews recorded -- and every verifier
    treats a missing/None ledger as 'no such review', never as a pass."""
    return load_json(human_review_ledger_path(case_id))


def record_source_digest(case_id, doc_id, held_by, run_id, expect=None):
    """Record a registered source's digest and return a structured outcome.

    This is an in-process DAO capability for transaction drivers.  The CLI
    wrapper below remains the public interface and owns its existing output.
    """
    def outcome(success, *messages):
        return {"success": success, "messages": list(messages),
                "document_id": doc_id}

    actual = registered_source_pdf_sha256(case_id, doc_id)
    if actual is None:
        return outcome(False, f"BLOCKED: no readable registered raw source for {doc_id} "
                       "-- source_pdf_sha256 is derived from the file itself and cannot "
                       "be recorded without it")
    if expect and expect != actual:
        return outcome(False, f"REFUSED: submitted digest {expect!r} does not match the "
                       f"registered raw source ({actual!r}) -- the file this document "
                       "was extracted from is not the file the case registered")

    target = case_dir(case_id) / "document_manifest.json"
    existing_lock = acquire_lock_blocking(
        target, held_by, run_id, f"record source digest for {doc_id}")
    if existing_lock is not None:
        return outcome(False, f"LOCKED: held_by={existing_lock['held_by']} "
                       f"run_id={existing_lock['run_id']}")
    try:
        manifest = json.loads(target.read_text(encoding="utf-8"))
        entry = next((d for d in manifest["documents"]
                      if d["document_id"] == doc_id), None)
        if entry is None:
            return outcome(False, f"FAIL: {doc_id} is not in document_manifest.json")
        recorded = entry.get("source_pdf_sha256")
        if recorded is not None and recorded != actual:
            return outcome(False, f"REFUSED: {doc_id} already records "
                           f"{recorded!r}; the registered file now hashes to {actual!r} "
                           "-- an immutable raw source changed, which is a case-integrity "
                           "problem, not a field to overwrite")
        entry["source_pdf_sha256"] = actual
        manifest["updated_at"] = now_iso()
        errors = _schema_check(manifest, "document_manifest.schema.json")
        if errors:
            return outcome(False, "FAIL: manifest would be schema-invalid -- not written:",
                           *(f"  - {error}" for error in errors))
        atomic_write_json(target, manifest)
        return outcome(True, f"PASS: {doc_id} source_pdf_sha256 = {actual}")
    finally:
        release_lock(target)


def cmd_record_source_digest(args):
    """Record source_pdf_sha256 by hashing the registered raw file itself.

    The only write path for this field. It is the first input to every
    canonical UID, so accepting a caller-supplied value would let a UID be
    minted against a file the case never registered -- and a submitted digest
    that disagrees with the file on disk is refused rather than recorded,
    since the disagreement is the finding.
    """
    result = record_source_digest(args.case_id, args.doc_id, args.held_by,
                                  args.run_id, args.expect)
    for message in result["messages"]:
        print(message)
    return 0 if result["success"] else 1


def enable_canonical_uids(case_id, doc_id, held_by, run_id, *,
                          run_state_lock_already_held=False):
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
    def outcome(success, *messages, invalidated=None):
        return {"success": success, "messages": list(messages),
                "document_id": doc_id, "invalidated_stages": invalidated or []}

    manifest = read_contract_data(case_id, "document_manifest.json")
    if manifest is None:
        return outcome(False, "BLOCKED: document_manifest.json does not exist")
    entry = next((d for d in manifest.get("documents", [])
                  if d.get("document_id") == doc_id), None)
    if entry is None:
        return outcome(False, f"BLOCKED: {doc_id} is not a registered document")

    blockers = []
    actual = registered_source_pdf_sha256(case_id, doc_id)
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

    revision_entry = revision_entry_for(case_id, doc_id)
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
        return outcome(False, f"BLOCKED: cannot enable canonical_v1 for {doc_id}:",
                       *(f"  - {blocker}" for blocker in blockers))

    # Every activation transaction takes run-state before revision-index.  The
    # preflight already owns run-state for its whole attempt-open boundary;
    # standalone CLI calls acquire it here.  A reverse order would let a CLI
    # activation hold revision-index while waiting for preflight's run-state
    # lock, while preflight waited for that revision-index lock.
    run_state_target = run_state_path(case_id)
    acquired_run_state_lock = False
    if not run_state_lock_already_held:
        existing_run_state_lock = acquire_lock_blocking(
            run_state_target, held_by, run_id,
            f"canonical UID activation transaction ({doc_id})")
        if existing_run_state_lock is not None:
            return outcome(False,
                f"FAIL: {doc_id} was NOT switched to canonical_v1 -- the "
                f"policy stage and its downstream could not be invalidated: "
                "could not acquire the run-state lock for canonical UID activation -- "
                f"held_by={existing_run_state_lock['held_by']} "
                f"run_id={existing_run_state_lock['run_id']}",
                "  activating canonical UIDs while a stage still claims "
                "'passed' against unverified artifacts would be exactly "
                "the fail-open state this gate exists to prevent; "
                "resolve the run-state lock and retry")
        acquired_run_state_lock = True

    target = revision_index_path(case_id)
    existing_lock = acquire_lock_blocking(
        target, held_by, run_id, f"enable canonical UIDs ({doc_id})")
    if existing_lock is not None:
        if acquired_run_state_lock:
            release_lock(run_state_target)
        return outcome(False, f"LOCKED: held_by={existing_lock['held_by']} "
                       f"run_id={existing_lock['run_id']}")
    try:
        index = load_revision_index(case_id)
        doc_entry = next((d for d in index.get("documents", [])
                          if d.get("document_id") == doc_id), None)
        if doc_entry is None:
            # Re-read under the lock: the precondition pass above ran before
            # acquiring it, so the entry could have gone between the two.
            return outcome(False, f"REFUSED: {doc_id} has no revision entry -- there is no "
                           "recorded state to transition, and activation may not "
                           "create one")
        # Activation ALWAYS mutates a record that already exists, so a missing
        # uid_scheme here is damage rather than a fresh document. Passing that
        # distinction explicitly is what stops a corrupt entry being repaired
        # into canonical_v1 by the very command that grants verification.
        previous_scheme = doc_entry.get("uid_scheme")
        errors = source_provenance.scheme_transition_errors(
            previous_scheme, "canonical_v1", entry_exists=True)
        if errors:
            return outcome(False, *(f"REFUSED: {error}" for error in errors))
        doc_entry["uid_scheme"] = "canonical_v1"
        schema_errors = _schema_check(index, "revision_index.schema.json")
        if schema_errors:
            return outcome(False, "FAIL: revision index would be schema-invalid -- not written:",
                           *(f"  - {error}" for error in schema_errors))

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
                    case_id,
                    f"canonical_v1 UID verification enabled for {doc_id}: "
                    "policy artifacts written under the legacy scheme were "
                    "never UID-verified and must be rewritten canonically",
                    held_by, run_id,
                    lock_already_held=True)
            except CascadeFailed as exc:
                return outcome(False,
                    f"FAIL: {doc_id} was NOT switched to canonical_v1 -- the "
                    f"policy stage and its downstream could not be invalidated: {exc}",
                    "  activating canonical UIDs while a stage still claims "
                    "'passed' against unverified artifacts would be exactly "
                    "the fail-open state this gate exists to prevent; "
                    "resolve the run-state lock and retry")
        else:
            invalidated = []

        atomic_write_json(target, index)
        messages = [f"PASS: {doc_id} is now canonical_v1 -- every policy UID on this "
                    "document is recomputed and must match. This cannot be undone."]
        if invalidated:
            messages.extend([
                "INVALIDATED (legacy policy artifacts are no longer "
                f"citable): {', '.join(sorted(invalidated))}",
                "  rewrite this document's policy contracts with canonically "
                "derived UIDs, then re-finalize policy_clause_processing."])
        return outcome(True, *messages, invalidated=invalidated)
    finally:
        release_lock(target)
        if acquired_run_state_lock:
            release_lock(run_state_target)


def cmd_enable_canonical_uids(args):
    """CLI wrapper for canonical UID activation.

    The CLI never exposes the in-process lock-ownership capability; it always
    takes and releases the run-state lock through the normal cascade path.
    """
    result = enable_canonical_uids(args.case_id, args.doc_id, args.held_by,
                                   args.run_id)
    for message in result["messages"]:
        print(message)
    return 0 if result["success"] else 1


# ------------------------------------------- segment derivation receipts --
# P0-6. Everything below exists because segment lineage used to be checked only
# against values the extracting agent itself produced. See
# segment_derivation.py's module docstring for the exact bypass.

def segment_derivation_index_path(case_id: str) -> Path:
    return case_dir(case_id) / "_segment_derivation_index.json"


def load_segment_derivation_index(case_id: str) -> dict:
    data = load_json(segment_derivation_index_path(case_id))
    if data is None:
        return {"case_id": case_id, "segments": []}
    return data


def segment_derivation_receipt_for(case_id: str, doc_id: str):
    for receipt in load_segment_derivation_index(case_id).get("segments", []):
        if receipt.get("document_id") == doc_id:
            return receipt
    return None


def _raw_source_path(case_id: str, doc_id: str):
    """The on-disk raw file for a registered document, or None.

    Located via the manifest's file_path but resolved through the same
    containment guard every other DAO path uses, so a manifest naming a path
    outside the data root cannot make the DAO read it.
    """
    manifest = read_contract_data(case_id, "document_manifest.json")
    entry = next(
        (d for d in (manifest or {}).get("documents", [])
         if d.get("document_id") == doc_id), None)
    if entry is None or not entry.get("file_path"):
        return None
    path = _require_within(DATA.parent, entry["file_path"])
    return path if path.exists() else None


def _extract_parent_pages(pdf_path: Path, physical_pages):
    """Structured parent pages read directly by the DAO.

    The DAO does this itself rather than accepting an extraction someone else
    performed: the whole point of the receipt is that the digests and the
    printed page numbers come from bytes this process read out of the
    registered file.  Text blocks retain coordinates so a body reference to
    "page 12" cannot impersonate a printed header/footer page number.
    Returns `(pages, extractor, error)`.
    """
    try:
        import fitz  # pymupdf
    except ImportError:
        return None, None, (
            "pymupdf is not installed -- the DAO cannot open the parent PDF "
            "itself, and a page mapping it did not derive is not a receipt it "
            "may issue")
    extractor = {
        "tool": "dao.register-segment-derivation",
        "library": "pymupdf",
        "library_version": getattr(fitz, "VersionBind", None) or None,
        "settings": (
            "page.get_text() + page.get_text('blocks'); "
            "header_footer_blocks_v1"),
    }
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:  # noqa: BLE001
        return None, extractor, f"the parent PDF could not be opened: {exc}"
    try:
        pages = {}
        for physical in sorted(set(physical_pages)):
            if physical < 1 or physical > doc.page_count:
                continue  # reported per-page by verify_candidate_mapping
            text = doc[physical - 1].get_text()
            if not text.strip():
                # An empty text layer cannot confirm anything. Left out so the
                # page is reported as unconfirmable rather than mapped on a
                # digest of nothing.
                continue
            page = doc[physical - 1]
            blocks = []
            for block in page.get_text("blocks"):
                if len(block) < 5:
                    continue
                blocks.append({
                    "bbox": [float(block[0]), float(block[1]),
                             float(block[2]), float(block[3])],
                    "text": str(block[4]),
                })
            pages[physical] = {
                "text": text,
                "width": float(page.rect.width),
                "height": float(page.rect.height),
                "blocks": blocks,
            }
        return pages, extractor, None
    finally:
        doc.close()


def _parent_page_count(pdf_path: Path):
    try:
        import fitz  # pymupdf
        doc = fitz.open(pdf_path)
        try:
            return doc.page_count
        finally:
            doc.close()
    except Exception:  # noqa: BLE001
        return None


def _current_parent_digest_reader(case_id):
    """Closure returning a document's raw digest, recomputed from the file.

    Deliberately not the manifest's recorded `source_pdf_sha256`: comparing a
    recorded field to a recorded field proves only that two fields agree, which
    is the class of check P0-6 exists to replace.
    """
    def _read(doc_id):
        return registered_source_pdf_sha256(case_id, doc_id) if doc_id else None
    return _read


def _current_segment_revision_reader(case_id):
    def _read(doc_id):
        entry = revision_entry_for(case_id, doc_id)
        return (entry or {}).get("current_revision_sha256")
    return _read


def _current_segment_text_reader(case_id):
    def _read(doc_id):
        return _registered_revision_text(case_id, doc_id)
    return _read


def _segment_derivation_blockers(case_id: str, manifest: dict) -> list[str]:
    """P0-6 finalization gate: every segment's page map must still be the one
    the DAO derived from the parent PDF that is on disk right now."""
    return segment_derivation.segment_finalization_blockers(
        manifest,
        lambda doc_id: segment_derivation_receipt_for(case_id, doc_id),
        _current_parent_digest_reader(case_id),
        _current_segment_revision_reader(case_id),
        _current_segment_text_reader(case_id),
    )


def cmd_register_segment_derivation(args):
    """Issue a segment_page_map_v2 receipt and project it into the manifest.

    The ONLY writer of a segment's `page_map` (the field is sealed against
    write-contract and patch-manifest-document in
    source_provenance.PROTECTED_MANIFEST_FIELDS). Nothing the caller submits is
    trusted: the parent digest, the page count, every page-text digest and
    every printed-page quote are computed here, by this process, from the
    registered raw file. A `--page-map-file` may be supplied, but only so a
    DISAGREEMENT is reported as the finding it is -- its values are never
    stored.

    Ordering follows dao_transaction's rule -- everything that can fail runs
    before anything irreversible, and the pointer flips last:

      1. resolve + verify the parent (registered, physical, digest, page count)
      2. re-derive the mapping from the parent's own pages
      3. cross-check any submitted mapping; disagreement refuses
      4. build the receipt; an identical one already on file is a NO-OP that
         does not cascade
      5. validate the prospective index AND the prospective manifest
      6. journal, then invalidate downstream stages
      7. write the receipt, then the manifest projection
      8. clear the journal

    A crash before 7 leaves the old mapping with stages conservatively
    invalidated (a rerun). The unreachable state is a new mapping with a stale
    `passed` downstream.
    """
    case_id, doc_id = args.case_id, args.doc_id
    case_directory = case_dir(case_id)

    blockers = dao_transaction.pending_journal_errors(case_directory)
    if blockers:
        for blocker in blockers:
            print(f"BLOCKED: {blocker}")
        return 1

    manifest = read_contract_data(case_id, "document_manifest.json")
    if manifest is None:
        print("BLOCKED: document_manifest.json does not exist -- a segment's "
              "parent cannot be resolved, so no source may be opened")
        return 1
    by_id = {d.get("document_id"): d for d in manifest.get("documents", [])}

    entry = by_id.get(doc_id)
    if entry is None:
        print(f"BLOCKED: {doc_id} is not registered in document_manifest.json")
        return 1
    if entry.get("document_role") != "segment":
        print(f"BLOCKED: {doc_id} is not a segment (document_role="
              f"{entry.get('document_role')!r}) -- only a segment has a page "
              "map to derive")
        return 1

    parent_id = args.parent_document_id or entry.get("source_document_id")
    if not parent_id:
        print(f"BLOCKED: {doc_id} names no source_document_id -- there is no "
              "registered parent to derive a mapping from")
        return 1
    if args.parent_document_id and entry.get("source_document_id") and \
            args.parent_document_id != entry.get("source_document_id"):
        print(f"REFUSED: --parent-document-id {args.parent_document_id!r} "
              f"disagrees with the manifest's source_document_id "
              f"{entry.get('source_document_id')!r} for {doc_id} -- the parent "
              "is read from the registered lineage, not chosen per call")
        return 1
    parent = by_id.get(parent_id)
    if parent is None:
        print(f"BLOCKED: parent {parent_id} is not registered in "
              "document_manifest.json")
        return 1
    if parent.get("document_role") == "segment":
        print(f"BLOCKED: parent {parent_id} is itself a segment -- a segment "
              "must be carved from a physical document")
        return 1

    method = entry.get("derivation_method")
    if method not in segment_derivation.VERIFIABLE_METHODS:
        print(f"BLOCKED: derivation_method {method!r} cannot be verified "
              "deterministically against the parent PDF. UID verification may "
              "not re-run OCR, so no page-map receipt can be issued for an "
              "ocr_segment; it cannot finalize. This is a stated P0-6 "
              "limitation, not a state to work around.")
        return 1

    # --- 1. the parent, verified by reading it -----------------------------
    pdf_path = _raw_source_path(case_id, parent_id)
    if pdf_path is None:
        print(f"BLOCKED: parent {parent_id} has no readable registered raw "
              "file -- there is no immutable source to derive from")
        return 1
    actual_parent_digest = registered_source_pdf_sha256(case_id, parent_id)
    if actual_parent_digest is None:
        print(f"BLOCKED: parent {parent_id}'s raw source could not be hashed")
        return 1
    recorded_parent_digest = parent.get("source_pdf_sha256")
    if recorded_parent_digest and recorded_parent_digest != actual_parent_digest:
        print(f"REFUSED: parent {parent_id} records source_pdf_sha256 "
              f"{recorded_parent_digest!r} but the registered file now hashes "
              f"to {actual_parent_digest!r} -- the raw source changed; a "
              "mapping derived from it cannot be trusted")
        return 1
    if args.expect_parent_sha256 and args.expect_parent_sha256 != actual_parent_digest:
        print(f"REFUSED: submitted parent digest "
              f"{args.expect_parent_sha256!r} does not match the registered "
              f"raw source ({actual_parent_digest!r}) -- the page hashes and "
              "printed-page quotes were taken from a different file")
        return 1

    real_page_count = _parent_page_count(pdf_path)
    if real_page_count is None:
        print(f"BLOCKED: parent {parent_id}'s PDF page count could not be read")
        return 1
    recorded_total = parent.get("source_total_pages")
    if isinstance(recorded_total, int) and recorded_total != real_page_count:
        print(f"REFUSED: parent {parent_id} has {real_page_count} physical "
              f"pages but the manifest records source_total_pages="
              f"{recorded_total}")
        return 1

    # --- 2. re-derive the mapping from the parent's own pages --------------
    logical_pages = _parse_logical_pages(args.pages)
    if not logical_pages:
        print("BLOCKED: --pages resolved to no logical pages")
        return 1
    physical_candidates = [lp + args.page_offset for lp in logical_pages]
    parent_pages, extractor, read_error = _extract_parent_pages(
        pdf_path, physical_candidates)
    if read_error:
        print(f"BLOCKED: {read_error}")
        return 1

    verified_pages, mapping_errors = segment_derivation.verify_candidate_mapping(
        logical_pages, args.page_offset,
        parent_pages.get, _find_printed_logical_page,
        parent_total_pages=real_page_count)
    if mapping_errors:
        print(f"BLOCKED: the page mapping for {doc_id} could not be verified "
              f"against parent {parent_id}; NOTHING was written:")
        for error in mapping_errors:
            print(f"  - {error}")
        print("  resolve these for review. An offset is a search hint, never a "
              "proof: a page whose printed number cannot be read is not mapped "
              "on the grounds that the offset worked on other pages.")
        return 1

    # --- 3. a submitted mapping is a claim to CHECK, never an input --------
    if args.page_map_file:
        submitted = json.loads(
            Path(args.page_map_file).read_text(encoding="utf-8"))
        if isinstance(submitted, dict):
            submitted = submitted.get("page_map") or []
        conflicts = segment_derivation.submitted_value_conflicts(
            verified_pages, submitted)
        if conflicts:
            print(f"REFUSED: the submitted page map for {doc_id} disagrees "
                  "with what the parent PDF's own pages show; NOTHING was "
                  "written:")
            for conflict in conflicts:
                print(f"  - {conflict}")
            return 1

    # --- 4. build the receipt; an identical one is a no-op -----------------
    segment_revision = revision_entry_for(case_id, doc_id)
    current_revision = (segment_revision or {}).get("current_revision_sha256")
    if not current_revision:
        print(f"BLOCKED: {doc_id} has no registered source-text revision -- "
              "run `dao.py write-redacted-text` first. A page map must be "
              "bound to the exact segment bytes it produced, or a later "
              "rewrite would silently inherit this mapping's verification.")
        return 1

    segment_revision_text = _registered_revision_text(case_id, doc_id)
    verified_pages, binding_errors = segment_derivation.bind_segment_revision(
        verified_pages, segment_revision_text or "",
        lambda physical: (
            (parent_pages.get(physical) or {}).get("text")))
    if binding_errors:
        print(f"BLOCKED: {doc_id}'s registered segment text was not derived "
              "exactly from the verified parent pages; NOTHING was written:")
        for error in binding_errors:
            print(f"  - {error}")
        print("  embedded-text segment receipts require exact page bytes "
              "after only CRLF/LF and Unicode NFC normalization; lexical or "
              "semantic similarity is not provenance")
        return 1

    receipt = segment_derivation.build_receipt(
        segment_document_id=doc_id,
        parent_document_id=parent_id,
        parent_source_pdf_sha256=actual_parent_digest,
        parent_source_total_pages=real_page_count,
        segment_source_text_revision_sha256=current_revision,
        derivation_method=method,
        extractor=extractor,
        pages=verified_pages,
        page_offset_candidate=args.page_offset,
        issued_at=now_iso(),
        issued_by=args.held_by,
        run_id=args.run_id,
    )
    projection = segment_derivation.manifest_page_map_from_receipt(receipt)

    existing = segment_derivation_receipt_for(case_id, doc_id)
    if (existing is not None
            and segment_derivation.receipt_fingerprint(existing)
            == segment_derivation.receipt_fingerprint(receipt)
            and (entry.get("page_map") or []) == projection):
        # Same parent bytes, same mapping, same segment revision, and the
        # manifest already projects it. Nothing changed, so nothing downstream
        # may be invalidated -- re-running a registration must not cost a rerun.
        print(f"PASS: {doc_id} derivation receipt is unchanged (no-op) -- "
              f"{len(projection)} pages, parent {parent_id} "
              f"{actual_parent_digest[:12]}")
        return 0

    # --- 5-8. the transaction ---------------------------------------------
    return _commit_segment_derivation(
        case_id, doc_id, parent_id, receipt, projection,
        args.held_by, args.run_id)


def _commit_segment_derivation(case_id, doc_id, parent_id, receipt, projection,
                                held_by, run_id):
    """Write the receipt, the manifest projection and the downstream
    invalidation as one fail-closed transaction.

    Locks are taken in dao_transaction.LOCK_ORDER (run_state ->
    document_manifest -> revision_index-slot for the receipt index), asserted
    rather than assumed. Both prospective documents are schema-validated BEFORE
    either is written, so a schema failure cannot leave the receipt written and
    the manifest not, or the reverse.

    This deliberately does NOT reuse patch_manifest_document: that path writes
    the manifest first and cascades afterwards, which for a page-map change is
    precisely the ordering that can leave a new mapping standing next to a
    downstream stage still marked passed.
    """
    lock_kinds = ("run_state", "document_manifest", "revision_index")
    order_errors = dao_transaction.check_lock_order(lock_kinds)
    if order_errors:
        for error in order_errors:
            print(f"FAIL: {error}")
        return 1

    case_directory = case_dir(case_id)
    state_target = run_state_path(case_id)
    manifest_target = case_directory / "document_manifest.json"
    index_target = segment_derivation_index_path(case_id)
    index_preimage: bytes | None = None
    manifest_preimage: bytes | None = None

    held: list[Path] = []
    try:
        for target, purpose in (
            (state_target, f"register segment derivation for {doc_id}"),
            (manifest_target, f"project page_map for {doc_id}"),
            (index_target, f"issue derivation receipt for {doc_id}"),
        ):
            existing_lock = acquire_lock_blocking(
                target, held_by, run_id or "unknown", purpose)
            if existing_lock is not None:
                print(f"LOCKED: held_by={existing_lock['held_by']} "
                      f"run_id={existing_lock['run_id']} on {target.name} -- "
                      "the receipt, the manifest projection and the downstream "
                      "invalidation must land together, so a contended lock "
                      "aborts the whole registration rather than writing part "
                      "of it")
                return 1
            held.append(target)

        # Re-read both files under the locks: everything above ran before they
        # were taken, so this is the first guaranteed-fresh read.
        manifest = load_json(manifest_target)
        if manifest is None:
            print("FAIL: document_manifest.json disappeared before the write")
            return 1
        entry = next((d for d in manifest.get("documents", [])
                      if d.get("document_id") == doc_id), None)
        if entry is None:
            print(f"FAIL: {doc_id} is no longer in document_manifest.json")
            return 1
        if entry.get("document_role") != "segment":
            print(f"FAIL: {doc_id} is no longer a segment -- derivation "
                  "candidate discarded before persistence")
            return 1
        if entry.get("source_document_id") != parent_id:
            print(f"FAIL: {doc_id} was re-parented to "
                  f"{entry.get('source_document_id')!r} while its receipt was "
                  f"being derived for {parent_id!r}; rerun from fresh state")
            return 1
        if entry.get("derivation_method") != receipt.get("derivation_method"):
            print(f"FAIL: {doc_id} derivation_method changed while its receipt "
                  "was being derived; rerun from fresh state")
            return 1
        fresh_revision = revision_entry_for(case_id, doc_id)
        fresh_revision_sha = (
            fresh_revision or {}).get("current_revision_sha256")
        if fresh_revision_sha != receipt.get(
                "segment_source_text_revision_sha256"):
            print(f"FAIL: {doc_id} source-text revision changed while its "
                  "receipt was being derived; rerun from fresh state")
            return 1
        fresh_parent_digest = registered_source_pdf_sha256(case_id, parent_id)
        if fresh_parent_digest != receipt.get("parent_source_pdf_sha256"):
            print(f"FAIL: parent {parent_id} source bytes changed while the "
                  "receipt was being derived; rerun from fresh state")
            return 1
        fresh_parent_path = _raw_source_path(case_id, parent_id)
        fresh_parent_total = (
            _parent_page_count(fresh_parent_path)
            if fresh_parent_path is not None else None)
        if fresh_parent_total != receipt.get("parent_source_total_pages"):
            print(f"FAIL: parent {parent_id} page count changed while the "
                  "receipt was being derived; rerun from fresh state")
            return 1

        index = load_segment_derivation_index(case_id)
        segments = [s for s in index.get("segments", [])
                    if s.get("document_id") != doc_id]
        segments.append(receipt)
        segments.sort(key=lambda s: s.get("document_id") or "")
        index["segments"] = segments
        index["case_id"] = case_id

        # Step 5: validate BOTH prospective documents before either lands.
        index_errors = _schema_check(
            index, segment_derivation.INDEX_SCHEMA)
        if index_errors:
            print("FAIL: the derivation receipt would be schema-invalid -- "
                  "nothing written:")
            for error in index_errors:
                print(f"  - {error}")
            return 1

        entry["page_map"] = projection
        manifest["updated_at"] = now_iso()
        manifest_errors = _schema_check(
            manifest, "document_manifest.schema.json")
        if manifest_errors:
            print("FAIL: the projected manifest would be schema-invalid -- "
                  "nothing written:")
            for error in manifest_errors:
                print(f"  - {error}")
            return 1
        lineage_errors = _validate_manifest_lineage(case_id, manifest)
        if lineage_errors:
            print("FAIL: the projected manifest fails segment-lineage "
                  "validation -- nothing written:")
            for error in lineage_errors:
                print(f"  - {error}")
            return 1

        # Preserve exact preimages for caught write failures. A hard crash can
        # still happen between the two atomic replaces; the durable journal is
        # what makes that intermediate state visible and blocking.
        index_preimage = (
            index_target.read_bytes() if index_target.exists() else None)
        manifest_preimage = manifest_target.read_bytes()

        # Step 6: journal, then invalidate. Nothing irreversible yet.
        dao_transaction.write_journal(case_directory, {
            "operation": "register_segment_derivation",
            "case_id": case_id,
            "document_id": doc_id,
            "parent_source_document_id": parent_id,
            "parent_source_pdf_sha256": receipt["parent_source_pdf_sha256"],
            "status": "invalidating",
            "started_at": now_iso(),
        })
        try:
            _invalidate_dependents(
                case_id, "document_processing",
                f"upstream document_processing changed: {doc_id} page map "
                f"re-derived from parent {parent_id}",
                held_by, run_id, strict=True, lock_already_held=True)
        except CascadeFailed as exc:
            dao_transaction.clear_journal(case_directory)
            print(f"FAIL: {doc_id} page map NOT registered -- {exc}")
            print("  the existing receipt and manifest page_map are unchanged")
            return 1

        # Step 7: the receipt first, then the projection. A caught second-write
        # failure rolls the receipt back to its exact preimage. A hard crash
        # between the replaces leaves the journal pending; every subsequent
        # registration/finalization is blocked rather than treating the
        # partial state as committed.
        try:
            atomic_write_json(index_target, index)
            atomic_write_json(manifest_target, manifest)
        except Exception as exc:  # noqa: BLE001
            rollback_error = None
            try:
                # Restore both participants regardless of which call raised.
                # A wrapper may raise *after* os.replace completed, so a local
                # "write returned" flag cannot establish whether the file
                # changed.
                _restore_file_preimage(index_target, index_preimage)
                _restore_file_preimage(
                    manifest_target, manifest_preimage)
                dao_transaction.clear_journal(case_directory)
            except Exception as restore_exc:  # noqa: BLE001
                rollback_error = restore_exc
            print(f"FAIL: segment derivation persistence failed: {exc}")
            if rollback_error is None:
                print("  receipt and manifest were restored to their exact "
                      "pre-transaction bytes; downstream remains "
                      "conservatively invalidated")
            else:
                print("  ROLLBACK INCOMPLETE: the transaction journal remains "
                      f"pending and blocks further work: {rollback_error}")
            return 1

        try:
            dao_transaction.clear_journal(case_directory)
        except Exception as exc:  # noqa: BLE001
            # Both new documents are consistent and downstream is invalidated,
            # but the uncleared journal must force explicit operator recovery.
            print("FAIL: derivation files were committed and downstream was "
                  "invalidated, but the transaction journal could not be "
                  f"cleared: {exc}")
            print("  the pending journal intentionally blocks further work; "
                  "do not delete it without inspecting both files")
            return 1

        print(f"PASS: issued {segment_derivation.RECEIPT_SCHEME} receipt for "
              f"{doc_id} -- {len(projection)} pages verified against parent "
              f"{parent_id} ({receipt['parent_source_pdf_sha256'][:12]})")
        for page in receipt["pages"]:
            evidence = page["logical_page_evidence"]
            quote = (evidence.get("quote")
                     if isinstance(evidence, dict) else evidence)
            print(f"  logical {page['logical_page']} -> physical "
                  f"{page['source_physical_page']} "
                  f"(printed {quote!r})")
        print(f"  bound to segment revision "
              f"{receipt['segment_source_text_revision_sha256'][:12]}")
        return 0
    finally:
        for target in reversed(held):
            release_lock(target)


def cmd_record_unverifiable_segment_derivation(args):
    """P0-6 human-authorized override: an ocr_segment's page map cannot be
    verified against the parent PDF (see segment_derivation.py's module
    docstring -- confirming it would require re-running OCR, which UID
    verification is forbidden to do). This does not weaken
    `register-segment-derivation`'s real verification; it records a
    DIFFERENT, honestly-labeled state (segment_page_map_unverified_v1, never
    segment_page_map_v2) that finalization accepts only because a human
    explicitly authorized proceeding without per-page confirmation. The risk
    this accepts: the asserted physical-page offset may be wrong for one or
    more logical pages, silently mis-anchoring every evidence quote, policy
    clause offset and canonical UID keyed to this segment.

    Sets `document_role: "segment"` on first use if not already set -- a
    caller who has never run Stage-1 lineage assignment can invoke this
    directly, since the override IS the human asserting this document is a
    segment of the named parent. It never sets these fields on a document
    that already declares document_role=physical (refused instead).
    """
    case_id, doc_id = args.case_id, args.doc_id
    case_directory = case_dir(case_id)

    blockers = dao_transaction.pending_journal_errors(case_directory)
    if blockers:
        for blocker in blockers:
            print(f"BLOCKED: {blocker}")
        return 1

    manifest = read_contract_data(case_id, "document_manifest.json")
    if manifest is None:
        print("BLOCKED: document_manifest.json does not exist -- a segment's "
              "parent cannot be resolved, so no override can be recorded")
        return 1
    by_id = {d.get("document_id"): d for d in manifest.get("documents", [])}

    entry = by_id.get(doc_id)
    if entry is None:
        print(f"BLOCKED: {doc_id} is not registered in document_manifest.json")
        return 1
    if entry.get("document_role") == "physical":
        print(f"BLOCKED: {doc_id} is declared document_role=physical -- an "
              "override is for a segment of a physical parent, not a "
              "physical document itself")
        return 1

    parent_id = args.parent_document_id or entry.get("source_document_id")
    if not parent_id:
        print(f"BLOCKED: {doc_id} names no parent (no --parent-document-id "
              "and no registered source_document_id) -- there is no parent "
              "to bind this override to")
        return 1
    if entry.get("source_document_id") and args.parent_document_id and \
            args.parent_document_id != entry.get("source_document_id"):
        print(f"REFUSED: --parent-document-id {args.parent_document_id!r} "
              f"disagrees with the manifest's source_document_id "
              f"{entry.get('source_document_id')!r} for {doc_id}")
        return 1
    parent = by_id.get(parent_id)
    if parent is None:
        print(f"BLOCKED: parent {parent_id} is not registered in "
              "document_manifest.json")
        return 1
    if parent.get("document_role") == "segment":
        print(f"BLOCKED: parent {parent_id} is itself a segment -- a segment "
              "must be carved from a physical document")
        return 1

    actual_parent_digest = registered_source_pdf_sha256(case_id, parent_id)
    if actual_parent_digest is None:
        print(f"BLOCKED: parent {parent_id}'s raw source could not be hashed")
        return 1
    recorded_parent_digest = parent.get("source_pdf_sha256")
    if recorded_parent_digest and recorded_parent_digest != actual_parent_digest:
        print(f"REFUSED: parent {parent_id} records source_pdf_sha256 "
              f"{recorded_parent_digest!r} but the registered file now hashes "
              f"to {actual_parent_digest!r} -- the raw source changed")
        return 1

    logical_pages = _parse_logical_pages(args.pages)
    if not logical_pages:
        print("BLOCKED: --pages resolved to no logical pages")
        return 1

    segment_revision = revision_entry_for(case_id, doc_id)
    current_revision = (segment_revision or {}).get("current_revision_sha256")
    if not current_revision:
        print(f"BLOCKED: {doc_id} has no registered source-text revision -- "
              "run `dao.py write-redacted-text` first. An override must "
              "still be bound to exact segment bytes, or a later rewrite "
              "would silently inherit its authorization")
        return 1

    receipt = segment_derivation.build_unverified_override_receipt(
        segment_document_id=doc_id,
        parent_document_id=parent_id,
        parent_source_pdf_sha256=actual_parent_digest,
        segment_source_text_revision_sha256=current_revision,
        logical_pages=logical_pages,
        page_offset=args.page_offset,
        issued_at=now_iso(),
        issued_by=args.held_by,
        run_id=args.run_id,
        authorized_by=args.authorized_by,
        reason=args.reason,
    )
    projection = segment_derivation.manifest_page_map_from_unverified_override(receipt)

    existing = segment_derivation_receipt_for(case_id, doc_id)
    if (existing is not None
            and existing.get("scheme") == segment_derivation.UNVERIFIED_OVERRIDE_SCHEME
            and segment_derivation.unverified_override_fingerprint(existing)
            == segment_derivation.unverified_override_fingerprint(receipt)
            and (entry.get("page_map") or []) == projection):
        print(f"PASS: {doc_id} unverified-OCR override is unchanged (no-op) "
              f"-- {len(projection)} pages, parent {parent_id} "
              f"{actual_parent_digest[:12]}")
        return 0

    return _commit_unverifiable_segment_derivation(
        case_id, doc_id, parent_id, receipt, projection,
        args.held_by, args.run_id)


def _commit_unverifiable_segment_derivation(case_id, doc_id, parent_id,
                                            receipt, projection,
                                            held_by, run_id):
    """Write the override receipt, manifest projection (including
    document_role/source_document_id/derivation_method if not already set),
    and the downstream invalidation as one fail-closed transaction. Mirrors
    `_commit_segment_derivation`'s lock order and journal discipline exactly.
    """
    lock_kinds = ("run_state", "document_manifest", "revision_index")
    order_errors = dao_transaction.check_lock_order(lock_kinds)
    if order_errors:
        for error in order_errors:
            print(f"FAIL: {error}")
        return 1

    case_directory = case_dir(case_id)
    state_target = run_state_path(case_id)
    manifest_target = case_directory / "document_manifest.json"
    index_target = segment_derivation_index_path(case_id)
    index_preimage: bytes | None = None
    manifest_preimage: bytes | None = None

    held: list[Path] = []
    try:
        for target, purpose in (
            (state_target, f"record unverifiable segment derivation for {doc_id}"),
            (manifest_target, f"project unverified page_map for {doc_id}"),
            (index_target, f"issue unverified derivation override for {doc_id}"),
        ):
            existing_lock = acquire_lock_blocking(
                target, held_by, run_id or "unknown", purpose)
            if existing_lock is not None:
                print(f"LOCKED: held_by={existing_lock['held_by']} "
                      f"run_id={existing_lock['run_id']} on {target.name} -- "
                      "the receipt, the manifest projection and the downstream "
                      "invalidation must land together")
                return 1
            held.append(target)

        manifest = load_json(manifest_target)
        if manifest is None:
            print("FAIL: document_manifest.json disappeared before the write")
            return 1
        entry = next((d for d in manifest.get("documents", [])
                      if d.get("document_id") == doc_id), None)
        if entry is None:
            print(f"FAIL: {doc_id} is no longer in document_manifest.json")
            return 1
        if entry.get("document_role") == "physical":
            print(f"FAIL: {doc_id} became document_role=physical while this "
                  "override was being prepared; rerun from fresh state")
            return 1

        fresh_revision = revision_entry_for(case_id, doc_id)
        fresh_revision_sha = (fresh_revision or {}).get("current_revision_sha256")
        if fresh_revision_sha != receipt.get("segment_source_text_revision_sha256"):
            print(f"FAIL: {doc_id} source-text revision changed while this "
                  "override was being prepared; rerun from fresh state")
            return 1
        fresh_parent_digest = registered_source_pdf_sha256(case_id, parent_id)
        if fresh_parent_digest != receipt.get("parent_source_pdf_sha256"):
            print(f"FAIL: parent {parent_id} source bytes changed while this "
                  "override was being prepared; rerun from fresh state")
            return 1

        index = load_segment_derivation_index(case_id)
        segments = [s for s in index.get("segments", [])
                    if s.get("document_id") != doc_id]
        segments.append(receipt)
        segments.sort(key=lambda s: s.get("document_id") or "")
        index["segments"] = segments
        index["case_id"] = case_id

        index_errors = _schema_check(index, segment_derivation.INDEX_SCHEMA)
        if index_errors:
            print("FAIL: the override receipt would be schema-invalid -- "
                  "nothing written:")
            for error in index_errors:
                print(f"  - {error}")
            return 1

        entry["document_role"] = "segment"
        entry["source_document_id"] = parent_id
        entry["derivation_method"] = "ocr_segment"
        entry["page_map"] = projection
        manifest["updated_at"] = now_iso()
        manifest_errors = _schema_check(manifest, "document_manifest.schema.json")
        if manifest_errors:
            print("FAIL: the projected manifest would be schema-invalid -- "
                  "nothing written:")
            for error in manifest_errors:
                print(f"  - {error}")
            return 1
        lineage_errors = _validate_manifest_lineage(case_id, manifest)
        if lineage_errors:
            print("FAIL: the projected manifest fails segment-lineage "
                  "validation -- nothing written:")
            for error in lineage_errors:
                print(f"  - {error}")
            return 1

        index_preimage = (
            index_target.read_bytes() if index_target.exists() else None)
        manifest_preimage = manifest_target.read_bytes()

        dao_transaction.write_journal(case_directory, {
            "operation": "record_unverifiable_segment_derivation",
            "case_id": case_id,
            "document_id": doc_id,
            "parent_source_document_id": parent_id,
            "parent_source_pdf_sha256": receipt["parent_source_pdf_sha256"],
            "status": "invalidating",
            "started_at": now_iso(),
        })
        try:
            _invalidate_dependents(
                case_id, "document_processing",
                f"upstream document_processing changed: {doc_id} unverified "
                f"page map recorded against parent {parent_id}",
                held_by, run_id, strict=True, lock_already_held=True)
        except CascadeFailed as exc:
            dao_transaction.clear_journal(case_directory)
            print(f"FAIL: {doc_id} override NOT recorded -- {exc}")
            print("  the existing receipt and manifest page_map are unchanged")
            return 1

        try:
            atomic_write_json(index_target, index)
            atomic_write_json(manifest_target, manifest)
        except Exception as exc:  # noqa: BLE001
            rollback_error = None
            try:
                _restore_file_preimage(index_target, index_preimage)
                _restore_file_preimage(manifest_target, manifest_preimage)
                dao_transaction.clear_journal(case_directory)
            except Exception as restore_exc:  # noqa: BLE001
                rollback_error = restore_exc
            print(f"FAIL: unverifiable segment derivation persistence failed: {exc}")
            if rollback_error is None:
                print("  receipt and manifest were restored to their exact "
                      "pre-transaction bytes; downstream remains "
                      "conservatively invalidated")
            else:
                print("  ROLLBACK INCOMPLETE: the transaction journal remains "
                      f"pending and blocks further work: {rollback_error}")
            return 1

        try:
            dao_transaction.clear_journal(case_directory)
        except Exception as exc:  # noqa: BLE001
            print("FAIL: override files were committed and downstream was "
                  "invalidated, but the transaction journal could not be "
                  f"cleared: {exc}")
            print("  the pending journal intentionally blocks further work; "
                  "do not delete it without inspecting both files")
            return 1

        print(f"PASS: {doc_id} recorded as {segment_derivation.UNVERIFIED_OVERRIDE_SCHEME} "
              f"-- NO per-page mapping was verified; this is a human-authorized "
              f"reduced-assurance pass, authorized by {receipt['human_override']['authorized_by']!r}: "
              f"{receipt['human_override']['reason']!r}")
        for page in receipt["pages"]:
            print(f"  logical {page['logical_page']} -> physical "
                  f"{page['source_physical_page']} (UNVERIFIED)")
        return 0
    finally:
        for target in reversed(held):
            release_lock(target)


def _parse_logical_pages(spec: str) -> list[int]:
    """'120-189' or '57,58,118,119' or a mix -> sorted unique logical pages."""
    pages: set[int] = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        else:
            pages.add(int(part))
    return sorted(pages)


# Korean policy books print the page as 'N / TOTAL', '- N -', or 'page N'.
# Evidence is accepted only from positioned text blocks in a narrow header or
# footer band. Whole-page regex search is intentionally forbidden: a body
# sentence saying "see page 12" is not page identity.
_HEADER_MAX_RATIO = 0.07
_FOOTER_MIN_RATIO = 0.80
_PAGE_EVIDENCE_PROFILE = "header_footer_blocks_v1"


def _printed_page_patterns():
    return [
        re.compile(r"(?<!\d)(\d+)\s*/\s*\d+(?!\d)"),
        re.compile(r"-\s*(\d+)\s*-"),
        re.compile(
            r"(?:page|페이지|쪽)\s*(\d+)(?!\d)", re.IGNORECASE),
    ]


def _find_printed_logical_page(page_record: dict, logical_page: int):
    """Position-bound page-number evidence, or None.

    Conflicting header/footer numbers also return None.  A receipt must identify
    one page, not select whichever of several numbers agrees with the caller's
    candidate offset.
    """
    if not isinstance(page_record, dict):
        return None
    width = page_record.get("width")
    height = page_record.get("height")
    blocks = page_record.get("blocks") or []
    if not isinstance(width, (int, float)) or not isinstance(
            height, (int, float)) or width <= 0 or height <= 0:
        return None

    candidates = []
    for block in blocks:
        bbox = block.get("bbox") or []
        if len(bbox) != 4:
            continue
        y_center = (float(bbox[1]) + float(bbox[3])) / 2
        ratio = y_center / float(height)
        if ratio <= _HEADER_MAX_RATIO:
            region = "header"
        elif ratio >= _FOOTER_MIN_RATIO:
            region = "footer"
        else:
            continue
        block_text = str(block.get("text") or "")
        for pattern in _printed_page_patterns():
            for match in pattern.finditer(block_text):
                candidates.append({
                    "logical": int(match.group(1)),
                    "quote": match.group(0).strip(),
                    "bbox": [float(v) for v in bbox],
                    "region": region,
                    "page_width": float(width),
                    "page_height": float(height),
                    "evidence_profile": _PAGE_EVIDENCE_PROFILE,
                })

    numbers = {candidate["logical"] for candidate in candidates}
    if numbers != {logical_page}:
        return None
    matching = [candidate for candidate in candidates
                if candidate["logical"] == logical_page]
    if not matching:
        return None
    matching.sort(key=lambda item: (
        0 if item["region"] == "footer" else 1,
        item["bbox"][1], item["bbox"][0], item["quote"]))
    evidence = dict(matching[0])
    evidence.pop("logical")
    return evidence


def cmd_read_segment_derivation_index(args):
    print(json.dumps(load_segment_derivation_index(args.case_id),
                     ensure_ascii=False, indent=2))
    return 0


# ------------------------------------------- table region receipts (P0-8) --
# A reference table's `source_regions` were the extracting agent's own
# declaration. Everything downstream enforced completeness WITHIN them, so a
# region drawn narrowly hid every row outside it. See
# table_region_provenance.py's module docstring for the exact bypass.

def table_region_index_path(case_id: str) -> Path:
    return case_dir(case_id) / "_table_region_index.json"


def load_table_region_index(case_id: str) -> dict:
    data = load_json(table_region_index_path(case_id))
    if data is None:
        return {"case_id": case_id, "tables": []}
    return data


def table_region_receipts_for(case_id: str, doc_id: str) -> list:
    """Every receipt issued for one document, in issue order."""
    return [receipt for receipt in load_table_region_index(case_id).get(
        "tables", []) if receipt.get("document_id") == doc_id]


def table_candidate_inventory_for(case_id: str, doc_id: str):
    """The DAO's scan of every table candidate in one document, or None.

    Recorded in the same DAO-owned index as the receipts. Verifying only the
    receipts a contract cites answers "is this table complete?" but never
    "were there other tables?" -- so a document with two appendices could have
    one extracted and the other never mentioned, with every check passing.
    """
    for inventory in load_table_region_index(case_id).get("candidates", []):
        if inventory.get("document_id") == doc_id:
            return inventory
    return None


def _build_candidate_inventory(case_id, doc_id, candidates, possible_tables,
                               detector, sentinel, pdf_digest, revision_sha, *,
                               pages,
                               scan_errors=(),
                               scanned_logical_pages, pdf_owner=None,
                               segment_receipt_id=None):
    """One inventory entry per detected/possible table, checksummed by
    `scan_id`.

    `scanned_logical_pages` is recorded because the inventory's real claim is a
    NEGATIVE one -- "these are all the tables in this document" -- and that
    claim is only as wide as the pages the detector actually looked at. Without
    it, a scan of pages 1-2 read identically to a scan of a 4-page document
    that found nothing on 3-4, which is the "no scan means no tables" inference
    this whole gate exists to refuse.
    """
    entries = []
    for candidate in candidates:
        logical = candidate.get("logical_page")
        if logical is None:
            continue
        rows = candidate.get("rows") or []
        header = table_region_provenance._header_texts(candidate) or ()
        entries.append({
            "candidate_id": table_region_provenance.compute_candidate_id(
                document_id=doc_id,
                source_pdf_sha256=pdf_digest,
                source_text_revision_sha256=revision_sha,
                detector_profile=detector["profile"],
                config_fingerprint=detector["config_fingerprint"],
                pages_geometry=(
                    logical, candidate["physical_page"],
                    tuple(round(float(v), 2) for v in candidate["bbox"])),
            ),
            "page": logical,
            "physical_page": candidate["physical_page"],
            "bbox": [float(v) for v in candidate["bbox"]],
            "row_count": len(rows),
            "header_preview": " | ".join(str(name) for name in header)[:200],
        })
    entries.sort(key=lambda entry: (entry["page"], entry["bbox"][1]))
    possible_entries = []
    for signal in possible_tables:
        logical = signal.get("logical_page")
        body = pages.get(logical)
        if logical is None or body is None:
            continue
        page_digest = table_region_provenance.text_sha256(
            table_region_provenance.canonical_page_text(body))
        entry = {
            "signal_id": None,
            "document_id": doc_id,
            "page": logical,
            "physical_page": signal["physical_page"],
            "bbox": [float(value) for value in signal["bbox"]],
            "reason": signal["reason"],
            "status": "review_required",
            "page_text_sha256": page_digest,
            "sentinel_profile": sentinel["profile"],
            "sentinel_library_version": sentinel["library_version"],
            "preview": (signal.get("preview") or "")[:200],
        }
        entry["signal_id"] = \
            table_region_provenance.compute_possible_table_id(
                document_id=doc_id,
                source_pdf_sha256=pdf_digest,
                source_text_revision_sha256=revision_sha,
                sentinel_profile=sentinel["profile"],
                sentinel_config_fingerprint=sentinel["config_fingerprint"],
                page=logical,
                physical_page=signal["physical_page"],
                bbox=signal["bbox"],
                reason=signal["reason"],
                page_text_sha256=page_digest,
            )
        possible_entries.append(entry)
    possible_entries.sort(
        key=lambda entry: (entry["page"], entry["bbox"][1],
                           entry["signal_id"]))

    errors = [str(error) for error in scan_errors if str(error)]
    if errors:
        scan_status = "failed"
    elif possible_entries:
        scan_status = "inconclusive"
    elif entries:
        scan_status = "complete_with_candidates"
    else:
        scan_status = "complete_no_candidates"
    inventory = {
        "scheme": table_region_provenance.SCAN_SCHEME,
        "document_id": doc_id,
        "pdf_owner_document_id": pdf_owner or doc_id,
        "source_pdf_sha256": pdf_digest,
        "source_text_revision_sha256": revision_sha,
        "segment_derivation_receipt_id": segment_receipt_id,
        "detector_profile": detector["profile"],
        "detector_config_fingerprint": detector["config_fingerprint"],
        "detector_library_version": detector["library_version"],
        "sentinel_profile": sentinel["profile"],
        "sentinel_config_fingerprint": sentinel["config_fingerprint"],
        "sentinel_library_version": sentinel["library_version"],
        "scan_status": scan_status,
        "scan_errors": errors,
        "scanned_logical_pages": sorted(int(p) for p in scanned_logical_pages),
        "scanned_at": now_iso(),
        "candidates": entries,
        "possible_tables": possible_entries,
    }
    inventory["scan_id"] = table_region_provenance.compute_scan_id(inventory)
    return inventory


def _detect_table_candidates(pdf_path: Path, physical_pages, profile: str):
    """Table candidates read directly out of the registered PDF by the DAO.

    The DAO runs the detector itself rather than accepting an extent someone
    else derived: the whole point of the receipt is that the geometry came from
    bytes this process read out of the registered file.

    Returns `(candidates, possible_tables, detector, sentinel, error)`.
    `candidates` come only from the strict line detector and may authorize a
    receipt. `possible_tables` come from the broader text-layout sentinel (or
    from ambiguity inside a strict result) and can only block for review.
    """
    settings = table_region_provenance.DETECTOR_PROFILES.get(profile)
    if settings is None:
        return None, None, None, None, (
            f"unknown detector profile {profile!r} -- a profile must be "
            "declared in table_region_provenance.DETECTOR_PROFILES so its "
            "settings enter the receipt fingerprint")
    try:
        import fitz  # pymupdf
    except ImportError:
        return None, None, None, None, (
            "pymupdf is not installed -- the DAO cannot open the PDF itself, "
            "and a table extent it did not derive is not one it may issue")
    detector = {
        "tool": "dao.register-table-region",
        "profile": profile,
        "library": "pymupdf",
        "library_version": getattr(fitz, "VersionBind", None) or None,
        "config_fingerprint": table_region_provenance.detector_fingerprint(
            profile),
        "settings": repr(sorted(settings.items())),
    }
    sentinel_profile = table_region_provenance.DEFAULT_SENTINEL_PROFILE
    sentinel_settings = table_region_provenance.sentinel_settings(
        sentinel_profile)
    sentinel_library = table_region_provenance.sentinel_library(
        sentinel_profile)
    sentinel = {
        "tool": "dao.scan-table-candidates.sentinel",
        "profile": sentinel_profile,
        "library": sentinel_library,
        "library_version":
            table_region_provenance.current_sentinel_library_version(
                sentinel_profile),
        "config_fingerprint": table_region_provenance.sentinel_fingerprint(
            sentinel_profile),
        "settings": repr(sorted(sentinel_settings.items())),
    }
    # A sentinel backed by a library that is not installed must FAIL, never
    # silently degrade to "no table-like structure": that is precisely the
    # fail-open reading this whole module exists to prevent.
    if sentinel_library == "pdfplumber":
        try:
            import pdfplumber  # noqa: F401
        except ImportError:
            return None, None, detector, sentinel, (
                f"sentinel profile {sentinel_profile!r} needs pdfplumber, "
                f"which is not installed; a scan cannot establish table-free "
                f"without its high-recall second reading")
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:  # noqa: BLE001
        return None, None, detector, sentinel, \
            f"the PDF could not be opened: {exc}"
    # The sentinel may be backed by a different library than the strict
    # detector. Opened once for the whole scan rather than per page, and closed
    # in the same finally block as the fitz handle.
    plumber_doc = None
    if sentinel_library == "pdfplumber":
        import pdfplumber
        try:
            plumber_doc = pdfplumber.open(pdf_path)
        except Exception as exc:  # noqa: BLE001
            doc.close()
            return None, None, detector, sentinel, (
                f"the PDF could not be opened for the sentinel reading: {exc}")
    try:
        candidates = []
        possible_tables = []
        for physical in sorted(set(physical_pages)):
            if physical < 1 or physical > doc.page_count:
                return None, None, detector, sentinel, (
                    f"physical page {physical} is outside the document's "
                    f"{doc.page_count} pages")
            page = doc[physical - 1]
            try:
                found = page.find_tables(**settings)
            except Exception as exc:  # noqa: BLE001
                return None, None, detector, sentinel, (
                    f"table detection failed on physical page {physical}: "
                    f"{exc}")
            try:
                sentinel_tables = _run_sentinel(
                    page, plumber_doc, physical, sentinel_settings)
            except Exception as exc:  # noqa: BLE001
                return None, None, detector, sentinel, (
                    f"high-recall table sentinel failed on physical page "
                    f"{physical}: {exc}")
            # Every word with its own box, in the order the text layer emits
            # them. This is what binds a band to the RIGHT occurrence of its
            # text (see table_region_provenance.align_words_to_text); the
            # detector's geometry decides, not a substring search.
            words = list(page.get_text("words"))
            # Where the page's printed content actually starts and ends, from
            # the words themselves rather than the paper size -- margins vary,
            # and "the table runs to the bottom of the page" has to mean the
            # bottom of the TEXT. Feeds the continuation evidence below.
            body_top, body_bottom, page_bottom = _page_body_extent(words, page)
            for order, table in enumerate(found.tables):
                rows = []
                for row in table.rows:
                    cells = []
                    for cell_bbox in row.cells:
                        if cell_bbox is None:
                            # A merged cell reports None for the covered
                            # positions. Recorded as such so the ambiguity is
                            # visible rather than silently collapsed.
                            cells.append(None)
                            continue
                        cells.append({
                            "bbox": [float(v) for v in cell_bbox],
                            "text": page.get_textbox(fitz.Rect(cell_bbox)),
                        })
                    rows.append({
                        "bbox": [float(v) for v in row.bbox],
                        "cells": cells,
                    })
                header_names = []
                header_external = False
                if table.header is not None:
                    header_names = list(table.header.names or [])
                    header_external = bool(table.header.external)
                candidates.append({
                    "physical_page": physical,
                    "order": order,
                    "bbox": [float(v) for v in table.bbox],
                    "rows": rows,
                    "header_names": header_names,
                    "header_external": header_external,
                    "words": words,
                    "page_width": float(page.rect.width),
                    "page_height": float(page.rect.height),
                    # Text printed above this table and below any earlier
                    # table: a continuation does not reintroduce a heading, so
                    # a new one is evidence of a NEW table rather than a
                    # continuation (continuation_decision reads this).
                    "preceding_heading": _preceding_heading(
                        page, table.bbox, found.tables, order),
                    # Positive continuation evidence, read off the geometry the
                    # DAO itself measured. A table that runs out of PAGE was cut
                    # off by the break; one that begins at the top of the next
                    # page's body resumes it. Matching header and grid alone are
                    # not evidence of that -- two identically laid out
                    # appendices look the same -- so continuation_decision
                    # requires one of these.
                    #
                    # "Runs out of page" needs both the last line of text and
                    # the paper's edge: a small table alone on a page is
                    # trivially its own last line, which would otherwise read as
                    # cut off when it plainly ends mid-page.
                    "reaches_page_bottom": (
                        body_bottom is not None
                        and float(table.bbox[3]) >= body_bottom
                        - table_region_provenance.PAGE_EDGE_TOLERANCE
                        and body_bottom >= page_bottom
                        - table_region_provenance.PAGE_BOTTOM_MARGIN),
                    "starts_at_page_top": (
                        body_top is not None
                        and float(table.bbox[1]) <= body_top
                        + table_region_provenance.PAGE_EDGE_TOLERANCE),
                })

            strict_on_page = [
                candidate for candidate in candidates
                if candidate["physical_page"] == physical
            ]
            for table in sentinel_tables:
                if not _sentinel_table_is_plausible(table):
                    continue
                sentinel_bbox = [float(value) for value in table.bbox]
                if any(_strict_bbox_accounts_for_sentinel(
                        candidate["bbox"], sentinel_bbox)
                       for candidate in strict_on_page):
                    continue
                possible_tables.append({
                    "physical_page": physical,
                    "bbox": sentinel_bbox,
                    "reason": (
                        "high_recall_text_detector_found_table_like_structure_"
                        "outside_strict_geometry"),
                    "preview": page.get_textbox(
                        fitz.Rect(sentinel_bbox)).strip(),
                })

            # A strict bbox with merged/covered cell slots is a real candidate,
            # but its row/column relation is not authoritative. Keep the strict
            # candidate for audit and registration refusal, while also recording
            # an unresolved signal so the scan itself cannot be called complete.
            for candidate in strict_on_page:
                if any(cell is None
                       for row in candidate.get("rows") or []
                       for cell in row.get("cells") or []):
                    possible_tables.append({
                        "physical_page": physical,
                        "bbox": candidate["bbox"],
                        "reason": "strict_candidate_contains_merged_cells",
                        "preview": page.get_textbox(
                            fitz.Rect(candidate["bbox"])).strip(),
                    })
        return candidates, possible_tables, detector, sentinel, None
    finally:
        doc.close()
        if plumber_doc is not None:
            plumber_doc.close()


def _strict_bbox_accounts_for_sentinel(strict_bbox, sentinel_bbox) -> bool:
    """Whether a broad signal is materially contained in strict geometry.

    The sentinel often adds a few points of whitespace around the same table,
    so exact bbox equality would create false review signals. Conversely, a
    partially ruled strict bbox nested inside a materially larger text-layout
    bbox must remain inconclusive: that is precisely how rows outside the drawn
    lines would otherwise disappear.
    """
    sx0, sy0, sx1, sy1 = (float(value) for value in strict_bbox)
    bx0, by0, bx1, by1 = (float(value) for value in sentinel_bbox)
    broad_width = max(0.0, bx1 - bx0)
    strict_height = max(0.0, sy1 - sy0)
    if broad_width == 0 or strict_height == 0:
        return False
    horizontal_overlap = max(0.0, min(sx1, bx1) - max(sx0, bx0))
    vertical_overlap = max(0.0, min(sy1, by1) - max(sy0, by0))
    # The text strategy frequently absorbs a caption immediately ABOVE a ruled
    # table. That is not an omitted row. Permit top-only expansion, but never
    # bottom expansion: text rows below the strict bbox are exactly the partial-
    # ruling omission the sentinel exists to expose.
    return (
        horizontal_overlap / broad_width >= 0.85
        and vertical_overlap / strict_height >= 0.85
        and by1 <= sy1 + table_region_provenance.PAGE_EDGE_TOLERANCE
    )


def _run_sentinel(page, plumber_doc, physical: int, settings: dict) -> list:
    """The sentinel's tables for one page, whichever library backs the profile.

    Returns objects exposing `.bbox` and `.extract()` either way, which is all
    the caller and _sentinel_table_is_plausible need. pdfplumber's page index is
    0-based like fitz's, and both report bboxes in PDF points from the top-left,
    so the geometry stays directly comparable to the strict detector's.
    """
    if plumber_doc is None:
        return list(page.find_tables(**settings).tables)
    if physical - 1 >= len(plumber_doc.pages):
        raise IndexError(
            f"physical page {physical} is outside the sentinel document's "
            f"{len(plumber_doc.pages)} pages")
    plumber_page = plumber_doc.pages[physical - 1]
    return list(plumber_page.find_tables(table_settings=dict(settings)))


def _sentinel_table_is_plausible(table) -> bool:
    """Reject the common two-line prose false positive, keep high recall.

    PyMuPDF's text strategy can split two ordinary sentences into artificial
    columns. A real tabular pattern needs at least three populated rows, or two
    populated rows with repeated numeric columns. This remains a sentinel, not
    proof: anything admitted here blocks for review and never mints a receipt.
    """
    try:
        extracted = table.extract() or []
    except Exception:  # noqa: BLE001
        return True  # unreadable sentinel output is uncertainty, not absence
    populated = [
        [str(cell or "").strip() for cell in row]
        for row in extracted
        if sum(bool(str(cell or "").strip()) for cell in row) >= 2
    ]
    if len(populated) >= 3:
        return True
    numeric_rows = sum(
        any(re.search(r"\d", cell) for cell in row)
        for row in populated
    )
    if len(populated) < 2:
        return False
    if numeric_rows >= 1:
        return True
    # Two short, consistently multi-cell rows can be a small text-only lookup
    # table. Long sentence fragments are the prose false positive this branch
    # excludes. Bias toward review when uncertain.
    nonempty_cells = [
        cell for row in populated for cell in row if cell
    ]
    return bool(nonempty_cells) and max(map(len, nonempty_cells)) <= 30


def _page_body_extent(words, page):
    """Where the page's printed text starts and ends, and where the paper does.

    Both are needed, and neither alone works:

      * text extent alone is degenerate when the table IS the whole page body.
        A small table alone on a page trivially sits at both the first and last
        line, which would read as "cut off at the bottom" for a table that
        plainly ends mid-page.
      * paper edge alone ignores margins, so a table that genuinely runs to the
        last printable line looks like it stops well short.

    So `reaches_page_bottom` requires the table to end at the last line of text
    AND for that line to be near the bottom of the paper -- i.e. the text ran
    out of page, which is what a page break cutting a table looks like.

    Returns `(body_top, body_bottom, page_bottom)`, all None for a page with no
    words -- on which neither continuation fact can be established, so
    `continuation_decision` falls through to ambiguous rather than guessing.
    """
    tops = [float(word[1]) for word in words if str(word[4]).strip()]
    bottoms = [float(word[3]) for word in words if str(word[4]).strip()]
    if not tops or not bottoms:
        return None, None, None
    return min(tops), max(bottoms), float(page.rect.height)


def _preceding_heading(page, table_bbox, tables, order) -> str:
    """The nearest text printed above a table and below the previous one.

    Deliberately narrow: only lines that sit between this table's top and the
    bottom of whatever precedes it on the page. A heading further up the page
    belongs to an earlier table, and picking it up would make two sibling
    appendices look like one.
    """
    top = float(table_bbox[1])
    floor = 0.0
    for other in tables[:order]:
        floor = max(floor, float(other.bbox[3]))
    lines = []
    for block in page.get_text("blocks"):
        if len(block) < 5:
            continue
        y0, y1 = float(block[1]), float(block[3])
        if y1 <= top and y0 >= floor:
            text = str(block[4]).strip()
            if text:
                lines.append((y0, text))
    if not lines:
        return ""
    lines.sort()
    return lines[-1][1]


def _candidate_matches_anchor(candidate: dict, anchor: str) -> bool:
    """Whether a selector picks this candidate.

    The anchor is matched against the candidate's HEADER text only, not its
    whole body: matching anywhere in the table would let a value that happens
    to appear in one data row select the table, which is a selector that
    changes meaning when the data changes.
    """
    if not anchor:
        return True
    needle = anchor.strip()
    if not needle:
        return True
    header_text = " ".join(str(name or "") for name in
                           candidate.get("header_names") or [])
    if needle in header_text:
        return True
    # Fall back to the candidate's first row, which is the header band for a
    # table whose header the detector did not name separately.
    rows = candidate.get("rows") or []
    if rows:
        first = " ".join(
            str((cell or {}).get("text") or "") for cell in rows[0]["cells"])
        return needle in first
    return False


def _derive_table_scope(seed, candidates):
    """Follow a table across pages using the DAO's own scan.

    P0-8's first pass detected only the pages the CALLER named, so `--page 1`
    on a table running 1->2 produced a *verified* page-1-only receipt and the
    matching contract passed every check. The table's extent has to be derived
    like everything else in this module.

    Starting from the selected candidate, this walks forward one page at a
    time. Each step consults `continuation_decision`, which uses column
    geometry, the reprinted header and any new heading -- never the caller's
    page spec. The walk stops at the first `separate`, and REFUSES on
    `ambiguous`: an unresolvable continuation must not silently become a
    partial extent, which is the exact bug being fixed.

    Returns `(candidates_by_logical_page, error)`.
    """
    by_page = {seed["logical_page"]: seed}
    on_page = {}
    for candidate in candidates:
        on_page.setdefault(candidate.get("logical_page"), []).append(candidate)

    current = seed
    while True:
        following = current["logical_page"] + 1
        siblings = on_page.get(following) or []
        if not siblings:
            return by_page, None
        # A continuation is the FIRST table on the next page: a table starting
        # below another one cannot be the continuation of the previous page.
        siblings = sorted(siblings, key=lambda c: (c["bbox"][1], c["order"]))
        head_of_page = siblings[0]
        verdict, reason = table_region_provenance.continuation_decision(
            current, head_of_page)
        if verdict == "separate":
            return by_page, None
        if verdict == "ambiguous":
            return None, (
                f"logical page {following} carries a table that cannot be "
                f"distinguished from a continuation of page "
                f"{current['logical_page']}: {reason}")
        if following in by_page:
            return None, (
                f"continuation search revisited logical page {following}")
        by_page[following] = head_of_page
        current = head_of_page


def _derive_table_regions(candidates_by_page, pages, physical_for, profile):
    """Turn detected candidates into receipt regions bound to exact offsets.

    Two coordinate systems have to agree here: the detector works in PDF
    geometry, the contract's spans are offsets into the registered processed
    text. This maps one onto the other and REFUSES whenever the mapping is not
    exact and unique -- an approximate offset is indistinguishable from a
    correct one downstream, which is the whole failure class P0-8 exists to
    close.

    Returns `(extent, regions, errors)`.
    """
    extent: list = []
    regions: list = []
    errors: list[str] = []

    for logical in sorted(candidates_by_page):
        candidate = candidates_by_page[logical]
        page_text = pages.get(logical)
        if page_text is None:
            errors.append(
                f"logical page {logical} does not exist in the registered "
                "source-text revision, so the detected table cannot be bound "
                "to any processed text")
            continue
        canonical = table_region_provenance.canonical_page_text(page_text)
        page_digest = table_region_provenance.text_sha256(canonical)
        physical = physical_for(logical)
        if physical is None:
            errors.append(
                f"logical page {logical} has no physical page mapping -- a "
                "table region must name the physical page its geometry came "
                "from, and must not fall back to the logical number")
            continue

        rows = candidate.get("rows") or []
        if not rows:
            errors.append(
                f"logical page {logical}: the detected table has no row bands")
            continue

        # Anchor every word on the page to its OWN offset. This is what makes
        # the geometry decide the binding: the second `Grade` on a page
        # resolves to the second `Grade` in the text, so prose repeating a
        # row's wording above the table cannot capture the receipt.
        anchors, anchor_error = table_region_provenance.align_words_to_text(
            candidate.get("words") or [], canonical)
        if anchor_error:
            errors.append(f"logical page {logical}: {anchor_error}")
            continue

        # Header identification: the detector's own, never inferred from
        # position alone. `external` means the header sits outside the table
        # body, in which case the first body row is data, not a header.
        header_indexes = set()
        if candidate.get("header_names") and not candidate.get(
                "header_external"):
            header_indexes.add(0)

        page_regions = []
        for index, row in enumerate(rows):
            if any(cell is None for cell in row["cells"]):
                errors.append(
                    f"logical page {logical}: row band {index} contains merged "
                    "cells, so its row/column structure is ambiguous -- an "
                    "ambiguous table is refused rather than resolved by "
                    "heuristic; resolve it by review")
                continue

            band_words = table_region_provenance.words_in_bbox(
                anchors, row["bbox"])
            contiguity = table_region_provenance.contiguity_error(
                band_words, anchors,
                f"logical page {logical}, row band {index}")
            if contiguity:
                errors.append(contiguity)
                continue
            start, end, span_error = \
                table_region_provenance.span_from_anchors(band_words)
            if span_error:
                errors.append(f"logical page {logical}, row band {index}: "
                              f"{span_error}")
                continue

            cell_records = []
            for cell in row["cells"]:
                # EVERY word of the cell, not just its first line. A cell
                # reading "보험금 지급 / 단 동일 사고는 1회로 제한" carries its
                # limitation on the second line; binding only the first line
                # left that text outside the receipt AND outside reverse
                # coverage, so it could be dropped unnoticed.
                cell_words = table_region_provenance.words_in_bbox(
                    anchors, cell["bbox"])
                if not cell_words:
                    continue
                cell_contiguity = table_region_provenance.contiguity_error(
                    cell_words, anchors,
                    f"logical page {logical}, row band {index}, cell")
                if cell_contiguity:
                    errors.append(cell_contiguity)
                    continue
                cell_start, cell_end, cell_error = \
                    table_region_provenance.span_from_anchors(cell_words)
                if cell_error:
                    errors.append(
                        f"logical page {logical}, row band {index}: "
                        f"{cell_error}")
                    continue
                if cell_start < start or cell_end > end:
                    errors.append(
                        f"logical page {logical}, row band {index}: a cell's "
                        "derived range falls outside its own row band")
                    continue
                cell_records.append({
                    "page": logical,
                    "start_char": cell_start,
                    "end_char": cell_end,
                    "quote": canonical[cell_start:cell_end],
                    "text": canonical[cell_start:cell_end],
                    "bbox": [float(v) for v in cell["bbox"]],
                })

            page_regions.append({
                "page": logical,
                "physical_page": physical,
                "kind": (
                    table_region_provenance.DATA_ROW_KIND
                    if index not in header_indexes
                    else ("column_header" if not extent else "repeated_header")
                ),
                "start_char": start,
                "end_char": end,
                "quote": canonical[start:end],
                "page_text_sha256": page_digest,
                "bbox": [float(v) for v in row["bbox"]],
                "cells": cell_records,
            })

        if errors:
            continue
        if not page_regions:
            errors.append(
                f"logical page {logical}: no row band could be bound to the "
                "registered text")
            continue

        # The extent is the union of the bands actually derived, not the
        # detector's raw bbox: the receipt's extent has to be expressible in the
        # same offsets a contract's source_regions use, or the two could never
        # be compared exactly.
        page_start = min(region["start_char"] for region in page_regions)
        page_end = max(region["end_char"] for region in page_regions)
        extent.append({
            "page": logical,
            "physical_page": physical,
            "kind": "table_extent",
            "start_char": page_start,
            "end_char": page_end,
            "quote": canonical[page_start:page_end],
            "page_text_sha256": page_digest,
            "bbox": [float(v) for v in candidate["bbox"]],
        })
        regions.extend(page_regions)

    if errors:
        return [], [], errors
    return extent, regions, []


def _table_region_finalize_blockers(case_id: str, doc_id: str) -> list[str]:
    """P0-8 gate: this document must have been SCANNED, and everything the scan
    found must have a disposition.

    The follow-up's version asked the wrong question. It started from the
    reference_table contract -- `if tables is None: return []` -- so the gate
    only ran on documents somebody had already chosen to extract a table from.
    A policy PDF full of tables with no reference_table contract at all was not
    "unverified", it was UNASKED, and finalization was clean. That is the same
    self-declaration defect P0-8 exists to remove, moved up one more level: the
    caller could no longer declare a table's extent, but could still decide
    whether the document had tables by simply not writing a contract.

    So the gate now begins at the document, not the contract:

      1. the document must carry a CURRENT candidate scan (its absence is a
         blocker, never a pass -- "nobody scanned" is not "no tables")
      2. the scan's own integrity and currency are re-derived here
      3. if the scan found candidates, a reference_table contract is required,
         and every candidate must be extracted or human-reviewed
      4. if the scan found none, no contract is owed

    Deliberately a PURE gate: it verifies a scan exists and is current, and
    never runs the detector itself. Scanning here would mean finalization
    mutated state, and a document that had never been scanned would silently
    acquire one at the last moment instead of the operator being told a real
    step was skipped.
    """
    if uid_scheme_for(case_id, doc_id) != "canonical_v1":
        return []

    tables = read_contract_data(case_id, f"reference_table_{doc_id}.json")
    location = f"{doc_id} table scan"

    index = load_table_region_index(case_id)
    integrity = table_region_provenance.index_integrity_errors(
        index, case_id, location)
    if integrity:
        return integrity
    schema_errors = _schema_check(index, table_region_provenance.INDEX_SCHEMA)
    if schema_errors:
        return [f"{location}: the table region index is schema-invalid, so "
                f"nothing in it may be relied on: {error}"
                for error in schema_errors]

    inventory = table_candidate_inventory_for(case_id, doc_id)
    if inventory is None:
        return [
            f"{doc_id}: no DAO-derived table candidate scan exists -- a "
            "policy document may not finalize on the assumption that it "
            "contains no tables. Run `dao.py scan-table-candidates "
            f"--case-id {case_id} --doc-id {doc_id}`"
        ]

    blockers = table_region_provenance.inventory_integrity_errors(
        inventory, doc_id, location)
    if blockers:
        return blockers

    text = _registered_revision_text(case_id, doc_id)
    current_pages = None
    if text is not None:
        try:
            current_pages = policy_completeness.split_pages(text)
        except Exception:  # noqa: BLE001
            current_pages = None
    segment_receipt = segment_derivation_receipt_for(case_id, doc_id)
    blockers = table_region_provenance.inventory_currency_errors(
        inventory,
        current_pdf_sha256=registered_source_pdf_sha256(
            case_id, _table_region_pdf_owner(case_id, doc_id)),
        current_revision_sha256=(revision_entry_for(case_id, doc_id) or {}).get(
            "current_revision_sha256"),
        current_pages=current_pages,
        segment_receipt_id=(_segment_receipt_digest(segment_receipt)
                            if segment_receipt else None),
        location=location,
    )
    if blockers:
        return blockers

    scan_status = inventory.get("scan_status")
    if scan_status == "unverifiable_ocr_source":
        # A human explicitly authorized proceeding without a verified table-
        # boundary scan (see cmd_record_unverifiable_table_scan). This is
        # never a blocker -- that is the entire point of the override -- but
        # it is also never silent: human_override is schema-required on this
        # status, so the authorization is permanently on record in
        # _table_region_index.json for any later audit.
        return []
    if scan_status in ("inconclusive", "failed"):
        signals = inventory.get("possible_tables") or []
        pages = sorted({signal.get("page") for signal in signals
                        if signal.get("page") is not None})
        reasons = sorted({signal.get("reason") for signal in signals
                          if signal.get("reason")})
        detail = reasons or inventory.get("scan_errors") or [
            "unspecified detector failure"]
        return [
            f"{doc_id}: table candidate scan is {scan_status}, not a verified "
            f"empty/complete result -- possible table-like structure remains "
            f"on logical page(s) {pages or 'unknown'} ({detail}). "
            "A high-recall sentinel finding never authorizes 'no tables'; "
            "resolve it through genuine review or improve strict extraction."
        ]

    detected = inventory.get("candidates") or []
    if scan_status == "complete_no_candidates" and not detected:
        # The one case where no reference_table is owed -- and only because the
        # strict detector AND the independent high-recall sentinel looked and
        # found nothing. This is a scanned result, not an absent one.
        return []
    if scan_status != "complete_with_candidates":
        return [
            f"{doc_id}: table candidate scan has inconsistent status "
            f"{scan_status!r}; finalization refuses rather than treating it as "
            "an empty document"
        ]

    if tables is None:
        role = policy_roles.declared_role(next(
            (d for d in (read_contract_data(
                case_id, "document_manifest.json") or {}).get("documents", [])
             if d.get("document_id") == doc_id), {}) or {})
        pages = sorted({candidate.get("page") for candidate in detected})
        extra = ""
        if role in ("clause_segment", "standalone_policy"):
            # A clause document that turns out to contain tables is not exempt
            # by virtue of its role -- the role has to change to match what the
            # document actually is.
            extra = (f" This document declares policy_processing_role "
                     f"{role!r}, which owes no reference_table; a "
                     f"{role!r} containing tables must be redeclared "
                     "'mixed_clause_and_table' and extract them.")
        return [
            f"{doc_id}: the DAO's scan found {len(detected)} table(s) on "
            f"logical page(s) {pages}, but no reference_table_{doc_id}.json "
            "extracts any of them -- a document with detected tables and no "
            "extraction is unverified, not table-free." + extra
        ]

    # Candidate coverage is a FINALIZATION question, not a write-time one: a
    # document is built up one table at a time, so demanding every candidate be
    # extracted on the first write would make an incremental extraction
    # impossible. By finalization there is no later write to wait for.
    return _table_region_binding_blockers(
        case_id, doc_id, tables, check_candidate_coverage=True)


def _table_region_binding_blockers(case_id: str, doc_id: str, tables: dict,
                                    check_candidate_coverage: bool = False
                                    ) -> list[str]:
    """Bind a reference_table contract to its receipts, freshly re-checked.

    Used by BOTH the write gate and finalization, so a table cannot be written
    against a current receipt and then finalize after the world moved -- the
    identical comparison runs at both moments against freshly read state.

    Two things the first P0-8 pass got wrong here:

    * **Currency was checked over EVERY receipt on the document.** After a
      legitimate source revision and a fresh re-registration, the superseded
      receipt still failed its currency check and blocked the document
      permanently, with no recovery path. Currency now applies to the receipts
      the contract actually CITES; superseded ones stay in the index as audit
      history. Citing a stale receipt is of course still refused -- that is the
      same check, just correctly scoped.
    * **The index was read and trusted.** `receipt_id` was compared as a
      string, so a receipt body edited in place still resolved. The index's own
      integrity is now re-derived first, and a corrupt index blocks rather than
      degrading to "no receipts exist".
    """
    index = load_table_region_index(case_id)
    integrity = table_region_provenance.index_integrity_errors(
        index, case_id, f"reference_table_{doc_id}.json")
    if integrity:
        # Fail closed on the whole index: with the receipt bodies unverified,
        # nothing built on top of them can be trusted either.
        return integrity
    schema_errors = _schema_check(index, table_region_provenance.INDEX_SCHEMA)
    if schema_errors:
        return [
            f"reference_table_{doc_id}.json: the table region index is "
            f"schema-invalid, so no receipt in it may be relied on: {error}"
            for error in schema_errors
        ]

    receipts = table_region_receipts_for(case_id, doc_id)
    text = _registered_revision_text(case_id, doc_id)
    if text is None:
        return [
            f"{doc_id}: no registered source-text revision -- a table region "
            "receipt's offsets cannot be re-checked against anything"
        ]
    try:
        pages = policy_completeness.split_pages(text)
    except Exception as exc:  # noqa: BLE001
        return [f"{doc_id}: registered source text is unusable: {exc}"]

    current_pdf = registered_source_pdf_sha256(
        case_id, _table_region_pdf_owner(case_id, doc_id))
    current_revision = (revision_entry_for(case_id, doc_id) or {}).get(
        "current_revision_sha256")
    segment_receipt = segment_derivation_receipt_for(case_id, doc_id)
    segment_receipt_id = (
        _segment_receipt_digest(segment_receipt) if segment_receipt else None)

    cited = {table.get("table_region_receipt_id")
             for table in tables.get("tables") or []}

    blockers: list[str] = []
    # A receipt that is no longer current cannot authorize anything, so
    # staleness is checked before the structural comparison rather than after.
    fresh: list = []
    for receipt in receipts:
        if receipt.get("receipt_id") not in cited:
            continue  # audit history, not a current authority
        currency = table_region_provenance.receipt_currency_errors(
            receipt=receipt,
            current_pdf_sha256=current_pdf,
            current_revision_sha256=current_revision,
            current_pages=pages,
            segment_receipt_id=segment_receipt_id,
            location=f"reference_table_{doc_id}.json receipt "
                     f"{receipt.get('receipt_id')}",
        )
        if currency:
            blockers.extend(currency)
            continue
        fresh.append(receipt)

    blockers.extend(table_region_provenance.contract_binding_errors(
        tables, fresh, pages, "tables"))

    if check_candidate_coverage and not blockers:
        # Only meaningful once the cited tables themselves verify: reporting
        # "another candidate is unaccounted for" on top of a broken table would
        # bury the finding that matters.
        inventory = table_candidate_inventory_for(case_id, doc_id)
        location = f"reference_table_{doc_id}.json"
        # The inventory's own integrity FIRST. Coverage reads the candidate
        # list to decide what still needs a disposition, so an edited list
        # would let coverage confirm the very omission it exists to detect.
        if inventory is not None:
            integrity = table_region_provenance.inventory_integrity_errors(
                inventory, doc_id, location)
            if integrity:
                return integrity
        blockers.extend(table_region_provenance.candidate_coverage_errors(
            inventory,
            receipts,
            {receipt.get("receipt_id") for receipt in fresh},
            _table_candidate_human_reviews(case_id, doc_id),
            location,
        ))
    return blockers


def _table_candidate_human_reviews(case_id: str, doc_id: str) -> set:
    """Candidate ids a genuine human review has recorded a decision on.

    Read from the DAO-owned human-review ledger, which is the existing
    authenticated path (`dao.py record-human-review`). Deliberately not a field
    an agent can set on the candidate itself: a self-declared "not a table" is
    the same shape as the self-declared region P0-8 exists to remove.
    """
    ledger = load_human_review_ledger(case_id) or {}
    reviewed = set()
    for entry in ledger.get("reviews", []):
        if entry.get("artifact_kind") != "table_candidate":
            continue
        if entry.get("artifact_id") != doc_id:
            continue
        if entry.get("decision") in ("accepted_risk", "verified"):
            reviewed.add(entry.get("target_key"))
    return reviewed


def _table_region_pdf_owner(case_id: str, doc_id: str) -> str:
    """Which document's raw PDF a table's regions are derived from.

    A segment's table geometry comes from the PARENT's pages, matching the
    canonical UID rule that identity is keyed to the immutable original.
    """
    manifest = read_contract_data(case_id, "document_manifest.json") or {}
    entry = next((d for d in manifest.get("documents", [])
                  if d.get("document_id") == doc_id), None)
    if entry and entry.get("document_role") == "segment":
        return entry.get("source_document_id") or doc_id
    return doc_id


def _segment_receipt_digest(receipt: dict) -> str:
    """A stable digest of a P0-6 receipt, used to detect it being re-derived."""
    return hashlib.sha256(
        repr(segment_derivation.receipt_fingerprint(receipt)
             ).encode("utf-8")).hexdigest()


class _TableScanRefused(Exception):
    """The document cannot be scanned; the message is the caller-facing reason.

    Raised rather than returned so a partially-prepared scan can never be
    mistaken for a completed one: there is no value to accidentally treat as
    success.
    """


def _prepare_table_scan(case_id: str, doc_id: str, profile: str | None):
    """Everything both `scan-table-candidates` and `register-table-region`
    must establish before a single table candidate may be believed.

    Extracted so the two commands cannot drift: a scan issued by one and a
    receipt issued by the other have to rest on the SAME verified source
    identity, or a receipt could be current against bytes its own scan never
    saw. Raises `_TableScanRefused` on any failure -- the whole point is that
    an unverifiable source produces no scan at all, never a partial one.

    Returns a dict with the verified source identity, the registered pages, the
    logical->physical map, the detected candidates and the detector identity.
    """
    manifest = read_contract_data(case_id, "document_manifest.json")
    if manifest is None:
        raise _TableScanRefused(
            "document_manifest.json does not exist -- no registered source "
            "can be resolved, so no PDF may be opened")
    entry = next((d for d in manifest.get("documents", [])
                  if d.get("document_id") == doc_id), None)
    if entry is None:
        raise _TableScanRefused(
            f"{doc_id} is not registered in document_manifest.json")

    method = entry.get("extraction_method")
    if method not in table_region_provenance.VERIFIABLE_EXTRACTION_METHODS:
        raise _TableScanRefused(
            f"extraction_method {method!r} has no deterministic text layer to "
            "scan for tables. Establishing what tables an image-only page "
            "contains means running OCR, which UID verification may not do, so "
            "no scan can be issued and the document cannot finalize. This is a "
            "stated P0-8 limitation, not a state to work around -- resolve it "
            "by human review.")

    pdf_owner = _table_region_pdf_owner(case_id, doc_id)
    pdf_path = _raw_source_path(case_id, pdf_owner)
    if pdf_path is None:
        raise _TableScanRefused(
            f"{pdf_owner} has no readable registered raw file -- there is no "
            "immutable source to derive a region from")
    actual_pdf_digest = registered_source_pdf_sha256(case_id, pdf_owner)
    if actual_pdf_digest is None:
        raise _TableScanRefused(f"{pdf_owner}'s raw source could not be hashed")
    recorded_digest = next(
        (d.get("source_pdf_sha256") for d in manifest.get("documents", [])
         if d.get("document_id") == pdf_owner), None)
    if recorded_digest and recorded_digest != actual_pdf_digest:
        raise _TableScanRefused(
            f"{pdf_owner} records source_pdf_sha256 {recorded_digest!r} but "
            f"the registered file now hashes to {actual_pdf_digest!r} -- the "
            "raw source changed; a region derived from it cannot be trusted")

    revision_sha = (revision_entry_for(case_id, doc_id) or {}).get(
        "current_revision_sha256")
    if not revision_sha:
        raise _TableScanRefused(
            f"{doc_id} has no registered source-text revision -- a table scan "
            "must be bound to the exact processed bytes its offsets index "
            "into, or a later rewrite would silently inherit this "
            "derivation's verification.")
    text = _registered_revision_text(case_id, doc_id)
    if text is None:
        raise _TableScanRefused(
            f"{doc_id}'s registered revision text could not be read")
    try:
        pages = policy_completeness.split_pages(text)
    except Exception as exc:  # noqa: BLE001
        raise _TableScanRefused(
            f"{doc_id}'s registered source text is unusable: {exc}") from exc
    if not pages:
        raise _TableScanRefused(
            f"{doc_id}'s registered source text has no pages to scan")

    # Which physical pages this document actually OWNS. For a segment that is
    # the P0-6 page map, so a segment's scan covers the parent's pages it owns
    # and no others -- scanning the whole parent would report its siblings'
    # tables as this document's unhandled candidates.
    physical_by_logical = {}
    unmapped = []
    for logical in sorted(pages):
        physical = _physical_page_for(case_id, doc_id, logical)
        if physical is None:
            unmapped.append(logical)
            continue
        physical_by_logical[logical] = physical
    if unmapped:
        raise _TableScanRefused(
            f"logical page(s) {unmapped} of {doc_id} have no physical page "
            "mapping, so they cannot be scanned -- and an unscanned page is "
            "not a page without a table. For a segment this mapping comes from "
            "the P0-6 derivation receipt, which must be registered first")

    profile = profile or table_region_provenance.DEFAULT_DETECTOR_PROFILE
    candidates, possible_tables, detector, sentinel, detect_error = \
        _detect_table_candidates(
            pdf_path, sorted(physical_by_logical.values()), profile)
    if detect_error:
        # Once source identity and page ownership are established, a detector
        # failure is a meaningful scan result. The explicit scan command
        # persists it as `failed` so "attempted and failed" is distinguishable
        # from "never scanned"; register-table-region still refuses below.
        if detector is None or sentinel is None:
            raise _TableScanRefused(detect_error)
        candidates = candidates or []
        possible_tables = possible_tables or []

    logical_by_physical = {physical: logical
                           for logical, physical in physical_by_logical.items()}
    for candidate in candidates:
        candidate["logical_page"] = logical_by_physical.get(
            candidate["physical_page"])
    for signal in possible_tables:
        signal["logical_page"] = logical_by_physical.get(
            signal["physical_page"])

    segment_receipt = segment_derivation_receipt_for(case_id, doc_id)
    return {
        "manifest": manifest,
        "entry": entry,
        "pdf_owner": pdf_owner,
        "pdf_path": pdf_path,
        "pdf_digest": actual_pdf_digest,
        "revision_sha": revision_sha,
        "pages": pages,
        "physical_by_logical": physical_by_logical,
        "candidates": candidates,
        "possible_tables": possible_tables,
        "detector": detector,
        "sentinel": sentinel,
        "scan_errors": [detect_error] if detect_error else [],
        "segment_receipt_id": (
            _segment_receipt_digest(segment_receipt) if segment_receipt
            else None),
    }


def _scan_inventory_from(case_id, doc_id, prepared):
    """Build the checksummed, DAO-owned inventory from a prepared scan."""
    return _build_candidate_inventory(
        case_id, doc_id, prepared["candidates"], prepared["possible_tables"],
        prepared["detector"], prepared["sentinel"], prepared["pdf_digest"],
        prepared["revision_sha"], pages=prepared["pages"],
        scan_errors=prepared.get("scan_errors") or (),
        scanned_logical_pages=sorted(prepared["physical_by_logical"]),
        pdf_owner=prepared["pdf_owner"],
        segment_receipt_id=prepared["segment_receipt_id"],
    )


_UNVERIFIABLE_OCR_PROFILE = "none_ocr_source_unverifiable"


def _prepare_unverifiable_table_scan(case_id: str, doc_id: str):
    """Everything `record-unverifiable-table-scan` must establish before an
    OCR-sourced document may be allowed past P0-8 without a deterministic
    table-boundary scan.

    Deliberately NOT a relaxed `_prepare_table_scan`: it still requires the
    document be registered and its raw source/text-revision identity resolved
    (an unregistered or digest-mismatched document is refused exactly as it
    would be for a real scan) but it never opens the PDF or runs a detector --
    there is nothing deterministic to run on an image-only page. The only
    thing this function is willing to certify is source identity, not table
    geometry.
    """
    manifest = read_contract_data(case_id, "document_manifest.json")
    if manifest is None:
        raise _TableScanRefused(
            "document_manifest.json does not exist -- no registered source "
            "can be resolved, so no override can be recorded")
    entry = next((d for d in manifest.get("documents", [])
                  if d.get("document_id") == doc_id), None)
    if entry is None:
        raise _TableScanRefused(
            f"{doc_id} is not registered in document_manifest.json")

    method = entry.get("extraction_method")
    if method in table_region_provenance.VERIFIABLE_EXTRACTION_METHODS:
        raise _TableScanRefused(
            f"{doc_id}'s extraction_method is {method!r}, which HAS a "
            "deterministic text layer -- use `scan-table-candidates` for a "
            "real verified scan instead of an override; recording an "
            "unverifiable-source override for a verifiable document would "
            "hide a check that could actually run.")

    pdf_owner = _table_region_pdf_owner(case_id, doc_id)
    actual_pdf_digest = registered_source_pdf_sha256(case_id, pdf_owner)
    if actual_pdf_digest is None:
        raise _TableScanRefused(f"{pdf_owner}'s raw source could not be hashed")
    recorded_digest = next(
        (d.get("source_pdf_sha256") for d in manifest.get("documents", [])
         if d.get("document_id") == pdf_owner), None)
    if recorded_digest and recorded_digest != actual_pdf_digest:
        raise _TableScanRefused(
            f"{pdf_owner} records source_pdf_sha256 {recorded_digest!r} but "
            f"the registered file now hashes to {actual_pdf_digest!r} -- the "
            "raw source changed; an override recorded against it cannot be "
            "trusted")

    revision_sha = (revision_entry_for(case_id, doc_id) or {}).get(
        "current_revision_sha256")
    if not revision_sha:
        raise _TableScanRefused(
            f"{doc_id} has no registered source-text revision -- the override "
            "must still be bound to exact processed bytes, or a later "
            "rewrite would silently inherit this override's authorization")
    text = _registered_revision_text(case_id, doc_id)
    if text is None:
        raise _TableScanRefused(
            f"{doc_id}'s registered revision text could not be read")
    try:
        pages = policy_completeness.split_pages(text)
    except Exception as exc:  # noqa: BLE001
        raise _TableScanRefused(
            f"{doc_id}'s registered source text is unusable: {exc}") from exc
    if not pages:
        raise _TableScanRefused(
            f"{doc_id}'s registered source text has no pages to cover")

    physical_by_logical = {}
    unmapped = []
    for logical in sorted(pages):
        physical = _physical_page_for(case_id, doc_id, logical)
        if physical is None:
            unmapped.append(logical)
            continue
        physical_by_logical[logical] = physical
    if unmapped:
        raise _TableScanRefused(
            f"logical page(s) {unmapped} of {doc_id} have no physical page "
            "mapping -- an override still requires knowing which physical "
            "pages this document owns")

    segment_receipt = segment_derivation_receipt_for(case_id, doc_id)
    return {
        "pdf_owner": pdf_owner,
        "pdf_digest": actual_pdf_digest,
        "revision_sha": revision_sha,
        "physical_by_logical": physical_by_logical,
        "segment_receipt_id": (
            _segment_receipt_digest(segment_receipt) if segment_receipt
            else None),
    }


def _unverifiable_scan_inventory(case_id, doc_id, prepared, *, authorized_by,
                                  reason):
    """Build the `unverifiable_ocr_source` inventory: source identity is
    verified exactly as a real scan requires, but scan_status records
    plainly that no table-boundary geometry was ever derived -- never
    `complete_no_candidates`, which is reserved for a genuine scan that
    looked and found nothing.
    """
    inventory = {
        "scheme": table_region_provenance.SCAN_SCHEME,
        "document_id": doc_id,
        "pdf_owner_document_id": prepared["pdf_owner"] or doc_id,
        "source_pdf_sha256": prepared["pdf_digest"],
        "source_text_revision_sha256": prepared["revision_sha"],
        "segment_derivation_receipt_id": prepared["segment_receipt_id"],
        "detector_profile": _UNVERIFIABLE_OCR_PROFILE,
        "detector_config_fingerprint": "n/a",
        "detector_library_version": "n/a",
        "sentinel_profile": _UNVERIFIABLE_OCR_PROFILE,
        "sentinel_config_fingerprint": "n/a",
        "sentinel_library_version": "n/a",
        "scan_status": "unverifiable_ocr_source",
        "human_override": {
            "authorized_by": authorized_by,
            "authorized_at": now_iso(),
            "reason": reason,
        },
        "scan_errors": [],
        "scanned_logical_pages": sorted(prepared["physical_by_logical"]),
        "scanned_at": now_iso(),
        "candidates": [],
        "possible_tables": [],
    }
    inventory["scan_id"] = table_region_provenance.compute_scan_id(inventory)
    return inventory


def cmd_record_unverifiable_table_scan(args):
    """Human-authorized P0-8 override for an OCR-sourced document.

    `extraction_method: ocr` has no deterministic text layer, so P0-8's real
    detector can never run on it -- `scan-table-candidates` always refuses.
    This command does not weaken that check; it records a DIFFERENT, honestly
    labeled state (`unverifiable_ocr_source`, never `complete_no_candidates`)
    that finalization accepts only because a human explicitly authorized
    proceeding without a verified table-boundary scan. The risk this accepts:
    an un-scanned OCR document may contain a table (e.g. a 장해분류표 payout
    schedule) whose boundary is never verified, so a downstream clause could
    silently mismap a row it should have anchored to. See known-gaps.md for
    the recorded risk acceptance behind this command's existence.
    """
    case_id, doc_id = args.case_id, args.doc_id
    case_directory = case_dir(case_id)

    blockers = dao_transaction.pending_journal_errors(case_directory)
    if blockers:
        for blocker in blockers:
            print(f"BLOCKED: {blocker}")
        return 1

    try:
        prepared = _prepare_unverifiable_table_scan(case_id, doc_id)
    except _TableScanRefused as refusal:
        print(f"BLOCKED: {refusal}")
        return 1

    inventory = _unverifiable_scan_inventory(
        case_id, doc_id, prepared,
        authorized_by=args.authorized_by, reason=args.reason)
    existing = table_candidate_inventory_for(case_id, doc_id)
    if existing is not None and existing.get("scan_id") == inventory["scan_id"]:
        print(f"PASS: {doc_id} unverifiable-OCR override is unchanged (no-op) "
              f"-- {inventory['scan_id'][:16]}")
        return 0

    rc = _commit_table_region(case_id, doc_id, None, inventory,
                              args.held_by, args.run_id)
    if rc == 0:
        print(f"PASS: {doc_id} recorded as unverifiable_ocr_source -- NO table "
              "boundary was verified; this is a human-authorized reduced-"
              f"assurance pass, authorized by {args.authorized_by!r}: "
              f"{args.reason!r}")
    return rc


def cmd_scan_table_candidates(args):
    """Record what tables the DAO's own detector finds in a document.

    Separated from `register-table-region` deliberately. When the inventory was
    a side effect of registration, a document nobody registered a table for had
    NO scan -- and the finalization gate then read the absent scan as "nothing
    to check", so the way to make a table invisible was simply never to mention
    it. The scan has to be an obligation the document carries, independent of
    whether anybody chose to extract anything.

    This is also why finalization does not silently run the detector itself:
    the gate is a pure verification that a current scan exists, so it cannot
    mutate state, and refusing tells the operator that a real step is missing
    rather than quietly performing it.
    """
    case_id, doc_id = args.case_id, args.doc_id
    case_directory = case_dir(case_id)

    blockers = dao_transaction.pending_journal_errors(case_directory)
    if blockers:
        for blocker in blockers:
            print(f"BLOCKED: {blocker}")
        return 1

    try:
        prepared = _prepare_table_scan(case_id, doc_id, args.detector_profile)
    except _TableScanRefused as refusal:
        print(f"BLOCKED: {refusal}")
        return 1

    inventory = _scan_inventory_from(case_id, doc_id, prepared)
    existing = table_candidate_inventory_for(case_id, doc_id)
    if existing is not None and existing.get("scan_id") == inventory["scan_id"]:
        # Identical bytes, identical detector, identical findings. Re-scanning
        # must not cost a downstream rerun.
        label = "BLOCKED" if inventory["scan_status"] == "failed" else "PASS"
        print(f"{label}: {doc_id} table candidate scan is unchanged (no-op) -- "
              f"{len(inventory['candidates'])} candidate(s) across "
              f"{len(inventory['scanned_logical_pages'])} page(s), scan "
              f"{inventory['scan_id'][:16]}")
        return 1 if inventory["scan_status"] == "failed" else 0

    rc = _commit_table_region(case_id, doc_id, None, inventory,
                              args.held_by, args.run_id)
    if rc == 0:
        print(f"PASS: {doc_id} scanned -- {len(inventory['candidates'])} table "
              f"candidate(s) across {len(inventory['scanned_logical_pages'])} "
              f"page(s), status {inventory['scan_status']}, scan "
              f"{inventory['scan_id'][:16]}")
        for candidate in inventory["candidates"]:
            print(f"  {candidate['candidate_id'][:20]} logical page "
                  f"{candidate['page']}: {candidate['row_count']} row band(s), "
                  f"header {candidate['header_preview']!r}")
        for signal in inventory["possible_tables"]:
            print(f"  REVIEW_REQUIRED {signal['signal_id'][:20]} logical page "
                  f"{signal['page']}: {signal['reason']}; "
                  f"{signal['preview'][:80]!r}")
        if inventory["scan_status"] == "complete_no_candidates":
            print("  no tables detected -- recorded as a scanned result, which "
                  "is what lets finalization tell 'no tables' from 'never "
                  "looked'")
        elif inventory["scan_status"] == "inconclusive":
            print("  scan is inconclusive -- high-recall table-like signals "
                  "cannot authorize an empty document and block finalization")
        elif inventory["scan_status"] == "failed":
            print("  scan failed -- recorded for audit and finalization remains "
                  f"blocked: {inventory['scan_errors']}")
            return 1
    return rc


def cmd_register_table_region(args):
    """Derive a table's authoritative region and issue a table_region_v1
    receipt.

    Nothing the caller submits defines the table: the extent, the row bands and
    their classification all come from running a real detector over the
    registered PDF in this process. `--anchor` and `--page` are a SELECTOR --
    they choose among candidates the DAO found, and a selector matching two
    candidates or none issues no receipt at all.

    Ordering follows dao_transaction's rule -- everything that can fail runs
    before anything irreversible:

      1. resolve + verify the document (registered, canonical, digest)
      2. detect table candidates in the PDF ourselves
      3. resolve the selector to EXACTLY one candidate per page
      4. bind every band to exact offsets in the registered revision
      5. build the receipt; an identical one already on file is a NO-OP
      6. validate the prospective index
      7. journal, then write, then clear
    """
    case_id, doc_id = args.case_id, args.doc_id
    case_directory = case_dir(case_id)

    blockers = dao_transaction.pending_journal_errors(case_directory)
    if blockers:
        for blocker in blockers:
            print(f"BLOCKED: {blocker}")
        return 1

    # --- 1+2. the verified source, and a scan of the WHOLE document --------
    # Shared with `scan-table-candidates` so a receipt and the scan that must
    # account for it always rest on the same verified source identity.
    try:
        prepared = _prepare_table_scan(case_id, doc_id, args.detector_profile)
    except _TableScanRefused as refusal:
        print(f"BLOCKED: {refusal}")
        return 1
    if prepared.get("scan_errors"):
        print("BLOCKED: table detection failed; no authoritative region can be "
              f"issued: {prepared['scan_errors']}")
        return 1

    pages = prepared["pages"]
    physical_by_logical = prepared["physical_by_logical"]
    candidates = prepared["candidates"]
    detector = prepared["detector"]
    actual_pdf_digest = prepared["pdf_digest"]
    revision_sha = prepared["revision_sha"]
    profile = detector["profile"]
    physical_for = (lambda logical: _physical_page_for(
        case_id, doc_id, logical))

    # `--page` is a SEED, not the scope. The whole document's logical pages are
    # available to the DAO, and the table's real extent is derived below by
    # following continuations -- a caller naming page 1 of a two-page table
    # must not be able to obtain a verified page-1-only receipt.
    seed_pages = _parse_logical_pages(str(args.page))
    if not seed_pages:
        print("BLOCKED: --page resolved to no seed page")
        return 1
    missing = [page for page in seed_pages if page not in pages]
    if missing:
        print(f"BLOCKED: seed page(s) {missing} are not in {doc_id}'s "
              "registered source-text revision")
        return 1

    # --- 3. the selector picks ONE starting candidate ----------------------
    anchor = args.anchor
    seed_candidates = [
        candidate for candidate in candidates
        if candidate.get("logical_page") in seed_pages
        and _candidate_matches_anchor(candidate, anchor)
    ]
    if not seed_candidates:
        detected_here = [c for c in candidates
                         if c.get("logical_page") in seed_pages]
        print(f"BLOCKED: no detected table on logical page(s) {seed_pages} "
              f"matches anchor {anchor!r}. {len(detected_here)} table "
              "candidate(s) were detected there. A page whose table cannot be "
              "established by the layout detector is NOT a page without a "
              "table -- it is an unverified one, and it stays "
              "review_required.")
        return 1
    if len(seed_candidates) > 1:
        print(f"REFUSED: anchor {anchor!r} matches more than one detected "
              f"table on logical page(s) {seed_pages} -- "
              f"{len(seed_candidates)} candidates. Selecting the first would "
              "be a coin flip recorded as provenance; narrow the anchor or "
              "resolve the table by review.")
        return 1

    # --- 3b. the DAO decides where the table ENDS --------------------------
    by_page, scope_error = _derive_table_scope(
        seed_candidates[0], candidates)
    if scope_error:
        print(f"BLOCKED: the extent of the table on logical page "
              f"{seed_candidates[0].get('logical_page')} could not be "
              "established; NOTHING was written:")
        print(f"  - {scope_error}")
        print("  a table whose continuation cannot be resolved is refused "
              "rather than issued as a partial extent -- a page-1-only "
              "receipt for a table that continues would hide every row on the "
              "following page. Resolve by review.")
        return 1
    logical_pages = sorted(by_page)

    # --- 4. bind every band to exact offsets -------------------------------
    extent, regions, bind_errors = _derive_table_regions(
        by_page, pages, physical_for, profile)
    if bind_errors:
        print(f"BLOCKED: the table region for {doc_id} could not be bound to "
              "the registered source text; NOTHING was written:")
        for error in bind_errors:
            print(f"  - {error}")
        print("  an exact, unique mapping between the PDF's layout and the "
              "processed text is required -- an approximate region is "
              "indistinguishable from a correct one downstream. Resolve by "
              "review.")
        return 1

    review_reasons = table_region_provenance.classification_review_reasons(
        regions)
    if review_reasons:
        print(f"BLOCKED: the derived region for {doc_id} contains bands the "
              "detector could not classify; NOTHING was written:")
        for reason in review_reasons:
            print(f"  - {reason}")
        return 1

    # --- 5. build the receipt; an identical one is a no-op -----------------
    inventory = _scan_inventory_from(case_id, doc_id, prepared)

    def _candidate_id_of(candidate):
        return table_region_provenance.compute_candidate_id(
            document_id=doc_id,
            source_pdf_sha256=actual_pdf_digest,
            source_text_revision_sha256=revision_sha,
            detector_profile=detector["profile"],
            config_fingerprint=detector["config_fingerprint"],
            pages_geometry=(
                candidate["logical_page"], candidate["physical_page"],
                tuple(round(float(v), 2) for v in candidate["bbox"])),
        )

    # A multi-page table is ONE receipt but appears in the inventory once per
    # page it occupies. All of them are recorded as consumed, or coverage
    # would block on the continuation pages of every legitimate multi-page
    # table -- a false positive as harmful as the omission it looks for.
    covered_candidate_ids = sorted(
        _candidate_id_of(by_page[logical]) for logical in sorted(by_page))
    candidate_id = _candidate_id_of(by_page[min(by_page)])

    receipt = table_region_provenance.build_receipt(
        document_id=doc_id,
        case_id=case_id,
        candidate_id=candidate_id,
        covered_candidate_ids=covered_candidate_ids,
        source_pdf_sha256=actual_pdf_digest,
        source_text_revision_sha256=revision_sha,
        detector=detector,
        extent=extent,
        regions=regions,
        selector={"anchor": anchor, "logical_pages": logical_pages},
        segment_derivation_receipt_id=prepared["segment_receipt_id"],
        issued_at=now_iso(),
        issued_by=args.held_by,
        run_id=args.run_id,
    )

    existing = next(
        (r for r in table_region_receipts_for(case_id, doc_id)
         if r.get("receipt_id") == receipt.get("receipt_id")), None)
    existing_inventory = table_candidate_inventory_for(case_id, doc_id) or {}
    if existing is not None and existing_inventory.get("scan_id") == \
            inventory["scan_id"]:
        # Same PDF bytes, same revision, same detector output, same scan.
        # Nothing changed, so nothing downstream may be invalidated --
        # re-running a registration must not cost a rerun.
        print(f"PASS: {doc_id} table region receipt is unchanged (no-op) -- "
              f"{len(extent)} page(s), "
              f"{len([r for r in regions if r['kind'] == 'data_row'])} data "
              f"rows, receipt {receipt['receipt_id'][:16]}")
        return 0

    return _commit_table_region(case_id, doc_id, receipt, inventory,
                                args.held_by, args.run_id)


def _commit_table_region(case_id, doc_id, receipt, inventory, held_by, run_id):
    """Write the receipt and/or scan, plus the downstream invalidation, as one
    fail-closed transaction.

    `receipt` is None for a scan-only commit (`scan-table-candidates`): the
    inventory changes what finalization must account for, so it invalidates
    downstream work exactly as a receipt does, and goes through the identical
    locked, journalled, validate-before-write path rather than a lighter one.

    Locks are taken in dao_transaction.LOCK_ORDER and asserted rather than
    assumed. The prospective index is schema-validated BEFORE anything is
    written, so a schema failure cannot leave a partial receipt behind.
    """
    lock_kinds = ("run_state", "revision_index")
    order_errors = dao_transaction.check_lock_order(lock_kinds)
    if order_errors:
        for error in order_errors:
            print(f"FAIL: {error}")
        return 1

    case_directory = case_dir(case_id)
    state_target = run_state_path(case_id)
    index_target = table_region_index_path(case_id)
    index_preimage: bytes | None = None

    held: list[Path] = []
    try:
        action = ("register table region" if receipt is not None
                  else "scan table candidates")
        for target, purpose in (
            (state_target, f"{action} for {doc_id}"),
            (index_target, f"{action} for {doc_id}"),
        ):
            existing_lock = acquire_lock_blocking(
                target, held_by, run_id or "unknown", purpose)
            if existing_lock is not None:
                print(f"LOCKED: held_by={existing_lock['held_by']} "
                      f"run_id={existing_lock['run_id']} on {target.name} -- "
                      "the receipt and the downstream invalidation must land "
                      "together, so a contended lock aborts the whole "
                      "registration rather than writing part of it")
                return 1
            held.append(target)

        # First guaranteed-fresh reads: everything above ran before the locks.
        # The inventory carries the same two digests as a receipt, so a
        # scan-only commit is checked against exactly the same moving world.
        derived = receipt if receipt is not None else inventory
        fresh_revision = (revision_entry_for(case_id, doc_id) or {}).get(
            "current_revision_sha256")
        if fresh_revision != derived.get("source_text_revision_sha256"):
            print(f"FAIL: {doc_id} source-text revision changed while its "
                  "table regions were being derived; rerun from fresh state")
            return 1
        fresh_pdf = registered_source_pdf_sha256(
            case_id, _table_region_pdf_owner(case_id, doc_id))
        if fresh_pdf != derived.get("source_pdf_sha256"):
            print(f"FAIL: {doc_id}'s raw source bytes changed while its table "
                  "regions were being derived; rerun from fresh state")
            return 1

        index = load_table_region_index(case_id)
        tables = list(index.get("tables", []))
        if receipt is not None:
            tables = [t for t in tables
                      if t.get("receipt_id") != receipt.get("receipt_id")]
            # Superseded receipts are KEPT: they are the audit record of what
            # was once derived, and P0-8's follow-up scopes currency checking
            # to the receipts a contract actually cites, so history no longer
            # blocks.
            tables.append(receipt)
        tables.sort(key=lambda t: (t.get("document_id") or "",
                                   t.get("receipt_id") or ""))
        index["tables"] = tables
        # The inventory is a scan of the document as it is NOW, so it replaces
        # rather than accumulates -- an inventory listing candidates from an
        # older revision would block on tables that no longer exist.
        inventories = [i for i in index.get("candidates", [])
                       if i.get("document_id") != doc_id]
        inventories.append(inventory)
        inventories.sort(key=lambda i: i.get("document_id") or "")
        index["candidates"] = inventories
        index["case_id"] = case_id

        # Step 6: validate before anything lands.
        index_errors = _schema_check(index, table_region_provenance.INDEX_SCHEMA)
        if index_errors:
            print("FAIL: the table region receipt would be schema-invalid -- "
                  "nothing written:")
            for error in index_errors:
                print(f"  - {error}")
            return 1

        index_preimage = (
            index_target.read_bytes() if index_target.exists() else None)

        # Step 7: journal, then invalidate. Nothing irreversible yet.
        dao_transaction.write_journal(case_directory, {
            "operation": ("register_table_region" if receipt is not None
                          else "scan_table_candidates"),
            "case_id": case_id,
            "document_id": doc_id,
            "receipt_id": (receipt.get("receipt_id") if receipt is not None
                           else inventory.get("scan_id")),
            "status": "invalidating",
            "started_at": now_iso(),
        })
        try:
            _invalidate_policy_layer(
                case_id,
                (f"table region re-derived for {doc_id}: a reference table's "
                 "source regions must be re-bound to the new receipt")
                if receipt is not None else
                (f"table candidates re-scanned for {doc_id}: which tables the "
                 "document must account for has changed"),
                held_by, run_id, lock_already_held=True)
        except CascadeFailed as exc:
            dao_transaction.clear_journal(case_directory)
            print(f"FAIL: {doc_id} table region index NOT updated -- {exc}")
            print("  the existing receipt index is unchanged")
            return 1

        try:
            atomic_write_json(index_target, index)
        except Exception as exc:  # noqa: BLE001
            rollback_error = None
            try:
                _restore_file_preimage(index_target, index_preimage)
                dao_transaction.clear_journal(case_directory)
            except Exception as restore_exc:  # noqa: BLE001
                rollback_error = restore_exc
            print(f"FAIL: table region persistence failed: {exc}")
            if rollback_error is None:
                print("  the receipt index was restored to its exact "
                      "pre-transaction bytes; downstream remains "
                      "conservatively invalidated")
            else:
                print("  ROLLBACK INCOMPLETE: the transaction journal remains "
                      f"pending and blocks further work: {rollback_error}")
            return 1

        try:
            dao_transaction.clear_journal(case_directory)
        except Exception as exc:  # noqa: BLE001
            print("FAIL: the table region index was committed and downstream "
                  "was invalidated, but the transaction journal could not be "
                  f"cleared: {exc}")
            print("  the pending journal intentionally blocks further work; "
                  "do not delete it without inspecting the index")
            return 1

        if receipt is None:
            return 0  # scan-only; the caller reports the candidate list
        data_rows = [r for r in receipt["regions"]
                     if r["kind"] == table_region_provenance.DATA_ROW_KIND]
        print(f"PASS: issued {table_region_provenance.RECEIPT_SCHEME} receipt "
              f"{receipt['receipt_id'][:16]} for {doc_id} -- "
              f"{len(receipt['extent'])} page(s), {len(data_rows)} data rows "
              f"derived from {receipt['source_pdf_sha256'][:12]}")
        for region in receipt["extent"]:
            print(f"  logical page {region['page']} (physical "
                  f"{region['physical_page']}): extent "
                  f"[{region['start_char']}:{region['end_char']}]")
        print(f"  bound to source revision "
              f"{receipt['source_text_revision_sha256'][:12]}")
        return 0
    finally:
        for target in reversed(held):
            release_lock(target)


def cmd_read_table_region_index(args):
    print(json.dumps(load_table_region_index(args.case_id),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_read_revision_index(args):
    print(json.dumps(load_revision_index(args.case_id),
                     ensure_ascii=False, indent=2))
    return 0


@traced_read("dao.policy_snapshot")
def cmd_policy_snapshot(args):
    """Print the upstream_policy_snapshot value for the given documents.

    An agent writing a policy-referencing contract needs the exact digests the
    DAO will recompute at write time; making it derive them by hand would mean
    reading `outputs/` directly, which P2 forbids. So the DAO computes and
    prints them, and still verifies at write time -- printing here is a
    convenience, never the authority.
    """
    doc_ids = sorted(set(args.document_id))
    manifest = read_contract_data(args.case_id, "document_manifest.json") or {}
    entries = {
        d.get("document_id"): d for d in manifest.get("documents", [])}

    # What a snapshot requires is PROCESSED TEXT, not a normalized contract.
    # It used to require the contract, which silently made normalization
    # mandatory for anyone who merely CITES a policy document: with
    # normalization opt-IN (`text_only_no_normalization`), that gate blocked
    # claim-analysis and denial-response from citing a perfectly well-processed
    # policy document, for want of an artifact nothing downstream reads. The
    # digest itself never needed it -- `_policy_audit_context` hashes a missing
    # normalized contract as JSON null, which is a real, distinguishable state
    # ("no contract exists") and not a hole. What genuinely cannot be cited is
    # a document whose text the pipeline never extracted.
    blocked = []
    for doc_id in doc_ids:
        entry = entries.get(doc_id)
        if entry is None:
            blocked.append(f"{doc_id}: not in document_manifest.json")
            continue
        disposition = entry.get("downstream_disposition")
        if disposition not in policy_completeness._TEXT_PROCESSED:
            blocked.append(
                f"{doc_id}: downstream_disposition is {disposition!r} -- only a "
                "text-processed document may be cited; expert_review_only and "
                "superseded_bundle are excluded from text extraction entirely")
    if blocked:
        print("BLOCKED: a snapshot may only be issued for documents whose text "
              "the pipeline actually processed:\n"
              + "\n".join(f"  - {reason}" for reason in blocked))
        return 1

    snapshot = policy_snapshot_for(args.case_id, doc_ids)
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    # Advisory, on stderr so it never contaminates the JSON an agent pastes in
    # verbatim. Citing a non-normalized document is a supported path, not a
    # warning -- but which documents were normalized changes what the citation
    # can be addressed BY (canonical clause_uid vs document/page/quote), and
    # that is worth stating at the point of use.
    plain = [
        doc_id for doc_id in doc_ids
        if read_contract_data(
            args.case_id, f"normalized_policy_clause_{doc_id}.json") is None
    ]
    if plain:
        print(
            f"NOTE: {', '.join(plain)} has no normalized clause contract "
            "(normalization is opt-in). The snapshot is valid and records that "
            "absence as a real state. Address clauses in these documents by "
            "document_id + page + verbatim quote, resolved against "
            "policy_boundary_inventory_{DOC}.json and the processed text -- "
            "canonical clause_uid exists only for normalized documents. If this "
            "case actually disputes one, promote it with "
            "`dao.py promote-policy-document`.", file=sys.stderr)
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
    elif kind == "classification_review":
        artifact_name = f"classification_result_{args.artifact_id}.json"
        schema_name = "classification_result.schema.json"
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
    elif kind == "classification_review":
        # The only reviewable condition on a classification is the one that
        # raised review_required in the first place. Pinning the target key to
        # it means a record cannot be filed against a document that never
        # needed review, and cannot silently stand in for some other concern.
        if args.target_key != "classification_text_source:raw_page_text":
            print("FAIL: target_key for classification_review must be "
                  "'classification_text_source:raw_page_text' -- that is the "
                  "condition this review kind exists to clear")
            return 1
        if artifact.get("classification_text_source") != "raw_page_text":
            print(f"FAIL: {artifact_name} has classification_text_source="
                  f"{artifact.get('classification_text_source')!r}, not "
                  "'raw_page_text' -- there is no pre-redaction reading to "
                  "review. Do not file a review for a condition that did not "
                  "occur.")
            return 1
        if not artifact.get("review_required"):
            print(f"FAIL: {artifact_name} does not have review_required set -- "
                  "nothing is awaiting review")
            return 1
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


# ------------------------------------------------------------ trace rollup --

TIMING_SUMMARY_FILENAME = "_timing_summary.json"
TIMING_SUMMARY_SCHEMA = "timing_summary.schema.json"

# The stage whose successful finalize closes the SLA window. draft_report_v1,
# not critic_v1: what the practice actually delivers is the report, and critic
# is review rather than production (plan section 2.1, user decision
# 2026-08-10). critic_v1 still RUNS -- evaluation depends on it -- it is simply
# outside the measured window.
SLA_END_STAGE = "draft_report_v1"


def _emit_stage_attempt_marker(case_id: str, run_id: str | None, stage: str,
                               attempt: int | None, kind: str,
                               outcome: str | None = None) -> bool:
    """Emit one idempotent stage-attempt lifecycle event.

    The governed run-state transition is committed first. This trace event is
    diagnostic and may be missing after a crash; it never repairs itself from
    run-state timestamps, because those are marker-movement times rather than
    measured work boundaries. A per-attempt O_EXCL stamp prevents a replayed
    command from emitting a second marker for the same lifecycle edge.
    """
    if not run_id or not isinstance(attempt, int) or attempt < 1:
        return False
    if kind not in {"start", "end", "abandoned"}:
        return False
    try:
        trace_mod.configure(case_id, run_id, root=OUTPUTS)
        if not trace_mod.enabled():
            return False
        marker_dir = trace_spans_dir(case_id, run_id).parent / "markers"
        marker_dir.mkdir(parents=True, exist_ok=True)
        stamp = marker_dir / f"stage.attempt.{kind}.{stage}.{attempt}.json"
        try:
            fd = os.open(stamp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        op = f"stage.attempt.{kind}"
        marker_kind = f"stage_attempt_{kind}"
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({
                "op": op, "case_id": case_id, "run_id": run_id,
                "stage_name": stage, "attempt": attempt,
                "attempt_outcome": outcome, "emitted_at": now_iso(),
            }, fh, ensure_ascii=False)
        attrs = {"marker_kind": marker_kind, "stage_name": stage}
        if outcome is not None:
            attrs["attempt_outcome"] = outcome
        trace_mod.event(
            op, category="marker", case_id=case_id, attempt=attempt,
            status="ok" if outcome in (None, "passed") else "error",
            **attrs)
        return True
    except Exception:  # noqa: BLE001 -- diagnostics must never break a run
        return False


def _emit_human_wait_span(case_id: str, run_id: str | None, stage: str,
                          requested_at: str | None, received_at: str | None) -> bool:
    """Record a closed human-input wait so active_s can deduct it.

    P7 already stores requested_at/received_at in _run_state.json, so the
    interval is known exactly -- this only carries it into the trace, where
    compute_sla subtracts human_wait from the SLA wall clock. Without it the
    subtraction is real but always zero, and a "30 minute" figure quietly
    includes however long a person took to answer.

    Never raises, for the same reason as _emit_sla_marker: a failure to record
    a measurement must not fail the pipeline operation that triggered it.
    """
    if not (run_id and requested_at and received_at):
        return False
    try:
        start = datetime.fromisoformat(requested_at)
        end = datetime.fromisoformat(received_at)
        waited = (end - start).total_seconds()
        if waited < 0:
            return False
        trace_mod.configure(case_id, run_id)
        if not trace_mod.enabled():
            return False
        trace_mod.closed_interval(
            "human.gate", category="human_wait", t_start_wall=requested_at,
            duration_s=waited, case_id=case_id,
            gate_kind=stage, waited_s=round(waited, 3))
        return True
    except Exception:  # noqa: BLE001 -- diagnostics must never break a run
        return False


def _emit_sla_marker(case_id: str, run_id: str | None, op: str) -> bool:
    """Emit an SLA boundary marker exactly once per (case, run).

    Idempotence is the whole contract here. `check-source-ledger-clear` is a
    query an agent may legitimately call several times, and a stage can be
    finalized again after a rerun -- but a window with two starts has no
    defined width. The marker file is the record of "already emitted"; it sits
    beside the shards under _trace/ because it describes the trace, not the
    case, and like the shards it is excluded from P10 snapshots.

    Returns True if this call emitted the marker. Never raises: a failure to
    record a measurement must not fail the pipeline operation that triggered
    it, so every error path here degrades to "no marker" and the run proceeds.
    """
    if not run_id:
        # Nothing to scope the marker to. A run without an id cannot be
        # aggregated anyway -- aggregate-trace is keyed by run_id.
        return False
    try:
        trace_mod.configure(case_id, run_id)
        if not trace_mod.enabled():
            return False
        marker_dir = trace_spans_dir(case_id, run_id).parent / "markers"
        marker_dir.mkdir(parents=True, exist_ok=True)
        stamp = marker_dir / f"{op}.json"
        # O_EXCL, not exists()-then-write: two concurrent callers must not both
        # conclude they were first. Same atomic-create reasoning as the lock
        # primitive, for the same reason.
        try:
            fd = os.open(stamp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"op": op, "case_id": case_id, "run_id": run_id,
                       "emitted_at": now_iso()}, fh, ensure_ascii=False)
        trace_mod.event(op, category="marker", case_id=case_id,
                        marker_kind="sla_start" if op.endswith("start") else "sla_end")
        return True
    except Exception:  # noqa: BLE001 -- diagnostics must never break a run
        return False


def trace_spans_dir(case_id: str, run_id: str) -> Path:
    """Where tools/trace.py's shards land for one run.

    Built through _require_within like every other DAO path, so a crafted
    run_id cannot walk out of the case directory -- the shard tree is written
    outside the lock discipline, which makes its path the one part of it that
    still has to be checked here.
    """
    _require_safe_id("run_id", run_id)
    return _require_within(case_dir(case_id), "_trace", run_id, "spans")


def cmd_record_dispatch(args):
    """Record one closed subagent-dispatch interval into the run's trace.

    Closes the largest measurement hole this project has: a stage's marker
    window minus the agent's own reported duration left a residual that nothing
    explained. On CASE_142 that residual was 578.3s across nine stages, of
    which finalize and snapshot -- the obvious suspects -- accounted for 2.79s.
    The other 575.5s was split between dispatch round-trip (asking for the work
    and receiving the result) and operator-side work done while the marker
    happened to be open, and NOTHING on disk could tell the two apart.

    That distinction decides what a timing number means. Round-trip is a cost
    the pipeline pays structurally and can be engineered against; operator-side
    work is one person's working style and changes with the next person. Until
    they are separable, a per-stage figure cannot honestly be called a stage
    cost -- which is why this is recorded rather than estimated.

    Deliberately caller-declared, not inferred. The orchestrator is the only
    party that knows when it asked for work: the dispatch crosses a session
    boundary the DAO cannot observe, and a subagent's tool calls arrive in a
    process this one never forked. `--agent-reported-s` carries what the
    harness says the agent itself took, so the round trip is
    `duration_s - agent_reported_s` and stays visible instead of being folded
    into a single opaque number.

    A dispatch that is never recorded leaves the residual exactly as it is
    today: reported, unexplained, and honestly labelled. Missing data is not
    fabricated from marker times.
    """
    if args.duration_s < 0:
        print("REFUSED: --duration-s cannot be negative")
        return 1
    if args.agent_reported_s is not None:
        if args.agent_reported_s < 0:
            print("REFUSED: --agent-reported-s cannot be negative")
            return 1
        if args.agent_reported_s > args.duration_s:
            # The agent cannot have run longer than the dispatch that contains
            # it. Accepting this would produce a negative round trip, which
            # reads as a measurement rather than the mistake it is.
            print(f"REFUSED: --agent-reported-s ({args.agent_reported_s}) exceeds "
                  f"--duration-s ({args.duration_s}) -- the agent cannot outlast "
                  "the dispatch that contains it")
            return 1
    try:
        datetime.fromisoformat(args.started_at)
    except ValueError:
        print(f"REFUSED: --started-at is not an ISO-8601 timestamp: {args.started_at!r}")
        return 1

    trace_mod.configure(args.case_id, args.run_id, root=OUTPUTS)
    if not trace_mod.enabled():
        print("NOTE: tracing is off (HARNESS_TRACE=0) -- nothing recorded")
        return 0

    attrs = {"stage_name": args.stage}
    if args.agent_kind:
        attrs["agent_kind"] = args.agent_kind
    if args.agent_reported_s is not None:
        attrs["agent_reported_s"] = args.agent_reported_s
    if args.attempt is not None:
        attrs["attempt"] = args.attempt

    span_id = trace_mod.closed_interval(
        "dispatch.subagent", category="dispatch",
        t_start_wall=args.started_at, duration_s=args.duration_s,
        case_id=args.case_id,
        status="ok" if args.outcome == "completed" else "error",
        **attrs)
    if span_id is None:
        print("NOTE: tracing produced no span -- nothing recorded")
        return 0
    round_trip = (args.duration_s - args.agent_reported_s
                  if args.agent_reported_s is not None else None)
    print(f"OK: recorded dispatch for {args.stage} ({args.duration_s:.1f}s"
          + (f", round trip {round_trip:.1f}s)" if round_trip is not None else ")"))
    return 0


def cmd_aggregate_trace(args):
    """Roll the run's span shards up into _timing_summary.json.

    Runs exactly once, after the run's SLA end marker, so it contends with
    nothing -- which is what lets the shards themselves be written lock-free
    (see tools/trace.py). The write itself is an ordinary governed DAO write:
    locked, schema-validated, atomic, refusing to persist on a validation
    failure exactly like write-contract.
    """
    spans_dir = trace_spans_dir(args.case_id, args.run_id)
    if not spans_dir.exists():
        print(f"NO_TRACE: {spans_dir} does not exist -- nothing was recorded "
              f"for {args.case_id}/{args.run_id}. Was HARNESS_TRACE=0?")
        return 1

    spans, dropped_lines, shard_count = trace_aggregate.read_shards(spans_dir)
    if not spans:
        print(f"NO_TRACE: {spans_dir} holds no parseable spans "
              f"({dropped_lines} unparseable line(s) discarded)")
        return 1

    run_state = load_run_state(args.case_id)
    run_state_stages = (run_state.get("stages")
                        if run_state.get("run_id") == args.run_id else None)
    summary = trace_aggregate.summarize(
        spans, case_id=args.case_id, run_id=args.run_id,
        generated_at=now_iso(), dropped_span_lines=dropped_lines,
        shard_count=shard_count, input_class=args.input_class,
        cold_or_warm=args.cold_or_warm,
        run_state_stages=run_state_stages)

    target = _require_within(case_dir(args.case_id), TIMING_SUMMARY_FILENAME)
    existing_lock = acquire_lock_blocking(
        target, args.held_by, args.run_id, f"aggregate trace for {args.run_id}")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    try:
        errors = _schema_check(summary, TIMING_SUMMARY_SCHEMA)
        if errors:
            print(f"FAIL: schema validation errors for {target} -- not written:")
            for e in errors:
                print(f"  - {e}")
            return 1
        atomic_write_json(target, summary)
    finally:
        release_lock(target)

    print(f"PASS: wrote {target}")
    print(f"  spans={summary['span_count']} shards={shard_count} "
          f"dropped_lines={dropped_lines}")
    if summary.get("active_s") is None:
        # Without both markers there is no SLA window, so every duration below
        # is still a real measurement but none of them is an SLA verdict. Say
        # so rather than printing a number that reads like one.
        print("  active_s=n/a -- sla.phase1.start / sla.phase1.end markers not "
              "both present in this trace")
    else:
        print(f"  sla_wall_clock_s={summary['sla_wall_clock_s']} "
              f"human_wait_s={summary['human_wait_s']} "
              f"active_s={summary['active_s']}")
    coverage = summary.get("stage_coverage") or {}
    print(f"  stage_coverage={'complete' if coverage.get('coverage_complete') else 'incomplete'} "
          f"closed={coverage.get('complete_attempts', 0)} "
          f"open={coverage.get('open_attempts', 0)} "
          f"missing={coverage.get('missing_marker_attempts', 0)}")
    top = sorted(summary["by_category"].items(),
                 key=lambda kv: kv[1]["self_time_s"], reverse=True)[:5]
    for name, entry in top:
        print(f"  {name}: {entry['self_time_s']}s "
              f"({entry['share_of_total'] * 100:.1f}%, n={entry['count']})")
    return 0


def cmd_read_timing_summary(args):
    p = _require_within(case_dir(args.case_id), TIMING_SUMMARY_FILENAME)
    if not p.exists():
        print(f"NOT_FOUND: {p}")
        return 1
    print(p.read_text(encoding="utf-8"))
    return 0


DOCUMENT_INDEX_FILENAME = "_document_index.json"


@trace_mod.traced("dao.build_document_index", category="compute")
def cmd_build_document_index(args):
    """Regenerate the derived policy navigation index.

    Deliberately not a `write_contract` write. The index owes nothing: it is
    recomputable from processed text plus the registered PDF, no stage
    consumes it as a precondition, and no gate reads it. Routing it through
    the contract machinery would attach a schema obligation and a finalize
    dependency to an artifact whose entire value is that it has neither --
    which is the shape clause normalization died of (retired 2026-08-15): a
    gate on an artifact nothing could produce, bypassed rather than met.

    It still writes under the lock and through `_require_within`, because
    those protect the CASE DIRECTORY, not the contract semantics.
    """
    manifest = read_contract_data(args.case_id, "document_manifest.json")
    if manifest is None:
        print("BLOCKED: document_manifest.json does not exist")
        return 1
    index = document_index.build_index(
        args.case_id, manifest,
        raw_pdf_for=lambda doc_id: _raw_source_path(args.case_id, doc_id))
    target = _require_within(case_dir(args.case_id), DOCUMENT_INDEX_FILENAME)
    existing = acquire_lock_blocking(
        target, args.held_by, args.run_id, "build document index")
    if existing is not None:
        print(f"LOCKED: held_by={existing['held_by']}")
        return 1
    try:
        atomic_write_json(target, index)
    finally:
        release_lock(target)
    clauses = sum(len(d["clauses"]) for d in index["documents"])
    tables = sum(len(d["tables"]) for d in index["documents"])
    print(f"OK: {target}")
    print(f"  {len(index['documents'])} document(s), {clauses} clause "
          f"heading(s), {tables} table(s)")
    return 0


def cmd_read_document_index(args):
    p = _require_within(case_dir(args.case_id), DOCUMENT_INDEX_FILENAME)
    if not p.exists():
        print(f"NOT_FOUND: {p} -- run `dao.py build-document-index` "
              "(the index is derived; its absence blocks nothing)")
        return 1
    print(p.read_text(encoding="utf-8"))
    return 0


# ------------------------------------------------------------------- main --

def build_parser():
    """The CLI surface, separated from main() so tests can assert what the
    production command does and does not accept without running it."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("read-document-text"); p.add_argument("case_id"); p.add_argument("doc_id")
    p.add_argument("--run-id", help="Optional. Records this read's cost into the run's trace so it stops landing in unattributed stage time; without it the read still works and simply records nothing.")
    p.set_defaults(fn=cmd_read_document_text)

    p = sub.add_parser(
        "read-redacted-text-bundle",
        help="Return selected active documents' page-labelled redacted text and revision bindings as JSON. Never returns processed-layer paths.")
    p.add_argument("case_id")
    p.add_argument("--doc-id", required=True, action="append",
                   help="active processed document to include; repeat for each document")
    p.add_argument("--run-id", help="Optional trace attribution for this read.")
    p.set_defaults(fn=cmd_read_redacted_text_bundle)

    p = sub.add_parser(
        "verify-evidence-references",
        help="Verify candidate page/quote/range references against the current redacted text revision.")
    p.add_argument("case_id")
    p.add_argument("--references-file", required=True,
                   help="JSON object containing a non-empty references list")
    p.add_argument("--run-id", help="Optional trace attribution for this read.")
    p.set_defaults(fn=cmd_verify_evidence_references)

    p = sub.add_parser("read-driver-receipt",
                       help="Read one DAO-owned driver resume receipt.")
    p.add_argument("case_id")
    p.add_argument("--stage", required=True)
    p.add_argument("--unit-id", required=True)
    p.add_argument("--run-id", help="Optional trace attribution for this read.")
    p.set_defaults(fn=cmd_read_driver_receipt)

    p = sub.add_parser("write-driver-receipt",
                       help="Schema-validate and publish one DAO-owned driver resume receipt.")
    p.add_argument("case_id")
    p.add_argument("--stage", required=True)
    p.add_argument("--unit-id", required=True)
    p.add_argument("--data-file", required=True)
    p.add_argument("--held-by", required=True)
    p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_write_driver_receipt)

    p = sub.add_parser("read-driver-candidates",
                       help="Read every DAO-owned completed sub-unit of one driver stage unit.")
    p.add_argument("case_id")
    p.add_argument("--stage", required=True)
    p.add_argument("--unit-id", required=True)
    p.add_argument("--run-id", help="Optional trace attribution for this read.")
    p.set_defaults(fn=cmd_read_driver_candidates)

    p = sub.add_parser("write-driver-candidate",
                       help="Schema-validate and publish one DAO-owned driver sub-unit result.")
    p.add_argument("case_id")
    p.add_argument("--stage", required=True)
    p.add_argument("--unit-id", required=True)
    p.add_argument("--candidate-id", required=True)
    p.add_argument("--data-file", required=True)
    p.add_argument("--held-by", required=True)
    p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_write_driver_candidate)

    p = sub.add_parser("split-core-field-accuracy",
                       help="Recompute an evaluation_result's fact-extraction vs discretionary "
                            "sub-scores from its own field_comparisons. Read-only.")
    p.add_argument("case_id")
    p.add_argument("--filename", default="evaluation_result_v2.json",
                   help="Which evaluation_result file to read (default evaluation_result_v2.json).")
    p.set_defaults(fn=cmd_split_core_field_accuracy)

    p = sub.add_parser("check-untagged-claims",
                       help="Flag analytical-section lines that assert something but carry no "
                            "[E#] citation -- the shape read-evidence-tags cannot see.")
    p.add_argument("doc_path")
    p.add_argument("--template", required=True,
                   help="Registry key (e.g. 배상책임_후유장해형) whose analytical_heading_patterns "
                        "scope the scan.")
    p.set_defaults(fn=cmd_check_untagged_claims)

    p = sub.add_parser("search-document-text",
                       help="Whitespace/NFKC-insensitive search over processed text. Required "
                            "before asserting a term is ABSENT -- a raw search finds one spelling.")
    p.add_argument("case_id"); p.add_argument("doc_id", nargs="?")
    p.add_argument("term")
    p.add_argument("--all-docs", action="store_true",
                   help="Search every processed document in the case instead of one.")
    p.add_argument("--context", type=int, default=60,
                   help="Characters of surrounding source text per hit (default 60).")
    p.add_argument("--run-id", help="Optional. Records this read's cost into the run's trace so it stops landing in unattributed stage time; without it the read still works and simply records nothing.")
    p.set_defaults(fn=cmd_search_document_text)

    p = sub.add_parser("read-page-text"); p.add_argument("case_id"); p.add_argument("doc_id")
    p.add_argument("page", type=int)
    p.add_argument("--caller-stage", required=True,
                   help="Stage making the call. Pre-redaction text is restricted to "
                        f"{sorted(PAGE_TEXT_ALLOWED_STAGES)}; anything else is DENIED.")
    p.add_argument("--run-id", help="Optional. Records this read's cost into the run's trace so it stops landing in unattributed stage time; without it the read still works and simply records nothing.")
    p.set_defaults(fn=cmd_read_page_text)

    p = sub.add_parser("read-ground-truth"); p.add_argument("case_id"); p.add_argument("--caller-stage", required=True)
    p.add_argument("--version", required=True, choices=["v1", "v2"])
    p.add_argument("--file", help="Ground-truth file id (stem, e.g. GT_001). Prints its "
                                  "text content -- the sanctioned read path. Without this "
                                  "the command prints only the directory path, which no "
                                  "agent is permitted to open directly.")
    p.add_argument("--list", action="store_true",
                   help="List available ground-truth file ids, names and sizes.")
    p.add_argument("--transcribe", action="store_true",
                   help="For a SCANNED ground-truth PDF (no embedded text): render pages to a "
                        "temporary directory, vision-transcribe them, and return the text without "
                        "persisting anything. The pages are deleted on exit, so no answer-key "
                        "artifact is left on disk for a later stage to read.")
    p.set_defaults(fn=cmd_read_ground_truth)

    p = sub.add_parser("read-contract"); p.add_argument("case_id"); p.add_argument("filename")
    p.add_argument("--run-id", help="Optional. Records this read's cost into the run's trace so it stops landing in unattributed stage time; without it the read still works and simply records nothing.")
    p.set_defaults(fn=cmd_read_contract)

    p = sub.add_parser("check-segmentation-ready")
    p.add_argument("case_id"); p.add_argument("--doc-id")
    p.set_defaults(fn=cmd_check_segmentation_ready)

    p = sub.add_parser("set-segmentation-status")
    p.add_argument("case_id"); p.add_argument("doc_id")
    p.add_argument("status", choices=["required", "not_required"])
    p.add_argument("--reviewer", required=True); p.add_argument("--note")
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_set_segmentation_status)

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

    p = sub.add_parser("promote-policy-document")
    p.add_argument("case_id"); p.add_argument("doc_id")
    p.add_argument("--policy-processing-role", required=True,
                   choices=list(policy_roles.POLICY_ROLES),
                   help="what kind of policy processing this document owes once promoted")
    p.add_argument("--disputed-by", required=True,
                   help="the denial reason / policy_match that disputes this document (e.g. R04 / PM-2)")
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.add_argument("--purpose")
    p.set_defaults(fn=cmd_promote_policy_document)

    p = sub.add_parser("replace-manifest-documents")
    p.add_argument("case_id"); p.add_argument("bundle_id")
    p.add_argument("--bundle-fields-file", required=True, help="JSON object merged into the bundle's entry (e.g. downstream_disposition: superseded_bundle)")
    p.add_argument("--new-documents-file", required=True, help="JSON array of new per-document manifest entries to append")
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.add_argument("--purpose"); p.add_argument("--stage")
    p.set_defaults(fn=cmd_replace_manifest_documents)

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
    p.add_argument("--run-id", help="Optional. Records this read's cost into the run's trace so it stops landing in unattributed stage time; without it the read still works and simply records nothing.")
    p.set_defaults(fn=cmd_read_ledger)

    p = sub.add_parser("set-ledger-status")
    p.add_argument("case_id"); p.add_argument("file_name"); p.add_argument("status", choices=["pending", "approved", "rejected"])
    p.add_argument("--reviewer"); p.add_argument("--reason")
    # Optional so a pre-v0.4 ledger still works without one; a v0.4 ledger
    # refuses the write without it, because that is the id its history binds
    # the operation to. intake_case.py's own instructions already tell the
    # reviewer to pass this.
    p.add_argument("--operation-id", default=None,
                   help="Unique id for this approval, required for a v0.4 "
                        "(history-tracked) ledger. Re-running with the same id "
                        "and the same request is an idempotent no-op.")
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_set_ledger_status)

    p = sub.add_parser("check-source-ledger-clear"); p.add_argument("case_id")
    # Optional on purpose: this is a read-only query with existing callers, and
    # making it required would break them for the sake of a measurement. With a
    # run-id the clear result also opens the SLA window (plan B10); without
    # one, the check behaves exactly as before.
    p.add_argument("--run-id", default=None,
                   help="Records sla.phase1.start for this run when the ledger is clear.")
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
    p.add_argument(
        "--attempt-outcome",
        choices=["failed", "partial", "schema_failed", "finalize_refused",
                 "interrupted"],
        help="Closed diagnostic outcome for status=failed. interrupted leaves the attempt duration open.")
    p.set_defaults(fn=cmd_update_run_state)

    p = sub.add_parser("migrate-run-state-v03")
    p.add_argument("case_id"); p.add_argument("run_id")
    p.add_argument("--held-by", required=True)
    p.set_defaults(fn=cmd_migrate_run_state_v03)

    p = sub.add_parser(
        "reset-document-processing",
        help="Recoverably reset Stage 2 contracts/manifest/run-state for an operator-requested cold rerun.")
    p.add_argument("case_id")
    p.add_argument("new_run_id")
    p.add_argument("--held-by", required=True)
    p.add_argument(
        "--confirm-case-id", required=True,
        help="Destructive-scope confirmation; must exactly equal case_id.")
    p.set_defaults(fn=cmd_reset_document_processing)

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
    p.add_argument("--run-id", help="Optional. Records this read's cost into the run's trace so it stops landing in unattributed stage time; without it the read still works and simply records nothing.")
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
    p.add_argument("--operation-id", default=None,
                   help="Unique id binding this entry into the ledger history. "
                        "Re-running with the same id is an idempotent no-op.")
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_add_conflict_entry)

    p = sub.add_parser("set-conflict-verdict")
    p.add_argument("case_id"); p.add_argument("conflict_id")
    p.add_argument("verdict", choices=["resolved", "false_positive"]); p.add_argument("--note", required=True)
    p.add_argument("--operation-id", default=None,
                   help="Unique id binding this verdict into the ledger history. "
                        "Re-running with the same id is an idempotent no-op.")
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_set_conflict_verdict)

    p = sub.add_parser("check-conflicts-clear"); p.add_argument("case_id")
    p.add_argument("--run-id", help="Optional. Records this read's cost into the run's trace so it stops landing in unattributed stage time; without it the read still works and simply records nothing.")
    p.set_defaults(fn=cmd_check_conflicts_clear)

    # --- medical review (restored after merge 3569d50; see known-gaps 48) ---
    p = sub.add_parser("write-medical-variables")
    p.add_argument("case_id"); p.add_argument("data_file")
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.add_argument("--purpose")
    p.set_defaults(fn=cmd_write_medical_variables)

    p = sub.add_parser("read-medical-variables")
    p.add_argument("case_id"); p.add_argument("--revision-sha")
    p.set_defaults(fn=cmd_read_medical_variables)

    p = sub.add_parser("read-medical-evidence")
    p.add_argument("case_id"); p.add_argument("locator_id")
    p.set_defaults(fn=cmd_read_medical_evidence)

    p = sub.add_parser("check-medical-reviews-clear")
    p.add_argument("case_id")
    p.set_defaults(fn=cmd_check_medical_reviews_clear)

    p = sub.add_parser("reconcile-medical-review-waits")
    p.add_argument("case_id")
    p.add_argument("--operation-id", required=True)
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_reconcile_medical_review_waits)

    p = sub.add_parser("open-medical-review-item")
    p.add_argument("case_id"); p.add_argument("--issue-id", required=True)
    p.add_argument("--decision-owner", required=True, choices=["policy", "human"])
    p.add_argument("--operation-id", required=True)
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_open_medical_review_item)

    p = sub.add_parser("record-medical-referral-decision")
    p.add_argument("case_id"); p.add_argument("review_item_id")
    p.add_argument("decision_file")
    p.add_argument("--operation-id", required=True)
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_record_medical_referral_decision)

    p = sub.add_parser("provide-medical-review-information")
    p.add_argument("case_id"); p.add_argument("review_item_id")
    p.add_argument("--reason", required=True)
    p.add_argument("--operation-id", required=True)
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_provide_medical_review_information)

    p = sub.add_parser("transition-medical-review")
    p.add_argument("case_id"); p.add_argument("review_item_id")
    p.add_argument(
        "--action",
        required=True,
        choices=[
            "provide_information",
            "assign",
            "request_information",
            "supplement_package",
            "reassign",
            "submit_response",
            "amend_response",
            "withdraw_response",
            "flag_conflict",
            "adjudicate",
            "cancel",
            "close",
            "reopen",
        ],
    )
    p.add_argument("--data-file")
    p.add_argument("--reason")
    p.add_argument("--operation-id", required=True)
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_transition_medical_review)

    p = sub.add_parser("read-medical-review-ledger")
    p.add_argument("case_id")
    p.set_defaults(fn=cmd_read_medical_review_ledger)

    p = sub.add_parser("read-medical-review-evidence")
    p.add_argument("case_id"); p.add_argument("review_item_id")
    p.add_argument("request_id"); p.add_argument("request_version", type=int)
    p.add_argument("locator_id")
    p.set_defaults(fn=cmd_read_medical_review_evidence)

    p = sub.add_parser("read-medical-review-outcomes")
    p.add_argument("case_id"); p.add_argument("--caller-stage", required=True)
    p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_read_medical_review_outcomes)

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

    p = sub.add_parser(
        "register-segment-derivation",
        help="derive and record a segment's page_map from the registered "
             "parent PDF (the ONLY writer of page_map)")
    p.add_argument("case_id")
    p.add_argument("--doc-id", dest="doc_id", required=True,
                   help="the segment whose page map is being derived")
    p.add_argument("--parent-document-id", dest="parent_document_id",
                   default=None,
                   help="optional cross-check; the parent is read from the "
                        "manifest's source_document_id and a disagreement is "
                        "refused, not resolved in favour of this flag")
    p.add_argument("--pages", required=True,
                   help="logical page spec, e.g. '120-189' or '57,58,118,119'")
    p.add_argument("--page-offset", dest="page_offset", type=int, required=True,
                   help="CANDIDATE offset (physical = logical + offset). A "
                        "search hint only: every page must carry its own "
                        "printed-number confirmation, and a page that lacks "
                        "one blocks the registration")
    p.add_argument("--page-map-file", dest="page_map_file", default=None,
                   help="optional page_map to CROSS-CHECK; its values are "
                        "never stored, and a disagreement with what the parent "
                        "PDF shows refuses the registration")
    p.add_argument("--expect-parent-sha256", dest="expect_parent_sha256",
                   default=None,
                   help="optional: fail if the parent raw file does not hash "
                        "to this")
    p.add_argument("--held-by", dest="held_by", required=True)
    p.add_argument("--run-id", dest="run_id", required=True)
    p.set_defaults(fn=cmd_register_segment_derivation)

    p = sub.add_parser("read-segment-derivation-index")
    p.add_argument("case_id")
    p.set_defaults(fn=cmd_read_segment_derivation_index)

    p = sub.add_parser(
        "record-unverifiable-segment-derivation",
        help="P0-6 human-authorized override: record an ocr_segment's page "
             "map as unverified (no per-page confirmation is possible "
             "against an image-only parent PDF) instead of the real "
             "register-segment-derivation verification")
    p.add_argument("case_id")
    p.add_argument("--doc-id", dest="doc_id", required=True,
                   help="the segment this override is for")
    p.add_argument("--parent-document-id", dest="parent_document_id",
                   default=None,
                   help="optional cross-check against the manifest's "
                        "source_document_id (or sets it, if not yet "
                        "registered as a segment)")
    p.add_argument("--pages", required=True,
                   help="logical page spec, e.g. '120-189' or '57,58,118,119'")
    p.add_argument("--page-offset", dest="page_offset", type=int, required=True,
                   help="ASSERTED offset (physical = logical + offset). Not "
                        "verified against the parent's own pages -- this is "
                        "exactly the confirmation an ocr_segment cannot "
                        "obtain, which is why this call requires "
                        "--authorized-by and --reason")
    p.add_argument("--authorized-by", dest="authorized_by", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--held-by", dest="held_by", required=True)
    p.add_argument("--run-id", dest="run_id", required=True)
    p.set_defaults(fn=cmd_record_unverifiable_segment_derivation)

    p = sub.add_parser(
        "register-table-region",
        help="derive a table's authoritative source region from the "
             "registered PDF's own layout (the ONLY issuer of a "
             "table_region_v1 receipt)")
    p.add_argument("case_id")
    p.add_argument("--doc-id", dest="doc_id", required=True,
                   help="the document whose table region is being derived")
    p.add_argument("--page", required=True,
                   help="SEED only: the logical page where the table STARTS, "
                        "e.g. '12'. It does NOT define the scope -- the DAO "
                        "scans the document and follows continuations itself, "
                        "so a table spanning 12-13 is seeded with '12' and "
                        "comes back covering both. A caller cannot hide a "
                        "continuation page by naming fewer pages")
    p.add_argument("--anchor", default=None,
                   help="SELECTOR only: text that must appear in the "
                        "candidate table's header. It chooses among candidates "
                        "the DAO detected and never defines an extent; "
                        "matching two candidates, or none, issues no receipt")
    p.add_argument("--detector-profile", dest="detector_profile", default=None,
                   help=f"detection profile (default: "
                        f"{table_region_provenance.DEFAULT_DETECTOR_PROFILE}). "
                        "The profile name enters the receipt fingerprint, so "
                        "changing it requires a fresh derivation")
    p.add_argument("--held-by", dest="held_by", required=True)
    p.add_argument("--run-id", dest="run_id", required=True)
    p.set_defaults(fn=cmd_register_table_region)

    p = sub.add_parser(
        "scan-table-candidates",
        help="record every table the DAO's detector finds in a document -- "
             "the obligation a policy document carries whether or not anyone "
             "extracts a table from it")
    p.add_argument("case_id")
    p.add_argument("--doc-id", dest="doc_id", required=True,
                   help="the document to scan; every logical page it owns is "
                        "examined, and the scanned page list is recorded so an "
                        "unscanned page can never read as a table-free one")
    p.add_argument("--detector-profile", dest="detector_profile", default=None,
                   help=f"detection profile (default: "
                        f"{table_region_provenance.DEFAULT_DETECTOR_PROFILE}). "
                        "Enters the scan fingerprint, so reconfiguring it "
                        "makes existing scans stale")
    p.add_argument("--held-by", dest="held_by", required=True)
    p.add_argument("--run-id", dest="run_id", required=True)
    p.set_defaults(fn=cmd_scan_table_candidates)

    p = sub.add_parser(
        "record-unverifiable-table-scan",
        help="human-authorized P0-8 override for a document whose "
             "extraction_method is OCR and therefore has no deterministic "
             "text layer a real table scan could run against. Records "
             "scan_status=unverifiable_ocr_source (never "
             "complete_no_candidates) plus a required human_override -- this "
             "does not claim the document has no tables, only that a human "
             "explicitly authorized finalizing without a verified table-"
             "boundary scan. Refuses for any document whose extraction_method "
             "IS verifiable -- use scan-table-candidates for those instead.")
    p.add_argument("case_id")
    p.add_argument("--doc-id", dest="doc_id", required=True)
    p.add_argument("--authorized-by", dest="authorized_by", required=True,
                   help="the human who authorized this override, for the "
                        "permanent audit record")
    p.add_argument("--reason", dest="reason", required=True,
                   help="why proceeding without a verified table-boundary "
                        "scan was accepted for this document")
    p.add_argument("--held-by", dest="held_by", required=True)
    p.add_argument("--run-id", dest="run_id", required=True)
    p.set_defaults(fn=cmd_record_unverifiable_table_scan)

    p = sub.add_parser("read-table-region-index")
    p.add_argument("case_id")
    p.set_defaults(fn=cmd_read_table_region_index)

    p = sub.add_parser("policy-snapshot")
    p.add_argument("case_id")
    p.add_argument("--document-id", required=True, action="append",
                   help="referenced policy document; repeat for each one")
    p.add_argument("--run-id", help="Optional. Records this read's cost into the run's trace so it stops landing in unattributed stage time; without it the read still works and simply records nothing.")
    p.set_defaults(fn=cmd_policy_snapshot)

    p = sub.add_parser("aggregate-trace",
                       help="Roll this run's span shards up into _timing_summary.json. "
                            "Run once, after the SLA end marker.")
    p.add_argument("case_id")
    p.add_argument("--run-id", required=True)
    p.add_argument("--held-by", required=True)
    p.add_argument("--input-class", default=None,
                   choices=["S-min", "S", "M", "L", "XL", "embedded"],
                   help="Case size class, for comparing runs of like inputs.")
    p.add_argument("--cold-or-warm", default=None, choices=["cold", "warm"],
                   help="Whether resume caches were cleared first. Only cold "
                        "numbers are SLA-judgable.")
    p.set_defaults(fn=cmd_aggregate_trace)

    p = sub.add_parser("record-dispatch",
                       help="Record one closed subagent-dispatch interval, so a "
                            "stage's residual separates round-trip cost from "
                            "operator-side work.")
    p.add_argument("case_id")
    p.add_argument("--run-id", required=True)
    p.add_argument("--stage", required=True,
                   help="the stage this dispatch belongs to")
    p.add_argument("--started-at", required=True,
                   help="ISO-8601 wall time the dispatch was issued")
    p.add_argument("--duration-s", type=float, required=True,
                   help="whole dispatch, from asking for the work to holding "
                        "its result")
    p.add_argument("--agent-reported-s", type=float, default=None,
                   help="what the harness says the subagent itself took; the "
                        "difference from --duration-s is the round trip")
    p.add_argument("--agent-kind", default=None,
                   help="which agent identity was dispatched")
    p.add_argument("--attempt", type=int, default=None)
    p.add_argument("--outcome", default="completed",
                   choices=["completed", "failed", "interrupted"])
    p.set_defaults(fn=cmd_record_dispatch)

    p = sub.add_parser("read-timing-summary",
                       help="Read _timing_summary.json (read-contract's symmetric reader).")
    p.add_argument("case_id")
    p.set_defaults(fn=cmd_read_timing_summary)

    p = sub.add_parser("build-document-index",
                       help="Regenerate _document_index.json: clause headings "
                            "and recovered table structure for the case's "
                            "policy documents. Derived and advisory -- no "
                            "stage requires it and its absence blocks nothing.")
    p.add_argument("case_id")
    p.add_argument("--held-by", required=True)
    p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_build_document_index)

    p = sub.add_parser("read-document-index",
                       help="Read _document_index.json.")
    p.add_argument("case_id")
    p.add_argument("--run-id", default=None,
                   help="records this read's cost into the run's trace; "
                        "omitting it still works but records nothing")
    p.set_defaults(fn=cmd_read_document_index)

    p = sub.add_parser("record-human-review")
    p.add_argument("case_id")
    p.add_argument("--artifact-kind", required=True,
                   choices=["policy_audit_finding", "unpaged_physical_exclusion",
                            "classification_review"])
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

    return ap


def main():
    args = build_parser().parse_args()
    # Switch tracing on for the CLI process too. dao.py already called
    # configure() deep inside _emit_sla_marker, which was enough for the SLA
    # markers and hid the fact that every OTHER dao subcommand ran untraced:
    # `finalize-stage` emitted no dao.snapshot span at all, so the P10 snapshot
    # cost -- the plan's B5 -- stayed unmeasured while looking instrumented.
    # In-process callers (run_checkpoint1, redact_document) configure for
    # themselves and are unaffected; this covers the subprocess path.
    trace_mod.configure_from_args(args)
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
