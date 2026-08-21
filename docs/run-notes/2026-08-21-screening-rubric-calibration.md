# Screening-rubric calibration run — 2026-08-21

First run of `templates/rubric-screening-report.md` (rubric_version
`screening_rubric.v0.1`) against real screening reports. Five reports scored,
all five results schema-validated.

Rubric: `templates/rubric-screening-report.md`
Contract: `schemas/screening_rubric_result.schema.json`
Results: `rubric_test/results/screening_rubric_result_CASE_*.json`

## Corpus

`case_id` in each result is taken from the report's filename, not from a case in
`outputs/` — the four `rubric_test/` reports arrived as standalone markdown with
no case directory. Only CASE_021 is a real case in this repo.

| Report | SHA-256 (first 12) | Shape |
| --- | --- | --- |
| `rubric_test/screening_report_705.md` | `bfd69a1b4c0b` | 10-section |
| `rubric_test/screening_report_710.md` | `5a6a71d25ef2` | 10-section |
| `rubric_test/screening_report_711.md` | `6eeb778bf9c0` | 10-section |
| `rubric_test/screening_report_712.md` | `6eeb778bf9c0` | 10-section, byte-identical to 711 |
| `outputs/CASE_021/screening_report.md` | `6712638dd7e0` | 8-section narrative (shape control) |

## Results

| Report | A1 | A2 | A3 | A4 | A5 | A6 | total | verdict | findings |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| CASE_705 | 4 | 4 | 3 | 4 | 4 | 3 | 92.5 | pass | 2 |
| CASE_710 | 3 | 4 | 4 | 4 | 4 | 3 | 93.8 | pass | 2 |
| CASE_711 | 4 | 3 | 3 | 4 | 4 | 3 | 86.2 | pass | 3 |
| CASE_712 | 4 | 3 | 3 | 4 | 4 | 3 | 86.2 | pass | 3 |
| CASE_021 | 4 | 2 | 3 | 3 | 4 | 4 | 78.8 | revise | 2 |

No gate fired on any report. `python tools/dao.py check-forbidden-expressions`
returned `{"clean": true, "hits": []}` for all five (measured, not assumed); the
semantic pass found no unhedged assertion either — the four `rubric_test`
reports state repeatedly that they do not decide the questions they raise, and
CASE_021 carries an explicit P3 disclaimer in its assessment section.

## Which axes discriminated

- **A2 (25) discriminated most**, spanning 2–4. It separated a report that
  declares every grade-A variable's state (710) from one that leaves an A-grade
  variable to a review-point aside (711: 환자 자가응답 기왕력 appears only in
  §10 prose) from one that covers absent variables with a single blanket
  document-absence sentence (021).
- **A1, A3, A4, A6 each moved by one level** and each for a different reason —
  an untranslated internal string (710 A1), core diagnostic evidence resting on
  a B-tier record while an A/B-tier 영상판독지 sat unread (705 A3), a disease
  claim with no matching required-document set (021 A4), an unflagged
  status/period tension (705 A6).
- **A5 (15) returned 4 on every report and measured nothing here.** All five
  reports route issues to 손해사정사 / 의사 / 법률전문가 with concrete asks. That
  is a property of this corpus, not evidence the axis is redundant, but on this
  evidence A5 currently carries 15 points of weight that never move. Revisit if
  a second corpus behaves the same way.

## Determinism probe — inconclusive, by construction

711 and 712 are byte-identical and scored identically (same axis levels, same
86.2, same verdict). **This is not evidence of scorer determinism.** Both were
scored in one session by one scorer, and the second result reused the first's
axis judgements directly. A real probe needs two independent scoring passes with
no shared context. What the identical pair did confirm is narrower and still
worth having: the same input produced the same `target_report_sha256`, so the
binding between a score and the bytes it describes behaves.

## Anchor wording that proved ambiguous while scoring

1. **A1 vs an untranslated internal string.** 710 prints
   `the case holds no document of any kind this field routes to` six times in a
   Korean deliverable. Level 2 mentions "플레이스홀더·미완성 문장", level 3 covers a
   role that is only formally filled. Neither names a leaked internal string
   whose *content* is correct. Scored 3 with the reasoning recorded in the
   result; the anchor should name this case explicitly.
2. **A6 level 1 vs gate G4.** The A6 anchor for level 1 points at G4, but G4 is
   defined as one fact carrying two different values. 705's 현재 치료 상태
   '입원예정' against 입원기간 2023-12-04~07 is a tension between two *different*
   fields. Scored as an A6 deduction, not a gate. The rubric should say which
   side of that line related-but-distinct fields fall on.
3. **A4 on a claim the four case types do not cover.** CASE_021 is a disease
   benefit claim (뇌혈관질환진단비); the required-document sets in the hwpx are
   for 상해·배상 claims. Scored 3 as applicable, but a partial N/A may be the
   more honest record. Needs a decision before the rubric is used on a
   disease-claim corpus.

## Incidental defect found and fixed

`tools/dao.py` would not start on Python 3.14.7: argparse validates help strings
at `add_argument` time, and `--pages`'s help contains `81% of the`, which parses
as the `%o` conversion. `build_parser()` raised `ValueError: badly formed help
string` before any subcommand ran — every `dao.py` invocation, not just the
`--pages` path. Fixed by escaping to `81%%` (commit `575a7e0`). Discovered by
running the rubric's own G1 floor.

## Not measured

- Whether these scores agree with a human reviewer's. No human scoring pass has
  been run against this rubric.
- Whether the rubric transfers to a disease-claim corpus, or to the 7-section
  template shape — no report of that shape exists on disk to test with.
- Anything about report *accuracy*. This rubric compares a report to a rubric;
  ground-truth comparison remains the deferred isolated Unit 11 service's.
