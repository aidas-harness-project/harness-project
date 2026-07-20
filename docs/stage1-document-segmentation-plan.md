# Stage 1 Redesign: Case Intake & Document Segmentation

> A Korean-language copy of this document is kept at
> `stage1-document-segmentation-plan.ko.md` for the project owner. This English
> version is authoritative per `CLAUDE.md`'s documentation-language rule; if the
> two drift, this one wins.

## Context

The pipeline currently treats **one source PDF as one logical document
(`DOC_XXX`)**. The real material in `source-cases/` does not work that way:

| Pages | File |
|---|---|
| 110p | 배상-상완골 근위부 골절OP |
| 77p | 배상 한화손보 손해사정서 |
| 59p | 배상 손해사정서 |
| 21-23p | 뇌혈관질환진단비 (4 insurers) |

A single bundle concatenates claim forms, diagnosis certificates, medical
records, receipts, and insurer responses. OCR'ing that as one document means
every downstream stage (classification, field extraction, evidence citation)
operates on wrong document boundaries.

**Goal:** split bundles into logical documents *before* OCR. Stage 1's output is
**document structure, not text**.

Stage 1 explicitly does not do: full OCR, text transcription, final document
typing, redaction, chunking.

---

## Measured findings (the basis for this design)

These are measurements against the real PDFs, not assumptions.

### 1. Cheap text-based boundary detection is impossible

```
110p bundle: pages with embedded text = 0 / 110  (all 9 PDFs identical)
```

Every page is a scan/capture image. Filename- and embedded-text-based boundary
detection is ruled out at the source. **Vision is the only available signal** —
confirmed by data, not assumed.

### 2. Structural signals (image count/size) don't find boundaries either

Scanning per-page embedded-image count and largest-overlay dimensions across all
pages produced a "change" marker on nearly every page — signal-to-noise too poor
to use. The 2→24 image-count swings track capture method, not document
boundaries. No free LLM-less prefilter is available.

*Recorded deliberately: this is the answer to "why didn't you use a cheaper
method?"*

### 3. Vertical strips are self-defeating — grid layout adopted instead

Claude vision caps the **long edge at 1568px and total pixels at ~1.15M**.
Stacking pages vertically makes height the long edge, so fitting height to 1568
**shrinks the width along with it**.

| Layout | Final size | Tokens/page |
|---|---|---|
| Vertical strip, 15p | **222 × 1568 — width collapses** | 31 |
| Vertical strip, 8p | 416 × 1568 | 108.8 |
| 4×4 grid, 16p | 1568 × 731 | 95.8 |

A grid costs ~1533 tokens per sheet **regardless of layout** (the pixel budget
binds), so packing more pages per sheet is purely cheaper per page. **A 4×4 grid
is cheaper per page than an 8p vertical strip while holding twice as many
pages.** The vertical strip is strictly worse at the stated goal of saving
tokens. → **Grid adopted.**

### 4. Grid size — SETTLED at 4×4 by rendering real sheets (build step 3)

**Resolved.** Rendering the real bundle's first sheet at 2×4, 3×4, and 4×4 and
looking at them settled this: **4×4 is legible enough**. Titles (손해 사정서,
진 단 서, 후유장해진단서), letterheads, and even body paragraphs read clearly at a
387×177 cell. The opening sheet also showed a real document boundary directly —
p1-13 carry one adjuster's letterhead, p14 switches to a 진단서 — which is the
signal this stage exists to find.

The earlier concern (list items breaking down at 387px) assumed a render-then-
downscale pipeline. Rendering each cell **directly at its final size** via a zoom
matrix removes that loss, so the concern did not materialize. 3×4 is meaningfully
larger and remains a one-flag change if a harder bundle needs it.

The subsection below is kept as the record of why this was contested.

#### Original analysis — arithmetic and eyeballing disagreed

A flaw in the legibility metric surfaced while planning. Recording it.

Arithmetically a 16pt title in a 4×4 grid renders at 10.5px (independently
reproduced). But rendering a real page and looking at it, **at a 387px cell the
section heading is readable while the list items beneath it are already breaking
down.** At 544px (2 columns) the same page is comfortably legible including the
list items.

