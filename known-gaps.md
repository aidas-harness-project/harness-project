# Known Gaps

Findings from the 2026-07-12 pipeline/tooling review, tracked explicitly so
they don't get lost. Unlike `open-decisions.md` (deferred, waiting on the
user), most of these have a clear resolution -- they're TODO, not
undecided. Each entry: what's missing/broken, why it matters, what closes it.

## 1. Missing output schemas -- RESOLVED 2026-07-12

All 12 were written and validated (schema loads, cross-file `$ref`s resolve,
a realistic sample instance passes, and the conditional rules -- e.g.
"inconsistent requires a conflict_id", "rejected requires a note" -- were
checked to actually reject bad input, not just accept good input):

`coverage_result`, `case_type_result`, `requirement_matching_result`
(claim-analysis checkpoints 2-4), `normalized_policy_clause`
(policy-pipeline), `evidence_validation_result` (consistency-check),
`denial_validation_result`, `rebuttal_points` (denial-validation),
`draft_report_metadata` (draft-report), `critic_result` (critic),
`expert_review`, `evaluation_result`, `evaluation_summary` (evaluation).

Also patched `denial_reason_result.schema.json` (pre-existing) to add
`policy_matches` -- its own producing agent's spec (denial-response.md step
5) called for policy-clause matching that the schema had no field for.

**Two fields were deliberately made dev-phase-only, decided during this
pass, revisit once the harness is past PoC:**
- `evidence_validation_result.json`'s `checks` logs every field checked
  (consistent or not), not just findings -- full audit trail for now, may
  narrow to findings-only later.
- `denial_validation_result.json`'s `retrieved_chunk_ids` is required (the
  full retrieval set, not just cited evidence) -- may relax once retrieval
  quality is trusted.

**Update 2026-07-12:** the 8 affected agent specs (`claim-analysis`,
`policy-pipeline`, `consistency-check`, `denial-response`,
`denial-validation`, `draft-report`, `critic`, `evaluation`) were updated to
match these schemas and synced to `.codex/agents/*.toml` via
`tools/sync_agents.py`. `screening-report.md` and `document-pipeline.md`
were checked and don't need changes -- neither references a field these 12
schemas touch. `screening_report.schema.json` (pre-existing) was also
checked against the new `case_type_result` shape and doesn't need a change
-- its `case_summary.case_type` is already a free-form placeholder string,
explicitly marked pending real template rules.

