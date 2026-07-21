# Stage 1: Case Intake & Document Segmentation

## Status

Implemented on `feature/stage1` as of 2026-07-21.

Stage 1 is no longer plain file intake. It is the pre-OCR stage that isolates
ground truth, turns each approved raw bundle into logical documents, and blocks
Stage 2 until a human has approved every proposed page range.

This document describes the implementation that exists now. It is not a build
plan or a chronological work log. Historical measurements are retained only
where they explain a current default.

## 1. Scope and non-goals

Stage 1 owns:

1. D2-gated raw/ground-truth intake.
2. Low-resolution page previews and contact sheets for raw PDF bundles.
3. Pre-OCR visual boundary proposals.
4. Targeted full-page fallback for pages the crop pass cannot judge.
5. Optional full-page refinement of long, probably over-merged segments.
6. Human review of every proposed page range.
7. Splitting an approved bundle into logical `DOC_XXX.pdf` files.
8. Provenance-preserving manifest replacement through the DAO.

Stage 1 does **not** perform:

- OCR or text transcription;
- final document classification;
- PII redaction;
- chunking or evidence extraction;
- autonomous approval of a model proposal.

`segmentation_proposal.schema.json` pins `method.ocr_performed` to `false` with
a JSON Schema `const`. Stage 1's output is document structure, not text.

## 2. How Stage 1 is connected to the pipeline

The harness does not have a standalone `run_pipeline.py` executable. Pipeline
execution is agentic: the `loss-adjustment-pipeline` skill is the orchestrator.
When a user asks Claude or Codex to process a case, that orchestrator must run
Stage 1 in the order below and must not dispatch `document-pipeline` until the
segmentation gate is clear.

Stage 1 itself has no dedicated agent identity. It is owned by the orchestrator
plus deterministic tools:

- `tools/intake_case.py`
- `tools/segment_case.py`
- `tools/dao.py`

`document-pipeline` begins at Stage 2. It consumes the logical documents Stage 1
created, skips entries marked `superseded_bundle`, and independently classifies
each logical document after OCR.

There is no standalone pipeline runner, but Stage 2 can no longer silently
bypass Stage 1. `run_checkpoint1.py` performs a case-wide DAO preflight before
provider construction or PDF access and returns `blocked_segmentation` with zero
OCR/output work when the gate is not clear.

## 3. Required execution sequence

For every new case:

1. Run intake and complete the D2 `_source_ledger.json` human review.
2. Every intaken PDF starts `segmentation_status: pending_review`. A genuine
   human records `required` or `not_required` through `dao.py
   set-segmentation-status`; an agent does not make this decision.
3. For every PDF marked `required`, render contact sheets with
   `segment_case.py sheets` if a human wants to
   inspect them before using a model.
4. Run `segment_case.py propose`; the crop pass and targeted full-page fallback
   run automatically.
5. Optionally add `--refine` to re-examine long segments full-page.
6. Halt and wait for a genuine human to inspect the proposal and sheets.
7. Apply human approvals or range corrections.
8. Run `segment_case.py split`.
9. Confirm the bundle is `superseded_bundle`, its status and children are
   `completed`, and its logical children exist in the manifest.
10. Run `dao.py check-segmentation-ready CASE_ID`.
11. Only when that returns clear, dispatch `document-pipeline`.

The current implementation requires a human to identify which raw PDFs are
bundles. Automatic bundle-selection rules based on page count or filename are
not implemented. This human decision is now explicit and auditable rather than
an unrecorded orchestrator assumption.

## 4. Contact-sheet design

### 4.1 Why vision is required

The measured 110-page sample contained zero pages with embedded text. Structural
PDF signals such as image count and overlay size changed on too many pages to
serve as useful boundaries. Pre-OCR vision is therefore the available signal
for this corpus.

### 4.2 Grid and crop defaults

Current defaults:

