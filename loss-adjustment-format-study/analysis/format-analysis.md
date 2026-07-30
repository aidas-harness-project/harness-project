# Loss-adjustment report corpus: format and reasoning analysis

## 1. Scope and method

This study covers every PDF recursively found under `sources/` at the time of the scan.

The corpus is strongly template-driven and appears to be dominated by one report producer. Its recurring forms support an operational authoring model for this corpus, but they must not be treated as representative of every Korean loss-adjustment firm or as a universal industry standard.

- Source PDFs: 95
- Source pages: 2,883
- Source bytes: 227,287,372
- Documents containing a reviewed loss-adjustment report: 94
- Documents without one: 1
- Extracted report pages: 864
- OCR: Korean and English (`kor+eng`), with embedded text used only where complete enough
- Boundary rule: include the report cover, submission letter, inner cover, substantive report, terminal evidence list/signature, and a branded closing divider when it separates the report from attachments; exclude the actual attachments
- Verification: all 95 classifications and ranges are marked `verified`; the study validator checks review hashes, live source-inventory closure, source-bound OCR caches, exact reviewed text slices, and rendered equality for every selected source/output page

The authoritative inventory and page decisions are:

- `../manifest.json`
- `../boundary-review.json`
- `report-index.csv`
- `corpus-metrics.json`

OCR-derived phrase frequencies are lower bounds. OCR errors can hide a phrase, but they do not change the reviewed page ranges or PDF cuts.

The manifest retains the detector's pre-review confidence labels (70 high, 10 medium, 15 low) for auditability. Those labels describe the automated proposal, not the final boundary status; all 95 final decisions have a separate `review_status: verified` gate. A second OCR/text-only review independently assessed every document (89 high confidence and 6 medium confidence) and matched all applied presence/range decisions with zero discrepancies.

## 2. Corpus families

| Corpus group | Sources | Reports | Report pages | Typical length | Dominant form |
|---|---:|---:|---:|---:|---|
| POC mixed | 9 | 8 | 82 | median 11, range 2-13 | Mixed full form plus one portal-style short form |
| Automobile/TA | 16 | 16 | 49 | median 3, range 2-10 | Compact compensation report; one full self-injury variant |
| Personal accident benefit | 40 | 40 | 378 | median 9, range 9-11 | Full 보험금사정서 |
| Liability damages | 22 | 22 | 270 | median 12, range 10-14 | Full 손해사정서 with damages model |
| Disease benefit | 8 | 8 | 85 | median 10.5, range 9-13 | Full 보험금사정서 with diagnosis-definition analysis |

These directory groups are useful for corpus description, but an authoring model must select a report family from the claim's legal and contractual mechanism, not from a source folder name.

## 3. The common document architecture

The full reports repeatedly use the following outer sequence:

1. Cover
2. Submission letter
3. Inner cover or branded title page
4. Adjustment summary
5. Assignment, parties, and contract
6. Accident/claim and treatment facts
7. Governing law, policy wording, and responsibility
8. Issue-specific application and calculation
9. Adjustment result and opinion
10. Reservation/variability language
11. Evidence list and responsible adjuster signature

Representative complete sequences appear in:

- Personal accident benefit: DOC_026, source pages 1-9
- Automobile self-injury: DOC_020, source pages 1-10
- Liability damages: DOC_066, source pages 1-13
- Disease benefit: DOC_088, source pages 1-9

The compact automobile form preserves the same logical sequence but collapses the front matter and body into two or three pages. DOC_019 is a clean example:

- Page 1: amount/opinion summary, assignment, party details, accident facts, diagnosis
- Page 2: treatment/current status, negligence, governing rule, damages calculation
- Page 3: conclusion, amount in numerals and Korean financial wording, signature/brand

DOC_002 is a distinct portal-style short form at source pages 4-5. It uses labeled fields for assignment, contract/accident information, investigation basis, applicable rules, amount, payment responsibility, and opinion rather than a long Roman-numeral narrative.

## 4. Component functions

### 4.1 Cover and submission package

