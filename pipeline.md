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

Case intake → document processing → policy/claim analysis → screening
report → draft report v1. 10 top-level stages (redesigned down from an
18-step draft; several old steps turned out to be redundant or wrongly
ordered once P8's cross-validation and the DAO's guardrail hooks were
worked through -- see CLAUDE.md's changelog).

| # | Stage | Agent | Notes |
|---|---|---|---|
| 1 | Case Intake | (`tools/intake_case.py`) | D2-gated: every file gets a `_source_ledger.json` entry (pending → human sets approved/rejected); a single rejected file blocks the whole case |
| 2 | Document Processing | `document-pipeline` | One top-level stage, 3 internal DAO checkpoints: (a) provider-backed OCR + P8 dual-path cross-validation + classification (a plain-text source takes a deterministic embedded-text decode instead, `cross_validation_mode: deferred_poc`), (b) provider-backed redaction through `tools/redact_document.py` (via the `tools/redaction.py` Redactor abstraction: the LLM identifies PII spans, substitution is deterministic, a detected leak hard-fails), (c) deterministic chunking. A whole-document photograph/visual-evidence input may be human-resolved as `non_text_image`: no transcription/classification quote/redaction/chunk is fabricated, and it is recorded as `expert_review_only` plus an explicit `page_chunks.json` exclusion. All P8 readers are LLM-vision-backed (claude-cli / codex-cli / openai-api), so any reader pair is a documented weak P8 (`single_technology_weak_p8_poc`); a genuinely technology-independent reader (a real OCR engine) is deferred -- see `open-decisions.md` #4. |
| 3 | Indexing (adapter) | (tool, no agent) | Pass-through by default; swappable for real vector/BM25 indexing later without restructuring anything downstream |
| 4 | Policy Clause Processing | `policy-pipeline` | One stage, 3 internal sub-phases (boundary ID → extraction → normalization) |
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

# Phase 2 -- insurer denial/reduction response

Only 2 new stages -- everything else is Phase 1's agents reused on new
input (redesigned down from a 13-step draft that duplicated intake/OCR/
redaction for the insurer document and never actually assigned an owner
for rebuttal generation).

| # | Stage | Agent | Notes |
|---|---|---|---|
| 1 | Denial Validation | `denial-validation` | 2 internal checkpoints: (a) evidence retrieval + validate each denial reason against it, (b) generate rebuttal points from the validation. Insurer-vs-evidence disagreement is this stage's actual purpose, **not** a P6 conflict -- don't route it through the conflict ledger |
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

`insurance_certificate`, `insurance_policy`, `diagnosis_certificate`,
`medical_record`, `imaging_report`, `receipt`, `insurer_response`, `other`.

## Case types

후유장해 (permanent disability), 진단·수술비 (diagnosis/surgery cost), 실손
(out-of-pocket medical), 배상책임 (liability), 기타 (other). Determines
`template_id` at claim-analysis's checkpoint 3.

## Denial/reduction reason codes (R-codes)

The frequency tier is loss-adjuster-reviewed taxonomy metadata received on
2026-07-21. It describes observed operational frequency only. It is not source
evidence, classification confidence, or permission to prefer a frequent code
over the insurer's actual wording. The machine-readable copy lives beside the
enum in `common_component_output.schema.json`'s `taxonomy_code.x-codebook` and
is kept synchronized with this table by tests.

| Code | Reason | Frequency |
|---|---|---|
| R01 | 기왕증 / 기존 질환 기여도 (pre-existing condition contribution) | 상 |
| R02 | 장해율 과다 (disability rate overstated) | 상 |
| R03 | 손해액 과다 (damages overstated) | 상 |
| R04 | 약관상 지급요건 미충족 (policy conditions not met) | 상 |
| R05 | 면책사항 (exclusion clause) | 상 |
| R06 | 치료 필요성 부족 (treatment necessity insufficient) | 상 |
| R07 | 과잉진료 / 비급여 적정성 (overtreatment / non-covered-item appropriateness) | 중 |
| R08 | 서류 부족 (missing documents) | 하 |
| R09 | 동일 사유 재청구 (repeat claim, same reason) | 하 |
| R10 | 기존장해·동일 부위 장해 공제 (existing disability / same-body-part disability deduction) | 상 |
| R11 | 피해자 과실상계 (claimant contributory negligence) | 상 |
| R12 | 자기부담금·약정 공제금액 적용 (deductible / contracted deduction) | 하 |
| R13 | 중복보상·실손 비례보상 (duplicate coverage / indemnity proportional payment) | 중 |
| R14 | 가입금액·보상한도·일수한도 적용 (insured amount / coverage / day limit) | 하 |
| R15 | 면책기간·감액기간 적용 (exclusion / reduction period) | 중 |
| R16 | 치료기간·입원일수 일부 불인정 (partial disallowance of treatment / hospitalization duration) | 중 |
| R17 | 치료항목·비급여 비용 일부 불인정 (partial disallowance of treatment items / non-covered costs) | 중 |
| R18 | 소득·휴업기간·가동기간 일부 불인정 (partial disallowance of income / work-loss / working period) | 상 |
| R19 | 의료자문 결과에 따른 감액 (reduction based on medical advisory) | 상 |
| R20 | 계약 전 알릴의무 위반에 따른 비례감액 (proportional reduction for pre-contract disclosure violation) | 상 |
| R21 | 산재·타보험·제3자 기지급액 공제 (deduction of amounts paid by workers' compensation / other insurance / third parties) | 중 |
| R99 | 기타 / 분류 불가 (other / unclassifiable) | 중 |

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
