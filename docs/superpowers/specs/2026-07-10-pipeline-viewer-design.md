# Pipeline Viewer -- Design Spec

## Purpose

A presentation-ready frontend that visualizes an actual completed (or paused/errored) case run through the loss-adjustment pipeline, for a mid-point presentation to 손사/의사 professionals. Replaces explaining the process via code/diagrams with something intuitive to walk through live.

Not a permanent production tool for this phase -- built to demo real results from a case run before the presentation, reading whatever is actually on disk in `outputs/CASE_XXX/`.

## Scope

In scope:
- Visualizing the 12-stage pipeline (Phase 1's 10 stages + Phase 2's 2), including internal checkpoints for multi-checkpoint agents (document-pipeline: 3, claim-analysis: 4, policy-pipeline: 3, denial-validation: 2)
- Every halt/pause condition the harness defines: P4 (schema validation failure), P6 (conflict ledger), P8 (extraction cross-validation mismatch), P9 (retry-exhausted), D2 (source-ledger file pending/rejected), P7 (human input awaited)
- Rendering the narrative reports (screening_report.md, draft_report_v1/v2.md) with interactive `[E#]` citations
- Rendering the source ledger and conflict ledger as readable UI, not raw JSON
- Reading real case data from `outputs/CASE_XXX/` -- no fabricated/sample data baked into the frontend

Out of scope (explicitly not building now):
- Live/streaming updates while a run is actively executing (this reads state on request/refresh, not via websockets or polling)
- Authentication, multi-user access, or deployment beyond local use
- Editing capability (approving ledger entries, resolving conflicts) from the UI -- this is a read-only viewer; those actions still go through `dao.py` from the terminal
- Template-rule-aware rendering (task pending in `open-decisions.md` #2) -- the viewer renders whatever sections exist in a report file today

## Architecture

**Backend:** FastAPI app under `frontend/backend/`, imports `tools/dao.py`'s existing read helpers (`load_json`, `case_dir`, `load_run_state`, `load_conflict_ledger`, etc.) directly rather than reimplementing file access -- the DAO stays the single source of truth for how case data is read, even from this new consumer.

Endpoints:
- `GET /api/cases` -- list case IDs found under `outputs/`
- `GET /api/cases/{id}/run-state` -- stage statuses + `human_input_status`, as recorded in `_run_state.json`
- `GET /api/cases/{id}/ledgers` -- `_source_ledger.json` + `_conflict_ledger.json`, merged into one response
- `GET /api/cases/{id}/contract/{name}` -- a stage's raw contract JSON (e.g. `extracted_claim_fields.json`)
- `GET /api/cases/{id}/report/{name}` -- a narrative `.md` document merged with its `.evidence.json` sidecar, so the frontend receives citation data alongside the text rather than having to fetch and cross-reference separately

**Frontend:** React + Vite app under `frontend/web/`. Single page, timeline-first (per the approved design):

- **Stage timeline** -- vertical list of all 12 stages in Phase order, each showing status (passed/failed/in-progress/pending/paused) pulled from `run-state`. Multi-checkpoint agents show their internal checkpoints as sub-items. Clicking a stage expands its detail inline (its contract output, formatted).
- **Paused-state banner** -- when a stage's status reflects a halt, a distinct visual state names which rule triggered it (conflict pending, ledger file pending/rejected, human input awaited, schema validation failure, extraction mismatch, retries exhausted) and what's needed to unblock it, sourced directly from the relevant ledger/run-state field -- never inferred or hardcoded per case.
- **Report viewer** -- renders `screening_report.md` / `draft_report_v1.md` / `draft_report_v2.md` with `[E#]` tags as clickable chips; clicking reveals the cited document/page/quote from the merged sidecar data.
- **Ledger panels** -- source ledger (per-file classification + review status) and conflict ledger (per-conflict sources + verdict) as cards/tables, reachable from the timeline point where they're relevant (Case Intake for the source ledger, Consistency Check/Claim Analysis for the conflict ledger).

## Data flow

1. User runs an actual case through the pipeline via the existing agents/DAO (unchanged) -- this produces real files under `outputs/CASE_XXX/`.
2. User starts the FastAPI backend (`uvicorn` or similar) pointed at the repo root.
3. User starts/opens the React frontend, selects the case, and the frontend calls the API endpoints above to render the current state of `outputs/CASE_XXX/`.
4. To see updated results after rerunning a stage, the user refreshes the browser -- no live push, matches "loosely wired" from the design conversation.

## Error handling

The viewer is read-only and non-destructive by construction -- it never writes to `outputs/` or calls any DAO write path. If a case's files are incomplete or a report's sidecar is missing, the affected panel shows an explicit "not yet available" state rather than a blank or broken view; this is itself informative (matches the harness's own principle of never silently upgrading incomplete data to look complete).

## Repo integration

Lives under `frontend/` (`frontend/backend/`, `frontend/web/`) -- kept separate from `tools/` since this is a presentational consumer of the harness, not part of the pipeline itself. `.gitignore` gets new entries for `frontend/web/node_modules/`, `frontend/web/dist/`, and Python's `__pycache__`/venv under `frontend/backend/` if a dedicated venv is used there.

## Testing

Manual verification against a real case run before the presentation: confirm every one of the 12 stages renders with correct status, at least one paused/halted state is demonstrated end-to-end (triggered deliberately if the demo case doesn't naturally hit one), and both report and ledger views render against real generated data -- not sample/mock data.
