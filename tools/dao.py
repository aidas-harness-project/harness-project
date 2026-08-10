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
    set-ledger-status CASE_ID FILE_NAME STATUS --operation-id OPERATION_ID --held-by NAME --run-id RUN_ID
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
        --operation-id OPERATION_ID --held-by NAME --run-id RUN_ID
    set-conflict-verdict CASE_ID CONFLICT_ID VERDICT --note TEXT
        --operation-id OPERATION_ID --held-by NAME --run-id RUN_ID
    check-conflicts-clear CASE_ID
"""
import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import sys
import threading
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
            created = True
            with os.fdopen(file_fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                created_identity = _owned_file_identity(os.fstat(stream.fileno()))
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
                        current_identity = _owned_file_identity(current_metadata)
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


def _safe_lock_descriptor(fd: int) -> bool:
    metadata = os.fstat(fd)
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and metadata.st_nlink == 1
    )


_KERNEL_LOCK_TOKENS_GUARD = threading.Lock()


class _KernelLockToken:
    """Non-filesystem kernel ownership for one logical lock coordinate.

    Linux abstract UNIX socket names cannot be unlinked or replaced through a
    pathname. The persistent ``.lock`` file remains a human-readable sidecar,
    while this token prevents a replacement sidecar from creating a second
    owner of the same logical lock.
    """

    def __init__(self, owner: socket.socket):
        self.owner = owner

    def close(self) -> None:
        with _KERNEL_LOCK_TOKENS_GUARD:
            owner = self.owner
            self.owner = None
            _KERNEL_LOCK_TOKENS.discard(self)
        if owner is not None:
            owner.close()

    def discard_after_fork(self) -> None:
        """Close a child duplicate without consulting an inherited guard."""
        if self.owner is not None:
            self.owner.close()
            self.owner = None


_KERNEL_LOCK_TOKENS = set()


def _kernel_lock_address(key: str) -> bytes:
    digest = hashlib.sha256(os.fsencode(key)).hexdigest().encode("ascii")
    return b"\0aidas-harness-lock-" + digest


def _try_kernel_lock(key: str) -> _KernelLockToken | None:
    with _KERNEL_LOCK_TOKENS_GUARD:
        flags = socket.SOCK_DGRAM | getattr(socket, "SOCK_CLOEXEC", 0)
        owner = socket.socket(socket.AF_UNIX, flags)
        try:
            owner.bind(_kernel_lock_address(key))
        except OSError as error:
            owner.close()
            if error.errno in {errno.EADDRINUSE, errno.EACCES}:
                return None
            raise
        token = _KernelLockToken(owner)
        _KERNEL_LOCK_TOKENS.add(token)
        return token


def _lock_key(path: Path) -> str:
    return str(path.absolute())


def _read_lock_metadata_path(path: Path) -> dict:
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return {"held_by": "unknown", "run_id": "unknown", "purpose": "active kernel lock"}
    try:
        if not _safe_lock_descriptor(fd):
            return {"held_by": "unknown", "run_id": "unknown", "purpose": "unsafe lock path"}
        return _read_lock_fd(fd)
    finally:
        os.close(fd)


def read_lock(target: Path):
    lp = lock_path(target)
    token = _try_kernel_lock(_lock_key(lp))
    if token is None:
        return _read_lock_metadata_path(lp)
    token.close()
    try:
        fd = os.open(lp, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        return {"held_by": "unknown", "run_id": "unknown", "purpose": "unsafe lock path"}
    try:
        if not _safe_lock_descriptor(fd):
            return {"held_by": "unknown", "run_id": "unknown", "purpose": "unsafe lock path"}
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _read_lock_fd(fd)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def _read_lock_fd(fd: int) -> dict:
    placeholder = {
        "held_by": "unknown",
        "run_id": "unknown",
        "purpose": "lock being written",
    }
    try:
        raw = os.pread(fd, 64 * 1024, 0)
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return placeholder
    return {**placeholder, **value} if isinstance(value, dict) else placeholder


class _GenericLockHandle:
    def __init__(self, fd: int, token: _KernelLockToken):
        self.fd = fd
        self.token = token
        self.owner_pid = os.getpid()
        self.owner_thread = threading.get_ident()


_LOCK_HANDLES: dict[tuple[int, int, str], list[_GenericLockHandle]] = {}
_LOCK_HANDLES_GUARD = threading.Lock()


def acquire_lock(target: Path, held_by: str, run_id: str, purpose: str):
    """Returns None on success, or the existing lock dict if already held.

    Uses a persistent regular file plus a kernel advisory lock. Release closes
    only the descriptor this process owns and never unlinks a pathname, so a
    replacement owner cannot be deleted between an identity check and unlink."""
    lp = lock_path(target)
    lp.parent.mkdir(parents=True, exist_ok=True)
    key = _lock_key(lp)
    token = _try_kernel_lock(key)
    if token is None:
        return _read_lock_metadata_path(lp)
    try:
        fd = os.open(
            lp,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o644,
        )
    except OSError:
        token.close()
        return {"held_by": "unknown", "run_id": "unknown", "purpose": "unsafe lock path"}
    if not _safe_lock_descriptor(fd):
        os.close(fd)
        token.close()
        return {"held_by": "unknown", "run_id": "unknown", "purpose": "unsafe lock path"}
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        existing = _read_lock_fd(fd)
        os.close(fd)
        token.close()
        return existing
    try:
        payload = json.dumps(
            {"held_by": held_by, "run_id": run_id, "started_at": now_iso(), "purpose": purpose},
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        os.ftruncate(fd, 0)
        os.pwrite(fd, payload, 0)
        os.fsync(fd)
    except BaseException:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        token.close()
        raise
    owner_key = (os.getpid(), threading.get_ident(), key)
    with _LOCK_HANDLES_GUARD:
        _LOCK_HANDLES.setdefault(owner_key, []).append(_GenericLockHandle(fd, token))
    return None


def release_lock(target: Path) -> None:
    key = (os.getpid(), threading.get_ident(), _lock_key(lock_path(target)))
    with _LOCK_HANDLES_GUARD:
        handles = _LOCK_HANDLES.get(key, [])
        handle = handles.pop() if handles else None
        if not handles:
            _LOCK_HANDLES.pop(key, None)
    if handle is not None:
        fcntl.flock(handle.fd, fcntl.LOCK_UN)
        os.close(handle.fd)
        handle.token.close()


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


def _owned_file_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    """Identity strong enough to reject a replacement that reuses an inode."""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_ctime_ns,
        metadata.st_size,
    )


class _OwnedPathLock:
    """Kernel-owned lock released only through its retained descriptor."""

    def __init__(self, target: Path, fd: int, token: _KernelLockToken):
        self.target = target
        self.fd = fd
        self.token = token
        self.owner_pid = os.getpid()
        self.released = False

    def discard_after_fork(self) -> None:
        if self.released:
            return
        os.close(self.fd)
        self.token.close()
        self.released = True


_OWNED_LOCK_HANDLES = set()


def acquire_owned_lock(
    target: Path,
    held_by: str,
    run_id: str,
    purpose: str,
) -> tuple[_OwnedPathLock | None, dict | None]:
    """Acquire a compatible persistent lock and retain its kernel ownership."""
    lp = lock_path(target)
    lp.parent.mkdir(parents=True, exist_ok=True)
    token = _try_kernel_lock(_lock_key(lp))
    if token is None:
        return None, _read_lock_metadata_path(lp)
    try:
        fd = os.open(
            lp,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o644,
        )
    except OSError:
        token.close()
        return None, {
            "held_by": "unknown",
            "run_id": "unknown",
            "purpose": "unsafe lock path",
        }
    try:
        if not _safe_lock_descriptor(fd):
            raise OSError("unsafe lock path")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        existing = _read_lock_fd(fd)
        os.close(fd)
        token.close()
        return None, existing
    except OSError:
        os.close(fd)
        token.close()
        return None, {"held_by": "unknown", "run_id": "unknown", "purpose": "unsafe lock path"}
    try:
        payload = json.dumps(
            {"held_by": held_by, "run_id": run_id, "started_at": now_iso(), "purpose": purpose},
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        os.ftruncate(fd, 0)
        os.pwrite(fd, payload, 0)
        os.fsync(fd)
    except BaseException:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        token.close()
        raise
    lock = _OwnedPathLock(target, fd, token)
    _OWNED_LOCK_HANDLES.add(lock)
    return lock, None


def acquire_owned_lock_blocking(
    target: Path,
    held_by: str,
    run_id: str,
    purpose: str,
) -> tuple[_OwnedPathLock | None, dict | None]:
    """Wait for and acquire an identity-preserving pathname lock."""
    waited = 0.0
    while True:
        owned, existing = acquire_owned_lock(target, held_by, run_id, purpose)
        if owned is not None:
            return owned, None
        if waited >= LOCK_MAX_WAIT_SECONDS:
            return None, existing
        time.sleep(LOCK_POLL_INTERVAL_SECONDS)
        waited += LOCK_POLL_INTERVAL_SECONDS


def release_owned_lock(lock: _OwnedPathLock) -> None:
    """Release only the retained kernel lock; never unlink a pathname."""
    if lock.released:
        return
    if os.getpid() != lock.owner_pid:
        lock.discard_after_fork()
        return
    fcntl.flock(lock.fd, fcntl.LOCK_UN)
    os.close(lock.fd)
    lock.token.close()
    lock.released = True
    _OWNED_LOCK_HANDLES.discard(lock)


class _AnchoredLock:
    """Owned generic lock retained by parent and locked-file descriptors."""

    def __init__(
        self,
        parent_fd: int,
        name: str,
        lock_fd: int,
        token: _KernelLockToken,
    ):
        self.parent_fd = parent_fd
        self.name = name
        self.lock_fd = lock_fd
        self.token = token
        self.owner_pid = os.getpid()
        self.released = False
        parent_metadata = os.fstat(parent_fd)
        self.parent_identity = (parent_metadata.st_dev, parent_metadata.st_ino)

    def discard_after_fork(self) -> None:
        if self.released:
            return
        os.close(self.lock_fd)
        os.close(self.parent_fd)
        self.token.close()
        self.released = True


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
    logical_lock_path = Path(str((directory / relative_path).absolute()) + ".lock")
    token = _try_kernel_lock(_lock_key(logical_lock_path))
    if token is None:
        existing = _read_lock_metadata_path(
            Path(f"/proc/self/fd/{parent_fd}") / name
        )
        os.close(parent_fd)
        return None, existing
    try:
        try:
            fd = os.open(
                name,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o644,
                dir_fd=parent_fd,
            )
        except OSError:
            os.close(parent_fd)
            token.close()
            return None, {
                "held_by": "unknown",
                "run_id": "unknown",
                "purpose": "unsafe lock path",
            }
        try:
            if not _safe_lock_descriptor(fd):
                raise OSError("unsafe lock path")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            existing = _read_lock_fd(fd)
            os.close(fd)
            os.close(parent_fd)
            token.close()
            return None, existing
        except OSError:
            os.close(fd)
            os.close(parent_fd)
            token.close()
            return None, {
                "held_by": "unknown",
                "run_id": "unknown",
                "purpose": "unsafe lock path",
            }
        try:
            payload = json.dumps(
                {
                    "held_by": held_by,
                    "run_id": run_id,
                    "started_at": now_iso(),
                    "purpose": purpose,
                },
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            os.ftruncate(fd, 0)
            os.pwrite(fd, payload, 0)
            os.fsync(fd)
        except BaseException:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise
        lock = _AnchoredLock(parent_fd, name, fd, token)
        _OWNED_LOCK_HANDLES.add(lock)
        return lock, None
    except BaseException:
        os.close(parent_fd)
        token.close()
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
    """Release the retained kernel lock without unlinking any pathname."""
    if lock.released:
        return
    if os.getpid() != lock.owner_pid:
        lock.discard_after_fork()
        return
    try:
        fcntl.flock(lock.lock_fd, fcntl.LOCK_UN)
        os.close(lock.lock_fd)
    finally:
        os.close(lock.parent_fd)
        lock.token.close()
        lock.released = True
        _OWNED_LOCK_HANDLES.discard(lock)


def _prepare_lock_registry_for_fork() -> None:
    _KERNEL_LOCK_TOKENS_GUARD.acquire()


def _release_lock_registry_after_fork() -> None:
    _KERNEL_LOCK_TOKENS_GUARD.release()


def _discard_inherited_locks_after_fork() -> None:
    """Close child duplicates without unlocking the parent's open descriptions."""
    global _KERNEL_LOCK_TOKENS, _KERNEL_LOCK_TOKENS_GUARD
    global _LOCK_HANDLES, _LOCK_HANDLES_GUARD, _OWNED_LOCK_HANDLES
    for token in _KERNEL_LOCK_TOKENS:
        token.discard_after_fork()
    _KERNEL_LOCK_TOKENS = set()
    _KERNEL_LOCK_TOKENS_GUARD = threading.Lock()
    for handles in _LOCK_HANDLES.values():
        for handle in handles:
            os.close(handle.fd)
            handle.token.close()
    for handle in _OWNED_LOCK_HANDLES:
        handle.discard_after_fork()
    _LOCK_HANDLES = {}
    _OWNED_LOCK_HANDLES = set()
    _LOCK_HANDLES_GUARD = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_prepare_lock_registry_for_fork,
        after_in_parent=_release_lock_registry_after_fork,
        after_in_child=_discard_inherited_locks_after_fork,
    )


