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
physical page index differs from it by an offset (CASE_030: +7).

WHICH PDF, AND WHICH PAGES -- the Part 11G contract
---------------------------------------------------
This tool used to accept an arbitrary `--pdf` path and an authoritative
`--page-offset`. Both were unverifiable claims: point it at a different file,
or state an offset that is wrong by two, and the manifest and the <<<PAGE>>>
markers would agree with each other perfectly while describing pages that were
never the source. Nothing downstream could tell.

Now:

  - the source is RESOLVED from the manifest, not supplied. The caller names a
    `parent_document_id`; the registered physical parent's `file_path` is the
    only file that will be opened.
  - the parent PDF's SHA-256 is computed and, if the manifest already records
    `source_pdf_sha256`, must match it; its real page count must match
    `source_total_pages`. A parent whose bytes changed since registration is
    refused.
  - `--page-offset` is a CANDIDATE. For every logical page the tool reads the
    printed page marker off the physical page it lands on and requires it to
    say that logical number. The offset is accepted because the pages confirm
    it, not because it was asserted.
  - a page whose printed number cannot be read is never written with a guess:
    extraction blocks and reports it for review.
  - each page_map entry records `physical_page_sha256` (the digest of the text
    actually extracted from that physical page) and `logical_page_evidence`
    (the verbatim printed marker), so the mapping is bound to real content.

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
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAO = ROOT / "tools" / "dao.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))


class ExtractionBlocked(Exception):
    """Raised when extraction cannot proceed on verifiable ground: the parent
    cannot be resolved, its bytes disagree with the manifest, or a page's
    printed logical number cannot be confirmed. Never downgraded to a warning --
    a page written on a guessed mapping is worse than no page at all."""


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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- printed logical page markers ----------------------------------------
# Korean policy books print the page as 'N / TOTAL', '- N -', or a bare number
# in a header/footer. Only the first two are accepted as positive confirmation:
# a bare number appears far too often inside body text (amounts, article
# numbers, rates) to distinguish a page marker from a coincidence, and a
# coincidence is exactly what this check exists to rule out.
def _printed_page_patterns(logical_page: int) -> list[re.Pattern]:
    n = re.escape(str(logical_page))
    return [
        re.compile(rf"(?<!\d){n}\s*/\s*\d+(?!\d)"),        # 12 / 240
        re.compile(rf"-\s*{n}\s*-"),                        # - 12 -
        re.compile(rf"(?:page|페이지|쪽)\s*{n}(?!\d)", re.IGNORECASE),
    ]


def find_printed_logical_page(page_text: str, logical_page: int) -> str | None:
    """The verbatim printed marker confirming `logical_page` on this page, or
    None when it cannot be confirmed. None is a blocking condition for the
    caller, never a value to work around."""
    for pattern in _printed_page_patterns(logical_page):
        match = pattern.search(page_text)
        if match:
            return match.group(0).strip()
    return None


# --- parent resolution and verification ----------------------------------

def resolve_parent(manifest: dict, parent_document_id: str) -> dict:
    """The registered physical parent entry, or raise. A segment, a missing
    entry, or an entry without a raw file_path are all refused: the source of
    an extraction must be an immutable registered raw document."""
    if manifest is None:
        raise ExtractionBlocked(
            "no document_manifest.json -- the parent document cannot be "
            "resolved, so no source may be opened")
    entry = next(
        (d for d in manifest.get("documents", [])
         if d.get("document_id") == parent_document_id), None)
    if entry is None:
        raise ExtractionBlocked(
            f"{parent_document_id} is not registered in document_manifest.json "
            "-- extraction may only read a registered source")
    if entry.get("document_role") == "segment":
        raise ExtractionBlocked(
            f"{parent_document_id} is a segment; a segment must be carved from "
            "a PHYSICAL parent, not from another segment")
    file_path = entry.get("file_path")
    if not file_path or not str(file_path).startswith("data/raw/"):
        raise ExtractionBlocked(
            f"{parent_document_id} has no raw file_path under data/raw/ "
            f"(got {file_path!r}) -- there is no immutable source to read")
    return entry


