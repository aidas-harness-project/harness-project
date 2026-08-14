"""Document-pipeline checkpoint 2: redact validated page text via a Redactor.

All case data access goes through tools/dao.py. Redaction itself goes through
the `redaction.Redactor` abstraction (today: `LlmRedactor` over any configured
provider), so a future dedicated de-identification model can drop in without
changing this tool. Dev-phase default provider is `claude-cli`.

Redaction is span-substitution, not page rewriting: the model only IDENTIFIES
PII values and `redaction.py` deterministically replaces them in the source, so
non-PII content is preserved by construction (omission/fabrication impossible).
A possible PII leak -- structured PII surviving in the output, or a model-named
value not found verbatim in the source -- HARD-FAILS the document (RedactionLeakError,
nothing written), like a P8 disagreement. Over-redaction risk (a span left
un-redacted to avoid corrupting kept text) is privacy-safe and only sets
review_required. A redaction is never trusted silently.

Usage:
    python tools/redact_document.py CASE_ID DOC_ID \
        --held-by document-pipeline --run-id RUN_ID \
        --provider claude-cli --model MODEL
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import dao
# tools/trace.py, not the stdlib `trace` module.
import trace as trace_mod
from llm_providers import (
    ProviderConfig,
    ProviderConfigError,
    ProviderExecutionError,
    SUPPORTED_PROVIDERS,
    build_provider,
)
from redaction import (PROMPT_VERSION, DevNoLlmRedactor, LlmRedactor,
                       NoPiiClassRedactor, RedactionLeakError, RedactionOutcome,
                       RedactionParseError)


ROOT = Path(__file__).resolve().parent.parent
DAO = ROOT / "tools" / "dao.py"
# claude-cli, not codex-cli: on Windows the codex npm shim installs as
# `codex.CMD`, which `shutil.which` resolves but `subprocess.run(["codex"])`
# cannot launch (WinError 2) -- a batch file needs a shell, not execve. The
# default has to be a provider that actually starts, so an operator who passes
# no --provider gets a working redaction rather than a FileNotFoundError.
# codex-cli remains selectable via --provider / HARNESS_REDACTION_PROVIDER.
DEFAULT_REDACTION_PROVIDER = "claude-cli"


# Bumped when the CACHE ENTRY's own shape changes (not when redaction changes
# -- that is what fingerprint() covers). An old entry with a different version
# is treated as a miss rather than migrated.
CACHE_FORMAT_VERSION = 1

# The work is provider round-trips, not local computation, so the ceiling is
# the backend's rate limit and (for CLI providers) one child process per
# concurrent call -- not CPU count.
#
# This said "Matches ocr_extract's default" while sitting at 4 and
# DEFAULT_OCR_WORKERS sat at 16 -- the claim silently stopped being true when
# OCR was raised (8 -> 16 on 2026-08-11) and this was not. Measured on
# CASE_911, that desync is what made redaction the slower half of the same
# 34 scanned pages: OCR cleared them at 15-16 workers in 35-42s per document
# while redaction took 25.0s (DOC_002, 15p) and 32.7s (DOC_005, 19p) at 4.
#
# Now pinned to the same measured knee as OCR (12; see
# llm_providers.DEFAULT_LLM_MAX_INFLIGHT for the curve) rather than restating
# the other module's value in prose, which is the drift that caused this.
# The process-wide in-flight cap is the real ceiling regardless of what this
# is set to, so raising it cannot push total concurrency past the knee.
DEFAULT_REDACT_WORKERS = 12
REDACT_WORKERS_ENV = "HARNESS_REDACT_WORKERS"


def _resolve_workers(max_workers: int | None) -> int:
    """Explicit argument wins, then HARNESS_REDACT_WORKERS, then the default.
    An unparseable or non-positive env value falls back rather than raising --
    a malformed worker count must not turn every redaction into a hard
    configuration failure, and 0 or negative would mean no workers at all."""
    if max_workers is not None:
        return max(1, max_workers)
    raw = str(os.environ.get(REDACT_WORKERS_ENV, "")).strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_REDACT_WORKERS
        if value > 0:
            return value
    return DEFAULT_REDACT_WORKERS


def _resume_cache_dir(case_id: str, doc_id: str) -> Path:
    """Per-document dir holding one JSON per completed page.

    Mirrors ocr_extract._resume_cache_dir: stable (not pid-tagged) so it
    survives across runs, one file per page so there is exactly one writer per
    file and no lock is needed (see tools/trace.py's docstring for the same
    argument stated in full).
    """
    # ROOT is resolved at CALL time, not import time. Tests monkeypatch
    # rd.ROOT to a tmp_path; a module-level SCRATCH_ROOT would be frozen
    # before that patch and every test would write into the real repository
    # scratch dir -- which is not hypothetical, it happened while building
    # this: two tests sharing CASE_009/DOC_001 leaked cache entries into each
    # other and turned a passing leak-detection test into a failure.
    return ROOT / "_redaction_scratch" / "_resume" / f"{case_id}_{doc_id}"


def _cache_fingerprint(page_text: str, redactor) -> str:
    """What the cached redaction is only valid FOR.

    Every input that can change a redaction's OUTPUT is in here:

    * `PROMPT_VERSION` -- the plan is explicit that serving a redaction
      produced by an older prompt is "not a performance defect but a privacy
      defect". A prompt revision usually means the previous one missed
      something, so an entry from before it must never be reused.
    * `redactor.label` -- method + provider + model. A different model is a
      different redactor; NoPiiClassRedactor and LlmRedactor must never share
      an entry, since the former deliberately redacts nothing.
    * sha256 of the exact page text -- if checkpoint 1's output changed (a P8
      disagreement resolved, a page re-transcribed), the old redaction was
      computed against text that no longer exists.

    A mismatch on any of them is a miss, and a miss re-runs the real call.
    Nothing here is a heuristic: the entry either was produced by this exact
    combination or it was not.
    """
    digest = hashlib.sha256(page_text.encode("utf-8")).hexdigest()
    label = getattr(redactor, "label", getattr(redactor, "method", "unknown"))
    return f"{CACHE_FORMAT_VERSION}:{PROMPT_VERSION}:{label}:{digest}"


def _load_cached_page(cache_dir: Path, page: int, fingerprint: str):
    """Return the cached RedactionOutcome for this page, or None.

    Fails closed in every ambiguous case -- unreadable file, malformed JSON,
    missing fingerprint, fingerprint mismatch -- because the cost of a miss is
    one provider call while the cost of a wrong hit is serving a stale
    redaction.
    """
    p = cache_dir / f"page_{page:03d}.json"
    if not p.exists():
        return None
    try:
        entry = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None  # corrupt/partial entry -> re-redact this page
    if not isinstance(entry, dict) or entry.get("fingerprint") != fingerprint:
        return None
    outcome = entry.get("outcome")
    if not isinstance(outcome, dict) or "redacted_text" not in outcome:
        return None
    return RedactionOutcome(
        redacted_text=outcome["redacted_text"],
        items_redacted=int(outcome.get("items_redacted") or 0),
        categories=list(outcome.get("categories") or []),
        provider_metadata=outcome.get("provider_metadata"),
        review_warnings=list(outcome.get("review_warnings") or []),
        spans=outcome.get("spans"),
    )


def _save_cached_page(cache_dir: Path, page: int, fingerprint: str, outcome) -> None:
    """Persist one page's redaction. Atomic tmp->replace, so an interrupt
    mid-write never leaves a half-entry that a later run would trust.

    Only leak-free outcomes reach here: redact_page raises on a detected leak
    and nothing is cached for that page, so a failing document cannot poison
    the cache with a partial result."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "fingerprint": fingerprint,
        "outcome": {
            "redacted_text": outcome.redacted_text,
            "items_redacted": outcome.items_redacted,
            "categories": list(outcome.categories),
            "provider_metadata": outcome.provider_metadata,
            "review_warnings": list(outcome.review_warnings),
            "spans": outcome.spans,
        },
    }
    tmp = cache_dir / f"page_{page:03d}.json.tmp"
    tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cache_dir / f"page_{page:03d}.json")


