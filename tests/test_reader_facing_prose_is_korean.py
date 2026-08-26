"""Prose a 손해사정사 reads must be Korean.

CLAUDE.md: documentation, code and agent definitions are English; the actual
deliverables the pipeline produces -- the screening report and the draft report
-- are Korean, because they are submitted to Korean-speaking professionals.

Two `skip_reason` strings were not. They reached section 7 of the screening
report verbatim and printed beside Korean ones:

    - 피해자 과실비율: the field is not routed to any document source
    - 적용 법조: the field is not routed to any document source
    - 회복 지연 기재: 라우팅 우선순위상의 어떤 출처도 이 항목을 기재하지 않았습니다

Measured across the CASE_7* corpus (2026-08-26): 82 rows carried the first
string and 8 the second -- 90 of 728 section-7 rows, concentrated on the six
배상책임 fields plus 과실비율.

The reason they were easy to miss is that `skip_reason` reads like an internal
diagnostic. It is not: `_extract_field` and the wave loop both assign it
straight to `outcome.reason`, which the DAO publishes as `resolution_reason`
and the screening renderer prints as the row's text. There is no translation
layer anywhere between the constant and the page.

These tests check the strings this path can produce, not every string in the
tools -- an English log line or exception message is correct and must stay.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import claim_analysis_selection as selection

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads(
    (ROOT / "config" / "claim_analysis" / "claim_analysis_routing_v0.1.json")
    .read_text(encoding="utf-8"))

HANGUL = re.compile(r"[가-힣]")
# A run of ASCII letters long enough to be a sentence rather than a code, an
# identifier, or a unit. `DOC_002`, `M4856` and `%` must all stay legal.
ENGLISH_SENTENCE = re.compile(r"[A-Za-z]{3,}(\s+[A-Za-z]{2,}){2,}")

# The two strings this change replaced. Contracts written before 2026-08-26
# still hold them: `resolution_reason` is baked into
# `claim_analysis_result.json` by STAGE 5, so a stage-7 rerun cannot clear it.
RETIRED_ENGLISH_REASONS = {
    "the field is not routed to any document source",
    "the case holds no document of any kind this field routes to",
}


def _skip_reasons() -> list[tuple[str, str]]:
    """Every `skip_reason` the config can produce, with the field that made it.

    Planned against an EMPTY document list on purpose: a skip reason is by
    definition what a field says when the pack gave it nothing to read, so this
    is the state that produces all of them.
    """
    reasons = []
    for field_row in CONFIG.get("fields") or []:
        if field_row.get("extraction_wave") == "deferred":
            continue
        plan = selection.plan_field(field_row, CONFIG, [])
        if plan.skip_reason:
            reasons.append((field_row["field_id"], plan.skip_reason))
    return reasons


def test_the_config_produces_skip_reasons_at_all() -> None:
    """Guards the premise: if planning stops producing them, every check below
    passes while checking nothing."""
    assert _skip_reasons(), "no field produced a skip reason; the premise broke"


@pytest.mark.parametrize("field_id,reason", _skip_reasons())
def test_a_skip_reason_is_korean(field_id: str, reason: str) -> None:
    """`skip_reason` is published prose, not a diagnostic.

    `_extract_field` assigns it to `outcome.reason` -> `resolution_reason` ->
    the section 7 row a 손해사정사 reads. Nothing translates it on the way.
    """
    assert HANGUL.search(reason), (
        f"{field_id} publishes an English reason to the report: {reason!r}")


@pytest.mark.parametrize("field_id,reason", _skip_reasons())
def test_a_skip_reason_carries_no_english_sentence(field_id: str,
                                                   reason: str) -> None:
    """A Korean sentence with an English clause bolted on is the same defect
    half-fixed. Document ids and codes are not sentences and stay legal."""
    found = ENGLISH_SENTENCE.search(reason)
    assert not found, (
        f"{field_id} mixes an English sentence into Korean prose: "
        f"{found.group(0)!r} in {reason!r}")


def test_the_liability_reason_names_the_documents_not_the_routing() -> None:
    """The replacement says something different, not just the same thing in
    Korean.

    "the field is not routed to any document source" was true of the MEDICAL
    ladder and silent about what actually governs these fields: stage 3-a reads
    them from the `legal_opinion`/`insurer_response` documents that
    `additional_fields_by_case_type` names. A reader's question is which
    document would have held the answer, so the reason names those.
    """
    row = next(f for f in CONFIG["fields"]
               if f["field_id"] == "comparative_negligence_rate")
    reason = selection.plan_field(row, CONFIG, []).skip_reason
    assert "법률의견서" in reason and "보험사" in reason, reason
    assert "라우트" not in reason and "route" not in reason.lower(), (
        f"a route is an internal notion; the reader needs the document: "
        f"{reason!r}")


def test_the_shipped_reports_carry_no_english_sentences() -> None:
    """The property over what actually reached disk.

    Scoped to the fields a reviewer reads as prose. `field_id` and
    `document_id` are identifiers and are deliberately not checked.

    Contracts still carrying one of the two RETIRED strings are counted and
    reported, not asserted on: the 24 corpus cases need a stage-5 rerun to
    clear them, which is a corpus refresh rather than part of the fix, and
    failing here would leave the test red for a reason no code change can
    address. Any OTHER English sentence is a new defect and fails.
    """
    reports = sorted((ROOT / "outputs").glob("CASE_7*/screening_report.json"))
    if not reports:
        pytest.skip("no CASE_7* corpus in this checkout")

    offenders: list[str] = []
    stale = 0
    for path in reports:
        data = json.loads(path.read_text(encoding="utf-8"))
        for row in data.get("unconfirmed_items") or []:
            text = (row.get("reason") or "").strip()
            # A quote is verbatim source text and may legitimately be English
            # (`Fx. distal radius, wrist, Rt.`); only the pipeline's own
            # sentence is checked.
            if not ENGLISH_SENTENCE.search(text) or HANGUL.search(text):
                continue
            if text in RETIRED_ENGLISH_REASONS:
                stale += 1
                continue
            offenders.append(
                f"{path.parent.name}/{row['field_id']}: {text[:60]}")

    detail = "\n  ".join(sorted(set(offenders))[:15])
    assert not offenders, (
        f"{len(offenders)} section 7 rows publish an English sentence:"
        f"\n  {detail}")
    if stale:
        print(f"\n{stale} rows still carry a retired English reason; "
              f"rerunning stage 5 on those cases clears them.")