# ------------------------------------------------------------- run-state --

def run_state_path(case_id: str) -> Path:
    return case_dir(case_id) / "_run_state.json"


def load_run_state(case_id: str) -> dict:
    p = run_state_path(case_id)
    existing = load_json(p)
    if existing is not None:
        return existing
    return {
        "run_state_version": "run_state.v0.3",
        "case_id": case_id,
        "run_id": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "stages": [],
        "human_input_status": [],
        "medical_review_adopted": False,
    }


def save_run_state(case_id: str, state: dict) -> None:
    state["updated_at"] = now_iso()
    atomic_write_json(run_state_path(case_id), state)


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


def _normalize_legacy_run_state(case_id: str, state: dict) -> dict:
    if "run_state_version" in state:
        return state
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
                "legacy Evaluation wait has no unambiguous draft version"
            )
        version = match.group(1)
        evaluation_versions.add(version)
        entry["stage_name"] = f"human_review_v{version}"
    for entry in state.get("stages", []):
        if entry.get("stage_name") != "evaluation":
            continue
        if len(evaluation_versions) != 1:
            raise ValueError(
                "legacy Evaluation stage has no unique human-review version"
            )
        entry["stage_name"] = f"human_review_v{next(iter(evaluation_versions))}"
    directory = case_dir(case_id)
    state["run_state_version"] = "run_state.v0.3"
    state["medical_review_adopted"] = any(
        path.exists() or path.is_symlink()
        for path in (
            directory / "medical_variables.json",
            directory / "_medical_review_ledger.json",
            directory / "_medical_variable_revisions",
        )
    )
    for operation in state.get(
        "medical_review_wait_reconciliation_operations", []
    ):
        if "receipt_sha256" not in operation:
            operation["receipt_sha256"] = _reconciliation_receipt_sha(operation)
    return state