def _dao(*args: str, capability: str | None = None) -> str:
    """Run a DAO subcommand as a subprocess.

    Only the WRITE path still uses this. The per-page read moved in-process
    (see redact_document), which is where the cost was: a subprocess per page
    multiplied ~0.38s of interpreter startup by page count. The remaining
    calls are a fixed three per document, and they go through write-contract's
    schema + cross-contract validation, which is not worth re-implementing
    in-process to save a constant.

    `capability` is retained for callers that still need the subprocess form:
    it travels through the child environment rather than argv so it does not
    land in a process listing, and it is minted fresh per run so it cannot be
    replayed from a log. The in-process read passes the same token as an
    argument instead, which keeps it out of the environment entirely.
    """
    env = dict(os.environ)
    if capability is not None:
        env[dao.PAGE_TEXT_CAPABILITY_ENV] = capability
    # Instrumented to quantify B1's per-page tax: this spawns a fresh Python
    # interpreter for every page of every document, and a bare `import dao`
    # subprocess measures ~0.35s. Only the subcommand name is recorded -- the
    # rest of argv carries case/doc/page identifiers, and the capability secret
    # travels in env and must never reach a trace file.
    with trace_mod.span("subprocess.dao", category="subprocess",
                        argv0="dao.py",
                        subcommand=args[0] if args else None) as sp:
        result = subprocess.run(
            [sys.executable, str(DAO), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=str(ROOT), env=env,
        )
        sp.set(exit_code=result.returncode)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"dao.py {' '.join(args[:2])} failed: {detail}")
        return result.stdout


