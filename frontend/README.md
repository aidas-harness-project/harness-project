# Pipeline Viewer

Read-only viewer for a case's actual run through the loss-adjustment pipeline. See `docs/superpowers/specs/2026-07-10-pipeline-viewer-design.md` for the design.

Run a real case through the pipeline first (via the agents/DAO, unchanged) so there's something under `outputs/CASE_XXX/` to look at.

## Backend

```
cd frontend/backend
python3 -m venv venv          # first time only
./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn main:app --reload --port 8000
```

## Frontend

```
cd frontend/web
npm install                    # first time only
npm run dev
```

Open http://localhost:5173. Select a case from the pills at the top; refresh after rerunning any stage to see updated results (no live push -- read on demand, matches the harness's own file-based design).
