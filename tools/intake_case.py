"""Copies a source-cases/ case folder into pipeline input, isolating ground
truth via a per-file, human-approved ledger (harness-guardrails-dev D2).

Workflow:
    1. Dry run (default): propose a raw/ground_truth classification per file,
       by filename pattern. Nothing is written yet.
    2. --init-ledger: for every file proposed as 'raw' (PDFs only -- see
       below), run a cheap content pre-check (one vision call over the
       document's first few pages) before writing the ledger -- filename
       patterns alone missed a real case (see known-gaps.md item 2:
       CASE_002's DOC_002/DOC_003, filenames looked like plain claim docs
       but were actually completed third-party loss-adjustment reports with
       stated payout figures). A flagged file gets `content_warning` set in
       its ledger entry -- this does NOT auto-reject it, it makes the risk
       visible for the human review step below, which is still mandatory
       either way. Writes outputs/CASE_XXX/_source_ledger.json with every
       file's proposed classification and review_status: pending.

       Scope of the content pre-check, deliberately narrow: only files
       proposed as 'raw' (a file already proposed as ground_truth is
       already headed for isolation, not the risk this catches), only PDFs
       (the only format this project's raw case files come in; a .txt/.md
       file's content is already inspectable directly if that becomes
       relevant later), and NOT --split-derived files (those are carved
       from a source PDF only at --execute time, after ledger approval --
       a --split spec needs to be reviewed with its page ranges in mind
       regardless, so this check doesn't apply to them). This is a
       classification SIGNAL over the first few pages, not a full read --
       document-pipeline's checkpoint 1 still owns real OCR + P8
       cross-validation over the whole document.
    3. A human reviews the plan (and any content_warning) and sets each
       file's status via
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
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from dao import (
    case_dir, atomic_write_json, now_iso, source_ledger_path, load_json,
    acquire_lock_blocking, release_lock,
)
from _validation import load_registry, validate_instance
from ocr_extract import scratch_dir, split_to_page_images

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))

# Default filename patterns treated as ground truth (evaluation-only, never model input)
DEFAULT_GT_PATTERNS = ["*손해사정서*", "*지급 근거*", "*지급내역*"]
IGNORE = {".DS_Store", "Thumbs.db"}

# --------------------------------------------------- content pre-check --

CONTENT_SCAN_PAGES = 5

CONTENT_SCAN_PROMPT = (
    "You are given up to {n} page images from the START of a document -- a "
    "candidate input file for an insurance loss-adjustment pipeline. Determine "
    "whether this document is, or contains, a COMPLETED, submitted professional "
    "loss-adjustment report -- i.e. a document where a licensed adjuster "
    "(손해사정사) has already reached and stated a final conclusion and/or "
    "payout amount for an insurance claim. Typical indicators: titles like "
    "보험금사정서/손해사정서, section headers like 사정 결과, 사정 요약, "
    "보험금 사정내역, 사정 의견, an adjuster's license number or stamp, a "
    "위임장 granting loss-adjustment authority to a firm, a stated 지급 금액 "
    "or 사정금액 in 원 with a specific figure. This is DIFFERENT from an "
    "ordinary claim document (diagnosis certificate, medical record, "
    "insurance policy, plain claim form, denial notice) which does NOT "
    "contain a professional's own completed adjustment conclusion.\n\n"
    "Reply with exactly one line: FLAGGED: <brief reason, quoting the "
    "specific text you saw> or CLEAR."
)

FLAGGED_RE = re.compile(r"\bFLAGGED\b")
CLEAR_RE = re.compile(r"\bCLEAR\b")


def _parse_content_scan_verdict(response_text: str) -> dict:
    """Pure parsing, kept separate from the subprocess call so it's testable
    without real page images or a real claude CLI. Fails safe toward
    flagged=True on anything unparseable -- this is a safety check, not a
    productivity one, so an ambiguous response should mean "a human looks
    at this," not "silently wave it through." Same discipline as
    ocr_extract.compare()'s fail-toward-disagreed rule."""
    text = response_text.strip()
    text_upper = text.upper()
    flagged_match = FLAGGED_RE.search(text_upper)
    clear_match = CLEAR_RE.search(text_upper)
    if flagged_match:
        return {"flagged": True, "evidence": text}
    if clear_match:
        return {"flagged": False, "evidence": None}
    return {"flagged": True, "evidence": f"unparseable content-scan response, flagged for safety: {text!r}"}


