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

For a whole document that a genuine human verifies is non-text visual
evidence, `resolve-non-text` records that distinct outcome without choosing a
reader or fabricating an image description. It writes no page text and routes
the document to expert review only. Mixed text/image documents are rejected by
that command.

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
    python tools/run_checkpoint1.py resolve-non-text CASE_ID DOC_ID \
        --verified-by NAME --reviewer-role 의사 --note TEXT \
        --held-by document-pipeline --run-id RUN_ID

Also usable as a library -- run_checkpoint1() / resolve_from_raw_ocr()
return a summary dict rather than just printing, so a caller (e.g. a
scenario/branch-testing script) can inspect the outcome programmatically.
"""
import argparse
import contextlib
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from dao import case_dir, atomic_write_json, now_iso, load_registry, validate_instance, read_contract_data
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
import segment_case as _segment_case
# tools/trace.py, not the stdlib `trace` module.
import trace as trace_mod

ROOT = Path(__file__).resolve().parent.parent

DOCUMENT_TYPES = ["insurance_certificate", "insurance_policy", "application_form",
                   "diagnosis_certificate", "medical_record", "imaging_report",
                   "receipt", "insurer_response", "other"]
CLASSIFICATION_PROMPT_VERSION = "classification_v0.2"

# The three easily-confused Korean insurance forms all carry policy-like
# language, so a bare type list collapses them into insurance_policy (CASE_030:
# a 증권 and a 청약 both misclassified that way). This guidance names the
# distinguishing signal for each so the model separates them.
CLASSIFY_PROMPT_TEMPLATE = """You are classifying an insurance claim document by its type, from its
already-transcribed text (not the raw image). Choose exactly one of these types:
{types}

These three Korean forms look similar -- distinguish them by their defining marker:
- insurance_policy (보험약관): the full contract terms/clauses -- articles like 제N조, 지급사유, 면책, a table of contents of 특별약관. It is the rulebook, not a record of one contract.
- insurance_certificate (증권서류): a "보험증권" issued as proof of ONE concluded contract -- a 계약번호/증권번호, 보험기간, 보장내용 with 가입금액 per coverage, 총보험료. It references the 약관 but is not the 약관 itself.
- application_form (청약서류): a "청약서"/가입 신청서 the applicant fills in and signs to APPLY -- 청약일, applicant/피보험자 자필서명, 계약전 알릴의무 질문서, 상품설명서 cover pages. It precedes the contract; it is not the contract terms and not the issued certificate.

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


# A title line that identifies a policy booklet part, as printed by Korean
# insurers. Mirrors the anchors segment_case.text_anchor_boundaries() cuts on;
# a slice carrying one of these IS a policy document by construction.
_POLICY_TITLE_RE = re.compile(r"(?:보통약관|특별약관|특약|약관)\s*$")
# Inherited types are not a classifier verdict, so they do not borrow a
# classifier's confidence. This is the deterministic-provenance value: the
# split evidence is exact, but no model examined this document's own content.
PARENT_INHERITED_CONFIDENCE = 0.95


def default_disposition(document_type: str | None) -> str:
    """The downstream disposition a freshly classified document starts at.

    A policy document starts at `text_only_no_normalization`: processed,
    chunked and citable, but owing no normalized clause contract. Normalizing
    is opt-IN, because it is the expensive obligation (one 145-page bundle
    carries 800+ conditions) and nothing downstream consumes its output --
    clauses are addressed by document/page/quote, verified verbatim against the
    processed source. A case that genuinely disputes a specific policy document
    promotes just that one to `automated_text_pipeline`.

    Every other document type is unaffected: the disposition only ever gates
    the policy-normalization obligation, so a diagnosis certificate or an
    insurer response keeps the full-pipeline value it always had.
    """
    if document_type == "insurance_policy":
        return "text_only_no_normalization"
    return "automated_text_pipeline"


