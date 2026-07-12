"""Per-page text extraction with P8 dual-path cross-validation, using the
Claude CLI as the reading engine on both paths (per-project direction --
no dedicated OCR engine exists yet).

Splits the source document into per-page images (pymupdf), then for each
page runs two independent `claude -p` transcriptions -- fresh subprocess,
no shared context between them -- and asks a third, cheap text-only call
to judge whether they materially agree (same names/dates/numbers/
diagnoses), not verbatim match.

Known limitation (see open-decisions.md #3, now also #4): both reading
paths are the same underlying model. Two isolated invocations do reduce
transient/one-off misreads, but they don't reduce SYSTEMATIC blind spots
the way a genuinely different technology (a real OCR engine) would --
this is a "for now" stand-in, not a full solution to P8's intent. Flag
this in ocr_result.json's ocr_engine/vision_model_name fields honestly
rather than implying a real second engine exists.

This tool does not write any contract file itself -- it prints page-level
results as JSON. document-pipeline reads that JSON, writes each page's
text via `dao.py write-page-text`, and assembles/writes ocr_result.json
via `dao.py write-contract` itself, same as any other DAO write.

Page images are staged under a project-local `_ocr_scratch/` (gitignored,
cleaned up on exit), not system /tmp -- the nested `claude` reads used for
transcription can only see files inside the project dir.

Usage:
    python tools/ocr_extract.py CASE_ID DOC_ID /path/to/document.pdf
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

ROOT = Path(__file__).resolve().parent.parent
SCRATCH_ROOT = ROOT / "_ocr_scratch"

TRANSCRIBE_PROMPT = (
    "Transcribe every piece of text visible in this page/image exactly as written, "
    "preserving structure (headers, tables, lists) as plain text. Output ONLY the "
    "transcription -- no commentary, no markdown code fences, no preamble."
)

COMPARE_PROMPT_TEMPLATE = (
    "Two independent transcriptions of the same document page follow. Judge whether "
    "they materially agree -- same names, dates, numbers, diagnoses -- even if wording "
    "or formatting differs. Verbatim match is not required. Reply with exactly one line: "
    "AGREE or DISAGREE: <brief reason>.\n\n"
    "--- Transcription A ---\n{a}\n\n--- Transcription B ---\n{b}"
)


def transcribe_once(image_path: Path) -> str:
    # cwd=ROOT: the nested claude CLI's --allowedTools Read is scoped to its
    # project dir, so image_path (under SCRATCH_ROOT, inside ROOT) must be
    # reachable from there -- a path under system /tmp would not be.
    result = subprocess.run(
        ["claude", "-p", f"{TRANSCRIBE_PROMPT}\n\nImage: {image_path}", "--allowedTools", "Read"],
        capture_output=True, text=True, timeout=180, cwd=str(ROOT),
    )
    if result.returncode != 0:
        sys.exit(f"error: claude transcription failed for {image_path}: {result.stderr.strip()}")
    return result.stdout.strip()


DISAGREE_RE = re.compile(r"\bDISAGREE\b")
AGREE_RE = re.compile(r"\bAGREE\b")


def compare(text_a: str, text_b: str) -> dict:
    if text_a.strip() == text_b.strip():
        return {"agreement": "agreed", "disagreement_details": []}
    prompt = COMPARE_PROMPT_TEMPLATE.format(a=text_a, b=text_b)
    result = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    verdict = result.stdout.strip()
    verdict_upper = verdict.upper()
    # Word-boundary search, not startswith -- the model doesn't always lead
    # with the bare token despite the prompt asking for exactly that (e.g. a
    # full sentence like "The two transcriptions AGREE on..."). Check
    # DISAGREE before AGREE for readability; \b makes the order irrelevant
    # for correctness since "AGREE" as a substring of "DISAGREE" doesn't sit
    # on a word boundary and won't match AGREE_RE.
    if DISAGREE_RE.search(verdict_upper):
        return {"agreement": "disagreed", "disagreement_details": [verdict]}
    if AGREE_RE.search(verdict_upper):
        return {"agreement": "agreed", "disagreement_details": []}
    # Neither token found -- the model didn't follow the expected format.
    # Fail safe as disagreed (P8: no tolerance, never silently assume
    # agreement) rather than crashing the whole multi-page run.
    return {
        "agreement": "disagreed",
        "disagreement_details": [f"unparseable compare() verdict, treated as disagreement: {verdict!r}"],
    }


@contextlib.contextmanager
def scratch_dir(case_id: str, doc_id: str):
    # Project-local, not system /tmp -- see transcribe_once's cwd note.
    # Session-tagged (pid) instead of a bare case/doc directory so concurrent
    # runs against the same document (e.g. a retry racing a stale process)
    # can't collide on one path.
    d = SCRATCH_ROOT / f"{case_id}_{doc_id}_{os.getpid()}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def split_to_page_images(doc_path: Path, out_dir: Path, max_pages: int | None = None) -> list[Path]:
    """max_pages caps how many pages get rendered (from the start) -- used by
    intake_case.py's content pre-check, which only needs the first few pages,
    not a full render. None (default) renders every page, unchanged from
    this function's original behavior."""
    try:
        import fitz  # pymupdf
    except ImportError:
        sys.exit("error: pymupdf required -- pip install pymupdf")
    doc = fitz.open(doc_path)
    page_count = doc.page_count if max_pages is None else min(max_pages, doc.page_count)
    paths = []
    for i in range(page_count):
        page = doc.load_page(i)
        pix = page.get_pixmap(dpi=200)
        out_path = out_dir / f"page_{i + 1:03d}.png"
        pix.save(out_path)
        paths.append(out_path)
    doc.close()
    return paths


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case_id")
    ap.add_argument("doc_id")
    ap.add_argument("doc_path")
    args = ap.parse_args()

    doc_path = Path(args.doc_path)
    if not doc_path.exists():
        sys.exit(f"error: document not found -- {doc_path}")

    pages_out = []
    with scratch_dir(args.case_id, args.doc_id) as tmp_dir:
        if doc_path.suffix.lower() == ".pdf":
            page_images = split_to_page_images(doc_path, tmp_dir)
        else:
            page_images = [doc_path]  # already a single image

        for i, img_path in enumerate(page_images, start=1):
            reading_a = transcribe_once(img_path)
            reading_b = transcribe_once(img_path)
            result = compare(reading_a, reading_b)
            pages_out.append({
                "page": i,
                "reading_a": reading_a,
                "reading_b": reading_b,
                "agreement": result["agreement"],
                "disagreement_details": result["disagreement_details"],
            })
            print(f"page {i}/{len(page_images)}: {result['agreement']}", file=sys.stderr)

    print(json.dumps({"document_path": str(doc_path), "pages": pages_out}, ensure_ascii=False))
    any_disagreement = any(p["agreement"] == "disagreed" for p in pages_out)
    sys.exit(1 if any_disagreement else 0)


if __name__ == "__main__":
    main()
