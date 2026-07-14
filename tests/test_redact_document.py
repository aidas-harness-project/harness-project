import json

import redact_document as rd
from llm_providers import FixtureProvider


def test_parse_redaction_rejects_non_json():
    try:
        rd._parse_redaction("not json")
    except Exception as exc:
        assert "not JSON" in str(exc)
    else:
        raise AssertionError("non-JSON redaction output must fail closed")


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
            return f"page {args[3]} Patient Kim 010-1234-5678"
        if args[0] == "write-redacted-text":
            source = args[args.index("--text-file") + 1]
            captured["redacted"] = rd.Path(source).read_text(encoding="utf-8")
        if args[0] == "write-contract":
            source = args[args.index("--data-file") + 1]
            captured["contract"] = json.loads(rd.Path(source).read_text(encoding="utf-8"))
        return "OK"

    monkeypatch.setattr(rd, "_dao", fake_dao)
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    provider = FixtureProvider(
        model_name="local-fixture",
        responses={
            "redact_text": json.dumps({
                "redacted_text": "[PERSON_NAME] [PHONE_NUMBER]",
                "items_redacted": 2,
                "categories": ["person_name", "phone_number"],
            })
        },
    )

    result = rd.redact_document(
        "CASE_009", "DOC_001", "document-pipeline", "RUN_20260714_001", provider
    )

    assert [call[0] for call in calls].count("read-page-text") == 2
    assert captured["redacted"].startswith("<<<PAGE page=1>>>")
    assert "<<<PAGE page=2>>>" in captured["redacted"]
    assert captured["contract"]["items_redacted"] == 4
    assert captured["contract"]["redacted_text_path"] == (
        "data/processed/CASE_009/DOC_001/redacted_text.md"
    )
    assert result["status"] == "success"