Cause: **"16pt title height" did not represent legibility.** Judging document
type needs the form structure and line items under the title, and those are set
in smaller type.

| Grid | p/sheet | Cell | 16pt title | Tokens/p | 110p |
|---|---|---|---|---|---|
| 2×4 | 8 | 555×259 | 14.9px | 192 | 14 sheets |
| **3×4** | 12 | 453×211 | 12.2px | 128 | 10 sheets |
| 4×4 | 16 | 392×183 | 10.5px | 96 | 7 sheets |

**→ Default to 3×4 provisionally, but expose `--grid COLSxROWS` and settle it by
looking at real sheets** (a hard stop in build step 3). Do not hard-code: this
must be a tuning decision, not a rewrite.

### 5. Render DPI doesn't affect final quality — render low, resize ourselves

```
dpi 100/150/200 → identical final output (the long-edge cap binds)
```

High-DPI rendering is pure waste. Rendering **directly at cell size** with
`fitz.Matrix(zoom, zoom)` removes the intermediate full-res PNG entirely.
`ocr_extract.split_to_page_images` hard-codes DPI in both backends
(`ocr_extract.py:188`, `:226`) and that constant is **quality-affecting on the P8
OCR path** — leave it alone. Stage 1 renders independently.

### 6. Content starts at the top of the page — with two exceptions

Measuring overlay bbox vertical placement, body content starts at **0.02-0.20 of
page height**. A top-1/3 crop captures the document opening. Two exceptions:

- Pages with no overlay at all exist → blank-crop handling required.
- **Rotated pages are the norm, not an exception: 51 of 110 (46%).** Scanning
  every page put them in runs at p21, p29-73, p101-105. `page.rotation` is 0
  throughout — content orientation, not a PDF flag — so metadata cannot reveal it.

  **The originally planned detector does not work, and was verified not to.**
  Comparing the ink bounding box's width to its height fails because a full-page
  table fills the page whichever way it was scanned: upright p1 measured 348×419
  and rotated p41 measured 372×531 — both taller than wide, both reported
  upright, zero detections.

  **Replacement (measured):** compare the variance of the row-projection against
  the column-projection. Horizontal text concentrates ink into lines, so density
  oscillates sharply scanning down the page and stays flat scanning across;
  rotating the page swaps the two. Across all 110 pages the rotated ones top out
  at 1.36 and the upright ones bottom out at 1.62, so the threshold sits at 1.5.

  *Correction to an earlier claim in this document:* a first pass reported
  "2.99-27.2 vs 0.43-0.98, ~2× headroom." That came from a 7-page sample and was
  wrong — the real margin is ±0.14, much tighter. An apparent counterexample
  (upright p60 scoring 0.057) turned out to be a mislabel on my part: p60 is a
  rotated 진료비 세부내역서, confirmed by rendering it. The detector was right;
  the label was not.

- **Rotated pages are left rotated. Do not straighten them.** Two measurements
  settled this:

  * *Detecting which way a page is turned does not work.* Rotating each way and
    comparing vertical ink skew within text bands picked the wrong direction on 3
    of 9 known-rotated pages, and genuinely upright controls scored 0.472-0.551 —
    no usable baseline. Guessing one direction leaves half the corpus upside
    down; rendering both directions costs +57% (rotated pages only) to +100% (all
    pages) more sheets, against a design whose whole point is fewer tokens.
  * *Asking the model to read them as-is works.* A real 4×4 sheet spanning p65-80
    (p65-73 rotated, p74-80 upright), with one prompt line saying some pages are
    rotated, came back having read **all 9 rotated cells, none unreadable**, and
    found the p74 boundary at **0.92 confidence** citing the orientation-and-
    layout switch itself as the evidence. It also correctly hedged on p65 —
    confidence 0.45, listed in `needs_full_page` — because the top strip cannot
    show whether p65 continues from p64 on the previous sheet.

  So `orientation_suspect` is informational: it feeds human review and explains a
  low-confidence cell. It gates nothing and transforms nothing.

### 7. Corpus-wide token effect

344 pages at 3×4 → 29 sheets → ~44,000 tokens, versus one call per page (344
calls, ~527,000 tokens). **~92% reduction.** The goal is met.

---

## Branch strategy

