# Medical Review Request Guardrail and Process Harness Implementation Plan

> **For Hermes:** Use subagent-driven-development and strict test-driven development to implement this plan task-by-task only after the user approves the decisions below.

**Goal:** Add a hard guardrail and an auditable, DAO-mediated process for deciding, packaging, assigning, answering, amending, and closing focused medical expert-review requests without confusing them with draft critic review, OCR resolution, factual conflicts, or final medical judgments.

**Architecture:** Introduce `_medical_review_ledger.json` as the canonical per-case medical-review lifecycle record. Request packages and human responses are versioned records embedded in that ledger and validated through shared schemas; all transitions occur through purpose-built DAO commands. `_run_state.json.human_input_status` remains a projection for P7 visibility, while `check-medical-reviews-clear` reads the canonical ledger and provides the structural downstream gate. The existing `expert_review_v{n}.json` critic/evaluation handoff remains unchanged and separate.

**Tech Stack:** Python 3, JSON Schema Draft 2020-12, `jsonschema`, existing file-lock/atomic-write patterns in `tools/dao.py`, FastAPI/Pydantic, React/Vite, pytest, existing npm workflow.

---

## 1. Scope and requirement map

This plan covers:

- `MED-REF-001` through `MED-REF-009`
- `MED-PKG-001` through `MED-PKG-009`
- `MED-EXP-001` through `MED-EXP-009`
- `MED-UI-001` through `MED-UI-007`
- `MED-LIF-001` through `MED-LIF-009`
- `MED-RTE-001` through `MED-RTE-007`
- `MED-SRC-001` through `MED-SRC-007` as applied to requests and responses
- `MED-KB-001`, `MED-KB-004`, and `MED-KB-006` for future-compatible storage only
- M4's auditable decision boundary and M5's process/package/response acceptance criteria
- Harness guardrails P1, P3, P5, P6, P7, P8, P10, D1, and D2

This plan does not implement:

- numeric referral thresholds or medically approved anomaly-severity definitions;
- an automated anomaly-detection model; the plan does define the source-grounded anomaly/medical-issue input contract needed to guard referral decisions;
- prior-answer search/retrieval;
- production authentication, authorization, multi-tenancy, retention, or service-level escalation;
- outbound email/messaging or a remotely reachable expert portal;
- ground-truth-backed metrics;
- automatic medical, legal, coverage, denial, reduction, or treatment-appropriateness decisions.

## 2. Existing behavior that must remain separate

The repository already has `request-expert-review` and `expert_review_v{version}.json`, but those names refer to the critic → human draft review → evaluation gate:

- `tools/dao.py:791-845`
- `schemas/expert_review.schema.json`
- `tests/test_dao_human_review.py`
- `frontend/web/src/components/HumanReviewPanel.jsx`

That workflow opens D1's answer-key evaluation gate. It is not medical consultation and must not be renamed, overloaded, or used as the medical-review lifecycle. This plan should document the distinction and may later rename the existing CLI to `request-draft-review` only through a separate backward-compatible migration; such renaming is outside this implementation.

## 3. Proposed decisions requiring user review

Implementation must not begin until these decisions are approved or changed.

### Decision R1: canonical lifecycle record

**Proposal:** Use one canonical shared state file per case:

`outputs/CASE_XXX/_medical_review_ledger.json`

It contains referral decisions, request package versions, assignments, human response versions, adjudications, and an append-only transition history. Package/response schemas are separate reusable definitions, but their records are embedded in the ledger so one locked atomic ledger write cannot leave a package file and lifecycle pointer out of sync.

**Reasoning:** The current file-backed architecture cannot atomically commit two independent files. A single ledger is the smallest reliable PoC ownership boundary and gives one place to recover, audit, and mint request IDs.

Resolves the PoC storage choice in `MED-OQ-002` and `MED-OQ-012` for same-case workflow state. Cross-case search remains deferred.

### Decision R2: exact lifecycle states

**Proposal:** Adopt these canonical states:

1. `decision_pending`
2. `do_not_refer`
3. `needs_information`
4. `package_ready`
5. `awaiting_expert`
6. `expert_needs_information`
7. `answered`
8. `adjudication_required`
9. `cancelled`
10. `closed`

Reassignment, reopening, amendment, withdrawal, and adjudication are transition/event types, not additional current-state labels. Every event records prior state, next state, actor, actor role, timestamp, reason, and relevant contract/config versions.

`open-medical-review-item` first creates a ledger-local review-item ID (`MRI_0001`) in `decision_pending`, with the issue ID and declared decision owner (`policy` or `human`). Only a later `refer` decision mints a request ID (`MRR_0001`), and it must include a conforming package in the same atomic ledger mutation, moving directly to `package_ready`. `do_not_refer` and `needs_information` remain auditable review items but are not mislabeled as expert requests. Reopening an item starts a new decision cycle; a later referral mints a new request ID rather than mutating a closed request.

Resolves the state-name portion of `MED-OQ-018`; actor authority and service levels remain open.

### Decision R3: request eligibility guardrail

**Proposal:** A medical expert request may enter `package_ready` only when all of the following validate:

1. A referral decision is explicitly `refer`.
2. The decision references a valid `medical_issues[]` entry and contributing observations from `medical_variables.json`.
3. A/B/C importance is not the sole reason.
4. The linked source was reinspected and the unresolved issue remains.
5. The package has exactly one `issue_id` and one structured question with `scope_kind: issue_only`, configured `requested_interpretation`, explicit subject variable/observation IDs, and an optional time window. A whole-case scope is not representable.
6. The package contains all required summary, timeline, prior-condition/prognostic, uncertainty, and materially conflicting evidence fields.
7. Every cited observation/evidence locator resolves through the DAO-managed contracts.
8. Every issue-evidence link marked `core` is included, including `weakens` and `conflicts` links. Every omitted `supporting` link has an explicit omission reason reviewed during assignment; `context` links may be omitted.
9. OCR disagreement and schema failure have already been handled by P8/P4; factual source contradiction remains under P6.
10. The package contains no ground-truth/answer-key reference and does not state an adverse medical or insurance verdict.

The guardrail determines whether a request package is conforming. It does not invent the clinical referral decision or threshold.

The v0.1 harness has two explicitly separated decision origins:

- `policy`: permitted only when a validated referral-input snapshot supplies anomaly severity and convergence around the issue and an approved, versioned referral policy is enabled;
- `authorized_human_override`: permitted before automated policy approval, but requires actor identity, role, rationale, and evidence and is never represented as an automated result.

If no medical lead has approved the severity definitions and referral policy, the DAO must reject `decision_origin: policy`. This lets the request/response harness ship without smuggling an unapproved clinical threshold into code.

Because automated anomaly production is outside this plan, the only possible real-case route in the initial disabled-referral-policy slice, after the human role policy and request vocabulary are separately approved/enabled, is a coordinator-opened, human-owned review item and an `authorized_human_override` submission. An agent may prepare nonbinding draft inputs/package content for that coordinator to inspect, but cannot publish or attest the human decision. Synthetic tests may exercise both origins; production policy origin remains unreachable until a separately approved producer and policy are connected.

The DAO can deterministically enforce issue scope, references, core-evidence completeness, allowed fields, and policy/actor ownership. It cannot prove that free text is clinically well phrased. The human assignment action therefore includes `package_review_attestation` confirming that the question is understandable, focused, and balanced; assignment fails without that attestation. Do not substitute a broad keyword blacklist for human semantic review.

