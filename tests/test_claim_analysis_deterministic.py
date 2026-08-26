"""The label-anchored rules, against the two real pages that motivated them.

CASE_705/DOC_013 and CASE_713/DOC_012 are the SAME source document processed
twice. The model read 13 / 영구 / 맥브라이드식 장애평가 off the first and
returned `unavailable` for all three off the second. The fixtures below are the
two redacted page texts verbatim (via `dao.py read-redacted-text-bundle`), and
the point of the first test is that one rule set produces one answer from both
-- which is the property the model call did not have.

The texts differ only in markdown pipe framing: CASE_713's table rows are
wrapped in leading/trailing `|`. That is precisely the kind of incidental
difference a rule must not notice, so it is load-bearing that both fixtures
stay byte-exact rather than being normalised into one.
"""
from __future__ import annotations

import re

import pytest

import claim_analysis_deterministic as deterministic


# --- the 맥브라이드 disability page, as processed on each case ------------

PAGE_705 = """후유장애 진단서(Mc Bride)                                    [원본대조필 인]

병록번호 :
연 번 호 :                    주민번호 :

성명 |          | 생년월일 |          | 성별 |          | 연령 | 만    세
주소 |                              | 전화 |
수상일 |          | 초진일 |          | 장해진단일 |

상병명 | S6280 : 손목 및 손의 기타 및 상세불명 부분의 골절, 폐쇄성
전병력 | 기왕증 |
        | 기왕증의 현재 장애에 대한 기여율 |
        | 기존장애 및 장애율 |
        | 기왕증과 교통사고 상당 인과관계 유/무 | 무

[주요치료 내용 및 경과]
관혈적 정복술 및 내고정술 시행(                    )
타원에서 포괄적 재활 및 보존적 치료 시행.

[각종검사 소견] (장애내용과 관련있는 소견만 기재)
상기 환자 상기 일, 환자 계단에서 넘어지며 수상(환자진술)
신체검사, 신경학적검사, 단순방사선검사,자기공명영상검사

[후유장애 내용]
우측 수관절 운동 부전강직 잔존
ROM(24/6/28)
Wrist(AMA)
 - Flex(30/70), Ext(30/60)
 - R.Dev(10/20), U.Dev(15/30)

[맥브라이드식 장애평가]
장해부위:우측 수관
장해평가 해당항목:III-A-2
노동능력상실율(%):13%
맥브라이드 장해평가법 상, 수관절 부전강직 III-A-2 준용한 13%의 노동능력상실률에 해당함.

노동능력 상실율(%) | 13 | 비고사항 (영구/한시) | 영구
"""

PAGE_713 = """후유장애 진단서(Mc Bride)

원본대조필 인

병록번호 :
연 번 호 :                    주민번호 :

| 성명 |  | 생년월일 |  | 성별 |  | 연령 | 만    세 |
| 주소 |  | 전화 |  |
| 수상일 |  | 초진일 |  | 장해진단일 |  |

| 상병명 | S6280 : 손목 및 손의 기타 및 상세불명 부분의 골절, 폐쇄성 | 전병력 | 기왕증 |  |
|  |  |  | 기왕증의 현재 장애에 대한 기여율 |  |
|  |  |  | 기존장애 및 장애율 |  |
|  |  |  | 기왕증과 교통사고 상당 인과관계 유/무 | 무 |

[주요치료 내용 및 경과]
관혈적 정복술 및 내고정술 시행(                    )
타원에서 포괄적 재활 및 보존적 치료 시행.

[각종검사 소견] (장애내용과 관련있는 소견만 기재)
상기 환자 상기 일, 환자 계단에서 넘어지며 수상(환자진술)
신체검사, 신경학적검사, 단순방사선검사,자기공명영상검사

[후유장애 내용]
우측 수관절 운동 부전강직 잔존
ROM(24/6/28)
Wrist(AMA)
 - Flex(30/70), Ext(30/60)
 - R.Dev(10/20), U.Dev(15/30)

[맥브라이드식 장애평가]
장해부위:우측 수관
장해평가 해당항목:III-A-2
노동능력상실율(%):13%
맥브라이드 장해평가법 상, 수관절 부전강직 III-A-2 준용한 13%의 노동능력상실률에 해당함.

| 노동능력 상실율(%) | 13 | 비고사항 (영구/한시) | 영구 |
"""

