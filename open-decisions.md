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
- **Per-coverage split slots in 변형 A -- DEFERRED 2026-08-04, current behaviour kept deliberately.** CASE_907's insurer denied 배상책임 while accepting 구내치료비 ₩2,000,000, and 변형 A has no slot for writing a split outcome coverage-by-coverage, so draft v2 expressed it in prose instead. This was one of four places the `accepted_coverages` axis failed to propagate (findings §2); the other three are closed -- `denial_reason_result` and `screening_report` gained the axis, and the verification gap became `known-gaps.md` item 39's fix -- but this one is a **domain question, not a structural defect**, so it is recorded rather than guessed at:

  The prose workaround is working and was verified, not assumed: critic v2 read draft v2 and found no dependency misreading, and the split is recorded structurally upstream anyway (`decided_coverage` + `accepted_coverages` in `denial_reason_result.json`, with acceptance-side `policy_matches` now held to the same verification as denials). So nothing downstream is reading a wrong value, and no output states anything false -- unlike the other three instances of this shape.

  The reason not to just add the slot: `templates/` is not a design of ours. Its section structure was extracted from **4 real completed 손해사정서** (CASE_003/004/005/006), and none of them splits its assessment section per coverage -- a real adjuster handling a split outcome writes it as narrative. Adding a slot would make the harness emit a document shape that working adjusters do not produce, trading fidelity to real practice for structural tidiness. Since the deliverable is submitted to Korean-speaking professionals, that is their call, not ours.

  **To resolve:** ask a 손해사정사 how a split denial/acceptance is actually laid out in practice (one narrative assessment section, or a per-담보 breakdown). If per-담보 is real practice, add the slot to 변형 A and a matching `heading_patterns` entry in `templates/registry.json` in the same commit (D4). If not, this stays closed and the prose form is correct. Until then, do not add the slot to make the template "complete" -- the absence is evidence about real reports, not an oversight.

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

## 8. Adjuster-supplied case type at intake (phase 1 BUILT 2026-08-04; phase 2 open)

**Where:** `tools/intake_case.py` (Stage 1), `claim-analysis` checkpoint 3
(`case_type_result.json`), `evaluation` (`case_type_accuracy`).

**Current:** `case_type` is *inferred* by claim-analysis checkpoint 3 from
extracted claim fields plus coverage, and `evaluation` scores that inference
against ground truth via `case_type_accuracy`. The adjuster has no way to state
the case type at intake even though, in real practice, they know it at the
moment they accept the engagement.

**The argument for supplying it (user, 2026-08-04):** `case_type` is not the
same kind of field as 진단명 or KCD code. Those are written on a document and
are genuinely extractable. Case type is *not written anywhere* -- no document
says "this is a 후유장해 case". The harness reverse-infers it from document
character, while the adjuster already holds it as a reliable practice fact.
Inferring what is already known is a self-imposed handicap, and the cost is on
record: CASE_022's `case_type` scored `correct=false` **not through a reasoning
error** but because the ground truth was a 배상책임 quantum whose claim
documents were never in the pack (`known-gaps.md` item 14 addendum). An adjuster
input would have made that value correct by construction, and -- more valuable
-- would have exposed the missing documents at intake.

**Two objections raised and withdrawn:**

- *"It bypasses P1"* -- withdrawn. P1 exists to stop a **model** asserting
  ungrounded facts. A reviewed human input is not the failure mode P1 guards;
  it is a verified non-model source. What is needed is not prohibition but
  **provenance recorded distinctly**, so an adjuster-supplied value is never
  silently indistinguishable from an inferred one.
- *"It invalidates `case_type_accuracy`"* -- withdrawn. That metric is only
  meaningful while case type is the harness's task. If it becomes an input, the
  metric is not corrupted, it is **out of scope**, and the schema can already
  say so honestly: `evaluation_result.schema.json`'s `applicable: false` +
  `na_reason`. What the PoC must measure is the layer beneath -- clause
  matching, R-code classification, draft quality -- not type-guessing.

**Objections that survive:**

