# Pipeline Viewer

Viewer for a case's actual run through the loss-adjustment pipeline. Most panels are read-only. The medical workspace adds authenticated, DAO-mediated lifecycle actions; see `docs/superpowers/specs/2026-07-10-pipeline-viewer-design.md` for the base design.

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

## Medical workspace boundary

The Claim Analysis page stores an entered medical operator token in session-only browser storage and sends it as a bearer credential only to the medical API endpoints. The backend remains localhost-only and revalidates every mutation through the DAO and canonical role/operator policies.

The shipped operator and medical role policies are disabled-by-default and contain no approved tokens or named actors. The workspace therefore fails closed until an approved local policy is installed. Static snapshot builds do not connect to medical records or enable medical actions. This is a technical harness, not operational medical-screening approval.
