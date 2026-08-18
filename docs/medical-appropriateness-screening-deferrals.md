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
| Operator authentication | `operations_enabled: false`; no approved operator tokens or actors | The localhost API rejects medical reads and mutations until an approved local policy is explicitly installed. |
| Review-request vocabulary | `requests_enabled: false`; no approved interpretation entries | Real medical-review packages are rejected. |
| Source viewing | Redacted, revision-pinned quote/table path only | Controlled originals are unavailable. |
| Deployment | Localhost bearer-token authentication against the disabled-by-default operator policy; no production identity assurance | Remote or multi-user deployment is non-conforming. |
| Retrieval/reuse | No cross-case index or retrieval API | Expert responses cannot become precedent or automatic input. |
| Evaluation | No local ground-truth access or runnable Evaluation stage | Evaluation is deferred to an unavailable isolated Unit 11 service. |

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
| MED-DEF-011 | Approve production authentication, authorization, audit retention, de-identification, tenant boundaries, accessibility, localization, and deployment requirements. | MED-OQ-019 | Privacy/security, product, accessibility/localization owners, and engineering | Backend/UI use localhost-only bearer-token authentication when explicitly enabled; the shipped policy is disabled, has no approved operator tokens, and is not production identity assurance. |
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

## How the medical path is actually switched off, and how it would be switched on

Recorded 2026-08-18 after CASE_047 (97p, 배상책임) ran the whole Phase 1 chain
with medical publication refused. This section is the mechanical counterpart to
the deferral table above: the table says *what must be approved*, this says
*where the switches are and what each one does*. Approval is still the gate --
none of these edits is authorised by this document.

### The switch is one config file

`config/medical/medical_structuring_v0.1.json`, read by
`validate_medical_variables()` in `tools/medical_contracts.py`. Its shipped
state:

```json
{
  "config_version": "medical_structuring.v0.1",
  "behavior_enabled": false,
  "approval": null,
  "enabled_case_types": [],
  "variable_kinds": []
}
```

Three independent conditions reject a publication, so flipping any one of them
alone changes nothing:

| Check | Code | Refusal text |
|---|---|---|
| `behavior_enabled` false **or** `approval` null | `medical_contracts.py:66` | `medical structuring behavior is disabled or lacks approval metadata` |
| `case_type` not in `enabled_case_types` (empty list matches nothing) | `medical_contracts.py:74` | `case type '...' is not enabled by medical configuration` |
| `config_version` mismatch between contract and loaded config | `medical_contracts.py:68` | `config_version '...' does not match loaded '...'` |

Independence is the point: a stray `behavior_enabled: true` still fails on the
null `approval`, and both together still fail on the empty case-type list.

### A refusal here does not fail the stage

`run_claim_analysis.py` carries `_DEFERRED_REFUSALS` (line 1357), matching the
first two refusal texts above. When publication is refused for those reasons
the stage records the refusal and continues:

```json
"medical_publication": {"published": false, "deferred_config_refusal": true, "detail": "..."},
"status": "complete"
```

That is CASE_047's actual result. A configuration refusal and a genuine
extraction failure are deliberately not the same outcome.

Downstream stays consistent rather than blocked:

- `dao.py read-medical-variables CASE_047` → `FAIL: medical variable contract or revision not found`
- `dao.py check-medical-reviews-clear CASE_047` → `{"clear": true, "gate": "not_applicable"}`

`not_applicable` is what let `consistency_check` and `screening_report` proceed
on CASE_047. It also explains a question `screening-report` raised on that run:
`extracted_claim_fields.json` carried no `projection_mode`, which its spec calls
an error. With no medical revision ever published, that projection is
*pre-canonical* rather than a corrupted canonical one — the `not_applicable`
gate says the same thing — so proceeding with an explicit "no structured
medical screening was performed" statement in the report was correct.

### What enabling would require, in order

Every step below is blocked on the deferral table above; the order is what makes
each step verifiable rather than a bulk edit.

1. **Resolve the naming deferrals first.** MED-DEF-001 (medical lead) and the
   clinical-content deferrals it gates. Without a named approver, steps 2-4
   write approval metadata that has no one behind it, which is precisely what
   `approval: null` exists to prevent.
2. **Populate the vocabulary.** `domains` has 8 entries, but `variable_kinds`
   and `units` are both empty, so today the config could not describe a single
   variable even if enabled. `validate_medical_variables()` checks every
   observation's domain, kind and unit against these lists, so an
   enabled-but-empty config would reject each variable individually instead of
   refusing cleanly up front.
3. **Scope the case types.** Add to `enabled_case_types` only the types whose
   clinical content was actually approved. CASE_047 is 배상책임 — a liability
   case whose medical content is 후유장해 rating, which is a different
   approval question from 실손 treatment-cost screening.
4. **Set `approval` and `behavior_enabled` together**, and record the same
   approval in the Resolution log below. `config_version` must then match what
   publishing contracts declare, or step 5 fails on the third check.
5. **Re-run a case and read the receipt, not the log.** Publication success is
   `medical_publication.published: true` in the driver result, and
   `dao.py read-medical-variables CASE_ID` returning a contract. Note that
   `check-medical-reviews-clear` will stop returning `not_applicable` and start
   gating for real — a case that published medical variables can then block
   downstream stages on medical review, which is the behaviour the disabled
   path has been hiding.

### What is NOT switched by this file

`config/medical/` holds four other configs, each with its own flag and its own
deferral row: `medical_referral_policy_v0.1.json` (`policy_enabled`),
`medical_review_roles_v0.1.json` (`operations_enabled`),
`medical_review_request_v0.1.json` (`requests_enabled`), and
`medical_projection_v0.1.json`. Enabling structuring alone does not enable
referral, human review lifecycle, or review-request packaging.

## Resolution log

No clinical, operational, privacy, production, retrieval, or evaluation deferral is resolved as of 2026-07-23.