def verify_parent_source(entry: dict, pdf_path: Path,
                         actual_page_count: int) -> str:
    """Confirm the on-disk parent really is the registered one. Returns its
    SHA-256 so the caller can record it. Raises on any disagreement."""
    if not pdf_path.exists():
        raise ExtractionBlocked(
            f"registered source {pdf_path} does not exist on disk")
    actual_sha = file_sha256(pdf_path)
    recorded_sha = entry.get("source_pdf_sha256")
    if recorded_sha and recorded_sha != actual_sha:
        raise ExtractionBlocked(
            f"parent PDF digest changed: manifest records {recorded_sha}, the "
            f"file now hashes to {actual_sha} -- the registered source is not "
            "the file on disk; segments derived from it cannot be trusted")
    recorded_pages = entry.get("source_total_pages")
    if isinstance(recorded_pages, int) and recorded_pages != actual_page_count:
        raise ExtractionBlocked(
            f"parent PDF has {actual_page_count} physical pages but the "
            f"manifest records source_total_pages={recorded_pages}")
    return actual_sha


def build_page_map(pages_by_physical: dict[int, str],
                   logical_pages: list[int],
                   page_offset_candidate: int) -> list[dict]:
    """Confirm the candidate offset page by page and build the bound page_map.

    pages_by_physical maps a 1-based physical page index to its extracted text.
    Every logical page must land on a physical page whose PRINTED number says
    that same logical page. Any page that cannot be confirmed blocks the whole
    extraction -- a partially-verified mapping is not a mapping.
    """
    entries: list[dict] = []
    unconfirmed: list[str] = []
    for logical in logical_pages:
        physical = logical + page_offset_candidate
        text = pages_by_physical.get(physical)
        if text is None:
            unconfirmed.append(
                f"logical {logical} -> physical {physical}: no such page in "
                "the parent PDF")
            continue
        evidence = find_printed_logical_page(text, logical)
        if evidence is None:
            unconfirmed.append(
                f"logical {logical} -> physical {physical}: the printed page "
                f"number could not be confirmed on that page (candidate offset "
                f"{page_offset_candidate:+d} is unverified here)")
            continue
        entries.append({
            "logical_page": logical,
            "source_physical_page": physical,
            "physical_page_sha256": text_sha256(text),
            "logical_page_evidence": evidence,
        })
    if unconfirmed:
        raise ExtractionBlocked(
            "page mapping could not be verified against the parent PDF; "
            "nothing was written. Resolve these for review rather than "
            "asserting an offset:\n  - " + "\n  - ".join(unconfirmed))
    return entries


# --- PDF reading ----------------------------------------------------------

def read_pdf_pages(pdf_path: Path, physical_pages: list[int]) -> dict[int, str]:
    """{physical_page: embedded_text} for the requested 1-based pages, plus the
    document's total page count via read_pdf_page_count.

    Raises if a page has an empty text layer -- an empty embedded decode is a
    fail-loud condition, never a silently-blank processed page.
    """
    import fitz  # pymupdf

    doc = fitz.open(pdf_path)
    try:
        out: dict[int, str] = {}
        for physical in physical_pages:
            if physical < 1 or physical > doc.page_count:
                continue  # reported by build_page_map as an unmapped page
            text = doc[physical - 1].get_text()
            if not text.strip():
                raise ExtractionBlocked(
                    f"physical page {physical} has an empty embedded text "
                    "layer -- cannot decode deterministically")
            out[physical] = text
        return out
    finally:
        doc.close()


def read_pdf_page_count(pdf_path: Path) -> int:
    import fitz  # pymupdf

    doc = fitz.open(pdf_path)
    try:
        return doc.page_count
    finally:
        doc.close()


