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


def test_m2_transport_enforces_compact_requirement_evidence():
    """P1 evidence remains required, but M2 must not repeat full narratives."""
    from jsonschema import Draft202012Validator, ValidationError

    validator = Draft202012Validator(driver._transport_schema_cp4())
    shell = {"status": "success", "confidence": 0.9, "review_required": False,
             "warnings": []}
    requirement = {
        "requirement_id": "REQ-1", "requirement_text": "보험사고 요건",
        "status": "met", "evidence_references": [{
            "document_id": "DOC_001", "page": 1, "quote": "사고 경위"
        }],
    }
    body = {**shell, "coverage_requirements": [{
        "standardized_coverage_name": "facility_owner_liability",
        "requirements": [requirement],
    }]}
    validator.validate(body)

    verbose = json.loads(json.dumps(body))
    verbose["coverage_requirements"][0]["requirements"][0]["requirement_text"] = "가" * 241
    with pytest.raises(ValidationError):
        validator.validate(verbose)

    repeated = json.loads(json.dumps(body))
    repeated["coverage_requirements"][0]["requirements"][0]["evidence_references"] *= 3
    with pytest.raises(ValidationError):
        validator.validate(repeated)

    too_many = json.loads(json.dumps(body))
    too_many["coverage_requirements"][0]["requirements"] *= 13
    with pytest.raises(ValidationError):
        validator.validate(too_many)


def test_m2_prompt_instructs_compact_no_policy_output():
    prompt = driver._m2_prompt("{}", "", None, [])
    assert "COMPACT OUTPUT IS REQUIRED" in prompt
    assert "do not invent generic policy conditions" in prompt


def test_cp1_corrects_an_unambiguous_citation_page_and_records_it():
    """Measured 2026-08-17: the model cited DOC_019's N1611 row on page 1
    while it sits on page 2. Where the page is knowably right, the driver
    fixes it rather than spending a ~90s correction round -- but it must
    leave a trace, or the signal that the model cited the wrong page is
    erased."""
    bundle = [{"document_id": "DOC_019", "pages": [
        {"page": 1, "text": "진찰료 초진료 18,520"},
        {"page": 2, "text": "사지골절정복술 582,738"}]}]
    body = {"status": "success", "confidence": 0.9, "review_required": False,
            "warnings": ["기존 경고"],
            "fields": {"surgery_name": {
                "value": "수술", "confidence": 0.9, "review_required": False,
                "evidence_references": [{"document_id": "DOC_019", "page": 1,
                                         "quote": "사지골절정복술"}]}}}
    out = driver._validate_cp1_output(body, bundle, driver._body_schema())
    assert out["fields"]["surgery_name"]["evidence_references"][0]["page"] == 2
    assert "기존 경고" in out["warnings"]
    assert any("page corrected" in w for w in out["warnings"])


def test_cp1_still_refuses_an_ambiguous_or_absent_citation():
    """The correction must not become 'page numbers are advisory'."""
    bundle = [{"document_id": "DOC_019", "pages": [
        {"page": 1, "text": "수술 2023-12-05"},
        {"page": 2, "text": "검사 2023-12-05"},
        {"page": 3, "text": "합계"}]}]

    def body(quote, page):
        return {"status": "success", "confidence": 0.9, "review_required": False,
                "warnings": [],
                "fields": {"surgery_date": {
                    "value": "2023-12-05", "confidence": 0.9, "review_required": False,
                    "evidence_references": [{"document_id": "DOC_019", "page": page,
                                             "quote": quote}]}}}

    # On two pages: which one the model meant is not knowable.
    with pytest.raises(ValueError, match="not present on page"):
        driver._validate_cp1_output(body("2023-12-05", 3), bundle, driver._body_schema())
    # Nowhere: the fabrication case.
    with pytest.raises(ValueError, match="not present on page"):
        driver._validate_cp1_output(body("음주 상태", 1), bundle, driver._body_schema())


def test_normalize_drops_a_null_normalized_value():
    """The public schema declares `normalized_value` a bare string and does
    not require the key, so ABSENT is how "none" is said and null is invalid.
    A CASE_039 run emitted null and the gate refused it; dropping the key is
    the honest repair, since inventing a string would be fabrication."""
    body = {"status": "success", "confidence": 0.9, "review_required": False,
            "warnings": [],
            "fields": {"claim_item": {
                "value": "배상책임", "normalized_value": None, "confidence": 0.9,
                "review_required": False,
                "evidence_references": [
                    {"document_id": "DOC_011", "page": 1, "quote": QUOTE}]}}}
    fixed, log = driver.normalize_cp1_shape(body)
    assert "normalized_value" not in fixed["fields"]["claim_item"]
    assert fixed["fields"]["claim_item"]["value"] == "배상책임"
    assert any("normalized_value" in entry for entry in log)


