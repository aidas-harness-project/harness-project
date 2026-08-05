# Medical Appropriateness Screening and Expert Intervention -- Requirements Baseline

## Document control

| Field | Value |
|---|---|
| Internal baseline | v0.1 |
| Status | Approved technical implementation baseline; non-UI harness implementation tracked below; clinical and operational activation deferred |
| Prepared | 2026-07-23 |
| External request source | [`의료적정성_스크리닝_설계안_v0.3.pdf`](../의료적정성_스크리닝_설계안_v0.3.pdf), 19 pages, v0.3 |
| Source audience | Medical professionals, project leadership, and the development team |
| Internal audience | Product, medical reviewers, loss adjusters, developers, evaluators, and future planning agents |

This document is the canonical internal synthesis of the medical professionals'
request. The PDF remains the authoritative source artifact. This baseline translates
that request into stable requirement IDs, boundaries, acceptance criteria, planning
workstreams, and open decisions without claiming that the requested behavior already
exists.

The project user explicitly approved the two linked technical implementation plans on
2026-07-23. That approval authorizes the fail-closed schemas, DAO boundaries, synthetic
tests, lifecycle harness, and localhost UI described by those plans. It does not approve
clinical codebooks, thresholds, real-case activation, reviewer authority, controlled
original source access, production deployment, retrieval/reuse, or evaluation policy.
The authoritative unresolved list and activation rule are recorded in
[`medical-appropriateness-screening-deferrals.md`](medical-appropriateness-screening-deferrals.md).

Documentation, requirement IDs, and implementation guidance are English. Korean
domain labels remain Korean where they are actual workflow values or source terms.

## 0. Authority, conformance, and plan readiness

### 0.1 Normative hierarchy

These artifacts have different authority and must not silently override one another:

| Authority | Governs | Conflict handling |
|---|---|---|
| [Canonical harness guardrails](../.claude/skills/harness-guardrails/SKILL.md) and [canonical PoC guardrails](../.claude/skills/harness-guardrails-dev/SKILL.md) | Safety, data access, evidence, human-input, and ground-truth boundaries. `.agents/` copies are generated mirrors. | They are non-negotiable. A requested feature must be redesigned if it would violate a guardrail. |
| [Medical-professional PDF](../의료적정성_스크리닝_설계안_v0.3.pdf) | External stakeholder intent and the meaning of the requested medical workflow | A disputed interpretation returns to the medical professionals; developers must not settle it by assumption. |
| This baseline | Internal requirement IDs, normalized scope, planning dependencies, and conformance claims | This baseline governs implementation traceability after stakeholder approval. If it appears to differ from the PDF, implementation pauses until the baseline is corrected or the interpretation is explicitly approved. |
| [Live pipeline specification](../pipeline.md) and versioned schemas/tools | Current implemented behavior | They describe what exists, not what the medical request already guarantees. A plan must identify every required migration. |

The labels in requirement tables are also normative:

- A numeric PDF page cites a source-derived request.
- **Project interpretation** translates the request into a repository-compatible
  behavior and requires product/engineering approval.
- **Safety interpretation**, **project guardrail**, or a named P/D rule is an added
  constraint and cannot be attributed to the medical professionals.
- **Derived planning metric** is an internal proposal rather than a metric explicitly
  requested in the PDF.

### 0.2 PoC conformance boundary

| Area | PoC conformance treatment |
|---|---|
| M1-M5 | All applicable **must** requirements in the associated MED-DAT, DOC, QTY, IMP, ANM, SIG, SRC, REF, PKG, EXP, UI, LIF, and RTE groups are required unless a named approver records a deferral. |
| M6 | MED-MET-001 through MED-MET-006, MED-MET-008, and the applicable MED-EVL controls are required. MED-MET-007 is a recommended diagnostic metric, not a source-request conformance condition. |
| Expert-answer storage | Structured storage, issue coding, provenance, uncertainty, and future-search compatibility are required through MED-EXP and MED-KB-001/004/006. |
| Automated prior-answer retrieval | MED-KB-002/003/005 apply only when retrieval is enabled. Retrieval may be deferred from the initial PoC, but the deferral must be explicit. |
| Should/may requirements | Every **should** must be implemented or dispositioned with a rationale. **May** requirements are optional unless a later approved plan promotes them. |
| Open decisions | A plan may be created to resolve an open decision. An implementation plan must not commit behavior controlled by an unresolved blocking decision. |

Conformance is recorded against requirement IDs, not inferred from mission names or a
passing test suite. Each implementation plan must include an applicability matrix with
`required`, `deferred`, `not_applicable`, or `implemented` for every requirement it
touches, plus the approver and rationale for any deferral.

### 0.3 What can be planned now

The baseline is sufficient for dependency-aware improvement planning. It is not a
substitute for the clinical codebooks, interface approvals, workflow ownership, and
evaluation protocol listed as deferred. Plans may therefore be:

1. **Decision plans**, which obtain and record missing medical, product, privacy, or
   evaluation decisions;
2. **Fail-closed technical harness plans**, which implement generic contracts, disabled
   configuration, structural gates, and synthetic tests without enabling real behavior;
   or
3. **Operational activation plans**, which may begin only after their mission's blocking
   deferrals are resolved and must cite the approved decisions/configuration versions
   they consume.

## 1. Executive requirement

Do not send OCR output directly into screening, policy matching, or loss-adjustment
report drafting. Introduce an evidence-linked **medical structuring layer** that:

1. converts validated document text into reusable medical variables;
2. screens for material anomalies, missing evidence, and questions that require
   medical interpretation; and
3. prepares a narrow, source-linked package for a medical expert when human review is
   warranted.

The target outcome is not automated medical adjudication. It is an efficient
collaboration model in which AI and humans work from the same source evidence, the AI
explains where and why attention is needed, and a human makes the medical judgment.

## 2. Source intent and governing principles

### 2.1 Governing principles

| ID | Principle |
|---|---|
| MED-PR-001 | The system screens and routes; it does not issue a final medical appropriateness, diagnosis, causation, disability-rate, pre-existing-condition contribution, or reduction-rate determination. |
| MED-PR-002 | Normal clinical discretion is respected. The system should surface only signals that could materially affect insurance or loss-adjustment analysis. |
| MED-PR-003 | Every core medical variable, anomaly, referral reason, and expert-facing question must be traceable to source evidence. |
| MED-PR-004 | The system should tell a reviewer what to inspect and why before attempting to provide an answer. |
| MED-PR-005 | Medical experts should be called selectively, with a focused package, rather than asked to reread the entire case. |
| MED-PR-006 | Expert answers should be stored in a structured, reusable form, while preserving uncertainty and the limits of each answer. |
| MED-PR-007 | Referral sensitivity must be measured and calibrated. A referral rate that is too low or too high is a failure mode. |

### 2.2 Interpretation of requirement language

- **Must** means required for a PoC implementation to claim conformance with this
  baseline.
- **Should** means expected unless a documented design decision explains why not.
- **May** means optional or deferred.
- **TBD** marks a decision the medical request does not resolve and that must not be
  guessed during implementation.

### 2.3 Working terminology

