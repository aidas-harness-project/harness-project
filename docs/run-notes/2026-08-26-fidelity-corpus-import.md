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

## CASE_710 and CASE_711 — not scored: the answer keys are for different cases

Read through the gate and compared against each report's own subject:

| Placed as | Answer key is about | The report is about |
| --- | --- | --- |
| `data/ground_truth/CASE_710/GT_001.pdf` | 자동차보험 손해사정서 — 상치골지골절 / 천골골절(천장관절) / 골반환 골절, 자배법 제3조, 과실 10%, 상실수익 27% | 요골 하단의 상세불명 골절 (S52590), 좌측 손목, 개인보험 후유장해, 보행 중 전도 |
| `data/ground_truth/CASE_711/GT_001.pdf` | 배상책임 손해사정서 — 요추부 압박골절 (S32090), 나무 구덩이에 걸려 전도 | 천골(薦骨)의 골절 (S3210), 차대 차 교통사고, 골반환 골절, 맥브라이드 27% |

Neither pair matches. One re-pairing is evident from content: the answer key
placed under CASE_710 is the traffic case with 천장관절 골절 and a 27% rating —
which is CASE_711's report. The file names carry the same signal
(`CASE_710_GT_TA1.pdf` — TA for traffic accident).

That leaves two open ends:

- The answer key placed under CASE_711 (`배상 17`, 요추부 압박골절) matches **no
  report in this corpus**.
- CASE_710's report (개인보험, 손목) has no matching answer key here. The one
  named `CASE_712_GT_개인보험 15.pdf` is the only 개인보험 file, so it is the
  likely partner — **unverified**, because the gate refuses CASE_712 (its run
  failed) and no read was attempted around it.

Nothing was re-placed. Scoring a report against another case's answer key would
produce a number with no meaning, and moving answer-key files on inference is
not a step to take without the owner's decision.
