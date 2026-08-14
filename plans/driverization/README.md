# Driverization Implementation Plans

Claim-analysis driverization is archived and not selected for throughput
promotion; claim analysis remains agent-led. Policy and denial-response plans,
and their shared driver platform, remain active.

This directory turns the architectural decision record in
[`driverization-plan.md`](../../driverization-plan.md) into implementation
plans. Each driver preserves the existing stage contracts and DAO authority.
The orchestrator alone retains stage-attempt lifecycle transitions.

The completed CASE_135 run changed the rollout order. Read
`case135-measurement-replan.md` before treating any stage plan below as an
implementation priority: it is the measurement-backed authority for priority,
parallelism, and what remains agent judgement.

## Order

`case135-measurement-replan.md` sets the priority. Read it first; the plans
below are the implementations of items it schedules.

## Current three-driver build scope

`three-driver-program.md` is the implementation order for the policy,
denial-response, and claim-analysis drivers. Its D0 shared work is deliberately
above all three stage implementations: native Codex structured output and a
DAO-owned redacted-content/evidence surface are functional blockers, not
optional cleanup. It keeps P0.2 policy preflight separate from the later policy
semantic driver and does not delay the P1 scheduler pilot for any driver.

The same document now requires a provider smoke matrix for both Claude and
Codex, plus explicit DAO, resume, cold-control, and multi-unit parallel
execution gates. Passing mocked tests alone is not acceptance for a driver.

## User-directed throughput-first order

The labels P0–P3 still describe correctness and rollout scope. For the current
throughput objective, this order takes precedence over their numeric order;
deferred assurance work remains required before a production/evaluation claim.

1. P0.4: correct the `critic_v1` → `draft_report_v2` edge.
2. P0.2: land policy preflight so the policy lane can open without a
   mid-attempt invalidation/reopen.
3. P0.3a: add only the metric labels, closed-attempt coverage warning, and
   scheduler dispatch spans needed to measure a pilot.
4. P1: build the readiness-based scheduler and run the
   `policy_clause_processing ∥ denial_response` and early
   `denial_validation` pilots under their existing DAO gates.
5. P2's content bundle/structured-provider adapter, then the P3
   claim-analysis checkpoint pilot, measured against the scheduler control.

Defer P0.1's broad split-child finalization audit, P0.3b's full thirteen-read
trace expansion, and the remaining P2/P3 wrappers from the **throughput
lane**. They are not deleted or relaxed; they remain prerequisites for a
production/evaluation conclusion.

**P0 — correctness and measurement.** All items are required before a
production/evaluation claim. The user-directed throughput pilot above requires
only its named P0 subset; deferred items remain mandatory for the full rollout:

1. Split-child finalization audit (P0.1) — no plan file; the work is a
   finalize-time gate in `tools/dao.py` plus real-writer regressions, described
   in the replan.
2. `policy-preflight.md` (P0.2) — the canonical promotion sequence, moved
   before the attempt opens. Smallest change here and the only P0 item that
   alters a dispatch route.
3. Timing semantics and coverage (P0.3) — no plan file; `--run-id` plus
   traced reads on the triaged read-only DAO commands, agent/driver control
   spans, phase-scoped metrics, explicit closed-attempt coverage, and no
   parallel sum presented as elapsed time. Described in the replan.
4. `critic_v1` → `draft_report_v2` graph correction (P0.4) — a correctness
   fix, not a scheduling optimisation. It ships independently before any v2
   dispatch changes.

**P1 — readiness-based orchestration.** `orchestration-scheduler.md` and
`execution-lanes.md` — after P0 timing acceptance, dispatch each ready stage
through its own DAO preflight rather than serializing on Phase 1/Phase 2
labels. The scheduler applies equally to agent-led and driver-led stages; it
does not wait for driver acceptance.

**P2 — shared leaves, built once and reused.**
`three-driver-program.md` now makes the DAO content bundle, batch evidence
verifier, structured-provider adapter, and snapshot/resume receipt concrete.
Every stage plan below assumes these exist.

**P3 — stage pilots, in measured order.** These do not become dispatch paths
until their cold comparison passes:

4. `claim-analysis.md` and `three-driver-program.md` D1 — checkpoint 1 pilot
   is implemented and unit-tested; checkpoint 4 remains planned. Neither is
   dispatched yet.
5. `draft-report.md` and `critic.md` — reuse the evidence/snapshot leaves
   without replacing narrative or P1/P3 judgement.
6. `screening-report.md` — only after the denial-availability race is safe.
7. `consistency-check.md` — later semantic wrapper, not an early full-driver
   conversion. `denial-response.md` and `three-driver-program.md` D2 now
   define the separately assigned denial-response driver; it still needs its
   own measured acceptance before live routing.

