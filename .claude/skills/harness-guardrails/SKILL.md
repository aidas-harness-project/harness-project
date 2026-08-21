---
name: harness-guardrails
description: Hard constraints that apply to every agent and every stage of the loss-adjustment harness, at all times. Not stage-specific guidance — a violation here invalidates the run's evaluation or corrupts shared state for every downstream stage. Every agent must follow this regardless of which stage it's executing, in addition to whatever stage-specific skill it's using.
---

# Harness Guardrails

These are non-negotiable. Stage-specific skills describe *how* to do a stage's job; this document constrains *what any agent is ever allowed to do*, full stop.

During the PoC/evaluation phase, also see `harness-guardrails-dev` — ground-truth isolation and intake rules that only apply while a ground-truth answer key exists in this repo.

## PoC scope: four rules are OFF

Turned off by the PoC owner on 2026-08-20, each with the measurement behind
it. The register is `dao.POC_INACTIVE_GUARDRAILS`, and removing a code from
there turns the gate back on. **These are throughput reductions, not
corrections** — a run made under them has no P8 evidence and is not
privacy-preserving, and any report of its results has to say so.

| Rule | What is off | What stays on |
|---|---|---|
| **P7** | human-input wait tracking (`human_input_status`) | Every synchronous human gate: D2 approval, segmentation boundaries, P8 resolution, P6 dispositions. 0 of 220 cases ever carried a `waiting` entry — the PoC has no asynchronous human step for it to track. |
| **P9** | the 3-attempt cap and its audit halt | Attempt counts are still recorded in `_run_state.json`. What is off is the automatic halt, not the measurement. |
| **P10** | the *cumulative snapshot* half | The run-state file — P10's other half — is untouched and is still the single source of truth for where a run stopped. Snapshots still run; they now carry the governed contracts only. `_backups` was 17.8MB of CASE_489's 21.4MB (83%, 594 of 1011 files), almost entirely `ocr_result_*.json`, which is regenerable and already held in `data/processed/`. |
| **P8** | reader independence (`--single-reader` is now the default) | **Disagreement handling is unchanged.** If a comparison does happen, a material disagreement still hard-halts with no tolerance threshold. Every single-reader document is still stamped `ocr_quality: low`, `cross_validation_status: single_reader_no_cross_validation`, `review_required`. |

`--dual-read`, `--redact`, and `HARNESS_SINGLE_READER=0` / `HARNESS_SKIP_REDACTION=0`
restore the full paths for a run that needs them.

**P1, P2, P3, P4, P5, P6, P11 are unchanged.** P4 earned its place the day
this register was written: it caught CASE_048's `denial_response` returning a
Korean value for an English enum, twice, which would otherwise have reached
the screening report.

## P1. No fabricated claims

Every extracted value or assertion must trace to a specific source quote. If it can't be traced, it is marked unconfirmed and routed for review — never stated as fact. This is the harness's highest-probability failure mode; treat any unlinked claim as a bug.

**A claim about the SYSTEM needs evidence too, and a relayed report is not evidence.** Anything you say about how a tool behaves, what a gate refuses, or what state a file is in must come from running the tool or reading the file *in this session* — never from a briefing, a prior agent's findings, an earlier note, or a `known-gaps` entry. Those describe what was true when written, and this repo changes under you: on 2026-08-05 an agent reported a text-leak defect as live and reproducible one hour after it had been fixed and committed, because it inherited the observation from an earlier agent's report instead of running the command. A stale report is worse than silence — it sends the next reader to re-fix something already fixed, and it discredits the real findings sitting next to it. If you cannot re-verify a relayed claim, say it is unverified and name what you would run; do not repeat it as a finding.

**Reading the code is not evidence of what ran.** The rule above closes stale and relayed claims; this closes the other route to a fabricated system claim — deriving behaviour from source structure and reporting it as observation. Source tells you what *can* happen; only records tell you what *did*. A claim about how a stage behaved, what an agent retried, or whether a failure was absorbed must come from a record of that execution — `read-timing-summary` (per-category `count`/`error_count`), `read-ledger`, `read-conflict-ledger`, `read-driver-receipt`, a contract on disk, a test run. When the record cannot settle it, say "not measured" or "not determinable from retained records" and name what would, rather than supplying a mechanism that sounds right: on 2026-08-16 an assistant explained away four straight validation failures by asserting the agent path "silently absorbs" such rejections through its retry loop; the timing summaries showed **0 validate errors across all three agent arms** (CASE_029/032/035), and `write-contract` makes exactly one attempt and does not implement P4's retry loop at all. The invented mechanism inverted the actual finding — that those failures were specific to the path under test — and would have buried it.

