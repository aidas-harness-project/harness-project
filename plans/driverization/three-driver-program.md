# Three-Driver Implementation Program

## Decision and order

This is the implementation plan for the policy, denial-response, and
policy and denial-response drivers. Claim-analysis driverization is archived,
not selected for throughput promotion, and remains agent-led. This program is
deliberately ordered by *shared blockers* and
measurable throughput, not by the order in which the three stage names appear
in the pipeline.

The scheduler work remains the first system-level throughput change. It can
schedule an agent-led stage just as well as a driver-led stage, so driver
implementation must not hold the P1 scheduler pilot hostage. Conversely, no
driver is promoted to the live dispatch path until the scheduler control and a
cold driver comparison have both been measured.

| Priority | Deliverable | Why it precedes the stage drivers |
|---|---|---|
| D0.1 | Provider-neutral structured text output | All three need machine-valid candidate results. Codex currently discards `output_schema`; Claude already has a native path. One common interface prevents the drivers from diverging by provider. |
| D0.2 | DAO redacted-content bundle plus evidence verifier | `read-document-text` returns a governed path, not text. A driver must neither expose that path to a model nor open it itself. All three need page-labelled redacted text and quote/offset verification. |
| D0.3 | Common driver receipt and minimum spans | A resumable input fingerprint prevents stale reuse; identical spans make a driver/control comparison meaningful. This is the driver-facing part of P0.3a, not the deferred thirteen-command trace expansion. |
| D1 | Claim-analysis checkpoint-1 pilot | It is the largest identified fan-out: 23 documents before the first CASE_135 write. Its 460.6-second baseline is mtime-derived, so D0.3 must land first. |
| D2 | Denial-response bundle driver | It can remove agent choreography, but CASE_135 contains one bundle, so parallel speedup is conditional on a later multi-bundle case. |
| D3 | Policy pipeline driver | Keep the already-planned preflight separate. Full normalized-policy production has the widest contract/table surface and is often a legitimate zero-normalization pass. |

`policy_clause_processing` preflight remains P0.2. Its real transition test
must be completed before the scheduler opens a policy attempt; it is not
subsumed by the policy driver below.

## D0.1 — one structured-provider surface, for Codex and Claude

Add an explicit text-analysis method to `BaseProvider`, for example
`analyze_text_structured(prompt, prompt_version, output_schema)`. The method
is for schema-constrained analysis, rather than overloading the semantically
different `compare_text` name in every driver.

Expose the same `analyze_text_structured(prompt, prompt_version,
output_schema)` interface from both CLI providers. It returns a normalized
`ProviderResult.structured_output` mapping and a JSON `text` representation.
The driver owns exactly one correction prompt after either provider returns a
syntactically valid value that fails its *driver-owned* response schema or
evidence verification. Provider retries remain only for transient execution
failures.

For `CodexCliProvider`:

1. Extend `_run` to accept `output_schema`.
2. Serialize that schema to a unique temporary JSON file outside `outputs/`
   and `data/`; close the file before launching the native Windows
   `codex.exe` process.
3. Pass `codex exec --output-schema <schema-file>` and retain
   `--output-last-message` for the result.
4. Parse the result as JSON, preserve it in `ProviderResult.structured_output`,
   and make the one caller-owned correction attempt explicit. Delete both temp
   files on success, failure, and retry exhaustion.
5. Keep the existing provider-slot/retry behavior. No driver receives its own
   subprocess runner or concurrency limiter.

For `ClaudeCliProvider`, route the same interface to its existing `_run(...,
output_schema=...)` implementation, which uses `--output-format json` plus
`--json-schema` and extracts the CLI envelope's `structured_output`. Do not
make the drivers parse a Claude envelope or a Codex last-message file
differently. Preserve the current `--safe-mode`, `--allowedTools Read`, and
secret-scrubbed execution rules for every Claude call.

Tests must mock both processes and assert their distinct native flag forms,
parsed normalized structured result, empty/invalid output handling, schema
validation failure, and one-correction exhaustion. The native command
configured by `HARNESS_CODEX_COMMAND` must be `codex.exe`, never the
`codex.cmd` shim. Do not commit an installation path, account name, token, or
other local credential: the command is supplied only through the environment.

