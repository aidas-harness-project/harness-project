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

**Canonical stage names** (enforced by `run_state.schema.json` v0.2 — any other spelling is rejected at write time; conflict-ledger `raised_by_stage` uses the same enum): `intake`, `document_processing`, `indexing`, `policy_clause_processing`, `claim_analysis`, `denial_response`, `consistency_check`, `screening_report`, `draft_report_v1`, `draft_report_v2`, `critic_v1`, `critic_v2`, `denial_validation`, `evaluation`. Pass exactly these to every `--stage`/`update-run-state` call and require the same of every dispatched agent (each agent spec now pins its own). Adding a pipeline stage means adding it to the schema enum in the same change (D4).

`document_segmentation` is **deprecated and must never be written**. It remains in the schema enum only so runs recorded before 2026-08-05 (CASE_112) still validate; their record is accurate for how they actually executed and is deliberately not rewritten. Segmentation is now a checkpoint *inside* `document_processing`: the bundle is OCR'd and redacted, then split, then its children are classified and redacted — so document processing runs on both sides of the split and the two cannot be separate stages without one of them being wrong about what is in progress.

## Stage-attempt lifecycle (T13 — required around every dispatch)

The orchestrator owns stage attempt boundaries and finalization. Stage agents
write their governed outputs and return a summary/warnings; they do **not**
call `finalize-stage` themselves.

After the ordinary conflict/lock/dependency gates clear and immediately before
dispatching a stage, begin exactly one attempt:

```text
python tools/dao.py update-run-state CASE_ID RUN_ID STAGE in_progress --held-by orchestrator
```

A replay while that attempt is already `in_progress` is idempotent: it neither
increments `attempt_count` nor emits another timing marker. Contract writes
with `--stage` are checkpoints inside this invocation; they do not begin an
attempt and do not count as P9 retries.

After a successful agent return, the orchestrator alone calls:

```text
python tools/dao.py finalize-stage CASE_ID RUN_ID STAGE --held-by orchestrator
```

Only a successful P10 finalization closes the attempt as `passed`. A refused
finalize emits no passed marker. Close a non-successful invocation explicitly:

```text
python tools/dao.py update-run-state CASE_ID RUN_ID STAGE failed \
  --attempt-outcome {failed|partial|schema_failed|finalize_refused} \
  --held-by orchestrator
```

For a process interruption whose real end time is unknowable, use
`--attempt-outcome interrupted` before retrying. This records the state as
failed and leaves the timing interval open rather than turning an overnight
gap into work cost. Then begin the retry with a new `in_progress` transition.
P9's three-attempt limit applies to these explicit dispatch attempts, not to
the number of contract checkpoints written inside one attempt.

The timing layer is diagnostic: `HARNESS_TRACE=0` or a trace-write failure
does not change any gate or state transition. `aggregate-trace` reports
incomplete stage coverage instead of reconstructing missing timings from
run-state marker timestamps.

**Reducing P8 for a throughput run is YOUR decision, never an agent's.**
Stage 2's cost is dominated by dual-read OCR (the corpus is overwhelmingly
scans), so two flags exist on `run_document_stage.py` to cut it. Pass one only
when the run's purpose is timing or plumbing, name it explicitly in the
briefing you dispatch, and never let a stage adopt one on its own to get past
a document that blocked:

- `--on-disagreement assume-reading-a` — dual reads still run and are still
  compared; only the halt is deferred. Disagreed pages take reading_a and
  record `agreement: assume_reading_a` plus an `auto_resolution` block. A
  deferral of the judgement, not a finding: on CASE_911/DOC_005 both reads were
  wrong on 8 of 19 pages (the source was scanned 90° rotated).
- `--single-reader` — P8 off entirely. One read per page, no comparison,
  roughly half the calls and wall time. Pages record `agreement: single_reader`
  and the document reads `cross_validation_status:
  single_reader_no_cross_validation`, `ocr_quality: low`.

**Development sessions set `HARNESS_SINGLE_READER=1`**, which makes P8-off the
default for every OCR call without passing the flag each time. It is an env var
rather than a changed default so an evaluation run can still force real
dual-read P8 with `--dual-read`; an explicit flag always beats the environment,
and passing `--on-disagreement` also implies dual-read (a disagreement policy is
meaningless without a comparison). Check the variable before treating a run's
output as evaluation-grade — a case can be P8-off without any flag appearing in
the command you see.

Mutually exclusive, rejected together at parse. Neither is gated by
`finalize-stage` — a run using either completes normally and the resulting
`review_required: true` is an honest grade on the text, not a work order. That
is exactly why **a case processed with either flag must not be used as
evaluation input**: nothing downstream will stop you, so the scoping decision
is yours here. Record which flag was used in the run notes; a later reader must
not have to infer from a passing stage that P8 was reduced or skipped.