def inherited_classification(case_id: str, doc_id: str, manifest: dict | None = None) -> dict | None:
    """The parent bundle's classification, when this document is a text-anchor
    slice of it. None means "classify normally".

    Why this is sound rather than a shortcut: a text-anchor segment is not a
    model's guess about where a document starts -- `segment_case.text_anchor_
    boundaries()` cuts strictly on the publisher's own typed title lines
    (`...보통약관`/`...특별약관`/`...특약`), measured at precision 1.0000 across
    173 boundaries on CASE_112's two policy bundles. Every slice is therefore a
    part of the same physical policy booklet the parent already was, and asking
    a model 176 separate times whether each piece of one 약관 bundle is an
    insurance policy re-derives, probabilistically, something the split itself
    established deterministically.

    Deliberately narrow. It requires ALL of:
      * a recorded parent (`source_file_name`) that is present in the manifest,
      * the parent carrying a real `document_type` and confidence,
      * the parent's type being `insurance_policy` -- the only type whose
        subdivisions are the same type by construction. A 진단서 bundle sliced
        into per-patient documents is NOT self-similar this way, so it still
        pays for its own classification.

    It deliberately does NOT require the slice to be `embedded_text`. That
    condition was standing in for "the boundary was not a model's guess", which
    `mode == 'text_anchor'` already establishes directly -- those cuts are made
    on printed 약관 title lines, never by a model. Where the TEXT came from does
    not decide how the BOUNDARY was found. Measured on CASE_112's two policy
    bundles (323 pages): boundaries derived from the OCR-produced page text are
    identical to those from the embedded text layer, same 173 boundaries,
    precision 1.0000 against the human-approved baseline for both.

    P8 is untouched -- each slice still carries its own cross-validation. The
    only thing skipped is the classifier call.

    Anything else returns None and the normal provider call runs.
    """
    if manifest is None:
        manifest_path = case_dir(case_id) / "document_manifest.json"
        if not manifest_path.exists():
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    by_id = {d.get("document_id"): d for d in manifest.get("documents", [])}
    child = by_id.get(doc_id)
    if not child:
        return None
    parent_name = child.get("source_file_name")
    proposal_rel = child.get("segmentation_proposal_path")
    if not parent_name or not proposal_rel:
        return None

    parent = next((d for d in manifest.get("documents", [])
                   if d.get("file_name") == parent_name), None)
    if not parent:
        return None

    # The evidence is the split itself, read back from the proposal -- not the
    # parent's own document_type, which a superseded bundle never has (it is
    # excluded from checkpoint 1 by design), and not the segment's
    # provisional_document_type, which pipeline.md says is never trusted
    # downstream. What IS trustworthy is the boundary evidence: `text_anchor`
    # mode cuts only on a printed 약관 title line, so a slice whose own title
    # ends in 약관/특약 is part of a policy booklet by construction.
    proposal_path = ROOT / proposal_rel
    if not proposal_path.exists():
        return None
    try:
        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (proposal.get("method") or {}).get("mode") != "text_anchor":
        return None
    if proposal.get("review_status") != "approved":
        return None

    label = next((s.get("provisional_type_label") for s in proposal.get("segments", [])
                  if s.get("page_start") == child.get("source_page_start")), None)
    if not label or not _POLICY_TITLE_RE.search(label):
        return None

    return {
        "predicted_document_type": "insurance_policy",
        "document_type_label": label,
        "confidence": PARENT_INHERITED_CONFIDENCE,
        "quote": "",
        "_inherited_from": parent["document_id"],
        "_inherited_label": label,
        "_provider_metadata": {},
    }


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


def _assemble_ocr_result(
        case_id, doc_id, run_id, ocr_data, source_total_pages=None):
    providers = ocr_data.get("providers", {})
    reader_a_label = _provider_label(providers.get("reader_a"))
    reader_b_label = _provider_label(providers.get("reader_b"))
    comparator_label = _provider_label(providers.get("comparator"))
    pages_out = []
    for p in ocr_data["pages"]:
        agreed = p["agreement"] == "agreed"
        pages_out.append({
            "page": p["page"],
            "content_kind": "text",
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
    # Embedded-text (plain-text passthrough) documents carry their own honest
    # extraction_method/encoding from ocr_extract._run_embedded_text -- a
    # lossless decode, not OCR. Default to the OCR values for the image/PDF path.
    extraction_method = ocr_data.get("extraction_method", "ocr")
    is_embedded_text = extraction_method == "embedded_text"
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
        "extraction_method": extraction_method, "ocr_status": "completed", "pages": pages_out,
        "source_total_pages": source_total_pages,
        "encoding_detected": ocr_data.get("encoding_detected"),
        "document_mean_confidence": None,
        # Embedded text is a lossless decode, not a probabilistic read -- its
        # quality is 'high' and its cross-validation status 'agreed' (the decode
        # is its own ground truth); cross_validation_mode 'deferred_poc' below is
        # what records that no dual-read OCR cross-check was performed.
        "ocr_quality": "low" if any_disagreement else "high",
        "cross_validation_status": "disagreed_pending_review" if any_disagreement else "agreed",
        # P8 cross-validation strength label, computed from the actual readers by
        # ocr_extract.run_ocr. Every current provider is LLM-vision-backed, so this
        # is single_technology_weak_p8_poc for any reader pair -- honestly not
        # genuine dual-technology P8 (see open-decisions.md #4). The default here
        # is the honest weak label, never dual_technology, so a missing field can
        # never be misread as genuine independence. (A text-passthrough document
        # sets deferred_poc explicitly instead.)
        "cross_validation_mode": ocr_data.get("cross_validation_mode", "single_technology_weak_p8_poc"),
        "cross_validation_note": ocr_data.get("cross_validation_note", ""),
        "review_required": any_disagreement,
    }
    if any_disagreement:
        disagreed_pages = [p["page"] for p in ocr_data["pages"] if p["agreement"] == "disagreed"]
        result["reviewer_role"] = "손해사정사"
        result["review_reason"] = f"Page(s) {disagreed_pages}: the two independent reads disagree -- P8, no tolerance threshold, blocked pending human resolution."
    return result


def source_pdf_page_count(pdf_path: Path) -> int:
    """Read immutable physical page count before any page-range extraction."""
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(pdf_path)).pages)
    except Exception as exc:
        raise RuntimeError(
            f"cannot determine immutable source PDF page count for "
            f"{pdf_path}: {exc}") from exc


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
        "source_total_pages": ocr_result.get("source_total_pages"),
        "ocr_status": "failed",
        "ocr_text_path": None,
        "ocr_quality": None,
        "uncertain_region_count": None,
        "cross_validation_status": ocr_result["cross_validation_status"],
        "redacted_text_path": None,
        "document_type": None,
        "classification_confidence": None,
        "extraction_method": None,
        "downstream_disposition": None,
        "non_text_verification": None,
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

    # select(), not insert_pdf(): insert_pdf rebuilds each page's resources into
    # a fresh document and drops the glyphs of a page whose text is drawn in a
    # subset TrueType font with a broken/WinAnsi-mislabelled encoding -- which
    # is what Korean insurer PDFs use ("ABCDEE+바탕체"). select() keeps the
    # page's own resources and reproduces the source byte-for-byte.
    #
    # segment_case.split_bundle already fixed exactly this and measured it
    # (CASE_905, 323 pages: insert_pdf lost the text layer on 3 cover pages,
    # select on 0). This slicer was never updated, so the same bug survived on
    # the other path that cuts a PDF. Re-measured here on CASE_902 DOC_001
    # (249p, 248 with text): insert_pdf lost pages 2-7 and 248, select lost none
    # and reproduced every char count exactly.
    #
    # The failure is invisible except as cost and quality: a page that extracts
    # 0 chars reads as a genuine scan to ocr_extract's embedded-text check,
    # which routes the whole document to vision OCR -- paying for hundreds of
    # provider calls to re-read text that was already perfect, on the very path
    # where vision has been observed hallucinating an insurer slogan.
    #
    # select() mutates the document it is called on, so this opens its own
    # handle rather than sharing the caller's.
    src = fitz.open(pdf_path)
    try:
        if page_end > src.page_count:
            sys.exit(f"error: page range {page_start}-{page_end} exceeds {pdf_path} ({src.page_count} pages)")
        src.select(list(range(page_start - 1, page_end)))
        src.save(temp_path)
        yield temp_path
    finally:
        src.close()
        temp_path.unlink(missing_ok=True)


