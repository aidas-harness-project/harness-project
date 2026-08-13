# Driverization Plan

## Purpose

Replace high-level agent-led tool choreography with small Python drivers where
the execution route is already fixed. A driver owns DAO reads, checkpoint
selection, prompt construction, schema validation, evidence-address checks,
resumption, and DAO writes. A provider call remains only for a bounded
semantic judgement. The orchestrator continues to own the stage-attempt
lifecycle (`update-run-state` and `finalize-stage`).

This plan does not weaken P1/P3/P4/P5/P6/P7/P8/D1/D2. In particular, drivers
must stop at human gates, preserve P6 conflicts rather than decide them, and
use the DAO for all case-state reads and writes.

**Measurement addendum:** `plans/driverization/case135-measurement-replan.md`
supersedes this document's delivery order and any implication that every stage
should become a full driver. CASE_135 showed a flat agent-cost distribution:
shared deterministic leaves, race-free inter-stage parallelism, and targeted
claim fan-out come before semantic-stage wrappers.

## Common Driver Contract

Every driver will:

1. Read current state through the DAO, including dependencies, conflict gate,
   manifest, and its existing checkpoint contracts.
2. Resume from the first missing or invalid checkpoint. It must not overwrite
   a valid completed checkpoint merely because the process restarted.
3. Batch its input reads once per checkpoint and build a minimal,
   case-scoped input bundle outside `data/` and `outputs/`.
4. Invoke the provider with a checkpoint-specific JSON Schema. The Codex CLI
   adapter must pass its existing `output_schema` argument to the CLI's native
   `--output-schema` option before it is used for a driver.
5. Run deterministic validation before each DAO write: JSON Schema, quoted
   evidence present on the cited processed page, valid document/page identity,
   and any contract-specific DAO validation.
6. Make exactly one bounded correction call after a schema/shape failure;
   then return a blocked failure for the orchestrator's P4 handling.
7. Use temporary files only outside governed case directories; publish final
   contracts only via `dao.py write-contract`.
8. Return a compact progress report and never move a stage attempt boundary.

## Workstream A: Claim Analysis

### Target

Create `tools/run_claim_analysis.py` with the existing four checkpoint
contracts. Initial execution is serial because each checkpoint consumes the
previous checkpoint's validated output.

| Checkpoint | Provider judgement | Deterministic driver responsibility |
|---|---|---|
| 1. Claim fields | Extract facts from supplied redacted text; distinguish case facts from cited precedent or unrelated material | Select relevant processed text, deduplicate it, verify every quote, normalize fixed date/period shapes, write `extracted_claim_fields.json` |
| 2. Coverage | Match facts to policy coverage and decide applicability | Select policy excerpts, obtain a fresh policy snapshot, validate source/UID clause references, write `coverage_result.json` |
| 3. Case type | Infer axes only where no adjuster input exists; perform the independent cross-check where it does | Read `adjuster_case_type`, deterministically select canonical `template_id` from `templates/registry.json`, validate case-type shape, write `case_type_result.json` |
| 4. Requirement matching | Identify policy conditions and determine `met`, `not_met`, or `uncertain` | Preserve checkpoint-2 coverage joins, deterministically assign stable `REQ-N` identifiers and order, validate policy references/snapshot, write `requirement_matching_result.json` |

### Non-goals

- Do not use regexes to make medical, legal, causation, coverage, or
  applicability determinations.
- Do not derive a case type from a filename or override a supplied adjuster
  type.
- Do not treat a missing accident date after redaction as a value; represent it
  as absent/uncertain with the required review routing.

### Tests and measurement

1. Add unit tests for resume after each of the four contracts.
2. Add tests for quote mismatch, stale policy snapshot, invalid source/UID
   reference, and exactly-one correction behavior.
3. Verify the CLI adapter passes `--output-schema` and fails closed when the
   provider returns invalid JSON.
4. Compare one cold, identical-case driver run with an agent-led baseline;
   report provider time, DAO/tool time, driver time, and unattributed time.
5. Only after semantic equivalence is established, assess parallel field
   extraction within checkpoint 1. Checkpoints themselves remain serial.

## Workstream B: Consistency Check

### Assessment

Consistency check is a good **hybrid-driver** candidate, but is less suitable
than claim analysis for pure deterministic conversion. Its route and audit
mechanics are deterministic; its central decision is not.

