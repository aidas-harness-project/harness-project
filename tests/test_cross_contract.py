"""Cross-contract invariants (tools/_cross_contract.py) and their DAO gate.

Every attack below validated CLEANLY against the real committed outputs
before this layer existed -- they are reproductions of an audit's findings,
not hypotheticals:

  * denial_validation_result.json naming `DR_999`, a reason id that exists
    nowhere, passed schema validation.
  * So did dropping validations for real reasons, and duplicating one.
  * So did two denial reasons both calling themselves `DR_1`.
  * So did `candidate_codes` whose top entry disagreed with the assigned
    `taxonomy_code` -- the Top-1 evaluation input describing a classification
    nobody made.

None of these are expressible in JSON Schema: they compare a document to a
sibling file, or one array member to another. The fixtures are built to the
real contracts' shapes so a schema revision that breaks them shows up here.
"""
import copy
import json
from pathlib import Path

import pytest

import dao
import _cross_contract as cc
from _cross_contract import check

ROOT = Path(__file__).resolve().parent.parent


def _reason(reason_id, code="R04", label="약관상 지급요건 미충족", matches=None):
    return {
        "reason_id": reason_id,
        "decision_type": "denial",
        "payment_status": "unpaid",
        "taxonomy_code": code,
        "taxonomy_label": label,
        "candidate_codes": [{"taxonomy_code": code, "confidence": 0.9}],
        "policy_matches": matches if matches is not None else [],
    }


@pytest.fixture
def reasons_doc():
    return {"denial_reasons": [_reason("DR_1"), _reason("DR_2")]}


@pytest.fixture
def case_dir(tmp_path, reasons_doc):
    """A case directory holding a real denial_reason_result.json, so the
    validation contract has something to resolve its ids against."""
    d = tmp_path / "CASE_TEST"
    d.mkdir()
    (d / "denial_reason_result.json").write_text(
        json.dumps(reasons_doc, ensure_ascii=False), encoding="utf-8")
    return d


def _validation(reason_id, match_ids=()):
    return {
        "reason_id": reason_id,
        "verdict": "partially_supported",
        "policy_match_validations": [{"policy_match_id": m} for m in match_ids],
    }


def _derived(case_dir, payload):
    """Stamp the payload with the hash of the denial_reason_result.json in
    this case dir. Downstream contracts must declare what they were built
    from (see tests/test_upstream_staleness.py); these tests are about ids and
    decision types, so the provenance field is filled in correctly rather than
    re-asserted here."""
    upstream = json.loads((case_dir / "denial_reason_result.json").read_text(encoding="utf-8"))
    return dict(payload, source_denial_contract_hash=cc.upstream_hash(upstream))


# ---- denial_reason_result: ids and ranked lists ----

def test_clean_denial_reasons_pass(reasons_doc, case_dir):
    assert check("denial_reason_result.json", reasons_doc, case_dir) == []


def test_duplicate_reason_id_is_rejected(reasons_doc, case_dir):
    """Ids are how every downstream stage addresses a reason; two rows sharing
    one means the later silently wins."""
    reasons_doc["denial_reasons"].append(_reason("DR_1"))
    errors = check("denial_reason_result.json", reasons_doc, case_dir)
    assert any("duplicate reason_id 'DR_1'" in e for e in errors)


def test_duplicate_policy_match_id_is_rejected_across_reasons(reasons_doc, case_dir):
    """Uniqueness is contract-wide, not per-reason: denial_validation
    addresses matches by bare id with no reason qualifier."""
    reasons_doc["denial_reasons"][0]["policy_matches"] = [{"policy_match_id": "PM_1"}]
    reasons_doc["denial_reasons"][1]["policy_matches"] = [{"policy_match_id": "PM_1"}]
    errors = check("denial_reason_result.json", reasons_doc, case_dir)
    assert any("duplicate policy_match_id 'PM_1'" in e for e in errors)


def test_top_candidate_must_equal_assigned_code(reasons_doc, case_dir):
    """candidate_codes feeds Top-1/Top-3 evaluation. A list whose winner is not
    the assigned code scores a classification that was never made."""
    reasons_doc["denial_reasons"][0]["candidate_codes"] = [
        {"taxonomy_code": "R21", "confidence": 0.9},
        {"taxonomy_code": "R04", "confidence": 0.1},
    ]
    errors = check("denial_reason_result.json", reasons_doc, case_dir)
    assert any("candidate_codes[0] is 'R21' but taxonomy_code is 'R04'" in e for e in errors)