def scan_for_answer_key_content(pdf_path: Path, case_id: str, index: int, n_pages: int = CONTENT_SCAN_PAGES) -> dict:
    """One cheap vision call over the document's first n_pages -- a
    classification SIGNAL for intake's human reviewer, not a full read.
    See known-gaps.md item 2 (the CASE_002 incident this exists to catch)
    and this module's docstring for scope."""
    with scratch_dir(case_id, f"INTAKE_{index:03d}") as tmp_dir:
        page_paths = split_to_page_images(pdf_path, tmp_dir, max_pages=n_pages)
        image_refs = "\n".join(f"Page {i + 1} image: {p}" for i, p in enumerate(page_paths))
        prompt = f"{CONTENT_SCAN_PROMPT.format(n=n_pages)}\n\n{image_refs}"
        result = subprocess.run(
            ["claude", "-p", prompt, "--allowedTools", "Read"],
            capture_output=True, text=True, timeout=180, cwd=str(ROOT),
        )
        if result.returncode != 0:
            sys.exit(f"error: content-scan claude call failed for {pdf_path.name}: {result.stderr.strip()}")
        verdict = _parse_content_scan_verdict(result.stdout)
        verdict["pages_checked"] = len(page_paths)
        return verdict


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


FORMAT_BY_EXT = {
    ".pdf": "pdf", ".png": "image", ".jpg": "image", ".jpeg": "image", ".tiff": "image",
    ".txt": "text", ".md": "text", ".xlsx": "spreadsheet", ".csv": "spreadsheet",
}


def file_format_for(ext: str) -> str:
    return FORMAT_BY_EXT.get(ext.lower(), "other")


def write_manifest(case_id: str, run_id: str, documents: list[dict]) -> Path:
    """Writes document_manifest.json the same way dao.py write-contract does
    -- lock, schema-validate, atomic write, release -- reusing its helpers
    directly rather than shelling out to itself."""
    target = case_dir(case_id) / "document_manifest.json"
    manifest = {
        "case_id": case_id, "created_at": now_iso(), "updated_at": now_iso(),
        "documents": documents,
    }
    existing_lock = acquire_lock_blocking(target, "intake_case.py", run_id, "write document_manifest.json")
    if existing_lock is not None:
        sys.exit(f"error: {target} is locked by {existing_lock['held_by']} (run {existing_lock['run_id']}) -- "
                  f"not writing the manifest.")
    try:
        schemas, registry = load_registry()
        errors = validate_instance(manifest, "document_manifest.schema.json", schemas, registry)
        if errors:
            sys.exit("error: document_manifest failed its own schema validation -- this is an intake_case.py "
                      "bug, not a data problem:\n" + "\n".join(f"  - {e}" for e in errors))
        atomic_write_json(target, manifest)
        return target
    finally:
        release_lock(target)