### Decision R4: process placement

**Proposal:** Medical review is an internal human gate inside the `claim_analysis` stage for the first PoC slice. The single authoritative blocking-state set is `decision_pending`, `needs_information`, `package_ready`, `awaiting_expert`, `expert_needs_information`, `answered`, and `adjudication_required`. Only `do_not_refer`, `cancelled`, and `closed` are clear. An expert response therefore cannot unlock downstream work by itself: `answered` requires a coordinator to close it or route it to adjudication. `check-medical-reviews-clear` enforces this set for dependent claim-analysis checkpoints and every downstream stage.

No new top-level stage or autonomous medical agent is added.

Resolves `MED-OQ-001` consistently with the variable-extraction plan.

### Decision R5: canonical versus projected waiting state

**Proposal:** `_medical_review_ledger.json` is authoritative for lifecycle and gating. `_run_state.json.human_input_status` is an observability projection with optional fields:

- `input_kind: medical_referral_decision | medical_review_assignment | medical_evidence | medical_expert_review | medical_review_disposition | medical_review_adjudication`
- `human_input_id`
- `review_item_id`
- `related_review_item_ids`
- `request_id`
- `adjudication_id`
- `wait_cycle`
- `status: waiting | received | no_longer_required`
- `resolved_at`
- `resolution`

Each human-owned step gets its own immutable wait episode: referral decision when human-owned, assignment, missing evidence, expert response, response disposition/closure, or adjudication. The DAO mints ledger-global `human_input_id` values (`MRH_000001` onward) and stores the canonical wait episode inside the review ledger before projecting it into run state. A shared adjudication therefore has one human-input ID plus all related review-item IDs rather than pretending to belong to one item. `wait_cycle` is the per-item/request ordinal used for display and duplicate detection. Completion updates that ledger episode to `received`; cancellation or supersession updates it to `no_longer_required`; reopening/withdrawal appends a new episode rather than changing an old received episode back to waiting. Episodes are never deleted.

For nonshared episodes, `review_item_id` is required and `related_review_item_ids` is empty. For a shared adjudication episode, `review_item_id` is absent, `adjudication_id` is required, and `related_review_item_ids` contains at least two item IDs. This same conditional shape is used in the ledger record and run-state projection.

`check-medical-reviews-clear` reads the ledger directly, so a crash between the ledger write and run-state projection cannot let the pipeline proceed. `reconcile-medical-review-waits` repairs missing/stale projections idempotently.

**Reasoning:** Multi-file atomicity is unavailable. State-first publication plus a canonical gate and recoverable projection is safer than pretending both files commit atomically.

### Decision R6: actor authority in the localhost PoC

**Proposal:** Record a closed actor assertion containing `actor_id`, display name, declared role, medical specialty code, attestation timestamp, and assertion source on every human-owned action. Agents may open a policy-owned review item, calculate a policy decision only when an approved policy is enabled, and prepare a validated package. They may not use `authorized_human_override`, assign a real expert, submit expert content, attest that review occurred, cancel on a human's behalf, or close/adjudicate the request. A human-owned decision item can be completed only through the human CLI/UI action.

The current unauthenticated localhost UI cannot prove identity or authorization. Therefore:

- actor metadata is an audit assertion, not production authentication;
- the backend remains bound to localhost;
- the UI displays this limitation;
- deployment outside localhost is non-conforming until `MED-OQ-019` is resolved.

The user must approve who may assign, cancel, close, reopen, and adjudicate under `MED-OQ-018` before those controls are enabled.

### Decision R7: source view

**Proposal:** The package UI opens evidence through a request-scoped backend endpoint that validates `case_id`, `request_id`, request version, pinned medical-variable revision, evidence locator, ledger membership, source classification, and D1 restrictions before serving anything.

Default PoC presentation:

- the exact redacted `quote` or `table_location` from the request's pinned medical-variable revision for ordinary textual evidence; v0.1 does not silently replace it with current page/chunk context;
- controlled original raw source only when the approved medical workflow requires it and the source ledger class is `raw`;
- never ground truth;
- non-text visual evidence remains human-only and uses its existing `expert_review_only` disposition.

The user/privacy owner must approve whether experts can see controlled originals (`MED-OQ-008`, `MED-OQ-019`). Until then, implement and test the redacted processed-text path first.

### Decision R8: response storage and reuse

**Proposal:** Store human response versions inside the same case ledger with medical `issue_id` and `issue_category`, evidence actually reviewed from the request's closed locator set and pinned medical-variable revision, interpretation, basis, uncertainty, alternatives, additional evidence needed, reviewer/specialty, timestamps, request/package version, request-config version, and nullable referral-policy version. Do not implement cross-case search or automatic reuse now.

This satisfies future-search compatibility without exposing responses across cases or turning them into precedent.

### Decision R9: expert handoff and operational acceptance

**Proposal:** The initial PoC supports the assigned medical professional submitting their own response through the localhost UI in the approved environment. `submit_response` requires that professional's actor assertion to match the current assignment; the DAO derives the persisted reviewer identity from the assignment and actor assertion. Coordinator import of another person's response is deferred until an identity-verification and reviewer-attestation policy is approved. The PoC does not email case material, expose a remote portal, or silently export a PII-bearing package.

Before M5 is called operationally complete, run one supervised, pseudonymized, ground-truth-blind dry run with a real medical professional and record only the normal ledger audit fields. Synthetic tests establish software correctness but do not prove that the request is understandable or that a real response can be captured. If remote/asynchronous review is required, authentication, authorization, privacy, secure delivery, and reviewer identity verification must be approved and planned first.

## 4. Canonical contracts

### 4.1 Shared definitions

Reuse `schemas/medical_common.schema.json` from the variable-extraction plan only for:

- medical issue IDs;
- issue-evidence-link IDs;
- variable/observation IDs;
- evidence locators;
- importance tiers;
- issue categories;
- contract/config versions;
- closed medical-variable revision references (`sha256`, `run_id`, `schema_version`, `config_version`).

Create `schemas/medical_review_common.schema.json` as the canonical owner of review-item/request/event/human-input/adjudication/source-reinspection IDs, the closed actor assertion (`actor_id`, `display_name`, `declared_role`, `specialty_code`, `attested_at`, `assertion_source`), reviewer classes, specialty-code format, lifecycle states/actions, response provenance, and package-review attestation. The variable plan does not own review actors or specialties.

The role vocabulary is fixed for v0.1 as `system_router`, `medical_coordinator`, `medical_reviewer`, and `medical_adjudicator`. A separate versioned role-policy configuration maps each lifecycle action to allowed roles and named actor records with the closed shape `{actor_id, display_name, declared_role, specialty_code}`. Its bootstrap file has `operations_enabled: false`, `specialty_codes: ["unspecified"]`, and no named actors; real-case human actions fail until the workflow owner approves a populated policy. Synthetic tests inject an enabled fixture rather than weakening the production bootstrap. Human CLI actions accept this assertion only through `--actor-file`; all required fields are validated against the enabled role policy. Policy/system actions forbid a caller-provided human actor and derive their technical actor provenance inside the DAO.

