# LLM authoring rules for expert-like Korean loss-adjustment reports

## 1. Purpose

Use these rules to create the structured content for a Korean `손해사정서` or `보험금사정서`. The output must validate against `loss-adjustment-report.schema.json` before it is rendered into a narrative document.

These rules are derived from a strongly template-driven corpus dominated by one report producer. Use them as the operational contract for this corpus, not as proof of a universal Korean industry format or current legal/medical correctness.

These rules reproduce the corpus's professional organization, tone, and reasoning discipline without authorizing the model to make professional decisions.

## 2. Instruction priority

Apply requirements in this order:

1. Evidence and privacy guardrails
2. Medical, legal, and calculation review gates
3. The actual policy, contract, law, and source documents for the case
4. `loss-adjustment-report.schema.json`
5. These authoring rules
6. Corpus style conventions

If a corpus convention conflicts with a guardrail, the guardrail wins.

## 3. Never proceed as if evidence exists when it does not

Before drafting:

1. Build `evidence_registry` from the admitted case sources.
2. Assign stable `E#` identifiers.
3. Record a precise locator for every source.
4. Mark missing or unavailable sources.
5. Do not use a final historical loss-adjustment report as drafting input when it is an evaluation answer key.
6. Do not invent policy text, legal provisions, medical findings, attachment numbers, dates, rates, amounts, or signatures.

Every material factual, analytical, and final statement must have at least one `evidence_ref`.

## 4. Choose the document family from the claim mechanism

Set exactly one `document_profile.family` and its matching `claim_mechanism`. The mechanism distinguishes, for example, insured liability from mutual/cooperative liability even when both use the `liability_damages` family.

Set `case_reference.event_type` consistently with that family:

- `automobile_compensation`, `automobile_self_injury`, `personal_accident_benefit`: `accident` or `mixed`
- `disease_benefit`: `disease` or `mixed`
- `liability_damages`: `accident`, `mixed`, or `other`
- `other_review_required`: `mixed` or `other`

### `automobile_compensation`

Use when calculating third-party automobile bodily-injury compensation under automobile liability/compensation rules.

Set `claim_mechanism` to `statutory_or_policy_auto_compensation`.

Required issue:

- comparative negligence

Required calculation:

- total damages

Usually use `mode: compact` when the organization requires a two- or three-page compensation opinion; use `full` when a complete submission package is required.

### `automobile_self_injury`

Use when the insured claims under first-party automobile self-injury or a similar automobile coverage.

Set `claim_mechanism` to `automobile_policy_benefit`.

Required issues:

- coverage
- disability, when the benefit depends on a disability grade

Set `case_reference.disability_benefit_claimed` explicitly. A value of `true` requires a disability issue; `false` must not be used to suppress a materially claimed disability benefit.

Required calculation:

- benefit amount

Do not substitute third-party tort damages logic for policy benefit logic.

### `personal_accident_benefit`

Use for personal accident/injury benefits under a policy or rider.

Set `claim_mechanism` to `personal_accident_policy_benefit`.

Required issue:

- coverage

Required calculation:

- benefit amount

Add disability, causation, and exclusion issues when material.

### `liability_damages`

Use when an insured's liability to a third party and the amount of damages must be assessed.

Set `claim_mechanism` to `insured_liability` or `mutual_or_cooperative_liability` according to the actual indemnity system.

Required issues:

- liability
- comparative negligence

Required calculation:

- total damages

Add income basis, disability, causation, and damages issues as applicable.

### `disease_benefit`

Use for diagnosis-, procedure-, or disease-triggered policy benefits.

Set `claim_mechanism` to `disease_policy_benefit`.

Required issues:

- coverage
- diagnosis-definition match

Required calculation:

- benefit amount

Medical review is mandatory whenever policy-definition matching requires specialized interpretation or the evidence is incomplete.

### `other_review_required`

Use only when no defined family fits. It must remain `review_required`; the model-authored schema cannot represent approval for this or any other family.

Set `claim_mechanism` to `other_review_required`.

## 5. Select full or compact mode

### Full mode

Include all twelve components in exactly this order:

1. cover
2. submission letter
3. inner cover
4. summary
5. assignment and contract
6. facts
7. governing basis
8. analysis
9. calculations
10. conclusion
11. evidence index
12. signature

