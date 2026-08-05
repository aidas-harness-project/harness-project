# Medical Appropriateness Screening Reconstruction Plan

> **Durable control file for Hermes:** Read this file before every resumed session or after context compression. Treat the task table and evidence log as the source of truth. Update them immediately after each RED/GREEN gate. Do not infer status from chat history or obsolete certification reports.

**Goal:** Reconstruct the approved fail-closed medical appropriateness screening harness on a clean branch, preserving earned contracts and tests while excluding accidental whole-harness architecture.

**Target architecture:** One canonical medical-variable publication, one canonical medical-review ledger, one canonical clearance gate, and one bounded downstream outcome projection. All clinical/policy configuration stays disabled. Build one complete synthetic non-UI loop before authentication, API, or UI work.

**Stack:** Python, pytest, JSON Schema Draft 2020-12, existing DAO conventions, FastAPI, React, pnpm.

## Authority order

1. `AGENTS.md`, `CLAUDE.md`, `.agents/skills/harness-guardrails/SKILL.md`, `.agents/skills/harness-guardrails-dev/SKILL.md`.
2. `.hermes/plans/2026-07-23_055253-medical-variable-extraction-hierarchy.md`.
3. `.hermes/plans/2026-07-23_055254-medical-review-request-guardrail-harness.md`.
4. `docs/medical-appropriateness-screening-requirements.md`.
5. `docs/medical-appropriateness-screening-deferrals.md`.
6. This file for execution order and progress.
7. The old feature worktree only as a read-only salvage source.

If an approved plan conflicts with a later artifact, stop and record the conflict. Never guess clinical, role, threshold, activation, or privacy policy.

## Workspaces

### Read-only salvage source

- `/home/toxiclemon/Working/Labs/AIDAS/harness-project`
- Branch `feat/medical-appropriateness-screening`
- Start HEAD `04e017f5ff6b1d33890acf7c01f5f38b74358563`
- Intentionally dirty; do not stash, reset, restore, commit, or edit production files there.

### Sole mutable target

- `/home/toxiclemon/Working/Labs/AIDAS/harness-project-medical-reconstruction`
- Branch `rebuild/medical-appropriateness-screening`
- Base `d9dc310d907d02ae5379fb009a7097fd3a52bbae`

Checkpoint commits are authorized by the user as reconstruction units become GREEN. Pushes remain unauthorized unless separately requested.

## Non-negotiable boundaries

- Never read or modify `source-cases/`, `case_qna.pdf`, `archive/sources/`, `outputs/`, `data/`, runtime ledgers, or answer-key material directly.
- Raw sources are immutable; local Units 1-7 never access ground truth.
- Medical structuring/referral/request/role/projection policies remain disabled.
- Never invent clinical codebooks, thresholds, specialties, actors, expert responses, or approval metadata.
- Do not copy old `tools/dao.py`, `tools/medical_review_ledger.py`, or `frontend/backend/main.py` wholesale.
- Do not restore resume, OCR recovery, intake correction, attachments, terminal attestation, Phase 2, or generic frontend hardening on this branch.
- Do not rebuild multi-certifier fleets or use stale fingerprints as evidence.
- Canonical `.claude` agent/skill definitions are edited first; generated `.agents`/`.codex` copies change only through `python tools/sync_agents.py`.
- Strict TDD: one behavior at a time; observe expected RED before production code; then minimal GREEN and focused regression.

## Preserve / port / rebuild / exclude

### Preserve

- Approved plans, requirement IDs, vocabulary, and explicit deferrals.
- Disabled v0.1 configs.
- Earned medical schemas and synthetic negative cases.
- Canonical lifecycle names and transition vocabulary.

### Port selectively under tests

- Medical contract validation.
- Canonical medical-variable publication and immutable revision identity.
- Medical lifecycle ledger behavior.
- Clearance gate and run-state wait projection.
- Bounded downstream outcome projection.
- Medical API endpoints and agent instruction wiring.
- Operator authentication as a later, narrow prerequisite.
- Useful frontend API/state helpers.

