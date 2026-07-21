"""Deterministic page-chunker for document-pipeline's checkpoint 3.

Splits each document's redacted_text.md into one chunk per page, using the
<<<PAGE page=N>>> markers checkpoint 2 embeds when assembling redacted_text.md
(see document-pipeline.md checkpoint 2 -- this tool depends on that exact
convention). page_chunks.schema.json requires each chunk's text to be
"verbatim from redacted_text.md -- not re-summarized" -- this tool slices
exact substrings, never regenerates text via a model call, so that
guarantee is structural rather than a prompting instruction.

One chunk per page (page_start == page_end always) -- simplest, guaranteed-
correct boundaries. Runs once per case across every text document that has
a redacted_text.md, producing one combined result and an explicit exclusion
list for human-verified non-text documents (see
page_chunks.schema.json's "for a case's documents" framing -- one file for
the whole case, not one per document). chunk_id is sequential across every
document passed in, in the order given.

This tool does not write page_chunks.json itself -- same division of labor
as ocr_extract.py: it prints the assembled {"chunks": [...]} JSON to
stdout, and document-pipeline writes page_chunks.json via
dao.py write-contract, so the write stays locked/schema-validated/backed up.

Usage:
    python tools/chunk_text.py CASE_ID DOC_ID [DOC_ID ...] \
        --exclude-non-text DOC_ID
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# (?m)^ anchors the marker to the START OF A LINE. Checkpoint 2 always emits the
# marker as its own leading line; requiring line-start means an identical string
# occurring INSIDE real page content (a coincidental or adversarial
# "<<<PAGE page=99>>>" mid-line) is NOT mistaken for a page boundary -- the
# in-band-signaling corruption the fleet review found (F1).
PAGE_MARKER_RE = re.compile(r"(?m)^<<<PAGE page=(\d+)>>>\n?")


def split_pages(redacted_text: str) -> list[tuple[int, str]]:
    """Returns [(page_number, page_text), ...] in order. Fails loud (never
    guesses) on: no markers, content before the first marker, or page numbers
    that are not strictly increasing -- any of which means the redacted_text.md
    convention was violated and page boundaries can't be trusted."""
    matches = list(PAGE_MARKER_RE.finditer(redacted_text))
    if not matches:
        sys.exit("error: no <<<PAGE page=N>>> markers found -- redacted_text.md was not "
                  "assembled with the expected page-boundary convention "
                  "(see document-pipeline.md checkpoint 2).")
    # Content before the first marker would be silently dropped -- refuse (F2).
    if redacted_text[:matches[0].start()].strip():
        sys.exit("error: content found before the first <<<PAGE>>> marker -- "
                  "redacted_text.md is malformed; refusing to silently drop it.")
    pages = []
    prev_page = 0
    for i, m in enumerate(matches):
        page_num = int(m.group(1))
        # Strictly increasing page numbers (F3): a duplicate or out-of-order
        # page makes downstream evidence_reference page lookups ambiguous.
        if page_num <= prev_page:
            sys.exit(f"error: page numbers not strictly increasing ({prev_page} -> {page_num}) "
                      "-- refusing to build ambiguous chunks.")
        prev_page = page_num
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(redacted_text)
        # .strip("\n") only -- removes the delimiter's own formatting
        # artifacts (the newline between a marker and its page's text, and
        # between that text and the next marker), nothing from the actual
        # document content. Not general whitespace stripping.
        pages.append((page_num, redacted_text[start:end].strip("\n")))
    return pages


def chunk_document(case_id: str, doc_id: str, chunk_id_start: int) -> tuple[list[dict], int]:
    redacted_path = DATA / "processed" / case_id / doc_id / "redacted_text.md"
    if not redacted_path.exists():
        sys.exit(f"error: {redacted_path} not found -- redaction (checkpoint 2) hasn't run for {doc_id} yet.")
    text = redacted_path.read_text(encoding="utf-8")
    pages = split_pages(text)
    chunks = []
    n = chunk_id_start
    for page_num, page_text in pages:
        chunks.append({
            "chunk_id": f"CHUNK_{n}", "document_id": doc_id,
            "page_start": page_num, "page_end": page_num,
            "text": page_text,
        })
        n += 1
    return chunks, n


def assemble_chunks(case_id: str, doc_ids: list[str], excluded_non_text: list[str]) -> dict:
    """Build checkpoint 3 without fabricating text for visual evidence."""
    overlap = sorted(set(doc_ids) & set(excluded_non_text))
    if overlap:
        sys.exit(f"error: document(s) cannot be both chunked and excluded as non-text: {overlap}")
    if len(set(excluded_non_text)) != len(excluded_non_text):
        sys.exit("error: duplicate --exclude-non-text document id")
    if not doc_ids and not excluded_non_text:
        sys.exit("error: provide at least one text DOC_ID or --exclude-non-text DOC_ID")

    all_chunks = []
    next_id = 1
    for doc_id in doc_ids:
        chunks, next_id = chunk_document(case_id, doc_id, next_id)
        all_chunks.extend(chunks)
    return {
        "chunks": all_chunks,
        "excluded_documents": [
            {"document_id": doc_id, "reason": "non_text_expert_review_only"}
            for doc_id in excluded_non_text
        ],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case_id")
    ap.add_argument("doc_ids", nargs="*", metavar="DOC_ID")
    ap.add_argument(
        "--exclude-non-text", action="append", default=[], metavar="DOC_ID",
        help="Human-verified non-text document omitted from chunks and routed to expert review only",
    )
    args = ap.parse_args()
    print(json.dumps(assemble_chunks(args.case_id, args.doc_ids, args.exclude_non_text), ensure_ascii=False))


if __name__ == "__main__":
    main()