A component may be `not_applicable` only with a rationale. Do not silently omit it from structured content.

### Compact mode

Preserve the same reasoning but collapse the presentation:

1. amount/opinion summary
2. assignment/parties
3. accident, diagnosis, treatment, current condition
4. governing basis and negligence/coverage
5. visible calculation
6. conclusion and signature

Compact mode may reduce front matter; it may not remove evidence, rule, application, calculation, or review requirements.

## 6. Build an issue map before writing prose

For every material question create one `reasoning_issues` entry.

Required internal sequence:

1. `question`
2. `facts`
3. `rules`
4. `application_steps`
5. `counterevidence`
6. `alternative_interpretations`
7. `unresolved_items`
8. `finding`
9. `disposition`

Do not write the conclusion first and backfill support.

An empty counterevidence or alternatives array is a positive assertion that the admitted evidence was checked and none was found. Do not leave either field empty merely to simplify the preferred conclusion.

### Fact rule

A fact is a direct statement from an admitted source. Use `support_type: direct`.

Examples:

- a diagnosis is written in a diagnosis certificate
- a test result appears in a medical record
- a coverage amount appears in the contract schedule
- a treatment payment appears in a receipt

### Derived rule

A derived statement follows by transparent comparison or arithmetic. Use `support_type: derived` and cite every input.

Examples:

- the event date falls within the policy period
- the rider amount multiplied by a supported rate equals the benefit
- the listed treatment dates total a specified period

### Professional-judgment rule

Use `support_type: professional_judgment` when interpretation is required. Set `human_review_required: true` unless an authorized reviewer has approved the issue.

Examples:

- diagnosis satisfies a specialized policy definition
- accident caused a residual impairment
- a disability percentage is appropriate
- a negligence percentage is appropriate
- a medical exclusion applies

## 7. Draft each issue as fact -> rule -> application -> counterevidence -> finding

### 7.1 Fact

State only what the evidence establishes. Include dates, diagnoses, rates, periods, and document locators where material.

Preferred:

- `진단서에는 ...로 기재되어 있음 [E3].`
- `가입내역상 해당 담보의 가입금액은 ...임 [E2].`

Avoid:

- `자료상 그러한 것으로 보임` without identifying the source
- converting a symptom into a diagnosis
- converting chronological sequence into causation

### 7.2 Rule

Identify the exact controlling source:

- law and article
- policy/rider and clause
- definition/table
- precedent or calculation standard
- medical criteria incorporated by the contract

Do not cite a rule from memory if the authoritative text is not admitted as evidence.

### 7.3 Application

Compare facts to each rule element. Do not merely repeat both.

Good application answers:

- Which event element is met?
- Who made the diagnosis?
- Which test/method was used?
- Was the diagnosis during the covered period?
- Which exclusion was searched and what evidence bears on it?
- Why does a disability item match?
- Why is a wage basis or coefficient applicable?

### 7.4 Finding

Use one of:

- supported
- not supported
- partially supported
- undetermined
- human review required

Use restrained Korean:

- `현 자료 기준 ...에 해당하는 것으로 검토됨.`
- `해당 여부를 확정하기 위해 ... 확인이 필요함.`
- `확인 가능한 자료만으로는 ...를 단정하기 어려움.`
- `전문가 검토 전 판단을 유보함.`

Definitive phrases such as `지급책임이 발생함`, `면책사유에 해당하지 않음`, or `타당하다고 판단됨` are allowed only after all supporting elements and gates pass.

## 8. Family-specific reasoning

### 8.1 Automobile compensation

Address, when applicable:

1. accident mechanism and parties
2. injury/treatment/current status
3. statutory or policy compensation basis
4. comparative negligence
5. income basis
6. disability/labor-capacity loss
7. consolation damages
8. treatment/future treatment/nursing/other damages
9. total and rounding

Do not leave `과실의 평가` blank. If no supported percentage can be selected, mark the issue unresolved and block finalization.

### 8.2 Automobile self-injury

Address:

1. insured and covered vehicle status
2. qualifying automobile accident
3. covered injury or disability grade
4. non-covered loss/exclusion review
5. injury/disability benefit calculation

