# Activating selective Claim Analysis (routing config v0.1)

`claim_analysis_routing_v0.1.json` ships with `behavior_enabled: false`. While
it is false, Stage 2 classifies exactly as before, `run_claim_analysis.py` runs
its existing two-call spine, and none of the selective contracts are produced.
Nothing about a case's runtime behaviour changes.

Activation is **two edits made together**, and the config schema refuses either
one alone:

```json
{
  "behavior_enabled": true,
  "activation": {
    "approved_by": "<the person who approved it>",
    "authority_role": "<their role, e.g. 손해사정사>",
    "approved_at": "<ISO-8601 timestamp>",
    "scope": "<what was approved, e.g. traumatic injury PoC corpus>"
  }
}
```

Setting `behavior_enabled: true` while `activation` is still `null` fails
validation (`activation: None is not of type 'object'`), and so does a partial
activation block — every one of the four fields is required. This is deliberate:
the flag alone would let the feature switch on with no record of who decided it
or for which cases.

## What must be true before flipping it

The evidence to gather first, none of which the flag itself checks:

1. **A cold synthetic E2E run passes.** `tests/test_selective_pipeline_e2e.py`
   covers Stage 2 classification → Claim Analysis → Consistency Check →
   Screening Report on a fabricated case, including the four fail-closed
   conditions.
2. **A measured comparison on a real case.** Not yet run. Activation should be
   supported by a controlled before/after on the same case — documents read,
   wall time, and whether the fields the current path resolves are still
   resolved. Until that measurement exists, no speedup or recall claim about
   the selective path may be stated as fact.
3. **The medical revision exists for the case.** The result binds to
   `medical_variables.json` by digest, and the DAO refuses the write when that
   revision cannot be resolved.

## What activation does NOT authorize

- It does not enable medical structuring, referral policy, or any P11 gate.
  Those have their own separate approvals and stay disabled.
- It does not permit amount calculation or a final eligibility verdict; both are
  `out_of_scope` in the config and remain so.
- It does not let Claim Analysis write the P6 conflict ledger. Conflict
  candidates stay candidates until `consistency_check` verifies them.

## Rollback

Set `behavior_enabled` back to `false`. The `activation` block may stay — the
schema permits a recorded activation alongside a disabled flag, so the history
of who approved what is not destroyed by turning the feature off.
