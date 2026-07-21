# Open Decisions

Deferred decisions from the 2026-07-10 restructure, tracked explicitly so they don't get lost. Each entry: what's in place now, what's undecided, and what would resolve it.

## 1. Redaction model choice

**Where:** `document-pipeline`, checkpoint 2 (Redaction).

**Current:** `tools/redact_document.py` provides an executable checkpoint-2 path that redacts through the `tools/redaction.py` **Redactor abstraction**. The only implementation today is `LlmRedactor`, which uses a configured LLM provider (dev default `codex-cli`) ONLY to identify PII spans; the redacted text is then built deterministically by substituting those spans in the source, so non-PII content is preserved by construction (omission/fabrication impossible). A possible PII leak -- structured PII surviving the output (`scan_residual_pii`) or a model-named value not found verbatim in the source -- hard-fails the document; over-redaction risk sets `review_required`. The *model choice* is still open -- what's settled is the seam, so a dedicated NER de-identification model (returning offsets, which the span design already consumes) can replace the LLM without touching the tool.

**Candidate:** OpenMed -- an open-source suite of self-hosted biomedical NER models (Hugging Face), including PHI/PII de-identification. As a span-based NER model it would return entity offsets rather than a rewritten page, which the `RedactionOutcome.spans` field already anticipates; it may offer more deterministic, verifiable entity handling than a general LLM. Not yet adopted. (The earlier offline local-llm/Ollama redactor was removed -- it never produced parseable redactions on real content; see `known-gaps.md` item 16.)

**To resolve:** verify OpenMed (or another de-identification model) maturity and integration effort, then add a `NerRedactor` behind the existing seam. Low urgency at PoC scale; revisit if redaction quality or data-handling trust becomes a concern.

## 2. Document-assembly template rules

**Where:** `screening-report`, `draft-report`, `denial-validation` (rebuttal points) -- everything that produces a narrative document via `tools/document_assembly.py`.

**Status: partially resolved (2026-07-13).** The structure itself is now defined -- `templates/draft-report.md`, `templates/screening-report.md`, `templates/rebuttal-points.md`, `templates/forbidden-expressions.md`, `templates/component-output.md`, adopted from `wiki/templates/` (which had extracted the real section structure from the 4 ground-truth reports back on 2026-07-08, but that never made it into this repo or into `pipeline.md`/these schemas until now). `templates/` is the go-forward authoritative copy; wiki's copy will be caught up separately and may drift.

**What's covered:** `template_id` values `배상책임_후유장해형` (변형 A, sections I~VII) and `진단수술비형` (변형 B, sections I~VI), both grounded in real ground-truth cases (CASE_003/004/005/006).

**Enforcement wrapper: RESOLVED 2026-07-14.** `templates/registry.json` (machine-readable section contracts derived from the template .md files) + `document_assembly.py --template <key>` -- validates section presence AND order before anything touches disk, hard-exit on mismatch (same fail/don't-persist contract as its sidecar validation). Enforced keys: `배상책임_후유장해형`, `진단수술비형`, `screening_report`. `rebuttal_points` deliberately has no registry entry (per-reason repeating structure -- fixed-list enforcement can't express it; stays prompt-enforced + critic-verified). Agent specs updated to pass the flag; `claim-analysis.md` now requires canonical registry keys as `template_id`.

**Still undecided:**
- `실손형`/`기타형` `template_id`s have no ground-truth basis yet -- no case in `data/ground_truth/` is that case type. `templates/draft-report.md` flags this as TODO; 변형 A is the interim fallback with a `warnings` entry from draft-report until real material arrives.

**To resolve fully:** obtain or construct ground-truth-backed structure for 실손형/기타형.

## 3. Vision-model PII exposure in cross-validation

**Where:** `document-pipeline`, checkpoint 1 (P8's dual-path cross-validation, `tools/ocr_extract.py`).

**Current:** unresolved. Every available provider (claude-cli / codex-cli / openai-api) sends the page image or extracted text to an external service. The offline local path that would have closed the on-machine transmission gap was removed (never produced usable transcriptions on real Korean pages; single-machine Windows/E: only -- `known-gaps.md` item 16). So this risk is back to open, exactly as it was before that path was attempted.

**Problem:** P8's two readers must see the raw, unredacted page image (that's the point -- they have to see what's actually on the page before redaction). The comparator and classifier may also see unredacted extracted text. If any configured provider path is not under a no-data-retention arrangement, every checkpoint-1 run may send PII to that destination.

**Options on the table (see conversation history for the full discussion):**
- Establish a no-retention trust arrangement for the deployment running these reads (procurement/vendor question, not an architecture change).
- Re-introduce a genuinely working on-machine reader (a real OCR engine, tied to #4) -- but only one validated on real Korean case documents, not the removed synthetic-only stack.

**To resolve:** a deployment/vendor decision, not something to default on silently.

## 4. No dedicated OCR engine -- provider-based P8 may still be LLM vision

**Where:** `tools/ocr_extract.py`, used by `document-pipeline` checkpoint 1.

**Current:** no dedicated OCR engine exists. Every P8 reader is LLM-vision-backed (claude-cli / codex-cli / openai-api), so both reads share one extraction technology class and `ocr_result.json` records `cross_validation_mode: single_technology_weak_p8_poc` honestly. `dual_technology` remains a defined-but-unreachable schema value, reserved for the day a real OCR engine is added as one of the two reading paths. `ocr_result.json` records the actual provider/model labels.

**Problem:** two LLM-vision reads (even from different vendors) can produce a correlated confident error that P8 cannot catch -- the protection is real but weaker than the original design intended, which assumed two genuinely different extraction technologies.

**History:** an offline Tesseract (`local-ocr`) + Ollama-vision (`local-vlm`) pair was built to be that technology-independent second reader, but it never transcribed real Korean case pages (`qwen3-vl:4b` returned empty output; smoke-test only) and was single-machine (Windows/E:) -- it was removed rather than left as dead, misleading scaffolding (`known-gaps.md` item 16).

**To resolve:** integrate an actual OCR engine (Tesseract validated on real Korean claim documents, Upstage OCR, or similar) as one of the two reading paths, keeping an LLM vision model as the genuinely independent second path -- and prove it on real content before flipping any run to `dual_technology`.

## 5. Whole-document non-text visual evidence

**Status: resolved 2026-07-15.** A document consisting entirely of photographs or other visual evidence is represented as `extraction_method: non_text_image`, `ocr_status: not_applicable`, `cross_validation_status: non_text_verified`, and `downstream_disposition: expert_review_only`. A genuine human must make this decision through `run_checkpoint1.py resolve-non-text`; the tool preserves the original P8 disagreement, creates no page text or model-generated image description, skips text classification/redaction, and records an explicit exclusion in `page_chunks.json`.

This does not resolve mixed text/image documents. The whole-document command refuses any document with an already-written text page, so a future per-page mixed-content contract cannot silently reuse this bypass.