`schemas/medical_review_request_config.schema.json` and `config/medical/medical_review_request_v0.1.json` are the sole owner of the `requested_interpretation` vocabulary. Each entry is a closed `{code, label, allowed_issue_categories, allowed_specialty_codes}` record with approval metadata; requests store the code and `request_config_version`. The bootstrap has `requests_enabled: false` and no approved interpretation entries. Both policy-originated and human-override requests fail closed until this configuration is approved/enabled; synthetic tests inject an enabled fixture.

Upstream authority remains explicit: the variable plan's current `medical_variables.json` and DAO-owned immutable revisions own medical issue/evidence-link/observation facts; `document_manifest.json` and `page_chunks.json` own processed source identity/text; P8 must be resolved before automated text can support a request; P6 `_conflict_ledger.json` owns factual-conflict status/resolution; D1 keeps final-report/answer-key material out of all pre-evaluation inputs. Review validators receive the same-case P6 ledger explicitly and resolve medical-variable `conflict_id` references instead of copying P6 status into the review ledger. Every referral input, reinspection, and package pins the exact variable revision used. The review ledger references those authorities and stores immutable decision-time snapshots, but never replaces or repairs them. A `blocked_upstream` source may justify `insufficient_information`; it may not be disguised as a resolved medical anomaly.

### 4.2 Referral inputs

`schemas/medical_referral_inputs.schema.json` must represent the screening inputs needed by `MED-REF-001` without making the decision itself:

- source-grounded anomalies with stable IDs and configured severity labels;
- the `medical_issues[]` entry each anomaly concerns;
- contributing observation and importance-assignment IDs;
- convergence/grouping around the same issue;
- source-reinspection status;
- source-coverage limits;
- anomaly/config versions.

Until severity definitions are medically approved, this contract may be populated for audit but cannot authorize a policy-originated referral.

Exact v0.1 shape:

| Object | Required fields | Rules |
|---|---|---|
| Referral-input snapshot | `referral_input_id`, `issue_id`, `medical_variables_revision`, `anomalies`, `convergence`, `importance_assignment_ids`, `source_reinspection_id`, `source_coverage_statuses`, `schema_version`, `config_version` | Embedded immutably with the decision event; all medical IDs resolve against the pinned immutable revision. |
| Anomaly | `anomaly_id`, `anomaly_kind`, `severity_state`, `variable_ids`, `observation_ids`, `evidence_locator_ids`, `rationale` | `severity_state` is `classified` or `unclassified_pending_policy`; `severity_code` is required only when classified and must exist in the approved policy. |
| Convergence | `status`, `contributing_anomaly_ids`, `common_issue_id`, `rationale` | `status` is `none`, `single_signal`, or `multiple_signals`; it never implies referral by itself. |
| Source-reinspection record | `source_reinspection_id`, `medical_variables_revision`, `performed`, `actor_or_process`, `performed_at`, `observation_ids`, `evidence_locator_ids`, `result`, `unresolved_reason` | Stored once in the review item. `performed` must be true for `refer`; all references resolve against the pinned immutable revision. |

Policy-originated decisions require every relied-on anomaly to be classified under the approved policy. A human override may use `unclassified_pending_policy`, but the override must state why a focused expert question is still necessary and cannot be represented as policy output.

The validated referral-input object is embedded immutably in the review item when the decision is recorded; it is not maintained as a second mutable output file. The `--decision-file` submission contains referral inputs without `referral_input_id`/`source_reinspection_id`/`medical_variables_revision`, one source-reinspection object without its ID/revision, the decision, and, for `refer`, the first request package. Under the ledger lock, the DAO loads the current validated medical-variable revision, mints `MRI_NNNN-I01` and `MRI_NNNN-S01`, stores the reinspection record once, injects the same closed revision reference and reinspection ID into the persisted referral-input snapshot and initial request version, then validates/publishes the set in one mutation. Callers cannot choose or override the revision digest.

### 4.3 Referral decision

`schemas/medical_referral_decision.schema.json` must support:

- `decision_id`
- `issue_id`
- `decision: refer | do_not_refer | insufficient_information`
- `decision_origin: policy | authorized_human_override`
- referral-input contract/version reference
- contributing variable/observation IDs
- importance assignments and anomaly inputs as references, not copied facts
- rationale
- source-reinspection status, actor, timestamp, and notes through the embedded referral-input snapshot
- referral-logic/config version
- evidence locators
- `additional_information_required` when insufficient
- `decision_scope: screening_route_only`, a constant that makes the decision a routing record rather than a medical verdict

The persisted decision object is closed and exact: `decision_id`, `decision_version`, `decision`, `decision_origin`, `decision_scope`, `issue_id`, `referral_inputs`, `rationale`, `evidence_locator_ids`, `policy_version`, `actor`, `additional_information_required`, and `created_at`. `decision_scope` is the constant `screening_route_only`. `policy_version` is required for `policy` and must be null for human override. `actor` is null for policy origin; for human override, the DAO derives the complete shared actor assertion from `--actor-file`. The decision submission schema forbids `actor` and omits DAO-owned `decision_id`, `decision_version`, and `created_at`, so identity has one ingress path. `refer` additionally requires one request package in the same command payload; other outcomes forbid a request package. `additional_information_required` is required only for `insufficient_information`.

### 4.4 Request package

`schemas/medical_review_request.schema.json` must support:

- `request_version`, which is the package version; there is no separate `package_id`
- referral decision reference
- concise case and accident/onset summary
- diagnosis/test/treatment/clinical-course timeline by observation ID
- relevant prior conditions and prognostic factors
- exact uncertainty/conflict and why review is needed
- medical `issue_id`, `issue_category`, and suggested specialty
- focused question
- structured question scope (`issue_only`, one issue, configured interpretation type, subject IDs, optional time window)
- all core relevant/countervailing/conflicting evidence links
- omitted supporting-evidence links with reasons
- referral-input ID and source-reinspection-record IDs; the records themselves live once in the review item
- the immutable medical-variable revision used to construct the package and the exact evidence-locator IDs made available to the reviewer
- source-coverage limitations
- schema/request-config/referral-policy versions

The persisted request object is closed and exact: `request_id`, `request_version`, `issue_id`, `issue_category`, `decision_id`, `referral_input_id`, `medical_variables_revision`, `source_reinspection_ids`, `question`, `question_scope`, `suggested_specialty_code`, `case_summary`, `accident_summary`, `timeline_observation_ids`, `prior_condition_observation_ids`, `prognostic_observation_ids`, `included_issue_evidence_link_ids`, `included_evidence_locator_ids`, `omitted_supporting_links`, `uncertainties`, `source_coverage`, `schema_version`, `request_config_version`, `referral_policy_version`, and `created_at`. `referral_policy_version` equals the decision policy version for policy origin and is null for human override. `request_config_version` always identifies the approved/enabled request configuration that owns `requested_interpretation`; it is required even for a human override. `question_scope` is `{scope_kind: "issue_only", requested_interpretation: <configured code>, subject_variable_ids, subject_observation_ids, time_window}`. Each omitted-supporting entry is `{issue_evidence_link_id, reason}`. Every included locator must belong to an included observation/link in the pinned medical-variable revision, and each included core link must expose at least one locator. The initial submission omits DAO-owned `request_id`, `request_version`, `decision_id`, `referral_input_id`, `medical_variables_revision`, `source_reinspection_ids`, `request_config_version`, and `created_at`; the DAO injects the IDs/revision/config version selected in the same mutation.

