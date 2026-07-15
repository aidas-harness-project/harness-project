"""Checkpoint 1 wrapper: OCR + cross-validation + classification, in one call.

Automates the mechanical sequence a document-pipeline subagent normally
performs: run dual-path OCR (ocr_extract.run_ocr, in-process, not a
subprocess-of-a-subprocess), write each agreed page (dao.py's
write-page-text logic, called directly), classify the document from its
first agreed page's already-transcribed TEXT (reasoning over text, not
re-viewing the raw image, which is a smaller PII exposure footprint than
the original design's "same vision-model call that read the page" -- see
open-decisions.md #3), assemble and write
ocr_result_{doc_id}.json + classification_result_{doc_id}.json, and update
document_manifest.json's per-document fields.

Does NOT proceed past a P8 disagreement. If any page disagrees, this stops
after writing ocr_result_{doc_id}.json (cross_validation_status:
'disagreed_pending_review', review_required: true) and returns without
writing classification_result or marking the run-state stage passed --
resolving a disagreement (choosing which reading is correct, and why) is a
human decision, not something this script does on its own.

ocr_result.json only retains reading_b (as cross_validation.vision_model_reading)
-- reading_a's full text is never persisted there. So when a disagreement
blocks the run, the complete dual-read data (both readings, every page) is
also saved to _ocr_scratch/{case_id}_{doc_id}_raw.json (gitignored, not a
schema-validated contract -- forensic/resume data, same spirit as
_ocr_scratch_dev/'s role in known-gaps.md item 2). Without this, resolving
a disagreement in a later, separate process would have no way to recover
reading_a's actual text short of re-running real OCR from scratch --
exactly the cost this wrapper exists to avoid paying twice. Use
resolve_from_raw_ocr() once a human has decided which reading is correct
(loading that raw JSON back in) -- it writes the resolved page(s), updates
ocr_result_{doc_id}.json's cross_validation_status/resolution fields, and
if every page is now resolved, continues on to classification + manifest
update, same as the no-disagreement path would have.

Does NOT run checkpoint 2 (redaction, see redact_document.py) or
checkpoint 3 (chunking, tools/chunk_text.py -- already exists, unchanged).

Usage:
    python tools/run_checkpoint1.py CASE_ID DOC_ID <path to raw pdf> --held-by NAME --run-id RUN_ID

Also usable as a library -- run_checkpoint1() / resolve_from_raw_ocr()
return a summary dict rather than just printing, so a caller (e.g. a
scenario/branch-testing script) can inspect the outcome programmatically.
"""
import argparse
import contextlib
import json
import os
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from dao import case_dir, atomic_write_json, now_iso, load_registry, validate_instance
from dao import acquire_lock_blocking, release_lock, atomic_write_text, processed_dir
import dao as _dao
from llm_providers import (
    DEFAULT_PROVIDER,
    ProviderConfig,
    ProviderConfigError,
    ProviderExecutionError,
    SUPPORTED_PROVIDERS,
    build_provider,
)
from ocr_extract import build_ocr_providers, run_ocr

ROOT = Path(__file__).resolve().parent.parent

DOCUMENT_TYPES = ["insurance_certificate", "insurance_policy", "diagnosis_certificate",
                   "medical_record", "imaging_report", "receipt", "insurer_response", "other"]
CLASSIFICATION_PROMPT_VERSION = "classification_v0.1"

CLASSIFY_PROMPT_TEMPLATE = """You are classifying an insurance claim document by its type, from its
already-transcribed text (not the raw image). Choose exactly one of these types:
{types}

Reply with ONLY a JSON object, no other text, in exactly this shape:
{{"predicted_document_type": "<one of the types above>", "document_type_label": "<Korean display label>",
  "confidence": <0-1>, "quote": "<a short verbatim quote from the text supporting this classification>"}}

--- Document text (page 1) ---
{text}
"""


