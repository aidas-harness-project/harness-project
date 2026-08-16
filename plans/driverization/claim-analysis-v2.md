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

## 4a. Arm E-cp1 result (2026-08-16): CP1 driven, CP2-4 agent — hybrid rejected

Step 1's skeleton ran for real on CASE_033: **CP1 in 137.0s** (one 131.9s
opus call, zero corrections, 2.6s glue), schema-PASS, 50 fields, redacted
facts null-not-substituted. The per-call projection was low (60–90s
projected, 131.9s actual for the grouped 15-document payload) — update the
spine estimate accordingly.

**The hybrid does not compose.** The remaining CP2–4 agent dispatch cost
517.3s / 59 tool uses — statistically indistinguishable from arm C running
all four checkpoints (528.2s / 47). An agent dispatch's cost is fixed
context establishment plus discipline overhead; the marginal checkpoint is
cheap. Therefore incremental migration (drive CP1, keep the agent for the
rest) is dead: the value hypothesis rests entirely on step 2 — all four
checkpoints driven, no agent dispatch. Revised spine estimate with the
measured CP1 figure: CP1 132s + CP2 ~60–90s + CP3 ~40s + CP4 ~60–90s + glue
≈ **300–360s**, still under arm C but with less margin than first projected;
the CP2+CP3 merge variant matters more than it did.

Also observed: `coverage_result.applicable` is a non-nullable boolean, and an
unresolved liability question forced opposite readings across runs (three
arms `true`, arm E `false` with an explicit not-adopting-the-denial warning).
A schema design observation to carry to the coverage_result owner, not an
agent defect and not this plan's scope.

## 4b. Step 2 detailed design (authorized 2026-08-16)

Five provider calls, serial spine, no agent dispatch:

| unit | calls | inputs served | deterministic work |
|---|---|---|---|
| cp1_field_extraction | 1 | all non-policy docs whole | (shipped in step 1) |
| cp2_coverage | 2 — **select** then **judge** | select: CP1 fields + the document index's full clause listing (title/page lines only). judge: CP1 fields + the redacted text of exactly the selected pages (`--pages`, one read) | page-list expansion, `policy-snapshot` fetch/embed, clause-ref local verify against served pages |
| cp3_case_type | 1 | CP1 fields + CP2 coverages + `adjuster_case_type` + the profile mapping table from the agent spec | registry template mapping is DAO-validated; adjuster copy-in when present |
| cp4_requirements | 1 | CP2 result + CP1 fields + the same served policy pages | REQ-N renumbering, coverage-name join enforcement, snapshot embed |
| medical publication | 0 | — | deterministic candidate projection from CP1+CP3; attempt `write-medical-variables`; a deferred-config refusal is recorded and non-blocking (matching observed agent behaviour on CASE_029/032/033), any other refusal raises |

**Evidence discipline for calls that see no new source text (CP2 judge partly,
CP3 entirely):** an evidence reference must either quote a SERVED policy page
or reuse a quote already verified in an earlier checkpoint's contract
(matched by document, page, and whitespace-normalized quote). Anything else
is refused in local validation, so it gets P4's one correction rather than a
write-time surprise. The DAO's `verify-evidence-references` still binds every
reference before publication — the local rule is a faster, narrower prefilter,
not a replacement.

Selection-call risk (missing the governing clause) is mitigated by
instructed inclusiveness (읽을지 말지 망설여지면 포함), a generous page cap,
and always including the 보통약관's 보상하는손해/면책 articles; CP4 reuses the
same served set so an exclusion the judge call saw is visible to the
requirements call too.

## 6. VERDICT (2026-08-16): kill criterion met — plan closed as measured-negative

Arm E ran to completion on CASE_034 (all four contracts published,
DAO-validated, clause refs verified, stage finalized). The numbers end the
plan under its own rule:

| unit | successful call | failed calls actually paid |
|---|---:|---:|
| CP1 grouped | 253.5s (131.9s on CASE_033 — 2x call variance) | 241.5s (scalar-in-fields) |
| CP2 select | 29.2s | 78.7s (over-strict page rule, since fixed) |
| CP2 judge | 75.3s | — |
| CP3 | 248.0s | 262.5s (misnamed profile keys, since fixed) |
| CP4 | 326.0s | 322.4s (grounding, since fixed) + 180.1s (timeout) |
| **clean composite** | **≈944s** (≈822s with best-observed CP1) | |