- grid: 4 columns × 4 rows;
- capacity: 16 pages per sheet;
- crop: top 0.33 of each page;
- saturated-red 4px cell borders;
- absolute page labels such as `p41`;
- fixed-size white cells for unused positions on the final sheet;
- vision caps: 1568px long edge and approximately 1.15M total pixels.

Vertical strips were rejected because the long-edge cap collapses their width.
A 15-page strip was estimated at about 222×1568px, while a 4×4 sheet retains
about 1568×731px and costs fewer tokens per page.

Cells are rendered directly at their final width with a PyMuPDF zoom matrix.
The tool does not create a high-resolution intermediate image and then shrink
it.

### 4.3 Rotation handling

Every sheet can be rendered in three variants:

- `as_scanned`
- `cw`
- `ccw`

The companion rotations exist for human review. The proposal model receives
only `as_scanned`; its prompt tells it to read quarter-turned cells in place.
An earlier orientation detector was removed because it did not gate any action
and produced unreliable direction guesses.

All sheet artifacts live under gitignored `_segmentation_scratch/` and must
survive process exit so a human can inspect the exact images used.

## 5. Boundary proposal

### 5.1 Contact-sheet pass

The proposal prompt asks for:

- `boundaries`: pages that start a document, with type label, confidence, and
  visible evidence;
- `continuations`: pages that continue the previous document;
- `needs_full_page`: pages whose top crop is insufficient.

The provider call always goes through `provider.transcribe_image()`. Supported
CLI selections are `claude-cli`, `codex-cli`, `openai-api`, and `fixture`;
`anthropic-api` is selectable but its execution adapter is not implemented.

The parser fails safe. Invalid JSON, out-of-sheet page numbers, or malformed
fields produce no invented boundaries. Only the failed sheet's pages become
unassigned; valid neighboring sheets remain usable.

### 5.2 Merge semantics

Only `boundaries` create segments. Sheet edges never create segments, and
`continuations` are coverage evidence rather than split instructions.

Rules:

1. If page 1 was examined but omitted as a boundary, add it and warn.
2. If a page is both a boundary and a continuation, boundary wins and the
   contradiction is recorded.
3. A page no sheet named stays unassigned unless it lies strictly between two
   known boundaries.
4. An unnamed page strictly enclosed by known boundaries is absorbed into the
   earlier document and reported in warnings. This handles enumeration slips at
   sheet edges.
5. An unnamed page after the last known boundary stays unassigned.
6. Any unassigned page blocks `split`.

## 6. Full-page decision policy

The owner-set policy as of 2026-07-21 is deliberately split-biased:

- a page with its own document/form title starts a new logical document;
- a repeated title also starts a new document;
- a page with no title of its own, even after full-page inspection, continues
  the preceding document;
- an unreadable or failed full-page verdict is not converted into either fact;
  it remains flagged for human review.

This replaces the earlier unimplemented idea of merging record-like forms but
splitting receipt-like forms based on amount or document type.

### 6.1 Targeted crop-ambiguity fallback

This pass is automatic during `propose`.

1. Collect and deduplicate `needs_full_page` pages from all contact sheets.
2. Compute the cap as `int(page_count * 0.25)`.
3. If the page count exceeds the cap, mark fallback `saturated`, execute zero
   fallback calls, and leave every page flagged. Saturation means the grid or
   crop is unsuitable and should be retuned rather than hidden by spending.
4. Otherwise render each named page full-page and ask the single-page boundary
   prompt.
5. A successful title verdict replaces the crop uncertainty with a boundary.
6. A successful no-title verdict replaces it with a continuation.
7. A parse/provider failure leaves `needs_full_page` intact.

### 6.2 Optional long-segment refinement

The crop model can confidently over-merge repeating forms without setting
`needs_full_page`. `propose --refine` addresses that separate failure mode.

- segments of length at least 4 are selected by default;
- every interior page is inspected full-page;
- the pass may add boundaries but never remove an existing boundary;
- failed verdicts remain continuations, so no split is invented;
- it is off by default because it can add many provider calls.