Branch `feature/stage1` from `fix_codex` (owner's decision).

Rationale: `fix_codex` already merged `main` (`a3f60cf`) and is a superset, and
the `tools/llm_providers.py` (884 lines, new) and `ocr_extract.py` improvements
this work reuses exist only on `fix_codex`.

**Conflict management:**
- `outputs/` and `data/` are already gitignored (`.gitignore:22,28`), so run
  artifacts never enter a commit.
- The main deliverables are **new files** → minimal conflict surface.
- **The one real conflict point is `tools/dao.py`.** `main` recently added
  `check-forbidden-expressions` in the middle of the parser list (line 987). →
  Append any new subcommand **at the end of the list only**; touch no existing
  lines.

---

## Implementation plan

### A. New schema: `schemas/segmentation_proposal.schema.json`

Written to `outputs/CASE_XXX/segmentation_proposal_{source_doc_id}.json` — one
file per bundle, following the `ocr_result_{doc_id}.json` precedent (a shared
flat filename lets the second write destroy the first). This is shared review
state, not a component output, so like `source_ledger.schema.json` it does not
use the common output envelope.

Key fields:

- `case_id`, `source_document_id`, `source_file_name`, `source_file_path`
  (pattern `^data/raw/`), `source_page_count`, `review_status`
  (`pending|approved|rejected`), `reviewed_by`/`reviewed_at`/`rejection_reason`
- `unassigned_pages[]` — pages covered by no segment, as **explicit data** rather
  than something a reviewer has to compute
- `method`:
  - **`ocr_performed` as `{"const": false}`** — not merely `false`. The schema
    structurally refuses a proposal claiming OCR happened. Cheapest possible
    enforcement of the stage boundary.
  - `method_version`, `mode` (`manual|vision_proposal` — Modes B and C are both
    `vision_proposal`, distinguished by provider fields; do not encode the
    backend as a mode), `provider_name`/`model_name`/`prompt_version`/
    `provider_metadata`, `render_dpi`, `crop_ratio`, `grid_cols`/`grid_rows`,
    `sheet_pixel_budget{long_edge,total_pixels}` (so a token-cost regression is
    diagnosable from the artifact alone), `contact_sheets[]`,
    `full_page_fallback{triggered,pages,saturated,cap}`
- `segments[]`: `segment_index`, `page_start`/`page_end` (both inclusive),
  `provisional_document_type` (enum|null), **`provisional_type_label` (free
  text)** — the closed enum often has no good bucket and forcing one loses
  information, so keep the model's own wording; `confidence`,
  `boundary_evidence` (what makes human review possible in bounded time),
  `review_status` (`pending|approved|edited|rejected`), `needs_full_page`,
  `orientation_suspect`, `assigned_document_id`

JSON Schema cannot express "contiguous, non-overlapping, within range" — enforce
in `validate_segments()` (pure function). Do not fake it with `allOf`.

### B. `document_manifest.schema.json` v0.4 → v0.5

Add (owner: Case Intake/Segmentation): `source_file_name`, `source_page_start`,
`source_page_end`, `segmentation_proposal_path`, `provisional_document_type`.

**`provisional_document_type` needs an explicit annotation:** *"A pre-OCR visual
guess. Not authoritative. checkpoint 1 owns `document_type` and must run its own
classification — unlike `pre_flagged_type`, which is human-asserted and IS
trusted."* Without that written down, someone will wire it into the
classification short-circuit.

One narrow conditional: if `source_page_start` is present, `source_file_name` and
`source_page_end` are required (a page range never exists half-specified).
`page_end >= page_start` cannot be expressed in JSON Schema — that's the tool's
job.

`required` stays unchanged so existing manifests remain valid. Run
`validate_output.py` over existing manifests after the bump.

### C. `tools/segment_case.py`

**CLI (subcommands):**

```
sheets  CASE_ID DOC_ID [--crop-ratio 0.33] [--grid 3x4] [--dpi 110]
propose CASE_ID DOC_ID [--mode manual|vision] [provider args] [--resume]
show    CASE_ID DOC_ID
approve CASE_ID DOC_ID --reviewer NAME [--segment N] [--edit N=start-end]
split   CASE_ID DOC_ID --held-by NAME --run-id RUN_ID
```

`sheets` is Mode A and the PoC default: render sheets, write a proposal skeleton,
print paths, stop. **Zero LLM calls.**

**Pure functions (testable):** `compute_sheet_geometry(...)` (encodes both caps,
tested at boundaries), `plan_sheets(page_count, per_sheet)`,
`parse_segmentation_response(raw, sheet_pages)`,
`validate_segments(segments, page_count)`,
`merge_sheet_proposals(per_sheet, page_count)`, `build_manifest_entries(...)`.

#### Parser failure mode — the two existing precedents disagree

- `redact_document._parse_redaction` **raises** (`ProviderExecutionError`).
- `intake_case._parse_content_scan_verdict` **returns a dict and fails safe**.

Segmentation follows the **second**. One sheet failing to parse must not kill the
sheets around it — their expensive vision calls are already paid for — and under
this plan's "never invent a boundary" rule, a failure is properly expressed as
"leave that sheet's pages in `unassigned_pages` and warn."

`parse_segmentation_response` therefore returns `{"ok": bool, "boundaries": [...],
"continuations": [...], "needs_full_page": [...], "warning": str|None}` and
**never raises**. The `raw_decode` scan loop itself is still borrowed verbatim
from `_parse_redaction` — the need for tolerance to leading/trailing prose is
identical.

#### `merge_sheet_proposals` algorithm

The key insight: **segment boundaries come from the union of `boundaries` alone,
and sheet edges carry no meaning whatsoever.** `continuations` is a coverage
check, not a basis for splitting. Seen that way, the "document spanning a sheet
break" problem disappears rather than needing special handling.

```
sheet 1: p1-12   boundaries=[1]   continuations=[2..12]
sheet 2: p13-24  boundaries=[15]  continuations=[13,14,16..24]
correct: SEG(1-14), SEG(15-24)
wrong:   SEG(1-12), SEG(13-14), SEG(15-24)   <- cut at the sheet edge
```

The two fields are **not symmetric**: only `boundaries` creates segments, while
`continuations` records that the model actually looked at a page. That asymmetry
is what makes a page missing from *both* lists a meaningful signal that the model
skipped it.

Edge cases — **all four confirmed with the owner**:

| Case | Handling | Why |
|---|---|---|
| **A.** p1 not reported as a boundary | **Treat p1 as a boundary** + warn | Page 1 of a bundle is by definition the first page of something. The model not saying so does not change that. |
| **B.** A page appears in neither list | **Leave it in `unassigned_pages`** | Silently absorbing a page the model never mentioned means a human approves without knowing. `split` halts on unassigned pages, so a human necessarily sees it. |
| **C.** A `needs_full_page` page | **Re-check via the full-page fallback** (§F) and use that verdict. If still unjudgeable, flag the segment `needs_full_page: true` for a human | Decide on real evidence rather than a guess. There is no third round. |
| **D.** A page in both lists (contradiction) | **Boundary wins**, contradiction recorded in `warnings` | The error costs are asymmetric. Over-splitting is fixed by a human merging two segments; over-merging surfaces only after OCR, classification, and field extraction have all run on wrong boundaries, by which point downstream artifacts are contaminated. |

If some sheets returned `ok: false`, **only those sheets' page ranges** go to
`unassigned_pages`; boundaries from the healthy sheets are processed normally.

**Orchestration:** `segment_case(..., provider=None, progress=None, resume=True)
-> dict`, following `run_checkpoint1.run_checkpoint1()`'s contract — **providers
injectable, returns a status dict rather than raising, `sys.exit` only in
`main()`**. (`intake_case.scan_for_answer_key_content` `sys.exit`s from library
code — borrow its structure, not that.)

**Scratch:** new `_segmentation_scratch/` (needs a `.gitignore` entry). **Do not
use `ocr_extract.scratch_dir`** — it rmtrees in `finally`, and sheets must
outlive the process for human review.

### D. Contact sheet compositor (Pillow 11.1.0; Malgun Gothic confirmed present)

- Render at `zoom = cell_w / 595.0` **directly at cell size** → crop → no resize
- **4px red separators** (3px reads as antialiasing noise after downscale). Pure
  red `(255,0,0)` — no scanned document contains saturated red, so it's maximally
  separable. **Box every cell fully, including sheet edges**; a cell bounded on
  only two sides is exactly where "is this the same document continuing?"
  ambiguity comes from.
- **Page numbers:** red chip with white text at each cell's top-left (highest
  available contrast against arbitrary scan content). Use the **absolute source
  page number** (`p41`, not `cell 5`) — the whole point is that the model never
  counts positions. Load fonts defensively (`arial` → `DejaVuSans` →
  `load_default`).