**Pass `--run-id` on read commands too**, and require the same of every
dispatched agent. `read-contract`, `read-document-text`, `read-page-text`,
`search-document-text`, `policy-snapshot`, `read-ledger`,
`check-conflicts-clear` and `get-last-passed-stage` accept an optional
`--run-id`; supplying it records that read's cost into the run's trace, and
omitting it means the read still works but records nothing. Measured on
CASE_910 this accounts for under 2% of an analysis stage, so it is not a
performance lever — it is what lets a stage's remaining unattributed time be
stated honestly instead of merely assumed.

## Phase 0 — context and gating (every run)

1. **Resolve `run_id`**: new run → issue `RUN_{YYYYMMDD}_{NNN}`. Resuming → read `_run_state.json` via the DAO's `get_last_passed_stage(case_id)` query; resume from the next stage after the last one that passed. Do not restart from scratch just because a run was interrupted — that's what P10's per-step backups exist for.
2. **Intake check**: if `data/raw/CASE_XXX/` doesn't exist yet, run intake first (D2 — `_source_ledger.json` gate, every file `pending`→human sets `approved`/`rejected`, whole case blocks on any rejection). Never skip the human confirmation step.
   - **Segmentation**: a raw source may be a *bundle* concatenating several logical documents. **Do not decide in advance which PDFs are bundles.** That question is answered by `propose`, from the text — a proposal with one boundary is a single document, several boundaries is a bundle — so it cannot be a precondition for producing that text. Run every PDF through `tools/segment_case.py` (`propose` → `approve` → `split`); a single-boundary proposal simply splits into itself and the document continues unchanged.

3. **Start the SLA clock**: once intake review is finished, run
   `python tools/dao.py check-source-ledger-clear CASE_ID --run-id RUN_ID`. A `clear` result
   emits the `sla.phase1.start` marker, which is what makes the run's timing measurable at all
   — without it `aggregate-trace` reports `active_s: n/a` and no 30-minute judgment is possible.
   It is idempotent (safe to call repeatedly) and it does **not** add a human gate: it only reads
   the ledger you already had to satisfy, so when D2 eventually goes away this check simply
   always passes. Pass `--run-id` — without it the check still works but records nothing.

**OCR the bundle first.** `propose` resolves its evidence in the order processed text → the PDF's own embedded layer → vision, and the deterministic paths are both better and cheaper than vision. Only the first reaches a scan, which is most of this corpus, so a `required` bundle takes this sequence:

1. `run_checkpoint1.py CASE_ID BUNDLE_ID <pdf> --bundle-ocr` — OCR only, no classification (`document_type` is per-document; one label cannot be right for a bundle).
2. `redact_document.py CASE_ID BUNDLE_ID` — segmentation reads the REDACTED text, so pre-redaction page text never leaves the capability gate. Titles survive redaction (verified on CASE_112's 217 split children), so nothing needed for boundaries is lost.
3. `segment_case.py propose` → `approve` → `split`. The split hands each child the bundle's pages, renumbered from 1 with P8 verdicts intact, so **children are not re-OCR'd**.
4. Classify each child with `run_checkpoint1.py classify-only` — **never `run`**, which begins by OCR'ing and would re-read pages the child just inherited, replacing their P8 history (CASE_909's DOC_006-013 lost theirs exactly that way). A child whose approved title names a form takes its `document_type` from that title with no model call; a genre-only title like `REPORT` falls through to the classifier. Then redact each child.

Measured on CASE_908/DOC_005, a 19-page scan with zero embedded text: precision 1.0000 / recall 0.9091 from the title rule alone, and 1.0000/1.0000 with the LLM tier on the 5 pages (26%) it could not settle. Boundaries from redacted OCR text were byte-identical to those from the embedded layer on CASE_112's 323 policy pages.

Titles are the boundary signal on both paths — `...보통약관`/`...특별약관`/`...특약` for policy, form names (진단서, 수 술 기 록, 진료비 세부산정내역) for medical, matched through a 5-line header window because a statutory header or field labels often precede the title. A page the deterministic rule cannot settle — a generic heading like `REPORT`, or no title at all — goes to the LLM tier, which reads that page and the one before it. An unusable verdict splits and is flagged for review: over-splitting is undone by a human merge at the approval gate, over-merging fuses two documents into one `document_type` and propagates downstream.

On the vision fallback, `propose` automatically rechecks crop-ambiguous `needs_full_page` pages full-page when the 25% spend cap is not saturated; `--refine` is a separate, opt-in pass over long segments that may have been confidently over-merged. Both use the owner-set title rule: own title → new document, no title after full-page inspection → continuation, unreadable → human review.

