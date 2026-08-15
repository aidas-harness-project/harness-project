# Claim-Analysis Driver v2 — Redesign Proposal

Status: **proposal, not approved for implementation.** The archived v1 plan
(`claim-analysis.md`) rules that reassessment requires a new design with a
demonstrated end-to-end speed advantage; this document defines that design and
the measurement that would demonstrate (or kill) it. Accuracy-affecting work
is not started on this document alone.

## 1. Why reopen a de-scoped plan

Two things changed since 709a499 de-scoped v1:

1. **The baseline is now measurable.** v1's acceptance section blocked on
   mtime-derived figures; the 2026-08-16 four-arm measurement produced
   span-measured, dispatch-recorded numbers on one held-constant input
   (stage-cut forks of CASE_142's processed state):

   | arm | discipline | dispatch | tool uses | tokens |
   |---|---|---:|---:|---:|
   | A (CASE_027) | whole-document reads | 691.8s | 70 | 218,589 |
   | B (CASE_028) | narrowed reads, calls split | 817.6s | 83 | 241,252 |
   | C (CASE_029) | batched reads | **528.2s** | 47 | 201,062 |
   | D (CASE_032) | C + two-round writes | 562.2s | 45 | 221,721 |

   The cost model is settled: **wall ≈ tool turns × ~10s**, payload
   second-order, write grouping neutral. Spec-level instruction has one
   measured win (A→C, −22%) and is near its floor — an agentic loop cannot
   drop far below ~40 turns while still reading, searching, verifying,
   writing four contracts, and running its gates.

2. **The v1 failure is now decomposed, not just observed.** CASE_601's trace
   shows checkpoint 1 ran 16 per-document provider calls summing 647.0s in a
   355.8s window at **max concurrency 2**, per-call durations 12.5–85.3s
   (mean ~40s). The failure was not "drivers are slow"; it was the product of
   three specific factors, each addressable:

   - **~40s per call** — cold `claude -p` child (measured ~4.3s spawn floor),
     no conversation cache, full context re-sent per call;
   - **near-serial execution** — 2-wide against an in-flight cap of 24 and an
     OCR path that already runs 24-wide on the same machine;
   - **16 calls for checkpoint 1 alone** — one per document, on a case whose
     entire non-policy payload fits comfortably in one prompt (CASE_027:
     50,571 chars ≈ the minority share of a 272,655-char input).

## 2. Cost model and target

Agent arm C spends 528s ≈ 47 turns × ~11s: the loop pays a full model turn
for every read, search, and write, and its serial nature is structural.

Driver v2's spine is **4–6 provider calls**, each curated-context and
structured-output, with fan-out only where the work is genuinely parallel:

| step | calls | est. wall | basis |
|---|---|---:|---|
| CP1 extraction | 1 grouped call (variant P: per-doc fan-out, 12-wide) | 60–90s | CASE_601 per-call 12–85s; grouped payload ≈ one large call |
| CP1 consolidation | 1 (only in variant P) | ~40s | CASE_601 |
| CP2 coverage | 1 (fields + index excerpt + selected policy pages) | 50–70s | index lookup is deterministic pre-work |
| CP3 case type | 1 (small prompt) | 30–45s | smallest call |
| CP4 requirements | 1 (variant P: per-coverage fan-out) | 50–70s | joins on CP2 names |
| deterministic glue | 0 | ~10s | verify/write/snapshot measured 0.7–2s each |

Projected: **~250–350s** — a further −35–50% from arm C, but NOT enough for
the 5-minute P50 path alone. Reaching ≤180s requires one of the deferred
levers on top: merging CP2+CP3 into one call (CONDITIONAL — same inputs, one
extra output object) or a smaller model for CP1 extraction (P2, accuracy
risk, separate decision). The projection is the number this plan must beat or
die by; it is written down before implementation on purpose.

## 3. Design

### Spine (serial, Python-owned)

```
read run state / resume point           (DAO, deterministic)
assemble input bundle                   (DAO reads only: all short docs whole,
                                         index, policy pages named by index/search)
CP1 call  -> candidates                 (structured output, native --json-schema)
  verify every quote verbatim           (Python, _cross_contract; refuse -> one
                                         bounded correction round, then halt P4-style)
  DAO-write extracted_claim_fields.json
CP2 call  -> coverages                  (fields + curated policy context)
  verify clause refs (source form), fetch policy-snapshot, DAO-write
CP3 call  -> case type                  (Python copies adjuster type when present;
                                         registry template mapping is table lookup)
  DAO-write case_type_result.json
  medical-variable publication          (deterministic; halts on policy refusal
                                         exactly as the agent does today)
CP4 call  -> requirements               (per-coverage; Python assigns REQ-N,
                                         preserves CP2 join names)
  verify + DAO-write
exit with per-checkpoint receipt        (driver receipts, like policy/denial drivers)
```

### Rules carried over from v1 unchanged

- Checkpoints serial; only CP1 documents and CP4 coverages may fan out.
- Python never infers medical/legal facts, applicability, or case type; it
  transports, verifies, numbers, and writes.
- Python never collapses a cross-document disagreement — competing candidate
  values reach the (single, serial) semantic consolidation or surface as
  `inconsistencies`/P6 material.
- A redaction-blanked fact is absent/uncertain, never substituted (the
  four-arm runs all correctly null `accident_date`; that behaviour is pinned
  in tests).
- The driver never moves run-state markers; T13 open/record-dispatch/finalize
  stay with the orchestrator, exactly like `run_stage2.py`.

### What is new versus v1

1. **Grouped-call default (variant G).** CP1 is ONE call carrying every
   in-scope non-policy document — the driver translation of arm C's measured
   win. Per-document fan-out (variant P) is kept as a benchmarked variant,
   not the default, and runs 12-wide under the existing in-flight cap if
   used. v1 hard-coded the per-document shape and paid 16 calls.
2. **Curated context, assembled deterministically.** The index names policy
   pages; the driver reads exactly those pages plus the short documents. No
   call ever receives a whole policy bundle. (Arm B showed narrowing is free
   when it does not multiply calls; in a driver, call count is fixed by
   design, so narrowing is purely positive here.)
3. **claude-cli native structured output.** Stage 1 has used
   `--output-format json --json-schema` since 2026-07-29 with a
   one-correction-then-halt contract; reuse that seam. v1's codex
   `--output-schema` temp-file adapter becomes optional, not prerequisite.
4. **Persistent per-checkpoint candidates** (kept from v1): a failed later
   checkpoint resumes without re-running earlier calls; P9 retry granularity
   stays per checkpoint.
5. **Receipts + spans.** `driver.provider_wait` / `driver.evidence_verify` /
   `driver.dao_publish` spans already exist from the CASE_601 pilot;
   `record-dispatch` is NOT used (no agent) — the driver's receipt and spans
   are the record, mirroring the policy driver.

### Guardrail posture (why this is not weaker than the agent)

P1 is *structurally stronger*: every quote in every candidate is verified
verbatim by Python before any write, where the agent relies on its own
discipline plus DAO write-time checks. P2 unchanged (DAO reads only). P4: one
bounded correction per structured-output failure, then halt for the
orchestrator — same shape as Stage 1 sheets. P11: publication path identical.
Semantic judgment stays in model calls; the driver replaces *tool
choreography*, which is exactly the boundary `run_stage2.py` proved.

## 4. Measurement plan (precondition for acceptance)

Arm E, same protocol as arms A–D: stage-cut fork of CASE_142's processed
state, cold, policy driver + index first, then `run_claim_analysis.py`.
Report, from spans and receipts (never mtimes):

- wall per checkpoint, provider wait sum vs window (concurrency actually
  achieved), verify/merge/DAO time, correction rounds;
- semantic diff against arms A/C/D outputs (type/template/coverages/KCD,
  requirement statuses — allow decomposition variance, flag any
  met/not_met flip);
- a second case from the new corpus (one S, one M) before any routing
  decision — one case cannot carry a routing change.

Kill criteria, stated in advance: arm E ≥ arm C's 528s on the same input, or
any met↔not_met flip attributable to curated context, or a correction-round
rate that pushes P90 above the agent's. Any of these ends the plan the way
CASE_601 ended v1 — recorded, with numbers.

## 5. Effort and sequence

1. `tools/run_claim_analysis.py` skeleton: bundle assembly + CP1 grouped call
   + verify + write, resumable. Reuses `llm_providers` structured seam,
   `_cross_contract` verification, driver-receipt plumbing. (~1 session)
2. CP2–CP4 + medical publication + receipts. (~1 session)
3. Arm E measurement + semantic diff + this document's verdict. (~0.5)
4. Only after acceptance: spec/skill routing change (agent spec becomes the
   prompt source, dispatch docs updated), per v1's existing-file plan.

Out of scope here: CP2+CP3 merge and model downgrade (each its own measured
decision after arm E), codex structured-output adapter, any change to
checkpoint contracts or schemas (all four outputs stay byte-compatible).