def test_candidate_codes_must_not_repeat_a_code(reasons_doc, case_dir):
    reasons_doc["denial_reasons"][0]["candidate_codes"] = [
        {"taxonomy_code": "R04", "confidence": 0.9},
        {"taxonomy_code": "R04", "confidence": 0.1},
    ]
    errors = check("denial_reason_result.json", reasons_doc, case_dir)
    assert any("lists 'R04' more than once" in e for e in errors)


def test_candidate_confidences_must_not_increase(reasons_doc, case_dir):
    """A 'ranked' list that is not ranked makes Top-1 meaningless."""
    reasons_doc["denial_reasons"][0]["candidate_codes"] = [
        {"taxonomy_code": "R04", "confidence": 0.4},
        {"taxonomy_code": "R05", "confidence": 0.9},
    ]
    errors = check("denial_reason_result.json", reasons_doc, case_dir)
    assert any("not non-increasing" in e for e in errors)


def test_equal_confidences_are_allowed(reasons_doc, case_dir):
    """Non-increasing, not strictly decreasing -- a genuine tie is honest."""
    reasons_doc["denial_reasons"][0]["candidate_codes"] = [
        {"taxonomy_code": "R04", "confidence": 0.5},
        {"taxonomy_code": "R05", "confidence": 0.5},
    ]
    assert check("denial_reason_result.json", reasons_doc, case_dir) == []


def test_taxonomy_label_must_match_the_codebook(reasons_doc, case_dir):
    """The codebook in common_component_output is the one machine-readable
    source of R-code labels; a contract must not invent a second one."""
    reasons_doc["denial_reasons"][0]["taxonomy_label"] = "그럴듯한 오답"
    errors = check("denial_reason_result.json", reasons_doc, case_dir)
    assert any("does not match the codebook label" in e for e in errors)


def test_omitted_taxonomy_label_is_not_invented(reasons_doc, case_dir):
    """Absent is not wrong -- the schema decides whether the field is required;
    this layer only checks a present label for agreement."""
    reasons_doc["denial_reasons"][0].pop("taxonomy_label")
    assert check("denial_reason_result.json", reasons_doc, case_dir) == []


# ---- denial_validation_result: the orphan-id findings ----

def test_validation_of_a_nonexistent_reason_is_rejected(case_dir):
    """The headline finding: DR_999 resolves to nothing and used to pass."""
    doc = {"validations": [_validation("DR_1"), _validation("DR_999")]}
    errors = check("denial_validation_result.json", _derived(case_dir, doc), case_dir)
    assert any("DR_999" in e and "does not exist" in e for e in errors)


def test_every_denial_reason_must_be_validated(case_dir):
    """Skipping one silently leaves an insurer's denial unrebutted while the
    contract still reports success."""
    doc = {"validations": [_validation("DR_1")]}
    errors = check("denial_validation_result.json", _derived(case_dir, doc), case_dir)
    assert any("DR_2" in e and "no validation" in e for e in errors)


def test_a_reason_must_not_be_validated_twice(case_dir):
    doc = {"validations": [_validation("DR_1"), _validation("DR_1"), _validation("DR_2")]}
    errors = check("denial_validation_result.json", _derived(case_dir, doc), case_dir)
    assert any("appears more than once" in e for e in errors)


def test_exact_one_to_one_validation_passes(case_dir):
    doc = {"validations": [_validation("DR_1"), _validation("DR_2")]}
    assert check("denial_validation_result.json", _derived(case_dir, doc), case_dir) == []


def test_policy_match_verification_must_resolve(tmp_path):
    reasons = {"denial_reasons": [_reason("DR_1", matches=[{"policy_match_id": "PM_1"}])]}
    d = tmp_path / "CASE_PM"
    d.mkdir()
    (d / "denial_reason_result.json").write_text(json.dumps(reasons, ensure_ascii=False),
                                                 encoding="utf-8")
    doc = {"validations": [_validation("DR_1", match_ids=["PM_999"])]}
    errors = check("denial_validation_result.json", _derived(d, doc), d)
    assert any("PM_999" in e and "does not exist" in e for e in errors)
    assert any("PM_1" in e and "no verification" in e for e in errors)