def extract_segment(case_id: str, doc_id: str, parent_document_id: str,
                    logical_pages: list[int], page_offset_candidate: int,
                    held_by: str, run_id: str,
                    manifest_reader=None,
                    page_reader=None,
                    page_counter=None,
                    source_verifier=None,
                    dao_call=None) -> dict:
    """Write processed page files + a marker-assembled redacted_text.md for a
    segment's logical page range, decoded from the REGISTERED parent's embedded
    text with a verified page mapping.

    The reader/verifier hooks exist so the verification logic is testable
    without a real PDF; production passes none of them and gets the real
    implementations.
    """
    manifest_reader = manifest_reader or _default_manifest_reader
    page_reader = page_reader or read_pdf_pages
    page_counter = page_counter or read_pdf_page_count
    source_verifier = source_verifier or verify_parent_source
    dao_call = dao_call or _dao

    manifest = manifest_reader(case_id)
    parent_entry = resolve_parent(manifest, parent_document_id)
    pdf_path = ROOT / parent_entry["file_path"]

    page_count = page_counter(pdf_path)
    parent_sha = source_verifier(parent_entry, pdf_path, page_count)

    physical_pages = [lp + page_offset_candidate for lp in logical_pages]
    pages_by_physical = page_reader(pdf_path, physical_pages)
    page_map = build_page_map(
        pages_by_physical, logical_pages, page_offset_candidate)

    pages = {
        entry["logical_page"]: pages_by_physical[entry["source_physical_page"]]
        for entry in page_map
    }

    for lp in logical_pages:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".md", delete=False, encoding="utf-8") as tf:
            tf.write(pages[lp])
            page_file = tf.name
        try:
            dao_call("write-page-text", case_id, doc_id, str(lp),
                     "--text-file", page_file, "--held-by", held_by,
                     "--run-id", run_id)
        finally:
            Path(page_file).unlink(missing_ok=True)

    redacted_text = "\n".join(
        f"<<<PAGE page={lp}>>>\n{pages[lp]}" for lp in logical_pages) + "\n"
    with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8") as tf:
        tf.write(redacted_text)
        redacted_file = tf.name
    try:
        dao_call("write-redacted-text", case_id, doc_id,
                 "--text-file", redacted_file, "--held-by", held_by,
                 "--run-id", run_id)
    finally:
        Path(redacted_file).unlink(missing_ok=True)

    return {
        "status": "success",
        "case_id": case_id,
        "doc_id": doc_id,
        "parent_document_id": parent_document_id,
        "source_pdf_sha256": parent_sha,
        "source_total_pages": page_count,
        "logical_pages": logical_pages,
        "page_offset_confirmed": page_offset_candidate,
        "page_map": page_map,
        "derived_text_sha256": text_sha256(redacted_text),
        "extraction_method": "embedded_text",
        "cross_validation_mode": "deferred_poc",
    }


def _default_manifest_reader(case_id: str):
    """Read the manifest through the DAO, never by opening outputs/ directly."""
    output = _dao("read-contract", case_id, "document_manifest.json")
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


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
    ap.add_argument("--parent-document-id", required=True,
                    help="the REGISTERED physical parent this segment is carved "
                         "from; its manifest file_path is the only file opened")
    ap.add_argument("--pages", required=True,
                    help="logical page spec, e.g. '120-189' or '57,58,118,119'")
    ap.add_argument("--page-offset", type=int, required=True,
                    help="CANDIDATE offset: physical = logical + offset "
                         "(CASE_030: 7). Confirmed page by page against each "
                         "page's printed number; never trusted as given.")
    ap.add_argument("--held-by", required=True)
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args(argv)

    try:
        result = extract_segment(
            args.case_id, args.doc_id, args.parent_document_id,
            _parse_pages(args.pages), args.page_offset,
            args.held_by, args.run_id)
    except ExtractionBlocked as exc:
        print(f"BLOCKED: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
