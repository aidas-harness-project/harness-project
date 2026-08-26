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