DISABILITY_FIELDS = [
    {"field_id": "documented_disability_rate", "value_shape": "number"},
    {"field_id": "documented_disability_duration", "value_shape": "text"},
    {"field_id": "documented_disability_standard", "value_shape": "text"},
]


def _pages(text: str):
    return [{"page": 1, "text": text}]


# ------------------------------------------------------ the regression --

@pytest.mark.parametrize("text", [PAGE_705, PAGE_713],
                         ids=["case_705", "case_713"])
def test_same_source_yields_same_values(text):
    """The observed regression, as an assertion.

    The model produced these three values from PAGE_705 and none from
    PAGE_713. Both fixtures must now yield the identical triple.
    """
    found = deterministic.extract(_pages(text), DISABILITY_FIELDS)

    assert found["documented_disability_rate"]["value"] == 13
    assert found["documented_disability_duration"]["value"] == "영구"
    assert found["documented_disability_standard"]["value"] == "맥브라이드식 장애평가"


@pytest.mark.parametrize("text", [PAGE_705, PAGE_713],
                         ids=["case_705", "case_713"])
def test_every_quote_is_verbatim_and_unique(text):
    """Each cited quote must survive the driver's `locate_exact` gate.

    That gate accepts a quote occurring EXACTLY once. A rule citing a fragment
    that repeats on the page would be dropped downstream, so the uniqueness is
    asserted here rather than discovered on a live run.
    """
    found = deterministic.extract(_pages(text), DISABILITY_FIELDS)
    assert found, "no rule fired"

    for field_id, reading in found.items():
        quote = reading["quote"]
        assert quote in text, f"{field_id}: quote is not verbatim"
        assert text.count(quote) == 1, f"{field_id}: quote is ambiguous"


def test_quote_carries_the_label_not_just_the_number():
    """`13` alone is not a citation a human can check.

    The rate's quote must show the printed label that gives the digit meaning;
    otherwise the evidence reference proves only that the page contains a 13.
    """
    found = deterministic.extract(_pages(PAGE_705), DISABILITY_FIELDS)
    quote = found["documented_disability_rate"]["quote"]

    assert "노동능력" in quote and "13" in quote


# ------------------------------------------------------ what must NOT fire --

def test_duration_is_read_after_the_label_never_inside_it():
    """The label itself names both options, so it cannot be the value.

    A form printing `비고사항 (영구/한시)` with the value cell left EMPTY states
    no duration. A pattern that matched the bare word 영구 would report
    "영구" here -- a value the document never gave.
    """
    blank = PAGE_705.replace(
        "비고사항 (영구/한시) | 영구", "비고사항 (영구/한시) |")

    found = deterministic.extract(_pages(blank), DISABILITY_FIELDS)

    assert "documented_disability_duration" not in found


def test_conflicting_printed_rates_defer_to_the_provider():
    """Two DIFFERENT printed rates is a conflict, not a doubly-confirmed read.

    Taking the first would silently resolve a disagreement that belongs to the
    model and the consistency stage.
    """
    conflicted = PAGE_705.replace(
        "노동능력 상실율(%) | 13 |", "노동능력 상실율(%) | 27 |")

    found = deterministic.extract(_pages(conflicted), DISABILITY_FIELDS)

    assert "documented_disability_rate" not in found
    # The unrelated rules still fire -- one conflict does not blind the rest.
    assert found["documented_disability_standard"]["value"] == "맥브라이드식 장애평가"


def test_repeated_identical_rate_is_not_a_conflict():
    """The same rate printed twice is the ordinary form, not a disagreement."""
    found = deterministic.extract(_pages(PAGE_705), DISABILITY_FIELDS)

    # PAGE_705 prints 13 in both the colon line and the table row.
    assert found["documented_disability_rate"]["value"] == 13


def test_a_page_without_the_form_settles_nothing():
    """Silence from a rule is silence, never a `not_mentioned` verdict.

    The field must fall through to the provider, so the result carries no key
    at all -- an empty dict, not a dict of negatives.
    """
    found = deterministic.extract(
        _pages("영상판독지\n판독소견: 골절 소견 없음\n"), DISABILITY_FIELDS)

    assert found == {}


