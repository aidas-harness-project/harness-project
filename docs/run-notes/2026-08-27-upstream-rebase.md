# Rebased onto origin/main, and what the merged schemas say — 2026-08-27

## Branch state

| Ref | Commit |
| --- | --- |
| local `main` | `58f4f2e` — still 129 behind (`0 129`); it is checked out in the primary worktree, so it has to be pulled there |
| `origin/main` | `285befb` (re-fetched; unchanged) |
| `worktree-screening-rubric` | rebased onto `origin/main`, linear, 23 commits replayed |

Both integrations were tried. `git merge origin/main` produced **no conflicts**,
and `git rebase --onto origin/main 58f4f2e` replayed all 23 commits with **no
conflicts either**. The rebase was kept for the linear history. Backups:
`backup/screening-rubric-premerge` (`595963b`, pre-integration) and
`backup/merge-approach` (`4a3147a`, the merge version).

83 tests across the six affected suites pass on the rebased tree, and the gate
still answers on the real corpus.

## The schemas, re-checked on the rebased tree

Contracts that had no schema before the integration now validate:

```
PASS outputs/CASE_705/claim_analysis_result.json      (claim_analysis_result.schema.json)
PASS outputs/CASE_705/screening_report_judgement.json (screening_report_judgement.schema.json)
```

`claim_analysis_result.schema.json` → `$defs.field_result` is the vocabulary the
fidelity rubric should bind to, and it is richer than the rubric's own:

| Field | Values |
| --- | --- |
| `priority_grade` | `A` / `B` |
| `authority` | `source_document_extraction` / `claim_analysis_native` / `legal_opinion_assertion` |
| `resolution_status` | `asserted` / `explicitly_absent` / `unavailable` / `not_applicable` / `conflict` |
| `stop_reason` | `trusted_value_found` / `sources_exhausted` / `conflict_found` / `not_applicable` / `explicitly_absent` / `partial_value_only` / `printed_but_blank_only` |
| `unavailable_reason` | `not_mentioned` / `source_document_missing` / `unreadable` / `outside_poc_scope` / `route_not_activated` / `not_scheduled` / `printed_but_blank` / `conflict_unresolved` / `partial_reading_only` |

The rubric's hand-built `declared` / `silent` split is a two-value approximation
of `unavailable_reason`. Binding to it would separate cases the rubric currently
scores identically — a value absent because the document kind is missing
(`source_document_missing`) is a different failure from one the document had but
left blank (`printed_but_blank`), and neither is `not_mentioned`.

`config/claim_analysis/claim_analysis_routing_v0.1.json` carries the item set:
64 field entries with `medical_advisory_grade` (A/B/C), `extraction_wave`,
`value_shape`, `source_route_id`, `critical_conflict_field`, plus
`required_documents_by_case_type` and `case_type_rules`.

Note the two grade systems are not the same axis: the config's
`medical_advisory_grade` is the 핵심변수 A/B/C priority, while the result's
`priority_grade` is A/B and matches `extraction_wave`. A rubric binding has to
name which one it means.

## `screening_report.json` "failing" its schema — resolved, not a defect

`validate_output.py` reports three errors on it (`not_assessed` for
feasibility/difficulty, an unexpected `evidence_references`). The cause is the
validator's filename convention, not the artifact:

- `tools/run_screening_report.py` writes the file as `screening_report.json` but
  validates it against `SCHEMA = "screening_report_selective.schema.json"`.
- `validate_output.py` derives the schema from the *filename*, so it picks
  `screening_report.schema.json` — the legacy shape.

Checked directly: the artifact **PASSes** `screening_report_selective.schema.json`
and fails the legacy one. The write was validated; it is the standalone checker
that reaches for the other schema. Consequence to know about:
`validate_output.py --all outputs/CASE_X` will always report a failure on a
selective-lane case's screening report.

## The Python 3.14 fix, in full

**The bug.** `argparse` treats a `help=` string as a %-format template — that is
how `%(default)s` works. `dao.py`'s `--pages` help contained the literal text
`81% of the`. In %-formatting, `%` starts a conversion: here `% o` reads as the
octal conversion with a space flag, so argparse tried to format its parameter
dict as an octal integer.

**Why it only appeared on 3.14.** Older Pythons expanded the help string lazily —
only when help was actually printed, which nothing did. Python 3.14 added
`_check_help()`, which expands it eagerly inside `add_argument()`. Reproduced
here on 3.14.7:

```
$ p.add_argument("--pages", help="the bundles were 81% of the stage's input")
ValueError: badly formed help string
```

`dao.py` builds its whole parser at startup, so the failure was not scoped to
`--pages` or to help output: **every `dao.py` subcommand died before parsing
anything**, and every test shelling out to the CLI failed with it.

**The fix** is one character doubled — `81%%` — which %-formatting renders as a
literal `%`:

```
$ p.add_argument("--pages", help="the bundles were 81%% of the stage's input")
--pages PAGES  the bundles were 81% of the stage's input
```

The rendered help is identical, and the change is inert on older interpreters,
so it is safe to merge regardless of which Python a machine runs.

**Why it matters for the decision.** `origin/main` does not have it, so on
Python 3.14 the DAO CLI cannot start there at all. Measured in this environment:
`origin/main` 69 failed / 3688 passed; this branch 53 failed / 3833 passed — the
16-test difference is that fix. It has been written three times independently and
merged none of them: `a4e1a8c` (feature/openrouter-lane), `2bd4a07`
(port/d1-ground-truth-retirement), `575a7e0` (this branch).