def test_cp1_transport_refuses_a_null_normalized_value():
    """Blocked at generation time too, or the model keeps emitting it."""
    from jsonschema import Draft202012Validator, ValidationError

    validator = Draft202012Validator(driver._transport_schema())
    body = {"status": "success", "confidence": 0.9, "review_required": False,
            "warnings": [],
            "fields": {"claim_item": {
                "value": "x", "normalized_value": None, "confidence": 0.9,
                "review_required": False,
                "evidence_references": [
                    {"document_id": "DOC_011", "page": 1, "quote": QUOTE}]}}}
    with pytest.raises(ValidationError):
        validator.validate(body)


def test_policy_documents_selects_only_insurance_policy_entries():
    """The permitted set for a clause reference. Read from the manifest, not
    the document index: the index is derived and optional, while
    `document_type` is what the DAO's canonical-UID gate ultimately judges."""
    manifest = {"documents": [
        _document("DOC_009", "insurance_policy"),
        _document("DOC_010", "insurance_policy"),
        _document("DOC_022", "other"),            # 위자료 산정기준표 (CASE_040)
        _document("DOC_011", "diagnosis_certificate"),
    ]}
    assert driver.policy_documents(manifest) == ["DOC_009", "DOC_010"]


def test_clause_ref_transport_pins_document_id_to_the_policy_set():
    """Generation-time half. CASE_040 grounded a requirement in DOC_022 and
    the DAO refused the write after the call was already paid for."""
    from jsonschema import Draft202012Validator, ValidationError

    validator = Draft202012Validator(driver._transport_schema_cp4(["DOC_009", "DOC_010"]))
    shell = {"status": "success", "confidence": 0.9, "review_required": False,
             "warnings": []}

    def body(doc_id):
        return {**shell, "coverage_requirements": [{
            "standardized_coverage_name": "facility_owner_liability",
            "requirements": [{"requirement_id": "REQ-1", "requirement_text": "조건",
                              "status": "met",
                              "clause_ref": {"document_id": doc_id, "page": 2,
                                             "quote": "보상합니다"}}]}]}

    validator.validate(body("DOC_009"))
    with pytest.raises(ValidationError):
        validator.validate(body("DOC_022"))

    # A requirement resting on no located clause stays expressible -- by
    # OMITTING the key. `"type": ["object", "null"]` would be a union, which
    # ajv strict mode refuses (see test_no_transport_schema_declares_a_union_type).
    omitted = json.loads(json.dumps(body("DOC_009")))
    del omitted["coverage_requirements"][0]["requirements"][0]["clause_ref"]
    validator.validate(omitted)

    # With no policy set known, the shape must not forbid every clause ref.
    permissive = Draft202012Validator(driver._transport_schema_cp4())
    permissive.validate(body("DOC_022"))


def test_validate_m2_refuses_a_clause_ref_outside_the_policy_set():
    """The gate half: the transport enum is a hint the model can still miss,
    and this is where the violation costs a correction round rather than a
    refused DAO write."""
    cov = {"coverages": [{"standardized_coverage_name": "x",
                          "matched_clause_ref": {"document_id": "DOC_022",
                                                 "page": 1, "quote": "위자료"}}]}
    req = {"coverage_requirements": []}
    with pytest.raises(ValueError, match="not one of this case's policy documents"):
        driver._check_clause_documents(cov, req, ["DOC_009", "DOC_010"])

    ok = {"coverages": [{"matched_clause_ref": {"document_id": "DOC_009",
                                                "page": 1, "quote": "보상"}}]}
    driver._check_clause_documents(ok, req, ["DOC_009", "DOC_010"])
    # Unknown policy set: refusing every clause reference would be worse.
    driver._check_clause_documents(cov, req, None)


