---
type: Architecture
title: Loss-Adjustment Pipeline
description: Stage/agent map, taxonomy, and I/O contracts for the loss-adjustment harness. Redesigned from a coworker's rough draft into the shape actually implemented -- see `CHANGELOG.md` for what changed and why.
tags: [pipeline, agent-harness]
---

Two phases, matching the real workflow: claim comes in, insurer responds,
you respond to the insurer. See `harness-guardrails` and
`harness-guardrails-dev` for the hard rules every stage follows regardless
of which one it's in -- this document is the stage map and I/O contracts,
not the rules.

All inter-stage data goes through the DAO (`tools/dao.py`) -- no agent
reads or writes `outputs/`, `data/`, or a ledger/run-state file directly.
File path convention: original documents `data/raw/CASE_XXX/` →
intermediate `data/processed/CASE_XXX/DOC_XXX/` → final contract outputs
`outputs/CASE_XXX/`.

# Phase 1 -- initial claim review

Case intake → document processing (OCR → redaction → segmentation →
per-child classification/redaction → chunking) → policy/claim analysis →
screening report → draft report v1. 10 top-level stages (redesigned down
from an 18-step draft; several old steps turned out to be redundant or
wrongly ordered once P8's cross-validation and the DAO's guardrail hooks
were worked through -- see `CHANGELOG.md`).

Stages are not uniformly agent-run: several are **drivers** (a deterministic
`tools/*.py` the orchestrator invokes directly), some are **agent** dispatches,
and two are **mixed** on the selective lane -- a helper brackets the agent's
judgement on either side. The Mode column says which, and
`.claude/agents/pipeline-orchestrator.md` carries the exact command per stage.