def test_legacy_policy_matches_without_ids_are_not_retro_failed(tmp_path):
    """CASE_021 was written before policy_match_id existed. Those entries have
    no id to address, so demanding a verification for them would report a
    legacy shape as corruption -- the schema governs new writes instead."""
    reasons = {"denial_reasons": [
        _reason("DR_1", matches=[{"document_id": "DOC_002", "clause_id": "제3조"}])]}
    d = tmp_path / "CASE_LEGACY"
    d.mkdir()
    (d / "denial_reason_result.json").write_text(json.dumps(reasons, ensure_ascii=False),
                                                 encoding="utf-8")
    assert check("denial_validation_result.json",
                 _derived(d, {"validations": [_validation("DR_1")]}), d) == []


def test_validation_before_its_source_contract_exists_is_rejected(tmp_path):
    """Writing Phase 2's validation with no Phase 1 reasons to validate means
    every id in it is unresolvable by definition."""
    d = tmp_path / "CASE_EMPTY"
    d.mkdir()
    errors = check("denial_validation_result.json", {"validations": [_validation("DR_1")]}, d)
    assert any("cannot be written before" in e for e in errors)


# ---- policy match ownership: a match belongs to ONE reason ----

@pytest.fixture
def owned_matches_case(tmp_path):
    """DR_1 owns PM_1, DR_2 owns PM_2 -- the setup a flat existence check
    cannot tell apart from 'both ids exist somewhere'."""
    reasons = {"denial_reasons": [
        _reason("DR_1", matches=[{"policy_match_id": "PM_1"}]),
        _reason("DR_2", matches=[{"policy_match_id": "PM_2"}]),
    ]}
    d = tmp_path / "CASE_OWN"
    d.mkdir()
    (d / "denial_reason_result.json").write_text(json.dumps(reasons, ensure_ascii=False),
                                                 encoding="utf-8")
    return d


def test_match_verified_under_the_wrong_reason_is_rejected(owned_matches_case):
    """The finding: PM_1 belongs to DR_1, but was verified under DR_2. Every
    id exists, so a global-set check passes -- while PM_1 was never actually
    checked under the reason that owns it, and PM_2 never checked at all."""
    doc = {"validations": [
        _validation("DR_1"),
        _validation("DR_2", match_ids=["PM_1"]),
    ]}
    errors = check("denial_validation_result.json", _derived(owned_matches_case, doc), owned_matches_case)
    assert any("PM_1" in e and "belongs to 'DR_1', not 'DR_2'" in e for e in errors)


def test_correct_ownership_passes(owned_matches_case):
    doc = {"validations": [
        _validation("DR_1", match_ids=["PM_1"]),
        _validation("DR_2", match_ids=["PM_2"]),
    ]}
    assert check("denial_validation_result.json", _derived(owned_matches_case, doc), owned_matches_case) == []


def test_owned_match_left_unverified_is_reported_against_its_reason(owned_matches_case):
    doc = {"validations": [_validation("DR_1"), _validation("DR_2", match_ids=["PM_2"])]}
    errors = check("denial_validation_result.json", _derived(owned_matches_case, doc), owned_matches_case)
    assert any("DR_1" in e and "PM_1" in e and "no verification" in e for e in errors)


def test_same_match_verified_under_two_reasons_is_rejected(owned_matches_case):
    """Wrong-parent and duplicate at once -- both must surface."""
    doc = {"validations": [
        _validation("DR_1", match_ids=["PM_1"]),
        _validation("DR_2", match_ids=["PM_1", "PM_2"]),
    ]}
    errors = check("denial_validation_result.json", _derived(owned_matches_case, doc), owned_matches_case)
    assert any("belongs to 'DR_1', not 'DR_2'" in e for e in errors)
    assert any("verified more than once" in e for e in errors)


def test_match_repeated_within_one_validation_is_rejected(owned_matches_case):
    doc = {"validations": [
        _validation("DR_1", match_ids=["PM_1", "PM_1"]),
        _validation("DR_2", match_ids=["PM_2"]),
    ]}
    errors = check("denial_validation_result.json", _derived(owned_matches_case, doc), owned_matches_case)
    assert any("verified more than once" in e for e in errors)


# ---- policy documents, clauses, and cited locations must be real ----

CLAUSE_QUOTE = "「뇌혈관질환」의 진단확정은 의료법 제3조에서 규정한 의료기관의 의사에 의하여"


def _policy_doc(document_id="DOC_002", clause_id="제3조", page=2, quote=CLAUSE_QUOTE):
    return {
        "document_id": document_id,
        "clauses": [{
            "clause_id": clause_id,
            "evidence_references": [
                {"document_id": document_id, "page": page, "quote": quote}],
        }],
    }