| Term | Meaning in this baseline |
|---|---|
| Medical structuring layer | The validated intermediate layer that converts processed medical text into observations, timelines, missing-evidence records, and review signals. It is a logical boundary, not yet a decided agent or tool. |
| Medical variable | A named clinical concept, such as diagnosis, test finding, treatment, symptom, prior condition, or functional finding. |
| Observation | One source-grounded occurrence or value of a medical variable at a point or period in time. A variable may have several observations. |
| Anomaly | A source-grounded conflict, gap, discontinuity, or interpretive question. It is not itself an adverse medical finding. |
| Issue cluster | One or more anomalies that converge on the same medical question. Referral logic operates on issue clusters rather than a raw anomaly count. |
| Referral | A versioned recommendation that a real human medical expert review a narrow issue. It is not a conclusion that care or diagnosis was wrong. |
| Referral package | The case summary, focused question, issue rationale, and linked evidence prepared for the expert. |
| Medical expert response | Genuine human input answering a referral question under the structured contract in this baseline. It is distinct from the existing human review of critic findings. |
| Benchmark issue | An independently established medical issue used only for evaluation after the applicable ground-truth gate. |
| Material | Capable of changing a medical-review question, referral decision, or downstream insurance/loss-adjustment analysis. The operational threshold is not defined by this word and requires an approved codebook. |
| Critical evidence | Evidence whose absence prevents a material medical question from being assessed. The evidence type and reason it is critical must be stated. |
| Objective evidence | A documented test, image/report, measured functional finding, pathology result, procedure record, or other observation not based only on an uncorroborated narrative statement. |
| Clinical course | The time-ordered relationship among symptoms, findings, treatment, response, recovery, deterioration, and unresolved gaps. |
| Source-grounded | Linked to a valid evidence reference or, for missing evidence, to the reviewed document coverage that supports the absence claim. |

## 3. Scope and boundaries

### 3.1 In scope

- Evidence-linked medical-variable extraction from validated, redacted, and chunked
  document text.
- Medical timelines spanning diagnosis, testing, treatment, and clinical course.
- Extraction of prior conditions, comorbidities, prognostic factors, and missing
  evidence.
- Treatment frequency and repetition patterns.
- Variable-importance stratification.
- Anomaly severity and clustering by medical issue.
- Medical-expert referral recommendations with explicit reasons.
- Focused expert-review packages and source navigation.
- Structured medical-expert responses.
- Search and reuse of prior expert responses as reference material.
- PoC metrics for referral quality, traceability, time savings, issue detection, and
  structured-data accuracy.

### 3.2 Explicit non-goals

- No automated final finding that treatment was appropriate or inappropriate.
- No automated coverage verdict, payout verdict, disability percentage,
  pre-existing-condition contribution percentage, or reduction percentage.
- No rule that repeated treatment automatically implies overtreatment or reduction.
- No replacement for the loss adjuster, medical expert, or legal reviewer.
- No automatic conversion of prior expert answers into binding rules, ground truth,
  or model-training data.
- No claim that A/B/C importance or anomaly counts alone can decide referral.
- No production liability allocation in this baseline. The source proposal assumes
  human final confirmation during the initial PoC.

### 3.3 Expected benefits and acknowledged risks

The source proposal gives these outcomes and risks as the reason to build and measure
the layer. They are planning context, not proof that the PoC will achieve them.

| Source expectation | Intended response in this baseline | Source page |
|---|---|---|
| Create a common language between OCR material and loss-adjustment work. | Use one validated, evidence-linked medical representation for downstream agents and humans. | 4, 17 |
| Reduce the cost of expert collaboration. | Route only focused issues and avoid making the expert reread the entire case. | 13, 17 |
| Control AI over-inference. | Preserve uncertainty, prohibit final medical verdicts, and require genuine human answers. | 2, 10, 17 |
| Improve policy and reduction mapping accuracy. | Give downstream work structured medical facts and expert interpretations without moving the final insurance decision into this layer. | 2, 4, 17 |
| Make case summaries reusable. | Store versioned variables, timelines, packages, and expert responses with provenance. | 14-17 |
| Intermediate structuring errors may propagate. | Validate contracts, retain source references, and stop invalid output before downstream use. | 17 |
| A summary may hide exceptions. | Keep conflicting evidence visible and let an authorized expert open the relevant source. | 13, 17 |
| Referral volume may become excessive. | Measure referrals, unnecessary referrals, and misses, then calibrate sensitivity. | 16-17 |
| Expert responses may become a bottleneck. | Store structured answers for permission-safe future reference while measuring the actual human workflow. | 14-17 |

## 4. Actors and responsibilities

| Actor | Responsibility |
|---|---|
| Medical structuring component | Extract source-grounded medical variables and timelines from processed documents. |
| Anomaly screening component | Identify and group medically relevant conflicts, gaps, and interpretive questions without resolving them. |
| Referral router (target role) | Under an approved versioned policy, decide whether a human review package should be prepared, explain why, and identify an appropriate specialty or reviewer class. The bootstrap policy is disabled and makes no real-case decision. |
| Medical expert | Review the focused issue and source evidence, then provide a structured human answer. |
| Loss adjuster | Use medical variables and expert answers in later coverage, reduction, rebuttal, and report work. |
| Evaluator | Measure the layer only after the applicable human-review and ground-truth gates are satisfied. |
| Product/development team | Define contracts, workflow, UI, calibration rules, privacy controls, and measurement protocol. |

The existing `reviewer_role: "의사"` value is sufficient only as a broad routing class.
The source request also asks which specialty should review an issue. A specialty
vocabulary and routing policy remain open decisions.

## 5. Target workflow and integration boundary

### 5.1 Source-request flow

```text
OCR material
    -> medical structuring layer
       -> medical variables
       -> anomaly signals
       -> expert referral decision/package
    -> screening and policy matching
    -> loss-adjustment report drafting
```

The source describes this as a transformation layer, not as a request to increase the
number of operational stages. Any later decision to expose it as a new top-level stage
is a repository integration choice and must preserve that intent.

### 5.2 Guardrail-compatible project interpretation

Agents in this repository must not read raw case files directly. Therefore the
medical layer should consume the validated processed layer after document processing,
not raw OCR output or raw images:

```text
D2-approved intake
    -> document processing
       -> P8 extraction/cross-validation
       -> classification
       -> redaction
       -> chunking
    -> medical structuring and anomaly screening
    -> claim analysis / consistency check / screening report
    -> draft report / critic / human review / evaluation
```

Policy-clause processing remains independently schedulable because policy text is not
itself a medical-structuring dependency. Claim analysis and screening must not consume
medical facts that bypass the canonical medical contract once the layer is adopted.

The approved repository architecture implements the layer as additional checkpoints
and a human gate inside the existing `claim_analysis` stage, not as a new top-level
stage or autonomous medical agent. `medical_variables.json` plus its immutable revision
is the validated contract boundary. After that revision is published,
`check-medical-reviews-clear` must succeed before `claim_analysis` completes and again
before every downstream stage starts. This architecture resolves MED-OQ-001 for the PoC;
it does not activate the disabled medical policies or decide future production stage
ownership.

### 5.3 Interface contract prerequisites

The approved technical plans now define versioned medical-variable, referral,
request/response, immutable-revision, and ledger envelopes and canonical filenames.
Those contracts are implementation surfaces, not clinical approval. The guarantees
below remain the minimum interface requirements, and disabled bootstrap configuration
must reject real-case operation until its owners approve activation.

**Required upstream guarantees:**

- stable case, document, page, chunk, and evidence identifiers plus source metadata,
  including document date when available;
- D2-approved document classification and access class;
- validated processed text with page ordering and source-page mapping;
- redaction status and sufficient location metadata to resolve an authorized human's
  source link without giving agents raw-source access;
- completed P8 extraction resolution for every consumed page;
- explicit document coverage, omissions, and processing failures; and
- contract and producer versions needed for reproducible reruns.

**Required medical-layer outputs:**

- medical variables and source-grounded observations;
- timelines and repeated-treatment quantities with counting scope;
- missing-evidence records;
- anomalies and issue-cluster membership;
- an auditable referral decision and, when referred, a package;
- a separately gated genuine-human expert response; and
- schema, codebook, logic, producer, and source versions.

