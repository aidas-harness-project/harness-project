# PR Readiness Review: Out-of-Scope Findings

Date: 2026-08-12
Branch: `rebuild/loss-adjustment-format-study`

## Purpose

This document preserves findings from the fresh PR-readiness review that are
not caused by, and are not directly addressed by, the loss-adjustment format
study reconstruction. The two findings that were in scope were remediated in
the branch: structured draft rendering now reads only the canonical
DAO-governed case/version contract, and narrative rendering can no longer skip
citation-quote verification because its output path lacks a case identity.

## Deferred findings

### 1. Full-suite tests depend on absent ignored case outputs

Some repository-wide tests expect case artifacts under ignored `outputs/`
paths that are not present in a clean checkout. These failures are unrelated to
the format-study files and focused pipeline contracts changed on this branch.

Follow-up owner/action: make those tests construct isolated fixtures through
the DAO, or explicitly provision their required integration-test corpus before
the suite runs.

### 2. Some tests require write capabilities unavailable in the restricted sandbox

Repository-wide runs produced errors when tests attempted scratch or
capability writes that the restricted review sandbox does not permit. The same
environment also emits pytest cache warnings because the linked worktree is
read-only to the sandbox profile.

Follow-up owner/action: separate environment-dependent integration tests from
hermetic unit tests and document the required writable locations/capabilities
for the former.

### 3. Baseline dependency gap: `rfc3339-validator`

One unchanged `origin/main` test path requires `rfc3339-validator`, but that
package is not available in the review environment. This was present before
the branch diff and is not introduced by the format reconstruction.

Follow-up owner/action: declare the dependency in the repository's test
environment or remove the implicit dependency from the baseline test path.

### 4. Review volume is high but already partitioned

At the readiness review point, the branch comprised 35 text files and roughly
9,641 additions / 65 deletions. The reconstruction was split into nine
coherent commits; the scoped readiness remediation is one additional atomic
commit. Reviewers should still expect a larger-than-usual documentation and
generated-artifact pass.

Follow-up owner/action: preserve the current commit boundaries and use the PR
description to route reviewers separately through corpus evidence, executable
authoring contracts, pipeline integration, and hardening.

## Recorded verification context

- Fresh-agent focused gate: 126 passed.
- Earlier focused remediation gate: 158 passed.
- Post-remediation branch-focused gate: 129 passed.
- Post-remediation repository-wide run in the restricted worktree: 1,826
  passed, 40 failed, 21 errors, 29 skipped. The failures remain in the
  documented fixture, capability/scratch-write, and baseline-environment
  categories; the three additional passes are the new renderer regressions.
- Escalated repository-wide run: 1,849 passed, 21 failed, 14 errors, 29 skipped.
- Fresh-agent repository-wide run: 1,823 passed, 40 failed, 21 errors, 29 skipped.

The differing repository-wide totals reflect environment/fixture availability,
not a claim that the full suite is green. PR readiness should therefore be
decided from the branch-focused gates plus an explicit CI run in the canonical
test environment; the baseline issues above remain visible rather than being
misrepresented as branch regressions.
