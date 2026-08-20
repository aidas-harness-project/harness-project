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
       `python tools/dao.py set-ledger-status CASE_XXX <file> approved --reviewer <name> --operation-id <id> --held-by <name> --run-id RUN_ID`
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
import os
import re
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import dao
from dao import (
    case_dir, atomic_write_json, now_iso, source_ledger_path, load_json,
    acquire_lock_blocking, release_lock,
)
from _validation import load_registry, validate_instance
from llm_providers import (
    DEFAULT_PROVIDER,
    ProviderConfig,
    ProviderConfigError,
    ProviderExecutionError,
    SUPPORTED_PROVIDERS,
    build_provider,
)
from ocr_extract import scratch_dir, split_to_page_images
# tools/trace.py, not the stdlib `trace` module.
import trace as trace_mod

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))

# Default filename patterns treated as ground truth (evaluation-only, never model input)
DEFAULT_GT_PATTERNS = ["*손해사정서*", "*지급 근거*", "*지급내역*"]
IGNORE = {".DS_Store", "Thumbs.db"}

# ------------------------------------------- adjuster-supplied case type --


def _axis_values(def_name: str) -> list[str]:
    """Reads an axis enum out of case_type_result.schema.json rather than
    restating it here. The taxonomy has drifted between a writer and a checker
    before (the forbidden-expression table, 2026-07-17); a CLI choices list is
    exactly that shape of duplicate, so it is derived, not copied."""
    schema = json.loads(
        (ROOT / "schemas" / "case_type_result.schema.json").read_text(encoding="utf-8")
    )
    return [v for v in schema["$defs"][def_name]["enum"] if v is not None]


COVERAGE_BASIS_VALUES = _axis_values("coverage_basis")
LOSS_TYPE_VALUES = _axis_values("loss_type")

# 실손 has no templates/registry.json contract, so a draft for one cannot render.
# Accepting it at intake is fine -- silently accepting it and failing at draft
# time is not, so intake says so up front (open-decisions.md #8a).
LOSS_TYPES_WITHOUT_TEMPLATE = ["실손"]


def build_adjuster_case_type(args) -> dict | None:
    """Turns the case-type CLI arguments into the manifest's
    `adjuster_case_type` object, or None when the adjuster supplied nothing.

    Both axes are required together: a half-classified claim would let the
    draft stage select a template from one axis alone. `supplied_by` is
    mandatory whenever anything is supplied, because that provenance is what
    stands in for the document quote P1 would otherwise demand -- no document
    states a case's type, so an unattributed one is unverifiable."""
    supplied = [args.coverage_basis, args.loss_type]
    if args.non_claim_case:
        if any(v is not None for v in supplied):
            sys.exit("error: --non-claim-case means the material carries no claim at all, so it cannot "
                     "also carry --coverage-basis/--loss-type. Drop whichever is wrong.")
        if not args.supplied_by:
            sys.exit("error: --non-claim-case needs --supplied-by -- recording who judged this to be "
                     "non-claim material is the whole provenance of the claim.")
        return {
            "coverage_basis": None, "loss_type": None, "is_claim_case": False,
            "supplied_by": args.supplied_by, "supplied_at": now_iso(),
            **({"note": args.case_type_note} if args.case_type_note else {}),
        }

    if all(v is None for v in supplied):
        if args.supplied_by or args.case_type_note:
            sys.exit("error: --supplied-by/--case-type-note only mean something alongside a case type. "
                     "Add --coverage-basis and --loss-type (or --non-claim-case).")
        return None

    if any(v is None for v in supplied):
        sys.exit("error: --coverage-basis and --loss-type must be given together. They are two independent "
                 "axes (보상 근거 / 손해 유형) and a claim has a value on each; supplying one alone would "
                 "leave the case half-classified. See open-decisions.md #8a.")
    if not args.supplied_by:
        sys.exit("error: an adjuster-supplied case type needs --supplied-by. No document states a case's "
                 "type, so the adjuster's attribution is the provenance that stands in for a source quote.")

    if args.loss_type in LOSS_TYPES_WITHOUT_TEMPLATE:
        print(f"warning: loss_type '{args.loss_type}' has no templates/registry.json contract yet, so the "
              f"draft stage will fail at render time for this case. Recording it anyway -- surfacing this "
              f"now rather than at draft time is deliberate (open-decisions.md #8a).", file=sys.stderr)

    return {
        "coverage_basis": args.coverage_basis, "loss_type": args.loss_type, "is_claim_case": True,
        "supplied_by": args.supplied_by, "supplied_at": now_iso(),
        **({"note": args.case_type_note} if args.case_type_note else {}),
    }

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