Against agent arm C's 528.2s the driver loses by 56–79% even on the
defect-free path. **Root cause is not the architecture's logic but the call
economics**: a cold monolithic `claude -p` structured call at opus costs
200–330s nearly regardless of payload (CP3's small output cost 248s; CP2's
small select cost 29–79s only because its output is trivial), while the
agent's warm conversation pays ~10s per incremental turn. Five cold calls
cannot beat 47 warm turns. The v1 CASE_601 pilot (16 calls × ~40s) and this
v2 (5 calls × ~50–330s) bracket the same wall from both sides.

What survives: the driver itself is a working, resumable, guardrail-stronger
implementation (per-unit receipts meant each fix-and-rerun reused every
finished unit — the five debugging invocations re-paid zero completed calls),
and its three generation-time schema hardenings are general lessons. Semantic
output was plausible but noticeably more variant than agent runs (19
requirements vs 10–13; first-ever `not_met`; mixed `applicable`).

Reassessment paths, each a separate P2/accuracy decision, none authorized
here: (a) `anthropic-api` provider with prompt caching and streaming instead
of cold CLI children; (b) a faster model for the extraction-shaped calls; (c)
a ≤2-call design (CP1+CP2 merged, CP3+CP4 merged) that accepts coarser P9
resume. Absent one of those, **the agent path with arm C's turn-budget spec
(528s) remains the production route for claim_analysis.**

**Implementation removed 2026-08-16** after the verdict, restoring the
pre-driver tree: `tools/run_claim_analysis.py` and its tests live at commit
`198baf6` if a reassessment path (api-with-caching / faster model / <=2-call
design) is ever authorized -- do not rewrite them from scratch.

**Path (a) opened and paused 2026-08-16, same day.**
`AnthropicApiProvider.analyze_text_structured` is now implemented (Messages
API, forced tool-use transport, `cache_control` on the prompt block,
truncation/fail-closed guards, unit-tested) and a paired one-call CP1 bench
exists; the CLI side of that pair measured **227.2s** on CASE_034's identical
prompt (bracketed by the earlier 131.9/253.5s), model confirmed as the CLI's
default `claude-fable-5`. The API side was **not run: no `ANTHROPIC_API_KEY`
was available and the user chose to stop rather than provide one**, so the
cold-call-cost hypothesis (CLI child overhead vs true model latency) stays
untested and no part of the arm E verdict changes. Known design constraint
recorded for whenever a key exists: Anthropic's cache prefix runs
tools→system→messages, and each checkpoint sends a different transport schema
as its forced tool, so the 5-call spine gets **no cross-checkpoint cache
reuse** under the current shape -- only identical re-calls (P4 correction
rounds, reruns inside the TTL) hit. A caching win would need the schema moved
out of the tool position or a shared-tool design, which is part of path (a)'s
work, not a free rider.

**Paths (b) and (c) measured/assessed 2026-08-16, same session.** The bench
walked the model axis on the identical prompt: **opus-5 146.0/149.2s with
51/59 fields (n=2)**; fable-5 -- the CLI default, i.e. what every prior
"opus 200-330s" record actually measured -- 227.2/237.4s with 36/35 (n=2;
131.9/253.5 historically); sonnet-5 95.3s/26; haiku-4.5 71.1s/21 (n=1).
Latency is the model, not the CLI child (3.3x spread, same process).
First-shot gate failures happen at every tier (fable 1/2, opus 1/2,
sonnet 0/1, haiku 0/1) -- P4's correction is part of any driver's cost.
Recall is the differentiator and is monotone: sonnet loses 21 of fable's
fields including `disability_rate`, the liability/fault opinion pairs,
`surgery_date` and the expense/limit figures CP4 consumes; haiku loses more;
**opus-5 extracts more than the default in ~62% of its time.** (c) is
mechanically feasible at exactly 2 calls (CP1+select / judge+CP3+CP4; no
dependency blocker; merged transports fit the argv cap) but projects
500-700s on default-model call costs; **a 2-call opus-5 spine (~2x147s +
correction margin) is the first (c) shape whose projection lands under
528s.** Downgrade-(b) rejected on measured recall; (c) not built alone. Live
candidates: (a) with a key; an opus-5 model switch (extraction call or whole
dispatch); (b)+(c) jointly on opus-5. Each needs a stage-level accuracy A/B
at n>1 and user authorization -- nothing rewired here.

