# Loss-Adjustment Agent Harness PoC

A 3-week experiment validating an agent harness that takes pseudonymized,
closed insurance-claim cases and produces a screening report plus a draft
loss-adjustment report, evaluated against the real final report. Full
pipeline/agent/taxonomy reference: `pipeline.md`. Original 3-week plan
(success criteria, Go/No-Go): `POC guide.md` (Korean, not yet reviewed for
accuracy against the current design -- treat as historical planning
material, not a live spec).

## Hard rules

- **Every session follows `harness-guardrails` and `harness-guardrails-dev`.** These are the non-negotiable constraints every agent follows in every stage -- read them before touching anything in this pipeline. `harness-guardrails-dev` only applies while a ground-truth answer key exists in this repo (the PoC/evaluation phase); it stops applying once there's no ground truth to isolate.
- **Raw sources are immutable**: `source-cases/`, `case_qna.pdf`, `archive/sources/` are read-only. Never modify or delete.
- **No agent reads or writes `outputs/`, `data/`, or a ledger/run-state file directly.** Everything goes through `tools/dao.py` -- see harness-guardrails P2/P5/P7/P10 and harness-guardrails-dev D1/D2. Direct file access bypassing the DAO is exactly the kind of thing these rules exist to prevent. (The OCR reader path does not need a carve-out here: `tools/ocr_extract.py`'s `claude -p` reads use a neutral "transcribe this image" prompt and only print to stdout -- the calling tool performs the governed DAO write itself, so a blind reader bypasses nothing.)
- The final loss-adjustment report inside `source-cases/` is the evaluation answer key. **No stage that produces pipeline output ever reads it** -- never feed it to a model as input. **One exception, added 2026-08-22: the verification agent.** The PoC's success measure is agreement with the adjuster's real report, so a scoring agent that never sees the answer key cannot measure the thing being validated. `screening-fidelity` is that agent and the only one, under four conditions that keep the exception from becoming a hole: (1) it reads ground truth **only** through `python tools/dao.py read-ground-truth`, never a direct file read -- the `Read()` deny globs in `.claude/settings.json` stay exactly as they are, so the DAO stays the single logged door; (2) only after that version's human review is marked complete; (3) its result is **terminal**, and structurally so -- `screening_fidelity_result_v{n}.json` is written under `data/ground_truth/CASE_ID/_verification/`, inside the denied tree, through `dao.py write-verification-result` / `read-verification-result`, both restricted to the `screening_fidelity` stage. It names ground-truth values, so a copy under `outputs/` would hand every producing stage the extracted answers; (4) every other agent is unchanged, and an access by any of them still means halt the run and exclude its outputs. See `harness-guardrails-dev` D1.
- Documentation, code, and agent/skill definitions are English. The two exceptions: raw source material (Korean, as collected) and the actual deliverable documents the pipeline produces (screening report, draft report -- Korean, since they're submitted to Korean-speaking professionals).
- **Answer questions about system behaviour from executed evidence, not from reading the code and inferring.** When the user asks what happened, what a stage did, or whether something is a problem, the answer must rest on something actually run or read *this session* -- a DAO read (`read-timing-summary`, `read-ledger`, `read-conflict-ledger`, `read-contract`, `read-driver-receipt`), a test run, a command's real output, a file's real contents. Code structure tells you what *can* happen; only records tell you what *did*. If the evidence cannot be obtained, say "not measured" or "not determinable from retained records" and name what would settle it -- never fill the gap with a plausible mechanism. Cite the source with the number or path, so the user can check it. **This applies with full force to claims about the agent path**, whose behaviour is recorded in timing summaries and ledgers, not deducible from the tools it calls. Origin: on 2026-08-16 the assistant claimed schema rejections were "silently absorbed by the agent's retry loop"; the timing summaries showed **0 validate errors across all three agent arms** (CASE_029/032/035) and `write-contract`'s own docstring states it makes exactly one attempt and does not implement P4's retry loop -- the mechanism described did not exist, and the inference inverted the finding it was explaining away.

## Tools

- `python tools/dao.py <subcommand>` -- the sole data-access path (locking, ledgers, run-state, conflict tracking, schema-validated writes). See its module docstring for the full subcommand list.
- `tools/trace.py` -- performance instrumentation. Every pipeline tool records timing spans
  automatically (its `main()` calls `trace.configure_from_args`); `HARNESS_TRACE=0` turns the
  whole layer off. Shards are written lock-free under `outputs/CASE_X/_trace/<run_id>/` because
  each names exactly one writer thread — see the module docstring for why that is not a P5
  bypass. `attrs` are filtered against a per-category allow-list, so a prompt or a page of case
  material cannot reach a trace file. The two SLA markers (`sla.phase1.start` on a clear source
  ledger, `sla.phase1.end` on `finalize-stage draft_report_v1`) are emitted by the DAO itself.
  **One manual step:** `dao.py aggregate-trace CASE_ID --run-id RID --held-by NAME` rolls the
  shards into a schema-validated `_timing_summary.json` (read back with `read-timing-summary`).
  Nothing calls it automatically, so a run that skips it leaves raw shards and no summary.
  T13 adds cross-process `stage.attempt.start/end` markers around every explicit subagent
  dispatch. They are aggregated separately into `stage_attempts`/`by_stage`/`stage_coverage`,
  never added to ordinary span self-time. `unattributed_active_s` means only that no current
  tool span explains that wall interval; it is not a claim about model reasoning. Contract
  checkpoint writes do not begin attempts, and an interrupted attempt remains duration-null.
  **A second manual step, required around every subagent dispatch:** `dao.py record-dispatch`
  records the interval from asking for the work to holding its result, with the harness's own
  `--agent-reported-s` and `--input-tokens`/`--output-tokens`/`--total-tokens`/`--tool-uses`.
  Tool spans do not explain an agent stage -- `claim_analysis` on CASE_022 spent 1.20s in tools
  across 773.0s of wall (0.15%) while tokens tracked wall time at 277 tok/s -- so without these
  counts the only reading of the remainder is `unattributed_active_s`, which is not one. Every
  value is harness-reported and copied, never estimated: an omitted flag records as "not
  measured", a guessed one is indistinguishable from a reading. The same rule governs a
  driver receipt's `provider_usage`, whose field names are ONE canonical vocabulary rather
  than any single provider's: an OpenAI-compatible backend (openrouter, openai-api) reports
  `prompt_tokens`/`completion_tokens`/`cached_tokens`/`reasoning_tokens`, and
  `driver_runtime._normalize_usage` renames those onto the canonical fields. A rename is not
  an estimate; deriving one figure from another, or filling a missing one with a zero, would
  be -- and neither happens. The pipeline skill's T13
  lifecycle places the call between agent return and `finalize-stage`.
- `dao.py read-redacted-text-bundle CASE_ID --doc-id DOC_X [--pages DOC_X=11,35-36]` -- the
  redacted read for analysis stages. `--pages` narrows ONE document to named pages (repeat per
  document; anything without it still comes back whole), for a long policy after
  `search-document-text` or `read-document-index` has said which pages matter. `read-page-text`
  serves the PRE-redaction layer and is refused here, so before this flag an agent that knew its
  five pages still had to take the bundle whole -- on CASE_027 the two policy documents were
  222,084 of `claim_analysis`'s 272,655 input characters (81%) and five pages of one were cited,
  none of the other. `redacted_text_sha256` still covers the FULL document, so a quote from a
  narrowed read verifies identically; `pages_omitted`/`total_page_count` say whether a read was
  narrowed. A requested page the document lacks is refused, never returned empty.
- `python tools/validate_output.py <file.json>` -- standalone schema validation (also used internally by `dao.py write-contract`).
- `python tools/intake_case.py <source-cases folder> <CASE_ID>` -- case intake with the D2 per-file review ledger.
- `python tools/document_assembly.py --sections-file <spec.json> --held-by <agent> --run-id <run>` -- renders narrative reports and auto-generates `[E#]` citation tags + sidecar (P1). DAO-backed like any other write path: locked, atomic, sidecar schema-validated before either file touches disk.
- `python tools/chunk_text.py CASE_ID DOC_ID [DOC_ID ...]` -- deterministic page-chunker for document-pipeline's checkpoint 3 (no LLM call; slices exact verbatim text using the `<<<PAGE page=N>>>` markers checkpoint 2 embeds). Case-scoped -- one call across every document in the case, not one per document.
- `python tools/fork_case.py SOURCE_CASE_ID --label "..." --held-by NAME --run-id RUN_ID` -- forks `outputs/`+`data/processed/` into a fresh, auto-numbered `case_id` (schema-locked to `CASE_NNN`) so a branch can reuse already-completed OCR/redaction work instead of re-running it. `--include-raw`/`--include-ground-truth` opt in to copying those too (the latter duplicates real answer-key material -- deliberate, not default).
- `python tools/run_stage2.py CASE_ID --held-by NAME --run-id RUN_ID` -- **the way Stage 2 is run.** Takes `--provider`/`--model`, both optional and both applied to every provider-backed step in the stage (readers, comparator, classifier, redaction, segmentation judge); omitted, each falls back to the configured default and to each role's own env var. A phase-0 preflight builds every role before the first paid call, so a provider that needs a model it was never given blocks the stage instead of failing at redaction after the OCR is spent. Performs every mechanical step of `document_processing` in one invocation (checkpoint 1 → checkpoint 2 → segmentation propose/approve/split → per-child `classify-only` → child redaction → chunking → `page_chunks.json` contract write) and stops with `blocked_gate` at the four decisions that are genuinely a human's: a P8 disagreement, segmentation boundary approval (`--auto-approve-segmentation` skips it for timing runs only), a `raw_page_text` classification review, and a possible PII leak. It never moves a run-state marker -- `update-run-state`/`finalize-stage` stay with the orchestrator (T13). Exists because the per-step sequence was serialized in a SPEC rather than in code: Stage 2's checkpoint control is deterministic, and its four real decisions stay explicit human gates. **Do not cite an exact Stage 2 speedup ratio.** Earlier revisions of this file quoted `897s` agent-led against `79s` driven with `~510s` of model round trips (elsewhere `547s -> 79s`, `6.9x`); that is a *historical observation, not reproducible from retained timing records*. CASE_911's closed agent-led `document_processing` attempt of 897.4s is real and DAO-verifiable, but no retained trace holds a closed, cold, input-equivalent driver arm, and no dispatch-boundary instrumentation exists to recover per-decision-round timing. A future performance claim requires a controlled cold A/B plus that instrumentation. It also adds the contract envelope `chunk_text.py` does not emit (`case_id`/`component`/`status`/`run_id`/`created_at`), which was previously an undocumented hand-filled step.
- `python tools/run_checkpoint1.py CASE_ID DOC_ID <pdf> --held-by NAME --run-id RUN_ID` -- automates checkpoint 1's mechanical sequence. Before provider construction or PDF access it refuses exactly one target -- the retained superseded bundle, whose children already own its pages (`blocked_segmentation`, zero OCR/output work). An unsplit bundle is NOT refused: reading it is what lets segmentation place boundaries from real text. `--bundle-ocr` runs OCR without classification, for a bundle whose per-document types are not yet knowable. Otherwise checkpoint 1 runs dual-path OCR, page writes, classification, and manifest update, stopping cold at any P8 disagreement. `run` and `run_document_stage.py` both preflight the providers they will actually use before the first paid call -- a provider that needs a model it was never given blocks the checkpoint instead of failing partway through, after the OCR is spent. `classify-only` is deliberately exempt: a printed form title settles most types with no model call, so it builds its provider lazily. `classify-only CASE_ID DOC_ID` classifies a document whose pages already exist without re-reading it -- the path for a split child, which inherits its pages from the bundle; `run` refuses such a document (`already_extracted`) rather than silently paying for OCR twice and overwriting its inherited P8 history. A child whose approved text-anchor title NAMES a form (진단서/기록지/내역서) is typed from that title with no model call (`classification_source: printed_form_title`); a genre-only title like `REPORT` falls through to the classifier.
- `python tools/run_scenario_matrix.py CASE_ID DOC_ID <pdf> --held-by NAME` -- runs real OCR once; if a real disagreement comes back, forks it three ways (reading_a/reading_b/unresolved) via `fork_case.py` and reports each outcome. Deliberately scoped to P8's resolution gate, not all decision points -- see its module docstring for why the others are better tested by `tests/test_dao_*.py` instead.
- `python tools/segment_case.py {sheets|propose|show|approve|split} CASE_ID DOC_ID [...]` -- document segmentation: split a raw *bundle* PDF into per-document `DOC_XXX.pdf`. **A checkpoint inside Stage 2, not a stage before it** (`open-decisions.md` #7, resolved 2026-08-05): the bundle is OCR'd (`run_checkpoint1.py --bundle-ocr`) and redacted first, so boundaries come from real page text instead of a downscaled crop -- which is what lets a *scan* reach the deterministic path at all. `propose` resolves evidence in the order **processed text → the PDF's embedded layer → vision**, taking the redacted text when it exists (titles survive redaction: verified on all 217 of CASE_112's split children, so nothing needed for boundaries is lost and pre-redaction text stays behind its capability gate). Boundaries come from the publisher's own title lines -- `...보통약관`/`...특별약관`/`...특약` for policy, medical form names (진단서, 수 술 기 록, 진료비 세부산정내역) through a 5-line header window, since a statutory 별지 서식 header or field labels often precede the title. Consecutive pages repeating one title are one document; a generic form-kind heading (`REPORT`) is not a document name and never merges. Article headings (`제N조`) are deliberately *not* boundary signals and a table-of-contents block is suppressed by line density. A page the deterministic rule cannot settle goes to an **LLM tier** that reads that page and the one before it; an unusable verdict splits and is flagged (`undecided_pages`), because over-splitting is undone by a human merge while over-merging propagates a wrong `document_type` downstream. Vision remains the fallback for a bundle with no processed text: `propose` rechecks crop-ambiguous `needs_full_page` pages full-page under the 25% cap, and optional `--refine` re-examines long, confidently over-merged segments (F1 0.94 on the 110p bundle); `--no-text-anchor` forces vision for comparison. `approve` moves the human gate; `split` cuts PDFs, retains the bundle as `superseded_bundle`, and **hands each child the bundle's OCR pages** renumbered from 1 with P8 verdicts intact -- children are never re-OCR'd. **Segmentation itself performs no OCR -- its output is document structure, not text** (`ocr_performed` is schema-const false). Provisional type guesses are never trusted downstream, except a text-anchor slice of an already-classified `insurance_policy` bundle, which inherits that type by construction.
- `python tools/score_segmentation.py --proposal PATH --baseline PATH` -- scores a segmentation proposal's boundary set against a hand-recorded ground-truth baseline (precision/recall/F1 + the exact over-split/over-merge pages), so a crop-ratio or grid change is a measured number, not a guess.
- `python tools/sync_agents.py` -- regenerates `.codex/agents/*.toml` and `.agents/skills/*/SKILL.md` from the canonical `.claude/` definitions. Run this after editing any `.claude/agents/*.md` or `.claude/skills/*/SKILL.md` -- never hand-edit the generated copies.
- `pytest` -- runs `tests/`, covering the DAO's locking/write-contract/conflict-ledger/run-state paths and the document-assembly/validation/OCR tools. Every filesystem test runs against a `tmp_path`, never the real `outputs/`/`data/` trees.
- Version-controlled with git. Propose a commit when the user asks, or when a meaningful unit of change is complete.

## Harness: loss-adjustment case pipeline

**Goal:** closed-case input → screening report + draft report + evaluation, via 10 specialized agents across 2 phases.

**Trigger:** use the `loss-adjustment-pipeline` skill for case processing, reruns/updates, or evaluation requests. Simple questions about pipeline design can be answered directly from `pipeline.md`.

**Load that skill BEFORE running any stage command -- not only when a request
says "run the pipeline".** Advancing a case's run state IS case processing, even
when it looks like a one-off `finalize-stage` or a single driver invocation. The
skill is the executable orchestration contract; this file's tool list is a
reference, not a runbook, and it deliberately does not carry stage order,
preconditions, or the T13 dispatch lifecycle. **Which stages are driver-owned is
recorded there and nowhere else** -- and the agent roster keeps advertising
agents (`claim-analysis`, `policy-pipeline`) for stages whose agent path is
retired, so an unread skill leaves no way to tell which is authoritative.
Origin, 2026-08-19 on the CASE_302~321 corpus run: the assistant reconstructed
the flow from this file plus prior sessions' leftovers, and in one sequence
(a) dispatched the `claim-analysis` agent for a stage the skill marks
"(driver, no agent)" with an explicit "Do not dispatch `claim-analysis`",
(b) hand-finalized `policy_clause_processing` without running
`run_policy_pipeline_driver.py`, and (c) skipped `run_policy_preflight.py`
entirely. (a) and (b) were the same defect twice; (b) then blocked stage 5,
because the policy driver is what WRITES the `_document_index.json` that stage 5
links policy clauses from -- a dependency stated in the skill's stage table.
(The legacy driver hard-required it; since its deletion on 2026-08-20 the
selective driver treats it as optional, so the same omission now degrades
silently -- every `policy_link` returns `not_found` -- instead of blocking.) A stage marked `passed` whose driver never ran is worse than a failed
one: it reports success and silently withholds a downstream input.

**Changelog: `CHANGELOG.md`** -- the full dated history of every design change,
defect, and measurement (moved from this file 2026-08-16; this file is injected into
every subagent dispatch, and the history was ~90KB of per-dispatch context cost).
Record new changelog entries there, same table format. Consult it before assuming a
defect or behaviour is current -- entries are dated observations, newest last.

**Open decisions deferred for later:** see `open-decisions.md` -- redaction model choice, document-assembly template rules (structure defined in `templates/` 2026-07-13, section presence/order enforcement built 2026-07-14; only 실손형/기타형 ground-truth basis still open), and the vision-model PII-exposure risk in P8's cross-validation. **Known gaps tracked for follow-up:** see `known-gaps.md` -- the CASE_002 D1 near-miss still sitting on disk, two `ocr_extract.py` bugs, no test suite, unreviewed frontend.