## D0.2 — DAO input and evidence boundary

Add two case-scoped DAO capabilities; do not let a driver read processed files
after receiving a path.

1. `read-redacted-text-bundle CASE_ID --doc-id ... --run-id ...` returns JSON
   containing only requested, active, text-processed documents. Each entry
   contains `document_id`, source provenance needed for deterministic grouping,
   current text revision/fingerprint, and page-numbered redacted text. It
   refuses `expert_review_only`, `superseded_bundle`, missing text, unlisted
   document IDs, and any ground-truth path. Explicit document selection keeps
   relevance a stage decision while the DAO remains the access authority.
2. `verify-evidence-references CASE_ID --references-file ... --run-id ...`
   verifies page, quote, and optional exact range against that same processed
   revision and returns normalized verified references. It changes no public
   stage artifact. The caller may use it before a correction attempt; final
   `write-contract` validation remains authoritative.

Both commands need trace records keyed by `run_id` and unit tests for active
documents, superseded-parent refusal, non-text refusal, revision change, and
quote/range mismatch. This is also the right place to settle the CLI payload
shape once, rather than creating three incompatible JSON readers.

## D0.3 — common driver control contract

Each driver command takes `case_id`, `--run-id`, `--held-by`, provider/model,
and a bounded `--workers`. It reads the run state but never calls
`update-run-state` or `finalize-stage`.

For every publishable checkpoint/document/bundle, record a DAO-written
receipt with: input contract hashes and text revisions, prompt/schema version,
provider/model, completed output contracts, and status. On restart, reuse only
an output whose receipt's full input fingerprint still matches; otherwise
resume at the first invalid unit. Receipts are driver-internal metadata, never
a substitute for a stage contract or DAO invalidation.

Emit the same spans from every driver:

```text
input_snapshot -> dao_content_read -> provider_wait -> evidence_verify
-> deterministic_merge -> dao_publish
```

For fan-out units, additionally record queue wait, unit ID, worker width, and
attempt number. Report active-wall sum and interval union separately; do not
score against a file mtime or call a parallel sum elapsed time.

Avoid a speculative generic "driver runtime" beyond these small shared
helpers. Stage semantics, prompts, output schemas, and final merge rules stay
in their own drivers.

## D1 — Claim-analysis driver (archived; do not implement)

This section is retained as rejected/archived design evidence only. It does
not authorize implementation, promotion, or a throughput comparison; claim
analysis remains agent-led pending a new design with a demonstrated end-to-end
speed advantage.

Build this driver in checkpoints, retaining the current four public contracts
and their serial dependencies.

1. **Checkpoint 1 pilot first.** Read eligible documents through D0.2 and run
   bounded per-document candidate-fact calls. Do not let Python select between
   competing facts: one serial provider consolidation emits
   `extracted_claim_fields.json`. Verify every candidate quotation before the
   one DAO publication.
2. **Checkpoint 2.** After a fresh policy snapshot, use the structured provider
   for coverage applicability and write `coverage_result.json`. Source-form
   policy references remain valid for text-only policies; the driver must not
   promote policy normalization.
3. **Checkpoint 3.** Infer/cross-check case type only after the preceding
   contracts are valid, then write `case_type_result.json`. Preserve an
   adjuster-provided type and use the existing template registry rather than a
   Python keyword classifier.
4. **Checkpoint 4.** Fan out only independent coverage requirement candidates,
   then serially canonicalize order and stable `REQ-N` IDs before writing
   `requirement_matching_result.json`.

Add internal response schemas per checkpoint. Test resume at every checkpoint,
conflicting candidates, quote/revision/UID failure, stable IDs despite provider
order, and a valid existing contract remaining untouched. Acceptance compares
checkpoint 1 and 4 against a cold agent-led control with D0.3 spans and reviews
all four final contracts for semantic differences.

### D1 implementation record — 2026-08-13

Checkpoint 1 is implemented as a non-dispatch pilot in
The former `tools/run_claim_analysis_driver.py` design selected active, text-processed
claim-fact documents, reads their redacted page bundles only through the DAO,
and runs bounded per-document candidates (default: two workers). One serial
structured-provider consolidation still decides primary/secondary facts. The
driver then verifies every final quote through the DAO, publishes the existing
`extracted_claim_fields.json` public contract, and writes a `checkpoint_1`
driver receipt. It does not alter stage state or finalize a stage.