`request_version` is the sole package-version identity. Initial referral creates version 1. A supplement appends the next request version under the same `request_id`; that version pins the then-current immutable medical-variable revision, carries forward compatible prior `source_reinspection_ids`, and appends a newly minted source-reinspection record under the new revision whenever new observations/evidence are added. A prior reinspection record may be carried forward only when every referenced observation and locator has byte-identical canonical JSON in both pinned revisions; otherwise the coordinator must reinspect and create a new record. The semantic validator rejects a new evidence reference that is not covered by either a compatible prior referenced reinspection or the supplement's new reinspection. No `package_id` exists.

### 4.5 Human response

`schemas/medical_review_response.schema.json` must support:

- request ID and `request_version` (the package version)
- medical `issue_id` and `issue_category`
- referral decision ID and nullable policy version reviewed
- real reviewer identifier, reviewer role, specialty, and reviewed timestamp
- evidence actually reviewed
- medical interpretation
- basis
- uncertainty and reasonable alternatives
- additional evidence needed
- optional downstream loss-adjustment advice, clearly separated
- amendment/withdrawal relationship to prior response version
- attestation that the content is the reviewer's own response

The persisted response object is closed and exact: `response_id`, `response_version`, `assignment_id`, `request_id`, `request_version_reviewed`, `issue_id`, `issue_category`, `decision_id`, `request_config_version_reviewed`, `referral_policy_version_reviewed`, `reviewer`, `reviewed_at`, `submitted_at`, `evidence_locator_ids_reviewed`, `interpretation`, `basis`, `uncertainty`, `alternative_interpretations`, `additional_evidence_needed`, `downstream_adjustment_advice`, `supersedes_response_id`, `response_status`, and `attestation`. `reviewed_at` is the reviewer's declared clinical completion time from the response payload; `submitted_at` is the DAO-owned server receipt time. `reviewer` and `assignment_id` are not accepted in the response payload: the DAO derives them from the active assignment for an initial response, or from the superseded/current response's assignment for amendment/withdrawal, and requires the `--actor-file` actor ID/role/specialty to match that assignment. `issue_id`, `issue_category`, `decision_id`, `request_config_version_reviewed`, and `referral_policy_version_reviewed` must exactly match the assigned request/decision snapshot; the referral-policy version is null for human override. `response_status` is `completed`, `amended`, or `withdrawn`; a needs-information action uses the action schema rather than fabricating a completed response. Every reviewed locator must be a member of `included_evidence_locator_ids` in the exact request version named by the assignment; because that closed set and its medical revision are stored on the request, response validation requires no mutable-current medical artifact. The action submission schema forbids `reviewer`/`assignment_id` and omits DAO-owned `response_id`, `response_version`, and `submitted_at`.

### 4.6 Ledger

`schemas/medical_review_ledger.schema.json` must support:

- `case_id`
- schema version
- monotonic `next_review_item_number`, `next_request_number`, `next_human_input_number`, `next_adjudication_number`, and `next_event_number`
- `updated_at`
- review-items array containing immutable decision cycles, zero or more version-preserving request records, and at most one `current_request_id`
- current state
- each review item's immutable `source_reinspection_records`, referenced by referral-input and request versions rather than copied
- each request record's immutable request versions (package snapshots pinned to medical-variable revision SHA and closed locator set), assignment history, and immutable response versions
- each request record's optional `current_assignment_id`, which resolves to exactly one immutable assignment record and is cleared when supplementation, cancellation, closure, or reopening makes the assignment inactive; response withdrawal reactivates the response's recorded assignment instead
- each request record's `current_response_id`, which is absent after withdrawal and present only for its active non-withdrawn response
- adjudication records
- optional `current_adjudication_id` on every item involved in an adjudication; expert responses remain immutable and are not rewritten as adjudicator-authored responses
- ledger-global immutable wait episodes keyed by `human_input_id`; run-state entries are projections of these records
- append-only events

Review-item IDs are minted under the ledger lock as `MRI_0001`, `MRI_0002`, and so on. A `refer` decision atomically embeds request version 1 and mints `MRR_0001`, `MRR_0002`, and so on; there is no stored “refer but request package missing” state and no separate package ID. Referral-input, decision, source-reinspection, and response IDs are item/request-local versioned IDs (`MRI_0001-I01`, `MRI_0001-D01`, `MRI_0001-S01`, `MRR_0001-R01`). Human-input, adjudication, and event IDs are ledger-global monotonic IDs minted from their explicit counters (`MRH_000001`, `MRA_000001`, `MRE_000001`). A shared adjudication and its shared wait therefore have unambiguous case-ledger ownership. Callers never choose these IDs, counters, or versions.

Each immutable assignment record is request-local and DAO-minted (`MRR_0001-A01`, `MRR_0001-A02`, ...). Its closed shape is `assignment_id`, `request_id`, `request_version`, `reviewer` (a named role-policy identity snapshot), `role_policy_version`, `assigned_by_actor_id`, `package_review_attestation`, `assignment_event_id`, and `assigned_at`. `current_assignment_id` is the sole active-assignment pointer. `assign`/`reassign` always bind one exact existing request version; supplementation clears the old pointer and requires assignment of the new package version before expert response. Initial `submit_response` and `request_information` require the acting `--actor-file` identity to match the current assignment. Amendment/withdrawal instead resolve the immutable `assignment_id` stored on the response being changed; withdrawal reactivates that assignment and creates a new expert wait. Every response's `request_version_reviewed` must equal its assignment's `request_version`.

## 5. State transition policy

| Action | Allowed source states | Destination | Actor authority | Run-state projection |
|---|---|---|---|---|
| Open review item | no item | decision_pending | Enabled system router for policy owner; authorized coordinator for human owner | Human-owner creates decision wait; policy-owner creates no human wait |
| Record do-not-refer decision | decision_pending | do_not_refer | Human only for override origin; otherwise approved policy | Resolve decision wait if present |
| Record insufficient-information decision | decision_pending | needs_information | Human only for override origin; otherwise approved policy | Resolve decision wait; create medical-evidence wait |
| Provide requested information | needs_information | decision_pending | Yes, coordinator | Resolve evidence wait; create decision wait when owner is human |
| Record refer decision + conforming package | decision_pending | package_ready | Human only for override origin; otherwise approved policy | Resolve decision wait; create assignment wait |
| Assign expert with package attestation | package_ready | awaiting_expert | Yes, coordinator | Resolve assignment wait; create expert-response wait |
| Expert requests information | awaiting_expert | expert_needs_information | Yes, assigned expert | Resolve expert wait as no longer required; create medical-evidence wait |
| Supplement package | expert_needs_information | package_ready | Yes, coordinator | Resolve evidence wait; append package version; create assignment wait |
| Reassign | awaiting_expert | awaiting_expert | Yes, coordinator | Resolve old expert wait as no longer required; create a new expert wait cycle |
| Submit response | awaiting_expert | answered | Yes, assigned expert | Resolve current expert wait as received; set current response; create disposition wait |
| Amend response | answered, closed | answered | Same assigned expert | Append version and replace current response; preserve or create disposition wait |
| Withdraw response without replacement | answered, closed | awaiting_expert | Same assigned expert | Resolve disposition wait as no longer required; clear current response; reactivate the assignment with a new expert wait cycle |
| Flag conflicting expert opinions | answered | adjudication_required | Yes, coordinator | Resolve disposition wait as no longer required; create adjudication wait |
| Record adjudication | adjudication_required | answered | Yes, adjudicator | Resolve adjudication wait; preserve expert responses; set shared current adjudication reference; create disposition waits |
| Cancel | decision_pending, needs_information, package_ready, awaiting_expert, expert_needs_information, adjudication_required | cancelled | Yes, coordinator | Resolve every active wait as no longer required |
| Close | answered, cancelled | closed | Yes, coordinator | Resolve disposition wait; answered requires an active non-withdrawn response and no pending adjudication |
| Reopen | do_not_refer, cancelled, closed | decision_pending | Yes, coordinator | Clear active request/adjudication pointers without deleting history; start new decision cycle and decision wait; a later referral mints a new request ID |