Targeted fallback and refinement share the same per-page verdict cache and
`segment_full_page_v0.2` prompt version. A page inspected by the first path is
not paid for again by the second.

## 7. Resume and cache behavior

The tool uses stable, non-PID scratch paths.

- one cached result per contact sheet;
- geometry fingerprint covers grid, crop, page count, and pixel geometry;
- geometry changes invalidate sheet-response reuse;
- full-page verdicts are cached per page;
- full-page prompt-version changes invalidate the verdict cache;
- cache files use temporary-write then atomic replace;
- provider/parse failures are not cached.

This preserves paid model work across interruption without trusting stale
images or stale prompts.

## 8. Human review gate

There are two distinct human gates.

### 8.1 Bundle decision gate

Each new PDF starts `pending_review`. A human records one of:

- `required`: this PDF is a bundle and must be segmented;
- `not_required`: this PDF is already one logical document.

The DAO records reviewer, timestamp, and optional note. Only the split tool can
write `completed`; non-PDF inputs are `not_applicable`. A legacy PDF missing the
field fails closed as `pending_review`.

Before checkpoint 1 performs any provider or PDF work, it checks the entire
case. Any pending decision or required-but-unsplit bundle blocks Stage 2, as
does attempting to process the retained superseded bundle instead of a child.

### 8.2 Boundary approval gate

`segmentation_proposal_{DOC_ID}.json` begins with case and segment review states
set to `pending`.

`split` refuses unless all conditions hold:

- case-level `review_status == approved`;
- every segment is `approved` or `edited`;
- no unassigned pages remain;
- segment ranges are ordered, in range, non-overlapping, and structurally
  valid.

The model never approves its own proposal. `approve` supports bulk approval,
single-segment approval, and range edits. It does not currently provide rich
add/delete/merge operations for segments; complex corrections require a
reviewed replacement proposal written through the DAO.

## 9. Split and provenance

After approval, `split`:

1. writes one new PDF per approved segment under `data/raw/CASE_XXX/`;
2. allocates document IDs after the highest existing `DOC_NNN`;
3. records `source_file_name`, inclusive source page range, proposal path, and
   provisional labels on each child;
4. marks the original bundle `downstream_disposition: superseded_bundle` and
   `ocr_status: not_applicable`;
5. calls `dao.replace_manifest_documents()` once under an exclusive lock;
6. updates the `document_segmentation` run-state stage;
7. returns `already_split` on an idempotent rerun.

The split transaction also changes the retained bundle and every logical child
to `segmentation_status: completed`.

The bundle entry is retained rather than deleted so the immutable-source to
logical-document audit chain survives. `data/raw/` is intake's output layer;
Stage 1 creates new files there but never modifies the original bundle or
anything under `source-cases/`.

If PDF creation succeeds but the manifest transaction fails, the files are
reported as orphans and no downstream stage trusts them because the manifest
does not name them.

## 10. CLI reference

```text
python tools/segment_case.py sheets  CASE_ID DOC_ID [--grid 4x4] [--crop-ratio 0.33]

python tools/segment_case.py propose CASE_ID DOC_ID \
  --held-by NAME --run-id RUN_ID \
  [--grid 4x4] [--crop-ratio 0.33] \
  [--provider {claude-cli,codex-cli,anthropic-api,openai-api,fixture}] \
  [--model MODEL] [--no-resume] [--refine] [--refine-threshold 4]

python tools/segment_case.py show    CASE_ID DOC_ID

python tools/segment_case.py approve CASE_ID DOC_ID \
  --reviewer NAME --run-id RUN_ID \
  [--segment N] [--edit N=start-end] [--held-by NAME]

python tools/segment_case.py split   CASE_ID DOC_ID \
  --held-by NAME --run-id RUN_ID

python tools/dao.py set-segmentation-status CASE_ID DOC_ID \
  {required|not_required} --reviewer NAME [--note TEXT] \
  --held-by NAME --run-id RUN_ID

python tools/dao.py check-segmentation-ready CASE_ID [--doc-id DOC_ID]
```