The driver can remove repeated state reads, repeated document lookup, manual
quote/page verification, manual `CHK-N` and conflict bookkeeping, and
checkpoint orchestration. It cannot safely replace the semantic decision of
whether two statements address the same real-world fact and whether their
difference is a P6 factual contradiction rather than competing legal
argument. That decision must remain in a bounded provider call and be made
against the supplied sources only.

### Observed CASE_135 execution evidence

The agent-led CASE_135 consistency-check log completed the intended P6 path:
it wrote two pending ledger entries, wrote a schema-valid
`evidence_validation_result.json`, and left stage finalization to the
orchestrator. It also demonstrates the driver opportunity: the execution
repeated state/contract/document discovery, debugged command and console
encoding issues, assembled scratch scripts, checked schemas immediately
before writing, and manually correlated ledger IDs back into audit checks.
Those activities do not require open-ended agent planning.

The same log records direct reads of `data/processed/.../redacted_text.md`
after a DAO command returned a path, and a scratch verifier that opens those
paths directly. That route is not permitted by the repository's DAO-only
rule, even though it is the processed rather than raw layer. The driver must
instead capture text from DAO read commands (or add a DAO batch-read command)
and must never expose an on-disk processed path as a driver read interface.

The semantic boundary is visible in the two raised conflicts: deciding whether
the phrase about a narrow stair and the phrase about a large facility are
competing assertions about one fact, and whether claimant carelessness is a
factual assertion rather than a legal conclusion, cannot be reduced to a
safe fixed rule. The resulting P6 entries remain human-resolved exactly as
the guardrails require.

### Target

Create `tools/run_consistency_check.py` after Workstream A proves the shared
provider/JSON infrastructure. The driver runs one semantic comparison pass and
then writes the audit trail mechanically.

| Phase | Provider judgement | Deterministic driver responsibility |
|---|---|---|
| Input assembly | None | Read the five required upstream contracts and eligible redacted text/page chunks once; exclude superseded bundles; build a source-indexed evidence bundle |
| Candidate review | Decide same-fact relation; distinguish factual assertion from legal conclusion; identify unresolved internal contradiction | Give the model a closed candidate inventory and require `consistent`, `inconsistent`, or `not_comparable` with two attributable quotes |
| Ledger publication | None | Verify every quote/page, deterministically deduplicate equivalent conflicts, submit each unresolved conflict with `add-conflict-entry`, capture returned IDs |
| Audit publication | None | Assign stable `CHK-N` ordering, map returned conflict IDs to inconsistent checks, emit `evidence_validation_result.json` via DAO |

### Candidate inventory

The first version should derive candidates from structured claim-field slots
(dates, diagnoses, treatment periods, accident location/time/circumstance,
claim amount) plus a controlled extraction pass over designated
accident/medical documents. It must not assume that every phrase occurring in
an insurer response is a case fact: quoted precedent, legal opinion, and
adversarial assertion need source/context classification before comparison.

`not_comparable` is internal driver/provenance state, not a published schema
result. It prevents the driver from manufacturing a `consistent` finding where
two statements are about different units, people, events, or cited precedent.
Only actually performed comparisons are published; in the current PoC schema,
published entries are `consistent` or `inconsistent`.

### Hard stops

- If a model calls a statement a factual conflict, the driver must not resolve
  it. It publishes a P6 ledger entry with `verdict: pending` and lets the
  next-stage gate halt the case.
- If source provenance or quote verification fails, do not create a conflict
  entry and do not publish a purported audit result.
- Do not use prior-case adjudications as facts about the current case. They
  may only inform test fixtures or documented policy, never a case decision.
- Do not fold insurer-vs-evidence merits analysis into this stage; that belongs
  to denial validation.

### Tests and measurement

1. Test consistent values, genuine factual conflict, legal-opinion-only
   disagreement, quoted-precedent contamination, and different-unit
   statements.
2. Test idempotent resume: rerun must reuse existing matching ledger entries
   and not append duplicates.
3. Test a ledger write failure leaves no `inconsistent` audit entry pointing
   to a nonexistent conflict ID.
4. Measure one cold run against the agent-led baseline, separately reporting
   input assembly, provider judgement, quote verification, ledger writes, and
   audit write.

## Superseded delivery order

The following older order is retained for design history only. Do not execute
it as a roadmap; use `case135-measurement-replan.md` instead. In particular,
consistency and denial-response wrappers are later semantic pilots, whereas
the actual first changes are split-child validation, canonical policy
preflight, complete timing spans, the clean critic/denial-validation lane, and
the shared evidence/snapshot leaves.

No production-stage routing changes occur until the corresponding tests and
one measured comparison pass.