def _write_page_text(case_id, doc_id, page, text, held_by, run_id):
    target = processed_dir(case_id, doc_id) / f"page_{page:03d}.md"
    existing_lock = acquire_lock_blocking(target, held_by, run_id, f"write page {page}")
    if existing_lock is not None:
        sys.exit(f"error: {target} is locked by {existing_lock['held_by']} (run {existing_lock['run_id']})")
    try:
        atomic_write_text(target, text)
    finally:
        release_lock(target)
    return target


def _write_contract(case_id, filename, data, schema_name, held_by, run_id):
    schemas, registry = load_registry()
    errors = validate_instance(data, schema_name, schemas, registry)
    if errors:
        sys.exit(f"error: {filename} fails {schema_name} -- this is a run_checkpoint1.py bug, not a data "
                  f"problem:\n" + "\n".join(f"  - {e}" for e in errors))
    target = case_dir(case_id) / filename
    existing_lock = acquire_lock_blocking(target, held_by, run_id, f"write {filename}")
    if existing_lock is not None:
        sys.exit(f"error: {target} is locked by {existing_lock['held_by']} (run {existing_lock['run_id']})")
    try:
        atomic_write_json(target, data)
    finally:
        release_lock(target)
    return target


def classify_document(text: str, classifier=None) -> dict:
    """Classify already-transcribed text through the configured provider.

    Fails loud on an unparseable response -- same fail-safe discipline as
    ocr_extract.compare(), not a silent guess.
    """
    selected_classifier = classifier or build_provider(ProviderConfig(DEFAULT_PROVIDER), root=ROOT)
    prompt = CLASSIFY_PROMPT_TEMPLATE.format(types=", ".join(DOCUMENT_TYPES), text=text[:3000])
    try:
        provider_result = selected_classifier.classify_document(prompt, CLASSIFICATION_PROMPT_VERSION)
    except ProviderExecutionError as exc:
        sys.exit(f"error: classification provider failed: {exc}")
    raw = provider_result.text.strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        sys.exit(f"error: classification response wasn't parseable JSON, refusing to guess: {raw!r}")
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError:
        sys.exit(f"error: classification response wasn't valid JSON, refusing to guess: {raw!r}")
    if parsed.get("predicted_document_type") not in DOCUMENT_TYPES:
        sys.exit(f"error: classification returned an unknown document_type {parsed.get('predicted_document_type')!r}")
    parsed["_provider_metadata"] = provider_result.metadata()
    return parsed


def build_classifier_provider(
    *,
    classifier_provider_name: str | None = None,
    classifier_model: str | None = None,
    comparator_provider=None,
    env=None,
):
    source_env = env if env is not None else os.environ
    provider_name = (
        classifier_provider_name
        or source_env.get("HARNESS_CLASSIFIER_PROVIDER")
        or (comparator_provider.provider_name if comparator_provider is not None else None)
        or source_env.get("HARNESS_OCR_COMPARATOR_PROVIDER")
        or source_env.get("HARNESS_LLM_PROVIDER")
        or DEFAULT_PROVIDER
    )
    env_comparator_provider = source_env.get("HARNESS_OCR_COMPARATOR_PROVIDER")
    comparator_model_name = (
        comparator_provider.model_name
        if comparator_provider is not None and provider_name == comparator_provider.provider_name
        else None
    )
    env_comparator_model = (
        source_env.get("HARNESS_OCR_COMPARATOR_MODEL")
        if env_comparator_provider is not None and provider_name == env_comparator_provider
        else None
    )
    model_name = (
        classifier_model
        or source_env.get("HARNESS_CLASSIFIER_MODEL")
        or comparator_model_name
        or env_comparator_model
        or source_env.get("HARNESS_LLM_MODEL")
    )
    if comparator_provider is not None and provider_name == comparator_provider.provider_name and (
        classifier_provider_name is None
        and classifier_model is None
        and not source_env.get("HARNESS_CLASSIFIER_PROVIDER")
        and not source_env.get("HARNESS_CLASSIFIER_MODEL")
    ):
        return comparator_provider
    return build_provider(ProviderConfig(provider_name, model_name), env=source_env, root=ROOT)


def _provider_label(provider_info: dict | None) -> str:
    if not provider_info:
        return "claude-cli"
    provider_name = provider_info.get("provider_name") or "unknown-provider"
    model_name = provider_info.get("model_name") or "unknown-model"
    return f"{provider_name}:{model_name}"


