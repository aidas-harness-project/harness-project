# CASE_135 Measurement Replan — 2026-08-13

## Status and scope

This document supersedes the **delivery priority and rollout claims** in the
earlier driverization plans. Policy and denial-response stage-specific designs
remain active candidates; claim-analysis driverization is archived and not
selected for throughput promotion. Claim analysis remains agent-led pending a
new design with a demonstrated end-to-end speed advantage.
designs, not approved production changes. It is based on the completed
throughput/plumbing run recorded in
`docs/run-notes/CASE_135_RUN_20260812_135.md` and its optimisation analysis in
`docs/phase1-optimization-plan-2026-08-13.md`.

CASE_135 used P8 single-reader and operator-closed P6 conflicts. It is useful
for timing/plumbing evidence only, never for semantic quality or evaluation
claims. `evaluation` remains deliberately unmeasured behind its real D1 human
review gate.

## What the measurement changes

| Observation | Decision |
|---|---|
| 12 runnable stages total 6,665.5s; ten substantive stages lie between 418s and 891s | Do not chase a single stage bottleneck. Prefer levers that apply across stages. |
| Agent-dispatched work is 5,837.5s (88%); tool spans are about one second per stage | Instrument and remove agent tool choreography before small Stage-2 tuning. |
| `denial_response ∥ screening_report` saved 519.3s, but exposed a false no-denial encoding while the denial contract was absent | Do not make this a default final-stage lane yet. Preserve the finding as an experiment with an explicit availability model. |
| `critic_v1 ∥ denial_validation` saved 392.4s with no shared-input race | Make this the first production parallel lane after scheduler tests. |
| Stage 2 is already driven and is 12.4% of total; width=24, OCR overlap, and payload hypotheses were all refuted | Keep redaction width at 12; do not add a deterministic redaction skip. |
| 18 split-child contracts were invalid (`run_id: null`, `items_redacted: null`) while stages passed | Correctness gates and real schema-writer tests remain required before production/evaluation; the user-directed provisional throughput lane may defer this audit. |
| v2 consumes binding critic findings, but the current graph permits v2 before `critic_v1` | Add `critic_v1` as a v2 hard prerequisite before enabling the v2 driver. |

## User-directed throughput-first order

The following sequence overrides the normal P0–P3 delivery order for the
current throughput objective. It does **not** weaken DAO-only access, stage
attempt ownership, human gates, or ground-truth isolation. Deferred work is
required before a production/evaluation claim, but does not block an explicitly
provisional plumbing run.

1. **P0.4 graph correction:** add `critic_v1` to the v2 join. It is small and
   prevents a faster scheduler from starting an incomplete v2.
2. **P0.2 policy preflight:** move digest/UID enablement before opening the
   policy attempt, eliminating the current invalidation/reopen detour from the
   earliest fan-out path.
3. **P0.3a minimal telemetry:** correct attempt-sum versus interval-union
   labels, show incomplete marker coverage, and emit graph-ready/preflight/
   dispatch/no-ready spans. Do not wait for the full thirteen-command read
   tracing expansion to pilot scheduling.
4. **P1 guarded DAG scheduler:** dispatch `policy_clause_processing ∥
   denial_response` when their independent preflights clear; dispatch
   `denial_validation` immediately after its two prerequisites rather than
   after a Phase 2 label. Measure both as pilots.
5. **P2/P3 focused driver path:** build only the DAO content bundle and native
   structured-provider adapter needed for the claim-analysis checkpoint pilot,
   then compare it against the scheduler control.

Defer from this throughput lane: P0.1's broad split-child finalization audit,
P0.3b's remaining read/intra-agent tracing coverage, screening availability
schema work, and all other semantic-stage wrappers. They are deferred, not
cancelled; no provisional run may be represented as production/evaluation
evidence.

## P0 — make correctness and timing trustworthy

