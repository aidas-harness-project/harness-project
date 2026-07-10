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

**Where:** `document-pipeline`, checkpoint 1 (P8's dual-path cross-validation).

**Current:** accepted as a known, unresolved risk -- flagged inline in `harness-guardrails` P8, not fixed.

**Problem:** the cross-validation step requires a vision-capable model to read the raw, unredacted page image (that's the point -- it has to see what the OCR engine saw, before redaction). If that model isn't under an equivalent no-data-retention arrangement as the trusted OCR engine, every cross-validation run sends PII to a less-trusted destination.

**Options on the table (see conversation history for the full discussion):**
- Extend an equivalent no-retention trust arrangement to the vision-model deployment (procurement/vendor question, not an architecture change).
- Replace the vision-model path with a second independent OCR engine instead -- avoids needing a new "vision-capable trust tier" at all.

**To resolve:** a deployment/vendor decision, not something to default on silently.
