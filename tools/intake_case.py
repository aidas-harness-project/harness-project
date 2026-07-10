"""Copies a source-cases/ case folder into pipeline input, isolating ground
truth via a per-file, human-approved ledger (harness-guardrails-dev D2).

Workflow:
    1. Dry run (default): propose a raw/ground_truth classification per file,
       by filename pattern. Nothing is written yet.
    2. --init-ledger: write outputs/CASE_XXX/_source_ledger.json with every
       file's proposed classification and review_status: pending.
    3. A human reviews the plan and sets each file's status via
       `python tools/dao.py set-ledger-status CASE_XXX <file> approved --reviewer <name>`
       (or rejected --reason "...").
    4. --execute: copies files to data/raw/CASE_XXX/ and
       data/ground_truth/CASE_XXX/, but only if every ledger entry is
       approved (checked via the same logic as `dao.py check-source-ledger-clear`).
       A single rejected entry blocks the whole case -- nothing copies, not
       even already-approved files, until it's resolved.

source-cases/ is never modified, only copied from.

One file containing multiple documents (e.g. a report plus its supporting
evidence in one PDF) can be divided with --split, by page range, into
different destinations. Files covered by a --split spec skip filename-pattern
classification; pages not covered by any range are not copied (shown as
"excluded" in the dry run).

Usage:
    python tools/intake_case.py "source-cases/permanent-disability case" CASE_003
    python tools/intake_case.py "source-cases/permanent-disability case" CASE_003 --init-ledger
    python tools/intake_case.py "source-cases/permanent-disability case" CASE_003 --execute
    python tools/intake_case.py "source-cases/..." CASE_001 --ground-truth "*손해사정서*" "*지급*" --init-ledger
    python tools/intake_case.py "source-cases/permanent-disability case" CASE_003 \
        --files "배상-상완골*" \
        --split "배상-상완골 근위부 골절OP (김태윤) - 고객정보 삭제.pdf:1-13=ground_truth,14-110=raw" \
        --init-ledger
"""
import argparse
import fnmatch
import json
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from dao import case_dir, atomic_write_json, now_iso, source_ledger_path, load_json

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))

# Default filename patterns treated as ground truth (evaluation-only, never model input)
DEFAULT_GT_PATTERNS = ["*손해사정서*", "*지급 근거*", "*지급내역*"]
IGNORE = {".DS_Store", "Thumbs.db"}


def classify(files, gt_patterns):
    plan = []
    for f in files:
        if f.name in IGNORE:
            continue
        is_gt = any(fnmatch.fnmatch(f.name, pat) for pat in gt_patterns)
        plan.append((f, "ground_truth" if is_gt else "raw"))
    return plan


def parse_split_spec(spec):
    """'filename.pdf:1-13=ground_truth,14-110=raw' -> (filename, [(1, 13, dest), ...])

    Pages are 1-based, ranges inclusive on both ends. Split on the last ':'
    since Windows filenames can't contain ':' anyway.
    """
    if ":" not in spec:
        sys.exit(f"error: --split format is 'filename:start-end=tier,...' -- {spec}")
    fname, ranges_part = spec.rsplit(":", 1)
    ranges = []
    for part in ranges_part.split(","):
        try:
            rng, dest = part.split("=")
            start, end = (int(x) for x in rng.split("-"))
        except ValueError:
            sys.exit(f"error: --split range format is 'start-end=tier' -- {part!r}")
        if dest not in ("raw", "ground_truth"):
            sys.exit(f"error: --split tier must be raw|ground_truth -- {dest!r}")
        if not 1 <= start <= end:
            sys.exit(f"error: --split page range looks wrong -- {part!r}")
        ranges.append((start, end, dest))
    ranges.sort()
    for (_, e1, _), (s2, _, _) in zip(ranges, ranges[1:]):
        if s2 <= e1:
            sys.exit(f"error: --split ranges overlap -- {fname}")
    return fname, ranges


def split_output_name(src, start, end):
    return f"{src.stem}__p{start:03d}-{end:03d}{src.suffix}"


def build_ledger(case_id, case_dir_path, plan, splits):
    files = []
    for f, dest in plan:
        files.append({"file_name": f.name, "classification": dest, "review_status": "pending",
                      "reviewed_by": None, "reviewed_at": None, "rejection_reason": None})
    for fname, (src, ranges, _pc) in splits.items():
        for start, end, dest in ranges:
            out_name = split_output_name(src, start, end)
            files.append({"file_name": out_name, "classification": dest, "review_status": "pending",
                          "reviewed_by": None, "reviewed_at": None, "rejection_reason": None})
    return {
        "case_id": case_id, "source_dir": str(case_dir_path),
        "created_at": now_iso(), "updated_at": now_iso(), "files": files,
    }