**Failure semantics:**

- Schema-invalid or unvalidated upstream input is a stage failure and cannot be
  reclassified as a medical anomaly.
- A source contradiction follows P6 and cannot be resolved by the medical layer.
- A valid record stating that medically necessary evidence is absent is a
  missing-evidence signal, not a schema failure.
- A required output item with a missing or invalid evidence reference fails contract
  validation and moves the stage to explicit review; it cannot be promoted
  downstream.
- Partial document coverage must remain visible in quantity, timeline, and referral
  outputs so a partial record is never presented as complete.
- Once a canonical medical-variable revision has been published for the run, a missing,
  unreadable, schema-invalid, or semantically invalid medical-review ledger fails closed.
  `check-medical-reviews-clear` must pass before claim analysis completes and before each
  downstream stage starts; run-state/UI status cannot substitute for the ledger check.

## 6. Functional requirements

### 6.1 Medical-variable model

| ID | Requirement | Source pages |
|---|---|---|
| MED-DAT-001 | The system must represent diagnoses and diagnosis codes, preserving multiple values when sources disagree. | 5-7 |
| MED-DAT-002 | The system must distinguish accident/onset, first-recorded, test, and confirmed-diagnosis dates when the evidence supports those distinctions. | 7 |
| MED-DAT-003 | The system must represent tests, imaging findings, laboratory findings, functional tests, and pathology findings without collapsing them into one free-text field. | 6-8 |
| MED-DAT-004 | The system must represent operations, procedures, injections, manual therapy, rehabilitation, and other treatments with dates or periods where available. | 6, 11 |
| MED-DAT-005 | The system must represent clinical course, including symptoms, treatment response, recovery, deterioration, and temporal discontinuities. | 7 |
| MED-DAT-006 | The system must represent prior conditions, comorbidities, and prognostic factors separately from the current diagnosis. | 7, 10 |
| MED-DAT-007 | The system must represent missing critical evidence as structured data, including what is missing and which question cannot be assessed without it. | 7, 10 |
| MED-DAT-008 | The system must extract objective functional evidence that could affect a later disability assessment, while leaving the disability percentage to a downstream human-led module. | 7, 10 |
| MED-DAT-009 | Each variable must support one or more observations over time rather than assuming that one case has one permanent value. | 5-7 |
| MED-DAT-010 | Each variable or observation must carry evidence references, confidence, review status, and provenance about how it was produced. | 3, 5, 12 |
| MED-DAT-011 | Unknown, absent, contradictory, and not-applicable states must remain distinguishable; they must not be flattened to an empty string or a generic warning. | 5, 7, 10 |
| MED-DAT-012 | The model should remain specialty-neutral at the common layer and permit specialty-specific extensions later. | 7 |

### 6.2 Document focus and extraction depth

| ID | Requirement | Source pages |
|---|---|---|
| MED-DOC-001 | The system must prioritize diagnosis certificates, diagnosis names, and diagnosis codes when building the diagnosis view. | 6 |
| MED-DOC-002 | The system must prioritize imaging reports, laboratory tests, and functional-test results when building the objective-evidence view. | 6 |
| MED-DOC-003 | The system must prioritize operation names, operation records, procedures, injections, and manual-therapy records when building the treatment view. | 6 |
| MED-DOC-004 | The system must prioritize outpatient charts, admission/discharge records, and emergency-room records when building the clinical-course view. | 6 |
| MED-DOC-005 | The system must use itemized billing details as supporting evidence for treatment quantities and patterns, not as a substitute for the clinical record. | 6, 11 |
| MED-DOC-006 | Extraction depth must be configurable by document type or medical evidence role; the system should not process every document at identical depth. | 6 |

### 6.3 Repeated-treatment and quantity structure

| ID | Requirement | Source pages |
|---|---|---|
| MED-QTY-001 | The system must represent injection count, covered period, and spacing when supported by records. | 11 |
| MED-QTY-002 | The system must represent manual-therapy count and spacing when supported by records. | 11 |
| MED-QTY-003 | The system must represent physical-therapy and rehabilitation duration. | 11 |
| MED-QTY-004 | The system must represent repeated imaging and test frequency. | 11 |
| MED-QTY-005 | The system must represent treatment changes before and after a procedure or operation. | 11 |
| MED-QTY-006 | Quantity and repetition data must be treated as context for review, never as an automatic inappropriateness or reduction rule. | 11 |
| MED-QTY-007 | A count must state its counting basis, source coverage, and date range so that a partial record is not presented as a complete lifetime count. | 11; project interpretation |

### 6.4 Importance stratification

| ID | Requirement | Source pages |
|---|---|---|
| MED-IMP-001 | Each medically relevant variable or observation must be assigned an importance tier of A, B, or C in the context of the case and medical issue being screened. | 8; project interpretation of scope |
| MED-IMP-002 | Tier A must represent objective evidence that directly affects diagnosis, treatment, or timing, including imaging, operation records, pathology, definitive tests, and critical dates. | 8 |
| MED-IMP-003 | Tier B must represent supporting evidence that becomes important when several observations converge, including symptom timing, repeated visits, treatment response, and differences between specialties. | 8 |
| MED-IMP-004 | Tier C must represent contextual evidence that cannot independently support a conclusion, including patient statements, occupation, accident circumstances, and descriptions of daily limitations. | 8 |
| MED-IMP-005 | The output must retain the reason for the assigned tier; the letter alone is not sufficient for audit or calibration. | 8; project interpretation |
| MED-IMP-006 | Referral logic must not use importance tier as a standalone decision rule. | 8-9 |
| MED-IMP-007 | The tier assignment must record its codebook version and may be revised when new evidence changes the case context; revision must preserve history rather than overwrite the prior assignment. | Project interpretation |

### 6.5 Anomaly screening and issue accumulation

| ID | Requirement | Source pages |
|---|---|---|
| MED-ANM-001 | The system must distinguish variable importance from anomaly severity. | 8-9 |
| MED-ANM-002 | Anomaly severity must support at least minor, needs-confirmation, and significant states, with operational definitions to be calibrated during the PoC. | 9 |
| MED-ANM-003 | Every anomaly must identify the medical issue it concerns, such as diagnosis basis, timing, clinical course, missing evidence, imaging, differential diagnosis, or prior-condition/prognostic effect. | 9-10 |
| MED-ANM-004 | The system must group multiple anomalies that converge on the same medical issue. | 9 |
| MED-ANM-005 | The system must not use a simple total anomaly count as the referral rule. | 9 |
| MED-ANM-006 | A significant Tier-A conflict that remains after source reinspection should create a high referral likelihood. | 9 |
| MED-ANM-007 | Multiple Tier-B anomalies supporting the same causation or clinical-course concern should increase referral likelihood. | 9 |
| MED-ANM-008 | Ambiguity limited to Tier-C context may pass without referral when it does not affect a core question. | 9 |
| MED-ANM-009 | The output must explain which observations accumulated, how they relate, and why they did or did not justify referral. | 9; project interpretation |
| MED-ANM-010 | A medically uncertain inference must remain an anomaly or question for review; it must not be rewritten as an established fact. | 2, 10 |

### 6.6 Referral signal categories

The initial referral taxonomy must support the following categories. Codes and final
English/Korean labels are TBD and should be established in one codebook rather than
repeated across prompts and schemas.