No transition is implied by file existence or UI state. Invalid transitions fail without modifying the ledger.

`flag_conflict` is the normal multi-item action: its payload names at least two `answered` review-item/response pairs for the same medical issue. Under one ledger lock it mints one ledger-global `MRA_NNNNNN` adjudication ID and one `MRH_NNNNNN` shared wait ID, moves every named item to `adjudication_required`, and stores the wait episode with all related item IDs. `adjudicate` references that adjudication ID, records the human adjudication without selecting a model answer, returns every involved item to `answered`, and creates their disposition waits. If `cancel` is invoked for an item in shared adjudication, it must name the adjudication ID and atomically cancel every involved item. Partial multi-item mutation is forbidden.

## 6. Implementation tasks

### Task 1: Record approved lifecycle and actor decisions

**Objective:** Resolve the choices the harness cannot safely guess.

**Files:**

- Modify: `docs/medical-appropriateness-screening-requirements.md` only for approved `MED-OQ-*` resolutions.
- Modify: `open-decisions.md` only if a project-level cross-reference is needed.

**Required before technical implementation:**

- R1-R9;

**May remain explicitly pending while schemas, disabled policy, synthetic tests, and the localhost harness are built, but block policy activation or a real-review dry run:**

- named medical workflow owner;
- who may assign/cancel/close/reopen/adjudicate;
- initial specialty codes or an explicit `unspecified` PoC value;
- initial requested-interpretation codes and their allowed issue-category/specialty combinations;
- redacted-only versus controlled-original source access;
- first PoC case types/specialties;
- whether the first reviewer will use the co-located localhost UI or whether a separate secure remote-delivery/authentication plan is required.

When any operational item remains pending, store `unassigned`/disabled configuration and reject the affected action; do not infer approval from silence.

Run `git diff --check` after documentation-only changes.

### Task 2: Add request/response/ledger schemas under TDD

**Objective:** Define the lifecycle contracts before implementing commands.

**Files:**

- Create: `schemas/medical_referral_decision.schema.json`
- Create: `schemas/medical_referral_inputs.schema.json`
- Create: `schemas/medical_referral_policy.schema.json`
- Create: `schemas/medical_review_common.schema.json`
- Create: `schemas/medical_source_reinspection.schema.json`
- Create: `schemas/medical_referral_submission.schema.json` for caller-provided decision/referral-input/package data without DAO-owned IDs, versions, or timestamps.
- Create: `schemas/medical_review_request.schema.json`
- Create: `schemas/medical_review_response.schema.json`
- Create: `schemas/medical_review_action.schema.json` as a discriminated union of the exact payload for each lifecycle action.
- Create: `schemas/medical_review_ledger.schema.json`
- Create: `schemas/medical_review_role_config.schema.json`
- Create: `schemas/medical_review_request_config.schema.json`
- Create: `config/medical/medical_referral_policy_v0.1.json` with `policy_enabled: false`; enabling it or adding executable thresholds requires the later medical-lead approval.
- Create: `config/medical/medical_review_roles_v0.1.json` with `operations_enabled: false`, `specialty_codes: ["unspecified"]`, and no named human actors until workflow-owner approval.
- Create: `config/medical/medical_review_request_v0.1.json` with `requests_enabled: false` and no approved interpretation entries until medical/workflow-owner approval.
- Modify: `tests/test_validation.py`

**RED-GREEN slices:**

1. `do_not_refer` decision with rationale and evidence.
2. `insufficient_information` decision with required information.
3. Source-grounded anomalies and convergence around one medical issue.
4. Policy-originated decision rejected while referral policy is unapproved/disabled.
5. Authorized-human override with actor, rationale, and evidence.
6. `refer` decision plus focused package.
7. Package rejected when A/B/C is its only justification.
8. Package rejected without source-reinspection record.
9. Package rejected unless its structured scope is one issue and configured interpretation type.
10. Package includes every core supporting/weakening/conflicting link and records reasons for omitted supporting links.
11. Human response with uncertainty and alternatives.
12. Expert-needs-information response/action.
13. Versioned amendment and adjudication.
14. Complete ledger event history.
15. Initial referral input and request version share one DAO-minted source-reinspection record; supplemented evidence appends one new record without copying prior truth.
16. Disabled/unapproved request configuration rejects all real packages; an enabled synthetic fixture accepts only configured interpretation/issue/specialty combinations.
17. Referral inputs, reinspections, and request versions pin one existing immutable medical-variable revision; historical request versions retain their original revision after current medical variables change.
18. Assignment records bind an exact request ID/version and role-policy identity; supplementation invalidates the active assignment, and response submission against a different version fails.
19. Decision/response payloads containing caller-supplied `actor`, `reviewer`, or `assignment_id` fail; the DAO injects those persisted fields from the sole actor/assignment path.
20. Reviewer-declared `reviewed_at` and DAO-owned `submitted_at` remain distinct and validate independently.

Use synthetic fixtures only.

### Task 3: Add semantic guardrail validation

**Objective:** Enforce cross-contract conditions that JSON Schema cannot express.

**Files:**

- Modify: `tools/medical_contracts.py` from the variable plan
- Create: `tests/test_medical_review_guardrail.py`

**Functions:**

```python
def validate_referral_inputs(inputs: dict, *, medical_variables: dict, medical_variables_revision: dict, source_reinspection_records: list[dict], policy: dict | None) -> list[str]: ...
def validate_referral_decision(decision: dict, *, medical_variables: dict, medical_variables_revision: dict, referral_inputs: dict, source_reinspection_records: list[dict], policy: dict | None) -> list[str]: ...
def validate_review_request(package: dict, *, decision: dict, source_reinspection_records: list[dict], medical_variables: dict, medical_variables_revision: dict, conflict_ledger: dict, manifest: dict, page_chunks: dict, request_config: dict) -> list[str]: ...
def validate_review_response(response: dict, *, request: dict, assignment: dict, action_actor: dict) -> list[str]: ...
def transition_allowed(current_state: str, action: str, *, actor: dict, role_policy: dict, has_current_response: bool, has_pending_adjudication: bool) -> bool: ...
```

**Required tests:**