`tools/driver_schema.py` was added as a shared prerequisite when implementation
confirmed that the existing public contract contains repository-relative
`$ref`s. It materializes only schemas under `schemas/`; no case content is
opened by that helper. The fixture provider now implements the same structured
text surface as Claude and Codex so the driver path is testable without a
network call.

The shared runtime now makes P4's one correction attempt explicit for
driver-owned candidate validation. A second invalid result propagates as a
halt; it is never coerced into a contract.

Completed verification: synthetic DAO/provider integration verifies candidate
selection, exact evidence binding, public-contract publication, receipt write,
and fingerprint-matched reuse. Pending execution gates: V3 interruption and
resume against an isolated fixture, then V4 cold agent/driver semantic and
timing comparison on a non-evaluation case. D1 must not be added to canonical
dispatch instructions until those gates pass.

## D2 — `tools/run_denial_response.py`

The work unit is an insurer decision bundle, never one manifest document.

1. From the manifest, build groups by `source_file_name` and contiguous source
   page ranges **before** filtering by document type. CASE_135's DOC_007--009
   proves why: all belong to `DOC_002.pdf`, `source_document_id` is null, and
   DOC_009 is typed `other`.
2. Read each complete bundle through D0.2. Use one initial structured call per
   independent bundle for decisions, accepted coverages, grounds, amounts,
   R-code candidates, and citations. Do not split a single bundle into a
   per-reason provider fan-out before a measurement proves that the extra CLI
   launches win.
3. Perform a second provider call only when a policy match cannot be resolved
   from the initial cited material. It may emit source-form matches; it never
   auto-promotes a policy document.
4. Verify insurer and policy evidence, then serially deduplicate and assign
   stable `DR-N`, `AC-N`, `PM-N`, `BASIS-N`, and display `C-N` identifiers.
   Obtain a fresh policy snapshot for exactly the referenced policy documents
   and write `denial_reason_result.json` once.

Test sibling grouping, DOC_009 inclusion, independent bundle concurrency,
byte-stable merging, no-policy-match, agent-inferred review routing, stale
snapshot, and resume after one bundle. First acceptance must use a case with
at least two independent response bundles; CASE_135 only validates grouping,
not fan-out benefit.

### D2 implementation record — 2026-08-13

The initial extraction pilot is implemented as
`tools/run_denial_response_driver.py`. It groups active text documents by
`source_file_name` and contiguous source pages **before** considering document
type, so the CASE_135-shaped `insurer_response` + `other` tail remains one
work unit. Independent bundles use bounded provider workers; the serial
publisher assigns stable `DR-N`, `AC-N`, and `BASIS-N` identifiers, verifies
every insurer-text quotation through the DAO, and records a stage receipt.

This is intentionally not a dispatch candidate yet. It refuses non-empty
`policy_matches`: clause linking needs the planned policy-aware enrichment
checkpoint, a fresh snapshot, and policy-stage gates. It also has only a
whole-checkpoint receipt because candidate payloads are not persisted; the
per-bundle resume design remains required before V3. The implemented unit is
therefore useful for grouping/plumbing measurements, not a semantic replacement
for the current denial-response agent.

## D3 — `tools/run_policy_pipeline.py`

This is a separate driver from `run_policy_preflight.py`.

1. Run the already-built preflight before the orchestrator opens the stage.
   Once the stage is open, read its state and manifest. Documents still marked
   `text_only_no_normalization` are a valid zero-normalization route; report
   them, but do not create clause artifacts or promote them.
2. For every `automated_text_pipeline` policy document, obtain the D0.2 bundle
   and the current revision. Run the DAO table scan first for that document.
   A scan is an obligation even where the result has no candidates.
3. Use a per-document workflow: structured boundary candidates -> evidence
   verification -> DAO-write `policy_boundary_inventory_{DOC}.json`; then
   structured clause/condition candidates -> deterministic canonical UID
   derivation with the existing `policy_uid` helper -> DAO-write
   `normalized_policy_clause_{DOC}.json`.
