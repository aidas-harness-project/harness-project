"""Per-page text extraction with P8 dual-path cross-validation.

Splits the source document into per-page images (pymupdf), then for each
page runs two independent transcriptions and asks a third, cheap text-only
call to judge whether they materially agree (same names/dates/numbers/
diagnoses), not verbatim match. That third call also flags any one-sided
extraneous content (a fabricated appendix, meta-commentary, anything one
reading has that the other lacks entirely) as a disagreement even when the
core facts otherwise match.

The reader/comparator backends are provider-configurable (openrouter /
claude-cli / codex-cli / anthropic-api / openai-api). All available providers
are LLM-vision-backed, so any
reader pair is a documented weak-P8 (see _classify_cross_validation): the two
reads share one extraction technology class and cannot catch a correlated
confident error. A genuinely technology-independent reader (a real OCR engine)
is deferred -- see open-decisions.md #4.

This tool does not write any contract file itself -- it prints page-level
results as JSON. document-pipeline reads that JSON, writes each page's
text via `dao.py write-page-text`, and assembles/writes ocr_result.json
via `dao.py write-contract` itself, same as any other DAO write.

Page images are staged under a project-local `_ocr_scratch/` (gitignored,
cleaned up on exit), not system /tmp.

Usage:
    python tools/ocr_extract.py CASE_ID DOC_ID /path/to/document.pdf
    python tools/ocr_extract.py CASE_ID DOC_ID /path/to/document.pdf \
        --reader-a PROVIDER --reader-b PROVIDER --comparator PROVIDER
"""
import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# tools/trace.py, not the stdlib `trace` module -- tools/ precedes stdlib on
# sys.path for every entry point in this repo.
import trace as trace_mod

sys.stdout.reconfigure(encoding="utf-8")

from llm_providers import (
    DEFAULT_PROVIDER,
    ProviderConfig,
    ProviderConfigError,
    ProviderExecutionError,
    SUPPORTED_PROVIDERS,
    build_provider,
)

ROOT = Path(__file__).resolve().parent.parent
SCRATCH_ROOT = ROOT / "_ocr_scratch"
OCR_PROMPT_VERSION = "ocr_extraction_v0.1"
# v0.2 (2026-08-03): the single-line instruction was the prompt's last line and
# carried no more weight than the checks above it, so the comparator routinely
# wrote its reasoning first, emitted a verdict, then corrected itself and
# emitted a SECOND verdict. Measured on CASE_907's two scanned documents: 4 of
# 34 pages (12%) came back "DISAGREE: <reason> / Correction: ... / AGREE: ...",
# and the parser -- which searches the whole string -- saw only the first token
# and blocked the page. Every one of those 4 was a false block. The format rule
# is now stated first, as a governing instruction, and explicitly forbids
# revising a verdict in place.
COMPARE_PROMPT_VERSION = "ocr_compare_v0.2"

TRANSCRIBE_PROMPT = (
    "Transcribe every piece of text visible in this page/image exactly as written, "
    "preserving structure (headers, tables, lists) as plain text. Output ONLY the "
    "transcription -- no commentary, no markdown code fences, no preamble."
)

COMPARE_PROMPT_TEMPLATE = (
    "Two independent transcriptions of the same document page follow. Judge whether "
    "they materially agree -- same names, dates, numbers, diagnoses -- even if wording "
    "or formatting differs. Verbatim match is not required.\n\n"
    "Separately, also check: does EITHER transcription contain any content the other "
    "one lacks entirely -- an extra paragraph, appended commentary, notes, a summary, "
    "or anything resembling meta-commentary about the transcription task itself -- even "
    "if that extra content doesn't conflict with any specific fact in the other reading? "
    "One transcription containing text the source page doesn't actually have (hallucinated "
    "content) is exactly the failure this check exists to catch. Treat any such one-sided "
    "addition as a disagreement, not just conflicting facts.\n\n"
    "This second check is about CONTENT that one reading has and the other does not. "
    "It is not about layout. Differences in whitespace, line breaks, paragraph breaks, "
    "indentation, table borders (| or +---+), column alignment, or cell ordering are "
    "presentation, not content: if every name, date, number, code and diagnosis is "
    "present in both, the answer is AGREE even when the two readings look nothing "
    "alike as strings.\n\n"
    "OUTPUT FORMAT -- this governs your entire reply:\n"
    "Decide FIRST, then write. Your reply must be exactly one line, beginning "
    "with the single word AGREE or DISAGREE, followed by ': ' and a brief "
    "reason. State the verdict word ONCE. Do not think aloud, do not write a "
    "preamble, and do not revise a verdict after writing it -- if you find "
    "yourself about to correct your own answer, stop and emit only the "
    "corrected verdict as that single line.\n\n"
    "--- Transcription A ---\n{a}\n\n--- Transcription B ---\n{b}"
)

DISAGREE_RE = re.compile(r"\bDISAGREE\b")
AGREE_RE = re.compile(r"\bAGREE\b")
# Both tokens in one pass, in order, so a reply carrying more than one verdict
# can be read as the sequence it is. DISAGREE is listed first so the alternation
# prefers it -- otherwise "AGREE" would match inside "DISAGREE" at a position
# where \b holds on the right but the token is the wrong one.
VERDICT_RE = re.compile(r"\bDISAGREE\b|\bAGREE\b")

