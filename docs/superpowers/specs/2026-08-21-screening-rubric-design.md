# Screening-report rubric — design

**Date:** 2026-08-21
**Status:** approved in chat, pending spec review
**Unit:** `screening-rubric` (scoring agent + rubric + result contract)

## Purpose

Give an agent a finished screening report and have it produce an auditable
score for how well that report was written. The scoring logic comes from the
domain material in `rubric_reference/`, which encodes what a medical-advisory
reviewer actually looks for.

The rubric is **reference-free**: it judges the report as a document, against
domain requirements, without an answer key.

## Governance position

`screening-rubric` is a blind reviewer, structurally identical in standing to
`critic`: it never reads `source-cases/`, `data/ground_truth/`, or any other
answer-key material, and it never opens `outputs/`/`data/` directly (P2/P5/P7 —
all reads and writes go through `tools/dao.py`).

It is **not** the deferred evaluation unit. `evaluation` compares pipeline
output against ground truth and stays unavailable until the isolated Unit 11
service exists. This unit compares a report against a rubric. The name
`evaluation` is deliberately not reused anywhere in this design, including in
field and stage names.

## Inputs — deliberately narrow

The scorer receives the rendered screening report and, when the case has one,
its machine-readable twin:

- `screening_report*.md` — the rendered report; the object being scored. Always
  required.
- `screening_report.json` — optional. Used only to cross-check section roles and
  `evidence_references` when it exists. **The rubric must score correctly from
  the `.md` alone**, because the supplied test corpus (`rubric_test/`, four
  reports read 2026-08-21) is markdown-only.

It does **not** receive upstream contracts (`extracted_claim_fields`,
`coverage_result`, `case_type_result`, `document_manifest`,
`denial_reason_result`) and does **not** receive redacted source text.

Consequence, stated plainly: the scorer cannot distinguish "the report omitted
a fact" from "the fact was never in the case material". The rubric resolves
this with the silence-vs-declaration rule below rather than by widening inputs.

### Section roles (what A1 actually checks)

Three different rendered shapes exist in this repo, observed 2026-08-21:

| Shape | Where | Sections |
| --- | --- | --- |
| 7-section bullet skeleton | `templates/screening-report.md`, `templates/registry.json` | 사건 개요 / 보험사 판단 / 핵심 쟁점 / 문서 간 불일치 / 추가 필요 서류 / 전문가 검수 포인트 / 1차 판단 |
| 8-section narrative | `outputs/CASE_021/screening_report.md` | prose sections + 초안 작성 참고 |
| 10-section current | all four reports in `rubric_test/` | 사고와 공통 의료정보 / 사건유형 병렬 판정 / 유형별 추가정보와 접수 상태 / 의료영역 확보 현황 / 필요서류 체크리스트 / 중요 충돌과 유형 판정 영향 / 주요 미확인 항목 / 관련 약관과 요건 자료상태 / 보험사 응답 / 검토 시 유의사항 |

A1 therefore scores **roles, never heading literals**. The rubric defines eight
roles — case-and-medical overview, case-type determination, medical-variable
coverage, required-document status, conflicts, unconfirmed items, policy/coverage
status, insurer response, reviewer guidance — and asks whether each role is
served *somewhere* in the document. A role a given shape does not carry (the
7-section shape has no separate 미확인 항목 section) is `applicable: false`, not
a zero.

The 10-section shape is the one the corpus uses and the one the anchors are
written against; the other two must not auto-fail.

## Scoring model

Six axes, each scored 0–4 against written anchors, combined by weight into a
0–100 total. Gates are evaluated separately and can block a report regardless
of its total.

| Axis | Measures | Source | Weight |
| --- | --- | --- | --- |
| A1 structural completeness | every required section role present; no empty fields, placeholders, or stub text | `templates/screening-report.md` + `schemas/screening_report.schema.json` | 10 |
| A2 core-variable coverage | grade-A variables present as a value **or** an explicit scoped absence; grade-B variables where the report itself establishes the record exists | `rubric_reference/의료자문_핵심변수_우선순위.docx` | 25 |
| A3 evidence-tier grounding | material assertions attributed to grade-A/B records; a core claim resting only on grade-C records is penalised | `rubric_reference/의료자문_의무기록_우선순위.docx` | 15 |
| A4 case-type and required-document coherence | declared case type coherent with claimed coverages and accident narrative; the missing-documents section reflects that type's required document set | `rubric_reference/유형별 주 필요서류 정리 및 유형분류 프로세스.hwpx` | 20 |
| A5 issue and assessment actionability | key issues tied to facts stated in the report; reviewer routing (손해사정사 vs 의사) matched to the nature of each question; preliminary assessment reasoned rather than asserted | combined | 15 |
| A6 internal consistency | dates, diagnoses, and treatment periods agree across sections; declared inconsistencies are real contradictions | combined | 15 |