The cover identifies the document as `손해사정서` or `보험금사정서`. The submission letter is not analysis; it establishes the transmittal context and cites the regulatory basis for submission, correction, and processing. Full-form reports commonly use formal recipient language such as:

- `귀사의 무궁한 발전을 기원합니다.`
- `손해사정을 완료하여 ... 사정서를 작성하여 ... 제출하오니 ... 처리하여 주시기 바랍니다.`

The submission letter uses polite formal endings (`-습니다`, `-바랍니다`). The analytical body uses concise report endings (`-함`, `-없음`, `-판단됨`). These registers should not be mixed.

### 4.2 Adjustment summary

The summary is an executive result block. It normally identifies:

- governing basis
- adjusted amount
- one-paragraph opinion
- responsible adjuster

The summary is intentionally redundant with the final result. Its function is navigational: a reviewer can understand the proposed outcome before reading the evidence and analysis. It must not introduce a number or conclusion that differs from the detailed body.

Examples:

- DOC_026 source page 4: `사정 근거`, `사정 금액`, `사정 의견`
- DOC_066 source page 4: damages summary and professional opinion
- DOC_088 source page 4: policy/medical basis and benefit amount
- DOC_019 source page 1: compact `보험금 사정 요약`

### 4.3 Assignment, parties, and contract

This component establishes authority and scope before merits analysis. Common fields are:

- assignment date and scope
- claimant/insured/beneficiary role
- delegating party
- insurer and contract period
- covered vehicle or insured interest where relevant
- adjuster identity and registration role

The report should state roles, not merely list names. The contract section must identify the exact policy/rider or liability basis later applied.

### 4.4 Facts and evidence

Facts are separated into stable subgroups:

- event date, place, and mechanism
- accident or claim narrative
- diagnoses and codes
- medical institutions and treatment timeline
- operations/tests
- current condition or residual impairment
- income/occupation facts where damages require them

Good reports distinguish source facts from professional findings. They repeatedly point to attachments, for example `[별첨 제n호] ... 참조`. The attachment pointer is part of the reasoning trace: it lets a reviewer move from the assertion to the supporting document.

### 4.5 Governing basis

The report quotes or paraphrases the controlling rule before applying it. The source differs by family:

- policy and special-rider wording for benefit claims
- automobile policy and `자동차손해배상 보장법` for automobile compensation
- Civil Act, Commercial Act, policy wording, and direct-claim provisions for liability
- medical/diagnostic definitions incorporated into a disease rider
- disability tables or assessment standards for impairment

The governing-basis section commonly follows this local pattern:

1. State the payment or liability rule.
2. Identify the covered person/event or legally responsible person.
3. Check exclusions or non-covered loss.
4. State a short `소결` before moving to amount.

### 4.6 Application and findings

The corpus's expert-like quality comes mainly from its issue-by-issue application, not its typography. The recurring reasoning unit is:

`documented fact -> governing rule -> comparison/application -> issue finding -> effect on amount`

Typical issue findings include:

- event qualifies as an insured accident
- diagnosis satisfies or does not yet satisfy a policy definition
- a disability item/rate applies
- liability exists under the applicable law/policy
- comparative negligence percentage applies
- documented income is sufficient or a fallback wage basis is used
- an exclusion has or has not been established

A conclusion without this bridge reads like an assertion rather than a professional adjustment.

### 4.7 Calculation

Calculations are shown rather than merely described. A high-quality calculation block exposes:

- category
- source value
- unit
- formula
- intermediate result
- adjustment factor
- rounding/truncation rule
- final category subtotal
- final total

The same result is often shown three ways:

1. Formula with numerals
2. Total in comma-separated Korean won
3. Formal financial wording such as `一金 ... 整 (₩...)`

Those representations must reconcile exactly.

### 4.8 Result, opinion, and reservation

The final section normally includes:

- adjusted amount or benefit
- concise basis statement
- professional opinion
- unresolved contingencies or future-change reservation
- responsible adjuster signature

Common corpus phrases include:

- `타당하다고 판단됨/판단됩니다.`
- `적정하다고 판단됨/판단됩니다.`
- `위 금액을 ... 지급하여야 할 ... 금액으로 사정함.`
- `향후 ... 예기치 못한 추가 손해가 발생할 경우 ... 사정을 유보함.`
- `판단에 따라 위 금액은 달라질 수 있음.`

These are observed source conventions, not unconditional generation instructions. A production model must not use a definitive payment, exclusion, causation, diagnosis, or liability statement unless the required evidence and review gates support it.

### 4.9 Evidence index

The report ends before the attachments, on a page that indexes them. Typical entries are:

- diagnosis certificate
- disability certificate
- medical-record copy
- hospitalization/discharge confirmation
- medical-cost details and receipts
- accident statement
- policy/contract schedule
- identity/bank copy
- authorization
- income or consolation-damages basis
- expert/medical opinion

The evidence index is not decorative. Every cited attachment should resolve to an evidence-registry entry, and every listed item should be available or explicitly marked missing.

## 5. Family-specific hierarchies

### 5.1 Automobile compensation: compact form

Typical hierarchy:

1. `보험금 사정 요약`
   - adjusted amount
   - one-paragraph opinion
2. `사건위임 및 가해사실 확인`
   - assignment
   - claimant/party details
3. `사고발생의 조사 및 확인한 사실`
   - accident
   - diagnosis, treatment, current state
4. `과실의 평가`
5. `보상금의 사정`
   - legal basis
   - consolation damages
   - lost income
   - lost earning capacity
   - other damages
   - treatment/future treatment/nursing where applicable
6. `결론`

The compact form uses short paragraphs and visible formulas. DOC_019 source pages 1-3 demonstrates the full compact sequence. Fifteen TA reports use this compact form: six are two pages and nine are three pages. OCR metrics found quantification wording in all 16 TA reports and an explicit final-opinion/conclusion signal in at least 14. None of DOC_001-DOC_009 is an additional automobile report; superficially similar items there are liability or fixed-benefit forms.

One TA report is a full automobile self-injury form rather than compact third-party compensation. DOC_020 source pages 1-10 analyzes policy status, insured status, accident qualification, non-covered loss, injury/disability grades, and then benefit amount. This should be modeled as `automobile_self_injury`, not forced into the compact compensation template.

### 5.2 Personal accident benefit

Typical hierarchy:

1. Cover/submission/inner cover
2. `사정 요약`
3. Assignment and contract
4. Accident and treatment facts
5. Payment responsibility
   - policy payment event
   - accident qualification/causation
   - disability finding
   - exclusion review
   - `소결`
6. `보험금 사정`
   - rider/coverage
   - insured amount
   - disability/payment rate
   - multiplication and rider total
7. `사정 결과 및 의견`
8. Reservation
9. `증빙자료`

DOC_026 source pages 4-9 is representative. Across the 40-report family, 24 packets are nine pages, 14 are ten pages, and two are eleven pages. Longer packets expand policy analysis or multi-rider calculation rather than changing the outer sequence. The benefit logic is usually:

`insured amount x contractually applicable rate = benefit`

When several riders respond, calculate each rider separately and then total them. Do not apply a disability rate to a rider unless that rider's wording uses that rate.

OCR lower bounds show governing-law/policy wording and quantification in all 40 reports, an evidence-basis phrase in all 40, and explicit reservation wording in at least 35.

### 5.3 Liability damages

Typical hierarchy:

1. Cover/submission/inner cover
2. Summary
3. Assignment, parties, and liability contract
4. Accident and medical facts
5. Governing law/policy and liability
   - tort or contractual responsibility
   - insured status
   - insurer responsibility/direct claim
6. Damages basis
   - comparative negligence
   - disability/labor-capacity loss
   - occupation and income
   - worklife and intermediate-interest coefficient
   - consolation-damages standard
   - treatment and future treatment
7. Damages calculation
   - consolation damages
   - active loss
   - passive loss
   - deductions/fault
   - total and rounding
8. Opinion and litigation/medical variability
9. Reservation
10. Evidence index/signature