### Rebuild fresh

- Integration branch and reviewable change boundaries.
- Missing medical-structuring integration suite and three synthetic scenarios.
- Missing review-lifecycle integration suite and three synthetic scenarios.
- Complete end-to-end synthetic tracer.
- Purpose-built medical UI with structured forms and evidence navigation.
- Final bounded verification/review evidence.
- Any v0.2 migration, only after separate approval.

### Exclude

- Resume/process lifecycle, OCR/checkpoint recovery, intake/source corrections, attachments, terminal attestation, Phase 2, expanded generic UI, presentations/ZIP/HTML/scratch, and late certifier-only artifacts.

## First vertical loop and QG-MED-1

The first synthetic loop must prove:

1. A schema-valid generated medical-variable candidate enters via the DAO staging boundary.
2. `claim_analysis` publishes one canonical immutable revision.
3. Publication initializes a valid medical-review ledger but does not invent a decision owner or human action.
4. A no-issue case produces an empty valid ledger; an issue without an explicitly opened review item remains a fail-closed coverage error.
5. `check-medical-reviews-clear` blocks missing issue coverage, invalid/stale state, and unresolved items, and clears the valid no-issue revision.
6. Only an authorized in-progress downstream stage can read a bounded schema-valid outcome projection.
7. No clinical policy is enabled, no human action is fabricated, and no protected source is read.

QG-MED-1 is GREEN only after:

- RED was observed on the clean base for missing medical capability;
- the minimal port makes the tracer pass;
- focused contract/publication/lifecycle/gate tests pass;
- configurations remain disabled;
- `git diff --check` passes;
- post-edit evidence is recorded below.

No auth/API/UI work before QG-MED-1.

## Task status

Statuses: `PENDING`, `IN_PROGRESS`, `BLOCKED`, `GREEN`, `DEFERRED`.

| ID | Status | Task | Gate |
|---|---|---|---|
| R00 | GREEN | Create isolated worktree/branch | Correct branch/base/registration and initial cleanliness verified |
| R01 | GREEN | Install authority documents | Reconstruction plan, two approved plans, requirements, and deferrals present |
| R02 | GREEN | Establish clean-base test baseline | 433 Python tests passed before medical production edits |
| R03 | GREEN | Add QG-MED-1 tracer RED | Tracer fails for expected missing medical capability |
| R04 | GREEN | Port minimum schemas and disabled v0.1 configs | Registry loads; all configs structurally disabled |
| R05 | GREEN | Port contract validation | Focused contract tests pass; tracer advances |
| R06 | GREEN | Port canonical publication/revision | Stale/torn/foreign revisions fail closed |
| R07 | GREEN | Port minimal lifecycle ledger | Canonical storage/schema/state validation passes; no pre-auth human mutation interface ported |
| R08 | GREEN | Port clearance gate | Missing/invalid/unresolved/stale block; valid no-issue clears |
| R09 | GREEN | Port downstream outcome projection | Authorized in-progress consumers only; schema-valid v0.1 no-issue output |
| R10 | GREEN | Close QG-MED-1 | Tracer, focused suite, full regression, disabled configs, and diff checks green |
| R11 | GREEN | Build three medical-structuring scenarios | Hierarchy, contradiction/coverage, and quantity/timeline/importance scenarios pass |
| R12 | IN_PROGRESS | Build three authenticated review-lifecycle scenarios | Fourth-review publication serialization, assignment/response replay, and amendment-routing blockers are remediated locally with focused RED/GREEN evidence; full regression and a fresh exact-tree independent approval are still required |
| R13 | GREEN | Add narrow operator-auth prerequisite | Duplicate operator token/actor and role actor/action keys reject deterministically; policy-owned open fails before mutation while policy routing is disabled; third review found no new auth-policy blocker |
| R14 | PENDING | Add medical API endpoints | Blocked on complete authenticated lifecycle and exact public CLI surface; operation IDs are not approved scope |
| R15 | PENDING | Rebuild purpose-built medical UI | Rendered requirements tests and evidence navigation pass |
| R16 | PENDING | Prove report-consumption path | Synthetic outcome reaches authorized report input with provenance |
| R17 | PENDING | Synchronize agent definitions | Canonical edits synced; expected-only generated diff |
| R18 | PENDING | Update truthful docs/deferrals | Implemented, verified, and operational states distinguished |
| R19 | PENDING | Final verification and bounded reviews | Exact final scoped tree passes Python/frontend/build/reviews |
| R20 | IN_PROGRESS | Maintain semantic checkpoint commits | Commit each independently verified reconstruction unit; never push without separate authorization |