- `template_id`: 실손/기타 have no registry contract
  (`templates/registry.json` has exactly three keys: `배상책임_후유장해형`,
  `진단수술비형`, `screening_report`). An adjuster entering "실손" makes
  `document_assembly.py --template` fail at render. This is a pre-existing gap
  surfacing *earlier and louder*, which is an improvement, but it must be
  handled before the input is accepted for those types.
- Adjuster typo / mis-entry: real but cheap to cover with a cross-check, and
  not an argument that inference is more reliable.

**Design, and what is BUILT as of 2026-08-04 (phase 1):**

1. **BUILT.** Case type is adjuster input on two axes (see #8a); checkpoint 3
   no longer infers when input is present. `intake_case.py` takes
   `--coverage-basis/--loss-type/--non-claim-case/--supplied-by/--case-type-note`,
   and `document_manifest` carries an optional `adjuster_case_type`.
2. **BUILT.** `case_type_source: "adjuster_input" | "inferred"` added to
   `case_type_result.schema.json` (v0.2) -- **additive**, so every pre-existing
   output (CASE_021/024/112) and the in-flight CASE_907 still validate
   untouched.
3. **BUILT.** `evidence_references` for an adjuster-supplied type cites the
   recorded intake input, not a document quote; `supplied_by` is schema-required
   because that attribution is the provenance standing in for the quote.
4. **BUILT.** Checkpoint 3 is repurposed as a cross-check, not deleted. It
   still forms its own view; on contradiction it records `axis_cross_check`
   (schema-required whenever the source is `adjuster_input`, so a disagreement
   cannot be silently dropped) and sets `review_required: true` rather than
   overriding. This catches both mis-entry and the CASE_022 shape ("adjuster
   says 배상책임, but no 배상 claim documents are in the pack").
5. **NOT BUILT -- phase 2.** `evaluation` should record
   `case_type_accuracy.applicable: false` with
   `na_reason: "case type supplied by adjuster at intake"` when the source is
   `adjuster_input`. The schema already permits this shape
   (`evaluation_result.schema.json`'s `applicable`/`na_reason`), but neither
   `evaluation.md` nor `evaluation_summary`'s aggregate has been updated, so
   today an adjuster-supplied type would still be scored as if the harness had
   predicted it. Must land before any case with adjuster input reaches
   evaluation.

**Relationship to #7 (Stage 1/2 merge): none -- these are independent.** #7 is
about *where document boundaries are decided*. Case type is **case-level
metadata**, not document-level, so its attachment point is identical under all
three of #7's options (a/b/c). Supplying it need not wait on that decision.

**One constraint to preserve:** the type hint must **not** feed Stage 1's
boundary decision. The text-anchor path is deterministic at precision 1.0000
(2026-08-03); injecting an expectation like "this is a 후유장해 case, so a
장해진단서 should exist" converts a deterministic rule into a biased guess. The
hint is for **post-split reconciliation only**.

**To resolve:** the taxonomy question below (whether the current 5-value enum is
the right vocabulary to hand an adjuster) should be settled first, since it
determines what the input field can accept. Then the 4 change points: an
`intake_case.py` argument, the schema field, checkpoint 3's rewording, and
evaluation's applicability handling.

### 8a. The current case_type enum is NOT the right vocabulary for adjuster input

`case_type_result.schema.json` defines five values: `후유장해`, `진단·수술비`,
`실손`, `배상책임`, `기타`. A survey of the raw corpus
(`docs/case-type-inventory-2026-08-04.md`, read under user authorization) found
this vocabulary cannot express what the corpus contains.

**The corpus is ~4x larger than `outputs/` reflects:** 45 PDFs across 8 folders,
of which **~37 have never been intaken**, plus 2 unexpanded `.zip` archives.
The unprocessed material is two coherent series the enum was never designed
against:

- **개인보험1-20 + 개인보험16 (21 files)** -- verified by reading each `I. 사정
  요약`: every one is **상해후유장해보험금**, a first-party personal-accident
  disability claim (5.5M-50M원). This is the user's "개인보험형".
- **TA1-16 (16 files)** -- **자동차보험 대인배상**, computed on 자동차보험약관
  지급기준 with 소득 and 과실상계. This basis **does not exist anywhere in the
  taxonomy**: it is not 배상책임 as the enum uses that value (시설소유자/영업
  배상책임), not a first-party benefit claim, and has its own computation
  regime. It is the single largest series in the corpus.

**Root finding: the enum conflates two orthogonal axes.** Every multi-type case
straddles in the same direction (배상책임 × 후유장해), because `배상책임`
answers *who pays and on what legal basis* while `후유장해`/`진단·수술비`/`실손`
answer *what kind of loss is computed*. `secondary_case_types` is absorbing that
structure as if it were uncertainty. The registry key `배상책임_후유장해형` is
already a compound of both axes -- the template layer conceded what the enum has
not.

Also found: `실손` has never occurred in any real case (only ever a 0.02-0.15
losing candidate) and has no registry template; `기타` is doing double duty for
"unusual claim" and "not a claim at all" (CASE_030 is policy/증권/청약 only);
and the registry's three keys cover neither large unprocessed series -- though
unlike 실손, both now have real reports on disk to derive structure from, which
is what #2 said was missing.

**DECIDED (user, 2026-08-04): split into two axes.** What the adjuster records
at intake is the *real-world case type* -- 개인보험/후유장해 and the like -- not
the document composition of the pack (약관+손해사정서 etc.), which is a separate
thing the manifest already describes.

| Axis | Field | Values |
|---|---|---|
| 보상 근거 (who pays, on what legal basis) | `coverage_basis` | `배상책임` / `개인보험` / `자동차보험` |
| 손해 유형 (what loss is computed) | `loss_type` | `후유장해` / `진단·수술비` / `실손` |

Both are adjuster-supplied at intake per #8. The pair replaces the flat
`case_type` enum + `secondary_case_types` hedge: the recurring
배상책임 × 후유장해 "straddle" becomes one value on each axis, which is what it
always was.

**Design consequences to work through when implementing:**

1. **One definition point, several consumers.** `case_type` is defined once in
   `case_type_result.schema.json#/$defs/case_type` and `$ref`'d by
   `draft_report_metadata` and others, so the enum itself changes in one place
   -- but `screening_report`, `evaluation_result` (`case_type_accuracy`),
   `evaluation_summary` (`case_type_correct`, aggregate `case_type_accuracy`),
   and 5 agent specs each name the field and need review.
2. **`기타` disappears as a value** and must be re-expressed. It was doing two
   jobs: a genuinely unusual claim, and CASE_030's "not a claim at all". The
   second is not a case type -- see (4).
3. **Migration for the 4 existing outputs** (CASE_021/024/112/908). The mapping
   is mechanical given the survey: CASE_021 → 개인보험/진단·수술비;
   CASE_024 → primary 후유장해 + secondary 배상책임 becomes
   배상책임/후유장해; CASE_112/908 → 배상책임/후유장해. They must stay
   readable, so `case_type` should be retained as a deprecated-but-valid field
   for one version rather than deleted outright (the same one-version
   compatibility approach used for the denial/reduction split on 2026-07-22).
4. **Policy-only, non-claim cases** (CASE_030: 약관/증권/청약, no claim, no
   accident, no insurer response) get no case type at all. Represent as
   nullable-with-reason or an explicit non-claim scope marker -- not as a value
   inside either axis.
5. **`실손` stays in the enum but has no template and no real case.** Accepting
   it as adjuster input means `document_assembly.py --template` fails at render.
   Acceptable only if that failure is explicit at intake rather than at draft
   time.
6. **Template registry must grow to match the axes.** Today's three keys cover
   배상책임×후유장해 and 진단·수술비 only. The corpus now supplies real completed
   reports for 자동차보험 (TA1-16) and first-party 개인보험×후유장해
   (개인보험1-20) -- so unlike 실손, these two can be derived from ground-truth
   structure exactly as #2 did for the original two. `template_id` likely
   becomes derivable from the axis pair rather than being a third
   independently-asserted field.
7. **Not every pair is meaningful.** 자동차보험×실손 or 개인보험×실손 may not
   correspond to real practice. Decide whether to constrain valid combinations
   (a matrix) or accept any pair and let the template lookup fail.
