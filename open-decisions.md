# Open Decisions

Deferred decisions from the 2026-07-10 restructure, tracked explicitly so they don't get lost. Each entry: what's in place now, what's undecided, and what would resolve it.

## 1. Redaction model choice

**Where:** `document-pipeline`, checkpoint 2 (Redaction).

**Current:** general LLM.

**Candidate:** OpenMed -- an open-source suite of self-hosted biomedical NER models (Hugging Face), including PHI/PII de-identification. Matches the no-data-collection trust property already required for the OCR engine. Not yet adopted.

**To resolve:** verify current library/model maturity and integration effort before switching. Low urgency at PoC scale, but should be revisited if redaction quality or data-handling trust becomes a concern.

## 2. Document-assembly template rules

**Where:** `screening-report`, `draft-report`, `denial-validation` (rebuttal points) -- everything that produces a narrative document via `tools/document_assembly.py`.

**Status: partially resolved (2026-07-13).** The structure itself is now defined -- `templates/draft-report.md`, `templates/screening-report.md`, `templates/rebuttal-points.md`, `templates/forbidden-expressions.md`, `templates/component-output.md`, adopted from `wiki/templates/` (which had extracted the real section structure from the 4 ground-truth reports back on 2026-07-08, but that never made it into this repo or into `pipeline.md`/these schemas until now). `templates/` is the go-forward authoritative copy; wiki's copy will be caught up separately and may drift.

**What's covered:** `template_id` values `배상책임_후유장해형` (변형 A, sections I~VII) and `진단수술비형` (변형 B, sections I~VI), both grounded in real ground-truth cases (CASE_003/004/005/006).

**Still undecided:**
- `실손형`/`기타형` `template_id`s have no ground-truth basis yet -- no case in `data/ground_truth/` is that case type. `templates/draft-report.md` flags this as TODO; 변형 A is the interim fallback with a `warnings` entry from draft-report until real material arrives.
- `tools/document_assembly.py` itself still renders whatever sections it's given, in order, with no validation against a `template_id`'s required section list. Encoding `templates/draft-report.md`'s structure into something the tool (or a wrapper) checks section presence/order against is still a follow-up -- not done as part of this pass, which only adopted the structure as reference documentation.

**To resolve fully:** (a) obtain or construct ground-truth-backed structure for 실손형/기타형, (b) build the document-assembly enforcement wrapper described above.

## 3. Vision-model PII exposure in cross-validation

**Where:** `document-pipeline`, checkpoint 1 (P8's dual-path cross-validation, `tools/ocr_extract.py`).

**Current:** accepted as a known, unresolved risk -- flagged inline in `harness-guardrails` P8, not fixed. Provider abstraction lets the run choose `claude-cli`, `openai-api`, or future provider paths, but it does not by itself solve data-retention/privacy.

**Problem:** P8's two readers must see the raw, unredacted page image (that's the point -- they have to see what's actually on the page before redaction). The comparator and classifier may also see unredacted extracted text. If any configured provider path is not under a no-data-retention arrangement, every checkpoint-1 run may send PII to that destination.

**Options on the table (see conversation history for the full discussion):**
- Establish a no-retention trust arrangement for the deployment running these reads (procurement/vendor question, not an architecture change).
- Replace one or both LLM-vision reads with a local or vendor-approved OCR engine once #4 below is resolved.

**To resolve:** a deployment/vendor decision, not something to default on silently.

## 4. No dedicated OCR engine -- provider-based P8 may still be LLM vision

**Where:** `tools/ocr_extract.py`, used by `document-pipeline` checkpoint 1.

**Current:** partially resolved for execution portability. The old stand-in was two fresh `claude-cli` invocations with no shared context. `tools/ocr_extract.py` now has provider-configurable `reader_a`, `reader_b`, and `comparator` paths, so Codex-compatible runs can use configured API providers such as `openai-api` and are no longer blocked solely by a missing Claude CLI. `ocr_result.json` records provider/model labels through `ocr_engine`, `vision_model_name`, and component `model_info`.

**Remaining problem:** provider-configurable does not mean technology-independent. Same-provider runs such as `openai-api` + `openai-api` or `claude-cli` + `claude-cli` are separate calls, but they still share more failure modes than two genuinely different technologies would. Cross-provider LLM vision can reduce some shared model-specific risk, but it is still not the same as pairing a traditional OCR/text-extraction engine with an independent vision read. Current LLM-vision provider paths also do not produce per-block numeric OCR confidence; P8's hard signal remains page-level agreed/disagreed.

**To resolve:** integrate an actual OCR/text-extraction engine (e.g. Upstage OCR, Tesseract, embedded-PDF text extraction, or similar) as one of the two reading paths, then configure P8 so the second path is a genuinely independent reader rather than another call to the same class of LLM-vision model.
