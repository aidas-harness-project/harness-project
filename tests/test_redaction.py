import pytest

import redaction as R
from redaction import (
    apply_redaction_spans,
    is_built_from_source,
    parse_pii_items,
    scan_residual_pii,
    RedactionParseError,
)


# ---------------------------------------------------------------------------
# S1: apply_redaction_spans -- deterministic substitution
# ---------------------------------------------------------------------------
def test_apply_spans_replaces_from_source_preserving_non_pii():
    src = "피보험자 홍길동 연락처 010-1234-5678 진단 골절 지급 12,340,000원"
    items = [
        {"text": "홍길동", "category": "person_name"},
        {"text": "010-1234-5678", "category": "phone_number"},
    ]
    app = apply_redaction_spans(src, items)
    assert app.redacted_text == "피보험자 [PERSON_NAME] 연락처 [PHONE_NUMBER] 진단 골절 지급 12,340,000원"
    assert app.items_redacted == 2
    assert app.categories == ["person_name", "phone_number"]
    assert app.unmatched_spans == []
    assert app.ambiguous_spans == []
    # non-PII (amount, diagnosis) preserved verbatim by construction
    assert "12,340,000원" in app.redacted_text and "골절" in app.redacted_text


def test_apply_spans_replaces_all_occurrences():
    src = "홍길동 서명, 홍길동 확인, 홍길동 날인"
    app = apply_redaction_spans(src, [{"text": "홍길동", "category": "person_name"}])
    assert app.redacted_text.count("[PERSON_NAME]") == 3
    assert app.items_redacted == 3


def test_apply_spans_longest_first_avoids_nested_overredaction():
    # A name that is also a substring of an address the model listed separately.
    src = "서울시 강남구 홍길동로 5, 환자 홍길동"
    items = [
        {"text": "홍길동", "category": "person_name"},
        {"text": "서울시 강남구 홍길동로 5", "category": "address"},
    ]
    app = apply_redaction_spans(src, items)
    # address replaced whole (longest first); only the standalone name remains to redact
    assert app.redacted_text == "[ADDRESS], 환자 [PERSON_NAME]"


def test_apply_spans_unmatched_is_reported_not_dropped():
    src = "환자 홍길동"
    # model reformatted the name (space inserted) -> not present verbatim
    app = apply_redaction_spans(src, [{"text": "홍 길동", "category": "person_name"}])
    assert app.redacted_text == src  # nothing changed
    assert app.unmatched_spans == [{"text": "홍 길동", "category": "person_name"}]


def test_apply_spans_flags_too_short_span_without_redacting():
    src = "이 사람은 이번 사고에서 이 씨를 만났다"
    app = apply_redaction_spans(src, [{"text": "이", "category": "person_name"}])
    assert "[PERSON_NAME]" not in app.redacted_text  # not blindly replaced
    assert app.ambiguous_spans and "too short" in app.ambiguous_spans[0]["reason"]


def test_apply_spans_flags_implausible_occurrence_count():
    # A multi-char token occurring far more than a real PII value would: the
    # over-redaction guard leaves it un-redacted and flags it for review.
    src = "환자분 " * 25  # 25 > _HIGH_OCCURRENCE_FLAG
    app = apply_redaction_spans(src, [{"text": "환자분", "category": "person_name"}])
    assert "[PERSON_NAME]" not in app.redacted_text
    assert app.ambiguous_spans and "occurrences" in app.ambiguous_spans[0]["reason"]


def test_apply_spans_unknown_category_becomes_other_pii():
    src = "코드 ABC123"
    app = apply_redaction_spans(src, [{"text": "ABC123", "category": "made_up"}])
    assert app.redacted_text == "코드 [OTHER_PII]"


def test_is_built_from_source_true_for_own_output():
    src = "환자 홍길동 전화 010-1234-5678 끝"
    app = apply_redaction_spans(src, [
        {"text": "홍길동", "category": "person_name"},
        {"text": "010-1234-5678", "category": "phone_number"},
    ])
    assert is_built_from_source(app.redacted_text, src) is True


def test_is_built_from_source_false_for_fabricated_text():
    src = "환자 홍길동 진단 골절"
    fabricated = "환자 [PERSON_NAME] 진단 골절. 참고: 조작된 문장."
    assert is_built_from_source(fabricated, src) is False


# ---------------------------------------------------------------------------
# S3: scan_residual_pii -- structured leak detection, calibrated for zero FP
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("leak", [
    "010-1234-5678",       # dashed mobile
    "02-123-4567",         # dashed landline
    "01012345678",         # no-dash mobile
    "800101-1234567",      # RRN
    "hong@example.com",    # email
    "12가3456",            # vehicle plate
    "12345678901",         # 11-digit run (account)
])
def test_scan_residual_pii_detects_structured_leaks(leak):
    hits = scan_residual_pii(f"내용 중 {leak} 남음")
    assert hits, f"should have flagged {leak}"


@pytest.mark.parametrize("clean", [
    "사고일 2023-05-14 진단 확정",          # date, not phone/RRN
    "지급보험금 12,340,000원",              # comma-grouped amount
    "질병분류기호 S82.3 골절",              # KCD code
    "면책 조항 제3조 제2항",                # clause refs
    "피보험자 [PERSON_NAME] 연락처 [PHONE_NUMBER]",  # already-redacted placeholders
    "기간 2020-01 ~ 2023-12",              # year-month ranges
])
def test_scan_residual_pii_no_false_positive(clean):
    assert scan_residual_pii(clean) == [], f"false positive on {clean!r}"


# ---------------------------------------------------------------------------
# S2: parse_pii_items
# ---------------------------------------------------------------------------
def test_parse_pii_items_happy_path():
    raw = '{"pii_items": [{"text": "홍길동", "category": "person_name"}]}'
    assert parse_pii_items(raw) == [{"text": "홍길동", "category": "person_name"}]


def test_parse_pii_items_empty_list_ok():
    assert parse_pii_items('{"pii_items": []}') == []


def test_parse_pii_items_coerces_unknown_category():
    raw = '{"pii_items": [{"text": "X", "category": "nonsense"}]}'
    assert parse_pii_items(raw)[0]["category"] == "other_pii"


def test_parse_pii_items_rejects_non_json():
    with pytest.raises(RedactionParseError):
        parse_pii_items("no json here")


def test_parse_pii_items_rejects_missing_array():
    with pytest.raises(RedactionParseError):
        parse_pii_items('{"something_else": 1}')


def test_parse_pii_items_rejects_bad_entry():
    with pytest.raises(RedactionParseError):
        parse_pii_items('{"pii_items": [{"category": "person_name"}]}')
