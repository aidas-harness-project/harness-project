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

## 2. Live D1 near-miss -- CASE_002 -- RESOLVED 2026-07-13

`data/processed/CASE_002/DOC_002/*.md` (19 pages) and
`DOC_005/page_00{1,2,3}.md` were written before a document-pipeline subagent
run caught that DOC_002/DOC_003 (filenames looked like plain claim docs)
actually contain 손해사정서/보험금사정서 content -- an orchestrator-agent
had "approved" both in `_source_ledger.json`, which isn't valid D2 human
consent.

**Verified directly, not just taken on the prior investigation's word:**
- DOC_002: read in full (already-processed text, `data/processed/`, not a
  raw-file read). Confirmed: a completed loss-adjustment report by a
  licensed independent adjuster (바른결 손해사정, 김태윤, BD00001058),
  submitted to NH농협손해보험, stating a final payout determination of
  20,000,000원 (page 9).
- DOC_003: had never actually been OCR'd -- the original claim came from a
  prior agent's summary only. Ran the real dual-path OCR tool
  (`tools/ocr_extract.py`, 21 pages, output kept in job scratch, never
  written to `data/processed/`) to verify before deciding. Confirmed on an
  *agreed* (trustworthy) page: same firm, same adjuster, submitted to
  삼성화재, stating a 20,000,000원 payout determination (10M + 10M, page 4).
  3 of 21 pages disagreed under P8 -- moot, since the document is rejected
  regardless of OCR quality.

**Resolved:** both files rejected in `_source_ledger.json` with reviewer
`Dev` and a documented reason each (`set-ledger-status ... rejected`).
`check-source-ledger-clear CASE_002` now correctly returns `clear: false`,
listing both under `rejected` -- the case is structurally blocked from
proceeding until resolved further, which is D2 working as intended.

**Resolution, decided with the user 2026-07-13: re-run the case excluding
the answer-key-class files entirely, rather than building reclassify
tooling or leaving it blocked indefinitely.** Doing this surfaced a
bigger finding than expected:

- **2 more of CASE_002's files were also wrongly approved.** Re-intaking
  the same source folder as a fresh case (`CASE_020`) to exclude the 2
  already-rejected files ran the (now-existing) D2 content pre-check
  against the *other* 2 insurer-specific files for the first time --
  `DOC_001` (KB) and `DOC_004` (한화), both originally approved 2026-07-10
  by `orchestrator-agent` on filename pattern alone, before the content
  pre-check tool existed at all. Both flagged: same firm (바른결손해사정),
  same adjuster (김태윤, BD00001058), same "완료된 손해사정서 제출" pattern,
  stated payout figures (10,000,000원 / 20,000,000원 각각). **All 4 of
  CASE_002's insurer-specific submission documents turned out to be the
  same third-party adjuster's completed reports** -- only the 5th file
  (보험사 면책 공문, an insurer denial notice) was ever genuinely raw claim
  material.
- **CASE_002's ledger corrected to match**, not just left stale: `DOC_001`
  and `DOC_004` re-set to `rejected` via `dao.py set-ledger-status`, same
  evidence-based-reason discipline as the original 2 rejections (reviewer
  `Dev`). `check-source-ledger-clear CASE_002` now correctly shows all 4
  insurer files rejected, case still structurally blocked -- CASE_002
  itself is kept exactly as it was otherwise (not purged, not re-executed),
  serving as the historical incident record per the original decision not
  to touch it further.
- **`CASE_020` created as the actual go-forward case**: fresh intake from
  the same `source-cases/` folder via `intake_case.py --files "*면책 공문*"`,
  so the ledger only ever contained the 1 clean file -- no rejections to
  work around, no need for reclassify tooling. Content pre-check ran real
  and came back clear. `--execute` completed:
  `data/raw/CASE_020/DOC_001.pdf` is the case's only document. A very thin
  case (1 document, a denial letter, no claim substance beyond it), but a
  real, D1-clean one -- a legitimate downstream run would need to decide
  whether that's enough to actually process, separate from this item.
- **Evaluation exclusion (D1)** -- not formally recorded anywhere (no run
  is currently being evaluated), moot for CASE_002 since it can no longer
  proceed with meaningful content; the ledger block is the safeguard.
- **Item (d), intake's content-blind classification -- RESOLVED 2026-07-13.**
  `tools/intake_case.py` now runs a content pre-check on every `raw`-proposed
  PDF before writing the ledger (`scan_for_answer_key_content` -- one vision
  call over the document's first 5 pages, not a full read; document-pipeline
  still owns real OCR/P8). Design constraint discovered while building this:
  the case's PDFs have **zero embedded text layer** (confirmed directly --
  `fitz`'s `get_text()` returns empty on all four), so a cheap keyword-scan
  wasn't possible -- any content check has to be vision-based, which is why
  this couldn't be a free/instant fix and needed a real design call (made
  with the user: check first 3-5 pages, one call per file; flag rather than
  auto-reject, so a false positive doesn't lock out a legitimate file).
  A flagged file gets `content_warning` on its ledger entry
  (`source_ledger.schema.json` updated, v0.1 -> v0.2, to add the field);
  human review is still mandatory either way, this just makes the risk
  impossible to miss going in. Parsing fails safe toward `flagged=True` on
  an unparseable model response, same discipline as `ocr_extract.compare()`.
  Scope, deliberately narrow: PDFs only, `raw`-proposed files only (a file
  already headed for `ground_truth` isn't the risk this catches), and does
  NOT cover `--split`-derived files (reviewed via their page ranges instead).
  10 new tests (`tests/test_intake_content_scan.py`) cover the verdict
  parser and `build_ledger`'s wiring without needing a real PDF or `claude`
  call. `harness-guardrails-dev` D2 updated to describe this, synced to
  Codex/generic copies.

  **Would this have caught CASE_002?** All 4 of CASE_002's rejected files
  (not just DOC_002/DOC_003 -- DOC_001/DOC_004 too, confirmed above) had a
  giveaway title on page 1 (literally "보험금사정서") -- yes, a 5-page scan
  catches all four in practice, confirmed by actually running it against
  DOC_001/DOC_004 for real rather than assumed. A document that buries its
  conclusion beyond page 5 without an early giveaway would still slip
  through; this raises the bar, it doesn't make the check exhaustive.

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

**Residual gap -- RESOLVED 2026-07-13.** The fix above guaranteed freshness
for the DAO's own atomic read-modify-write subcommands, but left
`document_manifest.json` itself still going through `write-contract`'s
generic path -- read *outside* the DAO via a separate earlier
`read-contract` call, write only locked at the very end. Closed with a
dedicated atomic subcommand: `dao.py patch-manifest-document CASE_ID
DOC_ID --fields-file PATH --held-by NAME --run-id RUN_ID [--stage STAGE]`
(and the underlying `patch_manifest_document()` function, callable
in-process). It acquires the lock first, reads the manifest fresh under
that lock, merges only the given fields into the named document's entry
(leaving every other document and field untouched), validates the whole
file, and writes -- closing the exact gap the module docstring flagged.

`run_checkpoint1.py`'s two manifest-mutation sites (`_finish_checkpoint1`'s
success path, `_reset_manifest_for_blocked_ocr`'s blocked path -- the same
staleness bug fixed in item 12 lived in these exact two call sites)
switched to it, replacing their local read-then-`_write_contract` pattern.
`document-pipeline.md` updated to call `patch-manifest-document` for both
checkpoint 1's and checkpoint 2's manifest updates instead of implying
`read-contract`+`write-contract`; checkpoint 2's `redacted_text_path`
update was never actually instructed before this pass either (a smaller,
adjacent gap found while fixing this one) -- now is.

8 new tests (`tests/test_dao_manifest_patch.py`, 148 total), including one
that actually exercises the freshness guarantee: a background thread
modifies a sibling field while the main call is blocked waiting on the
lock, and the test confirms the eventual write preserves that concurrent
change rather than clobbering it with data read before the wait began --
the same shape of bug this item exists to prevent, reproduced and proven
fixed, not just asserted. Verified against real repo data too: ran the
new CLI subcommand for real (via subprocess) against a scratch copy of
`CASE_020`'s actual `document_manifest.json`, confirming the argparse
wiring works end-to-end, not just the function called directly in tests.

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