| # | Stage | Mode / Agent | Notes |
|---|---|---|---|
| 1 | Case Intake | (`tools/intake_case.py`) | D2-gated intake: every file gets a `_source_ledger.json` entry; a single rejection blocks the case. **Nobody declares which PDFs are bundles here** -- that is what `propose` reports from the text stage 2 produces, so it cannot be a precondition for producing it. **No OCR and no splitting here** -- intake produces the reviewed file inventory; the split itself is a checkpoint of stage 2, because it now happens between two halves of document processing. Records under the `intake` run-state stage. |
| 2 | Document Processing | **driver** (`run_stage2.py`) | One top-level stage; segmentation lives **inside** it, since processing runs on both sides of the split. Checkpoints: **(a)** bundle OCR (`run_checkpoint1.py --bundle-ocr`) -- OCR only, no classification, because `document_type` is per-document and one label cannot fit a bundle mixing a 진단서, a 검사 판독지 and a 진료비 명세서; **(b)** bundle redaction; **(c)** segmentation over the *redacted* text -- `segment_case.py propose` resolves evidence as processed text → the PDF's embedded layer → vision, so a scan reaches the deterministic path for the first time. Titles are the boundary signal (`...보통약관`/`...특별약관`/`...특약`; medical form names through a 5-line header window), a page the rule cannot settle goes to the LLM tier, and an unusable verdict splits and is flagged. Human range approval gates `split`, which retains the bundle as `superseded_bundle` and hands each child the bundle's pages renumbered from 1 with P8 verdicts intact -- **children are never re-OCR'd**; **(d)** per-child classification; **(e)** per-child redaction via `tools/redact_document.py` (the `tools/redaction.py` Redactor abstraction: the LLM identifies PII spans, substitution is deterministic, a detected leak hard-fails); **(f)** case-wide deterministic chunking. A plain-text source skips (a) for a deterministic embedded-text decode (`cross_validation_mode: deferred_poc`). A whole-document photograph/visual-evidence input may be human-resolved as `non_text_image`: no transcription/classification quote/redaction/chunk is fabricated, and it is recorded as `expert_review_only` plus an explicit `page_chunks.json` exclusion. Each materialized child owns a real `data/raw/.../DOC_XXX.pdf`, so its manifest `document_role` is `physical`; `source_file_name` and `source_page_start`/`source_page_end` retain bundle provenance but do not make it a raw-less processed-text `segment`. `run_checkpoint1.py` refuses exactly one target before any provider or PDF work -- the retained `superseded_bundle`, whose children already own its pages (`blocked_segmentation`, in both modes). An unsplit bundle is not refused: reading it is the point. A provisional type is never trusted downstream. All P8 readers are LLM-vision-backed (openrouter / claude-cli / codex-cli / openai-api), so any reader pair is a documented weak P8 (`single_technology_weak_p8_poc`); a genuinely technology-independent reader (a real OCR engine) is deferred -- see `open-decisions.md` #4. **Run this stage as ONE command**: `tools/run_stage2.py CASE_ID --held-by document-pipeline --run-id RUN_ID --provider claude-cli` performs every checkpoint above and stops at the four gates that are genuinely a human's (a P8 disagreement, boundary approval, a `raw_page_text` classification review, a possible PII leak). It never moves a run-state marker -- `update-run-state`/`finalize-stage` stay with the orchestrator. Do not ask an agent to invoke the checkpoint tools one at a time: that serialization was in a SPEC rather than in code, and the driver is what removes it. Two flags reduce P8 for a timing or plumbing run and are the ORCHESTRATOR's decision, never a stage's: `--on-disagreement assume-reading-a` (dual reads still run and are compared; only the halt is deferred) and `--single-reader` (P8 off, roughly half the calls). `HARNESS_SINGLE_READER=1` makes the latter the default without appearing in the command, so check it before treating output as evaluation-grade. Neither is gated by `finalize-stage`, which is exactly why the scoping call is yours. |
| 3 | Indexing (adapter) | (tool, no agent) | Pass-through by default; swappable for real vector/BM25 indexing later without restructuring anything downstream |
| 4 | Policy Clause Processing | (driver, no agent) | **Clause normalization is retired (2026-08-15)**, so every policy document is `text_only_no_normalization` and the stage has no extraction work. Run `tools/run_policy_preflight.py` first, OUTSIDE the attempt (it may invalidate stale artifacts, and doing that inside a live attempt demoted the stage mid-run on CASE_142), then `tools/run_policy_pipeline_driver.py`, which records the manifest fingerprint, writes `_document_index.json`, and returns a no-op. **Do not dispatch `policy-pipeline`** -- measured on CASE_142 the agent path spent 370.9s across two attempts with zero provider calls and zero output files. It BLOCKS if a normalized policy document somehow exists; that is a real precondition failure to report, not a reason to fall back to the agent. The paragraph below describes what normalization required while it was live, and is retained because a promoted document (`dao.py promote-policy-document --disputed-by`) still owes it. **Normalization was opt-IN.** A document classified `insurance_policy` starts `text_only_no_normalization`: fully OCR'd, redacted, chunked and citable, owing no clause contract. Nothing downstream consumes the normalized buckets -- `claim-analysis` and `denial-response` address a clause by `document_id` + page + verbatim quote, verified byte-for-byte against the processed text, so requiring normalization to CITE a policy document made the expensive obligation universal for no consumer (one 145-page bundle carries 800+ conditions). Promotion to `automated_text_pipeline` is per-document and requires a stated dispute (`dao.py promote-policy-document --disputed-by`), which demotes this stage so it re-finalizes against the new obligation. What the stage still gates on is TEXT: a case with no text-processed policy document cannot finalize, and canonical UID verification covers every citable policy document regardless of normalization. Clause references accordingly accept two addressing forms (UID into a normalized contract, or source-form page+quote); see `.claude/agents/claim-analysis.md` and the `coverage_result`/`requirement_matching_result` schemas. When a document IS normalized, everything below applies to it: boundary inventory → exact source-span accounting → semantic classification → normalization mapping. Every non-whitespace policy-source character is covered or explicitly excluded, but normative article/paragraph/item anchors cannot be hidden inside an exclusion. Boundary↔clause mappings are checked in both directions; unresolved/missing/dangling boundaries block finalization. Immutable source-local `clause_uid`/`condition_uid` values are downstream join keys, while sequential `clause_id` is display-only; cross-insurer canonical matching, if added later, is a separate layer. Clause kinds use dedicated buckets so obligations, definitions, procedures, termination, disputes, and coverage-start rules cannot be mistaken for payout conditions. Meaning validation precedes the lexical support floor: complete operative predicates, negation, and numeric/temporal terms must be evidenced, while composite or uncertain support is review-gated and condition wording is never weakened merely to raise overlap. When a large policy PDF is one physical parent carved into segments (e.g. CASE_030's 240-page 약관: parent `DOC_001` split into per-특약/조항 segments + a 별표 appendix segment), two shapes are exempt from the per-document normalization requirement: a **segmented physical parent** is normalized through its segments, so `policy_parent_coverage_{id}.json` accounts for every logical and immutable-source physical page exactly once. Page ownership must resolve against the segment's actual logical/physical `page_map`; administrative exclusions require page-specific evidence, and unpaged physical exclusions require human provenance. A **reference-table-only segment** is waived from normalized clauses and audit only when its tables and cells have page-local evidence and no unresolved review flag; it still owes a complete boundary inventory. Every other segment owes normalized clauses + boundary inventory + audit. Decision-bearing appendices use immutable source-derived table/row/cell UIDs and normalized clauses link by UID, never by array position or display label. Each normalizing document also has a version-bound audit, but an empty authored findings list cannot conceal deterministic defects: finalization reruns completeness and provenance checks independently. Downstream references resolve only against the current clear audit. |
| 5 | Claim Analysis | (driver, no agent) | `tools/run_claim_analysis.py` runs the 4 public checkpoints in two structured calls: M1 = field extraction + policy-page selection; M2 = coverage ID + case-type classification + requirement matching. It reads/writes only through the DAO, verifies citations before publication, writes driver receipts, and never finalizes the stage. The orchestrator opens/finalizes the attempt and runs medical clearance after canonical medical variables publish. It requires `_document_index.json`, which the policy driver produces. **Two lanes.** When `config/claim_analysis/claim_analysis_routing_v0.1.json` has `behavior_enabled: true`, run `tools/run_claim_analysis_selective.py` instead: reading is demand-driven (each round asks only the unresolved fields which document they need next, batches everyone wanting the same one into a single call, and a field that finds a trusted value drops out -- on CASE_489, 7 of 24 documents were opened). It publishes `claim_analysis_result.json` with `authority: source_document_extraction` and `medical_projection_status: not_configured`, needs no canonical medical revision, treats `_document_index.json` as OPTIONAL (a case with no processed policy yields `policy_links` with status `not_found`, not a blocked stage), leaves the filing fields `unavailable`/`outside_poc_scope` because no stage here produces a filing declaration, and records conflict *candidates* only -- it never writes the P6 ledger. The legacy entry point exits 2 and names it rather than running the old spine. Activation is a recorded policy decision (`activation` needs approver, role, timestamp and scope), not a flag to flip while running. **Do not dispatch `claim-analysis`** -- that agent is retired on both lanes. |
| 6 | Consistency Check | `consistency-check` (selective: driver + agent) | Any cross-document disagreement goes to `_conflict_ledger.json`, not an inline halt -- see harness-guardrails P6. On the selective lane the judgement is bracketed by a deterministic helper: `tools/run_consistency_check.py prepare` builds work items carrying both readings and their evidence and NO verdict field (a prepared answer would decide by suggestion), the agent judges each `confirmed`/`not_material`/`withdrawn` and writes a neutral `professional_summary` on every confirmed one, then `register` verifies the verdicts bind to what was prepared and creates the entries. Every entry is created `pending`; `resolved`/`false_positive`/`deferred_to_report` are human calls. The helper reads `claim_analysis_result.json`, which only the selective lane produces, so it does not apply to a legacy run. |
| 7 | Screening Report | `screening-report` | Conflict-gated -- `finalize-stage` itself refuses while any ledger entry is `pending` (`dao.CONFLICT_GATED_STAGES`, which covers every stage that reasons from the case's facts, not this one alone; `consistency_check` is excluded because it raises them). Consumes `denial-response`'s output whenever an insurer-response document exists (a dependency, not a phase gate). On the selective lane the split is explicit: the agent supplies ONLY its judgement -- `key_issues`, `review_points`, per-conflict severity/placement -- to `screening_report_judgement.json`, and `tools/run_screening_report.py` then assembles all three artifacts, rendering nine sections through `document_assembly.py --template screening_report_selective`. It produces no 진행 가능성/난이도/지급 가능성 and copies a deferred conflict's `professional_summary` verbatim from the ledger. The helper reads the judgement with `allow_missing`, so skipping the agent yields a report of fallback values that still reports success -- confirm the judgement contract exists before assembling. |
| 8 | Draft Report v1 | `draft-report` | Same agent reused for the v2 update in Phase 2 |
| 9 | Critic Pass (v1) | `critic` | Blind -- structurally cannot read ground truth |
| 10 | Human Review v1 / external handoff | human-owned | Completes the local v1 boundary; any future Evaluation remains in the isolated Unit 11 service |

`denial-response` is not numbered here -- it's dependency-triggered, not
phase-gated. It runs whenever a flagged insurer-response document's
processed text (from stage 2) exists, whether that's during Phase 1
(closed cases bundle the insurer notice from the start) or genuinely later.
Its output keeps `decision_type` (`denial`/`reduction`) separate from the
observed `payment_status`; `partial_payment` is not a decision type. Each
materially distinct insurer reason gets its own `reason_id`, three explicit
ground arrays (contractual, medical/factual, calculation), an explicit amount
object, and policy matches whose insurer-citation and clause locations remain
distinguishable. Empty ground/match arrays are valid and preferable to an
unsupported inference.

A decision is scoped to ONE coverage, not to the case: `decided_coverage`
names which, and `accepted_coverages` records what the same response
accepted. An acceptance is neither a denial nor a reduction, so it has no
`decision_type` and cannot be a reason -- but omitting it makes a split
outcome read as a total one (CASE_907: 배상책임 denied, 구내치료비
₩2,000,000 notified in the same letter). Field-level rules live in
`.claude/agents/denial-response.md` and `schemas/denial_reason_result.schema.json`.

## Medical-review checkpoint and downstream boundary

Medical structuring and review are checkpoints and a human gate inside
`claim_analysis`, not a new top-level autonomous stage. Claim analysis derives
medical-variable content after field extraction, then publishes it only after
canonical case-type classification exists. Once a run publishes a
canonical medical-variable revision, the claim-analysis driver and orchestrator must run
`python tools/dao.py check-medical-reviews-clear CASE_ID` before claim analysis can pass
and immediately before every downstream agent dispatch. Missing, malformed, stale, uncovered, or unresolved
medical-review state fails closed.

Authorized in-progress report consumers read only the bounded ledger-derived view:

```bash
python tools/dao.py read-medical-review-outcomes CASE_ID --caller-stage STAGE --run-id RUN_ID
```

`STAGE` is restricted to `screening_report`, `denial_validation`,
`draft_report_v1`, or `draft_report_v2`. The projection pins the canonical medical
revision and preserves response attribution, interpretation, uncertainty, alternatives,
and downstream-adjustment advice. Downstream agents never read the internal medical
ledger directly.

**Capability versus activation:** schemas, canonical storage, lifecycle controls,
localhost API/UI, and synthetic tests are implemented. The shipped medical structuring,
projection, referral, request, role, and operator policies remain disabled and contain
no approved clinical thresholds, real-case scope, named medical actors, or operator
tokens. No real run may claim medical screening until the deferrals in
`docs/medical-appropriateness-screening-deferrals.md` are explicitly resolved.

# Phase 2 -- insurer denial/reduction response

Only 2 new stages -- everything else is Phase 1's agents reused on new
input (redesigned down from a 13-step draft that duplicated intake/OCR/
redaction for the insurer document and never actually assigned an owner
for rebuttal generation).

| # | Stage | Agent | Notes |
|---|---|---|---|
| 1 | Denial Validation | `denial-validation` | 2 internal checkpoints: (a) verify every policy-match ID/location, retrieve evidence, and validate each denial/reduction reason; (b) generate rebuttal points using only verified policy links. Invalid/unverifiable links are review-routed and never silently replaced. Insurer-vs-evidence disagreement is this stage's actual purpose, **not** a P6 conflict -- don't route it through the conflict ledger |
| 2 | Draft Report v2 | `draft-report` | Second checkpoint of the Phase 1 agent |
| — | Critic Pass (v2) | `critic` | Same agent as Phase 1 |
| — | Human Review v2 / external handoff | human-owned | Completes the local v2 boundary; any future Evaluation remains in the isolated Unit 11 service |

# Document-assembly tool

Narrative outputs (`screening_report.md`, `draft_report_v*.md`,
`rebuttal_points.md`) are never hand-written directly by an agent. Screening
and rebuttal inputs use per-section `{content, evidence_references}` with
`{{E}}` placeholders. Draft reports first become a DAO-governed
`loss_adjustment_report_v*.json`; `document_assembly.py
--structured-report-file` validates the full authoring contract, maps its
structured sections through the selected registry template, renders the
Korean narrative, and generates `[E#]` tags plus the `.evidence.json` sidecar
in one pass. See harness-guardrails P1.

The registry contains supported forms for liability damages, disease benefit,
personal-accident disability benefit, and compact third-party automobile
compensation. Automobile self-injury is present only as a provisional form and
always requires professional review. `case_type_result.report_profile` must
match the selected template's family, claim mechanism, mode, and support
status. `실손` and otherwise unsupported/unknown cases use
`other_review_required`, require `template_id: null`, and halt before drafting;
there is no closest-template fallback. Section presence and order are
structurally enforced by `templates/registry.json`. Rebuttal points remain the
one deliberate dynamic exception. See `templates/draft-report.md`, the format
study authoring rules/schema, and `open-decisions.md` #2.

# Taxonomy

## Document types

`insurance_certificate`, `insurance_policy`, `application_form`,
`diagnosis_certificate`, `medical_record`, `imaging_report`, `receipt`,
`insurer_response`, `legal_opinion`, `legal_reference`, `other`.

`insurer_response` / `legal_opinion` / `legal_reference` are the non-medical
codes a liability case turns on, and are confusable in the same way the three
policy forms are — distinguish by **who wrote it and what it decides**. The
insurer's covering letter (협조요청, 부지급 통보) is `insurer_response` even when
it summarizes an opinion; the reasoned answer itself — 법률질의회신서, citing
민법 제750조/제758조 and 대법원 판례, ending in "…판단됩니다" — is
`legal_opinion`, **whichever side commissioned it**, since both parties'
opinions are evidence and the 수신/발신 block is usually masked; published
material a party merely attached (서울중앙지법 위자료 산정기준표, a
노동능력상실률/맥브라이드 표) is `legal_reference`, which disability-rate and
위자료 calculation cite.

Added 2026-08-21. Before that the classifier read these titles correctly at
0.90–0.95 confidence and had no bucket but `other`, so CASE_053's two opposing
법률의견서 (24 pages, one concluding 배상책임 있음 and one 없음) never reached
claim analysis at all.

`insurance_certificate` (증권서류) / `insurance_policy` (보험약관) /
`application_form` (청약서류) are easily confused — all three carry
policy-like language. Distinguish by defining marker: the 약관 is the full
contract rulebook (제N조/지급사유/면책/특별약관 목차); the 증권 is proof of one
concluded contract (계약·증권번호, 보험기간, 보장내용·가입금액, 총보험료); the
청약서 is the signed application that precedes the contract (청약일, 자필서명,
계약전 알릴의무 질문서, 상품설명서). `application_form` was added 2026-07-23
after CASE_030 found a 청약서류 had no type to land in and was absorbed into
`insurance_policy`.

## Case types

Classification retains the legacy `case_type` while using two primary axes:
`coverage_basis` (`배상책임`, `개인보험`, `자동차보험`) and `loss_type`
(`후유장해`, `진단·수술비`, `실손`, `기타`). Claim-analysis checkpoint 3 then
derives `report_profile` (family, claim mechanism, presentation mode, support
status) and selects a compatible `template_id`, or fails closed when no
evidence-backed form exists.

## Denial/reduction reason codes (R-codes)

`decision_type` has exactly two values: `denial` means no payment for the
specific claim coverage/item; `reduction` means payment liability is recognized
but the payable amount is reduced. One case may contain both as separate reason
entries. `payment_status` (`unpaid`, `partially_paid`, `paid`, `unknown`) records
the observed outcome independently; no automatic relationship is enforced until
enough real data exists. R12/R14 remain unclassified pending adjuster review, so
either decision type is schema-valid only with `review_required: true`.

The frequency tier and applicable decision types are loss-adjuster-reviewed
taxonomy metadata received on 2026-07-21 and 2026-07-22 respectively. They are
not source evidence, classification confidence, or permission to override the
insurer's actual wording. `감액` maps to `reduction`; `거절` maps to `denial`.
A `미분류` cell means the adjuster's source table left the classification blank,
not that the code is inapplicable. The machine-readable copy lives beside the
enum in `common_component_output.schema.json`'s `taxonomy_code.x-codebook` and
is kept synchronized with this table by tests.

| Code | Reason | Frequency | Classification |
|---|---|---|---|
| R01 | 기왕증 / 기존 질환 기여도 (pre-existing condition contribution) | 상 | 감액 |
| R02 | 장해율 과다 (disability rate overstated) | 상 | 감액 |
| R03 | 손해액 과다 (damages overstated) | 상 | 감액 |
| R04 | 약관상 지급요건 미충족 (policy conditions not met) | 상 | 거절 |
| R05 | 면책사항 (exclusion clause) | 상 | 거절 |
| R06 | 치료 필요성 부족 (treatment necessity insufficient) | 상 | 감액 |
| R07 | 과잉진료 / 비급여 적정성 (overtreatment / non-covered-item appropriateness) | 중 | 감액 |
| R08 | 서류 부족 (missing documents) | 하 | 거절 |
| R09 | 동일 사유 재청구 (repeat claim, same reason) | 하 | 거절 |
| R10 | 기존장해·동일 부위 장해 공제 (existing disability / same-body-part disability deduction) | 상 | 감액 |
| R11 | 피해자 과실상계 (claimant contributory negligence) | 상 | 감액 |
| R12 | 자기부담금·약정 공제금액 적용 (deductible / contracted deduction) | 하 | 미분류 |
| R13 | 중복보상·실손 비례보상 (duplicate coverage / indemnity proportional payment) | 중 | 감액 |
| R14 | 가입금액·보상한도·일수한도 적용 (insured amount / coverage / day limit) | 하 | 미분류 |
| R15 | 면책기간·감액기간 적용 (exclusion / reduction period) | 중 | 감액 및 거절 |
| R16 | 치료기간·입원일수 일부 불인정 (partial disallowance of treatment / hospitalization duration) | 중 | 감액 |
| R17 | 치료항목·비급여 비용 일부 불인정 (partial disallowance of treatment items / non-covered costs) | 중 | 감액 |
| R18 | 소득·휴업기간·가동기간 일부 불인정 (partial disallowance of income / work-loss / working period) | 상 | 감액 |
| R19 | 의료자문 결과에 따른 감액 (reduction based on medical advisory) | 상 | 감액 |
| R20 | 계약 전 알릴의무 위반에 따른 비례감액 (proportional reduction for pre-contract disclosure violation) | 상 | 감액 |
| R21 | 산재·타보험·제3자 기지급액 공제 (deduction of amounts paid by workers' compensation / other insurance / third parties) | 중 | 감액 |
| R99 | 기타 / 분류 불가 (other / unclassifiable) | 중 | 감액 및 거절 |

## Forbidden-expression substitutions

Definitive legal/medical assertions get hedged per harness-guardrails P3. The
substitution table lives in `templates/forbidden-expressions.md` -- the
authoritative copy per `open-decisions.md` #2, and the one both `draft-report`
(writer) and `critic` (checker) read. Not restated here: this file used to carry
its own copy, and a duplicate that nothing reads is a table that drifts silently.

# Priorities

- **P0**: OCR/text extraction + cross-validation, document classification, core-field extraction, coverage identification, denial/reduction-reason extraction, case-type classification.
- **P1**: cross-document inconsistency detection, policy-clause mapping (normalization + requirement matching), rebuttal generation, draft report structure/v1/v2.
- **Optional/deferred**: real vector indexing (the Stage-3 adapter's default no-op is fine at PoC scale; direct prompting/chunk search is what `denial-validation`'s retrieval sub-phase actually uses today).
