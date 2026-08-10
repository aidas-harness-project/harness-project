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

Ground truth is never scanned or copied. Evaluation belongs to the deferred
isolated Unit 11 service, not this local Units 1-7 utility.

Content-addressed medical revisions are not rebased by this legacy utility.
If canonical medical artifacts exist, the fork fails closed before copying;
use a future purpose-built medical lineage operation instead.

_backups/ and *.lock files are never copied -- persistent lock inodes are
source-local coordination state, and a fork starts its own backup history
rather than inheriting the source's.

The forked _source_ledger.json keeps the source case's approved/rejected
statuses as-is -- it's a copy of already-reviewed content, not new raw
input, so resetting every entry to pending would force a pointless
re-review of files a human already looked at. This means a forked case_id's
D2 approvals did NOT come from an independent human review of that specific
case_id -- _fork_record.json exists specifically so nobody mistakes a fork
for a freshly-reviewed case.

Refuses to fork if any persistent *.lock file under the source is kernel-held
or unsafe. Unlocked persistent lock inodes are expected and are never copied.
Forking possibly-half-written state would just propagate the problem into the
branch (same "don't poll, don't assume stale" discipline P5 uses for a lock
found at run start).

Known limitation, documented rather than silently assumed away: case_id
auto-assignment (scan existing CASE_NNN dirs, use the next number) has a
TOCTOU race if two fork operations run concurrently -- acceptable for a
manual, occasional-use tool at this project's scale, not hardened for
concurrent multi-actor use.

Usage:
    python tools/fork_case.py SOURCE_CASE_ID --label "..." --held-by NAME --run-id RUN_ID
    python tools/fork_case.py SOURCE_CASE_ID --from-step 3 --label "..." --held-by NAME --run-id RUN_ID
    python tools/fork_case.py SOURCE_CASE_ID --include-raw --label "..." --held-by NAME --run-id RUN_ID
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import dao
from dao import case_dir, atomic_write_json, now_iso, read_lock, OUTPUTS, DATA
from _validation import load_registry, validate_instance, schema_name_for

CASE_ID_DIR_RE = re.compile(r"^CASE_(\d+)$")


def next_free_case_id() -> str:
    """Scans every root a case_id directory could exist under and returns
    the next free CASE_NNN, zero-padded to 3 digits (matching the existing
    CASE_001/CASE_002/CASE_009 convention). Non-numeric dirs (CASE_DEMO,
    CASE_SMOKE) are ignored -- they predate or fall outside the
    ^CASE_[0-9]+$ schema pattern and aren't part of this numbering."""
    max_n = 0
    for root in (OUTPUTS, DATA / "raw", DATA / "processed"):
        if not root.exists():
            continue
        for d in root.iterdir():
            m = CASE_ID_DIR_RE.match(d.name)
            if m:
                max_n = max(max_n, int(m.group(1)))
    return f"CASE_{max_n + 1:03d}"


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
    active = [
        path
        for path in locks
        if read_lock(path.with_name(path.name.removesuffix(".lock"))) is not None
    ]
    if active:
        sys.exit(f"error: refusing to fork -- {len(active)} active or unsafe lock(s) under {source_root}, "
                  f"a write may be in progress: {[str(p) for p in active]}")