**Arms F and G ran the same day (user-directed) and closed both candidates.**
Arm F, opus-5 agent dispatch under arm C's verbatim briefing (CASE_035):
620.7s / 63 uses / 184,393 tokens -- rejected; per-turn cost 9.85s is
identical to the default's, and opus took MORE turns. The big-call advantage
does not transfer to a turn-dominated loop. Arm G, the driver restored from
198baf6 and rebuilt as the 2-call spine above, run on opus-5 (CASE_036):
971.9s -- M1 (CP1+selection) 134.2s, vindicating the model half; M2
(judge+type+requirements merged) 822.5s, killing the merge half. Whether M2
was one call or a P4 correction pair is not determinable from retained
records (provider_wait wraps both attempts -- the same instrumentation gap
arm E recorded); the charitable clean reading (~560s) still fails the
criterion. Third cost-model correction: a structured call's price follows
its OUTPUT volume -- input payload is nearly free -- so merging checkpoints
serializes their combined output into one stream and gives back the saved
fixed overhead. **Production route unchanged: agent dispatch under arm C's
turn-budget spec (528.2s).** Unmeasured remainder: (a) with a key (which
also unlocks streaming and parallel fan-out), and a fan-out variant
M1 -> judge -> CP3 in parallel with CP4 (bounded by max instead of sum,
projected ~450-550s, borderline, not built).

**The sonnet 1-pass extraction question, asked and closed (2026-08-16).**
Downgrade-(b) was rejected on recall; this asked whether recall was a *prompt*
problem rather than a model ceiling -- i.e. whether `claude-sonnet-5` could do
CP1 in ONE call at ~95s with no supplementary call, no 2-pass, and no reliance
on P4. Scoring was pre-registered first: **46 semantic slots reconciled by
fact, not field name** (`slots.py`), because the earlier "sonnet drops 21 of
fable's 36 fields" number was a key-set diff. On that axis the real gap is
**9, not 21** (sonnet 31, fable bar 40, opus 45-46) -- sonnet had already
captured the disability rate, evaluation item, coverage limit and hospital
totals under its own names.

Three bench-only prompt revisions (production driver and agent spec untouched;
budget pre-registered at 3):

| variant | s | fields | refs | slots/46 | first-shot gate |
|---|---|---|---|---|---|
| v1 exhaustive checklist | 90.3 | 57 | 63 | 43 | FAIL -- `review_required` missing on 38/57 |
| v2 + required member keys pinned | 168.5 | 56 | 66 | 36* | FAIL -- `value` missing on 56/56 |
| v3 + payload keys declared | 99.6 | 59 | 86 | 44 | FAIL -- 2 numeric `normalized_value`, 3 non-verbatim quotes |

\* v2's recall score is an artifact of the empty fields, not a regression.

**Recall closes; first-shot compliance does not.** v1 cleared the recall bar
immediately and kept sonnet's speed -- the checklist is the whole lever, and
it works. But each shape fixed exposed the next: pinning the common member
keys made the model read the enumeration as the complete member spec and drop
the payload; declaring the payload keys fixed that and left a numeric
`normalized_value` on 2 of 59 fields plus **3 of 86 quotes not verbatim** --
the second axis, checked independently because a schema error short-circuits
the quote pass, and the one that is not schema-blockable at all. v3's two
scored slot misses are matcher false negatives (both facts present and
correctly cited); the pre-registered list was not loosened afterward.

**Rejected at budget.** A fourth revision would be tuning to n=1 on one source
case -- the failure mode the budget exists to prevent. Production route
unchanged: agent dispatch under arm C's turn-budget spec (528.2s). What the
run does establish, for whoever revisits (a): the exhaustive-checklist prompt
is a real, transferable recall lever independent of model choice, and the
generation-time-shape pattern now has seven instances, three of them from
this one session.

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