**The human gate is the boundary approval**, and it is the only one: `split` refuses until a human approves the `segmentation_proposal_{DOC}.json` (case-level `approved` AND every segment `approved`/`edited` AND no unassigned page). A reviewer sees the proposed ranges and the title line each cut is made on — real evidence — instead of answering "is this a bundle?" from a filename before anything has been read. `run_checkpoint1.py` still refuses one thing before any provider or PDF work: a retained `superseded_bundle`, since reading it again would duplicate every page its children already own.

**A policy bundle is deliberately NOT split** — approve it as a single segment even though it plainly concatenates dozens of 약관. Korean policy clauses print their owning 약관 in the clause body (`회사는 '물적손해 확장 추가특별약관' 제2조...`), so downstream identification runs off `clause_id` text, not the PDF the clause happens to sit in -- CASE_021's four policy matches distinguished 삼성화재/KB/한화 while all citing one `document_id`. Splitting is pure cost: measured on CASE_905's two bundles, split produced 197 documents / 176 classification calls / 39.8 minutes against 5 documents / 5 calls / ~40 seconds unsplit, and `chunk_text` re-splits to page granularity either way, so both arms ended at the identical 323 page chunks. This exception is specific to policy bundles: a MEDICAL bundle (진단서 + 검사보고서 + 입퇴원확인서 + 진료비명세서 scanned into one PDF, as CASE_907's DOC_005) still wants splitting, because `document_type` is per-document and one label cannot be right for all of them.

   Records under the `document_processing` stage — segmentation is one of its checkpoints, not a stage of its own. Segmentation's own output is document STRUCTURE, not text or type: `document-pipeline` still owns real classification, and a `provisional_type_label` is never copied into `document_type`.
3. **Conflict-ledger check**: before dispatching *any* stage, call `check_conflicts_clear(case_id)`. If not clear, halt and report every pending entry (old and new) — do not proceed past an unresolved conflict, no matter which stage raised it.
4. **Lock check**: if a stage's target file already has a `.lock` present at run start/resume, do not poll and do not assume it's stale — halt, report the lock's full contents, wait for human confirmation (P5).
5. **Begin the stage attempt** using the T13 lifecycle command above, then dispatch. Never infer an attempt start from the first output write.

## Phase 1 — initial claim review

| # | Stage | Agent | Internal checkpoints |
|---|---|---|---|
| 1 | Case Intake | (orchestrator + `intake_case.py`) | D2-gated `_source_ledger.json` — the one intake decision is raw vs ground_truth, not bundle vs single document. Records under `intake` |
| 2 | Document Processing | `document-pipeline` | (a) bundle OCR (`--bundle-ocr`, no classification) → (b) bundle redaction → (c) **segmentation**: propose/approve/split, children inherit the bundle's pages → (d) per-child classification → (e) per-child redaction → (f) case-wide chunking. All under `document_processing`; segmentation sits *inside* because processing runs on both sides of it |
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
| Stage returns `partial` or fails (P9) | Close the current attempt with `--attempt-outcome partial` or `failed`, then retry from its last internal checkpoint up to 3 explicit dispatch attempts; after 3, halt for user audit |
| Conflict-ledger has any `pending` entry (P6) | Halt before dispatching the next stage, list all pending entries |
| Extraction cross-validation disagrees (P8) | Halt immediately, no tolerance threshold, even for one field on one document |
| Human input pending (P7) | Wait — `human_input_status` in `_run_state.json` (written via `dao.py set-human-input-status`/`request-expert-review`) shows exactly what's pending; never fabricate a stand-in, and never call `mark-human-review-complete` yourself |
| Unauthorized ground-truth access detected outside `evaluation` (D1) | Halt immediately, exclude the run's outputs from evaluation |

## Completion report

**Aggregate the run's timing first.** After `draft_report_v1` finalizes (which emits
`sla.phase1.end`), run once:

```
python tools/dao.py aggregate-trace CASE_ID --run-id RUN_ID --held-by <name>     [--input-class S|M|L|XL] [--cold-or-warm cold|warm]
```

This is the ONLY manual step in the timing path — spans are recorded automatically by every
tool, and both SLA markers are emitted by the DAO itself, but nothing calls the aggregator on
its own, so skipping it means the run leaves raw shards and no `_timing_summary.json`. It is
read-only over the trace, runs after the work is done, and contends with nothing. Report
`active_s` (the SLA number: wall clock minus human-gate waiting) and the top `by_category`
entries. `active_s: n/a` means a marker is missing, not that the run was instant — usually the
Phase 0 ledger check was skipped or run without `--run-id`. Read it back later with
`dao.py read-timing-summary CASE_ID`. `HARNESS_TRACE=0` disables tracing entirely.

At the end of a run (or when halted), report to the user: per-stage pass/fail/pending status from `_run_state.json`, validation PASS/FAIL/SKIP tally, `review_required` count and routing (손사/의사), any partial/warning list, and next actions (e.g. awaiting human review). Ask for feedback — this harness evolves from it, see the root `CLAUDE.md` changelog.