- unknown issue/variable/observation IDs fail;
- policy-originated decision fails when severity definitions/referral policy are absent, unapproved, or disabled;
- policy-owned item creation fails under the disabled bootstrap referral policy; human-owned item creation is available only to an authorized coordinator under an enabled role policy;
- human override cannot masquerade as a policy decision and requires actor/rationale/evidence;
- disabled or unapproved role policy rejects every real-case human transition; enabled synthetic policy enforces action-to-role mappings;
- missing `actor_id`, display name, role, specialty, attestation time, or assertion source fails every human-owned CLI/API action; a caller-provided actor fails every policy/system action;
- invalid or non-resolvable evidence fails;
- ground-truth references fail;
- unresolved P8/schema failures cannot be relabeled as medical uncertainty;
- P6 factual contradictions remain linked and unresolved, not chosen by the package;
- every referenced medical-variable conflict ID resolves to a valid same-case P6 record; no request/package/response copies P6 status or resolution;
- a package cannot omit any core weakening/conflicting link and cannot omit a supporting link without recording a reason;
- request `issue_id`/`issue_category`/policy version match the medical issue and decision snapshot; response copies match its assigned request/decision exactly;
- request interpretation, issue category, and suggested specialty match one enabled entry in the versioned request configuration; ambiguous generic `config_version` is not accepted;
- revision digest/metadata must identify the exact immutable medical-variable bytes used for every referral input, source reinspection, and request version; current-file substitution or caller-supplied revision changes fail;
- every response evidence-locator ID is a subset of the assigned request version's stored `included_evidence_locator_ids`;
- assignment request ID/version, response request ID/version, and action actor must agree exactly; stale/supplemented assignment pointers and caller-supplied reviewer/assignment fields fail;
- referral inputs and every request version resolve source-reinspection IDs from the review item's single record collection; new package evidence without a covering reinspection fails;
- request/package schemas contain no final-medical-verdict or insurance-disposition field, and assignment requires the human package-review attestation;
- a model-produced object cannot satisfy human response fields without a human-owned submission action;
- request and response versions are monotonic.

Do not add a general free-text forbidden-language filter: it would reject legitimate source quotes and still miss paraphrases. Enforce forbidden outputs through closed schemas/structured scope, then require the human package-review attestation before assignment.

### Task 4: Add the canonical ledger and locked transition engine

**Objective:** Provide one safe read-modify-write owner for all medical-review state.

**Files:**

- Modify: `tools/dao.py`
- Modify: `tests/conftest.py`
- Create: `tests/test_dao_medical_review.py`

**Internal API:**

```python
def medical_review_ledger_path(case_id: str) -> Path: ...
def load_medical_review_ledger(case_id: str) -> dict: ...
def _mutate_medical_review_ledger(case_id, held_by, run_id, purpose, mutation): ...
def _reconcile_medical_review_waits(case_id, held_by, run_id): ...
```

**CLI surface:**

```text
read-medical-review-ledger CASE_ID
read-medical-review-evidence CASE_ID REVIEW_ITEM_ID REQUEST_ID REQUEST_VERSION LOCATOR_ID
open-medical-review-item CASE_ID --issue-id MCI_NNNN --decision-owner {policy|human} [--actor-file PATH] --held-by NAME --run-id RUN_ID
record-medical-referral-decision CASE_ID REVIEW_ITEM_ID --decision-file PATH [--actor-file PATH] --held-by NAME --run-id RUN_ID
transition-medical-review CASE_ID REVIEW_ITEM_ID ACTION --actor-file PATH [--data-file PATH] [--reason TEXT] --held-by NAME --run-id RUN_ID
check-medical-reviews-clear CASE_ID
reconcile-medical-review-waits CASE_ID --held-by NAME --run-id RUN_ID
```

`ACTION` is an argparse enum mapped to one canonical transition table; callers cannot supply arbitrary next states.

`--actor-file` validates the exact shared actor assertion. It is required when opening a human-owned item and recording a human override, forbidden for policy/system-owned operations, and required for every lifecycle `ACTION` above. The DAO checks `actor_id`, role, specialty, and action permission against the enabled role policy and stores the complete assertion on the event; `--held-by` remains lock/process attribution and is never substituted for the human actor.

The enum is exactly `provide_information`, `assign`, `request_information`, `supplement_package`, `reassign`, `submit_response`, `amend_response`, `withdraw_response`, `flag_conflict`, `adjudicate`, `cancel`, `close`, and `reopen`. Each action has one schema-selected payload; for example, `assign`/`reassign` require exact `request_id`, `request_version`, target `reviewer_actor_id`, and package-review attestation; `submit_response` requires a response but forbids reviewer/assignment fields; `supplement_package` requires a new source-reinspection object whenever it adds evidence; and `reopen` requires a new decision owner and reason. For supplementation, the source-reinspection submission omits revision metadata; the DAO pins the then-current immutable revision, injects it into the new record/version, and clears `current_assignment_id`. `amend_response`/`withdraw_response` from `closed` are valid only when that item was closed from `answered` and still references a current response.

**Behavior:**

1. Acquire `_medical_review_ledger.json.lock` across read, ID minting, validation, mutation, and atomic write.
2. Re-read all canonical state after lock acquisition.
3. Reject `decision_owner: policy` unless the referenced policy is approved and enabled; otherwise require a human-owned item and authorized coordinator action.
4. Load/revalidate the exact current or request-pinned immutable medical-variable revision, manifest, page chunks, same-case P6 conflict ledger, enabled referral/request/role policies, actor assertion, and exact assignment/request-version binding through DAO-owned paths. Mint prospective IDs under the lock, construct the prospective source-reinspection/referral-input/request/assignment/response records, derive persisted actor/reviewer/assignment fields inside the DAO, and validate ownership before publication.
5. Append event and update current state in the same ledger write.
6. Release ledger lock.
7. Publish/reconcile `_run_state.json` projection in a separate locked step.
8. If projection fails, report partial observability but keep the canonical ledger state; `check-medical-reviews-clear` still blocks correctly.
9. Reconciliation is idempotent and cannot fabricate `received` without a genuine stored response.
10. Both read commands use DAO-owned paths. The evidence read obtains the revision SHA from the immutable request version, reads that exact medical-variable revision, proves locator membership in both the request's `included_evidence_locator_ids` and the pinned revision, verifies current source classification/D1, and returns the exact locator `quote`/`table_location` from the immutable revision. It does not fall forward to current page/chunk text; callers cannot supply a revision or filesystem path.

### Task 5: Extend run-state human-input identity safely

**Objective:** Support multiple concurrent requests without resolving “the most recent wait for this stage.”

**Files:**

- Modify: `schemas/run_state.schema.json`
- Modify: `tools/dao.py`
- Modify: `tests/test_dao_human_review.py`
- Modify: `tests/test_dao_run_state.py`

**Changes:**

- Add optional `input_kind`, `human_input_id`, `review_item_id`, `related_review_item_ids`, `request_id`, `adjudication_id`, `wait_cycle`, `resolved_at`, and `resolution` fields.
- Add `no_longer_required` to status while preserving existing `waiting`/`received` entries.
- Add an identity-based internal update path for medical requests.
- Leave the existing draft-review wrapper behavior backward compatible.
- Require nonshared episodes to have one `review_item_id`; require shared adjudication episodes to omit it and have at least two `related_review_item_ids` plus one `adjudication_id`.
- Reject duplicate active nonshared waits for `(review_item_id, input_kind, wait_cycle)` and duplicate shared waits for `(adjudication_id, input_kind)`; require exactly one run-state projection per ledger-owned `human_input_id`.
- Never resolve one request because another request for the same stage received a response.

