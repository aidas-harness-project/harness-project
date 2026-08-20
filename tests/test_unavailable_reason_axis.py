"""Why a field is blank must survive as a STRUCTURED value, not only as prose.

Measured on CASE_700 (2026-08-21): all 25 unavailable fields published
`unavailable_reason: not_mentioned`, while the free-text reason held three
different causes -- 21 "no source in the routing order mentioned it", 3 "the
산재 route never activated", 1 "an opportunistic field schedules no read of its
own". The three call for different actions, and only the first is closable by
requesting documents, so collapsing them sent a reviewer chasing records that
could not have helped.

The axis then stopped at the claim-analysis contract: `unconfirmed_section`
carried only `reason`, and `screening_report_selective.schema.json` typed
`unconfirmed_items` as a bare `{"type": "object"}` -- so the cause could go
unpublished with nothing failing. That is the recurring shape: an axis added
upstream that no downstream consumer learns about.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import run_screening_report as screening  # noqa: E402
from _validation import load_registry  # noqa: E402

CONFIG = json.loads(
    (ROOT / "config" / "claim_analysis"
     / "claim_analysis_routing_v0.1.json").read_text(encoding="utf-8"))


def _fact(field_id: str, unavailable_reason: str, reason: str = "사유") -> dict:
    return {
        "field_id": field_id,
        "resolution_status": "unavailable",
        "unavailable_reason": unavailable_reason,
        "resolution_reason": reason,
    }


def _searched_field_ids(limit: int = 6) -> list[str]:
    return [row["field_id"] for row in CONFIG["fields"]
            if row["extraction_wave"] in {"A", "B"}][:limit]


# --- the enum carries the distinction --------------------------------------

def test_the_schema_offers_a_value_for_a_route_that_never_opened():
    """`not_mentioned` asserts sources were read and were silent. A route that
    never activated read nothing, so it needs its own value -- otherwise the
    two are indistinguishable to any machine consumer."""
    schemas, _ = load_registry()
    result = schemas["claim_analysis_result.schema.json"]
    for definition in ("observation", "field_result"):
        enum = result["$defs"][definition]["properties"][
            "unavailable_reason"]["enum"]
        assert "route_not_activated" in enum, definition
        assert "not_scheduled" in enum, definition


def test_both_definitions_carry_the_same_enum():
    """`observation` and `field_result` must not drift apart -- one is the
    reading, the other the field's verdict over readings."""
    schemas, _ = load_registry()
    result = schemas["claim_analysis_result.schema.json"]
    observation = result["$defs"]["observation"]["properties"][
        "unavailable_reason"]["enum"]
    field_result = result["$defs"]["field_result"]["properties"][
        "unavailable_reason"]["enum"]
    assert set(observation) == set(field_result)


def test_every_enum_value_maps_to_an_action():
    """A cause with no `gap_kind` mapping would silently render as a records
    gap -- the exact conflation this change removes."""
    schemas, _ = load_registry()
    enum = schemas["claim_analysis_result.schema.json"]["$defs"][
        "field_result"]["properties"]["unavailable_reason"]["enum"]
    unmapped = [v for v in enum if v not in screening.UNAVAILABLE_KIND]
    assert not unmapped, f"no action mapping for: {unmapped}"
    unlabelled = [k for k in set(screening.UNAVAILABLE_KIND.values())
                  if k not in screening.UNAVAILABLE_KIND_LABEL]
    assert not unlabelled, f"no Korean label for: {unlabelled}"


# --- the writers use it ----------------------------------------------------

def test_a_dormant_conditional_route_is_not_reported_as_a_records_gap():
    import run_claim_analysis_selective as driver

    source = (ROOT / "tools" / "run_claim_analysis_selective.py").read_text(
        encoding="utf-8")
    marker = source.index("FILING_ROUTE_NOT_TRIGGERED_REASON")
    window = source[max(0, marker - 500):marker]
    assert 'unavailable_reason = "route_not_activated"' in window, (
        "the dormant-route branch must not publish not_mentioned: no source "
        "was consulted, so no document request can close it")
    assert driver.FILING_ROUTE_NOT_TRIGGERED_REASON