**RESOLVED 2026-07-13:** `intake_case.py` -- 25 new tests
(`tests/test_intake_case.py`) covering the previously-untested pure
helpers (`classify`, `parse_split_spec`, `split_output_name`,
`file_format_for`), `write_manifest`'s DAO-backed write (success +
schema-failure), and the `--execute` path end to end: DOC_XXX/GT_XXX
sequential renaming, `document_manifest.json` only ever containing raw
documents (never ground truth), the `_intake_record.json` crosswalk being
the sole place original filenames survive, and D2's "any rejected or
pending entry blocks the whole case" rule under `--execute`. `sync_agents.py`
-- 14 new tests (`tests/test_sync_agents.py`) covering `parse_frontmatter`,
`toml_escape` (including the adversarial case a body containing a literal
`"""` would otherwise break the TOML's own triple-quoted delimiters),
`sync_skills`, and `sync_agents` (default model, multi-file processing,
halting on a malformed source file before finishing the batch). 187 tests
total. Neither tool had a proven-bug history before this pass and none
was found while writing these -- this was pure coverage debt, not a live
gap, closed for completeness now that the higher-severity items above are
done.

## 5. Frontend (`frontend/`) -- RESOLVED 2026-07-13

Reviewed `frontend/backend/main.py` (309 lines) in full and the React
app's core logic (`api.js`, `App.jsx`, `pipelineDefinition.js`,
`statusLogic.js`, plus a skim of the components) -- ~2400 LOC total.

**Real bug found and fixed: the two human-review endpoints were broken,
always.** `set_ledger_status` (`POST /api/cases/{id}/ledger/status`) and
`set_conflict_verdict` (`POST /api/cases/{id}/conflicts/{id}/verdict`)
shelled out to `tools/dao.py set-ledger-status` / `set-conflict-verdict`
without `--held-by`/`--run-id` -- both became `required=True` when P5's
locking got closed for these subcommands (item 7, earlier this session),
but `main.py` was never updated to match. Confirmed by reproducing the
exact call: `python tools/dao.py set-ledger-status CASE_020 <file>
approved --reviewer x` exits 2 with argparse's "the following arguments
are required" error, which `_run_dao_cli` turns into an opaque HTTP 400.
**This meant the actual point of the review UI -- a human approving or
rejecting a ledger entry, or resolving a conflict -- could never
succeed through the frontend, for any case, ever.** Same class of gap as
item 8 (P7/D1 write paths that were also never exercisable), just in the
frontend instead of the DAO itself.