## Workstream C: Denial Response

### Assessment

Denial response is a strong hybrid-driver and parallelization candidate. The
stage graph already allows it to start after `document_processing`, in
parallel with `policy_clause_processing`; it is not dependent on
`claim_analysis` or `consistency_check`. The driver must keep this early
extraction path: lack of a passed policy stage may prevent a policy match, but
must not prevent extraction of the insurer's stated decision, grounds,
amounts, and acceptance(s).

### Observed CASE_135 execution evidence

The supplied partial CASE_135 log shows repeated discovery that a single
source bundle was segmented into DOC_007, DOC_008, and DOC_009. A
document-ID-per-worker design would therefore over-count one decision and
create duplicate denial reasons. The unit of parallel work must be an
independent source decision/response bundle, not a manifest document.

Checked against the manifest on 2026-08-13 rather than left as recollection:
the three are pages 1, 2-12, and 13-15 of one `source_file_name`
(`DOC_002.pdf`). Two details this section previously got wrong or omitted —
`source_document_id` is `null` on all three (so it is not the join key), and
DOC_009 is typed `other`, not `insurer_response`, so selecting work by
document type alone silently drops the last three pages of the notice. See
`plans/driverization/denial-response.md` for the grouping rule.

The same log also repeats case-history and policy exploration, searches for a
conclusion across page boundaries, and performs terminal/encoding recovery.
Those are driver-owned input assembly and verification tasks, not semantic
decision work. The log is partial and has no reliable elapsed timings, so it
is evidence of an avoidable route, not yet a measured speedup claim.

The completed CASE_135 log confirms this design rather than changing it. One
DOC_007–009 response bundle yielded four distinct denial decisions and one
accepted coverage. The agent first made incorrect page attributions and then
searched/corrected them before publication. It also found that all three
policy matches were `agent_inferred`, not insurer-cited; the insurer named no
policy clause. DOC_011 was usable through source-form page+quote references
despite being `text_only_no_normalization`, and no promotion was needed. A
driver must therefore resolve/verify page locations mechanically before a
result reaches the model merge, preserve `agent_inferred` review routing, and
never promote merely because a source-form match exists.

### Target

Create `tools/run_denial_response.py`. Its provider calls should be limited to
classifying insurer-stated material; the driver owns grouping, identifiers,
evidence checks, cross-result merge, policy snapshot, and publication of
`denial_reason_result.json`.

| Phase | Parallelism | Provider judgement | Deterministic driver responsibility |
|---|---|---|---|
| Response inventory | Serial, inexpensive | None | Read manifest through DAO; select insurer-response documents and group sibling segments by their original source bundle/decision artifact |
| Decision extraction | One worker per independent response bundle | Split materially distinct insurer decisions; identify decided/accepted coverages, stated grounds and stated amounts | Supply only redacted source text and stable source/page labels; reject duplicate/missing evidence addresses |
| Reason enrichment | Bounded parallelism per unique decision | Assign R-code, decision type/payment status, three ground categories, and candidate taxonomy codes | Maintain the source decision identity; do not let workers assign final IDs |
| Policy matching | Parallel per unique reason where policy text is available | Identify only supported insurer-cited or clearly labelled agent-inferred matches | Verify each clause evidence/UID, collect exact policy document set, and preserve empty matches when policy support is unavailable |
| Merge and publish | Serial commit | None | Canonically sort source bundle, decision, and reason; assign `DR_N`, `AC_N`, and `PM-N`; detect duplicate coverage on accepted/denied sides; fetch one fresh policy snapshot; DAO-write once |

### Parallelization rules

- Run denial-response concurrently with policy-clause processing at the stage
  level. It can publish a reason result with no policy matches when the policy
  stage is not yet passed; it must not invent a match to make the contract
  look complete.
- At the document level, first group split children from the same original
  insurer response. CASE_135's DOC_007–009 must be one work item, not three.
- Across independent response bundles, extraction workers can run in parallel.
  Within one bundle, run the initial decision/acceptance segmentation once;
  a decision can depend on a cover letter and a later legal-opinion page, so
  workers must see the whole grouped bundle. Only subsequent per-reason
  taxonomy and policy-match work may fan out.
- In the CASE_135 shape, four reasons plus one acceptance make enrichment
  parallelizable but not unbounded; start with a small fixed cap and measure
  whether provider overhead outweighs the fan-out gain.
- Give every worker a fixed concurrency cap and a fully materialized redacted
  input bundle. Workers never read case files directly and never write DAO
  contracts.
