<!--
Per-case orchestrator briefing for a corpus throughput run.

Substitute {CASE} and {RUN}, hand the result to ONE pipeline-orchestrator.
One orchestrator per case -- never one carrying a list (standing decision
2026-08-26: concurrent stage agents sharing a scratchpad path put one case's
judgement under another case id, and write-contract accepted it).

This file exists because the first four cases of the CASE_70xx run each got a
hand-written briefing and they were NOT the same. D2 pre-authorisation was
missing from the first two, so both stalled on that gate and one had to ask;
the filename-looks-like-an-answer-key warning only appeared from the third;
the observation-reporting item only from the fifth. Each difference was
something learned and added to the next one, which meant every earlier case
ran under a briefing already known to be incomplete.

Edit HERE, not in a session scratchpad, so the next batch inherits it.
-->

Process **{CASE}** end to end, from intake through `screening_report`. This is the only case in your scope — never touch another case's `outputs/`, and note that other orchestrators are working other cases concurrently.

Run id: **{RUN}**

## Source

Staged at `_workspace/corpus7000/{CASE}/`. Nothing has been run — no `outputs/{CASE}/` yet, so start at `intake`. Read the staged directory rather than assuming a document count.

This material is from `_workspace/corpus-split/raw/`, the ground-truth-free half of a human-approved split recorded in `_workspace/corpus-split/split_manifest.json`. **A staged filename may still read like an answer key** (e.g. `손해사정서 + 약관__...pdf`) because it names the ORIGINAL mixed PDF; the staged file is the raw half only. Verify against `split_manifest.json` and the file's own page count/sha256 if D2 asks you to. Never read `data/ground_truth/`.

## Scope

`intake` → `document_processing` → `indexing` → `policy_clause_processing` → `claim_analysis` → `consistency_check` → `screening_report`. Stop there; `draft_report_v1` is out of scope.

## Environment — set BOTH provider vars, they resolve separately

    HARNESS_LLM_PROVIDER=claude-cli
    HARNESS_REDACTION_PROVIDER=claude-cli
    HARNESS_SINGLE_READER=1
    HARNESS_SKIP_PII_SCAN=1

**Use the `export VAR=... && python ...` form.** The inline `VAR=value command` env-prefix is refused by the permission classifier in this session, in both Bash and PowerShell. Two orchestrators hit it independently on the previous batch, twice each, before finding the working form — the same tool with the same arguments, just written differently. Do not persist the variables at user scope instead: that leaks settings into other orchestrators running concurrently.

`HARNESS_SINGLE_READER=1` is my decision: one read per page, so this run is throughput, NOT evaluation-grade. Say so in your report.

**On PII, so you do not have to rediscover it:** `HARNESS_SKIP_PII_SCAN=1` disables the deterministic sweep, and the redaction MODEL is separately skipped by default (`SKIP_REDACTION_DEFAULT = True`), so nothing checks these pages for PII and the tool will warn exactly that per page. Expected and pre-authorised — the corpus is pseudonymised at source (CASE_7001 DOC_002 verified: `환자의 성명 | [redacted]`, zero RRNs, zero phone numbers). Do not halt on it; do record which redaction method the artifacts carry.

## Decisions already made — do not re-ask

- **P6 conflicts must not end the run.** Dispose them `deferred_to_report` yourself, with a note stating the disagreement and what would settle it. Write the note from what the sources actually say — on CASE_7044 a note claimed a document "does not record fracture" when it recorded two fracture codes as 부상병, overstating the disagreement on a record a 손해사정사 reads verbatim. If no candidate reaches the ledger, do not manufacture one.
- **D5 (no 약관) is pre-authorised** by reviewer `pyun`. Record it with the real document composition and continue. Never re-type a 증권 as a 약관.
- **A document whose medical classification settled on no kind stays unrouted** (known-gaps 63, decided). Do not treat an `ambiguous` document as a blocker or force a kind onto it.
- Segmentation auto-approval by the driver is documented current behaviour; record the reviewer as the driver, never a person's name.

## Concurrency

**Dispatch at most ONE stage agent at a time, and have each write to a case-unique scratch path.** No parallel agents within your run. Before assembling any report, verify the judgement file's `case_id` is `{CASE}` and its `run_id` is `{RUN}`; if either disagrees, stop and report — never relabel a payload to make it fit.

## T13

Full lifecycle per stage: `update-run-state in_progress` → run → `record-dispatch` (agent dispatches only) → `finalize-stage`, then `aggregate-trace` at the end. Pass `--from-usage` with the harness's `<usage>` block verbatim; if it does not reach you in that form, omit the token flags rather than assembling one by hand. Driver stages have no dispatch boundary.

Stage 2 runs as ONE command (`run_stage2.py`). Stage 4 needs `run_policy_preflight.py` before the attempt opens, then `run_policy_pipeline_driver.py`. Stage 5 is `run_claim_analysis_selective.py` — never dispatch the retired `claim-analysis` agent.

**Stage 2 and stage 5 each take minutes and will outlive a foreground tool call.** Run them in the background and wait rather than killing them — an interrupted attempt costs a full re-run of paid provider work.

Waiting means ending your turn, and ending a turn means producing output, so a status line while a driver runs is unavoidable — not a failure to follow instructions. **Keep it to one line** ("stage 2 running, waiting"). Do not restate the whole run state, re-derive what you already know, or explain your next steps; save all of that for the final report, which is the one that gets read. An earlier version of this briefing said "do not report back until the case is finished", which cannot be done in this harness and only produced paragraphs of justification for reporting anyway.

## Report back, from the contracts

1. Stage outcomes and attempt counts.
2. Document count and `document_type` tally after Stage 2.
3. `stop_reason` tally across `claim_facts`, and the count of `unavailable_reason: printed_but_blank`.
4. Conflicts raised and how each was disposed.
5. Report path, key-issue and review-point counts.
6. Any stage that failed, with the exact error, and any gate you hit.
7. **Anything that looked wrong but did not block you** -- a field you expected to resolve and did not, a document nothing routed to, a figure that disagreed across sources without becoming a candidate.

   **Read the artifact from disk before reporting it, and name the file you read.** Four of five observations from the previous batch did not survive that check: `reviewer_role` reported as mojibake was clean UTF-8 on disk (a cp949 CONSOLE rendering -- Korean text routinely displays as `???` in this terminal, which says nothing about the file); `extraction_method: ocr` against `content_kind: text` turned out to be two different axes rather than a contradiction; a redaction method reported as `null` read `dev_no_llm_redaction` in all eight files; and a schema "defect" was a conditional rule working as designed. Each one cost a verification pass.

   So: if you read it from a file, name the file. If you only saw it in console output, write "console only, not verified against the artifact" -- that is still a useful report, just a different one. Do not re-adjudicate it and do not act on it either way.

Do NOT judge whether a `not_mentioned` field is "really absent", and do not sample fields to pronounce them missed. Report counts and quotes.

Do not patch tools. Stop and report if a gate outside the pre-authorised list blocks.