| ID | Referral category | Required meaning | Source page |
|---|---|---|---|
| MED-SIG-001 | Imaging review required | Image interpretation is required, or an imaging report conflicts with another record. | 10 |
| MED-SIG-002 | Critical evidence missing | A prerequisite document or result is absent, preventing assessment of a material question. | 10 |
| MED-SIG-003 | Diagnostic basis questioned | Tests or records do not clearly support the stated diagnosis, or material sources conflict. | 10 |
| MED-SIG-004 | Treatment/course mismatch | Diagnosis, treatment, response, and recovery contain a major unexplained discontinuity. | 10 |
| MED-SIG-005 | Differential diagnosis required | Another plausible diagnosis could materially affect loss analysis. | 10 |
| MED-SIG-006 | Prior condition/prognostic factor | Prior arthritis, diabetes, neuropathy, or another factor may affect prognosis or function. | 10 |
| MED-SIG-007 | Diagnosis reference-date issue | Acute/chronic classification or pre/post-policy timing depends on the medical reference date. | 10 |
| MED-SIG-008 | Disability-related objective evidence | Objective functional material should be reviewed for a later disability assessment. | 10 |

### 6.7 Source traceability and retrieval

| ID | Requirement | Source pages |
|---|---|---|
| MED-SRC-001 | Every core variable, anomaly, issue cluster, referral reason, and expert-facing question must carry one or more evidence references. | 3, 5, 12 |
| MED-SRC-002 | A reference must identify the document name/identifier, document date when available, page, and the relevant sentence, verbatim quote, or table location/value. | 3, 12 |
| MED-SRC-003 | The design must support a more precise location when needed, such as section, table, row, or page region; the exact representation is TBD. | 12 |
| MED-SRC-004 | A human reviewer must be able to navigate from a structured item or referral package to the relevant source page without searching the whole case. | 3, 12-13 |
| MED-SRC-005 | Downstream report work must be able to return from a disputed or ambiguous statement to the medical variable and then to its source evidence. | 12 |
| MED-SRC-006 | Source navigation must preserve repository guardrails: agents consume the DAO-managed processed layer, while any human access to original material occurs through a controlled read-only path. | Project guardrails P2/D1 |
| MED-SRC-007 | A required item with a missing or invalid source reference must fail contract validation, move the stage to explicit review, and remain unavailable to downstream consumers until corrected or formally dispositioned. | Project guardrails P1/P4/P7 |

### 6.8 Expert referral decision

| ID | Requirement | Source pages |
|---|---|---|
| MED-REF-001 | The referral decision must consider importance, anomaly severity, and accumulation around the same medical issue. | 8-9 |
| MED-REF-002 | The decision must state `refer`, `do_not_refer`, or `insufficient_information`; a boolean alone is insufficient. | 9; project interpretation |
| MED-REF-003 | The decision must include a human-readable rationale and references to the contributing issue cluster. | 3, 9-10 |
| MED-REF-004 | A referral must identify the reviewer class and, when possible, the appropriate medical specialty. | 3, 13 |
| MED-REF-005 | A referral recommendation is not a finding that a diagnosis or treatment is wrong. User-facing language must preserve that boundary. | 10 |
| MED-REF-006 | Thresholds and weights must be versioned and auditable so PoC calibration can be tied to the logic that produced each decision. | 9, 16; project interpretation |
| MED-REF-007 | Genuine source contradictions remain subject to the conflict ledger; an unresolved medical interpretation is routed through this referral flow. The implementation must not conflate those two conditions. | Project guardrail P6 |
| MED-REF-008 | OCR disagreement remains a P8 extraction gate and must be resolved before medical screening. It is not a medical anomaly. | Project guardrail P8 |
| MED-REF-009 | Before referral, the system or authorized reviewer must reinspect the linked source and record that the medical anomaly or missing-evidence question remains unresolved. | 9-10 |

### 6.9 Expert referral package

| ID | Requirement | Source page |
|---|---|---|
| MED-PKG-001 | The package must include a concise accident/onset and case summary. | 13 |
| MED-PKG-002 | The package must include a diagnosis, test, treatment, and clinical-course timeline. | 13 |
| MED-PKG-003 | The package must include prior conditions and prognostic factors relevant to the issue. | 13 |
| MED-PKG-004 | The package must state exactly what is unclear or conflicting and why expert review is requested. | 13 |
| MED-PKG-005 | The package must identify the narrow medical issue and suggested reviewer specialty. | 13 |
| MED-PKG-006 | The package must include the relevant documents/pages and all materially conflicting evidence. | 13 |
| MED-PKG-007 | The expert must be able to open the source evidence from the package. | 13 |
| MED-PKG-008 | The package must ask a focused question about a specific issue; broad questions about the appropriateness of all treatment are non-conforming. | 13 |
| MED-PKG-009 | Package generation must not hide exceptions or omit evidence that points away from the referral hypothesis. | 17; project interpretation |

Example of the required question shape:

> Does the mismatch between the MRI finding and the lesion location in the
> diagnosis certificate affect interpretation of the final diagnosis?

Non-conforming question shape:

> Was all treatment in this case appropriate?

### 6.10 Structured expert response

| ID | Requirement | Source pages |
|---|---|---|
| MED-EXP-001 | The response must identify the referred medical issue. | 14 |
| MED-EXP-002 | The response must list the critical source material actually reviewed. | 14 |
| MED-EXP-003 | The response must record the expert's medical interpretation. | 14 |
| MED-EXP-004 | The response must record the basis for that interpretation. | 14 |
| MED-EXP-005 | The response must record uncertainty and reasonable alternative interpretations. | 14 |
| MED-EXP-006 | The response must state whether additional evidence is required and identify it when known. | 14 |
| MED-EXP-007 | The response may include advice for the later loss-adjustment stage, clearly separated from the medical interpretation. | 14 |
| MED-EXP-008 | The response must identify the real human reviewer, specialty, review timestamp, referral logic version, and package version. | 14; project interpretation |
| MED-EXP-009 | No agent may fabricate or silently complete an expert response. Missing human input remains waiting state. | Project guardrail P7 |

The existing `expert_review.schema.json` records a human disposition of critic findings
on a draft. It is not the contract requested here and should not be overloaded. A
medical consultation needs a separate contract and lifecycle.

### 6.11 Reuse of expert answers

| ID | Requirement | Source page |
|---|---|---|
| MED-KB-001 | Expert responses should be assigned issue codes that support later retrieval. | 15 |
| MED-KB-002 | Users should be able to search for similar prior issues and responses. | 15 |
| MED-KB-003 | Retrieved answers are reference material, not automatic decisions for the new case. | 15; safety interpretation |
| MED-KB-004 | Retrieval must preserve the prior answer's source scope, reviewer, specialty, date, uncertainty, and applicability limits. | 14-15; project interpretation |
| MED-KB-005 | Reuse must not expose case PII or cross a permission boundary; de-identification and access policy are prerequisites. | Project privacy constraints |
| MED-KB-006 | The PoC may defer automated retrieval, but it must store responses in a form that does not block future search. | 14-15 |
| MED-KB-007 | The reuse design should support fewer repeated consultations, narrower future questions, improved drafts, and greater consistency for similar issue types without converting prior answers into binding decisions. | 15 |

### 6.12 UI requirements

| ID | Requirement | Source pages |
|---|---|---|
| MED-UI-001 | The UI must present the case summary, medical timeline, issue clusters, referral rationale, and expert question as separate views or clearly separated sections. | 12-13 |
| MED-UI-002 | Evidence references must be interactive and open the relevant source location. | 3, 12-13 |
| MED-UI-003 | Conflicting sources must be visible side by side or otherwise comparable; the UI must not show only the selected interpretation. | 13 |
| MED-UI-004 | The UI must show whether expert input is pending, received, or no longer required. | 3, 13; project P7 |
| MED-UI-005 | The UI must capture a real expert response using the standardized response fields. | 14 |
| MED-UI-006 | The UI should show the referral logic version, issue code, and evidence completeness so a reviewer can audit why the case was routed. | 9, 16; project interpretation |
| MED-UI-007 | The UI must be explicit that a referral is a request for human interpretation, not an adverse medical finding. | 10 |