def test_no_transport_schema_declares_a_union_type():
    """claude-cli validates --json-schema with ajv in STRICT mode, which
    refuses `"type": ["string", "null"]` outright ("use allowUnionTypes").

    A CASE_042 run died on exactly that after 411.9s -- AFTER the call was
    paid for -- and earlier runs survived only because those cases happened
    not to trip the check, which makes this the kind of defect a test has to
    hold rather than a run. Nullability and type are enforced by the public
    body schemas, which no transport replaces.
    """
    def unions(node, path=""):
        found = []
        if isinstance(node, dict):
            if isinstance(node.get("type"), list):
                found.append(path or "/")
            for key, child in node.items():
                found += unions(child, f"{path}/{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                found += unions(child, f"{path}/{index}")
        return found

    for name, schema in [
        ("cp1", driver._transport_schema()),
        ("m1", driver._transport_schema_m1()),
        ("cp2", driver._transport_schema_cp2(["DOC_009"])),
        ("cp3", driver._transport_schema_cp3()),
        ("cp4", driver._transport_schema_cp4(["DOC_009"])),
        ("m2", driver._transport_schema_m2(["DOC_009"])),
    ]:
        assert unions(schema) == [], f"{name} transport declares a union type"


def test_prompt_tells_the_model_how_to_encode_a_partially_known_date():
    """A date the document states only to the month has a legal encoding, and
    the prompt has to name it. CASE_050 (2026-08-18) returned
    `onset_date: "2025-10"`; the schema's date_field admits only a complete
    YYYY-MM-DD or null, so the M1 call was rejected on validation after its
    290.9s had already been spent. The PERIOD slot already carried this rule
    (arm H's all-null period finding) -- the DATE slot did not.
    """
    prompt = driver._prompt([{
        "document_id": "DOC_001", "document_type": "diagnosis_certificate",
        "pages": [{"page": 1, "text": "발병일 2025년 10월경"}],
    }])
    # The rule names the failing shape, the legal alternative, and the warning
    # channel -- a rule that says only "use null" loses the partial reading.
    assert "2025-10" in prompt, "the refused shape is not shown"
    assert "YYYY-MM-DD" in prompt
    assert "warnings" in prompt
    # And it forbids the other tempting repair: inventing a day.
    assert "2025-10-01" in prompt, "padding a partial date is not forbidden"


def test_date_and_period_partial_rules_are_both_present_and_distinct():
    """Two different shapes with two different repairs: a date nulls out and
    warns, a period is OMITTED entirely (its start_date is required). Collapsing
    them into one instruction is how one of the two regressed before."""
    prompt = driver._prompt([{
        "document_id": "DOC_001", "document_type": "medical_record",
        "pages": [{"page": 1, "text": "치료기간 약 8주"}],
    }])
    assert "DATE slot" in prompt and "PERIOD slot" in prompt
    assert "OMIT the field entirely" in prompt


# --- billing line items are folded out of the prompt, never out of evidence --

_RECEIPT_PAGE = "\n".join([
    "( 외래 ) 진료비 계산서 · 영수증",
    "환자등록번호 | 환자 성명 | 진료기간",
    "01.진찰료 | 2025-10-16 | AA156 | 초재진찰료-종합병원 | 19,100 | 1 | 1 | 1 | 19,100",
    "02.투약료 | 2025-10-16 | AB201 | 아목시실린 | 1,091 | 1 | 1 | 1 | 1,091",
    "합계 | ① 15,388 | ② 12,753 | ③ 0 | ④ 6,451",
    "⑥ 진료비 총액 (①+②+③+④) | 34,592",
    "⑬ 납부할 금액 ⑧-(⑨+⑩+⑪)+⑫ | 21,750",
    "※ 상급종합병원 : 2인실 50%, 3인실 40% / 병원급 의료기관 입원료 : 2인실 40%",
    "3. 상한액 초과금 : 「국민건강보험법 시행령」 별표 3 제1호에 따른 본인부담상한액",
])


def _billing_bundle():
    return [{"document_id": "DOC_044", "document_type": "receipt",
             "pages": [{"page": 1, "text": _RECEIPT_PAGE}]}]


def test_folding_keeps_every_total_and_drops_the_line_items():
    """The H-group consumes totals; a per-procedure row fills no slot.

    Measured on CASE_047 (2026-08-18): receipts were 241,082 of M1's 258,047
    input characters, against 46,000-53,000 for every case that has ever
    completed this stage. That run took 666.1s and failed.
    """
    folded, report = driver._fold_billing_documents(_billing_bundle())
    text = folded[0]["pages"][0]["text"]

    for total in ("합계 | ① 15,388", "⑥ 진료비 총액", "34,592",
                  "⑬ 납부할 금액", "21,750"):
        assert total in text, f"a figure the stage consumes was dropped: {total}"
    for item in ("AA156", "초재진찰료", "AB201", "아목시실린"):
        assert item not in text, f"a line item survived the fold: {item}"
    assert report["after"] < report["before"]
    assert report["documents"]["DOC_044"]["rows_omitted"] >= 2


def test_the_statutory_notice_is_dropped_even_though_it_names_kept_words():
    """The notice quotes the very words the keep list looks for (진료비,
    본인부담상한액), so it has to be tested BEFORE the keep test -- otherwise
    30K characters of 국민건강보험법 text ride along on every receipt."""
    folded, _ = driver._fold_billing_documents(_billing_bundle())
    text = folded[0]["pages"][0]["text"]
    assert "국민건강보험법" not in text
    assert "상급종합병원 : 2인실" not in text


def test_a_non_billing_document_is_returned_untouched():
    """Folding is scoped to billing types; a 진단서 keeps every line."""
    bundle = [{"document_id": "DOC_003", "document_type": "diagnosis_certificate",
               "pages": [{"page": 1, "text": "진 단 서\n01.병명 | 공황 장애 | F410"}]}]
    folded, report = driver._fold_billing_documents(bundle)
    assert folded[0]["pages"][0]["text"] == bundle[0]["pages"][0]["text"]
    assert report["documents"] == {}


def test_kept_lines_stay_verbatim_so_a_quote_still_verifies():
    """Kept lines are copied, not rewritten -- an evidence quote taken from one
    must still match the FULL document the validator checks against."""
    original = _billing_bundle()[0]["pages"][0]["text"]
    folded, _ = driver._fold_billing_documents(_billing_bundle())
    for line in folded[0]["pages"][0]["text"].splitlines():
        if line.startswith("[..."):
            continue
        if line.strip():
            assert line in original, f"fold rewrote a line: {line!r}"


# --- a D5 no-policy case selects no pages, and that is not an error ---------

def test_a_case_with_no_policy_index_selects_no_pages_instead_of_failing():
    """D5 (`dao.py declare-no-policy-documents`) lets a case with no 약관 pass
    `policy_clause_processing`, so claim_analysis must accept the same case.

    Measured on CASE_047 (2026-08-18): 52 documents, zero insurance_policy, an
    empty `_document_index.json`. M1 produced good fields in 397.5s and then
    died on "selection returned no readable pages" -- the whole call wasted on
    a state the DAO had already cleared.
    """
    assert driver._clamp_selection([], {}) == {"pages": []}
    # Even if the model names pages, there is no index to clamp them against.
    assert driver._clamp_selection(
        [{"document_id": "DOC_009", "page": 3}], {}) == {"pages": []}


def test_an_empty_selection_still_fails_when_the_case_has_policy_pages():
    """The waiver is narrow: with policy pages available, an empty selection
    means the model returned nothing usable, which is a real defect."""
    with pytest.raises(ValueError, match="no readable pages"):
        driver._clamp_selection([], {"DOC_009": {1, 2, 3}})
    with pytest.raises(ValueError, match="no readable pages"):
        driver._clamp_selection(
            [{"document_id": "DOC_009", "page": 99}], {"DOC_009": {1, 2, 3}})


def test_m2_transport_requires_at_least_one_evidence_reference():
    """Compaction must not compress evidence to zero.

    Every public contract requires >= 1 reference (coverage_result
    `$defs/coverage`, case_type_result `allOf`, requirement_matching_result
    `$defs/requirement/allOf/then`). A transport bounding only the TOP of the
    range lets the model return `[]`, satisfy the transport, and fail the DAO
    write after BOTH M1 and M2 are paid for -- what happened on CASE_305, where
    the compaction prompt pushed CP3 to emit an empty array and the resulting
    ValidationError killed the driver mid-run.

    Reintroducing the defect (dropping `minItems` from
    `_compact_evidence_references`) makes all three `pytest.raises` blocks fail,
    so this asserts the constraint rather than restating the schema.
    """
    from jsonschema import Draft202012Validator, ValidationError

    ref = {"document_id": "DOC_001", "page": 1, "quote": "사고 경위"}
    shell = {"status": "success", "confidence": 0.9, "review_required": False,
             "warnings": []}

    # CP3 -- evidence_references at the section's top level.
    v3 = Draft202012Validator(driver._transport_schema_cp3())
    ct = {**shell, "case_type": "후유장해", "case_type_source": "inferred",
          "template_id": "t1",
          "report_profile": {"format_contract_version": "1", "family": "f",
                             "claim_mechanism": "m", "mode": "mo",
                             "support_status": "s"},
          "evidence_references": [ref]}
    v3.validate(ct)
    with pytest.raises(ValidationError):
        v3.validate({**ct, "evidence_references": []})

    # CP2 -- per coverage.
    v2 = Draft202012Validator(driver._transport_schema_cp2([]))
    cov = {**shell, "coverages": [{"coverage_name": "상해후유장해",
                                   "standardized_coverage_name": "disability",
                                   "evidence_references": [ref]}]}
    v2.validate(cov)
    empty2 = json.loads(json.dumps(cov))
    empty2["coverages"][0]["evidence_references"] = []
    with pytest.raises(ValidationError):
        v2.validate(empty2)

    # CP4 -- per requirement.
    v4 = Draft202012Validator(driver._transport_schema_cp4())
    req = {**shell, "coverage_requirements": [{
        "standardized_coverage_name": "disability",
        "requirements": [{"requirement_id": "REQ-1",
                          "requirement_text": "보험사고 요건",
                          "status": "met",
                          "evidence_references": [ref]}]}]}
    v4.validate(req)
    empty4 = json.loads(json.dumps(req))
    empty4["coverage_requirements"][0]["requirements"][0]["evidence_references"] = []
    with pytest.raises(ValidationError):
        v4.validate(empty4)
