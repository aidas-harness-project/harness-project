"""Checkpoint 1 across every document in a case, with document-level parallelism (T8).

Why this exists as a separate tool rather than a loop inside run_checkpoint1.py:
run_checkpoint1 processes exactly ONE document per invocation, and nothing in
this repository ever looped over documents -- the agent did, by reading
document-pipeline.md and invoking the tool once per document. The
serialization was therefore in a SPEC, not in code, and no amount of editing
run_checkpoint1 would have removed it. This tool is the missing driver.

Measured on CASE_953 (fork of CASE_907; 5 documents, 6 pages each, 8 page
workers), real claude-cli calls, against a sequential baseline of 154.7s:

    doc workers   in-flight cap   wall     speedup   observed peak
        2               6         125.9s    1.23x     6/6  (pinned)
        2              16          98.1s    1.58x    12/16
        3              16          61.9s    2.50x    16/16

The theoretical floor is the slowest single document (53.5s -> 2.89x), so 3
workers reach 86% of what document parallelism can win on that case. The cap
matters as much as the worker count: at 6 the per-document time SUM inflated
154.6s -> 214.4s, i.e. concurrency was handed straight back as queueing.

FAILURE SEMANTICS -- deliberately the OPPOSITE of redaction's (T4c).
A P8 disagreement blocks its own document and the others keep going. Redaction
hard-fails the whole document on a possible leak because a leak is a privacy
event whose blast radius is the document; a P8 disagreement is a per-document
extraction failure whose blast radius is that document alone. P8's "no
tolerance threshold, report immediately" is about never accepting a disagreed
page as text -- which still holds exactly: the blocked document writes no page
text, is reported in the summary, and this tool exits non-zero. Letting
document B finish does not make document A's disagreement any less blocking.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import threading
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import dao as _dao
# tools/trace.py, not the stdlib `trace` module.
import trace as trace_mod
from llm_providers import SUPPORTED_PROVIDERS
import redact_document as redact_document_mod
from run_checkpoint1 import (
    build_classifier_provider,
    classify_existing,
    run_checkpoint1,
)

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DOC_WORKERS = 3
DOC_WORKERS_ENV = "HARNESS_DOC_WORKERS"

# Statuses that do NOT represent a failure of this step. Kept as an explicit
# allowlist rather than "not blocked_*" so a status added upstream shows up as
# an unknown value to look at instead of being silently counted as a pass.
#
# `already_extracted` belongs here even though no work was done: it is
# run_checkpoint1 REFUSING to re-OCR a document that already has extracted
# text, which is the guard against paying twice, not an extraction failure.
# Found by running the driver for real -- treating it as blocked made a
# perfectly healthy re-run exit non-zero. `blocked_segmentation` is likewise a
# refusal (the retained superseded bundle, whose children own its pages), and
# select_documents already filters those, so reaching one here means the
# manifest disagrees with the filter and it is left OUT of this set on
# purpose: that is worth surfacing.
_OK_STATUSES = {"success", "passed", "bundle_ocr_complete", "already_extracted"}


def _resolve_doc_workers(explicit: int | None) -> int:
    """Explicit argument wins, then HARNESS_DOC_WORKERS, then the default.
    A value <= 1 restores the strictly sequential loop."""
    if explicit is None:
        raw = os.environ.get(DOC_WORKERS_ENV, "")
        try:
            explicit = int(raw) if raw.strip() else None
        except ValueError:
            explicit = None
    if explicit is None:
        return DEFAULT_DOC_WORKERS
    return max(1, explicit)


def _pdf_path_for(case_id: str, doc: dict) -> Path:
    """The raw PDF for a manifest entry, via the manifest's own file_path when
    present so this tool does not re-derive a layout convention that already
    lives in the manifest."""
    file_path = doc.get("file_path")
    if file_path:
        return ROOT / file_path
    return ROOT / "data" / "raw" / case_id / (doc.get("file_name") or f"{doc['document_id']}.pdf")


def select_documents(manifest: dict, only: list[str] | None = None) -> list[dict]:
    """Documents this stage should process, in manifest order.

    Skips what checkpoint 1 would refuse anyway, so a refusal is not reported
    as a failure: the retained superseded bundle (its children own its pages)
    and anything already carrying completed OCR. run_checkpoint1 re-checks both
    itself -- this is a cheap pre-filter, never the authority.

    The bundle test reads `downstream_disposition`, NOT `segmentation_status`.
    It was written against the latter, where `superseded_bundle` is not even a
    permitted value (the enum is pending_review/required/not_required/
    completed/not_applicable) -- so the line could never match and the filter
    was dead. The bundle then reached run_checkpoint1, which correctly refused
    it with `blocked_segmentation`, and that correct refusal was reported as a
    blocking failure of the whole stage.
    """
    out = []
    for doc in manifest.get("documents", []):
        doc_id = doc.get("document_id")
        if only and doc_id not in only:
            continue
        if doc.get("downstream_disposition") == "superseded_bundle":
            continue
        if doc.get("ocr_status") == "completed":
            continue
        out.append(doc)
    return out


def select_redaction_documents(manifest: dict,
                               only: list[str] | None = None) -> list[dict]:
    """Documents checkpoint 2 should redact, in manifest order.

    Skips what redaction would refuse or has already done, so a skip is never
    reported as a failure:

    * the retained superseded bundle -- its children carry its pages;
    * `expert_review_only` -- checkpoint 2 is not applicable, and feeding a raw
      image to the text redactor is exactly what that disposition forbids;
    * anything already carrying a `redacted_text_path`;
    * anything whose OCR has not completed -- there is no page text to redact.

    redact_document re-checks the cross-validation gate itself; this is a cheap
    pre-filter, never the authority.

    The bundle test reads `downstream_disposition`, not `segmentation_status` --
    see select_documents for why the latter can never match. Here the mistake
    was masked: a superseded bundle also has `ocr_status: not_applicable` and
    a null `redacted_text_path`, so the later filters excluded it anyway and
    the dead line looked like it was working.
    """
    out = []
    for doc in manifest.get("documents", []):
        doc_id = doc.get("document_id")
        if only and doc_id not in only:
            continue
        if doc.get("downstream_disposition") == "superseded_bundle":
            continue
        if doc.get("downstream_disposition") == "expert_review_only":
            continue
        if doc.get("redacted_text_path"):
            continue
        if doc.get("ocr_status") not in {"completed", "pending"}:
            continue
        out.append(doc)
    return out


def select_classification_documents(manifest: dict,
                                     only: list[str] | None = None) -> list[dict]:
    """Documents that still owe a `document_type`, in manifest order.

    Classification runs AFTER redaction, not as checkpoint 1's tail, so that
    the classifier reads `redacted_text.md` rather than the raw page. The
    fallback in `_classification_input_text` still exists for a document that
    genuinely has no redacted layer, but ordering the work this way means it is
    reached by exception rather than by every top-level document on every run
    (CASE_909/911/961/962 each recorded 4-5 `raw_page_text` classifications,
    one per top-level document, every single time).

    Excluded, each because a type here would be wrong rather than merely
    redundant:

    * the retained superseded bundle -- its children carry its pages;
    * a PDF still awaiting its proposal/split (`segmentation_status` is
      `pending_review` or `required`) --
      `document_type` is a per-document value and one label cannot be right
      for a bundle mixing a 진단서, a 검사보고서 and a 진료비 명세서;
    * `expert_review_only` -- `resolve_as_non_text` already assigned its type
      without a classifier, and there is no text to classify;
    * anything already carrying a `document_type`;
    * anything whose OCR has not completed -- there is no page 1 text yet.
    """
    out = []
    for doc in manifest.get("documents", []):
        doc_id = doc.get("document_id")
        if only and doc_id not in only:
            continue
        if doc.get("downstream_disposition") == "superseded_bundle":
            continue
        if doc.get("downstream_disposition") == "expert_review_only":
            continue
        if doc.get("segmentation_status") in {"pending_review", "required"}:
            continue
        if doc.get("document_type"):
            continue
        if doc.get("ocr_status") != "completed":
            continue
        out.append(doc)
    return out


def run_classification_stage(
    case_id: str,
    held_by: str,
    run_id: str,
    *,
    doc_workers: int | None = None,
    only: list[str] | None = None,
    progress=None,
    classifier=None,
) -> dict:
    """Classify every document that owes a `document_type`, after redaction.

    Same per-document isolation as the other two stages: one document failing
    reports itself and the siblings finish. Uses `classify_existing`, the same
    entry point a split child takes -- a document whose pages are already on
    disk must never be re-OCR'd to obtain a label.
    """
    report = progress or (lambda msg: print(msg, file=sys.stderr, flush=True))

    manifest = _dao.read_contract_data(case_id, "document_manifest.json")
    if not manifest:
        return {"status": "failed", "case_id": case_id,
                "error": f"no document_manifest.json for {case_id}", "documents": []}

    targets = select_classification_documents(manifest, only)
    if not targets:
        return {"status": "success", "case_id": case_id, "documents": [],
                "note": "no documents required classification"}

    workers = _resolve_doc_workers(doc_workers)
    report(f"classification: {len(targets)} document(s), {workers} document worker(s)")

    slots: list[dict | None] = [None] * len(targets)
    lock = threading.Lock()

    def process(index: int, doc: dict) -> None:
        doc_id = doc["document_id"]
        with trace_mod.span("stage.classification", category="compute",
                            case_id=case_id, doc_id=doc_id):
            try:
                result = classify_existing(case_id, doc_id, held_by=held_by,
                                            run_id=run_id, classifier=classifier)
            except SystemExit as exc:
                # classify_existing exits on a document with no extracted text.
                # The selector already excludes those, so reaching one means the
                # manifest disagrees with the filter -- report it, do not abort
                # the siblings.
                result = {"status": "error", "case_id": case_id, "doc_id": doc_id,
                          "error": str(exc)}
            except Exception as exc:
                result = {"status": "error", "case_id": case_id, "doc_id": doc_id,
                          "error": f"{type(exc).__name__}: {exc}"}
        result.setdefault("doc_id", doc_id)
        slots[index] = result
        with lock:
            report(f"  {doc_id}: {result.get('status')} "
                   f"({result.get('classification_text_source')})")

    if workers == 1:
        for index, doc in enumerate(targets):
            process(index, doc)
    else:
        with trace_mod.span("pool.classification", category="compute",
                            case_id=case_id, worker_count=workers):
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(trace_mod.run_in_context(process), i, d)
                           for i, d in enumerate(targets)]
                for future in concurrent.futures.as_completed(futures):
                    future.result()

    results = [r for r in slots if r is not None]
    blocked = [r for r in results if r.get("status") not in _OK_STATUSES]
    return {
        "status": "success" if not blocked else "partial",
        "case_id": case_id,
        "run_id": run_id,
        "document_workers": workers,
        "documents": results,
        "blocked": [{"doc_id": r.get("doc_id"), "status": r.get("status")}
                    for r in blocked],
    }


def run_redaction_stage(
    case_id: str,
    held_by: str,
    run_id: str,
    *,
    doc_workers: int | None = None,
    only: list[str] | None = None,
    progress=None,
    provider_name: str | None = None,
    model: str | None = None,
    page_workers: int | None = None,
    skip_redaction: bool | None = None,
) -> dict:
    """Run checkpoint 2 (redaction) over every eligible document in the case.

    Same shape as run_document_stage: documents run concurrently, one document
    failing does not take down the others, and the step exits non-zero if any
    document did not complete.

    Note the two levels of concurrency multiply. redact_document already runs
    PAGES concurrently (4 by default, HARNESS_REDACT_WORKERS); this adds
    documents on top, so total in-flight redaction calls are doc_workers x
    page_workers. Both default low for that reason -- raise them together with
    that product in mind, the same caution DEFAULT_OCR_WORKERS carries.
    """
    report = progress or (lambda msg: print(msg, file=sys.stderr, flush=True))

    manifest = _dao.read_contract_data(case_id, "document_manifest.json")
    if not manifest:
        return {"status": "failed", "case_id": case_id,
                "error": f"no document_manifest.json for {case_id}", "documents": []}

    targets = select_redaction_documents(manifest, only)
    if not targets:
        return {"status": "success", "case_id": case_id, "documents": [],
                "note": "no documents required checkpoint 2"}

    workers = _resolve_doc_workers(doc_workers)
    report(f"checkpoint 2: {len(targets)} document(s), {workers} document worker(s)")

    slots: list[dict | None] = [None] * len(targets)
    lock = threading.Lock()

    def process(index: int, doc: dict) -> None:
        doc_id = doc["document_id"]
        with trace_mod.span("stage.redaction", category="compute",
                            case_id=case_id, doc_id=doc_id):
            try:
                # Built per document rather than shared, and deliberately
                # through redact_document's own selector: it is what routes a
                # PII-free document class to the deterministic pass-through
                # instead of paying for a model call, and duplicating that
                # choice here would be a second copy of the rule.
                redactor = redact_document_mod._redactor_for(
                    case_id, doc_id,
                    provider_name or redact_document_mod.DEFAULT_REDACTION_PROVIDER,
                    model, skip_redaction=skip_redaction)
                result = redact_document_mod.redact_document(
                    case_id, doc_id, held_by, run_id, redactor,
                    max_workers=page_workers)
                result = {"status": "success", "doc_id": doc_id, **(result or {})}
            except Exception as exc:
                # Per-document isolation, as in checkpoint 1: a leak-blocked or
                # gate-refused document reports itself and the siblings finish.
                result = {"status": "error", "case_id": case_id, "doc_id": doc_id,
                          "error": f"{type(exc).__name__}: {exc}"}
        result.setdefault("doc_id", doc_id)
        slots[index] = result
        with lock:
            report(f"  {doc_id}: {result.get('status')}")

    if workers == 1:
        for index, doc in enumerate(targets):
            process(index, doc)
    else:
        with trace_mod.span("pool.redaction", category="compute",
                            case_id=case_id, worker_count=workers):
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(trace_mod.run_in_context(process), i, d)
                           for i, d in enumerate(targets)]
                for future in concurrent.futures.as_completed(futures):
                    future.result()

    results = [r for r in slots if r is not None]
    blocked = [r for r in results if r.get("status") not in _OK_STATUSES]
    return {
        "status": "success" if not blocked else "partial",
        "case_id": case_id,
        "run_id": run_id,
        "document_workers": workers,
        "documents": results,
        "blocked": [{"doc_id": r.get("doc_id"), "status": r.get("status")}
                    for r in blocked],
    }


def run_document_stage(
    case_id: str,
    held_by: str,
    run_id: str,
    *,
    doc_workers: int | None = None,
    only: list[str] | None = None,
    progress=None,
    **checkpoint_kwargs,
) -> dict:
    """Run checkpoint 1 over every eligible document in the case."""
    report = progress or (lambda msg: print(msg, file=sys.stderr, flush=True))

    manifest = _dao.read_contract_data(case_id, "document_manifest.json")
    if not manifest:
        return {"status": "failed", "case_id": case_id,
                "error": f"no document_manifest.json for {case_id}", "documents": []}

    targets = select_documents(manifest, only)
    if not targets:
        return {"status": "success", "case_id": case_id, "documents": [],
                "note": "no documents required checkpoint 1"}

    workers = _resolve_doc_workers(doc_workers)
    report(f"checkpoint 1: {len(targets)} document(s), {workers} document worker(s)")

    slots: list[dict | None] = [None] * len(targets)
    lock = threading.Lock()

    def process(index: int, doc: dict) -> None:
        doc_id = doc["document_id"]
        pdf = _pdf_path_for(case_id, doc)
        with trace_mod.span("stage.document", category="compute",
                            case_id=case_id, doc_id=doc_id):
            try:
                result = run_checkpoint1(
                    case_id, doc_id, str(pdf), held_by, run_id,
                    progress=None, **checkpoint_kwargs)
            except Exception as exc:
                # One document raising must not take the others down with it --
                # that is the whole point of per-document isolation here.
                result = {"status": "error", "case_id": case_id, "doc_id": doc_id,
                          "error": f"{type(exc).__name__}: {exc}"}
        result.setdefault("doc_id", doc_id)
        slots[index] = result
        with lock:
            report(f"  {doc_id}: {result.get('status')}")

    if workers == 1:
        for index, doc in enumerate(targets):
            process(index, doc)
    else:
        with trace_mod.span("pool.documents", category="compute",
                            case_id=case_id, worker_count=workers):
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(trace_mod.run_in_context(process), i, d)
                           for i, d in enumerate(targets)]
                # Drain every future before reporting: a document that raised
                # must not discard the documents that finished.
                for future in concurrent.futures.as_completed(futures):
                    future.result()

    results = [r for r in slots if r is not None]
    blocked = [r for r in results if r.get("status") not in _OK_STATUSES]
    return {
        "status": "success" if not blocked else "partial",
        "case_id": case_id,
        "run_id": run_id,
        "document_workers": workers,
        "documents": results,
        "blocked": [{"doc_id": r.get("doc_id"), "status": r.get("status")}
                    for r in blocked],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case_id")
    ap.add_argument("--held-by", required=True)
    ap.add_argument("--run-id", required=True)
    skip_group = ap.add_mutually_exclusive_group()
    skip_group.add_argument(
        "--skip-redaction", dest="skip_redaction", action="store_true", default=None,
        help="Checkpoint 2 only: skip the redaction MODEL for every document "
             "(dev switch, or HARNESS_SKIP_REDACTION=1). The deterministic "
             "residual-PII scan still runs and still blocks a page carrying "
             "structured PII.")
    skip_group.add_argument(
        "--redact", dest="skip_redaction", action="store_false",
        help="Force the redaction model on even under HARNESS_SKIP_REDACTION.")
    ap.add_argument(
        "--checkpoint", choices=["1", "2", "classify"], default="1",
        help="Which checkpoint to drive across the case's documents. 1 "
             "(default) = OCR only. 2 = redaction, which was "
             "previously one redact_document.py call per document -- the same "
             "sequential pattern this driver exists to remove. Note redaction "
             "already runs PAGES concurrently, so total in-flight calls are "
             "--doc-workers x --page-workers. `classify` assigns each "
             "document_type and runs AFTER 2, so the classifier reads the "
             "redacted layer instead of the raw page.")
    ap.add_argument(
        "--page-workers", type=int, default=None, metavar="N",
        help="Checkpoint 2 only: pages redacted concurrently WITHIN each "
             "document (default 4, or HARNESS_REDACT_WORKERS).")
    ap.add_argument(
        "--provider", choices=SUPPORTED_PROVIDERS, default=None,
        help="Checkpoint 2 only: redaction provider (default claude-cli).")
    ap.add_argument(
        "--model", default=None,
        help="Checkpoint 2 only: redaction model.")
    ap.add_argument("--doc-workers", type=int, default=None, metavar="N",
                    help=f"Documents processed concurrently (default "
                         f"{DEFAULT_DOC_WORKERS}, or {DOC_WORKERS_ENV}). "
                         "1 = the sequential loop.")
    ap.add_argument("--only", nargs="+", metavar="DOC_ID",
                    help="Restrict to these document ids.")
    ap.add_argument("--workers", type=int, default=None, metavar="N",
                    help="Pages OCR'd concurrently WITHIN each document "
                         "(default 8, or HARNESS_OCR_WORKERS). Total demand is "
                         "doc-workers x workers, bounded by "
                         "HARNESS_LLM_MAX_INFLIGHT.")
    for name in ("reader-a", "reader-b", "comparator", "classifier-provider"):
        ap.add_argument(f"--{name}", choices=SUPPORTED_PROVIDERS)
    for name in ("reader-a-model", "reader-b-model", "comparator-model",
                 "classifier-model"):
        ap.add_argument(f"--{name}")
    # Both P8 policy flags exist on run_checkpoint1.py, and until now neither
    # was reachable through this driver -- which is the tool document-pipeline
    # is told to use. So a case-wide run could not defer a disagreement or run
    # single-read at all, and the only way to reach either was the per-document
    # loop this tool exists to replace.
    ap.add_argument(
        "--on-disagreement", choices=["block", "assume-reading-a"],
        default="block",
        help="What to do when the two P8 reads disagree, applied to every "
             "document in the case. 'block' (default) halts that document "
             "pending human resolution; siblings still finish and the step "
             "still exits non-zero. 'assume-reading-a' takes reading_a and "
             "continues, recording the deferral on every affected page. See "
             "run_checkpoint1.py --help for the full contract.")
    ap.add_argument(
        "--dual-read", dest="single_reader", action="store_false", default=None,
        help="Force full dual-read P8 even when HARNESS_SINGLE_READER is set in "
             "the environment. Use for any run whose text accuracy is judged.")
    ap.add_argument(
        "--single-reader", dest="single_reader", action="store_true", default=None,
        help="DEVELOPMENT THROUGHPUT MODE -- runs the case with P8 OFF (one "
             "read per page, no comparison), roughly halving provider calls "
             "and wall time. Every page records agreement='single_reader', "
             "never 'agreed'. Mutually exclusive with --on-disagreement. Not "
             "admissible for PoC evaluation -- see run_checkpoint1.py --help. "
             "Defaults to the HARNESS_SINGLE_READER environment variable when "
             "neither this nor --dual-read is given.")
    args = ap.parse_args(argv)

    # Same rejection as run_checkpoint1.py, enforced here too: this driver has
    # its own parser, so a check that lived only in the single-document tool
    # would not fire for a case-wide run.
    if args.single_reader is True and args.on_disagreement != "block":
        ap.error(
            "--single-reader and --on-disagreement are mutually exclusive. "
            "--single-reader performs no comparison, so no disagreement can "
            "arise for --on-disagreement to resolve.")

    trace_mod.configure(args.case_id, args.run_id)

    if args.checkpoint == "classify":
        classifier = None
        if args.classifier_provider:
            classifier = build_classifier_provider(
                classifier_provider_name=args.classifier_provider,
                classifier_model=args.classifier_model)
        result = run_classification_stage(
            args.case_id, args.held_by, args.run_id,
            doc_workers=args.doc_workers,
            only=args.only,
            classifier=classifier,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "success" else 1

    if args.checkpoint == "2":
        result = run_redaction_stage(
            args.case_id, args.held_by, args.run_id,
            doc_workers=args.doc_workers,
            only=args.only,
            provider_name=args.provider,
            model=args.model,
            page_workers=args.page_workers,
            skip_redaction=args.skip_redaction,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "success" else 1

    result = run_document_stage(
        args.case_id, args.held_by, args.run_id,
        doc_workers=args.doc_workers,
        only=args.only,
        max_workers=args.workers,
        reader_a_name=args.reader_a,
        reader_b_name=args.reader_b,
        comparator_name=args.comparator,
        classifier_provider_name=args.classifier_provider,
        reader_a_model=args.reader_a_model,
        reader_b_model=args.reader_b_model,
        comparator_model=args.comparator_model,
        classifier_model=args.classifier_model,
        on_disagreement=args.on_disagreement,
        single_reader=args.single_reader,
        # Checkpoint 1 extracts; it does not label. Classification moved to its
        # own `--checkpoint classify` pass that runs after redaction, so the
        # classifier reads redacted_text.md rather than the raw page. Leaving it
        # here meant every top-level document was classified from unredacted
        # text on every run and then had to be cleared by hand at the
        # `classification_review` gate -- an exception path taken by the normal
        # case. The `classify=True` default on run_checkpoint1 itself is
        # unchanged, so a single-document call still behaves as before.
        classify=False,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # Non-zero when any document did not complete, so a blocked P8 page still
    # fails the step even though its siblings finished.
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