def validated_run_state(case_id: str, *, allow_missing: bool = False) -> dict:
    path = run_state_path(case_id)
    path_missing = not path.exists()
    state = _normalize_legacy_run_state(case_id, load_run_state(case_id))
    directory = case_dir(case_id)
    if any(
        artifact.exists() or artifact.is_symlink()
        for artifact in (
            directory / "medical_variables.json",
            directory / "_medical_review_ledger.json",
            directory / "_medical_variable_revisions",
        )
    ):
        state["medical_review_adopted"] = True
    if allow_missing and path_missing:
        return state
    errors = _schema_check(state, "run_state.schema.json")
    if errors:
        raise ValueError("run state is invalid: " + "; ".join(errors))
    if state.get("case_id") != case_id:
        raise ValueError("run state belongs to a different case")
    operation_ids = [
        operation.get("operation_id")
        for operation in state.get(
            "medical_review_wait_reconciliation_operations", []
        )
    ]
    if len(operation_ids) != len(set(operation_ids)):
        raise ValueError("run state has duplicate reconciliation operation_id")
    stage_names = [entry["stage_name"] for entry in state["stages"]]
    if len(stage_names) != len(set(stage_names)):
        raise ValueError("run state has duplicate stage_name")
    human_input_ids = [
        entry["human_input_id"]
        for entry in state.get("human_input_status", [])
        if "human_input_id" in entry
    ]
    if len(human_input_ids) != len(set(human_input_ids)):
        raise ValueError("run state has duplicate human_input_id")
    run_id = state["run_id"]
    for operation in state.get(
        "medical_review_wait_reconciliation_operations", []
    ):
        operation_id = operation["operation_id"]
        ledger_sha = operation["medical_review_ledger_sha256"]
        if (
            operation_id.startswith("medical-projection:")
            and operation_id != f"medical-projection:{ledger_sha}"
        ):
            raise ValueError("automatic reconciliation operation_id is invalid")
        expected_sha = hashlib.sha256(_canonical_json_bytes(
            _reconciliation_request(
                case_id, run_id, operation_id, ledger_sha
            )
        )).hexdigest()
        if operation["request_sha256"] != expected_sha:
            raise ValueError("reconciliation operation request binding is invalid")
        if operation["receipt_sha256"] != _reconciliation_receipt_sha(operation):
            raise ValueError("reconciliation operation receipt binding is invalid")
    return state


def cmd_read_run_state(args) -> int:
    try:
        state = validated_run_state(args.case_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}")
        return 1
    print(json.dumps(state, ensure_ascii=False, sort_keys=True))
    return 0


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
    print(
        "DENIED: ground-truth access is unavailable in the local Units 1-7 "
        "harness; Evaluation requires the deferred authenticated isolated service"
    )
    return 1


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


