"""Per-page text extraction with P8 dual-path cross-validation.

Splits the source document into per-page images (pymupdf), then for each
page runs two independent transcriptions and asks a third, cheap text-only
call to judge whether they materially agree (same names/dates/numbers/
diagnoses), not verbatim match. That third call also flags any one-sided
extraneous content (a fabricated appendix, meta-commentary, anything one
reading has that the other lacks entirely) as a disagreement even when the
core facts otherwise match.

The reader/comparator backends are provider-configurable. The default
remains claude-cli for backward compatibility, but Codex-compatible runs
can select openai-api for reader_a, reader_b, and comparator. Using the
same provider twice is still two isolated calls; it is not a true
independent-engine cross-check, which is deferred to a later issue.

This tool does not write any contract file itself -- it prints page-level
results as JSON. document-pipeline reads that JSON, writes each page's
text via `dao.py write-page-text`, and assembles/writes ocr_result.json
via `dao.py write-contract` itself, same as any other DAO write.

Page images are staged under a project-local `_ocr_scratch/` (gitignored,
cleaned up on exit), not system /tmp.

Usage:
    python tools/ocr_extract.py CASE_ID DOC_ID /path/to/document.pdf
    python tools/ocr_extract.py CASE_ID DOC_ID /path/to/document.pdf \
        --reader-a openai-api --reader-b openai-api --comparator openai-api
"""
import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

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
COMPARE_PROMPT_VERSION = "ocr_compare_v0.1"

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
    "Reply with exactly one line: AGREE or DISAGREE: <brief reason>.\n\n"
    "--- Transcription A ---\n{a}\n\n--- Transcription B ---\n{b}"
)

DISAGREE_RE = re.compile(r"\bDISAGREE\b")
AGREE_RE = re.compile(r"\bAGREE\b")


def transcribe_once(image_path: Path, provider=None) -> dict:
    selected_provider = provider or build_provider(root=ROOT)
    result = selected_provider.transcribe_image(image_path, TRANSCRIBE_PROMPT, OCR_PROMPT_VERSION)
    return {"text": result.text, "metadata": result.metadata()}


def compare(text_a: str, text_b: str, comparator=None) -> dict:
    prompt = COMPARE_PROMPT_TEMPLATE.format(a=text_a, b=text_b)
    selected_comparator = comparator or build_provider(root=ROOT)
    provider_result = selected_comparator.compare_text(prompt, COMPARE_PROMPT_VERSION)
    verdict = provider_result.text.strip()
    metadata = provider_result.metadata()
    verdict_upper = verdict.upper()

    # Word-boundary search, not startswith -- the model doesn't always lead
    # with the bare token despite the prompt asking for exactly that (e.g. a
    # full sentence like "The two transcriptions AGREE on..."). Check
    # DISAGREE before AGREE for readability; \b makes the order irrelevant
    # for correctness since "AGREE" as a substring of "DISAGREE" doesn't sit
    # on a word boundary and won't match AGREE_RE.
    if DISAGREE_RE.search(verdict_upper):
        return {"agreement": "disagreed", "disagreement_details": [verdict], "metadata": metadata}
    if AGREE_RE.search(verdict_upper):
        return {"agreement": "agreed", "disagreement_details": [], "metadata": metadata}

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


def _split_to_page_images_fitz(doc_path: Path, out_dir: Path, max_pages: int | None = None) -> list[Path]:
    import fitz  # pymupdf

    doc = fitz.open(doc_path)
    try:
        page_count = doc.page_count if max_pages is None else min(max_pages, doc.page_count)
        paths = []
        for i in range(page_count):
            page = doc.load_page(i)
            pix = page.get_pixmap(dpi=200)
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


def _split_to_page_images_pdftoppm(doc_path: Path, out_dir: Path, max_pages: int | None = None) -> list[Path]:
    command = _find_pdftoppm()
    if command is None:
        sys.exit("error: pymupdf missing and pdftoppm not found for PDF rendering")

    prefix = out_dir / "page"
    cmd = [command, "-png", "-r", "200"]
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


def split_to_page_images(doc_path: Path, out_dir: Path, max_pages: int | None = None) -> list[Path]:
    """max_pages caps how many pages get rendered (from the start) -- used by
    intake_case.py's content pre-check, which only needs the first few pages,
    not a full render. None (default) renders every page, unchanged from
    this function's original behavior."""
    try:
        return _split_to_page_images_fitz(doc_path, out_dir, max_pages)
    except ImportError:
        return _split_to_page_images_pdftoppm(doc_path, out_dir, max_pages)


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


def run_ocr(
    case_id: str,
    doc_id: str,
    doc_path: Path,
    progress=None,
    reader_a=None,
    reader_b=None,
    comparator=None,
) -> dict:
    """The actual dual-path OCR loop, extracted out of main() so callers
    (run_checkpoint1.py) can invoke it in-process instead of shelling out
    to this script as a subprocess. Pure extraction -- main() below calls
    this and does exactly what it always did (print JSON to stdout, exit
    1 on any disagreement). progress(msg) is called per page if given,
    instead of always printing to stderr, so a library caller can route it
    (or silence it) rather than inheriting main()'s CLI-only behavior."""
    if not doc_path.exists():
        sys.exit(f"error: document not found -- {doc_path}")

    if reader_a is None or reader_b is None or comparator is None:
        providers = build_ocr_providers()
        reader_a = reader_a or providers["reader_a"]
        reader_b = reader_b or providers["reader_b"]
        comparator = comparator or providers["comparator"]

    pages_out = []
    with scratch_dir(case_id, doc_id) as tmp_dir:
        if doc_path.suffix.lower() == ".pdf":
            page_images = split_to_page_images(doc_path, tmp_dir)
        else:
            page_images = [doc_path]  # already a single image

        for i, img_path in enumerate(page_images, start=1):
            reading_a = transcribe_once(img_path, reader_a)
            reading_b = transcribe_once(img_path, reader_b)
            result = compare(reading_a["text"], reading_b["text"], comparator)
            pages_out.append({
                "page": i,
                "reading_a": reading_a["text"],
                "reading_b": reading_b["text"],
                "agreement": result["agreement"],
                "disagreement_details": result["disagreement_details"],
                "provider_metadata": {
                    "reader_a": reading_a["metadata"],
                    "reader_b": reading_b["metadata"],
                    "comparator": result["metadata"],
                },
            })
            msg = f"page {i}/{len(page_images)}: {result['agreement']}"
            progress(msg) if progress else print(msg, file=sys.stderr)

    return {
        "document_path": str(doc_path),
        "providers": {
            "reader_a": _metadata_for(reader_a),
            "reader_b": _metadata_for(reader_b),
            "comparator": _metadata_for(comparator),
        },
        "pages": pages_out,
    }


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
    args = ap.parse_args()

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