### 6.13 Minimum referral lifecycle

The approved PoC architecture uses the following canonical state names and actor
boundaries in `_medical_review_ledger.json`. These state semantics are implemented as a
human gate within `claim_analysis`; operational role authority remains disabled until
an approved role policy names real actors.

| Canonical state | Entered when | Permitted next outcomes | Owner |
|---|---|---|---|
| `decision_pending` | Medical observations and issue clusters are valid but referral logic has not completed. | `do_not_refer`, `needs_information`, or `package_ready`. | Approved policy router or authorized human coordinator, according to decision owner |
| `do_not_refer` | The auditable decision is `do_not_refer`. | Reopened only when new evidence or a recorded authorized override changes the inputs. | System record; authorized human may reopen with rationale |
| `needs_information` | The auditable decision is `insufficient_information`. | Evidence provided, decision rerun, authorized override, or cancellation. It must not silently become `do_not_refer`. | Authorized workflow coordinator |
| `package_ready` | The decision is `refer` and request version 1 validates atomically with it. | `awaiting_expert` or `cancelled`. | Authorized workflow coordinator |
| `awaiting_expert` | A named reviewer is assigned to one exact request ID/version. | `answered`, `expert_needs_information`, reassignment, or cancellation. | Assigned expert and coordinator |
| `expert_needs_information` | The assigned expert cannot answer from the closed package. | Supplemented package (new version), then assignment; reassignment; or cancellation. | Expert requests; coordinator resolves |
| `answered` | A genuine structured expert response validates. | `closed`, amendment, withdrawal, or `adjudication_required`. It remains blocking until disposition/closure. | Expert for content; coordinator for workflow |
| `adjudication_required` | At least two answered items contain materially conflicting expert opinions. | Human adjudication returns involved items to `answered`, then disposition; or atomic cancellation. | Authorized medical adjudicator |
| `cancelled` | An authorized human cancels the item with a reason. | `closed` or a new decision cycle through reopening. | Authorized workflow coordinator |
| `closed` | The response or unresolved/cancelled disposition has been acknowledged for downstream use. | Reopened only by a new recorded action; expert amendment/withdrawal rules preserve prior history. | Authorized workflow coordinator |

| ID | Lifecycle requirement | Source |
|---|---|---|
| MED-LIF-001 | Every transition must record case, issue cluster, prior state, next state, actor, timestamp, reason, and relevant contract/logic versions. | Project interpretation; P5/P7 |
| MED-LIF-002 | Only a real authorized human may assign, cancel, override, close, or submit expert content where the transition is labeled human-owned. | Project interpretation; P7 |
| MED-LIF-003 | A `refer` decision must produce a contract-valid package before assignment; package-validation failure moves to explicit review rather than `awaiting expert`. | Project interpretation; P4/P7 |
| MED-LIF-004 | `Insufficient_information` must remain distinguishable from both `refer` and `do_not_refer`; the workflow must show what information is required next. | Project interpretation |
| MED-LIF-005 | Reassignment, cancellation, reopening, and override must preserve history and require a reason. | Project interpretation; P5 |
| MED-LIF-006 | A timeout may create an overdue/escalation signal but must not fabricate a response or silently close the referral. Service levels are TBD. | Project interpretation; P7 |
| MED-LIF-007 | An expert may amend or withdraw their response; the prior version remains auditable and downstream consumers are notified through run state or an equivalent versioned mechanism. | Project interpretation |
| MED-LIF-008 | Conflicting expert opinions must remain separate and route to declared human adjudication; no model may silently choose one. | Project interpretation; P7 |
| MED-LIF-009 | Human corrections to variables or issue clusters must be attributed, versioned, and trigger deterministic invalidation or rerun of dependent decisions and packages. | Project interpretation; P5 |

The approved non-UI lifecycle surface is exact and closed:

```text
read-medical-review-ledger CASE_ID
read-medical-review-evidence CASE_ID REVIEW_ITEM_ID REQUEST_ID REQUEST_VERSION LOCATOR_ID
open-medical-review-item CASE_ID --issue-id MCI_NNNN --decision-owner {policy|human} [--actor-file PATH] --held-by NAME --run-id RUN_ID
record-medical-referral-decision CASE_ID REVIEW_ITEM_ID --decision-file PATH [--actor-file PATH] --held-by NAME --run-id RUN_ID
transition-medical-review CASE_ID REVIEW_ITEM_ID ACTION --actor-file PATH [--data-file PATH] [--reason TEXT] --held-by NAME --run-id RUN_ID
check-medical-reviews-clear CASE_ID
reconcile-medical-review-waits CASE_ID --held-by NAME --run-id RUN_ID
```

`ACTION` is one of `provide_information`, `assign`, `request_information`,
`supplement_package`, `reassign`, `submit_response`, `amend_response`,
`withdraw_response`, `flag_conflict`, `adjudicate`, `cancel`, `close`, or `reopen`;
callers never supply an arbitrary next state. Pre-request source reinspection reads an
explicit immutable revision through `read-medical-evidence CASE_ID LOCATOR_ID
--revision-sha SHA`. After a request version exists, evidence viewing uses only
`read-medical-review-evidence`: the DAO derives the stored revision, enforces request
and closed-locator membership plus D1/source classification, and never substitutes the
current revision or current page/chunk text. Controlled-original access is deferred.

These command and schema surfaces are harness capability, not operational activation.
The bootstrap referral, request, role, and medical-structuring configurations remain
disabled. Real medical-lead policy approval, named actor authority, specialties,
controlled-original source access, and the supervised human dry run remain pending.

### 6.14 Condition-routing decision table

| ID | Condition | Owner and required route | Must not happen |
|---|---|---|---|
| MED-RTE-001 | Two extraction passes materially disagree about page content. | Document processing stops under P8 for human resolution before medical structuring. | Treating the disagreement as a medical anomaly. |
| MED-RTE-002 | Input or output violates its schema, identifier, or evidence-reference contract. | The owning stage fails validation and enters explicit review under P4/P7. | Promoting partial data or relabeling it as missing medical evidence. |
| MED-RTE-003 | Two case sources assert contradictory facts. | Record both positions in the P6 conflict ledger and halt dependent work until human resolution. | Letting the medical layer select a preferred fact. |
| MED-RTE-004 | The record is valid, but evidence medically required to assess an issue is absent. | Create a source-grounded missing-evidence record and decide whether evidence acquisition or expert review is useful. | Treating absence as proof of the negative conclusion. |
| MED-RTE-005 | Sources are valid, but their medical meaning, relationship, or implication remains uncertain. | Create an anomaly and issue cluster; apply the referral logic and focused-question workflow. | Rewriting the uncertainty as an established fact. |
| MED-RTE-006 | Two experts provide materially different interpretations. | Preserve both responses and invoke the declared human adjudication path. | Averaging, merging, or silently selecting an answer. |
| MED-RTE-007 | One observation is relevant to several medical questions. | Keep one source-grounded observation and allow explicit links to multiple issue clusters. | Duplicating the fact into drifting copies or bypassing a P6 conflict. |

## 7. Evaluation requirements

### 7.1 Metric definitions

The following definitions are a starting measurement contract. Denominators, sampling,
and confidence intervals must be fixed in an evaluation protocol before results are
reported.

