---
name: loss-adjustment-pipeline
description: Orchestrator for the loss-adjustment agent harness. Use when the user asks to process a case, run/rerun the pipeline, generate a screening report or draft report, update a draft after an insurer denial, or run evaluation. Simple questions about pipeline design can be answered directly from pipeline.md.
---

# Loss-Adjustment Pipeline Orchestrator

Coordinates 10 agents across two phases to turn case intake into a screening report, draft report, and evaluation. See `pipeline.md` for the full stage/agent table and I/O contracts. See `harness-guardrails` and `harness-guardrails-dev` for the rules every agent (including this orchestrator) must follow regardless of stage.

**Execution mode: sub-agent pipeline.** Every stage below is dispatched as a subagent call naming that agent's definition file (`.claude/agents/{name}.md`), with `model: opus`. All inter-agent data passes through the DAO as files — agent return values carry only a summary and warnings, never the actual contract data.

**Canonical stage names** (enforced by `run_state.schema.json` v0.2 — any other spelling is rejected at write time; conflict-ledger `raised_by_stage` uses the same enum): `intake`, `document_processing`, `indexing`, `policy_clause_processing`, `claim_analysis`, `denial_response`, `consistency_check`, `screening_report`, `draft_report_v1`, `draft_report_v2`, `critic_v1`, `critic_v2`, `denial_validation`, `evaluation`. Pass exactly these to every `--stage`/`update-run-state` call and require the same of every dispatched agent (each agent spec now pins its own). Adding a pipeline stage means adding it to the schema enum in the same change (D4).

## Phase 0 — context and gating (every run)

1. **Resolve `run_id`**: new run → issue `RUN_{YYYYMMDD}_{NNN}`. Resuming → read `_run_state.json` via the DAO's `get_last_passed_stage(case_id)` query; resume from the next stage after the last one that passed. Do not restart from scratch just because a run was interrupted — that's what P10's per-step backups exist for.
2. **Intake check**: if `data/raw/CASE_XXX/` doesn't exist yet, run intake first (D2 — `_source_ledger.json` gate, every file `pending`→human sets `approved`/`rejected`, whole case blocks on any rejection). Never skip the human confirmation step.
3. **Conflict-ledger check**: before dispatching *any* stage, call `check_conflicts_clear(case_id)`. If not clear, halt and report every pending entry (old and new) — do not proceed past an unresolved conflict, no matter which stage raised it.
4. **Lock check**: if a stage's target file already has a `.lock` present at run start/resume, do not poll and do not assume it's stale — halt, report the lock's full contents, wait for human confirmation (P5).

## Phase 1 — initial claim review

| # | Stage | Agent | Internal checkpoints |
|---|---|---|---|
| 1 | Case Intake | (orchestrator + intake tool) | D2-gated `_source_ledger.json` |
| 2 | Document Processing | `document-pipeline` | (a) OCR+cross-validation+classification, (b) redaction, (c) chunking — each a real DAO checkpoint |
| 3 | Indexing (adapter, optional) | (tool, no agent) | pass-through by default; no-op unless enabled |
| 4 | Policy Clause Processing | `policy-pipeline` | (a) clause boundary ID, (b) extraction, (c) normalization |
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
