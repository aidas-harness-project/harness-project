"""Deterministic embedded-text segment extraction (document-pipeline helper).

A born-digital policy PDF carries a real embedded text layer. When a page range
of that parent was never carved into any processed segment -- CASE_030's 별표
appendix (logical 120-189) and referenced-law region (190-240) were invisible
to every downstream stage because no processed text existed for them -- this
tool produces the processed layer for a declared logical page range straight
from the embedded text, with no OCR and no vision call.

It is a document-pipeline stage, so it is the one sanctioned place that opens
the raw PDF (harness-guardrails P2: an agent never freelances extraction or
reads raw directly; it invokes the extraction stage, which is this). Everything
it writes goes through the DAO (write-page-text / write-redacted-text), never a
direct processed-layer write.

Page semantics: the parent's LOGICAL printed page number ('N / 240') keys the
processed page files and the <<<PAGE page=N>>> markers. The parent PDF's
physical page index is `logical + page_offset` (CASE_030: +7); the caller
passes the offset explicitly rather than guessing.

Cross-validation: this is a deterministic decode of an existing text layer, not
a transcription -- there is no independent second reading that could disagree,
so P8's dual-path check does not apply (the same stance _run_embedded_text takes
for a plain-text source: extraction_method 'embedded_text',
cross_validation_mode 'deferred_poc'). Redaction: 별표/법규 pages carry
classification tables and statute text, not personal data; this passthrough
writes the embedded text verbatim as the redacted text and records zero PII
items. If a future range does contain PII, redact it through the normal
redact_document path instead of this tool.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAO = ROOT / "tools" / "dao.py"


def _dao(*args: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(DAO), *args],
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"dao {args[0]} failed (rc={proc.returncode}): "
            f"{proc.stdout.strip()} {proc.stderr.strip()}")
    return proc.stdout


def read_embedded_pages(pdf_path: Path, logical_pages: list[int],
                        page_offset: int) -> dict[int, str]:
    """{logical_page: embedded_text} for each requested logical page.

    Raises if a page has an empty text layer -- an empty embedded decode is a
    fail-loud condition, never a silently-blank processed page.
    """
    import fitz  # pymupdf

    doc = fitz.open(pdf_path)
    try:
        out: dict[int, str] = {}
        for lp in logical_pages:
            physical = lp + page_offset  # 1-based physical index
            if physical < 1 or physical > doc.page_count:
                raise ValueError(
                    f"logical {lp} -> physical {physical} is outside the PDF "
                    f"(1..{doc.page_count})")
            text = doc[physical - 1].get_text()
            if not text.strip():
                raise ValueError(
                    f"logical page {lp} (physical {physical}) has an empty "
                    "embedded text layer -- cannot decode deterministically")
            out[lp] = text
        return out
    finally:
        doc.close()


def extract_segment(case_id: str, doc_id: str, pdf_path: Path,
                    logical_pages: list[int], page_offset: int,
                    held_by: str, run_id: str) -> dict:
    """Write processed page files + a marker-assembled redacted_text.md for a
    segment's logical page range, decoded from the parent PDF's embedded text.
    Returns a summary; the manifest segment entry itself is registered
    separately (it needs the derived_text_sha256 this produces)."""
    pages = read_embedded_pages(pdf_path, logical_pages, page_offset)

    # One processed page file per logical page (write-page-text).
    for lp in logical_pages:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".md", delete=False, encoding="utf-8") as tf:
            tf.write(pages[lp])
            page_file = tf.name
        try:
            _dao("write-page-text", case_id, doc_id, str(lp),
                 "--text-file", page_file, "--held-by", held_by,
                 "--run-id", run_id)
        finally:
            Path(page_file).unlink(missing_ok=True)

    # Marker-assembled redacted_text.md (same shape redact_document produces).
    redacted_text = "\n".join(
        f"<<<PAGE page={lp}>>>\n{pages[lp]}" for lp in logical_pages) + "\n"
    with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8") as tf:
        tf.write(redacted_text)
        redacted_file = tf.name
    try:
        _dao("write-redacted-text", case_id, doc_id,
             "--text-file", redacted_file, "--held-by", held_by,
             "--run-id", run_id)
    finally:
        Path(redacted_file).unlink(missing_ok=True)

    import hashlib
    sha = hashlib.sha256(redacted_text.encode("utf-8")).hexdigest()
    return {
        "status": "success",
        "case_id": case_id,
        "doc_id": doc_id,
        "logical_pages": logical_pages,
        "page_offset": page_offset,
        "derived_text_sha256": sha,
        "extraction_method": "embedded_text",
        "cross_validation_mode": "deferred_poc",
    }


def _parse_pages(spec: str) -> list[int]:
    """'120-189' or '57,58,118,119' or a mix -> sorted unique logical pages."""
    pages: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        elif part:
            pages.add(int(part))
    return sorted(pages)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("case_id")
    ap.add_argument("doc_id", help="the segment document_id to create text for")
    ap.add_argument("--pdf", required=True, help="parent PDF path (raw)")
    ap.add_argument("--pages", required=True,
                    help="logical page spec, e.g. '120-189' or '57,58,118,119'")
    ap.add_argument("--page-offset", type=int, required=True,
                    help="physical = logical + offset (CASE_030: 7)")
    ap.add_argument("--held-by", required=True)
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args(argv)

    result = extract_segment(
        args.case_id, args.doc_id, Path(args.pdf),
        _parse_pages(args.pages), args.page_offset,
        args.held_by, args.run_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