DOC_066 source pages 4-13 is representative. It demonstrates the full chain from facts and liability through negligence, disability, income, Hoffmann coefficient, category calculations, total, opinion, and reservation.

The report should distinguish:

- active loss: treatment and other incurred/future expenses
- passive loss: lost income and lost earning capacity
- non-pecuniary loss: consolation damages
- responsibility adjustment: comparative negligence and other legally supported deductions

OCR lower bounds show quantification and reservation language in all 22 liability reports. Twenty explicitly retained text that the amount may change after later adjudication or review.

Material variants preserve the same reasoning skeleton. DOC_076 and DOC_083 adapt insurer/insurance-benefit terminology to a mutual or cooperative setting. DOC_077 expands the liability and damages analysis for medical malpractice. DOC_081 separately deducts workers' compensation benefits from overlapping loss periods. A schema must therefore type the liability mechanism and each deduction rather than relying on one house phrase.

### 5.4 Disease benefit

Typical hierarchy:

1. Cover/submission/inner cover
2. Summary
3. Assignment and contract
4. diagnosis, tests, treatment, and current status
5. Payment responsibility
   - exact rider definition
   - diagnostic authority/method/date requirements
   - disease classification requirement
   - waiting/reduction period
   - exclusions
   - definition-to-record comparison
6. Benefit calculation by rider
7. Result and medical uncertainty/reservation
8. Evidence index/signature

DOC_088 source pages 4-9 is representative. The central reasoning is not merely `diagnosis exists`; it is:

`policy definition and diagnostic requirements <-> documented diagnosis, test, physician, date, and classification`

A diagnosis code alone is not sufficient when the rider requires a particular test, specialist, pathology, or clinical definition. Medical appropriateness must remain a human-review gate when evidence is incomplete or interpretation is specialized.

DOC_003-DOC_006 are especially instructive counterexamples to generic disease logic: four carriers' differently worded cerebrovascular riders are applied to substantially the same medical presentation. The correct outcome therefore depends on the exact carrier-specific definition, not the diagnosis label alone. DOC_089 also demonstrates that one packet can contain several independently triggered benefits with diagnosis, surgery, congenital-condition, treatment-cost, first-event, annual/lifetime-limit, and reduction-period conditions. Each rider must be modeled and resolved separately.

OCR lower bounds show governing-policy/payment-responsibility language, quantification, and reservation in all eight disease reports.

### 5.5 Portal-style short report

DOC_002 source pages 4-5 is a structurally different insurer/portal output. It compresses the report into labeled fields:

- assignment information
- contract information
- accident/claim information
- investigation data and findings
- applicable law/policy
- adjusted amount
- payment responsibility
- opinion

It is useful as a field inventory but should not replace the full expert reasoning model. If used, the same evidence, rule, application, calculation, and review requirements still apply.

## 6. Tone and drafting rules observed in the corpus

### 6.1 Register

Use a restrained professional register:

- Transmittal: polite formal (`-습니다`, `-바랍니다`).
- Body facts: report style (`-함`, `-받음`, `-확인됨`, `-중임`).
- Rule statement: `-라고 규정하고 있음`, `-에 의하면`.
- Application: `-에 해당함/해당하지 않음`, `-을 적용함`.
- Professional opinion: `-로 판단됨`, with an explicit basis.
- Reservation: `-할 경우 ... 유보함`, `-에 따라 달라질 수 있음`.

Avoid conversational explanation, rhetorical questions, promotional language, emotional characterization, and unexplained certainty.

### 6.2 Paragraph logic

A paragraph generally does one job:

- source fact
- governing rule
- application
- finding
- calculation
- limitation

Do not combine several unsupported conclusions into one long sentence. The corpus often uses `가./나./다.`, numbered items, `소결`, and `- 다음 -` to keep the reasoning auditable.

### 6.3 Precision

Preferred:

- dates, diagnoses, rates, amounts, periods, and document references
- explicit conditionality
- separate observed fact and opinion
- named calculation inputs and units

Avoid:

- vague `관련 자료에 따르면` without a locator
- `명백함`, `확실함`, or `당연함` without direct support
- medical causation stated as fact when it is an adjuster inference
- invented policy text, legal provisions, attachment numbers, rates, or amounts

## 7. The reasoning grammar to preserve

For every material issue, draft in this order:

1. **Question**: What must be decided?
2. **Facts**: What does each cited source directly establish?
3. **Rule**: Which law, policy provision, definition, table, or standard controls?
4. **Application**: How do the facts satisfy or fail each rule element?
5. **Counteranalysis**: What counterevidence, alternative interpretation, or missing item could change the result?
6. **Finding**: What is supported, not supported, partial, or unresolved?
7. **Effect**: What does that finding do to responsibility or amount?
8. **Review/uncertainty**: What remains for a medical, legal, or financial reviewer?

This is the core LLM content model implemented in `loss-adjustment-report.schema.json` as `reasoning_issues`.

## 8. Calculation patterns

### Benefit claims

- Per-rider insured amount
- Contractual payment/disability rate
- Waiting or reduction factor where the policy requires it
- Per-rider result
- Total

### Automobile compensation

- consolation damages
- lost income during treatment
- lost earning capacity
- treatment/future treatment
- nursing and other damages
- comparative negligence
- total

### Liability damages

- consolation damages, often adjusted for responsibility
- active loss
- passive loss
- present-value/intermediate-interest coefficient
- comparative negligence/deductions
- category subtotals and final total
- explicit truncation/rounding

Every numeric input must point to evidence or a cited standard. Source-visible formulas may be rendered from typed operations, ordered inputs, and rounding rules, but must not become a competing authoring input or hide assumptions in prose.

## 9. Corpus conventions that require safety overrides

The source corpus contains practitioner conclusions such as `지급책임이 발생함`, `해당사항 없음`, and `타당하다고 판단됨`. These phrases are appropriate only after their premises have been established.

An LLM must use `검토 필요`, `현 자료 기준`, `확인되지 않음`, `판단 유보`, or `전문가 검토 필요` when:

- a required source is missing
- diagnosis-definition matching is uncertain
- causation is not directly established
- an exclusion search is incomplete
- a rate, wage basis, fault percentage, or coefficient lacks a source
- calculation has not been independently recomputed
- a medical/legal approval gate is open

Imitating the tone is never permission to imitate unsupported certainty.

## 10. Design consequences for an LLM schema

A rendering-only template is insufficient. The generation contract must require:

- document family and mode
- ordered components
- pseudonymized case reference
- evidence registry
- supported statements with evidence references
- issue-by-issue fact/rule/application/finding chains
- counterevidence, alternative interpretations, and unresolved-item lists for every issue
- typed calculations with operation, ordered inputs, units, result, and rounding; any display formula is derived
- final assessment linked to evidence, with explicit denying-issue references for a `not_payable` outcome
- explicit reservations
- model-authored evidence, calculation, medical, legal, and draft/review-required finalization preflight gates, never authenticated review attestations
- no embedded approval record; any external human release attestation must be authenticated and bound to the exact rendered artifact

The accompanying JSON Schema encodes the shape and family requirements. `tools/validate_loss_adjustment_report.py` enforces cross-field conditions that JSON Schema alone cannot express conveniently, including evidence-reference integrity, canonical component order, unique identifiers, deterministic exact-rational recomputation with pre-rounding non-negativity and explicit whole-won rounding, complete relevant calculations for payable outcomes, denying-issue linkage for not-payable outcomes, and net-payable outcome/amount reconciliation. The model-authored contract cannot represent approval; release attestation belongs to a separate authenticated human workflow.

## 11. Limits

- The corpus represents one collected practitioner set and may overrepresent its house style.
- OCR phrase/component counts are reproducible lower-bound regex signals, not independently adjudicated semantic labels, and should not be interpreted as precise linguistic prevalence.
- This study analyzes document construction, not whether every historical conclusion was legally or medically correct.
- A future production template must still follow the repository's evidence, medical-review, uncertainty, and answer-key isolation guardrails.
