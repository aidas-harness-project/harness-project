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
| R12 | GREEN | Build three authenticated review-lifecycle scenarios | Full 529-test exact tree received sixteenth independent approval and was committed as `ed25e37` |
| R13 | GREEN | Add narrow operator-auth prerequisite | Duplicate operator token/actor and role actor/action keys reject deterministically; policy-owned open fails before mutation while policy routing is disabled; third review found no new auth-policy blocker |
| R14 | GREEN | Add medical API endpoints | Authenticated revision/ledger/evidence reads and 13-action DAO mutation boundary pass focused tests; canonical state validation stays DAO-authoritative so caller-retained operation IDs support exact lost-ack retry |
| R15 | GREEN | Rebuild purpose-built medical UI | Purpose-built timeline/conflict/package/response UI, issue selection, complete event history, static fail-closed notice, rendered contract tests, builds, and browser QA pass |
| R16 | GREEN | Prove report-consumption path | Authorized agents gate on clearance and consume bounded outcomes with attribution/provenance |
| R17 | GREEN | Synchronize agent definitions | Canonical edits synchronized; generated mirrors match their canonical definitions |
| R18 | GREEN | Update truthful docs/deferrals | Requirements, deferrals, pipeline, and README distinguish implemented, synthetic-verified, and disabled operational states |
| R19 | GREEN | Final verification and bounded reviews | Exact final scoped tree passes Python/frontend/build/reviews |
| R20 | GREEN | Maintain semantic checkpoint commits | Commit each independently verified reconstruction unit; never push without separate authorization |

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
  tests/test_medical_*.py \
  tests/test_dao_medical_*.py \
  tests/test_frontend_medical_endpoints.py

# Full regression
PYTHONDONTWRITEBYTECODE=1 pytest -q

