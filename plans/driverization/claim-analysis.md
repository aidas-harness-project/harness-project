# Claim Analysis Driver — Archived Design Evidence

## Goal

Claim-analysis driverization is not selected for throughput promotion and is
not an active implementation plan. Claim analysis remains agent-led for now.

The agent-led checkpoint-1 control completed; driver attempts exposed
transport and consolidation costs. No definitive speed comparison is claimed
unless the latest CASE_601 driver run completed successfully with a valid final
wall time. The retained design findings — per-document candidate persistence,
deterministic grouping, and model-selected candidate IDs — are rejected/
archived evidence. Reassessment requires a new design with a demonstrated
end-to-end speed advantage.

Everything below is retained only as rejected/archived design evidence. It
does not authorize implementation, promotion, or a throughput comparison.
The driver replaces high-level tool choreography, not semantic judgement.
It never calls `update-run-state` or `finalize-stage`.

`three-driver-program.md` is the current cross-stage implementation order. In
particular, its D0.1--D0.3 shared provider, DAO-input, and receipt/span work
is a functional prerequisite of this plan, not a parallel replacement for it.

**Activation priority:** this is a P3 pilot under
`case135-measurement-replan.md`, after the shared content-bundle, evidence
verification, structured-provider, and snapshot leaves exist. CASE_135 shows
that checkpoint 1 is the first worthwhile pilot: 460.6s (61% of the stage)
occurred before its first write over 23 documents. It is not approval to
replace all four semantic checkpoints in one change.

## Prerequisite

Extend the Codex CLI provider so a supplied `output_schema` is passed to the
native Codex CLI `--output-schema` flag. Preserve a bounded one-correction
path for invalid/failed structured output.

Two verified details (2026-08-13) this prerequisite is larger than it looks
because of: `CodexCliProvider.compare_text` currently **discards**
`output_schema` and carries a stale comment claiming the flag does not exist,
which must be corrected in the same change; and `--output-schema` takes a
**file path**, so the adapter needs a temp-file lifecycle outside governed
case directories. See P2 item 3 in `case135-measurement-replan.md`.

## Existing-file modification plan

- `tools/llm_providers.py`: implement native structured output for Codex CLI
  and preserve trace/retry behavior.
- `tools/dao.py`: add the read-only redacted-text content/batch-read command
  used by this driver; all input reads must remain traceable with `--run-id`.
- `schemas/`: add four internal provider-response schemas, one per checkpoint;
  retain the existing four public output schemas unchanged.
- `.claude/agents/claim-analysis.md`: after acceptance, replace the manual
  checkpoint procedure with one `run_claim_analysis.py` call and its stop
  semantics; keep the current substantive rules as driver prompt source.
- `.claude/skills/loss-adjustment-pipeline/SKILL.md` and `pipeline.md`: change
  dispatch documentation only after the driver is selected as the live path.
- `tests/`: add driver unit tests plus provider and DAO read-surface tests;
  retain existing schema/DAO tests as regression coverage.
- Run `tools/sync_agents.py` after the canonical agent/skill edit.

## Checkpoint flow

1. Read run-relevant state and existing contracts through DAO commands.
   Confirm the policy-stage dependency and resume from the first missing
   checkpoint.
2. Materialize a minimal redacted-text input bundle through DAO reads only.
   Never expose processed-layer paths to the model or open them directly.
3. Run bounded per-document provider workers that emit attributed *candidate*
   facts only. The driver verifies every cited quote/page and preserves all
   incompatible candidate values. One serial provider consolidation then
   selects/marks the existing claim-field shapes; Python never collapses a
   cross-document disagreement. DAO-write `extracted_claim_fields.json`.
4. Call the provider for coverage applicability. Fetch a fresh policy snapshot
   for exactly referenced documents; validate source/UID clause addresses;
   DAO-write `coverage_result.json`.
5. Call the provider for case-type inference/cross-check. Python copies an
   adjuster-supplied type when present and maps valid axis pairs to the
   registry template; DAO-write `case_type_result.json`.
6. After coverage is fixed, run bounded per-coverage provider workers for
   requirement candidates. Python preserves coverage joins, canonically sorts
   requirements, assigns stable `REQ-N` IDs, validates references/snapshot,
   and DAO-writes `requirement_matching_result.json`.

## Constraints

- Checkpoints are serial; each consumes a validated predecessor. Only the
  independent document work inside checkpoint 1 and per-coverage candidate
  work inside checkpoint 4 may fan out.
- Python does not infer medical/legal facts, coverage applicability, or case
  type from keywords.
- A redaction-blanked fact is absent/uncertain, never a substituted value.
- A completed checkpoint is reused on resume unless invalidated upstream.

## Tests

- Resume from each checkpoint; ensure an existing valid contract is untouched.
- Quote/page mismatch, stale snapshot, invalid UID/source address, and
  structured-output correction exhaustion.
- Adjuster type present/absent and template registry mapping.
- Stable `REQ-N` ordering independent of provider array order.
- A conflicting fact emitted by two document workers reaches the serial
  consolidation as competing evidence; it is never selected by a Python
  tie-breaker.

## Acceptance measurement

First compare the checkpoint-1 460.6s pre-write block and checkpoint-4 113.5s
block separately on one cold identical case. Report input/provider/merge/DAO
time, retries, and semantic differences in all four output contracts before
considering full-stage routing.

**The baseline is not yet measurable, and this plan cannot be accepted until
it is.** `docs/phase1-optimization-plan-2026-08-13.md` states the caveat its
own summary drops: 754.6s is one observation on one case, and the 460.6s/294.0s
split is **derived from file mtimes, not from trace spans**, because the
analysis agents have no intra-stage instrumentation. A mtime-derived figure
records when a file was written, not how long the work took, so comparing a
driver against it would measure the driver honestly against a number that is
not.

Adding intra-stage trace spans is therefore a hard prerequisite of this
pilot, not a reporting nicety — it is the same P0.3 instrumentation gap in a
different place. Until it exists, treat 460.6s as a reason to look here first,
never as the figure a driver is scored against.