| Work | Existing files to change later | Required regression coverage |
|---|---|---|
| Split-child finalization audit | `tools/segment_case.py`, `tools/dao.py`, relevant schema-validation helper | `tests/test_segment_case.py`, document-processing finalization tests, real writer (not mocked) |
| Canonical policy preflight | a small new preflight/driver tool plus `tools/dao.py` invocation boundary; then orchestration documentation | policy-stage dependency and run-state transition tests |
| Read/check tracing | `tools/dao.py` parser and handlers | DAO CLI/parser and trace aggregation tests for each command added (thirteen candidates, triaged — see P0.3) |
| Timing semantics, coverage, and scheduler observability | `tools/trace_aggregate.py`, timing schema/DAO aggregation, and the future orchestration runner/dispatcher | tests distinguish closed-attempt sum from interval union, Phase 1 SLA from full-run wall, coverage gaps, and dispatcher wait from child tool spans |

### P0.1 Preserve and enforce split-child validation

The two CASE_135 defects are reported fixed. Before a production-facing
rollout,
keep them covered by tests that invoke the real schema-validated writer, not a
mock: every split child has a string run ID, its own non-negative
`items_redacted` count, and a valid registered source revision.

Add a document-processing finalization audit that validates every active
derived contract, including split children. It must fail closed before the
stage is marked passed. A missing contract, `null` in a required numeric field,
or an invalid child revision is a stage failure, not a warning for a later
agent to discover.

### P0.2 Move canonical promotion into a pre-attempt policy preflight

Keep the canonical-v1 gate intact. Run the existing source-digest and
canonical-enable sequence from a small policy preflight **before** opening the
`policy_clause_processing` attempt. The preflight may perform invalidation;
the actual policy attempt must begin only after that state is stable. It must
not be hidden inside an agent invocation or occur mid-attempt.

Implementation plan: `policy-preflight.md`. Note that the UID promotion
(`uid_scheme`: `legacy` → `canonical_v1`) is a different axis from the
normalization opt-in (`downstream_disposition`, via
`promote-policy-document --disputed-by`); the preflight covers only the first.
The gate itself is unchanged: the preflight satisfies it by running the real
digest+enable sequence earlier, never by defaulting a document to canonical,
which `dao.py` refuses as claiming "a verification that has not happened".

### P0.3 Complete timing coverage and correct timing semantics

**P0.3a — throughput-pilot minimum.** First add the report labels,
closed-attempt coverage warning, and scheduler graph-ready/preflight/dispatch/
no-ready spans described below. This is enough to distinguish a lane's measured
elapsed boundary from an attempt-wall sum.

**P0.3b — full production coverage.** Then complete the triaged thirteen-command
read tracing and intra-agent instrumentation. It remains a production/evaluation
requirement but does not block the user-directed scheduling pilot.

Add optional `--run-id` and traced-read spans to the read-only DAO commands
that currently reject it. The parser, handler, and trace regression tests must
change together.

**Corrected 2026-08-13 — the count was wrong.** This plan said "the four
currently excluded commands". Enumerating every `add_parser` block in
`tools/dao.py` shows **thirteen**:

```text
split-core-field-accuracy   check-untagged-claims      read-ground-truth
check-segmentation-ready    check-lock                 read-evidence-tags
check-forbidden-expressions read-conflict-ledger       read-human-review-ledger
read-revision-index         read-segment-derivation-index
read-table-region-index     read-timing-summary
```

Confirmed live, not inferred: `dao.py read-conflict-ledger CASE_135 --run-id
RUN_20260812_135` exits with `unrecognized arguments`. The original four came
from the commands the CASE_135 critic route happened to touch, which is a
sample of what one stage used, not the actual gap.

This is why the instruction to pass `--run-id` on every DAO call is currently
unsatisfiable — the agent is told to do something the CLI rejects, which is a
spec/code contradiction as well as a timing hole.

Not all thirteen deserve the same treatment. `read-ground-truth` is D1-gated
and `check-lock` is a P5 pre-run probe that legitimately runs before a run id
exists; both need a deliberate decision rather than a mechanical addition.
Triage the list before implementing, and record the ones intentionally left
out so the next reader does not re-derive this.

