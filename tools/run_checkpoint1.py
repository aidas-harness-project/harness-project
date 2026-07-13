"""Checkpoint 1 wrapper: OCR + cross-validation + classification, in one call.

Automates the mechanical sequence a document-pipeline subagent normally
performs: run dual-path OCR (ocr_extract.run_ocr, in-process, not a
subprocess-of-a-subprocess), write each agreed page (dao.py's
write-page-text logic, called directly), classify the document from its
first agreed page's already-transcribed TEXT (one real claude -p call --
reasoning over text, not re-viewing the raw image, which is a smaller PII
exposure footprint than the original design's "same vision-model call that
read the page" -- see open-decisions.md #3), assemble and write
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
import json
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from dao import case_dir, atomic_write_json, now_iso, load_registry, validate_instance
from dao import acquire_lock_blocking, release_lock, atomic_write_text, processed_dir
import dao as _dao
from ocr_extract import run_ocr

ROOT = Path(__file__).resolve().parent.parent

DOCUMENT_TYPES = ["insurance_certificate", "insurance_policy", "diagnosis_certificate",
                   "medical_record", "imaging_report", "receipt", "insurer_response", "other"]

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


def classify_document(text: str) -> dict:
    """One real claude -p call, reasoning over already-transcribed text
    (no raw image view). Fails loud on an unparseable response -- same
    fail-safe discipline as ocr_extract.compare(), not a silent guess."""
    prompt = CLASSIFY_PROMPT_TEMPLATE.format(types=", ".join(DOCUMENT_TYPES), text=text[:3000])
    result = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    if result.returncode != 0:
        sys.exit(f"error: classification claude call failed: {result.stderr.strip()}")
    raw = result.stdout.strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        sys.exit(f"error: classification response wasn't parseable JSON, refusing to guess: {raw!r}")
    try:
        parsed = json.loads(m.group(0))
    except json.JSONDecodeError:
        sys.exit(f"error: classification response wasn't valid JSON, refusing to guess: {raw!r}")
    if parsed.get("predicted_document_type") not in DOCUMENT_TYPES:
        sys.exit(f"error: classification returned an unknown document_type {parsed.get('predicted_document_type')!r}")
    return parsed


def _assemble_ocr_result(case_id, doc_id, run_id, ocr_data):
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
        "created_at": now_iso(), "model_info": {"model_name": "claude-cli", "prompt_version": "ocr_extraction_v0.1"},
        "document_id": doc_id,
        "ocr_engine": "claude-cli (no dedicated OCR engine, see open-decisions.md)",
        "vision_model_name": "claude-cli (stand-in for a dedicated second engine, see open-decisions.md)",
        "uncertain_confidence_threshold": 1.0,
        "extraction_method": "ocr", "ocr_status": "completed", "pages": pages_out,
        "document_mean_confidence": None,
        "ocr_quality": "low" if any_disagreement else "high",
        "cross_validation_status": "disagreed_pending_review" if any_disagreement else "agreed",
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


def run_checkpoint1(case_id: str, doc_id: str, pdf_path: str, held_by: str, run_id: str, progress=None) -> dict:
    pdf_path = Path(pdf_path)
    ocr_data = run_ocr(case_id, doc_id, pdf_path, progress=progress)

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

    return _finish_checkpoint1(case_id, doc_id, run_id, held_by, ocr_data["pages"][0]["reading_a"])


def _finish_checkpoint1(case_id, doc_id, run_id, held_by, first_page_text):
    """Shared tail: classify from page 1's text, write
    classification_result_{doc_id}.json, update document_manifest.json.
    Called both by run_checkpoint1() (no disagreement) and
    apply_disagreement_resolution() (once every page is resolved)."""
    classification = classify_document(first_page_text)
    ocr_result = json.loads((case_dir(case_id) / f"ocr_result_{doc_id}.json").read_text(encoding="utf-8"))

    classification_result = {
        "case_id": case_id, "run_id": run_id, "component": "document-pipeline", "status": "success",
        "created_at": now_iso(), "model_info": {"model_name": "claude-cli", "prompt_version": "classification_v0.1"},
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
                          resolved_by: str, note: str, held_by: str, run_id: str) -> dict:
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
    return _finish_checkpoint1(case_id, doc_id, run_id, held_by, first_page_text)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case_id")
    ap.add_argument("doc_id")
    ap.add_argument("pdf_path")
    ap.add_argument("--held-by", required=True)
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args()

    result = run_checkpoint1(args.case_id, args.doc_id, args.pdf_path, args.held_by, args.run_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] == "blocked_disagreement":
        sys.exit(1)


if __name__ == "__main__":
    main()
