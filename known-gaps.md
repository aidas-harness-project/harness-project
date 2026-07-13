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

## 2. Live D1 near-miss -- CASE_002 -- PARTIALLY RESOLVED 2026-07-13

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

**Still open, deliberately not acted on without a human call:**
- **Processed pages** (`data/processed/CASE_002/DOC_002/*.md`) -- left
  as-is on explicit instruction, not purged/quarantined.
- **The raw files themselves** (`data/raw/CASE_002/DOC_002.pdf`,
  `DOC_003.pdf`) -- untouched. What "resolved" means for a *rejected raw
  classification* isn't fully defined by the current tooling: `classification`
  in `_source_ledger.json` is `raw` | `ground_truth`, and rejecting a file
  is meant to flag "the proposed classification is wrong" -- but
  `dao.py set-ledger-status` has no path to actually change `classification`
  from `raw` to `ground_truth` (only `review_status`). So there's currently
  no tool-supported way to "correctly reclassify and re-approve" these
  files even if that were the right call -- the only supported action was
  reject. Whether the case should be re-run with these two files simply
  excluded from raw input, or something else, is still an open call.
- **Evaluation exclusion (D1)** -- not formally recorded anywhere yet (no
  run currently being evaluated to exclude from); the blocked ledger state
  itself is the practical safeguard for now.
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

  **Would this have caught CASE_002?** DOC_002 and DOC_003 both had a
  giveaway title on page 1 (literally "보험금사정서") -- yes, a 5-page scan
  would have caught both. A document that buries its conclusion beyond page
  5 without an early giveaway would still slip through; this raises the bar,
  it doesn't make the check exhaustive.

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

## 11. P8's `compare()` has a real blind spot: it catches conflicting facts, not fabricated additions -- found running a real document through checkpoint 1, NOT YET FIXED

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

**NOT fixed -- the actual methodology gap in `compare()` itself.** This
needs a real design decision (e.g. asking `compare()` to also flag
material differing in *length or content scope*, not just conflicting
specific facts; or a stricter prompt; or a separate "does this reading
contain anything the other doesn't" pass) before it can be trusted not to
recur. Every "agreed" page written by every prior real OCR run in this
project (CASE_002's DOC_002/DOC_005, CASE_012's pages 1/2/4) has **not**
been re-checked for this specific failure mode -- this was only caught
because a human happened to read page 3's full text while preparing it for
redaction, not because any structural check would have caught it.

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
