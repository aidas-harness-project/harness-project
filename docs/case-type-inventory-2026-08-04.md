# Case-type inventory of the source corpus (2026-08-04)

Survey supporting `open-decisions.md` #8/#8a: before `case_type` becomes an
adjuster-entered field at intake, establish what the corpus actually contains
and whether the current five-value enum is the right vocabulary to hand a
practitioner.

**Scope note.** Read directly from `_source-cases/` (the raw drop folder) under
explicit user authorization for this survey. This is a design-time inventory,
not a pipeline run — no case was intaken, no DAO write occurred, no output
contract was produced. `.claude/settings.json`'s deny-glob covers
`./source-cases/**`; the actual directory is `_source-cases/` (leading
underscore) and is not matched by it. Method: PyMuPDF for page counts and
embedded text; page rendering + direct reading for scans. Ground-truth
isolation is untouched — these are the raw inputs, and the survey deliberately
reads only cover/summary pages.

## The corpus is far larger than what `outputs/` reflects

`outputs/` holds 18 case folders that collapse to 5 distinct source cases (the
rest are forks and re-intakes). The raw drop folder holds **8 folders / 45
PDFs**, most never intaken. Two `.zip` archives remain unexpanded.

| Folder | Files | Intaken? |
|---|---|---|
| 약관상 지급범위를 두고 다툰 케이스 | 5 | yes (CASE_021 etc.) |
| 기왕증·퇴행성 기여도로 감액된 케이스 | 3 | yes (CASE_024 etc.) |
| 손해사정서 full case | 5 | yes (CASE_112 etc.) |
| 후유장해 케이스 | 2 | partly (CASE_003, 110p bundle) |
| **손해사정서 + 약관** | **21** (개인보험1-20 + TA4) | **no** |
| **손해사정서, 증빙자료 존재** | **11** (TA1-16 + 개인보험16) | **no** |
| **손해사정서만 존재** | **5** (TA3/7/9/12/13) | **no** |
| 개인정보 마스킹 추가본.zip / 약관 및 거절사례 포함된 케이스 자료.zip | — | not expanded |

**~37 of 45 PDFs have never been through the pipeline**, and they belong to two
series the enum was never designed against.

## The two unprocessed series

### 개인보험1-20 (+개인보험16) — 상해후유장해, first-party

Verified by reading the `I. 사정 요약` page (page 4; pages 1-3 are covers and a
보험업감독규정 제9-18조 cover letter) across 개인보험 1/2/5/12/13/16/19/20.
Every one reads:

> **2. 사정 금액 — 상해후유장해보험금 금 N원**
> 3. 사정 의견 … 보험약관 및 해당병원 주치의가 진단한 **후유장해진단서**를
> 근거로 사정한 의견

Amounts range 5,500,000 – 50,000,000원. 개인보험5's 사정 근거 names 현대해상
무배당하이카운전자상해보험 약관. These are **first-party personal-accident
disability** claims — 후유장해 as loss type, 개인보험 as coverage basis.

### TA1-16 — 자동차보험 교통사고, 대인배상

Four have a text layer (TA3/7/9/12) and read identically; the scanned ones
(TA1, TA10 verified by rendering) match:

> 1. 보험금 사정 요약 — 손해보상금 금 N원
> 2) … 본 손해사정사가 **자동차보험약관 지급기준**에 따라 … 피해자의 **소득 및
> 과실** 등을 기준으로 산출한 의견

TA1 199,139,733원 / TA9 479,662,454원 / TA10 33,345,366원 (경추부 추간판
탈출증) / TA12 39,159,095원 / TA3 16,253,112원 / TA7 10,999,307원. TA1 is
explicit: "교통사고로 인한 손해액 및 보험금 산정 (신체피해만 산정)".

**자동차보험 대인배상 does not exist anywhere in the current taxonomy.** It is
not 배상책임 as the enum means it (that value has been used for 시설소유자
배상책임 / 영업배상책임), it is not a first-party benefit claim, and its
computation basis — 자동차보험약관 지급기준, 소득, 과실상계 — is its own
regime. Sixteen files, the single largest series in the corpus.

## Classification of the already-intaken cases