# File suffixes handled as embedded plain text rather than page images. A
# plain-text source is a lossless byte decode, not a probabilistic OCR/vision
# read -- there is no second, independent reading that could disagree, so P8's
# dual-path cross-validation does not apply (cross_validation_mode
# "deferred_poc"). Forcing such a file through vision transcription produced
# hallucinated readings and a spurious P8 disagreement (CASE_024/DOC_001, a
# CP949 Korean note read as "an AI assistant's message about running iconv").
TEXT_SUFFIXES = {".txt", ".md", ".text"}
# Minimum non-whitespace characters on a PDF page for its embedded text layer to
# count as real content. Deliberately 1, not a tuned threshold: measured on
# CASE_112, the low-character pages (DOC_077/DOC_094/DOC_124/DOC_183, 4-41 chars)
# were short because the page genuinely holds only a form blank or a one-line
# 준용규정 -- not because the text layer was partial. Vision OCR of those same
# pages returned identical content plus a hallucinated insurer slogan
# ("당신에게 좋은보험 삼성화재") absent from the source, so a character-count
# threshold would route the *more* faithful reading to the *less* faithful path.
# A genuine scan has a strictly empty text layer (0 chars), which is the only
# case this distinguishes.
PDF_EMBEDDED_TEXT_MIN_CHARS = 1
# Encodings tried in order for a text-file decode. utf-8-sig FIRST -- not cp949 --
# on purpose: UTF-8 is self-validating (invalid UTF-8 reliably raises), so a real
# UTF-8 file always decodes here and a cp949/euc-kr file falls through cleanly
# (its bytes are almost never valid UTF-8). Trying cp949 first risked silently
# mojibake-decoding a UTF-8 file, and plain "utf-8" leaves a BOM as a stray
# ﻿ in the text; utf-8-sig fixes both (strips a BOM if present). The reported
# encoding maps utf-8-sig -> "utf-8" (see decode_text_file).
TEXT_ENCODINGS = ("utf-8-sig", "cp949", "euc-kr")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}


def decode_text_file(doc_path: Path) -> tuple[str, str]:
    """Decode a plain-text source losslessly, returning (text, encoding_used).

    Tries TEXT_ENCODINGS in order and returns the first that decodes cleanly.
    A clean decode is deterministic and lossless -- unlike OCR, there is no
    confidence signal and no second reading to cross-validate against. Raises
    if none of the candidate encodings decode, rather than guessing with
    errors='replace' (a silent-corruption path P8 exists to prevent)."""
    raw = doc_path.read_bytes()
    for enc in TEXT_ENCODINGS:
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        # Fail closed on an empty/whitespace-only source: embedded-text bypasses
        # P8, so a silent empty page would enter the trusted layer with no
        # cross-check. An empty document is anomalous -- surface it.
        if not text.strip():
            raise ProviderExecutionError(
                f"{doc_path} decoded ({enc}) to empty/whitespace-only text; not a usable document"
            )
        return text, ("utf-8" if enc == "utf-8-sig" else enc)
    raise ProviderExecutionError(
        f"could not decode {doc_path} as text with any of {TEXT_ENCODINGS}; "
        "not forcing it through vision OCR"
    )


def transcribe_once(image_path: Path, provider=None) -> dict:
    selected_provider = provider or build_provider(root=ROOT)
    result = selected_provider.transcribe_image(image_path, TRANSCRIBE_PROMPT, OCR_PROMPT_VERSION)
    return {"text": result.text, "metadata": result.metadata()}


def compare(text_a: str, text_b: str, comparator=None) -> dict:
    # Byte-identical shortcut: if the two independent reads are exactly equal
    # after stripping, they trivially agree -- there is no possible one-sided
    # addition or fact conflict to find, so skip the comparator provider call
    # entirely (one fewer LLM round-trip per identical page). This does NOT
    # relax P8: it only short-circuits the trivially-agreed case; any
    # difference at all still goes through the full comparator below. Parity
    # with shared/main, which fix_codex had dropped.
    if text_a.strip() == text_b.strip():
        return {
            "agreement": "agreed",
            "disagreement_details": [],
            "metadata": {"shortcut": "identical_reads", "comparator_called": False},
        }
    prompt = COMPARE_PROMPT_TEMPLATE.format(a=text_a, b=text_b)
    selected_comparator = comparator or build_provider(root=ROOT)
    provider_result = selected_comparator.compare_text(prompt, COMPARE_PROMPT_VERSION)
    verdict = provider_result.text.strip()
    metadata = provider_result.metadata()
    verdict_upper = verdict.upper()

    # Word-boundary search, not startswith -- the model doesn't always lead
    # with the bare token despite the prompt asking for exactly that (e.g. a
    # full sentence like "The two transcriptions AGREE on...").
    #
    # Take the LAST verdict token, not the first. v0.2's prompt forbids
    # revising a verdict in place, but a prompt is a request, not a guarantee,
    # and the failure it addresses is a real observed one: on CASE_907's two
    # scanned documents 4 of 34 pages (12%) came back "DISAGREE: <reason> /
    # Correction: that reasoning supports AGREE / AGREE", and reading the first
    # token blocked all 4 -- every one a false block, on pages whose readings
    # differed only in whitespace. When a model corrects itself, the correction
    # is its answer; honouring the retracted token instead means a page is
    # blocked by reasoning the model itself withdrew.
    #
    # This does NOT weaken P8. A genuine "AGREE ... actually DISAGREE" self-
    # correction still lands on disagreed, and anything unparseable below still
    # fails closed. A multi-verdict reply is recorded either way so the format
    # violation stays visible rather than being silently normalised away.
    verdicts = [
        ("disagreed" if match.group(0) == "DISAGREE" else "agreed")
        for match in VERDICT_RE.finditer(verdict_upper)
    ]
    if verdicts:
        agreement = verdicts[-1]
        revised = len(set(verdicts)) > 1
        details = [verdict] if agreement == "disagreed" else []
        if revised:
            details = details or [
                f"comparator emitted {len(verdicts)} verdicts and revised "
                f"itself to AGREE; last verdict taken: {verdict!r}"]
        return {
            "agreement": agreement,
            "disagreement_details": details,
            "metadata": {**metadata, "verdict_revised_in_place": revised},
        }

    # Neither token found -- the model didn't follow the expected format.
    # Fail safe as disagreed (P8: no tolerance, never silently assume
    # agreement) rather than crashing the whole multi-page run.
    return {
        "agreement": "disagreed",
        "disagreement_details": [f"unparseable compare() verdict, treated as disagreement: {verdict!r}"],
        "metadata": metadata,
    }


@contextlib.contextmanager
def scratch_dir(case_id: str, doc_id: str):
    # Project-local, not system /tmp. Session-tagged (pid) instead of a bare
    # case/doc directory so concurrent runs against the same document cannot
    # collide on one path.
    d = SCRATCH_ROOT / f"{case_id}_{doc_id}_{os.getpid()}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