def main():
    ap = argparse.ArgumentParser(description="source-cases -> pipeline input intake (ground-truth isolation via D2 ledger)",
                                  formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("case_dir", help="Path to the source-cases/ case folder")
    ap.add_argument("case_id", help="e.g. CASE_001")
    ap.add_argument("--ground-truth", nargs="+", default=DEFAULT_GT_PATTERNS,
                    metavar="PATTERN", help=f"Ground-truth filename patterns (default: {DEFAULT_GT_PATTERNS})")
    ap.add_argument("--files", nargs="+", metavar="PATTERN",
                    help="Only intake files matching these patterns (default: whole folder)")
    ap.add_argument("--split", nargs="+", default=[], metavar="SPEC",
                    help="Page-range split: 'filename:1-13=ground_truth,14-110=raw'")
    ap.add_argument("--init-ledger", action="store_true", help="Write _source_ledger.json with the proposed plan (all pending)")
    ap.add_argument("--execute", action="store_true", help="Copy files, but only if the ledger is fully approved")
    args = ap.parse_args()

    src_dir = Path(args.case_dir)
    if not src_dir.is_dir():
        sys.exit(f"error: case folder not found -- {src_dir}")
    if not args.case_id.startswith("CASE_"):
        sys.exit("error: case_id needs a CASE_ prefix (e.g. CASE_001)")

    files = sorted(p for p in src_dir.rglob("*") if p.is_file())
    if args.files:
        files = [f for f in files if any(fnmatch.fnmatch(f.name, pat) for pat in args.files)]
        if not files:
            sys.exit(f"error: no files match --files patterns -- {args.files}")

    splits = {}
    for spec in args.split:
        fname, ranges = parse_split_spec(spec)
        src = next((f for f in files if f.name == fname), None)
        if src is None:
            sys.exit(f"error: --split target not in the intake file list -- {fname}")
        try:
            import fitz  # pymupdf -- only needed when --split is used
        except ImportError:
            sys.exit("error: --split needs pymupdf -- pip install pymupdf")
        page_count = fitz.open(src).page_count
        if ranges[-1][1] > page_count:
            sys.exit(f"error: --split range exceeds the {page_count}p document -- {fname}")
        splits[fname] = (src, ranges, page_count)

    plan = classify([f for f in files if f.name not in splits], args.ground_truth)
    gt = [f for f, dest in plan if dest == "ground_truth"]
    raw = [f for f, dest in plan if dest == "raw"]

    print(f"Case: {src_dir.name} -> {args.case_id}")
    print(f"\n[Model input -> data/raw/{args.case_id}/]  {len(raw)} file(s)")
    for f in raw:
        print(f"  - {f.name}")
    print(f"\n[Ground truth, isolated -> data/ground_truth/{args.case_id}/]  {len(gt)} file(s)")
    for f in gt:
        print(f"  - {f.name}")

    for fname, (src, ranges, page_count) in splits.items():
        print(f"\n[split] {fname} ({page_count}p total)")
        covered = set()
        for start, end, dest in ranges:
            covered.update(range(start, end + 1))
            print(f"  - p{start}-{end} -> {dest}/{split_output_name(src, start, end)}")
        excluded = sorted(set(range(1, page_count + 1)) - covered)
        if excluded:
            print(f"  - excluded (not copied): {len(excluded)}p -- {excluded}")

    has_gt = gt or any(d == "ground_truth" for _, rs, _ in splits.values() for *_, d in rs)
    if not has_gt:
        print("\nwarning: 0 files classified as ground truth. Check the patterns -- "
              "a ground-truth file leaking into raw contaminates evaluation.")

    if args.init_ledger:
        ledger_path = source_ledger_path(args.case_id)
        if ledger_path.exists():
            sys.exit(f"error: ledger already exists at {ledger_path} -- resolve/clear existing entries via "
                      f"`python tools/dao.py set-ledger-status` rather than overwriting it.")
        ledger = build_ledger(args.case_id, src_dir, plan, splits)
        case_dir(args.case_id)
        atomic_write_json(ledger_path, ledger)
        print(f"\nWrote {ledger_path} -- every file is 'pending'. "
              f"A human must review and set each to approved/rejected via "
              f"`python tools/dao.py set-ledger-status {args.case_id} <file> approved --reviewer <name>` "
              f"before --execute will run.")
        return

    if not args.execute:
        print("\n(dry run) pass --init-ledger to create the review ledger, or --execute to copy once it's approved.")
        return

    ledger = load_json(source_ledger_path(args.case_id))
    if ledger is None:
        sys.exit("error: no _source_ledger.json found -- run with --init-ledger first.")
    pending = [e["file_name"] for e in ledger["files"] if e["review_status"] == "pending"]
    rejected = [e["file_name"] for e in ledger["files"] if e["review_status"] == "rejected"]
    if pending or rejected:
        print("BLOCKED: cannot execute -- not every file is approved.")
        if pending:
            print(f"  pending: {pending}")
        if rejected:
            print(f"  rejected: {rejected} -- resolve before any file in this case can copy.")
        sys.exit(1)

    raw_dir = ROOT / "data" / "raw" / args.case_id
    gt_dir = ROOT / "data" / "ground_truth" / args.case_id
    dest_dirs = {"raw": raw_dir, "ground_truth": gt_dir}
    for d in (raw_dir, gt_dir):
        d.mkdir(parents=True, exist_ok=True)
    for f in raw:
        shutil.copy2(f, raw_dir / f.name)
    for f in gt:
        shutil.copy2(f, gt_dir / f.name)

    split_records = []
    for fname, (src, ranges, page_count) in splits.items():
        import fitz
        doc = fitz.open(src)
        for start, end, dest in ranges:
            out = fitz.open()
            out.insert_pdf(doc, from_page=start - 1, to_page=end - 1)
            out_name = split_output_name(src, start, end)
            out.save(dest_dirs[dest] / out_name)
            out.close()
            split_records.append({"source": fname, "pages": f"{start}-{end}", "dest": dest, "output": out_name})
        doc.close()

    record = {
        "case_id": args.case_id, "source": str(src_dir), "copied_at": now_iso(),
        "ground_truth_patterns": args.ground_truth, "file_patterns": args.files,
        "raw": [f.name for f in raw], "ground_truth": [f.name for f in gt], "splits": split_records,
    }
    (raw_dir / "_intake_record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nCopy complete. Record: {raw_dir / '_intake_record.json'}")


if __name__ == "__main__":
    main()
