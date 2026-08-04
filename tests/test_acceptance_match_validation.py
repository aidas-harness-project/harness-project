"""An acceptance-owned policy_match must be verified on the same terms as a
denial-owned one (known-gaps item 39).

Before this, check_denial_validation_result built owner_of_match from
denial_reasons alone, so a match owned by an accepted_coverage:
  - could not be recorded under a denial reason (reason_id is ^DR_[0-9]+$), and
  - raised nothing at all when omitted.
So the contract reported every match verified while one had never been checked
-- the exact failure the completeness message names, allowed by the check that
prints it. CASE_907 shipped in that state: AC_1 owned PM_3 and the validation
contract verified only PM_1/PM_2.
"""
import json

import pytest

import _cross_contract as cc


def _case(tmp_path, accepted_matches=("PM_3",), denial_matches=("PM_1",)):
    reasons = {
        "denial_reasons": [{
            "reason_id": "DR_1",
            "decision_type": "denial",
            "policy_matches": [{"policy_match_id": m} for m in denial_matches],
        }],
        "accepted_coverages": [{
            "accepted_coverage_id": "AC_1",
            "coverage_name": "구내치료비",
            "policy_matches": [{"policy_match_id": m} for m in accepted_matches],
        }],
    }
    (tmp_path / "denial_reason_result.json").write_text(
        json.dumps(reasons, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _errors(data, case_dir):
    # The upstream-hash check is a separate concern with its own tests; these
    # fixtures carry no hash, so filter its finding out to keep the assertions
    # about ownership alone.
    return [e for e in cc.check_denial_validation_result(data, case_dir)
            if "hash" not in e.lower()]


def _denial_ok():
    return {"reason_id": "DR_1", "policy_match_validations": [{"policy_match_id": "PM_1"}]}


def test_omitted_acceptance_match_is_now_an_error(tmp_path):
    """The silent pass: PM_3 exists, is never verified, and nothing complained."""
    case = _case(tmp_path)
    errors = _errors({"validations": [_denial_ok()]}, case)
    assert any("AC_1" in e and "PM_3" in e for e in errors), errors


def test_acceptance_match_verified_in_its_own_array_passes(tmp_path):
    case = _case(tmp_path)
    data = {
        "validations": [_denial_ok()],
        "acceptance_match_validations": [
            {"accepted_coverage_id": "AC_1",
             "policy_match_validations": [{"policy_match_id": "PM_3"}]}
        ],
    }
    assert _errors(data, case) == []


def test_acceptance_match_filed_under_a_denial_reason_is_rejected(tmp_path):
    """Ownership is enforced both ways -- a match may only be verified under
    the owner that holds it, so an acceptance's match cannot be laundered
    through a denial reason to look checked."""
    case = _case(tmp_path)
    data = {"validations": [{"reason_id": "DR_1", "policy_match_validations": [
        {"policy_match_id": "PM_1"}, {"policy_match_id": "PM_3"}]}]}
    errors = _errors(data, case)
    assert any("belongs to 'AC_1'" in e for e in errors), errors


def test_unknown_accepted_coverage_id_is_rejected(tmp_path):
    case = _case(tmp_path)
    data = {
        "validations": [_denial_ok()],
        "acceptance_match_validations": [
            {"accepted_coverage_id": "AC_9", "policy_match_validations": []}],
    }
    errors = _errors(data, case)
    assert any("AC_9" in e and "does not exist" in e for e in errors), errors
    # AC_1's real match is still reported as unverified -- a bogus entry must
    # not stand in for the one that was actually required.
    assert any("AC_1" in e and "PM_3" in e for e in errors), errors


def test_duplicate_acceptance_entries_rejected(tmp_path):
    case = _case(tmp_path)
    entry = {"accepted_coverage_id": "AC_1",
             "policy_match_validations": [{"policy_match_id": "PM_3"}]}
    errors = _errors({"validations": [_denial_ok()],
                      "acceptance_match_validations": [entry, dict(entry)]}, case)
    assert any("more than once" in e for e in errors), errors


def test_same_match_verified_on_both_sides_is_rejected(tmp_path):
    """Cross-owner duplicate: verifying PM_1 under the acceptance too must not
    quietly double-count as coverage."""
    case = _case(tmp_path)
    data = {
        "validations": [_denial_ok()],
        "acceptance_match_validations": [
            {"accepted_coverage_id": "AC_1", "policy_match_validations": [
                {"policy_match_id": "PM_3"}, {"policy_match_id": "PM_1"}]}],
    }
    errors = _errors(data, case)
    assert any("PM_1" in e for e in errors), errors


def test_acceptance_with_no_matches_needs_no_entry(tmp_path):
    """Only an acceptance that actually owns matches requires verification;
    demanding an empty entry otherwise would be noise."""
    case = _case(tmp_path, accepted_matches=())
    assert _errors({"validations": [_denial_ok()]}, case) == []


def test_contract_without_accepted_coverages_is_unaffected(tmp_path):
    """Backward compatibility: every pre-2026-08-04 contract predates the
    field and must keep validating unchanged (CASE_112 verified real)."""
    (tmp_path / "denial_reason_result.json").write_text(json.dumps(
        {"denial_reasons": [{"reason_id": "DR_1", "decision_type": "denial",
                             "policy_matches": [{"policy_match_id": "PM_1"}]}]}),
        encoding="utf-8")
    assert _errors({"validations": [_denial_ok()]}, tmp_path) == []


def test_schema_accepts_acceptance_array_and_rejects_bad_owner_id():
    """The schema side of the same gap: recording an acceptance's verification
    must be expressible, and the id must be an AC_ id."""
    from _validation import load_registry, validate_instance
    schemas, registry = load_registry()
    schema = "denial_validation_result.schema.json"

    base = {
        "validations": [],
        "acceptance_match_validations": [
            {"accepted_coverage_id": "AC_1", "policy_match_validations": []}],
    }
    errors = validate_instance(base, schema, schemas, registry)
    # Other required common fields are missing in this minimal fixture; the
    # assertion is specifically that the new array shape is not the objection.
    assert not any("acceptance_match_validations" in str(e) for e in errors), errors

    bad = json.loads(json.dumps(base))
    bad["acceptance_match_validations"][0]["accepted_coverage_id"] = "DR_1"
    errors2 = validate_instance(bad, schema, schemas, registry)
    assert any("acceptance_match_validations" in str(e) for e in errors2), errors2