DEFAULT_RENDER_DPI = 200
RENDER_DPI_ENV = "HARNESS_OCR_DPI"
# Above this, a full page render starts costing more in provider-side image
# handling than the extra detail is worth; a bad value should not silently
# become an enormous one.
MAX_RENDER_DPI = 600


def _resolve_render_dpi(dpi: int | None = None) -> int:
    """Explicit argument wins, then HARNESS_OCR_DPI, then the default.

    Both render backends (pymupdf and pdftoppm) must agree: the dpi is a
    property of the page image the READER sees, so letting it depend on which
    backend happens to be installed would make P8 agreement depend on the host
    rather than on the document. An unparseable, non-positive, or absurd env
    value falls back to the default instead of raising -- rendering is not the
    place to fail a run on a typo, and a silent 0-dpi render would be worse.
    """
    if dpi is None:
        raw = os.environ.get(RENDER_DPI_ENV, "")
        try:
            dpi = int(raw) if raw.strip() else None
        except ValueError:
            dpi = None
    if dpi is None or dpi <= 0 or dpi > MAX_RENDER_DPI:
        return DEFAULT_RENDER_DPI
    return dpi


def _split_to_page_images_fitz(doc_path: Path, out_dir: Path, max_pages: int | None = None,
                               dpi: int | None = None) -> list[Path]:
    import fitz  # pymupdf

    doc = fitz.open(doc_path)
    try:
        page_count = doc.page_count if max_pages is None else min(max_pages, doc.page_count)
        paths = []
        for i in range(page_count):
            page = doc.load_page(i)
            pix = page.get_pixmap(dpi=_resolve_render_dpi(dpi))
            out_path = out_dir / f"page_{i + 1:03d}.png"
            pix.save(out_path)
            paths.append(out_path)
        return paths
    finally:
        doc.close()