def read_generic_file_bytes(case_id: str, filename: str) -> bytes:
    """Read an allowed generic file from one verified regular-file descriptor."""
    medical_repository.require_generic_target_allowed(
        sys.modules[__name__], case_id, filename
    )
    medical_repository.require_generic_read_allowed(
        sys.modules[__name__], case_id, filename
    )
    return _read_generic_contract_bytes(case_id, filename)


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
            print("BLOCKED: medical-review mutation does not match the canonical run owner")
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
    try:
        ledger = validated_source_ledger(args.case_id)
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(json.dumps(ledger, ensure_ascii=False, sort_keys=True))
    return 0


def make_history_boundary(
    baseline_state: list[dict],
    *,
    mode: str,
    established_at: str | None = None,
    absorbed_operations: list[dict] | None = None,
    forked_operations: list[dict] | None = None,
    predecessor_state: list[dict] | None = None,
) -> dict:
    baseline = json.loads(json.dumps(baseline_state))
    boundary = {
        "mode": mode,
        "established_at": established_at or now_iso(),
        "baseline_sha256": hashlib.sha256(
            _canonical_json_bytes(baseline)
        ).hexdigest(),
        "baseline_state": baseline,
    }
    if absorbed_operations is not None:
        absorbed = json.loads(json.dumps(absorbed_operations))
        boundary["absorbed_operation_count"] = len(absorbed)
        boundary["absorbed_operations_sha256"] = hashlib.sha256(
            _canonical_json_bytes(absorbed)
        ).hexdigest()
    if forked_operations is not None:
        forked = json.loads(json.dumps(forked_operations))
        boundary["forked_operation_count"] = len(forked)
        boundary["forked_operations_sha256"] = hashlib.sha256(
            _canonical_json_bytes(forked)
        ).hexdigest()
    if predecessor_state is not None:
        predecessor = json.loads(json.dumps(predecessor_state))
        boundary["predecessor_state_sha256"] = hashlib.sha256(
            _canonical_json_bytes(predecessor)
        ).hexdigest()
    return boundary


def _replay_generic_ledger_history(ledger: dict, schema_name: str) -> None:
    boundary = ledger["history_boundary"]
    baseline = boundary["baseline_state"]
    if boundary["baseline_sha256"] != hashlib.sha256(
        _canonical_json_bytes(baseline)
    ).hexdigest():
        raise ValueError("ledger history boundary digest is invalid")
    replayed = json.loads(json.dumps(baseline))
    absorbed_count = boundary.get("absorbed_operation_count", 0)
    if absorbed_count > len(ledger["operations"]):
        raise ValueError("ledger absorbed-operation boundary exceeds history")
    absorbed = ledger["operations"][:absorbed_count]
    if "absorbed_operations_sha256" in boundary and boundary[
        "absorbed_operations_sha256"
    ] != hashlib.sha256(_canonical_json_bytes(absorbed)).hexdigest():
        raise ValueError("ledger absorbed-operation boundary digest is invalid")
    for operation in ledger["operations"][absorbed_count:]:
        request = operation["request"]
        payload = request["payload"]
        result = operation["result"]
        completed_at = operation["completed_at"]
        if schema_name == "source_ledger.schema.json":
            entry = next(
                (
                    item for item in replayed
                    if item["file_name"] == payload["file_name"]
                ),
                None,
            )
            if entry is None:
                raise ValueError("ledger operation targets no baseline file")
            entry["review_status"] = payload["status"]
            entry["reviewed_by"] = payload["reviewer"]
            entry["reviewed_at"] = completed_at
            entry["rejection_reason"] = (
                payload["reason"] if payload["status"] == "rejected" else None
            )
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
            entry = next(
                (
                    item for item in replayed
                    if item["conflict_id"] == payload["conflict_id"]
                ),
                None,
            )
            if entry is None:
                raise ValueError("ledger operation targets no replayed conflict")
            entry["verdict"] = payload["verdict"]
            entry["resolution_note"] = payload["note"]
            entry["resolved_at"] = completed_at
    current = (
        ledger["files"]
        if schema_name == "source_ledger.schema.json"
        else ledger["conflicts"]
    )
    if replayed != current:
        raise ValueError("ledger state does not replay from its history boundary")


def _validate_predecessor_final_state(ledger: dict, schema_name: str) -> None:
    if schema_name == "source_ledger.schema.json":
        latest = {}
        for operation in ledger["operations"]:
            latest[operation["request"]["payload"]["file_name"]] = operation
        current = {entry["file_name"]: entry for entry in ledger["files"]}
        for file_name, operation in latest.items():
            payload = operation["request"]["payload"]
            entry = current[file_name]
            expected_reason = (
                payload["reason"] if payload["status"] == "rejected" else None
            )
            if (
                entry["review_status"] != payload["status"]
                or entry.get("reviewed_by") != payload["reviewer"]
                or entry.get("rejection_reason") != expected_reason
            ):
                raise ValueError(
                    "predecessor source ledger contradicts its latest receipt"
                )
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
        if any(current[target_id].get(key) != value for key, value in fields.items()):
            raise ValueError(
                "predecessor conflict ledger contradicts its latest receipt"
            )


def _normalize_predecessor_timestamps(ledger: dict, schema_name: str) -> None:
    latest = {}
    for operation in ledger["operations"]:
        payload = operation["request"]["payload"]
        target_id = (
            payload["file_name"]
            if schema_name == "source_ledger.schema.json"
            else operation["result"]["target_id"]
        )
        latest[target_id] = operation
    state_key = (
        "files" if schema_name == "source_ledger.schema.json" else "conflicts"
    )
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
                    "pending predecessor conflict has a resolution timestamp"
                )
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
                "predecessor state timestamp is inconsistent with its receipt"
            )
        entry[timestamp_key] = completed_at


