"""Find documents that continue their predecessor's printed page numbering.

known-gaps #56. A document whose page 1 prints "- 7 -" while the document
before it ends on "- 6 -" is not a document: it is the second half of the one
before it, and the publisher's own page numbers say so.

Zero provider calls, zero writes. Reads the manifest and the processed page
text already on disk.

WHY THE PRINTED NUMBER AND NOT THE FIRST LINE'S SHAPE
-----------------------------------------------------
#56 proposed treating "a page whose first non-empty line is a bare page marker
or a mid-outline heading" as a continuation. Measured against the real corpus,
that rule is both too broad and too narrow:

  * too broad -- 11 of the 47 declining `legal_*` documents open with
    `번    호 :`, the first field of a legal-opinion letterhead. That IS a
    document start. Two more open with `Ⅰ. 사안의 요지 및 질의내용`, i.e.
    section Ⅰ, which is also a start.
  * too narrow -- 6 open mid-sentence ("의 피고 H의 주의의무 ...") with no
    page marker and no heading at all.

Continuity of the printed number decides all three cases without reading the
prose. Measured over every manifest in this repo:

   133  documents whose page 1 prints "- 1 -"   -> left alone (real starts)
    25  documents whose page 1 continues its predecessor -> reported
     0  documents printing a number > 1 that does NOT continue

(Of the documents that FOLLOW another in manifest order and print any marker
at all, the split is 60 starts against these 25 continuations; the 133 counts
every document including each case's first.)

The zero is what makes this usable: on this corpus the signal has no ambiguous
middle, so a hit is a hit rather than a candidate for review.

WHAT THIS IS NOT
----------------
Not a segmentation defect, which is where #56 placed it. All 25 pairs arrived
as SEPARATE RAW PDFs -- none carries a `segmentation_proposal_*.json` or a
parent document id, so `text_anchor_boundaries` and the judge tier never ran on
them and neither could have merged them. The split is in the source material:
one 10-page opinion was scanned into two files before the pipeline saw it. A
fix therefore belongs at intake (or in a merge step), not in segment_case.py.

Usage:
    python tools/score_document_continuity.py              # whole corpus
    python tools/score_document_continuity.py CASE_002     # one case
    python tools/score_document_continuity.py --json OUT   # machine-readable
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import re
import sys

# "- 7 -", "- 10 -". Anchored whole-line: a hyphenated fragment inside prose
# must not be read as a page number.
PAGE_MARKER_RE = re.compile(r"^-\s*(\d{1,3})\s*-$")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def printed_page_number(case_id: str, doc_id: str, page: int) -> int | None:
    """The page number this page prints, or None if it prints none.

    Reads the FIRST non-empty line only. A page marker that is not the first
    line is not this pattern -- the corpus prints it at the top -- and scanning
    further would start matching numbers inside tables.
    """
    path = os.path.join(ROOT, "data", "processed", case_id, doc_id,
                        f"page_{page:03d}.md")
    if not os.path.exists(path):
        return None
    try:
        text = io.open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = PAGE_MARKER_RE.match(line)
        return int(match.group(1)) if match else None
    return None


def continuation_pairs(case_id: str, documents: list[dict]) -> list[dict]:
    """Adjacent (previous, current) pairs where the numbering runs on.

    Manifest order is the comparison order because that is the order the
    documents were taken in; a document continues the one filed before it.
    """
    found = []
    for index in range(1, len(documents)):
        previous, current = documents[index - 1], documents[index]
        first = printed_page_number(case_id, current["document_id"], 1)
        # `first == 1` is redundant against the `first == last + 1` test below
        # (that would need last == 0, which no page prints) and is kept as a
        # cheap early exit that states the intent: a document numbering itself
        # from 1 is a start, whatever precedes it.
        if first is None or first == 1:
            continue
        previous_pages = previous.get("pages") or 0
        if not previous_pages:
            continue
        last = printed_page_number(case_id, previous["document_id"],
                                   previous_pages)
        if last is None or first != last + 1:
            continue
        found.append({
            "case_id": case_id,
            "parent": previous["document_id"],
            "parent_type": previous.get("document_type"),
            "parent_last_page": last,
            "child": current["document_id"],
            "child_type": current.get("document_type"),
            "child_first_page": first,
            # A pair typed differently is the harm #56 names: the same author's
            # single argument enters claim analysis as two independent sources.
            "types_disagree": (previous.get("document_type")
                               != current.get("document_type")),
            # Segmentation cannot be the cause when there is no proposal and no
            # parent id -- the pieces were separate PDFs at intake.
            "from_segmentation": bool(current.get("parent_document_id")
                                      or current.get("source_document_id")),
        })
    return found


def scan(case_filter: str | None = None) -> dict:
    pattern = os.path.join(ROOT, "outputs",
                           case_filter or "CASE_*", "document_manifest.json")
    pairs, cases_scanned, starts = [], 0, 0
    for manifest_path in sorted(glob.glob(pattern)):
        case_id = os.path.basename(os.path.dirname(manifest_path))
        try:
            documents = json.load(
                io.open(manifest_path, encoding="utf-8"))["documents"]
        except Exception:
            continue
        cases_scanned += 1
        for document in documents:
            if printed_page_number(case_id, document["document_id"], 1) == 1:
                starts += 1
        pairs.extend(continuation_pairs(case_id, documents))
    return {
        "cases_scanned": cases_scanned,
        "documents_starting_at_page_one": starts,
        "continuation_pairs": pairs,
        "affected_cases": sorted({p["case_id"] for p in pairs}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("case_id", nargs="?", default=None,
                        help="Restrict to one case (default: every case).")
    parser.add_argument("--json", metavar="PATH", default=None,
                        help="Also write the full result as JSON.")
    args = parser.parse_args(argv)

    result = scan(args.case_id)
    pairs = result["continuation_pairs"]

    print(f"cases scanned                     : {result['cases_scanned']}")
    print(f"documents whose page 1 prints '- 1 -': "
          f"{result['documents_starting_at_page_one']}  (real starts, untouched)")
    print(f"documents continuing a predecessor  : {len(pairs)}")
    print()
    if pairs:
        disagree = sum(1 for p in pairs if p["types_disagree"])
        from_seg = sum(1 for p in pairs if p["from_segmentation"])
        print(f"  typed differently on each side    : {disagree}"
              "   <- one argument read as two sources")
        print(f"  created by a segmentation split   : {from_seg}")
        print(f"  arrived as separate raw PDFs      : {len(pairs) - from_seg}")
        print()
        for pair in pairs:
            flag = " TYPES DIFFER" if pair["types_disagree"] else ""
            print(f"  {pair['case_id']:<10} "
                  f"{pair['parent']}(->p{pair['parent_last_page']}) + "
                  f"{pair['child']}(p{pair['child_first_page']}->)  "
                  f"{pair['parent_type']} | {pair['child_type']}{flag}")
        print()
        print(f"affected cases ({len(result['affected_cases'])}): "
              f"{', '.join(result['affected_cases'])}")

    if args.json:
        with io.open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        print(f"\nwrote {args.json}")
    # Exit 0 either way: this reports, it does not gate. A non-zero exit would
    # make it a check that a pipeline run has to pass, and merging documents is
    # a human decision on source material, not something to fail a stage over.
    return 0


if __name__ == "__main__":
    sys.exit(main())