Three P0/P2 items have no plan file because their scope is fully stated in the
replan and they change existing files rather than adding a driver. They are
listed here so the absence reads as deliberate rather than as an oversight.

No driver becomes the production dispatch path until its unit tests and one
cold A/B comparison with the agent-led path have passed semantic review.

## Shared existing-file changes

| Existing file | Required change | Used by |
|---|---|---|
| `tools/llm_providers.py` | Pass provider `output_schema` through to native Codex CLI `--output-schema`; retain one correction attempt and trace metadata. `CodexCliProvider.compare_text` currently discards it under a stale comment claiming the flag does not exist — correct that comment in the same change; the flag takes a **file path**, so a temp-file lifecycle is needed | All planned drivers |
| `tools/dao.py` | Add DAO-owned, case-scoped reads for redacted text and governed report artifacts; preserve capability checks, deny ground-truth through every new read, and trace every supported read/check with `--run-id`. Separately add the P0.1 document-processing finalize audit and the P0.3 triaged read/timing coverage changes | All planned drivers; P0.1/P0.3 are independent of them |
| `tools/run_policy_preflight.py` | New (P0.2). Runs UID verification for each in-scope noncanonical policy document before the policy attempt opens; calls existing DAO commands only. It does not alter the separate normalization opt-in | Policy stage |
| `tools/stage_dependencies.py` | Make `critic_v1` a hard prerequisite of `draft_report_v2` (P0.4). Add a pure `parallel_frontier` scheduling aid for the P1 scheduler after the throughput lane's P0.3a telemetry, not after driver acceptance | Orchestration |
| `tools/document_assembly.py` | Retain exclusive ownership of Markdown, citation-sidecar rendering, quote verification, locks, and atomic publication | Screening and draft reports |
| `tools/run_critic.py` | New hybrid critic driver; never receives ground-truth access | Critic |
| `.claude/skills/loss-adjustment-pipeline/SKILL.md` | Dispatch every readiness-based lane through its own DAO preflight; add driver dispatch only after that driver is accepted | Orchestration |
| `.claude/agents/*.md` | Replace manual tool choreography with one driver invocation and driver-specific stop/report rules, after the driver is proven | Affected stages |
| `tools/sync_agents.py` | Run after every canonical `.claude/` change; do not hand-edit generated `.agents/` or `.codex/` copies | All agent/skill changes |
| `pipeline.md` | Keep stage graph, execution lanes, and driver ownership in sync | Documentation |

New provider-response schemas may be added under `schemas/` only for bounded
internal calls. They are not replacement public contracts: the existing stage
schemas and DAO validation remain the only publication boundary.

## Verification pass — 2026-08-13

These plans were written from a run log and then checked against the code and
the on-disk case record. Six claims were wrong; all six are corrected in place,
with the correction stated rather than silently overwritten.

| Claim | Verdict |
|---|---|
| `critic_v1` missing from `draft_report_v2` prerequisites | **Confirmed** — graph reads `("draft_report_v1", "denial_validation")` |
| Four read-only DAO commands reject `--run-id` | **Wrong count** — thirteen do; the four were the ones one stage happened to touch |
| Canonical promotion flipped the policy marker mid-attempt | **Confirmed** — CASE_135 run state shows `policy_clause_processing` at `attempt_count: 2` |
| Canonical promotion is scoped by the `--disputed-by` opt-in | **Wrong** — that is `downstream_disposition` (normalization), a different axis from `uid_scheme` |
| Screening schema has no truthful denial-absent state | **Confirmed** — `has_denial` is a required non-nullable boolean and `true` forces `reason_ids` `minItems: 1` |
| DOC_007–009 are one insurer bundle | **Confirmed, but not joinable as described** — `source_document_id` is null; group by `source_file_name` + page range. DOC_009 is typed `other`, so type-filtering drops it |
| Codex CLI has a native `--output-schema` | **Both sides wrong** — the CLI has it, but the provider discards `output_schema` under a stale comment claiming it does not exist; the flag takes a file path |
| Claim-analysis 460.6s baseline | **Not span-measured** — derived from file mtimes; intra-stage instrumentation is a prerequisite, not a nicety |

Two structural gaps were also closed: P0.2 had no implementation plan
(`policy-preflight.md` now exists), and the order above listed only the stage
plans, omitting every P0/P1/P2 item that actually comes first.

Unverified and still to check before implementation: the per-stage CASE_135
execution narratives (what each agent did, in what order) are taken from run
logs and were not re-derived here.