def build_ledger(case_id, case_dir_path, plan, splits, content_warnings=None):
    content_warnings = content_warnings or {}
    files = []
    for f, dest in plan:
        entry = {"file_name": f.name, "classification": dest, "review_status": "pending",
                 "reviewed_by": None, "reviewed_at": None, "rejection_reason": None}
        warning = content_warnings.get(f.name)
        if warning is not None:
            entry["content_warning"] = {
                "evidence": warning["evidence"], "pages_checked": warning["pages_checked"],
                "checked_at": now_iso(),
            }
        files.append(entry)
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
    ap.add_argument("--run-id", help="Only used for lock metadata on document_manifest.json; a fresh one is generated if omitted")
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

        pdf_raw = [f for f in raw if f.suffix.lower() == ".pdf"]
        content_warnings = {}
        if pdf_raw:
            print(f"\nContent pre-check: scanning {len(pdf_raw)} 'raw'-proposed PDF(s) for "
                  f"answer-key-class content (harness-guardrails-dev D2, known-gaps.md item 2) ...")
            for i, f in enumerate(pdf_raw, start=1):
                verdict = scan_for_answer_key_content(f, args.case_id, i)
                if verdict["flagged"]:
                    content_warnings[f.name] = verdict
                    print(f"  FLAGGED: {f.name}\n    {verdict['evidence']}")
            if content_warnings:
                print(f"\n*** {len(content_warnings)} file(s) proposed as 'raw' show signs of "
                      f"answer-key-class content -- see content_warning in the ledger. This does "
                      f"NOT auto-reject them; a human still reviews every file, but this one needs "
                      f"real attention before approving, not a rubber stamp. ***")
            else:
                print("  clear -- no answer-key-class content detected in the scanned pages.")

        ledger = build_ledger(args.case_id, src_dir, plan, splits, content_warnings)
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

    # Original filenames often carry PII (e.g. the claimant's name) even
    # though content redaction happens later -- renaming to a sequential
    # document_id here, at copy time, is what actually keeps that PII out
    # of data/raw/ and everything downstream (manifest, evidence citations)
    # that references files by document_id from this point on.
    raw_id_map = {}   # original file name -> (doc_id, dest_path)
    gt_id_map = {}
    manifest_documents = []

    for i, f in enumerate(sorted(raw, key=lambda p: p.name), start=1):
        doc_id = f"DOC_{i:03d}"
        dest = raw_dir / f"{doc_id}{f.suffix.lower()}"
        shutil.copy2(f, dest)
        raw_id_map[f.name] = (doc_id, dest)
        manifest_documents.append({
            "document_id": doc_id, "file_name": dest.name, "file_path": f"data/raw/{args.case_id}/{dest.name}",
            "file_format": file_format_for(f.suffix), "file_size_bytes": dest.stat().st_size,
            "pre_flagged_type": None, "pages": None, "ocr_status": "pending",
            "ocr_text_path": None, "ocr_quality": None, "uncertain_region_count": None,
            "cross_validation_status": None, "redacted_text_path": None,
            "document_type": None, "classification_confidence": None,
        })

    for i, f in enumerate(sorted(gt, key=lambda p: p.name), start=1):
        gt_id = f"GT_{i:03d}"
        dest = gt_dir / f"{gt_id}{f.suffix.lower()}"
        shutil.copy2(f, dest)
        gt_id_map[f.name] = (gt_id, dest)

    split_records = []
    for fname, (src, ranges, page_count) in splits.items():
        import fitz
        doc = fitz.open(src)
        for start, end, dest in ranges:
            out = fitz.open()
            out.insert_pdf(doc, from_page=start - 1, to_page=end - 1)
            if dest == "raw":
                doc_id = f"DOC_{len(raw_id_map) + 1:03d}"
                out_path = raw_dir / f"{doc_id}.pdf"
                raw_id_map[split_output_name(src, start, end)] = (doc_id, out_path)
                manifest_documents.append({
                    "document_id": doc_id, "file_name": out_path.name,
                    "file_path": f"data/raw/{args.case_id}/{out_path.name}",
                    "file_format": "pdf", "file_size_bytes": None,  # filled in after save() below
                    "pre_flagged_type": None, "pages": None, "ocr_status": "pending",
                    "ocr_text_path": None, "ocr_quality": None, "uncertain_region_count": None,
                    "cross_validation_status": None, "redacted_text_path": None,
                    "document_type": None, "classification_confidence": None,
                })
            else:
                gt_id = f"GT_{len(gt_id_map) + 1:03d}"
                out_path = gt_dir / f"{gt_id}.pdf"
                gt_id_map[split_output_name(src, start, end)] = (gt_id, out_path)
            out.save(out_path)
            out.close()
            if dest == "raw":
                manifest_documents[-1]["file_size_bytes"] = out_path.stat().st_size
            split_records.append({"source": fname, "pages": f"{start}-{end}", "dest": dest, "output": out_path.name})
        doc.close()

    manifest_path = write_manifest(args.case_id, args.run_id or f"RUN_{datetime.now(KST).strftime('%Y%m%d')}_INTAKE", manifest_documents)
    print(f"Wrote {manifest_path} ({len(manifest_documents)} document(s), sequential DOC_XXX ids -- "
          f"original filenames are not preserved past this point, see _intake_record.json for the crosswalk).")

    # This crosswalk (original filename -> assigned id) is the ONLY place the
    # original, potentially PII-bearing filenames are recorded past intake --
    # kept here for audit traceability, not read by any agent during normal
    # pipeline operation the way document_manifest.json is.
    record = {
        "case_id": args.case_id, "source": str(src_dir), "copied_at": now_iso(),
        "ground_truth_patterns": args.ground_truth, "file_patterns": args.files,
        "raw": [{"original_file_name": name, "document_id": doc_id, "file_name": dest.name}
                for name, (doc_id, dest) in raw_id_map.items()],
        "ground_truth": [{"original_file_name": name, "document_id": gt_id, "file_name": dest.name}
                         for name, (gt_id, dest) in gt_id_map.items()],
        "splits": split_records,
    }
    (raw_dir / "_intake_record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nCopy complete. Record: {raw_dir / '_intake_record.json'}")


if __name__ == "__main__":
    main()
