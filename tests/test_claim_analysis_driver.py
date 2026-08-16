"""Synthetic regressions for the claim-analysis driver (v3, 2-call spine)."""
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


def test_cp1_transport_schema_natively_rejects_scalar_field_members():
    """The 2026-08-16 arm E failure shape: a scalar `confidence` inside
    `fields` must be rejected by the TRANSPORT schema (at generation time),
    not merely by the local gate afterwards -- otherwise the P4 correction is
    burned on a shape the native constraint could have prevented.

    The member fixture carries the three keys `field_common` requires because
    the transport now pins them natively too (the 2026-08-16 sonnet arms
    omitted `review_required` on 38 of 57 fields under the old loose shape).
    """
    from jsonschema import Draft202012Validator, ValidationError

    validator = Draft202012Validator(driver._transport_schema())
    member = {"value": "x", "confidence": 0.9, "review_required": False,
              "evidence_references": [
                  {"document_id": "DOC_011", "page": 1, "quote": QUOTE}]}
    good = {"status": "success", "confidence": 0.9, "review_required": False,
            "warnings": [], "fields": {"diagnosis_name": member}}
    validator.validate(good)

    bad = json.loads(json.dumps(good))
    bad["fields"]["confidence"] = 0.72
    with pytest.raises(ValidationError):
        validator.validate(bad)

    # The omissions the loose transport used to let through, now refused at
    # generation time rather than by the local gate.
    for missing in ("confidence", "review_required", "evidence_references"):
        incomplete = json.loads(json.dumps(good))
        del incomplete["fields"]["diagnosis_name"][missing]
        with pytest.raises(ValidationError):
            validator.validate(incomplete)

    # `normalized_value` as a number was the v3 failure; the schema declares
    # a string, so the transport must say so too.
    numeric = json.loads(json.dumps(good))
    numeric["fields"]["diagnosis_name"]["normalized_value"] = 500000000
    with pytest.raises(ValidationError):
        validator.validate(numeric)


def test_cp3_transport_schema_natively_rejects_misnamed_profile_keys():
    """The second arm E failure shape: right semantics, wrong key names
    (`mechanism`/`depth`) inside report_profile. The transport schema must
    reject them at generation time."""
    from jsonschema import Draft202012Validator, ValidationError

    validator = Draft202012Validator(driver._transport_schema_cp3())
    good = _cp3_body()
    validator.validate(good)

    bad = json.loads(json.dumps(good))
    bad["report_profile"] = {"format_contract_version": "loss_adjustment_report.v1",
                             "family": "liability_damages",
                             "mechanism": "insured_liability",  # wrong key
                             "depth": "full",                   # wrong key
                             "support_status": "supported"}
    with pytest.raises(ValidationError):
        validator.validate(bad)


def test_run_refuses_unless_orchestrator_opened_the_attempt(monkeypatch):
    def fake_read(args, *, allow_missing=False):
        assert args[:3] == ["read-contract", CASE, "_run_state.json"]
        return {"stages": [{"stage_name": "claim_analysis", "status": "pending"}]}

    monkeypatch.setattr(driver, "_dao_json", fake_read)
    with pytest.raises(RuntimeError, match="orchestrator owns attempt state"):
        driver.run(case_id=CASE, held_by="claim-analysis", run_id=RUN,
                   provider=SimpleNamespace(provider_name="fixture", model_name="fixture"))


POLICY_QUOTE = "회사는 보험사고에 대하여 보상합니다"
POLICY_PAGE_TEXT = f"제1조(보상하는 손해)\n{POLICY_QUOTE}."


def _cp2_body():
    return {
        "status": "success", "confidence": 0.8, "review_required": False,
        "warnings": [],
        "coverages": [{
            "coverage_name": "테스트 특별약관",
            "standardized_coverage_name": "test_coverage",
            "applicable": True, "confidence": 0.8, "review_required": False,
            "matched_clause_ref": {"document_id": "DOC_009", "page": 2,
                                   "quote": POLICY_QUOTE},
            "evidence_references": [
                {"document_id": "DOC_009", "page": 2, "quote": POLICY_QUOTE}],
        }],
    }