# Frontend after dependency setup
cd frontend/web
node --test src/*.test.js
npm run build
```

Never quote pre-edit passes as final evidence.

## Current state

- Initialized: 2026-08-04 Asia/Seoul.
- Active phase: R19/R20 complete; branch ready for PR handoff.
- Last completed: final persistence and whole-change confirmation closed on exact tree `9a6262652626f123481af7c1c35a765089288197`; the implementation checkpoint was committed without changing that tree.
- Next action: push the branch and create a PR when separately authorized.
- Reconstruction production code written: schemas, disabled v0.1 policies, contract validation, dedicated medical repository, thin DAO publication/read wrappers, fail-closed revision-aware ledger, narrow operator auth, all thirteen declared lifecycle action routes, durable operation replay, balanced request creation, assignment/reassignment, expert response/withdrawal/amendment, information/supplement cycles, ordinary/cohort-atomic cancellation, atomic shared adjudication, terminal closure/reopen, pinned authorization snapshots, authoritative lifecycle/head/wait/adjudication replay, authenticated localhost API, purpose-built UI, and bounded report-consumption instructions.
- Commits/pushes: sixteenth-reviewed core checkpoint `ed25e37`; final reviewed implementation checkpoint `633bd7b`; current closeout changes are documentation-only. No push performed.
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
| 2026-08-05 | APPROVE: sixteenth independent exact-tree review and semantic checkpoint | Exact tree `298eeb3a509a6da475aa18f2ff818fb89b223297`, patch SHA-256 `63d7dbdd632b90d0aee66f93d87b886c7e4770ce6375743c19fc235c3bfe0cc8`, 529 tests, static/security checks, and no overlap passed; committed without push as `ed25e37` | independent review `deleg_af88b319`; commit `ed25e37ed28a` |
| 2026-08-05 13:01 KST | R14-R15 GREEN | Eight authenticated backend tests, 537 full Python tests, two frontend tests, lint with two baseline warnings, production/static builds, and live browser QA with zero console errors | Narrow API, structured UI, exact evidence coordinates, disabled-policy auth gate; no operation IDs or generic frontend hardening |
| 2026-08-05 13:01 KST | R16-R18 GREEN | Report-agent consumption RED/GREEN, 15 sync tests, documentation truthfulness RED/GREEN, and generated-scope audit | Three canonical agents consume bounded outcomes; only their three Codex mirrors changed; docs preserve disabled operational boundary |
| 2026-08-05 13:20 KST | OPEN: first R14-R18 candidate failed self-review before commit | In-memory schema-valid foreign request/version assignment reached the DAO wrapper, violating the approved pre-subprocess current-request gate. UI audit also found missing issue selection/event history/safety/localhost banners and a static snapshot token dead end; frontend README and known-gaps lacked the activation boundary. | Rejected staged tree `3abf14ce4bf692861794d47cd6bcdcf407882630`; no commit |
| 2026-08-05 13:24 KST | GREEN: R14-R18 preflight/UI/static/docs blockers remediated | Added current request/version/response/evidence/adjudication preflight, explicit review-item selection and event history, human-interpretation and localhost banners, static-mode unavailability, and truthful frontend/gap docs. Focused Python and Node RED/GREEN tests pass. | `frontend/backend/main.py`; medical UI/client; focused tests; `frontend/README.md`; `known-gaps.md` |
| 2026-08-05 13:31 KST | GREEN: replacement ownership hardened against inode reuse | Concurrent final verification made two existing replacement-preservation tests fail because device+inode ownership could alias a newly created replacement after inode reuse. Generic locks and descriptor-created files now capture post-write device/inode/ctime/size identity; both deterministic tests pass. | `tools/dao.py`; existing replacement-ownership tests |
| 2026-08-05 13:40 KST | GREEN: superseded four-review findings remediated | Forwarded bearer context into every medical DAO read; bound transitions to the canonical revision run owner; added request/version preflight; made reviewed decision/response/adjudication outcomes reachable after clearance with attribution/provenance; added required reassign/withdraw reasons, complete response rendering, multi-item selection/history, explicit non-adverse/localhost banners, and fail-closed static mode. Full live suite: 540 passed; frontend tests/builds passed. | Superseded review `deleg_727dfa29`; no commit from rejected tree |
| 2026-08-05 13:55 KST | GREEN: authoritative run ownership and lock-window parity | The DAO mutation wrapper now serializes on run state before any ledger write, bootstraps an absent owner only from the exact canonical medical revision, rejects stale owners before invoking mutation code, and retains the owner lock through the canonical ledger commit. Open/decision/transition also recheck revision ownership under the ledger lock. FastAPI mutation timeout now exceeds the DAO's 900-second maximum wait. Deterministic lock-held/stale-owner/timeout tests pass; full live suite: 541 passed. | Superseded review `deleg_727dfa29` remaining findings |
| 2026-08-05 14:09 KST | GREEN: stale `3a98e9` review findings remediated | Replaced inferred response/package timestamps, blanket evidence declarations, package confirmations, source reinspection, and assigning identity with explicit operator inputs plus server-derived actor identity; cleared stale UI state; rendered complete request/response/event provenance; pinned balanced evidence/navigation to the request package and canonical nested locator shape. Serialized outcome reads in publication lock order, added a deterministic publication interleaving regression, bound decisions/adjudications to the current lifecycle cycle and current response cohort, exposed adjudicated response IDs/adjudicator provenance, promoted the deleted reopen/cancel probe into repository tests, and corrected the obsolete read-only/future-mutation documentation row. Focused Python and frontend tests/builds pass; full live suite passed 543 tests, all four frontend tests, lint with two pre-existing warnings, production/static builds, 15 synchronization tests, compilation, and diff checks. | Superseded review `deleg_48af7717`; replacement review required after freeze |
| 2026-08-05 14:32 KST | GREEN: stale `8e2d31` replacement-owner/UI-display findings remediated | A deterministic RED proved the authoritative mutation wrapper deleted a replacement run-state lock. Added identity-preserving owned pathname locks using device/inode/ctime/size and switched only the medical run-owner boundary to exact-owner release. Added canonical per-item/cohort wait-status rendering and request source-coverage limitations. The reviewer’s request to add open-item/referral-decision mutation UI was not adopted because the approved later R14 boundary permits exactly three routes and thirteen public actions; adding those mutations would be scope creep. Focused lifecycle/backend tests and all frontend tests/lint/builds pass; full live suite passed 544 tests, four frontend tests, 15 synchronization tests, compilation, and diff checks. | Superseded review `deleg_abae33f3`; current `371d62e` review invalidated by remediation and must be replaced |
| 2026-08-05 14:52 KST | GREEN: stale `371d62e` security/concurrency/frontend findings remediated | Blocked medical-owned contracts through unauthenticated generic contract/report routes using the canonical protected-target registry; bound private ingress cleanup to the created file identity; expanded HTTP timeout to the two sequential 900-second lock budgets plus margin; ignored historical resolved adjudications unless they bind the closing item’s current response; loaded medical variables from each request-pinned revision; fenced case/token/item/evidence async generations and validated returned evidence coordinates; rendered complete request, assignment, response, and source-reinspection provenance. Deterministic RED/GREEN tests cover each backend defect and pure frontend helpers cover revision/coordinate selection. Full live suite passed 546 tests, four frontend tests, lint with two pre-existing warnings, production/static builds, 15 synchronization tests, compilation, and diff checks. | Superseded review `deleg_05a95c35`; active `6bd1a9c` review invalidated and must be replaced |
| 2026-08-06 01:06 KST | OPEN: exact-tree review rejected `4abe7497` | Frontend/API review found stale action completion could strand `busy`, stale success skipped canonical reconciliation, projection-pending notice was cleared by reload, and canonical provenance fields were omitted or abbreviated. Lifecycle/outcome review found missing-mode legacy fail-open, explicit legacy rejection, missing decision authority, missing assignment role-policy provenance, and incomplete R19 documentation. Backend/security and whole-change tasks were provider-filtered and supplied no verdict. Opening/closing identity matched tree `4abe74973f5a385250a52399b4ecf750858907f6`, patch SHA-256 `91edb45b2cd524aebda3f231b5ed6a4978c7ceed77b9b5482567f347ffbde615`, 39 paths, with zero overlap; no commit authorized. | `deleg_f09f291f` |
| 2026-08-06 01:28 KST | GREEN: final review blockers remediated | Added observable RED coverage, generation-owned busy release, stale-success canonical reconciliation preserving current item selection, persistent projection-pending notice, and complete structured non-JSON rendering for canonical request/assignment/response/reinspection/evidence/wait/event records including role-policy snapshots. Legacy reads now require explicit `legacy_pre_medical` and reject any post-adoption authority; bounded outcomes include referral-decision authority and assignment role-policy provenance; schemas and all three report agents were aligned. Canonical P5 and generated mirrors now describe persistent descriptor-held advisory locks instead of deleted O_EXCL pathnames. | Review blockers plus D4 guardrail drift |
| 2026-08-06 01:31 KST | GREEN: post-remediation live verification | Full Python suite passed 552 tests. All five frontend tests passed; Oxlint reported zero errors and two pre-existing unrelated warnings; Vite production build passed. Agent synchronization check, Python compilation, and `git diff --check` passed. | Replacement exact-tree freeze/review still required before R20 |
| 2026-08-06 01:39 KST | OPEN: exact-tree review rejected `79285604` | Two reviewers proved all three replaceable sidecar lock domains could admit simultaneous owners; generic release was not PID/thread scoped. Frontend review found stale projection-pending notice loss, wrong-case transient actionability, and hidden history without a current request. Lifecycle review reproduced a pre-information decision revived after cancellation/closure and a stale shared adjudication retained for one participant after another participant amended. Whole-change review also found the documented `pnpm test` command was not executable and could not close identity after disposable cleanup was denied. Three reviews closed identity unchanged; the fourth remained OPEN without a closing gate. No commit authorized. | `deleg_e89359f5` |
| 2026-08-06 02:00 KST | GREEN: `79285604` review blockers remediated and live verification passed | Added a non-replaceable abstract UNIX-domain ownership token to generic, run-owner, and descriptor-anchored lock domains; made generic release PID/thread scoped; discarded inherited fork handles without unlocking parent ownership; and added replacement/thread/fork regressions. Preserved stale projection-pending warnings, case-bound and generation-fenced loads, keyed medical panels, and canonical history without a current request. Reset decision cycles at `provide_information` and require every shared-adjudication participant response to remain current. Corrected the executable frontend command. Full live suite passed 558 Python tests and six frontend tests; lint reported zero errors and the same two unrelated warnings; production build, synchronization, compilation, and diff checks passed. | Replacement exact-tree freeze/review still required before R20 |
| 2026-08-06 02:23 KST | OPEN: exact-tree review rejected `058124ec` | Frontend/API, lifecycle/docs/generated, and independent whole-change reviews returned CLOSED. Backend review reproduced a fork handoff gap across generic, run-owner, and descriptor-anchored locks: an abstract token bound by `_try_kernel_lock` was not registered until the later file-lock handle existed, so a long-lived child forked between those steps retained the socket after parent release. Opening and closing identity matched tree `058124ec9d9dd125d8de757a705f6849db091b32`, patch SHA-256 `3c973f6c27ff05424c239e9e9af71246c8784842c017e793f485742f6cbb25c5`, 45 paths, status SHA-256 `732795d5a0560423d03f4a6593da9f95e8a89492071db39b5aec4fe522024500`, and zero overlap. No commit authorized. | `deleg_d844f242` |
| 2026-08-06 02:29 KST | GREEN: fork handoff blocker remediated | Added a fork-guarded active-token registry inside `_try_kernel_lock`, before caller handoff, so child cleanup sees tokens even before generic/owned/anchored handle registration. Added a deterministic three-family RED/GREEN regression that holds the child alive, releases the parent, and requires immediate reacquisition. Full lock/manifest focused suite passed 30 tests; medical replacement-owner mutation regression and compilation/diff checks passed. | Full live and exact-index verification plus four replacement CLOSED reviews still required |
| 2026-08-06 02:30 KST | GREEN: post-handoff-remediation live verification | Full Python suite passed 561 tests. All six frontend tests passed; Oxlint reported zero errors and the same two unrelated warnings; Vite production build passed. Agent synchronization check, Python compilation, and `git diff --check` passed. | Replacement exact-tree freeze/review still required before R20 |
| 2026-08-06 11:50 KST | OPEN: replacement review rejected `c475b38e` | Two reviewers were provider-filtered and could not count. The lifecycle/docs reviewer proved the documented R19 focused command named three nonexistent test files and the current-state summary still described the 537-test pre-freeze state. The frontend reviewer’s live transcript additionally found static mode returned synthetic success for upload/run and rendered generic mutation controls; its final response was replaced by an ad-hoc verification message and could not count as a closing verdict. The valid lifecycle review opened and closed on tree `c475b38e4e7a455c382f374cd98b14ff8b7f1a86`, patch SHA-256 `ea32951a22230f9993244d91eb1f03ca0d4716f0f80bd5905e3c6a153af6f3e6`, 45 paths, status SHA-256 `732795d5a0560423d03f4a6593da9f95e8a89492071db39b5aec4fe522024500`, and zero overlap. No commit authorized. | `deleg_586a0d56` |
| 2026-08-06 11:58 KST | GREEN: R19 documentation and static fail-closed blockers remediated | Replaced nonexistent R19 test paths with executable medical globs and updated the current state. Static API mutation methods now reject; New Case is omitted; source/conflict ledger, OCR, and human-gate panels suppress mutation controls while retaining read-only rendering. Added a seven-test frontend regression. The documented focused command passed 112 tests; full Python passed 561; seven frontend tests, lint with zero errors/two unrelated warnings, production/static builds, synchronization, compilation, and diff checks passed. Removed the reviewer’s disposable sibling after its cleanup had been denied. | Replacement exact-tree freeze/review still required before R20 |
| 2026-08-06 12:23 KST | OPEN: exact-tree review rejected `0c333dfa` | Frontend/API and whole-change reviews returned CLOSED. Backend review reproduced foreign run ownership during wait reconciliation and medical publication plus torn/non-serialized snapshots lacking final run state and a completion manifest. Lifecycle/docs review proved the authoritative command block used nonexistent `--actor-file`/`--decision-file` forms, omitted `--action`, `provide-medical-review-information`, and bounded outcomes. Opening/closing identity matched tree `0c333dfa02023b8490ddf9b595e90a3a361399b5`, patch SHA-256 `8d08656ea4641ade5e70d07f5dbe79261acaee2427a2a96f15194b6a75da56eb`, 49 paths, status SHA-256 `fa2c645b5f3a7be6fd53c1a36566ebbec91f45e3d1b1092f37640502c5fe0c0a`, and zero overlap. Two OPEN findings invalidate the generation; no commit authorized. | `deleg_aba5217b` |
| 2026-08-06 12:40 KST | GREEN: canonical owner, coherent snapshot, and executable CLI authority blockers remediated | Publication and reconciliation now hold owned run-state locks and validate the immutable canonical revision owner; generic run-state ownership may advance before medical publication but becomes immutable afterward. Snapshots serialize on the owned run-state lock, use no-follow descriptor copies plus equal pre/copied/post inventories, retry boundedly, publish unique staged directories with final run state and a completion manifest, and clean failed staging. Deterministic regressions cover foreign owners, torn-copy retry, repeated/concurrent destinations, cleanup, and final-state equality. The authoritative nine-command lifecycle block now matches argparse and a fixture-isolated test dispatches every documented form. An initial full run exposed and preserved the legitimate pre-medical run handoff (`569 passed, 1 failed`); after refinement, the complete live matrix passed 570 Python and seven frontend tests, lint with zero errors/two unrelated warnings, production/static builds, synchronization, compilation, and diff checks. | Replacement exact-tree freeze/review still required before R20 |
| 2026-08-06 12:56 KST | OPEN: exact-tree review rejected `bd5f48ed` | Lifecycle/docs review returned CLOSED. Persistence review reproduced two pathname-ownership snapshot failures: promotion replaced a foreign destination that appeared after destination selection, and rollback deleted a foreign replacement after the owned snapshot had moved. Whole-change review reproduced clearance followed by an outcome crash when a resolved adjudication participant reopened/cancelled/closed with no current request, and proved post-commit wait reconciliation could add a third independent 900-second lock window beyond the 1,830-second API timeout. Frontend review found no semantic blocker but could not close after its cleanup command was denied. Opening/closing identities otherwise matched tree `bd5f48ed214366de8f672493ed810ebd9e6791b2`, patch SHA-256 `7a90de5ea2603ab5c1f007770f0e0dd9b6723059171062de34455c1967cf25b9`, 51 paths, status SHA-256 `4e81c9834ec892308a3c4990210ea8fa421c150d675519837fad64c8f3a88d7f`, and zero overlap. No commit authorized. | `deleg_bca0753e` |
| 2026-08-06 13:08 KST | GREEN: `bd5f48ed` review blockers remediated and reviewer residue removed | Snapshot promotion now uses parent-descriptor-anchored Linux `renameat2(RENAME_NOREPLACE)`, so a destination appearing at promotion is preserved. A complete promoted snapshot is never pathname-deleted after later durability/run-state failure; it remains a recoverable complete orphan rather than risking deletion of a foreign replacement. Outcome projection treats any adjudication participant without a current request as a stale cohort. Post-commit wait projection uses one nonblocking owned-lock attempt and warns for explicit reconciliation if busy, while the standalone reconcile command remains blocking. Added deterministic destination/replacement, reopen-cancel-close outcome, and nonblocking third-window regressions. Removed generation-owned review-1/review-2 siblings and exact temporary verifier artifacts. The affected matrix passed 135 tests; the full live matrix passed 573 Python and seven frontend tests, lint with zero errors/two unrelated warnings, production/static builds, synchronization, compilation, and diff checks. | Replacement exact-tree freeze/review still required before R20 |
| 2026-08-10 02:23 KST | GREEN: local Evaluation and P4 fail-closed drift remediated | Focused tests first reproduced ground-truth access after human-review completion plus stale orchestrator/agent/settings instructions. `read-ground-truth` now always denies locally; human-review completion records only a future Unit 11 handoff prerequisite; Evaluation is a non-runnable placeholder; P4 has no unvalidated propagation path; canonical definitions and generated mirrors agree. Full Python passed 574 tests; synchronization, staged diff check, and bounded secret scan passed. | Verified pre-log tree `50f5414a2a6ea81d50e839820ea3b200b39f3aec`, patch SHA-256 `b86157efca716ced4e68f9ebe1f7efc24c0701afa06bd4944407b6d28a730d1e`; freeze a new exact review identity after this evidence entry |
| 2026-08-10 02:26 KST | GREEN: fork utility ground-truth path removed | RED tests proved `next_free_case_id` scanned `data/ground_truth` and `--include-ground-truth` copied answer-key bytes. Ground truth is now excluded from numbering, rejected by the internal data-tree copier, and absent from the CLI. The fork record retains `included_ground_truth: false` for compatibility. | Fork suite passed 21 tests; full Python passed 576 tests; freeze a replacement exact review identity after this evidence entry |
| 2026-08-10 17:11 KST | GREEN: durable medical mutation operation IDs | Required caller-generated IDs at CLI/API boundaries; bound IDs to stable authenticated request fingerprints; exact retries return committed results; changed-request collisions and malformed/tampered receipts fail closed; wait projection records idempotent reconciliation receipts; browser IDs survive remount/lost acknowledgement and clear only after verifiable acknowledgement. Canonical P5 and generated mirrors state the retry contract. A five-axis self-review found and remediated silent browser cleanup failure plus projection-change reporting. | Focused Python passed 77 tests; full Python passed 582; all 10 frontend tests passed; lint had zero errors and two pre-existing warnings; production/static builds passed. Pre-log staged tree `76702aab24346761736b8a77b5931577ffd3e792`, patch SHA-256 `6b71d8ee3e3f034a3fa00dd21375930bd6480bc27516ea7e7bcfb5c0ea0594d8`; freeze replacement identity after this log entry, then obtain independent CLOSED reviews |
| 2026-08-10 | OPEN: four replacement reviews rejected `4eb1e033` | Frontend/API, lifecycle/docs/generated, backend/persistence/security, and whole-change reviews all closed on the unchanged tree. Deduplicated blockers: missing validated `read-run-state`; API preflight blocks lost-ack replay; automatic reconciliation falsely collides after intervening operations; lifecycle/reconciliation receipts permit downgrade or duplicates; three P5 generic mutators lack operation IDs across CLI/API/UI; claim-analysis pass/snapshot bypasses medical clearance; UI wait display bypasses run-state projection; stale local Evaluation/ground-truth and pathname-lock instructions remain. | Opening/closing tree `4eb1e0335d370f8868e67c914e0a22e177cc7c4f`, patch SHA-256 `5e27cb8ef15943dd96d3f431e8f9c028e59efd4ec21cec463061a94856472619`, 68 staged paths, no unstaged changes, no reviewer edits. All four reviews invalidated by remediation and must be repeated. |
| 2026-08-10 | GREEN: four-review blockers remediated | Added validated run-state reads; made lifecycle retries DAO-authoritative; derived automatic projection IDs from the committed ledger; closed and validated lifecycle plus generic mutation receipts; required operation IDs across CLI/API/UI; enforced medical clearance at claim-analysis pass and snapshot; projected UI waits from validated run state; integrated medical publication/clearance into canonical claim-analysis/orchestrator instructions; removed stale local-Evaluation and pathname-lock claims. Self-review additionally closed tampered/duplicate generic receipts and multi-item UI projection comparison. | Full Python passed 593 tests; all 10 frontend tests passed; lint had zero errors and two established warnings; production/static builds, compilation, sync, diff checks, and bounded stale-authority scans passed. Freeze a replacement exact identity and repeat all four independent reviews before R19/R20 can close. |
| 2026-08-10 | OPEN: four repeat reviews rejected the `4b9585d2` tree | All four bounded reviews opened and closed on the unchanged candidate. Deduplicated blockers: medical clearance was not rechecked at each downstream start/snapshot; the autonomous run prompt assigned D2 approvals to the agent; explicit reconciliation retries collided after later ledger generations; the generic API wrapper could time out before the authoritative lock window; generic ledgers lacked a schema-versioned migration and semantically bound receipts; consequential run-state writers trusted raw state; local run-state/UI/fixture surfaces still modeled Evaluation; browser operation signatures omitted the lifecycle head; and intake guidance omitted lock ownership arguments. | Opening/closing tree `4b9585d245bc3951789be2cdff127ec3bb84e85e`, patch SHA-256 `c3b3943b0f219e029f9b1e23e96bb4da97f0bd20ed62c5537d9a2a5275489f05`, 83 staged paths, no unstaged changes, no reviewer edits. All reviews invalidated by remediation. |
| 2026-08-10 18:45 KST | GREEN: repeat-review blockers remediated | Added transition- and snapshot-level downstream medical clearance; made D2 explicitly human-owned; separated stable explicit reconciliation IDs from ledger-derived automatic IDs; aligned the generic API timeout and 504 handling with P5; versioned, migrated, and semantically validated generic-ledger receipts; centralized validated run-state reads and duplicate/receipt checks; replaced local Evaluation stages with human-owned handoff stages; included the canonical lifecycle head in browser mutation signatures; and made intake guidance executable with ownership arguments. Publication now requires an existing validated conflict ledger. | Expected REDs were observed first. Focused remediation matrix passed 95 Python and 11 frontend tests; full Python passed 601; all 11 frontend tests passed; lint had zero errors and the two established warnings; production/static builds, synchronization, compilation, and diff checks passed. Refreeze and repeat all four reviews before R19/R20 can close. |
| 2026-08-10 | OPEN: second repeat round rejected tree `42b739c9` | Frontend/API returned CLOSED. Whole-change, lifecycle/docs, and backend/persistence returned OPEN. Deduplicated blockers: no persisted zero-conflict initializer; medical-clearance applicability depended on a removable canonical pointer and omitted dependency-triggered denial response; the automatic reconciliation namespace was not reserved and explicit receipt digests were unbound; legacy `evaluation` run states had no deterministic migration; and legacy generic ledgers lacked a replayable receipt-history boundary. | Opening/closing tree `42b739c93a2d6d2a5c1e7ab43581fcf9755769a9`, patch SHA-256 `c962513bfe6a138f5ab6e364ef7dfa08ec07c9d5f3e4af53d82815c65267f42c`, no unstaged changes, no reviewer edits. Three review axes invalidated by remediation; frontend/API remains CLOSED for this superseded tree only. |
| 2026-08-10 | GREEN: second-repeat blockers remediated | Added locked zero-conflict initialization; persisted a schema-required one-way medical-adoption marker before the publication commit point; gated denial response and every post-adoption downstream transition/snapshot from that marker; reserved `medical-projection:` for automatic reconciliation and added an independently recomputed receipt digest; deterministically migrated only version-identifiable legacy Evaluation waits/stages; and added schema-versioned replayable baselines for native and legacy source/conflict ledgers. Forking normalizes all three legacy state families before schema validation. | Six expected REDs observed, then GREEN. Affected suites passed 120 tests; full Python passed 605; all 11 frontend tests passed; lint had zero errors and two established warnings; production/static builds, compilation, synchronization, and diff checks passed. Replacement freeze and three bounded reviews required. |
| 2026-08-10 | OPEN: persistence/lifecycle reviews rejected tree `af01bcbe` | Whole-change and frontend/API returned CLOSED. Backend/persistence proved adoption could be downgraded by a missing/false run state and forks retained source-case receipt identities. Lifecycle/docs proved immediate-predecessor v0.3/v0.2 generic ledgers and pre-v0.3 reconciliation receipts lacked migrations. | Opening/closing tree `af01bcbe35fff157a01a2bafa3598db19a2692e5`, patch SHA-256 `4e94caf3c461b69c6552470b45a88bc25ecdbd2229dfab23a9ec2a9d856e2e64`, 92 staged paths, no unstaged changes, no reviewer edits. Persistence and lifecycle axes invalidated. |
| 2026-08-10 | GREEN: predecessor, fork-lineage, and adoption recovery remediated | Durable medical artifacts now reassert adoption when run state is missing or falsely downgraded. Immediate-predecessor generic ledgers are semantically validated, absorbed into an explicit hashed baseline, and upgraded; predecessor reconciliation receipts derive the v0.3 receipt hash. Forks establish their own source/conflict baselines, absorb old operation digests/counts, and reset case-bound reconciliation receipts. | Five expected REDs observed, then GREEN. Expanded neighborhood passed 126 tests; full Python passed 610; all 11 frontend tests passed; lint had zero errors and two established warnings; production/static builds, synchronization, compilation, and diff checks passed. Final freeze and bounded persistence/lifecycle/whole-change confirmation required. |
| 2026-08-10 | OPEN: persistence/whole-change rejected tree `e66ba7bf` | Lifecycle/docs returned CLOSED. Persistence and whole-change proved predecessor migration discarded searchable receipts, so exact retry could re-execute; whole-change also proved generic case forking corrupts content-addressed medical revision hashes and references. | Opening/closing tree `e66ba7bf8ea6dbe2efece32a3f87edd61d1329a6`, patch SHA-256 `3747b4fa352a22709d4a0e5b21de99fe1d1bbae0e1cdc13a4bb11a2b19bd982f`, 92 staged paths, no unstaged changes, no reviewer edits. |
| 2026-08-10 | GREEN: predecessor retry and medical fork boundary remediated | Predecessor receipts remain searchable as a validated absorbed prefix; replay begins after that prefix while exact retries still return their committed result. Migration rejects a current baseline contradicting its latest receipt. Pre-medical forks establish explicit forked-operation digests and reset case-bound receipts; adopted medical state now fails closed before generic copying because immutable revision lineage cannot be safely rebased by this utility. | Focused exact-retry/final-binding/fork-refusal checks passed 5 tests; expanded neighborhood passed 128 tests; full Python passed 612; all 11 frontend tests passed; lint had zero errors and two established warnings; production/static builds, synchronization, compilation, and diff checks passed. Persistence/whole-change confirmation remains required. |
| 2026-08-10 | OPEN: whole-change timestamp finding on tree `dd440565` | Persistence returned CLOSED. Whole-change confirmed predecessor retry and medical-fork blockers closed, then found retained predecessor receipts did not canonically bind `reviewed_at`/`resolved_at`; schema-valid missing or falsified timestamps could be frozen into the migration baseline. | Opening/closing tree `dd440565de22c8c87b9426b2199168c82dad9614`, patch SHA-256 `647cff3cbbbfbbde32c0ea9634325da437823deeb10a479134103c82a80887d2`, no unstaged changes, no reviewer edits. |
| 2026-08-10 | GREEN: predecessor audit timestamps normalized | Migration requires each receipt-authored state timestamp, checks it precedes the adjacent receipt completion within the known predecessor writer window, records a digest of the original predecessor state, and sets the canonical migrated timestamp to the retained receipt’s `completed_at`. Pending add-only conflicts require no resolution timestamp. | Focused timestamp tests passed 4; expanded persistence/lifecycle neighborhood passed 130; full Python passed 614; all 11 frontend tests passed; lint had zero errors and two established warnings; production/static builds, synchronization, compilation, and diff checks passed. Whole-change confirmation remains required. |
| 2026-08-10 | CLOSED: final persistence and whole-change confirmation | Both repeated review axes closed without findings or edits. They confirmed predecessor timestamp normalization and original-state digest binding, absorbed-prefix exact retry and subsequent replay, the medical fork boundary, and preservation of P11/P5/D1/API/UI controls. | Opening/closing tree `9a6262652626f123481af7c1c35a765089288197`, patch SHA-256 `6282382d78bf233f34b7cd0fc49b50909dd3585aa27f8f93c07010d2355c0150`, 92 staged paths, no unstaged changes. Earlier final lifecycle/docs and frontend/API reviews were also CLOSED; their surfaces were unaffected by the last persistence-only remediation. |
| 2026-08-10 | GREEN: final semantic implementation checkpoint | Committed the exact independently reviewed implementation tree. | Commit `633bd7b03878264abbbb03b15c8a60fb10a48c77`; tree `9a6262652626f123481af7c1c35a765089288197`; no push performed. |

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
