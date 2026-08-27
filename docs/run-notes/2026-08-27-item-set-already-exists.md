# The item set the rubric leaves open already exists upstream — 2026-08-27

Checked after the reproducibility note flagged the F1/F3 denominators as the
scorer's choice. They do not have to be: the generation pipeline declares both.

## What the artifacts declare

`claim_analysis_result.json`, in all four imported cases:

| | |
| --- | --- |
| `claim_facts` | **56 field_ids, identical set in all four cases** (`common=56, union=56`) |
| `priority_grade` | **A: 12, B: 44** — same split in all four |
| `resolution_status` per fact | `asserted` / `unavailable` / `conflict` / `explicitly_absent` |
| `required_document_checklist` | **14 document kinds**, each with `status` and `required_for_case_types` |

The A-grade twelve:

```
comparative_negligence_rate      current_treatment_status
diagnosis_certificate_anchor     disability_related_diagnosis
disability_type                  final_clinical_course
legal_basis_cited                liability_opinion_conclusion
major_treatment_category         patient_reported_prior_same_site_history
primary_diagnosis                surgery_or_major_procedure_status
```

This is the same taxonomy the rubric took from the 핵심변수 document — the
pipeline had already encoded it. The rubric's hand-built A list (seven items)
and this one are close but not equal, and where they differ the difference is
substantive: the pipeline grades `comparative_negligence_rate`,
`legal_basis_cited` and `liability_opinion_conclusion` as **A**, while the rubric
classes 과실률 and 적용 법조 as **discretionary** and keeps them out of the
headline. Both positions are defensible; they cannot both be the rule.

`required_document_checklist` carries its own answer to F3's denominator, and
per case type — the same mapping the 유형별 필요서류 document defines. Note what
is *not* among the fourteen: **사고경위서**. The finding raised separately in both
scored cases is now visible at the definition level rather than inferred from a
report.

## Where it is defined: not in this repository

Grepped the working tree and `HEAD` for the field ids and for `claim_facts` /
`priority_grade`: the only hit outside `outputs/` and `rubric_test/` is
`tools/generate_loss_adjustment_corpus_metrics.py`, which is unrelated.

Validating the imported contracts against this checkout's schemas:

```
SKIP outputs/CASE_705/claim_analysis_result.json      -- no matching schema
SKIP outputs/CASE_705/screening_report_judgement.json -- no matching schema
FAIL outputs/CASE_705/screening_report.json (screening_report.schema.json)
     - insurer_position/acceptance: 'evidence_references' was unexpected
     - preliminary_assessment/difficulty:  'not_assessed' not in [high, medium, low]
     - preliminary_assessment/feasibility: 'not_assessed' not in [high, medium, low]
```

So the cases were produced by a **newer pipeline than this repository holds**.
This checkout has `extracted_claim_fields.schema.json` (the older "claim fact
card"); the environment that produced these cases writes `claim_analysis_result`
with a 56-field graded universe, a 14-kind checklist, a
`screening_report_judgement` artifact, and a `screening_report` shape this repo's
schema rejects on three points. The ten-section report layout that did not match
`templates/registry.json` is the same gap seen from another angle.

## What this changes for the rubric

1. **F1's denominator can be rule-derived**: the 56 field ids, or the A-grade
   twelve plus any B-grade field the answer key states. Either is a rule; the
   hand-picked 11 and 13 used in the two scored cases were not.
2. **F3's denominator likewise**: the 14 kinds filtered by the case's determined
   type — which also makes the 사고경위서 gap a checklist change rather than a
   per-report finding.
3. **One conflict has to be settled**, not averaged: `comparative_negligence_rate`
   and `legal_basis_cited` are grade A upstream and discretionary in the rubric.
   If the rubric adopts the upstream grades, both re-enter the headline and both
   scored cases drop (each was `missing_in_screening` or `mismatch` there).
4. **Binding to the artifacts is not the same as binding to the code.** Deriving
   the denominator from `claim_analysis_result.json` works today but silently
   follows whatever the upstream pipeline emits. Pinning it needs that
   pipeline's field config in this repository, with a schema — which does not
   exist here yet.

## Not established

Whether the upstream 56-field list is itself config-driven or hard-coded, and
whether it is stable across upstream versions. Nothing in this repository can
answer that; the four imported cases agreeing only shows they came from one
upstream version.

---

# Re-scored on the bound denominators — 2026-08-27

Both cases re-scored with the field universe, grades and core set read from
`claim_analysis_routing_v0.1.json` instead of assembled by the scorer. Both
results validate and are stored.

| | CASE_705 | CASE_711 |
| --- | --- | --- |
| F1 (was) | 66.7 (83.3) | 40.0 (53.8) |
| F2 | 50.0 (unchanged) | 37.5 (unchanged) |
| F3 (was) | 75.0 (60.0) | 75.0 (60.0) |
| **total (was)** | **63.4** (68.7) | **46.2** (50.1) |
| **verdict (was)** | **divergent** (partial) | **divergent** (divergent) |

Both verdicts are now `divergent`, and in both cases it is a **core-field
mismatch** that pins them, not the score:

- CASE_705 — `legal_basis_cited`: 민법 제758조 제1항 against the key's 제750조·제755조.
  Grade **A** on the legal axis and `critical_conflict_field: true`. Under the
  hand-built rubric this sat in "discretionary", excluded from the headline, and
  the case scored `partial`.
- CASE_711 — `accident_mechanism`: the two preserved narratives against the key's
  나무 구덩이. Also `critical_conflict_field: true`.

## By grade

| | A | B | C |
| --- | --- | --- | --- |
| CASE_705 | 2/6 | 6/7 | 2/2 |
| CASE_711 | 2/6 | 4/7 | 0/2 |

The pattern the old scoring could not show: **both reports agree with the
adjuster on grade-B clinical detail and disagree on grade-A**. Six of the twelve
grade-A rows across the two cases are the liability triad
(`legal_basis_cited`, `comparative_negligence_rate`,
`liability_opinion_conclusion`), which the hand-built rubric had classed as
discretionary and kept out of the headline entirely.

## What moved and why

- **F1 fell** on both. Adopting the config's grades pulled 과실비율 / 적용 법조 /
  배상책임 성립 여부 into the denominator; all three are absent or divergent in
  both reports.
- **F3 rose** on both, 60.0 → 75.0. The denominator is now
  `required_documents_by_case_type` in the config's own `document_kinds`, and
  사고경위서 — which the old scoring counted as a `miss` against the report — is
  not a kind the config has. It moved to `out_of_universe_items`, where it is
  recorded as a gap in the configuration rather than charged to the report.
- **Upstream absence reasons came out uniform**: every absence in both cases is
  `not_mentioned` (3 in CASE_705, 9 in CASE_711). None is
  `source_document_missing` or `printed_but_blank`. So the nine-value split did
  not discriminate here — worth knowing before treating it as an improvement.

## Honest limits of this run

- **F2's denominator is still the scorer's.** The config says nothing about which
  issues an answer key raises, so issue recall stays a hand-enumerated 8 in both
  cases and is the one dimension the binding did not fix.
- **One row is scored against the report's correct behaviour.**
  `liability_opinion_conclusion` is a `legal_opinion` field, and preserving two
  opposing 법률의견서 without choosing is what the asymmetry rule asks for — yet
  the report carries no adopted value, so it counts as `missing_in_screening`.
  The rubric now penalises, in F1, the same behaviour it credits in F4. That is
  a real defect in the binding, not in the reports.