def test_unrequested_fields_are_never_returned():
    """A rule fires only for a field this document was asked for."""
    found = deterministic.extract(
        _pages(PAGE_705),
        [{"field_id": "documented_disability_rate", "value_shape": "number"}])

    assert set(found) == {"documented_disability_rate"}


# ------------------------------------------------------ shape contract --

def test_reading_shape_matches_the_provider_parser():
    """A deterministic reading must be indistinguishable in shape.

    The driver consumes these through the same path as a model reading, so a
    missing key would fail only at run time, on a real case.
    """
    found = deterministic.extract(_pages(PAGE_705), DISABILITY_FIELDS)
    reading = found["documented_disability_rate"]

    assert reading["presence"] == "asserted"
    assert reading["complete"] is True
    assert reading["unambiguous"] is True
    assert isinstance(reading["page"], int)
    assert reading["extraction_method"] == "deterministic_label_anchor"


def test_ocr_split_label_still_matches():
    """OCR of a form splits `노동능력 상실율` across a cell boundary.

    Both spacings occur in the two real fixtures, so the tolerance is not
    hypothetical.
    """
    spaced = PAGE_705.replace("노동능력상실율(%):13%", "노동 능력 상실률 ( % ) : 13%")

    found = deterministic.extract(_pages(spaced), DISABILITY_FIELDS)

    assert found["documented_disability_rate"]["value"] == 13


# ------------------------------------- 수술명: withdrawn, deliberately --------
# `_surgery_name` was removed from RULES on 2026-08-26. It is the one rule that
# never settled a field: across every case that published one, all 5 rule
# readings resolved to `conflict` -- 5 of 5 -- while the other seven rules
# mostly assert (diagnostic_certainty 5/5 asserted, disability_type 2/2).
#
# The cause is that the label does not introduce a single-line value. Measured
# across the processed corpus: of 44 `수술명`-then-value occurrences, only 4
# (9%) have one value line; 38 (86%) have two or more. `_SURGERY_NAME_NEXTLINE`
# captured the first and dropped the rest, so the rule published a partial
# value that then contradicted a fuller reading from another document:
#
#   수술명
#   Open reduction of fracture with internal fixation      <- rule took this
#   ㄴ 리스프랑 족근골부위 핀고정수술, 4중족골, ...          <- model read this
#
# `ㄴ` is a continuation marker: the second line details the SAME operation.
# The pipeline reported the two as contradicting when they agree (CASE_7044,
# reproduced on CASE_7015/7046/9415/9418).
#
# Not fixed by widening the capture, which is why this is a withdrawal rather
# than a repair. The extra lines are not uniformly surgery names -- the same
# survey found `Fracture of greater tuberosity of humerus, closed` (a
# diagnosis, 16x) and bare form labels (`입원사유`, `어깨 통증`, `수술 전
# 진단명`, 8x each) directly below the label. Capturing them all would
# manufacture a surgery name out of a diagnosis, which is worse than reading
# one line. Deciding line by line what a line MEANS is not what a label anchor
# does -- it is the judgement `rule-conversion-criteria` says a rule must back
# away from.

def test_the_surgery_name_rule_is_withdrawn() -> None:
    assert "surgery_or_procedure_name" not in deterministic.RULES, (
        "the 수술명 label does not introduce a single-line value: 86% of "
        "corpus occurrences carry two or more, and every reading this rule "
        "published resolved to a conflict against a fuller model reading")


def test_a_multi_line_surgery_field_is_left_to_the_model() -> None:
    """The real CASE_7044 shape: the rules must return nothing for it, so the
    field reaches the prompt whole instead of arriving pre-settled and wrong."""
    pages = [{"page": 1, "text": (
        "수술명\n"
        "Open reduction of fracture with internal fixation\n"
        "ㄴ 리스프랑 족근골부위 핀고정수술, 4중족골, 입방골, 주상골 관혈적 정복 및 내고정수술 시행\n"
        "\n수술 전 진단명\n")}]
    settled = deterministic.extract(
        pages, [{"field_id": "surgery_or_procedure_name"}])
    assert "surgery_or_procedure_name" not in settled