def _validate_generic_ledger(
    ledger: dict,
    case_id: str,
    schema_name: str,
    *,
    replay_history: bool = True,
) -> None:
    errors = _schema_check(ledger, schema_name)
    if errors:
        raise ValueError(f"{schema_name} is invalid: " + "; ".join(errors))
    if ledger.get("case_id") != case_id:
        raise ValueError("ledger belongs to a different case")
    operation_ids = [
        operation["operation_id"] for operation in ledger["operations"]
    ]
    if len(operation_ids) != len(set(operation_ids)):
        raise ValueError("ledger has duplicate operation_id")
    source_names = {entry["file_name"] for entry in ledger.get("files", [])}
    conflict_ids = {
        entry["conflict_id"] for entry in ledger.get("conflicts", [])
    }
    for operation in ledger["operations"]:
        request = operation["request"]
        if (
            request["case_id"] != case_id
            or request["operation_id"] != operation["operation_id"]
            or hashlib.sha256(_canonical_json_bytes(request)).hexdigest()
            != operation["request_sha256"]
        ):
            raise ValueError("ledger operation request binding is invalid")
        payload = request["payload"]
        result = operation["result"]
        if schema_name == "source_ledger.schema.json":
            expected = {
                "action": "set_status",
                "target_id": payload["file_name"],
                "status": payload["status"],
            }
            target_exists = result["target_id"] in source_names
        elif request["action"] == "add":
            expected = {
                "action": "add",
                "target_id": result["target_id"],
                "status": "pending",
            }
            target_exists = result["target_id"] in conflict_ids
            conflict = next(
                entry for entry in ledger["conflicts"]
                if entry["conflict_id"] == result["target_id"]
            ) if target_exists else None
            target_exists = target_exists and all((
                conflict["raised_by_stage"] == payload["stage"],
                conflict["field_or_topic"] == payload["topic"],
                conflict["sources"] == payload["sources"],
            ))
        else:
            expected = {
                "action": "set_verdict",
                "target_id": payload["conflict_id"],
                "status": payload["verdict"],
            }
            target_exists = result["target_id"] in conflict_ids
        if result != expected or not target_exists:
            raise ValueError("ledger operation result binding is invalid")
    if replay_history:
        _replay_generic_ledger_history(ledger, schema_name)


def _normalize_legacy_generic_ledger(
    ledger: dict,
    *,
    version: str,
    state_key: str,
    schema_name: str,
    predecessor_versions: set[str],
) -> dict:
    has_version = "ledger_version" in ledger
    has_operations = "operations" in ledger
    has_boundary = "history_boundary" in ledger
    if not has_version and not has_operations and not has_boundary:
        ledger = json.loads(json.dumps(ledger))
        ledger["ledger_version"] = version
        ledger["operations"] = []
        ledger["history_boundary"] = make_history_boundary(
            ledger[state_key],
            mode="legacy_snapshot",
            established_at=(
                ledger.get("updated_at")
                or ledger.get("created_at")
                or "1970-01-01T00:00:00+00:00"
            ),
        )
        return ledger
    if (
        has_version
        and has_operations
        and not has_boundary
        and ledger.get("ledger_version") in predecessor_versions
    ):
        candidate = json.loads(json.dumps(ledger))
        candidate["ledger_version"] = version
        candidate["history_boundary"] = make_history_boundary(
            candidate[state_key], mode="legacy_snapshot"
        )
        _validate_generic_ledger(
            candidate,
            candidate.get("case_id"),
            schema_name,
            replay_history=False,
        )
        _validate_predecessor_final_state(candidate, schema_name)
        absorbed = candidate["operations"]
        predecessor_state = json.loads(json.dumps(candidate[state_key]))
        _normalize_predecessor_timestamps(candidate, schema_name)
        candidate["history_boundary"] = make_history_boundary(
            candidate[state_key],
            mode="legacy_snapshot",
            absorbed_operations=absorbed,
            predecessor_state=predecessor_state,
        )
        return candidate
    if not (has_version and has_operations and has_boundary):
        raise ValueError("ledger version/operation/history boundary is incomplete")
    return ledger


def validated_source_ledger(case_id: str) -> dict:
    ledger = load_json(source_ledger_path(case_id))
    if ledger is None:
        raise ValueError("source ledger not found")
    ledger = _normalize_legacy_generic_ledger(
        ledger,
        version="source_ledger.v0.4",
        state_key="files",
        schema_name="source_ledger.schema.json",
        predecessor_versions={"source_ledger.v0.3"},
    )
    _validate_generic_ledger(
        ledger, case_id, "source_ledger.schema.json"
    )
    return ledger


def _prepare_ledger_operation(
    ledger: dict,
    args,
    action: str,
    payload: dict,
) -> tuple[dict, str, dict | None]:
    operation_id = getattr(args, "operation_id", None)
    if not isinstance(operation_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", operation_id
    ):
        raise ValueError("operation_id has an invalid format")
    request = {
        "case_id": args.case_id,
        "operation_id": operation_id,
        "action": action,
        "payload": payload,
    }
    request_sha256 = hashlib.sha256(_canonical_json_bytes(request)).hexdigest()
    matches = [
        operation
        for operation in ledger["operations"]
        if operation.get("operation_id") == operation_id
    ]
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


def _commit_ledger_operation(
    ledger: dict,
    args,
    request: dict,
    request_sha256: str,
    result: dict,
    completed_at: str,
) -> None:
    ledger["operations"].append({
        "operation_id": args.operation_id,
        "request": request,
        "request_sha256": request_sha256,
        "result": result,
        "completed_at": completed_at,
    })