### Anchors

Each axis carries five written anchors (0–4) in the rubric file, phrased against
concrete report behaviour, not adjectives. Every anchor states what the report
must *contain* to earn that level, so two scorers reading the same report reach
the same level for the same reason.

### Silence vs declaration (A2's core rule)

Taken directly from the 핵심변수 document's 표현 원칙:

- A grade-A variable stated with a value → full credit.
- A grade-A variable absent, with an explicit scoped absence
  ("제공된 자료에서 확인되는 …없음") → full credit. The report is doing the
  right thing with material it does not have.
- A grade-A variable absent, with no mention at all → penalty. Silence is
  indistinguishable from an oversight, and the reader cannot tell which.

This is the mechanism that makes a report-only rubric fair. It also aligns with
P3: a report is rewarded for scoping its claims to available material.

### N/A handling

An axis (or a listed sub-item) that cannot apply to the case — disability
sub-items on a non-disability claim, insurer-position depth on a case with no
insurer response — is recorded as `applicable: false` with an `na_reason`, and
its weight is removed from the denominator:

```
weighted_total = 100 * Σ_applicable(raw_i / 4 * w_i) / Σ_applicable(w_i)
```

Convention borrowed from `schemas/evaluation_result.schema.json`, which already
self-contains `applicable`/`na_reason` per metric so an unavailable comparison
is recorded rather than silently skipped.

### Gates

A gate is a categorical defect. Any triggered gate sets `verdict: blocked`
regardless of `weighted_total`, and the total is still reported.

| Gate | Trigger | Detection |
| --- | --- | --- |
| G1 forbidden expression | a listed literal from `templates/forbidden-expressions.md`, or an unhedged paraphrase of one | `python tools/dao.py check-forbidden-expressions <report_path>` for the deterministic floor, then a semantic pass. A `clean: true` does not discharge the semantic pass. |
| G2 advisory-judgment encroachment | the report computes a disability rate, declares 증세고정, or asserts causation outright | semantic; the 핵심변수 document places these in grade D as the advising physician's call, not an extraction |
| G3 unscoped absence claim | "기왕력 없음" or equivalent stated as fact rather than scoped to available material | semantic |
| G4 cross-section factual contradiction | a date, diagnosis, or period asserted differently in two sections | semantic, cross-checked against `screening_report.json` |
| G5 case-type/coverage contradiction | declared case type incompatible with the claimed coverages or the accident narrative | the hwpx type→coverage mapping |

**Verdict:** any gate triggered → `blocked`; else `weighted_total >= 80` →
`pass`; else `revise`.

### Evidence requirement

Every axis score and every triggered gate must carry at least one verbatim
quote from the report being scored. The schema requires it (`minItems: 1`), so
a score with no quotation cannot be written. This is what keeps a rubric score
auditable instead of a vibe.

## Artifacts

1. **`templates/rubric-screening-report.md`** — the rubric itself. **Written in
   Korean** (user decision, 2026-08-21): axes, 0–4 anchors, the
   silence-vs-declaration rule, gate definitions, N/A policy, worked examples.
   Korean because both the scored document and its human readers
   (손해사정사·의사) are Korean, and the anchors quote report phrasing directly.
   This is the exception this project already grants deliverable-facing
   material; the surrounding code, schema descriptions, and agent definition
   stay English.
2. **`schemas/screening_rubric_result.schema.json`** — the result contract.
3. **`.claude/agents/screening-rubric.md`** — the scoring agent, regenerated
   into `.codex/agents/` and `.agents/skills/` by `tools/sync_agents.py`.

## Result contract

Extends `common_component_output.schema.json` (`case_id`, `run_id`,
`component: "screening-rubric"`, `status`, `created_at`, `model_info`).

