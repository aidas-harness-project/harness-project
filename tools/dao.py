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
import stat
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from _validation import load_registry, validate_instance
import medical_repository

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs"
DATA = ROOT / "data"
FORBIDDEN_TEMPLATE = ROOT / "templates" / "forbidden-expressions.md"
MEDICAL_STRUCTURING_CONFIG = (
    ROOT / "config" / "medical" / "medical_structuring_v0.1.json"
)
MEDICAL_PROJECTION_CONFIG = (
    ROOT / "config" / "medical" / "medical_projection_v0.1.json"
)
MEDICAL_REVIEW_ROLE_CONFIG = (
    ROOT / "config" / "medical" / "medical_review_roles_v0.1.json"
)
MEDICAL_REVIEW_REQUEST_CONFIG = (
    ROOT / "config" / "medical" / "medical_review_request_v0.1.json"
)
MEDICAL_REFERRAL_POLICY = (
    ROOT / "config" / "medical" / "medical_referral_policy_v0.1.json"
)
KST = timezone(timedelta(hours=9))


class AtomicWriteCommittedError(OSError):
    """The destination was replaced, but directory durability was not confirmed."""


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
    atomic_write_bytes(
        path,
        json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def atomic_create_json(path: Path, obj) -> bool:
    """Durably create a JSON file once; return False if it already exists."""
    return atomic_create_bytes(
        path,
        json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def atomic_create_bytes(path: Path, content: bytes) -> bool:
    """Durably create exact bytes once without following an existing symlink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink():
            raise ValueError(f"refusing existing symlink at managed path: {path}")
        return False
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return True


def atomic_create_bytes_in_directory(
    directory: Path,
    filename: str,
    content: bytes,
) -> bool:
    """Create or compare exact bytes relative to one no-follow directory handle."""
    if not filename or Path(filename).name != filename:
        raise ValueError(f"unsafe descriptor-relative filename: {filename!r}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory_fd = os.open(directory, flags)
    except FileNotFoundError:
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        try:
            directory_fd = os.open(directory, flags)
        except OSError as exc:
            raise ValueError(
                f"cannot open managed directory without following links: {directory}"
            ) from exc
    except OSError as exc:
        raise ValueError(
            f"cannot open managed directory without following links: {directory}"
        ) from exc

    created = False
    created_identity = None
    try:
        directory_metadata = os.fstat(directory_fd)
        try:
            file_fd = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            try:
                file_fd = os.open(
                    filename,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
            except OSError as exc:
                raise ValueError(
                    f"cannot inspect existing managed file: {directory / filename}"
                ) from exc
            try:
                file_metadata = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(file_metadata.st_mode)
                    or file_metadata.st_nlink != 1
                ):
                    raise ValueError(
                        f"existing managed file is not a private regular file: "
                        f"{directory / filename}"
                    )
                with os.fdopen(file_fd, "rb") as stream:
                    file_fd = -1
                    existing = stream.read()
            finally:
                if file_fd >= 0:
                    os.close(file_fd)
            if existing != content:
                raise ValueError(
                    f"existing managed file content mismatch: {directory / filename}"
                )
        except OSError as exc:
            raise ValueError(
                f"cannot create managed file: {directory / filename}"
            ) from exc
        else:
            try:
                created_metadata = os.fstat(file_fd)
            except BaseException:
                os.close(file_fd)
                raise
            created = True
            created_identity = (created_metadata.st_dev, created_metadata.st_ino)
            with os.fdopen(file_fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory_fd)

        try:
            path_metadata = directory.lstat()
        except OSError as exc:
            raise ValueError(
                f"managed directory identity changed during publication: {directory}"
            ) from exc
        if (
            not stat.S_ISDIR(path_metadata.st_mode)
            or path_metadata.st_dev != directory_metadata.st_dev
            or path_metadata.st_ino != directory_metadata.st_ino
        ):
            raise ValueError(
                f"managed directory identity changed during publication: {directory}"
            )
        try:
            verification_fd = os.open(directory, flags)
        except OSError as exc:
            raise ValueError(
                f"managed directory identity changed during publication: {directory}"
            ) from exc
        try:
            verification_metadata = os.fstat(verification_fd)
        finally:
            os.close(verification_fd)
        if (
            verification_metadata.st_dev != directory_metadata.st_dev
            or verification_metadata.st_ino != directory_metadata.st_ino
        ):
            raise ValueError(
                f"managed directory identity changed during publication: {directory}"
            )
        return created
    except BaseException:
        if created and created_identity is not None:
            try:
                current_fd = os.open(
                    filename,
                    os.O_PATH | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
            except OSError:
                pass
            else:
                try:
                    try:
                        current_metadata = os.fstat(current_fd)
                    except OSError:
                        current_identity = None
                    else:
                        current_identity = (
                            current_metadata.st_dev,
                            current_metadata.st_ino,
                        )
                finally:
                    os.close(current_fd)
                if current_identity == created_identity:
                    try:
                        os.unlink(filename, dir_fd=directory_fd)
                        os.fsync(directory_fd)
                    except OSError:
                        pass
        raise
    finally:
        os.close(directory_fd)


def remove_managed_file_if_content(
    directory: Path,
    filename: str,
    expected: bytes,
) -> bool:
    """Remove one private regular child only while its exact bytes still match."""
    if not filename or Path(filename).name != filename:
        raise ValueError(f"unsafe descriptor-relative filename: {filename!r}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        directory_fd = os.open(directory, flags)
    except OSError:
        return False
    try:
        try:
            file_fd = os.open(
                filename,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
        except OSError:
            return False
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                return False
            with os.fdopen(file_fd, "rb") as stream:
                file_fd = -1
                existing = stream.read()
        finally:
            if file_fd >= 0:
                os.close(file_fd)
        if existing != expected:
            return False
        try:
            current = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            return False
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            return False
        try:
            os.unlink(filename, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except OSError:
            return False
        return True
    finally:
        os.close(directory_fd)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def _open_parent_directory_beneath(
    directory: Path,
    relative_path: str,
) -> tuple[int, str]:
    """Open a relative target's real parent without following any ancestor."""
    relative = Path(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("target path must be a safe relative path")
    directory.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd = os.open(directory, flags)
    try:
        for part in relative.parts[:-1]:
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                try:
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                except OSError as exc:
                    raise ValueError(
                        f"target ancestor is not a real directory: {part}"
                    ) from exc
            except OSError as exc:
                raise ValueError(
                    f"target ancestor is not a real directory: {part}"
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, relative.parts[-1]
    except BaseException:
        os.close(current_fd)
        raise


def atomic_write_bytes_beneath(
    directory: Path,
    relative_path: str,
    content: bytes,
    expected_parent_identity: tuple[int, int] | None = None,
) -> None:
    """Atomically write below a real directory without following ancestors."""
    current_fd, leaf = _open_parent_directory_beneath(directory, relative_path)
    try:
        current_metadata = os.fstat(current_fd)
        current_identity = (current_metadata.st_dev, current_metadata.st_ino)
        if (
            expected_parent_identity is not None
            and current_identity != expected_parent_identity
        ):
            raise ValueError("target parent identity changed after lock acquisition")
        temporary = f".{leaf}.tmp.{os.getpid()}.{time.time_ns()}"
        temporary_created = False
        try:
            temporary_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=current_fd,
            )
            temporary_created = True
            with os.fdopen(temporary_fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary,
                leaf,
                src_dir_fd=current_fd,
                dst_dir_fd=current_fd,
            )
            temporary_created = False
            os.fsync(current_fd)
            if expected_parent_identity is not None:
                verification_fd, verification_leaf = _open_parent_directory_beneath(
                    directory,
                    relative_path,
                )
                try:
                    verification_metadata = os.fstat(verification_fd)
                    verification_identity = (
                        verification_metadata.st_dev,
                        verification_metadata.st_ino,
                    )
                    if (
                        verification_leaf != leaf
                        or verification_identity != expected_parent_identity
                    ):
                        raise ValueError(
                            "target parent identity changed during generic write"
                        )
                finally:
                    os.close(verification_fd)
        finally:
            if temporary_created:
                try:
                    os.unlink(temporary, dir_fd=current_fd)
                except OSError:
                    pass
    finally:
        os.close(current_fd)


def atomic_write_text_beneath(
    directory: Path,
    relative_path: str,
    text: str,
    expected_parent_identity: tuple[int, int] | None = None,
) -> None:
    atomic_write_bytes_beneath(
        directory,
        relative_path,
        text.encode("utf-8"),
        expected_parent_identity,
    )


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Durably publish exact bytes without reformatting or a trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise AtomicWriteCommittedError(
                f"{path} was replaced but parent-directory durability was not confirmed: {exc}"
            ) from exc
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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


class _AnchoredLock:
    """Owned generic lock retained by parent descriptor and inode identity."""

    def __init__(self, parent_fd: int, name: str, identity: tuple[int, int]):
        self.parent_fd = parent_fd
        self.name = name
        self.identity = identity
        parent_metadata = os.fstat(parent_fd)
        self.parent_identity = (parent_metadata.st_dev, parent_metadata.st_ino)


def _read_lock_beneath(parent_fd: int, name: str) -> dict:
    placeholder = {
        "held_by": "unknown",
        "run_id": "unknown",
        "started_at": "unknown",
        "purpose": "already held",
    }
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent_fd,
        )
    except OSError:
        return placeholder
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            return placeholder
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            try:
                value = json.load(stream)
            except (json.JSONDecodeError, ValueError):
                return placeholder
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(value, dict):
        return placeholder
    return {**placeholder, **value}


def acquire_lock_beneath(
    directory: Path,
    relative_path: str,
    held_by: str,
    run_id: str,
    purpose: str,
) -> tuple[_AnchoredLock | None, dict | None]:
    """Acquire a compatible target lock without following target ancestors."""
    parent_fd, leaf = _open_parent_directory_beneath(directory, relative_path)
    name = leaf + ".lock"
    try:
        try:
            fd = os.open(
                name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                0o644,
                dir_fd=parent_fd,
            )
        except FileExistsError:
            existing = _read_lock_beneath(parent_fd, name)
            os.close(parent_fd)
            return None, existing
        try:
            metadata = os.fstat(fd)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = -1
                json.dump(
                    {
                        "held_by": held_by,
                        "run_id": run_id,
                        "started_at": now_iso(),
                        "purpose": purpose,
                    },
                    stream,
                    ensure_ascii=False,
                    indent=2,
                )
        finally:
            if fd >= 0:
                os.close(fd)
        return _AnchoredLock(
            parent_fd,
            name,
            (metadata.st_dev, metadata.st_ino),
        ), None
    except BaseException:
        os.close(parent_fd)
        raise


def acquire_lock_beneath_blocking(
    directory: Path,
    relative_path: str,
    held_by: str,
    run_id: str,
    purpose: str,
) -> tuple[_AnchoredLock | None, dict | None]:
    waited = 0.0
    while True:
        lock, existing = acquire_lock_beneath(
            directory,
            relative_path,
            held_by,
            run_id,
            purpose,
        )
        if lock is not None:
            return lock, None
        if waited >= LOCK_MAX_WAIT_SECONDS:
            return None, existing
        time.sleep(LOCK_POLL_INTERVAL_SECONDS)
        waited += LOCK_POLL_INTERVAL_SECONDS


def release_lock_beneath(lock: _AnchoredLock) -> None:
    """Release only the exact descriptor-owned lock created by this caller."""
    try:
        try:
            current = os.stat(
                lock.name,
                dir_fd=lock.parent_fd,
                follow_symlinks=False,
            )
        except OSError:
            return
        if (current.st_dev, current.st_ino) != lock.identity:
            return
        try:
            os.unlink(lock.name, dir_fd=lock.parent_fd)
        except OSError:
            pass
    finally:
        os.close(lock.parent_fd)


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


def _read_generic_contract_bytes(case_id: str, filename: str) -> bytes:
    path = _require_within(case_dir(case_id), filename)
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(
                f"{filename} is owned by a purpose-built medical DAO command"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def cmd_read_contract(args):
    try:
        normalized = medical_repository.normalized_case_relative_target(
            sys.modules[__name__], args.case_id, args.filename
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        medical_repository.require_generic_read_allowed(
            sys.modules[__name__], args.case_id, args.filename
        )
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 1
    if normalized == "extracted_claim_fields.json":
        data, error = medical_repository.load_projection(
            sys.modules[__name__], args.case_id
        )
        if error:
            print(f"FAIL: {error}")
            return 1
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if normalized == "medical_variables.json":
        data, error = _load_medical_revision(args.case_id, None)
        if error:
            print(f"FAIL: {error}")
            return 1
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if normalized == "_medical_review_ledger.json":
        try:
            data = load_medical_review_ledger(args.case_id)
        except ValueError as exc:
            print(f"FAIL: {exc}")
            return 1
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if Path(normalized).parts[:1] == ("_medical_variable_revisions",):
        print("FAIL: immutable medical revisions require read-medical-variables")
        return 1
    p = _require_within(case_dir(args.case_id), args.filename)
    if not p.exists():
        print(f"NOT_FOUND: {p}")
        return 1
    try:
        payload = _read_generic_contract_bytes(args.case_id, args.filename)
        text = payload.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    print(text)
    return 0


def read_contract_data(case_id: str, filename: str):
    """DAO-owned structured contract read for in-process pipeline tools."""
    normalized = medical_repository.normalized_case_relative_target(
        sys.modules[__name__], case_id, filename
    )
    medical_repository.require_generic_read_allowed(
        sys.modules[__name__], case_id, filename
    )
    if normalized == "extracted_claim_fields.json":
        data, error = medical_repository.load_projection(
            sys.modules[__name__], case_id
        )
        if error:
            raise ValueError(error)
        return data
    if normalized == "medical_variables.json":
        data, error = _load_medical_revision(case_id, None)
        if error:
            raise ValueError(error)
        return data
    if normalized == "_medical_review_ledger.json":
        return load_medical_review_ledger(case_id)
    if Path(normalized).parts[:1] == ("_medical_variable_revisions",):
        raise ValueError(
            "immutable medical revisions require read-medical-variables"
        )
    try:
        return json.loads(_read_generic_contract_bytes(case_id, filename))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"cannot read generic contract {filename}: {exc}") from exc


def medical_variables_path(case_id: str) -> Path:
    return medical_repository.medical_variables_path(sys.modules[__name__], case_id)


def medical_variable_revisions_dir(case_id: str) -> Path:
    return medical_repository.revisions_dir(sys.modules[__name__], case_id)


def _canonical_json_bytes(data: dict) -> bytes:
    return medical_repository.canonical_json_bytes(data)


def cmd_write_medical_variables(args):
    return medical_repository.publish(sys.modules[__name__], args)


def _load_medical_revision(
    case_id: str, revision_sha: str | None
) -> tuple[dict | None, str | None]:
    return medical_repository.load_revision(
        sys.modules[__name__], case_id, revision_sha
    )


def cmd_read_medical_variables(args):
    return medical_repository.cmd_read_variables(sys.modules[__name__], args)


def cmd_read_medical_evidence(args):
    return medical_repository.cmd_read_evidence(sys.modules[__name__], args)


def medical_review_ledger_path(case_id: str) -> Path:
    from medical_review_ledger import ledger_path

    return ledger_path(sys.modules[__name__], case_id)


def load_medical_review_ledger(case_id: str) -> dict:
    from medical_review_ledger import load_ledger

    return load_ledger(sys.modules[__name__], case_id)


def cmd_check_medical_reviews_clear(args):
    from medical_review_ledger import cmd_check_clear

    return cmd_check_clear(sys.modules[__name__], args)


def _run_medical_review_mutation(command, args):
    result = command(sys.modules[__name__], args)
    if result == 0:
        from medical_review_ledger import reconcile_wait_projection

        projected, _, error = reconcile_wait_projection(
            sys.modules[__name__], args
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
    state = load_run_state(args.case_id)
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


def cmd_write_contract(args):
    try:
        medical_repository.require_generic_target_allowed(
            sys.modules[__name__], args.case_id, args.filename
        )
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 1
    target = _require_within(case_dir(args.case_id), args.filename)
    try:
        owned_lock, existing_lock = acquire_lock_beneath_blocking(
            case_dir(args.case_id),
            args.filename,
            args.held_by,
            args.run_id,
            args.purpose or f"write {args.filename}",
        )
    except (OSError, ValueError) as exc:
        print(f"FAIL: unsafe generic lock target: {exc}")
        return 1
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    assert owned_lock is not None
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
        try:
            atomic_write_bytes_beneath(
                case_dir(args.case_id),
                args.filename,
                json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                owned_lock.parent_identity,
            )
        except (OSError, ValueError) as exc:
            print(f"FAIL: unsafe generic write target: {exc}")
            return 1
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
        release_lock_beneath(owned_lock)


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
    try:
        if not target.exists():
            return False, f"FAIL: no document_manifest.json for {case_id}"
        manifest = json.loads(target.read_text(encoding="utf-8"))
        doc = next((d for d in manifest["documents"] if d["document_id"] == document_id), None)
        if doc is None:
            return False, f"FAIL: document_id {document_id} not found in document_manifest.json"
        doc.update(fields)
        manifest["updated_at"] = now_iso()
        errors = _schema_check(manifest, "document_manifest.schema.json")
        if errors:
            return False, "FAIL: schema validation errors for " + str(target) + " -- not written:\n" + \
                "\n".join(f"  - {e}" for e in errors)
        atomic_write_json(target, manifest)
        if stage:
            state = _update_run_state(case_id, run_id, stage, "passed", held_by)
            if state is None:
                return True, f"PASS: patched {document_id} in {target}\n" \
                    "WARNING: patch succeeded, but run-state could not be updated (lock contention) -- " \
                    "run-state may now lag behind actual progress; retry the run-state update."
        return True, f"PASS: patched {document_id} in {target}"
    finally:
        release_lock(target)


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
    try:
        medical_repository.require_generic_target_allowed(
            sys.modules[__name__], case_id, filename
        )
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 1
    target = _require_within(case_dir(case_id), filename)
    try:
        owned_lock, existing_lock = acquire_lock_beneath_blocking(
            case_dir(case_id),
            filename,
            held_by,
            run_id,
            purpose or f"write {filename}",
        )
    except (OSError, ValueError) as exc:
        print(f"FAIL: unsafe generic lock target: {exc}")
        return 1
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    assert owned_lock is not None
    try:
        text = Path(text_file).read_text(encoding="utf-8")
        try:
            atomic_write_text_beneath(
                case_dir(case_id),
                filename,
                text,
                owned_lock.parent_identity,
            )
        except (OSError, ValueError) as exc:
            print(f"FAIL: unsafe generic write target: {exc}")
            return 1
        print(f"PASS: wrote {target}")
        return 0
    finally:
        release_lock_beneath(owned_lock)


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
        errors = _schema_check(state, "run_state.schema.json")
        if errors:
            print(f"FAIL: schema validation errors for {target} -- not written:")
            for e in errors:
                print(f"  - {e}")
            return None
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

    p = sub.add_parser("write-medical-variables")
    p.add_argument("case_id"); p.add_argument("data_file")
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.add_argument("--purpose")
    p.set_defaults(fn=cmd_write_medical_variables)

    p = sub.add_parser("read-medical-variables")
    p.add_argument("case_id"); p.add_argument("--revision-sha")
    p.set_defaults(fn=cmd_read_medical_variables)

    p = sub.add_parser("check-medical-reviews-clear")
    p.add_argument("case_id")
    p.set_defaults(fn=cmd_check_medical_reviews_clear)

    p = sub.add_parser("reconcile-medical-review-waits")
    p.add_argument("case_id")
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_reconcile_medical_review_waits)

    p = sub.add_parser("open-medical-review-item")
    p.add_argument("case_id"); p.add_argument("--issue-id", required=True)
    p.add_argument("--decision-owner", required=True, choices=["policy", "human"])
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_open_medical_review_item)

    p = sub.add_parser("record-medical-referral-decision")
    p.add_argument("case_id"); p.add_argument("review_item_id")
    p.add_argument("decision_file")
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_record_medical_referral_decision)

    p = sub.add_parser("provide-medical-review-information")
    p.add_argument("case_id"); p.add_argument("review_item_id")
    p.add_argument("--reason", required=True)
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