def _classification_model_info(provider_metadata: dict) -> dict:
    info = {
        "model_name": _provider_label(provider_metadata),
        "prompt_version": provider_metadata.get("prompt_version", CLASSIFICATION_PROMPT_VERSION),
    }
    if provider_metadata.get("provider_name"):
        info["provider_name"] = provider_metadata["provider_name"]
    return info


def _assemble_ocr_result(case_id, doc_id, run_id, ocr_data):
    providers = ocr_data.get("providers", {})
    reader_a_label = _provider_label(providers.get("reader_a"))
    reader_b_label = _provider_label(providers.get("reader_b"))
    comparator_label = _provider_label(providers.get("comparator"))
    pages_out = []
    for p in ocr_data["pages"]:
        agreed = p["agreement"] == "agreed"
        pages_out.append({
            "page": p["page"],
            "text_path": f"data/processed/{case_id}/{doc_id}/page_{p['page']:03d}.md" if agreed else None,
            "mean_confidence": None,
            "uncertain_regions": [],
            "cross_validation": {
                "vision_model_reading": p["reading_b"],
                "agreement": p["agreement"],
                "disagreement_details": p.get("disagreement_details", []),
            },
        })
    any_disagreement = any(p["agreement"] == "disagreed" for p in ocr_data["pages"])
    result = {
        "case_id": case_id, "run_id": run_id, "component": "document-pipeline", "status": "success",
        "created_at": now_iso(),
        "model_info": {
            "model_name": f"reader_a={reader_a_label}; reader_b={reader_b_label}; comparator={comparator_label}",
            "prompt_version": "ocr_extraction_v0.1",
        },
        "document_id": doc_id,
        "ocr_engine": reader_a_label,
        "vision_model_name": f"{reader_b_label}; comparator={comparator_label}",
        "uncertain_confidence_threshold": 1.0,
        "extraction_method": "ocr", "ocr_status": "completed", "pages": pages_out,
        "document_mean_confidence": None,
        "ocr_quality": "low" if any_disagreement else "high",
        "cross_validation_status": "disagreed_pending_review" if any_disagreement else "agreed",
        # P8 cross-validation strength label, computed from the actual readers by
        # ocr_extract.run_ocr (single_technology_weak_p8_poc when both readers are
        # one provider -- the PoC's claude-cli path, honestly not genuine
        # dual-technology P8; see open-decisions.md #4). Recorded here so the
        # ocr_result.json reader can never mistake a same-provider run for genuine
        # dual-technology independence.
        "cross_validation_mode": ocr_data.get("cross_validation_mode", "dual_technology"),
        "cross_validation_note": ocr_data.get("cross_validation_note", ""),
        "review_required": any_disagreement,
    }
    if any_disagreement:
        disagreed_pages = [p["page"] for p in ocr_data["pages"] if p["agreement"] == "disagreed"]
        result["reviewer_role"] = "손해사정사"
        result["review_reason"] = f"Page(s) {disagreed_pages}: the two independent reads disagree -- P8, no tolerance threshold, blocked pending human resolution."
    return result


def _reset_manifest_for_blocked_ocr(case_id, doc_id, ocr_result, held_by, run_id):
    """Called only on the blocked_disagreement path -- clears every field
    checkpoint 1 owns back to 'not validly known right now' rather than
    leaving stale values from a possible prior successful run.
    redacted_text_path/document_type/classification_confidence are nulled
    too: even if they were real before, this run's extraction just failed,
    so nothing downstream should trust them as current. Goes through
    dao.patch_manifest_document (read-modify-write under one lock hold),
    not a local read-then-_write_contract -- see known-gaps.md item 7."""
    fields = {
        "pages": len(ocr_result["pages"]),
        "ocr_status": "failed",
        "ocr_text_path": None,
        "ocr_quality": None,
        "uncertain_region_count": None,
        "cross_validation_status": ocr_result["cross_validation_status"],
        "redacted_text_path": None,
        "document_type": None,
        "classification_confidence": None,
    }
    ok, message = _dao.patch_manifest_document(case_id, doc_id, fields, held_by, run_id)
    if not ok:
        sys.exit(f"error: {message}")