## Expected first-loop files

- Create: `tests/test_medical_reconstruction_tracer.py`
- Port/create selectively: `schemas/medical_common.schema.json`
- Port/create selectively: `schemas/medical_variables.schema.json`
- Port/create selectively: required `schemas/medical_review_*.schema.json`
- Port/create: four disabled `config/medical/*_v0.1.json` files
- Port selectively: `tools/medical_contracts.py`
- Port selectively: `tools/medical_review_ledger.py`
- Modify selectively: `tools/dao.py`
- Modify only if required: `schemas/run_state.schema.json`

## Execution discipline

For every behavior:

1. Write one focused test.
2. Run that test and confirm expected RED, not a syntax/setup error.
3. Port/implement only enough for GREEN.
4. Run the focused test and neighboring tests.
5. Refactor only while green.
6. Update this task table and evidence log immediately.

Stop and mark BLOCKED if:

- source worktree changes unexpectedly during a transfer;
- target identity changes;
- an approved behavior requires excluded generic WIP;
- a test requires protected-source access;
- policy/authority would have to be invented;
- the same file hits three failed lint/type repair attempts;
- the tracer requires wholesale monolith transplantation;
- a supposed RED passes immediately.

## Planned synthetic acceptance scenarios after QG-MED-1

Medical structuring:

1. Diagnosis/imaging/treatment hierarchy.
2. Contradiction and source-coverage behavior.
3. Quantity/timeline/importance without clinical inference.

Medical review lifecycle:

1. Tier-A conflict/referral decision boundary.
2. Converging Tier-B evidence and balanced package.
3. Stale revision, additional information, amendment/adjudication, and closure.

All fixtures are synthetic and use closed evidence locators. Never derive them from raw or ground-truth cases.

## Verification commands

```bash
# Target identity
git branch --show-current
git rev-parse HEAD
git status --short
git diff --check

# First tracer
PYTHONDONTWRITEBYTECODE=1 pytest -q tests/test_medical_reconstruction_tracer.py

# Focused medical suite after files exist
PYTHONDONTWRITEBYTECODE=1 pytest -q \
  tests/test_medical_contracts.py \
  tests/test_medical_semantics.py \
  tests/test_dao_medical_variables.py \
  tests/test_dao_medical_review.py \
  tests/test_dao_medical_review_lifecycle.py \
  tests/test_dao_medical_review_recovery.py \
  tests/test_frontend_medical_endpoints.py

# Full regression
PYTHONDONTWRITEBYTECODE=1 pytest -q

# Frontend after intentional pnpm setup
cd frontend/web
pnpm test
pnpm build
```

Never quote pre-edit passes as final evidence.

## Current state

