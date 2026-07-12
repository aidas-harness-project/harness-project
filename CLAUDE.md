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
- **No agent reads or writes `outputs/`, `data/`, or a ledger/run-state file directly.** Everything goes through `tools/dao.py` -- see harness-guardrails P2/P5/P7/P10 and harness-guardrails-dev D1/D2. Direct file access bypassing the DAO is exactly the kind of thing these rules exist to prevent.
- The final loss-adjustment report inside `source-cases/` is the evaluation answer key. Never feed it to a model as input.
- Documentation, code, and agent/skill definitions are English. The two exceptions: raw source material (Korean, as collected) and the actual deliverable documents the pipeline produces (screening report, draft report -- Korean, since they're submitted to Korean-speaking professionals).

## Tools

- `python tools/dao.py <subcommand>` -- the sole data-access path (locking, ledgers, run-state, conflict tracking, schema-validated writes). See its module docstring for the full subcommand list.
- `python tools/validate_output.py <file.json>` -- standalone schema validation (also used internally by `dao.py write-contract`).
- `python tools/intake_case.py <source-cases folder> <CASE_ID>` -- case intake with the D2 per-file review ledger.
- `python tools/document_assembly.py --sections-file <spec.json> --held-by <agent> --run-id <run>` -- renders narrative reports and auto-generates `[E#]` citation tags + sidecar (P1). DAO-backed like any other write path: locked, atomic, sidecar schema-validated before either file touches disk.
- `python tools/sync_agents.py` -- regenerates `.codex/agents/*.toml` and `.agents/skills/*/SKILL.md` from the canonical `.claude/` definitions. Run this after editing any `.claude/agents/*.md` or `.claude/skills/*/SKILL.md` -- never hand-edit the generated copies.
- `pytest` -- runs `tests/`, covering the DAO's locking/write-contract/conflict-ledger/run-state paths and the document-assembly/validation/OCR tools. Every filesystem test runs against a `tmp_path`, never the real `outputs/`/`data/` trees.
- Version-controlled with git. Propose a commit when the user asks, or when a meaningful unit of change is complete.

## Harness: loss-adjustment case pipeline

**Goal:** closed-case input → screening report + draft report + evaluation, via 10 specialized agents across 2 phases.

**Trigger:** use the `loss-adjustment-pipeline` skill for case processing, reruns/updates, or evaluation requests. Simple questions about pipeline design can be answered directly from `pipeline.md`.

**Changelog:**

| Date | Change | Scope | Reason |
|---|---|---|---|
| 2026-07-07 | Initial setup (7 agents + 2 skills + 2 tools) | project-wide | -- |
| 2026-07-08 | Notes-first, incremental-write discipline added | skills/component-output-contract | Observation 1: claim-analysis exited without notes (context lost on interruption) |
| 2026-07-08 | Resume-from-interruption + notes-existence check | skills/loss-adjustment-pipeline | Observation 2: promoted file-based-stitching resilience to an intended feature |
| 2026-07-08 | Primary-diagnosis-code selection rule (document-character-based priority) | agents/claim-analysis + wiki/field-extraction | Observation 3: headline KCD misselected in a permanent-disability case (failure type F1) |
| 2026-07-10 | Full restructure: English throughout, root taxonomy cleanup (`source-cases/`, `archive/`), old `component-output-contract`/`loss-adjustment-pipeline` skills replaced by `harness-guardrails` (11 universal rules) + `harness-guardrails-dev` (4 PoC-only rules), entire pipeline redesigned (18+13-step draft → 10+2 stages) with a fresh 10-agent landscape (split from the old 7 to fix 3 identities that bundled unrelated jobs, most notably separating the ground-truth-blind critic pass from the ground-truth-sighted evaluation pass), and a DAO (`tools/dao.py`) making the guardrails structurally enforced rather than prompted. `outputs/` wiped -- CASE_003's old run predates all of this and isn't a valid baseline. | project-wide | Coworker brought a rough pipeline reference; review surfaced that the old skills leaned on dead `wiki/` cross-references (wiki now lives in a separate repo) and several rules were duplicated across files and already drifting between the Claude/Codex/generic copies |
| 2026-07-12 | Pipeline/tooling completeness review (findings in `known-gaps.md`); wrote the 12 output schemas the review found missing (`coverage_result`, `case_type_result`, `requirement_matching_result`, `normalized_policy_clause`, `evidence_validation_result`, `denial_validation_result`, `rebuttal_points`, `draft_report_metadata`, `critic_result`, `expert_review`, `evaluation_result`, `evaluation_summary`) plus a `policy_matches` patch to the pre-existing `denial_reason_result` schema; synced all 8 affected `.claude/agents/*.md` specs to match (via `tools/sync_agents.py`) | schemas/, agents/ | Half the pipeline's stages couldn't complete a DAO `write-contract` checkpoint -- no schema existed for their output, discovered via real test runs (CASE_002/CASE_009) that got further than prior smoke tests and hit this wall directly |

**Open decisions deferred for later:** see `open-decisions.md` -- redaction model choice, document-assembly template rules (pending from the user), and the vision-model PII-exposure risk in P8's cross-validation. **Known gaps tracked for follow-up:** see `known-gaps.md` -- the CASE_002 D1 near-miss still sitting on disk, two `ocr_extract.py` bugs, no test suite, unreviewed frontend.
