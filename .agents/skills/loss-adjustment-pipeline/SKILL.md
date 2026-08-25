---
name: loss-adjustment-pipeline
description: Orchestrator for the local Units 1-7 loss-adjustment harness. Use for case processing, reruns, screening/draft reports, and insurer-denial updates. Evaluation is deferred and unavailable locally.
---

# Loss-Adjustment Pipeline Orchestrator

Coordinates the authorized local pipeline across two phases to produce screening and draft reports through expert review. Evaluation is an unavailable external continuation that requires the deferred isolated Unit 11 service; never dispatch it or read ground truth locally. See `pipeline.md` for the full stage/agent table and I/O contracts.

**Execution mode: sub-agent and driver pipeline.** Agent-owned stages are dispatched as subagents; driver-owned stages run their named driver directly. In both cases, governed state passes through the DAO; agent return values carry only summaries and warnings, never contract data.

## Who runs this skill — check this before the first stage command

Everything below says "the orchestrator" does something. That word names a
**role with an agent behind it**, not whoever happens to have loaded this file.
Decide which of the two you are before running anything:

- **You are `pipeline-orchestrator`** (this text arrived as your system prompt,
  or you were dispatched as that agent) → you hold the role. Execute the
  contract below directly.
- **You are a main session, or any other agent** → you do **not** hold the
  role. Dispatch it and stop:
  `Agent(subagent_type="pipeline-orchestrator")` with the case, the run id, and
  the orchestrator-owned decisions the user has already made (a P8 reduction,
  a delegated gate and the name to record, run scope). Then wait for its
  report; do not run stage commands, `update-run-state`, `record-dispatch`, or
  `finalize-stage` yourself.

Loading this skill is not the same as holding the role, and reading a correct
procedure is not the same as being the right executor of it. A main session
that follows every gate below perfectly is still wrong if it never dispatched
the orchestrator: the T13 records then attribute the whole run to a session
that no `record-dispatch` covers, so the orchestration interval — the largest
single cost measured on this pipeline (Stage 2's 897s was 57% agent round
trips) — lands nowhere and the run's timing cannot be read honestly.

Origin, 2026-08-20 on CASE_700: the assistant loaded this skill, followed the
Phase 0 gates and the T13 lifecycle correctly, and dispatched `document-pipeline`
with a clean briefing — while never dispatching `pipeline-orchestrator`. Nothing
in the procedure caught it, because the procedure was not what was wrong. The
prose here ("the orchestrator alone calls...") reads naturally as addressing its
own reader, which is exactly how the role silently gets assumed rather than
assigned.

The narrow exception: a human operator driving one command by hand, or a
deliberate single-stage repair the user asked for by name. Neither is a case
processing request, and neither licenses running the full sequence in-session.

### What a dispatch briefing may contain

An agent's *procedure* already lives in its definition file, which is injected
as its system prompt. The briefing you write is only the part that definition
cannot know: which case, which run, and which decisions the orchestrator has
already made. Anything else you add is either redundant or harmful.

**Put in the briefing:**

- `case_id`, `run_id`, `held_by`, and the instruction to pass `--run-id` on
  every DAO call including reads.
- **Orchestrator-owned decisions the agent must not make or revisit** — a P8
  reduction (`HARNESS_SINGLE_READER` / `--on-disagreement`), a delegated human
  gate and the name to record for it, a scoping decision such as which
  documents are in scope.
- Where to resume, named as a checkpoint (`"checkpoint 1 is complete; start
  from checkpoint 2"`), and what to produce.
- The stop rule: report a blocked state and halt; do not work around it, patch
  tools, or investigate root causes in the code.

**Keep out of the briefing — this is the rule that gets broken:**

- **Contract state.** Never list per-document `document_type`, page counts,
  `ocr_status`, `cross_validation_status`, or any other value the agent can
  read from `document_manifest.json` and its contracts. Tell it what to do and
  let it read what is true. Handing it the state means (a) you cannot tell
  whether it read the DAO at all or just trusted your summary, (b) a stale
  briefing silently overrides current fact, and (c) a timed run stops paying
  the read cost a real run pays, so the SLA number measures a run nobody will
  ever perform.
