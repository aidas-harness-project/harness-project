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


# ------------------------------------------------------------ match_source ---
# Duplicated into the validation contract (schema v0.3) so a consumer reading
# only that file can tell what `verified` means: the clause exists where
# claimed, NOT that the insurer cited it. Those come apart completely -- on
# CASE_909 all three matches verified byte-exact while the insurer cited no
# policy clause anywhere. A copied field can drift, so it is checked.

def _case_with_sources(tmp_path, denial_source="agent_inferred",
                       acceptance_source="agent_inferred"):
    reasons = {
        "denial_reasons": [{
            "reason_id": "DR_1",
            "decision_type": "denial",
            "policy_matches": [{"policy_match_id": "PM_1",
                                "match_source": denial_source}],
        }],
        "accepted_coverages": [{
            "accepted_coverage_id": "AC_1",
            "coverage_name": "구내치료비",
            "policy_matches": [{"policy_match_id": "PM_3",
                                "match_source": acceptance_source}],
        }],
    }
    (tmp_path / "denial_reason_result.json").write_text(
        json.dumps(reasons, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _full(denial_claim, acceptance_claim):
    return {
        "validations": [{
            "reason_id": "DR_1",
            "policy_match_validations": [
                {"policy_match_id": "PM_1", "match_source": denial_claim}],
        }],
        "acceptance_match_validations": [{
            "accepted_coverage_id": "AC_1",
            "policy_match_validations": [
                {"policy_match_id": "PM_3", "match_source": acceptance_claim}],
        }],
    }


def test_matching_match_source_passes(tmp_path):
    case = _case_with_sources(tmp_path)
    assert _errors(_full("agent_inferred", "agent_inferred"), case) == []


def test_claiming_insurer_cited_for_an_inferred_match_is_rejected(tmp_path):
    """The inversion that matters: crediting the insurer with a policy
    argument it never made. A rebuttal built on it attacks nothing, and the
    insurer can simply disown the position."""
    case = _case_with_sources(tmp_path)
    errors = _errors(_full("insurer_cited", "agent_inferred"), case)
    assert any("PM_1" in e and "insurer_cited" in e and "agent_inferred" in e
               for e in errors), errors


def test_discarding_a_real_insurer_citation_is_rejected(tmp_path):
    """The opposite drift -- the insurer DID cite the clause and the
    validation records otherwise, dropping a rebuttable argument."""
    case = _case_with_sources(tmp_path, denial_source="insurer_cited")
    errors = _errors(_full("agent_inferred", "agent_inferred"), case)
    assert any("PM_1" in e for e in errors), errors


def test_acceptance_side_match_source_is_checked_too(tmp_path):
    """Both sides carry the field through the same $ref, so both are checked
    -- an acceptance's basis gets no exemption for being good news."""
    case = _case_with_sources(tmp_path)
    errors = _errors(_full("agent_inferred", "insurer_cited"), case)
    assert any("AC_1" in e and "PM_3" in e for e in errors), errors


def test_legacy_contract_without_match_source_upstream_is_not_retro_failed(tmp_path):
    """A pre-v0.3 upstream contract records no match_source; demanding
    agreement with a value that never existed would report a legacy shape as
    corruption. The schema requires it on new writes."""
    case = _case(tmp_path)  # no match_source anywhere upstream
    data = _full("agent_inferred", "agent_inferred")
    assert _errors(data, case) == []
