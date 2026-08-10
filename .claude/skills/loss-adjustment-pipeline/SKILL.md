---
name: loss-adjustment-pipeline
description: Orchestrator for the local Units 1-7 loss-adjustment harness. Use for case processing, reruns, screening/draft reports, and insurer-denial updates. Evaluation is deferred and unavailable locally.
---

# Loss-Adjustment Pipeline Orchestrator

Coordinates the authorized local pipeline across two phases to produce screening and draft reports through expert review. Evaluation is an unavailable external continuation that requires the deferred isolated Unit 11 service; never dispatch it or read ground truth locally. See `pipeline.md` for the full stage/agent table and I/O contracts.

**Execution mode: sub-agent pipeline.** Every stage below is dispatched as a subagent call naming that agent's definition file (`.claude/agents/{name}.md`), with `model: opus`. All inter-agent data passes through the DAO as files — agent return values carry only a summary and warnings, never the actual contract data.

**Canonical stage names** (enforced by `run_state.schema.json` v0.3 — any other spelling is rejected at write time; conflict-ledger `raised_by_stage` uses the same enum): `intake`, `document_processing`, `indexing`, `policy_clause_processing`, `claim_analysis`, `denial_response`, `consistency_check`, `screening_report`, `draft_report_v1`, `draft_report_v2`, `critic_v1`, `critic_v2`, `denial_validation`, `human_review_v1`, `human_review_v2`. Pass exactly these to every `--stage`/`update-run-state` call and require the same of every dispatched agent (each agent spec now pins its own). Adding a pipeline stage means adding it to the schema enum in the same change (D4).

## Phase 0 — context and gating (every run)

1. **Resolve `run_id`**: new run → issue `RUN_{YYYYMMDD}_{NNN}`. Resuming → read `_run_state.json` via the DAO's `get_last_passed_stage(case_id)` query; resume from the next stage after the last one that passed. Do not restart from scratch just because a run was interrupted — that's what P10's per-step backups exist for.
2. **Intake check**: if `data/raw/CASE_XXX/` doesn't exist yet, run intake first (D2 — `_source_ledger.json` gate, every file `pending`→human sets `approved`/`rejected`, whole case blocks on any rejection). Never skip the human confirmation step.
3. **Conflict-ledger check**: before dispatching *any* stage, call `check_conflicts_clear(case_id)`. If not clear, halt and report every pending entry (old and new) — do not proceed past an unresolved conflict, no matter which stage raised it.
4. **Lock check**: call `python tools/dao.py check-lock CASE_ID TARGET` for the stage target. Halt only when the DAO reports active ownership. Persistent unlocked diagnostic sidecars are expected and pathname presence alone never means a lock is held (P5).
5. **Medical-clearance check**: after canonical medical variables have been published, call `python tools/dao.py check-medical-reviews-clear CASE_ID` immediately before every downstream agent dispatch. Halt while it reports blocked. The DAO independently repeats this check before downstream `in_progress`/`passed` transitions and snapshots.

## Phase 1 — initial claim review

| # | Stage | Agent | Internal checkpoints |
|---|---|---|---|
| 1 | Case Intake | (orchestrator + intake tool) | D2-gated `_source_ledger.json` |
| 2 | Document Processing | `document-pipeline` | (a) provider-backed OCR/P8 cross-validation+classification, (b) redaction, (c) chunking — each a real DAO checkpoint |
| 3 | Indexing (adapter, optional) | (tool, no agent) | pass-through by default; no-op unless enabled |
| 4 | Policy Clause Processing | `policy-pipeline` | (a) clause boundary ID, (b) extraction, (c) normalization |
| 5 | Claim Analysis | `claim-analysis` | (a) field extraction + canonical medical-variable publication, (b) coverage ID, (c) case-type classification, (d) requirement matching, then medical clearance before pass/snapshot |
| 6 | Consistency Check | `consistency-check` | conflict-ledger-gated — any disagreement halts via `_conflict_ledger.json`, not an inline ad-hoc halt |
| 7 | Screening Report | `screening-report` | consumes `denial-response`'s output as a dependency if an insurer-response document exists — not phase-gated |
| 8 | Draft Report v1 | `draft-report` | same agent reused for v2 in Phase 2 |
| 9 | Critic Pass (v1) | `critic` | blind — never touches ground truth |
| 10 | Expert Review v1 / external handoff | human-owned | completes the local v1 boundary; Evaluation remains unavailable |

