---
name: loss-adjustment-pipeline
description: Orchestrator for the loss-adjustment agent harness. Use when the user asks to process a case, run/rerun the pipeline, generate a screening report or draft report, update a draft after an insurer denial, or run evaluation. Simple questions about pipeline design can be answered directly from pipeline.md.
---

# Loss-Adjustment Pipeline Orchestrator

Coordinates 10 agents across two phases to turn case intake into a screening report, draft report, and evaluation. See `pipeline.md` for the full stage/agent table and I/O contracts. See `harness-guardrails` and `harness-guardrails-dev` for the rules every agent (including this orchestrator) must follow regardless of stage.

**Execution mode: sub-agent pipeline.** Every stage below is dispatched as a subagent call naming that agent's definition file (`.claude/agents/{name}.md`), with `model: opus`. All inter-agent data passes through the DAO as files — agent return values carry only a summary and warnings, never the actual contract data.

There is no standalone `run_pipeline.py` process. This skill is the executable
orchestration contract: a case-processing request must follow every gate below
in order. Calling a later tool directly bypasses orchestration and is not a
pipeline run.

**Canonical stage names** (enforced by `run_state.schema.json` v0.2 — any other spelling is rejected at write time; conflict-ledger `raised_by_stage` uses the same enum): `intake`, `document_segmentation`, `document_processing`, `indexing`, `policy_clause_processing`, `claim_analysis`, `denial_response`, `consistency_check`, `screening_report`, `draft_report_v1`, `draft_report_v2`, `critic_v1`, `critic_v2`, `denial_validation`, `evaluation`. Pass exactly these to every `--stage`/`update-run-state` call and require the same of every dispatched agent (each agent spec now pins its own). Adding a pipeline stage means adding it to the schema enum in the same change (D4).

## Phase 0 — context and gating (every run)

1. **Resolve `run_id`**: new run → issue `RUN_{YYYYMMDD}_{NNN}`. Resuming → read `_run_state.json` via the DAO's `get_last_passed_stage(case_id)` query; resume from the next stage after the last one that passed. Do not restart from scratch just because a run was interrupted — that's what P10's per-step backups exist for.
2. **Intake check**: if `data/raw/CASE_XXX/` doesn't exist yet, run intake first (D2 — `_source_ledger.json` gate, every file `pending`→human sets `approved`/`rejected`, whole case blocks on any rejection). Never skip the human confirmation step.
   - **Segmentation check**: a raw source may be a *bundle* concatenating several logical documents. Every newly intaken PDF starts `segmentation_status: pending_review`; a genuine human records `required` or `not_required` with `python tools/dao.py set-segmentation-status CASE_ID DOC_ID STATUS --reviewer NAME --held-by NAME --run-id RUN_ID`. Automatic bundle-candidate classification is not implemented and an agent never supplies this human decision. Split every `required` bundle into per-document `DOC_XXX.pdf` via `tools/segment_case.py` (`sheets`/`propose [--refine]`/`approve`/`split`).

**OCR the bundle first.** `propose` resolves its evidence in the order processed text → the PDF's own embedded layer → vision, and the deterministic paths are both better and cheaper than vision. Only the first reaches a scan, which is most of this corpus, so a `required` bundle takes this sequence:

1. `run_checkpoint1.py CASE_ID BUNDLE_ID <pdf> --bundle-ocr` — OCR only, no classification (`document_type` is per-document; one label cannot be right for a bundle).
2. `redact_document.py CASE_ID BUNDLE_ID` — segmentation reads the REDACTED text, so pre-redaction page text never leaves the capability gate. Titles survive redaction (verified on CASE_112's 217 split children), so nothing needed for boundaries is lost.
3. `segment_case.py propose` → `approve` → `split`. The split hands each child the bundle's pages, renumbered from 1 with P8 verdicts intact, so **children are not re-OCR'd**.
4. Classify and redact the children normally.

Measured on CASE_908/DOC_005, a 19-page scan with zero embedded text: precision 1.0000 / recall 0.9091 from the title rule alone, and 1.0000/1.0000 with the LLM tier on the 5 pages (26%) it could not settle. Boundaries from redacted OCR text were byte-identical to those from the embedded layer on CASE_112's 323 policy pages.

Titles are the boundary signal on both paths — `...보통약관`/`...특별약관`/`...특약` for policy, form names (진단서, 수 술 기 록, 진료비 세부산정내역) for medical, matched through a 5-line header window because a statutory header or field labels often precede the title. A page the deterministic rule cannot settle — a generic heading like `REPORT`, or no title at all — goes to the LLM tier, which reads that page and the one before it. An unusable verdict splits and is flagged for review: over-splitting is undone by a human merge at the approval gate, over-merging fuses two documents into one `document_type` and propagates downstream.

On the vision fallback, `propose` automatically rechecks crop-ambiguous `needs_full_page` pages full-page when the 25% spend cap is not saturated; `--refine` is a separate, opt-in pass over long segments that may have been confidently over-merged. Both use the owner-set title rule: own title → new document, no title after full-page inspection → continuation, unreadable → human review. This has its **own human-review gate**: `split` refuses until a human approves the `segmentation_proposal_{DOC}.json` (case-level `approved` AND every segment `approved`/`edited` AND no unassigned page). Stage 2 also enforces a case-wide structural preflight in `run_checkpoint1.py`: any `pending_review`/`required` PDF, legacy PDF missing the status, or direct attempt to process a `superseded_bundle` returns `blocked_segmentation` before provider construction or OCR. Do not dispatch Stage 2 until `dao.py check-segmentation-ready CASE_ID` passes. **A policy bundle is deliberately NOT split.** Record `not_required` for an insurance-policy bundle even though it plainly concatenates dozens of 약관: Korean policy clauses print their owning 약관 in the clause body (`회사는 '물적손해 확장 추가특별약관' 제2조...`), so downstream identification runs off `clause_id` text, not the PDF the clause happens to sit in -- CASE_021's four policy matches distinguished 삼성화재/KB/한화 while all citing one `document_id`. Splitting is pure cost: measured on CASE_905's two bundles, split produced 197 documents / 176 classification calls / 39.8 minutes against 5 documents / 5 calls / ~40 seconds unsplit, and `chunk_text` re-splits to page granularity either way, so both arms ended at the identical 323 page chunks. This exception is specific to policy bundles: a MEDICAL bundle (진단서 + 검사보고서 + 입퇴원확인서 + 진료비명세서 scanned into one PDF, as CASE_907's DOC_005) still wants splitting, because `document_type` is per-document and one label cannot be right for all of them.

   Records under the `document_segmentation` stage. No OCR happens here — segmentation output is document structure, not text; `document-pipeline` still owns real classification.
3. **Conflict-ledger check**: before dispatching *any* stage, call `check_conflicts_clear(case_id)`. If not clear, halt and report every pending entry (old and new) — do not proceed past an unresolved conflict, no matter which stage raised it.
4. **Lock check**: if a stage's target file already has a `.lock` present at run start/resume, do not poll and do not assume it's stale — halt, report the lock's full contents, wait for human confirmation (P5).

## Phase 1 — initial claim review

| # | Stage | Agent | Internal checkpoints |
|---|---|---|---|
| 1 | Case Intake & Document Segmentation | (orchestrator + `intake_case.py` + `segment_case.py`) | D2-gated `_source_ledger.json`; then bundle→per-document split with its own human-approval gate on `segmentation_proposal_{DOC}.json` (blocks stage 2). No OCR — structure only |
| 2 | Document Processing | `document-pipeline` | (a) provider-backed OCR/P8 cross-validation+classification, (b) redaction, (c) chunking — each a real DAO checkpoint |
| 3 | Indexing (adapter, optional) | (tool, no agent) | pass-through by default; no-op unless enabled |
| 4 | Policy Clause Processing | `policy-pipeline` | (a) exact boundary inventory, (b) semantic extraction, (c) reference tables, (d) version-bound audit; finalize only when every audit is current and has no open finding |
| 5 | Claim Analysis | `claim-analysis` | (a) field extraction, (b) coverage ID, (c) case-type classification, (d) requirement matching |
| 6 | Consistency Check | `consistency-check` | conflict-ledger-gated — any disagreement halts via `_conflict_ledger.json`, not an inline ad-hoc halt |
| 7 | Screening Report | `screening-report` | consumes `denial-response`'s output as a dependency if an insurer-response document exists — not phase-gated |
| 8 | Draft Report v1 | `draft-report` | same agent reused for v2 in Phase 2 |
| 9 | Critic Pass (v1) | `critic` | blind — never touches ground truth |
| 10 | Evaluation | `evaluation` | sole D1 exception, only after human review is marked complete |

`denial-response` is **not** a numbered Phase 1 stage — it's dependency-triggered. It runs whenever a flagged insurer-response document's processed text (from stage 2) is ready, whether that happens to be during Phase 1 (closed-case packs that bundle the insurer notice from the start) or later. Same agent, same mechanism, no phase-based scheduling exception needed.

**Between stage 9 and stage 10**: once `critic` passes, call `dao.py request-expert-review CASE_ID {v1|v2}` to mark `human_input_status: waiting` (P7) and hand the reviewed draft + `critic_result_v{version}.json` to a human. Once that human's disposition genuinely exists, `evaluation` writes `expert_review_v{version}.json` from it (this part needs no ground truth, see evaluation.md). Then — **not by any agent, a genuine human action** — `dao.py mark-human-review-complete CASE_ID {v1|v2} --reviewer NAME` creates the D1 gate flag (it independently requires `expert_review_v{version}.json` to already exist and be schema-valid, so this can't be rubber-stamped). Only then does `evaluation`'s `read-ground-truth` call stop being denied.

## Phase 2 — insurer denial/reduction response

Only two genuinely new stages — everything else is Phase 1's agents reused on new input.

| # | Stage | Agent | Internal checkpoints |
|---|---|---|---|
| 1 | Denial Validation | `denial-validation` | (a) evidence retrieval + validate denial reasons against it → `denial_validation_result.json`, (b) rebuttal point generation → `rebuttal_points.json`/`.md` |
| 2 | Draft Report v2 | `draft-report` | second checkpoint of the same agent from Phase 1 stage 8 |
| 3 | Critic Pass (v2) | `critic` | same agent as Phase 1 stage 9 |
| 4 | Evaluation | `evaluation` | same agent as Phase 1 stage 10 |

**Important distinction for `denial-validation`**: insurer-vs-evidence disagreement is this stage's entire analytical purpose (that's what a rebuttal *is*), not a P6 conflict. P6 is for our own sources contradicting each other. Do not route denial-vs-evidence findings through the conflict ledger.

