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
        field_ids[1]: _fact(field_ids[1], "not_scheduled"),
    }
    rows = {row["field_id"]: row for row in
            screening.unconfirmed_section(facts, CONFIG)}
    assert rows[field_ids[0]]["gap_kind"] == "records_gap"
    assert rows[field_ids[1]]["gap_kind"] == "not_searched"
    assert rows[field_ids[1]]["unavailable_reason"] == "not_scheduled"


def test_a_route_that_never_activated_is_not_an_outstanding_item():
    """`route_not_activated` is dropped from the reader's list (2026-08-21).

    The trigger never fired, so nothing was read and nothing is missing --
    listing it told a 손해사정사 to chase records the case gives no reason to
    believe exist. On CASE_702 the three 산재 fields printed on a case with no
    산재 element at all, and the same three lines would print on every
    non-산재 case in the corpus.

    The cause is still carried per field in `claim_analysis_result.json`; what
    changes is only whether the practitioner's briefing lists it as a gap. The
    OTHER `not_searched` value, `not_scheduled`, is still reported: an
    opportunistic field genuinely could have been answered by a document
    another field opened.
    """
    field_ids = _searched_field_ids(3)
    facts = {
        field_ids[0]: _fact(field_ids[0], "not_mentioned"),
        field_ids[1]: _fact(field_ids[1], "route_not_activated"),
        field_ids[2]: _fact(field_ids[2], "not_scheduled"),
    }
    reported = {row["field_id"] for row in
                screening.unconfirmed_section(facts, CONFIG)}
    assert field_ids[1] not in reported, (
        "a route that never activated is not a gap in THIS case's records")
    assert field_ids[0] in reported
    assert field_ids[2] in reported, (
        "not_scheduled is a different situation and must still be reported")


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


def test_stop_reason_enum_is_identical_in_result_and_trace_schemas() -> None:
    """One writer copies `outcome.stop_reason` into BOTH contracts, so a value
    one schema accepts and the other rejects fails the stage on its last write.

    Both schemas already say "Keep the two enums identical" in prose, and the
    drift still happened twice: `explicitly_absent` (added to the result schema
    2026-08-20, elected first by CASE_712) and `partial_value_only` (added
    2026-08-26, elected first by CASE_9410 -- which failed on the trace write
    with 5 rejected field_stops AFTER the whole run had been paid for). Prose
    could not enforce it; this can.
    """
    schemas, _ = load_registry()
    result_enum = schemas["claim_analysis_result.schema.json"]["$defs"][
        "field_result"]["properties"]["stop_reason"]["enum"]
    trace_enum = schemas["claim_analysis_trace.schema.json"]["properties"][
        "field_stops"]["items"]["properties"]["stop_reason"]["enum"]
    assert set(result_enum) == set(trace_enum), (
        "stop_reason drifted between the result and trace schemas: "
        f"result-only={sorted(set(result_enum) - set(trace_enum))}, "
        f"trace-only={sorted(set(trace_enum) - set(result_enum))}")


def test_unavailable_reason_enum_is_identical_in_observation_and_field_result() -> None:
    """The same drift, one level down: `observation` and `field_result` each
    declare `unavailable_reason`, and the driver writes the field's value into
    both. They had already diverged when `partial_reading_only` was added.
    """
    schemas, _ = load_registry()
    defs = schemas["claim_analysis_result.schema.json"]["$defs"]
    observation = defs["observation"]["properties"]["unavailable_reason"]["enum"]
    field_result = defs["field_result"]["properties"]["unavailable_reason"]["enum"]
    assert set(observation) == set(field_result), (
        "unavailable_reason drifted between observation and field_result: "
        f"observation-only={sorted(set(observation) - set(field_result))}, "
        f"field_result-only={sorted(set(field_result) - set(observation))}")


# --- a printed field the writer left blank ---------------------------------
# `not_mentioned`'s own schema text promises "sources WERE read and none
# discussed the field, which is a real records gap a reviewer may close by
# requesting documents". A form that PRINTS the field and leaves the cell
# empty breaks both halves: the document did raise the item, and no further
# records request will ever fill that cell. Sending a reviewer to request
# documents is the wrong instruction; the right one is to check the original.
#
# Measured on CASE_7061 DOC_003 (2026-08-26), a 맥브라이드 후유장애진단서:
#
#   | 기왕증 | |
#   | 기왕증의 현재 장애에 대한 기여율 | |
#   | 기존장애 및 장애율 | |
#   | 수상일 | | 초진일 | | 장해진단일 | | |
#
# Four printed rows, every cell blank, all published `not_mentioned`. The same
# defect produced the date axis reading as a records gap on all four cases of
# that run -- one cause, two symptoms.
#
# This is NOT the same as inferring a value from silence, which
# `patient_reported_prior_same_site_history`'s own note forbids ("never infer
# no history from silence"). A blank cell still yields NO value; what changes
# is only what the contract says about WHY, and therefore what it asks a
# person to do about it.

def test_the_schema_offers_a_value_for_a_printed_but_blank_field():
    schemas, _ = load_registry()
    enum = schemas["claim_analysis_result.schema.json"]["$defs"][
        "field_result"]["properties"]["unavailable_reason"]["enum"]
    assert "printed_but_blank" in enum, (
        "a form that prints a field and leaves it empty has no code, so it "
        "reports as a records gap a document request could close")


def test_a_printed_blank_is_not_a_records_gap():
    """The whole point of the value: it must not route to 자료 미비, whose
    action is to request more documents."""
    assert screening.UNAVAILABLE_KIND.get("printed_but_blank") == "source_blank"
    assert "source_blank" in screening.UNAVAILABLE_KIND_LABEL


def test_a_printed_blank_is_not_grouped_with_records_gaps(tmp_path):
    """End-to-end through the report writer, not just the mapping table: a
    blank cell and a genuinely undiscussed field must land in different
    groups, because the reader's next action differs."""
    ids = _searched_field_ids(2)
    facts = {
        ids[0]: _fact(ids[0], "not_mentioned"),
        ids[1]: _fact(ids[1], "printed_but_blank"),
    }
    rows = screening.unconfirmed_section(facts, CONFIG)
    kinds = {row["field_id"]: row.get("gap_kind") for row in rows}
    assert kinds[ids[0]] == "records_gap"
    assert kinds[ids[1]] == "source_blank"