`denial-response` is **not** a numbered Phase 1 stage — it's dependency-triggered. It runs whenever a flagged insurer-response document's processed text (from stage 2) is ready, whether that happens to be during Phase 1 (closed-case packs that bundle the insurer notice from the start) or later. Same agent, same mechanism, no phase-based scheduling exception needed.

**Claim-analysis medical gate**: after the agent publishes its evidence-derived candidate with `write-medical-variables`, do not mark `claim_analysis` passed or call `snapshot-backup` until `python tools/dao.py check-medical-reviews-clear CASE_ID` succeeds. The DAO also rejects both transitions without clearance. The orchestrator does not open review items, choose referral policy, or stand in for a human decision.

**Between stage 9 and the external handoff**: once `critic` passes, call `dao.py request-expert-review CASE_ID {v1|v2}` to mark `human_input_status: waiting` (P7) and hand the reviewed draft + `critic_result_v{version}.json` to a genuine human reviewer. Once validated human-owned review content exists, the human-only `dao.py mark-human-review-complete CASE_ID {v1|v2} --reviewer NAME` records the future Unit 11 handoff prerequisite. It does not enable local Evaluation or ground-truth access.

## Phase 2 — insurer denial/reduction response

Only two genuinely new stages — everything else is Phase 1's agents reused on new input.

| # | Stage | Agent | Internal checkpoints |
|---|---|---|---|
| 1 | Denial Validation | `denial-validation` | (a) evidence retrieval + validate denial reasons against it → `denial_validation_result.json`, (b) rebuttal point generation → `rebuttal_points.json`/`.md` |
| 2 | Draft Report v2 | `draft-report` | second checkpoint of the same agent from Phase 1 stage 8 |
| 3 | Critic Pass (v2) | `critic` | same agent as Phase 1 stage 9 |
| 4 | Expert Review v2 / external handoff | human-owned | terminal local boundary; Evaluation remains unavailable |

**Important distinction for `denial-validation`**: insurer-vs-evidence disagreement is this stage's entire analytical purpose (that's what a rebuttal *is*), not a P6 conflict. P6 is for our own sources contradicting each other. Do not route denial-vs-evidence findings through the conflict ledger.

## Error handling (see `harness-guardrails` for the full rules — this is the orchestrator-level summary)

| Situation | Response |
|---|---|
| Schema validation fails twice (P4) | Halt; the user may request validated retries, provide a manual correction for validation, or abandon the run |
| Stage returns `partial` or fails (P9) | Retry the stage (from its last internal checkpoint, not from scratch) up to 3 fixed attempts, then halt for user audit |
| Conflict-ledger has any `pending` entry (P6) | Halt before dispatching the next stage, list all pending entries |
| Extraction cross-validation disagrees (P8) | Halt immediately, no tolerance threshold, even for one field on one document |
| Human input pending (P7) | Wait — `human_input_status` in `_run_state.json` (written via `dao.py set-human-input-status`/`request-expert-review`) shows exactly what's pending; never fabricate a stand-in, and never call `mark-human-review-complete` yourself |
| Any local ground-truth or Evaluation attempt (D1) | Halt immediately; the deferred isolated service is unavailable |

## Completion report

At the end of a run (or when halted), report to the user: per-stage pass/fail/pending status from `_run_state.json`, validation PASS/FAIL/SKIP tally, `review_required` count and routing (손사/의사), any partial/warning list, and next actions (e.g. awaiting human review). Ask for feedback — this harness evolves from it, see the root `CLAUDE.md` changelog.
