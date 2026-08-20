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


# --- reviewer-found guard-coverage fixes ---
def test_overlapping_spans_not_misclassified_as_leak():
    # F1: a short span consumed by a longer overlapping span must NOT be treated
    # as an unmatched leak (it was in the source, and is now redacted).
    src = "작성자: 홍길동 (인) 서명일 2026-01-01."
    app = apply_redaction_spans(src, [
        {"text": "홍길동 (인)", "category": "other_pii"},
        {"text": "홍길동", "category": "person_name"},
    ])
    assert app.unmatched_spans == []       # not a leak
    assert "홍길동" not in app.redacted_text  # fully redacted


def test_institution_glued_name_not_corrupted():
    # Reviewer finding: a person name that is a substring of a kept institution
    # name must not be blind-replaced; the standalone person still redacts.
    src = "환자 김영수, 김영수병원 진료 완료"
    app = apply_redaction_spans(src, [{"text": "김영수", "category": "person_name"}])
    assert "김영수병원" in app.redacted_text          # institution preserved
    assert "환자 [PERSON_NAME]" in app.redacted_text  # standalone person redacted
    assert app.ambiguous_spans                        # glued occurrence flagged for review


def test_name_with_particle_still_redacts():
    # A normal name+particle (no institution suffix) must redact cleanly.
    app = apply_redaction_spans("홍길동은 서명했다", [{"text": "홍길동", "category": "person_name"}])
    assert app.redacted_text == "[PERSON_NAME]은 서명했다"


def test_compound_institution_name_not_corrupted():
    # The suffix need not be adjacent: 김영수의료재단 (재단 after 의료) is kept,
    # but a SPACED hospital visit (김영수 병원에) still redacts the person.
    kept = apply_redaction_spans("김영수의료재단 후원", [{"text": "김영수", "category": "person_name"}])
    assert "김영수의료재단" in kept.redacted_text
    spaced = apply_redaction_spans("김영수 병원에 갔다", [{"text": "김영수", "category": "person_name"}])
    assert spaced.redacted_text == "[PERSON_NAME] 병원에 갔다"


@pytest.mark.parametrize("leak", [
    "900202 2345678",        # spaced RRN
    "800101.1234567",        # dotted RRN
    "010.5555.7777",         # dotted phone
    "010 1234 5678",         # spaced phone
    "02 123 4567",           # spaced landline
    "12가 3456",             # spaced vehicle
    "123-45-678901",         # dashed account
    "８００１０１－１２３４５６７",  # full-width RRN
])
def test_scan_catches_separator_variants(leak):
    assert scan_residual_pii(f"내용 {leak} 끝"), f"missed {leak}"


@pytest.mark.parametrize("clean", [
    "계약일 2023-05-14, 만기 2025-05-14",  # dashed dates
    "기간 2020-01 ~ 2023-12",              # year-month range
    "번호 제2023-100호",                    # document number
    "페이지 12 / 34",                       # pagination
])
def test_scan_no_false_positive_on_document_numbers(clean):
    assert scan_residual_pii(clean) == [], f"false positive on {clean!r}"


@pytest.mark.parametrize("leak", [
    "901010 - 1234567",      # multi-separator RRN
    "901010-\n1234567",      # line-wrapped RRN
    "010  1234  5678",       # double-spaced phone
    "010-1234-\n5678",       # line-wrapped phone
    "(02) 123-4567",         # parenthesized area code
    "0212345678",            # contiguous landline
    "서울12가3456",          # region-prefixed plate
    "경기78나9012",          # region-prefixed plate 2
])
def test_scan_catches_fleet_separator_and_region_variants(leak):
    assert scan_residual_pii(f"내용 {leak} 끝"), f"missed {leak}"


@pytest.mark.parametrize("clean", [
    "품목 12개 1234원",       # counter phrase (개 not a plate syllable)
    "조항 1-2-3",             # short dashed clause ref
    "만기 2025-05-14",        # date
    "기간 2020-01 ~ 2023-12", # year-month range
])
def test_scan_no_fp_on_fleet_clean_cases(clean):
    assert scan_residual_pii(clean) == [], f"false positive on {clean!r}"


# --- billing table: amounts meeting the next row's year are not a phone -----
# Measured on CASE_046/DOC_005 p14 (2026-08-17). A 진료비 세부산정내역 table
# printed as "<amount> <amount>\n<date>" put two 3-digit amounts directly above
# a year, which the plain \d{2,3} SEP \d{3,4} SEP \d{4} rule could not tell from
# a phone number. It blocked the document three times. The SAME page read by a
# different model drew box characters instead and produced no hit at all, so the
# rule's verdict depended on transcription layout rather than on content.

