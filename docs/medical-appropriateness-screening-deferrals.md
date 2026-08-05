# Medical Appropriateness Screening Approval and Deferral Register

## Purpose

This document records the boundary between the technical implementation approved on 2026-07-23 and the clinical, operational, privacy, and evaluation decisions that remain explicitly unapproved.

It is the activation guard for the implementation described by:

- `.hermes/plans/2026-07-23_055253-medical-variable-extraction-hierarchy.md`
- `.hermes/plans/2026-07-23_055254-medical-review-request-guardrail-harness.md`

An item remains deferred until this document identifies an explicit approver, approval record, approved value or policy version, and effective scope. Silence, elapsed time, schema availability, passing tests, or working UI controls are not approval.

## Approved technical baseline

On 2026-07-23, the project user explicitly approved both implementation plans. That approval covers decisions V1-V7 and R1-R9 as technical architecture and authorizes implementation of:

- the canonical medical-variable, observation, issue, evidence, and immutable-revision contracts;
- the deterministic compatibility projection to `extracted_claim_fields.json`;
- the DAO-only read/write and semantic-validation boundaries;
- the canonical medical-review ledger, state machine, wait projection, recovery, and concurrency controls;
- schemas and disabled bootstrap configurations;
- synthetic fixtures and automated tests;
- localhost-only backend and UI scaffolding;
- agent, skill, pipeline, migration, and operating documentation updates;
- a supervised real-human dry-run capability only after every prerequisite below is separately approved.

This approval does not activate clinical extraction, automated referral, real-case human review, controlled-original source access, or production deployment.

## Fail-closed bootstrap required by the approved plans

Until the corresponding deferrals are resolved, implementation must preserve these values and effects:

| Configuration area | Required bootstrap state | Effect |
|---|---|---|
| Medical structuring | `behavior_enabled: false`; no enabled case/document roles | Real-case medical extraction is rejected; schema and synthetic tests remain possible. |
| Referral policy | `policy_enabled: false`; no executable severity thresholds or referral rules | `decision_origin: policy` is rejected. |
| Human role policy | `operations_enabled: false`; no named human actors; only placeholder specialty `unspecified` | Real-case human lifecycle actions are rejected. |
| Review-request vocabulary | `requests_enabled: false`; no approved interpretation entries | Real medical-review packages are rejected. |
| Source viewing | Redacted, revision-pinned quote/table path only | Controlled originals are unavailable. |
| Deployment | Localhost only; no identity claim beyond an audited actor assertion | Remote or multi-user deployment is non-conforming. |
| Retrieval/reuse | No cross-case index or retrieval API | Expert responses cannot become precedent or automatic input. |
| Evaluation | No ground-truth access before the existing formal human-review/evaluation gate | D1 remains unchanged. |

## Deferred decisions requiring explicit approval

