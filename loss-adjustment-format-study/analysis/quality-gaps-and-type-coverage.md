# Loss-adjustment format study: quality gaps and type coverage

## Purpose

This document is the explicit quality ledger for the loss-adjustment format study. It separates safeguards already implemented from partially addressed limitations and work that remains open. It also records the evidence base for each document family so that a small or missing family is not mistaken for a mature template.

This ledger concerns document construction and authoring-contract coverage. It does not assess whether a historical report's legal, medical, or payment conclusion was correct.

## Status vocabulary

- **Addressed**: the current branch contains a documented and machine-checked control for the gap.
- **Partially addressed**: a control exists, but the corpus breadth or assurance level remains limited.
- **Open**: the required control, evidence, or integration does not yet exist.
- **Accepted limitation**: the limitation is explicitly bounded and does not block this local research study, but it would need a new decision before broader operational use.

## Addressed gaps

| Gap | Disposition | Evidence in the current branch |
|---|---|---|
| Coarse labels such as `교통사고` or `개인보험` can merge legally different report forms. | Addressed | The authoring contract selects a `family` and a compatible `claim_mechanism`. Automobile third-party compensation and first-party self-injury are separate families; personal accident and disease benefits are also separate. |
| Category guidance could remain prompt-only and drift from the schema. | Addressed | The JSON Schema enumerates the family/mechanism map and requires family-specific issues and calculation categories. Tests require every schema family and mechanism to appear in the rulebook. |
| Full and short reports could be forced into one section layout. | Addressed | `mode` is explicitly `full` or `compact`; full mode requires all canonical components, while the compact automobile sequence is separately documented. |
| A report could imitate practitioner certainty without evidentiary support. | Addressed | Material statements require evidence references; unresolved medical, legal, causation, exclusion, or calculation questions must remain review-gated. Model-authored final approval is prohibited. |
| Category-specific calculations could be represented as unchecked prose. | Addressed | Calculations use typed operations and inputs, exact rational recomputation, dimensional rules, explicit rounding, and final-amount reconciliation. |
| A family could be paired with the wrong event type or claim mechanism. | Addressed | The custom validator checks family/mechanism compatibility and event-type compatibility. |
| Protected answer-key derivatives could enter the branch or pull-request diff. | Addressed | Raw sources, OCR caches, extracted sections, reviewed source-path metadata, and the report index are ignored. Only aggregate analysis, rules, schema, tools, tests, and a pseudonymous example are version-controlled. |
| The completed-corpus claim could be confused with current-candidate validation. | Addressed | `validation-summary.json` separates historical aggregate facts, clean-checkout evidence, and the authorized external-fixture check. |
| The local research tool accumulated production-grade concurrency complexity beyond its actual operating model. | Addressed as an accepted limitation | The current workflow is explicitly scoped to one cooperative local writer. The documentation no longer claims crash recovery, adversarial pathname resistance, or concurrent mutation safety. |

## Partially addressed gaps

| Gap | Current control | Remaining limitation / closure condition |
|---|---|---|
| Boundary correctness is not the same as extraction fidelity. | Every classification and range received primary review plus a second OCR/text review; five representative boundaries received visual review. | The second review used the same OCR sidecars rather than independent OCR or all-source visual inspection. Broader visual or independently extracted review is needed for higher assurance. |
| Most OCR sidecars were inherited from the earlier study generation. | All sidecars are bound to source and sidecar hashes; selected output text and rendered PDF pages validate against the reviewed source range. | 83 of 95 sidecars were adopted as reviewed legacy text rather than freshly re-OCRed. Fresh OCR is needed to make a current-toolchain OCR-accuracy claim for every page. |
| Family-specific rules exist, but evidence breadth is uneven. | Every defined family has rulebook and schema coverage. | A document family is not mature merely because it has schema coverage. The type inventory below must show the actual number and variety of supporting reports. |
| Category metrics are reproducible. | Phrase and component counts come from versioned deterministic regex definitions. | The signals are lower bounds, not independently adjudicated semantic labels. They must not be described as exact component prevalence. |
| The authoring contract covers every family structurally. | Tests synthesize family changes and reject incompatible requirements. | Only the disease-benefit family has a complete version-controlled example. Each supported family needs a representative pseudonymous conformance fixture. |
| The format study is ready for downstream use as a contract. | Rules, schema, validator, and tests exist. | The live claim-analysis, draft-report, template registry, and document-assembly flow do not yet consume this contract. |

## Open gaps