| ID | Metric | Minimum definition | Source page |
|---|---|---|---|
| MED-MET-001 | Expert referral rate | Eligible cases referred for medical review divided by all eligible cases. Report overall and by issue category. | 16 |
| MED-MET-002 | Unnecessary referral rate | Completed referrals that the medical expert marks as not requiring expert review, divided by completed referrals. The exact expert disposition field is TBD. | 16 |
| MED-MET-003 | Source-linking rate | Required variables and anomaly signals with valid, resolvable source references divided by all variables and signals required to have references. | 16 |
| MED-MET-004 | Important-issue detection rate | Eligible issues from the final loss-adjustment report or an independently adjudicated benchmark that were detected during screening, divided by all applicable benchmark issues. Final-report ground truth may be accessed only through the evaluation-stage gate. | 16 |
| MED-MET-005 | Review-time reduction | Difference in human review time between the current full-record workflow and the referral-package workflow, using a predefined paired or controlled protocol. | 16 |
| MED-MET-006 | Summary/variable accuracy | Field-level agreement for diagnosis dates, treatment facts, prior conditions, and other structured variables against human-reviewed reference values. | 16 |
| MED-MET-007 | Referral yield by issue | Referrals that produce a material expert clarification divided by completed referrals, grouped by issue code. | Derived planning metric |
| MED-MET-008 | Miss rate | Applicable benchmark issues that should have triggered review but did not. This must be reported alongside unnecessary referrals so fewer referrals cannot masquerade as improvement. | Principle from page 16 |

### 7.2 Evaluation controls

| ID | Requirement |
|---|---|
| MED-EVL-001 | The evaluation set, benchmark issue definitions, and inclusion/exclusion rules must be fixed independently of model outputs. |
| MED-EVL-002 | Ground-truth-based issue detection must run only after the existing D1 human-review gate. |
| MED-EVL-003 | Referral thresholds must not be tuned and evaluated on the same cases without an explicitly labeled exploratory result. |
| MED-EVL-004 | Time measurements must define start/stop events and whether source-navigation time is included. |
| MED-EVL-005 | Results must be reported by case type and issue category where sample size allows; aggregate rates alone may hide systematic misses. |
| MED-EVL-006 | A metric reported as N/A must include a reason rather than being omitted or coerced to zero. |
| MED-EVL-007 | The PoC must preserve false negatives and false positives for review; it must not retain only successful examples. |
| MED-EVL-008 | Confirmatory evaluation cases must be excluded from expert-answer retrieval and threshold tuning, or separated by a declared temporal cutoff, so the system cannot recall the answers it is being scored against. |
| MED-EVL-009 | Benchmark issues and variable reference values must be produced by medical reviewers who are not shown the model output; disagreements must follow a declared adjudication process. |
| MED-EVL-010 | Review-time and unnecessary-referral studies should randomize or counterbalance workflow order where practical, and reviewers must not be told the expected direction of improvement. |
| MED-EVL-011 | The issue codebook may organize results, but the scorer must use independently reviewed case evidence rather than treating the system's own issue assignment as proof that an issue exists. |

## 8. Current capability and gap map

This table distinguishes executable technical harness surfaces from operational policy.
“Implemented” means code/schema/CLI behavior exists and is testable with synthetic data.
It does not mean the medical lead approved clinical content, that named humans are
authorized, or that real-case use is enabled. The disabled bootstrap configurations and
deferral register control that separate activation decision.

| Requested capability | Implemented non-UI foundation | Disabled or remaining gap |
|---|---|---|
| Evidence-linked longitudinal variables | `medical_common.schema.json`, `medical_variables.schema.json`, DAO publication/historical reads, immutable content-addressed revisions, and synthetic semantic tests represent variables, observations, issues, importance, source coverage, timelines, and locator-backed evidence. | Medical structuring config is disabled with no approved real case/document roles, clinical vocabulary, or counting policy. Optional fine `page_region` policy remains deferred. |
| Compatibility claim facts | `extracted_claim_fields.json` is published under the canonical medical lock as a deterministic read-only projection with a required canonical revision SHA and per-field variable/observation/rule/config provenance. Projection reads fail closed if the canonical revision does not match. An interruption before the canonical commit point is recovered by idempotently retrying `write-medical-variables`; provenance-incomplete historical fixtures must declare `legacy_pre_medical` and are rejected as canonical medical inputs. | Projection config and primary-diagnosis precedence remain disabled pending approved document roles/policy. |
| Medical-review lifecycle | Separate referral/request/response/ledger schemas and DAO commands implement item creation, human-owned decisions, information cycles, package versioning, assignment/reassignment, responses, amendment/withdrawal, cancellation, closure/reopening, shared conflict adjudication, immutable history, request-scoped evidence, canonical wait projection, reconciliation, and independent-process locking. Focused lifecycle/recovery/concurrency tests exercise the complete non-UI action matrix. This remains distinct from `expert_review_v{n}.json` critic review. | Referral, request-vocabulary, and role policies are disabled; no real thresholds, interpretation codes, specialty routes, or named authorized actors exist, and the supervised real-medical-professional dry run is pending. |
| Structural downstream gate | `check-medical-reviews-clear` reads and validates the canonical ledger, treats `answered` and all other unresolved states as blocking, and fails closed on a missing/malformed ledger. Canonical agent/orchestrator instructions require it before claim-analysis completion and every downstream stage after medical publication. | Old-run migration/backfill remains deferred; pre-adoption runs stay explicit legacy mode and cannot claim medical screening. |
| Conflict/route separation | P4/P6/P8 and medical-review schemas keep contract failure, factual conflict, extraction disagreement, and medical interpretation as separate routes. | Clinical anomaly definitions, convergence rules, and policy thresholds remain unapproved. |
| Revision-pinned source navigation | `read-medical-evidence` supports pre-request reinspection of an explicit immutable revision. `read-medical-review-evidence` derives the stored request revision and enforces request/version/closed-locator/source-classification/D1 membership without falling forward. | Controlled-original source access is disabled; only redacted revision-pinned quote/table evidence is available. Production authentication/authorization is unresolved. |
| Screening/referral output | Closed referral-input, decision, focused-package, response, and lifecycle contracts prevent final medical/insurance verdict fields and preserve balanced evidence/uncertainty. | No approved real-case anomaly producer, specialty taxonomy, requested-interpretation codebook, or referral policy is active. |
| Human-facing UI | The current localhost UI reads DAO-backed medical state and is not the authority for state or clearance. Any future lifecycle mutation surface must invoke the purpose-built DAO commands rather than write state itself. | UI presence is not authentication, medical-lead approval, or operational readiness; remote/production workflow and authenticated mutation remain deferred. |
| Evaluation | Existing D1 gates remain separate, and synthetic tests can verify structural correctness without answer keys. | Referral effectiveness, traceability, time-saving, medical-variable accuracy, issue-detection metrics, benchmark protocol, and Go/No-Go thresholds remain unapproved/unimplemented as operational evaluation. |
| Knowledge reuse | Structured expert responses are stored case-locally with issue/provenance/version data so future search is not blocked by shape. | No cross-case index, retrieval API, de-identification/access policy, or automatic reuse exists. |

## 9. Development missions and acceptance criteria

The source proposal names six missions. They remain the initiative acceptance anchors;
parts of the non-UI contract/ledger harness now exist, while operational activation and
later UI/evaluation/reuse acceptance remain governed by the capability map and deferrals.

### M1. Medical-variable data model

**Outcome:** A schema-validated, longitudinal medical representation covering
 diagnosis, dates, tests/findings, treatments, clinical course, prior conditions,
 prognostic factors, missing evidence, and source references.

**Minimum acceptance criteria:**