- **Partial sheets:** keep the canvas full size; leave unused cells white with no
  box and no label. Shrinking the sheet breaks the model's spatial expectation
  across sheets; repeating pages to fill guarantees phantom boundaries.
- **Blank/sideways pages:** mark `(blank)` or `orientation_suspect` → full-page
  path.

### E. Vision prompt

**Gotchas (documented real failure modes):**

- **Route every vision call through `provider.transcribe_image()`.** It applies
  the working imperative framing (`f"Read the image file at {path} and then:
  {prompt}"`). The label form failed **9/9** in controlled repeats. Never call
  `_run` directly. Side benefit: **Mode C works with no provider-class changes**,
  since `LocalVlmProvider` refuses everything except `transcribe_image`
  (`llm_providers.py:620-635`).
- **No self-legitimizing framing.** No "this is a sanctioned step," no "do not
  refuse," no role-play preamble — prior versions were read as prompt injection
  and refused. A genuine layout-analysis request does not argue for itself.

Response shape:

```json
{"boundaries": [{"page": 13, "type_label": "후유장해진단서",
                 "type_guess": "diagnosis_certificate",
                 "confidence": 0.7, "evidence": "..."}],
 "continuations": [14, 15, 16],
 "needs_full_page": [4, 17]}
```

**Parse-failure policy:** unlike `_parse_content_scan_verdict`, which fails safe
toward `flagged=True`, there is no safe default segmentation. On a parse failure,
emit **zero segments**, leave those pages in `unassigned_pages`, keep
`review_status: pending`, and record a warning. "No proposal, human does it" is
the fail-safe here. **Never invent a boundary from a failed parse.**