1. Migrate the protected `analysis/report-index.csv` to the current canonical headers, regenerate corpus metrics, and pass the staleness check.
2. Obtain human confirmation for subtype candidates that appear only as aggregate keyword signals rather than reviewed primary-type labels.
3. Add representative pseudonymous examples for every family intended for pipeline use.
4. Decide whether the study's one-producer-heavy corpus is sufficient for the intended deployment population; add other producers before claiming industry-wide coverage.
5. Integrate family selection and report validation into the live pipeline, or explicitly keep this package research-only.

## Document-type re-audit

Status: **completed for the current 95-document fixture on 2026-08-11**.

### Method and assurance

The audit used the reviewed report index for family counts and deterministic keyword presence over the 94 protected extracted report texts. It emitted aggregate counts only. No raw report text, filenames, source paths, claimant information, or per-document matches were copied into this document.

Two signal scopes were compared:

1. the complete selected report, which is sensitive to policy clauses and secondary issues; and
2. the first four extracted report pages, which normally contain covers and the adjustment summary and are more likely to identify the assigned claim type.

Keyword counts are lower-bound presence signals, not mutually exclusive or human-adjudicated subtype labels. A signal in a complete report may identify a discussed rider, exclusion, deduction, or comparison rather than the report's primary assignment. The tables therefore separate reviewed structural findings from candidates that still require human confirmation.

### Reviewed family inventory

| Reviewed corpus group | Source documents | Reports | Current authoring disposition |
|---|---:|---:|---|
| `automobile_ta` | 16 | 16 | Split by mechanism into `automobile_compensation` and `automobile_self_injury`. |
| `personal_accident_benefit` | 40 | 40 | `personal_accident_benefit`; full benefit-report form. |
| `liability_damages` | 22 | 22 | `liability_damages`, with insured-liability and mutual/cooperative mechanisms. |
| `disease_benefit` | 8 | 8 | `disease_benefit`; definition-to-medical-record analysis. |
| `poc_mixed` | 9 | 8 | A source grouping, not an authoring family; its reports must be assigned by mechanism. One source contains no report. |

Total: 95 source documents, 94 reports, one reviewed no-report document.

### Confirmed structural types already present

| Type present | Evidence strength | Current treatment |
|---|---|---|
| Third-party automobile bodily-injury compensation | Strong: 15 compact forms in the reviewed analysis; 14 summary-scope keyword hits are a lower bound. | `automobile_compensation`, normally `compact`. |
| First-party automobile self-injury benefit | Thin: one full report and one summary-scope signal. | `automobile_self_injury`, `full`; more examples required before calling the layout mature. |
| First-party personal-accident disability benefit | Strong: 40-family corpus; 33 summary-scope disability signals are a lower bound. | `personal_accident_benefit`, normally `full`. |
| Third-party liability damages | Strong: 22 reports with a stable damages reasoning skeleton. | `liability_damages`, `full`. |
| Disease/diagnosis-triggered benefit | Moderate: eight dedicated reports plus diagnosis reports in the mixed group. | `disease_benefit`, `full`. |
| Portal-style short report | Thin: one reviewed short form. | Presentation variant only; it does not replace the underlying claim family or reasoning requirements. |

### Additional subtypes or mechanisms found

These were omitted from the earlier high-level summary and demonstrate that the corpus contains more variety than five folder labels imply.