- Keep final IDs and the single contract write serial. Output ordering must be
  independent of worker completion order.

### Semantic and transaction boundaries

- Provider calls decide whether wording is an insurer decision, whether it is
  denial or reduction, its coverage/item, the R-code, and whether a clause
  match is insurer-cited or agent-inferred. Python must not infer these from
  keywords or a previously seen case.
- Source-form policy matches are valid without a normalized clause contract.
  The driver derives stable display `C-N` values only from the canonical sort
  order of unique `(document_id, page, quote)` references; it never treats
  that display ID as a semantic join key. The quote/page evidence is the
  support, and the DAO verifies it at write time.
- A quoted precedent or a legal opinion embedded in a response is evidence of
  what the insurer asserted, not proof of the precedent's underlying facts in
  this case. The driver must retain its document/page provenance and must not
  turn it into an internal P6 fact.
- A conflict-ledger verdict or an operator's timing-unblock resolution note is
  workflow state, not case evidence. The driver may read it to determine
  whether dispatch is permitted, but must never cite it as support for an
  insurer ground, coverage, or factual premise.
- Policy promotion is a serial transaction boundary, not parallel work. The
  first driver version does not promote automatically: source-form matches are
  already citable, so promotion needs a separately recorded reason beyond
  citation convenience. If an explicitly approved later path promotes, it can
  demote policy processing; the driver returns that dependency state for
  orchestration rather than concurrently writing matches against an
  invalidated policy layer.
- A fresh policy snapshot is obtained only after all selected policy matches
  are final and immediately before the DAO write. A stale snapshot is a
  failed write, not a reason to reuse older bytes.

### Tests and measurement

1. Test one decision split across several document segments produces one set
   of unique reasons, acceptance records, and policy matches.
   Include a decision whose authoritative wording crosses a segment/page
   boundary, and reject a provisional wrong page citation before publication.
2. Test two independent insurer-response bundles process concurrently yet
   produce byte-stable IDs/order across repeated runs.
3. Test a split outcome (one denied coverage plus one accepted coverage),
   duplicate coverage rejection, taxonomy decision-type constraints, and
   absent stated amounts.
4. Test no-policy-stage / no-policy-match extraction, stale snapshot refusal,
   and policy-promotion invalidation as separate outcomes.
   Also test valid non-normalized source-form matches and the mandatory review
   routing for every `agent_inferred` match.
5. Benchmark a cold baseline against the driver with bundle count, reason
   count, provider time, merge time, and total active time reported
   separately.

## Workstream D: Screening Report

### Assessment

Screening report is a strong hybrid-driver candidate. Its factual summary,
insurance-decision summaries, source hashes, report path, section order,
citation tags, evidence sidecar, and final rendering are deterministic once
the upstream contracts are fixed. `document_assembly.py` already owns the
last part. The bounded semantic work is to consolidate issues, turn upstream
flags into answerable human review questions, set priority/reviewer routing,
and make a hedged preliminary assessment.

The stage is also safely concurrent with denial response under the current
dependency graph: it requires `claim_analysis` and `consistency_check`, but
not `denial_response`. This is an intentional optional-input path, not a
reason to read an in-progress or half-written denial contract.

### Observed CASE_135 execution evidence

The supplied CASE_135 start log reads state, ledger resolution notes, and a
large set of upstream contracts before it begins report assembly. It then
looks up the sections-file format and manually samples citation validity.
Those are fixed-route activities a driver can perform once and consistently.
The denial-response contract was expressly allowed to be absent because that
stage was running concurrently; therefore the driver needs a defined
snapshot-policy rather than an implicit race.

The completed CASE_135 log demonstrates the race concretely: denial response
was absent when screening synthesis began, appeared before the first contract
write, and the DAO refused the stale/no-hash report. The agent then re-read
denial reasons and reconciled parts of the report after its initial drafting.
That produces a schema-valid current contract, but it is not a clean semantic
snapshot: key issues, review points, and preliminary assessment were authored
before the final insurer-decision set was available. The driver must restart
all denial-dependent synthesis after a snapshot change; it must not patch a
few fields into an otherwise earlier narrative.

The operator-cleared P6 conflicts are workflow context only. A screening
driver must not cite their resolution notes as evidence or translate the
operator action into a substantive finding. It may summarize the unresolved
underlying question as a review point only when supported by cited upstream
case evidence.

### Target