def write_manifest(case_id: str, run_id: str, documents: list[dict],
                   adjuster_case_type: dict | None = None) -> Path:
    """Writes document_manifest.json the same way dao.py write-contract does
    -- lock, schema-validate, atomic write, release -- reusing its helpers
    directly rather than shelling out to itself.

    `adjuster_case_type` is omitted entirely when the adjuster supplied none,
    so a manifest written without it is byte-identical to one written before
    the field existed."""
    target = case_dir(case_id) / "document_manifest.json"
    manifest = {
        "case_id": case_id, "created_at": now_iso(), "updated_at": now_iso(),
        "documents": documents,
    }
    if adjuster_case_type is not None:
        manifest["adjuster_case_type"] = adjuster_case_type
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
        "ledger_version": "source_ledger.v0.4",
        "case_id": case_id, "source_dir": str(case_dir_path),
        "created_at": now_iso(), "updated_at": now_iso(), "files": files,
        "history_boundary": dao.make_history_boundary(files, mode="native"),
        "operations": [],
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
    ap.add_argument("--coverage-basis", choices=COVERAGE_BASIS_VALUES, metavar="BASIS",
                    help="Adjuster-supplied 보상 근거 (%s). Requires --loss-type and --supplied-by."
                         % "|".join(COVERAGE_BASIS_VALUES))
    ap.add_argument("--loss-type", choices=LOSS_TYPE_VALUES, metavar="TYPE",
                    help="Adjuster-supplied 손해 유형 (%s). Requires --coverage-basis and --supplied-by."
                         % "|".join(LOSS_TYPE_VALUES))
    ap.add_argument("--non-claim-case", action="store_true",
                    help="This material carries no claim at all (policy/증권/청약 only). Mutually exclusive with "
                         "--coverage-basis/--loss-type; still requires --supplied-by.")
    ap.add_argument("--supplied-by", metavar="NAME",
                    help="Who supplied the case type. Mandatory whenever a type (or --non-claim-case) is given: it "
                         "is the provenance that stands in for a document quote, since no document states a case's type.")
    ap.add_argument("--case-type-note", metavar="TEXT",
                    help="Optional free-text context from the adjuster about the case type.")
    args = ap.parse_args()
    # Switch tracing on before any instrumented path runs. This tool does not
    # raise spans itself, but the dao/provider calls below do -- and without
    # this they are discarded silently (see trace.configure_from_args).
    trace_mod.configure_from_args(args)

    adjuster_case_type = build_adjuster_case_type(args)

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

        # D2's vision content pre-check was removed 2026-08-20 (PoC
        # owner's decision). The per-file HUMAN review gate below is
        # unchanged: every entry is still written 'pending', and
        # --execute still refuses while any file is unapproved. The scan
        # was advisory only -- it never auto-rejected a file -- cost ~41s
        # on a 4-PDF case, and read raw pre-redaction pages.
        # `content_warnings` stays an accepted input so a ledger written
        # while the scan ran still validates and still shows its warning.
        content_warnings = {}
        ledger = build_ledger(args.case_id, src_dir, plan, splits, content_warnings)
        case_dir(args.case_id)
        atomic_write_json(ledger_path, ledger)
        print(f"\nWrote {ledger_path} -- every file is 'pending'. "
              f"A human must review and set each to approved/rejected via "
              f"`python tools/dao.py set-ledger-status {args.case_id} <file> approved --reviewer <name> --operation-id <id> --held-by <name> --run-id RUN_ID` "
              f"before --execute will run.")
        return

    if not args.execute:
        print("\n(dry run) pass --init-ledger to create the review ledger, or --execute to copy once it's approved.")
        return

    try:
        ledger = dao.validated_source_ledger(args.case_id)
    except ValueError as exc:
        sys.exit(f"error: source ledger is not executable: {exc}")
    pending = [e["file_name"] for e in ledger["files"] if e["review_status"] == "pending"]
    rejected = [e["file_name"] for e in ledger["files"] if e["review_status"] == "rejected"]
    if pending or rejected:
        print("BLOCKED: cannot execute -- not every file is approved.")
        if pending:
            print(f"  pending: {pending}")
        if rejected:
            print(f"  rejected: {rejected} -- resolve before any file in this case can copy.")
        sys.exit(1)

    # D1 guard (fleet review): the raw/gt classification here is recomputed from
    # the CURRENT args, which may differ from what was reviewed at --init-ledger
    # (e.g. a changed --ground-truth pattern). Refuse to copy a file to a
    # destination that disagrees with its APPROVED ledger classification -- else a
    # file reviewed as ground_truth (the answer key) could be copied into
    # data/raw and fed to a model. Also refuse any file that has no reviewed
    # ledger entry at all (added to the folder after the ledger was created).
    ledger_class = {e["file_name"]: e.get("classification") for e in ledger["files"]}
    drift = []
    for p in raw:
        if p.name not in ledger_class:
            drift.append(f"{p.name}: staged for data/raw but has no reviewed ledger entry")
        elif ledger_class[p.name] != "raw":
            drift.append(f"{p.name}: reviewed as {ledger_class[p.name]!r} but would copy to data/raw")
    for p in gt:
        if p.name not in ledger_class:
            drift.append(f"{p.name}: staged for data/ground_truth but has no reviewed ledger entry")
        elif ledger_class[p.name] != "ground_truth":
            drift.append(f"{p.name}: reviewed as {ledger_class[p.name]!r} but would copy to data/ground_truth")
    if drift:
        print("BLOCKED: intake classification drifted from the reviewed ledger (D1):")
        for d in drift:
            print(f"  {d}")
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
        file_format = file_format_for(f.suffix)
        manifest_documents.append({
            "document_id": doc_id, "file_name": dest.name, "file_path": f"data/raw/{args.case_id}/{dest.name}",
            "file_format": file_format, "file_size_bytes": dest.stat().st_size,
            "pre_flagged_type": None, "pages": None, "ocr_status": "pending",
            "segmentation_status": "pending_review" if file_format == "pdf" else "not_applicable",
            "segmentation_reviewed_by": None, "segmentation_reviewed_at": None,
            "segmentation_review_note": None,
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
                    "segmentation_status": "pending_review",
                    "segmentation_reviewed_by": None, "segmentation_reviewed_at": None,
                    "segmentation_review_note": None,
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

    manifest_path = write_manifest(args.case_id, args.run_id or f"RUN_{datetime.now(KST).strftime('%Y%m%d')}_INTAKE",
                                   manifest_documents, adjuster_case_type)
    print(f"Wrote {manifest_path} ({len(manifest_documents)} document(s), sequential DOC_XXX ids -- "
          f"original filenames are not preserved past this point, see _intake_record.json for the crosswalk).")
    if adjuster_case_type is not None:
        if adjuster_case_type["is_claim_case"]:
            print(f"  adjuster case type: {adjuster_case_type['coverage_basis']} / "
                  f"{adjuster_case_type['loss_type']} (supplied by {adjuster_case_type['supplied_by']})")
        else:
            print(f"  adjuster case type: non-claim material, no type on either axis "
                  f"(supplied by {adjuster_case_type['supplied_by']})")

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