@contextlib.contextmanager
def _page_range_pdf(pdf_path: Path, case_id: str, doc_id: str, page_start: int | None, page_end: int | None):
    """Yield either the original PDF path or a temporary PDF containing only
    the requested 1-based inclusive page range. This supports legacy intake
    manifests where one raw PDF was split into multiple logical DOC_XXX
    entries by page count without mutating the immutable raw source."""
    if page_start is None and page_end is None:
        yield pdf_path
        return
    if page_start is None or page_end is None:
        sys.exit("error: --page-start and --page-end must be provided together")
    if page_start < 1 or page_end < page_start:
        sys.exit(f"error: invalid page range {page_start}-{page_end}")
    if pdf_path.suffix.lower() != ".pdf":
        sys.exit("error: --page-start/--page-end can only be used with PDF input")

    scratch_root = ROOT / "_ocr_scratch"
    scratch_root.mkdir(exist_ok=True)
    temp_path = scratch_root / f"{case_id}_{doc_id}_p{page_start:03d}-{page_end:03d}.pdf"
    try:
        import fitz
    except ImportError:
        fitz = None

    if fitz is None:
        _write_page_range_pdf_pypdf(pdf_path, temp_path, page_start, page_end)
        try:
            yield temp_path
        finally:
            temp_path.unlink(missing_ok=True)
        return

    src = fitz.open(pdf_path)
    try:
        if page_end > src.page_count:
            sys.exit(f"error: page range {page_start}-{page_end} exceeds {pdf_path} ({src.page_count} pages)")
        out = fitz.open()
        try:
            out.insert_pdf(src, from_page=page_start - 1, to_page=page_end - 1)
            out.save(temp_path)
        finally:
            out.close()
        yield temp_path
    finally:
        src.close()
        temp_path.unlink(missing_ok=True)


def _write_page_range_pdf_pypdf(pdf_path: Path, temp_path: Path, page_start: int, page_end: int) -> None:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        sys.exit("error: pymupdf missing and pypdf not installed for --page-start/--page-end")

    reader = PdfReader(str(pdf_path))
    if page_end > len(reader.pages):
        sys.exit(f"error: page range {page_start}-{page_end} exceeds {pdf_path} ({len(reader.pages)} pages)")

    writer = PdfWriter()
    for page_index in range(page_start - 1, page_end):
        writer.add_page(reader.pages[page_index])
    with temp_path.open("wb") as f:
        writer.write(f)


