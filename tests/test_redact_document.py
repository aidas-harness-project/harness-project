import json

import pytest

import redact_document as rd
import redaction
from redaction import LlmRedactor, RedactionParseError, verify_fidelity
from llm_providers import FixtureProvider


def _fixture_redactor(response: str) -> LlmRedactor:
    provider = FixtureProvider(model_name="fixture-model", responses={"redact_text": response})
    return LlmRedactor(provider)


def test_parse_redaction_rejects_non_json():
    with pytest.raises(RedactionParseError) as exc:
        redaction.parse_redaction_response("not json")
    assert "not JSON" in str(exc.value)


def test_verify_fidelity_passes_clean_substitution():
    source = "환자 홍길동 전화 010-1234-5678 진단 골절"
    redacted = "환자 [PERSON_NAME] 전화 [PHONE_NUMBER] 진단 골절"
    assert verify_fidelity(source, redacted, items_redacted=2) == []


def test_verify_fidelity_flags_fabricated_content():
    # The redacted text invents a sentence that never appeared in the source --
    # exactly the OCR-layer contamination class (known-gaps 11/14/15).
    source = "환자 홍길동 진단 골절"
    redacted = "환자 [PERSON_NAME] 진단 골절. Note: routed per D2 guidance."
    warnings = verify_fidelity(source, redacted, items_redacted=1)
    assert any("not found verbatim" in w for w in warnings)


def test_verify_fidelity_flags_count_mismatch():
    source = "환자 홍길동 전화 010-1234-5678"
    redacted = "환자 [PERSON_NAME] 전화 [PHONE_NUMBER]"
    warnings = verify_fidelity(source, redacted, items_redacted=5)
    assert any("does not match" in w for w in warnings)


def test_verify_fidelity_flags_reorder():
    source = "first line\nsecond line"
    redacted = "second line\nfirst line"
    warnings = verify_fidelity(source, redacted, items_redacted=0)
    assert warnings  # reordered content is not an in-order slice of the source


def test_redact_document_uses_dao_for_every_case_data_access(monkeypatch, tmp_path):
    calls = []
    captured = {}
    ocr_result = {
        "cross_validation_status": "agreed",
        "pages": [{"page": 1}, {"page": 2}],
    }

    def fake_dao(*args):
        calls.append(args)
        if args[0] == "read-contract":
            return json.dumps(ocr_result)
        if args[0] == "read-page-text":
            # Page-invariant so the single canned fixture response is a faithful
            # redaction of every page (FixtureProvider ignores the prompt).
            return "환자 홍길동 placeholder-free source"
        if args[0] == "write-redacted-text":
            source = args[args.index("--text-file") + 1]
            captured["redacted"] = rd.Path(source).read_text(encoding="utf-8")
        if args[0] == "write-contract":
            source = args[args.index("--data-file") + 1]
            captured["contract"] = json.loads(rd.Path(source).read_text(encoding="utf-8"))
        return "OK"

    monkeypatch.setattr(rd, "_dao", fake_dao)
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    # Redacted output is a verbatim slice of the source with one PII substitution
    # -> passes fidelity, review_required stays False.
    redactor = _fixture_redactor(json.dumps({
        "redacted_text": "환자 [PERSON_NAME] placeholder-free source",
        "items_redacted": 1,
        "categories": ["person_name"],
    }))

    result = rd.redact_document(
        "CASE_009", "DOC_001", "document-pipeline", "RUN_20260714_001", redactor
    )

    assert [call[0] for call in calls].count("read-page-text") == 2
    assert captured["redacted"].startswith("<<<PAGE page=1>>>")
    assert "<<<PAGE page=2>>>" in captured["redacted"]
    assert captured["contract"]["items_redacted"] == 2
    assert captured["contract"]["review_required"] is False
    assert captured["contract"]["method"] == "llm_redaction"
    assert result["status"] == "success"


def test_redact_document_marks_review_required_on_fidelity_drift(monkeypatch, tmp_path):
    ocr_result = {"cross_validation_status": "agreed", "pages": [{"page": 1}]}
    captured = {}

    def fake_dao(*args):
        if args[0] == "read-contract":
            return json.dumps(ocr_result)
        if args[0] == "read-page-text":
            return "환자 홍길동 진단 골절"
        if args[0] == "write-contract":
            source = args[args.index("--data-file") + 1]
            captured["contract"] = json.loads(rd.Path(source).read_text(encoding="utf-8"))
        return "OK"

    monkeypatch.setattr(rd, "_dao", fake_dao)
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    # Model fabricated a trailing sentence not present in the source.
    redactor = _fixture_redactor(json.dumps({
        "redacted_text": "환자 [PERSON_NAME] 진단 골절. Fabricated addendum.",
        "items_redacted": 1,
        "categories": ["person_name"],
    }))

    result = rd.redact_document(
        "CASE_009", "DOC_001", "document-pipeline", "RUN_1", redactor
    )

    assert captured["contract"]["review_required"] is True
    assert result["review_required"] is True
    assert any("page 1" in w for w in captured["contract"]["warnings"])
