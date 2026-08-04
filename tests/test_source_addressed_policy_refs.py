"""Citing a policy document that was never normalized.

Normalization is opt-in (`text_only_no_normalization`), so most policy
documents have no clause contract. Every downstream policy reference used to
assume one existed, which quietly made normalization mandatory for anyone who
merely CITED a policy document -- `claim-analysis` found real clauses in a real
145-page 약관 and had to record `matched_clause_ref: null`, conflating "no
clause found" with "clause found but unaddressable".

So `matched_clause_ref`/`clause_ref` accept two addressing forms:

  * UID form -- `clause_uid` (+ `condition_uid`) into a normalized contract.
    Stronger: the UIDs are recomputed from source, so a fabricated one is
    refused. Requires normalization.
  * SOURCE form -- `{document_id, page, quote}`, verified against the processed
    text. Requires nothing but a processed document.

These tests pin the property that matters: the source form is a REAL
verification, not an escape hatch. A quote that does not appear verbatim where
it claims is refused exactly as a fabricated UID is.
"""
import json

import pytest

import dao

QUOTE = "제3조(보상하는 손해) 회사는 법률상의 배상책임을 부담함으로써 입은 손해를 보상합니다."
OTHER = "제1조(사고) 시설의 소유·사용·관리로 생긴 우연한 사고를 말합니다."
TEXT = f"<<<PAGE page=1>>>\n{QUOTE}\n<<<PAGE page=2>>>\n{OTHER}\n"
RAW = b"%PDF-1.7 immutable policy source, never normalized"


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture(autouse=True)
def _fast_locks(monkeypatch):
    monkeypatch.setattr(dao, "LOCK_MAX_WAIT_SECONDS", 0)
    monkeypatch.setattr(dao, "LOCK_POLL_INTERVAL_SECONDS", 0)


@pytest.fixture
def unnormalized_policy(isolated_dao, make_args, canonicalize):
    """A policy document processed to text and deliberately NOT normalized.

    This is the ordinary state under opt-in normalization, not a broken one:
    OCR'd, redacted, canonical UIDs registered -- and no clause contract, no
    audit, no boundary inventory, because none is owed.
    """
    out = isolated_dao / "outputs" / "CASE_030"
    out.mkdir(parents=True)
    _write_json(out / "document_manifest.json", {
        "case_id": "CASE_030",
        "documents": [{
            "document_id": "DOC_001",
            "file_name": "DOC_001.pdf",
            "file_path": "data/raw/CASE_030/DOC_001.pdf",
            "file_format": "pdf",
            "file_size_bytes": 100,
            "ocr_status": "completed",
            "document_type": "insurance_policy",
            "downstream_disposition": "text_only_no_normalization",
            "extraction_method": "embedded_text",
        }],
    })
    raw = isolated_dao / "data" / "raw" / "CASE_030"
    raw.mkdir(parents=True)
    (raw / "DOC_001.pdf").write_bytes(RAW)
    canonicalize(make_args, isolated_dao, "CASE_030", "DOC_001", TEXT)
    _write_json(out / "_run_state.json", {
        "case_id": "CASE_030",
        "run_id": "RUN_20260804_001",
        "stages": [{
            "stage_name": "policy_clause_processing",
            "status": "passed",
            "attempt_count": 1,
            "backup_path": "outputs/CASE_030/_backups/step_01_policy",
        }],
    })
    return out


def _coverage(ref, snapshot=True):
    data = {"coverages": [{"matched_clause_ref": ref}]}
    if snapshot:
        data["upstream_policy_snapshot"] = dao.policy_snapshot_for(
            "CASE_030", ["DOC_001"])
    return data


def _errors(data):
    return dao._downstream_policy_ref_errors(
        "CASE_030", "coverage_result.schema.json", data)


def _source_ref(page=1, quote=QUOTE):
    return {"document_id": "DOC_001", "page": page, "quote": quote}


def test_a_snapshot_issues_for_a_document_with_no_clause_contract(
        unnormalized_policy):
    """The digest records the absent contract as a real state, not a hole."""
    snapshot = dao.policy_snapshot_for("CASE_030", ["DOC_001"])
    assert snapshot["documents"][0]["document_id"] == "DOC_001"
    assert snapshot["snapshot_sha256"]
    hashes, _, _, _ = dao._policy_audit_context("CASE_030", "DOC_001")
    assert hashes["normalized_sha256"] is None
    assert hashes["source_text_sha256"] is not None


def test_a_source_addressed_reference_resolves_without_normalization(
        unnormalized_policy):
    assert _errors(_coverage(_source_ref())) == []


def test_a_quote_that_is_not_on_the_page_is_refused(unnormalized_policy):
    """The core property: the source form verifies, it does not merely accept.

    Without this the opt-in would have bought citability by giving up the
    grounding that made a citation worth anything.
    """
    errors = _errors(_coverage(_source_ref(quote="제99조(존재하지 않는 조항)")))
    assert any("does not appear verbatim" in e for e in errors), errors