- **Expected findings.** Never say what a document contains, what a bundle will
  split into, or what a classification should come out as — even from a
  previous run of the same source. An agent told a 19-page bundle holds "a
  진단서, two REPORTs, an 입퇴원확인서 and two 진료비 명세서" can produce exactly
  that partition without the page text supporting it, and nothing downstream
  can distinguish that from a real reading. This is the difference between
  dispatching a stage and dictating its answer.
- **Restatements of the agent's own definition.** Checkpoint order, DAO-only
  access, "do not call finalize-stage", schema-validation behaviour — all
  already in the spec. Repeating them creates a second copy that drifts (the
  2026-07-17 forbidden-expression shape) and makes it ambiguous which text
  governs when they disagree.

The test to apply before dispatching: **if the briefing were deleted and
replaced with "resume CASE_X from checkpoint N", would the agent still reach
the right answer?** If no, find what is missing and add only that. If a line
would merely save the agent a DAO read, cut it.

There is no standalone `run_pipeline.py` process. This skill is the executable
orchestration contract: a case-processing request must follow every gate below
in order. Calling a later tool directly bypasses orchestration and is not a
pipeline run.

**Canonical stage names** (enforced by `run_state.schema.json` v0.3 — any other spelling is rejected at write time; conflict-ledger `raised_by_stage` uses the same enum): `intake`, `document_processing`, `indexing`, `policy_clause_processing`, `claim_analysis`, `denial_response`, `consistency_check`, `screening_report`, `draft_report_v1`, `draft_report_v2`, `critic_v1`, `critic_v2`, `denial_validation`, `human_review_v1`, `human_review_v2`. Pass exactly these to every `--stage`/`update-run-state` call and require the same of every dispatched agent (each agent spec now pins its own). Adding a pipeline stage means adding it to the schema enum in the same change (D4).

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

**Note the wall time before you dispatch**, so the interval below is a
measurement and not a recollection.

After the agent returns, record the dispatch before finalizing. The harness
reports the subagent's duration, token counts and tool-call count on
completion; pass them through:

```text
python tools/dao.py record-dispatch CASE_ID --run-id RUN_ID --stage STAGE \
  --started-at ISO8601 --duration-s WALL --agent-reported-s REPORTED \
  --agent-kind AGENT --attempt N \
  --input-tokens IN --output-tokens OUT --total-tokens TOTAL --tool-uses USES
```

Copy the harness's figures; never estimate one. Omit a flag you were not given
— an omitted count records as "not measured", while a guessed one is
indistinguishable from a real reading. This is diagnostic like the rest of the
timing layer: a failure here never blocks the stage.

**If the dispatch stopped for a human — a permission prompt, a gate answer —
pass `--human-wait-s`.** It is subtracted before any rate is computed and
emitted as a `human_wait` span, so the SLA's existing subtraction applies. A
prompt raised *inside* a dispatch produces no span on its own: on CASE_027's
`denial_response` the operator took 506.2s of an 862.2s dispatch to answer, the
summary read `human_wait_s: 0.0`, and the stage reported **170 tok/s for work
that actually ran at 411**. Recover the interval from the trace when you did
not time it directly — the gap sits between two adjacent tool spans:

```text
python tools/dao.py read-timing-summary CASE_ID
```

It matters because **tool spans do not explain an agent stage**. On CASE_022,
`claim_analysis` spent 1.20s in tools across 773.0s of wall (0.15%), while
token volume tracked wall time closely (277 tok/s; `denial_response` 327).
Without the counts, the only available reading of the remainder is
`unattributed_active_s`, which T13 states is *not* a claim about model
reasoning — and misreading it that way is what produced a document index that
optimized a 0.15-second lookup.

Then the orchestrator alone calls:

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

**Stage 2 runs as one command, not as a sequence you supervise.** Dispatch
`document-pipeline` to run `python tools/run_stage2.py CASE_ID --held-by
document-pipeline --run-id RUN_ID`, which performs every
mechanical step (checkpoint 1 → checkpoint 2 → segmentation → child
classification → child redaction → chunking → contract write) and stops at the
four real gates. Stage 2 is driven from code because its checkpoint control is
deterministic and its real human gates stay explicit. Do not ask an agent to
invoke the individual checkpoint tools in sequence — that is the serialization
this removes. The driver never moves a run-state marker: `update-run-state` and
`finalize-stage` stay yours (T13).