Use policy-benefit calculations, not tort damages categories, unless the policy explicitly adopts them.

### 8.3 Personal accident benefit

Address:

1. insured event and coverage period
2. accident definition and causation
3. applicable rider
4. disability/benefit trigger
5. exclusions
6. rider-by-rider calculation

For each rider:

`insured amount x applicable contractual rate = rider benefit`

Do not reuse one rider's rate for another rider without a source.

### 8.4 Liability damages

Address:

1. tort/contractual liability
2. insured status and insurer responsibility
3. direct-claim basis where relevant
4. comparative negligence
5. injury and disability
6. occupation and supported income basis
7. worklife and present-value coefficient
8. active loss
9. passive loss
10. non-pecuniary loss
11. deductions and total
12. litigation/medical variability

Keep active loss, passive loss, and consolation damages separate before totaling.

### 8.5 Disease benefit

For each claimed rider compare:

1. policy definition
2. authorized diagnostic professional
3. required test/pathology/procedure
4. diagnosis date and coverage period
5. disease classification, when contractually relevant
6. waiting/reduction period
7. exclusions
8. medical record

A diagnosis label or code alone is insufficient if the policy requires additional diagnostic conditions.

## 9. Calculation rules

For every `calculations` item:

1. Choose a typed category.
2. Select the executable operation: `identity`, `sum`, `subtract`, `multiply`, or `divide`.
3. List every numeric input separately as an exact decimal string, in operation order.
4. Give each input a unit.
5. Cite evidence for every input.
6. Show a whole-won integer result or `null` when the result cannot yet be computed.
7. Encode rounding as `mode` (`none`, `truncate`, or `half_up`) plus a positive whole-won `unit`.
8. Let the custom validator recompute the operation with exact rational arithmetic; a model-authored assertion cannot substitute for recomputation.
9. Do not emit a duplicate free-form formula. The typed operation, ordered inputs, and rounding rule are the single source of truth; a renderer may derive a display formula from them.
10. Use `provisional` or `human_review_required` when any input is provisional. `complete` requires a non-null result.

### Required reconciliation

Before approval:

- summary amount = calculation total
- calculation total = final assessment amount
- numeric amount = Korean financial wording
- subtotals sum to total
- all rates use the correct base
- fault and other deductions are applied exactly once
- present-value coefficient uses the supported period/method

Never hide an unsupported rate or assumption outside the typed inputs.

For `payable` and `partially_payable`, every relevant typed calculation must be complete and the final KRW amount must reconcile with the complete typed net calculation total. For `not_payable`, the final amount is zero, but a positive gross damages or policy-benefit calculation may remain when a separately evidenced coverage, exclusion, liability, or other issue explains why none of that gross amount is payable. The reasoning chain must make that gross-to-net distinction explicit, and `denial_basis_issue_refs` must identify at least one existing reasoning issue that supplies the denying basis.

## 10. Evidence and citation rules

1. Every `evidence_ref` must resolve to one `evidence_registry` entry.
2. Evidence IDs must be unique.
3. Issue, calculation, and section statements must cite their actual sources.
4. Rules must cite law/policy/standard evidence, not a downstream summary.
5. An attachment index must list the evidence actually used.
6. A missing source cannot be cited as if available.
7. Do not cite the report itself as support for its conclusion.
8. Preserve source locators through rendering as `[E#]` tags or attachment pointers.

## 11. Tone and Korean style

### Submission letter

Use polite formal prose:

- `-습니다`
- `-바랍니다`

### Analytical body

Use concise report prose:

- facts: `-함`, `-받음`, `-기재되어 있음`
- rules: `-라고 규정하고 있음`, `-에 의하면`
- application: `-에 해당함`, `-을 적용함`
- supported opinion: `-로 판단됨`
- uncertainty: `-확인이 필요함`, `-판단을 유보함`

### Structure

Use visible hierarchy:

- Roman numerals for major full-form sections
- Arabic numbers for issues
- `가./나./다.` for subissues
- numbered calculation lines
- `소결` after multi-step responsibility analysis

### Avoid

- conversational filler
- emotional or advocacy language
- unexplained adjectives
- rhetorical questions
- repeated conclusion without additional support
- certainty stronger than the evidence
- invented professional signatures or seals