def _match(policy_match_id="PM_1", document_id="DOC_002", clause_id="제3조",
           ref_document_id=None, page=2, quote=CLAUSE_QUOTE, match_source="insurer_cited"):
    return {
        "policy_match_id": policy_match_id,
        "document_id": document_id,
        "clause_id": clause_id,
        "match_source": match_source,
        "policy_clause_evidence_references": [
            {"document_id": ref_document_id or document_id, "page": page, "quote": quote}],
    }


@pytest.fixture
def policy_case(tmp_path):
    d = tmp_path / "CASE_POLICY"
    d.mkdir()
    (d / "normalized_policy_clause_DOC_002.json").write_text(
        json.dumps(_policy_doc(), ensure_ascii=False), encoding="utf-8")
    return d


def _reasons_with(match):
    return {"denial_reasons": [_reason("DR_1", matches=[match])]}


def test_resolvable_policy_match_passes(policy_case):
    assert check("denial_reason_result.json", _reasons_with(_match()), policy_case) == []


def test_match_citing_a_different_document_is_rejected(policy_case):
    """A DOC_002 match whose clause evidence points at DOC_999: the citation
    does not come from the document the match claims."""
    doc = _reasons_with(_match(ref_document_id="DOC_999"))
    errors = check("denial_reason_result.json", doc, policy_case)
    assert any("DOC_999" in e and "DOC_002" in e for e in errors)


def test_match_on_a_nonexistent_clause_is_rejected(policy_case):
    doc = _reasons_with(_match(clause_id="제99조"))
    errors = check("denial_reason_result.json", doc, policy_case)
    assert any("제99조" in e and "does not exist" in e for e in errors)


def test_match_on_an_unnormalized_policy_document_is_rejected(policy_case):
    """Fail-safe: a link that cannot be checked is not allowed to stand as
    though it had been."""
    doc = _reasons_with(_match(document_id="DOC_777"))
    errors = check("denial_reason_result.json", doc, policy_case)
    assert any("normalized_policy_clause_DOC_777.json" in e for e in errors)


def test_clause_evidence_on_the_wrong_page_is_rejected(policy_case):
    doc = _reasons_with(_match(page=9))
    errors = check("denial_reason_result.json", doc, policy_case)
    assert any("does not match any evidence reference" in e for e in errors)


def test_clause_evidence_with_an_unrecorded_quote_is_rejected(policy_case):
    doc = _reasons_with(_match(quote="약관에 그렇게 적혀 있다고 함"))
    errors = check("denial_reason_result.json", doc, policy_case)
    assert any("does not match any evidence reference" in e for e in errors)


def test_agent_inferred_matches_are_verified_identically(policy_case):
    """An inferred link is the one most in need of checking, not least."""
    doc = _reasons_with(_match(clause_id="제99조", match_source="agent_inferred"))
    errors = check("denial_reason_result.json", doc, policy_case)
    assert any("제99조" in e and "does not exist" in e for e in errors)


def test_match_may_cite_a_condition_level_evidence_reference(tmp_path):
    """A match may turn on a specific payout condition rather than the clause
    header, so condition-level references count as recorded locations."""
    policy = _policy_doc()
    policy["clauses"][0]["payout_conditions"] = [
        {"text": "진단확정 요건", "evidence_references": [
            {"document_id": "DOC_002", "page": 3, "quote": "조건 본문"}]}]
    d = tmp_path / "CASE_COND"
    d.mkdir()
    (d / "normalized_policy_clause_DOC_002.json").write_text(
        json.dumps(policy, ensure_ascii=False), encoding="utf-8")
    doc = _reasons_with(_match(page=3, quote="조건 본문"))
    assert check("denial_reason_result.json", doc, d) == []


def test_legacy_matches_without_ids_are_still_skipped(policy_case):
    """CASE_021's shape must not be retro-failed by the new clause checks."""
    doc = {"denial_reasons": [_reason("DR_1", matches=[
        {"document_id": "DOC_999", "clause_id": "제3조", "relevance_note": "legacy"}])]}
    assert check("denial_reason_result.json", doc, policy_case) == []


# ---- screening_report ----

def test_screening_report_reason_ids_must_resolve(case_dir):
    doc = {"denial_summary": {"reason_ids": ["DR_1", "DR_7"]}}
    errors = check("screening_report.json", _derived(case_dir, doc), case_dir)
    assert any("DR_7" in e for e in errors)


def test_screening_report_with_valid_ids_passes(case_dir):
    doc = {"denial_summary": {"reason_ids": ["DR_1", "DR_2"]}}
    assert check("screening_report.json", _derived(case_dir, doc), case_dir) == []