# Which extraction outcomes checkpoint 2 will redact.
#
# What this gate exists to stop is a document whose extraction question is
# still OPEN -- `disagreed_pending_review`, where two reads conflict and no
# human has said which is right. Redacting that would build a deliverable on
# text nobody has settled, and every downstream stage would treat the result as
# established fact.
#
# It is NOT a quality bar, and reading it as one is what broke it twice. Both
# throughput modes produce text whose extraction question is CLOSED -- closed by
# a recorded policy rather than by agreement, which is exactly what their status
# value says out loud:
#
#   * assume_reading_a_unreviewed -- the reads disagreed and reading_a was taken
#     under an explicit --on-disagreement policy (added 2026-08-11; this set was
#     never updated, so that flag could not reach checkpoint 2 at all).
#   * single_reader_no_cross_validation -- one read, no comparison, under
#     --single-reader. Nothing is pending, because nothing was ever compared.
#
# Both keep review_required true and carry ocr_quality 'low', so the honest
# grade travels with the document. Blocking them here does not make the text
# safer -- it makes the mode unusable, which is how CASE_911 stalled with its
# only two scanned documents unredactable while the three born-digital ones
# sailed through on the embedded-text path.
#
# `not_run` and `non_text_verified` stay OUT deliberately: the first has no
# text to redact, the second is expert-review-only visual evidence that must
# never be fed to the text redactor.
REDACTABLE_CROSS_VALIDATION_STATUS = frozenset({
    "agreed",
    "disagreed_resolved",
    "assume_reading_a_unreviewed",
    "single_reader_no_cross_validation",
})