### F. Fallback (second call) — watch for saturation

100%-scanned input plus small cells means `needs_full_page` may fire far more
than expected. If 40 of 110 pages get flagged, the fallback becomes the main path
and costs more than rendering full pages would have.

1. Union and dedupe `needs_full_page` across sheets.
2. **Over `--max-full-page-fallback` (default 25% of pages): record
   `saturated: true`, skip the fallback entirely, route to human review.**
   Saturation means the crop ratio or grid is wrong for this bundle — a **tuning
   signal**, not something to paper over with a large spend.
3. Under the cap: render only those pages full-page, 1-2 per sheet, and **batch**
   them (not N individual calls).
4. No third round — a page still unjudgeable at full page is a human decision.

### G. Split execution and manifest merge

`split` refuses unless case-level `review_status == approved` **and** every
segment is `approved`/`edited` **and** `validate_segments` is clean — mirroring
`intake_case.py:405-413`, where one unresolved entry blocks everything.

**Add `dao.replace_manifest_documents(case_id, documents, held_by, run_id) ->
(ok, message)`.** `write_manifest` overwrites wholesale and
`patch_manifest_document` patches one entry; neither can add N entries while
retiring one. Follow `patch_manifest_document`'s tuple-return convention and
**read under the lock** — that is the entire reason that function exists
(known-gaps item 7).

**Supersede the bundle entry; do not delete it.** Deleting orphans
`_intake_record.json`'s crosswalk and any `_source_ledger.json` reference, and
erases the audit trail from immutable source to logical document.

⚠️ **Schema collision:** `downstream_disposition: expert_review_only` triggers a
conditional (`document_manifest.schema.json:134-152`) requiring
`non_text_verification` — semantics meaning "a human confirmed this is
photographic evidence." That is **wrong** for a superseded bundle. → **Recommend
adding a `superseded_bundle` enum value** (smaller and more honest; any consumer
switching on that field gets a value meaning what actually happened). Requires an
enum addition plus a grep for consumers.

