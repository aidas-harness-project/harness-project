"""known-gaps.md's reference integrity (findings 2026-08-04 §6).

Items are cited by NUMBER from CLAUDE.md's changelog, open-decisions.md and
each other, so a duplicate number makes a citation ambiguous and an unnumbered
item uncitable. Two collisions (20, 21) and one unnumbered item had already
accumulated before anyone noticed -- and nobody would have, since nothing
checked. A one-time cleanup would just start the same drift again, so the
invariants are enforced here instead.

The status token matters for the same reason: findings §6-2 observed that
three headers were tool-addition records with no status at all, so "what is
actually open" could not be answered without reading all 43 sections. And a
header can go stale against its own body -- items 38/39/40 said OPEN in the
header while their bodies said FIXED, because the fix updated the body only.
"""
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
KNOWN_GAPS = ROOT / "known-gaps.md"

HEADER_RE = re.compile(r"^##\s*(?P<num>\d+)?\.?\s*(?P<title>.*)$")
STATUS_RE = re.compile(r"\b(OPEN|PARTIAL|RESOLVED|FIXED|CLOSED|RISK ACCEPTED|WONTFIX)\b")


def _headers():
    """Every '## ' heading as (line_no, number_or_None, title)."""
    out = []
    for line_no, line in enumerate(KNOWN_GAPS.read_text(encoding="utf-8").split("\n"), 1):
        if not line.startswith("## "):
            continue
        m = HEADER_RE.match(line)
        num = m.group("num")
        out.append((line_no, int(num) if num else None, m.group("title")))
    return out


def test_every_item_is_numbered():
    """An unnumbered item cannot be cited, so it silently drops out of every
    cross-reference."""
    unnumbered = [(ln, t) for ln, n, t in _headers() if n is None]
    assert unnumbered == [], f"unnumbered known-gaps items: {unnumbered}"


def test_item_numbers_are_unique():
    """A duplicate number makes 'known-gaps.md item N' ambiguous. Both real
    collisions were resolved by renumbering the item nothing cited -- the
    referenced one keeps its number."""
    seen, dupes = {}, []
    for line_no, num, title in _headers():
        if num in seen:
            dupes.append((num, seen[num], line_no))
        else:
            seen[num] = line_no
    assert dupes == [], f"duplicate item numbers (num, first_line, second_line): {dupes}"


def test_every_item_header_declares_a_status():
    """Findings §6-2: without a status token in the header, answering 'what is
    still open' means reading every section."""
    missing = [(ln, n, t[:70]) for ln, n, t in _headers() if not STATUS_RE.search(t)]
    assert missing == [], f"known-gaps items with no status token in the header: {missing}"


def test_header_status_is_not_stale_against_its_own_body():
    """A header saying OPEN above a body saying FIXED is worse than no status:
    it is confidently wrong. Real instance -- items 38/39/40 were fixed
    2026-08-04 and their headers were not updated in the same pass.

    Narrow on purpose: only flags a header whose ONLY status is open-ish while
    its body announces a resolution in bold ('**FIXED ...**' / '**RESOLVED
    ...**'). A body that merely mentions the word in prose does not trip it.
    """
    lines = KNOWN_GAPS.read_text(encoding="utf-8").split("\n")
    starts = [(i, line) for i, line in enumerate(lines) if line.startswith("## ")]
    resolution_re = re.compile(r"\*\*(FIXED|RESOLVED)\b")

    stale = []
    for idx, (start, header) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        header_tokens = set(STATUS_RE.findall(header))
        if header_tokens - {"OPEN"}:
            continue  # header already claims some form of resolution/partial
        if not header_tokens:
            continue  # covered by the status-token test above
        body = "\n".join(lines[start + 1:end])
        if resolution_re.search(body):
            stale.append((start + 1, header[:70]))
    assert stale == [], f"headers marked OPEN whose body announces a fix: {stale}"


def test_open_index_table_matches_the_headers():
    """The summary table at the top exists so 'what is still open' is
    answerable without reading 43 sections -- which only holds if it agrees
    with the headers. A hand-maintained index is exactly the thing that rots,
    so it is checked rather than trusted.

    Scoped to the table between '### Still open' and the next heading, so
    unrelated tables elsewhere in the file are not mistaken for the index.
    """
    text = KNOWN_GAPS.read_text(encoding="utf-8")
    start = text.index("### Still open")
    end = text.index("\n## ", start)
    listed = {int(n) for n in re.findall(r"^\|\s*(\d+)\s*\|", text[start:end], re.M)}

    open_tokens = {"OPEN", "PARTIAL", "RISK ACCEPTED"}
    actually_open = {n for _, n, t in _headers()
                     if n is not None and set(STATUS_RE.findall(t)) & open_tokens}

    assert listed == actually_open, (
        f"index/header mismatch -- listed but not open: {sorted(listed - actually_open)}; "
        f"open but missing from the index: {sorted(actually_open - listed)}")


def test_referenced_item_numbers_resolve():
    """'known-gaps.md item N' in any tracked doc must name an item that exists.
    A citation to a number nobody assigned is a dangling reference."""
    numbers = {n for _, n, _ in _headers() if n is not None}
    ref_re = re.compile(r"known-gaps(?:\.md)?[^\n]{0,20}?items?\s+(\d+)", re.I)
    dangling = []
    for doc in ("CLAUDE.md", "open-decisions.md", "pipeline.md", "known-gaps.md"):
        path = ROOT / doc
        if not path.exists():
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
            for num in ref_re.findall(line):
                if int(num) not in numbers:
                    dangling.append((doc, line_no, int(num)))
    assert dangling == [], f"references to nonexistent known-gaps items: {dangling}"
