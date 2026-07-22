# Loss-Adjustment Agent Harness PoC

A 3-week experiment validating an agent harness that takes pseudonymized,
closed insurance-claim cases and produces a screening report plus a draft
loss-adjustment report, evaluated against the real final report. Full
pipeline/agent/taxonomy reference: `pipeline.md`. Original 3-week plan
(success criteria, Go/No-Go): `POC guide.md` (Korean, not yet reviewed for
accuracy against the current design -- treat as historical planning
material, not a live spec).

## Hard rules

- **Every session follows the rules in `.agents/skills/harness-guardrails/SKILL.md` and `.agents/skills/harness-guardrails-dev/SKILL.md`.** These are the non-negotiable constraints every agent follows in every stage -- read them before touching anything in this pipeline. The `-dev` rules only apply while a ground-truth answer key exists in this repo (the PoC/evaluation phase); they stop applying once there's no ground truth to isolate.
- **Raw sources are immutable**: `source-cases/`, `case_qna.pdf`, `archive/sources/` are read-only. Never modify or delete.
- **No agent reads or writes `outputs/`, `data/`, or a ledger/run-state file directly.** Everything goes through `tools/dao.py` -- see the guardrail rules referenced above (P2/P5/P7/P10/D1/D2). Direct file access bypassing the DAO is exactly the kind of thing these rules exist to prevent.
- The final loss-adjustment report inside `source-cases/` is the evaluation answer key. Never feed it to a model as input.
- Documentation, code, and agent/skill definitions are English. The two exceptions: raw source material (Korean, as collected) and the actual deliverable documents the pipeline produces (screening report, draft report -- Korean, since they're submitted to Korean-speaking professionals).

## Tools

- `python tools/dao.py <subcommand>` -- the sole data-access path (locking, ledgers, run-state, conflict tracking, schema-validated writes). See its module docstring for the full subcommand list.
- `python tools/validate_output.py <file.json>` -- standalone schema validation (also used internally by `dao.py write-contract`).
- `python tools/intake_case.py <source-cases folder> <CASE_ID>` -- case intake with the D2 per-file review ledger.
- `python tools/document_assembly.py --sections-file <spec.json>` -- renders narrative reports and auto-generates `[E#]` citation tags + sidecar (P1).
- `python tools/sync_agents.py` -- regenerates this directory's copies (`.agents/skills/*/SKILL.md`) and the Codex copies (`.codex/agents/*.toml`) from the canonical `.claude/` definitions. Run this after editing any `.claude/agents/*.md` or `.claude/skills/*/SKILL.md` -- never hand-edit the generated copies.
- Version-controlled with git. Propose a commit when the user asks, or when a meaningful unit of change is complete.

## Harness: loss-adjustment case pipeline

**Goal:** closed-case input → screening report + draft report + evaluation, via 10 specialized agents across 2 phases. See `pipeline.md` for the full stage/agent map, and `.agents/skills/loss-adjustment-pipeline/SKILL.md` for the orchestration logic -- consult these directly for case processing, reruns/updates, or evaluation requests. Simple questions about pipeline design can be answered directly from `pipeline.md`.

**Changelog:**

| Date | Change | Scope | Reason |
|---|---|---|---|
| 2026-07-07 | Initial setup (7 agents + 2 skills + 2 tools) | project-wide | -- |
| 2026-07-08 | Notes-first, incremental-write discipline added | skills/component-output-contract | Observation 1: claim-analysis exited without notes (context lost on interruption) |
| 2026-07-08 | Resume-from-interruption + notes-existence check | skills/loss-adjustment-pipeline | Observation 2: promoted file-based-stitching resilience to an intended feature |
| 2026-07-08 | Primary-diagnosis-code selection rule (document-character-based priority) | agents/claim-analysis + wiki/field-extraction | Observation 3: headline KCD misselected in a permanent-disability case (failure type F1) |
| 2026-07-10 | Full restructure: English throughout, root taxonomy cleanup (`source-cases/`, `archive/`), old `component-output-contract`/`loss-adjustment-pipeline` skills replaced by `harness-guardrails` (11 universal rules) + `harness-guardrails-dev` (4 PoC-only rules), entire pipeline redesigned (18+13-step draft → 10+2 stages) with a fresh 10-agent landscape (split from the old 7 to fix 3 identities that bundled unrelated jobs, most notably separating the ground-truth-blind critic pass from the ground-truth-sighted evaluation pass), and a DAO (`tools/dao.py`) making the guardrails structurally enforced rather than prompted. `outputs/` wiped -- CASE_003's old run predates all of this and isn't a valid baseline. | project-wide | Coworker brought a rough pipeline reference; review surfaced that the old skills leaned on dead `wiki/` cross-references (wiki now lives in a separate repo) and several rules were duplicated across files and already drifting between the Claude/Codex/generic copies |
| 2026-07-21 | Expanded the denial/reduction taxonomy from R01-R09/R99 to R01-R21/R99 using the loss adjuster's reviewed frequency workbook. Added frequency as non-evidentiary `x-codebook` metadata, synchronized the human-readable table and schema with regression tests, updated `denial-response`, and removed stale draft-template code ranges. | denial-response, pipeline taxonomy, common schema, draft template, tests | Loss-adjuster review supplied twelve additional operational reason codes and high/medium/low frequency tiers |
| 2026-07-22 | Added the loss adjuster's denial/reduction classification to each R-code as `applicable_decision_types` metadata. R15/R99 support both; R12/R14 remain explicitly unclassified because the reviewed source left those cells blank. Synchronized the pipeline table and regression tests. | pipeline taxonomy, common schema, tests | Loss-adjuster supplied the operational denial-vs-reduction mapping |
| 2026-07-22 | Separated per-reason `decision_type` from `payment_status`, removed `partial_payment` as a decision type, split insurer grounds into contractual/medical-factual/calculation categories, made policy-match source locations mandatory, added downstream policy-link verification, and split screening denial/reduction summaries while retaining the combined boolean for one deprecated compatibility version. | denial-response, denial-validation, screening-report, schemas, templates, tests | User approved the reviewed denial/reduction semantics and prioritized fail-safe missing links over incorrect policy matches |

**Open decisions deferred for later:** see `open-decisions.md` -- redaction model choice, document-assembly template rules (pending from the user), and the vision-model PII-exposure risk in P8's cross-validation.
