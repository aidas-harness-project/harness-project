"""API for the pipeline viewer frontend.

Reads case data from outputs/CASE_XXX/ via tools/dao.py's own helpers --
this backend does not reimplement file access, so it can never drift from
how the DAO actually reads case state. See
docs/superpowers/specs/2026-07-10-pipeline-viewer-design.md.

Also runs cases: /api/upload + /api/cases/{id}/run spawn the Claude CLI in
one-shot print mode (`claude -p`) with a SCOPED tool allowlist
(--allowedTools) rather than --dangerously-skip-permissions -- only the
specific tools/patterns the pipeline needs are pre-approved, everything
else is refused rather than bypassed. This needed no disclaimer and no
prior interactive setup once the prompt-vs-flag ordering was right:
the prompt must come immediately after `-p`, before --allowedTools --
`--allowedTools` consumes every following bare token as another tool
name otherwise, silently swallowing the prompt.

An earlier version used `claude --background --dangerously-skip-permissions`;
dropped because (a) it required accepting a bypass-permissions disclaimer
interactively first, which a backend process can never do itself, and (b)
even after that, --background sessions came up idle/blocked rather than
executing the given prompt -- --background appears to expect a human to
`claude attach` and drive it, not run a task headlessly. Plain `-p` has
no such issue: it runs the prompt and exits.

Run: uvicorn main:app --reload --port 8000  (from frontend/backend/, with venv/ active)
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

import dao  # tools/dao.py
import run_checkpoint1  # tools/run_checkpoint1.py -- resolve_from_raw_ocr for P8 review

app = FastAPI(title="Pipeline Viewer API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

CASE_ID_RE = re.compile(r"^CASE_[A-Za-z0-9_-]+$")
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.(json|md)$")
CONFLICT_ID_RE = re.compile(r"^CONFLICT_[0-9]+$")  # matches conflict_ledger.schema.json's own pattern
DOC_ID_RE = re.compile(r"^DOC_[0-9]+$")  # matches document_manifest.schema.json's own pattern
VERSION_RE = re.compile(r"^v[12]$")
UPLOAD_EXTENSIONS = {".pdf", ".txt", ".md", ".png", ".jpg", ".jpeg", ".tiff", ".xlsx", ".csv"}
UPLOAD_FORBIDDEN_CHARS = set("/\\\0")
UPLOAD_DIR = Path(__file__).resolve().parent / "_uploads"
RUNS_FILE = Path(__file__).resolve().parent / "_runs.json"


def _valid_upload_filename(name: str) -> bool:
    """Real case documents are named in Korean, with spaces and punctuation --
    this blocks actual path-traversal characters rather than restricting to
    ASCII, which would reject every real document this project has."""
    if not name or name in (".", "..") or name.startswith("."):
        return False
    if any(c in UPLOAD_FORBIDDEN_CHARS for c in name):
        return False
    return Path(name).suffix.lower() in UPLOAD_EXTENSIONS


def _valid_case_id(case_id: str):
    if not CASE_ID_RE.fullmatch(case_id):
        raise HTTPException(400, "invalid case_id -- must match CASE_<alnum/-/_>")


def _valid_conflict_id(conflict_id: str):
    """conflict_id is a URL path parameter, so it's attacker-controlled and,
    unvalidated, flows straight into a subprocess argv as a positional
    argument (see _run_dao_cli) -- a value like '--held-by' would be
    consumed by dao.py's argparse as that OPTION instead of the intended
    positional, desyncing the rest of the parse (argument-injection /
    flag-smuggling). Validated against the same pattern
    conflict_ledger.schema.json itself requires, so this also fails fast
    with a clean 400 instead of a deep dao.py error for a genuinely
    malformed id."""
    if not CONFLICT_ID_RE.fullmatch(conflict_id):
        raise HTTPException(400, "invalid conflict_id -- must match CONFLICT_<digits>")


def _valid_doc_id(doc_id: str):
    """Same argument-injection discipline as _valid_conflict_id -- doc_id
    flows into resolve_from_raw_ocr and (indirectly) DAO write paths."""
    if not DOC_ID_RE.fullmatch(doc_id):
        raise HTTPException(400, "invalid doc_id -- must match DOC_<digits>")


def _valid_actor_name(name: str, what: str = "reviewer"):
    """A reviewer/actor name becomes an option VALUE in a dao.py argv (and
    lock metadata) -- require it non-empty and not flag-shaped, so it can
    never be mistaken for an option by a downstream parser."""
    if not name.strip():
        raise HTTPException(400, f"{what} name is required -- this is a real human-audit record")
    if name.lstrip().startswith("-"):
        raise HTTPException(400, f"{what} name must not start with '-'")


def _known_ledger_file_name(case_id: str, file_name: str):
    """file_name comes from the request body, so it's just as attacker-
    controlled as conflict_id above and hits the same subprocess-argv
    positional slot -- same injection risk. Unlike conflict_id there's no
    fixed pattern to check it against, so instead require it to already be
    a real entry in this case's own ledger before it's ever handed to
    dao.py; this closes the injection path (a crafted '--held-by' value
    won't be a real ledger entry) and gives a clear error for a genuine
    typo instead of dao.py's deeper NOT_FOUND."""
    ledger = dao.load_json(dao.source_ledger_path(case_id))
    known = {e["file_name"] for e in (ledger or {}).get("files", [])}
    if file_name not in known:
        raise HTTPException(400, f"{file_name!r} is not a file in this case's ledger")


def _require_case(case_id: str) -> Path:
    """Validates case_id and returns its resolved outputs/ directory. Every
    endpoint that touches an EXISTING case's filesystem goes through this
    rather than building a path from the raw path parameter directly."""
    _valid_case_id(case_id)
    base = (dao.OUTPUTS / case_id).resolve()
    if not base.is_relative_to(dao.OUTPUTS.resolve()) or not base.is_dir():
        raise HTTPException(404, f"case {case_id!r} not found under outputs/")
    return base


def _safe_child(case_dir: Path, name: str) -> Path:
    """Validates a contract/report filename and returns its resolved path,
    refusing anything that isn't a plain filename inside case_dir --
    blocks path traversal via '..' or absolute paths in `name`."""
    if not SAFE_FILENAME_RE.fullmatch(name):
        raise HTTPException(400, "invalid filename")
    candidate = (case_dir / name).resolve()
    if not candidate.is_relative_to(case_dir.resolve()):
        raise HTTPException(400, "invalid filename")
    return candidate


@app.get("/api/cases")
def list_cases():
    if not dao.OUTPUTS.exists():
        return []
    return sorted(p.name for p in dao.OUTPUTS.iterdir() if p.is_dir())


@app.get("/api/cases/{case_id}/run-state")
def run_state(case_id: str):
    _require_case(case_id)
    return dao.load_run_state(case_id)


@app.get("/api/cases/{case_id}/ledgers")
def ledgers(case_id: str):
    _require_case(case_id)
    source_ledger = dao.load_json(dao.source_ledger_path(case_id))
    conflict_ledger = dao.load_conflict_ledger(case_id)
    return {"source_ledger": source_ledger, "conflict_ledger": conflict_ledger}


class LedgerStatusBody(BaseModel):
    file_name: str
    status: str  # approved | rejected
    reviewer: str
    reason: str | None = None


class ConflictVerdictBody(BaseModel):
    verdict: str  # resolved | false_positive
    note: str


def _frontend_run_id() -> str:
    """--held-by/--run-id are lock metadata only (see dao.py's cmd_set_ledger_status/
    cmd_set_conflict_verdict) -- not part of the audit record itself (reviewer
    name + timestamp are captured separately), so a fresh synthetic id per
    request is fine. Must match _run_state.schema.json's run_id pattern
    (^RUN_[0-9]{8}_[0-9]+$), same as intake_case.py's own fallback."""
    return f"RUN_{time.strftime('%Y%m%d')}_{int(time.time() * 1000)}"


def _run_dao_cli(args: list[str], held_by: str) -> dict:
    """Every human-audit write goes through tools/dao.py's own CLI, exactly
    as a person running it from a terminal would -- this backend has no
    special library-level write access. See harness-guardrails D2/P6: the
    human decision itself (not this endpoint) is what the harness trusts."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "dao.py"), *args, "--held-by", held_by, "--run-id", _frontend_run_id()],
        cwd=str(ROOT), capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise HTTPException(400, result.stdout.strip() or result.stderr.strip() or "dao.py rejected this action")
    return {"ok": True, "output": result.stdout.strip()}


@app.post("/api/cases/{case_id}/ledger/status")
def set_ledger_status(case_id: str, body: LedgerStatusBody):
    _require_case(case_id)
    _known_ledger_file_name(case_id, body.file_name)
    if body.status not in ("approved", "rejected"):
        raise HTTPException(400, "status must be approved or rejected")
    if not body.reviewer.strip():
        raise HTTPException(400, "reviewer name is required -- this is a real human-audit record")
    args = ["set-ledger-status", case_id, body.file_name, body.status, "--reviewer", body.reviewer]
    if body.status == "rejected":
        if not (body.reason or "").strip():
            raise HTTPException(400, "a rejection reason is required")
        args += ["--reason", body.reason]
    return _run_dao_cli(args, held_by=body.reviewer)


@app.post("/api/cases/{case_id}/conflicts/{conflict_id}/verdict")
def set_conflict_verdict(case_id: str, conflict_id: str, body: ConflictVerdictBody):
    _require_case(case_id)
    _valid_conflict_id(conflict_id)
    if body.verdict not in ("resolved", "false_positive"):
        raise HTTPException(400, "verdict must be resolved or false_positive")
    if not body.note.strip():
        raise HTTPException(400, "a resolution note is required -- this is a real human-audit record")
    return _run_dao_cli(["set-conflict-verdict", case_id, conflict_id, body.verdict, "--note", body.note],
                         held_by="frontend-reviewer")


@app.get("/api/cases/{case_id}/contract/{name}")
def contract(case_id: str, name: str):
    case_dir = _require_case(case_id)
    path = _safe_child(case_dir, name)
    data = dao.load_json(path)
    if data is None:
        raise HTTPException(404, f"{name} not found for {case_id}")
    return data


@app.get("/api/cases/{case_id}/report/{name}")
def report(case_id: str, name: str):
    """name is the .md filename, e.g. draft_report_v1.md"""
    case_dir = _require_case(case_id)
    doc_path = _safe_child(case_dir, name)
    if not doc_path.exists():
        raise HTTPException(404, f"{name} not found for {case_id}")
    sidecar_path = doc_path.with_suffix(".evidence.json")
    return {
        "markdown": doc_path.read_text(encoding="utf-8"),
        "evidence": dao.load_json(sidecar_path) or {"citations": []},
    }


@app.get("/api/cases/{case_id}/source-file")
def source_file(case_id: str, name: str):
    """Serves the original source document a ledger entry refers to, so the
    human can actually READ what they're approving/rejecting without leaving
    the UI (D2: the review is only meaningful if the reviewer saw the file).
    Read-only serving for a human's eyes -- no agent consumes this endpoint,
    so P2's processed-layer rule isn't in play. `name` must already be a
    real entry in the case's own ledger (same containment logic as the
    audit-write endpoints), which also rules out path traversal: intake and
    upload both record plain basenames only."""
    _require_case(case_id)
    _known_ledger_file_name(case_id, name)
    if any(c in UPLOAD_FORBIDDEN_CHARS for c in name):
        raise HTTPException(400, "invalid file name")
    ledger = dao.load_json(dao.source_ledger_path(case_id)) or {}
    candidates = [UPLOAD_DIR / case_id / name]
    source_dir = ledger.get("source_dir")
    if source_dir and ".." not in Path(source_dir).parts:
        candidates.append(ROOT / source_dir / name)
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file() and resolved.is_relative_to(candidate.parent.resolve()):
            media_type = "application/pdf" if resolved.suffix.lower() == ".pdf" else None
            return FileResponse(resolved, media_type=media_type, filename=name,
                                content_disposition_type="inline")
    raise HTTPException(404, f"source file for {name!r} not found in staging or the ledger's source_dir")


# ------------------------------------------- human decisions in the UI --

class OcrResolveBody(BaseModel):
    doc_id: str
    page: int
    chosen_reading: str  # reading_a | reading_b
    reviewer: str
    note: str


SCRATCH_DIR = ROOT / "_ocr_scratch"


@app.get("/api/cases/{case_id}/ocr-review")
def ocr_review(case_id: str):
    """Everything a human needs to resolve a P8 dual-read disagreement on
    screen: each blocked document's still-unresolved pages with BOTH full
    readings side by side (from the _ocr_scratch raw save run_checkpoint1
    makes whenever a disagreement blocks a run)."""
    case_path = _require_case(case_id)
    documents = []
    for result_path in sorted(case_path.glob("ocr_result_DOC_*.json")):
        data = dao.load_json(result_path)
        if not data or data.get("cross_validation_status") != "disagreed_pending_review":
            continue
        doc_id = data.get("document_id")
        raw = None
        if doc_id and DOC_ID_RE.fullmatch(doc_id):
            raw_path = SCRATCH_DIR / f"{case_id}_{doc_id}_raw.json"
            raw = dao.load_json(raw_path)
        raw_pages = {p["page"]: p for p in (raw or {}).get("pages", [])}
        pages = []
        for p in data.get("pages", []):
            cv = p.get("cross_validation") or {}
            if cv.get("agreement") != "disagreed" or cv.get("resolution"):
                continue
            raw_page = raw_pages.get(p.get("page"), {})
            pages.append({
                "page": p.get("page"),
                "reading_a": raw_page.get("reading_a"),
                "reading_b": raw_page.get("reading_b") or cv.get("vision_model_reading"),
                "disagreement_details": raw_page.get("disagreement_details")
                                         or cv.get("disagreement_details") or [],
            })
        documents.append({
            "doc_id": doc_id,
            "review_reason": data.get("review_reason"),
            "raw_available": raw is not None,
            "pages": pages,
        })
    return {"documents": documents}


@app.post("/api/cases/{case_id}/ocr-resolve")
def ocr_resolve(case_id: str, body: OcrResolveBody):
    """A human picks which of the two independent readings is correct for one
    disagreed page -- the P8 resolution gate, driven from the UI. Delegates
    to run_checkpoint1.resolve_from_raw_ocr, the same path a terminal
    resolution uses; when the last page of a document resolves, that function
    continues into classification + manifest update, so this request can take
    a while (one real model call) -- the UI shows a busy state meanwhile."""
    _require_case(case_id)
    _valid_doc_id(body.doc_id)
    _valid_actor_name(body.reviewer)
    if body.chosen_reading not in ("reading_a", "reading_b"):
        raise HTTPException(400, "chosen_reading must be reading_a or reading_b")
    if not body.note.strip():
        raise HTTPException(400, "a resolution note is required -- what did you verify, and how?")
    raw_path = SCRATCH_DIR / f"{case_id}_{body.doc_id}_raw.json"
    ocr_data = dao.load_json(raw_path)
    if ocr_data is None:
        raise HTTPException(409, f"no raw dual-read data at {raw_path.name} -- this disagreement predates "
                                 "the scratch save; re-run checkpoint 1 to regenerate both readings")
    try:
        result = run_checkpoint1.resolve_from_raw_ocr(
            case_id, body.doc_id, ocr_data, body.page, body.chosen_reading,
            resolved_by=body.reviewer, note=body.note,
            held_by=body.reviewer, run_id=_frontend_run_id(),
        )
    except SystemExit as e:  # resolve_from_raw_ocr sys.exit()s on a bad page/reading
        raise HTTPException(400, str(e))
    return result


class HumanReviewCompleteBody(BaseModel):
    version: str  # v1 | v2
    reviewer: str


@app.get("/api/cases/{case_id}/human-review")
def human_review(case_id: str):
    """State of the critic -> human review -> evaluation gate (P7/D1) per
    draft version, so the UI can show exactly what the pipeline is waiting
    on and whether the gate is already open."""
    case_path = _require_case(case_id)
    out = {}
    for version in ("v1", "v2"):
        flag = dao.load_json(dao.human_review_flag_path(case_id, version))
        out[version] = {
            "reviewed_draft_exists": (case_path / f"draft_report_{version}_reviewed.md").exists(),
            "expert_review_exists": (case_path / f"expert_review_{version}.json").exists(),
            "review_complete": flag is not None,
            "completed_by": (flag or {}).get("reviewer"),
            "completed_at": (flag or {}).get("marked_complete_at"),
        }
    return out


@app.post("/api/cases/{case_id}/human-review-complete")
def human_review_complete(case_id: str, body: HumanReviewCompleteBody):
    """The human marks their expert review done, opening D1's versioned gate.
    dao.py itself enforces the hard precondition (expert_review_v{N}.json
    must exist and validate) -- this endpoint only relays the human's action,
    it cannot self-certify past that check."""
    _require_case(case_id)
    _valid_actor_name(body.reviewer)
    if not VERSION_RE.fullmatch(body.version):
        raise HTTPException(400, "version must be v1 or v2")
    return _run_dao_cli(["mark-human-review-complete", case_id, body.version, "--reviewer", body.reviewer],
                        held_by=body.reviewer)


# ---------------------------------------------------------- upload + run --

def _load_runs() -> dict:
    return json.loads(RUNS_FILE.read_text(encoding="utf-8")) if RUNS_FILE.exists() else {}


def _save_runs(runs: dict) -> None:
    RUNS_FILE.write_text(json.dumps(runs, indent=2), encoding="utf-8")


@app.post("/api/upload")
async def upload(case_id: str, files: list[UploadFile]):
    """Stages uploaded documents for a NEW case (not yet under outputs/ --
    it doesn't exist until the run actually produces something)."""
    _valid_case_id(case_id)
    if not files:
        raise HTTPException(400, "no files uploaded")
    dest = (UPLOAD_DIR / case_id).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        # filename is attacker-controlled: strip to a basename, then require it
        # pass the character/extension check and resolve to confirm it still
        # lands inside dest (belt-and-suspenders on top of that check).
        name = Path(f.filename or "").name
        if not _valid_upload_filename(name):
            raise HTTPException(400, f"rejected filename {name!r} -- must be a plain name with an allowed extension")
        candidate = (dest / name).resolve()
        if not candidate.is_relative_to(dest):
            raise HTTPException(400, f"rejected filename {name!r}")
        content = await f.read()
        candidate.write_bytes(content)
        saved.append(name)
    return {"case_id": case_id, "staged_at": str(dest), "files": saved}


LOGS_DIR = Path(__file__).resolve().parent / "_run_logs"

# Scoped tool allowlist for unattended runs -- see module docstring. Bash is
# restricted to the pipeline's own tools, not a bare shell; Write/Edit are
# NOT path-scoped by this flag (a real residual risk, see the design spec).
ALLOWED_TOOLS = "Read Write Edit Glob Grep Task Agent Bash(python3 tools/*) Bash(python tools/*)"


@app.post("/api/cases/{case_id}/run")
def run_case(case_id: str):
    """Launches Claude Code in one-shot print mode with a scoped tool
    allowlist to drive the actual pipeline for this case, unattended."""
    _valid_case_id(case_id)
    staging = UPLOAD_DIR / case_id
    if not staging.is_dir() or not any(staging.iterdir()):
        raise HTTPException(400, f"no uploaded documents staged for {case_id} -- call /api/upload first")

    prompt = (
        f"Process case {case_id} through the loss-adjustment-pipeline.\n"
        f"Source documents are at: {staging}\n"
        "Steps:\n"
        "1. Run intake (tools/intake_case.py) with --init-ledger, review the proposed "
        "raw/ground_truth classification yourself, and set each file's review status via "
        "tools/dao.py set-ledger-status (approved/rejected) based on that review.\n"
        "2. Once every file is approved, run intake with --execute to copy them.\n"
        "3. Proceed through Phase 1 of the pipeline (document processing through the "
        "draft report v1), following harness-guardrails and harness-guardrails-dev throughout.\n"
        "4. If you hit a hard guardrail halt (a conflict, an extraction mismatch, retries "
        "exhausted, or anything else) stop cleanly and do not fabricate a resolution -- "
        "the halt state will already be visible in the run-state/ledgers for a human to "
        "review later. You are working autonomously; there is no human available to answer "
        "questions during this run.\n"
        "Memory: do not write anything to your persistent auto-memory system during this run. "
        "This is real (redacted) case data, not development context -- case content must never "
        "become a memory entry, and this run must not be shaped by unrelated development "
        "memories either. Treat this as an isolated, single-purpose task."
    )

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"{case_id}.log"
    log_file = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        ["claude", "-p", prompt, "--allowedTools", ALLOWED_TOOLS],
        cwd=str(ROOT), stdout=log_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
    )
    _PROCS[case_id] = proc

    runs = _load_runs()
    runs[case_id] = {"pid": proc.pid, "log_path": str(log_path), "started_at": time.time(), "status": "running"}
    _save_runs(runs)
    return {"case_id": case_id, "pid": proc.pid, "status": "launched"}