def copy_outputs_and_rewrite_case_id(source_root: Path, new_case_id: str) -> list[str]:
    """Returns the list of validation warnings (empty if everything that has
    a schema still validates after the case_id rewrite)."""
    medical_artifacts = [
        source_root / "medical_variables.json",
        source_root / "_medical_review_ledger.json",
        source_root / "_medical_variable_revisions",
    ]
    if any(path.exists() or path.is_symlink() for path in medical_artifacts):
        raise ValueError(
            "forking adopted medical state is unavailable: content-addressed "
            "medical revision lineage cannot be generically rewritten"
        )
    dest = case_dir(new_case_id)
    for item in source_root.iterdir():
        if item.name == "_backups" or item.name.endswith(".lock"):
            continue
        if item.is_file():
            shutil.copy2(item, dest / item.name)
        elif item.is_dir():
            shutil.copytree(
                item,
                dest / item.name,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("*.lock"),
            )

    schemas, registry = load_registry()
    warnings = []
    for json_path in sorted(dest.rglob("*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "case_id" in data:
            source_case_id = data["case_id"]
            if json_path.name == "_run_state.json":
                data = dao._normalize_legacy_run_state(source_case_id, data)
            elif json_path.name == "_source_ledger.json":
                data = dao._normalize_legacy_generic_ledger(
                    data,
                    version="source_ledger.v0.4",
                    state_key="files",
                    schema_name="source_ledger.schema.json",
                    predecessor_versions={"source_ledger.v0.3"},
                )
                dao._validate_generic_ledger(
                    data, source_case_id, "source_ledger.schema.json"
                )
                data["history_boundary"] = dao.make_history_boundary(
                    data["files"],
                    mode="legacy_snapshot",
                    forked_operations=data["operations"],
                )
                data["operations"] = []
            elif json_path.name == "_conflict_ledger.json":
                data = dao._normalize_legacy_generic_ledger(
                    data,
                    version="conflict_ledger.v0.3",
                    state_key="conflicts",
                    schema_name="conflict_ledger.schema.json",
                    predecessor_versions={"conflict_ledger.v0.2"},
                )
                dao._validate_generic_ledger(
                    data, source_case_id, "conflict_ledger.schema.json"
                )
                data["history_boundary"] = dao.make_history_boundary(
                    data["conflicts"],
                    mode="legacy_snapshot",
                    forked_operations=data["operations"],
                )
                data["operations"] = []
            if json_path.name == "_run_state.json":
                data["medical_review_wait_reconciliation_operations"] = []
            data["case_id"] = new_case_id
            atomic_write_json(json_path, data)
            schema_name = schema_name_for(json_path)
            if schema_name:
                errors = validate_instance(data, schema_name, schemas, registry)
                if errors:
                    warnings.append(f"{json_path.name}: fails {schema_name} after case_id rewrite -- "
                                     + "; ".join(errors))
    return warnings


def copy_data_tree(subdir_name: str, source_case_id: str, new_case_id: str) -> Path | None:
    if subdir_name not in {"processed", "raw"}:
        raise ValueError(f"unsupported fork data namespace: {subdir_name!r}")
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
    ap.add_argument("--label", required=True, help="Human-readable description of what this branch is testing")
    ap.add_argument("--include-raw", action="store_true")
    ap.add_argument("--held-by", required=True)
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args()

    if not re.match(r"^CASE_\d+$", args.source_case_id):
        sys.exit(f"error: source_case_id must match CASE_NNN -- got {args.source_case_id!r}")

    source_root = resolve_source_root(args.source_case_id, args.from_step)
    check_no_active_locks(source_root)

    new_case_id = next_free_case_id()
    dest = case_dir(new_case_id)
    if any(dest.iterdir()):
        sys.exit(f"error: {dest} already exists and isn't empty -- refusing to fork into it. "
                  f"(TOCTOU race with a concurrent fork? re-run.)")

    try:
        warnings = copy_outputs_and_rewrite_case_id(source_root, new_case_id)
    except ValueError as exc:
        sys.exit(f"error: {exc}")

    copy_data_tree("processed", args.source_case_id, new_case_id)
    raw_copied = args.include_raw and copy_data_tree("raw", args.source_case_id, new_case_id) is not None

    fork_record = {
        "new_case_id": new_case_id,
        "forked_from": args.source_case_id,
        "forked_at_step": (f"step_{args.from_step:02d}" if args.from_step is not None else "current"),
        "forked_at": now_iso(),
        "label": args.label,
        "included_raw": raw_copied,
        "included_ground_truth": False,
        "ledger_carried_forward": True,
        "held_by": args.held_by,
        "run_id": args.run_id,
    }
    atomic_write_json(dest / "_fork_record.json", fork_record)

    print(f"OK: forked {args.source_case_id} ({'step_' + f'{args.from_step:02d}' if args.from_step is not None else 'current state'}) "
          f"-> {new_case_id}")
    print(f"  label: {args.label}")
    print(f"  outputs/: copied, case_id fields rewritten")
    print(f"  data/processed/: copied")
    print(f"  data/raw/: {'copied' if raw_copied else 'not copied (pass --include-raw)'}")
    print("  data/ground_truth/: unavailable to the local fork utility")
    if warnings:
        print(f"  WARNING: {len(warnings)} file(s) failed schema validation after the case_id rewrite:")
        for w in warnings:
            print(f"    - {w}")
        sys.exit(1)


if __name__ == "__main__":
    main()