For every future driver, emit separate spans for input snapshot, DAO content
read, provider call, deterministic verification, and publication. For an
agent-led control run, record dispatcher start/end around the complete stage
call so the opaque wall time is explicitly labelled; do not pretend internal
model latency is DAO time.

CASE_135 also exposed a reporting error that this P0 item must prevent. The
sum of `total_attempt_active_wall_s` is an **attempt-wall sum**: useful as a
cost/load measure, but not elapsed runtime when attempts overlap. A rollup
must publish separately:

1. the completed-attempt wall sum;
2. the union of completed stage-attempt intervals, labelled partial whenever
   any attempt is open, interrupted, orphaned, or otherwise lacks a paired
   end marker;
3. the Phase 1 SLA wall and active time, limited to the `sla.phase1` marker
   window; and
4. a whole-local-run wall only after explicit local-run start/end markers are
   defined. It must never be inferred by comparing an all-stage union to the
   Phase 1 SLA window.

The CASE_135 figures demonstrate why the labels matter: 6,665.5 seconds is a
closed-attempt sum, while 5,753.9 seconds is the closed-attempt interval union
after the two measured overlaps. Its trace coverage is incomplete (two open
attempts), so neither number is the complete end-to-end elapsed time. The
17,892.8-second `sla.phase1` wall includes a large unclassified interval and
does not cover the later Phase 2 attempts; it is not a denominator for the
all-stage percentage.

The future dispatcher must trace, per candidate, graph-ready time, preflight
start/end, dispatch start/end, and a reason-coded no-ready interval. Those
spans distinguish a scheduler queue from human waiting, an external token or
process interruption, and work performed inside a child stage. Do not create
an undifferentiated "orchestration cost" bucket.

**This also blocks the P3 claim-analysis pilot, which is easy to miss because
the two look unrelated.** The 460.6s/113.5s blocks that pilot is scored
against are derived from file mtimes, not spans — the analysis agents have no
intra-stage instrumentation at all. A driver measured against a mtime-derived
baseline produces a comparison in which only one side is real. Intra-stage
spans are therefore part of this P0 item, not a refinement to add later.

**P0 acceptance:** real-schema split-child regressions fail when either CASE_135
bug is restored; canonical promotion cannot flip an open policy attempt; every
read-only command in the triaged list accepts and traces `--run-id`, with the
deliberate exclusions recorded; analysis stages emit intra-stage spans, so no
acceptance figure rests on a file mtime; timing output labels attempt-wall sum,
closed-attempt union, Phase 1 SLA, and coverage without cross-window
percentages; and a timing summary distinguishes driver, provider, DAO,
dispatcher queue, and uninstrumented agent wall time.

### P0.4 Correct the v2 dependency before scheduling it

Add `critic_v1` as a hard prerequisite of `draft_report_v2` in
`tools/stage_dependencies.py`, with dependency and invalidation regression
tests. This is a correctness repair: v2 is instructed to dispose of v1 critic
findings, so its current `draft_report_v1 ∧ denial_validation` join is
insufficient. It is deliberately separate from the P1 scheduler and ships
before any v2 lane is enabled.

## P1 — guarded readiness-based parallelism

| Work | Existing files to change later | Required regression coverage |
|---|---|---|
| Readiness-based DAG scheduler and clean critic/validation lane | new orchestration runner/dispatcher, `tools/stage_dependencies.py`, `pipeline.md` | scheduler and dependency tests: independent attempt opens, `critic_v1` required for v2, no early v2, and dynamic-input holds |
| Screening private preparation experiment | new private driver/cache under `tools/`; no public contract change in the first experiment | absence/presence race, no screening artifact write, no screening attempt open, cache invalidation |
| Future public denial availability state (only if proposed later) | `schemas/screening_report.schema.json`, `tools/_cross_contract.py`, `tools/dao.py`, invalidation logic | pending/non-applicable/available semantics and downstream draft refusal |

### P1.1 Readiness-based scheduler and production lane

Implement the guarded scheduler described in `orchestration-scheduler.md`
after P0 acceptance. It consumes graph readiness, never phase labels, and
performs dynamic availability checks plus a separate DAO preflight and attempt
boundary for every stage.