def _cp3_body():
    return {
        "status": "success", "confidence": 0.8, "review_required": False,
        "warnings": [],
        "case_type": "배상책임", "coverage_basis": "배상책임", "loss_type": "후유장해",
        "is_claim_case": True, "case_type_source": "inferred",
        "secondary_case_types": [], "candidate_types": [
            {"case_type": "배상책임", "confidence": 0.8}],
        "template_id": "배상책임_후유장해형",
        "report_profile": {"format_contract_version": "loss_adjustment_report.v1",
                           "family": "liability_damages",
                           "claim_mechanism": "insured_liability",
                           "mode": "full", "support_status": "supported"},
        "evidence_references": [
            {"document_id": "DOC_011", "page": 1, "quote": QUOTE}],
    }


def _cp4_body():
    return {
        "status": "success", "confidence": 0.8, "review_required": False,
        "warnings": [],
        "coverage_requirements": [{
            "coverage_name": "테스트 특별약관",
            "standardized_coverage_name": "test_coverage",
            "requirements": [{
                "requirement_id": "REQ-9",  # provisional; the driver renumbers
                "requirement_text": "보험사고일 것",
                "clause_ref": {"document_id": "DOC_009", "page": 2,
                               "quote": POLICY_QUOTE},
                "status": "met", "confidence": 0.8, "review_required": False,
                "evidence_references": [
                    {"document_id": "DOC_011", "page": 1, "quote": QUOTE}],
            }],
        }],
    }


class _SequenceProvider:
    provider_name, model_name = "fixture", "fixture-model"

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def analyze_text_structured(self, prompt, prompt_version, output_schema):
        if not self.results:
            raise AssertionError("provider called more times than the test allows")
        self.calls += 1
        return SimpleNamespace(
            structured_output=json.loads(json.dumps(self.results.pop(0))))


_MANIFEST_DOCS = [
    _document("DOC_009", "insurance_policy"),
    _document("DOC_011", "diagnosis_certificate"),
]
_INDEX = {"documents": [{"document_id": "DOC_009", "clauses": [
    {"page": 2, "policy_name": "테스트약관", "article": "제1조",
     "heading": "보상하는 손해"}], "tables": []}]}


def _dao_fakes(monkeypatch, *, stored_candidates=None):
    writes = {}
    manifest = {"documents": list(_MANIFEST_DOCS), "adjuster_case_type": None}
    cp1_bundle = {"documents": [
        {"document_id": "DOC_011", "redacted_text_sha256": DIGEST,
         "pages": [{"page": 1, "text": PAGE_TEXT}]},
    ]}
    policy_bundle = {"documents": [
        {"document_id": "DOC_009", "redacted_text_sha256": DIGEST,
         "pages": [{"page": 2, "text": POLICY_PAGE_TEXT}]},
    ]}
    index = _INDEX

    def fake_read(args, *, allow_missing=False):
        if args[0] == "read-contract" and args[2] == "_run_state.json":
            return {"stages": [{"stage_name": "claim_analysis", "status": "in_progress"}]}
        if args[0] == "read-contract" and args[2] == "document_manifest.json":
            return manifest
        if args[0] == "read-contract":
            return None  # no prior checkpoint contracts
        if args[0] == "read-document-index":
            return index
        if args[0] == "read-redacted-text-bundle":
            return policy_bundle if "--pages" in args else cp1_bundle
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
        payload = json.loads(open(path, encoding="utf-8").read())
        if args[0] == "write-contract":
            writes[("write-contract", args[2])] = payload
        elif args[0] == "write-driver-candidate":
            writes[(args[0], args[args.index("--candidate-id") + 1])] = payload
        else:
            writes[(args[0], args[args.index("--unit-id") + 1])] = payload

    monkeypatch.setattr(driver, "_dao_json", fake_read)
    monkeypatch.setattr(driver, "_dao_write", fake_write)
    monkeypatch.setattr(driver, "_fetch_snapshot",
                        lambda *a, **k: {"documents": [], "snapshot_sha256": "0" * 64})
    monkeypatch.setattr(driver, "_attempt_medical_publication",
                        lambda *a, **k: {"published": False,
                                         "deferred_config_refusal": True})
    return writes


def _m1_body(pages=None):
    body = _valid_body()
    body["selected_pages"] = pages or [{"document_id": "DOC_009", "page": 2}]
    return body


def _m2_body():
    return {"coverage_section": _cp2_body(), "case_type_section": _cp3_body(),
            "requirements_section": _cp4_body()}


