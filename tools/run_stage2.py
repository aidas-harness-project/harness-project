"""Stage 2 (document_processing) end to end in one invocation.

WHY THIS EXISTS -- measured, not assumed.

`run_document_stage.py` removed the serialization *within* checkpoint 1 and
within checkpoint 2, and its own docstring names the disease exactly: "The
serialization was therefore in a SPEC, not in code." The same disease was still
present one level up. Six of Stage 2's eleven steps had no driver, so the agent
executed them one at a time, and between each pair of tool calls it had to read
the result, decide what came next, and emit the next call.

Measured on CASE_911 (RUN_20260812_002), `document_processing` ran 897.4s:

    provider calls (OCR / redaction / classify / judge)   ~360s    40%
    tool process startup (48 invocations x 0.51s)          ~25s     3%
    agent round trips BETWEEN tool calls                  ~510s    57%

The gaps sit between spans, not inside them: `lock.acquire` -> 59s of nothing
-> `lock.acquire`, with lock wait measured at 0.0s. A bare `dao.py
read-contract` subprocess costs 0.51s, so tool startup cannot explain gaps of
20-250s. What fills them is model inference deciding the next step of a
sequence that was already fully determined.

WHAT IS ACTUALLY DETERMINISTIC HERE

Every step this driver performs is mechanical -- the agent spec
(`.claude/agents/document-pipeline.md`) states each one as a rule, not as a
judgement:

  * which documents need checkpoint 1  -> selected from the manifest
  * when a document is classified      -> after redaction, so the classifier
                                          reads the redacted layer; a bundle
                                          awaiting split is excluded because
                                          one label cannot describe it
  * whether a PDF is a bundle          -> "Do not decide in advance which PDFs
                                          are bundles"; `propose` reports it
  * classify children with classify-only, never `run` -> always; `run` refuses
  * child type from a printed form title -> deterministic rule in checkpoint 1
  * downstream_disposition from type   -> set by checkpoint 1
  * which documents chunking excludes  -> manifest `expert_review_only`

WHAT THIS DRIVER DELIBERATELY DOES NOT DECIDE

Three decision points in Stage 2 are real, and every one of them is a HUMAN
gate, not agent reasoning. The driver stops and reports; it never answers them:

  * a P8 disagreement            -- a human picks a reading, or supplies a
                                    corrected transcription
  * a `raw_page_text` classification -- a human answers whether the evidence
                                    quote survives into the redacted text
  * a possible PII leak          -- a privacy event that must stop everything

Stopping at these is the point, not a limitation, for the same reason
`--single-reader` is orchestrator-owned rather than the agent's to choose.

Segmentation boundary approval was a fourth such gate until 2026-08-20, when it
was removed on the user's instruction: a `pending` proposal is now auto-approved
and split without stopping. The approval record still exists and still names a
reviewer -- it reads `<held_by> (auto-approved)` rather than a person, so a
later reader can tell which boundaries a human actually saw. `propose` still
flags `undecided_pages`, and a `rejected` proposal is still honoured rather than
overwritten; what changed is only that nobody is asked about a `pending` one.
The tradeoff accepted here: over-splitting stays cheap to undo by a later merge,
while an over-merge now propagates a wrong `document_type` downstream with no
human in its path.

FAILURE SEMANTICS

Phases run in order and a failed phase stops the ones after it, because each
genuinely depends on its predecessor's output (you cannot segment text that was
never extracted). Within a phase, the existing per-document isolation is
unchanged -- one blocked document does not take down its siblings. The exit
code is non-zero if any phase did not complete, and the JSON report always
names the phase that stopped and why.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import dao as _dao
# tools/trace.py, not the stdlib `trace` module.
import trace as trace_mod
from llm_providers import SUPPORTED_PROVIDERS

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
SCRATCH_ROOT = ROOT / "_stage2_scratch"

# Statuses that mean "this phase did its job". Enumerated from what the tools
# actually emit rather than guessed, and kept as an explicit ALLOW-list so an
# unfamiliar status stops the driver instead of being waved through:
#
#   run_checkpoint1.py  success, passed, already_extracted, bundle_ocr_complete,
#                       non_text_verified
#   run_document_stage  success
#   redact_document.py  success
#   segment_case.py     proposed, approval_applied, split, already_split
#
# `passed` is the one that mattered: `classify-only` returns it on success, and
# an earlier version of this set omitted it, so the driver halted on a
# correctly-classified document. The failure was silent in the worst way -- the
# tool exited 0 and did its work, and only the driver disagreed.
#
# Deliberately EXCLUDED, because each needs a human and not a retry:
# blocked_disagreement (P8), blocked_segmentation, partially_resolved,
# not_ready, partial, inconsistent_existing_split, manifest_write_failed,
# failed, error.
_PHASE_OK_STATUSES = frozenset({
    "success", "passed", "already_extracted", "bundle_ocr_complete",
    "non_text_verified", "proposed", "approval_applied", "split",
    "already_split",
})


def _run(argv: list[str], *, phase: str, progress) -> dict:
    """Run one pipeline tool as a subprocess and capture its verdict.

    Subprocess rather than in-process import: these tools each own
    `trace.configure_from_args`, argparse validation, and their own exit-code
    contract, and re-implementing that here would be a second place for the
    two to disagree. The cost is 0.51s of interpreter start per call, measured
    -- against agent round trips of 20-250s, which is what this replaces.
    """
    progress(f"  $ {' '.join(argv[1:])}")
    proc = subprocess.run(
        [sys.executable, *argv], cwd=str(ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = (proc.stdout or "").strip()
    payload: dict | None = None
    if out:
        # These tools print a JSON verdict on stdout; some also print human
        # progress lines first, so take the last decodable JSON object.
        for start in range(len(out)):
            if out[start] != "{":
                continue
            try:
                payload = json.loads(out[start:])
                break
            except json.JSONDecodeError:
                continue
    return {
        "phase": phase,
        "returncode": proc.returncode,
        "result": payload,
        "stdout_tail": out[-2000:] if payload is None else None,
        "stderr_tail": (proc.stderr or "").strip()[-2000:] or None,
    }


def _manifest(case_id: str) -> dict:
    return _dao.read_contract_data(case_id, "document_manifest.json") or {}


def pending_bundles(manifest: dict) -> list[dict]:
    """PDFs whose segmentation proposal/split still has to run.

    ``pending_review`` is the intake state.  The proposal is what supplies the
    page-boundary evidence a reviewer (or the explicit plumbing-run bypass)
    approves, so waiting for somebody to change the manifest to ``required``
    before proposing creates a circular gate: nothing ever produces the
    evidence needed to make that decision.
    """
    return [d for d in manifest.get("documents", [])
            if d.get("file_format") == "pdf"
            and d.get("downstream_disposition") != "superseded_bundle"
            and d.get("segmentation_status") in {"pending_review", "required"}]


def _proposal(case_id: str, doc_id: str) -> dict | None:
    """DAO-only read of an existing proposal for interruption-safe resume."""
    return _dao.read_contract_data(
        case_id, f"segmentation_proposal_{doc_id}.json")


def unclassified_children(manifest: dict,
                          reclassify: str | None = None) -> list[dict]:
    """Split children that still owe a classification.

    A child inherits its pages from the bundle, so it must be classified with
    `classify-only`; `run` would re-OCR pages it already owns and overwrite
    their P8 history (this really happened to CASE_909's DOC_006-013).

    `reclassify` re-opens children that ALREADY carry a type, which the default
    selection skips. It exists because a document's type is a verdict under one
    taxonomy, and the taxonomy changes: `legal_opinion`/`legal_reference` were
    added 2026-08-20 and three administrative codes 2026-08-22, but every case
    processed before those dates keeps its old answer forever, because
    `not document_type` is false for it. Measured on a CASE_701 fork the day
    after the second change: Stage 2 completed, made ZERO classification calls,
    and the manifest came out byte-identical -- a run that looked like a
    revalidation and revalidated nothing.

    Two values, and the narrow one is the default worth reaching for:

    * `"other"` -- only documents that landed in the catch-all bucket. This is
      the taxonomy-change case: a code was added, and the question is whether
      anything previously homeless now has a home. It cannot change a verdict
      that already succeeded, so it is safe to run broadly.
    * `"all"` -- every classified child. Re-opens verdicts that are already
      settled and may CHANGE them, since a classifier call is not deterministic.
      For a prompt or model change, where the old answers are what is in doubt.

    Neither touches a `superseded_bundle`: its children own its pages, and it
    carries `document_type: null` by construction rather than by omission.

    **Reclassification is not restricted to split children.** The default
    selection requires `source_file_name` because a top-level document is
    classified inside checkpoint 1, at the moment it is OCR'd -- so "not yet
    classified" really does mean "a split child". Re-classification asks a
    different question: not "who still owes a verdict" but "whose verdict was
    reached under the old taxonomy", and that is answered the same way whether
    a PDF held one document or thirty.

    Restricting it anyway is what the first corpus pass did, and it silently
    skipped 13 of the 184 `other` documents -- among them ten 법률질의회신서
    read at 0.95 confidence whose `legal_opinion` bucket had existed since
    2026-08-20. They were never attempted, so nothing reported a failure; they
    simply stayed in the catch-all. A document qualifies here by having pages
    of its own, which is what `file_path` records, not by how it came to exist.
    """
    if reclassify not in (None, "other", "all"):
        raise ValueError(f"unknown reclassify mode: {reclassify!r}")

    def wants(document: dict) -> bool:
        document_type = document.get("document_type")
        if not document_type:
            return True
        if reclassify == "all":
            return True
        return reclassify == "other" and document_type == "other"

    def in_scope(document: dict) -> bool:
        if document.get("downstream_disposition") == "superseded_bundle":
            return False
        # `expert_review_only` is a decision about the document, not a missing
        # verdict: it is excluded from the text pipeline entirely, so
        # re-typing it would change nothing downstream.
        if document.get("downstream_disposition") == "expert_review_only":
            return False
        if reclassify:
            return bool(document.get("file_path"))
        return bool(document.get("source_file_name"))

    return [d for d in manifest.get("documents", [])
            if in_scope(d) and wants(d)]


def chunkable_documents(manifest: dict) -> tuple[list[str], list[str]]:
    """(text documents to chunk, documents to exclude as non-text)."""
    text, excluded = [], []
    for doc in manifest.get("documents", []):
        if doc.get("downstream_disposition") == "superseded_bundle":
            continue
        if doc.get("downstream_disposition") == "expert_review_only":
            excluded.append(doc["document_id"])
        elif doc.get("redacted_text_path"):
            text.append(doc["document_id"])
    return text, excluded


# Default fan-out for split-child classification. Measured 2026-08-23 on a
# fixed 24-document workload, each width run twice:
#
#     workers   wall (s)               mean per call
#     4         49.8, 50.0             7.65s
#     8         28.9                   8.90s
#     12        22.5, 28.0, 31.0       9.85-13.89s
#     16        24.1, 29.9             11.49-13.86s
#
# 8 is where the reproducible gain stops. Width 4 repeated to 0.4%, so the
# 4 -> 8 improvement (-42%) is real; 12 and 16 land in the same 27-31s band as
# 8 while their spread grows to 38%, which is contention, not headroom. A first
# single run showed 22.5s at width 12 and would have justified 12 on its own --
# repeating it is what showed that number was one lucky sample.
#
# The value this replaced was 4, chosen on the reasoning that "the provider's
# own in-flight semaphore is the real ceiling anyway". It is not: that
# semaphore is 24 (`DEFAULT_LLM_MAX_INFLIGHT`, whose comment records it as the
# measured OCR knee), and nothing in this range ever reaches it. Classification
# saturates at a third of OCR's width because the calls are shorter -- 7.6s of
# text against 23s of image -- so per-call latency growth overtakes the
# parallel gain much sooner.
_CLASSIFY_WORKERS = 8


def _classify_children(case_id, children, *, common, provider, workers, report):
    """Classify every split child, several at a time, in manifest order.

    Independent by construction: each child writes its own
    `classification_result_DOC_X.json` and folds its fields into the manifest via
    `dao.patch_manifest_document`, a read-modify-write under a single lock hold --
    the same primitive `pool.documents` already relies on in run_document_stage.

    Returns one step per child, ordered as `children` was, NOT by completion.
    The steps list is a receipt a human reads against the manifest; ordering it
    by whichever provider answered first would make two runs of one case produce
    receipts that differ for no reason. Failures are returned like any other
    step -- the caller decides what halts.

    Measured cause, CASE_701: 10 children took 81.4s of wall for 60.4s of
    provider time, the remaining 21.1s being interpreter starts serialized
    between calls.
    """
    concurrency = trace_mod.ConcurrencyProbe()

    def one(child):
        doc_id = child["document_id"]
        argv = common([str(TOOLS / "run_checkpoint1.py"), "classify-only",
                       case_id, doc_id])
        if provider:
            argv += ["--classifier-provider", provider]
        with concurrency.enter():
            return _run(argv, phase=f"classify:{doc_id}", progress=report)

    limit = max(1, min(workers or _CLASSIFY_WORKERS, len(children)))
    if limit == 1 or len(children) == 1:
        return [one(child) for child in children]

    with trace_mod.span("pool.classify_children", category="compute",
                        case_id=case_id, worker_count=limit,
                        items=len(children)) as pool_span:
        # run_in_context: contextvars do not cross into pool workers, so a raw
        # submit would orphan each child's spans at parent None.
        submit = trace_mod.run_in_context(one)
        with ThreadPoolExecutor(max_workers=limit) as pool:
            futures = [pool.submit(submit, child) for child in children]
            # Every future is waited on before any result is inspected: a child
            # that already finished has written its contract, and abandoning the
            # pool early would discard that work while leaving its output on disk.
            steps = [future.result() for future in futures]
        # Bound rather than returned directly: the probe's maximum is only final
        # once every worker has left, so the attribute has to be set after the
        # pool exits and before this span closes.
        pool_span.set(observed_max_concurrency=concurrency.max_observed)
        return steps


def _phase_ok(step: dict) -> bool:
    """Whether a phase completed. Non-zero exit is always a failure; a zero
    exit with an unrecognized status is too, so a new status shows up as a
    stop to look at rather than being silently treated as success."""
    if step["returncode"] != 0:
        return False
    result = step.get("result")
    if not isinstance(result, dict):
        # No JSON verdict but a clean exit (e.g. dao write-contract).
        return True
    status = result.get("status")
    return status is None or status in _PHASE_OK_STATUSES


def run_stage2(
    case_id: str,
    held_by: str,
    run_id: str,
    *,
    provider: str | None = None,
    doc_workers: int | None = None,
    page_workers: int | None = None,
    single_reader: bool | None = None,
    skip_redaction: bool | None = None,
    auto_approve_segmentation: bool = False,
    segmentation_reviewer: str | None = None,
    reclassify: str | None = None,
    progress=None,
) -> dict:
    report = progress or (lambda msg: print(msg, file=sys.stderr, flush=True))

    def redaction_flags(argv: list[str]) -> list[str]:
        """Both redaction phases take the same switch, so it is applied in one
        place -- passing it to the top-level documents but not to the split
        children would redact half the case under a different policy."""
        if skip_redaction is True:
            argv += ["--skip-redaction"]
        elif skip_redaction is False:
            argv += ["--redact"]
        return argv
    steps: list[dict] = []

    def common(extra: list[str]) -> list[str]:
        argv = extra + ["--held-by", held_by, "--run-id", run_id]
        return argv

    def stop(phase: str, reason: str, gate: bool = False) -> dict:
        return {
            "status": "blocked_gate" if gate else "failed",
            "case_id": case_id, "run_id": run_id,
            "stopped_at": phase, "reason": reason, "steps": steps,
        }

    with trace_mod.span("stage2.driver", category="compute", case_id=case_id):
        # ---- phase 1: checkpoint 1 over everything that needs it ------------
        report("phase: checkpoint 1 (OCR)")
        argv = common([str(TOOLS / "run_document_stage.py"), case_id])
        if provider:
            for flag in ("--reader-a", "--reader-b", "--comparator",
                         "--classifier-provider"):
                argv += [flag, provider]
        if doc_workers is not None:
            argv += ["--doc-workers", str(doc_workers)]
        if page_workers is not None:
            argv += ["--workers", str(page_workers)]
        if single_reader is True:
            argv += ["--single-reader"]
        elif single_reader is False:
            argv += ["--dual-read"]
        step = _run(argv, phase="checkpoint1", progress=report)
        steps.append(step)
        if not _phase_ok(step):
            # A P8 disagreement lands here. It is a human decision, never this
            # driver's -- see resolve-disagreement in document-pipeline.md.
            return stop("checkpoint1",
                        "checkpoint 1 did not complete for every document; a P8 "
                        "disagreement or extraction failure needs human "
                        "resolution before Stage 2 can continue", gate=True)

        # ---- phase 2: checkpoint 2 (redaction) ------------------------------
        report("phase: checkpoint 2 (redaction)")
        argv = common([str(TOOLS / "run_document_stage.py"), case_id,
                       "--checkpoint", "2"])
        if provider:
            argv += ["--provider", provider]
        if doc_workers is not None:
            argv += ["--doc-workers", str(doc_workers)]
        argv = redaction_flags(argv)
        step = _run(argv, phase="redaction", progress=report)
        steps.append(step)
        if not _phase_ok(step):
            return stop("redaction",
                        "redaction did not complete; a possible PII leak halts "
                        "the document and is never worked around", gate=True)

        # ---- phase 3: segmentation, per bundle ------------------------------
        manifest = _manifest(case_id)
        bundles = pending_bundles(manifest)
        if bundles:
            report(f"phase: segmentation ({len(bundles)} bundle(s))")
        for bundle in bundles:
            doc_id = bundle["document_id"]
            proposal = _proposal(case_id, doc_id)
            if proposal is None:
                propose_argv = common([
                    str(TOOLS / "segment_case.py"), "propose", case_id, doc_id])
                if provider:
                    propose_argv += ["--provider", provider]
                step = _run(propose_argv,
                            phase=f"segment.propose:{doc_id}", progress=report)
                steps.append(step)
                if not _phase_ok(step):
                    return stop(f"segment.propose:{doc_id}",
                                "segmentation proposal failed or was partial")
                proposal = _proposal(case_id, doc_id)
                if proposal is None:
                    return stop(f"segment.propose:{doc_id}",
                                "proposal command succeeded but no DAO-governed "
                                "proposal exists")

            review_status = proposal.get("review_status")
            if review_status == "rejected":
                return stop(
                    f"segment.approve:{doc_id}",
                    "the existing segmentation proposal was rejected; an "
                    "automatic rerun must not overwrite that decision",
                    gate=True)
            if review_status not in {"pending", "approved"}:
                return stop(f"segment.approve:{doc_id}",
                            f"unknown proposal review_status {review_status!r}")

            if review_status != "approved":
                reviewer = segmentation_reviewer or f"{held_by} (auto-approved)"
                step = _run(common([str(TOOLS / "segment_case.py"), "approve",
                                    case_id, doc_id, "--reviewer", reviewer]),
                            phase=f"segment.approve:{doc_id}", progress=report)
                steps.append(step)
                if not _phase_ok(step):
                    return stop(f"segment.approve:{doc_id}", "approval failed")
                not_ready = (step.get("result") or {}).get("not_ready") or []
                if not_ready:
                    return stop(
                        f"segment.approve:{doc_id}",
                        "proposal is still not ready to split: "
                        + "; ".join(str(item) for item in not_ready),
                        gate=True)

            step = _run(common([str(TOOLS / "segment_case.py"), "split",
                                case_id, doc_id]),
                        phase=f"segment.split:{doc_id}", progress=report)
            steps.append(step)
            if not _phase_ok(step):
                return stop(f"segment.split:{doc_id}", "split failed")

        # ---- phase 4: classify split children -------------------------------
        manifest = _manifest(case_id)
        children = unclassified_children(manifest, reclassify)
        if children:
            report(f"phase: classify {len(children)} split child(ren)")
        if children:
            child_steps = _classify_children(
                case_id, children, common=common, provider=provider,
                workers=doc_workers, report=report)
            steps.extend(child_steps)
            # Reported in manifest order, so the halt names the same child every
            # time regardless of which provider call returned first.
            for step in child_steps:
                if not _phase_ok(step):
                    return stop(step["phase"], "child classification failed")

        # ---- phase 5: redact the children -----------------------------------
        if children:
            report("phase: checkpoint 2 (split children)")
            argv = common([str(TOOLS / "run_document_stage.py"), case_id,
                           "--checkpoint", "2"])
            if provider:
                argv += ["--provider", provider]
            if doc_workers is not None:
                argv += ["--doc-workers", str(doc_workers)]
            argv = redaction_flags(argv)
            step = _run(argv, phase="redaction.children", progress=report)
            steps.append(step)
            if not _phase_ok(step):
                return stop("redaction.children",
                            "child redaction did not complete", gate=True)

        # ---- phase 6: chunking ----------------------------------------------
        manifest = _manifest(case_id)
        text_docs, excluded = chunkable_documents(manifest)
        if not text_docs:
            return stop("chunking", "no document has a redacted_text_path to chunk")
        report(f"phase: chunking ({len(text_docs)} document(s), "
               f"{len(excluded)} excluded)")
        argv = [str(TOOLS / "chunk_text.py"), case_id, *text_docs,
                "--run-id", run_id]
        for doc_id in excluded:
            argv += ["--exclude-non-text", doc_id]
        step = _run(argv, phase="chunking", progress=report)
        steps.append(step)
        if step["returncode"] != 0:
            return stop("chunking", "chunking failed")

        chunks = step.get("result")
        if not isinstance(chunks, dict):
            return stop("chunking", "chunk_text.py produced no JSON verdict")

        # chunk_text.py emits the PAYLOAD only (`chunks`, `excluded_documents`);
        # page_chunks.schema.json also requires the common contract envelope.
        # Whoever ran this by hand was adding those five fields themselves --
        # an undocumented manual step between two documented commands, which is
        # precisely the kind of thing that belongs in a driver rather than in
        # someone's memory. Set here, not in chunk_text.py, because that tool is
        # deliberately a pure deterministic slicer that takes no --held-by and
        # writes nothing through the DAO.
        chunks = {
            "case_id": case_id,
            "component": "document-pipeline",
            "status": "success",
            "run_id": run_id,
            "created_at": _dao.now_iso(),
            **chunks,
        }

        # ---- phase 7: write the page_chunks contract ------------------------
        report("phase: write page_chunks contract")
        SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
                prefix=f"{case_id}_{run_id}_", dir=SCRATCH_ROOT) as temp_dir:
            payload = Path(temp_dir) / "page_chunks.json"
            payload.write_text(json.dumps(chunks, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            step = _run(common([str(TOOLS / "dao.py"), "write-contract", case_id,
                                "page_chunks.json", "--data-file", str(payload),
                                "--schema-name", "page_chunks.schema.json",
                                "--stage", "document_processing"]),
                        phase="write_page_chunks", progress=report)
        steps.append(step)
        if step["returncode"] != 0:
            return stop("write_page_chunks", "page_chunks contract write failed")

    return {
        "status": "success",
        "case_id": case_id,
        "run_id": run_id,
        "documents_chunked": len(text_docs),
        "documents_excluded": excluded,
        "steps": steps,
        "note": ("Stage 2 phases completed. finalize-stage remains the "
                 "orchestrator's call (T13): this driver never moves a "
                 "run-state marker."),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case_id")
    ap.add_argument("--held-by", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--provider", choices=SUPPORTED_PROVIDERS,
                    help="Applied to every provider-backed step in the stage")
    ap.add_argument("--doc-workers", type=int, default=None, metavar="N")
    ap.add_argument("--page-workers", type=int, default=None, metavar="N")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--single-reader", dest="single_reader",
                       action="store_true", default=None,
                       help="Turn P8 off (orchestrator-owned; timing runs only)")
    group.add_argument("--dual-read", dest="single_reader", action="store_false",
                       help="Force full dual-read P8 even under HARNESS_SINGLE_READER")
    skip_group = ap.add_mutually_exclusive_group()
    skip_group.add_argument(
        "--skip-redaction", dest="skip_redaction", action="store_true", default=None,
        help="Skip the redaction MODEL for every document (dev switch, or "
             "HARNESS_SKIP_REDACTION=1). Orchestrator-owned like --single-reader. "
             "The deterministic residual-PII scan still runs and still blocks a "
             "page carrying structured PII; unstructured PII goes unchecked, so "
             "a run using it is not privacy-preserving.")
    skip_group.add_argument(
        "--redact", dest="skip_redaction", action="store_false",
        help="Force the redaction model on even under HARNESS_SKIP_REDACTION.")
    ap.add_argument(
        "--auto-approve-segmentation", action="store_true",
        help="No-op since 2026-08-20: boundary approval is no longer a gate, so "
             "a pending proposal is auto-approved either way. Still accepted so "
             "existing invocations keep working.")
    ap.add_argument(
        "--reclassify", choices=("other", "all"), default=None,
        help="Re-run classification on children that ALREADY carry a type, "
             "which is otherwise skipped. 'other' re-opens only the catch-all "
             "bucket -- the taxonomy-change case, and the one to reach for. "
             "'all' re-opens every settled verdict and may change them.")
    ap.add_argument("--segmentation-reviewer", default=None,
                    help="Reviewer name recorded on an auto-approved proposal "
                         "(default: '<held_by> (auto-approved)')")
    args = ap.parse_args(argv)
    trace_mod.configure_from_args(args)

    result = run_stage2(
        args.case_id, args.held_by, args.run_id,
        provider=args.provider,
        doc_workers=args.doc_workers,
        page_workers=args.page_workers,
        single_reader=args.single_reader,
        skip_redaction=args.skip_redaction,
        auto_approve_segmentation=args.auto_approve_segmentation,
        segmentation_reviewer=args.segmentation_reviewer,
        reclassify=args.reclassify,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
