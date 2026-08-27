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