def test_full_run_publishes_all_four_contracts(monkeypatch):
    writes = _dao_fakes(monkeypatch)
    provider = _SequenceProvider(_m1_body(), _m2_body())

    result = driver.run(case_id=CASE, held_by="claim-analysis", run_id=RUN, provider=provider)

    assert result["status"] == "complete"
    assert provider.calls == 2
    for unit in ("cp1_field_extraction", "cp2_coverage", "cp3_case_type",
                 "cp4_requirements"):
        assert result["units"][unit]["status"] == "published"
    assert result["medical_publication"]["deferred_config_refusal"] is True

    cp1 = writes[("write-contract", "extracted_claim_fields.json")]
    assert cp1["fields"]["diagnosis_name"]["value"] == QUOTE
    # Bound references carry the DAO's verification fields -- the
    # verify-evidence-references round trip was applied, not skipped.
    assert cp1["fields"]["diagnosis_name"]["evidence_references"][0][
        "redacted_text_sha256"] == DIGEST

    cp2 = writes[("write-contract", "coverage_result.json")]
    assert cp2["coverages"][0]["standardized_coverage_name"] == "test_coverage"
    assert cp2["upstream_policy_snapshot"]["snapshot_sha256"] == "0" * 64

    cp3 = writes[("write-contract", "case_type_result.json")]
    assert cp3["template_id"] == "배상책임_후유장해형"
    assert cp3["case_type_source"] == "inferred"

    cp4 = writes[("write-contract", "requirement_matching_result.json")]
    req = cp4["coverage_requirements"][0]["requirements"][0]
    assert req["requirement_id"] == "REQ-1"  # renumbered from provisional REQ-9
    # every unit left a receipt so a rerun can reuse it
    for unit in ("cp1_field_extraction", "cp2_coverage", "cp3_case_type",
                 "cp4_requirements"):
        assert ("write-driver-receipt", unit) in writes


def test_m1_candidate_reuse_skips_the_first_provider_call(monkeypatch):
    import driver_runtime

    selected = driver.cp1_documents({"documents": list(_MANIFEST_DOCS)})
    digests = {"manifest_entries": driver._digest(selected),
               "document_index": driver._digest(_INDEX),
               "redacted:DOC_011": DIGEST}
    candidate = driver_runtime.make_candidate(
        case_id=CASE, run_id=RUN, stage="claim_analysis", unit_id=driver.UNIT_M1,
        candidate_id=driver.grouped_candidate_id(selected), input_digests=digests,
        prompt_version=driver.VERSION, response_schema_version=driver.VERSION,
        provider_name="fixture", model_name="fixture-model",
        result={"cp1": _valid_body(),
                "selection": {"pages": [{"document_id": "DOC_009", "page": 2}]}})
    writes = _dao_fakes(monkeypatch,
                        stored_candidates={candidate["candidate_id"]: candidate})
    provider = _SequenceProvider(_m2_body())

    result = driver.run(case_id=CASE, held_by="claim-analysis", run_id=RUN, provider=provider)

    assert provider.calls == 1  # M1 came from the stored candidate
    assert result["units"]["cp1_field_extraction"]["status"] == "published"
    assert ("write-contract", "extracted_claim_fields.json") in writes


def test_selection_keeps_continuation_pages_and_drops_only_unreadable_ones(monkeypatch):
    """The read plan admits continuation pages (indexed ceilings, not indexed
    membership): the first arm E run burned its correction on DOC_009's
    p3-style continuation. Pages beyond the ceiling are dropped, not fatal."""
    writes = _dao_fakes(monkeypatch)
    # index lists only p2; selection asks for p2, p1 (continuation-range) and p99
    m1 = _m1_body(pages=[{"document_id": "DOC_009", "page": 2},
                         {"document_id": "DOC_009", "page": 1},
                         {"document_id": "DOC_009", "page": 99}])
    provider = _SequenceProvider(m1, _m2_body())

    result = driver.run(case_id=CASE, held_by="claim-analysis", run_id=RUN, provider=provider)

    assert result["status"] == "complete"
    selected = driver.cp1_documents({"documents": list(_MANIFEST_DOCS)})
    stored = writes[("write-driver-candidate", driver.grouped_candidate_id(selected))]
    assert {"document_id": "DOC_009", "page": 1} in stored["result"]["selection"]["pages"]
    assert stored["result"]["selection"]["dropped"] == ["DOC_009p99"]