Create `tools/run_screening_report.py`. It reads a stable snapshot of its
available inputs, derives all mechanical sections, performs one bounded
structured judgement call for the triage synthesis, writes
`screening_report.json` through the DAO, then asks `document_assembly.py` to
render `screening_report.md` and its evidence sidecar using the required
`screening_report` template.

| Phase | Parallelism | Provider judgement | Deterministic driver responsibility |
|---|---|---|---|
| Input snapshot | Parallel DAO reads, then serial freeze | None | Read required claim/coverage/case-type/requirements/consistency contracts and the conflict status; atomically decide whether a completed denial contract exists |
| Mechanical projection | Parallel pure computation | None | Build case summary, canonical report path, insurance position from denial reasons/acceptances, explicit totals only when every source amount is stated, source denial hash, and an upstream flag inventory |
| Triage synthesis | One bounded structured call initially | Group material issues; create answerable review points; set reviewer/priority; make a hedged preliminary assessment | Give the model only the frozen structured snapshot and relevant attributed evidence; reject new facts or unsupported citations |
| Final merge | Serial | None | Assign stable `ISSUE_N`/`RP-N`, preserve prior IDs when resuming identical points, enforce denial/reduction/acceptance polarity and source hash, DAO-write `screening_report.json` |
| Narrative render | Deterministic tool | None | Build a seven-section assembly spec; call `document_assembly.py --template screening_report`; let it generate citation tags and evidence sidecar |

### Concurrent-input snapshot policy

- If an insurer-response source exists but `denial_reason_result.json` is not
  available, do not form or publish a screening-report input snapshot. The
  current contract has no truthful representation for that state: `false`
  means no denial and `true` requires actual `DR_N` IDs.
- A private preparation cache may read/project the non-denial inputs in
  parallel with denial response, but it writes neither a screening contract
  nor a stage attempt. The first publishable snapshot is formed only once the
  denial contract is available (or the DAO establishes no insurer-response
  source exists).
- Immediately before the JSON write, reread only the denial contract and
  recompute the input fingerprint. If its hash changes, discard the entire
  synthesized JSON/sections payload and restart from the frozen-input phase
  once. Do not reconcile selected fields into the old narrative.
- When denial is present, copy the DAO-validated
  `source_denial_contract_hash` into the report and derive all insurer
  summaries from that one contract only. The DAO independently refuses a
  report with reason IDs or an absent/stale hash against a present contract.
- If the fingerprint changes again on the one restart, stop with a bounded
  retry result for the orchestrator rather than chasing a continuously moving
  input. This is a concurrency condition, not a human or semantic decision.
- If denial response later publishes or rewrites its contract after a
  successful screening write, the DAO's stale-downstream mechanism resets
  screening-report to pending. The previous screening file remains historical
  output but cannot be treated as current; rerun against the new snapshot.

### Parallelization rules

- Parallelize only independent DAO input reads and pure projections. They have
  no shared writes and reduce startup/tool-choreography time.
- Do not independently author the seven narrative sections in parallel on the
  first implementation. Cross-section consistency, shared review-point IDs,
  and a single preliminary assessment make a single synthesis call safer.
- After A/B validation, consider two bounded calls in parallel: one for
  evidence-grounded issue/review-point candidates and one for the preliminary
  assessment. Merge only after deterministic duplicate/source checks; the
  assessment must be regenerated or rejected if the merged review points
  materially differ.
- Rendering, JSON publication, IDs, source hash, and citation-sidecar write
  stay serial and deterministic.

### Hard stops and tests

- Test that an insurer-response source plus an absent denial contract produces
  no screening artifact or attempt; only a private preparation cache is
  permitted. A case with no insurer-response source has an explicit DAO
  `not_applicable` determination; a completed denial contract produces exactly
  matching denial/reduction/acceptance IDs and hash.
- Test the CASE_135 race as preparation-only: denial absent at first read then
  present before final synthesis must produce exactly one report generated from
  the final contract; a second hash change returns a bounded retry result.
- Test a denial contract written after the report invalidates the report's
  stage state, while leaving the historical artifact intact.
- Test that ledger/operator resolution notes cannot appear as evidence refs,
  and that a resolved-on-operator-instruction conflict is not reported as an
  adjudicated fact.
- Test stable issue/review-point IDs, source-ref coverage of upstream flags,
  required reviewer routing, amount total/null rules, and all seven rendered
  headings.
- Benchmark input assembly, semantic synthesis, JSON validation/write, and
  narrative render separately. Compare against an agent-led cold baseline
  before enabling any internal parallel synthesis.