**Claim analysis runs as one driver, not as an agent dispatch.** Run
`python tools/run_claim_analysis_selective.py CASE_ID --held-by claim-analysis
--run-id RUN_ID --provider PROVIDER` after the ordinary gates and an
orchestrator-owned `claim_analysis in_progress` transition. It reads documents
per field in priority order, stopping at the first trusted value, validates
every citation through the DAO, publishes `claim_analysis_result.json` and
`claim_analysis_trace.json`, and never finalizes the stage. Do not dispatch
`claim-analysis` for this work. Before finalizing, the orchestrator still runs
`check-medical-reviews-clear` after canonical medical variables publish, as
required by the gate.

**Three document types decide a liability case, and Stage 2 must type them.**
`legal_opinion` (법률의견서 -- the reasoned answer, whichever side commissioned
it), `legal_reference` (published material a party attached: 위자료 산정기준표,
노동능력상실률 표) and `insurer_response` (the insurer's covering letter, which
may cite an opinion) were added to the document taxonomy 2026-08-21. Before
that the classifier read their titles correctly at 0.90-0.95 confidence and had
no bucket but `other`, so claim analysis skipped them entirely with "no medical
classification, so no route reaches it" -- 24 pages of CASE_053's central
evidence. A case whose 법률의견서 are typed `other` will still run and still
produce a liability verdict; it will simply produce the wrong one, silently. If
a run's manifest shows `other` on a document whose title names a legal opinion,
that is a Stage 2 classification defect to report, not something to work around.

**There is only one claim-analysis driver.** The legacy read-everything spine
(`run_claim_analysis.py`) was **deleted 2026-08-20** — do not look for it, and
do not treat the selective lane as conditional on a flag. The two spines wrote
different contracts, and only the selective one's are consumed: stages 6 and 7
read `claim_analysis_result.json` and nothing else. `behavior_enabled` is
retained in the routing config as a governance record (it carries the approval
block), and turning it off now HALTS the stage rather than selecting another
path.

**Do not cite an exact Stage 2 speedup ratio.** Earlier revisions of this skill
quoted `897s` agent-led against `79s` driven, with `~510s` of model round trips.
That is a **historical observation, not reproducible from retained timing
records**: CASE_911's closed agent-led `document_processing` attempt of 897.4s is
real and DAO-verifiable, but no retained trace provides a closed, cold,
input-equivalent driver arm, and no dispatch-boundary instrumentation exists to
recover per-decision-round timing. A future performance claim requires a
controlled cold A/B and that instrumentation.

**Policy UID preflight runs before its attempt, not inside it.** Before opening
`policy_clause_processing`, dispatch `python tools/run_policy_preflight.py
CASE_ID --held-by orchestrator --run-id RUN_ID`. It performs only the existing
digest/UID-enable DAO sequence for in-scope noncanonical policy documents and
may invalidate stale artifacts while no policy attempt is open. A nonzero
result is a blocked precondition: do not open or dispatch the policy stage.

Skipping the preflight is not a shortcut — it moves the invalidation INSIDE the
attempt. On CASE_142 it fired at 09:34:09 while the stage was `in_progress`,
demoting the stage mid-run and forcing a second attempt.

**Do not dispatch the policy agent. Run the driver.** Clause normalization is
retired (2026-08-15), so every policy document is `text_only_no_normalization`
and the stage has no extraction work:

```text
python tools/run_policy_pipeline_driver.py CASE_ID --held-by orchestrator --run-id RUN_ID
```

Then finalize through the ordinary T13 path. The driver records the manifest
fingerprint and returns a no-op result; it BLOCKS if a normalized policy
document somehow exists, which is a real precondition failure to report, not a
reason to fall back to dispatching the agent.

This is worth stating as a rule because dispatching cost real time for no work.
Measured on CASE_142: the stage's two attempts spanned **370.9s** of which the
DAO did **0.69s** (finalize + snapshot + locks), with zero provider calls and
zero output files. The span timeline shows 75s and 91s gaps containing no tool
activity at all — an agent reading the case to conclude there was nothing to do.
The manifest states that outright, so the orchestrator reads it instead.

