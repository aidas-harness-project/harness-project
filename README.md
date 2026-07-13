# Loss-Adjustment Agent Harness PoC

A 3-week experiment validating an agent harness that takes pseudonymized,
closed insurance-claim cases and produces a **screening report** plus a
**draft loss-adjustment report**, evaluated against the real final report
(the answer key). No models are trained from scratch -- this combines
existing OCR/LLM/retrieval models and validates the harness design itself.

This README is the entry point. The full pipeline/agent/taxonomy reference
is [`pipeline.md`](pipeline.md); the hard rules every agent follows at
every stage live in the `harness-guardrails` and `harness-guardrails-dev`
skills (`.claude/skills/`); session history and open items are tracked in
[`CLAUDE.md`](CLAUDE.md)'s changelog, [`known-gaps.md`](known-gaps.md), and
[`open-decisions.md`](open-decisions.md).

## Core questions

1. Can existing models structure insurance-claim documents well enough to be useful?
2. Can coverage and denial/reduction reasons be extracted at a practically usable level?
3. Can cross-document inconsistencies and key disputed points be caught?
4. Can candidate policy clauses be found as a starting point for adjuster review?
5. Do the resulting rebuttal points and draft report actually save real adjuster time?

## Folder structure

| Path | Layer | Purpose |
| --- | --- | --- |
| `source-cases/`, `case_qna.pdf` | raw (immutable) | Case source material and reference Q&A. **Read-only** -- never modified or deleted |
| `archive/sources/` | raw (immutable) | Archived rough-draft pipeline material, read-only |
| `schemas/` | contract | JSON Schemas validating every component's I/O |
| `tools/` | tooling | `dao.py` (sole data-access path), `intake_case.py`, `validate_output.py`, `document_assembly.py`, `sync_agents.py`, `ocr_extract.py` |
| `tests/` | tooling | `pytest` suite covering the DAO and tools, run against `tmp_path` never real `outputs/`/`data/` |
| `.claude/agents/` | harness | 10 specialized agent definitions (canonical; `.codex/`, `.agents/` are generated copies -- run `tools/sync_agents.py` after editing) |
| `.claude/skills/` | harness | `loss-adjustment-pipeline` (orchestrator), `harness-guardrails` (always-on rules), `harness-guardrails-dev` (PoC-only rules) |
| `data/` | run (gitignored) | Case intake copies and intermediate processing state, entirely DAO-managed |
| `outputs/CASE_XXX/` | deliverables (gitignored, per-case exceptions committed as needed) | Screening report, draft report, evaluation results -- written only via `tools/dao.py` |

No agent reads or writes `outputs/`, `data/`, or a ledger/run-state file
directly -- every access goes through `tools/dao.py` (locking, ledgers,
run-state, schema-validated writes). See `harness-guardrails` P2/P5/P7/P10.

## Pipeline overview

Two phases, matching the real workflow: claim comes in, insurer responds,
you respond to the insurer. Phase 1 is 10 stages (case intake through
evaluation); Phase 2 adds 2 new stages and reuses Phase 1's agents.

| Agent | Role |
| --- | --- |
| `document-pipeline` | OCR with dual-path cross-validation, document classification, redaction, chunking |
| `policy-pipeline` | Policy clause extraction and normalization |
| `claim-analysis` | Core field extraction, coverage identification, case-type classification, requirement matching |
| `consistency-check` | Cross-document inconsistency detection (internal QA) |
| `denial-response` | Denial/reduction reason extraction, policy-clause matching (dependency-triggered, not phase-gated) |
| `denial-validation` | Phase 2: validates insurer denial reasons against evidence, generates rebuttal points |
| `screening-report` | Assembles the internal triage document from Phase 1 outputs |
| `draft-report` | Authors the deliverable draft report (v1 in Phase 1, v2 update in Phase 2) |
| `critic` | Reviews every draft version for unlinked claims and forbidden expressions -- structurally cannot read ground truth |
| `evaluation` | Sole agent permitted to read ground truth, only after human review completes; compares the reviewed draft against the real final report |

Full stage table, internal checkpoints, and I/O contracts: [`pipeline.md`](pipeline.md).

## Ground-truth isolation (the most important design principle)

The final loss-adjustment report inside each `source-cases/` case is the
evaluation answer key. It is never fed to a model as input at any stage
except `evaluation`, which runs only after human review of the draft is
complete.

- `tools/intake_case.py` gates every source file through a per-file review
  ledger (`_source_ledger.json`) before it's usable -- a single rejected
  file blocks the whole case.
- Every other agent, skill, and orchestrator step is structurally barred
  from the ground-truth path.

## Cases

Case source material lives under `source-cases/` (Korean folder/file names,
as collected -- raw source material is the one documentation exception to
the English-only rule):

- `기왕증·퇴행성 기여도로 감액된 케이스` -- pre-existing/degenerative-condition contribution reduction case
- `약관상 지급범위를 두고 다툰 케이스` -- dispute over policy coverage scope (cerebrovascular diagnosis benefit, 4 insurers)
- `후유장해 케이스` -- permanent disability case (humeral fracture, liability adjustment)

## Running the pipeline

Open this repository in Claude Code or a Codex-compatible workspace and ask
in natural language; the `loss-adjustment-pipeline` skill orchestrates the
agents in order.

```
"Process CASE_003"          -> full initial run (intake through evaluation)
"Rerun CASE_003 screening"   -> partial rerun
"Run CASE_003 evaluation"    -> compare against ground truth, produce Go/No-Go material
```

Each stage's output must pass `python tools/dao.py write-contract` (schema
validation via `tools/validate_output.py`) before the next stage proceeds.
Run-mode detection (initial / full rerun / partial rerun / resume-from-
interruption) and per-stage rules are defined in
`.claude/skills/loss-adjustment-pipeline/SKILL.md`.

Checkpoint 1's P8 extraction gate is provider-configurable. `claude-cli`
remains the backward-compatible default, while Codex-compatible runs can
use API-backed providers such as `openai-api` for `--reader-a`,
`--reader-b`, `--comparator`, and `--classifier-provider` when the required
environment credentials are present.

## Tools

| Command | Purpose |
| --- | --- |
| `python tools/dao.py <subcommand>` | Sole data-access path -- locking, ledgers, run-state, conflict tracking, schema-validated writes |
| `python tools/validate_output.py <file.json>` | Standalone schema validation |
| `python tools/intake_case.py <source-cases folder> <CASE_ID>` | Case intake with the D2 per-file review ledger |
| `python tools/document_assembly.py --sections-file <spec.json> --held-by <agent> --run-id <run>` | Renders narrative reports, auto-generates `[E#]` citation tags and sidecar |
| `python tools/sync_agents.py` | Regenerates `.codex/agents/*.toml` and `.agents/skills/*/SKILL.md` from canonical `.claude/` definitions |
| `pytest` | Runs the DAO/tooling test suite |

## More

- Pipeline/agent/taxonomy reference: [`pipeline.md`](pipeline.md)
- Session history and rationale: [`CLAUDE.md`](CLAUDE.md)'s changelog
- Known gaps and follow-ups: [`known-gaps.md`](known-gaps.md)
- Deferred decisions: [`open-decisions.md`](open-decisions.md)
- Hard rules every agent follows: `harness-guardrails` / `harness-guardrails-dev` skills, and [`AGENTS.md`](AGENTS.md) / [`CLAUDE.md`](CLAUDE.md)