In narrative documents (screening reports, drafts), agents never hand-write citation tags or maintain sidecar files themselves. An agent provides content plus `evidence_references` (document_id, page, quote) per field/section to the document-assembly tool; the tool deterministically renders the document and auto-generates the `[E#]` tags plus the matching `.evidence.json` sidecar (e.g. `draft_report_v1.evidence.json` next to `draft_report_v1.md`) in the same pass. This removes the possibility of a tag and its citation drifting out of sync — one process produces both from the same source data. Structured JSON outputs keep using `evidence_references` directly — no tag layer needed there, the field already is the citation.

## P2. Raw input is read-only; read from the processed layer

Original case input files are never modified or deleted, by any agent, under any circumstance. Extracted/processed document text lives in `data/processed/CASE_XXX/`. Before reading a document's content, an agent checks whether a processed result already exists there. If it exists, read from there — never re-read the raw original. If it doesn't exist, the agent does not read the raw file directly and does not perform extraction itself — it invokes the extraction pipeline stage (currently: document-pipeline) to produce the processed result first. No agent freelances extraction or bypasses the processed layer, even when the processed file is missing — that's the guardrail.

(Directory/stage names above are current as of this writing — see D4 in `harness-guardrails-dev` for the rule keeping them in sync.)

## P3. No definitive legal/medical assertions — targets inference, not restatement

Direct citations of what a source document states are not legal/medical-certainty claims — they're facts, covered by P1's evidence-linking. This rule applies only when an agent draws its own conclusion beyond what's written: causation between an event and a diagnosis, a disability-percentage determination, a coverage-eligibility verdict, a prognosis, an opinion on case outcome.

When an agent draws this kind of inference, it hedges the phrasing and flags the claim for review — it does not halt the current stage. Flagged claims propagate forward and surface together at the aggregation/report stage for batch human review.

## P4. Schema validation failure → retry once → halt → human decides

A stage whose output fails schema validation gets exactly one self-correction attempt. If the retry also fails, the entire pipeline run halts — do not pass unvalidated output downstream under any pressure to keep moving. Contamination is far more expensive to trace once it's propagated past one stage.

On halt, the orchestrator alerts the user with the validation errors and the failing output, and asks the user to pick one:

- **retry-N-times** — user specifies N, the agent retries up to N additional attempts before halting again
- **fix-manually** — user edits the output directly; the orchestrator re-runs validation on the edited file before resuming
- **abandon-run** — leave the run halted and do not publish or pass the invalid output downstream

The pipeline does not pick one of these on its own — it's a human decision every time. Retry or manual correction resumes only after the replacement output validates successfully; no option authorizes unvalidated propagation.

## P5. Shared-file editing requires an exclusive lock

Every write path in `tools/dao.py` (`write-contract`, `write-page-text`, `write-redacted-text`, `write-text`, `write-reviewed-draft`, `patch-manifest-document`, `add-conflict-entry`, `set-conflict-verdict`, `update-run-state`, `set-human-input-status`, `snapshot-backup`, `set-ledger-status`) acquires two kernel-owned resources before making any edit: a Linux abstract UNIX-domain ownership token keyed by the logical lock coordinate, and an exclusive advisory lock on a persistent `<filename>.lock` sidecar inode. The abstract token is the non-replaceable serialization authority: unlinking or replacing the sidecar pathname cannot create a second owner. Each successfully bound token enters a fork-guarded active-token registry before `_try_kernel_lock` hands it to generic, run-owner, or descriptor-anchored acquisition; child at-fork cleanup therefore closes even a token whose later file-lock handle has not yet been registered. The sidecar remains after ordinary release as human-readable diagnostics and compatibility state; pathname presence alone never means a lock is held. Generic nested writes bind the sidecar and payload to the same no-follow parent descriptor. Release is descriptor/token-owned, never unlinks a pathname, and generic path-based release is scoped to the acquiring PID and thread. Forked children discard inherited duplicates without issuing `LOCK_UN`, so they cannot release or retain a parent's live ownership. This is structural, not something an agent has to remember to do itself — an agent never writes to a shared file by any path other than these DAO subcommands.

