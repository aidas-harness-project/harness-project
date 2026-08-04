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

## 6. Evaluation measures R-code accuracy only, not decision_type or source-location accuracy

**Where:** the evaluation contract (`schemas/evaluation_result.schema.json`, `.claude/agents/evaluation.md`), against `denial_reason_result.json`.

**Current:** evaluation scores the denial/reduction classification as R-code Top-1/Top-3. Two things the contract now *records* are not *scored*:

- `decision_type` (denial vs reduction). A reason can carry the right R-code and the wrong decision type; today that counts as fully correct.
- Source-location accuracy. Since the 2026-07-22 audit fix (`known-gaps.md` item 20) every denial-family evidence reference must carry `document_id` + `page` + `quote`, so the data to score this now exists -- but nothing scores whether the cited location is the *right* one, only that one is present and resolvable.

**Problem:** the harness can look accurate on the metric it reports while being wrong in ways the metric cannot see -- the same failure shape as the schema gaps in item 20, one level up. Under-measurement is not neutral: it decides what the PoC's Go/No-Go is actually evidence for.

**Why it is not just "add two more numbers":** each needs a real definition first.
- Is a correct R-code with an incorrect `decision_type` a miss, or partial credit? They are not independent -- the reviewed codebook already constrains which decision types each code admits (`x-codebook.applicable_decision_types`), so some combinations are contradictions rather than errors of degree.
- What counts as a correct location? Exact page match, or overlap with the ground-truth adjuster's cited span? Quote-level or page-level? A stricter rule punishes a correct finding cited one page off; a looser one cannot distinguish grounded from approximately-grounded.
- Ground truth for locations may not exist in comparable form: the real adjuster's report cites its own documents, not the case's `DOC_XXX`/page coordinates.

**To resolve:** a deliberate evaluation design pass, deciding the scoring rules above before implementing them. Explicitly not built at gate-building time -- inventing a metric to fill the gap would produce a number nobody could interpret, which is worse than a documented absence.

## 7. Should segmentation move after OCR, merging Stage 1 into Stage 2?

**Where:** `tools/segment_case.py` (Stage 1, `document_segmentation`) and
`tools/run_checkpoint1.py` (Stage 2, `document_processing`).

**Current:** Stage 1 decides document boundaries from the raw PDF -- vision
contact sheets, or the deterministic text-anchor path for a born-digital
bundle -- and Stage 2 then OCRs whatever documents that produced. Stage 1
strictly precedes Stage 2 and blocks it case-wide until every PDF is human-
cleared or split.

**What prompted this:** two things stopped holding.

The first is that Stage 1's original cost argument was "cut the bundle so OCR
runs on right-sized documents". Once `ocr_extract.py` began using embedded PDF
text (2026-08-03), a born-digital bundle takes no OCR at all, so that saving is
zero for exactly the documents Stage 1 was hardest at.

The second is that boundary evidence is better after extraction than before it.
The text-anchor path already reads the text layer to find titles, and reaches
precision 1.0000 against the human baseline in 0.75s with no model calls --
against ~10 vision calls for the same bundle. Doing that work in Stage 2, on
text that has been extracted once, would let a scan get the same treatment: OCR
the pages, then split on the transcribed titles, instead of guessing boundaries
from downsampled contact sheets.

The concrete failure that made this visible: CASE_907's DOC_005 is 19 scanned
pages containing a 진단서, an English lab REPORT, an 입퇴원확인서, and two
진료비 명세서. It was intaken `segmentation_status: not_required`, so Stage 2
classified all 19 pages as one `diagnosis_certificate` -- the first page's type.
Every downstream stage now sees four documents' worth of pages under one wrong
label. Nothing in Stage 1 could have caught it cheaply, because the boundaries
are legible in the page text and not in the page images.

**Against merging:** Stage 1's human approval gate is the only point where a
person confirms document structure before any downstream work rests on it, and
it is deliberately separate from Stage 2's P8 gate (structure vs transcription
fidelity -- two different judgments by two different reviewers). Merging risks
collapsing them into one "approve everything" moment. Re-OCR after a split is
also not free unless page text is reused, which the current split path does not
do. And Stage 1 currently produces `superseded_bundle` lineage that Stage 2's
preflight relies on; that ordering would have to be rebuilt.

**Note:** policy bundles are excluded from this question -- they are not split
at all (see `loss-adjustment-pipeline`'s segmentation check for why). This is
about medical/mixed scan bundles, where per-document `document_type` is the
thing splitting exists to get right.

**To resolve:** decide whether Stage 1 becomes (a) unchanged, (b) a
post-extraction step inside Stage 2 operating on transcribed text, or (c)
split by source kind -- text-anchor stays pre-OCR for born-digital bundles,
scans defer to post-OCR. Measure (c) against CASE_907's DOC_005 before
choosing, since it is the only real mis-split on record.