def run_checkpoint1(
    case_id: str,
    doc_id: str,
    pdf_path: str,
    held_by: str,
    run_id: str,
    progress=None,
    reader_a=None,
    reader_b=None,
    comparator=None,
    classifier=None,
    reader_a_name: str | None = None,
    reader_b_name: str | None = None,
    comparator_name: str | None = None,
    classifier_provider_name: str | None = None,
    reader_a_model: str | None = None,
    reader_b_model: str | None = None,
    comparator_model: str | None = None,
    classifier_model: str | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
) -> dict:
    pdf_path = Path(pdf_path)
    if reader_a is None or reader_b is None or comparator is None:
        providers = build_ocr_providers(
            reader_a_name=reader_a_name,
            reader_b_name=reader_b_name,
            comparator_name=comparator_name,
            reader_a_model=reader_a_model,
            reader_b_model=reader_b_model,
            comparator_model=comparator_model,
        )
        reader_a = reader_a or providers["reader_a"]
        reader_b = reader_b or providers["reader_b"]
        comparator = comparator or providers["comparator"]
    if classifier is None:
        classifier = build_classifier_provider(
            classifier_provider_name=classifier_provider_name,
            classifier_model=classifier_model,
            comparator_provider=comparator,
        )

    with _page_range_pdf(pdf_path, case_id, doc_id, page_start, page_end) as extraction_path:
        ocr_data = run_ocr(
            case_id,
            doc_id,
            extraction_path,
            progress=progress,
            reader_a=reader_a,
            reader_b=reader_b,
            comparator=comparator,
        )

    for p in ocr_data["pages"]:
        if p["agreement"] == "agreed":
            _write_page_text(case_id, doc_id, p["page"], p["reading_a"], held_by, run_id)

    ocr_result = _assemble_ocr_result(case_id, doc_id, run_id, ocr_data)
    _write_contract(case_id, f"ocr_result_{doc_id}.json", ocr_result, "ocr_result.schema.json", held_by, run_id)

    any_disagreement = ocr_result["review_required"]
    if any_disagreement:
        scratch_root = ROOT / "_ocr_scratch"
        scratch_root.mkdir(exist_ok=True)
        raw_ocr_path = scratch_root / f"{case_id}_{doc_id}_raw.json"
        atomic_write_json(raw_ocr_path, ocr_data)

        # Real bug, found by actually running this against a case that had
        # previously PASSED (a fork of an already-completed run): without
        # this, document_manifest.json/run-state keep whatever stale
        # completed/passed values they had from before, directly
        # contradicting the ocr_result.json just written above. This isn't
        # fork-specific -- the same staleness would hit a genuine re-run
        # that newly fails after a prior success.
        _reset_manifest_for_blocked_ocr(case_id, doc_id, ocr_result, held_by, run_id)
        _dao._update_run_state(case_id, run_id, "document_processing", "failed", held_by)

        return {"status": "blocked_disagreement", "case_id": case_id, "doc_id": doc_id,
                "disagreed_pages": [p["page"] for p in ocr_data["pages"] if p["agreement"] == "disagreed"],
                "ocr_result_path": str(case_dir(case_id) / f"ocr_result_{doc_id}.json"),
                "raw_ocr_path": str(raw_ocr_path)}

    return _finish_checkpoint1(case_id, doc_id, run_id, held_by, ocr_data["pages"][0]["reading_a"], classifier=classifier)


def _finish_checkpoint1(case_id, doc_id, run_id, held_by, first_page_text, classifier=None):
    """Shared tail: classify from page 1's text, write
    classification_result_{doc_id}.json, update document_manifest.json.
    Called both by run_checkpoint1() (no disagreement) and
    apply_disagreement_resolution() (once every page is resolved)."""
    classification = classify_document(first_page_text, classifier) if classifier is not None else classify_document(first_page_text)
    ocr_result = json.loads((case_dir(case_id) / f"ocr_result_{doc_id}.json").read_text(encoding="utf-8"))
    provider_metadata = classification.get("_provider_metadata", {})

    classification_result = {
        "case_id": case_id, "run_id": run_id, "component": "document-pipeline", "status": "success",
        "created_at": now_iso(), "model_info": _classification_model_info(provider_metadata),
        "document_id": doc_id,
        "predicted_document_type": classification["predicted_document_type"],
        "document_type_label": classification.get("document_type_label", ""),
        "confidence": classification.get("confidence", 0.5),
        "pre_flagged": False,
        "evidence_references": [{"page": 1, "quote": classification.get("quote", "")}],
        "review_required": False,
    }
    _write_contract(case_id, f"classification_result_{doc_id}.json", classification_result,
                     "classification_result.schema.json", held_by, run_id)

    fields = {
        "pages": len(ocr_result["pages"]),
        "ocr_status": "completed",
        "ocr_quality": ocr_result["ocr_quality"],
        "uncertain_region_count": 0,
        "cross_validation_status": ocr_result["cross_validation_status"],
        "document_type": classification["predicted_document_type"],
        "classification_confidence": classification.get("confidence", 0.5),
    }
    ok, message = _dao.patch_manifest_document(case_id, doc_id, fields, held_by, run_id)
    if not ok:
        sys.exit(f"error: {message}")

    _dao._update_run_state(case_id, run_id, "document_processing", "passed", held_by)

    return {"status": "passed", "case_id": case_id, "doc_id": doc_id,
            "document_type": classification["predicted_document_type"],
            "cross_validation_status": ocr_result["cross_validation_status"]}