- Every `MED-DAT-*` requirement has a field or an explicit documented deferral.
- The model supports multiple observations and contradictory values without deleting
  alternatives.
- Unknown, absent, contradictory, and not-applicable states validate distinctly.
- Representative samples validate for at least diagnosis, imaging, surgery,
  repeated treatment, clinical course, and prior-condition scenarios.
- No real case data is used in schema tests; tests use synthetic fixtures under
  `tmp_path`.

### M2. Source evidence linking

**Outcome:** Every medical variable and signal can be traced to a source and opened by
an authorized human.

**Minimum acceptance criteria:**

- Every required item with a missing or invalid evidence reference fails contract
  validation and moves the stage to explicit review; it cannot be promoted
  downstream.
- Document name/identifier, document date when available, page, and sentence/quote or
  table location are mandatory where source material exists.
- The chosen fine-location representation is documented and tested.
- A UI/API integration test proves navigation from a medical variable to the correct
  source location.
- Agent and human access paths remain compliant with P2 and D1.

### M3. Document focus policy

**Outcome:** A documented, testable policy identifies which records receive deeper
medical structuring and which serve as supporting context.

**Minimum acceptance criteria:**

- The policy covers diagnosis, objective tests, treatment, clinical course, and
  itemized billing records.
- Document types map to extraction objectives rather than only generic labels.
- Unsupported or `other` documents fail visibly into review/configuration rather than
  being silently skipped.
- Extraction depth is configuration or codebook data, not duplicated prompt prose.

### M4. Expert referral logic v0.1

**Outcome:** Versioned referral logic combines importance, severity, and issue
accumulation and produces an auditable decision.

**Minimum acceptance criteria:**

- A/B/C importance and anomaly severity are represented separately.
- Multiple signals can link to one issue cluster.
- The decision has three states: refer, do not refer, insufficient information.
- Every decision records its contributing signals, rationale, evidence, and logic
  version.
- Unit scenarios cover significant Tier-A conflict, converging Tier-B anomalies,
  context-only Tier-C ambiguity, missing critical evidence, and mixed signals.
- The logic never emits a final medical appropriateness or loss-adjustment verdict.

### M5. Expert referral package UI

**Outcome:** A medical expert can understand and answer a narrow issue without reading
the entire case first.

**Minimum acceptance criteria:**

- The package displays all fields required by `MED-PKG-*`.
- Source links are resolvable and conflicting evidence remains visible.
- The expert can record every `MED-EXP-*` field.
- Waiting/received status is driven by run state, not inferred from UI state.
- A user test measures package comprehension and review time against the current
  workflow.

### M6. PoC metrics dashboard

**Outcome:** The team can inspect referral quality, traceability, time savings, issue
detection, and variable accuracy without recomputing metrics manually.

**Minimum acceptance criteria:**

- Metric definitions and denominators are versioned.
- The dashboard exposes `MED-MET-001` through `MED-MET-006` plus the safety-critical
  miss rate in `MED-MET-008`; `MED-MET-007` is recommended but not required for
  source-request conformance.
- Rates show numerator, denominator, exclusions, and N/A reasons, not only a
  percentage.
- False-negative and unnecessary-referral examples can be inspected.
- Ground-truth-backed metrics are unavailable before the evaluation gate.

## 10. Planning order and dependency map

```text
M1 medical data model
   +
M2 source linking
   +
M3 document focus policy
   -> M4 referral logic v0.1
      -> M5 expert package UI and structured response capture
         -> M6 metrics dashboard and calibration loop

Structured expert response contract from M5
   -> later permission-safe expert-answer retrieval/reuse
```

Recommended planning slices:

1. **Contract foundation:** medical observations, timeline, evidence locator, missing
   evidence, issue taxonomy, referral decision, expert response.
2. **Deterministic extraction support:** document-focus codebook, treatment counting,
   date normalization, and contract validation.
3. **Medical reasoning boundary:** importance, anomaly severity, issue clustering,
   referral rationale, and prohibited final-judgment checks.
4. **Human workflow:** run-state stage(s), review request/receipt, source navigation,
   package UI, and response capture.
5. **Evaluation:** benchmark protocol, metric contracts, aggregation, dashboard, and
   calibration reports.
6. **Reuse:** de-identification/access policy, issue indexing, similarity retrieval,
   and explicit non-binding presentation of prior answers.

### 10.1 Mission activation gates

The generic fail-closed schemas and synthetic harness may be implemented under the
approved technical plans. The decisions below block enabling real clinical/operational
behavior; they are not silently resolved by code or tests.

| Mission | Blocking decisions before operational behavior is enabled | Minimum approval |
|---|---|---|
| M1 | MED-OQ-002, MED-OQ-009, MED-OQ-014, MED-OQ-015, MED-OQ-017 | Medical lead for the variable model; engineering for contracts/versioning |
| M2 | MED-OQ-002, MED-OQ-007, MED-OQ-008, MED-OQ-014, MED-OQ-019 | Medical lead for required evidence; privacy/security and engineering for access |
| M3 | MED-OQ-009, MED-OQ-014, MED-OQ-015 | Medical lead for document focus and counting basis |
| M4 | MED-OQ-003 through MED-OQ-006, MED-OQ-014, MED-OQ-015, MED-OQ-018 | Medical lead for clinical codebooks; product for operating thresholds/lifecycle |
| M5 | MED-OQ-001 through MED-OQ-003, MED-OQ-007, MED-OQ-008, MED-OQ-012, MED-OQ-018, MED-OQ-019 | Product, medical workflow owner, privacy/security, and engineering |
| M6 | MED-OQ-010, MED-OQ-011, MED-OQ-014 through MED-OQ-016 | Evaluation owner and medical adjudication lead |
| Retrieval/reuse | MED-OQ-012, MED-OQ-013, MED-OQ-016, MED-OQ-019 | Privacy/security, medical lead, and product |

An approver is a role here, not a named person. Before execution, each plan must name
the responsible person or decision record, define the deliverable, and state the
objective check that closes the decision. Unknown numerical thresholds, fixture
counts, service levels, and statistical targets remain plan inputs; they must not be
invented merely to make an acceptance criterion look precise.

Each implementation plan should cite the relevant requirement IDs and state which open
decisions it resolves. A change is not complete merely because code exists; it must
include schema validation, isolated tests, guardrail reconciliation, frontend impact,
and evaluation impact where applicable.

## 11. Decision and deferral register

Technical architecture decisions approved on 2026-07-23 are recorded as resolved or
partially resolved below. “Resolved for the PoC harness” means the contract/state-machine
shape may be implemented and tested synthetically; it does not mean clinical policy,
real actors, source access, or production operation is active. Operational remainder is
governed by `medical-appropriateness-screening-deferrals.md`.