def _find_pdftoppm() -> str | None:
    dependency_root = Path(sys.executable).resolve().parent.parent
    candidates = [
        dependency_root / "native" / "poppler" / "Library" / "bin" / "pdftoppm.exe",
        dependency_root / "bin" / "pdftoppm.exe",
        dependency_root / "bin" / "pdftoppm.cmd",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    for name in ("pdftoppm.exe", "pdftoppm", "pdftoppm.cmd"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _pdftoppm_page_number(path: Path) -> int:
    match = re.search(r"-(\d+)\.png$", path.name)
    return int(match.group(1)) if match else 0


def _split_to_page_images_pdftoppm(doc_path: Path, out_dir: Path, max_pages: int | None = None,
                                   dpi: int | None = None) -> list[Path]:
    command = _find_pdftoppm()
    if command is None:
        sys.exit("error: pymupdf missing and pdftoppm not found for PDF rendering")

    prefix = out_dir / "page"
    cmd = [command, "-png", "-r", str(_resolve_render_dpi(dpi))]
    if max_pages is not None:
        cmd.extend(["-f", "1", "-l", str(max_pages)])
    cmd.extend([str(doc_path), str(prefix)])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        sys.exit(f"error: pdftoppm failed while rendering {doc_path}: {result.stderr.strip()}")

    generated = sorted(out_dir.glob("page-*.png"), key=_pdftoppm_page_number)
    if not generated:
        sys.exit(f"error: pdftoppm did not produce page images for {doc_path}")

    paths = []
    for i, generated_path in enumerate(generated, start=1):
        out_path = out_dir / f"page_{i:03d}.png"
        generated_path.replace(out_path)
        paths.append(out_path)
    return paths


def split_to_page_images(doc_path: Path, out_dir: Path, max_pages: int | None = None,
                         dpi: int | None = None) -> list[Path]:
    """max_pages caps how many pages get rendered (from the start) -- used by
    intake_case.py's content pre-check, which only needs the first few pages,
    not a full render. None (default) renders every page, unchanged from
    this function's original behavior.

    dpi=None keeps the 200-dpi default (see _resolve_render_dpi)."""
    try:
        return _split_to_page_images_fitz(doc_path, out_dir, max_pages, dpi)
    except ImportError:
        return _split_to_page_images_pdftoppm(doc_path, out_dir, max_pages, dpi)


def build_ocr_providers(
    *,
    reader_a_name: str | None = None,
    reader_b_name: str | None = None,
    comparator_name: str | None = None,
    reader_a_model: str | None = None,
    reader_b_model: str | None = None,
    comparator_model: str | None = None,
    env=None,
) -> dict:
    source_env = env if env is not None else os.environ
    default_provider = source_env.get("HARNESS_LLM_PROVIDER") or DEFAULT_PROVIDER
    default_model = source_env.get("HARNESS_LLM_MODEL")

    reader_a_provider = reader_a_name or source_env.get("HARNESS_OCR_READER_A_PROVIDER") or default_provider
    reader_b_provider = reader_b_name or source_env.get("HARNESS_OCR_READER_B_PROVIDER") or reader_a_provider
    comparator_provider = comparator_name or source_env.get("HARNESS_OCR_COMPARATOR_PROVIDER") or reader_a_provider

    reader_a_model = reader_a_model or source_env.get("HARNESS_OCR_READER_A_MODEL") or default_model
    reader_b_model = reader_b_model or source_env.get("HARNESS_OCR_READER_B_MODEL") or reader_a_model
    comparator_model = comparator_model or source_env.get("HARNESS_OCR_COMPARATOR_MODEL") or reader_a_model

    return {
        "reader_a": build_provider(ProviderConfig(reader_a_provider, reader_a_model), env=source_env, root=ROOT),
        "reader_b": build_provider(ProviderConfig(reader_b_provider, reader_b_model), env=source_env, root=ROOT),
        "comparator": build_provider(ProviderConfig(comparator_provider, comparator_model), env=source_env, root=ROOT),
    }


def _metadata_for(provider) -> dict:
    return {"provider_name": provider.provider_name, "model_name": provider.model_name}


def _resume_cache_dir(case_id: str, doc_id: str) -> Path:
    """Stable (NOT pid-tagged) per-document dir holding one JSON per completed
    page. Unlike scratch_dir()'s pid-tagged, rmtree-on-exit image staging,
    this survives across process runs so an interrupted multi-page OCR can
    resume instead of re-paying for pages it already transcribed."""
    return SCRATCH_ROOT / "_resume" / f"{case_id}_{doc_id}"


# Bumped when the CACHE ENTRY's own shape changes. An entry written before
# fingerprinting existed carries no `fingerprint` key at all and is treated as
# a miss, which is the intended handling: it cannot be shown to match.
OCR_CACHE_FORMAT_VERSION = 1


def _cache_fingerprint(img_path: Path, reader_a, reader_b, comparator,
                       dpi: int | None = None, single_reader: bool = False) -> str:
    """What the cached P8 verdict is only valid FOR.

    Until 2026-08-11 this cache was keyed on case_id/doc_id/page alone, so a
    re-run with a different render dpi, a different reader provider or model,
    or a revised prompt was served the OLD verdict as a hit -- silently. That
    is worse here than in the redaction cache: the cached value is a P8
    AGREEMENT decision, so a stale hit can report `agreed` for a page pair
    that was never actually read at the current settings, and P8 is the gate
    everything downstream trusts.

    Every input that can change the verdict is in here:

    * sha256 of the exact page IMAGE bytes -- this is what the readers see, so
      it covers render dpi, the render backend, and a re-rendered source
      without needing to enumerate them.
    * `OCR_PROMPT_VERSION` -- a revised transcription or comparison prompt
      usually means the previous one misread something.
    * both readers' and the comparator's provider+model -- a different model
      is a different reader, and P8's premise is which two readers agreed.

    A mismatch on any of them is a miss, and a miss re-runs the real calls.
    """
    try:
        digest = hashlib.sha256(img_path.read_bytes()).hexdigest()
    except OSError:
        # Unreadable image -> a fingerprint nothing can match, so the page is
        # re-read rather than served from cache on a guess.
        digest = "unreadable"

    def _label(provider) -> str:
        return (f"{getattr(provider, 'provider_name', 'unknown')}"
                f":{getattr(provider, 'model_name', None)}")

    if single_reader:
        # A single-reader page carries NO P8 verdict at all, so it must never be
        # interchangeable with a dual-read cache entry in either direction: a
        # single-reader hit would hand a throughput run's unvalidated text to a
        # run that asked for cross-validation, and a dual-read hit would let a
        # --single-reader run report an `agreed` it never paid for. Different
        # namespace, not a different reader list.
        return (f"{OCR_CACHE_FORMAT_VERSION}:{OCR_PROMPT_VERSION}:"
                f"{_resolve_render_dpi(dpi)}:single_reader:{_label(reader_a)}:{digest}")

    readers = f"{_label(reader_a)}|{_label(reader_b)}|{_label(comparator)}"
    return (f"{OCR_CACHE_FORMAT_VERSION}:{OCR_PROMPT_VERSION}:"
            f"{_resolve_render_dpi(dpi)}:{readers}:{digest}")


def _load_cached_page(cache_dir: Path, page: int, fingerprint: str | None = None) -> dict | None:
    """Return the cached page result, or None.

    Fails closed in every ambiguous case -- unreadable file, malformed JSON,
    missing fingerprint, fingerprint mismatch. A miss costs three provider
    calls; a wrong hit reports a P8 verdict that was never measured under the
    current settings.
    """
    p = cache_dir / f"page_{page:03d}.json"
    if not p.exists():
        return None
    try:
        entry = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None  # corrupt/partial cache entry -> re-transcribe this page
    if not isinstance(entry, dict):
        return None
    if fingerprint is not None and entry.get("fingerprint") != fingerprint:
        return None  # different dpi/provider/model/prompt, or a pre-fingerprint entry
    return entry


def _save_cached_page(cache_dir: Path, page: int, page_result: dict,
                      fingerprint: str | None = None) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    entry = dict(page_result)
    if fingerprint is not None:
        entry["fingerprint"] = fingerprint
    # atomic write so an interrupt mid-write never leaves a half-page that
    # would be trusted on resume.
    tmp = cache_dir / f"page_{page:03d}.json.tmp"
    tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cache_dir / f"page_{page:03d}.json")


# Raised 4 -> 8 on 2026-08-11. Measured on a real 12-page scan at dd6d8aa:
# 141.32s -> 86.06s, a 1.64x speedup from this constant alone. The new corpus
# is entirely scans, so this applies to every case rather than a subset.
#
# 2026-08-11: raised 8 -> 16 (user decision), explicitly recorded at the time
# as "a deliberate bet, not a measurement" -- 16 had never been compared
# against anything. The bet's stated safety net was that being wrong would
# cost rate-limit failures rather than slowness.
#
# 2026-08-12: measured twice, and the two measurements disagree -- which is
# the whole point, because they measure different workloads.
#
# First, 24 TRIVIAL `claude -p` calls (no image, no reasoning) at varying
# width peaked at 12 and degraded above it: 12 -> 19.9s, 16 -> 23.6s,
# 24 -> 32.1s. That curve is real but it is the SPAWN curve: with model work
# held at ~0, a call is nothing but local process cost, so contention
# dominates immediately. Setting OCR to 12 from that number was applying a
# short-call result to a long-call workload.
#
# Second, the actual thing: 34 REAL scanned pages (every OCR page in
# CASE_911 -- DOC_002's 15 plus the 11 medical children's 19), batch larger
# than every width tested so each is genuinely distinct:
#
#   workers   wall     per page   mean latency
#     12      75.2s     2.21s       23.5s
#     24      54.6s     1.61s       23.4s   <- chosen
#     34      53.0s     1.56s       28.6s
#
# 12 -> 24 is a 20.6s (27%) win with per-call latency FLAT (23.5 -> 23.4),
# i.e. no contention had begun at 24. 24 -> 34 buys only 1.6s more while
# latency jumps +5.2s -- work is being queued inside the calls, the onset of
# saturation, for essentially no wall-clock return.
#
# Why this workload tolerates ~2x the trivial-call width: a real OCR call
# spends ~23s awaiting the model with the local CPU idle, so the 4.34s of
# spawn cost is a small share of each call rather than all of it. An earlier
# 15-page run appeared to show "16 is fastest" and was discarded as
# unreadable -- with only 15 items, W=16 and W=24 both dispatch the whole
# batch at once and are the same configuration.
#
# Raised 12 -> 24 on that measurement. This deliberately DIFFERS from the
# short-call ops (redaction/classify/segment-judge), which stay at 12 where
# their own measurement put them; one global width would have to be wrong
# for one of the two.
#
# This is a per-DOCUMENT knob, so demand is this value x HARNESS_DOC_WORKERS
# (24 x 3 = 72). The process-wide in-flight cap (T6) is what actually bounds
# that; it is set to this same 24 so a single document can reach full width
# while several documents cannot multiply past it.
DEFAULT_OCR_WORKERS = 24

# Turns P8 off for every OCR call in the process, so a development session does
# not have to remember --single-reader on each invocation. Set
# HARNESS_SINGLE_READER=1 in the dev environment; unset (or 0) keeps full dual-read
# P8, which is what an evaluation run needs.
#
# Deliberately an env var rather than flipping the flag's default: a default of
# True would leave no way to ask for P8 on the command line, and the PoC's
# evaluation runs need exactly that. Precedence matches every other knob here --
# an explicit argument wins, then the env var, then the default (off).
SINGLE_READER_ENV = "HARNESS_SINGLE_READER"


# PoC default (2026-08-20, set by the PoC owner): unspecified means P8 OFF.
# It was the reverse until then. The reduction is not hidden by being the
# default -- run_checkpoint1 still stamps `ocr_quality: low`,
# `cross_validation_status: single_reader_no_cross_validation` and
# `review_required` on every document read this way, so a single-reader
# document never reads as a clean P8 pass. Set HARNESS_SINGLE_READER=0, or
# pass --dual-read, for an evaluation run that needs real cross-validation.
SINGLE_READER_DEFAULT = True


def resolve_single_reader(single_reader: bool | None) -> bool:
    """Whether to run with P8 off. Explicit argument wins, then the env var.

    `None` means "not specified" -- only then is the environment consulted,
    and only then does `SINGLE_READER_DEFAULT` apply. Passing True or False
    explicitly is always honoured, so an evaluation run can force dual-read P8
    without changing this file or the shell.
    """
    if single_reader is not None:
        return bool(single_reader)
    raw = os.environ.get(SINGLE_READER_ENV, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return SINGLE_READER_DEFAULT


def _resolve_workers(max_workers: int | None) -> int:
    """Page-level concurrency. Explicit argument wins, then HARNESS_OCR_WORKERS,
    then DEFAULT_OCR_WORKERS.

    The ceiling is not CPU-derived: the work is provider round-trips, not local
    computation, so the real limits are the backend's rate limit and -- for the
    CLI providers -- one child process per in-flight call. HARNESS_LLM_MAX_INFLIGHT
    can still cap the whole process rather than one document, but it is
    uncapped by default since 2026-08-11, so nothing bounds this globally
    unless that env var is set. A value <= 1 restores the strictly sequential
    loop, which is also what a single-page document gets.
    """
    if max_workers is None:
        raw = os.environ.get("HARNESS_OCR_WORKERS")
        if raw is None or not raw.strip():
            return DEFAULT_OCR_WORKERS
        try:
            max_workers = int(raw)
        except ValueError:
            raise ProviderConfigError(
                f"HARNESS_OCR_WORKERS must be an integer, got {raw!r}"
            ) from None
    if max_workers < 1:
        return 1
    return max_workers


def run_ocr(
    case_id: str,
    doc_id: str,
    doc_path: Path,
    progress=None,
    reader_a=None,
    reader_b=None,
    comparator=None,
    resume: bool = True,
    max_workers: int | None = None,
    dpi: int | None = None,
    single_reader: bool | None = None,
) -> dict:
    """The actual dual-path OCR loop, extracted out of main() so callers
    (run_checkpoint1.py) can invoke it in-process instead of shelling out
    to this script as a subprocess. Pure extraction -- main() below calls
    this and does exactly what it always did (print JSON to stdout, exit
    1 on any disagreement). progress(msg) is called per page if given,
    instead of always printing to stderr, so a library caller can route it
    (or silence it) rather than inheriting main()'s CLI-only behavior.

    resume=True (default): each page's dual-read result is cached to a stable
    per-document dir as it completes, and a re-run skips the (expensive) reader
    calls for any page already cached. This makes a long multi-page run
    interruptible -- kill it and re-invoke, and it picks up where it stopped
    rather than restarting from page 1 (checkpoint 1 for CASE_003/DOC_008, 75
    pages, otherwise loses everything on any interruption since ocr_result is
    only written after the whole document finishes). The cache is cleared on
    full completion. Page images are deterministic per PDF, so a cached page N
    always corresponds to the same source page.

    max_workers controls how many PAGES are in flight at once (see
    _resolve_workers). Pages are independent -- each is two reads plus one
    comparison over one image -- so this is a wall-time change only: results are
    collected into page-indexed slots and returned in source order, each page's
    two reads still see the same image, and a page still costs exactly one pass.
    The two readers of a single page stay sequential relative to each other, so
    P8's pairing cannot drift. Pass max_workers=1 for the original strictly
    sequential behaviour.

    single_reader=True is a DEVELOPMENT-ONLY throughput mode that turns P8 off
    rather than relaxing it: reader_b and the comparator are never called, so a
    page costs one read instead of two-plus-a-comparison. There is no agreement
    to report, so every page is recorded as `single_reader` (never `agreed` --
    nothing agreed) and the document's cross_validation_mode becomes
    `single_reader_no_cross_validation`. Nothing produced this way is admissible
    as PoC evaluation text: the four real extraction faults P8 caught on this
    corpus (CASE_012's fabricated appendix, CASE_021's second fabricated
    addition and its KCD I678 misread, CASE_022's context contamination) were
    each visible ONLY as a disagreement between two reads, and a single read
    would have carried all four downstream silently. Use it for timing and
    plumbing runs; leave it off for any case whose text accuracy is judged."""
    single_reader = resolve_single_reader(single_reader)

    if not doc_path.exists():
        sys.exit(f"error: document not found -- {doc_path}")

    # Plain-text sources take a deterministic decode path, never vision OCR.
    # This must precede any provider/scratch/image work: there is nothing to
    # transcribe, split, or cross-validate for a text file.
    if doc_path.suffix.lower() in TEXT_SUFFIXES:
        return _run_embedded_text(case_id, doc_id, doc_path, progress=progress)

    # A born-digital PDF carries the publisher's own text; transcribing it by
    # vision is strictly worse (measured on CASE_112: identical content, plus
    # reordered footnotes, substituted quote glyphs, and an appended insurer
    # slogan that is not on the page). Checked before provider construction so
    # such a document costs zero model calls. Returns None -- falling through to
    # OCR -- for a genuine scan or a mixed bundle.
    if doc_path.suffix.lower() == ".pdf":
        page_texts = pdf_embedded_page_texts(doc_path)
        if page_texts is not None:
            return _run_embedded_pdf(
                case_id, doc_id, doc_path, page_texts, progress=progress
            )

    if single_reader:
        # Only reader_a is ever used, so only reader_a is required. Building the
        # other two would be harmless but misleading -- a constructed provider
        # reads as a configured one, and nothing here calls them.
        if reader_a is None:
            reader_a = build_ocr_providers()["reader_a"]
        reader_b = None
        comparator = None
    elif reader_a is None or reader_b is None or comparator is None:
        providers = build_ocr_providers()
        reader_a = reader_a or providers["reader_a"]
        reader_b = reader_b or providers["reader_b"]
        comparator = comparator or providers["comparator"]

    cache_dir = _resume_cache_dir(case_id, doc_id)
    workers = _resolve_workers(max_workers)

    with scratch_dir(case_id, doc_id) as tmp_dir:
        if doc_path.suffix.lower() == ".pdf":
            page_images = split_to_page_images(doc_path, tmp_dir, dpi=dpi)
        else:
            page_images = [doc_path]  # already a single image

        total = len(page_images)
        # Results are collected into a page-indexed slot, never appended, so the
        # output order is the SOURCE order no matter which page finishes first.
        # Downstream writes pages[i] as page i+1's text, so an
        # order-of-completion list would silently file one page's text under
        # another's number.
        slots: list[dict | None] = [None] * total
        progress_lock = threading.Lock()

        def report(msg: str) -> None:
            # progress() is called from worker threads once workers > 1; a
            # caller's callback (and plain print) is not guaranteed thread-safe.
            with progress_lock:
                progress(msg) if progress else print(msg, file=sys.stderr)

        concurrency = trace_mod.ConcurrencyProbe()

        def process_page(index: int, img_path: Path) -> None:
            page_no = index + 1
            with trace_mod.span("ocr.page", category="io", case_id=case_id,
                                doc_id=doc_id, page=page_no) as sp, concurrency.enter():
                fingerprint = _cache_fingerprint(img_path, reader_a, reader_b,
                                                 comparator, dpi,
                                                 single_reader=single_reader)
                cached = (_load_cached_page(cache_dir, page_no, fingerprint)
                          if resume else None)
                if cached is not None:
                    slots[index] = cached
                    sp.set_status("cache_hit")
                    report(f"page {page_no}/{total}: {cached['agreement']} (cached)")
                    return

                # reader_a and reader_b are deliberately BOTH given img_path: P8's
                # premise is two independent reads of the same page. They stay
                # sequential relative to each other -- the parallelism is across
                # pages, so pairing can never drift.
                reading_a = transcribe_once(img_path, reader_a)
                if single_reader:
                    # No second read and no comparison: there is no agreement to
                    # report, so `agreement` says exactly that rather than
                    # borrowing a P8 verdict word. reading_b is null (not a copy
                    # of reading_a, which would read as two reads concurring)
                    # and disagreement_details is empty because nothing was
                    # compared -- not because nothing differed.
                    page_result = {
                        "page": page_no,
                        "reading_a": reading_a["text"],
                        "reading_b": None,
                        "agreement": "single_reader",
                        "disagreement_details": [],
                        "provider_metadata": {
                            "reader_a": reading_a["metadata"],
                            "reader_b": None,
                            "comparator": None,
                        },
                    }
                else:
                    reading_b = transcribe_once(img_path, reader_b)
                    result = compare(reading_a["text"], reading_b["text"], comparator)
                    page_result = {
                        "page": page_no,
                        "reading_a": reading_a["text"],
                        "reading_b": reading_b["text"],
                        "agreement": result["agreement"],
                        "disagreement_details": result["disagreement_details"],
                        "provider_metadata": {
                            "reader_a": reading_a["metadata"],
                            "reader_b": reading_b["metadata"],
                            "comparator": result["metadata"],
                        },
                    }
                # Cache before publishing the slot: an interrupt between the two
                # loses nothing (the page is re-read), whereas the reverse could
                # report a page as done that was never persisted.
                if resume:
                    _save_cached_page(cache_dir, page_no, page_result, fingerprint)
                slots[index] = page_result
                # Reads from page_result, not from `result`: the comparator
                # verdict only exists on the dual-read branch, so referencing it
                # here crashed every --single-reader page with an UnboundLocalError.
                report(f"page {page_no}/{total}: {page_result['agreement']}")

        with trace_mod.span("pool.ocr_pages", category="compute",
                            case_id=case_id, doc_id=doc_id,
                            worker_count=min(workers, total),
                            items=total) as pool_span:
            if workers <= 1 or total <= 1:
                for index, img_path in enumerate(page_images):
                    process_page(index, img_path)
            else:
                with ThreadPoolExecutor(max_workers=min(workers, total)) as pool:
                    # run_in_context: concurrent.futures does NOT propagate
                    # contextvars into workers, so submitting process_page raw
                    # would leave every ocr.page span parented at None. The
                    # page loop is the most expensive thing in the pipeline;
                    # orphaning its spans would make it precisely the part the
                    # critical path could not explain.
                    submit = trace_mod.run_in_context(process_page)
                    futures = {
                        pool.submit(submit, index, img_path): index
                        for index, img_path in enumerate(page_images)
                    }
                    # Surface the first failure, but only after every in-flight page
                    # has settled -- a page that already finished has been cached,
                    # and killing the pool early would throw that work away. That is
                    # the property the sequential loop had for free: a crash on page
                    # 7 kept pages 1-6.
                    first_error = None
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except BaseException as exc:  # noqa: BLE001 -- re-raised below
                            first_error = first_error or exc
                    if first_error is not None:
                        raise first_error
            pool_span.set(observed_max_concurrency=concurrency.max_observed)

        pages_out = [slot for slot in slots if slot is not None]
        if len(pages_out) != total:
            missing = [i + 1 for i, slot in enumerate(slots) if slot is None]
            raise ProviderExecutionError(
                f"OCR produced no result for page(s) {missing} of {doc_path}; "
                "refusing to return a document with silently missing pages"
            )

    # Full document finished -> the per-page resume cache is no longer needed.
    if resume:
        shutil.rmtree(cache_dir, ignore_errors=True)

    if single_reader:
        cross_validation_mode = "single_reader_no_cross_validation"
        cross_validation_note = (
            f"P8 was NOT performed: --single-reader ran one read "
            f"({_metadata_for(reader_a).get('provider_name')}, model "
            f"{_metadata_for(reader_a).get('model_name')}) per page with no "
            "second reader and no comparison. Every page is `single_reader`, "
            "never `agreed` -- nothing agreed. This text is unvalidated: a "
            "misread, a fabricated addition, or context contamination would be "
            "carried downstream with nothing able to detect it. Development "
            "throughput mode only; not admissible for PoC evaluation."
        )
    else:
        cross_validation_mode, cross_validation_note = _classify_cross_validation(reader_a, reader_b)

    return {
        "document_path": str(doc_path),
        "providers": {
            "reader_a": _metadata_for(reader_a),
            "reader_b": _metadata_for(reader_b) if reader_b is not None else None,
            "comparator": _metadata_for(comparator) if comparator is not None else None,
        },
        "cross_validation_mode": cross_validation_mode,
        "cross_validation_note": cross_validation_note,
        "pages": pages_out,
    }


def pdf_embedded_page_texts(pdf_path: Path) -> list[str] | None:
    """Return per-page embedded text for a born-digital PDF, or None.

    None means "this PDF is not a whole-document embedded-text source" and the
    caller must fall through to vision OCR. That is returned in two cases:

      * no page carries a text layer -- a genuine scan (the CASE_112 medical
        records: DOC_214-DOC_224 all extract 0 characters), and
      * only SOME pages carry one -- a mixed bundle. Deliberately not handled
        per-page here: mixing a lossless decode and a probabilistic vision read
        inside one document would make extraction_method a single label over two
        different provenances, and `cross_validation_mode` likewise. Such a
        document stays wholly on the OCR path until that distinction can be
        carried per page in the schema.

    A PDF whose every page has real text is the case worth taking: measured
    across CASE_112's 202 comparable documents, the embedded layer matched the
    vision transcription at 0.976 mean similarity, and every top divergence was
    the vision read reordering a footnote, substituting quote glyphs, or
    appending an insurer slogan that is not on the page at all."""
    try:
        import fitz
    except ImportError:
        return None
    try:
        with fitz.open(str(pdf_path)) as document:
            if document.page_count < 1:
                return None
            texts = [document[i].get_text() for i in range(document.page_count)]
    except Exception:
        # A malformed/encrypted PDF is not an embedded-text source; the OCR path
        # rasterizes and reports its own error rather than this one masking it.
        return None
    if not all(len(t.strip()) >= PDF_EMBEDDED_TEXT_MIN_CHARS for t in texts):
        return None
    return texts


def _run_embedded_pdf(
    case_id: str, doc_id: str, doc_path: Path, page_texts: list[str], progress=None
) -> dict:
    """Embedded-text passthrough for a born-digital PDF, shaped exactly like
    run_ocr()'s dual-path output so run_checkpoint1._assemble_ocr_result
    consumes it unchanged.

    Same honesty contract as _run_embedded_text: reading_a == reading_b is not a
    fake second read, it is the single decoded text carried in the slot the
    downstream page-write reads; `deferred_poc` records that no dual-read
    cross-check happened, and `agreed` reflects that a text-layer extraction is
    its own ground truth rather than a probabilistic read that could be
    confidently wrong."""
    pages = []
    total = len(page_texts)
    for i, text in enumerate(page_texts, start=1):
        pages.append({
            "page": i,
            "reading_a": text,
            "reading_b": text,
            "agreement": "agreed",
            "disagreement_details": [],
            "provider_metadata": {
                "reader_a": {"provider_name": "embedded-text", "model_name": "pdf:text-layer"},
                "reader_b": {"provider_name": "embedded-text", "model_name": "pdf:text-layer"},
                "comparator": {
                    "shortcut": "embedded_text_no_cross_validation",
                    "comparator_called": False,
                },
            },
        })
    msg = f"pages 1-{total}: embedded PDF text layer (no OCR/cross-validation)"
    progress(msg) if progress else print(msg, file=sys.stderr)
    return {
        "document_path": str(doc_path),
        "providers": {
            "reader_a": {"provider_name": "embedded-text", "model_name": "pdf:text-layer"},
            "reader_b": {"provider_name": "embedded-text", "model_name": "pdf:text-layer"},
            "comparator": {"provider_name": "embedded-text", "model_name": "none"},
        },
        "extraction_method": "embedded_text",
        "encoding_detected": None,
        "cross_validation_mode": "deferred_poc",
        "cross_validation_note": (
            f"Born-digital PDF: all {total} page(s) carry an embedded text layer, extracted "
            "losslessly. No OCR or vision read was performed, so P8's dual-path "
            "cross-validation does not apply (there is no independent second reading that "
            "could disagree). Deterministic extraction, not a probabilistic read."
        ),
        "pages": pages,
    }


def _run_embedded_text(case_id: str, doc_id: str, doc_path: Path, progress=None) -> dict:
    """Text-passthrough path for a plain-text source (no OCR, no vision).

    Decodes the file losslessly and returns a single-page result shaped like
    run_ocr()'s dual-path output so run_checkpoint1._assemble_ocr_result
    consumes it unchanged. The distinction is carried honestly in the returned
    dict: extraction_method 'embedded_text', the detected encoding, and
    cross_validation_mode 'deferred_poc' (P8's dual-read cross-validation does
    not apply to a deterministic decode -- there is no independent second
    reading that could disagree). The page's cross_validation.agreement is
    'agreed' because a lossless decode is its own ground truth; the mode label,
    not a fake second reader, is what records that no OCR cross-check happened.
    reading_a and reading_b are the identical decoded text so the downstream
    page-write (which writes reading_a) writes the real content."""
    text, encoding = decode_text_file(doc_path)
    msg = f"page 1/1: embedded_text decode (encoding={encoding}, no OCR/cross-validation)"
    progress(msg) if progress else print(msg, file=sys.stderr)
    page = {
        "page": 1,
        "reading_a": text,
        "reading_b": text,
        "agreement": "agreed",
        "disagreement_details": [],
        "provider_metadata": {
            "reader_a": {"provider_name": "embedded-text", "model_name": f"decode:{encoding}"},
            "reader_b": {"provider_name": "embedded-text", "model_name": f"decode:{encoding}"},
            "comparator": {"shortcut": "embedded_text_no_cross_validation", "comparator_called": False},
        },
    }
    return {
        "document_path": str(doc_path),
        "providers": {
            "reader_a": {"provider_name": "embedded-text", "model_name": f"decode:{encoding}"},
            "reader_b": {"provider_name": "embedded-text", "model_name": f"decode:{encoding}"},
            "comparator": {"provider_name": "embedded-text", "model_name": "none"},
        },
        "extraction_method": "embedded_text",
        "encoding_detected": encoding,
        "cross_validation_mode": "deferred_poc",
        "cross_validation_note": (
            f"Plain-text source decoded losslessly as {encoding}; no OCR or vision read was "
            "performed, so P8's dual-path cross-validation does not apply (there is no independent "
            "second reading that could disagree). Deterministic byte decode, not a probabilistic "
            "read that could be confidently wrong."
        ),
        "pages": [page],
    }


def _classify_cross_validation(reader_a, reader_b) -> tuple[str, str]:
    """Label P8's cross-validation strength honestly, computed from the actual
    readers rather than hard-coded. Every provider available today is
    LLM-vision-backed (openrouter / claude-cli / codex-cli / openai-api): even two different
    vendors share the same extraction *technology class* and can produce a
    correlated confident error, so any current reader pair is a documented
    weak-P8. `dual_technology` stays a defined schema value but is currently
    unreachable -- it is reserved for a future genuinely-independent reader (a
    real OCR engine), deferred per open-decisions.md #4. The hard-halt on a
    genuine content disagreement is unchanged regardless of this label; what
    this records is reader *independence*, not disagreement tolerance."""
    a_label = f"{reader_a.provider_name} (model {getattr(reader_a, 'model_name', 'n/a')})"
    b_label = f"{reader_b.provider_name} (model {getattr(reader_b, 'model_name', 'n/a')})"
    if a_label == b_label:
        pair_desc = f"Both readers are {a_label}; one model self-checking against itself."
    else:
        pair_desc = (
            f"reader_a is {a_label} and reader_b is {b_label} -- two different "
            "LLM-vision backends, but the same extraction technology class."
        )
    return (
        "single_technology_weak_p8_poc",
        pair_desc
        + " This is NOT genuine dual-technology P8 -- it cannot detect a correlated "
        "confident error shared by both LLM reads. A genuinely technology-independent "
        "reader (a real OCR engine) is deferred (open-decisions.md #4).",
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case_id")
    ap.add_argument("doc_id")
    ap.add_argument("doc_path")
    ap.add_argument("--reader-a", choices=SUPPORTED_PROVIDERS, help="Provider for the first independent page read")
    ap.add_argument("--reader-b", choices=SUPPORTED_PROVIDERS, help="Provider for the second independent page read")
    ap.add_argument("--comparator", choices=SUPPORTED_PROVIDERS, help="Provider for comparing the two page reads")
    ap.add_argument("--reader-a-model", help="Model name for --reader-a")
    ap.add_argument("--reader-b-model", help="Model name for --reader-b")
    ap.add_argument("--comparator-model", help="Model name for --comparator")
    # Optional, and deliberately not synthesized when absent: this tool is
    # usually driven as a library by run_checkpoint1 (which configures tracing
    # itself). Run standalone it has no run to attribute spans to, and a
    # made-up id would put shards where `aggregate-trace --run-id` never
    # looks -- traced in appearance, unreachable in fact.
    ap.add_argument("--run-id", default=None,
                    help="Record this run's spans under outputs/<case>/_trace/<run-id>/. "
                         "Without it, a standalone run is simply not traced.")
    ap.add_argument("--workers", type=int, default=None, metavar="N",
                    help="Pages transcribed concurrently (default %d, or HARNESS_OCR_WORKERS). "
                         "1 = the strictly sequential loop. Output is identical either way -- "
                         "pages are always returned in source order." % DEFAULT_OCR_WORKERS)
    ap.add_argument("--dpi", type=int, default=None, metavar="N",
                    help="Page render resolution (default %d, or HARNESS_OCR_DPI). "
                         "Higher resolution costs proportionally more provider-side "
                         "image handling, so raise it deliberately -- see known-gaps "
                         "item 45." % DEFAULT_RENDER_DPI)
    args = ap.parse_args()
    trace_mod.configure_from_args(args)

    try:
        providers = build_ocr_providers(
            reader_a_name=args.reader_a,
            reader_b_name=args.reader_b,
            comparator_name=args.comparator,
            reader_a_model=args.reader_a_model,
            reader_b_model=args.reader_b_model,
            comparator_model=args.comparator_model,
        )
        result = run_ocr(
            args.case_id,
            args.doc_id,
            Path(args.doc_path),
            reader_a=providers["reader_a"],
            reader_b=providers["reader_b"],
            comparator=providers["comparator"],
            max_workers=args.workers,
            dpi=args.dpi,
        )
    except ProviderConfigError as exc:
        sys.exit(f"error: {exc}")
    except ProviderExecutionError as exc:
        sys.exit(f"error: {exc}")

    print(json.dumps(result, ensure_ascii=False))
    any_disagreement = any(p["agreement"] == "disagreed" for p in result["pages"])
    sys.exit(1 if any_disagreement else 0)


if __name__ == "__main__":
    main()