def resolve_from_raw_ocr(case_id: str, doc_id: str, ocr_data: dict, page: int, chosen_reading: str,
                          resolved_by: str, note: str, held_by: str, run_id: str,
                          classifier=None) -> dict:
    """Resolves one disagreed page using the original run_ocr() result
    (which has both reading_a and reading_b) plus a human's decision of
    which one is correct and why. Writes that page's text, updates
    ocr_result_{doc_id}.json's cross_validation_status + this page's
    resolution record. If every page is now agreed-or-resolved, continues
    on to classification + manifest update (same tail run_checkpoint1()
    uses when there's no disagreement at all)."""
    if chosen_reading not in ("reading_a", "reading_b"):
        sys.exit(f"error: chosen_reading must be reading_a or reading_b -- got {chosen_reading!r}")
    page_data = next((p for p in ocr_data["pages"] if p["page"] == page), None)
    if page_data is None:
        sys.exit(f"error: no page {page} in this OCR result")
    chosen_text = page_data[chosen_reading]

    _write_page_text(case_id, doc_id, page, chosen_text, held_by, run_id)

    ocr_result_path = case_dir(case_id) / f"ocr_result_{doc_id}.json"
    ocr_result = json.loads(ocr_result_path.read_text(encoding="utf-8"))
    page_entry = next(p for p in ocr_result["pages"] if p["page"] == page)
    page_entry["text_path"] = f"data/processed/{case_id}/{doc_id}/page_{page:03d}.md"
    page_entry["cross_validation"]["resolution"] = {
        "chosen_reading": chosen_reading, "resolved_by": resolved_by, "resolved_at": now_iso(), "note": note,
    }

    still_unresolved = [p["page"] for p in ocr_result["pages"]
                         if p["cross_validation"]["agreement"] == "disagreed"
                         and p["cross_validation"].get("resolution") is None]
    if still_unresolved:
        ocr_result["review_reason"] = f"Page(s) {still_unresolved} still unresolved."
        _write_contract(case_id, f"ocr_result_{doc_id}.json", ocr_result, "ocr_result.schema.json", held_by, run_id)
        return {"status": "partially_resolved", "case_id": case_id, "doc_id": doc_id, "still_unresolved": still_unresolved}

    ocr_result["cross_validation_status"] = "disagreed_resolved"
    ocr_result["review_required"] = False
    ocr_result["review_reason"] = "All disagreements resolved -- see each page's cross_validation.resolution."
    _write_contract(case_id, f"ocr_result_{doc_id}.json", ocr_result, "ocr_result.schema.json", held_by, run_id)

    first_page_agreed_or_resolved = ocr_result["pages"][0]
    first_page_text = Path(ROOT / first_page_agreed_or_resolved["text_path"]).read_text(encoding="utf-8")
    return _finish_checkpoint1(case_id, doc_id, run_id, held_by, first_page_text, classifier=classifier)