def cmd_set_ledger_status(args):
    p = source_ledger_path(args.case_id)
    existing_lock = acquire_lock_blocking(p, args.held_by, args.run_id, f"set-ledger-status {args.file_name} -> {args.status}")
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    try:
        try:
            ledger = validated_source_ledger(args.case_id)
            request, request_sha256, replay = _prepare_ledger_operation(
                ledger,
                args,
                "set_status",
                {
                    "file_name": args.file_name,
                    "status": args.status,
                    "reviewer": args.reviewer,
                    "reason": args.reason,
                },
            )
        except ValueError as exc:
            print(f"FAIL: {exc}")
            return 1
        if replay is not None:
            print(json.dumps(replay, sort_keys=True))
            return 0
        if args.status == "approved" and not args.reviewer:
            print("ERROR: --reviewer is required to set status approved")
            return 1
        if args.status == "rejected" and not args.reason:
            print("ERROR: --reason is required to set status rejected")
            return 1
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
        ledger["updated_at"] = now_iso()
        result = {
            "action": "set_status",
            "target_id": args.file_name,
            "status": args.status,
        }
        _commit_ledger_operation(
            ledger, args, request, request_sha256, result, completed_at
        )
        errors = _schema_check(ledger, "source_ledger.schema.json")
        if errors:
            print(f"FAIL: schema validation errors for {p} -- not written:")
            for e in errors:
                print(f"  - {e}")
            return 1
        atomic_write_json(p, ledger)
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        release_lock(p)


def cmd_check_source_ledger_clear(args):
    try:
        ledger = validated_source_ledger(args.case_id)
    except ValueError as exc:
        print(json.dumps({"clear": False, "error": str(exc)}))
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
    if not _medical_clearance_required(
        state, stage, status, snapshot=snapshot
    ):
        return
    from medical_review_ledger import require_clearance

    require_clearance(sys.modules[__name__], case_id, run_id)

def _update_run_state(case_id, run_id, stage, status, held_by, backup_path=None):
    """Holds the run-state lock across the whole read+modify+write, not just
    the write -- see acquire_lock_blocking. Returns the updated state on
    success, or None if the lock never cleared (caller reports and halts)."""
    target = run_state_path(case_id)
    owned_lock, existing_lock = acquire_owned_lock_blocking(
        target,
        held_by,
        run_id or "unknown",
        f"update run-state: {stage} -> {status}",
    )
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return None
    assert owned_lock is not None
    try:
        try:
            state = validated_run_state(case_id, allow_missing=True)
            _require_transition_medical_clearance(
                case_id, run_id, state, stage, status
            )
        except ValueError as exc:
            print(f"BLOCKED: {exc}")
            return None
        owner_changes = (
            state.get("run_id") not in {None, run_id}
            and run_id is not None
        )
        canonical_exists = (
            medical_variables_path(case_id).exists()
            or medical_variables_path(case_id).is_symlink()
        )
        if owner_changes and canonical_exists:
            print("BLOCKED: run-state update does not match the canonical run owner")
            return None
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
        release_owned_lock(owned_lock)


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
    owned_lock, existing_lock = acquire_owned_lock_blocking(
        target,
        held_by,
        run_id or "unknown",
        f"set human_input_status: {stage} -> {status}",
    )
    if existing_lock is not None:
        print(f"LOCKED: held_by={existing_lock['held_by']} run_id={existing_lock['run_id']} "
              f"since={existing_lock['started_at']} purpose={existing_lock['purpose']}")
        return 1
    assert owned_lock is not None
    try:
        try:
            state = validated_run_state(case_id, allow_missing=True)
        except ValueError as exc:
            print(f"BLOCKED: {exc}")
            return 1
        owner_changes = (
            state.get("run_id") not in {None, run_id}
            and run_id is not None
        )
        canonical_exists = (
            medical_variables_path(case_id).exists()
            or medical_variables_path(case_id).is_symlink()
        )
        if owner_changes and canonical_exists:
            print("BLOCKED: human-input update does not match the canonical run owner")
            return 1
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
        release_owned_lock(owned_lock)


def cmd_set_human_input_status(args):
    """Generic write path for P7 -- see harness-guardrails P7. Usable by any
    stage that needs to wait on a human, not just the critic->human-review
    handoff (that handoff has its own narrow wrapper, request-expert-review,
    built on this)."""
    return _set_human_input_status(args.case_id, args.stage, args.status, args.description, args.held_by, args.run_id)


def cmd_request_expert_review(args):
    """Purpose-built wrapper around set-human-input-status for the
    critic -> human review -> external handoff. The local human-review
    stage is the stage actually blocked/pending, matching P7's
    'naming exactly which stage... is pending.' Keeps the description
    convention defined in one place rather than every caller constructing
    it by hand."""
    description = f"expert review of draft_report_{args.version}_reviewed.md"
    stage = f"human_review_{args.version}"
    return _set_human_input_status(
        args.case_id, stage, "waiting", description, args.held_by, args.run_id
    )