def redact_document(case_id: str, doc_id: str, held_by: str, run_id: str, redactor,
                    *, resume: bool = True, max_workers: int | None = None) -> dict:
    # read_contract_data is the DAO's own in-process contract read -- same
    # path resolution and traversal guard as the CLI, without paying an
    # interpreter start to get it.
    ocr_result = dao.read_contract_data(case_id, f"ocr_result_{doc_id}.json")
    if ocr_result is None:
        raise RuntimeError(f"checkpoint 2 blocked: no ocr_result for {doc_id}")
    if ocr_result.get("cross_validation_status") not in REDACTABLE_CROSS_VALIDATION_STATUS:
        raise RuntimeError(
            f"checkpoint 2 blocked: {doc_id} cross_validation_status is "
            f"{ocr_result.get('cross_validation_status')!r}. Redaction proceeds "
            "only for a document whose extraction question is settled -- either "
            "the readers agreed, a human resolved them, or a recorded policy "
            "decided not to cross-validate at all. "
            f"Allowed: {sorted(REDACTABLE_CROSS_VALIDATION_STATUS)}."
        )

    redacted_pages: list[str] = []
    total_items = 0
    categories: set[str] = set()
    provider_metadata = None
    review_warnings: list[str] = []
    cache_dir = _resume_cache_dir(case_id, doc_id)
    cache_hits = 0

    # Scoped to this one document and revoked in the finally, so the window in
    # which pre-redaction text is obtainable at all is the page loop and
    # nothing more. An analysis agent shelling out to dao.py cannot produce
    # this, so --caller-stage alone stops being enough.
    capability, capability_path = dao._issue_page_text_capability(case_id, doc_id)
    pages = list(ocr_result.get("pages", []))
    total = len(pages)
    # Page-indexed slots, never append: the output order must be SOURCE order
    # no matter which page finishes first, because the assembled text is
    # concatenated in list order and an order-of-completion list would file one
    # page's redacted text under another's <<<PAGE>>> marker.
    slots: list[dict | None] = [None] * total
    workers = _resolve_workers(max_workers)
    concurrency = trace_mod.ConcurrencyProbe()
    counter_lock = threading.Lock()

    def process_page(index: int, page: dict) -> None:
        nonlocal cache_hits
        page_number = page["page"]
        with trace_mod.span("redact.page", category="io", case_id=case_id,
                            doc_id=doc_id, page=page_number) as sp, concurrency.enter():
            # In-process, not a subprocess: this is the per-PAGE call, so
            # the ~0.38s of interpreter startup (measured) was multiplied
            # by page count -- 11.2% of redaction wall-clock on a real
            # 17-page document. Both DAO gates still run, in the same
            # order, and the capability now travels as an argument instead
            # of through the child environment, so it never appears in a
            # process listing. read_page_text_data raises on refusal, so a
            # denial cannot be mistaken for page content.
            text = dao.read_page_text_data(
                case_id, doc_id, page_number,
                caller_stage="document-pipeline", capability=capability)
            sp.set(bytes_in=len(text))
            fingerprint = _cache_fingerprint(text, redactor)
            outcome = (_load_cached_page(cache_dir, page_number, fingerprint)
                       if resume else None)
            if outcome is not None:
                with counter_lock:
                    cache_hits += 1
                sp.set_status("cache_hit")
            else:
                # redact_page HARD-FAILS (RedactionLeakError) on any detected
                # possible PII leak -- that propagates out of this function and
                # nothing is written, blocking the document exactly like a P8
                # disagreement. Only a leak-free page returns an outcome.
                outcome = redactor.redact_page(text)
                # Cache only after the leak check has passed, so a blocked
                # document leaves nothing reusable behind.
                if resume:
                    _save_cached_page(cache_dir, page_number, fingerprint, outcome)
            sp.set(bytes_out=len(outcome.redacted_text))
            slots[index] = {"page": page_number, "outcome": outcome}

    try:
        with trace_mod.span("pool.redact_pages", category="compute",
                            case_id=case_id, doc_id=doc_id,
                            worker_count=min(workers, total) if total else 0,
                            items=total) as pool_span:
            if workers <= 1 or total <= 1:
                for index, page in enumerate(pages):
                    process_page(index, page)
            else:
                with ThreadPoolExecutor(max_workers=min(workers, total)) as pool:
                    # copy_context per submit: concurrent.futures does not
                    # propagate contextvars, so without this every redact.page
                    # span records parent_span_id None and the pool's own cost
                    # becomes unattributable (see trace.run_in_context).
                    submit = trace_mod.run_in_context(process_page)
                    futures = [pool.submit(submit, index, page)
                               for index, page in enumerate(pages)]
                    # Drain before re-raising. A page that already finished has
                    # been cached, and killing the pool early would throw that
                    # work away -- the property the sequential loop had for
                    # free. Crucially this does NOT downgrade a leak: the first
                    # error is re-raised below, so a leaking document still
                    # aborts with nothing written. Completed pages survive only
                    # in the cache, never in a "successful document".
                    first_error = None
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except BaseException as exc:  # noqa: BLE001 -- re-raised
                            first_error = first_error or exc
                    if first_error is not None:
                        raise first_error
            pool_span.set(observed_max_concurrency=concurrency.max_observed)
    finally:
        dao.release_page_text_capability(capability_path)

    # Assemble in source order from the slots.
    for slot in slots:
        if slot is None:
            continue
        outcome = slot["outcome"]
        redacted_pages.append(f"<<<PAGE page={slot['page']}>>>\n{outcome.redacted_text}")
        total_items += outcome.items_redacted
        categories.update(outcome.categories)
        provider_metadata = outcome.provider_metadata
        for warning in outcome.review_warnings:
            review_warnings.append(f"page {slot['page']}: {warning}")

    if total and len(redacted_pages) != total:
        # Belt-and-braces: a slot left empty without an exception would mean a
        # page silently vanished from the document. Refuse rather than write a
        # document that is missing pages nobody was told about.
        missing = [p["page"] for p, s in zip(pages, slots) if s is None]
        raise RuntimeError(
            f"checkpoint 2 blocked: {doc_id} produced no redaction for page(s) "
            f"{missing}; refusing to write a document with silently missing pages")

    if not redacted_pages:
        raise RuntimeError(f"checkpoint 2 blocked: {doc_id} has no validated pages")

    # review_required is COMPUTED, never hardcoded. A leak already hard-failed
    # above (no write), so what remains here is over-redaction risk (a span left
    # un-redacted because replace-all was unsafe) -- privacy-safe but worth a
    # human look. A downstream agent may raise this floor, never lower it.
    review_required = bool(review_warnings)

    warnings: list[str] = list(review_warnings)
    if provider_metadata:
        warnings.append(
            "Provider execution metadata is recorded in model_info.provider_metadata; "
            "source text was accessed only through dao.py."
        )

    redacted_text = "\n".join(redacted_pages) + "\n"
    redacted_path = f"data/processed/{case_id}/{doc_id}/redacted_text.md"
    contract = {
        "case_id": case_id,
        "run_id": run_id,
        "component": "document-pipeline",
        "status": "success",
        "model_info": {
            "model_name": redactor.label,
            "prompt_version": PROMPT_VERSION,
            "provider_metadata": provider_metadata or {},
        },
        "method": redactor.method,
        "document_id": doc_id,
        "redacted_text_path": redacted_path,
        "items_redacted": total_items,
        "categories": sorted(categories),
        "review_required": review_required,
        "warnings": warnings,
    }
    if review_required:
        # Schema rule (common_component_output.schema.json): review_required
        # true requires reviewer_role set. Over-redaction risk is a masking-
        # completeness question, not a medical/legal judgment call -- routes
        # to 손해사정사, same role run_checkpoint1.py uses for its own
        # review_required case (a P8 disagreement).
        contract["reviewer_role"] = "손해사정사"
        contract["review_reason"] = (
            "Over-redaction risk: a span was left un-redacted because a safe "
            "replacement could not be made without risking corruption of "
            "surrounding kept text (privacy-safe direction, but needs a human "
            "check). See warnings for the specific page(s)/span(s)."
        )

    scratch_root = ROOT / "_redaction_scratch"
    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{case_id}_{doc_id}_", dir=scratch_root) as temp_dir:
        temp = Path(temp_dir)
        text_file = temp / "redacted_text.md"
        contract_file = temp / "redaction_result.json"
        fields_file = temp / "manifest_fields.json"
        text_file.write_text(redacted_text, encoding="utf-8")
        contract_file.write_text(json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
        fields_file.write_text(json.dumps({"redacted_text_path": redacted_path}), encoding="utf-8")

        _dao("write-redacted-text", case_id, doc_id, "--text-file", str(text_file),
             "--held-by", held_by, "--run-id", run_id)
        _dao("write-contract", case_id, f"redaction_result_{doc_id}.json",
             "--data-file", str(contract_file), "--schema-name", "redaction_result.schema.json",
             "--held-by", held_by, "--run-id", run_id)
        _dao("patch-manifest-document", case_id, doc_id, "--fields-file", str(fields_file),
             "--held-by", held_by, "--run-id", run_id)

    return {
        "status": "success",
        "case_id": case_id,
        "doc_id": doc_id,
        "pages": len(redacted_pages),
        "items_redacted": total_items,
        "review_required": review_required,
        "redactor": redactor.label,
        # Reported so a run's cost is legible: pages == cache_hits means the
        # document cost zero provider calls this time.
        "cache_hits": cache_hits,
    }


# Document classes whose text is published standard-form contract wording,
# identical for every policyholder, and therefore structurally free of claimant
# PII. Eligibility is decided from the manifest's classified document_type --
# never from a filename or an agent's assertion. See
# redaction.NoPiiClassRedactor for what the exemption does and does not give up
# (the deterministic residual-PII sweep still runs on every page and still
# hard-fails, so the claim is verified per page rather than trusted).
NO_PII_DOCUMENT_TYPES = frozenset({"insurance_policy"})


SKIP_REDACTION_ENV = "HARNESS_SKIP_REDACTION"


def resolve_skip_redaction(skip: bool | None = None) -> bool:
    """Whether to skip the redaction MODEL. Explicit argument wins, then the env.

    Same precedence as every other knob here (and as
    ocr_extract.resolve_single_reader): `None` means "not specified", so an
    evaluation run can force real redaction back on inside a shell that exports
    the dev default.
    """
    if skip is not None:
        return bool(skip)
    raw = str(os.environ.get(SKIP_REDACTION_ENV, "")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _redactor_for(case_id: str, doc_id: str, provider_name: str, model: str | None,
                  *, skip_redaction: bool | None = None):
    """Pick the redactor for this document: the dev no-model switch, then the
    deterministic pass-through for a PII-free document class, otherwise the
    real LLM span redactor."""
    if resolve_skip_redaction(skip_redaction):
        # Checked before the manifest read: this switch does not depend on a
        # document_type, which is exactly what makes it survive the
        # classification-after-redaction order that broke the class exemption.
        print(f"{doc_id}: {SKIP_REDACTION_ENV} set -- redaction model skipped, "
              "deterministic residual-PII scan still enforced per page",
              file=sys.stderr)
        return DevNoLlmRedactor()
    manifest = dao.read_contract_data(case_id, "document_manifest.json")
    if manifest is None:
        raise RuntimeError(f"no document_manifest.json for {case_id}")
    entry = next((d for d in manifest.get("documents", [])
                  if d.get("document_id") == doc_id), None)
    if entry and entry.get("document_type") in NO_PII_DOCUMENT_TYPES:
        print(f"{doc_id}: document_type={entry['document_type']} is a PII-free class -- "
              "deterministic pass-through, no redaction model called "
              "(residual-PII scan still enforced per page)", file=sys.stderr)
        return NoPiiClassRedactor()
    provider = build_provider(ProviderConfig(provider_name, model), env=os.environ, root=ROOT)
    return LlmRedactor(provider)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_id")
    parser.add_argument("doc_id")
    parser.add_argument("--held-by", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--provider", choices=SUPPORTED_PROVIDERS,
                        default=os.environ.get("HARNESS_REDACTION_PROVIDER", DEFAULT_REDACTION_PROVIDER))
    parser.add_argument("--model", default=os.environ.get("HARNESS_REDACTION_MODEL"))
    parser.add_argument("--workers", type=int, default=None, metavar="N",
                        help="Pages redacted concurrently (default %d, or "
                             "HARNESS_REDACT_WORKERS). 1 = the strictly sequential "
                             "loop. Output is identical either way -- pages are "
                             "always assembled in source order."
                             % DEFAULT_REDACT_WORKERS)
    parser.add_argument("--no-resume", action="store_true",
                        help="Ignore and do not write the per-page redaction cache. "
                             "The cache is keyed on prompt version, redactor identity "
                             "and page-text hash, so a stale entry is already a miss; "
                             "this is for forcing a cold measurement.")
    skip_group = parser.add_mutually_exclusive_group()
    skip_group.add_argument(
        "--skip-redaction", dest="skip_redaction", action="store_true", default=None,
        help="Skip the redaction MODEL for every document (dev switch, or "
             "HARNESS_SKIP_REDACTION=1). The deterministic residual-PII scan "
             "still runs and still blocks a page carrying structured PII; what "
             "is given up is unstructured PII, which only a model can see. Text "
             "produced this way is not admissible as a privacy-preserving run.")
    skip_group.add_argument(
        "--redact", dest="skip_redaction", action="store_false",
        help="Force the redaction model on even under HARNESS_SKIP_REDACTION.")
    args = parser.parse_args()

    # Without this every span below is a no-op: trace.enabled() stays False
    # until a case/run is configured, so the redact.page and subprocess.dao
    # spans this tool raises are silently discarded. Found by running a real
    # 17-page redaction and getting an empty rollup for the very path the
    # runtime plan calls the pipeline's worst (B1) -- the instrumentation was
    # planted here but never switched on.
    trace_mod.configure(args.case_id, args.run_id)

    try:
        redactor = _redactor_for(args.case_id, args.doc_id, args.provider, args.model,
                                 skip_redaction=args.skip_redaction)
        result = redact_document(args.case_id, args.doc_id, args.held_by,
                                 args.run_id, redactor, resume=not args.no_resume,
                                 max_workers=args.workers)
    except RedactionLeakError as exc:
        # Possible PII leak detected -- nothing was written. Block the document.
        sys.exit(f"REDACTION BLOCKED ({args.doc_id}): possible PII leak -- {exc}")
    except (ProviderConfigError, ProviderExecutionError, RedactionParseError, RuntimeError) as exc:
        sys.exit(f"error: {exc}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