**Reducing P8 for a throughput run is YOUR decision, never an agent's.**
Stage 2's cost is dominated by dual-read OCR (the corpus is overwhelmingly
scans), so two flags exist on `run_document_stage.py` (and pass through
`run_stage2.py`) to cut it. Pass one only
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

**OCR and redact the bundle first.** A governed `propose` requires complete redacted processed text plus a P8-cleared OCR result; it must fail closed rather than fall back to raw-PDF vision. The deterministic title path is both better and cheaper, and it is the only normal pipeline path for a `required` bundle:

1. `run_checkpoint1.py CASE_ID BUNDLE_ID <pdf> --bundle-ocr` — OCR only, no classification (`document_type` is per-document; one label cannot be right for a bundle).
2. `redact_document.py CASE_ID BUNDLE_ID` — segmentation reads the REDACTED text, so pre-redaction page text never leaves the capability gate. Titles survive redaction (verified on CASE_112's 217 split children), so nothing needed for boundaries is lost.
3. `segment_case.py propose` → `approve` → `split`. The split hands each child the bundle's pages, renumbered from 1 with P8 verdicts intact, so **children are not re-OCR'd**.
4. Classify each child with `run_checkpoint1.py classify-only` — **never `run`**, which begins by OCR'ing and would re-read pages the child just inherited, replacing their P8 history (CASE_909's DOC_006-013 lost theirs exactly that way). A child whose approved title names a form takes its `document_type` from that title with no model call; a genre-only title like `REPORT` falls through to the classifier. Then redact each child.

Measured on CASE_908/DOC_005, a 19-page scan with zero embedded text: precision 1.0000 / recall 0.9091 from the title rule alone, and 1.0000/1.0000 with the LLM tier on the 5 pages (26%) it could not settle. Boundaries from redacted OCR text were byte-identical to those from the embedded layer on CASE_112's 323 policy pages.

Titles are the boundary signal on both paths — `...보통약관`/`...특별약관`/`...특약` for policy, form names (진단서, 수 술 기 록, 진료비 세부산정내역) for medical, matched through a 5-line header window because a statutory header or field labels often precede the title. A page the deterministic rule cannot settle — a generic heading like `REPORT`, or no title at all — goes to the LLM tier, which reads that page and the one before it. An unusable verdict splits and is flagged for review: over-splitting is undone by a human merge at the approval gate, over-merging fuses two documents into one `document_type` and propagates downstream.

The contact-sheet/vision code is retained only as a diagnostic tuning seam; it cannot create an approvable governed proposal. A saturated or unresolved `needs_full_page` result is not split-ready: retune the crop/grid and create a new proposal instead of increasing the call cap to paper over the failure.

**The human gate is the boundary approval**, and it is the only one: `split` refuses until a human approves the `segmentation_proposal_{DOC}.json` (case-level `approved` AND every segment `approved`/`edited` AND no unassigned page). A reviewer sees the proposed ranges and the title line each cut is made on — real evidence — instead of answering "is this a bundle?" from a filename before anything has been read. `run_checkpoint1.py` still refuses one thing before any provider or PDF work: a retained `superseded_bundle`, since reading it again would duplicate every page its children already own.