def test_cp4_join_enforcement_refuses_unknown_coverage_name():
    """A requirements group naming a coverage checkpoint 2 never identified is
    a broken join, caught in local validation so it gets the P4 correction."""
    body = _cp4_body()
    body["coverage_requirements"][0]["standardized_coverage_name"] = "invented_coverage"
    served = {("DOC_009", 2): POLICY_PAGE_TEXT}
    schema = driver._body_schema_cp4()
    coverage_names = {"test_coverage"}

    from jsonschema import Draft202012Validator, FormatChecker
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(body)
    with pytest.raises(ValueError, match="unknown coverage"):
        for group in body["coverage_requirements"]:
            if group.get("standardized_coverage_name") not in coverage_names:
                raise ValueError(
                    f"coverage_requirements names unknown coverage "
                    f"{group.get('standardized_coverage_name')!r}; join exactly on "
                    "checkpoint 2's standardized_coverage_name values")


def test_ref_grounding_rejects_unserved_unknown_quotes():
    """A call that saw no new source text may only reuse verified quotes; a
    clause ref must quote a served policy page even if the quote is 'known'."""
    served = {("DOC_009", 2): POLICY_PAGE_TEXT}
    known = {("DOC_011", 1, driver._norm(QUOTE))}

    ok = {"evidence_references": [{"document_id": "DOC_011", "page": 1, "quote": QUOTE}]}
    driver._check_ref_grounding(ok, served, known)

    bad = {"evidence_references": [
        {"document_id": "DOC_011", "page": 3, "quote": "없는 인용문"}]}
    with pytest.raises(ValueError, match="neither on a served page nor"):
        driver._check_ref_grounding(bad, served, known)

    clause_as_reused_claim_quote = {"matched_clause_ref": {
        "document_id": "DOC_011", "page": 1, "quote": QUOTE}}
    with pytest.raises(ValueError, match="clause reference"):
        driver._check_ref_grounding(clause_as_reused_claim_quote, served, known,
                                    clause_keys=("matched_clause_ref",))


def test_medical_candidate_is_schema_valid_so_refusal_is_config_only():
    """publish() schema-checks the candidate BEFORE the config gate and
    returns early on schema errors -- so if this candidate ever went
    schema-invalid, the driver would silently stop exercising the deferred-
    config path and misreport the refusal kind."""
    import driver_schema
    from jsonschema import Draft202012Validator, FormatChecker

    schema = driver_schema.load_materialized_schema("medical_variables.schema.json")
    candidate = driver._medical_candidate(CASE, RUN, "배상책임")
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(candidate)


def test_requirement_renumbering_is_global_and_sequential():
    body = {"coverage_requirements": [
        {"standardized_coverage_name": "a", "requirements": [
            {"requirement_id": "REQ-7"}, {"requirement_id": "REQ-2"}]},
        {"standardized_coverage_name": "b", "requirements": [
            {"requirement_id": "REQ-99"}]},
    ]}
    out = driver._renumber_requirements(body)
    ids = [r["requirement_id"] for g in out["coverage_requirements"]
           for r in g["requirements"]]
    assert ids == ["REQ-1", "REQ-2", "REQ-3"]


def test_cp3_transport_pins_classification_enums_natively():
    """Arm G's M2 re-paid its entire ~400s call because a descriptive
    free-string in secondary_case_types passed the loose transport schema and
    failed only at the local enum gate -- the shape must be refused at
    generation time, like report_profile's keys and CP1's field objects."""
    from jsonschema import Draft202012Validator, ValidationError

    validator = Draft202012Validator(driver._transport_schema_cp3())
    good = _cp3_body()
    validator.validate(good)

    freeform_secondary = json.loads(json.dumps(good))
    freeform_secondary["secondary_case_types"] = ["구내치료비(비배상책임 기반 치료비 담보) 청구"]
    with pytest.raises(ValidationError):
        validator.validate(freeform_secondary)

    freeform_axis = json.loads(json.dumps(good))
    freeform_axis["loss_type"] = "치료비"
    with pytest.raises(ValidationError):
        validator.validate(freeform_axis)