def test_an_opportunistic_field_is_not_reported_as_a_records_gap():
    source = (ROOT / "tools" / "run_claim_analysis_selective.py").read_text(
        encoding="utf-8")
    assert 'outcome.unavailable_reason = "not_scheduled"' in source, (
        "an opportunistic field buys no read of its own; saying no source "
        "mentioned it overstates what was looked at")


# --- the axis reaches the reader -------------------------------------------

def test_the_report_publishes_the_structured_cause():
    field_ids = _searched_field_ids(2)
    facts = {
        field_ids[0]: _fact(field_ids[0], "not_mentioned"),
        field_ids[1]: _fact(field_ids[1], "route_not_activated"),
    }
    rows = {row["field_id"]: row for row in
            screening.unconfirmed_section(facts, CONFIG)}
    assert rows[field_ids[0]]["gap_kind"] == "records_gap"
    assert rows[field_ids[1]]["gap_kind"] == "not_searched"
    assert rows[field_ids[1]]["unavailable_reason"] == "route_not_activated"


def test_a_result_without_the_axis_still_renders():
    """A pre-2026-08-21 claim_analysis_result carries no `unavailable_reason`;
    it must default to a records gap rather than crash or vanish."""
    field_id = _searched_field_ids(1)[0]
    facts = {field_id: {"field_id": field_id,
                        "resolution_status": "unavailable",
                        "resolution_reason": "예전 산출물"}}
    rows = screening.unconfirmed_section(facts, CONFIG)
    assert rows[0]["gap_kind"] == "records_gap"
    assert rows[0]["unavailable_reason"] is None


def test_section_seven_groups_by_what_the_reader_can_do():
    field_ids = _searched_field_ids(3)
    rows = [
        {"field_id": field_ids[0], "label": "사고일",
         "gap_kind": "records_gap", "reason": "기재 없음"},
        {"field_id": field_ids[1], "label": "산재 접수 근거",
         "gap_kind": "not_searched", "reason": "경로 미활성"},
        {"field_id": field_ids[2], "label": "산재 승인 상태",
         "gap_kind": "not_searched", "reason": "경로 미활성"},
    ]
    section = next(s for s in screening.markdown_sections(
        {"unconfirmed_items": rows}) if s["heading"].startswith("7."))
    content = section["content"]
    assert "**자료 미비** (1건)" in content
    assert "**탐색 미실행** (2건)" in content
    # A records gap must not sit under the not-searched heading.
    assert content.index("자료 미비") < content.index("탐색 미실행")


def test_a_single_cause_needs_no_grouping_headers():
    """Headings earn their place only when there is a distinction to draw."""
    field_ids = _searched_field_ids(2)
    rows = [{"field_id": f, "label": f, "gap_kind": "records_gap",
             "reason": "기재 없음"} for f in field_ids]
    section = next(s for s in screening.markdown_sections(
        {"unconfirmed_items": rows}) if s["heading"].startswith("7."))
    assert "**자료 미비**" not in section["content"]
    assert section["content"].count("\n- ") == 1


def test_no_unconfirmed_items_still_renders():
    section = next(s for s in screening.markdown_sections(
        {"unconfirmed_items": []}) if s["heading"].startswith("7."))
    assert section["content"] == "- 미확인 항목 없음"


# --- the contract pins the shape -------------------------------------------

def test_the_report_schema_requires_the_cause():
    """It was `{"type": "object"}`, which is why the axis could be dropped
    without any validation noticing."""
    schemas, _ = load_registry()
    items = schemas["screening_report_selective.schema.json"]
    found = None
    stack = [items]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if "unconfirmed_items" in node:
                found = node["unconfirmed_items"]
                break
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    assert found is not None, "unconfirmed_items is not in the schema"
    assert "gap_kind" in found["items"]["required"]
    assert set(found["items"]["properties"]["gap_kind"]["enum"]) == set(
        screening.UNAVAILABLE_KIND_LABEL)