# Live Popen handles for runs launched by THIS backend process -- the only
# way to observe a child's real exit code (and to reap it: a plain
# os.kill(pid, 0) liveness probe reports a zombie child as alive forever,
# which is exactly how a finished/crashed run used to stay "running" until
# the backend restarted).
_PROCS: dict[str, subprocess.Popen] = {}


def _pid_alive(pid: int) -> bool:
    try:
        # signal 0 does no actual signaling, just checks the process exists
        # and is ours to signal -- standard liveness check on POSIX.
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


@app.get("/api/cases/{case_id}/run-status")
def run_status(case_id: str):
    """Honest run lifecycle for the UI: `running` only while the child is
    genuinely alive; a zero exit is `finished`, a non-zero exit is `crashed`
    (with the exit code), and a run whose Popen handle was lost to a backend
    restart ends as `ended_unknown` rather than being dressed up as finished.
    log_age_seconds lets the UI flag a run that's alive but silent (stuck)."""
    _valid_case_id(case_id)
    runs = _load_runs()
    entry = runs.get(case_id)
    if not entry:
        return {"status": "not_started"}
    if entry["status"] == "running":
        proc = _PROCS.get(case_id)
        if proc is not None and proc.pid == entry["pid"]:
            exit_code = proc.poll()  # also reaps the child if it exited
            if exit_code is not None:
                entry["status"] = "finished" if exit_code == 0 else "crashed"
                entry["exit_code"] = exit_code
                entry["ended_at"] = time.time()
                runs[case_id] = entry
                _save_runs(runs)
                _PROCS.pop(case_id, None)
        elif not _pid_alive(entry["pid"]):
            # Launched by a previous backend process: the exit code is gone
            # with it. Say so instead of guessing success.
            entry["status"] = "ended_unknown"
            entry["ended_at"] = time.time()
            runs[case_id] = entry
            _save_runs(runs)
    log_tail, log_size, log_age_seconds = "", None, None
    log_path = Path(entry["log_path"])
    if log_path.exists():
        log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
        stat = log_path.stat()
        log_size = stat.st_size
        log_age_seconds = round(time.time() - stat.st_mtime, 1)
    return {
        "status": entry["status"], "pid": entry["pid"], "started_at": entry["started_at"],
        "ended_at": entry.get("ended_at"), "exit_code": entry.get("exit_code"),
        "log_tail": log_tail, "log_size": log_size, "log_age_seconds": log_age_seconds,
    }