Lock metadata while the abstract ownership token and sidecar advisory lock are held (required; stale bytes in an unlocked persistent inode are not ownership):

```json
{
  "held_by": "<agent-name>",
  "run_id": "<run-id>",
  "started_at": "<ISO-8601 timestamp>",
  "purpose": "<short description of the edit>"
}
```

**Mid-run conflict handling:** if a DAO write subcommand finds a lock already held, it does not fail immediately. It sleeps 30 seconds, then rechecks. Repeats on a fixed 30-second interval (no exponential backoff) up to 15 minutes total, *inside the same CLI call* — the calling agent doesn't implement this loop itself, it just waits for the one call to return. For the DAO's own read-modify-write subcommands (`add-conflict-entry`, `set-conflict-verdict`, `update-run-state`, `set-ledger-status`), the lock is held across the *entire* read+modify+write, not just the final write, so the read those calls act on is always fresh once the lock clears — nothing else could have written to that file while this call was waiting. `write-contract` is the one caller-driven exception: the data it writes was already assembled by the calling agent *before* the call (via an earlier, separate `read-contract`), so waiting for the lock here prevents write/write corruption but does not by itself guarantee the agent's read was fresh — an agent updating a shared multi-writer file (e.g. `document_manifest.json`) is still responsible for re-reading right before building what it hands to `write-contract`. If 15 minutes pass with the lock still held, the call gives up and reports the lock's full contents; the calling agent halts and waits for explicit human confirmation before proceeding.

`set-ledger-status`, `add-conflict-entry`, `set-conflict-verdict`, and every mutating medical-review lifecycle command require a caller-generated `--operation-id`. Preserve that exact ID across retries after an unknown transport outcome; a new operation gets a new ID. The live API and UI must transport and retain the same ID rather than relying on a DAO fallback.

**Run-start/resume conflict handling (deliberately different from mid-run):** the orchestrator asks the DAO whether the logical kernel ownership token or compatible sidecar advisory lock is actually held. If held when a run starts or resumes, it does not poll or infer staleness; it halts immediately, reports the held lock metadata, and waits for human confirmation before anything touches that file. An unlocked persistent sidecar does not block resume.

Holding the lock means owning the entire file for that edit — nothing else may write, even to unrelated fields.

## P6. Conflicting data is never deleted — enforced via the conflict ledger

When two of the case's own sources disagree (dates, diagnoses, amounts, etc.), the finding agent does not halt inline and does not resolve it itself. It writes an entry to `_conflict_ledger.json` (one per case, shared across every stage that can raise a conflict) via the DAO: the field/topic, both values with full source attribution (document_id, page, quote), and `verdict: pending`.

Before starting any stage, the orchestrator checks `check_conflicts_clear(case_id)` — if any entry across the whole case is still `pending` (old or new), it halts and lists all of them, not just the newest. A stage only proceeds once every entry for the case reads `resolved`, `false_positive`, or `deferred_to_report`. Values are never silently discarded to resolve a conflict — the ledger entry, not a deleted value, is how a conflict gets closed.

**Three dispositions, and only two of them are decisions.** `resolved` means somebody worked out which reading holds; `false_positive` means there was never a disagreement. `deferred_to_report` means neither: the disagreement is real, stands un-withdrawn, no value has been preferred, and the decision itself is handed to the loss adjuster or physician by carrying the conflict into the screening report with its sources. Use it when settling the question needs domain judgement or a document the case does not contain — which is most of the interesting ones. It is not a softer `resolved`: `finalize-stage screening_report` refuses unless every deferred entry appears in the report's `inconsistencies` with `conflict_ref` set to that `conflict_id`. Deferring is taking on an obligation, and the obligation is checked.

Added 2026-08-18. Before it, CASE_047's four billing-vs-clinical disagreements (implant identity, injury laterality, treated body part, psychiatric onset) were filed as `resolved` because that was the only value that let the pipeline advance, and every `resolution_note` had to state in prose that the verdict field did not mean what it said. The screening report then opened its inconsistency section with a warning box explaining that the ledger's own verdicts should not be believed. A verdict that must be contradicted by its own note is a missing enum value, not a judgement call.