**Still not done:** no agent has actually been run against any of these 12
schemas yet -- real-world shape mismatches (a field an agent naturally wants
to produce that the schema doesn't have, or vice versa) will only surface on
first use.

## 2. Live D1 near-miss sitting on disk -- CASE_002

`data/processed/CASE_002/DOC_002/*.md` (19 pages) and
`DOC_005/page_00{1,2,3}.md` were written before a document-pipeline subagent
run caught that DOC_002/DOC_003 (filenames looked like plain claim docs)
actually contain 손해사정서/보험금사정서 content -- including a stated
payout figure -- the answer-key document class this harness exists to
isolate. Intake's filename-only pattern match didn't catch it; an
orchestrator-agent "approved" the file in `_source_ledger.json`, which isn't
valid D2 human consent.

**Problem:** these processed-layer files may contain answer-key content and
haven't been quarantined or deleted. `_source_ledger.json` still shows both
files `approved`/`raw`.

**To resolve:** a human needs to (a) confirm/reject DOC_002 and DOC_003 as
ground truth, (b) decide whether to purge the already-written processed
pages, (c) decide whether this run is excluded from evaluation regardless of
outcome (D1), and (d) fix intake's classification so content, not just
filename, is checked before something lands in `raw`.

## 3. `tools/ocr_extract.py` -- two known bugs -- RESOLVED 2026-07-12

Both fixed directly in the tool, verified with a mocked-subprocess sanity
check (5 verdict-phrasing cases + the identical-text short-circuit + scratch
dir placement/cleanup, all passed):

- Sandbox `/tmp` access: page images now stage under a project-local
  `_ocr_scratch/` (gitignored, PID-tagged per run, cleaned up on exit) instead
  of system `/tmp` -- the nested `claude -p --allowedTools Read` call can only
  see files inside the project dir. Both `claude` subprocess calls
  (`transcribe_once`, `compare`) also now pin `cwd=ROOT` explicitly.
- `compare()` now does a word-boundary regex search for `DISAGREE`/`AGREE`
  instead of `verdict.upper().startswith("AGREE")` -- catches verdicts
  phrased as a full sentence, not just a bare leading token. A verdict
  matching neither now fails safe as `disagreed` (P8: no tolerance, never
  silently assume agreement) instead of silently passing or crashing the
  whole multi-page run.

`tools/_run_doc.dev.py` and `tools/_process_ocr_run.dev.py` (the workarounds)
are deleted -- the real tool no longer needs them. `_ocr_scratch_dev/` was
deliberately **left in place** -- its contents (`ocr_DOC_002.json`,
`ocr_DOC_005.json`) are forensic evidence for item 2's still-open CASE_002
incident, not cleanup debt; don't delete it as part of closing this item.

## 6. Full tool audit against the new schemas -- RESOLVED 2026-07-12

Went through every tool an agent invokes (`dao.py`, `document_assembly.py`,
`intake_case.py`, `validate_output.py`, `_validation.py`, `ocr_extract.py`)
checking existence, structure, and match against the 12 new schemas. Found
and fixed three more real bugs beyond item 3's two:

- **`document_assembly.py` bypassed the DAO entirely.** It wrote
  `outputs/CASE_XXX/*.md` and `*.evidence.json` straight to disk -- no lock,
  no atomic write, no schema validation -- despite
  `evidence_sidecar.schema.json`'s own description saying the sidecar is
  "generated entirely by the document-assembly tool" (implying it should be
  a real, validated DAO write). Fixed: now takes `--held-by`/`--run-id`,
  acquires the same lock file `dao.py check-lock` reads, writes both files
  atomically via `dao.py`'s own `atomic_write_text`/`atomic_write_json`
  (imported directly, same pattern `intake_case.py` already uses), and
  schema-validates the sidecar before either file touches disk.
- **`_validation.py`'s `schema_name_for()` couldn't resolve any
  `*.evidence.json` sidecar.** `Path.stem` only strips one suffix, so
  `draft_report_v1.evidence.json` -> stem `draft_report_v1.evidence`, and
  the `_v\d+$`-stripping regex never matches it. Every sidecar file has been
  silently unvalidatable via `validate_output.py` since the schema was
  introduced -- always reported `SKIP`, never `PASS` or `FAIL`. Fixed with a
  `.evidence.json` special case.
- **`document_assembly.py`'s `render()` wrote `"page": null`** for any
  citation whose `evidence_reference` omitted `page` -- `evidence_sidecar
  .schema.json`'s `page` is integer-typed with no `null` option, so this
  would have failed the validation just added above on the very first real
  citation without a page number. Fixed: omit the key entirely when absent,
  don't write it as `null`.

All three were caught by writing and running actual smoke tests (mocked
`claude` subprocess for the OCR comparisons, real end-to-end
`document_assembly.py` runs for the rest), not just by reading the code --
worth remembering given item 4 (no test suite) below.

**Also noted here, resolved as item 7 below:** `document_manifest.json`'s
read-modify-write race, and the broader locking gap it turned out to be a
symptom of.

## 7. `dao.py` locking gap: 4 read-modify-write subcommands had none, and every lock failed fast instead of waiting -- RESOLVED 2026-07-12

Found while explaining item 6's `document_manifest.json` note in more
depth: `grep -n "acquire_lock" tools/dao.py` showed only 3 of the DAO's
write paths (`write-contract`, `write-page-text`, `write-redacted-text`)
ever touched the lock mechanism at all. `add-conflict-entry`,
`set-conflict-verdict`, `update-run-state` (and `snapshot-backup`, which
calls it), and `set-ledger-status` did their read-modify-write with **zero
locking** -- not even the partial write-only protection the three locked
paths had. This directly contradicted CLAUDE.md's claim that P5 ("lock
before writing") is structurally enforced by the DAO -- for these four
files, it wasn't enforced at all. Concretely exploitable: `add-conflict
-entry` derives its next id from `len(ledger["conflicts"])`, so two
concurrent unlocked calls could both read the same length and both mint
`CONFLICT_1`.

**Fixed, two parts:**

1. All four now hold the lock across their *entire* read+modify+write
   (`--held-by`/`--run-id` added to their CLI args where missing), not just
   the final write -- closes the unlocked read-then-clobber race and the
   id-collision case above.
2. Every lock acquisition in `dao.py` (all 7 write paths now, plus
   `document_assembly.py` and `intake_case.py`, which import the same
   primitive) switched from fail-fast to **wait-until-clear** --
   `acquire_lock_blocking()`, P5's already-documented 30s-interval/15min-cap
   poll loop, now implemented by the DAO itself instead of left to the
   calling agent. Slower under contention, on purpose: a request now queues
   behind a held lock rather than immediately failing, so by the time it
   proceeds the state it reads is guaranteed fresh -- nothing else could
   have written while it waited. `LOCK_POLL_INTERVAL_SECONDS`/
   `LOCK_MAX_WAIT_SECONDS` are module-level constants (not bound into
   function defaults) specifically so tests can monkeypatch them to near-zero
   instead of a test suite actually waiting 15 minutes to see a timeout.

**Scope boundary, stated plainly (this does NOT fully close
`document_manifest.json`'s original staleness risk):** the fix above
guarantees freshness for the DAO's own atomic read-modify-write
subcommands. `write-contract` -- the generic path `document_manifest.json`
actually goes through -- still has its read happen *outside* the DAO, via a
separate earlier `read-contract` call the calling agent makes before
constructing what it hands to `write-contract`. Waiting for the lock before
writing now prevents write/write corruption on that path, but it can't
retroactively fix a read that already happened before the wait began. Fully
closing that would need a dedicated atomic patch subcommand (e.g. "update
this one document's fields in `document_manifest.json`"), which doesn't
exist yet and wasn't built in this pass -- not a live bug today (one
sequential `document-pipeline` writer per case), same as originally noted,
but the residual gap is real and distinct from what got fixed here.

Regression tests: `acquire_lock_blocking` waits-then-succeeds and
waits-then-times-out, `add-conflict-entry`/`set-ledger-status` staying
locked (and unmodified) while contended. See item 4 below.

## 4. No automated test suite -- RESOLVED 2026-07-12

`tests/` now exists, 56 tests, all passing (`pytest` from repo root, no
config needed -- `tests/conftest.py` puts `tools/` on `sys.path`):

- `test_dao_locking.py` -- write-contract's lock acquire/release, the
  atomic-write-then-validate-fail rollback this item named explicitly (a
  schema-invalid write leaves nothing on disk and no stale lock), and (added
  for item 7) `acquire_lock_blocking`'s wait-then-succeed /
  wait-then-timeout behavior plus the newly-locked
  `add-conflict-entry`/`set-ledger-status` staying untouched while contended.
- `test_dao_conflict_ledger.py` -- sequential `CONFLICT_N` ids,
  `check_conflicts_clear` blocking on `pending`, verdict resolution never
  discarding a source (P6).
- `test_dao_run_state.py` -- `get_last_passed_stage`, `attempt_count`
  incrementing per retry without resetting `started_at`, failed stages not
  counting as passed.
- `test_dao_source_ledger.py` -- approved/rejected requiring
  reviewer/reason, and D2's "one rejected file blocks the whole case" rule.
- `test_dao_evidence_tags.py` -- orphaned-tag/unused-citation detection.
- `test_document_assembly.py` / `test_validation.py` / `test_ocr_extract.py`
  -- regression coverage for item 6's three bugs (DAO-bypass +
  lock/atomic/validate, the `*.evidence.json` schema-resolution bug, the
  `page: null` bug) plus `ocr_extract.py`'s `compare()`/`scratch_dir` fixes
  from item 3, all via a mocked `claude` subprocess -- no real CLI calls.

None of this touches the real `outputs/`/`data/` trees -- every filesystem
test runs against a `tmp_path`, with `dao.py`'s `OUTPUTS`/`DATA` and
`document_assembly.py`'s `ROOT` monkeypatched per test. Schema validation
tests run against the real `schemas/` dir, since that's the actual contract
being tested, not a fake one.

**Not covered yet:** `intake_case.py` (the DOC_XXX/GT_XXX rename +
manifest-write path) and `sync_agents.py`. Lower priority -- neither has a
proven-bug history the way the tools above did this session.

## 5. Frontend (`frontend/`) unreviewed

Web (Vite/React) + backend (`main.py`) exist and were actually used to drive
the CASE_002/CASE_009 test runs (that's where `_run_logs/` came from), so
it's functional enough to matter. Not reviewed for code quality or
completeness in this pass.

**To resolve:** a dedicated pass once the schema gap (item 1) stops blocking
real end-to-end runs -- reviewing a frontend against a pipeline that can't
finish yet has limited value.