| Deferral ID | Decision still required | Requirement links | Minimum explicit approver/evidence | Behavior while deferred |
|---|---|---|---|---|
| MED-DEF-001 | Name the medical lead/workflow owner responsible for clinical variable, issue, codebook, and referral-policy approval. | MED-OQ-014 | Named person or controlled decision record with scope and effective date | Clinical configurations remain disabled. |
| MED-DEF-002 | Select the first PoC case types, document roles, and medical specialties. | MED-OQ-015 | Medical lead plus product/workflow owner | Enabled case/document-role sets remain empty; unsupported inputs report `unsupported_needs_configuration`. |
| MED-DEF-003 | Approve variable kinds, issue categories, domain labels, coding systems, unit vocabulary, date/cardinality rules, provenance vocabulary, and correction/extension policy beyond the approved generic contract. | MED-OQ-017 | Medical lead and engineering schema owner | Only generic schema structure and disabled placeholder configuration exist. |
| MED-DEF-004 | Approve counting bases and equivalence rules for injections, manual therapy, rehabilitation, repeated tests, and other quantities. | MED-OQ-009 | Medical lead with documented counting examples | Quantity aggregation remains unavailable unless an approved rule applies; partial records cannot claim complete counts. |
| MED-DEF-005 | Approve medical anomaly kinds and definitions for minor, needs-confirmation, and significant severity. | MED-OQ-004 | Medical lead with versioned definitions and examples | Anomalies may be stored only as `unclassified_pending_policy`; they cannot authorize policy referral. |
| MED-DEF-006 | Approve convergence/issue-clustering rules for multiple signals. | MED-OQ-005 | Medical lead with versioned grouping rules | The system may preserve explicit issue references but cannot infer policy-significant convergence. |
| MED-DEF-007 | Approve referral thresholds and rules for `refer`, `do_not_refer`, and `insufficient_information`. | MED-OQ-006 | Medical lead and product/workflow owner | Referral policy remains disabled; no automated referral decision is available. |
| MED-DEF-008 | Approve specialty taxonomy, specialty routing, and requested-interpretation codes with allowed issue-category/specialty combinations. | MED-OQ-003 | Medical lead and workflow owner | Only `unspecified` placeholder specialty exists and request vocabulary remains empty/disabled. |
| MED-DEF-009 | Name authorized actors and approve who may confirm, override, assign, cancel, reassign, amend, withdraw, adjudicate, close, and reopen; also approve escalation and service-level rules. | MED-OQ-018 | Workflow owner with named actor IDs, roles, permissions, and policy version | Human role policy remains disabled and has no named actors. |
| MED-DEF-010 | Approve whether medical reviewers may see controlled original pages in addition to redacted revision-pinned text/table evidence. | MED-OQ-008 | Privacy/security owner and medical workflow owner | Controlled-original access is rejected. |
| MED-DEF-011 | Approve authentication, authorization, audit retention, de-identification, tenant boundaries, accessibility, localization, and production deployment requirements. | MED-OQ-019 | Privacy/security, product, accessibility/localization owners, and engineering | Backend/UI remain localhost-only and actor metadata is explicitly not authentication. |
| MED-DEF-012 | Approve remote/asynchronous expert delivery and coordinator import of another professional's response, including identity verification and reviewer attestation. | MED-OQ-018, MED-OQ-019 | Workflow owner and privacy/security owner | Only the currently assigned professional may submit their own response in the localhost workflow. |
| MED-DEF-013 | Approve any automated anomaly producer and its validated input/output boundary. | MED-OQ-004 through MED-OQ-006 | Medical lead and engineering owner | The approved implementation accepts guarded referral inputs but does not create a real-case anomaly model. |
| MED-DEF-014 | Define benchmark medical issues without leaking ground truth, review-time measurement, unnecessary-referral measurement, sample sizes, confidence reporting, and Go/No-Go thresholds. | MED-OQ-010, MED-OQ-011, MED-OQ-016 | Evaluation owner and medical adjudication lead | No medical-screening effectiveness or Go/No-Go claim may be made. |
| MED-DEF-015 | Approve cross-case expert-answer storage access, retrieval eligibility, applicability limits, precedent warnings, and reuse evaluation. | MED-OQ-012, MED-OQ-013, MED-OQ-016, MED-OQ-019 | Privacy/security, medical lead, product, and evaluation owner | Responses remain case-local; no retrieval or reuse is implemented. |
| MED-DEF-016 | Approve downstream adoption deadlines, backfill policy, invalidation rules, old-run migration, cost/latency targets, and reproducibility requirements. | MED-OQ-020 | Product and engineering owners | New runs may use the new contract when enabled; old runs remain explicit legacy mode and are not silently backfilled. |
| MED-DEF-017 | Approve source-location detail beyond document/page/quote/table plus the exact `page_region` representation. | MED-OQ-007 | Medical workflow and engineering owners | The v0.1 locator uses document/page/quote or table location; `page_region` remains optional and not relied upon. |
| MED-DEF-018 | Approve an objective clinical-policy activation artifact and change-control process. | MED-OQ-014, MED-OQ-017, MED-OQ-018 | Medical lead, workflow owner, and engineering owner | Configuration presence alone cannot activate behavior; approval metadata is mandatory and validated. |

## Explicitly out of scope without a new approved plan

The approved implementation must not produce automatic conclusions about medical appropriateness, medical causation, disability, prognosis, legal liability, coverage, denial, reduction, or final claim disposition. Changing that boundary requires a new reviewed plan and cannot be accomplished by resolving one configuration deferral above.

Production authentication, remote portals, outbound email/messaging, multi-tenancy, cross-case retrieval, retention automation, and automatic expert-answer reuse also require separate approved implementation plans after their governing deferrals are resolved.

## How to resolve a deferral

A resolution entry must contain all of the following:

1. Deferral ID.
2. Exact approved value, policy, codebook, or operating rule.
3. Named approver and authority role.
4. Approval artifact or controlled decision-record reference.
5. Effective date and case/document/specialty scope.
6. Configuration/schema version to activate.
7. Required tests and rollback condition.
8. Privacy/evaluation impact where applicable.

After recording a resolution, implementation must still pass schema, semantic, DAO-boundary, regression, and guardrail tests before the associated behavior is enabled.

## Resolution log

No clinical, operational, privacy, production, retrieval, or evaluation deferral is resolved as of 2026-07-23.