This is enforced structurally as well as by the orchestrator: `finalize-stage` refuses any stage in `dao.CONFLICT_GATED_STAGES` while an entry is `pending`. `consistency_check` is deliberately NOT gated — it is the stage that raises conflicts, so gating it would stop it finalizing the finding it just recorded — and neither are `intake`/`document_processing`, which run before any cross-document comparison exists. Until 2026-08-05 the gate was prose only: `check-conflicts-clear` existed as a CLI command an agent had to remember to call, and the finalize path never read the ledger, so `screening_report` finalized cleanly on CASE_909 with a conflict still pending.

Note: this rule is for the case's *own* sources contradicting each other. An insurer's claim disagreeing with the case's evidence (Phase 2's denial-validation) is a different kind of comparison — that's the stage's actual analytical purpose, not a conflict-ledger entry.

## P7. Human-review steps are never fabricated

> **OFF for the PoC (2026-08-20).** The wait-tracking field below is not
> maintained. The prohibition it serves — never synthesise a stand-in for
> a human decision — is NOT lifted and never was: it is enforced at each
> synchronous gate instead.

If a stage depends on human input that hasn't arrived, the pipeline waits — it never synthesizes a stand-in for a human decision. Waiting status is tracked as a field in the run-state file (P10): `human_input_status: waiting`, naming exactly which stage and what input is pending. The moment genuine human input is confirmed present — not merely claimed by an agent — the status flips to `received`. The field is never deleted, only updated in place, so the run's full history of what was waited on and when it cleared stays visible for as long as the run-state file exists.

## P8. Extraction failure is measured by cross-validation, not self-reported confidence

A single process, however many times it checks itself, can be confidently wrong. This runs at the document-processing stage (document-pipeline), before document type is known — not at claim-field extraction, which trusts this stage's already-validated text and does not re-cross-validate.

Every page is read independently by two configured provider paths (`tools/ocr_extract.py`): `reader_a` and `reader_b` are separate calls with no shared context, reading the raw page blind, neither seeing the other's output. Since document type isn't known yet at this point, the two reads are diffed on raw page-text material-content agreement (same names/dates/numbers/diagnoses present) — not verbatim match, since two independent transcriptions will differ in formatting even when both are correct. The selectable providers (openrouter / claude-cli / codex-cli / openai-api) are all LLM-vision-backed, so any reader pair is a documented weak P8 (`cross_validation_mode: single_technology_weak_p8_poc`) — two reads sharing one extraction technology class can make a correlated confident error. A genuinely technology-independent reader (a real OCR engine) is deferred; see `open-decisions.md` #4. (A plain-text source skips this entirely: it is decoded deterministically as embedded text, `deferred_poc`.)

A page/document is marked extraction-failed if the two independent reads materially disagree. This blocks that document from downstream use until a human verifies which reading is correct — a hard gate, not a queued-for-later flag, since everything downstream depends on this being right. Any single failure — even one document, even one page — is reported immediately. There is no tolerance threshold here; accuracy at extraction is the foundation everything downstream depends on, so nothing waits for a batch or a percentage to accumulate before surfacing.

**Non-text visual evidence is a separate human resolution, not a transcription shortcut.** If a blocked document consists entirely of photographs or other visual evidence with no faithful text transcription, a genuine human may resolve the extraction question as `non_text_image` instead of choosing reader A or B. The original disagreement remains recorded. No page text, image description, classification quote, redacted text, or text chunk is fabricated. The contract records `ocr_status: not_applicable`, `cross_validation_status: non_text_verified`, and `downstream_disposition: expert_review_only`; automated downstream agents must not use the raw image. This resolution never happens automatically and never applies to a mixed document that already has validated text pages — that requires a separate per-page design.

**Known open risk (deferred, not resolved), two parts:**
- Provider-based P8 does not automatically mean genuinely different extraction technologies. Every provider today is LLM-vision-backed, so no configurable pair is genuine dual-technology independence — record `single_technology_weak_p8_poc` and never describe two LLM reads as equivalent to a real OCR-engine-vs-vision-model pair. See `open-decisions.md` #4.
- The configured reader/comparator/classifier provider calls may see raw, unredacted page images or text before redaction. If the provider path is not under a no-data-retention arrangement, this is a real PII exposure gap. Not yet resolved — flag it, don't treat it as handled.