New entries: `DOC_{n:03d}` from max+1 (never reuse the bundle's id);
`insert_pdf(from_page, to_page)` (the `intake_case.py:454-457` pattern);
`file_path` with forward slashes even on Windows; `file_size_bytes` from `stat()`
**after** `save()`; `ocr_status: "pending"`; `pages: null` (owned by
document-pipeline).

**Guardrail note (required in the docstring and `pipeline.md`):** the split
writes to `data/raw/`. What is immutable is `source-cases/`; `data/raw/` is
intake's *output*, and segmentation is part of intake, so it is a legitimate
writer. Say so explicitly — this is exactly the kind of thing a future reader
will flag as a violation. Segmentation only creates new files there, never
modifies existing ones.

**Idempotency:** if entries matching `source_file_name` + `source_page_start`
exist, report `already_split` and exit 0.

### H. Halt / resume discipline

Mirror `ocr_extract._resume_cache_dir` (`ocr_extract.py:290-315`) — that design
came out of a real 75-page loss.

`_segmentation_scratch/_resume/{case_id}_{doc_id}/`, **stable, not pid-tagged**,
one JSON per sheet (parsed result + raw text + provider metadata). Check cache
before each call and report `(cached)` via `progress()`. Save with the
tmp-write-then-`replace()` atomic pattern so an interrupt mid-write never leaves
a half-sheet resume would trust. Corrupt JSON → `None` → re-call.

**The sheet images are themselves a cache.** Rendering 110 pages costs real time.
Store a geometry hash (`crop_ratio`, grid, dpi, page list) in `_sheets_meta.json`
beside the PNGs and invalidate on mismatch — otherwise changing `--crop-ratio`
silently reuses stale sheets, a nasty and nearly invisible bug.

On `split` failure, follow `run_checkpoint1`'s four-part discipline
(`run_checkpoint1.py:430-450`): persist forensics → reset owned fields → mark
run-state `failed` → return a status dict, exiting only in `main()`. **Build the
full new `documents` list in memory and write once**, so the manifest is
atomic-or-nothing even if PDF writing partially succeeded. An orphaned
`DOC_XXX.pdf` is recoverable; a manifest entry pointing at a nonexistent file is
not.

### I. Test plan

New `tests/test_segment_case.py`. All filesystem tests on `tmp_path`; providers
via `FixtureProvider`.

- **Geometry:** every combination of 3×4/4×4/2×4 × crop 0.25/0.33/0.5 asserts both
  caps hold; an impossible request raises.
- **`plan_sheets`:** (110,12) → 10 sheets with last of 2; (12,12) → exactly 1
  (off-by-one guard); (1,12) → 1.
- **Parsing:** clean JSON; trailing prose; leading prose; unparseable → zero
  segments + warning + pending and specifically **does not invent a boundary**; a
  page number not on the sheet → parse failure; `needs_full_page` deduped.
- **Segment validation:** overlaps rejected; gaps → `unassigned_pages`; reversed
  ranges rejected; out-of-range rejected.
- **`merge_sheet_proposals`:** a document spanning a sheet break stays one segment
  (p1-14, not split at 12) — **the highest-value test in the file**.
- **Fallback:** under cap → call happens and overrides; over cap → `saturated`
  with **zero provider calls** (assert the call count) and case stays pending.
- **Schema:** a proposal with `ocr_performed: true` **fails** validation (proves
  the `const` guard).
- **Split:** a synthetic 10p PDF (built in-test with `fitz`), 3 segments → 3
  `DOC_XXX.pdf` with correct page counts and provenance; bundle entry retained and
  marked superseded; re-run is idempotent.
- **Resume:** cache hit → provider not called (assert count); corrupt cache →
  re-called without crashing; geometry mismatch invalidates cached PNGs.

**Real end-to-end** (costs real provider calls):

```
1) Run `sheets`; open the PNGs. Are the red lines and numbers legible? Can you
   tell document types apart?  → settle the grid size here (§4).
2) Hand-record the true boundaries → ground-truth baseline.
3) Run `propose --mode vision`; score precision/recall against the baseline;
   count needs_full_page; check that p41-class sideways pages were flagged.
4) `approve` + `split`, then confirm `run_checkpoint1` runs cleanly on one
   resulting DOC_XXX.
```

**Step 2's hand baseline gets built regardless** — without it there is no way to
tell whether a crop-ratio or grid change helped or hurt.

### J. Integration points

- **`.claude/agents/document-pipeline.md`** — document the new provenance fields
  under checkpoint 1 and state that **`provisional_document_type` must not be
  trusted** (unlike `pre_flagged_type` it is not human-asserted, so checkpoint 1
  still runs its own classification); superseded bundle entries are skipped.
- **`pipeline.md:30`** — retitle Stage 1 to "Case Intake & Document
  Segmentation", describe the contact-sheet → proposal → approval → split flow,
  and state **"No OCR at this stage — the output is document structure, not
  text."**
- **`CLAUDE.md`** — add `segment_case.py` to `## Tools` plus a changelog entry.
- **`.claude/skills/loss-adjustment-pipeline/SKILL.md`** — the orchestrator needs
  to know Stage 1 now has a human gate that blocks Stage 2.
- **`tools/sync_agents.py`** — run after editing anything under `.claude/`; never
  hand-edit generated copies.
- **`.gitignore`** — add `_segmentation_scratch/`.
- **`known-gaps.md`** — (1) `intake_case.scan_for_answer_key_content` still uses
  the broken label form and was never migrated; (2) the sideways-scanned-page
  class.
- **`open-decisions.md`** — the grid choice (§4) and the `downstream_disposition`
  collision (§G) are genuine open decisions, not settled details.

---

## Build order

1. ~~`.gitignore` + new schema + manifest v0.5; check existing manifests for
   regression.~~ **Done** (`a5f35ef`): the `ocr_performed` const guard is verified
   to reject a proposal claiming OCR ran, 7 accept/reject cases behave as
   intended, and all 6 existing manifests still validate.
2. **← current.** Pure functions + tests. No I/O yet.
   **Scope (owner-confirmed): pure functions and tests only; rendering and
   compositing move to step 3.**

   Six I/O-free functions in `tools/segment_case.py`: `compute_sheet_geometry`,
   `plan_sheets`, `parse_segmentation_response`, `validate_segments`,
   `merge_sheet_proposals`, `build_manifest_entries`. New
   `tests/test_segment_case.py` covering the geometry / `plan_sheets` / parsing /
   segment-validation / merge items from §I; the rest arrive with the code they
   test.

   **Done when:** the new tests pass and the existing 287 still pass (excluding
   `test_dao_forbidden_expr`'s one pre-existing failure inherited from the main
   merge).
3. ~~Renderer + compositor → **stop and look at real sheets.**~~ **Done.** Grid
   settled at 4×4 (§4); the sideways detector was found broken and replaced with
   a working projection-variance test (§6). `sheets` CLI still to wire up.
4. Hand-record the ground-truth boundary baseline.
5. Provider path (`propose`, Mode B) + resume cache + fallback + FixtureProvider tests.
6. `approve` + `split` + `dao.replace_manifest_documents` + tests.
7. Real end-to-end run; score against step 4.
8. Docs → `sync_agents.py` → `pytest`.

---

## Open decisions

**Settled:**

- ~~`downstream_disposition`~~ → resolved as a new `superseded_bundle` enum value,
  implemented in step 1. `expert_review_only` was not reused because its
  conditional requires `non_text_verification` — an assertion that a human
  confirmed photographic evidence, which is false for a superseded bundle and
  would put a fabricated human verification into the record.
- ~~The four `merge_sheet_proposals` edge cases~~ → settled in the table above.

**Still open:**

1. **Fallback saturation** — if a lot of pages get flagged, halt for re-tuning or
   spend the calls? The picture improved: rotated pages were the feared driver,
   and the live test showed the model reading them rather than punting, flagging
   only 4 of 16 pages for full-page review (25%, right at the cap). Two of those
   four were sheet-edge pages where a full-page look genuinely cannot help — the
   ambiguity is about the PREVIOUS sheet, not resolution. Worth deciding whether
   sheet-edge hedges should even count toward saturation.
2. **Is Mode A viable as the default** — 344 pages is 22 sheets at 4×4. Having now
   seen real sheets, a human can scan one in well under a minute, so manual review
   looks more practical than assumed. Confirm on a full case.

**Settled at step 3:** grid size (4×4, §4); rotated-page handling (leave them
rotated, tell the model — §6, verified against a live call).