## Error handling (see `harness-guardrails` for the full rules — this is the orchestrator-level summary)

| Situation | Response |
|---|---|
| Schema validation fails twice (P4) | Halt, present ignore-and-proceed / retry-N-times / fix-manually to the user |
| Stage returns `partial` or fails (P9) | Retry the stage (from its last internal checkpoint, not from scratch) up to 3 fixed attempts, then halt for user audit |
| Conflict-ledger has any `pending` entry (P6) | Halt before dispatching the next stage, list all pending entries |
| Extraction cross-validation disagrees (P8) | Halt immediately, no tolerance threshold, even for one field on one document |
| Human input pending (P7) | Wait — `human_input_status` in `_run_state.json` (written via `dao.py set-human-input-status`/`request-expert-review`) shows exactly what's pending; never fabricate a stand-in, and never call `mark-human-review-complete` yourself |
| Unauthorized ground-truth access detected outside `evaluation` (D1) | Halt immediately, exclude the run's outputs from evaluation |

## Completion report

At the end of a run (or when halted), report to the user: per-stage pass/fail/pending status from `_run_state.json`, validation PASS/FAIL/SKIP tally, `review_required` count and routing (손사/의사), any partial/warning list, and next actions (e.g. awaiting human review). Ask for feedback — this harness evolves from it, see the root `CLAUDE.md` changelog.