4. For a table candidate, obtain the DAO table-region receipt before generating
   its table contract. The driver may fan out distinct policy documents, but
   boundary, receipt, table, and clause work within one document remain
   ordered. Begin with a no-table single-document pilot; add table and
   multi-document workers only after it passes.
5. Reuse a document only when its D0.3 receipt, source revision, table scan,
   inventory, and normalized contract all agree. A changed source revision
   reprocesses that document; it never re-stamps old offsets or UIDs.

Policy tests must exercise actual DAO writes and finalization gates: complete
zero-normalization case; canonical per-document inventory/clause pair;
uncovered boundary; stale revision; table scan without accounting; receipt
mismatch; table-region failure; and a restart that leaves an existing valid
document untouched. The first speed evaluation reports separate table-scan,
boundary, clause, verification, and publish spans. It does not claim a generic
parallel gain until a case has two eligible normalized policy documents.

### D3 implementation record — 2026-08-13

`tools/run_policy_pipeline_driver.py` implements the valid zero-normalization
route. When all active, text-processed policy documents remain
`text_only_no_normalization`, it makes no model call, records a manifest-bound
receipt, and returns a no-op result for the orchestrator's existing finalization
gate. If no active text policy exists, or any policy has opted into
`automated_text_pipeline`, it blocks without writing a clause artifact.

The canonical normalization driver remains unimplemented. It must add the
ordered table scan → boundary inventory → clause/condition → audit path and
its per-document source-revision/receipt resume semantics; this no-op driver
must not be extended by emitting partial canonical contracts.

## Execution-required verification

The following cannot be established by mocked unit tests alone. They are
explicit execution gates, to be run after the corresponding implementation is
complete. All use a synthetic/no-claim fixture unless a cold case comparison
is named; no provider smoke prompt receives raw or ground-truth material.

| Gate | What runs | Pass condition |
|---|---|---|
| V0a: native CLI smoke matrix | One schema-constrained benign prompt through native `claude` and native `codex.exe` | Both return a parsed object conforming to the same schema and clean their temporary result directory afterwards. |
| V0b: provider-adapter smoke matrix | The same prompt through the new shared provider method for both providers | In addition to V0a, `ProviderResult` identifies the selected provider/model and exposes the same normalized structured object. |
| V1: DAO boundary integration | Actual DAO commands against an isolated processed-text fixture | The content bundle preserves page labels/revision, refuses superseded/non-text IDs, and the evidence verifier rejects a wrong quote/range. |
| V2: policy preflight transition | Actual DAO promotion -> stage-open -> policy finalize path on a disposable case | Canonical promotion occurs before the open attempt and never creates the CASE_135 failed/reopen marker. |
| V3: driver resume | Interrupt each driver after one checkpoint/document/bundle, then invoke it again with identical input | Only the incomplete unit re-runs; a changed source revision or input contract invalidates the affected unit. |
| V4: cold semantic control | One cold agent-led run and one cold driver-led run of the same non-evaluation case per driver/provider | All public contracts validate; reviewer records semantic differences; timing has the D0.3 spans and is not mtime-derived. |
| V5: parallel-benefit run | A denial case with at least two independent insurer bundles and a policy case with at least two normalized policy documents | Worker width 1 versus bounded width is compared by interval union; no claim of parallel speedup is made from CASE_135's single bundle or a single policy document. |

Run V0a separately for Claude and Codex before any stage-driver work is
trusted. V0b follows D0.1, because the current provider interface does not yet
have the planned `analyze_text_structured` entry point. V1--V3 can run in an
isolated fixture environment. V4 and V5 are the only
gates that require a real cold case run; they must use the same redaction mode,
model/provider, and worker configuration within each A/B pair. Record timeout,
retry, and provider-queue spans even on a failed execution so a token/provider
outage is not misreported as driver speed.

## Integration and rollout

1. Finish P0.4, P0.2's real-transition regression, and P0.3a before the P1
   scheduler measurement.
2. Build D0.1--D0.3 once, with their tests, then implement D1--D3 in separate
   files so the three drivers do not conflict.