## P9. Partial or failed stages retry 3 times, then halt for audit

> **OFF for the PoC (2026-08-20).** Attempts are still counted and
> recorded; the automatic halt at 3 is not enforced.

A stage that fails or completes with `status: partial` does not proceed to the next stage — it retries, up to 3 attempts total, fixed (no exponential backoff). If, after 3 attempts, the stage still hasn't completed fully, the pipeline halts and requests a user audit — same hard-gate pattern as P4 (schema validation) and P8 (extraction cross-validation). Partial output is never silently forwarded or upgraded to "complete"; either the stage succeeds within 3 tries, or a human looks at it.

## P10. Run-state tracking and per-step backups

> **Half off for the PoC (2026-08-20).** Run-state tracking is unchanged.
> The cumulative snapshot is narrowed to the governed contracts — the
> per-document extraction artifacts (`ocr_result_*`, `redaction_result_*`,
> `classification_result_*`, `page_chunks*`) are excluded as regenerable
> bulk. See `dao.snapshot_excludes_name`.

Every run maintains a persistent run-state file (`outputs/CASE_XXX/_run_state.json`, provisional path per D4) recording every stage's status — `pending` / `in_progress` / `passed` / `failed` — with timestamps, plus the `human_input_status` field from P7. This file is the single source of truth for where the pipeline stopped; a crash or resume never has to guess or re-derive it from scattered output files.

After every stage passes validation, the orchestrator takes a full cumulative snapshot of all outputs produced so far — not just that stage's own delta — and retains it as a dedicated backup for that step (e.g. `outputs/CASE_XXX/_backups/step_<N>_<stage>/`). One snapshot per passed step, kept for the life of the run — never overwritten or rolled off. If a later stage crashes or produces contaminated output, restart reverts to the most recent intact step's snapshot instead of trying to salvage or reason about a half-corrupted state.

Small, cheap cost per step against a much more expensive failure: a pipeline crashing near the end and leaving the entire case's output contaminated with no clean point to resume from.

## P11. Medical review is a structural human gate, not a medical verdict

A medical referral requests narrow human interpretation. It is never a finding that care, diagnosis, causation, disability, coverage, denial, reduction, or final disposition is correct or incorrect. A/B/C contextual importance, anomaly severity, convergence, or anomaly count cannot authorize referral by itself. A request requires the approved decision route, mandatory source reinspection, one focused issue/question, a valid package, balanced supporting and countervailing evidence, and a closed evidence-locator set. OCR disagreement remains P8, schema failure remains P4, and contradictory case-source facts remain P6; none may be relabeled as medical uncertainty to bypass its own gate. Ground truth and final reports remain forbidden request inputs under D1.

All review-item creation, decisions, assignments, responses, amendments, withdrawals, cancellations, adjudications, closures, wait reconciliation, and lifecycle reads go through the medical-review DAO commands. The canonical clear/block check is:

```bash
python tools/dao.py check-medical-reviews-clear CASE_ID
```

Medical-review gating becomes applicable to a run as soon as claim analysis successfully publishes the canonical medical-variable revision. From then on, the command must exit successfully before `claim_analysis` may be marked `passed` or snapshotted, and the orchestrator must run it again immediately before dispatching every downstream stage. A nonzero result halts the run. A missing, unreadable, schema-invalid, or semantically invalid `_medical_review_ledger.json` is **not** an empty/clear ledger and fails closed. A stale or missing run-state wait projection cannot create clearance because this command reads the canonical ledger directly.

The schemas, ledger/state machine, CLI contract, and gate are harness capabilities, not permission to operate a real medical-review program. Disabled medical-structuring, referral-policy, request-vocabulary, or role-policy configuration remains disabled until its named medical/workflow/privacy approver activates a versioned policy. Do not create stand-in actors, thresholds, specialties, request codes, ledgers, or responses to make the gate pass. Pre-adoption legacy runs remain explicitly legacy and cannot be silently treated as medically screened or backfilled; their migration policy is still deferred.