@pytest.fixture
def mixed_decision_case(tmp_path):
    """DR_1 is a denial, DR_2 a reduction -- the distinction the screening
    report's two sections exist to preserve."""
    reasons = {"denial_reasons": [
        dict(_reason("DR_1", code="R04"), decision_type="denial"),
        dict(_reason("DR_2", code="R01"), decision_type="reduction"),
    ]}
    d = tmp_path / "CASE_MIXED"
    d.mkdir()
    (d / "denial_reason_result.json").write_text(json.dumps(reasons, ensure_ascii=False),
                                                 encoding="utf-8")
    return d


def _position(denial_ids, reduction_ids):
    return {"insurer_position": {
        "denial": {"reason_ids": list(denial_ids)},
        "reduction": {"reason_ids": list(reduction_ids)},
    }}


def test_correctly_split_screening_sections_pass(mixed_decision_case):
    assert check("screening_report.json",
                 _derived(mixed_decision_case, _position(["DR_1"], ["DR_2"])),
                 mixed_decision_case) == []


def test_swapped_denial_and_reduction_sections_are_rejected(mixed_decision_case):
    """The regression the audit asked for: both ids resolve, so an
    existence-only check passes while the report inverts what the insurer
    actually decided."""
    errors = check("screening_report.json", _derived(mixed_decision_case, _position(["DR_2"], ["DR_1"])), mixed_decision_case)
    assert any("DR_2" in e and "'reduction'" in e and "denial" in e for e in errors)
    assert any("DR_1" in e and "'denial'" in e and "reduction" in e for e in errors)


def test_a_reduction_listed_under_denial_is_rejected(mixed_decision_case):
    errors = check("screening_report.json", _derived(mixed_decision_case, _position(["DR_1", "DR_2"], [])), mixed_decision_case)
    assert any("DR_2" in e and "must not be summarized as a denial" in e for e in errors)


def test_same_reason_in_both_sections_is_rejected(mixed_decision_case):
    errors = check("screening_report.json", _position(["DR_1"], ["DR_1", "DR_2"]),
                   mixed_decision_case)
    assert any("BOTH denial and reduction" in e for e in errors)


def test_reason_repeated_within_one_section_is_rejected(mixed_decision_case):
    errors = check("screening_report.json", _position(["DR_1", "DR_1"], ["DR_2"]),
                   mixed_decision_case)
    assert any("more than once" in e for e in errors)


# ---- dispatch and the DAO gate ----

def test_unknown_contracts_are_not_gated(case_dir):
    """Additive layer: a contract with no registered invariants must not need
    to opt out."""
    assert check("ocr_result_DOC_001.json", {"anything": True}, case_dir) == []


def test_dao_write_contract_refuses_and_does_not_persist(isolated_dao, make_args,
                                                        tmp_path, capsys):
    """The gate that matters: a corrupt contract must be refused at the DAO,
    and the previous good file must survive untouched -- the same
    fail/don't-persist rule schema validation already follows.

    Built from the real CASE_903 output rather than a minimal fixture, so the
    payload is genuinely schema-valid and the ONLY thing wrong with it is the
    duplicate id. Otherwise schema validation would reject it first and this
    would never reach the cross-contract layer it means to test.
    """
    real = ROOT / "outputs" / "CASE_903" / "denial_reason_result.json"
    if not real.exists():
        pytest.skip("CASE_903 output not present in this checkout")
    reasons_doc = json.loads(real.read_text(encoding="utf-8"))

    case = dao.case_dir("CASE_009")
    case.mkdir(parents=True, exist_ok=True)
    good = json.dumps(reasons_doc, ensure_ascii=False)
    (case / "denial_reason_result.json").write_text(good, encoding="utf-8")

    corrupt = copy.deepcopy(reasons_doc)
    corrupt["denial_reasons"].append(copy.deepcopy(corrupt["denial_reasons"][0]))
    payload = tmp_path / "corrupt.json"
    payload.write_text(json.dumps(corrupt, ensure_ascii=False), encoding="utf-8")

    rc = dao.cmd_write_contract(make_args(
        filename="denial_reason_result.json", data_file=str(payload),
        schema_name="denial_reason_result.schema.json"))
    out = capsys.readouterr().out

    assert rc == 1
    assert "cross-contract validation errors" in out
    assert (case / "denial_reason_result.json").read_text(encoding="utf-8") == good
    assert not list(case.glob("*.lock")), "the lock must be released even when the write is refused"