**Tests:** two simultaneous medical requests, reverse-order responses, cancellation of one while another remains waiting, needs-information → provide-information → decision retry, withdrawal creating a new wait cycle while preserving the prior received episode, one shared adjudication wait projecting under its ledger-global human-input ID, and old run-state documents validating unchanged.

### Task 6: Add the structural downstream gate and recovery tests

**Objective:** Prove a pending request cannot be bypassed by stale or missing run-state projection.

**Files:**

- Modify: `tools/dao.py`
- Create: `tests/test_dao_medical_review_recovery.py`

Task 8 wires the proven gate command into the exact pipeline/agent instruction files; this task keeps the structural gate and recovery behavior isolated in DAO tests first.

**Tests:**

- the authoritative blocking set is exactly `decision_pending`, `needs_information`, `package_ready`, `awaiting_expert`, `expert_needs_information`, `answered`, and `adjudication_required`;
- only `do_not_refer`, `cancelled`, and `closed` are clear; submitting a response creates a disposition wait and does not unlock downstream work until coordinator closure;
- crash after ledger write but before run-state projection remains blocked;
- reconciliation recreates a missing wait;
- reconciliation preserves the received expert episode, recreates a missing disposition wait for `answered`, and marks active waits no longer required for `cancelled`;
- repeated reconciliation is idempotent;
- malformed ledger fails closed;
- no answer-key access occurs.

### Task 7: Falsify concurrency and ownership assumptions

**Objective:** Demonstrate that ledger-local IDs and transitions survive independent writers.

**Files:**

- Create: `tests/test_dao_medical_review_concurrency.py`

**Independent-process probe:**

1. Create a temporary case/output root.
2. Launch two Python subprocesses that import `dao`, point `dao.OUTPUTS`/`dao.DATA` at that same temporary root, and concurrently open items then record valid refer decisions and packages.
3. Verify unique `MRI_0001`/`MRI_0002` review-item IDs, unique `MRR_0001`/`MRR_0002` request IDs, two intact request histories, and four distinct open/decision events.
4. Concurrently submit response/cancel or assign/reassign actions and verify one legal order wins while the illegal stale transition fails visibly.
5. Kill one actor between canonical ledger commit and projection and verify `check-medical-reviews-clear` plus reconciliation.

Do not use two objects in one process as the only concurrency evidence.

### Task 8: Add the hard guardrail and procedural skill

**Objective:** Make request safety universal while keeping detailed process instructions out of the universal guardrail.

**Files:**

- Modify: `.claude/skills/harness-guardrails/SKILL.md`
- Create: `.claude/skills/medical-review-process/SKILL.md`
- Modify: `.claude/skills/loss-adjustment-pipeline/SKILL.md`
- Modify: `.claude/agents/claim-analysis.md`
- Regenerate: `.agents/skills/harness-guardrails/SKILL.md`
- Regenerate: `.agents/skills/medical-review-process/SKILL.md`
- Regenerate: `.agents/skills/loss-adjustment-pipeline/SKILL.md`
- Regenerate: `.codex/agents/claim-analysis.toml`
- Run: `python tools/sync_agents.py`

**Universal guardrail content:**

- medical referral is a request for interpretation, not a verdict;
- A/B/C or anomaly count alone cannot request review;
- source reinspection and a valid focused package are mandatory;
- OCR/schema/factual-conflict routes remain distinct;
- only DAO commands may create/transition a request;
- agents never fabricate, submit, or attest human expert content;
- pending requests block dependent work;
- answer keys never enter request generation.

Update P5's enumerated DAO write paths and P7's run-state semantics in the same change so the canonical guardrail does not contradict the implementation.

**Procedural skill content:** exact CLI order, allowed actors, package checklist, source-view rules, response capture, amendment/adjudication, failure handling, resume/reconciliation, and verification commands.

### Task 9: Add backend request/read/transition endpoints

**Objective:** Expose the DAO lifecycle to the localhost human UI without adding a privileged direct-write path.

**Files:**

- Modify: `frontend/backend/main.py`
- Modify: `tests/test_frontend_review_endpoints.py`

**Endpoints:**

```text
GET  /api/cases/{case_id}/medical-reviews
POST /api/cases/{case_id}/medical-reviews/{review_item_id}/actions/{action}
GET  /api/cases/{case_id}/medical-reviews/{review_item_id}/requests/{request_id}/versions/{request_version}/evidence/{locator_id}
```

The backend must:

- validate case/review-item/current-request/action and the complete actor assertion before mutation subprocess execution, plus explicit request/version/revision membership for evidence reads;
- call the DAO CLI for every ledger, contract, and evidence read and every mutation;
- never write the ledger or run state directly;
- validate evidence membership in the specified immutable request version and its pinned medical-variable revision, including historical package versions rather than silently substituting the current request or current medical variables;
- deny ground-truth sources;
- serve redacted processed evidence by default;
- allow controlled raw source only after the approved source-view decision;
- keep localhost-only deployment warnings.

**Tests:** invalid action, incomplete/unknown actor assertion, actor-file forbidden on a policy action, unknown review item, missing current request where one is required, unknown request/version/revision, path traversal, cross-case locator, unlisted locator, ground-truth source, historical package-version evidence resolution after current medical variables change, missing human attestation, DAO rejection propagation, successful response, disposition/closure, and cancellation.

### Task 10: Add the medical-review workspace UI

**Objective:** Let a coordinator and expert understand and complete a focused request from one auditable screen.

**Files:**

- Modify: `frontend/web/src/api.js`
- Create: `frontend/web/src/components/MedicalReviewPanel.jsx`
- Create: `frontend/web/src/components/MedicalReviewPackage.jsx`
- Create: `frontend/web/src/components/MedicalReviewResponseForm.jsx`
- Modify: `frontend/web/src/components/StageDetail.jsx`
- Modify: `frontend/web/src/pipelineDefinition.js`
- Modify: `frontend/web/src/statusLogic.js`
- Modify: `frontend/web/src/staticCaseData.js` with synthetic data only.

**UI sections:**

1. Current state and complete event history.
2. Human-owned referral-input, source-reinspection, and decision form when the policy route is disabled.
3. Case/accident summary.
4. Medical timeline.
5. Exact issue and referral rationale.
6. Focused question and suggested specialty.
7. Relevant and countervailing evidence, side by side where applicable.
8. Source-coverage limitations.
9. Assignment/reassignment/cancellation controls for authorized PoC actors.
10. Structured response fields, including uncertainty and alternatives.
11. Coordinator disposition, conflict/adjudication, withdrawal, and closure controls.
12. Explicit banner: “This is a request for human medical interpretation, not an adverse finding.”
13. Explicit localhost/no-auth limitation.

The generic `HumanReviewPanel.jsx` remains for critic/evaluation review and is not reused for medical consultation.

### Task 11: Add end-to-end synthetic lifecycle scenarios

**Objective:** Exercise the actual request process without case data or answer keys.

**Files:**

- Create: `tests/fixtures/medical/review_request_tier_a_conflict.json`
- Create: `tests/fixtures/medical/review_request_converging_tier_b.json`
- Create: `tests/fixtures/medical/review_request_needs_information.json`
- Create: `tests/test_medical_review_lifecycle_integration.py`

**Scenarios:**

