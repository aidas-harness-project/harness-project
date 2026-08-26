# Fidelity corpus import — 2026-08-26

`묶음.zip` (2.8 MB, worktree root) unpacked into this checkout. `outputs/` and
`data/` are gitignored, so nothing here is committed; this note is the record.

## What landed

**Case trees → `outputs/`** — CASE_705, CASE_710, CASE_711, CASE_712, each with
`screening_report.md/.json/.evidence.json`, `screening_report_judgement.json`,
the upstream contracts, `_backups/step_01_*`, `_source_ledger.json`,
`_run_state.json`. No collision: none of the four existed here before.

**Ground truth → `data/ground_truth/CASE_XXX/GT_001.pdf`**, inside the denied
tree. Renamed because `read-ground-truth --file` takes a stem and the originals
carry spaces and Korean; the mapping is here so provenance survives outside the
tree (a filename is not answer content).

| Case | Original name | Bytes | SHA-256 |
| --- | --- | --- | --- |
| CASE_705 | `CASE_705_GT_001.pdf` | 283,689 | `c5056813d78abac2…` |
| CASE_710 | `CASE_710_GT_TA1.pdf` | 82,329 | `64a816895000ccab…` |
| CASE_711 | `CASE_711_GT_배상 17.pdf` | 284,246 | `9ed8a4f147f1227d…` |
| CASE_712 | `CASE_712_GT_개인보험 15.pdf` | 229,933 | `ba4c7248b55d6a6f…` |

Contents were not opened. Placement was a copy.

## Gates verified on the real cases

```
$ dao.py read-ground-truth CASE_705 --caller-stage claim_analysis --version screening --list
DENIED: ground truth may only be read by ['evaluation', 'screening_fidelity'] …

$ dao.py read-ground-truth CASE_705 --caller-stage screening_fidelity --version screening --list
DENIED: human review is not yet marked complete for screening of this case …
```

Both correct. The second is the remaining human step: **the sign-off was not
performed.** `mark-human-review-complete` is trusted precisely because a person
runs it; an agent running it would be self-certifying, which is the CASE_002
failure shape.

## Three findings that change what the corpus is worth

### 1. CASE_712 is not a usable fidelity case

Its run did not finish:

| Stage | Status |
| --- | --- |
| intake | passed |
| document_processing | **in_progress** |
| policy_clause_processing | **failed** |
| claim_analysis | **failed** |
| consistency_check | **failed** |
| screening_report | **failed** |

A `screening_report.md` exists anyway. The screening-review gate refuses this
case — correctly: a report whose stage never passed is not a reviewed artifact.
How a report came to exist under a failed stage is not determinable from the
retained records here.

CASE_705, CASE_710 and CASE_711 all end at `screening_report: passed`.

### 2. The earlier reference-free scores mislabelled one case

`rubric_test/`'s `screening_report_711.md` and `_712.md` were byte-identical
(md5 `58a5770…`). Against the imported trees:

| File | md5 |
| --- | --- |
| `outputs/CASE_711/screening_report.md` | `77f2a3e6…` |
| `outputs/CASE_712/screening_report.md` | `58a5770…` |

So the bundle carried **CASE_712's report twice**, labelled 711 and 712. The
`screening_rubric` run of 2026-08-21 scored that document under both names; the
real CASE_711 report was never scored. Rows 711 and 712 of that table both
describe CASE_712's report — and CASE_712 is the case whose run failed.

The identical-pair "determinism probe" in that calibration note was therefore
measuring even less than recorded: not two independent scorings of one document,
but two labels on one file that was never CASE_711's.

### 3. Three of the four answer keys arrived outside intake review

`_source_ledger.json` per case:

| Case | raw entries | ground_truth entries |
| --- | --- | --- |
| CASE_705 | 5 | 1, approved |
| CASE_710 | 1 | 0 |
| CASE_711 | 1 | 0 |
| CASE_712 | 1 | 0 |

Only CASE_705's D2 intake review covered a ground-truth file. The other three
answer keys were placed by this import, so no per-file human classification
review stands behind them. They are ground truth by provenance — the user
supplied them as the adjuster's reports — not by a recorded review. No ledger
entries were fabricated to paper over this.

## State

Ready to score: **CASE_705, CASE_710, CASE_711**, once each is signed off with
`dao.py mark-human-review-complete CASE_ID screening --reviewer NAME --held-by NAME --run-id RUN_ID`.

Not ready: CASE_712 (failed run).

---

# Scoring run — 2026-08-26

## CASE_705 — scored

`screening_fidelity_result_v1.json` written to
`data/ground_truth/CASE_705/_verification/`, schema-validated by the DAO.

