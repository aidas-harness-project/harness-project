---
type: Architecture
title: Loss-Adjustment Pipeline
description: Stage/agent map, taxonomy, and I/O contracts for the loss-adjustment harness. Redesigned from a coworker's rough draft into the shape actually implemented -- see CLAUDE.md's changelog for what changed and why.
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
were worked through -- see CLAUDE.md's changelog).

| # | Stage | Agent | Notes |
|---|---|---|---|
| 1 | Case Intake | (`tools/intake_case.py`) | D2-gated intake: every file gets a `_source_ledger.json` entry; a single rejection blocks the case. Every intaken PDF starts `segmentation_status: pending_review` until a genuine human records `required` or `not_required` through the DAO. Automatic bundle classification is not implemented. **No OCR and no splitting here** -- intake produces the reviewed file inventory; the split itself is a checkpoint of stage 2, because it now happens between two halves of document processing. Records under the `intake` run-state stage. |
| 2 | Document Processing | `document-pipeline` | One top-level stage; segmentation lives **inside** it, since processing runs on both sides of the split. Checkpoints: **(a)** bundle OCR (`run_checkpoint1.py --bundle-ocr`) -- OCR only, no classification, because `document_type` is per-document and one label cannot fit a bundle mixing a 진단서, a 검사 판독지 and a 진료비 명세서; **(b)** bundle redaction; **(c)** segmentation over the *redacted* text -- `segment_case.py propose` resolves evidence as processed text → the PDF's embedded layer → vision, so a scan reaches the deterministic path for the first time. Titles are the boundary signal (`...보통약관`/`...특별약관`/`...특약`; medical form names through a 5-line header window), a page the rule cannot settle goes to the LLM tier, and an unusable verdict splits and is flagged. Human range approval gates `split`, which retains the bundle as `superseded_bundle` and hands each child the bundle's pages renumbered from 1 with P8 verdicts intact -- **children are never re-OCR'd**; **(d)** per-child classification; **(e)** per-child redaction via `tools/redact_document.py` (the `tools/redaction.py` Redactor abstraction: the LLM identifies PII spans, substitution is deterministic, a detected leak hard-fails); **(f)** case-wide deterministic chunking. A plain-text source skips (a) for a deterministic embedded-text decode (`cross_validation_mode: deferred_poc`). A whole-document photograph/visual-evidence input may be human-resolved as `non_text_image`: no transcription/classification quote/redaction/chunk is fabricated, and it is recorded as `expert_review_only` plus an explicit `page_chunks.json` exclusion. Each materialized child owns a real `data/raw/.../DOC_XXX.pdf`, so its manifest `document_role` is `physical`; `source_file_name` and `source_page_start`/`source_page_end` retain bundle provenance but do not make it a raw-less processed-text `segment`. `run_checkpoint1.py`'s case-wide DAO preflight still returns `blocked_segmentation` before any provider or PDF work -- `--bundle-ocr` narrows it (a `pending_review`/`required` PDF may be OCR'd, that being the point) but never lifts the `superseded_bundle` refusal. A provisional type is never trusted downstream. All P8 readers are LLM-vision-backed (claude-cli / codex-cli / openai-api), so any reader pair is a documented weak P8 (`single_technology_weak_p8_poc`); a genuinely technology-independent reader (a real OCR engine) is deferred -- see `open-decisions.md` #4. Pipeline execution remains agentic through the skill; there is no standalone runner. |
| 3 | Indexing (adapter) | (tool, no agent) | Pass-through by default; swappable for real vector/BM25 indexing later without restructuring anything downstream |
| 4 | Policy Clause Processing | `policy-pipeline` | **Normalization is opt-IN.** A document classified `insurance_policy` starts `text_only_no_normalization`: fully OCR'd, redacted, chunked and citable, owing no clause contract. Nothing downstream consumes the normalized buckets -- `claim-analysis` and `denial-response` address a clause by `document_id` + page + verbatim quote, verified byte-for-byte against the processed text, so requiring normalization to CITE a policy document made the expensive obligation universal for no consumer (one 145-page bundle carries 800+ conditions). Promotion to `automated_text_pipeline` is per-document and requires a stated dispute (`dao.py promote-policy-document --disputed-by`), which demotes this stage so it re-finalizes against the new obligation. What the stage still gates on is TEXT: a case with no text-processed policy document cannot finalize, and canonical UID verification covers every citable policy document regardless of normalization. Clause references accordingly accept two addressing forms (UID into a normalized contract, or source-form page+quote); see `.claude/agents/claim-analysis.md` and the `coverage_result`/`requirement_matching_result` schemas. When a document IS normalized, everything below applies to it: boundary inventory → exact source-span accounting → semantic classification → normalization mapping. Every non-whitespace policy-source character is covered or explicitly excluded, but normative article/paragraph/item anchors cannot be hidden inside an exclusion. Boundary↔clause mappings are checked in both directions; unresolved/missing/dangling boundaries block finalization. Immutable source-local `clause_uid`/`condition_uid` values are downstream join keys, while sequential `clause_id` is display-only; cross-insurer canonical matching, if added later, is a separate layer. Clause kinds use dedicated buckets so obligations, definitions, procedures, termination, disputes, and coverage-start rules cannot be mistaken for payout conditions. Meaning validation precedes the lexical support floor: complete operative predicates, negation, and numeric/temporal terms must be evidenced, while composite or uncertain support is review-gated and condition wording is never weakened merely to raise overlap. When a large policy PDF is one physical parent carved into segments (e.g. CASE_030's 240-page 약관: parent `DOC_001` split into per-특약/조항 segments + a 별표 appendix segment), two shapes are exempt from the per-document normalization requirement: a **segmented physical parent** is normalized through its segments, so `policy_parent_coverage_{id}.json` accounts for every logical and immutable-source physical page exactly once. Page ownership must resolve against the segment's actual logical/physical `page_map`; administrative exclusions require page-specific evidence, and unpaged physical exclusions require human provenance. A **reference-table-only segment** is waived from normalized clauses and audit only when its tables and cells have page-local evidence and no unresolved review flag; it still owes a complete boundary inventory. Every other segment owes normalized clauses + boundary inventory + audit. Decision-bearing appendices use immutable source-derived table/row/cell UIDs and normalized clauses link by UID, never by array position or display label. Each normalizing document also has a version-bound audit, but an empty authored findings list cannot conceal deterministic defects: finalization reruns completeness and provenance checks independently. Downstream references resolve only against the current clear audit. |
| 5 | Claim Analysis | `claim-analysis` | One stage, 4 internal checkpoints: field extraction → coverage ID → case-type classification → requirement matching. Case-type classification is a hard, independently-validated gate -- see claim-analysis.md's note on why |
| 6 | Consistency Check | `consistency-check` | Any cross-document disagreement goes to `_conflict_ledger.json`, not an inline halt -- see harness-guardrails P6 |
| 7 | Screening Report | `screening-report` | Gated on `check-conflicts-clear`; consumes `denial-response`'s output whenever an insurer-response document exists (a dependency, not a phase gate) |
| 8 | Draft Report v1 | `draft-report` | Same agent reused for the v2 update in Phase 2 |
| 9 | Critic Pass (v1) | `critic` | Blind -- structurally cannot read ground truth |
| 10 | Evaluation | `evaluation` | Sole D1 exception, only after human review is marked complete |

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
| — | Evaluation | `evaluation` | Same agent as Phase 1 |