def _write_page_range_pdf_pypdf(pdf_path: Path, temp_path: Path, page_start: int, page_end: int) -> None:
    """Fallback slicer for hosts without pymupdf.

    KNOWN LIMITATION, measured rather than assumed: like fitz's insert_pdf,
    pypdf's add_page rebuilds the page into a new document and loses the text
    layer of pages drawn in a subset TrueType font with a mislabelled encoding.
    On CASE_902 DOC_001 it lost exactly the same 7 pages insert_pdf did.
    pymupdf's select() is the only slicer measured to preserve them, so this
    path warns instead of failing silently -- a document that quietly drops to
    vision OCR costs hundreds of provider calls and re-reads text that was
    already correct.
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        sys.exit("error: pymupdf missing and pypdf not installed for --page-start/--page-end")

    reader = PdfReader(str(pdf_path))
    if page_end > len(reader.pages):
        sys.exit(f"error: page range {page_start}-{page_end} exceeds {pdf_path} ({len(reader.pages)} pages)")

    print(
        "WARNING: slicing with pypdf because pymupdf is unavailable. pypdf can "
        "drop the embedded text layer of pages using subset fonts with a "
        "mislabelled encoding (common in Korean insurer PDFs), which silently "
        "routes those pages to vision OCR. Install pymupdf for a lossless "
        "slice.",
        file=sys.stderr,
    )
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
    classify: bool = True,
    max_workers: int | None = None,
) -> dict:
    # Evaluated before provider construction, PDF rendering, or any output
    # write, so a blocked call cannot spend tokens. What it refuses is now only
    # the retained superseded bundle -- reading it again would duplicate every
    # page under a document its children replaced.
    #
    # A bundle awaiting its split is NOT refused: reading it is the point of the
    # inverted order. Boundaries derived from real page text beat those read off
    # a downscaled contact-sheet crop (CASE_112, 323 pages, precision 1.0000 vs
    # 0.84-0.95), and unlike the embedded text layer they are available for a
    # scan. What still must wait for the split is CLASSIFICATION, because
    # `document_type` is a per-document value and one label cannot be right for
    # a bundle mixing a 진단서, a 검사보고서 and an 입퇴원확인서 -- that is what
    # `classify=False` expresses, and it is a narrower request, not a bypass.
    segmentation = _dao.check_segmentation_ready(case_id, doc_id)
    if segmentation["blockers"] or segmentation.get("error"):
        return {
            "status": "blocked_segmentation",
            "case_id": case_id,
            "doc_id": doc_id,
            "blockers": segmentation["blockers"],
            "error": segmentation.get("error"),
            "next_action": (
                "Process the bundle's logical children, not the retained "
                "superseded bundle itself."
            ),
        }

    # Refuse to re-read a document that already has extracted text. `run` starts
    # by OCR'ing, so calling it on a split child -- whose pages were inherited
    # from its bundle -- destroys the very record redistribution just created,
    # replacing an inherited P8 history with a fresh verdict. CASE_909's
    # DOC_006-013 lost theirs exactly this way. Naming the alternative matters:
    # the caller usually wants a document_type, not a second reading.
    existing_ocr = case_dir(case_id) / f"ocr_result_{doc_id}.json"
    if existing_ocr.exists():
        return {
            "status": "already_extracted",
            "case_id": case_id,
            "doc_id": doc_id,
            "ocr_result_path": str(existing_ocr),
            "next_action": (
                "This document already has extracted text. To give it a "
                "document_type without re-reading it, use classify-only. To "
                "genuinely re-extract, delete the existing ocr_result first."
            ),
        }

    pdf_path = Path(pdf_path)
    source_total_pages = source_pdf_page_count(pdf_path)
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
    if classifier is None and classify:
        # Not built in bundle-OCR mode: no classification happens, so
        # constructing a provider for it would resolve credentials and a model
        # for a call that is never made.
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
            max_workers=max_workers,
        )

    for p in ocr_data["pages"]:
        if p["agreement"] == "agreed":
            _write_page_text(case_id, doc_id, p["page"], p["reading_a"], held_by, run_id)

    ocr_result = _assemble_ocr_result(
        case_id, doc_id, run_id, ocr_data,
        source_total_pages=source_total_pages)
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

    if not classify:
        # Bundle OCR for the inverted order: the text exists and segmentation
        # reads it to place boundaries, but this document is about to stop
        # existing as a processing target -- split_bundle supersedes it and
        # redistributes these pages to its children, which classify
        # individually. Writing a document_type here would be asserting one
        # label for a bundle, the very thing the gate above protects against.
        _dao._update_run_state(case_id, run_id, "document_processing", "in_progress", held_by)
        return {"status": "bundle_ocr_complete", "case_id": case_id, "doc_id": doc_id,
                "pages": len(ocr_data["pages"]),
                "cross_validation_status": ocr_result["cross_validation_status"],
                "next_action": "derive boundaries from this text, then split; children classify individually"}

    return _finish_checkpoint1(case_id, doc_id, run_id, held_by, ocr_data["pages"][0]["reading_a"], classifier=classifier)


def printed_title_classification(case_id: str, doc_id: str,
                                  manifest: dict | None = None) -> dict | None:
    """The document_type its own printed title determines, or None.

    The split cut this document's boundary ON that title, at precision 1.0000
    against the human baseline, so the title is not a guess to be checked -- it
    is recorded evidence. Asking a model to re-read the same page and name a
    type is a second opinion on something already held exactly.

    Narrow by construction. `document_type_from_title` maps only titles that
    name a FORM, never a genre: "REPORT" says a report exists, not which kind,
    and CASE_909's p6/p7 are imaging readings only by coincidence of that
    bundle. Anything unmapped returns None and the model classifies as before.

    Verified against the model on CASE_909 before being trusted: of the 12
    segments, 4 mapped and all 4 agreed with the classifier's own verdict, 0
    differed, and the 5 it declined include exactly the ambiguous ones (both
    REPORTs, the untitled page, and 입퇴원확인서 -- which the model itself
    answered at only 0.72).
    """
    if manifest is None:
        manifest_path = case_dir(case_id) / "document_manifest.json"
        if not manifest_path.exists():
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    child = next((d for d in manifest.get("documents", [])
                  if d.get("document_id") == doc_id), None)
    if not child:
        return None
    proposal_rel = child.get("segmentation_proposal_path")
    page_start = child.get("source_page_start")
    if not proposal_rel or page_start is None:
        return None
    proposal_path = ROOT / proposal_rel
    if not proposal_path.exists():
        return None
    try:
        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    # Only a human-approved deterministic split. A vision proposal's label is a
    # model's reading of a downscaled crop, which is exactly the kind of guess
    # pipeline.md says never to trust downstream.
    if (proposal.get("method") or {}).get("mode") != "text_anchor":
        return None
    if proposal.get("review_status") != "approved":
        return None
    label = next((s.get("provisional_type_label") for s in proposal.get("segments", [])
                  if s.get("page_start") == page_start), None)
    doc_type = _segment_case.document_type_from_title(label)
    if doc_type is None:
        return None
    return {
        "predicted_document_type": doc_type,
        "document_type_label": label,
        "confidence": PARENT_INHERITED_CONFIDENCE,
        "quote": label,
        "_title_classified": label,
        "_provider_metadata": {},
    }


def classify_existing(case_id: str, doc_id: str, *, held_by: str, run_id: str,
                       classifier=None) -> dict:
    """Classify a document whose pages already exist, without re-reading it.

    A split child inherits its pages from the bundle, so its text is on disk
    before it ever needs a document_type. Without this entry point the only way
    to get one was `run`, which begins by OCR'ing -- re-reading pages that were
    just redistributed and replacing their inherited P8 history with a fresh
    verdict. That is not hypothetical: it happened to CASE_909's DOC_006-013.
    """
    ocr_path = case_dir(case_id) / f"ocr_result_{doc_id}.json"
    if not ocr_path.exists():
        sys.exit(f"error: {ocr_path} does not exist -- this document has no "
                  f"extracted text yet, so run checkpoint 1 for it first")
    ocr_result = json.loads(ocr_path.read_text(encoding="utf-8"))
    first_page_text, text_source = _classification_input_text(case_id, doc_id, ocr_result)
    return _finish_checkpoint1(case_id, doc_id, run_id, held_by, first_page_text,
                                classifier=classifier, text_source=text_source)


def _classification_input_text(case_id: str, doc_id: str, ocr_result: dict) -> tuple[str, str]:
    """Page 1's text for classification, redacted layer preferred.

    `page_NNN.md` still carries claimant PII -- dao.read-page-text guards it
    behind checkpoint 2's one-shot capability precisely so no analysis stage
    reads it -- and following ocr_result's text_path walked straight past that.
    A split child inherits the bundle's redaction, so the redacted text is
    normally already there.

    The raw fallback stays, because a document not yet redacted still has to be
    classifiable, but it is never silent: the caller records which source was
    used on the contract, so reading unredacted text is visible afterwards
    rather than being an invisible default.
    """
    redacted = ROOT / "data" / "processed" / case_id / doc_id / "redacted_text.md"
    if redacted.exists():
        pages = [block for block in redacted.read_text(encoding="utf-8").split("<<<PAGE")
                  if block.strip()]
        if pages:
            first = pages[0].split(">>>", 1)[-1].strip()
            if first:
                return first, "redacted_text"
    first_page = ocr_result["pages"][0]
    text_path = first_page.get("text_path")
    if not text_path:
        sys.exit(f"error: {doc_id} page 1 has no text_path; nothing to classify from")
    return (ROOT / text_path).read_text(encoding="utf-8"), "raw_page_text"


def _finish_checkpoint1(case_id, doc_id, run_id, held_by, first_page_text, classifier=None,
                         text_source: str = "raw_page_text"):
    """Shared tail: classify from page 1's text, write
    classification_result_{doc_id}.json, update document_manifest.json.
    Called both by run_checkpoint1() (no disagreement) and
    apply_disagreement_resolution() (once every page is resolved)."""
    classification = inherited_classification(case_id, doc_id)
    if classification is None:
        classification = printed_title_classification(case_id, doc_id)
    if classification is None:
        classification = (classify_document(first_page_text, classifier) if classifier is not None
                          else classify_document(first_page_text))
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
    classification_result["classification_text_source"] = text_source
    if text_source == "raw_page_text":
        # Not an error -- a document not yet redacted still has to be
        # classifiable -- but reading unredacted text is a fact a reviewer
        # should see rather than an invisible default.
        classification_result["review_required"] = True
        classification_result["reviewer_role"] = "손해사정사"
        classification_result["review_reason"] = (
            "classified from raw page text: no redacted_text.md existed for this "
            "document at classification time, so the input still carried any PII "
            "the page holds"
        )

    title_classified = classification.get("_title_classified")
    if title_classified:
        # Record that no classifier ran and what decided instead, so an audit
        # can tell a printed-title verdict from a model's.
        classification_result["classification_source"] = "printed_form_title"
        classification_result["evidence_references"] = [{
            "page": 1,
            "quote": (f"printed form title {title_classified!r}: the approved "
                      "text-anchor split cut this document's boundary on it"),
        }]
    inherited_from = classification.get("_inherited_from")
    if inherited_from:
        # Record that no classifier ran, and from where the type came, so an
        # audit can tell an inherited type from a model verdict.
        classification_result["classification_source"] = "inherited_from_parent_bundle"
        classification_result["inherited_from_document_id"] = inherited_from
        classification_result["evidence_references"] = [{
            "page": 1,
            "quote": (f"inherited from {inherited_from}: deterministic text-anchor slice of an "
                      "already-classified insurance_policy bundle; no classifier call made"),
        }]
    _write_contract(case_id, f"classification_result_{doc_id}.json", classification_result,
                     "classification_result.schema.json", held_by, run_id)

    fields = {
        "pages": len(ocr_result["pages"]),
        "source_total_pages": ocr_result.get("source_total_pages"),
        "ocr_status": "completed",
        "ocr_quality": ocr_result["ocr_quality"],
        "uncertain_region_count": 0,
        "cross_validation_status": ocr_result["cross_validation_status"],
        "document_type": classification["predicted_document_type"],
        "classification_confidence": classification.get("confidence", 0.5),
        "extraction_method": ocr_result.get("extraction_method", "ocr"),
        "downstream_disposition": default_disposition(
            classification["predicted_document_type"]),
        "non_text_verification": None,
    }
    ok, message = _dao.patch_manifest_document(case_id, doc_id, fields, held_by, run_id)
    if not ok:
        sys.exit(f"error: {message}")

    # Checkpoint 1 succeeding for one document does not complete the
    # case-scoped document_processing stage. Other documents may still need
    # checkpoint 1, and checkpoints 2 (redaction) and 3 (case chunking) have
    # not run yet. Keep the top-level stage in progress; only the orchestrator
    # may mark it passed after every checkpoint is complete and snapshotted.
    _dao._update_run_state(case_id, run_id, "document_processing", "in_progress", held_by)

    return {"status": "passed", "case_id": case_id, "doc_id": doc_id,
            "document_type": classification["predicted_document_type"],
            "classification_text_source": text_source,
            "cross_validation_status": ocr_result["cross_validation_status"]}


def resolve_as_non_text(
    case_id: str,
    doc_id: str,
    *,
    verified_by: str,
    reviewer_role: str,
    note: str,
    held_by: str,
    run_id: str,
) -> dict:
    """Resolve an entire P8-blocked document as human-verified visual evidence.

    This deliberately does NOT choose either OCR reading and does NOT create a
    page text file. The original disagreement remains in each page's
    cross_validation record; the human verification only establishes that text
    extraction is not applicable and that downstream use is expert-review-only.
    """
    if reviewer_role not in {"손해사정사", "의사", "법률전문가"}:
        sys.exit(f"error: unsupported reviewer role {reviewer_role!r}")
    if not verified_by.strip() or not note.strip():
        sys.exit("error: --verified-by and --note must be non-empty")

    ocr_result = read_contract_data(case_id, f"ocr_result_{doc_id}.json")
    if ocr_result is None:
        sys.exit(
            f"error: ocr_result_{doc_id}.json not found -- run checkpoint 1 before resolving non-text content"
        )
    if ocr_result.get("cross_validation_status") != "disagreed_pending_review":
        sys.exit(
            "error: non-text resolution requires cross_validation_status "
            f"'disagreed_pending_review', got {ocr_result.get('cross_validation_status')!r}"
        )
    pages = ocr_result.get("pages", [])
    if not pages:
        sys.exit("error: non-text resolution requires at least one page record")
    pages_with_text = [page["page"] for page in pages if page.get("text_path")]
    if pages_with_text:
        sys.exit(
            "error: whole-document non-text resolution cannot invalidate already-written page text; "
            f"page(s) {pages_with_text} have text_path values"
        )

    # Never silently invalidate downstream artifacts from an earlier run.
    stale = [
        name for name in (
            f"classification_result_{doc_id}.json",
            f"redaction_result_{doc_id}.json",
        )
        if read_contract_data(case_id, name) is not None
    ]
    if stale:
        sys.exit(
            "error: non-text resolution would conflict with existing downstream contract(s): "
            + ", ".join(stale)
            + "; halt for a human audit instead of deleting or overwriting them"
        )

    verified_at = now_iso()
    verification = {
        "verified_by": verified_by,
        "verified_at": verified_at,
        "note": note,
        "downstream_disposition": "expert_review_only",
    }
    for page in pages:
        page["content_kind"] = "non_text_image"
        page["text_path"] = None
        page["mean_confidence"] = None
        page["uncertain_regions"] = []
        page["non_text_verification"] = dict(verification)

    ocr_result.update({
        "run_id": run_id,
        "extraction_method": "non_text_image",
        "ocr_status": "not_applicable",
        "document_mean_confidence": None,
        "ocr_quality": None,
        "cross_validation_status": "non_text_verified",
        "review_required": True,
        "reviewer_role": reviewer_role,
        "review_reason": (
            "Human verified that this document is non-text visual evidence. "
            "No transcription was selected or fabricated; automated downstream use is prohibited."
        ),
    })
    _write_contract(
        case_id,
        f"ocr_result_{doc_id}.json",
        ocr_result,
        "ocr_result.schema.json",
        held_by,
        run_id,
    )

    manifest_verification = {
        "verified_by": verified_by,
        "verified_at": verified_at,
        "note": note,
        "reviewer_role": reviewer_role,
    }
    fields = {
        "pages": len(pages),
        "ocr_status": "not_applicable",
        "ocr_text_path": None,
        "ocr_quality": None,
        "uncertain_region_count": 0,
        "cross_validation_status": "non_text_verified",
        "redacted_text_path": None,
        "document_type": "other",
        "classification_confidence": None,
        "extraction_method": "non_text_image",
        "downstream_disposition": "expert_review_only",
        "non_text_verification": manifest_verification,
    }
    ok, message = _dao.patch_manifest_document(case_id, doc_id, fields, held_by, run_id)
    if not ok:
        sys.exit(f"error: {message}")

    # Do not mark the top-level stage passed here. Other documents plus
    # redaction/chunking still have to finish; the orchestrator owns that state.
    return {
        "status": "non_text_verified",
        "case_id": case_id,
        "doc_id": doc_id,
        "pages": len(pages),
        "downstream_disposition": "expert_review_only",
        "reviewer_role": reviewer_role,
    }


def resolve_from_raw_ocr(case_id: str, doc_id: str, ocr_data: dict, page: int, chosen_reading: str | None,
                          resolved_by: str, note: str, held_by: str, run_id: str,
                          classifier=None, corrected_text: str | None = None,
                          classify: bool = True) -> dict:
    """Resolves one disagreed page using the original run_ocr() result
    (which has both reading_a and reading_b) plus a human's decision of
    which one is correct and why. Writes that page's text, updates
    ocr_result_{doc_id}.json's cross_validation_status + this page's
    resolution record. If every page is now agreed-or-resolved, continues
    on to classification + manifest update (same tail run_checkpoint1()
    uses when there's no disagreement at all)."""
    if (chosen_reading is None) == (corrected_text is None):
        sys.exit(
            "error: provide exactly one of chosen_reading or corrected_text")
    if chosen_reading is not None and chosen_reading not in ("reading_a", "reading_b"):
        sys.exit(f"error: chosen_reading must be reading_a or reading_b -- got {chosen_reading!r}")
    page_data = next((p for p in ocr_data["pages"] if p["page"] == page), None)
    if page_data is None:
        sys.exit(f"error: no page {page} in this OCR result")
    if corrected_text is not None:
        if not corrected_text.strip():
            sys.exit("error: corrected_text must contain a complete non-empty page transcription")
        if corrected_text in (page_data["reading_a"], page_data["reading_b"]):
            sys.exit(
                "error: corrected_text exactly matches an original reading; "
                "use --chosen-reading so the audit record identifies that reading")
        chosen_text = corrected_text
        resolution_choice = "human_corrected"
    else:
        chosen_text = page_data[chosen_reading]
        resolution_choice = chosen_reading

    _write_page_text(case_id, doc_id, page, chosen_text, held_by, run_id)

    ocr_result_path = case_dir(case_id) / f"ocr_result_{doc_id}.json"
    ocr_result = json.loads(ocr_result_path.read_text(encoding="utf-8"))
    page_entry = next(p for p in ocr_result["pages"] if p["page"] == page)
    page_entry["text_path"] = f"data/processed/{case_id}/{doc_id}/page_{page:03d}.md"
    resolution = {
        "chosen_reading": resolution_choice,
        "resolved_by": resolved_by,
        "resolved_at": now_iso(),
        "note": note,
    }
    if corrected_text is not None:
        resolution["corrected_text_sha256"] = hashlib.sha256(
            corrected_text.encode("utf-8")).hexdigest()
    page_entry["cross_validation"]["resolution"] = resolution

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

    if not classify:
        # A bundle's disagreements being resolved does not make it one document.
        # Reaching the shared tail here would write the very document_type
        # --bundle-ocr withheld -- found on CASE_909, where resolving DOC_005's
        # 8 pages labelled a 19-page bundle `diagnosis_certificate` from its
        # first page, over two imaging REPORTs, an 입퇴원확인서 and two
        # 진료비 명세서.
        _dao._update_run_state(case_id, run_id, "document_processing", "in_progress", held_by)
        return {"status": "bundle_ocr_complete", "case_id": case_id, "doc_id": doc_id,
                "pages": len(ocr_result["pages"]),
                "cross_validation_status": ocr_result["cross_validation_status"],
                "next_action": "derive boundaries from this text, then split; children classify individually"}

    first_page_agreed_or_resolved = ocr_result["pages"][0]
    first_page_text = Path(ROOT / first_page_agreed_or_resolved["text_path"]).read_text(encoding="utf-8")
    return _finish_checkpoint1(case_id, doc_id, run_id, held_by, first_page_text, classifier=classifier)


@trace_mod.traced("stage.document_processing")
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
            classify=not args.bundle_ocr,
            max_workers=args.workers,
        )
    except ProviderConfigError as exc:
        sys.exit(f"error: {exc}")
    except ProviderExecutionError as exc:
        sys.exit(f"error: {exc}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] in {"blocked_disagreement", "blocked_segmentation"}:
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
        classifier = None if args.bundle_ocr else build_classifier_provider(
            classifier_provider_name=args.classifier_provider,
            classifier_model=args.classifier_model,
        )
        corrected_text = None
        if args.corrected_text_file is not None:
            corrected_path = Path(args.corrected_text_file)
            try:
                corrected_text = corrected_path.read_text(encoding="utf-8")
            except OSError as exc:
                sys.exit(
                    f"error: could not read corrected transcription file "
                    f"{corrected_path}: {exc}")
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
            classify=not args.bundle_ocr,
            classifier=classifier,
            corrected_text=corrected_text,
        )
    except ProviderConfigError as exc:
        sys.exit(f"error: {exc}")
    except ProviderExecutionError as exc:
        sys.exit(f"error: {exc}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _resolve_non_text_from_args(args):
    result = resolve_as_non_text(
        args.case_id,
        args.doc_id,
        verified_by=args.verified_by,
        reviewer_role=args.reviewer_role,
        note=args.note,
        held_by=args.held_by,
        run_id=args.run_id,
    )
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
    parser.add_argument(
        "--workers", type=int, default=None, metavar="N",
        help="Pages OCR'd concurrently (default 4, or HARNESS_OCR_WORKERS). 1 = "
             "sequential. Wall-time only: pages are independent, and results are "
             "returned in source order either way.")
    parser.add_argument(
        "--bundle-ocr", action="store_true",
        help="OCR an unsplit bundle without classifying it, so segmentation can "
             "derive boundaries from real page text. Skips classification and the "
             "document_type manifest write -- split_bundle then redistributes "
             "these pages to the children, which classify individually. Still "
             "refuses a superseded bundle.")


# Reserved subcommand names dispatched explicitly; anything else is treated as
# the legacy positional `run` invocation (CASE DOC PDF ...) for backward
# compatibility with document-pipeline.md and existing callers.
_SUBCOMMANDS = {"run", "resolve-disagreement", "resolve-non-text", "classify-only"}


def _configure_trace(args) -> None:
    """Point tracing at this case/run, once, as early as the ids are known.

    Every span raised deeper in the call tree (provider calls, DAO locks,
    schema validation, the page pool) is dropped until this runs, so it has to
    happen before any work starts rather than beside it. A tool invoked
    without both ids simply is not traced -- there is nowhere case-scoped to
    put the shards, and inventing a location would scatter them.
    """
    case_id = getattr(args, "case_id", None)
    run_id = getattr(args, "run_id", None)
    if case_id and run_id:
        trace_mod.configure(case_id, run_id)


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
    resolution_source = resolve_parser.add_mutually_exclusive_group(required=True)
    resolution_source.add_argument(
        "--chosen-reading",
        choices=["reading_a", "reading_b"],
        help="Which of the two independent reads the human verified as correct",
    )
    resolution_source.add_argument(
        "--corrected-text-file",
        help=(
            "UTF-8 file containing the complete page transcription verified by a human "
            "when neither independent reading is correct"
        ),
    )
    resolve_parser.add_argument("--resolved-by", required=True, help="Name of the human (e.g. 손해사정사) making the call")
    resolve_parser.add_argument("--note", required=True, help="Why this reading is correct")
    resolve_parser.add_argument("--held-by", required=True)
    resolve_parser.add_argument("--run-id", required=True)
    resolve_parser.add_argument(
        "--bundle-ocr", action="store_true",
        help="This document is an unsplit bundle: resolve its page(s) but do "
             "not classify it. Resolving a disagreement does not make a bundle "
             "one document -- without this the shared tail writes the very "
             "document_type the bundle OCR withheld.")
    resolve_parser.add_argument(
        "--classifier-provider",
        choices=SUPPORTED_PROVIDERS,
        help="Provider for post-resolution document classification",
    )
    resolve_parser.add_argument(
        "--classifier-model",
        help="Model name for --classifier-provider",
    )

    non_text_parser = sub.add_parser(
        "resolve-non-text",
        help="Record a human decision that the whole document is non-text visual evidence",
    )
    non_text_parser.add_argument("case_id")
    non_text_parser.add_argument("doc_id")
    non_text_parser.add_argument("--verified-by", required=True)
    non_text_parser.add_argument(
        "--reviewer-role", choices=["손해사정사", "의사", "법률전문가"], required=True,
        help="Expert role that must review the visual evidence outside the automated text pipeline",
    )
    non_text_parser.add_argument("--note", required=True)
    non_text_parser.add_argument("--held-by", required=True)
    non_text_parser.add_argument("--run-id", required=True)

    classify_parser = sub.add_parser(
        "classify-only",
        help="Classify a document whose pages already exist, without re-reading it",
    )
    classify_parser.add_argument("case_id")
    classify_parser.add_argument("doc_id")
    classify_parser.add_argument("--held-by", required=True)
    classify_parser.add_argument("--run-id", required=True)
    classify_parser.add_argument("--classifier-provider", choices=SUPPORTED_PROVIDERS,
                                  help="Provider for classification")
    classify_parser.add_argument("--classifier-model", help="Model for --classifier-provider")

    # Backward compatibility: the legacy form is `... CASE DOC PDF --held-by ...`
    # with no subcommand token. If the first arg isn't a known subcommand (and
    # isn't a help flag), route to the `run` parser so old invocations keep working.
    if argv and argv[0] not in _SUBCOMMANDS and argv[0] not in ("-h", "--help"):
        args = run_parser.parse_args(argv)
        _configure_trace(args)
        _run_from_args(args)
        return

    args = ap.parse_args(argv)
    _configure_trace(args)
    if args.command == "classify-only":
        # The provider is built lazily: a printed form title decides most types
        # with no model call, and constructing one would resolve credentials for
        # a call that never happens.
        classifier = None
        if args.classifier_provider or args.classifier_model:
            classifier = build_classifier_provider(
                classifier_provider_name=args.classifier_provider,
                classifier_model=args.classifier_model)
        result = classify_existing(args.case_id, args.doc_id, held_by=args.held_by,
                                    run_id=args.run_id, classifier=classifier)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "resolve-disagreement":
        _resolve_from_args(args)
    elif args.command == "resolve-non-text":
        _resolve_non_text_from_args(args)
    elif args.command == "run":
        _run_from_args(args)
    else:
        ap.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