- Initialized: 2026-08-04 Asia/Seoul.
- Active phase: R12 fourth-review blocker remediation and exact-tree recertification.
- Last completed: deterministic publication/supplement race, authoritative assignment/response replay, and amendment action-separation RED/GREEN slices; 71 medical and 504 full tests pass with compile/diff checks clean.
- Next action: freeze the bounded tree, run exact-tree verification/security checks, and obtain independent approval before commit or R14.
- Reconstruction production code written: schemas, disabled v0.1 policies, contract validation, dedicated medical repository, thin DAO publication/read wrappers, fail-closed revision-aware ledger, narrow operator auth, all thirteen declared lifecycle action routes, balanced request creation, assignment/reassignment, expert response/withdrawal/amendment, information/supplement cycles, ordinary/cohort-atomic cancellation, atomic shared adjudication, terminal closure/reopen, pinned authorization snapshots, and authoritative lifecycle/head/wait/adjudication replay.
- Commits/pushes: all four checkpoint candidates were blocked by independent review and unstaged without discarding work; no commits or pushes performed.
- Authority incident: the two approved plan files disappeared from the source worktree before transfer. Exact approved copies were recovered from non-truncated session messages `107110` and `107111`; implementation did not continue from memory or the earlier pre-approval drafts.

## Evidence log

