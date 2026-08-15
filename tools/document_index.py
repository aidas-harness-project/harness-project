"""Derived navigation index for a case's policy documents.

Why this exists: `claim_analysis` on CASE_142 ran 678.4s with
`unattributed_active_s` equal to the whole stage -- instrumented tool time
across the entire run was about 5s over 221 spans, so essentially all of it is
an agent reading chunks and deciding. Two measured symptoms:

  - 26 of that run's 29 `search-document-text` calls fired in one stage window
    and 13 of them exited 0-hit. Half the searches missed.
  - A born-digital policy's tables lose their structure on the way to the
    analysis stages. `pymupdf` sees DOC_010 p13 as `기 간 | 지 급 이 자` over
    four data rows; embedded-text extraction flattens it to a run of lines
    (`기 간` / `지 급 이 자` / `지급기일의 다음 날부터 30일 이내 기간` /
    `보험계약대출이율` / ...), and which value belongs to which period is then
    positional guesswork. Scans are ironically better served -- vision OCR
    reproduces a 진료비 세부산정내역 as a pipe table -- so the loss is
    specific to the born-digital path.

Both are the same problem: the page states something plainly and the agent has
to hunt for it. This module states it once, deterministically, with no model
call.

DERIVED, NOT A CONTRACT. Regenerable from processed text plus the registered
PDF, carries no obligation, and its absence never blocks a stage. That is a
deliberate reaction to what clause normalization got wrong (retired
2026-08-15): it made a gate out of an artifact nothing could produce, and the
gate was then bypassed rather than satisfied. An index nobody is forced to
build cannot fail that way.

CANDIDATES, NOT VERDICTS -- the same contract `search-document-text` carries.
The index says "제38조 is on page 38". Whether that clause governs this
accident stays the agent's judgement, and is not a question a regex can be
allowed to appear to answer.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import policy_completeness

ROOT = Path(__file__).resolve().parent.parent
INDEX_VERSION = "document_index.v0.1"

# A heading occupies its whole line. The loose form (no `$`) also matches a
# wrapped body sentence that happens to begin a line with a cross-reference --
# `제3조(보상하는 손해)에 의한 손해에서 다음의 금액을 뺍니다.` -- which is a
# reference TO a clause, not the clause itself. Measured on CASE_142: strict
# anchoring drops 3 of DOC_010's 325 matches and 0 of DOC_009's 242, so the
# precision is nearly free. Mid-line occurrences (152 in DOC_010) are excluded
# by the leading anchor and were never candidates.
_ARTICLE_HEADING = re.compile(r"^\s*제\s*(\d+)\s*조\s*\(([^)]{1,40})\)\s*$")

# The publisher's own document title. Same signal segmentation cuts on, reused
# here to say WHICH 약관 a clause belongs to -- Korean policy bundles print
# dozens of 약관 in one PDF and `제1조` means nothing without its owner.
_POLICY_TITLE = re.compile(
    r"^\s*(.{2,40}?(?:보통약관|특별약관|추가특별약관|재추가특별약관|특약))\s*$")

# Tables that are tables only typographically. Measured on CASE_142's 55
# detected candidates: 24 are single-row and 44 are two-column, and the bulk
# are term-definition boxes (`치료비 | 치료비라 함은 응급처치...`) or blank
# intake forms (`시설명세 | 명칭: 용도: 구조:`). Seven have >=4 rows and
# exactly one is substantive. Filtering on shape plus filled-cell density keeps
# the real one without hand-listing titles.
_MIN_TABLE_ROWS = 3
_MIN_FILLED_RATIO = 0.5


def _page_files(case_id: str, doc_id: str) -> list[tuple[int, Path]]:
    """Processed page files as (page_number, path), ascending.

    Reads the per-page `page_NNN.md` layout rather than the redacted bundle,
    because a page number is the index's join key and parsing it back out of
    `<<<PAGE page=N>>>` markers would reintroduce exactly the in-band-marker
    fragility that broke `chunk_text` (2026-07-22).
    """
    base = ROOT / "data" / "processed" / case_id / doc_id
    pages = []
    for path in sorted(base.glob("page_*.md")):
        try:
            pages.append((int(path.stem.split("_")[1]), path))
        except (IndexError, ValueError):
            continue
    return sorted(pages)


def extract_clauses(case_id: str, doc_id: str) -> list[dict[str, Any]]:
    """Article headings in one document, each tagged with its owning 약관.

    The owner is carried forward from the most recent title line, which is how
    the document itself reads: a title introduces a run of articles and the
    articles restart at 제1조 under the next title. A clause found before any
    title gets `policy_name: None` rather than a guess -- front matter and
    tables of contents legitimately precede the first 약관.
    """
    clauses: list[dict[str, Any]] = []
    current_policy: str | None = None
    for page, path in _page_files(case_id, doc_id):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            title = _POLICY_TITLE.match(line)
            if title:
                current_policy = title.group(1).strip()
                continue
            heading = _ARTICLE_HEADING.match(line)
            if heading:
                clauses.append({
                    "page": page,
                    "policy_name": current_policy,
                    "article": f"제{heading.group(1)}조",
                    "heading": heading.group(2).strip(),
                })
    return clauses


def _is_substantive(rows: list[list[str | None]]) -> bool:
    """Whether an extracted table carries data rather than layout.

    Two signals, both structural. A table under `_MIN_TABLE_ROWS` is a
    definition box or a single form field. Above it, a blank intake form is
    distinguished from a data table by what its body cells contain.

    The density is measured over LINES inside cells, not over cells. A blank
    form packs its prompts into few cells:

        구 분     | 내 용
        시설명세  | 상호(성명):\\n구조:\\n용도\\n면적:\\n권리관계...
        업무내용  | 시설 내의 업무:\\n시설 밖의 업무:

    Counting cells scores that 1-of-2 filled -- the label column passes and
    the prompt bundle is a single failing unit -- which clears a 50%
    threshold and admits the form. Counting lines scores it near zero, which
    is what it is: two labels against eleven unanswered prompts. Measured on
    CASE_142's 55 candidates, per-cell counting kept 17 and per-line keeps 3,
    with DOC_010 p13's 지연이자율표 -- the one substantive table in the set --
    retained either way.
    """
    if len(rows) < _MIN_TABLE_ROWS:
        return False
    lines = [
        line.strip()
        for row in rows[1:] for cell in row
        for line in (cell or "").splitlines()
        if line.strip()
    ]
    if not lines:
        return False
    # A value-bearing line states something; a prompt ends at its colon or
    # carries nothing after it. `보험계약대출이율+가산이율(4.0%)` is a value,
    # `상호(성명):` is a prompt, and `용도` alone is a bare field label.
    filled = sum(1 for line in lines
                 if ":" not in line or line.split(":", 1)[1].strip())
    return filled / len(lines) >= _MIN_FILLED_RATIO


def extract_tables(pdf_path: Path) -> list[dict[str, Any]]:
    """Row/column structure recovered from a born-digital PDF's own layout.

    This is the only path that recovers it. `pymupdf.find_tables` reads the
    text layer's word coordinates and ruling lines, so it returns nothing on a
    scan (verified: CASE_142 DOC_005 and DOC_008, 0 embedded characters, 0
    tables) -- there is no geometry to read on an image-only page. A scan is
    not thereby unserved: its vision OCR already emits a pipe table.

    Returns [] rather than raising on an unreadable PDF. The index is
    advisory; a missing table entry costs an agent the structure it would have
    had, while an exception here would take down a stage that has no stake in
    the outcome.
    """
    try:
        import fitz
    except ImportError:
        return []
    tables: list[dict[str, Any]] = []
    try:
        with fitz.open(pdf_path) as pdf:
            for page_number, page in enumerate(pdf, start=1):
                for found in page.find_tables().tables:
                    try:
                        rows = found.extract()
                    except Exception:  # noqa: BLE001 -- one bad table, not the doc
                        continue
                    if not rows or not _is_substantive(rows):
                        continue
                    tables.append({
                        "page": page_number,
                        "rows": found.row_count,
                        "cols": found.col_count,
                        "header": [(c or "").strip() for c in rows[0]],
                        "cells": [[(c or "").strip() for c in row]
                                  for row in rows[1:]],
                    })
    except Exception:  # noqa: BLE001 -- unreadable PDF is a skip, not a failure
        return []
    return tables


def build_index(case_id: str, manifest: dict, *,
                raw_pdf_for) -> dict[str, Any]:
    """The whole case's policy navigation index.

    `raw_pdf_for(doc_id) -> Path | None` is injected rather than resolved here
    so the DAO supplies its own path-safety-checked resolver and tests can
    pass a stub. A document with no reachable PDF still gets its clauses --
    those come from processed text -- and simply carries no tables.
    """
    documents = []
    for entry in manifest.get("documents", []):
        if entry.get("document_type") != "insurance_policy":
            continue
        if entry.get("downstream_disposition") not in (
                policy_completeness._TEXT_PROCESSED):
            continue
        doc_id = entry.get("document_id")
        if not doc_id:
            continue
        pdf_path = raw_pdf_for(doc_id)
        documents.append({
            "document_id": doc_id,
            "extraction_method": entry.get("extraction_method"),
            "clauses": extract_clauses(case_id, doc_id),
            "tables": (extract_tables(pdf_path)
                       if pdf_path and Path(pdf_path).exists() else []),
        })
    return {
        "index_version": INDEX_VERSION,
        "case_id": case_id,
        "documents": documents,
    }
