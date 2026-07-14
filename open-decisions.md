# Open Decisions

Deferred decisions from the 2026-07-10 restructure, tracked explicitly so they don't get lost. Each entry: what's in place now, what's undecided, and what would resolve it.

## 1. Redaction model choice

**Where:** `document-pipeline`, checkpoint 2 (Redaction).

**Current:** `tools/redact_document.py` provides an executable checkpoint-2 path. Privacy-sensitive runs use the `local-llm` provider backed by a model already present in a loopback-only Ollama deployment. Missing runtime/model fails closed; there is no external fallback. Synthetic quality checks are not yet passing: `qwen3:1.7b` returned valid JSON but failed to replace detected name/phone values, while the `qwen3:4b` CPU run was stopped after excessive latency. Local execution therefore resolves the transmission path, not redaction correctness or throughput.

**Candidate:** OpenMed -- an open-source suite of self-hosted biomedical NER models (Hugging Face), including PHI/PII de-identification. It may offer more deterministic entity handling than a general local LLM. Not yet adopted.

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

**Current:** the offline path and E:-scoped runtime are installed. A synthetic image smoke test confirmed exact transcription through both Tesseract and the loopback `qwen3-vl:4b` reader, and preflight confirmed the pinned local text/vision models. No real-case run has been performed. These local providers refuse automatic downloads during a run and never fall back externally, so they close the external-transmission path for local runs; the risk remains in full for external CLI/API provider selections.

**Problem:** P8's two readers must see the raw, unredacted page image (that's the point -- they have to see what's actually on the page before redaction). The comparator and classifier may also see unredacted extracted text. If any configured provider path is not under a no-data-retention arrangement, every checkpoint-1 run may send PII to that destination.

**Options on the table (see conversation history for the full discussion):**
- Establish a no-retention trust arrangement for the deployment running these reads (procurement/vendor question, not an architecture change).
- Use the implemented local path (`local-ocr` + `local-vlm` + `local-llm`) after `tools/local_runtime.py` passes.

**To resolve:** a deployment/vendor decision, not something to default on silently.

## 4. No dedicated OCR engine -- provider-based P8 may still be LLM vision

**Where:** `tools/ocr_extract.py`, used by `document-pipeline` checkpoint 1.

**Current:** two offline reader technologies now exist. `local-ocr` invokes preinstalled Tesseract, while `local-vlm` sends the page image only to a preloaded loopback Ollama vision model. `local-llm` performs comparison and classification. `ocr_result.json` records the actual provider/model labels.

**Remaining problem:** the Tesseract + vision-model pair is technologically independent, but the chosen local model still needs real Korean insurance-document validation. The synthetic ASCII smoke test is not evidence of Korean table, handwriting, stamp, skew, or low-resolution accuracy. Two `local-ocr` reads remain available only as a weaker fallback and still share Tesseract's systematic errors. P8's hard signal remains page-level agreed/disagreed.

**To resolve:** run the real smoke/quality matrix for the pinned local vision model and document its Korean transcription failure modes before treating the local pair as production-ready.