| Time | Task | Command/evidence | Result |
|---|---|---|---|
| 2026-08-04 08:28 KST | Preflight | branch/HEAD/worktree inventory | Source `feat/medical-appropriateness-screening@04e017f`; base `main@d9dc310`; target branch absent |
| 2026-08-04 08:29 KST | R00 | `git worktree add -b rebuild/medical-appropriateness-screening ... d9dc310` plus identity checks | Target registered at exact base and initially clean; source HEAD/fingerprint preserved |
| 2026-08-04 08:30 KST | R01 | copied control plan, two approved plans, committed requirements, and committed deferrals | Five authority documents present; no production files copied |
| 2026-08-04 08:31 KST | R02 | `PYTHONDONTWRITEBYTECODE=1 pytest -q` | 433 passed in 1.76s on clean base |
| 2026-08-04 08:44 KST | R01 recovery | restored non-truncated approved session messages `107110` and `107111` | Variable plan: 592 lines, SHA-256 `3b05a8d8dc5f7782eda395f39b2dcf26b8490260712c3e62a8c3040ed7f43372`; review plan: 812 lines, SHA-256 `8aac9e349c94bf1f5a95174af7f566af22609ce202fe1abfcdfdd3f2bb405188` |
| 2026-08-04 | R03 | `PYTHONDONTWRITEBYTECODE=1 pytest -q tests/test_medical_reconstruction_tracer.py` | Expected RED: collection failed only because `medical_contracts` is absent |
| 2026-08-04 | R04 RED/GREEN | `pytest -q tests/test_medical_policy_defaults.py` before/after 17 schemas and five v0.1 configs | RED: missing config; GREEN: 1 passed; all enable flags false, approvals null, no v0.2 |
| 2026-08-04 | R05 RED/GREEN | restored earned contract tests before `tools/medical_contracts.py`; then ran focused contract/policy suite | RED: missing module; GREEN: 9 passed; tracer next fails only on absent `medical_review_ledger` |
| 2026-08-04 | R06 RED/GREEN | restored publication tests before implementation; added dedicated `medical_repository.py` and thin DAO wrappers | GREEN: 6 passed in `tests/test_dao_medical_variables.py`; issue without explicit review item now blocks as `missing:MCI_0001` |
| 2026-08-04 | R07 boundary | old DAO lifecycle test required a caller-provided `local_actor_file` | Not ported: authenticated identity is R13. Retained canonical ledger schema/semantic validation and state vocabulary; no human action synthesized. |
| 2026-08-04 | R08 GREEN | no-issue DAO clearance plus missing/malformed/coverage/revision tests | 19 focused tests passed; stale terminal event SHA blocks while matching SHA clears |
| 2026-08-04 | R09 RED/GREEN | added authorized outcome test before DAO command/schema | RED: absent command; GREEN: authorized matching in-progress stage reads v0.1 no-issue projection, other callers block |
| 2026-08-04 | R10 QG-MED-1 | tracer, focused suite, full suite, CLI, compile, diff check | tracer 2 passed; focused 29 passed; full 464 passed; path/CLI/tracer subset 19 passed; compile and `git diff --check` clean |
| 2026-08-04 | R11 RED/GREEN | three fresh synthetic structuring scenarios | RED: summary count 3 incorrectly accepted for two source counts of 1; GREEN: 11 focused tests pass with deterministic source-count validation |
| 2026-08-04 | R13 RED/GREEN | medical-only operator-auth policy/verifier | RED: `operator_auth` absent; GREEN: 6 focused tests; shipped policy disabled/unapproved; schema permits only `medical_review` |
| 2026-08-04 | R12 partial GREEN | authenticated open, structured Tier-B do-not-refer decision, and stale/additional-information rebinding | 3 fresh scenarios pass; duplicate/unauthenticated opens block; revised issue evidence remains stale until authenticated rebind; no expert response fabricated |
| 2026-08-04 | Regression checkpoint | `PYTHONDONTWRITEBYTECODE=1 pytest -q`; compile; `git diff --check` | 476 passed in 3.57s; compile and diff checks clean |
| 2026-08-04 | R12 request/expert seam | fresh balanced refer and authenticated coordinator/reviewer scenarios | Explicit refer rejection observed RED; balanced revision/config-pinned request, named assignment, completed response, and append-only amendment GREEN; 23 focused tests pass |
| 2026-08-04 | R12 closure seam | authenticated coordinator closes an answered amended response | Expected RED at inaccessible `close`; GREEN with nonblank disposition, resolved coordinator wait, terminal `closed`, and clear downstream gate; 23 neighboring tests pass |
| 2026-08-04 | R12 shared adjudication GREEN | two answered same-issue response heads move atomically through one shared adjudication and terminal disposition | Duplicate-open and inaccessible-action REDs observed; exact response/revision/role checks, one adjudication/wait, per-item events, malformed-history rejection, and stale-cohort close protection GREEN; 40 medical-neighborhood tests and 479 full-suite tests pass; compile and diff checks clean |
| 2026-08-04 | R14 canonical read seam | authenticated ledger read and request/version/revision-pinned evidence read | Missing-command RED observed; exact CLI surface, canonical validation, immutable revision lookup, included-locator enforcement, and unlisted-locator rejection GREEN; 2 focused tests pass |
| 2026-08-04 | First pre-commit review | independent exact-worktree review after 479 full-suite pass and clean staged scope/security scan | BLOCKED: generic medical-owned read/write bypasses, incomplete lifecycle replay, unconstrained medical ingress, stranded expert-information state, and one whitespace defect; staged bytes were safely unstaged |
| 2026-08-04 | Pre-commit ownership remediation | generic text writes and generic ledger/revision reads across existing/missing protected targets | 8 expected REDs observed; centralized medical ownership guards now reject all generic writes before input/lock access and reject protected reads before existence/validation disclosure; 8 focused tests GREEN |
| 2026-08-04 | Pre-commit replay remediation | forged opening transition, broken mid-chain source state, and mismatched response actor/object binding | Reviewer probe reproduced RED; global event IDs/counter, per-item origin/chain/action legality, authenticated assertions, and assignment/response/adjudication bindings now replay from immutable ledger history; 81 medical/DAO neighborhood tests GREEN |
| 2026-08-04 | Pre-commit ingress remediation | valid private ingress plus outside-root, oversized, symlink, and non-private-root probes | 5 expected REDs observed; referral/transition submissions now require an owner-only canonical ingress root, `O_NOFOLLOW`, regular owner file, one link, and a descriptor-bounded 256 KiB read; trusted request config remains on the separate secure config loader; 86 neighborhood tests GREEN |
| 2026-08-04 | Pre-commit supplement remediation | assigned response requests more evidence, coordinator appends package version 2 | Expected unreconstructed-action RED observed; immutable source reinspection/package version, inactive old heads, resolved evidence wait, and one assignment wait GREEN; 87 medical/DAO neighborhood tests pass; trailing-whitespace finding removed |
| 2026-08-04 | R14 cancellation RED/GREEN | ordinary decision wait cancellation plus pending shared-adjudication cancellation | Unreconstructed/nonshared failures observed; coordinator reason required, every active wait resolves `no_longer_required/cancelled`, active heads clear, and exact adjudication ID cancels the complete cohort with per-item events; 9 lifecycle scenario tests GREEN |
| 2026-08-04 | Remediated checkpoint regression | full pytest, in-memory Python compile, and `git diff --check` | 495 passed in 5.59s; compile and diff-quality gates PASS |
| 2026-08-04 | Second independent checkpoint review | exact 54-path staged tree after first-review remediation | NOT APPROVED: accepted historical owner/wait/head/cancelled-adjudication tampering; incomplete 13-action CLI/implementation surface; false source-reinspection supplementation; duplicate token/action policy keys; disabled-policy item stranding. Index unstaged without discarding bytes; R12/R13 downgraded |
| 2026-08-04 | Second-review blocker remediation | focused REDs plus full medical/canonical regression | Embedded validated role-policy snapshots and decision/request/assignment/response event IDs; owner/auth/request/assignment/response/wait/cancelled-adjudication tamper rejection; all thirteen action choices with generic provide-information and four reconstructed actions; `performed=true` supplementation; duplicate policy-key rejection; disabled policy ownership rejection. 67 medical tests and 500 full tests passed; compile and `git diff --check` PASS |
| 2026-08-04 | Third independent checkpoint review | exact 54-path staged tree `1e182c0927c3865864cf71f1e69a482f110e6c1f`, patch SHA-256 `2e5974bb15b140f0ca31132aacbe37db1e9ae2fb362b010fb1b711394563b156` | NOT APPROVED: decision origin/actor/policy and mandatory reinspection remained rewriteable; required waits and three DAO counters were not replayed; P7 run-state projection/reconciliation was absent; submit-response still doubled as request-information. Index unstaged without discarding bytes; no commit made |
| 2026-08-04 | Third-review decision/reinspection remediation | reviewer’s fabricated-policy and `performed=false` probes captured as focused REDs | Replay now binds human decision origin/actor/null policy, exact event/revision reinspection, request policy, and mandatory completed referenced reinspections; focused refer scenario GREEN |
| 2026-08-04 | Third-review wait/counter remediation | deleted/rewritten mandatory wait plus three corrupted-counter REDs | Event-derived wait creation identities and all DAO counters fail closed; complex information/reopen/withdraw/shared-adjudication scenarios GREEN |
| 2026-08-04 | P7 projection remediation | missing projection RED, recovery/idempotency and held-lock partial-failure scenarios | Every successful purpose-built mutation projects canonical waits after ledger unlock; explicit `reconcile-medical-review-waits` repairs drift while preserving unrelated history; partial projection failure warns without erasing ledger success |
| 2026-08-04 | Strict response-action remediation | completed response with nonempty `additional_evidence_needed` reproduced old `expert_needs_information` route | `submit_response` now always enters `answered` with coordinator disposition; `request_information` is the sole explicit expert information-request transition |
| 2026-08-04 | Third-review remediation regression | `PYTHONDONTWRITEBYTECODE=1 pytest -q tests/test_medical_*.py tests/test_dao_medical_*.py`; full `pytest -q`; `git diff --check` | 70 medical tests and 503 full tests passed; diff-quality gate PASS |
| 2026-08-04 | Projection concurrency cold-read | deterministic L1-waiting/L2-cancelled reconciliation ordering probe | RED proved a slow reconciler could overwrite newer L2 projection with stale L1 after waiting for the run-state lock; moving canonical ledger read inside the acquired projection lock made the race GREEN |
| 2026-08-04 | Supplemental reinspection cold-read | schema-valid supplemental `actor_or_process` rewrite probe | RED proved a supplemental record was only completed/referenced, not event-bound; supplement events now persist request ID and replay binds exact next request version, reinspection actor/time/revision, and source ID |
| 2026-08-04 | Post-cold-read regression | medical-focused suite, full suite, diff quality | 70 medical tests and 503 full tests passed; `git diff --check` PASS |
| 2026-08-04 | Source-reference closure cold-read | rewrite initial request source from `S01` to schema-valid nonexistent `S99` | RED proved references were not required to resolve; replay now enforces bidirectional exact resolution and binds the initial request version to the decision source/time/revision |
| 2026-08-04 | Fourth independent checkpoint review | exact 55-path staged tree `3d2946434c372a3691c6714f4e4326d83650f0b3`, patch SHA-256 `1e1fc66907641a44172b1e07115a6580daeb6b9420cb715a39bf90248a551d63` | NOT APPROVED: canonical publication raced lifecycle mutation; responses did not bind active assignment/request/evidence/decision; assignment policy provenance was mutable; schema-valid evidence-bearing amendment was false-rejected. Index unstaged without discarding bytes; no commit made |
| 2026-08-04 | Fourth-review graph/action remediation | exact reviewer response/assignment tamper probes plus evidence-bearing amendment | REDs reproduced; assignment event/policy/actor/time/reviewer and response event/active assignment/request version/decision/evidence/config metadata now replay exactly; amendments remain answered and `request_information` remains the sole information-request transition |
| 2026-08-04 | Fourth-review publication serialization | deterministic paused-publication/concurrent-supplement public-command probe | RED proved publication and supplementation both returned success and stranded an old-revision package; publication now holds variable then ledger locks through acceptance and canonical commit, so the later supplement fails stale and the item remains recoverable; 71 medical tests pass |
| 2026-08-04 | Fourth-review remediation regression | full `pytest -q`, changed Python compile, `git diff --check` | 504 tests passed; compile and diff-quality gates PASS |
| 2026-08-05 | Twelfth independent checkpoint review | exact staged tree `f7159deccb47fb103d4773af20b85bd4809d4b66`, patch SHA-256 `1c5ca4587922c6ec3ca6eac4b005fe080cc1dcc827023a72953b2c4baaf2b9f5` | NOT APPROVED: post-replace durability failure could roll back ledger/revision while leaving canonical; approved table-only evidence was rejected; an old resolved adjudication could strand a later cancelled cycle. Index unstaged without discarding bytes; no commit made. |
| 2026-08-05 | GREEN: twelfth-review durability/table/adjudication blockers remediated | Added typed post-replace committed-state handling, initial-publication retry preservation, existing-ledger propagation compatibility, schema-valid table-only canonical publication with conservative legacy omission, and current-cycle-only adjudication freshness; focused tests passed, full live and exact-tree archive suites each passed 526 tests, staged Python/JSON parsing and diff checks passed, and thirteenth exact-tree review dispatched against tree `c46d60ef43930961ee1885c4ace652734df9b979` | `tools/dao.py`; `tools/medical_repository.py`; `tools/medical_contracts.py`; `tools/medical_review_ledger.py`; focused tests; tree `c46d60ef43930961ee1885c4ace652734df9b979` |
| 2026-08-05 | REJECT: thirteenth independent exact-tree review found the requirements authority truncated | Reviewer proved `docs/medical-appropriateness-screening-requirements.md` contained a literal output-truncation marker replacing about 20 KB of normative requirements, so the artifact was incomplete despite 526 passing tests | independent review `deleg_e0765325`; rejected tree `c46d60ef43930961ee1885c4ace652734df9b979` |
| 2026-08-05 | GREEN: complete requirements authority restored from exact committed provenance | Compared the staged head and tail against both the live original worktree and Git history. The staged head matched committed `244467c` through the truncation boundary and diverged from the later live UI-updated copy at character 219; the staged tail matched the committed blob through EOF. Restored the complete `244467c`/`04e017f` blob byte-for-byte (`70389` bytes, SHA-256 `a970d1dbed25c7fbd9cb624265191ef0c784ca7a68138db18c4cdfd665dc3b45`) to preserve this checkpoint's approved non-UI scope without importing later UI/redaction claims. | `docs/medical-appropriateness-screening-requirements.md`; source commit `244467c16cd6d4ec308ab22d979257d18a92c9a7` |
| 2026-08-05 | REJECT: fourteenth independent exact-tree review found generic lock acquisition escaped through a raced ancestor | Reviewer replaced an accepted generic ancestor with a symlink to `_medical_variable_revisions` after the generic guard and observed the pathname-based writer create `<digest>.json.lock` inside the protected namespace before the descriptor-anchored payload rejected the path | independent review `deleg_cac66f9b`; rejected tree `e86b138c31e854ffe4123cf68a63ee6d1057f7cf` |
| 2026-08-05 | GREEN: generic lock creation, ownership, and payload parent identity are descriptor anchored | Added a shared no-follow ancestor walker and an anchored lock token retaining parent FD plus lock/parent inode identities. Generic JSON/text writers now acquire and release compatible `.lock` files descriptor-relatively, preserve replacement lock inodes, handle FIFO collisions nonblocking, and bind payload publication to the locked parent identity before and after commit. Deterministic RED tests observed escaped locks for both writers and false success after a real-parent replacement; all are GREEN and the affected lock/path modules pass 40 tests. | `tools/dao.py`; `tests/test_dao_locking.py`; `tests/test_dao_write_text.py` |
| 2026-08-05 | REJECT: fifteenth independent exact-tree review found closure retained an active assignment head | Reviewer extended the authenticated assignment → response → amendment → close scenario and proved the canonical closed request retained `current_assignment_id = MRR_0001-A01`, violating the approved sole-active-assignment-pointer rule | independent review `deleg_38571bca`; rejected tree `b88fe29dbbda1fa5e71a6d54fc6c0471b015eab6` |
| 2026-08-05 | GREEN: closure clears assignment head and replay enforces/reactivates it correctly | Added deterministic RED assertions that canonical closure clears `current_assignment_id` while preserving the active response head and that semantic validation rejects a restored active assignment on a closed ledger. The close mutation and lifecycle replay now clear assignment heads; sibling testing exposed and fixed the corresponding `closed → amend_response/withdraw_response` replay path so immutable response assignment provenance reactivates the correct head. Exact scenario passed and lifecycle/shared-adjudication/ledger modules passed 18 tests. | `tools/medical_review_ledger.py`; `tests/test_medical_review_lifecycle_scenarios.py` |