| ID | Status | Decision or remaining question | Why it matters |
|---|---|---|---|
| MED-OQ-001 | Resolved for PoC harness | Medical structuring and medical review are checkpoints/a human gate within `claim_analysis`; no new top-level or autonomous medical stage. Future production ownership may be revisited. | Fixes run-state, orchestration, retries, backups, and ownership for this implementation. |
| MED-OQ-002 | Resolved for PoC harness | Canonical medical-variable, immutable-revision, referral, request/response, and `_medical_review_ledger.json` schemas/filenames are owned by the approved plans. | Prevents medical facts from being spread across warnings or unrelated contracts. |
| MED-OQ-003 | Deferred | What specialty taxonomy and routing rules should supplement `reviewer_role: "의사"`? | The bootstrap contains only `unspecified` and requests remain disabled. |
| MED-OQ-004 | Deferred | What operational definitions separate minor, needs-confirmation, and significant anomalies? | Required for consistent referral logic and evaluation. |
| MED-OQ-005 | Deferred | How are multiple signals assigned to the same issue cluster? | Issue accumulation is central and cannot be replaced by raw counts. |
| MED-OQ-006 | Deferred | What thresholds produce `refer`, `do_not_refer`, and `insufficient_information` in v0.1? | The referral policy remains disabled until medically approved. |
| MED-OQ-007 | Partially resolved | v0.1 supports document/page/quote or table location; what additional `page_region` granularity is approved? | Drives later source-view precision. |
| MED-OQ-008 | Partially resolved | Redacted, revision-pinned request evidence is the only current boundary; may a reviewer later see controlled originals? | Controlled-original access remains disabled pending privacy/workflow approval. |
| MED-OQ-009 | Deferred | What is the counting basis for injections, manual therapy, rehabilitation, and repeated tests? | Partial records can otherwise produce misleading counts. |
| MED-OQ-010 | Deferred | How are benchmark medical issues defined without leaking ground truth into generation stages? | Required for a valid important-issue detection metric. |
| MED-OQ-011 | Deferred | What study design measures review-time reduction and unnecessary referrals? | Denominators and workflow timing must be fixed before collecting results. |
| MED-OQ-012 | Partially resolved | Same-case structured responses live in the canonical review ledger. Who may search cross-case responses, and under what de-identification/access policy? | Cross-case search/reuse remains unimplemented and disabled. |
| MED-OQ-013 | Deferred | When may a prior expert answer be shown for a new case, and how are applicability limits displayed? | Prevents reference material becoming an automatic verdict. |
| MED-OQ-014 | Deferred | What named medical-professional review approves the variable model, issue taxonomy, and referral logic? | Technical implementation approval is not medical-lead approval. |
| MED-OQ-015 | Deferred | Which case types and specialties are in the first PoC slice? | Enabled real-case/document/specialty sets remain empty. |
| MED-OQ-016 | Deferred | What sample sizes, target ranges, confidence reporting, and success/failure thresholds define the PoC evaluation protocol? | No Go/No-Go or effectiveness claim is currently authorized. |
| MED-OQ-017 | Partially resolved | Generic closed schemas and versioning are fixed; which domain field types, units, coding systems, correction policy, and extension vocabulary receive clinical approval? | Bootstrap medical structuring remains disabled. |
| MED-OQ-018 | Partially resolved | Lifecycle states, actions, role vocabulary, actor-assertion shape, and wait semantics are fixed. Which named people may act, and what service levels/escalations apply? | Human role policy remains disabled with no named actors. |
| MED-OQ-019 | Deferred | What authentication, authorization, audit, retention, de-identification, tenant-boundary, accessibility, localization, and production source-view requirements apply? | Local actor assertions are audit metadata, not authentication; production/remote use is non-conforming. |
| MED-OQ-020 | Partially resolved | For an adopted new run, medical-variable publication activates the fail-closed ledger gate before claim-analysis completion and every downstream stage. What are the old-run migration/backfill, invalidation, cost, latency, and reproducibility policies? | Pre-adoption runs remain explicit legacy mode and are not silently backfilled or called medically screened. |

## 12. Cross-cutting project constraints

All implementation work remains subject to the existing harness guardrails. In
particular:

- **P1:** Every extracted value and assertion must trace to source evidence.
- **P2:** Agents read through the DAO-managed processed layer, not raw source files.
- **P3:** Medical inferences are hedged and routed for review, not asserted as final.
- **P4/P9:** Invalid or partial contract output does not silently continue downstream.
- **P5/P10:** Writes use locks, run state, and backups through sanctioned paths.
- **P6:** Contradictory case-source facts remain in the conflict ledger.
- **P7:** A medical expert response is genuine human input and is never fabricated.
- **P8:** Extraction disagreement is resolved before medical structuring consumes the
  text.
- **P11:** After medical-variable publication, the canonical medical-review ledger must
  be valid and clear before claim-analysis completion and every downstream stage;
  missing/malformed ledgers fail closed.
- **D1/D2:** Ground truth remains isolated and source classification remains
  human-approved during the PoC.

Normative project references for planning are:

- [Canonical universal harness guardrails](../.claude/skills/harness-guardrails/SKILL.md)
- [Canonical PoC/evaluation guardrails](../.claude/skills/harness-guardrails-dev/SKILL.md)
- [Live pipeline and stage ownership](../pipeline.md)
- [`extracted_claim_fields` contract](../schemas/extracted_claim_fields.schema.json)
- [Common component/evidence contract](../schemas/common_component_output.schema.json)
- [Existing critic-finding human-review contract](../schemas/expert_review.schema.json)
- [DAO data/state boundary](../tools/dao.py)

These links define the project terms summarized above. This document pins the exact
medical lifecycle command boundary needed for safe operation but intentionally does not
duplicate every schema field or transition payload. A plan must pin the repository
revision it was written against and re-check these references before execution.

Adding a stage or canonical stage name requires updating the run-state enum,
orchestrator, agent/skill documentation, frontend pipeline definition, and generated
agent copies in the same change. Canonical `.claude/` definitions remain the source;
`tools/sync_agents.py` regenerates `.agents/` and `.codex/` copies.

## 13. Source-to-requirement traceability

| PDF page | Source topic | Requirement groups |
|---|---|---|
| 1 | Purpose and medical structuring proposal | Executive requirement |
| 2 | Screen rather than decide; structure, trace, selectively refer | MED-PR |
| 3 | Source links, source viewing, focus, referral reasons, reuse | MED-SRC, MED-REF, MED-KB |
| 4 | New layer between OCR and downstream work | Target workflow |
| 5 | Layer responsibilities and core medical-variable scope | MED-DAT, MED-ANM, MED-REF |
| 6 | Priority evidence and treatment-count records | MED-DOC, MED-QTY |
| 7 | Common medical judgment axes | MED-DAT |
| 8 | A/B/C importance stratification | MED-IMP |
| 9 | Severity, issue accumulation, and referral logic | MED-ANM, MED-REF |
| 10 | Referral-signal examples | MED-SIG |
| 11 | Repeated treatment and quantitative patterns | MED-QTY |
| 12 | Source traceability and return-to-source flow | MED-SRC, MED-UI |
| 13 | Focused expert package and question design | MED-PKG, MED-UI |
| 14 | Standardized expert response | MED-EXP |
| 15 | Long-term reuse of expert answers | MED-KB |
| 16 | Required PoC metrics | MED-MET, MED-EVL |
| 17 | Benefits, risks, mitigations, and human-final-confirmation boundary | Scope, constraints |
| 18 | M1-M6 development missions | Development missions |
| 19 | Structure, screen, collaborate | Executive requirement |

## 14. Definition of initiative-level done

The medical professionals' request is materially implemented only when all of the
following are true:

1. M1 through M6 meet their minimum acceptance criteria or carry explicit, approved
   deferrals.
2. Every implemented requirement ID has test, inspection, or evaluation evidence.
3. Every medical variable and signal can be traced to source evidence through a
   sanctioned access path.
4. Referral decisions are explainable, versioned, and distinct from medical verdicts.
5. A real medical expert can review a focused package and submit a structured answer
   without an agent fabricating any part of that answer.
6. The team can measure referral rate, unnecessary referrals, source linking,
   important-issue detection, review time, and variable accuracy with declared
   denominators and exclusions.
7. Existing guardrails, ground-truth isolation, conflict handling, run-state recovery,
   and generated-agent synchronization remain intact.
8. A medical professional signs off that the variable model, issue taxonomy, and
   referral questions preserve the intended clinical boundary.
