# Screening rubric — engineering review items (local only)

The unsettled points in `screening_rubric.v0.1` whose decision is engineering's,
split out of the reviewer-facing document (`docs/스크리닝 루브릭 검토 필요 항목.md`,
published) so that document only carries what a 손해사정사 or a 의사 can decide.

Not published. 2026-08-21.

---

## D1 · An untranslated internal string has no anchor level

CASE_710 prints `the case holds no document of any kind this field routes to`
six times in a Korean deliverable. The content is correct; only the language is
wrong. A1's level 2 names "플레이스홀더·미완성 문장" and level 3 names a role that
is only formally filled — neither describes a leaked internal string whose
meaning is right.

**Currently:** scored 3 with the reasoning recorded in the result JSON.
**To decide:** whether a rendering/localisation defect belongs on A1 at all, or
should be a separate deterministic check (a non-Korean-line scan) that never
competes with the structural anchors.

## D2 · The conflict section split is probably a renderer problem, not a rubric one

Three of five reports show the same shape: the conflict section reads
`검증된 충돌 없음` while the review-guidance section raises inconsistencies the
report itself found. The body explains the distinction (ledger-registered vs
self-identified), so it is not a contradiction — but a reader who stops at the
conflict section never learns the inconsistency exists.

Repeating in three independent reports makes this a property of the renderer,
which never merges the two conflict classes into one section, not a per-report
mistake.

**Currently:** scored as an A6 deduction on each report.
**To decide:** whether to change the renderer so self-identified conflicts appear
in the conflict section (with their ledger status), and if so, drop the A6
deduction — the rubric would be penalising reports for a structure they do not
control.

## D3 · A6's lowest level and gate G4 have no boundary

G4 is defined as one fact carrying two different values. CASE_705's
`현재 치료 상태: 입원예정` against `입원기간: 2023-12-04 ~ 2023-12-07` is a tension
between two *different* fields whose values imply different states.

**Currently:** scored as an A6 deduction, not a gate, with the call recorded.
**To decide:** state in the rubric which side related-but-distinct fields fall on.
As written, two scorers can legitimately reach `revise` and `blocked` on the same
report.

## D4 · Absence-declaration phrasing: whitelist or semantic judgement

The silence-vs-declaration rule accepts an explicit scoped absence. The rubric
lists five phrasings the current reports use and leaves anything else to the
scorer.

**Currently:** examples plus judgement.
**To decide:** a fixed whitelist raises reproducibility and unfairly penalises a
new phrasing the moment the renderer changes wording. A semantic test keeps
fairness and costs reproducibility. Pick one, or define a normalisation step.

## D5 · G1's semantic pass is unverified

`dao.py check-forbidden-expressions` is a deterministic literal floor and it ran
clean on all five reports. The semantic half — an unhedged paraphrase of a
forbidden assertion — is scorer judgement, and nothing has tested whether two
scorers agree on it.

**Currently:** documented as required, unmeasured.
**To decide:** whether the semantic pass needs its own fixture set (known-bad
paraphrases the scorer must catch) before the rubric is trusted on gate outcomes.

## D6 · `templates/registry.json` does not match any report on disk

The `screening_report` entry pins seven headings with
`allow_extra_sections: false` (`^4\. 문서 간 불일치`, `^5\. 추가 필요 서류`,
`^7\. 1차 판단`). `outputs/CASE_021/screening_report.md` has eight sections with
different titles; the four `rubric_test/` reports have ten. Both read 2026-08-21.

This is why A1 scores roles rather than heading literals — a rubric keyed to the
registry would fail every real report.

**To decide:** whether the registry or the renderer is the stale one. Separate
defect from the rubric; it needs an owner either way.

---

## Shared with the reviewer-facing document

Three items are joint and stay in the published document because a
손해사정사 has to be in the decision:

- **A5 discriminated nothing** (5/5 scored 4) — anchor rewrite or weight
  redistribution is an engineering change, but whether the axis is measuring the
  right thing is not.
- **A2's weight of 25** — set by judgement, not measurement.
- **Fixing the expected role set per report shape** — currently "a role the shape
  lacks costs nothing," which structurally favours shorter shapes.

## Not a decision, just unmeasured

- No human has scored a report with this rubric; agreement is unknown.
- Scorer reproducibility is unverified. The byte-identical 711/712 pair scored
  identically, but the second reused the first's judgements in one session — that
  is not evidence. A real probe needs two context-isolated scoring passes.