## Decision log

| ID | Decision | Basis |
|---|---|---|
| D-R01 | Use a sibling worktree | Subtracting from the large dirty source tree is unsafe |
| D-R02 | Base on exact local `main@d9dc310` | It is the known clean merge base |
| D-R03 | Old medical code is a behavioral oracle, not a transplant unit | Focused tests pass but implementation is entangled with generic monolith changes |
| D-R04 | First gate is one complete synthetic non-UI loop | Proves publication/lifecycle/gate/outcome before dependencies and UI |
| D-R05 | Commit verified reconstruction units; do not push | User explicitly authorized commits along the way on 2026-08-04; publication still requires separate authorization |
| D-R06 | Publication does not auto-open issue items | Approved R2 requires a declared decision owner; the DAO must not invent `policy` or `human`. Missing issue coverage blocks clearance until an explicit open action occurs. |
| D-R07 | Pre-auth lifecycle mutation waits for R13 | The old `local_actor_file` assertion is audit metadata, not authentication. QG-MED-1 uses a no-issue path; human transitions will be rebuilt with authenticated identity in R12-R13. |
| D-R08 | Execute R13 before R12 | Authenticated lifecycle scenarios depend on trustworthy operator identity; reversing the table order avoids rebuilding the rejected caller-asserted actor path. |
| D-R09 | Allow revision supersession only from information-wait states | New canonical evidence must be publishable for `needs_information`/`expert_needs_information`; all other unresolved states still block supersession, and clearance stays stale-blocked until explicit authenticated rebind. |