| Field | Shape | Why |
| --- | --- | --- |
| `rubric_version` | const, e.g. `screening_rubric.v0.1` | scores from different rubric revisions are not comparable; the version says which one produced this |
| `target_report_path` | string | which document was scored |
| `target_report_sha256` | 64-hex | binds the score to the exact report bytes. Same device `screening_report.schema.json` uses with `source_denial_contract_hash`: a score cannot silently describe a superseded report |
| `axis_scores[]` | `{axis_id, raw (0–4 int), weight, applicable, na_reason, rationale, evidence_quotes[] (minItems 1 when applicable)}` | per-axis, auditable |
| `weighted_total` | number 0–100 | the headline |
| `gates[]` | `{gate_id, triggered, quote, section, description}` | categorical defects, separate from the score |
| `verdict` | enum `pass` / `revise` / `blocked` | the decision |
| `findings[]` | `{finding_id (SR-N), severity, section, description, fix_suggestion}` | what to change, numbered stably so a human disposition can reference one |

Written through `python tools/dao.py write-contract` under stage name
`screening_rubric_v1` — never a bare `screening-rubric`, since `_run_state.json`
rejects unregistered spellings and free-form names have previously forked one
stage into duplicate entries (CASE_021).

## Agent behaviour

1. Read the report and its JSON twin through the DAO.
2. Run the deterministic floor: `check-forbidden-expressions` on the report path.
3. Score each axis against the anchors, quoting the report for each.
4. Evaluate the five gates.
5. Compute `weighted_total` over applicable axes; set `verdict`.
6. Write `screening_rubric_result_v1.json` via `write-contract`.

Schema validation failure: one self-correction attempt, then halt (P4). A low
score or a triggered gate is normal output, not a stage failure.

## Calibration and delivery

The rubric is not done until it has been run. Corpus, all read 2026-08-21:

| Report | Size | Note |
| --- | --- | --- |
| `rubric_test/screening_report_705.md` | 31,339 B | liability case; conflicting legal opinions; disability rating present |
| `rubric_test/screening_report_710.md` | 24,523 B | separate case |
| `rubric_test/screening_report_711.md` | 28,029 B | — |
| `rubric_test/screening_report_712.md` | 28,029 B | **byte-identical to 711** (md5 `58a5770…`) |

The 711/712 pair is a free determinism probe: identical bytes must produce the
same axis scores and the same verdict. A divergence is a finding about the
rubric's anchors being underspecified, and is reported as such rather than
quietly averaged.

`outputs/CASE_021/screening_report.md` stays in the calibration set as the
narrative-shape control — it must not blow up on a different section shape.

Deliverable: one scored result per report, plus a short cross-report readout
saying which axes discriminated and which anchor wording proved ambiguous.

Two things the corpus already shows, which the anchors are written against:

- Explicit scoped absence is the house style: "라우팅 우선순위상의 어떤 출처도
  이 항목을 기재하지 않았습니다", "기재가 없다는 점을 부정 소견으로 보지는
  않습니다". That is A2 level-4 behaviour, and CASE_021 does the same thing in
  prose.
- Every factual line carries an `[E#]` tag resolved in a trailing 출처 block that
  names the document kind (진단서, 수술기록지, 경과기록지, 입퇴원요약,
  법률의견서). A3's evidence-tier check reads that block; it is what makes
  grade-A/B/C attribution decidable from the `.md` alone.

## Observed context — registry drift, not part of this unit

`templates/registry.json`'s `screening_report` entry pins seven headings with
`allow_extra_sections: false` (`^4\. 문서 간 불일치`, `^5\. 추가 필요 서류`,
`^7\. 1차 판단`). The report on disk has eight sections with different titles
("4. 문서 정합성 검토", "5. 추가 필요 자료", "6. 검토 요청 사항 (라우팅)",
"7. 예비 평가 (잠정 소견)", "8. 초안 작성 참고"). Both files were read on
2026-08-21; the mismatch is observed, not inferred.

Consequence for this design: **A1 scores section ROLES, mapped to
`screening_report.schema.json`'s fields, never heading literals.** A rubric
keyed to the registry's strings would fail the only real report in the repo.

The drift itself is a separate defect — either the registry or the renderer is
stale — and is out of scope here. Flag it to the user; do not fix it inside
this unit.

## Out of scope

- `rubric_reference/후유장해 기간 분류표.zip` — thirteen era-specific disability
  tables in legacy binary `.hwp` plus a McBride `.xlsx`. Excluded for two
  reasons: extracting `.hwp` needs a converter this repo does not have, and
  "did the report select the disability table matching the contract date" is
  not decidable from the report alone, which is this unit's whole input.
  Revisit if the scorer's inputs ever widen.
- Automatic scoring code. The scorer is an agent; the only deterministic helper
  is the existing forbidden-expression check.
- Any comparison against ground truth. That remains Unit 11's.