def test_a_real_quote_on_the_wrong_page_is_refused(unnormalized_policy):
    """Page and quote are checked together, so neither alone is enough."""
    errors = _errors(_coverage(_source_ref(page=2)))
    assert any("does not appear verbatim" in e for e in errors), errors


def test_a_page_outside_the_document_is_refused(unnormalized_policy):
    errors = _errors(_coverage(_source_ref(page=99)))
    assert any("does not exist in the processed text" in e for e in errors), errors


def test_a_uid_reference_still_requires_a_normalized_contract(
        unnormalized_policy):
    """The stronger form keeps its stronger precondition.

    Opening the source form must not open the UID form: a `PC-…` that resolves
    against nothing is exactly the unverifiable address the UID path exists to
    refuse, and the error names the remedy.
    """
    errors = _errors(_coverage(
        {"document_id": "DOC_001", "clause_uid": "PC-" + "a" * 32}))
    assert any("normalized policy contract is missing" in e for e in errors), errors
    assert any("promote-policy-document" in e for e in errors), errors


def test_a_missing_contract_is_reported_once_not_twice(unnormalized_policy):
    """A missing audit is a consequence of the missing contract, not news.

    Reporting both buried the actionable line under an undiagnostic one.
    """
    errors = _errors(_coverage(
        {"document_id": "DOC_001", "clause_uid": "PC-" + "a" * 32}))
    assert not any("policy audit is missing" in e for e in errors), errors


def test_one_bad_uid_reference_does_not_fail_the_source_ones_beside_it(
        unnormalized_policy):
    """Per-document caching used to leak a UID verdict onto source refs.

    The shared per-document work is cached, but part of the verdict depends on
    the addressing FORM -- so a document cited both ways had every source-form
    reference inherit the UID-form failure and be refused while perfectly well
    grounded.
    """
    data = {
        "coverages": [
            {"matched_clause_ref": {
                "document_id": "DOC_001", "clause_uid": "PC-" + "a" * 32}},
            {"matched_clause_ref": _source_ref()},
        ],
        "upstream_policy_snapshot": dao.policy_snapshot_for(
            "CASE_030", ["DOC_001"]),
    }
    errors = _errors(data)
    assert any("coverages[0]" in e for e in errors), errors
    assert not any("coverages[1]" in e for e in errors), errors


def test_a_source_reference_to_an_unprocessed_document_is_refused(
        unnormalized_policy, isolated_dao):
    """Fail closed: no processed text means nothing can be verified."""
    (isolated_dao / "data" / "processed" / "CASE_030" / "DOC_001"
     / "redacted_text.md").unlink()
    errors = _errors(_coverage(_source_ref()))
    assert any("no processed/redacted text found" in e for e in errors), errors


def _denial(page=1, quote=QUOTE):
    return {
        "denial_reasons": [{
            "reason_id": "DR_1",
            "policy_matches": [{
                "policy_match_id": "PM_1",
                "document_id": "DOC_001",
                "clause_id": "C-1",
                "policy_clause_evidence_references": [
                    {"document_id": "DOC_001", "page": page, "quote": quote}],
            }],
        }],
        "upstream_policy_snapshot": dao.policy_snapshot_for(
            "CASE_030", ["DOC_001"]),
    }


def test_a_denial_policy_match_grounds_itself_one_level_down(
        unnormalized_policy):
    """A policy_match carries its location in policy_clause_evidence_references.

    Regression: the source-form branch read `page`/`quote` off the reference
    object, which is right for `matched_clause_ref`/`clause_ref` (inline) and
    wrong for a policy_match (one level down). Every match on a non-normalized
    document was rejected as "missing page or quote" -- blocking the exact path
    this change exists to open, for every case citing an unpromoted policy
    document. `check_policy_matches` verifies these correctly; this branch must
    not second-guess it from the wrong level.
    """
    assert dao._downstream_policy_ref_errors(
        "CASE_030", "denial_reason_result.schema.json", _denial()) == []


def test_a_denial_policy_match_with_a_fabricated_quote_is_still_refused(
        unnormalized_policy, isolated_dao):
    """Not second-guessing must not become not checking."""
    import _cross_contract
    errors = _cross_contract.check_policy_matches(
        _denial(quote="제99조(존재하지 않는 조항)"),
        isolated_dao / "outputs" / "CASE_030",
        lambda d: dao._redacted_text_for_doc("CASE_030", d))
    assert any("does not appear verbatim" in e for e in errors), errors


def test_the_snapshot_still_detects_a_changed_policy_layer(
        unnormalized_policy):
    """Staleness detection must survive the absent contract.

    The whole point of the snapshot is that a downstream artifact cannot keep
    agreeing with bytes it never read. If dropping normalization also dropped
    this, the source form would be citable but unauditable.
    """
    before = dao.policy_document_digest("CASE_030", "DOC_001")
    ok, message = dao.promote_policy_document(
        "CASE_030", "DOC_001", "standalone_policy", "R04 / PM-1",
        "denial-response", "RUN_20260804_001")
    assert ok, message
    assert dao.policy_document_digest("CASE_030", "DOC_001") != before