`denial_validation` becomes ready as soon as `denial_response` and
`consistency_check` pass. `critic_v1` becomes ready when `draft_report_v1`
passes. The scheduler dispatches either immediately; it does not wait to make
their starts simultaneous. Their overlap is safe because they read distinct
upstream contracts and neither consumes the other's output. The CASE_135
overlap saved 392.4s (37.0%) within that pair; treat it as one-case evidence,
not a universal constant.

Before dispatch, the scheduler performs the normal DAO conflict/lock/dependency
preflight independently for each stage and opens one attempt per stage. It
does not share a lock or attempt boundary. Make `critic_v1` a hard prerequisite
of `draft_report_v2`, because v2 must dispose of binding v1 findings.

### P1.2 Experimental lane: denial response versus screening preparation

Do **not** dispatch a publishable `screening_report` against an absent denial
contract. The current boolean cannot truthfully mean “insurer-response source
exists but its extraction result is not available”: `false` means no denial,
and `true` requires real `DR_` IDs and a source hash.

Verified in `schemas/screening_report.schema.json` on 2026-08-13, so the
constraint is structural rather than a matter of convention:

- `insurer_position.required` includes `has_denial`, and its type is
  `boolean` — not nullable, and with no "unknown" enum member. There are
  exactly two representable states.
- An `allOf` branch makes `has_denial: true` require
  `denial.reason_ids` `minItems: 1`, so the true side cannot be asserted
  without inventing IDs that do not exist yet.

Both halves must hold for the argument to stand, and both do. Any future
"pending" proposal therefore has to change the schema; it cannot be expressed
by populating existing fields differently.

The safe first experiment is an internal, non-contract
`screening_preparation` cache: after consistency passes, it may DAO-read and
project only the non-denial inputs while denial response runs. It writes no
`screening_report.json`, Markdown, or stage state. After denial completes, the
normal screening synthesis freezes the complete input snapshot and publishes
once. Measure whether reused preparation saves enough time to justify itself.

Only if that experiment demonstrates a real benefit should a separate proposal
consider a public `denial_input_status` state (`not_applicable`, `available`,
`pending`) plus a DAO rule that forbids draft consumption/finalization from a
`pending` screening artifact. That proposal needs contract, invalidation, and
downstream-gate tests; it is not part of P1.1.

### P1.3 Measure, but do not yet claim, the earlier fan-out

`policy_clause_processing ∥ denial_response` is structurally available after
document processing when an insurer-response input exists. The P1 scheduler
may run it as an instrumented pilot after its per-stage preflights pass, but
there is no comparable clean wall-time measurement in CASE_135. Report the
result as a pilot, not an SLA claim. Never widen redaction workers beyond 12
based on this plan.

## P2 — build shared deterministic leaves before stage wrappers

| Work | Existing files to change later | Required regression coverage |
|---|---|---|
| DAO content bundle and batch verifier | `tools/dao.py` and a focused new verifier helper/tool | direct-path denial, superseded-document exclusion, page/quote mismatch, traced reads |
| Structured provider adapter | `tools/llm_providers.py` | native Codex `--output-schema`, one correction only, provider span |
| Snapshot/resume receipt | new common driver helper, targeted metadata schemas only where required | hash mismatch, one restart, no stale artifact reuse |

The common repeated shape is: DAO discovery → many source reads → page/quote
verification → structured response → one governed write. Build the following
once and reuse it before declaring several stages “drivers”.

1. **Case-scoped DAO content bundle.** Batch-read redacted source text and
   governed report artifacts by case/document/version, reject superseded and
   ground-truth paths, and record every read with `--run-id`.
2. **Batch evidence verifier.** Given case-scoped `(document_id, page, quote)`
   references, return exact page/quote validation in a stable order. It is the
   common leaf for claim analysis, denial response, reports, and critic; it
   does not decide what a citation proves.