Fixed: added `_frontend_run_id()` (matches `_run_state.schema.json`'s
`run_id` pattern) and threaded `--held-by`/`--run-id` through
`_run_dao_cli`; `set_ledger_status` uses the reviewer's own name as
`held_by` (they're the human acting), `set_conflict_verdict` uses a fixed
`"frontend-reviewer"` (the request has no separate name field --
`LedgerPanel.jsx` already embeds the reviewer's name into the resolution
note text itself for conflicts, so this is lock metadata only, not a gap
in the audit trail). Verified for real, twice -- first attempt actually
shelled out to the real `tools/dao.py` against the real, committed
`CASE_020` and modified its ledger's `reviewed_by`/`reviewed_at` by
accident (a Python-level `monkeypatch` of `dao.OUTPUTS` has no effect on
a subprocess, which loads its own fresh copy of the module) -- caught via
`git diff`, reverted with `git checkout`. Redone safely with a throwaway
case (`CASE_997`, created and deleted within `outputs/`, never committed):
both endpoints now succeed end-to-end through the real subprocess path.

**Checked and ruled out as non-issues:** `oxlint` flagged an unused
`PHASE_2` import in `App.jsx` -- traced it; `Sidebar.jsx` is the component
that actually renders the stage list and correctly imports/maps both
`PHASE_1` and `PHASE_2`, so Phase 2 stages do render in the UI, this was
just a redundant import in a file that only needed `ALL_STAGES`. Also
flagged a `useEffect` exhaustive-deps warning in `StageDetail.jsx` --
false positive: `stageDef` comes from the static, module-level
`pipelineDefinition.js` data, so `stageDef.key` already fully determines
`stageDef.contracts`/`.report` for a given render; adding them as deps
would be redundant, not a fix.

**Design-level completeness note, not fixed (a scoping decision, not a
bug):** `denial-response` (dependency-triggered, not phase-gated per its
own agent spec) has no representation anywhere in the pipeline viewer --
absent from `pipelineDefinition.js`'s stage list and the design spec's
stage count alike. A user watching a case with a flagged insurer-response
document would see no status for this stage at all. Consistent with the
viewer's linear phase-list data model not fitting a dependency-triggered
stage naturally; would need an actual design decision (a separate
"triggered stages" section? inline under the triggering document?) before
building it, not a mechanical fix.

**Overall assessment:** the backend is well-structured and already
security-conscious going in -- real path-traversal guards on both upload
filenames and contract/report filenames (with commented rationale), CORS
scoped to the dev origins, a genuinely scoped `--allowedTools` allowlist
for launched runs (with its own residual-risk note already in the design
spec: Write/Edit aren't path-scoped by the flag). Every read goes through
`dao.py`'s own helpers (never reimplements file access), and every write
now correctly goes through `dao.py`'s CLI with proper lock metadata. The
one real bug found was a drift issue (the DAO evolved a required-argument
change that the frontend's one call site never picked up), not a design
flaw -- exactly the kind of thing an "unreviewed" surface accumulates
silently.

## 8. End-to-end pipeline audit -- 4 real blockers found, all RESOLVED 2026-07-13

A full re-check ("does this actually run end-to-end") after items 1-4, 6-7
were closed, prompted by nothing having ever run far enough to reach
stages 9-10 (every real test run so far stopped by stage 2-6). Found four
structural gaps that mechanical schema/lock fixes hadn't touched, all now
fixed and tested (25 new tests, `tests/test_dao_write_text.py`,
`tests/test_dao_human_review.py`, `tests/test_chunk_text.py`):

1. **`critic`'s `draft_report_v{version}_reviewed.md` had no write path at
   all** -- not JSON (`write-contract` doesn't fit), not section-assembled
   narrative content (`document_assembly.py` doesn't fit either). Fixed:
   `dao.py` gained a generic `write-text` (locked+atomic, unschema'd, for
   `outputs/`) and a narrow `write-reviewed-draft` wrapper built on it that
   critic actually calls, per the user's direction to build both, layered.
2. **No write path existed for `human_input_status`** (P7's tracked
   human-wait mechanism) or for creating `_human_review_complete.flag`
   (D1's actual evaluation gate) -- `evaluation` could never be legitimately
   unblocked, for any case, ever; nothing had ever exercised this far to
   notice. Fixed: `set-human-input-status` (generic) + `request-expert-review`
   (narrow wrapper, same layered pattern as #1) for the wait-tracking side;
   `mark-human-review-complete` for the gate, which (a) requires
   `expert_review_v{version}.json` to already exist and pass schema
   validation first -- you cannot claim review is complete without real
   recorded review content backing it, closing the same class of gap as the
   CASE_002 incident (item 2) at a different point in the pipeline -- and
   (b) requires an explicit `--reviewer` name, same accountability pattern
   as `set-ledger-status`. The flag is versioned
   (`_human_review_complete_v1.flag` / `_v2.flag`) so a stale v1 flag can't
   look valid during v2's later review; `read-ground-truth` now takes
   `--version` to check the matching one. `evaluation.md` rewritten to
   describe the real two-phase flow this revealed: writing
   `expert_review_v{version}.json` needs no ground truth at all (just
   `critic_result` + the human's live disposition) -- only the actual
   answer-key comparison does, so evaluation splits into a pre-gate phase
   and a post-gate phase. Also load-bearing: evaluation never calls
   `mark-human-review-complete` itself -- that's a genuine human action,
   same discipline as CASE_002's ledger rejections requiring a real
   reviewer name, not an agent self-certifying its own gate.
3. **`normalized_policy_clause.json` had no per-document filename or
   `document_id` field** -- a case with 2+ policy documents (very plausible;
   CASE_002 alone has up to 4 separate insurer policies) would have each
   `policy-pipeline` invocation silently overwrite the previous one's
   output. Worse than the other three: doesn't halt, just quietly destroys
   data. The schema's own description already said "one file per policy
   document" when written (item 1) -- this was a spec-to-schema wiring gap,
   not a fresh design question. Fixed: `normalized_policy_clause_{document_id}.json`,
   threaded through `policy-pipeline.md`, `claim-analysis.md`,
   `denial-response.md`. Also found and fixed while verifying this:
   `_validation.py`'s `schema_name_for()` didn't strip a `_DOC_\d+$` suffix
   either, so `validate_output.py` would have silently `SKIP`ped every one
   of these files (mirrors the exact `*.evidence.json` bug from item 6,
   just a different suffix pattern this time).
4. **No chunking tool for checkpoint 3** -- relied on the agent re-typing
   "verbatim" text itself, which the schema explicitly requires
   (`page_chunks.schema.json`: "not re-summarized") but nothing enforced.
   Building this surfaced a second, prerequisite gap: `redacted_text.md`
   had no page-boundary markers at all, so no deterministic tool could
   ever have recovered `page_start`/`page_end` from it regardless. Fixed
   both together: checkpoint 2 now assembles redaction output with a fixed
   `<<<PAGE page=N>>>` marker between pages, and a new `tools/chunk_text.py`
   (no LLM call -- pure string slicing on the markers) produces one chunk
   per page, guaranteeing byte-identical verbatim text structurally rather
   than by prompting instruction. Runs once per case across every document
   with a `redacted_text.md` (not once per document) since
   `page_chunks.json` is one combined file for the whole case, per its own
   schema -- `document-pipeline.md`'s opening framing updated to state this
   scope difference explicitly (checkpoints 1-2 are per-document, 3 is
   case-scoped).

**Not found to be a problem, checked and confirmed fine:** `draft_report_v1`/`v2`
and `critic_result_v1`/`v2` etc.'s versioned-filename fix from item 1 was
already correct — no new collision found there. `document_manifest.json`'s
read-modify-write scope boundary (item 7) is unchanged by this pass, still
open, still theoretical under the current single-writer-per-run design.

## 9. `ocr_result.json`/`classification_result.json`/`redaction_result.json` -- same silent-overwrite bug as `normalized_policy_clause.json`, wider blast radius -- RESOLVED 2026-07-13

Found by grepping every schema for a top-level `documents: [...]` array (the
shape that made `normalized_policy_clause.json`'s bug possible) and
checking which ones are written by a per-document stage. Four matched:
`document_manifest.json` (already known -- item 7, genuinely needs to stay
shared, multi-stage-owned) and three that didn't need to be shared at all:
`ocr_result.json`, `classification_result.json`, `redaction_result.json`.
`document-pipeline`'s checkpoints 1/2 run once per document; none of the
three had a per-document filename or a merge instruction, and
`write-contract` has no merge logic -- it overwrites whatever it's given.
Concretely: process DOC_001 -> `ocr_result.json` gets
`documents: [DOC_001]`. Process DOC_002 -> the file gets **overwritten**
with `documents: [DOC_002]`, silently destroying DOC_001's OCR record. Same
for the other two.

This is worse than `normalized_policy_clause.json`'s risk (only bites
cases with 2+ *policy* documents) -- **this bites every case with 2+
documents of any kind**, which is nearly all of them (CASE_002 and
CASE_009 each have 5). Nothing had caught it because no real run has yet
gotten far enough into checkpoint 1 across multiple documents in one
session to observe it.

**Fixed, one at a time, same treatment for all three (per-user
confirmation each round, not assumed):** renamed to
`ocr_result_{document_id}.json` / `classification_result_{document_id}.json`
/ `redaction_result_{document_id}.json`, and flattened each schema's
`documents: [single_entry]` wrapper away entirely (not just kept as a
length-1 array) -- `document_id` is now a top-level field in each,
matching the filename. `ocr_result.schema.json` v0.2->v0.3,
`classification_result.schema.json` v0.1->v0.2,
`redaction_result.schema.json` v0.1->v0.2. `document_manifest.schema.json`'s
doc-comment references to `ocr_result.json` updated to match.
`document-pipeline.md` updated for all three. 3 new regression tests in
`test_validation.py` confirming `schema_name_for()` resolves each new
suffix (same `_DOC_\d+$` stripping added for `normalized_policy_clause.json`
already covers these automatically -- confirmed, not assumed).

**Also flagged, RESOLVED same day:** `document_assembly.py` requires
`--held-by`/`--run-id` (fixed earlier). `document-pipeline.md` and
`critic.md` showed the literal CLI invocation with those flags;
`screening-report.md`, `draft-report.md`, `denial-validation.md` used to
just say "the document-assembly tool" abstractly. Not a hard blocker (an
agent can infer `--held-by=<its own name>` from context), but inconsistent
with the precedent set elsewhere -- all three now show the literal
`python tools/document_assembly.py --sections-file <spec.json> --held-by
<agent-name> --run-id RUN_ID` invocation, matching `document-pipeline.md`/
`critic.md`.

## 10. `tools/fork_case.py` added -- reuse expensive OCR/redaction work across branching test runs

Built to support testing the pipeline in pieces rather than one all-in-one
run: P10's `snapshot-backup` only versions `outputs/` (never `data/`), and
`case_id` is the primary key almost everywhere in the DAO (locks, ledgers,
run-state, conflict ledger) -- there's no run_id-scoped branching. A real
branch needs its own `case_id`. `case_id` is schema-pattern-locked to
`^CASE_[0-9]+$` (no letters/suffix), so a branch is just the next free
`CASE_NNN`, auto-assigned, with the actual fork relationship (source case,
step, label) recorded in `_fork_record.json` instead of the id itself.

Copies `outputs/` (case_id fields inside every JSON rewritten, then
re-validated against each file's own schema) and `data/processed/` by
default; `data/raw/` and `data/ground_truth/` are opt-in
(`--include-raw`/`--include-ground-truth` -- the latter prints a loud
warning, since it duplicates real answer-key material under a second
case_id). Can fork from current state or a specific P10 backup step
(`--from-step N`). Refuses to fork if any `.lock` file is present under the
source (mirrors P5's "don't poll, don't assume stale" discipline for a
lock found unexpectedly). The forked `_source_ledger.json` keeps the
source's approved/rejected statuses as-is, not reset to pending -- it's a
copy of already-reviewed content, not new raw input.

18 tests (`tests/test_fork_case.py`), plus a real smoke test against actual
repo data (forked `CASE_009` -> `CASE_010`, verified the ledger/run-state
case_id rewrite and schema validity for real, then cleaned up the
throwaway artifact).

**Found while verifying the real smoke test, not part of the tool itself:**
`schema_name_for()` never resolved `_source_ledger.json` / `_run_state.json`
/ `_conflict_ledger.json` -- their on-disk names carry a leading underscore
(the project's "shared state, not a component's own output" convention)
but their schema files don't. `validate_output.py` had been silently
`SKIP`ping all three, always, project-wide -- not something specific to
forking. Fixed with a leading-underscore strip in `schema_name_for()`.

**More serious, found by the same check -- RESOLVED 2026-07-13.** None of
`_source_ledger.json`/`_run_state.json`/`_conflict_ledger.json`'s own DAO
write paths (`cmd_set_ledger_status`, `_update_run_state`,
`cmd_add_conflict_entry`, `cmd_set_conflict_verdict`) ever called
`validate_instance()` -- confirmed by grepping every call site in `dao.py`;
the only two were `write-contract` (explicit `--schema-name`) and
`mark-human-review-complete`'s `expert_review.json` check. These three
files -- the D2 intake gate, the run-state resume mechanism, and the P6
conflict gate -- had **no schema enforcement anywhere**, at write time or
otherwise.

Fixed: added a shared `_schema_check()` helper (mirrors `write-contract`'s
own failure contract exactly -- print `FAIL` + the errors, don't persist,
return the function's existing failure sentinel: `1` for the ledger/
conflict-ledger commands, `None` for `_update_run_state`, matching what
each already returned on a lock failure) and wired it into all four
functions, validating the fully-modified structure right before the write.
No P4 self-correction-retry loop added -- these functions build their own
structures rather than accepting arbitrary agent-supplied content the way
`write-contract` does, so a failure here means a bug in this file's own
construction logic or a pre-existing malformed file, not bad agent output
to retry.

Verified this isn't just "nothing broke" (all 118 pre-existing tests still
passed unchanged, meaning existing fixtures were already valid -- that
alone doesn't prove the new checks do anything): added 3 adversarial tests
that seed genuinely schema-invalid state and confirm each function now
actually rejects it and writes nothing, rather than silently persisting
garbage. 121 tests total.

## 11. P8's `compare()` has a real blind spot: it catches conflicting facts, not fabricated additions -- found running a real document through checkpoint 1, RESOLVED 2026-07-13

Running `CASE_012`/DOC_001 (a real 4-page document) through checkpoint 1 for
real surfaced this directly. Page 3's two independent reads were marked
`agreed` by `compare()` -- but `reading_a` contained a fabricated appendix
after the real document content ended: English meta-commentary referencing
this project's own internal terminology (`D2`, `harness-guardrails-dev`),
telling the (simulated) downstream process how to route the document.
`reading_b` had no trace of it. Verified directly against the raw page
image (rendered at 250dpi): the actual page ends cleanly at
"KB손해보험주식회사" with nothing after -- the fabricated text does not
exist in the source document at all. It was hallucinated by whichever
`claude -p` call produced `reading_a`, in direct violation of
`ocr_extract.py`'s own transcription prompt ("Output ONLY the
transcription -- no commentary").

**Why `compare()` missed it:** its prompt asks whether the two readings
"materially agree -- same names, dates, numbers, diagnoses." That's a
check for *conflicting* core facts. It has no check for *extra* content
one reading has that the other doesn't -- a whole fabricated paragraph can
pass as "agreed" as long as it doesn't touch the specific fields being
compared. This is a real methodology gap in P8 as currently prompted, not
a one-off fluke: the exact same blind spot would let a hallucinated
addition slip through on any page, on any document, silently.

**What this could have meant if unnoticed:** `page_003.md` was already
written to the trusted processed layer with the fabricated content
attached (since "agreed" pages get written without further scrutiny) --
every downstream stage (`claim-analysis`, `screening-report`,
`draft-report`, etc.) would have read this as real document content. This
is exactly the P1 fabrication risk the whole harness exists to prevent,
and it came from the harness's own extraction tooling, not from a
malicious source document.

**Fixed for this one real occurrence:** re-verified against the raw page
image, corrected `page_003.md` to the clean `reading_b` content, recorded
the finding in `ocr_result_DOC_001.json`'s page-3 `cross_validation
.resolution` (the same field built for genuine disagreements -- broadened,
since this is a legitimate second use case: "agreed" but a human found a
problem `compare()` missed) and flagged `review_required: true` at the
document level with an explicit note.

**Fixed 2026-07-13: `COMPARE_PROMPT_TEMPLATE` now explicitly asks a second
question** -- not just "do the core facts conflict" but "does either
transcription contain content the other lacks entirely (extra paragraph,
appended commentary, meta-commentary about the transcription task itself)"
-- and instructs the model to treat any one-sided addition as a
disagreement even when no specific fact conflicts. Chose a stricter prompt
over a separate verification pass: no extra `claude` call per page, and
the existing DISAGREE/AGREE parsing path already handles it unchanged.

**Verified for real, not just by re-reading the prompt.** The original
fabricated `reading_a` text wasn't persisted verbatim anywhere (compare()
returned "agreed" at the time, so the disagreement-only raw-scratch save
never triggered) -- reconstructed a faithful analog from the resolution
note (clean page-3 text + an appended English meta-commentary block
referencing this project's own D2/harness-guardrails-dev routing
terminology, matching what was actually described) and ran the real, fixed
`compare()` against it via the actual `claude` CLI (no mocking). Result:
`DISAGREE`, correctly identifying the trailing block as a one-sided
addition absent from the other reading. Notably, the model's own verdict
text flagged the embedded "route per D2 guidance" instruction as
resembling a prompt-injection attempt and stated it was ignoring rather
than following it -- correct behavior on both counts, unprompted.

One new regression test (`test_compare_prompt_asks_about_one_sided_extraneous_content`)
locks the prompt's key phrasing so a future edit can't silently drop this
check. 140 tests total.

**Retroactive audit, PARTIAL 2026-07-13.** Only one reading was ever
persisted per "agreed" page (item 9's flattening kept the chosen text,
not both raw readings), so re-running the fixed `compare()` against the
original two readings isn't possible for historical pages -- the only
real audit method left is checking the stored text directly against the
raw page image, the same way the original page-3 finding was made.

- **CASE_012 pages 1, 2, 4 (DOC_001) -- audited, clean.** Rendered each
  raw page at 200dpi and read it directly against the stored
  `page_00N.md` text. All three verbatim-match the source with no
  extraneous content. This closes out CASE_012's exposure -- all 4 of its
  pages (3 plus 4's independently-resolved page) are now confirmed clean.
- **CASE_002 DOC_002 (19 pages) / DOC_005 (3 pages) -- deliberately NOT
  audited yet.** The case is D2-blocked (both files rejected as
  answer-key-class content, `check-source-ledger-clear` returns
  `clear: false`) -- structurally, nothing downstream can read this
  content while blocked, so there's no live P1 exposure to close right
  now the way CASE_012's was. Deferred rather than silently dropped:
  22 pages of manual audit against a case that can't proceed anyway is
  low-value compared to auditing content that's actually in the active
  pipeline. Revisit if/when item 2's disposition question is resolved and
  CASE_002 (or its raw files under a new case) is ever unblocked.

## 12. `tools/run_checkpoint1.py` and `tools/run_scenario_matrix.py` added

Built after manually running checkpoint 1 step-by-step (item 11's real run)
made clear how many separate commands that actually took. Two scripts,
composable:

- **`run_checkpoint1.py`** -- automates the mechanical sequence: real
  dual-path OCR (`ocr_extract.run_ocr`, called in-process now, not
  subprocess-of-a-subprocess -- `ocr_extract.py` was refactored to expose
  `run_ocr()` as a reusable function, pure extraction from `main()`, no
  behavior change, confirmed by the existing 11 `test_ocr_extract.py` tests
  still passing unchanged), write each agreed page, classify from page 1's
  transcribed text (one real `claude -p` call -- reasoning over text, not
  re-viewing the raw image, a smaller PII-exposure footprint than the
  original design), assemble + write `ocr_result_{doc_id}.json` +
  `classification_result_{doc_id}.json`, update `document_manifest.json`.
  Stops cold at a P8 disagreement -- resolving one is still a human
  decision, not something this script does on its own.

  Real gap found while building this, fixed before it shipped:
  `ocr_result.json` only retains `reading_b` (as `vision_model_reading`) --
  `reading_a`'s full text was never persisted anywhere. If a disagreement
  blocked the run and someone came back *later*, in a separate process, to
  resolve it, `reading_a` would already be gone, forcing a wasteful
  real-OCR re-run just to recover it. Fixed: the full dual-read data (both
  readings, every page) is now saved to `_ocr_scratch/{case_id}_{doc_id}
  _raw.json` (gitignored, not a schema-validated contract) whenever a
  disagreement blocks the run, so `resolve_from_raw_ocr()` can act on it
  later without repeating the expensive part.

- **`run_scenario_matrix.py`** -- built on top of `run_checkpoint1.py` and
  `fork_case.py`. Runs real OCR exactly once; if a real disagreement comes
  back, forks the blocked case three ways (`reading_a` / `reading_b` /
  left unresolved) and reports each branch's outcome. Deliberately scoped
  to this one gate, not literal all-combinations -- see the module
  docstring for why: this is the one decision point whose outcome depends
  on real, non-deterministic LLM output and is genuinely expensive to
  re-derive per branch. Every other gate (D2 approve/reject, P6
  resolved/false_positive, P4's three-way schema-failure handling) is
  structural DAO logic already covered exhaustively and cheaply by
  `tests/test_dao_*.py` -- forking real cases to re-prove that would just
  be a slower, costlier way to reach the same conclusion. Each forked
  branch's resolution note is explicitly marked as an automated scenario
  probe, not a genuine verified resolution (distinct from the real I67/
  I67.8 resolution in item 11's run) -- so nobody mistakes a scenario fork
  for a trustworthy case later.

16 new tests (138 total), all claude subprocess calls mocked -- no real
LLM cost in the test suite itself.

**Run for real, RESOLVED 2026-07-13.** Forked `CASE_012` (with `--include-raw`)
into `CASE_013` and ran `run_scenario_matrix.py` against it for real, twice.
Confirmed real, non-deterministic OCR variance between the two runs (first
run: pages 1/3/4 disagreed; second run, same document: only page 4
disagreed) -- genuine evidence this isn't a scripted/fixed test fixture.

**First real run found a genuine bug, not a mocking gap:** the `unresolved`
scenario's fork showed `document_manifest.json` with `ocr_status:
completed, stages: ['passed']` -- stale values inherited from `CASE_012`'s
earlier successful run. `run_checkpoint1()`'s blocked-disagreement path
wrote `ocr_result_{doc_id}.json` correctly but never touched
`document_manifest.json` or `_run_state.json` at all -- if a case
previously had `completed`/`passed` values (exactly this fork scenario,
but *not* fork-specific: the same staleness would hit any genuine
production re-run that newly fails after a prior success), those stale
values just sat there, directly contradicting the fresh `ocr_result.json`.
Fixed: `_reset_manifest_for_blocked_ocr()` now resets every field
checkpoint 1 owns (`ocr_status: failed`, `redacted_text_path`/
`document_type`/`classification_confidence`/etc. all nulled -- nothing
downstream should trust stale values after a fresh extraction failure) and
`_update_run_state(..., "failed", ...)` marks the run-state stage
correctly, both on the blocked path. 1 new regression test seeds exactly
this stale-prior-success scenario and confirms the reset (139 tests
total); a second real run against the same document then re-confirmed the
fix works on genuine non-deterministic data, not just the mocked test:
`CASE_019`'s (unresolved) manifest correctly showed
`ocr_status: failed, cross_validation_status: disagreed_pending_review,
redacted_text_path: null, document_type: null`.

Also confirmed for real: `reading_a`/`reading_b` forks (`CASE_017`/
`CASE_018`) produced genuinely different page 4 text (real branch
divergence, not a no-op), both `passed` with real classification
(`document_type: insurer_response`), and all 21 real schema-validatable
files across the three forks passed (`_fork_record.json` correctly `SKIP`s
-- it was never meant to be a schema-validated contract).

## 13. `frontend/backend/main.py` -- argument-injection via unvalidated positionals into a subprocess argv -- RESOLVED 2026-07-13

Found by an automated background security review of the commit that first
added `frontend/` (item 5). Two endpoints shell out to `tools/dao.py`'s
CLI via `_run_dao_cli` (`subprocess.run` with a list, not `shell=True` --
no shell-metacharacter risk, but a real argv-level one): `set_ledger_status`
placed `body.file_name` (a request-body field) as a bare positional
argument, and `set_conflict_verdict` placed `conflict_id` (a URL path
parameter) the same way. Neither was validated before use. Since
`subprocess.run([...])` passes each Python list element as exactly one
argv token (no shell word-splitting), the injection vector isn't "smuggle
multiple tokens from one string" -- it's narrower but still real: a value
that exactly matches one of `dao.py`'s own defined flags for that
subcommand (e.g. `file_name="--held-by"`, `conflict_id="--held-by"`) gets
consumed by argparse as that OPTION instead of the intended positional,
desyncing every argument after it (the next token becomes that flag's
value, the real positional goes unfilled, etc.).

Verified the exact attack before fixing anything, not just reasoned about
it in the abstract: called `set_ledger_status`/`set_conflict_verdict`
directly with `file_name="--held-by"` / `conflict_id="--held-by"` --
confirmed both reached `_run_dao_cli` and would have altered the argv
`dao.py` actually parses.

**Fixed with two validators, run before either endpoint's request body is
ever used to build argv:**
- `_valid_conflict_id` -- pattern match against `^CONFLICT_[0-9]+$`,
  mirroring `conflict_ledger.schema.json`'s own pattern (a real identity
  check, not just a leading-dash blocklist), rejecting on mismatch with a
  clean 400 rather than a deep `dao.py` error.
- `_known_ledger_file_name` -- `file_name` has no fixed pattern to check
  against (real filenames vary), so instead it must already be a real
  entry in the case's own `_source_ledger.json` before being used at all.
  This closes the injection path (a crafted `--held-by` value is never a
  real ledger entry) and gives a clear error for a genuine typo too,
  instead of `dao.py`'s own deeper `NOT_FOUND`.

15 new tests (`tests/test_frontend_main.py`, 202 total): both validators
directly (attack values rejected, legitimate values still pass), plus two
full-endpoint-level tests that call `set_ledger_status`/
`set_conflict_verdict` with the exact crafted payloads and confirm they
raise before `_run_dao_cli` is ever reached. Re-verified the same crafted
attacks are now blocked, and that the real, previously-approved `CASE_020`
filename and a real `CONFLICT_1`-shaped id both still pass -- no
regression on the legitimate path this same commit had just fixed (item 5).

## 14. First full end-to-end run (CASE_021) -- 3 gaps sealed, 1 scope question OPEN

CASE_021 (fresh intake from the 약관상 지급범위 source: raw = the 4-page
3-insurer denial pack, ground truth = the adjuster's 4 완성 손해사정서) was
the first case ever to traverse intake -> evaluation in one run
(RUN_20260714_001, all stages passed, 21/21 contracts schema-PASS, real P8
disagreement resolved by a delegated-human image review along the way).
The run surfaced four gaps; three sealed 2026-07-14:

- **Run-state stage-name drift -- SEALED.** `run_checkpoint1.py` wrote
  `document_processing` while the document-pipeline agent freely chose
  `document-pipeline`, and critic wrote both `critic` and `critic_v1` --
  one stage forked into parallel entries, breaking
  `get_last_passed_stage`'s resume logic and every stage_name consumer.
  Fixed structurally: `run_state.schema.json` v0.2 makes `stage_name` an
  enum of the 14 canonical names (conflict-ledger `raised_by_stage` now
  $refs the same enum -- one vocabulary, one drift surface), every agent
  spec pins its own canonical name, the orchestrator skill lists the full
  set, and CASE_021's run-state was repaired under lock. Regression test:
  a drifted name is rejected and nothing persists.
- **`extracted_claim_fields` too narrow + broken ad-hoc dates -- SEALED.**
  The run produced 6 real facts with no slot (imaging_date,
  claim_received_date, policy_contract_date, claim_item, disposition,
  insurers) which got smuggled through `warnings`, losing typed structure
  and evidence links. Worse: `additionalProperties` used `oneOf` over the
  three field shapes, but a YYYY-MM-DD string satisfies both `value_field`
  and `date_field` -- exactly-one matching made every ad-hoc date field
  structurally unvalidatable. Schema v0.2: named slots added, oneOf ->
  anyOf. And the root enabler: `validate_instance()` never passed a
  FormatChecker, so every `format: date` in every schema was decorative --
  a malformed date validated fine. Now enforced project-wide; full
  revalidation sweep of all real cases' outputs passed.
- **Redaction scope undefined -- SEALED.** CASE_012's real run redacted 0
  items from the same content CASE_021's redacted 4 (corporate hotline,
  addresses, CEO signatory name) -- neither was wrong against the spec,
  because the spec never said. document-pipeline.md now fixes the
  convention: all natural-person names regardless of capacity + all
  phone/address/policy-number values including published corporate contact
  info; corporate entity names stay.
- **Single-denial-pack under-scoping -- OPEN (user decision, not a code
  fix).** The pipeline only ever saw the 3 insurers present in the denial
  pack; ground truth shows a 4th policy (농협, 20,000,000원 payable) with
  no in-case denial notice -- invisible to every stage and only surfaced
  at evaluation. Real question: should intake of a multi-insurer case
  require a per-insurer completeness check (does every GT insurer have a
  corresponding raw-side document?), or is partial-scope processing
  acceptable with the asymmetry recorded at evaluation (what CASE_021
  did)? Deferred to the user; evaluation_result_v1.json records the
  asymmetry explicitly either way.

## 15. OCR subprocess inherited project context and editorialized -- two-sided fabrication that defeats item 11's check -- RESOLVED 2026-07-14 (tool), pages scrubbed

Found live during CASE_022's checkpoint 1 (the 기왕증 case, first run
after the template wrapper). `ocr_extract.py`'s `claude -p` calls run with
`cwd=ROOT`, so the transcription subprocess auto-loaded this project's
CLAUDE.md, skills, and session hooks -- and a context-aware reader
editorializes: on a document pack containing third-party adjuster
determinations, BOTH blind reads appended similar meta-commentary after
the true page end ("answer-key-class content per D2, check
_source_ledger.json...", one literally phrased "Flagging, not caveman" --
the session's hook text leaking into the subprocess). Because the
additions were two-sided and materially similar, `compare()` judged the
pages agreed and the fabricated blocks landed in the trusted processed
layer (pages 2/3/5) -- the exact escalation item 11's one-sided-addition
check cannot catch. Pages 4/6 disagreed for ordinary reasons and carried
the same flags in both readings.

Also notable: the subprocess's "flags" were *correct about the content*
(the pack does contain adjuster determinations) but wrong about the
situation -- the delegated D2 review had already examined those exact
pages and approved them as third-party precedent evidence. A transcriber
is not the place for classification judgment; that's what intake's
content pre-check and D2 human review are for.

**Fixed:**
- `ocr_extract.py`: both `claude -p` calls (`transcribe_once`, `compare`)
  now pass `--safe-mode` -- all customizations (CLAUDE.md, hooks, skills)
  disabled, auth untouched; the reader sees nothing but the page. Verified
  live before adopting (`--safe-mode` smoke test) and locked by a
  regression test asserting both subprocess argvs carry the flag.
- Pages 2/3/5 scrubbed under DAO write-page-text: flag blocks stripped,
  true page ends verified against 150dpi renders, scrub recorded in each
  page's `cross_validation.resolution` (the item-11 "agreed but human
  found a problem" use case). Pages 4/6 resolved normally (image-verified
  hand corrections, flags stripped) -- notes in ocr_result_DOC_001.json.

**Residual, deliberate:** prior cases' processed pages were NOT re-audited
for this pattern (CASE_012/013/020/021 predate the wrapper-era prompts and
their runs' readings never showed flag tails -- CASE_021's page-2 fabricated
addition was one-sided and caught; still, the retroactive-audit debt from
item 11 now covers two failure patterns instead of one).

**Item 14 addendum (2026-07-15, CASE_022):** second live instance of the
same under-scoping class, worse shape. The 기왕증 case's evidence pack
(교보/KB payment notices + 의료자문) supported a clean first-party
후유장해 analysis -- which GT_001.txt fully vindicates (50% -> 기왕증
10%p 공제 -> 40%, R01 confirmed, mechanism confirmed) -- but the actual
final report (GT_002, the 59p "배상 손해사정서") is a THIRD-PARTY
배상책임 quantum (지자체 계단 하자, 노동능력상실률 32%, 사정금액
89,952,400원) whose claim documents were never in the pack at all.
case_type scored correct=false through no reasoning error; the filename's
leading "배상" was the visible hint. Same user decision as the 농협
question, sharpened: intake needs either a completeness check against the
GT's claim scope, or an explicit acceptance that evaluation measures
"analysis of what was provided," not "prediction of the final report."

## 16. CASE_003 checkpoint 1 -- local model stack attempted then REMOVED; reader self-refusal + non-text documents -- PARTIALLY OPEN

**SUPERSEDED 2026-07-21 (PR #8 review): the local model stack was removed.**
Everything below about `local-ocr`/`local-vlm`/`local-llm`, `tools/local_runtime.py`,
and the Instruct-Q4 model tags is retained as the historical record of why. The
stack never reached real-content validation on a multi-document matrix and was
single-machine (Windows/E:) only, so it was deleted rather than left as
misleading scaffolding; a genuinely technology-independent reader (a real OCR
engine) is deferred to `open-decisions.md` #4. The two findings that OUTLIVE the
removal -- reader self-refusal (c) and the non-text-image document path (d) --
are still live and summarized at the end. The "Image: {path}" misread that made
the refusals look non-deterministic was separately root-caused and fixed; see
item 17.

Two separate problems surfaced while running CASE_003's document-pipeline
stage:

**(a) `local-vlm` (qwen3-vl:4b) and `local-llm` (qwen3:4b) both fail on
real content, confirmed live, not assumed.** The dispatched agent's
instruction was explicit same-provider weak-P8 (`claude-cli`/`claude-cli`,
per `harness-guardrails-dev`'s P8 fallback section), but the agent's stage
skill documents the fully-local `local-ocr`+`local-vlm` pair as the
*primary* recommendation, and `tools/local_runtime.py`'s preflight passed
-- so the agent judgment-called trying the stronger dual-technology path
first. Result: `local-vlm` returned `done_reason=length` with an **empty
transcription** on every real document page (reproduced across multiple
image sizes/token budgets -- it burns its generation budget on internal
reasoning and emits no transcription text), confirmed working on the
smoke-test image but not real pages. `local-llm` (checkpoint 2's default
redaction provider) failed the same way for a different reason: ~11,000
chars of reasoning prose instead of the required JSON, no parseable
output. **No case output was ever written from either local attempt** --
both errored at the tool level before any DAO write, so nothing needs to
be discarded; the agent self-corrected to `--reader-a claude-cli
--reader-b claude-cli --comparator claude-cli --classifier-provider
claude-cli` before writing anything. Confirms `open-decisions.md` #4's
"not yet validated" condition for the local pair is very much still open
-- this is a second, independent data point (beyond whatever motivated
the original weak-P8 fallback) that the currently-configured local models
are not fit for this document type yet.

**2026-07-16 update:** the runtime defaults were replaced with explicit
Instruct Q4 tags: `qwen3-vl:4b-instruct-q4_K_M` for vision and
`qwen3:4b-instruct-2507-q4_K_M` for comparison/classification. The vision
replacement fixed the empty-output failure, but the first real recheck still
timed out because the old `qwen3:4b` comparator remained. After replacing the
text model too, a scoped DOC_013 checkpoint-1 retry completed in 245.6 seconds:
one page agreed and local classification succeeded. This is a useful v1 data
point, not closure: the representative multi-document matrix and local
redaction validation remain open. The run also exposed and fixed a state bug:
one document's checkpoint-1 success had marked the whole
`document_processing` stage passed before redaction/chunking; checkpoint 1 now
keeps the stage `in_progress`, and the real run-state was corrected through the
DAO.

**(b) The dispatched background agent was killed mid-run by the
account-level API spend cap** ("You've hit your monthly spend limit"),
not by any harness logic. It had gotten as far as switching to the
claude-cli fallback and starting checkpoint 1 on real documents when it
was terminated. No `ocr_result_{doc_id}.json` had been confirmed written
before termination -- state as of writeup: `document_manifest.json` still
shows the pre-run `pending` skeleton for all 13 documents, only
`page_001.md` for DOC_001 shows a fresh (2026-07-14 22:29) timestamp,
meaning checkpoint 1 was mid-flight on the very first document when the
cap hit. This is an operational/infra constraint, not a pipeline design
gap -- flagged here because CASE_003 is left in a half-run state
(`_run_state.json` shows `document_processing: in_progress`, no
snapshot-backup exists yet since no stage has passed) and resuming needs
a human to either raise the spend limit or wait for the monthly reset
before re-dispatching.

**Not yet resolved:** whether to (1) wait out the cap and resume
CASE_003's document-pipeline stage from scratch (checkpoint 1 hadn't
completed even one document), or (2) investigate a genuinely-working
local-vlm model/config before the next attempt so the dev-phase weak-P8
fallback stops being the only working path. Both are user decisions, not
mechanical fixes.

**UPDATE 2026-07-15 (resumed run RUN_20260714_003):** the spend cap cleared
and the run was resumed with the claude-cli/claude-cli weak-P8 fallback.
Two things surfaced and were addressed:

- **(c) claude-cli reader self-refusal is a recurring P8 failure on real
  case documents, not a one-off.** Across DOC_005 (p4,p9), DOC_007 (p1),
  DOC_009 (p1), DOC_010 (p1,p2), DOC_013 (p1), one of the two
  claude-cli reads repeatedly *refused to transcribe* and emitted
  meta-commentary (e.g. "this looks like sensitive case data, what's the
  authorization?", or a fabricated preamble about "the git history in this
  repo"), while the OTHER read transcribed faithfully. compare() correctly
  flags this one-sided noise as a disagreement, so P8 hard-halts every time
  -- the gate is behaving correctly; the *reader* is the problem. `--safe-mode`
  (which strips CLAUDE.md/skills) is already applied and did NOT prevent it:
  the model self-censors from the raw document image + the neutral prompt's
  path hints alone, not from inherited repo context. Reintroducing defensive
  "this is sanctioned, do not refuse" framing is still forbidden (it made
  this worse before). This is the dev-phase weak-P8's real cost and a
  stronger argument for a genuinely different reader technology (a real OCR
  engine, open-decisions #4) so at least one reader structurally cannot refuse
  -- the removed local-vlm was to have been that reader but never worked on real
  pages. Until a real OCR engine lands, the only remedy is the human-resolution
  path below; trying a different LLM provider (codex-cli / openai-api) for the
  offending reader may reduce the rate but cannot guarantee a fix. Rough rate
  this run: ~5 of 12 text documents hit at least one refusal-caused disagreement.

- **The human-resolution path for these disagreements was un-runnable and is
  now wired.** `resolve_from_raw_ocr()` existed and was unit-tested but had
  no CLI entry point, so a blocked document could not actually be resolved
  without re-running OCR. Added `run_checkpoint1.py resolve-disagreement`
  (loads the `_ocr_scratch/{case}_{doc}_raw.json` dual-read dump, applies a
  human's per-page `--chosen-reading`, rolls the doc up to
  `disagreed_resolved`). Used it to resolve DOC_005/007/009/012/013 under
  reviewer Pyun. NB DOC_012 p1 was a GENUINE content conflict, not a refusal
  (14,488 vs 34,488 on the 산출기초 field -- reading_b misread a form bracket
  ']' as a leading '3'); resolved to reading_a (14,488) after direct source
  inspection. The refusal cases and the genuine-conflict case go through the
  exact same human-in-the-loop path -- the tool never auto-picks.

- **(d) DOC_010 is a non-text document (clinical injury photographs -- an arm
  scar measured with a tape, i.e. 후유장해 evidence), which the pipeline has no
  first-class path for.** Both pages P8-disagreed only because one read
  refused while the other correctly said "this is a photo, no transcribable
  text"; there is no faithful *text* to choose. The taxonomy
  (common_component_output document_type) has no image/photo type -- closest
  is `other`. Forcing a photo-description into the page-text field would be
  wrong. LEFT PENDING as a design decision (user asked for it to be handled
  as an image/non-text document, not shoehorned into text). Needs: a
  first-class no-text/image extraction_method + how downstream stages treat
  such a page. Deferred, not resolved.

  **CONTRACT RESOLVED 2026-07-15 (case state not yet changed):** added the
  human-only `resolve-non-text` path and `non_text_image` /
  `non_text_verified` / `expert_review_only` contract. It preserves the P8
  disagreement, writes no text or classification quote, skips text redaction,
  and makes chunk omission explicit. The implementation deliberately refuses
  mixed documents with any validated text page. DOC_010 itself remains
  untouched until the user separately authorizes the DAO-backed resolution.


## 17. `transcribe_image()`'s "Image: {path}" label was misread as attachment metadata, not a Read instruction -- caused the claude-cli OCR refusals thought to be non-deterministic -- RESOLVED 2026-07-16

CASE_024's Stage 2 run needed 9 manual OCR retries after both readers
initially refused on 4 pages, attributed at the time to generic claude-cli
vision non-determinism (queued as a follow-up investigation rather than
fixed inline, since a prior attempt to fix a *different* refusal problem via
defensive prompt framing had backfired). Investigating it properly found the
refusals were not non-deterministic at all: the framing
(`f"{prompt}

Image: {image_path}"`) states the image as a trailing
label, and the model reads that as descriptive metadata about an attachment
that a text-only `claude -p` CLI call never actually delivers -- rather than
as an instruction to invoke the `Read` tool on that path -- so it never even
attempts the read and reports "no image was attached."

Controlled repeats on CASE_024's actual page images confirmed this
empirically: the label form failed **9/9** (with and without `--safe-mode`,
including on pages that appeared to succeed on the first pass during the
real run -- meaning the real run was silently absorbing this failure rate
throughout, not just on the 4 pages that happened to end up flagged).
Rewriting the same call as an explicit imperative ("Read the image file at
{path} and then: {prompt}") succeeded **every time tested** (3/3 in the
controlled repeat, plus 2 more ad hoc confirmations on previously-refusing
pages). This is not a reversal of the earlier defensive-framing lesson
(known-gaps history in `llm_providers.py`'s docstring) -- that framing
pre-argued its own legitimacy and told the model not to refuse, which itself
read as a prompt-injection signal; this fix carries no self-legitimizing or
anti-refusal language, just a concrete action to take.

**Cross-checked against the original failure, not just the reproduction.**
CASE_024's original dual-read dump (`_ocr_scratch/CASE_024_DOC_002_raw.json`,
preserved from the actual blocked run) was re-read to confirm this diagnosis
explains what genuinely happened, not just what the reproduction produced.
Of the 9 disagreed pages, **3 (pages 9, 41, 45) show exactly this failure
mode verbatim** in the original reader output -- e.g. p9 reading_b: "I can't
transcribe this image because I don't have visual access to it -- **no image
data was included with your message, only a file path**"; p41 reading_b: "...
**no image was actually provided in this message**"; p45 reading_b: "I don't
have the ability to view images directly in this conversation ... I can't
access or transcribe the contents of the file at that path." All three name
the identical root cause the reproduction found -- a missing attachment, not
a content-based refusal -- confirming this is the real, not merely
plausible, cause for those pages.

**The other 6 disagreed pages were NOT this bug and remain correctly
understood as separate failure modes, already covered by item 11's fix, not
this one:** page 40's dump shows both readers producing real transcribed
content but each appending a different one-sided fabricated sentence
(hallucinated content, the failure item 11's `compare()` prompt update
specifically targets); pages 6/11/20/29/44 were genuine transcription
disagreements or ambiguous glyphs resolved by direct human/image review, not
refusals at all. So this fix explains 3 of CASE_024's 9 disagreements
directly, is the majority explanation for the reader-level "refusal" pattern
specifically (as opposed to disagreement in general), and the 9/9
reproduction rate indicates it was very likely under way on other pages too
that happened to still resolve to agreement by chance (e.g. one reader
failing this way while the other transcribed correctly, with `compare()`'s
one-sided-addition check then catching the mismatch) -- but the dump alone
does not let every one of the 9 be individually attributed to this cause with
certainty.

**Fixed:** `ClaudeCliProvider.transcribe_image` in `tools/llm_providers.py`
now builds `f"Read the image file at {image_path} and then: {prompt}"`.
Regression test in `tests/test_llm_providers.py` updated to assert the new
literal command. Full suite (271 tests, frontend excluded per its pre-existing
fastapi gap) passes.

**Found but NOT fixed -- a related risk in `compare_text()`:** the comparator
is equally text-only and, if two independent reads both come back as
refusals (different wording each time, so the byte-identical shortcut in
`compare()` doesn't trigger), the comparator has no conflicting *facts* to
find between them -- a live test confirmed the comparator can itself suffer
the same "nothing was actually provided" misfire when asked to compare two
refusal texts. In the one case actually observed, this was caught only by
`compare()`'s existing fail-safe (an unparseable verdict is treated as a
disagreement) -- not because the comparator correctly recognized the refusals
as one-sided fabricated content per the item-11 fix. With
`transcribe_image()` fixed, reader-side refusals should now be rare; this
residual path is a second line of defense that has not been independently
hardened and is not urgent to close before it recurs in practice.

## 18. Full review-fleet findings -- fixed set + deferred set (2026-07-22)

An 8-reviewer adversarial fleet went over `main`. The clear, verified fixes
landed (see the CLAUDE.md 2026-07-22 row). These are the findings deliberately
DEFERRED, with why:

- **D1 is not sealed against Bash-based reads.** The new `.claude/settings.json`
  deny-glob blocks the *Read tool* on `data/ground_truth`/`source-cases`, and the
  DAO traversal is closed, but an agent with the Bash tool can still `cat`/`python`
  the answer key. A complete seal needs OS-level permissions (or moving/encrypting
  ground truth keyed on the human-review flag). Tracked as the real structural
  boundary for production; the PoC accepts prompt+Read-deny isolation.
- **Per-field manifest ownership not enforced.** `patch-manifest-document` +
  `document_manifest.schema.json` allow any stage to rewrite any field (no
  `additionalProperties:false`, no owner check). `additionalProperties:false`
  was NOT added because it could reject existing valid manifests with extra
  fields; per-field ownership needs a write-time owner map. Deferred.
- **P8 halt is wrapper-level, not DAO-level.** `dao write-page-text` will persist
  a page an agent hand-labels; the halt is guaranteed only via `run_checkpoint1`.
  A DAO guard (refuse write-page-text for a `disagreed` page without a
  `resolution`) would make it structural. Deferred.
- **Unstructured / ordinary-word over-redaction.** A person name that is a
  substring of a KEPT ordinary word (영수 -> 영수증) is still blind-replaced with
  no signal; `scan_residual_pii` covers only structured PII. Fully closing this
  needs offset-based (NER) redaction -- open-decisions.md #1.
- **document_assembly orphan `[E#]` tags.** A hand-written `[E#]` in section
  content produces a tag with no sidecar entry; caught downstream by the critic,
  not structurally by the assembler. Deferred.
- **D2 content scan is `.pdf`-only and first-5-pages-only**, and is defeatable by
  an injected "CLEAR" in the page image. It is explicitly a human-review signal,
  not a structural gate; a `.hwp`/`.docx` answer key or a report with its
  conclusion on page 6+ gets no content check. Deferred (needs format coverage).
- **`fork_case` rewrites only top-level `case_id`.** Embedded case-id-derived
  paths (backup_path, file_path, redacted_text_path) still point at the source
  case after a fork. Deferred.
- **Frontend has no auth / CSRF / rate limits.** ACCEPTED, not fixed: per review
  decision the pipeline viewer is a localhost-only dev tool. The one D1-relevant
  frontend hole (serving ground-truth files) WAS fixed. If the frontend is ever
  exposed, auth + CSRF + upload/spawn caps + scrubbing the `/run` child env
  become required.

## 19. Analysis agents could read pre-redaction page text -- RESOLVED 2026-07-22 (specs + regression), residual risk OPEN

Found live during the CASE_901 test run (`intake -> document-pipeline ->
denial-response`, RUN_20260722_001).

**What happened.** The `denial-response` subagent followed its spec and called
`dao.py read-document-text CASE_901 DOC_001`. That command returns the **path**
to `redacted_text.md`, not its text -- but all three consuming specs described
it as the way to "read the processed text". Getting back a one-line path where
text was promised, the agent concluded the command had failed, judged that
opening the returned path itself would violate P2, and fell back to
`read-page-text` for all four pages. `page_NNN.md` is checkpoint 1 output --
**before** redaction.

**Measured exposure** (CASE_901/DOC_001):

| page | `page_NNN.md` (checkpoint 1) | `redacted_text.md` (checkpoint 2) |
|---|---|---|
| 2 | `서울시 서초구 서초대로74길 14, 33층` / `(02)758-7755` | `[ADDRESS]` / `[PHONE_NUMBER]` |
| 4 | `대표이사 나 채 범` | `대표이사 [PERSON_NAME]` |

The agent read a natural person's name, a street address, and phone/fax numbers
that checkpoint 2 exists to remove. **No PII reached the output** -- all 19
evidence quotes in `denial_reason_result.json` were checked and are clean -- but
that was the quotes it happened to pick, not a control.

**Root cause was the specs, not the code.** The two commands are deliberately
different, because their consumers are:

| command | file | redaction | consumer | returns |
|---|---|---|---|---|
| `read-page-text` | `page_NNN.md` | **before** | `redact_document.py:75` (tool) | text (in-process, feeds the Redactor) |
| `read-document-text` | `redacted_text.md` | **after** | agents | path (agent reads as much as it needs) |

Making `read-page-text` return a path instead would be worse -- it would put a
pre-redaction path into circulation and make opening it the normal idiom.
`read-document-text` returning a path is fine: the file is redacted, and a
136KB document (`CASE_024/DOC_002`) should not be force-fed into an agent's
context. What was wrong was three specs describing the path-returner as a
text-returner.

The specs also wrote the command as `read_document_text` -- underscored, which
matches neither the CLI name (`read-document-text`) nor any in-process function
(only `cmd_read_document_text`, an argparse entry point, exists). Unlike
`read_contract_data`/`patch_manifest_document`, which are real callable
functions, this name existed nowhere. Same files already wrote every other DAO
call in executable form (`dao.py write-contract`, `dao.py patch-manifest-document`).

**Fixed.**
- `denial-response.md` / `policy-pipeline.md`: state that `read-document-text`
  returns the redacted document's path and that the path is what you read; ban
  `read-page-text` outright as checkpoint 2's pre-redaction input.
- `document-pipeline.md`: keeps `read-page-text` (it owns checkpoint 2) but now
  says plainly that it is pre-redaction data, redaction's input and nothing
  else, never to be quoted into a contract or handed to another stage.
- All three rewritten to the executable CLI form; `sync_agents.py` re-run.
- `tests/test_redaction_boundary.py` (6 tests) pins the invariant: PII present
  in `page_NNN.md` is absent from `redacted_text.md`; `read-page-text` yields
  pre-redaction text; `read-document-text` yields a path, never content; the
  path it yields holds redacted content; the two commands are not
  interchangeable; `expert_review_only` still emits no path at all. Verified by
  mutation -- reintroducing the "print the text" change fails 2 of the 6.

**Residual risk, OPEN.** A spec is a prompt. Nothing structurally prevents an
analysis agent from calling `read-page-text`; the DAO does not know who is
calling. A real gate needs caller identity at the DAO boundary -- e.g. a
required `--caller-stage` on `read-page-text` accepted only for
`document_processing`, mirroring how `read-ground-truth` already takes
`--caller-stage`. Deferred, and worth doing: this is the second finding in this
file (see item 18) where the sanctioned path was correct but not sealed.