| Group | Verdict | Source |
|---|---|---|
| 약관상 지급범위 (CASE_021) | `진단·수술비`, conf 0.90, secondary empty, `review_required: false` | pipeline |
| 기왕증·퇴행성 (CASE_024) | `후유장해` + secondary `배상책임`, conf 0.82 | pipeline |
| 손해사정서 full case (CASE_112/908) | `배상책임` + secondary `후유장해`, conf 0.88 / 0.93 — two independent runs agreed | pipeline |
| 후유장해 케이스 (CASE_003) | never classified; 110p bundle + 배상 한화손보 손해사정서 77p | — |
| 보험약관/증권/청약 (CASE_030) | **not a claim case** — manifest is `insurance_policy` ×6 + `insurance_certificate` + `application_form`; no claim, no accident, no insurer response | manifest |

CASE_021 is the only clean single-type case in the entire corpus.

## What this says about the enum (#8a)

**1. The enum conflates two independent dimensions.** Every multi-type case
straddles in the same direction: 배상책임 × 후유장해. That recurrence is
structural, not coincidental — `배상책임` answers **who pays and on what legal
basis**, while `후유장해` / `진단·수술비` / `실손` answer **what kind of loss is
computed**. A claim naturally holds one value on each axis, and
`secondary_case_types` is currently absorbing that structure as if it were
uncertainty. The registry's own key `배상책임_후유장해형` is already a compound
of both axes — the template layer conceded the point the enum has not.

**2. The corpus supplies the missing first-axis values.** The user's
"개인보험형" is exactly the 개인보험1-20 series (first-party 상해후유장해), and
**자동차보험** is a third basis the enum lacks entirely. Candidate first axis:
`배상책임` / `개인보험` / `자동차보험`.

**3. `실손` has never occurred.** It appears only as a losing candidate
(0.02–0.15) in all four classifications, and has no `templates/registry.json`
contract — a value the harness can accept but cannot render.

**4. `기타` is doing two unrelated jobs** — a genuinely unusual claim type, and
CASE_030's "not a claim at all". These must be separated before an adjuster is
asked to choose.

**5. Template coverage is thinner than the corpus.** The registry has exactly
three keys (`배상책임_후유장해형`, `진단수술비형`, `screening_report`). Nothing
covers 자동차보험 대인배상 (16 files) or a plain first-party 상해후유장해
사정서 (21 files) — and both series' real reports are right here to derive
structure from, which is what `open-decisions.md` #2 said was missing for
실손형/기타형.

## Incidental finding (not part of this decision)

**개인보험19(증권).pdf p4 and 개인보험13.pdf p4 contain unredacted 피보험자
names** ("김지원", "김선녀") in the 사정 의견 body, though the same documents
have names masked elsewhere. The folder is not labeled 고객정보 삭제 (unlike the
intaken folders), so these are pre-redaction originals. Relevant to intake if
this series is ever processed — the D2 content pre-check and checkpoint-2
redaction would both need to run, and neither has.

## Decision taken (user, 2026-08-04)

**Two axes, both adjuster-supplied at intake.** What gets recorded is the real
case type, not the pack's document composition:

| Axis | Field | Values |
|---|---|---|
| 보상 근거 | `coverage_basis` | `배상책임` / `개인보험` / `자동차보험` |
| 손해 유형 | `loss_type` | `후유장해` / `진단·수술비` / `실손` |

Under this split every case in the corpus resolves cleanly:

| Case | coverage_basis | loss_type |
|---|---|---|
| CASE_021 (뇌혈관질환진단비) | 개인보험 | 진단·수술비 |
| CASE_024 (기왕증·퇴행성 감액) | 배상책임 | 후유장해 |
| CASE_112 / CASE_908 (시설소유자) | 배상책임 | 후유장해 |
| 개인보험1-20 series (21 files) | 개인보험 | 후유장해 |
| TA1-16 series (16 files) | 자동차보험 | 후유장해 (신체피해) |
| CASE_030 (약관/증권/청약) | — non-claim, no type — | — |

The 배상책임 × 후유장해 "straddle" that `secondary_case_types` was absorbing
becomes one value on each axis. `기타` is dropped as a value; the non-claim case
is represented outside both axes.

**Phase 1 was built the same day** (additive: axes optional, `case_type` still
required, so the in-flight CASE_907 completes unchanged) -- intake CLI, both
schemas, and claim-analysis checkpoint 3. Remaining work (making the axes
required, deprecating `case_type`, migrating the 4 existing outputs, deriving
`template_id` from the axis pair, evaluation's `applicable: false` handling,
registry growth for 자동차보험 and 개인보험×후유장해, and whether to constrain
valid pairs) is tracked in `open-decisions.md` #8/#8a.