# Document-assembly tool

Narrative outputs (`screening_report.md`, `draft_report_v*.md`,
`rebuttal_points.md`) are never hand-written directly by an agent. An agent
provides per-section `{content, evidence_references}` (with `{{E}}`
placeholders inline wherever a citation belongs) to
`tools/document_assembly.py`, which renders the file and auto-generates the
`[E#]` tags plus the `.evidence.json` sidecar in one pass -- see
harness-guardrails P1.

**Section/template rules are defined** for `배상책임_후유장해형`
(변형 A, I~VII) and `진단수술비형` (변형 B, I~VI) -- see `templates/`
(`draft-report.md`, `screening-report.md`, `rebuttal-points.md`,
`forbidden-expressions.md`, `component-output.md`), adopted from the wiki
2026-07-13. `실손형`/`기타형` still have no ground-truth basis (TODO in
`templates/draft-report.md`). Section presence/order is structurally
enforced (2026-07-14): `document_assembly.py --template <key>` validates
against `templates/registry.json` and refuses to write on mismatch --
rebuttal_points is the one deliberate exception (dynamic per-reason
structure, no registry entry). See open-decisions.md #2.

# Taxonomy

## Document types

`insurance_certificate`, `insurance_policy`, `application_form`,
`diagnosis_certificate`, `medical_record`, `imaging_report`, `receipt`,
`insurer_response`, `other`.

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

후유장해 (permanent disability), 진단·수술비 (diagnosis/surgery cost), 실손
(out-of-pocket medical), 배상책임 (liability), 기타 (other). Determines
`template_id` at claim-analysis's checkpoint 3.

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