def cmd_mark_human_review_complete(args):
    """Record the versioned future Unit 11 handoff prerequisite.

    This does not unlock local Evaluation or ground-truth access. Requires
    expert_review_v{version}.json
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

    status_rc = _set_human_input_status(
        args.case_id,
        f"human_review_{args.version}",
        "received",
        None,
        args.held_by,
        args.run_id,
    )
    if status_rc != 0:
        print("note: no matching 'waiting' human_input_status entry was found to flip to 'received' -- "
              "the handoff-prerequisite flag was still created, but the wait-tracking history "
              "is incomplete for this version.")
    print(f"OK: {target} created -- future Unit 11 handoff prerequisite recorded for {args.version}; "
          "local Evaluation and ground-truth access remain unavailable.")
    return 0


def cmd_get_last_passed_stage(args):
    try:
        state = validated_run_state(args.case_id, allow_missing=True)
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 1
    passed = [s["stage_name"] for s in state["stages"] if s["status"] == "passed"]
    print(passed[-1] if passed else "NONE")
    return 0


SNAPSHOT_MAX_ATTEMPTS = 3


def _snapshot_path_excluded(relative: Path) -> bool:
    return (
        not relative.parts
        or relative.parts[0] == "_backups"
        or relative == Path("_run_state.json")
        or relative.name.endswith(".lock")
        or re.search(r"\.tmp\d+$", relative.name) is not None
    )


def _snapshot_inventory(root: Path) -> dict[str, dict[str, object]]:
    inventory: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if _snapshot_path_excluded(relative):
            continue
        metadata = path.lstat()
        key = relative.as_posix()
        if stat.S_ISDIR(metadata.st_mode):
            inventory[key] = {"kind": "directory"}
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"snapshot source contains an unsupported path: {relative}")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError(f"snapshot source is not a regular file: {relative}")
            digest = hashlib.sha256()
            size = 0
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        finally:
            os.close(descriptor)
        inventory[key] = {
            "kind": "file",
            "size": size,
            "sha256": digest.hexdigest(),
        }
    return inventory


def _snapshot_inventory_sha(inventory: dict[str, dict[str, object]]) -> str:
    encoded = json.dumps(
        inventory,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _copy_snapshot_file(source: Path, destination: Path) -> None:
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination_fd = -1
    try:
        metadata = os.fstat(source_fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"snapshot source is not a regular file: {source}")
        destination_fd = os.open(
            destination,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            stat.S_IMODE(metadata.st_mode),
        )
        while chunk := os.read(source_fd, 1024 * 1024):
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
        os.fsync(destination_fd)
    finally:
        os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)


def _copy_snapshot_inventory(
    source_root: Path,
    destination_root: Path,
    inventory: dict[str, dict[str, object]],
) -> None:
    for relative_text, entry in inventory.items():
        relative = Path(relative_text)
        destination = destination_root / relative
        if entry["kind"] == "directory":
            destination.mkdir(parents=True, exist_ok=True)
        else:
            _copy_snapshot_file(source_root / relative, destination)


def _next_snapshot_destination(backups: Path, stage: str) -> Path:
    highest = 0
    for child in backups.iterdir():
        match = re.fullmatch(r"step_(\d+)_.*", child.name)
        if child.is_dir() and not child.is_symlink() and match:
            highest = max(highest, int(match.group(1)))
    return backups / f"step_{highest + 1:02d}_{stage}"


def _rename_snapshot_directory_noreplace(source: Path, destination: Path) -> None:
    """Atomically publish a snapshot only if its final name is still absent."""
    if source.parent != destination.parent:
        raise OSError("snapshot staging and destination must share one directory")
    parent_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOSYS, "renameat2 is unavailable")
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            parent_fd,
            os.fsencode(source.name),
            parent_fd,
            os.fsencode(destination.name),
            1,  # RENAME_NOREPLACE
        )
        if result != 0:
            error_number = ctypes.get_errno()
            raise OSError(
                error_number,
                os.strerror(error_number),
                destination,
            )
    finally:
        os.close(parent_fd)


def cmd_snapshot_backup(args):
    src = case_dir(args.case_id)
    src.mkdir(parents=True, exist_ok=True)
    backups = src / "_backups"
    backups.mkdir(parents=True, exist_ok=True)
    if backups.is_symlink() or not backups.is_dir():
        print("FAIL: snapshot backup namespace is not a safe directory")
        return 1
    state_target = run_state_path(args.case_id)
    owned_lock, existing_lock = acquire_owned_lock_blocking(
        state_target,
        args.held_by,
        args.run_id,
        f"publish coherent snapshot for {args.stage}",
    )
    if existing_lock is not None:
        print(
            f"LOCKED: held_by={existing_lock['held_by']} "
            f"run_id={existing_lock['run_id']}"
        )
        return 1
    assert owned_lock is not None
    dest: Path | None = None
    staging: Path | None = None
    promoted = False
    try:
        try:
            state = validated_run_state(args.case_id, allow_missing=True)
        except ValueError as exc:
            print(f"BLOCKED: {exc}")
            return 1
        owner = state.get("run_id")
        if owner not in {None, args.run_id}:
            print("BLOCKED: snapshot does not match the canonical run owner")
            return 1
        try:
            _require_transition_medical_clearance(
                args.case_id,
                args.run_id,
                state,
                args.stage,
                snapshot=True,
            )
        except ValueError as exc:
            print(f"BLOCKED: {exc}")
            return 1
        dest = _next_snapshot_destination(backups, args.stage)
        if dest.exists() or dest.is_symlink():
            print(f"FAIL: snapshot destination already exists: {dest}")
            return 1

        final_state = json.loads(json.dumps(state))
        final_state["run_id"] = args.run_id
        stages = final_state["stages"]
        entry = next(
            (item for item in stages if item["stage_name"] == args.stage),
            None,
        )
        if entry is None:
            entry = {
                "stage_name": args.stage,
                "status": "pending",
                "started_at": None,
                "completed_at": None,
                "attempt_count": 0,
                "backup_path": None,
            }
            stages.append(entry)
        completed_at = now_iso()
        entry["status"] = "passed"
        entry["completed_at"] = completed_at
        entry["backup_path"] = str(dest)
        final_state["updated_at"] = completed_at
        errors = _schema_check(final_state, "run_state.schema.json")
        if errors:
            print("FAIL: schema validation errors for final snapshot run state:")
            for error in errors:
                print(f"  - {error}")
            return 1

        source_inventory = None
        for attempt in range(1, SNAPSHOT_MAX_ATTEMPTS + 1):
            staging = backups / (
                f".{dest.name}.incomplete-{os.getpid()}-"
                f"{threading.get_ident()}-{attempt}"
            )
            shutil.rmtree(staging, ignore_errors=True)
            try:
                before = _snapshot_inventory(src)
                staging.mkdir(parents=False, exist_ok=False)
                _copy_snapshot_inventory(src, staging, before)
                after = _snapshot_inventory(src)
                copied = _snapshot_inventory(staging)
                if before == after == copied:
                    source_inventory = after
                    break
            except OSError:
                if attempt == SNAPSHOT_MAX_ATTEMPTS:
                    raise
            shutil.rmtree(staging, ignore_errors=True)
        if source_inventory is None:
            print(
                "FAIL: case artifacts changed during every bounded snapshot attempt"
            )
            return 1
        assert staging is not None

        atomic_write_json(staging / "_run_state.json", final_state)
        atomic_write_json(staging / "_snapshot_manifest.json", {
            "schema_version": "snapshot_manifest.v0.1",
            "complete": True,
            "case_id": args.case_id,
            "run_id": args.run_id,
            "stage": args.stage,
            "completed_at": completed_at,
            "source_inventory_sha256": _snapshot_inventory_sha(source_inventory),
            "entry_count": len(source_inventory),
        })
        _rename_snapshot_directory_noreplace(staging, dest)
        promoted = True
        directory_fd = os.open(backups, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        try:
            atomic_write_json(state_target, final_state)
        except AtomicWriteCommittedError as exc:
            print(
                f"PARTIAL: coherent snapshot and run state were published, but "
                f"run-state directory durability was not confirmed: {exc}"
            )
            return 1
        except OSError as exc:
            print(
                f"PARTIAL: coherent snapshot was published at {dest}, but "
                f"run state was not updated; retry snapshot registration: {exc}"
            )
            return 1
        print(f"OK: snapshot at {dest}")
        return 0
    except OSError as exc:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if promoted and dest is not None:
            print(
                f"PARTIAL: coherent snapshot was published at {dest}, but "
                f"post-publication durability failed: {exc}"
            )
            return 1
        print(f"FAIL: coherent snapshot was not published: {exc}")
        return 1
    finally:
        release_owned_lock(owned_lock)


# ------------------------------------------------------------ conflict ledger --

def conflict_ledger_path(case_id: str) -> Path:
    return case_dir(case_id) / "_conflict_ledger.json"


def validated_conflict_ledger(
    case_id: str, *, allow_missing: bool = False
) -> dict:
    existing = load_json(conflict_ledger_path(case_id))
    if existing is not None:
        existing = _normalize_legacy_generic_ledger(
            existing,
            version="conflict_ledger.v0.3",
            state_key="conflicts",
            schema_name="conflict_ledger.schema.json",
            predecessor_versions={"conflict_ledger.v0.2"},
        )
        _validate_generic_ledger(
            existing, case_id, "conflict_ledger.schema.json"
        )
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


def load_conflict_ledger(case_id: str) -> dict:
    return validated_conflict_ledger(case_id, allow_missing=True)


def ensure_conflict_ledger(case_id: str, held_by: str, run_id: str) -> dict:
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
            raise ValueError("generated conflict ledger is invalid: " + "; ".join(errors))
        atomic_write_json(target, ledger)
        return ledger
    finally:
        release_lock(target)


def cmd_read_conflict_ledger(args):
    try:
        ledger = load_conflict_ledger(args.case_id)
    except ValueError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(json.dumps(ledger, ensure_ascii=False, indent=2))
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
        except ValueError as exc:
            print(f"FAIL: {exc}")
            return 1
        sources = json.loads(Path(args.sources_file).read_text(encoding="utf-8"))
        try:
            request, request_sha256, replay = _prepare_ledger_operation(
                ledger,
                args,
                "add",
                {
                    "stage": args.stage,
                    "topic": args.topic,
                    "sources": sources,
                },
            )
        except ValueError as exc:
            print(f"FAIL: {exc}")
            return 1
        if replay is not None:
            print(json.dumps(replay, sort_keys=True))
            return 0
        n = len(ledger["conflicts"]) + 1
        completed_at = now_iso()
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
        result = {
            "action": "add",
            "target_id": f"CONFLICT_{n}",
            "status": "pending",
        }
        _commit_ledger_operation(
            ledger, args, request, request_sha256, result, completed_at
        )
        errors = _schema_check(ledger, "conflict_ledger.schema.json")
        if errors:
            print(f"FAIL: schema validation errors for {target} -- not written:")
            for e in errors:
                print(f"  - {e}")
            return 1
        atomic_write_json(target, ledger)
        print(json.dumps(result, sort_keys=True))
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
        except ValueError as exc:
            print(f"FAIL: {exc}")
            return 1
        try:
            request, request_sha256, replay = _prepare_ledger_operation(
                ledger,
                args,
                "set_verdict",
                {
                    "conflict_id": args.conflict_id,
                    "verdict": args.verdict,
                    "note": args.note,
                },
            )
        except ValueError as exc:
            print(f"FAIL: {exc}")
            return 1
        if replay is not None:
            print(json.dumps(replay, sort_keys=True))
            return 0
        entry = next((c for c in ledger["conflicts"] if c["conflict_id"] == args.conflict_id), None)
        if entry is None:
            print(f"NOT_FOUND: {args.conflict_id}")
            return 1
        completed_at = now_iso()
        entry["verdict"] = args.verdict
        entry["resolution_note"] = args.note
        entry["resolved_at"] = completed_at
        ledger["updated_at"] = now_iso()
        result = {
            "action": "set_verdict",
            "target_id": args.conflict_id,
            "status": args.verdict,
        }
        _commit_ledger_operation(
            ledger, args, request, request_sha256, result, completed_at
        )
        errors = _schema_check(ledger, "conflict_ledger.schema.json")
        if errors:
            print(f"FAIL: schema validation errors for {target} -- not written:")
            for e in errors:
                print(f"  - {e}")
            return 1
        atomic_write_json(target, ledger)
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        release_lock(target)


def cmd_check_conflicts_clear(args):
    try:
        ledger = load_conflict_ledger(args.case_id)
    except ValueError as exc:
        print(json.dumps({"clear": False, "error": str(exc)}))
        return 1
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

    p = sub.add_parser("read-run-state")
    p.add_argument("case_id")
    p.set_defaults(fn=cmd_read_run_state)

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
    p.add_argument("--operation-id", required=True)
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
    p.add_argument("--operation-id", required=True)
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_add_conflict_entry)

    p = sub.add_parser("set-conflict-verdict")
    p.add_argument("case_id"); p.add_argument("conflict_id")
    p.add_argument("verdict", choices=["resolved", "false_positive"]); p.add_argument("--note", required=True)
    p.add_argument("--operation-id", required=True)
    p.add_argument("--held-by", required=True); p.add_argument("--run-id", required=True)
    p.set_defaults(fn=cmd_set_conflict_verdict)

    p = sub.add_parser("check-conflicts-clear"); p.add_argument("case_id")
    p.set_defaults(fn=cmd_check_conflicts_clear)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
