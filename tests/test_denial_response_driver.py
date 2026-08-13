"""Synthetic regressions for the bundle-level denial-response driver."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import run_denial_response_driver as driver
from llm_providers import CLAUDE_CLI_SCHEMA_MAX_CHARS


CASE, RUN = "CASE_9001", "RUN_20260813_1"
DIGEST, QUOTE = "b" * 64, "Synthetic insurer denial"


def _document(doc_id, start, end, doc_type):
    return {
        "document_id": doc_id, "source_file_name": "notice.pdf",
        "source_page_start": start, "source_page_end": end,
        "document_type": doc_type, "downstream_disposition": "text_only_no_normalization",
    }


def test_insurer_bundle_keeps_other_typed_contiguous_tail():
    manifest = {"documents": [
        _document("DOC_007", 1, 1, "insurer_response"),
        _document("DOC_008", 2, 12, "insurer_response"),
        _document("DOC_009", 13, 15, "other"),
        _document("DOC_010", 20, 20, "other"),
    ]}

    bundles = driver.insurer_bundles(manifest)

    assert [[doc["document_id"] for doc in bundle] for bundle in bundles] == [
        ["DOC_007", "DOC_008", "DOC_009"]
    ]


def test_denial_transport_schema_is_bounded_while_local_schema_remains_authoritative():
    transport = json.dumps(driver._transport_schema(), ensure_ascii=False, separators=(",", ":"))
    local = json.dumps(driver._body_schema(), ensure_ascii=False, separators=(",", ":"))

    assert len(transport) <= CLAUDE_CLI_SCHEMA_MAX_CHARS
    assert len(local) > CLAUDE_CLI_SCHEMA_MAX_CHARS
    assert transport != local
    assert not ({"allOf", "anyOf", "oneOf"} & set(driver._transport_schema()))


def test_local_schema_keeps_reviewer_role_requirement_outside_transport_schema():
    value = {
        "status": "success", "confidence": 0.9, "review_required": True,
        "warnings": [], "denial_reasons": [], "accepted_coverages": [],
    }
    bundle = [{"document_id": "DOC_007"}]

    with pytest.raises(Exception, match="reviewer_role"):
        driver._validate_bundle_output(value, bundle, driver._body_schema())


class _Provider:
    provider_name, model_name = "fixture", "fixture-model"

    def __init__(self):
        self.schemas = []

    def analyze_text_structured(self, prompt, prompt_version, output_schema):
        self.schemas.append(output_schema)
        return SimpleNamespace(structured_output={
            "status": "success", "confidence": 0.9, "review_required": False,
            "warnings": [], "accepted_coverages": [],
            "denial_reasons": [{
                "reason_id": "DR_1", "decided_coverage": "Synthetic coverage",
                "decision_type": "denial", "payment_status": "unpaid", "taxonomy_code": "R04",
                "candidate_codes": [{"taxonomy_code": "R04", "confidence": 0.9}],
                "raw_reason_text": QUOTE, "insurer_claim_summary": "Synthetic insurer position",
                "grounds": {"contractual_basis": [], "medical_or_factual_basis": [], "calculation_basis": []},
                "amounts": {"claimed_amount": None, "payable_amount": None, "denied_amount": None,
                            "reduction_amount": None, "reduction_rate": None},
                "requested_documents": [], "policy_matches": [], "confidence": 0.9,
                "evidence_references": [{"document_id": "DOC_007", "page": 1, "quote": QUOTE}],
                "review_required": False,
            }],
        })


def _result_with_quote(quote):
    return {
        "status": "success", "confidence": 0.9, "review_required": False,
        "warnings": [], "accepted_coverages": [],
        "denial_reasons": [{
            "reason_id": "DR_1", "decided_coverage": "Synthetic coverage",
            "decision_type": "denial", "payment_status": "unpaid", "taxonomy_code": "R04",
            "candidate_codes": [{"taxonomy_code": "R04", "confidence": 0.9}],
            "raw_reason_text": quote, "insurer_claim_summary": "Synthetic insurer position",
            "grounds": {"contractual_basis": [], "medical_or_factual_basis": [], "calculation_basis": []},
            "amounts": {"claimed_amount": None, "payable_amount": None, "denied_amount": None,
                        "reduction_amount": None, "reduction_rate": None},
            "requested_documents": [], "policy_matches": [], "confidence": 0.9,
            "evidence_references": [{"document_id": "DOC_007", "page": 1, "quote": quote}],
            "review_required": False,
        }],
    }


class _SequenceProvider:
    provider_name, model_name = "fixture", "fixture-model"

    def __init__(self, *results):
        self.results = list(results)
        self.prompts = []

    def analyze_text_structured(self, prompt, prompt_version, output_schema):
        self.prompts.append(prompt)
        return SimpleNamespace(structured_output=self.results.pop(0))


def test_bundle_evidence_validator_matches_dao_whitespace_rule_and_rejects_wrong_page_quote():
    bundle = [{"document_id": "DOC_007", "pages": [{"page": 1, "text": "Synthetic   insurer\n denial"}]}]
    schema = driver._body_schema()

    accepted = driver._validate_bundle_output(
        _result_with_quote("Synthetic insurer denial"), bundle, schema)
    assert accepted["denial_reasons"][0]["evidence_references"][0]["quote"] == "Synthetic insurer denial"

    with pytest.raises(ValueError, match="DOC_007: quote is not present on page 1"):
        driver._validate_bundle_output(_result_with_quote("not in the source"), bundle, schema)


def test_bundle_evidence_failure_uses_exactly_one_p4_correction():
    bundle = [{"document_id": "DOC_007", "pages": [{"page": 1, "text": QUOTE}]}]
    provider = _SequenceProvider(_result_with_quote("not in the source"), _result_with_quote(QUOTE))

    result = driver._extract(
        provider, bundle, driver._body_schema(), driver._transport_schema(), driver.VERSION,
        CASE, RUN,
    )

    assert result == _result_with_quote(QUOTE)
    assert len(provider.prompts) == 2
    assert "DOC_007: quote is not present on page 1" in provider.prompts[1]


def test_driver_publishes_one_deduplicated_bundle_contract(monkeypatch):
    writes = {}
    manifest = {"documents": [
        _document("DOC_007", 1, 1, "insurer_response"),
        _document("DOC_009", 2, 2, "other"),
    ]}
    bundle = {"documents": [
        {"document_id": "DOC_007", "redacted_text_sha256": DIGEST, "pages": [{"page": 1, "text": QUOTE}]},
        {"document_id": "DOC_009", "redacted_text_sha256": DIGEST, "pages": [{"page": 1, "text": "tail"}]},
    ]}

    def fake_read(args, *, allow_missing=False):
        if args[0] == "read-contract" and args[2] == "_run_state.json":
            return {"stages": [{"stage_name": "denial_response", "status": "in_progress"}]}
        if args[0] == "read-contract" and args[2] == "document_manifest.json":
            return manifest
        if args[0] == "read-redacted-text-bundle":
            return bundle
        if args[0] == "read-driver-receipt":
            return None
        if args[0] == "read-driver-candidates":
            return {"candidates": {}}
        if args[0] == "verify-evidence-references":
            refs = json.loads(open(args[args.index("--references-file") + 1], encoding="utf-8").read())["references"]
            return {"verified_references": [{**ref, "source_text_revision_sha256": None,
                                               "redacted_text_sha256": DIGEST} for ref in refs]}
        raise AssertionError(args)

    def fake_write(args):
        if args[0] == "write-contract":
            assert args[:3] == ["write-contract", CASE, "denial_reason_result.json"]
            assert args[args.index("--schema-name") + 1] == "denial_reason_result.schema.json"
        path = args[args.index("--data-file") + 1]
        writes[args[0]] = json.loads(open(path, encoding="utf-8").read())

    monkeypatch.setattr(driver, "_dao_json", fake_read)
    monkeypatch.setattr(driver, "_dao_write", fake_write)
    provider = _Provider()
    result = driver.run(case_id=CASE, held_by="denial-response", run_id=RUN, provider=provider, workers=2)

    assert result == {"status": "published", "bundle_count": 1,
                      "reused_candidate_count": 0, "contract": "denial_reason_result.json"}
    assert writes["write-driver-candidate"]["status"] == "complete"
    reason = writes["write-contract"]["denial_reasons"][0]
    assert reason["reason_id"] == "DR_1"
    assert reason["policy_matches"] == []
    assert writes["write-driver-receipt"]["completed_contracts"] == ["denial_reason_result.json"]
    assert provider.schemas == [driver._transport_schema()]