**A policy bundle is deliberately NOT split** — approve it as a single segment even though it plainly concatenates dozens of 약관. Korean policy clauses print their owning 약관 in the clause body (`회사는 '물적손해 확장 추가특별약관' 제2조...`), so downstream identification runs off `clause_id` text, not the PDF the clause happens to sit in -- CASE_021's four policy matches distinguished 삼성화재/KB/한화 while all citing one `document_id`. Splitting is pure cost: measured on CASE_905's two bundles, split produced 197 documents / 176 classification calls / 39.8 minutes against 5 documents / 5 calls / ~40 seconds unsplit, and `chunk_text` re-splits to page granularity either way, so both arms ended at the identical 323 page chunks. This exception is specific to policy bundles: a MEDICAL bundle (진단서 + 검사보고서 + 입퇴원확인서 + 진료비명세서 scanned into one PDF, as CASE_907's DOC_005) still wants splitting, because `document_type` is per-document and one label cannot be right for all of them.

   Records under the `document_processing` stage — segmentation is one of its checkpoints, not a stage of its own. Segmentation's own output is document STRUCTURE, not text or type: `document-pipeline` still owns real classification, and a `provisional_type_label` is never copied into `document_type`.
3. **Conflict-ledger check**: before dispatching *any* stage, call `check_conflicts_clear(case_id)`. If not clear, halt and report every pending entry (old and new) — do not proceed past an unresolved conflict, no matter which stage raised it.

   Halting is not the only disposition. A `pending` entry needs a human, but most conflicts worth raising cannot be settled from the case file at all — they need a physician's reading, or a document nobody has. For those, record `deferred_to_report`: `python tools/dao.py set-conflict-verdict CASE_ID CONFLICT_N deferred_to_report --note "why this needs a human and what would settle it" --held-by NAME --run-id RUN --operation-id ...`. The disagreement stays open and un-withdrawn, the pipeline proceeds, and the conflict must then be carried into the screening report — `finalize-stage screening_report` refuses unless every deferred id appears in the report's `inconsistencies` with a matching `conflict_ref`. Pass the deferred ids from `check_conflicts_clear`'s `deferred_to_report` list into the screening-report dispatch so the agent knows what it must carry. Do NOT reach for `resolved` to unblock a run: that is what happened on CASE_047, where four real disagreements were marked resolved and every note had to explain that the verdict was not to be believed.
4. **Lock check**: at run start/resume, ask the DAO — `python tools/dao.py check-lock CASE_ID TARGET_FILENAME` — rather than looking for a lock file yourself; the lock is the DAO's to interpret and its on-disk shape is not an agent-facing contract. If a lock is held, do not poll and do not assume it is stale — halt, report the lock's full contents, and wait for human confirmation (P5).
5. **Medical-clearance check**: after canonical medical variables have been published, call `python tools/dao.py check-medical-reviews-clear CASE_ID` immediately before every downstream agent dispatch. Halt while it reports blocked. The DAO independently repeats this check before downstream `in_progress`/`passed` transitions and snapshots.
6. **Begin the stage attempt** using the T13 lifecycle command above, noting the wall time, then dispatch. Never infer an attempt start from the first output write.
7. **Record the dispatch** with `record-dispatch` when the agent returns, passing the harness's reported duration, token counts and tool-use count (T13 lifecycle above). Before `finalize-stage`, not after — finalize closes the attempt.

## Phase 1 — initial claim review

| # | Stage | Agent | Internal checkpoints |
|---|---|---|---|
| 1 | Case Intake | (orchestrator + `intake_case.py`) | D2-gated `_source_ledger.json` — the one intake decision is raw vs ground_truth, not bundle vs single document. Records under `intake` |
| 2 | Document Processing | `document-pipeline` | (a) bundle OCR (`--bundle-ocr`, no classification) → (b) bundle redaction → (c) **segmentation**: propose/approve/split, children inherit the bundle's pages → (d) per-child classification → (e) per-child redaction → (f) case-wide chunking. All under `document_processing`; segmentation sits *inside* because processing runs on both sides of it |
| 3 | Indexing (adapter, optional) | (tool, no agent) | pass-through by default; no-op unless enabled |
| 4 | Policy Clause Processing | (driver, no agent) | `run_policy_pipeline_driver.py` after the UID preflight. Normalization retired 2026-08-15, so there is no extraction to delegate; the driver records the manifest fingerprint and the orchestrator finalizes. Policy text stays fully processed, chunked and citable |
| 5 | Claim Analysis | (driver, no agent) | `run_claim_analysis_selective.py` — the source-grounded selective spine, and since 2026-08-20 the **only** claim-analysis driver (the legacy `run_claim_analysis.py` was deleted; there is no flag to choose between them). Reading is **demand-driven**: each round asks only the still-unresolved fields which document they need next, batches everyone wanting the same one into a single call, and a field that finds a trusted value drops out, so its lower-priority sources are never opened (a document is still read at most once per RUN). It publishes `authority: source_document_extraction` with `medical_projection_status: not_configured` (no canonical projection, and **no canonical medical revision required**), links clauses from `_document_index.json` (**optional** — no index means `policy_links` come back `not_found` with a reason, not a blocked stage), reading only the candidate clauses’ own pages rather than the whole 약관 bundle; case-type assessment runs BEFORE clause linking, so a type-specific coverage term (배상책임의 시설소유/구내치료비) is searched for even when the medical fact terms find nothing; leaves the industrial filing/approval fields `unavailable`/`outside_poc_scope` because **no stage in this pipeline produces a filing declaration** (deferred — filing status therefore stays `unknown` on every case, and is never inferred from how the accident happened); and records conflict *candidates* only — it never writes the P6 ledger. Run medical clearance before pass/snapshot; on this lane the gate applies only when a canonical revision exists: see §8 precedence in `dao.source_grounded_lane_only`, and never record the carve-out as cleared/approved. **Stage 3-a (type-conditional round), added 2026-08-21:** after the case-type verdicts, the driver re-opens fields the common pass left `unavailable` from NON-medical sources named in `additional_fields_by_case_type` -- `legal_opinion` (법률의견서) and `insurer_response`. It exists because every medical route ranks medical form kinds only, so on CASE_053 the one field liability is judged on came back `unavailable` while two 법률의견서 in the same case argued that exact question. A field the common pass already answered is never re-opened, so a 진료기록's reading is never overwritten and a case whose medical records covered everything pays nothing here. Two opposing opinions become a `conflict` candidate and the type still reads `applicable` with '성립 여부 자체가 쟁점' -- an opinion finding 불성립 never closes a type, because that is one side's position on the disputed question. Cost is reported separately in `type_conditional_documents_read`/`_provider_calls`; do not fold it into the common pass's counts. |
| 6 | Consistency Check | `consistency-check` | conflict-ledger-gated — any disagreement halts via `_conflict_ledger.json`. On the selective lane this stage is **agent-owned with a deterministic helper on both sides**: `run_consistency_check.py prepare` builds work items carrying both readings and their evidence (no verdict field), the agent judges each `confirmed`/`not_material`/`withdrawn` and writes a neutral `professional_summary` on every confirmed one, then `run_consistency_check.py register` verifies the verdicts against what was prepared (candidate digest) and creates the entries. Every entry is `pending`; never auto-`deferred_to_report` **The judgement can also run in code**: `tools/run_consistency_check.py judge CASE_ID --held-by consistency-check --run-id RUN_ID` reaches the same verdicts through one bounded provider call (OpenRouter by default) and writes the same `consistency_check_verdicts.json`. `register` is unchanged either way and still runs its binding checks, so the agent and the driver are interchangeable producers of that contract; both routes exist until they have been compared on a real case. |
| 7 | Screening Report | `screening-report` | consumes `denial-response`'s output as a dependency if an insurer-response document exists — not phase-gated. On the selective lane the agent supplies only `key_issues`, `review_points`, and per-conflict severity/placement (via `screening_report_judgement.json`); `run_screening_report.py` then produces all three artifacts — `screening_report.json`, `screening_report.md`, and the evidence sidecar — rendering the narrative through `document_assembly.py --template screening_report_selective` (nine sections). It produces **no** 진행 가능성/난이도/지급 가능성, and copies a deferred conflict's `professional_summary` verbatim from the ledger |
| 8 | Draft Report v1 | `draft-report` | same agent reused for v2 in Phase 2 |
| 9 | Critic Pass (v1) | `critic` | blind — never touches ground truth |
| 10 | Expert Review v1 / external handoff | human-owned | completes the local v1 boundary; Evaluation remains unavailable |

`denial-response` is **not** a numbered Phase 1 stage — it's dependency-triggered. It runs whenever a flagged insurer-response document's processed text (from stage 2) is ready, whether that happens to be during Phase 1 (closed-case packs that bundle the insurer notice from the start) or later. Same agent, same mechanism, no phase-based scheduling exception needed.

**Readiness lanes:** Phase labels do not serialize graph-independent work. After
document processing, policy preflight → `policy_clause_processing` and an
eligible `denial_response` may be dispatched concurrently, after separate
preflights and T13 attempt opens. `denial_validation` starts as soon as both
`denial_response` and `consistency_check` pass; it may overlap the independent
v1 draft/critic lane. Do not open publishable `screening_report` while an
insurer-response input exists but `denial_reason_result.json` is absent. Each
concurrent member retains its own locks, attempt boundary, result handling, and
downstream gate; a phase label is never a reason to delay a ready stage.

**The document index is an OPTIONAL input to claim analysis.** It reads the
index with `allow_missing`, and a case without one simply produces
`policy_links` whose status is `not_found`, each carrying the reason. The stage
completes normally: a case with no processed policy has no clause to link, and
that is the honest result rather than a failure. It is therefore never a
precondition to check before running this stage. (The deleted legacy driver
hard-gated on it, which is why older notes describe a blocked stage.)

The policy driver writes `_document_index.json` -- every article in the case's
policy documents under `clauses` (`{page, policy_name, article, heading}`) with
its page and owning 약관, plus any table whose row/column structure was
recovered from the PDF under `tables`. Confirm it is there
(`dao.py read-document-index CASE_ID --run-id RUN_ID`) before the legacy lane;
`denial-response` and `critic` should be told in their briefing to start clause
lookup from it rather than scanning chunks. If the read returns `NOT_FOUND`, do
not dispatch those agents with a promise that the file exists.

The index is a **derived** artifact, not a contract: `build-document-index`
writes it under the lock but outside `write-contract`, nothing gates on it, and
it is recomputable from processed text. That is what makes an optional
dependency on it coherent.

For agent briefings this is one of the few tool-availability facts that belongs
in a briefing. The rule about keeping contract values out is unchanged: never
list document types, page counts, or what the index contains.

**It is not a gate and must not become one.** Nothing blocks on the index and
no contract references it; an agent without one falls back to
`search-document-text` and the chunks. Making it required would recreate what
killed clause normalization -- an obligation nothing could reliably discharge,
bypassed rather than met. But an advisory artifact nobody is told about is the
*other* failure this project keeps hitting (`raw_page_text`,
`medical_review_adopted`: written by several call sites, read by none), so
naming it in the briefing is what keeps it from being a file that exists and
goes unused.

**Claim-analysis medical gate**: after the agent publishes its evidence-derived candidate with `write-medical-variables`, do not mark `claim_analysis` passed or call `snapshot-backup` until `python tools/dao.py check-medical-reviews-clear CASE_ID` succeeds. The DAO also rejects both transitions without clearance. The orchestrator does not open review items, choose referral policy, or stand in for a human decision.

**Between stage 9 and the external handoff**: once `critic` passes, call `dao.py request-expert-review CASE_ID {v1|v2}` to mark `human_input_status: waiting` (P7) and hand the reviewed draft + `critic_result_v{version}.json` to a genuine human reviewer. Once validated human-owned review content exists, the human-only `dao.py mark-human-review-complete CASE_ID {v1|v2} --reviewer NAME` records the future Unit 11 handoff prerequisite. It does not enable local Evaluation or ground-truth access.

## Phase 2 — insurer denial/reduction response

Only two genuinely new stages — everything else is Phase 1's agents reused on new input.

`draft_report_v2` is a strict join: `draft_report_v1`, `critic_v1`, and
`denial_validation` must all be passed. The critic dependency is correctness,
not a reason to delay the earlier denial-validation dispatch.

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
| Schema validation fails twice (P4) | Halt, present retry-N-times / fix-manually / abandon-run to the user |
| Stage returns `partial` or fails (P9) | Close the current attempt with `--attempt-outcome partial` or `failed`, then retry from its last internal checkpoint up to 3 explicit dispatch attempts; after 3, halt for user audit |
| Conflict-ledger has any `pending` entry (P6) | Halt before dispatching the next stage, list all pending entries |
| Extraction cross-validation disagrees (P8) | Halt immediately, no tolerance threshold, even for one field on one document |
| Human input pending (P7) | Wait — `human_input_status` in `_run_state.json` (written via `dao.py set-human-input-status`/`request-expert-review`) shows exactly what's pending; never fabricate a stand-in, and never call `mark-human-review-complete` yourself |
| Any local ground-truth or Evaluation attempt (D1) | Halt immediately; the deferred isolated service is unavailable |

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

At the end of a run (or when halted), report to the user: per-stage pass/fail/pending status from `_run_state.json`, validation PASS/FAIL/SKIP tally, `review_required` count and routing (손사/의사), any partial/warning list, and next actions (e.g. awaiting human review). Ask for feedback — this harness evolves from it, see `CHANGELOG.md`.