1. Valid significant Tier-A issue after source reinspection → package → assignment → answer → close.
2. Multiple Tier-B observations converging on one issue → focused request.
3. Tier-C ambiguity only → do-not-refer; no waiting state.
4. Missing critical evidence → needs-information; no fabricated medical answer.
5. Expert asks for information → supplemented package → reassignment → response.
6. Cancellation preserves history and marks wait no longer required.
7. Expert amendment preserves prior response.
8. Two answered items for the same issue with conflicting expert responses move atomically to one shared human adjudication and back to disposition; no model selection.
9. Invalid whole-case structured scope is rejected before ledger publication.
10. Draft critic review and medical review coexist without unlocking each other's gates.

### Task 12: Documentation and full verification

**Objective:** Make operation, recovery, and limitations explicit.

**Files:**

- Modify: `README.md`
- Modify: `pipeline.md`
- Modify: `known-gaps.md`
- Modify: `frontend/README.md`
- Modify: `docs/medical-appropriateness-screening-requirements.md` only for approved decisions and implementation links.

**Document:**

- medical review versus critic review, OCR review, and P6 conflicts;
- command examples for each lifecycle action;
- actor responsibilities;
- wait/gate/reconciliation behavior;
- crash recovery and lock handling;
- response amendment/adjudication;
- source access/PII restrictions;
- localhost/no-auth limitation;
- deferred specialty taxonomy, coordinator response import, SLAs, retention, cross-case retrieval, and production auth;
- migration for old runs with no medical-review ledger.
- immutable medical-variable revision retention and the rule that historical review evidence never falls forward to the current revision.

**Final commands:**

```bash
pytest tests/test_validation.py tests/test_medical_review_guardrail.py tests/test_dao_medical_review.py tests/test_dao_medical_review_recovery.py tests/test_dao_medical_review_concurrency.py tests/test_frontend_review_endpoints.py tests/test_medical_review_lifecycle_integration.py -v
pytest -q
python tools/sync_agents.py
npm run lint --prefix frontend/web
npm run build --prefix frontend/web
git diff --check
```

Inspect the final diff to prove no raw sources, protected case data, outputs, ledgers, or answer keys were modified during development tests.

After software verification and only with explicit user/privacy approval, complete the R9 supervised human dry run. Do not use the final report/answer key, do not tune policy from that run, and do not claim M5 operational readiness from synthetic fixtures alone.

## 7. Verification matrix

| Requirement | Proof |
|---|---|
| Request is not a medical verdict | Schema/guardrail text and forbidden-boundary tests |
| Importance alone cannot refer | Guardrail semantic test |
| Source reinspection required | Invalid-package test |
| Narrow question and balanced evidence | Package schema/semantic tests and UI |
| Separate from OCR/P6/critic review | Route tests and coexistence integration test |
| Real human response only | Human-owned DAO/UI action and no-agent process rule |
| Full lifecycle history | Append-only event/version tests |
| Multiple requests do not collide | Independent-process concurrency test |
| Pending request blocks progression | Canonical ledger gate tests |
| Crash cannot create false progress | ledger-first publication and reconciliation tests |
| Source navigation is contained | backend D1/path/case/request membership tests |
| Historical package evidence is stable | immutable medical-variable revision and closed locator-set tests |
| Existing D1 evaluation gate remains intact | full existing `test_dao_human_review.py` suite |

## 8. Shared-resource ownership analysis

| Field | Decision |
|---|---|
| Resource identity | One `_medical_review_ledger.json` per case; one `_run_state.json` projection per case |
| Scope | Case-local, persistent across process exit and resume |
| Coordinator | DAO lock per canonical file; ledger lock owns every review/request/referral-input/reinspection/response/wait/adjudication/event ID and lifecycle mutation |
| Writers | Referral router through DAO; human coordinator/expert through DAO-backed CLI/UI |
| Ownership | Actor/event records plus process lock; true identity remains unverified in localhost PoC |
| Overlap | Different requests may progress concurrently; one request's transitions serialize under ledger lock |
| Recovery | Ledger is authoritative; run-state projection reconciles idempotently |
| Conflict rule | Invalid stale transition fails; no last-writer-wins state overwrite |
| Observability | Ledger state + event history + run-state wait projection + clear/block command |

The ledger and run-state files are not claimed to update atomically. Safety comes from making the ledger canonical, publishing the gate-relevant state first, checking the ledger directly before downstream work, and treating run state as a repairable projection.

## 9. Risks and mitigations

- **The localhost UI cannot prove a human identity:** preserve explicit limitation and forbid non-local deployment; production auth remains a blocker.
- **A model attempts to submit its own answer:** no agent instruction permits the human-owned action; structured response submission is a separate CLI/UI action with actor attestation and audit. This is not cryptographic proof and must not be described as such.
- **Generic `request-expert-review` naming causes confusion:** documentation and tests distinguish draft review from medical review; no contract is overloaded.
- **Two-file lifecycle/run-state drift:** ledger is authoritative, downstream gate reads it directly, and reconciliation repairs the projection.
- **Concurrent request ID collisions:** IDs are minted while holding the ledger lock and falsified with independent subprocess tests.
- **Source package hides counterevidence:** package validation compares the canonical medical issue's evidence links, requires every core weakening/conflicting link, and requires reasons for omitted supporting links.
- **Re-extraction changes historical package meaning:** each request version pins a content-addressed immutable medical-variable revision and a closed locator set; historical reads never substitute current data.
- **Medical thresholds are invented during implementation:** the guardrail validates conforming requests but does not decide clinical threshold; behavior remains blocked on medical approval.
- **Request volume becomes excessive:** metrics are deferred but every decision/request is stored with versions so referral rate and unnecessary-referral rate can later be computed.
- **Cross-case response leakage:** no retrieval API or cross-case index in this slice.

## 10. Dependency on the variable-extraction plan

Before this plan's Tasks 2-4 are finalized, the companion variable plan must provide:

- `medical_common.schema.json`;
- canonical variable/observation and evidence-locator identities;
- stable `medical_issues[]` entries that group observations without making referral decisions;
- validated `medical_variables.json`;
- DAO-owned content-addressed medical-variable revisions plus the closed revision-reference definition;
- approved domain/importance configuration for any real case (disabled bootstrap configuration is sufficient for synthetic harness work);
- source-coverage semantics.

This review plan's Task 2, not the variable plan, owns the referral-input, referral-policy, request-vocabulary configuration, review-actor, request, response, and lifecycle contracts. Policy-originated decisions additionally require a medically approved referral-policy version. The human-override path does not require that clinical policy, but it still requires approved/enabled human role and request-vocabulary configurations.

The DAO lifecycle and UI can then be implemented against stable IDs. Do not copy a temporary definition into the review schemas while waiting; use one canonical shared definition.

For shared files, follow the sequencing in the variable plan's Section 7: variable checkpoint/instruction integration precedes this plan's Task 8; the variable hierarchy UI/API precedes this plan's Tasks 9-10; documentation is applied sequentially. Every later task re-reads the shared file, preserves the earlier contract, and reruns both plans' affected tests. Do not run the two plans' shared-file edits concurrently.

## 11. Commit policy

Do not commit automatically. If the user authorizes commits during implementation, use explicit path lists only and split commits by semantic unit:

1. review schemas + guardrail validation;
2. canonical ledger + DAO transitions/recovery/concurrency tests;
3. guardrail/process skill + synchronized generated copies;
4. backend and frontend workflow;
5. documentation and migration.