def _run_from_args(args):
    try:
        result = run_checkpoint1(
            args.case_id,
            args.doc_id,
            args.pdf_path,
            args.held_by,
            args.run_id,
            reader_a_name=args.reader_a,
            reader_b_name=args.reader_b,
            comparator_name=args.comparator,
            classifier_provider_name=args.classifier_provider,
            reader_a_model=args.reader_a_model,
            reader_b_model=args.reader_b_model,
            comparator_model=args.comparator_model,
            classifier_model=args.classifier_model,
            page_start=args.page_start,
            page_end=args.page_end,
        )
    except ProviderConfigError as exc:
        sys.exit(f"error: {exc}")
    except ProviderExecutionError as exc:
        sys.exit(f"error: {exc}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "blocked_disagreement":
        sys.exit(1)


def _resolve_from_args(args):
    """Wire the already-existing, unit-tested resolve_from_raw_ocr() to the CLI.

    A P8 disagreement blocks the document pending a HUMAN decision of which
    reading is correct (choosing the reading is never automated -- that would
    reintroduce the very judgment P8 hands to a person). The full dual-read
    data is recovered from _ocr_scratch/{case}_{doc}_raw.json (saved by
    run_checkpoint1 exactly for this), so no OCR re-run is needed. --chosen-reading
    is required; the tool does not guess."""
    raw_path = ROOT / "_ocr_scratch" / f"{args.case_id}_{args.doc_id}_raw.json"
    if not raw_path.exists():
        sys.exit(
            f"error: raw dual-read dump not found -- {raw_path}\n"
            "resolve-disagreement needs the _ocr_scratch/{case}_{doc}_raw.json that "
            "run_checkpoint1 writes when it blocks on a disagreement. If it was cleaned "
            "up, re-run checkpoint 1 for this document to regenerate both readings."
        )
    try:
        ocr_data = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"error: could not read raw dual-read dump {raw_path}: {exc}")

    try:
        result = resolve_from_raw_ocr(
            args.case_id,
            args.doc_id,
            ocr_data,
            page=args.page,
            chosen_reading=args.chosen_reading,
            resolved_by=args.resolved_by,
            note=args.note,
            held_by=args.held_by,
            run_id=args.run_id,
        )
    except ProviderConfigError as exc:
        sys.exit(f"error: {exc}")
    except ProviderExecutionError as exc:
        sys.exit(f"error: {exc}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _add_run_arguments(parser):
    parser.add_argument("case_id")
    parser.add_argument("doc_id")
    parser.add_argument("pdf_path")
    parser.add_argument("--held-by", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--reader-a", choices=SUPPORTED_PROVIDERS, help="Provider for the first independent OCR read")
    parser.add_argument("--reader-b", choices=SUPPORTED_PROVIDERS, help="Provider for the second independent OCR read")
    parser.add_argument("--comparator", choices=SUPPORTED_PROVIDERS, help="Provider for OCR read comparison")
    parser.add_argument("--classifier-provider", choices=SUPPORTED_PROVIDERS,
                        help="Provider for document classification; defaults to the comparator provider")
    parser.add_argument("--reader-a-model", help="Model name for --reader-a")
    parser.add_argument("--reader-b-model", help="Model name for --reader-b")
    parser.add_argument("--comparator-model", help="Model name for --comparator")
    parser.add_argument("--classifier-model", help="Model name for --classifier-provider")
    parser.add_argument("--page-start", type=int, help="1-based first source PDF page for this logical document")
    parser.add_argument("--page-end", type=int, help="1-based last source PDF page for this logical document")


# Reserved subcommand names dispatched explicitly; anything else is treated as
# the legacy positional `run` invocation (CASE DOC PDF ...) for backward
# compatibility with document-pipeline.md and existing callers.
_SUBCOMMANDS = {"run", "resolve-disagreement"}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Run checkpoint 1 (OCR + cross-validation + classification)")
    _add_run_arguments(run_parser)

    resolve_parser = sub.add_parser(
        "resolve-disagreement",
        help="Resolve one P8-disagreed page by selecting the correct reading (human decision)",
    )
    resolve_parser.add_argument("case_id")
    resolve_parser.add_argument("doc_id")
    resolve_parser.add_argument("--page", type=int, required=True, help="1-based page number of the disagreed page")
    resolve_parser.add_argument("--chosen-reading", choices=["reading_a", "reading_b"], required=True,
                                help="Which of the two independent reads the human verified as correct")
    resolve_parser.add_argument("--resolved-by", required=True, help="Name of the human (e.g. 손해사정사) making the call")
    resolve_parser.add_argument("--note", required=True, help="Why this reading is correct")
    resolve_parser.add_argument("--held-by", required=True)
    resolve_parser.add_argument("--run-id", required=True)

    # Backward compatibility: the legacy form is `... CASE DOC PDF --held-by ...`
    # with no subcommand token. If the first arg isn't a known subcommand (and
    # isn't a help flag), route to the `run` parser so old invocations keep working.
    if argv and argv[0] not in _SUBCOMMANDS and argv[0] not in ("-h", "--help"):
        args = run_parser.parse_args(argv)
        _run_from_args(args)
        return

    args = ap.parse_args(argv)
    if args.command == "resolve-disagreement":
        _resolve_from_args(args)
    elif args.command == "run":
        _run_from_args(args)
    else:
        ap.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