| | |
| --- | --- |
| F1 factual field agreement | **83.3** (fact 9/11; discretionary 0/5, reported separately) |
| F2 issue-prediction recall | **50.0** (4 of the answer key's 8 issues) |
| F3 required-document agreement | **60.0** (3 correct, 1 under, 1 miss) |
| F4 conclusion direction | `screening_declined` — not scored |
| **fidelity_score** | **68.7** |
| core field mismatch | none |
| **verdict** | **partial** |

The pair is genuinely matched: same diagnosis and code, same accident narrative,
same admission period, same 13% permanent rating.

Four findings, of which two are structural rather than per-report:

- **SF-1 (high)** — `현재 치료 상태: 입원예정` against an answer key recording
  ongoing outpatient care. The report also contradicts itself: the same section
  gives 입원기간 2023-12-04~07. An upstream value was carried without a
  reference date.
- **SF-2 (medium)** — all four missed issues sit in damages calculation (income
  basis, 위자료, direct claim right, future treatment). That is work screening
  does not do, so the recall loss is about not signalling what the next stage
  will need, rather than failing to spot issues.
- **SF-3 (medium)** — the answer key's evidence for the accident narrative is a
  사고경위서, a document kind the required-document checklist does not contain at
  all. The central issue of this case is the accident narrative.
- **SF-4 (low)** — statute differs (report carried DOC_006's 민법 제758조 제1항;
  the adjuster applied 민법 제750조·제755조). Discretionary, kept out of the
  headline.

## Correction — CASE_711 is correctly paired; the earlier claim was wrong

The section this replaces said CASE_711's answer key was for a different case.
That was wrong, and wrong in a way worth recording: I never read
`outputs/CASE_711/screening_report.md`. What I read and called "711" was
`rubric_test/screening_report_711.md`, which is CASE_712's report — the
mislabelling found earlier in this same note. I then compared CASE_712's subject
against CASE_711's answer key and reported a mismatch that does not exist.

Read from the real file:

| | Report | Answer key placed with it |
| --- | --- | --- |
| CASE_705 | 우측 손목 요골 원위부 골절, 결빙 계단, 배상 | same case ✅ |
| CASE_711 | **요추부 압박골절 (L1)**, 척추 32% 영구 | 요추부 압박골절 S32090, 나무 구덩이 전도, 척추골절 I-A-1-C 32% ✅ |

## CASE_711 — scored

| | |
| --- | --- |
| F1 factual field agreement | **53.8** (fact 7/13; discretionary 0/7) |
| F2 issue-prediction recall | **37.5** (3 of 8) |
| F3 required-document agreement | **60.0** |
| F4 | `screening_declined` |
| **fidelity_score** | **50.1** |
| **verdict** | **divergent** |

Diagnosis, level, and the whole disability block (32%, permanent, McBride) match
exactly. The score falls on two things: an accident narrative where the answer
key's version (나무 구덩이) is **neither** of the two the report preserved, and
five fields the report declared unavailable that the answer key does carry
(admission period, outpatient period, current status, treatment cost, coverage).

The report's own diagnosis of that gap is correct — it names the missing
document kinds (최종진료기록, 주요검사결과, 진료비영수증) as the cause. The loss
is in evidence collection, not judgement.

**SF-1 (high)** is a self-contradiction: the header says `진단코드: 확인 불가`
while §10 quotes S32090 from the 진단서. The report has the value and declares it
missing.

**SF-4** repeats CASE_705's SF-3 exactly: 사고경위서 is not a document kind the
checklist contains, in a second case where the accident narrative is the central
issue. Two for two makes it a checklist defect, not a per-report miss.

## CASE_710 — still not scored, and the evidence points at a 710/712 swap

`data/ground_truth/CASE_710/GT_001.pdf` is a 자동차보험 손해사정서: 자배법 제3조,
상치골지골절 / 천골골절(천장관절) / 골반환 골절, 휴업손해 60일, 상실수익 27%,
과실 10%. CASE_710's report is a wrist case — 요골 하단의 상세불명 골절 (S52590),
좌측, 개인보험 후유장해, 보행 중 전도. No body part, insurance line, or mechanism
in common.

That answer key matches **CASE_712's** report on three independent points:

| | Answer key (placed under CASE_710) | CASE_712's report |
| --- | --- | --- |
| 장해상병명 | 상치골지골절 / 천골골절(천장관절) / 골반환 골절 | 상치골지골절 / 천골 골절 (천장관절 골절) / 골반환 골절 |
| 노동능력상실률 | 27% | 맥브라이드 27% (천장관절 골절) |
| 입원일수 | 60일 | 합계 만 60일 (DOC_012) |

So the likely state is that the CASE_710 and CASE_712 answer keys are swapped —
`CASE_712_GT_개인보험 15.pdf` being the only 개인보험 file, and CASE_710's report
being the only 개인보험 report. That last step is **unverified**: the file sits
under CASE_712, whose run failed, so the gate refuses to open it and no attempt
was made to go around the gate.

Nothing was moved. Swapping the two files would make CASE_710 scorable and is one
copy away, but it is a change to answer-key placement and belongs to the owner.
