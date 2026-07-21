"""One-shot backfill: add cross_validation_mode/cross_validation_note to
pre-existing ocr_result files.

ocr_result.schema.json made cross_validation_mode (and _note) REQUIRED. Every
ocr_result written before that field existed (CASE_004 legacy, CASE_012-022,
plus per-step backups) now fails validation on any fork or re-run. All of those
runs used the claude-cli/claude-cli reader pair, so the honest label is
`single_technology_weak_p8_poc`. This stamps exactly that where the field is
missing; it never overwrites an existing value, so it is safe to re-run.

Usage (dry run by default):
    python tools/backfill_cross_validation_mode.py <outputs_root>
    python tools/backfill_cross_validation_mode.py <outputs_root> --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


MODE = "single_technology_weak_p8_poc"
NOTE = (
    "Backfilled: this run predates the cross_validation_mode field. It used the "
    "claude-cli/claude-cli reader pair -- one LLM-vision model self-checking, a "
    "documented weak P8 that cannot catch a correlated confident error shared by "
    "both reads. Not genuine dual-technology independence (open-decisions.md #4)."
)


def backfill_file(path: Path, *, apply: bool) -> bool:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "cross_validation_mode" in data and "cross_validation_note" in data:
        return False
    data.setdefault("cross_validation_mode", MODE)
    data.setdefault("cross_validation_note", NOTE)
    if apply:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outputs_root", type=Path)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()

    if not args.outputs_root.is_dir():
        sys.exit(f"error: {args.outputs_root} is not a directory")

    targets = sorted(args.outputs_root.rglob("ocr_result*.json"))
    changed = [p for p in targets if backfill_file(p, apply=args.apply)]
    verb = "stamped" if args.apply else "would stamp"
    for p in changed:
        print(f"{verb}: {p}")
    print(f"{len(changed)} file(s) {verb}; {len(targets) - len(changed)} already had the field")
    if not args.apply and changed:
        print("dry run -- re-run with --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