3. For each provider, pass V0a and then V0b after D0.1; for the common DAO surface, pass V1; and for
   policy preflight, pass V2. For each driver, then pass fixture/unit tests,
   V3 resume, V4 cold control, and (where it has multiple work units) V5
   parallel measurement before adding its invocation to the canonical agent
   instructions, `pipeline.md`, and the orchestration skill, followed by
   `tools/sync_agents.py`.
4. Do not replace all three agent paths in one change. The scheduler continues
   to dispatch the agent path for every driver that has not individually met
   its acceptance check.

## Files affected

| Area | Planned changes |
|---|---|
| Shared | `tools/llm_providers.py`, `tools/dao.py`, shared internal response schemas, provider/DAO tests, timing tests |
| Claim analysis | `tools/run_claim_analysis.py`, checkpoint schemas/tests, later `.claude/agents/claim-analysis.md` |
| Denial response | `tools/run_denial_response.py`, bundle schemas/tests, later `.claude/agents/denial-response.md` |
| Policy | `tools/run_policy_pipeline.py`, policy response schemas/tests, later `.claude/agents/policy-pipeline.md` |
| Orchestration/docs | `pipeline.md`, canonical orchestration skill, then generated copies through `tools/sync_agents.py` — only after each individual driver is accepted |

## Verification record

### V0a — passed 2026-08-13

The benign schema fixture requires exactly:

```json
{"ok": true, "value": "provider-smoke"}
```

Both native CLIs returned that object with exit code 0 and the temporary
output directory was absent after cleanup:

| Provider | Native command | Duration |
|---|---|---:|
| Codex | native executable supplied for this process via `HARNESS_CODEX_COMMAND` | 12.022 s |
| Claude | configured `claude` command | 4.539 s |

The native Codex command reported a ChatGPT login and `codex exec --help`
advertised `--output-schema`. `HARNESS_CODEX_COMMAND` was unset at the time of
the test, so D0.1 must configure it to a locally installed native executable
through the environment (not a WindowsApps bundle that cannot be launched and
not `codex.cmd`).

### V0b — passed 2026-08-13

`tools/provider_smoke.py` called the new driver-facing
`analyze_text_structured` method against the same benign schema through both
`codex-cli` and `claude-cli`. Both returned the required object, identified
their provider/model, and independently passed `Draft202012Validator`.

The smoke command locates the native Codex executable only at execution time,
passes it via `HARNESS_CODEX_COMMAND` for that process, and does not record the
machine-specific path. No case content, account name, API key, or access token
is accepted or printed by the smoke tool.

### V1 — passed 2026-08-13

An isolated DAO fixture exercised both new read-only commands against a
page-marked redacted document. `read-redacted-text-bundle` returned only
page-labelled content, source provenance, and revision/digest bindings; it
returned no processed-layer path. It refused both `superseded_bundle` and
`expert_review_only` documents even when a redacted file was physically
present.

`verify-evidence-references` returned current revision bindings for a valid
page/range/quote reference and refused a stale revision and a quote whose
claimed exact range did not match. Both CLI parsers expose optional `--run-id`
for trace attribution. This test used synthetic redacted text only; no claim
or ground-truth content was used.

### D0.3 foundation — passed 2026-08-13

`tools/driver_runtime.py` now supplies the three drivers with one deterministic
input fingerprint, receipt-match decision, and six-phase measurement vocabulary:
`input_snapshot`, `dao_content_read`, `provider_wait`, `evidence_verify`,
`deterministic_merge`, and `dao_publish`.

The DAO owns receipt publication through `write-driver-receipt` and retrieval
through `read-driver-receipt`. A receipt is schema-validated, locked, and bound
to its command's case/run/stage/unit identity. It contains only SHA-256 input
digests, provider/schema/prompt identifiers, and completed contract filenames;
it never contains source text, page text, prompts, filesystem paths, or
credentials. Trace spans likewise record only a SHA-256 unit hash, not a unit
identifier.

Fixture tests verified stable reuse for reordered inputs, invalidation on an
input change, DAO receipt write/read, identity-mismatch refusal, and the
absence of a raw unit identifier from the span record. Driver-specific V3--V5
remain pending until the corresponding drivers exist.