| Additional observed subtype/mechanism | Aggregate evidence | Disposition |
|---|---|---|
| Mutual/cooperative liability | Explicitly discussed in the reviewed analysis; four complete-report signals and three summary-scope signals in the liability group. | Represented as `mutual_or_cooperative_liability` under `liability_damages`; needs its own example fixture. |
| Premises/owner-manager liability | One summary-scope signal. Earlier project inventory also identifies premises liability as a real basis. | Covered only generically by `insured_liability`; retain as an explicit subtype candidate. |
| Commercial/general liability | One complete-report signal. | Covered only generically by `insured_liability`; human-confirm the primary assignment before promoting it to a subtype. |
| Daily-life liability | One complete-report signal, absent from the summary-scope scan. | Present as a discussed coverage signal, but not yet confirmed as the primary report type. |
| Medical-malpractice liability | One complete-report signal and an explicit reviewed-analysis example. | Valid liability subtype; needs a dedicated pseudonymous fixture. |
| Employer/workers-compensation interaction | Two complete-report signals across the corpus; one summary-scope signal in the mixed group. The reviewed analysis confirms a workers-compensation deduction variant. | Model as liability/damages subtype or deduction context, not a new top-level family without human review. |
| Driver-injury personal policy | One summary-scope signal in the personal-accident family. | Personal-accident subtype candidate; one example is insufficient for a dedicated layout. |
| Cerebrovascular diagnosis benefit | Seven complete-report signals and five summary-scope signals across dedicated and mixed groups. | `disease_benefit`; carrier-specific definitions must remain separate per rider. |
| Cardiac diagnosis benefit | Five complete-report signals and three summary-scope signals in the disease group. | `disease_benefit`; document a representative fixture. |
| Cancer diagnosis benefit | One complete-report and one summary-scope signal. | Thin disease-benefit subtype. |
| Surgery benefit | One complete-report signal but no summary-scope signal. | A rider/content signal, not yet a confirmed standalone report type. |
| Congenital-condition benefit/limitation | One complete-report and one summary-scope signal. | Thin disease-benefit subtype or condition; human confirmation required. |
| Property/fire terminology | Three complete-report signals in mixed/liability reports, but no summary-scope signal and no property-loss family. | Discussed coverage or context is present; no primary property/fire adjustment assignment is confirmed. |

### Coverage model implied by the audit

A single flat category is insufficient. The observed reports need at least four independent descriptors:

1. **Coverage basis**: `배상책임`, `개인보험`, or `자동차보험`.
2. **Loss/benefit type**: at minimum `후유장해` and `진단·수술비`; other candidates below require confirmation or new material.
3. **Claim mechanism/subtype**: for example third-party automobile compensation, automobile self-injury, mutual/cooperative liability, premises liability, or a carrier-specific disease rider.
4. **Presentation mode**: `full`, `compact`, or the thinly evidenced portal-style presentation.

The authoring schema already captures family, mechanism, and presentation mode. The live pipeline's two-axis case taxonomy captures coverage basis and broad loss type, but it does not yet preserve every subtype listed above.

### Types not evidenced or not confirmed in the current corpus

The deterministic audit found no usable evidence for the following as primary report types. Absence of a keyword is not proof of absence, but these also lack a dedicated reviewed family/example in the current study. If these types are expected, representative completed reports are needed.

| Missing or unconfirmed type | Current evidence | Material requested if in scope |
|---|---|---|
| `실손` / indemnity medical-expense report | No summary or complete-report signal; the earlier authorized corpus inventory also recorded no real case. | At least two representative full reports, ideally from different producers or carriers. |
| Standalone accidental-death benefit report | Death-related terms occur in complete policy discussions but disappear from the summary-scope scan, so no primary death assignment is confirmed. | A completed death-benefit report and its reviewed category label. |
| Standalone surgery-benefit report | One whole-report signal, none in summary scope. | A report whose assignment is specifically a surgery benefit rather than a rider discussed inside a broader diagnosis report. |
| Uninsured-motorist automobile report | No signal. | A completed first- or third-party uninsured-motorist report. |
| Automobile property-damage report | No signal. | A completed `대물` report if property damage is intended scope. |
| Product/manufacturer liability | No signal. | A completed product-liability report. |
| Professional liability other than the observed medical-malpractice variant | No signal. | A completed professional-liability report. |
| Sports/leisure liability | No signal. | A completed sports, leisure, or facility-specific report if intended scope. |
| Property/fire loss-adjustment report | Three whole-report terminology signals but none in summary scope; no primary property/fire assignment or report family is confirmed. | A representative property/fire report set; this likely requires a new top-level family rather than a subtype. |

### Corpus acquisition priority

1. `실손`, because it is already accepted by the live case taxonomy but has no real report or renderable template.
2. Standalone accidental-death and surgery-benefit reports, to determine whether `personal_accident_benefit` and `disease_benefit` need explicit loss subtypes.
3. Additional automobile self-injury and portal-style reports, because each current layout rests on one example.
4. Product, professional, sports/leisure, and property/fire reports only if those lines of business are intended for the harness scope.
5. Reports from another producer, across any existing high-volume family, to test whether the current rules are house style or general structure.

## Closure rule

This quality ledger is complete for the study only when:

1. every observed report type has a documented disposition: dedicated family, explicit subtype, presentation mode, or `other_review_required`;
2. the evidence count and assurance limit for each type are visible;
3. missing expected types are listed plainly for corpus acquisition;
4. the current metrics check passes; and
5. no version-controlled document overstates protected-fixture, OCR, reviewer-independence, or industry-wide assurance.
