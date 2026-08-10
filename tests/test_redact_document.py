import json

import pytest

import redact_document as rd
from redaction import LlmRedactor, RedactionLeakError
from llm_providers import FixtureProvider


def _fixture_redactor(pii_items_json: str) -> LlmRedactor:
    provider = FixtureProvider(model_name="fixture-model", responses={"redact_text": pii_items_json})
    return LlmRedactor(provider)


OCR_RESULT = {"cross_validation_status": "agreed", "pages": [{"page": 1}, {"page": 2}]}


def _install_dao_stubs(monkeypatch, page_text, captured=None, calls=None):
    """Stub the DAO surface redact_document uses.

    The per-page read is now an in-process call (T4a: a subprocess per page
    multiplied ~0.38s of interpreter startup by page count), so it is stubbed
    separately from the subprocess writes. The capability assertion is kept on
    BOTH paths -- it is the check that stops a regression from reading
    pre-redaction text without checkpoint 2's token, and moving the call
    in-process must not be allowed to quietly drop it.
    """
    def fake_read_contract(case_id, filename):
        if calls is not None:
            calls.append(("read-contract", filename))
        return dict(OCR_RESULT) if filename.startswith("ocr_result") else {}

    def fake_read_page_text(case_id, doc_id, page, *, caller_stage, capability):
        if calls is not None:
            calls.append(("read-page-text", page))
        assert capability, "read_page_text_data must receive checkpoint 2's capability"
        assert caller_stage == "document-pipeline", (
            "the caller-stage gate must still be asserted in-process")
        return page_text  # page-invariant so one canned fixture fits both pages

    def fake_dao(*args, capability=None):
        if calls is not None:
            calls.append(args)
        assert capability is None, f"{args[0]} must not be handed the page-text capability"
        if captured is not None and args[0] == "write-redacted-text":
            captured["redacted"] = rd.Path(args[args.index("--text-file") + 1]).read_text(encoding="utf-8")
        if captured is not None and args[0] == "write-contract":
            captured["contract"] = json.loads(rd.Path(args[args.index("--data-file") + 1]).read_text(encoding="utf-8"))
        return "OK"

    monkeypatch.setattr(rd.dao, "read_contract_data", fake_read_contract)
    monkeypatch.setattr(rd.dao, "read_page_text_data", fake_read_page_text)
    monkeypatch.setattr(rd, "_dao", fake_dao)
    return fake_dao


def test_clean_redaction_writes_via_dao_and_flags_no_review(monkeypatch, tmp_path):
    calls, captured = [], {}
    _install_dao_stubs(monkeypatch, "환자 홍길동 진단 골절", captured, calls)
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    redactor = _fixture_redactor(json.dumps({"pii_items": [{"text": "홍길동", "category": "person_name"}]}))

    result = rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1", redactor)

    assert [c[0] for c in calls].count("read-page-text") == 2
    assert captured["redacted"].startswith("<<<PAGE page=1>>>")
    assert "[PERSON_NAME]" in captured["redacted"]
    assert "홍길동" not in captured["redacted"]  # PII gone
    assert "진단 골절" in captured["redacted"]   # non-PII preserved by construction
    assert captured["contract"]["review_required"] is False
    assert captured["contract"]["method"] == "llm_span_redaction"
    assert captured["contract"]["items_redacted"] == 2  # one per page
    assert result["status"] == "success"


def test_residual_structured_pii_hard_fails_and_writes_nothing(monkeypatch, tmp_path):
    calls = []
    # Page has a phone number; the model lists only the name -> phone survives.
    _install_dao_stubs(monkeypatch, "환자 홍길동 010-1234-5678", calls=calls)
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    redactor = _fixture_redactor(json.dumps({"pii_items": [{"text": "홍길동", "category": "person_name"}]}))

    with pytest.raises(RedactionLeakError) as exc:
        rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1", redactor)
    assert "phone" in str(exc.value).lower()
    # nothing was written
    assert "write-redacted-text" not in [c[0] for c in calls]
    assert "write-contract" not in [c[0] for c in calls]


def test_unmatched_span_hard_fails(monkeypatch, tmp_path):
    _install_dao_stubs(monkeypatch, "환자 홍길동 진단 골절")
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    # Model names a person not present verbatim in the source.
    redactor = _fixture_redactor(json.dumps({"pii_items": [{"text": "김철수", "category": "person_name"}]}))

    with pytest.raises(RedactionLeakError) as exc:
        rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1", redactor)
    assert "verbatim" in str(exc.value).lower()


def test_over_redaction_ambiguous_span_writes_with_review(monkeypatch, tmp_path):
    captured = {}
    _install_dao_stubs(monkeypatch, "이 사람은 이번 사고를 겪었다", captured)
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    # A 1-char "name" -> over-redaction guard leaves it, flags review (no leak).
    redactor = _fixture_redactor(json.dumps({"pii_items": [{"text": "이", "category": "person_name"}]}))

    result = rd.redact_document("CASE_009", "DOC_001", "document-pipeline", "RUN_1", redactor)

    assert captured["contract"]["review_required"] is True
    assert any("not redacted" in w for w in captured["contract"]["warnings"])
    assert result["review_required"] is True
