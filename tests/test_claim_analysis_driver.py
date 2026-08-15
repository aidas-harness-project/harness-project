"""Synthetic regressions for the grouped claim-analysis CP1 driver (v2 pilot)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import run_claim_analysis as driver
from llm_providers import CLAUDE_CLI_SCHEMA_MAX_CHARS


CASE, RUN = "CASE_9001", "RUN_20260816_1"
DIGEST = "b" * 64
QUOTE = "우측 원위 요골 골절"
PAGE_TEXT = f"진단명: {QUOTE} (S6280)"


def _document(doc_id, doc_type, disposition="text_only_no_normalization"):
    return {"document_id": doc_id, "document_type": doc_type,
            "downstream_disposition": disposition}


def _valid_body():
    return {
        "status": "success", "confidence": 0.9, "review_required": False,
        "warnings": [],
        "fields": {
            "diagnosis_name": {
                "value": QUOTE, "confidence": 0.95, "review_required": False,
                "evidence_references": [
                    {"document_id": "DOC_011", "page": 1, "quote": QUOTE}],
            },
        },
    }


def test_cp1_documents_excludes_policy_untyped_and_non_text_dispositions():
    """The selection IS the P2/double-count boundary: a policy bundle is CP2's
    input, an untyped entry is a bundle parent, and a non-text disposition
    (superseded bundle, expert-review-only image) must never be extraction
    input. Widening any of these silently recreates the arm A shape."""
    manifest = {"documents": [
        _document("DOC_009", "insurance_policy"),
        _document("DOC_011", "diagnosis_certificate"),
        _document("DOC_005", None),                                # bundle parent
        _document("DOC_013", "medical_record", disposition="superseded_bundle"),
        _document("DOC_016", "imaging_report", disposition="non_text_image"),
        _document("DOC_007", "insurer_response"),
    ]}
    assert [doc["document_id"] for doc in driver.cp1_documents(manifest)] == [
        "DOC_007", "DOC_011"]


def test_transport_schema_is_bounded_and_flat_while_local_schema_is_authoritative():
    transport = json.dumps(driver._transport_schema(), ensure_ascii=False,
                           separators=(",", ":"))
    assert len(transport) <= CLAUDE_CLI_SCHEMA_MAX_CHARS
    assert not ({"allOf", "anyOf", "oneOf"} & set(driver._transport_schema()))
    # The local gate keeps what transport cannot carry: the reviewer_role
    # conditional and the real field shapes.
    body = _valid_body()
    body["review_required"] = True  # no reviewer_role -> local schema must refuse
    bundle = [{"document_id": "DOC_011", "pages": [{"page": 1, "text": PAGE_TEXT}]}]
    with pytest.raises(Exception, match="reviewer_role"):
        driver._validate_cp1_output(body, bundle, driver._body_schema())


def test_body_schema_resolves_field_shape_refs():
    """The fields subschema references #/$defs/value_field by fragment; the
    body schema must carry the public $defs or every validation would raise
    an unresolvable-reference error instead of judging the value. A field
    missing its evidence_references must be REFUSED by the ridden-along
    field_common shape."""
    bad = _valid_body()
    del bad["fields"]["diagnosis_name"]["evidence_references"]
    bundle = [{"document_id": "DOC_011", "pages": [{"page": 1, "text": PAGE_TEXT}]}]
    with pytest.raises(Exception, match="evidence_references|is not valid"):
        driver._validate_cp1_output(bad, bundle, driver._body_schema())


def test_quote_validator_applies_dao_whitespace_rule_and_rejects_absent_quote():
    schema = driver._body_schema()
    bundle = [{"document_id": "DOC_011",
               "pages": [{"page": 1, "text": "진단명:  우측   원위 요골\n골절 (S6280)"}]}]
    body = _valid_body()  # quote with single spaces -- must pass ws-normalized
    assert driver._validate_cp1_output(body, bundle, schema)["status"] == "success"

    body["fields"]["diagnosis_name"]["evidence_references"][0]["quote"] = "좌측 상완골 골절"
    with pytest.raises(ValueError, match="not present on page"):
        driver._validate_cp1_output(body, bundle, schema)


def test_run_refuses_unless_orchestrator_opened_the_attempt(monkeypatch):
    def fake_read(args, *, allow_missing=False):
        assert args[:3] == ["read-contract", CASE, "_run_state.json"]
        return {"stages": [{"stage_name": "claim_analysis", "status": "pending"}]}

    monkeypatch.setattr(driver, "_dao_json", fake_read)
    with pytest.raises(RuntimeError, match="orchestrator owns attempt state"):
        driver.run(case_id=CASE, held_by="claim-analysis", run_id=RUN,
                   provider=SimpleNamespace(provider_name="fixture", model_name="fixture"))


class _Provider:
    provider_name, model_name = "fixture", "fixture-model"

    def __init__(self, body=None, fail=False):
        self.schemas = []
        self.body = body or _valid_body()
        self.fail = fail

    def analyze_text_structured(self, prompt, prompt_version, output_schema):
        if self.fail:
            raise AssertionError("provider must not be called when the candidate is reusable")
        self.schemas.append(output_schema)
        return SimpleNamespace(structured_output=json.loads(json.dumps(self.body)))


def _dao_fakes(monkeypatch, *, stored_candidates=None):
    writes = {}
    manifest = {"documents": [
        _document("DOC_009", "insurance_policy"),
        _document("DOC_011", "diagnosis_certificate"),
    ]}
    bundle = {"documents": [
        {"document_id": "DOC_011", "redacted_text_sha256": DIGEST,
         "pages": [{"page": 1, "text": PAGE_TEXT}]},
    ]}

    def fake_read(args, *, allow_missing=False):
        if args[0] == "read-contract" and args[2] == "_run_state.json":
            return {"stages": [{"stage_name": "claim_analysis", "status": "in_progress"}]}
        if args[0] == "read-contract" and args[2] == "document_manifest.json":
            return manifest
        if args[0] == "read-redacted-text-bundle":
            assert args.count("--doc-id") == 1 and "DOC_011" in args and "DOC_009" not in args
            return bundle
        if args[0] == "read-driver-receipt":
            return None
        if args[0] == "read-driver-candidates":
            return {"candidates": stored_candidates or {}}
        if args[0] == "verify-evidence-references":
            refs = json.loads(open(args[args.index("--references-file") + 1],
                                   encoding="utf-8").read())["references"]
            return {"verified_references": [
                {**ref, "source_text_revision_sha256": None,
                 "redacted_text_sha256": DIGEST} for ref in refs]}
        raise AssertionError(args)

    def fake_write(args):
        path = args[args.index("--data-file") + 1]
        writes[args[0]] = json.loads(open(path, encoding="utf-8").read())
        if args[0] == "write-contract":
            assert args[:3] == ["write-contract", CASE, "extracted_claim_fields.json"]
            assert args[args.index("--schema-name") + 1] == "extracted_claim_fields.schema.json"

    monkeypatch.setattr(driver, "_dao_json", fake_read)
    monkeypatch.setattr(driver, "_dao_write", fake_write)
    return writes


def test_driver_publishes_grouped_cp1_contract(monkeypatch):
    writes = _dao_fakes(monkeypatch)
    provider = _Provider()

    result = driver.run(case_id=CASE, held_by="claim-analysis", run_id=RUN, provider=provider)

    assert result == {"status": "published", "document_count": 1,
                      "provider_called": True, "contract": "extracted_claim_fields.json"}
    assert provider.schemas == [driver._transport_schema()]
    contract = writes["write-contract"]
    assert contract["component"] == "claim-analysis"
    assert contract["fields"]["diagnosis_name"]["value"] == QUOTE
    # The bound reference must carry the DAO's verification fields, proving the
    # verify-evidence-references round trip was applied and not skipped.
    ref = contract["fields"]["diagnosis_name"]["evidence_references"][0]
    assert ref["redacted_text_sha256"] == DIGEST
    assert writes["write-driver-candidate"]["status"] == "complete"
    assert writes["write-driver-receipt"]["completed_contracts"] == [
        "extracted_claim_fields.json"]


def test_matching_candidate_is_reused_without_a_provider_call(monkeypatch):
    import driver_runtime

    selected = driver.cp1_documents({"documents": [
        _document("DOC_009", "insurance_policy"),
        _document("DOC_011", "diagnosis_certificate"),
    ]})
    digests = {"manifest_entries": driver._digest(selected),
               "redacted:DOC_011": DIGEST}
    candidate = driver_runtime.make_candidate(
        case_id=CASE, run_id=RUN, stage="claim_analysis", unit_id="cp1_field_extraction",
        candidate_id=driver.grouped_candidate_id(selected), input_digests=digests,
        prompt_version=driver.VERSION, response_schema_version=driver.VERSION,
        provider_name="fixture", model_name="fixture-model", result=_valid_body())
    writes = _dao_fakes(monkeypatch,
                        stored_candidates={candidate["candidate_id"]: candidate})
    provider = _Provider(fail=True)  # any provider call fails the test

    result = driver.run(case_id=CASE, held_by="claim-analysis", run_id=RUN, provider=provider)

    assert result["provider_called"] is False
    assert result["status"] == "published"
    assert "write-contract" in writes
