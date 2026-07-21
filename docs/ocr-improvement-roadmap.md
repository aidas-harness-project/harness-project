# OCR Improvement Roadmap

> **SUPERSEDED 2026-07-21 (PR #8 review).** The offline local stack this roadmap
> plans against (`tools/local_runtime.py`, `local-ocr`/`local-vlm`/`local-llm`,
> the `.runtime/` model install) was removed: it never transcribed real Korean
> pages reliably and was single-machine only. The provider abstraction it builds
> on remains. A genuinely technology-independent reader (a real OCR engine) is
> deferred to `open-decisions.md` #4; if that work is revived, this roadmap is a
> historical reference, not a live plan, and its tool paths no longer exist.

This roadmap evolves the provider-based OCR architecture incrementally. It
keeps the `fix_codex` provider abstraction and adds local quality controls only
after the preceding layer has been validated on representative Korean
insurance documents.

## Guiding sequence

`v0 provider baseline` -> `v1 local dual-reader` -> `v1.5 PDF routing` ->
`v2 structured regions` -> `v3 multi-engine voting` -> `v4 bounded
correction` -> `v5 operational benchmark`

Advancement is evidence-gated. A runtime preflight or synthetic smoke test
proves only that a component loads; it does not prove real-document quality.
P8 disagreement remains a hard halt in every version.

## v0 — Provider baseline

Goal: establish that reader, comparator, classifier, schemas, and provider
metadata work independently of a hard-coded Claude path.

Deliverables and exit criteria:

- fixture-provider and CLI-provider regression tests pass;
- reader/comparator/classifier selection works through CLI and environment
  configuration;
- same-provider reads are labelled `single_technology_weak_p8_poc`;
- schema changes remain backward compatible; and
- generated run artifacts do not enter normal code-review diffs.

This version does not add layout detection, voting, confidence aggregation, or
LLM correction.

## v1 — Local dual-reader PoC

Goal: validate a fully local checkpoint-1 path on small, representative real
document samples.

Reference profile:

- reader A: Tesseract `local-ocr`, initially `kor+eng:6`;
- reader B: preloaded loopback Ollama `local-vlm`;
- comparator/classifier: preloaded loopback Ollama `local-llm`; and
- cross-validation mode: `dual_technology`.

Start with 3–5 pages per available category rather than a whole case: diagnosis
certificates, medical records, receipts, claim forms, and insurer responses.
Record empty output, hallucination, Korean corruption, timeout, latency, and
repeatability. Do not promote the profile unless raw dual reads are retained,
disagreement blocks downstream use, and critical names/dates/codes/amounts are
preserved in real scans.

## v1.5 — PDF routing

Goal: avoid OCR when a trustworthy embedded text layer exists.

Add page-level detection and route pages as `embedded_text`, `ocr`, or `mixed`.
The recorded `extraction_method` must match the path actually used. Scan-only
pages continue through P8; mixed documents require an explicit per-page
contract and must not reuse the whole-document `non_text_image` resolution.

## v2 — Line and region structure

Goal: replace page-only plain text as the internal representation with
traceable lines and regions.

Introduce line/region identifiers, reading order, pixel or normalized bounding
boxes, engine confidence, and `uncertain_regions`. Compare Tesseract boxes,
OpenCV baselines, and a dedicated detector before choosing the detector. Page
text remains a deterministic assembly of the structured result.

Exit criteria include populated uncertainty regions, inspectable confidence,
and region-level review routing without losing the page-level audit trail.

## v3 — Multi-engine OCR and ROVER-style voting

Goal: align two or more structured OCR outputs and produce a traceable voted
candidate.

Add one dedicated OCR engine candidate (for example PaddleOCR or Surya), then
implement line/token alignment, confidence calibration, and voting. Names,
dates, KCD codes, diagnoses, periods, amounts, and insurer decisions are
critical fields: disagreement on them cannot be hidden by a page-wide majority.
All engine readings and the voted result remain available for audit.

## v4 — Evidence-bounded correction and human review

Goal: use an LLM as a correction proposer, never as an untracked replacement
writer.

Every proposal records region, before/after text, evidence-based reason,
confidence, and a diff. Critical-field changes always require genuine human
review. The final text, original engine candidates, model proposal, and human
decision remain separate in the audit trail.

## v5 — Benchmark and operating profile

Goal: select the production OCR profile using repeatable measurements.

Compare the Claude baseline, Tesseract plus local VLM, dedicated OCR pairs,
multi-engine voting, and bounded correction. Measure CER, WER, critical-field
accuracy, table preservation, hallucination rate, false agreement rate, human
review rate, page latency, local-only compliance, and repeatability.

Initial target criteria:

- critical-field accuracy at least 95% on the approved benchmark;
- hallucination and false agreement as close to zero as practical;
- review volume low enough for the intended workflow;
- no external transmission in the local operating profile; and
- stable repeated results on identical inputs.

## Work packages

1. Provider/local baseline: provider regression, target-machine preflight,
   local checkpoint 1, baseline comparison, failure catalogue.
2. PDF handling: text-layer detection, embedded/scan/mixed routing, page-level
   extraction metadata and tests.
3. Structured OCR: line/bbox/confidence contract, uncertainty population, and
   region-level disagreement.
4. OCR ensemble: second OCR engine, alignment, voting, and critical-field
   weighting.
5. Correction/review: correction-diff contract, bounded proposals,
   critical-field human routing, and audit trail.

The immediate milestone is v1. v2 design begins only after v1 evidence shows
which failures come from Tesseract, the vision reader, or the comparator.