`sheets` performs no model call. `propose` writes through the DAO. `show` reads
through the DAO. `approve` is a human action recorded through the DAO. `split`
creates logical PDFs and atomically updates the manifest through the DAO.

## 11. Measured results retained as defaults

### CASE_025, 110 pages

Crop-only 4×4 result:

- 7 contact-sheet calls;
- precision 0.95;
- recall 0.81;
- F1 0.88;
- zero parse failures and zero unassigned pages.

The same bundle at 3×4 produced precision 0.90, recall 0.50, and F1 0.64 with
10 calls. Page density, not larger cells, was the useful signal for repeating
forms. This settled 4×4 as the default.

Long-segment refinement at threshold 4 raised recall to 0.96 and F1 to 0.94 at
the cost of about 45 additional full-page calls. This settled the optional
refinement threshold but did not make refinement the default.

The exercised chain was sheets → propose → score → approve → split → real
Stage 2 checkpoint on one resulting logical document.

### CASE_026, 59 pages

A sheet-edge enumeration omission left pages 33–41 unassigned even though the
model described them as continuing the document that began on page 28. The
enclosed-page absorption rule reduced unassigned pages from 9 to 0 while
preserving the rule that a gap after the final boundary remains unassigned.

## 12. Validation coverage

`tests/test_segment_case.py` covers:

- geometry and pixel caps;
- page batching and partial sheets;
- parser failure and contradiction handling;
- sheet-edge merge behavior;
- contact-sheet rendering and rotations;
- resume-cache invalidation;
- targeted full-page fallback, saturation, and unresolved verdicts;
- full-page title/no-title policy;
- long-segment refinement and shared verdict caching;
- human approval and split readiness;
- case-wide Stage-2 preflight, legacy fail-closed behavior, and attributable
  bundle decisions;
- PDF split provenance and idempotency.

Related tests cover segmentation scoring, schema validation, and atomic manifest
replacement. No test reads or writes the real `outputs/` or `data/` trees.

## 13. Current limitations and owner decisions still needed

The implementation is functional, but these product/operations decisions are
not settled by code:

1. **Pipeline entrypoint:** the Stage-2 preflight now prevents segmentation
   bypass; decide later whether a standalone deterministic runner is still
   useful for full-sequence automation.
2. **Bundle selection:** require operator selection, or implement automatic
   candidate rules based on page count, filename, and/or a cheap visual check.
3. **Human correction interface:** keep DAO-level proposal replacement for
   complex edits, extend the CLI with add/delete/merge/split operations, or add
   frontend support.
4. **Fallback cap for small bundles:** keep exact `int(page_count * 0.25)`, which
   yields a zero-page cap for one-to-three-page files, or guarantee a minimum of
   one fallback page.
5. **Refinement default:** keep `--refine` opt-in, enable it for every bundle, or
   trigger it from a cheaper heuristic.
6. **Pre-redaction provider policy:** approve an external provider under an
   appropriate no-retention arrangement, or defer real use until a validated
   local vision path exists.
7. **Dependency packaging:** add a root Python dependency manifest/environment
   so Claude and Codex run the same tested versions of PyMuPDF, Pillow,
   jsonschema, and pytest.

## 14. Implementation map

- `tools/segment_case.py` — rendering, proposal, fallback, refine, approval,
  and split.
- `schemas/segmentation_proposal.schema.json` — proposal and review contract.
- `schemas/document_manifest.schema.json` — provenance and superseded-bundle
  fields.
- `tools/dao.py` — atomic manifest replacement and contract access.
- `tools/score_segmentation.py` — boundary precision/recall/F1 scoring.
- `tests/test_segment_case.py` — Stage 1 behavior.
- `tests/test_dao_replace_manifest.py` — manifest transaction behavior.
- `.claude/skills/loss-adjustment-pipeline/SKILL.md` — actual agentic
  orchestration requirement.
- `.claude/agents/document-pipeline.md` and generated Codex counterpart — Stage
  2 handoff behavior.