@pytest.mark.parametrize("clean", [
    "810              810\n2023",   # the exact CASE_046 p14 text
    "210              210\n2023",
    "315              315\n2023",
    "4,870  4,870\n2024",           # comma-grouped amounts above a year
    "540    540\n1998",             # 19xx year is excluded too
])
def test_billing_amounts_above_a_year_are_not_read_as_a_phone(clean):
    assert scan_residual_pii(clean) == [], f"false positive on {clean!r}"


@pytest.mark.parametrize("leak", [
    "010-1234-\n5678",   # a REAL wrapped phone must still be caught
    "010-1234-\n5679",   # wrapped, last group simply is not a year
    "010-1234-2023",     # inline phone whose last group happens to be a year
    "02 123 4567",       # ordinary in-line phone
])
def test_year_exclusion_does_not_blind_the_scan_to_real_phones(leak):
    assert scan_residual_pii(f"내용 {leak} 끝"), f"missed {leak}"


# --- NoPiiClassRedactor: class-scoped exemption, still leak-checked ----------

def test_no_pii_class_redactor_passes_clean_policy_text_through_verbatim():
    from redaction import NoPiiClassRedactor
    text = "제1조(보상하는 손해)\n회사는 영업배상책임보험 보통약관에 따라 보상합니다."
    out = NoPiiClassRedactor().redact_page(text)
    assert out.redacted_text == text
    assert out.items_redacted == 0
    assert out.review_warnings == []


def test_no_pii_class_redactor_hard_fails_on_structured_pii():
    """The exemption is a claim about a document CLASS, verified per page.
    If structured PII is actually present, the document is blocked -- it is
    never silently passed through."""
    from redaction import NoPiiClassRedactor, RedactionLeakError
    text = "제1조(보상하는 손해)\n피보험자 주민등록번호 800101-1234567"
    with pytest.raises(RedactionLeakError):
        NoPiiClassRedactor().redact_page(text)


def test_no_pii_class_redactor_makes_no_provider_call():
    """It must not need a provider at all -- that is the entire cost saving."""
    from redaction import NoPiiClassRedactor
    r = NoPiiClassRedactor()
    assert not hasattr(r, "provider")
    assert r.method == "no_pii_class_passthrough"


# --- billing table: a drug code in the code column is not a phone -----------
# Measured on CASE_488/DOC_005 p14 (2026-08-20). A 진료비 세부산정내역 table
# prints "<date>\t<drug code>\t<name>\t<amount>...", and the 의약품 표준코드 in
# the code column is a 9-10 digit run that begins 0 whenever the manufacturer's
# prefix does -- indistinguishable from a contiguous landline under the plain
# 0\d{9,10} rule. 0647801081 (타우롤린주사2%250ml) blocked the document on BOTH
# paths: --skip-redaction flagged it as present, and the LLM redactor correctly
# judged it non-PII and left it, whereupon the residual scan flagged it again.
# There was no configuration under which the document could pass.
#
# The exemption is anchored on the TABLE CELL, not on the number's shape: the
# run must occupy a whole tab-delimited cell whose preceding cell is a date.
# A real phone is never typeset that way -- it carries a label (연락처/전화) or
# separators -- so this cannot wave a claimant's number through.

@pytest.mark.parametrize("clean", [
    # the exact CASE_488 p14 row
    "\t2023-12-05\t0647801081\t타우롤린주사2%250ml (삼진)\t90,000\t1\t1\t90,000",
    # same column, other manufacturers' codes from the same page
    "\t2023-12-05\t0527014310\t이부프로펜주400mg/100ml/bag\t35,000\t1\t1\t35,000",
    "\t2023-12-07\t0416029400\t일반타인파워정 (대응)\t400\t1\t12\t4,800",
])
def test_drug_code_in_billing_code_column_is_not_read_as_a_phone(clean):
    assert scan_residual_pii(clean) == [], f"false positive on {clean!r}"


@pytest.mark.parametrize("leak", [
    # a real contiguous landline in the same table must STILL be caught
    "\t2023-12-05\t연락처 0212345678\t비고",
    # bare contiguous number with no date cell before it
    "\t환자 연락\t0647801081\t비고",
    # a mobile in a cell after a date is still a leak (mobile prefix)
    "\t2023-12-05\t01012345678\t비고",
])
def test_code_column_exemption_does_not_blind_the_scan_to_real_phones(leak):
    assert scan_residual_pii(f"내용 {leak} 끝"), f"missed {leak}"