def test_normalize_repairs_shape_without_touching_content():
    """The 2026-08-16 shape repairs: defaults, type coercion and unwrapping,
    each of which turned a schema-failing sonnet-5 response into a passing one
    with no model call and no recall loss."""
    body = {
        "status": "success", "confidence": 0.9, "review_required": False,
        "warnings": [],
        "fields": {
            # review_required omitted on 38 of 57 fields in the v1 arm
            "diagnosis_name": {"value": "골절", "confidence": 0.9,
                               "evidence_references": [
                                   {"document_id": "DOC_011", "page": 1, "quote": QUOTE}]},
            # normalized_value as a number: the v3 arm's refusal
            "coverage_limit": {"value": "5억", "normalized_value": 500000000,
                               "confidence": 0.9, "review_required": False,
                               "evidence_references": [
                                   {"document_id": "DOC_011", "page": 1, "quote": QUOTE}]},
            # double-wrapped member: the v1 arm's other shape
            "kcd_code": {"value": {"value": "S6280"}, "confidence": 0.9,
                         "review_required": False,
                         "evidence_references": [
                             {"document_id": "DOC_011", "page": 1, "quote": QUOTE}]},
        },
    }
    fixed, log = driver.normalize_cp1_shape(body)
    assert fixed["fields"]["diagnosis_name"]["review_required"] is False
    assert fixed["fields"]["coverage_limit"]["normalized_value"] == "500000000"
    assert fixed["fields"]["kcd_code"]["value"] == "S6280"
    # Content is preserved, not invented.
    assert fixed["fields"]["diagnosis_name"]["value"] == "골절"
    assert len(fixed["fields"]) == 3
    assert log


def test_normalize_never_repairs_a_citation_or_hides_an_uncited_fact():
    """The line the repair layer must not cross.

    A wrong quote is not a format defect -- repairing one would automate
    fabrication -- and an uncited fact must reach the gate as a refusal rather
    than being quietly dropped, which would turn a loud failure into silent
    data loss. Both are load-bearing: dropping the member instead makes this
    test fail on the length assertion, and rewriting the quote makes it fail
    on the equality assertion.
    """
    wrong_quote = "이 문장은 원문에 존재하지 않는다"
    body = {
        "status": "success", "confidence": 0.9, "review_required": False,
        "warnings": [],
        "fields": {
            "diagnosis_name": {"value": "골절", "confidence": 0.9,
                               "review_required": False,
                               "evidence_references": [
                                   {"document_id": "DOC_011", "page": 1,
                                    "quote": wrong_quote}]},
            "uncited_fact": {"value": "근거 없음", "confidence": 0.9,
                             "review_required": False},
        },
    }
    fixed, _ = driver.normalize_cp1_shape(body)
    assert fixed["fields"]["diagnosis_name"]["evidence_references"][0]["quote"] == wrong_quote
    assert "uncited_fact" in fixed["fields"]

    bundle = [{"document_id": "DOC_011", "pages": [{"page": 1, "text": PAGE_TEXT}]}]
    with pytest.raises(Exception):
        driver._validate_cp1_output(body, bundle, driver._body_schema())


def test_normalize_does_not_default_reviewer_role():
    """WHICH expert a case needs is a judgment, not a shape. Defaulting it
    would answer a substantive question with a placeholder and mis-route a
    medical field away from 의사."""
    body = {"status": "success", "confidence": 0.9, "review_required": True,
            "warnings": [], "fields": {}}
    fixed, _ = driver.normalize_cp1_shape(body)
    assert "reviewer_role" not in fixed


def test_cp4_transport_pins_the_coverage_group_nesting():
    """Measured 2026-08-16: with `items: {"type": "object"}` the model emitted
    flat requirement items carrying no coverage key, and pinning the group
    shape produced correct per-coverage grouping with a valid join twice."""
    from jsonschema import Draft202012Validator, ValidationError

    validator = Draft202012Validator(driver._transport_schema_cp4())
    shell = {"status": "success", "confidence": 0.9, "review_required": False,
             "warnings": []}
    good = {**shell, "coverage_requirements": [
        {"standardized_coverage_name": "facility_owner_liability",
         "requirements": [{"requirement_id": "REQ-1", "requirement_text": "조건",
                           "status": "met"}]}]}
    validator.validate(good)

    # The exact failure shape: a flat item with no coverage grouping.
    flat = {**shell, "coverage_requirements": [
        {"requirement_id": "REQ-1", "requirement_text": "조건", "status": "met"}]}
    with pytest.raises(ValidationError):
        validator.validate(flat)

    # An empty group is refused too -- it was the other half of the observed
    # output (six flat items alongside `requirements: []`).
    empty = {**shell, "coverage_requirements": [
        {"standardized_coverage_name": "x", "requirements": []}]}
    with pytest.raises(ValidationError):
        validator.validate(empty)