3. **Structured-provider adapter.** Pass the output schema to native Codex
   CLI, retain exactly one shape-correction call, and trace the provider span.

   **Checked 2026-08-13 — the code and the CLI disagree, and the plans
   inherited the wrong half.** `codex exec --help` really does offer
   `--output-schema <FILE>` (a JSON Schema file describing the final response
   shape), so this passthrough is implementable as every plan assumes. But
   `CodexCliProvider.compare_text` in `tools/llm_providers.py` carries a
   comment stating the opposite — "codex-cli has no native structured-output
   flag (unlike claude-cli's `--json-schema`) … cannot be enforced here" — and
   currently drops `output_schema` on the floor, accepting it only "for
   interface parity".

   Two things follow. First, the comment is stale and must be corrected in the
   same change, or the next reader will treat the passthrough as impossible
   and re-derive this. Second, the flag takes a **file path**, not an inline
   JSON string, so the adapter needs a temp-file lifecycle (written outside
   governed case directories, per the common driver contract) — a detail none
   of the stage plans account for when they describe this as simply "passing
   the schema through".

   Until this lands, only `claude-cli` enforces a schema natively, so any
   driver piloted before it must keep its own parse-with-one-correction path
   rather than assuming enforcement.
4. **Snapshot/resume receipt.** Bind a driver output to hashes of required
   contracts, source revisions, template, and version. A mismatched receipt
   is never reused; a changed input causes one bounded restart.

These leaves are mechanical. They must not infer coverage, resolve P6,
classify a denial reason, judge a legal/medical proposition, or decide a P3
hedge.

## P3 — pilot stage changes in measured order

| Order | Scope | Driver/agent boundary | Required measurement gate |
|---:|---|---|---|
| 1 | Policy preflight | Drive canonical promotion only; policy clause interpretation remains agent judgement | No open-attempt invalidation; source UID gate remains strict |
| 2 | Claim analysis checkpoint 1 | Per-document workers emit attributed fact candidates; one serial semantic consolidation emits the existing fields contract | Compare the 460.6s pre-checkpoint block — **but that figure is mtime-derived, not span-measured**, so intra-stage trace spans must land first; preserve divergent facts rather than silently merging them |
| 3 | Claim analysis checkpoint 4 | Per-coverage requirement candidates after coverage is fixed; deterministic sort/ID merge, semantic verdict per worker | Compare the 113.5s checkpoint-4 block and stable `REQ-N` output, under the same instrumentation prerequisite |
| 4 | Draft v1/v2 and critic | Reuse batch evidence verification, snapshot/resume, renderer, and deterministic floors; Korean narrative and P1/P3 finding judgement remain model work | Compare citation-verification time separately from drafting/review judgement |
| 5 | Screening | Partial driver after P1.2's race-safe input protocol is proven | No publishable artifact has an ambiguous denial state |
| 6 | Consistency, denial response, denial validation | Keep primary semantic pass agent-led initially; introduce a wrapper only when shared leaves show material non-semantic savings | P6, insurer-vs-evidence, and policy-match semantics unchanged in a cold A/B review |
| 7 | Evaluation | No optimisation work before a genuine D1 review produces a valid test run | D1 isolation remains structural |

The previous per-stage driver documents describe possible later wrappers. They
must not be read as authorisation to replace these semantic stages wholesale.

## Explicit non-goals

- Do not tune redaction worker width, suppress OCR cross-validation, or add a
  deterministic redaction skip from this result.
- Do not weaken canonical-v1 UID verification or promote policy documents
  automatically merely to simplify scheduling.
- Do not merge claim analysis with consistency, denial response with denial
  validation, screening with draft, or critic with evaluation.
- Do not fabricate the D1 human review merely to measure evaluation.

## Rollout scoreboard

For every change, report attempt-wall sum and actual elapsed wall separately;
parallel work must never be summed as elapsed time. Use `6,665.5s` only as the
CASE_135 attempt-wall baseline, not an SLA measurement, and use the two measured
parallel savings only for their respective pairs. A stage wrapper advances from
pilot only when it passes schema/DAO regression tests, preserves semantic
review on a cold identical case, and reports its own input/provider/verify/write
breakdown.
