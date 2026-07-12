# Open Decisions

Deferred decisions from the 2026-07-10 restructure, tracked explicitly so they don't get lost. Each entry: what's in place now, what's undecided, and what would resolve it.

## 1. Redaction model choice

**Where:** `document-pipeline`, checkpoint 2 (Redaction).

**Current:** general LLM.

**Candidate:** OpenMed -- an open-source suite of self-hosted biomedical NER models (Hugging Face), including PHI/PII de-identification. Matches the no-data-collection trust property already required for the OCR engine. Not yet adopted.

**To resolve:** verify current library/model maturity and integration effort before switching. Low urgency at PoC scale, but should be revisited if redaction quality or data-handling trust becomes a concern.

## 2. Document-assembly template rules

**Where:** `screening-report`, `draft-report`, `denial-validation` (rebuttal points) -- everything that produces a narrative document via `tools/document_assembly.py`.

**Current:** the tool works generically -- it renders whatever sections it's given, in the order given, and auto-generates `[E#]` tags + the `.evidence.json` sidecar regardless of content.

**Undecided:** the actual required sections/fields per `case_type`'s `template_id` for `screening_report.md`, `draft_report_v1.md`/`v2.md`, and `rebuttal_points.md`.

**To resolve:** waiting on the user to provide the rules/structure for these target documents. Once provided, encode them as template definitions the document-assembly tool (or a wrapper around it) validates section presence/order against -- see `pipeline.md`'s note.

## 3. Vision-model PII exposure in cross-validation

**Where:** `document-pipeline`, checkpoint 1 (P8's dual-path cross-validation, `tools/ocr_extract.py`).

**Current:** accepted as a known, unresolved risk -- flagged inline in `harness-guardrails` P8, not fixed.

**Problem:** both cross-validation reads require a Claude CLI invocation to see the raw, unredacted page image (that's the point -- it has to see what's actually on the page, before redaction). If the model isn't under a no-data-retention arrangement, every cross-validation run sends PII to that destination.

**Options on the table (see conversation history for the full discussion):**
- Establish a no-retention trust arrangement for the deployment running these reads (procurement/vendor question, not an architecture change).
- Replace one or both Claude reads with a real OCR engine once #4 below is resolved.

**To resolve:** a deployment/vendor decision, not something to default on silently.

## 4. No dedicated OCR engine -- Claude CLI stands in for both cross-validation reads

**Where:** `tools/ocr_extract.py`, used by `document-pipeline` checkpoint 1.

**Current:** per explicit direction, both of P8's independent reads are Claude CLI invocations (fresh process, no shared context) rather than one being a real OCR engine and the other a vision model. `ocr_result.json`'s `ocr_engine`/`uncertain_confidence_threshold` fields say so honestly rather than implying a real engine exists; there's no per-block numeric confidence, only a binary agreed/disagreed verdict per page.

**Problem:** two invocations of the *same* underlying model share more failure modes than two genuinely different technologies would. This catches transient/one-off misreads (the two calls disagreeing by chance) but not systematic blind spots (both calls confidently misreading the same unusual layout/handwriting the same way). P8's protection is real but weaker than the original design intended.

**To resolve:** integrate an actual OCR engine (e.g. Upstage OCR, Tesseract, or similar -- see the original schema comments for candidates) as one of the two reading paths, keeping Claude as the second, genuinely independent, path.