## 12. Summary and conclusion rules

Draft the summary last.

The summary must contain only information already established in the body:

- governing basis
- amount
- key issue finding
- open review or reservation

The conclusion must state:

1. outcome
2. amount or reason amount is undetermined
3. shortest sufficient reasoning summary
4. evidence basis
5. unresolved conditions
6. reservation

Do not remove uncertainty from the summary merely to sound authoritative.

## 13. Authoring preflight gates

Set and enforce all five model-authored preflight gates. A `passed` value means the draft's stated evidence and typed checks satisfy this authoring contract; it is not authenticated proof that a human, medical professional, or legal professional completed a review.

### Evidence

Pass only when every material statement and calculation input has an admitted source.

### Calculation

Pass only when every typed operation has been independently recomputed and reconciled.

### Medical

Pass only after required medical appropriateness, causation, diagnosis-definition, or disability review is complete. Otherwise use `review_required` or `blocked`.

### Legal

Pass only after required liability, exclusion, policy interpretation, negligence, limitation, and precedent review is complete. Otherwise use `review_required` or `blocked`.

### Finalization

An LLM-authored document must set finalization to `draft` or `review_required`. `approved` and embedded approval records are deliberately absent from the authoring schema. The model must never invent a reviewer, approval identifier, timestamp, signature, approval record, or approval source.

Final release belongs to a separate, authenticated, human-controlled workflow. Its attestation must be bound to the exact report artifact outside this model-authored JSON. That workflow must independently reject release when any of these remain:

- `human_review_required: true`
- issue disposition is `human_review_required`
- calculation status is not `complete`
- any gate is `review_required` or `blocked`

The custom authoring validator rejects any attempt to add `approved` or an embedded approval record. Passing this validator therefore means only “valid gated draft,” never “human-approved” or “released.”

## 14. Rendering map

Render structured fields into this order.

The canonical schema component names, in order, are: `cover`, `submission_letter`, `inner_cover`, `summary`, `assignment_contract`, `facts`, `governing_basis`, `analysis`, `calculations`, `conclusion`, `evidence_index`, `signature`. Compact reports may mark inapplicable components accordingly but must preserve the relative order of those retained.

### Full benefit report

1. cover
2. submission letter
3. inner cover
4. `I. 사정 요약`
5. `II. 위임 및 보험계약 사항`
6. `III. 보험사고 및 치료 사실`
7. `IV. 보험금 지급책임`
8. `V. 보험금 사정`
9. `VI. 사정 결과 및 의견`
10. `VII. 증빙자료`
11. signature

Exact Roman-numeral positions may shift when the house template separates analysis from calculation; the relative logic must not.

### Full liability report

1. cover/submission/inner cover
2. summary
3. assignment/parties/contract
4. accident and loss
5. law and liability
6. damages basis
7. damages calculation
8. opinion and reservation
9. evidence index/signature

### Compact automobile report

1. amount/opinion summary
2. assignment and parties
3. accident/treatment/current condition
4. negligence and governing basis
5. calculation
6. conclusion/signature

## 15. Validation

Validate JSON shape and cross-field invariants with:

```text
python tools/validate_loss_adjustment_report.py path/to/report.json
```

A valid example is:

```text
loss-adjustment-format-study/analysis/examples/example-disease-benefit.json
```

Validation success is necessary but not sufficient. Human medical/legal/financial approval is still required when the case or open gates require it.

## 16. Final self-check

Before delivery, answer all items:

- Correct family and mode selected?
- All sources admitted and pseudonymized?
- Every material statement cited?
- Every issue follows fact -> rule -> application -> finding?
- Counterevidence, alternatives, and unresolved items represented explicitly?
- Exclusions and alternative explanations addressed?
- Every calculation input sourced and unit-labeled?
- Arithmetic independently recomputed?
- Summary, body, and final amount identical?
- Uncertainty preserved?
- Open medical/legal issues block approval?
- No embedded approval record or `approved` finalization present?
- Evidence index complete?
- No invented signature, policy text, law, medical finding, or amount?
- Schema and custom validator pass?

If any answer is no, do not finalize.
